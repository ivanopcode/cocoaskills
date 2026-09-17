"""Descriptor-confined filesystem primitives used by source selection.

Selection has two deliberately separate lifecycles.  Phase A is an
unconfined identity preflight: it may inspect the configured csk home,
physical ancestors, and absolute link targets, but it publishes no session
capability.  Phase B starts with a fresh source-root open.  From that event
on, every descendant is opened by a bare name relative to an already opened
directory descriptor and all boundary decisions use the frozen Phase-A
identities and link plans.

This split matters more than a comment calling an operation "preflight".  The
fresh root open is the observable transition, and the descriptor returned by
that open is the only source capability retained by the session.
"""

from __future__ import annotations

import errno
import fnmatch
import io
import os
import stat
from contextlib import contextmanager
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from types import MappingProxyType
from typing import Callable, ContextManager, Final, Iterator, cast

from .errors import (
    CODE_MEMBER_INVALID,
    CODE_OUTPUT_OVERLAP,
    CODE_SELECTION_INVALID,
    CODE_SNAPSHOT_CHANGED,
    SourceError,
)


_FS_ERRORS: Final[tuple[type[Exception], ...]] = (
    OSError,
    RuntimeError,
    ValueError,
    UnicodeError,
)


@dataclass(frozen=True)
class DescriptorEvent:
    """One observed descriptor operation for boundary-property tests."""

    operation: str
    path: str | int
    parent_fd: int | None
    parent_identity: tuple[int, int] | None
    result_fd: int | None
    result_identity: tuple[int, int] | None


TraceSink = Callable[[DescriptorEvent], None]
_TRACE_SINK: TraceSink | None = None


@contextmanager
def trace_filesystem(sink: TraceSink) -> Iterator[None]:
    """Record the actual parent descriptor of every seam operation.

    This is intentionally a test seam.  It is independent of Python's audit
    event tuple, which does not expose the ``dir_fd`` supplied to ``os.open``.
    The production code still uses the same wrappers when no sink is active.
    """

    global _TRACE_SINK
    previous = _TRACE_SINK
    _TRACE_SINK = sink
    try:
        yield
    finally:
        _TRACE_SINK = previous


def _identity_from_stat(value: os.stat_result) -> tuple[int, int]:
    return (value.st_dev, value.st_ino)


def _safe_identity(fd: int | None) -> tuple[int, int] | None:
    if fd is None:
        return None
    try:
        return _identity_from_stat(os.fstat(fd))
    except _FS_ERRORS:
        return None


def _emit(
    operation: str,
    path: str | int,
    *,
    parent_fd: int | None,
    result_fd: int | None = None,
) -> None:
    sink = _TRACE_SINK
    if sink is None:
        return
    sink(
        DescriptorEvent(
            operation=operation,
            path=path,
            parent_fd=parent_fd,
            parent_identity=_safe_identity(parent_fd),
            result_fd=result_fd,
            result_identity=_safe_identity(result_fd),
        )
    )


def _open_descriptor(
    path: str | Path,
    flags: int,
    *,
    dir_fd: int | None = None,
) -> int:
    """Open one object and record its actual parent descriptor."""

    native = os.fspath(path)
    try:
        if dir_fd is None:
            descriptor = os.open(native, flags)
        else:
            descriptor = os.open(native, flags, dir_fd=dir_fd)
    except _FS_ERRORS:
        _emit("open", native, parent_fd=dir_fd)
        raise
    _emit("open", native, parent_fd=dir_fd, result_fd=descriptor)
    return descriptor


def _read_descriptor(
    fd: int,
    size: int,
    *,
    parent_fd: int | None,
    name: str,
) -> bytes:
    """Read bytes through the one phase-B content seam.

    The parent descriptor and bare name are retained in the trace event even
    though the actual read uses the already opened file descriptor. This is
    the provenance record the boundary property uses; a read primitive called
    outside this seam cannot manufacture it.
    """

    _emit("read", name, parent_fd=parent_fd, result_fd=fd)
    return os.read(fd, size)


def read_regular_path(path: Path, *, code: str, label: str, what: str) -> bytes:
    """Read a directly supplied regular file through the owned filesystem seam.

    Normal source selection never uses this fallback: Phase-B package reads
    use :meth:`SelectionSession._read_regular_file` with a parent descriptor.
    Keeping this legacy direct-entry helper in this module still makes every
    observable open/read primitive visible to the same test seam and keeps
    callers from acquiring a second filesystem capability in ``selection``.
    """

    try:
        entry = os.lstat(path)
    except FileNotFoundError as exc:
        raise SourceError(code, f"Skill member {label} has no {what}") from exc
    except _FS_ERRORS as exc:
        raise SourceError(
            code,
            f"Skill member {label} file {what!r} cannot be inspected: {exc}",
        ) from exc
    if not stat.S_ISREG(entry.st_mode) or stat.S_ISLNK(entry.st_mode):
        raise SourceError(
            code,
            f"Skill member {label} file {what!r} is not a regular file",
        )
    try:
        fd = _open_descriptor(path, _file_flags(nofollow=True))
    except _FS_ERRORS as exc:
        raise SourceError(
            code,
            f"Skill member {label} file {what!r} cannot be read: {exc}",
        ) from exc
    try:
        value = _fstat(
            fd,
            code=code,
            context=f"Skill member {label}",
            path=what,
        )
        if (
            not stat.S_ISREG(value.st_mode)
            or getattr(value, "st_nlink", 1) != 1
            or _identity_from_stat(value) != _identity_from_stat(entry)
        ):
            raise SourceError(
                code,
                f"Skill member {label} file {what!r} changed after entry inspection",
            )
        chunks: list[bytes] = []
        while True:
            chunk = _read_descriptor(fd, 1024 * 1024, parent_fd=None, name=what)
            if not chunk:
                break
            chunks.append(chunk)
        return b"".join(chunks)
    except SourceError:
        raise
    except _FS_ERRORS as exc:
        raise SourceError(
            code,
            f"Skill member {label} file {what!r} cannot be read: {exc}",
        ) from exc
    finally:
        _close_quietly(fd)


def read_optional_regular_path(
    path: Path,
    *,
    code: str,
    label: str,
    what: str,
) -> bytes | None:
    """Read an optional regular file, distinguishing absence from failures."""

    try:
        return read_regular_path(path, code=code, label=label, what=what)
    except SourceError as exc:
        if isinstance(exc.__cause__, FileNotFoundError):
            return None
        raise


def _scandir(
    path: int | str | Path,
    *,
    fallback_path: Path | None = None,
) -> ContextManager[Iterator[os.DirEntry[str]]]:
    """Open one directory iterator through the boundary trace seam."""

    parent_fd = path if isinstance(path, int) else None
    _emit(
        "scandir",
        os.fspath(path) if not isinstance(path, int) else path,
        parent_fd=parent_fd,
    )
    try:
        return os.scandir(path)
    except (TypeError, NotImplementedError):
        if fallback_path is None:
            raise
        return os.scandir(fallback_path)


def _directory_flags(*, nofollow: bool = True) -> int:
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
    if nofollow:
        flags |= getattr(os, "O_NOFOLLOW", 0)
    return flags


def _file_flags(*, nofollow: bool = True) -> int:
    flags = os.O_RDONLY
    if nofollow:
        flags |= getattr(os, "O_NOFOLLOW", 0)
    return flags


@dataclass(frozen=True)
class AliasEntry:
    """A symlink component traversed while resolving one directory."""

    parent: "Directory"
    name: str


@dataclass(frozen=True)
class Directory:
    """An opened directory and its physical, descriptor-backed ancestry."""

    fd: int
    identity: tuple[int, int]
    name: str
    display: Path
    parent: "Directory | None"
    aliases: tuple[AliasEntry, ...] = ()

    def ancestry(self) -> tuple["Directory", ...]:
        chain: list[Directory] = []
        current: Directory | None = self
        while current is not None:
            chain.append(current)
            current = current.parent
        chain.reverse()
        return tuple(chain)


@dataclass(frozen=True)
class MemberSnapshot:
    """Bytes and directory entries read through member-relative descriptors."""

    root: Path
    files: dict[tuple[str, ...], bytes]
    directories: frozenset[tuple[str, ...]]


_SNAPSHOT_PATH_BASE = type(Path())


class _SnapshotPath(_SNAPSHOT_PATH_BASE):  # type: ignore[misc, valid-type]
    """Read-only Path facade backed by a descriptor-captured byte snapshot."""

    __slots__ = ("_snapshot",)

    def __new__(
        cls: type["_SnapshotPath"],
        *parts: str,
        snapshot: MemberSnapshot | None = None,
    ) -> "_SnapshotPath":
        value = cast("_SnapshotPath", super().__new__(cls, *parts))
        object.__setattr__(value, "_snapshot", snapshot)
        return value

    def _copy_with_snapshot(self, value: Path) -> "_SnapshotPath":
        object.__setattr__(value, "_snapshot", self._snapshot)
        return value  # type: ignore[return-value]

    def with_segments(self, *pathsegments: str) -> "_SnapshotPath":
        return self._copy_with_snapshot(super().with_segments(*pathsegments))

    def _from_parsed_parts(
        self, drv: str, root: str, parts: tuple[str, ...]
    ) -> "_SnapshotPath":
        value = super()._from_parsed_parts(drv, root, parts)
        return self._copy_with_snapshot(value)

    @property
    def _member_snapshot(self) -> MemberSnapshot:
        snapshot = getattr(self, "_snapshot", None)
        if not isinstance(snapshot, MemberSnapshot):
            raise OSError("snapshot path has no snapshot")
        return snapshot

    def _relative_key(self) -> tuple[str, ...]:
        snapshot = self._member_snapshot
        current: _SnapshotPath = self
        components: list[str] = []
        while current != snapshot.root:
            parent = current.parent
            if parent == current:
                raise FileNotFoundError(os.fspath(self))
            components.insert(0, current.name)
            current = parent
        return tuple(components)

    def exists(self) -> bool:
        key = self._relative_key()
        snapshot = self._member_snapshot
        return key in snapshot.files or key in snapshot.directories

    def is_file(self) -> bool:
        return self._relative_key() in self._member_snapshot.files

    def is_dir(self) -> bool:
        return self._relative_key() in self._member_snapshot.directories

    def is_symlink(self) -> bool:
        return False

    def lstat(self) -> os.stat_result:
        """Return metadata from the captured tree, never from the path."""

        key = self._relative_key()
        snapshot = self._member_snapshot
        if key in snapshot.directories:
            mode = stat.S_IFDIR | 0o755
            size = 0
        elif key in snapshot.files:
            mode = stat.S_IFREG | 0o644
            size = len(snapshot.files[key])
        else:
            raise FileNotFoundError(os.fspath(self))
        return os.stat_result((mode, 0, 0, 1, 0, 0, size, 0, 0, 0))

    def stat(self, *, follow_symlinks: bool = True) -> os.stat_result:
        """Return snapshot metadata for callers that use ``Path.stat``."""
        _ = follow_symlinks
        return self.lstat()

    def read_bytes(self) -> bytes:
        """Return captured bytes without dispatching to a path read."""

        raw = self._member_snapshot.files.get(self._relative_key())
        if raw is None:
            raise FileNotFoundError(os.fspath(self))
        return raw

    def read_text(
        self,
        encoding: str | None = None,
        errors: str | None = None,
        newline: str | None = None,
    ) -> str:
        """Decode captured bytes through the virtual, in-memory ``open``."""

        _ = newline
        # ``Path.read_text`` dispatches to ``self.open``.  Since this class
        # overrides ``open`` below, the inherited method consumes BytesIO and
        # never reaches the host filesystem.  It remains observable when a
        # downstream fault-injection test replaces Path.read_text.
        return cast(str, super().read_text(encoding=encoding, errors=errors))

    def open(
        self,
        mode: str = "r",
        buffering: int = -1,
        encoding: str | None = None,
        errors: str | None = None,
        newline: str | None = None,
    ) -> io.IOBase:
        _ = buffering
        if any(flag in mode for flag in ("w", "a", "x", "+")):
            raise OSError("selection snapshot is read-only")
        key = self._relative_key()
        raw = self._member_snapshot.files.get(key)
        if raw is None:
            raise FileNotFoundError(os.fspath(self))
        binary = "b" in mode
        stream = io.BytesIO(raw)
        if binary:
            return stream
        return io.TextIOWrapper(
            stream,
            encoding=encoding or "utf-8",
            errors=errors,
            newline=newline,
        )

    def _children(self, pattern: str | None, recursive: bool) -> list["_SnapshotPath"]:
        base = self._relative_key()
        snapshot = self._member_snapshot
        candidates = sorted(
            (*snapshot.files.keys(), *snapshot.directories),
            key=lambda value: value,
        )
        values: list[_SnapshotPath] = []
        for key in candidates:
            if key == base or key[: len(base)] != base:
                continue
            rest = key[len(base) :]
            if not rest:
                continue
            if not recursive and len(rest) != 1:
                continue
            relative = Path(*rest).as_posix()
            if pattern is not None and not fnmatch.fnmatchcase(relative, pattern):
                continue
            values.append(self.joinpath(*rest))
        return values

    def iterdir(self) -> list["_SnapshotPath"]:
        if not self.is_dir():
            raise NotADirectoryError(os.fspath(self))
        return self._children(None, False)

    def glob(self, pattern: str) -> list["_SnapshotPath"]:
        return self._children(pattern, False)

    def rglob(self, pattern: str) -> list["_SnapshotPath"]:
        return self._children(pattern, True)


def _snapshot_path(snapshot: MemberSnapshot) -> Path:
    value = _SnapshotPath(os.fspath(snapshot.root))
    object.__setattr__(value, "_snapshot", snapshot)
    return value


@dataclass(frozen=True)
class TraversalResult:
    directory: Directory
    followed_link: bool = False
    final_component_link: bool = False


@dataclass(frozen=True)
class AbsoluteLinkPlan:
    """Phase-A decision for an absolute directory-link target.

    ``components`` is replayed from the Phase-B root descriptor.  A missing
    component is kept as an errno-bearing refusal so wildcard discovery can
    treat a dangling link as ordinary absence without recomputing the target
    from a path string in Phase B.
    """

    components: tuple[str, ...] | None
    error_code: str | None = None
    detail: str = ""
    error_errno: int | None = None
    # Kept for diagnostics and mutant harnesses only. Phase B never compares
    # these spellings; it replays ``components`` from the root descriptor.
    target_spelling: str | None = None
    root_spelling: str | None = None


@dataclass(frozen=True)
class ComponentBinding:
    """Phase-A identity for one descriptor-relative directory component."""

    identity: tuple[int, int]
    is_symlink: bool
    target: str | None = None


@dataclass(frozen=True)
class ManagedRootBinding:
    """One managed-root entry observed from a reachable source directory.

    ``identity`` is the physical directory identity used by the boundary
    predicate.  ``entry_identity`` and ``target`` bind the directory entry
    itself as well, so a recheck can reject a retargeted alias without
    following a newly introduced link before it has compared the frozen
    entry.  ``identity`` is ``None`` for a location that was absent or was
    not a directory during Phase A; that absence is part of the frozen
    record, not an unknown value.
    """

    base_components: tuple[str, ...]
    name: str
    identity: tuple[int, int] | None
    entry_identity: tuple[int, int] | None
    target: str | None = None


ComponentKey = tuple[tuple[int, int], str]


@dataclass(frozen=True)
class PreflightPath:
    """One declared path that Phase A must resolve completely."""

    components: tuple[str, ...]
    code: str = CODE_SELECTION_INVALID
    missing_code: str | None = None
    not_directory_code: str | None = None
    context: str = "Declared selector preflight"
    collect_boundaries: bool = True


@dataclass(frozen=True)
class PreflightRequest:
    """The selector reach that Phase A must resolve before Phase B."""

    # Tuple values remain accepted for the small internal compatibility
    # surface; production callers use PreflightPath so the public error code,
    # context and boundary-collection scope are frozen with the request.
    paths: tuple[PreflightPath | tuple[str, ...], ...] = ()
    wildcard_bases: tuple[tuple[str, ...], ...] = ()
    wildcard_excludes: tuple[frozenset[str], ...] = ()


@dataclass(frozen=True)
class PreflightState:
    """Frozen identity decisions handed across the Phase-A/Phase-B seam."""

    root_identity: tuple[int, int]
    home_identity: tuple[int, int] | None
    home_contains_root: bool
    root_inside_managed: str | None
    managed_root_identities: Mapping[tuple[int, int], str]
    managed_root_bindings: tuple[ManagedRootBinding, ...]
    absolute_links: Mapping[tuple[int, int], AbsoluteLinkPlan]
    component_bindings: Mapping[ComponentKey, ComponentBinding]


@dataclass
class SelectionSession:
    """Own every descriptor used by one selection operation."""

    root: Directory
    home_directory: Directory | None
    home_path: Path
    home_identity_value: tuple[int, int] | None
    home_contains_root: bool
    root_inside_managed: str | None
    managed_root_identities: Mapping[tuple[int, int], str]
    managed_root_bindings: tuple[ManagedRootBinding, ...]
    absolute_links: Mapping[tuple[int, int], AbsoluteLinkPlan]
    component_bindings: Mapping[ComponentKey, ComponentBinding]
    _fds: set[int] = field(default_factory=set)

    @classmethod
    def open(
        cls,
        source_root: Path,
        home: Path,
        *,
        managed_names: frozenset[str] = frozenset(),
        preflight: PreflightRequest = PreflightRequest(),
    ) -> "SelectionSession":
        """Run Phase A, then start Phase B with a fresh root descriptor."""

        preflight_state = _prepare_preflight(
            source_root,
            home,
            managed_names,
            preflight=preflight,
        )
        confined_fd: int | None = None
        try:
            # This is the only event that starts the confined lifecycle. Do
            # not reuse the Phase-A descriptor: the source root may have been
            # replaced between the two phases, and that must be a refusal.
            confined_fd = _open_root(source_root, phase="b")
            confined_stat = _fstat(
                confined_fd,
                code=CODE_SELECTION_INVALID,
                context=f"Source root {source_root}",
                path=os.fspath(source_root),
            )
            if not stat.S_ISDIR(confined_stat.st_mode):
                raise SourceError(
                    CODE_SELECTION_INVALID,
                    f"Source root {source_root} is not a directory",
                )
            if _identity_from_stat(confined_stat) != preflight_state.root_identity:
                raise SourceError(
                    CODE_SELECTION_INVALID,
                    f"Source root {source_root} changed between preflight and selection",
                )
            try:
                root_display = Path(os.path.abspath(os.fspath(source_root)))
            except _FS_ERRORS as exc:
                raise SourceError(
                    CODE_SELECTION_INVALID,
                    f"Source root {source_root} cannot be displayed: {exc}",
                ) from exc
            root = Directory(
                fd=confined_fd,
                identity=_identity_from_stat(confined_stat),
                name=Path(os.fspath(source_root)).name,
                display=root_display,
                parent=None,
            )
            session = cls(
                root=root,
                # Phase-A boundary descriptors are intentionally not retained
                # in Phase B. Only their frozen identity is needed.
                home_directory=None,
                home_path=Path(os.path.abspath(os.fspath(home))),
                home_identity_value=preflight_state.home_identity,
                home_contains_root=preflight_state.home_contains_root,
                root_inside_managed=preflight_state.root_inside_managed,
                managed_root_identities=preflight_state.managed_root_identities,
                managed_root_bindings=preflight_state.managed_root_bindings,
                absolute_links=preflight_state.absolute_links,
                component_bindings=preflight_state.component_bindings,
            )
            session._fds.add(confined_fd)
            confined_fd = None
            return session
        except SourceError:
            raise
        except _FS_ERRORS as exc:
            raise SourceError(
                CODE_SELECTION_INVALID,
                f"Source root {source_root} cannot be inspected: {exc}",
            ) from exc
        finally:
            if confined_fd is not None:
                _close_quietly(confined_fd)

    def __enter__(self) -> "SelectionSession":
        return self

    def __exit__(self, exc_type: object, exc: object, traceback: object) -> None:
        self.close()

    def close(self) -> None:
        """Close every descriptor owned by this session exactly once."""

        for fd in tuple(self._fds):
            _close_quietly(fd)
        self._fds.clear()

    @property
    def home_identity(self) -> tuple[int, int] | None:
        return self.home_identity_value

    def descend(
        self,
        start: Directory,
        components: list[str],
        *,
        code: str,
        missing_code: str | None,
        context: str,
    ) -> TraversalResult:
        """Descend by names from ``start`` without lexical normalisation."""

        if not components:
            return TraversalResult(start)

        pending: list[tuple[str, bool, bool]] = [
            (part, True, index == len(components) - 1)
            for index, part in enumerate(components)
        ]
        stack: list[Directory] = list(start.ancestry())
        aliases: list[AliasEntry] = list(start.aliases)
        followed_link = False
        final_component_link = False
        visited_states: set[tuple[tuple[int, int], tuple[str, ...]]] = set()
        expansions = 0

        while pending:
            part, from_selector, is_final_selector_component = pending.pop(0)
            if part in ("", "."):
                continue
            if part == "..":
                if len(stack) == 1:
                    raise SourceError(
                        code,
                        f"{context}: parent traversal would escape the source root",
                    )
                stack.pop()
                aliases = list(stack[-1].aliases)
                continue

            parent = stack[-1]
            expected = self.component_bindings.get((parent.identity, part))
            if expected is None:
                raise SourceError(
                    code,
                    f"{context}: component {part!r} was not resolved in Phase A",
                )
            try:
                child_fd = _open_child_directory(
                    parent.fd, part, parent_path=parent.display
                )
            except SourceError:
                raise
            except OSError as exc:
                if exc.errno == errno.ENOENT:
                    detail = f"{context}: component {part!r} does not exist or is not a directory"
                    raise SourceError(missing_code or code, detail) from exc
                if exc.errno not in (errno.ELOOP, errno.EMLINK, errno.ENOTDIR):
                    raise SourceError(code, f"{context}: {part!r}: {exc}") from exc
                try:
                    link_stat = _lstat_at(
                        parent.fd,
                        part,
                        parent_path=parent.display,
                        code=code,
                        context=context,
                    )
                except SourceError:
                    raise
                if not stat.S_ISLNK(link_stat.st_mode):
                    raise SourceError(
                        CODE_MEMBER_INVALID if missing_code is not None else code,
                        f"{context}: component {part!r} exists but is not a directory",
                    ) from exc
                link_identity = _identity_from_stat(link_stat)
                if not expected.is_symlink or expected.identity != link_identity:
                    raise SourceError(
                        code,
                        f"{context}: component {part!r} changed after Phase A",
                    ) from exc
                resolution_state = (
                    link_identity,
                    tuple(pending_part for pending_part, _, _ in pending),
                )
                if resolution_state in visited_states:
                    raise SourceError(code, f"{context}: symlink loop at {part!r}")
                visited_states.add(resolution_state)
                target = _readlink_at(
                    parent.fd,
                    part,
                    parent_path=parent.display,
                    code=code,
                    context=context,
                )
                if expected.target != target:
                    raise SourceError(
                        code,
                        f"{context}: symlink component {part!r} changed after Phase A",
                    ) from exc
                expansions += 1
                if expansions > 64:
                    raise SourceError(
                        code, f"{context}: too many symlink expansions at {part!r}"
                    )
                followed_link = True
                if from_selector and is_final_selector_component:
                    final_component_link = True

                if _is_absolute_target(target):
                    relative_target = self._absolute_target_components(
                        _identity_from_stat(link_stat),
                        context=context,
                        code=code,
                    )
                    stack = list(self.root.ancestry())
                    aliases = []
                else:
                    relative_target = _split_link_target(target)
                for target_part in reversed(relative_target):
                    pending.insert(0, (target_part, False, False))
                continue
            except _FS_ERRORS as exc:
                raise SourceError(code, f"{context}: {part!r}: {exc}") from exc

            try:
                child_stat = _fstat(
                    child_fd,
                    code=code,
                    context=context,
                    path=part,
                )
                self._require_source_device(
                    child_stat,
                    code=code,
                    context=context,
                    path=part,
                )
                if not stat.S_ISDIR(child_stat.st_mode):
                    raise SourceError(code, f"{context}: {part!r} is not a directory")
                child_identity = _identity_from_stat(child_stat)
                if expected.is_symlink or child_identity != expected.identity:
                    raise SourceError(
                        code,
                        f"{context}: component {part!r} changed after Phase A",
                    )
                if any(item.identity == child_identity for item in stack):
                    raise SourceError(code, f"{context}: directory cycle at {part!r}")
                child = Directory(
                    fd=child_fd,
                    identity=child_identity,
                    name=part,
                    display=_display_child(parent.display, part),
                    parent=parent,
                    aliases=tuple(aliases),
                )
            except SourceError:
                _close_quietly(child_fd)
                raise
            self._fds.add(child_fd)
            stack.append(child)
            aliases = list(child.aliases)

        return TraversalResult(
            directory=stack[-1],
            followed_link=followed_link,
            final_component_link=final_component_link,
        )

    def _absolute_target_components(
        self,
        link_identity: tuple[int, int],
        *,
        context: str,
        code: str,
    ) -> list[str]:
        """Replay the Phase-A identity decision for one absolute link.

        No target path is inspected in Phase B. A link not present in the
        frozen plan is treated as a race or an incomplete preflight and is
        refused rather than resolved optimistically.
        """

        plan = self.absolute_links.get(link_identity)
        if plan is None:
            raise SourceError(
                code,
                f"{context}: absolute symlink target was not preflighted",
            )
        if plan.components is not None:
            return list(plan.components)
        detail = plan.detail or f"{context}: absolute symlink target cannot be resolved"
        if plan.error_errno is not None:
            cause = OSError(plan.error_errno, detail)
            raise SourceError(plan.error_code or code, detail) from cause
        raise SourceError(plan.error_code or code, detail)

    def child_directories(self, base: Directory, *, context: str) -> list[str]:
        """List immediate directory entries using ``scandir(base.fd)``."""

        names: list[str] = []
        try:
            with _scandir(base.fd, fallback_path=base.display) as entries:
                for entry in entries:
                    try:
                        entry_stat = entry.stat(follow_symlinks=False)
                    except FileNotFoundError as exc:
                        # A directory entry disappearing after scandir is a
                        # failed inspection, not proof that the candidate is
                        # absent. Dangling wildcard links are handled by the
                        # descriptor descent below, where ENOENT is the
                        # protocol's ordinary non-member case.
                        raise SourceError(
                            CODE_SELECTION_INVALID,
                            f"{context}: child disappeared during inspection",
                        ) from exc
                    except _FS_ERRORS as exc:
                        raise SourceError(
                            CODE_SELECTION_INVALID,
                            f"{context}: child cannot be inspected: {exc}",
                        ) from exc
                    if stat.S_ISDIR(entry_stat.st_mode):
                        names.append(entry.name)
                        continue
                    if not stat.S_ISLNK(entry_stat.st_mode):
                        continue
                    try:
                        result = self.descend(
                            base,
                            [entry.name],
                            code=CODE_SELECTION_INVALID,
                            missing_code=None,
                            context=f"{context}: child {entry.name!r}",
                        )
                    except SourceError as exc:
                        if _is_absence_error(exc):
                            continue
                        if exc.code == CODE_OUTPUT_OVERLAP:
                            continue
                        raise
                    if result.directory is not None:
                        names.append(entry.name)
        except SourceError:
            raise
        except _FS_ERRORS as exc:
            raise SourceError(
                CODE_SELECTION_INVALID,
                f"{context}: directory cannot be enumerated: {exc}",
            ) from exc
        return names

    def _require_source_device(
        self,
        value: os.stat_result,
        *,
        code: str,
        context: str,
        path: str,
    ) -> None:
        """Reject a directory or file crossing a mounted filesystem boundary."""

        if value.st_dev != self.root.identity[0]:
            raise SourceError(
                code,
                f"{context}: {path!r} crosses the source filesystem boundary",
            )

    def managed_boundary(
        self,
        directory: Directory,
        managed_names: frozenset[str],
        *,
        context: str,
    ) -> str | None:
        """Return a managed-output reason from frozen identities.

        Phase A has already resolved every managed root reached by this
        selector.  Phase B therefore only compares the selected directory's
        descriptor-backed ancestry with that identity set.  No managed-name
        probe, parent open, or path-based containment decision is permitted
        here.
        """

        del managed_names, context
        if self.root_inside_managed is not None:
            return f"inside managed output {self.root_inside_managed!r}"
        if self.home_contains_root:
            return "inside the csk home"
        if self.home_identity is not None and any(
            item.identity == self.home_identity for item in directory.ancestry()
        ):
            return "inside the csk home"

        for item in directory.ancestry():
            managed_name = self.managed_root_identities.get(item.identity)
            if managed_name is not None:
                return f"inside managed output {managed_name!r}"
        return None

    def reverify_managed_boundaries(self) -> None:
        """Recheck source-local managed entries before a selection returns.

        Phase A records both present and absent managed-root locations.  The
        final check walks to each recorded base from the retained source-root
        descriptor and compares the current entry with that frozen record.
        It is deliberately a downward, descriptor-relative operation.  The
        entry identity and link spelling are checked before a symlink target
        is inspected, so a newly introduced alias cannot turn this check into
        an optimistic outside probe.

        Managed roots whose physical parent is outside the source root are
        not present in ``managed_root_bindings``; their final publication
        recheck belongs to the serialized publication transaction.
        """

        for binding in self.managed_root_bindings:
            context = (
                "Managed output boundary changed after Phase A at "
                f"{'/'.join((*binding.base_components, binding.name)) or '.'!r}"
            )
            try:
                base = self.root
                if binding.base_components:
                    base = self.descend(
                        self.root,
                        list(binding.base_components),
                        code=CODE_SNAPSHOT_CHANGED,
                        missing_code=CODE_SNAPSHOT_CHANGED,
                        context=context,
                    ).directory
                try:
                    current_entry = _lstat_at(
                        base.fd,
                        binding.name,
                        parent_path=base.display,
                        code=CODE_SNAPSHOT_CHANGED,
                        context=context,
                    )
                except SourceError as exc:
                    cause = exc.__cause__
                    if isinstance(cause, OSError) and cause.errno in (
                        errno.ENOENT,
                        errno.ENOTDIR,
                    ):
                        current_entry = None
                    else:
                        raise

                if current_entry is None:
                    current_identity = None
                else:
                    current_entry_identity = _identity_from_stat(current_entry)
                    if binding.entry_identity != current_entry_identity:
                        raise SourceError(CODE_SNAPSHOT_CHANGED, context)
                    if binding.target is not None:
                        current_target = _readlink_at(
                            base.fd,
                            binding.name,
                            parent_path=base.display,
                            code=CODE_SNAPSHOT_CHANGED,
                            context=context,
                        )
                        if current_target != binding.target:
                            raise SourceError(CODE_SNAPSHOT_CHANGED, context)
                        try:
                            target_stat = _stat_child(
                                base.fd,
                                binding.name,
                                parent_path=base.display,
                                follow_symlinks=True,
                            )
                        except OSError as exc:
                            if exc.errno in (errno.ENOENT, errno.ENOTDIR):
                                current_identity = None
                            else:
                                raise SourceError(
                                    CODE_SNAPSHOT_CHANGED,
                                    f"{context}: {exc}",
                                ) from exc
                        else:
                            current_identity = (
                                _identity_from_stat(target_stat)
                                if stat.S_ISDIR(target_stat.st_mode)
                                else None
                            )
                    else:
                        current_identity = (
                            current_entry_identity
                            if stat.S_ISDIR(current_entry.st_mode)
                            else None
                        )

                if current_identity != binding.identity:
                    raise SourceError(CODE_SNAPSHOT_CHANGED, context)
            except SourceError as exc:
                if exc.code == CODE_SNAPSHOT_CHANGED:
                    if exc.detail == context:
                        raise
                    raise SourceError(CODE_SNAPSHOT_CHANGED, context) from exc
                raise SourceError(CODE_SNAPSHOT_CHANGED, context) from exc
            except _FS_ERRORS as exc:
                raise SourceError(CODE_SNAPSHOT_CHANGED, context) from exc

    def snapshot_member(
        self,
        member: Directory,
        *,
        label: str,
    ) -> MemberSnapshot:
        """Read every member entry before downstream readers run."""

        files: dict[tuple[str, ...], bytes] = {}
        directories: set[tuple[str, ...]] = {()}
        stack: list[tuple[Directory, tuple[str, ...]]] = [(member, ())]
        while stack:
            current, relative = stack.pop()
            try:
                with _scandir(current.fd, fallback_path=current.display) as entries:
                    for entry in entries:
                        name = entry.name
                        child_relative = (*relative, name)
                        try:
                            entry_stat = entry.stat(follow_symlinks=False)
                        except _FS_ERRORS as exc:
                            raise SourceError(
                                CODE_MEMBER_INVALID,
                                f"Skill member {label} cannot inspect {name!r}: {exc}",
                            ) from exc
                        if stat.S_ISLNK(entry_stat.st_mode):
                            raise SourceError(
                                CODE_MEMBER_INVALID,
                                f"Skill member {label} contains rejected link {name!r}; "
                                "the link escapes the source root or links in admitted inputs are not allowed",
                            )
                        if stat.S_ISDIR(entry_stat.st_mode):
                            child = self._open_regular_child(
                                current,
                                name,
                                code=CODE_MEMBER_INVALID,
                                context=f"Skill member {label}",
                            )
                            directories.add(child_relative)
                            stack.append((child, child_relative))
                            continue
                        if stat.S_ISREG(entry_stat.st_mode):
                            if getattr(entry_stat, "st_nlink", 1) > 1:
                                raise SourceError(
                                    CODE_MEMBER_INVALID,
                                    f"Skill member {label} entry {name!r} is a hard link",
                                )
                            files[child_relative] = self._read_regular_file(
                                current,
                                name,
                                code=CODE_MEMBER_INVALID,
                                context=f"Skill member {label}",
                                expected_identity=_identity_from_stat(entry_stat),
                                expected_nlink=getattr(entry_stat, "st_nlink", 1),
                            )
                            continue
                        raise SourceError(
                            CODE_MEMBER_INVALID,
                            f"Skill member {label} entry {name!r} is not a regular file or directory",
                        )
            except SourceError:
                raise
            except _FS_ERRORS as exc:
                raise SourceError(
                    CODE_MEMBER_INVALID,
                    f"Skill member {label} cannot be inspected: {exc}",
                ) from exc
        return MemberSnapshot(
            root=member.display,
            files=files,
            directories=frozenset(directories),
        )

    def _open_regular_child(
        self,
        parent: Directory,
        name: str,
        *,
        code: str,
        context: str,
    ) -> Directory:
        try:
            fd = _open_child_directory(parent.fd, name, parent_path=parent.display)
        except _FS_ERRORS as exc:
            if isinstance(exc, SourceError):
                raise
            raise SourceError(code, f"{context}: {name!r}: {exc}") from exc
        try:
            value = _fstat(fd, code=code, context=context, path=name)
            self._require_source_device(
                value,
                code=code,
                context=context,
                path=name,
            )
            if not stat.S_ISDIR(value.st_mode):
                raise SourceError(code, f"{context}: {name!r} is not a directory")
            identity = _identity_from_stat(value)
            if any(item.identity == identity for item in parent.ancestry()):
                raise SourceError(code, f"{context}: directory cycle at {name!r}")
            child = Directory(
                fd=fd,
                identity=identity,
                name=name,
                display=_display_child(parent.display, name),
                parent=parent,
                aliases=parent.aliases,
            )
            self._fds.add(fd)
            return child
        except SourceError:
            _close_quietly(fd)
            raise

    def _read_regular_file(
        self,
        parent: Directory,
        name: str,
        *,
        code: str,
        context: str,
        expected_identity: tuple[int, int] | None = None,
        expected_nlink: int = 1,
    ) -> bytes:
        try:
            fd = _open_child_file(parent.fd, name, parent_path=parent.display)
        except _FS_ERRORS as exc:
            if isinstance(exc, SourceError):
                raise
            raise SourceError(code, f"{context}: {name!r}: {exc}") from exc
        try:
            value = _fstat(fd, code=code, context=context, path=name)
            self._require_source_device(
                value,
                code=code,
                context=context,
                path=name,
            )
            if not stat.S_ISREG(value.st_mode):
                raise SourceError(code, f"{context}: {name!r} is not a regular file")
            opened_identity = _identity_from_stat(value)
            opened_nlink = getattr(value, "st_nlink", 1)
            if opened_nlink != 1 or expected_nlink != 1:
                raise SourceError(
                    code,
                    f"{context}: {name!r} is a hard link",
                )
            if expected_identity is not None and opened_identity != expected_identity:
                raise SourceError(
                    code,
                    f"{context}: {name!r} changed after entry inspection",
                )
            chunks: list[bytes] = []
            while True:
                chunk = _read_descriptor(
                    fd,
                    1024 * 1024,
                    parent_fd=parent.fd,
                    name=name,
                )
                if not chunk:
                    break
                chunks.append(chunk)
            return b"".join(chunks)
        except SourceError:
            raise
        except _FS_ERRORS as exc:
            raise SourceError(code, f"{context}: {name!r}: {exc}") from exc
        finally:
            _close_quietly(fd)


def _prepare_preflight(
    source_root: Path,
    home: Path,
    managed_names: frozenset[str],
    *,
    preflight: PreflightRequest,
) -> PreflightState:
    """Resolve all outside identity questions before the Phase-B transition.

    The temporary descriptors opened here are never handed to a selection
    session.  Absolute-link replay plans are resolved only for the paths the
    declared selector can reach (and for the immediate children of wildcard
    collection bases).  An ordinary unselected subtree is never enumerated.
    """

    home_directory = _probe_optional_directory(home)
    preflight_fd: int | None = None
    phase_a_state: _PhaseAState | None = None
    try:
        preflight_fd = _open_root(source_root, phase="a")
        preflight_stat = _fstat(
            preflight_fd,
            code=CODE_SELECTION_INVALID,
            context=f"Source root {source_root}",
            path=os.fspath(source_root),
        )
        if not stat.S_ISDIR(preflight_stat.st_mode):
            raise SourceError(
                CODE_SELECTION_INVALID,
                f"Source root {source_root} is not a directory",
            )
        root_identity = _identity_from_stat(preflight_stat)
        root_display = Path(os.path.abspath(os.fspath(source_root)))
        managed_root_identities: dict[tuple[int, int], str] = {}
        root_inside_managed = _root_managed_ancestor(
            preflight_fd,
            root_identity,
            managed_names,
            source_root=source_root,
            context=f"Source root {source_root} and managed outputs",
            managed_root_identities=managed_root_identities,
        )
        home_identity = None if home_directory is None else home_directory.identity
        home_contains_root = False
        if home_identity is not None:
            home_contains_root = _root_has_ancestor(
                preflight_fd,
                root_identity,
                home_identity,
                root_path=source_root,
                context=f"Source root {source_root} and csk home",
            )
        phase_a_root = Directory(
            fd=preflight_fd,
            identity=root_identity,
            name=Path(os.fspath(source_root)).name,
            display=root_display,
            parent=None,
        )
        phase_a_state = _PhaseAState(
            root=phase_a_root,
            root_identity=root_identity,
            home_identity=home_identity,
            managed_names=managed_names,
            managed_root_identities=managed_root_identities,
            managed_root_bindings={},
            absolute_links={},
            component_bindings={},
        )
        _phase_a_collect_managed_roots(
            phase_a_state,
            phase_a_root,
            context=f"Source root {source_root} and managed outputs",
        )
        _phase_a_selector_reachability(phase_a_state, preflight)
        return PreflightState(
            root_identity=root_identity,
            home_identity=home_identity,
            home_contains_root=home_contains_root,
            root_inside_managed=root_inside_managed,
            managed_root_identities=MappingProxyType(
                dict(phase_a_state.managed_root_identities)
            ),
            managed_root_bindings=tuple(phase_a_state.managed_root_bindings.values()),
            absolute_links=MappingProxyType(dict(phase_a_state.absolute_links)),
            component_bindings=MappingProxyType(
                dict(phase_a_state.component_bindings)
            ),
        )
    except SourceError:
        raise
    except _FS_ERRORS as exc:
        raise SourceError(
            CODE_SELECTION_INVALID,
            f"Source root {source_root} preflight failed: {exc}",
        ) from exc
    finally:
        if phase_a_state is not None:
            for fd in phase_a_state.temporary_fds:
                _close_quietly(fd)
        if preflight_fd is not None:
            _close_quietly(preflight_fd)
        if home_directory is not None:
            _close_quietly(home_directory.fd)


@dataclass
class _PhaseAState:
    """Mutable workspace used only while constructing the frozen record."""

    root: Directory
    root_identity: tuple[int, int]
    home_identity: tuple[int, int] | None
    managed_names: frozenset[str]
    managed_root_identities: dict[tuple[int, int], str]
    managed_root_bindings: dict[tuple[tuple[str, ...], str], ManagedRootBinding]
    absolute_links: dict[tuple[int, int], AbsoluteLinkPlan]
    component_bindings: dict[ComponentKey, ComponentBinding]
    temporary_fds: list[int] = field(default_factory=list)


def _phase_a_selector_reachability(
    state: _PhaseAState,
    request: PreflightRequest,
) -> None:
    """Preflight only declared paths and non-excluded wildcard frontiers."""

    for raw_spec in request.paths:
        spec = (
            raw_spec
            if isinstance(raw_spec, PreflightPath)
            else PreflightPath(tuple(raw_spec))
        )
        _phase_a_descend(
            state,
            list(spec.components),
            code=spec.code,
            missing_code=spec.missing_code,
            not_directory_code=spec.not_directory_code,
            context=spec.context,
            collect_boundaries=spec.collect_boundaries,
        )

    for index, components in enumerate(request.wildcard_bases):
        excluded = (
            request.wildcard_excludes[index]
            if index < len(request.wildcard_excludes)
            else frozenset()
        )
        base = _phase_a_descend(
            state,
            list(components),
            code=CODE_SELECTION_INVALID,
            context="Wildcard collection preflight",
            collect_boundaries=True,
        )
        if base is None:
            continue
        try:
            with _scandir(base.fd) as entries:
                for entry in entries:
                    if entry.name in excluded:
                        # The immediate entry was observed, which is all the
                        # exclusion semantics require. Do not inspect the
                        # excluded member's contents or managed boundaries.
                        continue
                    try:
                        entry_stat = entry.stat(follow_symlinks=False)
                    except _FS_ERRORS as exc:
                        raise SourceError(
                            CODE_SELECTION_INVALID,
                            "Wildcard collection preflight cannot inspect "
                            f"{entry.name!r}: {exc}",
                        ) from exc
                    if stat.S_ISDIR(entry_stat.st_mode) or stat.S_ISLNK(
                        entry_stat.st_mode
                    ):
                        try:
                            _phase_a_descend(
                                state,
                                [entry.name],
                                start=base,
                                code=CODE_SELECTION_INVALID,
                                context="Wildcard collection preflight",
                                allow_absence=True,
                                collect_boundaries=True,
                            )
                        except SourceError as exc:
                            # A wildcard candidate that is already known to
                            # overlap a managed output is an explicit Phase-A
                            # pruning decision.  Keep its recorded link
                            # binding/plan so Phase B can replay the same
                            # refusal and prune it without probing elsewhere.
                            if (
                                exc.code == CODE_OUTPUT_OVERLAP
                                and "absolute symlink target lies inside the csk home"
                                in exc.detail
                            ):
                                continue
                            raise
        except SourceError:
            raise
        except _FS_ERRORS as exc:
            raise SourceError(
                CODE_SELECTION_INVALID,
                "Wildcard collection preflight cannot enumerate the "
                f"selected collection: {exc}",
            ) from exc


def _phase_a_descend(
    state: _PhaseAState,
    components: list[str],
    *,
    start: Directory | None = None,
    code: str = CODE_SELECTION_INVALID,
    missing_code: str | None = None,
    not_directory_code: str | None = None,
    context: str,
    collect_boundaries: bool = True,
    allow_absence: bool = False,
) -> Directory | None:
    """Resolve one reachable path and freeze every component it opens.

    A declared path has only two Phase-A outcomes: a complete resolution or a
    structured refusal. ``allow_absence`` is reserved for a wildcard child
    that disappeared after the immediate frontier was enumerated; all other
    failures, including an unreadable probe, propagate and prevent Phase B
    from starting with an incomplete record.
    """

    if start is None:
        start = state.root
    if not components:
        return start

    pending: list[str] = list(components)
    stack: list[Directory] = list(start.ancestry())
    aliases: list[AliasEntry] = list(start.aliases)
    visited_states: set[tuple[tuple[int, int], tuple[str, ...]]] = set()
    expansions = 0

    while pending:
        part = pending.pop(0)
        if part in ("", "."):
            continue
        if part == "..":
            if len(stack) == 1:
                raise SourceError(
                    code,
                    f"{context}: parent traversal would escape the source root",
                )
            stack.pop()
            aliases = list(stack[-1].aliases)
            continue

        parent = stack[-1]
        try:
            child_fd = _open_child_directory(
                parent.fd,
                part,
                parent_path=parent.display,
            )
        except SourceError as exc:
            if allow_absence and _is_absence_error(exc):
                return None
            raise
        except OSError as exc:
            if exc.errno == errno.ENOENT:
                if allow_absence:
                    return None
                raise SourceError(
                    missing_code or code,
                    f"{context}: component {part!r} does not exist or is not a directory",
                ) from exc
            if exc.errno not in (errno.ELOOP, errno.EMLINK, errno.ENOTDIR):
                raise SourceError(code, f"{context}: {part!r}: {exc}") from exc
            try:
                link_stat = _lstat_at(
                    parent.fd,
                    part,
                    parent_path=parent.display,
                    code=CODE_SELECTION_INVALID,
                    context=context,
                )
            except SourceError as lstat_error:
                if allow_absence and _is_absence_error(lstat_error):
                    return None
                raise
            if not stat.S_ISLNK(link_stat.st_mode):
                raise SourceError(
                    not_directory_code or code,
                    f"{context}: component {part!r} exists but is not a directory",
                ) from exc
            link_identity = _identity_from_stat(link_stat)
            resolution_state = (link_identity, tuple(pending))
            if resolution_state in visited_states:
                raise SourceError(
                    code, f"{context}: symlink loop at {part!r}"
                ) from exc
            visited_states.add(resolution_state)
            target = _readlink_at(
                parent.fd,
                part,
                parent_path=parent.display,
                code=code,
                context=context,
            )
            _record_component_binding(
                state.component_bindings,
                parent.identity,
                part,
                ComponentBinding(link_identity, is_symlink=True, target=target),
            )
            expansions += 1
            if expansions > 64:
                raise SourceError(
                    code, f"{context}: too many symlink expansions at {part!r}"
                ) from exc
            if _is_absolute_target(target):
                plan = state.absolute_links.get(link_identity)
                if plan is None:
                    plan = _phase_a_absolute_link_plan(
                        _display_child(parent.display, part),
                        target,
                        root_identity=state.root_identity,
                        home_identity=state.home_identity,
                        root_spelling=os.fspath(state.root.display),
                    )
                    state.absolute_links[link_identity] = plan
                if plan.components is None:
                    if allow_absence and plan.error_errno in (
                        errno.ENOENT,
                        errno.ENOTDIR,
                    ):
                        return None
                    detail = plan.detail or (
                        f"{context}: absolute symlink target cannot be resolved"
                    )
                    if plan.error_errno is not None:
                        cause = OSError(plan.error_errno, detail)
                        raise SourceError(plan.error_code or code, detail) from cause
                    raise SourceError(plan.error_code or code, detail)
                stack = [state.root]
                aliases = []
                pending = [*plan.components, *pending]
            else:
                pending = [*_split_link_target(target), *pending]
            continue
        except _FS_ERRORS as exc:
            raise SourceError(code, f"{context}: {part!r}: {exc}") from exc

        try:
            child_stat = _fstat(
                child_fd,
                code=code,
                context=context,
                path=part,
            )
        except SourceError:
            _close_quietly(child_fd)
            raise
        except _FS_ERRORS as exc:
            _close_quietly(child_fd)
            raise SourceError(code, f"{context}: {part!r}: {exc}") from exc
        if not stat.S_ISDIR(child_stat.st_mode):
            _close_quietly(child_fd)
            raise SourceError(code, f"{context}: {part!r} is not a directory")
        if child_stat.st_dev != state.root_identity[0]:
            _close_quietly(child_fd)
            raise SourceError(
                code,
                f"{context}: {part!r} crosses the source filesystem boundary",
            )
        child_identity = _identity_from_stat(child_stat)
        if any(item.identity == child_identity for item in stack):
            _close_quietly(child_fd)
            raise SourceError(code, f"{context}: directory cycle at {part!r}")
        try:
            _record_component_binding(
                state.component_bindings,
                parent.identity,
                part,
                ComponentBinding(child_identity, is_symlink=False),
            )
        except SourceError:
            _close_quietly(child_fd)
            raise
        child = Directory(
            fd=child_fd,
            identity=child_identity,
            name=part,
            display=_display_child(parent.display, part),
            parent=parent,
            aliases=tuple(aliases),
        )
        state.temporary_fds.append(child_fd)
        stack.append(child)
        aliases = list(child.aliases)
        if collect_boundaries:
            _phase_a_collect_managed_roots(
                state,
                child,
                context=context,
            )

    return stack[-1]


def _record_component_binding(
    bindings: dict[ComponentKey, ComponentBinding],
    parent_identity: tuple[int, int],
    name: str,
    binding: ComponentBinding,
) -> None:
    """Add one Phase-A component decision without allowing contradictions."""

    key = (parent_identity, name)
    previous = bindings.get(key)
    if previous is not None and previous != binding:
        raise SourceError(
            CODE_SELECTION_INVALID,
            f"Phase-A component {name!r} was observed with conflicting identities",
        )
    bindings[key] = binding


def _managed_base_components(root: Directory, base: Directory) -> tuple[str, ...]:
    """Return the physical downward names from ``root`` to ``base``."""

    ancestry = base.ancestry()
    if not ancestry or ancestry[0].identity != root.identity:
        raise SourceError(
            CODE_SELECTION_INVALID,
            "Phase-A managed boundary is not rooted at the source descriptor",
        )
    return tuple(item.name for item in ancestry[1:])


def _record_managed_root_binding(
    state: _PhaseAState,
    binding: ManagedRootBinding,
    *,
    context: str,
) -> None:
    """Freeze one managed entry, including an explicit absent state."""

    key = (binding.base_components, binding.name)
    previous = state.managed_root_bindings.get(key)
    if previous is not None and previous != binding:
        raise SourceError(
            CODE_OUTPUT_OVERLAP,
            f"{context}: managed boundary {binding.name!r} changed during Phase A",
        )
    state.managed_root_bindings[key] = binding
    if binding.identity is not None:
        _record_managed_root_identity(
            state.managed_root_identities,
            binding.identity,
            binding.name,
        )


def _phase_a_collect_managed_roots(
    state: _PhaseAState,
    base: Directory,
    *,
    context: str,
) -> None:
    """Record managed roots and absent locations at one reachable base."""

    base_components = _managed_base_components(state.root, base)

    for managed_name in sorted(
        state.managed_names,
        key=lambda name: name.encode("utf-8"),
    ):
        child_fd: int | None = None
        try:
            child_fd = _open_child_directory(
                base.fd,
                managed_name,
                parent_path=base.display,
            )
        except OSError as exc:
            if exc.errno == errno.ENOENT:
                _record_managed_root_binding(
                    state,
                    ManagedRootBinding(
                        base_components,
                        managed_name,
                        identity=None,
                        entry_identity=None,
                    ),
                    context=context,
                )
                continue
            if exc.errno not in (errno.ELOOP, errno.EMLINK, errno.ENOTDIR):
                raise SourceError(
                    CODE_OUTPUT_OVERLAP,
                    f"{context}: boundary undetermined at {managed_name!r}: {exc}",
                ) from exc
            try:
                link_free = _lstat_at(
                    base.fd,
                    managed_name,
                    parent_path=base.display,
                    code=CODE_OUTPUT_OVERLAP,
                    context=context,
                )
            except SourceError as lstat_error:
                cause = lstat_error.__cause__
                if isinstance(cause, OSError) and cause.errno in (
                    errno.ENOENT,
                    errno.ENOTDIR,
                ):
                    _record_managed_root_binding(
                        state,
                        ManagedRootBinding(
                            base_components,
                            managed_name,
                            identity=None,
                            entry_identity=None,
                        ),
                        context=context,
                    )
                    continue
                raise
            if not stat.S_ISLNK(link_free.st_mode):
                if exc.errno == errno.ENOTDIR:
                    _record_managed_root_binding(
                        state,
                        ManagedRootBinding(
                            base_components,
                            managed_name,
                            identity=None,
                            entry_identity=_identity_from_stat(link_free),
                        ),
                        context=context,
                    )
                    continue
                raise SourceError(
                    CODE_OUTPUT_OVERLAP,
                    f"{context}: boundary undetermined at {managed_name!r}: {exc}",
                ) from exc
            link_identity = _identity_from_stat(link_free)
            target = _readlink_at(
                base.fd,
                managed_name,
                parent_path=base.display,
                code=CODE_OUTPUT_OVERLAP,
                context=context,
            )
            try:
                candidate_stat = _stat_child(
                    base.fd,
                    managed_name,
                    parent_path=base.display,
                    follow_symlinks=True,
                )
            except OSError as stat_error:
                if stat_error.errno in (errno.ENOENT, errno.ENOTDIR):
                    _record_managed_root_binding(
                        state,
                        ManagedRootBinding(
                            base_components,
                            managed_name,
                            identity=None,
                            entry_identity=link_identity,
                            target=target,
                        ),
                        context=context,
                    )
                    continue
                raise SourceError(
                    CODE_OUTPUT_OVERLAP,
                    f"{context}: boundary undetermined at {managed_name!r}: "
                    f"{stat_error}",
                ) from stat_error
            except _FS_ERRORS as stat_error:
                raise SourceError(
                    CODE_OUTPUT_OVERLAP,
                    f"{context}: boundary undetermined at {managed_name!r}: "
                    f"{stat_error}",
                ) from stat_error
            _record_managed_root_binding(
                state,
                ManagedRootBinding(
                    base_components,
                    managed_name,
                    identity=(
                        _identity_from_stat(candidate_stat)
                        if stat.S_ISDIR(candidate_stat.st_mode)
                        else None
                    ),
                    entry_identity=link_identity,
                    target=target,
                ),
                context=context,
            )
            if stat.S_ISDIR(candidate_stat.st_mode):
                _record_managed_root_identity(
                    state.managed_root_identities,
                    _identity_from_stat(candidate_stat),
                    managed_name,
                )
            continue
        except _FS_ERRORS as exc:
            raise SourceError(
                CODE_OUTPUT_OVERLAP,
                f"{context}: boundary undetermined at {managed_name!r}: {exc}",
            ) from exc

        try:
            candidate_stat = os.fstat(child_fd)
        except _FS_ERRORS as exc:
            raise SourceError(
                CODE_OUTPUT_OVERLAP,
                f"{context}: boundary undetermined at {managed_name!r}: {exc}",
            ) from exc
        finally:
            _close_quietly(child_fd)
        _record_managed_root_binding(
            state,
            ManagedRootBinding(
                base_components,
                managed_name,
                identity=(
                    _identity_from_stat(candidate_stat)
                    if stat.S_ISDIR(candidate_stat.st_mode)
                    else None
                ),
                entry_identity=(
                    _identity_from_stat(candidate_stat)
                    if stat.S_ISDIR(candidate_stat.st_mode)
                    else None
                ),
            ),
            context=context,
        )


def _record_managed_root_identity(
    identities: dict[tuple[int, int], str],
    identity: tuple[int, int],
    name: str,
) -> None:
    """Keep one deterministic diagnostic name for each managed identity."""

    identities.setdefault(identity, name)


def _phase_a_absolute_link_plan(
    link_path: Path,
    target: str,
    *,
    root_identity: tuple[int, int],
    home_identity: tuple[int, int] | None,
    root_spelling: str,
) -> AbsoluteLinkPlan:
    """Decide an absolute link by descriptor identity and record names.

    The target is opened in Phase A, then its physical ancestry is walked by
    descriptor.  Each physical component name is recovered by scanning its
    already-opened parent and comparing ``(st_dev, st_ino)``.  No lexical
    prefix or relative-path calculation decides containment.  The resulting
    names are the only thing Phase B replays.
    """

    if os.name != "posix":
        return AbsoluteLinkPlan(
            components=None,
            detail=(
                f"{link_path}: absolute symlink targets are unsupported by "
                "the Windows fallback"
            ),
        )

    target_fd: int | None = None
    temporary: list[int] = []
    try:
        try:
            target_fd = _open_descriptor(
                Path(target), _directory_flags(nofollow=False)
            )
        except OSError as exc:
            return AbsoluteLinkPlan(
                components=None,
                detail=f"{link_path}: absolute target {target!r} cannot be opened: {exc}",
                error_errno=exc.errno,
            )
        target_stat = _fstat(
            target_fd,
            code=CODE_SELECTION_INVALID,
            context=f"Absolute target {target!r}",
            path=target,
        )
        current_fd = target_fd
        current_identity = _identity_from_stat(target_stat)
        visited: set[tuple[int, int]] = set()
        reversed_components: list[str] = []
        while True:
            if current_identity == root_identity:
                reversed_components.reverse()
                return AbsoluteLinkPlan(
                    components=tuple(reversed_components),
                    target_spelling=target,
                    root_spelling=root_spelling,
                )
            if home_identity is not None and current_identity == home_identity:
                return AbsoluteLinkPlan(
                    components=None,
                    error_code=CODE_OUTPUT_OVERLAP,
                    detail=f"{link_path}: absolute symlink target lies inside the csk home",
                )
            if current_identity in visited:
                return AbsoluteLinkPlan(
                    components=None,
                    detail=f"{link_path}: absolute target has an ancestor cycle",
                )
            visited.add(current_identity)
            parent_fd = _open_descriptor(
                "..",
                _directory_flags(nofollow=True),
                dir_fd=current_fd,
            )
            temporary.append(parent_fd)
            parent_stat = _fstat(
                parent_fd,
                code=CODE_SELECTION_INVALID,
                context=f"Absolute target {target!r}",
                path="..",
            )
            parent_identity = _identity_from_stat(parent_stat)
            if parent_identity == current_identity:
                return AbsoluteLinkPlan(
                    components=None,
                    detail=f"{link_path}: symlink target escapes the source root",
                )
            child_name = _phase_a_name_for_identity(parent_fd, current_identity)
            if child_name is None:
                return AbsoluteLinkPlan(
                    components=None,
                    detail=(
                        f"{link_path}: cannot recover the physical name of "
                        "the absolute target"
                    ),
                )
            reversed_components.append(child_name)
            current_fd = parent_fd
            current_identity = parent_identity
    except SourceError as exc:
        cause = exc.__cause__
        return AbsoluteLinkPlan(
            components=None,
            detail=f"{link_path}: absolute target identity walk failed: {exc.detail}",
            error_errno=cause.errno if isinstance(cause, OSError) else None,
        )
    except OSError as exc:
        return AbsoluteLinkPlan(
            components=None,
            detail=f"{link_path}: absolute target identity walk failed: {exc}",
            error_errno=exc.errno,
        )
    except _FS_ERRORS as exc:
        return AbsoluteLinkPlan(
            components=None,
            detail=f"{link_path}: absolute target identity walk failed: {exc}",
        )
    finally:
        if target_fd is not None:
            _close_quietly(target_fd)
        for fd in temporary:
            _close_quietly(fd)


def _phase_a_name_for_identity(
    parent_fd: int,
    wanted_identity: tuple[int, int],
) -> str | None:
    """Find a physical child name in an already opened Phase-A parent."""

    with _scandir(parent_fd) as entries:
        for entry in entries:
            try:
                link_free = entry.stat(follow_symlinks=False)
            except _FS_ERRORS as exc:
                raise SourceError(
                    CODE_SELECTION_INVALID,
                    f"Cannot inspect absolute-target parent entry {entry.name!r}: {exc}",
                ) from exc
            if stat.S_ISLNK(link_free.st_mode):
                continue
            try:
                value = entry.stat(follow_symlinks=True)
            except _FS_ERRORS as exc:
                raise SourceError(
                    CODE_SELECTION_INVALID,
                    f"Cannot inspect absolute-target parent entry {entry.name!r}: {exc}",
                ) from exc
            if _identity_from_stat(value) == wanted_identity:
                return entry.name
    return None


def _open_root(path: Path, *, phase: str = "a") -> int:
    descriptor: int
    try:
        descriptor = _open_descriptor(path, _directory_flags(nofollow=True))
    except OSError as exc:
        # The source path itself may be a symlink.  This is the source-root
        # capability acquisition exception; descendants remain no-follow.
        if exc.errno in (errno.ELOOP, errno.EMLINK, errno.ENOTDIR):
            try:
                descriptor = _open_descriptor(path, _directory_flags(nofollow=False))
            except _FS_ERRORS as retry:
                raise SourceError(
                    CODE_SELECTION_INVALID,
                    f"Source root {path} cannot be opened: {retry}",
                ) from retry
        else:
            raise SourceError(
                CODE_SELECTION_INVALID,
                f"Source root {path} cannot be opened: {exc}",
            ) from exc
    except _FS_ERRORS as exc:
        raise SourceError(
            CODE_SELECTION_INVALID,
            f"Source root {path} cannot be opened: {exc}",
        ) from exc
    if phase == "b":
        _emit("phase-b-root", os.fspath(path), parent_fd=None, result_fd=descriptor)
    return descriptor


def _root_has_ancestor(
    root_fd: int,
    root_identity: tuple[int, int],
    wanted_identity: tuple[int, int],
    *,
    root_path: Path,
    context: str,
) -> bool:
    """Check an opened root's ancestors without listing or retaining them."""

    current_fd = root_fd
    current_identity = root_identity
    current_path = Path(os.path.abspath(os.fspath(root_path)))
    temporary: list[int] = []
    seen: set[tuple[int, int]] = {root_identity}
    try:
        while True:
            if current_identity == wanted_identity:
                return True
            try:
                parent_fd, parent_path = _open_parent_directory(
                    current_fd,
                    current_path,
                )
                temporary.append(parent_fd)
                parent_stat = _fstat(
                    parent_fd,
                    code=CODE_OUTPUT_OVERLAP,
                    context=context,
                    path="..",
                )
            except SourceError:
                raise
            except _FS_ERRORS as exc:
                raise SourceError(
                    CODE_OUTPUT_OVERLAP,
                    f"{context}: boundary undetermined: {exc}",
                ) from exc
            parent_identity = _identity_from_stat(parent_stat)
            if parent_identity == current_identity:
                return False
            if parent_identity in seen:
                raise SourceError(
                    CODE_OUTPUT_OVERLAP,
                    f"{context}: boundary ancestor cycle",
                )
            seen.add(parent_identity)
            current_fd = parent_fd
            current_identity = parent_identity
            current_path = parent_path
    finally:
        for fd in temporary:
            _close_quietly(fd)


def _root_managed_ancestor(
    root_fd: int,
    root_identity: tuple[int, int],
    managed_names: frozenset[str],
    *,
    source_root: Path,
    context: str,
    managed_root_identities: dict[tuple[int, int], str] | None = None,
) -> str | None:
    """Find a managed ancestor of the source root by filesystem identity.

    The source-root descriptor deliberately has no parent link in the
    confined session.  This preflight walks its physical ancestors with
    temporary descriptors and compares each current object with managed-name
    entries opened from its parent.  Case-equivalent spellings and symlink
    aliases therefore use the filesystem's identity semantics instead of a
    component-string comparison.
    """

    if not managed_names:
        return None
    temporary: list[int] = []
    current_fd = root_fd
    current_identity = root_identity
    current_path = Path(os.path.abspath(os.fspath(source_root)))
    seen: set[tuple[int, int]] = {root_identity}
    try:
        while True:
            try:
                parent_fd, parent_path = _open_parent_directory(
                    current_fd,
                    current_path,
                )
                temporary.append(parent_fd)
                parent_stat = _fstat(
                    parent_fd,
                    code=CODE_OUTPUT_OVERLAP,
                    context=context,
                    path="..",
                )
            except SourceError:
                raise
            except _FS_ERRORS as exc:
                raise SourceError(
                    CODE_OUTPUT_OVERLAP,
                    f"{context}: boundary undetermined: {exc}",
                ) from exc

            for managed_name in sorted(
                managed_names, key=lambda name: name.encode("utf-8")
            ):
                try:
                    candidate_stat = _stat_child(
                        parent_fd,
                        managed_name,
                        parent_path=parent_path,
                        follow_symlinks=True,
                    )
                except OSError as exc:
                    if exc.errno in (errno.ENOENT, errno.ENOTDIR):
                        continue
                    raise SourceError(
                        CODE_OUTPUT_OVERLAP,
                        f"{context}: boundary undetermined at {managed_name!r}: {exc}",
                    ) from exc
                except _FS_ERRORS as exc:
                    raise SourceError(
                        CODE_OUTPUT_OVERLAP,
                        f"{context}: boundary undetermined at {managed_name!r}: {exc}",
                    ) from exc
                if stat.S_ISDIR(candidate_stat.st_mode):
                    candidate_identity = _identity_from_stat(candidate_stat)
                    if managed_root_identities is not None:
                        _record_managed_root_identity(
                            managed_root_identities,
                            candidate_identity,
                            managed_name,
                        )
                    if candidate_identity == current_identity:
                        return managed_name

            parent_identity = _identity_from_stat(parent_stat)
            if parent_identity == current_identity:
                return None
            if parent_identity in seen:
                raise SourceError(
                    CODE_OUTPUT_OVERLAP,
                    f"{context}: boundary ancestor cycle",
                )
            seen.add(parent_identity)
            current_fd = parent_fd
            current_identity = parent_identity
            current_path = parent_path
    finally:
        for fd in temporary:
            _close_quietly(fd)


def _open_parent_directory(current_fd: int, current_path: Path) -> tuple[int, Path]:
    """Open a physical parent, with the documented Windows fallback."""

    try:
        return (
            _open_descriptor(
                "..",
                _directory_flags(nofollow=True),
                dir_fd=current_fd,
            ),
            current_path.parent,
        )
    except (TypeError, NotImplementedError):
        parent_path = current_path.parent
        try:
            value = os.lstat(parent_path)
            if stat.S_ISLNK(value.st_mode) or _is_reparse_point(value):
                raise OSError(errno.ELOOP, f"parent is a link: {parent_path}")
            return (
                _open_descriptor(parent_path, _directory_flags(nofollow=False)),
                parent_path,
            )
        except _FS_ERRORS:
            raise


def _probe_optional_directory(path: Path) -> Directory | None:
    """Open an optional boundary; only ENOENT means a fresh absent home."""

    try:
        fd = _open_descriptor(path, _directory_flags(nofollow=False))
    except OSError as exc:
        if exc.errno == errno.ENOENT:
            return None
        raise SourceError(
            CODE_OUTPUT_OVERLAP,
            f"{path}: boundary undetermined: {exc}",
        ) from exc
    except _FS_ERRORS as exc:
        raise SourceError(
            CODE_OUTPUT_OVERLAP,
            f"{path}: boundary undetermined: {exc}",
        ) from exc
    try:
        value = _fstat(
            fd,
            code=CODE_OUTPUT_OVERLAP,
            context=f"csk home {path}",
            path=os.fspath(path),
        )
        if not stat.S_ISDIR(value.st_mode):
            raise SourceError(
                CODE_OUTPUT_OVERLAP,
                f"{path}: boundary undetermined: not a directory",
            )
        return Directory(
            fd=fd,
            identity=_identity_from_stat(value),
            name=Path(os.fspath(path)).name,
            display=Path(os.path.abspath(os.fspath(path))),
            parent=None,
        )
    except SourceError:
        _close_quietly(fd)
        raise
    except _FS_ERRORS as exc:
        _close_quietly(fd)
        raise SourceError(
            CODE_OUTPUT_OVERLAP,
            f"{path}: boundary undetermined: {exc}",
        ) from exc


def _open_child_directory(
    parent_fd: int,
    name: str,
    *,
    parent_path: Path | None = None,
) -> int:
    try:
        return _open_descriptor(
            name,
            _directory_flags(nofollow=True),
            dir_fd=parent_fd,
        )
    except (TypeError, NotImplementedError):
        # Windows has no descriptor-relative openat.  The fallback checks the
        # entry before opening and callers re-stat the result.  Its weaker
        # no-follow guarantee is documented in the task evidence.
        if parent_path is None:
            raise OSError(errno.ENOTSUP, "descriptor-relative directory open unavailable")
        child = parent_path / name
        value = os.lstat(child)
        if stat.S_ISLNK(value.st_mode) or _is_reparse_point(value):
            raise OSError(errno.ELOOP, f"symbolic or reparse link: {child}")
        return _open_descriptor(child, _directory_flags(nofollow=False))


def _open_child_file(
    parent_fd: int,
    name: str,
    *,
    parent_path: Path | None = None,
) -> int:
    try:
        return _open_descriptor(name, _file_flags(nofollow=True), dir_fd=parent_fd)
    except (TypeError, NotImplementedError):
        if parent_path is None:
            raise OSError(errno.ENOTSUP, "descriptor-relative file open unavailable")
        child = parent_path / name
        value = os.lstat(child)
        if stat.S_ISLNK(value.st_mode) or _is_reparse_point(value):
            raise OSError(errno.ELOOP, f"symbolic or reparse link: {child}")
        return _open_descriptor(child, _file_flags(nofollow=False))


def _stat_child(
    parent_fd: int,
    name: str,
    *,
    parent_path: Path | None,
    follow_symlinks: bool,
) -> os.stat_result:
    try:
        return os.stat(name, dir_fd=parent_fd, follow_symlinks=follow_symlinks)
    except (TypeError, NotImplementedError):
        if parent_path is None:
            raise OSError(errno.ENOTSUP, "descriptor-relative stat unavailable")
        return os.stat(parent_path / name, follow_symlinks=follow_symlinks)
    except _FS_ERRORS:
        raise


def _is_reparse_point(value: os.stat_result) -> bool:
    attributes = getattr(value, "st_file_attributes", 0)
    return bool(attributes & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400))


def _lstat_at(
    parent_fd: int,
    name: str,
    *,
    parent_path: Path | None = None,
    code: str,
    context: str,
) -> os.stat_result:
    try:
        return os.lstat(name, dir_fd=parent_fd)
    except (TypeError, NotImplementedError):
        if parent_path is None:
            raise SourceError(
                code,
                f"{context}: descriptor-relative lstat unavailable for {name!r}",
            )
        try:
            return os.lstat(parent_path / name)
        except _FS_ERRORS as exc:
            raise SourceError(code, f"{context}: {name!r}: {exc}") from exc
    except _FS_ERRORS as exc:
        raise SourceError(code, f"{context}: {name!r}: {exc}") from exc


def _readlink_at(
    parent_fd: int,
    name: str,
    *,
    parent_path: Path | None = None,
    code: str,
    context: str,
) -> str:
    try:
        value = os.readlink(name, dir_fd=parent_fd)
    except (TypeError, NotImplementedError):
        if parent_path is None:
            raise SourceError(
                code,
                f"{context}: descriptor-relative readlink unavailable for {name!r}",
            )
        try:
            value = os.readlink(parent_path / name)
        except _FS_ERRORS as exc:
            raise SourceError(code, f"{context}: {name!r}: {exc}") from exc
    except _FS_ERRORS as exc:
        raise SourceError(code, f"{context}: {name!r}: {exc}") from exc
    if not isinstance(value, str) or not value:
        raise SourceError(code, f"{context}: empty symlink target for {name!r}")
    return value


def _fstat(fd: int, *, code: str, context: str, path: str) -> os.stat_result:
    try:
        return os.fstat(fd)
    except _FS_ERRORS as exc:
        raise SourceError(code, f"{context}: {path!r}: {exc}") from exc


def _is_absolute_target(target: str) -> bool:
    if os.name == "posix":
        return target.startswith("/")
    return target.startswith(("/", "\\"))


def _split_link_target(target: str) -> list[str]:
    """Split a link target with native filename semantics.

    On POSIX a backslash is an ordinary filename character.  Replacing it
    with ``/`` would create a directory that the operating system never
    resolved and is therefore forbidden.  The Windows fallback accepts its
    native alternate separator.
    """

    if os.name == "nt":
        return [part for part in target.replace("/", "\\").split("\\") if part]
    return [part for part in target.split("/") if part]


def _is_absence_error(error: SourceError) -> bool:
    cause: BaseException | None = error.__cause__
    return isinstance(cause, OSError) and cause.errno in (errno.ENOENT, errno.ENOTDIR)


def _display_child(parent: Path, name: str) -> Path:
    # Display only.  Security decisions use Directory identities and fds.
    return Path(os.path.normpath(os.fspath(parent / name)))


def _close_quietly(fd: int) -> None:
    try:
        os.close(fd)
    except _FS_ERRORS:
        pass
