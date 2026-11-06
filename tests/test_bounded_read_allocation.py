from __future__ import annotations

import io
import os
from pathlib import Path

import pytest

from csk import _dispatch_runtime as runtime, protocol_json, transactions


# All three real descriptor readers bound individual allocations for a tiny
# file and still read growth through EOF or reject the exact size violation.
# This observes requested buffer sizes, not allocator internals or wall time.
@pytest.mark.parametrize("reader", ["json", "journal", "runtime"])
@pytest.mark.parametrize("growth", ["none", "boundary", "oversize"])
def test_bounded_read_allocation_and_growth(tmp_path: Path, monkeypatch, reader: str, growth: str) -> None:
    path = tmp_path / "record.json"
    path.write_bytes(b"{}")
    limit = protocol_json.MAX_INPUT_BYTES
    expected = b"{}"
    if growth != "none":
        expected += b" " * (limit - 2 + (growth == "oversize"))
    real_open, real_fdopen = io.open, os.fdopen
    requests: list[int] = []

    class ObservedStream:
        def __init__(self, stream):
            self.stream = stream
            self.first = True

        def __enter__(self):
            self.stream.__enter__()
            return self

        def __exit__(self, *args):
            return self.stream.__exit__(*args)

        def fileno(self):
            return self.stream.fileno()

        def read(self, size):
            assert 0 < size <= 64 * 1024, "read allocated the full admission budget"
            requests.append(size)
            if self.first:
                self.first = False
                # Grow after descriptor admission, before reading its content.
                if growth != "none":
                    with real_open(path, "ab") as writer:
                        writer.write(expected[2:])
            return self.stream.read(size)

    # A bounded plant proves the buffer observer catches the old read request.
    with real_open(path, "rb") as stream:
        with pytest.raises(AssertionError, match="full admission budget"):
            ObservedStream(stream).read(limit + 1)
    if reader == "json":
        monkeypatch.setattr(io, "open", lambda *a, **kw: ObservedStream(real_open(*a, **kw)))
    else:
        monkeypatch.setattr(os, "fdopen", lambda *a, **kw: ObservedStream(real_fdopen(*a, **kw)))
    if reader == "json":
        read = lambda: protocol_json.read_bytes(path)
        error, diagnostic = protocol_json.ProtocolJSONSizeError, "maximum size"
    elif reader == "journal":
        read = lambda: transactions._read_bounded_regular(path, limit=limit)
        error, diagnostic = transactions.TransactionCorruptionError, "size limit"
    else:
        read = lambda: runtime._read_no_follow(str(path), manager_home=str(tmp_path), what="test")
        error, diagnostic = runtime.DispatchError, "maximum size"
    if growth == "oversize":
        with pytest.raises(error, match=diagnostic):
            read()
    else:
        assert read() == expected
    assert requests
    assert path.stat().st_size == len(expected)
    assert list(tmp_path.iterdir()) == [path]
