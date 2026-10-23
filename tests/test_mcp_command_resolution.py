"""Portable alias grammar and bounded, non-executing host PATH observations."""

from __future__ import annotations

import json
import os
import shutil
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


def executable(path, body=b"\x7fELFopaque binary fixture\n"):
    path.write_bytes(body)
    path.chmod(0o700)
    return path


def runner_guard(tmp_path, monkeypatch, search):
    """Every dispatch oracle puts owned recording runners first in PATH."""
    stub = tmp_path / "stub"
    stub.mkdir()
    marker = tmp_path / "invoked"
    for name in ("npx", "uvx", "npm", "pnpm", "yarn", "bunx", "pipx", "uv", "pnpx", "dx"):
        executable(stub / name, b'#!/bin/sh\nprintf "%s\\n" "$*" > "$REVIEW_MARKER"\n')
    monkeypatch.setenv("PATH", str(stub) + os.pathsep + search + os.pathsep + os.environ["PATH"])
    monkeypatch.setenv("REVIEW_MARKER", str(marker))
    assert all(shutil.which(name) == str(stub / name) for name in ("npx", "uvx", "npm", "pnpm", "yarn", "bunx", "pipx"))
    return marker


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


# Exact runner recipes retain their interpreter substrate; ordinary
# opaque/unresolvable programs remain valid. No control executable is run.
@pytest.mark.parametrize("command,args", [("npx", ["pkg@1.2.3"]), ("uvx", ["pkg==1.2.3"]), ("my-server", ["--port", "1"]), ("my-filter", ["."]), ("missing-server", [])])
def test_resolution_positive_controls(tmp_path, monkeypatch, command, args):
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    if os.name == "nt" and command != "missing-server":
        executable(bin_dir / (command + ".exe"), b"MZfixture")
    elif command in {"npx", "uvx"}:
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


# Only the selected candidate is opened, through its original kernel-resolved
# path, and at most 1024 bytes are read. A known shebang refuses; an opaque body
# remains valid even when a symlink reaches outside the literal PATH directory.
@pytest.mark.skipif(os.name == "nt", reason="POSIX symlink fixture")
@pytest.mark.parametrize("target_name", ["vim", "opaque"])
@pytest.mark.parametrize("shebang", [False, True])
def test_selected_candidate_body_bound(tmp_path, monkeypatch, target_name, shebang):
    bin_dir, outside = tmp_path / "bin", tmp_path / "outside"
    bin_dir.mkdir()
    outside.mkdir()
    body = (b"#!/bin/sh\n" if shebang else b"\x7fELFopaque binary\n") + b"x" * 512
    executable(outside / target_name, body)
    candidate = bin_dir / "myedit"
    candidate.symlink_to(outside / target_name)
    original_open, original_read = os.open, os.read
    opened, sizes = [], []
    def selected_open(path, *args, **kwargs):
        opened.append(Path(path))
        assert Path(path) == candidate
        return original_open(path, *args, **kwargs)
    def bounded_read(descriptor, size):
        sizes.append(size)
        assert size <= 1024
        return original_read(descriptor, size)
    monkeypatch.setattr(resolution.os, "open", selected_open)
    monkeypatch.setattr(resolution.os, "read", bounded_read)
    monkeypatch.setenv("PATH", str(bin_dir))
    if shebang:
        with pytest.raises(mcp.McpDeclarationError, match="mcp_command_invalid"):
            resolution.validate_host_commands(parse("myedit"), cwd=tmp_path)
    else:
        resolution.validate_host_commands(parse("myedit"), cwd=tmp_path)
    assert opened == [candidate] and sizes == [1024]
    assert (outside / target_name).read_bytes() == body


def parent_component_fixture(tmp_path, kind, *, evaluator):
    a, b = tmp_path / "a", tmp_path / "b"
    a.mkdir()
    b.mkdir()
    (b / "nested").mkdir()
    (a / "hop").symlink_to(b / "nested", target_is_directory=True)
    if kind == "path-entry":
        (a / "bin").mkdir()
        (b / "bin").mkdir()
        executable(a / "bin/myedit", b"opaque\n" if evaluator else b"#!/bin/sh\n")
        selected = b / "bin/myedit"
        search = str(a / "hop/../bin")
    else:
        executable(a / "payload", b"opaque\n" if evaluator else b"#!/bin/sh\n")
        selected = b / "payload"
        target = "hop/../payload" if kind == "relative-target" else str(a / "hop/../payload")
        (a / "myedit").symlink_to(target)
        search = str(a) + os.pathsep + str(b)
    if evaluator:
        selected.symlink_to("/usr/bin/vim")
    else:
        executable(selected)
    return search, selected


# Each static symlink/parent traversal selects Vim at the OS, despite an opaque
# lexical decoy. Check, dry-run and install refuse before consent or any writes.
# Only the explicit dispatch witness executes Vim, reaching an owned runner stub.
@pytest.mark.skipif(sys.platform != "darwin", reason="actual system Vim dispatch witness")
@pytest.mark.parametrize("kind", ["relative-target", "absolute-target", "path-entry"])
@pytest.mark.parametrize("argv", [("check", "app"), ("install", "app", "--dry-run"), ("install", "app")])
def test_parent_component_cli_refuses(project_cli, tmp_path, monkeypatch, capsys, kind, argv):
    root, home, declare = project_cli
    search, _ = parent_component_fixture(tmp_path, kind, evaluator=True)
    marker = runner_guard(tmp_path, monkeypatch, search)
    args = ["-Nu", "NONE", "-i", "NONE", "-n", "-es", "-c", "call system('n'.'px pkg')", "-c", "qa!"]
    dispatch = subprocess.run(["myedit", *args], cwd=root, capture_output=True, text=True, timeout=5)
    assert dispatch.returncode == 0 and marker.read_text() == "pkg\n"
    marker.unlink()
    declare(stdio("myedit", args))
    before = _snapshot(root, home)
    monkeypatch.setattr("builtins.input", lambda prompt: pytest.fail("refusal must precede consent"))
    assert cli.main(list(argv)) == 1
    output = capsys.readouterr()
    assert "mcp_command_invalid" in output.out + output.err
    assert not marker.exists() and _snapshot(root, home) == before


# Kernel parent traversal may instead reach an opaque executable. The lexical
# decoy has a refused shebang, so rejecting every traversal cannot satisfy this control.
@pytest.mark.skipif(os.name == "nt", reason="POSIX symlink fixture")
@pytest.mark.parametrize("kind", ["relative-target", "absolute-target", "path-entry"])
def test_parent_component_positive_control(tmp_path, monkeypatch, kind):
    search, selected = parent_component_fixture(tmp_path, kind, evaluator=False)
    monkeypatch.setenv("PATH", search)
    resolution.validate_host_commands(parse("myedit", ["--port", "1"]), cwd=tmp_path)
    assert selected.read_bytes() == b"\x7fELFopaque binary fixture\n"


# Kernel PATH selection must skip non-program paths and recognize a case alias
# of the same evaluator. Each valid dispatch reaches only an owned runner stub;
# all three admission paths must refuse before consent and preserve every byte.
@pytest.mark.skipif(sys.platform != "darwin", reason="actual system Vim and APFS witness")
@pytest.mark.parametrize("kind", ["root", "slash", "dot", "file-parent", "case-alias"])
@pytest.mark.parametrize("argv", [("check", "app"), ("install", "app", "--dry-run"), ("install", "app")])
def test_kernel_selection_cli_refuses(project_cli, tmp_path, monkeypatch, capsys, kind, argv):
    root, home, declare = project_cli
    first, later = tmp_path / "bin", tmp_path / "later"
    first.mkdir()
    later.mkdir()
    (later / "myedit").symlink_to("/usr/bin/vim")
    search = str(first) + os.pathsep + str(later)
    if kind == "root":
        (first / "myedit").symlink_to("/")
    elif kind in {"slash", "dot"}:
        executable(first / "opaque")
        (first / "myedit").symlink_to("opaque/" if kind == "slash" else "opaque/.")
    elif kind == "file-parent":
        executable(first / "opaque")
        (first / "decoy").mkdir()
        executable(first / "decoy/myedit")
        search = str(first / "opaque/../decoy") + os.pathsep + str(later)
    else:
        alternate = first.with_name("BIN")
        if not alternate.exists() or not alternate.samefile(first):
            pytest.skip("requires case-insensitive filesystem")
        (first / "payload").symlink_to("/usr/bin/vim")
        (first / "myedit").symlink_to(alternate / "payload")
    marker = runner_guard(tmp_path, monkeypatch, search)
    args = ["-Nu", "NONE", "-i", "NONE", "-n", "-es", "-c", "call system('n'.'px pkg')", "-c", "qa!"]
    result = subprocess.run(["myedit", *args], cwd=root, capture_output=True, text=True, timeout=5)
    assert result.returncode == 0 and marker.read_text() == "pkg\n"
    marker.unlink()
    declare(stdio("myedit", args))
    before = _snapshot(root, home)
    monkeypatch.setattr("builtins.input", lambda prompt: pytest.fail("refusal must precede consent"))
    assert cli.main(list(argv)) == 1
    output = capsys.readouterr()
    assert "mcp_command_invalid" in output.out + output.err
    assert not marker.exists() and _snapshot(root, home) == before


# An opaque hard link of an in-PATH evaluator is identified by inode even when
# no link target or shebang exposes the evaluator name. Nothing is executed.
@pytest.mark.skipif(os.name == "nt", reason="POSIX hardlink fixture")
def test_kernel_identity_hardlink_refuses(tmp_path, monkeypatch):
    executable(tmp_path / "vim")
    os.link(tmp_path / "vim", tmp_path / "myedit")
    monkeypatch.setenv("PATH", str(tmp_path))
    with pytest.raises(mcp.McpDeclarationError, match="mcp_command_invalid"):
        resolution.validate_host_commands(parse("myedit"), cwd=tmp_path)


# An unknown interpreter name sharing a table-name inode is refused after just
# the selected script header read; the interpreter body is never opened.
@pytest.mark.skipif(os.name == "nt", reason="POSIX hardlink/shebang fixture")
def test_kernel_identity_interpreter_refuses(tmp_path, monkeypatch):
    executable(tmp_path / "python")
    os.link(tmp_path / "python", tmp_path / "custom-runtime")
    candidate = executable(tmp_path / "my-server", ("#!" + str(tmp_path / "custom-runtime") + "\n").encode())
    monkeypatch.setenv("PATH", str(tmp_path))
    original = os.open
    opened = []
    def selected_open(path, *args, **kwargs):
        opened.append(Path(path))
        assert Path(path) == candidate
        return original(path, *args, **kwargs)
    monkeypatch.setattr(resolution.os, "open", selected_open)
    with pytest.raises(mcp.McpDeclarationError, match="mcp_command_invalid"):
        resolution.validate_host_commands(parse("my-server"), cwd=tmp_path)
    assert opened == [candidate]


# Bytes beyond the selected 1024-byte header do not participate in admission;
# this is a bounded header observation, never a scan of arbitrary binary bodies.
def test_selected_header_bound_positive_control(tmp_path, monkeypatch):
    executable(tmp_path / ("my-server.exe" if os.name == "nt" else "my-server"),
               (b"MZxx" if os.name == "nt" else b"\x7fELF") + b"x" * 1020 + b"\n#!/bin/sh\n")
    monkeypatch.setenv("PATH", str(tmp_path))
    resolution.validate_host_commands(parse("my-server", ["--port", "1"]), cwd=tmp_path)


# A parent component requires a real preceding directory: a missing entry or
# ordinary file cannot be erased lexically to reach the refused payload.
@pytest.mark.skipif(os.name == "nt", reason="POSIX symlink fixture")
@pytest.mark.parametrize("kind", ["missing", "not-directory"])
def test_invalid_parent_component_retains_portable_result(tmp_path, monkeypatch, kind):
    if kind == "not-directory":
        executable(tmp_path / "gate")
    executable(tmp_path / "payload", b"#!/bin/sh\n")
    (tmp_path / "myedit").symlink_to("gate/../payload")
    monkeypatch.setenv("PATH", str(tmp_path))
    resolution.validate_host_commands(parse("myedit"), cwd=tmp_path)


# Parent traversal is left intact on the selected candidate open. Its known
# shell shebang refuses even beyond the former canonical-root body boundary.
@pytest.mark.skipif(os.name == "nt", reason="POSIX symlink fixture")
@pytest.mark.parametrize("name", ["opaque", "vim"])
def test_parent_component_selected_body_refuses(tmp_path, monkeypatch, name):
    a, b, outside = [tmp_path / name for name in ("a", "b", "outside")]
    for directory in (a, b, outside):
        directory.mkdir()
    (b / "nested").mkdir()
    (a / "hop").symlink_to(b / "nested", target_is_directory=True)
    executable(outside / name, b"#!/bin/sh\n")
    (a / "myedit").symlink_to("hop/../../outside/" + name)
    monkeypatch.setenv("PATH", str(a) + os.pathsep + str(b))
    original = os.open
    opened = []
    def selected_open(path, *args, **kwargs):
        opened.append(Path(path))
        assert Path(path) == a / "myedit"
        return original(path, *args, **kwargs)
    monkeypatch.setattr(resolution.os, "open", selected_open)
    with pytest.raises(mcp.McpDeclarationError, match="mcp_command_invalid"):
        resolution.validate_host_commands(parse("myedit"), cwd=tmp_path)
    assert opened == [a / "myedit"]


# A bounded unresolved chain retains table-only policy. An unreadable selected
# file refuses, without substituting the later known-interpreter executable.
@pytest.mark.skipif(os.name == "nt", reason="POSIX links and permissions")
@pytest.mark.parametrize("kind", ["long-chain", "unreadable"])
def test_resolution_bounded_neighbors(tmp_path, monkeypatch, kind):
    first, later = tmp_path / "first", tmp_path / "later"
    first.mkdir()
    later.mkdir()
    executable(later / "myedit", b"#!/bin/sh\n")
    if kind == "long-chain":
        (first / "myedit").symlink_to("a0")
        for i in range(45):
            (first / ("a" + str(i))).symlink_to("a" + str(i + 1))
        executable(first / "a45", b"#!/bin/sh\n")
        monkeypatch.setenv("PATH", str(first))
    else:
        executable(first / "myedit", b"#!/bin/sh\n").chmod(0o111)
        monkeypatch.setenv("PATH", str(first) + os.pathsep + str(later))
    try:
        if kind == "unreadable":
            with pytest.raises(mcp.McpDeclarationError, match="header cannot be observed"):
                resolution.validate_host_commands(parse("myedit"), cwd=tmp_path)
        else:
            resolution.validate_host_commands(parse("myedit"), cwd=tmp_path)
    finally:
        if kind == "unreadable":
            (first / "myedit").chmod(0o700)


# The review's actual macOS aliases dispatch the obfuscated runner to an owned
# recording stub; check/dry-run/terminal install must instead refuse with no writes.
@pytest.mark.skipif(sys.platform != "darwin", reason="actual macOS system alias dispatch witness")
@pytest.mark.parametrize("command", ["view", "vimdiff"])
def test_default_vim_alias_real_cli_refuses(project_cli, tmp_path, monkeypatch, command):
    import errno
    import pty
    import select
    import time
    root, home, declare = project_cli
    system_alias = Path("/usr/bin") / command
    assert system_alias.is_symlink() and system_alias.resolve() == Path("/usr/bin/vim")
    args = ["-Nu", "NONE", "-i", "NONE", "-n", "-es", "-c", "call system('n'.'px pkg')", "-c", "qa!"]
    declare(stdio(command, args))
    marker = runner_guard(tmp_path, monkeypatch, "/usr/bin")
    env = os.environ.copy()
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
