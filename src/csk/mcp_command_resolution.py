"""Best-effort, read-only host PATH checks, separate from the pure grammar.

No executable is started. Only candidate files inside PATH directories are read;
links leaving those directories can be classified by name but are not opened.
This is a point-in-time host observation, not a promise about concurrent directory
replacement or another agent's later PATH.
"""

from __future__ import annotations

import os
import shlex
import stat
from collections.abc import Mapping
from pathlib import Path

from . import mcp_declarations as declarations


def _known_evaluator(name: str) -> bool:
    # An opaque alias of a runner cannot inherit the direct recipe's pin check.
    return declarations._command_kind(declarations._normalize_command(name)) is not None


def _physical_candidate(
    candidate: Path, directories: tuple[Path, ...]
) -> tuple[Path, os.stat_result | None] | None:
    """Walk links before parent components, stopping at the PATH read boundary.

    Canonical PATH roots establish their physical ancestors. Within their trees
    only metadata is inspected here; the caller restricts body reads to roots.
    An outside target is returned with no stat result for name-only classification.
    """
    current = Path(candidate.anchor)
    pending = list(candidate.parts[1:])
    seen: set[tuple[Path, tuple[str, ...]]] = set()
    hops = 0
    while pending:
        part = pending.pop(0)
        if part == "..":
            current = current.parent
            continue
        child = current / part
        if not any(current.is_relative_to(root) for root in directories):
            if any(root.is_relative_to(child) for root in directories):
                # These ancestors already resolved when the PATH roots did.
                current = child
                continue
            return child.joinpath(*pending), None
        info = child.lstat()
        if stat.S_ISLNK(info.st_mode):
            state = child, tuple(pending)
            if state in seen or hops >= 40:
                return None
            seen.add(state)
            hops += 1
            target = Path(os.readlink(child))
            if target.is_absolute():
                current = Path(target.anchor)
                pending = list(target.parts[1:]) + pending
            else:
                pending = list(target.parts) + pending
        else:
            if pending and not stat.S_ISDIR(info.st_mode):
                return None
            current = child
    if current.parent not in directories:
        return current, None
    return current, current.lstat()


def _resolved_evaluator(command: str, *, path: str, cwd: Path) -> bool:
    roots = []
    for entry in path.split(os.pathsep):
        try:
            roots.append((cwd / entry).resolve(strict=True))
        except (OSError, RuntimeError, ValueError):
            continue
    directories = tuple(dict.fromkeys(roots))
    suffixes = [""]
    if os.name == "nt":
        suffixes += [s for s in os.environ.get("PATHEXT", ".COM;.EXE;.BAT;.CMD").split(";") if s]
    for directory in directories:
        for suffix in suffixes:
            try:
                resolved = _physical_candidate(directory / (command + suffix), directories)
                if resolved is None:
                    continue
                candidate, info = resolved
                # Never open a script outside the canonical PATH directories.
                if info is None:
                    return _known_evaluator(candidate.name)
                if not stat.S_ISREG(info.st_mode) or (os.name != "nt" and not info.st_mode & 0o111):
                    continue
                if _known_evaluator(candidate.name):
                    return True
                # O_NOFOLLOW closes the final-component link race on POSIX;
                # O_NONBLOCK prevents a substituted pipe from blocking reads.
                try:
                    descriptor = os.open(candidate, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0))
                except OSError:
                    # An unreadable selected executable does not make the
                    # next PATH entry the executable that would be chosen.
                    return False
                try:
                    if not stat.S_ISREG(os.fstat(descriptor).st_mode):
                        return False
                    head = os.read(descriptor, 4096)
                finally:
                    os.close(descriptor)
                if head.startswith(b"#!"):
                    line = head.split(b"\n", 1)[0][2:].decode("utf-8", errors="replace").strip()
                    try:
                        words = shlex.split(line)
                    except ValueError:
                        words = []
                    if words and _known_evaluator(words[0]):
                        return True
                # The first executable is what PATH selects; later files
                # must not change the verdict for an earlier opaque binary.
                return False
            except (OSError, RuntimeError, ValueError):
                continue
    return False


def validate_host_commands(
    parsed: Mapping[str, declarations.McpDeclaration], *, cwd: Path
) -> None:
    """Refuse resolved evaluator aliases after the portable grammar passes.

    Supported runner recipes have already passed their exact-pin grammar; their
    own interpreter is expected and does not turn the recipe into an opaque one.
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
