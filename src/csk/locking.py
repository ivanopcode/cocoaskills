from __future__ import annotations

import hashlib
import json
import os
import threading
import time
import uuid
from pathlib import Path
from types import TracebackType
from typing import Self


class LockError(Exception):
    pass


class LockOrderError(LockError):
    pass


class _ThreadLockState(threading.local):
    def __init__(self) -> None:
        self.projects: list[bytes] = []
        self.build: bytes | None = None
        self.home: ManagerHomeLock | None = None


_STATE = _ThreadLockState()
_PROCESS_STATE_GUARD = threading.Lock()
_PROCESS_HOME: ManagerHomeLock | None = None


def canonical_project_identity(project: Path) -> str:
    """Return the physical absolute identity used by project operation locks."""
    identity = os.path.normcase(str(project.expanduser().resolve(strict=False)))
    identity.encode("utf-8", errors="strict")
    return identity


def unsigned_utf8_key(value: str) -> bytes:
    return value.encode("utf-8", errors="strict")


class _ExclusiveFileLock:
    def __init__(self, path: Path, *, timeout: float | None = None):
        self.path = path
        self.timeout = _timeout_from_env() if timeout is None else max(0.0, timeout)
        self.acquired = False
        self._token = uuid.uuid4().hex

    def __enter__(self) -> Self:
        self._check_order_before_acquire()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        start = time.monotonic()
        while True:
            try:
                fd = os.open(
                    str(self.path), os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600
                )
                payload = {
                    "pid": os.getpid(),
                    "created_at": time.time(),
                    "token": self._token,
                }
                with os.fdopen(fd, "w", encoding="utf-8") as handle:
                    json.dump(payload, handle, sort_keys=True, separators=(",", ":"))
                    handle.write("\n")
                    handle.flush()
                    os.fsync(handle.fileno())
                self.acquired = True
                try:
                    self._record_acquired()
                except BaseException:
                    data = _read_lock(self.path)
                    if data is not None and data.get("token") == self._token:
                        self.path.unlink(missing_ok=True)
                    self.acquired = False
                    raise
                return self
            except FileExistsError as exc:
                if self._break_stale_lock():
                    continue
                if time.monotonic() - start >= self.timeout:
                    raise LockError(_timeout_message(self.path)) from exc
                time.sleep(min(0.1, max(0.01, self.timeout / 10)))

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        if not self.acquired:
            return
        self._check_order_before_release()
        try:
            data = _read_lock(self.path)
            if data is not None and data.get("token") == self._token:
                self.path.unlink(missing_ok=True)
        finally:
            self.acquired = False
            self._record_released()

    def assert_held(self) -> None:
        if not self.acquired:
            raise LockError(f"lock is not held: {self.path}")
        data = _read_lock(self.path)
        if (
            data is None
            or data.get("pid") != os.getpid()
            or data.get("token") != self._token
        ):
            raise LockError(f"lock ownership was lost: {self.path}")

    def _check_order_before_acquire(self) -> None:
        raise NotImplementedError

    def _record_acquired(self) -> None:
        raise NotImplementedError

    def _check_order_before_release(self) -> None:
        raise NotImplementedError

    def _record_released(self) -> None:
        raise NotImplementedError

    def _break_stale_lock(self) -> bool:
        """Remove a lock only when its recorded process is provably dead."""
        data = _read_lock(self.path)
        if data is None:
            return False
        pid = data.get("pid")
        if not isinstance(pid, int) or isinstance(pid, bool) or _pid_alive(pid):
            return False
        stale = self.path.with_name(
            f"{self.path.name}.stale-{os.getpid()}-{uuid.uuid4().hex}"
        )
        try:
            self.path.rename(stale)
        except OSError:
            return False
        current = _read_lock(stale)
        if current != data:
            try:
                stale.rename(self.path)
            except OSError:
                pass
            return False
        try:
            stale.unlink()
        except OSError:
            pass
        return True


class ProjectLock(_ExclusiveFileLock):
    def __init__(self, csk_home: Path, project: Path, *, timeout: float | None = None):
        self.identity = canonical_project_identity(project)
        self.identity_key = unsigned_utf8_key(self.identity)
        digest = hashlib.sha256(
            b"csk-project-lock-v1\0" + self.identity_key
        ).hexdigest()
        super().__init__(
            csk_home / "locks" / "projects" / f"{digest}.lock", timeout=timeout
        )

    def _check_order_before_acquire(self) -> None:
        with _PROCESS_STATE_GUARD:
            process_home = _PROCESS_HOME
        if process_home is not None:
            raise LockOrderError(
                "a project lock cannot be acquired while the manager-home lock is held"
            )
        if _STATE.home is not None:
            raise LockOrderError(
                "a project lock cannot be acquired while the manager-home lock is held"
            )
        if _STATE.build is not None:
            raise LockOrderError(
                "a project lock cannot be acquired while a cache-build lock is held"
            )
        if self.identity_key in _STATE.projects:
            raise LockOrderError(f"project lock is already held: {self.identity}")
        if _STATE.projects and self.identity_key <= _STATE.projects[-1]:
            raise LockOrderError(
                "project locks must be acquired in unsigned UTF-8 byte order"
            )

    def _record_acquired(self) -> None:
        with _PROCESS_STATE_GUARD:
            if _PROCESS_HOME is not None:
                raise LockOrderError(
                    "a project lock cannot be acquired while the manager-home lock is held"
                )
        _STATE.projects.append(self.identity_key)

    def _check_order_before_release(self) -> None:
        if _STATE.home is not None or _STATE.build is not None:
            raise LockOrderError(
                "project locks must outlive cache-build and manager-home locks"
            )
        if not _STATE.projects or _STATE.projects[-1] != self.identity_key:
            raise LockOrderError(
                "project locks must be released in reverse acquisition order"
            )

    def _record_released(self) -> None:
        if _STATE.projects and _STATE.projects[-1] == self.identity_key:
            _STATE.projects.pop()


class ProjectLocks:
    """Acquire a project set by canonical identity in unsigned UTF-8 order."""

    def __init__(
        self,
        csk_home: Path,
        projects: list[Path] | tuple[Path, ...],
        *,
        timeout: float | None = None,
    ):
        by_identity = {
            canonical_project_identity(project): project for project in projects
        }
        identities = sorted(by_identity, key=unsigned_utf8_key)
        self.locks = [
            ProjectLock(csk_home, by_identity[identity], timeout=timeout)
            for identity in identities
        ]

    def __enter__(self) -> Self:
        acquired: list[ProjectLock] = []
        try:
            for lock in self.locks:
                lock.__enter__()
                acquired.append(lock)
        except BaseException:
            for lock in reversed(acquired):
                lock.__exit__(None, None, None)
            raise
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        for lock in reversed(self.locks):
            lock.__exit__(exc_type, exc, tb)


class BuildLock(_ExclusiveFileLock):
    def __init__(self, csk_home: Path, key: str, *, timeout: float | None = None):
        self.key = unsigned_utf8_key(key)
        digest = hashlib.sha256(b"csk-build-lock-v1\0" + self.key).hexdigest()
        super().__init__(
            csk_home / "locks" / "builds" / f"{digest}.lock", timeout=timeout
        )

    def _check_order_before_acquire(self) -> None:
        with _PROCESS_STATE_GUARD:
            process_home = _PROCESS_HOME
        if process_home is not None:
            raise LockOrderError(
                "a cache-build lock cannot be acquired while the manager-home lock is held"
            )
        if _STATE.home is not None:
            raise LockOrderError(
                "a cache-build lock cannot be acquired while the manager-home lock is held"
            )
        if not _STATE.projects:
            raise LockOrderError(
                "a cache-build lock requires a held project operation lock"
            )
        if _STATE.build is not None:
            raise LockOrderError("at most one cache-build lock may be held")

    def _record_acquired(self) -> None:
        with _PROCESS_STATE_GUARD:
            if _PROCESS_HOME is not None:
                raise LockOrderError(
                    "a cache-build lock cannot be acquired while the manager-home lock is held"
                )
        _STATE.build = self.key

    def _check_order_before_release(self) -> None:
        if _STATE.home is not None:
            raise LockOrderError(
                "the cache-build lock must be released before the manager-home lock"
            )
        if _STATE.build != self.key:
            raise LockOrderError("cache-build lock ownership is inconsistent")

    def _record_released(self) -> None:
        if _STATE.build == self.key:
            _STATE.build = None


class ManagerHomeLock(_ExclusiveFileLock):
    def __init__(self, csk_home: Path, timeout: float | None = None):
        super().__init__(csk_home / ".lock", timeout=timeout)

    def _check_order_before_acquire(self) -> None:
        if _STATE.home is not None:
            raise LockOrderError("the manager-home lock is already held")
        if _STATE.build is not None:
            raise LockOrderError(
                "the cache-build lock must be released before the manager-home lock"
            )

    def _record_acquired(self) -> None:
        global _PROCESS_HOME
        with _PROCESS_STATE_GUARD:
            if _PROCESS_HOME is not None:
                raise LockOrderError("the manager-home lock is already held")
            _PROCESS_HOME = self
        _STATE.home = self

    def _check_order_before_release(self) -> None:
        if _STATE.home is not self:
            raise LockOrderError("manager-home lock ownership is inconsistent")

    def _record_released(self) -> None:
        global _PROCESS_HOME
        with _PROCESS_STATE_GUARD:
            if _PROCESS_HOME is self:
                _PROCESS_HOME = None
        if _STATE.home is self:
            _STATE.home = None

    def assert_held(self) -> None:
        with _PROCESS_STATE_GUARD:
            process_home = _PROCESS_HOME
        if _STATE.home is not self or process_home is not self:
            raise LockError("manager-home lock is not held by this execution thread")
        super().assert_held()


class GlobalLock(ManagerHomeLock):
    """Backward-compatible name for the single manager-home mutation lock."""


def _read_lock(path: Path) -> dict[str, object] | None:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return value if isinstance(value, dict) else None


def _pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    if os.name == "nt":
        import ctypes

        process_query_limited_information = 0x1000
        still_active = 259
        kernel32 = ctypes.windll.kernel32  # type: ignore[attr-defined]
        handle = kernel32.OpenProcess(process_query_limited_information, False, pid)
        if not handle:
            return False
        try:
            exit_code = ctypes.c_ulong()
            if kernel32.GetExitCodeProcess(handle, ctypes.byref(exit_code)):
                return exit_code.value == still_active
            return True
        finally:
            kernel32.CloseHandle(handle)
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return True
    return True


def _timeout_message(path: Path) -> str:
    detail = ""
    data = _read_lock(path)
    if data is not None:
        detail = f" pid={data.get('pid')} created_at={data.get('created_at')}"
    return (
        f"another csk process holds lock at {path};{detail} "
        "remove it only after verifying the process is stale"
    )


def _timeout_from_env() -> float:
    raw = os.environ.get("CSK_LOCK_TIMEOUT")
    if raw is None:
        return 30.0
    try:
        return max(0.0, float(raw))
    except ValueError:
        return 30.0
