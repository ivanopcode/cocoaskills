from __future__ import annotations

import os
from pathlib import Path

import pytest
from sparse_files import truncate_sparse


def _allocated_bytes(path: Path) -> int:
    if os.name != "nt":
        return path.stat().st_blocks * 512
    import ctypes

    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    allocated = kernel.GetCompressedFileSizeW
    allocated.argtypes = [ctypes.c_wchar_p, ctypes.POINTER(ctypes.c_uint32)]
    allocated.restype = ctypes.c_uint32
    high = ctypes.c_uint32()
    low = allocated(str(path), ctypes.byref(high))
    if low == 0xFFFFFFFF and ctypes.get_last_error():
        raise ctypes.WinError(ctypes.get_last_error())
    return (high.value << 32) | low


def _assert_sparse(path: Path, size: int) -> None:
    assert path.stat().st_size == size
    assert _allocated_bytes(path) <= 64 * 1024, "fixture allocated its logical payload"


# Real filesystem control: large logical files keep zero-filled holes, and a
# bounded dense-file plant proves the allocation checker rejects a full payload.
# The 4-GiB case covers the Windows CRT extension path missed by small fixtures.
@pytest.mark.parametrize("size", [2 * 1024**2, 4 * 1024**3])
def test_sparse_fixture_allocation_and_dense_control(tmp_path: Path, size: int) -> None:
    path = tmp_path / "sparse"
    path.write_bytes(b"obsolete")
    truncate_sparse(path, size)
    _assert_sparse(path, size)
    with path.open("rb") as stream:
        assert stream.read(8) == b"\0" * 8
        stream.seek(size - 8)
        assert stream.read() == b"\0" * 8
    dense = tmp_path / "dense"
    dense_size = 2 * 1024**2
    dense.write_bytes(b"x" * dense_size)
    with pytest.raises(AssertionError, match="allocated its logical payload"):
        _assert_sparse(dense, dense_size)
