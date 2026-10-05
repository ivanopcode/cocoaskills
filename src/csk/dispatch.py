"""Dispatch generation publication for project-aware bare commands.

``csk install`` and ``csk global install`` publish one dispatch generation per
successful install: project records hold a stable checkout identity, the
canonical root, the active skill pins, the normalized command owners and
the absolute activation targets, all derived from that install's publication,
never from Skillfile text or checkout files at call time. The POSIX slice
also publishes ``<manager>/shims/<cmd>`` launchers: each shim runs the
absolute manager dispatcher in RESOLVE mode only, then execs the resolved
target itself, so caller signal and environment state pass through
untouched; see :mod:`csk._dispatch_runtime` for the standalone call-time
half.

Identity, the error taxonomy and command composition are implemented once
in :mod:`csk._dispatch_runtime` and shared: this publisher calls the same
``resolve_checkout_id`` / ``compose_layers`` the dispatcher runs, so
publication and dispatch cannot disagree. Publication additionally
validates the union of public shim names across all projects and global
under the shims volume's case rules: a case-only collision between any
two names refuses with both owners named.

Recovery and publication share one activation function
(:func:`_activate_generation`): stage launchers, switch ``current``,
reconcile. ``recover --reset`` publishes a fresh empty generation and
removes corrupt ones, so a following reinstall works.

Every dispatch file write goes through :func:`atomic_write_bytes` (temp
file in the same directory, fsync, rename); a live file is never unlinked
before its replacement is staged, so readers always see old-or-new bytes.

A configured project with install history but no dispatch record (it was
installed before dispatch records existed) gets a scope flagged
``needs_migration`` instead of silently serving the global fallback:
calls inside it refuse with explicit reinstall guidance, and reinstalling
publishes its record. Targets and roots containing a newline or carriage
return are refused at publication, so the resolve stdout protocol stays
unambiguous.

Per-call integrity bound: each activation target is recorded with its
sha256 plus the cheap ``(st_ino, size, mtime_ns)`` identity tuple.
Dispatch checks the tuple on every call; a same-user replacement that
preserves that metadata is outside the per-call guarantee and is caught
by ``csk status --check`` / ``csk global status --check``, which verify
the full sha256. Device numbers are not identity anywhere: they depend
on mount order, not on the bytes.
"""

from __future__ import annotations

import hashlib
import json
import os
import secrets
import shutil
import stat
import sys
import tempfile
import time
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

#: Activation steps, in commit order. Publication and explicit recovery
#: share the one :func:`_activate_generation` function, so interrupting
#: either at any of these leaves the same old-or-new surface.
ACTIVATE_STEPS: tuple[str, ...] = (
    "activate:stage_launchers",
    "activate:swap_current",
    "activate:reconcile",
)

#: Publication steps, in commit order. The state-machine test interrupts
#: the commit at every one of these: the visible surface must stay
#: old-or-new and dispatch must keep working throughout.
PUBLISH_STEPS: tuple[str, ...] = (
    "publish:write_generation",
    *ACTIVATE_STEPS,
)

#: Explicit-recovery steps: activating the recovered generation plus the
#: reset-only removal of corrupt generations. The recovery state machine
#: drives a real ``dispatch recover`` on a corrupt registry at every one.
RECOVER_EXPLICIT_STEPS: tuple[str, ...] = (
    *ACTIVATE_STEPS,
    "recover:remove_corrupt",
)

#: Setup-path recovery steps. ``recover_launchers`` (``dispatch setup``)
#: reconciles shims against the healthy active generation through these.
RECOVER_STEPS: tuple[str, ...] = (
    "recover:stage",
    "recover:prune",
)

#: Fault points inside :func:`atomic_write_bytes` itself: after the temp
#: file is written (before fsync), after fsync (before chmod/replace),
#: and immediately before the rename. Interrupting at any of these must
#: still leave old-or-new bytes and no temp file behind.
ATOMIC_STEPS: tuple[str, ...] = (
    "atomic:after_write",
    "atomic:after_fsync",
    "atomic:before_replace",
)

#: Test-only fault hook consulted by :func:`_maybe_fault`. Production
#: never sets it; the state-machine test raises through it.
_fault_hook: Callable[[str], None] | None = None


class DispatchPublishError(Exception):
    pass


def _maybe_fault(step: str) -> None:
    if _fault_hook is not None:
        _fault_hook(step)


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


def dispatcher_runtime_path(csk_home: Path) -> Path:
    return dispatch_dir(csk_home) / "dispatcher.py"


def shims_dir(csk_home: Path) -> Path:
    return Path(csk_home) / "shims"


def shim_generations_dir(csk_home: Path) -> Path:
    return dispatch_dir(csk_home) / "shim-generations"


def atomic_write_bytes(path: Path, content: bytes, *, mode: int) -> None:
    """Write one file atomically: temp file, fsync, rename.

    The live path is never unlinked before its replacement is staged:
    readers always see the old or the new bytes, never a missing file.
    A symlink at ``path`` is replaced itself (rename does not follow
    the final component); a real directory is refused rather than
    destroyed. A best-effort directory fsync follows the rename.
    """
    target = Path(path)
    try:
        info = target.lstat()
    except FileNotFoundError:
        info = None
    if info is not None and stat.S_ISDIR(info.st_mode):
        # lstat: a symlink to a directory reports SYMLINK, so only a
        # real directory lands here; replacing it would destroy a tree.
        raise DispatchPublishError(
            f"refusing to replace directory {target} with a file"
        )
    if (
        info is not None
        and stat.S_ISREG(info.st_mode)
        and not stat.S_ISLNK(info.st_mode)
        and target.read_bytes() == content
    ):
        if stat.S_IMODE(info.st_mode) != mode:
            os.chmod(target, mode)
        return
    target.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary_name = tempfile.mkstemp(
        prefix=f".{target.name}.", dir=target.parent
    )
    # The fault step carries the file name, so state machines can
    # interrupt a chosen write (``atomic:after_write:current``) while
    # earlier writes in the same commit complete normally.
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(content)
            handle.flush()
            _maybe_fault(f"atomic:after_write:{target.name}")
            os.fsync(handle.fileno())
            _maybe_fault(f"atomic:after_fsync:{target.name}")
        os.chmod(temporary_name, mode)
        _maybe_fault(f"atomic:before_replace:{target.name}")
        os.replace(temporary_name, target)
    except BaseException:
        Path(temporary_name).unlink(missing_ok=True)
        raise
    try:
        dir_fd = os.open(target.parent, os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(dir_fd)
    except OSError:
        pass
    finally:
        os.close(dir_fd)


def canonical_root_for_project(project_path: Path) -> str:
    """Return the physical canonical root for one configured project path.

    ``os.path.realpath`` (not ``Path.resolve``) keeps the resolution
    syscall shape identical across supported Pythons: the
    filesystem-boundary sweep pins the exact touch sequence, and
    ``Path.resolve`` internals differ between 3.12 and 3.14.
    """
    return os.path.realpath(os.fspath(project_path))


def _with_recover_guidance(exc: _dispatch_runtime.DispatchError) -> str:
    text = str(exc)
    if exc.kind in (
        "current_missing",
        "registry_unreadable",
        "registry_corrupt",
    ):
        text += (
            " Run 'csk dispatch recover' to restore the last readable "
            "generation explicitly."
        )
    return text


def read_registry(csk_home: Path) -> dict[str, Any]:
    """Read the active registry; empty when nothing was ever published."""
    try:
        return _dispatch_runtime.load_registry(str(csk_home))
    except _dispatch_runtime.DispatchError as exc:
        raise DispatchPublishError(_with_recover_guidance(exc)) from exc


def shims_case_insensitive(csk_home: Path) -> bool:
    """Probe whether the shims volume folds command-name case.

    A mixed-case probe file decides it: when the casefolded spelling
    resolves to the probe, ``Zed`` and ``zed`` would collide in this
    directory. The probe lands in the staged-generations directory
    (the same volume as the shim files, without traversing the live
    ``shims`` link) and is removed before returning; a failed probe
    refuses publication rather than guessing the volume's rules.
    """
    target_dir = shim_generations_dir(csk_home)
    target_dir.mkdir(parents=True, exist_ok=True)
    name = f".csk-CASE-{os.getpid()}-{secrets.token_hex(4)}"
    probe = target_dir / name
    try:
        probe.write_bytes(b"case")
    except OSError as exc:
        raise DispatchPublishError(
            f"dispatch shims directory {target_dir} cannot be probed for "
            f"case rules ({exc}); refusing to publish"
        ) from exc
    try:
        return (target_dir / name.casefold()).exists()
    finally:
        try:
            probe.unlink()
        except OSError:
            pass


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
    Composition runs through the shared ``compose_layers``: whole-skill
    replacement first, then owner uniqueness under the shims volume's
    case rules. A scope the dispatcher would have to guess refuses
    instead of recording it; the diagnostic names both skill identities.
    The union of public shim names across every project and global is
    validated under the same case rules: one name shared by several
    scopes is one shim resolved per directory, but two spellings
    differing only by case cannot share a file and refuse.
    """
    home = Path(csk_home)
    registry, current_id, warnings = _registry_for_publish(home)
    _refuse_manager_inside_checkout(
        home, new_root=canonical_root, registry=registry
    )
    case_insensitive = shims_case_insensitive(home)
    entry = _project_entry(
        home,
        canonical_root=canonical_root,
        project_alias=project_alias,
        checkout_alias=checkout_alias,
        skills=skills,
        commands=commands,
        existing_projects=registry["projects"],
    )
    try:
        _dispatch_runtime.compose_layers(
            project_skills=entry["skills"],
            project_commands=entry["commands"],
            global_commands=registry["global"]["commands"],
            case_insensitive=case_insensitive,
            project_where=f"project at {canonical_root}",
        )
    except _dispatch_runtime.CompositionError as exc:
        raise DispatchPublishError(str(exc)) from exc
    validate_public_shim_union(
        projects={**registry["projects"], entry["checkout_id"]: entry},
        global_commands=registry["global"]["commands"],
        case_insensitive=case_insensitive,
    )
    refresh_launchers(home, registry)
    registry["projects"][entry["checkout_id"]] = entry
    registry["shim_case_insensitive"] = case_insensitive
    return _commit_if_changed(home, registry, current_id=current_id), warnings


def publish_empty_scope(
    csk_home: Path,
    *,
    canonical_root: str,
    project_alias: str,
    checkout_alias: str | None,
    detect_legacy: bool = True,
) -> tuple[str, list[str]]:
    """Publish an empty dispatch scope for one registered project.

    Registration paths call this so a registered but never-installed
    project forms a scope boundary: calls from inside it use the global
    fallback (or report unavailable), never an enclosing project's set.
    Re-adding an already published scope keeps its record untouched,
    including the installed pins when the same directory is re-added
    under another spelling.

    When ``detect_legacy`` holds and the manager's consumer record shows
    this root was previously installed, the scope is flagged
    ``needs_migration`` instead: the project predates dispatch records,
    so calls inside it refuse with explicit reinstall guidance rather
    than silently serving the global fallback, and reinstalling
    publishes its record. An unreadable consumer record tombstones
    the same way: failed history is unknown, not proof the project was
    never installed. Callers whose remedy cannot run (an install
    skip, where reinstall would skip again) pass ``detect_legacy=False``
    for a plain boundary.
    """
    home = Path(csk_home)
    registry, current_id, warnings = _registry_for_publish(home)
    _refuse_manager_inside_checkout(
        home, new_root=canonical_root, registry=registry
    )
    consumer_roots, consumer_warnings, history_unknown = (
        _installed_consumer_roots(home)
    )
    warnings.extend(consumer_warnings)
    entry = _project_entry(
        home,
        canonical_root=canonical_root,
        project_alias=project_alias,
        checkout_alias=checkout_alias,
        skills=[],
        commands={},
        existing_projects=registry["projects"],
        needs_migration=detect_legacy
        and (history_unknown or canonical_root in consumer_roots),
    )
    refresh_launchers(home, registry)
    if (
        entry["checkout_id"] in registry["projects"]
        and current_id is not None
    ):
        return current_id, warnings
    registry["projects"][entry["checkout_id"]] = entry
    registry["shim_case_insensitive"] = shims_case_insensitive(home)
    return _commit_if_changed(home, registry, current_id=current_id), warnings


def _installed_consumer_roots(
    home: Path,
) -> tuple[set[str], list[str], bool]:
    """Return canonical consumer roots, read warnings, and unknown flag.

    The manager's ``consumers.json`` records every successfully
    installed checkout; a configured root listed there but holding no
    dispatch record was installed before dispatch records existed. A
    missing file means no install history (unknown is False, no
    warning). An unreadable or malformed file is unknown (unknown is
    True): the caller tombstones the scope so dispatch refuses with
    reinstall guidance instead of granting the absence fallback for a
    possibly-installed project. Only valid empty history means
    never-installed.
    """
    path = Path(home) / "consumers.json"
    try:
        raw = path.read_bytes()
    except FileNotFoundError:
        return set(), [], False
    except OSError as exc:
        return set(), [
            f"dispatch consumers record {path} cannot be read ({exc}); "
            f"install history is unknown, so the scope is flagged for "
            f"migration"
        ], True
    try:
        data = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        return set(), [
            f"dispatch consumers record {path} is not valid JSON; install "
            f"history is unknown, so the scope is flagged for migration"
        ], True
    items = data.get("consumers") if isinstance(data, dict) else None
    if not isinstance(items, list):
        return set(), [
            f"dispatch consumers record {path} is malformed; install "
            f"history is unknown, so the scope is flagged for migration"
        ], True
    roots: set[str] = set()
    for item in items:
        if not isinstance(item, str) or not item:
            continue
        try:
            roots.add(os.path.realpath(item))
        except OSError:
            continue
    return roots, [], False


def publish_legacy_scopes(
    csk_home: Path,
    configured: list[tuple[str, str, str | None]],
) -> list[str]:
    """Publish scopes for configured projects that hold no record.

    Each successful install runs this over the configured projects, so
    a project installed before dispatch records existed gains its
    migration scope on the next install of any kind instead of silently
    serving the global fallback. Roots that already hold any record
    (live or stale) are skipped: reinstalling is their repair path, and
    the scan never shadows installed pins. Warn-not-fail per project: a
    broken legacy root never vetoes the install that triggered the scan.
    Callers hold the manager lock.
    """
    home = Path(csk_home)
    try:
        registry = read_registry(home)
    except DispatchPublishError as exc:
        return [f"dispatch legacy scan skipped: {exc}"]
    warnings: list[str] = []
    recorded = {
        entry["canonical_root"] for entry in registry["projects"].values()
    }
    for raw_root, project_alias, checkout_alias in configured:
        try:
            canonical_root = canonical_root_for_project(Path(raw_root))
        except OSError as exc:
            warnings.append(
                f"dispatch scope for {raw_root} not published: {exc}"
            )
            continue
        if canonical_root in recorded:
            continue
        try:
            os.stat(canonical_root)
        except OSError:
            warnings.append(
                f"dispatch scope for {raw_root} not published: project "
                f"root {canonical_root} cannot be stated"
            )
            continue
        try:
            _generation, scope_warnings = publish_empty_scope(
                home,
                canonical_root=canonical_root,
                project_alias=project_alias,
                checkout_alias=checkout_alias,
            )
        except (DispatchPublishError, OSError) as exc:
            warnings.append(
                f"dispatch scope for {raw_root} not published: {exc}"
            )
            continue
        warnings.extend(scope_warnings)
        recorded.add(canonical_root)
    return warnings


def _union_members(
    projects: dict[str, Any], global_commands: dict[str, Any]
) -> list[tuple[str, str, str]]:
    """Return ``(name, owner, scope)`` for every validated public export.

    The one export union: live project records plus global. Stale
    records contribute nothing. Validation and staging share this
    list, so a dead record can neither veto an install nor overwrite a
    live shim file.
    """
    members: list[tuple[str, str, str]] = []
    for checkout_id in sorted(projects):
        entry = projects[checkout_id]
        if _dispatch_runtime.classify_record(entry) != "live":
            continue
        scope = f"project at {entry['canonical_root']}"
        for name in sorted(entry["commands"]):
            owner = entry["commands"][name]["owner"]
            members.append((name, owner, scope))
    for name in sorted(global_commands):
        owner = global_commands[name]["owner"]
        members.append((name, owner, "the global installation"))
    return members


def validate_public_shim_union(
    *,
    projects: dict[str, Any],
    global_commands: dict[str, Any],
    case_insensitive: bool,
) -> None:
    """Refuse case-only shim collisions across every scope.

    One spelling shared by several projects (or global) is one shim
    file resolved per directory, so it publishes. Two spellings that
    differ only by case cannot share a file on a case-insensitive shims
    volume: publication refuses, naming both owners and both scopes.
    Stale records are excluded from the validated union (and, through
    the same list, from staging): a dead record never vetoes an
    install and never overwrites a live shim.
    """
    members = _union_members(projects, global_commands)
    groups: dict[str, list[tuple[str, str, str]]] = {}
    for name, owner, scope in members:
        key = _dispatch_runtime.normalize_command_name(
            name, case_insensitive=case_insensitive
        )
        groups.setdefault(key, []).append((name, owner, scope))
    for key in sorted(groups):
        group = groups[key]
        spellings = sorted({name for name, _owner, _scope in group})
        if len(spellings) < 2:
            continue
        first = next(
            (name, owner, scope)
            for name, owner, scope in sorted(group)
            if name == spellings[0]
        )
        second = next(
            (name, owner, scope)
            for name, owner, scope in sorted(group)
            if name == spellings[1]
        )
        raise DispatchPublishError(
            f"dispatch commands {first[0]!r} (skill {first[1]!r} in "
            f"{first[2]}) and {second[0]!r} (skill {second[1]!r} in "
            f"{second[2]}) differ only by case and collide in the shims "
            f"directory on this volume; give the command a unique name"
        )


def publish_global(
    csk_home: Path,
    *,
    skills: list[SkillPin],
    commands: dict[str, CommandRecord],
    retained_skill_names: frozenset[str] = frozenset(),
) -> tuple[str, list[str]]:
    """Publish the global dispatch record; return (generation, warnings).

    Skills this run resolved replace their whole previous export set; skills
    outside a selective run keep their published records untouched. Every
    live project record composes against the merged global layer through
    the shared ``compose_layers``; records whose roots no longer match
    are skipped with a warning (the runtime re-enforces composition for
    the one scope it actually dispatches, so skipping cannot admit a
    collision at call time).
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
    case_insensitive = shims_case_insensitive(home)
    for checkout_id in sorted(registry["projects"]):
        project_entry = registry["projects"][checkout_id]
        status = _dispatch_runtime.classify_record(project_entry)
        if status != "live":
            warnings.append(
                f"dispatch record for project at "
                f"{project_entry['canonical_root']} ignored (root "
                f"{status}); reinstall the project ('csk install') if it "
                f"still exists"
            )
            continue
        try:
            _dispatch_runtime.compose_layers(
                project_skills=project_entry["skills"],
                project_commands=project_entry["commands"],
                global_commands=merged_commands,
                case_insensitive=case_insensitive,
                project_where=(
                    f"project at {project_entry['canonical_root']}"
                ),
            )
        except _dispatch_runtime.CompositionError as exc:
            raise DispatchPublishError(str(exc)) from exc
    validate_public_shim_union(
        projects=registry["projects"],
        global_commands=merged_commands,
        case_insensitive=case_insensitive,
    )
    refresh_launchers(home, registry)
    registry["global"] = {"skills": merged_skills, "commands": merged_commands}
    registry["shim_case_insensitive"] = case_insensitive
    return _commit_if_changed(home, registry, current_id=current_id), warnings


def _surface_generation_id(home: Path) -> str | None:
    """Return the generation id the ``current`` pointer names, if any.

    Convergence helpers resolve the active generation through this.
    They run after the registry already validated (or just switched
    to a fresh id), so the pointer holds a safe single-component id;
    only a missing pointer (a manager that never published) yields
    None.
    """
    try:
        return current_path(home).read_text(encoding="utf-8").strip()
    except OSError:
        return None


def refresh_launchers(csk_home: Path, registry: dict[str, Any]) -> None:
    """Converge the launchers to the given (active) registry, idempotently.

    The recovery shape: staging creates every name the registry
    exports, the live link points at the generation, and pruning
    removes everything else, so a previous interruption converges back
    to the registry's surface. With no active generation (a manager
    that never published) only the dispatcher is ensured. Callers hold
    the manager lock.
    """
    home = Path(csk_home)
    generation_id = _surface_generation_id(home)
    if generation_id is None:
        ensure_dispatcher(home)
        return
    stage_launchers(home, registry, generation_id=generation_id)
    if os.name != "nt":
        _point_shims_at(home, generation_id)
    prune_launchers(home, registry)


def recover_launchers(csk_home: Path) -> dict[str, Any]:
    """Reconcile the launchers against the active generation; return it.

    Callers hold the manager lock (``csk dispatch setup`` takes it), so
    recovery cannot interleave with a publication's stage/swap window.
    """
    home = Path(csk_home)
    registry = read_registry(home)
    generation_id = _surface_generation_id(home)
    if generation_id is None:
        ensure_dispatcher(home)
        return registry
    _maybe_fault("recover:stage")
    stage_launchers(home, registry, generation_id=generation_id)
    if os.name != "nt":
        _point_shims_at(home, generation_id)
    _maybe_fault("recover:prune")
    prune_launchers(home, registry)
    return registry


def stage_launchers(
    home: Path, registry: dict[str, Any], *, generation_id: str
) -> None:
    """Stage the dispatcher plus every validated shim for one generation.

    POSIX stages off-surface into
    ``shim-generations/<generation_id>/``: the live ``shims`` surface
    is untouched until the pointer swap, so a case-only rename can
    never corrupt the still-active generation. Windows (whose
    launchers are a separate task) stages the flat directory directly,
    without the atomic swap. One nonce covers the whole staging,
    derived from the generation id: distinct generations get distinct
    unpredictable nonces, while re-staging one generation is
    byte-identical (and atomic writes skip it entirely).
    """
    home = Path(home)
    ensure_dispatcher(home)
    if os.name == "nt":
        _stage_shims(home, registry, generation_id)
        return
    dispatcher = dispatcher_path(home)
    target_dir = shim_generations_dir(home) / generation_id
    target_dir.mkdir(parents=True, exist_ok=True)
    nonce = _staging_nonce(generation_id)
    for name in sorted(_exported_names(registry)):
        _require_publishable_command_name(name)
        _write_dispatch_shim(target_dir, name, dispatcher, nonce=nonce)


def _staging_nonce(generation_id: str) -> str:
    """Derive one staging's shim nonce from its generation id.

    The generation id carries 122 random bits, so the truncated
    digest is unpredictable and unique per generation; deriving it
    (instead of sampling per staging) keeps re-staging one
    generation byte-identical, so convergence rewrites nothing.
    """
    return hashlib.sha256(generation_id.encode("utf-8")).hexdigest()[:16]


def _stage_shims(
    home: Path, registry: dict[str, Any], generation_id: str
) -> None:
    """Stage shims into the flat live directory (Windows only)."""
    dispatcher = dispatcher_path(home)
    target_dir = shims_dir(home)
    target_dir.mkdir(parents=True, exist_ok=True)
    nonce = _staging_nonce(generation_id)
    for name in sorted(_exported_names(registry)):
        _require_publishable_command_name(name)
        _write_dispatch_shim(target_dir, name, dispatcher, nonce=nonce)


def _point_shims_at(home: Path, generation_id: str) -> None:
    """Atomically point the live ``shims`` surface at one generation.

    POSIX only (Windows keeps the flat directory and never calls
    this): the swap is one symlink rename, so concurrent bare calls
    resolve through the link per exec and always see one coherent
    generation. A legacy flat ``shims`` entry (pre-link managers)
    migrates aside first; the aside copy is removed after the link
    lands, and any leftover asides are dropped by the next prune, so
    a crash between the moves retries to the same link.
    """
    home = Path(home)
    link = shims_dir(home)
    target = str(shim_generations_dir(home) / generation_id)
    if not link.is_symlink() and os.path.lexists(link):
        aside = link.parent / (
            f".shims-flat-{os.getpid()}-{secrets.token_hex(4)}"
        )
        os.rename(link, aside)
        try:
            _swap_shims_link(link, target)
        except BaseException:
            # Restore the flat surface so the retry converges from a
            # coherent state instead of a missing one.
            try:
                os.rename(aside, link)
            except OSError:
                pass
            raise
        try:
            if aside.is_symlink() or aside.is_file():
                aside.unlink()
            else:
                shutil.rmtree(aside)
        except OSError:
            pass
        return
    _swap_shims_link(link, target)


def _swap_shims_link(link: Path, target: str) -> None:
    """Rename one fresh symlink over the ``shims`` link, atomically."""
    tmp_link = link.parent / (
        f".shims-link-{os.getpid()}-{secrets.token_hex(4)}"
    )
    try:
        tmp_link.unlink(missing_ok=True)
    except OSError:
        pass
    os.symlink(target, tmp_link)
    os.rename(tmp_link, link)


def prune_launchers(home: Path, registry: dict[str, Any]) -> None:
    """Converge the live surface to exactly what the registry exports.

    POSIX prunes staged generations except the active one and scrubs
    stray files from the active generation's directory, so the link
    always serves exactly the validated union. Windows (flat,
    non-atomic) removes flat shims the registry no longer exports.
    Callers pass the active registry; the active id resolves from the
    ``current`` pointer the activation just switched.
    """
    home = Path(home)
    if os.name == "nt":
        _prune_flat_shims(home, registry)
        return
    union = _exported_names(registry)
    active_id = _surface_generation_id(home)
    generations = shim_generations_dir(home)
    try:
        if generations.is_symlink():
            raise DispatchPublishError(
                f"dispatch staged-shims directory {generations} is a "
                f"symlink; refusing to prune generations through an alias"
            )
        children = sorted(generations.iterdir())
    except FileNotFoundError:
        children = []
    for child in children:
        if child.name == active_id:
            continue
        try:
            if child.is_symlink() or child.is_file():
                child.unlink()
            elif child.is_dir():
                shutil.rmtree(child)
        except OSError:
            continue
    # Leftover migration asides and link temps never serve; drop them.
    try:
        siblings = sorted(home.iterdir())
    except OSError:
        return
    for sibling in siblings:
        if not sibling.name.startswith((".shims-flat-", ".shims-link-")):
            continue
        try:
            if sibling.is_symlink() or sibling.is_file():
                sibling.unlink()
            elif sibling.is_dir():
                shutil.rmtree(sibling)
        except OSError:
            continue
    if active_id is None:
        return
    try:
        staged = sorted((generations / active_id).iterdir())
    except OSError:
        return
    for child in staged:
        if child.name in union:
            continue
        try:
            info = child.lstat()
        except OSError:
            continue
        if stat.S_ISDIR(info.st_mode) and not child.is_symlink():
            continue
        try:
            child.unlink()
        except OSError:
            continue


def _prune_flat_shims(home: Path, registry: dict[str, Any]) -> None:
    """Remove flat shims for names the registry no longer exports.

    Windows only: the flat directory is the live surface there (no
    atomic swap; Windows launchers are a separate task).
    """
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
    """Return the validated public names: the shared live union."""
    union: dict[str, None] = {}
    for name, _owner, _scope in _union_members(
        registry["projects"], registry["global"]["commands"]
    ):
        union.setdefault(name)
    return union


def _runtime_template_bytes() -> bytes:
    return Path(__file__).with_name("_dispatch_runtime.py").read_bytes()


def ensure_dispatcher_runtime(csk_home: Path) -> Path:
    """Install the standalone dispatcher copy; fail closed on drift."""
    runtime = dispatcher_runtime_path(csk_home)
    runtime.parent.mkdir(parents=True, exist_ok=True)
    template = _runtime_template_bytes()
    for marker in (b"from csk", b"import csk", b"from .", b"from csk."):
        if marker in template:
            raise DispatchPublishError(
                "dispatch runtime template imports the manager package; "
                "the standalone dispatcher must stay stdlib-only"
            )
    atomic_write_bytes(runtime, template, mode=0o644)
    return runtime


def dispatcher_wrapper_text(*, python: str, runtime: Path | str) -> str:
    """Return the generated ``/bin/sh`` dispatcher wrapper.

    The wrapper refuses a missing interpreter with repair guidance and
    execs the recorded absolute interpreter on the runtime copy. It
    holds no shell variables at all, so it exports nothing and cannot
    clobber caller-owned names (an assignment to a caller-exported
    name would keep the export attribute and leak into the runtime);
    resolve mode prints only its two-line protocol on stdout. Every
    interpolated path is a single-quoted literal in an unquoted
    position, so spaces and quotes in installation paths work and
    nothing evaluates.
    """
    quoted_python = sh_single_quote(python)
    quoted_runtime = sh_single_quote(str(runtime))
    lines = [
        "#!/bin/sh",
        "# Generated by csk dispatch: run the recorded interpreter on the",
        "# standalone dispatcher. Do not edit: republished on every install.",
        "# The wrapper holds no shell variables at all, so it exports",
        "# nothing and cannot clobber caller-owned names.",
        f"if [ ! -x {quoted_python} ]; then",
        '  echo "error: dispatch interpreter" '
        f"{quoted_python} "
        "\"is missing or not executable; rerun 'csk dispatch setup' "
        "or reinstall ('csk install' / 'csk global install') to "
        'republish dispatch" >&2',
        "  exit 1",
        "fi",
        f"exec {quoted_python} -I {quoted_runtime} " + '"$@"',
        "",
    ]
    return "\n".join(lines)


def ensure_dispatcher_wrapper(csk_home: Path) -> Path:
    """Install the ``/bin/sh`` wrapper execing the recorded interpreter."""
    target = dispatcher_path(csk_home)
    target.parent.mkdir(parents=True, exist_ok=True)
    executable = sys.executable
    if not os.path.isabs(executable):
        executable = os.path.abspath(executable)
    if not os.path.isabs(executable):
        raise DispatchPublishError(
            f"dispatch dispatcher cannot use interpreter path "
            f"{executable!r}: it must be absolute"
        )
    for scalar in ("\r", "\n", "\x00"):
        if scalar in executable:
            raise DispatchPublishError(
                f"dispatch dispatcher cannot use interpreter path "
                f"{executable!r}: it must not contain control characters"
            )
    # Isolated mode: the dispatcher ignores PYTHONPATH, user site and
    # startup hooks, so caller-controlled Python configuration can neither
    # shadow its stdlib imports nor slow its startup with site processing.
    text = dispatcher_wrapper_text(
        python=executable, runtime=dispatcher_runtime_path(csk_home)
    )
    atomic_write_bytes(target, text.encode("utf-8"), mode=0o755)
    return target


def ensure_dispatcher(csk_home: Path) -> Path:
    """Install the standalone dispatcher copy plus its ``/bin/sh`` wrapper."""
    ensure_dispatcher_runtime(csk_home)
    return ensure_dispatcher_wrapper(csk_home)


def sh_single_quote(text: str) -> str:
    """Quote one shell word as a single-quoted literal.

    POSIX ``sh``, ``bash`` and ``zsh`` treat every byte inside single
    quotes literally; an embedded quote becomes the ``'\\''`` sequence.
    The result is only ever spliced into an unquoted position, never
    inside double quotes, where single quotes would lose their meaning.
    """
    return "'" + text.replace("'", "'\\''") + "'"


def shim_text(
    dispatcher: Path | str, name: str, *, nonce: str | None = None
) -> str:
    """Return the ``/bin/sh`` shim resolving one command, then execing it.

    The shim runs the dispatcher in RESOLVE mode only and captures its
    two-line protocol (absolute target, then the project root or ``-``
    for global). A resolve failure propagates its message and exit code
    unchanged; a malformed protocol refuses instead of execing a guess.
    On success the shim sets ``CSK_PROJECT_ROOT`` (or unsets it for
    global) and execs the target directly, so caller signal
    dispositions, the signal mask and the environment reach it
    untouched. Every interpolated value is a single-quoted literal, so
    spaces and quotes in paths work and nothing evaluates; diagnostic
    messages pass the command name through ``printf %s`` so even a
    hostile name cannot regroup the message.

    Environment contract: apart from ``CSK_PROJECT_ROOT`` the target
    sees the caller's environment byte-identical. Every working
    variable carries the staging's random nonce
    (``_csk_<nonce>_<role>``), so working names cannot collide with
    caller names; each is saved on entry and restored before the final
    exec (a plain assignment would keep a caller-supplied export
    attribute and leak the shim's value into the target), and the
    target travels to the final exec in ``$1``, so no
    dispatcher-private value survives. The save slots themselves are
    unset before the exec. No caller-owned name is ever assigned or
    exported; the only stated bound is a caller that exports the
    generation's own nonce save-slot names (readable only from the
    installed shim bytes): those twelve scratch values are dropped
    rather than preserved. The text is pure POSIX ``sh`` (no
    ``local``, arrays or ``[[``), so it runs under ``sh``, ``bash``,
    ``dash`` and ``zsh`` alike.
    """
    if nonce is None:
        nonce = secrets.token_hex(8)
    if not nonce or any(char not in "0123456789abcdef" for char in nonce):
        raise DispatchPublishError(
            f"dispatch shim nonce must be lowercase hex: {nonce!r}"
        )
    quoted_dispatcher = sh_single_quote(str(dispatcher))
    quoted_name = sh_single_quote(name)
    malformed_format = (
        '"error: dispatch resolver for command %s returned '
        "malformed output; reinstall the project ('csk "
        "install') or the global set ('csk global install') to "
        'republish dispatch\\n"'
    )

    def ref(variable: str) -> str:
        return "${" + variable + "}"

    keys = ("dispatcher", "command", "out", "nl", "target", "root")
    var = {key: f"_csk_{nonce}_{key}" for key in keys}
    have = {key: f"_csk_{nonce}_have_{key}" for key in keys}
    save = {key: f"_csk_{nonce}_save_{key}" for key in keys}
    lines = [
        "#!/bin/sh",
        "# Generated by csk dispatch: resolve the pinned target, then",
        "# exec it directly. Do not edit: republished on every install.",
        "# Environment contract: the caller's environment reaches the",
        "# target untouched apart from CSK_PROJECT_ROOT. Every working",
        "# variable carries a per-generation nonce, so working names",
        "# cannot collide with caller names; each is saved on entry and",
        "# restored before the final exec, and the target travels in",
        "# $1, so no dispatcher-private value ever leaks into the target.",
    ]
    for key in keys:
        lines.append(f"{have[key]}=${{{var[key]}+set}}")
        lines.append(f"{save[key]}=${{{var[key]}-}}")
    lines.extend(
        [
            f"{var['dispatcher']}={quoted_dispatcher}",
            f"{var['command']}={quoted_name}",
            f"{var['out']}=$(\"{ref(var['dispatcher'])}\" resolve "
            f"\"{ref(var['command'])}\") || exit $?",
            f"{var['nl']}='",
            "'",
            f"{var['target']}=${{{var['out']}%%\"{ref(var['nl'])}\"*}}",
            f"{var['root']}=${{{var['out']}#*\"{ref(var['nl'])}\"}}",
            f"if [ \"{ref(var['target'])}\" = \"{ref(var['out'])}\" ] || "
            f"[ -z \"{ref(var['target'])}\" ]; then",
            f"  printf {malformed_format} \"{ref(var['command'])}\" >&2",
            "  exit 1",
            "fi",
            f"case \"{ref(var['root'])}\" in",
            f"  *\"{ref(var['nl'])}\"*)",
            f"    printf {malformed_format} \"{ref(var['command'])}\" >&2",
            "    exit 1",
            "    ;;",
            "esac",
            f"if [ \"{ref(var['root'])}\" = \"-\" ]; then",
            "  unset CSK_PROJECT_ROOT || exit 1",
            "else",
            f"  CSK_PROJECT_ROOT={ref(var['root'])}",
            "  export CSK_PROJECT_ROOT",
            "fi",
            f"set -- \"{ref(var['target'])}\" \"$@\"",
        ]
    )
    for key in keys:
        lines.extend(
            [
                f"if [ -n \"{ref(have[key])}\" ]; then",
                f"  {var[key]}={ref(save[key])}",
                f"  export {var[key]}",
                "else",
                f"  unset {var[key]}",
                "fi",
            ]
        )
    scratch = " ".join(
        [have[key] for key in keys] + [save[key] for key in keys]
    )
    lines.append(f"unset {scratch}")
    lines.append('exec "$@"')
    lines.append("")
    return "\n".join(lines)


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
    """Append the setup snippet once; return whether the file changed.

    A symlinked rc file keeps its link: the snippet lands in the link
    target, and the target's mode is preserved. A new file gets 0644.
    """
    if SETUP_MARKER_BEGIN not in snippet or SETUP_MARKER_END not in snippet:
        raise DispatchPublishError("dispatch setup snippet is missing markers")
    target = Path(rc_path)
    if target.is_symlink():
        target = Path(os.path.realpath(target))
    try:
        existing = target.read_text(encoding="utf-8")
    except FileNotFoundError:
        existing = ""
    if SETUP_MARKER_BEGIN in existing:
        return False
    prefix = existing
    if prefix and not prefix.endswith("\n"):
        prefix += "\n"
    try:
        mode: int | None = stat.S_IMODE(target.stat().st_mode)
    except OSError:
        mode = None
    atomic_write_bytes(
        target,
        (prefix + snippet).encode("utf-8"),
        mode=mode if mode is not None else 0o644,
    )
    return True


def _project_entry(
    home: Path,
    *,
    canonical_root: str,
    project_alias: str,
    checkout_alias: str | None,
    skills: list[SkillPin],
    commands: dict[str, CommandRecord],
    existing_projects: dict[str, Any],
    needs_migration: bool = False,
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
    if "\n" in canonical_root or "\r" in canonical_root:
        raise DispatchPublishError(
            f"dispatch project root must not contain a line break "
            f"(the resolve protocol is line-based): {canonical_root!r}"
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
        "checkout_id": _dispatch_runtime.resolve_checkout_id(
            existing_projects,
            canonical_root=canonical_root,
            root_ino=root_info.st_ino,
            case_insensitive=_dispatch_runtime.volume_case_insensitive(
                canonical_root, manager_home=str(home)
            ),
            normalization_insensitive=(
                _dispatch_runtime.volume_normalization_insensitive(
                    canonical_root, manager_home=str(home)
                )
            ),
        ),
        "canonical_root": canonical_root,
        "project_alias": project_alias,
        "checkout_alias": checkout_alias,
        "root_identity": {
            "st_ino": str(root_info.st_ino),
        },
        "needs_migration": needs_migration,
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
    if "\n" in target or "\r" in target:
        raise DispatchPublishError(
            f"dispatch command {name!r} target must not contain a line "
            f"break (the resolve protocol is line-based)"
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


def manager_home_inside_root(home: Path, root: Path) -> bool:
    """Return whether the manager home sits inside one checkout root.

    Path-first identity through the one shared comparison: the home's
    canonical components must extend the root's canonical components
    under the home volume's actual rules, and the home ancestor at the
    root's depth must still stat to the root's live inode. A bare
    inode coincidence without path ancestry never counts (inodes
    recycle across volumes). A root that cannot be stated contains
    nothing provable and reports False.
    """
    try:
        want = os.stat(root).st_ino
    except OSError:
        return False
    home_real = os.path.realpath(os.fspath(home))
    root_real = os.path.realpath(os.fspath(root))
    home_parts = _dispatch_runtime.split_components(home_real)
    root_parts = _dispatch_runtime.split_components(root_real)
    if len(home_parts) < len(root_parts):
        return False
    if not _dispatch_runtime.components_equal(
        root_parts,
        home_parts[: len(root_parts)],
        case_insensitive=_dispatch_runtime.volume_case_insensitive(
            home_real, manager_home=home_real
        ),
        normalization_insensitive=(
            _dispatch_runtime.volume_normalization_insensitive(
                home_real, manager_home=home_real
            )
        ),
    ):
        return False
    ancestor = (
        "/" + "/".join(home_parts[: len(root_parts)])
        if root_parts
        else "/"
    )
    try:
        return os.stat(ancestor).st_ino == want
    except OSError:
        return False


def _refuse_manager_inside_checkout(
    home: Path,
    *,
    new_root: str | None,
    registry: dict[str, Any],
) -> None:
    """Refuse publication when the manager home sits in a checkout.

    Path-first identity through the one shared comparison, uniform
    with selection: the home's canonical components must extend a
    root's canonical components under the home volume's actual rules,
    and the home ancestor at the root's depth must still stat to that
    root's live inode. A bare inode coincidence without path ancestry
    never counts. An unverifiable home or new root refuses (nothing
    provable about the layout being published); a registered root that
    cannot be stated is skipped (a stale record never vetoes an
    unrelated install, and a moved root no longer matches by path
    until re-registration).
    """
    home_real = os.path.realpath(os.fspath(home))
    home_parts = _dispatch_runtime.split_components(home_real)
    case_insensitive = _dispatch_runtime.volume_case_insensitive(
        home_real, manager_home=home_real
    )
    normalization_insensitive = (
        _dispatch_runtime.volume_normalization_insensitive(
            home_real, manager_home=home_real
        )
    )
    candidates: list[str] = []
    if new_root is not None:
        candidates.append(new_root)
    for checkout_id in sorted(registry["projects"]):
        candidates.append(registry["projects"][checkout_id]["canonical_root"])
    for root in candidates:
        try:
            want = os.stat(root).st_ino
        except OSError as exc:
            if root == new_root:
                raise DispatchPublishError(
                    f"dispatch manager home {home} cannot be verified "
                    f"outside the registered checkout at {new_root} "
                    f"({exc}); refusing to publish"
                ) from exc
            continue
        try:
            root_parts = _dispatch_runtime.split_components(root)
        except _dispatch_runtime.DispatchError:
            if root == new_root:
                raise DispatchPublishError(
                    f"dispatch manager home {home} cannot be verified "
                    f"outside the registered checkout at {new_root}; "
                    f"refusing to publish"
                )
            continue
        if len(home_parts) < len(root_parts):
            continue
        if not _dispatch_runtime.components_equal(
            root_parts,
            home_parts[: len(root_parts)],
            case_insensitive=case_insensitive,
            normalization_insensitive=normalization_insensitive,
        ):
            continue
        ancestor = (
            "/" + "/".join(home_parts[: len(root_parts)])
            if root_parts
            else "/"
        )
        try:
            confirmed = os.stat(ancestor).st_ino == want
        except OSError as exc:
            raise DispatchPublishError(
                f"dispatch manager home {home} cannot be verified "
                f"outside the registered checkout at {root} ({exc}); "
                f"refusing to publish"
            ) from exc
        if confirmed:
            raise DispatchPublishError(
                f"dispatch manager home {home} is inside the registered "
                f"checkout at {root}; refusing to publish. Use a manager "
                f"home outside every checkout"
            )


def referenced_skill_commits(csk_home: Path) -> set[tuple[str, str]]:
    """Return the (skill, commit) pins the active generation references.

    Install planning retains these runtime commits, so an interrupted
    publication leaves the still-active generation's bytes intact and
    the visible surface stays coherent. The post-install collector
    reclaims superseded commits once a new generation commits. Stale
    records stay referenced: their pins are retained, never collected
    out from under a record status still reports.
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

    problems, registry = _verify_digests(
        csk_home,
        select,
        remedy="reinstall the project ('csk install') to republish it",
    )
    if registry is not None:
        problems.extend(_stale_problems(registry, wanted))
        problems.extend(_migration_problems(registry, wanted))
    return problems


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

    problems, _registry = _verify_digests(
        csk_home,
        select,
        remedy="run 'csk global install' to republish it",
    )
    return problems


def _stale_problems(
    registry: dict[str, Any], wanted: set[str]
) -> list[str]:
    """Report records whose roots no longer match as status problems."""
    problems: list[str] = []
    for checkout_id in sorted(registry["projects"]):
        entry = registry["projects"][checkout_id]
        if entry["canonical_root"] not in wanted:
            continue
        status = _dispatch_runtime.classify_record(entry)
        if status == "replaced":
            problems.append(
                f"dispatch record for project at {entry['canonical_root']}: "
                f"its root no longer matches the recorded filesystem "
                f"identity (it moved or was replaced); reinstall the "
                f"project ('csk install') to republish it"
            )
        elif status == "unstatable":
            problems.append(
                f"dispatch record for project at {entry['canonical_root']}: "
                f"its root cannot be verified; reinstall the project "
                f"('csk install') to republish it"
            )
    return problems


def _migration_problems(
    registry: dict[str, Any], wanted: set[str]
) -> list[str]:
    """Report scopes still waiting for their pre-dispatch migration."""
    problems: list[str] = []
    for checkout_id in sorted(registry["projects"]):
        entry = registry["projects"][checkout_id]
        if entry["canonical_root"] not in wanted:
            continue
        if not entry.get("needs_migration", False):
            continue
        problems.append(
            f"dispatch record for project at {entry['canonical_root']}: "
            f"the project was installed before dispatch records existed "
            f"and calls inside it refuse until it migrates; run 'csk "
            f"install' in that project to publish its record"
        )
    return problems


def stale_scope_notes(csk_home: Path) -> list[str]:
    """Report ignored records whose roots no longer exist.

    These are notes, not problems: a deleted project cannot fail
    ``status --check``. Replaced or unverifiable roots are problems
    instead (see :func:`verify_project_digests`).
    """
    try:
        registry = read_registry(Path(csk_home))
    except DispatchPublishError:
        return []
    notes: list[str] = []
    for checkout_id, status in _dispatch_runtime.stale_records(registry):
        if status != "gone":
            continue
        entry = registry["projects"][checkout_id]
        notes.append(
            f"note: dispatch record for project at "
            f"{entry['canonical_root']} ({entry['project_alias']}) is "
            f"ignored: its root no longer exists"
        )
    return notes


def _verify_digests(
    csk_home: Path,
    select: Callable[
        [dict[str, Any]], list[tuple[str, dict[str, Any]]]
    ],
    *,
    remedy: str,
) -> tuple[list[str], dict[str, Any] | None]:
    home = Path(csk_home)
    try:
        registry = read_registry(home)
    except DispatchPublishError as exc:
        # A known publication read failure stays a failure here, never
        # absence: a missing pointer after publication is corruption even
        # when the pointer file itself is gone. A fresh manager raises
        # nothing (the empty registry applies) and stays clean.
        return [
            f"dispatch activation records cannot be verified: {exc}"
        ], None
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
    return problems, registry


def _registry_for_publish(
    home: Path,
) -> tuple[dict[str, Any], str | None, list[str]]:
    """Return (registry, clean current id or None, warnings).

    Publication never heals an unreadable registry from an older
    generation: that silently reverts pins. It refuses with repair
    guidance instead; ``csk dispatch recover`` is the explicit heal.
    """
    pointer = current_path(home)
    try:
        raw = pointer.read_bytes()
    except FileNotFoundError:
        if _dispatch_runtime.has_publication_artifacts(str(home)):
            raise DispatchPublishError(
                f"dispatch generation pointer {pointer} is missing though "
                f"this manager has published dispatch state. Run 'csk "
                f"dispatch recover' to restore the last readable "
                f"generation explicitly"
            )
        return _dispatch_runtime.empty_registry(), None, []
    except OSError as exc:
        raise DispatchPublishError(
            f"dispatch generation pointer {pointer} cannot be read ({exc}). "
            f"Run 'csk dispatch recover' to restore the last readable "
            f"generation explicitly"
        ) from exc
    try:
        current_id = raw.decode("utf-8").strip()
    except UnicodeDecodeError as exc:
        raise DispatchPublishError(
            f"dispatch generation pointer {pointer} is not valid UTF-8 "
            f"({exc}). Run 'csk dispatch recover' to restore the last "
            f"readable generation explicitly"
        ) from exc
    try:
        registry = _dispatch_runtime.load_registry(str(home))
    except _dispatch_runtime.DispatchError as exc:
        raise DispatchPublishError(_with_recover_guidance(exc)) from exc
    return registry, current_id, []


def _readable_generation(
    registry_path: Path, *, candidate_id: str
) -> dict[str, Any] | None:
    """Return the validated registry at ``registry_path``, if readable."""
    try:
        data = protocol_json.loads(registry_path.read_bytes())
    except (OSError, protocol_json.ProtocolJSONError):
        return None
    try:
        return _dispatch_runtime.validate_registry_data(
            data, generation_id=candidate_id
        )
    except _dispatch_runtime.DispatchError:
        return None


def _find_last_readable_generation(
    home: Path,
) -> tuple[str, dict[str, Any]] | None:
    """Return the newest readable generation, or None when there is none.

    Symlinked generation entries are skipped, never followed: recovery
    must not restore authoritative bytes through an alias.
    """
    candidates: list[tuple[float, str, Path]] = []
    generations = generations_dir(home)
    try:
        children = list(generations.iterdir())
    except FileNotFoundError:
        children = []
    for child in children:
        try:
            if child.is_symlink():
                continue
            registry_path = child / "registry.json"
            if registry_path.is_symlink():
                continue
            info = registry_path.stat()
        except OSError:
            continue
        candidates.append((info.st_mtime_ns, child.name, registry_path))
    candidates.sort(reverse=True)
    for _, candidate_id, registry_path in candidates:
        registry = _readable_generation(
            registry_path, candidate_id=candidate_id
        )
        if registry is not None:
            return candidate_id, registry
    return None


def recover_registry(home: Path, *, reset: bool = False) -> str:
    """Heal dispatch explicitly; return the human-readable outcome.

    When the active registry reads, the launchers are still converged
    against it and unreadable inactive generations are removed (so any
    retry, including one after an interrupted reset, converges); there
    is nothing further to recover. When it does not, the newest
    readable older generation is activated through the one shared
    activation path (a loud, explicit downgrade: reinstall affected
    projects to restore newer records). With no readable generation at
    all, ``reset=True`` archives the corrupt pointer, publishes a fresh
    empty generation and removes corrupt generations, so a following
    reinstall works; without it, recovery refuses and names the reset
    command. Callers hold the manager lock.
    """
    manager = Path(home)
    try:
        _dispatch_runtime.load_registry(str(manager))
    except _dispatch_runtime.DispatchError as exc:
        failure: _dispatch_runtime.DispatchError | None = exc
    else:
        recover_launchers(manager)
        try:
            current_id = current_path(manager).read_text(encoding="utf-8").strip()
        except OSError:
            current_id = ""
        removed, removal_errors = _remove_unreadable_generations(
            manager, keep=current_id
        )
        outcome = (
            f"dispatch registry is healthy (generation "
            f"{current_id or '(empty)'}); nothing to recover"
        )
        if removed:
            outcome += f" Removed {removed} unreadable inactive generation(s)."
        if removal_errors:
            outcome += " " + " ".join(removal_errors)
        return outcome
    assert failure is not None
    if not reset:
        candidate = _find_last_readable_generation(manager)
        if candidate is None:
            raise DispatchPublishError(
                f"dispatch registry cannot be read ({failure}); no "
                f"readable generation remains. Run 'csk dispatch recover "
                f"--reset' to archive the corrupt pointer and start fresh, "
                f"then reinstall affected projects"
            )
        generation_id, registry = candidate
        _activate_generation(manager, generation_id, registry)
        return (
            f"recovered dispatch generation {generation_id}; the "
            f"unreadable registry was replaced explicitly "
            f"({failure}). Reinstall affected projects ('csk install') "
            f"to restore any newer dispatch records"
        )
    pointer = current_path(manager)
    try:
        raw = pointer.read_bytes()
    except FileNotFoundError:
        raw = b""
    except OSError as exc:
        raise DispatchPublishError(
            f"dispatch generation pointer {pointer} cannot be read ({exc}); "
            f"recovery refused"
        ) from exc
    if raw:
        archive = pointer.parent / f"current.corrupt-{time.time_ns()}"
        try:
            pointer.rename(archive)
        except OSError as exc:
            raise DispatchPublishError(
                f"dispatch generation pointer {pointer} cannot be "
                f"archived ({exc}); recovery refused"
            ) from exc
        archived = f"archived as {archive.name}; "
    else:
        archived = ""
    empty = _dispatch_runtime.empty_registry()
    generation_id = uuid.uuid4().hex
    _maybe_fault("publish:write_generation")
    _write_generation_file(
        manager,
        generation_id,
        protocol_json.canonical_bytes(empty) + b"\n",
    )
    _activate_generation(manager, generation_id, empty)
    _maybe_fault("recover:remove_corrupt")
    removed, removal_errors = _remove_unreadable_generations(
        manager, keep=generation_id
    )
    outcome = (
        f"dispatch registry reset ({archived}unreadable state was "
        f"{failure}). Reinstall affected projects ('csk install') and the "
        f"global set ('csk global install') to republish dispatch records"
    )
    if removed:
        outcome += f" Removed {removed} corrupt generation(s)."
    if removal_errors:
        outcome += " " + " ".join(removal_errors)
    return outcome


def _remove_unreadable_generations(
    home: Path, *, keep: str
) -> tuple[int, list[str]]:
    """Remove corrupt generations; return (removed count, error notes).

    Readable generations stay (recovery may still select them); the
    freshly published generation stays by id. A symlinked generations
    directory refuses outright instead of deleting through an alias,
    and symlinked entries are unlinked as links, never traversed.
    Removal failures are reported in the outcome, never fatal: the
    empty generation is already active, so reinstalling works anyway.
    """
    generations = generations_dir(home)
    try:
        if generations.is_symlink():
            raise DispatchPublishError(
                f"dispatch generations directory {generations} is a "
                f"symlink; refusing to remove generations through an alias"
            )
        children = list(generations.iterdir())
    except FileNotFoundError:
        return 0, []
    removed = 0
    errors: list[str] = []
    for child in sorted(children):
        if child.name == keep:
            continue
        try:
            if child.is_symlink():
                child.unlink()
                removed += 1
                continue
            if not child.is_dir():
                continue
            registry_path = child / "registry.json"
            if registry_path.is_symlink():
                # Unlink the alias itself, then the (only corrupt)
                # husk around it; never traverse into its target.
                registry_path.unlink()
                shutil.rmtree(child)
                removed += 1
                continue
            if (
                _readable_generation(
                    registry_path, candidate_id=child.name
                )
                is not None
            ):
                continue
            shutil.rmtree(child)
            removed += 1
        except OSError as exc:
            errors.append(
                f"Could not remove corrupt generation {child.name} ({exc})."
            )
    return removed, errors


def _activate_generation(
    home: Path, generation_id: str, registry: dict[str, Any]
) -> None:
    """Activate one generation: stage off-surface, switch, reconcile.

    The one function recovery and publication share. Staging lands in
    the new generation's own directory (plus the dispatcher), so an
    interruption before the switch leaves the old generation with its
    old surface byte-identical; the switch swaps the ``current``
    pointer and the live ``shims`` link together, so the visible
    surface is always one coherent generation; reconciling prunes
    superseded staged generations, so an interruption there leaves the
    new generation exact with invisible leftovers a retry drops.
    """
    _maybe_fault("activate:stage_launchers")
    stage_launchers(home, registry, generation_id=generation_id)
    _maybe_fault("activate:swap_current")
    _swap_current_pointer(home, generation_id)
    if os.name != "nt":
        _point_shims_at(home, generation_id)
    _maybe_fault("activate:reconcile")
    prune_launchers(home, registry)


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
    _maybe_fault("publish:write_generation")
    _write_generation_file(home, generation_id, payload)
    _activate_generation(home, generation_id, registry)
    return generation_id


def _write_generation_file(
    home: Path, generation_id: str, payload: bytes
) -> Path:
    generation = generations_dir(home) / generation_id
    generation.mkdir(parents=True, exist_ok=True)
    registry_path = generation / "registry.json"
    atomic_write_bytes(registry_path, payload, mode=0o644)
    return registry_path


def _swap_current_pointer(home: Path, generation_id: str) -> None:
    pointer = current_path(home)
    pointer.parent.mkdir(parents=True, exist_ok=True)
    atomic_write_bytes(
        pointer, (generation_id + "\n").encode("utf-8"), mode=0o644
    )


def _write_dispatch_shim(
    target_dir: Path, name: str, dispatcher: Path, *, nonce: str
) -> None:
    atomic_write_bytes(
        target_dir / name,
        shim_text(dispatcher, name, nonce=nonce).encode("utf-8"),
        mode=0o755,
    )
