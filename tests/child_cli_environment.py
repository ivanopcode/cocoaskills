"""Keep acquisition CLI scratch under its fixture, without changing parent temp defaults."""
from __future__ import annotations

import os
import stat
from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from pathlib import Path

_owner: ContextVar[Path | None] = ContextVar("cli_temp_owner", default=None)


def _remove_tree(path: Path) -> None:
    """Remove joined children's scratch; only directories need unlink permission.

    Fixture children have exited before teardown. lstat keeps foreign symlinks
    out of traversal, and unlink never changes an outside hardlink's inode mode.
    Other filesystem errors propagate to the fixture owner.
    """
    mode = path.lstat().st_mode
    if not stat.S_ISDIR(mode):
        path.unlink()
        return
    path.chmod(stat.S_IMODE(mode) | stat.S_IRWXU)
    for child in path.iterdir():
        _remove_tree(child)
    path.rmdir()


@contextmanager
def owned_cli_environment(root: Path) -> Iterator[None]:
    """Bound child defaults to one fixture; never initialize the tempfile cache."""
    token = _owner.set(root)
    try:
        yield
    finally:
        _owner.reset(token)
        # An unused fixture does not allocate a temp directory at all.
        try:
            root.lstat()
        except FileNotFoundError:
            pass
        else:
            _remove_tree(root)


def child_cli_environment() -> dict[str, str]:
    """Copy inherited settings, then set owned temp defaults before caller overrides.

    Pass this mapping to acquisition CLIs, including fault/replay launchers.
    Nested children inherit it normally. Callers can still update any setting
    explicitly; parent os.environ and tempfile's natural cache stay untouched.
    """
    root = _owner.get()
    if root is None:
        raise AssertionError("acquisition CLI needs an owning test fixture")
    root.mkdir(exist_ok=True)
    return {**os.environ, **{name: str(root) for name in ("TMPDIR", "TEMP", "TMP")}}
