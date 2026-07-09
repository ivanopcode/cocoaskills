from __future__ import annotations

import json
from pathlib import Path

import pytest

from conftest import make_config, make_project, make_skill_repo, write_skillfile
from csk import installer, skillspec


CAPS = {"exec": "none", "network": "none"}


def _manifest(entry: dict) -> dict:
    return {
        "schema_version": 5,
        "capabilities": CAPS,
        "dependencies": {"mcp_servers": {"sheets": entry}},
    }


def _write_manifest(tmp_path: Path, payload: dict) -> Path:
    (tmp_path / "csk-skill.json").write_text(json.dumps(payload), encoding="utf-8")
    return tmp_path


def test_schema_v5_parses_mcp_requirement(tmp_path):
    _write_manifest(
        tmp_path,
        _manifest({"hint": "Connect the Sheets MCP server.", "transport": "http", "required_in": "all"}),
    )
    spec = skillspec.load_skill_spec(tmp_path)
    requirement = spec.mcp_servers["sheets"]
    assert requirement.hint == "Connect the Sheets MCP server."
    assert requirement.transport == "http"
    assert requirement.required_in == "all"


def test_required_in_defaults_to_any_and_transport_is_optional(tmp_path):
    _write_manifest(tmp_path, _manifest({"hint": "Connect it."}))
    spec = skillspec.load_skill_spec(tmp_path)
    assert spec.mcp_servers["sheets"].required_in == "any"
    assert spec.mcp_servers["sheets"].transport is None


def test_mcp_requirement_requires_hint(tmp_path):
    _write_manifest(tmp_path, _manifest({"transport": "stdio"}))
    with pytest.raises(skillspec.SkillSpecError, match="'hint'"):
        skillspec.load_skill_spec(tmp_path)


@pytest.mark.parametrize(
    ("entry", "match"),
    [
        ({"hint": "x", "transport": "websocket"}, "transport"),
        ({"hint": "x", "required_in": "some"}, "required_in"),
        ({"hint": "x", "url": "https://example.com"}, "unsupported field"),
    ],
)
def test_mcp_requirement_rejects_invalid_fields(tmp_path, entry, match):
    _write_manifest(tmp_path, _manifest(entry))
    with pytest.raises(skillspec.SkillSpecError, match=match):
        skillspec.load_skill_spec(tmp_path)


def test_mcp_servers_require_schema_v5(tmp_path):
    _write_manifest(
        tmp_path,
        {
            "schema_version": 4,
            "capabilities": CAPS,
            "dependencies": {"mcp_servers": {"sheets": {"hint": "x"}}},
        },
    )
    with pytest.raises(skillspec.SkillSpecError, match="schema_version 5"):
        skillspec.load_skill_spec(tmp_path)


def _mcp_skill_files(required_in: str = "any") -> dict:
    return {
        "csk-skill.json": json.dumps(
            {
                "schema_version": 5,
                "capabilities": CAPS,
                "dependencies": {
                    "mcp_servers": {
                        "sheets": {
                            "hint": "Add the sheets MCP server to your agent configuration.",
                            "required_in": required_in,
                        }
                    }
                },
            }
        )
    }


def _install(tmp_path, skills_root, csk_home, *, agents, required_in="any"):
    project = make_project(tmp_path)
    make_skill_repo(skills_root, "skill-sheets", _mcp_skill_files(required_in), tag="v1")
    write_skillfile(
        project,
        {"schema_version": 1, "agents": agents, "skills": [{"name": "skill-sheets", "tag": "v1"}]},
    )
    cfg = make_config(csk_home, skills_root, project, agents=agents)
    return project, installer.install(cfg)[0]


def test_any_satisfied_through_project_mcp_json(tmp_path, skills_root, csk_home):
    project_dir = tmp_path / "project"
    project_dir.mkdir()
    (project_dir / ".mcp.json").write_text(
        json.dumps({"mcpServers": {"sheets": {"command": "sheets-mcp"}}}), encoding="utf-8"
    )
    project, result = _install(tmp_path, skills_root, csk_home, agents=["claude_code"])
    assert not result.errors, result.errors
    marker = json.loads(
        (project / ".agents" / "skills" / "skill-sheets" / ".csk-install.json").read_text(encoding="utf-8")
    )
    assert marker["mcp_servers"] == {"sheets": ["claude_code"]}


def test_any_fails_with_hint_when_unconfigured(tmp_path, skills_root, csk_home):
    _, result = _install(tmp_path, skills_root, csk_home, agents=["claude_code"])
    assert result.errors
    assert "not configured in any target agent environment" in result.errors[0]
    assert "Add the sheets MCP server" in result.errors[0]


def test_any_satisfied_through_user_level_configs(tmp_path, skills_root, csk_home, monkeypatch):
    home = tmp_path / "home"
    (home / ".codex").mkdir(parents=True)
    (home / ".codex" / "config.toml").write_text(
        '[mcp_servers.sheets]\ncommand = "sheets-mcp"\n', encoding="utf-8"
    )
    _, result = _install(tmp_path, skills_root, csk_home, agents=["codex_cli"])
    assert not result.errors, result.errors


def test_all_requires_every_target_agent(tmp_path, skills_root, csk_home):
    project_dir = tmp_path / "project"
    project_dir.mkdir()
    (project_dir / ".mcp.json").write_text(
        json.dumps({"mcpServers": {"sheets": {}}}), encoding="utf-8"
    )
    _, result = _install(
        tmp_path, skills_root, csk_home, agents=["claude_code", "cursor"], required_in="all"
    )
    assert result.errors
    assert "not configured for agent(s): cursor" in result.errors[0]


def test_all_satisfied_across_agents(tmp_path, skills_root, csk_home):
    project_dir = tmp_path / "project"
    project_dir.mkdir()
    (project_dir / ".mcp.json").write_text(
        json.dumps({"mcpServers": {"sheets": {}}}), encoding="utf-8"
    )
    (project_dir / ".cursor").mkdir()
    (project_dir / ".cursor" / "mcp.json").write_text(
        json.dumps({"mcpServers": {"sheets": {}}}), encoding="utf-8"
    )
    project, result = _install(
        tmp_path, skills_root, csk_home, agents=["claude_code", "cursor"], required_in="all"
    )
    assert not result.errors, result.errors
    marker = json.loads(
        (project / ".agents" / "skills" / "skill-sheets" / ".csk-install.json").read_text(encoding="utf-8")
    )
    assert marker["mcp_servers"] == {"sheets": ["claude_code", "cursor"]}


def test_marker_refreshes_when_configuration_changes(tmp_path, skills_root, csk_home):
    project_dir = tmp_path / "project"
    project_dir.mkdir()
    (project_dir / ".mcp.json").write_text(
        json.dumps({"mcpServers": {"sheets": {}}}), encoding="utf-8"
    )
    project, result = _install(tmp_path, skills_root, csk_home, agents=["claude_code", "cursor"])
    assert not result.errors, result.errors

    (project / ".cursor").mkdir(exist_ok=True)
    (project / ".cursor" / "mcp.json").write_text(
        json.dumps({"mcpServers": {"sheets": {}}}), encoding="utf-8"
    )
    cfg = make_config(csk_home, skills_root, project, agents=["claude_code", "cursor"])
    result = installer.install(cfg)[0]
    assert not result.errors, result.errors
    marker = json.loads(
        (project / ".agents" / "skills" / "skill-sheets" / ".csk-install.json").read_text(encoding="utf-8")
    )
    assert marker["mcp_servers"] == {"sheets": ["claude_code", "cursor"]}


def test_any_satisfied_through_project_opencode_json(tmp_path, skills_root, csk_home):
    project_dir = tmp_path / "project"
    project_dir.mkdir()
    (project_dir / "opencode.json").write_text(
        json.dumps({"mcp": {"sheets": {"type": "local", "command": ["sheets-mcp"]}}}),
        encoding="utf-8",
    )
    project, result = _install(tmp_path, skills_root, csk_home, agents=["opencode"])
    assert not result.errors, result.errors
    marker = json.loads(
        (project / ".agents" / "skills" / "skill-sheets" / ".csk-install.json").read_text(encoding="utf-8")
    )
    assert marker["mcp_servers"] == {"sheets": ["opencode"]}


def test_opencode_disabled_server_counts_as_unconfigured(tmp_path, skills_root, csk_home):
    project_dir = tmp_path / "project"
    project_dir.mkdir()
    (project_dir / "opencode.json").write_text(
        json.dumps({"mcp": {"sheets": {"type": "local", "enabled": False}}}), encoding="utf-8"
    )
    _, result = _install(tmp_path, skills_root, csk_home, agents=["opencode"])
    assert result.errors
    assert "not configured" in result.errors[0]


def test_any_satisfied_through_windsurf_home_config(tmp_path, skills_root, csk_home):
    home = tmp_path / "home"
    (home / ".codeium" / "windsurf").mkdir(parents=True)
    (home / ".codeium" / "windsurf" / "mcp_config.json").write_text(
        json.dumps({"mcpServers": {"sheets": {"command": "sheets-mcp"}}}), encoding="utf-8"
    )
    _, result = _install(tmp_path, skills_root, csk_home, agents=["windsurf"])
    assert not result.errors, result.errors


def test_any_satisfied_through_project_codex_config(tmp_path, skills_root, csk_home):
    project_dir = tmp_path / "project"
    (project_dir / ".codex").mkdir(parents=True)
    (project_dir / ".codex" / "config.toml").write_text(
        '[mcp_servers.sheets]\ncommand = "sheets-mcp"\n', encoding="utf-8"
    )
    _, result = _install(tmp_path, skills_root, csk_home, agents=["codex_cli"])
    assert not result.errors, result.errors


def test_any_satisfied_through_project_gemini_settings(tmp_path, skills_root, csk_home):
    project_dir = tmp_path / "project"
    (project_dir / ".gemini").mkdir(parents=True)
    (project_dir / ".gemini" / "settings.json").write_text(
        json.dumps({"mcpServers": {"sheets": {"httpUrl": "https://example.com/mcp"}}}),
        encoding="utf-8",
    )
    _, result = _install(tmp_path, skills_root, csk_home, agents=["gemini"])
    assert not result.errors, result.errors


def test_claude_disabled_mcpjson_server_counts_as_unconfigured(tmp_path, skills_root, csk_home):
    project_dir = tmp_path / "project"
    project_dir.mkdir()
    (project_dir / ".mcp.json").write_text(
        json.dumps({"mcpServers": {"sheets": {"command": "sheets-mcp"}}}), encoding="utf-8"
    )
    (project_dir / ".claude").mkdir()
    (project_dir / ".claude" / "settings.json").write_text(
        json.dumps({"disabledMcpjsonServers": ["sheets"]}), encoding="utf-8"
    )
    _, result = _install(tmp_path, skills_root, csk_home, agents=["claude_code"])
    assert result.errors
    assert "not configured" in result.errors[0]


def test_stdio_command_missing_from_path_warns(tmp_path, skills_root, csk_home):
    project_dir = tmp_path / "project"
    project_dir.mkdir()
    (project_dir / ".mcp.json").write_text(
        json.dumps({"mcpServers": {"sheets": {"command": "definitely-not-a-real-binary-9x7"}}}),
        encoding="utf-8",
    )
    _, result = _install(tmp_path, skills_root, csk_home, agents=["claude_code"])
    assert not result.errors, result.errors
    assert any("not on PATH" in message for message in result.messages), result.messages


def test_resolvable_stdio_command_does_not_warn(tmp_path, skills_root, csk_home):
    project_dir = tmp_path / "project"
    project_dir.mkdir()
    (project_dir / ".mcp.json").write_text(
        json.dumps({"mcpServers": {"sheets": {"command": "git"}}}), encoding="utf-8"
    )
    _, result = _install(tmp_path, skills_root, csk_home, agents=["claude_code"])
    assert not result.errors, result.errors
    assert not any("not on PATH" in message for message in result.messages), result.messages


def test_project_only_declaration_emits_trust_hint(tmp_path, skills_root, csk_home):
    project_dir = tmp_path / "project"
    project_dir.mkdir()
    (project_dir / ".mcp.json").write_text(
        json.dumps({"mcpServers": {"sheets": {"command": "git"}}}), encoding="utf-8"
    )
    _, result = _install(tmp_path, skills_root, csk_home, agents=["claude_code"])
    assert not result.errors, result.errors
    assert any("pending" in message for message in result.messages), result.messages


def test_user_level_declaration_emits_no_trust_hint(tmp_path, skills_root, csk_home):
    home = tmp_path / "home"
    home.mkdir(parents=True, exist_ok=True)
    (home / ".claude.json").write_text(
        json.dumps({"mcpServers": {"sheets": {"command": "git"}}}), encoding="utf-8"
    )
    _, result = _install(tmp_path, skills_root, csk_home, agents=["claude_code"])
    assert not result.errors, result.errors
    assert not any("pending" in message for message in result.messages), result.messages


def test_malformed_agent_config_counts_as_no_servers(tmp_path, skills_root, csk_home):
    project_dir = tmp_path / "project"
    project_dir.mkdir()
    (project_dir / ".mcp.json").write_text("{not json", encoding="utf-8")
    _, result = _install(tmp_path, skills_root, csk_home, agents=["claude_code"])
    assert result.errors
    assert "not configured" in result.errors[0]
