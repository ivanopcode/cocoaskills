"""Platform-neutral identities for the declarative build domain."""

from typing import Final, Literal

from .source import (
    BuildSourceError,
    BuildSourceIdentity,
    FrozenSnapshot,
    InvalidSnapshotError,
    SnapshotMutationError,
    freeze_snapshot,
)


BuildDriver = Literal["go-v1"]

GO_V1_DRIVER: Final[BuildDriver] = "go-v1"
SUPPORTED_BUILD_DRIVERS: Final[frozenset[str]] = frozenset({GO_V1_DRIVER})

__all__ = [
    "BuildDriver",
    "BuildSourceError",
    "BuildSourceIdentity",
    "FrozenSnapshot",
    "GO_V1_DRIVER",
    "InvalidSnapshotError",
    "SUPPORTED_BUILD_DRIVERS",
    "SnapshotMutationError",
    "freeze_snapshot",
]
