"""Project MCP installation through the public CLI and its transaction seams."""

from __future__ import annotations

import json
import os
import stat
import sys
import tomllib
from pathlib import Path

import pytest
from conftest import make_config, make_project, write_files, write_skillfile

from csk import (
    cli,
    config,
    locking,
    mcp_declarations,
    mcp_native,
    mcp_ownership,
    mcp_project,
)
from csk.sources import publish
import test_mcp_install_acceptance as acceptance

URL = "https://mcp.example.invalid/mcp"
AGENTS = ["claude_code", "codex_cli", "cursor"]
PATHS = [".mcp.json", ".codex/config.toml", ".cursor/mcp.json"]
pytestmark = pytest.mark.usefixtures("stable_env")


@pytest.fixture
def project_cli(tmp_path, csk_home, skills_root, monkeypatch):
    root = make_project(tmp_path)
    cfg = make_config(csk_home, skills_root, root, agents=AGENTS)
    config.save_config(cfg)
    monkeypatch.setenv("CSK_CONFIG", str(cfg.path))

    def declare(servers=None, agents=None, skills=None, schema=3):
        data = {
            "schema_version": schema,
            "agents": agents or AGENTS,
            "skills": skills or [],
        }
        if servers is not None:
            data["mcp_servers"] = servers
        if skills:
            data["sources"] = {"local": {"path": "."}}
        write_skillfile(root, data)

    declare({"docs": URL})
    return root, csk_home, declare


def install(*args):
    return cli.main(["install", "app", "--fix-gitignore", *args])


def native_bytes(root):
    return {
        path: (root / path).read_bytes() for path in PATHS if (root / path).exists()
    }


# Owned removal pairs survive codec/unreadable refusals in install and dry-run; repairing the surface permits the same removal.
@pytest.mark.parametrize("change", ["remove", "deselect"])
@pytest.mark.parametrize("surface_state", ["inline", "unreadable"])
@pytest.mark.parametrize("dry_run", [False, True])
def test_owned_removal_refusal_has_total_rows(tmp_path, change, surface_state, dry_run):
    case = acceptance._case(tmp_path / "owned-removal")
    case.declare({"docs": URL})
    acceptance._assert_success(case.run("--fix-gitignore"))
    path = case.project / PATHS[1]
    original = path.read_bytes()
    if surface_state == "inline":
        path.write_text("mcp_servers = { docs = { url = " + json.dumps(URL) + " } }\n")
        code = "mcp_native_config_unsupported_syntax"
    else:
        path.unlink()
        path.mkdir()
        (path / "keep.txt").write_text("foreign directory content")
        code = "mcp_native_config_unreadable"
    if change == "remove":
        case.declare({})
    else:
        case.skillfile["agents"] = ["claude_code", "cursor"]
        case.declare({"docs": URL})
    before = acceptance._snapshot(case.project, case.home)
    result = case.run(*(["--dry-run"] if dry_run else []))
    assert result.returncode != 0, result.stdout
    assert code in result.stdout
    assert acceptance._snapshot(case.project, case.home) == before
    rows = acceptance._expected_rows(f"refused: {code}", dry_run=dry_run)
    assert acceptance._rows(result, dry_run=dry_run) == rows, result.stdout
    lines = result.stdout.splitlines()
    summary = next(
        i for i, line in enumerate(lines) if code in line and line not in rows
    )
    assert max(lines.index(row) for row in rows) < summary
    acceptance._assert_seed_installed(case)

    if surface_state == "unreadable":
        (path / "keep.txt").unlink()
        path.rmdir()
    path.write_bytes(original)
    repaired = case.run()
    acceptance._assert_success(repaired)
    expected = [
        f"mcp docs -> {agent} {relpath} ({'removed' if change == 'remove' or agent == 'codex_cli' else 'unchanged'})"
        for agent, relpath in zip(AGENTS, PATHS)
    ]
    assert acceptance._rows(repaired) == expected
    assert not path.exists()
    acceptance._assert_seed_installed(case)


# Early declaration refusal reports new desired pairs and old owned pairs once, with no publication.
def test_invalid_declaration_reports_owned_removal_pairs(tmp_path):
    case = acceptance._case(tmp_path / "invalid-removal")
    case.declare({"docs": URL})
    acceptance._assert_success(case.run("--fix-gitignore"))
    case.skillfile["agents"] = ["cursor"]
    case.declare({"other": "http://example.invalid"})
    before = acceptance._snapshot(case.project, case.home)
    result = case.run()
    assert result.returncode != 0
    assert "mcp_url_invalid" in result.stdout
    assert acceptance._snapshot(case.project, case.home) == before
    assert acceptance._rows(result) == [
        *acceptance._expected_rows("refused: mcp_url_invalid"),
        "mcp other -> cursor .cursor/mcp.json (refused: mcp_url_invalid)",
    ], result.stdout


# A plain URL installs three precise native shapes headlessly, reports them, and repeats without byte changes.
def test_url_install_and_repeat(project_cli, capsys):
    root, home, _ = project_cli
    assert install() == 0
    output = capsys.readouterr().out
    for agent, path in zip(AGENTS, PATHS):
        assert f"mcp docs -> {agent} {path} (added)" in output
        assert "/" + path in (root / ".gitignore").read_text()
    assert "MCP configuration installed. Authentication not checked." in output
    assert json.loads((root / PATHS[0]).read_bytes())["mcpServers"]["docs"] == {
        "type": "http",
        "url": URL,
    }
    assert tomllib.loads((root / PATHS[1]).read_text())["mcp_servers"]["docs"] == {
        "url": URL
    }
    assert json.loads((root / PATHS[2]).read_bytes())["mcpServers"]["docs"] == {
        "url": URL
    }
    before = native_bytes(root)
    assert not list((home / "state/mcp/consent").glob("*.json"))
    assert install() == 0
    assert capsys.readouterr().out.count("(unchanged)") == 3
    assert native_bytes(root) == before
    assert not list(root.rglob(".csk-txn-*"))


# Invalid URL/type declarations refuse before native, skill, lock, ledger or gitignore publication.
@pytest.mark.parametrize(
    "value,code",
    [
        ("http://example.invalid", "mcp_url_invalid"),
        ("https://u:p@example.invalid", "mcp_url_invalid"),
        (URL + "?x=1", "mcp_url_invalid"),
        (URL + "#f", "mcp_url_invalid"),
        ("https://é.invalid", "mcp_url_invalid"),
        ("", "mcp_url_invalid"),
        (3, "mcp_declaration_invalid"),
        ([], "mcp_declaration_invalid"),
    ],
)
def test_bad_shorthand_is_atomic(project_cli, capsys, value, code):
    root, home, declare = project_cli
    write_files(
        root,
        {
            "skills/demo/SKILL.md": "---\nname: demo\ndescription: Example\n---\n\nExample\n"
        },
    )
    declare(
        {"docs": value},
        skills=[{"name": "demo", "from": "local", "directory": "skills/demo"}],
    )
    before = (root / ".gitignore").read_bytes()
    assert install() != 0
    assert code in capsys.readouterr().err
    assert native_bytes(root) == {}
    assert not (root / ".agents/skills/demo").exists()
    assert not (root / "Skillfile.lock.json").exists()
    assert not list((home / "state/mcp/ledger").glob("*.json"))
    assert (root / ".gitignore").read_bytes() == before


# Removal and agent deselection delete only owned entries, including newly empty csk-created files.
def test_removal_and_deselection(project_cli, capsys):
    root, _, declare = project_cli
    assert install() == 0
    declare({"docs": URL}, agents=["cursor"])
    assert install() == 0
    assert not (root / PATHS[0]).exists() and not (root / PATHS[1]).exists()
    assert (root / PATHS[2]).exists()
    declare({})
    assert install() == 0
    assert native_bytes(root) == {}
    assert "(removed)" in capsys.readouterr().out


# User-modified owned entries are skipped while declared, restored if deleted, and released on removal.
def test_drift_restore_release(project_cli, capsys):
    root, _, declare = project_cli
    assert install() == 0
    path = root / PATHS[0]
    path.write_text(
        json.dumps(
            {"mcpServers": {"docs": {"type": "http", "url": URL, "enabled": False}}}
        )
    )
    changed = path.read_bytes()
    assert install() == 0
    assert "(skipped: drift)" in capsys.readouterr().out
    assert path.read_bytes() == changed
    path.unlink()
    assert install() == 0
    assert "(restored)" in capsys.readouterr().out
    path.write_bytes(changed)
    declare({})
    assert install() == 0
    assert "(released)" in capsys.readouterr().out
    assert path.read_bytes() == changed


# Equal foreign entries still conflict; malformed, linked and oversize surfaces also block the whole batch.
@pytest.mark.parametrize(
    "kind,code",
    [
        ("foreign", "mcp_entry_conflict"),
        ("malformed", "mcp_native_config_unsupported_syntax"),
        ("link", "mcp_native_config_unreadable"),
        ("large", "mcp_native_config_too_large"),
    ],
)
def test_native_refusals(project_cli, capsys, kind, code):
    root, home, _ = project_cli
    path = root / PATHS[0]
    if kind == "foreign":
        path.write_text(
            json.dumps({"mcpServers": {"docs": {"type": "http", "url": URL}}})
        )
    elif kind == "malformed":
        path.write_bytes(b'{"mcpServers":{},"mcpServers":{}}')
    elif kind == "large":
        path.write_bytes(b" " * (4 * 1024 * 1024 + 1))
    else:
        destination = home / "foreign.json"
        destination.write_text("{}")
        path.symlink_to(destination)
    before = path.read_bytes()
    assert install() != 0
    err = capsys.readouterr().err
    assert code in err
    assert "Remedy:" in err
    expected_rows = []
    for index, (agent, relpath) in enumerate(zip(AGENTS, PATHS)):
        action = (
            "conflict: foreign entry"
            if kind == "foreign" and index == 0
            else f"refused: {code}"
        )
        expected_rows.append(f"mcp docs -> {agent} {relpath} ({action})")
    assert [
        line for line in err.splitlines() if line.startswith("mcp ")
    ] == expected_rows
    assert path.read_bytes() == before
    assert not (root / PATHS[1]).exists()
    assert not (root / PATHS[2]).exists()


# Dry-run displays required consent without writing or approving; project-local forged approval is ignored.
@pytest.mark.parametrize(
    "declaration",
    [
        {"server": {"transport": "stdio", "command": "my-server", "args": []}},
        {
            "server": {"transport": "http", "url": URL},
            "install": {
                "headers": {"Authorization": {"env": "DOCS_TOKEN", "prefix": "bearer"}}
            },
        },
    ],
)
def test_headless_consent_and_dry_run(project_cli, capsys, declaration):
    root, home, declare = project_cli
    declare({"docs": declaration})
    (root / "approval.json").write_text('{"approved":true}')
    assert install("--dry-run") == 0
    out = capsys.readouterr().out
    assert "would: mcp docs approval: required" in out
    assert native_bytes(root) == {}
    assert not (root / "Skillfile.lock.json").exists()
    assert install() != 0
    assert "mcp_plan_unapproved" in capsys.readouterr().err
    assert not list((home / "state/mcp/consent").glob("*.json"))
    assert native_bytes(root) == {}


# Only affirmative interactive approval records exact-plan consent; rejection leaves all targets absent.
@pytest.mark.parametrize("answer", ["n", "yes"])
def test_interactive_consent(project_cli, capsys, monkeypatch, answer):
    root, home, declare = project_cli
    declare(
        {
            "tools": {
                "server": {"transport": "stdio", "command": "my-server", "args": []}
            }
        }
    )
    monkeypatch.setattr(sys.stdin, "isatty", lambda: True)
    monkeypatch.setattr(sys.stdout, "isatty", lambda: True)
    prompts = []
    monkeypatch.setattr(
        "builtins.input", lambda prompt: prompts.append(prompt) or answer
    )
    code = install()
    assert (
        len(prompts) == 1
        and "Approve 1 MCP server(s) for app? [y/N]" == prompts[0].strip()
    )
    # Unknown stdio programs disclose their limit alongside the exact argv.
    output = capsys.readouterr().out
    assert '["my-server"]' in output
    assert "csk cannot verify what this program runs." in output
    if answer == "n":
        assert code != 0 and native_bytes(root) == {}
        assert not list((home / "state/mcp/consent").glob("*.json"))
    else:
        assert code == 0
        monkeypatch.setattr(sys.stdin, "isatty", lambda: False)
        assert install() == 0
        assert len(prompts) == 1
        assert list((home / "state/mcp/consent").glob("*.json"))


# URL and expanded object normalize to identical declaration identities; old public shape is rejected.
def test_normalization_and_closed_object():
    short = mcp_declarations.parse_mcp_servers(
        {"docs": URL}, invocation_scope="project", target_agents=AGENTS
    )["docs"]
    expanded = mcp_declarations.parse_mcp_servers(
        {"docs": {"server": {"transport": "http", "url": URL}}},
        invocation_scope="project",
        target_agents=AGENTS,
    )["docs"]
    assert mcp_declarations.declaration_digest(
        short
    ) == mcp_declarations.declaration_digest(expanded)
    with pytest.raises(
        mcp_declarations.McpDeclarationError, match="mcp_declaration_invalid"
    ):
        mcp_declarations.parse_mcp_servers(
            {
                "docs": {
                    "server": {"transport": "http", "url": URL},
                    "auth": {"kind": "none"},
                }
            },
            invocation_scope="project",
            target_agents=AGENTS,
        )


# LF/CRLF foreign bytes and comments survive add/update/removal; repeats are byte-identical and pre-existing files remain.
@pytest.mark.parametrize("newline", [b"\n", b"\r\n"], ids=["LF", "CRLF"])
def test_foreign_toml_comments_survive_removal(project_cli, newline):
    root, _, declare = project_cli
    path = root / PATHS[1]
    path.parent.mkdir()
    before = b'# user preferences\nmodel = "example"\n\n[mcp_servers.foreign]\nurl = "https://foreign.example.invalid"\n'.replace(
        b"\n", newline
    )
    path.write_bytes(before)
    assert install() == 0
    added = path.read_bytes()
    assert added.startswith(before)
    assert added.replace(newline, b"").find(b"\n") == -1
    assert install() == 0
    assert path.read_bytes() == added
    comment = b"# keep this comment" + newline
    path.write_bytes(
        added.replace(b"[mcp_servers.docs]", comment + b"[mcp_servers.docs]")
    )
    updated_url = URL + "/updated"
    declare({"docs": updated_url})
    assert install() == 0
    updated = path.read_bytes()
    assert updated.startswith(before + newline + comment)
    assert updated.replace(newline, b"").find(b"\n") == -1
    assert tomllib.loads(updated.decode())["mcp_servers"]["docs"]["url"] == updated_url
    declare({})
    assert install() == 0
    assert path.read_bytes() == before + newline + comment
    assert "docs" not in tomllib.loads(path.read_bytes().decode())["mcp_servers"]


# Schema 1/2 retain their unknown-field errors and global scope never accepts schema 3.
def test_schema_boundaries(project_cli, capsys):
    from csk import global_install, manifest

    root, home, declare = project_cli
    for schema in (1, 2):
        declare({"docs": URL}, schema=schema)
        assert install() != 0
        assert "unsupported field(s): mcp_servers" in capsys.readouterr().err
    global_install.init(home, default_agents=AGENTS)
    global_install.global_skillfile(home).write_text(
        json.dumps({"schema_version": 3, "skills": [], "mcp_servers": {"docs": URL}})
    )
    assert cli.main(["global", "install"]) != 0
    assert "schema_version 3" in capsys.readouterr().err
    assert native_bytes(root) == {}
    manifest.ensure_empty_manifest(root)
    assert json.loads((root / "Skillfile.json").read_text())["schema_version"] == 2


# An empty/absent MCP map adds no MCP state or output, while selectors keep schema-2 behavior.
@pytest.mark.parametrize("servers", [None, {}])
def test_empty_map_is_inert(project_cli, capsys, servers):
    root, home, declare = project_cli
    declare(servers)
    assert install() == 0
    assert "MCP" not in capsys.readouterr().out
    assert not (home / "state/mcp").exists()
    assert native_bytes(root) == {}


# A planned HTTP entry satisfies a real skill requirement; a mismatched transport blocks all publication.
@pytest.mark.parametrize("transport", ["http", "stdio"])
def test_skill_requirement_uses_planned_view(project_cli, capsys, transport):
    root, _, declare = project_cli
    write_files(
        root,
        {
            "skills/demo/SKILL.md": "---\nname: demo\ndescription: Example\n---\n\nExample\n",
            "skills/demo/agent-skill.json": json.dumps(
                {
                    "schema_version": 5,
                    "capabilities": {},
                    "commands": {},
                    "dependencies": {
                        "mcp_servers": {
                            "docs": {
                                "transport": transport,
                                "required_in": "all",
                                "hint": "Configure docs",
                            }
                        }
                    },
                }
            ),
        },
    )
    declare(
        {"docs": URL},
        skills=[{"name": "demo", "from": "local", "directory": "skills/demo"}],
    )
    code = install()
    output = capsys.readouterr()
    if transport == "http":
        assert code == 0, output.err
        assert (root / ".agents/skills/demo/SKILL.md").exists()
    else:
        assert code != 0 and "transport" in output.err
        assert native_bytes(root) == {}
        assert not (root / ".agents/skills/demo").exists()


# Missing MCP ignore entries are repaired automatically; tracked native files remain tracked and are never ignored.
def test_native_gitignore_gate(project_cli, capsys):
    from conftest import run

    root, _, _ = project_cli
    assert cli.main(["install", "app", "--dry-run"]) == 0
    assert "would: added MCP gitignore entries:" in capsys.readouterr().out
    assert native_bytes(root) == {}
    path = root / PATHS[0]
    path.write_text('{"mcpServers":{}}')
    run(["git", "add", ".mcp.json"], root)
    assert install() == 0
    assert "/.mcp.json" not in (root / ".gitignore").read_text()
    assert run(["git", "ls-files", ".mcp.json"], root).stdout.strip() == ".mcp.json"


# Resolved and explicitly listed unsupported environments each get one skipped row without warnings or writes.
def test_unsupported_agent_rows(project_cli, capsys):
    root, _, declare = project_cli
    declare({"docs": URL}, agents=[*AGENTS, "gemini"])
    assert install() == 0
    output = capsys.readouterr().out
    assert output.count("mcp docs -> gemini (skipped: unsupported)") == 1
    assert "mcp_capability_unsupported" not in output
    declare(
        {
            "docs": URL,
            "other": {
                "server": {"transport": "http", "url": URL, "environments": ["gemini"]}
            }
        },
        agents=[*AGENTS, "gemini"],
    )
    before = native_bytes(root)
    assert install() == 0
    output = capsys.readouterr().out
    assert output.count("mcp other -> gemini (skipped: unsupported)") == 1
    assert "mcp_capability_unsupported" not in output
    assert native_bytes(root) == before


# Install never launches declared binaries or auth commands: exact runner recipes
# publish after approval, while an opaque program with a known shell shebang refuses.
@pytest.mark.parametrize(
    "command,args", [("npx", ["pkg@1.2.3"]), ("uvx", ["pkg==1.2.3"]), ("my-server", [])]
)
def test_no_server_or_auth_process(project_cli, monkeypatch, tmp_path, capsys, command, args):
    root, home, declare = project_cli
    traps = tmp_path / "trap-bin"
    traps.mkdir()
    marker = tmp_path / "executed"
    for trap_command in ("npx", "uvx", "my-server", "codex"):
        target = traps / (trap_command + ".exe" if sys.platform == "win32" else trap_command)
        target.write_text(f'#!/bin/sh\necho forbidden >> "{marker}"\nexit 99\n')
        if sys.platform == "win32" and trap_command == "uvx":
            # PE-magic admission fixture, never dispatched by install.
            target.write_bytes(b"MZfixture")
        target.chmod(0o755)
    monkeypatch.setenv("PATH", str(traps) + os.pathsep + os.environ["PATH"])
    declare(
        {
            "docs": {
                "server": {"transport": "http", "url": URL},
                "install": {"auth": {"kind": "oauth"}},
            },
            "tools": {
                "server": {
                    "transport": "stdio",
                    "command": command,
                    "args": args,
                }
            },
        }
    )
    monkeypatch.setattr(sys.stdin, "isatty", lambda: True)
    monkeypatch.setattr(sys.stdout, "isatty", lambda: True)
    monkeypatch.setattr("builtins.input", lambda _: "yes")
    before = acceptance._snapshot(root, home)
    if command == "my-server":
        monkeypatch.setattr("builtins.input", lambda _: pytest.fail("resolved shell must refuse before consent"))
    code = install()
    if command == "my-server":
        assert code == 1
        output = capsys.readouterr()
        assert "mcp_command_invalid" in output.out + output.err
        assert acceptance._snapshot(root, home) == before
    elif sys.platform == "win32" and command == "npx":
        assert code != 0
        assert native_bytes(root) == {}
    else:
        assert code == 0
    assert not marker.exists()


# A native file edited after planning causes CAS refusal and survives the rollback byte-for-byte.
def test_concurrent_native_write_is_preserved(project_cli, capsys, monkeypatch):
    root, home, _ = project_cli
    original = publish.TransactionEngine
    path = root / PATHS[0]
    external = b'{"mcpServers":{"user":{"url":"https://user.example.invalid"}}}'
    reached = []

    def factory(home, **kwargs):
        def fault(point, target):
            if point == "prepared" and not reached:
                reached.append(point)
                path.write_bytes(external)

        return original(home, fault_hook=fault, **kwargs)

    monkeypatch.setattr(publish, "TransactionEngine", factory)
    assert install() != 0
    assert reached == ["prepared"]
    assert path.read_bytes() == external
    assert not (root / PATHS[1]).exists() and not (root / PATHS[2]).exists()
    assert not list((home / "state/mcp/ledger").glob("*.json"))


# Staging and backups remain adjacent with no broader modes, and successful commit removes every copy.
@pytest.mark.skipif(
    os.name == "nt",
    reason="POSIX permission bits; Windows inherits the native directory ACL",
)
def test_native_sidecar_privacy_and_cleanup(project_cli, monkeypatch):
    root, _, _ = project_cli
    path = root / PATHS[0]
    path.write_text('{"mcpServers":{},"private":"foreign-value"}')
    path.chmod(0o640)
    original = publish.TransactionEngine
    observed = set()
    sidecars = set()

    def factory(home, **kwargs):
        def fault(point, target):
            if target is None or target.target_class != "mcp-native":
                return
            for value in (target.staged_path, target.backup_path, target.rollback_path):
                if value:
                    copy = Path(value)
                    sidecars.add(copy)
                    if copy.exists():
                        observed.add(point)
                        assert copy.parent == Path(target.live_path).parent
                        original_mode = (
                            0o640 if target.identifier == "claude_code" else 0o600
                        )
                        assert stat.S_IMODE(copy.stat().st_mode) & ~original_mode == 0

        return original(home, fault_hook=fault, **kwargs)

    monkeypatch.setattr(publish, "TransactionEngine", factory)
    assert install() == 0
    assert "after_backup" in observed and "staging_entry_created" in observed
    assert sidecars and all(not path.exists() for path in sidecars)
    assert stat.S_IMODE(path.stat().st_mode) == 0o640


CRASH_POINTS = [
    "before_staging_entry_create",
    "staging_entry_created",
    "after_staging_chunk_sync",
    "during_staging_copy",
    "staging_entry_completed",
    "before_staging_mode_finalize",
    "staging_mode_finalized",
    "staging_mode_finalize_completed",
    "prepared",
    "after_backup",
    "after_install",
    "target_committed",
    "before_cleanup",
    "cleanup_journaled",
    "sidecar_tombed",
    "before_sidecar_mode_writable",
    "sidecar_mode_writable",
    "before_journal_tomb",
    "journal_tombed",
    "before_journal_unlink",
]


# Hard process death at each exposed journal boundary recovers into a coherent native/ledger/skill installation.
@pytest.mark.parametrize("point", CRASH_POINTS)
def test_process_death_recovery(project_cli, tmp_path, point):
    import subprocess

    root, home, declare = project_cli
    write_files(
        root,
        {
            "skills/demo/SKILL.md": "---\nname: demo\ndescription: Example\n---\n\nExample\n"
        },
    )
    declare(
        {"docs": URL},
        skills=[{"name": "demo", "from": "local", "directory": "skills/demo"}],
    )
    path = root / PATHS[0]
    path.write_text('{"mcpServers":{},"foreign":"preserve"}')
    reached = tmp_path / "crash-reached"
    script = """
import os, sys
from pathlib import Path
from csk import cli
from csk.sources import publish
original = publish.TransactionEngine
def factory(home, **kwargs):
    def fault(point, target):
        if point == sys.argv[1] and (target is None or target.target_class == "mcp-native"):
            Path(sys.argv[2]).write_text(point)
            os._exit(86)
    return original(home, fault_hook=fault, **kwargs)
publish.TransactionEngine = factory
sys.exit(cli.main(["install", "app", "--fix-gitignore"]))
"""
    child = subprocess.run(
        [sys.executable, "-c", script, point, str(reached)],
        capture_output=True,
        text=True,
    )
    assert child.returncode == 86, child.stderr
    assert reached.read_text() == point
    assert install() == 0
    for agent in AGENTS:
        read, _ = mcp_native.read_surface(root, mcp_native.project_surface(agent))
        assert read.state == "present" and "docs" in read.entries
    consumer = mcp_ownership.consumer_id(
        locking.canonical_project_identity(root), "project"
    )
    ledger = mcp_ownership.load_ledger(home, consumer)
    assert isinstance(ledger, mcp_ownership.Ledger) and len(ledger.entries) == 3
    assert json.loads(path.read_bytes())["foreign"] == "preserve"
    assert (root / ".agents/skills/demo/SKILL.md").read_bytes() == (
        root / "skills/demo/SKILL.md"
    ).read_bytes()
    assert (root / "Skillfile.lock.json").is_file()
    assert not list(root.rglob("*.csk-transaction-*"))


# A failure after skill publication rolls native files, ledger, gitignore and skill targets back together.
def test_skill_failure_rolls_back_native_and_ledger(project_cli, monkeypatch):
    root, home, declare = project_cli
    write_files(
        root,
        {
            "skills/demo/SKILL.md": "---\nname: demo\ndescription: Example\n---\n\nExample\n"
        },
    )
    declare(
        {"docs": URL},
        skills=[{"name": "demo", "from": "local", "directory": "skills/demo"}],
    )
    original = publish.TransactionEngine
    reached = []
    before = (root / ".gitignore").read_bytes()

    def factory(home, **kwargs):
        def fault(point, target):
            if (
                point == "target_committed"
                and target is not None
                and target.target_class == publish.CLASS_CONTEXT
                and not reached
            ):
                reached.append(point)
                raise OSError("injected skill publication failure")

        return original(home, fault_hook=fault, **kwargs)

    monkeypatch.setattr(publish, "TransactionEngine", factory)
    assert install() != 0
    assert reached == ["target_committed"]
    assert native_bytes(root) == {}
    assert not list((home / "state/mcp/ledger").glob("*.json"))
    assert (root / ".gitignore").read_bytes() == before
    assert not (root / ".agents/skills/demo").exists()
    assert not (root / "Skillfile.lock.json").exists()


# A later CAS failure rolls back earlier native writes while preserving the external replacement.
def test_concurrent_write_after_first_commit_rolls_back(project_cli, monkeypatch):
    root, home, _ = project_cli
    original = publish.TransactionEngine
    before_ignore = (root / ".gitignore").read_bytes()
    external = b'# user config\n[other]\nvalue = "preserve"\n'
    reached = []

    def factory(home, **kwargs):
        def fault(point, target):
            if (
                point == "target_committed"
                and target is not None
                and target.target_class == "mcp-native"
                and target.identifier == "claude_code"
                and not reached
            ):
                reached.append(point)
                (root / PATHS[1]).write_bytes(external)

        return original(home, fault_hook=fault, **kwargs)

    monkeypatch.setattr(publish, "TransactionEngine", factory)
    assert install() != 0
    assert reached == ["target_committed"]
    assert not (root / PATHS[0]).exists()
    assert (root / PATHS[1]).read_bytes() == external
    assert not (root / PATHS[2]).exists()
    assert (root / ".gitignore").read_bytes() == before_ignore
    assert not list((home / "state/mcp/ledger").glob("*.json"))


# A parent swapped for a link after preparation refuses without publishing through the link.
@pytest.mark.skipif(
    os.name == "nt", reason="requires an unprivileged POSIX directory symlink"
)
def test_native_parent_swap_refuses(project_cli, monkeypatch, tmp_path):
    root, _, _ = project_cli
    original = publish.TransactionEngine
    outside = tmp_path / "outside"
    outside.mkdir()
    reached = []

    def factory(home, **kwargs):
        def fault(point, target):
            if point == "prepared" and not reached:
                reached.append(point)
                (root / ".codex").rename(root / "original-codex")
                (root / ".codex").symlink_to(outside, target_is_directory=True)

        return original(home, fault_hook=fault, **kwargs)

    monkeypatch.setattr(publish, "TransactionEngine", factory)
    assert install() != 0
    assert reached == ["prepared"]
    assert list(outside.iterdir()) == []
    assert (root / ".codex").is_symlink()
    assert not (root / PATHS[0]).exists()


# An invalid protected ledger blocks even removal and leaves native bytes unchanged, with total refusal rows.
def test_corrupt_ledger_blocks_install(project_cli, capsys):
    root, home, declare = project_cli
    assert install() == 0
    capsys.readouterr()
    before = native_bytes(root)
    ledger = next((home / "state/mcp/ledger").glob("*.json"))
    ledger.write_text("{}")
    declare({"other": URL})
    assert install() != 0
    output = capsys.readouterr().err
    assert output.count("(refused: mcp_ownership_unknown)") == 3
    assert native_bytes(root) == before
    assert ledger.read_text() == "{}"
    declare({})
    assert install() != 0
    assert native_bytes(root) == before


# Canonical but unsupported ledger profiles/agents refuse ownership without touching native files.
@pytest.mark.parametrize("field", ["capability", "agent"])
def test_unsupported_ledger_profile_refuses(project_cli, capsys, field):
    from dataclasses import replace

    root, home, _ = project_cli
    assert install() == 0
    capsys.readouterr()
    before = native_bytes(root)
    path = next((home / "state/mcp/ledger").glob("*.json"))
    consumer = mcp_ownership.consumer_id(
        locking.canonical_project_identity(root), "project"
    )
    ledger = mcp_ownership.load_ledger(home, consumer)
    assert isinstance(ledger, mcp_ownership.Ledger)
    entry = ledger.entries[0]
    if field == "capability":
        ledger = replace(
            ledger,
            entries=(replace(entry, capability="future-profile"), *ledger.entries[1:]),
        )
    else:
        ledger = replace(
            ledger,
            entries=(
                replace(entry, address=replace(entry.address, agent="future-agent")),
                *ledger.entries[1:],
            ),
        )
    encoded = mcp_ownership.encode_ledger(ledger)
    path.write_bytes(encoded)
    assert install() != 0
    assert capsys.readouterr().err.count("(refused: mcp_ownership_unknown)") == 3
    assert native_bytes(root) == before
    assert path.read_bytes() == encoded


# Consent binds the normalized declaration: changing executable arguments cannot reuse the old approval.
def test_changed_declaration_requires_new_consent(project_cli, monkeypatch, capsys):
    root, _, declare = project_cli
    declaration = {
        "server": {"transport": "stdio", "command": "my-server", "args": ["--first"]}
    }
    declare({"tools": declaration})
    monkeypatch.setattr(sys.stdin, "isatty", lambda: True)
    monkeypatch.setattr(sys.stdout, "isatty", lambda: True)
    monkeypatch.setattr("builtins.input", lambda _: "yes")
    assert install() == 0
    before = native_bytes(root)
    declaration["server"]["args"] = ["--second"]
    declare({"tools": declaration})
    monkeypatch.setattr(sys.stdin, "isatty", lambda: False)
    assert install() != 0
    assert "mcp_plan_unapproved" in capsys.readouterr().err
    assert native_bytes(root) == before


# Windows capability checks admit HTTP/native executable plans and refuse known or resolved batch launchers.
@pytest.mark.parametrize(
    "command", [None, "native-server.exe", "npx", "launcher", "tools.cmd"]
)
def test_windows_capabilities(project_cli, monkeypatch, capsys, command):
    from types import SimpleNamespace

    root, _, declare = project_cli
    monkeypatch.setattr(
        mcp_project,
        "sys",
        SimpleNamespace(platform="win32", stdin=sys.stdin, stdout=sys.stdout),
    )
    original_which = mcp_project.shutil.which
    monkeypatch.setattr(
        mcp_project.shutil,
        "which",
        lambda cmd: "launcher.cmd" if cmd == "launcher" else original_which(cmd),
    )
    if command is not None:
        args = ["pkg@1.2.3"] if command == "npx" else []
        declare(
            {
                "tools": {
                    "server": {"transport": "stdio", "command": command, "args": args}
                }
            }
        )
    before = (root / ".gitignore").read_bytes()
    code = install("--dry-run")
    output = capsys.readouterr()
    if command in (None, "native-server.exe"):
        assert code == 0, output.err
    else:
        assert code != 0
        assert output.err.count("would: mcp tools ->") == 3
        assert "mcp_windows_batch_unsupported" in output.err
    assert native_bytes(root) == {}
    assert (root / ".gitignore").read_bytes() == before


# Client-disabled external entries cannot satisfy requirements; enabling the same entry is the positive control.
@pytest.mark.parametrize("agent", ["claude_code", "codex_cli", "opencode"])
def test_disabled_external_requirement(project_cli, capsys, agent):
    root, _, declare = project_cli
    write_files(
        root,
        {
            "skills/demo/SKILL.md": "---\nname: demo\ndescription: Example\n---\n\nExample\n",
            "skills/demo/agent-skill.json": json.dumps(
                {
                    "schema_version": 5,
                    "capabilities": {},
                    "commands": {},
                    "dependencies": {
                        "mcp_servers": {
                            "docs": {
                                "transport": "stdio" if agent == "opencode" else "http",
                                "required_in": "all",
                                "hint": "Enable docs",
                            }
                        }
                    },
                }
            ),
        },
    )
    declare(
        {},
        agents=[agent],
        skills=[{"name": "demo", "from": "local", "directory": "skills/demo"}],
    )
    if agent == "claude_code":
        write_files(
            root,
            {
                ".mcp.json": json.dumps(
                    {"mcpServers": {"docs": {"type": "http", "url": URL}}}
                ),
                ".claude/settings.json": json.dumps(
                    {"disabledMcpjsonServers": ["docs"]}
                ),
            },
        )
    elif agent == "codex_cli":
        write_files(
            root,
            {
                ".codex/config.toml": f'[mcp_servers.docs]\nurl = "{URL}"\nenabled = false\n'
            },
        )
    else:
        write_files(
            root,
            {
                "opencode.json": json.dumps(
                    {
                        "mcp": {
                            "docs": {
                                "type": "local",
                                "command": ["my-server", "--tools"],
                                "enabled": False,
                            }
                        }
                    }
                )
            },
        )
    before = native_bytes(root)
    assert install() != 0
    assert "docs" in capsys.readouterr().err
    assert native_bytes(root) == before
    assert not (root / ".agents/skills/demo").exists()
    assert not (root / "Skillfile.lock.json").exists()
    if agent == "claude_code":
        (root / ".claude/settings.json").write_text("{}")
    elif agent == "codex_cli":
        path = root / PATHS[1]
        path.write_text(path.read_text().replace("enabled = false", "enabled = true"))
    else:
        path = root / "opencode.json"
        data = json.loads(path.read_bytes())
        data["mcp"]["docs"]["enabled"] = True
        path.write_text(json.dumps(data))
    assert install() == 0
    assert (root / ".agents/skills/demo/SKILL.md").exists()


# With no declarations or requirements, an unrelated malformed native file is untouched and does not gate skills.
def test_empty_map_does_not_validate_unrelated_native(project_cli):
    root, home, declare = project_cli
    declare({})
    path = root / PATHS[0]
    path.write_text("// user-owned JSONC")
    assert install() == 0
    assert path.read_text() == "// user-owned JSONC"
    assert not (home / "state/mcp").exists()


# A transient planning race retries successfully; exhausting retries retains every refusal row and publishes nothing.
@pytest.mark.parametrize("recover", [True, False])
def test_planning_retry_preserves_report(project_cli, monkeypatch, capsys, recover):
    from csk.builds.planner import BuildPlanningError

    root, home, _ = project_cli
    original = publish.install_schema2
    calls = []

    def race(*args, **kwargs):
        calls.append(True)
        if recover and len(calls) == 2:
            return original(*args, **kwargs)
        raise BuildPlanningError("concurrent_state_change", "injected planning race")

    monkeypatch.setattr(publish, "install_schema2", race)
    code = install()
    output = capsys.readouterr()
    if recover:
        assert code == 0
        assert len(calls) == 2
        assert len(native_bytes(root)) == 3
    else:
        assert code != 0
        assert len(calls) == 3
        assert [
            line for line in output.err.splitlines() if line.startswith("mcp ")
        ] == [
            f"mcp docs -> {agent} {path} (refused: concurrent_state_change)"
            for agent, path in zip(AGENTS, PATHS)
        ]
        assert native_bytes(root) == {}
        assert not list((home / "state/mcp/ledger").glob("*.json"))
