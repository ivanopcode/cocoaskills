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
        return SimpleNamespace(primary_gid=1000, user_name='alice', members=members)
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
        SimpleNamespace(primary_gid=1000, user_name='alice', members=()), raising=False)
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
        SimpleNamespace(primary_gid=0, user_name='root', members=()), raising=False)
    monkeypatch.setattr(gate, 'linux_metadata', lambda p: SimpleNamespace(st_uid=0,
        st_gid=0, st_mode=(stat.S_IFREG if p == path else stat.S_IFDIR) | state['mode']))
    call = lambda: gate.resolve_launcher(linux(), platform='linux', roots=(tmp_path,),
        verifier=lambda *a: gate.LinuxVerification(True))
    assert call().trust == 'verified'
    state['mode'] = 0o775
    with pytest.raises(gate.McpLauncherError) as caught: call()
    assert caught.value.code == 'mcp_launcher_path_invalid'


# Unix adapters query the uid's account primary gid/name and the requested
# group's explicit member list. Lookup failures are propagated to the admission
# gate, whose table above requires refusal. Fake modules keep this portable.
@pytest.mark.parametrize('failure', [None, 'user', 'group'])
def test_group_metadata_adapter(monkeypatch, failure):
    calls = []
    monkeypatch.setattr(gate, 'linux_uid', lambda: 1000)
    def user(uid):
        calls.append(('user', uid))
        if failure == 'user': raise KeyError(uid)
        return SimpleNamespace(pw_gid=1000, pw_name='alice')
    def group(gid):
        calls.append(('group', gid))
        if failure == 'group': raise OSError('group database unavailable')
        return SimpleNamespace(gr_mem=['alice'])
    monkeypatch.setitem(sys.modules, 'pwd', SimpleNamespace(getpwuid=user))
    monkeypatch.setitem(sys.modules, 'grp', SimpleNamespace(getgrgid=group))
    if failure:
        with pytest.raises((KeyError, OSError)): gate.linux_group_facts(1234)
    else:
        facts = gate.linux_group_facts(1234)
        assert (facts.primary_gid, facts.user_name, facts.members) == (1000, 'alice', ('alice',))
    assert calls == ([('user', 1000)] if failure == 'user' else [('user', 1000), ('group', 1234)])
