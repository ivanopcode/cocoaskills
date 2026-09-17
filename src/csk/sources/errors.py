"""Stable diagnostics for draft skillfile-sources-v1 (opt-in).

Skillfile schema 2 sources report the nine stable error classes required by
protocol skillfile-sources section 5. Each class is a module constant so that
later leaves (expansion, snapshots, lock, transport) raise identical codes.

The local-snapshot inventory also has a typed path-conflict error. It is kept
separate from the selector/name conflict because it names the two package
relative paths that cannot coexist in one admitted inventory.
"""

from __future__ import annotations

from typing import Final

CODE_ALIAS_UNKNOWN: Final = "source_alias_unknown"
CODE_SELECTION_INVALID: Final = "source_selection_invalid"
CODE_MEMBER_MISSING: Final = "source_member_missing"
CODE_MEMBER_INVALID: Final = "source_member_invalid"
CODE_NAME_CONFLICT: Final = "source_name_conflict"
CODE_OUTPUT_OVERLAP: Final = "source_output_overlap"
CODE_SNAPSHOT_CHANGED: Final = "source_snapshot_changed"
CODE_SNAPSHOT_UNAVAILABLE: Final = "source_snapshot_unavailable"
CODE_LOCK_STALE: Final = "source_lock_stale"
CODE_PATH_CONFLICT: Final = "source_path_conflict"
CODE_INVENTORY_INVALID: Final = "source_inventory_invalid"
CODE_PATH_EQUIVALENCE_INVALID: Final = "source_path_equivalence_invalid"

SOURCE_DIAGNOSTICS: Final[frozenset[str]] = frozenset(
    {
        CODE_ALIAS_UNKNOWN,
        CODE_SELECTION_INVALID,
        CODE_MEMBER_MISSING,
        CODE_MEMBER_INVALID,
        CODE_NAME_CONFLICT,
        CODE_OUTPUT_OVERLAP,
        CODE_SNAPSHOT_CHANGED,
        CODE_SNAPSHOT_UNAVAILABLE,
        CODE_LOCK_STALE,
        CODE_PATH_CONFLICT,
        CODE_INVENTORY_INVALID,
        CODE_PATH_EQUIVALENCE_INVALID,
    }
)


class SourceError(ValueError):
    """One stable draft-sources diagnostic with a machine-readable code."""

    def __init__(self, code: str, detail: str) -> None:
        super().__init__(f"{code}: {detail}")
        self.code = code
        self.detail = detail


class SourcePathConflictError(SourceError):
    """Two admitted package-relative paths occupy one filesystem name."""

    def __init__(self, first_path: str, second_path: str) -> None:
        self.first_path = first_path
        self.second_path = second_path
        super().__init__(
            CODE_PATH_CONFLICT,
            f"inventory paths {first_path!r} and {second_path!r} collide",
        )
