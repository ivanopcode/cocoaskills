"""Read-only, point-in-time kernel PATH and evaluator-identity observations.

Only the selected executable's first 1024 bytes and an opaque interpreter's
first 2 bytes are read. No command is executed.
Concurrent replacement and a same-user attacker who can write PATH directories
are outside this boundary; the check does not freeze filesystem identities.
"""

from __future__ import annotations

import ntpath
import os
import shutil
import stat
from collections.abc import Mapping
from pathlib import Path

from . import mcp_declarations as declarations
from .mcp_base_commands import EXEC_CAPABLE_BASE_COMMANDS, is_base_recipe

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


def _is_windows() -> bool:
    return os.name == "nt"


def _windows_final_path(path: str) -> str:
    # Windows realpath uses the kernel's final-path API. Resolve before lexical
    # normalization to retain symlink and parent-component semantics.
    return os.path.realpath(path, strict=True)


def _windows_path_key(path: str) -> str:
    path = path.replace("/", "\\")
    if path[:8].upper() == "\\\\?\\UNC\\":
        path = "\\\\" + path[8:]
    elif path.startswith("\\\\?\\"):
        path = path[4:]
    return ntpath.normcase(ntpath.normpath(path))


def _windows_root_key() -> str:
    root = os.environ.get("SystemRoot")
    if not root:
        raise _ObservationRefused("Windows SystemRoot is unset")
    try:
        return _windows_path_key(_windows_final_path(root)).rstrip("\\")
    except (OSError, ValueError) as exc:
        raise _ObservationRefused("Windows SystemRoot cannot be resolved") from exc


def _windows_base_candidate(candidate: str) -> bool:
    root_key = _windows_root_key()
    try:
        selected_key = _windows_path_key(_windows_final_path(candidate))
    except (OSError, ValueError) as exc:
        raise _ObservationRefused("base-system final path cannot be resolved") from exc
    return selected_key == root_key or selected_key.startswith(root_key + "\\")


def _base_system_inodes() -> set[_Identity]:
    identities: set[_Identity] = set()
    # Flat POSIX roots only; never traverse SystemRoot on Windows.
    for directory in _BASE_DIRECTORIES:
        try:
            with os.scandir(directory) as entries:
                for entry in entries:
                    try:
                        info = entry.stat()
                        if stat.S_ISREG(info.st_mode):
                            identities.add(_identity(info))
                    except OSError:
                        continue
        except FileNotFoundError:
            continue
        except OSError as exc:
            raise _ObservationRefused("base-system identities cannot be enumerated") from exc
    return identities


def _base_recipe_identity(command: str, directories: tuple[str, ...]) -> bool:
    selected = _selected_candidate(command, directories)
    if selected is None or _is_windows():
        return False
    for directory in _BASE_DIRECTORIES:
        try:
            info = os.stat(os.path.join(directory, command))
            if stat.S_ISREG(info.st_mode) and _identity(info) == _identity(selected[1]):
                return True
        except OSError:
            continue
    return False


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
    windows = _is_windows()
    # PATH admission uses libuv's native suffix priority (PATHEXT is ignored):
    # https://github.com/libuv/libuv/blob/v1.x/src/win/process.c
    # path_search_walk_ext: extended name first, then .com, then .exe.
    # Rust std differs: extensionless names search .exe only, extended names
    # are exact. This shared PATH policy uses .com before .exe; it does not
    # claim to reproduce Rust's distinct search directories/order.
    # https://github.com/rust-lang/rust/blob/master/library/std/src/sys/process/windows.rs
    # resolve_exe/search_paths. Cwd/application/system implicit searches are
    # outside this PATH observation; later agent environments may differ.
    suffixes = ([""] if ntpath.splitext(command)[1] else [".com", ".exe"]) if windows else [""]
    for directory in directories:
        for suffix in suffixes:
            candidate = os.path.join(directory, command + suffix)
            try:
                info = os.stat(candidate)
                if stat.S_ISREG(info.st_mode) and (windows or os.access(candidate, os.X_OK)):
                    return candidate, info
            except (OSError, ValueError):
                continue
    return None


def _native_header(head: bytes) -> bool:
    # Recognition only, not a full loader/architecture validity check. Universal
    # headers share Java's magic; permit only a conservative 1..16 arch count.
    if head[:4] in {b"\xfe\xed\xfa\xce", b"\xfe\xed\xfa\xcf", b"\xce\xfa\xed\xfe", b"\xcf\xfa\xed\xfe", b"\x7fELF"}:
        return True
    if head.startswith(b"MZ"):
        return True
    if len(head) < 8:
        return False
    if head[:4] == b"\xca\xfe\xba\xbe":
        count = int.from_bytes(head[4:8], "big")
    elif head[:4] == b"\xbe\xba\xfe\xca":
        count = int.from_bytes(head[4:8], "little")
    else:
        return False
    return 1 <= count <= 16


def _resolved_evaluator(command: str, *, path: str, cwd: Path, runner: bool = False) -> bool:
    directories = _path_directories(path, cwd)
    selected = _selected_candidate(command, directories)
    if selected is None:
        return False
    candidate, info = selected
    if (_windows_base_candidate(candidate) if _is_windows() else _identity(info) in _base_system_inodes()):
        return True
    identities = set() if runner else _evaluator_inodes(directories)
    if _identity(info) in identities:
        return True
    try:
        # Follow the original candidate path through the kernel, including its
        # symlinks and directory requirements. Opaque interpreter reads are separately limited to two bytes.
        descriptor = os.open(candidate, os.O_RDONLY | getattr(os, "O_NONBLOCK", 0))
        try:
            if not stat.S_ISREG(os.fstat(descriptor).st_mode):
                raise _ObservationRefused("selected executable is not a regular file")
            head = os.read(descriptor, 1024)
        finally:
            os.close(descriptor)
    except OSError as exc:
        raise _ObservationRefused("selected executable header cannot be observed") from exc
    if _is_windows():
        if not head.startswith(b"MZ"):
            raise _ObservationRefused("selected Windows executable header is not PE (MZ)")
        return False
    if not head.startswith(b"#!"):
        if not _native_header(head):
            raise _ObservationRefused("selected executable header is not a recognized native format or shebang")
        return False
    newline = head.find(b"\n")
    if newline < 0 or newline >= 256:
        raise _ObservationRefused("script header has no newline within 256 bytes")
    # Parse horizontal whitespace conservatively, without shell quoting.
    # In particular, CR is part of the interpreter path, not whitespace to strip.
    raw = head.split(b"\n", 1)[0][2:].lstrip(b" \t")
    interpreter_bytes = raw.split(b" ", 1)[0].split(b"\t", 1)[0]
    if b"#" in interpreter_bytes:
        raise _ObservationRefused("script interpreter word contains #")
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
    if _known_evaluator(interpreter) or _identity(interpreter_info) in identities:
        return not runner
    try:
        descriptor = os.open(interpreter_path, os.O_RDONLY | getattr(os, "O_NONBLOCK", 0))
        try:
            if not stat.S_ISREG(os.fstat(descriptor).st_mode):
                raise _ObservationRefused("script interpreter is not a regular file")
            prefix = os.read(descriptor, 2)
        finally:
            os.close(descriptor)
    except OSError as exc:
        raise _ObservationRefused("script interpreter header cannot be observed") from exc
    if prefix == b"#!":
        raise _ObservationRefused("nested script interpreter is refused")
    return False


def validate_windows_batch_commands(
    parsed: Mapping[str, declarations.McpDeclaration], *, windows: bool
) -> None:
    """Preserve the native-launcher error before observing host executable headers."""
    if not windows:
        return
    for name, declaration in parsed.items():
        command = declaration.server.command
        if not declaration.agents or declaration.server.transport != "stdio" or command is None:
            continue
        resolved = shutil.which(command)
        if (
            command.lower() in ("npx", "npm", "pnpm", "yarn")
            or command.lower().endswith((".cmd", ".bat"))
            or (resolved and resolved.lower().endswith((".cmd", ".bat")))
        ):
            raise declarations.McpDeclarationError(
                "mcp_windows_batch_unsupported", f"mcp_servers.{name}.server.command",
                f"{name} uses a batch launcher",
            )


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
        try:
            if _is_windows():
                _windows_root_key()
        except _ObservationRefused as exc:
            raise declarations.McpDeclarationError(
                "mcp_command_invalid", f"mcp_servers.{name}.server.command",
                f"command {command!r} is unresolvable: {exc}",
            ) from exc
        if is_base_recipe(command, declaration.server.args):
            if not _base_recipe_identity(command, _path_directories(path, cwd)):
                raise declarations.McpDeclarationError(
                    "mcp_command_invalid", f"mcp_servers.{name}.server.command",
                    "exact Apple MCP recipe requires the named base-file identity",
                )
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
