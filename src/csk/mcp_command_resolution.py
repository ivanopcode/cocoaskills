"""Best-effort, read-only host PATH checks, separate from the pure grammar.

No executable is started. Only candidate files inside PATH directories are read;
links leaving those directories can be classified by name but are not opened.
This is a host observation, not a promise about another agent's later PATH.
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


def _resolved_evaluator(command: str, *, path: str, cwd: Path) -> bool:
    roots = []
    for entry in path.split(os.pathsep):
        try:
            roots.append(Path(os.path.abspath(cwd / entry)).resolve())
        except (OSError, RuntimeError, ValueError):
            continue
    directories = tuple(dict.fromkeys(roots))
    suffixes = [""]
    if os.name == "nt":
        suffixes += [s for s in os.environ.get("PATHEXT", ".COM;.EXE;.BAT;.CMD").split(";") if s]
    for directory in directories:
        for suffix in suffixes:
            candidate = directory / (command + suffix)
            seen: set[Path] = set()
            for _ in range(40):
                # Never open a script or inspect a link outside PATH directories.
                if candidate.parent not in directories:
                    return _known_evaluator(candidate.name)
                if candidate in seen:
                    break
                seen.add(candidate)
                try:
                    info = candidate.lstat()
                    if stat.S_ISLNK(info.st_mode):
                        target = Path(os.readlink(candidate))
                        candidate = Path(os.path.abspath(candidate.parent / target))
                        continue
                    if not stat.S_ISREG(info.st_mode) or (os.name != "nt" and not info.st_mode & 0o111):
                        break
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
                    break
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
