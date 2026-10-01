"""Resolve manager tools without executing project or csk command shims."""

from __future__ import annotations

import os
import stat
from functools import lru_cache
from pathlib import Path


def _is_shim_directory(directory: Path) -> bool:
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
        (parent / ".csk-managed.json").exists()
        for parent in (directory, *directory.parents)
    )


def _admit_executable(candidate: Path) -> str | None:
    if not candidate.is_absolute():
        return None
    try:
        if _is_shim_directory(candidate.parent):
            return None
        resolved = candidate.resolve(strict=True)
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
