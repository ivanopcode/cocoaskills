"""Read-only, point-in-time kernel PATH and evaluator-identity observations.

Only the selected executable's first 1024 bytes are read. No command is executed.
Concurrent replacement and a same-user attacker who can write PATH directories
are outside this boundary; the check does not freeze filesystem identities.
"""

from __future__ import annotations

import os
import stat
from collections.abc import Mapping
from pathlib import Path

from . import mcp_declarations as declarations
from .mcp_base_commands import EXEC_CAPABLE_BASE_COMMANDS, is_mcpbridge

_BASE_DIRECTORIES = ("/usr/bin", "/bin", "/usr/sbin", "/sbin")
_SYSTEM_DIRECTORIES = ("/usr/bin", "/bin", "/usr/sbin", "/sbin", "/usr/local/bin", "/opt/homebrew/bin")
_Identity = tuple[int, int]


class _ObservationRefused(Exception):
    """The selected script or identity inventory cannot be safely observed."""


def _known_evaluator(name: str) -> bool:
    normalized = declarations._normalize_command(name)
    return declarations._command_kind(normalized) is not None or normalized in EXEC_CAPABLE_BASE_COMMANDS


def _path_directories(path: str, cwd: Path) -> tuple[str, ...]:
    # Keep every separator, dot and parent component for the kernel. Empty PATH
    # entries and relative entries are relative to the agent's project cwd.
    return tuple(os.path.join(os.fspath(cwd), entry) for entry in path.split(os.pathsep))


def _identity(info: os.stat_result) -> _Identity:
    return info.st_dev, info.st_ino


def _base_system_roots() -> tuple[str, ...]:
    if os.name == "nt":
        root = os.environ.get("SystemRoot") or os.environ.get("WINDIR")
        return (root,) if root else ()
    return _BASE_DIRECTORIES


def _base_system_inodes() -> set[_Identity]:
    identities: set[_Identity] = set()
    visited: set[_Identity] = set()
    pending = list(_base_system_roots())
    while pending:
        directory = pending.pop()
        try:
            info = os.stat(directory)
            identity = _identity(info)
            if not stat.S_ISDIR(info.st_mode) or identity in visited:
                continue
            visited.add(identity)
            with os.scandir(directory) as entries:
                for entry in entries:
                    try:
                        info = entry.stat()
                        if stat.S_ISREG(info.st_mode):
                            identities.add(_identity(info))
                        elif os.name == "nt" and entry.is_dir(follow_symlinks=False):
                            pending.append(entry.path)
                    except OSError:
                        continue
        except FileNotFoundError:
            continue
        except OSError as exc:
            raise _ObservationRefused("base-system identities cannot be enumerated") from exc
    return identities


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
            except OSError as exc:
                # Guessing finite lowercase spellings loses versioned and
                # mixed-case names. Refuse an incomplete identity observation.
                raise _ObservationRefused("PATH identities cannot be enumerated") from exc
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


def _resolved_evaluator(command: str, *, path: str, cwd: Path, runner: bool = False) -> bool:
    directories = _path_directories(path, cwd)
    selected = _selected_candidate(command, directories)
    if selected is None:
        return False
    candidate, info = selected
    if _identity(info) in _base_system_inodes():
        return True
    if runner:
        return False
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
            head = os.read(descriptor, 1024)
        finally:
            os.close(descriptor)
    except OSError:
        # An unreadable selected file must not substitute a later executable.
        return False
    if not head.startswith(b"#!"):
        return False
    if b"\n" not in head:
        raise _ObservationRefused("script header has no newline within 1024 bytes")
    # Kernels split at horizontal whitespace; shell quoting does not apply.
    # In particular, CR is part of the interpreter path, not whitespace to strip.
    raw = head.split(b"\n", 1)[0][2:].lstrip(b" \t")
    interpreter_bytes = raw.split(b" ", 1)[0].split(b"\t", 1)[0]
    try:
        interpreter = interpreter_bytes.decode("utf-8", errors="strict")
    except UnicodeDecodeError as exc:
        raise _ObservationRefused("script interpreter path is not valid UTF-8") from exc
    if not interpreter:
        raise _ObservationRefused("script interpreter path is empty")
    interpreter_path = os.path.join(os.fspath(cwd), interpreter)
    try:
        interpreter_info = os.stat(interpreter_path)
        executable = stat.S_ISREG(interpreter_info.st_mode) and os.access(interpreter_path, os.X_OK)
    except (OSError, ValueError):
        executable = False
    if not executable:
        raise _ObservationRefused("script interpreter is not an executable regular file")
    # env is already a recognized wrapper; no unreachable second-stage env X
    # search and no interpreter-body or nested-interpreter traversal is needed.
    return _known_evaluator(interpreter) or _identity(interpreter_info) in identities


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
        if is_mcpbridge(command, declaration.server.args):
            continue
        runner = declarations._normalize_command(command) in {"npx", "uvx", "uv"}
        try:
            refused = _resolved_evaluator(command, path=path, cwd=cwd, runner=runner)
        except _ObservationRefused as exc:
            raise declarations.McpDeclarationError(
                "mcp_command_invalid", f"mcp_servers.{name}.server.command",
                f"command {command!r} is unresolvable: {exc}",
            ) from exc
        if refused:
            raise declarations.McpDeclarationError(
                "mcp_command_invalid", f"mcp_servers.{name}.server.command",
                "resolved command is a base-system program or recognized evaluator/wrapper",
            )
