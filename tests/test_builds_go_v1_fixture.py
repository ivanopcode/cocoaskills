from __future__ import annotations

import hashlib
import json
import os
import shutil
import signal
import subprocess
import sys
from pathlib import Path

import pytest

from csk.builds import go_v1, source, toolchain


def test_installed_hidden_worker_literal_is_parser_rejected(
    tmp_path: Path,
):
    manager_value = os.environ.get("CSK_GO_V1_MANAGER_EXECUTABLE")
    if not manager_value:
        pytest.skip("CSK_GO_V1_MANAGER_EXECUTABLE is not set")
    manager = Path(manager_value).resolve(strict=True)
    go_marker = tmp_path / "go-was-started"
    poison_bin = tmp_path / "poison-bin"
    poison_bin.mkdir()
    poison_go = poison_bin / ("go.exe" if os.name == "nt" else "go")
    poison_go.write_text(
        "#!/bin/sh\n"
        f"printf started > {go_marker}\n",
        encoding="utf-8",
    )
    poison_go.chmod(0o755)
    environment = {"PATH": str(poison_bin)}
    if os.name == "nt":
        environment.update(
            {
                name: value
                for name in (
                    "SYSTEMROOT",
                    "WINDIR",
                    "USERPROFILE",
                    "HOMEDRIVE",
                    "HOMEPATH",
                    "APPDATA",
                    "LOCALAPPDATA",
                )
                if (value := os.environ.get(name))
            }
        )

    completed = subprocess.run(
        [str(manager), go_v1.WORKER_MODE],
        input=b"",
        capture_output=True,
        check=False,
        env=environment,
    )

    assert completed.returncode == 2
    assert completed.stdout == b""
    assert b"invalid choice" in completed.stderr
    assert not go_marker.exists()


@pytest.mark.skipif(
    sys.platform != "darwin" and os.name != "nt",
    reason="the portable source-aware policy covers exactly macOS and Windows",
)
@pytest.mark.parametrize(
    "scenario",
    (
        "accepted",
        "accepted-with-private-runtime",
        "accepted-with-bound-startup-hook",
        "manager-startup-hook-inserted-after-launch",
        "manager-stdlib-module-replaced-after-launch",
        "manager-runtime-image-replaced-after-launch",
        "manager-package-replaced-after-launch",
        "worker-executable-replaced-between-checks",
        "worker-domain-teardown-failure",
    ),
)
def test_real_native_go_fixture_build_uses_closed_worker_boundary(
    scenario: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    manager_value = os.environ.get("CSK_GO_V1_MANAGER_EXECUTABLE")
    if not manager_value:
        pytest.skip("CSK_GO_V1_MANAGER_EXECUTABLE is not set")
    manager = Path(manager_value).resolve(strict=True)
    go_value = os.environ.get("CSK_GO_V1_GO_EXECUTABLE") or shutil.which("go")
    if not go_value:
        pytest.skip("a native Go executable is not available")
    go_executable = Path(go_value).resolve(strict=True)

    snapshot_root = tmp_path / "snapshot"
    source_dir = snapshot_root / "build" / "cmd"
    source_dir.mkdir(parents=True)
    marker = tmp_path / "artifact-was-run"
    (snapshot_root / "build" / "go.mod").write_text(
        "module example.test/closedfixture\n\n"
        "go 1.25\n\n"
        "require example.test/fixturedep v1.0.0\n",
        encoding="utf-8",
    )
    vendor = snapshot_root / "build" / "vendor"
    dependency = vendor / "example.test" / "fixturedep"
    dependency.mkdir(parents=True)
    (dependency / "dep.go").write_text(
        "package fixturedep\n\nfunc Payload() []byte { return []byte(\"ran\") }\n",
        encoding="utf-8",
    )
    (vendor / "modules.txt").write_text(
        "# example.test/fixturedep v1.0.0\n"
        "## explicit; go 1.25\n"
        "example.test/fixturedep\n",
        encoding="utf-8",
    )
    (source_dir / "main.go").write_text(
        "package main\n\n"
        "import (\n"
        "\t\"os\"\n"
        "\t\"example.test/fixturedep\"\n"
        ")\n\n"
        "func main() {\n"
        f"\t_ = os.WriteFile({json.dumps(str(marker))}, fixturedep.Payload(), 0600)\n"
        "}\n",
        encoding="utf-8",
    )
    frozen = source.freeze_snapshot(snapshot_root)

    private_base = tmp_path / "private"
    private_base.mkdir()
    repository_root = Path(__file__).resolve().parents[1]
    session = toolchain.establish_toolchain(
        toolchain.ToolchainConfig(
            private_base=private_base,
            operator_search_path=toolchain.OperatorSearchPath(
                (str(go_executable.parent),)
            ),
            forbidden_roots=(snapshot_root, repository_root),
            go_executable=go_executable,
        )
    )
    states: list[str] = []
    replaced_identity_file: Path | None = None
    original_identity_file = b""
    startup_hook: Path | None = None
    startup_marker = tmp_path / "startup-hook-ran"
    try:
        monkeypatch.setenv("PATH", str(snapshot_root))
        monkeypatch.setenv("GOFLAGS", "-toolexec=/attacker")
        monkeypatch.setenv("GOENV", "/attacker/go.env")
        monkeypatch.setenv("GOWORK", "/attacker/go.work")
        monkeypatch.setenv("GOTOOLCHAIN", "auto")
        monkeypatch.setenv("GOPROXY", "https://attacker.invalid")
        monkeypatch.setenv("CC", "/attacker/cc")
        monkeypatch.setenv("HTTP_PROXY", "http://attacker.invalid")

        request = go_v1.BuildRequest(
            toolchain_session=session,
            source_snapshot=frozen,
            command_object={
                "type": "build",
                "driver": "go-v1",
                "source_dir": "build/cmd",
            },
            build_root="build",
            source_dir="build/cmd",
            command="closed-fixture",
            limits=go_v1.ResourceLimits(timeout_seconds=90),
        )
        if scenario in {
            "accepted-with-private-runtime",
            "manager-stdlib-module-replaced-after-launch",
            "manager-runtime-image-replaced-after-launch",
        }:
            if (
                scenario == "manager-stdlib-module-replaced-after-launch"
                and sys.platform != "darwin"
            ):
                pytest.skip(
                    "the installed private-stdlib mutation negative is "
                    "macOS-native"
                )
            manager = _copy_installed_manager_with_private_runtime(
                go_v1._resolve_manager_identity(manager),
                tmp_path / "private-manager",
            )
        worker_pids: list[int] = []
        worker_processes: list[subprocess.Popen[bytes]] = []
        site_root = go_v1._resolve_manager_identity(manager).startup.site_root
        if scenario in {
            "accepted-with-bound-startup-hook",
            "manager-startup-hook-inserted-after-launch",
        }:
            startup_hook = site_root / "csk_fixture_startup_hook.pth"
            hook_source = (
                "import pathlib; pathlib.Path("
                + json.dumps(str(startup_marker))
                + ").write_text('unverified startup code executed')\n"
            )
            if scenario == "accepted-with-bound-startup-hook":
                # A startup hook already installed beside the manager package is
                # bound by the manager identity and can never execute, because
                # the fixed worker launch disables site processing entirely.
                startup_hook.write_text(hook_source, encoding="utf-8")
                result = go_v1.build(
                    request,
                    _state_observer=states.append,
                    _manager_executable=manager,
                )
                assert result.artifact.staged_path.is_file()
                assert states == list(go_v1.SESSION_STATES)
                assert not startup_marker.exists()
                assert not marker.exists()
                return

            if sys.platform != "darwin":
                pytest.skip(
                    "the macOS negative mutates after launch; Windows denies "
                    "the insertion through retained handles"
                )
            original_launch = go_v1._NativeControlDomain.launch
            hook = startup_hook

            def launch_then_insert_hook(
                domain: go_v1._NativeControlDomain,
                identity: go_v1._ManagerIdentity,
                worker_cache: Path,
            ):
                process = original_launch(domain, identity, worker_cache)
                worker_pids.append(process.pid)
                os.kill(process.pid, signal.SIGSTOP)
                try:
                    hook.write_text(hook_source, encoding="utf-8")
                finally:
                    os.kill(process.pid, signal.SIGCONT)
                return process

            monkeypatch.setattr(
                go_v1._NativeControlDomain,
                "launch",
                launch_then_insert_hook,
            )
            with pytest.raises(go_v1.GoV1Error) as raised:
                go_v1.build(
                    request,
                    _state_observer=states.append,
                    _manager_executable=manager,
                )
            assert raised.value.code == go_v1.CODE_WORKER_IDENTITY_INVALID
            if scenario == "manager-runtime-image-replaced-after-launch":
                assert (
                    "execution identity changed while the worker ran"
                    in raised.value.detail
                    or "retained runtime identity denied replacement"
                    in raised.value.detail
                )
            assert len(worker_pids) == 1
            with pytest.raises(ProcessLookupError):
                os.kill(worker_pids[0], 0)
            assert states == list(go_v1.SESSION_STATES[:3])
            assert not startup_marker.exists()
            assert not marker.exists()
            assert not list(session.operation_root.glob(".csk-go-build-*"))
            return
        if scenario in {
            "manager-stdlib-module-replaced-after-launch",
            "manager-runtime-image-replaced-after-launch",
            "manager-package-replaced-after-launch",
            "worker-executable-replaced-between-checks",
        }:
            if (
                scenario != "manager-runtime-image-replaced-after-launch"
                and sys.platform != "darwin"
            ):
                pytest.skip(
                    "the macOS negative mutates after launch; Windows denies "
                    "the replacement through retained handles"
                )
            manager_identity = go_v1._resolve_manager_identity(manager)
            replaced_identity_file = (
                manager_identity.interpreter.runtime.runtime_image.path
                if scenario == "manager-runtime-image-replaced-after-launch"
                else (
                    manager_identity.startup.stdlib_root
                    / "json"
                    / "__init__.py"
                    if scenario == "manager-stdlib-module-replaced-after-launch"
                    else (
                        manager_identity.package_tree.path
                        / "builds"
                        / "go_v1.py"
                        if scenario == "manager-package-replaced-after-launch"
                        else manager_identity.launcher.path
                    )
                )
            )
            original_identity_file = replaced_identity_file.read_bytes()
            original_launch = go_v1._NativeControlDomain.launch

            def launch_then_replace(
                domain: go_v1._NativeControlDomain,
                identity: go_v1._ManagerIdentity,
                worker_cache: Path,
            ):
                process = original_launch(domain, identity, worker_cache)
                worker_pids.append(process.pid)
                worker_processes.append(process)
                if sys.platform == "darwin":
                    os.kill(process.pid, signal.SIGSTOP)
                try:
                    replaced_identity_file.write_bytes(
                        original_identity_file
                        + b"\n# identity replacement\n"
                    )
                except OSError as exc:
                    domain.terminate(process)
                    raise go_v1.GoV1Error(
                        go_v1.CODE_WORKER_IDENTITY_INVALID,
                        "retained runtime identity denied replacement",
                    ) from exc
                finally:
                    if (
                        sys.platform == "darwin"
                        and process.poll() is None
                    ):
                        os.kill(process.pid, signal.SIGCONT)
                return process

            monkeypatch.setattr(
                go_v1._NativeControlDomain,
                "launch",
                launch_then_replace,
            )
            with pytest.raises(go_v1.GoV1Error) as raised:
                go_v1.build(
                    request,
                    _state_observer=states.append,
                    _manager_executable=manager,
                )
            assert raised.value.code == go_v1.CODE_WORKER_IDENTITY_INVALID
            assert len(worker_pids) == 1
            assert len(worker_processes) == 1
            assert worker_processes[0].poll() is not None
            if sys.platform == "darwin":
                with pytest.raises(ProcessLookupError):
                    os.kill(worker_pids[0], 0)
            assert states == list(go_v1.SESSION_STATES[:3])
            assert not marker.exists()
            assert not list(
                session.operation_root.glob(".csk-go-build-*")
            )
            return
        if scenario == "worker-domain-teardown-failure":
            if sys.platform != "darwin":
                pytest.skip(
                    "the native Windows teardown path is exercised by CI"
                )

            def fail_teardown(
                _domain: go_v1._NativeControlDomain,
                _process: object,
            ) -> None:
                raise go_v1.GoV1Error(
                    go_v1.CODE_CONTROL_UNAVAILABLE,
                    "injected complete-domain join failure",
                )

            monkeypatch.setattr(
                go_v1._NativeControlDomain,
                "terminate",
                fail_teardown,
            )
            with pytest.raises(go_v1.GoV1Error) as raised:
                go_v1.build(
                    request,
                    _state_observer=states.append,
                    _manager_executable=manager,
                )
            assert raised.value.code == go_v1.CODE_CONTROL_UNAVAILABLE
            assert not marker.exists()
            assert not list(
                session.operation_root.glob(".csk-go-build-*")
            )
            return

        result = go_v1.build(
            request,
            _state_observer=states.append,
            _manager_executable=manager,
        )

        artifact = result.artifact.staged_path
        payload = artifact.read_bytes()
        assert result.artifact.metadata.path == (
            "bin/closed-fixture.exe"
            if os.name == "nt"
            else "bin/closed-fixture"
        )
        assert result.artifact.metadata.size == len(payload)
        assert result.artifact.metadata.sha256 == (
            "sha256:" + hashlib.sha256(payload).hexdigest()
        )
        assert artifact.is_file()
        assert not marker.exists(), "the verified output must never be launched"
        assert states == list(go_v1.SESSION_STATES)
        assert result.capability_evidence == go_v1.evidence_from_applied(
            go_v1.inventory_platform(),
            _synthetic_host_probes(),
            [
                probe.name
                for probe in _synthetic_host_probes()
                if probe.availability == go_v1.AVAILABILITY_AVAILABLE
            ],
        )
    finally:
        if replaced_identity_file is not None and original_identity_file:
            replaced_identity_file.write_bytes(original_identity_file)
        if startup_hook is not None:
            startup_hook.unlink(missing_ok=True)
        frozen.close()
        session.close()


def _synthetic_host_probes() -> tuple[go_v1.ControlProbe, ...]:
    platform = go_v1.inventory_platform()
    records = go_v1._NATIVE_CONTROL_PLATFORMS[platform]
    return tuple(
        go_v1.ControlProbe(
            name=name,
            availability=records[name].availability,
            mechanism=records[name].mechanism,
        )
        for name in go_v1.NATIVE_CONTROL_INVENTORY
    )


def _copy_installed_manager_with_private_runtime(
    identity: go_v1._ManagerIdentity,
    destination: Path,
) -> Path:
    source_home = identity.startup.python_home
    if os.name == "nt":
        base_home = destination / "base-python"
        manager_home = destination / "manager-venv"
        shutil.copytree(source_home, base_home, symlinks=True)
        shutil.copytree(
            identity.launcher.path.parent.parent,
            manager_home,
            symlinks=True,
        )
        configuration = manager_home / "pyvenv.cfg"
        lines: list[str] = []
        for line in configuration.read_text(encoding="utf-8-sig").splitlines():
            key = line.partition("=")[0].strip().casefold()
            if key == "home":
                lines.append(f"home = {base_home}")
            elif key == "executable":
                lines.append(f"executable = {base_home / 'python.exe'}")
            else:
                lines.append(line)
        configuration.write_text(
            "\n".join(lines) + "\n",
            encoding="utf-8",
        )
        return (manager_home / "Scripts" / "csk.exe").resolve(strict=True)

    shutil.copytree(source_home, destination, symlinks=True)
    runtime_image = (
        destination
        / identity.interpreter.runtime.runtime_image.path.relative_to(
            source_home
        )
    )
    interpreter = (
        destination
        / identity.interpreter.runtime.process_image.path.relative_to(
            source_home
        )
    )
    source_runtime = identity.interpreter.runtime.runtime_image.path
    if runtime_image != interpreter:
        subprocess.run(
            [
                "/usr/bin/install_name_tool",
                "-change",
                str(source_runtime),
                str(runtime_image),
                str(interpreter),
            ],
            check=True,
            capture_output=True,
        )
        subprocess.run(
            ["/usr/bin/codesign", "--force", "--sign", "-", str(interpreter)],
            check=True,
            capture_output=True,
        )
    site_root = (
        destination
        / "lib"
        / identity.startup.site_root.parent.name
        / "site-packages"
    )
    if site_root.is_symlink():
        site_root.unlink()
    site_root.mkdir(parents=True, exist_ok=True)
    package = site_root / "csk"
    if package.exists():
        shutil.rmtree(package)
    shutil.copytree(identity.package_tree.path, package)
    launcher = destination / "bin" / "csk"
    launcher.write_text(
        f"#!{interpreter}\n"
        "import sys\n"
        "from csk.cli import main\n"
        "if __name__ == '__main__':\n"
        "    sys.exit(main())\n",
        encoding="utf-8",
    )
    launcher.chmod(0o755)
    return launcher.resolve(strict=True)
