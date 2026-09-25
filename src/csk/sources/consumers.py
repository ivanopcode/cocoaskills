"""Consumer readers for the source-v1 snapshot store.

Audit, build, projection and install each get one named entry point. The
default entry points serve only verified frozen bytes through
:func:`store.lookup_snapshot`. Install has one explicit recovery seam:
``allow_missing=True`` distinguishes an absent entry from an unreadable or
invalid one, so the publisher can capture the locked source and compare it
before staging. Other consumers never receive that live-source capability.

Like the store, this module is deliberately NOT re-exported from
``csk.sources.__init__`` (see ``csk.sources.store``).
"""

from __future__ import annotations

from pathlib import Path
from typing import Final, Literal, overload

from .errors import SourceError
from .store import StoredSnapshot, lookup_snapshot, lookup_snapshot_if_present

_CONSUMER_AUDIT: Final = "audit"
_CONSUMER_BUILD: Final = "build"
_CONSUMER_PROJECTION: Final = "projection"
_CONSUMER_INSTALL: Final = "install"


def _open(consumer: str, home: Path, skill: str, package: str) -> StoredSnapshot:
    try:
        return lookup_snapshot(home, skill, package)
    except SourceError as exc:
        raise SourceError(exc.code, f"{consumer}: {exc.detail}") from exc


def open_for_audit(home: Path, skill: str, package: str) -> StoredSnapshot:
    """Serve the frozen copy to the audit consumer, or refuse."""
    return _open(_CONSUMER_AUDIT, home, skill, package)


def open_for_build(home: Path, skill: str, package: str) -> StoredSnapshot:
    """Serve the frozen copy to the build consumer, or refuse."""
    return _open(_CONSUMER_BUILD, home, skill, package)


def open_for_projection(home: Path, skill: str, package: str) -> StoredSnapshot:
    """Serve the frozen copy to the projection consumer, or refuse."""
    return _open(_CONSUMER_PROJECTION, home, skill, package)


@overload
def open_for_install(
    home: Path, skill: str, package: str, *, allow_missing: Literal[True]
) -> StoredSnapshot | None: ...


@overload
def open_for_install(
    home: Path, skill: str, package: str, *, allow_missing: Literal[False] = False
) -> StoredSnapshot: ...


def open_for_install(
    home: Path,
    skill: str,
    package: str,
    *,
    allow_missing: bool = False,
) -> StoredSnapshot | None:
    """Serve frozen bytes, optionally distinguishing an absent entry.

    ``allow_missing`` returns ``None`` only when the exact package entry
    is absent. A present entry that cannot be verified still refuses.
    """

    if not allow_missing:
        return _open(_CONSUMER_INSTALL, home, skill, package)
    try:
        return lookup_snapshot_if_present(home, skill, package)
    except SourceError as exc:
        raise SourceError(exc.code, f"{_CONSUMER_INSTALL}: {exc.detail}") from exc


ALL_CONSUMERS: Final = (
    open_for_audit,
    open_for_build,
    open_for_projection,
    open_for_install,
)

__all__ = [
    "ALL_CONSUMERS",
    "open_for_audit",
    "open_for_build",
    "open_for_install",
    "open_for_projection",
]
