"""Session-level git isolation for the test suite.

Stdlib only: this module never imports ``csk`` or ``pytest``, so the real
``tests/conftest.py`` session fixture and the throwaway conftests the
isolation tests generate for sub-pytest runs can share one implementation.

Two closed mechanisms (BUG-261004-473myt) replace the revision-3 AST static
gate, a denylist of spawn spellings the rev-3 panels proved cannot be
complete. Both cover EVERY subprocess, however it is spawned
(``subprocess``, ``sh -c``, ``os.exec*``, ``posix_spawn``, absolute-path
git), because both work through the pytest process environment, which every
child inherits:

1. Prevention: session-level environment isolation. Before any test runs,
   :func:`sanitize_session_environment` removes every ``GIT_*`` variable
   from ``os.environ`` and pins ``HOME``/``XDG_CONFIG_HOME`` to a
   session-private directory, ``GIT_CONFIG_NOSYSTEM=1``,
   ``GIT_CONFIG_GLOBAL`` to a session-private empty file, and
   ``GIT_CEILING_DIRECTORIES`` to the session tmp root. Tests may still set
   their own explicit ``GIT_*`` for their fixture repositories.
2. Detection: the tripwire. :func:`snapshot_repositories` records, for every
   repository enclosing the test run (plus any enclosing the session tmp
   root), the surfaces below at session start; the teardown compares and
   fails the session with a diff on any change. Layout-aware: the checkout
   is located and its git dir and common dir are resolved by git itself
   (``git rev-parse --absolute-git-dir`` / ``--git-common-dir``), so a normal
   checkout and a linked worktree are watched alike.

   ==============================  ======================================
   Watched (:data:`WATCHED_FIELDS`)  How
   ==============================  ======================================
   ``config``                      bytes of the COMMON dir ``config``
   ``config.worktree``             bytes, this checkout's git dir, if any
   ``HEAD``                        bytes of THIS checkout's ``HEAD``
   ``HEAD-target``                 commit the checked-out branch resolves to
   ``hooks``                       file list plus sha256 of each hook
   ``info/exclude``                bytes of the common ``info/exclude``
   ``index``                       ``git ls-files --stage`` content of this
                                   checkout; never stat or mtime
   ==============================  ======================================

Stated bounds -- NOT watched, because concurrent legitimate git activity
changes them: other branches, tags and ``packed-refs`` of a shared
repository (sibling worktrees and other agents move them); reflogs; the
object store. A test that only creates a tag or a branch in the enclosing
repository is therefore not detected. ``PATH`` is inherited by design, so a
``PATH`` shim remains a stated bound (same as the per-child runner), and
non-``GIT_*`` git inputs (``EMAIL``, proxies) are not managed. ``core.hooksPath``
relocating hooks is caught only through the watched ``config``.
"""

from __future__ import annotations

import contextlib
import difflib
import hashlib
import os
import subprocess
from collections.abc import Mapping
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


def session_git_environment(
    base: Mapping[str, str],
    private_dir: str | os.PathLike[str],
    *,
    ceiling: str | os.PathLike[str],
) -> dict[str, str]:
    """Return ``base`` with ``GIT_*`` scrubbed and session-private inputs pinned.

    Pure: ``base`` is not modified. Every ``GIT_*`` variable is dropped
    (matched case-insensitively: the Windows C runtime resolves them
    case-insensitively too), ``HOME``/``XDG_CONFIG_HOME`` point at
    ``private_dir``, ``GIT_CONFIG_NOSYSTEM=1`` is set with
    ``GIT_CONFIG_GLOBAL`` at the session-private empty file, and
    ``GIT_CEILING_DIRECTORIES`` is pinned to ``ceiling``.
    """
    private = Path(private_dir)
    env = {key: value for key, value in base.items() if not key.upper().startswith("GIT_")}
    env["HOME"] = str(private)
    env["XDG_CONFIG_HOME"] = str(private)
    env["GIT_CONFIG_NOSYSTEM"] = "1"
    env["GIT_CONFIG_GLOBAL"] = str(private / _PRIVATE_GLOBAL_CONFIG_NAME)
    env["GIT_CEILING_DIRECTORIES"] = os.fspath(ceiling)
    env[SESSION_MARKER] = "1"
    return env


def sanitize_session_environment(
    private_dir: str | os.PathLike[str],
    *,
    ceiling: str | os.PathLike[str],
) -> dict[str, str]:
    """Scrub ``GIT_*`` from this process and pin session-private git inputs.

    Applies :func:`session_git_environment` to ``os.environ`` after creating
    ``private_dir`` and its empty global gitconfig. Returns the removed
    variables for diagnostics. Every child process spawned afterwards -- by
    any test, through any spawn spelling -- inherits this environment.
    """
    private = Path(private_dir)
    private.mkdir(parents=True, exist_ok=True)
    (private / _PRIVATE_GLOBAL_CONFIG_NAME).write_bytes(b"")
    removed = {key: value for key, value in os.environ.items() if key.upper().startswith("GIT_")}
    pinned = session_git_environment(os.environ, private, ceiling=ceiling)
    for key in removed:
        os.environ.pop(key, None)
    os.environ.update(pinned)
    return removed


#: Every field :func:`snapshot_repositories` watches, in report order.
WATCHED_FIELDS = (
    "config",
    "config.worktree",
    "HEAD",
    "HEAD-target",
    "hooks",
    "info/exclude",
    "index",
)

#: A value that could not be read. An absence is ``None``; a failed or
#: partial read is this marker plus the reason, never a legitimate absence.
UNREADABLE = "unreadable"


def _discovery_env() -> dict[str, str]:
    """Environment for the tripwire's own read-only git children.

    No ``GIT_*`` redirector and no ceiling (the tripwire must see a
    repository ENCLOSING the session tmp root, which the session ceiling
    exists to hide from fixtures); global/system config off; optional index
    refresh writes off; ``C`` locale so refusals are recognisable.
    """
    env = {k: v for k, v in os.environ.items() if not k.upper().startswith("GIT_")}
    env["GIT_CONFIG_NOSYSTEM"] = "1"
    env["GIT_CONFIG_GLOBAL"] = os.devnull
    env["GIT_OPTIONAL_LOCKS"] = "0"
    env["LC_ALL"] = "C"
    return env


def _git(args: list[str], *, cwd: str | os.PathLike[str] | None = None) -> subprocess.CompletedProcess[bytes]:
    """Run one read-only tripwire git child in the isolated discovery env."""
    return subprocess.run(
        ["git", "-c", "safe.directory=*", *args],
        cwd=cwd,
        env=_discovery_env(),
        capture_output=True,
        check=False,
        timeout=120,
    )


def _text(raw: bytes) -> str:
    return raw.decode("utf-8", errors="replace").strip()


def _failed(proc: subprocess.CompletedProcess[bytes]) -> tuple[str, str]:
    return (UNREADABLE, f"git exit {proc.returncode}: {_text(proc.stderr)[:200]}")


def _existing_dir(start: str | os.PathLike[str]) -> Path | None:
    """The nearest existing directory at or above ``start``."""
    try:
        current = Path(start).resolve()
        if current.is_file():
            current = current.parent
        for candidate in (current, *current.parents):
            if candidate.is_dir():
                return candidate
    except OSError:
        return None
    return None


def find_enclosing_repo(start: str | os.PathLike[str]) -> tuple[Path | None, Path, Path] | None:
    """Locate the repository enclosing ``start`` through git itself.

    Returns ``(worktree_root, git_dir, common_dir)``. ``git_dir`` is THIS
    checkout's private git dir; ``common_dir`` is the shared one
    (``git rev-parse --git-common-dir``) -- the same directory for a normal
    checkout, the main repository's for a linked worktree. ``worktree_root``
    is ``None`` for a bare repository. ``None`` when no repository encloses
    ``start`` (e.g. tests running from an export). Any other git failure
    raises :class:`RuntimeError`: an unreadable repository must not read as
    an absent one.
    """
    cwd = _existing_dir(start)
    if cwd is None:
        return None
    dirs = _git(["rev-parse", "--absolute-git-dir", "--git-common-dir"], cwd=cwd)
    if dirs.returncode != 0:
        if "not a git repository" in _text(dirs.stderr):
            return None
        raise RuntimeError(f"tripwire cannot resolve the repository at {cwd}: {_text(dirs.stderr)}")
    lines = _text(dirs.stdout).splitlines()
    if len(lines) != 2:
        raise RuntimeError(f"tripwire got unexpected rev-parse output at {cwd}: {lines!r}")
    git_dir = Path(os.path.normpath(os.path.join(cwd, lines[0])))
    common_dir = Path(os.path.normpath(os.path.join(cwd, lines[1])))
    top = _git(["rev-parse", "--show-toplevel"], cwd=cwd)
    root = Path(_text(top.stdout)) if top.returncode == 0 and _text(top.stdout) else None
    return (root, git_dir, common_dir)


def _read_bytes(path: Path) -> bytes | tuple[str, str] | None:
    """File bytes; ``None`` when absent; :data:`UNREADABLE` on any other failure."""
    try:
        return path.read_bytes()
    except FileNotFoundError:
        return None
    except OSError as exc:
        return (UNREADABLE, str(exc))


def _read_hooks(hooks: Path) -> dict[str, str] | tuple[str, str] | None:
    """Map every file under ``hooks/`` to its sha256; ``None`` when no dir."""
    if not hooks.is_dir():
        return None
    found: dict[str, str] = {}
    try:
        entries = sorted(hooks.rglob("*"))
        for path in entries:
            if path.is_file():
                found[path.relative_to(hooks).as_posix()] = hashlib.sha256(path.read_bytes()).hexdigest()
    except OSError as exc:
        return (UNREADABLE, str(exc))
    return found


def _read_head_target(git_dir: Path) -> str | tuple[str, str] | None:
    """The commit THIS checkout's HEAD resolves to; ``None`` when unborn."""
    proc = _git(["--git-dir", os.fspath(git_dir), "rev-parse", "--verify", "-q", "HEAD"])
    if proc.returncode == 0:
        return _text(proc.stdout)
    if proc.returncode == 1:
        return None
    return _failed(proc)


def _read_index(root: Path | None) -> str | tuple[str, str] | None:
    """Index CONTENT via ``git ls-files --stage``; never stat or mtime."""
    if root is None:
        return None
    proc = _git(["ls-files", "--stage"], cwd=root)
    if proc.returncode != 0:
        return _failed(proc)
    return proc.stdout.decode("utf-8", errors="replace")


def snapshot_repositories(starts: list[str | os.PathLike[str]]) -> dict[str, dict[str, Any]]:
    """Snapshot the watched surfaces of every repository enclosing ``starts``.

    Keyed by worktree root (the git dir for a bare repository); each entry
    carries ``gitdir`` and ``common`` identity plus ``fields``, one value
    per :data:`WATCHED_FIELDS` name (see the module docstring for what each
    is and what is deliberately not watched). Repos are deduplicated by
    resolved git dir.
    """
    snapshots: dict[str, dict[str, Any]] = {}
    seen: set[str] = set()
    for start in starts:
        located = find_enclosing_repo(start)
        if located is None:
            continue
        worktree, git_dir, common = located
        identity = os.path.realpath(git_dir)
        if identity in seen:
            continue
        seen.add(identity)
        fields: dict[str, Any] = {
            "config": _read_bytes(common / "config"),
            "config.worktree": _read_bytes(git_dir / "config.worktree"),
            "HEAD": _read_bytes(git_dir / "HEAD"),
            "HEAD-target": _read_head_target(git_dir),
            "hooks": _read_hooks(common / "hooks"),
            "info/exclude": _read_bytes(common / "info" / "exclude"),
            "index": _read_index(worktree),
        }
        snapshots[os.fspath(worktree if worktree is not None else git_dir)] = {
            "gitdir": os.fspath(git_dir),
            "common": os.fspath(common),
            "fields": fields,
        }
    return snapshots


def _diff_bytes_lines(name: str, before: Any, after: Any) -> list[str]:
    if isinstance(before, tuple) or isinstance(after, tuple):
        return [f"{name}: {before!r} -> {after!r}"]
    if before is None:
        return [f"{name}: created ({len(after)} bytes)"]
    if after is None:
        return [f"{name}: deleted ({len(before)} bytes)"]
    lines = [f"{name}: {len(before)} bytes -> {len(after)} bytes"]
    old = before.decode("utf-8", errors="replace").splitlines()
    new = after.decode("utf-8", errors="replace").splitlines()
    diff = list(difflib.unified_diff(old, new, lineterm=""))[:40]
    lines.extend(f"    {line}" for line in diff)
    return lines


def _diff_field(name: str, before: Any, after: Any) -> list[str]:
    if name == "hooks":
        before = {} if before is None else before
        after = {} if after is None else after
    if isinstance(before, dict) and isinstance(after, dict):
        lines: list[str] = []
        for entry in sorted(set(before) | set(after)):
            old, new = before.get(entry), after.get(entry)
            if old is None:
                lines.append(f"{name}/{entry}: created ({new})")
            elif new is None:
                lines.append(f"{name}/{entry}: deleted (was {old})")
            elif old != new:
                lines.append(f"{name}/{entry}: {old[:12]} -> {new[:12]}")
        return lines[:20]
    if name == "index" and isinstance(before, str) and isinstance(after, str):
        diff = list(difflib.unified_diff(before.splitlines(), after.splitlines(), lineterm=""))[:40]
        return [f"{name}: changed"] + [f"    {line}" for line in diff]
    if name == "HEAD-target" or isinstance(before, (str, tuple)) or isinstance(after, (str, tuple)):
        return [f"{name}: {before!r} -> {after!r}"]
    return _diff_bytes_lines(name, before, after)


def _changed(
    before: dict[str, dict[str, Any]], after: dict[str, dict[str, Any]]
) -> list[tuple[str, str, list[str]]]:
    """``(repo, field, rendered lines)`` for every difference between snapshots."""
    changes: list[tuple[str, str, list[str]]] = []
    for worktree in sorted(set(before) | set(after)):
        old, new = before.get(worktree), after.get(worktree)
        if old is None:
            changes.append((worktree, "repository", [f"{worktree}: repository appeared mid-session"]))
            continue
        if new is None:
            changes.append((worktree, "repository", [f"{worktree}: repository vanished mid-session"]))
            continue
        for ident in ("gitdir", "common"):
            if old[ident] != new[ident]:
                changes.append((worktree, ident, [f"{worktree}: {ident} {old[ident]} -> {new[ident]}"]))
        for name in WATCHED_FIELDS:
            if old["fields"].get(name) != new["fields"].get(name):
                rendered = _diff_field(name, old["fields"].get(name), new["fields"].get(name))
                changes.append((worktree, name, [f"{worktree}: {line}" for line in rendered]))
    return changes


def changed_fields(
    before: dict[str, dict[str, Any]], after: dict[str, dict[str, Any]]
) -> list[tuple[str, str]]:
    """``(repo, field)`` pairs that differ between two snapshots; empty means identical."""
    return [(repo, field) for repo, field, _ in _changed(before, after)]


def diff_snapshots(
    before: dict[str, dict[str, Any]],
    after: dict[str, dict[str, Any]],
) -> list[str]:
    """Render human-readable diff lines between two tripwire snapshots.

    Empty means identical. Repositories appearing or vanishing mid-session
    are reported like any other change.
    """
    lines = [line for _, _, rendered in _changed(before, after) for line in rendered]
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
