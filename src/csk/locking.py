from __future__ import annotations

import ctypes
import errno
import hashlib
import json
import ntpath
import os
import re
import stat
import sys
import threading
import time
import uuid
from collections.abc import Callable
from pathlib import Path
from types import TracebackType
from typing import Any, Self

_LOCK_PROTOCOL = "csk-stable-file-lock-v1"
_LEGACY_PID_GUARD = f"{_LOCK_PROTOCOL}:persistent"
_LOCK_RECORD_LIMIT = 64 * 1024
_ERROR_LOCK_VIOLATION = 33
_ERROR_INVALID_FUNCTION = 1
_ERROR_NOT_SUPPORTED = 50
_ERROR_INVALID_PARAMETER = 87
_ERROR_CALL_NOT_IMPLEMENTED = 120
_LOCKFILE_FAIL_IMMEDIATELY = 0x00000001
_LOCKFILE_EXCLUSIVE_LOCK = 0x00000002
_FILE_READ_ATTRIBUTES = 0x00000080
_FILE_SHARE_READ = 0x00000001
_FILE_SHARE_WRITE = 0x00000002
_FILE_SHARE_DELETE = 0x00000004
_OPEN_EXISTING = 3
_FILE_FLAG_BACKUP_SEMANTICS = 0x02000000
_FILE_CASE_SENSITIVE_INFO = 23
_FILE_CS_FLAG_CASE_SENSITIVE_DIR = 0x00000001


class LockError(Exception):
    pass


class LockOrderError(LockError):
    pass


class _ThreadLockState(threading.local):
    def __init__(self) -> None:
        self.projects: list[tuple[str, bytes]] = []
        self.build: tuple[str, bytes] | None = None
        self.home: ManagerHomeLock | None = None


class _ManagerHomeProcessState:
    def __init__(self) -> None:
        self.project_pending: set[object] = set()
        self.project_held: set[object] = set()
        self.build_pending: set[object] = set()
        self.build_held: set[object] = set()
        self.home_pending: set[object] = set()
        self.home_held: set[object] = set()

    def empty(self) -> bool:
        return not (
            self.project_pending
            or self.project_held
            or self.build_pending
            or self.build_held
            or self.home_pending
            or self.home_held
        )


_STATE = _ThreadLockState()
_PROCESS_STATE_GUARD = threading.Lock()
_PROCESS_HOMES: dict[str, _ManagerHomeProcessState] = {}


def canonical_project_identity(project: Path) -> str:
    """Return the physical absolute identity used by project operation locks."""
    return _canonical_filesystem_identity(project, label="project path")


def canonical_manager_home_identity(csk_home: Path) -> str:
    """Return the physical absolute identity shared by all manager-home locks."""
    return _canonical_manager_home(csk_home)


def unsigned_utf8_key(value: str) -> bytes:
    return value.encode("utf-8", errors="strict")


def _canonical_manager_home(csk_home: Path) -> str:
    return _canonical_filesystem_identity(csk_home, label="manager home")


def _configured_manager_home(csk_home: Path) -> Path:
    try:
        configured = Path(os.path.abspath(csk_home.expanduser()))
    except (OSError, RuntimeError, ValueError) as exc:
        raise LockError(f"cannot resolve configured manager home: {csk_home}") from exc
    str(configured).encode("utf-8", errors="strict")
    return configured


def _prepare_manager_home(csk_home: Path) -> str:
    """Create first-use state, then bind locking to its observable identity."""
    provision_new_manager_home(csk_home)
    return _canonical_manager_home(csk_home)


def provision_new_manager_home(csk_home: Path) -> None:
    """Create a manager home, if absent, in its required private state.

    POSIX is finished once the create mode is applied: the effective user owns
    what it creates. Windows assigns new-object ownership from the token owner
    and inherits the containing DACL, so an elevated administrator does not own
    the home it just made and the protected build cache would reject it. Only a
    home this call creates is provisioned, so ownership drift on an established
    home still fails closed at inspection.
    """
    from .builds.cache import BuildCacheError
    from .builds.cache import provision_manager_home as _provision

    try:
        csk_home.parent.mkdir(parents=True, exist_ok=True)
        csk_home.mkdir(mode=0o700)
    except FileExistsError as exc:
        # mkdir(exist_ok=True) tolerates only an existing *directory*: CPython
        # re-raises when the name is taken by anything else. Distinguishing a
        # home this call created from one it found costs that condition, so it
        # is restated here. Anything but a directory must still fail closed,
        # and a dangling symlink is not a directory.
        if not csk_home.is_dir():
            raise LockError(f"cannot create manager home: {csk_home}") from exc
        return
    except OSError as exc:
        raise LockError(f"cannot create manager home: {csk_home}") from exc
    try:
        _provision(csk_home)
    except (BuildCacheError, OSError) as exc:
        raise LockError(f"cannot make the manager home private: {csk_home}") from exc


def _canonical_filesystem_identity(path: Path, *, label: str) -> str:
    try:
        absolute = Path(os.path.abspath(path.expanduser()))
        resolved = absolute.resolve(strict=False)
        existing, missing = _longest_existing_prefix(resolved)
        if os.name == "nt":
            identity = _canonicalize_windows_identity(
                str(existing),
                missing,
                case_sensitive=_windows_directory_case_sensitive,
            )
        else:
            canonical_existing = (
                _canonicalize_darwin_existing_path(existing)
                if sys.platform == "darwin"
                else existing
            )
            identity = str(canonical_existing.joinpath(*reversed(missing)))
    except (OSError, RuntimeError, ValueError) as exc:
        raise LockError(f"cannot resolve physical {label}: {path}") from exc
    identity.encode("utf-8", errors="strict")
    return identity


def _longest_existing_prefix(path: Path) -> tuple[Path, list[str]]:
    current = path
    missing: list[str] = []
    while True:
        try:
            current.lstat()
            return current, missing
        except FileNotFoundError:
            parent = current.parent
            if parent == current:
                raise
            missing.append(current.name)
            current = parent


def _canonicalize_darwin_existing_path(path: Path) -> Path:
    """Recover stored spelling without enumerating every parent directory."""
    if not path.is_absolute():
        raise ValueError("physical identity path is not absolute")

    import fcntl

    flags = getattr(os, "O_EVTONLY", os.O_RDONLY)
    flags |= getattr(os, "O_CLOEXEC", 0)
    flags |= getattr(os, "O_NOFOLLOW", 0)
    fd = os.open(path, flags)
    try:
        # Darwin MAXPATHLEN is 1024, also Python's maximum mutable fcntl buffer.
        raw = fcntl.fcntl(fd, 50, b"\0" * 1024)
    finally:
        os.close(fd)
    if not isinstance(raw, bytes):
        raise OSError(f"cannot recover stored path spelling for {path}")
    encoded = raw.split(b"\0", 1)[0]
    if not encoded:
        raise OSError(f"cannot recover stored path spelling for {path}")
    canonical = Path(os.fsdecode(encoded))
    if not canonical.is_absolute() or not os.path.samefile(path, canonical):
        raise OSError(f"physical path identity changed while resolving {path}")
    return canonical


def _canonicalize_windows_identity(
    existing: str,
    missing: list[str],
    *,
    case_sensitive: Callable[[str], bool],
) -> str:
    """Canonicalize each component using its parent directory's lookup rules."""
    normalized = ntpath.normpath(existing)
    volume, tail = ntpath.splitdrive(normalized)
    if not volume or not tail.startswith(("\\", "/")):
        raise ValueError("Windows physical identity has no absolute volume")
    root = volume + "\\"
    components = [value for value in re.split(r"[\\/]+", tail.lstrip("\\/")) if value]
    lookup = root
    canonical = volume.upper() + "\\"
    parent_is_case_sensitive = case_sensitive(lookup)
    for index, component in enumerate(components):
        identity_component = (
            component if parent_is_case_sensitive else component.upper()
        )
        canonical = ntpath.join(canonical, identity_component)
        lookup = ntpath.join(lookup, component)
        if index + 1 < len(components) or missing:
            parent_is_case_sensitive = case_sensitive(lookup)
    for component in reversed(missing):
        identity_component = (
            component if parent_is_case_sensitive else component.upper()
        )
        canonical = ntpath.join(canonical, identity_component)
        parent_is_case_sensitive = False
    return ntpath.normpath(canonical)


def _windows_directory_case_sensitive(path: str) -> bool:
    win_dll: Any = ctypes.WinDLL  # type: ignore[attr-defined]
    get_last_error: Any = ctypes.get_last_error  # type: ignore[attr-defined]
    format_error: Any = ctypes.FormatError  # type: ignore[attr-defined]
    kernel32: Any = win_dll("kernel32", use_last_error=True)
    create_file: Any = kernel32.CreateFileW
    create_file.argtypes = [
        ctypes.c_wchar_p,
        ctypes.c_uint32,
        ctypes.c_uint32,
        ctypes.c_void_p,
        ctypes.c_uint32,
        ctypes.c_uint32,
        ctypes.c_void_p,
    ]
    create_file.restype = ctypes.c_void_p
    get_file_information: Any = kernel32.GetFileInformationByHandleEx
    get_file_information.argtypes = [
        ctypes.c_void_p,
        ctypes.c_int,
        ctypes.c_void_p,
        ctypes.c_uint32,
    ]
    get_file_information.restype = ctypes.c_int
    close_handle: Any = kernel32.CloseHandle
    close_handle.argtypes = [ctypes.c_void_p]
    close_handle.restype = ctypes.c_int

    handle = create_file(
        path,
        _FILE_READ_ATTRIBUTES,
        _FILE_SHARE_READ | _FILE_SHARE_WRITE | _FILE_SHARE_DELETE,
        None,
        _OPEN_EXISTING,
        _FILE_FLAG_BACKUP_SEMANTICS,
        None,
    )
    if handle in {None, ctypes.c_void_p(-1).value}:
        error_number = int(get_last_error())
        raise OSError(
            error_number,
            f"{format_error(error_number)}: cannot inspect case sensitivity: {path}",
        )
    flags = ctypes.c_uint32()
    try:
        if get_file_information(
            handle,
            _FILE_CASE_SENSITIVE_INFO,
            ctypes.byref(flags),
            ctypes.sizeof(flags),
        ):
            return bool(flags.value & _FILE_CS_FLAG_CASE_SENSITIVE_DIR)
        error_number = int(get_last_error())
        if error_number in {
            _ERROR_INVALID_FUNCTION,
            _ERROR_NOT_SUPPORTED,
            _ERROR_INVALID_PARAMETER,
            _ERROR_CALL_NOT_IMPLEMENTED,
        }:
            return False
        raise OSError(
            error_number,
            f"{format_error(error_number)}: cannot inspect case sensitivity: {path}",
        )
    finally:
        close_handle(handle)


def _home_process_state(identity: str) -> _ManagerHomeProcessState:
    return _PROCESS_HOMES.setdefault(identity, _ManagerHomeProcessState())


def _discard_empty_home_process_state(identity: str) -> None:
    state = _PROCESS_HOMES.get(identity)
    if state is not None and state.empty():
        del _PROCESS_HOMES[identity]


class _ExclusiveFileLock:
    """Hold one stable lock file with an operating-system advisory lock."""

    def __init__(self, path: Path, *, timeout: float | None = None):
        self.path = path
        self.timeout = _timeout_from_env() if timeout is None else max(0.0, timeout)
        self.acquired = False
        self._token = uuid.uuid4().hex
        self._fd: int | None = None

    def __enter__(self) -> Self:
        deadline = time.monotonic() + self.timeout
        self._prepare_for_acquire()
        if self.timeout > 0 and time.monotonic() >= deadline:
            raise LockError(
                f"lock acquisition timed out during preparation: {self.path}"
            )
        self._check_order_before_acquire()
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            if self.timeout > 0 and time.monotonic() >= deadline:
                raise LockError(
                    f"lock acquisition timed out during preparation: {self.path}"
                )
            while True:
                flags = os.O_CREAT | os.O_RDWR
                flags |= getattr(os, "O_CLOEXEC", 0)
                flags |= getattr(os, "O_NOFOLLOW", 0)
                # Windows text translation makes fstat and read byte counts diverge.
                flags |= getattr(os, "O_BINARY", 0)
                fd = os.open(str(self.path), flags, 0o600)
                locked = False
                retained = False
                try:
                    locked = _try_file_lock(fd)
                    if locked:
                        if not _path_matches_fd(self.path, fd):
                            raise LockError(
                                f"lock path changed while acquiring: {self.path}"
                            )
                        _assert_no_legacy_stale_breaker(self.path)
                        _validate_stable_lock_record(fd, self.path)
                        payload = {
                            "protocol": _LOCK_PROTOCOL,
                            "pid": _LEGACY_PID_GUARD,
                            "owner_pid": os.getpid(),
                            "created_at": time.time(),
                            "token": self._token,
                        }
                        _write_lock_fd(fd, payload)
                        if not _path_matches_fd(self.path, fd):
                            raise LockError(
                                f"lock path changed after publication: {self.path}"
                            )
                        _assert_no_legacy_stale_breaker(self.path)
                        published = _read_lock_fd(fd)
                        if (
                            published is None
                            or published.get("protocol") != _LOCK_PROTOCOL
                            or published.get("owner_pid") != os.getpid()
                            or published.get("token") != self._token
                        ):
                            raise LockError(
                                f"lock publication witness was lost: {self.path}"
                            )
                        if not _path_matches_fd(self.path, fd):
                            raise LockError(
                                f"lock path changed after publication: {self.path}"
                            )
                        _assert_no_legacy_stale_breaker(self.path)
                        self._fd = fd
                        self.acquired = True
                        try:
                            self._record_acquired()
                        except BaseException:
                            self.acquired = False
                            self._fd = None
                            raise
                        retained = True
                        return self
                finally:
                    if not retained:
                        if locked:
                            try:
                                _unlock_file(fd)
                            finally:
                                os.close(fd)
                        else:
                            os.close(fd)
                now = time.monotonic()
                if now >= deadline:
                    raise LockError(_timeout_message(self.path))
                time.sleep(min(0.1, max(0.01, self.timeout / 10), deadline - now))
        except BaseException:
            self._record_acquire_failed()
            raise

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        if not self.acquired:
            return
        self._check_order_before_release()
        fd = self._fd
        try:
            if fd is not None:
                try:
                    _unlock_file(fd)
                finally:
                    os.close(fd)
        finally:
            self._fd = None
            self.acquired = False
            self._record_released()

    def assert_held(self) -> None:
        fd = self._fd
        if not self.acquired or fd is None:
            raise LockError(f"lock is not held: {self.path}")
        data = _read_lock_fd(fd)
        if (
            not _path_matches_fd(self.path, fd)
            or data is None
            or data.get("protocol") != _LOCK_PROTOCOL
            or data.get("owner_pid") != os.getpid()
            or data.get("token") != self._token
        ):
            raise LockError(f"lock ownership was lost: {self.path}")

    def _prepare_for_acquire(self) -> None:
        pass

    def _check_order_before_acquire(self) -> None:
        raise NotImplementedError

    def _record_acquired(self) -> None:
        raise NotImplementedError

    def _record_acquire_failed(self) -> None:
        raise NotImplementedError

    def _check_order_before_release(self) -> None:
        raise NotImplementedError

    def _record_released(self) -> None:
        raise NotImplementedError


class ProjectLock(_ExclusiveFileLock):
    def __init__(self, csk_home: Path, project: Path, *, timeout: float | None = None):
        self._configured_home = _configured_manager_home(csk_home)
        self.home_identity = _canonical_manager_home(self._configured_home)
        self.identity = canonical_project_identity(project)
        self.identity_key = unsigned_utf8_key(self.identity)
        self._lock_digest = hashlib.sha256(
            b"csk-project-lock-v1\0" + self.identity_key
        ).hexdigest()
        super().__init__(
            Path(self.home_identity)
            / "locks"
            / "projects"
            / f"{self._lock_digest}.lock",
            timeout=timeout,
        )

    def _prepare_for_acquire(self) -> None:
        if self.acquired:
            return
        self.home_identity = _prepare_manager_home(self._configured_home)
        self.path = (
            Path(self.home_identity)
            / "locks"
            / "projects"
            / f"{self._lock_digest}.lock"
        )

    def _check_order_before_acquire(self) -> None:
        if _STATE.home is not None:
            raise LockOrderError(
                "a project lock cannot be acquired while the manager-home lock is held"
            )
        if _STATE.build is not None:
            raise LockOrderError(
                "a project lock cannot be acquired while a cache-build lock is held"
            )
        ownership = (self.home_identity, self.identity_key)
        if ownership in _STATE.projects:
            raise LockOrderError(f"project lock is already held: {self.identity}")
        if _STATE.projects and self.identity_key <= _STATE.projects[-1][1]:
            raise LockOrderError(
                "project locks must be acquired in unsigned UTF-8 byte order"
            )
        with _PROCESS_STATE_GUARD:
            process = _home_process_state(self.home_identity)
            if process.home_pending or process.home_held:
                _discard_empty_home_process_state(self.home_identity)
                raise LockOrderError(
                    "a project lock cannot be acquired while the manager-home lock "
                    "is pending or held"
                )
            process.project_pending.add(self)

    def _record_acquired(self) -> None:
        with _PROCESS_STATE_GUARD:
            process = _home_process_state(self.home_identity)
            if self not in process.project_pending:
                raise LockOrderError("project lock reservation is inconsistent")
            if process.home_pending or process.home_held:
                raise LockOrderError(
                    "a project lock cannot be acquired while the manager-home lock "
                    "is pending or held"
                )
            process.project_pending.remove(self)
            process.project_held.add(self)
        _STATE.projects.append((self.home_identity, self.identity_key))

    def _record_acquire_failed(self) -> None:
        with _PROCESS_STATE_GUARD:
            process = _PROCESS_HOMES.get(self.home_identity)
            if process is not None:
                process.project_pending.discard(self)
                _discard_empty_home_process_state(self.home_identity)

    def _check_order_before_release(self) -> None:
        if _STATE.home is not None or _STATE.build is not None:
            raise LockOrderError(
                "project locks must outlive cache-build and manager-home locks"
            )
        if not _STATE.projects or _STATE.projects[-1] != (
            self.home_identity,
            self.identity_key,
        ):
            raise LockOrderError(
                "project locks must be released in reverse acquisition order"
            )

    def _record_released(self) -> None:
        with _PROCESS_STATE_GUARD:
            process = _PROCESS_HOMES.get(self.home_identity)
            if process is not None:
                process.project_held.discard(self)
                _discard_empty_home_process_state(self.home_identity)
        if _STATE.projects and _STATE.projects[-1] == (
            self.home_identity,
            self.identity_key,
        ):
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
        self._configured_home = _configured_manager_home(csk_home)
        self.home_identity = _canonical_manager_home(self._configured_home)
        self.key = unsigned_utf8_key(key)
        self._lock_digest = hashlib.sha256(
            b"csk-build-lock-v1\0" + self.key
        ).hexdigest()
        super().__init__(
            Path(self.home_identity) / "locks" / "builds" / f"{self._lock_digest}.lock",
            timeout=timeout,
        )

    def _prepare_for_acquire(self) -> None:
        if self.acquired:
            return
        self.home_identity = _prepare_manager_home(self._configured_home)
        self.path = (
            Path(self.home_identity) / "locks" / "builds" / f"{self._lock_digest}.lock"
        )

    def _check_order_before_acquire(self) -> None:
        if _STATE.home is not None:
            raise LockOrderError(
                "a cache-build lock cannot be acquired while the manager-home lock is held"
            )
        if not any(
            home_identity == self.home_identity for home_identity, _ in _STATE.projects
        ):
            raise LockOrderError(
                "a cache-build lock requires a held project operation lock"
            )
        if _STATE.build is not None:
            raise LockOrderError("at most one cache-build lock may be held")
        with _PROCESS_STATE_GUARD:
            process = _home_process_state(self.home_identity)
            if process.home_pending or process.home_held:
                _discard_empty_home_process_state(self.home_identity)
                raise LockOrderError(
                    "a cache-build lock cannot be acquired while the manager-home "
                    "lock is pending or held"
                )
            process.build_pending.add(self)

    def _record_acquired(self) -> None:
        with _PROCESS_STATE_GUARD:
            process = _home_process_state(self.home_identity)
            if self not in process.build_pending:
                raise LockOrderError("cache-build lock reservation is inconsistent")
            if process.home_pending or process.home_held:
                raise LockOrderError(
                    "a cache-build lock cannot be acquired while the manager-home "
                    "lock is pending or held"
                )
            process.build_pending.remove(self)
            process.build_held.add(self)
        _STATE.build = (self.home_identity, self.key)

    def _record_acquire_failed(self) -> None:
        with _PROCESS_STATE_GUARD:
            process = _PROCESS_HOMES.get(self.home_identity)
            if process is not None:
                process.build_pending.discard(self)
                _discard_empty_home_process_state(self.home_identity)

    def _check_order_before_release(self) -> None:
        if _STATE.home is not None:
            raise LockOrderError(
                "the cache-build lock must be released before the manager-home lock"
            )
        if _STATE.build != (self.home_identity, self.key):
            raise LockOrderError("cache-build lock ownership is inconsistent")

    def _record_released(self) -> None:
        with _PROCESS_STATE_GUARD:
            process = _PROCESS_HOMES.get(self.home_identity)
            if process is not None:
                process.build_held.discard(self)
                _discard_empty_home_process_state(self.home_identity)
        if _STATE.build == (self.home_identity, self.key):
            _STATE.build = None


class ManagerHomeLock(_ExclusiveFileLock):
    def __init__(self, csk_home: Path, timeout: float | None = None):
        self._configured_home = _configured_manager_home(csk_home)
        self._home_identity = _canonical_manager_home(self._configured_home)
        super().__init__(Path(self._home_identity) / ".lock", timeout=timeout)

    @property
    def home_identity(self) -> str:
        """Immutable identity of the home covered by an acquired witness."""
        return self._home_identity

    def _prepare_for_acquire(self) -> None:
        if self.acquired:
            return
        self._home_identity = _prepare_manager_home(self._configured_home)
        self.path = Path(self._home_identity) / ".lock"

    def _check_order_before_acquire(self) -> None:
        if _STATE.home is not None:
            raise LockOrderError("the manager-home lock is already held")
        if _STATE.build is not None:
            raise LockOrderError(
                "the cache-build lock must be released before the manager-home lock"
            )
        with _PROCESS_STATE_GUARD:
            process = _home_process_state(self.home_identity)
            if process.build_pending or process.build_held:
                _discard_empty_home_process_state(self.home_identity)
                raise LockOrderError(
                    "the cache-build lock must be released before the manager-home lock"
                )
            if process.project_pending:
                _discard_empty_home_process_state(self.home_identity)
                raise LockOrderError(
                    "a project lock acquisition is pending before the manager-home lock"
                )
            process.home_pending.add(self)

    def _record_acquired(self) -> None:
        with _PROCESS_STATE_GUARD:
            process = _home_process_state(self.home_identity)
            if self not in process.home_pending:
                raise LockOrderError("manager-home lock reservation is inconsistent")
            if process.build_pending or process.build_held or process.project_pending:
                raise LockOrderError(
                    "outer lock acquisition appeared while the manager-home lock "
                    "was pending"
                )
            process.home_pending.remove(self)
            process.home_held.add(self)
        _STATE.home = self

    def _record_acquire_failed(self) -> None:
        with _PROCESS_STATE_GUARD:
            process = _PROCESS_HOMES.get(self.home_identity)
            if process is not None:
                process.home_pending.discard(self)
                _discard_empty_home_process_state(self.home_identity)

    def _check_order_before_release(self) -> None:
        if _STATE.home is not self:
            raise LockOrderError("manager-home lock ownership is inconsistent")

    def _record_released(self) -> None:
        with _PROCESS_STATE_GUARD:
            process = _PROCESS_HOMES.get(self.home_identity)
            if process is not None:
                process.home_held.discard(self)
                _discard_empty_home_process_state(self.home_identity)
        if _STATE.home is self:
            _STATE.home = None

    def assert_held(self) -> None:
        with _PROCESS_STATE_GUARD:
            process = _PROCESS_HOMES.get(self.home_identity)
            process_holds_home = process is not None and self in process.home_held
        if _STATE.home is not self or not process_holds_home:
            raise LockError("manager-home lock is not held by this execution thread")
        super().assert_held()


class GlobalLock(ManagerHomeLock):
    """Backward-compatible name for the single manager-home mutation lock."""


class _WindowsOverlapped(ctypes.Structure):
    _fields_ = [
        ("Internal", ctypes.c_size_t),
        ("InternalHigh", ctypes.c_size_t),
        ("Offset", ctypes.c_uint32),
        ("OffsetHigh", ctypes.c_uint32),
        ("hEvent", ctypes.c_void_p),
    ]


def _try_file_lock(fd: int) -> bool:
    if os.name == "nt":
        return _windows_try_file_lock(fd)
    return _posix_try_file_lock(fd)


def _unlock_file(fd: int) -> None:
    if os.name == "nt":
        _windows_unlock_file(fd)
    else:
        _posix_unlock_file(fd)


def _posix_try_file_lock(fd: int) -> bool:
    import fcntl

    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError as exc:
        if exc.errno in {errno.EACCES, errno.EAGAIN, errno.EWOULDBLOCK}:
            return False
        raise
    return True


def _posix_unlock_file(fd: int) -> None:
    import fcntl

    fcntl.flock(fd, fcntl.LOCK_UN)


def _windows_try_file_lock(fd: int) -> bool:
    import msvcrt

    win_dll: Any = ctypes.WinDLL  # type: ignore[attr-defined]
    get_last_error: Any = ctypes.get_last_error  # type: ignore[attr-defined]
    format_error: Any = ctypes.FormatError  # type: ignore[attr-defined]
    get_osfhandle: Any = msvcrt.get_osfhandle  # type: ignore[attr-defined]
    kernel32: Any = win_dll("kernel32", use_last_error=True)
    lock_file_ex: Any = kernel32.LockFileEx
    lock_file_ex.argtypes = [
        ctypes.c_void_p,
        ctypes.c_uint32,
        ctypes.c_uint32,
        ctypes.c_uint32,
        ctypes.c_uint32,
        ctypes.POINTER(_WindowsOverlapped),
    ]
    lock_file_ex.restype = ctypes.c_int
    overlapped = _WindowsOverlapped()
    handle = ctypes.c_void_p(get_osfhandle(fd))
    if lock_file_ex(
        handle,
        _LOCKFILE_EXCLUSIVE_LOCK | _LOCKFILE_FAIL_IMMEDIATELY,
        0,
        1,
        0,
        ctypes.byref(overlapped),
    ):
        return True
    error_number = int(get_last_error())
    if error_number == _ERROR_LOCK_VIOLATION:
        return False
    raise OSError(
        error_number,
        f"{format_error(error_number)}: cannot acquire stable file lock",
    )


def _windows_unlock_file(fd: int) -> None:
    import msvcrt

    win_dll: Any = ctypes.WinDLL  # type: ignore[attr-defined]
    get_last_error: Any = ctypes.get_last_error  # type: ignore[attr-defined]
    format_error: Any = ctypes.FormatError  # type: ignore[attr-defined]
    get_osfhandle: Any = msvcrt.get_osfhandle  # type: ignore[attr-defined]
    kernel32: Any = win_dll("kernel32", use_last_error=True)
    unlock_file_ex: Any = kernel32.UnlockFileEx
    unlock_file_ex.argtypes = [
        ctypes.c_void_p,
        ctypes.c_uint32,
        ctypes.c_uint32,
        ctypes.c_uint32,
        ctypes.POINTER(_WindowsOverlapped),
    ]
    unlock_file_ex.restype = ctypes.c_int
    overlapped = _WindowsOverlapped()
    handle = ctypes.c_void_p(get_osfhandle(fd))
    if unlock_file_ex(handle, 0, 1, 0, ctypes.byref(overlapped)):
        return
    error_number = int(get_last_error())
    raise OSError(
        error_number,
        f"{format_error(error_number)}: cannot release stable file lock",
    )


def _validate_stable_lock_record(fd: int, path: Path) -> None:
    """Reject every online legacy-to-v1 transition without changing its bytes."""
    payload = _read_lock_fd_bytes(fd)
    if payload == b"":
        return
    if payload is None:
        raise LockError(
            f"unrecognized lock state at {path}; offline inspection is required"
        )
    data = _decode_lock(payload)
    if data is None:
        raise LockError(
            f"unrecognized lock state at {path}; offline inspection is required"
        )
    if data.get("protocol") == _LOCK_PROTOCOL:
        if data.get("pid") == _LEGACY_PID_GUARD:
            return
        raise LockError(
            f"unguarded stable lock state at {path} cannot be migrated online; "
            "offline migration is required"
        )
    pid = data.get("pid")
    if isinstance(pid, int) and not isinstance(pid, bool) and _pid_alive(pid):
        raise LockError(
            f"another csk process holds lock at {path}; "
            "legacy lock state cannot be migrated online and requires "
            "offline migration"
        )
    raise LockError(
        f"legacy lock state at {path} cannot be migrated online; "
        "offline migration is required"
    )


def _assert_no_legacy_stale_breaker(path: Path) -> None:
    """Fail closed while an old rename-based breaker owns a side witness."""
    prefix = f"{path.name}.stale-"
    try:
        if any(
            candidate.name.startswith(prefix) for candidate in path.parent.iterdir()
        ):
            raise LockError(
                f"legacy stale-lock breaker state overlaps {path}; "
                "offline migration is required"
            )
    except FileNotFoundError:
        return
    except OSError as exc:
        raise LockError(
            f"cannot verify legacy stale-lock breaker state for {path}"
        ) from exc


def _write_lock_fd(fd: int, data: dict[str, object]) -> None:
    payload = (json.dumps(data, sort_keys=True, separators=(",", ":")) + "\n").encode(
        "utf-8"
    )
    os.ftruncate(fd, 0)
    os.lseek(fd, 0, os.SEEK_SET)
    view = memoryview(payload)
    while view:
        written = os.write(fd, view)
        if written <= 0:
            raise OSError("short stable lock record write")
        view = view[written:]
    os.fsync(fd)


def _read_lock_fd(fd: int) -> dict[str, object] | None:
    payload = _read_lock_fd_bytes(fd)
    if payload is None:
        return None
    return _decode_lock(payload)


def _read_lock_fd_bytes(fd: int) -> bytes | None:
    info = os.fstat(fd)
    if not stat.S_ISREG(info.st_mode) or info.st_size > _LOCK_RECORD_LIMIT:
        return None
    os.lseek(fd, 0, os.SEEK_SET)
    remaining = info.st_size
    chunks: list[bytes] = []
    while remaining:
        chunk = os.read(fd, remaining)
        if not chunk:
            return None
        chunks.append(chunk)
        remaining -= len(chunk)
    after = os.fstat(fd)
    if not os.path.samestat(info, after) or after.st_size != info.st_size:
        return None
    return b"".join(chunks)


def _decode_lock(payload: bytes) -> dict[str, object] | None:
    try:
        value = json.loads(payload)
    except (UnicodeDecodeError, json.JSONDecodeError):
        return None
    return value if isinstance(value, dict) else None


def _path_matches_fd(path: Path, fd: int) -> bool:
    try:
        path_info = path.lstat()
        descriptor_info = os.fstat(fd)
    except OSError:
        return False
    return stat.S_ISREG(path_info.st_mode) and os.path.samestat(
        path_info,
        descriptor_info,
    )


def _read_lock(path: Path) -> dict[str, object] | None:
    try:
        payload = path.read_bytes()
    except OSError:
        return None
    return _decode_lock(payload)


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
        owner_pid = (
            data.get("owner_pid")
            if data.get("protocol") == _LOCK_PROTOCOL
            else data.get("pid")
        )
        detail = f" owner_pid={owner_pid} created_at={data.get('created_at')}"
    return (
        f"another csk process holds the stable lock at {path};{detail} "
        "the persistent v1 lock file must not be removed"
    )


def _timeout_from_env() -> float:
    raw = os.environ.get("CSK_LOCK_TIMEOUT")
    if raw is None:
        return 30.0
    try:
        return max(0.0, float(raw))
    except ValueError:
        return 30.0
