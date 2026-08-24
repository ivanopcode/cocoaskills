"""The operator HTTPS token surface for external build repositories.

The invariants under test: the config stores a token *source* and never a
secret, the broker answers only for the pinned host and only the two prompts
Git asks, and every degradation — absent material, an unreadable state, a
foreign prompt — fails closed rather than yielding a credential.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from csk import build_https, config, git_admission, https_broker


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
