"""Dispatch generation publication for project-aware bare commands.

``csk install`` and ``csk global install`` publish one dispatch generation per
successful install: project records hold a stable checkout identity, the
canonical root, the active skill pins, the normalized command owners and
the absolute activation targets, all derived from that install's publication,
never from Skillfile text or checkout files at call time. The POSIX slice
also publishes ``<manager>/shims/<cmd>`` launchers that exec the absolute
manager dispatcher; see :mod:`csk._dispatch_runtime` for the standalone
call-time half.

Identity, the error taxonomy and command composition are implemented once
in :mod:`csk._dispatch_runtime` and shared: this publisher calls the same
``resolve_checkout_id`` / ``compose_layers`` the dispatcher runs, so
publication and dispatch cannot disagree.

Every dispatch file write goes through :func:`atomic_write_bytes` (temp
file in the same directory, fsync, rename); a live file is never unlinked
before its replacement is staged, so readers always see old-or-new bytes.

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
import os
import secrets
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

#: Publication steps, in commit order. The state-machine test interrupts
#: the commit at every one of these: the visible surface must stay
#: old-or-new and dispatch must keep working throughout.
PUBLISH_STEPS: tuple[str, ...] = (
    "publish:write_generation",
    "publish:stage_runtime",
    "publish:stage_wrapper",
    "publish:stage_shims",
    "publish:swap_current",
    "publish:prune_shims",
)

#: Recovery steps. ``recover_launchers`` and the ``recover --reset``
#: convergence share them, so interrupting recovery is covered once.
RECOVER_STEPS: tuple[str, ...] = (
    "recover:stage",
    "recover:prune",
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
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temporary_name, mode)
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
    directory. The probe is removed before returning; a failed probe
    refuses publication rather than guessing the volume's rules.
    """
    target_dir = shims_dir(csk_home)
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
        existing_projects=registry["projects"],
    )
    case_insensitive = shims_case_insensitive(home)
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
) -> tuple[str, list[str]]:
    """Publish an empty dispatch scope for one registered project.

    Registration paths call this so a registered but never-installed
    project forms a scope boundary: calls from inside it use the global
    fallback (or report unavailable), never an enclosing project's set.
    Re-adding an already published scope keeps its record untouched,
    including the installed pins when the same directory is re-added
    under another spelling.
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
        existing_projects=registry["projects"],
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
    refresh_launchers(home, registry)
    registry["global"] = {"skills": merged_skills, "commands": merged_commands}
    registry["shim_case_insensitive"] = case_insensitive
    return _commit_if_changed(home, registry, current_id=current_id), warnings


def refresh_launchers(csk_home: Path, registry: dict[str, Any]) -> None:
    """Publish the dispatcher plus one shim per exported name, idempotently.

    This is the recovery shape: staging creates every name the given
    registry exports, pruning removes names it no longer exports, so a
    previous interruption converges back to the registry's surface.
    Callers hold the manager lock.
    """
    home = Path(csk_home)
    stage_launchers(home, registry)
    prune_launchers(home, registry)


def recover_launchers(csk_home: Path) -> dict[str, Any]:
    """Reconcile the launchers against the active generation; return it.

    Callers hold the manager lock (``csk dispatch setup`` takes it), so
    recovery cannot interleave with a publication's stage/swap window.
    """
    home = Path(csk_home)
    registry = read_registry(home)
    _maybe_fault("recover:stage")
    stage_launchers(home, registry)
    _maybe_fault("recover:prune")
    prune_launchers(home, registry)
    return registry


def stage_launchers(home: Path, registry: dict[str, Any]) -> None:
    """Create the dispatcher plus every shim the registry exports.

    Additive only: names outside the registry are left for
    :func:`prune_launchers`, so staging before the ``current`` switch
    can never remove a name the still-active generation needs.
    """
    ensure_dispatcher(home)
    _stage_shims(home, registry)


def _stage_shims(home: Path, registry: dict[str, Any]) -> None:
    dispatcher = dispatcher_path(home)
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

    The wrapper snapshots the caller's locale variables (so the runtime
    can remove exactly what the interpreter startup adds), refuses a
    missing interpreter with repair guidance, and execs the recorded
    absolute interpreter on the runtime copy. Every interpolated path
    is a single-quoted literal in an unquoted position, so spaces and
    quotes in installation paths work.
    """
    lines = [
        "#!/bin/sh",
        "# Generated by csk dispatch: run the recorded interpreter on the",
        "# standalone dispatcher. Do not edit: republished on every install.",
        f"_CSK_PYTHON={sh_single_quote(python)}",
        f"_CSK_RUNTIME={sh_single_quote(str(runtime))}",
        'if [ -z "${LC_CTYPE+x}" ]; then',
        "  _CSK_ENV_SET_LC_CTYPE=0; _CSK_ENV_VAL_LC_CTYPE=",
        "else",
        '  _CSK_ENV_SET_LC_CTYPE=1; _CSK_ENV_VAL_LC_CTYPE="$LC_CTYPE"',
        "fi",
        'if [ -z "${__CF_USER_TEXT_ENCODING+x}" ]; then',
        "  _CSK_ENV_SET_CF=0; _CSK_ENV_VAL_CF=",
        "else",
        '  _CSK_ENV_SET_CF=1; _CSK_ENV_VAL_CF="$__CF_USER_TEXT_ENCODING"',
        "fi",
        "export _CSK_ENV_SET_LC_CTYPE _CSK_ENV_VAL_LC_CTYPE "
        "_CSK_ENV_SET_CF _CSK_ENV_VAL_CF",
        'if [ ! -x "$_CSK_PYTHON" ]; then',
        "  echo \"error: dispatch interpreter $_CSK_PYTHON is missing or "
        "not executable; rerun 'csk dispatch setup' or reinstall ('csk "
        "install' / 'csk global install') to republish dispatch\" >&2",
        "  exit 1",
        "fi",
        'exec "$_CSK_PYTHON" -I "$_CSK_RUNTIME" "$@"',
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
        "checkout_id": _dispatch_runtime.resolve_checkout_id(
            existing_projects,
            canonical_root=canonical_root,
            root_ino=root_info.st_ino,
        ),
        "canonical_root": canonical_root,
        "project_alias": project_alias,
        "checkout_alias": checkout_alias,
        "root_identity": {
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

    The comparison is by filesystem identity, not spelling: the home's
    ancestors are walked with ``stat`` against the root's inode, so
    symlinks and case aliases cannot hide the overlap. A root that
    cannot be stated contains nothing provable and reports False.
    """
    try:
        want_info = os.stat(root)
    except OSError:
        return False
    want = want_info.st_ino
    node = os.path.realpath(os.fspath(home))
    while True:
        try:
            info = os.stat(node)
        except OSError:
            return True
        if info.st_ino == want:
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
    home_chain: set[int] = set()
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
        home_chain.add(info.st_ino)
        parent = os.path.dirname(node)
        if parent == node:
            break
        node = parent
    candidates: dict[int, str] = {}
    if new_root is not None:
        try:
            info = os.stat(new_root)
        except OSError as exc:
            raise DispatchPublishError(
                f"dispatch manager home {home} cannot be verified "
                f"outside the registered checkout at {new_root} ({exc}); "
                f"refusing to publish"
            ) from exc
        candidates[info.st_ino] = new_root
    for checkout_id in sorted(registry["projects"]):
        entry = registry["projects"][checkout_id]
        root = entry["canonical_root"]
        try:
            info = os.stat(root)
        except OSError:
            pass
        else:
            candidates.setdefault(info.st_ino, root)
        recorded = entry.get("root_identity", {})
        if (
            isinstance(recorded, dict)
            and isinstance(recorded.get("st_ino"), str)
            and recorded["st_ino"].isdigit()
        ):
            candidates.setdefault(int(recorded["st_ino"]), root)
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
        if current_path(home).exists():
            return [
                f"dispatch activation records cannot be verified: {exc}"
            ], None
        return [], None
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


def _find_last_readable_generation(
    home: Path,
) -> tuple[str, dict[str, Any]] | None:
    """Return the newest readable generation, or None when there is none."""
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
        return candidate_id, registry
    return None


def recover_registry(home: Path, *, reset: bool = False) -> str:
    """Heal dispatch explicitly; return the human-readable outcome.

    When the active registry reads, there is nothing to recover. When
    it does not, the newest readable older generation becomes current
    again (a loud, explicit downgrade: reinstall affected projects to
    restore newer records). With no readable generation at all,
    ``reset=True`` archives the corrupt pointer and starts fresh;
    without it, recovery refuses and names the reset command. Callers
    hold the manager lock.
    """
    manager = Path(home)
    try:
        _dispatch_runtime.load_registry(str(manager))
    except _dispatch_runtime.DispatchError as exc:
        failure: _dispatch_runtime.DispatchError | None = exc
    else:
        try:
            current_id = current_path(manager).read_text(encoding="utf-8")
        except OSError:
            current_id = ""
        return (
            f"dispatch registry is healthy (generation "
            f"{current_id.strip() or '(empty)'}); nothing to recover"
        )
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
        _swap_current_pointer(manager, generation_id)
        refresh_launchers(manager, registry)
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
    refresh_launchers(manager, _dispatch_runtime.empty_registry())
    return (
        f"dispatch registry reset ({archived}unreadable state was "
        f"{failure}). Reinstall affected projects ('csk install') and the "
        f"global set ('csk global install') to republish dispatch records"
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
    _maybe_fault("publish:write_generation")
    _write_generation_file(home, generation_id, payload)
    # Launchers before the pointer switch: staging is additive, so an
    # interruption here leaves the old generation with its old surface
    # (plus inert extra shims the old dispatcher reports unavailable).
    # Pruning runs after the switch; an interruption there leaves the
    # new generation with stale shims the new dispatcher reports
    # unavailable. Either way the visible surface is one coherent
    # generation, and recovery converges the shims to it.
    _maybe_fault("publish:stage_runtime")
    ensure_dispatcher_runtime(home)
    _maybe_fault("publish:stage_wrapper")
    ensure_dispatcher_wrapper(home)
    _maybe_fault("publish:stage_shims")
    _stage_shims(home, registry)
    _maybe_fault("publish:swap_current")
    _swap_current_pointer(home, generation_id)
    _maybe_fault("publish:prune_shims")
    prune_launchers(home, registry)
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


def _write_dispatch_shim(target_dir: Path, name: str, dispatcher: Path) -> None:
    content = ("#!/bin/sh\n" + shim_line(dispatcher, name) + "\n").encode(
        "utf-8"
    )
    atomic_write_bytes(target_dir / name, content, mode=0o755)
