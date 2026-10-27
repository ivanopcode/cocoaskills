"""Admission and private staging permissions for manager-published skill trees.

Inventories come from retained source snapshots or install markers, never from
the live runtime being checked. Permission changes here apply only to private
staging copies; the transaction engine journals publication and backup cleanup.
"""

from __future__ import annotations

import os
from pathlib import Path
import shlex
import stat
from typing import Iterable, Mapping

from . import install_marker, skillspec


CODE_RUNTIME_TREE_MODIFIED = "runtime_tree_modified"
AUTHORING_GUIDE = "https://github.com/ivanopcode/cocoaskills/blob/main/docs/skill-authoring.md"


class RuntimeTreeModified(Exception):
    """A foreign entry must be removed explicitly before publication can proceed."""

    code = CODE_RUNTIME_TREE_MODIFIED


class RuntimeInventoryUnavailable(Exception):
    """An unverified generation cannot be adopted or silently discarded."""


def _entries(root: Path) -> list[tuple[Path, int]]:
    """Walk real directories only, without opening links or special files."""
    entries: list[tuple[Path, int]] = []
    pending = [root]
    while pending:
        directory = pending.pop()
        for path in sorted(directory.iterdir()):
            mode = path.lstat().st_mode
            entries.append((path, mode))
            if stat.S_ISDIR(mode):
                pending.append(path)
    return sorted(entries)


def _add_parents(inventory: dict[str, str], relative: str) -> None:
    for parent in Path(relative).parents:
        if parent != Path("."):
            inventory[parent.as_posix()] = "directory"


def runtime_inventory(snapshot: Path) -> dict[str, str]:
    """Describe the paths csk publishes from one retained, immutable snapshot."""
    spec = skillspec.load_skill_spec(snapshot)
    entries = [entry for root in spec.runtime_roots for entry in _entries(snapshot / root)]
    inventory = runtime_inventory_from_files(
        spec, (path.relative_to(snapshot).as_posix() for path, mode in entries if not stat.S_ISDIR(mode))
    )
    for path, mode in entries:
        if stat.S_ISDIR(mode):
            inventory[path.relative_to(snapshot).as_posix()] = "directory"
    return inventory


def runtime_inventory_from_files(spec: skillspec.SkillSpec, files: Iterable[str]) -> dict[str, str]:
    """Describe a verified frozen capture before its store record is published."""
    inventory: dict[str, str] = {}
    if spec.runtime_roots:
        for root in spec.runtime_roots:
            inventory[root] = "directory"
            _add_parents(inventory, root)
        for relative in files:
            if any(relative.startswith(root + "/") for root in spec.runtime_roots):
                inventory[relative] = "file"
                _add_parents(inventory, relative)
    else:
        for name, command in spec.commands.items():
            if command.type != "script":
                continue
            command_path = command.win_path if os.name == "nt" else command.unix_path
            if command_path is not None:
                filename = name + ".cmd" if os.name == "nt" and not name.endswith(".cmd") else name
                inventory[f"bin/{filename}"] = "file"
                inventory["bin"] = "directory"
    return inventory


def validate_tree(
    path: Path, *, home: Path, skill: str, member: str,
    inventory: Mapping[str, str] | None,
) -> None:
    """Refuse foreign paths without changing modes, bytes, or namespace entries."""
    try:
        root_mode = path.lstat().st_mode
    except FileNotFoundError:
        return
    # A substituted root is handled by the existing publication-boundary gate.
    if not stat.S_ISDIR(root_mode):
        return
    entries = _entries(path)
    foreign: list[Path] = []
    special: list[Path] = []
    for entry, mode in entries:
        kind = "directory" if stat.S_ISDIR(mode) else "file"
        if not (stat.S_ISDIR(mode) or stat.S_ISREG(mode)):
            special.append(entry)
        relative = entry.relative_to(path).as_posix()
        if inventory is not None and inventory.get(relative) != kind:
            foreign.append(entry)
    if not special and not foreign:
        return
    offending = special[0] if special else foreign[0]
    remedy = offending
    for parent in reversed((offending, *offending.parents)):
        if parent in foreign:
            remedy = parent
            break
    try:
        display = "$CSK_HOME/" + offending.relative_to(home).as_posix()
    except ValueError:
        display = str(offending)
    if os.name == "nt":
        quoted = "'" + str(remedy).replace("'", "''") + "'"
        command = f"Remove-Item -LiteralPath {quoted} -Recurse -Force"
    else:
        command = f"rm -rf -- {shlex.quote(str(remedy))}"
    raise RuntimeTreeModified(
        f"{CODE_RUNTIME_TREE_MODIFIED}: skill {skill!r}, member {member!r}: "
        f"foreign entry {display!r}. Skills must not write into their installed "
        f"runtime tree; see {AUTHORING_GUIDE}\n"
        f"remediation: remove only the foreign entry with {command}; "
        "then rerun the same command"
    )


def validate_context(path: Path, *, home: Path) -> None:
    """Use a published context's marker as its file inventory, when available."""
    marker_path = path / ".csk-install.json"
    try:
        if not stat.S_ISDIR(path.lstat().st_mode):
            return
        if not stat.S_ISREG(marker_path.lstat().st_mode):
            return
        marker = install_marker.read_install_marker(marker_path.read_bytes())
    except (OSError, install_marker.InstallMarkerError):
        return
    inventory = {name: "file" for name in marker.files}
    inventory[".csk-install.json"] = "file"
    for name in tuple(inventory):
        _add_parents(inventory, name)
    member = getattr(marker, "commit", None)
    if member is None:
        package = marker.package if isinstance(marker, install_marker.InstallMarkerV5) else None
        commit = getattr(package, "commit", None)
        member = commit.hex if commit is not None else str(getattr(package, "snapshot", "local snapshot"))
    validate_tree(path, home=home, skill=marker.name, member=member, inventory=inventory)


def validate_legacy_runtime(path: Path, *, home: Path) -> None:
    """Find a legacy generation's content-addressed source snapshot, if retained."""
    if path.name.startswith("."):
        # Private interrupted staging is not a published member generation.
        return
    try:
        mode = path.lstat().st_mode
    except FileNotFoundError:
        return
    if not stat.S_ISDIR(mode):
        return
    skill, commit = path.parent.name, path.name
    inventory = None
    cache = home / "cache"
    if cache.is_dir():
        # Equal Git commit IDs identify the same source tree across aliases.
        for snapshot in sorted(cache.glob(f"**/{commit}/snapshot")):
            if snapshot.is_dir() and not snapshot.is_symlink():
                inventory = runtime_inventory(snapshot)
                break
    if inventory is None:
        # Still report a known unsafe entry with its precise removal remedy.
        validate_tree(path, home=home, skill=skill, member=commit, inventory=None)
        raise RuntimeInventoryUnavailable(
            f"runtime_inventory_unavailable: skill {skill!r}, member {commit!r}: "
            "the retained source snapshot is missing; restore it from the trusted "
            "source before retrying. The runtime tree has been preserved"
        )
    validate_tree(path, home=home, skill=skill, member=commit, inventory=inventory)


def validate_legacy_store(home: Path) -> None:
    """Validate every generation copied by legacy private materialization."""
    root = home / "runtime"
    try:
        if not stat.S_ISDIR(root.lstat().st_mode):
            return
    except FileNotFoundError:
        return
    for skill in sorted(root.iterdir()):
        if not stat.S_ISDIR(skill.lstat().st_mode):
            continue
        for member in sorted(skill.iterdir()):
            if not member.name.startswith("."):
                validate_legacy_runtime(member, home=home)


def make_private_writable(path: Path) -> None:
    """Thaw an unpublished copy; never call this on a live runtime generation."""
    if os.name != "posix" or not path.exists():
        return
    entries = [(path, path.lstat().st_mode), *_entries(path)]
    for entry, mode in entries:
        if stat.S_ISDIR(mode):
            entry.chmod(stat.S_IMODE(mode) | stat.S_IRWXU)
        elif stat.S_ISREG(mode):
            entry.chmod(stat.S_IMODE(mode) | stat.S_IRUSR | stat.S_IWUSR)


def seal_private_runtime(path: Path) -> None:
    """Record read-only desired modes; publication finalizes them durably."""
    if os.name != "posix":
        return
    entries = [(path, path.lstat().st_mode), *_entries(path)]
    for entry, mode in reversed(entries):
        if stat.S_ISREG(mode) or stat.S_ISDIR(mode):
            entry.chmod(stat.S_IMODE(mode) & ~0o222)
