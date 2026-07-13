from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from csk import env_files, shell_init


def test_powershell_hook_installs_idempotent_prompt_wrapper() -> None:
    hook = shell_init.shell_init("powershell", include_global=False)
    assert "function global:prompt" in hook
    assert "CskPromptWrapped" in hook
    assert "CskOriginalPrompt" in hook
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
