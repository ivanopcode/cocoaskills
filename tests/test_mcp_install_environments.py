"""Neutral install options and environment selection through real CLI entry points."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest
from test_mcp_install_acceptance import (
    ENV_HEADER,
    SURFACES,
    URL,
    _case,
    _rows,
    _snapshot,
)
from test_mcp_project_install import project_cli, install
from test_mcp_status import _status

from csk import mcp_declarations as declarations, mcp_project, protocol_json


def _http(environments=None):
    server = {"transport": "http", "url": URL}
    if environments is not None:
        server["environments"] = environments
    return {"server": server, "install": {"auth": {"kind": "none"}}}


def test_install_member_rejects_old_name_and_remaps_errors():
    # The unreleased old name has no alias; install accepts valid options and reports exact public error paths.
    valid = _http()
    assert declarations.parse_mcp_servers(
        {"docs": valid}, invocation_scope="project", target_agents=["cursor"]
    )
    for options, field in [
        ({"csk": {}}, "mcp_servers.docs.csk"),
        ({"install": []}, "mcp_servers.docs.install"),
        ({"install": {"unknown": True}}, "mcp_servers.docs.install.unknown"),
        (
            {"install": {"headers": {"X-Key": {"env": "PATH"}}}},
            "mcp_servers.docs.install.headers.X-Key.env",
        ),
    ]:
        with pytest.raises(declarations.McpDeclarationError) as error:
            declarations.parse_mcp_servers(
                {"docs": {"server": valid["server"], **options}},
                invocation_scope="project",
                target_agents=["cursor"],
            )
        assert error.value.field == field
        assert error.value.code == (
            "mcp_env_name_reserved"
            if field.endswith(".env")
            else "mcp_declaration_invalid"
        )


def test_install_member_preserves_normalized_digest():
    # An independently specified pre-rename normalized model fixes the digest; the new spelling cannot alter it.
    model = {
        "name": "docs",
        "server": {
            "transport": "http",
            "command": None,
            "args": [],
            "env_names": [],
            "url": URL,
            "headers": [],
        },
        "auth": {"kind": "none", "help_url": None},
        "agents": ["cursor"],
        "scope": "project",
        "git_policy": "local",
    }
    expected = hashlib.sha256(
        b"csk-mcp-declaration-v1\0" + protocol_json.canonical_bytes(model)
    ).hexdigest()
    declaration = declarations.parse_mcp_servers(
        {"docs": _http()},
        invocation_scope="project",
        target_agents=["cursor"],
    )["docs"]
    assert declarations.declaration_digest(declaration) == expected


@pytest.mark.parametrize(
    "environments,writable,unsupported",
    [
        (None, ["claude_code"], ["gemini", "windsurf"]),
        (
            ["windsurf", "cursor", "opencode", "codex_cli"],
            ["codex_cli", "cursor"],
            ["opencode", "windsurf"],
        ),
    ],
)
@pytest.mark.parametrize("dry_run", [False, True])
def test_environment_selection_reports_every_known_pair(
    tmp_path, environments, writable, unsupported, dry_run
):
    # Defaults use resolved agents; explicit writable agents outside them install, while unsupported native bytes stay intact.
    case = _case(tmp_path / "selection")
    case.skillfile["agents"] = ["claude_code", "gemini", "windsurf"]
    case.declare({"docs": _http(environments)})
    foreign = case.project / ".gemini/settings.json"
    foreign.parent.mkdir()
    foreign.write_bytes(
        b'{"mcpServers":{"foreign":{"url":"https://foreign.example.invalid"}}}\n'
    )
    before = _snapshot(case.project, case.home, Path(case.env["HOME"]))
    result = case.run("--fix-gitignore", *(["--dry-run"] if dry_run else []))
    assert result.returncode == 0, result.stdout
    prefix = "would: " if dry_run else ""
    expected = [
        f"{prefix}mcp docs -> {agent} {path} (added)"
        for agent, path in SURFACES
        if agent in writable
    ]
    expected += [
        f"{prefix}mcp docs -> {agent} (skipped: unsupported)" for agent in unsupported
    ]
    assert _rows(result, dry_run=dry_run) == expected
    assert "mcp_capability_unsupported" not in result.stdout
    assert (
        foreign.read_bytes()
        == b'{"mcpServers":{"foreign":{"url":"https://foreign.example.invalid"}}}\n'
    )
    if dry_run:
        assert _snapshot(case.project, case.home, Path(case.env["HOME"])) == before
    else:
        assert [
            (agent, path) for agent, path in SURFACES if (case.project / path).exists()
        ] == [(agent, path) for agent, path in SURFACES if agent in writable]


@pytest.mark.parametrize("dry_run", [False, True])
def test_unsupported_only_has_no_writes_or_consent(tmp_path, dry_run):
    # A known unsupported-only stdio declaration neither writes any tree nor prompts for code publication.
    case = _case(tmp_path / "unsupported")
    case.skillfile["agents"] = ["claude_code"]
    case.declare({})
    assert case.run("--fix-gitignore").returncode == 0
    case.declare(
        {
            "docs": {
                "server": {
                    "transport": "stdio",
                    "command": "npx",
                    "args": ["pkg@1.0.0"],
                    "environments": ["windsurf", "opencode", "gemini"],
                }
            }
        }
    )
    before = _snapshot(case.project, case.home, Path(case.env["HOME"]))
    result = case.run(*(["--dry-run"] if dry_run else []))
    assert result.returncode == 0, result.stdout
    prefix = "would: " if dry_run else ""
    assert _rows(result, dry_run=dry_run) == [
        f"{prefix}mcp docs -> {agent} (skipped: unsupported)"
        for agent in ["gemini", "opencode", "windsurf"]
    ]
    assert "mcp_plan_unapproved" not in result.stdout
    assert _snapshot(case.project, case.home, Path(case.env["HOME"])) == before
    status = _status(case, "--check", "--json")
    assert status.returncode == 0, status.stdout + status.stderr
    assert json.loads(status.stdout)[0]["mcp"] == [
        {"server": "docs", "agent": agent, "path": None, "state": "unsupported"}
        for agent in ["gemini", "opencode", "windsurf"]
    ]
    assert _snapshot(case.project, case.home, Path(case.env["HOME"])) == before


@pytest.mark.parametrize("missing", [False, True])
def test_status_unsupported_is_neutral_to_check(tmp_path, missing):
    # Unsupported rows do not dirty current state or conceal a missing writable entry; all status forms remain read-only.
    case = _case(tmp_path / "status")
    case.skillfile["agents"] = ["claude_code"]
    case.declare({"docs": _http(["gemini", "cursor"])})
    assert case.run("--fix-gitignore").returncode == 0
    if missing:
        (case.project / ".cursor/mcp.json").unlink()
    before = _snapshot(case.project, case.home, Path(case.env["HOME"]))
    state = "missing" if missing else "current"
    for flags in [(), ("--check",), ("--check", "--json")]:
        result = _status(case, *flags)
        assert result.returncode == (int(missing) if "--check" in flags else 0), (
            result.stdout + result.stderr
        )
        if "--json" in flags:
            payload = json.loads(result.stdout)[0]
            assert payload["mcp"] == [
                {
                    "server": "docs",
                    "agent": "cursor",
                    "path": ".cursor/mcp.json",
                    "state": state,
                },
                {
                    "server": "docs",
                    "agent": "gemini",
                    "path": None,
                    "state": "unsupported",
                },
            ]
            assert payload["clean"] is not missing
        else:
            assert [
                line for line in result.stdout.splitlines() if line.startswith("mcp ")
            ] == [
                f"mcp docs -> cursor .cursor/mcp.json ({state})",
                "mcp docs -> gemini (unsupported)",
            ]
    assert _snapshot(case.project, case.home, Path(case.env["HOME"])) == before


@pytest.mark.parametrize("failure", ["grammar", "foreign", "codec", "consent"])
@pytest.mark.parametrize("dry_run", [False, True])
def test_refused_install_retains_unsupported_pairs(tmp_path, failure, dry_run):
    # Early, reconcile, codec and consent refusals retain sorted owned/desired/unsupported rows and publish nothing.
    case = _case(tmp_path / "refusal")
    declaration = _http(["cursor", "gemini"])
    expected = [("cursor", ".cursor/mcp.json")]
    if failure == "codec":
        case.declare({"docs": URL})
        assert case.run("--fix-gitignore").returncode == 0
        (case.project / ".codex/config.toml").write_bytes(
            ("mcp_servers = { docs = { url = " + json.dumps(URL) + " } }\n").encode()
        )
        expected = list(SURFACES)
        code = "mcp_native_config_unsupported_syntax"
    elif failure == "foreign":
        path = case.project / ".cursor/mcp.json"
        path.parent.mkdir()
        path.write_text(json.dumps({"mcpServers": {"docs": {"url": URL}}}))
        code = "mcp_entry_conflict"
    elif failure == "consent":
        declaration = json.loads(json.dumps(ENV_HEADER))
        declaration["server"]["environments"] = ["cursor", "gemini"]
        code = "mcp_plan_unapproved"
    else:
        declaration["server"]["url"] = "http://example.invalid"
        code = "mcp_url_invalid"
    case.declare({"docs": declaration})
    before = _snapshot(case.project, case.home, Path(case.env["HOME"]))
    result = case.run("--fix-gitignore", *(["--dry-run"] if dry_run else []))
    # Dry-run consent gaps are permitted, but must still show the unsupported pair.
    assert result.returncode == (0 if failure == "consent" and dry_run else 1), (
        result.stdout
    )
    prefix = "would: " if dry_run else ""
    action = (
        "added"
        if failure == "consent" and dry_run
        else "conflict: foreign entry"
        if failure == "foreign"
        else f"refused: {code}"
    )
    rows = [
        f"{prefix}mcp docs -> {agent} {path} ({action})" for agent, path in expected
    ]
    rows.append(f"{prefix}mcp docs -> gemini (skipped: unsupported)")
    assert _rows(result, dry_run=dry_run) == rows, result.stdout
    if not (failure == "consent" and dry_run):
        lines = result.stdout.splitlines()
        summary = next(
            i for i, line in enumerate(lines) if code in line and line not in rows
        )
        assert max(lines.index(row) for row in rows) < summary
    assert _snapshot(case.project, case.home, Path(case.env["HOME"])) == before


def test_consent_binds_new_writable_agents_and_excludes_unsupported(
    project_cli, monkeypatch, capsys
):
    # Adding only unsupported targets reuses approval; a new explicit writable target needs fresh consent before any write.
    import sys

    root, home, declare = project_cli
    server = {
        "transport": "stdio",
        "command": "my-server",
        "environments": ["claude_code"],
    }
    declare({"tools": {"server": server}}, agents=["claude_code"])
    monkeypatch.setattr(sys.stdin, "isatty", lambda: True)
    monkeypatch.setattr(sys.stdout, "isatty", lambda: True)
    monkeypatch.setattr("builtins.input", lambda _: "yes")
    assert install() == 0
    capsys.readouterr()
    server["environments"].append("gemini")
    declare({"tools": {"server": server}}, agents=["claude_code"])
    monkeypatch.setattr(sys.stdin, "isatty", lambda: False)
    assert install() == 0
    assert "mcp_plan_unapproved" not in capsys.readouterr().err
    server["environments"].append("cursor")
    raw = {"tools": {"server": server}}
    declare(raw, agents=["claude_code"])
    plan = mcp_project.plan_project(root, ["claude_code"], raw, home)
    assert len(plan.pending) == 1
    assert plan.pending[0].agents == ("claude_code", "cursor")
    before = _snapshot(root, home)
    assert install() == 1
    assert "mcp_plan_unapproved" in capsys.readouterr().err
    assert _snapshot(root, home) == before
    monkeypatch.setattr(sys.stdin, "isatty", lambda: True)
    assert install() == 0
    assert (root / ".cursor/mcp.json").exists()
    assert not mcp_project.plan_project(root, ["claude_code"], raw, home).pending


def test_unsupported_batch_launcher_has_no_windows_write_capability(
    project_cli, monkeypatch
):
    # The simulated Windows capability check skips a reporting-only launcher but still refuses a writable batch target.
    import sys
    from types import SimpleNamespace

    root, home, declare = project_cli
    monkeypatch.setattr(
        mcp_project,
        "sys",
        SimpleNamespace(platform="win32", stdin=sys.stdin, stdout=sys.stdout),
    )
    server = {
        "transport": "stdio",
        "command": "npx",
        "args": ["pkg@1.0.0"],
        "environments": ["gemini"],
    }
    raw = {"tools": {"server": server}}
    declare(raw)
    before = _snapshot(root, home)
    plan = mcp_project.plan_project(root, [agent for agent, _ in SURFACES], raw, home)
    assert not plan.desired and not plan.edits and not plan.subjects
    assert [row.render() for row in plan.rows] == [
        "mcp tools -> gemini (skipped: unsupported)"
    ]
    assert _snapshot(root, home) == before
    server["environments"].append("cursor")
    declare(raw)
    before = _snapshot(root, home)
    with pytest.raises(
        mcp_project.McpProjectError, match="mcp_windows_batch_unsupported"
    ):
        mcp_project.plan_project(root, [agent for agent, _ in SURFACES], raw, home)
    assert _snapshot(root, home) == before


def test_unsupported_rows_sort_with_each_server(project_cli):
    # Each server's writable rows precede its alphabetic unsupported rows, including explicit writers outside project targets.
    root, home, declare = project_cli
    raw = {
        "docs": _http(["windsurf", "codex_cli", "opencode"]),
        "api": _http(["gemini", "cursor"]),
    }
    declare(raw, agents=["claude_code"])
    before = _snapshot(root, home)
    plan = mcp_project.plan_project(root, ["claude_code"], raw, home)
    assert [row.render() for row in plan.rows] == [
        "mcp api -> cursor .cursor/mcp.json (added)",
        "mcp api -> gemini (skipped: unsupported)",
        "mcp docs -> codex_cli .codex/config.toml (added)",
        "mcp docs -> opencode (skipped: unsupported)",
        "mcp docs -> windsurf (skipped: unsupported)",
    ]
    assert _snapshot(root, home) == before


def test_consent_refusal_retains_absent_owned_pairs(tmp_path):
    # A consent refusal retains absent owned pairs as blocked by the new plan, plus unsupported rows.
    case = _case(tmp_path / "absent-owned")
    case.declare({"old": URL})
    assert case.run("--fix-gitignore").returncode == 0
    for _, path in SURFACES:
        (case.project / path).unlink()
    declaration = json.loads(json.dumps(ENV_HEADER))
    declaration["server"]["environments"] = ["cursor", "gemini"]
    case.declare({"api": declaration})
    before = _snapshot(case.project, case.home, Path(case.env["HOME"]))
    result = case.run("--fix-gitignore")
    assert result.returncode == 1, result.stdout
    assert _rows(result) == [
        "mcp api -> cursor .cursor/mcp.json (refused: mcp_plan_unapproved)",
        "mcp api -> gemini (skipped: unsupported)",
        *[
            f"mcp old -> {agent} {path} (blocked by mcp_plan_unapproved)"
            for agent, path in SURFACES
        ],
    ]
    assert _snapshot(case.project, case.home, Path(case.env["HOME"])) == before
