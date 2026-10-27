"""Canonical selection and separately approved operator-local targets."""

import json
import sys
from pathlib import Path

import pytest

from csk import cli, manifest, mcp_declarations as d, mcp_project
from test_mcp_project_install import project_cli, install, native_bytes
from test_mcp_launcher_lifecycle import approve
from test_mcp_install_acceptance import _snapshot

URL = "https://mcp.example.invalid/mcp"


def declaration(targets=None):
    server = {"transport": "http", "url": URL}
    if targets is not None:
        server["environments"] = targets
    return {"docs": {"server": server}}


# Unknown/empty/duplicate selectors refuse the shared parser; each canonical neighbor parses.
@pytest.mark.parametrize(
    "targets", [["gemini"], ["windsurf"], ["cursor"], [], ["pi", "pi"]]
)
def test_shared_vocabulary(targets):
    with pytest.raises(d.McpDeclarationError) as error:
        d.parse_mcp_servers(
            declaration(targets),
            invocation_scope="project",
            target_agents=["claude_code"],
        )
    assert error.value.code == "mcp_declaration_invalid"
    for agent in ("claude_code", "codex_cli", "pi", "muse", "opencode"):
        parsed = d.parse_mcp_servers(
            declaration([agent]), invocation_scope="project", target_agents=[]
        )
        assert parsed["docs"].agents == ((agent,) if agent in d.SLICE1_AGENTS else ())


# Absence survives normalized identity and selects all writers; explicit selection never widens.
def test_absence_identity_and_explicit_control():
    absent = d.parse_mcp_servers(
        declaration(), invocation_scope="project", target_agents=["claude_code"]
    )["docs"]
    assert set(absent.agents) == set(d.SLICE1_AGENTS)
    assert absent.environments is None
    explicit = d.parse_mcp_servers(
        declaration(["claude_code"]), invocation_scope="project", target_agents=[]
    )["docs"]
    assert explicit.agents == ("claude_code",)
    assert d.declaration_digest(absent) != d.declaration_digest(explicit)


# A new local target refuses headlessly without writes; approval adds Cursor without editing the shared selection.
def test_local_target_is_separate_and_requires_consent(
    project_cli, monkeypatch, capsys
):
    root, home, declare = project_cli
    declare(declaration(["claude_code"]), agents=["claude_code"])
    assert install() == 0
    capsys.readouterr()
    original = (root / "Skillfile.json").read_bytes()
    argv = [
        "mcp",
        "local-target",
        "add",
        "cursor",
        "--server",
        "docs",
        "--project",
        str(root),
    ]
    before = _snapshot(root, home)
    monkeypatch.setattr(sys.stdin, "isatty", lambda: False)
    assert cli.main(argv) == 1
    assert "mcp_plan_unapproved" in capsys.readouterr().err
    assert _snapshot(root, home) == before
    approve(monkeypatch)
    assert cli.main(argv) == 0
    capsys.readouterr()
    assert install() == 0
    assert "local: cursor" in capsys.readouterr().out
    assert set(native_bytes(root)) == {".mcp.json", ".cursor/mcp.json"}
    assert (root / "Skillfile.json").read_bytes() == original


# Local diagnostic stays distinct from shared invalidity; the valid list command remains read-only.
def test_local_invalid_target(project_cli, capsys):
    root, home, _ = project_cli
    before = _snapshot(root, home)
    assert cli.main(["mcp", "local-target", "add", "pi", "--project", str(root)]) == 1
    assert "mcp_local_target_invalid" in capsys.readouterr().err
    assert _snapshot(root, home) == before
    assert (
        cli.main(["mcp", "local-target", "list", "cursor", "--project", str(root)]) == 0
    )
    assert _snapshot(root, home) == before


# Cursor-only migration needs a canonical decision; mixed migration edits only the legacy array member.
@pytest.mark.parametrize("targets", [["cursor"], ["claude_code", "cursor"]])
def test_lossless_migration(project_cli, monkeypatch, capsys, targets):
    root, home, declare = project_cli
    declare(declaration(targets))
    old = (root / "Skillfile.json").read_bytes()
    argv = ["mcp", "migrate", "--project", str(root)]
    before = _snapshot(root, home)
    if targets == ["cursor"]:
        assert cli.main(argv) == 1
        assert "--targets" in capsys.readouterr().err
        assert _snapshot(root, home) == before
        argv += ["--targets", "claude_code"]
    approve(monkeypatch)
    assert cli.main(argv) == 0
    capsys.readouterr()
    data = json.loads((root / "Skillfile.json").read_bytes())
    assert data["mcp_servers"]["docs"]["server"]["environments"] == ["claude_code"]
    assert (
        data["ignore_mode"] == "gitignore"
    )  # Other deprecated form is migration-exempt.
    if targets != ["cursor"]:
        assert (root / "Skillfile.json").read_bytes() == old.replace(
            b',\n          "cursor"', b""
        )
    assert (
        cli.main(["mcp", "local-target", "list", "cursor", "--project", str(root)]) == 0
    )
    assert "docs" in capsys.readouterr().out


# Each deprecated form independently refuses ordinary writers with its migration command; a clean neighbor reaches normal validation.
@pytest.mark.parametrize("legacy", ["cursor", "ignore_mode", "both"])
def test_legacy_writer_refusal(project_cli, capsys, legacy):
    root, home, declare = project_cli
    declare(
        declaration(
            ["claude_code"] if legacy == "ignore_mode" else ["claude_code", "cursor"]
        )
    )
    if legacy == "cursor":
        data = json.loads((root / "Skillfile.json").read_bytes())
        data.pop("ignore_mode")
        (root / "Skillfile.json").write_text(json.dumps(data))
    before = _snapshot(root, home)
    assert cli.main(["remove", "missing", "--project", str(root)]) == 2
    error = capsys.readouterr().err
    assert "skillfile_deprecated_form" in error
    if legacy != "ignore_mode":
        assert "csk mcp migrate" in error
    if legacy != "cursor":
        assert "csk ignore-mode set <exclude|gitignore>" in error
    assert _snapshot(root, home) == before
    declare(declaration(["claude_code"]))
    data = json.loads((root / "Skillfile.json").read_bytes())
    data.pop("ignore_mode")
    (root / "Skillfile.json").write_text(json.dumps(data))
    with pytest.raises(manifest.ManifestError, match="Skill not declared"):
        manifest.remove_skill_decl(root, "missing")


# Unsupported canonical pairs remain visible with null paths and never make status drift.
@pytest.mark.parametrize("agent", ["pi", "muse", "opencode"])
def test_unsupported_pair(project_cli, capsys, agent):
    root, home, declare = project_cli
    declare(declaration([agent]))
    assert install() == 0
    output = capsys.readouterr().out
    assert (
        f"{agent}" in output
        and "skipped: unsupported" in output
        and "path: unavailable" in output
    )
    assert native_bytes(root) == {}
    assert cli.main(["status", "app", "--json", "--check"]) == 0
    payload = json.loads(capsys.readouterr().out)[0]["mcp"]
    assert len(payload) == 1 and payload[0]["path"] is None
    assert payload[0]["state"] == "skipped: unsupported"


# Server A's grant cannot approve B; removing A revokes only A and readding it requires fresh approval.
def test_local_grants_are_independent(project_cli, monkeypatch, capsys):
    root, home, declare = project_cli
    raw = declaration(["claude_code"])
    raw["other"] = raw["docs"]
    declare(raw)
    prompts = approve(monkeypatch)

    def command(action, server):
        return [
            "mcp",
            "local-target",
            action,
            "cursor",
            "--server",
            server,
            "--project",
            str(root),
        ]

    assert cli.main(command("add", "docs")) == 0
    assert len(prompts) == 1
    before = _snapshot(root, home)
    with monkeypatch.context() as patch:
        patch.setattr(sys.stdin, "isatty", lambda: False)
        assert cli.main(command("add", "other")) == 1
        assert _snapshot(root, home) == before
    assert cli.main(command("add", "other")) == 0
    assert len(prompts) == 2
    assert cli.main(command("remove", "docs")) == 0
    before = _snapshot(root, home)
    with monkeypatch.context() as patch:
        patch.setattr(sys.stdin, "isatty", lambda: False)
        assert cli.main(command("add", "docs")) == 1
        assert _snapshot(root, home) == before
    assert cli.main(command("add", "other")) == 0
    assert len(prompts) == 2


# A transaction failure between migration writes restores both files; the valid retry removes only Cursor.
def test_migration_failure_rolls_back(project_cli, monkeypatch, capsys):
    from csk import transactions

    root, home, declare = project_cli
    declare(declaration(["claude_code", "cursor"]))
    approve(monkeypatch)
    original = transactions.TransactionEngine

    def fail(event, target):
        if event == "after_install" and target.identifier == "local-targets":
            raise OSError("injected migration failure")

    before = (root / "Skillfile.json").read_bytes()
    with monkeypatch.context() as patch:
        patch.setattr(
            transactions,
            "TransactionEngine",
            lambda *a, **kw: original(*a, **kw, fault_hook=fail),
        )
        assert cli.main(["mcp", "migrate", "--project", str(root)]) == 1
    assert (root / "Skillfile.json").read_bytes() == before
    from csk import mcp_local_targets

    assert mcp_local_targets.read(root, home) == {}
    assert native_bytes(root) == {}
    assert cli.main(["mcp", "migrate", "--project", str(root)]) == 0


# A previous narrow consent never approves newly writable destinations; unchanged old pairs retain authority.
def test_absent_old_grant_does_not_widen(project_cli, monkeypatch, capsys):
    from dataclasses import replace
    from csk import mcp_ownership as o, mcp_native

    root, home, declare = project_cli
    raw = {"docs": {"server": {"transport": "stdio", "command": "example-helper"}}}
    declare(raw, agents=["claude_code"])
    parsed = d.parse_mcp_servers(
        raw, invocation_scope="project", target_agents=["claude_code"]
    )["docs"]
    consumer = o.consumer_id(str(root.resolve()), "project")
    # Reconstruct exactly the 0.21.0 declaration/subject hash, before absence was preserved.
    old = replace(parsed, agents=("claude_code",), environments=("claude_code",))
    old_subject = o.ConsentSubject(
        "docs",
        ("claude_code",),
        "project",
        d.declaration_digest(old),
        (mcp_native.project_surface("claude_code").capability,),
    )
    o.record_consent(home, consumer, [o.consent_key(consumer, old_subject)])
    plan = mcp_project.plan_project(root, ["claude_code"], raw, home)
    assert {agent for subject in plan.pending for agent in subject.agents} == {
        "codex_cli",
        "cursor",
    }
    before = _snapshot(root, home)
    monkeypatch.setattr(sys.stdin, "isatty", lambda: False)
    assert install() == 1
    assert _snapshot(root, home) == before
    prompts = approve(monkeypatch)
    assert install() == 0
    assert len(prompts) == 2


# A separate invalid URL keeps its independently known unsupported pair in both real and dry-run refusals.
@pytest.mark.parametrize("dry", [False, True])
def test_unsupported_survives_independent_refusal(project_cli, capsys, dry):
    root, home, declare = project_cli
    raw = declaration(["pi"])
    raw["bad"] = {"server": {"transport": "http", "url": "http://invalid.example"}}
    declare(raw)
    before = _snapshot(root, home)
    assert install(*(["--dry-run"] if dry else [])) != 0
    out = capsys.readouterr()
    text = out.out + out.err
    assert text.count("mcp docs -> pi path: unavailable (skipped: unsupported)") == 1
    assert "mcp_url_invalid" in text
    assert _snapshot(root, home) == before
    declare(declaration(["pi"]))
    assert install(*(["--dry-run"] if dry else [])) == 0


# Duplicate keys and a symlink cannot be migrated; the regular mixed input is the adjacent valid control.
@pytest.mark.parametrize("bad", ["duplicate", "symlink"])
def test_unsafe_migration_input(project_cli, monkeypatch, capsys, bad):
    root, home, declare = project_cli
    declare(declaration(["claude_code", "cursor"]))
    path = root / "Skillfile.json"
    original = path.read_bytes()
    if bad == "duplicate":
        path.write_bytes(
            original.replace(
                b'"schema_version": 3', b'"schema_version": 3, "schema_version": 3'
            )
        )
    else:
        other = root / "other.json"
        path.rename(other)
        path.symlink_to(other.name)
    before = _snapshot(root, home)
    approve(monkeypatch)
    assert cli.main(["mcp", "migrate", "--project", str(root)]) != 0
    assert _snapshot(root, home) == before
    if bad == "symlink":
        path.unlink()
        (root / "other.json").rename(path)
    else:
        path.write_bytes(original)
    assert cli.main(["mcp", "migrate", "--project", str(root)]) == 0


# JSON status keeps one warning per deprecated form on stderr, and warnings alone do not fail its check.
def test_legacy_warning_is_not_json_or_drift(project_cli, capsys):
    root, home, declare = project_cli
    declare(declaration(["claude_code", "cursor"]))
    assert install() == 0
    capsys.readouterr()
    assert cli.main(["status", "app", "--json", "--check"]) == 0
    output = capsys.readouterr()
    assert output.err.count("warning: cursor ") == 1
    assert output.err.count("warning: ignore_mode ") == 1
    assert "not portable" not in output.out
    assert json.loads(output.out)[0]["clean"]


# Init and refresh refuse deprecated input before writes; a clean file is an executable positive control.
@pytest.mark.parametrize("command", ["init", "update", "upgrade"])
def test_normal_writer_commands(project_cli, capsys, command):
    root, home, declare = project_cli
    declare(declaration(["claude_code", "cursor"]))
    args = [command, str(root)] if command != "update" else [command]
    before = _snapshot(root, home)
    assert cli.main(args) == 2
    assert "skillfile_deprecated_form" in capsys.readouterr().err
    assert _snapshot(root, home) == before
    declare(declaration(["claude_code"]))
    raw = json.loads((root / "Skillfile.json").read_bytes())
    raw.pop("ignore_mode")
    (root / "Skillfile.json").write_text(json.dumps(raw))
    assert cli.main(args) == 0


# Global local-target preferences cannot authorize a future server or reuse the per-server grant.
def test_project_target_does_not_approve_future_server(
    project_cli, monkeypatch, capsys
):
    root, home, declare = project_cli
    declare(declaration(["claude_code"]))
    prompts = approve(monkeypatch)
    argv = ["mcp", "local-target", "add", "cursor", "--project", str(root)]
    assert cli.main(argv) == 0
    assert len(prompts) == 1
    assert install() == 0
    raw = declaration(["claude_code"])
    raw["other"] = raw["docs"]
    declare(raw)
    before = _snapshot(root, home)
    with monkeypatch.context() as patch:
        patch.setattr(sys.stdin, "isatty", lambda: False)
        assert install() != 0
        assert _snapshot(root, home) == before
    assert install() == 0
    assert len(prompts) == 2


# Local settings are rebound at publication: a changed config invalidates an otherwise valid staged plan.
def test_local_configuration_change_invalidates_plan(
    project_cli, monkeypatch, tmp_path
):
    from csk import mcp_local_targets as local

    root, home, declare = project_cli
    declare(declaration(["claude_code"]), agents=["claude_code"])
    plan = mcp_project.plan_project(
        root, ["claude_code"], declaration(["claude_code"]), home
    )
    approve(monkeypatch)
    assert (
        cli.main(
            [
                "mcp",
                "local-target",
                "add",
                "cursor",
                "--server",
                "docs",
                "--project",
                str(root),
            ]
        )
        == 0
    )
    staging = tmp_path / "staging"
    staging.mkdir()
    with pytest.raises(mcp_project.McpProjectError, match="mcp_local_target_invalid"):
        mcp_project.stage_targets(plan, staging, "test")
    assert not list(staging.iterdir())
    refreshed = mcp_project.plan_project(
        root, ["claude_code"], declaration(["claude_code"]), home
    )
    assert mcp_project.stage_targets(refreshed, staging, "test")


# Corrupt or linked local state refuses with a local diagnostic and no writes; a valid empty store remains usable.
@pytest.mark.parametrize("bad", ["json", "shape", "directory", "symlink"])
def test_invalid_local_store(project_cli, capsys, bad):
    from csk import mcp_local_targets as local

    root, home, declare = project_cli
    declare(declaration(["claude_code"]))
    local.ensure_directory(home)
    path = local.path(root, home)
    if bad == "json":
        path.write_bytes(b"{broken")
    elif bad == "shape":
        path.write_bytes(b'{"targets": {}}')
    elif bad == "directory":
        path.mkdir()
    else:
        other = home / "foreign-local-store"
        other.write_bytes(local.encode(root, {}))
        path.symlink_to(other)
    before = _snapshot(root, home)
    args = ["mcp", "local-target", "list", "cursor", "--project", str(root)]
    assert cli.main(args) == 1
    assert "mcp_local_target_invalid" in capsys.readouterr().err
    assert install() != 0
    text = capsys.readouterr()
    assert "mcp_local_target_invalid" in text.err + text.out
    assert cli.main(["status", "app", "--check"]) != 0
    text = capsys.readouterr()
    assert "mcp_local_target_invalid" in text.out + text.err
    assert _snapshot(root, home) == before
    if bad == "directory":
        path.rmdir()
    else:
        path.unlink()
    path.write_bytes(local.encode(root, {}))
    assert cli.main(args) == 0
    assert install() == 0


# Skill agents never narrow an absent MCP selector; an explicit supported selector is the bounded positive control.
def test_absent_writes_all_and_explicit_narrows(project_cli):
    root, home, declare = project_cli
    declare(declaration(), agents=["claude_code"])
    assert install() == 0
    assert set(native_bytes(root)) == {
        ".mcp.json",
        ".codex/config.toml",
        ".cursor/mcp.json",
    }
    declare(declaration(["claude_code"]), agents=["claude_code"])
    assert install() == 0
    assert set(native_bytes(root)) == {".mcp.json"}


# Hard death after either migration publication is recoverable to both original files, then the same migration succeeds.
@pytest.mark.parametrize("identifier", ["local-targets", "skillfile"])
def test_migration_process_death(project_cli, monkeypatch, tmp_path, identifier):
    import subprocess
    from csk import mcp_local_targets as local

    root, home, declare = project_cli
    declare(declaration(["claude_code", "cursor"]))
    before = (root / "Skillfile.json").read_bytes()
    script = """
import os, sys
from csk import cli, transactions
original = transactions.TransactionEngine
def factory(*a, **kw):
    def fail(event, target):
        if event == 'after_install' and target.identifier == sys.argv[2]:
            os._exit(86)
    return original(*a, **kw, fault_hook=fail)
transactions.TransactionEngine = factory
sys.stdin.isatty = lambda: True
sys.stdout.isatty = lambda: True
import builtins
builtins.input = lambda prompt: 'yes'
sys.exit(cli.main(['mcp', 'migrate', '--project', sys.argv[1]]))
"""
    result = subprocess.run(
        [sys.executable, "-c", script, str(root), identifier],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 86, result.stderr
    journals = list((home / "state/transactions/v1").glob("*.json"))
    assert len(journals) == 1
    assert cli.main(["recover", "--rollback", journals[0].stem]) == 0
    assert (root / "Skillfile.json").read_bytes() == before
    assert local.read(root, home) == {}
    assert native_bytes(root) == {}
    approve(monkeypatch)
    assert cli.main(["mcp", "migrate", "--project", str(root)]) == 0


# Status retains a selected unsupported row alongside a separate invalid declaration, with null path/code; removing the invalid neighbor is clean.
def test_status_unsupported_survives_refusal(project_cli, capsys):
    root, home, declare = project_cli
    raw = declaration(["pi"])
    raw["bad"] = {"server": {"transport": "http", "url": "http://invalid.example"}}
    declare(raw)
    before = _snapshot(root, home)
    assert cli.main(["status", "app", "--json", "--check"]) == 1
    payload = json.loads(capsys.readouterr().out)[0]
    assert payload["mcp"] == [
        {
            "server": "docs",
            "agent": "pi",
            "path": None,
            "state": "skipped: unsupported",
            "outcome": "skipped: unsupported",
            "code": None,
        }
    ]
    assert any("mcp_url_invalid" in error for error in payload["errors"])
    assert _snapshot(root, home) == before
    declare(declaration(["pi"]))
    assert install() == 0
    capsys.readouterr()
    assert cli.main(["status", "app", "--json", "--check"]) == 0


# Local consent discloses argv and env names before headless refusal, never env values; explicit approval records only this target.
def test_local_consent_discloses_effective_plan(project_cli, monkeypatch, capsys):
    root, home, declare = project_cli
    declare(
        {
            "docs": {
                "server": {
                    "transport": "stdio",
                    "command": "example-helper",
                    "args": ["--serve"],
                    "env_names": ["TOOL_TOKEN"],
                    "environments": ["claude_code"],
                }
            }
        }
    )
    monkeypatch.setenv("TOOL_TOKEN", "fixture-secret-must-not-be-disclosed")
    args = [
        "mcp",
        "local-target",
        "add",
        "cursor",
        "--server",
        "docs",
        "--project",
        str(root),
    ]
    monkeypatch.setattr(sys.stdin, "isatty", lambda: False)
    before = _snapshot(root, home)
    assert cli.main(args) == 1
    output = capsys.readouterr()
    assert '["example-helper", "--serve"]' in output.out
    assert "agents=cursor; env=TOOL_TOKEN" in output.out
    assert "fixture-secret-must-not-be-disclosed" not in output.out + output.err
    assert _snapshot(root, home) == before
    prompts = approve(monkeypatch)
    assert cli.main(args) == 0
    assert len(prompts) == 1
    output = capsys.readouterr()
    assert '["example-helper", "--serve"]' in output.out
    assert "fixture-secret-must-not-be-disclosed" not in output.out + output.err
