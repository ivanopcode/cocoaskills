"""Reviewed shebang and Apple recipe cases through real CLI and guarded dispatch."""
import json, os, shutil, stat, subprocess
from pathlib import Path
import pytest
from csk import mcp_declarations as mcp
from test_mcp_project_install import project_cli
from test_mcp_install_acceptance import _snapshot
from test_mcp_base_cli import cli, terminal, stdio, executable, _stub, _oracles, _admission, VIM_ARGS

pytestmark = [pytest.mark.usefixtures("stable_env"), pytest.mark.skipif(os.sys.platform != "darwin", reason="macOS reviewed dispatch witnesses")]

def parse(command, args=()):
    return mcp.parse_mcp_servers(stdio(command, list(args)), invocation_scope="project", target_agents=["claude_code"])

# Every executable regular file in the four macOS base roots refuses as command.
def test_host_base_roots_all_refused():
    accepted = []
    for root in ("/usr/bin", "/bin", "/usr/sbin", "/sbin"):
        for name in os.listdir(root):
            p = os.path.join(root, name)
            try: st = os.stat(p)
            except OSError: continue
            if not stat.S_ISREG(st.st_mode) or not os.access(p, os.X_OK) or not mcp._COMMAND_RE.fullmatch(name): continue
            for token in (name, name.upper(), name + ".exe"):
                try: parse(token, ["--port", "1"]); accepted.append((root, token))
                except mcp.McpDeclarationError as exc: assert exc.code == "mcp_command_invalid"
    assert accepted == []

# Bridge exception is exact (20 widening variants refuse).
@pytest.mark.parametrize("command,args", [
    ("XCRUN", ["mcpbridge"]), ("Xcrun", ["mcpbridge"]), ("xcrun.exe", ["mcpbridge"]), ("xcrun.EXE", ["mcpbridge"]),
    ("xcrun", ["MCPBRIDGE"]), ("xcrun", ["mcpbridge", ""]), ("xcrun", ["", "mcpbridge"]), ("xcrun", ["mcpbridge "]),
    ("xcrun", [" mcpbridge"]), ("xcrun", ["./mcpbridge"]), ("xcrun", ["--sdk", "macosx", "mcpbridge"]),
    ("xcrun", ["-r", "mcpbridge"]), ("xcrun", ["-f", "mcpbridge"]), ("xcrun", ["mcpbridge", "--", "npx"]),
    ("xcrun", ["mcpbridge.exe"]), ("xcrun", ["/usr/bin/mcpbridge"]), ("xcrun", ["mcpbridge", "mcpbridge"]),
    ("xcrun", ["-l", "mcpbridge"]), ("xcrun", ["--toolchain", "x", "mcpbridge"]), ("xcrun", [])])
def test_bridge_widening_refused(command, args):
    with pytest.raises(mcp.McpDeclarationError) as caught: parse(command, args)
    assert caught.value.code == "mcp_command_invalid"

# Host aliases into base dirs refuse through the real CLI; a distinct copy stays opaque (decided).
@pytest.mark.parametrize("target,refuse", [("/usr/bin/apply", True), ("/usr/bin/true", True), ("/usr/bin/lockf", True),
    ("/usr/bin/xcrun", True), ("/bin/sh", True), ("/usr/sbin/dtrace", True), ("copy:/usr/bin/true", False)])
def test_alias_into_base_dirs(project_cli, tmp_path, monkeypatch, target, refuse):
    root, home, declare = project_cli
    a = tmp_path / "bin"; a.mkdir()
    if target.startswith("copy:"): shutil.copyfile(target[5:], a / "srvx"); (a / "srvx").chmod(0o700)
    else: (a / "srvx").symlink_to(target)
    _stub(tmp_path, monkeypatch, str(a))
    declare(stdio("srvx", ["--port", "1"]))
    assert cli(root, "check", "app").returncode == (1 if refuse else 0)

# Positive controls (real CLI check + dry-run).
@pytest.mark.parametrize("command,args", [("npx", ["pkg@1.2.3"]), ("uvx", ["pkg==1.2.3"]), ("my-server", ["--port", "1"]), ("xcrun", ["mcpbridge"]), ("safaridriver", ["--mcp"])])
def test_positive_controls_cli(project_cli, tmp_path, monkeypatch, command, args):
    root, home, declare = project_cli
    a = tmp_path / "bin"; a.mkdir(); shutil.copyfile("/usr/bin/true", a / "my-server"); (a / "my-server").chmod(0o700)
    marker = _stub(tmp_path, monkeypatch, str(a) + os.pathsep + "/usr/bin"); declare(stdio(command, args))
    assert cli(root, "check", "app").returncode == 0 and cli(root, "install", "app", "--dry-run").returncode == 0 and not marker.exists()

# A # in the interpreter word can select another program or continue PATH;
# all three guarded dispatchers reach the stub, but admission must refuse.
@pytest.mark.parametrize("kind", ["kernel-runs-vim-alias", "kernel-interp-missing-path-continues"])
def test_hash_terminated_shebang(project_cli, tmp_path, monkeypatch, kind):
    root, home, declare = project_cli
    a = tmp_path / "bin"; a.mkdir(); b = tmp_path / "later"; b.mkdir()
    d = tmp_path / "e#x"; d.mkdir(); shutil.copyfile("/usr/bin/true", d / "opaque"); (d / "opaque").chmod(0o700)
    if kind == "kernel-runs-vim-alias":
        (tmp_path / "e").symlink_to("/usr/bin/vim"); search = str(a)
    else:
        (b / "myedit").symlink_to("/usr/bin/vim"); search = str(a) + os.pathsep + str(b)
    executable(a / "myedit", b"#!" + str(d / "opaque").encode() + b"\n")
    marker = _stub(tmp_path, monkeypatch, search)
    _oracles(root, ["myedit", *VIM_ARGS], marker)          # execvp, Python, libuv all reach the stub
    _admission(root, home, declare, "myedit", VIM_ARGS, marker, kind)   # refusal preserves native configs and the complete snapshot

# 513/600-byte lines and a nested script interpreter refuse before consent or
# writes. Nearby 511-byte and separate-comment controls still pass real CLI.
@pytest.mark.parametrize("kind", ["line-600", "line-513", "nested-interpreter", "line-511-control", "comment-control"])
def test_shebang_line_between_512_and_1024(project_cli, tmp_path, monkeypatch, kind):
    root, home, declare = project_cli
    a = tmp_path / "bin"; a.mkdir(); b = tmp_path / "later"; b.mkdir(); opq = tmp_path / "opq"; opq.mkdir()
    shutil.copyfile("/usr/bin/true", opq / "opaque"); (opq / "opaque").chmod(0o700); (b / "myedit").symlink_to("/usr/bin/vim")
    head = b"#!" + str(opq / "opaque").encode() + b" "
    if kind == "line-600": line = head + b"x" * 600 + b"\n"
    elif kind == "line-513": line = head + b"x" * (512 - len(head)) + b"\n"
    elif kind == "line-511-control": line = head + b"x" * (510 - len(head)) + b"\n"
    elif kind == "comment-control": line = head + b"# comment\n"
    else: executable(opq / "inner", b"#!/usr/bin/true\n"); line = b"#!" + str(opq / "inner").encode() + b"\n"
    executable(a / "myedit", line)
    marker = _stub(tmp_path, monkeypatch, str(a) + os.pathsep + str(b))
    if kind.endswith("control"):
        declare(stdio("myedit", VIM_ARGS)); before = _snapshot(root, home)
        assert cli(root, "check", "app").returncode == 0
        assert cli(root, "install", "app", "--dry-run").returncode == 0
        assert not marker.exists() and _snapshot(root, home) == before
    else:
        _admission(root, home, declare, "myedit", VIM_ARGS, marker, kind)

# Both exact recipes still require the named base file, never a Python alias.
@pytest.mark.parametrize("command", ["xcrun", "safaridriver"])
def test_bridge_host_alias(project_cli, tmp_path, monkeypatch, command):
    root, home, declare = project_cli
    a = tmp_path / "bin"; a.mkdir(); (a / command).symlink_to("/usr/bin/python3")
    _stub(tmp_path, monkeypatch, str(a)); declare(stdio(command, ["mcpbridge"] if command == "xcrun" else ["--mcp"]))
    assert cli(root, "check", "app").returncode == 1      # evaluator aliases cannot receive an Apple recipe exception
