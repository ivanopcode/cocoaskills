from __future__ import annotations

import json
import os
import shutil
import stat
import sys
import threading
from pathlib import Path

import pytest

from csk import locking, transactions
from csk.locking import BuildLock, LockError, ManagerHomeLock, ProjectLock
from csk.transactions import (
    ABSENT_DIGEST,
    JournalTarget,
    MutableTarget,
    TransactionCorruptionError,
    TransactionEngine,
    TransactionError,
    TransactionPlan,
    digest_path,
)


class SimulatedCrash(BaseException):
    pass


def _write(path: Path, value: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(value, encoding="utf-8")
    return path


def _held_lock_bytes(
    lock: ManagerHomeLock | ProjectLock | BuildLock,
) -> bytes:
    assert lock._fd is not None
    payload = locking._read_lock_fd_bytes(lock._fd)
    assert payload is not None
    return payload


def _target(
    target_class: str, identifier: str, live: Path, desired: Path | None
) -> MutableTarget:
    return MutableTarget(
        target_class=target_class,
        identifier=identifier,
        live_path=live,
        desired_path=desired,
        expected_preimage_digest=digest_path(live),
    )


def _namespace_target(
    target_class: str, identifier: str, live: Path, desired: Path | None
) -> MutableTarget:
    """Build a target that must be rejected without reopening its live path."""
    return MutableTarget(
        target_class=target_class,
        identifier=identifier,
        live_path=live,
        desired_path=desired,
        expected_preimage_digest=ABSENT_DIGEST,
    )


def _assert_mode_identity(path: Path, expected: int) -> None:
    actual = stat.S_IMODE(path.stat().st_mode)
    if os.name == "nt":
        assert bool(actual & stat.S_IWRITE) is bool(expected & stat.S_IWRITE)
    else:
        assert actual == expected


def _plan(
    transaction_id: str, project: Path, *targets: MutableTarget
) -> TransactionPlan:
    return TransactionPlan(
        transaction_id=transaction_id,
        project_identity=str(project.resolve()),
        targets=tuple(targets),
        generation_digests={"runtime/default": "sha256:" + "a" * 64},
    )


def _symlink(path: Path, destination: str, *, directory: bool = False) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        path.symlink_to(destination, target_is_directory=directory)
    except OSError as exc:
        pytest.skip(f"symlinks unavailable: {exc}")
    return path


def _entry_target(
    target_class: str,
    identifier: str,
    live: Path,
    desired: Path | None,
) -> MutableTarget:
    return MutableTarget(
        target_class=target_class,
        identifier=identifier,
        live_path=live,
        desired_path=desired,
        expected_preimage_digest=transactions.digest_target(live, kind="entry"),
        kind="entry",
    )


@pytest.mark.parametrize("kind", ["file", "directory"])
def test_native_no_replace_preserves_destination_created_at_mutation_boundary(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, kind: str
):
    source = tmp_path / "source"
    destination = tmp_path / "destination"
    if kind == "file":
        _write(source, "transaction")
    else:
        _write(source / "payload", "transaction")
    native_rename = transactions._native_rename_no_replace

    def create_competitor_then_rename(
        current_source: Path, current_destination: Path
    ) -> None:
        assert current_source == source
        assert current_destination == destination
        if kind == "file":
            _write(destination, "competing-success")
        else:
            _write(destination / "payload", "competing-success")
        native_rename(current_source, current_destination)

    monkeypatch.setattr(
        transactions, "_native_rename_no_replace", create_competitor_then_rename
    )

    with pytest.raises(
        TransactionCorruptionError, match="transaction destination exists"
    ):
        transactions._rename_no_replace(source, destination)

    assert source.exists()
    if kind == "file":
        assert source.read_text(encoding="utf-8") == "transaction"
        assert destination.read_text(encoding="utf-8") == "competing-success"
    else:
        assert (source / "payload").read_text(encoding="utf-8") == "transaction"
        assert (destination / "payload").read_text(
            encoding="utf-8"
        ) == "competing-success"


def test_windows_journal_state_transitions_use_write_through_moves(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    home = tmp_path / "home"
    live = _write(tmp_path / "live", "old")
    desired = _write(tmp_path / "desired", "new")
    engine = TransactionEngine(home)
    journal, _ = engine._build_journal(
        _plan(
            "txn-windows-routing",
            tmp_path / "project",
            _target("10-context", "skill", live, desired),
        )
    )
    moves: list[tuple[Path, Path, int]] = []
    synced_directories: list[Path] = []

    def move_file(source: Path, destination: Path, flags: int) -> None:
        moves.append((source, destination, flags))
        if flags & transactions._MOVEFILE_REPLACE_EXISTING:
            source.replace(destination)
        else:
            if destination.exists():
                raise FileExistsError(destination)
            source.rename(destination)

    monkeypatch.setattr(transactions, "_is_windows", lambda: True)
    monkeypatch.setattr(transactions, "_windows_move_file", move_file)
    monkeypatch.setattr(
        transactions,
        "_windows_sync_directory",
        lambda path: synced_directories.append(path),
    )

    engine._save_journal(journal, create=True)
    for target in journal.targets:
        target.staged_source = None
        target.staging_entries = []
        target.staging_index = 0
    journal.phase = "prepared"
    engine._save_journal(journal)
    engine._remove_journal(journal)

    assert [flags for _, _, flags in moves] == [
        transactions._MOVEFILE_WRITE_THROUGH,
        (
            transactions._MOVEFILE_REPLACE_EXISTING
            | transactions._MOVEFILE_WRITE_THROUGH
        ),
        (
            transactions._MOVEFILE_REPLACE_EXISTING
            | transactions._MOVEFILE_WRITE_THROUGH
        ),
        transactions._MOVEFILE_WRITE_THROUGH,
    ]
    assert all(source.parent == engine.journal_root for source, _, _ in moves)
    assert all(destination.parent == engine.journal_root for _, destination, _ in moves)
    assert synced_directories
    assert engine.journal_root in synced_directories
    assert not (engine.journal_root / "txn-windows-routing.json").exists()
    assert not (engine.journal_root / "txn-windows-routing.json.delete").exists()


def test_windows_sidecar_cleanup_uses_write_through_owned_tombs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    home = tmp_path / "home"
    live = _write(tmp_path / "live", "old")
    desired = _write(tmp_path / "desired", "new")
    engine = TransactionEngine(home)
    moves: list[tuple[Path, Path, int]] = []

    def move_file(source: Path, destination: Path, flags: int) -> None:
        moves.append((source, destination, flags))
        if flags & transactions._MOVEFILE_REPLACE_EXISTING:
            source.replace(destination)
        else:
            if destination.exists():
                raise FileExistsError(destination)
            source.rename(destination)

    monkeypatch.setattr(transactions, "_is_windows", lambda: True)
    monkeypatch.setattr(transactions, "_windows_move_file", move_file)
    monkeypatch.setattr(transactions, "_windows_sync_regular", lambda path: None)
    monkeypatch.setattr(transactions, "_windows_sync_directory", lambda path: None)

    with ManagerHomeLock(home) as lock:
        engine.prepare(
            lock,
            _plan(
                "txn-windows-sidecar-cleanup",
                tmp_path / "project",
                _target("10-context", "skill", live, desired),
            ),
        )
        engine.commit(lock, "txn-windows-sidecar-cleanup")

    cleanup_moves = [
        (source, destination, flags)
        for source, destination, flags in moves
        if source.name.endswith((".desired", ".backup", ".rollback"))
        and destination.name == f"{source.name}.delete"
    ]
    assert cleanup_moves
    assert all(
        flags == transactions._MOVEFILE_WRITE_THROUGH for _, _, flags in cleanup_moves
    )
    assert not any(tmp_path.glob(".csk-txn-*"))


def test_windows_sync_helpers_request_flushable_file_and_directory_handles(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    requests: list[tuple[Path, int, int, frozenset[int]]] = []

    def record_flush(
        path: Path,
        *,
        desired_access: int,
        flags_and_attributes: int,
        ignored_flush_errors: frozenset[int],
    ) -> None:
        requests.append(
            (
                path,
                desired_access,
                flags_and_attributes,
                ignored_flush_errors,
            )
        )

    monkeypatch.setattr(transactions, "_windows_flush_path", record_flush)
    regular = tmp_path / "regular"
    directory = tmp_path / "directory"

    transactions._windows_sync_regular(regular)
    transactions._windows_sync_directory(directory)

    assert requests == [
        (regular, 0x40000000, 0x00200000, frozenset()),
        (directory, 0x80000000, 0x02000000, frozenset({5, 6})),
    ]


def test_windows_read_only_file_sync_falls_back_to_read_handle(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    requests: list[tuple[int, frozenset[int]]] = []

    def flush(
        path: Path,
        *,
        desired_access: int,
        flags_and_attributes: int,
        ignored_flush_errors: frozenset[int],
    ) -> None:
        del path, flags_and_attributes
        requests.append((desired_access, ignored_flush_errors))
        if desired_access == transactions._GENERIC_WRITE:
            raise OSError(transactions._ERROR_ACCESS_DENIED, "read-only")

    monkeypatch.setattr(transactions, "_windows_flush_path", flush)

    transactions._windows_sync_regular(tmp_path / "read-only")

    assert requests == [
        (transactions._GENERIC_WRITE, frozenset()),
        (
            transactions._GENERIC_READ,
            frozenset(
                {
                    transactions._ERROR_ACCESS_DENIED,
                    transactions._ERROR_INVALID_HANDLE,
                }
            ),
        ),
    ]


@pytest.mark.skipif(not transactions._is_windows(), reason="requires Windows")
def test_windows_sync_helpers_flush_real_file_and_tree(tmp_path: Path):
    regular = _write(tmp_path / "regular", "durable")
    tree = tmp_path / "tree"
    _write(tree / "nested" / "payload", "durable tree")

    transactions._sync_regular(regular)
    transactions._sync_tree(tree)


@pytest.mark.parametrize("rollback", [False, True])
def test_recovery_finishes_interrupted_journal_removal_tomb(
    tmp_path: Path, rollback: bool
):
    home = tmp_path / "home"
    live = _write(tmp_path / "live", "old")
    desired = _write(tmp_path / "desired", "new")

    def crash_after_tomb(point: str, target: JournalTarget | None) -> None:
        if rollback and point == "target_committed":
            raise RuntimeError("force rollback")
        if point == "journal_tombed":
            raise SimulatedCrash(point)

    engine = TransactionEngine(home, fault_hook=crash_after_tomb)
    with ManagerHomeLock(home) as lock:
        engine.prepare(
            lock,
            _plan(
                "txn-journal-tomb",
                tmp_path / "project",
                _target("10-context", "skill", live, desired),
            ),
        )
        with pytest.raises(SimulatedCrash, match="journal_tombed"):
            engine.commit(lock, "txn-journal-tomb")

    assert live.read_text(encoding="utf-8") == ("old" if rollback else "new")
    assert not (engine.journal_root / "txn-journal-tomb.json").exists()
    assert (engine.journal_root / "txn-journal-tomb.json.delete").exists()

    with ManagerHomeLock(home) as lock:
        TransactionEngine(home).recover(lock)

    assert live.read_text(encoding="utf-8") == ("old" if rollback else "new")
    assert not (engine.journal_root / "txn-journal-tomb.json").exists()
    assert not (engine.journal_root / "txn-journal-tomb.json.delete").exists()
    assert not any(live.parent.glob(".csk-txn-*"))


@pytest.mark.parametrize(
    ("phase", "record_suffix"),
    [
        ("prepared", ".json"),
        ("committing", ".json"),
        ("rolling_back", ".json"),
        ("cleanup", ".json"),
        ("removal_tomb", ".json.delete"),
    ],
)
def test_recovery_rejects_journal_filename_id_mismatch_before_mutation(
    tmp_path: Path,
    phase: str,
    record_suffix: str,
):
    home = tmp_path / "home"
    live = _write(tmp_path / "live", "old")
    desired = _write(tmp_path / "desired", "new")
    transaction_id = f"txn-id-binding-{phase}"
    alias_id = f"alias-id-binding-{phase}"
    engine = TransactionEngine(home)

    with ManagerHomeLock(home) as lock:
        journal = engine.prepare(
            lock,
            _plan(
                transaction_id,
                tmp_path / "project",
                _target("10-context", "skill", live, desired),
            ),
        )
        target = journal.targets[0]
        if phase == "committing":
            journal.phase = "committing"
            transactions._rename_no_replace(
                Path(target.live_path),
                Path(target.backup_path),
            )
            target.backup_digest = digest_path(Path(target.backup_path))
            target.state = "backed_up"
            engine._save_journal(journal)
        elif phase == "rolling_back":
            journal.phase = "rolling_back"
            engine._save_journal(journal)
        elif phase == "cleanup":
            transactions._rename_no_replace(
                Path(target.live_path),
                Path(target.backup_path),
            )
            target.backup_digest = digest_path(Path(target.backup_path))
            assert target.staged_path is not None
            transactions._rename_no_replace(
                Path(target.staged_path),
                Path(target.live_path),
            )
            target.state = "committed"
            journal.phase = "cleanup"
            engine._save_journal(journal)
        elif phase == "removal_tomb":

            def crash_after_tomb(point: str, current: JournalTarget | None) -> None:
                del current
                if point == "journal_tombed":
                    raise SimulatedCrash(point)

            engine._fault_hook = crash_after_tomb
            with pytest.raises(SimulatedCrash, match="journal_tombed"):
                engine._remove_journal(journal)

        canonical_record = engine.journal_root / f"{transaction_id}{record_suffix}"
        alias_record = engine.journal_root / f"{alias_id}{record_suffix}"
        canonical_record.rename(alias_record)

        transaction_paths = [
            Path(target.live_path),
            Path(target.backup_path),
            Path(target.rollback_path),
        ]
        if target.staged_path is not None:
            transaction_paths.append(Path(target.staged_path))
        before_digests = {path: digest_path(path) for path in transaction_paths}
        record_bytes = alias_record.read_bytes()
        lock_witness = _held_lock_bytes(lock)

        with pytest.raises(
            TransactionCorruptionError,
            match="filename|transaction id",
        ):
            TransactionEngine(home).recover(lock)

        lock.assert_held()
        assert _held_lock_bytes(lock) == lock_witness
        assert alias_record.read_bytes() == record_bytes
        assert {path: digest_path(path) for path in transaction_paths} == before_digests
        assert not canonical_record.exists()


@pytest.mark.parametrize("kind", ["file", "directory"])
def test_recovery_discards_interrupted_partial_staging(
    tmp_path: Path,
    kind: str,
):
    home = tmp_path / "home"
    live = tmp_path / "live"
    desired = tmp_path / "desired"
    if kind == "file":
        desired.write_bytes(b"complete desired bytes\n" * 4096)
        crash_point = "after_staging_chunk_sync"
    else:
        _write(desired / "nested" / "payload", "complete desired bytes")
        crash_point = "during_staging_copy"

    def crash_during_copy(point: str, target: JournalTarget | None) -> None:
        if point == crash_point:
            raise SimulatedCrash(point)

    engine = TransactionEngine(home, fault_hook=crash_during_copy)
    with (
        ManagerHomeLock(home) as lock,
        pytest.raises(SimulatedCrash, match=crash_point),
    ):
        engine.prepare(
            lock,
            _plan(
                "txn-partial-staging",
                tmp_path / "project",
                _target("10-context", "skill", live, desired),
            ),
        )

    journal_path = engine.journal_root / "txn-partial-staging.json"
    raw = json.loads(journal_path.read_text(encoding="utf-8"))
    target = raw["targets"][0]
    assert target["staged_source"] == str(desired.resolve())
    assert target["staging_entries"]
    assert target["staging_active"] is True
    assert any(tmp_path.glob(".csk-txn-*.desired"))
    if kind == "file":
        assert target["staging_created"] is True
        assert target["staging_bytes"] == 0
        assert target["staging_write_bytes"] > 0
        assert target["staging_write_digest"].startswith("sha256:")
    else:
        assert target["staging_created"] is True
        assert target["staging_index"] > 0
        assert target["staging_bytes"] > 0
        assert target["staging_prefix_digest"].startswith("sha256:")

    with ManagerHomeLock(home) as lock:
        TransactionEngine(home).recover(lock)

    assert not live.exists()
    assert not journal_path.exists()
    assert not any(tmp_path.glob(".csk-txn-*"))


def test_staging_entry_intent_is_durable_before_sidecar_creation(tmp_path: Path):
    home = tmp_path / "home"
    desired = _write(tmp_path / "desired", "complete desired bytes")

    def crash_before_create(point: str, target: JournalTarget | None) -> None:
        if point == "before_staging_entry_create":
            raise SimulatedCrash(point)

    engine = TransactionEngine(home, fault_hook=crash_before_create)
    with (
        ManagerHomeLock(home) as lock,
        pytest.raises(SimulatedCrash, match="before_staging_entry_create"),
    ):
        engine.prepare(
            lock,
            _plan(
                "txn-staging-intent",
                tmp_path / "project",
                _target("10-context", "skill", tmp_path / "live", desired),
            ),
        )

    journal_path = engine.journal_root / "txn-staging-intent.json"
    raw = json.loads(journal_path.read_text(encoding="utf-8"))
    target = raw["targets"][0]
    assert target["staging_active"] is True
    assert target["staging_created"] is False
    assert target["staging_index"] == 0
    assert target["staging_entries"][0]["relative_path"] == ""
    assert not any(tmp_path.glob(".csk-txn-*.desired"))

    with ManagerHomeLock(home) as lock:
        TransactionEngine(home).recover(lock)

    assert not journal_path.exists()


def test_completed_staging_manifest_recovers_after_source_disappears(tmp_path: Path):
    home = tmp_path / "home"
    desired = _write(tmp_path / "desired", "complete desired bytes")

    def crash_after_entry(point: str, target: JournalTarget | None) -> None:
        if point == "staging_entry_completed":
            raise SimulatedCrash(point)

    engine = TransactionEngine(home, fault_hook=crash_after_entry)
    with (
        ManagerHomeLock(home) as lock,
        pytest.raises(SimulatedCrash, match="staging_entry_completed"),
    ):
        engine.prepare(
            lock,
            _plan(
                "txn-completed-staging",
                tmp_path / "project",
                _target("10-context", "skill", tmp_path / "live", desired),
            ),
        )

    desired.unlink()
    journal_path = engine.journal_root / "txn-completed-staging.json"
    raw = json.loads(journal_path.read_text(encoding="utf-8"))
    target = raw["targets"][0]
    assert target["staging_index"] == len(target["staging_entries"])
    assert target["staging_active"] is False
    assert any(tmp_path.glob(".csk-txn-*.desired"))

    with ManagerHomeLock(home) as lock:
        TransactionEngine(home).recover(lock)

    assert not journal_path.exists()
    assert not any(tmp_path.glob(".csk-txn-*"))


@pytest.mark.parametrize(
    ("kind", "mutation"),
    [
        ("file", "mutate"),
        ("file", "replace"),
        ("directory", "mutate"),
        ("directory", "replace"),
        ("directory", "add"),
    ],
)
def test_recovery_rejects_foreign_post_crash_staging_bytes(
    tmp_path: Path,
    kind: str,
    mutation: str,
):
    home = tmp_path / "home"
    desired = tmp_path / "desired"
    if kind == "file":
        desired.write_bytes(b"trusted desired bytes\n" * 4096)
        crash_point = "after_staging_chunk_sync"
    else:
        _write(desired / "nested" / "payload", "trusted desired bytes")
        crash_point = "during_staging_copy"

    def crash_during_copy(point: str, target: JournalTarget | None) -> None:
        if point == crash_point:
            raise SimulatedCrash(point)

    transaction_id = f"txn-foreign-staging-{kind}-{mutation}"
    engine = TransactionEngine(home, fault_hook=crash_during_copy)
    with (
        ManagerHomeLock(home) as lock,
        pytest.raises(SimulatedCrash, match=crash_point),
    ):
        engine.prepare(
            lock,
            _plan(
                transaction_id,
                tmp_path / "project",
                _target("10-context", "skill", tmp_path / "live", desired),
            ),
        )

    staged = next(tmp_path.glob(".csk-txn-*.desired"))
    if kind == "file":
        if mutation == "mutate":
            with staged.open("r+b") as handle:
                handle.write(b"FOREIGN")
                handle.flush()
                os.fsync(handle.fileno())
        else:
            size = staged.stat().st_size
            staged.unlink()
            staged.write_bytes(b"F" * size)
    elif mutation == "mutate":
        _write(staged / "nested" / "payload", "foreign mutation")
    elif mutation == "replace":
        shutil.rmtree(staged)
        _write(staged / "replacement", "foreign replacement")
    else:
        _write(staged / "foreign-added", "foreign addition")
    before_recovery = digest_path(staged)
    journal_path = engine.journal_root / f"{transaction_id}.json"

    with (
        ManagerHomeLock(home) as lock,
        pytest.raises(
            TransactionError,
            match="changed|unrecorded|durable|staging",
        ),
    ):
        TransactionEngine(home).recover(lock)

    assert digest_path(staged) == before_recovery
    assert journal_path.exists()
    assert not (tmp_path / "live").exists()


@pytest.mark.parametrize("rollback", [False, True])
def test_recovery_finishes_interrupted_sidecar_cleanup_tomb(
    tmp_path: Path, rollback: bool
):
    home = tmp_path / "home"
    live = _write(tmp_path / "live", "old")
    desired = _write(tmp_path / "desired", "new")

    def crash_after_sidecar_tomb(point: str, target: JournalTarget | None) -> None:
        if rollback and point == "target_committed":
            raise RuntimeError("force rollback")
        if point == "sidecar_tombed":
            raise SimulatedCrash(point)

    engine = TransactionEngine(home, fault_hook=crash_after_sidecar_tomb)
    with ManagerHomeLock(home) as lock:
        engine.prepare(
            lock,
            _plan(
                "txn-sidecar-tomb",
                tmp_path / "project",
                _target("10-context", "skill", live, desired),
            ),
        )
        with pytest.raises(SimulatedCrash, match="sidecar_tombed"):
            engine.commit(lock, "txn-sidecar-tomb")

    journal_path = engine.journal_root / "txn-sidecar-tomb.json"
    raw = json.loads(journal_path.read_text(encoding="utf-8"))
    assert raw["cleanup_sidecars"]
    assert any(
        cleanup["state"] == "pending" and cleanup["expected_digest"] != ABSENT_DIGEST
        for cleanup in raw["cleanup_sidecars"]
    )
    assert any(tmp_path.glob(".csk-txn-*.delete"))

    with ManagerHomeLock(home) as lock:
        TransactionEngine(home).recover(lock)

    assert live.read_text(encoding="utf-8") == ("old" if rollback else "new")
    assert not journal_path.exists()
    assert not any(tmp_path.glob(".csk-txn-*"))


def test_recovery_resumes_recorded_partial_directory_cleanup(tmp_path: Path):
    home = tmp_path / "home"
    live = tmp_path / "live"
    desired = tmp_path / "desired"
    _write(live / "nested" / "one", "old-one")
    _write(live / "nested" / "two", "old-two")
    _write(desired / "nested" / "one", "new-one")
    removed_entries = 0

    def crash_during_cleanup(point: str, target: JournalTarget | None) -> None:
        nonlocal removed_entries
        if point == "sidecar_entry_removed":
            removed_entries += 1
            if removed_entries == 1:
                raise SimulatedCrash(point)

    engine = TransactionEngine(home, fault_hook=crash_during_cleanup)
    with ManagerHomeLock(home) as lock:
        engine.prepare(
            lock,
            _plan(
                "txn-partial-cleanup",
                tmp_path / "project",
                _target("10-context", "skill", live, desired),
            ),
        )
        with pytest.raises(SimulatedCrash, match="sidecar_entry_removed"):
            engine.commit(lock, "txn-partial-cleanup")

    cleanup_tombs = list(tmp_path.glob(".csk-txn-*.delete"))
    assert len(cleanup_tombs) == 1
    assert cleanup_tombs[0].is_dir()
    assert (engine.journal_root / "txn-partial-cleanup.json").exists()

    with ManagerHomeLock(home) as lock:
        TransactionEngine(home).recover(lock)

    assert (live / "nested" / "one").read_text(encoding="utf-8") == "new-one"
    assert not (engine.journal_root / "txn-partial-cleanup.json").exists()
    assert not any(tmp_path.glob(".csk-txn-*"))


@pytest.mark.parametrize("mutation", ["unrecorded", "changed"])
def test_recovery_rejects_foreign_bytes_in_partial_cleanup_tomb(
    tmp_path: Path, mutation: str
):
    home = tmp_path / "home"
    live = tmp_path / "live"
    desired = tmp_path / "desired"
    _write(live / "payload", "old")
    _write(desired / "payload", "new")

    def crash_after_sidecar_tomb(point: str, target: JournalTarget | None) -> None:
        if point == "sidecar_tombed":
            raise SimulatedCrash(point)

    engine = TransactionEngine(home, fault_hook=crash_after_sidecar_tomb)
    with ManagerHomeLock(home) as lock:
        engine.prepare(
            lock,
            _plan(
                "txn-foreign-cleanup",
                tmp_path / "project",
                _target("10-context", "skill", live, desired),
            ),
        )
        with pytest.raises(SimulatedCrash, match="sidecar_tombed"):
            engine.commit(lock, "txn-foreign-cleanup")

    cleanup_tomb = next(tmp_path.glob(".csk-txn-*.delete"))
    if mutation == "unrecorded":
        foreign = _write(cleanup_tomb / "foreign", "must-not-delete")
    else:
        foreign = cleanup_tomb / "payload"
        foreign.write_text("foreign-change", encoding="utf-8")
    with (
        ManagerHomeLock(home) as lock,
        pytest.raises(TransactionError, match="unrecorded|changed cleanup bytes"),
    ):
        TransactionEngine(home).recover(lock)

    assert foreign.read_text(encoding="utf-8") == (
        "must-not-delete" if mutation == "unrecorded" else "foreign-change"
    )
    assert (engine.journal_root / "txn-foreign-cleanup.json").exists()


def test_recovery_finishes_crash_before_final_journal_tomb(tmp_path: Path):
    home = tmp_path / "home"
    live = _write(tmp_path / "live", "old")
    desired = _write(tmp_path / "desired", "new")

    def crash_before_journal_tomb(point: str, target: JournalTarget | None) -> None:
        if point == "before_journal_tomb":
            raise SimulatedCrash(point)

    engine = TransactionEngine(home, fault_hook=crash_before_journal_tomb)
    with ManagerHomeLock(home) as lock:
        engine.prepare(
            lock,
            _plan(
                "txn-before-journal-tomb",
                tmp_path / "project",
                _target("10-context", "skill", live, desired),
            ),
        )
        with pytest.raises(SimulatedCrash, match="before_journal_tomb"):
            engine.commit(lock, "txn-before-journal-tomb")

    assert live.read_text(encoding="utf-8") == "new"
    assert (engine.journal_root / "txn-before-journal-tomb.json").exists()
    assert not any(tmp_path.glob(".csk-txn-*"))

    with ManagerHomeLock(home) as lock:
        TransactionEngine(home).recover(lock)

    assert live.read_text(encoding="utf-8") == "new"
    assert not (engine.journal_root / "txn-before-journal-tomb.json").exists()


@pytest.mark.parametrize("kind", ["file", "directory"])
def test_read_only_targets_stage_commit_and_cleanup(tmp_path: Path, kind: str):
    home = tmp_path / "home"
    live = tmp_path / "live"
    desired = tmp_path / "desired"
    if kind == "file":
        desired.write_text("read-only desired", encoding="utf-8")
        desired.chmod(0o444)
    else:
        _write(desired / "nested" / "payload", "read-only desired")
        (desired / "nested" / "payload").chmod(0o444)
        (desired / "nested").chmod(0o555)
        desired.chmod(0o555)

    engine = TransactionEngine(home)
    with ManagerHomeLock(home) as lock:
        engine.prepare(
            lock,
            _plan(
                f"txn-read-only-{kind}",
                tmp_path / "project",
                _target("10-context", "skill", live, desired),
            ),
        )
        engine.commit(lock, f"txn-read-only-{kind}")

    if kind == "file":
        assert live.read_text(encoding="utf-8") == "read-only desired"
        _assert_mode_identity(live, 0o444)
    else:
        assert (live / "nested" / "payload").read_text(
            encoding="utf-8"
        ) == "read-only desired"
        _assert_mode_identity(live, 0o555)
        _assert_mode_identity(live / "nested", 0o555)
        _assert_mode_identity(live / "nested" / "payload", 0o444)
    assert not (engine.journal_root / f"txn-read-only-{kind}.json").exists()
    assert not any(tmp_path.glob(".csk-txn-*"))


@pytest.mark.parametrize(
    ("kind", "read_only_mode", "writable_mode"),
    [
        ("file", 0o444, 0o666),
        ("directory", 0o555, 0o777),
    ],
)
def test_windows_staging_mode_identity_uses_only_the_settable_read_only_bit(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    kind: str,
    read_only_mode: int,
    writable_mode: int,
):
    staged = tmp_path / "staged"
    if kind == "file":
        staged.write_text("payload", encoding="utf-8")
    else:
        staged.mkdir()
    staged.chmod(writable_mode)
    observed = transactions._staging_tree_entry(staged, "", staged.lstat())
    expected = transactions.StagingTreeEntry(
        relative_path=observed.relative_path,
        kind=observed.kind,
        mode=read_only_mode,
        size=observed.size,
        digest=observed.digest,
        link_target=observed.link_target,
        link_is_directory=observed.link_is_directory,
    )
    monkeypatch.setattr(transactions, "_is_windows", lambda: True)

    construction_mode = transactions._staging_construction_mode(expected)
    transactions._validate_staging_entry_modes(
        staged,
        expected,
        {construction_mode},
    )
    with pytest.raises(TransactionCorruptionError, match="staging entry changed"):
        transactions._validate_staging_entry_modes(
            staged,
            expected,
            {read_only_mode},
        )

    staged.chmod(read_only_mode)
    transactions._validate_staging_entry_modes(
        staged,
        expected,
        {read_only_mode},
    )
    with pytest.raises(TransactionCorruptionError, match="staging entry changed"):
        transactions._validate_staging_entry_modes(
            staged,
            expected,
            {construction_mode},
        )


@pytest.mark.parametrize(
    ("kind", "read_only_mode", "writable_mode"),
    [
        ("file", 0o444, 0o666),
        ("directory", 0o555, 0o777),
    ],
)
def test_windows_cleanup_mode_identity_detects_read_only_attribute_changes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    kind: str,
    read_only_mode: int,
    writable_mode: int,
):
    sidecar = tmp_path / "sidecar"
    if kind == "file":
        sidecar.write_text("payload", encoding="utf-8")
    else:
        sidecar.mkdir()
    sidecar.chmod(writable_mode)
    observed = transactions._cleanup_tree_entry(sidecar, "", sidecar.lstat())
    expected = transactions.CleanupTreeEntry(
        relative_path=observed.relative_path,
        kind=observed.kind,
        mode=read_only_mode,
        digest=observed.digest,
        link_target=observed.link_target,
    )
    monkeypatch.setattr(transactions, "_is_windows", lambda: True)

    cleanup_mode = transactions._cleanup_writable_mode(expected)
    transactions._validate_cleanup_entry_modes(
        sidecar,
        expected,
        {cleanup_mode},
    )
    with pytest.raises(TransactionCorruptionError, match="changed cleanup bytes"):
        transactions._validate_cleanup_entry_modes(
            sidecar,
            expected,
            {read_only_mode},
        )

    sidecar.chmod(read_only_mode)
    transactions._validate_cleanup_entry_modes(
        sidecar,
        expected,
        {read_only_mode},
    )
    with pytest.raises(TransactionCorruptionError, match="changed cleanup bytes"):
        transactions._validate_cleanup_entry_modes(
            sidecar,
            expected,
            {cleanup_mode},
        )


def test_read_only_directory_rollback_restores_mode_and_cleans_sidecars(
    tmp_path: Path,
):
    home = tmp_path / "home"
    live = tmp_path / "live"
    desired = tmp_path / "desired"
    _write(live / "payload", "old")
    _write(desired / "payload", "new")
    (live / "payload").chmod(0o444)
    (desired / "payload").chmod(0o444)
    live.chmod(0o555)
    desired.chmod(0o555)

    def fail_after_commit(point: str, target: JournalTarget | None) -> None:
        if point == "target_committed" and target is not None:
            raise RuntimeError("force read-only rollback")

    engine = TransactionEngine(home, fault_hook=fail_after_commit)
    with ManagerHomeLock(home) as lock:
        engine.prepare(
            lock,
            _plan(
                "txn-read-only-rollback",
                tmp_path / "project",
                _target("10-context", "skill", live, desired),
            ),
        )
        with pytest.raises(RuntimeError, match="force read-only rollback"):
            engine.commit(lock, "txn-read-only-rollback")

    assert (live / "payload").read_text(encoding="utf-8") == "old"
    _assert_mode_identity(live, 0o555)
    _assert_mode_identity(live / "payload", 0o444)
    assert not (engine.journal_root / "txn-read-only-rollback.json").exists()
    assert not any(tmp_path.glob(".csk-txn-*"))


@pytest.mark.parametrize("kind", ["file", "directory"])
def test_recovery_discards_crash_during_read_only_mode_finalization(
    tmp_path: Path,
    kind: str,
):
    home = tmp_path / "home"
    desired = tmp_path / "desired"
    if kind == "file":
        desired.write_text("read-only desired", encoding="utf-8")
        desired.chmod(0o444)
    else:
        _write(desired / "nested" / "payload", "read-only desired")
        (desired / "nested" / "payload").chmod(0o444)
        (desired / "nested").chmod(0o555)
        desired.chmod(0o555)

    def crash_after_mode(point: str, target: JournalTarget | None) -> None:
        if point == "staging_mode_finalized" and target is not None:
            raise SimulatedCrash(point)

    transaction_id = f"txn-read-only-mode-crash-{kind}"
    engine = TransactionEngine(home, fault_hook=crash_after_mode)
    with (
        ManagerHomeLock(home) as lock,
        pytest.raises(SimulatedCrash, match="staging_mode_finalized"),
    ):
        engine.prepare(
            lock,
            _plan(
                transaction_id,
                tmp_path / "project",
                _target("10-context", "skill", tmp_path / "live", desired),
            ),
        )

    journal_path = engine.journal_root / f"{transaction_id}.json"
    raw = json.loads(journal_path.read_text(encoding="utf-8"))
    target = raw["targets"][0]
    assert target["staging_index"] == len(target["staging_entries"])
    assert target["staging_finalize_active"] is True
    assert target["staging_finalize_index"] == 0
    desired.chmod(0o700 if kind == "directory" else 0o600)
    if kind == "directory":
        (desired / "nested").chmod(0o700)
        (desired / "nested" / "payload").chmod(0o600)
        shutil.rmtree(desired)
    else:
        desired.unlink()

    with ManagerHomeLock(home) as lock:
        TransactionEngine(home).recover(lock)

    assert not (tmp_path / "live").exists()
    assert not journal_path.exists()
    assert not any(tmp_path.glob(".csk-txn-*"))


def test_recovery_rejects_foreign_mode_during_staging_finalization(
    tmp_path: Path,
):
    home = tmp_path / "home"
    desired = _write(tmp_path / "desired", "read-only desired")
    desired.chmod(0o444)

    def crash_before_mode(point: str, target: JournalTarget | None) -> None:
        if point == "before_staging_mode_finalize" and target is not None:
            raise SimulatedCrash(point)

    engine = TransactionEngine(home, fault_hook=crash_before_mode)
    with (
        ManagerHomeLock(home) as lock,
        pytest.raises(SimulatedCrash, match="before_staging_mode_finalize"),
    ):
        engine.prepare(
            lock,
            _plan(
                "txn-read-only-mode-foreign",
                tmp_path / "project",
                _target("10-context", "skill", tmp_path / "live", desired),
            ),
        )

    journal_path = engine.journal_root / "txn-read-only-mode-foreign.json"
    raw = json.loads(journal_path.read_text(encoding="utf-8"))
    target = raw["targets"][0]
    staged = Path(target["staged_path"])
    staged.chmod(0o440 if os.name == "nt" else 0o640)
    before_mode = staged.stat().st_mode & 0o777

    with (
        ManagerHomeLock(home) as lock,
        pytest.raises(TransactionError, match="staging entry changed"),
    ):
        TransactionEngine(home).recover(lock)

    assert staged.stat().st_mode & 0o777 == before_mode
    assert journal_path.exists()
    assert not (tmp_path / "live").exists()


def test_recovery_finishes_crash_during_read_only_committed_cleanup(
    tmp_path: Path,
):
    home = tmp_path / "home"
    live = tmp_path / "live"
    desired = tmp_path / "desired"
    _write(live / "payload", "old")
    _write(desired / "nested" / "payload", "new")
    (live / "payload").chmod(0o444)
    (desired / "nested" / "payload").chmod(0o444)
    live.chmod(0o555)
    (desired / "nested").chmod(0o555)
    desired.chmod(0o555)

    def crash_after_writable(point: str, target: JournalTarget | None) -> None:
        if point == "sidecar_mode_writable" and target is not None:
            raise SimulatedCrash(point)

    engine = TransactionEngine(home, fault_hook=crash_after_writable)
    with ManagerHomeLock(home) as lock:
        engine.prepare(
            lock,
            _plan(
                "txn-read-only-cleanup-crash",
                tmp_path / "project",
                _target("10-context", "skill", live, desired),
            ),
        )
        with pytest.raises(SimulatedCrash, match="sidecar_mode_writable"):
            engine.commit(lock, "txn-read-only-cleanup-crash")

    journal_path = engine.journal_root / "txn-read-only-cleanup-crash.json"
    raw = json.loads(journal_path.read_text(encoding="utf-8"))
    assert any(
        cleanup["state"] == "tombed" and cleanup["writable_active"] is True
        for cleanup in raw["cleanup_sidecars"]
    )
    assert (live / "nested" / "payload").read_text(encoding="utf-8") == "new"
    _assert_mode_identity(live, 0o555)

    with ManagerHomeLock(home) as lock:
        TransactionEngine(home).recover(lock)

    assert (live / "nested" / "payload").read_text(encoding="utf-8") == "new"
    _assert_mode_identity(live, 0o555)
    _assert_mode_identity(live / "nested", 0o555)
    _assert_mode_identity(live / "nested" / "payload", 0o444)
    assert not journal_path.exists()
    assert not any(tmp_path.glob(".csk-txn-*"))


def test_recovery_rejects_foreign_mode_during_read_only_cleanup(
    tmp_path: Path,
):
    home = tmp_path / "home"
    live = tmp_path / "live"
    desired = tmp_path / "desired"
    _write(live / "payload", "old")
    _write(desired / "payload", "new")
    (live / "payload").chmod(0o444)
    (desired / "payload").chmod(0o444)
    live.chmod(0o555)
    desired.chmod(0o555)

    def crash_before_writable(point: str, target: JournalTarget | None) -> None:
        if point == "before_sidecar_mode_writable" and target is not None:
            raise SimulatedCrash(point)

    engine = TransactionEngine(home, fault_hook=crash_before_writable)
    with ManagerHomeLock(home) as lock:
        engine.prepare(
            lock,
            _plan(
                "txn-read-only-cleanup-foreign",
                tmp_path / "project",
                _target("10-context", "skill", live, desired),
            ),
        )
        with pytest.raises(SimulatedCrash, match="before_sidecar_mode_writable"):
            engine.commit(lock, "txn-read-only-cleanup-foreign")

    journal_path = engine.journal_root / "txn-read-only-cleanup-foreign.json"
    raw = json.loads(journal_path.read_text(encoding="utf-8"))
    active = next(
        cleanup
        for cleanup in raw["cleanup_sidecars"]
        if cleanup["state"] == "tombed" and cleanup["writable_active"] is True
    )
    order = sorted(
        active["entries"],
        key=lambda entry: (
            len(Path(entry["relative_path"]).parts) if entry["relative_path"] else 0,
            0 if entry["kind"] == "directory" else 1,
            entry["relative_path"].encode("utf-8"),
        ),
    )
    entry = order[active["writable_index"]]
    entry_path = Path(active["tomb_path"])
    if entry["relative_path"]:
        entry_path = entry_path.joinpath(*Path(entry["relative_path"]).parts)
    entry_path.chmod(0o751)
    before_mode = entry_path.stat().st_mode & 0o777

    with (
        ManagerHomeLock(home) as lock,
        pytest.raises(TransactionError, match="changed cleanup bytes"),
    ):
        TransactionEngine(home).recover(lock)

    assert entry_path.stat().st_mode & 0o777 == before_mode
    assert journal_path.exists()
    assert (live / "payload").read_text(encoding="utf-8") == "new"


def test_journal_records_complete_ordered_recovery_state_and_consumer_commits_last(
    tmp_path: Path,
):
    home = tmp_path / "home"
    project = tmp_path / "project"
    events: list[tuple[str, str]] = []
    backup_paths: list[Path] = []
    backups_present_at_consumer_commit: list[bool] = []

    def observe(point: str, target: JournalTarget | None) -> None:
        if point == "target_committed" and target is not None:
            events.append((target.target_class, target.identifier))
            if target.target_class == "90-consumer":
                backups_present_at_consumer_commit.append(
                    all(path.exists() for path in backup_paths)
                )

    engine = TransactionEngine(home, fault_hook=observe)
    live_a = _write(tmp_path / "live-a", "old-a")
    live_b = _write(tmp_path / "live-b", "old-b")
    ledger = _write(tmp_path / "consumers.json", "[]")
    desired_a = _write(tmp_path / "desired-a", "new-a")
    desired_b = _write(tmp_path / "desired-b", "new-b")
    desired_ledger = _write(tmp_path / "desired-consumers", '["project"]')
    plan = _plan(
        "txn-order",
        project,
        _target("90-consumer", "ledger", ledger, desired_ledger),
        _target("10-context", "é", live_a, desired_a),
        _target("10-context", "z", live_b, desired_b),
    )

    with ProjectLock(home, project), ManagerHomeLock(home) as lock:
        journal = engine.prepare(lock, plan)
        backup_paths.extend(Path(target.backup_path) for target in journal.targets)
        raw = json.loads(
            (engine.journal_root / "txn-order.json").read_text(encoding="utf-8")
        )
        assert journal.ordered_target_classes == ["10-context", "90-consumer"]
        assert [
            (target.target_class, target.identifier) for target in journal.targets
        ] == [
            ("10-context", "z"),
            ("10-context", "é"),
            ("90-consumer", "ledger"),
        ]
        assert raw["transaction_id"] == "txn-order"
        assert raw["project_identity"] == str(project.resolve())
        assert raw["generation_digests"] == {"runtime/default": "sha256:" + "a" * 64}
        assert all(target["backup_path"] for target in raw["targets"])
        assert all(
            target["desired_digest"].startswith("sha256:") for target in raw["targets"]
        )
        assert all(target["state"] == "pending" for target in raw["targets"])
        engine.commit(lock, "txn-order")

    assert events[-1] == ("90-consumer", "ledger")
    assert backups_present_at_consumer_commit == [True]
    assert live_a.read_text(encoding="utf-8") == "new-a"
    assert live_b.read_text(encoding="utf-8") == "new-b"
    assert ledger.read_text(encoding="utf-8") == '["project"]'
    assert not (engine.journal_root / "txn-order.json").exists()


@pytest.mark.parametrize(
    "crash_point",
    ["after_backup", "after_install", "target_committed", "before_cleanup"],
)
def test_recovery_completes_interrupted_commit_for_any_initiating_project(
    tmp_path: Path, crash_point: str
):
    home = tmp_path / "home"
    project_a = tmp_path / "project-a"
    project_b = tmp_path / "project-b"
    live = _write(tmp_path / "live", "old")
    desired = _write(tmp_path / "desired", "new")
    crashed = False

    def crash(point: str, target: JournalTarget | None) -> None:
        nonlocal crashed
        if not crashed and point == crash_point:
            crashed = True
            raise SimulatedCrash(point)

    first = TransactionEngine(home, fault_hook=crash)
    with ProjectLock(home, project_a), ManagerHomeLock(home) as lock:
        first.prepare(
            lock,
            _plan(
                "txn-crash", project_a, _target("10-context", "skill", live, desired)
            ),
        )
        with pytest.raises(SimulatedCrash):
            first.commit(lock, "txn-crash")

    recovering = TransactionEngine(home)
    with ProjectLock(home, project_b), ManagerHomeLock(home) as lock:
        recovering.recover(lock)

    assert live.read_text(encoding="utf-8") == "new"
    assert not (recovering.journal_root / "txn-crash.json").exists()


def test_commit_failure_rolls_back_in_exact_reverse_commit_order(tmp_path: Path):
    home = tmp_path / "home"
    restored: list[str] = []

    def fail_last(point: str, target: JournalTarget | None) -> None:
        if point == "after_restore" and target is not None:
            restored.append(target.identifier)
        if (
            point == "target_committed"
            and target is not None
            and target.identifier == "c"
        ):
            raise RuntimeError("injected failure")

    engine = TransactionEngine(home, fault_hook=fail_last)
    targets: list[MutableTarget] = []
    for identifier in ("a", "b", "c"):
        live = _write(tmp_path / f"live-{identifier}", f"old-{identifier}")
        desired = _write(tmp_path / f"desired-{identifier}", f"new-{identifier}")
        targets.append(_target("10-context", identifier, live, desired))

    with ManagerHomeLock(home) as lock:
        engine.prepare(lock, _plan("txn-rollback", tmp_path / "project", *targets))
        with pytest.raises(RuntimeError, match="injected failure"):
            engine.commit(lock, "txn-rollback")

    assert restored == ["c", "b", "a"]
    for identifier in ("a", "b", "c"):
        assert (tmp_path / f"live-{identifier}").read_text(
            encoding="utf-8"
        ) == f"old-{identifier}"
    assert not (engine.journal_root / "txn-rollback.json").exists()


def test_commit_race_preserves_competing_success_then_recovers(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    home = tmp_path / "home"
    live = _write(tmp_path / "live", "preimage")
    desired = _write(tmp_path / "desired", "transaction")
    engine = TransactionEngine(home)

    with ManagerHomeLock(home) as lock:
        journal = engine.prepare(
            lock,
            _plan(
                "txn-commit-race",
                tmp_path / "project",
                _target("10-context", "skill", live, desired),
            ),
        )
        record = journal.targets[0]
        staged = Path(record.staged_path or "")
        backup = Path(record.backup_path)
        native_rename = transactions._native_rename_no_replace
        injected = False

        def create_competitor_then_rename(source: Path, destination: Path) -> None:
            nonlocal injected
            if not injected and source == staged and destination == live:
                _write(destination, "competing-success")
                injected = True
            native_rename(source, destination)

        monkeypatch.setattr(
            transactions,
            "_native_rename_no_replace",
            create_competitor_then_rename,
        )

        with pytest.raises(ExceptionGroup):
            engine.commit(lock, "txn-commit-race")

        assert injected
        assert live.read_text(encoding="utf-8") == "competing-success"
        assert backup.read_text(encoding="utf-8") == "preimage"
        assert staged.read_text(encoding="utf-8") == "transaction"
        assert (engine.journal_root / "txn-commit-race.json").exists()

        live.unlink()
        engine.recover(lock)

    assert live.read_text(encoding="utf-8") == "preimage"
    assert not staged.exists()
    assert not backup.exists()
    assert not (engine.journal_root / "txn-commit-race.json").exists()


def test_rollback_race_preserves_competing_directory_then_recovers(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    home = tmp_path / "home"
    live = tmp_path / "live"
    desired = tmp_path / "desired"
    _write(live / "payload", "preimage")
    _write(desired / "payload", "transaction")

    def fail_after_commit(point: str, target: JournalTarget | None) -> None:
        if point == "target_committed":
            raise RuntimeError("force rollback")

    engine = TransactionEngine(home, fault_hook=fail_after_commit)
    with ManagerHomeLock(home) as lock:
        journal = engine.prepare(
            lock,
            _plan(
                "txn-rollback-race",
                tmp_path / "project",
                _target("10-context", "skill", live, desired),
            ),
        )
        record = journal.targets[0]
        backup = Path(record.backup_path)
        rollback = Path(record.rollback_path)
        native_rename = transactions._native_rename_no_replace
        injected = False

        def create_competitor_then_rename(source: Path, destination: Path) -> None:
            nonlocal injected
            if not injected and source == backup and destination == live:
                _write(destination / "payload", "competing-success")
                injected = True
            native_rename(source, destination)

        monkeypatch.setattr(
            transactions,
            "_native_rename_no_replace",
            create_competitor_then_rename,
        )

        with pytest.raises(ExceptionGroup):
            engine.commit(lock, "txn-rollback-race")

        assert injected
        assert (live / "payload").read_text(encoding="utf-8") == "competing-success"
        assert (backup / "payload").read_text(encoding="utf-8") == "preimage"
        assert (rollback / "payload").read_text(encoding="utf-8") == "transaction"
        assert (engine.journal_root / "txn-rollback-race.json").exists()

        shutil.rmtree(live)
        engine.recover(lock)

    assert (live / "payload").read_text(encoding="utf-8") == "preimage"
    assert not backup.exists()
    assert not rollback.exists()
    assert not (engine.journal_root / "txn-rollback-race.json").exists()


def test_prepare_rejects_absent_live_parent_without_namespace_residue(
    tmp_path: Path,
):
    home = tmp_path / "home"
    existing_live = _write(tmp_path / "existing" / "live", "old")
    existing_desired = _write(tmp_path / "existing-desired", "new")
    absent_parent = tmp_path / "absent-parent"
    absent_live = absent_parent / "live"
    absent_desired = _write(tmp_path / "absent-desired", "new")
    engine = TransactionEngine(home)

    with (
        ManagerHomeLock(home) as lock,
        pytest.raises(TransactionError, match="staging parent does not exist"),
    ):
        engine.prepare(
            lock,
            _plan(
                "txn-absent-parent",
                tmp_path / "project",
                _target("10-context", "a", existing_live, existing_desired),
                _target("10-context", "b", absent_live, absent_desired),
            ),
        )

    assert existing_live.read_text(encoding="utf-8") == "old"
    assert not absent_parent.exists()
    assert not (engine.journal_root / "txn-absent-parent.json").exists()
    assert not any((tmp_path / "existing").glob(".csk-txn-*"))


@pytest.mark.parametrize("relation", ["journal-ancestor", "journal-descendant"])
def test_prepare_rejects_target_overlapping_journal_namespace_without_residue(
    tmp_path: Path, relation: str
):
    home = tmp_path / "home"
    engine = TransactionEngine(home)
    live = (
        home / "state"
        if relation == "journal-ancestor"
        else engine.journal_root / "live"
    )
    desired = _write(tmp_path / f"desired-{relation}", "wanted")

    with (
        ManagerHomeLock(home) as lock,
        pytest.raises(TransactionError, match="namespace overlap"),
    ):
        engine.prepare(
            lock,
            _plan(
                f"txn-{relation}",
                tmp_path / "project",
                _target("10-context", relation, live, desired),
            ),
        )

    assert not live.exists()
    assert not engine.journal_root.exists()
    assert not any(home.glob(".csk-txn-*"))


@pytest.mark.parametrize(
    "relative_live",
    [
        ".lock",
        ".lock.stale-123",
        ".lock.stale-123/captured",
        "locks",
        "locks/projects",
        "locks/projects/arbitrary.lock",
        "locks/builds",
        "locks/builds/arbitrary.lock",
    ],
)
def test_prepare_rejects_manager_lock_namespaces_before_mutation(
    tmp_path: Path,
    relative_live: str,
):
    home = tmp_path / "home"
    engine = TransactionEngine(home)
    live = home / relative_live
    desired = _write(tmp_path / "desired-lock-namespace", "replacement")

    with ManagerHomeLock(home) as lock:
        witness = _held_lock_bytes(lock)
        with pytest.raises(TransactionError, match="lock.*namespace|namespace.*lock"):
            engine.prepare(
                lock,
                _plan(
                    "txn-lock-namespace",
                    tmp_path / "project",
                    _namespace_target("10-context", relative_live, live, desired),
                ),
            )
        lock.assert_held()
        assert _held_lock_bytes(lock) == witness
        assert not engine.journal_root.exists()
        assert not any(live.parent.glob(".csk-txn-*"))


def test_prepare_rejects_held_project_lock_and_preserves_both_witnesses(
    tmp_path: Path,
):
    home = tmp_path / "home"
    project_lock = ProjectLock(home, tmp_path / "project")
    engine = TransactionEngine(home)
    desired = _write(tmp_path / "desired-project-lock", "replacement")

    with project_lock, ManagerHomeLock(home) as home_lock:
        project_witness = _held_lock_bytes(project_lock)
        home_witness = _held_lock_bytes(home_lock)
        with pytest.raises(TransactionError, match="project lock namespace"):
            engine.prepare(
                home_lock,
                _plan(
                    "txn-held-project-lock",
                    tmp_path / "project",
                    _namespace_target(
                        "10-context",
                        "held-project-lock",
                        project_lock.path,
                        desired,
                    ),
                ),
            )
        project_lock.assert_held()
        home_lock.assert_held()
        assert _held_lock_bytes(project_lock) == project_witness
        assert _held_lock_bytes(home_lock) == home_witness
        assert not engine.journal_root.exists()


def test_build_plan_rejects_held_build_lock_and_preserves_outer_witnesses(
    tmp_path: Path,
):
    home = tmp_path / "home"
    project_lock = ProjectLock(home, tmp_path / "project")
    build_lock = BuildLock(home, "build-key")
    engine = TransactionEngine(home)
    desired = _write(tmp_path / "desired-build-lock", "replacement")

    with project_lock, build_lock:
        project_witness = _held_lock_bytes(project_lock)
        build_witness = _held_lock_bytes(build_lock)
        with pytest.raises(TransactionError, match="build lock namespace"):
            engine._build_journal(
                _plan(
                    "txn-held-build-lock",
                    tmp_path / "project",
                    _namespace_target(
                        "10-context",
                        "held-build-lock",
                        build_lock.path,
                        desired,
                    ),
                )
            )
        project_lock.assert_held()
        build_lock.assert_held()
        assert _held_lock_bytes(project_lock) == project_witness
        assert _held_lock_bytes(build_lock) == build_witness
        assert not engine.journal_root.exists()


def test_prepare_rejects_physical_alias_of_project_lock_namespace(
    tmp_path: Path,
):
    home = tmp_path / "home"
    project_namespace = home / "locks" / "projects"
    project_namespace.mkdir(parents=True)
    alias = tmp_path / "project-lock-alias"
    try:
        alias.symlink_to(project_namespace, target_is_directory=True)
    except OSError as exc:
        pytest.skip(f"symlinks unavailable: {exc}")
    engine = TransactionEngine(home)
    desired = _write(tmp_path / "desired-lock-alias", "replacement")

    with ManagerHomeLock(home) as lock:
        witness = _held_lock_bytes(lock)
        with pytest.raises(TransactionError, match="project lock namespace"):
            engine.prepare(
                lock,
                _plan(
                    "txn-lock-alias",
                    tmp_path / "project",
                    _target(
                        "10-context",
                        "project-lock-alias",
                        alias / "arbitrary.lock",
                        desired,
                    ),
                ),
            )
        lock.assert_held()
        assert _held_lock_bytes(lock) == witness
        assert not engine.journal_root.exists()


def test_prepare_rejects_parent_child_targets_without_residue(tmp_path: Path):
    home = tmp_path / "home"
    engine = TransactionEngine(home)
    parent = tmp_path / "live"
    child = parent / "child"
    desired_parent = _write(tmp_path / "desired-parent", "parent")
    desired_child = _write(tmp_path / "desired-child", "child")

    with (
        ManagerHomeLock(home) as lock,
        pytest.raises(TransactionError, match="namespace overlap"),
    ):
        engine.prepare(
            lock,
            _plan(
                "txn-parent-child",
                tmp_path / "project",
                _target("10-context", "parent", parent, desired_parent),
                _target("10-context", "child", child, desired_child),
            ),
        )

    assert not parent.exists()
    assert not engine.journal_root.exists()
    assert not any(tmp_path.glob(".csk-txn-*"))


def test_prepare_rejects_live_path_equal_to_another_target_sidecar(
    tmp_path: Path,
):
    home = tmp_path / "home"
    engine = TransactionEngine(home)
    transaction_id = "txn-sidecar-live"
    first_live = tmp_path / "first-live"
    second_live = transactions._sidecar(first_live, transaction_id, 0, "backup")
    first_desired = _write(tmp_path / "first-desired", "first")
    second_desired = _write(tmp_path / "second-desired", "second")

    with (
        ManagerHomeLock(home) as lock,
        pytest.raises(TransactionError, match="namespace overlap"),
    ):
        engine.prepare(
            lock,
            _plan(
                transaction_id,
                tmp_path / "project",
                _target("10-context", "a", first_live, first_desired),
                _target("10-context", "b", second_live, second_desired),
            ),
        )

    assert not first_live.exists()
    assert not second_live.exists()
    assert not engine.journal_root.exists()
    assert not any(tmp_path.glob(".csk-txn-*"))


def test_prepare_rejects_symlinked_parent_child_alias_without_residue(
    tmp_path: Path,
):
    home = tmp_path / "home"
    engine = TransactionEngine(home)
    physical_parent = tmp_path / "physical"
    physical_parent.mkdir()
    alias = tmp_path / "alias"
    try:
        alias.symlink_to(physical_parent, target_is_directory=True)
    except OSError as exc:
        pytest.skip(f"symlinks unavailable: {exc}")
    desired_parent = tmp_path / "desired-parent"
    desired_parent.mkdir()
    desired_child = _write(tmp_path / "desired-child", "child")

    with (
        ManagerHomeLock(home) as lock,
        pytest.raises(TransactionError, match="namespace overlap"),
    ):
        engine.prepare(
            lock,
            _plan(
                "txn-symlink-alias",
                tmp_path / "project",
                _target(
                    "10-context",
                    "parent",
                    physical_parent,
                    desired_parent,
                ),
                _target(
                    "10-context",
                    "child",
                    alias / "child",
                    desired_child,
                ),
            ),
        )

    assert not (physical_parent / "child").exists()
    assert not engine.journal_root.exists()
    assert not any(tmp_path.glob(".csk-txn-*"))


@pytest.mark.skipif(
    sys.platform != "darwin" and os.name != "nt",
    reason="platform path matching is case-sensitive",
)
def test_prepare_rejects_platform_case_alias_without_residue(tmp_path: Path):
    home = tmp_path / "home"
    engine = TransactionEngine(home)
    desired_a = _write(tmp_path / "desired-a", "a")
    desired_b = _write(tmp_path / "desired-b", "b")

    with (
        ManagerHomeLock(home) as lock,
        pytest.raises(TransactionError, match="namespace overlap"),
    ):
        engine.prepare(
            lock,
            _plan(
                "txn-case-alias",
                tmp_path / "project",
                _target("10-context", "a", tmp_path / "Target", desired_a),
                _target("10-context", "b", tmp_path / "target", desired_b),
            ),
        )

    assert not (tmp_path / "Target").exists()
    assert not (tmp_path / "target").exists()
    assert not engine.journal_root.exists()
    assert not any(tmp_path.glob(".csk-txn-*"))


@pytest.mark.skipif(
    sys.platform != "darwin",
    reason="canonical Unicode aliases are a macOS filesystem behavior",
)
def test_prepare_rejects_platform_unicode_alias_without_residue(tmp_path: Path):
    home = tmp_path / "home"
    engine = TransactionEngine(home)
    desired_a = _write(tmp_path / "desired-a", "a")
    desired_b = _write(tmp_path / "desired-b", "b")

    with (
        ManagerHomeLock(home) as lock,
        pytest.raises(TransactionError, match="namespace overlap"),
    ):
        engine.prepare(
            lock,
            _plan(
                "txn-unicode-alias",
                tmp_path / "project",
                _target("10-context", "a", tmp_path / "é", desired_a),
                _target("10-context", "b", tmp_path / "e\u0301", desired_b),
            ),
        )

    assert not (tmp_path / "é").exists()
    assert not (tmp_path / "e\u0301").exists()
    assert not engine.journal_root.exists()
    assert not any(tmp_path.glob(".csk-txn-*"))


def test_stale_preimage_is_rejected_without_overwriting_newer_state(tmp_path: Path):
    home = tmp_path / "home"
    live = _write(tmp_path / "live", "old")
    desired = _write(tmp_path / "desired", "wanted")
    engine = TransactionEngine(home)

    with ManagerHomeLock(home) as lock:
        engine.prepare(
            lock,
            _plan(
                "txn-stale",
                tmp_path / "project",
                _target("10-context", "skill", live, desired),
            ),
        )
        live.write_text("newer-writer", encoding="utf-8")
        with pytest.raises(TransactionCorruptionError, match="stale preimage"):
            engine.commit(lock, "txn-stale")

    assert live.read_text(encoding="utf-8") == "newer-writer"
    assert not (engine.journal_root / "txn-stale.json").exists()


def test_generation_change_is_rejected_at_commit(tmp_path: Path):
    home = tmp_path / "home"
    live = tmp_path / "live"
    desired = tmp_path / "desired"
    _write(live / "generation.txt", "generation-1")
    _write(live / "payload", "old")
    _write(desired / "generation.txt", "generation-2")
    _write(desired / "payload", "new")
    target = MutableTarget(
        target_class="20-runtime",
        identifier="runtime",
        live_path=live,
        desired_path=desired,
        expected_generation="generation-1",
        generation_path="generation.txt",
    )
    engine = TransactionEngine(home)

    with ManagerHomeLock(home) as lock:
        engine.prepare(lock, _plan("txn-generation", tmp_path / "project", target))
        (live / "generation.txt").write_text("generation-newer", encoding="utf-8")
        with pytest.raises(TransactionCorruptionError, match="stale generation"):
            engine.commit(lock, "txn-generation")

    assert (live / "generation.txt").read_text(encoding="utf-8") == "generation-newer"


def test_rollback_refuses_to_overwrite_unknown_current_bytes_and_keeps_journal(
    tmp_path: Path,
):
    home = tmp_path / "home"
    live = _write(tmp_path / "live", "old")
    desired = _write(tmp_path / "desired", "wanted")

    def replace_desired_then_fail(point: str, target: JournalTarget | None) -> None:
        if point == "after_install" and target is not None:
            Path(target.live_path).write_text("foreign-success", encoding="utf-8")
            raise RuntimeError("injected failure")

    engine = TransactionEngine(home, fault_hook=replace_desired_then_fail)
    with ManagerHomeLock(home) as lock:
        engine.prepare(
            lock,
            _plan(
                "txn-defense",
                tmp_path / "project",
                _target("10-context", "skill", live, desired),
            ),
        )
        with pytest.raises(ExceptionGroup):
            engine.commit(lock, "txn-defense")

    assert live.read_text(encoding="utf-8") == "foreign-success"
    assert (engine.journal_root / "txn-defense.json").exists()
    with (
        ManagerHomeLock(home) as lock,
        pytest.raises(TransactionError, match="rollback refused unknown current bytes"),
    ):
        TransactionEngine(home).recover(lock)
    assert live.read_text(encoding="utf-8") == "foreign-success"


def test_corrupt_journal_cannot_redirect_recovery_sidecars(tmp_path: Path):
    home = tmp_path / "home"
    live = _write(tmp_path / "live", "old")
    desired = _write(tmp_path / "desired", "wanted")
    sentinel = _write(tmp_path / "must-not-touch", "foreign")
    engine = TransactionEngine(home)

    with ManagerHomeLock(home) as lock:
        engine.prepare(
            lock,
            _plan(
                "txn-corrupt-path",
                tmp_path / "project",
                _target("10-context", "skill", live, desired),
            ),
        )
        journal_path = engine.journal_root / "txn-corrupt-path.json"
        raw = json.loads(journal_path.read_text(encoding="utf-8"))
        raw["targets"][0]["backup_path"] = str(sentinel)
        journal_path.write_text(
            json.dumps(raw, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
            + "\n",
            encoding="utf-8",
        )
        with pytest.raises(TransactionCorruptionError, match="sidecar path"):
            engine.commit(lock, "txn-corrupt-path")

    assert sentinel.read_text(encoding="utf-8") == "foreign"
    assert live.read_text(encoding="utf-8") == "old"
    assert journal_path.exists()


def test_corrupt_journal_cannot_redirect_live_target_into_home_lock(
    tmp_path: Path,
):
    home = tmp_path / "home"
    live = _write(tmp_path / "live", "old")
    desired = _write(tmp_path / "desired", "wanted")
    engine = TransactionEngine(home)

    with ManagerHomeLock(home) as lock:
        engine.prepare(
            lock,
            _plan(
                "txn-corrupt-lock-path",
                tmp_path / "project",
                _target("10-context", "skill", live, desired),
            ),
        )
        journal_path = engine.journal_root / "txn-corrupt-lock-path.json"
        raw = json.loads(journal_path.read_text(encoding="utf-8"))
        target = raw["targets"][0]
        redirected_live = lock.path.resolve()
        target["live_path"] = str(redirected_live)
        target["staged_path"] = str(
            transactions._sidecar(
                redirected_live,
                "txn-corrupt-lock-path",
                0,
                "desired",
            )
        )
        target["backup_path"] = str(
            transactions._sidecar(
                redirected_live,
                "txn-corrupt-lock-path",
                0,
                "backup",
            )
        )
        target["rollback_path"] = str(
            transactions._sidecar(
                redirected_live,
                "txn-corrupt-lock-path",
                0,
                "rollback",
            )
        )
        journal_path.write_text(
            json.dumps(raw, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
            + "\n",
            encoding="utf-8",
        )
        witness = _held_lock_bytes(lock)

        with pytest.raises(
            TransactionCorruptionError,
            match="manager-home lock namespace",
        ):
            engine.commit(lock, "txn-corrupt-lock-path")

        lock.assert_held()
        assert _held_lock_bytes(lock) == witness

    assert live.read_text(encoding="utf-8") == "old"
    assert journal_path.exists()


def test_commit_reports_home_lock_witness_loss_at_mutation_boundary(
    tmp_path: Path,
):
    home = tmp_path / "home"
    live = _write(tmp_path / "live", "old")
    desired = _write(tmp_path / "desired", "wanted")
    active_lock: list[ManagerHomeLock] = []

    def corrupt_witness(point: str, target: JournalTarget | None) -> None:
        if point == "after_install" and target is not None:
            assert active_lock[0]._fd is not None
            locking._write_lock_fd(
                active_lock[0]._fd,
                {
                    "protocol": transactions.JOURNAL_SCHEMA,
                    "pid": os.getpid(),
                    "token": "foreign",
                },
            )

    engine = TransactionEngine(home, fault_hook=corrupt_witness)
    with ManagerHomeLock(home) as lock:
        active_lock.append(lock)
        engine.prepare(
            lock,
            _plan(
                "txn-witness-loss",
                tmp_path / "project",
                _target("10-context", "skill", live, desired),
            ),
        )
        with pytest.raises(LockError, match="ownership was lost"):
            engine.commit(lock, "txn-witness-loss")


def test_concurrent_project_transactions_preserve_both_consumers(tmp_path: Path):
    home = tmp_path / "home"
    ledger = _write(tmp_path / "consumers.json", "[]")
    barrier = threading.Barrier(2)
    errors: list[Exception] = []

    def install(project_name: str) -> None:
        project = tmp_path / project_name
        try:
            with ProjectLock(home, project, timeout=3):
                barrier.wait(timeout=3)
                with ManagerHomeLock(home, timeout=3) as lock:
                    consumers = set(json.loads(ledger.read_text(encoding="utf-8")))
                    consumers.add(project_name)
                    desired = _write(
                        tmp_path / f"desired-{project_name}",
                        json.dumps(sorted(consumers)),
                    )
                    engine = TransactionEngine(home)
                    engine.recover(lock)
                    engine.prepare(
                        lock,
                        _plan(
                            f"txn-{project_name}",
                            project,
                            _target("90-consumer", "ledger", ledger, desired),
                        ),
                    )
                    engine.commit(lock, f"txn-{project_name}")
        except Exception as exc:  # noqa: BLE001 - surface worker failures in the parent test
            errors.append(exc)

    threads = [
        threading.Thread(target=install, args=(name,))
        for name in ("project-a", "project-b")
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=5)

    assert not errors
    assert all(not thread.is_alive() for thread in threads)
    assert set(json.loads(ledger.read_text(encoding="utf-8"))) == {
        "project-a",
        "project-b",
    }


def test_failed_second_project_restores_ledger_preimage_containing_first_success(
    tmp_path: Path,
):
    home = tmp_path / "home"
    ledger = _write(tmp_path / "consumers.json", "[]")

    def commit_project(name: str, *, fail: bool = False) -> None:
        project = tmp_path / name

        def fault(point: str, target: JournalTarget | None) -> None:
            if (
                fail
                and point == "target_committed"
                and target is not None
                and target.target_class == "90-consumer"
            ):
                raise RuntimeError("consumer publication failed")

        with ProjectLock(home, project), ManagerHomeLock(home) as lock:
            existing = set(json.loads(ledger.read_text(encoding="utf-8")))
            existing.add(name)
            desired_ledger = _write(
                tmp_path / f"desired-ledger-{name}", json.dumps(sorted(existing))
            )
            live_context = tmp_path / f"context-{name}"
            desired_context = _write(tmp_path / f"desired-context-{name}", name)
            engine = TransactionEngine(home, fault_hook=fault)
            engine.prepare(
                lock,
                _plan(
                    f"txn-{name}",
                    project,
                    _target("10-context", name, live_context, desired_context),
                    _target("90-consumer", "ledger", ledger, desired_ledger),
                ),
            )
            if fail:
                with pytest.raises(RuntimeError, match="consumer publication failed"):
                    engine.commit(lock, f"txn-{name}")
            else:
                engine.commit(lock, f"txn-{name}")

    commit_project("project-a")
    commit_project("project-b", fail=True)

    assert json.loads(ledger.read_text(encoding="utf-8")) == ["project-a"]
    assert (tmp_path / "context-project-a").read_text(encoding="utf-8") == "project-a"
    assert not (tmp_path / "context-project-b").exists()


def test_removal_target_uses_absent_desired_digest_and_is_rollback_safe(tmp_path: Path):
    home = tmp_path / "home"
    live = _write(tmp_path / "stale-managed", "old")
    engine = TransactionEngine(home)
    target = _target("80-removal", "stale", live, None)

    with ManagerHomeLock(home) as lock:
        journal = engine.prepare(
            lock, _plan("txn-remove", tmp_path / "project", target)
        )
        assert journal.targets[0].desired_digest == ABSENT_DIGEST
        engine.commit(lock, "txn-remove")

    assert not live.exists()


@pytest.mark.parametrize(
    "operation",
    ["prepare", "commit", "recover", "referenced_generation_digests"],
)
def test_home_lock_witness_is_bound_to_the_engine_home_before_state_access(
    tmp_path: Path,
    operation: str,
):
    home_a = tmp_path / "home-a"
    home_b = tmp_path / "home-b"
    live = _write(tmp_path / "targets" / "live", "old")
    desired = _write(tmp_path / "desired", "new")
    engine = TransactionEngine(home_b)

    with ManagerHomeLock(home_a) as wrong_lock:
        with pytest.raises(
            TransactionError, match="manager-home.*identity|home.*witness"
        ):
            if operation == "prepare":
                engine.prepare(
                    wrong_lock,
                    _plan(
                        "txn-wrong-home",
                        tmp_path / "project",
                        _target("10-context", "skill", live, desired),
                    ),
                )
            elif operation == "commit":
                engine.commit(wrong_lock, "txn-wrong-home")
            elif operation == "recover":
                engine.recover(wrong_lock)
            else:
                engine.referenced_generation_digests(wrong_lock)

        wrong_lock.assert_held()
        assert live.read_text(encoding="utf-8") == "old"
        assert not home_b.exists()
        assert not any(live.parent.glob(".csk-txn-*"))

    with ManagerHomeLock(home_b) as correct_lock:
        assert engine.referenced_generation_digests(correct_lock) == {}


def test_entry_target_commit_replaces_the_link_without_mutating_referents(
    tmp_path: Path,
):
    home = tmp_path / "home"
    old_referent = _write(tmp_path / "old-store" / "payload", "old referent")
    new_referent = _write(tmp_path / "new-store" / "payload", "new referent")
    live = _symlink(tmp_path / "published" / "skill", "../old-store", directory=True)
    desired = _symlink(tmp_path / "staged" / "skill", "../new-store", directory=True)
    engine = TransactionEngine(home)

    with ManagerHomeLock(home) as lock:
        engine.prepare(
            lock,
            _plan(
                "txn-entry-commit",
                tmp_path / "project",
                _entry_target("60-adapter", "skill", live, desired),
            ),
        )
        engine.commit(lock, "txn-entry-commit")

    assert live.is_symlink()
    assert os.readlink(live) == "../new-store"
    assert old_referent.read_text(encoding="utf-8") == "old referent"
    assert new_referent.read_text(encoding="utf-8") == "new referent"
    assert not any(live.parent.glob(".csk-txn-*"))


def test_entry_kind_digests_the_link_destination_while_bytes_remain_strict(
    tmp_path: Path,
):
    referent = _write(tmp_path / "store" / "payload", "referent")
    first = _symlink(tmp_path / "first", "store", directory=True)
    second = _symlink(tmp_path / "second", "store", directory=True)
    changed = _symlink(tmp_path / "changed", "other", directory=True)

    with pytest.raises(TransactionError, match="unsafe transaction target"):
        digest_path(first)

    first_digest = transactions.digest_target(first, kind="entry")
    assert first_digest == transactions.digest_target(second, kind="entry")
    assert first_digest != transactions.digest_target(changed, kind="entry")
    assert transactions.digest_target(referent, kind="entry") == digest_path(referent)


def test_entry_target_removal_unlinks_only_the_owned_entry(
    tmp_path: Path,
):
    home = tmp_path / "home"
    referent = _write(tmp_path / "store" / "payload", "referent")
    live = _symlink(tmp_path / "published" / "skill", "../store", directory=True)
    engine = TransactionEngine(home)

    with ManagerHomeLock(home) as lock:
        engine.prepare(
            lock,
            _plan(
                "txn-entry-removal",
                tmp_path / "project",
                _entry_target("80-removal", "skill", live, None),
            ),
        )
        engine.commit(lock, "txn-entry-removal")

    assert not live.exists()
    assert not live.is_symlink()
    assert referent.read_text(encoding="utf-8") == "referent"
    assert not any(live.parent.glob(".csk-txn-*"))


def test_entry_target_rollback_restores_the_exact_link(
    tmp_path: Path,
):
    home = tmp_path / "home"
    old_referent = _write(tmp_path / "old-store" / "payload", "old referent")
    new_referent = _write(tmp_path / "new-store" / "payload", "new referent")
    live = _symlink(tmp_path / "published" / "skill", "../old-store", directory=True)
    desired = _symlink(tmp_path / "staged" / "skill", "../new-store", directory=True)

    def fail_after_commit(point: str, target: JournalTarget | None) -> None:
        del target
        if point == "target_committed":
            raise RuntimeError("force entry rollback")

    engine = TransactionEngine(home, fault_hook=fail_after_commit)
    with ManagerHomeLock(home) as lock:
        engine.prepare(
            lock,
            _plan(
                "txn-entry-rollback",
                tmp_path / "project",
                _entry_target("60-adapter", "skill", live, desired),
            ),
        )
        with pytest.raises(RuntimeError, match="force entry rollback"):
            engine.commit(lock, "txn-entry-rollback")

    assert live.is_symlink()
    assert os.readlink(live) == "../old-store"
    assert old_referent.read_text(encoding="utf-8") == "old referent"
    assert new_referent.read_text(encoding="utf-8") == "new referent"
    assert not any(live.parent.glob(".csk-txn-*"))
    assert not (engine.journal_root / "txn-entry-rollback.json").exists()


def test_entry_target_recovery_finishes_an_interrupted_link_commit(
    tmp_path: Path,
):
    home = tmp_path / "home"
    old_referent = _write(tmp_path / "old-store" / "payload", "old referent")
    new_referent = _write(tmp_path / "new-store" / "payload", "new referent")
    live = _symlink(tmp_path / "published" / "skill", "../old-store", directory=True)
    desired = _symlink(tmp_path / "staged" / "skill", "../new-store", directory=True)

    def crash_after_install(point: str, target: JournalTarget | None) -> None:
        del target
        if point == "after_install":
            raise SimulatedCrash(point)

    engine = TransactionEngine(home, fault_hook=crash_after_install)
    with ManagerHomeLock(home) as lock:
        engine.prepare(
            lock,
            _plan(
                "txn-entry-recovery",
                tmp_path / "project",
                _entry_target("60-adapter", "skill", live, desired),
            ),
        )
        with pytest.raises(SimulatedCrash, match="after_install"):
            engine.commit(lock, "txn-entry-recovery")

    with ManagerHomeLock(home) as lock:
        TransactionEngine(home).recover(lock)

    assert live.is_symlink()
    assert os.readlink(live) == "../new-store"
    assert old_referent.read_text(encoding="utf-8") == "old referent"
    assert new_referent.read_text(encoding="utf-8") == "new referent"
    assert not any(live.parent.glob(".csk-txn-*"))


def test_entry_target_stale_preimage_preserves_a_repointed_link_and_referents(
    tmp_path: Path,
):
    home = tmp_path / "home"
    old_referent = _write(tmp_path / "old-store" / "payload", "old referent")
    new_referent = _write(tmp_path / "new-store" / "payload", "new referent")
    foreign_referent = _write(
        tmp_path / "foreign-store" / "payload", "foreign referent"
    )
    live = _symlink(tmp_path / "published" / "skill", "../old-store", directory=True)
    desired = _symlink(tmp_path / "staged" / "skill", "../new-store", directory=True)
    target = _entry_target("60-adapter", "skill", live, desired)
    engine = TransactionEngine(home)

    with ManagerHomeLock(home) as lock:
        engine.prepare(
            lock,
            _plan("txn-entry-stale", tmp_path / "project", target),
        )
        live.unlink()
        _symlink(live, "../foreign-store", directory=True)
        with pytest.raises(TransactionCorruptionError, match="stale preimage"):
            engine.commit(lock, "txn-entry-stale")

    assert live.is_symlink()
    assert os.readlink(live) == "../foreign-store"
    assert old_referent.read_text(encoding="utf-8") == "old referent"
    assert new_referent.read_text(encoding="utf-8") == "new referent"
    assert foreign_referent.read_text(encoding="utf-8") == "foreign referent"
    assert not any(live.parent.glob(".csk-txn-*"))


def test_entry_target_and_its_referent_are_independent_transaction_namespaces(
    tmp_path: Path,
):
    home = tmp_path / "home"
    live_root = tmp_path / "published"
    canonical = _write(live_root / "canonical" / "payload", "old canonical").parent
    mirror = _symlink(live_root / "mirror", "canonical", directory=True)
    desired_canonical = _write(
        tmp_path / "staged" / "canonical" / "payload", "new canonical"
    ).parent
    desired_mirror = _symlink(
        tmp_path / "staged" / "mirror", "canonical", directory=True
    )
    engine = TransactionEngine(home)

    with ManagerHomeLock(home) as lock:
        engine.prepare(
            lock,
            _plan(
                "txn-entry-alias",
                tmp_path / "project",
                _target(
                    "20-runtime",
                    "canonical",
                    canonical,
                    desired_canonical,
                ),
                _entry_target("60-adapter", "mirror", mirror, desired_mirror),
            ),
        )
        engine.commit(lock, "txn-entry-alias")

    assert mirror.is_symlink()
    assert os.readlink(mirror) == "canonical"
    assert (canonical / "payload").read_text(encoding="utf-8") == "new canonical"
    assert not any(live_root.glob(".csk-txn-*"))


@pytest.mark.parametrize("kind", ["file", "directory"])
@pytest.mark.parametrize("content", ["exact", "different"])
def test_recovery_preserves_cleanup_tomb_reappearing_after_removed_state(
    tmp_path: Path,
    kind: str,
    content: str,
):
    home = tmp_path / "home"
    live = tmp_path / "live"
    desired = tmp_path / "desired"
    if kind == "file":
        _write(live, "old")
        _write(desired, "new")
    else:
        _write(live / "payload", "old")
        _write(desired / "payload", "new")

    def crash_after_journal_tomb(point: str, target: JournalTarget | None) -> None:
        del target
        if point == "journal_tombed":
            raise SimulatedCrash(point)

    engine = TransactionEngine(home, fault_hook=crash_after_journal_tomb)
    with ManagerHomeLock(home) as lock:
        engine.prepare(
            lock,
            _plan(
                f"txn-removed-reappeared-{kind}-{content}",
                tmp_path / "project",
                _target("10-context", "skill", live, desired),
            ),
        )
        with pytest.raises(SimulatedCrash, match="journal_tombed"):
            engine.commit(lock, f"txn-removed-reappeared-{kind}-{content}")

    journal_tomb = (
        engine.journal_root / f"txn-removed-reappeared-{kind}-{content}.json.delete"
    )
    record = json.loads(journal_tomb.read_text(encoding="utf-8"))
    backup_cleanup = next(
        cleanup for cleanup in record["cleanup_sidecars"] if cleanup["role"] == "backup"
    )
    assert backup_cleanup["state"] == "removed"
    reappeared = Path(backup_cleanup["tomb_path"])
    value = "old" if content == "exact" else "foreign"
    if kind == "file":
        _write(reappeared, value)
    else:
        _write(reappeared / "payload", value)
    before = digest_path(reappeared)
    if content == "exact":
        assert before == backup_cleanup["expected_digest"]
    else:
        assert before != backup_cleanup["expected_digest"]

    with (
        ManagerHomeLock(home) as lock,
        pytest.raises(TransactionError, match="reappeared"),
    ):
        TransactionEngine(home).recover(lock)

    assert digest_path(reappeared) == before
    assert journal_tomb.exists()
    if kind == "file":
        assert live.read_text(encoding="utf-8") == "new"
    else:
        assert (live / "payload").read_text(encoding="utf-8") == "new"
