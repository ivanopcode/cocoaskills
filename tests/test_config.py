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


def test_go_future_family_policy_parses_round_trips_and_obeys_env_override(
    tmp_path, monkeypatch
):
    monkeypatch.delenv(config.GO_FUTURE_FAMILIES_ENV_VAR, raising=False)
    path = tmp_path / "config.json"
    raw = {
        "schema_version": 1,
        "skills_root": "x",
        "projects": {},
        "builds": {"go_future_families": "refuse"},
    }

    parsed = config.parse_config(raw, path)

    assert parsed.builds.go_future_families == "refuse"
    assert config.resolve_go_future_families(parsed.builds) == "refuse"
    assert (
        config.resolve_go_future_families(
            parsed.builds,
            {config.GO_FUTURE_FAMILIES_ENV_VAR: "warn"},
        )
        == "warn"
    )
    config.save_config(parsed)
    assert json.loads(path.read_text(encoding="utf-8"))["builds"] == {
        "go_future_families": "refuse"
    }


@pytest.mark.parametrize("value", ["allow", "", None, 1])
def test_go_future_family_policy_rejects_unknown_values(value, tmp_path, monkeypatch):
    monkeypatch.delenv(config.GO_FUTURE_FAMILIES_ENV_VAR, raising=False)
    with pytest.raises(config.ConfigError, match="go_future_families"):
        config.parse_config(
            {
                "schema_version": 1,
                "skills_root": "x",
                "projects": {},
                "builds": {"go_future_families": value},
            },
            tmp_path / "config.json",
        )


def test_go_future_family_policy_rejects_unknown_env_and_build_keys(
    tmp_path, monkeypatch
):
    monkeypatch.setenv(config.GO_FUTURE_FAMILIES_ENV_VAR, "allow")
    with pytest.raises(config.ConfigError, match=config.GO_FUTURE_FAMILIES_ENV_VAR):
        config.parse_config(
            {
                "schema_version": 1,
                "skills_root": "x",
                "projects": {},
            },
            tmp_path / "config.json",
        )
    monkeypatch.delenv(config.GO_FUTURE_FAMILIES_ENV_VAR)
    with pytest.raises(config.ConfigError, match="unsupported field"):
        config.parse_config(
            {
                "schema_version": 1,
                "skills_root": "x",
                "projects": {},
                "builds": {"typo": "warn"},
            },
            tmp_path / "config.json",
        )


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


def _minimal_config_data(tmp_path, **overrides):
    data = {
        "schema_version": 1,
        "skills_root": str(tmp_path / "skills"),
        "projects": {},
    }
    data.update(overrides)
    return data


def test_config_experimental_absent_defaults_to_disabled(tmp_path):
    cfg = config.parse_config(_minimal_config_data(tmp_path), tmp_path / "config.json")
    assert cfg.experimental.skillfile_sources is False


def test_config_experimental_flag_parses(tmp_path):
    cfg = config.parse_config(
        _minimal_config_data(tmp_path, experimental={"skillfile_sources": True}),
        tmp_path / "config.json",
    )
    assert cfg.experimental.skillfile_sources is True


@pytest.mark.parametrize(
    "experimental",
    [[], "yes", {"skillfile_sources": "yes"}, {"skillfile_sources": 1}, {"other": True}],
)
def test_config_experimental_malformed_fails(tmp_path, experimental):
    with pytest.raises(config.ConfigError, match="experimental"):
        config.parse_config(
            _minimal_config_data(tmp_path, experimental=experimental),
            tmp_path / "config.json",
        )


def test_config_experimental_flag_round_trips(tmp_path):
    path = tmp_path / "config.json"
    cfg = config.parse_config(
        _minimal_config_data(tmp_path, experimental={"skillfile_sources": True}), path
    )
    config.save_config(cfg)
    assert config.load_config(path).experimental.skillfile_sources is True


def test_config_absent_experimental_key_stays_absent_on_save(tmp_path):
    path = tmp_path / "config.json"
    cfg = config.parse_config(_minimal_config_data(tmp_path), path)
    config.save_config(cfg)
    assert "experimental" not in json.loads(path.read_bytes())


@pytest.mark.parametrize(
    ("env", "flag", "expected"),
    [
        (None, None, True),
        ("0", None, True),
        ("yes", None, True),
        ("1", None, True),
        (None, False, True),
        (None, True, True),
        ("1", False, True),
        ("0", True, True),
    ],
)
def test_skillfile_sources_enabled_matrix(monkeypatch, tmp_path, env, flag, expected):
    if env is None:
        monkeypatch.delenv("CSK_EXPERIMENTAL_SKILLFILE_SOURCES", raising=False)
    else:
        monkeypatch.setenv("CSK_EXPERIMENTAL_SKILLFILE_SOURCES", env)
    cfg = None
    if flag is not None:
        cfg = config.parse_config(
            _minimal_config_data(tmp_path, experimental={"skillfile_sources": flag}),
            tmp_path / "config.json",
        )
    assert config.skillfile_sources_enabled(cfg) is expected


def test_skillfile_sources_legacy_env_is_ignored(monkeypatch, tmp_path):
    monkeypatch.setenv("CSK_EXPERIMENTAL_SKILLFILE_SOURCES", "1")
    cfg = config.parse_config(_minimal_config_data(tmp_path), tmp_path / "config.json")
    assert cfg.experimental.skillfile_sources is False
    assert config.skillfile_sources_enabled(cfg) is True


def _write_valid_config(tmp_path, monkeypatch):
    from csk import cli  # noqa: F401

    cfg_path = tmp_path / "home" / "config.json"
    cfg = config.parse_config(_minimal_config_data(tmp_path), cfg_path)
    config.save_config(cfg)
    monkeypatch.setenv("CSK_CONFIG", str(cfg_path))
    return cfg_path


@pytest.mark.parametrize(
    ("extra_args", "expected_fragment"),
    [
        (["--identity", ""], "build_ssh.gitlab.example.com.identity must be a non-empty string"),
        (["--agent", ""], "build_ssh.gitlab.example.com.agent must be a non-empty string"),
        (
            ["--agent", "auto", "--known-hosts", ""],
            "build_ssh.gitlab.example.com.known_hosts must be a non-empty string",
        ),
        (
            ["--identity", "x" * 4097],
            "build_ssh.gitlab.example.com.identity must be a non-empty string",
        ),
        (
            ["--agent", "a" * 4097],
            "build_ssh.gitlab.example.com.agent must be a non-empty string",
        ),
    ],
)
def test_cli_build_ssh_add_rejects_invalid_values_and_preserves_config_bytes(
    tmp_path, monkeypatch, capsys, extra_args, expected_fragment
):
    # Invalid build-ssh add arguments fail before writing and leave config.json byte-identical; a valid add succeeds and reloads.
    from csk import cli

    cfg_path = _write_valid_config(tmp_path, monkeypatch)
    before = cfg_path.read_bytes()

    rc = cli.main(["config", "build-ssh", "add", "gitlab.example.com", *extra_args])
    err = capsys.readouterr().err
    assert rc != 0
    assert expected_fragment in err
    assert cfg_path.read_bytes() == before

    # Positive control: valid rule succeeds and reloads cleanly.
    assert (
        cli.main(
            ["config", "build-ssh", "add", "gitlab.example.com", "--identity", "~/.ssh/id_ed25519"]
        )
        == 0
    )
    capsys.readouterr()
    loaded = config.load_config(cfg_path)
    assert len(loaded.build_ssh) == 1
    assert loaded.build_ssh[0].identity == "~/.ssh/id_ed25519"


def test_cli_build_ssh_remove_repairs_broken_entry_without_weakening_load(
    tmp_path, monkeypatch, capsys
):
    # config build-ssh remove repairs an entry broken by an older release while load_config and unrelated invalid configs still refuse strictly.
    from csk import cli

    cfg_path = _write_valid_config(tmp_path, monkeypatch)
    raw = json.loads(cfg_path.read_text(encoding="utf-8"))
    raw["build_ssh"] = {
        "gitlab.example.com": {"identity": ""},
        "code.example.com": {"agent": "auto"},
    }
    cfg_path.write_text(json.dumps(raw, indent=2, sort_keys=True) + "\n", encoding="utf-8")

    # Load-time validation stays strict.
    assert cli.main(["config", "build-ssh", "list"]) == cli.EXIT_CONFIG
    assert "must be a non-empty string" in capsys.readouterr().err

    # Repair removes the broken scope and restores a valid config.
    assert cli.main(["config", "build-ssh", "remove", "gitlab.example.com"]) == 0
    out = capsys.readouterr().out
    assert "Removed build-ssh scope gitlab.example.com" in out
    loaded = config.load_config(cfg_path)
    assert [rule.scope for rule in loaded.build_ssh] == ["code.example.com"]
    assert cli.main(["config", "build-ssh", "list"]) == 0
    assert "code.example.com: agent=auto" in capsys.readouterr().out

    # Stepwise repair when two build_ssh scopes are broken works sequentially.
    raw2 = json.loads(cfg_path.read_text(encoding="utf-8"))
    raw2["build_ssh"] = {
        "first.example.com": {"identity": ""},
        "second.example.com": {"agent": ""},
    }
    cfg_path.write_text(json.dumps(raw2, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    assert cli.main(["config", "build-ssh", "remove", "first.example.com"]) == 0
    captured = capsys.readouterr()
    assert "Removed build-ssh scope first.example.com" in captured.out
    assert "warning:" in captured.err
    assert cli.main(["config", "build-ssh", "remove", "second.example.com"]) == 0
    capsys.readouterr()
    assert config.load_config(cfg_path).build_ssh == ()

    # Negative control: if the config is invalid outside build_ssh, remove refuses and preserves bytes.
    raw3 = json.loads(cfg_path.read_text(encoding="utf-8"))
    raw3["build_ssh"] = {"gitlab.example.com": {"identity": ""}}
    raw3["projects"] = {"my app": {"path": str(tmp_path)}}
    cfg_path.write_text(json.dumps(raw3, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    before_bad = cfg_path.read_bytes()
    assert cli.main(["config", "build-ssh", "remove", "gitlab.example.com"]) == cli.EXIT_CONFIG
    capsys.readouterr()
    assert cfg_path.read_bytes() == before_bad


def test_cli_project_add_rejects_invalid_alias_and_missing_path_preserving_config_bytes(
    tmp_path, monkeypatch, capsys
):
    # project add validates both alias and directory existence before writing, leaving config.json byte-identical on refusal; valid project add succeeds.
    from csk import cli

    cfg_path = _write_valid_config(tmp_path, monkeypatch)
    existing = tmp_path / "good-project"
    existing.mkdir()
    before = cfg_path.read_bytes()

    rc_alias = cli.main(["project", "add", "my app", str(existing)])
    err_alias = capsys.readouterr().err
    assert rc_alias != 0
    assert "portable identifier" in err_alias
    assert cfg_path.read_bytes() == before
    assert not (existing / "Skillfile.json").exists()

    missing = tmp_path / "nonexistent-project"
    rc_missing = cli.main(["project", "add", "good", str(missing)])
    err_missing = capsys.readouterr().err
    assert rc_missing != 0
    assert "project path does not exist" in err_missing
    assert cfg_path.read_bytes() == before

    # Positive control: valid alias and existing directory succeed and reload cleanly.
    assert cli.main(["project", "add", "good", str(existing)]) == 0
    capsys.readouterr()
    loaded = config.load_config(cfg_path)
    assert "good" in loaded.projects
    assert loaded.projects["good"].path == existing


def test_load_config_invalid_project_alias_names_file_and_key(tmp_path):
    # Invalid project alias load errors name both the config file path and the projects.<alias> key without weakening validation.
    cfg_path = tmp_path / "config.json"
    raw = _minimal_config_data(tmp_path, projects={"my app": {"path": str(tmp_path)}})
    cfg_path.write_text(json.dumps(raw, indent=2, sort_keys=True) + "\n", encoding="utf-8")

    with pytest.raises(config.ConfigError) as exc_info:
        config.load_config(cfg_path)
    message = str(exc_info.value)
    assert str(cfg_path) in message
    assert "projects.my app" in message
    assert "portable identifier" in message


def test_other_config_mutators_validate_before_writing(tmp_path, monkeypatch, capsys):
    # save_config, bootstrap, and build-https add/login reject invalid mutations before writing, leaving config.json byte-identical.
    from csk import cli

    cfg_path = _write_valid_config(tmp_path, monkeypatch)
    before = cfg_path.read_bytes()

    # Direct save_config validates with parse_config before touching disk.
    loaded = config.load_config(cfg_path)
    bad_cfg = config.GlobalConfig(
        path=cfg_path,
        skills_root=loaded.skills_root,
        preferred_locale="../bad",
        default_agents=loaded.default_agents,
        adapter_mode=loaded.adapter_mode,
        worktree_alias_pattern=loaded.worktree_alias_pattern,
        projects=loaded.projects,
    )
    with pytest.raises(config.ConfigError, match="preferred_locale"):
        config.save_config(bad_cfg)
    assert cfg_path.read_bytes() == before

    # bootstrap --non-interactive --force refuses invalid preferred_locale before overwriting.
    rc_boot = cli.main(
        [
            "bootstrap",
            "--non-interactive",
            "--force",
            "--skills-root",
            str(tmp_path / "skills"),
            "--preferred-locale",
            "../en",
        ]
    )
    capsys.readouterr()
    assert rc_boot != 0
    assert cfg_path.read_bytes() == before

    # build-https add rejects invalid token_env before writing.
    rc_https = cli.main(
        ["config", "build-https", "add", "gitlab.example.com", "--token-env", "not valid"]
    )
    capsys.readouterr()
    assert rc_https != 0
    assert cfg_path.read_bytes() == before

    # build-https login rejects oversized username before storing token or writing config.
    rc_login = cli.main(
        [
            "config",
            "build-https",
            "login",
            "gitlab.example.com",
            "--username",
            "u" * 4097,
        ]
    )
    capsys.readouterr()
    assert rc_login != 0
    assert cfg_path.read_bytes() == before
