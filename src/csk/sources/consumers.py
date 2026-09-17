"""Consumer readers for the source-v1 snapshot store.

Audit, build, projection and install each get one named entry point. Every
entry point serves the frozen bytes through :func:`store.lookup_snapshot`
and nothing else: no consumer takes a source path, opens a file, or falls
back to live bytes, so a consumer that reads the live authored directory is
not expressible here. A missing locked snapshot fails
``source_snapshot_unavailable`` from every entry point alike; later stories
call these readers and never the store directly.

Like the store, this module is deliberately NOT re-exported from
``csk.sources.__init__`` (see ``csk.sources.store``).
"""

from __future__ import annotations

from pathlib import Path
from typing import Final

from .errors import SourceError
from .store import StoredSnapshot, lookup_snapshot

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


def open_for_install(home: Path, skill: str, package: str) -> StoredSnapshot:
    """Serve the frozen copy to the install consumer, or refuse."""
    return _open(_CONSUMER_INSTALL, home, skill, package)


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
