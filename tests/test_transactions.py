from __future__ import annotations

import json
import shutil
import threading
from pathlib import Path

import pytest

from csk import transactions
from csk.locking import ManagerHomeLock, ProjectLock
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


def _plan(
    transaction_id: str, project: Path, *targets: MutableTarget
) -> TransactionPlan:
    return TransactionPlan(
        transaction_id=transaction_id,
        project_identity=str(project.resolve()),
        targets=tuple(targets),
        generation_digests={"runtime/default": "sha256:" + "a" * 64},
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
    journal.phase = "prepared"
    engine._save_journal(journal)
    engine._remove_journal(journal)

    assert [flags for _, _, flags in moves] == [
        transactions._MOVEFILE_WRITE_THROUGH,
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
