from __future__ import annotations

import json

import pytest

from csk import config


def test_config_round_trip(tmp_path):
    path = tmp_path / "config.json"
    cfg = config.parse_config(
        {
            "schema_version": 1,
            "skills_root": str(tmp_path / "skills"),
            "preferred_locale": "ru",
            "default_agents": ["codex_cli"],
            "adapter_mode": "copy",
            "worktree_alias_pattern": "[a-z]+_[0-9]+",
            "projects": {
                "app": {
                    "path": str(tmp_path / "app"),
                    "agents": ["cursor"],
                    "project_alias": "logical-app",
                    "checkout_alias": "app",
                }
            },
        },
        path,
    )
    config.save_config(cfg)
    loaded = config.load_config(path)
    assert loaded.skills_root == tmp_path / "skills"
    assert loaded.projects["app"].agents == ["cursor"]
    assert loaded.projects["app"].project_alias == "logical-app"
    assert loaded.worktree_alias_pattern == "[a-z]+_[0-9]+"


def test_config_schema_mismatch_fails(tmp_path):
    with pytest.raises(config.ConfigError):
        config.parse_config({"schema_version": 2, "skills_root": "x", "projects": {}}, tmp_path / "config.json")


def test_config_requires_projects_field(tmp_path):
    with pytest.raises(config.ConfigError):
        config.parse_config({"schema_version": 1, "skills_root": "x"}, tmp_path / "config.json")


def test_config_rejects_invalid_worktree_alias_pattern(tmp_path):
    with pytest.raises(config.ConfigError):
        config.parse_config(
            {
                "schema_version": 1,
                "skills_root": "x",
                "worktree_alias_pattern": "[",
                "projects": {},
            },
            tmp_path / "config.json",
        )


def test_config_path_uses_env(monkeypatch, tmp_path):
    monkeypatch.setenv("CSK_CONFIG", str(tmp_path / "custom.json"))
    assert config.config_path() == tmp_path / "custom.json"


def test_validate_skills_root_creates_missing_directory(tmp_path):
    cfg = config.GlobalConfig(
        path=tmp_path / "config.json",
        skills_root=tmp_path / "missing" / "skills",
        preferred_locale=None,
        default_agents=["codex_cli"],
        adapter_mode="auto",
        worktree_alias_pattern="[A-Z]+-[0-9]+",
        projects={},
    )

    config.validate_skills_root_for_work(cfg)

    assert cfg.skills_root.is_dir()
