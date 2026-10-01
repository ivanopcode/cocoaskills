from __future__ import annotations

import os
import shutil
import subprocess
import sys

import pytest

from csk import env_files


def test_env_files_generated(tmp_path):
    project = tmp_path / "project"
    env_files.write_env_files(project)
    assert ".agents/bin" in (project / ".agents" / "env.sh").read_text(encoding="utf-8")
    assert ".agents\\bin" in (project / ".agents" / "env.ps1").read_text(encoding="utf-8")


def test_env_ps1_appends_project_bin(tmp_path):
    env_files.write_env_files(tmp_path)
    env_ps1 = (tmp_path / ".agents" / "env.ps1").read_text(encoding="utf-8")
    assert 'if ($env:PATH) {' in env_ps1
    assert '$env:PATH = "$env:PATH;$CskProjectRoot\\.agents\\bin"' in env_ps1
    assert '$env:PATH = "$CskProjectRoot\\.agents\\bin"' in env_ps1


@pytest.mark.skipif(sys.platform == "win32", reason="native POSIX PATH semantics")
@pytest.mark.parametrize("shell", ["bash", "zsh"])
@pytest.mark.parametrize("initial_path", ["", "/usr/bin:/bin"], ids=["empty", "set"])
def test_env_sh_never_introduces_empty_path_entry(tmp_path, shell, initial_path):
    executable = shutil.which(shell)
    if executable is None:
        pytest.skip(f"{shell} not available")
    project = tmp_path / "project with spaces"
    env_files.write_env_files(project)
    arguments = ["-dfc"] if shell == "zsh" else ["--noprofile", "--norc", "-c"]
    completed = subprocess.run(
        [executable, *arguments, '. "$ENV_FILE" || exit; printf "%s\\n" "$CSK_PROJECT_ROOT" "$PATH"'],
        cwd=tmp_path,
        env={**os.environ, "PATH": initial_path, "ENV_FILE": str(project / ".agents" / "env.sh")},
        text=True, capture_output=True, timeout=120, check=False,
    )
    assert completed.returncode == 0, completed.stderr
    assert completed.stderr == ""
    root, path = completed.stdout.splitlines()
    assert root == str(project.resolve())
    expected = ([initial_path] if initial_path else []) + [str(project.resolve() / ".agents" / "bin")]
    assert path == ":".join(expected)
    assert "" not in path.split(":")


@pytest.mark.parametrize("initial_path", ["", "trusted;other"], ids=["empty", "set"])
def test_env_ps1_never_introduces_empty_path_entry(tmp_path, initial_path):
    executable = shutil.which("pwsh") or shutil.which("powershell")
    if executable is None:
        if sys.platform == "win32":
            pytest.fail("PowerShell is required on Windows")
        pytest.skip("PowerShell not available")
    project = tmp_path / "project with spaces"
    env_files.write_env_files(project)
    completed = subprocess.run(
        [executable, "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-Command",
         "$ErrorActionPreference = 'Stop'; $env:PATH = $env:INITIAL_PATH; . $env:ENV_FILE; Write-Output $env:PATH"],
        cwd=tmp_path,
        env={**os.environ, "INITIAL_PATH": initial_path, "ENV_FILE": str(project / ".agents" / "env.ps1")},
        text=True, capture_output=True, timeout=120, check=False,
    )
    assert completed.returncode == 0, completed.stderr
    expected = ([initial_path] if initial_path else []) + [f"{project}\\.agents\\bin"]
    path = completed.stdout.strip()
    assert path == ";".join(expected)
    assert "" not in path.split(";")


@pytest.mark.skipif(sys.platform == "win32", reason="native POSIX PATH semantics")
@pytest.mark.parametrize("shell", ["bash", "zsh"])
@pytest.mark.parametrize("real_bin_first", [True, False], ids=["first-entry", "later-entry"])
def test_env_sh_keeps_real_binaries_first(tmp_path, shell, real_bin_first):
    executable = shutil.which(shell)
    if executable is None:
        pytest.skip(f"{shell} not available")
    project = tmp_path / "project with spaces"
    env_files.write_env_files(project)
    project_bin = project / ".agents" / "bin"
    real_bin = tmp_path / "real bin"
    other_bin = tmp_path / "other bin"
    for directory in (project_bin, real_bin, other_bin):
        directory.mkdir()
    for directory, name, output in (
        (real_bin, "git", "real-binary"),
        (project_bin, "git", "project-shadow"),
        (project_bin, "csk-project-only-probe", "project-command"),
    ):
        command = directory / name
        command.write_text(f"#!/bin/sh\nprintf '%s\\n' '{output}'\n", encoding="utf-8")
        command.chmod(0o755)
    entries = [real_bin, other_bin] if real_bin_first else [other_bin, real_bin]
    original_path = os.pathsep.join([*(str(entry) for entry in entries), os.defpath])
    script = r'''
. "$ENV_FILE" || exit
git
csk-project-only-probe
printf '%s\n' "$PATH"
'''
    arguments = ["-dfc"] if shell == "zsh" else ["--noprofile", "--norc", "-c"]
    completed = subprocess.run(
        [executable, *arguments, script],
        cwd=tmp_path,
        env={**os.environ, "PATH": original_path, "ENV_FILE": str(project / ".agents" / "env.sh")},
        text=True,
        capture_output=True,
        timeout=120,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr
    output = completed.stdout.splitlines()
    assert output[:2] == ["real-binary", "project-command"]
    assert output[2] == f"{original_path}{os.pathsep}{project_bin.resolve()}"


@pytest.mark.skipif(sys.platform != "win32", reason="native Windows PowerShell PATH semantics")
@pytest.mark.parametrize("real_bin_first", [True, False], ids=["first-entry", "later-entry"])
def test_env_ps1_keeps_real_binaries_first(tmp_path, real_bin_first):
    executable = shutil.which("pwsh") or shutil.which("powershell")
    assert executable is not None, "PowerShell is required on Windows"
    project = tmp_path / "project with spaces"
    env_files.write_env_files(project)
    project_bin = project / ".agents" / "bin"
    real_bin = tmp_path / "real bin"
    other_bin = tmp_path / "other bin"
    for directory in (project_bin, real_bin, other_bin):
        directory.mkdir()
    for directory, name, output in (
        (real_bin, "git.cmd", "real-binary"),
        (project_bin, "git.cmd", "project-shadow"),
        (project_bin, "csk-project-only-probe.cmd", "project-command"),
    ):
        (directory / name).write_text(f"@echo off\necho {output}\n", encoding="utf-8")
    entries = [real_bin, other_bin] if real_bin_first else [other_bin, real_bin]
    original_path = os.pathsep.join([*(str(entry) for entry in entries), os.environ["PATH"]])
    script = r'''
$ErrorActionPreference = 'Stop'
Write-Output $env:PATH
. $env:ENV_FILE
& (Get-Command git -CommandType Application)[0].Source
& (Get-Command csk-project-only-probe -CommandType Application)[0].Source
Write-Output $env:PATH
'''
    completed = subprocess.run(
        [executable, "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-Command", script],
        cwd=tmp_path,
        env={**os.environ, "PATH": original_path, "ENV_FILE": str(project / ".agents" / "env.ps1")},
        text=True,
        capture_output=True,
        timeout=120,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr
    output = completed.stdout.splitlines()
    inherited_path = output[0]
    assert inherited_path.endswith(original_path)
    assert output[1:3] == ["real-binary", "project-command"]
    assert output[3] == f"{inherited_path}{os.pathsep}{project_bin}"


def test_staged_global_env_files_activate_the_final_manager_home(tmp_path):
    staged_home = tmp_path / "private-stage"
    final_home = tmp_path / "manager home"

    env_files.write_global_env_files(
        staged_home,
        activation_home=final_home,
    )

    env_sh = (staged_home / "global" / "env.sh").read_text(
        encoding="utf-8"
    )
    env_ps1 = (staged_home / "global" / "env.ps1").read_text(
        encoding="utf-8"
    )
    assert str(final_home / "global") in env_sh
    assert str(final_home / "global") in env_ps1
    assert str(staged_home / "global") not in env_sh
    assert str(staged_home / "global") not in env_ps1


def _source_and_print_root(shell: str, env_sh, cwd) -> str:
    proc = subprocess.run(
        [shell, "-c", f'. "{env_sh}" && printf %s "$CSK_PROJECT_ROOT"'],
        cwd=cwd,
        text=True,
        capture_output=True,
        check=False,
    )
    assert proc.returncode == 0, proc.stderr
    return proc.stdout


def _shell_can_run(shell: str) -> bool:
    proc = subprocess.run(
        [shell, "-c", "printf ok"],
        text=True,
        capture_output=True,
        check=False,
    )
    return proc.returncode == 0 and proc.stdout == "ok"


@pytest.mark.parametrize("shell", ["bash", "zsh"])
def test_env_sh_resolves_project_root_when_sourced_from_elsewhere(tmp_path, shell):
    if shutil.which(shell) is None:
        pytest.skip(f"{shell} not available")
    if not _shell_can_run(shell):
        pytest.skip(f"{shell} is present but not runnable")
    project = tmp_path / "project"
    env_files.write_env_files(project)
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()

    root = _source_and_print_root(shell, project / ".agents" / "env.sh", elsewhere)

    assert root == str(project.resolve())
