from __future__ import annotations

import io
import os
from pathlib import Path

import pytest

from csk import _dispatch_runtime as runtime, protocol_json


# Short reads must drain exactly through EOF or the one-byte overrun witness;
# helper return values do not themselves imply caller admission.
@pytest.mark.parametrize("maximum", [0, 1, 10, 1024])
@pytest.mark.parametrize("extra", [-1, 0, 1, 5])
def test_bounded_helper_short_reads(maximum: int, extra: int) -> None:
    body = b"x" * max(0, maximum + extra)

    class Short(io.BytesIO):
        def read(self, size=-1):
            assert 0 < size <= 64 * 1024
            return super().read(min(3, size))

    assert protocol_json._read_bounded(Short(body), maximum) == body[:maximum + 1]


# Invalid budgets are rejected before touching a stream; zero remains valid.
@pytest.mark.parametrize("maximum", [-1, -5])
def test_bounded_helper_rejects_negative_budget(maximum: int) -> None:
    class Unreadable:
        def read(self, size):
            pytest.fail("invalid budget touched the stream")

    with pytest.raises(ValueError, match="read budget must be non-negative"):
        protocol_json._read_bounded(Unreadable(), maximum)
    assert protocol_json._read_bounded(io.BytesIO(), 0) == b""


# A noncompliant stream must fail on its first over-return, without a negative
# read or accumulating unbounded content. A real stream remains accepted.
@pytest.mark.parametrize("reader", ["shared", "runtime"])
def test_bounded_helper_rejects_over_return(tmp_path: Path, monkeypatch, reader: str) -> None:
    calls = []
    real_fdopen = os.fdopen

    class OverReturn(io.BytesIO):
        def __init__(self, stream=None):
            super().__init__()
            self.stream = stream

        def fileno(self):
            return self.stream.fileno()

        def __exit__(self, *args):
            if self.stream is not None:
                self.stream.close()
            return super().__exit__(*args)

        def read(self, size=-1):
            calls.append(size)
            assert size > 0 and len(calls) == 1
            return b"x" * (size + 1)

    if reader == "shared":
        read = lambda: protocol_json._read_bounded(OverReturn(), 10)
        error = OSError
        assert protocol_json._read_bounded(io.BytesIO(b"ok"), 10) == b"ok"
    else:
        path = tmp_path / "record.json"
        path.write_bytes(b"ok")
        assert runtime._read_no_follow(str(path), manager_home=str(tmp_path), what="test") == b"ok"
        # Retain descriptor ownership so the rejected read cannot leak a handle.
        def bad_fdopen(*args, **kwargs):
            return OverReturn(real_fdopen(*args, **kwargs))
        monkeypatch.setattr(os, "fdopen", bad_fdopen)
        read = lambda: runtime._read_no_follow(str(path), manager_home=str(tmp_path), what="test")
        error = runtime.DispatchError
    with pytest.raises(error, match="bounded read exceeded requested size"):
        read()
    assert len(calls) == 1
