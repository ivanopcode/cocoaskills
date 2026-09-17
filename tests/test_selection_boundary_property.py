"""Property-style attacks for the descriptor-backed selection boundary.

The hook is intentionally installed once for the process.  The assertions do
not trust the selection walk's return value: they inspect the filesystem audit
events emitted while both public selection entry points run.
"""

from __future__ import annotations

import ast
import builtins
import hashlib
import io
import mmap
import os
import stat
import sys
import unicodedata
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest

from csk.sources import _selection_fs
from csk.sources import errors as source_errors
from csk.sources import selection
from csk.sources.selection import expand_collection, resolve_individual
from csk.sources.skillfile_v2 import CollectionSelector, IndividualSelector


_AUDIT_EVENT_NAMES = frozenset(
    {
        "open",
        "os.scandir",
        "os.listdir",
        "pathlib.Path.glob",
        "pathlib.Path.rglob",
    }
)
_AUDIT_EVENTS: list[tuple[str, tuple[Any, ...]]] = []


@dataclass(frozen=True)
class _OpenObservation:
    path: object
    flags: int
    dir_fd: int | None
    parent_identity: tuple[int, int] | None
    result_fd: int | None
    result_identity: tuple[int, int] | None


@dataclass(frozen=True)
class _ReadObservation:
    operation: str
    fd: int


_OPEN_OBSERVATIONS: list[_OpenObservation] = []
_SCANDIR_OBSERVATIONS: list[tuple[object, tuple[int, int] | None]] = []
_DESCRIPTOR_EVENTS: list[_selection_fs.DescriptorEvent] = []
_READ_OBSERVATIONS: list[_ReadObservation] = []
_OTHER_PRIMITIVE_OBSERVATIONS: list[str] = []
_SESSION_ROOT_INDEX: int | None = None
_SESSION_ROOT_IDENTITY: tuple[int, int] | None = None
_SESSION_EVENT_INDEX: int | None = None
_SESSION_AUDIT_INDEX: int | None = None
_SESSION_SCANDIR_INDEX: int | None = None
_SESSION_READ_INDEX: int | None = None
_SESSION_OTHER_INDEX: int | None = None
_SESSION_FD_OFFSETS: dict[int, tuple[tuple[int, int], int]] = {}


def _record_audit_event(event: str, args: tuple[Any, ...]) -> None:
    if event in _AUDIT_EVENT_NAMES:
        _AUDIT_EVENTS.append((event, args))


sys.addaudithook(_record_audit_event)


def _write_skill(directory: Path, name: str = "review") -> bytes:
    directory.mkdir(parents=True, exist_ok=True)
    raw = (
        f"---\nname: {name}\ndescription: Property fixture\n---\n"
        "# Property fixture\n"
    ).encode("utf-8")
    (directory / "SKILL.md").write_bytes(raw)
    return raw


def _audit_open_matches(
    args: tuple[Any, ...], observation: _OpenObservation
) -> bool:
    """Return whether one audit ``open`` belongs to the descriptor seam."""

    if not args:
        return False
    audit_path = args[0]
    observed_path = observation.path
    if isinstance(audit_path, int) or not isinstance(observed_path, (str, bytes)):
        return False
    try:
        return os.fsdecode(audit_path) == os.fsdecode(observed_path)
    except (TypeError, ValueError, UnicodeError):
        return False


def _collection(directory: str, include: tuple[str, ...]) -> CollectionSelector:
    return CollectionSelector(
        from_alias="local", directory=directory, include=include, exclude=()
    )


def _run_entry(root: Path, entry: str, directory: str) -> object:
    if entry == "collection":
        return expand_collection(root, _collection(".", (directory,)))
    return resolve_individual(
        root,
        IndividualSelector(name="review", from_alias="local", directory=directory),
    )


class _TrackedStream:
    """Record a read on a stream opened before the Phase-B window."""

    def __init__(self, stream: Any, fd: int) -> None:
        self._stream = stream
        self._fd = fd

    def read(self, size: int) -> bytes:
        _READ_OBSERVATIONS.append(_ReadObservation("stream.read", self._fd))
        return self._stream.read(size)


def _install_session_marker(monkeypatch: pytest.MonkeyPatch) -> list[int]:
    """Install the provenance seam before session setup starts.

    Python's ``open`` audit event does not carry ``dir_fd``.  Test-only
    wrappers around the actual ``os.open`` and read primitives record the
    supplied parent, the returned descriptor and the descriptor used for each
    read. This catches capabilities acquired before the observation window,
    not just new outside opens.
    """

    global _SESSION_FD_OFFSETS
    _SESSION_FD_OFFSETS = _capture_regular_file_offsets()
    del _AUDIT_EVENTS[:]
    del _OPEN_OBSERVATIONS[:]
    del _SCANDIR_OBSERVATIONS[:]
    del _DESCRIPTOR_EVENTS[:]
    del _READ_OBSERVATIONS[:]
    del _OTHER_PRIMITIVE_OBSERVATIONS[:]
    global _SESSION_ROOT_INDEX
    _SESSION_ROOT_INDEX = None
    global _SESSION_ROOT_IDENTITY, _SESSION_EVENT_INDEX
    _SESSION_ROOT_IDENTITY = None
    _SESSION_EVENT_INDEX = None
    global _SESSION_AUDIT_INDEX, _SESSION_SCANDIR_INDEX
    _SESSION_AUDIT_INDEX = None
    _SESSION_SCANDIR_INDEX = None
    global _SESSION_READ_INDEX
    _SESSION_READ_INDEX = None
    global _SESSION_OTHER_INDEX
    _SESSION_OTHER_INDEX = None
    original_open = os.open
    original_scandir = os.scandir
    original_listdir = os.listdir
    original_read = os.read
    original_pread = getattr(os, "pread", None)
    original_preadv = getattr(os, "preadv", None)
    original_readv = getattr(os, "readv", None)
    original_builtin_open = builtins.open
    original_io_open = io.open
    original_mmap = mmap.mmap
    original_path_open = Path.open
    original_path_read_bytes = Path.read_bytes
    original_path_read_text = Path.read_text
    original_path_iterdir = Path.iterdir
    original_path_glob = Path.glob
    original_path_rglob = Path.rglob

    def traced_open(path: object, flags: int, *args: object, **kwargs: object) -> int:
        dir_fd_value = kwargs.get("dir_fd")
        dir_fd = dir_fd_value if isinstance(dir_fd_value, int) else None
        parent_identity: tuple[int, int] | None = None
        if dir_fd is not None:
            try:
                parent_stat = os.fstat(dir_fd)
                parent_identity = (parent_stat.st_dev, parent_stat.st_ino)
            except (OSError, RuntimeError, ValueError, UnicodeError):
                parent_identity = None
        try:
            descriptor = original_open(path, flags, *args, **kwargs)
        except BaseException:
            _OPEN_OBSERVATIONS.append(
                _OpenObservation(path, flags, dir_fd, parent_identity, None, None)
            )
            raise
        try:
            result_stat = os.fstat(descriptor)
            result_identity = (result_stat.st_dev, result_stat.st_ino)
        except (OSError, RuntimeError, ValueError, UnicodeError):
            result_identity = None
        _OPEN_OBSERVATIONS.append(
            _OpenObservation(
                path, flags, dir_fd, parent_identity, descriptor, result_identity
            )
        )
        return descriptor

    monkeypatch.setattr(os, "open", traced_open)

    def traced_other(name: str) -> None:
        _OTHER_PRIMITIVE_OBSERVATIONS.append(name)

    def traced_builtin_open(*args: object, **kwargs: object):  # type: ignore[no-untyped-def]
        traced_other("builtins.open")
        return original_builtin_open(*args, **kwargs)

    monkeypatch.setattr(builtins, "open", traced_builtin_open)

    def traced_io_open(*args: object, **kwargs: object):  # type: ignore[no-untyped-def]
        traced_other("io.open")
        return original_io_open(*args, **kwargs)

    monkeypatch.setattr(io, "open", traced_io_open)

    def traced_mmap(*args: object, **kwargs: object):  # type: ignore[no-untyped-def]
        traced_other("mmap.mmap")
        return original_mmap(*args, **kwargs)

    monkeypatch.setattr(mmap, "mmap", traced_mmap)

    def traced_path_open(self: Path, *args: object, **kwargs: object):  # type: ignore[no-untyped-def]
        traced_other("Path.open")
        return original_path_open(self, *args, **kwargs)

    monkeypatch.setattr(Path, "open", traced_path_open)

    def traced_path_read_bytes(self: Path, *args: object, **kwargs: object) -> bytes:
        traced_other("Path.read_bytes")
        return original_path_read_bytes(self, *args, **kwargs)

    monkeypatch.setattr(Path, "read_bytes", traced_path_read_bytes)

    def traced_path_read_text(self: Path, *args: object, **kwargs: object) -> str:
        if type(self).__name__ != "_SnapshotPath":
            traced_other("Path.read_text")
        return original_path_read_text(self, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", traced_path_read_text)

    def traced_path_iterdir(self: Path, *args: object, **kwargs: object):  # type: ignore[no-untyped-def]
        traced_other("Path.iterdir")
        return original_path_iterdir(self, *args, **kwargs)

    monkeypatch.setattr(Path, "iterdir", traced_path_iterdir)

    def traced_path_glob(self: Path, *args: object, **kwargs: object):  # type: ignore[no-untyped-def]
        traced_other("Path.glob")
        return original_path_glob(self, *args, **kwargs)

    monkeypatch.setattr(Path, "glob", traced_path_glob)

    def traced_path_rglob(self: Path, *args: object, **kwargs: object):  # type: ignore[no-untyped-def]
        traced_other("Path.rglob")
        return original_path_rglob(self, *args, **kwargs)

    monkeypatch.setattr(Path, "rglob", traced_path_rglob)

    def traced_read(fd: int, size: int) -> bytes:
        _READ_OBSERVATIONS.append(_ReadObservation("os.read", fd))
        return original_read(fd, size)

    monkeypatch.setattr(os, "read", traced_read)

    if original_pread is not None:
        def traced_pread(fd: int, size: int, offset: int) -> bytes:
            _READ_OBSERVATIONS.append(_ReadObservation("os.pread", fd))
            return original_pread(fd, size, offset)

        monkeypatch.setattr(os, "pread", traced_pread)

    if original_preadv is not None:
        def traced_preadv(
            fd: int,
            buffers: object,
            offset: int,
            flags: int = 0,
        ) -> int:
            _READ_OBSERVATIONS.append(_ReadObservation("os.preadv", fd))
            return original_preadv(fd, buffers, offset, flags)  # type: ignore[arg-type]

        monkeypatch.setattr(os, "preadv", traced_preadv)

    if original_readv is not None:
        def traced_readv(fd: int, buffers: object) -> int:
            _READ_OBSERVATIONS.append(_ReadObservation("os.readv", fd))
            return original_readv(fd, buffers)  # type: ignore[arg-type]

        monkeypatch.setattr(os, "readv", traced_readv)

    def traced_listdir(*args: object, **kwargs: object):  # type: ignore[no-untyped-def]
        traced_other("os.listdir")
        return original_listdir(*args, **kwargs)

    monkeypatch.setattr(os, "listdir", traced_listdir)

    def traced_scandir(path: object):  # type: ignore[no-untyped-def]
        identity: tuple[int, int] | None = None
        if isinstance(path, int):
            try:
                value = os.fstat(path)
                identity = (value.st_dev, value.st_ino)
            except (OSError, RuntimeError, ValueError, UnicodeError):
                pass
        _SCANDIR_OBSERVATIONS.append((path, identity))
        return original_scandir(path)

    monkeypatch.setattr(os, "scandir", traced_scandir)
    markers: list[int] = []

    def traced_descriptor_event(event: _selection_fs.DescriptorEvent) -> None:
        global _SESSION_ROOT_INDEX, _SESSION_ROOT_IDENTITY
        global _SESSION_EVENT_INDEX, _SESSION_AUDIT_INDEX, _SESSION_SCANDIR_INDEX
        global _SESSION_READ_INDEX
        global _SESSION_OTHER_INDEX
        _DESCRIPTOR_EVENTS.append(event)
        if event.operation == "phase-b-root" and _SESSION_ROOT_INDEX is None:
            root_indices = [
                index
                for index, observation in enumerate(_OPEN_OBSERVATIONS)
                if observation.result_fd == event.result_fd
            ]
            assert root_indices, "phase-B root was not recorded by the open seam"
            _SESSION_ROOT_INDEX = root_indices[-1]
            _SESSION_ROOT_IDENTITY = event.result_identity
            event_indices = [
                index
                for index, descriptor_event in enumerate(_DESCRIPTOR_EVENTS)
                if descriptor_event.operation == "open"
                and descriptor_event.result_fd == event.result_fd
            ]
            assert event_indices, "phase-B root was not recorded by the descriptor seam"
            _SESSION_EVENT_INDEX = event_indices[-1]
            audit_indices = [
                index
                for index, (audit_event, args) in enumerate(_AUDIT_EVENTS)
                if audit_event == "open"
                and args
                and args[0] == _OPEN_OBSERVATIONS[_SESSION_ROOT_INDEX].path
            ]
            assert audit_indices, "phase-B root was not recorded by the audit hook"
            _SESSION_AUDIT_INDEX = audit_indices[-1]
            _SESSION_SCANDIR_INDEX = len(_SCANDIR_OBSERVATIONS)
            _SESSION_READ_INDEX = len(_READ_OBSERVATIONS)
            _SESSION_OTHER_INDEX = len(_OTHER_PRIMITIVE_OBSERVATIONS)
            markers.append(_SESSION_ROOT_INDEX)

    monkeypatch.setattr(_selection_fs, "_TRACE_SINK", traced_descriptor_event)
    return markers


def _capture_regular_file_offsets() -> dict[int, tuple[tuple[int, int], int]]:
    """Snapshot seekable regular descriptors before the Phase-B window."""

    if os.name != "posix":
        return {}
    try:
        descriptor_names = os.listdir("/dev/fd")
    except (OSError, RuntimeError, ValueError, UnicodeError):
        return {}
    offsets: dict[int, tuple[tuple[int, int], int]] = {}
    for descriptor_name in descriptor_names:
        try:
            descriptor = int(descriptor_name)
            value = os.fstat(descriptor)
            if not stat.S_ISREG(value.st_mode):
                continue
            offset = os.lseek(descriptor, 0, os.SEEK_CUR)
        except (OSError, ValueError, UnicodeError):
            continue
        offsets[descriptor] = ((value.st_dev, value.st_ino), offset)
    return offsets


def _read_provenance_is_owned(
    observation: _ReadObservation, owned_fds: set[int]
) -> bool:
    """Return whether a read primitive used a descriptor opened in Phase B."""

    return observation.fd in owned_fds


def _assert_descriptor_boundary(
    root: Path,
    start: int,
    *,
    allow_root_open: bool = False,
    require_read_seam: bool = False,
) -> None:
    """Assert provenance, not just filename shape, for post-root operations."""

    root_text = os.path.abspath(os.fspath(root))

    def is_path_below_root(value: object) -> bool:
        try:
            candidate = os.path.abspath(os.fspath(value))
            return os.path.commonpath((root_text, candidate)) == root_text
        except (TypeError, ValueError, OSError):
            return False

    root_indices = [
        index
        for index, observation in enumerate(_OPEN_OBSERVATIONS[: start + 1])
        if isinstance(observation.path, str)
        and os.path.abspath(observation.path) == root_text
        and observation.result_identity is not None
    ]
    assert root_indices, "the confined source root was never opened"
    confined_root_index = root_indices[-1]
    root_observation = _OPEN_OBSERVATIONS[confined_root_index]
    assert root_observation.result_identity is not None
    root_identity = (
        _SESSION_ROOT_IDENTITY
        if _SESSION_ROOT_IDENTITY is not None
        else root_observation.result_identity
    )
    known_inside = {root_identity}

    post_setup_start = (
        _SESSION_ROOT_INDEX
        if _SESSION_ROOT_INDEX is not None
        else confined_root_index + 1
    )
    nofollow_flag = getattr(os, "O_NOFOLLOW", 0)
    phase_b_owned_fds: set[int] = set()
    for index, observation in enumerate(
        _OPEN_OBSERVATIONS[post_setup_start:], start=post_setup_start
    ):
        path = observation.path
        if observation.dir_fd is None:
            if (
                (allow_root_open or index == _SESSION_ROOT_INDEX)
                and isinstance(path, str)
                and os.path.abspath(path) == root_text
            ):
                if observation.result_fd is not None:
                    phase_b_owned_fds.add(observation.result_fd)
                continue
            if os.name != "posix" and is_path_below_root(path):
                # Windows has no descriptor-relative openat.  The production
                # fallback checks lstat/reparse identity and re-stats after
                # opening; the path-below-root assertion is its weaker test
                # oracle bound (no atomic no-follow at open time).
                continue
            pytest.fail(f"post-root open had no confined parent descriptor: {path!r}")
        assert observation.parent_identity in known_inside, (
            "open used a descriptor not descended from the source root: "
            f"path={path!r}, parent={observation.parent_identity!r}"
        )
        if isinstance(path, str):
            assert path not in ("..", "."), (
                f"post-root open traversed a non-descending component: {path!r}"
            )
            assert not os.path.isabs(path), (
                f"post-root open named an absolute path: {path!r}"
            )
        if os.name == "posix":
            assert nofollow_flag and observation.flags & nofollow_flag, (
                "post-root open did not request atomic no-follow traversal: "
                f"path={path!r}, flags={observation.flags:#x}"
            )
        if observation.result_identity is not None:
            known_inside.add(observation.result_identity)
        if observation.result_fd is not None:
            phase_b_owned_fds.add(observation.result_fd)

    # The production seam carries the actual parent identity and is the
    # provenance oracle.  The audit event does not expose ``dir_fd``; this
    # assertion is intentionally independent of its filename-shaped proxy.
    event_start = _SESSION_EVENT_INDEX if _SESSION_EVENT_INDEX is not None else 0
    phase_b_descriptor_events = _DESCRIPTOR_EVENTS[event_start:]
    for event_offset, event in enumerate(phase_b_descriptor_events):
        if event.operation == "open":
            if event_offset == 0:
                assert event.parent_fd is None
                assert event.result_identity == root_identity
                continue
            assert event.parent_fd is not None
            assert event.parent_identity in known_inside, (
                "descriptor seam opened outside the source capability: "
                f"path={event.path!r}, parent={event.parent_identity!r}"
            )
            if isinstance(event.path, str):
                assert event.path not in ("..", ".")
                assert not os.path.isabs(event.path)
            if event.result_identity is not None:
                known_inside.add(event.result_identity)
        elif event.operation == "scandir":
            assert event.parent_identity in known_inside, (
                "descriptor seam listed outside the source capability: "
                f"path={event.path!r}, parent={event.parent_identity!r}"
            )

    phase_b_open_events = [
        event for event in phase_b_descriptor_events if event.operation == "open"
    ]
    phase_b_open_observations = [
        observation
        for observation in _OPEN_OBSERVATIONS[post_setup_start:]
    ]
    assert len(phase_b_open_events) == len(phase_b_open_observations), (
        "every phase-B os.open call must come from the production descriptor seam"
    )
    for event, observation in zip(
        phase_b_open_events, phase_b_open_observations, strict=True
    ):
        assert event.path == observation.path
        assert event.parent_fd == observation.dir_fd
        assert event.parent_identity == observation.parent_identity
        assert event.result_fd == observation.result_fd
        assert event.result_identity == observation.result_identity

    phase_b_read_events = [
        event for event in phase_b_descriptor_events if event.operation == "read"
    ]
    for event in phase_b_read_events:
        assert event.parent_fd is not None
        assert event.parent_identity in known_inside, (
            "read seam used a parent outside the source capability: "
            f"name={event.path!r}, parent={event.parent_identity!r}"
        )
        assert event.result_fd in phase_b_owned_fds, (
            "read seam used a descriptor not opened during Phase B: "
            f"name={event.path!r}, fd={event.result_fd!r}"
        )
        assert isinstance(event.path, str)
        assert event.path not in ("..", ".")
        assert not os.path.isabs(event.path)
        assert "/" not in event.path and "\\" not in event.path

    read_start = _SESSION_READ_INDEX if _SESSION_READ_INDEX is not None else 0
    phase_b_read_observations = _READ_OBSERVATIONS[read_start:]
    observed_read_fds = {observation.fd for observation in phase_b_read_observations}
    offset_reads: list[_ReadObservation] = []
    for descriptor, (identity, initial_offset) in _SESSION_FD_OFFSETS.items():
        if descriptor in phase_b_owned_fds:
            continue
        try:
            current = os.fstat(descriptor)
            if (current.st_dev, current.st_ino) != identity:
                continue
            current_offset = os.lseek(descriptor, 0, os.SEEK_CUR)
        except (OSError, ValueError, UnicodeError):
            continue
        if current_offset != initial_offset and descriptor not in observed_read_fds:
            offset_reads.append(_ReadObservation("stream.read", descriptor))
    phase_b_read_observations.extend(offset_reads)
    for observation in phase_b_read_observations:
        assert _read_provenance_is_owned(observation, phase_b_owned_fds), (
            "read primitive used a descriptor not opened during Phase B: "
            f"operation={observation.operation!r}, fd={observation.fd!r}"
        )
    other_start = _SESSION_OTHER_INDEX if _SESSION_OTHER_INDEX is not None else 0
    assert not _OTHER_PRIMITIVE_OBSERVATIONS[other_start:], (
        "phase-B filesystem primitive bypassed the owned descriptor seam: "
        f"{_OTHER_PRIMITIVE_OBSERVATIONS[other_start:]!r}"
    )
    if require_read_seam:
        assert phase_b_read_events, (
            "phase-B member bytes were not emitted by the production read seam"
        )

    audit_start = _SESSION_AUDIT_INDEX if _SESSION_AUDIT_INDEX is not None else 0
    phase_b_audit_events = _AUDIT_EVENTS[audit_start:]
    phase_b_audit_opens = [
        args for event, args in phase_b_audit_events if event == "open"
    ]
    assert len(phase_b_audit_opens) == len(phase_b_open_observations), (
        "every phase-B audit open must belong to the production open seam"
    )
    for args, observation in zip(
        phase_b_audit_opens, phase_b_open_observations, strict=True
    ):
        assert _audit_open_matches(args, observation), (
            "phase-B audit open has no production descriptor provenance: "
            f"args={args!r}"
        )

    for audit_offset, (event, args) in enumerate(phase_b_audit_events):
        if event == "open" and args:
            path = args[0]
            if audit_offset == 0:
                assert isinstance(path, (str, bytes))
                assert os.path.abspath(os.fsdecode(path)) == root_text
            elif isinstance(path, (str, bytes)):
                native = os.fsdecode(path)
                assert not os.path.isabs(native), (
                    f"path-based open after the phase transition: {args!r}"
                )
                assert "/" not in native and "\\" not in native, (
                    f"multi-component path open after the phase transition: {args!r}"
                )
        if event == "os.listdir" and args:
            path = args[0]
            if os.name == "posix" or not is_path_below_root(path):
                pytest.fail(f"directory listing escaped the source root: {args!r}")
        if event in {"pathlib.Path.glob", "pathlib.Path.rglob"}:
            pytest.fail(f"path-based walker event after root open: {event} {args!r}")

    scandir_start = (
        _SESSION_SCANDIR_INDEX if _SESSION_SCANDIR_INDEX is not None else 0
    )
    phase_b_scandir_events = [
        event for event in phase_b_descriptor_events if event.operation == "scandir"
    ]
    phase_b_scandir_observations = _SCANDIR_OBSERVATIONS[scandir_start:]
    assert len(phase_b_scandir_events) == len(phase_b_scandir_observations), (
        "every phase-B os.scandir call must come from the production descriptor seam"
    )
    for event, observation in zip(
        phase_b_scandir_events, phase_b_scandir_observations, strict=True
    ):
        path, identity = observation
        assert event.path == path
        assert event.parent_identity == identity
    assert sum(event == "os.scandir" for event, _ in phase_b_audit_events) == len(
        phase_b_scandir_events
    ), "every phase-B scandir audit event must belong to the descriptor seam"
    # Inspect setup too.  A directory listing is never needed to establish a
    # managed-root boundary, so an outside ``scandir`` is an R1 violation even
    # when it happens before the confined phase marker is installed.
    for path, identity in _SCANDIR_OBSERVATIONS[scandir_start:]:
        if isinstance(path, int):
            assert identity in known_inside, (
                f"directory walker used a descriptor outside the source root: {path!r}"
            )
        else:
            assert os.name != "posix" and is_path_below_root(path), (
                f"path-based directory walker used after root open: {path!r}"
            )


def test_selection_modules_have_one_filesystem_primitive_seam() -> None:
    """The source-selection modules contain no filesystem primitive bypass."""

    source_dir = Path(_selection_fs.__file__).parent
    allowed = {"_selection_fs.py"}
    module_calls = {
        "open",
        "read",
        "readv",
        "pread",
        "preadv",
        "scandir",
        "listdir",
        "lstat",
        "stat",
        "readlink",
    }
    path_calls = {
        "open",
        "read_bytes",
        "read_text",
        "glob",
        "rglob",
        "iterdir",
    }
    violations: list[str] = []

    class Visitor(ast.NodeVisitor):
        def __init__(self) -> None:
            self.class_stack: list[str] = []

        def visit_ClassDef(self, node: ast.ClassDef) -> None:
            self.class_stack.append(node.name)
            self.generic_visit(node)
            self.class_stack.pop()

        def visit_Call(self, node: ast.Call) -> None:
            function = node.func
            if isinstance(function, ast.Name) and function.id == "open":
                violations.append(f"{path.name}:{node.lineno}: open")
            elif isinstance(function, ast.Attribute):
                receiver = function.value
                if (
                    isinstance(receiver, ast.Name)
                    and receiver.id in {"os", "io", "mmap"}
                    and function.attr in module_calls
                ):
                    violations.append(
                        f"{path.name}:{node.lineno}: "
                        f"{receiver.id}.{function.attr}"
                    )
                elif function.attr in path_calls:
                    snapshot_method = self.class_stack == ["_SnapshotPath"]
                    session_constructor = (
                        function.attr == "open"
                        and isinstance(receiver, ast.Name)
                        and receiver.id == "SelectionSession"
                    )
                    if not snapshot_method and not session_constructor:
                        violations.append(
                            f"{path.name}:{node.lineno}: .{function.attr}()"
                        )
            self.generic_visit(node)

    scanned = 0
    for path in sorted(source_dir.glob("*.py")):
        if path.name in allowed:
            continue
        scanned += 1
        Visitor().visit(ast.parse(path.read_text(encoding="utf-8"), filename=str(path)))
    assert scanned > 0
    assert not violations, "filesystem primitive bypasses owned seam: " + ", ".join(
        violations
    )


def _identity_of_descriptor_from_observations(
    descriptor: int,
) -> tuple[int, int] | None:
    # Kept as a tiny diagnostic helper for attack tests that inspect a live
    # descriptor; the property itself records scandir identity at event time.
    try:
        value = os.fstat(descriptor)
    except (OSError, RuntimeError, ValueError, UnicodeError):
        return None
    return (value.st_dev, value.st_ino)


@pytest.mark.parametrize("entry", ["collection", "individual"])
@pytest.mark.parametrize(
    "attack",
    [
        "symlink-out",
        "self-loop",
        "two-link-cycle",
        "hard-link",
        "case-variant",
        "unicode-variant",
        "permission-error",
        "second-walker",
        "mid-walk-swap",
    ],
)
def test_selection_boundary_property(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    entry: str,
    attack: str,
) -> None:
    """Every R1 attack family keeps public selection inside the source root."""

    monkeypatch.setenv("CSK_CONFIG", str(tmp_path / "home" / "config.json"))
    root = tmp_path / "source"
    root.mkdir()
    target_directory = "review"
    outside = tmp_path / "outside"
    _write_skill(outside, "review")
    markers = _install_session_marker(monkeypatch)

    if attack == "symlink-out":
        (root / "review").symlink_to(outside, target_is_directory=True)
    elif attack == "self-loop":
        (root / "review").symlink_to("review")
    elif attack == "two-link-cycle":
        (root / "review").symlink_to("cycle-b")
        (root / "cycle-b").symlink_to("review")
    elif attack == "hard-link":
        member = root / "review"
        member.mkdir()
        raw = _write_skill(outside, "review")
        os.link(outside / "SKILL.md", member / "SKILL.md")
        assert (member / "SKILL.md").read_bytes() == raw
    elif attack == "case-variant":
        _write_skill(root / "CaseSkill", "review")
        target_directory = "caseskill"
    elif attack == "unicode-variant":
        authored = "reviéw"
        variant = unicodedata.normalize("NFD", authored)
        _write_skill(root / authored, "review")
        target_directory = variant
    elif attack == "permission-error":
        _write_skill(root / "review", "review")
        if entry == "collection":
            def denied_child_directories(self, base, *, context):  # type: ignore[no-untyped-def]
                raise PermissionError("property-injected scandir denial")

            monkeypatch.setattr(
                _selection_fs.SelectionSession,
                "child_directories",
                denied_child_directories,
            )
        else:
            def denied_snapshot(self, member, *, label):  # type: ignore[no-untyped-def]
                raise PermissionError("property-injected scandir denial")

            monkeypatch.setattr(
                _selection_fs.SelectionSession,
                "snapshot_member",
                denied_snapshot,
            )
    elif attack == "second-walker":
        member = root / "review"
        _write_skill(member, "review")
        (member / "references").mkdir()
        (member / "references" / "notes.md").write_text(
            "# notes\n", encoding="utf-8"
        )
        (member / "agent-skill.json").write_text(
            '{"schema_version":2,"runtime_roots":["references"]}',
            encoding="utf-8",
        )
    elif attack == "mid-walk-swap":
        member = root / "review"
        _write_skill(member, "review")
        swapped = False
        original_open_child = _selection_fs._open_child_directory

        def swap_before_open(
            parent_fd: int, name: str, *, parent_path: Path | None = None
        ) -> int:
            nonlocal swapped
            if name == "review" and not swapped:
                member.rename(root / "review-before-swap")
                member.symlink_to(outside, target_is_directory=True)
                swapped = True
            return original_open_child(parent_fd, name, parent_path=parent_path)

        monkeypatch.setattr(
            _selection_fs, "_open_child_directory", swap_before_open
        )
    else:  # pragma: no cover - the parametrized table is closed above.
        raise AssertionError(attack)

    try:
        try:
            _run_entry(root, entry, target_directory)
        except source_errors.SourceError:
            pass
        if markers:
            _assert_descriptor_boundary(root, markers[-1])
        else:
            # Phase A now refuses an unresolved selector before the
            # transition. There is then no confined phase to audit; assert
            # that the fresh phase-B root was not acquired accidentally.
            assert not any(
                event.operation == "phase-b-root" for event in _DESCRIPTOR_EVENTS
            )
    finally:
        # Keep the global audit buffer bounded across the parametrized corpus.
        del _AUDIT_EVENTS[:]
        del _OPEN_OBSERVATIONS[:]
        del _SCANDIR_OBSERVATIONS[:]
        del _DESCRIPTOR_EVENTS[:]
        global _SESSION_ROOT_INDEX
        _SESSION_ROOT_INDEX = None


@pytest.mark.parametrize("entry", ["individual", "collection"])
def test_r1_setup_does_not_list_outside(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, entry: str
) -> None:
    """Session setup does not enumerate a parent of the confined root."""

    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("CSK_CONFIG", str(home / "config.json"))
    root = tmp_path / "source"
    _write_skill(root / "review", "review")
    parent_identity = (tmp_path.stat().st_dev, tmp_path.stat().st_ino)
    original_scandir = os.scandir
    outside_scans: list[object] = []

    def traced_scandir(path: object):  # type: ignore[no-untyped-def]
        if isinstance(path, int):
            value = os.fstat(path)
            if (value.st_dev, value.st_ino) == parent_identity:
                outside_scans.append(path)
        return original_scandir(path)

    monkeypatch.setattr(os, "scandir", traced_scandir)
    _run_entry(root, entry, "review")
    assert not outside_scans


@pytest.mark.parametrize("entry", ["individual", "collection"])
def test_r1_setup_opens_no_outside_ancestor(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, entry: str
) -> None:
    """The full lifecycle has no parent open after the Phase-B transition."""

    monkeypatch.setenv("CSK_CONFIG", str(tmp_path / "home" / "config.json"))
    root = tmp_path / "source"
    _write_skill(root / "review", "review")
    markers = _install_session_marker(monkeypatch)
    _run_entry(root, entry, "review")
    assert markers
    _assert_descriptor_boundary(root, markers[-1], require_read_seam=True)


def test_phase_b_fresh_root_rejects_identity_change(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The fresh transition descriptor must match Phase A's frozen root."""

    monkeypatch.setenv("CSK_CONFIG", str(tmp_path / "home" / "config.json"))
    root = tmp_path / "source"
    _write_skill(root / "review", "review")
    original_open_root = _selection_fs._open_root
    swapped = False

    def replace_after_phase_a(path: Path, *, phase: str = "a") -> int:
        nonlocal swapped
        descriptor = original_open_root(path, phase=phase)
        if phase == "a" and not swapped:
            swapped = True
            moved = path.with_name("source-before-transition")
            path.rename(moved)
            path.mkdir()
        return descriptor

    monkeypatch.setattr(_selection_fs, "_open_root", replace_after_phase_a)
    with pytest.raises(source_errors.SourceError) as excinfo:
        resolve_individual(
            root,
            IndividualSelector(name="review", from_alias="local", directory="review"),
        )
    assert swapped
    assert excinfo.value.code == source_errors.CODE_SELECTION_INVALID
    assert "changed between preflight and selection" in excinfo.value.detail


@pytest.mark.parametrize("entry", ["individual", "collection"])
@pytest.mark.parametrize("failure", [PermissionError, RuntimeError, ValueError])
def test_phase_a_resolution_failure_refuses_without_partial_record(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    entry: str,
    failure: type[Exception],
) -> None:
    """A reached Phase-A component failure cannot become boundary absence."""

    monkeypatch.setenv("CSK_CONFIG", str(tmp_path / "home" / "config.json"))
    root = tmp_path / "source"
    _write_skill(root / "nested" / "review")
    (root / "nested" / ".agents").symlink_to("review", target_is_directory=True)

    def run() -> object:
        if entry == "individual":
            return resolve_individual(
                root,
                IndividualSelector(
                    name="review", from_alias="local", directory="nested/review"
                ),
            )
        return expand_collection(root, _collection("nested", ("review",)))

    with pytest.raises(source_errors.SourceError) as control:
        run()
    assert control.value.code == source_errors.CODE_OUTPUT_OVERLAP

    original_prepare = _selection_fs._prepare_preflight
    phase_a = True

    def prepare(*args: object, **kwargs: object):  # type: ignore[no-untyped-def]
        nonlocal phase_a
        try:
            return original_prepare(*args, **kwargs)
        finally:
            phase_a = False

    monkeypatch.setattr(_selection_fs, "_prepare_preflight", prepare)
    original_open_child = _selection_fs._open_child_directory
    hits: list[str] = []

    def denied(
        parent_fd: int, name: str, *, parent_path: Path | None = None
    ) -> int:
        if phase_a and parent_path == root and name == "nested":
            hits.append(name)
            raise failure("injected Phase-A resolution failure")
        return original_open_child(parent_fd, name, parent_path=parent_path)

    monkeypatch.setattr(_selection_fs, "_open_child_directory", denied)
    markers = _install_session_marker(monkeypatch)
    with pytest.raises(source_errors.SourceError) as excinfo:
        run()
    assert hits == ["nested"]
    assert isinstance(excinfo.value.__cause__, failure)
    assert markers == []


@pytest.mark.parametrize("entry", ["individual", "collection"])
@pytest.mark.parametrize("managed_shape", ["direct", "alias"])
def test_replaced_managed_descendant_cannot_escape_frozen_record(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    entry: str,
    managed_shape: str,
) -> None:
    """Phase B rejects a managed descendant replaced after Phase A."""

    monkeypatch.setenv("CSK_CONFIG", str(tmp_path / "home" / "config.json"))
    root = tmp_path / "source"
    if managed_shape == "direct":
        _write_skill(root / ".agents" / "review")
    else:
        _write_skill(root / "nested" / "managed" / "review")
        (root / ".agents").symlink_to("nested/managed", target_is_directory=True)

    def run() -> object:
        if entry == "individual":
            return resolve_individual(
                root,
                IndividualSelector(
                    name="review", from_alias="local", directory=".agents/review"
                ),
            )
        return expand_collection(root, _collection(".agents", ("review",)))

    with pytest.raises(source_errors.SourceError) as control:
        run()
    assert control.value.code == source_errors.CODE_OUTPUT_OVERLAP

    original_prepare = _selection_fs._prepare_preflight
    changed: list[bool] = []

    def replace(*args: object, **kwargs: object):  # type: ignore[no-untyped-def]
        record = original_prepare(*args, **kwargs)
        (root / ".agents").rename(root / "retired")
        if managed_shape == "direct":
            _write_skill(root / ".agents" / "review")
        else:
            _write_skill(root / "replacement" / "review")
            (root / ".agents").symlink_to("replacement", target_is_directory=True)
        changed.append(True)
        return record

    monkeypatch.setattr(_selection_fs, "_prepare_preflight", replace)
    markers = _install_session_marker(monkeypatch)
    with pytest.raises(source_errors.SourceError) as excinfo:
        run()
    assert changed == [True]
    assert markers
    assert "changed after Phase A" in excinfo.value.detail


@pytest.mark.parametrize("entry", ["individual", "collection"])
@pytest.mark.parametrize("managed_shape", ["direct", "alias"])
def test_replaced_nested_managed_descendant_cannot_escape_frozen_record(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    entry: str,
    managed_shape: str,
) -> None:
    """A managed root below the source root is also frozen per component."""

    monkeypatch.setenv("CSK_CONFIG", str(tmp_path / "home" / "config.json"))
    root = tmp_path / "source"
    nested = root / "nested"
    if managed_shape == "direct":
        _write_skill(nested / ".agents" / "review")
    else:
        _write_skill(nested / "managed" / "review")
        (nested / ".agents").symlink_to("managed", target_is_directory=True)

    def run() -> object:
        if entry == "individual":
            return resolve_individual(
                root,
                IndividualSelector(
                    name="review",
                    from_alias="local",
                    directory="nested/.agents/review",
                ),
            )
        return expand_collection(
            root,
            _collection("nested/.agents", ("review",)),
        )

    with pytest.raises(source_errors.SourceError) as control:
        run()
    assert control.value.code == source_errors.CODE_OUTPUT_OVERLAP

    original_prepare = _selection_fs._prepare_preflight
    changed: list[bool] = []

    def replace(*args: object, **kwargs: object):  # type: ignore[no-untyped-def]
        record = original_prepare(*args, **kwargs)
        (nested / ".agents").rename(nested / "retired")
        if managed_shape == "direct":
            _write_skill(nested / ".agents" / "review")
        else:
            _write_skill(nested / "replacement" / "review")
            (nested / ".agents").symlink_to(
                "replacement", target_is_directory=True
            )
        changed.append(True)
        return record

    monkeypatch.setattr(_selection_fs, "_prepare_preflight", replace)
    markers = _install_session_marker(monkeypatch)
    with pytest.raises(source_errors.SourceError) as excinfo:
        run()
    assert changed == [True]
    assert markers
    assert "changed after Phase A" in excinfo.value.detail


@pytest.mark.parametrize("entry", ["individual", "collection"])
@pytest.mark.parametrize("change", ["absent", "retarget"])
def test_managed_boundary_alias_change_before_return_refuses_snapshot_change(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    entry: str,
    change: str,
) -> None:
    """A managed alias created or retargeted after Phase A cannot hide overlap."""

    monkeypatch.setenv("CSK_CONFIG", str(tmp_path / "home" / "config.json"))
    root = tmp_path / "source"
    _write_skill(root / "review")
    managed = root / ".agents"
    if change == "retarget":
        (root / "old-managed").mkdir(parents=True)
        managed.symlink_to("old-managed", target_is_directory=True)

    def run() -> object:
        if entry == "individual":
            return resolve_individual(
                root,
                IndividualSelector(
                    name="review", from_alias="local", directory="review"
                ),
            )
        return expand_collection(root, _collection(".", ("review",)))

    original_validate = selection._validate_selected_member

    def mutate_after_validation(*args: object, **kwargs: object):  # type: ignore[no-untyped-def]
        if managed.is_symlink() or managed.exists():
            managed.unlink()
        managed.symlink_to("review", target_is_directory=True)
        return original_validate(*args, **kwargs)

    monkeypatch.setattr(selection, "_validate_selected_member", mutate_after_validation)
    with pytest.raises(source_errors.SourceError) as excinfo:
        run()
    assert excinfo.value.code == source_errors.CODE_SNAPSHOT_CHANGED
    assert "Managed output boundary changed after Phase A" in excinfo.value.detail


@pytest.mark.parametrize("entry", ["individual", "collection"])
@pytest.mark.parametrize("replacement", ["hard-link", "rename"])
def test_opened_member_identity_is_bound_to_entry_inspection(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    entry: str,
    replacement: str,
) -> None:
    """A replacement between stat and open cannot leak bytes."""

    monkeypatch.setenv("CSK_CONFIG", str(tmp_path / "home" / "config.json"))
    root = tmp_path / "source"
    member = root / "review"
    _write_skill(member)
    outside = tmp_path / "outside"
    outside.mkdir(parents=True)
    outside_file = outside / "SKILL.md"
    outside_file.write_bytes(b"outside-secret")
    member_file = member / "SKILL.md"
    original_open_child_file = _selection_fs._open_child_file
    swapped = False

    def replace_before_open(
        parent_fd: int,
        name: str,
        *,
        parent_path: Path | None = None,
    ) -> int:
        nonlocal swapped
        if parent_path == member and name == "SKILL.md" and not swapped:
            member_file.unlink()
            if replacement == "hard-link":
                os.link(outside_file, member_file)
            else:
                os.replace(outside_file, member_file)
            swapped = True
        return original_open_child_file(parent_fd, name, parent_path=parent_path)

    monkeypatch.setattr(_selection_fs, "_open_child_file", replace_before_open)
    with pytest.raises(source_errors.SourceError) as excinfo:
        _run_entry(root, entry, "review")
    assert swapped
    assert excinfo.value.code == source_errors.CODE_MEMBER_INVALID
    assert "changed after entry inspection" in excinfo.value.detail or "hard link" in (
        excinfo.value.detail
    )


@pytest.mark.parametrize("include", [("*",), ("review", "unused")])
def test_excluded_member_boundary_not_required(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    include: tuple[str, ...],
) -> None:
    """Exclusions require frontier existence, not member boundary probes."""

    monkeypatch.setenv("CSK_CONFIG", str(tmp_path / "home" / "config.json"))
    root = tmp_path / "source"
    _write_skill(root / "review")
    (root / "unused").mkdir(parents=True)
    selector = CollectionSelector(
        from_alias="local",
        directory=".",
        include=include,
        exclude=("unused",),
    )
    assert [member.name for member in expand_collection(root, selector)] == ["review"]

    original_open_child = _selection_fs._open_child_directory
    hits: list[str] = []

    def denied(
        parent_fd: int, name: str, *, parent_path: Path | None = None
    ) -> int:
        if parent_path == root / "unused" and name == ".agents":
            hits.append(name)
            raise PermissionError("excluded member boundary inaccessible")
        return original_open_child(parent_fd, name, parent_path=parent_path)

    monkeypatch.setattr(_selection_fs, "_open_child_directory", denied)
    assert [member.name for member in expand_collection(root, selector)] == ["review"]
    assert hits == []


def test_wildcard_boundary_probe_failure_refuses(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A failed managed probe is not mistaken for a pruned wildcard child."""

    monkeypatch.setenv("CSK_CONFIG", str(tmp_path / "home" / "config.json"))
    root = tmp_path / "source"
    _write_skill(root / "review")
    original = _selection_fs._open_child_directory

    def denied(
        parent_fd: int, name: str, *, parent_path: Path | None = None
    ) -> int:
        if parent_path == root / "review" and name == ".agents":
            raise PermissionError("managed probe unavailable")
        return original(parent_fd, name, parent_path=parent_path)

    monkeypatch.setattr(_selection_fs, "_open_child_directory", denied)
    with pytest.raises(source_errors.SourceError) as excinfo:
        expand_collection(root, _collection(".", ("*",)))
    assert excinfo.value.code == source_errors.CODE_OUTPUT_OVERLAP
    assert isinstance(excinfo.value.__cause__, PermissionError)


@pytest.mark.parametrize("entry", ["individual", "literal", "wildcard"])
@pytest.mark.parametrize("managed_name", [".agents", ".git", ".codex"])
@pytest.mark.parametrize("managed_target", ["nested", "nested/review"])
def test_r2_symlinked_managed_root_hard_path_refused(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    entry: str,
    managed_name: str,
    managed_target: str,
) -> None:
    """A managed root alias and its descendants are caught at every entry point."""

    monkeypatch.setenv("CSK_CONFIG", str(tmp_path / "home" / "config.json"))
    root = tmp_path / "source"
    _write_skill(root / "nested" / "review", "review")
    (root / managed_name).symlink_to(managed_target, target_is_directory=True)
    with pytest.raises(source_errors.SourceError) as excinfo:
        if entry == "individual":
            resolve_individual(
                root,
                IndividualSelector(
                    name="review", from_alias="local", directory="nested/review"
                ),
            )
        elif entry == "literal":
            expand_collection(root, _collection("nested", ("review",)))
        else:
            expand_collection(root, _collection("nested", ("*",)))
    expected = (
        source_errors.CODE_OUTPUT_OVERLAP
        if entry != "wildcard"
        else source_errors.CODE_MEMBER_INVALID
    )
    assert excinfo.value.code == expected


@pytest.mark.skipif(
    os.name != "posix",
    reason="native backslash filename semantics are a POSIX-specific test bound",
)
@pytest.mark.parametrize("entry", ["individual", "collection"])
def test_r3_native_backslash_target_not_rewritten(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, entry: str
) -> None:
    """A POSIX backslash in a link target remains a filename character."""

    monkeypatch.setenv("CSK_CONFIG", str(tmp_path / "home" / "config.json"))
    root = tmp_path / "source"
    literal_target = root / "literal\\dir"
    _write_skill(literal_target / "sub", "review")
    (root / "alias").symlink_to("literal\\dir", target_is_directory=True)
    assert _run_native_backslash_entry(root, entry, "alias/sub")


@pytest.mark.skipif(
    os.name != "posix",
    reason="native backslash filename semantics are a POSIX-specific test bound",
)
@pytest.mark.parametrize("entry", ["individual", "collection"])
def test_r3_backslash_is_literal_on_posix(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, entry: str
) -> None:
    """A dangling POSIX target is not fabricated by separator rewriting."""

    monkeypatch.setenv("CSK_CONFIG", str(tmp_path / "home" / "config.json"))
    root = tmp_path / "source"
    _write_skill(root / "review" / "sub", "review")
    (root / "alias").symlink_to("absent\\..\\review", target_is_directory=True)
    with pytest.raises(source_errors.SourceError):
        _run_native_backslash_entry(root, entry, "alias/sub")


def _run_native_backslash_entry(root: Path, entry: str, directory: str) -> object:
    if entry == "individual":
        return resolve_individual(
            root,
            IndividualSelector(
                name="review", from_alias="local", directory=directory
            ),
        )
    parent, _, child = directory.partition("/")
    return expand_collection(
        root,
        CollectionSelector(
            from_alias="local", directory=parent, include=(child,), exclude=()
        ),
    )


@pytest.mark.parametrize("entry", ["individual", "collection"])
@pytest.mark.parametrize("outcome", ["success", "refusal", "repeated"])
def test_r6_descriptors_released(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    entry: str,
    outcome: str,
) -> None:
    """Every session descriptor is released on success and refusal."""

    monkeypatch.setenv("CSK_CONFIG", str(tmp_path / "home" / "config.json"))
    root = tmp_path / "source"
    _write_skill(root / "review", "review")
    outside = tmp_path / "outside"
    _write_skill(outside, "review")
    if outcome == "refusal":
        (root / "review").rename(root / "review-real")
        (root / "review").symlink_to(outside, target_is_directory=True)

    original_open = os.open
    original_close = os.close
    active: set[int] = set()

    def tracked_open(*args: object, **kwargs: object) -> int:
        descriptor = original_open(*args, **kwargs)
        active.add(descriptor)
        return descriptor

    def tracked_close(descriptor: int) -> None:
        active.discard(descriptor)
        original_close(descriptor)

    monkeypatch.setattr(os, "open", tracked_open)
    monkeypatch.setattr(os, "close", tracked_close)

    attempts = 8 if outcome == "repeated" else 1
    for _ in range(attempts):
        if outcome == "refusal":
            with pytest.raises(source_errors.SourceError):
                _run_entry(root, entry, "review")
        else:
            _run_entry(root, entry, "review")
        assert not active


@pytest.mark.parametrize("entry", ["individual", "collection"])
@pytest.mark.parametrize(
    "failure", [OSError, RuntimeError, ValueError, UnicodeError]
)
def test_r6_boundary_probe_structured(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    entry: str,
    failure: type[Exception],
) -> None:
    """Boundary-probe failures become structured refusals, never raw errors."""

    monkeypatch.setenv("CSK_CONFIG", str(tmp_path / "home" / "config.json"))
    root = tmp_path / "source"
    _write_skill(root / "review", "review")
    original = _selection_fs._open_child_directory
    hits: list[str] = []

    def probe(
        parent_fd: int, name: str, *, parent_path: Path | None = None
    ) -> int:
        if name == ".agents":
            hits.append(name)
            raise failure("injected managed probe failure")
        return original(parent_fd, name, parent_path=parent_path)

    monkeypatch.setattr(_selection_fs, "_open_child_directory", probe)
    with pytest.raises(source_errors.SourceError) as excinfo:
        _run_entry(root, entry, "review")
    assert hits
    assert excinfo.value.code == source_errors.CODE_OUTPUT_OVERLAP
    assert isinstance(excinfo.value.__cause__, failure)


@pytest.mark.skipif(
    os.name != "posix",
    reason="descriptor-relative outside-read control is POSIX-specific; Windows fallback bound",
)
@pytest.mark.parametrize("entry", ["individual", "collection"])
def test_oracle_detects_outside_descriptor_read(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, entry: str
) -> None:
    """The boundary oracle fails on a real outside descriptor read."""

    monkeypatch.setenv("CSK_CONFIG", str(tmp_path / "home" / "config.json"))
    root = tmp_path / "source"
    _write_skill(root / "review", "review")
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "leak").write_text("secret", encoding="utf-8")
    outside_fd = os.open(outside, os.O_RDONLY | os.O_DIRECTORY)
    original = selection._validate_selected_member

    def evil(*args: object, **kwargs: object):  # type: ignore[no-untyped-def]
        leak_fd = os.open("leak", os.O_RDONLY, dir_fd=outside_fd)
        try:
            assert os.read(leak_fd, 6) == b"secret"
        finally:
            os.close(leak_fd)
        return original(*args, **kwargs)

    monkeypatch.setattr(selection, "_validate_selected_member", evil)
    markers = _install_session_marker(monkeypatch)
    try:
        _run_entry(root, entry, "review")
        with pytest.raises(AssertionError):
            _assert_descriptor_boundary(root, markers[-1])
    finally:
        os.close(outside_fd)
        del _AUDIT_EVENTS[:]
        del _OPEN_OBSERVATIONS[:]
        del _SCANDIR_OBSERVATIONS[:]
        _SESSION_ROOT_INDEX = None


@pytest.mark.parametrize("entry", ["individual", "collection"])
@pytest.mark.parametrize(
    "read_shape",
    [
        pytest.param(
            "descriptor",
            marks=pytest.mark.skipif(
                os.name != "posix",
                reason="dir_fd outside-read control is POSIX-specific; Windows fallback bound",
            ),
        ),
        "path",
        "bare-path",
        "foreign-fd",
    ],
)
def test_oracle_catches_real_outside_read(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    entry: str,
    read_shape: str,
) -> None:
    """The oracle rejects every verified outside-read shape.

    This is an adversarial test of the test seam itself. The injected read
    verifies six real bytes before returning to the normal validator; a
    passing selection result is therefore not evidence that the oracle saw the
    read.
    """

    monkeypatch.setenv("CSK_CONFIG", str(tmp_path / "home" / "config.json"))
    root = tmp_path / "source"
    _write_skill(root / "review", "review")
    outside = tmp_path / "outside"
    outside.mkdir()
    leak = outside / "leak"
    leak.write_text("secret", encoding="utf-8")
    outside_fd = os.open(outside, os.O_RDONLY | os.O_DIRECTORY)
    foreign_fd: int | None = None
    if read_shape == "foreign-fd":
        foreign_fd = os.open(leak, os.O_RDONLY)
    original = selection._validate_selected_member

    def evil(*args: object, **kwargs: object):  # type: ignore[no-untyped-def]
        if read_shape == "descriptor":
            leak_fd = os.open("leak", os.O_RDONLY, dir_fd=outside_fd)
            try:
                assert os.read(leak_fd, 6) == b"secret"
            finally:
                os.close(leak_fd)
        elif read_shape == "path":
            assert leak.read_bytes() == b"secret"
        elif read_shape == "bare-path":
            monkeypatch.chdir(outside)
            with open("leak", "rb") as stream:
                assert stream.read(6) == b"secret"
        else:
            assert foreign_fd is not None
            with open(foreign_fd, "rb", closefd=False) as stream:
                assert stream.read(6) == b"secret"
        return original(*args, **kwargs)

    monkeypatch.setattr(selection, "_validate_selected_member", evil)
    markers = _install_session_marker(monkeypatch)
    try:
        _run_entry(root, entry, "review")
        assert markers
        with pytest.raises(AssertionError):
            _assert_descriptor_boundary(root, markers[-1])
    finally:
        os.close(outside_fd)
        if foreign_fd is not None:
            os.close(foreign_fd)
        del _AUDIT_EVENTS[:]
        del _OPEN_OBSERVATIONS[:]
        del _SCANDIR_OBSERVATIONS[:]
        del _DESCRIPTOR_EVENTS[:]
        global _SESSION_ROOT_INDEX, _SESSION_AUDIT_INDEX, _SESSION_SCANDIR_INDEX
        _SESSION_ROOT_INDEX = None
        _SESSION_AUDIT_INDEX = None
        _SESSION_SCANDIR_INDEX = None


@pytest.mark.parametrize("entry", ["individual", "collection"])
@pytest.mark.parametrize(
    "read_shape",
    [
        "read",
        pytest.param(
            "pread",
            marks=pytest.mark.skipif(
                not hasattr(os, "pread"),
                reason="os.pread is unavailable on this platform; read-provenance bound",
            ),
        ),
        pytest.param(
            "readv",
            marks=pytest.mark.skipif(
                not hasattr(os, "readv"),
                reason="os.readv is unavailable on this platform; read-provenance bound",
            ),
        ),
        pytest.param(
            "preadv",
            marks=pytest.mark.skipif(
                not hasattr(os, "preadv"),
                reason="os.preadv is unavailable on this platform; read-provenance bound",
            ),
        ),
        "stream",
    ],
)
def test_oracle_all_read_seams(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    entry: str,
    read_shape: str,
) -> None:
    """Pre-existing foreign capabilities are rejected for every read shape."""

    monkeypatch.setenv("CSK_CONFIG", str(tmp_path / "home" / "config.json"))
    root = tmp_path / "source"
    _write_skill(root / "review")
    leak = tmp_path / "leak"
    leak.write_bytes(b"secret")
    foreign_fd = os.open(leak, os.O_RDONLY)
    stream = open(leak, "rb")
    tracked_stream = _TrackedStream(stream, stream.fileno())
    original = selection._validate_selected_member
    verified: list[bytes] = []

    def evil(*args: object, **kwargs: object):  # type: ignore[no-untyped-def]
        if read_shape == "read":
            raw = os.read(foreign_fd, 6)
        elif read_shape == "pread":
            raw = os.pread(foreign_fd, 6, 0)
        elif read_shape == "readv":
            buffer = bytearray(6)
            count = os.readv(foreign_fd, [buffer])
            raw = bytes(buffer[:count])
        elif read_shape == "preadv":
            buffer = bytearray(6)
            count = os.preadv(foreign_fd, [buffer], 0)
            raw = bytes(buffer[:count])
        else:
            raw = tracked_stream.read(6)
        assert raw == b"secret"
        verified.append(raw)
        return original(*args, **kwargs)

    monkeypatch.setattr(selection, "_validate_selected_member", evil)
    markers = _install_session_marker(monkeypatch)
    try:
        _run_entry(root, entry, "review")
        assert verified == [b"secret"]
        with pytest.raises(AssertionError):
            _assert_descriptor_boundary(root, markers[-1])
    finally:
        os.close(foreign_fd)
        stream.close()
        del _AUDIT_EVENTS[:]
        del _OPEN_OBSERVATIONS[:]
        del _SCANDIR_OBSERVATIONS[:]
        del _DESCRIPTOR_EVENTS[:]
        del _READ_OBSERVATIONS[:]
        global _SESSION_ROOT_INDEX, _SESSION_AUDIT_INDEX, _SESSION_SCANDIR_INDEX
        global _SESSION_READ_INDEX
        _SESSION_ROOT_INDEX = None
        _SESSION_AUDIT_INDEX = None
        _SESSION_SCANDIR_INDEX = None
        _SESSION_READ_INDEX = None


@pytest.mark.parametrize("entry", ["individual", "collection"])
def test_r11_finite_link_revisit_uses_remaining_suffix(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, entry: str
) -> None:
    """A link may repeat when the unresolved suffix is different."""

    monkeypatch.setenv("CSK_CONFIG", str(tmp_path / "home" / "config.json"))
    root = tmp_path / "source"
    _write_skill(root / "real" / "review", "review")
    (root / "alias").symlink_to("real", target_is_directory=True)
    (root / "real" / "again").symlink_to("../alias", target_is_directory=True)

    if entry == "individual":
        selected = resolve_individual(
            root,
            IndividualSelector(
                name="review", from_alias="local", directory="alias/again/review"
            ),
        )
        assert selected.name == "review"
    else:
        selected = expand_collection(
            root, _collection("alias/again", ("review",))
        )
        assert [member.name for member in selected] == ["review"]


@pytest.mark.parametrize("entry", ["individual", "collection"])
def test_r10_unselected_unreadable_subtree_is_not_preflighted(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, entry: str
) -> None:
    """A valid selector does not enumerate an unrelated unreadable sibling."""

    monkeypatch.setenv("CSK_CONFIG", str(tmp_path / "home" / "config.json"))
    root = tmp_path / "source"
    _write_skill(root / "review", "review")
    unused = root / "unused"
    unused.mkdir()
    original_scandir = os.scandir
    denied: list[object] = []

    def deny_unused(path: object):  # type: ignore[no-untyped-def]
        if not isinstance(path, int) and Path(path) == unused:
            denied.append(path)
            raise PermissionError("unselected subtree is unreadable")
        return original_scandir(path)

    monkeypatch.setattr(os, "scandir", deny_unused)
    original_open_child = _selection_fs._open_child_directory

    def deny_unused_open(
        parent_fd: int, name: str, *, parent_path: Path | None = None
    ) -> int:
        if parent_path == root and name == "unused":
            denied.append((parent_path, name))
            raise PermissionError("unselected subtree is unreadable")
        return original_open_child(parent_fd, name, parent_path=parent_path)

    monkeypatch.setattr(_selection_fs, "_open_child_directory", deny_unused_open)
    if entry == "individual":
        selected = resolve_individual(
            root,
            IndividualSelector(name="review", from_alias="local", directory="review"),
        )
        assert selected.name == "review"
    else:
        selected = expand_collection(root, _collection(".", ("review",)))
        assert [member.name for member in selected] == ["review"]
    assert denied == []


@pytest.mark.parametrize("entry", ["collection", "individual"])
def test_selection_identity_uses_raw_bytes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, entry: str
) -> None:
    """The descriptor snapshot passes exact source bytes to downstream readers."""

    monkeypatch.setenv("CSK_CONFIG", str(tmp_path / "home" / "config.json"))
    root = tmp_path / "source"
    raw = _write_skill(root / "review", "review")
    expected = hashlib.sha256(raw).hexdigest()
    observed: list[str] = []
    original = selection.validate_member_package

    def validate(*args, **kwargs):  # type: ignore[no-untyped-def]
        snapshot = kwargs["snapshot"]
        observed.append(hashlib.sha256(snapshot.files[("SKILL.md",)]).hexdigest())
        return original(*args, **kwargs)

    monkeypatch.setattr(selection, "validate_member_package", validate)
    _run_entry(root, entry, "review")
    assert observed == [expected]


@pytest.mark.parametrize("entry", ["collection", "individual"])
def test_snapshot_validation_uses_captured_stat(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, entry: str
) -> None:
    """Downstream manifest checks do not reopen the displayed member path."""

    monkeypatch.setenv("CSK_CONFIG", str(tmp_path / "home" / "config.json"))
    root = tmp_path / "source"
    member = root / "review"
    _write_skill(member)
    build_root = member / "build"
    (build_root / "cmd" / "tool").mkdir(parents=True)
    (build_root / "go.mod").write_text(
        "module example.com/tool\n\ngo 1.23\n", encoding="utf-8"
    )
    (build_root / "cmd" / "tool" / "main.go").write_text(
        "package main\n", encoding="utf-8"
    )
    (member / "agent-skill.json").write_text(
        '{"schema_version":6,"build_roots":["build"],'
        '"capabilities":{},"commands":{"tool":{"type":"build",'
        '"driver":"go-v1","source_dir":"build/cmd/tool"}}}',
        encoding="utf-8",
    )
    calls: list[str] = []

    def denied(path: Path) -> object:
        calls.append(os.fspath(path))
        raise AssertionError(f"downstream reader reopened {path}")

    monkeypatch.setattr(Path, "lstat", denied)
    _run_entry(root, entry, "review")
    assert calls == []


@pytest.mark.parametrize("entry", ["collection", "individual"])
@pytest.mark.parametrize("fault", ["selection-open", "member-walk", "reader"])
@pytest.mark.parametrize(
    "failure", [OSError, PermissionError, RuntimeError, ValueError, UnicodeError]
)
def test_selection_structured_error_property(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    entry: str,
    fault: str,
    failure: type[Exception],
) -> None:
    """The public error matrix stays structured through both entry points."""

    monkeypatch.setenv("CSK_CONFIG", str(tmp_path / "home" / "config.json"))
    root = tmp_path / "source"
    _write_skill(root / "review", "review")
    hit: list[str] = []

    if fault == "selection-open":
        original = _selection_fs._open_child_directory

        def denied(parent_fd, name, *, parent_path=None):  # type: ignore[no-untyped-def]
            if name == "review":
                hit.append(name)
                raise failure("property-injected selection denial")
            return original(parent_fd, name, parent_path=parent_path)

        monkeypatch.setattr(_selection_fs, "_open_child_directory", denied)
    elif fault == "member-walk":
        original = _selection_fs.SelectionSession._read_regular_file

        def denied(
            self,
            parent,
            name,
            *,
            code,
            context,
            expected_identity=None,
            expected_nlink=1,
        ):  # type: ignore[no-untyped-def]
            if name == "SKILL.md":
                hit.append(name)
                raise failure("property-injected member denial")
            return original(
                self,
                parent,
                name,
                code=code,
                context=context,
                expected_identity=expected_identity,
                expected_nlink=expected_nlink,
            )

        monkeypatch.setattr(_selection_fs.SelectionSession, "_read_regular_file", denied)
    else:
        original = selection.skillcheck.validate_skill

        def denied(*args, **kwargs):  # type: ignore[no-untyped-def]
            hit.append("reader")
            raise failure("property-injected reader denial")

        monkeypatch.setattr(selection.skillcheck, "validate_skill", denied)

    with pytest.raises(source_errors.SourceError) as excinfo:
        _run_entry(root, entry, "review")
    assert hit
    assert excinfo.value.code in {
        source_errors.CODE_SELECTION_INVALID,
        source_errors.CODE_MEMBER_INVALID,
    }
    assert isinstance(excinfo.value.__cause__, failure)
