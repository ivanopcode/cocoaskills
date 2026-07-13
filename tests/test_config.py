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
    assert not loaded.audit.enabled
    assert loaded.audit.mode == "advisory"


def test_config_parses_and_round_trips_audit_settings(tmp_path):
    path = tmp_path / "config.json"
    cfg = config.parse_config(
        {
            "schema_version": 1,
            "skills_root": str(tmp_path / "skills"),
            "projects": {},
            "audit": {
                "enabled": True,
                "mode": "strict",
                "fail_on": "medium",
                "backend": "codex",
                "model": "gpt-5",
                "allow_cloud": True,
                "max_request_bytes": 2048,
                "snapshot_max_age_seconds": 86400,
                "snapshot_clock_skew_seconds": 0,
                "cache_ttl_seconds": 0,
                "offline_grace_seconds": 0,
                "backends": {"codex": {"kind": "codex", "timeout_seconds": 30, "cloud": True}},
                "grants": [{"skill": "skill-gitlab", "content_sha256": "abc"}],
                "revocations": [
                    "sha256:" + "d" * 64,
                    "source:gitlab.example.com",
                ],
                "source_policy": {
                    "default_class": "internal",
                    "rules": [{"pattern": "github.com", "class": "public"}],
                },
            },
        },
        path,
    )

    assert cfg.audit.enabled
    assert cfg.audit.mode == "strict"
    assert cfg.audit.fail_on == "medium"
    assert cfg.audit.backend == "codex"
    assert cfg.audit.model == "gpt-5"
    assert cfg.audit.allow_cloud
    assert cfg.audit.max_request_bytes == 2048
    assert cfg.audit.snapshot_max_age_seconds == 86400
    assert cfg.audit.snapshot_clock_skew_seconds == 0
    assert cfg.audit.cache_ttl_seconds == 0
    assert cfg.audit.offline_grace_seconds == 0
    assert cfg.audit.source_policy.classify(None, "git@github.com:ivanopcode/cocoaskills.git") == "public"

    config.save_config(cfg)
    loaded = config.load_config(path)

    assert loaded.audit.enabled
    assert loaded.audit.mode == "strict"
    assert loaded.audit.fail_on == "medium"
    assert loaded.audit.backend == "codex"
    assert loaded.audit.model == "gpt-5"
    assert loaded.audit.allow_cloud
    assert loaded.audit.max_request_bytes == 2048
    assert loaded.audit.snapshot_max_age_seconds == 86400
    assert loaded.audit.snapshot_clock_skew_seconds == 0
    assert loaded.audit.cache_ttl_seconds == 0
    assert loaded.audit.offline_grace_seconds == 0
    assert loaded.audit.backends == {"codex": {"kind": "codex", "timeout_seconds": 30, "cloud": True}}
    assert loaded.audit.grants == [{"skill": "skill-gitlab", "content_sha256": "abc"}]
    assert loaded.audit.revocations == ["sha256:" + "d" * 64, "source:gitlab.example.com"]
    assert loaded.audit.source_policy.classify(None, "git@github.com:ivanopcode/cocoaskills.git") == "public"


def test_config_schema_mismatch_fails(tmp_path):
    with pytest.raises(config.ConfigError):
        config.parse_config({"schema_version": 2, "skills_root": "x", "projects": {}}, tmp_path / "config.json")


def test_config_requires_projects_field(tmp_path):
    with pytest.raises(config.ConfigError):
        config.parse_config({"schema_version": 1, "skills_root": "x"}, tmp_path / "config.json")


def test_config_rejects_unknown_top_and_project_fields(tmp_path):
    with pytest.raises(config.ConfigError, match="unsupported field"):
        config.parse_config(
            {"schema_version": 1, "skills_root": "x", "projects": {}, "typo": True},
            tmp_path / "config.json",
        )
    with pytest.raises(config.ConfigError, match="unsupported field"):
        config.parse_config(
            {
                "schema_version": 1,
                "skills_root": "x",
                "projects": {"app": {"path": "x", "typo": True}},
            },
            tmp_path / "config.json",
        )


@pytest.mark.parametrize("field", ["project_alias", "checkout_alias"])
def test_config_project_matching_aliases_are_portable_or_null(tmp_path, field):
    parsed = config.parse_config(
        {
            "schema_version": 1,
            "skills_root": "x",
            "projects": {"app": {"path": "x", field: None}},
        },
        tmp_path / "config.json",
    )
    assert parsed.projects["app"].project_alias == "app"
    assert parsed.projects["app"].checkout_alias == "app"
    with pytest.raises(config.ConfigError, match="portable identifier"):
        config.parse_config(
            {
                "schema_version": 1,
                "skills_root": "x",
                "projects": {"app": {"path": "x", field: "App Label"}},
            },
            tmp_path / "config.json",
        )


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


def test_config_rejects_unsafe_preferred_locale(tmp_path):
    with pytest.raises(config.ConfigError, match="preferred_locale"):
        config.parse_config(
            {
                "schema_version": 1,
                "skills_root": "x",
                "preferred_locale": "../en",
                "projects": {},
            },
            tmp_path / "config.json",
        )


def test_config_rejects_invalid_audit_mode(tmp_path):
    with pytest.raises(config.ConfigError, match="audit.mode"):
        config.parse_config(
            {
                "schema_version": 1,
                "skills_root": "x",
                "projects": {},
                "audit": {"mode": "blocking"},
            },
            tmp_path / "config.json",
        )


def test_config_rejects_unknown_audit_fields(tmp_path):
    with pytest.raises(config.ConfigError, match="unsupported field"):
        config.parse_config(
            {
                "schema_version": 1,
                "skills_root": "x",
                "projects": {},
                "audit": {"enabled": True, "prompt": "ignore this typo"},
            },
            tmp_path / "config.json",
        )


def test_config_rejects_invalid_audit_source_policy(tmp_path):
    with pytest.raises(config.ConfigError, match="audit.source_policy"):
        config.parse_config(
            {
                "schema_version": 1,
                "skills_root": "x",
                "projects": {},
                "audit": {"source_policy": {"rules": [{"pattern": "github.com", "class": "external"}]}},
            },
            tmp_path / "config.json",
        )


def test_config_rejects_invalid_audit_revocation(tmp_path):
    with pytest.raises(config.ConfigError, match="audit.revocations"):
        config.parse_config(
            {
                "schema_version": 1,
                "skills_root": str(tmp_path / "skills"),
                "projects": {},
                "audit": {"revocations": ["deadbeef"]},
            },
            tmp_path / "config.json",
        )


def test_config_rejects_unknown_audit_backend_reference(tmp_path):
    with pytest.raises(config.ConfigError, match="Unsupported audit backend"):
        config.parse_config(
            {
                "schema_version": 1,
                "skills_root": "x",
                "projects": {},
                "audit": {"backend": "codex"},
            },
            tmp_path / "config.json",
        )


def test_config_rejects_cloud_backend_without_allow_cloud(tmp_path):
    with pytest.raises(config.ConfigError, match="allow_cloud"):
        config.parse_config(
            {
                "schema_version": 1,
                "skills_root": "x",
                "projects": {},
                "audit": {
                    "backend": "codex",
                    "backends": {"codex": {"kind": "codex", "cloud": True}},
                },
            },
            tmp_path / "config.json",
        )


def test_config_rejects_codex_local_without_local_provider(tmp_path):
    with pytest.raises(config.ConfigError, match="oss=true and local_provider"):
        config.parse_config(
            {
                "schema_version": 1,
                "skills_root": "x",
                "projects": {},
                "audit": {
                    "backend": "codex",
                    "backends": {"codex": {"kind": "codex", "oss": True}},
                },
            },
            tmp_path / "config.json",
        )


def test_config_rejects_codex_cloud_with_local_provider(tmp_path):
    with pytest.raises(config.ConfigError, match="must not set oss or local_provider"):
        config.parse_config(
            {
                "schema_version": 1,
                "skills_root": "x",
                "projects": {},
                "audit": {
                    "backend": "codex",
                    "allow_cloud": True,
                    "backends": {
                        "codex": {
                            "kind": "codex",
                            "cloud": True,
                            "oss": True,
                            "local_provider": "ollama",
                        }
                    },
                },
            },
            tmp_path / "config.json",
        )


def test_config_rejects_unsafe_codex_extra_args(tmp_path):
    with pytest.raises(config.ConfigError, match="unsafe Codex option"):
        config.parse_config(
            {
                "schema_version": 1,
                "skills_root": "x",
                "projects": {},
                "audit": {
                    "backend": "codex",
                    "backends": {
                        "codex": {
                            "kind": "codex",
                            "oss": True,
                            "local_provider": "ollama",
                            "extra_args": ["--sandbox=danger-full-access"],
                        }
                    },
                },
            },
            tmp_path / "config.json",
        )


def test_config_rejects_invalid_max_request_bytes(tmp_path):
    with pytest.raises(config.ConfigError, match="audit.max_request_bytes"):
        config.parse_config(
            {
                "schema_version": 1,
                "skills_root": "x",
                "projects": {},
                "audit": {"max_request_bytes": 0},
            },
            tmp_path / "config.json",
        )


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("snapshot_max_age_seconds", 0),
        ("snapshot_clock_skew_seconds", -1),
        ("cache_ttl_seconds", -1),
        ("offline_grace_seconds", -1),
    ],
)
def test_config_rejects_invalid_registry_time_bounds(tmp_path, field, value):
    with pytest.raises(config.ConfigError, match=field):
        config.parse_config(
            {
                "schema_version": 1,
                "skills_root": "x",
                "projects": {},
                "audit": {field: value},
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
