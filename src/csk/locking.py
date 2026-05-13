from __future__ import annotations

import json
import os
import time
from pathlib import Path


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
                if time.monotonic() - start >= self.timeout:
                    raise LockError(_timeout_message(self.path)) from exc
                time.sleep(0.1)

    def __exit__(self, exc_type, exc, tb) -> None:
        if self.acquired:
            try:
                self.path.unlink()
            except FileNotFoundError:
                pass


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
