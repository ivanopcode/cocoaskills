"""Handle-validated Windows storage for immutable csk build-cache entries.

The logical layout matches the POSIX backend, but every Windows object is
opened with reparse processing disabled and checked through its handle::

    <manager-home>/builds/go-v1/<64-hex-key>/
        csk-receipt.ccj.json
        bin/<command>.exe

The live, staging, and quarantine roots have protected DACLs. A sealed entry
has a protected read/execute DACL for the manager principal and full control
only for SYSTEM and the built-in Administrators group. Publication seals the
complete stage before a same-volume no-replace ``MoveFileExW``.

This module deliberately resolves Win32 functions lazily so importing it is
safe on non-Windows hosts.
"""

from __future__ import annotations

import ctypes
import errno
import hashlib
import math
import ntpath
import os
import secrets
import threading
import time
from collections.abc import Callable, Collection, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Any, Final

from . import metadata as _metadata
from ._windows import named_data_streams
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

_DRIVER_DIRECTORY: Final[str] = str(_metadata.GO_V1_DRIVER)
_MAX_RECEIPT_BYTES: Final = 1 << 20
_READ_CHUNK: Final = 128 * 1024

_GENERIC_READ: Final = 0x80000000
_GENERIC_WRITE: Final = 0x40000000
_GENERIC_EXECUTE: Final = 0x20000000
_GENERIC_ALL: Final = 0x10000000
_DELETE: Final = 0x00010000
_READ_CONTROL: Final = 0x00020000
_WRITE_DAC: Final = 0x00040000
_WRITE_OWNER: Final = 0x00080000
_SYNCHRONIZE: Final = 0x00100000

_FILE_READ_DATA: Final = 0x0001
_FILE_WRITE_DATA: Final = 0x0002
_FILE_APPEND_DATA: Final = 0x0004
_FILE_READ_EA: Final = 0x0008
_FILE_WRITE_EA: Final = 0x0010
_FILE_EXECUTE: Final = 0x0020
_FILE_DELETE_CHILD: Final = 0x0040
_FILE_READ_ATTRIBUTES: Final = 0x0080
_FILE_WRITE_ATTRIBUTES: Final = 0x0100
_FILE_ALL_ACCESS: Final = 0x001F01FF
_FILE_GENERIC_READ: Final = (
    _READ_CONTROL
    | _FILE_READ_DATA
    | _FILE_READ_ATTRIBUTES
    | _FILE_READ_EA
    | _SYNCHRONIZE
)
_FILE_GENERIC_EXECUTE: Final = (
    _READ_CONTROL | _FILE_READ_ATTRIBUTES | _FILE_EXECUTE | _SYNCHRONIZE
)

_FILE_SHARE_READ: Final = 0x00000001
_FILE_SHARE_WRITE: Final = 0x00000002
_FILE_SHARE_DELETE: Final = 0x00000004
_CREATE_NEW: Final = 1
_OPEN_EXISTING: Final = 3
_FILE_ATTRIBUTE_READONLY: Final = 0x00000001
_FILE_ATTRIBUTE_NORMAL: Final = 0x00000080
_FILE_ATTRIBUTE_REPARSE_POINT: Final = 0x00000400
_FILE_FLAG_BACKUP_SEMANTICS: Final = 0x02000000
_FILE_FLAG_OPEN_REPARSE_POINT: Final = 0x00200000
_FILE_TYPE_DISK: Final = 0x0001
_MOVEFILE_WRITE_THROUGH: Final = 0x00000008
_FILE_BEGIN: Final = 0

_FILE_BASIC_INFO_CLASS: Final = 0
_FILE_STANDARD_INFO_CLASS: Final = 1
_FILE_ATTRIBUTE_TAG_INFO_CLASS: Final = 9
_FILE_ID_INFO_CLASS: Final = 18
_FILE_RENAME_INFORMATION_CLASS: Final = 10

_TOKEN_QUERY: Final = 0x0008
_TOKEN_USER_CLASS: Final = 1
_ERROR_SUCCESS: Final = 0
_ERROR_FILE_NOT_FOUND: Final = 2
_ERROR_PATH_NOT_FOUND: Final = 3
_ERROR_ACCESS_DENIED: Final = 5
_ERROR_INVALID_HANDLE: Final = 6
_ERROR_ALREADY_EXISTS: Final = 183
_ERROR_FILE_EXISTS: Final = 80
_ERROR_INSUFFICIENT_BUFFER: Final = 122

_SE_FILE_OBJECT: Final = 1
_OWNER_SECURITY_INFORMATION: Final = 0x00000001
_DACL_SECURITY_INFORMATION: Final = 0x00000004
_PROTECTED_DACL_SECURITY_INFORMATION: Final = 0x80000000
_SE_DACL_PRESENT: Final = 0x0004
_SE_DACL_PROTECTED: Final = 0x1000
_SDDL_REVISION_1: Final = 1
_ACL_SIZE_INFORMATION_CLASS: Final = 2

_ACCESS_ALLOWED_ACE_TYPE: Final = 0x00
_ACCESS_DENIED_ACE_TYPE: Final = 0x01
_ACCESS_ALLOWED_OBJECT_ACE_TYPE: Final = 0x05
_ACCESS_DENIED_OBJECT_ACE_TYPE: Final = 0x06
_OBJECT_INHERIT_ACE: Final = 0x01
_CONTAINER_INHERIT_ACE: Final = 0x02
_INHERIT_ONLY_ACE: Final = 0x08
_INHERITED_ACE: Final = 0x10
_ACE_OBJECT_TYPE_PRESENT: Final = 0x00000001
_ACE_INHERITED_OBJECT_TYPE_PRESENT: Final = 0x00000002

_SYSTEM_SID: Final = "S-1-5-18"
_ADMINISTRATORS_SID: Final = "S-1-5-32-544"

_MUTATING_ACCESS: Final = (
    _GENERIC_WRITE
    | _GENERIC_ALL
    | _DELETE
    | _WRITE_DAC
    | _WRITE_OWNER
    | _FILE_WRITE_DATA
    | _FILE_APPEND_DATA
    | _FILE_WRITE_EA
    | _FILE_DELETE_CHILD
    | _FILE_WRITE_ATTRIBUTES
)
_MANAGER_HOME_REQUIRED_ACCESS: Final = (
    _FILE_WRITE_DATA
    | _FILE_APPEND_DATA
    | _FILE_DELETE_CHILD
    | _READ_CONTROL
    | _WRITE_DAC
)
_SECURITY_CHANGE_ACCESS: Final = (
    _READ_CONTROL
    | _WRITE_DAC
    | _FILE_READ_ATTRIBUTES
    | _FILE_WRITE_ATTRIBUTES
)


class _MissingState(Exception):
    pass


class _UntrustedState(Exception):
    pass


class _CorruptState(Exception):
    pass


class _YoungEntry(Exception):
    pass


class _FileBasicInfo(ctypes.Structure):
    _fields_ = [
        ("creation_time", ctypes.c_longlong),
        ("last_access_time", ctypes.c_longlong),
        ("last_write_time", ctypes.c_longlong),
        ("change_time", ctypes.c_longlong),
        ("file_attributes", ctypes.c_uint32),
    ]


class _FileStandardInfo(ctypes.Structure):
    _fields_ = [
        ("allocation_size", ctypes.c_longlong),
        ("end_of_file", ctypes.c_longlong),
        ("number_of_links", ctypes.c_uint32),
        ("delete_pending", ctypes.c_ubyte),
        ("directory", ctypes.c_ubyte),
    ]


class _FileAttributeTagInfo(ctypes.Structure):
    _fields_ = [
        ("file_attributes", ctypes.c_uint32),
        ("reparse_tag", ctypes.c_uint32),
    ]


class _FileId128(ctypes.Structure):
    _fields_ = [("identifier", ctypes.c_ubyte * 16)]


class _FileIdInfo(ctypes.Structure):
    _fields_ = [
        ("volume_serial_number", ctypes.c_ulonglong),
        ("file_id", _FileId128),
    ]


class _FileRenameInformation(ctypes.Structure):
    _fields_ = [
        ("replace_if_exists", ctypes.c_ubyte),
        ("root_directory", ctypes.c_void_p),
        ("file_name_length", ctypes.c_uint32),
        ("file_name", ctypes.c_uint16 * 1),
    ]


class _IoStatusValue(ctypes.Union):
    _fields_ = [
        ("status", ctypes.c_long),
        ("pointer", ctypes.c_void_p),
    ]


class _IoStatusBlock(ctypes.Structure):
    _anonymous_ = ("value",)
    _fields_ = [
        ("value", _IoStatusValue),
        ("information", ctypes.c_size_t),
    ]


class _AclSizeInformation(ctypes.Structure):
    _fields_ = [
        ("ace_count", ctypes.c_uint32),
        ("acl_bytes_in_use", ctypes.c_uint32),
        ("acl_bytes_free", ctypes.c_uint32),
    ]


class _AceHeader(ctypes.Structure):
    _fields_ = [
        ("ace_type", ctypes.c_ubyte),
        ("ace_flags", ctypes.c_ubyte),
        ("ace_size", ctypes.c_ushort),
    ]


class _SidAndAttributes(ctypes.Structure):
    _fields_ = [
        ("sid", ctypes.c_void_p),
        ("attributes", ctypes.c_uint32),
    ]


class _TokenUser(ctypes.Structure):
    _fields_ = [("user", _SidAndAttributes)]


@dataclass(frozen=True)
class _WindowsApi:
    kernel32: Any
    advapi32: Any
    ntdll: Any


@dataclass(frozen=True)
class _FileIdentity:
    volume_serial_number: int
    file_id: bytes


@dataclass(frozen=True)
class _Ace:
    ace_type: int
    flags: int
    mask: int
    sid: str


@dataclass(frozen=True)
class _SecuritySnapshot:
    owner_sid: str
    dacl_present: bool
    dacl_protected: bool
    aces: tuple[_Ace, ...]


@dataclass(frozen=True)
class _SecurityProfile:
    manager_mask: int
    manager_flags: int
    readonly: bool = False


_MUTABLE_DIRECTORY = _SecurityProfile(
    manager_mask=_FILE_ALL_ACCESS,
    manager_flags=_OBJECT_INHERIT_ACE | _CONTAINER_INHERIT_ACE,
)
_MUTABLE_FILE = _SecurityProfile(
    manager_mask=_FILE_ALL_ACCESS,
    manager_flags=0,
)
_SEALED_ENTRY = _SecurityProfile(
    manager_mask=_FILE_GENERIC_READ | _FILE_GENERIC_EXECUTE,
    manager_flags=0,
)
_SEALED_DIRECTORY = _SecurityProfile(
    manager_mask=_FILE_GENERIC_READ | _FILE_GENERIC_EXECUTE,
    manager_flags=0,
)
_SEALED_RECEIPT = _SecurityProfile(
    manager_mask=_FILE_GENERIC_READ,
    manager_flags=0,
    readonly=True,
)
_SEALED_ARTIFACT = _SecurityProfile(
    manager_mask=_FILE_GENERIC_READ | _FILE_GENERIC_EXECUTE,
    manager_flags=0,
    readonly=True,
)


@dataclass
class _Handle:
    value: int
    path: Path
    identity: _FileIdentity
    final_path: str
    basic: _FileBasicInfo
    standard: _FileStandardInfo

    def close(self) -> None:
        api = _api()
        if not api.kernel32.CloseHandle(self.value):
            error = _last_error()
            raise _windows_error(error, "cannot close protected-cache handle", self.path)


@dataclass(frozen=True)
class _ObjectState:
    identity: _FileIdentity
    size: int
    links: int
    attributes: int
    last_write_time: int
    change_time: int


@dataclass(frozen=True)
class _VerifiedEntry:
    receipt: _metadata.BuildReceipt
    receipt_bytes: bytes
    receipt_sha256: str
    artifact_sha256: str
    artifact_size: int
    entry_identity: _FileIdentity


class WindowsBuildCache:
    """Protected immutable cache rooted below one trusted Windows manager home."""

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
        self._publication_lock = threading.RLock()

    @property
    def manager_home(self) -> Path:
        return self._manager_home

    def inspect(self, expectation: CacheExpectation) -> CacheInspection:
        """Inspect one candidate without creating, repairing, or quarantining."""

        if not self._supported:
            return CacheInspection(
                status=CacheEntryStatus.UNSUPPORTED,
                reason="required Windows protection primitives are unavailable",
            )
        try:
            key = _metadata.cache_key(expectation.input)
            artifact_name = _artifact_name(expectation.input)
            with _open_manager_home(self._manager_home) as home:  # noqa: SIM117
                with _open_protected_child_directory(
                    home,
                    LIVE_ROOT_NAME,
                    "build cache root",
                    _MUTABLE_DIRECTORY,
                    missing=_MissingState("build cache root is absent"),
                ) as builds:
                    with _open_protected_child_directory(
                        builds,
                        _DRIVER_DIRECTORY,
                        "build driver cache",
                        _MUTABLE_DIRECTORY,
                        missing=_MissingState("build driver cache is absent"),
                    ) as driver:
                        verified = _inspect_entry(
                            driver,
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
            return CacheInspection(status=CacheEntryStatus.MISS, reason=str(exc))
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
        _require_guard(guard)
        if not self._supported:
            raise BuildCacheError(
                "cache_protection_unsupported",
                "required Windows protection primitives are unavailable",
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

        try:
            with _open_publication_source(publication.artifact_source) as source:
                source_hash, source_size = _hash_handle(
                    source,
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

                _require_guard(guard)
                with _open_manager_home(self._manager_home) as home:  # noqa: SIM117
                    with _open_or_create_mutable_directory(
                        home,
                        QUARANTINE_ROOT_NAME,
                        "build cache quarantine",
                        replacement_parent=home,
                        replacement_prefix="builds-quarantine-untrusted",
                        guard=guard,
                    ) as quarantine:
                        with _open_or_create_mutable_directory(
                            home,
                            STAGING_ROOT_NAME,
                            "build cache staging root",
                            replacement_parent=quarantine,
                            replacement_prefix="boundary-staging",
                            guard=guard,
                        ) as staging:
                            stage_name = _create_stage(
                                staging,
                                artifact_name,
                                publication.receipt_bytes,
                                source,
                            )
                            stage_exists = True
                            try:
                                staged = _inspect_entry(
                                    staging,
                                    stage_name,
                                    CacheExpectation(
                                        input=publication.input,
                                        receipt_sha256=receipt_hash,
                                    ),
                                    key,
                                    artifact_name,
                                )
                                if (
                                    staged.receipt_bytes
                                    != publication.receipt_bytes
                                    or staged.artifact_sha256 != source_hash
                                    or staged.artifact_size != source_size
                                ):
                                    raise _CorruptState(
                                        "staged entry differs from the verified publication"
                                    )
                                _validate_source_unchanged(source)
                                _require_guard(guard)

                                with _open_or_create_mutable_directory(  # noqa: SIM117
                                    home,
                                    LIVE_ROOT_NAME,
                                    "build cache root",
                                    replacement_parent=quarantine,
                                    replacement_prefix="boundary-builds",
                                    guard=guard,
                                ) as builds:
                                    with _open_or_create_mutable_directory(
                                        builds,
                                        _DRIVER_DIRECTORY,
                                        "build driver cache",
                                        replacement_parent=quarantine,
                                        replacement_prefix="boundary-go-v1",
                                        guard=guard,
                                    ) as driver:
                                        result = self._select_winner(
                                            staging,
                                            stage_name,
                                            driver,
                                            quarantine,
                                            publication,
                                            staged,
                                            key,
                                            artifact_name,
                                            source,
                                            guard,
                                        )
                                        stage_exists = result.status is not (
                                            CachePublicationStatus.PUBLISHED
                                        )
                                        if result.status is (
                                            CachePublicationStatus.REUSED_WINNER
                                        ):
                                            _remove_stage(staging, stage_name)
                                            stage_exists = False
                                        return result
                            finally:
                                if stage_exists:
                                    try:
                                        _remove_stage(staging, stage_name)
                                    except OSError:
                                        pass
        except CacheConflictError:
            raise
        except BuildCacheError:
            raise
        except _UntrustedState as exc:
            raise BuildCacheError("cache_boundary_untrusted", str(exc)) from exc
        except (_CorruptState, _metadata.BuildMetadataError) as exc:
            raise BuildCacheError("cache_publication_invalid", str(exc)) from exc
        except _MissingState as exc:
            raise BuildCacheError("cache_boundary_missing", str(exc)) from exc
        except OSError as exc:
            raise BuildCacheError("cache_publication_failed", str(exc)) from exc

    def _select_winner(
        self,
        staging: _Handle,
        stage_name: str,
        driver: _Handle,
        quarantine: _Handle,
        publication: CachePublication,
        staged: _VerifiedEntry,
        key: str,
        artifact_name: str,
        source: _Handle,
        guard: CacheMutationGuard,
    ) -> CachePublicationResult:
        component = _key_component(key)
        for _attempt in range(8):
            _require_guard(guard)
            try:
                winner = _inspect_entry(
                    driver,
                    component,
                    CacheExpectation(input=publication.input),
                    key,
                    artifact_name,
                )
            except _MissingState:
                winner = None
            except (_UntrustedState, _CorruptState):
                moved = _move_aside(
                    driver,
                    component,
                    quarantine,
                    f"entry-{component}",
                    missing_ok=True,
                )
                if moved is None:
                    continue
                winner = None

            if winner is not None:
                if (
                    winner.receipt_bytes == publication.receipt_bytes
                    and _artifact_files_equal(
                        staging,
                        stage_name,
                        driver,
                        component,
                        artifact_name,
                    )
                ):
                    return CachePublicationResult(
                        status=CachePublicationStatus.REUSED_WINNER,
                        artifact_path=self._artifact_path(
                            key,
                            publication.input,
                        ),
                        receipt_sha256=winner.receipt_sha256,
                    )
                raise CacheConflictError(key)

            _validate_source_unchanged(source)
            _require_guard(guard)
            with _open_protected_child_directory(
                staging,
                stage_name,
                "staged cache entry",
                _SEALED_ENTRY,
                missing=_CorruptState("staged cache entry disappeared"),
                stable_name=False,
            ) as staged_handle:
                if staged_handle.identity != staged.entry_identity:
                    raise _CorruptState("staged cache entry identity changed")
                try:
                    _move_no_replace(
                        staging.path / stage_name,
                        driver.path / component,
                    )
                except FileExistsError:
                    continue
                _revalidate_handle(staging, _MUTABLE_DIRECTORY)
                _revalidate_handle(driver, _MUTABLE_DIRECTORY)
                selected = _inspect_entry(
                    driver,
                    component,
                    CacheExpectation(
                        input=publication.input,
                        receipt_sha256=staged.receipt_sha256,
                    ),
                    key,
                    artifact_name,
                )
                if selected.entry_identity == staged_handle.identity:
                    if (
                        selected.receipt_bytes != staged.receipt_bytes
                        or selected.artifact_sha256 != staged.artifact_sha256
                        or selected.artifact_size != staged.artifact_size
                    ):
                        raise _CorruptState(
                            "published entry differs from staged bytes"
                        )
                    return CachePublicationResult(
                        status=CachePublicationStatus.PUBLISHED,
                        artifact_path=self._artifact_path(
                            key,
                            publication.input,
                        ),
                        receipt_sha256=selected.receipt_sha256,
                    )
                if (
                    selected.receipt_bytes == staged.receipt_bytes
                    and _artifact_files_equal(
                        staging,
                        stage_name,
                        driver,
                        component,
                        artifact_name,
                        first_entry_path=_handle_path_after_move(
                            staged_handle,
                        ),
                        first_entry_identity=staged_handle.identity,
                    )
                ):
                    return CachePublicationResult(
                        status=CachePublicationStatus.REUSED_WINNER,
                        artifact_path=self._artifact_path(
                            key,
                            publication.input,
                        ),
                        receipt_sha256=selected.receipt_sha256,
                    )
                raise CacheConflictError(key)
        raise BuildCacheError(
            "cache_publication_race",
            "could not select or validate an atomic cache winner",
        )

    def quarantine(
        self,
        cache_key: str,
        *,
        guard: CacheMutationGuard,
    ) -> Path | None:
        _require_guard(guard)
        if not self._supported:
            raise BuildCacheError(
                "cache_protection_unsupported",
                "required Windows protection primitives are unavailable",
            )
        component = _key_component(cache_key)
        try:
            with _open_manager_home(self._manager_home) as home:  # noqa: SIM117
                with _open_or_create_mutable_directory(
                    home,
                    QUARANTINE_ROOT_NAME,
                    "build cache quarantine",
                    replacement_parent=home,
                    replacement_prefix="builds-quarantine-untrusted",
                    guard=guard,
                ) as quarantine:
                    try:
                        with _open_protected_child_directory(  # noqa: SIM117
                            home,
                            LIVE_ROOT_NAME,
                            "build cache root",
                            _MUTABLE_DIRECTORY,
                            missing=_MissingState("build cache root is absent"),
                        ) as builds:
                            with _open_protected_child_directory(
                                builds,
                                _DRIVER_DIRECTORY,
                                "build driver cache",
                                _MUTABLE_DIRECTORY,
                                missing=_MissingState(
                                    "build driver cache is absent"
                                ),
                            ) as driver:
                                _require_guard(guard)
                                moved = _move_aside(
                                    driver,
                                    component,
                                    quarantine,
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
                    "build cache retained: required Windows protection primitives are unavailable",
                )
            )
        referenced = {_key_component(key) for key in referenced_cache_keys}
        removed = 0
        warnings: list[str] = []
        try:
            with _open_manager_home(self._manager_home) as home:
                try:
                    with _open_protected_child_directory(  # noqa: SIM117
                        home,
                        LIVE_ROOT_NAME,
                        "build cache root",
                        _MUTABLE_DIRECTORY,
                        missing=_MissingState("build cache root is absent"),
                    ) as builds:
                        with _open_protected_child_directory(
                            builds,
                            _DRIVER_DIRECTORY,
                            "build driver cache",
                            _MUTABLE_DIRECTORY,
                            missing=_MissingState(
                                "build driver cache is absent"
                            ),
                        ) as driver:
                            names = _directory_names(
                                driver,
                                "build driver cache",
                            )
                            with _open_or_create_mutable_directory(
                                home,
                                QUARANTINE_ROOT_NAME,
                                "build cache quarantine",
                                replacement_parent=home,
                                replacement_prefix="builds-quarantine-untrusted",
                                guard=guard,
                            ) as quarantine:
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
                                        _verified, expected_state = _inspect_gc_entry(
                                            driver,
                                            component,
                                            f"sha256:{component}",
                                            older_than=float(older_than),
                                        )
                                    except _YoungEntry:
                                        continue
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
                                    _require_guard(guard)
                                    moved = _move_aside(
                                        driver,
                                        component,
                                        quarantine,
                                        f"gc-entry-{component}",
                                        missing_ok=True,
                                        expected_state=expected_state,
                                    )
                                    if moved is None:
                                        continue
                                    removed += 1
                                    try:
                                        _remove_stage(quarantine, moved)
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
        artifact_path = str(build_input.artifact_path)
        return (
            self._manager_home
            / LIVE_ROOT_NAME
            / _DRIVER_DIRECTORY
            / _key_component(key)
            / Path(*artifact_path.split("/"))
        )


def _protection_supported() -> bool:
    if os.name != "nt":
        return False
    try:
        _api()
        _current_user_sid()
    except (AttributeError, OSError, RuntimeError):
        return False
    return True


@lru_cache(maxsize=1)
def _api() -> _WindowsApi:
    if os.name != "nt":
        raise OSError(errno.ENOSYS, "Win32 APIs are unavailable")
    win_dll: Any = ctypes.__dict__["WinDLL"]
    kernel32: Any = win_dll("kernel32", use_last_error=True)
    advapi32: Any = win_dll("advapi32", use_last_error=True)
    ntdll: Any = win_dll("ntdll")

    kernel32.CreateFileW.argtypes = [
        ctypes.c_wchar_p,
        ctypes.c_uint32,
        ctypes.c_uint32,
        ctypes.c_void_p,
        ctypes.c_uint32,
        ctypes.c_uint32,
        ctypes.c_void_p,
    ]
    kernel32.CreateFileW.restype = ctypes.c_void_p
    kernel32.CloseHandle.argtypes = [ctypes.c_void_p]
    kernel32.CloseHandle.restype = ctypes.c_int
    kernel32.GetFileType.argtypes = [ctypes.c_void_p]
    kernel32.GetFileType.restype = ctypes.c_uint32
    kernel32.GetFileInformationByHandleEx.argtypes = [
        ctypes.c_void_p,
        ctypes.c_int,
        ctypes.c_void_p,
        ctypes.c_uint32,
    ]
    kernel32.GetFileInformationByHandleEx.restype = ctypes.c_int
    kernel32.GetFinalPathNameByHandleW.argtypes = [
        ctypes.c_void_p,
        ctypes.c_wchar_p,
        ctypes.c_uint32,
        ctypes.c_uint32,
    ]
    kernel32.GetFinalPathNameByHandleW.restype = ctypes.c_uint32
    kernel32.SetFilePointerEx.argtypes = [
        ctypes.c_void_p,
        ctypes.c_longlong,
        ctypes.POINTER(ctypes.c_longlong),
        ctypes.c_uint32,
    ]
    kernel32.SetFilePointerEx.restype = ctypes.c_int
    kernel32.ReadFile.argtypes = [
        ctypes.c_void_p,
        ctypes.c_void_p,
        ctypes.c_uint32,
        ctypes.POINTER(ctypes.c_uint32),
        ctypes.c_void_p,
    ]
    kernel32.ReadFile.restype = ctypes.c_int
    kernel32.WriteFile.argtypes = [
        ctypes.c_void_p,
        ctypes.c_void_p,
        ctypes.c_uint32,
        ctypes.POINTER(ctypes.c_uint32),
        ctypes.c_void_p,
    ]
    kernel32.WriteFile.restype = ctypes.c_int
    kernel32.FlushFileBuffers.argtypes = [ctypes.c_void_p]
    kernel32.FlushFileBuffers.restype = ctypes.c_int
    kernel32.SetFileInformationByHandle.argtypes = [
        ctypes.c_void_p,
        ctypes.c_int,
        ctypes.c_void_p,
        ctypes.c_uint32,
    ]
    kernel32.SetFileInformationByHandle.restype = ctypes.c_int
    kernel32.MoveFileExW.argtypes = [
        ctypes.c_wchar_p,
        ctypes.c_wchar_p,
        ctypes.c_uint32,
    ]
    kernel32.MoveFileExW.restype = ctypes.c_int
    kernel32.GetCurrentProcess.argtypes = []
    kernel32.GetCurrentProcess.restype = ctypes.c_void_p
    kernel32.LocalFree.argtypes = [ctypes.c_void_p]
    kernel32.LocalFree.restype = ctypes.c_void_p

    ntdll.NtSetInformationFile.argtypes = [
        ctypes.c_void_p,
        ctypes.POINTER(_IoStatusBlock),
        ctypes.c_void_p,
        ctypes.c_uint32,
        ctypes.c_int,
    ]
    ntdll.NtSetInformationFile.restype = ctypes.c_long
    ntdll.RtlNtStatusToDosError.argtypes = [ctypes.c_long]
    ntdll.RtlNtStatusToDosError.restype = ctypes.c_uint32

    advapi32.OpenProcessToken.argtypes = [
        ctypes.c_void_p,
        ctypes.c_uint32,
        ctypes.POINTER(ctypes.c_void_p),
    ]
    advapi32.OpenProcessToken.restype = ctypes.c_int
    advapi32.GetTokenInformation.argtypes = [
        ctypes.c_void_p,
        ctypes.c_int,
        ctypes.c_void_p,
        ctypes.c_uint32,
        ctypes.POINTER(ctypes.c_uint32),
    ]
    advapi32.GetTokenInformation.restype = ctypes.c_int
    advapi32.ConvertSidToStringSidW.argtypes = [
        ctypes.c_void_p,
        ctypes.POINTER(ctypes.c_wchar_p),
    ]
    advapi32.ConvertSidToStringSidW.restype = ctypes.c_int
    advapi32.ConvertStringSecurityDescriptorToSecurityDescriptorW.argtypes = [
        ctypes.c_wchar_p,
        ctypes.c_uint32,
        ctypes.POINTER(ctypes.c_void_p),
        ctypes.POINTER(ctypes.c_uint32),
    ]
    advapi32.ConvertStringSecurityDescriptorToSecurityDescriptorW.restype = (
        ctypes.c_int
    )
    advapi32.GetSecurityDescriptorOwner.argtypes = [
        ctypes.c_void_p,
        ctypes.POINTER(ctypes.c_void_p),
        ctypes.POINTER(ctypes.c_int),
    ]
    advapi32.GetSecurityDescriptorOwner.restype = ctypes.c_int
    advapi32.GetSecurityDescriptorDacl.argtypes = [
        ctypes.c_void_p,
        ctypes.POINTER(ctypes.c_int),
        ctypes.POINTER(ctypes.c_void_p),
        ctypes.POINTER(ctypes.c_int),
    ]
    advapi32.GetSecurityDescriptorDacl.restype = ctypes.c_int
    advapi32.GetSecurityDescriptorControl.argtypes = [
        ctypes.c_void_p,
        ctypes.POINTER(ctypes.c_ushort),
        ctypes.POINTER(ctypes.c_uint32),
    ]
    advapi32.GetSecurityDescriptorControl.restype = ctypes.c_int
    advapi32.GetAclInformation.argtypes = [
        ctypes.c_void_p,
        ctypes.c_void_p,
        ctypes.c_uint32,
        ctypes.c_int,
    ]
    advapi32.GetAclInformation.restype = ctypes.c_int
    advapi32.GetAce.argtypes = [
        ctypes.c_void_p,
        ctypes.c_uint32,
        ctypes.POINTER(ctypes.c_void_p),
    ]
    advapi32.GetAce.restype = ctypes.c_int
    advapi32.GetSecurityInfo.argtypes = [
        ctypes.c_void_p,
        ctypes.c_int,
        ctypes.c_uint32,
        ctypes.POINTER(ctypes.c_void_p),
        ctypes.c_void_p,
        ctypes.POINTER(ctypes.c_void_p),
        ctypes.c_void_p,
        ctypes.POINTER(ctypes.c_void_p),
    ]
    advapi32.GetSecurityInfo.restype = ctypes.c_uint32
    advapi32.SetSecurityInfo.argtypes = [
        ctypes.c_void_p,
        ctypes.c_int,
        ctypes.c_uint32,
        ctypes.c_void_p,
        ctypes.c_void_p,
        ctypes.c_void_p,
        ctypes.c_void_p,
    ]
    advapi32.SetSecurityInfo.restype = ctypes.c_uint32
    return _WindowsApi(kernel32=kernel32, advapi32=advapi32, ntdll=ntdll)


def _last_error() -> int:
    get_last_error: Any = ctypes.__dict__["get_last_error"]
    return int(get_last_error())


def _windows_error(error: int, action: str, path: Path | None = None) -> OSError:
    format_error: Any = ctypes.__dict__["FormatError"]
    detail = str(format_error(error)).strip()
    message = f"{action}: {detail}" if detail else action
    return OSError(error, message, None if path is None else os.fspath(path))


def _extended_path(path: Path) -> str:
    value = os.fspath(path)
    if value.startswith("\\\\?\\"):
        return value
    if value.startswith("\\\\"):
        return "\\\\?\\UNC\\" + value[2:]
    return "\\\\?\\" + value


@contextmanager
def _open_raw_handle(
    path: Path,
    *,
    desired_access: int,
    creation_disposition: int = _OPEN_EXISTING,
    attributes: int = _FILE_ATTRIBUTE_NORMAL,
    missing: Exception | None = None,
) -> Iterator[_Handle]:
    api = _api()
    flags = (
        attributes
        | _FILE_FLAG_OPEN_REPARSE_POINT
        | _FILE_FLAG_BACKUP_SEMANTICS
    )
    raw = api.kernel32.CreateFileW(
        _extended_path(path),
        desired_access,
        _FILE_SHARE_READ | _FILE_SHARE_WRITE | _FILE_SHARE_DELETE,
        None,
        creation_disposition,
        flags,
        None,
    )
    invalid = ctypes.c_void_p(-1).value
    if raw in {None, invalid}:
        error = _last_error()
        if missing is not None and error in {
            _ERROR_FILE_NOT_FOUND,
            _ERROR_PATH_NOT_FOUND,
        }:
            raise missing
        raise _windows_error(error, "cannot open protected-cache object", path)
    try:
        handle = _handle_details(int(raw), path)
    except BaseException:
        api.kernel32.CloseHandle(raw)
        raise
    close_error: OSError | None = None
    try:
        yield handle
    finally:
        try:
            handle.close()
        except OSError as exc:
            close_error = exc
        if close_error is not None:
            raise close_error


def _handle_details(value: int, path: Path) -> _Handle:
    api = _api()
    file_type = int(api.kernel32.GetFileType(value))
    if file_type != _FILE_TYPE_DISK:
        raise _UntrustedState(f"{path} is not a disk file or directory")
    basic = _query_handle_info(value, _FILE_BASIC_INFO_CLASS, _FileBasicInfo)
    standard = _query_handle_info(
        value,
        _FILE_STANDARD_INFO_CLASS,
        _FileStandardInfo,
    )
    tag = _query_handle_info(
        value,
        _FILE_ATTRIBUTE_TAG_INFO_CLASS,
        _FileAttributeTagInfo,
    )
    if tag.file_attributes & _FILE_ATTRIBUTE_REPARSE_POINT:
        raise _UntrustedState(f"{path} is a reparse point")
    identity_info = _query_handle_info(
        value,
        _FILE_ID_INFO_CLASS,
        _FileIdInfo,
    )
    identity = _FileIdentity(
        volume_serial_number=int(identity_info.volume_serial_number),
        file_id=bytes(identity_info.file_id.identifier),
    )
    return _Handle(
        value=value,
        path=path,
        identity=identity,
        final_path=_final_path(value, path),
        basic=basic,
        standard=standard,
    )


def _query_handle_info(
    handle: int,
    information_class: int,
    structure_type: type[Any],
) -> Any:
    api = _api()
    result = structure_type()
    if not api.kernel32.GetFileInformationByHandleEx(
        handle,
        information_class,
        ctypes.byref(result),
        ctypes.sizeof(result),
    ):
        raise _windows_error(
            _last_error(),
            "cannot query protected-cache handle information",
        )
    return result


def _final_path(handle: int, path: Path) -> str:
    api = _api()
    capacity = 512
    for _attempt in range(4):
        buffer = ctypes.create_unicode_buffer(capacity)
        length = int(
            api.kernel32.GetFinalPathNameByHandleW(
                handle,
                buffer,
                capacity,
                0,
            )
        )
        if length == 0:
            raise _windows_error(
                _last_error(),
                "cannot resolve protected-cache handle path",
                path,
            )
        if length < capacity:
            return ntpath.normpath(buffer.value)
        capacity = length + 1
    raise _UntrustedState(f"cannot bound final path for {path}")


def _handle_path_after_move(handle: _Handle) -> Path:
    final_path = _final_path(handle.value, handle.path)
    if final_path.startswith("\\\\?\\UNC\\"):
        return Path("\\\\" + final_path[8:])
    if final_path.startswith("\\\\?\\"):
        return Path(final_path[4:])
    return Path(final_path)


def _require_direct_child(parent: _Handle, child: _Handle, name: str) -> None:
    if parent.identity.volume_serial_number != child.identity.volume_serial_number:
        raise _UntrustedState(f"{child.path} escaped the protected cache volume")
    child_parent = ntpath.dirname(child.final_path)
    if child_parent.casefold() != parent.final_path.casefold():
        raise _UntrustedState(f"{child.path} escaped its protected parent")
    if ntpath.basename(child.final_path).casefold() != name.casefold():
        raise _UntrustedState(f"{child.path} resolved to an unexpected name")


@contextmanager
def _open_manager_home(path: Path) -> Iterator[_Handle]:
    with _open_raw_handle(
        path,
        desired_access=_READ_CONTROL | _FILE_READ_ATTRIBUTES,
        missing=_MissingState("manager home is absent"),
    ) as handle:
        if not handle.standard.directory:
            raise _UntrustedState("manager home is not a directory")
        _validate_manager_home_security(handle)
        _ensure_no_named_streams(handle, "manager home")
        yield handle
        _revalidate_manager_home(handle)


@contextmanager
def _open_protected_child_directory(
    parent: _Handle,
    name: str,
    label: str,
    profile: _SecurityProfile,
    *,
    missing: Exception,
    stable_name: bool = True,
) -> Iterator[_Handle]:
    path = parent.path / name
    with _open_raw_handle(
        path,
        desired_access=_READ_CONTROL | _FILE_READ_ATTRIBUTES | _GENERIC_READ,
        missing=missing,
    ) as handle:
        _require_direct_child(parent, handle, name)
        if not handle.standard.directory:
            raise _UntrustedState(f"{label} is not a directory")
        _validate_security_profile(handle, profile, label)
        _ensure_no_named_streams(handle, label)
        yield handle
        if stable_name:
            _revalidate_child(parent, handle, name, profile, label)
        else:
            _revalidate_handle(
                handle,
                profile,
                label,
                allow_path_change=True,
            )
            _revalidate_handle(parent, None)


@contextmanager
def _open_protected_child_file(
    parent: _Handle,
    name: str,
    label: str,
    profile: _SecurityProfile,
    *,
    missing: Exception,
) -> Iterator[_Handle]:
    path = parent.path / name
    with _open_raw_handle(
        path,
        desired_access=_READ_CONTROL | _FILE_READ_ATTRIBUTES | _GENERIC_READ,
        missing=missing,
    ) as handle:
        _require_direct_child(parent, handle, name)
        _require_singly_linked_file(handle, label)
        _validate_security_profile(handle, profile, label)
        _ensure_no_named_streams(handle, label)
        yield handle
        _revalidate_file_child(parent, handle, name, profile, label)


def _require_singly_linked_file(handle: _Handle, label: str) -> None:
    if handle.standard.directory:
        raise _UntrustedState(f"{label} is not a regular file")
    if int(handle.standard.number_of_links) != 1:
        raise _UntrustedState(f"{label} is not singly linked")


def _revalidate_file_child(
    parent: _Handle,
    handle: _Handle,
    name: str,
    profile: _SecurityProfile,
    label: str,
) -> None:
    retained = _revalidate_handle(handle, profile, label)
    _require_singly_linked_file(retained, label)
    _revalidate_handle(parent, None)
    with _open_raw_handle(
        parent.path / name,
        desired_access=_READ_CONTROL | _FILE_READ_ATTRIBUTES,
        missing=_UntrustedState(f"{label} disappeared during validation"),
    ) as selected:
        _require_direct_child(parent, selected, name)
        if selected.identity != handle.identity:
            raise _UntrustedState(f"{label} identity changed during validation")
        _require_singly_linked_file(selected, label)
        _validate_security_profile(selected, profile, label)
        retained_final = _revalidate_handle(handle, None, label)
        selected_final = _revalidate_handle(selected, None, label)
        _require_singly_linked_file(retained_final, label)
        _require_singly_linked_file(selected_final, label)


def _revalidate_child(
    parent: _Handle,
    handle: _Handle,
    name: str,
    profile: _SecurityProfile,
    label: str,
) -> None:
    _revalidate_handle(handle, profile, label)
    _revalidate_handle(parent, None)
    with _open_raw_handle(
        parent.path / name,
        desired_access=_READ_CONTROL | _FILE_READ_ATTRIBUTES,
        missing=_UntrustedState(f"{label} disappeared during validation"),
    ) as selected:
        _require_direct_child(parent, selected, name)
        if selected.identity != handle.identity:
            raise _UntrustedState(f"{label} identity changed during validation")
        _validate_security_profile(selected, profile, label)


def _revalidate_handle(
    handle: _Handle,
    profile: _SecurityProfile | None,
    label: str = "protected cache object",
    *,
    allow_path_change: bool = False,
) -> _Handle:
    current = _handle_details(handle.value, handle.path)
    if (
        current.identity != handle.identity
        or (
            not allow_path_change
            and current.final_path.casefold() != handle.final_path.casefold()
        )
    ):
        raise _UntrustedState(f"{label} handle identity changed")
    if profile is not None:
        _validate_security_profile(current, profile, label)
    return current


def _revalidate_manager_home(handle: _Handle) -> None:
    _revalidate_handle(handle, None, "manager home")
    _validate_manager_home_security(handle)


def _object_state(handle: _Handle) -> _ObjectState:
    current = _handle_details(handle.value, handle.path)
    return _ObjectState(
        identity=current.identity,
        size=int(current.standard.end_of_file),
        links=int(current.standard.number_of_links),
        attributes=int(current.basic.file_attributes),
        last_write_time=int(current.basic.last_write_time),
        change_time=int(current.basic.change_time),
    )


@lru_cache(maxsize=1)
def _current_user_sid() -> str:
    api = _api()
    token = ctypes.c_void_p()
    if not api.advapi32.OpenProcessToken(
        api.kernel32.GetCurrentProcess(),
        _TOKEN_QUERY,
        ctypes.byref(token),
    ):
        raise _windows_error(_last_error(), "cannot open the current process token")
    try:
        required = ctypes.c_uint32()
        api.advapi32.GetTokenInformation(
            token,
            _TOKEN_USER_CLASS,
            None,
            0,
            ctypes.byref(required),
        )
        error = _last_error()
        if error != _ERROR_INSUFFICIENT_BUFFER or required.value == 0:
            raise _windows_error(error, "cannot size the current user SID")
        buffer = ctypes.create_string_buffer(required.value)
        if not api.advapi32.GetTokenInformation(
            token,
            _TOKEN_USER_CLASS,
            buffer,
            required.value,
            ctypes.byref(required),
        ):
            raise _windows_error(_last_error(), "cannot read the current user SID")
        token_user = ctypes.cast(buffer, ctypes.POINTER(_TokenUser)).contents
        return _sid_string(int(token_user.user.sid))
    finally:
        if not api.kernel32.CloseHandle(token):
            raise _windows_error(_last_error(), "cannot close the process token")


def _sid_string(sid_pointer: int) -> str:
    api = _api()
    output = ctypes.c_wchar_p()
    if not api.advapi32.ConvertSidToStringSidW(
        sid_pointer,
        ctypes.byref(output),
    ):
        raise _windows_error(_last_error(), "cannot convert a Windows SID")
    try:
        if output.value is None:
            raise _UntrustedState("Windows SID conversion returned no value")
        return output.value
    finally:
        api.kernel32.LocalFree(output)


def _security_snapshot(handle: _Handle) -> _SecuritySnapshot:
    api = _api()
    owner = ctypes.c_void_p()
    dacl = ctypes.c_void_p()
    descriptor = ctypes.c_void_p()
    result = int(
        api.advapi32.GetSecurityInfo(
            handle.value,
            _SE_FILE_OBJECT,
            _OWNER_SECURITY_INFORMATION | _DACL_SECURITY_INFORMATION,
            ctypes.byref(owner),
            None,
            ctypes.byref(dacl),
            None,
            ctypes.byref(descriptor),
        )
    )
    if result != _ERROR_SUCCESS:
        raise _windows_error(
            result,
            "cannot read protected-cache owner and DACL",
            handle.path,
        )
    try:
        if not owner.value:
            raise _UntrustedState(f"{handle.path} has no owner SID")
        owner_sid = _sid_string(int(owner.value))
        control = ctypes.c_ushort()
        revision = ctypes.c_uint32()
        if not api.advapi32.GetSecurityDescriptorControl(
            descriptor,
            ctypes.byref(control),
            ctypes.byref(revision),
        ):
            raise _windows_error(
                _last_error(),
                "cannot read protected-cache security controls",
                handle.path,
            )
        dacl_present = bool(control.value & _SE_DACL_PRESENT)
        if not dacl_present or not dacl.value:
            aces: tuple[_Ace, ...] = ()
        else:
            aces = _read_aces(int(dacl.value))
        return _SecuritySnapshot(
            owner_sid=owner_sid,
            dacl_present=dacl_present,
            dacl_protected=bool(control.value & _SE_DACL_PROTECTED),
            aces=aces,
        )
    finally:
        api.kernel32.LocalFree(descriptor)


def _read_aces(dacl_pointer: int) -> tuple[_Ace, ...]:
    api = _api()
    information = _AclSizeInformation()
    if not api.advapi32.GetAclInformation(
        dacl_pointer,
        ctypes.byref(information),
        ctypes.sizeof(information),
        _ACL_SIZE_INFORMATION_CLASS,
    ):
        raise _windows_error(_last_error(), "cannot inspect protected-cache DACL")
    aces: list[_Ace] = []
    for index in range(int(information.ace_count)):
        ace_pointer = ctypes.c_void_p()
        if not api.advapi32.GetAce(
            dacl_pointer,
            index,
            ctypes.byref(ace_pointer),
        ):
            raise _windows_error(_last_error(), "cannot inspect protected-cache ACE")
        if ace_pointer.value is None:
            raise _UntrustedState("protected-cache DACL contains a null ACE")
        address = int(ace_pointer.value)
        header = ctypes.cast(address, ctypes.POINTER(_AceHeader)).contents
        if header.ace_size < 8:
            raise _UntrustedState("protected-cache DACL contains a truncated ACE")
        mask = ctypes.c_uint32.from_address(address + 4).value
        if header.ace_type in {
            _ACCESS_ALLOWED_ACE_TYPE,
            _ACCESS_DENIED_ACE_TYPE,
        }:
            sid_offset = 8
        elif header.ace_type in {
            _ACCESS_ALLOWED_OBJECT_ACE_TYPE,
            _ACCESS_DENIED_OBJECT_ACE_TYPE,
        }:
            if header.ace_size < 12:
                raise _UntrustedState(
                    "protected-cache DACL contains a truncated object ACE"
                )
            object_flags = ctypes.c_uint32.from_address(address + 8).value
            sid_offset = 12
            if object_flags & _ACE_OBJECT_TYPE_PRESENT:
                sid_offset += 16
            if object_flags & _ACE_INHERITED_OBJECT_TYPE_PRESENT:
                sid_offset += 16
        else:
            raise _UntrustedState(
                f"protected-cache DACL contains unsupported ACE type "
                f"{header.ace_type}"
            )
        if sid_offset >= header.ace_size:
            raise _UntrustedState("protected-cache DACL contains an invalid SID")
        aces.append(
            _Ace(
                ace_type=int(header.ace_type),
                flags=int(header.ace_flags),
                mask=int(mask),
                sid=_sid_string(address + sid_offset),
            )
        )
    return tuple(aces)


def _validate_manager_home_security(handle: _Handle) -> None:
    snapshot = _security_snapshot(handle)
    current_sid = _current_user_sid()
    if snapshot.owner_sid != current_sid:
        raise _UntrustedState(
            "manager home owner does not match the current manager principal"
        )
    if not snapshot.dacl_present:
        raise _UntrustedState("manager home has an absent or null DACL")
    trusted = {current_sid, _SYSTEM_SID, _ADMINISTRATORS_SID}
    manager_grants = 0
    for ace in snapshot.aces:
        if ace.ace_type in {
            _ACCESS_ALLOWED_ACE_TYPE,
            _ACCESS_ALLOWED_OBJECT_ACE_TYPE,
        }:
            expanded = _expanded_access_mask(ace.mask)
            inherit_only = bool(ace.flags & _INHERIT_ONLY_ACE)
            inheritable = bool(
                ace.flags & (_OBJECT_INHERIT_ACE | _CONTAINER_INHERIT_ACE)
            )
            if ace.sid == current_sid and not inherit_only:
                manager_grants |= expanded
            if (
                ace.sid not in trusted
                and expanded & _MUTATING_ACCESS
                and (not inherit_only or inheritable)
            ):
                raise _UntrustedState(
                    "manager home grants mutation rights to an untrusted principal"
                )
    if (
        manager_grants & _MANAGER_HOME_REQUIRED_ACCESS
        != _MANAGER_HOME_REQUIRED_ACCESS
    ):
        raise _UntrustedState(
            "manager home does not grant the manager principal required control"
        )


def _expanded_access_mask(mask: int) -> int:
    if mask & _GENERIC_ALL:
        return mask | _FILE_ALL_ACCESS
    expanded = mask
    if mask & _GENERIC_WRITE:
        expanded |= (
            _FILE_WRITE_DATA
            | _FILE_APPEND_DATA
            | _FILE_WRITE_EA
            | _FILE_WRITE_ATTRIBUTES
            | _READ_CONTROL
        )
    if mask & _GENERIC_READ:
        expanded |= _FILE_GENERIC_READ
    if mask & _GENERIC_EXECUTE:
        expanded |= _FILE_GENERIC_EXECUTE
    return expanded


def _expected_aces(profile: _SecurityProfile) -> tuple[_Ace, ...]:
    return (
        _Ace(
            ace_type=_ACCESS_ALLOWED_ACE_TYPE,
            flags=profile.manager_flags,
            mask=profile.manager_mask,
            sid=_current_user_sid(),
        ),
        _Ace(
            ace_type=_ACCESS_ALLOWED_ACE_TYPE,
            flags=profile.manager_flags,
            mask=_FILE_ALL_ACCESS,
            sid=_SYSTEM_SID,
        ),
        _Ace(
            ace_type=_ACCESS_ALLOWED_ACE_TYPE,
            flags=profile.manager_flags,
            mask=_FILE_ALL_ACCESS,
            sid=_ADMINISTRATORS_SID,
        ),
    )


def _validate_security_profile(
    handle: _Handle,
    profile: _SecurityProfile,
    label: str,
) -> None:
    snapshot = _security_snapshot(handle)
    if snapshot.owner_sid != _current_user_sid():
        raise _UntrustedState(
            f"{label} owner does not match the current manager principal"
        )
    if not snapshot.dacl_present or not snapshot.dacl_protected:
        raise _UntrustedState(f"{label} DACL is absent, null, or inheritable")
    if sorted(snapshot.aces, key=_ace_sort_key) != sorted(
        _expected_aces(profile),
        key=_ace_sort_key,
    ):
        raise _UntrustedState(f"{label} DACL differs from its protected profile")
    attributes = _handle_details(handle.value, handle.path).basic.file_attributes
    is_readonly = bool(attributes & _FILE_ATTRIBUTE_READONLY)
    if profile.readonly != is_readonly:
        state = "read-only" if profile.readonly else "mutable"
        raise _UntrustedState(f"{label} is not in its required {state} state")


def _ace_sort_key(ace: _Ace) -> tuple[int, int, int, str]:
    return ace.ace_type, ace.flags, ace.mask, ace.sid


def _profile_sddl(profile: _SecurityProfile) -> str:
    flags = ""
    if profile.manager_flags & _OBJECT_INHERIT_ACE:
        flags += "OI"
    if profile.manager_flags & _CONTAINER_INHERIT_ACE:
        flags += "CI"
    current_sid = _current_user_sid()
    return (
        f"O:{current_sid}D:P"
        f"(A;{flags};0x{profile.manager_mask:08x};;;{current_sid})"
        f"(A;{flags};0x{_FILE_ALL_ACCESS:08x};;;SY)"
        f"(A;{flags};0x{_FILE_ALL_ACCESS:08x};;;BA)"
    )


def _apply_security_profile(handle: _Handle, profile: _SecurityProfile) -> None:
    _apply_profile_security(handle, profile, set_owner=True)
    _set_readonly(handle, profile.readonly)
    _validate_security_profile(handle, profile, os.fspath(handle.path))


def _apply_profile_dacl(handle: _Handle, profile: _SecurityProfile) -> None:
    _apply_profile_security(handle, profile, set_owner=False)


def _apply_profile_security(
    handle: _Handle,
    profile: _SecurityProfile,
    *,
    set_owner: bool,
) -> None:
    api = _api()
    descriptor = ctypes.c_void_p()
    descriptor_size = ctypes.c_uint32()
    if not api.advapi32.ConvertStringSecurityDescriptorToSecurityDescriptorW(
        _profile_sddl(profile),
        _SDDL_REVISION_1,
        ctypes.byref(descriptor),
        ctypes.byref(descriptor_size),
    ):
        raise _windows_error(
            _last_error(),
            "cannot construct protected-cache security descriptor",
            handle.path,
        )
    try:
        owner = ctypes.c_void_p()
        if set_owner:
            owner_defaulted = ctypes.c_int()
            if not api.advapi32.GetSecurityDescriptorOwner(
                descriptor,
                ctypes.byref(owner),
                ctypes.byref(owner_defaulted),
            ):
                raise _windows_error(
                    _last_error(),
                    "cannot read constructed owner SID",
                    handle.path,
                )
        dacl_present = ctypes.c_int()
        dacl = ctypes.c_void_p()
        dacl_defaulted = ctypes.c_int()
        if not api.advapi32.GetSecurityDescriptorDacl(
            descriptor,
            ctypes.byref(dacl_present),
            ctypes.byref(dacl),
            ctypes.byref(dacl_defaulted),
        ):
            raise _windows_error(
                _last_error(),
                "cannot read constructed protected DACL",
                handle.path,
            )
        if not dacl_present.value or not dacl.value:
            raise _UntrustedState("constructed protected DACL is absent")
        result = int(
            api.advapi32.SetSecurityInfo(
                handle.value,
                _SE_FILE_OBJECT,
                (
                    _DACL_SECURITY_INFORMATION
                    | _PROTECTED_DACL_SECURITY_INFORMATION
                    | (_OWNER_SECURITY_INFORMATION if set_owner else 0)
                ),
                owner if set_owner else None,
                None,
                dacl,
                None,
            )
        )
        if result != _ERROR_SUCCESS:
            raise _windows_error(
                result,
                "cannot apply protected-cache owner and DACL",
                handle.path,
            )
    finally:
        api.kernel32.LocalFree(descriptor)


def _set_readonly(handle: _Handle, readonly: bool) -> None:
    api = _api()
    basic = _query_handle_info(
        handle.value,
        _FILE_BASIC_INFO_CLASS,
        _FileBasicInfo,
    )
    current = bool(basic.file_attributes & _FILE_ATTRIBUTE_READONLY)
    if current == readonly:
        return
    if readonly:
        basic.file_attributes |= _FILE_ATTRIBUTE_READONLY
    else:
        basic.file_attributes &= ~_FILE_ATTRIBUTE_READONLY
    if not api.kernel32.SetFileInformationByHandle(
        handle.value,
        _FILE_BASIC_INFO_CLASS,
        ctypes.byref(basic),
        ctypes.sizeof(basic),
    ):
        raise _windows_error(
            _last_error(),
            "cannot set protected-cache read-only state",
            handle.path,
        )


def _ensure_no_named_streams(handle: _Handle, label: str) -> None:
    try:
        streams = named_data_streams(handle.path)
    except OSError as exc:
        raise _UntrustedState(
            f"cannot inspect alternate data streams on {label}: {exc}"
        ) from exc
    if streams:
        raise _UntrustedState(f"{label} has alternate data streams")


@contextmanager
def _open_or_create_mutable_directory(
    parent: _Handle,
    name: str,
    label: str,
    *,
    replacement_parent: _Handle,
    replacement_prefix: str,
    guard: CacheMutationGuard,
) -> Iterator[_Handle]:
    for _attempt in range(8):
        try:
            with _open_protected_child_directory(
                parent,
                name,
                label,
                _MUTABLE_DIRECTORY,
                missing=_MissingState(f"{label} is absent"),
            ):
                pass
        except _MissingState:
            try:
                os.mkdir(parent.path / name)
            except FileExistsError:
                continue
            try:
                with _open_raw_handle(
                    parent.path / name,
                    desired_access=_FILE_ALL_ACCESS,
                ) as created:
                    _require_direct_child(parent, created, name)
                    if not created.standard.directory:
                        raise _UntrustedState(f"{label} is not a directory")
                    _apply_security_profile(created, _MUTABLE_DIRECTORY)
            except BaseException:
                try:
                    os.rmdir(parent.path / name)
                except OSError:
                    pass
                raise
            continue
        except (_UntrustedState, OSError):
            if _attempt < 3:
                time.sleep(0.005)
                continue
            _require_guard(guard)
            moved = _move_aside(
                parent,
                name,
                replacement_parent,
                replacement_prefix,
                missing_ok=True,
            )
            if moved is None:
                continue
            continue
        else:
            with _open_protected_child_directory(
                parent,
                name,
                label,
                _MUTABLE_DIRECTORY,
                missing=_MissingState(f"{label} disappeared"),
            ) as existing:
                yield existing
            return
    raise _UntrustedState(f"could not establish fresh protected {label}")


def _create_stage(
    staging: _Handle,
    artifact_name: str,
    receipt_bytes: bytes,
    source: _Handle,
) -> str:
    stage_name = ""
    for _attempt in range(16):
        candidate = f"entry-{secrets.token_hex(16)}"
        try:
            os.mkdir(staging.path / candidate)
        except FileExistsError:
            continue
        stage_name = candidate
        break
    if not stage_name:
        raise OSError(errno.EEXIST, "could not allocate unique staging directory")

    with _open_raw_handle(
        staging.path / stage_name,
        desired_access=_FILE_ALL_ACCESS,
    ) as entry:
        _require_direct_child(staging, entry, stage_name)
        if not entry.standard.directory:
            raise _UntrustedState("new staging entry is not a directory")
        _apply_security_profile(entry, _MUTABLE_DIRECTORY)

        os.mkdir(entry.path / "bin")
        with _open_raw_handle(
            entry.path / "bin",
            desired_access=_FILE_ALL_ACCESS,
        ) as bin_handle:
            _require_direct_child(entry, bin_handle, "bin")
            _apply_security_profile(bin_handle, _MUTABLE_DIRECTORY)

            with _create_child_file(
                entry,
                RECEIPT_FILENAME,
            ) as receipt_handle:
                _write_all(receipt_handle, receipt_bytes)
                _flush_file(receipt_handle)
                _apply_security_profile(receipt_handle, _SEALED_RECEIPT)

            with _create_child_file(bin_handle, artifact_name) as artifact_handle:
                _copy_handle(source, artifact_handle)
                _flush_file(artifact_handle)
                _apply_security_profile(artifact_handle, _SEALED_ARTIFACT)

            _apply_security_profile(bin_handle, _SEALED_DIRECTORY)
        _apply_security_profile(entry, _SEALED_ENTRY)
    _revalidate_handle(staging, _MUTABLE_DIRECTORY)
    return stage_name


@contextmanager
def _create_child_file(parent: _Handle, name: str) -> Iterator[_Handle]:
    path = parent.path / name
    with _open_raw_handle(
        path,
        desired_access=_FILE_ALL_ACCESS,
        creation_disposition=_CREATE_NEW,
        attributes=_FILE_ATTRIBUTE_NORMAL,
    ) as handle:
        _require_direct_child(parent, handle, name)
        if handle.standard.directory:
            raise _UntrustedState(f"new cache file {name!r} is a directory")
        _apply_security_profile(handle, _MUTABLE_FILE)
        yield handle


def _write_all(handle: _Handle, raw: bytes) -> None:
    api = _api()
    view = memoryview(raw)
    while view:
        chunk = bytes(view[: min(len(view), _READ_CHUNK)])
        buffer = ctypes.create_string_buffer(chunk)
        written = ctypes.c_uint32()
        if not api.kernel32.WriteFile(
            handle.value,
            buffer,
            len(chunk),
            ctypes.byref(written),
            None,
        ):
            raise _windows_error(
                _last_error(),
                "cannot write protected-cache file",
                handle.path,
            )
        if written.value == 0:
            raise OSError(errno.EIO, "short Windows cache write")
        view = view[int(written.value) :]


def _read_chunk(handle: _Handle, size: int) -> bytes:
    api = _api()
    buffer = ctypes.create_string_buffer(size)
    read = ctypes.c_uint32()
    if not api.kernel32.ReadFile(
        handle.value,
        buffer,
        size,
        ctypes.byref(read),
        None,
    ):
        raise _windows_error(
            _last_error(),
            "cannot read protected-cache file",
            handle.path,
        )
    return bytes(buffer.raw[: int(read.value)])


def _seek_start(handle: _Handle) -> None:
    api = _api()
    position = ctypes.c_longlong()
    if not api.kernel32.SetFilePointerEx(
        handle.value,
        0,
        ctypes.byref(position),
        _FILE_BEGIN,
    ):
        raise _windows_error(
            _last_error(),
            "cannot seek protected-cache file",
            handle.path,
        )


def _flush_file(handle: _Handle) -> None:
    if not _api().kernel32.FlushFileBuffers(handle.value):
        raise _windows_error(
            _last_error(),
            "cannot flush protected-cache file",
            handle.path,
        )


def _copy_handle(source: _Handle, destination: _Handle) -> None:
    before = _object_state(source)
    _seek_start(source)
    remaining = before.size
    while remaining:
        chunk = _read_chunk(source, min(_READ_CHUNK, remaining))
        if not chunk:
            raise BuildCacheError(
                "cache_publication_invalid",
                "publication artifact changed while staging",
            )
        _write_all(destination, chunk)
        remaining -= len(chunk)
    if _read_chunk(source, 1):
        raise BuildCacheError(
            "cache_publication_invalid",
            "publication artifact grew while staging",
        )
    if _object_state(source) != before:
        raise BuildCacheError(
            "cache_publication_invalid",
            "publication artifact source changed during staging",
        )


def _read_bounded_handle(handle: _Handle, limit: int, label: str) -> bytes:
    before = _object_state(handle)
    if before.size < 0 or before.size > limit:
        raise _CorruptState(f"{label} size is outside the supported range")
    _seek_start(handle)
    remaining = before.size
    chunks: list[bytes] = []
    while remaining:
        chunk = _read_chunk(handle, min(_READ_CHUNK, remaining))
        if not chunk:
            raise _CorruptState(f"{label} changed while reading")
        chunks.append(chunk)
        remaining -= len(chunk)
    if _read_chunk(handle, 1):
        raise _CorruptState(f"{label} grew while reading")
    if _object_state(handle) != before:
        raise _CorruptState(f"{label} changed while reading")
    return b"".join(chunks)


def _hash_handle(
    handle: _Handle,
    *,
    expected_size: int,
    label: str,
    error_factory: Callable[[str], Exception],
) -> tuple[str, int]:
    before = _object_state(handle)
    if before.size != expected_size:
        raise error_factory(f"{label} size does not match the receipt")
    _seek_start(handle)
    remaining = expected_size
    digest = hashlib.sha256()
    while remaining:
        chunk = _read_chunk(handle, min(_READ_CHUNK, remaining))
        if not chunk:
            raise error_factory(f"{label} changed while reading")
        digest.update(chunk)
        remaining -= len(chunk)
    if _read_chunk(handle, 1):
        raise error_factory(f"{label} grew while reading")
    if _object_state(handle) != before:
        raise error_factory(f"{label} changed while reading")
    return f"sha256:{digest.hexdigest()}", expected_size


def provision_manager_home(path: Path) -> None:
    """Stamp the manager profile on a manager home this process just created.

    Windows takes new-object ownership from the process token's owner, so a
    directory an elevated administrator creates belongs to the Administrators
    group rather than to the manager principal, and it inherits the DACL of
    whatever contains it. Provisioning gives a home the manager just created
    the private state the protected cache requires. Only creation provisions:
    an existing home is never repaired, so real ownership drift still fails
    closed.
    """

    try:
        with _open_raw_handle(path, desired_access=_FILE_ALL_ACCESS) as handle:
            if not handle.standard.directory:
                raise BuildCacheError(
                    "cache_boundary_untrusted",
                    "manager home is not a directory",
                )
            _apply_security_profile(handle, _MUTABLE_DIRECTORY)
    except _UntrustedState as exc:
        raise BuildCacheError(
            "cache_boundary_untrusted",
            f"cannot make the manager home private: {exc}",
        ) from exc
    except OSError as exc:
        raise BuildCacheError(
            "cache_boundary_untrusted",
            f"cannot make the manager home private: {exc}",
        ) from exc


def make_publication_source_private(path: Path) -> None:
    """Stamp manager-private ownership and DACL on a manager-built artifact.

    Windows takes new-object ownership from the process token's owner, which is
    the Administrators group for an elevated administrator, and inherits the
    DACL of the containing directory. A freshly compiled artifact therefore
    never starts in the owner-controlled state publication demands, even though
    the manager itself produced it inside its own private operation root. The
    manager applies the same mutable-file profile it applies to every file the
    protected cache creates.
    """

    raw = os.fspath(path)
    if not raw or not os.path.isabs(raw) or os.path.normpath(raw) != raw:
        raise BuildCacheError(
            "cache_publication_invalid",
            "publication artifact source must be a clean absolute path",
        )
    try:
        with _open_raw_handle(
            path,
            desired_access=_FILE_ALL_ACCESS,
        ) as handle:
            if handle.standard.directory:
                raise BuildCacheError(
                    "cache_publication_invalid",
                    "publication artifact source must be a regular non-link file",
                )
            _apply_security_profile(handle, _MUTABLE_FILE)
    except _UntrustedState as exc:
        raise BuildCacheError(
            "cache_publication_invalid",
            f"cannot make the publication artifact source private: {exc}",
        ) from exc
    except OSError as exc:
        raise BuildCacheError(
            "cache_publication_invalid",
            f"cannot make the publication artifact source private: {exc}",
        ) from exc


def _publication_error(detail: str) -> BuildCacheError:
    return BuildCacheError("cache_publication_invalid", detail)


@contextmanager
def _open_publication_source(path: Path) -> Iterator[_Handle]:
    raw = os.fspath(path)
    if not raw or not os.path.isabs(raw) or os.path.normpath(raw) != raw:
        raise BuildCacheError(
            "cache_publication_invalid",
            "publication artifact source must be a clean absolute path",
        )
    with _open_raw_handle(
        path,
        desired_access=_GENERIC_READ | _READ_CONTROL | _FILE_READ_ATTRIBUTES,
    ) as handle:
        _validate_publication_source_handle(handle)
        _ensure_no_named_streams(handle, "publication artifact")
        yield handle
        _validate_source_unchanged(handle)


def _validate_publication_source_handle(handle: _Handle) -> None:
    if handle.standard.directory:
        raise BuildCacheError(
            "cache_publication_invalid",
            "publication artifact source must be a regular non-link file",
        )
    if int(handle.standard.number_of_links) != 1:
        raise BuildCacheError(
            "cache_publication_invalid",
            "publication artifact source must remain singly linked",
        )
    try:
        _validate_manager_home_security(handle)
        current = _revalidate_handle(
            handle,
            None,
            "publication artifact source",
        )
    except _UntrustedState as exc:
        raise BuildCacheError(
            "cache_publication_invalid",
            "publication artifact source is not private, singly linked, "
            f"owner-controlled regular state: {exc}",
        ) from exc
    if current.standard.directory:
        raise BuildCacheError(
            "cache_publication_invalid",
            "publication artifact source must remain a regular non-link file",
        )
    if int(current.standard.number_of_links) != 1:
        raise BuildCacheError(
            "cache_publication_invalid",
            "publication artifact source must remain singly linked",
        )


def _validate_source_unchanged(source: _Handle) -> None:
    retained = _revalidate_handle(
        source,
        None,
        "publication artifact source",
    )
    _validate_publication_source_handle(retained)
    with _open_raw_handle(
        source.path,
        desired_access=_GENERIC_READ | _READ_CONTROL | _FILE_READ_ATTRIBUTES,
    ) as selected:
        if selected.identity != source.identity:
            raise BuildCacheError(
                "cache_publication_invalid",
                "publication artifact source identity changed during staging",
            )
        _validate_publication_source_handle(selected)


def _inspect_entry(
    parent: _Handle,
    entry_name: str,
    expectation: CacheExpectation,
    key: str,
    artifact_name: str,
) -> _VerifiedEntry:
    with _open_protected_child_directory(
        parent,
        entry_name,
        "cache entry",
        _SEALED_ENTRY,
        missing=_MissingState("cache entry is absent"),
    ) as entry:
        names = _directory_names(entry, "cache entry")
        if names != ["bin", RECEIPT_FILENAME]:
            raise _CorruptState("cache entry has unexpected contents")
        with _open_protected_child_file(  # noqa: SIM117
            entry,
            RECEIPT_FILENAME,
            "cache receipt",
            _SEALED_RECEIPT,
            missing=_CorruptState("cache receipt is absent"),
        ) as receipt_handle:
            with _open_protected_child_directory(
                entry,
                "bin",
                "artifact directory",
                _SEALED_DIRECTORY,
                missing=_CorruptState("artifact directory is absent"),
            ) as bin_handle:
                artifact_names = _directory_names(
                    bin_handle,
                    "artifact directory",
                )
                if artifact_names != [artifact_name]:
                    raise _CorruptState(
                        "artifact directory has unexpected contents"
                    )
                with _open_protected_child_file(
                    bin_handle,
                    artifact_name,
                    "cache artifact",
                    _SEALED_ARTIFACT,
                    missing=_CorruptState("cache artifact is absent"),
                ) as artifact_handle:
                    receipt_bytes = _read_bounded_handle(
                        receipt_handle,
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
                    artifact_hash, artifact_size = _hash_handle(
                        artifact_handle,
                        expected_size=receipt.artifact.size,
                        label="cache artifact",
                        error_factory=_CorruptState,
                    )
                    if artifact_hash != receipt.artifact.sha256:
                        raise _CorruptState(
                            "cache artifact hash does not match"
                        )
                    _revalidate_child(
                        parent,
                        entry,
                        entry_name,
                        _SEALED_ENTRY,
                        "cache entry",
                    )
                    return _VerifiedEntry(
                        receipt=receipt,
                        receipt_bytes=receipt_bytes,
                        receipt_sha256=_metadata.receipt_sha256(
                            receipt_bytes
                        ),
                        artifact_sha256=artifact_hash,
                        artifact_size=artifact_size,
                        entry_identity=entry.identity,
                    )


def _inspect_gc_entry(
    parent: _Handle,
    entry_name: str,
    key: str,
    *,
    older_than: float,
) -> tuple[_VerifiedEntry, _ObjectState]:
    with _open_protected_child_directory(
        parent,
        entry_name,
        "cache entry",
        _SEALED_ENTRY,
        missing=_MissingState("cache entry is absent"),
    ) as entry:
        if _filetime_to_unix_seconds(entry.basic.last_write_time) >= older_than:
            raise _YoungEntry
        names = _directory_names(entry, "cache entry")
        if names != ["bin", RECEIPT_FILENAME]:
            raise _CorruptState("cache entry has unexpected contents")
        with _open_protected_child_file(
            entry,
            RECEIPT_FILENAME,
            "cache receipt",
            _SEALED_RECEIPT,
            missing=_CorruptState("cache receipt is absent"),
        ) as receipt_handle:
            receipt_bytes = _read_bounded_handle(
                receipt_handle,
                _MAX_RECEIPT_BYTES,
                "cache receipt",
            )
        receipt = _metadata.read_receipt(receipt_bytes)
        if (
            _metadata.cache_key(receipt.input) != key
            or receipt.cache_key != key
        ):
            raise _CorruptState(
                "cache directory name does not match its canonical receipt input"
            )
        identity = entry.identity
    verified = _inspect_entry(
        parent,
        entry_name,
        CacheExpectation(
            input=receipt.input,
            receipt_sha256=_metadata.receipt_sha256(receipt_bytes),
        ),
        key,
        _artifact_name(receipt.input),
    )
    if verified.entry_identity != identity:
        raise _UntrustedState("cache entry changed during GC inspection")
    with _open_protected_child_directory(
        parent,
        entry_name,
        "cache entry",
        _SEALED_ENTRY,
        missing=_MissingState("cache entry is absent"),
    ) as selected:
        state = _object_state(selected)
    if state.identity != verified.entry_identity:
        raise _UntrustedState("cache entry changed during GC inspection")
    if _filetime_to_unix_seconds(state.last_write_time) >= older_than:
        raise _YoungEntry
    return verified, state


def _filetime_to_unix_seconds(value: int) -> float:
    return value / 10_000_000 - 11_644_473_600


def _directory_names(handle: _Handle, label: str) -> list[str]:
    before = _object_state(handle)
    try:
        names = sorted(os.listdir(handle.path))
    except OSError as exc:
        raise _UntrustedState(f"cannot list {label}: {exc}") from exc
    if _object_state(handle) != before:
        raise _UntrustedState(f"{label} changed while listing")
    return names


def _artifact_files_equal(
    first_parent: _Handle,
    first_entry_name: str,
    second_parent: _Handle,
    second_entry_name: str,
    artifact_name: str,
    *,
    first_entry_path: Path | None = None,
    first_entry_identity: _FileIdentity | None = None,
) -> bool:
    first_path = (
        first_parent.path / first_entry_name
        if first_entry_path is None
        else first_entry_path
    )
    with _open_raw_handle(
        first_path,
        desired_access=_GENERIC_READ | _READ_CONTROL | _FILE_READ_ATTRIBUTES,
    ) as first_entry:
        if first_entry_identity is not None:
            if first_entry.identity != first_entry_identity:
                raise _UntrustedState(
                    "retained staged cache entry identity is no longer selected"
                )
        else:
            _require_direct_child(first_parent, first_entry, first_entry_name)
        _validate_security_profile(
            first_entry,
            _SEALED_ENTRY,
            "first cache entry",
        )
        with _open_protected_child_directory(  # noqa: SIM117
            first_entry,
            "bin",
            "first artifact directory",
            _SEALED_DIRECTORY,
            missing=_CorruptState("first artifact directory is absent"),
        ) as first_bin:
            with _open_protected_child_file(
                first_bin,
                artifact_name,
                "first cache artifact",
                _SEALED_ARTIFACT,
                missing=_CorruptState("first cache artifact is absent"),
            ) as first_artifact:
                with _open_protected_child_directory(
                    second_parent,
                    second_entry_name,
                    "second cache entry",
                    _SEALED_ENTRY,
                    missing=_MissingState("second cache entry is absent"),
                ) as second_entry:
                    with _open_protected_child_directory(
                        second_entry,
                        "bin",
                        "second artifact directory",
                        _SEALED_DIRECTORY,
                        missing=_CorruptState(
                            "second artifact directory is absent"
                        ),
                    ) as second_bin:
                        with _open_protected_child_file(
                            second_bin,
                            artifact_name,
                            "second cache artifact",
                            _SEALED_ARTIFACT,
                            missing=_CorruptState(
                                "second cache artifact is absent"
                            ),
                        ) as second_artifact:
                            return _open_files_equal(
                                first_artifact,
                                second_artifact,
                            )


def _open_files_equal(first: _Handle, second: _Handle) -> bool:
    first_before = _object_state(first)
    second_before = _object_state(second)
    if first_before.size != second_before.size:
        return False
    _seek_start(first)
    _seek_start(second)
    remaining = first_before.size
    while remaining:
        size = min(_READ_CHUNK, remaining)
        first_chunk = _read_chunk(first, size)
        second_chunk = _read_chunk(second, size)
        if first_chunk != second_chunk or not first_chunk:
            return False
        remaining -= len(first_chunk)
    if _read_chunk(first, 1) or _read_chunk(second, 1):
        return False
    if _object_state(first) != first_before:
        raise _UntrustedState("first cache artifact changed while comparing")
    if _object_state(second) != second_before:
        raise _UntrustedState("second cache artifact changed while comparing")
    return True


def _move_aside(
    source_parent: _Handle,
    source_name: str,
    destination_parent: _Handle,
    prefix: str,
    *,
    missing_ok: bool,
    expected_state: _ObjectState | None = None,
) -> str | None:
    source = source_parent.path / source_name
    try:
        with _open_raw_handle(
            source,
            desired_access=_READ_CONTROL | _FILE_READ_ATTRIBUTES | _DELETE,
            missing=_MissingState(f"{source_name} is absent"),
        ) as source_handle:
            _require_direct_child(source_parent, source_handle, source_name)
            if (
                expected_state is not None
                and _object_state(source_handle) != expected_state
            ):
                raise _UntrustedState(
                    "cache entry pathname no longer selects the inspected object"
                )
            for _attempt in range(16):
                destination_name = (
                    f"{prefix}-{secrets.token_hex(8)}"
                )
                destination = destination_parent.path / destination_name
                try:
                    _move_handle_no_replace(
                        source_handle,
                        destination_parent,
                        destination_name,
                    )
                except FileExistsError:
                    continue
                _revalidate_handle(source_parent, None)
                _revalidate_handle(destination_parent, None)
                moved_handle = _revalidate_handle(
                    source_handle,
                    None,
                    allow_path_change=True,
                )
                _require_direct_child(
                    destination_parent,
                    moved_handle,
                    destination_name,
                )
                with _open_raw_handle(
                    destination,
                    desired_access=_READ_CONTROL | _FILE_READ_ATTRIBUTES,
                ) as selected:
                    _require_direct_child(
                        destination_parent,
                        selected,
                        destination_name,
                    )
                    if selected.identity != source_handle.identity:
                        raise _UntrustedState(
                            "quarantined object identity changed during move"
                        )
                return destination_name
            raise OSError(errno.EEXIST, "could not reserve quarantine destination")
    except _MissingState:
        if missing_ok:
            return None
        raise


def _move_handle_no_replace(
    source: _Handle,
    destination_parent: _Handle,
    destination_name: str,
) -> None:
    """Rename the exact open object to one unused child of a held parent."""

    raw_name = destination_name.encode("utf-16-le")
    file_name_offset = _FileRenameInformation.file_name.offset
    buffer = ctypes.create_string_buffer(
        ctypes.sizeof(_FileRenameInformation) + len(raw_name)
    )
    info = _FileRenameInformation.from_buffer(buffer)
    info.replace_if_exists = 0
    info.root_directory = destination_parent.value
    info.file_name_length = len(raw_name)
    ctypes.memmove(
        ctypes.addressof(buffer) + file_name_offset,
        raw_name,
        len(raw_name),
    )
    # The Win32 FileRenameInfo wrapper rejects its advertised relative-root
    # form on current hosted Windows runners.  The native contract explicitly
    # accepts a held RootDirectory handle and therefore binds both endpoints.
    io_status = _IoStatusBlock()
    api = _api()
    status = int(
        api.ntdll.NtSetInformationFile(
            source.value,
            ctypes.byref(io_status),
            ctypes.byref(buffer),
            len(buffer),
            _FILE_RENAME_INFORMATION_CLASS,
        )
    )
    if status >= 0:
        _revalidate_handle(destination_parent, None)
        return
    error = int(api.ntdll.RtlNtStatusToDosError(status))
    if error in {_ERROR_FILE_EXISTS, _ERROR_ALREADY_EXISTS}:
        raise FileExistsError(
            errno.EEXIST,
            f"destination already exists: {destination_name}",
            destination_name,
        )
    raise _windows_error(
        error,
        f"cannot atomically move open cache object to {destination_name}",
    )


def _move_no_replace(source: Path, destination: Path) -> None:
    api = _api()
    if api.kernel32.MoveFileExW(
        _extended_path(source),
        _extended_path(destination),
        _MOVEFILE_WRITE_THROUGH,
    ):
        return
    error = _last_error()
    if error in {_ERROR_FILE_EXISTS, _ERROR_ALREADY_EXISTS}:
        raise FileExistsError(
            errno.EEXIST,
            f"destination already exists: {destination}",
            os.fspath(destination),
        )
    raise _windows_error(
        error,
        f"cannot atomically move {source} to {destination}",
    )


def _remove_stage(staging: _Handle, stage_name: str) -> None:
    stage_path = staging.path / stage_name
    try:
        with _open_raw_handle(
            stage_path,
            desired_access=_READ_CONTROL | _WRITE_DAC | _FILE_READ_ATTRIBUTES,
            missing=_MissingState("staging entry is absent"),
        ) as entry:
            _require_direct_child(staging, entry, stage_name)
            _apply_profile_dacl(entry, _MUTABLE_DIRECTORY)
        with _open_raw_handle(
            stage_path,
            desired_access=_FILE_ALL_ACCESS,
        ) as entry:
            _require_direct_child(staging, entry, stage_name)
            _set_readonly(entry, False)
            _validate_security_profile(
                entry,
                _MUTABLE_DIRECTORY,
                "staging entry",
            )
            _make_known_stage_mutable(entry)
    except _MissingState:
        return
    for root, directories, files in os.walk(
        stage_path,
        topdown=False,
        followlinks=False,
    ):
        root_path = Path(root)
        for name in files:
            os.unlink(root_path / name)
        for name in directories:
            os.rmdir(root_path / name)
    os.rmdir(stage_path)
    _revalidate_handle(staging, _MUTABLE_DIRECTORY)


def _make_known_stage_mutable(entry: _Handle) -> None:
    names = _directory_names(entry, "staging entry")
    if any(name not in {"bin", RECEIPT_FILENAME} for name in names):
        raise _UntrustedState("refusing to clean unexpected staging contents")
    receipt = entry.path / RECEIPT_FILENAME
    if receipt.exists():
        _make_path_mutable(
            receipt,
            _MUTABLE_FILE,
            parent=entry,
            name=RECEIPT_FILENAME,
        )
    bin_path = entry.path / "bin"
    if bin_path.exists():
        with _open_raw_handle(
            bin_path,
            desired_access=_READ_CONTROL | _WRITE_DAC | _FILE_READ_ATTRIBUTES,
        ) as bin_handle:
            _require_direct_child(entry, bin_handle, "bin")
            if not bin_handle.standard.directory:
                raise _UntrustedState("staged bin path is not a directory")
            _apply_profile_dacl(bin_handle, _MUTABLE_DIRECTORY)
        with _open_raw_handle(
            bin_path,
            desired_access=_FILE_ALL_ACCESS,
        ) as bin_handle:
            _require_direct_child(entry, bin_handle, "bin")
            _set_readonly(bin_handle, False)
            _validate_security_profile(
                bin_handle,
                _MUTABLE_DIRECTORY,
                "staged bin directory",
            )
            artifact_names = _directory_names(bin_handle, "staged bin directory")
            if len(artifact_names) > 1:
                raise _UntrustedState(
                    "refusing to clean unexpected staged artifacts"
                )
            for artifact_name in artifact_names:
                _make_path_mutable(
                    bin_path / artifact_name,
                    _MUTABLE_FILE,
                    parent=bin_handle,
                    name=artifact_name,
                )


def _make_path_mutable(
    path: Path,
    profile: _SecurityProfile,
    *,
    parent: _Handle,
    name: str,
) -> None:
    with _open_raw_handle(
        path,
        desired_access=_READ_CONTROL | _WRITE_DAC | _FILE_READ_ATTRIBUTES,
    ) as handle:
        _require_direct_child(parent, handle, name)
        _apply_profile_dacl(handle, profile)
    with _open_raw_handle(
        path,
        desired_access=(
            _READ_CONTROL | _FILE_READ_ATTRIBUTES | _FILE_WRITE_ATTRIBUTES
        ),
    ) as handle:
        _require_direct_child(parent, handle, name)
        _set_readonly(handle, False)
    with _open_raw_handle(path, desired_access=_FILE_ALL_ACCESS) as handle:
        _require_direct_child(parent, handle, name)
        _validate_security_profile(handle, profile, os.fspath(path))


def _artifact_name(build_input: _metadata.GoBuildInput) -> str:
    parts = str(build_input.artifact_path).split("/")
    if len(parts) != 2 or parts[0] != "bin" or not parts[1]:
        raise ValueError("manager-derived artifact path is not a direct bin child")
    name = parts[1]
    if (
        name in {".", ".."}
        or "/" in name
        or "\\" in name
        or ":" in name
        or os.sep in name
    ):
        raise ValueError("manager-derived artifact filename is unsafe")
    return name


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


__all__ = [
    "LIVE_ROOT_NAME",
    "QUARANTINE_ROOT_NAME",
    "RECEIPT_FILENAME",
    "STAGING_ROOT_NAME",
    "WindowsBuildCache",
]
