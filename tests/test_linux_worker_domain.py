"""Worker launch capability, consumption and descriptor handling.

These tests exercise the Linux launch path through its explicit
platform selection only. Public admission stays closed on Linux.
"""

import errno
import hashlib
import io
import json
import os
import stat
import struct
import subprocess
import sys
import threading
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from csk.builds import go_v1

_POSIX_ONLY = pytest.mark.skipif(
    os.name != "posix",
    reason="POSIX descriptor and pipe semantics",
)
_POSIX_CHILD = pytest.mark.skipif(
    sys.platform not in {"darwin", "linux"},
    reason="real POSIX worker child",
)
_LINUX_ONLY = pytest.mark.skipif(
    sys.platform != "linux",
    reason="needs a real Linux kernel for fork-before-consume pipe semantics",
)


def _free_slots(count: int) -> tuple[int, ...]:
    import fcntl

    slots: list[int] = []
    start = 2048
    read_fd, write_fd = os.pipe()
    try:
        while len(slots) < count:
            try:
                candidate = fcntl.fcntl(
                    read_fd, fcntl.F_DUPFD_CLOEXEC, start
                )
            except OSError:
                if start <= 128:
                    raise
                start //= 2
                continue
            os.close(candidate)
            if candidate not in slots:
                slots.append(candidate)
            start = candidate + 16
    finally:
        os.close(read_fd)
        os.close(write_fd)
    return tuple(slots)


def _dup_onto_slot(descriptor: int, slot: int) -> None:
    import fcntl

    duplicate = fcntl.fcntl(descriptor, fcntl.F_DUPFD_CLOEXEC, slot)
    assert duplicate == slot


def _place_pipe(slot: int, payload: bytes) -> None:
    read_fd, write_fd = os.pipe()
    try:
        if payload:
            assert os.write(write_fd, payload) == len(payload)
    finally:
        os.close(write_fd)
    try:
        _dup_onto_slot(read_fd, slot)
    finally:
        os.close(read_fd)


def _place_file(slot: int, path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY)
    try:
        _dup_onto_slot(descriptor, slot)
    finally:
        os.close(descriptor)


def _assert_closed(descriptor: int) -> None:
    with pytest.raises(OSError) as raised:
        os.fstat(descriptor)
    assert raised.value.errno == errno.EBADF


def _open_fd_count() -> int | None:
    for directory in ("/dev/fd", "/proc/self/fd"):
        try:
            return len(os.listdir(directory))
        except OSError:
            continue
    return None


def _bare_linux_domain() -> go_v1._NativeControlDomain:
    domain = object.__new__(go_v1._NativeControlDomain)
    domain.platform = go_v1.PLATFORM_LINUX
    domain.controls = (go_v1.CONTROL_DESCENDANT_DOMAIN_TERMINATION,)
    domain.limits = go_v1.ResourceLimits()
    domain.installed = False
    domain.terminated = False
    domain.job_handle = 0
    domain.file_limit = 0
    domain.launch_secret = b""
    return domain


def _launch_identity() -> SimpleNamespace:
    return SimpleNamespace(
        path=Path(sys.executable),
        startup=SimpleNamespace(
            site_root=Path(go_v1.__file__).resolve().parents[2],
            python_home=Path(sys.base_prefix),
        ),
    )


def _finish_child(
    process, timeout: float = 60.0
) -> tuple[bytes, bytes]:
    # Bound the wait for a real child and clean it up on timeout.
    try:
        return process.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        process.kill()
        process.communicate(timeout=timeout)
        raise


def _cleanup_child(process) -> None:
    if process is None:
        return
    try:
        if process.poll() is None:
            process.kill()
            try:
                process.wait(timeout=60)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=60)
    finally:
        for stream in (process.stdin, process.stdout, process.stderr):
            if stream is not None:
                stream.close()


def test_launch_record_rejects_malformed_and_forged_bytes(monkeypatch):
    # A malformed or forged launch record refuses; the well-formed record
    # for this parent round-trips with its secret intact.
    parent_pid = 424242
    monkeypatch.setattr(os, "getppid", lambda: parent_pid)
    secret = b"a" * go_v1._WORKER_LAUNCH_SECRET_BYTES
    for bad_secret, bad_pid in (
        (secret[:-1], parent_pid),
        (secret + b"a", parent_pid),
        (b"", parent_pid),
        (secret, 1),
        (secret, 0),
    ):
        with pytest.raises(go_v1.GoV1Error, match="is malformed") as raised:
            go_v1._worker_launch_record(bad_secret, bad_pid)
        assert raised.value.code == go_v1.CODE_WORKER_IDENTITY_INVALID

    record = go_v1._worker_launch_record(secret, parent_pid)
    context = go_v1._parse_worker_launch_record(record)
    assert context.parent_pid == parent_pid
    assert context.secret == secret
    assert context.public_dict() == {
        "parent_pid": parent_pid,
        "transport": go_v1._WORKER_LAUNCH_TRANSPORT,
    }

    for forged in (record[:-1], record + b"x"):
        with pytest.raises(
            go_v1.GoV1Error, match="invalid length"
        ) as raised:
            go_v1._parse_worker_launch_record(forged)
        assert raised.value.code == go_v1.CODE_WORKER_IDENTITY_INVALID
    flipped_digest = record[:-1] + bytes([record[-1] ^ 0xFF])
    with pytest.raises(
        go_v1.GoV1Error, match="capability is invalid"
    ) as raised:
        go_v1._parse_worker_launch_record(flipped_digest)
    assert raised.value.code == go_v1.CODE_WORKER_IDENTITY_INVALID
    flipped_magic = bytes([record[0] ^ 0xFF]) + record[1:]
    with pytest.raises(
        go_v1.GoV1Error, match="capability is invalid"
    ) as raised:
        go_v1._parse_worker_launch_record(flipped_magic)
    assert raised.value.code == go_v1.CODE_WORKER_IDENTITY_INVALID
    foreign = go_v1._worker_launch_record(secret, parent_pid + 1)
    with pytest.raises(
        go_v1.GoV1Error, match="not a direct child"
    ) as raised:
        go_v1._parse_worker_launch_record(foreign)
    assert raised.value.code == go_v1.CODE_WORKER_IDENTITY_INVALID

    # Parse-side pid floor: a hand-built record carrying pid 1 with a
    # valid digest still refuses when the consumer reports parent pid 1.
    body_pid1 = (
        go_v1._WORKER_LAUNCH_MAGIC + struct.pack(">Q", 1) + secret
    )
    forged_pid1 = body_pid1 + hashlib.sha256(
        go_v1._WORKER_LAUNCH_RECORD_DOMAIN + body_pid1
    ).digest()
    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(os, "getppid", lambda: 1)
        with pytest.raises(
            go_v1.GoV1Error, match="not a direct child"
        ) as raised:
            go_v1._parse_worker_launch_record(forged_pid1)
    assert raised.value.code == go_v1.CODE_WORKER_IDENTITY_INVALID
    assert go_v1._parse_worker_launch_record(record).secret == secret

    # An unsupported platform refuses without allocating a descriptor.
    before = _open_fd_count()
    with pytest.raises(
        go_v1.GoV1Error,
        match="launch capability is unsupported on this host",
    ) as raised:
        go_v1._PreparedWorkerLaunch("plan9")
    assert raised.value.code == go_v1.CODE_CONTROL_UNAVAILABLE
    after = _open_fd_count()
    if before is not None and after is not None:
        assert after == before

    with pytest.raises(
        go_v1.GoV1Error, match="launch secret is malformed"
    ) as raised:
        go_v1._launch_authenticator(
            secret[:-1],
            go_v1._WORKER_LAUNCH_REQUEST_DOMAIN,
            "nonce",
            {"kind": "request"},
        )
    assert raised.value.code == go_v1.CODE_WORKER_IDENTITY_INVALID
    authenticator = go_v1._launch_authenticator(
        secret,
        go_v1._WORKER_LAUNCH_REQUEST_DOMAIN,
        "nonce",
        {"kind": "request"},
    )
    assert len(authenticator) == 64
    other = go_v1._launch_authenticator(
        b"b" * go_v1._WORKER_LAUNCH_SECRET_BYTES,
        go_v1._WORKER_LAUNCH_REQUEST_DOMAIN,
        "nonce",
        {"kind": "request"},
    )
    assert other != authenticator


@_POSIX_ONLY
def test_sequential_capabilities_are_fresh_and_single_use(monkeypatch):
    # Each prepared launch mints a fresh secret on its own descriptor, and
    # consuming one record leaves nothing for a second consumption.
    slots = _free_slots(2)
    monkeypatch.setattr(go_v1, "_WORKER_LAUNCH_FDS", slots)
    monkeypatch.setattr(os, "getppid", os.getpid)
    first = go_v1._PreparedWorkerLaunch(go_v1.PLATFORM_LINUX)
    second = go_v1._PreparedWorkerLaunch(go_v1.PLATFORM_LINUX)
    try:
        assert first.secret != second.secret
        assert first._read_fd != second._read_fd
        assert {first._read_fd, second._read_fd} == set(slots)
        assert not os.get_inheritable(first._read_fd)
        assert not os.get_inheritable(second._read_fd)
        payload = {"kind": "request"}
        assert go_v1._launch_authenticator(
            first.secret,
            go_v1._WORKER_LAUNCH_REQUEST_DOMAIN,
            "nonce",
            payload,
        ) != go_v1._launch_authenticator(
            second.secret,
            go_v1._WORKER_LAUNCH_REQUEST_DOMAIN,
            "nonce",
            payload,
        )

        monkeypatch.setattr(
            go_v1, "_WORKER_LAUNCH_FDS", (first._read_fd,)
        )
        context = go_v1._consume_worker_launch_context()
        assert context.secret == first.secret
        assert context.parent_pid == os.getpid()
        # Consumption closed the descriptor; the prepared launch must not
        # close the (possibly reused) number a second time.
        consumed = first._read_fd
        first._read_fd = -1
        _assert_closed(consumed)
        with pytest.raises(
            go_v1.GoV1Error, match="lacks one manager-owned"
        ) as raised:
            go_v1._consume_worker_launch_context()
        assert raised.value.code == go_v1.CODE_WORKER_IDENTITY_INVALID

        monkeypatch.setattr(
            go_v1, "_WORKER_LAUNCH_FDS", (second._read_fd,)
        )
        context = go_v1._consume_worker_launch_context()
        assert context.secret == second.secret
        second._read_fd = -1
    finally:
        first.close_parent_copy()
        second.close_parent_copy()


@_POSIX_ONLY
def test_consumption_refuses_zero_or_two_records(monkeypatch):
    # Zero records and two records both refuse; exactly one is accepted.
    secret = b"c" * go_v1._WORKER_LAUNCH_SECRET_BYTES
    record = go_v1._worker_launch_record(secret, os.getpid())
    monkeypatch.setattr(os, "getppid", os.getpid)

    monkeypatch.setattr(go_v1, "_WORKER_LAUNCH_FDS", _free_slots(2))
    with pytest.raises(
        go_v1.GoV1Error, match="lacks one manager-owned"
    ) as raised:
        go_v1._consume_worker_launch_context()
    assert raised.value.code == go_v1.CODE_WORKER_IDENTITY_INVALID

    slots = _free_slots(2)
    monkeypatch.setattr(go_v1, "_WORKER_LAUNCH_FDS", slots)
    _place_pipe(slots[0], record)
    _place_pipe(slots[1], record)
    with pytest.raises(
        go_v1.GoV1Error, match="lacks one manager-owned"
    ) as raised:
        go_v1._consume_worker_launch_context()
    assert raised.value.code == go_v1.CODE_WORKER_IDENTITY_INVALID
    _assert_closed(slots[0])
    _assert_closed(slots[1])

    slots = _free_slots(2)
    monkeypatch.setattr(go_v1, "_WORKER_LAUNCH_FDS", slots)
    _place_pipe(slots[1], record)
    assert go_v1._read_posix_worker_launch_record() == record
    _assert_closed(slots[0])
    _assert_closed(slots[1])


@_POSIX_ONLY
def test_consumption_skips_plants_and_closes_every_slot(
    tmp_path, monkeypatch
):
    # Trailing bytes and short records refuse; junk pipes and regular
    # files are skipped, and every probed slot is closed either way.
    secret = b"d" * go_v1._WORKER_LAUNCH_SECRET_BYTES
    record = go_v1._worker_launch_record(secret, os.getpid())
    monkeypatch.setattr(os, "getppid", os.getpid)

    slots = _free_slots(1)
    monkeypatch.setattr(go_v1, "_WORKER_LAUNCH_FDS", slots)
    _place_pipe(slots[0], record + b"x")
    with pytest.raises(
        go_v1.GoV1Error, match="lacks one manager-owned"
    ) as raised:
        go_v1._consume_worker_launch_context()
    assert raised.value.code == go_v1.CODE_WORKER_IDENTITY_INVALID
    _assert_closed(slots[0])

    slots = _free_slots(1)
    monkeypatch.setattr(go_v1, "_WORKER_LAUNCH_FDS", slots)
    _place_pipe(slots[0], record[:20])
    with pytest.raises(
        go_v1.GoV1Error, match="invalid length"
    ) as raised:
        go_v1._consume_worker_launch_context()
    assert raised.value.code == go_v1.CODE_WORKER_IDENTITY_INVALID
    _assert_closed(slots[0])

    slots = _free_slots(2)
    monkeypatch.setattr(go_v1, "_WORKER_LAUNCH_FDS", slots)
    _place_pipe(slots[0], b"not-a-launch-record")
    _place_pipe(slots[1], record)
    assert go_v1._consume_worker_launch_context().secret == secret
    _assert_closed(slots[0])
    _assert_closed(slots[1])

    planted_file = tmp_path / "planted"
    planted_file.write_bytes(b"regular file, not a pipe")
    slots = _free_slots(2)
    monkeypatch.setattr(go_v1, "_WORKER_LAUNCH_FDS", slots)
    _place_file(slots[0], planted_file)
    _place_pipe(slots[1], record)
    assert go_v1._consume_worker_launch_context().secret == secret
    _assert_closed(slots[0])
    _assert_closed(slots[1])

    slots = _free_slots(1)
    monkeypatch.setattr(go_v1, "_WORKER_LAUNCH_FDS", slots)
    _place_file(slots[0], planted_file)
    with pytest.raises(
        go_v1.GoV1Error, match="lacks one manager-owned"
    ) as raised:
        go_v1._consume_worker_launch_context()
    assert raised.value.code == go_v1.CODE_WORKER_IDENTITY_INVALID
    _assert_closed(slots[0])

    record_file = tmp_path / "record-in-a-regular-file"
    record_file.write_bytes(record)
    slots = _free_slots(1)
    monkeypatch.setattr(go_v1, "_WORKER_LAUNCH_FDS", slots)
    _place_file(slots[0], record_file)
    with pytest.raises(
        go_v1.GoV1Error, match="lacks one manager-owned"
    ) as raised:
        go_v1._consume_worker_launch_context()
    assert raised.value.code == go_v1.CODE_WORKER_IDENTITY_INVALID
    _assert_closed(slots[0])

    # A pipe whose writer stays open with only a record prefix refuses
    # without blocking; the read is non-blocking and the slot is closed.
    slots = _free_slots(1)
    monkeypatch.setattr(go_v1, "_WORKER_LAUNCH_FDS", slots)
    read_fd, write_fd = os.pipe()
    try:
        assert os.write(write_fd, record[:20]) == 20
        _dup_onto_slot(read_fd, slots[0])
    finally:
        os.close(read_fd)
    outcome: dict[str, object] = {}

    def consume() -> None:
        try:
            outcome["context"] = go_v1._consume_worker_launch_context()
        except BaseException as exc:  # asserted in the main thread
            outcome["error"] = exc

    worker = threading.Thread(target=consume, daemon=True)
    try:
        worker.start()
        worker.join(timeout=10)
        assert not worker.is_alive(), (
            "consumption blocked on a pipe with an open writer"
        )
        error = outcome.get("error")
        assert isinstance(error, go_v1.GoV1Error)
        assert error.code == go_v1.CODE_WORKER_IDENTITY_INVALID
        assert "lacks one manager-owned" in error.detail
        _assert_closed(slots[0])
    finally:
        os.close(write_fd)
        worker.join(timeout=10)


class _FakeLaunchedProcess:
    def __init__(self, pid: int = 987654):
        self.pid = pid
        self.stdin = io.BytesIO()
        self.stdout = io.BytesIO()
        self.stderr = io.BytesIO()
        self.returncode = None

    def poll(self):
        return self.returncode

    def kill(self):
        self.returncode = -9

    def wait(self, timeout=None):
        if self.returncode is None:
            self.returncode = 0
        return self.returncode


@_POSIX_ONLY
def test_all_launch_slots_occupied_refuses_before_any_process(
    tmp_path, monkeypatch
):
    # With every fixed slot occupied the launch refuses without starting
    # a process, displacing an occupant, or leaking a descriptor on the
    # allocator refusal path; a freed slot accepts.
    slots = _free_slots(2)
    monkeypatch.setattr(go_v1, "_WORKER_LAUNCH_FDS", slots)
    for slot in slots:
        _place_pipe(slot, b"occupied")
    occupants = {
        slot: (os.fstat(slot).st_dev, os.fstat(slot).st_ino)
        for slot in slots
    }
    cache = tmp_path / "worker-cache"
    cache.mkdir()
    monkeypatch.setattr(
        go_v1, "worker_argv", lambda _identity: ("worker",)
    )
    popen_calls = []

    def forbidden(*args, **kwargs):
        popen_calls.append((args, kwargs))
        pytest.fail("worker started with no launch descriptor")

    domain = _bare_linux_domain()
    try:
        monkeypatch.setattr(go_v1.subprocess, "Popen", forbidden)
        before = _open_fd_count()
        with pytest.raises(
            go_v1.GoV1Error, match="no fixed worker launch descriptor"
        ) as raised:
            domain.launch(_launch_identity(), cache)
        assert raised.value.code == go_v1.CODE_CONTROL_UNAVAILABLE
        assert popen_calls == []
        assert domain.launch_secret == b""
        after = _open_fd_count()
        if before is not None and after is not None:
            assert after == before
        # The refused launch leaves the domain releasable.
        domain.close()
        for slot in slots:
            info = os.fstat(slot)
            assert (info.st_dev, info.st_ino) == occupants[slot]
            assert stat.S_ISFIFO(info.st_mode)
    finally:
        for slot in slots:
            os.close(slot)
        domain.close()

    launched = _FakeLaunchedProcess()
    monkeypatch.setattr(
        go_v1.subprocess, "Popen", lambda *a, **k: launched
    )
    domain = _bare_linux_domain()
    try:
        assert domain.launch(_launch_identity(), cache) is launched
        assert len(domain.launch_secret) == (
            go_v1._WORKER_LAUNCH_SECRET_BYTES
        )
    finally:
        domain.close()


@_POSIX_ONLY
def test_launch_places_one_capability_and_start_failure_cleans_up(
    tmp_path, monkeypatch
):
    # A launch passes exactly its one capability descriptor in a new
    # session and closes the parent copy; a start failure keeps nothing.
    cache = tmp_path / "worker-cache"
    cache.mkdir()
    monkeypatch.setattr(
        go_v1, "worker_argv", lambda _identity: ("worker",)
    )

    slot = _free_slots(1)[0]
    monkeypatch.setattr(go_v1, "_WORKER_LAUNCH_FDS", (slot,))
    captured = {}
    launched = _FakeLaunchedProcess()

    def recording_popen(argv, **kwargs):
        captured["argv"] = argv
        captured["kwargs"] = kwargs
        return launched

    monkeypatch.setattr(go_v1.subprocess, "Popen", recording_popen)
    domain = _bare_linux_domain()
    try:
        assert domain.launch(_launch_identity(), cache) is launched
        kwargs = captured["kwargs"]
        assert kwargs["pass_fds"] == (slot,)
        assert kwargs["close_fds"] is True
        assert kwargs["shell"] is False
        assert kwargs["start_new_session"] is True
        assert kwargs["stdin"] is subprocess.PIPE
        assert kwargs["stdout"] is subprocess.PIPE
        assert kwargs["stderr"] is subprocess.PIPE
        _assert_closed(slot)
        assert domain.installed_controls() == domain.controls
        assert len(domain.launch_secret) == (
            go_v1._WORKER_LAUNCH_SECRET_BYTES
        )
    finally:
        domain.close()

    slot = _free_slots(1)[0]
    monkeypatch.setattr(go_v1, "_WORKER_LAUNCH_FDS", (slot,))

    def failing_popen(*args, **kwargs):
        raise OSError(errno.ENOENT, "planted start failure")

    monkeypatch.setattr(go_v1.subprocess, "Popen", failing_popen)
    domain = _bare_linux_domain()
    try:
        with pytest.raises(
            go_v1.GoV1Error,
            match="cannot launch the identity-verified manager worker",
        ) as raised:
            domain.launch(_launch_identity(), cache)
        assert raised.value.code == go_v1.CODE_WORKER_IDENTITY_INVALID
        assert isinstance(raised.value.__cause__, OSError)
        _assert_closed(slot)
        assert domain.launch_secret == b""
        assert domain.installed_controls() == ()
    finally:
        domain.close()


@_POSIX_ONLY
def test_launch_record_write_failure_closes_pipe_ends(monkeypatch):
    # A write that makes no progress refuses, partial writes complete,
    # and a failed capability preparation leaks no descriptor.
    read_fd, write_fd = os.pipe()
    try:
        with pytest.MonkeyPatch.context() as patch:
            patch.setattr(os, "write", lambda fd, data: 0)
            with pytest.raises(
                OSError, match="cannot write manager-owned"
            ):
                go_v1._write_launch_record_fd(write_fd, b"record")
        real_write = os.write

        def partial_write(fd, data):
            return real_write(fd, data[:2])

        with pytest.MonkeyPatch.context() as patch:
            patch.setattr(os, "write", partial_write)
            go_v1._write_launch_record_fd(write_fd, b"abcdef")
        os.close(write_fd)
        write_fd = -1
        assert os.read(read_fd, 6) == b"abcdef"
    finally:
        os.close(read_fd)
        if write_fd >= 0:
            os.close(write_fd)

    monkeypatch.setattr(
        go_v1, "_WORKER_LAUNCH_FDS", _free_slots(1)
    )
    before = _open_fd_count()

    def failing_write(descriptor, record):
        raise OSError(errno.EIO, "planted capability write failure")

    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(go_v1, "_write_launch_record_fd", failing_write)
        with pytest.raises(OSError, match="planted capability"):
            go_v1._PreparedWorkerLaunch(go_v1.PLATFORM_LINUX)
    after = _open_fd_count()
    if before is not None and after is not None:
        assert after == before

    capability = go_v1._PreparedWorkerLaunch(go_v1.PLATFORM_LINUX)
    capability.close_parent_copy()
    after = _open_fd_count()
    if before is not None and after is not None:
        assert after == before


@_POSIX_ONLY
def test_protocol_descriptor_release_refuses_inheritable_stream():
    # Released protocol descriptors stop crossing exec; a descriptor
    # that stays inheritable refuses before any tool process starts.
    read_fd, write_fd = os.pipe()
    stream = os.fdopen(read_fd, "rb")
    try:
        os.set_inheritable(read_fd, True)
        go_v1._release_protocol_descriptors([stream])
        assert not os.get_inheritable(read_fd)
    finally:
        stream.close()
        os.close(write_fd)

    read_fd, write_fd = os.pipe()
    stream = os.fdopen(read_fd, "rb")
    try:
        os.set_inheritable(read_fd, True)
        with pytest.MonkeyPatch.context() as patch:
            patch.setattr(os, "set_inheritable", lambda fd, value: None)
            with pytest.raises(
                go_v1.GoV1Error,
                match="cannot close protocol descriptors across exec",
            ) as raised:
                go_v1._release_protocol_descriptors([stream])
        assert raised.value.code == (
            go_v1.CODE_CAPABILITY_EVIDENCE_INVALID
        )
        assert os.get_inheritable(read_fd)
    finally:
        os.set_inheritable(read_fd, False)
        stream.close()
        os.close(write_fd)

    read_fd, write_fd = os.pipe()
    stream = os.fdopen(read_fd, "rb")
    try:
        def denied(fd, value):
            raise OSError(errno.EPERM, "planted descriptor denial")

        with pytest.MonkeyPatch.context() as patch:
            patch.setattr(os, "set_inheritable", denied)
            with pytest.raises(
                go_v1.GoV1Error,
                match="cannot close protocol descriptors across exec",
            ) as raised:
                go_v1._release_protocol_descriptors([stream])
        assert raised.value.code == (
            go_v1.CODE_CAPABILITY_EVIDENCE_INVALID
        )
    finally:
        stream.close()
        os.close(write_fd)


@_POSIX_CHILD
def test_real_worker_child_sees_no_unrelated_or_launch_descriptors(
    tmp_path, monkeypatch
):
    # In a real child, an unrelated inheritable manager descriptor and
    # the consumed launch descriptors are absent, while an explicitly
    # passed descriptor in the control spawn is visible.
    import resource

    hard = resource.getrlimit(resource.RLIMIT_NOFILE)[1]
    if hard != resource.RLIM_INFINITY and hard <= min(
        go_v1._WORKER_LAUNCH_FDS
    ):
        pytest.skip("descriptor hard limit cannot hold a fixed slot")
    unrelated = _free_slots(2)[1]
    read_fd, write_fd = os.pipe()
    try:
        _dup_onto_slot(read_fd, unrelated)
    finally:
        os.close(read_fd)
    os.set_inheritable(unrelated, True)
    cache = tmp_path / "worker-cache"
    cache.mkdir()
    source_root = Path(go_v1.__file__).resolve().parents[2]
    script = f"""
import json, os, stat, subprocess, sys
sys.path.insert(0, {str(source_root)!r})
from csk.builds import go_v1 as g
context = g._consume_worker_launch_context()
def is_pipe(fd):
    try:
        return stat.S_ISFIFO(os.fstat(fd).st_mode)
    except OSError:
        return False
result = {{"parent_ok": context.parent_pid == os.getppid()}}
result["unrelated_present"] = is_pipe({unrelated})
result["fixed_present"] = [
    fd for fd in g._WORKER_LAUNCH_FDS if is_pipe(fd)
]
proto_read, proto_write = os.pipe()
os.set_inheritable(proto_read, True)
proto_stream = os.fdopen(os.dup(proto_read), "rb")
os.set_inheritable(proto_stream.fileno(), True)
g._release_protocol_descriptors([proto_stream])
result["released_inheritable"] = os.get_inheritable(
    proto_stream.fileno()
)
witness = "import os, sys; os.fstat(int(sys.argv[1]))"
target = str(proto_stream.fileno())
result["grandchild_without_pass"] = subprocess.run(
    [sys.executable, "-c", witness, target],
    capture_output=True, timeout=60,
).returncode
result["grandchild_with_pass"] = subprocess.run(
    [sys.executable, "-c", witness, target],
    capture_output=True, timeout=60,
    pass_fds=(proto_stream.fileno(),),
).returncode
os.write(1, (json.dumps(result) + "\\n").encode())
"""
    monkeypatch.setattr(
        go_v1,
        "worker_argv",
        lambda _identity: (sys.executable, "-B", "-c", script),
    )
    domain = _bare_linux_domain()
    process = None
    try:
        process = domain.launch(_launch_identity(), cache)
        stdout, stderr = _finish_child(process)
        assert process.returncode == 0, stderr
        witness = json.loads(stdout.splitlines()[0])
        assert witness["parent_ok"] is True
        assert witness["unrelated_present"] is False
        assert witness["fixed_present"] == []
        assert witness["released_inheritable"] is False
        assert witness["grandchild_without_pass"] != 0
        assert witness["grandchild_with_pass"] == 0
    finally:
        _cleanup_child(process)
        domain.close()
        os.close(unrelated)
        os.close(write_fd)


@_POSIX_ONLY
def test_descriptor_limit_restore_mismatch_refuses_and_closes(
    monkeypatch,
):
    # A descriptor-limit restore that does not read back refuses and
    # closes the capability; a prepare that really raises the soft
    # limit restores it byte-identically afterwards.
    import resource

    real_getrlimit = resource.getrlimit
    real_setrlimit = resource.setrlimit
    previous = real_getrlimit(resource.RLIMIT_NOFILE)
    slot = _free_slots(1)[0]
    monkeypatch.setattr(go_v1, "_WORKER_LAUNCH_FDS", (slot,))
    calls = {"count": 0}

    def lying_getrlimit(which):
        calls["count"] += 1
        value = real_getrlimit(which)
        if calls["count"] >= 2:
            return (previous[0] + 1, previous[1])
        return value

    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(resource, "getrlimit", lying_getrlimit)
        with pytest.raises(
            go_v1.GoV1Error,
            match="cannot restore manager descriptor capacity",
        ) as raised:
            go_v1._PreparedWorkerLaunch(go_v1.PLATFORM_LINUX)
    assert raised.value.code == go_v1.CODE_CONTROL_UNAVAILABLE
    _assert_closed(slot)
    assert real_getrlimit(resource.RLIMIT_NOFILE) == previous

    # Lower the soft limit to the slot itself so the prepare must raise
    # it to slot + 1; afterwards the lowered limit is back exactly.
    hard = previous[1]
    if hard != resource.RLIM_INFINITY and hard <= slot:
        capability = go_v1._PreparedWorkerLaunch(go_v1.PLATFORM_LINUX)
        try:
            assert capability._read_fd == slot
            assert real_getrlimit(resource.RLIMIT_NOFILE) == previous
        finally:
            capability.close_parent_copy()
        return
    real_setrlimit(resource.RLIMIT_NOFILE, (slot, hard))
    try:
        seen: list[tuple[int, int]] = []

        def recording_setrlimit(which, limits):
            seen.append(limits)
            return real_setrlimit(which, limits)

        with pytest.MonkeyPatch.context() as patch:
            patch.setattr(resource, "setrlimit", recording_setrlimit)
            capability = go_v1._PreparedWorkerLaunch(
                go_v1.PLATFORM_LINUX
            )
        try:
            assert capability._read_fd == slot
            assert seen[0] == (slot + 1, hard)
            assert seen[-1] == (slot, hard)
            assert real_getrlimit(resource.RLIMIT_NOFILE) == (slot, hard)
        finally:
            capability.close_parent_copy()
    finally:
        real_setrlimit(resource.RLIMIT_NOFILE, previous)
    assert real_getrlimit(resource.RLIMIT_NOFILE) == previous


@_POSIX_ONLY
def test_pipe_creation_failure_leaks_no_descriptor(monkeypatch):
    # A pipe creation failure propagates with the descriptor count
    # unchanged; the same preparation succeeds without the plant.
    monkeypatch.setattr(go_v1, "_WORKER_LAUNCH_FDS", _free_slots(1))
    before = _open_fd_count()

    def failing_pipe():
        raise OSError(errno.EMFILE, "planted pipe failure")

    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(os, "pipe", failing_pipe)
        with pytest.raises(OSError, match="planted pipe failure"):
            go_v1._PreparedWorkerLaunch(go_v1.PLATFORM_LINUX)
    after = _open_fd_count()
    if before is not None and after is not None:
        assert after == before

    capability = go_v1._PreparedWorkerLaunch(go_v1.PLATFORM_LINUX)
    capability.close_parent_copy()
    after = _open_fd_count()
    if before is not None and after is not None:
        assert after == before


def test_executor_missing_output_pipes_reports_start_failure(monkeypatch):
    # A tool process without capture pipes is killed, waited for and
    # reported as a start failure; a process with pipes runs normally.
    executable = Path(sys.executable).resolve()
    identity = SimpleNamespace(
        go=SimpleNamespace(path=executable),
        verify=lambda: None,
    )
    request = go_v1.ProcessRequest(
        executable=executable,
        identity=identity,
        arguments=(),
        cwd=Path.cwd(),
        environment={},
        timeout_seconds=5.0,
        output_limit=4096,
    )

    class PipelessProcess:
        stdout = None
        stderr = io.BytesIO()

        def __init__(self):
            self.killed = False
            self.waited = False

        def kill(self):
            self.killed = True

        def wait(self, timeout=None):
            self.waited = True
            return -9

    pipeless = PipelessProcess()
    monkeypatch.setattr(
        go_v1.subprocess, "Popen", lambda *a, **k: pipeless
    )
    result = go_v1.SubprocessProcessExecutor().run(request)
    assert result.start_failed is True
    assert result.returncode == -1
    assert result.detail == "cannot capture the fixed Go process output"
    assert pipeless.killed and pipeless.waited

    class PipedProcess:
        def __init__(self):
            self.stdout = io.BytesIO(b"out")
            self.stderr = io.BytesIO(b"")

        def poll(self):
            return 0

        def wait(self, timeout=None):
            return 0

    captured = {}

    def recording_popen(argv, **kwargs):
        captured["argv"] = argv
        captured["kwargs"] = kwargs
        return PipedProcess()

    monkeypatch.setattr(go_v1.subprocess, "Popen", recording_popen)
    result = go_v1.SubprocessProcessExecutor().run(request)
    assert result.start_failed is False
    assert result.returncode == 0
    assert result.stdout == b"out"
    assert captured["kwargs"]["stdin"] is subprocess.DEVNULL
    assert captured["kwargs"]["close_fds"] is True
    assert captured["kwargs"]["shell"] is False
    assert "pass_fds" not in captured["kwargs"]


def test_worker_client_missing_pipes_refuse():
    # Missing diagnostic, request or response pipes refuse with their
    # own diagnostics; present pipes carry a frame in each direction.
    pipeless = SimpleNamespace(stdin=None, stdout=None, stderr=None)
    with pytest.raises(
        go_v1.GoV1Error, match="worker diagnostic pipe is unavailable"
    ) as raised:
        go_v1._WorkerClient(
            SimpleNamespace(),
            pipeless,
            SimpleNamespace(),
            SimpleNamespace(),
            "nonce",
            "secret",
            b"x" * go_v1._WORKER_LAUNCH_SECRET_BYTES,
            SimpleNamespace(),
            time.monotonic() + 60.0,
        )
    assert raised.value.code == go_v1.CODE_WORKER_PROTOCOL_INVALID

    client = object.__new__(go_v1._WorkerClient)
    client.process = pipeless
    client._expired = threading.Event()
    client.deadline = time.monotonic() + 60.0
    client.nonce = "n" * 64
    with pytest.raises(
        go_v1.GoV1Error, match="worker request pipe is unavailable"
    ) as raised:
        client._send({"kind": "list", "nonce": client.nonce})
    assert raised.value.code == go_v1.CODE_WORKER_PROTOCOL_INVALID
    with pytest.raises(
        go_v1.GoV1Error, match="worker response pipe is unavailable"
    ) as raised:
        client._receive("ready")
    assert raised.value.code == go_v1.CODE_WORKER_PROTOCOL_INVALID

    process = SimpleNamespace(
        stdin=io.BytesIO(),
        stdout=io.BytesIO(),
        stderr=io.BytesIO(b"diagnostic"),
    )
    client = go_v1._WorkerClient(
        SimpleNamespace(),
        process,
        SimpleNamespace(),
        SimpleNamespace(),
        "n" * 64,
        "secret",
        b"x" * go_v1._WORKER_LAUNCH_SECRET_BYTES,
        SimpleNamespace(),
        time.monotonic() + 60.0,
    )
    try:
        client._send({"kind": "list", "nonce": client.nonce})
        process.stdin.seek(0)
        assert go_v1._read_message(process.stdin) == {
            "kind": "list",
            "nonce": client.nonce,
        }
        go_v1._write_message(
            process.stdout, {"kind": "ready", "nonce": client.nonce}
        )
        process.stdout.seek(0)
        assert client._receive("ready") == {
            "kind": "ready",
            "nonce": client.nonce,
        }
    finally:
        client._deadline_timer.cancel()
        client._stderr_thread.join(timeout=5)


@_POSIX_ONLY
def test_linux_guard_retains_one_descriptor_for_any_set_size(
    tmp_path, monkeypatch
):
    # The Linux identity guard keeps one event descriptor however large
    # the retained set grows, so retention cannot starve launch slots.
    from test_linux_identity_guard import _FakeInotifyLibc, _fake_guard

    class CountingLibc(_FakeInotifyLibc):
        def __init__(self):
            super().__init__()
            self.inits = 0
            self.watches = 0

        def inotify_init1(self, flags):
            self.inits += 1
            return super().inotify_init1(flags)

        def inotify_add_watch(self, fd, path, mask):
            self.watches += 1
            return super().inotify_add_watch(fd, path, mask)

    monkeypatch.setattr(go_v1, "_WORKER_LAUNCH_FDS", _free_slots(1))
    deltas = []
    watch_counts = []
    for count in (1, 24):
        retained_dir = tmp_path / f"set-{count}"
        retained_dir.mkdir()
        paths = tuple(
            retained_dir / f"tool-{index}" for index in range(count)
        )
        for path in paths:
            path.write_bytes(b"retained tool bytes")
        before = _open_fd_count()
        fake = CountingLibc()
        try:
            guard = _fake_guard(monkeypatch, paths, fake)
            try:
                assert fake.inits == 1
                assert guard._linux_fd == fake.read_fd
                assert len(guard._linux_targets_by_wd) >= count
                watch_counts.append(fake.watches)
                after = _open_fd_count()
                if before is not None and after is not None:
                    deltas.append(after - before)
                guard.verify()
                capability = go_v1._PreparedWorkerLaunch(
                    go_v1.PLATFORM_LINUX
                )
                capability.close_parent_copy()
            finally:
                guard.close()
        finally:
            fake.close()
    assert watch_counts[1] > watch_counts[0]
    if deltas:
        # Only the fake pipe pair is added, for the small and the large
        # retained set alike; the guard adds no per-path descriptor.
        assert deltas == [2, 2]


@_LINUX_ONLY
@pytest.mark.parametrize("order", ["twin_first", "worker_first"])
def test_fork_before_consumption_admits_at_most_one_consumer(
    tmp_path, monkeypatch, order
):
    # Deterministic order inside the worker script: with twin_first the
    # worker waits for the twin and both refuse; with worker_first the
    # twin blocks on a marker pipe until the direct child has consumed
    # (the positive control) and then finds no record left.
    outcomes = tmp_path / "outcomes"
    outcomes.mkdir()
    cache = tmp_path / "worker-cache"
    cache.mkdir()
    source_root = Path(go_v1.__file__).resolve().parents[2]
    script = f"""
import json, os, sys
sys.path.insert(0, {str(source_root)!r})
from csk.builds import go_v1 as g
ORDER = {order!r}
OUTCOMES = {str(outcomes)!r}
def attempt():
    try:
        context = g._consume_worker_launch_context()
        return {{"ok": True, "pid": os.getpid(), "parent_pid": context.parent_pid}}
    except g.GoV1Error as exc:
        return {{"ok": False, "pid": os.getpid(), "code": exc.code, "detail": exc.detail}}
def record(who, outcome):
    with open(OUTCOMES + "/" + who + ".json", "w") as handle:
        json.dump(outcome, handle)
marker_read, marker_write = os.pipe()
fork_result = os.fork()
if fork_result == 0:
    try:
        os.close(marker_write)
        if ORDER == "worker_first":
            os.read(marker_read, 1)
        os.close(marker_read)
        record("twin", attempt())
    finally:
        os._exit(0)
os.close(marker_read)
if ORDER == "twin_first":
    os.waitpid(fork_result, 0)
    os.close(marker_write)
    record("worker", attempt())
else:
    record("worker", attempt())
    os.write(marker_write, b"x")
    os.close(marker_write)
    os.waitpid(fork_result, 0)
"""
    monkeypatch.setattr(
        go_v1,
        "worker_argv",
        lambda _identity: (sys.executable, "-B", "-c", script),
    )
    domain = _bare_linux_domain()
    process = None
    try:
        process = domain.launch(_launch_identity(), cache)
        _stdout, stderr = _finish_child(process)
        assert process.returncode == 0, stderr
        worker = json.loads((outcomes / "worker.json").read_text())
        twin = json.loads((outcomes / "twin.json").read_text())
        if order == "twin_first":
            assert twin["ok"] is False
            assert twin["code"] == go_v1.CODE_WORKER_IDENTITY_INVALID
            assert twin["detail"] == (
                "hidden worker is not a direct child of its launch manager"
            )
            assert worker["ok"] is False
            assert worker["code"] == go_v1.CODE_WORKER_IDENTITY_INVALID
            assert worker["detail"] == (
                "hidden mode lacks one manager-owned launch capability"
            )
        else:
            assert worker["ok"] is True
            assert worker["pid"] == process.pid
            assert worker["parent_pid"] == os.getpid()
            assert twin["ok"] is False
            assert twin["code"] == go_v1.CODE_WORKER_IDENTITY_INVALID
            assert twin["detail"] == (
                "hidden mode lacks one manager-owned launch capability"
            )
        domain.terminate(process)
        with pytest.raises(ProcessLookupError):
            os.killpg(process.pid, 0)
    finally:
        _cleanup_child(process)
        domain.close()
