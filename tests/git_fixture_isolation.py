"""Harden test git fixtures against ambient repository discovery.

Stdlib only: this module never imports ``csk``, so every test helper can share
it. Invariant (BUG-261004-473myt): a test fixture can never change git state
outside its own fixture repository, no matter what ``GIT_*`` variables the
ambient environment carries or where the fixture directory sits.

One shared runner, ``run_fixture_git``, serves every test helper that runs git
against a fixture repository. Each invocation applies three gates:

1. ``scrub_git_child_env`` removes every git redirector the runner does not
   set itself -- the six repository-discovery variables, the ``GIT_CONFIG``
   single-file override, the ``GIT_CONFIG_COUNT``/``KEY_*``/``VALUE_*``
   overlay pairs, and ``GIT_NAMESPACE`` -- then pins
   ``GIT_CEILING_DIRECTORIES`` above the discovery directory and replaces
   ``GIT_CONFIG_GLOBAL``/``GIT_CONFIG_SYSTEM`` with fixture-local paths that
   never exist (config reads see nothing; a config write outside a
   repository fails closed instead of landing in an ambient file). The scrub
   runs after the environment merge, so neither ambient variables nor an
   explicit per-call override can smuggle a redirector into the child.
2. The runner resolves the repository git will actually discover -- the
   ``--git-dir``/``-C`` target when argv names one, else the child's ``cwd``
   -- and, when that directory is repo-shaped, ``check_own_git_dir`` proves
   the discovered git dir is the fixture's own before the child runs.
   Bare repositories (no ``.git`` subdirectory) attest against the bare
   directory itself; linked worktrees (a ``.git`` *file*) resolve the
   ``gitdir:`` pointer. Directories that are not repo-shaped -- a fresh
   ``init``/``clone`` target, a version probe -- skip the attestation
   because there is nothing to attest yet; the scrub is their protection.
3. ``discover_git_dir`` runs ``git rev-parse --absolute-git-dir`` under the
   same scrubbed environment the fixture's children get, and decodes the
   path as UTF-8 explicitly: git emits paths as UTF-8, while the ambient
   locale (cp1252 on Windows runners) would mojibake non-ASCII fixture
   paths and false-refuse.

Stated bounds. Argv itself is author-controlled -- ambient input cannot inject
``-c``/``--work-tree`` flags -- so the runner parses argv only to attest the
right repository, never to police authors. ``GIT_CONFIG_NOSYSTEM`` is left
alone (skipping the system file is only more isolated),
``GIT_DISCOVERY_ACROSS_FILESYSTEM`` is left alone (the ceiling still bounds
upward traversal), and unknown pre-subcommand flags are assumed boolean: a
misparse falls back to cwd attestation or skips it, while the scrub still
applies. ``conftest.run`` takes argv as a parameter, so its git routing is
proven behaviorally (sentinel tests drive it under hostile env), not by the
static guard.
"""

from __future__ import annotations

import os
import subprocess
from collections.abc import Mapping, MutableMapping, Sequence
from pathlib import Path

#: Repository-discovery variables git consults before the working tree. Every
#: one of them can redirect a fixture's writes into another repository:
#: ``GIT_DIR``/``GIT_WORK_TREE`` repoint the repository, ``GIT_INDEX_FILE``
#: repoints ``git add``, ``GIT_COMMON_DIR`` repoints ``git config`` while the
#: reported git dir stays put, ``GIT_OBJECT_DIRECTORY`` repoints new objects,
#: and ``GIT_ALTERNATE_OBJECT_DIRECTORIES`` adds foreign object stores.
GIT_DISCOVERY_ENV_VARS: tuple[str, ...] = (
    "GIT_DIR",
    "GIT_WORK_TREE",
    "GIT_INDEX_FILE",
    "GIT_COMMON_DIR",
    "GIT_OBJECT_DIRECTORY",
    "GIT_ALTERNATE_OBJECT_DIRECTORIES",
)

#: Redirectors removed outright: repository discovery, the ``GIT_CONFIG``
#: single-file override (every config read and write consults it), the
#: ``GIT_CONFIG_COUNT`` overlay, and the ref namespace.
GIT_REMOVED_ENV_VARS: tuple[str, ...] = (
    *GIT_DISCOVERY_ENV_VARS,
    "GIT_CONFIG",
    "GIT_CONFIG_COUNT",
    "GIT_NAMESPACE",
)

#: ``GIT_CONFIG_COUNT`` overlay pair prefixes: ``GIT_CONFIG_KEY_<n>`` carries
#: a config key and ``GIT_CONFIG_VALUE_<n>`` its value. Every indexed pair is
#: removed, not just the count.
GIT_CONFIG_COUNT_PREFIXES: tuple[str, ...] = (
    "GIT_CONFIG_KEY_",
    "GIT_CONFIG_VALUE_",
)

#: Config files replaced (not removed): pointing them at fixture-local paths
#: that never exist keeps config reads empty and fails config writes closed,
#: without ever consulting the ambient user or system files.
GIT_REPLACED_ENV_VARS: tuple[str, ...] = (
    "GIT_CONFIG_GLOBAL",
    "GIT_CONFIG_SYSTEM",
)

FIXTURE_GLOBAL_CONFIG_NAME = "fixture-global-config"
FIXTURE_SYSTEM_CONFIG_NAME = "fixture-system-config"


def scrub_git_child_env(
    env: MutableMapping[str, str],
    discovery_root: str | os.PathLike[str],
) -> MutableMapping[str, str]:
    """Remove redirectors and pin the ceiling and config files for ``discovery_root``.

    Operates in place and returns ``env`` for chaining. ``discovery_root`` is
    the directory git discovers the repository from: the child's ``cwd``, or
    the ``-C``/``--git-dir`` target when the helper passes one. The global
    and system config files are replaced by fixture-local paths that are
    never created: reads see an empty file, writes fail for the missing
    parent instead of reaching an ambient or hostile file.
    """
    for var in GIT_REMOVED_ENV_VARS:
        env.pop(var, None)
    for key in list(env.keys()):
        if key.startswith(GIT_CONFIG_COUNT_PREFIXES):
            env.pop(key, None)
    root = Path(os.path.realpath(discovery_root))
    env["GIT_CEILING_DIRECTORIES"] = str(root.parent)
    env["GIT_CONFIG_GLOBAL"] = str(root / ".git" / FIXTURE_GLOBAL_CONFIG_NAME)
    env["GIT_CONFIG_SYSTEM"] = str(root / ".git" / FIXTURE_SYSTEM_CONFIG_NAME)
    return env


def _resolve_git_target(
    args: Sequence[str], cwd: str | os.PathLike[str] | None
) -> tuple[str | None, str]:
    """Resolve ``(explicit_target, discovery_dir)`` for a git invocation.

    ``explicit_target`` is the ``--git-dir`` value when argv names one, else
    the ``-C`` target with git's own sequential semantics (each ``-C``
    chdirs, relative paths compound), else ``None``. ``discovery_dir`` is
    that target when present, else the child's ``cwd`` (or the inherited
    working directory). Only tokens before the subcommand -- the first token
    that is not a flag -- are parsed as flags, so ``rev-parse --git-dir``
    (a report flag, not a redirect) and everything past ``--`` never
    misresolve. A dangling ``-C``/``--git-dir`` value fails closed.
    """
    tokens = [os.fspath(arg) for arg in args]
    base = os.fspath(cwd) if cwd is not None else os.getcwd()
    directory = base
    explicit: str | None = None
    git_dir: str | None = None
    i = 0
    while i < len(tokens):
        token = tokens[i]
        if token == "--":
            break
        if token in ("-C", "--git-dir", "-c", "--work-tree"):
            i += 1
            if i >= len(tokens) or tokens[i] == "":
                raise AssertionError(
                    "refusing to run git with a dangling "
                    f"{token} flag before any repository is attested: {tokens!r}"
                )
            value = tokens[i]
            if token == "-C":
                directory = value if os.path.isabs(value) else os.path.join(directory, value)
                explicit = directory
            elif token == "--git-dir":
                git_dir = value if os.path.isabs(value) else os.path.join(directory, value)
            i += 1
            continue
        if token.startswith("-C") and len(token) > 2:
            value = token[2:]
            if value == "":
                raise AssertionError(
                    "refusing to run git with a dangling -C flag before any "
                    f"repository is attested: {tokens!r}"
                )
            directory = value if os.path.isabs(value) else os.path.join(directory, value)
            explicit = directory
            i += 1
            continue
        if token.startswith("--git-dir="):
            value = token.split("=", 1)[1]
            if value == "":
                raise AssertionError(
                    "refusing to run git with a dangling --git-dir flag before "
                    f"any repository is attested: {tokens!r}"
                )
            git_dir = value if os.path.isabs(value) else os.path.join(directory, value)
            i += 1
            continue
        if token.startswith("--work-tree="):
            i += 1
            continue
        if token.startswith("-") and token != "-":
            # Unknown pre-subcommand flag: assumed boolean (stated bound).
            i += 1
            continue
        break
    target = git_dir if git_dir is not None else explicit
    return target, (target if target is not None else base)


def _is_repo_shape(path: str) -> bool:
    """Whether ``path`` looks like a repository root: a ``.git`` entry or bare."""
    try:
        if os.path.lexists(os.path.join(path, ".git")):
            return True
        return (
            os.path.isfile(os.path.join(path, "HEAD"))
            and os.path.isdir(os.path.join(path, "objects"))
            and os.path.isdir(os.path.join(path, "refs"))
        )
    except OSError:
        return False


def _expected_git_dir(fixture: str) -> str:
    """Return the git dir a fixture must discover.

    A ``.git`` directory expects itself; a ``.git`` file (linked worktree or
    submodule pointer) resolves its ``gitdir:`` line; a bare layout (``HEAD``,
    ``objects``, ``refs`` at the top) expects the directory itself. Anything
    else falls back to ``fixture/.git`` as a pure path expectation, so a
    caller comparing rev-parse output shapes never false-refuses; in real
    runs the discovered dir then mismatches (or discovery itself fails) and
    the guard refuses. An unreadable or malformed pointer likewise falls
    back and refuses, since rev-parse never reports the pointer path.
    """
    dot_git = os.path.join(fixture, ".git")
    if os.path.isdir(dot_git):
        return dot_git
    if os.path.isfile(dot_git):
        try:
            content = Path(dot_git).read_bytes().decode("utf-8", errors="surrogateescape")
        except OSError:
            return dot_git
        # Split on \n only: str.splitlines() also splits on U+2028/U+2029,
        # which are legal path characters and would truncate the pointer.
        for line in content.split("\n"):
            stripped = line.strip()
            if stripped.startswith("gitdir:"):
                target = stripped[len("gitdir:"):].strip()
                if target == "":
                    return dot_git
                return target if os.path.isabs(target) else os.path.join(fixture, target)
        return dot_git
    try:
        bare = (
            os.path.isfile(os.path.join(fixture, "HEAD"))
            and os.path.isdir(os.path.join(fixture, "objects"))
            and os.path.isdir(os.path.join(fixture, "refs"))
        )
    except OSError:
        bare = False
    if bare:
        return fixture
    return dot_git


def discover_git_dir(fixture: str | os.PathLike[str]) -> str:
    """Return the git directory git discovers for ``fixture``, as UTF-8 text.

    Runs under the same scrubbed environment (and ceiling) the fixture's
    config-writing children get, so the guard observes exactly what they
    would. A nonzero rev-parse -- no repository, e.g. the ceiling blocked
    traversal into an enclosing checkout -- raises AssertionError: fail
    closed through the same channel as a mismatch.
    """
    env = scrub_git_child_env(dict(os.environ), fixture)
    proc = subprocess.run(
        ["git", "rev-parse", "--absolute-git-dir"],
        cwd=fixture,
        capture_output=True,
        check=False,
        encoding="utf-8",
        errors="surrogateescape",
        env=env,
    )
    if proc.returncode != 0:
        raise AssertionError(
            "refusing to write git config outside the fixture repository: "
            f"no git repository discovered in fixture {str(fixture)!r}: "
            f"{proc.stderr.strip()}"
        )
    return proc.stdout


def check_own_git_dir(fixture: str | os.PathLike[str], actual_git_dir: str) -> None:
    """Refuse to write config when the discovered git dir is not the fixture's.

    ``actual_git_dir`` is the stdout of ``git rev-parse --absolute-git-dir``
    run in ``fixture``. Both sides are resolved and case-folded so macOS
    ``/tmp`` symlinks and Windows drive-letter case cannot cause a false
    refusal, and any mismatch -- including garbage output or a malformed
    worktree pointer -- raises.
    """
    expected = _expected_git_dir(str(fixture))
    actual = os.path.normcase(os.path.realpath(actual_git_dir.strip()))
    if actual != os.path.normcase(os.path.realpath(expected)):
        raise AssertionError(
            "refusing to write git config outside the fixture repository: "
            f"discovered git dir {actual_git_dir.strip()!r} is not the "
            f"fixture's {expected!r} (fixture {str(fixture)!r})"
        )


def run_fixture_git(
    args: Sequence[str | os.PathLike[str]],
    *,
    cwd: str | os.PathLike[str] | None = None,
    env: Mapping[str, str] | None = None,
    extra_env: Mapping[str, str] | None = None,
    executable: str | os.PathLike[str] = "git",
    check: bool = True,
    text: bool = True,
    timeout: float | None = None,
    stdin=None,
    capture_output: bool = True,
) -> subprocess.CompletedProcess:
    """Run one git child against a fixture repository, isolated.

    The child environment is ``env`` (default: the ambient environment) plus
    ``extra_env``, scrubbed after the merge: neither ambient variables nor
    an explicit override can smuggle a redirector in. The ceiling and the
    replacement global/system config files are pinned to the directory git
    discovers from -- the ``--git-dir``/``-C`` target when argv names one,
    else ``cwd``. When that directory is repo-shaped the discovered git dir
    is attested against the fixture before the child runs; anything else
    (a fresh ``init``/``clone`` target, a version probe, an explicit-path
    read) runs scrubbed but unattested, because there is no repository to
    attest yet. ``check``/``text``/``timeout``/``stdin``/``capture_output``
    behave exactly like :func:`subprocess.run`. ``argv`` is a tuple, matching
    the convention of the fixture helpers this runner replaces: tests that
    observe the production subprocess seam assert tuple commands, and the
    runner must pass through those seams transparently.
    """
    argv = (os.fspath(executable), *(os.fspath(arg) for arg in args))
    child_env = dict(env) if env is not None else dict(os.environ)
    if extra_env:
        child_env.update(extra_env)
    explicit, discovery = _resolve_git_target([os.fspath(arg) for arg in args], cwd)
    scrub_git_child_env(child_env, discovery)
    if explicit is not None:
        candidate: str | None = explicit
    elif cwd is not None:
        candidate = os.fspath(cwd)
    else:
        candidate = None
    if candidate is not None and _is_repo_shape(candidate):
        check_own_git_dir(candidate, discover_git_dir(candidate))
    return subprocess.run(
        argv,
        cwd=cwd,
        env=child_env,
        text=text,
        capture_output=capture_output,
        timeout=timeout,
        stdin=stdin,
        check=check,
    )
