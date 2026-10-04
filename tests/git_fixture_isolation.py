"""Harden test git fixtures against ambient repository discovery.

Stdlib only: this module never imports ``csk``, so every test helper can share
it. Invariant (BUG-261004-473myt): a test fixture can never change git state
outside its own fixture repository, no matter what ``GIT_*`` variables the
ambient environment carries or where the fixture directory sits.

One shared runner, ``run_fixture_git``, serves every test helper that runs git
against a fixture repository. Each invocation applies three gates:

1. ``build_fixture_git_env`` constructs the child environment from an
   ALLOWLIST only: ``PATH``, ``TMPDIR``/``TEMP``/``TMP`` and (on Windows)
   ``SYSTEMROOT``/``COMSPEC`` are inherited when present; ``LANG``/``LC_ALL``
   are pinned to ``C``; ``HOME`` and ``XDG_CONFIG_HOME`` point at a
   fixture-private directory; ``GIT_CONFIG_NOSYSTEM=1``,
   ``GIT_CONFIG_GLOBAL=<fixture-private empty file>``,
   ``GIT_TEMPLATE_DIR=<fixture-private empty dir>``,
   ``GIT_CEILING_DIRECTORIES=<fixture parent>`` and
   ``GIT_TERMINAL_PROMPT=0`` are pinned. Nothing else is inherited -- every
   documented ``GIT_*`` name (config channels, templates, trace
   destinations, hooks inputs, discovery redirectors) and every future one
   is absent unless the caller passes it through the explicit allowlist.
   Explicit caller entries (``env``/``extra_env``) are filtered to author
   identity (``GIT_AUTHOR_*``/``GIT_COMMITTER_*``) plus operational
   non-redirectors (``GIT_SSH``/``GIT_SSH_COMMAND``/``GIT_SSH_VARIANT``,
   ``GIT_SSL_CAINFO``); everything else a caller passes -- including an
   explicit ``GIT_DIR`` -- is silently dropped, and the runner pins always
   win. Fixed author/committer values ride along as explicit caller entries
   (golden ``PINNED_GIT_ENV``, ssh fixture identity); the runner never
   invents identity.
2. The runner resolves the repository git will actually discover -- the
   ``--git-dir``/``-C`` target when argv names one, else the child's ``cwd``
   -- and, when that directory is repo-shaped, ``check_own_git_dir`` proves
   the discovered git dir is the fixture's own before the child runs. A
   ``.git`` *directory* expects itself; a bare layout (``HEAD``,
   ``objects``, ``refs`` at the top) expects the directory itself; a
   ``.git`` *file* (gitfile pointer, e.g. a linked worktree) is NEVER
   followed -- a mutating command against a gitfile checkout is refused,
   while a read-only command (``rev-parse``, ``cat-file``, ...) runs
   unattested under the allowlist environment, which is what distinguishes
   legitimate read-only worktree access from authority to mutate foreign
   storage. Directories that are not repo-shaped -- a fresh ``init``/``clone``
   target, a version probe -- skip the attestation because there is nothing
   to attest yet; the allowlist environment plus the ceiling is their
   protection.
3. Owned repositories are routed explicitly: a normal ``.git``-directory
   repository gets ``--git-dir=<abs path/.git> --work-tree=<abs path>``
   prepended, a bare repository ``--git-dir=<abs path>``, unless argv already
   carries ``-C``/``--git-dir`` (explicit author routing is attested, never
   rewritten). ``discover_git_dir`` runs ``git rev-parse --absolute-git-dir``
   under the same allowlist environment the fixture's children get, and
   decodes the path as UTF-8 explicitly: git emits paths as UTF-8, while the
   ambient locale (cp1252 on Windows runners) would mojibake non-ASCII
   fixture paths and false-refuse.

Stated bounds. Argv and the git executable are author-controlled -- ambient
input cannot inject ``-c``/``--work-tree`` flags or a ``PATH`` shim -- so the
runner parses argv only to attest the right repository, never to police
authors; a ``PATH`` shim remains a stated bound, as is any git invocation
outside this runner (policed by ``test_fixture_git_runner_guard.py``).
Unknown pre-subcommand flags are assumed boolean and unknown subcommands are
treated as mutating: a misparse falls back to cwd attestation, skips it, or
refuses a gitfile, while the allowlist environment still applies.
``conftest.run`` takes argv as a parameter, so its git routing is proven
behaviorally (sentinel tests drive it under hostile env), not by the static
guard.
"""

from __future__ import annotations

import atexit
import os
import shutil
import subprocess
import tempfile
from collections.abc import Mapping, Sequence
from pathlib import Path

#: Repository-discovery variables git consults before the working tree. Every
#: one of them can redirect a fixture's writes into another repository:
#: ``GIT_DIR``/``GIT_WORK_TREE`` repoint the repository, ``GIT_INDEX_FILE``
#: repoints ``git add``, ``GIT_COMMON_DIR`` repoints ``git config`` while the
#: reported git dir stays put, ``GIT_OBJECT_DIRECTORY`` repoints new objects,
#: and ``GIT_ALTERNATE_OBJECT_DIRECTORIES`` adds foreign object stores.
#: Kept as documentation and as the hostile matrix for the sentinel tests;
#: the runner itself inherits none of them (allowlist, not denylist).
GIT_DISCOVERY_ENV_VARS: tuple[str, ...] = (
    "GIT_DIR",
    "GIT_WORK_TREE",
    "GIT_INDEX_FILE",
    "GIT_COMMON_DIR",
    "GIT_OBJECT_DIRECTORY",
    "GIT_ALTERNATE_OBJECT_DIRECTORIES",
)

#: Ambient variables the runner inherits when present: executable search,
#: temporary-file locations, and the Windows process baseline. Everything
#: else -- every ``GIT_*`` name, ``HOME``, shell and locale settings -- is
#: dropped or pinned below.
AMBIENT_INHERIT_ENV_VARS: tuple[str, ...] = (
    "PATH",
    "TMPDIR",
    "TEMP",
    "TMP",
    "SYSTEMROOT",
    "COMSPEC",
)

#: Author identity the runner accepts from explicit caller entries. Fixture
#: helpers own their committed identity (golden dates, ssh names); ambient
#: identity never leaks into fixture objects because it is never inherited.
EXPLICIT_IDENTITY_ENV_VARS: tuple[str, ...] = (
    "GIT_AUTHOR_NAME",
    "GIT_AUTHOR_EMAIL",
    "GIT_AUTHOR_DATE",
    "GIT_COMMITTER_NAME",
    "GIT_COMMITTER_EMAIL",
    "GIT_COMMITTER_DATE",
)

#: Operational non-redirectors accepted from explicit caller entries. Each of
#: these selects a program or trust input the test author names (an ssh
#: stand-in, a loopback CA); none of them can redirect repository discovery,
#: config destinations, hooks, templates, or trace output. Redirectors in an
#: explicit entry -- ``GIT_DIR``, ``GIT_CONFIG``, ``GIT_CONFIG_PARAMETERS``,
#: ``GIT_TEMPLATE_DIR``, trace destinations, ... -- are silently dropped.
EXPLICIT_OPERATIONAL_ENV_VARS: tuple[str, ...] = (
    "GIT_SSH",
    "GIT_SSH_COMMAND",
    "GIT_SSH_VARIANT",
    "GIT_SSL_CAINFO",
    "GIT_TERMINAL_PROMPT",
    "LANG",
    "LC_ALL",
)

#: Full explicit-entry allowlist: ambient-shaped keys a caller may restate,
#: plus identity and operational entries. Runner pins (``HOME``,
#: ``XDG_CONFIG_HOME``, ``GIT_CONFIG_NOSYSTEM``, ``GIT_CONFIG_GLOBAL``,
#: ``GIT_TEMPLATE_DIR``, ``GIT_CEILING_DIRECTORIES``) always win over
#: explicit entries and are deliberately absent here.
EXPLICIT_ALLOW_ENV_VARS: frozenset[str] = frozenset(
    (
        *AMBIENT_INHERIT_ENV_VARS,
        *EXPLICIT_IDENTITY_ENV_VARS,
        *EXPLICIT_OPERATIONAL_ENV_VARS,
    )
)

#: Subcommands that can neither write git state nor run hooks nor spawn
#: user-controlled programs. Only these may run against a gitfile checkout
#: (linked worktree): everything else -- including ambiguous readers such as
#: ``config``/``branch``/``symbolic-ref``/``worktree`` and every unknown
#: subcommand -- is treated as mutating and refused there. The set is
#: deliberately small: fail closed, and a false refusal names itself in the
#: test that trips it.
READ_ONLY_GIT_COMMANDS: frozenset[str] = frozenset(
    {
        "rev-parse",
        "cat-file",
        "ls-tree",
        "ls-files",
        "rev-list",
        "merge-base",
        "name-rev",
        "count-objects",
        "verify-pack",
        # ``archive`` reads objects and writes stdout (or an author-controlled
        # ``--output`` path): it never mutates git state and runs no hooks,
        # so the golden release extraction may run it against the (gitfile)
        # Story worktree checkout.
        "archive",
        "log",
        "show",
        "status",
        "diff",
        "grep",
        "shortlog",
        "describe",
        "blame",
        "check-ignore",
        "check-attr",
        "var",
        "help",
    }
)

#: Pre-subcommand flags that consume the following token. ``--config-env``
#: and ``--namespace`` take values; anything else starting with ``-`` is
#: assumed boolean (stated bound: a misparse attests the cwd or refuses a
#: gitfile, while the allowlist environment still applies).
_VALUE_FLAGS: frozenset[str] = frozenset(
    {"-C", "--git-dir", "-c", "--work-tree", "--config-env", "--namespace"}
)

_PINNED_LOCALE = "C"
_PRIVATE_GLOBAL_CONFIG_NAME = "global-config"
_PRIVATE_TEMPLATE_NAME = "template"

_private_env_dir: Path | None = None


def _fixture_private_env_dir() -> Path:
    """Return the process-private directory for pinned git inputs (making it).

    Holds the empty global-config file and the empty template directory the
    runner pins every fixture child to. Outside every fixture worktree, so it
    can never be picked up by a fixture ``git add .``; mode 0700, removed at
    exit. No fixture flow writes ``git config --global``, so the empty file
    stays empty for the life of the process.
    """
    global _private_env_dir
    if _private_env_dir is not None:
        return _private_env_dir
    directory = Path(tempfile.mkdtemp(prefix="csk-fixture-git-env-"))
    try:
        (directory / _PRIVATE_GLOBAL_CONFIG_NAME).write_bytes(b"")
        (directory / _PRIVATE_TEMPLATE_NAME).mkdir(exist_ok=True)
    except OSError:
        shutil.rmtree(directory, ignore_errors=True)
        raise
    _private_env_dir = directory

    def _cleanup() -> None:
        shutil.rmtree(directory, ignore_errors=True)

    atexit.register(_cleanup)
    return directory


def build_fixture_git_env(
    discovery_root: str | os.PathLike[str],
    explicit: Mapping[str, str] | None = None,
) -> dict[str, str]:
    """Build a fixture git child environment from the allowlist only.

    ``discovery_root`` is the directory git discovers the repository from:
    the child's ``cwd``, or the ``-C``/``--git-dir`` target when the helper
    passes one. ``explicit`` carries caller entries (``env`` plus
    ``extra_env``), filtered to :data:`EXPLICIT_ALLOW_ENV_VARS`; the runner
    pins always win. Ambient ``os.environ`` contributes only
    :data:`AMBIENT_INHERIT_ENV_VARS`.
    """
    child: dict[str, str] = {}
    for var in AMBIENT_INHERIT_ENV_VARS:
        value = os.environ.get(var)
        if value is not None:
            child[var] = value
    if explicit:
        for key, value in explicit.items():
            if (
                isinstance(key, str)
                and isinstance(value, str)
                and key in EXPLICIT_ALLOW_ENV_VARS
            ):
                child[key] = value
    private = _fixture_private_env_dir()
    root = Path(os.path.realpath(discovery_root))
    child["LANG"] = _PINNED_LOCALE
    child["LC_ALL"] = _PINNED_LOCALE
    child["HOME"] = str(private)
    child["XDG_CONFIG_HOME"] = str(private)
    child["GIT_CONFIG_NOSYSTEM"] = "1"
    child["GIT_CONFIG_GLOBAL"] = str(private / _PRIVATE_GLOBAL_CONFIG_NAME)
    child["GIT_TEMPLATE_DIR"] = str(private / _PRIVATE_TEMPLATE_NAME)
    child["GIT_CEILING_DIRECTORIES"] = str(root.parent)
    child["GIT_TERMINAL_PROMPT"] = "0"
    return child


def _split_pre_subcommand_flags(
    tokens: list[str],
) -> tuple[list[tuple[str, str | None]], str | None]:
    """Split argv into ``([(flag, value)], subcommand)`` before the subcommand.

    Only tokens before the subcommand -- the first token that is not a flag
    -- are parsed as flags, so ``rev-parse --git-dir`` (a report flag, not a
    redirect) and everything past ``--`` never misresolve. A dangling
    ``-C``/``--git-dir``/``-c``/``--work-tree`` value fails closed.
    """
    flags: list[tuple[str, str | None]] = []
    subcommand: str | None = None
    i = 0
    while i < len(tokens):
        token = tokens[i]
        if token == "--":
            break
        if token in _VALUE_FLAGS:
            i += 1
            if i >= len(tokens) or tokens[i] == "":
                raise AssertionError(
                    "refusing to run git with a dangling "
                    f"{token} flag before any repository is attested: {tokens!r}"
                )
            flags.append((token, tokens[i]))
            i += 1
            continue
        if token.startswith("-C") and len(token) > 2:
            value = token[2:]
            if value == "":
                raise AssertionError(
                    "refusing to run git with a dangling -C flag before any "
                    f"repository is attested: {tokens!r}"
                )
            flags.append(("-C", value))
            i += 1
            continue
        if token.startswith("--git-dir="):
            value = token.split("=", 1)[1]
            if value == "":
                raise AssertionError(
                    "refusing to run git with a dangling --git-dir flag before "
                    f"any repository is attested: {tokens!r}"
                )
            flags.append(("--git-dir", value))
            i += 1
            continue
        if token.startswith("--work-tree="):
            flags.append(("--work-tree", token.split("=", 1)[1]))
            i += 1
            continue
        if token.startswith("-") and token != "-":
            # Unknown pre-subcommand flag: assumed boolean (stated bound).
            flags.append((token, None))
            i += 1
            continue
        subcommand = token
        break
    return flags, subcommand


def _resolve_git_target(
    args: Sequence[str], cwd: str | os.PathLike[str] | None
) -> tuple[str | None, str]:
    """Resolve ``(explicit_target, discovery_dir)`` for a git invocation.

    ``explicit_target`` is the ``--git-dir`` value when argv names one, else
    the ``-C`` target with git's own sequential semantics (each ``-C``
    chdirs, relative paths compound), else ``None``. ``discovery_dir`` is
    that target when present, else the child's ``cwd`` (or the inherited
    working directory).
    """
    tokens = [os.fspath(arg) for arg in args]
    base = os.fspath(cwd) if cwd is not None else os.getcwd()
    directory = base
    explicit: str | None = None
    git_dir: str | None = None
    flags, _subcommand = _split_pre_subcommand_flags(tokens)
    for flag, value in flags:
        if flag == "-C":
            assert value is not None
            directory = (
                value if os.path.isabs(value) else os.path.join(directory, value)
            )
            explicit = directory
        elif flag == "--git-dir":
            assert value is not None
            git_dir = (
                value if os.path.isabs(value) else os.path.join(directory, value)
            )
    target = git_dir if git_dir is not None else explicit
    return target, (target if target is not None else base)


def _git_subcommand(args: Sequence[str | os.PathLike[str]]) -> str | None:
    """Return the git subcommand in ``args``, or ``None`` for flag-only argv.

    Flag-only invocations (``--version``, ``--exec-path``) and bare ``git``
    carry no subcommand and are read-only. Unknown subcommands are the
    caller's responsibility to spell; anything outside
    :data:`READ_ONLY_GIT_COMMANDS` is treated as mutating.
    """
    tokens = [os.fspath(arg) for arg in args]
    _flags, subcommand = _split_pre_subcommand_flags(tokens)
    return subcommand


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


def _layout_of(path: str) -> str:
    """Classify a candidate directory: ``normal``, ``bare``, ``gitfile``, or ``absent``.

    ``normal`` is a ``.git`` real directory, ``bare`` is a top-level git
    store (``HEAD``, ``objects``, ``refs``) with no ``.git`` entry at all,
    ``gitfile`` is any other ``.git`` entry (a ``gitdir:`` pointer file, a
    symlink to one, or anything non-directory), and ``absent`` is a fresh
    ``init``/``clone`` target or probe directory with no repository yet.
    """
    dot_git = os.path.join(path, ".git")
    try:
        if os.path.isdir(dot_git):
            return "normal"
        if os.path.lexists(dot_git):
            return "gitfile"
        bare = (
            os.path.isfile(os.path.join(path, "HEAD"))
            and os.path.isdir(os.path.join(path, "objects"))
            and os.path.isdir(os.path.join(path, "refs"))
        )
    except OSError:
        return "absent"
    return "bare" if bare else "absent"


def _expected_git_dir(fixture: str) -> str:
    """Return the git dir a fixture must discover.

    A ``.git`` directory expects itself; a bare layout (``HEAD``,
    ``objects``, ``refs`` at the top, no ``.git`` entry) expects the
    directory itself. A ``.git`` file is never followed -- a foreign
    ``gitdir:`` pointer is not proof of fixture ownership -- and a missing
    ``.git`` has no expectation: both refuse, since there is no owned git
    dir to compare discovery against.
    """
    dot_git = os.path.join(fixture, ".git")
    if os.path.isdir(dot_git):
        return dot_git
    if os.path.lexists(dot_git):
        raise AssertionError(
            "refusing to write git config outside the fixture repository: "
            f"{dot_git!r} is not a directory (a gitfile pointer is never "
            f"followed as proof of ownership; fixture {fixture!r})"
        )
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
    raise AssertionError(
        "refusing to write git config outside the fixture repository: "
        f"fixture {fixture!r} has no owned git dir to attest"
    )


def discover_git_dir(fixture: str | os.PathLike[str]) -> str:
    """Return the git directory git discovers for ``fixture``, as UTF-8 text.

    Runs under the same allowlist environment (and ceiling) the fixture's
    config-writing children get, so the guard observes exactly what they
    would. A nonzero rev-parse -- no repository, e.g. the ceiling blocked
    traversal into an enclosing checkout -- raises AssertionError: fail
    closed through the same channel as a mismatch.
    """
    env = build_fixture_git_env(fixture)
    proc = subprocess.run(
        ("git", "rev-parse", "--absolute-git-dir"),
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
    run in ``fixture``. The fixture owns exactly ``fixture/.git`` as a real
    directory (or the bare directory itself); a gitfile pointer, a missing
    ``.git``, garbage output, or any mismatch raises. Both sides are resolved
    and case-folded so macOS ``/tmp`` symlinks and Windows drive-letter case
    cannot cause a false refusal.
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

    The child environment is built from the allowlist only (see
    :func:`build_fixture_git_env`): ambient ``os.environ`` contributes just
    ``PATH``/tmp/Windows-baseline entries, ``env`` plus ``extra_env``
    contribute only explicit-allowlist keys, and the runner pins (private
    home, empty global config, empty template, ceiling, ``NOSYSTEM``) always
    win. The ceiling is pinned above the directory git discovers from -- the
    ``--git-dir``/``-C`` target when argv names one, else ``cwd``.

    Ownership: when the caller pins a target (``cwd`` or
    ``-C``/``--git-dir``) and it is repo-shaped, a normal repository is
    attested against its own ``.git`` directory and routed explicitly via
    prepended ``--git-dir``/``--work-tree`` (a bare repository via
    ``--git-dir`` alone); argv that already routes explicitly is attested,
    never rewritten. A gitfile checkout runs read-only subcommands
    unattested and refuses everything else. Anything else (a fresh
    ``init``/``clone`` target, a version probe, an explicit-path read) runs
    in the allowlist environment unattested, because there is no repository
    to attest yet. ``check``/``text``/``timeout``/``stdin``/
    ``capture_output`` behave exactly like :func:`subprocess.run`. ``argv``
    is a tuple, matching the convention of the fixture helpers this runner
    replaces: tests that observe the production subprocess seam assert tuple
    commands, and the runner must pass through those seams transparently.
    """
    str_args = [os.fspath(arg) for arg in args]
    explicit, discovery = _resolve_git_target(str_args, cwd)
    merged: dict[str, str] = {}
    if env is not None:
        merged.update(env)
    if extra_env:
        merged.update(extra_env)
    child_env = build_fixture_git_env(discovery, merged if merged else None)
    if explicit is not None:
        candidate: str | None = explicit
    elif cwd is not None:
        candidate = os.fspath(cwd)
    else:
        candidate = None
    routed = list(str_args)
    if candidate is not None and _is_repo_shape(candidate):
        layout = _layout_of(candidate)
        if layout == "normal":
            check_own_git_dir(candidate, discover_git_dir(candidate))
            if explicit is None:
                git_dir = os.path.abspath(os.path.join(candidate, ".git"))
                work_tree = os.path.abspath(candidate)
                routed = [
                    f"--git-dir={git_dir}",
                    f"--work-tree={work_tree}",
                    *routed,
                ]
        elif layout == "bare":
            check_own_git_dir(candidate, discover_git_dir(candidate))
            if explicit is None:
                routed = [f"--git-dir={os.path.abspath(candidate)}", *routed]
        elif layout == "gitfile":
            subcommand = _git_subcommand(str_args)
            if subcommand is not None and subcommand not in READ_ONLY_GIT_COMMANDS:
                raise AssertionError(
                    "refusing to run git mutating command outside an owned "
                    f"repository: subcommand {subcommand!r} against a gitfile "
                    f"checkout is never fixture-owned (target {candidate!r})"
                )
    argv = (os.fspath(executable), *routed)
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
