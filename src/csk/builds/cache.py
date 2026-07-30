"""Platform-neutral interface for protected immutable build-cache backends.

Logical cache keys, receipt bytes, and artifact-relative paths belong to the
portable build protocol. Physical manager-home layout and protection checks
belong to a backend. Callers therefore address this interface only with a
complete :class:`GoBuildInput`, an optional previously recorded receipt hash,
and a private artifact source.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Protocol, runtime_checkable

from .metadata import BuildReceipt, GoBuildInput

_SHA256_IDENTITY = re.compile(r"sha256:[0-9a-f]{64}\Z")


class BuildCacheError(RuntimeError):
    """Stable protected-cache failure."""

    def __init__(self, code: str, detail: str):
        self.code = code
        self.detail = detail
        super().__init__(f"{code}: {detail}")


class CacheConflictError(BuildCacheError):
    """Different bytes competed for one logical cache key."""

    def __init__(self, cache_key: str):
        self.cache_key = cache_key
        super().__init__(
            "cache_publication_conflict",
            f"different bytes were published for logical key {cache_key}",
        )


class CacheEntryStatus(str, Enum):
    """Read-only protected-cache lookup outcome."""

    HIT = "hit"
    MISS = "miss"
    CORRUPT = "corrupt"
    UNTRUSTED_PROVENANCE = "untrusted-provenance"
    UNSUPPORTED = "unsupported"


class CachePublicationStatus(str, Enum):
    """Atomic publication outcome."""

    PUBLISHED = "published"
    REUSED_WINNER = "reused-winner"


@dataclass(frozen=True)
class CacheExpectation:
    """Complete independently derived state required for one lookup."""

    input: GoBuildInput
    receipt_sha256: str | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.input, GoBuildInput):
            raise TypeError("cache expectation input must be a GoBuildInput")
        if self.receipt_sha256 is not None:
            _require_sha256(self.receipt_sha256, "expected receipt SHA-256")


@dataclass(frozen=True)
class CacheInspection:
    """One read-only lookup result.

    Physical artifact paths are exposed only for exact protected hits. A later
    consumer must inspect again before publishing a launcher or otherwise
    relying on the path.
    """

    status: CacheEntryStatus
    reason: str
    receipt: BuildReceipt | None = None
    receipt_bytes: bytes | None = None
    receipt_sha256: str | None = None
    artifact_path: Path | None = None

    @property
    def dry_run_outcome(self) -> str:
        """Map this result to the stable manager dry-run vocabulary."""

        if self.status is CacheEntryStatus.HIT:
            return "cache-hit"
        if self.status is CacheEntryStatus.MISS:
            return "would-preflight-and-build"
        if self.status is CacheEntryStatus.UNTRUSTED_PROVENANCE:
            return "would-rebuild-untrusted-cache"
        if self.status is CacheEntryStatus.CORRUPT:
            return "corrupt"
        return "unsupported"

    @property
    def reusable(self) -> bool:
        return self.status is CacheEntryStatus.HIT


@dataclass(frozen=True)
class CachePublication:
    """A verified private build offered for protected publication."""

    input: GoBuildInput
    receipt_bytes: bytes
    artifact_source: Path

    def __post_init__(self) -> None:
        if not isinstance(self.input, GoBuildInput):
            raise TypeError("cache publication input must be a GoBuildInput")
        if not isinstance(self.receipt_bytes, bytes):
            raise TypeError("cache publication receipt_bytes must be bytes")
        object.__setattr__(self, "artifact_source", Path(self.artifact_source))


@dataclass(frozen=True)
class CachePublicationResult:
    """The protected immutable winner selected by publication."""

    status: CachePublicationStatus
    artifact_path: Path
    receipt_sha256: str

    def __post_init__(self) -> None:
        _require_sha256(self.receipt_sha256, "published receipt SHA-256")


@runtime_checkable
class CacheMutationGuard(Protocol):
    """Caller-owned witness for the exclusive manager-home mutation lock."""

    def assert_held(self) -> None:
        """Raise if the exclusive manager-home lock is no longer held."""


@runtime_checkable
class BuildCacheBackend(Protocol):
    """Backend contract shared by POSIX and future Windows protection."""

    @property
    def manager_home(self) -> Path:
        """Return the clean absolute manager home without mutating it."""

    def inspect(self, expectation: CacheExpectation) -> CacheInspection:
        """Inspect one logical key without creating or repairing state."""

    def publish(
        self,
        publication: CachePublication,
        *,
        guard: CacheMutationGuard,
    ) -> CachePublicationResult:
        """Atomically publish or reuse a protected byte-identical winner."""

    def quarantine(
        self,
        cache_key: str,
        *,
        guard: CacheMutationGuard,
    ) -> Path | None:
        """Move one live entry outside the live namespace under the lock."""


def cache_for_manager_home(manager_home: str | os.PathLike[str]) -> BuildCacheBackend:
    """Select the native protected-cache backend without changing state."""

    if os.name == "posix":
        from .cache_posix import PosixBuildCache

        return PosixBuildCache(manager_home)
    raise BuildCacheError(
        "cache_protection_unsupported",
        f"no protected build-cache backend is available for os.name={os.name!r}",
    )


def _require_sha256(value: str, label: str) -> None:
    if not isinstance(value, str) or _SHA256_IDENTITY.fullmatch(value) is None:
        raise ValueError(f"{label} must be sha256 followed by 64 lowercase hex digits")


__all__ = [
    "BuildCacheBackend",
    "BuildCacheError",
    "CacheConflictError",
    "CacheEntryStatus",
    "CacheExpectation",
    "CacheInspection",
    "CacheMutationGuard",
    "CachePublication",
    "CachePublicationResult",
    "CachePublicationStatus",
    "cache_for_manager_home",
]
