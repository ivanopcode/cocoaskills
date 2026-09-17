"""Property-style attacks for the descriptor-backed selection boundary.

The hook is intentionally installed once for the process.  The assertions do
not trust the selection walk's return value: they inspect the filesystem audit
events emitted while both public selection entry points run.
"""

from __future__ import annotations

import ast
import builtins
import errno
import hashlib
import io
import json
import mmap
import os
import shutil
import stat
import subprocess
import sys
import unicodedata
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest

from csk.sources import _selection_fs
from csk.sources import errors as source_errors
from csk.sources import selection
from csk.sources.selection import (
    expand_collection,
    resolve_individual,
    resolve_selector_directory,
)
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


def _write_skill(
    directory: Path, name: str = "review", description: str = "Property fixture"
) -> bytes:
    directory.mkdir(parents=True, exist_ok=True)
    raw = (
        f"---\nname: {name}\ndescription: {description}\n---\n"
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


def _collection(
    directory: str = ".",
    include: tuple[str, ...] = ("*",),
    exclude: tuple[str, ...] = (),
) -> CollectionSelector:
    return CollectionSelector(
        from_alias="local", directory=directory, include=include, exclude=exclude
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


_SELECTION_PACKAGE = "csk.sources"

# Bare-name references that make the static import closure undecidable. Any
# load of one inside a closure module is a scope violation: nothing on the
# selection path needs code loading by name, computed-source evaluation, or
# out-of-band module execution.
_DYNAMIC_IMPORT_NAMES = frozenset(
    {"__import__", "import_module", "exec_module", "eval", "exec"}
)

# Attribute forms of code-loading facilities, matched on the attribute alone
# so receiver aliasing (``import importlib as il``) cannot hide them.
_DYNAMIC_IMPORT_ATTRS = frozenset(
    {
        "import_module",
        "exec_module",
        "run_module",
        "run_path",
        "SourceFileLoader",
    }
)

# Calls whose literal first argument names the dynamically imported module;
# the call itself is always a violation, and the literal target is queued as
# well so it is scanned even if the violation check were ever weakened.
_DYNAMIC_LITERAL_CALLS = frozenset({"__import__", "import_module"})


def _selection_module_candidates(dotted: str) -> list[str]:
    """POSIX-relative file candidates for one in-package dotted name.

    A dotted suffix may name a plain module (``evil.py``) or a subpackage
    (``evil/__init__.py``); when both exist both are scanned (a documented
    superset: at most one of them executes). Every parent prefix of a dotted
    target executes its ``__init__.py`` on import, so those are queued too.
    Candidates that resolve to no existing file are skipped by the walk: they
    cannot execute, because the import would raise at runtime.
    """

    if dotted == _SELECTION_PACKAGE:
        return ["__init__.py"]
    parts = dotted[len(_SELECTION_PACKAGE) + 1 :].split(".")
    candidates = [
        "/".join(parts[:depth]) + "/__init__.py" for depth in range(1, len(parts))
    ]
    candidates.append("/".join(parts) + ".py")
    candidates.append("/".join(parts) + "/__init__.py")
    return candidates


def _dynamic_import_candidates(literal: str, pkg_parts: list[str]) -> list[str]:
    """Best-effort closure candidates for a literal dynamic-import target."""

    if literal.startswith("."):
        level = len(literal) - len(literal.lstrip("."))
        rest = literal.lstrip(".")
        if level - 1 > len(pkg_parts):
            return []
        base = pkg_parts[: len(pkg_parts) - (level - 1)]
        if rest:
            base = [*base, *rest.split(".")]
        dotted = ".".join(base)
    else:
        dotted = literal
    if dotted == "csk":
        return ["__init__.py"]
    if dotted == _SELECTION_PACKAGE or dotted.startswith(_SELECTION_PACKAGE + "."):
        return _selection_module_candidates(dotted)
    return []


def _walk_selection_imports(source_dir: Path) -> tuple[set[str], list[str]]:
    """Import closure of the selection path plus fail-closed scope violations.

    The walk starts at the selection entry module and at the package
    ``__init__.py`` (which always executes on import) and follows
    same-package imports transitively: level-1 relatives resolved against the
    containing package of each scanned file, level-2 ``..sources`` (which is
    this package from here), and absolute ``csk.sources[.x]`` forms,
    descending into subpackage directories. A module nothing on the selection
    path imports is out of scope; a module it starts importing later comes
    into scope automatically.

    Fail-closed rules: a dynamic code-loading facility (``importlib`` in any
    spelling, ``__import__``, ``eval``/``exec``, runpy, file loaders) is a
    violation, as an early signal. Binding the package object itself without
    naming a submodule target (bare ``import csk`` / ``import csk.sources``,
    ``from csk import sources``, ``from .. import sources``) is refused as
    unresolvable scope AND widens the scanned set to every ``*.py`` under
    the package, so attribute-channel use cannot hide an unscanned module.
    Static imports that name nothing executable (missing files, beyond-top
    relatives) are complete as the empty set, not violations: the import
    would raise at runtime.

    The dynamic-facility enumeration above is defence in depth only;
    completeness of the scanned set is established by the runtime-vs-static
    observation (``test_runtime_loaded_modules_are_statically_scanned``
    and the runtime half of the shipped seam test), not by this list.
    A novel spelling this walk does not flag is still caught when the
    module it loads appears in the runtime snapshot without a static
    scan; see the ``_RUNTIME_COVERED_SILENT_SHAPES`` test.

    Residual bounds (unchanged from the directory walk this replaced, which
    only ever scanned this package): imports of parent/sibling packages
    (``from .. import adapters``) are out of scope; computed-string
    evaluation (``eval`` of a non-literal is refused, but smuggled strings
    are invisible to any static scan), ``getattr``-computed primitive names,
    and non-source modules (``.pyc``-only, extensions) are outside static
    analysis, covered instead by the runtime observation and the dynamic
    boundary oracle. Carried here from TASK-260917-3q5h87 by orchestrator
    decision: the sibling leaf's directory walk flagged
    ``repository_policy.py`` (whose job is reading ``source-policy.json``)
    once trunk added it to the package, although the selection path never
    imports it.
    """

    closure: set[str] = set()
    found_violations: set[str] = set()
    widened = False
    queue: list[str] = ["selection.py", "__init__.py"]

    def widen_scope(reason: str) -> None:
        nonlocal widened
        found_violations.add(reason)
        if widened:
            return
        widened = True
        queue.extend(
            sorted(
                candidate.relative_to(source_dir).as_posix()
                for candidate in source_dir.rglob("*.py")
            )
        )

    def note(reason: str) -> None:
        found_violations.add(reason)

    while queue:
        name = queue.pop()
        if name in closure or not (source_dir / name).is_file():
            continue
        closure.add(name)
        try:
            tree = ast.parse(
                (source_dir / name).read_text(encoding="utf-8"), filename=name
            )
        except (OSError, SyntaxError, ValueError, UnicodeError) as exc:
            note(f"{name}: cannot parse for import closure: {exc}")
            continue
        parent = name.rpartition("/")[0]
        pkg_parts = ["csk", "sources", *(parent.split("/") if parent else [])]
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom):
                if node.level == 0:
                    module = node.module or ""
                    if module == "importlib" or module.startswith("importlib."):
                        note(
                            f"{name}:{node.lineno}: importlib facility import "
                            "makes the import closure undecidable"
                        )
                    elif module == "csk":
                        for alias in node.names:
                            if alias.name == "sources":
                                widen_scope(
                                    f"{name}:{node.lineno}: binds the "
                                    "csk.sources package without naming a "
                                    "submodule target"
                                )
                                queue.append("__init__.py")
                    elif module == _SELECTION_PACKAGE or module.startswith(
                        _SELECTION_PACKAGE + "."
                    ):
                        queue.extend(_selection_module_candidates(module))
                        for alias in node.names:
                            if alias.name != "*":
                                queue.extend(
                                    _selection_module_candidates(
                                        f"{module}.{alias.name}"
                                    )
                                )
                    # Other absolute imports live outside csk.sources: out of
                    # scope for this instrument.
                else:
                    base_parts = pkg_parts[: max(0, len(pkg_parts) - (node.level - 1))]
                    if node.module:
                        base_parts = [*base_parts, *node.module.split(".")]
                    base = ".".join(base_parts)
                    if base == "csk":
                        for alias in node.names:
                            if alias.name == "sources":
                                widen_scope(
                                    f"{name}:{node.lineno}: binds the "
                                    "csk.sources package without naming a "
                                    "submodule target"
                                )
                                queue.append("__init__.py")
                    elif base == _SELECTION_PACKAGE or base.startswith(
                        _SELECTION_PACKAGE + "."
                    ):
                        queue.extend(_selection_module_candidates(base))
                        for alias in node.names:
                            if alias.name != "*":
                                queue.extend(
                                    _selection_module_candidates(f"{base}.{alias.name}")
                                )
                    # Parent/sibling packages (``from .. import adapters``)
                    # and beyond-top relatives (ImportError at runtime): no
                    # in-package module executes.
            elif isinstance(node, ast.Import):
                for alias in node.names:
                    dotted = alias.name
                    if dotted in {"csk", _SELECTION_PACKAGE}:
                        widen_scope(
                            f"{name}:{node.lineno}: bare import {dotted!r} binds "
                            "the package without naming a submodule target"
                        )
                        queue.append("__init__.py")
                    elif dotted.startswith(_SELECTION_PACKAGE + "."):
                        queue.extend(_selection_module_candidates(dotted))
                    elif dotted == "importlib" or dotted.startswith("importlib."):
                        note(
                            f"{name}:{node.lineno}: importlib facility import "
                            "makes the import closure undecidable"
                        )
            elif isinstance(node, ast.Name):
                if node.id in _DYNAMIC_IMPORT_NAMES and isinstance(
                    node.ctx, ast.Load
                ):
                    note(
                        f"{name}:{node.lineno}: dynamic facility {node.id!r} "
                        "makes the import closure undecidable"
                    )
            elif isinstance(node, ast.Attribute):
                if node.attr in _DYNAMIC_IMPORT_ATTRS:
                    note(
                        f"{name}:{node.lineno}: dynamic facility {node.attr!r} "
                        "makes the import closure undecidable"
                    )
            if isinstance(node, ast.Call):
                function = node.func
                called: str | None = None
                if isinstance(function, ast.Name):
                    called = function.id
                elif isinstance(function, ast.Attribute):
                    called = function.attr
                if called in _DYNAMIC_LITERAL_CALLS and node.args:
                    first = node.args[0]
                    if isinstance(first, ast.Constant) and isinstance(
                        first.value, str
                    ):
                        queue.extend(
                            _dynamic_import_candidates(first.value, pkg_parts)
                        )
    return closure, sorted(found_violations)


def _selection_import_closure(source_dir: Path) -> set[str]:
    """POSIX-relative names of the ``csk.sources`` modules selection imports."""

    closure, _ = _walk_selection_imports(source_dir)
    return closure


def _selection_import_violations(source_dir: Path) -> list[str]:
    """Fail-closed scope violations: dynamic or unresolvable imports."""

    _, violations = _walk_selection_imports(source_dir)
    return violations


_RUNTIME_CLOSURE_DRIVER = """\
import json
import os
import sys
import tempfile
from pathlib import Path

package_root = Path(sys.argv[1])
sys.path.insert(0, str(package_root))
exec_files: list[str] = []
open_files: list[str] = []

def _hook(event: str, args: tuple) -> None:
    if event == "exec":
        try:
            exec_files.append(str(args[0].co_filename))
        except Exception:
            pass
    elif event == "open":
        try:
            if args and not isinstance(args[0], int):
                open_files.append(str(args[0]))
        except Exception:
            pass

sys.addaudithook(_hook)
import csk.sources._selection_fs as _fs
import csk.sources.selection as _sel
import csk.sources.skillfile_v2 as _sv

errors: list[str] = []
with tempfile.TemporaryDirectory(prefix="runtime-closure-") as tmp:
    tmp_path = Path(tmp)
    os.environ["CSK_CONFIG"] = str(tmp_path / "home" / "config.json")
    source = tmp_path / "source"
    member = source / "review"
    member.mkdir(parents=True)
    (member / "SKILL.md").write_text(
        "---\\nname: review\\ndescription: runtime probe\\n---\\n# body\\n",
        encoding="utf-8",
    )
    try:
        _sel.expand_collection(
            source,
            _sv.CollectionSelector(
                from_alias="local", directory=".", include=("*",), exclude=()
            ),
        )
    except Exception as exc:
        errors.append(f"expand_collection: {type(exc).__name__}: {exc}")
    try:
        _sel.resolve_individual(
            source,
            _sv.IndividualSelector(
                name="review", from_alias="local", directory="review"
            ),
        )
    except Exception as exc:
        errors.append(f"resolve_individual: {type(exc).__name__}: {exc}")

pkgdir = str(Path(_fs.__file__).parent)
prefix = pkgdir + os.sep
modules: dict[str, str | None] = {}
for _name in sorted(sys.modules):
    if _name == "csk.sources" or _name.startswith("csk.sources."):
        _mod = sys.modules[_name]
        _fname = getattr(_mod, "__file__", None)
        modules[_name] = str(_fname) if _fname is not None else None
payload = {
    "modules": modules,
    "exec": sorted({f for f in exec_files if f.startswith(prefix)}),
    "open": sorted({f for f in open_files if f.startswith(prefix)}),
    "errors": errors,
}
print(json.dumps(payload))
"""


def _runtime_selection_observations(package_root: Path) -> dict[str, Any]:
    """Run the fresh-subprocess runtime probe and return its JSON payload."""

    proc = subprocess.run(
        [sys.executable, "-c", _RUNTIME_CLOSURE_DRIVER, str(package_root)],
        capture_output=True,
        text=True,
        timeout=120,
    )
    if proc.returncode != 0:
        raise AssertionError(
            "runtime closure probe failed: "
            f"exit={proc.returncode} "
            f"stdout={proc.stdout[-2000:]} stderr={proc.stderr[-2000:]}"
        )
    try:
        payload = json.loads(proc.stdout)
    except (json.JSONDecodeError, ValueError) as exc:
        raise AssertionError(
            f"runtime closure probe emitted no JSON: {exc} "
            f"stdout={proc.stdout[-2000:]} stderr={proc.stderr[-2000:]}"
        ) from exc
    if not isinstance(payload, dict):
        raise AssertionError("runtime closure probe payload is not an object")
    return payload


def _static_closure_dotted_names(closure: set[str]) -> set[str]:
    """Dotted ``csk.sources`` names for one static file closure."""

    dotted: set[str] = set()
    for name in closure:
        if name == "__init__.py":
            dotted.add("csk.sources")
        elif name.endswith("/__init__.py"):
            dotted.add(
                "csk.sources." + name[: -len("/__init__.py")].replace("/", ".")
            )
        elif name.endswith(".py"):
            dotted.add("csk.sources." + name[: -len(".py")].replace("/", "."))
    return dotted


def _map_pycache_to_source(relative: str) -> str | None:
    """Map one ``__pycache__`` relative path to its source ``.py``, if any."""

    parts = relative.split("/")
    if "__pycache__" not in parts:
        return None
    idx = parts.index("__pycache__")
    base = parts[-1].split(".")[0]
    if not base:
        return None
    prefix = parts[:idx]
    return "/".join([*prefix, base + ".py"]) if prefix else base + ".py"


def _runtime_closure_violations_from_obs(
    source_dir: Path, closure: set[str], obs: dict[str, Any]
) -> list[str]:
    """Unscanned runtime files from one probe payload (fail-closed).

    Three signals, one comparison: the ``sys.modules`` snapshot catches
    ordinary and importlib/getattr imports; the audit ``exec`` filenames
    catch ``runpy`` and loader shapes that never enter ``sys.modules``
    (CPython runs them as ``__main__``); the audit ``open`` paths catch
    ``exec(open().read())`` shapes that execute as ``<string>``. Every
    file-backed signal maps to a POSIX-relative path under ``source_dir``
    and must be in the static closure. A module that loaded but was never
    scanned is a violation naming the module.
    """

    static_dotted = _static_closure_dotted_names(closure)
    violations: list[str] = []
    try:
        resolved_root = source_dir.resolve()
    except (OSError, RuntimeError, ValueError, UnicodeError) as exc:
        return [f"{source_dir}: cannot resolve scanned tree: {exc}"]
    runtime_files: dict[str, str] = {}

    def _record(relative: str, provenance: str) -> None:
        runtime_files.setdefault(relative, provenance)

    modules = obs.get("modules", {})
    if not isinstance(modules, dict):
        return ["runtime probe modules payload is not a mapping"]
    for dotted in sorted(str(k) for k in modules):
        fname = modules[dotted]
        if fname is None:
            if dotted not in static_dotted:
                violations.append(f"{dotted}: loaded but never scanned (no file)")
            continue
        if not isinstance(fname, str):
            violations.append(f"{dotted}: probe file is not a string")
            continue
        try:
            rel = Path(fname).resolve().relative_to(resolved_root).as_posix()
        except (OSError, RuntimeError, ValueError, UnicodeError):
            violations.append(f"{dotted}: loaded from outside scanned tree: {fname}")
            continue
        if rel.endswith(".pyc") and "__pycache__" in rel:
            mapped = _map_pycache_to_source(rel)
            if mapped is None or mapped not in closure:
                _record(rel, f"{dotted}: loaded {rel}")
            else:
                _record(mapped, f"{dotted}: loaded {rel}")
            continue
        _record(rel, f"{dotted}: loaded {rel}")
    exec_files = obs.get("exec", [])
    if not isinstance(exec_files, list):
        return violations + ["runtime probe exec payload is not a list"]
    for fname in sorted(str(f) for f in exec_files):
        try:
            rel = Path(fname).resolve().relative_to(resolved_root).as_posix()
        except (OSError, RuntimeError, ValueError, UnicodeError):
            continue
        _record(rel, f"executed {rel}")
    open_files = obs.get("open", [])
    if not isinstance(open_files, list):
        return violations + ["runtime probe open payload is not a list"]
    for fname in sorted(str(f) for f in open_files):
        try:
            rel = Path(fname).resolve().relative_to(resolved_root).as_posix()
        except (OSError, RuntimeError, ValueError, UnicodeError):
            continue
        if "__pycache__" in rel:
            mapped = _map_pycache_to_source(rel)
            if mapped is None:
                violations.append(f"read {rel} but never scanned (unmappable)")
            else:
                _record(mapped, f"read {rel}")
            continue
        if rel.endswith(".py"):
            _record(rel, f"read {rel}")
    for rel in sorted(runtime_files):
        if rel not in closure:
            violations.append(f"{runtime_files[rel]} but never scanned")
    return sorted(set(violations))


def _runtime_closure_violations(source_dir: Path, package_root: Path) -> list[str]:
    """Fresh-subprocess runtime-vs-static completeness violations, if any."""

    closure, _ = _walk_selection_imports(source_dir)
    obs = _runtime_selection_observations(package_root)
    return _runtime_closure_violations_from_obs(source_dir, closure, obs)


def test_selection_modules_have_one_filesystem_primitive_seam() -> None:
    """The source-selection modules contain no filesystem primitive bypass.

    Completeness of the scanned set rests on the runtime-vs-static
    observation below, not on the static enumeration of dynamic-import
    spellings (which remains as a defence-in-depth early signal only):
    a module the selection path loads or executes but the static walk
    never scanned fails this gate naming the module.
    """

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
    display_name = ""

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
                violations.append(f"{display_name}:{node.lineno}: open")
            elif isinstance(function, ast.Attribute):
                receiver = function.value
                if (
                    isinstance(receiver, ast.Name)
                    and receiver.id in {"os", "io", "mmap"}
                    and function.attr in module_calls
                ):
                    violations.append(
                        f"{display_name}:{node.lineno}: "
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
                            f"{display_name}:{node.lineno}: .{function.attr}()"
                        )
            self.generic_visit(node)

    closure = _selection_import_closure(source_dir)
    scope_violations = _selection_import_violations(source_dir)
    assert not scope_violations, (
        "import-closure scope is undecidable: " + "; ".join(scope_violations)
    )
    assert {"selection.py", "_selection_fs.py", "errors.py", "__init__.py"} <= closure
    assert "repository_policy.py" not in closure
    package_root = source_dir.parent.parent
    runtime_violations = _runtime_closure_violations(source_dir, package_root)
    assert not runtime_violations, (
        "runtime-loaded modules were never scanned: " + "; ".join(runtime_violations)
    )
    scanned: set[str] = set()
    for name in sorted(closure):
        if name in allowed:
            continue
        path = source_dir / name
        scanned.add(name)
        display_name = name
        Visitor().visit(ast.parse(path.read_text(encoding="utf-8"), filename=str(path)))
    assert {"selection.py", "errors.py", "__init__.py"} <= scanned
    assert not violations, "filesystem primitive bypasses owned seam: " + ", ".join(
        violations
    )


def test_runtime_loaded_modules_are_statically_scanned() -> None:
    """The runtime import set is exactly the statically scanned set.

    A fresh subprocess imports the selection entry point, drives both
    public entry points (``expand_collection`` with ``("*",)`` and
    ``resolve_individual``) over a one-member fixture (source ``review``
    with valid SKILL.md, temp ``CSK_CONFIG`` home), and reports every
    ``csk.sources`` module loaded plus every sources file executed
    (audit ``exec``) or opened (audit ``open``). Each file-backed
    signal must be in the static import closure; anything else is a
    completeness violation naming the module. The ``exec`` signal is
    what catches ``runpy`` shapes: CPython runs them as ``__main__``
    so they never enter ``sys.modules``.

    Residual bound: a module loaded only on a code path that neither
    the entry-point import nor this exercised selection path reaches
    will not appear in the runtime set. It also does not execute; if
    it ever executes, it appears.
    """

    source_dir = Path(_selection_fs.__file__).parent
    package_root = source_dir.parent.parent
    closure, _ = _walk_selection_imports(source_dir)
    obs = _runtime_selection_observations(package_root)
    assert obs.get("errors", []) == [], (
        f"runtime probe fixture failed: {obs.get('errors', [])}"
    )
    assert sorted(obs["modules"]) == [
        "csk.sources",
        "csk.sources._selection_fs",
        "csk.sources.errors",
        "csk.sources.selection",
        "csk.sources.skillfile_v2",
    ]
    resolved_root = source_dir.resolve()
    exec_rels = sorted(
        Path(fname).resolve().relative_to(resolved_root).as_posix()
        for fname in obs["exec"]
    )
    assert exec_rels == [
        "__init__.py",
        "_selection_fs.py",
        "errors.py",
        "selection.py",
        "skillfile_v2.py",
    ]
    violations = _runtime_closure_violations_from_obs(source_dir, closure, obs)
    assert violations == [], (
        "runtime-loaded modules were never scanned: " + "; ".join(violations)
    )


_CLOSURE_EVIL = "import os\n\n\ndef leak(path):\n    return os.open(path, os.O_RDONLY)\n"

# (selection.py body, extra files, expected closure member, expected extra
# members, expected-absent members, expect scope violation). Covers the six
# import shapes the revision-24 review proved the old walk missed
# (importlib absolute/relative, __import__, bare package import, level-2
# relatives, subpackages) plus the previously covered controls.
_CLOSURE_SHAPE_CASES: tuple[
    tuple[str, dict[str, str], str | None, tuple[str, ...], tuple[str, ...], bool],
    ...,
] = (
    ("from . import evil\n", {}, "evil.py", (), (), False),
    ("from .evil import thing\n", {}, "evil.py", (), (), False),
    ("from .evil import *\n", {}, "evil.py", (), (), False),
    ("from csk.sources import evil\n", {}, "evil.py", (), (), False),
    ("from csk.sources.evil import thing\n", {}, "evil.py", (), (), False),
    ("import csk.sources.evil\n", {}, "evil.py", (), (), False),
    ("import csk.sources.evil as e\n", {}, "evil.py", (), (), False),
    (
        "def f():\n    from . import evil\n    return evil\n",
        {},
        "evil.py",
        (),
        (),
        False,
    ),
    (
        "from typing import TYPE_CHECKING\nif TYPE_CHECKING:\n    from . import evil\n",
        {},
        "evil.py",
        (),
        (),
        False,
    ),
    (
        "try:\n    from . import evil\nexcept ImportError:\n    evil = None\n",
        {},
        "evil.py",
        (),
        (),
        False,
    ),
    (
        "from . import evil\n",
        {"evil.py": "from . import selection\n"},
        "evil.py",
        (),
        (),
        False,
    ),
    ("from ..sources import evil\n", {}, "evil.py", (), (), False),
    ("from ..sources.evil import thing\n", {}, "evil.py", (), (), False),
    (
        "from .subpkg import thing\n",
        {"subpkg/__init__.py": _CLOSURE_EVIL},
        "subpkg/__init__.py",
        (),
        ("evil.py",),
        False,
    ),
    (
        "from .subpkg.evil import thing\n",
        {"subpkg/__init__.py": "x = 1\n", "subpkg/evil.py": _CLOSURE_EVIL},
        "subpkg/evil.py",
        ("subpkg/__init__.py",),
        (),
        False,
    ),
    (
        "import csk.sources.subpkg.evil\n",
        {"subpkg/__init__.py": "x = 1\n", "subpkg/evil.py": _CLOSURE_EVIL},
        "subpkg/evil.py",
        ("subpkg/__init__.py",),
        (),
        False,
    ),
    (
        "import csk.sources.subpkg\n",
        {"subpkg/__init__.py": _CLOSURE_EVIL},
        "subpkg/__init__.py",
        (),
        ("evil.py",),
        False,
    ),
    (
        "from csk.sources import subpkg\n",
        {"subpkg/__init__.py": _CLOSURE_EVIL},
        "subpkg/__init__.py",
        (),
        ("evil.py",),
        False,
    ),
    ("from . import *\n", {}, None, (), ("evil.py",), False),
    (
        "from .subpkg import thing\n",
        {
            "subpkg/__init__.py": "from . import evil\n",
            "subpkg/evil.py": _CLOSURE_EVIL,
        },
        "subpkg/evil.py",
        ("subpkg/__init__.py",),
        ("evil.py",),
        False,
    ),
    ("from .. import adapters\n", {}, None, (), ("evil.py",), False),
    (
        "import os\nfrom collections import OrderedDict\n",
        {},
        None,
        (),
        ("evil.py",),
        False,
    ),
    ("from . import nonexistent\n", {}, None, (), ("evil.py",), False),
    ("from ... import x\n", {}, None, (), ("evil.py",), False),
    (
        "import importlib\nm = importlib.import_module('csk.sources.evil')\n",
        {},
        "evil.py",
        (),
        (),
        True,
    ),
    (
        "from importlib import import_module\nm = import_module('csk.sources.evil')\n",
        {},
        "evil.py",
        (),
        (),
        True,
    ),
    (
        "import importlib\nm = importlib.import_module('.evil', __package__)\n",
        {},
        "evil.py",
        (),
        (),
        True,
    ),
    (
        "import importlib\nm = importlib.import_module(name)\n",
        {},
        None,
        (),
        ("evil.py",),
        True,
    ),
    (
        "import importlib as il\nm = il.import_module('csk.sources.evil')\n",
        {},
        "evil.py",
        (),
        (),
        True,
    ),
    (
        "m = __import__('csk.sources.evil')\n",
        {},
        "evil.py",
        (),
        (),
        True,
    ),
    (
        "import csk.sources\nm = csk.sources.evil\n",
        {},
        "evil.py",
        (),
        (),
        True,
    ),
    ("import csk\n", {}, "evil.py", (), (), True),
    ("from csk import sources\n", {}, "evil.py", (), (), True),
    ("from .. import sources\n", {}, "evil.py", (), (), True),
    ("x = eval('1')\n", {}, None, (), ("evil.py",), True),
    ("exec('x = 1')\n", {}, None, (), ("evil.py",), True),
    (
        "import runpy\nrunpy.run_module('x')\n",
        {},
        None,
        (),
        ("evil.py",),
        True,
    ),
    (
        "loader = None\nx = loader.SourceFileLoader('m', 'p')\n",
        {},
        None,
        (),
        ("evil.py",),
        True,
    ),
    ("def broken(:\n", {}, None, (), ("evil.py",), True),
)

_CLOSURE_SHAPE_IDS = (
    "direct-from-dot-import",
    "direct-from-module",
    "direct-star",
    "absolute-from-package",
    "absolute-from-module",
    "absolute-import",
    "absolute-import-alias",
    "lazy-in-function",
    "type-checking-guard",
    "conditional-try",
    "cycle-a",
    "level-two-relative",
    "level-two-relative-module",
    "subpackage",
    "subpackage-module",
    "absolute-subpackage",
    "absolute-import-subpackage",
    "absolute-from-package-subpackage",
    "package-star",
    "nested-relative-scoped-to-subpackage",
    "parent-package-ignored",
    "absolute-unrelated-ignored",
    "missing-target-silent",
    "beyond-top-silent",
    "importlib-import-module",
    "importlib-from-import",
    "importlib-relative",
    "importlib-nonliteral",
    "importlib-aliased",
    "dunder-import",
    "bare-package-import",
    "bare-parent-import",
    "from-parent-bind-package",
    "level-two-bind-package",
    "eval-call",
    "exec-call",
    "runpy-attr",
    "loader-attr",
    "unparseable-selection",
)


@pytest.mark.parametrize(
    "body,extra,expect_member,expect_extras,expect_absent,expect_violation",
    _CLOSURE_SHAPE_CASES,
    ids=_CLOSURE_SHAPE_IDS,
)
def test_closure_covers_import_shape(
    tmp_path: Path,
    body: str,
    extra: dict[str, str],
    expect_member: str | None,
    expect_extras: tuple[str, ...],
    expect_absent: tuple[str, ...],
    expect_violation: bool,
) -> None:
    """Every import shape is resolved, refused, or provably non-executing."""

    (tmp_path / "selection.py").write_text(body, encoding="utf-8")
    if "evil.py" not in extra:
        (tmp_path / "evil.py").write_text(_CLOSURE_EVIL, encoding="utf-8")
    for relative, content in extra.items():
        target = tmp_path / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
    closure, violations = _walk_selection_imports(tmp_path)
    assert "selection.py" in closure
    if expect_member is not None:
        assert expect_member in closure, (
            f"imported module missed by closure {sorted(closure)}"
        )
    for other in expect_extras:
        assert other in closure, f"parent init missed by closure {sorted(closure)}"
    for missing in expect_absent:
        assert missing not in closure, (
            f"unimported module {missing} pulled into closure {sorted(closure)}"
        )
    if expect_violation:
        assert violations, "dynamic or unresolvable import admitted without refusal"
    else:
        assert violations == [], f"unexpected scope violations: {violations}"


_RUNTIME_COVERED_SILENT_SHAPES: tuple[tuple[str, str], ...] = (
    (
        "from runpy import run_module\nrun_module('csk.sources.evil')\n",
        "runpy-from-import",
    ),
    (
        "from runpy import run_module as rm\nrm('csk.sources.evil')\n",
        "runpy-from-alias",
    ),
    (
        "from runpy import run_path\nrun_path('csk/sources/evil.py')\n",
        "runpy-run-path",
    ),
    (
        "import sys\n"
        "_get = getattr(sys.modules['importlib'], 'import_module')\n"
        "_get('csk.sources.evil')\n",
        "getattr-importlib",
    ),
)


@pytest.mark.parametrize("body", [body for body, _ in _RUNTIME_COVERED_SILENT_SHAPES],
                         ids=[ident for _, ident in _RUNTIME_COVERED_SILENT_SHAPES])
def test_closure_dynamic_shape_is_runtime_covered(
    tmp_path: Path, body: str
) -> None:
    """Novel dynamic spellings are statically silent and runtime-caught.

    The static enumeration is defence in depth only; these shapes are
    asserted silent here so a future enumeration change cannot silently
    alter the contract. Their cover is the runtime-vs-static observation:
    each shape loads ``evil`` at import time, so the shadow end-to-end
    for the same family fires via the runtime half of the shipped seam
    test (``test_shadow_package_scope_violation_detected``).
    """

    (tmp_path / "selection.py").write_text(body, encoding="utf-8")
    (tmp_path / "evil.py").write_text(_CLOSURE_EVIL, encoding="utf-8")
    closure, violations = _walk_selection_imports(tmp_path)
    assert violations == [], f"shape unexpectedly refused: {violations}"
    assert "evil.py" not in closure, (
        f"shape unexpectedly queued by static walk: {sorted(closure)}"
    )


def test_closure_matches_reviewed_selection_tree() -> None:
    """The real-tree closure is exactly the reviewed module set, no refusal."""

    source_dir = Path(_selection_fs.__file__).parent
    closure, violations = _walk_selection_imports(source_dir)
    assert violations == []
    assert closure == {
        "selection.py",
        "_selection_fs.py",
        "errors.py",
        "skillfile_v2.py",
        "__init__.py",
    }


_SHADOW_SEAM_DRIVER = (
    "import importlib.util\n"
    "import os\n"
    "import sys\n"
    "from pathlib import Path\n"
    "\n"
    "shadow_root, shipped_test, expect = sys.argv[1], sys.argv[2], sys.argv[3]\n"
    "sys.path.insert(0, shadow_root)\n"
    "spec = importlib.util.spec_from_file_location('_shipped_boundary', shipped_test)\n"
    "assert spec is not None and spec.loader is not None\n"
    "module = importlib.util.module_from_spec(spec)\n"
    "sys.modules['_shipped_boundary'] = module\n"
    "spec.loader.exec_module(module)\n"
    "import csk.sources._selection_fs as fs\n"
    "resolved = str(Path(fs.__file__).resolve())\n"
    "wanted = str(Path(shadow_root).resolve())\n"
    "assert resolved.startswith(wanted + os.sep), (resolved, wanted)\n"
    "try:\n"
    "    module.test_selection_modules_have_one_filesystem_primitive_seam()\n"
    "except AssertionError as exc:\n"
    "    print(f'GATE FIRED: {exc}')\n"
    "    outcome = 'violation'\n"
    "else:\n"
    "    print('GATE SILENT')\n"
    "    outcome = 'clean'\n"
    "sys.exit(0 if outcome == expect else 1)\n"
)


@pytest.mark.parametrize(
    "family,mutation,evil_files,expect",
    [
        pytest.param(
            "importlib",
            "\nimport importlib\n"
            "importlib.import_module('csk.sources.evil_shadow')  # noqa\n",
            {"evil_shadow.py": _CLOSURE_EVIL},
            "violation",
            id="importlib",
        ),
        pytest.param(
            "subpackage",
            "\nfrom .subpkg import leak  # noqa\n",
            {"subpkg/__init__.py": _CLOSURE_EVIL},
            "violation",
            id="subpackage",
        ),
        pytest.param(
            "import-subpackage",
            "\nimport csk.sources.subpkg  # noqa\n",
            {"subpkg/__init__.py": _CLOSURE_EVIL},
            "violation",
            id="import-subpackage",
        ),
        pytest.param(
            "read-bytes",
            "\nfrom .subpkg import leak  # noqa\n",
            {
                "subpkg/__init__.py": "from pathlib import Path\n"
                "\n\ndef leak(path):\n"
                "    return Path(path).read_bytes()\n"
            },
            "violation",
            id="read-bytes",
        ),
        pytest.param(
            "runpy-from-import",
            "\nfrom runpy import run_module  # noqa\n"
            "run_module('csk.sources.evil_shadow')  # noqa\n",
            {"evil_shadow.py": _CLOSURE_EVIL},
            "violation",
            id="runpy-from-import",
        ),
        pytest.param(
            "runpy-from-alias",
            "\nfrom runpy import run_module as rm  # noqa\n"
            "rm('csk.sources.evil_shadow')  # noqa\n",
            {"evil_shadow.py": _CLOSURE_EVIL},
            "violation",
            id="runpy-from-alias",
        ),
        pytest.param(
            "runpy-run-path",
            "\nfrom runpy import run_path  # noqa\n"
            "run_path(str(Path(__file__).parent / 'evil_path.py'))  # noqa\n",
            {"evil_path.py": _CLOSURE_EVIL},
            "violation",
            id="runpy-run-path",
        ),
        pytest.param(
            "getattr-importlib",
            "\nimport sys  # noqa\n"
            "_get = getattr(sys.modules['importlib'], 'import_module')  # noqa\n"
            "_get('csk.sources.evil_shadow')  # noqa\n",
            {"evil_shadow.py": _CLOSURE_EVIL},
            "violation",
            id="getattr-importlib",
        ),
        pytest.param(
            "star-all",
            "\nfrom . import *  # noqa\n",
            {
                "evil_star.py": _CLOSURE_EVIL,
                "__init__.py": "__all__ = ['evil_star']\n",
            },
            "violation",
            id="star-all",
        ),
        pytest.param("control", None, {}, "clean", id="control"),
    ],
)
def test_shadow_package_scope_violation_detected(
    tmp_path: Path,
    family: str,
    mutation: str | None,
    evil_files: dict[str, str],
    expect: str,
) -> None:
    """The shipped seam test fires on a shadowed package carrying a miss shape.

    Replicates the revision-24 through revision-26 reviews end to end: a
    full copy of ``csk`` with the selection module extended by one
    dynamic/subpackage import and an ``os.open`` in the imported module
    must fail the REAL shipped seam test, while the unmutated shadow stays
    clean (the harness-validity control). The ``runpy``/``getattr``/``star``
    families are statically silent by design (see
    ``test_closure_dynamic_shape_is_runtime_covered``) and fire only via
    the runtime half of the shipped gate.
    """

    package_root = Path(_selection_fs.__file__).parent.parent
    shadow_root = tmp_path / "shadow"
    shutil.copytree(
        package_root,
        shadow_root / "csk",
        ignore=shutil.ignore_patterns("__pycache__"),
    )
    if mutation is not None:
        selection_path = shadow_root / "csk" / "sources" / "selection.py"
        selection_path.write_text(
            selection_path.read_text(encoding="utf-8") + mutation, encoding="utf-8"
        )
    for relative, content in evil_files.items():
        target = shadow_root / "csk" / "sources" / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
    driver = tmp_path / "shadow_driver.py"
    driver.write_text(_SHADOW_SEAM_DRIVER, encoding="utf-8")
    proc = subprocess.run(
        [sys.executable, str(driver), str(shadow_root), str(Path(__file__)), expect],
        capture_output=True,
        text=True,
        timeout=300,
    )
    assert proc.returncode == 0, (
        f"family={family} expect={expect}:\nstdout={proc.stdout}\n"
        f"stderr={proc.stderr[-2000:]}"
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


# ---------------------------------------------------------------------------
# Moved boundary ownership (TASK-260916-2wjh3m re-entry): the selection leaf
# owns selector-to-member-set semantics; the filesystem access layer
# (TASK-260917-3q5h87) owns every read/boundary/traversal mechanic below.
# These tests moved here intact (same names, same fixtures, same assertions);
# only the shared _write_skill/_collection test helpers were unified (a
# backward-compatible superset) so both suites share them.
# ---------------------------------------------------------------------------

def _same_file(first: Path, second: Path) -> bool:
    try:
        return os.path.samefile(first, second)
    except OSError:
        return False


def _counting_content_reads(
    monkeypatch: pytest.MonkeyPatch,
) -> list[tuple[str, str]]:
    calls: list[tuple[str, str]] = []
    orig_read_text = Path.read_text
    orig_read_bytes = Path.read_bytes

    def counting_read_text(self: Path, *args: object, **kwargs: object) -> str:
        calls.append(("text", str(self)))
        return orig_read_text(self, *args, **kwargs)  # type: ignore[arg-type]

    def counting_read_bytes(self: Path, *args: object, **kwargs: object) -> bytes:
        calls.append(("bytes", str(self)))
        return orig_read_bytes(self, *args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(Path, "read_text", counting_read_text)
    monkeypatch.setattr(Path, "read_bytes", counting_read_bytes)
    return calls


def _probes_same_file(first: Path, second: Path) -> bool:
    try:
        return first.exists() and os.path.samefile(first, second)
    except OSError:
        return False


def _review_write(path: Path, name: str = "review", description: str = "valid") -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f"---\nname: {name}\ndescription: {description}\n---\n")


_PRE_READ_LINK_SHAPES: tuple[str, ...] = (
    "references-git",
    "references-agents",
    "references-codex",
    "deep-nested",
    "member-root-file",
    "member-dir-link",
)


def _install_pre_read_link_shape(member: Path, shape: str, outside_file: Path) -> None:
    if shape == "references-git":
        hidden = member / "references" / ".git"
        hidden.mkdir(parents=True)
        (hidden / "leak.md").symlink_to(outside_file)
    elif shape == "references-agents":
        hidden = member / "references" / ".agents"
        hidden.mkdir(parents=True)
        (hidden / "leak.md").symlink_to(outside_file)
    elif shape == "references-codex":
        hidden = member / "references" / ".codex"
        hidden.mkdir(parents=True)
        (hidden / "leak.md").symlink_to(outside_file)
    elif shape == "deep-nested":
        hidden = member / "references" / "deep" / "nested" / "dir"
        hidden.mkdir(parents=True)
        (hidden / "leak.md").symlink_to(outside_file)
    elif shape == "member-root-file":
        (member / "evil.md").symlink_to(outside_file)
    elif shape == "member-dir-link":
        outside_dir = outside_file.parent
        outside_dir.mkdir(parents=True, exist_ok=True)
        (member / "references").mkdir(parents=True, exist_ok=True)
        (member / "references" / "linked").symlink_to(
            outside_dir, target_is_directory=True
        )
    else:  # pragma: no cover - exhaustive parametrize ids
        raise AssertionError(f"unknown pre-read shape {shape!r}")


_CYCLIC_LINK_POSITIONS: tuple[str, ...] = (
    "member-root-self-loop",
    "nested-two-link-cycle",
    "references-git-self-loop",
    "skill-md-self-loop",
)


def _install_cyclic_link_position(member: Path, position: str) -> None:
    if position == "member-root-self-loop":
        (member / "loop.md").symlink_to("loop.md")
    elif position == "nested-two-link-cycle":
        nested = member / "docs" / "deep"
        nested.mkdir(parents=True)
        (nested / "a.md").symlink_to("b.md")
        (nested / "b.md").symlink_to("a.md")
    elif position == "references-git-self-loop":
        hidden = member / "references" / ".git"
        hidden.mkdir(parents=True)
        (hidden / "loop.md").symlink_to("loop.md")
    elif position == "skill-md-self-loop":
        (member / "SKILL.md").unlink()
        (member / "SKILL.md").symlink_to("SKILL.md")
    else:  # pragma: no cover - exhaustive parametrize ids
        raise AssertionError(f"unknown cyclic position {position!r}")


_BOUNDARY_UNICODE_NFC = "sourc\u00e9"


_BOUNDARY_UNICODE_NFD = unicodedata.normalize("NFD", _BOUNDARY_UNICODE_NFC)


def test_selector_inside_symlink_resolves(tmp_path: Path) -> None:
    root = tmp_path / "src"
    _write_skill(root / "real" / "review", "review")
    (root / "link").symlink_to(root / "real", target_is_directory=True)
    resolved = resolve_selector_directory(root, "link/review")
    assert resolved == (root / "real" / "review").resolve()
    individual = resolve_individual(
        root, IndividualSelector(name="review", from_alias="local", directory="link/review")
    )
    assert individual.name == "review"


@pytest.mark.parametrize("entry", ["individual", "collection"])
@pytest.mark.parametrize("spelling", ["root-alias", "case-equivalent"])
def test_absolute_contained_link_resolves(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    entry: str,
    spelling: str,
) -> None:
    """Phase-A identity containment admits contained absolute link targets.

    The root may itself be supplied through a symlink, and on a
    case-insensitive filesystem the link target may use a different spelling
    of the root component. The selected package is the real ``review``
    directory below the intermediate link, not the link itself.
    """

    real_root = tmp_path / "source"
    target = real_root / "target"
    _write_skill(target / "review", "review")
    if spelling == "root-alias":
        root = tmp_path / "source-alias"
        root.symlink_to(real_root, target_is_directory=True)
        absolute_target = target
    else:
        root = real_root
        case_parent = tmp_path / "SOURCE"
        if not _same_file(case_parent, real_root):
            pytest.skip("case-insensitive filesystem unavailable: absolute target bound")
        absolute_target = case_parent / "target"

    (real_root / "alias").symlink_to(absolute_target, target_is_directory=True)
    monkeypatch.setenv("CSK_CONFIG", str(tmp_path / "home" / "config.json"))
    if entry == "individual":
        selected = resolve_individual(
            root,
            IndividualSelector(
                name="review", from_alias="local", directory="alias/review"
            ),
        )
        assert selected.name == "review"
    else:
        selected = expand_collection(
            root, _collection("alias", ("review",))
        )
        assert [member.name for member in selected] == ["review"]


def test_selector_escape_never_reads_outside(tmp_path: Path) -> None:
    """The escape refusal does not depend on outside bytes.

    Both a valid and an invalid outside skill fail with the same selection
    code, proving the gate fires before any member read.
    """

    for variant in ("valid", "invalid"):
        root = tmp_path / f"src-{variant}"
        root.mkdir()
        outside = tmp_path / f"outside-{variant}" / "review"
        outside.mkdir(parents=True)
        if variant == "valid":
            _write_skill(outside, "review")
        else:
            (outside / "SKILL.md").write_text("no frontmatter here\n", encoding="utf-8")
        (root / "skills").symlink_to(tmp_path / f"outside-{variant}", target_is_directory=True)
        with pytest.raises(source_errors.SourceError) as excinfo:
            resolve_selector_directory(root, "skills/review")
        assert excinfo.value.code == source_errors.CODE_SELECTION_INVALID


@pytest.mark.parametrize("entry", ["individual", "collection"])
def test_missing_component_before_parent_is_not_existing_selector(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, entry: str
) -> None:
    """A missing symlink component is never collapsed across ``..``."""

    root = tmp_path / "source"
    package = root / "real" / "review"
    _write_skill(package, "review", "A review skill")
    monkeypatch.setenv("CSK_CONFIG", str(tmp_path / "home" / "config.json"))
    link = root / "link"
    link.symlink_to("missing/../real", target_is_directory=True)
    assert not link.exists()
    assert not link.is_dir()
    if entry == "individual":
        run = lambda directory: resolve_individual(
            root,
            IndividualSelector(
                name="review", from_alias="local", directory=f"{directory}/review"
            ),
        )
    else:
        run = lambda directory: expand_collection(
            root, _collection(directory, ("*",))
        )
    run("real")
    with pytest.raises(source_errors.SourceError) as excinfo:
        run("link")
    assert excinfo.value.code == source_errors.CODE_SELECTION_INVALID


def test_selection_never_reads_outside_member_symlink(tmp_path: Path) -> None:
    root = tmp_path / "src"
    collection = root / "col"
    collection.mkdir(parents=True)
    outside = tmp_path / "outside-skill"
    _write_skill(outside, "evil")
    (collection / "evil").symlink_to(outside, target_is_directory=True)
    with pytest.raises(source_errors.SourceError) as excinfo:
        expand_collection(root, _collection("col", ("evil",)))
    assert excinfo.value.code == source_errors.CODE_SELECTION_INVALID


def test_selection_case_variant_directory_bound(tmp_path: Path) -> None:
    """Case-variant containment on a case-insensitive filesystem.

    On a case-sensitive host the two spellings are distinct directories and
    the test declares the platform bound instead of forcing a collision.
    """

    root = tmp_path / "src"
    (root / "Skills").mkdir(parents=True)
    _write_skill(root / "Skills" / "review", "review")
    probe = root / "SKILLS"
    try:
        probe.mkdir(exist_ok=False)
    except FileExistsError:
        pass
    if not _same_file(root / "Skills", probe):
        pytest.skip("case-insensitive filesystem not available: bound declared")
    resolved = resolve_selector_directory(root, "SKILLS/review")
    assert resolved.is_dir()


def test_member_skill_md_symlink_escape_rejected_without_external_read(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An escaping SKILL.md link fails before its outside bytes are opened."""

    root = tmp_path / "src"
    member = root / "col" / "review"
    member.mkdir(parents=True)
    outside = tmp_path / "outside.md"
    outside.write_text(
        "---\nname: evil\ndescription: evil outside\n---\n# evil\n", encoding="utf-8"
    )
    (member / "SKILL.md").symlink_to(outside)
    calls = _counting_content_reads(monkeypatch)
    with pytest.raises(source_errors.SourceError) as excinfo:
        expand_collection(root, _collection("col", ("review",)))
    assert excinfo.value.code == source_errors.CODE_MEMBER_INVALID
    assert "escapes the source root" in excinfo.value.detail
    assert calls == [], f"outside bytes were opened: {calls}"


def test_individual_skill_md_symlink_escape_rejected_without_external_read(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "src"
    member = root / "skills" / "review"
    member.mkdir(parents=True)
    outside = tmp_path / "outside.md"
    outside.write_text(
        "---\nname: review\ndescription: valid outside\n---\n# review\n",
        encoding="utf-8",
    )
    (member / "SKILL.md").symlink_to(outside)
    calls = _counting_content_reads(monkeypatch)
    with pytest.raises(source_errors.SourceError) as excinfo:
        resolve_individual(
            root,
            IndividualSelector(
                name="review", from_alias="local", directory="skills/review"
            ),
        )
    assert excinfo.value.code == source_errors.CODE_MEMBER_INVALID
    assert "escapes the source root" in excinfo.value.detail
    assert calls == [], f"outside bytes were opened: {calls}"


def test_member_manifest_symlink_escape_rejected_without_external_read(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An escaping manifest link fails before any package content is read."""

    root = tmp_path / "src"
    member = root / "col" / "review"
    _write_skill(member, "review")
    outside = tmp_path / "outside-manifest.json"
    outside.write_text('{"schema_version": 1, "name": "review"}', encoding="utf-8")
    (member / "agent-skill.json").symlink_to(outside)
    calls = _counting_content_reads(monkeypatch)
    with pytest.raises(source_errors.SourceError) as excinfo:
        expand_collection(root, _collection("col", ("review",)))
    assert excinfo.value.code == source_errors.CODE_MEMBER_INVALID
    assert "escapes the source root" in excinfo.value.detail
    assert calls == [], f"outside bytes were opened: {calls}"


def test_member_nested_symlink_escape_rejected_before_skillcheck_read(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A nested escaping link fails before skillcheck opens package files."""

    root = tmp_path / "src"
    member = root / "col" / "review"
    _write_skill(member, "review")
    references = member / "references"
    references.mkdir()
    outside = tmp_path / "outside-note.md"
    outside.write_text("# outside\n", encoding="utf-8")
    (references / "evil.md").symlink_to(outside)
    calls = _counting_content_reads(monkeypatch)
    with pytest.raises(source_errors.SourceError) as excinfo:
        expand_collection(root, _collection("col", ("review",)))
    assert excinfo.value.code == source_errors.CODE_MEMBER_INVALID
    assert "escapes the source root" in excinfo.value.detail
    assert calls == [], f"outside bytes were opened: {calls}"


def test_member_inside_skill_md_link_rejected(tmp_path: Path) -> None:
    """Links inside admitted inputs are rejected even without an escape."""

    root = tmp_path / "src"
    member = root / "col" / "review"
    member.mkdir(parents=True)
    (member / "real.md").write_text(
        "---\nname: review\ndescription: inside target\n---\n# review\n",
        encoding="utf-8",
    )
    (member / "SKILL.md").symlink_to(member / "real.md")
    with pytest.raises(source_errors.SourceError) as excinfo:
        expand_collection(root, _collection("col", ("review",)))
    assert excinfo.value.code == source_errors.CODE_MEMBER_INVALID
    assert "link" in excinfo.value.detail


def test_collection_star_prunes_case_alias_git(tmp_path: Path) -> None:
    """On a case-insensitive host `.GIT` prunes as `.git` (authored control kept)."""

    root = tmp_path / "src"
    base = root / "col"
    _write_skill(base / "normal", "review")
    _write_skill(base / ".GIT", "generated")
    if not _probes_same_file(base / ".git", base / ".GIT"):
        pytest.skip(
            "case-insensitive filesystem not available: "
            ".GIT/.git alias bound (pruning needs actual FS equivalence)"
        )
    members = expand_collection(root, _collection("col", ("*",)))
    assert [member.name for member in members] == ["review"]


def test_collection_star_prunes_case_alias_managed_output(tmp_path: Path) -> None:
    """On a case-insensitive host `.AGENTS` prunes as `.agents`."""

    root = tmp_path / "src"
    base = root / "col"
    _write_skill(base / "normal", "review")
    _write_skill(base / ".AGENTS", "generated")
    if not _probes_same_file(base / ".agents", base / ".AGENTS"):
        pytest.skip(
            "case-insensitive filesystem not available: "
            ".AGENTS/.agents alias bound (pruning needs actual FS equivalence)"
        )
    members = expand_collection(root, _collection("col", ("*",)))
    assert [member.name for member in members] == ["review"]


def test_collection_star_case_variant_distinct_is_selected(tmp_path: Path) -> None:
    """Without FS aliasing a case variant stays authored input (no naive fold)."""

    root = tmp_path / "src"
    base = root / "col"
    _write_skill(base / "normal", "review")
    _write_skill(base / ".GIT", "generated")
    if _probes_same_file(base / ".git", base / ".GIT"):
        pytest.skip(
            "case-insensitive filesystem aliases .GIT/.git: "
            "distinct-input bound (pruning test covers this host)"
        )
    members = expand_collection(root, _collection("col", ("*",)))
    assert [member.folder for member in members] == sorted(
        ["normal", ".GIT"], key=lambda value: value.encode("utf-8")
    )


def test_collection_star_prunes_linked_alias_git(tmp_path: Path) -> None:
    """A symlink to the sibling `.git` directory prunes like `.git` itself."""

    root = tmp_path / "src"
    base = root / "col"
    _write_skill(base / "normal", "review")
    pruned = base / ".git"
    pruned.mkdir(parents=True)
    (pruned / "SKILL.md").write_text("broken\n", encoding="utf-8")
    (base / "evil-link").symlink_to(pruned, target_is_directory=True)
    members = expand_collection(root, _collection("col", ("*",)))
    assert [member.folder for member in members] == ["normal"]


def test_collection_star_prunes_linked_alias_managed_output(tmp_path: Path) -> None:
    """A symlink to the sibling `.agents` directory prunes with it."""

    root = tmp_path / "src"
    base = root / "col"
    _write_skill(base / "normal", "review")
    pruned = base / ".agents"
    pruned.mkdir(parents=True)
    (pruned / "SKILL.md").write_text("broken\n", encoding="utf-8")
    (base / "agents-link").symlink_to(pruned, target_is_directory=True)
    members = expand_collection(root, _collection("col", ("*",)))
    assert [member.folder for member in members] == ["normal"]


@pytest.mark.parametrize("managed", [".agents", ".git", ".codex"])
def test_managed_descendant_alias_pruned(tmp_path: Path, managed: str) -> None:
    """A child alias into a managed-output DESCENDANT prunes (subtree class).

    The target carries valid SKILL.md, so without subtree containment it
    would be discovered as `generated` alongside the authored skill.
    """

    root = tmp_path / "src"
    _write_skill(root / "normal", "review")
    _write_skill(root / managed / "nested" / "generated", "generated")
    (root / "alias").symlink_to(
        root / managed / "nested" / "generated", target_is_directory=True
    )
    members = expand_collection(root, _collection(".", ("*",)))
    assert [member.name for member in members] == ["review"]


def test_managed_descendant_alias_chain_pruned(tmp_path: Path) -> None:
    """A two-hop link chain into a managed descendant still prunes."""

    root = tmp_path / "src"
    _write_skill(root / "normal", "review")
    _write_skill(root / ".agents" / "nested" / "deep-target", "deep")
    (root / "hop").symlink_to(
        root / ".agents" / "nested" / "deep-target", target_is_directory=True
    )
    (root / "alias").symlink_to(root / "hop", target_is_directory=True)
    members = expand_collection(root, _collection(".", ("*",)))
    assert [member.name for member in members] == ["review"]


def test_managed_descendant_alias_pruned_from_nested_base(tmp_path: Path) -> None:
    """A child alias into the source-root managed subtree prunes from any base.

    Per protocol section 2 ("reject a selected package within ANY managed
    output") the managed roots are anchored at the source root as well as at
    the enumerated collection base.
    """

    root = tmp_path / "src"
    _write_skill(root / "col" / "normal", "review")
    _write_skill(root / ".agents" / "nested" / "nested-out", "nested")
    (root / "col" / "alias").symlink_to(
        root / ".agents" / "nested" / "nested-out", target_is_directory=True
    )
    members = expand_collection(root, _collection("col", ("*",)))
    assert [member.name for member in members] == ["review"]


@pytest.mark.parametrize("managed", [".agents", ".git", ".codex"])
def test_nested_managed_descendant_alias_pruned(tmp_path: Path, managed: str) -> None:
    """A child alias into a NESTED-workspace managed descendant prunes.

    Committed reviewer attack (rev3): the target lives under an intermediate
    ``workspace`` directory, so anchor enumeration at the source root or the
    collection base cannot see it. Only the physical-ancestry walk prunes it.
    """

    root = tmp_path / "src"
    base = root / "collection"
    _write_skill(base / "authored", "review")
    generated = root / "workspace" / managed / "nested" / "generated"
    _write_skill(generated, "generated")
    (base / "alias").symlink_to(generated, target_is_directory=True)
    members = expand_collection(root, _collection("collection", ("*",)))
    assert [member.name for member in members] == ["review"]


def test_collection_star_prunes_child_inside_csk_home(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A child alias into the csk home prunes (runtime/staging/snapshot outputs)."""

    home = tmp_path / "home"
    monkeypatch.setenv("CSK_CONFIG", str(home / "config.json"))
    root = tmp_path / "src"
    base = root / "col"
    _write_skill(base / "normal", "review")
    generated = home / "source-v1" / "snapshots" / "generated"
    _write_skill(generated, "generated")
    (base / "alias").symlink_to(generated, target_is_directory=True)
    members = expand_collection(root, _collection("col", ("*",)))
    assert [member.name for member in members] == ["review"]


@pytest.mark.parametrize("entry", ["individual", "collection"])
@pytest.mark.parametrize(
    "shape",
    ["direct", "nested", "link", "case-variant", "csk-home"],
    ids=["direct-.agents", "nested-workspace", "link-into-managed", "case-variant", "csk-home"],
)
def test_managed_boundary_explicit_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, entry: str, shape: str
) -> None:
    """Every explicit managed selection refuses with source_output_overlap.

    Covers both production entry points (individual selector, literal include)
    for direct, nested-workspace, linked-alias, case-variant and csk-home
    shapes. The predicate runs before any metadata read, so even a valid
    SKILL.md inside managed output refuses.
    """

    if shape == "direct":
        root = tmp_path / "src"
        _write_skill(root / ".agents", "review", "Generated output")
        directory = ".agents"
        collection_base = "."
        literal = ".agents"
    elif shape == "nested":
        root = tmp_path / "src"
        _write_skill(root / "workspace" / ".agents" / "nested" / "generated", "review")
        directory = "workspace/.agents/nested/generated"
        collection_base = "workspace/.agents/nested"
        literal = "generated"
    elif shape == "link":
        root = tmp_path / "src"
        _write_skill(root / ".agents" / "nested" / "target", "review")
        (root / "col").mkdir(parents=True)
        (root / "col" / "alias").symlink_to(
            root / ".agents" / "nested" / "target", target_is_directory=True
        )
        directory = "col/alias"
        collection_base = "col"
        literal = "alias"
    elif shape == "case-variant":
        root = tmp_path / "src"
        _write_skill(root / ".AGENTS", "review", "Generated output")
        if not _probes_same_file(root / ".agents", root / ".AGENTS"):
            pytest.skip(
                "case-insensitive filesystem not available: "
                ".AGENTS/.agents explicit bound"
            )
        directory = ".AGENTS"
        collection_base = "."
        literal = ".AGENTS"
    else:
        home = tmp_path / "home"
        monkeypatch.setenv("CSK_CONFIG", str(home / "config.json"))
        root = home / "src"
        _write_skill(root / "pkg", "review")
        directory = "pkg"
        collection_base = "."
        literal = "pkg"
    with pytest.raises(source_errors.SourceError) as excinfo:
        if entry == "individual":
            resolve_individual(
                root,
                IndividualSelector(name="review", from_alias="local", directory=directory),
            )
        else:
            expand_collection(root, _collection(collection_base, (literal,)))
    assert excinfo.value.code == source_errors.CODE_OUTPUT_OVERLAP


@pytest.mark.parametrize(
    "shape",
    ["direct", "nested", "link", "case-variant", "csk-home"],
    ids=["direct-.agents", "nested-workspace", "link-into-managed", "case-variant", "csk-home"],
)
def test_managed_boundary_wildcard_pruned(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, shape: str
) -> None:
    """The same predicate prunes silently for '*' discovery."""

    if shape == "csk-home":
        home = tmp_path / "home"
        monkeypatch.setenv("CSK_CONFIG", str(home / "config.json"))
        root = tmp_path / "src"
        base = root / "col"
        _write_skill(base / "normal", "review")
        generated = home / "source-v1" / "snapshots" / "generated"
        _write_skill(generated, "generated")
        (base / "alias").symlink_to(generated, target_is_directory=True)
    elif shape == "nested":
        root = tmp_path / "src"
        base = root / "col"
        _write_skill(base / "normal", "review")
        generated = root / "workspace" / ".agents" / "nested" / "generated"
        _write_skill(generated, "generated")
        (base / "alias").symlink_to(generated, target_is_directory=True)
    elif shape == "link":
        root = tmp_path / "src"
        base = root / "col"
        _write_skill(base / "normal", "review")
        target = root / ".agents" / "nested" / "target"
        _write_skill(target, "generated")
        (base / "alias").symlink_to(target, target_is_directory=True)
    elif shape == "case-variant":
        root = tmp_path / "src"
        base = root / "col"
        _write_skill(base / "normal", "review")
        _write_skill(base / ".AGENTS", "generated")
        if not _probes_same_file(base / ".agents", base / ".AGENTS"):
            pytest.skip(
                "case-insensitive filesystem not available: "
                ".AGENTS/.agents wildcard bound"
            )
    else:
        root = tmp_path / "src"
        base = root / "col"
        _write_skill(base / "normal", "review")
        _write_skill(base / ".agents", "generated")
    members = expand_collection(root, _collection("col", ("*",)))
    assert [member.name for member in members] == ["review"]


def test_managed_boundary_predicate_is_single_source(tmp_path: Path) -> None:
    """The predicate reports every shape; discovery and explicit share it."""

    root = tmp_path / "src"
    target = root / "workspace" / ".agents" / "nested" / "pkg"
    target.mkdir(parents=True)
    _write_skill(target, "review")
    _write_skill(root / "ordinary", "ordinary")
    with pytest.raises(source_errors.SourceError) as managed:
        resolve_individual(
            root,
            IndividualSelector(name="review", from_alias="local", directory="workspace/.agents/nested/pkg"),
        )
    assert managed.value.code == source_errors.CODE_OUTPUT_OVERLAP
    ordinary = resolve_individual(
        root,
        IndividualSelector(name="ordinary", from_alias="local", directory="ordinary"),
    )
    assert ordinary.name == "ordinary"


def test_review_external_skill_md_is_rejected(tmp_path: Path) -> None:
    """Committed reviewer attack (rev1): escaping SKILL.md link refuses."""

    root = tmp_path / "src"
    member = root / "member"
    member.mkdir(parents=True)
    _review_write(tmp_path / "outside.md")
    (member / "SKILL.md").symlink_to(tmp_path / "outside.md")
    with pytest.raises(source_errors.SourceError):
        expand_collection(root, CollectionSelector("local", ".", ("*",), ()))


def test_review_case_equivalent_git_is_pruned(tmp_path: Path) -> None:
    """Committed reviewer attack (rev1): case-equivalent .git prunes where aliased."""

    root = tmp_path / "src"
    _review_write(root / "normal" / "SKILL.md")
    _review_write(root / ".GIT" / "SKILL.md", name="generated")
    if not (root / ".git").exists():
        pytest.skip("case-insensitive filesystem unavailable")
    assert [
        member.name
        for member in expand_collection(root, CollectionSelector("local", ".", ("*",), ()))
    ] == ["review"]


@pytest.mark.parametrize('entry', ['collection', 'individual'])
@pytest.mark.parametrize('managed', ['.git', '.agents', '.codex'])
def test_pruned_nested_tree_never_read_outside(tmp_path, monkeypatch, entry, managed):
    # Committed from the rev15 review: `references/<managed>/leak.md ->
    # /outside` bypassed `_reject_links_in_member` (which skipped pruned
    # names) and was opened by `skillcheck._prompt_markdown_files`
    # (`references.rglob("*.md")` without that pruning). The pre-read walk
    # now covers every entry with no name pruning, so no outside bytes open.
    monkeypatch.setenv('CSK_CONFIG', str(tmp_path / 'home/config.json'))
    root = tmp_path / 'source'
    member = root / 'review'
    hidden = member / 'references' / managed
    hidden.mkdir(parents=True)
    outside = tmp_path / 'outside.md'
    outside.write_text('scripts/tool outside bytes', encoding='utf-8')
    (hidden / 'leak.md').symlink_to(outside)
    (member / 'SKILL.md').write_text('---\nname: review\ndescription: valid\n---\n')
    (member / 'scripts').mkdir()
    (member / 'scripts/tool').write_text('#!/bin/sh\n')
    (member / 'agent-skill.json').write_text(json.dumps({'schema_version': 2, 'runtime_roots': ['scripts'], 'commands': {'tool': {'type': 'script', 'unix_path': 'scripts/tool'}}}))
    reads = []
    original = Path.read_text
    def watched(path, *args, **kwargs):
        if path.resolve() == outside.resolve():
            reads.append(str(path.relative_to(member)))
        return original(path, *args, **kwargs)
    monkeypatch.setattr(Path, 'read_text', watched)
    try:
        if entry == 'collection':
            expand_collection(root, CollectionSelector(from_alias='local', directory='.', include=('*',), exclude=()))
        else:
            resolve_individual(root, IndividualSelector(name='review', from_alias='local', directory='review'))
    except source_errors.SourceError:
        pass
    assert reads == [], f'External reads through pruned tree: {reads}'


@pytest.mark.parametrize("entry", ["collection", "individual"])
@pytest.mark.parametrize(
    "shape",
    [pytest.param(shape, id=shape) for shape in _PRE_READ_LINK_SHAPES],
)
def test_pre_read_walk_refuses_link_without_outside_read(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, entry: str, shape: str
) -> None:
    """Every link shape refuses AND opens zero outside bytes on BOTH entries."""

    monkeypatch.setenv("CSK_CONFIG", str(tmp_path / "home" / "config.json"))
    root = tmp_path / "src"
    member = root / "review"
    member.mkdir(parents=True)
    if shape == "member-dir-link":
        outside_dir = tmp_path / "outside-dir"
        outside_dir.mkdir()
        outside_file = outside_dir / "inside.md"
        outside_file.write_text("scripts/tool outside bytes", encoding="utf-8")
    else:
        outside_file = tmp_path / "outside.md"
        outside_file.write_text("scripts/tool outside bytes", encoding="utf-8")
    _install_pre_read_link_shape(member, shape, outside_file)
    (member / "SKILL.md").write_text(
        "---\nname: review\ndescription: valid\n---\n", encoding="utf-8"
    )
    (member / "scripts").mkdir(exist_ok=True)
    (member / "scripts" / "tool").write_text("#!/bin/sh\n", encoding="utf-8")
    (member / "agent-skill.json").write_text(
        json.dumps(
            {
                "schema_version": 2,
                "runtime_roots": ["scripts"],
                "commands": {
                    "tool": {"type": "script", "unix_path": "scripts/tool"}
                },
            }
        ),
        encoding="utf-8",
    )
    outside_root = outside_file.resolve().parent if shape == "member-dir-link" else None
    outside_resolved = outside_file.resolve()
    reads: list[str] = []
    original_read_text = Path.read_text
    original_read_bytes = Path.read_bytes

    def watched_text(path: Path, *args: object, **kwargs: object) -> str:
        try:
            resolved = path.resolve()
        except OSError:
            resolved = path
        if resolved == outside_resolved or (
            outside_root is not None
            and (resolved == outside_root or outside_root in resolved.parents)
        ):
            reads.append(str(path))
        return original_read_text(path, *args, **kwargs)  # type: ignore[arg-type]

    def watched_bytes(path: Path, *args: object, **kwargs: object) -> bytes:
        try:
            resolved = path.resolve()
        except OSError:
            resolved = path
        if resolved == outside_resolved or (
            outside_root is not None
            and (resolved == outside_root or outside_root in resolved.parents)
        ):
            reads.append(str(path))
        return original_read_bytes(path, *args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(Path, "read_text", watched_text)
    monkeypatch.setattr(Path, "read_bytes", watched_bytes)
    with pytest.raises(source_errors.SourceError) as excinfo:
        if entry == "collection":
            expand_collection(
                root,
                CollectionSelector(
                    from_alias="local", directory=".", include=("*",), exclude=()
                ),
            )
        else:
            resolve_individual(
                root,
                IndividualSelector(
                    name="review", from_alias="local", directory="review"
                ),
            )
    assert excinfo.value.code == source_errors.CODE_MEMBER_INVALID
    assert reads == [], f"outside bytes were opened for {shape}: {reads}"


@pytest.mark.parametrize("entry", ["collection", "individual"])
def test_pre_read_walk_accepts_regular_pruned_name_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, entry: str
) -> None:
    """A regular `references/.git/notes.md` (no link) is accepted on BOTH entries.

    The pre-read walk has no name pruning, so the regular file is walked and
    contained; it is inside the member, so validation proceeds and the
    downstream `rglob` reads it safely.
    """

    monkeypatch.setenv("CSK_CONFIG", str(tmp_path / "home" / "config.json"))
    root = tmp_path / "src"
    member = root / "review"
    hidden = member / "references" / ".git"
    hidden.mkdir(parents=True)
    (hidden / "notes.md").write_text("# notes\n", encoding="utf-8")
    (member / "SKILL.md").write_text(
        "---\nname: review\ndescription: valid\n---\n", encoding="utf-8"
    )
    (member / "scripts").mkdir()
    (member / "scripts" / "tool").write_text("#!/bin/sh\n", encoding="utf-8")
    (member / "agent-skill.json").write_text(
        json.dumps(
            {
                "schema_version": 2,
                "runtime_roots": ["scripts"],
                "commands": {
                    "tool": {"type": "script", "unix_path": "scripts/tool"}
                },
            }
        ),
        encoding="utf-8",
    )
    if entry == "collection":
        members = expand_collection(
            root,
            CollectionSelector(
                from_alias="local", directory=".", include=("*",), exclude=()
            ),
        )
        assert [found.name for found in members] == ["review"]
    else:
        found = resolve_individual(
            root,
            IndividualSelector(name="review", from_alias="local", directory="review"),
        )
        assert found.name == "review"


@pytest.mark.parametrize("entry", ["collection", "individual"])
@pytest.mark.parametrize("shape", ["self-loop", "two-link-cycle", "fifo"])
def test_pre_read_walk_structured_refusal(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, entry: str, shape: str
) -> None:
    # Committed from the rev16 review: the pre-read walk resolved each link
    # before refusing and caught only OSError, so a self-referential or
    # two-link cyclic symlink raised RuntimeError on Python 3.12 instead of
    # source_member_invalid. Refusal is now decided from lstat alone, with
    # resolution kept diagnostic-only behind a never-raising wrapper.
    if shape == "fifo" and not hasattr(os, "mkfifo"):
        pytest.skip("os.mkfifo unavailable: fifo bound not observable on this host")
    monkeypatch.setenv("CSK_CONFIG", str(tmp_path / "home" / "config.json"))
    root = tmp_path / "source"
    member = root / "review"
    member.mkdir(parents=True)
    (member / "SKILL.md").write_text(
        "---\nname: review\ndescription: Review skill\n---\n", encoding="utf-8"
    )
    nested = member / "references" / ".git"
    nested.mkdir(parents=True)
    if shape == "self-loop":
        (nested / "loop.md").symlink_to("loop.md")
    elif shape == "two-link-cycle":
        (nested / "a.md").symlink_to("b.md")
        (nested / "b.md").symlink_to("a.md")
    else:
        os.mkfifo(nested / "pipe")
    with pytest.raises(source_errors.SourceError) as excinfo:
        if entry == "collection":
            expand_collection(root, _collection(".", ("*",), ()))
        else:
            resolve_individual(
                root,
                IndividualSelector(
                    name="review", from_alias="local", directory="review"
                ),
            )
    assert excinfo.value.code == source_errors.CODE_MEMBER_INVALID


@pytest.mark.parametrize("entry", ["collection", "individual"])
@pytest.mark.parametrize(
    "position",
    [pytest.param(position, id=position) for position in _CYCLIC_LINK_POSITIONS],
)
def test_cyclic_link_position_yields_structured_refusal(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, entry: str, position: str
) -> None:
    """A cyclic link at any member position refuses structured on BOTH entries.

    Covers the member root, a nested directory, `references/.git`, and the
    SKILL.md path itself: no path-based post-open resolution may leak
    the symlink-loop `RuntimeError` for any filesystem shape.
    """

    monkeypatch.setenv("CSK_CONFIG", str(tmp_path / "home" / "config.json"))
    root = tmp_path / "src"
    member = root / "review"
    member.mkdir(parents=True)
    (member / "SKILL.md").write_text(
        "---\nname: review\ndescription: valid\n---\n", encoding="utf-8"
    )
    _install_cyclic_link_position(member, position)
    with pytest.raises(source_errors.SourceError) as excinfo:
        if entry == "collection":
            expand_collection(root, _collection(".", ("*",), ()))
        else:
            resolve_individual(
                root,
                IndividualSelector(
                    name="review", from_alias="local", directory="review"
                ),
            )
    assert excinfo.value.code == source_errors.CODE_MEMBER_INVALID


def test_cyclic_skill_md_link_refused_before_read(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A self-referential SKILL.md link refuses without resolving its target.

    Drives the metadata-file gate (`read_skill_md_name` ->
    `_ensure_contained_regular_file`) directly: the link decision comes from
    `lstat` alone, so the cyclic target never raises.
    """

    monkeypatch.setenv("CSK_CONFIG", str(tmp_path / "home" / "config.json"))
    root = tmp_path / "src"
    member = root / "review"
    member.mkdir(parents=True)
    (member / "SKILL.md").write_text(
        "---\nname: review\ndescription: valid\n---\n", encoding="utf-8"
    )
    (member / "SKILL.md").unlink()
    (member / "SKILL.md").symlink_to("SKILL.md")
    with pytest.raises(source_errors.SourceError) as excinfo:
        selection.read_skill_md_name(member, "'review'", resolved_root=root.resolve())
    assert excinfo.value.code == source_errors.CODE_MEMBER_INVALID


def test_nul_source_root_yields_structured_refusal(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An embedded-NUL path converts to SourceError, never a leaked ValueError."""

    monkeypatch.setenv("CSK_CONFIG", str(tmp_path / "home" / "config.json"))
    with pytest.raises(source_errors.SourceError) as excinfo:
        resolve_selector_directory(Path(str(tmp_path) + "\x00"), ".")
    assert excinfo.value.code == source_errors.CODE_SELECTION_INVALID


@pytest.mark.parametrize("entry", ["collection", "individual"])
def test_cyclic_selector_directory_yields_structured_refusal(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, entry: str
) -> None:
    """A self-referential selector directory refuses structured on BOTH entries.

    Pins the `candidate.resolve()` conversion in `resolve_selector_directory`:
    the symlink loop raises `RuntimeError` on resolution, which must surface
    as `source_selection_invalid` instead of leaking.
    """

    monkeypatch.setenv("CSK_CONFIG", str(tmp_path / "home" / "config.json"))
    root = tmp_path / "src"
    root.mkdir(parents=True)
    (root / "loop").symlink_to("loop")
    with pytest.raises(source_errors.SourceError) as excinfo:
        if entry == "collection":
            expand_collection(root, _collection("loop", ("*",), ()))
        else:
            resolve_individual(
                root,
                IndividualSelector(
                    name="loop", from_alias="local", directory="loop"
                ),
            )
    assert excinfo.value.code == source_errors.CODE_SELECTION_INVALID


@pytest.mark.parametrize("entry", ["collection", "individual"])
def test_home_realpath_failure_is_not_absence(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, entry: str
) -> None:
    """Committed from the rev18 review: a failed home probe is not absence.

    A valid ``source/review`` package with ``CSK_CONFIG`` under a home-alias
    symlink to the source refuses ``source_output_overlap`` (the package is
    inside the csk home). Injecting ``PermissionError`` only at the descriptor
    open of that home alias must STILL refuse structured through BOTH entry
    points instead of treating the failed boundary probe as absence.
    """

    root = tmp_path / "source"
    member = root / "review"
    member.mkdir(parents=True)
    (member / "SKILL.md").write_text(
        "---\nname: review\ndescription: Review\n---\n", encoding="utf-8"
    )
    home_alias = tmp_path / "home-alias"
    home_alias.symlink_to(root, target_is_directory=True)
    monkeypatch.setenv("CSK_CONFIG", str(home_alias / "config.json"))

    def invoke():  # type: ignore[no-untyped-def]
        if entry == "collection":
            return expand_collection(
                root,
                CollectionSelector(
                    from_alias="local",
                    directory=".",
                    include=("review",),
                    exclude=(),
                ),
            )
        return resolve_individual(
            root,
            IndividualSelector(
                name="review", from_alias="local", directory="review"
            ),
        )

    with pytest.raises(source_errors.SourceError) as baseline:
        invoke()
    assert baseline.value.code == source_errors.CODE_OUTPUT_OVERLAP
    original_open = _selection_fs.os.open
    hits: list[str] = []

    def denied(path, *args, **kwargs):  # type: ignore[no-untyped-def]
        if os.fspath(path) == str(home_alias):
            hits.append(str(path))
            raise PermissionError("injected home alias lookup denial")
        return original_open(path, *args, **kwargs)

    monkeypatch.setattr(_selection_fs.os, "open", denied)
    try:
        with pytest.raises(source_errors.SourceError) as excinfo:
            invoke()
        assert excinfo.value.code == source_errors.CODE_OUTPUT_OVERLAP
        assert "boundary undetermined" in excinfo.value.detail
        assert isinstance(excinfo.value.__cause__, PermissionError)
    finally:
        assert hits, "injected home-alias lookup was not reached"


@pytest.mark.parametrize("entry", ["collection", "individual"])
def test_member_intermediate_realpath_failure_is_not_absence(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, entry: str
) -> None:
    """A failed intermediate member probe refuses ``source_member_invalid``.

    Same shape as the home-alias fault, but for a member subtree: the
    descriptor-backed pre-read walk's open of ``references`` raises
    ``PermissionError``. BOTH entry points must still refuse structured
    instead of accepting the package after a failed filesystem operation.
    """

    monkeypatch.setenv("CSK_CONFIG", str(tmp_path / "home" / "config.json"))
    root = tmp_path / "source"
    member = root / "review"
    refs = member / "references"
    refs.mkdir(parents=True)
    (member / "SKILL.md").write_text(
        "---\nname: review\ndescription: Review skill\n---\n", encoding="utf-8"
    )
    (refs / "notes.md").write_text("# Notes\n", encoding="utf-8")

    def invoke():  # type: ignore[no-untyped-def]
        if entry == "collection":
            return expand_collection(root, _collection(".", ("review",), ()))
        return resolve_individual(
            root,
            IndividualSelector(
                name="review", from_alias="local", directory="review"
            ),
        )

    invoke()  # Positive control: the same readable package is valid.
    original_open_child = _selection_fs.SelectionSession._open_regular_child
    hits: list[str] = []

    def counting(self, parent, name, *, code, context):  # type: ignore[no-untyped-def]
        if name == "references" and parent.display == member:
            hits.append(name)
            raise PermissionError("injected intermediate lookup denial")
        return original_open_child(self, parent, name, code=code, context=context)

    monkeypatch.setattr(_selection_fs.SelectionSession, "_open_regular_child", counting)
    try:
        with pytest.raises(source_errors.SourceError) as excinfo:
            invoke()
        assert excinfo.value.code == source_errors.CODE_MEMBER_INVALID
        assert isinstance(excinfo.value.__cause__, PermissionError)
    finally:
        assert hits, "injected intermediate lookup was not reached"


@pytest.mark.parametrize("entry", ["collection", "individual"])
def test_selector_two_link_cycle_yields_structured_refusal(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, entry: str
) -> None:
    """A two-link selector cycle refuses structured via the visited set.

    Pins the descriptor traversal's visited-link identity detection plus the
    expansion cap: ``loop-a`` <-> ``loop-b`` must
    surface as ``source_selection_invalid`` on BOTH entries, never leak a
    raw ``RuntimeError``/``OSError`` and never resolve to a partial path.
    """

    monkeypatch.setenv("CSK_CONFIG", str(tmp_path / "home" / "config.json"))
    root = tmp_path / "src"
    root.mkdir(parents=True)
    (root / "loop-a").symlink_to("loop-b")
    (root / "loop-b").symlink_to("loop-a")
    with pytest.raises(source_errors.SourceError) as excinfo:
        if entry == "collection":
            expand_collection(root, _collection("loop-a", ("*",), ()))
        else:
            resolve_individual(
                root,
                IndividualSelector(
                    name="loop", from_alias="local", directory="loop-a"
                ),
            )
    assert excinfo.value.code == source_errors.CODE_SELECTION_INVALID


@pytest.mark.parametrize("entry", ["collection", "individual"])
def test_home_eloop_failure_is_not_absence(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, entry: str
) -> None:
    """An ``ELOOP`` home probe failure refuses, never reports outside.

    Same alias shape as ``test_home_realpath_failure_is_not_absence`` but
    with the ``ELOOP`` errno instead of ``PermissionError``: every errno
    except ``ENOENT`` propagates fail-closed through BOTH entry points.
    """

    root = tmp_path / "source"
    member = root / "review"
    member.mkdir(parents=True)
    (member / "SKILL.md").write_text(
        "---\nname: review\ndescription: Review\n---\n", encoding="utf-8"
    )
    home_alias = tmp_path / "home-alias"
    home_alias.symlink_to(root, target_is_directory=True)
    monkeypatch.setenv("CSK_CONFIG", str(home_alias / "config.json"))

    def invoke():  # type: ignore[no-untyped-def]
        if entry == "collection":
            return expand_collection(
                root,
                CollectionSelector(
                    from_alias="local",
                    directory=".",
                    include=("review",),
                    exclude=(),
                ),
            )
        return resolve_individual(
            root,
            IndividualSelector(
                name="review", from_alias="local", directory="review"
            ),
        )

    with pytest.raises(source_errors.SourceError) as baseline:
        invoke()
    assert baseline.value.code == source_errors.CODE_OUTPUT_OVERLAP
    original_open = _selection_fs.os.open
    hits: list[str] = []

    def eloop(path, *args, **kwargs):  # type: ignore[no-untyped-def]
        if os.fspath(path) == str(home_alias):
            hits.append(str(path))
            raise OSError(errno.ELOOP, "injected too many levels")
        return original_open(path, *args, **kwargs)

    monkeypatch.setattr(_selection_fs.os, "open", eloop)
    try:
        with pytest.raises(source_errors.SourceError) as excinfo:
            invoke()
        assert excinfo.value.code == source_errors.CODE_OUTPUT_OVERLAP
        assert "boundary undetermined" in excinfo.value.detail
        assert isinstance(excinfo.value.__cause__, OSError)
    finally:
        assert hits, "injected ELOOP lookup was not reached"


@pytest.mark.parametrize("entry", ["collection", "individual"])
def test_fresh_home_does_not_refuse(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, entry: str
) -> None:
    """A nonexistent csk home resolves via the literal tail and passes.

    Positive control for the ``ENOENT``-tail rule: ``CSK_CONFIG`` under a
    never-created home directory must not refuse ``source_output_overlap``;
    BOTH entry points accept the valid package.
    """

    home = tmp_path / "home"
    assert not home.exists()
    monkeypatch.setenv("CSK_CONFIG", str(home / "config.json"))
    root = tmp_path / "source"
    member = root / "review"
    _write_skill(member, "review", "Review skill")
    if entry == "collection":
        members = expand_collection(root, _collection(".", ("review",), ()))
        assert [member.name for member in members] == ["review"]
    else:
        selected = resolve_individual(
            root,
            IndividualSelector(
                name="review", from_alias="local", directory="review"
            ),
        )
        assert selected.name == "review"


@pytest.mark.parametrize("entry", ["collection", "individual"])
@pytest.mark.parametrize("alias", ["SOURCE", "source"])
def test_home_case_equivalent_boundary_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, entry: str, alias: str
) -> None:
    """Committed from the rev19 review: a case-variant home spelling refuses.

    ``CSK_CONFIG`` under ``SOURCE/`` (the same directory as ``source/`` on a
    case-insensitive filesystem) must refuse ``source_output_overlap``
    through BOTH entry points exactly like the identical spelling: home
    containment compares filesystem identity, never string spelling. The
    ``SOURCE`` params skip with a named bound where the host does not alias
    the two spellings.
    """

    root = tmp_path / "source"
    member = root / "review"
    member.mkdir(parents=True)
    (member / "SKILL.md").write_text(
        "---\nname: review\ndescription: Review\n---\n", encoding="utf-8"
    )
    home = tmp_path / alias
    if not _probes_same_file(home, root):
        pytest.skip(
            "case-insensitive filesystem unavailable: "
            "SOURCE/source home-alias bound (identity needs actual FS equivalence)"
        )
    assert home.samefile(root)
    monkeypatch.setenv("CSK_CONFIG", str(home / "config.json"))
    with pytest.raises(source_errors.SourceError) as excinfo:
        if entry == "collection":
            expand_collection(
                root,
                CollectionSelector(
                    from_alias="local",
                    directory=".",
                    include=("review",),
                    exclude=(),
                ),
            )
        else:
            resolve_individual(
                root,
                IndividualSelector(
                    name="review", from_alias="local", directory="review"
                ),
            )
    assert excinfo.value.code == source_errors.CODE_OUTPUT_OVERLAP


@pytest.mark.parametrize("entry", ["collection", "individual"])
@pytest.mark.parametrize("variant", ["nfc-exact", "nfd-variant"])
def test_home_unicode_equivalent_boundary_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, entry: str, variant: str
) -> None:
    """A Unicode-equivalent home spelling refuses like the identical one.

    Same shape as ``test_home_case_equivalent_boundary_refused`` with an
    NFC/NFD pair: ``CSK_CONFIG`` under the NFD spelling of the NFC home
    directory refuses ``source_output_overlap`` through BOTH entry points
    where the filesystem normalises the two spellings to one object. The
    ``nfd-variant`` params skip with a named bound elsewhere.
    """

    assert _BOUNDARY_UNICODE_NFC != _BOUNDARY_UNICODE_NFD
    root = tmp_path / _BOUNDARY_UNICODE_NFC
    member = root / "review"
    _write_skill(member, "review", "Review skill")
    home = tmp_path / (
        _BOUNDARY_UNICODE_NFC if variant == "nfc-exact" else _BOUNDARY_UNICODE_NFD
    )
    if not _probes_same_file(home, root):
        pytest.skip(
            "unicode-normalising filesystem unavailable: "
            "NFC/NFD home-alias bound (identity needs actual FS equivalence)"
        )
    assert home.samefile(root)
    monkeypatch.setenv("CSK_CONFIG", str(home / "config.json"))
    with pytest.raises(source_errors.SourceError) as excinfo:
        if entry == "collection":
            expand_collection(root, _collection(".", ("review",), ()))
        else:
            resolve_individual(
                root,
                IndividualSelector(
                    name="review", from_alias="local", directory="review"
                ),
            )
    assert excinfo.value.code == source_errors.CODE_OUTPUT_OVERLAP


def test_home_case_variant_wildcard_pruned(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Wildcard discovery prunes a case-variant csk home, keeping the survivor.

    The home is ``source/sub`` reached via ``SOURCE/SUB`` config spelling:
    ``"*"`` prunes ``sub`` (itself inside the home, no SKILL.md needed
    since pruning precedes validation) and returns the outside-home
    survivor, proving the wildcard path uses the same identity predicate
    as the explicit paths. Skips with a named bound on case-sensitive
    hosts.
    """

    root = tmp_path / "source"
    (root / "sub").mkdir(parents=True)
    _write_skill(root / "ok", "ok", "Survivor skill")
    probe = tmp_path / "SOURCE" / "SUB"
    if not _probes_same_file(probe, root / "sub"):
        pytest.skip(
            "case-insensitive filesystem unavailable: "
            "SOURCE/SUB home-alias bound (identity needs actual FS equivalence)"
        )
    monkeypatch.setenv("CSK_CONFIG", str(probe / "config.json"))
    members = expand_collection(root, _collection(".", ("*",), ()))
    assert [member.name for member in members] == ["ok"]


@pytest.mark.parametrize("entry", ["individual", "literal", "wildcard"])
def test_existing_home_outside_package_accepted(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, entry: str
) -> None:
    """Authored controls: an existing home refuses nothing outside itself.

    The csk home exists (with a config file) in a separate tree; a valid
    package under an unrelated source root is accepted through every entry
    point, pinning that the identity predicate refuses only true
    containment, never mere coexistence with a home.
    """

    home = tmp_path / "home"
    home.mkdir(parents=True)
    (home / "config.json").write_text("{}", encoding="utf-8")
    monkeypatch.setenv("CSK_CONFIG", str(home / "config.json"))
    root = tmp_path / "source"
    _write_skill(root / "review", "review", "Review skill")
    if entry == "individual":
        selected = resolve_individual(
            root,
            IndividualSelector(
                name="review", from_alias="local", directory="review"
            ),
        )
        assert selected.name == "review"
    elif entry == "literal":
        members = expand_collection(root, _collection(".", ("review",), ()))
        assert [member.name for member in members] == ["review"]
    else:
        members = expand_collection(root, _collection(".", ("*",), ()))
        assert [member.name for member in members] == ["review"]


@pytest.mark.parametrize("entry", ["collection", "individual"])
def test_source_root_inside_csk_home_boundary_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, entry: str
) -> None:
    """Home containment also covers a source root nested below the home.

    The source-root descriptor is opened directly, so this regression proves
    the boundary check uses its physical ancestor identities rather than only
    the selector's opened child chain or path spelling.
    """

    home = tmp_path / "home"
    root = home / "source"
    _write_skill(root / "review", "review", "Review skill")
    monkeypatch.setenv("CSK_CONFIG", str(home / "config.json"))
    with pytest.raises(source_errors.SourceError) as excinfo:
        if entry == "collection":
            expand_collection(root, _collection(".", ("review",), ()))
        else:
            resolve_individual(
                root,
                IndividualSelector(
                    name="review", from_alias="local", directory="review"
                ),
            )
    assert excinfo.value.code == source_errors.CODE_OUTPUT_OVERLAP


@pytest.mark.parametrize("entry", ["collection", "individual"])
@pytest.mark.parametrize("managed_name", [".agents", ".git"])
@pytest.mark.parametrize("spelling", ["exact", "case-variant"])
def test_source_root_inside_managed_ancestor_refused(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    entry: str,
    managed_name: str,
    spelling: str,
) -> None:
    """A source root below a managed output is not selectable by spelling."""

    monkeypatch.setenv("CSK_CONFIG", str(tmp_path / "home" / "config.json"))
    root_name = managed_name if spelling == "exact" else managed_name.upper()
    managed_root = tmp_path / managed_name
    root = tmp_path / root_name / "workspace"
    if spelling == "case-variant" and not _probes_same_file(
        managed_root, tmp_path / root_name
    ):
        pytest.skip(
            "case-insensitive filesystem unavailable: "
            f"{root_name}/{managed_name} managed-ancestor bound"
        )
    _write_skill(root / "review", "review", "Review skill")
    with pytest.raises(source_errors.SourceError) as excinfo:
        if entry == "collection":
            expand_collection(root, _collection(".", ("review",), ()))
        else:
            resolve_individual(
                root,
                IndividualSelector(
                    name="review", from_alias="local", directory="review"
                ),
            )
    assert excinfo.value.code == source_errors.CODE_OUTPUT_OVERLAP


@pytest.mark.parametrize(
    "root_kind",
    ["source-root", "csk-home", "adapter-agents", "git", "staging", "snapshot"],
)
@pytest.mark.parametrize(
    "spelling",
    ["exact", "case-variant", "unicode-variant", "symlink-alias", "hard-path"],
)
@pytest.mark.parametrize("entry", ["individual", "literal", "wildcard"])
def test_boundary_decision_table(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    root_kind: str,
    spelling: str,
    entry: str,
) -> None:
    """Boundary decision table: root kind x spelling x entry point.

    Every cell drives a production entry point and asserts its verdict:

    * ``source-root``: an escape reached with the root spelled the given
      way refuses ``source_selection_invalid`` through every entry point
      (wildcard carries a valid survivor to prove the whole operation
      fails, never partially publishes); each cell first accepts the
      inside survivor through the same spelling, pinning that containment
      holds (not merely refuses) through every spelling.
    * managed kinds (``csk-home``, ``adapter-agents``, ``git``,
      ``staging``, ``snapshot``): a package inside the managed location
      refuses ``source_output_overlap`` for explicit selections
      (``individual``, ``literal``). Wildcard prunes: ``csk-home``,
      ``adapter-agents`` and ``git`` assert the outside survivor list,
      while ``staging`` and ``snapshot`` (whose roots coincide with the
      home, so no survivor can exist beside the managed tree) assert the
      empty-set ``source_member_invalid`` over a VALID package -- without
      pruning the valid package would be accepted, so the refusal proves
      the prune.
    * ``staging``/``snapshot`` live under the csk home (representative
      ``staging/`` and ``source-v1/`` subdirs: csk keeps no separate
      staging/snapshot path constant outside the home, so any in-home
      path refuses through the one home-containment rule).

    ``case-variant``/``unicode-variant`` cells skip with a named platform
    bound where the host filesystem does not alias the spellings;
        ``hard-path`` spells the location with zero links remaining after the
        fixture's link is resolved, and asserts the linked form it was
        resolved from differs textually, so the cell is meaningful even where
        the temporary tree is already link-free.
    """

    base = tmp_path / "w"
    if root_kind == "source-root":
        if spelling == "unicode-variant":
            src = base / _BOUNDARY_UNICODE_NFC
            root_arg: Path = base / _BOUNDARY_UNICODE_NFD
        else:
            src = base / "source"
            root_arg = src
        outside = base / "outside" / "review"
        _write_skill(outside, "review", "Outside skill")
        src.mkdir(parents=True, exist_ok=True)
        (src / "evil").symlink_to(base / "outside", target_is_directory=True)
        _write_skill(src / "ok", "ok", "Survivor skill")
        monkeypatch.setenv(
            "CSK_CONFIG", str(tmp_path / "isolated-home" / "config.json")
        )
        if spelling == "case-variant":
            root_arg = base / "SOURCE"
            if not _probes_same_file(root_arg, src):
                pytest.skip(
                    "case-insensitive filesystem unavailable: "
                    "SOURCE/source root-alias bound"
                )
        elif spelling == "unicode-variant":
            if not _probes_same_file(root_arg, src):
                pytest.skip(
                    "unicode-normalising filesystem unavailable: "
                    "NFC/NFD root-alias bound"
                )
        elif spelling == "symlink-alias":
            linked = base / "rlink"
            linked.symlink_to(src, target_is_directory=True)
            root_arg = linked
        elif spelling == "hard-path":
            linked = base / "rlink"
            linked.symlink_to(src, target_is_directory=True)
            root_arg = linked.resolve()
            assert root_arg != linked
        control = resolve_individual(
            root_arg,
            IndividualSelector(name="ok", from_alias="local", directory="ok"),
        )
        assert control.name == "ok"
        with pytest.raises(source_errors.SourceError) as excinfo:
            if entry == "individual":
                resolve_individual(
                    root_arg,
                    IndividualSelector(
                        name="review", from_alias="local", directory="evil"
                    ),
                )
            elif entry == "literal":
                expand_collection(root_arg, _collection(".", ("evil",), ()))
            else:
                expand_collection(root_arg, _collection(".", ("*",), ()))
        assert excinfo.value.code == source_errors.CODE_SELECTION_INVALID
        return

    if root_kind == "csk-home":
        src = base / "source"
        home_name = (
            "s\u00fcb" if spelling == "unicode-variant" else "sub"
        )
        home_dir = src / home_name
        home_dir.mkdir(parents=True)
        _write_skill(src / "ok", "ok", "Survivor skill")
        config_spelling = home_dir
        if spelling == "case-variant":
            config_spelling = src / "SUB"
            if not _probes_same_file(config_spelling, home_dir):
                pytest.skip(
                    "case-insensitive filesystem unavailable: "
                    "SUB/sub home-alias bound"
                )
        elif spelling == "unicode-variant":
            config_spelling = src / unicodedata.normalize("NFD", home_name)
            if not _probes_same_file(config_spelling, home_dir):
                pytest.skip(
                    "unicode-normalising filesystem unavailable: "
                    "NFC/NFD home-alias bound"
                )
        elif spelling == "symlink-alias":
            linked_home = base / "hlink"
            linked_home.symlink_to(home_dir, target_is_directory=True)
            config_spelling = linked_home
        elif spelling == "hard-path":
            linked_home = base / "hlink"
            linked_home.symlink_to(home_dir, target_is_directory=True)
            config_spelling = linked_home.resolve()
            assert config_spelling != linked_home
        monkeypatch.setenv("CSK_CONFIG", str(config_spelling / "config.json"))
        if entry == "wildcard":
            members = expand_collection(src, _collection(".", ("*",), ()))
            assert [member.name for member in members] == ["ok"]
            return
        with pytest.raises(source_errors.SourceError) as excinfo:
            if entry == "individual":
                resolve_individual(
                    src,
                    IndividualSelector(
                        name="review", from_alias="local", directory=home_name
                    ),
                )
            else:
                expand_collection(src, _collection(".", (home_name,), ()))
        assert excinfo.value.code == source_errors.CODE_OUTPUT_OVERLAP
        return

    if root_kind in ("adapter-agents", "git"):
        managed_exact = ".agents" if root_kind == "adapter-agents" else ".git"
        src = base / "source"
        if spelling == "unicode-variant":
            src = base / _BOUNDARY_UNICODE_NFC
        member = managed_exact
        if spelling == "case-variant":
            # On-disk uppercase: discovery yields the alias spelling itself.
            member = managed_exact.upper()
        nested = src / member / "x"
        _write_skill(nested, "inner", "Managed skill")
        _write_skill(src / "ok", "ok", "Survivor skill")
        monkeypatch.setenv(
            "CSK_CONFIG", str(tmp_path / "isolated-home" / "config.json")
        )
        root_arg = src
        if spelling == "case-variant":
            if not _probes_same_file(src / managed_exact, src / member):
                pytest.skip(
                    "case-insensitive filesystem unavailable: "
                    f"{member}/{managed_exact} managed-alias bound"
                )
        elif spelling == "unicode-variant":
            root_arg = base / _BOUNDARY_UNICODE_NFD
            if not _probes_same_file(root_arg, src):
                pytest.skip(
                    "unicode-normalising filesystem unavailable: "
                    "NFC/NFD root-alias bound"
                )
        elif spelling == "symlink-alias":
            linked_member = src / "mlink"
            linked_member.symlink_to(nested, target_is_directory=True)
            member = "mlink"
        elif spelling == "hard-path":
            linked_root = base / "rlink"
            linked_root.symlink_to(src, target_is_directory=True)
            root_arg = linked_root.resolve()
            assert root_arg != linked_root
        if entry == "wildcard":
            members = expand_collection(root_arg, _collection(".", ("*",), ()))
            assert [member.name for member in members] == ["ok"]
            return
        with pytest.raises(source_errors.SourceError) as excinfo:
            if entry == "individual":
                resolve_individual(
                    root_arg,
                    IndividualSelector(
                        name="review", from_alias="local", directory=member
                    ),
                )
            else:
                expand_collection(root_arg, _collection(".", (member,), ()))
        assert excinfo.value.code == source_errors.CODE_OUTPUT_OVERLAP
        return

    # staging / snapshot: the package sits under the csk home itself, so the
    # selectable root and the home share one path (spelled the given way).
    assert root_kind in ("staging", "snapshot")
    subdir = "staging" if root_kind == "staging" else "source-v1"
    home = base / "home"
    if spelling == "unicode-variant":
        home = base / "hom\u00e9"
    package = home / subdir / "pkg"
    _write_skill(package, "review", "In-home skill")
    home_arg = home
    if spelling == "case-variant":
        home_arg = base / "HOME"
        if not _probes_same_file(home_arg, home):
            pytest.skip(
                "case-insensitive filesystem unavailable: "
                "HOME/home alias bound"
            )
    elif spelling == "unicode-variant":
        home_arg = base / unicodedata.normalize("NFD", home.name)
        if not _probes_same_file(home_arg, home):
            pytest.skip(
                "unicode-normalising filesystem unavailable: "
                "NFC/NFD home-alias bound"
            )
    elif spelling == "symlink-alias":
        linked_home = base / "alink"
        linked_home.symlink_to(home, target_is_directory=True)
        home_arg = linked_home
    elif spelling == "hard-path":
        linked_home = base / "alink"
        linked_home.symlink_to(home, target_is_directory=True)
        home_arg = linked_home.resolve()
        assert home_arg != linked_home
    monkeypatch.setenv("CSK_CONFIG", str(home_arg / "config.json"))
    selector_dir = f"{subdir}/pkg"
    if entry == "wildcard":
        with pytest.raises(source_errors.SourceError) as excinfo:
            expand_collection(home_arg, _collection(subdir, ("*",), ()))
        assert excinfo.value.code == source_errors.CODE_MEMBER_INVALID
        assert "expands to an empty skill set" in excinfo.value.detail
        return
    with pytest.raises(source_errors.SourceError) as excinfo:
        if entry == "individual":
            resolve_individual(
                home_arg,
                IndividualSelector(
                    name="review", from_alias="local", directory=selector_dir
                ),
            )
        else:
            expand_collection(home_arg, _collection(subdir, ("pkg",), ()))
    assert excinfo.value.code == source_errors.CODE_OUTPUT_OVERLAP
