"""Independent CLI acceptance contract for project MCP installation."""

from __future__ import annotations

import json
import os
import re
import stat
import subprocess
import sys
import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest
from conftest import make_config, make_project, write_files, write_skillfile

from csk import config, locking


URL = "https://mcp.example.invalid/mcp"
SURFACES = (
    ("claude_code", ".mcp.json"),
    ("codex_cli", ".codex/config.toml"),
    ("cursor", ".cursor/mcp.json"),
)
STDIO = {
    "server": {"transport": "stdio", "command": "uvx", "args": ["pkg==1.0.0"]},
}
ENV_HEADER = {
    "server": {
        "transport": "http",
        "url": URL,
    },
    "install": {
        "headers": {"Authorization": {"env": "DOCS_TOKEN", "prefix": "bearer"}},
    },
}


@dataclass
class InstallCase:
    project: Path
    home: Path
    env: dict[str, str]
    skillfile: dict[str, Any]

    def declare(self, declarations: dict[str, Any]) -> None:
        self.skillfile["mcp_servers"] = declarations
        write_skillfile(self.project, self.skillfile)

    def run(self, *options: str, globally: bool = False) -> subprocess.CompletedProcess[str]:
        command = ["global", "install"] if globally else ["install", "app"]
        # A merged stream preserves observable ordering between rows and the refusal summary.
        result = subprocess.run(
            [sys.executable, "-m", "csk", *command, *options],
            cwd=self.project,
            env=self.env,
            stdin=subprocess.DEVNULL,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            timeout=60,
        )
        return subprocess.CompletedProcess(result.args, result.returncode, result.stdout, "")


def _case(root: Path, *, schema: int = 3) -> InstallCase:
    root.mkdir(parents=True)
    project = make_project(root)
    home = root / "manager"
    locking.provision_new_manager_home(home)
    skills = root / "skills"
    skills.mkdir()
    cfg = make_config(home, skills, project, agents=[agent for agent, _ in SURFACES])
    config.save_config(cfg)
    skillfile: dict[str, Any] = {
        "schema_version": schema,
        "agents": [agent for agent, _ in SURFACES],
        "skills": [],
    }
    if schema != 1:
        # A real local skill makes partial skill publication observable on MCP refusal.
        write_files(project, {"source/seed/SKILL.md": "---\nname: seed\ndescription: Local seed.\n---\n\n# Seed\n"})
        skillfile.update(
            sources={"local": {"path": "source"}},
            skills=[{"name": "seed", "from": "local", "directory": "seed"}],
        )
    write_skillfile(project, skillfile)
    env = {key: value for key, value in os.environ.items() if not key.startswith("CSK_")}
    isolated_home = root / "home"
    isolated_home.mkdir()
    env.update(
        HOME=str(isolated_home),
        USERPROFILE=str(isolated_home),
        XDG_CONFIG_HOME=str(isolated_home / "config"),
        XDG_CACHE_HOME=str(isolated_home / "cache"),
        XDG_DATA_HOME=str(isolated_home / "data"),
        APPDATA=str(isolated_home / "appdata"),
        LOCALAPPDATA=str(isolated_home / "localappdata"),
        CSK_CONFIG=str(cfg.path),
        PYTHONPATH=str(Path(__file__).resolve().parents[1] / "src"),
    )
    return InstallCase(project, home, env, skillfile)


def _snapshot(*roots: Path) -> dict[tuple[int, str], tuple[object, ...]]:
    state: dict[tuple[int, str], tuple[object, ...]] = {}
    for index, root in enumerate(roots):
        for path in [root, *sorted(root.rglob("*"))]:
            relative = path.relative_to(root)
            if index == 1 and (relative.as_posix() == ".lock" or relative.parts[:1] == ("locks",)):
                continue
            info = path.lstat()
            if stat.S_ISREG(info.st_mode):
                content: object = path.read_bytes()
            elif stat.S_ISLNK(info.st_mode):
                content = os.readlink(path)
            else:
                content = None
            state[index, path.relative_to(root).as_posix()] = (
                info.st_mode, None if stat.S_ISDIR(info.st_mode) else info.st_mtime_ns, content,
            )
    return state


def _rows(result: subprocess.CompletedProcess[str], *, dry_run: bool = False) -> list[str]:
    prefix = "would:" if dry_run else "mcp "
    return [
        line for line in result.stdout.splitlines()
        if line.startswith(prefix)
        and re.match(r"^(would: )?mcp [A-Za-z0-9][A-Za-z0-9_-]{0,127} -> ", line)
    ]


def _expected_rows(action: str, *, dry_run: bool = False) -> list[str]:
    prefix = "would: " if dry_run else ""
    return [f"{prefix}mcp docs -> {agent} {path} ({action})" for agent, path in SURFACES]


def _assert_success(result: subprocess.CompletedProcess[str]) -> None:
    assert result.returncode == 0, result.stdout + result.stderr


def _assert_refusal_without_writes(
    case: InstallCase, code: str, *, expected_rows: list[str] | None = None,
) -> subprocess.CompletedProcess[str]:
    before = _snapshot(case.project, case.home)
    result = case.run("--fix-gitignore")
    assert result.returncode != 0, result.stdout + result.stderr
    rows = _expected_rows(f"refused: {code}") if expected_rows is None else expected_rows
    assert _rows(result) == rows, "MCP refusal rows must match the exact ordered outcomes"
    lines = (result.stdout + "\n" + result.stderr).splitlines()
    code_pattern = rf"(?<!\w){re.escape(code)}(?!\w)"
    summary_positions = [
        index for index, line in enumerate(lines)
        if not line.startswith("mcp ") and re.search(code_pattern, line)
    ]
    assert summary_positions, "MCP refusal summary must contain the exact refusal code"
    last_row = max(index for index, line in enumerate(lines) if line.startswith("mcp "))
    assert last_row < min(summary_positions), "MCP refusal rows must precede the summary"
    assert _snapshot(case.project, case.home) == before
    return result


def _native_bytes(case: InstallCase) -> dict[str, bytes]:
    return {path: (case.project / path).read_bytes() for _, path in SURFACES}


def _assert_seed_installed(case: InstallCase) -> None:
    assert (case.project / ".agents/skills/seed/SKILL.md").read_bytes() == (
        case.project / "source/seed/SKILL.md"
    ).read_bytes()


def test_url_shorthand_installs_three_native_configs_headless(tmp_path: Path) -> None:
    # A headless URL-only install publishes all native entries, exact ignore paths and the skill.
    case = _case(tmp_path / "url")
    case.declare({"docs": URL})
    result = case.run("--fix-gitignore")
    _assert_success(result)
    assert _rows(result) == _expected_rows("added")
    assert "MCP configuration installed. Authentication not checked." in result.stdout.splitlines()
    assert json.loads((case.project / ".mcp.json").read_bytes())["mcpServers"]["docs"] == {
        "type": "http", "url": URL,
    }
    with (case.project / ".codex/config.toml").open("rb") as stream:
        assert tomllib.load(stream)["mcp_servers"]["docs"]["url"] == URL
    assert json.loads((case.project / ".cursor/mcp.json").read_bytes())["mcpServers"]["docs"]["url"] == URL
    ignore_lines = (case.project / ".gitignore").read_text(encoding="utf-8").splitlines()
    for _, path in SURFACES:
        assert f"/{path}" in ignore_lines
    _assert_seed_installed(case)


def test_shorthand_and_authless_http_object_have_identical_publication(tmp_path: Path) -> None:
    # Expanding a URL into a header-free HTTP object preserves native bytes and report rows.
    shorthand = _case(tmp_path / "shorthand")
    expanded = _case(tmp_path / "expanded")
    shorthand.declare({"docs": URL})
    expanded.declare({"docs": {"server": {"transport": "http", "url": URL}}})
    first, second = shorthand.run("--fix-gitignore"), expanded.run("--fix-gitignore")
    _assert_success(first)
    _assert_success(second)
    assert _rows(first) == _rows(second) == _expected_rows("added")
    assert _native_bytes(shorthand) == _native_bytes(expanded)


def test_server_environments_limits_native_publication(tmp_path: Path) -> None:
    # server.environments narrows MCP publication to Claude while skill publication remains enabled.
    case = _case(tmp_path / "environments")
    case.declare({"docs": {"server": {
        "transport": "http", "url": URL, "environments": ["claude_code"],
    }}})
    result = case.run("--fix-gitignore")
    _assert_success(result)
    assert _rows(result) == ["mcp docs -> claude_code .mcp.json (added)"]
    assert json.loads((case.project / ".mcp.json").read_bytes())["mcpServers"]["docs"] == {
        "type": "http", "url": URL,
    }
    for _, path in SURFACES[1:]:
        assert not (case.project / path).exists()
    ignore_lines = (case.project / ".gitignore").read_text(encoding="utf-8").splitlines()
    assert "/.mcp.json" in ignore_lines
    assert "/.codex/config.toml" not in ignore_lines
    assert "/.cursor/mcp.json" not in ignore_lines
    _assert_seed_installed(case)


def test_repeat_reports_unchanged_and_preserves_native_bytes(tmp_path: Path) -> None:
    # Reinstalling the same declaration leaves all native and ignore bytes unchanged.
    case = _case(tmp_path / "repeat")
    case.declare({"docs": URL})
    _assert_success(case.run("--fix-gitignore"))
    before = _native_bytes(case)
    ignore_before = (case.project / ".gitignore").read_bytes()
    result = case.run()
    _assert_success(result)
    assert _rows(result) == _expected_rows("unchanged")
    assert _native_bytes(case) == before
    assert (case.project / ".gitignore").read_bytes() == ignore_before


def test_removal_deletes_only_created_empty_native_files(tmp_path: Path) -> None:
    # Removing the last declaration reports removal and deletes all three csk-created files.
    case = _case(tmp_path / "remove-created")
    case.declare({"docs": URL})
    _assert_success(case.run("--fix-gitignore"))
    assert len(_native_bytes(case)) == 3
    case.declare({})
    result = case.run()
    _assert_success(result)
    assert _rows(result) == _expected_rows("removed")
    for _, path in SURFACES:
        assert not (case.project / path).exists()
    _assert_seed_installed(case)


def test_removal_preserves_foreign_server_bytes_in_preexisting_file(tmp_path: Path) -> None:
    # Removing docs preserves the raw foreign entry and never deletes a pre-existing native file.
    case = _case(tmp_path / "remove-shared")
    foreign = b'"other" : { "type" : "http", "url" : "https://other.example.invalid/mcp" }'
    native = case.project / ".mcp.json"
    native.write_bytes(b'{\n  "mcpServers": {\n    ' + foreign + b'\n  }\n}\n')
    case.declare({"docs": URL})
    _assert_success(case.run("--fix-gitignore"))
    assert foreign in native.read_bytes()
    assert set(json.loads(native.read_bytes())["mcpServers"]) == {"docs", "other"}
    case.declare({})
    result = case.run()
    _assert_success(result)
    assert _rows(result) == _expected_rows("removed")
    assert foreign in native.read_bytes()
    assert json.loads(native.read_bytes())["mcpServers"] == {
        "other": {"type": "http", "url": "https://other.example.invalid/mcp"},
    }
    for _, path in SURFACES[1:]:
        assert not (case.project / path).exists()


@pytest.mark.parametrize(
    ("declaration", "code"),
    [
        pytest.param("http://mcp.example.invalid/mcp", "mcp_url_invalid", id="insecure-url"),
        pytest.param("https://u@mcp.example.invalid/", "mcp_url_invalid", id="userinfo"),
        pytest.param(URL + "?q=1", "mcp_url_invalid", id="query"),
        pytest.param(URL + "#part", "mcp_url_invalid", id="fragment"),
        pytest.param("https://\u00e9xample.invalid/mcp", "mcp_url_invalid", id="non-ascii-host"),
        pytest.param("", "mcp_url_invalid", id="empty-url"),
        pytest.param(42, "mcp_declaration_invalid", id="number"),
        pytest.param([], "mcp_declaration_invalid", id="list"),
        pytest.param(True, "mcp_declaration_invalid", id="boolean"),
        pytest.param(
            {"server": {"transport": "http", "url": URL, "headers": {
                "Authorization": {"env": "DOCS_TOKEN", "prefix": "bearer"},
            }}},
            "mcp_declaration_invalid", id="headers-inside-server",
        ),
        pytest.param(
            {"server": {"transport": "http", "url": URL}, "agents": ["claude_code"]},
            "mcp_declaration_invalid", id="agents-at-declaration-level",
        ),
    ],
)
def test_invalid_declaration_refuses_before_any_project_or_home_write(
    tmp_path: Path, declaration: Any, code: str,
) -> None:
    # Each invalid declaration reports three exact refusals before its summary and preserves project/home state.
    case = _case(tmp_path / "invalid")
    case.declare({"docs": declaration})
    _assert_refusal_without_writes(case, code)


def test_foreign_same_name_conflict_refuses_before_any_write(tmp_path: Path) -> None:
    # A foreign docs entry reports two blocked pairs and its conflict before the summary, with no writes.
    case = _case(tmp_path / "conflict")
    write_files(case.project, {".cursor/mcp.json": json.dumps({"mcpServers": {"docs": {"url": URL}}})})
    case.declare({"docs": URL})
    expected = _expected_rows("refused: mcp_entry_conflict")
    expected[-1] = "mcp docs -> cursor .cursor/mcp.json (conflict: foreign entry)"
    _assert_refusal_without_writes(case, "mcp_entry_conflict", expected_rows=expected)


def test_drift_reports_skipped_and_preserves_user_edit(tmp_path: Path) -> None:
    # A user-edited owned entry stays untouched and reports skipped: drift while install succeeds.
    case = _case(tmp_path / "drift")
    case.declare({"docs": URL})
    _assert_success(case.run("--fix-gitignore"))
    native = case.project / ".mcp.json"
    edited = json.loads(native.read_bytes())
    edited["mcpServers"]["docs"]["url"] = "https://edited.example.invalid/mcp"
    native.write_text(json.dumps(edited, indent=4) + "\n", encoding="utf-8")
    before = _native_bytes(case)
    result = case.run()
    _assert_success(result)
    expected = _expected_rows("unchanged")
    expected[0] = "mcp docs -> claude_code .mcp.json (skipped: drift)"
    assert _rows(result) == expected
    assert "mcp_entry_drift" in result.stdout + result.stderr
    assert _native_bytes(case) == before


def test_trailing_comma_native_json_refuses_before_any_write(tmp_path: Path) -> None:
    # Unsupported native JSON reports three exact refusals before its summary and preserves project/home state.
    case = _case(tmp_path / "syntax")
    write_files(case.project, {".mcp.json": '{"mcpServers": {},}\n'})
    case.declare({"docs": URL})
    _assert_refusal_without_writes(case, "mcp_native_config_unsupported_syntax")


@pytest.mark.parametrize("declaration", [STDIO, ENV_HEADER], ids=["stdio", "env-header"])
@pytest.mark.parametrize("forged_env", [False, True], ids=["no-approval", "env-is-not-approval"])
def test_headless_authority_requires_real_consent(
    tmp_path: Path, declaration: dict[str, Any], forged_env: bool,
) -> None:
    # Headless authority, including forged approval, reports three unapproved pairs before its summary with no writes.
    case = _case(tmp_path / "consent")
    case.declare({"docs": declaration})
    case.env["DOCS_TOKEN"] = "test-value"
    if forged_env:
        case.env.update(CSK_MCP_APPROVED="1", CSK_MCP_APPROVE="yes")
    _assert_refusal_without_writes(case, "mcp_plan_unapproved")


@pytest.mark.parametrize("declaration", [URL, STDIO], ids=["url-install", "unapproved-stdio"])
def test_install_never_starts_server_or_package_runner(
    tmp_path: Path, declaration: Any,
) -> None:
    # A successful URL install and unapproved stdio never execute PATH traps; controls prove traps record calls.
    case = _case(tmp_path / "no-execution")
    traps = tmp_path / "traps"
    traps.mkdir()
    marker = tmp_path / "trap-called"
    env = case.env.copy()
    env["MCP_TRAP_MARKER"] = str(marker)
    env["PATH"] = str(traps) + os.pathsep + env.get("PATH", "")
    for name in ("npx", "uvx", "node"):
        if os.name == "nt":
            executable = traps / f"{name}.cmd"
            executable.write_text('@echo off\r\necho invoked>>"%MCP_TRAP_MARKER%"\r\nexit /b 91\r\n', encoding="utf-8")
            control = subprocess.run(
                ["cmd.exe", "/d", "/c", str(executable)], env=env, capture_output=True,
            )
        else:
            executable = traps / name
            executable.write_text('#!/bin/sh\nprintf "invoked\\n" >> "$MCP_TRAP_MARKER"\nexit 91\n', encoding="utf-8")
            executable.chmod(0o700)
            control = subprocess.run([str(executable)], env=env, capture_output=True)
        assert control.returncode == 91
        assert marker.read_bytes().strip() == b"invoked"
        marker.unlink()
    case.env = env
    case.declare({"docs": declaration})
    if declaration == URL:
        result = case.run("--fix-gitignore")
        _assert_success(result)
        assert _rows(result) == _expected_rows("added")
    else:
        # Windows batch launchers are refused before consent under the native-executable contract.
        code = "mcp_windows_batch_unsupported" if os.name == "nt" else "mcp_plan_unapproved"
        _assert_refusal_without_writes(case, code)
    assert not marker.exists()


@pytest.mark.parametrize(
    ("declaration", "approval"),
    [(URL, "not-required"), (STDIO, "required")],
    ids=["url", "consent-required"],
)
def test_dry_run_reports_plan_and_approval_without_writes(
    tmp_path: Path, declaration: Any, approval: str,
) -> None:
    # Dry run previews all three actions and approval state without publishing or recording consent.
    case = _case(tmp_path / "dry-run")
    case.declare({"docs": declaration})
    before = _snapshot(case.project, case.home)
    result = case.run("--fix-gitignore", "--dry-run")
    _assert_success(result)
    assert _rows(result, dry_run=True) == _expected_rows("added", dry_run=True)
    # In particular, required must not match the token not-required.
    approval_pattern = rf"(?<![\w-]){re.escape(approval)}(?![\w-])"
    assert any("docs" in line and re.search(approval_pattern, line) for line in result.stdout.splitlines())
    assert _snapshot(case.project, case.home) == before


@pytest.mark.parametrize("schema", [1, 2])
def test_legacy_schema_still_refuses_mcp_field(tmp_path: Path, schema: int) -> None:
    # Schema 1/2 retain the exact unknown-field refusal instead of silently accepting MCP declarations.
    case = _case(tmp_path / "legacy-field", schema=schema)
    case.declare({"docs": URL})
    result = case.run("--fix-gitignore")
    assert result.returncode != 0
    assert _rows(result) == []
    assert "Skillfile has unsupported field(s): mcp_servers" in result.stdout + result.stderr
    for _, path in SURFACES:
        assert not (case.project / path).exists()


@pytest.mark.parametrize("schema", [1, 2])
def test_legacy_project_install_has_no_mcp_output(tmp_path: Path, schema: int) -> None:
    # Unchanged schema 1/2 installs succeed without MCP output or native configurations.
    case = _case(tmp_path / "legacy-install", schema=schema)
    result = case.run("--fix-gitignore")
    _assert_success(result)
    assert not any("mcp" in line.lower() for line in (result.stdout + result.stderr).splitlines())
    for _, path in SURFACES:
        assert not (case.project / path).exists()
    assert not (case.home / "state/mcp").exists()
    if schema == 2:
        _assert_seed_installed(case)


def test_global_install_refuses_schema_three(tmp_path: Path) -> None:
    # The global CLI retains its schema-3 upgrade refusal and never writes native agent files.
    case = _case(tmp_path / "global", schema=1)
    write_files(case.home, {"global/Skillfile.json": json.dumps({"schema_version": 3, "skills": []})})
    before = _snapshot(case.project, Path(case.env["HOME"]))
    result = case.run(globally=True)
    assert result.returncode != 0
    assert _rows(result) == []
    assert "Unsupported Skillfile schema_version 3; this Skillfile requires a newer csk" in result.stdout + result.stderr
    assert _snapshot(case.project, Path(case.env["HOME"])) == before
    assert not (case.home / "state/mcp").exists()


@pytest.mark.parametrize("root_index", [0, 1], ids=["project", "home"])
@pytest.mark.parametrize("mutation", ["extra", "bytes"])
def test_snapshot_detects_noncoordination_changes(tmp_path: Path, root_index: int, mutation: str) -> None:
    # Coordination changes are allowed; an extra file or changed bytes in either root still fails the snapshot.
    project, home = tmp_path / "project", tmp_path / "home"
    for root in (project, home):
        root.mkdir()
        (root / "record.txt").write_bytes(b"original")
    before = _snapshot(project, home)
    (home / ".lock").write_text("coordination")
    (home / "locks/projects").mkdir(parents=True)
    (home / "locks/projects/probe.lock").write_text("coordination")
    assert _snapshot(project, home) == before
    changed_root = (project, home)[root_index]
    if mutation == "extra":
        (changed_root / "unexpected.json").write_bytes(b"unexpected")
    else:
        (changed_root / "record.txt").write_bytes(b"changed")
    assert _snapshot(project, home) != before
