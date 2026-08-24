"""The operator HTTPS token surface for external build repositories.

The invariants under test: the config stores a token *source* and never a
secret, the broker answers only for the pinned host and only the two prompts
Git asks, and every degradation — absent material, an unreadable state, a
foreign prompt — fails closed rather than yielding a credential.
"""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from csk import build_repository as build_repository_model
from csk import (
    build_https,
    config,
    dev_substitutions,
    git_admission,
    https_broker,
    installer,
    skillspec,
)


# --- grammar ----------------------------------------------------------------


def test_parse_rules_accepts_each_token_source() -> None:
    rules = build_https.parse_rules(
        {
            "gitlab.example.com/portals": {"token": "git-credentials"},
            "gitlab.example.com/vendor": {"token": "keyring", "username": "oauth2"},
            "ci.example.com": {"token_env": "CI_TOKEN"},
        }
    )
    assert {rule.scope for rule in rules} == {
        "gitlab.example.com/portals",
        "gitlab.example.com/vendor",
        "ci.example.com",
    }
    by_scope = {rule.scope: rule for rule in rules}
    assert by_scope["gitlab.example.com/vendor"].username == "oauth2"
    assert by_scope["ci.example.com"].token_env == "CI_TOKEN"


def test_parse_rules_refuses_a_literal_secret() -> None:
    # The single most important rejection: a token pasted into the config.
    with pytest.raises(build_https.BuildHTTPSError) as excinfo:
        build_https.parse_rules({"gitlab.example.com": {"token": "glpat-secret"}})
    assert "secrets never live in the config" in str(excinfo.value)


@pytest.mark.parametrize(
    "entry",
    [
        {},
        {"token": "keyring", "token_env": "CI_TOKEN"},
        {"token_env": "not an identifier"},
        {"token": "keyring", "helper": "evil"},
    ],
)
def test_parse_rules_rejects_invalid_entries(entry: dict) -> None:
    with pytest.raises(build_https.BuildHTTPSError):
        build_https.parse_rules({"gitlab.example.com": entry})


def test_scope_grammar_is_shared_with_the_ssh_surface() -> None:
    with pytest.raises(build_https.BuildHTTPSError):
        build_https.parse_rules({"UPPER.example.com": {"token": "keyring"}})
    rules = build_https.parse_rules(
        {
            "gitlab.example.com": {"token": "keyring"},
            "gitlab.example.com/portals/infra": {"token": "git-credentials"},
        }
    )
    matched = build_https.match(rules, "gitlab.example.com/portals/infra/cli/tool")
    assert matched is not None and matched.scope == "gitlab.example.com/portals/infra"
    assert build_https.match(rules, "gitlab.example.com/portals-evil/x").scope == (
        "gitlab.example.com"
    )


# --- config roundtrip -------------------------------------------------------


def _config_data(tmp_path: Path, build_https_value: dict) -> dict:
    return {
        "schema_version": 1,
        "skills_root": str(tmp_path / "skills"),
        "default_agents": ["claude_code"],
        "projects": {},
        "build_https": build_https_value,
    }


def test_config_roundtrips_build_https(tmp_path: Path) -> None:
    path = tmp_path / "config.json"
    data = _config_data(tmp_path, {"gitlab.example.com": {"token": "keyring"}})
    cfg = config.parse_config(data, path)
    assert cfg.build_https[0].token == "keyring"
    config.save_config(cfg)
    reloaded = config.parse_config(json.loads(path.read_text()), path)
    assert reloaded.build_https == cfg.build_https


def test_config_rejects_invalid_build_https(tmp_path: Path) -> None:
    data = _config_data(tmp_path, {"gitlab.example.com": {"token": "plaintext"}})
    with pytest.raises(config.ConfigError):
        config.parse_config(data, tmp_path / "config.json")


def test_build_https_is_not_lockable() -> None:
    # Operator credential selections are never lockable (ratified 2026-08-23).
    assert "build_https" not in config.LOCKABLE_KEYS
    assert "build_ssh" not in config.LOCKABLE_KEYS


# --- broker -----------------------------------------------------------------


def _state(tmp_path: Path, **overrides: object) -> str:
    payload = {"host": "gitlab.example.com", "username": "oauth2"}
    payload.update(overrides)
    path = tmp_path / "state.json"
    path.write_text(json.dumps(payload))
    return str(path)


def test_broker_answers_both_prompts_for_the_pinned_host(tmp_path: Path) -> None:
    state = _state(tmp_path)
    env = {https_broker.TOKEN_ENV: "s3cret"}
    assert (
        https_broker.answer(state, "Username for 'https://gitlab.example.com': ", env)
        == "oauth2"
    )
    assert (
        https_broker.answer(state, "Password for 'https://gitlab.example.com': ", env)
        == "s3cret"
    )


def test_broker_accepts_the_userinfo_form_git_uses(tmp_path: Path) -> None:
    state = _state(tmp_path)
    env = {https_broker.TOKEN_ENV: "s3cret"}
    prompt = "Password for 'https://oauth2@gitlab.example.com': "
    assert https_broker.answer(state, prompt, env) == "s3cret"


@pytest.mark.parametrize(
    "prompt",
    [
        "Password for 'https://evil.example.com': ",
        "Password for 'https://gitlab.example.com.evil.test': ",
        "Password for 'http://gitlab.example.com': ",
        "Passphrase for key '/home/o/.ssh/id_ed25519': ",
        "",
        "arbitrary text",
    ],
)
def test_broker_fails_closed_on_anything_but_the_pinned_https_host(
    tmp_path: Path, prompt: str
) -> None:
    state = _state(tmp_path)
    env = {https_broker.TOKEN_ENV: "s3cret"}
    assert https_broker.answer(state, prompt, env) is None


def test_broker_fails_closed_without_material(tmp_path: Path) -> None:
    state = _state(tmp_path)
    prompt = "Password for 'https://gitlab.example.com': "
    assert https_broker.answer(state, prompt, {}) is None


def test_broker_fails_closed_on_unreadable_or_foreign_state(tmp_path: Path) -> None:
    prompt = "Password for 'https://gitlab.example.com': "
    env = {https_broker.TOKEN_ENV: "s3cret"}
    assert https_broker.answer(str(tmp_path / "absent.json"), prompt, env) is None
    broken = tmp_path / "broken.json"
    broken.write_text("{not json")
    assert https_broker.answer(str(broken), prompt, env) is None
    assert https_broker.answer(_state(tmp_path, host=""), prompt, env) is None
    assert https_broker.answer(_state(tmp_path, username=""), prompt, env) is None


def test_broker_main_prints_nothing_when_it_declines(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    state = _state(tmp_path)
    code = https_broker.main([state, "Password for 'https://evil.example.com': "])
    assert code == 1
    assert capsys.readouterr().out == ""


# --- credential plumbing ----------------------------------------------------


def test_token_value_never_reaches_a_diagnostic() -> None:
    credentials = git_admission.OperatorHTTPSCredentials(
        scope="gitlab.example.com",
        host="gitlab.example.com",
        source="env",
        username="oauth2",
        token_value="s3cret",
    )
    assert "s3cret" not in repr(credentials)
    assert "s3cret" not in str(credentials)


def test_serialized_rules_carry_no_secret_material() -> None:
    rules = build_https.parse_rules(
        {"gitlab.example.com": {"token": "keyring", "username": "oauth2"}}
    )
    serialized = build_https.serialize_rules(rules)
    assert serialized == {
        "gitlab.example.com": {"token": "keyring", "username": "oauth2"}
    }


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


def _https_config(
    tmp_path: Path, rules: tuple[build_https.BuildHTTPSRule, ...]
) -> config.GlobalConfig:
    return config.GlobalConfig(
        path=tmp_path / "config.json",
        skills_root=tmp_path / "skills",
        preferred_locale=None,
        default_agents=["claude_code"],
        adapter_mode="auto",
        worktree_alias_pattern=config.DEFAULT_WORKTREE_ALIAS_PATTERN,
        projects={},
        build_https=rules,
    )


def test_resolver_prefers_the_run_wide_token_over_a_matching_scope(
    tmp_path: Path,
) -> None:
    # The scope names an unset variable, so consulting it would raise: the
    # run-wide override must win before the scope is ever resolved.
    node = _node("skill-a", "https://gitlab.example.com/portals/infra/tool")
    messages: list[str] = []
    selection = installer._resolve_build_https_credentials(
        _https_config(
            tmp_path,
            (
                build_https.BuildHTTPSRule(
                    scope="gitlab.example.com", token_env="CSK_TEST_UNSET_PROOF"
                ),
            ),
        ),
        [(node, "tool")],
        _empty_dev_manifest(),
        run_wide=installer.OperatorHTTPSToken(username="oauth2", token="s3cret"),
        interactive=False,
        messages=messages,
        dry_run=True,
    )
    credentials = selection[("skill-a", "tool")]
    assert credentials is not None
    assert credentials.source == "env"
    assert credentials.token_value == "s3cret"
    assert any("operator environment" in message for message in messages)


def test_resolver_host_pin_limits_the_run_wide_token(tmp_path: Path) -> None:
    # HTTPS basic auth transmits the token to whichever host receives the
    # fetch, so the pinned override must never reach a foreign host.
    pinned = _node("skill-a", "https://gitlab.example.com/x/tool")
    foreign = _node("skill-b", "https://other.example.com/y/tool")
    messages: list[str] = []
    selection = installer._resolve_build_https_credentials(
        _https_config(tmp_path, ()),
        [(pinned, "tool"), (foreign, "tool")],
        _empty_dev_manifest(),
        run_wide=installer.OperatorHTTPSToken(
            username="oauth2", token="s3cret", host="gitlab.example.com"
        ),
        interactive=False,
        messages=messages,
        dry_run=True,
    )
    credentials = selection[("skill-a", "tool")]
    assert credentials is not None
    assert credentials.host == "gitlab.example.com"
    assert credentials.token_value == "s3cret"
    assert selection[("skill-b", "tool")] is None
    assert any("anonymous" in message for message in messages)


def test_capture_reads_the_optional_host_pin(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("CSK_BUILD_HTTPS_TOKEN", "s3cret")
    monkeypatch.setenv("CSK_BUILD_HTTPS_USERNAME", "oauth2")
    monkeypatch.setenv("CSK_BUILD_HTTPS_HOST", " GitLab.Example.Com ")
    captured = installer._capture_operator_https_token()
    assert captured == installer.OperatorHTTPSToken(
        username="oauth2", token="s3cret", host="gitlab.example.com"
    )
    assert "s3cret" not in repr(captured)


def test_resolver_stays_anonymous_when_nothing_matches(tmp_path: Path) -> None:
    node = _node("skill-a", "https://gitlab.example.com/portals/tool")
    messages: list[str] = []
    selection = installer._resolve_build_https_credentials(
        _https_config(
            tmp_path,
            (
                build_https.BuildHTTPSRule(
                    scope="ci.example.com", token_env="CSK_TEST_UNSET_PROOF"
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
    assert selection[("skill-a", "tool")] is None
    assert any("anonymous" in message for message in messages)


def test_resolver_skips_ssh_repositories(tmp_path: Path) -> None:
    # The transport check precedes even the run-wide override.
    node = _node("skill-a", "git@gitlab.example.com:portals/tool.git")
    selection = installer._resolve_build_https_credentials(
        _https_config(tmp_path, ()),
        [(node, "tool")],
        _empty_dev_manifest(),
        run_wide=installer.OperatorHTTPSToken(username="oauth2", token="s3cret"),
        interactive=False,
        messages=[],
        dry_run=False,
    )
    assert selection[("skill-a", "tool")] is None


def test_resolver_selects_the_longest_config_scope(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("CSK_TEST_CI_TOKEN", "s3cret")
    node = _node("skill-a", "https://gitlab.example.com/portals/infra/tool")
    messages: list[str] = []
    selection = installer._resolve_build_https_credentials(
        _https_config(
            tmp_path,
            (
                build_https.BuildHTTPSRule(
                    scope="gitlab.example.com", token_env="CSK_TEST_UNSET_PROOF"
                ),
                build_https.BuildHTTPSRule(
                    scope="gitlab.example.com/portals",
                    token_env="CSK_TEST_CI_TOKEN",
                    username="oauth2",
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
    assert credentials.token_value == "s3cret"
    assert credentials.username == "oauth2"
    assert credentials.host == "gitlab.example.com"
    assert any(
        "config scope 'gitlab.example.com/portals'" in message
        for message in messages
    )


def test_rule_credentials_fail_closed_on_an_unset_token_env() -> None:
    rule = build_https.BuildHTTPSRule(
        scope="h.example.com", token_env="CSK_TEST_UNSET_PROOF"
    )
    with pytest.raises(installer.InstallError) as excinfo:
        installer._https_rule_credentials(rule, "h.example.com/x/tool")
    text = str(excinfo.value)
    assert git_admission.CREDENTIAL_POLICY_INVALID in text
    assert "unset" in text


def test_rule_credentials_fail_closed_on_an_absent_keyring_token(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(build_https, "read_namespaced_token", lambda *a, **k: None)
    rule = build_https.BuildHTTPSRule(scope="h.example.com", token="keyring")
    with pytest.raises(installer.InstallError) as excinfo:
        installer._https_rule_credentials(rule, "h.example.com/x/tool")
    text = str(excinfo.value)
    assert git_admission.CREDENTIAL_POLICY_INVALID in text
    assert "csk config build-https login h.example.com" in text


def test_rule_credentials_fail_closed_on_an_absent_host_credential(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(build_https, "read_host_credentials", lambda *a, **k: None)
    rule = build_https.BuildHTTPSRule(scope="h.example.com", token="git-credentials")
    with pytest.raises(installer.InstallError) as excinfo:
        installer._https_rule_credentials(rule, "h.example.com/x/tool")
    text = str(excinfo.value)
    assert git_admission.CREDENTIAL_POLICY_INVALID in text
    assert "clone once over HTTPS" in text


def test_a_run_only_prompt_choice_never_reaches_the_config(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Two prompts in one run: only the persisted answer may be saved."""

    node_a = _node("skill-a", "https://one.example.com/x/tool")
    node_b = _node("skill-b", "https://two.example.com/y/tool")
    prompted = iter(
        [
            (
                build_https.BuildHTTPSRule(
                    scope="one.example.com/x", token="git-credentials"
                ),
                False,
            ),
            (
                build_https.BuildHTTPSRule(
                    scope="two.example.com/y", token="git-credentials"
                ),
                True,
            ),
        ]
    )
    monkeypatch.setattr(
        installer, "_prompt_build_https_rule", lambda *a, **k: next(prompted)
    )
    dummy = git_admission.OperatorHTTPSCredentials(
        scope="s", host="h", source="git-credentials", username="u", token_value="t"
    )
    monkeypatch.setattr(installer, "_https_rule_credentials", lambda *a, **k: dummy)
    saved: list[config.GlobalConfig] = []
    monkeypatch.setattr(installer.config_module, "save_config", saved.append)
    installer._resolve_build_https_credentials(
        _https_config(tmp_path, ()),
        [(node_a, "tool"), (node_b, "tool")],
        _empty_dev_manifest(),
        run_wide=None,
        interactive=True,
        messages=[],
        dry_run=False,
    )
    assert len(saved) == 1
    assert [rule.scope for rule in saved[0].build_https] == ["two.example.com/y"]


# --- interactive precheck ----------------------------------------------------


def test_prompt_selects_the_default_candidate(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(
        build_https,
        "discover_host_material",
        lambda *a, **k: build_https.HostMaterial(
            host_credentials=True, host_username="oauth2"
        ),
    )
    answers = iter(["", ""])  # Enter on the menu, Enter on the scope choice
    monkeypatch.setattr("builtins.input", lambda *_: next(answers))
    rule, persist = installer._prompt_build_https_rule(
        "skill-a", "tool", "gitlab.example.com/portals/infra/tool"
    )
    assert rule is not None
    assert persist is True
    assert rule.token == "git-credentials"
    assert rule.scope == "gitlab.example.com/portals/infra"
    assert "Detected candidates" in capsys.readouterr().out


def test_prompt_this_run_only_does_not_persist(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        build_https,
        "discover_host_material",
        lambda *a, **k: build_https.HostMaterial(
            host_credentials=True, host_username="oauth2"
        ),
    )
    answers = iter(["1", "3"])
    monkeypatch.setattr("builtins.input", lambda *_: next(answers))
    rule, persist = installer._prompt_build_https_rule(
        "skill-a", "tool", "gitlab.example.com/portals/infra/tool"
    )
    assert rule is not None
    assert persist is False
    assert rule.scope == "gitlab.example.com/portals/infra"


def test_prompt_abort(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        build_https,
        "discover_host_material",
        lambda *a, **k: build_https.HostMaterial(),
    )
    monkeypatch.setattr("builtins.input", lambda *_: "n")
    rule, persist = installer._prompt_build_https_rule(
        "skill-a", "tool", "gitlab.example.com/x/tool"
    )
    assert rule is None and persist is False


# --- broker materialization --------------------------------------------------


def test_materialize_broker_rejects_a_foreign_host(tmp_path: Path) -> None:
    paths = git_admission._make_private_paths(tmp_path)
    source = build_repository_model.parse_repository_source(
        "https://gitlab.example.com/x/tool"
    )
    credentials = git_admission.OperatorHTTPSCredentials(
        scope="other.example.com",
        host="other.example.com",
        source="env",
        username="oauth2",
        token_value="s3cret",
    )
    with pytest.raises(git_admission.GitAdmissionError) as excinfo:
        git_admission._materialize_https_broker(paths, source, credentials)
    assert excinfo.value.code == git_admission.CREDENTIAL_POLICY_INVALID


def test_broker_state_carries_only_host_and_username(tmp_path: Path) -> None:
    paths = git_admission._make_private_paths(tmp_path)
    source = build_repository_model.parse_repository_source(
        "https://gitlab.example.com/x/tool"
    )
    credentials = git_admission.OperatorHTTPSCredentials(
        scope="gitlab.example.com",
        host="gitlab.example.com",
        source="keyring",
        username="oauth2",
        token_value="s3cret",
    )
    wrapper = git_admission._materialize_https_broker(paths, source, credentials)
    state = json.loads((paths.https / "broker-state.json").read_text())
    assert state == {"host": "gitlab.example.com", "username": "oauth2"}
    assert "s3cret" not in wrapper.read_text()


# --- cross-platform credential mechanism ------------------------------------


def test_namespaced_username_separates_manager_entries() -> None:
    # The manager entry must not collide with the operator's own credential
    # for the same host, on any platform's helper.
    assert build_https.namespace_username("gitlab.example.com/portals") == (
        "csk-build-https:gitlab.example.com/portals"
    )
    assert build_https.scope_host("gitlab.example.com/portals/infra") == (
        "gitlab.example.com"
    )


def test_credential_reads_go_through_git_on_every_platform(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """No platform branch: one mechanism, whichever helper the operator has."""

    calls: list[tuple[tuple[str, ...], str]] = []

    class _Completed:
        returncode = 0
        stdout = b"protocol=https\nhost=gitlab.example.com\nusername=oauth2\npassword=s3cret\n"

    def fake_run(argv, **kwargs):  # type: ignore[no-untyped-def]
        calls.append((tuple(argv), kwargs["input"].decode()))
        return _Completed()

    monkeypatch.setattr(build_https.subprocess, "run", fake_run)
    material = build_https.read_host_credentials(
        "gitlab.example.com", "/usr/bin/git", "/home/operator"
    )
    assert material == ("oauth2", "s3cret")
    argv, payload = calls[0]
    assert argv == ("/usr/bin/git", "credential", "fill")
    assert "host=gitlab.example.com" in payload


def test_credential_calls_disable_interactive_prompting(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen: dict[str, str] = {}

    class _Completed:
        returncode = 1
        stdout = b""

    def fake_run(argv, **kwargs):  # type: ignore[no-untyped-def]
        seen.update(kwargs["env"])
        return _Completed()

    monkeypatch.setattr(build_https.subprocess, "run", fake_run)
    assert build_https.read_host_credentials("gitlab.example.com") is None
    assert seen["GIT_TERMINAL_PROMPT"] == "0"
    assert seen["GCM_INTERACTIVE"] == "never"


def test_operator_home_is_pinned_for_the_helper_lookup(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # The fetch owns a private HOME; a helper configured in the operator's Git
    # configuration is only found when the manager restores their home.
    seen: dict[str, str] = {}

    class _Completed:
        returncode = 1
        stdout = b""

    def fake_run(argv, **kwargs):  # type: ignore[no-untyped-def]
        seen.update(kwargs["env"])
        return _Completed()

    monkeypatch.setattr(build_https.subprocess, "run", fake_run)
    build_https.read_namespaced_token("h/x", "h", None, "/home/operator")
    assert seen["HOME"] == "/home/operator"
    assert seen["USERPROFILE"] == "/home/operator"


def test_store_reports_a_missing_helper_with_platform_guidance(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(build_https, "_run_credential", lambda *a, **k: None)
    with pytest.raises(build_https.BuildHTTPSError) as excinfo:
        build_https.store_namespaced_token("h/x", "h", "tok")
    message = str(excinfo.value)
    assert "osxkeychain" in message
    assert "libsecret" in message
    assert "dpapi" in message


def test_store_rejects_a_helper_that_silently_persists_nothing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Observed on Windows: approve exits 0 while the store is unreachable.

    Git Credential Manager reports success and stores nothing when its
    Windows Credential Manager store has no interactive session behind it, so
    the write is only trusted after reading it back.
    """

    answers = iter([{"ok": "1"}, None])
    monkeypatch.setattr(
        build_https, "_run_credential", lambda *a, **k: next(answers)
    )
    with pytest.raises(build_https.BuildHTTPSError) as excinfo:
        build_https.store_namespaced_token("h/x", "h", "tok")
    assert "did not persist" in str(excinfo.value)


def test_store_accepts_a_helper_that_reads_the_token_back(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    answers = iter([{"ok": "1"}, {"password": "tok"}])
    monkeypatch.setattr(
        build_https, "_run_credential", lambda *a, **k: next(answers)
    )
    build_https.store_namespaced_token("h/x", "h", "tok")
