"""Durable replay guards fail closed before every forward target."""
from dataclasses import replace

import pytest

from csk import mcp_project, transactions
from csk.locking import ManagerHomeLock
from csk.transactions import TransactionEngine, TransactionIncompleteError
from test_transactions import _write, _target, _plan


# The guard lives on the last target, yet a failed/missing hook must preserve
# both live preimages and the exact prepared journal. A valid guard then
# recovers both targets, including when the initial engine instance is gone.
@pytest.mark.parametrize('missing_hook', [False, True])
def test_commit_guard_precedes_all_forward_writes(tmp_path, missing_hook):
    home = tmp_path / 'home'
    first = _target('first', 'a', _write(tmp_path / 'a', 'old-a'), _write(tmp_path / 'new-a', 'new-a'))
    last = replace(_target('last', 'b', _write(tmp_path / 'b', 'old-b'), _write(tmp_path / 'new-b', 'new-b')),
                   publication_recheck={'kind': 'test', 'commit_guard': {'kind': 'test'}})
    engine = TransactionEngine(home, pre_write_hook=lambda target: None)
    with ManagerHomeLock(home) as lock:
        engine.prepare(lock, _plan('guard-control', tmp_path / 'project', first, last))
        journal_path = engine.journal_root / 'guard-control.json'
        before = journal_path.read_bytes()
        observations = []

        def reject(journal):
            observations.append(journal.targets[-1].publication_recheck['commit_guard'])
            raise ValueError('guard rejected')

        failing = TransactionEngine(home, pre_write_hook=lambda target: None,
                                    pre_commit_hook=None if missing_hook else reject)
        with pytest.raises(TransactionIncompleteError) as failure:
            failing.recover(lock)
        assert failure.value.code == 'transaction_recovery_required'
        assert 'rerun csk install' in str(failure.value)
        assert (tmp_path / 'a').read_text() == 'old-a'
        assert (tmp_path / 'b').read_text() == 'old-b'
        assert journal_path.read_bytes() == before
        assert observations == ([] if missing_hook else [{'kind': 'test'}])
        allowed = []
        trusted = TransactionEngine(home, pre_write_hook=lambda target: None,
                                    pre_commit_hook=lambda journal: allowed.append(journal.transaction_id))
        trusted.recover(lock)
        assert allowed == ['guard-control']
        assert (tmp_path / 'a').read_text() == 'new-a'
        assert (tmp_path / 'b').read_text() == 'new-b'
        assert not journal_path.exists()


# Corrupt recovery metadata cannot become a fresh approval. These shapes must
# reject before the first live replacement; a valid empty observation guard
# is the nearby non-launcher positive control.
@pytest.mark.parametrize('guard', [
    None,
    {'kind': 'unknown', 'observations': []},
    {'kind': 'mcp-launchers-v1', 'observations': [], 'extra': True},
    {'kind': 'mcp-launchers-v1', 'observations': {}},
    {'kind': 'mcp-launchers-v1', 'observations': [None]},
    {'kind': 'mcp-launchers-v1', 'observations': [{'name': 'app', 'launcher': {}, 'observed': {}}]},
    {'kind': 'mcp-launchers-v1', 'observations': [{
        'name': 'app', 'launcher': {'macos': {'bundle_id': 'bad"', 'team_id': 'ABCDE12345',
                                           'executable': 'Contents/MacOS/helper'}},
        'observed': {'path': '/app/helper', 'trust': 'verified', 'publisher': 'example', 'content_sha256': '0' * 64},
    }]},
])
def test_malformed_launcher_guard_refuses_before_publication(tmp_path, guard):
    home = tmp_path / 'home'
    live = _write(tmp_path / 'live', 'old')
    desired = _write(tmp_path / 'desired', 'new')
    target = replace(_target('mcp-native', 'native', live, desired),
                     publication_recheck={'kind': 'test', 'commit_guard': guard})
    engine = TransactionEngine(home, pre_write_hook=lambda target: None,
                               pre_commit_hook=mcp_project.recheck_commit)
    with ManagerHomeLock(home) as lock:
        journal = engine.prepare(lock, _plan('bad-guard', tmp_path / 'project', target))
        before = (engine.journal_root / 'bad-guard.json').read_bytes()
        with pytest.raises(TransactionIncompleteError) as failure:
            engine.commit(lock, 'bad-guard')
        assert failure.value.code == 'transaction_recovery_required'
        assert 'invalid MCP' in str(failure.value) or 'invalid journaled MCP' in str(failure.value)
        assert live.read_text() == 'old'
        assert (engine.journal_root / 'bad-guard.json').read_bytes() == before
        # Only test-owned metadata is repaired here, before a real replay.
        journal.targets[0].publication_recheck['commit_guard'] = {'kind': 'mcp-launchers-v1', 'observations': []}
        (engine.journal_root / 'bad-guard.json').write_bytes(transactions._journal_bytes(journal))
        engine.recover(lock)
        assert live.read_text() == 'new'
        assert not (engine.journal_root / 'bad-guard.json').exists()


# Explicit rollback restores an interrupted target even when its forward
# guard rejects. It must refuse a committed/cleanup journal without changing
# either the desired live bytes or the cleanup journal.
@pytest.mark.parametrize('committed', [False, True])
def test_explicit_rollback_does_not_undo_committed_install(tmp_path, committed):
    home = tmp_path / 'home'
    live = _write(tmp_path / 'live', 'old')
    desired = _write(tmp_path / 'desired', 'new')
    target = _target('mcp-native', 'native', live, desired)
    def stop(event, target):
        if event == ('before_cleanup' if committed else 'after_install'):
            raise KeyboardInterrupt('simulated process interruption')
    engine = TransactionEngine(home, fault_hook=stop)
    with ManagerHomeLock(home) as lock:
        engine.prepare(lock, _plan('explicit-rollback', tmp_path / 'project', target))
        with pytest.raises(KeyboardInterrupt):
            engine.commit(lock, 'explicit-rollback')
        journal_path = engine.journal_root / 'explicit-rollback.json'
        before = journal_path.read_bytes()
        rejecting = TransactionEngine(home, pre_commit_hook=lambda _: pytest.fail('forward guard ran during rollback'))
        if committed:
            with pytest.raises(transactions.TransactionError, match='already committed'):
                rejecting.rollback(lock, 'explicit-rollback')
            assert live.read_text() == 'new' and journal_path.read_bytes() == before
            rejecting.recover(lock)
            assert live.read_text() == 'new' and not journal_path.exists()
        else:
            rejecting.rollback(lock, 'explicit-rollback')
            assert live.read_text() == 'old' and not journal_path.exists()


# Interrupted rollback can already have restored all preimages and tombed its
# journal. Retrying the explicit rollback command must finish that removal,
# rather than report that this rolled-back installation committed.
def test_explicit_rollback_finishes_rollback_tomb(tmp_path):
    home = tmp_path / 'home'
    live = _write(tmp_path / 'live', 'old')
    desired = _write(tmp_path / 'desired', 'new')
    target = _target('mcp-native', 'native', live, desired)
    def interrupt_commit(event, target):
        if event == 'after_install':
            raise KeyboardInterrupt('simulated interruption')
    def interrupt_removal(event, target):
        if event == 'before_journal_unlink':
            raise OSError('injected rollback journal removal failure')
    engine = TransactionEngine(home, fault_hook=interrupt_commit)
    with ManagerHomeLock(home) as lock:
        engine.prepare(lock, _plan('rollback-tomb', tmp_path / 'project', target))
        with pytest.raises(KeyboardInterrupt):
            engine.commit(lock, 'rollback-tomb')
        with pytest.raises(TransactionIncompleteError) as failure:
            TransactionEngine(home, fault_hook=interrupt_removal).rollback(lock, 'rollback-tomb')
        assert failure.value.code == 'transaction_rollback_deferred'
        assert live.read_text() == 'old'
        tomb = engine.journal_root / 'rollback-tomb.json.delete'
        assert tomb.exists()
        TransactionEngine(home).rollback(lock, 'rollback-tomb')
        assert live.read_text() == 'old' and not tomb.exists()
