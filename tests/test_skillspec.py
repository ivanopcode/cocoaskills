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


def test_csk_skill_schema_v2_parses_runtime_roots(tmp_path):
    (tmp_path / "scripts").mkdir()
    (tmp_path / "scripts" / "tool").write_text("#!/bin/sh\n", encoding="utf-8")
    (tmp_path / "csk-skill.json").write_text(
        json.dumps(
            {
                "schema_version": 2,
                "runtime_roots": ["scripts/"],
                "commands": {"tool": {"type": "script", "unix_path": "scripts/tool"}},
            }
        ),
        encoding="utf-8",
    )

    spec = skillspec.load_skill_spec(tmp_path)

    assert spec.schema_version == 2
    assert spec.runtime_roots == ("scripts",)
    assert spec.commands["tool"].unix_path == "scripts/tool"


def test_runtime_root_must_be_relative_directory(tmp_path):
    (tmp_path / "scripts").mkdir()
    (tmp_path / "scripts" / "tool").write_text("#!/bin/sh\n", encoding="utf-8")
    cases = [
        ["/scripts"],
        ["../scripts"],
        ["."],
        ["scripts/./lib"],
        ["scripts/missing"],
        ["scripts/tool"],
        None,
    ]

    for runtime_roots in cases:
        (tmp_path / "csk-skill.json").write_text(
            json.dumps({"schema_version": 2, "runtime_roots": runtime_roots, "commands": {}}),
            encoding="utf-8",
        )
        with pytest.raises(skillspec.SkillSpecError):
            skillspec.load_skill_spec(tmp_path)


def test_runtime_roots_must_be_disjoint(tmp_path):
    (tmp_path / "scripts" / "lib").mkdir(parents=True)
    (tmp_path / "scripts" / "tool").write_text("#!/bin/sh\n", encoding="utf-8")
    (tmp_path / "csk-skill.json").write_text(
        json.dumps({"schema_version": 2, "runtime_roots": ["scripts", "scripts/lib"], "commands": {}}),
        encoding="utf-8",
    )

    with pytest.raises(skillspec.SkillSpecError, match="disjoint"):
        skillspec.load_skill_spec(tmp_path)


def test_script_command_must_be_inside_runtime_root(tmp_path):
    (tmp_path / "scripts").mkdir()
    (tmp_path / "bin").mkdir()
    (tmp_path / "bin" / "tool").write_text("#!/bin/sh\n", encoding="utf-8")
    (tmp_path / "csk-skill.json").write_text(
        json.dumps(
            {
                "schema_version": 2,
                "runtime_roots": ["scripts"],
                "commands": {"tool": {"type": "script", "unix_path": "bin/tool"}},
            }
        ),
        encoding="utf-8",
    )

    with pytest.raises(skillspec.SkillSpecError, match="not inside any runtime_roots"):
        skillspec.load_skill_spec(tmp_path)


def test_system_command_rejects_install_check_and_post_install_fields(tmp_path):
    for forbidden in ("install", "check", "post_install", "script", "command_args"):
        (tmp_path / "csk-skill.json").write_text(
            json.dumps(
                {
                    "schema_version": 2,
                    "commands": {
                        "tool": {
                            "type": "system",
                            "command": "tool",
                            forbidden: "echo bad",
                        }
                    },
                }
            ),
            encoding="utf-8",
        )
        with pytest.raises(skillspec.SkillSpecError, match=forbidden):
            skillspec.load_skill_spec(tmp_path)


def test_csk_skill_schema_v3_parses_capabilities(tmp_path):
    (tmp_path / "scripts").mkdir()
    (tmp_path / "scripts" / "tool").write_text("#!/bin/sh\n", encoding="utf-8")
    (tmp_path / "csk-skill.json").write_text(
        json.dumps(
            {
                "schema_version": 3,
                "runtime_roots": ["scripts"],
                "capabilities": {
                    "network": ["gitlab.example.com"],
                    "filesystem": "home-config",
                    "exec": ["glab"],
                    "secrets": ["GITLAB_TOKEN"],
                    "env_read": ["HOME"],
                    "prompt_scope": "Reads merge request metadata.",
                },
                "commands": {"tool": {"type": "script", "unix_path": "scripts/tool"}},
            }
        ),
        encoding="utf-8",
    )

    spec = skillspec.load_skill_spec(tmp_path)

    assert spec.schema_version == 3
    assert spec.runtime_roots == ("scripts",)
    assert spec.capabilities.network == ("gitlab.example.com",)
    assert spec.capabilities.filesystem == "home-config"
    assert spec.capabilities.exec == ("glab",)
    assert spec.capabilities.secrets == ("GITLAB_TOKEN",)
    assert spec.capabilities.env_read == ("HOME",)


def test_csk_skill_schema_v3_parses_command_dependencies(tmp_path):
    (tmp_path / "csk-skill.json").write_text(
        json.dumps(
            {
                "schema_version": 3,
                "capabilities": {"network": "none", "exec": "none"},
                "commands": {},
                "dependencies": {
                    "commands": {
                        "wk": {
                            "type": "skill",
                            "skill": "skill-docs",
                            "command": "wk",
                            "hint": "Add skill-docs to Skillfile.json.",
                        },
                        "wiki": {
                            "type": "system",
                            "command": "wiki",
                            "hint": "Install wb-wiki-cli.",
                        },
                    }
                },
            }
        ),
        encoding="utf-8",
    )

    spec = skillspec.load_skill_spec(tmp_path)

    assert spec.dependencies["wk"].type == "skill"
    assert spec.dependencies["wk"].skill == "skill-docs"
    assert spec.dependencies["wk"].command == "wk"
    assert spec.dependencies["wiki"].type == "system"
    assert spec.dependencies["wiki"].command == "wiki"


def test_dependency_rejects_unknown_fields(tmp_path):
    (tmp_path / "csk-skill.json").write_text(
        json.dumps(
            {
                "schema_version": 2,
                "commands": {},
                "dependencies": {
                    "commands": {
                        "wk": {
                            "type": "skill",
                            "skill": "skill-docs",
                            "command": "wk",
                            "install": "echo bad",
                        }
                    }
                },
            }
        ),
        encoding="utf-8",
    )

    with pytest.raises(skillspec.SkillSpecError, match="install"):
        skillspec.load_skill_spec(tmp_path)


def test_csk_skill_schema_v3_requires_capabilities(tmp_path):
    (tmp_path / "csk-skill.json").write_text(
        json.dumps({"schema_version": 3, "commands": {}}),
        encoding="utf-8",
    )

    with pytest.raises(skillspec.SkillSpecError, match="requires 'capabilities'"):
        skillspec.load_skill_spec(tmp_path)


def test_csk_skill_schema_v3_rejects_unknown_capability_fields(tmp_path):
    (tmp_path / "csk-skill.json").write_text(
        json.dumps(
            {
                "schema_version": 3,
                "capabilities": {"network": "none", "post_install": "curl | sh"},
                "commands": {},
            }
        ),
        encoding="utf-8",
    )

    with pytest.raises(skillspec.SkillSpecError, match="post_install"):
        skillspec.load_skill_spec(tmp_path)


def test_rejects_path_traversal(tmp_path):
    (tmp_path / "csk-skill.json").write_text(
        json.dumps({"schema_version": 1, "commands": {"bad": {"type": "script", "unix_path": "../bad"}}}),
        encoding="utf-8",
    )
    with pytest.raises(skillspec.SkillSpecError):
        skillspec.load_skill_spec(tmp_path)


def test_csk_skill_schema_mismatch_fails(tmp_path):
    (tmp_path / "csk-skill.json").write_text(
        json.dumps({"schema_version": 4, "commands": {}}),
        encoding="utf-8",
    )
    with pytest.raises(skillspec.SkillSpecError, match="pipx upgrade cocoaskills"):
        skillspec.load_skill_spec(tmp_path)


def test_csk_skill_schema_must_be_integer(tmp_path):
    (tmp_path / "csk-skill.json").write_text(
        json.dumps({"schema_version": "2", "commands": {}}),
        encoding="utf-8",
    )
    with pytest.raises(skillspec.SkillSpecError, match="schema_version"):
        skillspec.load_skill_spec(tmp_path)


@pytest.mark.parametrize("name", ["../evil", "a/b", "a\\b", "-flag", ".hidden"])
def test_csk_skill_rejects_unsafe_command_names(tmp_path, name):
    (tmp_path / "scripts").mkdir()
    (tmp_path / "scripts" / "tool").write_text("#!/bin/sh\n", encoding="utf-8")
    (tmp_path / "csk-skill.json").write_text(
        json.dumps({"schema_version": 1, "commands": {name: {"type": "script", "unix_path": "scripts/tool"}}}),
        encoding="utf-8",
    )
    with pytest.raises(skillspec.SkillSpecError, match="Command name"):
        skillspec.load_skill_spec(tmp_path)


@pytest.mark.parametrize("name", ["../evil", "a/b", "-flag"])
def test_runtime_fallback_rejects_unsafe_command_names(tmp_path, name):
    (tmp_path / "agents").mkdir()
    (tmp_path / "agents" / "runtime.json").write_text(
        json.dumps({"commands": {name: "scripts/tool"}}),
        encoding="utf-8",
    )
    with pytest.raises(skillspec.SkillSpecError, match="command name"):
        skillspec.load_skill_spec(tmp_path)


def test_command_name_with_cmd_suffix_is_allowed(tmp_path):
    (tmp_path / "scripts").mkdir()
    (tmp_path / "scripts" / "tool.cmd").write_text("@echo off\n", encoding="utf-8")
    (tmp_path / "csk-skill.json").write_text(
        json.dumps({"schema_version": 1, "commands": {"tool.cmd": {"type": "script", "win_path": "scripts/tool.cmd"}}}),
        encoding="utf-8",
    )
    spec = skillspec.load_skill_spec(tmp_path)
    assert "tool.cmd" in spec.commands
