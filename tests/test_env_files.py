from __future__ import annotations

import shutil
import subprocess

import pytest

from csk import env_files


def test_env_files_generated(tmp_path):
    project = tmp_path / "project"
    env_files.write_env_files(project)
    assert ".agents/bin" in (project / ".agents" / "env.sh").read_text(encoding="utf-8")
    assert ".agents\\bin" in (project / ".agents" / "env.ps1").read_text(encoding="utf-8")


def _source_and_print_root(shell: str, env_sh, cwd) -> str:
    proc = subprocess.run(
        [shell, "-c", f'. "{env_sh}" && printf %s "$CSK_PROJECT_ROOT"'],
        cwd=cwd,
        text=True,
        capture_output=True,
    )
    assert proc.returncode == 0, proc.stderr
    return proc.stdout


def _shell_can_run(shell: str) -> bool:
    proc = subprocess.run([shell, "-c", "printf ok"], text=True, capture_output=True)
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
