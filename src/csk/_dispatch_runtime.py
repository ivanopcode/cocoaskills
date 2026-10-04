"""Project-aware command dispatcher (POSIX slice).

STDLIB ONLY. This module has two lives: it is imported by the test suite as
``csk._dispatch_runtime``, and its exact bytes are copied by
``csk.dispatch.ensure_dispatcher`` to ``<manager>/dispatch/dispatcher`` with an
absolute-python isolated-mode (``-I``) shebang prepended. The installed copy
runs standalone: it must never import :mod:`csk` or any third-party package,
so dispatch stays fast and works without the manager importable; ``-I``
additionally makes it ignore caller PYTHONPATH, user site and startup hooks.

Resolution (see ``.temp/orchestration/dispatch/design.md``, part A):

* physical ``getcwd()``; ``PWD``, ``CSK_PROJECT_ROOT``, ``.git``, ``.agents``
  and Skillfile content are never consulted;
* the deepest registered root wins by filesystem identity (``(st_dev,
  st_ino)`` of the CWD ancestor chain), never by string prefix; a root
  whose live identity no longer matches its record refuses with
  re-register guidance;
* the project skill set wins, then the global fallback; a registered project
  with nothing installed falls back to global while its root metadata stays;
* different skills exporting one effective command name refuse instead of
  picking a layer; the same skill identity across layers still replaces
  its whole export set;
* a known pin whose target is missing or corrupt refuses with reinstall
  guidance and never substitutes the global version;
* the recorded target must resolve inside the manager home and outside
  every registered checkout, so the dispatch path never executes a file
  from a checkout.

Per-call integrity bound: every call checks the cheap ``(st_dev, st_ino,
size, mtime_ns)`` identity tuple recorded at publication, not the full
content hash. A same-user replacement that preserves that metadata is
outside the per-call guarantee; ``csk status --check`` and ``csk global
status --check`` verify the full recorded sha256 instead.
"""

from __future__ import annotations

import json
import os
import stat
import sys
from typing import Any


SCHEMA_VERSION = 1

EXIT_OK = 0
EXIT_ERROR = 1
EXIT_USAGE = 2
EXIT_UNAVAILABLE = 127

_COMMAND_KINDS = ("script", "build")


class DispatchError(Exception):
    """A dispatcher refusal with its process exit status."""

    def __init__(self, message: str, *, code: int = EXIT_ERROR) -> None:
        super().__init__(message)
        self.code = code


def manager_home_from_dispatcher(dispatcher_path: str) -> str:
    """Return the manager home owning one dispatcher executable path."""
    here = os.path.realpath(os.path.abspath(dispatcher_path))
    return os.path.dirname(os.path.dirname(here))


def empty_registry() -> dict[str, Any]:
    """Return the registry meaning "no install has ever published"."""
    return {
        "schema_version": SCHEMA_VERSION,
        "projects": {},
        "global": {"skills": [], "commands": {}},
    }


def load_registry(manager_home: str) -> dict[str, Any]:
    """Read and validate the manager's active dispatch registry.

    A missing ``current`` pointer means nothing was ever published and yields
    the empty registry (every command is then "unavailable"). Any other read
    or validation failure raises :class:`DispatchError`: a failed read is an
    error, never "no project".
    """
    dispatch_dir = os.path.join(manager_home, "dispatch")
    current_path = os.path.join(dispatch_dir, "current")
    try:
        with open(current_path, encoding="utf-8") as handle:
            generation_id = handle.read().strip()
    except FileNotFoundError:
        return empty_registry()
    except OSError as exc:
        raise DispatchError(
            f"dispatch registry for manager at {manager_home} cannot be read "
            f"({current_path}: {exc}); reinstall the affected project "
            f"('csk install') or the global set ('csk global install') to "
            f"republish it. This is an error, not 'outside any project'"
        ) from exc
    if not _is_generation_id(generation_id):
        raise DispatchError(
            f"dispatch registry for manager at {manager_home} is corrupt "
            f"(the {current_path} pointer names no generation); reinstall "
            f"the affected project ('csk install') or the global set "
            f"('csk global install') to republish it. This is an error, "
            f"not 'outside any project'"
        )
    registry_path = os.path.join(
        dispatch_dir, "generations", generation_id, "registry.json"
    )
    try:
        with open(registry_path, encoding="utf-8") as handle:
            raw = handle.read()
    except OSError as exc:
        raise DispatchError(
            f"dispatch registry generation {generation_id} for manager at "
            f"{manager_home} cannot be read ({registry_path}: {exc}); "
            f"reinstall the affected project ('csk install') or the global "
            f"set ('csk global install') to republish it. This is an error, "
            f"not 'outside any project'"
        ) from exc
    try:
        data = json.loads(raw)
    except ValueError as exc:
        raise DispatchError(
            f"dispatch registry generation {generation_id} for manager at "
            f"{manager_home} is not valid JSON ({exc}); reinstall the "
            f"affected project ('csk install') or the global set "
            f"('csk global install') to republish it. This is an error, "
            f"not 'outside any project'"
        ) from exc
    return validate_registry_data(data, generation_id=generation_id)


def validate_registry_data(data: Any, *, generation_id: str) -> dict[str, Any]:
    """Validate one parsed registry document and return its closed shape."""
    where = f"dispatch registry generation {generation_id}"
    if not isinstance(data, dict) or set(data.keys()) != {
        "schema_version",
        "projects",
        "global",
    }:
        raise DispatchError(
            f"{where} is corrupt (top-level shape); reinstall the affected "
            f"project ('csk install') or the global set ('csk global "
            f"install') to republish it"
        )
    version = data["schema_version"]
    if version != SCHEMA_VERSION or isinstance(version, bool):
        raise DispatchError(
            f"{where} has schema_version {version!r}, expected "
            f"{SCHEMA_VERSION}; reinstall with a matching manager version"
        )
    projects = data["projects"]
    if not isinstance(projects, dict):
        raise DispatchError(f"{where} is corrupt ('projects' is not an object)")
    validated_projects: dict[str, Any] = {}
    for checkout_id, entry in projects.items():
        validated = _validate_project_entry(entry, where=where)
        if checkout_id != validated["checkout_id"]:
            raise DispatchError(
                f"{where} is corrupt (project key {checkout_id!r} does not "
                f"match its checkout_id)"
            )
        validated_projects[checkout_id] = validated
    global_entry = data["global"]
    if not isinstance(global_entry, dict) or set(global_entry.keys()) != {
        "skills",
        "commands",
    }:
        raise DispatchError(
            f"{where} is corrupt ('global' shape); reinstall the global "
            f"set ('csk global install') to republish it"
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
    }


def find_project(
    projects: dict[str, Any], cwd: str
) -> dict[str, Any] | None:
    """Return the deepest registered root containing ``cwd``, if any.

    Containment is by filesystem identity: the physical CWD ancestors are
    walked with ``stat`` and a registered root matches when its recorded
    ``(st_dev, st_ino)`` appears in that chain, so case aliases, symlinks
    and lookalike prefixes resolve to the true directory. Identical
    canonical roots, or one directory, under contradictory registrations
    refuse instead of guessing. A root whose live identity no longer
    matches its record (it moved or was replaced) refuses with
    re-register guidance instead of silently mismatching.
    """
    wanted = _split_components(cwd)
    if not projects:
        # No registered root can match; "outside every project" holds
        # without stating a CWD that may be unreadable.
        return None
    seen_roots: dict[str, str] = {}
    seen_identities: dict[tuple[int, int], str] = {}
    for checkout_id in sorted(projects):
        entry = projects[checkout_id]
        root = entry["canonical_root"]
        previous = seen_roots.get(root)
        if previous is not None and previous != checkout_id:
            raise DispatchError(
                f"dispatch registry has contradictory registrations for "
                f"root {root} ({previous} and {checkout_id}); re-register "
                f"the project with 'csk install' to repair it"
            )
        seen_roots[root] = checkout_id
        identity = _recorded_root_identity(entry)
        owner = seen_identities.get(identity)
        if owner is not None and owner != checkout_id:
            raise DispatchError(
                f"dispatch registry has contradictory registrations for "
                f"one filesystem directory ({owner} and {checkout_id}); "
                f"re-register the project with 'csk install' to repair it"
            )
        seen_identities[identity] = checkout_id
    spelled: list[tuple[int, dict[str, Any]]] = []
    for checkout_id in sorted(projects):
        entry = projects[checkout_id]
        depth = _ancestor_depth(entry["canonical_root"], wanted)
        if depth is not None:
            spelled.append((depth, entry))
    chain = _ancestor_identities(cwd)
    position: dict[tuple[int, int], int] = {}
    for depth, identity in enumerate(chain):
        position.setdefault(identity, depth)
    best: dict[str, Any] | None = None
    best_index: int | None = None
    for checkout_id in sorted(projects):
        entry = projects[checkout_id]
        identity = _recorded_root_identity(entry)
        index = position.get(identity)
        if index is not None and (
            best_index is None or index < best_index
        ):
            best = entry
            best_index = index
    verify: list[dict[str, Any]] = [entry for _, entry in spelled]
    if best is not None and all(entry is not best for _, entry in spelled):
        verify.append(best)
    for entry in verify:
        _require_live_root_identity(entry)
    return best


def select_command(
    registry: dict[str, Any], cwd: str, command: str
) -> tuple[dict[str, Any], str | None, bool]:
    """Select one command's activation for ``cwd``.

    Returns ``(entry, project_root_or_None, from_project)``. The project
    skill set wins, then the global fallback; a project skill present at
    any pin suppresses the same skill's global exports (same-skill
    replacement replaces the whole export set). A command exported by
    different skills in the project and global layers refuses as an
    inconsistent record instead of picking a layer. Raises
    :class:`DispatchError` with code 127 when the command is unavailable.
    """
    projects = registry["projects"]
    project = find_project(projects, cwd)
    project_root = project["canonical_root"] if project is not None else None
    project_commands: dict[str, Any] = project["commands"] if project else {}
    global_commands: dict[str, Any] = registry["global"]["commands"]
    if project is not None and command in project_commands:
        selected = project_commands[command]
        other = global_commands.get(command)
        if other is not None and other["owner"] != selected["owner"]:
            raise DispatchError(
                f"command {command!r} is exported by different skills in "
                f"the project at {project_root} ({selected['owner']!r}) "
                f"and the global installation ({other['owner']!r}); "
                f"refusing to guess. Give the command a unique name or "
                f"install the same skill in both layers, then reinstall "
                f"the project ('csk install') or the global set ('csk "
                f"global install') to republish the record"
            )
        return selected, project_root, True
    if command in global_commands:
        if project is not None:
            project_skills = {skill["name"] for skill in project["skills"]}
            owner = global_commands[command]["owner"]
            if owner in project_skills:
                raise DispatchError(
                    f"command {command!r} is unavailable in project at "
                    f"{project_root}: the project replaces skill {owner!r} "
                    f"with a version that does not export {command!r}, so "
                    f"the global {command!r} from the same skill is "
                    f"suppressed (same-skill replacement replaces the "
                    f"whole export set)",
                    code=EXIT_UNAVAILABLE,
                )
        return global_commands[command], project_root, False
    if project_root is not None:
        raise DispatchError(
            f"command {command!r} is unavailable: project at {project_root} "
            f"installs no {command!r} and the global installation exports "
            f"none either",
            code=EXIT_UNAVAILABLE,
        )
    raise DispatchError(
        f"command {command!r} is unavailable: the current directory is "
        f"outside every registered project and the global installation "
        f"exports no {command!r}",
        code=EXIT_UNAVAILABLE,
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
    ``(st_dev, st_ino, size, mtime_ns)`` identity tuple recorded at
    publication.
    """
    reason = _target_refusal_reason(
        manager_home, target, expected_digest, projects
    )
    if reason is not None:
        raise DispatchError(
            f"command {command!r} {scope} but its activation {target} is "
            f"{reason}; {reinstall}"
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
                f"to guess project scope"
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
        if project_root is not None:
            env["CSK_PROJECT_ROOT"] = project_root
        else:
            env.pop("CSK_PROJECT_ROOT", None)
        try:
            os.execve(target, [target, *rest], env)
        except OSError as exc:
            raise DispatchError(
                f"cannot execute {target} for command {command!r}: {exc}"
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
        or set(digest.keys())
        != {"sha256", "st_dev", "st_ino", "size", "mtime_ns"}
        or not isinstance(digest["sha256"], str)
        or len(digest["sha256"]) != 64
        or any(
            character not in "0123456789abcdef"
            for character in digest["sha256"]
        )
        or any(
            not _is_identity_component(digest[key])
            for key in ("st_dev", "st_ino", "size", "mtime_ns")
        )
    ):
        raise DispatchError(
            f"{where} is corrupt (activation digest shape); reinstall to "
            f"republish it"
        )
    return {
        "sha256": digest["sha256"],
        "st_dev": digest["st_dev"],
        "st_ino": digest["st_ino"],
        "size": digest["size"],
        "mtime_ns": digest["mtime_ns"],
    }


def _validate_root_identity(identity: Any, *, where: str) -> dict[str, str]:
    if (
        not isinstance(identity, dict)
        or set(identity.keys()) != {"st_dev", "st_ino"}
        or any(
            not _is_identity_component(identity[key])
            for key in ("st_dev", "st_ino")
        )
    ):
        raise DispatchError(
            f"{where} is corrupt (project root_identity shape); "
            f"re-register the project with 'csk install' to repair it"
        )
    return {"st_dev": identity["st_dev"], "st_ino": identity["st_ino"]}


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
        raise DispatchError(f"{where} is corrupt (project entry shape)")
    checkout_id = entry["checkout_id"]
    if (
        not isinstance(checkout_id, str)
        or len(checkout_id) != 64
        or any(
            character not in "0123456789abcdef" for character in checkout_id
        )
    ):
        raise DispatchError(
            f"{where} is corrupt (checkout_id is not a full-strength hex id)"
        )
    root = entry["canonical_root"]
    if not isinstance(root, str) or not root.startswith("/") or "\x00" in root:
        raise DispatchError(
            f"{where} is corrupt (canonical_root is not absolute)"
        )
    alias = entry["project_alias"]
    if not isinstance(alias, str) or not alias:
        raise DispatchError(
            f"{where} is corrupt (project_alias is not a string)"
        )
    checkout_alias = entry["checkout_alias"]
    if checkout_alias is not None and (
        not isinstance(checkout_alias, str) or not checkout_alias
    ):
        raise DispatchError(
            f"{where} is corrupt (checkout_alias is not a string)"
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
        raise DispatchError(f"{where} is corrupt ('skills' is not a list)")
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
                f"{where} is corrupt (skill pin shape); reinstall to republish it"
            )
        validated.append({"name": skill["name"], "commit": skill["commit"]})
    return validated


def _validate_commands(
    commands: Any, *, where: str
) -> dict[str, dict[str, Any]]:
    if not isinstance(commands, dict):
        raise DispatchError(f"{where} is corrupt ('commands' is not an object)")
    validated: dict[str, dict[str, Any]] = {}
    for name, entry in commands.items():
        if not _is_identifier(name):
            raise DispatchError(
                f"{where} is corrupt (command name {name!r}); reinstall to "
                f"republish it"
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
                f"republish it"
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
        raise DispatchError(f"dispatch scope path is not absolute: {path}")
    normalized = os.path.normpath(path)
    return [part for part in normalized.split("/") if part not in ("", ".")]


def _ancestor_depth(root: str, wanted: list[str]) -> int | None:
    """Return the component depth of ``root`` when it contains ``wanted``."""
    try:
        parts = _split_components(root)
    except DispatchError:
        return None
    if len(parts) > len(wanted):
        return None
    for index, part in enumerate(parts):
        # One component at a time: "/proj" must not match "/proj-evil".
        if wanted[index] != part:
            return None
    return len(parts)


def _recorded_root_identity(entry: dict[str, Any]) -> tuple[int, int]:
    try:
        identity = entry["root_identity"]
        dev = identity["st_dev"]
        ino = identity["st_ino"]
    except (KeyError, TypeError) as exc:
        raise DispatchError(
            "dispatch registry is corrupt (project root_identity shape); "
            "re-register the project with 'csk install' to repair it"
        ) from exc
    if not _is_identity_component(dev) or not _is_identity_component(ino):
        raise DispatchError(
            "dispatch registry is corrupt (project root_identity shape); "
            "re-register the project with 'csk install' to repair it"
        )
    return (int(dev), int(ino))


def _ancestor_identities(cwd: str) -> list[tuple[int, int]]:
    """Return the ``(st_dev, st_ino)`` chain from ``cwd`` up to ``/``."""
    chain: list[tuple[int, int]] = []
    node = os.path.normpath(cwd)
    while True:
        try:
            info = os.stat(node)
        except OSError as exc:
            raise DispatchError(
                f"cannot determine dispatch scope: {node} cannot be "
                f"stated ({exc}); refusing to guess project scope"
            ) from exc
        chain.append((info.st_dev, info.st_ino))
        parent = os.path.dirname(node)
        if parent == node:
            return chain
        node = parent


def _require_live_root_identity(entry: dict[str, Any]) -> None:
    root = entry["canonical_root"]
    recorded = _recorded_root_identity(entry)
    try:
        info = os.stat(root)
    except OSError as exc:
        raise DispatchError(
            f"registered project root {root} cannot be stated ({exc}); "
            f"it may have moved. Re-register the project with "
            f"'csk install' to repair dispatch"
        ) from exc
    if (info.st_dev, info.st_ino) != recorded:
        raise DispatchError(
            f"registered project root {root} no longer matches its "
            f"recorded filesystem identity (it moved or was replaced). "
            f"Re-register the project with 'csk install' to repair dispatch"
        )


def _checkout_identities(
    projects: dict[str, Any],
) -> dict[tuple[int, int], str]:
    """Return the filesystem identities owned by registered checkouts.

    Both the recorded identity and the live identity of the spelled
    path count: a renamed root keeps its recorded identity, while a
    replaced directory keeps only its spelling. Either way the bytes
    under it are checkout bytes.
    """
    identities: dict[tuple[int, int], str] = {}
    for checkout_id in sorted(projects):
        entry = projects[checkout_id]
        root = entry["canonical_root"]
        try:
            info = os.stat(root)
        except OSError:
            pass
        else:
            identities.setdefault((info.st_dev, info.st_ino), root)
        identities.setdefault(_recorded_root_identity(entry), root)
    return identities


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
        for key in ("st_dev", "st_ino", "size", "mtime_ns"):
            component = expected_digest.get(key)
            parts.append(component if isinstance(component, str) else "")
    live = (
        str(info.st_dev),
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


def _checkout_container(
    real: str, projects: dict[str, Any]
) -> str | None:
    """Return the registered checkout containing ``real``, if any."""
    identities = _checkout_identities(projects)
    if not identities:
        return None
    node = real
    while True:
        try:
            info = os.stat(node)
        except OSError:
            # The target itself was stated by the caller, so only a
            # concurrent change can land here; fail closed without
            # naming a checkout the walk could not prove.
            raise DispatchError(
                f"cannot verify {real} is outside the registered "
                f"checkouts; refusing to guess"
            )
        hit = identities.get((info.st_dev, info.st_ino))
        if hit is not None:
            return hit
        parent = os.path.dirname(node)
        if parent == node:
            return None
        node = parent


if __name__ == "__main__":
    raise SystemExit(main())
