from __future__ import annotations

import pytest

from conftest import make_project
from csk import gitignore_gate


def test_gitignore_gate_blocks_missing_entries(tmp_path):
    project = make_project(tmp_path, gitignore=False)
    with pytest.raises(gitignore_gate.GitignoreError):
        gitignore_gate.ensure_ignored(project, [".agents/"])


def test_fix_gitignore_appends_missing_entries(tmp_path):
    project = make_project(tmp_path, gitignore=False)
    gitignore_gate.ensure_ignored(project, [".agents/", ".claude/skills/"], fix=True)
    text = (project / ".gitignore").read_text(encoding="utf-8")
    assert ".agents/" in text
    assert ".claude/skills/" in text

