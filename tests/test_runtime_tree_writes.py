"""Published runtime trees reject foreign entries and remain immutable to skills."""
from __future__ import annotations

from dataclasses import replace
import json
import os
from pathlib import Path
import shlex
import shutil
import stat
import subprocess
import sys
from types import SimpleNamespace

import pytest

from conftest import commit_all, make_config, make_project, make_skill_repo, run, write_files, write_skillfile
from csk import cli, config, gc, locking, published_trees, transactions
from csk.sources import lock as source_lock, publish
from csk.sources.package_identity import package_identity_sha256


def tree_state(*roots):
    state = {}
    for root in roots:
        if not root.exists():
            continue
        for path in (root, *root.rglob('*')):
            info = path.lstat()
            value = (path.read_bytes() if stat.S_ISREG(info.st_mode) else
                     os.readlink(path) if stat.S_ISLNK(info.st_mode) else None)
            # Stable lock ownership tokens are permitted coordination telemetry.
            if isinstance(value, bytes) and b'"protocol":"csk-stable-file-lock-v1"' in value:
                value = b'stable-lock-owner'
            state[str(path)] = (stat.S_IMODE(info.st_mode), stat.S_IFMT(info.st_mode), value)
    return state


def writable_directories(root):
    for path in (root, *root.rglob('*')):
        if stat.S_ISDIR(path.lstat().st_mode):
            path.chmod(stat.S_IMODE(path.lstat().st_mode) | 0o700)


@pytest.fixture(params=[(1, True), (1, False), (2, True), (2, False)])
def installed_runtime(tmp_path, monkeypatch, capsys, request):
    schema, roots = request.param
    project = make_project(tmp_path)
    skills = tmp_path / 'skills'
    skills.mkdir()
    home = tmp_path / "manager with 'quotes'"
    locking.provision_new_manager_home(home)
    files = {
        'SKILL.md': '---\nname: writer\ndescription: Runtime fixture\n---\n',
        'agent-skill.json': json.dumps({'schema_version': 2, 'runtime_roots': ['scripts'] if roots else [],
                                      'commands': {'write-tool': {'type': 'script',
                                          'unix_path': 'scripts/tool', 'win_path': 'scripts/tool'}}}),
        'scripts/tool': '#!/bin/sh\nprintf original\n',
        'scripts/library.py': 'VALUE = 7\n',
    }
    cfg = make_config(home, skills, project, agents=['codex_cli'])
    cfg = replace(cfg, experimental=config.ExperimentalConfig(skillfile_sources=True))
    if schema == 1:
        _, commit = make_skill_repo(skills, 'writer', files, tag='v1')
        document = {'schema_version': 1, 'skills': [{'name': 'writer', 'tag': 'v1'}]}
        runtime = home / 'runtime/writer' / commit
    else:
        source = tmp_path / 'source'
        write_files(source / 'writer', files)
        document = {'schema_version': 2, 'sources': {'local': {'path': str(source)}},
                    'skills': [{'name': 'writer', 'from': 'local', 'directory': 'writer'}]}
        runtime = None
        commit = None
    write_skillfile(project, document)
    config.save_config(cfg)
    monkeypatch.chdir(project)
    monkeypatch.setenv('CSK_CONFIG', str(cfg.path))
    assert cli.main(['install', 'app']) == 0
    capsys.readouterr()
    if schema == 2:
        member = source_lock.read_lock((project / 'Skillfile.lock.json').read_bytes()).members[0]
        key = package_identity_sha256(member.package)
        runtime = publish.runtime_entry_path(home, 'writer', key)
    assert runtime.is_dir()
    return project, home, runtime, commit, cfg


# Each foreign-entry refusal preserves published state; deleting only the named entry makes the same command succeed.
@pytest.mark.parametrize('command', ['install', 'upgrade'])
@pytest.mark.parametrize('foreign_kind', ['venv', 'file', 'fifo', 'directory'])
def test_runtime_foreign_entry_is_actionable_and_read_only(installed_runtime, capsys, command, foreign_kind):
    project, home, runtime, commit, _ = installed_runtime
    if foreign_kind == 'fifo' and not hasattr(os, 'mkfifo'):
        pytest.skip('FIFO creation requires POSIX')
    # Simulate a tree written by a pre-protection installation, not a writable newly published tree.
    writable_directories(runtime)
    foreign = runtime / ('.venv' if foreign_kind == 'venv' else 'foreign entry')
    offending = foreign
    if foreign_kind == 'venv':
        (foreign / 'bin').mkdir(parents=True)
        offending = foreign / 'bin/python'
        offending.symlink_to(sys.executable)
    elif foreign_kind == 'file':
        foreign.write_bytes(b'foreign state')
    elif foreign_kind == 'fifo':
        os.mkfifo(foreign)
    else:
        foreign.mkdir()
    before = tree_state(project / '.agents', home, project / 'Skillfile.lock.json')
    assert cli.main([command, 'app']) == 1
    output = capsys.readouterr()
    diagnostic = output.out + output.err
    assert 'runtime_tree_modified' in diagnostic
    assert 'writer' in diagnostic and offending.name in diagnostic
    if commit is not None:
        assert commit in diagnostic
    assert 'docs/skill-authoring.md' in diagnostic
    if os.name == 'nt':
        assert 'Remove-Item -LiteralPath' in diagnostic
    else:
        assert f'rm -rf -- {shlex.quote(str(foreign))}' in diagnostic
    assert tree_state(project / '.agents', home, project / 'Skillfile.lock.json') == before
    if foreign.is_dir() and not foreign.is_symlink():
        shutil.rmtree(foreign)
    else:
        foreign.unlink()
    assert cli.main([command, 'app']) == 0
    capsys.readouterr()
    assert not foreign.exists()
    if os.name == 'posix':
        assert all(not path.stat().st_mode & 0o222 for path in (runtime, *runtime.rglob('*')))


# POSIX publication clears every write bit, preserving execution; an ordinary child can write outside but gets EACCES inside.
@pytest.mark.skipif(os.name != 'posix' or os.geteuid() == 0, reason='Unprivileged POSIX write protection')
def test_published_runtime_rejects_skill_process_writes(installed_runtime, tmp_path):
    _, _, runtime, _, _ = installed_runtime
    paths = [runtime, *runtime.rglob('*')]
    assert all(not path.stat().st_mode & 0o222 for path in paths)
    command = runtime / 'scripts/tool' if (runtime / 'scripts').exists() else runtime / 'bin/write-tool'
    assert command.stat().st_mode & 0o100
    control = tmp_path / 'positive-control'
    program = ('import errno,pathlib,sys; pathlib.Path(sys.argv[1]).write_text("ok"); '
               'p=pathlib.Path(sys.argv[2]);\n'
               'try: p.write_text("forbidden")\n'
               'except OSError as exc: sys.exit(0 if exc.errno==errno.EACCES else 2)\n'
               'sys.exit(3)\n')
    child = subprocess.run([sys.executable, '-c', program, str(control), str(runtime / 'new-file')],
                           capture_output=True, text=True)
    assert child.returncode == 0, child.stderr
    assert control.read_text() == 'ok' and not (runtime / 'new-file').exists()


# Python writes bytecode beside a mutable control module but imports an immutable runtime module without creating a cache.
@pytest.mark.skipif(os.name != 'posix' or os.geteuid() == 0, reason='Unprivileged POSIX read-only runtime')
def test_python_import_skips_runtime_pycache(installed_runtime, tmp_path):
    _, _, runtime, _, _ = installed_runtime
    library = runtime / 'scripts/library.py'
    if not library.exists():
        pytest.skip('Rootless publication contains only command files')
    control = tmp_path / 'mutable.py'
    control.write_text('VALUE = 9\n')
    program = ('import importlib.util,sys; sys.dont_write_bytecode=False; sys.pycache_prefix=None;\n'
               'for index,path in enumerate(sys.argv[1:]):\n'
               ' spec=importlib.util.spec_from_file_location("module"+str(index),path); '
               'module=importlib.util.module_from_spec(spec); spec.loader.exec_module(module); print(module.VALUE)\n')
    child = subprocess.run([sys.executable, '-c', program, str(control), str(library)],
                           capture_output=True, text=True)
    assert child.returncode == 0, child.stderr
    assert child.stdout.splitlines() == ['9', '7']
    assert list((tmp_path / '__pycache__').glob('mutable.*.pyc'))
    assert not (library.parent / '__pycache__').exists()


# A foreign context file is preserved on refusal; removing only it permits the same installation.
def test_foreign_context_is_actionable(installed_runtime, capsys):
    project, home, _, _, _ = installed_runtime
    context = project / '.agents/skills/writer'
    foreign = context / 'foreign-file'
    foreign.write_text('keep me')
    before = tree_state(context)
    assert cli.main(['install', 'app']) == 1
    output = capsys.readouterr()
    assert 'runtime_tree_modified' in output.out + output.err
    assert tree_state(context) == before
    foreign.unlink()
    assert cli.main(['install', 'app']) == 0


# Refresh replaces a protected generation and removal uninstalls it without leaving writable runtime files.
def test_read_only_runtime_upgrade_and_uninstall(installed_runtime, tmp_path, capsys):
    project, home, old_runtime, commit, cfg = installed_runtime
    document = json.loads((project / 'Skillfile.json').read_text())
    if commit is not None:
        authored = cfg.skills_root / 'writer'
        (authored / 'scripts/tool').write_text('#!/bin/sh\nprintf replacement\n')
        new_commit = commit_all(authored, 'replacement')
        run(['git', 'tag', 'v2'], authored)
        document['skills'][0]['tag'] = 'v2'
    else:
        authored = tmp_path / 'source/writer'
        (authored / 'scripts/tool').write_text('#!/bin/sh\nprintf replacement\n')
    write_skillfile(project, document)
    assert cli.main(['upgrade', 'app']) == 0
    if commit is not None:
        new_runtime = home / 'runtime/writer' / new_commit
    else:
        member = source_lock.read_lock((project / 'Skillfile.lock.json').read_bytes()).members[0]
        new_runtime = publish.runtime_entry_path(home, 'writer', package_identity_sha256(member.package))
    assert new_runtime != old_runtime and new_runtime.is_dir()
    assert not old_runtime.exists()
    command = new_runtime / 'scripts/tool' if (new_runtime / 'scripts').exists() else next((new_runtime / 'bin').iterdir())
    assert b'replacement' in command.read_bytes()
    if os.name == 'posix':
        assert all(not path.stat().st_mode & 0o222 for path in (new_runtime, *new_runtime.rglob('*')))
    document['skills'] = []
    write_skillfile(project, document)
    if commit is None:
        assert cli.main(['install', 'app']) == 1
        output = capsys.readouterr()
        assert 'source_lock_stale' in output.out + output.err
        assert new_runtime.is_dir()
    assert cli.main(['install' if commit is not None else 'upgrade', 'app']) == 0
    assert not new_runtime.exists()
    assert not (project / '.agents/bin/write-tool').exists()
    capsys.readouterr()


# A referenced protected generation survives sweep; an unreferenced one is removed through journaled cleanup.
def test_read_only_runtime_gc(installed_runtime):
    _, home, runtime, commit, _ = installed_runtime
    if commit is None:
        pytest.skip('Legacy collector; source-v1 removals are exercised by uninstall')
    with locking.ManagerHomeLock(home) as guard:
        assert gc._collect_runtime_entries(home, {('writer', commit)}, guard=guard) == (0, [])
        assert runtime.exists()
        assert gc._collect_runtime_entries(home, set(), guard=guard) == (1, [])
    assert not runtime.exists()


# Refresh must not silently discard foreign state in the old generation when a different member is selected.
def test_refresh_preserves_foreign_old_generation(installed_runtime, tmp_path, capsys):
    project, home, runtime, commit, cfg = installed_runtime
    writable_directories(runtime)
    foreign = runtime / 'foreign-state'
    foreign.write_text('preserve')
    document = json.loads((project / 'Skillfile.json').read_text())
    if commit is not None:
        authored = cfg.skills_root / 'writer'
        (authored / 'scripts/tool').write_text('#!/bin/sh\nprintf new\n')
        commit_all(authored, 'next generation')
        run(['git', 'tag', 'v2'], authored)
        document['skills'][0]['tag'] = 'v2'
    else:
        (tmp_path / 'source/writer/scripts/tool').write_text('#!/bin/sh\nprintf new\n')
    write_skillfile(project, document)
    roots = (project / '.agents', project / 'Skillfile.lock.json',
             home / 'runtime', home / 'source-v1/runtime')
    before = tree_state(*roots)
    assert cli.main(['upgrade', 'app']) == 1
    output = capsys.readouterr()
    assert 'runtime_tree_modified' in output.out + output.err
    assert tree_state(*roots) == before
    foreign.unlink()
    assert cli.main(['upgrade', 'app']) == 0
    assert not runtime.exists()
    capsys.readouterr()


# Legacy GC preserves foreign bytes and reports their remedy; removing only the foreign file permits collection.
def test_gc_never_deletes_foreign_runtime_entries(installed_runtime):
    _, home, runtime, commit, _ = installed_runtime
    if commit is None:
        pytest.skip('Legacy collector; source-v1 stale generation refusal is covered by refresh')
    writable_directories(runtime)
    foreign = runtime / 'foreign-state'
    foreign.write_text('preserve')
    before = tree_state(runtime)
    with locking.ManagerHomeLock(home) as guard:
        removed, warnings = gc._collect_runtime_entries(home, set(), guard=guard)
        assert removed == 0 and len(warnings) == 1
        assert 'runtime_tree_modified' in warnings[0] and 'foreign-state' in warnings[0]
        assert tree_state(runtime) == before
        foreign.unlink()
        assert gc._collect_runtime_entries(home, set(), guard=guard) == (1, [])
    assert not runtime.exists()


# Missing inventory cannot authorize adopting/deleting a generation; locked reinstall restores its trusted snapshot.
def test_runtime_without_inventory_is_preserved(installed_runtime, capsys):
    _, home, runtime, commit, _ = installed_runtime
    if commit is None:
        pytest.skip('Source-v1 missing-store recovery has separate integration coverage')
    shutil.rmtree(home / 'cache/writer' / commit / 'snapshot')
    before = tree_state(runtime)
    with pytest.raises(published_trees.RuntimeInventoryUnavailable, match='runtime_inventory_unavailable'):
        published_trees.validate_legacy_runtime(runtime, home=home)
    assert tree_state(runtime) == before
    assert cli.main(['install', 'app']) == 0
    published_trees.validate_legacy_runtime(runtime, home=home)
    capsys.readouterr()


# Windows sealing/thawing do not chmod; a foreign file still refuses with a literal-path PowerShell remedy.
def test_windows_refusal_without_permission_changes(tmp_path, monkeypatch):
    runtime = tmp_path / 'runtime'
    runtime.mkdir()
    original = runtime / 'tool'
    original.write_text('published')
    before = tree_state(runtime)
    monkeypatch.setattr(published_trees, 'os', SimpleNamespace(name='nt'))
    with monkeypatch.context() as patch:
        def forbidden_chmod(*args, **kwargs):
            pytest.fail('Windows must not change permissions')
        patch.setattr(Path, 'chmod', forbidden_chmod)
        published_trees.seal_private_runtime(runtime)
        published_trees.make_private_writable(runtime)
        published_trees.validate_tree(runtime, home=tmp_path, skill='writer', member='a' * 40,
                                      inventory={'tool': 'file'})
        foreign = runtime / "foreign'file"
        foreign.write_text('foreign')
        with pytest.raises(published_trees.RuntimeTreeModified) as refusal:
            published_trees.validate_tree(runtime, home=tmp_path, skill='writer', member='a' * 40,
                                          inventory={'tool': 'file'})
        assert f"Remove-Item -LiteralPath '{str(foreign).replace(chr(39), chr(39) * 2)}' -Recurse -Force" in str(refusal.value)
        assert foreign.read_text() == 'foreign'
        foreign.unlink()
    assert tree_state(runtime) == before


class SimulatedCrash(BaseException):
    pass


# Sealing removes owner/group/other write bits, preserving both executable and non-executable file modes and bytes.
@pytest.mark.skipif(os.name != 'posix', reason='POSIX mode bits')
def test_seal_removes_all_write_bits(tmp_path):
    private = tmp_path / 'private'
    private.mkdir(mode=0o777)
    executable = private / 'tool'
    executable.write_text('#!/bin/sh\nexit 0\n')
    data = private / 'data'
    data.write_text('content')
    private.chmod(0o777)
    executable.chmod(0o777)
    data.chmod(0o666)
    published_trees.seal_private_runtime(private)
    assert stat.S_IMODE(private.stat().st_mode) == 0o555
    assert stat.S_IMODE(executable.stat().st_mode) == 0o555
    assert stat.S_IMODE(data.stat().st_mode) == 0o444
    assert executable.read_text() == '#!/bin/sh\nexit 0\n' and data.read_text() == 'content'


# Interrupting each chmod on a private copy cannot change the live tree; rebuilding and sealing a fresh copy succeeds.
@pytest.mark.skipif(os.name != 'posix', reason='POSIX private mode changes')
@pytest.mark.parametrize('operation', ['seal', 'thaw'])
@pytest.mark.parametrize('point', ['before', 'after'])
@pytest.mark.parametrize('ordinal', [1, 2, 3])
def test_private_runtime_permission_interruptions(tmp_path, monkeypatch, operation, point, ordinal):
    live = tmp_path / 'live'
    (live / 'scripts').mkdir(parents=True)
    (live / 'scripts/tool').write_text('original')
    published_trees.seal_private_runtime(live)
    before = tree_state(live)
    private = tmp_path / 'private'
    shutil.copytree(live, private)
    if operation == 'seal':
        published_trees.make_private_writable(private)
    native_chmod = Path.chmod
    calls = 0
    def crash_chmod(path, mode, **kwargs):
        nonlocal calls
        assert path.is_relative_to(private)
        calls += 1
        if calls == ordinal and point == 'before':
            raise SimulatedCrash(point)
        native_chmod(path, mode, **kwargs)
        if calls == ordinal and point == 'after':
            raise SimulatedCrash(point)
    with monkeypatch.context() as patch:
        patch.setattr(Path, 'chmod', crash_chmod)
        with pytest.raises(SimulatedCrash, match=point):
            if operation == 'seal':
                published_trees.seal_private_runtime(private)
            else:
                published_trees.make_private_writable(private)
    assert calls == ordinal and tree_state(live) == before
    fresh = tmp_path / 'retry'
    shutil.copytree(live, fresh)
    published_trees.make_private_writable(fresh)
    (fresh / 'scripts/tool').write_text('replacement')
    published_trees.seal_private_runtime(fresh)
    assert (fresh / 'scripts/tool').read_text() == 'replacement'
    assert all(not path.stat().st_mode & 0o222 for path in (fresh, *fresh.rglob('*')))
    assert tree_state(live) == before


# Crashing before/after every reused mode step preserves the old protected tree or completes the new one; retry succeeds.
@pytest.mark.skipif(os.name != 'posix', reason='POSIX mode transitions')
@pytest.mark.parametrize('event', ['before_staging_mode_finalize', 'staging_mode_finalized',
                                  'staging_mode_finalize_completed', 'before_sidecar_mode_writable',
                                  'sidecar_mode_writable'])
@pytest.mark.parametrize('ordinal', [1, 2, 3])
def test_runtime_permission_crash_recovery(tmp_path, event, ordinal):
    home = tmp_path / 'home'
    live = home / 'runtime/writer' / ('a' * 40)
    desired = tmp_path / 'desired'
    for root, content in ((live, 'old'), (desired, 'new')):
        (root / 'scripts').mkdir(parents=True)
        (root / 'scripts/tool').write_text(content)
        (root / 'scripts/tool').chmod(0o755)
        published_trees.seal_private_runtime(root)
    old_state = tree_state(live)
    target = transactions.MutableTarget('20-runtime', 'writer/' + 'a' * 40, live, desired,
                                         expected_preimage_digest=transactions.digest_path(live), kind='entry')
    plan = transactions.TransactionPlan('runtime-mode-crash', str(tmp_path), (target,))
    calls = 0
    def crash(point, current):
        nonlocal calls
        if point == event:
            calls += 1
            if calls == ordinal:
                raise SimulatedCrash(point)
    engine = transactions.TransactionEngine(home, fault_hook=crash)
    with locking.ManagerHomeLock(home) as guard:
        with pytest.raises(SimulatedCrash, match=event):
            engine.prepare(guard, plan)
            engine.commit(guard, plan.transaction_id)
    assert calls == ordinal
    with locking.ManagerHomeLock(home) as guard:
        transactions.TransactionEngine(home).recover(guard)
    if 'staging' in event:
        assert tree_state(live) == old_state
    else:
        assert (live / 'scripts/tool').read_text() == 'new'
        assert all(not path.stat().st_mode & 0o222 for path in (live, *live.rglob('*')))
    assert not list((home / 'state/transactions/v1').glob('*.json'))
    with locking.ManagerHomeLock(home) as guard:
        fresh_target = replace(target, expected_preimage_digest=transactions.digest_path(live))
        fresh = replace(plan, transaction_id='runtime-mode-retry', targets=(fresh_target,))
        clean = transactions.TransactionEngine(home)
        clean.prepare(guard, fresh)
        clean.commit(guard, fresh.transaction_id)
    assert (live / 'scripts/tool').read_text() == 'new'
    assert all(not path.stat().st_mode & 0o222 for path in (live, *live.rglob('*')))
