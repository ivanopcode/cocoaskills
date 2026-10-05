"""Project-aware command dispatcher (POSIX slice).

STDLIB ONLY. This module has two lives: it is imported by the test suite as
``csk._dispatch_runtime``, and its exact bytes are copied by
``csk.dispatch.ensure_dispatcher_runtime`` to
``<manager>/dispatch/dispatcher.py``. A generated ``/bin/sh`` wrapper at
``<manager>/dispatch/dispatcher`` execs the recorded absolute interpreter on
that copy with isolated-mode (``-I``), so resolve stays fast and works
without the manager importable; ``-I`` additionally makes it ignore caller
PYTHONPATH, user site and startup hooks.

Resolve, then exec from the shell: the ``/bin/sh`` shim runs this
dispatcher in RESOLVE mode only. Resolve prints the absolute target and
the project root on stdout (or fails with the taxonomy message and exit
code); the shim itself sets ``CSK_PROJECT_ROOT`` (or unsets it for
global) and runs ``exec "$target" "$@"``. Python never sits in the exec
path, so caller signal dispositions, the signal mask and the environment
reach the target untouched. No dispatcher-private environment variables
are set in the caller's environment. Targets and roots containing a
newline or carriage return are refused at publication (and at load), so
the two-line stdout protocol is unambiguous.

This module is the single implementation of three shared contracts; the
publisher (``csk.dispatch``) and this runtime both call them:

* checkout identity and root matching (``new_checkout_id``,
  ``resolve_checkout_id``, ``find_project``, ``classify_record``,
  plus the one shared path comparison ``components_equal`` /
  ``split_components`` and the volume oracles);
* the dispatch error taxonomy (``DispatchError.kind`` plus
  ``load_registry`` / ``validate_registry_data``);
* effective command composition (``compose_layers``).

Resolution (see ``.temp/orchestration/dispatch/design.md``, part A):

* physical ``getcwd()``; ``PWD``, ``CSK_PROJECT_ROOT``, ``.git``, ``.agents``
  and Skillfile content are never consulted;
* the deepest registered root wins by canonical-path component prefix,
  compared under that volume's actual rules (case sensitivity and
  NFC/NFD equivalence, both probed per volume, never assumed), then
  confirmed by the recorded inode; ``st_dev`` is never identity, so a
  bare remount changes nothing, and an inode match without a path
  match is never a match;
* a record whose path is gone or whose inode differs is stale: it is
  never selected, it never affects another scope's dispatch, and status
  reports it; a CWD provably beneath a stale record (same-path
  replacement) refuses with re-register guidance instead of silently
  substituting another scope, while a moved-away root's CWD simply no
  longer matches and falls through until re-registration;
* a record flagged ``needs_migration`` (a project installed before
  dispatch records existed) refuses with explicit reinstall guidance
  instead of silently serving the global fallback;
* the project skill set wins, then the global fallback; a registered project
  with nothing installed falls back to global while its root metadata stays;
* whole-skill replacement applies before owner uniqueness, and command
  names use the shims volume's case rules (recorded at publication);
* a known pin whose target is missing or corrupt refuses with reinstall
  guidance and never substitutes the global version;
* the recorded target must resolve inside the manager home and outside
  every registered checkout, so the dispatch path never executes a file
  from a checkout.

Registry reads never follow symlinks: the ``current`` pointer, every
``registry.json`` and their parent components are verified
non-symlinks (leaf files opened with ``O_NOFOLLOW``) before anything is
read, so a checkout alias can never supply authoritative bytes.

Per-call integrity bound: every call checks the cheap ``(st_ino, size,
mtime_ns)`` identity tuple recorded at publication, not the full content
hash. A same-user replacement that preserves that metadata is outside the
per-call guarantee; ``csk status --check`` and ``csk global status --check``
verify the full recorded sha256 instead. Device numbers are excluded
because they depend on mount order, not on the bytes. A same-user swap of
registry bytes between the symlink check and the read is outside the
per-call guarantee in the same way.

Case-sensitivity bound: the comparison rule comes from the CWD volume's
actual oracle (``pathconf`` ``_PC_CASE_SENSITIVE`` where available,
otherwise a create-probe in the manager's own dir when the CWD shares
the manager's volume). Volumes with no oracle match exactly: a
case-alias there falls through to the enclosing scope or global instead
of the pin. No wrong pin is ever served either way, because the inode
confirmation decides identity; the rule only affects alias friendliness.
Mixed-sensitivity nested mounts under one root use the CWD volume's
rule, with the same soundness argument.

Normalization bound: NFC/NFD spelling equivalence is probed per volume
with a transient create-probe, exactly like case sensitivity. APFS and
HFS+ equate the spellings; bytewise volumes keep them distinct. With
no oracle the comparison is exact and an NFC/NFD alias falls through,
with the same soundness argument as the case rule: the inode
confirmation decides identity either way.

Exec transparency bound: resolve runs as a child process and never
execs, so dispositions, mask and environment pass through the ``/bin/sh``
shim untouched. A ``/bin/sh`` launcher necessarily materializes
PWD/SHLVL when the caller lacks them (and normalizes a PWD that does not
name the cwd), so a non-sh target observes those startup values instead
of the absence; sh targets always observe identical values because their
own shell applies the same normalization, and caller-set correct values
pass through untouched to every target. ``_`` names the invocation path
and differs by construction.
"""

from __future__ import annotations

import json
import os
import re
import secrets
import stat
import sys
import unicodedata
from typing import Any


SCHEMA_VERSION = 1

EXIT_OK = 0
EXIT_ERROR = 1
EXIT_USAGE = 2
EXIT_UNAVAILABLE = 127

_COMMAND_KINDS = ("script", "build")

#: Printed as the resolve protocol's root line when no project selects:
#: roots are absolute paths, so a bare ``-`` is unambiguous.
GLOBAL_ROOT_SENTINEL = "-"


class DispatchError(Exception):
    """A dispatcher refusal with its process exit status and taxonomy kind."""

    def __init__(
        self, message: str, *, code: int = EXIT_ERROR, kind: str = "error"
    ) -> None:
        super().__init__(message)
        self.code = code
        self.kind = kind


class CompositionError(ValueError):
    """An effective-scope composition refusal with its taxonomy kind.

    Neutral error type: the publisher converts it to
    ``DispatchPublishError`` and the runtime to ``DispatchError``.
    """

    def __init__(self, message: str, *, kind: str) -> None:
        super().__init__(message)
        self.kind = kind


def manager_home_from_dispatcher(dispatcher_path: str) -> str:
    """Return the manager home owning one dispatcher path.

    ``dispatcher_path`` is the runtime copy (``.../dispatch/dispatcher.py``
    under the generated wrapper), the wrapper itself, or any of those
    reached through a symlink: two levels up is always the manager home.
    """
    here = os.path.realpath(os.path.abspath(dispatcher_path))
    return os.path.dirname(os.path.dirname(here))


def empty_registry() -> dict[str, Any]:
    """Return the registry meaning "no install has ever published"."""
    return {
        "schema_version": SCHEMA_VERSION,
        "projects": {},
        "global": {"skills": [], "commands": {}},
        "shim_case_insensitive": False,
    }


def has_publication_artifacts(manager_home: str) -> bool:
    """Return whether the manager holds any dispatch publication output.

    A generation directory or a shim proves an install published here, so
    a missing ``current`` pointer is corruption rather than a fresh
    manager. The dispatcher alone proves nothing: ``dispatch setup``
    installs it on a fresh manager that has never published a record.
    The generations directory being a symlink refuses fail-closed:
    listing through an alias could mistake a linked empty directory
    for a fresh manager and report corruption as absence. The
    ``shims`` entry is the atomic-switch symlink on current managers
    (a flat directory on pre-link ones): any existing entry proves a
    publication, except a legacy flat directory that is still empty,
    which ``dispatch setup`` used to create on fresh managers.
    """
    dispatch_dir = os.path.join(manager_home, "dispatch")
    for directory in (
        os.path.join(dispatch_dir, "generations"),
        os.path.join(dispatch_dir, "shim-generations"),
    ):
        _refuse_symlink_component(directory, manager_home=manager_home)
        try:
            with os.scandir(directory) as iterator:
                for _entry in iterator:
                    return True
        except (FileNotFoundError, NotADirectoryError):
            continue
        except OSError as exc:
            raise DispatchError(
                f"dispatch state for manager at {manager_home} cannot be "
                f"listed ({directory}: {exc}); refusing to guess whether "
                f"anything was published"
            ) from exc
    shims = os.path.join(manager_home, "shims")
    try:
        info = os.lstat(shims)
    except FileNotFoundError:
        return False
    except OSError as exc:
        raise DispatchError(
            f"dispatch state for manager at {manager_home} cannot be "
            f"listed ({shims}: {exc}); refusing to guess whether "
            f"anything was published"
        ) from exc
    if stat.S_ISLNK(info.st_mode):
        return True
    if not stat.S_ISDIR(info.st_mode):
        return True
    try:
        with os.scandir(shims) as iterator:
            for _entry in iterator:
                return True
    except OSError as exc:
        raise DispatchError(
            f"dispatch state for manager at {manager_home} cannot be "
            f"listed ({shims}: {exc}); refusing to guess whether "
            f"anything was published"
        ) from exc
    return False


def _refuse_symlink_component(path: str, *, manager_home: str) -> None:
    """Refuse when ``path`` itself is a symlink, before anything is read."""
    try:
        info = os.lstat(path)
    except FileNotFoundError:
        return
    except OSError as exc:
        raise DispatchError(
            f"dispatch registry for manager at {manager_home} cannot be "
            f"verified ({path}: {exc}); refusing to read through an "
            f"unverifiable path. This is an error, not 'outside any "
            f"project'",
            kind="registry_unreadable",
        ) from exc
    if stat.S_ISLNK(info.st_mode):
        raise DispatchError(
            f"dispatch registry for manager at {manager_home} refuses to "
            f"follow the symlink at {path}; a registry component must be "
            f"a manager-owned file or directory, never an alias (possibly "
            f"into a checkout). Reinstall the affected project ('csk "
            f"install') or the global set ('csk global install') to "
            f"republish it, or run 'csk dispatch recover' to restore the "
            f"last readable generation. This is an error, not 'outside "
            f"any project'",
            kind="registry_unreadable",
        )


def _read_no_follow(path: str, *, manager_home: str, what: str) -> bytes:
    """Read one registry file without following a final-component symlink.

    ``O_NOFOLLOW`` makes the leaf check atomic: a symlink races the
    ``lstat`` walk as ``ELOOP`` instead of checkout bytes.
    """
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    try:
        fd = os.open(path, flags)
    except FileNotFoundError:
        raise
    except OSError as exc:
        raise DispatchError(
            f"dispatch registry for manager at {manager_home} cannot be "
            f"read ({what} {path}: {exc}); reinstall the affected "
            f"project ('csk install') or the global set ('csk global "
            f"install') to republish it. This is an error, not 'outside "
            f"any project'",
            kind="registry_unreadable",
        ) from exc
    try:
        with os.fdopen(fd, "rb") as handle:
            return handle.read()
    except OSError as exc:
        raise DispatchError(
            f"dispatch registry for manager at {manager_home} cannot be "
            f"read ({what} {path}: {exc}); reinstall the affected "
            f"project ('csk install') or the global set ('csk global "
            f"install') to republish it. This is an error, not 'outside "
            f"any project'",
            kind="registry_unreadable",
        ) from exc


def load_registry(manager_home: str) -> dict[str, Any]:
    """Read and validate the manager's active dispatch registry.

    The taxonomy, each reported separately:

    * no ``current`` pointer and no publication artifacts: nothing was
      ever published, so the empty registry applies (every command is
      then "unavailable", exit 127);
    * a missing ``current`` after publication, an unreadable or
      undecodable pointer or registry (any ``OSError``,
      ``UnicodeDecodeError`` or JSON failure), a symlink anywhere along
      the registry path, or an unknown version: distinct guided errors
      (exit 1), never "outside any project".
    """
    dispatch_dir = os.path.join(manager_home, "dispatch")
    current_path = os.path.join(dispatch_dir, "current")
    _refuse_symlink_component(dispatch_dir, manager_home=manager_home)
    _refuse_symlink_component(current_path, manager_home=manager_home)
    try:
        raw_pointer = _read_no_follow(
            current_path, manager_home=manager_home, what="pointer"
        )
    except FileNotFoundError:
        if has_publication_artifacts(manager_home):
            raise DispatchError(
                f"dispatch pointer {current_path} for manager at "
                f"{manager_home} is missing though generations or shims "
                f"were published; reinstall the affected project "
                f"('csk install') or the global set ('csk global install') "
                f"to republish it, or run 'csk dispatch recover' to "
                f"restore the last readable generation. This is an error, "
                f"not 'outside any project'",
                kind="current_missing",
            )
        return empty_registry()
    try:
        generation_id = raw_pointer.decode("utf-8").strip()
    except UnicodeDecodeError as exc:
        raise DispatchError(
            f"dispatch pointer {current_path} for manager at {manager_home} "
            f"is not valid UTF-8 ({exc}); reinstall the affected project "
            f"('csk install') or the global set ('csk global install') to "
            f"republish it. This is an error, not 'outside any project'",
            kind="registry_unreadable",
        ) from exc
    if not _is_generation_id(generation_id):
        raise DispatchError(
            f"dispatch registry for manager at {manager_home} is corrupt "
            f"(the {current_path} pointer names no generation); reinstall "
            f"the affected project ('csk install') or the global set "
            f"('csk global install') to republish it. This is an error, "
            f"not 'outside any project'",
            kind="registry_corrupt",
        )
    generations_dir = os.path.join(dispatch_dir, "generations")
    generation_dir = os.path.join(generations_dir, generation_id)
    registry_path = os.path.join(generation_dir, "registry.json")
    _refuse_symlink_component(generations_dir, manager_home=manager_home)
    _refuse_symlink_component(generation_dir, manager_home=manager_home)
    _refuse_symlink_component(registry_path, manager_home=manager_home)
    try:
        raw = _read_no_follow(
            registry_path, manager_home=manager_home, what="registry"
        )
    except FileNotFoundError as exc:
        raise DispatchError(
            f"dispatch registry generation {generation_id} for manager at "
            f"{manager_home} cannot be read ({registry_path}: no such "
            f"generation); reinstall the affected project ('csk install') "
            f"or the global set ('csk global install') to republish it. "
            f"This is an error, not 'outside any project'",
            kind="registry_unreadable",
        ) from exc
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise DispatchError(
            f"dispatch registry generation {generation_id} for manager at "
            f"{manager_home} is not valid UTF-8 ({exc}); reinstall the "
            f"affected project ('csk install') or the global set "
            f"('csk global install') to republish it. This is an error, "
            f"not 'outside any project'",
            kind="registry_unreadable",
        ) from exc
    try:
        data = json.loads(text)
    except ValueError as exc:
        raise DispatchError(
            f"dispatch registry generation {generation_id} for manager at "
            f"{manager_home} is not valid JSON ({exc}); reinstall the "
            f"affected project ('csk install') or the global set "
            f"('csk global install') to republish it. This is an error, "
            f"not 'outside any project'",
            kind="registry_unreadable",
        ) from exc
    return validate_registry_data(data, generation_id=generation_id)


def validate_registry_data(data: Any, *, generation_id: str) -> dict[str, Any]:
    """Validate one parsed registry document and return its closed shape."""
    where = f"dispatch registry generation {generation_id}"
    if not isinstance(data, dict):
        raise DispatchError(
            f"{where} is corrupt (top-level shape); reinstall the affected "
            f"project ('csk install') or the global set ('csk global "
            f"install') to republish it",
            kind="registry_corrupt",
        )
    version = data.get("schema_version")
    if (
        version != SCHEMA_VERSION
        or isinstance(version, bool)
        or "schema_version" not in data
    ):
        raise DispatchError(
            f"{where} has schema_version {version!r}, expected "
            f"{SCHEMA_VERSION}; reinstall with a matching manager version",
            kind="unknown_version",
        )
    if set(data.keys()) != {
        "schema_version",
        "projects",
        "global",
        "shim_case_insensitive",
    }:
        raise DispatchError(
            f"{where} is corrupt (top-level shape); reinstall the affected "
            f"project ('csk install') or the global set ('csk global "
            f"install') to republish it",
            kind="registry_corrupt",
        )
    case_flag = data["shim_case_insensitive"]
    if type(case_flag) is not bool:
        raise DispatchError(
            f"{where} is corrupt ('shim_case_insensitive' is not a "
            f"boolean); reinstall to republish it",
            kind="registry_corrupt",
        )
    projects = data["projects"]
    if not isinstance(projects, dict):
        raise DispatchError(
            f"{where} is corrupt ('projects' is not an object)",
            kind="registry_corrupt",
        )
    validated_projects: dict[str, Any] = {}
    for checkout_id, entry in projects.items():
        validated = _validate_project_entry(entry, where=where)
        if checkout_id != validated["checkout_id"]:
            raise DispatchError(
                f"{where} is corrupt (project key {checkout_id!r} does not "
                f"match its checkout_id)",
                kind="registry_corrupt",
            )
        validated_projects[checkout_id] = validated
    global_entry = data["global"]
    if not isinstance(global_entry, dict) or set(global_entry.keys()) != {
        "skills",
        "commands",
    }:
        raise DispatchError(
            f"{where} is corrupt ('global' shape); reinstall the global "
            f"set ('csk global install') to republish it",
            kind="registry_corrupt",
        )
    return {
        "schema_version": SCHEMA_VERSION,
        "projects": validated_projects,
        "global": {
            "skills": _validate_skills(global_entry["skills"], where=where),
            "commands": _validate_commands(
                global_entry["commands"], where=f"{where} global"
            ),
        },
        "shim_case_insensitive": case_flag,
    }


def new_checkout_id() -> str:
    """Return a fresh full-strength checkout identity."""
    return secrets.token_hex(32)


def resolve_checkout_id(
    projects: dict[str, Any],
    *,
    canonical_root: str,
    root_ino: int,
    case_insensitive: bool,
    normalization_insensitive: bool,
) -> str:
    """Return the checkout id one registration must record.

    Re-registration replaces the record by checkout id: when the same
    directory is already registered under an equal spelling (equal
    under the one shared path comparison for the volume's actual
    rules, with the inode also matching), its id is reused so the
    registry never holds two records for one directory. Anything else
    mints a fresh id; a moved root's old record goes stale and is
    ignored for matching.
    """
    try:
        wanted = split_components(canonical_root)
    except DispatchError:
        return new_checkout_id()
    for checkout_id in sorted(projects):
        entry: dict[str, Any] = projects[checkout_id]
        if _recorded_ino(entry) != root_ino:
            continue
        try:
            recorded = split_components(entry["canonical_root"])
        except DispatchError:
            continue
        if components_equal(
            recorded,
            wanted,
            case_insensitive=case_insensitive,
            normalization_insensitive=normalization_insensitive,
        ):
            return checkout_id
    return new_checkout_id()


def classify_record(entry: dict[str, Any]) -> str:
    """Classify one project record against the live filesystem.

    Returns ``live`` (the recorded path still stats to the recorded
    inode), ``gone`` (the path no longer resolves), ``replaced`` (the
    path stats to a different inode), or ``unstatable`` (any other read
    failure, which fails closed). Device numbers are never compared: a
    bare remount keeps the record live.
    """
    recorded = _recorded_ino(entry)
    try:
        info = os.stat(entry["canonical_root"])
    except (FileNotFoundError, NotADirectoryError):
        return "gone"
    except OSError:
        return "unstatable"
    return "live" if info.st_ino == recorded else "replaced"


def stale_records(registry: dict[str, Any]) -> list[tuple[str, str]]:
    """Return ``(checkout_id, status)`` for every non-live record.

    Status and garbage-collection reporting consume this: stale records
    stay in the registry (their pins stay referenced) but are never
    selected for a scope and never fail unrelated dispatches.
    """
    stale: list[tuple[str, str]] = []
    for checkout_id in sorted(registry["projects"]):
        status = classify_record(registry["projects"][checkout_id])
        if status != "live":
            stale.append((checkout_id, status))
    return stale


def volume_case_insensitive(path: str, *, manager_home: str) -> bool:
    """Return whether ``path``'s volume folds filename case.

    The oracle chain, first answer wins: ``pathconf``
    ``_PC_CASE_SENSITIVE`` where the platform offers it (its actual
    per-volume answer); otherwise a transient create-probe in the
    manager's own home directory, but only when the path shares the
    manager's live volume (equal ``st_dev``), so the probe answers for
    the right volume. With no oracle the comparison is exact (see the
    case-sensitivity bound in the module docstring): a case-alias then
    falls through instead of matching, and no wrong pin is ever served
    because the inode confirmation decides identity either way.
    """
    case_sensitive_name = getattr(os, "pathconf_names", {}).get(
        "PC_CASE_SENSITIVE"
    )
    if case_sensitive_name is not None:
        try:
            value = os.pathconf(path, case_sensitive_name)
        except OSError:
            pass
        else:
            if value == 0:
                return True
            if value == 1:
                return False
    try:
        same_volume = os.stat(path).st_dev == os.stat(manager_home).st_dev
    except OSError:
        return False
    if not same_volume:
        return False
    probe_name = f".csk-CASE-{os.getpid()}-{secrets.token_hex(4)}"
    probe = os.path.join(manager_home, probe_name)
    try:
        with open(probe, "wb") as handle:
            handle.write(b"case")
    except OSError:
        return False
    try:
        return os.path.exists(os.path.join(manager_home, probe_name.casefold()))
    finally:
        try:
            os.unlink(probe)
        except OSError:
            pass


def volume_normalization_insensitive(path: str, *, manager_home: str) -> bool:
    """Return whether ``path``'s volume equates NFC/NFD spellings.

    APFS and HFS+ resolve either spelling to the one file; bytewise
    volumes (ext4, tmpfs and friends) keep them distinct. The oracle
    is a transient probe mirroring :func:`volume_case_insensitive`: an
    NFC-named file is created in the manager's own home directory,
    but only when the path shares the manager's live volume (equal
    ``st_dev``), so the probe answers for the right volume. With no
    oracle the comparison is exact (see the normalization bound in the
    module docstring): an NFC/NFD alias then falls through instead of
    matching, and no wrong pin is ever served because the inode
    confirmation decides identity either way.
    """
    try:
        same_volume = os.stat(path).st_dev == os.stat(manager_home).st_dev
    except OSError:
        return False
    if not same_volume:
        return False
    probe_name = (
        f".csk-NORM-{os.getpid()}-{secrets.token_hex(4)}-caf\u00e9"
    )
    probe = os.path.join(manager_home, probe_name)
    alias = os.path.join(
        manager_home, unicodedata.normalize("NFD", probe_name)
    )
    try:
        with open(probe, "wb") as handle:
            handle.write(b"norm")
    except OSError:
        return False
    try:
        try:
            return os.path.samefile(probe, alias)
        except OSError:
            return False
    finally:
        try:
            os.unlink(probe)
        except OSError:
            pass


def find_project(
    projects: dict[str, Any], cwd: str, *, manager_home: str
) -> dict[str, Any] | None:
    """Return the project record selecting ``cwd``, if any.

    The deepest registered root containing the physical CWD by path
    component decides the scope; components compare under the CWD
    volume's actual rules (case sensitivity and NFC/NFD equivalence,
    both probed per volume, never assumed), and the matched root is
    then confirmed by its recorded inode (``st_dev`` may differ after
    a remount). An inode match without a path match is never a match:
    there is no inode-only fallback. A deepest record that is stale
    (gone or replaced) refuses with re-register guidance instead of
    falling through to another scope, and a deepest record that cannot
    even be verified refuses without guessing; records outside the CWD
    path are never even stated, so one stale record can never fail an
    unrelated dispatch. A moved-away root simply no longer matches its
    old path and needs re-registration to serve its pin again.
    """
    if not projects:
        # No registered root can match; "outside every project" holds
        # without stating a CWD that may be unreadable.
        return None
    try:
        physical = os.path.realpath(cwd)
    except OSError as exc:
        raise DispatchError(
            f"cannot determine dispatch scope: {cwd} cannot be resolved "
            f"({exc}); refusing to guess project scope",
            kind="scope_unknown",
        ) from exc
    wanted = split_components(physical)
    case_insensitive = volume_case_insensitive(
        physical, manager_home=manager_home
    )
    normalization_insensitive = volume_normalization_insensitive(
        physical, manager_home=manager_home
    )
    best_depth = -1
    candidates: list[dict[str, Any]] = []
    for checkout_id in sorted(projects):
        entry: dict[str, Any] = projects[checkout_id]
        try:
            parts = split_components(entry["canonical_root"])
        except DispatchError:
            continue
        if len(parts) > len(wanted):
            continue
        if not components_equal(
            parts,
            wanted[: len(parts)],
            case_insensitive=case_insensitive,
            normalization_insensitive=normalization_insensitive,
        ):
            continue
        if len(parts) > best_depth:
            best_depth = len(parts)
            candidates = [entry]
        elif len(parts) == best_depth:
            candidates.append(entry)
    if not candidates:
        return None
    live = [
        entry
        for entry in candidates
        if classify_record(entry) == "live"
    ]
    if any(
        classify_record(entry) == "unstatable" for entry in candidates
    ):
        # An unverifiable same-depth record could be a contradictory
        # live registration; guessing would select a possibly wrong pin.
        raise DispatchError(
            f"registered project root {candidates[0]['canonical_root']} "
            f"cannot be verified; refusing to guess project scope. "
            f"Re-register the project with 'csk install' to repair dispatch",
            kind="scope_unknown",
        )
    if len(live) > 1:
        owners = sorted(entry["checkout_id"] for entry in live)
        raise DispatchError(
            f"dispatch registry has contradictory registrations for "
            f"one filesystem directory ({' and '.join(owners)}); "
            f"re-register the project with 'csk install' to repair it",
            kind="contradictory",
        )
    if len(live) == 1:
        return live[0]
    raise _stale_root_error(candidates[0])


def normalize_command_name(name: str, *, case_insensitive: bool) -> str:
    """Return the shims-directory comparison key for one command name."""
    return name.casefold() if case_insensitive else name


def compose_layers(
    *,
    project_skills: list[dict[str, str]],
    project_commands: dict[str, Any],
    global_commands: dict[str, Any],
    case_insensitive: bool,
    project_where: str | None = None,
) -> tuple[dict[str, tuple[dict[str, Any], bool]], dict[str, str]]:
    """Compose one effective command map from the project/global layers.

    Whole-skill replacement applies first (a project skill present at
    any pin suppresses the same skill's global exports entirely), then
    owner uniqueness over the survivors: one normalized name claimed by
    two spellings that differ only by case, or by two different skills,
    refuses instead of guessing. Returns ``(effective, suppressed)``
    where ``effective`` maps each name to ``(entry, from_project)`` and
    ``suppressed`` maps names dropped by replacement to their owner.
    """
    project_skill_names = {skill["name"] for skill in project_skills}
    surviving_global = {
        name: entry
        for name, entry in global_commands.items()
        if entry["owner"] not in project_skill_names
    }
    suppressed = {
        name: entry["owner"]
        for name, entry in global_commands.items()
        if entry["owner"] in project_skill_names
    }
    members: list[tuple[str, dict[str, Any], bool]] = [
        (name, entry, True) for name, entry in project_commands.items()
    ]
    members.extend(
        (name, entry, False) for name, entry in surviving_global.items()
    )
    groups: dict[str, list[tuple[str, dict[str, Any], bool]]] = {}
    for name, entry, from_project in members:
        key = normalize_command_name(name, case_insensitive=case_insensitive)
        groups.setdefault(key, []).append((name, entry, from_project))
    for key in sorted(groups):
        group = groups[key]
        spellings = sorted({name for name, _entry, _flag in group})
        if len(spellings) > 1:
            joined = " and ".join(repr(spelling) for spelling in spellings)
            raise CompositionError(
                f"dispatch commands {joined} differ only by case and "
                f"collide in the shims directory on this volume; give "
                f"the command a unique name",
                kind="case_collision",
            )
        owners = sorted({entry["owner"] for _n, entry, _f in group})
        if len(owners) > 1:
            name = spellings[0]
            if project_where is not None:
                raise CompositionError(
                    f"command {name!r} is exported by different skills in "
                    f"the {project_where} ({owners[0]!r}) and the global "
                    f"installation ({owners[1]!r}); refusing to guess. "
                    f"Give the command a unique name or install the same "
                    f"skill in both layers",
                    kind="owner_collision",
                )
            raise CompositionError(
                f"command {name!r} is exported by different skills "
                f"({owners[0]!r} and {owners[1]!r}); refusing to guess. "
                f"Give the command a unique name",
                kind="owner_collision",
            )
    effective: dict[str, tuple[dict[str, Any], bool]] = {}
    for key in sorted(groups):
        group = groups[key]
        name = group[0][0]
        project_entries = [entry for _n, entry, flag in group if flag]
        if project_entries:
            effective[name] = (project_entries[0], True)
        else:
            effective[name] = (group[0][1], False)
    return effective, suppressed


def select_command(
    registry: dict[str, Any], cwd: str, command: str, *, manager_home: str
) -> tuple[dict[str, Any], str | None, bool]:
    """Select one command's activation for ``cwd``.

    Returns ``(entry, project_root_or_None, from_project)``. A scope
    flagged ``needs_migration`` refuses with explicit reinstall
    guidance before any layer is consulted, so a project installed
    before dispatch records existed never silently serves the global
    fallback. Otherwise the project layer composes over the global
    layer through :func:`compose_layers`, so publication and dispatch
    share one replacement/owner/case decision. Raises
    :class:`DispatchError` with code 127 when the command is
    unavailable.
    """
    projects = registry["projects"]
    project = find_project(projects, cwd, manager_home=manager_home)
    project_root = project["canonical_root"] if project is not None else None
    if project is not None and project.get("needs_migration", False):
        raise DispatchError(
            f"project at {project_root} was installed before dispatch "
            f"records existed and has no dispatch record; run 'csk "
            f"install' in that project to publish its record. The "
            f"global fallback stays disabled for this project until it "
            f"migrates, so no other version is silently substituted",
            kind="needs_migration",
        )
    try:
        effective, suppressed = compose_layers(
            project_skills=project["skills"] if project is not None else [],
            project_commands=(
                project["commands"] if project is not None else {}
            ),
            global_commands=registry["global"]["commands"],
            case_insensitive=registry["shim_case_insensitive"],
            project_where=(
                f"project at {project_root}"
                if project_root is not None
                else None
            ),
        )
    except CompositionError as exc:
        raise DispatchError(
            f"{exc}, then reinstall the project ('csk install') or the "
            f"global set ('csk global install') to republish the record",
            kind=exc.kind,
        ) from exc
    if command in effective:
        selected, from_project = effective[command]
        return selected, project_root, from_project
    if command in suppressed:
        raise DispatchError(
            f"command {command!r} is unavailable in project at "
            f"{project_root}: the project replaces skill "
            f"{suppressed[command]!r} with a version that does not export "
            f"{command!r}, so the global {command!r} from the same skill "
            f"is suppressed (same-skill replacement replaces the whole "
            f"export set)",
            code=EXIT_UNAVAILABLE,
            kind="suppressed",
        )
    if project_root is not None:
        raise DispatchError(
            f"command {command!r} is unavailable: project at {project_root} "
            f"installs no {command!r} and the global installation exports "
            f"none either",
            code=EXIT_UNAVAILABLE,
            kind="unavailable",
        )
    raise DispatchError(
        f"command {command!r} is unavailable: the current directory is "
        f"outside every registered project and the global installation "
        f"exports no {command!r}",
        code=EXIT_UNAVAILABLE,
        kind="outside_roots",
    )


def require_usable_target(
    manager_home: str,
    target: str,
    *,
    command: str,
    scope: str,
    reinstall: str,
    expected_digest: dict[str, Any],
    projects: dict[str, Any],
) -> str:
    """Return ``target`` when it is the recorded manager-owned executable.

    Anything else refuses with reinstall guidance and never falls back:
    a missing or corrupt pin is an error, not a reason to substitute
    another layer's version. The target must resolve inside the manager
    home, outside every registered checkout, and still match the
    ``(st_ino, size, mtime_ns)`` identity tuple recorded at publication.
    """
    reason = _target_refusal_reason(
        manager_home, target, expected_digest, projects
    )
    if reason is not None:
        raise DispatchError(
            f"command {command!r} {scope} but its activation {target} is "
            f"{reason}; {reinstall}",
            kind="pin_broken",
        )
    return target


def scope_guidance(
    *,
    project_root: str | None,
    from_project: bool,
    owner: str,
) -> tuple[str, str]:
    """Return the (scope, reinstall) phrases for one selected command."""
    if from_project:
        return (
            f"is pinned by the project at {project_root} (skill {owner!r})",
            "reinstall the project ('csk install') to repair it; the "
            "global version is never substituted for a pinned command",
        )
    if project_root is not None:
        return (
            f"is pinned by the global installation (skill {owner!r}), "
            f"used from project at {project_root}",
            "run 'csk global install' to repair it",
        )
    return (
        f"is pinned by the global installation (skill {owner!r})",
        "run 'csk global install' to repair it",
    )


def resolve_command(
    manager_home: str, command: str, *, cwd: str | None = None
) -> tuple[str, str | None]:
    """Resolve one command to ``(target, project_root_or_None)``.

    This is the whole dispatcher: load the registry, select the scope
    and command, and validate the recorded target. It never execs; the
    calling shim execs the returned target itself, so caller signal and
    environment state pass through untouched.
    """
    registry = load_registry(manager_home)
    if cwd is None:
        try:
            cwd = os.getcwd()
        except OSError as exc:
            raise DispatchError(
                f"cannot determine the current directory ({exc}); refusing "
                f"to guess project scope",
                kind="scope_unknown",
            ) from exc
    entry, project_root, from_project = select_command(
        registry, cwd, command, manager_home=manager_home
    )
    scope, reinstall = scope_guidance(
        project_root=project_root,
        from_project=from_project,
        owner=entry["owner"],
    )
    target = require_usable_target(
        manager_home,
        entry["target"],
        command=command,
        scope=scope,
        reinstall=reinstall,
        expected_digest=entry["digest"],
        projects=registry["projects"],
    )
    return target, project_root


def main(argv: list[str] | None = None) -> int:
    """Resolve one command and print the shim exec protocol on stdout.

    Usage: ``dispatcher resolve <command>``. On success stdout carries
    exactly two lines, the absolute target and the project root (or
    ``-`` for global); the shim validates that shape before execing.
    Any refusal prints the taxonomy message on stderr and exits with
    its code, which the shim propagates unchanged.
    """
    args = list(sys.argv) if argv is None else list(argv)
    if len(args) != 3 or args[1] != "resolve" or not args[2]:
        sys.stderr.write("usage: dispatcher resolve <command>\n")
        return EXIT_USAGE
    command = args[2]
    try:
        manager_home = manager_home_from_dispatcher(args[0])
        target, project_root = resolve_command(manager_home, command)
        protocol_root = (
            project_root if project_root is not None else GLOBAL_ROOT_SENTINEL
        )
        for value in (target, protocol_root):
            if "\n" in value or "\r" in value:
                raise DispatchError(
                    f"dispatch record for command {command!r} contains a "
                    f"line break and cannot be resolved safely; reinstall "
                    f"the project ('csk install') or the global set ('csk "
                    f"global install') to republish it",
                    kind="registry_corrupt",
                )
        sys.stdout.write(f"{target}\n{protocol_root}\n")
    except DispatchError as exc:
        sys.stderr.write(f"error: {exc}\n")
        return exc.code
    return EXIT_OK


_GENERATION_ID_RE = re.compile(r"[A-Za-z0-9_-]{1,128}")
_IDENTIFIER_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}")
_HEX64_RE = re.compile(r"[0-9a-f]{64}")


def _is_generation_id(value: object) -> bool:
    # ASCII alphanumerics plus _-: exactly [A-Za-z0-9_-].
    return isinstance(value, str) and _GENERATION_ID_RE.fullmatch(value) is not None


def _is_identifier(value: object) -> bool:
    # First char ASCII alnum, rest ASCII alnum or ._-; the explicit
    # classes keep fullmatch equivalent to the per-character checks
    # (ASCII alphanumerics are exactly [A-Za-z0-9]).
    return isinstance(value, str) and _IDENTIFIER_RE.fullmatch(value) is not None


def _is_identity_component(value: object) -> bool:
    """Return whether ``value`` is a canonical uint64 decimal string.

    The registry's canonical JSON only carries safe-range integers, so
    filesystem identity components travel as decimal strings instead.
    """
    if not isinstance(value, str) or not value or len(value) > 20:
        return False
    # isascii+isdigit is exactly [0-9] (isdigit alone would admit
    # non-ASCII decimal digits, which the registry never carries).
    if not value.isascii() or not value.isdigit():
        return False
    if len(value) > 1 and value[0] == "0":
        return False
    return int(value) <= 18_446_744_073_709_551_615


def _validate_digest(digest: Any, *, where: str) -> dict[str, Any]:
    if (
        not isinstance(digest, dict)
        or set(digest.keys()) != {"sha256", "st_ino", "size", "mtime_ns"}
        or not isinstance(digest["sha256"], str)
        or _HEX64_RE.fullmatch(digest["sha256"]) is None
        or any(
            not _is_identity_component(digest[key])
            for key in ("st_ino", "size", "mtime_ns")
        )
    ):
        raise DispatchError(
            f"{where} is corrupt (activation digest shape); reinstall to "
            f"republish it",
            kind="registry_corrupt",
        )
    return {
        "sha256": digest["sha256"],
        "st_ino": digest["st_ino"],
        "size": digest["size"],
        "mtime_ns": digest["mtime_ns"],
    }


def _validate_root_identity(identity: Any, *, where: str) -> dict[str, str]:
    if (
        not isinstance(identity, dict)
        or set(identity.keys()) != {"st_ino"}
        or not _is_identity_component(identity["st_ino"])
    ):
        raise DispatchError(
            f"{where} is corrupt (project root_identity shape); "
            f"re-register the project with 'csk install' to repair it",
            kind="registry_corrupt",
        )
    return {"st_ino": identity["st_ino"]}


def _validate_project_entry(entry: Any, *, where: str) -> dict[str, Any]:
    if not isinstance(entry, dict) or set(entry.keys()) != {
        "checkout_id",
        "canonical_root",
        "project_alias",
        "checkout_alias",
        "root_identity",
        "needs_migration",
        "skills",
        "commands",
    }:
        raise DispatchError(
            f"{where} is corrupt (project entry shape)",
            kind="registry_corrupt",
        )
    checkout_id = entry["checkout_id"]
    if not isinstance(checkout_id, str) or (
        _HEX64_RE.fullmatch(checkout_id) is None
    ):
        raise DispatchError(
            f"{where} is corrupt (checkout_id is not a full-strength hex id)",
            kind="registry_corrupt",
        )
    root = entry["canonical_root"]
    if not isinstance(root, str) or not root.startswith("/") or "\x00" in root:
        raise DispatchError(
            f"{where} is corrupt (canonical_root is not absolute)",
            kind="registry_corrupt",
        )
    if "\n" in root or "\r" in root:
        raise DispatchError(
            f"{where} is corrupt (canonical_root contains a line break); "
            f"reinstall to republish it",
            kind="registry_corrupt",
        )
    alias = entry["project_alias"]
    if not isinstance(alias, str) or not alias:
        raise DispatchError(
            f"{where} is corrupt (project_alias is not a string)",
            kind="registry_corrupt",
        )
    checkout_alias = entry["checkout_alias"]
    if checkout_alias is not None and (
        not isinstance(checkout_alias, str) or not checkout_alias
    ):
        raise DispatchError(
            f"{where} is corrupt (checkout_alias is not a string)",
            kind="registry_corrupt",
        )
    if type(entry["needs_migration"]) is not bool:
        raise DispatchError(
            f"{where} is corrupt ('needs_migration' is not a boolean); "
            f"reinstall to republish it",
            kind="registry_corrupt",
        )
    return {
        "checkout_id": checkout_id,
        "canonical_root": root,
        "project_alias": alias,
        "checkout_alias": checkout_alias,
        "root_identity": _validate_root_identity(
            entry["root_identity"], where=f"{where} project at {root}"
        ),
        "needs_migration": entry["needs_migration"],
        "skills": _validate_skills(entry["skills"], where=where),
        "commands": _validate_commands(
            entry["commands"], where=f"{where} project at {root}"
        ),
    }


def _validate_skills(skills: Any, *, where: str) -> list[dict[str, str]]:
    if not isinstance(skills, list):
        raise DispatchError(
            f"{where} is corrupt ('skills' is not a list)",
            kind="registry_corrupt",
        )
    validated: list[dict[str, str]] = []
    for skill in skills:
        if (
            not isinstance(skill, dict)
            or set(skill.keys()) != {"name", "commit"}
            or not _is_identifier(skill["name"])
            or not isinstance(skill["commit"], str)
            or not skill["commit"]
        ):
            raise DispatchError(
                f"{where} is corrupt (skill pin shape); reinstall to republish it",
                kind="registry_corrupt",
            )
        validated.append({"name": skill["name"], "commit": skill["commit"]})
    return validated


def _validate_commands(
    commands: Any, *, where: str
) -> dict[str, dict[str, Any]]:
    if not isinstance(commands, dict):
        raise DispatchError(
            f"{where} is corrupt ('commands' is not an object)",
            kind="registry_corrupt",
        )
    validated: dict[str, dict[str, Any]] = {}
    for name, entry in commands.items():
        if not _is_identifier(name):
            raise DispatchError(
                f"{where} is corrupt (command name {name!r}); reinstall to "
                f"republish it",
                kind="registry_corrupt",
            )
        if (
            isinstance(entry, dict)
            and isinstance(entry.get("target"), str)
            and ("\n" in entry["target"] or "\r" in entry["target"])
        ):
            raise DispatchError(
                f"{where} is corrupt (command {name!r} target contains a "
                f"line break); reinstall to republish it",
                kind="registry_corrupt",
            )
        if (
            not isinstance(entry, dict)
            or set(entry.keys()) != {"owner", "target", "kind", "digest"}
            or not _is_identifier(entry["owner"])
            or not isinstance(entry["target"], str)
            or not entry["target"].startswith("/")
            or "\x00" in entry["target"]
            or entry["kind"] not in _COMMAND_KINDS
        ):
            raise DispatchError(
                f"{where} is corrupt (command {name!r} entry); reinstall to "
                f"republish it",
                kind="registry_corrupt",
            )
        validated[name] = {
            "owner": entry["owner"],
            "target": entry["target"],
            "kind": entry["kind"],
            "digest": _validate_digest(
                entry["digest"], where=f"{where} command {name!r}"
            ),
        }
    return validated


def split_components(path: str) -> list[str]:
    """Split one absolute path into normalized components."""
    if not path.startswith("/"):
        raise DispatchError(
            f"dispatch scope path is not absolute: {path}",
            kind="scope_unknown",
        )
    normalized = os.path.normpath(path)
    return [part for part in normalized.split("/") if part not in ("", ".")]


def components_equal(
    first: list[str],
    second: list[str],
    *,
    case_insensitive: bool,
    normalization_insensitive: bool,
) -> bool:
    """Return whether two component lists name one path on this volume.

    This is the one path comparison: selection, registration dedup,
    containment and the manager-home checks all route through it, so
    they cannot disagree about what "the same directory" means. Exact
    equality always counts; an NFC/NFD spelling difference counts only
    when the volume equates normalizations (APFS/HFS+ do; probed per
    volume, never assumed), and a case difference only when the
    volume's actual case oracle says so (never blanket casefold).
    """
    if len(first) != len(second):
        return False
    for left, right in zip(first, second):
        if left == right:
            continue
        if normalization_insensitive:
            left = unicodedata.normalize("NFC", left)
            right = unicodedata.normalize("NFC", right)
            if left == right:
                continue
        if not case_insensitive or left.casefold() != right.casefold():
            return False
    return True


def _recorded_ino(entry: dict[str, Any]) -> int:
    try:
        identity = entry["root_identity"]
        ino = identity["st_ino"]
    except (KeyError, TypeError) as exc:
        raise DispatchError(
            "dispatch registry is corrupt (project root_identity shape); "
            "re-register the project with 'csk install' to repair it",
            kind="registry_corrupt",
        ) from exc
    if not _is_identity_component(ino):
        raise DispatchError(
            "dispatch registry is corrupt (project root_identity shape); "
            "re-register the project with 'csk install' to repair it",
            kind="registry_corrupt",
        )
    return int(ino)


def _stale_root_error(entry: dict[str, Any]) -> DispatchError:
    root = entry["canonical_root"]
    try:
        info = os.stat(root)
    except (FileNotFoundError, NotADirectoryError):
        detail = (
            f"registered project root {root} no longer exists (it moved "
            f"or was deleted)"
        )
    except OSError as exc:
        detail = (
            f"registered project root {root} cannot be stated ({exc})"
        )
    else:
        if info.st_ino == _recorded_ino(entry):
            detail = (
                f"registered project root {root} no longer matches its "
                f"recorded path spelling"
            )
        else:
            detail = (
                f"registered project root {root} no longer matches its "
                f"recorded filesystem identity (it moved or was replaced)"
            )
    return DispatchError(
        f"{detail}. Re-register the project with 'csk install' to "
        f"repair dispatch",
        kind="stale_root",
    )


def _checkout_container(
    real: str, projects: dict[str, Any], *, manager_home: str
) -> str | None:
    """Return the registered checkout containing ``real``, if any.

    Containment is a component prefix under the target volume's actual
    rules through the one shared comparison, verified by inode at the
    matched level. A bare inode coincidence without path ancestry
    never counts: inodes recycle across volumes, so an inode-only
    clause would veto valid dispatches. A root moved after
    registration no longer contains by its old spelling (uniform with
    selection: moves need re-registration).
    """
    if not projects:
        return None
    try:
        parts = split_components(real)
    except DispatchError:
        return None
    case_insensitive = volume_case_insensitive(real, manager_home=manager_home)
    normalization_insensitive = volume_normalization_insensitive(
        real, manager_home=manager_home
    )
    ancestors: list[tuple[str, int]] = []
    node = os.path.normpath(real)
    while True:
        try:
            info = os.stat(node)
        except OSError:
            # The target itself was stated by the caller, so only a
            # concurrent change can land here; fail closed without
            # naming a checkout the walk could not prove.
            raise DispatchError(
                f"cannot verify {real} is outside the registered "
                f"checkouts; refusing to guess",
                kind="pin_broken",
            )
        ancestors.append((node, info.st_ino))
        parent = os.path.dirname(node)
        if parent == node:
            break
        node = parent
    for checkout_id in sorted(projects):
        entry: dict[str, Any] = projects[checkout_id]
        try:
            root_parts = split_components(entry["canonical_root"])
        except DispatchError:
            continue
        recorded = _recorded_ino(entry)
        if len(root_parts) <= len(parts) and components_equal(
            root_parts,
            parts[: len(root_parts)],
            case_insensitive=case_insensitive,
            normalization_insensitive=normalization_insensitive,
        ):
            if ancestors[len(parts) - len(root_parts)][1] == recorded:
                hit: str = entry["canonical_root"]
                return hit
    return None


def _target_refusal_reason(
    manager_home: str,
    target: str,
    expected_digest: dict[str, Any],
    projects: dict[str, Any],
) -> str | None:
    if "\x00" in target:
        return "not a valid path"
    home_real = os.path.realpath(manager_home)
    try:
        real = os.path.realpath(target)
    except OSError:
        return "missing or unreadable"
    try:
        home_parts = split_components(home_real)
        real_parts = split_components(real)
    except DispatchError:
        return "outside the manager home"
    if len(real_parts) <= len(home_parts):
        return "outside the manager home"
    if not components_equal(
        home_parts,
        real_parts[: len(home_parts)],
        case_insensitive=volume_case_insensitive(
            real, manager_home=manager_home
        ),
        normalization_insensitive=volume_normalization_insensitive(
            real, manager_home=manager_home
        ),
    ):
        return "outside the manager home"
    try:
        info = os.stat(real)
    except OSError:
        return "missing or unreadable"
    checkout_hit = _checkout_container(real, projects, manager_home=manager_home)
    if checkout_hit is not None:
        return f"inside the registered checkout at {checkout_hit}"
    parts: list[str] = []
    if isinstance(expected_digest, dict):
        for key in ("st_ino", "size", "mtime_ns"):
            component = expected_digest.get(key)
            parts.append(component if isinstance(component, str) else "")
    live = (
        str(info.st_ino),
        str(info.st_size),
        str(info.st_mtime_ns),
    )
    if tuple(parts) != live:
        return (
            "modified or replaced since publication (its recorded "
            "identity no longer matches)"
        )
    if not stat.S_ISREG(info.st_mode):
        return "not a regular file"
    if not os.access(real, os.X_OK):
        return "not executable"
    return None


if __name__ == "__main__":
    raise SystemExit(main())
