"""Dispatch generation publication for project-aware bare commands.

``csk install`` and ``csk global install`` publish one dispatch generation per
successful install: project records hold the full-strength checkout identity,
the canonical root, the active skill pins, the normalized command owners and
the absolute activation targets, all derived from that install's publication,
never from Skillfile text or checkout files at call time. The POSIX slice
also publishes ``<manager>/shims/<cmd>`` launchers that exec the absolute
manager dispatcher; see :mod:`csk._dispatch_runtime` for the standalone
call-time half.

Per-call integrity bound: each activation target is recorded with its
sha256 plus the cheap ``(st_dev, st_ino, size, mtime_ns)`` identity
tuple. Dispatch checks the tuple on every call; a same-user
replacement that preserves that metadata is outside the per-call
guarantee and is caught by ``csk status --check`` /
``csk global status --check``, which verify the full sha256.
"""

from __future__ import annotations

import hashlib
import os
import stat
import sys
import tempfile
import uuid
from collections.abc import Callable
from pathlib import Path
from typing import Any

from . import _dispatch_runtime
from . import command_names, identifiers, protocol_json


DISPATCH_SCHEMA_VERSION = _dispatch_runtime.SCHEMA_VERSION

SETUP_MARKER_BEGIN = "# >>> csk dispatch >>>"
SETUP_MARKER_END = "# <<< csk dispatch <<<"

# Command record: (owner skill, absolute target, kind).
CommandRecord = tuple[str, str, str]
# Skill pin record: (skill name, commit).
SkillPin = tuple[str, str]
# Validated command: (owner skill, absolute target, kind, digest record).
ValidatedCommand = tuple[str, str, str, dict[str, Any]]


class DispatchPublishError(Exception):
    pass


def publish_supported() -> bool:
    """Return whether this platform publishes dispatch records.

    The dispatch slice is POSIX-only (Windows launchers are a separate
    task): the registry, dispatcher and shims assume POSIX paths, so
    installers skip publication on Windows instead of recording paths
    the dispatcher could never resolve.
    """
    return os.name != "nt"


def dispatch_dir(csk_home: Path) -> Path:
    return Path(csk_home) / "dispatch"


def generations_dir(csk_home: Path) -> Path:
    return dispatch_dir(csk_home) / "generations"


def current_path(csk_home: Path) -> Path:
    return dispatch_dir(csk_home) / "current"


def generation_registry_path(csk_home: Path, generation_id: str) -> Path:
    return generations_dir(csk_home) / generation_id / "registry.json"


def dispatcher_path(csk_home: Path) -> Path:
    return dispatch_dir(csk_home) / "dispatcher"


def shims_dir(csk_home: Path) -> Path:
    return Path(csk_home) / "shims"


def checkout_id_for_root(canonical_root: str) -> str:
    """Return the full-strength checkout identity for one canonical root."""
    return hashlib.sha256(canonical_root.encode("utf-8")).hexdigest()


def canonical_root_for_project(project_path: Path) -> str:
    """Return the physical canonical root for one configured project path.

    ``os.path.realpath`` (not ``Path.resolve``) keeps the resolution
    syscall shape identical across supported Pythons: the
    filesystem-boundary sweep pins the exact touch sequence, and
    ``Path.resolve`` internals differ between 3.12 and 3.14.
    """
    return os.path.realpath(os.fspath(project_path))


def read_registry(csk_home: Path) -> dict[str, Any]:
    """Read the active registry; empty when nothing was ever published."""
    try:
        return _dispatch_runtime.load_registry(str(csk_home))
    except _dispatch_runtime.DispatchError as exc:
        raise DispatchPublishError(str(exc)) from exc


def publish_project(
    csk_home: Path,
    *,
    canonical_root: str,
    project_alias: str,
    checkout_alias: str | None,
    skills: list[SkillPin],
    commands: dict[str, CommandRecord],
) -> tuple[str, list[str]]:
    """Publish one project's dispatch record; return (generation, warnings).

    The write is atomic: the new generation lands under a fresh id and the
    ``current`` pointer swaps to it. Identical content republishes nothing.
    A command the project and global layers export from different skills
    refuses instead of recording a scope the dispatcher would have to
    guess; the diagnostic names both skill identities.
    """
    home = Path(csk_home)
    registry, current_id, warnings = _registry_for_publish(home)
    _refuse_manager_inside_checkout(
        home, new_root=canonical_root, registry=registry
    )
    entry = _project_entry(
        home,
        canonical_root=canonical_root,
        project_alias=project_alias,
        checkout_alias=checkout_alias,
        skills=skills,
        commands=commands,
    )
    _refuse_cross_layer_collisions(
        entry["commands"],
        registry["global"]["commands"],
        project_where=f"project at {canonical_root}",
    )
    refresh_launchers(home, registry)
    registry["projects"][entry["checkout_id"]] = entry
    return _commit_if_changed(home, registry, current_id=current_id), warnings


def publish_empty_scope(
    csk_home: Path,
    *,
    canonical_root: str,
    project_alias: str,
    checkout_alias: str | None,
) -> tuple[str, list[str]]:
    """Publish an empty dispatch scope for one registered project.

    ``csk project add`` calls this so a registered but never-installed
    project forms a scope boundary: calls from inside it use the global
    fallback (or report unavailable), never an enclosing project's set.
    Re-adding an already published scope keeps its record untouched.
    """
    home = Path(csk_home)
    registry, current_id, warnings = _registry_for_publish(home)
    _refuse_manager_inside_checkout(
        home, new_root=canonical_root, registry=registry
    )
    entry = _project_entry(
        home,
        canonical_root=canonical_root,
        project_alias=project_alias,
        checkout_alias=checkout_alias,
        skills=[],
        commands={},
    )
    refresh_launchers(home, registry)
    if (
        entry["checkout_id"] in registry["projects"]
        and current_id is not None
    ):
        return current_id, warnings
    registry["projects"][entry["checkout_id"]] = entry
    return _commit_if_changed(home, registry, current_id=current_id), warnings


def publish_global(
    csk_home: Path,
    *,
    skills: list[SkillPin],
    commands: dict[str, CommandRecord],
    retained_skill_names: frozenset[str] = frozenset(),
) -> tuple[str, list[str]]:
    """Publish the global dispatch record; return (generation, warnings).

    Skills this run resolved replace their whole previous export set; skills
    outside a selective run keep their published records untouched. A
    command the global and project layers export from different skills
    refuses instead of recording a scope the dispatcher would have to
    guess; the diagnostic names both skill identities.
    """
    home = Path(csk_home)
    registry, current_id, warnings = _registry_for_publish(home)
    _refuse_manager_inside_checkout(home, new_root=None, registry=registry)
    validated_skills = _validated_pins(skills, where="global skills")
    validated_commands = _validated_commands(home, commands, where="global")
    current_global: dict[str, Any] = registry["global"]
    new_names = {name for name, _commit in validated_skills}
    # A skill this run resolved replaces its whole previous record; only
    # skills outside the run survive from the previous generation.
    keep_names = set(retained_skill_names) - new_names
    merged_skills = [
        pin for pin in current_global["skills"] if pin["name"] in keep_names
    ]
    merged_skills.extend(
        {"name": name, "commit": commit} for name, commit in validated_skills
    )
    merged_commands: dict[str, Any] = {
        name: entry
        for name, entry in current_global["commands"].items()
        if entry["owner"] in keep_names
    }
    for name, record in validated_commands.items():
        owner, target, kind, digest = record
        previous = merged_commands.get(name)
        if previous is not None and previous["owner"] != owner:
            raise DispatchPublishError(
                f"global dispatch command collision for {name!r}: exported "
                f"by {previous['owner']} (installed, outside this run) and "
                f"{owner}"
            )
        merged_commands[name] = {
            "owner": owner,
            "target": target,
            "kind": kind,
            "digest": digest,
        }
    for checkout_id in sorted(registry["projects"]):
        project_entry = registry["projects"][checkout_id]
        _refuse_cross_layer_collisions(
            project_entry["commands"],
            merged_commands,
            project_where=(
                f"project at {project_entry['canonical_root']}"
            ),
        )
    refresh_launchers(home, registry)
    registry["global"] = {"skills": merged_skills, "commands": merged_commands}
    return _commit_if_changed(home, registry, current_id=current_id), warnings


def refresh_launchers(csk_home: Path, registry: dict[str, Any]) -> None:
    """Publish the dispatcher plus one shim per exported name, idempotently.

    This is the recovery shape: staging creates every name the given
    registry exports, pruning removes names it no longer exports, so a
    previous interruption converges back to the registry's surface.
    """
    home = Path(csk_home)
    stage_launchers(home, registry)
    prune_launchers(home, registry)


def recover_launchers(csk_home: Path) -> dict[str, Any]:
    """Reconcile the launchers against the active generation; return it."""
    home = Path(csk_home)
    registry = read_registry(home)
    refresh_launchers(home, registry)
    return registry


def stage_launchers(home: Path, registry: dict[str, Any]) -> None:
    """Create the dispatcher plus every shim the registry exports.

    Additive only: names outside the registry are left for
    :func:`prune_launchers`, so staging before the ``current`` switch
    can never remove a name the still-active generation needs.
    """
    dispatcher = ensure_dispatcher(home)
    target_dir = shims_dir(home)
    target_dir.mkdir(parents=True, exist_ok=True)
    for name in sorted(_exported_names(registry)):
        _require_publishable_command_name(name)
        _write_dispatch_shim(target_dir, name, dispatcher)


def prune_launchers(home: Path, registry: dict[str, Any]) -> None:
    """Remove shims for names the registry no longer exports."""
    union = _exported_names(registry)
    target_dir = shims_dir(home)
    try:
        children = sorted(target_dir.iterdir())
    except FileNotFoundError:
        return
    for child in children:
        if child.name in union:
            continue
        try:
            info = child.lstat()
        except FileNotFoundError:
            continue
        if stat.S_ISDIR(info.st_mode) and not child.is_symlink():
            continue
        child.unlink()


def _exported_names(registry: dict[str, Any]) -> dict[str, None]:
    union: dict[str, None] = {}
    for entry in registry["projects"].values():
        for name in entry["commands"]:
            union.setdefault(name)
    for name in registry["global"]["commands"]:
        union.setdefault(name)
    return union


def ensure_dispatcher(csk_home: Path) -> Path:
    """Install the standalone dispatcher; fail closed on template drift."""
    target = dispatcher_path(csk_home)
    target.parent.mkdir(parents=True, exist_ok=True)
    runtime_source = (
        Path(__file__).with_name("_dispatch_runtime.py").read_bytes()
    )
    for marker in (b"from csk", b"import csk", b"from .", b"from csk."):
        if marker in runtime_source:
            raise DispatchPublishError(
                "dispatch runtime template imports the manager package; "
                "the standalone dispatcher must stay stdlib-only"
            )
    executable = sys.executable
    if not os.path.isabs(executable):
        executable = os.path.abspath(executable)
    for scalar in (" ", '"', "\r", "\n", "\x00"):
        if scalar in executable:
            raise DispatchPublishError(
                f"dispatch dispatcher cannot use interpreter path "
                f"{executable!r}: it must be a single absolute word"
            )
    # Isolated mode: the dispatcher ignores PYTHONPATH, user site and
    # startup hooks, so caller-controlled Python configuration can neither
    # shadow its stdlib imports nor slow its startup with site processing.
    payload = b"#!" + os.fsencode(executable) + b" -I\n" + runtime_source
    _write_file_if_different(target, payload, 0o755)
    return target


def sh_single_quote(text: str) -> str:
    """Quote one shell word as a single-quoted literal.

    POSIX ``sh``, ``bash`` and ``zsh`` treat every byte inside single
    quotes literally; an embedded quote becomes the ``'\\''`` sequence.
    The result is only ever spliced into an unquoted position, never
    inside double quotes, where single quotes would lose their meaning.
    """
    return "'" + text.replace("'", "'\\''") + "'"


def shim_line(dispatcher: Path | str, name: str) -> str:
    """Return the ``exec`` line for one dispatch shim."""
    return (
        f"exec {sh_single_quote(str(dispatcher))} "
        f"{sh_single_quote(name)} \"$@\""
    )


def setup_snippet(shims: Path, *, shell: str) -> str:
    """Return the idempotent PATH-append snippet for one shell."""
    if shell in {"bash", "zsh", "sh"}:
        quoted = sh_single_quote(str(shims))
        return (
            f"{SETUP_MARKER_BEGIN}\n"
            'case ":$PATH:" in\n'
            f"  *:{quoted}:*) ;;\n"
            f'  *) export PATH="$PATH":{quoted} ;;\n'
            "esac\n"
            f"{SETUP_MARKER_END}\n"
        )
    if shell == "powershell":
        quoted = str(shims).replace("'", "''")
        return (
            f"{SETUP_MARKER_BEGIN}\n"
            f"if (($env:PATH -split ';') -notcontains '{quoted}') "
            f"{{ $env:PATH = \"$env:PATH;{quoted}\" }}\n"
            f"{SETUP_MARKER_END}\n"
        )
    raise DispatchPublishError(
        f"unsupported shell for dispatch setup: {shell}"
    )


def default_rc_path(shell: str) -> Path | None:
    """Return the default rc file for ``setup --install``, if there is one."""
    home = Path.home()
    if shell == "bash":
        return home / ".bashrc"
    if shell == "zsh":
        return home / ".zshrc"
    if shell == "sh":
        return home / ".profile"
    return None


def install_setup_snippet(rc_path: Path, snippet: str) -> bool:
    """Append the setup snippet once; return whether the file changed."""
    if SETUP_MARKER_BEGIN not in snippet or SETUP_MARKER_END not in snippet:
        raise DispatchPublishError("dispatch setup snippet is missing markers")
    try:
        existing = rc_path.read_text(encoding="utf-8")
    except FileNotFoundError:
        existing = ""
    if SETUP_MARKER_BEGIN in existing:
        return False
    prefix = existing
    if prefix and not prefix.endswith("\n"):
        prefix += "\n"
    rc_path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary_name = tempfile.mkstemp(
        prefix=f".{rc_path.name}.", dir=rc_path.parent
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(prefix + snippet)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, rc_path)
    except Exception:
        temporary.unlink(missing_ok=True)
        raise
    return True


def _project_entry(
    home: Path,
    *,
    canonical_root: str,
    project_alias: str,
    checkout_alias: str | None,
    skills: list[SkillPin],
    commands: dict[str, CommandRecord],
) -> dict[str, Any]:
    if (
        not isinstance(canonical_root, str)
        or not os.path.isabs(canonical_root)
        or "\x00" in canonical_root
    ):
        raise DispatchPublishError(
            f"dispatch project root must be an absolute path: "
            f"{canonical_root!r}"
        )
    if not isinstance(project_alias, str) or not project_alias:
        raise DispatchPublishError("dispatch project alias must be non-empty")
    if checkout_alias is not None and (
        not isinstance(checkout_alias, str) or not checkout_alias
    ):
        raise DispatchPublishError("dispatch checkout alias must be a string")
    try:
        root_info = os.stat(canonical_root)
    except OSError as exc:
        raise DispatchPublishError(
            f"dispatch project root {canonical_root} cannot be stated "
            f"({exc}); re-register the project to repair dispatch"
        ) from exc
    return {
        "checkout_id": checkout_id_for_root(canonical_root),
        "canonical_root": canonical_root,
        "project_alias": project_alias,
        "checkout_alias": checkout_alias,
        "root_identity": {
            "st_dev": str(root_info.st_dev),
            "st_ino": str(root_info.st_ino),
        },
        "skills": [
            {"name": name, "commit": commit}
            for name, commit in _validated_pins(skills, where="project skills")
        ],
        "commands": {
            name: {
                "owner": owner,
                "target": target,
                "kind": kind,
                "digest": digest,
            }
            for name, (owner, target, kind, digest) in _validated_commands(
                home, commands, where="project"
            ).items()
        },
    }


def _validated_pins(
    skills: list[SkillPin], *, where: str
) -> list[SkillPin]:
    seen: dict[str, str] = {}
    for name, commit in skills:
        if not identifiers.is_valid_identifier(name):
            raise DispatchPublishError(
                f"dispatch {where} name {name!r} {identifiers.IDENTIFIER_RULE}"
            )
        if not isinstance(commit, str) or not commit:
            raise DispatchPublishError(
                f"dispatch {where} commit for {name!r} must be non-empty"
            )
        previous = seen.get(name)
        if previous is not None and previous != commit:
            raise DispatchPublishError(
                f"dispatch {where} pins {name!r} twice "
                f"({previous} and {commit})"
            )
        seen[name] = commit
    return [(name, seen[name]) for name in sorted(seen)]


def _validated_commands(
    home: Path, commands: dict[str, CommandRecord], *, where: str
) -> dict[str, ValidatedCommand]:
    validated: dict[str, ValidatedCommand] = {}
    for name in sorted(commands):
        owner, target, kind = commands[name]
        _require_publishable_command_name(name)
        if not identifiers.is_valid_identifier(owner):
            raise DispatchPublishError(
                f"dispatch {where} owner {owner!r} {identifiers.IDENTIFIER_RULE}"
            )
        if kind not in {"script", "build"}:
            raise DispatchPublishError(
                f"dispatch {where} command {name!r} has kind {kind!r}"
            )
        checked, digest = _validated_target(home, target, name=name)
        validated[name] = (owner, checked, kind, digest)
    return validated


def _validated_target(
    home: Path, target: str, *, name: str
) -> tuple[str, dict[str, Any]]:
    if not isinstance(target, str) or not os.path.isabs(target):
        raise DispatchPublishError(
            f"dispatch command {name!r} target must be absolute: {target!r}"
        )
    if "\x00" in target:
        raise DispatchPublishError(
            f"dispatch command {name!r} target is not a valid path"
        )
    home_real = home.resolve()
    try:
        target_real = Path(target).resolve()
    except OSError as exc:
        raise DispatchPublishError(
            f"dispatch command {name!r} target cannot be resolved: "
            f"{target} ({exc})"
        ) from exc
    if target_real == home_real or home_real not in target_real.parents:
        raise DispatchPublishError(
            f"dispatch command {name!r} target must live below the manager "
            f"home {home}: {target}"
        )
    try:
        info = target_real.stat()
    except OSError as exc:
        raise DispatchPublishError(
            f"dispatch command {name!r} target is missing: {target} ({exc})"
        ) from exc
    if not stat.S_ISREG(info.st_mode):
        raise DispatchPublishError(
            f"dispatch command {name!r} target is not a regular file: {target}"
        )
    try:
        content = target_real.read_bytes()
    except OSError as exc:
        raise DispatchPublishError(
            f"dispatch command {name!r} target cannot be read: "
            f"{target} ({exc})"
        ) from exc
    # Identity components travel as decimal strings: canonical JSON
    # only carries safe-range integers, and these exceed it.
    digest = {
        "sha256": hashlib.sha256(content).hexdigest(),
        "st_dev": str(info.st_dev),
        "st_ino": str(info.st_ino),
        "size": str(info.st_size),
        "mtime_ns": str(info.st_mtime_ns),
    }
    return target, digest


def _require_publishable_command_name(name: str) -> None:
    if not identifiers.is_valid_identifier(name):
        raise DispatchPublishError(
            f"dispatch command name {name!r} {identifiers.IDENTIFIER_RULE}"
        )
    try:
        command_names.require_unreserved_command_name(name)
    except command_names.CommandNameReservedError as exc:
        raise DispatchPublishError(str(exc)) from exc


def _refuse_cross_layer_collisions(
    project_commands: dict[str, Any],
    global_commands: dict[str, Any],
    *,
    project_where: str,
) -> None:
    """Refuse one effective scope claimed by two different skills."""
    for name in sorted(project_commands):
        owner = project_commands[name]["owner"]
        other = global_commands.get(name)
        if other is not None and other["owner"] != owner:
            raise DispatchPublishError(
                f"dispatch command {name!r} is exported by different "
                f"skills in the {project_where} ({owner}) and global "
                f"({other['owner']}) layers; refusing to guess. Give the "
                f"command a unique name or install the same skill in both "
                f"layers"
            )


def manager_home_inside_root(home: Path, root: Path) -> bool:
    """Return whether the manager home sits inside one checkout root.

    The comparison is by filesystem identity, not spelling: the home's
    ancestors are walked with ``stat`` against the root's identity, so
    symlinks and case aliases cannot hide the overlap. A root that
    cannot be stated contains nothing provable and reports False.
    """
    try:
        want_info = os.stat(root)
    except OSError:
        return False
    want = (want_info.st_dev, want_info.st_ino)
    node = os.path.realpath(os.fspath(home))
    while True:
        try:
            info = os.stat(node)
        except OSError:
            return True
        if (info.st_dev, info.st_ino) == want:
            return True
        parent = os.path.dirname(node)
        if parent == node:
            return False
        node = parent


def _refuse_manager_inside_checkout(
    home: Path,
    *,
    new_root: str | None,
    registry: dict[str, Any],
) -> None:
    """Refuse publication when the manager home sits in a checkout.

    Both the live spelling and the recorded identity of every
    registered root count, so a renamed or replaced root cannot smuggle
    the manager inside checkout bytes.
    """
    home_chain: set[tuple[int, int]] = set()
    node = os.path.realpath(os.fspath(home))
    while True:
        try:
            info = os.stat(node)
        except OSError as exc:
            raise DispatchPublishError(
                f"dispatch manager home {home} cannot be verified "
                f"outside the registered checkouts ({exc}); refusing to "
                f"publish"
            ) from exc
        home_chain.add((info.st_dev, info.st_ino))
        parent = os.path.dirname(node)
        if parent == node:
            break
        node = parent
    candidates: dict[tuple[int, int], str] = {}
    if new_root is not None:
        try:
            info = os.stat(new_root)
        except OSError as exc:
            raise DispatchPublishError(
                f"dispatch manager home {home} cannot be verified "
                f"outside the registered checkout at {new_root} ({exc}); "
                f"refusing to publish"
            ) from exc
        candidates[(info.st_dev, info.st_ino)] = new_root
    for checkout_id in sorted(registry["projects"]):
        entry = registry["projects"][checkout_id]
        root = entry["canonical_root"]
        try:
            info = os.stat(root)
        except OSError:
            pass
        else:
            candidates.setdefault((info.st_dev, info.st_ino), root)
        recorded = entry.get("root_identity", {})
        if (
            isinstance(recorded, dict)
            and isinstance(recorded.get("st_dev"), str)
            and isinstance(recorded.get("st_ino"), str)
            and recorded["st_dev"].isdigit()
            and recorded["st_ino"].isdigit()
        ):
            candidates.setdefault(
                (int(recorded["st_dev"]), int(recorded["st_ino"])), root
            )
    for identity in sorted(home_chain):
        hit = candidates.get(identity)
        if hit is not None:
            raise DispatchPublishError(
                f"dispatch manager home {home} is inside the registered "
                f"checkout at {hit}; refusing to publish. Use a manager "
                f"home outside every checkout"
            )


def referenced_skill_commits(csk_home: Path) -> set[tuple[str, str]]:
    """Return the (skill, commit) pins the active generation references.

    Install planning retains these runtime commits, so an interrupted
    publication leaves the still-active generation's bytes intact and
    the visible surface stays coherent. The post-install collector
    reclaims superseded commits once a new generation commits. An
    unreadable registry retains nothing: publication then falls back
    to the last readable generation and records the new state.
    """
    try:
        registry = read_registry(Path(csk_home))
    except DispatchPublishError:
        return set()
    references: set[tuple[str, str]] = set()
    for entry in registry["projects"].values():
        for pin in entry["skills"]:
            references.add((pin["name"], pin["commit"]))
    for pin in registry["global"]["skills"]:
        references.add((pin["name"], pin["commit"]))
    return references


def verify_project_digests(
    csk_home: Path, canonical_roots: list[str]
) -> list[str]:
    """Re-hash recorded project targets; return one problem per failure."""
    wanted = set(canonical_roots)

    def select(registry: dict[str, Any]) -> list[tuple[str, dict[str, Any]]]:
        scopes: list[tuple[str, dict[str, Any]]] = []
        for checkout_id in sorted(registry["projects"]):
            entry = registry["projects"][checkout_id]
            if entry["canonical_root"] in wanted:
                scopes.append(
                    (
                        f"in the project at {entry['canonical_root']}",
                        entry["commands"],
                    )
                )
        return scopes

    return _verify_digests(
        csk_home,
        select,
        remedy="reinstall the project ('csk install') to republish it",
    )


def verify_global_digests(csk_home: Path) -> list[str]:
    """Re-hash recorded global targets; return one problem per failure."""

    def select(
        registry: dict[str, Any],
    ) -> list[tuple[str, dict[str, Any]]]:
        return [
            (
                "in the global installation",
                registry["global"]["commands"],
            )
        ]

    return _verify_digests(
        csk_home,
        select,
        remedy="run 'csk global install' to republish it",
    )


def _verify_digests(
    csk_home: Path,
    select: Callable[
        [dict[str, Any]], list[tuple[str, dict[str, Any]]]
    ],
    *,
    remedy: str,
) -> list[str]:
    home = Path(csk_home)
    try:
        registry = read_registry(home)
    except DispatchPublishError as exc:
        if current_path(home).exists():
            return [f"dispatch activation records cannot be verified: {exc}"]
        return []
    problems: list[str] = []
    for scope_where, commands in select(registry):
        for name in sorted(commands):
            entry = commands[name]
            target = entry["target"]
            try:
                content = Path(target).read_bytes()
            except OSError as exc:
                problems.append(
                    f"dispatch command {name!r} (skill "
                    f"{entry['owner']}) {scope_where}: activation target "
                    f"{target} cannot be read ({exc}); {remedy}"
                )
                continue
            if hashlib.sha256(content).hexdigest() != entry["digest"]["sha256"]:
                problems.append(
                    f"dispatch command {name!r} (skill "
                    f"{entry['owner']}) {scope_where}: activation target "
                    f"{target} no longer matches its recorded sha256; "
                    f"{remedy}"
                )
    return problems


def _registry_for_publish(
    home: Path,
) -> tuple[dict[str, Any], str | None, list[str]]:
    """Return (registry, clean current id or None, warnings)."""
    pointer = current_path(home)
    try:
        current_id = pointer.read_text(encoding="utf-8").strip()
    except FileNotFoundError:
        return _dispatch_runtime.empty_registry(), None, []
    except OSError as exc:
        raise DispatchPublishError(
            f"dispatch generation pointer {pointer} cannot be read: {exc}"
        ) from exc
    if current_id:
        try:
            registry = _dispatch_runtime.load_registry(str(home))
        except _dispatch_runtime.DispatchError as exc:
            fallback, warning = _last_readable_generation(home, current_id, exc)
            return fallback, None, [warning]
        return registry, current_id, []
    fallback, warning = _last_readable_generation(home, current_id, None)
    return fallback, None, [warning]


def _last_readable_generation(
    home: Path,
    current_id: str,
    exc: BaseException | None,
) -> tuple[dict[str, Any], str]:
    candidates: list[tuple[float, str, Path]] = []
    generations = generations_dir(home)
    try:
        children = list(generations.iterdir())
    except FileNotFoundError:
        children = []
    for child in children:
        registry_path = child / "registry.json"
        try:
            info = registry_path.stat()
        except OSError:
            continue
        candidates.append((info.st_mtime_ns, child.name, registry_path))
    candidates.sort(reverse=True)
    for _, candidate_id, registry_path in candidates:
        try:
            data = protocol_json.loads(registry_path.read_bytes())
        except (OSError, protocol_json.ProtocolJSONError):
            continue
        try:
            registry = _dispatch_runtime.validate_registry_data(
                data, generation_id=candidate_id
            )
        except _dispatch_runtime.DispatchError:
            continue
        detail = f": {exc}" if exc is not None else ""
        return registry, (
            f"dispatch generation {current_id or '(empty)'} is unreadable"
            f"{detail}; continuing from the last readable generation "
            f"{candidate_id}. Reinstall affected projects to restore any "
            f"newer dispatch records"
        )
    detail = f": {exc}" if exc is not None else ""
    return _dispatch_runtime.empty_registry(), (
        f"dispatch generation {current_id or '(empty)'} is unreadable"
        f"{detail}; starting a fresh dispatch registry. Reinstall affected "
        f"projects to restore their dispatch records"
    )


def _commit_if_changed(
    home: Path, registry: dict[str, Any], *, current_id: str | None
) -> str:
    payload = protocol_json.canonical_bytes(registry) + b"\n"
    if current_id is not None:
        try:
            previous = generation_registry_path(home, current_id).read_bytes()
        except OSError:
            previous = b""
        if previous == payload:
            refresh_launchers(home, registry)
            return current_id
    generation_id = uuid.uuid4().hex
    _write_generation_file(home, generation_id, payload)
    # Launchers before the pointer switch: staging is additive, so an
    # interruption here leaves the old generation with its old surface
    # (plus inert extra shims the old dispatcher reports unavailable).
    # Pruning runs after the switch; an interruption there leaves the
    # new generation with stale shims the new dispatcher reports
    # unavailable. Either way the visible surface is one coherent
    # generation, and recovery converges the shims to it.
    stage_launchers(home, registry)
    _swap_current_pointer(home, generation_id)
    prune_launchers(home, registry)
    return generation_id


def _write_generation_file(
    home: Path, generation_id: str, payload: bytes
) -> Path:
    generation = generations_dir(home) / generation_id
    generation.mkdir(parents=True, exist_ok=True)
    registry_path = generation / "registry.json"
    fd, temporary_name = tempfile.mkstemp(
        prefix=".registry.json.", dir=generation
    )
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary_name, registry_path)
    except Exception:
        Path(temporary_name).unlink(missing_ok=True)
        raise
    return registry_path


def _swap_current_pointer(home: Path, generation_id: str) -> None:
    pointer = current_path(home)
    pointer.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary_name = tempfile.mkstemp(
        prefix=".current.", dir=pointer.parent
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(generation_id + "\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary_name, pointer)
    except Exception:
        Path(temporary_name).unlink(missing_ok=True)
        raise


def _write_dispatch_shim(target_dir: Path, name: str, dispatcher: Path) -> None:
    content = ("#!/bin/sh\n" + shim_line(dispatcher, name) + "\n").encode(
        "utf-8"
    )
    _write_file_if_different(target_dir / name, content, 0o755)


def _write_file_if_different(path: Path, content: bytes, mode: int) -> None:
    try:
        info = path.lstat()
    except FileNotFoundError:
        info = None
    if info is not None:
        if not stat.S_ISREG(info.st_mode):
            path.unlink()
        elif path.read_bytes() == content:
            if stat.S_IMODE(info.st_mode) != mode:
                os.chmod(path, mode)
            return
        else:
            path.unlink()
    fd, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temporary_name, mode)
        os.replace(temporary_name, path)
    except Exception:
        Path(temporary_name).unlink(missing_ok=True)
        raise
