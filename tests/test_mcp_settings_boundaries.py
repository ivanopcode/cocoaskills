"""Regression witnesses for local storage, interrupted migration and writers."""

import json
import subprocess
import sys

import pytest

from csk import cli, mcp_local_targets as local, transactions
from conftest import make_project, write_files
from test_mcp_environments_spec import declaration
from test_mcp_install_acceptance import _snapshot
from test_mcp_launcher_lifecycle import approve
from test_mcp_project_install import project_cli, install, native_bytes


def target_command(root, action="add"):
    return [
        "mcp", "local-target", action, "cursor", "--server", "docs", "--project", str(root)
    ]


# Nested projects reject manager homes in either their containing or another repository; external storage accepts canonical aliases.
@pytest.mark.parametrize(
    "location", ["sibling", "another-repository", "linked-sibling", "external"]
)
def test_local_storage_outside_every_repository(
    project_cli, monkeypatch, capsys, tmp_path, location
):
    root, home, declare = project_cli
    declare(declaration(["claude_code"]))
    nested = root / "nested"
    nested.mkdir()
    (nested / "Skillfile.json").write_bytes((root / "Skillfile.json").read_bytes())
    if location == "external":
        approve(monkeypatch)
        assert cli.main(target_command(nested)) == 0
        assert set(local.read(nested, home)) == {"docs"}
        return
    embedded = root / "manager"
    if location == "another-repository":
        embedded = make_project(tmp_path, "other") / "manager"
    elif location == "linked-sibling":
        alias = tmp_path / "linked-manager"
        alias.symlink_to(embedded, target_is_directory=True)
        embedded = alias
    approve(monkeypatch)
    monkeypatch.setenv("CSK_CONFIG", str(embedded / "config.json"))
    before = _snapshot(root, home)
    assert cli.main(target_command(nested)) == 1
    assert "mcp_local_target_invalid" in capsys.readouterr().err
    assert not embedded.exists()
    assert _snapshot(root, home) == before
    monkeypatch.setenv("CSK_CONFIG", str(home / "config.json"))
    assert cli.main(target_command(nested)) == 0
    alias = tmp_path / "project-alias"
    alias.symlink_to(nested, target_is_directory=True)
    assert local.path(alias, home) == local.path(nested, home)
    assert set(local.read(alias, home)) == {"docs"}


# Direct retry of a crashed migration either completes or refuses without publishing; explicit rollback then retry is the positive control.
@pytest.mark.parametrize("identifier", ["local-targets", "skillfile"])
def test_direct_migration_retry_preserves_refusal_state(
    project_cli, monkeypatch, capsys, identifier
):
    root, home, declare = project_cli
    declare(declaration(["claude_code", "cursor"]))
    script = """
import os, sys, builtins
from csk import cli, transactions
original = transactions.TransactionEngine
def factory(*a, **kw):
    def fail(event, target):
        if event == 'after_install' and target.identifier == sys.argv[2]:
            os._exit(86)
    return original(*a, **kw, fault_hook=fail)
transactions.TransactionEngine = factory
sys.stdin.isatty = sys.stdout.isatty = lambda: True
builtins.input = lambda prompt: 'yes'
sys.exit(cli.main(['mcp', 'migrate', '--project', sys.argv[1]]))
"""
    result = subprocess.run(
        [sys.executable, "-c", script, str(root), identifier],
        capture_output=True, text=True, timeout=60,
    )
    assert result.returncode == 86, result.stderr
    before = _snapshot(root, home)
    approve(monkeypatch)
    command = ["mcp", "migrate", "--project", str(root)]
    code = cli.main(command)
    if code:
        assert "transaction_recovery_required" in capsys.readouterr().err
        assert _snapshot(root, home) == before
        journals = list((home / "state/transactions/v1").glob("*.json"))
        assert len(journals) == 1
        assert cli.main(["recover", "--rollback", journals[0].stem]) == 0
        assert cli.main(command) == 0
    data = json.loads((root / "Skillfile.json").read_bytes())
    assert data["mcp_servers"]["docs"]["server"]["environments"] == ["claude_code"]
    assert set(local.read(root, home)) == {"docs"}
    assert not list((home / "state/transactions/v1").glob("*.json"))


# Ordinary writers reject each forbidden selector before writes; the same writer accepts its canonical neighbour.
@pytest.mark.parametrize(
    "selector", [["gemini"], ["windsurf"], [], ["pi", "pi"], ["claude_code"]]
)
@pytest.mark.parametrize("writer", ["add", "remove", "init", "update", "upgrade"])
def test_skillfile_writers_validate_selectors(project_cli, capsys, selector, writer):
    root, home, declare = project_cli

    def setup(targets):
        write_files(root, {
            "source/seed/SKILL.md": "---\nname: seed\ndescription: Seed.\n---\n"
        })
        declare(declaration(targets), skills=[{
            "name": "seed", "from": "local", "directory": "source/seed"
        }])
        path = root / "Skillfile.json"
        data = json.loads(path.read_bytes())
        data.pop("ignore_mode")
        path.write_text(json.dumps(data))

    if writer == "add":
        args = ["add", "next", "--tag", "v1", "--project", str(root)]
    elif writer == "remove":
        args = ["remove", "seed", "--project", str(root)]
    elif writer == "update":
        args = [writer]
    else:
        args = [writer, str(root)]
    setup(selector)
    if selector == ["claude_code"]:
        assert cli.main(args) == 0
        return
    before = _snapshot(root, home)
    assert cli.main(args) == 2
    assert "mcp_declaration_invalid" in capsys.readouterr().err
    assert _snapshot(root, home) == before
    setup(["claude_code"])
    assert cli.main(args) == 0
    after = json.loads((root / "Skillfile.json").read_bytes())
    assert after["mcp_servers"]["docs"]["server"]["environments"] == ["claude_code"]


# Removal narrows authority without TTY, preserves the neighbouring grant, and the next install removes only its owned native row.
def test_headless_removal_revokes_only_own_target(project_cli, monkeypatch, capsys):
    root, home, declare = project_cli
    servers = declaration(["claude_code"])
    servers["other"] = servers["docs"]
    declare(servers, agents=["claude_code"])
    approve(monkeypatch)
    assert cli.main(target_command(root)) == 0
    other = target_command(root)
    other[other.index("docs")] = "other"
    assert cli.main(other) == 0
    assert install() == 0
    grants = local.read(root, home)
    shared = (root / "Skillfile.json").read_bytes()
    monkeypatch.setattr(sys.stdin, "isatty", lambda: False)
    monkeypatch.setattr(sys.stdout, "isatty", lambda: False)
    assert cli.main(target_command(root, "remove")) == 0
    assert local.read(root, home) == {"other": grants["other"]}
    assert install() == 0
    assert (root / "Skillfile.json").read_bytes() == shared
    cursor = json.loads(native_bytes(root)[".cursor/mcp.json"])
    assert set(cursor["mcpServers"]) == {"other"}
    claude = json.loads(native_bytes(root)[".mcp.json"])
    assert set(claude["mcpServers"]) == {"docs", "other"}


# An edit observed after recovery invalidates both preimages before publication; a fresh retry accepts the new input.
@pytest.mark.parametrize("changed", ["skillfile", "local-targets"])
def test_preimages_rechecked_after_recovery(
    project_cli, monkeypatch, capsys, changed
):
    root, home, declare = project_cli
    declare(declaration(["claude_code"]))
    approve(monkeypatch)
    recover = transactions.TransactionEngine.recover
    observed = []

    def edit(engine, lock):
        recover(engine, lock)
        if changed == "skillfile":
            path = root / "Skillfile.json"
            path.write_bytes(path.read_bytes() + b"\n")
        else:
            local.ensure_directory(home)
            local.path(root, home).write_bytes(local.encode(root, {"other": {}}))
        observed.append(_snapshot(root, home))

    with monkeypatch.context() as patch:
        patch.setattr(transactions.TransactionEngine, "recover", edit)
        assert cli.main(target_command(root)) == 1
    assert "mcp_local_target_invalid" in capsys.readouterr().err
    assert _snapshot(root, home) == observed[0]
    assert cli.main(target_command(root)) == 0


# Global/hybrid writers reject invalid selectors before any write; their valid control uses each scope's supported manifest grammar.
@pytest.mark.parametrize("selector", [["gemini"], ["windsurf"], [], ["pi", "pi"]])
@pytest.mark.parametrize("writer", [
    "global-init", "global-add", "global-remove", "hybrid-add", "hybrid-remove"
])
def test_other_skillfile_writers_validate_selectors(
    project_cli, capsys, selector, writer
):
    root, home, _ = project_cli
    scope, command = writer.split("-")
    path = home / scope / "Skillfile.json"
    path.parent.mkdir()
    entry = {"name": "seed", "tag": "v1"}
    if scope == "hybrid":
        entry["targets"] = ["app"]
    data = {
        "schema_version": 1, "skills": [entry], "mcp_servers": declaration(selector)
    }
    path.write_text(json.dumps(data))
    args = [scope, command]
    if command == "add":
        args += ["next", "--tag", "v1"]
        if scope == "hybrid":
            args += ["--target", "app"]
    elif command == "remove":
        args += ["seed"]
    before = _snapshot(root, home)
    assert cli.main(args) == 2
    assert "mcp_declaration_invalid" in capsys.readouterr().err
    assert _snapshot(root, home) == before
    # Schema 1 in these scopes has no MCP extension, including canonical selectors.
    data.pop("mcp_servers")
    path.write_text(json.dumps(data))
    assert cli.main(args) == 0


# A linked-worktree marker, bare repository, or repository at the storage leaf also forbids local authority; an external home is valid.
@pytest.mark.parametrize("marker", ["git-file", "bare", "store-repository"])
def test_local_storage_repository_markers(project_cli, tmp_path, marker):
    root, home, _ = project_cli
    candidate = tmp_path / "repository-home"
    candidate.mkdir()
    if marker == "git-file":
        (candidate / ".git").write_text("gitdir: ../worktree-metadata\n")
    elif marker == "bare":
        (candidate / "HEAD").write_text("ref: refs/heads/main\n")
        (candidate / "objects").mkdir()
        (candidate / "refs").mkdir()
    else:
        leaf = candidate / "state/mcp/local-targets"
        leaf.mkdir(parents=True)
        (leaf / ".git").mkdir()
    before = _snapshot(root, candidate)
    with pytest.raises(ValueError, match="mcp_local_target_invalid"):
        local.read(root, candidate)
    with pytest.raises(ValueError, match="mcp_local_target_invalid"):
        local.path(root, candidate)
    assert _snapshot(root, candidate) == before
    assert local.read(root, home) == {}
