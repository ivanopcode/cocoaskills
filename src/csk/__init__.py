"""CocoaSkill local skill manager package."""
from __future__ import annotations

from importlib.metadata import PackageNotFoundError, version as _metadata_version


def _resolve_version() -> str:
    try:
        return _metadata_version("cocoaskills")
    except PackageNotFoundError:
        try:
            from ._version import __version__ as scm_version  # type: ignore[import-not-found]
        except ImportError:
            return "0.0.0+unknown"
        return scm_version


__version__ = _resolve_version()
