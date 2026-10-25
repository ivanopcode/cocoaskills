"""Portable strict Linux launcher modes and actionable refusal controls."""
import os
import shlex
import stat
import subprocess
from pathlib import PurePosixPath
from types import SimpleNamespace

import pytest

from csk import mcp_launchers as gate
from test_mcp_launcher_platforms import helper, linux


# Each chain element and owner first admits a strict 0755 control. Group/world
# write then refuses before package probing, independent of the trust override,
# with the exact element, octal mode and a shell-quoted, bit-specific remedy.
@pytest.mark.parametrize('element', ['file', 'parent', 'root'])
@pytest.mark.parametrize('uid,owner', [(1000, 0), (1000, 1000), (0, 0)], ids=['root-owned', 'user-owned', 'root-caller'])
@pytest.mark.parametrize('mode,remove', [(0o775, 'g-w'), (0o757, 'o-w'), (0o777, 'g-w,o-w')])
def test_strict_linux_modes(tmp_path, monkeypatch, element, uid, owner, mode, remove):
    path = helper(tmp_path, 'Example/bin/server')
    selected = {'file': path, 'parent': path.parent, 'root': tmp_path}[element]
    state = {'mode': 0o755}
    probes = []
    monkeypatch.setattr(gate, 'linux_uid', lambda: uid)
    monkeypatch.setattr(gate, 'linux_metadata', lambda p: SimpleNamespace(st_uid=owner,
        st_mode=(stat.S_IFREG if p == path else stat.S_IFDIR) | (state['mode'] if p == selected else 0o755)))
    def verify(*args):
        probes.append(True)
        return gate.LinuxVerification(False)
    call = lambda allow=True: gate.resolve_launcher(linux(allow), platform='linux', roots=(tmp_path,), verifier=verify)
    assert call().trust == 'unverified'
    probes.clear()
    state['mode'] = mode
    for allow in (True, False):
        with pytest.raises(gate.McpLauncherError) as caught: call(allow)
        assert caught.value.code == 'mcp_launcher_path_invalid'
        message = str(caught.value)
        assert str(selected) in message and f'{mode:04o}' in message
        command = message.split('run ', 1)[1]
        assert shlex.split(command) == ['chmod', '--', remove, str(selected)]
        assert all(char.isprintable() for char in message)
    assert not probes
    state['mode'] = 0o755
    assert call().trust == 'unverified'


# Printable remedies execute unchanged in POSIX sh, removing exactly the named
# bits from the selected file. Other fixture files retain contents, modes and
# modification times, and no extra fixture paths remain after the command.
@pytest.mark.skipif(os.name == 'nt', reason='requires POSIX modes and /bin/sh')
@pytest.mark.parametrize('name', [
    'space name', "single'quote", 'double"quote', 'dollar$(touch injected)',
    '`touch injected`', '-leading-dash', 'éclair',
])
@pytest.mark.parametrize('mode,remove', [(0o777, 0o022), (0o775, 0o020), (0o757, 0o002)])
def test_printable_linux_remedy_executes_in_posix_sh(tmp_path, name, mode, remove):
    target = tmp_path / name
    target.write_bytes(b'unchanged target bytes')
    target.chmod(mode)
    guard = tmp_path / 'guard'
    guard.write_bytes(b'unchanged guard bytes')
    guard.chmod(0o640)

    def snapshot():
        return {p.name: (stat.S_IMODE(p.stat().st_mode), p.read_bytes(), p.stat().st_mtime_ns)
                for p in tmp_path.iterdir()}

    before = snapshot()
    root_mode = stat.S_IMODE(tmp_path.stat().st_mode)
    message = str(gate._linux_mode_error(target, mode))
    command = message.split('run ', 1)[1]
    result = subprocess.run(['/bin/sh', '-c', command], cwd=tmp_path, capture_output=True,
                            text=True, env={'PATH': '/usr/bin:/bin', 'LC_ALL': 'C'})
    assert result.returncode == 0, result.stderr
    expected = dict(before)
    expected[name] = (mode & ~remove, *before[name][1:])
    assert snapshot() == expected
    assert stat.S_IMODE(tmp_path.stat().st_mode) == root_mode


# Control-bearing names are displayed as UTF-8 byte escapes without raw control
# characters. They name the exact mode and required bits but offer no command;
# a shell must never interpret an escaped display name as the actual filename.
@pytest.mark.parametrize('name,escaped', [
    ('line\nname', 'line\\x0aname'),
    ('trailing\n', 'trailing\\x0a'),
    ('tab\tname', 'tab\\x09name'),
    ('c1\x85', 'c1\\xc2\\x85'),
    ('escape\x1b', 'escape\\x1b'),
])
@pytest.mark.parametrize('mode,bits', [(0o777, 'g-w,o-w'), (0o775, 'g-w'), (0o757, 'o-w')])
def test_linux_control_name_diagnostic_has_no_shell_command(name, escaped, mode, bits):
    error = gate._linux_mode_error(PurePosixPath('/safe') / name, mode)
    message = str(error)
    assert error.code == 'mcp_launcher_path_invalid'
    assert '/safe/' + escaped in message and f'{mode:04o}' in message
    assert bits in message
    assert 'no shell command is suggested because the name contains control characters' in message
    assert 'run chmod' not in message and "$'" not in message
    assert all(char.isprintable() for char in message)
