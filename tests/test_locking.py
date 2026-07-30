from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
import time
import unicodedata
from pathlib import Path

import pytest

from csk import locking
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

_LOCK_HOLDER = """
import time
import sys
from pathlib import Path

from csk.locking import ManagerHomeLock

home = Path(sys.argv[1])
ready = Path(sys.argv[2])
release = Path(sys.argv[3])
with ManagerHomeLock(home, timeout=5):
    ready.write_text("ready", encoding="utf-8")
    while not release.exists():
        time.sleep(0.01)
"""

_LOCK_CONTENDER = """
import json
import time
import sys
from pathlib import Path

from csk.locking import ManagerHomeLock

home = Path(sys.argv[1])
barrier = Path(sys.argv[2])
result = Path(sys.argv[3])
while not barrier.exists():
    time.sleep(0.01)
with ManagerHomeLock(home, timeout=5) as lock:
    started = time.monotonic_ns()
    time.sleep(0.2)
    lock.assert_held()
    ended = time.monotonic_ns()
    result.write_text(
        json.dumps({"started": started, "ended": ended}),
        encoding="utf-8",
)
"""

_LEGACY_STALE_BREAKER = """
import json
import os
import sys
import time
from pathlib import Path

lock_path = Path(sys.argv[1])
ready = Path(sys.argv[2])
move = Path(sys.argv[3])
moved = Path(sys.argv[4])
restore = Path(sys.argv[5])
original = json.loads(lock_path.read_text(encoding="utf-8"))
ready.write_text("ready", encoding="utf-8")
while not move.exists():
    time.sleep(0.01)
stale = lock_path.with_name(f".lock.stale-{os.getpid()}")
lock_path.rename(stale)
moved.write_text("moved", encoding="utf-8")
while not restore.exists():
    time.sleep(0.01)
current = json.loads(stale.read_text(encoding="utf-8"))
if current != original:
    try:
        stale.rename(lock_path)
    except OSError:
        pass
else:
    try:
        stale.unlink()
    except OSError:
        pass
"""


def _lock_subprocess_env() -> dict[str, str]:
    env = os.environ.copy()
    source_root = str(Path(__file__).resolve().parents[1] / "src")
    previous = env.get("PYTHONPATH")
    env["PYTHONPATH"] = (
        source_root if not previous else source_root + os.pathsep + previous
    )
    return env


def _wait_for_subprocess_path(
    path: Path,
    process: subprocess.Popen[str],
    *,
    timeout: float = 5,
) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if path.exists():
            return
        if process.poll() is not None:
            stdout, stderr = process.communicate()
            pytest.fail(
                f"lock subprocess exited before {path.name}: "
                f"code={process.returncode} stdout={stdout!r} stderr={stderr!r}"
            )
        time.sleep(0.01)
    pytest.fail(f"lock subprocess did not create {path} within {timeout} seconds")


def _terminate_subprocess(process: subprocess.Popen[str]) -> None:
    process.terminate()
    try:
        process.wait(timeout=5)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait(timeout=5)


def test_global_lock_times_out_when_held(tmp_path):
    home = tmp_path / "home"
    with (
        GlobalLock(home, timeout=0.1),
        pytest.raises(LockError),
        GlobalLock(home, timeout=0.1),
    ):
        pass


def test_stable_lock_timeout_message_forbids_removal(tmp_path: Path):
    with GlobalLock(tmp_path, timeout=0.1) as lock:
        message = locking._timeout_message(lock.path)

    assert "persistent v1 lock file must not be removed" in message


def test_legacy_dead_lock_record_requires_offline_migration(tmp_path: Path):
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
    original = lock_path.read_bytes()

    with (
        pytest.raises(LockError, match="legacy.*offline"),
        GlobalLock(tmp_path, timeout=0.5),
    ):
        pass
    assert lock_path.read_bytes() == original

    lock_path.unlink()
    with GlobalLock(tmp_path, timeout=0.5) as lock:
        assert lock.acquired
    record = json.loads(lock_path.read_text(encoding="utf-8"))
    assert record["protocol"] == locking._LOCK_PROTOCOL
    assert lock_path.exists()


def test_unguarded_v1_record_requires_offline_migration(tmp_path: Path):
    lock_path = tmp_path / ".lock"
    lock_path.write_text(
        json.dumps(
            {
                "protocol": locking._LOCK_PROTOCOL,
                "pid": 123,
                "created_at": 0,
                "token": "unguarded",
            }
        ),
        encoding="utf-8",
    )
    original = lock_path.read_bytes()

    with (
        pytest.raises(LockError, match="unguarded.*offline"),
        GlobalLock(tmp_path, timeout=0.5),
    ):
        pass

    assert lock_path.read_bytes() == original


def test_legacy_stale_breaker_cannot_split_current_stable_owners(tmp_path: Path):
    home = tmp_path / "home"
    home.mkdir()
    proc = subprocess.run(
        [sys.executable, "-c", "import os; print(os.getpid())"],
        capture_output=True,
        text=True,
        check=False,
    )
    dead_pid = int(proc.stdout.strip())
    lock_path = home / ".lock"
    lock_path.write_text(
        json.dumps({"pid": dead_pid, "created_at": 0}), encoding="utf-8"
    )

    ready = tmp_path / "legacy-ready"
    move = tmp_path / "legacy-move"
    moved = tmp_path / "legacy-moved"
    restore = tmp_path / "legacy-restore"
    legacy_breaker = subprocess.Popen(
        [
            sys.executable,
            "-c",
            _LEGACY_STALE_BREAKER,
            str(lock_path),
            str(ready),
            str(move),
            str(moved),
            str(restore),
        ],
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env=_lock_subprocess_env(),
    )
    first_settled = threading.Event()
    release_first = threading.Event()
    entered: list[str] = []
    rejected: list[str] = []
    errors: list[Exception] = []

    def first_owner() -> None:
        try:
            with ManagerHomeLock(home, timeout=0.3):
                entered.append("first")
                first_settled.set()
                if not release_first.wait(timeout=3):
                    raise TimeoutError("test did not release first current owner")
        except LockError:
            rejected.append("first")
            first_settled.set()
        except Exception as exc:  # noqa: BLE001 - surface worker failures
            errors.append(exc)
            first_settled.set()

    first_thread = threading.Thread(target=first_owner)
    try:
        _wait_for_subprocess_path(ready, legacy_breaker)
        first_thread.start()
        assert first_settled.wait(timeout=2)

        move.write_text("move", encoding="utf-8")
        _wait_for_subprocess_path(moved, legacy_breaker)

        try:
            with ManagerHomeLock(home, timeout=0.3):
                entered.append("second")
        except LockError:
            rejected.append("second")

        assert not errors
        assert entered == []
        assert rejected == ["first", "second"]
    finally:
        release_first.set()
        first_thread.join(timeout=3)
        restore.write_text("restore", encoding="utf-8")
        stdout, stderr = legacy_breaker.communicate(timeout=5)
        assert legacy_breaker.returncode == 0, (stdout, stderr)

    assert not first_thread.is_alive()
    with ManagerHomeLock(home, timeout=1) as migrated:
        migrated.assert_held()


def test_released_lock_file_remains_and_can_be_reacquired(tmp_path: Path):
    lock_path = tmp_path / ".lock"

    with GlobalLock(tmp_path, timeout=0.5) as first:
        first.assert_held()
        first_record = json.loads(lock_path.read_text(encoding="utf-8"))
        first_token = first_record["token"]
        assert first_record["pid"] == locking._LEGACY_PID_GUARD
        assert first_record["owner_pid"] == os.getpid()

    assert lock_path.exists()
    with GlobalLock(tmp_path, timeout=0.5) as second:
        second.assert_held()
        second_token = json.loads(lock_path.read_text(encoding="utf-8"))["token"]

    assert first_token != second_token
    assert lock_path.exists()


def test_acquire_rechecks_canonical_witness_after_publication(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    home = tmp_path / "home"
    lock = ManagerHomeLock(home)
    moved = tmp_path / "published-but-moved.lock"
    real_write = locking._write_lock_fd

    def move_after_write(fd: int, data: dict[str, object]) -> None:
        real_write(fd, data)
        lock.path.rename(moved)
        lock.path.write_text("competing bytes", encoding="utf-8")

    monkeypatch.setattr(locking, "_write_lock_fd", move_after_write)

    with pytest.raises(LockError, match="changed after publication"), lock:
        pass

    assert not lock.acquired
    assert moved.exists()
    assert lock.path.read_text(encoding="utf-8") == "competing bytes"
    with locking._PROCESS_STATE_GUARD:
        assert lock.home_identity not in locking._PROCESS_HOMES


def test_crashed_owner_and_barrier_contenders_share_one_stable_lock(
    tmp_path: Path,
):
    home = tmp_path / "home"
    stale_ready = tmp_path / "stale-ready"
    never_release = tmp_path / "never-release"
    environment = _lock_subprocess_env()
    stale_owner = subprocess.Popen(
        [
            sys.executable,
            "-c",
            _LOCK_HOLDER,
            str(home),
            str(stale_ready),
            str(never_release),
        ],
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env=environment,
    )
    try:
        _wait_for_subprocess_path(stale_ready, stale_owner)
        with pytest.raises(LockError), ManagerHomeLock(home, timeout=0.15):
            pass
    finally:
        _terminate_subprocess(stale_owner)

    with ManagerHomeLock(home, timeout=2) as recovered:
        recovered.assert_held()

    barrier = tmp_path / "contender-barrier"
    results = [tmp_path / "contender-a.json", tmp_path / "contender-b.json"]
    contenders = [
        subprocess.Popen(
            [
                sys.executable,
                "-c",
                _LOCK_CONTENDER,
                str(home),
                str(barrier),
                str(result),
            ],
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=environment,
        )
        for result in results
    ]
    barrier.write_text("go", encoding="utf-8")
    for contender in contenders:
        stdout, stderr = contender.communicate(timeout=10)
        assert contender.returncode == 0, (stdout, stderr)

    intervals = sorted(
        (json.loads(result.read_text(encoding="utf-8")) for result in results),
        key=lambda interval: interval["started"],
    )
    assert intervals[1]["started"] >= intervals[0]["ended"]

    lock_path = home / ".lock"
    record = json.loads(lock_path.read_text(encoding="utf-8"))
    assert record["protocol"] == locking._LOCK_PROTOCOL
    with ManagerHomeLock(home, timeout=2) as final_owner:
        final_owner.assert_held()


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


def test_home_lock_is_forbidden_while_build_lock_is_held_by_another_thread(
    tmp_path: Path,
):
    home = tmp_path / "home"
    started = threading.Event()
    release = threading.Event()
    errors: list[Exception] = []

    def hold_build() -> None:
        try:
            with (
                ProjectLock(home, tmp_path / "project"),
                BuildLock(home, "build-key"),
            ):
                started.set()
                release.wait(timeout=2)
        except Exception as exc:  # noqa: BLE001 - surface worker failures in the parent test
            errors.append(exc)

    thread = threading.Thread(target=hold_build)
    thread.start()
    assert started.wait(timeout=2)
    try:
        with (
            pytest.raises(LockOrderError, match="released before"),
            ManagerHomeLock(home),
        ):
            pass
    finally:
        release.set()
        thread.join(timeout=2)
    assert not errors
    assert not thread.is_alive()


def test_process_lock_order_uses_canonical_manager_home_identity(
    tmp_path: Path,
):
    home = tmp_path / "home"
    home.mkdir()
    alias = tmp_path / "home-alias"
    try:
        alias.symlink_to(home, target_is_directory=True)
    except OSError as exc:
        pytest.skip(f"symlinks unavailable: {exc}")
    started = threading.Event()
    release = threading.Event()
    errors: list[Exception] = []

    def hold_build_through_alias() -> None:
        try:
            with (
                ProjectLock(alias, tmp_path / "project"),
                BuildLock(alias, "build-key"),
            ):
                started.set()
                release.wait(timeout=2)
        except Exception as exc:  # noqa: BLE001 - surface worker failures in the parent test
            errors.append(exc)

    thread = threading.Thread(target=hold_build_through_alias)
    thread.start()
    assert started.wait(timeout=2)
    try:
        with (
            pytest.raises(LockOrderError, match="released before"),
            ManagerHomeLock(home),
        ):
            pass
    finally:
        release.set()
        thread.join(timeout=2)
    assert not errors
    assert not thread.is_alive()


@pytest.mark.skipif(sys.platform != "darwin", reason="requires macOS path lookup")
def test_case_aliases_share_project_and_manager_home_lock_identity(
    tmp_path: Path,
):
    home = tmp_path / "ManagerHome"
    project = tmp_path / "Project"
    home.mkdir()
    project.mkdir()
    home_alias = tmp_path / "managerhome"
    project_alias = tmp_path / "project"
    if not (
        home_alias.exists()
        and project_alias.exists()
        and os.path.samefile(home, home_alias)
        and os.path.samefile(project, project_alias)
    ):
        pytest.skip("test filesystem is case-sensitive")

    assert canonical_project_identity(project_alias) == canonical_project_identity(
        project
    )
    assert (
        ManagerHomeLock(home_alias).home_identity == ManagerHomeLock(home).home_identity
    )

    contender_result: list[str] = []

    def acquire_aliased_project() -> None:
        try:
            with ProjectLock(home_alias, project_alias, timeout=0.1):
                contender_result.append("acquired")
        except LockError:
            contender_result.append("blocked")

    with ProjectLock(home, project):
        contender = threading.Thread(target=acquire_aliased_project)
        contender.start()
        contender.join(timeout=2)
        assert not contender.is_alive()
        assert contender_result == ["blocked"]

    build_started = threading.Event()
    release_build = threading.Event()
    errors: list[Exception] = []

    def hold_build_through_aliases() -> None:
        try:
            with (
                ProjectLock(home_alias, project_alias),
                BuildLock(home_alias, "build-key"),
            ):
                build_started.set()
                release_build.wait(timeout=2)
        except Exception as exc:  # noqa: BLE001 - surface worker failure
            errors.append(exc)

    holder = threading.Thread(target=hold_build_through_aliases)
    holder.start()
    assert build_started.wait(timeout=2)
    try:
        with (
            pytest.raises(LockOrderError, match="released before"),
            ManagerHomeLock(home),
        ):
            pass
    finally:
        release_build.set()
        holder.join(timeout=2)
    assert not holder.is_alive()
    assert not errors


@pytest.mark.skipif(sys.platform != "darwin", reason="requires macOS path lookup")
def test_unicode_aliases_share_project_lock_identity(tmp_path: Path):
    composed_name = "caf\u00e9"
    decomposed_name = unicodedata.normalize("NFD", composed_name)
    if composed_name == decomposed_name:
        pytest.skip("test names do not differ after normalization")
    project = tmp_path / composed_name
    project.mkdir()
    alias = tmp_path / decomposed_name
    if not alias.exists() or not os.path.samefile(project, alias):
        pytest.skip("test filesystem distinguishes canonical Unicode spellings")

    assert canonical_project_identity(alias) == canonical_project_identity(project)

    contender_result: list[str] = []

    def acquire_alias() -> None:
        try:
            with ProjectLock(tmp_path / "home", alias, timeout=0.1):
                contender_result.append("acquired")
        except LockError:
            contender_result.append("blocked")

    with ProjectLock(tmp_path / "home", project):
        contender = threading.Thread(target=acquire_alias)
        contender.start()
        contender.join(timeout=2)
        assert not contender.is_alive()
        assert contender_result == ["blocked"]


def test_windows_identity_components_respect_parent_case_sensitivity():
    case_sensitive = {
        "C:\\": False,
        "C:\\Root": False,
        "C:\\Root\\Sensitive": True,
    }

    def lookup(path: str) -> bool:
        return case_sensitive[path]

    upper = locking._canonicalize_windows_identity(
        "C:\\Root\\Sensitive",
        ["Project"],
        case_sensitive=lookup,
    )
    lower = locking._canonicalize_windows_identity(
        "C:\\Root\\Sensitive",
        ["project"],
        case_sensitive=lookup,
    )
    assert upper != lower

    existing_upper = locking._canonicalize_windows_identity(
        "C:\\Root\\Sensitive\\Project",
        [],
        case_sensitive=lookup,
    )
    existing_lower = locking._canonicalize_windows_identity(
        "C:\\Root\\Sensitive\\project",
        [],
        case_sensitive=lookup,
    )
    assert existing_upper != existing_lower

    insensitive_upper = locking._canonicalize_windows_identity(
        "C:\\Root",
        ["Project"],
        case_sensitive=lookup,
    )
    insensitive_lower = locking._canonicalize_windows_identity(
        "C:\\Root",
        ["project"],
        case_sensitive=lookup,
    )
    assert insensitive_upper == insensitive_lower


def test_windows_nested_missing_identity_is_recanonicalized_after_creation():
    case_sensitive = {
        "C:\\": False,
        "C:\\Sensitive": True,
        "C:\\Sensitive\\Parent": True,
    }

    def lookup(path: str) -> bool:
        return case_sensitive[path]

    provisional = locking._canonicalize_windows_identity(
        "C:\\Sensitive",
        ["Child", "Parent"],
        case_sensitive=lookup,
    )
    physical = locking._canonicalize_windows_identity(
        "C:\\Sensitive\\Parent\\Child",
        [],
        case_sensitive=lookup,
    )

    assert provisional == "C:\\SENSITIVE\\Parent\\CHILD"
    assert physical == "C:\\SENSITIVE\\Parent\\Child"
    assert provisional != physical


@pytest.mark.parametrize("lock_class", ["home", "project", "build"])
def test_each_lock_class_recanonicalizes_the_home_before_process_reservation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    lock_class: str,
):
    home = tmp_path / lock_class / "Parent" / "Child"
    provisional = str(tmp_path / f"{lock_class}-provisional")
    physical = str(home)

    def canonicalize(candidate: Path) -> str:
        assert Path(os.path.abspath(candidate.expanduser())) == home
        return physical if home.exists() else provisional

    monkeypatch.setattr(locking, "_canonical_manager_home", canonicalize)
    project = ProjectLock(home, tmp_path / f"{lock_class}-project")
    build = BuildLock(home, "build-key")
    home_lock = ManagerHomeLock(home)
    selected = {"home": home_lock, "project": project, "build": build}[lock_class]
    assert selected.home_identity == provisional

    if lock_class == "home":
        with home_lock:
            assert home_lock.home_identity == physical
            assert home_lock.path == home / ".lock"
            with pytest.raises(AttributeError):
                home_lock.home_identity = provisional  # type: ignore[misc]
            with locking._PROCESS_STATE_GUARD:
                assert home_lock in locking._PROCESS_HOMES[physical].home_held
                assert provisional not in locking._PROCESS_HOMES
    elif lock_class == "project":
        with project:
            assert project.home_identity == physical
            assert project.path.parent == home / "locks" / "projects"
            with locking._PROCESS_STATE_GUARD:
                assert project in locking._PROCESS_HOMES[physical].project_held
                assert provisional not in locking._PROCESS_HOMES
    else:
        with project, build:
            assert project.home_identity == physical
            assert build.home_identity == physical
            assert build.path.parent == home / "locks" / "builds"
            with locking._PROCESS_STATE_GUARD:
                assert build in locking._PROCESS_HOMES[physical].build_held
                assert provisional not in locking._PROCESS_HOMES

    with locking._PROCESS_STATE_GUARD:
        assert physical not in locking._PROCESS_HOMES
        assert provisional not in locking._PROCESS_HOMES


@pytest.mark.skipif(os.name != "nt", reason="requires Windows case-sensitive dirs")
@pytest.mark.parametrize("lock_class", ["home", "project", "build"])
def test_windows_case_sensitive_first_use_keeps_distinct_lock_classes_concurrent(
    tmp_path: Path,
    lock_class: str,
):
    sensitive = tmp_path / "Sensitive"
    sensitive.mkdir()
    enabled = subprocess.run(
        ["fsutil", "file", "setCaseSensitiveInfo", str(sensitive), "enable"],
        capture_output=True,
        text=True,
        check=False,
    )
    if enabled.returncode != 0:
        pytest.skip(f"cannot enable per-directory case sensitivity: {enabled.stderr}")
    inherited_probe = sensitive / "Inherited"
    inherited_probe.mkdir()
    if not locking._windows_directory_case_sensitive(str(inherited_probe)):
        pytest.skip("new Windows directories do not inherit case sensitivity")
    inherited_probe.rmdir()

    upper_home = sensitive / "Parent" / "Child"
    lower_home = sensitive / "Parent" / "child"
    upper_project = tmp_path / "project-upper"
    lower_project = tmp_path / "project-lower"
    upper_project.mkdir()
    lower_project.mkdir()

    if lock_class == "home":
        upper_locks = (ManagerHomeLock(upper_home, timeout=2),)
        lower_locks = (ManagerHomeLock(lower_home, timeout=2),)
    elif lock_class == "project":
        upper_locks = (ProjectLock(upper_home, upper_project, timeout=2),)
        lower_locks = (ProjectLock(lower_home, lower_project, timeout=2),)
    else:
        upper_locks = (
            ProjectLock(upper_home, upper_project, timeout=2),
            BuildLock(upper_home, "key", timeout=2),
        )
        lower_locks = (
            ProjectLock(lower_home, lower_project, timeout=2),
            BuildLock(lower_home, "key", timeout=2),
        )

    entered = [threading.Event(), threading.Event()]
    release = threading.Event()
    identities: list[str] = []
    errors: list[Exception] = []

    def hold(index: int, locks: tuple[locking._ExclusiveFileLock, ...]) -> None:
        acquired: list[locking._ExclusiveFileLock] = []
        try:
            for current in locks:
                current.__enter__()
                acquired.append(current)
            identities.append(locks[-1].home_identity)
            entered[index].set()
            if not release.wait(timeout=3):
                raise TimeoutError("test did not release Windows lock holders")
        except Exception as exc:  # noqa: BLE001 - surface worker failures
            errors.append(exc)
            entered[index].set()
        finally:
            for current in reversed(acquired):
                current.__exit__(None, None, None)

    threads = [
        threading.Thread(target=hold, args=(0, upper_locks)),
        threading.Thread(target=hold, args=(1, lower_locks)),
    ]
    for thread in threads:
        thread.start()
    try:
        assert entered[0].wait(timeout=3)
        assert entered[1].wait(timeout=3)
        assert not errors
        assert len(set(identities)) == 2
    finally:
        release.set()
        for thread in threads:
            thread.join(timeout=3)
            assert not thread.is_alive()


def test_build_lock_is_forbidden_under_home_lock_held_by_another_thread(
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

    with ProjectLock(home, tmp_path / "project"):
        thread = threading.Thread(target=hold_home)
        thread.start()
        assert started.wait(timeout=2)
        try:
            with (
                pytest.raises(LockOrderError, match="manager-home lock"),
                BuildLock(home, "build-key"),
            ):
                pass
        finally:
            release.set()
            thread.join(timeout=2)
    assert not errors
    assert not thread.is_alive()


@pytest.mark.parametrize("pending_class", ["project", "build"])
def test_home_lock_is_forbidden_while_outer_lock_acquisition_is_pending(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    pending_class: str,
):
    home = tmp_path / "home"
    project = ProjectLock(home, tmp_path / "project", timeout=2)
    build = BuildLock(home, "build-key", timeout=2)
    pending = project if pending_class == "project" else build
    entered_open = threading.Event()
    release_open = threading.Event()
    errors: list[Exception] = []
    real_open = os.open

    def gated_open(
        path: str | bytes | os.PathLike[str] | os.PathLike[bytes],
        flags: int,
        mode: int = 0o777,
    ) -> int:
        if Path(path) == pending.path:
            entered_open.set()
            if not release_open.wait(timeout=2):
                raise TimeoutError("test did not release pending filesystem acquire")
        return real_open(path, flags, mode)

    monkeypatch.setattr(locking.os, "open", gated_open)

    def acquire_outer() -> None:
        try:
            if pending_class == "project":
                with project:
                    pass
            else:
                with project, build:
                    pass
        except Exception as exc:  # noqa: BLE001 - surface worker failures in the parent test
            errors.append(exc)

    thread = threading.Thread(target=acquire_outer)
    thread.start()
    assert entered_open.wait(timeout=2)
    try:
        with (
            pytest.raises(LockOrderError, match="pending|released before"),
            ManagerHomeLock(home),
        ):
            pass
    finally:
        release_open.set()
        thread.join(timeout=2)
    assert not errors
    assert not thread.is_alive()


def test_pending_home_lock_blocks_project_and_build_acquisition(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    home = tmp_path / "home"
    held_project = ProjectLock(home, tmp_path / "held-project")
    pending_home = ManagerHomeLock(home, timeout=2)
    entered_open = threading.Event()
    release_open = threading.Event()
    errors: list[Exception] = []
    real_open = os.open

    def gated_open(
        path: str | bytes | os.PathLike[str] | os.PathLike[bytes],
        flags: int,
        mode: int = 0o777,
    ) -> int:
        if Path(path) == pending_home.path:
            entered_open.set()
            if not release_open.wait(timeout=2):
                raise TimeoutError("test did not release pending filesystem acquire")
        return real_open(path, flags, mode)

    monkeypatch.setattr(locking.os, "open", gated_open)

    def acquire_home() -> None:
        try:
            with pending_home:
                pass
        except Exception as exc:  # noqa: BLE001 - surface worker failures in the parent test
            errors.append(exc)

    with held_project:
        thread = threading.Thread(target=acquire_home)
        thread.start()
        assert entered_open.wait(timeout=2)
        try:
            with (
                pytest.raises(LockOrderError, match="manager-home lock"),
                ProjectLock(home, tmp_path / "other-project"),
            ):
                pass
            with (
                pytest.raises(LockOrderError, match="manager-home lock"),
                BuildLock(home, "build-key"),
            ):
                pass
        finally:
            release_open.set()
            thread.join(timeout=2)
    assert not errors
    assert not thread.is_alive()


@pytest.mark.parametrize("failed_class", ["project", "build", "home"])
def test_failed_filesystem_acquire_releases_process_reservation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    failed_class: str,
):
    home = tmp_path / "home"
    project = ProjectLock(home, tmp_path / "project")
    build = BuildLock(home, "build-key")
    home_lock = ManagerHomeLock(home)
    failed = {"project": project, "build": build, "home": home_lock}[failed_class]
    real_open = os.open
    injected = False

    def fail_once(
        path: str | bytes | os.PathLike[str] | os.PathLike[bytes],
        flags: int,
        mode: int = 0o777,
    ) -> int:
        nonlocal injected
        if Path(path) == failed.path and not injected:
            injected = True
            raise PermissionError("injected filesystem failure")
        return real_open(path, flags, mode)

    monkeypatch.setattr(locking.os, "open", fail_once)

    if failed_class == "project":
        with pytest.raises(PermissionError), project:
            pass
        with ManagerHomeLock(home):
            pass
    elif failed_class == "build":
        with project:
            with pytest.raises(PermissionError), build:
                pass
            with ManagerHomeLock(home):
                pass
    else:
        with pytest.raises(PermissionError), home_lock:
            pass
        with ProjectLock(home, tmp_path / "after-failure"):
            pass
    assert injected


def test_failed_project_reentry_preserves_held_process_reservation(
    tmp_path: Path,
):
    home = tmp_path / "home"
    project = ProjectLock(home, tmp_path / "project")

    with project:
        with pytest.raises(LockOrderError, match="already held"), project:
            pass
        with locking._PROCESS_STATE_GUARD:
            process = locking._PROCESS_HOMES[project.home_identity]
            assert project in process.project_held
            assert project not in process.project_pending

    with locking._PROCESS_STATE_GUARD:
        assert project.home_identity not in locking._PROCESS_HOMES


def test_failed_build_reentry_keeps_home_acquisition_blocked(
    tmp_path: Path,
):
    home = tmp_path / "home"
    project = ProjectLock(home, tmp_path / "project")
    build = BuildLock(home, "build-key")
    result: list[str] = []

    def acquire_home() -> None:
        try:
            with ManagerHomeLock(home):
                result.append("acquired")
        except LockOrderError:
            result.append("blocked")

    with project, build:
        with pytest.raises(LockOrderError, match="at most one"), build:
            pass
        with locking._PROCESS_STATE_GUARD:
            process = locking._PROCESS_HOMES[build.home_identity]
            assert build in process.build_held
            assert build not in process.build_pending
        thread = threading.Thread(target=acquire_home)
        thread.start()
        thread.join(timeout=2)
        assert not thread.is_alive()
        assert result == ["blocked"]


def test_failed_home_reentry_keeps_project_acquisition_blocked(
    tmp_path: Path,
):
    home = tmp_path / "home"
    home_lock = ManagerHomeLock(home)
    result: list[str] = []

    def acquire_project() -> None:
        try:
            with ProjectLock(home, tmp_path / "other-project"):
                result.append("acquired")
        except LockOrderError:
            result.append("blocked")

    with home_lock:
        with pytest.raises(LockOrderError, match="already held"), home_lock:
            pass
        with locking._PROCESS_STATE_GUARD:
            process = locking._PROCESS_HOMES[home_lock.home_identity]
            assert home_lock in process.home_held
            assert home_lock not in process.home_pending
        thread = threading.Thread(target=acquire_project)
        thread.start()
        thread.join(timeout=2)
        assert not thread.is_alive()
        assert result == ["blocked"]


def test_home_release_window_hands_off_to_next_serialized_consumer(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    home = tmp_path / "home"
    first = ManagerHomeLock(home, timeout=2)
    second = ManagerHomeLock(home, timeout=2)
    first_acquired = threading.Event()
    release_first = threading.Event()
    after_unlink = threading.Event()
    finish_release = threading.Event()
    results: list[str] = []
    errors: list[Exception] = []
    real_record_released = first._record_released

    def gated_record_released() -> None:
        after_unlink.set()
        if not finish_release.wait(timeout=2):
            raise TimeoutError("test did not finish the first process-state release")
        real_record_released()

    monkeypatch.setattr(first, "_record_released", gated_record_released)

    def first_consumer() -> None:
        try:
            with first:
                first_acquired.set()
                if not release_first.wait(timeout=2):
                    raise TimeoutError("test did not release the first home consumer")
        except Exception as exc:  # noqa: BLE001 - surface worker failures in the parent test
            errors.append(exc)

    def second_consumer() -> None:
        try:
            with second:
                results.append("acquired")
        except Exception as exc:  # noqa: BLE001 - surface worker failures in the parent test
            errors.append(exc)

    first_thread = threading.Thread(target=first_consumer)
    first_thread.start()
    assert first_acquired.wait(timeout=2)
    release_first.set()
    assert after_unlink.wait(timeout=2)

    second_thread = threading.Thread(target=second_consumer)
    second_thread.start()
    second_thread.join(timeout=2)
    finish_release.set()
    first_thread.join(timeout=2)

    assert not first_thread.is_alive()
    assert not second_thread.is_alive()
    assert not errors
    assert results == ["acquired"]


def test_repeated_concurrent_home_consumers_survive_release_handoffs(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    home = tmp_path / "home"
    worker_count = 12
    barrier = threading.Barrier(worker_count)
    acquired: list[int] = []
    errors: list[Exception] = []
    real_record_released = ManagerHomeLock._record_released

    def delayed_record_released(lock: ManagerHomeLock) -> None:
        time.sleep(0.002)
        real_record_released(lock)

    monkeypatch.setattr(
        ManagerHomeLock,
        "_record_released",
        delayed_record_released,
    )

    def consume(index: int) -> None:
        try:
            barrier.wait(timeout=2)
            with ManagerHomeLock(home, timeout=5):
                acquired.append(index)
        except Exception as exc:  # noqa: BLE001 - surface worker failures in the parent test
            errors.append(exc)

    threads = [
        threading.Thread(target=consume, args=(index,)) for index in range(worker_count)
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=7)

    assert not any(thread.is_alive() for thread in threads)
    assert not errors
    assert sorted(acquired) == list(range(worker_count))


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
