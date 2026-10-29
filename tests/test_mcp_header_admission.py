"""Fail-closed selected headers and Windows system-root observations."""
import os
import sys
from types import SimpleNamespace

from mcp_native_fixture import native_header

import pytest

from csk import mcp_command_resolution as resolution, mcp_declarations as mcp
from test_mcp_command_resolution import executable as posix_executable, parse

pytestmark = pytest.mark.usefixtures('stable_env')

def executable(path, body):
    return posix_executable(path.with_name(path.name + ".exe") if os.name == "nt" else path, body)

# Open and read failures refuse the selected candidate (including a runner),
# name only the bare command, and never fall through to a later PATH executable.
@pytest.mark.parametrize('stage', ['open', 'read'])
@pytest.mark.parametrize('command,args', [('my-server', []), ('npx', ['pkg@1.2.3'])])
def test_selected_io_failure_refuses(tmp_path, monkeypatch, stage, command, args):
    first = tmp_path / 'first'; first.mkdir()
    later = tmp_path / 'later'; later.mkdir()
    candidate = executable(first / command, b'\x7fELFfixture')
    executable(later / command, b'\x7fELFlater')
    monkeypatch.setenv('PATH', str(first) + os.pathsep + str(later))
    original = os.open; opened = []
    def observed(path, *a, **kw):
        opened.append(os.fspath(path))
        if stage == 'open': raise PermissionError('fixture')
        return original(path, *a, **kw)
    monkeypatch.setattr(resolution, 'os', SimpleNamespace(**{**vars(resolution.os), 'open': observed}))
    if stage == 'read':
        def denied(*a): raise OSError('fixture')
        monkeypatch.setattr(resolution, 'os', SimpleNamespace(**{**vars(resolution.os), 'read': denied}))
    with pytest.raises(mcp.McpDeclarationError) as caught:
        resolution.validate_host_commands(parse(command, args), cwd=tmp_path)
    assert caught.value.code == 'mcp_command_invalid'
    assert command in str(caught.value) and 'header cannot be observed' in str(caught.value)
    assert str(tmp_path) not in str(caught.value) and opened == [str(candidate)]


# Current-platform opaque fixed headers pass; foreign headers refuse. Fat
# controls include the whole bounded arch table, not just its shared Java magic.
FAT = [(bytes.fromhex('CAFEBABE'), 'big'), (bytes.fromhex('BEBAFECA'), 'little')]
@pytest.mark.parametrize('platform', ['darwin', 'linux', 'win32'])
def test_native_magic_positive(tmp_path, monkeypatch, platform):
    executable(tmp_path / 'my-server', native_header(platform) + b'x' * 2048)
    monkeypatch.setenv('PATH', str(tmp_path))
    if sys.platform == platform:
        resolution.validate_host_commands(parse('my-server'), cwd=tmp_path)
    else:
        with pytest.raises(mcp.McpDeclarationError, match='mcp_command_invalid'):
            resolution.validate_host_commands(parse('my-server'), cwd=tmp_path)


# Empty/text/short/Java-count headers refuse without dispatch on all platforms.
@pytest.mark.parametrize('header', [b'', b'opaque text\n', b'\x7fEL', b'M', b'\0' * 16] +
    [magic + count.to_bytes(4, order) for magic, order in FAT for count in [0, 17, 65, 0xffffffff]] +
    [magic + b'\0\0\0' for magic, order in FAT])
def test_unknown_or_java_header_refuses(tmp_path, monkeypatch, header):
    executable(tmp_path / 'my-server', header)
    monkeypatch.setenv('PATH', str(tmp_path))
    with pytest.raises(mcp.McpDeclarationError, match='mcp_command_invalid'):
        resolution.validate_host_commands(parse('my-server'), cwd=tmp_path)


# On POSIX, 255 and 256 include the newline and pass; 257 refuses.
# No candidate body is inspected past 1024.
@pytest.mark.parametrize('length', [255, 256, 257])
@pytest.mark.skipif(os.name == "nt", reason="POSIX shebang semantics")
def test_shebang_256_boundary(tmp_path, monkeypatch, length):
    interpreter = executable(tmp_path / 'opaque', native_header())
    head = b'#!' + os.fsencode(interpreter) + b' '
    assert len(head) < 254
    executable(tmp_path / 'my-server', head + b'x' * (length - 1 - len(head)) + b'\n')
    monkeypatch.setenv('PATH', str(tmp_path))
    if length > 256:
        with pytest.raises(mcp.McpDeclarationError, match='mcp_command_invalid'):
            resolution.validate_host_commands(parse('my-server'), cwd=tmp_path)
    else:
        resolution.validate_host_commands(parse('my-server'), cwd=tmp_path)


# A missing/unresolvable SystemRoot cannot disable Windows containment, even
# when the declared executable is absent or is a supported pinned runner.
@pytest.mark.parametrize('state', ['unset', 'missing', 'loop', 'permission'])
@pytest.mark.parametrize('command,args', [('my-server', []), ('npx', ['pkg@1.2.3']), ('missing-server', [])])
def test_windows_root_failure_refuses(tmp_path, monkeypatch, state, command, args):
    if command != 'missing-server': executable(tmp_path / command, b'MZfixture')
    root = tmp_path / 'Windows'
    monkeypatch.setattr(resolution, '_is_windows', lambda: True)
    monkeypatch.setenv('PATH', str(tmp_path))
    monkeypatch.delenv('WINDIR', raising=False)
    if state == 'unset': monkeypatch.delenv('SystemRoot', raising=False)
    else:
        monkeypatch.setenv('SystemRoot', str(root))
        if state == 'loop': root.symlink_to(root)
        elif state == 'permission':
            def denied(path): raise PermissionError('fixture')
            monkeypatch.setattr(resolution, '_windows_final_path', denied)
    with pytest.raises(mcp.McpDeclarationError) as caught:
        resolution.validate_host_commands(parse(command, args), cwd=tmp_path)
    assert caught.value.code == 'mcp_command_invalid'
    assert command in str(caught.value) and 'SystemRoot' in str(caught.value)
    assert str(tmp_path) not in str(caught.value)
