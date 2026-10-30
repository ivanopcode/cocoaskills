"""Applied controls for test-local observation; no native platform qualification."""
from __future__ import annotations

import errno
import hashlib
import json
import os
import stat
from pathlib import Path

import pytest

from csk import transactions as tx
from csk.locking import ManagerHomeLock
import publication_capture as capture_module
from publication_capture import MAX_BYTES, PublicationCapture, exception_summary


@pytest.fixture(autouse=True)
def private_environment(tmp_path, monkeypatch):
    home = tmp_path / "private-home"
    home.mkdir()
    for name, tail in (("HOME", ""), ("USERPROFILE", ""), ("XDG_CONFIG_HOME", "config"),
                       ("XDG_CACHE_HOME", "cache"), ("XDG_DATA_HOME", "data"),
                       ("APPDATA", "appdata"), ("LOCALAPPDATA", "localappdata"),
                       ("CSK_HOME", "manager"), ("CSK_CONFIG", "manager/config.json")):
        monkeypatch.setenv(name, str(home / tail))
    temporary = tmp_path / "temporary"
    temporary.mkdir()
    for name in ("TMPDIR", "TEMP", "TMP"):
        monkeypatch.setenv(name, str(temporary))


def state(path):
    info = path.lstat()
    return path.read_bytes(), info.st_mode, info.st_mtime_ns


def plan_fixture(tmp_path):
    project, source = tmp_path / "project", tmp_path / "source"
    project.mkdir()
    source.mkdir()
    live, wanted = project / "data", source / "data"
    live.write_bytes(b"old")
    live.chmod(0o640)
    wanted.write_bytes(b"new")
    wanted.chmod(0o600)
    canary = tmp_path / "outside-canary"
    canary.write_bytes(b"never inspect the link referent")
    canary.chmod(0o640)
    alias = source / "alias"
    alias.symlink_to(canary)
    plan = tx.TransactionPlan("capture-control", str(project), (
        tx.MutableTarget("10-context", "bytes", live, wanted, tx.digest_path(live)),
        tx.MutableTarget("60-adapter-ledger", "alias", project / "alias", alias,
                         tx.ABSENT_DIGEST, kind="entry"),
    ))
    home = tmp_path / "manager"
    capture = PublicationCapture(tmp_path / "publication-capture", roots=(home, project), output_anchor=tmp_path)
    return home, project, canary, plan, capture


def counted(engine, capture, calls):
    prepare, discard = engine.prepare, engine._discard_prepared

    def preparing(*args, **kwargs):
        calls.append(("prepare", args, kwargs))
        result = prepare(*args, **kwargs)
        calls.append(("result", result))
        return result

    def discarding(*args, **kwargs):
        calls.append(("discard", args, kwargs))
        return discard(*args, **kwargs)

    engine.prepare, engine._discard_prepared = preparing, discarding
    return capture.observe(engine)


# Actual byte/link staging reaches the planted EIO. The observer snapshots before
# cleanup, delegates exactly once, preserves both error edges, and never reads a
# symlink referent. A separate remove-journal EACCES retains recoverable sidecars.
@pytest.mark.skipif(os.name != "posix", reason="raw private journal/CLI persistence requires POSIX modes; unsupported-output transaction controls run on Windows")
@pytest.mark.parametrize("obstruct", [False, True])
def test_prepare_identity_snapshot_before_cleanup_and_recovery(tmp_path, monkeypatch, obstruct):
    shared = (os.open, os.scandir, Path.open, tx.TransactionEngine.prepare)
    home, project, canary, plan, capture = plan_fixture(tmp_path)
    before, outside = state(project / "data"), state(canary)
    first = OSError(errno.EIO, "private exception text")
    cause = ValueError("private cause text")
    first.__cause__ = cause
    second = PermissionError(errno.EACCES, "private cleanup text")
    events, calls, removals = [], [], []

    def fault(event, target):
        events.append((event, target))
        if event == "staging_entry_completed" and target.identifier == "alias":
            raise first

    engine = tx.TransactionEngine(home, fault_hook=fault)
    original_remove = engine._remove_journal

    def remove(journal):
        removals.append(journal)
        # Snapshot and exact private bytes already exist at the destructive seam.
        assert capture.rows[-1]["snapshot"] == 1
        raw = engine._journal_path(plan.transaction_id).read_bytes()
        assert (capture.directory / "journal-001.private").read_bytes() == raw
        assert hashlib.sha256(raw).hexdigest() == capture.rows[-1]["journal_sha256"]
        assert len(list(project.glob(".csk-txn-*"))) == 2
        if obstruct:
            raise second
        return original_remove(journal)

    monkeypatch.setattr(engine, "_remove_journal", remove)
    counted(engine, capture, calls)
    with ManagerHomeLock(home) as lock:
        with pytest.raises(OSError) as caught:
            engine.prepare(lock, plan)
        expected = second if obstruct else first
        assert caught.value is expected
        assert first.__cause__ is cause and first.__context__ is None
        assert second.__context__ is first if obstruct else second.__context__ is None
        assert [call[0] for call in calls] == ["prepare", "discard"]
        assert calls[0][1] == (lock, plan) and calls[0][2] == {}
        assert calls[1][1] == (removals[0],) and calls[1][2] == {}
        assert len(removals) == 1
        observed = [row for row in capture.rows if "event" in row]
        assert len(observed) == len(events)  # Original hook called once per emit.
        assert sum(event == "staging_entry_completed" and target.identifier == "alias"
                   for event, target in events) == 1
        snapshot = next(row for row in capture.rows if "snapshot" in row)
        assert snapshot["phase"] == "preparing"
        assert all(row["state"] == "pending" for row in snapshot["progress"])
        assert any(row["kind"] == "link" for row in snapshot["sidecars"])
        assert any(row.get("sha256") == hashlib.sha256(b"new").hexdigest()
                   for row in snapshot["sidecars"])
        outward = next(row for row in capture.rows if row.get("call") == "prepare_error")
        assert outward["exception"] == exception_summary(expected)
        assert capture.rows.index(snapshot) < capture.rows.index(outward)
        assert state(project / "data") == before and not (project / "alias").exists()
        pending = engine.pending_failure(lock, plan.transaction_id, expected)
        if obstruct:
            assert pending.code == "transaction_preparation_cleanup_deferred"
            assert len(list(project.glob(".csk-txn-*"))) == 2
            tx.TransactionEngine(home).recover(lock)
        else:
            assert pending is None
        assert not list(project.glob(".csk-txn-*"))
        assert not list(engine.journal_root.glob("*.json"))
        # Unchanged product success, including the exact return object.
        positive_calls = []
        positive = counted(tx.TransactionEngine(home), capture, positive_calls)
        result = positive.prepare(lock, plan)
        assert positive_calls[0][1] == (lock, plan)
        assert positive_calls[-1] == ("result", result) and positive_calls[-1][1] is result
        positive.commit(lock, plan.transaction_id)
    assert (project / "data").read_bytes() == b"new"
    assert stat.S_IMODE((project / "data").stat().st_mode) == 0o600
    assert (project / "alias").is_symlink()
    assert state(canary) == outside
    capture.cli(1, "full private stdout\n" * 2000, "full private stderr\n" * 2000)
    assert capture.complete and not capture.errors
    assert (capture.directory / "cli-001.stdout.private").read_bytes() == ("full private stdout\n" * 2000).encode()
    assert (capture.directory / "cli-001.stderr.private").read_bytes() == ("full private stderr\n" * 2000).encode()
    portable = b"".join(path.read_bytes() for path in capture.directory.glob("summary-*.json"))
    assert b"private exception" not in portable and str(tmp_path).encode() not in portable
    assert b"never inspect" not in portable and b"private stdout" not in portable
    assert shared == (os.open, os.scandir, Path.open, tx.TransactionEngine.prepare)


# A recorder write failure cannot replace staging/cleanup errors, nor turn a
# successful prepare/commit into evidence claimed complete. No retry is used.
@pytest.mark.parametrize("product_fails", [False, True])
def test_recorder_failure_preserves_product_result_and_marks_missing(tmp_path, monkeypatch, product_fails):
    home, project, canary, plan, capture = plan_fixture(tmp_path)
    outside = state(canary)
    failure = OSError(errno.EIO, "original product refusal")
    recorder = RuntimeError("unavailable diagnostic sink")
    writes, calls = [], []

    def fail_write(*args):
        writes.append(args[0])
        raise recorder

    monkeypatch.setattr(capture, "_write", fail_write)

    def fault(event, target):
        if product_fails and event == "prepared":
            raise failure

    engine = counted(tx.TransactionEngine(home, fault_hook=fault), capture, calls)
    with ManagerHomeLock(home) as lock:
        if product_fails:
            with pytest.raises(OSError) as caught:
                engine.prepare(lock, plan)
            assert caught.value is failure and failure.__context__ is None
            assert (project / "data").read_bytes() == b"old"
            assert [call[0] for call in calls] == ["prepare", "discard"]
        else:
            result = engine.prepare(lock, plan)
            assert calls[-1][1] is result
            engine.commit(lock, plan.transaction_id)
            assert (project / "data").read_bytes() == b"new"
    capture.cli(1 if product_fails else 0, "complete output", "")
    assert writes and capture.errors and not capture.complete
    assert not capture.directory.exists()
    assert not list(project.glob(".csk-txn-*"))
    assert not list(engine.journal_root.glob("*.json"))
    assert state(canary) == outside


# Unowned ancestors/links are never traversed; oversized sidecar bytes are marked
# incomplete, and the bounded exception graph contains no arbitrary class/text.
def test_bounded_capture_and_exception_graph(tmp_path, monkeypatch):
    home, project, canary, plan, capture = plan_fixture(tmp_path)
    outside = state(canary)
    reads = []
    original = capture_module._read_regular

    def read(path, **kwargs):
        reads.append(path)
        return original(path, **kwargs)

    monkeypatch.setattr(capture_module, "_read_regular", read)
    entries = []
    link = project / "link"
    link.symlink_to(tmp_path, target_is_directory=True)
    capture._entry(link, entries)
    assert entries == [{"mode": stat.S_IMODE(link.lstat().st_mode), "kind": "link"}]
    with pytest.raises(ValueError, match="non-directory capture ancestor"):
        capture._entry(link / "outside-canary", [])
    with pytest.raises(ValueError, match="outside fixture roots"):
        capture._entry(canary, [])
    large = project / "large"
    large.write_bytes(b"x" * (MAX_BYTES + 1))
    entries = []
    capture._entry(large, entries)
    assert entries[0]["status"] == "truncated" and "sha256" not in entries[0]
    errors = [ValueError("secret") for _ in range(10)]
    for current, following in zip(errors, errors[1:]):
        current.__context__ = following
    errors[-1].__cause__ = errors[0]
    summary = exception_summary(errors[0])
    assert len(summary) == 8 and summary[-1]["context"] == "truncated"
    assert "secret" not in json.dumps(summary)
    assert reads == []  # No referent/oversized/outside file was opened.
    assert state(canary) == outside


# Applied omission proves the checker requires same-attempt journal bytes rather
# than accepting a correct refusal alone. The adjacent unmodified sink succeeds.
@pytest.mark.skipif(os.name != "posix", reason="per-attempt private-file persistence requires POSIX modes; Windows explicitly refuses output")
def test_missing_snapshot_control_and_per_attempt_manifests(tmp_path, monkeypatch):
    home, project, canary, plan, capture = plan_fixture(tmp_path)
    before, outside = state(project / "data"), state(canary)
    capture.directory = tmp_path / "omission-control" / "publication-capture"
    applied = []

    def omitted(*args):
        applied.append(True)

    def refuse(point, target):
        if point == "prepared":
            raise OSError(errno.EIO, "known preparation refusal")

    with ManagerHomeLock(home) as lock:
        with monkeypatch.context() as patch:
            patch.setattr(capture, "_snapshot", omitted)
            engine = capture.observe(tx.TransactionEngine(home, fault_hook=refuse))
            with pytest.raises(OSError, match="known preparation refusal"):
                engine.prepare(lock, plan)
        assert applied == [True]
        with pytest.raises(AssertionError, match="snapshot omitted"):
            assert (capture.directory / "journal-001.private").exists(), "snapshot omitted"
        assert not capture.complete
        capture = PublicationCapture(tmp_path / "publication-capture", roots=(home, project), output_anchor=tmp_path)
        engine = capture.observe(tx.TransactionEngine(home, fault_hook=refuse))
        with pytest.raises(OSError, match="known preparation refusal"):
            engine.prepare(lock, plan)
        capture.cli(1, "first output", "")
        engine = capture.observe(tx.TransactionEngine(home, fault_hook=refuse))
        with pytest.raises(OSError, match="known preparation refusal"):
            engine.prepare(lock, plan)
        capture.cli(1, "second output", "")
    assert capture.complete
    assert (capture.directory / "journal-001.private").is_file()
    assert (capture.directory / "cli-001.stdout.private").read_bytes() == b"first output"
    assert (capture.directory / "cli-002.stdout.private").read_bytes() == b"second output"
    manifests = [json.loads(path.read_bytes()) for path in sorted(capture.directory.glob("summary-*.json"))]
    assert len([row for manifest in manifests for row in manifest["rows"] if row.get("cli") == 1]) == 1
    assert manifests[-1]["rows"][-1]["cli"] == 2
    assert state(project / "data") == before and state(canary) == outside


# The table uses a module-local tempfile view: cached global tempfile state and
# shared stream objects stay untouched, and source workspaces stay test-private.
def test_publication_attachment_fixture_is_local(tmp_path, monkeypatch):
    from types import SimpleNamespace
    import tempfile
    from csk import cli, mcp_project
    from csk.sources import publish
    from test_mcp_launcher_publication import _approve_publication, _publication_capture

    case = SimpleNamespace(project=tmp_path / "project", home=tmp_path / "manager")
    for path in (case.project, case.home):
        path.mkdir()
    import sys
    shared = (sys.stdin.isatty, sys.stdout.isatty, tempfile.TemporaryDirectory)
    _approve_publication(monkeypatch)
    capture = _publication_capture(case, tmp_path, monkeypatch)
    with publish.tempfile.TemporaryDirectory() as workspace:
        assert Path(workspace).parent == tmp_path / "publication-temporary"
    assert not Path(workspace).exists()
    assert cli.sys.stdin.isatty() and mcp_project.input("prompt") == "yes"
    assert shared == (sys.stdin.isatty, sys.stdout.isatty, tempfile.TemporaryDirectory)
    assert capture.directory == tmp_path / "publication-capture"


def outside_state(directory):
    info = directory.lstat()
    return (info.st_mode, info.st_mtime_ns, sorted(path.name for path in directory.iterdir()),
            state(directory / "data"))


# The matched input root must still be a directory at observation time. Replaced
# root links/files and missing roots cannot reach the reader or complete evidence.
# An absent leaf under a normal root remains a legitimate absent observation.
@pytest.mark.parametrize("replacement", ["link", "file", "missing"])
def test_stored_input_root_is_checked_before_reader(tmp_path, monkeypatch, replacement):
    root, outside = tmp_path / "root", tmp_path / "outside"
    root.mkdir()
    outside.mkdir()
    (outside / "data").write_bytes(b"outside input canary")
    before = outside_state(outside)
    capture = PublicationCapture(tmp_path / "publication-capture", roots=(root,), output_anchor=tmp_path)
    root.rename(tmp_path / "old-root")
    if replacement == "link":
        root.symlink_to(outside, target_is_directory=True)
    elif replacement == "file":
        root.write_bytes(b"not a directory")
    reads = []
    reader = capture_module._read_regular

    def read(path, **kwargs):
        reads.append(path)
        return reader(path, **kwargs)

    monkeypatch.setattr(capture_module, "_read_regular", read)
    capture._safe(lambda: capture._entry(root / "data", []))
    capture.cli(1, "refused observation", "")
    expected = "FileNotFoundError" if replacement == "missing" else "ValueError"
    assert capture.errors[0][0]["type"] == expected
    assert reads == [] and not capture.complete
    assert outside_state(outside) == before
    ordinary = PublicationCapture(tmp_path / "normal-output", roots=(outside,), output_anchor=tmp_path)
    entries = []
    ordinary._entry(outside / "data", entries)
    ordinary._entry(outside / "absent", entries)
    assert reads == [outside / "data"]
    assert entries[0]["sha256"] == hashlib.sha256(b"outside input canary").hexdigest()
    assert entries[1] == {"kind": "absent"}
    ordinary.cli(0, "normal", "")
    if os.name == "posix":
        assert ordinary.complete
    else:
        assert not ordinary.complete and ordinary.errors
        assert not ordinary.directory.exists()
    assert outside_state(outside) == before


# Existing output ancestors, the output leaf and the trusted anchor itself must
# be checked before any creation/write. Refusal preserves sibling bytes/modes/
# mtimes/listing; an adjacent nested output gets private directories and files.
@pytest.mark.parametrize("linked", ["ancestor", "leaf", "anchor"])
def test_output_ancestry_precedes_creation(tmp_path, linked):
    root, outside = tmp_path / "root", tmp_path / "outside"
    root.mkdir()
    outside.mkdir()
    (outside / "data").write_bytes(b"outside output canary")
    before = outside_state(outside)
    anchor = root
    if linked == "ancestor":
        (root / "linked").symlink_to(outside, target_is_directory=True)
        directory = root / "linked" / "nested" / "publication-capture"
    elif linked == "leaf":
        directory = root / "publication-capture"
        directory.symlink_to(outside, target_is_directory=True)
    else:
        root.rename(tmp_path / "old-root")
        root.symlink_to(outside, target_is_directory=True)
        directory = root / "publication-capture"
    capture = PublicationCapture(directory, roots=(tmp_path,), output_anchor=anchor)
    capture.cli(0, "private stdout", "private stderr")
    assert not capture.complete and capture.errors[0][0]["type"] == "ValueError"
    assert outside_state(outside) == before
    ordinary = PublicationCapture(tmp_path / "normal" / "nested" / "publication-capture",
                                  roots=(outside,), output_anchor=tmp_path)
    ordinary.cli(0, "complete stdout", "complete stderr")
    if os.name != "posix":
        assert not ordinary.complete and ordinary.errors[0][0]["type"] == "ValueError"
        assert not (tmp_path / "normal").exists()
        assert outside_state(outside) == before
        return  # Unsupported private persistence: no POSIX-mode/ACL claim.
    assert ordinary.complete and not ordinary.errors
    assert (ordinary.directory / "cli-001.stdout.private").read_bytes() == b"complete stdout"
    assert (ordinary.directory / "cli-001.stderr.private").read_bytes() == b"complete stderr"
    assert all(stat.S_IMODE(path.stat().st_mode) == 0o700 for path in (
        tmp_path / "normal", tmp_path / "normal/nested", ordinary.directory))
    assert all(stat.S_IMODE(path.stat().st_mode) == 0o600 for path in ordinary.directory.iterdir())
    assert outside_state(outside) == before


# A boundary refusal in observation cannot replace an actual transaction error
# (including its cause) or suppress unchanged prepare/commit success. The sink
# link is the applied fault; all product calls are still the original delegates.
@pytest.mark.parametrize("product_fails", [False, True])
def test_output_boundary_failure_preserves_product_identity(tmp_path, product_fails):
    home, project, canary, plan, capture = plan_fixture(tmp_path)
    outside = tmp_path / "outside-output"
    outside.mkdir()
    (outside / "data").write_bytes(b"outside canary")
    before = outside_state(outside)
    link = tmp_path / "linked-output"
    link.symlink_to(outside, target_is_directory=True)
    capture.directory = link / "publication-capture"
    failure = OSError(errno.EIO, "original refusal")
    cause = ValueError("original cause")
    failure.__cause__ = cause
    calls = []

    def fault(point, target):
        if product_fails and point == "prepared":
            raise failure

    engine = counted(tx.TransactionEngine(home, fault_hook=fault), capture, calls)
    with ManagerHomeLock(home) as lock:
        if product_fails:
            with pytest.raises(OSError) as caught:
                engine.prepare(lock, plan)
            assert caught.value is failure and failure.__cause__ is cause
            assert failure.__context__ is None
            assert [call[0] for call in calls] == ["prepare", "discard"]
            assert (project / "data").read_bytes() == b"old"
        else:
            result = engine.prepare(lock, plan)
            assert calls[-1][1] is result
            engine.commit(lock, plan.transaction_id)
            assert (project / "data").read_bytes() == b"new"
    capture.cli(1 if product_fails else 0, "complete output", "")
    assert not capture.complete and capture.errors
    assert outside_state(outside) == before
    assert not list(project.glob(".csk-txn-*"))
    assert not list(engine.journal_root.glob("*.json"))


# Model only the reparse metadata bit through a module-local classification seam;
# real lstat/read/write functions stay untouched. This is not native Windows proof.
@pytest.mark.parametrize("gate", ["input", "output"])
def test_reparse_directory_metadata_refused_before_io(tmp_path, monkeypatch, gate):
    from types import SimpleNamespace

    root = tmp_path / "root"
    root.mkdir()
    (root / "data").write_bytes(b"owned input")
    identity = root.stat().st_dev, root.stat().st_ino
    original = capture_module._link

    def reparse(info):
        if (info.st_dev, info.st_ino) == identity:
            return original(SimpleNamespace(st_mode=info.st_mode, st_file_attributes=0x400))
        return original(info)

    capture = PublicationCapture(root / "publication-capture", roots=(root,), output_anchor=root)
    monkeypatch.setattr(capture_module, "_link", reparse)
    if gate == "input":
        with pytest.raises(ValueError, match="non-directory capture root"):
            capture._entry(root / "data", [])
    capture.cli(0, "private output", "")
    assert not capture.complete and capture.errors[0][0]["type"] == "ValueError"
    assert sorted(path.name for path in root.iterdir()) == ["data"]


# The explicit anchor must exist and lexically contain the output. Missing
# anchors are not created, and outside/parent-component destinations are refused.
@pytest.mark.parametrize("invalid", ["missing", "outside", "parent"])
def test_output_anchor_contract_refuses_before_creation(tmp_path, invalid):
    root, outside = tmp_path / "root", tmp_path / "outside"
    root.mkdir()
    outside.mkdir()
    (outside / "data").write_bytes(b"outside canary")
    before = outside_state(outside)
    anchor = root
    if invalid == "missing":
        anchor = tmp_path / "missing-anchor"
        directory = anchor / "publication-capture"
    elif invalid == "outside":
        directory = outside / "publication-capture"
    else:
        directory = root / ".." / "outside" / "publication-capture"
    capture = PublicationCapture(directory, roots=(root,), output_anchor=anchor)
    capture.cli(0, "private output", "")
    assert not capture.complete and capture.errors
    assert not (tmp_path / "missing-anchor").exists()
    assert outside_state(outside) == before


# Mode bits cannot admit private raw capture on Windows. This module-local
# platform view models refusal, not Windows ACL support; product I/O is real.
@pytest.mark.parametrize("outcome", ["success", "prepare-error", "cleanup-error"])
def test_unsupported_private_output_preserves_transaction(tmp_path, monkeypatch, outcome):
    from types import SimpleNamespace

    home, project, canary, plan, capture = plan_fixture(tmp_path)
    before, outside = state(project / "data"), state(canary)
    failure = OSError(errno.EIO, "private original error")
    cause, context = ValueError("cause"), LookupError("context")
    failure.__cause__, failure.__context__ = cause, context
    cleanup = PermissionError(errno.EACCES, "private cleanup obstruction")
    calls, applied, removals = [], [], []

    def fault(point, target):
        if outcome != "success" and point == "staging_entry_completed" and target.identifier == "alias":
            applied.append(True)
            raise failure

    monkeypatch.setattr(capture_module, "os", SimpleNamespace(**{**vars(os), "name": "nt"}))
    engine = tx.TransactionEngine(home, fault_hook=fault)
    if outcome == "cleanup-error":
        def obstruct(journal):
            removals.append(journal)
            raise cleanup
        monkeypatch.setattr(engine, "_remove_journal", obstruct)
    counted(engine, capture, calls)
    with ManagerHomeLock(home) as lock:
        if outcome != "success":
            with pytest.raises(OSError) as caught:
                engine.prepare(lock, plan)
            expected = cleanup if outcome == "cleanup-error" else failure
            assert caught.value is expected
            assert failure.__cause__ is cause and failure.__context__ is context
            assert applied == [True] and [row[0] for row in calls] == ["prepare", "discard"]
            assert len(calls[1][1]) == 1 and calls[1][2] == {}
            assert state(project / "data") == before and not (project / "alias").exists()
            pending = engine.pending_failure(lock, plan.transaction_id, expected)
            if outcome == "cleanup-error":
                assert cleanup.__context__ is failure and cleanup.__cause__ is None
                assert removals == [calls[1][1][0]]
                assert pending.code == "transaction_preparation_cleanup_deferred"
                assert len(list(project.glob(".csk-txn-*"))) == 2
                tx.TransactionEngine(home).recover(lock)
            else:
                assert pending is None and removals == []
        else:
            result = engine.prepare(lock, plan)
            assert calls[-1][1] is result and applied == []
            assert [row[0] for row in calls] == ["prepare", "result"]
            engine.commit(lock, plan.transaction_id)
            assert (project / "data").read_bytes() == b"new"
            assert (project / "alias").is_symlink()
        assert calls[0][1] == (lock, plan) and calls[0][2] == {}
    capture.cli(0 if outcome == "success" else 1, "private raw output", "private stderr")
    assert capture.errors and not capture.complete
    assert capture.errors[0][0]["type"] == "ValueError"
    assert not capture.directory.exists()
    assert not list(project.glob(".csk-txn-*"))
    assert not list(engine.journal_root.glob("*.json"))
    assert state(canary) == outside


# An existing raw-output filename cannot be overwritten or followed. On Windows
# the unsupported private sink refuses even earlier; the outside referent and
# the final link remain unchanged on both paths.
def test_final_output_link_is_not_overwritten(tmp_path):
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "data").write_bytes(b"outside final-file canary")
    before = outside_state(outside)
    directory = tmp_path / "publication-capture"
    directory.mkdir(mode=0o700)
    target = directory / "cli-001.stdout.private"
    target.symlink_to(outside / "data")
    link = target.lstat()
    capture = PublicationCapture(directory, roots=(tmp_path,), output_anchor=tmp_path)
    capture.cli(0, "never overwrite", "never create stderr")
    assert not capture.complete and capture.errors
    assert target.is_symlink() and target.lstat().st_ino == link.st_ino
    assert not (directory / "cli-001.stderr.private").exists()
    assert outside_state(outside) == before
