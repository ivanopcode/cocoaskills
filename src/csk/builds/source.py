from __future__ import annotations

import hashlib
import os
import stat
import threading
import unicodedata
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from pathlib import Path
from types import TracebackType
from typing import BinaryIO, Final, Literal, Protocol, TypeVar

from .. import hashing
from ..identifiers import is_valid_portable_path


class BuildSourceError(Exception):
    pass


class InvalidSnapshotError(BuildSourceError):
    pass


class SnapshotMutationError(BuildSourceError):
    pass


@dataclass(frozen=True)
class BuildSourceIdentity:
    algorithm: str
    content_sha256: str


@dataclass(frozen=True)
class _DiscoveredFile:
    path: str
    path_bytes: bytes
    size: int
    file_key: tuple[int, int]
    absolute_path: Path


@dataclass(frozen=True)
class _StateEntry:
    path_bytes: bytes
    kind: str
    size: int = 0
    content_sha256: bytes = b""


@dataclass(frozen=True)
class _SnapshotScan:
    identity: BuildSourceIdentity
    state: tuple[_StateEntry, ...]


_T = TypeVar("_T")
_READ_CHUNK_SIZE: Final = 1024 * 1024
_REPARSE_ATTRIBUTE: Final = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)


class _Digest(Protocol):
    def update(self, data: bytes, /) -> None: ...


class FrozenSnapshot:
    """Identity and retained tree state for one validated snapshot instance."""

    def __init__(
        self,
        path: Path,
        root_stat: os.stat_result,
        root_fd: int | None,
        scan: _SnapshotScan,
    ) -> None:
        self._path = path
        self._root_stat = root_stat
        self._root_fd = root_fd
        self._identity = scan.identity
        self._state = scan.state
        self._lock = threading.RLock()
        self._closed = False

    @property
    def path(self) -> Path:
        return self._path

    @property
    def identity(self) -> BuildSourceIdentity:
        return self._identity

    def equivalent(self, other: FrozenSnapshot) -> bool:
        return self._identity == other._identity and self._state == other._state

    def recheck(self) -> None:
        with self._lock:
            if self._closed:
                raise SnapshotMutationError("build-source snapshot token is closed")
            try:
                current_root = os.lstat(self._path)
            except OSError as exc:
                raise SnapshotMutationError(f"build-source snapshot root is unavailable: {self._path}") from exc
            if _is_link_or_reparse(current_root) or not stat.S_ISDIR(current_root.st_mode):
                raise SnapshotMutationError(f"build-source snapshot root was replaced: {self._path}")
            if not _same_file(self._root_stat, current_root):
                raise SnapshotMutationError(f"build-source snapshot root was replaced: {self._path}")
            try:
                current = _scan_snapshot(self._path, self._root_fd)
            except InvalidSnapshotError as exc:
                raise SnapshotMutationError(f"build-source snapshot mutated: {exc}") from exc
            if current.identity != self._identity or current.state != self._state:
                raise SnapshotMutationError(f"build-source snapshot mutated: {self._path}")

    def use(self, callback: Callable[[FrozenSnapshot], _T]) -> _T:
        """Recheck immediately before and after cache or child-process use."""

        self.recheck()
        try:
            result = callback(self)
        except BaseException as callback_error:
            try:
                self.recheck()
            except SnapshotMutationError as mutation_error:
                raise mutation_error from callback_error
            raise
        self.recheck()
        return result

    def close(self) -> None:
        with self._lock:
            if self._closed:
                return
            self._closed = True
            root_fd = self._root_fd
            self._root_fd = None
        if root_fd is not None:
            os.close(root_fd)

    def __enter__(self) -> FrozenSnapshot:
        try:
            self.recheck()
        except BaseException:
            self.close()
            raise
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> Literal[False]:
        del exc_type, traceback
        mutation_error: SnapshotMutationError | None = None
        try:
            self.recheck()
        except SnapshotMutationError as error:
            mutation_error = error
        finally:
            self.close()
        if mutation_error is not None:
            if exc is not None:
                raise mutation_error from exc
            raise mutation_error
        return False


def freeze_snapshot(root: Path) -> FrozenSnapshot:
    """Validate all descendants without following links and retain the root."""

    path = Path(os.path.abspath(os.fspath(root)))
    root_stat = _validated_root_stat(path)
    root_fd = _open_root_fd(path)
    try:
        if root_fd is not None:
            opened_root = os.fstat(root_fd)
            if not stat.S_ISDIR(opened_root.st_mode) or not _same_file(root_stat, opened_root):
                raise InvalidSnapshotError(f"build-source snapshot root changed while opening: {path}")
            root_stat = opened_root
        scan = _scan_snapshot(path, root_fd)
        current_root = _validated_root_stat(path)
        if not _same_file(root_stat, current_root):
            raise InvalidSnapshotError(f"build-source snapshot root changed while scanning: {path}")
        return FrozenSnapshot(path, root_stat, root_fd, scan)
    except BaseException:
        if root_fd is not None:
            os.close(root_fd)
        raise


def _validated_root_stat(path: Path) -> os.stat_result:
    try:
        root_stat = os.lstat(path)
    except OSError as exc:
        raise InvalidSnapshotError(f"build-source snapshot root is unavailable: {path}") from exc
    if _is_link_or_reparse(root_stat):
        raise InvalidSnapshotError(f"build-source snapshot root must not be a link: {path}")
    if not stat.S_ISDIR(root_stat.st_mode):
        raise InvalidSnapshotError(f"build-source snapshot root is not a directory: {path}")
    return root_stat


def _open_root_fd(path: Path) -> int | None:
    if (
        os.name != "posix"
        or not hasattr(os, "O_DIRECTORY")
        or not hasattr(os, "O_NOFOLLOW")
        or os.scandir not in os.supports_fd
    ):
        return None
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
    flags |= getattr(os, "O_CLOEXEC", 0)
    try:
        return os.open(path, flags)
    except OSError as exc:
        raise InvalidSnapshotError(f"cannot retain build-source snapshot root: {path}") from exc


def _scan_snapshot(path: Path, root_fd: int | None) -> _SnapshotScan:
    paths = _PathRegistry()
    directories: list[_StateEntry] = []
    files: list[_DiscoveredFile] = []
    if root_fd is None:
        _collect_path_entries(path, path, "", paths, directories, files)
    else:
        _collect_fd_entries(path, root_fd, "", paths, directories, files)

    hasher = hashing._BuildSourceHasher()
    file_states: list[_StateEntry] = []
    for record in sorted(files, key=lambda item: item.path_bytes):
        file_states.append(_hash_file(path, root_fd, record, hasher))
    state = tuple(sorted((*directories, *file_states), key=lambda item: (item.path_bytes, item.kind)))
    return _SnapshotScan(
        identity=BuildSourceIdentity(
            algorithm=hashing.BUILD_SOURCE_ALGORITHM,
            content_sha256=hasher.content_sha256(),
        ),
        state=state,
    )


def _collect_fd_entries(
    root: Path,
    directory_fd: int,
    prefix: str,
    paths: _PathRegistry,
    directories: list[_StateEntry],
    files: list[_DiscoveredFile],
) -> None:
    try:
        with os.scandir(directory_fd) as iterator:
            names = [entry.name for entry in iterator]
    except OSError as exc:
        raise InvalidSnapshotError(f"cannot list build-source snapshot directory: {prefix or '.'}") from exc

    for name in names:
        relative = f"{prefix}/{name}" if prefix else name
        path_bytes = paths.add(relative)
        try:
            entry_stat = os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
        except OSError as exc:
            raise InvalidSnapshotError(f"cannot inspect build-source path: {relative}") from exc
        if _is_link_or_reparse(entry_stat):
            raise InvalidSnapshotError(f"link forbidden in build-source snapshot: {relative}")
        if stat.S_ISDIR(entry_stat.st_mode):
            directories.append(_StateEntry(path_bytes=path_bytes, kind="D"))
            child_fd = _open_directory_at(directory_fd, name, relative)
            try:
                opened_stat = os.fstat(child_fd)
                if not stat.S_ISDIR(opened_stat.st_mode) or not _same_file(entry_stat, opened_stat):
                    raise InvalidSnapshotError(
                        f"build-source directory changed while opening: {relative}"
                    )
                _collect_fd_entries(root, child_fd, relative, paths, directories, files)
            finally:
                os.close(child_fd)
        elif stat.S_ISREG(entry_stat.st_mode):
            if entry_stat.st_size < 0:
                raise InvalidSnapshotError(f"negative file size in build-source snapshot: {relative}")
            files.append(
                _DiscoveredFile(
                    path=relative,
                    path_bytes=path_bytes,
                    size=entry_stat.st_size,
                    file_key=_file_key(entry_stat),
                    absolute_path=root.joinpath(*relative.split("/")),
                )
            )
        else:
            raise InvalidSnapshotError(f"special file forbidden in build-source snapshot: {relative}")


def _collect_path_entries(
    root: Path,
    directory: Path,
    prefix: str,
    paths: _PathRegistry,
    directories: list[_StateEntry],
    files: list[_DiscoveredFile],
) -> None:
    try:
        with os.scandir(directory) as iterator:
            entries = list(iterator)
    except OSError as exc:
        raise InvalidSnapshotError(f"cannot list build-source snapshot directory: {prefix or '.'}") from exc

    for entry in entries:
        relative = f"{prefix}/{entry.name}" if prefix else entry.name
        path_bytes = paths.add(relative)
        try:
            entry_stat = entry.stat(follow_symlinks=False)
        except OSError as exc:
            raise InvalidSnapshotError(f"cannot inspect build-source path: {relative}") from exc
        if _is_link_or_reparse(entry_stat):
            raise InvalidSnapshotError(f"link forbidden in build-source snapshot: {relative}")
        absolute_path = root.joinpath(*relative.split("/"))
        if stat.S_ISDIR(entry_stat.st_mode):
            directories.append(_StateEntry(path_bytes=path_bytes, kind="D"))
            try:
                opened_stat = os.lstat(absolute_path)
            except OSError as exc:
                raise InvalidSnapshotError(
                    f"cannot open build-source snapshot directory: {relative}"
                ) from exc
            if _is_link_or_reparse(opened_stat) or not _same_file(entry_stat, opened_stat):
                raise InvalidSnapshotError(f"build-source directory changed while opening: {relative}")
            _collect_path_entries(root, absolute_path, relative, paths, directories, files)
        elif stat.S_ISREG(entry_stat.st_mode):
            if entry_stat.st_size < 0:
                raise InvalidSnapshotError(f"negative file size in build-source snapshot: {relative}")
            files.append(
                _DiscoveredFile(
                    path=relative,
                    path_bytes=path_bytes,
                    size=entry_stat.st_size,
                    file_key=_file_key(entry_stat),
                    absolute_path=absolute_path,
                )
            )
        else:
            raise InvalidSnapshotError(f"special file forbidden in build-source snapshot: {relative}")


def _open_directory_at(parent_fd: int, name: str, relative: str) -> int:
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
    flags |= getattr(os, "O_CLOEXEC", 0)
    try:
        return os.open(name, flags, dir_fd=parent_fd)
    except OSError as exc:
        raise InvalidSnapshotError(f"cannot open build-source snapshot directory: {relative}") from exc


def _hash_file(
    root: Path,
    root_fd: int | None,
    record: _DiscoveredFile,
    hasher: hashing._BuildSourceHasher,
) -> _StateEntry:
    if root_fd is None:
        return _hash_path_file(record, hasher)
    return _hash_fd_file(root, root_fd, record, hasher)


def _hash_fd_file(
    root: Path,
    root_fd: int,
    record: _DiscoveredFile,
    hasher: hashing._BuildSourceHasher,
) -> _StateEntry:
    del root
    file_fd = _open_file_below_root(root_fd, record.path)
    try:
        opened_stat = os.fstat(file_fd)
        if (
            not stat.S_ISREG(opened_stat.st_mode)
            or _file_key(opened_stat) != record.file_key
            or opened_stat.st_size != record.size
        ):
            raise InvalidSnapshotError(f"build-source file changed while opening: {record.path}")
        content_digest = hashlib.sha256()
        hasher.add_file(
            record.path,
            record.size,
            _fd_chunks(file_fd, record.size, record.path, content_digest),
        )
        after_stat = os.fstat(file_fd)
        if (
            not stat.S_ISREG(after_stat.st_mode)
            or _file_key(after_stat) != record.file_key
            or after_stat.st_size != record.size
        ):
            raise InvalidSnapshotError(f"build-source file changed while reading: {record.path}")
        return _StateEntry(
            path_bytes=record.path_bytes,
            kind="F",
            size=record.size,
            content_sha256=content_digest.digest(),
        )
    finally:
        os.close(file_fd)


def _hash_path_file(
    record: _DiscoveredFile,
    hasher: hashing._BuildSourceHasher,
) -> _StateEntry:
    try:
        before_stat = os.lstat(record.absolute_path)
    except OSError as exc:
        raise InvalidSnapshotError(f"cannot inspect build-source file: {record.path}") from exc
    if (
        _is_link_or_reparse(before_stat)
        or not stat.S_ISREG(before_stat.st_mode)
        or _file_key(before_stat) != record.file_key
        or before_stat.st_size != record.size
    ):
        raise InvalidSnapshotError(f"build-source file changed while opening: {record.path}")

    content_digest = hashlib.sha256()
    try:
        with record.absolute_path.open("rb", buffering=0) as file:
            opened_stat = os.fstat(file.fileno())
            if (
                not stat.S_ISREG(opened_stat.st_mode)
                or _file_key(opened_stat) != record.file_key
                or opened_stat.st_size != record.size
            ):
                raise InvalidSnapshotError(f"build-source file changed while opening: {record.path}")
            hasher.add_file(
                record.path,
                record.size,
                _file_chunks(file, record.size, record.path, content_digest),
            )
            after_stat = os.fstat(file.fileno())
            if (
                not stat.S_ISREG(after_stat.st_mode)
                or _file_key(after_stat) != record.file_key
                or after_stat.st_size != record.size
            ):
                raise InvalidSnapshotError(f"build-source file changed while reading: {record.path}")
    except OSError as exc:
        raise InvalidSnapshotError(f"cannot read build-source file: {record.path}") from exc
    return _StateEntry(
        path_bytes=record.path_bytes,
        kind="F",
        size=record.size,
        content_sha256=content_digest.digest(),
    )


def _open_file_below_root(root_fd: int, relative: str) -> int:
    parts = relative.split("/")
    directory_fd = os.dup(root_fd)
    try:
        for index, component in enumerate(parts[:-1]):
            child_fd = _open_directory_at(directory_fd, component, "/".join(parts[: index + 1]))
            os.close(directory_fd)
            directory_fd = child_fd
        flags = os.O_RDONLY | os.O_NOFOLLOW
        flags |= getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_BINARY", 0)
        try:
            return os.open(parts[-1], flags, dir_fd=directory_fd)
        except OSError as exc:
            raise InvalidSnapshotError(f"cannot open build-source file: {relative}") from exc
    finally:
        os.close(directory_fd)


def _fd_chunks(
    file_fd: int,
    size: int,
    relative: str,
    content_digest: _Digest,
) -> Iterator[bytes]:
    remaining = size
    while remaining:
        try:
            chunk = os.read(file_fd, min(remaining, _READ_CHUNK_SIZE))
        except OSError as exc:
            raise InvalidSnapshotError(f"cannot read build-source file: {relative}") from exc
        if not chunk:
            raise InvalidSnapshotError(f"build-source file shrank while reading: {relative}")
        remaining -= len(chunk)
        content_digest.update(chunk)
        yield chunk
    try:
        extra = os.read(file_fd, 1)
    except OSError as exc:
        raise InvalidSnapshotError(f"cannot finish reading build-source file: {relative}") from exc
    if extra:
        raise InvalidSnapshotError(f"build-source file grew while reading: {relative}")


def _file_chunks(
    file: BinaryIO,
    size: int,
    relative: str,
    content_digest: _Digest,
) -> Iterator[bytes]:
    remaining = size
    while remaining:
        chunk = file.read(min(remaining, _READ_CHUNK_SIZE))
        if not chunk:
            raise InvalidSnapshotError(f"build-source file shrank while reading: {relative}")
        remaining -= len(chunk)
        content_digest.update(chunk)
        yield chunk
    if file.read(1):
        raise InvalidSnapshotError(f"build-source file grew while reading: {relative}")


def _same_file(left: os.stat_result, right: os.stat_result) -> bool:
    return _file_key(left) == _file_key(right)


def _file_key(value: os.stat_result) -> tuple[int, int]:
    return value.st_dev, value.st_ino


def _is_link_or_reparse(value: os.stat_result) -> bool:
    if stat.S_ISLNK(value.st_mode):
        return True
    attributes = getattr(value, "st_file_attributes", 0)
    return bool(attributes & _REPARSE_ATTRIBUTE)


class _PathRegistry:
    def __init__(self) -> None:
        self._encoded: set[bytes] = set()
        self._platform: dict[str, str] = {}

    def add(self, path: str) -> bytes:
        if not is_valid_portable_path(path):
            raise InvalidSnapshotError(f"non-portable path in build-source snapshot: {path!r}")
        try:
            path_bytes = path.encode("utf-8", errors="strict")
        except UnicodeEncodeError as exc:
            raise InvalidSnapshotError(f"invalid Unicode in build-source path: {path!r}") from exc
        if path_bytes in self._encoded:
            raise InvalidSnapshotError(f"duplicate path in build-source snapshot: {path!r}")
        self._encoded.add(path_bytes)
        platform_key = unicodedata.normalize(
            "NFD",
            unicodedata.normalize("NFD", path).casefold(),
        )
        previous = self._platform.get(platform_key)
        if previous is not None and previous != path:
            raise InvalidSnapshotError(
                f"platform path collision in build-source snapshot: {previous!r} and {path!r}"
            )
        self._platform[platform_key] = path
        return path_bytes
