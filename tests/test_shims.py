from __future__ import annotations

import os
import stat
import subprocess
import sys
from pathlib import Path

import pytest

from csk import shims
from csk.skillspec import CommandSpec

SCRIPT_COMMAND = CommandSpec(
    name="tool",
    type="script",
    unix_path="scripts/tool",
    win_path="scripts/tool.cmd",
)


def _write_runtime_snapshot(root: Path) -> Path:
    """Build a snapshot whose runtime root carries the declared command file."""

    scripts = root / "scripts"
    scripts.mkdir(parents=True)
    (scripts / "tool").write_text("#!/bin/sh\necho snapshot\n", encoding="utf-8")
    (scripts / "tool.cmd").write_text("@echo off\r\necho snapshot\r\n", encoding="utf-8")
    (scripts / "lib.sh").write_text("# helper\n", encoding="utf-8")
    return root


def _cmd_lines(shim: Path) -> list[str]:
    """Return exact CRLF-separated launcher lines without newline translation."""

    raw = shim.read_bytes().decode("utf-8")
    assert raw.endswith("\r\n")
    return raw[: -len("\r\n")].split("\r\n")


def _install_roots(tmp_path: Path, *, platform_name: str = "unix") -> tuple[Path, Path]:
    snapshot = _write_runtime_snapshot(tmp_path / "snapshot")
    csk_home = tmp_path / "home"
    runtime_dir = shims.install_runtime_roots(
        csk_home=csk_home,
        skill_name="skill",
        commit="abc",
        snapshot=snapshot,
        runtime_roots=("scripts",),
        required_commands=(SCRIPT_COMMAND,),
        platform_name=platform_name,
    )
    return csk_home, runtime_dir


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX shim layout uses symlinks")
def test_unix_shim_is_symlink_and_runtime_is_executable(tmp_path):
    snapshot = tmp_path / "snapshot"
    snapshot.mkdir()
    script = snapshot / "scripts" / "tool"
    script.parent.mkdir()
    script.write_text("#!/bin/sh\n", encoding="utf-8")
    command = CommandSpec(name="tool", type="script", unix_path="scripts/tool")
    runtime = shims.install_runtime_command(
        csk_home=tmp_path / "home",
        skill_name="skill",
        commit="abc",
        snapshot=snapshot,
        command=command,
        platform_name="unix",
    )
    shim = shims.write_project_shim(tmp_path / "project", "tool", runtime, platform_name="unix")
    assert shim.is_symlink()
    assert os.access(runtime, os.X_OK)


def test_windows_shim_is_cmd_wrapper(tmp_path):
    runtime = tmp_path / "home" / "runtime" / "skill" / "abc" / "bin" / "tool.cmd"
    runtime.parent.mkdir(parents=True)
    runtime.write_text("@echo off\r\n", encoding="utf-8")
    shim = shims.write_project_shim(tmp_path / "project", "tool", runtime, platform_name="windows")
    assert shim.name == "tool.cmd"
    assert str(runtime) in shim.read_text(encoding="utf-8")


@pytest.mark.skipif(sys.platform == "win32", reason="Executes a POSIX shim")
def test_unix_wrapper_prepends_runtime_path_entries(tmp_path):
    helper_bin = tmp_path / "helper bin"
    helper_bin.mkdir()
    helper = helper_bin / "helper"
    helper.write_text("#!/bin/sh\necho resolved\n", encoding="utf-8")
    helper.chmod(0o755)
    runtime = tmp_path / "runtime" / "tool"
    runtime.parent.mkdir()
    runtime.write_text("#!/bin/sh\nhelper\n", encoding="utf-8")
    runtime.chmod(0o755)

    shim = shims.write_project_shim(
        tmp_path / "project",
        "tool",
        runtime,
        platform_name="unix",
        path_entries=(helper_bin,),
    )
    proc = subprocess.run(
        [str(shim)],
        check=True,
        text=True,
        capture_output=True,
        env={"PATH": os.defpath},
    )

    assert not shim.is_symlink()
    assert proc.stdout.strip() == "resolved"


def test_windows_wrapper_prepends_runtime_path_entries(tmp_path):
    runtime = tmp_path / "home" / "runtime" / "skill" / "abc" / "bin" / "tool.cmd"
    runtime.parent.mkdir(parents=True)
    runtime.write_text("@echo off\r\n", encoding="utf-8")
    helper_bin = tmp_path / "helper bin"

    shim = shims.write_project_shim(
        tmp_path / "project",
        "tool",
        runtime,
        platform_name="windows",
        path_entries=(helper_bin,),
    )
    content = shim.read_text(encoding="utf-8")

    assert "setlocal" in content
    assert f'set "PATH={helper_bin};%PATH%"' in content
    assert f'call "{runtime}" %*' in content


def test_runtime_reuse_keeps_complete_commit_keyed_state(tmp_path):
    csk_home, runtime_dir = _install_roots(tmp_path)
    witness = runtime_dir / "scripts" / "unmanaged-witness"
    witness.write_text("kept\n", encoding="utf-8")

    reused = shims.install_runtime_roots(
        csk_home=csk_home,
        skill_name="skill",
        commit="abc",
        snapshot=tmp_path / "snapshot",
        runtime_roots=("scripts",),
        required_commands=(SCRIPT_COMMAND,),
        platform_name="unix",
    )

    assert reused == runtime_dir
    assert witness.is_file()


@pytest.mark.parametrize("platform_name", ["unix", "windows"])
def test_runtime_reuse_replaces_state_with_a_missing_required_command(tmp_path, platform_name):
    csk_home, runtime_dir = _install_roots(tmp_path, platform_name=platform_name)
    relative = "scripts/tool.cmd" if platform_name == "windows" else "scripts/tool"
    required = runtime_dir / relative
    required.unlink()
    witness = runtime_dir / "scripts" / "unmanaged-witness"
    witness.write_text("discarded\n", encoding="utf-8")

    replaced = shims.install_runtime_roots(
        csk_home=csk_home,
        skill_name="skill",
        commit="abc",
        snapshot=tmp_path / "snapshot",
        runtime_roots=("scripts",),
        required_commands=(SCRIPT_COMMAND,),
        platform_name=platform_name,
    )

    assert replaced == runtime_dir
    assert required.is_file()
    assert not witness.exists()


def test_runtime_reuse_replaces_state_when_a_required_path_is_a_directory(tmp_path):
    csk_home, runtime_dir = _install_roots(tmp_path)
    required = runtime_dir / "scripts" / "tool"
    required.unlink()
    required.mkdir()

    shims.install_runtime_roots(
        csk_home=csk_home,
        skill_name="skill",
        commit="abc",
        snapshot=tmp_path / "snapshot",
        runtime_roots=("scripts",),
        required_commands=(SCRIPT_COMMAND,),
        platform_name="unix",
    )

    assert required.is_file()


@pytest.mark.skipif(sys.platform == "win32", reason="Creates a POSIX symlink")
def test_runtime_reuse_replaces_a_required_path_that_escapes_through_a_link(tmp_path):
    csk_home, runtime_dir = _install_roots(tmp_path)
    outside = tmp_path / "outside"
    outside.write_text("outside\n", encoding="utf-8")
    required = runtime_dir / "scripts" / "tool"
    required.unlink()
    required.symlink_to(outside)

    shims.install_runtime_roots(
        csk_home=csk_home,
        skill_name="skill",
        commit="abc",
        snapshot=tmp_path / "snapshot",
        runtime_roots=("scripts",),
        required_commands=(SCRIPT_COMMAND,),
        platform_name="unix",
    )

    assert required.is_file()
    assert not required.is_symlink()
    assert required.read_text(encoding="utf-8") == "#!/bin/sh\necho snapshot\n"
    assert outside.read_text(encoding="utf-8") == "outside\n"


@pytest.mark.skipif(sys.platform == "win32", reason="Creates a POSIX symlink")
def test_runtime_reuse_replaces_state_reached_through_a_linked_directory(tmp_path):
    csk_home, runtime_dir = _install_roots(tmp_path)
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    (elsewhere / "tool").write_text("#!/bin/sh\necho hijack\n", encoding="utf-8")
    scripts = runtime_dir / "scripts"
    shims._discard(scripts)
    scripts.symlink_to(elsewhere, target_is_directory=True)

    shims.install_runtime_roots(
        csk_home=csk_home,
        skill_name="skill",
        commit="abc",
        snapshot=tmp_path / "snapshot",
        runtime_roots=("scripts",),
        required_commands=(SCRIPT_COMMAND,),
        platform_name="unix",
    )

    assert not (runtime_dir / "scripts").is_symlink()
    assert (runtime_dir / "scripts" / "tool").read_text(encoding="utf-8") == "#!/bin/sh\necho snapshot\n"


def test_runtime_reuse_replaces_a_runtime_path_that_is_not_a_directory(tmp_path):
    snapshot = _write_runtime_snapshot(tmp_path / "snapshot")
    csk_home = tmp_path / "home"
    runtime_dir = shims.runtime_directory(csk_home, "skill", "abc")
    runtime_dir.parent.mkdir(parents=True)
    runtime_dir.write_text("not a directory\n", encoding="utf-8")

    shims.install_runtime_roots(
        csk_home=csk_home,
        skill_name="skill",
        commit="abc",
        snapshot=snapshot,
        runtime_roots=("scripts",),
        required_commands=(SCRIPT_COMMAND,),
        platform_name="unix",
    )

    assert (runtime_dir / "scripts" / "tool").is_file()


def test_runtime_reuse_replaces_state_with_a_missing_declared_root(tmp_path):
    csk_home, runtime_dir = _install_roots(tmp_path)
    shims._discard(runtime_dir / "scripts")

    shims.install_runtime_roots(
        csk_home=csk_home,
        skill_name="skill",
        commit="abc",
        snapshot=tmp_path / "snapshot",
        runtime_roots=("scripts",),
        required_commands=(),
        platform_name="unix",
    )

    assert (runtime_dir / "scripts").is_dir()


def test_runtime_root_command_path_rejects_a_linked_command_file(tmp_path):
    csk_home, runtime_dir = _install_roots(tmp_path)
    if sys.platform == "win32":
        pytest.skip("Creates a POSIX symlink")
    outside = tmp_path / "outside"
    outside.write_text("#!/bin/sh\n", encoding="utf-8")
    required = runtime_dir / "scripts" / "tool"
    required.unlink()
    required.symlink_to(outside)

    with pytest.raises(shims.ShimError, match="runtime file not found"):
        shims.runtime_root_command_path(
            csk_home=csk_home,
            skill_name="skill",
            commit="abc",
            command=SCRIPT_COMMAND,
            platform_name="unix",
        )


def test_install_runtime_command_does_not_write_through_a_stale_link(tmp_path):
    if sys.platform == "win32":
        pytest.skip("Creates a POSIX symlink")
    snapshot = _write_runtime_snapshot(tmp_path / "snapshot")
    csk_home = tmp_path / "home"
    outside = tmp_path / "outside"
    outside.write_text("outside\n", encoding="utf-8")
    runtime_path = shims.runtime_directory(csk_home, "skill", "abc") / "bin" / "tool"
    runtime_path.parent.mkdir(parents=True)
    runtime_path.symlink_to(outside)

    installed = shims.install_runtime_command(
        csk_home=csk_home,
        skill_name="skill",
        commit="abc",
        snapshot=snapshot,
        command=SCRIPT_COMMAND,
        platform_name="unix",
    )

    assert installed == runtime_path
    assert not runtime_path.is_symlink()
    assert outside.read_text(encoding="utf-8") == "outside\n"
    assert os.access(runtime_path, os.X_OK)


def test_runtime_paths_reject_an_unsupported_platform():
    with pytest.raises(shims.ShimError, match="Unsupported activation platform"):
        shims.required_runtime_paths((SCRIPT_COMMAND,), platform_name="plan9")


def test_windows_launcher_forwards_arguments_and_preserves_errorlevel(tmp_path):
    target = tmp_path / "home" / "runtime" / "skill" / "abc" / "bin" / "tool.cmd"
    target.parent.mkdir(parents=True)
    target.write_text("@echo off\r\n", encoding="utf-8")

    shim = shims.write_project_shim(tmp_path / "project", "tool", target, platform_name="windows")
    raw = shim.read_bytes()

    assert _cmd_lines(shim) == [
        "@echo off",
        "setlocal DisableDelayedExpansion",
        'set "ERRORLEVEL="',
        f'call "{target}" %*',
        "exit /b %ERRORLEVEL%",
    ]
    # Exact CRLF on every host: no bare LF and no doubled CR from translation.
    assert raw.endswith(b"\r\n")
    assert raw.replace(b"\r\n", b"") == raw.replace(b"\r\n", b"").replace(b"\r", b"").replace(b"\n", b"")


def test_windows_launcher_escapes_percent_in_target_and_path_entries(tmp_path):
    target = tmp_path / "100%PATH%" / "tool.cmd"
    target.parent.mkdir(parents=True)
    target.write_text("@echo off\r\n", encoding="utf-8")
    helper = tmp_path / "50%helpers"
    helper.mkdir()

    shim = shims.write_project_shim(
        tmp_path / "project",
        "tool",
        target,
        platform_name="windows",
        path_entries=(helper,),
    )
    content = shim.read_text(encoding="utf-8")

    assert _cmd_lines(shim) == [
        "@echo off",
        "setlocal DisableDelayedExpansion",
        'set "ERRORLEVEL="',
        f'set "PATH={str(helper).replace("%", "%%")};%PATH%"',
        f'call "{str(target).replace("%", "%%")}" %*',
        "exit /b %ERRORLEVEL%",
    ]
    # Every literal percent from a path is doubled, so cmd expands nothing the
    # launcher did not intend.
    assert "100%%PATH%%" in content
    assert "50%%helpers" in content


@pytest.mark.parametrize("platform_name", ["unix", "windows"])
@pytest.mark.parametrize(
    "injected",
    ['evil" & echo pwned & rem ', "evil\r\nrem injected", "evil\nrem injected"],
)
def test_launcher_rejects_target_path_injection(tmp_path, platform_name, injected):
    if platform_name == "unix" and sys.platform == "win32":
        pytest.skip("POSIX launcher layout")
    target = tmp_path / injected / "tool"

    with pytest.raises(shims.ShimError, match="must not contain"):
        shims.write_project_shim(
            tmp_path / "project",
            "tool",
            target,
            platform_name=platform_name,
        )


@pytest.mark.parametrize("platform_name", ["unix", "windows"])
def test_launcher_rejects_a_relative_target(tmp_path, platform_name):
    if platform_name == "unix" and sys.platform == "win32":
        pytest.skip("POSIX launcher layout")
    with pytest.raises(shims.ShimError, match="must be an absolute path"):
        shims.write_project_shim(
            tmp_path / "project",
            "tool",
            Path("runtime") / "tool",
            platform_name=platform_name,
        )


@pytest.mark.parametrize("platform_name", ["unix", "windows"])
@pytest.mark.parametrize(
    "command_name",
    ["../evil", "evil/tool", "evil\\tool", 'evil" & echo pwned', "%PATH%", "tool;rm -rf /", ".hidden", ""],
)
def test_launcher_rejects_command_name_injection(tmp_path, platform_name, command_name):
    if platform_name == "unix" and sys.platform == "win32":
        pytest.skip("POSIX launcher layout")
    target = tmp_path / "runtime" / "tool"
    target.parent.mkdir(parents=True)
    target.write_text("#!/bin/sh\n", encoding="utf-8")

    with pytest.raises(shims.ShimError, match="Command name"):
        shims.write_project_shim(
            tmp_path / "project",
            command_name,
            target,
            platform_name=platform_name,
        )


@pytest.mark.parametrize(
    ("platform_name", "separator"),
    [("unix", ":"), ("windows", ";")],
)
def test_launcher_rejects_a_path_entry_carrying_the_platform_separator(tmp_path, platform_name, separator):
    if platform_name == "unix" and sys.platform == "win32":
        pytest.skip("POSIX launcher layout")
    target = tmp_path / "runtime" / "tool"
    target.parent.mkdir(parents=True)
    target.write_text("#!/bin/sh\n", encoding="utf-8")

    with pytest.raises(shims.ShimError, match="must not contain"):
        shims.write_project_shim(
            tmp_path / "project",
            "tool",
            target,
            platform_name=platform_name,
            path_entries=(tmp_path / f"helper{separator}injected",),
        )


@pytest.mark.parametrize("platform_name", ["unix", "windows"])
@pytest.mark.skipif(sys.platform == "win32", reason="Creates a POSIX symlink")
def test_launcher_replaces_an_existing_link_instead_of_writing_through_it(tmp_path, platform_name):
    outside = tmp_path / "outside"
    outside.write_text("outside\n", encoding="utf-8")
    target = tmp_path / "runtime" / "tool"
    target.parent.mkdir(parents=True)
    target.write_text("#!/bin/sh\n", encoding="utf-8")
    bin_dir = tmp_path / "project" / ".agents" / "bin"
    bin_dir.mkdir(parents=True)
    planted = shims.shim_path(bin_dir, "tool", platform_name=platform_name)
    planted.symlink_to(outside)

    shim = shims.write_project_shim(
        tmp_path / "project",
        "tool",
        target,
        platform_name=platform_name,
        path_entries=(target.parent,),
    )

    assert shim == planted
    assert not shim.is_symlink()
    assert outside.read_text(encoding="utf-8") == "outside\n"


@pytest.mark.parametrize("platform_name", ["unix", "windows"])
def test_launcher_rejects_a_directory_at_the_shim_path(tmp_path, platform_name):
    if platform_name == "unix" and sys.platform == "win32":
        pytest.skip("POSIX launcher layout")
    target = tmp_path / "runtime" / "tool"
    target.parent.mkdir(parents=True)
    target.write_text("#!/bin/sh\n", encoding="utf-8")
    bin_dir = tmp_path / "project" / ".agents" / "bin"
    shims.shim_path(bin_dir, "tool", platform_name=platform_name).mkdir(parents=True)

    with pytest.raises(shims.ShimError, match="Launcher path is a directory"):
        shims.write_project_shim(tmp_path / "project", "tool", target, platform_name=platform_name)


@pytest.mark.skipif(sys.platform == "win32", reason="Executes POSIX launchers")
@pytest.mark.parametrize("with_path_entries", [False, True])
def test_unix_launcher_forwards_arguments_and_nonzero_exit_status(tmp_path, with_path_entries):
    target = tmp_path / "runtime" / "tool"
    target.parent.mkdir(parents=True)
    target.write_text('#!/bin/sh\necho "args:$*"\nexit 7\n', encoding="utf-8")
    target.chmod(0o755)
    path_entries = (target.parent,) if with_path_entries else ()

    shim = shims.write_project_shim(
        tmp_path / "project",
        "tool",
        target,
        platform_name="unix",
        path_entries=path_entries,
    )
    proc = subprocess.run(
        [str(shim), "first arg", "--flag=x y", "third"],
        check=False,
        text=True,
        capture_output=True,
        env={"PATH": os.defpath},
    )

    assert proc.returncode == 7
    assert proc.stdout.strip() == "args:first arg --flag=x y third"


@pytest.mark.skipif(sys.platform != "win32", reason="Executes a Windows .cmd launcher")
@pytest.mark.parametrize("with_path_entries", [False, True])
def test_windows_launcher_forwards_arguments_and_nonzero_exit_status(tmp_path, with_path_entries):
    target = tmp_path / "runtime" / "tool.cmd"
    target.parent.mkdir(parents=True)
    target.write_text("@echo off\r\necho args:%*\r\nexit /b 7\r\n", encoding="utf-8")
    path_entries = (target.parent,) if with_path_entries else ()

    shim = shims.write_project_shim(
        tmp_path / "project",
        "tool",
        target,
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

    assert proc.returncode == 7
    assert proc.stdout.strip() == "args:first second"


@pytest.mark.parametrize("platform_name", ["unix", "windows"])
def test_stale_shim_removal_keeps_every_expected_command_name(tmp_path, platform_name):
    if platform_name == "unix" and sys.platform == "win32":
        pytest.skip("POSIX launcher layout")
    target = tmp_path / "runtime" / "tool"
    target.parent.mkdir(parents=True)
    target.write_text("#!/bin/sh\n", encoding="utf-8")
    bin_dir = tmp_path / "bin"
    kept = ["script-tool", "build-tool", "dotted.cmd"]
    for name in [*kept, "removed-tool"]:
        shims.write_bin_shim(bin_dir, name, target, platform_name=platform_name, path_entries=(target.parent,))

    shims.remove_stale_shims_in(bin_dir, set(kept), platform_name=platform_name)

    survivors = sorted(child.name for child in bin_dir.iterdir())
    expected = sorted(
        shims.shim_path(bin_dir, name, platform_name=platform_name).name for name in kept
    )
    assert survivors == expected


def test_windows_stale_shim_removal_maps_a_bare_name_and_a_dot_cmd_name(tmp_path):
    target = tmp_path / "runtime" / "tool.cmd"
    target.parent.mkdir(parents=True)
    target.write_text("@echo off\r\n", encoding="utf-8")
    bin_dir = tmp_path / "bin"
    shims.write_bin_shim(bin_dir, "tool", target, platform_name="windows")
    (bin_dir / "orphan.cmd").write_text("@echo off\r\n", encoding="utf-8")

    shims.remove_stale_shims_in(bin_dir, {"tool"}, platform_name="windows")

    assert (bin_dir / "tool.cmd").is_file()
    assert not (bin_dir / "orphan.cmd").exists()

    shims.remove_stale_shims_in(bin_dir, {"tool.cmd"}, platform_name="windows")

    assert (bin_dir / "tool.cmd").is_file()


def test_runtime_command_and_shim_paths_agree_for_a_dot_cmd_command_name(tmp_path):
    snapshot = _write_runtime_snapshot(tmp_path / "snapshot")
    command = CommandSpec(name="tool.cmd", type="script", win_path="scripts/tool.cmd")
    csk_home = tmp_path / "home"

    runtime = shims.install_runtime_command(
        csk_home=csk_home,
        skill_name="skill",
        commit="abc",
        snapshot=snapshot,
        command=command,
        platform_name="windows",
    )
    shim = shims.write_project_shim(
        tmp_path / "project",
        "tool.cmd",
        runtime,
        platform_name="windows",
    )

    assert runtime.name == "tool.cmd"
    assert shim.name == "tool.cmd"


def test_runtime_paths_reject_skill_and_commit_component_injection(tmp_path):
    with pytest.raises(shims.ShimError, match="Skill name"):
        shims.runtime_directory(tmp_path, "../escape", "abc")
    with pytest.raises(shims.ShimError, match="Commit"):
        shims.runtime_directory(tmp_path, "skill", "../escape")


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX permission model")
def test_unix_runtime_command_becomes_executable(tmp_path):
    snapshot = _write_runtime_snapshot(tmp_path / "snapshot")
    (snapshot / "scripts" / "tool").chmod(0o600)
    csk_home = tmp_path / "home"

    runtime = shims.install_runtime_command(
        csk_home=csk_home,
        skill_name="skill",
        commit="abc",
        snapshot=snapshot,
        command=SCRIPT_COMMAND,
        platform_name="unix",
    )

    assert stat.S_IMODE(runtime.stat().st_mode) & stat.S_IXUSR
