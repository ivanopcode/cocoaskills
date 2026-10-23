"""Fail-closed script observations and exact base-file MCP recipes."""
import os
import shutil
from pathlib import Path

import pytest

from csk import mcp_command_resolution as resolution, mcp_declarations as mcp
from test_mcp_command_resolution import executable, parse

pytestmark = pytest.mark.usefixtures('stable_env')
RECIPES = [('xcrun', ['mcpbridge']), ('safaridriver', ['--mcp'])]
MISSED_EXEC = 'dtrace lockstat plockstat sc_usage ktrace parl jjs javac jdb ssh-keygen xcodebuild sg pkexec bwrap dbus-run-session strace ltrace valgrind faketime firejail'.split()


# A # inside the interpreter word refuses, even if the full literal path exists;
# a separate comment argument remains accepted, without emulating a host kernel.
@pytest.mark.parametrize('kind', ['hash-existing', 'hash-truncated-missing', 'nested', 'line-513', 'line-600'])
@pytest.mark.skipif(os.name == "nt", reason="POSIX shebang semantics")
def test_script_divergences_refuse(tmp_path, monkeypatch, kind):
    opq = tmp_path / 'opq'; opq.mkdir()
    interpreter = executable(opq / 'opaque')
    if kind.startswith('hash'):
        d = tmp_path / 'e#x'; d.mkdir()
        interpreter = executable(d / 'opaque')
        if kind == 'hash-existing': (tmp_path / 'e').symlink_to('/usr/bin/vim')
    elif kind == 'nested': interpreter = executable(opq / 'inner', b'#!/usr/bin/true\n')
    line = b'#!' + os.fsencode(interpreter) + b' '
    if kind.startswith('line-'):
        line += b'x' * (int(kind[5:]) - 1 - len(line))
    executable(tmp_path / 'my-server', line + b'\n')
    monkeypatch.setenv('PATH', str(tmp_path))
    with pytest.raises(mcp.McpDeclarationError) as caught:
        resolution.validate_host_commands(parse('my-server'), cwd=tmp_path)
    assert caught.value.code == 'mcp_command_invalid' and 'my-server' in str(caught.value)
    assert str(tmp_path) not in str(caught.value)


@pytest.mark.parametrize('kind', ['comment', 'line-255', 'line-256'])
@pytest.mark.skipif(os.name == "nt", reason="POSIX shebang semantics")
def test_script_boundary_positive(tmp_path, monkeypatch, kind):
    interpreter = executable(tmp_path / 'opaque')
    line = b'#!' + os.fsencode(interpreter) + b' '
    line += b'# comment' if kind == 'comment' else b'x' * (int(kind[5:]) - 1 - len(line))
    candidate = executable(tmp_path / 'my-server', line + b'\n')
    monkeypatch.setenv('PATH', str(tmp_path))
    original_open, original_read = os.open, os.read
    opened, sizes = [], []
    def observe_open(path, *a, **kw):
        opened.append(os.fspath(path)); return original_open(path, *a, **kw)
    def observe_read(fd, count):
        sizes.append(count); return original_read(fd, count)
    monkeypatch.setattr(resolution.os, 'open', observe_open)
    monkeypatch.setattr(resolution.os, 'read', observe_read)
    resolution.validate_host_commands(parse('my-server'), cwd=tmp_path)
    assert opened == [str(candidate), str(interpreter)] and sizes == [1024, 2]


# Neither an evaluator alias, opaque copy nor missing PATH candidate receives a
# recipe exception. Symlinks/hardlinks to the named base file do receive it.
@pytest.mark.skipif(os.name == 'nt', reason='Apple/POSIX base identity fixture')
@pytest.mark.parametrize('command,args', RECIPES)
@pytest.mark.parametrize('kind', ['evaluator', 'copy', 'missing', 'symlink', 'hardlink'])
def test_recipe_selected_identity(tmp_path, monkeypatch, command, args, kind):
    base = tmp_path / 'base'; base.mkdir()
    own = tmp_path / 'bin'; own.mkdir()
    base_file = executable(base / command)
    candidate = own / command
    if kind == 'evaluator': candidate.symlink_to('/usr/bin/python3')
    elif kind == 'copy': shutil.copyfile(base_file, candidate); candidate.chmod(0o700)
    elif kind == 'symlink': candidate.symlink_to(base_file)
    elif kind == 'hardlink': os.link(base_file, candidate)
    monkeypatch.setenv('PATH', str(own))
    monkeypatch.setattr(resolution, '_BASE_DIRECTORIES', (str(base),))
    if kind in {'symlink', 'hardlink'}:
        monkeypatch.setattr(resolution.os, 'open', lambda *a, **kw: pytest.fail('recipe needs metadata only'))
        resolution.validate_host_commands(parse(command, args), cwd=tmp_path)
    else:
        with pytest.raises(mcp.McpDeclarationError, match='mcp_command_invalid'):
            resolution.validate_host_commands(parse(command, args), cwd=tmp_path)


# Recipe matching is literal and complete, not normalized, prefixed or widened.
@pytest.mark.parametrize('command,args', [('safaridriver', []), ('safaridriver', ['--mcp', '--x']),
    ('safaridriver', ['--MCP']), ('safaridriver.exe', ['--mcp']), ('SAFARIDRIVER', ['--mcp']),
    ('safarimcp', ['--mcp']), ('safaridriver', ['--mcp '])])
def test_safari_recipe_variants_refuse(command, args):
    with pytest.raises(mcp.McpDeclarationError, match='mcp_command_invalid'): parse(command, args)


# These independently specified review omissions must refuse as argument words
# and as otherwise opaque script interpreters, without relying on base inodes.
@pytest.mark.parametrize('name', MISSED_EXEC)
@pytest.mark.skipif(os.name == "nt", reason="POSIX shebang semantics")
def test_review_exec_words_refuse(tmp_path, monkeypatch, name):
    with pytest.raises(mcp.McpDeclarationError, match='mcp_command_invalid'):
        parse('my-server', ['--exec=' + name])
    interpreter = executable(tmp_path / name)
    executable(tmp_path / 'my-server', b'#!' + os.fsencode(interpreter) + b'\n')
    monkeypatch.setenv('PATH', str(tmp_path))
    with pytest.raises(mcp.McpDeclarationError, match='mcp_command_invalid'):
        resolution.validate_host_commands(parse('my-server'), cwd=tmp_path)


# Windows containment uses final paths (case/separator/component boundaries),
# including outside aliases into SystemRoot, not a recursive directory walk.
@pytest.mark.parametrize('final,refuse', [(r'C:\Windows\System32\tool.exe', True),
    ('c:/WINDOWS/System32/tool.exe', True), (r'\\?\C:\Windows\System32\tool.exe', True),
    (r'C:\WindowsExtra\tool.exe', False), (r'D:\Windows\tool.exe', False),
    (r'C:\tools\tool.exe', False)])
def test_windows_final_path_containment(tmp_path, monkeypatch, final, refuse):
    executable(tmp_path / 'my-server.exe', b'MZfixture')
    monkeypatch.setenv('PATH', str(tmp_path)); monkeypatch.setenv('SystemRoot', r'C:\Windows')
    monkeypatch.setattr(resolution, '_is_windows', lambda: True)
    monkeypatch.setattr(resolution, '_windows_final_path', lambda p: r'C:\Windows' if p == r'C:\Windows' else final)
    if refuse:
        with pytest.raises(mcp.McpDeclarationError, match='mcp_command_invalid'):
            resolution.validate_host_commands(parse('my-server'), cwd=tmp_path)
    else: resolution.validate_host_commands(parse('my-server'), cwd=tmp_path)


@pytest.mark.parametrize('command,args', [('npx', ['pkg@1.2.3']), ('my-server', ['--port', '1'])])
def test_windows_unlistable_descendants_do_not_block(tmp_path, monkeypatch, command, args):
    system = tmp_path / 'Windows'; system.mkdir()
    forbidden = system / 'ServiceProfiles'; forbidden.mkdir()
    own = tmp_path / 'bin'; own.mkdir(); executable(own / (command + '.exe'), b'MZfixture')
    scan = os.scandir; visited = []
    def guarded(path):
        visited.append(os.fspath(path))
        if Path(path) == system or Path(path).is_relative_to(system):
            raise PermissionError('SystemRoot must never be enumerated')
        return scan(path)
    monkeypatch.setenv('SystemRoot', str(system)); monkeypatch.setenv('PATH', str(own))
    monkeypatch.setattr(resolution, '_is_windows', lambda: True)
    monkeypatch.setattr(resolution.os, 'scandir', guarded)
    resolution.validate_host_commands(parse(command, args), cwd=tmp_path)
    assert all(not Path(p).is_relative_to(system) for p in visited)


# A nested interpreter is inspected once for exactly two bytes, and an unreadable
# header refuses rather than treating the interpreter as an opaque binary.
@pytest.mark.parametrize('kind', ['nested', 'unreadable'])
@pytest.mark.skipif(os.name == "nt", reason="POSIX shebang semantics")
def test_interpreter_header_read_bound(tmp_path, monkeypatch, kind):
    interpreter = executable(tmp_path / 'opaque', b'#!/do-not-follow\n' + b'x' * 2000)
    candidate = executable(tmp_path / 'my-server', b'#!' + os.fsencode(interpreter) + b'\n')
    monkeypatch.setenv('PATH', str(tmp_path))
    original_open, original_read = os.open, os.read
    opened, sizes = [], []
    def bounded_open(path, *a, **kw):
        opened.append(os.fspath(path))
        if os.fspath(path) == str(interpreter) and kind == 'unreadable': raise PermissionError('fixture')
        assert os.fspath(path) in [str(candidate), str(interpreter)]
        return original_open(path, *a, **kw)
    def bounded_read(fd, size):
        sizes.append(size); assert sizes == [1024] or sizes == [1024, 2]
        return original_read(fd, size)
    monkeypatch.setattr(resolution.os, 'open', bounded_open)
    monkeypatch.setattr(resolution.os, 'read', bounded_read)
    with pytest.raises(mcp.McpDeclarationError, match='mcp_command_invalid'):
        resolution.validate_host_commands(parse('my-server'), cwd=tmp_path)
    assert opened == [str(candidate), str(interpreter)]
    assert sizes == ([1024, 2] if kind == 'nested' else [1024])


# Extended UNC prefixes normalize case and separators, with a component boundary.
@pytest.mark.parametrize('final,refuse', [(r'\\?\unc\SERVER\Share\Windows\bin\tool.exe', True),
    (r'\\server\share\WindowsExtra\tool.exe', False)])
def test_windows_unc_boundary(monkeypatch, final, refuse):
    monkeypatch.setenv('SystemRoot', r'\\SERVER\SHARE\Windows')
    monkeypatch.setattr(resolution, '_windows_final_path', lambda p: p if p == os.environ['SystemRoot'] else final)
    assert resolution._windows_base_candidate('alias') is refuse
