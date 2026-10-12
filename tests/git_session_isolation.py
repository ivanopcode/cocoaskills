"""Session-level git isolation for the test suite.

Stdlib only: this module never imports ``csk`` or ``pytest``, so the real
``tests/conftest.py`` session fixture and the throwaway conftests the
isolation tests generate for sub-pytest runs can share one implementation.

Two closed mechanisms (BUG-261004-473myt revision 4) replace the revision-3
AST static gate, a denylist of spawn spellings the rev-3 panels proved
cannot be complete. Both cover EVERY subprocess, however it is spawned
(``subprocess``, ``sh -c``, ``os.exec*``, ``posix_spawn``, absolute-path
git), because both work through the pytest process environment, which every
child inherits:

1. Session-level environment isolation. Before any test runs,
   :func:`sanitize_session_environment` removes every ``GIT_*`` variable
   from ``os.environ`` and pins ``HOME``/``XDG_CONFIG_HOME`` to a
   session-private directory, ``GIT_CONFIG_NOSYSTEM=1``,
   ``GIT_CONFIG_GLOBAL`` to a session-private empty file, and
   ``GIT_CEILING_DIRECTORIES`` to the session tmp root. Tests may still set
   their own explicit ``GIT_*`` for their fixture repositories.
2. Tripwire. :func:`snapshot_repositories` records the git state of every
   repository enclosing the test run (plus any enclosing the session tmp
   root) at session start; the teardown compares and fails the session with
   a diff on any change, catching writes no prevention caught.

Stated bounds. ``PATH`` is inherited by design, so a ``PATH`` shim remains
a stated bound (same as the per-child runner). Non-``GIT_*`` git inputs
(``EMAIL``, proxies) are not managed. For a linked worktree checkout, the
common dir's branch refs/``HEAD``/``packed-refs``/index are shared with
sibling checkouts and move under concurrent runs, so only the common
``config`` is watched there; everything else watched is worktree-private.
"""

from __future__ import annotations

import contextlib
import difflib
import hashlib
import os
from pathlib import Path
from typing import Any, Iterator

#: Marker left in the process environment once isolation is active. Tests
#: assert it to prove the fixture ran before them (a missing marker is a
#: loud no-isolation signal, not a silent pass).
SESSION_MARKER = "CSK_GIT_SESSION_ISOLATED"

#: Name of the session-private empty global gitconfig inside the private dir.
_PRIVATE_GLOBAL_CONFIG_NAME = "global-config"

#: Cap on rendered tripwire diff lines; the report names the truncation.
_MAX_DIFF_LINES = 120


class CeilingUnsafeError(ValueError):
    """The session tmp root would defeat ``GIT_CEILING_DIRECTORIES``."""


class TripwireBreach(RuntimeError):
    """Git state outside the test fixtures changed during the session."""


def ensure_ceiling_safe(ceiling: str | os.PathLike[str]) -> str:
    """Return ``ceiling`` as a string, refusing path-list separators in it.

    Git splits ``GIT_CEILING_DIRECTORIES`` on ``:`` (``;`` on Windows), so
    a fixture tmp root containing the separator silently loses the ceiling
    and fixture git commands can discover -- and write to -- an enclosing
    repository. On Windows exactly the drive-letter colon (``C:\\...``) is
    allowed; any other colon fails the session.
    """
    text = os.fspath(ceiling)
    bad: str | None = None
    if os.pathsep in text:
        bad = os.pathsep
    elif ":" in text:
        if os.name == "nt":
            rest = text[2:] if len(text) >= 2 and text[1] == ":" else text
            if ":" in rest:
                bad = ":"
        else:
            bad = ":"
    if bad is not None:
        raise CeilingUnsafeError(
            "refusing to run tests: the session tmp root "
            f"{text!r} contains the path-list separator {bad!r}, so git "
            "would split GIT_CEILING_DIRECTORIES there and the ceiling "
            "would not hold -- fixture git commands could discover and "
            "write to an enclosing repository. Re-run with TMPDIR (or "
            f"--basetemp) set to a path without {bad!r}."
        )
    return text


def sanitize_session_environment(
    private_dir: str | os.PathLike[str],
    *,
    ceiling: str | os.PathLike[str],
) -> dict[str, str]:
    """Scrub ``GIT_*`` from this process and pin session-private git inputs.

    Removes every ``GIT_*`` variable (matched case-insensitively: the
    Windows C runtime resolves them case-insensitively too), points
    ``HOME``/``XDG_CONFIG_HOME`` at ``private_dir`` (created), pins
    ``GIT_CONFIG_NOSYSTEM=1`` with ``GIT_CONFIG_GLOBAL`` at a fresh empty
    file there, and pins ``GIT_CEILING_DIRECTORIES`` to ``ceiling``.
    Returns the removed variables for diagnostics. Every child process
    spawned afterwards -- by any test, through any spawn spelling --
    inherits this environment.
    """
    private = Path(private_dir)
    private.mkdir(parents=True, exist_ok=True)
    global_config = private / _PRIVATE_GLOBAL_CONFIG_NAME
    global_config.write_bytes(b"")
    removed: dict[str, str] = {}
    for key in list(os.environ):
        if key.upper().startswith("GIT_"):
            removed[key] = os.environ.pop(key)
    os.environ["HOME"] = str(private)
    os.environ["XDG_CONFIG_HOME"] = str(private)
    os.environ["GIT_CONFIG_NOSYSTEM"] = "1"
    os.environ["GIT_CONFIG_GLOBAL"] = str(global_config)
    os.environ["GIT_CEILING_DIRECTORIES"] = os.fspath(ceiling)
    os.environ[SESSION_MARKER] = "1"
    return removed


def _resolve_gitfile_pointer(dot_git: Path) -> Path | None:
    """Resolve a ``.git`` file or symlink to its target directory."""
    try:
        if dot_git.is_symlink():
            target = Path(os.readlink(dot_git))
            return target if target.is_absolute() else dot_git.parent / target
        if not dot_git.is_file():
            return None
        first_line = dot_git.read_bytes().decode("utf-8", errors="replace").split("\n")[0]
    except OSError:
        return None
    if not first_line.startswith("gitdir:"):
        return None
    target = Path(first_line[len("gitdir:"):].strip())
    return target if target.is_absolute() else dot_git.parent / target


def _resolve_common_dir(git_dir: Path) -> Path | None:
    """Return the common dir when ``git_dir`` is a linked-worktree gitdir."""
    commondir = git_dir / "commondir"
    try:
        if not commondir.is_file():
            return None
        target = Path(commondir.read_text(encoding="utf-8", errors="replace").strip())
    except OSError:
        return None
    resolved = target if target.is_absolute() else git_dir / target
    try:
        if os.path.realpath(resolved) == os.path.realpath(git_dir):
            return None
    except OSError:
        return None
    return resolved


def find_enclosing_repo(start: str | os.PathLike[str]) -> tuple[Path, Path, Path | None] | None:
    """Locate the repository enclosing ``start``.

    Returns ``(worktree_root, git_dir, common_dir)`` where ``common_dir``
    is set only for a linked worktree (its shared dir, whose ``config`` is
    where ``git config`` writes land), else ``None`` meaning ``git_dir``
    itself. A ``.git`` directory, a ``gitdir:`` pointer file, a ``.git``
    symlink, and a bare layout (``HEAD`` + ``objects`` + ``refs``) all
    resolve; anything unresolvable keeps walking up. ``None`` when no
    enclosing repository exists (e.g. tests running from an export).
    """
    try:
        current = Path(start).resolve()
    except OSError:
        return None
    try:
        if current.is_file():
            current = current.parent
    except OSError:
        return None
    for candidate in (current, *current.parents):
        dot_git = candidate / ".git"
        try:
            if dot_git.is_dir() and not dot_git.is_symlink():
                return (candidate, dot_git, _resolve_common_dir(dot_git))
            if dot_git.is_symlink() or dot_git.is_file():
                target = _resolve_gitfile_pointer(dot_git)
                if target is not None:
                    return (candidate, target, _resolve_common_dir(target))
                return (candidate, dot_git, None)
            bare = (
                (candidate / "HEAD").is_file()
                and (candidate / "objects").is_dir()
                and (candidate / "refs").is_dir()
            )
        except OSError:
            continue
        if bare:
            return (candidate, candidate, _resolve_common_dir(candidate))
    return None


def _read_bytes(path: Path) -> bytes | None:
    try:
        return path.read_bytes()
    except OSError:
        return None


def _read_entry(dot_git: Path) -> tuple[str, Any]:
    """Describe the ``.git`` entry itself: dir, file bytes, link, or bare."""
    try:
        if dot_git.is_symlink():
            return ("symlink", os.readlink(dot_git))
        if dot_git.is_dir():
            return ("dir", None)
        if dot_git.is_file():
            return ("file", _read_bytes(dot_git))
    except OSError as exc:
        return ("error", str(exc))
    return ("bare", None)


def _read_index(path: Path) -> tuple[int, int, str] | None:
    """Snapshot the index as ``(size, mtime_ns, sha256)``; ``None`` if absent."""
    try:
        stat = path.stat()
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
    except OSError:
        return None
    return (stat.st_size, stat.st_mtime_ns, digest)


def _read_refs(git_dir: Path) -> dict[str, str]:
    """Map every file under ``refs/`` to its sha256 hex digest."""
    found: dict[str, str] = {}
    refs = git_dir / "refs"
    try:
        entries = sorted(refs.rglob("*"))
    except OSError:
        return found
    for path in entries:
        try:
            if not path.is_file() or path.is_symlink():
                continue
            rel = path.relative_to(refs).as_posix()
            found[rel] = hashlib.sha256(path.read_bytes()).hexdigest()
        except OSError:
            continue
    return found


def snapshot_repositories(starts: list[str | os.PathLike[str]]) -> dict[str, dict[str, Any]]:
    """Snapshot git state for every repository enclosing any of ``starts``.

    Each entry records the ``.git`` entry itself plus, from the worktree
    gitdir, ``config``/``HEAD``/``packed-refs`` bytes, the ``refs/`` tree
    digests, and the index stat -- and, for a linked worktree, the common
    dir's ``config`` bytes (where its config writes land). Common
    refs/``HEAD``/``packed-refs``/index are deliberately NOT watched: they
    are shared with sibling checkouts and move under concurrent runs. Repos
    are deduplicated by resolved gitdir.
    """
    snapshots: dict[str, dict[str, Any]] = {}
    seen: set[str] = set()
    for start in starts:
        located = find_enclosing_repo(start)
        if located is None:
            continue
        worktree, git_dir, common = located
        try:
            identity = os.path.realpath(git_dir)
        except OSError:
            identity = os.fspath(git_dir)
        if identity in seen:
            continue
        seen.add(identity)
        entry: dict[str, Any] = {
            "worktree": os.fspath(worktree),
            "gitdir": os.fspath(git_dir),
            "common": os.fspath(common) if common is not None else None,
            ".git-entry": _read_entry(worktree / ".git"),
            "config": _read_bytes(git_dir / "config"),
            "HEAD": _read_bytes(git_dir / "HEAD"),
            "packed-refs": _read_bytes(git_dir / "packed-refs"),
            "refs": _read_refs(git_dir),
            "index": _read_index(git_dir / "index"),
        }
        if common is not None:
            entry["common-config"] = _read_bytes(common / "config")
        snapshots[os.fspath(worktree)] = entry
    return snapshots


def _diff_bytes_lines(name: str, before: bytes | None, after: bytes | None) -> list[str]:
    if before is None and after is None:
        return []
    if before is None:
        assert after is not None
        return [f"{name}: created ({len(after)} bytes)"]
    if after is None:
        return [f"{name}: deleted ({len(before)} bytes)"]
    lines = [f"{name}: {len(before)} bytes -> {len(after)} bytes"]
    old = before.decode("utf-8", errors="replace").splitlines()
    new = after.decode("utf-8", errors="replace").splitlines()
    diff = list(difflib.unified_diff(old, new, lineterm=""))[:40]
    lines.extend(f"    {line}" for line in diff)
    return lines


def _diff_refs(before: dict[str, str], after: dict[str, str]) -> list[str]:
    lines: list[str] = []
    for ref in sorted(set(before) | set(after)):
        old, new = before.get(ref), after.get(ref)
        if old is None:
            lines.append(f"refs/{ref}: created ({new})")
        elif new is None:
            lines.append(f"refs/{ref}: deleted (was {old})")
        elif old != new:
            lines.append(f"refs/{ref}: {old[:12]} -> {new[:12]}")
    return lines[:20]


def diff_snapshots(
    before: dict[str, dict[str, Any]],
    after: dict[str, dict[str, Any]],
) -> list[str]:
    """Render human-readable diff lines between two tripwire snapshots.

    Empty means identical. Repositories appearing or vanishing mid-session
    are reported like any other change.
    """
    lines: list[str] = []
    for worktree in sorted(set(before) | set(after)):
        old, new = before.get(worktree), after.get(worktree)
        if old is None:
            lines.append(f"{worktree}: repository appeared mid-session")
            continue
        if new is None:
            lines.append(f"{worktree}: repository vanished mid-session")
            continue
        if old.get("gitdir") != new.get("gitdir"):
            lines.append(f"{worktree}: gitdir {old.get('gitdir')} -> {new.get('gitdir')}")
        if old.get(".git-entry") != new.get(".git-entry"):
            lines.append(f"{worktree}: .git entry {old.get('.git-entry')!r} -> {new.get('.git-entry')!r}")
        for name in ("config", "HEAD", "packed-refs", "common-config"):
            if old.get(name) != new.get(name):
                lines.extend(
                    f"{worktree}: {line}"
                    for line in _diff_bytes_lines(name, old.get(name), new.get(name))
                )
        if old.get("refs") != new.get("refs"):
            lines.extend(f"{worktree}: {line}" for line in _diff_refs(old["refs"], new["refs"]))
        if old.get("index") != new.get("index"):
            lines.append(f"{worktree}: index {old.get('index')!r} -> {new.get('index')!r}")
    if len(lines) > _MAX_DIFF_LINES:
        lines = lines[:_MAX_DIFF_LINES] + [f"... truncated ({len(lines) - _MAX_DIFF_LINES} more lines)"]
    return lines


def format_tripwire_report(watched: list[str], diffs: list[str]) -> str:
    """Format the session-failing tripwire message for ``diffs``."""
    watched_lines = "\n".join(f"  - {repo}" for repo in watched) or "  (none)"
    diff_lines = "\n".join(diffs)
    return (
        "TRIPWIRE: git state outside the test fixtures changed during this session.\n"
        "A test (or a child it spawned, through any spawn spelling) wrote to an "
        "enclosing repository. Watched repositories:\n"
        f"{watched_lines}\n"
        "Changes:\n"
        f"{diff_lines}\n"
        "If no test writes to the checkout, look for concurrent git activity in "
        "this checkout or its sibling worktrees."
    )


@contextlib.contextmanager
def session_isolated(tmp_path_factory: Any) -> Iterator[dict[str, Any]]:
    """Isolate this pytest session's git environment and arm the tripwire.

    Setup removes every ``GIT_*`` from ``os.environ`` and pins the
    session-private inputs (see :func:`sanitize_session_environment`), with
    the ceiling at the session tmp root, then snapshots every repository
    enclosing the test run and the session tmp root. Teardown re-snapshots
    and raises :class:`TripwireBreach` on any change. Yields the isolation
    facts (``private_dir``, ``ceiling``, ``watched``).
    """
    base = Path(ensure_ceiling_safe(Path(tmp_path_factory.getbasetemp()).resolve()))
    private = Path(tmp_path_factory.mktemp("csk-git-session-env"))
    sanitize_session_environment(private, ceiling=base)
    starts: list[str | os.PathLike[str]] = [
        Path(__file__).resolve().parent,
        Path.cwd(),
        base,
    ]
    before = snapshot_repositories(starts)
    try:
        yield {"private_dir": private, "ceiling": base, "watched": sorted(before)}
    finally:
        after = snapshot_repositories(starts)
        diffs = diff_snapshots(before, after)
        if diffs:
            raise TripwireBreach(format_tripwire_report(sorted(before), diffs))
