"""Stable diagnostics for draft skillfile-sources-v1 (opt-in).

Skillfile schema 2 sources report the nine stable error classes required by
protocol skillfile-sources section 5. Each class is a module constant so that
later leaves (expansion, snapshots, lock, transport) raise identical codes.
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
    }
)


class SourceError(ValueError):
    """One stable draft-sources diagnostic with a machine-readable code."""

    def __init__(self, code: str, detail: str) -> None:
        super().__init__(f"{code}: {detail}")
        self.code = code
        self.detail = detail
