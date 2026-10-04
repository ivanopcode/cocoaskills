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
* the deepest registered root wins by path component, never by string prefix;
* the project skill set wins, then the global fallback; a registered project
  with nothing installed falls back to global while its root metadata stays;
* a known pin whose target is missing or corrupt refuses with reinstall
  guidance and never substitutes the global version;
* the recorded target must resolve inside the manager home, so the dispatch
  path never executes a file from a checkout.
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

    Containment is by path component, never by string prefix. Identical
    canonical roots under contradictory registrations refuse instead of
    guessing.
    """
    try:
        wanted = _split_components(cwd)
    except DispatchError:
        raise
    best: dict[str, Any] | None = None
    best_depth = -1
    seen_roots: dict[str, str] = {}
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
        depth = _ancestor_depth(root, wanted)
        if depth is not None and depth > best_depth:
            best = entry
            best_depth = depth
    return best


def select_command(
    registry: dict[str, Any], cwd: str, command: str
) -> tuple[dict[str, str], str | None, bool]:
    """Select one command's activation for ``cwd``.

    Returns ``(entry, project_root_or_None, from_project)``. The project
    skill set wins, then the global fallback; a project skill present at
    any pin suppresses the same skill's global exports (same-skill
    replacement replaces the whole export set). Raises :class:`DispatchError`
    with code 127 when the command is unavailable.
    """
    projects = registry["projects"]
    project = find_project(projects, cwd)
    project_root = project["canonical_root"] if project is not None else None
    project_commands: dict[str, Any] = project["commands"] if project else {}
    if project is not None and command in project_commands:
        return project_commands[command], project_root, True
    global_commands: dict[str, Any] = registry["global"]["commands"]
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
) -> str:
    """Return ``target`` when it is a manager-owned executable file.

    Anything else refuses with reinstall guidance and never falls back:
    a missing or corrupt pin is an error, not a reason to substitute
    another layer's version.
    """
    reason = _target_refusal_reason(manager_home, target)
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


def _validate_project_entry(entry: Any, *, where: str) -> dict[str, Any]:
    if not isinstance(entry, dict) or set(entry.keys()) != {
        "checkout_id",
        "canonical_root",
        "project_alias",
        "checkout_alias",
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
) -> dict[str, dict[str, str]]:
    if not isinstance(commands, dict):
        raise DispatchError(f"{where} is corrupt ('commands' is not an object)")
    validated: dict[str, dict[str, str]] = {}
    for name, entry in commands.items():
        if not _is_identifier(name):
            raise DispatchError(
                f"{where} is corrupt (command name {name!r}); reinstall to "
                f"republish it"
            )
        if (
            not isinstance(entry, dict)
            or set(entry.keys()) != {"owner", "target", "kind"}
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


def _target_refusal_reason(manager_home: str, target: str) -> str | None:
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
    if not stat.S_ISREG(info.st_mode):
        return "not a regular file"
    if not os.access(real, os.X_OK):
        return "not executable"
    return None


if __name__ == "__main__":
    raise SystemExit(main())
