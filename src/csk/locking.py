from __future__ import annotations

import json
import os
import time
from pathlib import Path
from types import TracebackType


class LockError(Exception):
    pass


class GlobalLock:
    def __init__(self, csk_home: Path, timeout: float | None = None):
        self.path = csk_home / ".lock"
        self.timeout = _timeout_from_env() if timeout is None else timeout
        self.acquired = False

    def __enter__(self) -> "GlobalLock":
        self.path.parent.mkdir(parents=True, exist_ok=True)
        start = time.monotonic()
        while True:
            try:
                fd = os.open(str(self.path), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
                with os.fdopen(fd, "w", encoding="utf-8") as handle:
                    json.dump({"pid": os.getpid(), "created_at": time.time()}, handle)
                self.acquired = True
                return self
            except FileExistsError as exc:
                if self._break_stale_lock():
                    continue
                if time.monotonic() - start >= self.timeout:
                    raise LockError(_timeout_message(self.path)) from exc
                time.sleep(0.1)

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        if self.acquired:
            try:
                self.path.unlink()
            except FileNotFoundError:
                pass

    def _break_stale_lock(self) -> bool:
        """Remove the lock if its holder is provably dead.

        The break is done by renaming the lock to a unique name first: rename
        is atomic, so when several waiters race only one wins, and re-reading
        after the rename guards against stealing a lock that a new live
        process created in between.
        """
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return False
        pid = data.get("pid")
        if not isinstance(pid, int) or isinstance(pid, bool) or _pid_alive(pid):
            return False
        stale = self.path.with_name(f".lock.stale-{os.getpid()}")
        try:
            self.path.rename(stale)
        except OSError:
            return False
        try:
            current = json.loads(stale.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            current = None
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


def _pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    if os.name == "nt":
        import ctypes

        PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
        STILL_ACTIVE = 259
        kernel32 = ctypes.windll.kernel32  # type: ignore[attr-defined]
        handle = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
        if not handle:
            return False
        try:
            exit_code = ctypes.c_ulong()
            if kernel32.GetExitCodeProcess(handle, ctypes.byref(exit_code)):
                return exit_code.value == STILL_ACTIVE
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
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        pid = data.get("pid")
        created_at = data.get("created_at")
        detail = f" pid={pid} created_at={created_at}"
    except Exception:
        pass
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
