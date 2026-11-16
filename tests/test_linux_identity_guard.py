"""Linux adapter for the identity mutation guard; Linux admission stays closed."""

import ctypes
import errno
import mmap
import os
import struct
import sys
from collections.abc import Iterator
from contextlib import contextmanager, suppress
from pathlib import Path
from types import SimpleNamespace

import pytest

from csk import installer
from csk.builds import go_v1

_POSIX_ONLY = pytest.mark.skipif(
    os.name != "posix",
    reason="the fake inotify guard needs POSIX pipe and flag semantics",
)


def _close_quietly(guard: go_v1._IdentityMutationGuard) -> None:
    with suppress(go_v1.GoV1Error):
        guard.close()


def _event_bytes(wd: int, mask: int, name: str = "") -> bytes:
    raw = os.fsencode(name)
    padded = raw + b"\x00" * (16 - len(raw) % 16) if raw else b""
    return struct.pack("iIII", wd, mask, 0, len(padded)) + padded


class _FakeInotifyLibc:
    """Pipe-backed inotify stand-in so macOS can exercise the Linux logic."""

    def __init__(self, *, fail_init: int = 0, fail_watch: int = 0):
        self.fail_init = fail_init
        self.fail_watch = fail_watch
        self.read_fd, self.write_fd = os.pipe()
        os.set_blocking(self.read_fd, False)
        self.wd_by_path: dict[bytes, int] = {}
        self.masks: dict[int, int] = {}
        self.init_returned = False

    def inotify_init1(self, flags: int) -> int:
        if self.fail_init:
            ctypes.set_errno(self.fail_init)
            return -1
        assert flags & os.O_NONBLOCK
        assert flags & os.O_CLOEXEC
        self.init_returned = True
        return self.read_fd

    def inotify_add_watch(self, fd: int, path: bytes, mask: int) -> int:
        if self.fail_watch:
            ctypes.set_errno(self.fail_watch)
            return -1
        assert fd == self.read_fd
        wd = self.wd_by_path.setdefault(path, len(self.wd_by_path) + 1)
        self.masks[wd] = mask
        return wd

    def emit(self, path: Path, mask: int, name: str = "") -> None:
        wd = self.wd_by_path[os.fsencode(path)]
        os.write(self.write_fd, _event_bytes(wd, mask, name))

    def emit_raw(self, payload: bytes) -> None:
        os.write(self.write_fd, payload)

    def close(self) -> None:
        with suppress(OSError):
            os.close(self.write_fd)
        if not self.init_returned:
            # The guard owns (and closes) the read end once init returned it.
            with suppress(OSError):
                os.close(self.read_fd)


class _MergingFakeInotifyLibc(_FakeInotifyLibc):
    """Fake whose distinct paths can share one wd, as one inode does."""

    def __init__(
        self,
        forced_wds: dict[bytes, int],
        *,
        mismatch_on_readd: bool = False,
    ):
        super().__init__()
        self.forced_wds = forced_wds
        self.mismatch_on_readd = mismatch_on_readd
        self.add_counts: dict[bytes, int] = {}

    def inotify_add_watch(self, fd: int, path: bytes, mask: int) -> int:
        self.add_counts[path] = self.add_counts.get(path, 0) + 1
        if self.mismatch_on_readd and self.add_counts[path] > 1:
            return 999
        if path in self.forced_wds:
            assert fd == self.read_fd
            wd = self.forced_wds[path]
            self.wd_by_path[path] = wd
            self.masks[wd] = mask
            return wd
        return super().inotify_add_watch(fd, path, mask)


def _fake_guard(
    monkeypatch: pytest.MonkeyPatch,
    paths: tuple[Path, ...],
    fake: _FakeInotifyLibc,
) -> go_v1._IdentityMutationGuard:
    monkeypatch.setattr(go_v1, "_linux_inotify_libc", lambda: fake)
    return go_v1._IdentityMutationGuard(go_v1.PLATFORM_LINUX, paths)


@contextmanager
def _observed_rows() -> Iterator[list[tuple[str, dict[str, object]]]]:
    rows: list[tuple[str, dict[str, object]]] = []
    token = go_v1._OBSERVATION_SINK.set(
        lambda stage, **values: rows.append((stage, dict(values)))
    )
    try:
        yield rows
    finally:
        go_v1._OBSERVATION_SINK.reset(token)


def _observed_event_flags(
    rows: list[tuple[str, dict[str, object]]],
) -> list[int]:
    return [
        int(values["event_flags"])  # type: ignore[arg-type]
        for stage, values in rows
        if stage == "identity-event"
    ]


# Claim: targets cover retained paths and parents only, with exact masks.
def test_linux_watch_targets_cover_retained_paths_and_parents(tmp_path: Path):
    retained = tmp_path / "tree" / "tool"
    other = tmp_path / "tree" / "other"
    targets = go_v1._linux_watch_targets((retained, other))
    by_path = {target.path: target for target in targets}
    assert by_path[retained].retained == retained
    assert by_path[tmp_path / "tree"].children == {
        "tool": retained,
        "other": other,
    }
    # Exact key set: retained paths plus their one shared parent, no more.
    assert set(by_path) == {retained, other, tmp_path / "tree"}
    # The retained file mask is exactly its content, attrib and self bits.
    assert go_v1._linux_retained_watch_mask(is_directory=False) == (
        go_v1._LINUX_IN_MODIFY
        | go_v1._LINUX_IN_ATTRIB
        | go_v1._LINUX_IN_CLOSE_WRITE
        | go_v1._LINUX_IN_DELETE_SELF
        | go_v1._LINUX_IN_MOVE_SELF
        | go_v1._LINUX_IN_DONT_FOLLOW
    )
    # The retained directory mask drops child-only content bits, exactly.
    assert go_v1._linux_retained_watch_mask(is_directory=True) == (
        go_v1._LINUX_IN_ATTRIB
        | go_v1._LINUX_IN_DELETE_SELF
        | go_v1._LINUX_IN_MOVE_SELF
        | go_v1._LINUX_IN_DONT_FOLLOW
        | go_v1._LINUX_IN_CREATE
        | go_v1._LINUX_IN_DELETE
        | go_v1._LINUX_IN_MOVED_FROM
        | go_v1._LINUX_IN_MOVED_TO
    )
    # The parent mask is exactly the entry/self bits.
    assert go_v1._linux_parent_watch_mask() == (
        go_v1._LINUX_IN_CREATE
        | go_v1._LINUX_IN_DELETE
        | go_v1._LINUX_IN_MOVED_FROM
        | go_v1._LINUX_IN_MOVED_TO
        | go_v1._LINUX_IN_DELETE_SELF
        | go_v1._LINUX_IN_MOVE_SELF
    )


# Claim: parsed events classify retained mutations, ignore unrelated names.
def test_linux_inotify_parse_and_classify(tmp_path: Path):
    retained = tmp_path / "tool"
    targets = go_v1._linux_watch_targets((retained,))
    by_wd = {index + 1: target for index, target in enumerate(targets)}
    wd_by_path = {target.path: wd for wd, target in by_wd.items()}

    quiet = go_v1._classify_linux_inotify_events((), by_wd)
    assert quiet.changed == () and quiet.fatal is None

    def verdict_for(payload: bytes) -> go_v1._LinuxInotifyVerdict:
        events = go_v1._parse_linux_inotify_buffer(payload)
        return go_v1._classify_linux_inotify_events(events, by_wd)

    modified = verdict_for(
        _event_bytes(wd_by_path[retained], go_v1._LINUX_IN_MODIFY)
    )
    assert modified.changed == (retained,)
    moved = verdict_for(
        _event_bytes(
            wd_by_path[tmp_path], go_v1._LINUX_IN_MOVED_FROM, "tool"
        )
    )
    assert moved.changed == (retained,)
    # Positive control: a change to an unrelated name does not reject.
    unrelated = verdict_for(
        _event_bytes(
            wd_by_path[tmp_path], go_v1._LINUX_IN_CREATE, "scratch"
        )
    )
    assert unrelated.changed == () and unrelated.fatal is None
    with pytest.raises(ValueError):
        go_v1._parse_linux_inotify_buffer(b"\x01\x00")


# Claim: overflow, removed, unknown-wd and unmount events fail closed.
def test_linux_inotify_overflow_and_removed_watch_are_fatal(tmp_path: Path):
    retained = tmp_path / "tool"
    targets = go_v1._linux_watch_targets((retained,))
    by_wd = {index + 1: target for index, target in enumerate(targets)}
    wd = next(
        wd for wd, target in by_wd.items() if target.path == retained
    )
    overflow = go_v1._classify_linux_inotify_events(
        go_v1._parse_linux_inotify_buffer(
            _event_bytes(-1, go_v1._LINUX_IN_Q_OVERFLOW)
        ),
        by_wd,
    )
    assert overflow.fatal is not None and "overflow" in overflow.fatal
    removed = go_v1._classify_linux_inotify_events(
        go_v1._parse_linux_inotify_buffer(
            _event_bytes(wd, go_v1._LINUX_IN_IGNORED)
        ),
        by_wd,
    )
    assert removed.fatal is not None and "removed" in removed.fatal
    unknown = go_v1._classify_linux_inotify_events(
        go_v1._parse_linux_inotify_buffer(
            _event_bytes(99, go_v1._LINUX_IN_MODIFY)
        ),
        by_wd,
    )
    assert unknown.fatal is not None and "unknown" in unknown.fatal
    unmount = go_v1._classify_linux_inotify_events(
        go_v1._parse_linux_inotify_buffer(
            _event_bytes(wd, go_v1._LINUX_IN_UNMOUNT)
        ),
        by_wd,
    )
    assert unmount.fatal is not None and "filesystem" in unmount.fatal
    # Positive control: a benign named event keeps the verdict usable.
    parent_wd = next(
        item for item, target in by_wd.items() if target.path == tmp_path
    )
    usable = go_v1._classify_linux_inotify_events(
        go_v1._parse_linux_inotify_buffer(
            _event_bytes(parent_wd, go_v1._LINUX_IN_CREATE, "scratch")
        ),
        by_wd,
    )
    assert usable.fatal is None and usable.changed == ()


# Claim: a moved or deleted parent-only watch marks its retained children.
def test_linux_parent_self_event_marks_retained_children(tmp_path: Path):
    retained = tmp_path / "tree" / "tool"
    targets = go_v1._linux_watch_targets((retained,))
    by_wd = {index + 1: target for index, target in enumerate(targets)}
    parent_wd = next(
        wd
        for wd, target in by_wd.items()
        if target.path == tmp_path / "tree"
    )
    for bit in (go_v1._LINUX_IN_MOVE_SELF, go_v1._LINUX_IN_DELETE_SELF):
        verdict = go_v1._classify_linux_inotify_events(
            go_v1._parse_linux_inotify_buffer(_event_bytes(parent_wd, bit)),
            by_wd,
        )
        assert verdict.changed == (retained,)
    # Positive control: a non-self event on the same parent marks nothing.
    quiet = go_v1._classify_linux_inotify_events(
        go_v1._parse_linux_inotify_buffer(
            _event_bytes(parent_wd, go_v1._LINUX_IN_ATTRIB)
        ),
        by_wd,
    )
    assert quiet.changed == () and quiet.fatal is None


# Claim: only entry changes reject for non-retained names in a retained dir.
def test_linux_retained_directory_ignores_child_content_writes(
    tmp_path: Path,
):
    directory = tmp_path / "dir"
    targets = go_v1._linux_watch_targets((directory,))
    by_wd = {index + 1: target for index, target in enumerate(targets)}
    wd = next(
        item for item, target in by_wd.items() if target.path == directory
    )

    def verdict_for(mask: int) -> go_v1._LinuxInotifyVerdict:
        return go_v1._classify_linux_inotify_events(
            go_v1._parse_linux_inotify_buffer(
                _event_bytes(wd, mask, "unrelated.log")
            ),
            by_wd,
        )

    for bit in (
        go_v1._LINUX_IN_CREATE,
        go_v1._LINUX_IN_DELETE,
        go_v1._LINUX_IN_MOVED_FROM,
        go_v1._LINUX_IN_MOVED_TO,
    ):
        assert verdict_for(bit).changed == (directory,)
    # Positive control: content writes to the same child do not reject.
    for bit in (
        go_v1._LINUX_IN_MODIFY,
        go_v1._LINUX_IN_CLOSE_WRITE,
        go_v1._LINUX_IN_ATTRIB,
    ):
        assert verdict_for(bit).changed == ()


# Claim: baseline identity detects same-size content and timestamp changes.
@_POSIX_ONLY
def test_linux_retained_identity_baseline_comparison(tmp_path: Path):
    retained = tmp_path / "tool"
    retained.write_bytes(b"original")
    baseline = (go_v1._capture_linux_retained_identity(retained),)
    recaptured = (go_v1._capture_linux_retained_identity(retained),)
    assert go_v1._linux_retained_identities_changed(baseline, recaptured) == ()

    retained.write_bytes(b"mutated!")
    changed = (go_v1._capture_linux_retained_identity(retained),)
    assert go_v1._linux_retained_identities_changed(baseline, changed) == (
        retained,
    )
    retained.write_bytes(b"original")
    # Pin a distinct mtime so detection never depends on timestamp grain.
    before = retained.lstat()
    os.utime(
        retained,
        ns=(before.st_atime_ns, before.st_mtime_ns + 1_000_000_000),
    )
    restored = (go_v1._capture_linux_retained_identity(retained),)
    # Restored bytes still differ in mtime/ctime, so the change is visible.
    assert go_v1._linux_retained_identities_changed(baseline, restored) == (
        retained,
    )
    # Positive control: a fresh baseline of the restored file is stable.
    assert go_v1._linux_retained_identities_changed(
        restored, (go_v1._capture_linux_retained_identity(retained),)
    ) == ()


# Claim: a quiet fake-guarded set passes verify() and close() untouched.
@_POSIX_ONLY
def test_linux_guard_quiet_control_passes_with_fake_inotify(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    retained = tmp_path / "tool"
    retained.write_bytes(b"original")
    fake = _FakeInotifyLibc()
    try:
        guard = _fake_guard(monkeypatch, (retained,), fake)
        guard.verify()
        guard.close()
    finally:
        fake.close()


# Claim: a reported modify rejects verify() and a separate close() alike.
@_POSIX_ONLY
def test_linux_guard_event_rejects_verify_and_close_with_fake_inotify(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    retained = tmp_path / "tool"
    retained.write_bytes(b"original")

    fake = _FakeInotifyLibc()
    try:
        guard = _fake_guard(monkeypatch, (retained,), fake)
        fake.emit(retained, go_v1._LINUX_IN_MODIFY)
        with pytest.raises(go_v1.GoV1Error) as raised:
            guard.verify()
        assert raised.value.code == go_v1.CODE_WORKER_IDENTITY_INVALID
        _close_quietly(guard)
    finally:
        fake.close()

    fake = _FakeInotifyLibc()
    try:
        guard = _fake_guard(monkeypatch, (retained,), fake)
        fake.emit(retained, go_v1._LINUX_IN_CLOSE_WRITE)
        with pytest.raises(go_v1.GoV1Error) as raised:
            guard.close()
        assert raised.value.code == go_v1.CODE_WORKER_IDENTITY_INVALID
    finally:
        fake.close()

    # Positive control: a fresh quiet guard over the same file passes.
    fake = _FakeInotifyLibc()
    try:
        guard = _fake_guard(monkeypatch, (retained,), fake)
        guard.verify()
        guard.close()
    finally:
        fake.close()


# Claim: overflow and kernel-removed watches refuse with control-unavailable.
@_POSIX_ONLY
def test_linux_guard_overflow_and_removed_watch_refuse_with_fake_inotify(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    retained = tmp_path / "tool"
    retained.write_bytes(b"original")

    fake = _FakeInotifyLibc()
    try:
        guard = _fake_guard(monkeypatch, (retained,), fake)
        fake.emit_raw(_event_bytes(-1, go_v1._LINUX_IN_Q_OVERFLOW))
        with pytest.raises(go_v1.GoV1Error) as raised:
            guard.verify()
        assert raised.value.code == go_v1.CODE_CONTROL_UNAVAILABLE
        _close_quietly(guard)
    finally:
        fake.close()

    fake = _FakeInotifyLibc()
    try:
        guard = _fake_guard(monkeypatch, (retained,), fake)
        fake.emit(retained, go_v1._LINUX_IN_IGNORED)
        with pytest.raises(go_v1.GoV1Error) as raised:
            guard.close()
        assert raised.value.code == go_v1.CODE_CONTROL_UNAVAILABLE
    finally:
        fake.close()

    # Positive control: a fresh quiet guard over the same file passes.
    fake = _FakeInotifyLibc()
    try:
        guard = _fake_guard(monkeypatch, (retained,), fake)
        guard.verify()
        guard.close()
    finally:
        fake.close()


# Claim: a persistent change rejects at the final checkpoint without events.
@_POSIX_ONLY
def test_linux_guard_persistent_change_rejects_with_fake_inotify(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    retained = tmp_path / "tool"
    retained.write_bytes(b"original")
    fake = _FakeInotifyLibc()
    try:
        guard = _fake_guard(monkeypatch, (retained,), fake)
        retained.write_bytes(b"mutated!")
        with pytest.raises(go_v1.GoV1Error) as raised:
            guard.close()
        assert raised.value.code == go_v1.CODE_WORKER_IDENTITY_INVALID
    finally:
        fake.close()
    # Positive control: restoring the file lets a fresh guard pass.
    retained.write_bytes(b"original")
    fake = _FakeInotifyLibc()
    try:
        guard = _fake_guard(monkeypatch, (retained,), fake)
        guard.verify()
        guard.close()
    finally:
        fake.close()


# Claim: descriptor/watch registration limits refuse setup, never degrade.
@_POSIX_ONLY
def test_linux_guard_watch_registration_limits_refuse_setup(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    retained = tmp_path / "tool"
    retained.write_bytes(b"original")

    fake = _FakeInotifyLibc(fail_watch=errno.ENOSPC)
    try:
        with pytest.raises(go_v1.GoV1Error) as raised:
            _fake_guard(monkeypatch, (retained,), fake)
        assert raised.value.code == go_v1.CODE_CONTROL_UNAVAILABLE
        with pytest.raises(OSError) as closed:
            os.fstat(fake.read_fd)
        assert closed.value.errno == errno.EBADF
    finally:
        fake.close()

    fake = _FakeInotifyLibc(fail_init=errno.ENFILE)
    try:
        with pytest.raises(go_v1.GoV1Error) as raised:
            _fake_guard(monkeypatch, (retained,), fake)
        assert raised.value.code == go_v1.CODE_CONTROL_UNAVAILABLE
    finally:
        fake.close()

    # Positive control: registration that succeeds constructs a quiet guard.
    fake = _FakeInotifyLibc()
    try:
        guard = _fake_guard(monkeypatch, (retained,), fake)
        guard.close()
    finally:
        fake.close()


# Claim: after a rejected verify() the Linux close() passes, as on macOS.
@_POSIX_ONLY
def test_linux_guard_close_passes_after_rejected_verify_with_fake_inotify(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    retained = tmp_path / "tool"
    retained.write_bytes(b"original")

    # Stat path: write-and-restore leaves only new timestamps behind.
    fake = _FakeInotifyLibc()
    try:
        guard = _fake_guard(monkeypatch, (retained,), fake)
        retained.write_bytes(b"mutated!")
        retained.write_bytes(b"original")
        before = retained.lstat()
        os.utime(
            retained,
            ns=(before.st_atime_ns, before.st_mtime_ns + 1_000_000_000),
        )
        with pytest.raises(go_v1.GoV1Error) as raised:
            guard.verify()
        assert raised.value.code == go_v1.CODE_WORKER_IDENTITY_INVALID
        guard.close()
    finally:
        fake.close()

    # Event path: the checkpoint captures the records it rejected on.
    fake = _FakeInotifyLibc()
    try:
        guard = _fake_guard(monkeypatch, (retained,), fake)
        retained.write_bytes(b"mutated!")
        fake.emit(retained, go_v1._LINUX_IN_MODIFY)
        with pytest.raises(go_v1.GoV1Error) as raised:
            guard.verify()
        assert raised.value.code == go_v1.CODE_WORKER_IDENTITY_INVALID
        guard.close()
    finally:
        fake.close()


# Claim: a further change after a rejected verify() still rejects close().
@_POSIX_ONLY
def test_linux_guard_close_reports_new_change_after_rejected_verify(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    retained = tmp_path / "tool"
    retained.write_bytes(b"original")

    def write_restore_bumped() -> None:
        previous = retained.lstat().st_mtime_ns
        retained.write_bytes(b"mutated!")
        retained.write_bytes(b"original")
        os.utime(
            retained,
            ns=(retained.lstat().st_atime_ns, previous + 1_000_000_000),
        )

    fake = _FakeInotifyLibc()
    try:
        guard = _fake_guard(monkeypatch, (retained,), fake)
        write_restore_bumped()
        with pytest.raises(go_v1.GoV1Error) as raised:
            guard.verify()
        assert raised.value.code == go_v1.CODE_WORKER_IDENTITY_INVALID
        write_restore_bumped()
        with pytest.raises(go_v1.GoV1Error) as raised:
            guard.close()
        assert raised.value.code == go_v1.CODE_WORKER_IDENTITY_INVALID
    finally:
        fake.close()

    # Positive control: a fresh quiet guard over the same file passes.
    fake = _FakeInotifyLibc()
    try:
        guard = _fake_guard(monkeypatch, (retained,), fake)
        guard.verify()
        guard.close()
    finally:
        fake.close()


# Claim: a detectable identity change outranks a fatal mechanism verdict.
@_POSIX_ONLY
def test_linux_guard_fatal_with_identity_change_reports_identity(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    retained = tmp_path / "tool"
    retained.write_bytes(b"original")

    fake = _FakeInotifyLibc()
    try:
        guard = _fake_guard(monkeypatch, (retained,), fake)
        retained.write_bytes(b"MUTATED")
        fake.emit_raw(_event_bytes(-1, go_v1._LINUX_IN_Q_OVERFLOW))
        with pytest.raises(go_v1.GoV1Error) as raised:
            guard.verify()
        assert raised.value.code == go_v1.CODE_WORKER_IDENTITY_INVALID
        _close_quietly(guard)
    finally:
        fake.close()

    retained.write_bytes(b"original")
    fake = _FakeInotifyLibc()
    try:
        guard = _fake_guard(monkeypatch, (retained,), fake)
        fake.emit(retained, go_v1._LINUX_IN_MODIFY)
        fake.emit_raw(_event_bytes(-1, go_v1._LINUX_IN_Q_OVERFLOW))
        with pytest.raises(go_v1.GoV1Error) as raised:
            guard.verify()
        assert raised.value.code == go_v1.CODE_WORKER_IDENTITY_INVALID
        _close_quietly(guard)
    finally:
        fake.close()

    # Control: the same overflow without any change stays control-unavailable.
    fake = _FakeInotifyLibc()
    try:
        guard = _fake_guard(monkeypatch, (retained,), fake)
        fake.emit_raw(_event_bytes(-1, go_v1._LINUX_IN_Q_OVERFLOW))
        with pytest.raises(go_v1.GoV1Error) as raised:
            guard.verify()
        assert raised.value.code == go_v1.CODE_CONTROL_UNAVAILABLE
        _close_quietly(guard)
    finally:
        fake.close()

    # Positive control: a fresh quiet guard over the same file passes.
    fake = _FakeInotifyLibc()
    try:
        guard = _fake_guard(monkeypatch, (retained,), fake)
        guard.verify()
        guard.close()
    finally:
        fake.close()


# Claim: events already read are classified before the read bound refuses.
@_POSIX_ONLY
def test_linux_guard_read_bound_classifies_events_already_read(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    retained = tmp_path / "tool"
    retained.write_bytes(b"original")

    # Positive control: under the default bound a quiet guard passes.
    fake = _FakeInotifyLibc()
    try:
        guard = _fake_guard(monkeypatch, (retained,), fake)
        guard.verify()
        guard.close()
    finally:
        fake.close()

    monkeypatch.setattr(go_v1, "_LINUX_INOTIFY_READ_LIMIT", 16)
    fake = _FakeInotifyLibc()
    try:
        guard = _fake_guard(monkeypatch, (retained,), fake)
        fake.emit(retained, go_v1._LINUX_IN_MODIFY)
        fake.emit(retained, go_v1._LINUX_IN_MODIFY)
        with pytest.raises(go_v1.GoV1Error) as raised:
            guard.verify()
        assert raised.value.code == go_v1.CODE_WORKER_IDENTITY_INVALID
        _close_quietly(guard)
    finally:
        fake.close()

    # Control: benign events over the bound refuse as control-unavailable.
    fake = _FakeInotifyLibc()
    try:
        guard = _fake_guard(monkeypatch, (retained,), fake)
        fake.emit(tmp_path, go_v1._LINUX_IN_CREATE, "scratch")
        fake.emit(tmp_path, go_v1._LINUX_IN_CREATE, "scratch")
        with pytest.raises(go_v1.GoV1Error) as raised:
            guard.verify()
        assert raised.value.code == go_v1.CODE_CONTROL_UNAVAILABLE
        _close_quietly(guard)
    finally:
        fake.close()


# Claim: a truncated event buffer refuses through the guard, never passes.
@_POSIX_ONLY
def test_linux_guard_truncated_buffer_refuses_with_fake_inotify(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    retained = tmp_path / "tool"
    retained.write_bytes(b"original")

    fake = _FakeInotifyLibc()
    try:
        guard = _fake_guard(monkeypatch, (retained,), fake)
        fake.emit_raw(b"\x01\x00")
        with pytest.raises(go_v1.GoV1Error) as raised:
            guard.verify()
        assert raised.value.code == go_v1.CODE_CONTROL_UNAVAILABLE
        _close_quietly(guard)
    finally:
        fake.close()

    # Identity wins over the bad drain when a persistent change is present.
    fake = _FakeInotifyLibc()
    try:
        guard = _fake_guard(monkeypatch, (retained,), fake)
        retained.write_bytes(b"MUTATED")
        fake.emit_raw(b"\x01\x00")
        with pytest.raises(go_v1.GoV1Error) as raised:
            guard.verify()
        assert raised.value.code == go_v1.CODE_WORKER_IDENTITY_INVALID
        _close_quietly(guard)
    finally:
        fake.close()

    # Positive control: restoring the file lets a fresh guard pass.
    retained.write_bytes(b"original")
    fake = _FakeInotifyLibc()
    try:
        guard = _fake_guard(monkeypatch, (retained,), fake)
        guard.verify()
        guard.close()
    finally:
        fake.close()


# Claim: Linux identity events reach the observation sink as numeric rows.
@_POSIX_ONLY
def test_linux_guard_observation_rows_with_fake_inotify(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    retained = tmp_path / "tool"
    retained.write_bytes(b"original")
    fake = _FakeInotifyLibc()
    try:
        guard = _fake_guard(monkeypatch, (retained,), fake)
        with _observed_rows() as rows:
            fake.emit(retained, go_v1._LINUX_IN_MODIFY)
            with pytest.raises(go_v1.GoV1Error) as raised:
                guard.verify()
        assert raised.value.code == go_v1.CODE_WORKER_IDENTITY_INVALID
        assert ("identity-events", {"event_count": 1}) in rows
        assert _observed_event_flags(rows) == [go_v1._LINUX_IN_MODIFY]
        event_row = next(
            values for stage, values in rows if stage == "identity-event"
        )
        assert event_row["path_index"] == 0
        _close_quietly(guard)
    finally:
        fake.close()

    # Positive control: a quiet guard emits no identity-event rows.
    fake = _FakeInotifyLibc()
    try:
        guard = _fake_guard(monkeypatch, (retained,), fake)
        with _observed_rows() as rows:
            guard.verify()
            guard.close()
        assert _observed_event_flags(rows) == []
    finally:
        fake.close()


# Claim: a merged-watch re-registration on another wd refuses setup.
@_POSIX_ONLY
def test_linux_guard_merge_reregistration_mismatch_refuses_setup(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    retained = tmp_path / "tool"
    retained.write_bytes(b"original")
    forced = {os.fsencode(retained): 7, os.fsencode(tmp_path): 7}

    fake = _MergingFakeInotifyLibc(forced, mismatch_on_readd=True)
    try:
        with pytest.raises(go_v1.GoV1Error) as raised:
            _fake_guard(monkeypatch, (retained,), fake)
        assert raised.value.code == go_v1.CODE_CONTROL_UNAVAILABLE
        with pytest.raises(OSError) as closed:
            os.fstat(fake.read_fd)
        assert closed.value.errno == errno.EBADF
    finally:
        fake.close()

    # Positive control: re-registering on the same wd constructs the guard.
    fake = _MergingFakeInotifyLibc(forced)
    try:
        guard = _fake_guard(monkeypatch, (retained,), fake)
        guard.verify()
        guard.close()
    finally:
        fake.close()


# Claim: the merged mask takes its directory kind from the existing path.
@_POSIX_ONLY
def test_linux_guard_merge_mask_uses_existing_path_kind(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    directory = tmp_path / "dir"
    directory.mkdir()
    other_dir = tmp_path / "other"
    other_dir.mkdir()
    retained_file = other_dir / "tool"
    retained_file.write_bytes(b"original")
    forced = {
        os.fsencode(directory): 7,
        os.fsencode(retained_file): 7,
    }
    fake = _MergingFakeInotifyLibc(forced)
    try:
        guard = _fake_guard(
            monkeypatch, (directory, retained_file), fake
        )
        # The existing target is the directory, so entry bits stay watched.
        assert fake.masks[7] & go_v1._LINUX_IN_CREATE
        guard.verify()
        guard.close()
    finally:
        fake.close()


# Claim: the internal Linux adapter does not open either public admission.
def test_linux_guard_does_not_open_admission(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(sys, "platform", "linux")
    monkeypatch.setattr(
        go_v1, "os", SimpleNamespace(**(vars(os) | {"name": "posix"}))
    )
    assert not installer.supports_external_builds()
    with pytest.raises(go_v1.GoV1Error) as raised:
        go_v1.inventory_platform()
    assert raised.value.code == go_v1.CODE_CONTROL_UNAVAILABLE
    # Positive control: the macOS host selection is unchanged.
    monkeypatch.setattr(sys, "platform", "darwin")
    assert installer.supports_external_builds()
    assert go_v1.inventory_platform() == go_v1.PLATFORM_MACOS


def _warmed_shared_mapping(retained: Path, original: bytes) -> mmap.mmap:
    handle = retained.open("r+b")
    mapping = mmap.mmap(
        handle.fileno(),
        0,
        flags=mmap.MAP_SHARED,
        prot=mmap.PROT_READ | mmap.PROT_WRITE,
    )
    handle.close()
    # Warm the already-writable mapping with the original bytes; this write
    # happens before any guard registration, as in the measured plant.
    mapping[: len(original)] = original
    return mapping


# Claim: a real Linux guard passes a quiet set in verify() and close().
@pytest.mark.skipif(
    sys.platform != "linux", reason="real inotify needs a Linux host"
)
def test_linux_guard_quiet_control_on_linux(tmp_path: Path):
    retained = tmp_path / "tool"
    retained.write_bytes(b"original")
    guard = go_v1._IdentityMutationGuard(go_v1.PLATFORM_LINUX, (retained,))
    with _observed_rows() as rows:
        guard.verify()
        guard.close()
    assert _observed_event_flags(rows) == []


# Claim: ordinary write-and-restore on Linux rejects verify() and close().
@pytest.mark.skipif(
    sys.platform != "linux", reason="real inotify needs a Linux host"
)
def test_linux_guard_write_and_restore_rejects_on_linux(tmp_path: Path):
    retained = tmp_path / "tool"
    original = b"original"
    retained.write_bytes(original)

    guard = go_v1._IdentityMutationGuard(go_v1.PLATFORM_LINUX, (retained,))
    retained.write_bytes(b"mutated!")
    retained.write_bytes(original)
    with _observed_rows() as rows:
        with pytest.raises(go_v1.GoV1Error) as raised:
            guard.verify()
    assert raised.value.code == go_v1.CODE_WORKER_IDENTITY_INVALID
    # The kernel event itself was seen, not only the timestamp change.
    assert any(
        flags & go_v1._LINUX_IN_MODIFY
        for flags in _observed_event_flags(rows)
    )
    _close_quietly(guard)

    guard = go_v1._IdentityMutationGuard(go_v1.PLATFORM_LINUX, (retained,))
    retained.write_bytes(b"mutated!")
    retained.write_bytes(original)
    with pytest.raises(go_v1.GoV1Error) as raised:
        guard.close()
    assert raised.value.code == go_v1.CODE_WORKER_IDENTITY_INVALID

    # Positive control: a fresh quiet guard over the restored file passes.
    guard = go_v1._IdentityMutationGuard(go_v1.PLATFORM_LINUX, (retained,))
    guard.verify()
    guard.close()


# Claim: rename-substitute-restore on Linux rejects before any acceptance.
@pytest.mark.skipif(
    sys.platform != "linux", reason="real inotify needs a Linux host"
)
def test_linux_guard_rename_substitute_restore_rejects_on_linux(
    tmp_path: Path,
):
    retained = tmp_path / "tool"
    original = b"original"
    retained.write_bytes(original)
    aside = tmp_path / "aside"
    substitute = tmp_path / "substitute"

    guard = go_v1._IdentityMutationGuard(go_v1.PLATFORM_LINUX, (retained,))
    os.rename(retained, aside)
    substitute.write_bytes(original)
    os.rename(substitute, retained)
    retained.unlink()
    os.rename(aside, retained)
    assert retained.read_bytes() == original
    with _observed_rows() as rows:
        with pytest.raises(go_v1.GoV1Error) as raised:
            guard.verify()
    assert raised.value.code == go_v1.CODE_WORKER_IDENTITY_INVALID
    # The kernel move events themselves were seen, not only the new inode.
    assert any(
        flags
        & (go_v1._LINUX_IN_MOVE_SELF | go_v1._LINUX_IN_MOVED_FROM)
        for flags in _observed_event_flags(rows)
    )
    _close_quietly(guard)

    # Positive control: a fresh quiet guard over the restored file passes.
    guard = go_v1._IdentityMutationGuard(go_v1.PLATFORM_LINUX, (retained,))
    guard.verify()
    guard.close()


# Claim: renaming a non-retained parent away and back rejects on Linux.
@pytest.mark.skipif(
    sys.platform != "linux", reason="real inotify needs a Linux host"
)
def test_linux_guard_parent_rename_substitute_restore_rejects_on_linux(
    tmp_path: Path,
):
    parent = tmp_path / "tree"
    parent.mkdir()
    retained = parent / "tool"
    original = b"original"
    retained.write_bytes(original)
    aside = tmp_path / "tree-aside"

    guard = go_v1._IdentityMutationGuard(go_v1.PLATFORM_LINUX, (retained,))
    os.rename(parent, aside)
    parent.mkdir()
    retained.write_bytes(original)
    retained.unlink()
    parent.rmdir()
    os.rename(aside, parent)
    assert retained.read_bytes() == original
    with _observed_rows() as rows:
        with pytest.raises(go_v1.GoV1Error) as raised:
            guard.verify()
    assert raised.value.code == go_v1.CODE_WORKER_IDENTITY_INVALID
    # Only the parent IN_MOVE_SELF event can detect this restored plant.
    assert any(
        flags & go_v1._LINUX_IN_MOVE_SELF
        for flags in _observed_event_flags(rows)
    )
    _close_quietly(guard)

    # Positive control: a fresh quiet guard over the restored file passes.
    guard = go_v1._IdentityMutationGuard(go_v1.PLATFORM_LINUX, (retained,))
    guard.verify()
    guard.close()


# Claim: a change still present at the final Linux checkpoint rejects.
@pytest.mark.skipif(
    sys.platform != "linux", reason="real inotify needs a Linux host"
)
def test_linux_guard_persistent_change_rejects_on_linux(tmp_path: Path):
    retained = tmp_path / "tool"
    retained.write_bytes(b"original")
    guard = go_v1._IdentityMutationGuard(go_v1.PLATFORM_LINUX, (retained,))
    retained.write_bytes(b"mutated!")
    with _observed_rows() as rows:
        with pytest.raises(go_v1.GoV1Error) as raised:
            guard.close()
    assert raised.value.code == go_v1.CODE_WORKER_IDENTITY_INVALID
    # The kernel event itself was seen, not only the final stat change.
    assert any(
        flags & go_v1._LINUX_IN_MODIFY
        for flags in _observed_event_flags(rows)
    )
    # Positive control: restoring the file lets a fresh guard pass.
    retained.write_bytes(b"original")
    guard = go_v1._IdentityMutationGuard(go_v1.PLATFORM_LINUX, (retained,))
    guard.verify()
    guard.close()


# Claim: a change between watch registration and baseline capture rejects.
@pytest.mark.skipif(
    sys.platform != "linux", reason="real inotify needs a Linux host"
)
def test_linux_guard_change_before_baseline_capture_rejects_on_linux(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    retained = tmp_path / "tool"
    original = b"original"
    retained.write_bytes(original)
    capture = go_v1._capture_linux_retained_identity
    planted = False

    def capturing(path: Path) -> go_v1._LinuxRetainedIdentity:
        nonlocal planted
        if not planted:
            planted = True
            path.write_bytes(b"mutated!")
            path.write_bytes(original)
        return capture(path)

    monkeypatch.setattr(
        go_v1, "_capture_linux_retained_identity", capturing
    )
    guard = go_v1._IdentityMutationGuard(go_v1.PLATFORM_LINUX, (retained,))
    assert planted
    with _observed_rows() as rows:
        with pytest.raises(go_v1.GoV1Error) as raised:
            guard.verify()
    assert raised.value.code == go_v1.CODE_WORKER_IDENTITY_INVALID
    # The baseline already holds the restored state; only the event detects.
    assert any(
        flags & go_v1._LINUX_IN_MODIFY
        for flags in _observed_event_flags(rows)
    )
    _close_quietly(guard)

    # Positive control: without the plant a fresh guard passes quietly.
    monkeypatch.setattr(go_v1, "_capture_linux_retained_identity", capture)
    guard = go_v1._IdentityMutationGuard(go_v1.PLATFORM_LINUX, (retained,))
    guard.verify()
    guard.close()


# Claim (pinned limit): the Linux guard accepts the documented blind spot.
# This pins the documented limit of the portable policy: a write through an
# already writable, warmed shared mapping, restored without a flush, yields
# no event and no final identity difference; the stronger property belongs
# to the deferred read-only-source-and-toolchain guarantee.  The test exists
# so this accepted behaviour cannot change silently in either direction.
@pytest.mark.skipif(
    sys.platform != "linux", reason="real inotify needs a Linux host"
)
def test_linux_guard_accepts_warmed_mapping_documented_limit(tmp_path: Path):
    retained = tmp_path / "tool"
    original = b"original"
    retained.write_bytes(original)
    mapping = _warmed_shared_mapping(retained, original)
    try:
        guard = go_v1._IdentityMutationGuard(
            go_v1.PLATFORM_LINUX, (retained,)
        )
        stamps_before = retained.lstat()
        mapping[: len(original)] = b"mutated!"
        assert mapping[: len(original)] == b"mutated!"
        assert retained.read_bytes() == b"mutated!"
        mapping[: len(original)] = original
        assert mapping[: len(original)] == original
        assert retained.read_bytes() == original
        stamps_after = retained.lstat()
        assert (
            stamps_after.st_mtime_ns == stamps_before.st_mtime_ns
            and stamps_after.st_ctime_ns == stamps_before.st_ctime_ns
        ), "plant precondition broken: the warmed page was written back"
        guard.verify()
        guard.close()
    finally:
        mapping.close()
    # Positive control: the ordinary detected write still rejects nearby.
    guard = go_v1._IdentityMutationGuard(go_v1.PLATFORM_LINUX, (retained,))
    retained.write_bytes(b"mutated!")
    retained.write_bytes(original)
    with pytest.raises(go_v1.GoV1Error) as raised:
        guard.verify()
    assert raised.value.code == go_v1.CODE_WORKER_IDENTITY_INVALID
    _close_quietly(guard)


# Claim (pinned limit): the macOS guard accepts the documented blind spot.
# This pins the documented limit of the portable policy: a write through an
# already writable, warmed shared mapping, restored without a flush, yields
# no event and no final identity difference; the stronger property belongs
# to the deferred read-only-source-and-toolchain guarantee.  The test exists
# so this accepted behaviour cannot change silently in either direction.
@pytest.mark.skipif(
    sys.platform != "darwin", reason="real kqueue needs a macOS host"
)
def test_macos_guard_accepts_warmed_mapping_documented_limit(tmp_path: Path):
    retained = tmp_path / "tool"
    original = b"original"
    retained.write_bytes(original)
    mapping = _warmed_shared_mapping(retained, original)
    try:
        guard = go_v1._IdentityMutationGuard(
            go_v1.PLATFORM_MACOS, (retained,)
        )
        stamps_before = retained.lstat()
        mapping[: len(original)] = b"mutated!"
        assert mapping[: len(original)] == b"mutated!"
        assert retained.read_bytes() == b"mutated!"
        mapping[: len(original)] = original
        assert mapping[: len(original)] == original
        assert retained.read_bytes() == original
        stamps_after = retained.lstat()
        assert (
            stamps_after.st_mtime_ns == stamps_before.st_mtime_ns
            and stamps_after.st_ctime_ns == stamps_before.st_ctime_ns
        ), "plant precondition broken: the warmed page was written back"
        guard.verify()
        guard.close()
    finally:
        mapping.close()
    # Positive control: the ordinary detected write still rejects nearby.
    guard = go_v1._IdentityMutationGuard(go_v1.PLATFORM_MACOS, (retained,))
    retained.write_bytes(b"mutated!")
    retained.write_bytes(original)
    with pytest.raises(go_v1.GoV1Error) as raised:
        guard.verify()
    assert raised.value.code == go_v1.CODE_WORKER_IDENTITY_INVALID
    _close_quietly(guard)
