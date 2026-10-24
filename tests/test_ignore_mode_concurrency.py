"""Cross-process publication and crash witnesses for shared Git excludes."""
from __future__ import annotations

import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

from conftest import make_config, make_project, run, write_skillfile
from csk import config, gitignore_gate, locking


@pytest.fixture
def consumers(tmp_path, skills_root):
    def create(schema, linked=False):
        repository = make_project(tmp_path, gitignore=False)
        run(['git', 'commit', '--allow-empty', '-m', 'fixture'], repository)
        if linked:
            second = tmp_path / 'linked'
            run(['git', 'worktree', 'add', '-b', 'linked', str(second)], repository)
            projects = [repository, second]
        else:
            projects = [repository / 'one', repository / 'two']
            for project in projects:
                project.mkdir()
        configs = []
        for index, project in enumerate(projects):
            write_skillfile(project, {'schema_version': schema, 'agents': ['codex_cli'], 'skills': []})
            home = tmp_path / f'manager-{index}'
            locking.provision_new_manager_home(home)
            cfg = make_config(home, skills_root, project, agents=['codex_cli'])
            config.save_config(cfg)
            configs.append(cfg.path)
        return repository, projects, configs
    return create


def _env(config_path):
    # Use this checkout in the child even when the parent runs via editable uv.
    return {**{k: v for k, v in os.environ.items() if k not in {'GIT_DIR', 'GIT_WORK_TREE'}}, 'CSK_CONFIG': str(config_path),
            'PYTHONPATH': str(Path(__file__).resolve().parents[1] / 'src')}


def _ignored(repository, project, path):
    # The fixture runner deliberately stops parent discovery at its attested
    # root; nested-project probes therefore run through the repository root.
    cwd = repository if project.is_relative_to(repository) else project
    return run(['git', 'check-ignore', '-q', '--', str(project / path)], cwd, check=False).returncode == 0


def _wait(path, child):
    deadline = time.monotonic() + 30
    while not path.exists():
        assert child.poll() is None, child.communicate()
        assert time.monotonic() < deadline, f'missing synchronization point {path.name}'
        time.sleep(0.01)


_BOOTSTRAP = '''
import sys, time
from contextlib import contextmanager, nullcontext
from pathlib import Path
from csk import cli, gitignore_gate as gate
barrier, label, disabled, operation = Path(sys.argv[1]), sys.argv[2], sys.argv[3] == 'True', sys.argv[4]
original_lock, original_write = gate.exclude_file_lock, gate.write_exclude
@contextmanager
def observed_lock(path):
    (barrier / ('attempt-' + label)).touch()
    with (nullcontext() if disabled else original_lock(path)):
        yield
gate.exclude_file_lock = observed_lock
def paused_write(path, data):
    (barrier / ('write-' + label)).touch()
    if label == '0':
        deadline = time.monotonic() + 30
        while not (barrier / 'release').exists():
            if time.monotonic() > deadline: raise RuntimeError('publication barrier timed out')
            time.sleep(0.01)
    return original_write(path, data)
gate.write_exclude = paused_write
args = ['init', '.', '--no-interactive'] if operation == 'init' else ['install', 'app']
raise SystemExit(cli.main(args))
'''


def _concurrent_witness(consumers, tmp_path, schema, operation, linked, disabled=False):
    repository, projects, configs = consumers(schema, linked)
    barrier = tmp_path / 'barrier'
    barrier.mkdir()
    children = []
    outputs = []
    try:
        for index in range(2):
            children.append(subprocess.Popen(
                [sys.executable, '-c', _BOOTSTRAP, str(barrier), str(index), str(disabled), operation],
                cwd=projects[index], env=_env(configs[index]), text=True,
                stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            ))
            _wait(barrier / ('write-0' if index == 0 else 'attempt-1'), children[-1])
        if disabled:
            # Widen exactly the reviewer's lost-update interleaving. With the
            # real OS lock the second process cannot finish before release.
            outputs.append(children[1].communicate(timeout=40))
        (barrier / 'release').touch()
        for child in children:
            outputs.append(child.communicate(timeout=60))
            assert child.returncode == 0, (child.returncode, outputs)
    finally:
        (barrier / 'release').touch()
        for child in children:
            if child.poll() is None:
                child.kill()
                child.wait()
    data = (repository / '.git/info/exclude').read_bytes()
    # Same oracle for the positive control and the disabled-lock plant: both
    # successful consumers retain their own block and real Git ignores paths.
    for project in projects:
        key = gitignore_gate.project_key(project)
        assert gitignore_gate.project_block(data, key) is not None, 'lost a successful project block'
        assert _ignored(repository, project, '.agents/probe')
    assert data.count(b'# CocoaSkill project ') == 2
    if schema in {2, 3}:
        for project, cfg in zip(projects, configs):
            retry = subprocess.run([sys.executable, '-m', 'csk', 'install', 'app'], cwd=project,
                                   env=_env(cfg), text=True, capture_output=True, timeout=90)
            assert retry.returncode == 0, (retry.stdout, retry.stderr)
        assert (repository / '.git/info/exclude').read_bytes() == data


# Real CLI processes from independent homes retain policies in nested projects
# and linked worktrees through init, schema 1, and transactional schemas 2/3.
@pytest.mark.parametrize('linked', [False, True])
@pytest.mark.parametrize('schema,operation', [(1, 'init'), (1, 'install'), (2, 'install'), (3, 'install')])
def test_concurrent_writers(consumers, tmp_path, schema, operation, linked):
    _concurrent_witness(consumers, tmp_path, schema, operation, linked)


# Disabling only the common OS lock makes the unchanged retention oracle fail,
# establishing that the synchronization witness detects the original defect.
@pytest.mark.parametrize('schema,operation', [(1, 'init'), (3, 'install')])
def test_lock_control_plant(consumers, tmp_path, schema, operation):
    with pytest.raises(AssertionError, match='lost a successful project block'):
        _concurrent_witness(consumers, tmp_path, schema, operation, False, disabled=True)


_CRASH = '''
import os, sys
from csk import cli, transactions
from csk.sources import publish
original = publish.TransactionEngine
def factory(home, **kwargs):
    def fault(point, item):
        if point == sys.argv[1] and item is not None and item.target_class == 'mcp-gitignore':
            os._exit(86)
    return original(home, fault_hook=fault, **kwargs)
publish.TransactionEngine = factory
transactions.TransactionEngine = factory
raise SystemExit(cli.main(['install', 'app']))
'''


# Process death releases the OS lock. A different home can publish before the
# crashed home replays; replay preserves that project and user bytes. The
# no-crash lane is the corresponding successful-publication positive control.
@pytest.mark.parametrize('schema', [2, 3])
@pytest.mark.parametrize('point', [None, 'after_backup', 'after_install', 'target_committed'])
def test_crashed_writer_retry(consumers, schema, point):
    repository, projects, configs = consumers(schema)
    target = repository / '.git/info/exclude'
    target.parent.mkdir(exist_ok=True)
    user = b'# user \xff\r\n/private\r\n'
    target.write_bytes(user)
    args = [sys.executable, '-m', 'csk', 'install', 'app'] if point is None else [sys.executable, '-c', _CRASH, point]
    first = subprocess.run(args, cwd=projects[0], env=_env(configs[0]), text=True, capture_output=True, timeout=90)
    assert first.returncode == (0 if point is None else 86), (first.stdout, first.stderr)
    for index in [1, 0, 1, 0]:
        result = subprocess.run([sys.executable, '-m', 'csk', 'install', 'app'], cwd=projects[index],
                                env=_env(configs[index]), text=True, capture_output=True, timeout=90)
        assert result.returncode == 0, (result.stdout, result.stderr)
    data = target.read_bytes()
    assert data.startswith(user)
    assert data.count(b'# CocoaSkill project ') == 2
    for project in projects:
        assert gitignore_gate.project_block(data, gitignore_gate.project_key(project)) is not None
        assert _ignored(repository, project, '.agents/probe')
        assert not (project / '.gitignore').exists()


# Replacing/removing this project's block leaves the other project's bytes
# untouched, removes stale owned rules, and is idempotent.
def test_project_block_removal(consumers):
    repository, projects, _ = consumers(3)
    target = repository / '.git/info/exclude'
    target.parent.mkdir(exist_ok=True)
    prefix = b'# user \xff\r\n/private\r\n'
    target.write_bytes(prefix)
    for project in projects:
        gitignore_gate.ensure_ignored(project, ['.agents/', 'old-file'], mode='exclude')
    other = gitignore_gate.project_block(target.read_bytes(), gitignore_gate.project_key(projects[1]))
    gitignore_gate.ensure_ignored(projects[0], ['.agents/'], mode='exclude')
    assert b'/one/old-file' not in target.read_bytes()
    assert gitignore_gate.project_block(target.read_bytes(), gitignore_gate.project_key(projects[1])) == other
    gitignore_gate.ensure_ignored(projects[0], [], mode='exclude')
    expected = prefix + other
    assert target.read_bytes() == expected
    gitignore_gate.ensure_ignored(projects[0], [], mode='exclude')
    assert target.read_bytes() == expected
    assert _ignored(repository, projects[1], 'old-file')
    assert not _ignored(repository, projects[0], 'old-file')


# Recovery rollback restores only its project's preimage after another manager
# has committed; the next ordinary retry succeeds with both policies retained.
@pytest.mark.parametrize('schema', [2, 3])
def test_recovery_rollback_preserves_neighbor(consumers, schema):
    repository, projects, configs = consumers(schema)
    target = repository / '.git/info/exclude'
    target.parent.mkdir(exist_ok=True)
    user = b'# preserved user bytes\r\n'
    target.write_bytes(user)
    first = subprocess.run([sys.executable, '-c', _CRASH, 'after_install'], cwd=projects[0],
                           env=_env(configs[0]), text=True, capture_output=True, timeout=90)
    assert first.returncode == 86, (first.stdout, first.stderr)
    second = subprocess.run([sys.executable, '-m', 'csk', 'install', 'app'], cwd=projects[1],
                            env=_env(configs[1]), text=True, capture_output=True, timeout=90)
    assert second.returncode == 0, (second.stdout, second.stderr)
    neighbor = gitignore_gate.project_block(target.read_bytes(), gitignore_gate.project_key(projects[1]))
    bootstrap = _CRASH.replace('os._exit(86)', "raise OSError('injected replay failure')")
    failure = subprocess.run([sys.executable, '-c', bootstrap, 'target_committed'], cwd=projects[0],
                             env=_env(configs[0]), text=True, capture_output=True, timeout=90)
    assert failure.returncode == 1 and 'injected replay failure' in failure.stderr
    assert target.read_bytes() == user + neighbor
    retry = subprocess.run([sys.executable, '-m', 'csk', 'install', 'app'], cwd=projects[0],
                           env=_env(configs[0]), text=True, capture_output=True, timeout=90)
    assert retry.returncode == 0, (retry.stdout, retry.stderr)
    assert gitignore_gate.project_block(target.read_bytes(), gitignore_gate.project_key(projects[1])) == neighbor
    assert _ignored(repository, projects[0], '.agents/probe')
    assert _ignored(repository, projects[1], '.agents/probe')


# A project-block edit is still a real conflict, unlike a neighbor's publication.
# Recovery refuses without erasing the modified block or the neighbor's rules.
@pytest.mark.parametrize('schema', [2, 3])
def test_recovery_refuses_changed_own_block(consumers, schema):
    repository, projects, configs = consumers(schema)
    crash = subprocess.run([sys.executable, '-c', _CRASH, 'after_install'], cwd=projects[0],
                           env=_env(configs[0]), text=True, capture_output=True, timeout=90)
    assert crash.returncode == 86, (crash.stdout, crash.stderr)
    gitignore_gate.ensure_ignored(projects[1], ['.agents/'], mode='exclude')
    target = repository / '.git/info/exclude'
    key = gitignore_gate.project_key(projects[0])
    block = gitignore_gate.project_block(target.read_bytes(), key)
    assert block is not None
    changed = gitignore_gate.replace_project_block(target.read_bytes(), key, block.replace(b'/.agents/', b'/altered/'))
    target.write_bytes(changed)
    retry = subprocess.run([sys.executable, '-m', 'csk', 'install', 'app'], cwd=projects[0],
                           env=_env(configs[0]), text=True, capture_output=True, timeout=90)
    assert retry.returncode == 1 and 'recover transaction' in retry.stderr
    # The first attempt records rollback after both forward and rollback refuse
    # the unknown slice. Replay of that rollback exposes the precise refusal.
    retry = subprocess.run([sys.executable, '-m', 'csk', 'install', 'app'], cwd=projects[0],
                           env=_env(configs[0]), text=True, capture_output=True, timeout=90)
    assert retry.returncode == 1 and 'source_output_overlap' in retry.stderr
    assert target.read_bytes() == changed
    assert _ignored(repository, projects[1], '.agents/probe')


# Refuse a linked lock inode rather than locking a different file and writing
# the exclude without coordination; the regular lock is the positive control.
@pytest.mark.parametrize('linked_lock', [False, True])
def test_common_lock_inode_safety(consumers, tmp_path, linked_lock):
    repository, projects, _ = consumers(1)
    sentinel = tmp_path / 'sentinel'
    sentinel.write_bytes(b'KEEP')
    path = repository / '.git/csk-exclude.lock'
    if linked_lock:
        path.symlink_to(sentinel)
        with pytest.raises(gitignore_gate.ExcludeError):
            gitignore_gate.ensure_ignored(projects[0], ['.agents/'], mode='exclude')
        assert not (repository / '.git/info/exclude').exists()
        assert sentinel.read_bytes() == b'KEEP'
    else:
        gitignore_gate.ensure_ignored(projects[0], ['.agents/'], mode='exclude')
        assert path.is_file() and not path.is_symlink()
        assert _ignored(repository, projects[0], '.agents/probe')


# A crash after restoring this slice but before recording rolled_back is
# replayable even after a different manager has published its own project.
@pytest.mark.parametrize('schema', [2, 3])
def test_crash_during_rollback_retry(consumers, schema):
    repository, projects, configs = consumers(schema)
    bootstrap = _CRASH.replace(
        "if point == sys.argv[1] and item is not None and item.target_class == 'mcp-gitignore':\n            os._exit(86)",
        "if item is not None and item.target_class == 'mcp-gitignore':\n"
        "            if point == 'after_install': raise OSError('start rollback')\n"
        "            if point == 'after_restore': os._exit(86)",
    )
    crash = subprocess.run([sys.executable, '-c', bootstrap, 'unused'], cwd=projects[0],
                           env=_env(configs[0]), text=True, capture_output=True, timeout=90)
    assert crash.returncode == 86, (crash.stdout, crash.stderr)
    target = repository / '.git/info/exclude'
    assert not target.exists()
    for index in [1, 0]:
        result = subprocess.run([sys.executable, '-m', 'csk', 'install', 'app'], cwd=projects[index],
                                env=_env(configs[index]), text=True, capture_output=True, timeout=90)
        assert result.returncode == 0, (result.stdout, result.stderr)
    assert target.read_bytes().count(b'# CocoaSkill project ') == 2
    assert all(_ignored(repository, project, '.agents/probe') for project in projects)
