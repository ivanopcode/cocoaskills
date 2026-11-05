"""Bound captured backend output while draining pipes concurrently."""
from __future__ import annotations

import subprocess
import threading
import time
from pathlib import Path
from typing import BinaryIO

from .base import AuditBackendError

MAX_RESPONSE_BYTES = 16 * 1024 * 1024


def run(argv: list[str], *, input: bytes, cwd: Path, env: dict[str, str],
        timeout: float, **_: object) -> subprocess.CompletedProcess[bytes]:
    deadline = time.monotonic() + timeout
    proc = subprocess.Popen(argv, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                            stderr=subprocess.PIPE, cwd=cwd, env=env)
    stdin, stdout, stderr = proc.stdin, proc.stdout, proc.stderr
    assert stdin is not None and stdout is not None and stderr is not None
    output = [bytearray(), bytearray()]
    overflow = threading.Event()

    def drain(stream: BinaryIO, index: int, limit: int) -> None:
        try:
            while chunk := stream.read(8192):
                available = limit - len(output[index])
                output[index].extend(chunk[:max(0, available)])
                if index == 0 and len(chunk) > available:
                    overflow.set()
                    proc.kill()
        finally:
            stream.close()

    def feed() -> None:
        try:
            stdin.write(input)
            stdin.flush()
        except (BrokenPipeError, OSError):
            pass
        finally:
            try:
                stdin.close()
            except (BrokenPipeError, OSError):
                pass

    workers = [threading.Thread(target=drain, args=(stdout, 0, MAX_RESPONSE_BYTES), daemon=True),
               threading.Thread(target=drain, args=(stderr, 1, 2001), daemon=True),
               threading.Thread(target=feed, daemon=True)]
    for worker in workers:
        worker.start()
    try:
        proc.wait(timeout=max(0, deadline - time.monotonic()))
        for worker in workers:
            worker.join(timeout=max(0, deadline - time.monotonic()))
        if any(worker.is_alive() for worker in workers):
            raise subprocess.TimeoutExpired(argv, timeout)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait()
        raise
    if overflow.is_set():
        raise AuditBackendError("Audit backend response exceeds output byte limit")
    return subprocess.CompletedProcess(argv, proc.returncode, bytes(output[0]), bytes(output[1]))
