"""Portable alias grammar and bounded, non-executing host PATH observations."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from csk import cli, mcp_command_resolution as resolution, mcp_declarations as mcp
from test_mcp_install_acceptance import _snapshot
from test_mcp_project_install import project_cli


FIXTURES = Path(__file__).parent / "fixtures/mcp"
OBSERVED = json.loads((FIXTURES / "macos-evaluator-aliases.json").read_text())
PACKAGED = json.loads((FIXTURES / "packaged-evaluator-aliases.json").read_text())
ALIASES = sorted({e["name"] for e in OBSERVED["entries"]} | {
    name for family in PACKAGED["families"] for name in family["names"]
})
pytestmark = pytest.mark.usefixtures("stable_env")


def stdio(command, args=()):
    return {"bad": {"server": {"transport": "stdio", "command": command, "args": list(args)}}}


def parse(command, args=()):
    return mcp.parse_mcp_servers(stdio(command, args), invocation_scope="project", target_agents=["claude_code"])


# Every mechanically observed or package-documented alias refuses in both
# command and embedded-word positions, including normalized suffix variants.
@pytest.mark.parametrize("alias", ALIASES)
@pytest.mark.parametrize("suffix", ["", ".exe", ".cmd", ".bat", ".ps1", ".com"])
def test_alias_fixture_is_refused(alias, suffix):
    token = alias.upper() + suffix
    for command, args, field in [(token, [], "command"), ("my-server", ["--exec=" + token], "args.0")]:
        with pytest.raises(mcp.McpDeclarationError) as caught:
            parse(command, args)
        assert caught.value.code == "mcp_command_invalid"
        assert caught.value.field == "mcp_servers.bad.server." + field


# Fixture evidence is independently retained rather than generated from the
# current table during tests; known omissions and each discovery mechanism stay visible.
def test_alias_fixture_provenance():
    assert OBSERVED["source"] == ".scripts/scan_mcp_evaluator_aliases.py"
    assert OBSERVED["roots"] == ["/usr/bin", "/bin", "/usr/sbin", "/sbin"]
    assert len(OBSERVED["seed_sha256"]) == 64 and OBSERVED["date"]
    assert {"view", "vimdiff", "rview", "rvim", "vimtutor"} <= set(ALIASES)
    assert {e["mechanism"] for row in OBSERVED["entries"] for e in row["evidence"]} >= {"symlink", "hardlink", "shebang"}
    assert all(family["source"].startswith("https://") for family in PACKAGED["families"])


# The scanner detects planted link/script relationships in an isolated tree,
# excludes an unrelated executable, and never invokes a subprocess.
@pytest.mark.skipif(os.name == "nt", reason="POSIX inode and shebang scan fixture")
def test_alias_scanner_mechanisms(tmp_path, monkeypatch):
    import runpy
    scanner = runpy.run_path(str(Path(__file__).parents[1] / ".scripts/scan_mcp_evaluator_aliases.py"))
    target = executable(tmp_path / "vim")
    (tmp_path / "my-view").symlink_to(target.name)
    os.link(target, tmp_path / "my-hard")
    executable(tmp_path / "my-script", b"#!/usr/bin/python3\n")
    executable(tmp_path / "my-exec", b'#!/unknown\nexec vim "$@"\n')
    executable(tmp_path / "my-server")
    monkeypatch.setattr(subprocess, "run", lambda *a, **kw: pytest.fail("scan must not execute anything"))
    result = scanner["scan"]((tmp_path,))
    observed = {row["name"]: {e["mechanism"] for e in row["evidence"]} for row in result["entries"]}
    assert observed["my-view"] == {"symlink"}
    assert observed["my-hard"] == {"hardlink"}
    assert observed["my-script"] == {"shebang"}
    assert observed["my-exec"] == {"literal-exec"}
    assert "my-server" not in observed and not result["errors"]


def executable(path, body=b"opaque binary fixture\n"):
    path.write_bytes(body)
    path.chmod(0o700)
    return path


# Owned aliases and shebangs refuse without executing their marker payload;
# a chain and executable suffix exercise the same normalized shared inventory.
@pytest.mark.skipif(os.name == "nt", reason="POSIX symlink and shebang fixture")
@pytest.mark.parametrize("kind", ["symlink", "chain", "shebang", "env", "suffix", "runner-alias"])
def test_resolved_evaluator_refuses(tmp_path, monkeypatch, kind):
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    marker = tmp_path / "must-not-run"
    target_name = "ViM.EXE" if kind == "suffix" else "npx" if kind == "runner-alias" else "vim"
    target = executable(bin_dir / target_name, b"#!/bin/sh\ntouch must-not-run\n")
    command = bin_dir / "myedit"
    if kind in {"symlink", "suffix", "runner-alias"}:
        command.symlink_to(target.name)
    elif kind == "chain":
        (bin_dir / "middle").symlink_to(target.name)
        command.symlink_to("middle")
    else:
        interpreter = str(target) if kind == "shebang" else "/usr/bin/env python3"
        executable(command, ("#!" + interpreter + "\ntouch must-not-run\n").encode())
    monkeypatch.setenv("PATH", str(bin_dir))
    with pytest.raises(mcp.McpDeclarationError) as caught:
        resolution.validate_host_commands(parse("myedit"), cwd=tmp_path)
    assert caught.value.code == "mcp_command_invalid"
    assert caught.value.field == "mcp_servers.bad.server.command"
    assert str(bin_dir) not in str(caught.value)
    assert not marker.exists()


# Host resolution is a CLI admission step for check, dry-run and publication;
# refusal precedes consent and leaves all project/home bytes unchanged.
@pytest.mark.skipif(os.name == "nt", reason="POSIX symlink fixture")
@pytest.mark.parametrize("kind", ["symlink", "shebang"])
@pytest.mark.parametrize("argv", [("check", "app"), ("install", "app", "--dry-run"), ("install", "app")])
def test_cli_resolved_evaluator_refuses(project_cli, tmp_path, monkeypatch, capsys, kind, argv):
    root, home, declare = project_cli
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    target = executable(bin_dir / "vim")
    if kind == "symlink":
        (bin_dir / "myedit").symlink_to(target.name)
    else:
        executable(bin_dir / "myedit", ("#!" + str(target) + "\n").encode())
    declare(stdio("myedit"))
    before = _snapshot(root, home)
    monkeypatch.setenv("PATH", str(bin_dir) + os.pathsep + os.environ["PATH"])
    monkeypatch.setattr("builtins.input", lambda prompt: pytest.fail("refusal must precede consent"))
    assert cli.main(list(argv)) == 1
    output = capsys.readouterr()
    assert "mcp_command_invalid" in output.out + output.err
    assert _snapshot(root, home) == before


# Exact runner recipes retain their interpreter substrate; jq and ordinary
# opaque/unresolvable programs remain valid. No control executable is run.
@pytest.mark.parametrize("command,args", [("npx", ["pkg@1.2.3"]), ("uvx", ["pkg==1.2.3"]), ("my-server", ["--port", "1"]), ("jq", ["."]), ("missing-server", [])])
def test_resolution_positive_controls(tmp_path, monkeypatch, command, args):
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    if command in {"npx", "uvx"}:
        executable(bin_dir / command, b"#!/usr/bin/env node\n")
    elif command != "missing-server":
        executable(bin_dir / command)
    monkeypatch.setenv("PATH", str(bin_dir))
    resolution.validate_host_commands(parse(command, args), cwd=tmp_path)


# Resolve only the first PATH executable; relative and empty entries use the
# project cwd. Broken/cyclic links and non-regular files never execute or hang.
@pytest.mark.skipif(os.name == "nt", reason="POSIX links and executable-mode fixture")
@pytest.mark.parametrize("kind", ["shadow", "relative", "empty", "cycle", "directory", "non-executable", "broken"])
def test_path_selection_boundaries(tmp_path, monkeypatch, kind):
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    later = tmp_path / "later"
    later.mkdir()
    executable(later / "myedit", b"#!/bin/sh\n")
    if kind in {"shadow", "relative", "empty"}:
        executable((tmp_path if kind == "empty" else bin_dir) / "myedit")
    elif kind == "cycle":
        (bin_dir / "myedit").symlink_to("myedit")
    elif kind == "directory":
        (bin_dir / "myedit").mkdir()
    elif kind == "broken":
        (bin_dir / "myedit").symlink_to("vim")
    else:
        (bin_dir / "myedit").write_bytes(b"#!/bin/sh\n")
    first = "" if kind == "empty" else "bin" if kind == "relative" else str(bin_dir)
    monkeypatch.setenv("PATH", first + os.pathsep + str(later))
    if kind == "broken":
        monkeypatch.setenv("PATH", str(bin_dir))
    if kind in {"shadow", "relative", "empty", "broken"}:
        resolution.validate_host_commands(parse("myedit"), cwd=tmp_path)
    else:
        with pytest.raises(mcp.McpDeclarationError, match="mcp_command_invalid"):
            resolution.validate_host_commands(parse("myedit"), cwd=tmp_path)


# Link targets outside PATH may be classified by basename but must never be
# opened. An opaque outside target and an unknown interpreter remain unknown.
@pytest.mark.skipif(os.name == "nt", reason="POSIX symlink fixture")
@pytest.mark.parametrize("target_name", ["vim", "opaque"])
def test_resolution_does_not_open_outside_path(tmp_path, monkeypatch, target_name):
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    target = executable(outside / target_name, b"#!/bin/sh\n")
    (bin_dir / "myedit").symlink_to(target)
    original = os.open
    opened = []
    def bounded_open(path, *args, **kwargs):
        opened.append(Path(path))
        assert Path(path).parent == bin_dir
        return original(path, *args, **kwargs)
    monkeypatch.setattr(resolution.os, "open", bounded_open)
    monkeypatch.setenv("PATH", str(bin_dir))
    if target_name == "vim":
        with pytest.raises(mcp.McpDeclarationError, match="mcp_command_invalid"):
            resolution.validate_host_commands(parse("myedit"), cwd=tmp_path)
    else:
        resolution.validate_host_commands(parse("myedit"), cwd=tmp_path)
    assert not opened


# The review's actual macOS aliases dispatch the obfuscated runner to an owned
# recording stub; check/dry-run/terminal install must instead refuse with no writes.
@pytest.mark.skipif(sys.platform != "darwin", reason="actual macOS system alias dispatch witness")
@pytest.mark.parametrize("command", ["view", "vimdiff"])
def test_default_vim_alias_real_cli_refuses(project_cli, tmp_path, command):
    import errno
    import pty
    import select
    import time
    root, home, declare = project_cli
    system_alias = Path("/usr/bin") / command
    assert system_alias.is_symlink() and system_alias.resolve() == Path("/usr/bin/vim")
    args = ["-Nu", "NONE", "-i", "NONE", "-n", "-es", "-c", "call system('n'.'px pkg')", "-c", "qa!"]
    declare(stdio(command, args))
    stub = tmp_path / "stub-bin"
    stub.mkdir()
    marker = tmp_path / "invoked"
    executable(stub / "npx", b'#!/bin/sh\nprintf "%s\\n" "$*" > "$REVIEW_MARKER"\n')
    env = dict(os.environ, PATH=str(stub) + os.pathsep + os.environ["PATH"], REVIEW_MARKER=str(marker))
    dispatch = subprocess.run([str(system_alias), *args], cwd=root, env=env, stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=5)
    assert dispatch.returncode == 0 and marker.read_text() == "pkg\n"
    before = _snapshot(root, home)
    for argv in [("check", "app"), ("install", "app", "--dry-run")]:
        result = subprocess.run([sys.executable, "-m", "csk", *argv], cwd=root, env=os.environ.copy(), stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=30)
        assert result.returncode == 1 and "mcp_command_invalid" in result.stdout + result.stderr
    master, slave = pty.openpty()
    child = subprocess.Popen([sys.executable, "-m", "csk", "install", "app"], cwd=root, env=os.environ.copy(), stdin=slave, stdout=slave, stderr=slave)
    os.close(slave)
    output = b""
    try:
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline:
            if select.select([master], [], [], 0.1)[0]:
                try:
                    data = os.read(master, 65536)
                except OSError as exc:
                    if exc.errno == errno.EIO:
                        break
                    raise
                if not data:
                    break
                output += data
            elif child.poll() is not None:
                break
        rc = child.wait(timeout=3)
    finally:
        if child.poll() is None:
            child.terminate()
            child.wait(timeout=5)
        os.close(master)
    assert rc == 1 and b"mcp_command_invalid" in output and b"[y/N]" not in output
    assert not (root / ".mcp.json").exists() and _snapshot(root, home) == before
    print(json.dumps({"alias": command, "recorded": marker.read_text(), "exit": rc, "transcript": output.decode(errors="replace")}))
