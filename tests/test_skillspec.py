from __future__ import annotations

import json

import pytest

from csk import skillspec


def test_csk_skill_json_takes_precedence_over_runtime_json(tmp_path):
    (tmp_path / "agents").mkdir()
    (tmp_path / "agents" / "runtime.json").write_text(
        json.dumps({"commands": {"legacy": "scripts/legacy"}}),
        encoding="utf-8",
    )
    (tmp_path / "csk-skill.json").write_text(
        json.dumps({"schema_version": 1, "commands": {"new": {"type": "script", "unix_path": "scripts/new"}}}),
        encoding="utf-8",
    )
    spec = skillspec.load_skill_spec(tmp_path)
    assert list(spec.commands) == ["new"]


def test_runtime_json_fallback(tmp_path):
    (tmp_path / "agents").mkdir()
    (tmp_path / "agents" / "runtime.json").write_text(
        json.dumps({"commands": {"legacy": "scripts/legacy"}}),
        encoding="utf-8",
    )
    spec = skillspec.load_skill_spec(tmp_path)
    assert spec.commands["legacy"].unix_path == "scripts/legacy"
    assert spec.commands["legacy"].source == "agents/runtime.json"


def test_rejects_path_traversal(tmp_path):
    (tmp_path / "csk-skill.json").write_text(
        json.dumps({"schema_version": 1, "commands": {"bad": {"type": "script", "unix_path": "../bad"}}}),
        encoding="utf-8",
    )
    with pytest.raises(skillspec.SkillSpecError):
        skillspec.load_skill_spec(tmp_path)


def test_csk_skill_schema_mismatch_fails(tmp_path):
    (tmp_path / "csk-skill.json").write_text(
        json.dumps({"schema_version": 2, "commands": {}}),
        encoding="utf-8",
    )
    with pytest.raises(skillspec.SkillSpecError):
        skillspec.load_skill_spec(tmp_path)
