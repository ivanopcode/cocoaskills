from __future__ import annotations

import hashlib
import os

import pytest

from csk.builds import go_v1, toolchain


def _environment(root):
    host = toolchain._Host(goos="linux", goarch="amd64", windows=False)
    layout = toolchain._create_probe_layout(root, host)
    target = toolchain.NativeTarget(
        goos="linux", goarch="amd64", tuning={"GOAMD64": "v1"}
    )
    goroot = root / "goroot"
    environment = toolchain._build_environment(
        toolchain._bootstrap_environment(layout, host), goroot, target
    )
    roots = tuple(path for path in root.iterdir() if path.is_dir())
    return environment, goroot, roots


# The real private environment builder accepts Linux; planted deviations fail
# with the intended protocol diagnostic, without launching a worker.
@pytest.mark.parametrize(("override", "platform", "diagnostic"), [
    ({}, "linux", None),
    ({"GOOS": "darwin"}, "linux", "native target"),
    ({"GOOS": "windows"}, "linux", "native target"),
    ({"GOARCH": "unknown"}, "linux", "tuning variable"),
    ({"GOAMD64": ""}, "linux", "tuning variable"),
    ({"CC": "/outside/cc"}, "linux", "forbidden variables"),
    ({"GOARM64": "v8.0"}, "linux", "forbidden variables"),
    ({"CGO_ENABLED": "1"}, "linux", "unexpected CGO_ENABLED"),
    ({}, "unknown", "unsupported platform"),
])
def test_linux_private_environment(tmp_path, override, platform, diagnostic):
    environment, goroot, roots = _environment(tmp_path)
    environment.update(override)
    if diagnostic is None:
        go_v1._validate_worker_environment(environment, goroot, roots, platform)
    else:
        with pytest.raises(go_v1.GoV1Error, match=diagnostic) as error:
            go_v1._validate_worker_environment(environment, goroot, roots, platform)
        assert error.value.code == go_v1.CODE_WORKER_PROTOCOL_INVALID


# Format magic only: not ELF structure, machine/type, loadability or execution.
# The positive control prevents reject-all success.
@pytest.mark.parametrize("payload", [
    b"\x7fELFnot executed", b"MZpe", b"\xcf\xfa\xed\xfe",
    b"", b"\x7f", b"\x7fE", b"\x7fEL", b"#!/bin/sh\n",
])
def test_linux_artifact_format(tmp_path, monkeypatch, payload):
    def forbidden(*args, **kwargs):
        pytest.fail("artifact verification attempted execution")
    monkeypatch.setattr(go_v1.subprocess, "Popen", forbidden)
    stage = tmp_path / "stage"
    output = stage / "bin" / "tool"
    output.parent.mkdir(parents=True)
    output.write_bytes(payload)
    if payload.startswith(b"\x7fELF"):
        result = go_v1._verify_artifact(stage, output, "bin/tool", 100, "linux")
        assert result.sha256 == "sha256:" + hashlib.sha256(payload).hexdigest()
        assert result.size == len(payload)
    else:
        with pytest.raises(go_v1.GoV1Error, match="not a native executable") as error:
            go_v1._verify_artifact(stage, output, "bin/tool", 100, "linux")
        assert error.value.code == "artifact_output_invalid"
    assert output.read_bytes() == payload


# Each forbidden storage shape has the same valid single-link ELF control.
@pytest.mark.parametrize(("kind", "code"), [
    ("regular", None), ("symlink", "artifact_special_file"),
    ("hardlink", "artifact_special_file"), ("oversize", "artifact_size_limit"),
])
def test_linux_artifact_storage(tmp_path, kind, code):
    stage = tmp_path / "stage"
    output = stage / "bin" / "tool"
    output.parent.mkdir(parents=True)
    original = tmp_path / "original"
    original.write_bytes(b"\x7fELFpayload")
    if kind == "symlink":
        output.symlink_to(original)
    elif kind == "hardlink":
        os.link(original, output)
    else:
        output.write_bytes(original.read_bytes())
    limit = 4 if kind == "oversize" else 100
    if code is None:
        assert go_v1._verify_artifact(stage, output, "bin/tool", limit, "linux").size == 11
    else:
        with pytest.raises(go_v1.GoV1Error) as error:
            go_v1._verify_artifact(stage, output, "bin/tool", limit, "linux")
        assert error.value.code == code
    assert original.read_bytes() == b"\x7fELFpayload"


# Actual bytes discriminate Linux tool launchers; a flag alone is insufficient.
@pytest.mark.parametrize("payload", [b"\x7fELFpayload", b"MZpe", b"\xcf\xfa\xed\xfe", b"\x7fEL"])
def test_linux_toolchain_header(payload):
    host = toolchain._Host(goos="linux", goarch="amd64", windows=False)
    assert toolchain._native_executable_header(payload, host) == payload.startswith(b"\x7fELF")


# Unknown platforms cannot fall through to the macOS artifact policy.
def test_unknown_artifact_platform():
    assert go_v1._native_executable_header(b"\xcf\xfa\xed\xfe", "macos")
    assert go_v1._native_executable_header(b"MZ", "windows")
    assert not go_v1._native_executable_header(b"\xcf\xfa\xed\xfe", "unknown")


# Opt-in native integration: Go 1.27 compiles a vendored import with the actual
# closed private environment. The ELF output is hashed, never executed. This
# does not qualify worker launch, containment, publication or public admission.
@pytest.mark.skipif(not os.environ.get("CSK_TEST_LINUX_GO"), reason="explicit native Go required")
def test_linux_real_vendored_build(tmp_path):
    import subprocess
    from pathlib import Path

    executable = Path(os.environ["CSK_TEST_LINUX_GO"]).resolve(strict=True)
    host = toolchain._native_host()
    assert host.goos == "linux"
    root = tmp_path / "source"
    root.mkdir()
    (root / "go.mod").write_text(
        "module example.test/tool\ngo 1.27\nrequire example.test/dep v1.0.0\n"
    )
    (root / "main.go").write_text(
        'package main\nimport "example.test/dep"\nfunc main() { println(dep.Value) }\n'
    )
    vendor = root / "vendor"
    dep = vendor / "example.test" / "dep"
    dep.mkdir(parents=True)
    (dep / "dep.go").write_text('package dep\nconst Value = "vendored"\n')
    (vendor / "modules.txt").write_text(
        "# example.test/dep v1.0.0\n## explicit; go 1.27\nexample.test/dep\n"
    )
    private = tmp_path / "private"
    private.mkdir(mode=0o700)
    config = toolchain.ToolchainConfig(
        private_base=private,
        operator_search_path=toolchain.OperatorSearchPath((str(executable.parent),)),
        forbidden_roots=(root,), go_executable=executable,
        go_future_families="refuse",
    )
    with toolchain.establish_toolchain(config) as session:
        assert "go1.27." in session.toolchain.go_version
        assert session.target.goos == host.goos
        assert session.target.goarch == host.goarch
        assert set(session.target.tuning) == {toolchain.TUNING_VARIABLES[host.goarch]}
        environment = dict(session.environment)
        roots = tuple(Path(environment[name]) for name in (
            "GOPATH", "GOMODCACHE", "GOCACHE", "GOTMPDIR", "HOME",
            "XDG_CONFIG_HOME", "PATH", "TMPDIR",
        ))
        go_v1._validate_worker_environment(environment, session.goroot, roots, "linux")
        listed = subprocess.run(
            [str(executable), *go_v1.LIST_ARGUMENTS], cwd=root,
            env=environment, capture_output=True, check=True,
        )
        go_v1.validate_package_graph(
            listed.stdout, build_root=root, source_dir=root, goroot=session.goroot,
        )
        stage = tmp_path / "stage"
        output = stage / "bin" / "tool"
        output.parent.mkdir(parents=True)
        subprocess.run(
            [str(executable), *go_v1.BUILD_ARGUMENT_PREFIX, str(output), "."],
            cwd=root, env=environment, capture_output=True, check=True,
        )
        result = go_v1._verify_artifact(stage, output, "bin/tool", 16 * 1024 * 1024, "linux")
        assert output.read_bytes().startswith(b"\x7fELF")
        assert result.size > 4
        session.verify()
