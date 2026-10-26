"""Portable controls for path/handle metadata differences and raced reads."""
from __future__ import annotations

import hashlib
import os
from pathlib import Path
from types import SimpleNamespace

import pytest

from csk import mcp_launchers, transactions


# Unchanged files tolerate path/handle timestamp differences. Actual size or
# same-size content/mtime changes and either identity change still refuse.
# Identity replacement is injected because Windows may forbid renaming an open file.
@pytest.mark.parametrize("reader", ["sidecar", "launcher"])
@pytest.mark.parametrize("change", [None, "size", "mtime", "handle-identity", "path-identity"])
def test_stable_read_metadata_sources(tmp_path, monkeypatch, reader, change):
    path = tmp_path / "payload"
    data = b"original"
    path.write_bytes(data)
    before = path.stat()
    real_fstat, real_stat, real_lstat = os.fstat, Path.stat, Path.lstat
    calls = 0

    def metadata(info, **changes):
        values = {name: getattr(info, name) for name in (
            "st_dev", "st_ino", "st_mode", "st_size", "st_mtime_ns", "st_ctime_ns")}
        return SimpleNamespace(**{**values, **changes})

    def fstat(fd):
        nonlocal calls
        calls += 1
        if calls == 2:
            if change == "size":
                with path.open("ab") as stream:
                    stream.write(b"extra")
            elif change == "mtime":
                path.write_bytes(b"modified")
                os.utime(path, ns=(before.st_atime_ns, before.st_mtime_ns + 2_000_000_000))
        info = real_fstat(fd)
        return metadata(info, st_ino=info.st_ino + (calls == 2 and change == "handle-identity"),
                        st_mtime_ns=info.st_mtime_ns + 1_000_000_000,
                        st_ctime_ns=info.st_ctime_ns + 3_000_000_000)

    def path_metadata(original):
        def inspect(candidate, *args, **kwargs):
            info = original(candidate, *args, **kwargs)
            if candidate == path and calls == 2 and change == "path-identity":
                return metadata(info, st_ino=info.st_ino + 1)
            return info
        return inspect

    monkeypatch.setattr(os, "fstat", fstat)
    monkeypatch.setattr(Path, "stat", path_metadata(real_stat))
    monkeypatch.setattr(Path, "lstat", path_metadata(real_lstat))
    if reader == "sidecar":
        target = transactions.MutableTarget(
            target_class="mcp-gitignore", identifier="exclude", live_path=tmp_path / "exclude",
            desired_path=path, expected_preimage_digest=transactions.ABSENT_DIGEST,
            publication_recheck={"kind": "git-common-exclude-block", "block_key": "0" * 64,
                                 "old_block": None, "file_existed": False, "original_sha256": "0" * 64},
        )
        read = lambda: transactions._target_digest(target, path)
        error, message = transactions.TransactionCorruptionError, "Shared block sidecar changed while reading"
        expected = transactions.shared_block_digest(data)
    else:
        read = lambda: mcp_launchers._digest_stable(path, before)
        error, message = mcp_launchers.McpLauncherError, "executable changed during verification"
        expected = hashlib.sha256(data).hexdigest()
    if change is None:
        assert read() == expected
        assert path.read_bytes() == data
    else:
        with pytest.raises(error, match=message):
            read()
    assert calls == 2
