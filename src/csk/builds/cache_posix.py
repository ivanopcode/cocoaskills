"""Rooted no-follow POSIX storage for immutable csk build-cache entries.

The live physical layout is deliberately csk-specific and non-portable::

    <manager-home>/builds/go-v1/<64-hex-key>/
        csk-receipt.ccj.json
        bin/<command>

Protocol callers never provide those paths. They provide only a complete
logical build input, an optional receipt hash, and a private artifact source.
Publication staging and quarantine live in separate manager-home namespaces so
an entry appears below ``builds`` only as one complete atomic directory.
"""

from __future__ import annotations

import ctypes
import errno
import hashlib
import math
import os
import secrets
import stat
import sys
import threading
import time
from collections.abc import Callable, Collection, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Final

from . import metadata as _metadata
from .cache import (
    BuildCacheError,
    CacheCollectionResult,
    CacheConflictError,
    CacheEntryStatus,
    CacheExpectation,
    CacheInspection,
    CacheMutationGuard,
    CachePublication,
    CachePublicationResult,
    CachePublicationStatus,
)

LIVE_ROOT_NAME: Final = "builds"
STAGING_ROOT_NAME: Final = ".builds-staging"
QUARANTINE_ROOT_NAME: Final = ".builds-quarantine"
RECEIPT_FILENAME: Final = "csk-receipt.ccj.json"

_DRIVER_DIRECTORY: Final = _metadata.GO_V1_DRIVER
_MAX_RECEIPT_BYTES: Final = 1 << 20
_DIRECTORY_PRIVATE_MODE: Final = 0o700
_DIRECTORY_IMMUTABLE_MODE: Final = 0o500
_RECEIPT_MODE: Final = 0o400
_ARTIFACT_MODE: Final = 0o500
_READ_CHUNK: Final = 128 * 1024
_RENAME_NOREPLACE_LINUX: Final = 0x1
_RENAME_EXCL_DARWIN: Final = 0x00000004
_AT_EMPTY_PATH_LINUX: Final = 0x1000
_FCHMODAT2_LINUX: Final = 452


class _MissingState(Exception):
    pass


class _UntrustedState(Exception):
    pass


class _CorruptState(Exception):
    pass


@dataclass(frozen=True)
class _VerifiedEntry:
    receipt: _metadata.BuildReceipt
    receipt_bytes: bytes
    receipt_sha256: str
    artifact_sha256: str
    artifact_size: int


class PosixBuildCache:
    """Protected immutable cache rooted below one trusted manager home."""

    def __init__(self, manager_home: str | os.PathLike[str]):
        raw = os.fspath(manager_home)
        if not raw:
            raise BuildCacheError("cache_home_invalid", "manager home is empty")
        if not os.path.isabs(raw):
            raise BuildCacheError(
                "cache_home_invalid",
                "manager home must be an absolute path",
            )
        if os.path.normpath(raw) != raw:
            raise BuildCacheError(
                "cache_home_invalid",
                "manager home must be a clean absolute path",
            )
        self._manager_home = Path(raw)
        self._supported = _protection_supported()
        # The caller-held manager-home lock is the cross-process authority.
        # This local lock also makes multiple threads sharing one backend
        # instance honor that same publication critical section even when a
        # test or embedding guard is only a witness.
        self._publication_lock = threading.RLock()

    @property
    def manager_home(self) -> Path:
        return self._manager_home

    def inspect(self, expectation: CacheExpectation) -> CacheInspection:
        """Validate one candidate using rooted no-follow access only."""

        if not self._supported:
            return CacheInspection(
                status=CacheEntryStatus.UNSUPPORTED,
                reason="required POSIX protection primitives are unavailable",
            )
        try:
            key = _metadata.cache_key(expectation.input)
            artifact_name = _artifact_name(expectation.input)
            with _open_manager_home(self._manager_home) as home_fd:  # noqa: SIM117
                with _open_private_directory_at(
                    home_fd,
                    LIVE_ROOT_NAME,
                    "build cache root",
                    missing=_MissingState("build cache root is absent"),
                ) as builds_fd:
                    with _open_private_directory_at(
                        builds_fd,
                        _DRIVER_DIRECTORY,
                        "build driver cache",
                        missing=_MissingState("build driver cache is absent"),
                    ) as driver_fd:
                        verified = _inspect_entry_at(
                            driver_fd,
                            _key_component(key),
                            expectation,
                            key,
                            artifact_name,
                        )
            return CacheInspection(
                status=CacheEntryStatus.HIT,
                reason="protected cache entry exactly matches expected state",
                receipt=verified.receipt,
                receipt_bytes=verified.receipt_bytes,
                receipt_sha256=verified.receipt_sha256,
                artifact_path=self._artifact_path(key, expectation.input),
            )
        except _MissingState as exc:
            return CacheInspection(
                status=CacheEntryStatus.MISS,
                reason=str(exc),
            )
        except _UntrustedState as exc:
            return CacheInspection(
                status=CacheEntryStatus.UNTRUSTED_PROVENANCE,
                reason=str(exc),
            )
        except (_CorruptState, _metadata.BuildMetadataError, ValueError) as exc:
            return CacheInspection(
                status=CacheEntryStatus.CORRUPT,
                reason=str(exc),
            )
        except OSError as exc:
            return CacheInspection(
                status=CacheEntryStatus.UNTRUSTED_PROVENANCE,
                reason=f"protected cache inspection failed: {exc}",
            )

    def publish(
        self,
        publication: CachePublication,
        *,
        guard: CacheMutationGuard,
    ) -> CachePublicationResult:
        with self._publication_lock:
            return self._publish_locked(publication, guard=guard)

    def _publish_locked(
        self,
        publication: CachePublication,
        *,
        guard: CacheMutationGuard,
    ) -> CachePublicationResult:
        """Publish one complete immutable directory with no replacement."""

        _require_guard(guard)
        if not self._supported:
            raise BuildCacheError(
                "cache_protection_unsupported",
                "required POSIX protection primitives are unavailable",
            )
        if len(publication.receipt_bytes) > _MAX_RECEIPT_BYTES:
            raise BuildCacheError(
                "cache_publication_invalid",
                "publication receipt exceeds the supported size",
            )
        try:
            key = _metadata.cache_key(publication.input)
            receipt = _metadata.verify_receipt(
                publication.receipt_bytes,
                expected_input=publication.input,
                expected_cache_key=key,
            )
            receipt_hash = _metadata.receipt_sha256(publication.receipt_bytes)
            artifact_name = _artifact_name(publication.input)
        except (_metadata.BuildMetadataError, ValueError) as exc:
            raise BuildCacheError(
                "cache_publication_invalid",
                f"publication receipt or input is invalid: {exc}",
            ) from exc

        with _open_publication_source(publication.artifact_source) as (
            source_fd,
            source_state,
        ):
            source_hash, source_size = _hash_file(
                source_fd,
                expected_size=receipt.artifact.size,
                label="publication artifact",
                error_factory=_publication_error,
            )
            if (
                source_hash != receipt.artifact.sha256
                or source_size != receipt.artifact.size
            ):
                raise BuildCacheError(
                    "cache_publication_invalid",
                    "publication artifact does not match its canonical receipt",
                )
            os.lseek(source_fd, 0, os.SEEK_SET)

            _require_guard(guard)
            try:
                with _open_manager_home(self._manager_home) as home_fd:  # noqa: SIM117
                    with _open_or_replace_auxiliary_root(
                        home_fd,
                        STAGING_ROOT_NAME,
                    ) as staging_fd:
                        stage_name = _create_stage_name(staging_fd)
                        stage_exists = True
                        try:
                            _write_staged_entry(
                                staging_fd,
                                stage_name,
                                artifact_name,
                                publication.receipt_bytes,
                                source_fd,
                                source_state,
                            )
                            staged = _inspect_entry_at(
                                staging_fd,
                                stage_name,
                                CacheExpectation(
                                    input=publication.input,
                                    receipt_sha256=receipt_hash,
                                ),
                                key,
                                artifact_name,
                                entry_immutable=False,
                            )
                            if (
                                staged.receipt_bytes != publication.receipt_bytes
                                or staged.artifact_sha256 != source_hash
                                or staged.artifact_size != source_size
                            ):
                                raise _CorruptState(
                                    "staged entry differs from the verified publication"
                                )
                            _validate_publication_source_state(
                                source_fd,
                                source_state,
                            )
                            _require_guard(guard)

                            with _open_live_driver_for_publish(
                                home_fd,
                                guard,
                            ) as (driver_fd, quarantine_fd):
                                for _attempt in range(8):
                                    _require_guard(guard)
                                    try:
                                        winner = _inspect_entry_at(
                                            driver_fd,
                                            _key_component(key),
                                            CacheExpectation(
                                                input=publication.input,
                                            ),
                                            key,
                                            artifact_name,
                                        )
                                    except _MissingState:
                                        winner = None
                                    except _UntrustedState:
                                        # Darwin's atomic RENAME_EXCL requires
                                        # the complete source directory to
                                        # remain owner-writable until rename.
                                        # A competing publisher can therefore
                                        # expose mode 0700 for the few
                                        # instructions before it seals 0500.
                                        # Never parse or adopt that state;
                                        # retry it briefly, then quarantine a
                                        # persistent untrusted candidate.
                                        if _attempt < 7:
                                            time.sleep(0.005)
                                            continue
                                        _require_guard(guard)
                                        moved = _move_aside(
                                            driver_fd,
                                            _key_component(key),
                                            quarantine_fd,
                                            f"entry-{_key_component(key)}",
                                            missing_ok=True,
                                        )
                                        if moved is None:
                                            continue
                                        winner = None
                                    except _CorruptState:
                                        _require_guard(guard)
                                        moved = _move_aside(
                                            driver_fd,
                                            _key_component(key),
                                            quarantine_fd,
                                            f"entry-{_key_component(key)}",
                                            missing_ok=True,
                                        )
                                        if moved is None:
                                            continue
                                        winner = None

                                    if winner is not None:
                                        if (
                                            winner.receipt_bytes
                                            == publication.receipt_bytes
                                            and _artifact_files_equal(
                                                staging_fd,
                                                stage_name,
                                                driver_fd,
                                                _key_component(key),
                                                artifact_name,
                                                first_entry_immutable=False,
                                            )
                                        ):
                                            _remove_stage(
                                                staging_fd,
                                                stage_name,
                                            )
                                            stage_exists = False
                                            return CachePublicationResult(
                                                status=CachePublicationStatus.REUSED_WINNER,
                                                artifact_path=self._artifact_path(
                                                    key,
                                                    publication.input,
                                                ),
                                                receipt_sha256=winner.receipt_sha256,
                                            )
                                        raise CacheConflictError(key)

                                    published_entry_fd = _open_staged_entry(
                                        staging_fd,
                                        stage_name,
                                    )
                                    try:
                                        ready = _inspect_open_entry(
                                            published_entry_fd,
                                            CacheExpectation(
                                                input=publication.input,
                                                receipt_sha256=receipt_hash,
                                            ),
                                            key,
                                            artifact_name,
                                        )
                                        if (
                                            ready.receipt_bytes
                                            != publication.receipt_bytes
                                            or ready.artifact_sha256 != source_hash
                                            or ready.artifact_size != source_size
                                        ):
                                            raise _CorruptState(
                                                "staged entry changed before publication"
                                            )
                                        _validate_publication_source_state(
                                            source_fd,
                                            source_state,
                                        )
                                        _require_guard(guard)
                                        try:
                                            _rename_noreplace(
                                                staging_fd,
                                                stage_name,
                                                driver_fd,
                                                _key_component(key),
                                            )
                                        except OSError as exc:
                                            if exc.errno in {
                                                errno.EEXIST,
                                                errno.ENOTEMPTY,
                                            }:
                                                continue
                                            raise
                                        stage_exists = False
                                        _seal_published_entry(
                                            published_entry_fd,
                                        )
                                        os.fsync(driver_fd)
                                        os.fsync(staging_fd)
                                        published = _inspect_open_entry(
                                            published_entry_fd,
                                            CacheExpectation(
                                                input=publication.input,
                                                receipt_sha256=receipt_hash,
                                            ),
                                            key,
                                            artifact_name,
                                        )
                                        if (
                                            published.receipt_bytes
                                            != publication.receipt_bytes
                                            or published.artifact_sha256
                                            != source_hash
                                            or published.artifact_size
                                            != source_size
                                        ):
                                            raise _CorruptState(
                                                "published entry differs from staged bytes"
                                            )
                                        status, selected = _resolve_atomic_winner(
                                            driver_fd,
                                            _key_component(key),
                                            published_entry_fd,
                                            published,
                                            CacheExpectation(
                                                input=publication.input,
                                                receipt_sha256=receipt_hash,
                                            ),
                                            key,
                                            artifact_name,
                                            guard,
                                        )
                                        return CachePublicationResult(
                                            status=status,
                                            artifact_path=self._artifact_path(
                                                key,
                                                publication.input,
                                            ),
                                            receipt_sha256=selected.receipt_sha256,
                                        )
                                    finally:
                                        os.close(published_entry_fd)
                                raise BuildCacheError(
                                    "cache_publication_race",
                                    "could not select or validate an atomic cache winner",
                                )
                        finally:
                            if stage_exists:
                                try:
                                    _remove_stage(staging_fd, stage_name)
                                except OSError:
                                    pass
            except CacheConflictError:
                raise
            except BuildCacheError:
                raise
            except _UntrustedState as exc:
                raise BuildCacheError(
                    "cache_boundary_untrusted",
                    str(exc),
                ) from exc
            except (_CorruptState, _metadata.BuildMetadataError) as exc:
                raise BuildCacheError(
                    "cache_publication_invalid",
                    str(exc),
                ) from exc
            except _MissingState as exc:
                raise BuildCacheError(
                    "cache_boundary_missing",
                    str(exc),
                ) from exc
            except OSError as exc:
                raise BuildCacheError(
                    "cache_publication_failed",
                    str(exc),
                ) from exc

        raise AssertionError("unreachable cache publication path")

    def quarantine(
        self,
        cache_key: str,
        *,
        guard: CacheMutationGuard,
    ) -> Path | None:
        """Atomically move a live entry outside ``builds`` without traversing it."""

        _require_guard(guard)
        if not self._supported:
            raise BuildCacheError(
                "cache_protection_unsupported",
                "required POSIX protection primitives are unavailable",
            )
        component = _key_component(cache_key)
        try:
            with _open_manager_home(self._manager_home) as home_fd:  # noqa: SIM117
                with _open_or_replace_auxiliary_root(
                    home_fd,
                    QUARANTINE_ROOT_NAME,
                ) as quarantine_fd:
                    try:
                        with _open_private_directory_at(
                            home_fd,
                            LIVE_ROOT_NAME,
                            "build cache root",
                            missing=_MissingState("build cache root is absent"),
                        ) as builds_fd, _open_private_directory_at(
                            builds_fd,
                            _DRIVER_DIRECTORY,
                            "build driver cache",
                            missing=_MissingState(
                                "build driver cache is absent"
                            ),
                        ) as driver_fd:
                            _require_guard(guard)
                            moved = _move_aside(
                                driver_fd,
                                component,
                                quarantine_fd,
                                f"entry-{component}",
                                missing_ok=True,
                            )
                    except _MissingState:
                        return None
            if moved is None:
                return None
            return self._manager_home / QUARANTINE_ROOT_NAME / moved
        except BuildCacheError:
            raise
        except _UntrustedState as exc:
            raise BuildCacheError("cache_boundary_untrusted", str(exc)) from exc
        except OSError as exc:
            raise BuildCacheError("cache_quarantine_failed", str(exc)) from exc

    def collect(
        self,
        referenced_cache_keys: Collection[str],
        *,
        older_than: float,
        guard: CacheMutationGuard,
    ) -> CacheCollectionResult:
        """Sweep only complete protected entries older than ``older_than``."""

        _require_guard(guard)
        if (
            isinstance(older_than, bool)
            or not isinstance(older_than, (int, float))
            or not math.isfinite(older_than)
        ):
            raise BuildCacheError(
                "cache_gc_invalid",
                "build-cache GC cutoff must be a finite timestamp",
            )
        if not self._supported:
            return CacheCollectionResult(
                warnings=(
                    "build cache retained: required POSIX protection primitives are unavailable",
                )
            )
        referenced = {_key_component(key) for key in referenced_cache_keys}
        removed = 0
        warnings: list[str] = []
        try:
            with _open_manager_home(self._manager_home) as home_fd:
                try:
                    with _open_private_directory_at(
                        home_fd,
                        LIVE_ROOT_NAME,
                        "build cache root",
                        missing=_MissingState("build cache root is absent"),
                    ) as builds_fd:
                        with _open_private_directory_at(
                            builds_fd,
                            _DRIVER_DIRECTORY,
                            "build driver cache",
                            missing=_MissingState(
                                "build driver cache is absent"
                            ),
                        ) as driver_fd:
                            names = _directory_names(
                                driver_fd,
                                "build driver cache",
                            )
                            with _open_or_replace_auxiliary_root(
                                home_fd,
                                QUARANTINE_ROOT_NAME,
                            ) as quarantine_fd:
                                for component in names:
                                    if not _is_key_component(component):
                                        warnings.append(
                                            "build cache retained unknown entry "
                                            f"{component!r}"
                                        )
                                        continue
                                    if component in referenced:
                                        continue
                                    try:
                                        with _open_private_directory_at(
                                            driver_fd,
                                            component,
                                            "cache entry",
                                            missing=_MissingState(
                                                "cache entry is absent"
                                            ),
                                            immutable=True,
                                        ) as entry_fd:
                                            before = os.fstat(entry_fd)
                                            if before.st_mtime >= older_than:
                                                continue
                                            _inspect_gc_entry(
                                                entry_fd,
                                                f"sha256:{component}",
                                            )
                                            after = os.fstat(entry_fd)
                                            if not _stable_directory_state(
                                                before,
                                                after,
                                            ):
                                                raise _UntrustedState(
                                                    "cache entry changed during GC inspection"
                                                )
                                            _require_guard(guard)
                                            moved = _move_aside(
                                                driver_fd,
                                                component,
                                                quarantine_fd,
                                                f"gc-entry-{component}",
                                                missing_ok=True,
                                                expected_state=after,
                                            )
                                    except _MissingState:
                                        continue
                                    except (
                                        _UntrustedState,
                                        _CorruptState,
                                        _metadata.BuildMetadataError,
                                        BuildCacheError,
                                        OSError,
                                        ValueError,
                                    ) as exc:
                                        warnings.append(
                                            "build cache retained uncertain entry "
                                            f"sha256:{component}: {exc}"
                                        )
                                        continue
                                    if moved is None:
                                        continue
                                    removed += 1
                                    try:
                                        _remove_stage(quarantine_fd, moved)
                                    except OSError as exc:
                                        warnings.append(
                                            "swept build entry remains in quarantine "
                                            f"{moved}: {exc}"
                                        )
                except _MissingState:
                    return CacheCollectionResult()
        except _MissingState:
            return CacheCollectionResult()
        except (_UntrustedState, OSError) as exc:
            return CacheCollectionResult(
                warnings=(
                    f"build cache retained because its protected boundary is uncertain: {exc}",
                )
            )
        return CacheCollectionResult(
            removed=removed,
            warnings=tuple(warnings),
        )

    def _artifact_path(self, key: str, build_input: _metadata.GoBuildInput) -> Path:
        return (
            self._manager_home
            / LIVE_ROOT_NAME
            / _DRIVER_DIRECTORY
            / _key_component(key)
            / Path(*build_input.artifact_path.split("/"))
        )


def _protection_supported() -> bool:
    if os.name != "posix":
        return False
    required_flags = all(
        hasattr(os, name)
        for name in (
            "O_CLOEXEC",
            "O_DIRECTORY",
            "O_NOFOLLOW",
        )
    )
    required_calls = (
        os.open in os.supports_dir_fd
        and os.mkdir in os.supports_dir_fd
        and os.rename in os.supports_dir_fd
        and os.listdir in os.supports_fd
    )
    return (
        required_flags
        and required_calls
        and hasattr(os, "geteuid")
        and _rename_noreplace_available()
    )


def _rename_noreplace_available() -> bool:
    library = ctypes.CDLL(None)
    if sys.platform == "darwin":
        return hasattr(library, "renameatx_np")
    if sys.platform.startswith("linux"):
        return hasattr(library, "renameat2")
    return False


def _effective_uid() -> int:
    try:
        return os.geteuid()
    except AttributeError as exc:
        raise _UntrustedState(
            "effective user identity is unavailable"
        ) from exc


def _artifact_name(build_input: _metadata.GoBuildInput) -> str:
    parts = build_input.artifact_path.split("/")
    if len(parts) != 2 or parts[0] != "bin" or not parts[1]:
        raise ValueError("manager-derived artifact path is not a direct bin child")
    if parts[1] in {".", ".."} or "/" in parts[1] or os.sep in parts[1]:
        raise ValueError("manager-derived artifact filename is unsafe")
    return parts[1]


def _key_component(cache_key: str) -> str:
    prefix = "sha256:"
    if (
        not isinstance(cache_key, str)
        or not cache_key.startswith(prefix)
        or len(cache_key) != len(prefix) + 64
    ):
        raise BuildCacheError("cache_key_invalid", "logical cache key is malformed")
    component = cache_key.removeprefix(prefix)
    if any(character not in "0123456789abcdef" for character in component):
        raise BuildCacheError("cache_key_invalid", "logical cache key is malformed")
    return component


def _is_key_component(value: str) -> bool:
    return (
        len(value) == 64
        and all(character in "0123456789abcdef" for character in value)
    )


def _require_guard(guard: CacheMutationGuard) -> None:
    if guard is None:
        raise BuildCacheError(
            "cache_lock_required",
            "caller-held manager-home mutation lock is required",
        )
    try:
        guard.assert_held()
    except Exception as exc:
        raise BuildCacheError(
            "cache_lock_required",
            f"manager-home mutation lock is not held: {exc}",
        ) from exc


def _directory_flags() -> int:
    return (
        os.O_RDONLY
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_DIRECTORY", 0)
        | getattr(os, "O_NOFOLLOW", 0)
    )


def _file_flags() -> int:
    return (
        os.O_RDONLY
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_NOFOLLOW", 0)
        | getattr(os, "O_NONBLOCK", 0)
        | getattr(os, "O_NOATIME", 0)
    )


@contextmanager
def _open_manager_home(path: Path) -> Iterator[int]:
    try:
        descriptor = os.open(path, _directory_flags())
    except FileNotFoundError as exc:
        raise _MissingState("manager home is absent") from exc
    except OSError as exc:
        raise _UntrustedState(
            f"cannot open manager home without following links: {exc}"
        ) from exc
    try:
        _validate_manager_home(descriptor)
        yield descriptor
    finally:
        os.close(descriptor)


@contextmanager
def _open_private_directory_at(
    parent_fd: int,
    name: str,
    label: str,
    *,
    missing: Exception,
    immutable: bool = False,
) -> Iterator[int]:
    try:
        descriptor = os.open(
            name,
            _directory_flags(),
            dir_fd=parent_fd,
        )
    except FileNotFoundError as exc:
        raise missing from exc
    except OSError as exc:
        raise _UntrustedState(
            f"cannot open {label} without following links: {exc}"
        ) from exc
    try:
        expected = (
            _DIRECTORY_IMMUTABLE_MODE if immutable else _DIRECTORY_PRIVATE_MODE
        )
        _validate_directory(descriptor, label, expected)
        yield descriptor
    finally:
        os.close(descriptor)


@contextmanager
def _open_protected_file_at(
    parent_fd: int,
    name: str,
    label: str,
    *,
    expected_mode: int,
) -> Iterator[int]:
    flags = _file_flags()
    try:
        descriptor = os.open(name, flags, dir_fd=parent_fd)
    except PermissionError:
        no_atime = getattr(os, "O_NOATIME", 0)
        if not no_atime:
            raise
        try:
            descriptor = os.open(
                name,
                flags & ~no_atime,
                dir_fd=parent_fd,
            )
        except FileNotFoundError as exc:
            raise _CorruptState(f"{label} is absent") from exc
        except OSError as exc:
            raise _UntrustedState(
                f"cannot open {label} without following links: {exc}"
            ) from exc
    except FileNotFoundError as exc:
        raise _CorruptState(f"{label} is absent") from exc
    except OSError as exc:
        raise _UntrustedState(
            f"cannot open {label} without following links: {exc}"
        ) from exc
    try:
        _validate_regular_file(
            descriptor,
            label,
            expected_mode=expected_mode,
        )
        yield descriptor
    finally:
        os.close(descriptor)


def _validate_manager_home(descriptor: int) -> None:
    info = os.fstat(descriptor)
    if not stat.S_ISDIR(info.st_mode):
        raise _UntrustedState("manager home is not a directory")
    if info.st_uid != _effective_uid():
        raise _UntrustedState(
            "manager home owner does not match the effective user"
        )
    mode = stat.S_IMODE(info.st_mode)
    if mode & 0o022:
        raise _UntrustedState("manager home is writable by group or other")
    if mode & 0o700 != 0o700:
        raise _UntrustedState("manager home does not grant owner control")


def _validate_directory(descriptor: int, label: str, expected_mode: int) -> None:
    info = os.fstat(descriptor)
    if not stat.S_ISDIR(info.st_mode):
        raise _UntrustedState(f"{label} is not a directory")
    if info.st_uid != _effective_uid():
        raise _UntrustedState(
            f"{label} owner does not match the effective user"
        )
    mode = stat.S_IMODE(info.st_mode)
    if mode != expected_mode:
        raise _UntrustedState(
            f"{label} mode {mode:o} is not protected mode {expected_mode:o}"
        )


def _validate_regular_file(
    descriptor: int,
    label: str,
    *,
    expected_mode: int,
) -> os.stat_result:
    info = os.fstat(descriptor)
    if not stat.S_ISREG(info.st_mode):
        raise _UntrustedState(f"{label} is not a regular file")
    if info.st_nlink != 1:
        raise _UntrustedState(f"{label} is not singly linked")
    if info.st_uid != _effective_uid():
        raise _UntrustedState(
            f"{label} owner does not match the effective user"
        )
    mode = stat.S_IMODE(info.st_mode)
    if mode != expected_mode:
        raise _UntrustedState(
            f"{label} mode {mode:o} is not immutable mode {expected_mode:o}"
        )
    return info


def _inspect_entry_at(
    parent_fd: int,
    entry_name: str,
    expectation: CacheExpectation,
    key: str,
    artifact_name: str,
    *,
    entry_immutable: bool = True,
) -> _VerifiedEntry:
    with _open_private_directory_at(
        parent_fd,
        entry_name,
        "cache entry",
        missing=_MissingState("cache entry is absent"),
        immutable=entry_immutable,
    ) as entry_fd:
        return _inspect_open_entry(
            entry_fd,
            expectation,
            key,
            artifact_name,
        )


def _inspect_open_entry(
    entry_fd: int,
    expectation: CacheExpectation,
    key: str,
    artifact_name: str,
) -> _VerifiedEntry:
    names = _directory_names(entry_fd, "cache entry")
    if names != ["bin", RECEIPT_FILENAME]:
        raise _CorruptState("cache entry has unexpected contents")
    with _open_protected_file_at(
        entry_fd,
        RECEIPT_FILENAME,
        "cache receipt",
        expected_mode=_RECEIPT_MODE,
    ) as receipt_fd, _open_private_directory_at(
        entry_fd,
        "bin",
        "artifact directory",
        missing=_CorruptState("artifact directory is absent"),
        immutable=True,
    ) as bin_fd:
        artifact_names = _directory_names(
            bin_fd,
            "artifact directory",
        )
        if artifact_names != [artifact_name]:
            raise _CorruptState(
                "artifact directory has unexpected contents"
            )
        with _open_protected_file_at(
            bin_fd,
            artifact_name,
            "cache artifact",
            expected_mode=_ARTIFACT_MODE,
        ) as artifact_fd:
            receipt_bytes = _read_bounded_file(
                receipt_fd,
                _MAX_RECEIPT_BYTES,
                "cache receipt",
            )
            try:
                receipt = _metadata.verify_receipt(
                    receipt_bytes,
                    expected_input=expectation.input,
                    expected_cache_key=key,
                    expected_receipt_sha256=expectation.receipt_sha256,
                )
            except _metadata.BuildMetadataError as exc:
                raise _CorruptState(
                    f"cache receipt is invalid: {exc}"
                ) from exc
            artifact_hash, artifact_size = _hash_file(
                artifact_fd,
                expected_size=receipt.artifact.size,
                label="cache artifact",
                error_factory=_CorruptState,
            )
            if artifact_hash != receipt.artifact.sha256:
                raise _CorruptState("cache artifact hash does not match")
            return _VerifiedEntry(
                receipt=receipt,
                receipt_bytes=receipt_bytes,
                receipt_sha256=_metadata.receipt_sha256(
                    receipt_bytes
                ),
                artifact_sha256=artifact_hash,
                artifact_size=artifact_size,
            )


def _inspect_gc_entry(entry_fd: int, key: str) -> _VerifiedEntry:
    names = _directory_names(entry_fd, "cache entry")
    if names != ["bin", RECEIPT_FILENAME]:
        raise _CorruptState("cache entry has unexpected contents")
    with _open_protected_file_at(
        entry_fd,
        RECEIPT_FILENAME,
        "cache receipt",
        expected_mode=_RECEIPT_MODE,
    ) as receipt_fd:
        receipt_bytes = _read_bounded_file(
            receipt_fd,
            _MAX_RECEIPT_BYTES,
            "cache receipt",
        )
    receipt = _metadata.read_receipt(receipt_bytes)
    if _metadata.cache_key(receipt.input) != key or receipt.cache_key != key:
        raise _CorruptState(
            "cache directory name does not match its canonical receipt input"
        )
    return _inspect_open_entry(
        entry_fd,
        CacheExpectation(
            input=receipt.input,
            receipt_sha256=_metadata.receipt_sha256(receipt_bytes),
        ),
        key,
        _artifact_name(receipt.input),
    )


def _directory_names(descriptor: int, label: str) -> list[str]:
    try:
        return sorted(os.listdir(descriptor))
    except OSError as exc:
        raise _UntrustedState(f"cannot list {label}: {exc}") from exc


def _read_bounded_file(
    descriptor: int,
    limit: int,
    label: str,
) -> bytes:
    before = os.fstat(descriptor)
    if before.st_size < 0 or before.st_size > limit:
        raise _CorruptState(f"{label} size is outside the supported range")
    os.lseek(descriptor, 0, os.SEEK_SET)
    remaining = before.st_size
    chunks: list[bytes] = []
    while remaining:
        chunk = os.read(descriptor, min(_READ_CHUNK, remaining))
        if not chunk:
            raise _CorruptState(f"{label} changed while reading")
        chunks.append(chunk)
        remaining -= len(chunk)
    if os.read(descriptor, 1):
        raise _CorruptState(f"{label} grew while reading")
    after = os.fstat(descriptor)
    if not _stable_file_state(before, after):
        raise _CorruptState(f"{label} changed while reading")
    return b"".join(chunks)


def _hash_file(
    descriptor: int,
    *,
    expected_size: int,
    label: str,
    error_factory: Callable[[str], Exception],
) -> tuple[str, int]:
    before = os.fstat(descriptor)
    if before.st_size != expected_size:
        raise error_factory(f"{label} size does not match the receipt")
    os.lseek(descriptor, 0, os.SEEK_SET)
    remaining = expected_size
    digest = hashlib.sha256()
    while remaining:
        chunk = os.read(descriptor, min(_READ_CHUNK, remaining))
        if not chunk:
            raise error_factory(f"{label} changed while reading")
        digest.update(chunk)
        remaining -= len(chunk)
    if os.read(descriptor, 1):
        raise error_factory(f"{label} grew while reading")
    after = os.fstat(descriptor)
    if not _stable_file_state(before, after):
        raise error_factory(f"{label} changed while reading")
    return f"sha256:{digest.hexdigest()}", expected_size


def _publication_error(detail: str) -> BuildCacheError:
    return BuildCacheError("cache_publication_invalid", detail)


def _stable_file_state(
    before: os.stat_result,
    after: os.stat_result,
) -> bool:
    return (
        before.st_dev,
        before.st_ino,
        before.st_mode,
        before.st_uid,
        before.st_nlink,
        before.st_size,
        before.st_mtime_ns,
        before.st_ctime_ns,
    ) == (
        after.st_dev,
        after.st_ino,
        after.st_mode,
        after.st_uid,
        after.st_nlink,
        after.st_size,
        after.st_mtime_ns,
        after.st_ctime_ns,
    )


def _stable_directory_state(
    before: os.stat_result,
    after: os.stat_result,
) -> bool:
    return (
        before.st_dev,
        before.st_ino,
        before.st_mode,
        before.st_uid,
        before.st_nlink,
        before.st_mtime_ns,
        before.st_ctime_ns,
    ) == (
        after.st_dev,
        after.st_ino,
        after.st_mode,
        after.st_uid,
        after.st_nlink,
        after.st_mtime_ns,
        after.st_ctime_ns,
    )


@contextmanager
def _open_publication_source(path: Path) -> Iterator[tuple[int, os.stat_result]]:
    raw = os.fspath(path)
    if not raw or not os.path.isabs(raw) or os.path.normpath(raw) != raw:
        raise BuildCacheError(
            "cache_publication_invalid",
            "publication artifact source must be a clean absolute path",
        )
    try:
        path_state = os.lstat(path)
    except OSError as exc:
        raise BuildCacheError(
            "cache_publication_invalid",
            f"cannot inspect publication artifact source: {exc}",
        ) from exc
    if stat.S_ISLNK(path_state.st_mode) or not stat.S_ISREG(path_state.st_mode):
        raise BuildCacheError(
            "cache_publication_invalid",
            "publication artifact source must be a regular non-link file",
        )
    try:
        try:
            descriptor = os.open(path, _file_flags())
        except PermissionError:
            no_atime = getattr(os, "O_NOATIME", 0)
            if not no_atime:
                raise
            descriptor = os.open(path, _file_flags() & ~no_atime)
    except OSError as exc:
        raise BuildCacheError(
            "cache_publication_invalid",
            f"cannot open publication artifact source: {exc}",
        ) from exc
    try:
        opened = os.fstat(descriptor)
        if (
            not stat.S_ISREG(opened.st_mode)
            or path_state.st_dev != opened.st_dev
            or path_state.st_ino != opened.st_ino
            or opened.st_nlink != 1
            or opened.st_uid != _effective_uid()
            or stat.S_IMODE(opened.st_mode) & 0o022
        ):
            raise BuildCacheError(
                "cache_publication_invalid",
                "publication artifact source is not private, singly linked, "
                "owner-controlled regular state",
            )
        yield descriptor, opened
    finally:
        os.close(descriptor)


def _validate_publication_source_state(
    descriptor: int,
    expected: os.stat_result,
) -> None:
    current = os.fstat(descriptor)
    if not _stable_file_state(expected, current):
        raise BuildCacheError(
            "cache_publication_invalid",
            "publication artifact source changed during staging",
        )


@contextmanager
def _open_or_replace_auxiliary_root(
    home_fd: int,
    name: str,
) -> Iterator[int]:
    with _open_or_create_private_directory(
        home_fd,
        name,
        replacement_parent_fd=home_fd,
        replacement_prefix=f"{name.removeprefix('.')}-untrusted",
    ) as descriptor:
        yield descriptor


@contextmanager
def _open_live_driver_for_publish(
    home_fd: int,
    guard: CacheMutationGuard,
) -> Iterator[tuple[int, int]]:
    with _open_or_replace_auxiliary_root(
        home_fd,
        QUARANTINE_ROOT_NAME,
    ) as quarantine_fd:
        _require_guard(guard)
        with _open_or_create_private_directory(
            home_fd,
            LIVE_ROOT_NAME,
            replacement_parent_fd=quarantine_fd,
            replacement_prefix="boundary-builds",
        ) as builds_fd:
            _require_guard(guard)
            with _open_or_create_private_directory(
                builds_fd,
                _DRIVER_DIRECTORY,
                replacement_parent_fd=quarantine_fd,
                replacement_prefix="boundary-go-v1",
            ) as driver_fd:
                yield driver_fd, quarantine_fd


@contextmanager
def _open_or_create_private_directory(
    parent_fd: int,
    name: str,
    *,
    replacement_parent_fd: int,
    replacement_prefix: str,
) -> Iterator[int]:
    for _attempt in range(8):
        try:
            descriptor = os.open(
                name,
                _directory_flags(),
                dir_fd=parent_fd,
            )
        except FileNotFoundError:
            try:
                os.mkdir(
                    name,
                    mode=_DIRECTORY_PRIVATE_MODE,
                    dir_fd=parent_fd,
                )
            except FileExistsError:
                continue
            continue
        except OSError:
            _move_aside(
                parent_fd,
                name,
                replacement_parent_fd,
                replacement_prefix,
            )
            continue
        try:
            _validate_directory(
                descriptor,
                name,
                _DIRECTORY_PRIVATE_MODE,
            )
        except _UntrustedState:
            os.close(descriptor)
            _move_aside(
                parent_fd,
                name,
                replacement_parent_fd,
                replacement_prefix,
            )
            continue
        try:
            yield descriptor
        finally:
            os.close(descriptor)
        return
    raise _UntrustedState(
        f"could not establish fresh protected directory {name!r}"
    )


def _create_stage_name(staging_fd: int) -> str:
    for _attempt in range(16):
        name = f"entry-{secrets.token_hex(16)}"
        try:
            os.mkdir(
                name,
                mode=_DIRECTORY_PRIVATE_MODE,
                dir_fd=staging_fd,
            )
        except FileExistsError:
            continue
        return name
    raise OSError(errno.EEXIST, "could not allocate unique staging directory")


def _open_staged_entry(staging_fd: int, stage_name: str) -> int:
    try:
        descriptor = os.open(
            stage_name,
            _directory_flags(),
            dir_fd=staging_fd,
        )
    except FileNotFoundError as exc:
        raise _CorruptState("staging entry disappeared") from exc
    except OSError as exc:
        raise _UntrustedState(
            f"cannot open staging entry without following links: {exc}"
        ) from exc
    try:
        _validate_directory(
            descriptor,
            "staging entry",
            _DIRECTORY_PRIVATE_MODE,
        )
    except BaseException:
        os.close(descriptor)
        raise
    return descriptor


def _write_staged_entry(
    staging_fd: int,
    stage_name: str,
    artifact_name: str,
    receipt_bytes: bytes,
    source_fd: int,
    source_state: os.stat_result,
) -> None:
    with _open_private_directory_at(
        staging_fd,
        stage_name,
        "staging entry",
        missing=_CorruptState("staging entry disappeared"),
    ) as entry_fd:
        os.mkdir("bin", mode=_DIRECTORY_PRIVATE_MODE, dir_fd=entry_fd)
        with _open_private_directory_at(
            entry_fd,
            "bin",
            "staged artifact directory",
            missing=_CorruptState("staged artifact directory disappeared"),
        ) as bin_fd:
            receipt_fd = _create_file_at(
                entry_fd,
                RECEIPT_FILENAME,
                _DIRECTORY_PRIVATE_MODE,
            )
            try:
                _write_all(receipt_fd, receipt_bytes)
                os.fsync(receipt_fd)
                os.fchmod(receipt_fd, _RECEIPT_MODE)
                os.fsync(receipt_fd)
            finally:
                os.close(receipt_fd)

            artifact_fd = _create_file_at(
                bin_fd,
                artifact_name,
                _DIRECTORY_PRIVATE_MODE,
            )
            try:
                os.lseek(source_fd, 0, os.SEEK_SET)
                remaining = source_state.st_size
                while remaining:
                    chunk = os.read(source_fd, min(_READ_CHUNK, remaining))
                    if not chunk:
                        raise BuildCacheError(
                            "cache_publication_invalid",
                            "publication artifact changed while staging",
                        )
                    _write_all(artifact_fd, chunk)
                    remaining -= len(chunk)
                if os.read(source_fd, 1):
                    raise BuildCacheError(
                        "cache_publication_invalid",
                        "publication artifact grew while staging",
                    )
                os.fsync(artifact_fd)
                os.fchmod(artifact_fd, _ARTIFACT_MODE)
                os.fsync(artifact_fd)
            finally:
                os.close(artifact_fd)

            _validate_publication_source_state(source_fd, source_state)
            os.fchmod(bin_fd, _DIRECTORY_IMMUTABLE_MODE)
            os.fsync(bin_fd)
        # Keep the complete private stage owner-writable until its atomic
        # no-replace rename. Darwin rejects RENAME_EXCL for a 0500 source
        # directory. The winner is sealed immediately after rename while the
        # caller still holds the manager-home mutation lock.
        os.fsync(entry_fd)
    os.fsync(staging_fd)


def _create_file_at(parent_fd: int, name: str, mode: int) -> int:
    flags = (
        os.O_WRONLY
        | os.O_CREAT
        | os.O_EXCL
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_NOFOLLOW", 0)
    )
    return os.open(name, flags, mode, dir_fd=parent_fd)


def _write_all(descriptor: int, raw: bytes) -> None:
    view = memoryview(raw)
    while view:
        written = os.write(descriptor, view)
        if written <= 0:
            raise OSError(errno.EIO, "short write")
        view = view[written:]


def _seal_published_entry(entry_fd: int) -> None:
    """Seal the exact staged directory that won the atomic rename."""

    _validate_directory(
        entry_fd,
        "newly published cache entry",
        _DIRECTORY_PRIVATE_MODE,
    )
    os.fchmod(entry_fd, _DIRECTORY_IMMUTABLE_MODE)
    os.fsync(entry_fd)
    _validate_directory(
        entry_fd,
        "newly published cache entry",
        _DIRECTORY_IMMUTABLE_MODE,
    )


def _resolve_atomic_winner(
    driver_fd: int,
    entry_name: str,
    published_entry_fd: int,
    published: _VerifiedEntry,
    expectation: CacheExpectation,
    key: str,
    artifact_name: str,
    guard: CacheMutationGuard,
) -> tuple[CachePublicationStatus, _VerifiedEntry]:
    """Resolve the live name without ever mutating through that name.

    A competing publisher may quarantine the private-mode directory after its
    atomic rename but before this publisher seals it. The retained descriptor
    keeps this publisher's identity stable. If the live name now selects a
    different complete entry, compare that pinned winner byte-for-byte.
    """

    published_state = os.fstat(published_entry_fd)
    for attempt in range(8):
        _require_guard(guard)
        try:
            with _open_private_directory_at(
                driver_fd,
                entry_name,
                "selected cache winner",
                missing=_MissingState("published cache winner is absent"),
                immutable=True,
            ) as selected_entry_fd:
                selected = _inspect_open_entry(
                    selected_entry_fd,
                    CacheExpectation(input=expectation.input),
                    key,
                    artifact_name,
                )
                if _same_directory_identity(
                    published_state,
                    os.fstat(selected_entry_fd),
                ):
                    if (
                        selected.receipt_bytes != published.receipt_bytes
                        or selected.artifact_sha256
                        != published.artifact_sha256
                        or selected.artifact_size != published.artifact_size
                    ):
                        raise _CorruptState(
                            "selected published entry changed after sealing"
                        )
                    return CachePublicationStatus.PUBLISHED, selected
                if (
                    selected.receipt_bytes == published.receipt_bytes
                    and _artifact_open_entries_equal(
                        published_entry_fd,
                        selected_entry_fd,
                        artifact_name,
                    )
                ):
                    return CachePublicationStatus.REUSED_WINNER, selected
                raise CacheConflictError(key)
        except (_MissingState, _UntrustedState):
            if attempt == 7:
                raise
            time.sleep(0.005)
    raise AssertionError("unreachable atomic winner resolution")


def _move_aside(
    source_parent_fd: int,
    source_name: str,
    destination_parent_fd: int,
    prefix: str,
    *,
    missing_ok: bool = False,
    expected_state: os.stat_result | None = None,
) -> str | None:
    try:
        source_state = os.stat(
            source_name,
            dir_fd=source_parent_fd,
            follow_symlinks=False,
        )
    except FileNotFoundError:
        if missing_ok:
            return None
        raise _MissingState(f"{source_name} disappeared before quarantine")
    if expected_state is not None and not _stable_directory_state(
        expected_state,
        source_state,
    ):
        raise _UntrustedState(
            "cache entry pathname no longer selects the inspected object"
        )
    is_directory = stat.S_ISDIR(source_state.st_mode)
    try:
        unlocked_fd, original_mode = _unlock_owned_directory_for_move(
            source_parent_fd,
            source_name,
            source_state,
        )
    except FileNotFoundError:
        if missing_ok:
            return None
        raise _MissingState(f"{source_name} disappeared before quarantine")

    moved_name: str | None = None
    try:
        for _attempt in range(16):
            destination_name = f"{prefix}-{secrets.token_hex(16)}"
            try:
                _reserve_move_destination(
                    destination_parent_fd,
                    destination_name,
                    is_directory=is_directory,
                )
            except FileExistsError:
                continue
            reservation_exists = True
            try:
                os.rename(
                    source_name,
                    destination_name,
                    src_dir_fd=source_parent_fd,
                    dst_dir_fd=destination_parent_fd,
                )
                reservation_exists = False
                moved_name = destination_name
            except FileNotFoundError:
                if missing_ok:
                    return None
                raise _MissingState(
                    f"{source_name} disappeared before quarantine"
                )
            except OSError as exc:
                if exc.errno in {
                    errno.EEXIST,
                    errno.EISDIR,
                    errno.ENOTDIR,
                    errno.ENOTEMPTY,
                }:
                    continue
                raise
            finally:
                if reservation_exists:
                    _remove_move_reservation(
                        destination_parent_fd,
                        destination_name,
                        is_directory=is_directory,
                    )
            break
        if moved_name is None:
            raise OSError(
                errno.EEXIST,
                "could not allocate unique quarantine name",
            )
    finally:
        _restore_moved_directory_mode(unlocked_fd, original_mode)

    if expected_state is not None:
        assert moved_name is not None
        try:
            moved_state = os.stat(
                moved_name,
                dir_fd=destination_parent_fd,
                follow_symlinks=False,
            )
        except OSError as exc:
            raise _UntrustedState(
                "cannot prove quarantined cache entry identity"
            ) from exc
        if not _same_gc_retirement_state(expected_state, moved_state):
            try:
                _rename_noreplace(
                    destination_parent_fd,
                    moved_name,
                    source_parent_fd,
                    source_name,
                )
                os.fsync(source_parent_fd)
                if source_parent_fd != destination_parent_fd:
                    os.fsync(destination_parent_fd)
            except OSError as exc:
                raise _UntrustedState(
                    "cache entry changed during quarantine; replacement was retained "
                    "outside the live namespace because it could not be restored"
                ) from exc
            raise _UntrustedState(
                "cache entry changed during quarantine; replacement was restored"
            )

    os.fsync(source_parent_fd)
    if source_parent_fd != destination_parent_fd:
        os.fsync(destination_parent_fd)
    return moved_name


def _same_gc_retirement_state(
    expected: os.stat_result,
    actual: os.stat_result,
) -> bool:
    """Compare the classified object after rename, excluding rename ctime."""

    return (
        stat.S_ISDIR(actual.st_mode)
        and actual.st_dev == expected.st_dev
        and actual.st_ino == expected.st_ino
        and actual.st_uid == expected.st_uid
        and actual.st_nlink == expected.st_nlink
        and actual.st_mode == expected.st_mode
        and actual.st_mtime_ns == expected.st_mtime_ns
    )


def _unlock_owned_directory_for_move(
    parent_fd: int,
    name: str,
    source_state: os.stat_result,
) -> tuple[int | None, int | None]:
    """Temporarily grant owner rwx needed to move a verified directory."""

    if (
        not stat.S_ISDIR(source_state.st_mode)
        or source_state.st_uid != _effective_uid()
    ):
        return None, None
    original_mode = stat.S_IMODE(source_state.st_mode)
    if (original_mode & 0o700) == 0o700:
        return None, None
    for _attempt in range(16):
        try:
            descriptor = os.open(
                name,
                _directory_flags(),
                dir_fd=parent_fd,
            )
        except InterruptedError:
            continue
        except PermissionError as exc:
            if exc.errno not in {errno.EACCES, errno.EPERM}:
                raise
            return _open_inaccessible_owned_directory_for_move(
                parent_fd,
                name,
                source_state,
            )
        break
    else:
        raise OSError(
            errno.EINTR,
            "could not open cache candidate after repeated interruption",
        )
    current = os.fstat(descriptor)
    if not _same_directory_identity(source_state, current):
        os.close(descriptor)
        raise OSError(errno.ESTALE, "cache candidate changed before quarantine")
    if stat.S_IMODE(current.st_mode) != original_mode:
        os.close(descriptor)
        raise OSError(
            errno.ESTALE,
            "cache candidate mode changed before quarantine",
        )
    changed = False
    try:
        os.fchmod(descriptor, original_mode | 0o700)
        changed = True
        os.fsync(descriptor)
    except BaseException:
        try:
            if changed:
                os.fchmod(descriptor, original_mode)
                os.fsync(descriptor)
        finally:
            os.close(descriptor)
        raise
    return descriptor, original_mode


def _open_inaccessible_owned_directory_for_move(
    parent_fd: int,
    name: str,
    source_state: os.stat_result,
) -> tuple[int, int]:
    if sys.platform.startswith("linux") and hasattr(os, "O_PATH"):
        return _open_linux_directory_for_move(
            parent_fd,
            name,
            source_state,
        )
    if (
        sys.platform == "darwin"
        and os.chmod in os.supports_dir_fd
        and os.chmod in os.supports_follow_symlinks
    ):
        return _open_darwin_directory_for_move(
            parent_fd,
            name,
            source_state,
        )
    raise BuildCacheError(
        "cache_protection_unsupported",
        "cannot safely acquire an inaccessible cache directory for quarantine",
    )


def _open_linux_directory_for_move(
    parent_fd: int,
    name: str,
    source_state: os.stat_result,
) -> tuple[int, int]:
    original_mode = stat.S_IMODE(source_state.st_mode)
    reference_fd = os.open(
        name,
        _directory_reference_flags_linux(),
        dir_fd=parent_fd,
    )
    changed = False
    try:
        current = os.fstat(reference_fd)
        if (
            not _same_directory_identity(source_state, current)
            or stat.S_IMODE(current.st_mode) != original_mode
        ):
            raise OSError(
                errno.ESTALE,
                "cache candidate changed before quarantine",
            )
        _fchmod_opath_linux(reference_fd, original_mode | 0o700)
        changed = True
        try:
            descriptor = os.open(
                name,
                _directory_flags(),
                dir_fd=parent_fd,
            )
        except BaseException:
            _fchmod_opath_linux(reference_fd, original_mode)
            changed = False
            raise
        opened = os.fstat(descriptor)
        if not _same_directory_identity(source_state, opened):
            try:
                _fchmod_opath_linux(reference_fd, original_mode)
            finally:
                os.close(descriptor)
            changed = False
            raise OSError(
                errno.ESTALE,
                "cache candidate changed before quarantine",
            )
        try:
            os.fsync(descriptor)
        except BaseException:
            try:
                os.fchmod(descriptor, original_mode)
                os.fsync(descriptor)
            finally:
                os.close(descriptor)
            changed = False
            raise
        changed = False
        return descriptor, original_mode
    finally:
        try:
            if changed:
                _fchmod_opath_linux(reference_fd, original_mode)
        finally:
            os.close(reference_fd)


def _open_darwin_directory_for_move(
    parent_fd: int,
    name: str,
    source_state: os.stat_result,
) -> tuple[int, int]:
    original_mode = stat.S_IMODE(source_state.st_mode)
    before = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
    if (
        not _same_directory_identity(source_state, before)
        or stat.S_IMODE(before.st_mode) != original_mode
    ):
        raise OSError(errno.ESTALE, "cache candidate changed before quarantine")
    try:
        os.chmod(
            name,
            original_mode | 0o700,
            dir_fd=parent_fd,
            follow_symlinks=False,
        )
    except (NotImplementedError, ValueError) as exc:
        raise BuildCacheError(
            "cache_protection_unsupported",
            "rooted no-follow directory chmod is unavailable",
        ) from exc
    changed = True
    try:
        descriptor = os.open(
            name,
            _directory_flags(),
            dir_fd=parent_fd,
        )
    except BaseException:
        try:
            os.chmod(
                name,
                original_mode,
                dir_fd=parent_fd,
                follow_symlinks=False,
            )
        except (NotImplementedError, ValueError) as restore_exc:
            raise BuildCacheError(
                "cache_protection_unsupported",
                "rooted no-follow directory mode restoration is unavailable",
            ) from restore_exc
        raise
    try:
        current = os.fstat(descriptor)
        if not _same_directory_identity(source_state, current):
            raise OSError(
                errno.ESTALE,
                "cache candidate changed before quarantine",
            )
        os.fsync(descriptor)
        changed = False
    finally:
        if changed:
            try:
                os.fchmod(descriptor, original_mode)
                os.fsync(descriptor)
            finally:
                os.close(descriptor)
    return descriptor, original_mode


def _same_directory_identity(
    expected: os.stat_result,
    actual: os.stat_result,
) -> bool:
    return (
        stat.S_ISDIR(actual.st_mode)
        and actual.st_dev == expected.st_dev
        and actual.st_ino == expected.st_ino
        and actual.st_uid == expected.st_uid
    )


def _directory_reference_flags_linux() -> int:
    path_flag = getattr(os, "O_PATH", 0)
    if not path_flag:
        raise BuildCacheError(
            "cache_protection_unsupported",
            "Linux O_PATH directory references are unavailable",
        )
    return (
        path_flag
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_DIRECTORY", 0)
        | getattr(os, "O_NOFOLLOW", 0)
    )


def _fchmod_opath_linux(descriptor: int, mode: int) -> None:
    machine = os.uname().machine.lower()
    if machine not in {"aarch64", "arm64", "amd64", "x86_64"}:
        raise BuildCacheError(
            "cache_protection_unsupported",
            f"Linux fchmodat2 is not mapped for architecture {machine!r}",
        )
    library = ctypes.CDLL(None, use_errno=True)
    syscall = library.syscall
    syscall.restype = ctypes.c_long
    ctypes.set_errno(0)
    result = syscall(
        ctypes.c_long(_FCHMODAT2_LINUX),
        ctypes.c_int(descriptor),
        ctypes.c_char_p(b""),
        ctypes.c_uint(mode),
        ctypes.c_int(_AT_EMPTY_PATH_LINUX),
    )
    if result == 0:
        return
    error_number = ctypes.get_errno()
    if error_number in {
        errno.EBADF,
        errno.EINVAL,
        errno.ENOSYS,
        errno.ENOTSUP,
        errno.EOPNOTSUPP,
    }:
        raise BuildCacheError(
            "cache_protection_unsupported",
            "Linux fchmodat2 with AT_EMPTY_PATH is unavailable",
        )
    raise OSError(error_number, os.strerror(error_number))


def _restore_moved_directory_mode(
    descriptor: int | None,
    original_mode: int | None,
) -> None:
    if descriptor is None:
        return
    try:
        if original_mode is not None:
            os.fchmod(descriptor, original_mode)
            os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _reserve_move_destination(
    parent_fd: int,
    name: str,
    *,
    is_directory: bool,
) -> None:
    """Reserve a unique same-kind target for an atomic locked rename.

    POSIX rename atomically replaces an empty directory with a directory, or a
    non-directory placeholder with a non-directory source. Reserving first
    prevents any existing quarantined state from being overwritten, while
    standard rename moves the verified, temporarily owner-controlled candidate
    across the auxiliary boundary.
    """

    if is_directory:
        os.mkdir(
            name,
            mode=_DIRECTORY_PRIVATE_MODE,
            dir_fd=parent_fd,
        )
        return
    descriptor = _create_file_at(
        parent_fd,
        name,
        _DIRECTORY_PRIVATE_MODE,
    )
    os.close(descriptor)


def _remove_move_reservation(
    parent_fd: int,
    name: str,
    *,
    is_directory: bool,
) -> None:
    try:
        if is_directory:
            os.rmdir(name, dir_fd=parent_fd)
        else:
            os.unlink(name, dir_fd=parent_fd)
    except FileNotFoundError:
        pass


def _remove_stage(staging_fd: int, stage_name: str) -> None:
    try:
        entry_fd = os.open(
            stage_name,
            _directory_flags(),
            dir_fd=staging_fd,
        )
    except FileNotFoundError:
        return
    try:
        os.fchmod(entry_fd, _DIRECTORY_PRIVATE_MODE)
        names = set(os.listdir(entry_fd))
        if "bin" in names:
            bin_fd = os.open("bin", _directory_flags(), dir_fd=entry_fd)
            try:
                os.fchmod(bin_fd, _DIRECTORY_PRIVATE_MODE)
                for name in os.listdir(bin_fd):
                    os.unlink(name, dir_fd=bin_fd)
            finally:
                os.close(bin_fd)
            os.rmdir("bin", dir_fd=entry_fd)
            names.remove("bin")
        for name in names:
            os.unlink(name, dir_fd=entry_fd)
    finally:
        os.close(entry_fd)
    os.rmdir(stage_name, dir_fd=staging_fd)
    os.fsync(staging_fd)


def _artifact_files_equal(
    first_parent_fd: int,
    first_entry_name: str,
    second_parent_fd: int,
    second_entry_name: str,
    artifact_name: str,
    *,
    first_entry_immutable: bool = True,
) -> bool:
    with _open_artifact_at(
        first_parent_fd,
        first_entry_name,
        artifact_name,
        entry_immutable=first_entry_immutable,
    ) as first_fd, _open_artifact_at(
        second_parent_fd,
        second_entry_name,
        artifact_name,
    ) as second_fd:
        return _open_files_equal(first_fd, second_fd)


def _artifact_open_entries_equal(
    first_entry_fd: int,
    second_entry_fd: int,
    artifact_name: str,
) -> bool:
    with _open_artifact_in_entry(
        first_entry_fd,
        artifact_name,
    ) as first_fd, _open_artifact_in_entry(
        second_entry_fd,
        artifact_name,
    ) as second_fd:
        return _open_files_equal(first_fd, second_fd)


def _open_files_equal(first_fd: int, second_fd: int) -> bool:
    first_state = os.fstat(first_fd)
    second_state = os.fstat(second_fd)
    if first_state.st_size != second_state.st_size:
        return False
    os.lseek(first_fd, 0, os.SEEK_SET)
    os.lseek(second_fd, 0, os.SEEK_SET)
    while True:
        first = os.read(first_fd, _READ_CHUNK)
        second = os.read(second_fd, _READ_CHUNK)
        if first != second:
            return False
        if not first:
            break
    return (
        _stable_file_state(first_state, os.fstat(first_fd))
        and _stable_file_state(second_state, os.fstat(second_fd))
    )


@contextmanager
def _open_artifact_at(
    parent_fd: int,
    entry_name: str,
    artifact_name: str,
    *,
    entry_immutable: bool = True,
) -> Iterator[int]:
    with _open_private_directory_at(
        parent_fd,
        entry_name,
        "cache entry",
        missing=_MissingState("cache entry is absent"),
        immutable=entry_immutable,
    ) as entry_fd, _open_artifact_in_entry(
        entry_fd,
        artifact_name,
    ) as artifact_fd:
        yield artifact_fd


@contextmanager
def _open_artifact_in_entry(
    entry_fd: int,
    artifact_name: str,
) -> Iterator[int]:
    with _open_private_directory_at(
        entry_fd,
        "bin",
        "artifact directory",
        missing=_CorruptState("artifact directory is absent"),
        immutable=True,
    ) as bin_fd, _open_protected_file_at(
        bin_fd,
        artifact_name,
        "cache artifact",
        expected_mode=_ARTIFACT_MODE,
    ) as artifact_fd:
        yield artifact_fd


def _rename_noreplace(
    source_dir_fd: int,
    source_name: str,
    destination_dir_fd: int,
    destination_name: str,
) -> None:
    source = os.fsencode(source_name)
    destination = os.fsencode(destination_name)
    library = ctypes.CDLL(None, use_errno=True)
    ctypes.set_errno(0)
    if sys.platform == "darwin":
        rename = library.renameatx_np
        rename.argtypes = [
            ctypes.c_int,
            ctypes.c_char_p,
            ctypes.c_int,
            ctypes.c_char_p,
            ctypes.c_uint,
        ]
        rename.restype = ctypes.c_int
        result = rename(
            source_dir_fd,
            source,
            destination_dir_fd,
            destination,
            _RENAME_EXCL_DARWIN,
        )
    elif sys.platform.startswith("linux"):
        rename = library.renameat2
        rename.argtypes = [
            ctypes.c_int,
            ctypes.c_char_p,
            ctypes.c_int,
            ctypes.c_char_p,
            ctypes.c_uint,
        ]
        rename.restype = ctypes.c_int
        result = rename(
            source_dir_fd,
            source,
            destination_dir_fd,
            destination,
            _RENAME_NOREPLACE_LINUX,
        )
    else:
        raise OSError(
            errno.ENOTSUP,
            "atomic no-replace directory rename is unavailable",
        )
    if result != 0:
        error_number = ctypes.get_errno()
        raise OSError(
            error_number,
            os.strerror(error_number),
            destination_name,
        )


__all__ = [
    "LIVE_ROOT_NAME",
    "QUARANTINE_ROOT_NAME",
    "RECEIPT_FILENAME",
    "STAGING_ROOT_NAME",
    "PosixBuildCache",
]
