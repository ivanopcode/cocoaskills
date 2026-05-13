from __future__ import annotations

from csk import env_files


def test_env_files_generated(tmp_path):
    project = tmp_path / "project"
    env_files.write_env_files(project)
    assert ".agents/bin" in (project / ".agents" / "env.sh").read_text(encoding="utf-8")
    assert ".agents\\bin" in (project / ".agents" / "env.ps1").read_text(encoding="utf-8")

