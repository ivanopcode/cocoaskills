"""Local ignore placement, explicit migration and deprecated writer boundaries."""
from __future__ import annotations

import errno
import json
from pathlib import Path

import pytest

from conftest import make_project, run, write_skillfile
from csk import cli, config, gitignore_gate, manifest, transactions
from csk.sources.errors import SourceError


@pytest.fixture
def policy(tmp_path, monkeypatch):
    root = make_project(tmp_path, gitignore=False)
    home = tmp_path / 'manager'
    monkeypatch.setenv('CSK_CONFIG', str(home / 'config.json'))
    write_skillfile(root, {'schema_version': 2, 'skills': []})
    return root, home


def switch(root, home, mode, **kwargs):
    from csk import ignore_policy
    return ignore_policy.set_mode(root, home, mode, **kwargs)


def files(root):
    paths = [root / '.gitignore', gitignore_gate.ignore_path(root, 'exclude'), root / 'Skillfile.json']
    return {p: p.read_bytes() if p.exists() else None for p in paths}


# Switching moves only managed rules; surrounding user bytes survive both directions.
@pytest.mark.parametrize('before,after', [(b'# user\r\nuser\r\n', b'!keep\r\n'), (b'', b'last-user-rule')])
def test_switch_preserves_user_bytes(policy, before, after):
    root, home = policy
    path = root / '.gitignore'
    path.write_bytes(before + b'# CocoaSkill\r\n.agents/\r\n' + after)
    exclude = gitignore_gate.ignore_path(root, 'exclude')
    exclude.parent.mkdir(exist_ok=True)
    exclude_user = b'# exclude user \xff\r\n/private'
    exclude.write_bytes(exclude_user)
    switch(root, home, 'exclude')
    assert path.read_bytes() == before + after
    assert gitignore_gate.effective_mode(root, None) == 'exclude'
    assert run(['git', 'check-ignore', '-q', '.agents/probe'], root).returncode == 0
    switch(root, home, 'gitignore')
    assert gitignore_gate.effective_mode(root, None) == 'gitignore'
    assert exclude.read_bytes() == exclude_user
    switch(root, home, 'exclude')
    assert path.read_bytes() == before + after


# Migration deletes one member without reformatting nested/escaped strings or CRLF bytes.
@pytest.mark.parametrize('fragment', [b'"ignore_mode" : "exclude",', b'"ignore\\u005fmode":"exclude",'])
def test_minimal_migration_survives_reload(policy, fragment):
    root, home = policy
    raw = b'{\r\n  ' + fragment + b'\r\n "schema_version":2, "skills": [], "project":{"alias":"ignore_mode"}\r\n}\r\n'
    (root / 'Skillfile.json').write_bytes(raw)
    switch(root, home, 'gitignore')
    assert (root / 'Skillfile.json').read_bytes() == raw.replace(fragment, b'', 1)
    assert manifest.load_manifest(root).ignore_mode is None
    assert gitignore_gate.effective_mode(root, None) == 'gitignore'


# Duplicate keys and unsafe destinations refuse before any of the three policy files changes.
@pytest.mark.parametrize('bad', ['duplicate', 'gitignore', 'exclude', 'skillfile', 'tracked', 'nested'])
def test_refusal_is_unchanged_then_positive(policy, bad):
    root, home = policy
    write_skillfile(root, {'schema_version': 2, 'ignore_mode': 'exclude', 'skills': []})
    path = {'gitignore': root / '.gitignore', 'exclude': root / '.git/info/exclude', 'skillfile': root / 'Skillfile.json'}.get(bad)
    other = root / 'user-policy'
    if path:
        other.write_bytes(path.read_bytes() if path.exists() else b'# user\n')
        path.parent.mkdir(parents=True, exist_ok=True)
        path.unlink(missing_ok=True)
        path.symlink_to(other)
    elif bad == 'duplicate':
        (root / 'Skillfile.json').write_bytes(b'{"schema_version":2,"ignore_mode":"exclude","skills":[],"skills":[]}')
    elif bad == 'tracked':
        (root / '.agents').mkdir()
        (root / '.agents/user').write_text('tracked')
        run(['git', 'add', '-f', '.agents/user'], root)
    else:
        (root / '.codex').mkdir()
        (root / '.codex/.gitignore').write_text('!config.toml\n')
    before = files(root)
    with pytest.raises((gitignore_gate.GitignoreError, manifest.ManifestError, SourceError)) as error:
        switch(root, home, 'exclude')
    expected = {'duplicate': 'skillfile_migration_unsafe', 'exclude': 'git_exclude_invalid'}
    assert error.value.code == expected.get(bad, 'gitignore_required')
    assert files(root) == before
    if path:
        assert other.read_bytes() == before[path]
        path.unlink()
        path.write_bytes(other.read_bytes())
    elif bad == 'tracked':
        run(['git', 'rm', '--cached', '.agents/user'], root)
    elif bad == 'nested':
        (root / '.codex/.gitignore').unlink()
    else:
        write_skillfile(root, {'schema_version': 2, 'ignore_mode': 'exclude', 'skills': []})
    switch(root, home, 'exclude')
    assert manifest.load_manifest(root).ignore_mode is None


# Interrupted publication restores all three preimages through the ordinary recovery engine.
@pytest.mark.parametrize('count', [1, 2, 3])
def test_crash_recovery_rolls_back_all(policy, count):
    root, home = policy
    write_skillfile(root, {'schema_version': 2, 'ignore_mode': 'exclude', 'skills': []})
    (root / '.gitignore').write_bytes(b'# user\r\n')
    switch(root, home, 'exclude')
    write_skillfile(root, {'schema_version': 2, 'ignore_mode': 'exclude', 'skills': []})
    before = files(root)
    seen = 0
    def crash(event, target):
        nonlocal seen
        if event == 'target_committed':
            seen += 1
            if seen == count:
                raise SystemExit('simulated process death')
    with pytest.raises(SystemExit):
        switch(root, home, 'gitignore', fault_hook=crash)
    from csk.locking import ManagerHomeLock
    from csk.sources.publish import make_publication_hook
    with ManagerHomeLock(home) as lock:
        transactions.TransactionEngine(home, pre_write_hook=make_publication_hook()).recover(lock)
    assert files(root) == before
    switch(root, home, 'gitignore')
    assert gitignore_gate.effective_mode(root, None) == 'gitignore'


# Both legacy forms are input-only; normal add refuses unchanged, migrated input permits it.
@pytest.mark.parametrize('form', ['ignore', 'cursor', 'both'])
def test_add_refuses_deprecated_before_any_write(policy, form, capsys):
    root, _ = policy
    data = {'schema_version': 3, 'skills': []}
    if form in ('ignore', 'both'):
        data['ignore_mode'] = 'exclude'
    if form in ('cursor', 'both'):
        data['mcp_servers'] = {'demo': {'server': {'transport': 'stdio', 'command': 'example-server', 'environments': ['claude_code', 'cursor']}}}
    write_skillfile(root, data)
    before = files(root)
    with pytest.raises(SourceError) as error:
        manifest.add_skill_decl(root, name='demo', ref_kind='tag', ref='v1')
    assert getattr(error.value, 'code', None) == 'skillfile_deprecated_form'
    assert 'csk ignore-mode set' in str(error.value) if form != 'cursor' else 'csk mcp migrate' in str(error.value)
    assert files(root) == before
    data.pop('ignore_mode', None)
    data.pop('mcp_servers', None)
    write_skillfile(root, data)
    manifest.add_skill_decl(root, name='demo', ref_kind='tag', ref='v1')
    assert manifest.load_manifest(root).skills[0].name == 'demo'


# CLI warnings deduplicate reloads per command, stay off JSON stdout, and name both remedies.
def test_show_warning_and_json(policy, monkeypatch, capsys):
    root, _ = policy
    write_skillfile(root, {'schema_version': 2, 'ignore_mode': 'exclude', 'skills': []})
    monkeypatch.chdir(root)
    for _ in range(2):
        assert cli.main(['ignore-mode', 'show', '--json']) == 0
        output = capsys.readouterr()
        assert json.loads(output.out) == {'mode': 'exclude', 'deprecated_ignore_mode': 'exclude'}
        assert output.err.count('not portable') == 1
        assert 'csk ignore-mode set' in output.err
    assert cli.main(['ignore-mode', 'set', 'gitignore']) == 0
    capsys.readouterr()
    assert cli.main(['ignore-mode', 'show', '--json']) == 0
    assert json.loads(capsys.readouterr().out)['mode'] == 'gitignore'


# Init emits no deprecated member; an existing deprecated document refuses before ignore edits.
def test_init_never_writes_deprecated(policy, capsys):
    root, _ = policy
    write_skillfile(root, {'schema_version': 2, 'ignore_mode': 'exclude', 'skills': []})
    before = files(root)
    assert cli.main(['init', str(root), '--no-interactive']) == 2
    assert 'skillfile_deprecated_form' in capsys.readouterr().err
    assert files(root) == before
    (root / 'Skillfile.json').unlink()
    assert cli.main(['init', str(root), '--no-interactive']) == 0
    assert 'ignore_mode' not in json.loads((root / 'Skillfile.json').read_bytes())


# Ignore migration alone preserves the entire other deprecated selection byte-for-byte.
def test_other_form_exempt(policy):
    root, home = policy
    raw = b'{"schema_version":3,"ignore_mode":"exclude","skills":[],"mcp_servers":{"demo":{"server":{"transport":"stdio","command":"example-server","environments":[ "claude_code" , "cursor" ]}}}}'
    (root / 'Skillfile.json').write_bytes(raw)
    switch(root, home, 'gitignore')
    assert (root / 'Skillfile.json').read_bytes() == raw.replace(b'"ignore_mode":"exclude",', b'')
    # The other order has already removed Cursor but still permits this migration.
    (root / 'Skillfile.json').write_bytes(raw.replace(b' , "cursor"', b''))
    switch(root, home, 'exclude')
    assert b'"ignore_mode"' not in (root / 'Skillfile.json').read_bytes()


# The token editor preserves opaque bytes around first, middle, last and sole members.
@pytest.mark.parametrize('raw,expected', [
    (b'{ "ignore_mode" : "exclude" }\n', b'{  }\n'),
    (b'{"skills":[],\n "ignore_mode":"exclude"\n}', b'{"skills":[]\n \n}'),
    (b' \n {"schema_version":2, "ignore_mode":"exclude" ,\t"skills":[]}', b' \n {"schema_version":2,  \t"skills":[]}'),
    (b'{"schema_version":2,"skills":[]}', b'{"schema_version":2,"skills":[]}'),
])
def test_member_editor(raw, expected):
    from csk.ignore_policy import remove_ignore_member
    assert remove_ignore_member(raw) == expected


# Duplicate keys anywhere are an explicit unsafe-edit refusal, not last-key-wins deletion.
def test_editor_rejects_duplicate_with_positive_control():
    from csk.ignore_policy import remove_ignore_member
    from csk.sources.errors import SourceError
    good = b'{"ignore_mode":"exclude","skills":[]}'
    assert remove_ignore_member(good) == b'{"skills":[]}'
    with pytest.raises(SourceError) as error:
        remove_ignore_member(good.replace(b'"skills":[]', b'"skills":[],"skills":[]'))
    assert error.value.code == 'skillfile_migration_unsafe'
    assert 'csk ignore-mode set' in str(error.value)


# A shared writer gate rejects both forms, but either explicit migration may preserve the other.
@pytest.mark.parametrize('migration', ['ignore_mode', 'cursor'])
def test_shared_migration_exemptions(migration):
    from csk import skillfile_deprecations as deprecated
    from csk.sources.errors import SourceError
    data = {'ignore_mode': 'exclude', 'mcp_servers': {'one': {'server': {'environments': ['cursor']}}}}
    with pytest.raises(SourceError) as error:
        deprecated.require_writable(data)
    assert error.value.code == 'skillfile_deprecated_form'
    assert all(command in str(error.value) for command in ['csk ignore-mode set', 'csk mcp migrate'])
    deprecated.require_writable(data, migration=migration)
    deprecated.require_writable({'schema_version': 2, 'skills': []})


# Repeated internal loads emit one warning per form in a command, with no stdout bytes.
def test_both_warning_forms_are_once_per_command(policy, capsys):
    from csk import skillfile_deprecations as deprecated
    root, _ = policy
    write_skillfile(root, {'schema_version': 3, 'ignore_mode': 'exclude', 'skills': [],
                          'mcp_servers': {'one': {'server': {'environments': ['cursor']}}}})
    with deprecated.command_warnings():
        manifest.load_manifest(root)
        manifest.load_manifest(root)
    output = capsys.readouterr()
    assert not output.out
    assert output.err.count('not portable') == 2
    assert output.err.count('csk mcp migrate') == 1
    assert output.err.count('run `csk ignore-mode set') == 1


# CLI add refuses before changes, then works on the same project after explicit migration.
def test_cli_add_writer_gate(policy, skills_root, monkeypatch, capsys):
    from conftest import make_config
    root, home = policy
    config.save_config(make_config(home, skills_root, root))
    write_skillfile(root, {'schema_version': 2, 'ignore_mode': 'exclude', 'skills': []})
    before = files(root)
    assert cli.main(['add', 'demo', '--tag', 'v1', '--project', str(root)]) == 2
    assert 'skillfile_deprecated_form' in capsys.readouterr().err
    assert files(root) == before
    switch(root, home, 'exclude')
    assert cli.main(['add', 'demo', '--tag', 'v1', '--project', str(root)]) == 0


# Status --check keeps a clean installed project clean; the warning is stderr-only, JSON remains valid.
def test_status_warning_not_drift(policy, skills_root, capsys):
    from conftest import make_config
    root, home = policy
    config.save_config(make_config(home, skills_root, root))
    write_skillfile(root, {'schema_version': 2, 'ignore_mode': 'exclude', 'skills': []})
    assert cli.main(['install', 'app']) == 0
    capsys.readouterr()
    assert cli.main(['status', 'app', '--check', '--json']) == 0
    output = capsys.readouterr()
    assert isinstance(json.loads(output.out), list)
    assert output.err.count('not portable') == 1
    assert 'csk ignore-mode set' in output.err


# Outside a Git worktree, set refuses with the existing exclude diagnostic and leaves input untouched.
def test_outside_worktree_refuses(tmp_path, policy):
    root, home = policy
    other = tmp_path / 'no-git'
    other.mkdir()
    write_skillfile(other, {'schema_version': 2, 'skills': []})
    original = (other / 'Skillfile.json').read_bytes()
    with pytest.raises(gitignore_gate.ExcludeError) as error:
        switch(other, home, 'exclude')
    assert error.value.code == 'git_exclude_invalid'
    assert (other / 'Skillfile.json').read_bytes() == original
    assert not (other / '.gitignore').exists()
    switch(root, home, 'exclude')


# A concurrent Skillfile edit during verification is retained, never overwritten by a stale plan.
def test_input_edit_during_planning_refuses(policy, monkeypatch):
    root, home = policy
    write_skillfile(root, {'schema_version': 2, 'ignore_mode': 'exclude', 'skills': []})
    before = files(root)
    original = gitignore_gate.verify_planned_ignored
    changed = b'{"schema_version":2,"ignore_mode":"exclude","locale":"en","skills":[]}'
    def edit(*args, **kwargs):
        original(*args, **kwargs)
        (root / 'Skillfile.json').write_bytes(changed)
    monkeypatch.setattr(gitignore_gate, 'verify_planned_ignored', edit)
    with pytest.raises(gitignore_gate.GitignoreError, match='input changed during planning'):
        switch(root, home, 'gitignore')
    expected = dict(before)
    expected[root / 'Skillfile.json'] = changed
    assert files(root) == expected
    monkeypatch.setattr(gitignore_gate, 'verify_planned_ignored', original)
    switch(root, home, 'gitignore')
    assert json.loads((root / 'Skillfile.json').read_bytes())['locale'] == 'en'


# A real child-process exit after a replacement leaves a journal that restores all three files.
def test_process_death_restores_preimages(policy):
    import os
    import subprocess
    import sys
    root, home = policy
    switch(root, home, 'exclude')
    write_skillfile(root, {'schema_version': 2, 'ignore_mode': 'exclude', 'skills': []})
    (root / '.gitignore').write_bytes(b'# user\r\n')
    before = files(root)
    code = '''
import os, sys
from pathlib import Path
from csk.ignore_policy import set_mode
seen = 0
def crash(event, target):
    global seen
    if event == 'after_install':
        seen += 1
        if seen == 2: os._exit(86)
set_mode(Path(sys.argv[1]), Path(sys.argv[2]), 'gitignore', fault_hook=crash)
'''
    env = {**os.environ, 'PYTHONPATH': str(Path(__file__).resolve().parents[1] / 'src')}
    result = subprocess.run([sys.executable, '-c', code, str(root), str(home)], env=env,
                            capture_output=True, text=True, timeout=90)
    assert result.returncode == 86, result.stdout + result.stderr
    assert files(root) != before
    from csk.locking import ManagerHomeLock
    from csk.sources.publish import make_publication_hook
    with ManagerHomeLock(home) as lock:
        transactions.TransactionEngine(home, pre_write_hook=make_publication_hook()).recover(lock)
    assert files(root) == before
    switch(root, home, 'gitignore')
    assert gitignore_gate.effective_mode(root, None) == 'gitignore'


# Adding then removing a shared block retains even user bytes with no final newline.
@pytest.mark.parametrize('user', [b'# user \xff\r\n/private', b'# user\r\n/private\r\n'])
def test_shared_block_roundtrip_preserves_user_bytes(user):
    key = 'a' * 64
    block = f'# CocoaSkill project {key}\n/.agents/\n# End CocoaSkill project {key}\n'.encode()
    added = gitignore_gate.replace_project_block(user, key, block)
    assert gitignore_gate.project_block(added, key) == block
    assert gitignore_gate.replace_project_block(added, key, None) == user


# Policy input I/O faults are typed refusals; restoring access returns the same untouched bytes.
@pytest.mark.parametrize('operation', ['lstat', 'read_bytes'])
@pytest.mark.parametrize('number', [errno.EACCES, errno.EIO, errno.ENAMETOOLONG, errno.ELOOP])
def test_policy_input_io_fault_is_typed(tmp_path, monkeypatch, operation, number):
    from csk.ignore_policy import _regular_bytes
    path = tmp_path / 'Skillfile.json'
    original_bytes = b'{"schema_version":2,"skills":[]}'
    path.write_bytes(original_bytes)
    original = getattr(Path, operation)
    def fail(self, *args, **kwargs):
        if self == path:
            raise OSError(number, 'injected input I/O failure')
        return original(self, *args, **kwargs)
    with monkeypatch.context() as fault:
        fault.setattr(Path, operation, fail)
        with pytest.raises(gitignore_gate.GitignoreError, match='Cannot read ignore policy file'):
            _regular_bytes(path)
    assert _regular_bytes(path) == original_bytes
