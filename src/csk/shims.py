from __future__ import annotations

import os
import shutil
import stat
from pathlib import Path

from .skillspec import CommandSpec


class ShimError(Exception):
    pass


def install_runtime_command(
    *,
    csk_home: Path,
    skill_name: str,
    commit: str,
    snapshot: Path,
    command: CommandSpec,
    platform_name: str | None = None,
) -> Path:
    platform_name = platform_name or ("windows" if os.name == "nt" else "unix")
    rel = command.win_path if platform_name == "windows" else command.unix_path
    if not rel:
        raise ShimError(f"Command {command.name!r} has no path for {platform_name}")
    src = (snapshot / rel).resolve()
    try:
        src.relative_to(snapshot.resolve())
    except ValueError as exc:
        raise ShimError(f"Command {command.name!r} path escapes skill snapshot") from exc
    if not src.is_file():
        raise ShimError(f"Command {command.name!r} source file not found: {rel}")
    suffix = ".cmd" if platform_name == "windows" and not command.name.endswith(".cmd") else ""
    runtime_path = csk_home / "runtime" / skill_name / commit / "bin" / f"{command.name}{suffix}"
    runtime_path.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(src, runtime_path)
    if platform_name != "windows":
        mode = runtime_path.stat().st_mode
        runtime_path.chmod(mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    return runtime_path


def write_project_shim(project_root: Path, command_name: str, runtime_path: Path, *, platform_name: str | None = None) -> Path:
    platform_name = platform_name or ("windows" if os.name == "nt" else "unix")
    bin_dir = project_root / ".agents" / "bin"
    bin_dir.mkdir(parents=True, exist_ok=True)
    if platform_name == "windows":
        shim = bin_dir / f"{command_name}.cmd"
        shim.write_text(
            "@echo off\r\n"
            f"\"{runtime_path}\" %*\r\n",
            encoding="utf-8",
        )
        return shim

    shim = bin_dir / command_name
    if shim.exists() or shim.is_symlink():
        shim.unlink()
    target = os.path.relpath(runtime_path, shim.parent)
    shim.symlink_to(target)
    return shim


def remove_stale_shims(project_root: Path, expected_commands: set[str], *, platform_name: str | None = None) -> None:
    platform_name = platform_name or ("windows" if os.name == "nt" else "unix")
    bin_dir = project_root / ".agents" / "bin"
    if not bin_dir.exists():
        return
    for child in bin_dir.iterdir():
        if not child.is_file() and not child.is_symlink():
            continue
        command = child.stem if platform_name == "windows" and child.suffix.lower() == ".cmd" else child.name
        if command not in expected_commands:
            child.unlink()

