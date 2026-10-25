"""Portable strict Linux launcher modes and actionable refusal controls."""
import shlex
import stat
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
        assert shlex.split(command) == ['chmod', remove, '--', str(selected)]
        assert all(char.isprintable() for char in message)
    assert not probes
    state['mode'] = 0o755
    assert call().trust == 'unverified'


# Diagnostic paths are displayed without control characters. Ordinary shell
# punctuation remains one chmod operand; control-bearing names use explicit
# ANSI-C byte escapes rather than embedding terminal controls in the message.
@pytest.mark.parametrize('name,quoted', [
    ('space and $dollar;quote\'', "'/safe/space and $dollar;quote'\"'\"''"),
    ('line\n\t\x1b', "$'/safe/line\\x0a\\x09\\x1b'"),
    ('c1\x85', "$'/safe/c1\\xc2\\x85'"),
])
def test_linux_mode_message_escapes_controls(name, quoted):
    from pathlib import PurePosixPath
    error = gate._linux_mode_error(PurePosixPath('/safe') / name, 0o777)
    message = str(error)
    assert error.code == 'mcp_launcher_path_invalid'
    assert quoted in message
    assert message.endswith('run chmod g-w,o-w -- ' + quoted)
    assert all(char.isprintable() for char in message)
