from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest

from csk import build_repository as build_repository_model
from csk import build_ssh, config, dev_substitutions, git_admission, installer, skillspec


# --- grammar -----------------------------------------------------------------


def test_parse_rules_accepts_host_and_namespace_scopes() -> None:
    rules = build_ssh.parse_rules(
        {
            "gitlab.example.com": {"identity": "~/.ssh/personal"},
            "gitlab.example.com/portals/Infra_x": {
                "agent": "auto",
                "identity": "~/.ssh/work.pub",
            },
        }
    )
    assert {rule.scope for rule in rules} == {
        "gitlab.example.com",
        "gitlab.example.com/portals/Infra_x",
    }


@pytest.mark.parametrize(
    "scope",
    [
        "",
        "UPPER.example.com",
        "gitlab.example.com//x",
        "gitlab.example.com/..",
        "gitlab.example.com/seg ment",
        " gitlab.example.com",
        "https://gitlab.example.com/x",
    ],
)
def test_parse_rules_rejects_invalid_scopes(scope: str) -> None:
    with pytest.raises(build_ssh.BuildSSHError):
        build_ssh.parse_rules({scope: {"agent": "auto"}})


def test_parse_rules_requires_a_selection_and_rejects_unknown_fields() -> None:
    with pytest.raises(build_ssh.BuildSSHError):
        build_ssh.parse_rules({"gitlab.example.com": {}})
    with pytest.raises(build_ssh.BuildSSHError):
        build_ssh.parse_rules({"gitlab.example.com": {"agent": "auto", "token": "x"}})


# --- matching ----------------------------------------------------------------


def _rules(*scopes: str) -> tuple[build_ssh.BuildSSHRule, ...]:
    return tuple(
        build_ssh.BuildSSHRule(scope=scope, agent="auto") for scope in scopes
    )


def test_match_prefers_the_longest_scope() -> None:
    rules = _rules("gitlab.example.com", "gitlab.example.com/portals/infra")
    matched = build_ssh.match(rules, "gitlab.example.com/portals/infra/cli/tool")
    assert matched is not None
    assert matched.scope == "gitlab.example.com/portals/infra"


def test_match_stops_at_segment_boundaries() -> None:
    rules = _rules("gitlab.example.com/portals")
    assert build_ssh.match(rules, "gitlab.example.com/portals-evil/tool") is None
    assert build_ssh.match(rules, "gitlab.example.com/portals/tool") is not None


def test_default_scope_is_the_repository_namespace() -> None:
    assert (
        build_ssh.default_scope("gitlab.example.com/portals/infra/cli/tool")
        == "gitlab.example.com/portals/infra/cli"
    )
    assert build_ssh.default_scope("gitlab.example.com/tool") == "gitlab.example.com/tool"


# --- config roundtrip --------------------------------------------------------


def test_config_roundtrips_build_ssh(tmp_path: Path) -> None:
    path = tmp_path / "config.json"
    path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "skills_root": str(tmp_path / "skills"),
                "default_agents": ["claude_code"],
                "projects": {},
                "build_ssh": {
                    "gitlab.example.com/portals": {
                        "agent": "auto",
                        "identity": "~/.ssh/work.pub",
                    }
                },
            }
        )
    )
    cfg = config.parse_config(json.loads(path.read_text()), path)
    assert cfg.build_ssh[0].scope == "gitlab.example.com/portals"
    config.save_config(cfg)
    reloaded = config.parse_config(json.loads(path.read_text()), path)
    assert reloaded.build_ssh == cfg.build_ssh


def test_config_rejects_invalid_build_ssh(tmp_path: Path) -> None:
    data = {
        "schema_version": 1,
        "skills_root": str(tmp_path),
        "default_agents": ["claude_code"],
        "projects": {},
        "build_ssh": {"gitlab.example.com": {}},
    }
    with pytest.raises(config.ConfigError):
        config.parse_config(data, tmp_path / "config.json")


# --- per-repository resolution ----------------------------------------------


def _node(name: str, git: str) -> SimpleNamespace:
    source = build_repository_model.parse_repository_source(git)
    repository = build_repository_model.BuildRepository(
        name="tool-repo",
        git=git,
        identity=source.identity,
        transport=source.transport,
        locked_commit=build_repository_model.LockedCommit("sha1", "0" * 40),
    )
    command = skillspec.CommandSpec(
        name="tool",
        type="build",
        driver="go-repository-v1",
        repository="tool-repo",
        target="tool",
        source="agent-skill.json",
    )
    spec = SimpleNamespace(
        commands={"tool": command},
        build_repositories={"tool-repo": repository},
    )
    return SimpleNamespace(name=name, spec=spec)


def _empty_dev_manifest() -> dev_substitutions.DevManifest:
    return dev_substitutions.DevManifest(
        schema_version=1, substitutions={}, build_repository_substitutions={}
    )


def _config_with_rules(
    tmp_path: Path, rules: tuple[build_ssh.BuildSSHRule, ...]
) -> config.GlobalConfig:
    return config.GlobalConfig(
        path=tmp_path / "config.json",
        skills_root=tmp_path / "skills",
        preferred_locale=None,
        default_agents=["claude_code"],
        adapter_mode="auto",
        worktree_alias_pattern=config.DEFAULT_WORKTREE_ALIAS_PATTERN,
        projects={},
        build_ssh=rules,
    )


def test_resolver_fails_closed_with_protocol_code_and_remedy(tmp_path: Path) -> None:
    node = _node("skill-a", "git@gitlab.example.com:portals/infra/tool.git")
    with pytest.raises(installer.InstallError) as excinfo:
        installer._resolve_build_ssh_credentials(
            _config_with_rules(tmp_path, ()),
            [(node, "tool")],
            _empty_dev_manifest(),
            run_wide=None,
            interactive=False,
            messages=[],
            dry_run=False,
        )
    text = str(excinfo.value)
    assert git_admission.SSH_CREDENTIAL_MISSING in text
    assert "csk config build-ssh add gitlab.example.com/portals/infra" in text
    assert "skill-a" in text


def test_resolver_prefers_run_wide_credentials(tmp_path: Path) -> None:
    node = _node("skill-a", "git@gitlab.example.com:portals/infra/tool.git")
    agent_socket = tmp_path / "agent.sock"
    run_wide = git_admission.OperatorSSHCredentials(
        identity=None, agent_socket=agent_socket, known_hosts=None
    )
    messages: list[str] = []
    selection = installer._resolve_build_ssh_credentials(
        _config_with_rules(
            tmp_path,
            (build_ssh.BuildSSHRule(scope="gitlab.example.com", identity="/nonexistent"),),
        ),
        [(node, "tool")],
        _empty_dev_manifest(),
        run_wide=run_wide,
        interactive=False,
        messages=messages,
        dry_run=True,
    )
    assert selection[("skill-a", "tool")] is run_wide
    assert any("operator flags/env" in message for message in messages)


def test_resolver_selects_config_scope_for_ssh_repository(tmp_path: Path) -> None:
    identity_file = tmp_path / "key.pub"
    identity_file.write_text("ssh-ed25519 AAAA test\n")
    known_hosts = tmp_path / "known_hosts"
    known_hosts.write_text("gitlab.example.com ssh-ed25519 AAAA\n")
    node = _node("skill-a", "git@gitlab.example.com:portals/infra/tool.git")
    messages: list[str] = []
    selection = installer._resolve_build_ssh_credentials(
        _config_with_rules(
            tmp_path,
            (
                build_ssh.BuildSSHRule(
                    scope="gitlab.example.com/portals",
                    identity=str(identity_file),
                    known_hosts=str(known_hosts),
                ),
            ),
        ),
        [(node, "tool")],
        _empty_dev_manifest(),
        run_wide=None,
        interactive=False,
        messages=messages,
        dry_run=True,
    )
    credentials = selection[("skill-a", "tool")]
    assert credentials is not None
    assert credentials.identity == identity_file.resolve()
    assert any("config scope 'gitlab.example.com/portals'" in m for m in messages)


def test_resolver_skips_https_and_local_repositories(tmp_path: Path) -> None:
    node = _node("skill-a", "https://gitlab.example.com/portals/infra/tool")
    selection = installer._resolve_build_ssh_credentials(
        _config_with_rules(tmp_path, ()),
        [(node, "tool")],
        _empty_dev_manifest(),
        run_wide=None,
        interactive=False,
        messages=[],
        dry_run=False,
    )
    assert selection[("skill-a", "tool")] is None


def test_add_project_preserves_build_ssh(tmp_path: Path) -> None:
    cfg = _config_with_rules(
        tmp_path, (build_ssh.BuildSSHRule(scope="gitlab.example.com", agent="auto"),)
    )
    updated = config.add_project(cfg, "probe", tmp_path / "probe")
    assert updated.build_ssh == cfg.build_ssh
