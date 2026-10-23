"""The committed base-system policy and fail-closed selected-script contract."""
from __future__ import annotations

import json
import os
from pathlib import Path
import runpy
import subprocess

import pytest

from csk import mcp_command_resolution as resolution, mcp_declarations as mcp
from test_mcp_command_resolution import executable, parse

FIXTURE = json.loads((Path(__file__).parent / 'fixtures/mcp/base-system-commands.json').read_text())
NAMES = sorted({mcp._normalize_command(e['name']) for e in FIXTURE['macos']['entries']} | {
    mcp._normalize_command(n) for family in FIXTURE['families'] for n in family['names']})
pytestmark = pytest.mark.usefixtures('stable_env')


# Every committed base-system name is refused in command position, including
# normalized case/suffix variants, even when no host executable is installed.
@pytest.mark.parametrize('name', NAMES)
def test_base_inventory_commands_refuse(name):
    for token in [name, *(name.upper() + suffix for suffix in mcp._EXECUTABLE_SUFFIXES)]:
        with pytest.raises(mcp.McpDeclarationError) as caught:
            parse(token)
        assert caught.value.code == 'mcp_command_invalid'
        assert caught.value.field == 'mcp_servers.bad.server.command'


# Exec-capable inventory words stay refused inside an opaque command's args;
# ordinary protocol arguments and the two exact Apple recipes remain valid.
@pytest.mark.parametrize('name', FIXTURE['exec_capable'])
def test_base_exec_words_refuse(name):
    with pytest.raises(mcp.McpDeclarationError) as caught:
        parse('my-server', ['--tool=' + name.upper() + '.EXE'])
    assert caught.value.code == 'mcp_command_invalid'
    assert caught.value.field == 'mcp_servers.bad.server.args.0'


@pytest.mark.parametrize('command,args', [('xcrun', ['mcpbridge']), ('safaridriver', ['--mcp']), ('npx', ['pkg@1.2.3']),
                                        ('uvx', ['pkg==1.2.3']), ('my-server', ['--port', '1'])])
def test_base_positive_controls(tmp_path, monkeypatch, command, args):
    monkeypatch.setenv('PATH', str(tmp_path))
    if command in {'xcrun', 'safaridriver'}:
        monkeypatch.setattr(resolution, '_is_windows', lambda: False)
        executable(tmp_path / command)
        monkeypatch.setattr(resolution, '_BASE_DIRECTORIES', (str(tmp_path),))
    resolution.validate_host_commands(parse(command, args), cwd=tmp_path)


# Bridge permission is exact, not a prefix, alternate normalized spelling or
# argument that happens to contain the server's name.
@pytest.mark.parametrize('command,args', [('xcrun', []), ('xcrun', ['other']),
    ('xcrun', ['mcpbridge', '--x']), ('XCRUN', ['mcpbridge']), ('xcrun.exe', ['mcpbridge'])])
def test_bridge_variants_refuse(command, args):
    with pytest.raises(mcp.McpDeclarationError, match='mcp_command_invalid'):
        parse(command, args)


# A selected alias of a non-evaluator OS binary is refused by inode even though
# the candidate's name/header are opaque; a distinct copied binary stays opaque.
@pytest.mark.skipif(os.name == 'nt', reason='POSIX OS binary and symlink fixture')
def test_base_selected_inode_refuses(tmp_path, monkeypatch):
    (tmp_path / 'my-server').symlink_to('/usr/bin/true')
    monkeypatch.setenv('PATH', str(tmp_path))
    monkeypatch.setattr(os, 'open', lambda *a, **k: pytest.fail('base identity needs no body read'))
    with pytest.raises(mcp.McpDeclarationError, match='mcp_command_invalid'):
        resolution.validate_host_commands(parse('my-server'), cwd=tmp_path)


# Pinning does not exempt an inode from the base-system rule, while a distinct
# non-base binary copy with an opaque name remains permitted.
@pytest.mark.skipif(os.name == 'nt', reason='POSIX base binary fixture')
@pytest.mark.parametrize('command,args', [('npx', ['pkg@1.2.3']), ('uvx', ['pkg==1.2.3'])])
def test_pinned_runner_base_inode_refuses(tmp_path, monkeypatch, command, args):
    (tmp_path / command).symlink_to('/usr/bin/true')
    monkeypatch.setenv('PATH', str(tmp_path))
    with pytest.raises(mcp.McpDeclarationError, match='mcp_command_invalid'):
        resolution.validate_host_commands(parse(command, args), cwd=tmp_path)


@pytest.mark.skipif(os.name == 'nt', reason='POSIX base binary fixture')
def test_distinct_non_base_copy_positive(tmp_path, monkeypatch):
    import shutil
    shutil.copyfile('/usr/bin/true', tmp_path / 'my-server')
    (tmp_path / 'my-server').chmod(0o700)
    monkeypatch.setenv('PATH', str(tmp_path))
    resolution.validate_host_commands(parse('my-server'), cwd=tmp_path)


# jq is a portable inventory-name refusal even for a separately installed
# non-base copy; only the two exact Apple recipes receive a base-file exception.
def test_jq_inventory_name_refuses(tmp_path, monkeypatch):
    executable(tmp_path / 'jq')
    monkeypatch.setenv('PATH', str(tmp_path))
    with pytest.raises(mcp.McpDeclarationError, match='mcp_command_invalid'):
        parse('jq', ['.'])


# Broken interpreter variants fail closed at the selected script rather than
# falling through, and invalid path bytes are never silently substituted.
@pytest.mark.parametrize('kind', ['missing', 'crlf', 'directory', 'not-executable', 'invalid-utf8', 'empty', 'quoted'])
@pytest.mark.skipif(os.name == "nt", reason="POSIX executable-mode and shebang contract")
def test_selected_interpreter_refuses(tmp_path, monkeypatch, kind):
    opaque = executable(tmp_path / 'opaque')
    raw = os.fsencode(opaque)
    if kind == 'missing': raw = os.fsencode(tmp_path / 'missing')
    elif kind == 'crlf': raw += b'\r'
    elif kind == 'directory': raw = os.fsencode(tmp_path)
    elif kind == 'not-executable': opaque.chmod(0o600)
    elif kind == 'invalid-utf8': raw += b'\xff'
    elif kind == 'empty': raw = b''
    elif kind == 'quoted': raw = b'"' + raw + b'"'
    candidate = executable(tmp_path / 'my-server', b'#!' + raw + b'\n')
    monkeypatch.setenv('PATH', str(tmp_path))
    with pytest.raises(mcp.McpDeclarationError) as caught:
        resolution.validate_host_commands(parse('my-server'), cwd=tmp_path)
    assert caught.value.code == 'mcp_command_invalid'
    assert "my-server" in str(caught.value)
    assert str(tmp_path) not in str(caught.value)
    assert candidate.read_bytes() == b'#!' + raw + b'\n'


# A runnable opaque interpreter wins over a later evaluator; spaces after #!
# and interpreter arguments do not change the raw interpreter's identity.
@pytest.mark.parametrize('prefix,tail', [(b'', b''), (b' \t', b' --flag')])
@pytest.mark.skipif(os.name == "nt", reason="POSIX executable-mode and shebang contract")
def test_runnable_interpreter_positive(tmp_path, monkeypatch, prefix, tail):
    executable(tmp_path / 'my-server', b'#!' + prefix + b'/usr/bin/true' + tail + b'\n')
    later = tmp_path / 'later'; later.mkdir()
    executable(later / 'my-server', b'#!/bin/sh\n')
    monkeypatch.setenv('PATH', str(tmp_path) + os.pathsep + str(later))
    resolution.validate_host_commands(parse('my-server', ['--port', '1']), cwd=tmp_path)


# The selected header is limited to 1024 bytes. Missing newline refuses; a
# known interpreter beyond the old 256-byte window also refuses.
@pytest.mark.parametrize('header', [b'#!' + b' ' * 1022, b'#!' + b' ' * 300 + b'/usr/bin/vim\n'], ids=['unterminated', 'padded'])
@pytest.mark.skipif(os.name == "nt", reason="POSIX executable-mode and shebang contract")
def test_selected_long_header_refuses(tmp_path, monkeypatch, header):
    executable(tmp_path / 'my-server', header)
    monkeypatch.setenv('PATH', str(tmp_path))
    with pytest.raises(mcp.McpDeclarationError, match='mcp_command_invalid'):
        resolution.validate_host_commands(parse('my-server'), cwd=tmp_path)


# Searchable but non-enumerable PATH directories cannot silently drop versioned
# or mixed-case evaluator identities: refuse the incomplete observation.
@pytest.mark.skipif(os.name == "nt", reason="POSIX executable-mode and shebang contract")
def test_non_enumerable_versioned_identity_refuses(tmp_path, monkeypatch):
    executable(tmp_path / 'PyThOn3.12')
    os.link(tmp_path / 'PyThOn3.12', tmp_path / 'my-server')
    scan = os.scandir
    def denied(path):
        if os.fspath(path) == str(tmp_path): raise PermissionError('fixture')
        return scan(path)
    monkeypatch.setattr(resolution.os, 'scandir', denied)
    monkeypatch.setenv('PATH', str(tmp_path))
    with pytest.raises(mcp.McpDeclarationError, match='mcp_command_invalid'):
        resolution.validate_host_commands(parse('my-server'), cwd=tmp_path)


# Provenance is committed independently of runtime data; a planted owned scan
# includes all executable names, excludes directories, and never reads bodies.
@pytest.mark.skipif(os.name == "nt", reason="POSIX executable-mode and shebang contract")
def test_base_inventory_provenance_and_scanner(tmp_path, monkeypatch):
    from csk.mcp_base_commands import BASE_SYSTEM_COMMANDS, EXEC_CAPABLE_BASE_COMMANDS
    assert set(NAMES) == BASE_SYSTEM_COMMANDS
    assert set(FIXTURE['exec_capable']) == EXEC_CAPABLE_BASE_COMMANDS
    assert FIXTURE['macos']['roots'] == ['/usr/bin', '/bin', '/usr/sbin', '/sbin']
    assert FIXTURE['date'] and len(FIXTURE['macos']['prior_alias_snapshot_sha256']) == 64
    assert {'apply', 'lockf', 'xcrun', 'sandbox-exec', 'ssh-agent', 'screen', 'tmux'} <= set(FIXTURE['exec_capable'])
    assert {'jq', 'true', 'ls', 'taskpolicy', 'netsh', 'cmd'} <= set(NAMES)
    assert all(x['source'].startswith('https://') and len(x['source_sha256']) == 64 for x in FIXTURE['families'])
    executable(tmp_path / 'not-an-evaluator')
    (tmp_path / 'not-executable').write_bytes(b'opaque')
    (tmp_path / 'directory').mkdir()
    scanner = runpy.run_path(str(Path(__file__).parents[1] / '.scripts/scan_mcp_base_commands.py'))
    monkeypatch.setattr(os, 'open', lambda *a, **k: pytest.fail('scanner must not open bodies'))
    monkeypatch.setattr(subprocess, 'run', lambda *a, **k: pytest.fail('scanner must not execute'))
    result = scanner['scan']((str(tmp_path),))
    assert [x['name'] for x in result['entries']] == ['not-an-evaluator'] and not result['errors']
