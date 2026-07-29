from __future__ import annotations

import threading
from pathlib import Path

import pytest

from csk.locking import (
    BuildLock,
    GlobalLock,
    LockError,
    LockOrderError,
    ManagerHomeLock,
    ProjectLock,
    ProjectLocks,
    canonical_project_identity,
    unsigned_utf8_key,
)


def test_global_lock_times_out_when_held(tmp_path):
    home = tmp_path / "home"
    with (
        GlobalLock(home, timeout=0.1),
        pytest.raises(LockError),
        GlobalLock(home, timeout=0.1),
    ):
        pass


def test_stale_lock_from_dead_process_is_broken(tmp_path):
    import json
    import subprocess
    import sys

    proc = subprocess.run(
        [sys.executable, "-c", "import os; print(os.getpid())"],
        capture_output=True,
        text=True,
        check=False,
    )
    dead_pid = int(proc.stdout.strip())
    lock_path = tmp_path / ".lock"
    lock_path.write_text(
        json.dumps({"pid": dead_pid, "created_at": 0}), encoding="utf-8"
    )

    with GlobalLock(tmp_path, timeout=0.5) as lock:
        assert lock.acquired
    assert not lock_path.exists()


def test_lock_held_by_live_process_still_times_out(tmp_path):
    import json
    import os

    import pytest

    lock_path = tmp_path / ".lock"
    lock_path.write_text(
        json.dumps({"pid": os.getpid(), "created_at": 0}), encoding="utf-8"
    )

    with pytest.raises(LockError), GlobalLock(tmp_path, timeout=0.3):
        pass
    assert lock_path.exists()


def test_project_locks_are_canonicalized_deduplicated_and_byte_sorted(tmp_path: Path):
    home = tmp_path / "home"
    projects = [tmp_path / "é", tmp_path / "a", tmp_path / "中", tmp_path / "." / "a"]
    locks = ProjectLocks(home, projects)

    expected = sorted(
        {canonical_project_identity(project) for project in projects},
        key=unsigned_utf8_key,
    )
    assert [lock.identity for lock in locks.locks] == expected
    with locks:
        assert all(lock.acquired for lock in locks.locks)
    assert all(not lock.acquired for lock in locks.locks)


def test_direct_project_lock_rejects_noncanonical_acquisition_order(tmp_path: Path):
    home = tmp_path / "home"
    identities = sorted(
        (tmp_path / "a", tmp_path / "z"),
        key=lambda path: unsigned_utf8_key(canonical_project_identity(path)),
    )

    with (
        ProjectLock(home, identities[1]),
        pytest.raises(LockOrderError, match="unsigned UTF-8"),
        ProjectLock(home, identities[0]),
    ):
        pass


def test_build_lock_must_be_released_before_home_lock(tmp_path: Path):
    home = tmp_path / "home"
    with (
        ProjectLock(home, tmp_path / "project"),
        BuildLock(home, "build-key"),
        pytest.raises(LockOrderError, match="released before"),
        ManagerHomeLock(home),
    ):
        pass
    with ProjectLock(home, tmp_path / "project"), ManagerHomeLock(home) as lock:
        lock.assert_held()


def test_project_and_build_locks_are_forbidden_under_home_lock(tmp_path: Path):
    home = tmp_path / "home"
    with ManagerHomeLock(home):
        with (
            pytest.raises(LockOrderError, match="project lock"),
            ProjectLock(home, tmp_path / "project"),
        ):
            pass
        with (
            pytest.raises(LockOrderError, match="cache-build lock"),
            BuildLock(home, "key"),
        ):
            pass


def test_project_lock_is_forbidden_under_home_lock_held_by_another_thread(
    tmp_path: Path,
):
    home = tmp_path / "home"
    started = threading.Event()
    release = threading.Event()
    errors: list[Exception] = []

    def hold_home() -> None:
        try:
            with ManagerHomeLock(home):
                started.set()
                release.wait(timeout=2)
        except Exception as exc:  # noqa: BLE001 - surface worker failures in the parent test
            errors.append(exc)

    thread = threading.Thread(target=hold_home)
    thread.start()
    assert started.wait(timeout=2)
    try:
        with (
            pytest.raises(LockOrderError, match="project lock"),
            ProjectLock(home, tmp_path / "project"),
        ):
            pass
    finally:
        release.set()
        thread.join(timeout=2)
    assert not errors
    assert not thread.is_alive()


def test_build_lock_requires_project_and_only_one_key(tmp_path: Path):
    home = tmp_path / "home"
    with pytest.raises(LockOrderError, match="requires"), BuildLock(home, "first"):
        pass
    with (
        ProjectLock(home, tmp_path / "project"),
        BuildLock(home, "first"),
        pytest.raises(LockOrderError, match="at most one"),
        BuildLock(home, "second"),
    ):
        pass


def test_corrupt_lock_is_not_broken(tmp_path):
    import pytest

    lock_path = tmp_path / ".lock"
    lock_path.write_text("not json", encoding="utf-8")

    with pytest.raises(LockError), GlobalLock(tmp_path, timeout=0.3):
        pass
    assert lock_path.exists()
