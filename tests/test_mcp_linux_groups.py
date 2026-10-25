"""Portable Linux private-primary-group permission and trust controls."""
import stat
import sys
from types import SimpleNamespace

import pytest

from csk import mcp_launchers as gate
from test_mcp_launcher_platforms import helper, linux


# Each element first passes with ordinary non-writable modes. Only a current
# user's private primary group may then write it; every rejected variant must
# fail with the path error, independently of allow-unverified or package facts.
@pytest.mark.parametrize('element', ['file', 'parent', 'root'])
@pytest.mark.parametrize('case', ['empty', 'self', 'shared', 'other', 'duplicate',
    'different-gid', 'lookup-failure', 'world', 'root-owned', 'foreign-owner'])
def test_private_group_table(tmp_path, monkeypatch, element, case):
    path = helper(tmp_path, 'Example/bin/server')
    selected = {'file': path, 'parent': path.parent, 'root': tmp_path}[element]
    state = {'changed': False}
    lookups = []
    monkeypatch.setattr(gate, 'linux_uid', lambda: 1000)
    def metadata(p):
        mode = (stat.S_IFREG if p == path else stat.S_IFDIR) | 0o755
        uid, gid = 1000, 1000
        if state['changed'] and p == selected:
            mode |= 0o020
            if case == 'world': mode |= 0o002
            if case == 'different-gid': gid = 2000
            if case == 'root-owned': uid = 0
            if case == 'foreign-owner': uid = 2000
        return SimpleNamespace(st_mode=mode, st_uid=uid, st_gid=gid)
    def groups(gid):
        lookups.append(gid)
        if case == 'lookup-failure': raise KeyError(gid)
        members = {'empty': (), 'self': ('alice',), 'shared': ('alice', 'bob'),
                   'other': ('bob',), 'duplicate': ('alice', 'alice')}.get(case, ())
        return SimpleNamespace(primary_gid=1000, user_name='alice', members=members, exclusive=True)
    monkeypatch.setattr(gate, 'linux_metadata', metadata)
    monkeypatch.setattr(gate, 'linux_group_facts', groups, raising=False)
    call = lambda: gate.resolve_launcher(linux(), platform='linux', roots=(tmp_path,),
        verifier=lambda *a: gate.LinuxVerification(False))
    assert call().trust == 'unverified'
    assert not lookups  # Ordinary modes require no group-database availability.
    state['changed'] = True
    if case in {'empty', 'self'}:
        assert call().trust == 'unverified' and lookups == [1000]
    else:
        with pytest.raises(gate.McpLauncherError) as caught: call()
        assert caught.value.code == 'mcp_launcher_path_invalid'


# Even a root-owned package helper cannot be verified through a group-writable
# user ancestor. The 0755 control verifies, then 0775 requires opt-in, skips the
# package probe and stays unverified; repairing the mode restores verification.
@pytest.mark.parametrize('element', ['parent', 'root'])
def test_private_group_cannot_verify_package(tmp_path, monkeypatch, element):
    path = helper(tmp_path, 'Example/bin/server')
    selected = path.parent if element == 'parent' else tmp_path
    state = {'writable': False}
    probes = []
    monkeypatch.setattr(gate, 'linux_uid', lambda: 1000)
    monkeypatch.setattr(gate, 'linux_group_facts', lambda gid:
        SimpleNamespace(primary_gid=1000, user_name='alice', members=(), exclusive=True), raising=False)
    def metadata(p):
        mode = (stat.S_IFREG if p == path else stat.S_IFDIR) | 0o755
        if p == selected and state['writable']: mode |= 0o020
        return SimpleNamespace(st_uid=0 if p == path else 1000, st_gid=1000, st_mode=mode)
    monkeypatch.setattr(gate, 'linux_metadata', metadata)
    def verify(*args):
        probes.append(True)
        return gate.LinuxVerification(True)
    def call(allow=True):
        return gate.resolve_launcher(linux(allow), platform='linux', roots=(tmp_path,), verifier=verify)
    assert call().trust == 'verified' and probes == [True]
    probes.clear(); state['writable'] = True
    with pytest.raises(gate.McpLauncherError) as caught: call(False)
    assert caught.value.code == 'mcp_launcher_untrusted'
    assert call().trust == 'unverified' and not probes
    state['writable'] = False
    assert call().trust == 'verified' and probes == [True]


# Running as uid zero does not turn a root-owned writable element into the
# private-group exception. A strict-mode root-owned control still verifies.
def test_root_user_keeps_strict_modes(tmp_path, monkeypatch):
    path = helper(tmp_path, 'Example/bin/server')
    state = {'mode': 0o755}
    monkeypatch.setattr(gate, 'linux_uid', lambda: 0)
    monkeypatch.setattr(gate, 'linux_group_facts', lambda gid:
        SimpleNamespace(primary_gid=0, user_name='root', members=(), exclusive=True), raising=False)
    monkeypatch.setattr(gate, 'linux_metadata', lambda p: SimpleNamespace(st_uid=0,
        st_gid=0, st_mode=(stat.S_IFREG if p == path else stat.S_IFDIR) | state['mode']))
    call = lambda: gate.resolve_launcher(linux(), platform='linux', roots=(tmp_path,),
        verifier=lambda *a: gate.LinuxVerification(True))
    assert call().trust == 'verified'
    state['mode'] = 0o775
    with pytest.raises(gate.McpLauncherError) as caught: call()
    assert caught.value.code == 'mcp_launcher_path_invalid'


# The adapter queries the current account, explicit membership and every account's
# primary gid. Lookup failures propagate to the gate, which refuses group write.
@pytest.mark.parametrize('failure', [None, 'user', 'group', 'enumeration'])
def test_group_metadata_adapter(monkeypatch, failure):
    calls = []
    monkeypatch.setattr(gate, 'linux_uid', lambda: 1000)
    monkeypatch.setattr(gate, 'linux_nsswitch', lambda: 'passwd: files\ngroup: files\n', raising=False)
    alice = SimpleNamespace(pw_uid=1000, pw_gid=1000, pw_name='alice')
    def user(uid):
        calls.append(('user', uid))
        if failure == 'user': raise KeyError(uid)
        return alice
    def group(gid):
        calls.append(('group', gid))
        if failure == 'group': raise OSError('group database unavailable')
        return SimpleNamespace(gr_mem=['alice'])
    def users():
        calls.append(('enumeration',))
        if failure == 'enumeration': raise OSError('account enumeration unavailable')
        return [alice]
    monkeypatch.setitem(sys.modules, 'pwd', SimpleNamespace(getpwuid=user, getpwall=users))
    monkeypatch.setitem(sys.modules, 'grp', SimpleNamespace(getgrgid=group))
    if failure:
        with pytest.raises((KeyError, OSError)): gate.linux_group_facts(1000)
    else:
        facts = gate.linux_group_facts(1000)
        assert (facts.primary_gid, facts.user_name, facts.members, facts.exclusive) == (1000, 'alice', ('alice',), True)
    expected = [('user', 1000), ('group', 1000), ('enumeration',)]
    end = {'user': 1, 'group': 2}.get(failure, 3)
    assert calls == expected[:end]


# Real parsing and the account adapter are exercised through the resolver at
# each chain position. Strict modes remain usable without NSS; group write
# refuses incomplete/shared identity facts and accepts the local-only controls.
@pytest.mark.parametrize('element', ['file', 'parent', 'root'])
@pytest.mark.parametrize('case,allowed', [
    ('files', True), ('systemd', True), ('compat', True), ('actions-comments', True),
    ('other-primary-gid', True), ('shared-primary-empty', False), ('shared-primary-self', False),
    ('explicit-shared', False), ('sss-passwd', False), ('ldap-group', False),
    ('nis', False), ('winbind', False), ('unknown', False), ('missing', False),
    ('unreadable', False), ('empty', False), ('missing-group', False), ('empty-passwd', False),
    ('duplicate-passwd', False), ('broken-action', False), ('enumeration-error', False),
    ('user-error', False), ('group-error', False), ('missing-current-account', False),
    ('same-uid-alias', False),
])
def test_group_exclusivity_table(tmp_path, monkeypatch, element, case, allowed):
    path = helper(tmp_path, 'Example/bin/server')
    selected = {'file': path, 'parent': path.parent, 'root': tmp_path}[element]
    alice = SimpleNamespace(pw_uid=1000, pw_gid=1000, pw_name='alice')
    bob = SimpleNamespace(pw_uid=2000, pw_gid=2000, pw_name='bob')
    sources = {
        'systemd': 'passwd: files systemd\ngroup: files systemd\n',
        'compat': 'passwd: compat\ngroup: files compat\n',
        'actions-comments': '# passwd: ldap\npasswd: files [SUCCESS=return] systemd # ldap\ngroup: files [NOTFOUND = return] systemd\n',
        'sss-passwd': 'passwd: files sss\ngroup: files\n',
        'ldap-group': 'passwd: files\ngroup: files ldap\n',
        'nis': 'passwd: files nis\ngroup: files\n',
        'winbind': 'passwd: files\ngroup: files winbind\n',
        'unknown': 'passwd: files future-provider\ngroup: files\n',
        'empty': '', 'missing-group': 'passwd: files\n',
        'empty-passwd': 'passwd: [SUCCESS=return]\ngroup: files\n',
        'duplicate-passwd': 'passwd: files\npasswd: systemd\ngroup: files\n',
        'broken-action': 'passwd: files [SUCCESS=return\ngroup: files\n',
    }
    lookups = []
    def nsswitch():
        lookups.append('nsswitch')
        if case == 'missing': raise FileNotFoundError('nsswitch absent')
        if case == 'unreadable': raise PermissionError('nsswitch unreadable')
        return sources.get(case, 'passwd: files\ngroup: files\n')
    def user(uid):
        lookups.append('user')
        if case == 'user-error': raise KeyError(uid)
        return alice
    def group(gid):
        lookups.append('group')
        if case == 'group-error': raise OSError('group lookup failed')
        members = ['alice', 'bob'] if case == 'explicit-shared' else ['alice'] if case == 'shared-primary-self' else []
        return SimpleNamespace(gr_mem=members)
    def users():
        lookups.append('enumeration')
        if case == 'enumeration-error': raise OSError('enumeration failed')
        if case == 'missing-current-account': return [bob]
        if case.startswith('shared-primary'): bob.pw_gid = 1000
        if case == 'same-uid-alias': bob.pw_uid, bob.pw_gid = 1000, 1000
        return [alice, bob] if case in {'other-primary-gid', 'shared-primary-empty', 'shared-primary-self', 'same-uid-alias'} else [alice]
    monkeypatch.setattr(gate, 'linux_uid', lambda: 1000)
    monkeypatch.setattr(gate, 'linux_nsswitch', nsswitch, raising=False)
    monkeypatch.setitem(sys.modules, 'pwd', SimpleNamespace(getpwuid=user, getpwall=users))
    monkeypatch.setitem(sys.modules, 'grp', SimpleNamespace(getgrgid=group))
    monkeypatch.setattr(gate, 'linux_metadata', lambda p: SimpleNamespace(
        st_uid=1000, st_gid=1000, st_mode=p.stat().st_mode))
    call = lambda: gate.resolve_launcher(linux(), platform='linux', roots=(tmp_path,))
    assert call().trust == 'unverified'
    assert not lookups
    selected.chmod(0o775)
    if allowed:
        assert call().trust == 'unverified'
        assert 'enumeration' in lookups
    else:
        with pytest.raises(gate.McpLauncherError) as caught: call()
        assert caught.value.code == 'mcp_launcher_path_invalid'
    selected.chmod(0o755)
    assert call().trust == 'unverified'


# NSS configuration comes from the fixed system file with explicit text decoding.
# A read failure propagates so admission can retain the strict permission rule.
@pytest.mark.parametrize('failure', [False, True])
def test_nsswitch_reader(monkeypatch, failure):
    from pathlib import Path
    seen = []
    def read(path, *, encoding):
        seen.append((str(path), encoding))
        if failure: raise PermissionError('unreadable')
        return 'passwd: files\ngroup: files\n'
    monkeypatch.setattr(Path, 'read_text', read)
    if failure:
        with pytest.raises(PermissionError): gate.linux_nsswitch()
    else:
        assert gate.linux_nsswitch() == 'passwd: files\ngroup: files\n'
    assert seen == [('/etc/nsswitch.conf', 'utf-8')]
