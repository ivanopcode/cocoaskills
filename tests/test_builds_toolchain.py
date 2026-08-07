from __future__ import annotations

import base64
import hashlib
import json
import os
import sys
import time
from pathlib import Path

import pytest

from csk.builds import toolchain


_TUNING_VALUES = {
    "GO386": "sse2",
    "GOAMD64": "v1",
    "GOARM": "7",
    "GOARM64": "v8.0",
    "GOMIPS": "hardfloat",
    "GOMIPS64": "hardfloat",
    "GOPPC64": "power8",
    "GORISCV64": "rva20u64",
    "GOWASM": "satconv,signext",
}


class RecordingRunner:
    def __init__(
        self,
        goroot: Path,
        *,
        version: str | None = None,
        environment_overrides: dict[str, str] | None = None,
    ):
        self.goroot = goroot
        self.host = toolchain._native_host()
        self.version = version or (
            f"go version go1.25.5 {self.host.goos}/{self.host.goarch}\n"
        )
        self.environment_overrides = environment_overrides or {}
        self.calls: list[tuple[tuple[str, ...], Path, dict[str, str]]] = []
        self.operation_roots: list[Path] = []
        self.env_payload: bytes | None = None
        self.returncodes: dict[tuple[str, ...], int] = {}
        self.poison_working_directory = False

    def run(
        self,
        argv: tuple[str, ...],
        *,
        cwd: Path,
        environment: dict[str, str],
        timeout: float,
        output_limit: int,
    ) -> toolchain.ProbeResult:
        del timeout, output_limit
        copied_environment = dict(environment)
        self.calls.append((argv, cwd, copied_environment))
        self.operation_roots.append(cwd.parent)
        arguments = argv[1:]
        returncode = self.returncodes.get(arguments, 0)
        if arguments == ("telemetry", "off"):
            _telemetry_dir(copied_environment, self.host).mkdir(
                parents=True,
                exist_ok=True,
            )
            return toolchain.ProbeResult(returncode=returncode)
        if arguments == ("version",):
            if self.poison_working_directory:
                (cwd / "package-controlled").write_text("poison", encoding="utf-8")
            return toolchain.ProbeResult(
                stdout=self.version.encode("utf-8"),
                returncode=returncode,
            )
        if arguments == ("env", "-json", *toolchain.GO_ENV_FIELDS):
            payload = self.env_payload
            if payload is None:
                values = _probe_environment(
                    self.goroot,
                    copied_environment,
                    self.host,
                )
                values.update(self.environment_overrides)
                payload = json.dumps(values, separators=(",", ":")).encode("utf-8")
            return toolchain.ProbeResult(stdout=payload, returncode=returncode)
        raise AssertionError(f"unexpected Go probe argv: {argv!r}")


class RepointingConfigRunner(RecordingRunner):
    def __init__(
        self,
        goroot: Path,
        outside_config: Path,
        repoint_on: tuple[str, ...],
    ):
        super().__init__(goroot)
        self.outside_config = outside_config
        self.repoint_on = repoint_on

    def run(
        self,
        argv: tuple[str, ...],
        *,
        cwd: Path,
        environment: dict[str, str],
        timeout: float,
        output_limit: int,
    ) -> toolchain.ProbeResult:
        if argv[1:] == self.repoint_on:
            config = _telemetry_dir(environment, self.host).parents[1]
            config.rename(config.with_name(f".{config.name}-original"))
            config.symlink_to(self.outside_config, target_is_directory=True)
        return super().run(
            argv,
            cwd=cwd,
            environment=environment,
            timeout=timeout,
            output_limit=output_limit,
        )


def _telemetry_dir(environment: dict[str, str], host: toolchain._Host) -> Path:
    if host.windows:
        config = Path(environment["APPDATA"])
    elif host.goos == "darwin":
        config = Path(environment["HOME"]) / "Library" / "Application Support"
    else:
        config = Path(environment["XDG_CONFIG_HOME"])
    return config / "go" / "telemetry"


def _probe_environment(
    goroot: Path,
    bootstrap: dict[str, str],
    host: toolchain._Host,
) -> dict[str, str]:
    values = {name: "" for name in toolchain.GO_ENV_FIELDS}
    values.update(_TUNING_VALUES)
    values.update(
        {
            "GOROOT": str(goroot),
            "GOHOSTOS": host.goos,
            "GOHOSTARCH": host.goarch,
            "GOOS": host.goos,
            "GOARCH": host.goarch,
            "GOTELEMETRY": "off",
            "GOTELEMETRYDIR": str(_telemetry_dir(bootstrap, host)),
        }
    )
    return values


def _native_header(host: toolchain._Host) -> bytes:
    if host.windows:
        return b"MZ\x90\x00"
    if host.goos == "darwin":
        return b"\xcf\xfa\xed\xfe"
    return b"\x7fELF"


def _make_goroot(path: Path) -> Path:
    host = toolchain._native_host()
    executable = path / "bin" / ("go.exe" if host.windows else "go")
    executable.parent.mkdir(parents=True)
    executable.write_bytes(_native_header(host) + b"fake-go")
    executable.chmod(0o755)
    (path / "VERSION").write_text("go1.25.5\n", encoding="utf-8")
    return path


def _setup(
    tmp_path: Path,
    *,
    version: str | None = None,
    environment_overrides: dict[str, str] | None = None,
) -> tuple[toolchain.ToolchainConfig, RecordingRunner, Path, Path]:
    goroot = _make_goroot(tmp_path / "trusted-go")
    forbidden = tmp_path / "repository"
    forbidden.mkdir()
    private_base = tmp_path / "private"
    private_base.mkdir()
    runner = RecordingRunner(
        goroot,
        version=version,
        environment_overrides=environment_overrides,
    )
    config = toolchain.ToolchainConfig(
        private_base=private_base,
        operator_search_path=toolchain.OperatorSearchPath((str(goroot / "bin"),)),
        forbidden_roots=(forbidden,),
        runner=runner,
    )
    return config, runner, goroot, private_base


def _assert_code(expected: str, raised: pytest.ExceptionInfo[toolchain.ToolchainError]) -> None:
    assert raised.value.code == expected


def test_establish_uses_only_exact_bootstrap_argv_and_clean_environment(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    config, runner, goroot, _ = _setup(tmp_path)
    monkeypatch.setenv("GOROOT", "/attacker/goroot")
    monkeypatch.setenv("GOFLAGS", "-tags=attacker")
    monkeypatch.setenv("CC", "/attacker/cc")
    monkeypatch.setenv("GOTOOLCHAIN", "auto")

    session = toolchain.establish_toolchain(config)
    operation_root = session.operation_root
    executable = str((goroot / "bin" / ("go.exe" if os.name == "nt" else "go")).resolve())

    assert [call[0] for call in runner.calls] == [
        (executable, "telemetry", "off"),
        (executable, "version"),
        (executable, "env", "-json", *toolchain.GO_ENV_FIELDS),
    ]
    assert len({call[1] for call in runner.calls}) == 1
    probe_cwd = runner.calls[0][1]
    assert probe_cwd == operation_root / "empty"
    assert not probe_cwd.samefile(goroot)
    for _, _, environment in runner.calls:
        assert "GOROOT" not in environment
        assert "GOOS" not in environment
        assert "GOARCH" not in environment
        assert "GOFLAGS" not in environment
        assert "CC" not in environment
        assert environment["GOTOOLCHAIN"] == "local"
        assert environment["GOENV"] == "off"
        assert environment["PATH"] == str(operation_root / "empty-path")
        assert environment["LC_ALL"] == "C"
        assert environment["LANG"] == "C"
        for key in (
            "GOPATH",
            "GOMODCACHE",
            "GOCACHE",
            "GOTMPDIR",
            "HOME",
            "XDG_CONFIG_HOME",
            "TMPDIR",
        ):
            assert Path(environment[key]).is_relative_to(operation_root)

    applicable = toolchain.TUNING_VARIABLES[session.target.goarch]
    assert dict(session.target.tuning) == {applicable: _TUNING_VALUES[applicable]}
    frozen = session.environment
    assert frozen["GOROOT"] == str(goroot.resolve())
    assert frozen["GOOS"] == session.target.goos
    assert frozen["GOARCH"] == session.target.goarch
    assert {key for key in toolchain.TUNING_VARIABLES.values() if key in frozen} == {
        applicable
    }
    assert frozen["GOFLAGS"] == ""
    assert frozen["GOPROXY"] == "off"
    assert frozen["GOTOOLCHAIN"] == "local"
    assert frozen["CGO_ENABLED"] == "0"

    session.close()
    assert not operation_root.exists()


def test_preflight_uses_only_version_and_does_not_allocate_private_state(
    tmp_path: Path,
) -> None:
    config, runner, goroot, private_base = _setup(tmp_path)

    toolchain.preflight_toolchain(config)

    executable = str(
        (goroot / "bin" / ("go.exe" if os.name == "nt" else "go")).resolve()
    )
    assert [call[0] for call in runner.calls] == [(executable, "version")]
    assert runner.calls[0][1] == goroot.resolve()
    assert runner.calls[0][2]["GOENV"] == "off"
    assert runner.calls[0][2]["GOTOOLCHAIN"] == "local"
    assert list(private_base.iterdir()) == []


def test_preflight_rejects_unsupported_family_without_private_state(
    tmp_path: Path,
) -> None:
    config, _runner, _goroot, private_base = _setup(
        tmp_path,
        version="go version go1.24.9 darwin/arm64\n",
    )

    with pytest.raises(toolchain.ToolchainError) as raised:
        toolchain.preflight_toolchain(config)

    _assert_code("unsupported_go_family", raised)
    assert list(private_base.iterdir()) == []


def test_probe_returns_frozen_snapshot_and_removes_private_root(tmp_path: Path):
    config, runner, _, private_base = _setup(tmp_path)

    snapshot = toolchain.probe_toolchain(config)

    assert snapshot.toolchain.algorithm == toolchain.TOOLCHAIN_ALGORITHM
    assert snapshot.toolchain.go_relpath == "bin/go"
    assert snapshot.toolchain.go_version.startswith("go version go1.25.5 ")
    assert snapshot.toolchain.content_sha256.startswith("sha256:")
    assert not list(private_base.glob(".csk-go-probe-*"))
    assert runner.operation_roots
    assert all(not path.exists() for path in runner.operation_roots)
    with pytest.raises(TypeError):
        snapshot.environment["GOFLAGS"] = "-tags=mutated"  # type: ignore[index]


def test_context_manager_removes_private_root(tmp_path: Path):
    config, _, _, _ = _setup(tmp_path)

    with toolchain.establish_toolchain(config) as session:
        operation_root = session.operation_root
        assert operation_root.is_dir()

    assert not operation_root.exists()


@pytest.mark.parametrize(
    ("version", "expected_code"),
    [
        ("go version go1.22.12 {goos}/{goarch}\n", "unsupported_go_family"),
        ("go version go1.26.1 {goos}/{goarch}\n", "unsupported_go_family"),
        ("go version go1.023.1 {goos}/{goarch}\n", "malformed_go_version"),
    ],
)
def test_release_family_fails_closed(
    tmp_path: Path,
    version: str,
    expected_code: str,
):
    host = toolchain._native_host()
    rendered = version.format(goos=host.goos, goarch=host.goarch)
    config, runner, _, private_base = _setup(tmp_path, version=rendered)

    with pytest.raises(toolchain.ToolchainError) as raised:
        toolchain.establish_toolchain(config)

    _assert_code(expected_code, raised)
    assert not list(private_base.glob(".csk-go-probe-*"))
    assert runner.operation_roots
    assert all(not path.exists() for path in runner.operation_roots)


@pytest.mark.parametrize(
    "payload",
    [
        b"",
        b"go version go1.25.5 darwin/arm64",
        b"go version go1.25.5 darwin/arm64\n\n",
        b"go version go1.25.5\rdarwin/arm64\n",
        b"go version go1.25.5 darwin/arm64\x00\n",
        b"\xff\n",
        b"x" * 4096 + b"\n",
    ],
)
def test_version_normalization_rejects_malformed_output(payload: bytes):
    with pytest.raises(toolchain.ToolchainError) as raised:
        toolchain.normalize_go_version(payload)
    _assert_code("malformed_go_version", raised)


def test_version_normalization_accepts_lf_and_crlf():
    expected = "go version go1.25.5 darwin/arm64"
    assert toolchain.normalize_go_version(expected.encode() + b"\n") == expected
    assert toolchain.normalize_go_version(expected.encode() + b"\r\n") == expected


def test_go_env_requires_exact_unique_string_fields(tmp_path: Path):
    config, runner, goroot, _ = _setup(tmp_path)
    host = toolchain._native_host()
    values = _probe_environment(
        goroot,
        {
            "HOME": str(tmp_path / "unused-home"),
            "XDG_CONFIG_HOME": str(tmp_path / "unused-config"),
            "APPDATA": str(tmp_path / "unused-appdata"),
        },
        host,
    )
    duplicate = (
        b'{"GOROOT":"'
        + str(goroot).encode()
        + b'","GOROOT":"'
        + str(goroot).encode()
        + b'"}'
    )
    cases = [
        duplicate,
        json.dumps({key: value for key, value in values.items() if key != "GOOS"}).encode(),
        json.dumps(values | {"UNKNOWN": ""}).encode(),
        json.dumps(values | {"GOOS": 1}).encode(),
        b"[]",
        b"{",
    ]
    for payload in cases:
        runner.env_payload = payload
        with pytest.raises(toolchain.ToolchainError) as raised:
            toolchain.establish_toolchain(config)
        _assert_code("invalid_go_env", raised)


def test_mismatched_goroot_fails_closed_and_cleans_up(tmp_path: Path):
    other_root = _make_goroot(tmp_path / "other-go")
    config, runner, _, private_base = _setup(
        tmp_path,
        environment_overrides={"GOROOT": str(other_root)},
    )

    with pytest.raises(toolchain.ToolchainError) as raised:
        toolchain.establish_toolchain(config)

    _assert_code("toolchain_executable_mismatch", raised)
    assert all(not path.exists() for path in runner.operation_roots)
    assert not list(private_base.glob(".csk-go-probe-*"))


@pytest.mark.parametrize("field", ["GOHOSTOS", "GOOS", "GOHOSTARCH", "GOARCH"])
def test_host_target_and_version_must_be_identical(tmp_path: Path, field: str):
    config, _, _, _ = _setup(
        tmp_path,
        environment_overrides={field: "mismatch"},
    )
    with pytest.raises(toolchain.ToolchainError) as raised:
        toolchain.establish_toolchain(config)
    _assert_code("target_mismatch", raised)


def test_telemetry_mode_and_directory_are_verified(tmp_path: Path):
    outside = tmp_path / "outside-telemetry"
    outside.mkdir()
    for overrides, expected in [
        ({"GOTELEMETRY": "local"}, "telemetry_initialization_failed"),
        ({"GOTELEMETRYDIR": str(outside)}, "telemetry_directory_untrusted"),
    ]:
        config, _, _, _ = _setup(
            tmp_path / expected,
            environment_overrides=overrides,
        )
        with pytest.raises(toolchain.ToolchainError) as raised:
            toolchain.establish_toolchain(config)
        _assert_code(expected, raised)


@pytest.mark.skipif(os.name == "nt", reason="unprivileged Windows symlinks are not portable")
@pytest.mark.parametrize(
    "repoint_on",
    [
        ("telemetry", "off"),
        ("version",),
        ("env", "-json", *toolchain.GO_ENV_FIELDS),
    ],
)
def test_platform_config_repoint_outside_private_root_fails_closed(
    tmp_path: Path,
    repoint_on: tuple[str, ...],
):
    config, _, goroot, private_base = _setup(tmp_path)
    outside_config = tmp_path / "outside-config"
    (outside_config / "go" / "telemetry").mkdir(parents=True)
    runner = RepointingConfigRunner(goroot, outside_config, repoint_on)
    config = toolchain.ToolchainConfig(
        private_base=config.private_base,
        operator_search_path=config.operator_search_path,
        forbidden_roots=config.forbidden_roots,
        runner=runner,
    )
    session: toolchain.ToolchainSession | None = None
    try:
        with pytest.raises(toolchain.ToolchainError) as raised:
            session = toolchain.establish_toolchain(config)
    finally:
        if session is not None:
            session.release()

    _assert_code("telemetry_directory_untrusted", raised)
    assert runner.operation_roots
    assert all(not root.exists() for root in runner.operation_roots)
    assert not list(private_base.glob(".csk-go-probe-*"))
    assert (outside_config / "go" / "telemetry").is_dir()


def test_empty_or_malformed_native_tuning_is_rejected(tmp_path: Path):
    host = toolchain._native_host()
    tuning_name = toolchain.TUNING_VARIABLES[host.goarch]
    for value in ("", "bad\nvalue", "x" * 8193):
        config, _, _, _ = _setup(
            tmp_path / str(len(value)),
            environment_overrides={tuning_name: value},
        )
        with pytest.raises(toolchain.ToolchainError) as raised:
            toolchain.establish_toolchain(config)
        _assert_code("target_mismatch", raised)


@pytest.mark.parametrize(
    ("goarch", "expected"),
    sorted(toolchain.TUNING_VARIABLES.items()),
)
def test_each_closed_architecture_freezes_exactly_one_tuning(
    goarch: str,
    expected: str,
):
    values = {name: value for name, value in _TUNING_VALUES.items()}
    values.update({"GOOS": "test", "GOARCH": goarch})
    target = toolchain._target_from_probe(values)
    assert dict(target.tuning) == {expected: _TUNING_VALUES[expected]}


def test_unknown_architecture_has_no_implicit_tuning():
    with pytest.raises(toolchain.ToolchainError) as raised:
        toolchain._target_from_probe({"GOOS": "test", "GOARCH": "future"})
    _assert_code("target_mismatch", raised)


def test_capture_is_immutable_across_project_path_augmentation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    config, runner, goroot, _ = _setup(tmp_path)
    captured = toolchain.capture_operator_search_path(
        {"PATH": str(goroot / "bin")}
    )
    project_bin = tmp_path / "repository" / ".agents" / "bin"
    project_bin.mkdir(parents=True)
    shim = project_bin / ("go.exe" if os.name == "nt" else "go")
    shim.write_text("#!/bin/sh\nexit 1\n", encoding="utf-8")
    shim.chmod(0o755)
    monkeypatch.setenv(
        "PATH",
        str(project_bin) + os.pathsep + str(goroot / "bin"),
    )
    config = toolchain.ToolchainConfig(
        private_base=config.private_base,
        operator_search_path=captured,
        forbidden_roots=config.forbidden_roots,
        runner=runner,
    )

    snapshot = toolchain.probe_toolchain(config)

    assert snapshot.executable == (goroot / "bin" / shim.name).resolve()


def test_repository_or_project_managed_candidate_is_rejected(tmp_path: Path):
    repository = tmp_path / "repository"
    goroot = _make_goroot(repository / ".agents" / "toolchains" / "go")
    private_base = tmp_path / "private"
    private_base.mkdir()
    runner = RecordingRunner(goroot)
    config = toolchain.ToolchainConfig(
        private_base=private_base,
        operator_search_path=toolchain.OperatorSearchPath((str(goroot / "bin"),)),
        forbidden_roots=(repository,),
        runner=runner,
    )

    with pytest.raises(toolchain.ToolchainError) as raised:
        toolchain.establish_toolchain(config)

    _assert_code("untrusted_go_executable", raised)
    assert not runner.calls


def test_relative_or_empty_captured_path_entry_fails_closed(tmp_path: Path):
    config, runner, _, _ = _setup(tmp_path)
    for entries in [("", str(tmp_path)), (".", str(tmp_path))]:
        unsafe = toolchain.ToolchainConfig(
            private_base=config.private_base,
            operator_search_path=toolchain.OperatorSearchPath(entries),
            forbidden_roots=config.forbidden_roots,
            runner=runner,
        )
        with pytest.raises(toolchain.ToolchainError) as raised:
            toolchain.establish_toolchain(unsafe)
        _assert_code("untrusted_operator_path", raised)


def test_wrapper_is_rejected_before_any_probe(tmp_path: Path):
    root = tmp_path / "wrapped"
    executable = root / "bin" / ("go.exe" if os.name == "nt" else "go")
    executable.parent.mkdir(parents=True)
    executable.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    executable.chmod(0o755)
    forbidden = tmp_path / "repository"
    forbidden.mkdir()
    private_base = tmp_path / "private"
    private_base.mkdir()
    runner = RecordingRunner(root)
    config = toolchain.ToolchainConfig(
        private_base=private_base,
        operator_search_path=toolchain.OperatorSearchPath((str(root / "bin"),)),
        forbidden_roots=(forbidden,),
        runner=runner,
    )

    with pytest.raises(toolchain.ToolchainError) as raised:
        toolchain.establish_toolchain(config)

    _assert_code("untrusted_go_executable", raised)
    assert not runner.calls


@pytest.mark.skipif(os.name == "nt", reason="unprivileged Windows symlinks are not portable")
def test_outside_launcher_symlink_resolves_to_real_goroot_binary(tmp_path: Path):
    config, runner, goroot, _ = _setup(tmp_path)
    operator_bin = tmp_path / "operator-bin"
    operator_bin.mkdir()
    (operator_bin / "go").symlink_to(goroot / "bin" / "go")
    linked = toolchain.ToolchainConfig(
        private_base=config.private_base,
        operator_search_path=toolchain.OperatorSearchPath((str(operator_bin),)),
        forbidden_roots=config.forbidden_roots,
        runner=runner,
    )

    snapshot = toolchain.probe_toolchain(linked)

    assert snapshot.executable == (goroot / "bin" / "go").resolve()
    assert snapshot.goroot == goroot.resolve()


def test_private_probe_base_cannot_be_project_managed(tmp_path: Path):
    repository = tmp_path / "repository"
    repository.mkdir()
    private_base = repository / ".agents" / "tmp"
    private_base.mkdir(parents=True)
    goroot = _make_goroot(tmp_path / "go")
    runner = RecordingRunner(goroot)
    config = toolchain.ToolchainConfig(
        private_base=private_base,
        operator_search_path=toolchain.OperatorSearchPath((str(goroot / "bin"),)),
        forbidden_roots=(repository,),
        runner=runner,
    )
    with pytest.raises(toolchain.ToolchainError) as raised:
        toolchain.establish_toolchain(config)
    _assert_code("private_probe_failed", raised)
    assert not runner.calls


def test_nonzero_probe_exit_is_stable_and_private(tmp_path: Path):
    config, runner, _, private_base = _setup(tmp_path)
    runner.returncodes[("telemetry", "off")] = 7
    with pytest.raises(toolchain.ToolchainError) as raised:
        toolchain.establish_toolchain(config)
    _assert_code("telemetry_initialization_failed", raised)
    assert not list(private_base.glob(".csk-go-probe-*"))


def test_default_runner_closes_stdin_and_shares_bounded_output_budget(
    tmp_path: Path,
):
    runner = toolchain.SubprocessProbeRunner()
    closed_stdin = runner.run(
        (
            sys.executable,
            "-c",
            "import sys; print(sys.stdin.buffer.read() == b'')",
        ),
        cwd=tmp_path,
        environment={},
        timeout=2,
        output_limit=64,
    )
    expected_newline = b"\r\n" if os.name == "nt" else b"\n"
    assert closed_stdin.stdout == b"True" + expected_newline

    with pytest.raises(toolchain.ToolchainError) as raised:
        runner.run(
            (
                sys.executable,
                "-c",
                "import sys; sys.stdout.write('a'*10); sys.stderr.write('b'*10)",
            ),
            cwd=tmp_path,
            environment={},
            timeout=2,
            output_limit=15,
        )
    _assert_code("process_output_limit", raised)


def test_default_runner_enforces_deadline(tmp_path: Path):
    runner = toolchain.SubprocessProbeRunner()
    with pytest.raises(toolchain.ToolchainError) as raised:
        runner.run(
            (
                sys.executable,
                "-c",
                "import time; time.sleep(1)",
            ),
            cwd=tmp_path,
            environment={},
            timeout=0.01,
            output_limit=64,
        )
    _assert_code("process_timeout", raised)


def test_probe_must_not_modify_manager_owned_empty_directory(tmp_path: Path):
    config, runner, _, _ = _setup(tmp_path)
    runner.poison_working_directory = True
    with pytest.raises(toolchain.ToolchainError) as raised:
        toolchain.establish_toolchain(config)
    _assert_code("process_environment_poisoned", raised)
    assert all(not root.exists() for root in runner.operation_roots)


@pytest.mark.skipif(os.name == "nt", reason="unprivileged Windows symlinks are not portable")
def test_shared_toolchain_vector_is_byte_exact_and_input_order_independent(
    tmp_path: Path,
):
    root = tmp_path / "goroot"
    (root / "bin").mkdir(parents=True)
    (root / "pkg").mkdir()
    (root / "bin" / "go").write_bytes(b"GO")
    (root / "pkg" / "tool-link").symlink_to("../bin/go")
    expected = "sha256:baf7c5f3b9c3f1fae3da4c356381bf74442aa7f8f0b6fb2304c9c10833d6032e"

    identity = toolchain.fingerprint_toolchain(
        root.resolve(),
        b"go version go1.25.5 darwin/arm64\n",
    )

    assert identity == toolchain.ToolchainIdentity(
        algorithm="curator-go-toolchain-v1",
        content_sha256=expected,
        go_relpath="bin/go",
        go_version="go version go1.25.5 darwin/arm64",
    )


def test_shared_toolchain_preimage_is_byte_exact():
    version = b"go version go1.25.5 darwin/arm64"
    preimage = (
        b"curator-go-toolchain-v1\x00"
        + toolchain._framed_record_header("D", b"bin", 0)
        + toolchain._framed_record_header("F", b"bin/go", 2)
        + b"GO"
        + toolchain._framed_record_header("D", b"pkg", 0)
        + toolchain._framed_record_header("L", b"pkg/tool-link", 9)
        + b"../bin/go"
        + toolchain._framed_record_header("V", b"", len(version))
        + version
    )
    expected_preimage = base64.b64decode(
        "Y3VyYXRvci1nby10b29sY2hhaW4tdjEARAAAAAAAAAADYmluAAAAAAAAAABG"
        "AAAAAAAAAAZiaW4vZ28AAAAAAAAAAkdPRAAAAAAAAAADcGtnAAAAAAAAAABM"
        "AAAAAAAAAA1wa2cvdG9vbC1saW5rAAAAAAAAAAkuLi9iaW4vZ29WAAAAAAAA"
        "AAAAAAAAAAAAIGdvIHZlcnNpb24gZ28xLjI1LjUgZGFyd2luL2FybTY0"
    )
    assert preimage == expected_preimage
    assert (
        "sha256:" + hashlib.sha256(preimage).hexdigest()
        == "sha256:baf7c5f3b9c3f1fae3da4c356381bf74442aa7f8f0b6fb2304c9c10833d6032e"
    )


@pytest.mark.skipif(os.name == "nt", reason="unprivileged Windows symlinks are not portable")
def test_lf_crlf_mode_and_timestamp_are_identity_non_inputs(tmp_path: Path):
    root = tmp_path / "goroot"
    (root / "bin").mkdir(parents=True)
    executable = root / "bin" / "go"
    executable.write_bytes(b"GO")
    lf = toolchain.fingerprint_toolchain(
        root.resolve(),
        b"go version go1.25.5 darwin/arm64\n",
    )
    executable.chmod(0o555)
    os.utime(executable, (1_893_456_000, 1_893_456_000))
    crlf = toolchain.fingerprint_toolchain(
        root.resolve(),
        b"go version go1.25.5 darwin/arm64\r\n",
    )
    assert lf == crlf


def test_file_and_directory_bytes_are_framed_and_mutate_identity(tmp_path: Path):
    root = tmp_path / "goroot"
    (root / "a").mkdir(parents=True)
    payload = root / "a" / "file"
    payload.write_bytes(b"one")
    first = toolchain.fingerprint_toolchain(root.resolve(), b"version\n")
    payload.write_bytes(b"two")
    second = toolchain.fingerprint_toolchain(root.resolve(), b"version\n")
    (root / "empty").mkdir()
    third = toolchain.fingerprint_toolchain(root.resolve(), b"version\n")
    assert first.content_sha256 != second.content_sha256
    assert second.content_sha256 != third.content_sha256


def test_tree_scan_does_not_use_cached_direntry_stat(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    root = tmp_path / "goroot"
    (root / "nested").mkdir(parents=True)
    (root / "nested" / "file").write_bytes(b"content")
    original_scandir = os.scandir

    class EntryWithoutPhysicalIdentity:
        def __init__(self, name: str):
            self.name = name

        def stat(self, *, follow_symlinks: bool = True) -> os.stat_result:
            del follow_symlinks
            raise AssertionError("toolchain scan must use os.lstat for physical identity")

    class ScandirWithoutPhysicalIdentity:
        def __init__(self, names: list[str]):
            self._entries = [EntryWithoutPhysicalIdentity(name) for name in names]

        def __enter__(self) -> object:
            return iter(self._entries)

        def __exit__(
            self,
            exc_type: object,
            exc: object,
            traceback: object,
        ) -> None:
            del exc_type, exc, traceback

    def scandir_without_physical_identity(path: os.PathLike[str]) -> object:
        with original_scandir(path) as entries:
            names = [entry.name for entry in entries]
        return ScandirWithoutPhysicalIdentity(names)

    monkeypatch.setattr(os, "scandir", scandir_without_physical_identity)

    identity = toolchain.fingerprint_toolchain(root.resolve(), b"version\n")
    assert identity.content_sha256.startswith("sha256:")


@pytest.mark.skipif(os.name == "nt", reason="unprivileged Windows symlinks are not portable")
@pytest.mark.parametrize(
    ("target", "expected_code"),
    [
        ("/outside", "toolchain_link_absolute"),
        ("../../outside", "toolchain_link_escape"),
        ("missing", "toolchain_link_dangling"),
    ],
)
def test_absolute_escaping_and_dangling_links_fail_closed(
    tmp_path: Path,
    target: str,
    expected_code: str,
):
    root = tmp_path / "goroot"
    (root / "pkg").mkdir(parents=True)
    (root / "pkg" / "link").symlink_to(target)
    with pytest.raises(toolchain.ToolchainError) as raised:
        toolchain.fingerprint_toolchain(root.resolve(), b"version\n")
    _assert_code(expected_code, raised)


@pytest.mark.skipif(os.name == "nt", reason="mkfifo is unavailable on Windows")
def test_special_file_is_rejected(tmp_path: Path):
    root = tmp_path / "goroot"
    root.mkdir()
    os.mkfifo(root / "fifo")
    with pytest.raises(toolchain.ToolchainError) as raised:
        toolchain.fingerprint_toolchain(root.resolve(), b"version\n")
    _assert_code("special_file_forbidden", raised)


def test_duplicate_and_invalid_protocol_paths_are_rejected(tmp_path: Path):
    root = tmp_path / "goroot"
    root.mkdir()
    payload = root / "file"
    payload.write_bytes(b"x")
    info = payload.lstat()
    record = toolchain._TreeRecord(
        protocol_path="file",
        path_bytes=b"file",
        native_path=payload,
        kind="F",
        initial_stat=info,
    )
    with pytest.raises(toolchain.ToolchainError) as duplicate:
        toolchain._canonical_records([record, record])
    _assert_code("duplicate_path", duplicate)
    for invalid in ("", ".", "../escape", "a//b", "bad\udcff"):
        with pytest.raises(toolchain.ToolchainError) as malformed:
            toolchain._protocol_path_bytes(invalid)
        _assert_code("invalid_unicode", malformed)


def test_launcher_must_be_regular_and_executable(tmp_path: Path):
    if os.name == "nt":
        pytest.skip("POSIX executable-mode assertion")
    config, runner, goroot, _ = _setup(tmp_path)
    executable = goroot / "bin" / "go"
    executable.chmod(0o644)
    with pytest.raises(toolchain.ToolchainError) as raised:
        toolchain.establish_toolchain(config)
    _assert_code("untrusted_go_executable", raised)
    assert not runner.calls


def test_tree_mutation_before_close_fails_and_still_deletes_private_state(
    tmp_path: Path,
):
    config, _, goroot, _ = _setup(tmp_path)
    session = toolchain.establish_toolchain(config)
    operation_root = session.operation_root
    (goroot / "VERSION").write_text("go1.25.6\n", encoding="utf-8")

    with pytest.raises(toolchain.ToolchainError) as raised:
        session.close()

    _assert_code("toolchain_mutated", raised)
    assert not operation_root.exists()


def test_release_cleans_without_second_fingerprint(tmp_path: Path):
    config, _, goroot, _ = _setup(tmp_path)
    session = toolchain.establish_toolchain(config)
    operation_root = session.operation_root
    (goroot / "VERSION").write_text("changed\n", encoding="utf-8")

    session.release()

    assert not operation_root.exists()


def test_selected_executable_and_reported_root_cannot_disagree(tmp_path: Path):
    selected = _make_goroot(tmp_path / "selected")
    reported = _make_goroot(tmp_path / "reported")
    forbidden = tmp_path / "repository"
    forbidden.mkdir()
    private_base = tmp_path / "private"
    private_base.mkdir()
    runner = RecordingRunner(
        selected,
        environment_overrides={"GOROOT": str(reported)},
    )
    config = toolchain.ToolchainConfig(
        private_base=private_base,
        operator_search_path=toolchain.OperatorSearchPath((str(selected / "bin"),)),
        forbidden_roots=(forbidden,),
        runner=runner,
    )
    with pytest.raises(toolchain.ToolchainError) as raised:
        toolchain.establish_toolchain(config)
    _assert_code("toolchain_executable_mismatch", raised)


def test_explicit_executable_and_goroot_must_agree(tmp_path: Path):
    first = _make_goroot(tmp_path / "first")
    second = _make_goroot(tmp_path / "second")
    forbidden = tmp_path / "repository"
    forbidden.mkdir()
    private_base = tmp_path / "private"
    private_base.mkdir()
    name = "go.exe" if os.name == "nt" else "go"
    config = toolchain.ToolchainConfig(
        private_base=private_base,
        operator_search_path=toolchain.OperatorSearchPath(()),
        forbidden_roots=(forbidden,),
        go_executable=first / "bin" / name,
        goroot=second,
        runner=RecordingRunner(first),
    )
    with pytest.raises(toolchain.ToolchainError) as raised:
        toolchain.establish_toolchain(config)
    _assert_code("toolchain_executable_mismatch", raised)


def test_caller_may_raise_the_fingerprint_deadline_above_the_default():
    raised = toolchain.DEFAULT_FINGERPRINT_TIMEOUT * 2
    assert toolchain.resolve_fingerprint_timeout(raised) == raised


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (1.0, 1.0),
        (toolchain.MAX_FINGERPRINT_TIMEOUT, toolchain.MAX_FINGERPRINT_TIMEOUT),
        (toolchain.MAX_FINGERPRINT_TIMEOUT * 10, toolchain.MAX_FINGERPRINT_TIMEOUT),
        (float("inf"), toolchain.MAX_FINGERPRINT_TIMEOUT),
        (0.0, toolchain.DEFAULT_FINGERPRINT_TIMEOUT),
        (-5.0, toolchain.DEFAULT_FINGERPRINT_TIMEOUT),
        (float("nan"), toolchain.DEFAULT_FINGERPRINT_TIMEOUT),
    ],
)
def test_fingerprint_deadline_stays_inside_its_supported_band(
    value: float,
    expected: float,
):
    assert toolchain.resolve_fingerprint_timeout(value) == expected


def test_operator_environment_sets_the_fingerprint_deadline(
    monkeypatch: pytest.MonkeyPatch,
):
    monkeypatch.delenv(toolchain.FINGERPRINT_TIMEOUT_ENV, raising=False)
    assert toolchain.resolve_fingerprint_timeout() == (
        toolchain.DEFAULT_FINGERPRINT_TIMEOUT
    )
    monkeypatch.setenv(toolchain.FINGERPRINT_TIMEOUT_ENV, "900")
    assert toolchain.resolve_fingerprint_timeout() == 900.0
    assert toolchain.resolve_fingerprint_timeout(300.0) == 300.0


@pytest.mark.parametrize("raw", ["", "  ", "later", "12s", "None"])
def test_unusable_operator_deadline_degrades_to_the_default(
    monkeypatch: pytest.MonkeyPatch,
    raw: str,
):
    monkeypatch.setenv(toolchain.FINGERPRINT_TIMEOUT_ENV, raw)
    assert toolchain.resolve_fingerprint_timeout() == (
        toolchain.DEFAULT_FINGERPRINT_TIMEOUT
    )


def test_operator_deadline_from_environment_is_also_bounded(
    monkeypatch: pytest.MonkeyPatch,
):
    monkeypatch.setenv(toolchain.FINGERPRINT_TIMEOUT_ENV, "0")
    assert toolchain.resolve_fingerprint_timeout() == (
        toolchain.DEFAULT_FINGERPRINT_TIMEOUT
    )
    monkeypatch.setenv(
        toolchain.FINGERPRINT_TIMEOUT_ENV,
        str(toolchain.MAX_FINGERPRINT_TIMEOUT * 100),
    )
    assert toolchain.resolve_fingerprint_timeout() == (
        toolchain.MAX_FINGERPRINT_TIMEOUT
    )


def test_exhausted_fingerprint_deadline_names_the_operator_override(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    config, _runner, _goroot, _private_base = _setup(tmp_path)
    monkeypatch.setenv(toolchain.FINGERPRINT_TIMEOUT_ENV, "0.000001")
    with pytest.raises(toolchain.ToolchainError) as raised:
        toolchain.establish_toolchain(config)
    _assert_code("toolchain_timeout", raised)
    assert str(raised.value) == (
        "go-v1 toolchain_timeout: toolchain fingerprint deadline exceeded"
    )
    assert any(
        toolchain.FINGERPRINT_TIMEOUT_ENV in note
        for note in raised.value.__notes__
    )


def test_a_slow_first_fingerprint_completes_once_the_operator_raises_it(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    config, _runner, goroot, private_base = _setup(tmp_path)
    monkeypatch.setenv(toolchain.FINGERPRINT_TIMEOUT_ENV, "0.000001")
    with pytest.raises(toolchain.ToolchainError) as raised:
        toolchain.establish_toolchain(config)
    _assert_code("toolchain_timeout", raised)

    monkeypatch.setenv(
        toolchain.FINGERPRINT_TIMEOUT_ENV,
        str(toolchain.MAX_FINGERPRINT_TIMEOUT),
    )
    with toolchain.establish_toolchain(config) as session:
        assert session.goroot == goroot.resolve(strict=True)
        session.verify()
    assert not any(private_base.iterdir())


def test_caller_deadline_overrides_an_unusably_small_operator_value(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    goroot = _make_goroot(tmp_path / "trusted-go")
    stdout = b"go version go1.25.5 %s/%s\n" % (
        toolchain._native_host().goos.encode("ascii"),
        toolchain._native_host().goarch.encode("ascii"),
    )
    monkeypatch.setenv(toolchain.FINGERPRINT_TIMEOUT_ENV, "0.000001")
    with pytest.raises(toolchain.ToolchainError) as raised:
        toolchain.fingerprint_toolchain(goroot, stdout)
    _assert_code("toolchain_timeout", raised)
    identity = toolchain.fingerprint_toolchain(
        goroot,
        stdout,
        timeout=toolchain.MAX_FINGERPRINT_TIMEOUT,
    )
    assert identity.algorithm == toolchain.TOOLCHAIN_ALGORITHM


def test_the_deadline_clock_outresolves_the_deadlines_it_enforces(
    monkeypatch: pytest.MonkeyPatch,
):
    """The deadline clock must see a deadline smaller than one platform tick.

    Windows CPython before 3.13 backs ``time.monotonic()`` with
    ``GetTickCount64()``, which advances once every 15.625 ms.  A deadline
    built from that clock and checked inside the same tick compares equal to
    itself, so an exhausted deadline reads as unreached and admits the work
    it exists to refuse -- and a fingerprint pass over a small GOROOT finishes
    well inside one tick.  Pin both halves of the fix here: the module reads
    ``perf_counter``, and that clock is monotonic and fine-grained on the host
    running this test.  Reverting to ``monotonic`` fails this on Windows.
    """

    monkeypatch.setattr(toolchain.time, "perf_counter", lambda: 4321.0)
    assert toolchain._elapsed() == 4321.0

    info = time.get_clock_info("perf_counter")
    assert info.monotonic
    assert info.resolution <= 1e-06
