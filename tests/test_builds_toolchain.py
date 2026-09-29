from __future__ import annotations

import base64
import hashlib
import json
import os
import shlex
import stat
import sys
import time
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest

from csk import config as manager_config
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
        argv: toolchain.ProbeArgv,
        *,
        cwd: Path,
        environment: dict[str, str],
        timeout: float,
        output_limit: int,
    ) -> toolchain.ProbeResult:
        del timeout, output_limit
        assert argv and isinstance(argv[0], toolchain.AdmittedExecutable)
        recorded_argv = (str(argv[0].path), *argv[1:])
        copied_environment = dict(environment)
        self.calls.append((recorded_argv, cwd, copied_environment))
        self.operation_roots.append(cwd.parent)
        arguments = recorded_argv[1:]
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
        raise AssertionError(f"unexpected Go probe argv: {recorded_argv!r}")


class ShimRecordingRunner(RecordingRunner):
    def __init__(
        self,
        goroot: Path,
        shim: Path,
        *,
        shim_stdout: bytes | None = None,
        shim_returncode: int = 0,
        version: str | None = None,
    ):
        super().__init__(goroot, version=version)
        self.shim = shim
        self.shim_stdout = shim_stdout
        self.shim_returncode = shim_returncode
        self.shim_calls = 0
        self.shim_probe_options: list[tuple[float, int]] = []

    def run(
        self,
        argv: toolchain.ProbeArgv,
        *,
        cwd: Path,
        environment: dict[str, str],
        timeout: float,
        output_limit: int,
    ) -> toolchain.ProbeResult:
        assert argv and isinstance(argv[0], toolchain.AdmittedExecutable)
        recorded_argv = (str(argv[0].path), *argv[1:])
        if recorded_argv == (str(self.shim.resolve()), "env", "GOROOT"):
            self.shim_calls += 1
            self.shim_probe_options.append((timeout, output_limit))
            copied_environment = dict(environment)
            self.calls.append((recorded_argv, cwd, copied_environment))
            self.operation_roots.append(cwd.parent)
            return toolchain.ProbeResult(
                stdout=(
                    f"{self.goroot}\n".encode()
                    if self.shim_stdout is None
                    else self.shim_stdout
                ),
                returncode=self.shim_returncode,
            )
        return super().run(
            argv,
            cwd=cwd,
            environment=environment,
            timeout=timeout,
            output_limit=output_limit,
        )


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
        argv: toolchain.ProbeArgv,
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
    (path / "src" / "runtime").mkdir(parents=True)
    (path / "pkg" / "tool").mkdir(parents=True)
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


def _setup_shim(
    tmp_path: Path,
    *,
    goroot: Path | None = None,
    shim_stdout: bytes | None = None,
    shim_returncode: int = 0,
    version: str | None = None,
) -> tuple[toolchain.ToolchainConfig, ShimRecordingRunner, Path, Path]:
    config, _runner, default_goroot, private_base = _setup(tmp_path)
    selected_goroot = default_goroot if goroot is None else goroot
    shim_directory = tmp_path / "goenv" / "shims"
    shim_directory.mkdir(parents=True)
    shim = shim_directory / ("go.exe" if os.name == "nt" else "go")
    shim.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    shim.chmod(0o755)
    runner = ShimRecordingRunner(
        selected_goroot,
        shim,
        shim_stdout=shim_stdout,
        shim_returncode=shim_returncode,
        version=version,
    )
    config = replace(
        config,
        operator_search_path=toolchain.OperatorSearchPath((str(shim_directory),)),
        runner=runner,
    )
    return config, runner, selected_goroot, private_base


def _assert_code(expected: str, raised: pytest.ExceptionInfo[toolchain.ToolchainError]) -> None:
    assert raised.value.code == expected


def _assert_go_path_in_detail(
    config: toolchain.ToolchainConfig,
    detail: str,
) -> None:
    executable = "go.exe" if os.name == "nt" else "go"
    expected = (Path(config.operator_search_path.entries[0]) / executable).resolve()
    assert expected.as_posix() in detail.replace("\\", "/")


def _override_launcher_file_attributes(
    monkeypatch: pytest.MonkeyPatch,
    executable: Path,
    file_attributes: int | None,
) -> None:
    original_lstat = Path.lstat

    def synthesized_lstat(path: Path):
        result = original_lstat(path)
        if path == executable:
            return SimpleNamespace(
                st_mode=result.st_mode,
                st_dev=result.st_dev,
                st_ino=result.st_ino,
                st_file_attributes=file_attributes,
            )
        return result

    monkeypatch.setattr(Path, "lstat", synthesized_lstat)


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


@pytest.mark.skipif(os.name != "nt", reason="exercises the Windows go.exe launcher shape")
def test_windows_go_exe_uses_structural_root_and_direct_probe_sequence(tmp_path: Path):
    config, runner, goroot, _private_base = _setup(tmp_path)

    session = toolchain.establish_toolchain(config)
    try:
        executable = str(goroot / "bin" / "go.exe")
        assert session.snapshot.executable == Path(executable)
        assert [call[0] for call in runner.calls] == [
            (executable, "telemetry", "off"),
            (executable, "version"),
            (executable, "env", "-json", *toolchain.GO_ENV_FIELDS),
        ]
    finally:
        session.close()


def test_manager_shim_resolution_runs_exactly_once_under_bootstrap(tmp_path: Path):
    config, runner, goroot, _private_base = _setup_shim(tmp_path)

    session = toolchain.establish_toolchain(config)
    try:
        executable = str((goroot / "bin" / ("go.exe" if os.name == "nt" else "go")).resolve())
        assert session.snapshot.executable == Path(executable)
        assert runner.shim_calls == 1
        assert [call[0] for call in runner.calls] == [
            (str(runner.shim), "env", "GOROOT"),
            (executable, "telemetry", "off"),
            (executable, "version"),
            (executable, "env", "-json", *toolchain.GO_ENV_FIELDS),
        ]
        assert len({call[1] for call in runner.calls}) == 1
        probe_cwd = runner.calls[0][1]
        assert probe_cwd == session.operation_root / "empty"
        assert runner.calls[0][2]["GOENV"] == "off"
        assert runner.calls[0][2]["GOTOOLCHAIN"] == "local"
        assert runner.calls[0][2]["LC_ALL"] == "C"
        assert runner.calls[0][2]["LANG"] == "C"
        assert "GOROOT" not in runner.calls[0][2]
        assert "GOOS" not in runner.calls[0][2]
        assert "GOARCH" not in runner.calls[0][2]
        assert runner.calls[0][2]["PATH"] == str(session.operation_root / "empty-path")
        assert set(runner.calls[0][2]) == {
            "GOENV",
            "GOTOOLCHAIN",
            "LC_ALL",
            "LANG",
            "GOPATH",
            "GOMODCACHE",
            "GOCACHE",
            "GOTMPDIR",
            "HOME",
            "XDG_CONFIG_HOME",
            "PATH",
            "TMPDIR",
        } | (
            {"APPDATA", "LOCALAPPDATA", "USERPROFILE", "TEMP", "TMP"}
            | {name for name in ("SYSTEMROOT", "WINDIR") if os.environ.get(name)}
            if os.name == "nt"
            else set()
        )
        assert runner.shim_probe_options == [
            (
                toolchain.DEFAULT_PROBE_TIMEOUT,
                toolchain.DEFAULT_OUTPUT_LIMIT,
            )
        ]
        for name in (
            "GOPATH",
            "GOMODCACHE",
            "GOCACHE",
            "GOTMPDIR",
            "HOME",
            "XDG_CONFIG_HOME",
            "TMPDIR",
        ):
            assert Path(runner.calls[0][2][name]).is_relative_to(session.operation_root)
    finally:
        session.close()


def test_relative_existing_shim_goroot_is_rejected(tmp_path: Path, monkeypatch):
    config, runner, goroot, _private_base = _setup_shim(
        tmp_path,
        shim_stdout=b"trusted-go\n",
    )
    monkeypatch.chdir(tmp_path)

    with pytest.raises(toolchain.ToolchainError) as raised:
        toolchain.establish_toolchain(config)

    _assert_code("toolchain_shim_unresolved", raised)
    assert "GOROOT answer must be an absolute path" in str(raised.value)
    assert runner.shim_calls == 1
    assert len(runner.calls) == 1
    assert goroot.is_dir()


@pytest.mark.parametrize(
    ("shim_stdout", "reason"),
    [
        (b"relative/go-root\n", "absolute path"),
        (None, "existing directory"),
        (b"/first/root\n/second/root\n", "exactly one line"),
        (b"\xff\n", "valid UTF-8"),
    ],
    ids=["relative", "missing", "multiple-lines", "invalid-utf8"],
)
def test_bad_shim_goroot_answers_refuse_with_actionable_code(
    tmp_path: Path,
    shim_stdout: bytes | None,
    reason: str,
):
    if shim_stdout is None:
        shim_stdout = f"{tmp_path / 'missing-go-root'}\n".encode()
    config, runner, _goroot, private_base = _setup_shim(
        tmp_path,
        shim_stdout=shim_stdout,
    )

    with pytest.raises(toolchain.ToolchainError) as raised:
        toolchain.establish_toolchain(config)

    _assert_code("toolchain_shim_unresolved", raised)
    message = str(raised.value)
    assert str(runner.shim) in message
    assert reason in message
    assert "put a real Go toolchain first on PATH" in message
    assert "mise activate" in message
    assert runner.shim_calls == 1
    assert len(runner.calls) == 1
    assert not list(private_base.glob(".csk-go-probe-*"))


def test_nonzero_shim_probe_refuses_with_actionable_code(tmp_path: Path):
    config, runner, _goroot, _private_base = _setup_shim(
        tmp_path,
        shim_returncode=17,
    )

    with pytest.raises(toolchain.ToolchainError) as raised:
        toolchain.establish_toolchain(config)

    _assert_code("toolchain_shim_unresolved", raised)
    assert "exited with status 17" in str(raised.value)
    assert str(runner.shim) in str(raised.value)
    assert runner.shim_calls == 1
    assert len(runner.calls) == 1


@pytest.mark.skipif(os.name == "nt", reason="symlink creation is not portable on Windows")
def test_shim_goroot_with_symlinked_go_refuses(tmp_path: Path):
    config, runner, goroot, _private_base = _setup_shim(tmp_path)
    elsewhere = _make_goroot(tmp_path / "elsewhere")
    executable = goroot / "bin" / "go"
    executable.unlink()
    executable.symlink_to(elsewhere / "bin" / "go")

    with pytest.raises(toolchain.ToolchainError) as raised:
        toolchain.establish_toolchain(config)

    _assert_code("toolchain_shim_unresolved", raised)
    assert "must be a regular file with one link" in str(raised.value)
    assert runner.shim_calls == 1
    assert len(runner.calls) == 1


def test_shim_goroot_with_hardlinked_go_refuses(tmp_path: Path):
    config, runner, goroot, _private_base = _setup_shim(tmp_path)
    elsewhere = _make_goroot(tmp_path / "elsewhere")
    executable = goroot / "bin" / ("go.exe" if os.name == "nt" else "go")
    executable.unlink()
    os.link(elsewhere / "bin" / executable.name, executable)

    with pytest.raises(toolchain.ToolchainError) as raised:
        toolchain.establish_toolchain(config)

    _assert_code("toolchain_shim_unresolved", raised)
    assert "must be a regular file with one link" in str(raised.value)
    assert runner.shim_calls == 1
    assert len(runner.calls) == 1


def test_shim_goroot_inside_forbidden_root_refuses(tmp_path: Path):
    config, runner, _goroot, _private_base = _setup_shim(tmp_path)
    forbidden = config.forbidden_roots[0]
    forbidden_goroot = _make_goroot(forbidden / "nested-go")
    runner.goroot = forbidden_goroot

    with pytest.raises(toolchain.ToolchainError) as raised:
        toolchain.establish_toolchain(config)

    _assert_code("toolchain_shim_unresolved", raised)
    assert "under a forbidden root" in str(raised.value)
    assert str(runner.shim) in str(raised.value)
    assert runner.shim_calls == 1
    assert len(runner.calls) == 1


def test_shim_answer_without_structural_goroot_refuses(tmp_path: Path):
    config, runner, goroot, _private_base = _setup_shim(tmp_path)
    (goroot / "pkg" / "tool").rmdir()

    with pytest.raises(toolchain.ToolchainError) as raised:
        toolchain.establish_toolchain(config)

    _assert_code("toolchain_shim_unresolved", raised)
    assert "not structurally a Go installation" in str(raised.value)
    assert runner.shim_calls == 1
    assert len(runner.calls) == 1


def _assert_forbidden_shim_alias_is_not_executed(
    tmp_path: Path,
    *,
    chain: bool,
) -> None:
    forbidden = tmp_path / "project"
    forbidden.mkdir()
    marker = tmp_path / "executed"
    target = forbidden / "go"
    target.write_text(
        "#!/bin/sh\n"
        f"printf executed > {shlex.quote(str(marker))}\n"
        f"printf '%s\\n' {shlex.quote(str(tmp_path / 'unused-goroot'))}\n",
        encoding="utf-8",
    )
    target.chmod(0o755)

    first = tmp_path / "outside-first"
    first.mkdir()
    if chain:
        second = tmp_path / "outside-second"
        second.mkdir()
        (second / "go").symlink_to(target)
        (first / "go").symlink_to(second / "go")
    else:
        (first / "go").symlink_to(target)
    private = tmp_path / "private"
    private.mkdir()
    config = toolchain.ToolchainConfig(
        private_base=private,
        operator_search_path=toolchain.OperatorSearchPath((str(first),)),
        forbidden_roots=(forbidden,),
    )

    with pytest.raises(toolchain.ToolchainError) as raised:
        toolchain.establish_toolchain(config)

    assert not marker.exists(), "forbidden project-owned shim executed through an alias"
    _assert_code("untrusted_go_executable", raised)


@pytest.mark.skipif(os.name == "nt", reason="symlink creation is not portable on Windows")
def test_symlink_to_forbidden_shim_is_not_executed(tmp_path: Path):
    _assert_forbidden_shim_alias_is_not_executed(tmp_path, chain=False)


@pytest.mark.skipif(os.name == "nt", reason="symlink creation is not portable on Windows")
def test_symlink_chain_to_forbidden_shim_is_not_executed(tmp_path: Path):
    _assert_forbidden_shim_alias_is_not_executed(tmp_path, chain=True)


def test_shim_goroot_symlinked_bin_cannot_alias_forbidden_root(tmp_path: Path):
    config, runner, _goroot, _private_base = _setup_shim(tmp_path)
    forbidden_goroot = _make_goroot(config.forbidden_roots[0] / "nested-go")
    answer_goroot = tmp_path / "answer-goroot"
    answer_goroot.mkdir()
    (answer_goroot / "bin").symlink_to(
        forbidden_goroot / "bin",
        target_is_directory=True,
    )
    runner.goroot = forbidden_goroot
    runner.shim_stdout = f"{answer_goroot}\n".encode()

    with pytest.raises(toolchain.ToolchainError) as raised:
        toolchain.establish_toolchain(config)

    _assert_code("toolchain_shim_unresolved", raised)
    assert "bin" in str(raised.value)
    assert "real directory" in str(raised.value)
    assert runner.shim_calls == 1
    assert len(runner.calls) == 1


def test_shim_and_real_launcher_have_identical_toolchain_fingerprint(tmp_path: Path):
    real_root = _make_goroot(tmp_path / "shared-go")
    shim_config, shim_runner, _shim_root, _ = _setup_shim(
        tmp_path / "shared-shim",
        goroot=real_root,
    )
    real_config, real_runner, _unused_root, _ = _setup(tmp_path / "shared-real")
    real_runner.goroot = real_root
    real_config = replace(
        real_config,
        operator_search_path=toolchain.OperatorSearchPath((str(real_root / "bin"),)),
    )

    shim_session = toolchain.establish_toolchain(shim_config)
    real_session = toolchain.establish_toolchain(real_config)
    try:
        assert shim_session.toolchain == real_session.toolchain
        assert shim_runner.shim_calls == 1
        assert len(real_runner.calls) == 3
        executable = str(real_root / "bin" / ("go.exe" if os.name == "nt" else "go"))
        assert [call[0] for call in real_runner.calls] == [
            (executable, "telemetry", "off"),
            (executable, "version"),
            (executable, "env", "-json", *toolchain.GO_ENV_FIELDS),
        ]
    finally:
        shim_session.close()
        real_session.close()


def test_shim_resolution_keeps_future_family_warning_gate(tmp_path: Path, capsys):
    host = toolchain._native_host()
    version = f"go version go1.28.1 {host.goos}/{host.goarch}\n"
    config, runner, _goroot, _ = _setup_shim(tmp_path, version=version)
    toolchain.reset_go_future_warning_state()

    session = toolchain.establish_toolchain(config)
    try:
        warning = capsys.readouterr().err
        assert "untested_go_family" in warning
        assert "1.28" in warning
        assert runner.shim_calls == 1
        assert len(runner.calls) == 4
    finally:
        session.close()


@pytest.mark.skipif(os.name != "nt", reason="Windows batch shim behavior is host-specific")
@pytest.mark.parametrize("extension", [".cmd", ".bat"])
def test_windows_batch_shims_refuse_clearly(tmp_path: Path, extension: str):
    config, runner, _goroot, _private_base = _setup(tmp_path)
    shim_directory = tmp_path / "batch-shims"
    shim_directory.mkdir()
    shim = shim_directory / f"go{extension}"
    shim.write_text("@echo off\r\n", encoding="utf-8")
    config = replace(
        config,
        operator_search_path=toolchain.OperatorSearchPath((str(shim_directory),)),
    )

    with pytest.raises(toolchain.ToolchainError) as raised:
        toolchain.establish_toolchain(config)

    _assert_code("toolchain_shim_unresolved", raised)
    assert str(shim) in str(raised.value)
    assert "cannot be executed by the direct-process probe runner" in str(raised.value)
    assert "remediation" in str(raised.value)
    assert runner.calls == []


def test_windows_bat_shim_refusal_gate_on_any_host(tmp_path: Path):
    config, _runner, _goroot, _private_base = _setup(tmp_path)
    shim_directory = tmp_path / "batch-shims"
    shim_directory.mkdir()
    shim = shim_directory / "go.bat"
    shim.write_text("@echo off\r\n", encoding="utf-8")
    config = replace(
        config,
        operator_search_path=toolchain.OperatorSearchPath((str(shim_directory),)),
    )
    host = toolchain._Host(goos="windows", goarch="amd64", windows=True)

    with pytest.raises(toolchain.ToolchainError) as raised:
        toolchain._select_toolchain(config, host, ())

    _assert_code("toolchain_shim_unresolved", raised)
    assert str(shim) in str(raised.value)
    assert "cannot be executed by the direct-process probe runner" in str(raised.value)


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


def test_tested_go_families_match_the_qualified_release_set() -> None:
    assert toolchain.TESTED_GO_FAMILIES == ("1.25", "1.26", "1.27")


@pytest.mark.parametrize(
    ("family", "version"),
    [("1.26", "1.26.8"), ("1.27", "1.27.1")],
    ids=["go-1.26", "go-1.27"],
)
def test_qualified_new_go_toolchains_are_accepted_under_the_full_lockdown(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    family: str,
    version: str,
):
    host = toolchain._native_host()
    output = f"go version go{version} {host.goos}/{host.goarch}\n"
    config, runner, _, private_base = _setup(tmp_path, version=output)
    config = replace(config, go_future_families="refuse")

    snapshot = toolchain.probe_toolchain(config)
    warning = capsys.readouterr().err

    assert "untested_go_family" not in warning
    assert snapshot.toolchain.go_version == output.strip()
    assert snapshot.toolchain.go_version.split()[2].startswith(f"go{family}.")
    assert snapshot.environment["GOTOOLCHAIN"] == "local"
    assert snapshot.environment["GOENV"] == "off"
    assert snapshot.environment["GOPROXY"] == "off"
    assert snapshot.environment["CGO_ENABLED"] == "0"
    assert [call[0][1:] for call in runner.calls] == [
        ("telemetry", "off"),
        ("version",),
        ("env", "-json", *toolchain.GO_ENV_FIELDS),
    ]
    assert not list(private_base.glob(".csk-go-probe-*"))


@pytest.mark.parametrize(
    "family,version",
    [("1.28", "1.28.0"), ("1.100", "1.100.0"), ("2.0", "2.0.0")],
)
def test_newer_go_family_is_accepted_once_with_full_lockdown_warning(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    family: str,
    version: str,
):
    host = toolchain._native_host()
    output = f"go version go{version} {host.goos}/{host.goarch}\n"
    config, runner, _, private_base = _setup(tmp_path, version=output)
    toolchain.reset_go_future_warning_state()

    toolchain.preflight_toolchain(config)
    snapshot = toolchain.probe_toolchain(config)

    warning = capsys.readouterr().err
    assert warning.count("untested_go_family") == 1
    assert family in warning
    assert str(snapshot.executable) in warning
    assert ", ".join(toolchain.TESTED_GO_FAMILIES) in warning
    assert "full go-v1 lockdown" in warning
    assert snapshot.environment["GOTOOLCHAIN"] == "local"
    assert snapshot.environment["GOENV"] == "off"
    assert snapshot.environment["GOPROXY"] == "off"
    assert snapshot.environment["GOSUMDB"] == "off"
    assert snapshot.environment["CGO_ENABLED"] == "0"
    assert [call[0][1:] for call in runner.calls].count(("telemetry", "off")) == 1
    assert not list(private_base.glob(".csk-go-probe-*"))


@pytest.mark.parametrize(
    ("stored_mode", "environment"),
    [
        ("refuse", {}),
        ("warn", {manager_config.GO_FUTURE_FAMILIES_ENV_VAR: "refuse"}),
    ],
    ids=["config", "environment-override"],
)
def test_newer_go_family_can_be_refused_by_operator_policy(
    tmp_path: Path,
    stored_mode: str,
    environment: dict[str, str],
):
    host = toolchain._native_host()
    output = f"go version go1.28.0 {host.goos}/{host.goarch}\n"
    config, _, _, private_base = _setup(tmp_path, version=output)
    policy = manager_config.resolve_go_future_families(
        manager_config.BuildConfig(go_future_families=stored_mode),
        environment,
    )
    assert policy == "refuse"
    config = replace(config, go_future_families=policy)
    toolchain.reset_go_future_warning_state()

    with pytest.raises(toolchain.ToolchainError) as raised:
        toolchain.establish_toolchain(config)

    _assert_code("unsupported_go_family", raised)
    assert "go1.28.0" in raised.value.detail
    _assert_go_path_in_detail(config, raised.value.detail)
    assert ", ".join(toolchain.TESTED_GO_FAMILIES) in raised.value.detail
    assert "remediation:" in raised.value.detail
    newest_tested = max(toolchain.TESTED_GO_FAMILIES, key=toolchain._go_family_key)
    assert f'go = "{newest_tested}"' in raised.value.detail
    assert "brew install go" in raised.value.detail
    assert not list(private_base.glob(".csk-go-probe-*"))


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
        ("go version go1.23.9 {goos}/{goarch}\n", "unsupported_go_family"),
        ("go version go1.24.9 {goos}/{goarch}\n", "unsupported_go_family"),
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
    if expected_code == "unsupported_go_family":
        assert rendered.split()[2] in raised.value.detail
        _assert_go_path_in_detail(config, raised.value.detail)
        assert ", ".join(toolchain.TESTED_GO_FAMILIES) in raised.value.detail
        assert "remediation:" in raised.value.detail
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

    _assert_code("toolchain_shim_unresolved", raised)
    assert [call[0] for call in runner.calls] == [(str(executable), "env", "GOROOT")]


@pytest.mark.skipif(os.name == "nt", reason="unprivileged Windows symlinks are not portable")
def test_outside_launcher_symlink_resolves_to_real_goroot_binary(tmp_path: Path):
    config, _runner, goroot, _ = _setup(tmp_path)
    operator_bin = tmp_path / "operator-bin"
    operator_bin.mkdir()
    shim = operator_bin / "go"
    shim.symlink_to(goroot / "bin" / "go")
    resolving_runner = ShimRecordingRunner(goroot, shim)
    linked = toolchain.ToolchainConfig(
        private_base=config.private_base,
        operator_search_path=toolchain.OperatorSearchPath((str(operator_bin),)),
        forbidden_roots=config.forbidden_roots,
        runner=resolving_runner,
    )

    session = toolchain.establish_toolchain(linked)
    try:
        executable = str((goroot / "bin" / "go").resolve())
        assert session.snapshot.executable == Path(executable)
        assert session.snapshot.goroot == goroot.resolve()
        assert resolving_runner.shim_calls == 0
        assert [call[0] for call in resolving_runner.calls] == [
            (executable, "telemetry", "off"),
            (executable, "version"),
            (executable, "env", "-json", *toolchain.GO_ENV_FIELDS),
        ]
    finally:
        session.close()


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
    admitted = toolchain._admit_executable(Path(sys.executable), (tmp_path,))
    closed_stdin = runner.run(
        (
            admitted,
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
                admitted,
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
    admitted = toolchain._admit_executable(Path(sys.executable), (tmp_path,))
    with pytest.raises(toolchain.ToolchainError) as raised:
        runner.run(
            (
                admitted,
                "-c",
                "import time; time.sleep(1)",
            ),
            cwd=tmp_path,
            environment={},
            timeout=0.01,
            output_limit=64,
        )
    _assert_code("process_timeout", raised)


def test_default_runner_refuses_an_unadmitted_path_before_start(tmp_path: Path):
    runner = toolchain.SubprocessProbeRunner()
    with pytest.raises(toolchain.ToolchainError) as raised:
        runner.run(
            (sys.executable, "-c", "raise SystemExit(99)"),  # type: ignore[arg-type]
            cwd=tmp_path,
            environment={},
            timeout=1,
            output_limit=64,
        )
    _assert_code("untrusted_go_executable", raised)


def test_runner_rejects_unadmitted_forbidden_alias_before_exec(tmp_path: Path):
    forbidden = tmp_path / "project"
    forbidden.mkdir()
    marker = tmp_path / "executed"
    target = forbidden / "go"
    target.write_text(
        "#!/bin/sh\n"
        f"printf executed > {shlex.quote(str(marker))}\n",
        encoding="utf-8",
    )
    target.chmod(0o755)
    outside = tmp_path / "outside"
    outside.mkdir()
    alias = outside / "go"
    alias.symlink_to(target)
    runner = toolchain.SubprocessProbeRunner()
    raised: toolchain.ToolchainError | None = None
    try:
        runner.run(
            (str(alias),),  # type: ignore[arg-type]
            cwd=tmp_path,
            environment={},
            timeout=1,
            output_limit=64,
        )
    except toolchain.ToolchainError as exc:
        raised = exc

    assert not marker.exists(), "runner executed an unadmitted forbidden alias"
    assert raised is not None, "runner accepted an unadmitted executable"
    assert raised.code == "untrusted_go_executable"


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
    assert runner.calls == []


def test_launcher_accepts_synthesized_stat_with_none_file_attributes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    goroot = _make_goroot(tmp_path / "trusted-go")
    executable = goroot / "bin" / ("go.exe" if os.name == "nt" else "go")
    _override_launcher_file_attributes(monkeypatch, executable, None)

    toolchain._validate_launcher(executable, toolchain._native_host())


@pytest.mark.skipif(
    os.name != "nt",
    reason="Windows os.stat_result exposes st_file_attributes",
)
def test_launcher_accepts_windows_stat_result_with_none_file_attributes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    goroot = _make_goroot(tmp_path / "trusted-go")
    executable = goroot / "bin" / "go.exe"
    original_lstat = Path.lstat
    entry_stat = os.stat_result(tuple(original_lstat(executable)))

    assert entry_stat.st_file_attributes is None
    assert toolchain._is_reparse_point(entry_stat) is False

    def synthesized_lstat(path: Path):
        if path == executable:
            return entry_stat
        return original_lstat(path)

    monkeypatch.setattr(Path, "lstat", synthesized_lstat)
    toolchain._validate_launcher(executable, toolchain._native_host())


def test_launcher_rejects_reparse_file_attribute(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    goroot = _make_goroot(tmp_path / "trusted-go")
    executable = goroot / "bin" / ("go.exe" if os.name == "nt" else "go")
    reparse_flag = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
    _override_launcher_file_attributes(monkeypatch, executable, reparse_flag)

    with pytest.raises(toolchain.ToolchainError) as raised:
        toolchain._validate_launcher(executable, toolchain._native_host())

    _assert_code("untrusted_go_executable", raised)


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
