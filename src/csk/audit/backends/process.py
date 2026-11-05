"""Bound captured backend output while draining pipes concurrently."""
from __future__ import annotations

import subprocess
import threading
import time

from .base import AuditBackendError

MAX_RESPONSE_BYTES = 16 * 1024 * 1024


def run(argv, *, input, cwd, env, timeout, **_):
    deadline = time.monotonic() + timeout
    proc = subprocess.Popen(argv, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                            stderr=subprocess.PIPE, cwd=cwd, env=env)
    output = [bytearray(), bytearray()]
    overflow = threading.Event()

    def drain(stream, index, limit):
        try:
            while chunk := stream.read(8192):
                available = limit - len(output[index])
                output[index].extend(chunk[:max(0, available)])
                if index == 0 and len(chunk) > available:
                    overflow.set()
                    proc.kill()
        finally:
            stream.close()

    def feed():
        try:
            proc.stdin.write(input)
            proc.stdin.flush()
        except (BrokenPipeError, OSError):
            pass
        finally:
            proc.stdin.close()

    workers = [threading.Thread(target=drain, args=(proc.stdout, 0, MAX_RESPONSE_BYTES), daemon=True),
               threading.Thread(target=drain, args=(proc.stderr, 1, 2001), daemon=True),
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
