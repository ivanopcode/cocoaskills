"""Keep acquisition CLI scratch under its fixture, without changing parent temp defaults."""
from __future__ import annotations

import os
import stat
from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from pathlib import Path

_owner: ContextVar[Path | None] = ContextVar("cli_temp_owner", default=None)
_WINDOWS = os.name == "nt"


def _remove_tree(path: Path) -> None:
    """Remove joined children's scratch without changing shared file attributes.

    Fixture children have exited before teardown. lstat keeps foreign symlinks
    out of traversal. POSIX files are only unlinked. Windows DeleteFileW needs
    the read-only attribute cleared, but only a single-link regular file is
    exclusively ours to change. Shared read-only files remain a cleanup error.
    """
    mode = path.lstat().st_mode
    if not stat.S_ISDIR(mode):
        try:
            path.unlink()
        except PermissionError:
            info = path.lstat()
            if (
                not _WINDOWS or not stat.S_ISREG(info.st_mode)
                or info.st_nlink != 1 or info.st_mode & stat.S_IWUSR
            ):
                raise
            original = stat.S_IMODE(info.st_mode)
            path.chmod(original | stat.S_IWUSR)
            try:
                path.unlink()
            except OSError as removal:
                # A failed retry must not leave even our private file writable.
                try:
                    path.chmod(original)
                except OSError as restoration:
                    raise ExceptionGroup(
                        "CLI scratch removal and mode restoration failed", [removal, restoration]
                    ) from None
                raise
        return
    path.chmod(stat.S_IMODE(mode) | stat.S_IRWXU)
    for child in path.iterdir():
        _remove_tree(child)
    path.rmdir()


@contextmanager
def owned_cli_environment(root: Path) -> Iterator[None]:
    """Bound child defaults to one fixture; never initialize the tempfile cache."""
    token = _owner.set(root)
    body_error: BaseException | None = None
    try:
        yield
    except BaseException as exc:
        body_error = exc
        raise
    finally:
        _owner.reset(token)
        # An unused fixture does not allocate a temp directory at all.
        try:
            try:
                root.lstat()
            except FileNotFoundError:
                pass
            else:
                _remove_tree(root)
        except (OSError, ExceptionGroup) as cleanup:
            cleanup.add_note(f"Failed to remove fixture-owned CLI scratch: {root}")
            if body_error is not None:
                raise BaseExceptionGroup(
                    "Test body and CLI scratch cleanup failed", [body_error, cleanup]
                ) from None
            raise


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
