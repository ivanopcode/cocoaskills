"""Resolve manager tools without executing project or csk command shims."""

from __future__ import annotations

import os
import stat
from functools import lru_cache
from pathlib import Path


_MAX_SYMLINK_HOPS = 40


def _has_shim_directory_name(directory: Path) -> bool:
    # normcase does not fold case on macOS, even on case-insensitive volumes.
    # Treat conventional shim directory names case-insensitively on every host.
    parts = tuple(part.casefold() for part in directory.parts)
    if any(parts[i:i + 2] == (".agents", "bin") for i in range(len(parts) - 1)):
        return True
    if any(
        parts[i:i + 3] == (".cocoaskills", "global", "bin")
        for i in range(len(parts) - 2)
    ):
        return True
    config = os.environ.get("CSK_CONFIG")
    if config is not None:
        global_bin = Path(config).expanduser().absolute().parent / "global" / "bin"
        if directory.is_relative_to(global_bin):
            return True
    return False


def _is_shim_directory(directory: Path) -> bool:
    if _has_shim_directory_name(directory):
        return True
    # CSK_CONFIG can relocate the canonical global shim directory.
    config = os.environ.get("CSK_CONFIG")
    if config:
        global_bin = Path(config).expanduser().absolute().parent / "global" / "bin"
        if directory.is_relative_to(global_bin) or directory.is_relative_to(global_bin.resolve()):
            return True
        # A relocated root can have aliases in any of its path components.
        # Compare directory identities as well as lexical/resolved prefixes.
        try:
            global_identity = global_bin.stat()
        except FileNotFoundError:
            pass
        else:
            if any(
                os.path.samestat(parent.stat(), global_identity)
                for parent in (directory, *directory.parents)
            ):
                return True
    # Published user-bin forwarders carry this ledger. Check ancestors too,
    # so nested PATH entries cannot bypass the directory exclusion.
    return any(
        _has_managed_ledger(parent / ".csk-managed.json")
        for parent in (directory, *directory.parents)
    )


def _has_managed_ledger(path: Path) -> bool:
    # A failed lookup is not evidence that the directory is unmanaged. lstat
    # also preserves the marker when its symlink target is broken.
    try:
        path.lstat()
    except FileNotFoundError:
        return False
    return True


def _admit_directory(directory: Path, budget: int) -> tuple[Path, int] | None:
    """Resolve directory aliases without erasing intermediate shim locations."""
    if _has_shim_directory_name(directory):
        return None
    current = Path(directory.anchor)
    pending = list(directory.parts[1:])
    seen: set[tuple[Path, tuple[str, ...]]] = set()
    hops = 0
    while pending:
        component = pending.pop(0)
        if component == "..":
            current = current.parent
            continue
        hop = current / component
        identity = hop.lstat()
        # Windows junctions are directory aliases even though lstat reports
        # S_IFDIR, rather than S_IFLNK. readlink supports them as well.
        junction_tag = getattr(stat, "IO_REPARSE_TAG_MOUNT_POINT", None)
        is_link = stat.S_ISLNK(identity.st_mode) or (
            junction_tag is not None and getattr(identity, "st_reparse_tag", 0) == junction_tag
        )
        if is_link:
            if _is_shim_directory(current):
                return None
            if _has_shim_directory_name(hop):
                return None
            state = (hop, tuple(pending))
            if state in seen or hops >= budget:
                return None
            seen.add(state)
            hops += 1
            target = Path(os.readlink(hop))
            lexical_target = target if target.is_absolute() else current / target
            if _has_shim_directory_name(lexical_target):
                return None
            if target.is_absolute():
                current = Path(target.anchor)
                pending = list(target.parts[1:]) + pending
            else:
                pending = list(target.parts) + pending
        else:
            if not stat.S_ISDIR(identity.st_mode):
                return None
            current = hop
            if _is_shim_directory(current):
                return None
    if _is_shim_directory(current):
        return None
    return current, hops


def _admit_executable(candidate: Path) -> str | None:
    if not candidate.is_absolute():
        return None
    try:
        if _has_shim_directory_name(candidate.parent):
            return None
        hop = candidate
        seen: set[Path] = set()
        hops = 0
        while True:
            # Check before following each link: production POSIX shims are
            # symlinks, so checking only the final target loses their ancestry.
            if _has_shim_directory_name(hop.parent):
                return None
            admitted_directory = _admit_directory(hop.parent, _MAX_SYMLINK_HOPS - hops)
            if admitted_directory is None:
                return None
            directory, directory_hops = admitted_directory
            hops += directory_hops
            # Canonicalize the link's own directory before interpreting a
            # relative target, keeping the lexical checks above as well.
            hop = directory / hop.name
            identity = hop.lstat()
            if not stat.S_ISLNK(identity.st_mode):
                break
            link_identity = hop
            if link_identity in seen or hops >= _MAX_SYMLINK_HOPS:
                return None
            seen.add(link_identity)
            hops += 1
            target = Path(os.readlink(hop))
            hop = target if target.is_absolute() else hop.parent / target
        resolved = hop.resolve(strict=True)
        if _is_shim_directory(resolved.parent):
            return None
        if not stat.S_ISREG(resolved.stat().st_mode) or not os.access(resolved, os.X_OK):
            return None
    except (OSError, RuntimeError):
        return None
    return os.fspath(resolved)


@lru_cache(maxsize=None)
def resolve_tool(
    name: str, *, search_path: str | None = None, executable: str | None = None,
) -> str:
    """Return an absolute regular executable, or fail without a PATH fallback.

    Default lookups are frozen on first use for the process. Callers that
    already captured the operator PATH may supply that snapshot; each distinct
    snapshot is resolved once. Explicit paths pass the same admission checks.
    Windows lookup walks only absolute PATH entries, never the implicit CWD.
    """
    if not name or Path(name).name != name or "/" in name or "\\" in name:
        raise ValueError("manager tool name must be a basename")
    if executable is not None:
        admitted = _admit_executable(Path(executable))
        if admitted is not None:
            return admitted
        raise FileNotFoundError(f"{name} executable is not an absolute regular tool outside csk shims")
    path = os.environ.get("PATH", os.defpath) if search_path is None else search_path
    names = [name]
    if os.name == "nt":
        extensions = os.environ.get("PATHEXT", ".COM;.EXE;.BAT;.CMD").split(os.pathsep)
        if not any(name.lower().endswith(extension.lower()) for extension in extensions if extension):
            names = [name + extension for extension in extensions if extension]
    for entry in path.split(os.pathsep):
        directory = Path(entry)
        if not directory.is_absolute():
            continue
        for filename in names:
            admitted = _admit_executable(directory / filename)
            if admitted is not None:
                return admitted
    raise FileNotFoundError(f"{name} executable not found outside project and csk shims; install {name} and ensure it is on PATH")
