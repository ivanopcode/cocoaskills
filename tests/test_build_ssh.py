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


# --- candidate discovery -----------------------------------------------------


def test_discover_candidates_lists_pub_files_only(tmp_path: Path) -> None:
    ssh_dir = tmp_path / ".ssh"
    ssh_dir.mkdir()
    (ssh_dir / "work.pub").write_text("ssh-ed25519 AAAA a\n")
    (ssh_dir / "personal.pub").write_text("ssh-ed25519 AAAA b\n")
    (ssh_dir / "work").write_text("PRIVATE\n")
    (ssh_dir / "config").write_text("Host *\n")

    found = build_ssh.discover_candidates(environment={}, home=str(tmp_path))

    assert found.agent_socket is None
    assert [Path(p).name for p in found.public_keys] == ["personal.pub", "work.pub"]


def test_discover_candidates_reports_agent_socket(tmp_path: Path) -> None:
    found = build_ssh.discover_candidates(
        environment={"SSH_AUTH_SOCK": str(tmp_path / "sock"), "PATH": ""},
        home=str(tmp_path),
    )
    # ssh-add is unreachable with an empty PATH; the socket still lists as a
    # candidate with an unknown key count.
    assert found.agent_socket == str(tmp_path / "sock")
    assert found.agent_key_count is None


def test_candidate_commands_prefer_pinned_agent(tmp_path: Path) -> None:
    candidates = build_ssh.DiscoveredCandidates(
        agent_socket="/tmp/sock",
        agent_key_count=5,
        public_keys=(str(tmp_path / "work.pub"), str(tmp_path / "b.pub")),
    )
    commands = build_ssh.candidate_commands("gitlab.example.com/portals", candidates)
    assert commands[0] == (
        "csk config build-ssh add gitlab.example.com/portals "
        f"--agent auto --identity {tmp_path / 'work.pub'}"
    )
    assert commands[1] == "csk config build-ssh add gitlab.example.com/portals --agent auto"
    assert any("--identity" in c and ".pub" not in c.split("--identity ")[1] for c in commands[2:])


def test_candidate_commands_without_agent(tmp_path: Path) -> None:
    candidates = build_ssh.DiscoveredCandidates(
        agent_socket=None, public_keys=(str(tmp_path / "k.pub"),)
    )
    commands = build_ssh.candidate_commands("gitlab.example.com", candidates)
    assert commands == [
        f"csk config build-ssh add gitlab.example.com --identity {tmp_path / 'k'}"
    ]


def test_missing_credentials_message_lists_candidates(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    node = _node("skill-a", "git@gitlab.example.com:portals/infra/tool.git")
    monkeypatch.setattr(
        build_ssh,
        "discover_candidates",
        lambda *a, **k: build_ssh.DiscoveredCandidates(
            agent_socket="/tmp/sock",
            agent_key_count=2,
            public_keys=(str(tmp_path / "work.pub"),),
        ),
    )
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
    assert "--agent auto --identity" in text
    assert str(tmp_path / "work.pub") in text


def test_prompt_selects_default_candidate(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        build_ssh,
        "discover_candidates",
        lambda *a, **k: build_ssh.DiscoveredCandidates(
            agent_socket="/tmp/sock",
            agent_key_count=3,
            public_keys=(str(tmp_path / "work.pub"),),
        ),
    )
    answers = iter(["", ""])  # Enter on the menu, Enter on the scope choice
    monkeypatch.setattr("builtins.input", lambda *_: next(answers))
    rule, persist = installer._prompt_build_ssh_rule(
        "skill-a", "tool", "gitlab.example.com/portals/infra/tool"
    )
    assert rule is not None
    assert persist is True
    assert rule.agent == "auto"
    assert rule.identity == str(tmp_path / "work.pub")
    assert rule.scope == "gitlab.example.com/portals/infra"


def test_prompt_abort(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        build_ssh,
        "discover_candidates",
        lambda *a, **k: build_ssh.DiscoveredCandidates(),
    )
    monkeypatch.setattr("builtins.input", lambda *_: "n")
    rule, persist = installer._prompt_build_ssh_rule(
        "skill-a", "tool", "gitlab.example.com/x/y"
    )
    assert rule is None and persist is False
