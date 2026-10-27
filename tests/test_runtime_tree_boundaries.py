"""Permission and unreadable-entry boundaries for published runtime operations."""
from dataclasses import replace
import json
import os
from pathlib import Path
import shlex
import shutil
import stat
import subprocess

import pytest

from conftest import commit_all, make_config, make_project, make_skill_repo, run, write_files, write_skillfile
from csk import cli, config, locking, transactions
from csk.sources import lock, publish
from csk.sources.package_identity import package_identity_sha256
from test_build_currentness import _installed_build, _build_row
from protocol_lifecycle_observations import _status_copy_build_root_to_runtime
from test_runtime_tree_writes import tree_state


@pytest.fixture(params=[1, 2, 3, 'global'])
def runtime_scope(tmp_path, monkeypatch, capsys, request):
    scope = request.param
    project = make_project(tmp_path)
    skills = tmp_path / 'skills'
    skills.mkdir()
    home = tmp_path / "manager 'quoted'"
    locking.provision_new_manager_home(home)
    cfg = make_config(home, skills, project, agents=['codex_cli'])
    cfg = replace(cfg, experimental=config.ExperimentalConfig(skillfile_sources=True))
    config.save_config(cfg)
    monkeypatch.setenv('CSK_CONFIG', str(cfg.path))
    monkeypatch.chdir(project)
    files = {'SKILL.md': '---\nname: writer\ndescription: Boundary fixture\n---\n',
             'agent-skill.json': json.dumps({'schema_version': 2, 'runtime_roots': ['scripts'],
                 'commands': {'tool': {'type': 'script', 'unix_path': 'scripts/tool', 'win_path': 'scripts/tool'}}}),
             'scripts/tool': '#!/bin/sh\nprintf original\n', 'scripts/data': 'retained\n'}
    legacy = scope in (1, 'global')
    if legacy:
        source, commit = make_skill_repo(skills, 'writer', files, tag='v1')
        document = {'schema_version': 1, 'skills': [{'name': 'writer', 'tag': 'v1'}]}
    else:
        source = tmp_path / 'source/writer'
        write_files(source, files)
        document = {'schema_version': scope, 'sources': {'local': {'path': str(source.parent)}},
                    'skills': [{'name': 'writer', 'from': 'local', 'directory': 'writer'}]}
    root = home / 'global' if scope == 'global' else project
    root.mkdir(parents=True, exist_ok=True)
    write_skillfile(root, document)
    def invoke(command):
        args = ['global', command] if scope == 'global' else [command, 'app']
        return cli.main(args)
    assert invoke('install') == 0
    capsys.readouterr()
    if legacy:
        runtime = home / 'runtime/writer' / commit
    else:
        member = lock.read_lock((root / 'Skillfile.lock.json').read_bytes()).members[0]
        runtime = publish.runtime_entry_path(home, 'writer', package_identity_sha256(member.package))
    def change_source():
        (source / 'scripts/tool').write_text('#!/bin/sh\nprintf upgraded\n')
        if legacy:
            commit_all(source, 'Update command')
            run(['git', 'tag', 'v2'], source)
            document['skills'][0]['tag'] = 'v2'
            write_skillfile(root, document)
    return project, home, runtime, invoke, change_source


# Unchanged install is a valid control; replacing any supported runtime never broadens an outside hardlink's mode.
@pytest.mark.skipif(os.name != 'posix', reason='POSIX mode boundary')
def test_upgrade_preserves_external_hardlink(runtime_scope, tmp_path):
    _, _, runtime, invoke, change = runtime_scope
    outside = tmp_path / 'external-data'
    os.link(runtime / 'scripts/data', outside)
    assert stat.S_IMODE(outside.stat().st_mode) == 0o444
    assert invoke('install') == 0
    assert stat.S_IMODE(outside.stat().st_mode) == 0o444
    change()
    assert invoke('upgrade') == 0
    assert outside.read_text() == 'retained\n'
    assert stat.S_IMODE(outside.stat().st_mode) == 0o444


# Referenced GC retains the generation; unreferenced GC unlinks it without changing its outside-linked file.
@pytest.mark.skipif(os.name != 'posix', reason='POSIX mode boundary')
def test_gc_preserves_external_hardlink(runtime_scope, tmp_path):
    project, home, runtime, _, _ = runtime_scope
    if not runtime.is_relative_to(home / 'runtime'):
        pytest.skip('Legacy runtime GC; source generation removal is exercised by upgrade')
    outside = tmp_path / 'external-data'
    os.link(runtime / 'scripts/data', outside)
    assert cli.main(['gc']) == 0
    assert runtime.exists() and stat.S_IMODE(outside.stat().st_mode) == 0o444
    context = home / 'global/skills/writer' if (home / 'global/Skillfile.json').exists() else project / '.agents/skills/writer'
    shutil.rmtree(context)
    assert cli.main(['gc']) == 0
    assert not runtime.exists()
    assert outside.read_text() == 'retained\n' and stat.S_IMODE(outside.stat().st_mode) == 0o444


# Both readable and unreadable foreign directories produce the same actionable refusal; the printed remedy alone repairs it.
@pytest.mark.skipif(os.name != 'posix' or os.geteuid() == 0, reason='Unprivileged POSIX permissions')
@pytest.mark.parametrize('command', ['install', 'upgrade'])
@pytest.mark.parametrize('mode', [0o000, 0o400, 0o100])
def test_unreadable_foreign_directory_remedy(runtime_scope, tmp_path, capsys, command, mode):
    project, home, runtime, invoke, _ = runtime_scope
    runtime.chmod(stat.S_IMODE(runtime.stat().st_mode) | stat.S_IWUSR)
    foreign = runtime / "foreign 'quoted'\nentry"
    nested = foreign / 'nested'
    nested.mkdir(parents=True)
    outside = tmp_path / 'external-foreign-data'
    outside.write_text('outside')
    os.link(outside, nested / 'data')
    outside.chmod(0o444)
    assert invoke(command) == 1
    readable = capsys.readouterr()
    assert 'runtime_tree_modified' in readable.out + readable.err
    before = tree_state(project / '.agents', home)
    try:
        nested.chmod(0)
        foreign.chmod(mode)
        assert invoke(command) == 1
        output = capsys.readouterr()
        message = output.out + output.err
        assert 'runtime_tree_modified' in message and 'writer' in message
        assert 'member' in message and '$CSK_HOME/' in message and 'docs/skill-authoring.md' in message
        member = (runtime.name if runtime.is_relative_to(home / 'runtime') else
                  lock.read_lock((project / 'Skillfile.lock.json').read_bytes()).members[0].package.snapshot)
        assert member in message
        assert 'Permission denied' not in message
        assert stat.S_IMODE(foreign.stat().st_mode) == mode
        foreign.chmod(0o755)
        assert stat.S_IMODE(nested.stat().st_mode) == 0
        nested.chmod(0o755)
        # Restore only fixture directory modes to inspect all preserved bytes and entries.
        assert tree_state(project / '.agents', home) == before
        nested.chmod(0)
        foreign.chmod(mode)
        remedy = message.split('with ', 1)[1].split('; then rerun the same command', 1)[0]
        quoted = shlex.quote(str(foreign))
        expected_remedy = (f"chmod u+rwx {quoted} && find -P {quoted} -type d "
                           f"-exec chmod u+rwx {{}} \\; && rm -rf -- {quoted}")
        assert remedy == expected_remedy
        result = subprocess.run(['/bin/sh', '-c', expected_remedy], capture_output=True, text=True)
        assert result.returncode == 0, result.stderr
        assert not foreign.exists() and runtime.is_dir()
        assert outside.read_text() == 'outside' and stat.S_IMODE(outside.stat().st_mode) == 0o444
        assert invoke(command) == 0
    finally:
        if foreign.exists():
            foreign.chmod(0o700)
            nested.chmod(0o700)


# The actual build-only producer is current; its absent or protected runtime corruption plant reaches the real integrity gate.
@pytest.mark.parametrize('existing', [False, True])
def test_protocol_build_root_plant_reaches_gate(tmp_path, monkeypatch, skills_root, csk_home, existing):
    project, cfg, _, marker_path, marker = _installed_build(monkeypatch, tmp_path, skills_root, csk_home)
    status, build = _build_row(cfg)
    assert status.clean and build.current
    runtime = csk_home / 'runtime/build-skill' / marker['commit']
    assert not runtime.exists()
    if existing:
        runtime.mkdir(parents=True)
        runtime.chmod(0o555)
    _status_copy_build_root_to_runtime(project, csk_home, marker_path, marker, monkeypatch)
    assert (runtime / 'build/leak.go').read_text() == 'package main\n'
    if existing and os.name == 'posix':
        assert stat.S_IMODE(runtime.stat().st_mode) == 0o555
    before = tree_state(project, csk_home)
    status, build = _build_row(cfg)
    assert not status.clean and not build.current
    assert 'runtime exposes a declared build root' in build.detail
    assert tree_state(project, csk_home) == before


# Commit, interrupted cleanup recovery and prepared rollback unlink shared files without chmod; unlinked controls complete too.
@pytest.mark.skipif(os.name != 'posix', reason='POSIX mode boundary')
@pytest.mark.parametrize('operation', ['commit', 'recover', 'rollback'])
@pytest.mark.parametrize('shared', [False, True])
def test_transaction_cleanup_preserves_external_inode(tmp_path, operation, shared):
    home, live, desired = (tmp_path / name for name in ('home', 'live', 'desired'))
    for root, value in [(live, 'old'), (desired, 'new')]:
        root.mkdir()
        (root / 'payload').write_text(value)
        (root / 'payload').chmod(0o444)
        root.chmod(0o555)
    class Crash(BaseException):
        pass
    calls = 0
    def interrupt(point, target):
        nonlocal calls
        if operation == 'recover' and point == 'sidecar_mode_writable':
            calls += 1
            if calls == 2:
                raise Crash()
    engine = transactions.TransactionEngine(home, fault_hook=interrupt)
    plan = transactions.TransactionPlan('inode-cleanup', str(tmp_path / 'project'), (
        transactions.MutableTarget('20-runtime', 'writer/member', live, desired,
                                   transactions.digest_path(live)),))
    outside = tmp_path / 'external-data'
    with locking.ManagerHomeLock(home) as witness:
        journal = engine.prepare(witness, plan)
        target = Path(journal.targets[0].staged_path) / 'payload' if operation == 'rollback' else live / 'payload'
        if shared:
            os.link(target, outside)
        else:
            outside.write_bytes(target.read_bytes())
            outside.chmod(0o444)
        expected = outside.read_bytes()
        if operation == 'rollback':
            engine.rollback(witness, plan.transaction_id)
        elif operation == 'recover':
            with pytest.raises(Crash):
                engine.commit(witness, plan.transaction_id)
            assert stat.S_IMODE(outside.stat().st_mode) == 0o444
            transactions.TransactionEngine(home).recover(witness)
        else:
            engine.commit(witness, plan.transaction_id)
    assert (live / 'payload').read_text() == ('old' if operation == 'rollback' else 'new')
    assert outside.read_bytes() == expected and stat.S_IMODE(outside.stat().st_mode) == 0o444
    assert not list(engine.journal_root.glob('*.json'))


# Private names do not authorize shared-inode chmod; exclusive copies accept the same requested permission change.
@pytest.mark.skipif(os.name != 'posix', reason='POSIX hardlink mode boundary')
@pytest.mark.parametrize('operation', ['thaw', 'seal', 'durable'])
def test_private_file_mode_change_requires_exclusive_inode(tmp_path, operation):
    from csk import published_trees
    private = tmp_path / 'private'
    private.mkdir()
    path = private / 'payload'
    path.write_text('retained')
    initial = 0o666 if operation == 'seal' else 0o444
    path.chmod(initial)
    outside = tmp_path / 'outside'
    os.link(path, outside)
    def change(root):
        if operation == 'durable':
            target = root / 'payload'
            entry = transactions._cleanup_tree_entry(target, 'payload', target.lstat())
            transactions._set_entry_mode_durably(target, entry, 0o644)
        elif operation == 'seal':
            published_trees.seal_private_runtime(root)
        else:
            published_trees.make_private_writable(root)
    with pytest.raises(published_trees.RuntimeTreeModified, match='runtime_tree_modified'):
        change(private)
    assert outside.read_text() == 'retained' and stat.S_IMODE(outside.stat().st_mode) == initial
    path.unlink()
    path.write_text('retained')
    path.chmod(initial)
    change(private)
    assert stat.S_IMODE(path.stat().st_mode) == (0o444 if operation == 'seal' else 0o644)
    assert stat.S_IMODE(outside.stat().st_mode) == initial
