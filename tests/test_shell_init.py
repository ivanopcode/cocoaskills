from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from csk import env_files, shell_init


def _posix_shell_command(executable: str, script: str) -> list[str]:
    if Path(executable).name == "zsh":
        return [executable, "-dfc", script]
    return [executable, "--noprofile", "--norc", "-c", script]


@pytest.mark.parametrize("shell", ["bash", "zsh"])
def test_posix_hook_activates_and_restores_project_env(tmp_path: Path, shell: str) -> None:
    executable = shutil.which(shell)
    if executable is None:
        pytest.skip(f"{shell} is unavailable")
    project = tmp_path / "project"
    nested = project / "nested"
    outside = tmp_path / "outside"
    nested.mkdir(parents=True)
    outside.mkdir()
    env_files.write_env_files(project)

    hook_path = tmp_path / "hook.sh"
    hook_path.write_text(shell_init.shell_init(shell, include_global=False), encoding="utf-8")
    script = r'''
original_path="$PATH"
cd "$NESTED"
. "$HOOK_PATH"
printf 'active=%s\n' "$CSK_PROJECT_ROOT"
case ":$PATH:" in
  *":$CSK_PROJECT_ROOT/.agents/bin:"*) printf 'project-bin=present\n' ;;
  *) printf 'project-bin=missing\n' ;;
esac
cd "$OUTSIDE"
_csk_auto_env
if [ "$PATH" = "$original_path" ]; then
  printf 'path=restored\n'
else
  printf 'path=changed\n'
fi
printf 'left=%s\n' "${CSK_ACTIVE_ENV-unset}"
'''
    completed = subprocess.run(
        _posix_shell_command(executable, script),
        check=False,
        capture_output=True,
        text=True,
        timeout=5,
        env={
            **os.environ,
            "HOOK_PATH": str(hook_path),
            "NESTED": str(nested),
            "OUTSIDE": str(outside),
            "SHELL": executable,
        },
    )

    assert completed.returncode == 0, completed.stderr
    assert f"active={project}" in completed.stdout
    assert "project-bin=present" in completed.stdout
    assert "path=restored" in completed.stdout
    assert "left=unset" in completed.stdout


@pytest.mark.parametrize("shell", ["bash", "zsh"])
@pytest.mark.parametrize("broken_pwd", ["", ".", "relative/path"])
def test_posix_hook_rejects_non_absolute_pwd_without_hanging(tmp_path: Path, shell: str, broken_pwd: str) -> None:
    executable = shutil.which(shell)
    if executable is None:
        pytest.skip(f"{shell} is unavailable")
    hook_path = tmp_path / "hook.sh"
    hook_path.write_text(shell_init.shell_init(shell, include_global=False), encoding="utf-8")
    script = r'''
PWD="$BROKEN_PWD"
export PWD
. "$HOOK_PATH"
printf 'completed\n'
'''
    completed = subprocess.run(
        _posix_shell_command(executable, script),
        check=False,
        capture_output=True,
        text=True,
        timeout=2,
        env={
            **os.environ,
            "BROKEN_PWD": broken_pwd,
            "HOOK_PATH": str(hook_path),
            "SHELL": executable,
        },
    )

    assert completed.returncode == 0, completed.stderr
    assert completed.stdout == "completed\n"


@pytest.mark.parametrize("shell", ["bash", "zsh"])
def test_posix_hook_sources_global_env_without_external_dirname(tmp_path: Path, shell: str) -> None:
    executable = shutil.which(shell)
    if executable is None:
        pytest.skip(f"{shell} is unavailable")
    csk_home = tmp_path / "csk-home"
    env_files.write_global_env_files(csk_home)
    hook_path = tmp_path / "hook.sh"
    hook = shell_init.shell_init(shell)
    hook_path.write_text(hook, encoding="utf-8")
    completed = subprocess.run(
        _posix_shell_command(executable, '. "$HOOK_PATH"; printf "global=%s\\n" "$CSK_GLOBAL_ROOT"'),
        check=False,
        capture_output=True,
        text=True,
        timeout=5,
        env={
            **os.environ,
            "CSK_CONFIG": str(csk_home / "config.json"),
            "HOOK_PATH": str(hook_path),
            "SHELL": executable,
        },
    )

    assert completed.returncode == 0, completed.stderr
    assert completed.stdout == f"global={csk_home / 'global'}\n"
    assert "dirname" not in hook


@pytest.mark.parametrize("shell", ["bash", "zsh"])
def test_posix_hook_can_disable_project_filesystem_scan(tmp_path: Path, shell: str) -> None:
    executable = shutil.which(shell)
    if executable is None:
        pytest.skip(f"{shell} is unavailable")
    project = tmp_path / "project"
    project.mkdir()
    env_files.write_env_files(project)
    csk_home = tmp_path / "csk-home"
    env_files.write_global_env_files(csk_home)
    hook_path = tmp_path / "hook.sh"
    hook_path.write_text(shell_init.shell_init(shell), encoding="utf-8")
    completed = subprocess.run(
        _posix_shell_command(
            executable,
            'cd "$PROJECT"; . "$HOOK_PATH"; '
            'printf "project=%s global=%s\\n" "${CSK_ACTIVE_ENV-unset}" "$CSK_GLOBAL_ROOT"',
        ),
        check=False,
        capture_output=True,
        text=True,
        timeout=5,
        env={
            **os.environ,
            "CSK_AUTO_ENV": "0",
            "CSK_CONFIG": str(csk_home / "config.json"),
            "HOOK_PATH": str(hook_path),
            "PROJECT": str(project),
            "SHELL": executable,
        },
    )

    assert completed.returncode == 0, completed.stderr
    assert completed.stdout == f"project=unset global={csk_home / 'global'}\n"


def test_powershell_hook_installs_idempotent_prompt_wrapper() -> None:
    hook = shell_init.shell_init("powershell", include_global=False)
    assert "function global:prompt" in hook
    assert "CskPromptWrapped" in hook
    assert "CskOriginalPrompt" in hook
    assert 'CSK_AUTO_ENV -eq "0"' in hook
    assert hook.count("Invoke-CskAutoEnv") >= 3


@pytest.mark.skipif(sys.platform != "win32", reason="PowerShell prompt integration runs on Windows")
def test_powershell_hook_activates_and_restores_on_every_prompt(tmp_path: Path) -> None:
    executable = shutil.which("pwsh") or shutil.which("powershell")
    if executable is None:
        pytest.skip("PowerShell is unavailable")
    project = tmp_path / "project"
    nested = project / "nested"
    outside = tmp_path / "outside"
    nested.mkdir(parents=True)
    outside.mkdir()
    env_files.write_env_files(project)

    hook_path = tmp_path / "hook.ps1"
    hook_path.write_text(shell_init.shell_init("powershell", include_global=False), encoding="utf-8")
    script_path = tmp_path / "verify.ps1"
    script_path.write_text(
        r'''
$originalPath = $env:PATH
function global:prompt { return "ORIGINAL>" }
. $env:HOOK_PATH
Set-Location $env:NESTED
$firstPrompt = prompt
Write-Output "first=$($env:CSK_PROJECT_ROOT):$firstPrompt"
Set-Location $env:OUTSIDE
$secondPrompt = prompt
$restored = if ($env:PATH -eq $originalPath) { "restored" } else { "changed" }
$active = if ($env:CSK_ACTIVE_ENV) { $env:CSK_ACTIVE_ENV } else { "unset" }
Write-Output ("left={0}:{1}:{2}" -f $active, $restored, $secondPrompt)
''',
        encoding="utf-8",
    )
    environment = {
        **os.environ,
        "HOOK_PATH": str(hook_path),
        "NESTED": str(nested),
        "OUTSIDE": str(outside),
    }
    completed = subprocess.run(
        [executable, "-NoProfile", "-File", str(script_path)],
        check=False,
        capture_output=True,
        text=True,
        env=environment,
    )
    assert completed.returncode == 0, completed.stderr
    assert f"first={project}:ORIGINAL>" in completed.stdout
    assert "left=unset:restored:ORIGINAL>" in completed.stdout
