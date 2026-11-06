from __future__ import annotations

import builtins
import io
import json
import os
import plistlib
import ssl
import tracemalloc
import urllib.request
from pathlib import Path

import pytest

from csk import (
    _dispatch_runtime as runtime, audit_registry, git_admission, git_trust,
    mcp_launchers, mcp_ownership, protocol_json, transactions,
)
from csk.sources import store


# Every converted reader caps requests for tiny files, drains valid growth to
# its real boundary, and rejects one byte more after descriptor admission.
# HTTP uses a file-backed response; this checks reading, not network transport.
@pytest.mark.parametrize("reader", [
    "json", "journal", "runtime", "snapshot", "ownership", "pem", "git", "http", "plist",
])
@pytest.mark.parametrize("growth", ["none", "boundary", "oversize"])
def test_bounded_read_allocation_and_growth(tmp_path: Path, monkeypatch, request, reader: str, growth: str) -> None:
    path = tmp_path / "record.json"
    initial = b"{}"
    limit = protocol_json.MAX_INPUT_BYTES
    digest = "sha256:" + "a" * 64
    if reader == "snapshot":
        initial = json.dumps({
            "schema": store.RECORD_SCHEMA, "schema_version": store.RECORD_VERSION,
            "skill": "sample", "package": "local:sample", "snapshot": digest,
            "inventory": {"snapshot": digest, "files": []},
        }).encode()
    elif reader == "ownership":
        consumer = "p-" + "0" * 40
        path = tmp_path / "state/mcp/ledger" / f"{consumer}.json"
        limit = mcp_ownership._STORE_LIMIT
    elif reader == "pem":
        path = request.getfixturevalue("git_ca_bundle")
        initial = path.read_bytes()
        limit = git_trust.MAX_BUNDLE_BYTES
    elif reader == "git":
        limit = 8 * 1024 * 1024  # Actual packed-refs budget; 2 GiB is covered below.
    elif reader == "plist":
        path = tmp_path / "Reader.app/Contents/Info.plist"
        initial = plistlib.dumps({"CFBundleIdentifier": "com.example.reader"})
        limit = 1024 * 1024
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(initial)
    original_files = {p for p in tmp_path.rglob("*") if p.is_file()}
    expected = initial
    if growth != "none":
        expected += b" " * (limit - len(initial) + (growth == "oversize"))
    real_open, real_fdopen, real_builtin_open = io.open, os.fdopen, builtins.open
    requests: list[int] = []
    received: list[bytes] = []

    class ObservedStream:
        status = 200
        headers = {"Content-Type": "application/json"}

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
                if growth != "none":
                    with real_open(path, "ab") as writer:
                        writer.write(expected[len(initial):])
            chunk = self.stream.read(size)
            received.append(chunk)
            return chunk

    # The observer must reject the original allocation, independently of parsing.
    with real_open(path, "rb") as stream:
        with pytest.raises(AssertionError, match="full admission budget"):
            ObservedStream(stream).read(limit + 1)
    if reader == "json":
        monkeypatch.setattr(io, "open", lambda *a, **kw: ObservedStream(real_open(*a, **kw)))
    elif reader == "snapshot":
        monkeypatch.setattr(builtins, "open", lambda *a, **kw: ObservedStream(real_builtin_open(*a, **kw)))
    else:
        monkeypatch.setattr(os, "fdopen", lambda *a, **kw: ObservedStream(real_fdopen(*a, **kw)))
    if reader == "json":
        read = lambda: protocol_json.read_bytes(path)
        error, diagnostic = protocol_json.ProtocolJSONSizeError, "maximum size"
    elif reader == "journal":
        read = lambda: transactions._read_bounded_regular(path, limit=limit)
        error, diagnostic = transactions.TransactionCorruptionError, "size limit"
    elif reader == "runtime":
        read = lambda: runtime._read_no_follow(str(path), manager_home=str(tmp_path), what="test")
        error, diagnostic = runtime.DispatchError, "maximum size"
    elif reader == "snapshot":
        read = lambda: store._read_record(tmp_path, "sample", "local:sample")
        error, diagnostic = store._StoreUnavailable, "maximum size"
    elif reader == "ownership":
        read = lambda: mcp_ownership._read_store(tmp_path, "ledger", consumer)
        error, diagnostic = mcp_ownership.McpOwnershipError, "store exceeds the size limit"
    elif reader == "pem":
        read = lambda: git_trust._check_bundle(path)
        error, diagnostic = git_trust.GitTrustError, "PEM bundle exceeds size limit"
    elif reader == "git":
        read = lambda: git_admission._read_proved_file(path, limit)[0]
        error, diagnostic = git_admission.GitAdmissionError, "repository file exceeds bound"
    elif reader == "http":
        monkeypatch.setattr(audit_registry, "_open_registry_request", lambda *a, **kw: ObservedStream(real_open(path, "rb")))
        read = lambda: audit_registry._request_json(urllib.request.Request("https://registry.example/v1/records"), timeout=5, label="test")
        error, diagnostic = audit_registry.RegistryError, "response exceeds 16 MiB"
    else:
        spec = mcp_launchers.MacOSLauncher("com.example.reader", "TEAM", "Contents/MacOS/reader")
        read = lambda: mcp_launchers._find_bundle(spec, [tmp_path])
        error, diagnostic = mcp_launchers.McpLauncherError, "no application has the declared bundle_id"
    if growth == "oversize":
        with pytest.raises(error, match=diagnostic):
            read()
    else:
        result = read()
        if reader == "snapshot":
            assert result.snapshot == digest and result.files == {}
        elif reader == "pem":
            assert ssl.PEM_cert_to_DER_cert(result.decode()) == ssl.PEM_cert_to_DER_cert(initial.decode())
        elif reader == "http":
            assert result == {}
        elif reader == "plist":
            assert result == (tmp_path / "Reader.app").resolve()
        else:
            assert result == expected
    assert requests
    assert b"".join(received) == expected
    assert path.stat().st_size == len(expected)
    assert {p for p in tmp_path.rglob("*") if p.is_file()} == original_files


# The real Git file reader must not allocate its 2-GiB pack/idx allowance for
# a tiny file. This checks actual peak Python allocations, not only requests;
# full 2-GiB content is not materialized here (boundary logic is tested above).
def test_git_large_budget_tiny_file_has_bounded_allocation(tmp_path: Path) -> None:
    path = tmp_path / "pack.idx"
    path.write_bytes(b"tiny pack index")
    tracemalloc.start()
    try:
        data, proof = git_admission._read_proved_file(path, 2 * 1024**3)
        _, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()
    assert data == b"tiny pack index" and proof.size == len(data)
    assert peak < 512 * 1024, f"tiny file allocated {peak} bytes for a 2-GiB budget"
