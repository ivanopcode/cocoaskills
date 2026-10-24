"""App identity consent and publication through the public CLI."""
from __future__ import annotations

import json
import sys
import tomllib
from dataclasses import replace

import pytest

from csk import cli, mcp_launchers as launchers, mcp_ownership as ownership, mcp_project
from test_mcp_install_acceptance import _snapshot
from test_mcp_project_install import project_cli, install, native_bytes, PATHS
from test_mcp_launchers import MAC, bundle, declaration, good_verification

pytestmark = pytest.mark.usefixtures("stable_env")


@pytest.fixture
def app_cli(project_cli, tmp_path, monkeypatch):
    root, home, declare = project_cli
    apps = tmp_path / "Applications"
    _, helper = bundle(apps)
    monkeypatch.setattr(launchers, "macos_roots", lambda: (apps,))
    monkeypatch.setattr(launchers, "_platform", lambda: "macos")
    monkeypatch.setattr(launchers, "verify_macos", lambda *args: good_verification())
    declare(declaration())
    return root, home, helper, declare


def approve(monkeypatch):
    monkeypatch.setattr(sys.stdin, "isatty", lambda: True)
    monkeypatch.setattr(sys.stdout, "isatty", lambda: True)
    prompts = []
    monkeypatch.setattr("builtins.input", lambda p: prompts.append(p) or "yes")
    return prompts


def status(capsys, *args):
    code = cli.main(["status", "app", "--json", *args])
    payload = json.loads(capsys.readouterr().out)[0]
    return code, [row["state"] for row in payload["mcp"]]


# Invalid signatures refuse check/install before prompting, with no native/store publication; the adjacent valid control installs absolute paths once.
@pytest.mark.parametrize("argv", [["check", "app"], ["install", "app"], ["install", "app", "--dry-run"]])
def test_verify_before_consent_and_no_partial_writes(app_cli, monkeypatch, capsys, argv):
    root, home, helper, _ = app_cli
    monkeypatch.setattr(launchers, "verify_macos", lambda *a: replace(good_verification(), signature_valid=False))
    monkeypatch.setattr("builtins.input", lambda *a: pytest.fail("untrusted app reached consent"))
    before = _snapshot(root, home)
    assert cli.main(argv) == 1
    text = capsys.readouterr()
    assert "mcp_launcher_untrusted" in text.err + text.out
    assert _snapshot(root, home) == before
    monkeypatch.setattr(launchers, "verify_macos", lambda *a: good_verification())
    assert cli.main(["check", "app"]) == 0
    capsys.readouterr()
    assert _snapshot(root, home) == before
    prompts = approve(monkeypatch)
    assert install() == 0
    out = capsys.readouterr().out
    assert "verified publisher and notarized application" in out
    assert MAC["team_id"] in out and str(helper.resolve()) in out
    assert len(prompts) == 1
    for path in PATHS:
        document = tomllib.loads((root / path).read_text()) if path.endswith("toml") else json.loads((root / path).read_bytes())
        entry = document.get("mcpServers", document.get("mcp_servers"))["app"]
        assert entry["command"] == str(helper.resolve())
    before = _snapshot(root, home)
    assert install() == 0
    capsys.readouterr()
    assert len(prompts) == 1
    assert _snapshot(root, home) == before
    assert status(capsys, "--check") == (0, ["current"] * 3)


# Injected resolved identities prove exact consent transitions: content changes stay clean/informational, while path/trust/publisher changes require new consent, even without a native config edit.
@pytest.mark.parametrize("change", ["same", "content", "path", "trust", "publisher"])
def test_consent_and_status_transition_table(app_cli, monkeypatch, capsys, change):
    root, home, helper, _ = app_cli
    prompts = approve(monkeypatch)
    assert install() == 0
    capsys.readouterr()
    original = launchers.resolve_launcher(mcp_project.declarations.parse_mcp_servers(declaration(), invocation_scope="project", target_agents=["claude_code"])["app"].server.launcher)
    current = {"same": original, "content": replace(original, content_sha256="a" * 64),
               "path": replace(original, path=str(helper.parent / "moved-server")),
               "trust": replace(original, trust="signed-user-writable"),
               "publisher": replace(original, publisher="different-publisher")}[change]
    monkeypatch.setattr(launchers, "resolve_launcher", lambda *a, **kw: current)
    before = _snapshot(root, home)
    expected = "updated (informational)" if change == "content" else "current" if change == "same" else "unapproved"
    assert status(capsys, "--check") == (0 if change in {"same", "content"} else 1, [expected] * 3)
    assert _snapshot(root, home) == before
    if change in {"path", "trust", "publisher"}:
        with monkeypatch.context() as patch:
            patch.setattr(sys.stdin, "isatty", lambda: False)
            assert install() == 1
            assert "mcp_plan_unapproved" in capsys.readouterr().err
            assert _snapshot(root, home) == before
    assert install() == 0
    capsys.readouterr()
    assert len(prompts) == (1 if change in {"same", "content"} else 2)
    assert status(capsys, "--check") == (0, ["current"] * 3)


# Signature failure after an approved install reports untrusted with check=1; status never rewrites configs, ledger or consent.
def test_status_reverification_untrusted(app_cli, monkeypatch, capsys):
    root, home, _, _ = app_cli
    approve(monkeypatch)
    assert install() == 0
    capsys.readouterr()
    before = _snapshot(root, home)
    monkeypatch.setattr(launchers, "verify_macos", lambda *a: replace(good_verification(), notarized=False))
    assert status(capsys, "--check") == (1, ["untrusted"] * 3)
    assert _snapshot(root, home) == before
    assert install() == 1
    assert "mcp_launcher_untrusted" in capsys.readouterr().err
    assert _snapshot(root, home) == before


# Replacing a verified application between planning and staging refuses before any transaction targets are staged; restoring it permits staging again.
def test_app_change_since_plan_refuses_staging(app_cli, tmp_path, monkeypatch):
    root, home, helper, _ = app_cli
    plan = mcp_project.plan_project(root, ["claude_code", "codex_cli", "cursor"], declaration(), home)
    staging = tmp_path / "staging"
    staging.mkdir()
    before = _snapshot(root, home)
    original = helper.read_bytes()
    helper.write_bytes(b"updated signed binary fixture")
    with pytest.raises(mcp_project.McpProjectError, match="mcp_launcher_changed"):
        mcp_project.stage_targets(plan, staging, "test")
    assert list(staging.iterdir()) == []
    assert _snapshot(root, home) == before
    helper.write_bytes(original)
    assert mcp_project.stage_targets(plan, staging, "test")


# The consent hash binds each identity component, excludes content, and rejects partial identity tuples rather than approving them as legacy subjects.
def test_consent_subject_identity_contract():
    consumer = ownership.consumer_id("/project", "project")
    subject = ownership.ConsentSubject("app", ("claude_code",), "project", "a" * 64, ("test",), "/Applications/App.app/Contents/MacOS/helper", "verified", "publisher")
    key = ownership.consent_key(consumer, subject)
    assert ownership.consent_key(consumer, replace(subject)) == key
    for changes in [{"resolved_path": "/new/path"}, {"trust_level": "unverified"}, {"publisher": "new-publisher"}]:
        assert ownership.consent_key(consumer, replace(subject, **changes)) != key
    with pytest.raises(ownership.McpOwnershipError) as caught:
        ownership.consent_key(consumer, replace(subject, publisher=None))
    assert caught.value.code == "mcp_declaration_invalid"


# Consent may carry forward to a higher trust level with identical path/publisher, but never from higher trust to a downgrade.
def test_trust_upgrade_preserves_consent_but_downgrade_does_not():
    consumer = ownership.consumer_id("/project", "project")
    subject = ownership.ConsentSubject("app", ("claude_code",), "project", "a" * 64, ("test",), "/app/helper", "unverified", "publisher")
    for old, new in [("unverified", "signed-user-writable"), ("signed-user-writable", "verified"), ("unverified", "verified")]:
        approved = frozenset({ownership.consent_key(consumer, replace(subject, trust_level=old))})
        assert ownership.unapproved(consumer, [replace(subject, trust_level=new)], approved) == ()
        approved = frozenset({ownership.consent_key(consumer, replace(subject, trust_level=new))})
        lower = replace(subject, trust_level=old)
        assert ownership.unapproved(consumer, [lower], approved) == (lower,)
