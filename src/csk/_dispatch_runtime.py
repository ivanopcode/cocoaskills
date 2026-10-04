"""Project-aware command dispatcher (POSIX slice).

STDLIB ONLY. This module has two lives: it is imported by the test suite as
``csk._dispatch_runtime``, and its exact bytes are copied by
``csk.dispatch.ensure_dispatcher_runtime`` to
``<manager>/dispatch/dispatcher.py``. A generated ``/bin/sh`` wrapper at
``<manager>/dispatch/dispatcher`` execs the recorded absolute interpreter on
that copy with isolated-mode (``-I``), so dispatch stays fast and works
without the manager importable; ``-I`` additionally makes it ignore caller
PYTHONPATH, user site and startup hooks.

This module is the single implementation of three shared contracts; the
publisher (``csk.dispatch``) and this runtime both call them:

* checkout identity and root matching (``new_checkout_id``,
  ``resolve_checkout_id``, ``find_project``, ``classify_record``);
* the dispatch error taxonomy (``DispatchError.kind`` plus
  ``load_registry`` / ``validate_registry_data``);
* effective command composition (``compose_layers``).

Resolution (see ``.temp/orchestration/dispatch/design.md``, part A):

* physical ``getcwd()``; ``PWD``, ``CSK_PROJECT_ROOT``, ``.git``, ``.agents``
  and Skillfile content are never consulted;
* the deepest registered root wins by path component, verified by
  filesystem identity (the ancestor inode plus a case-aware path match);
  ``st_dev`` is never identity, so a bare remount changes nothing;
* a record whose root no longer exists or no longer matches is ignored
  everywhere except directly beneath its recorded path, where it refuses
  with re-register guidance instead of silently substituting another
  scope; it never fails unrelated dispatches;
* the project skill set wins, then the global fallback; a registered project
  with nothing installed falls back to global while its root metadata stays;
* whole-skill replacement applies before owner uniqueness, and command
  names use the shims volume's case rules (recorded at publication);
* a known pin whose target is missing or corrupt refuses with reinstall
  guidance and never substitutes the global version;
* the recorded target must resolve inside the manager home and outside
  every registered checkout, so the dispatch path never executes a file
  from a checkout.

Per-call integrity bound: every call checks the cheap ``(st_ino, size,
mtime_ns)`` identity tuple recorded at publication, not the full content
hash. A same-user replacement that preserves that metadata is outside the
per-call guarantee; ``csk status --check`` and ``csk global status --check``
verify the full recorded sha256 instead. Device numbers are excluded
because they depend on mount order, not on the bytes.

Exec transparency bound: the dispatcher restores the signal dispositions
CPython changes at startup (SIGPIPE, SIGXFSZ) and the startup signal mask,
and removes exactly the environment variables the ``/bin/sh`` wrapper
recorded as absent in the caller. A caller that literally sets one of the
``_CSK_ENV_*`` sentinel variables loses it (the wrapper's snapshot wins);
direct invocation without the wrapper falls back to a documented heuristic
for LC_CTYPE only.

Shell-startup bound: a ``/bin/sh`` launcher necessarily materializes
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
import secrets
import signal
import stat
import sys
from typing import Any


SCHEMA_VERSION = 1

EXIT_OK = 0
EXIT_ERROR = 1
EXIT_USAGE = 2
EXIT_UNAVAILABLE = 127

_COMMAND_KINDS = ("script", "build")

#: Environment variables the interpreter startup may add, each paired with
#: the wrapper snapshot keys recording whether the caller had it and, when
#: set, its exact caller value.
_TRACKED_ENV_VARS: tuple[tuple[str, str, str], ...] = (
    ("LC_CTYPE", "_CSK_ENV_SET_LC_CTYPE", "_CSK_ENV_VAL_LC_CTYPE"),
    (
        "__CF_USER_TEXT_ENCODING",
        "_CSK_ENV_SET_CF",
        "_CSK_ENV_VAL_CF",
    ),
)


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


def _capture_startup_sigmask() -> frozenset[int] | None:
    """Snapshot the signal mask inherited from the caller, if possible."""
    mask_query = getattr(signal, "pthread_sigmask", None)
    block = getattr(signal, "SIG_BLOCK", None)
    if mask_query is None or block is None:
        return None
    try:
        return frozenset(mask_query(block, []))
    except (OSError, ValueError, RuntimeError):
        return None


_STARTUP_SIGMASK = _capture_startup_sigmask()


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
    """
    dispatch_dir = os.path.join(manager_home, "dispatch")
    for directory in (
        os.path.join(dispatch_dir, "generations"),
        os.path.join(manager_home, "shims"),
    ):
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
    return False


def load_registry(manager_home: str) -> dict[str, Any]:
    """Read and validate the manager's active dispatch registry.

    The taxonomy, each reported separately:

    * no ``current`` pointer and no publication artifacts: nothing was
      ever published, so the empty registry applies (every command is
      then "unavailable", exit 127);
    * a missing ``current`` after publication, an unreadable or
      undecodable pointer or registry (any ``OSError``,
      ``UnicodeDecodeError`` or JSON failure), or an unknown version:
      distinct guided errors (exit 1), never "outside any project".
    """
    dispatch_dir = os.path.join(manager_home, "dispatch")
    current_path = os.path.join(dispatch_dir, "current")
    try:
        with open(current_path, "rb") as handle:
            raw_pointer = handle.read()
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
    except OSError as exc:
        raise DispatchError(
            f"dispatch registry for manager at {manager_home} cannot be read "
            f"({current_path}: {exc}); reinstall the affected project "
            f"('csk install') or the global set ('csk global install') to "
            f"republish it. This is an error, not 'outside any project'",
            kind="registry_unreadable",
        ) from exc
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
    registry_path = os.path.join(
        dispatch_dir, "generations", generation_id, "registry.json"
    )
    try:
        with open(registry_path, "rb") as handle:
            raw = handle.read()
    except OSError as exc:
        raise DispatchError(
            f"dispatch registry generation {generation_id} for manager at "
            f"{manager_home} cannot be read ({registry_path}: {exc}); "
            f"reinstall the affected project ('csk install') or the global "
            f"set ('csk global install') to republish it. This is an error, "
            f"not 'outside any project'",
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
    projects: dict[str, Any], *, canonical_root: str, root_ino: int
) -> str:
    """Return the checkout id one registration must record.

    Re-registration replaces the record by checkout id: when the same
    directory is already registered under any spelling (a reinstall, or a
    case-alias ``project add``), its id is reused so the registry never
    holds two records for one directory. Anything else mints a fresh id;
    a moved root's old record goes stale and is ignored for matching.
    """
    for checkout_id in sorted(projects):
        entry: dict[str, Any] = projects[checkout_id]
        if _recorded_ino(entry) != root_ino:
            continue
        if _paths_equal(entry["canonical_root"], canonical_root):
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
    stay in the registry (their pins stay referenced) but never select
    a scope and never fail unrelated dispatches.
    """
    stale: list[tuple[str, str]] = []
    for checkout_id in sorted(registry["projects"]):
        status = classify_record(registry["projects"][checkout_id])
        if status != "live":
            stale.append((checkout_id, status))
    return stale


def find_project(
    projects: dict[str, Any], cwd: str
) -> dict[str, Any] | None:
    """Return the project record selecting ``cwd``, if any.

    The deepest registered root containing the physical CWD by path
    component decides the scope; it matches when the ancestor inode
    equals the record and the ancestor path is equal under case-aware
    comparison (safe without a volume probe because the inode must also
    match: two spellings with one inode are one directory on any
    volume). A deepest record that no longer matches refuses with
    re-register guidance instead of falling through to another scope,
    and a record whose inode still appears in the chain without any
    path match is a moved root and refuses the same way. Records
    outside the CWD chain are ignored entirely, so one stale record
    can never fail an unrelated dispatch.
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
    wanted = _split_components(physical)
    chain = _ancestor_chain(physical)
    best_depth = -1
    candidates: list[dict[str, Any]] = []
    for checkout_id in sorted(projects):
        entry: dict[str, Any] = projects[checkout_id]
        try:
            parts = _split_components(entry["canonical_root"])
        except DispatchError:
            continue
        if len(parts) > len(wanted):
            continue
        if not _components_equal(parts, wanted[: len(parts)]):
            continue
        if len(parts) > best_depth:
            best_depth = len(parts)
            candidates = [entry]
        elif len(parts) == best_depth:
            candidates.append(entry)
    if candidates:
        ancestor_ino = chain[len(wanted) - best_depth][1]
        live = [
            entry
            for entry in candidates
            if _recorded_ino(entry) == ancestor_ino
        ]
        if len(live) == 1:
            return live[0]
        if len(live) > 1:
            owners = sorted(entry["checkout_id"] for entry in live)
            raise DispatchError(
                f"dispatch registry has contradictory registrations for "
                f"one filesystem directory ({' and '.join(owners)}); "
                f"re-register the project with 'csk install' to repair it",
                kind="contradictory",
            )
        raise _stale_root_error(candidates[0])
    chain_inos = {ino for _path, ino in chain}
    for checkout_id in sorted(projects):
        entry = projects[checkout_id]
        if _recorded_ino(entry) not in chain_inos:
            continue
        # The same inode under a different spelling: an alias (bind
        # mount, an unresolved symlink) of a live root still selects it,
        # while a root whose recorded path is gone or changed moved away.
        try:
            info = os.stat(entry["canonical_root"])
        except (FileNotFoundError, NotADirectoryError):
            pass
        except OSError as exc:
            raise DispatchError(
                f"registered project root {entry['canonical_root']} "
                f"cannot be verified ({exc}); refusing to guess project "
                f"scope. Re-register the project with 'csk install' to "
                f"repair dispatch",
                kind="moved_root",
            ) from exc
        else:
            if info.st_ino == _recorded_ino(entry):
                return entry
        raise DispatchError(
            f"registered project root {entry['canonical_root']} moved "
            f"(its recorded path no longer resolves to the registered "
            f"directory). Re-register the project with 'csk install' to "
            f"repair dispatch",
            kind="moved_root",
        )
    return None


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
    registry: dict[str, Any], cwd: str, command: str
) -> tuple[dict[str, Any], str | None, bool]:
    """Select one command's activation for ``cwd``.

    Returns ``(entry, project_root_or_None, from_project)``. The project
    layer composes over the global layer through :func:`compose_layers`,
    so publication and dispatch share one replacement/owner/case
    decision. Raises :class:`DispatchError` with code 127 when the
    command is unavailable.
    """
    projects = registry["projects"]
    project = find_project(projects, cwd)
    project_root = project["canonical_root"] if project is not None else None
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


def restore_caller_env(env: dict[str, str]) -> None:
    """Remove interpreter-startup environment changes from ``env`` in place.

    The ``/bin/sh`` wrapper snapshots each tracked variable's caller
    presence and value in ``_CSK_ENV_*`` sentinels; those restore the
    exact caller state here and are always removed before exec. Without
    a sentinel (direct invocation, never through the wrapper), only an
    LC_CTYPE value the startup is known to introduce is removed.
    """
    for variable, set_key, val_key in _TRACKED_ENV_VARS:
        flag = env.pop(set_key, None)
        saved = env.pop(val_key, None)
        if flag == "1":
            env[variable] = saved if saved is not None else ""
        elif flag == "0":
            env.pop(variable, None)
        elif variable == "LC_CTYPE" and env.get(variable) == "C.UTF-8":
            lc_all = env.get("LC_ALL", "")
            lang = env.get("LANG", "")
            if not lc_all and lang in ("", "C", "POSIX"):
                env.pop(variable, None)


def restore_exec_signals() -> None:
    """Restore caller signal semantics before execing the target.

    CPython ignores SIGPIPE and SIGXFSZ at startup, and an ignored
    disposition survives exec; both return to SIG_DFL so pipelines and
    explicit kills behave exactly as for a directly called target. The
    signal mask captured at import (unchanged since process start) is
    restored as well. Any failure refuses instead of execing with
    unknown signal semantics.
    """
    for name in ("SIGPIPE", "SIGXFSZ"):
        signum = getattr(signal, name, None)
        if signum is None:
            continue
        try:
            signal.signal(signum, signal.SIG_DFL)
        except (OSError, ValueError, RuntimeError) as exc:
            raise DispatchError(
                f"cannot restore {name} to its default disposition "
                f"({exc}); refusing to exec with inherited signal state"
            ) from exc
    if _STARTUP_SIGMASK is not None:
        mask_set = getattr(signal, "pthread_sigmask", None)
        setmask = getattr(signal, "SIG_SETMASK", None)
        if mask_set is not None and setmask is not None:
            try:
                mask_set(setmask, _STARTUP_SIGMASK)
            except (OSError, ValueError, RuntimeError) as exc:
                raise DispatchError(
                    f"cannot restore the caller signal mask ({exc}); "
                    f"refusing to exec with inherited signal state"
                ) from exc


def main(argv: list[str] | None = None) -> int:
    """Dispatch one command: resolve it for the physical CWD and exec it."""
    args = list(sys.argv) if argv is None else list(argv)
    if len(args) < 2 or not args[1]:
        sys.stderr.write("usage: dispatcher <command> [args ...]\n")
        return EXIT_USAGE
    command = args[1]
    rest = args[2:]
    try:
        manager_home = manager_home_from_dispatcher(args[0])
        registry = load_registry(manager_home)
        try:
            cwd = os.getcwd()
        except OSError as exc:
            raise DispatchError(
                f"cannot determine the current directory ({exc}); refusing "
                f"to guess project scope",
                kind="scope_unknown",
            ) from exc
        entry, project_root, from_project = select_command(
            registry, cwd, command
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
        env = dict(os.environ)
        restore_caller_env(env)
        if project_root is not None:
            env["CSK_PROJECT_ROOT"] = project_root
        else:
            env.pop("CSK_PROJECT_ROOT", None)
        restore_exec_signals()
        try:
            os.execve(target, [target, *rest], env)
        except OSError as exc:
            raise DispatchError(
                f"cannot execute {target} for command {command!r}: {exc}",
                kind="pin_broken",
            ) from exc
    except DispatchError as exc:
        sys.stderr.write(f"error: {exc}\n")
        return exc.code
    return EXIT_ERROR


def _is_generation_id(value: object) -> bool:
    return (
        isinstance(value, str)
        and 1 <= len(value) <= 128
        and all(
            character.isascii() and (character.isalnum() or character in "_-")
            for character in value
        )
    )


def _is_identifier(value: object) -> bool:
    if not isinstance(value, str) or not value or len(value) > 128:
        return False
    first = value[0]
    if not (first.isascii() and (first.isalpha() or first.isdigit())):
        return False
    return all(
        character.isascii()
        and (character.isalnum() or character in "._-")
        for character in value
    )


def _is_identity_component(value: object) -> bool:
    """Return whether ``value`` is a canonical uint64 decimal string.

    The registry's canonical JSON only carries safe-range integers, so
    filesystem identity components travel as decimal strings instead.
    """
    if not isinstance(value, str) or not value or len(value) > 20:
        return False
    if value[0] not in "0123456789" or (value[0] == "0" and len(value) > 1):
        return False
    if any(character not in "0123456789" for character in value):
        return False
    return int(value) <= 18_446_744_073_709_551_615


def _validate_digest(digest: Any, *, where: str) -> dict[str, Any]:
    if (
        not isinstance(digest, dict)
        or set(digest.keys()) != {"sha256", "st_ino", "size", "mtime_ns"}
        or not isinstance(digest["sha256"], str)
        or len(digest["sha256"]) != 64
        or any(
            character not in "0123456789abcdef"
            for character in digest["sha256"]
        )
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
        "skills",
        "commands",
    }:
        raise DispatchError(
            f"{where} is corrupt (project entry shape)",
            kind="registry_corrupt",
        )
    checkout_id = entry["checkout_id"]
    if (
        not isinstance(checkout_id, str)
        or len(checkout_id) != 64
        or any(
            character not in "0123456789abcdef" for character in checkout_id
        )
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
    return {
        "checkout_id": checkout_id,
        "canonical_root": root,
        "project_alias": alias,
        "checkout_alias": checkout_alias,
        "root_identity": _validate_root_identity(
            entry["root_identity"], where=f"{where} project at {root}"
        ),
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


def _split_components(path: str) -> list[str]:
    if not path.startswith("/"):
        raise DispatchError(
            f"dispatch scope path is not absolute: {path}",
            kind="scope_unknown",
        )
    normalized = os.path.normpath(path)
    return [part for part in normalized.split("/") if part not in ("", ".")]


def _components_equal(first: list[str], second: list[str]) -> bool:
    """Return whether two component lists name one path on any volume.

    Exact equality always counts; case-insensitive equality only ever
    decides together with an inode match, so two spellings resolving to
    one inode are one directory on case-sensitive volumes too.
    """
    if len(first) != len(second):
        return False
    for left, right in zip(first, second):
        if left != right and left.casefold() != right.casefold():
            return False
    return True


def _paths_equal(first: str, second: str) -> bool:
    return first == second or first.casefold() == second.casefold()


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


def _ancestor_chain(cwd: str) -> list[tuple[str, int]]:
    """Return ``(path, st_ino)`` from ``cwd`` up to ``/`` (index 0 is cwd)."""
    chain: list[tuple[str, int]] = []
    node = os.path.normpath(cwd)
    while True:
        try:
            info = os.stat(node)
        except OSError as exc:
            raise DispatchError(
                f"cannot determine dispatch scope: {node} cannot be "
                f"stated ({exc}); refusing to guess project scope",
                kind="scope_unknown",
            ) from exc
        chain.append((node, info.st_ino))
        parent = os.path.dirname(node)
        if parent == node:
            return chain
        node = parent


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
    real: str, projects: dict[str, Any]
) -> str | None:
    """Return the registered checkout containing ``real``, if any.

    Containment is a case-aware component prefix verified by inode, plus
    the recorded identity anywhere in the target's ancestor chain (a
    renamed root still owns its bytes). Either way the bytes under it
    are checkout bytes.
    """
    if not projects:
        return None
    try:
        parts = _split_components(real)
    except DispatchError:
        return None
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
    by_ino = {ino for _path, ino in ancestors}
    for checkout_id in sorted(projects):
        entry: dict[str, Any] = projects[checkout_id]
        try:
            root_parts = _split_components(entry["canonical_root"])
        except DispatchError:
            continue
        recorded = _recorded_ino(entry)
        if len(root_parts) <= len(parts) and _components_equal(
            root_parts, parts[: len(root_parts)]
        ):
            if ancestors[len(parts) - len(root_parts)][1] == recorded:
                hit: str = entry["canonical_root"]
                return hit
        if recorded in by_ino:
            hit = entry["canonical_root"]
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
        home_parts = _split_components(home_real)
        real_parts = _split_components(real)
    except DispatchError:
        return "outside the manager home"
    if len(real_parts) <= len(home_parts):
        return "outside the manager home"
    for index, part in enumerate(home_parts):
        if real_parts[index] != part:
            return "outside the manager home"
    try:
        info = os.stat(real)
    except OSError:
        return "missing or unreadable"
    checkout_hit = _checkout_container(real, projects)
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
