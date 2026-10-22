"""Read-only, point-in-time kernel PATH and evaluator-identity observations.

Only the selected executable's first 256 bytes are read. No command is executed.
Concurrent replacement and a same-user attacker who can write PATH directories
are outside this boundary; the check does not freeze filesystem identities.
"""

from __future__ import annotations

import os
import shlex
import stat
from collections.abc import Mapping
from pathlib import Path

from . import mcp_declarations as declarations

_SYSTEM_DIRECTORIES = ("/usr/bin", "/bin", "/usr/sbin", "/sbin", "/usr/local/bin", "/opt/homebrew/bin")
_Identity = tuple[int, int]


def _known_evaluator(name: str) -> bool:
    return declarations._command_kind(declarations._normalize_command(name)) is not None


def _path_directories(path: str, cwd: Path) -> tuple[str, ...]:
    # Keep every separator, dot and parent component for the kernel. Empty PATH
    # entries and relative entries are relative to the agent's project cwd.
    return tuple(os.path.join(os.fspath(cwd), entry) for entry in path.split(os.pathsep))


def _identity(info: os.stat_result) -> _Identity:
    return info.st_dev, info.st_ino


def _evaluator_inodes(directories: tuple[str, ...]) -> set[_Identity]:
    identities: set[_Identity] = set()
    for directory in dict.fromkeys((*directories, *_SYSTEM_DIRECTORIES)):
        try:
            if not stat.S_ISDIR(os.stat(directory).st_mode):
                continue
            try:
                with os.scandir(directory) as entries:
                    # Actual spellings include versioned names, executable
                    # suffixes and case aliases recognized by the pure table.
                    names = {entry.name for entry in entries if _known_evaluator(entry.name)}
            except OSError:
                # Searchable directories need not permit enumeration.
                names = {name + suffix for name in declarations._COMMAND_KINDS
                         for suffix in ("", *declarations._EXECUTABLE_SUFFIXES)}
            for name in names:
                try:
                    info = os.stat(os.path.join(directory, name))
                except OSError:
                    continue
                if stat.S_ISREG(info.st_mode):
                    identities.add(_identity(info))
        except (OSError, ValueError):
            continue
    return identities


def _selected_candidate(command: str, directories: tuple[str, ...]) -> tuple[str, os.stat_result] | None:
    suffixes = [""]
    if os.name == "nt":
        suffixes += [s for s in os.environ.get("PATHEXT", ".COM;.EXE;.BAT;.CMD").split(";") if s]
    for directory in directories:
        for suffix in suffixes:
            candidate = os.path.join(directory, command + suffix)
            try:
                info = os.stat(candidate)
                if stat.S_ISREG(info.st_mode) and os.access(candidate, os.X_OK):
                    return candidate, info
            except (OSError, ValueError):
                continue
    return None


def _resolved_evaluator(command: str, *, path: str, cwd: Path) -> bool:
    directories = _path_directories(path, cwd)
    selected = _selected_candidate(command, directories)
    if selected is None:
        return False
    candidate, info = selected
    identities = _evaluator_inodes(directories)
    if _identity(info) in identities:
        return True
    try:
        # Follow the original candidate path through the kernel, including its
        # symlinks and directory requirements. Never open an interpreter body.
        descriptor = os.open(candidate, os.O_RDONLY | getattr(os, "O_NONBLOCK", 0))
        try:
            if not stat.S_ISREG(os.fstat(descriptor).st_mode):
                return False
            head = os.read(descriptor, 256)
        finally:
            os.close(descriptor)
    except OSError:
        # An unreadable selected file must not substitute a later executable.
        return False
    if not head.startswith(b"#!"):
        return False
    try:
        words = shlex.split(head.split(b"\n", 1)[0][2:].decode("utf-8", errors="replace").strip())
    except ValueError:
        return False
    interpreters = [(words[0], False)] if words else []
    if words and declarations._normalize_command(words[0]) == "env" and len(words) > 1:
        interpreters.append((words[1], True))
    for interpreter, search_path in interpreters:
        if _known_evaluator(interpreter):
            return True
        try:
            if search_path:
                resolved = _selected_candidate(interpreter, directories)
                if resolved is not None and _identity(resolved[1]) in identities:
                    return True
            elif _identity(os.stat(os.path.join(os.fspath(cwd), interpreter))) in identities:
                return True
        except (OSError, ValueError):
            continue
    return False


def validate_host_commands(
    parsed: Mapping[str, declarations.McpDeclaration], *, cwd: Path
) -> None:
    """Compose kernel identity admission after the portable grammar passes.

    Exact validated runner recipes retain their expected interpreter substrate.
    """
    path = os.environ.get("PATH", os.defpath)
    for name, declaration in parsed.items():
        command = declaration.server.command
        if declaration.server.transport != "stdio" or command is None:
            continue
        if declarations._normalize_command(command) in {"npx", "uvx", "uv"}:
            continue
        if _resolved_evaluator(command, path=path, cwd=cwd):
            raise declarations.McpDeclarationError(
                "mcp_command_invalid", f"mcp_servers.{name}.server.command",
                "resolved command uses a recognized evaluator or wrapper",
            )
