from __future__ import annotations

import json
from pathlib import Path

import pytest

from csk import skillspec


CAPS = {"exec": "none", "network": "none"}


def _write_manifest(tmp_path: Path, payload: dict) -> Path:
    (tmp_path / "csk-skill.json").write_text(json.dumps(payload), encoding="utf-8")
    return tmp_path


def _requirement_manifest(entry: dict) -> dict:
    return {
        "schema_version": 4,
        "capabilities": CAPS,
        "dependencies": {"skills": {"skill-tracker": entry}},
    }


def test_schema_v4_parses_requirement(tmp_path):
    _write_manifest(
        tmp_path,
        _requirement_manifest(
            {
                "git": "git@gitlab.example.com:skills/skill-tracker.git",
                "ref": {"kind": "tag", "value": "v1.4.2"},
                "mode": "runtime",
                "commands": ["trk"],
            }
        ),
    )
    spec = skillspec.load_skill_spec(tmp_path)
    requirement = spec.requirements["skill-tracker"]
    assert requirement.git == "git@gitlab.example.com:skills/skill-tracker.git"
    assert (requirement.ref_kind, requirement.ref_value) == ("tag", "v1.4.2")
    assert requirement.mode == "runtime"
    assert requirement.commands == ("trk",)


def test_mode_defaults_to_full(tmp_path):
    _write_manifest(
        tmp_path,
        _requirement_manifest(
            {"git": "git@gitlab.example.com:skills/skill-tracker.git", "ref": {"kind": "revision", "value": "abc123"}}
        ),
    )
    spec = skillspec.load_skill_spec(tmp_path)
    assert spec.requirements["skill-tracker"].mode == "full"
    assert spec.requirements["skill-tracker"].commands == ()


def test_requirements_reject_version_ranges(tmp_path):
    _write_manifest(
        tmp_path,
        _requirement_manifest(
            {"git": "git@gitlab.example.com:skills/skill-tracker.git", "version": "^1.0.0"}
        ),
    )
    with pytest.raises(skillspec.SkillSpecError, match="version ranges are not supported"):
        skillspec.load_skill_spec(tmp_path)


def test_requirements_reject_range_looking_ref_values(tmp_path):
    _write_manifest(
        tmp_path,
        _requirement_manifest(
            {"git": "git@gitlab.example.com:skills/skill-tracker.git", "ref": {"kind": "tag", "value": "^1.0.0"}}
        ),
    )
    with pytest.raises(skillspec.SkillSpecError, match="version range"):
        skillspec.load_skill_spec(tmp_path)


def test_requirements_reject_branch_refs(tmp_path):
    _write_manifest(
        tmp_path,
        _requirement_manifest(
            {"git": "git@gitlab.example.com:skills/skill-tracker.git", "ref": {"kind": "branch", "value": "main"}}
        ),
    )
    with pytest.raises(skillspec.SkillSpecError, match="exact 'tag' or 'revision'"):
        skillspec.load_skill_spec(tmp_path)


def test_requirements_reject_commands_outside_runtime_mode(tmp_path):
    _write_manifest(
        tmp_path,
        _requirement_manifest(
            {
                "git": "git@gitlab.example.com:skills/skill-tracker.git",
                "ref": {"kind": "tag", "value": "v1"},
                "mode": "full",
                "commands": ["trk"],
            }
        ),
    )
    with pytest.raises(skillspec.SkillSpecError, match="runtime requirements only"):
        skillspec.load_skill_spec(tmp_path)


def test_requirements_require_git_source(tmp_path):
    _write_manifest(tmp_path, _requirement_manifest({"ref": {"kind": "tag", "value": "v1"}}))
    with pytest.raises(skillspec.SkillSpecError, match="'git'"):
        skillspec.load_skill_spec(tmp_path)


def test_dependencies_skills_requires_schema_v4(tmp_path):
    _write_manifest(
        tmp_path,
        {
            "schema_version": 3,
            "capabilities": CAPS,
            "dependencies": {
                "skills": {
                    "skill-tracker": {
                        "git": "git@gitlab.example.com:skills/skill-tracker.git",
                        "ref": {"kind": "tag", "value": "v1"},
                    }
                }
            },
        },
    )
    with pytest.raises(skillspec.SkillSpecError, match="schema_version 4"):
        skillspec.load_skill_spec(tmp_path)


def test_schema_v4_requires_capabilities(tmp_path):
    _write_manifest(tmp_path, {"schema_version": 4, "commands": {}})
    with pytest.raises(skillspec.SkillSpecError, match="requires 'capabilities'"):
        skillspec.load_skill_spec(tmp_path)


def test_schema_v4_keeps_system_dependencies(tmp_path):
    _write_manifest(
        tmp_path,
        {
            "schema_version": 4,
            "capabilities": CAPS,
            "dependencies": {
                "commands": {"git": {"type": "system", "command": "git", "hint": "Install git and retry."}}
            },
        },
    )
    spec = skillspec.load_skill_spec(tmp_path)
    assert spec.dependencies["git"].type == "system"
    assert spec.requirements == {}
