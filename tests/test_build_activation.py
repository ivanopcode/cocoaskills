"""Activation of compiled ``go-v1`` commands against the protected build cache."""

from __future__ import annotations

import hashlib
import os
import shlex
import subprocess
import sys
from dataclasses import dataclass, replace
from pathlib import Path

import pytest

from csk import global_bins, shims
from csk.builds.cache import CacheEntryStatus, CacheInspection
from csk.builds.metadata import (
    BuildArtifact,
    BuildReceipt,
    GoBuildInput,
    build_receipt,
    canonical_receipt_bytes,
    receipt_sha256,
)
from csk.builds.source import BuildSourceIdentity
from csk.builds.toolchain import (
    GO_RELPATH,
    TOOLCHAIN_ALGORITHM,
    NativeTarget,
    ToolchainIdentity,
)
from csk.install_marker import InstallMarkerBuild
from csk.skillspec import CommandSpec

ARTIFACT_MODE = 0o500
UNIX_GOOS = "linux"


def _build_input(command: str = "golden-tool", *, goos: str = UNIX_GOOS) -> GoBuildInput:
    return GoBuildInput(
        build_source=BuildSourceIdentity(
            algorithm="curator-build-source-v1",
            content_sha256="sha256:" + "b" * 64,
        ),
        build_root="build",
        command=command,
        source_dir=f"build/cmd/{command}",
        target=NativeTarget(goos=goos, goarch="amd64", tuning={"GOAMD64": "v1"}),
        toolchain=ToolchainIdentity(
            algorithm=TOOLCHAIN_ALGORITHM,
            content_sha256="sha256:" + "c" * 64,
            go_relpath=GO_RELPATH,
            go_version=f"go version go1.25.1 {goos}/amd64",
        ),
    )


@dataclass(frozen=True)
class _Entry:
    """One published protected-cache entry plus the state a caller must hold."""

    csk_home: Path
    command: CommandSpec
    receipt: BuildReceipt
    receipt_sha256: str
    artifact_path: Path

    @property
    def inspection(self) -> CacheInspection:
        return CacheInspection(
            status=CacheEntryStatus.HIT,
            reason="exact protected entry",
            receipt=self.receipt,
            receipt_bytes=canonical_receipt_bytes(self.receipt),
            receipt_sha256=self.receipt_sha256,
            artifact_path=self.artifact_path,
        )

    @property
    def marker_build(self) -> InstallMarkerBuild:
        return InstallMarkerBuild(
            driver="go-v1",
            cache_key=self.receipt.cache_key,
            receipt_sha256=self.receipt_sha256,
            artifact_sha256=self.receipt.artifact.sha256,
            artifact_path=self.receipt.artifact.path,
        )


def _publish(
    tmp_path: Path,
    *,
    command_name: str = "golden-tool",
    goos: str = UNIX_GOOS,
    artifact_bytes: bytes = b"#!/bin/sh\necho compiled\n",
    home_name: str = ".cocoaskills",
    mode: int = ARTIFACT_MODE,
) -> _Entry:
    """Lay out one immutable cache entry the way a protected backend would."""

    build_input = _build_input(command_name, goos=goos)
    digest = hashlib.sha256(artifact_bytes).hexdigest()
    receipt = build_receipt(
        build_input,
        BuildArtifact(
            path=build_input.artifact_path,
            sha256=f"sha256:{digest}",
            size=len(artifact_bytes),
        ),
    )
    csk_home = tmp_path / home_name
    artifact_path = (
        csk_home
        / "builds"
        / "go-v1"
        / receipt.cache_key.removeprefix("sha256:")
        / Path(*build_input.artifact_path.split("/"))
    )
    artifact_path.parent.mkdir(parents=True, exist_ok=True)
    artifact_path.write_bytes(artifact_bytes)
    if os.name == "posix":
        artifact_path.chmod(mode)
    return _Entry(
        csk_home=csk_home,
        command=CommandSpec(
            name=command_name,
            type="build",
            driver="go-v1",
            source_dir=build_input.source_dir,
        ),
        receipt=receipt,
        receipt_sha256=receipt_sha256(canonical_receipt_bytes(receipt)),
        artifact_path=artifact_path,
    )


def _runnable_windows_activation(entry: _Entry, artifact_bytes: bytes) -> shims.BuildCommandActivation:
    """Retarget a validated activation at a runnable ``.cmd`` stand-in.

    Windows cannot execute a hand-written ``.exe``, and the contract under test
    is launcher behavior rather than the compiled artifact format.
    """

    runnable = entry.artifact_path.with_suffix(".cmd")
    runnable.write_bytes(artifact_bytes)
    validated = _select(entry, platform_name="windows")
    return shims.BuildCommandActivation(
        command_name=validated.command_name,
        artifact_path=runnable,
        cache_key=validated.cache_key,
        receipt_sha256=validated.receipt_sha256,
        artifact_sha256=validated.artifact_sha256,
        artifact_size=validated.artifact_size,
    )


def _select(entry: _Entry, *, platform_name: str = "unix", **overrides: object) -> shims.BuildCommandActivation:
    arguments: dict[str, object] = {
        "csk_home": entry.csk_home,
        "command": entry.command,
        "marker_build": entry.marker_build,
        "inspection": entry.inspection,
        "platform_name": platform_name,
    }
    arguments.update(overrides)
    return shims.select_build_activation(**arguments)  # type: ignore[arg-type]


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX activation layout")
def test_unix_project_activation_targets_the_immutable_cache_artifact(tmp_path):
    entry = _publish(tmp_path)

    activation = _select(entry)
    shim = shims.write_project_build_shim(tmp_path / "project", activation, platform_name="unix")

    assert activation.artifact_path == entry.artifact_path
    assert activation.cache_key == entry.receipt.cache_key
    assert shim == tmp_path / "project" / ".agents" / "bin" / "golden-tool"
    assert shim.is_symlink()
    assert shim.resolve() == entry.artifact_path.resolve()
    assert not str(shim.resolve()).startswith(str(entry.csk_home / "runtime"))
    assert oct(entry.artifact_path.stat().st_mode)[-3:] == oct(ARTIFACT_MODE)[-3:]


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX activation layout")
def test_unix_global_activation_targets_the_immutable_cache_artifact(tmp_path):
    entry = _publish(tmp_path)

    shim = shims.write_global_build_shim(entry.csk_home, _select(entry), platform_name="unix")

    assert shim == entry.csk_home / "global" / "bin" / "golden-tool"
    assert shim.resolve() == entry.artifact_path.resolve()


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX activation layout")
def test_unix_activation_needs_no_shell_profile(tmp_path):
    entry = _publish(tmp_path)
    home_before = sorted(path.name for path in (tmp_path).iterdir())

    shims.write_project_build_shim(
        tmp_path / "project",
        _select(entry),
        platform_name="unix",
        path_entries=(tmp_path / "helpers",),
    )
    content = (tmp_path / "project" / ".agents" / "bin" / "golden-tool").read_text(encoding="utf-8")

    # The whole launcher is asserted: it reads no profile, sources nothing, and
    # execs the cache artifact directly.
    assert content == (
        "#!/bin/sh\n"
        'if [ -n "${PATH:-}" ]; then\n'
        f"  PATH={shlex.quote(str(tmp_path / 'helpers'))}:\"$PATH\"\n"
        "else\n"
        f"  PATH={shlex.quote(str(tmp_path / 'helpers'))}\n"
        "fi\n"
        "export PATH\n"
        f'exec {shlex.quote(str(entry.artifact_path))} "$@"\n'
    )
    assert sorted(path.name for path in tmp_path.iterdir()) == sorted({*home_before, "project"})


@pytest.mark.parametrize("scope", ["project", "global"])
def test_windows_activation_quotes_the_executable_and_forwards_argv(tmp_path, scope):
    entry = _publish(tmp_path, goos="windows")
    activation = _select(entry, platform_name="windows")

    if scope == "project":
        shim = shims.write_project_build_shim(tmp_path / "project", activation, platform_name="windows")
    else:
        shim = shims.write_global_build_shim(entry.csk_home, activation, platform_name="windows")

    assert shim.name == "golden-tool.cmd"
    assert entry.artifact_path.name == "golden-tool.exe"
    assert shim.read_text(encoding="utf-8").splitlines() == [
        "@echo off",
        "setlocal DisableDelayedExpansion",
        'set "ERRORLEVEL="',
        f'call "{entry.artifact_path}" %*',
        "exit /b %ERRORLEVEL%",
    ]


def test_windows_activation_escapes_percent_in_the_artifact_path(tmp_path):
    entry = _publish(tmp_path, goos="windows", home_name="100%COMSPEC%")

    shim = shims.write_project_build_shim(
        tmp_path / "project",
        _select(entry, platform_name="windows"),
        platform_name="windows",
    )
    content = shim.read_text(encoding="utf-8")

    assert f'call "{str(entry.artifact_path).replace("%", "%%")}" %*' in content
    assert "100%%COMSPEC%%" in content


@pytest.mark.parametrize(
    "injected",
    ['evil" & echo pwned & rem ', "evil\r\nrem injected", "evil\nrem injected"],
)
def test_activation_rejects_an_injectable_artifact_path(tmp_path, injected):
    # Windows cannot even hold such a name on disk, so the typed activation is
    # the layer that must refuse it: no launcher can be built from one.
    with pytest.raises(shims.ShimError, match="must not contain"):
        shims.BuildCommandActivation(
            command_name="golden-tool",
            artifact_path=tmp_path / injected / "bin" / "golden-tool",
            cache_key="sha256:" + "a" * 64,
            receipt_sha256="sha256:" + "b" * 64,
            artifact_sha256="sha256:" + "c" * 64,
            artifact_size=1,
        )


@pytest.mark.parametrize("platform_name", ["unix", "windows"])
def test_activation_rejects_a_command_name_that_is_not_a_portable_identifier(tmp_path, platform_name):
    if platform_name == "unix" and sys.platform == "win32":
        pytest.skip("POSIX activation layout")
    goos = "windows" if platform_name == "windows" else UNIX_GOOS
    entry = _publish(tmp_path, goos=goos)
    hostile = replace(entry.command, name='golden-tool" & echo pwned')

    with pytest.raises(shims.ShimError, match="Command name"):
        _select(entry, platform_name=platform_name, command=hostile)


def test_activation_rejects_a_script_command(tmp_path):
    entry = _publish(tmp_path)
    script = CommandSpec(name="golden-tool", type="script", unix_path="scripts/golden-tool")

    with pytest.raises(shims.ShimError, match="is not a build command"):
        _select(entry, command=script)


def test_activation_rejects_an_unsupported_driver(tmp_path):
    entry = _publish(tmp_path)

    with pytest.raises(shims.ShimError, match="driver must be"):
        _select(entry, command=replace(entry.command, driver="go-v2"))


@pytest.mark.parametrize(
    "status",
    [
        CacheEntryStatus.MISS,
        CacheEntryStatus.CORRUPT,
        CacheEntryStatus.UNTRUSTED_PROVENANCE,
        CacheEntryStatus.UNSUPPORTED,
    ],
)
def test_activation_requires_a_protected_cache_hit(tmp_path, status):
    entry = _publish(tmp_path)
    inspection = CacheInspection(status=status, reason="not reusable")

    with pytest.raises(shims.ShimError, match="no protected cache hit"):
        _select(entry, inspection=inspection)


def test_activation_requires_receipt_and_artifact_state_on_a_hit(tmp_path):
    entry = _publish(tmp_path)
    inspection = CacheInspection(status=CacheEntryStatus.HIT, reason="hit without paths")

    with pytest.raises(shims.ShimError, match="missing receipt or artifact state"):
        _select(entry, inspection=inspection)


def test_activation_rejects_a_receipt_hash_the_marker_does_not_record(tmp_path):
    entry = _publish(tmp_path)
    marker = replace(entry.marker_build, receipt_sha256="sha256:" + "d" * 64)

    with pytest.raises(shims.ShimError, match="marker receipt hash"):
        _select(entry, marker_build=marker)


def test_activation_rejects_a_cache_key_the_marker_does_not_record(tmp_path):
    entry = _publish(tmp_path)
    marker = replace(entry.marker_build, cache_key="sha256:" + "e" * 64)

    with pytest.raises(shims.ShimError, match="marker cache key"):
        _select(entry, marker_build=marker)


def test_activation_rejects_an_artifact_hash_the_marker_does_not_record(tmp_path):
    entry = _publish(tmp_path)
    marker = replace(entry.marker_build, artifact_sha256="sha256:" + "f" * 64)

    with pytest.raises(shims.ShimError, match="marker artifact hash"):
        _select(entry, marker_build=marker)


def test_activation_rejects_a_marker_artifact_path_that_is_not_derived(tmp_path):
    entry = _publish(tmp_path)
    marker = replace(entry.marker_build, artifact_path="bin/other-tool")

    with pytest.raises(shims.ShimError, match="marker artifact path"):
        _select(entry, marker_build=marker)


def test_activation_rejects_a_receipt_built_for_another_command(tmp_path):
    entry = _publish(tmp_path)
    other = _publish(tmp_path, command_name="other-tool")

    with pytest.raises(shims.ShimError, match="was built for command"):
        _select(entry, command=other.command, marker_build=other.marker_build)


@pytest.mark.parametrize(
    ("platform_name", "goos"),
    [("unix", "windows"), ("windows", UNIX_GOOS)],
)
def test_activation_rejects_a_receipt_target_that_is_not_the_activation_platform(tmp_path, platform_name, goos):
    if platform_name == "unix" and sys.platform == "win32":
        pytest.skip("POSIX activation layout")
    entry = _publish(tmp_path, goos=goos)

    with pytest.raises(shims.ShimError, match="does not match"):
        _select(entry, platform_name=platform_name)


def test_activation_rejects_an_artifact_whose_size_left_the_receipt(tmp_path):
    entry = _publish(tmp_path)
    if os.name == "posix":
        entry.artifact_path.chmod(0o700)
    entry.artifact_path.write_bytes(b"#!/bin/sh\necho swapped for longer bytes\n")
    if os.name == "posix":
        entry.artifact_path.chmod(ARTIFACT_MODE)

    with pytest.raises(shims.ShimError, match="does not match receipt size"):
        _select(entry)


def test_activation_rejects_a_missing_artifact(tmp_path):
    entry = _publish(tmp_path)
    entry.artifact_path.unlink()

    with pytest.raises(shims.ShimError, match="artifact is unavailable"):
        _select(entry)


@pytest.mark.skipif(sys.platform == "win32", reason="Creates a POSIX symlink")
def test_activation_rejects_an_artifact_reached_through_a_link(tmp_path):
    entry = _publish(tmp_path)
    real = tmp_path / "outside-artifact"
    real.write_bytes(entry.artifact_path.read_bytes())
    real.chmod(0o700)
    entry.artifact_path.unlink()
    entry.artifact_path.symlink_to(real)

    with pytest.raises(shims.ShimError, match="not a regular file"):
        _select(entry)


@pytest.mark.skipif(os.name != "posix", reason="POSIX permission model")
def test_activation_rejects_an_artifact_that_is_not_owner_executable(tmp_path):
    entry = _publish(tmp_path, mode=0o400)

    with pytest.raises(shims.ShimError, match="not owner-executable"):
        _select(entry)


def test_activation_rejects_an_artifact_outside_the_manager_home(tmp_path):
    entry = _publish(tmp_path)

    with pytest.raises(shims.ShimError, match="must live below the manager home"):
        _select(entry, csk_home=tmp_path / "other-home")


def test_activation_rejects_an_artifact_in_the_script_runtime_namespace(tmp_path):
    entry = _publish(tmp_path)
    hijacked = entry.csk_home / "runtime" / "skill" / "abc" / "bin" / entry.artifact_path.name
    hijacked.parent.mkdir(parents=True)
    hijacked.write_bytes(entry.artifact_path.read_bytes())
    if os.name == "posix":
        hijacked.chmod(ARTIFACT_MODE)
    inspection = CacheInspection(
        status=CacheEntryStatus.HIT,
        reason="exact protected entry",
        receipt=entry.receipt,
        receipt_bytes=canonical_receipt_bytes(entry.receipt),
        receipt_sha256=entry.receipt_sha256,
        artifact_path=hijacked,
    )

    with pytest.raises(shims.ShimError, match="script runtime namespace"):
        _select(entry, inspection=inspection)


def test_activation_rejects_an_artifact_path_that_is_not_the_derived_tail(tmp_path):
    entry = _publish(tmp_path)
    misplaced = entry.artifact_path.parent.parent / "other" / entry.artifact_path.name
    misplaced.parent.mkdir(parents=True)
    misplaced.write_bytes(entry.artifact_path.read_bytes())
    if os.name == "posix":
        misplaced.chmod(ARTIFACT_MODE)
    inspection = CacheInspection(
        status=CacheEntryStatus.HIT,
        reason="exact protected entry",
        receipt=entry.receipt,
        receipt_bytes=canonical_receipt_bytes(entry.receipt),
        receipt_sha256=entry.receipt_sha256,
        artifact_path=misplaced,
    )

    with pytest.raises(shims.ShimError, match="does not end in"):
        _select(entry, inspection=inspection)


@pytest.mark.skipif(sys.platform == "win32", reason="Runs a POSIX artifact")
@pytest.mark.parametrize("scope", ["project", "global"])
def test_activation_never_launches_the_built_artifact(tmp_path, scope):
    sentinel = tmp_path / "launched"
    artifact_bytes = f'#!/bin/sh\n: > {sentinel}\n'.encode()
    entry = _publish(tmp_path, artifact_bytes=artifact_bytes)

    activation = _select(entry)
    if scope == "project":
        shim = shims.write_project_build_shim(tmp_path / "project", activation, platform_name="unix")
    else:
        shim = shims.write_global_build_shim(entry.csk_home, activation, platform_name="unix")

    assert shim.exists()
    assert not sentinel.exists()

    subprocess.run([str(shim)], check=True, env={"PATH": os.defpath})

    assert sentinel.exists()


@pytest.mark.skipif(sys.platform == "win32", reason="Runs a POSIX artifact")
@pytest.mark.parametrize("with_path_entries", [False, True])
def test_activated_unix_command_forwards_argv_and_nonzero_exit_status(tmp_path, with_path_entries):
    artifact_bytes = b'#!/bin/sh\necho "args:$*"\nexit 9\n'
    entry = _publish(tmp_path, artifact_bytes=artifact_bytes)
    path_entries = (tmp_path / "helpers",) if with_path_entries else ()

    shim = shims.write_project_build_shim(
        tmp_path / "project",
        _select(entry),
        platform_name="unix",
        path_entries=path_entries,
    )
    proc = subprocess.run(
        [str(shim), "first arg", "--flag=x y"],
        check=False,
        text=True,
        capture_output=True,
        env={"PATH": os.defpath},
    )

    assert proc.returncode == 9
    assert proc.stdout.strip() == "args:first arg --flag=x y"


@pytest.mark.skipif(sys.platform != "win32", reason="Runs a Windows artifact launcher")
@pytest.mark.parametrize("scope", ["project", "global"])
def test_windows_activation_never_launches_the_built_artifact(tmp_path, scope):
    sentinel = tmp_path / "launched"
    artifact_bytes = f'@echo off\r\ntype nul > "{sentinel}"\r\n'.encode()
    entry = _publish(tmp_path, goos="windows", artifact_bytes=artifact_bytes)
    activation = _runnable_windows_activation(entry, artifact_bytes)

    if scope == "project":
        shim = shims.write_project_build_shim(tmp_path / "project", activation, platform_name="windows")
    else:
        shim = shims.write_global_build_shim(entry.csk_home, activation, platform_name="windows")

    assert shim.exists()
    assert not sentinel.exists()

    subprocess.run([str(shim)], check=True)

    assert sentinel.exists()


@pytest.mark.skipif(sys.platform != "win32", reason="Runs a Windows artifact launcher")
@pytest.mark.parametrize("with_path_entries", [False, True])
def test_activated_windows_command_forwards_argv_and_nonzero_exit_status(tmp_path, with_path_entries):
    artifact_bytes = b"@echo off\r\necho args:%*\r\nexit /b 9\r\n"
    entry = _publish(tmp_path, goos="windows", artifact_bytes=artifact_bytes)
    path_entries = (tmp_path / "helpers",) if with_path_entries else ()

    shim = shims.write_project_build_shim(
        tmp_path / "project",
        _runnable_windows_activation(entry, artifact_bytes),
        platform_name="windows",
        path_entries=path_entries,
    )
    env = dict(os.environ)
    # A poisoned ERRORLEVEL variable must not shadow the real exit status.
    env["ERRORLEVEL"] = "0"
    proc = subprocess.run(
        [str(shim), "first", "second"],
        check=False,
        text=True,
        capture_output=True,
        env=env,
    )

    assert proc.returncode == 9
    assert proc.stdout.strip() == "args:first second"


@pytest.mark.parametrize("platform_name", ["unix", "windows"])
def test_mixed_script_and_build_commands_share_one_launcher_namespace(tmp_path, platform_name):
    if platform_name == "unix" and sys.platform == "win32":
        pytest.skip("POSIX activation layout")
    goos = "windows" if platform_name == "windows" else UNIX_GOOS
    entry = _publish(tmp_path, goos=goos)
    runtime = tmp_path / "runtime" / "script-tool"
    runtime.parent.mkdir(parents=True)
    runtime.write_text("#!/bin/sh\n", encoding="utf-8")
    bin_dir = tmp_path / "project" / ".agents" / "bin"

    shims.write_bin_shim(bin_dir, "script-tool", runtime, platform_name=platform_name)
    shims.activate_build_command(bin_dir, _select(entry, platform_name=platform_name), platform_name=platform_name)
    shims.write_bin_shim(bin_dir, "removed-tool", runtime, platform_name=platform_name)
    shims.remove_stale_shims_in(bin_dir, {"script-tool", "golden-tool"}, platform_name=platform_name)

    assert sorted(child.name for child in bin_dir.iterdir()) == sorted(
        shims.shim_path(bin_dir, name, platform_name=platform_name).name
        for name in ("golden-tool", "script-tool")
    )


@pytest.mark.parametrize("platform_name", ["unix", "windows"])
def test_a_build_command_replaces_a_script_launcher_of_the_same_name(tmp_path, platform_name):
    if platform_name == "unix" and sys.platform == "win32":
        pytest.skip("POSIX activation layout")
    goos = "windows" if platform_name == "windows" else UNIX_GOOS
    entry = _publish(tmp_path, goos=goos)
    runtime = tmp_path / "runtime" / "golden-tool"
    runtime.parent.mkdir(parents=True)
    runtime.write_text("#!/bin/sh\n", encoding="utf-8")
    bin_dir = tmp_path / "project" / ".agents" / "bin"

    script_shim = shims.write_bin_shim(bin_dir, "golden-tool", runtime, platform_name=platform_name)
    build_shim = shims.activate_build_command(
        bin_dir,
        _select(entry, platform_name=platform_name),
        platform_name=platform_name,
    )

    assert script_shim == build_shim
    assert sorted(child.name for child in bin_dir.iterdir()) == [build_shim.name]
    if platform_name == "windows":
        assert str(entry.artifact_path) in build_shim.read_text(encoding="utf-8")
    else:
        assert build_shim.resolve() == entry.artifact_path.resolve()


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX user-bin shims use symlinks")
def test_user_bin_publication_treats_build_and_script_commands_alike(tmp_path):
    entry = _publish(tmp_path)
    runtime = tmp_path / "runtime" / "script-tool"
    runtime.parent.mkdir(parents=True)
    runtime.write_text("#!/bin/sh\necho script\n", encoding="utf-8")
    runtime.chmod(0o755)
    canonical_build = shims.write_global_build_shim(entry.csk_home, _select(entry), platform_name="unix")
    canonical_script = shims.write_global_shim(entry.csk_home, "script-tool", runtime, platform_name="unix")
    user_bin = tmp_path / "user bin"

    messages = global_bins.refresh_user_bin_shims(
        entry.csk_home,
        {"golden-tool", "script-tool"},
        platform_name="unix",
        env={"CSK_GLOBAL_USER_BIN": str(user_bin), "PATH": ""},
        home=tmp_path,
    )

    assert messages == [f"global: command shims published to {user_bin}"]
    assert (user_bin / "golden-tool").resolve() == canonical_build.resolve()
    assert (user_bin / "script-tool").resolve() == canonical_script.resolve()

    global_bins.refresh_user_bin_shims(
        entry.csk_home,
        {"script-tool"},
        platform_name="unix",
        env={"CSK_GLOBAL_USER_BIN": str(user_bin), "PATH": ""},
        home=tmp_path,
    )

    assert not (user_bin / "golden-tool").exists()
    assert (user_bin / "script-tool").is_symlink()


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX user-bin shims use symlinks")
def test_user_bin_publication_reports_an_unmanaged_build_command_conflict(tmp_path):
    entry = _publish(tmp_path)
    shims.write_global_build_shim(entry.csk_home, _select(entry), platform_name="unix")
    user_bin = tmp_path / "user bin"
    user_bin.mkdir()
    manual = user_bin / "golden-tool"
    manual.write_text("#!/bin/sh\necho manual\n", encoding="utf-8")

    messages = global_bins.refresh_user_bin_shims(
        entry.csk_home,
        {"golden-tool"},
        platform_name="unix",
        env={"CSK_GLOBAL_USER_BIN": str(user_bin), "PATH": ""},
        home=tmp_path,
    )

    assert messages == [
        (
            f"global: command {'golden-tool'!r} not published to {user_bin}; "
            f"target exists and is not managed by csk: {manual}"
        )
    ]
    assert manual.read_text(encoding="utf-8") == "#!/bin/sh\necho manual\n"


def test_build_activation_rejects_a_hand_built_relative_artifact_path():
    with pytest.raises(shims.ShimError, match="must be an absolute path"):
        shims.BuildCommandActivation(
            command_name="golden-tool",
            artifact_path=Path("bin/golden-tool"),
            cache_key="sha256:" + "a" * 64,
            receipt_sha256="sha256:" + "b" * 64,
            artifact_sha256="sha256:" + "c" * 64,
            artifact_size=1,
        )


def test_build_activation_rejects_a_malformed_identity(tmp_path):
    with pytest.raises(shims.ShimError, match="64 lowercase hexadecimal digits"):
        shims.BuildCommandActivation(
            command_name="golden-tool",
            artifact_path=tmp_path / "bin" / "golden-tool",
            cache_key="sha256:not-hex",
            receipt_sha256="sha256:" + "b" * 64,
            artifact_sha256="sha256:" + "c" * 64,
            artifact_size=1,
        )


def test_activate_build_command_rejects_an_untyped_activation(tmp_path):
    with pytest.raises(shims.ShimError, match="must be a BuildCommandActivation"):
        shims.activate_build_command(
            tmp_path / "bin",
            "golden-tool",  # type: ignore[arg-type]
            platform_name="unix",
        )
