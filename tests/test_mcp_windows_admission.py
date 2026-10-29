"""Windows selection and PE admission, using owned files without dispatch."""
import os
from types import SimpleNamespace

import pytest

from csk import cli, mcp_command_resolution as resolution, mcp_declarations as mcp, mcp_project
from test_mcp_command_resolution import parse, stdio
from test_mcp_install_acceptance import _snapshot
from test_mcp_project_install import project_cli

pytestmark = pytest.mark.usefixtures("stable_env")

@pytest.fixture
def windows_host(tmp_path, monkeypatch):
    system = tmp_path / "Windows"; system.mkdir()
    first = tmp_path / "first"; first.mkdir()
    later = tmp_path / "later"; later.mkdir()
    monkeypatch.setattr(resolution, "_is_windows", lambda: True)
    monkeypatch.setenv("SystemRoot", str(system))
    monkeypatch.setenv("PATH", str(first) + os.pathsep + str(later))
    return first, later


# Bare files and PATHEXT scripts are never candidates; per-directory native
# suffix order wins over a later directory. The candidate needs no POSIX X_OK.
@pytest.mark.parametrize("files,command,winner", [
    (["first/my-server", "later/my-server.exe"], "my-server", "later/my-server.exe"),
    (["first/my-server.com", "first/my-server.exe"], "my-server", "first/my-server.com"),
    (["first/my-server.exe", "later/my-server.com"], "my-server", "first/my-server.exe"),
    (["first/my-server.cmd", "first/my-server.bat", "later/my-server.exe"], "my-server", "later/my-server.exe"),
    (["first/my-server.custom", "later/my-server.custom"], "my-server.custom", "first/my-server.custom"),
    (["first/my-server.exe", "later/my-server.exe"], "my-server.exe", "first/my-server.exe"),
    (["first/my-server", "first/my-server.cmd"], "my-server", None),
])
def test_windows_candidate_order(tmp_path, monkeypatch, windows_host, files, command, winner):
    for name in files: (tmp_path / name).write_bytes(b"MZfixture")
    monkeypatch.setenv("PATHEXT", ".BAT;.CMD;.EXE;.COM")
    monkeypatch.setattr(resolution, "os", SimpleNamespace(**{**vars(resolution.os), "access": lambda *a: pytest.fail("Windows never uses X_OK")}))
    selected = resolution._selected_candidate(command, tuple(map(str, windows_host)))
    assert (selected[0] if selected else None) == (str(tmp_path / winner) if winner else None)


# A selected Windows candidate must start MZ. Shebang, ELF, Mach-O, Java, text,
# empty and truncated headers refuse instead of inspecting a POSIX interpreter
# or falling through to a later PE. The PE control proves admission is usable.
@pytest.mark.parametrize("command,args", [("my-server", []), ("uvx", ["pkg==1.2.3"])])
@pytest.mark.parametrize("header,allowed", [(b"MZ", True), (b"MZfixture", True),
    (b"#!/usr/bin/env node\n", False), (b"\x7fELF", False), (bytes.fromhex("CFFAEDFE"), False),
    (bytes.fromhex("CAFEBABE00000001"), False), (b"plain text", False), (b"", False), (b"M", False)])
def test_windows_pe_only(tmp_path, monkeypatch, windows_host, command, args, header, allowed):
    first, later = windows_host
    candidate = first / (command + ".exe"); candidate.write_bytes(header)
    (later / (command + ".exe")).write_bytes(b"MZlater")
    original = os.open; opened = []
    def observe(path, *a, **kw):
        opened.append(os.fspath(path)); return original(path, *a, **kw)
    monkeypatch.setattr(resolution, "os", SimpleNamespace(**{**vars(resolution.os), "open": observe}))
    if allowed:
        resolution.validate_host_commands(parse(command, args), cwd=tmp_path)
    else:
        with pytest.raises(mcp.McpDeclarationError) as caught:
            resolution.validate_host_commands(parse(command, args), cwd=tmp_path)
        assert caught.value.code == "mcp_command_invalid" and "not PE" in str(caught.value)
        assert str(tmp_path) not in str(caught.value)
    assert opened == [str(candidate)]


# Windows batch refusal precedes host observation on check and both install
# paths, keeps its public code and makes no writes or approval prompt.
@pytest.mark.parametrize("command,args", [("npx", ["pkg@1.2.3"]), ("tools.cmd", []), ("tools.bat", []), ("launcher", [])])
@pytest.mark.parametrize("argv", [("check", "app"), ("install", "app", "--dry-run"), ("install", "app")])
def test_windows_batch_before_host(project_cli, monkeypatch, capsys, command, args, argv):
    root, home, declare = project_cli
    import sys
    platform = SimpleNamespace(platform="win32", stdin=sys.stdin, stdout=sys.stdout)
    monkeypatch.setattr(mcp_project, "sys", platform)
    # cli uses other sys members; change its platform view without mutating sys.
    monkeypatch.setattr(cli, "sys", SimpleNamespace(**{**vars(sys), "platform": "win32"}))
    original = resolution.shutil.which
    monkeypatch.setattr(resolution, "shutil", SimpleNamespace(which=lambda cmd: "launcher.cmd" if cmd == "launcher" else original(cmd)))
    monkeypatch.setattr(resolution, "validate_host_commands", lambda *a, **k: pytest.fail("batch check must run first"))
    monkeypatch.setattr("builtins.input", lambda _: pytest.fail("must refuse before consent"))
    declare(stdio(command, args)); before = _snapshot(root, home)
    assert cli.main(list(argv)) == 1
    output = capsys.readouterr()
    assert "mcp_windows_batch_unsupported" in output.out + output.err
    assert _snapshot(root, home) == before
