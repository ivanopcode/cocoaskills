from __future__ import annotations

import os
import shlex
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


def install_runtime_roots(
    *,
    csk_home: Path,
    skill_name: str,
    commit: str,
    snapshot: Path,
    runtime_roots: tuple[str, ...],
) -> Path:
    runtime_dir = csk_home / "runtime" / skill_name / commit
    if runtime_dir.exists():
        return runtime_dir

    runtime_dir.parent.mkdir(parents=True, exist_ok=True)
    tmp = runtime_dir.parent / f".{commit}.tmp-{os.getpid()}"
    if tmp.exists():
        shutil.rmtree(tmp)
    try:
        tmp.mkdir(parents=True)
        for root in runtime_roots:
            src = (snapshot / root).resolve()
            try:
                src.relative_to(snapshot.resolve())
            except ValueError as exc:
                raise ShimError(f"Runtime root escapes skill snapshot: {root}") from exc
            if not src.is_dir():
                raise ShimError(f"Runtime root not found: {root}")
            dst = tmp / root
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copytree(src, dst, copy_function=shutil.copy2)
        if runtime_dir.exists():
            shutil.rmtree(tmp)
            return runtime_dir
        tmp.rename(runtime_dir)
    except Exception:
        if tmp.exists():
            shutil.rmtree(tmp)
        raise
    return runtime_dir


def runtime_root_command_path(
    *,
    csk_home: Path,
    skill_name: str,
    commit: str,
    command: CommandSpec,
    platform_name: str | None = None,
) -> Path:
    platform_name = platform_name or ("windows" if os.name == "nt" else "unix")
    rel = command.win_path if platform_name == "windows" else command.unix_path
    if not rel:
        raise ShimError(f"Command {command.name!r} has no path for {platform_name}")
    runtime_path = csk_home / "runtime" / skill_name / commit / rel
    if not runtime_path.is_file():
        raise ShimError(f"Command {command.name!r} runtime file not found: {rel}")
    if platform_name != "windows":
        mode = runtime_path.stat().st_mode
        runtime_path.chmod(mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    return runtime_path


def write_project_shim(
    project_root: Path,
    command_name: str,
    runtime_path: Path,
    *,
    platform_name: str | None = None,
    path_entries: tuple[Path, ...] = (),
) -> Path:
    return write_bin_shim(
        project_root / ".agents" / "bin",
        command_name,
        runtime_path,
        platform_name=platform_name,
        path_entries=path_entries,
    )


def write_global_shim(
    csk_home: Path,
    command_name: str,
    runtime_path: Path,
    *,
    platform_name: str | None = None,
    path_entries: tuple[Path, ...] = (),
) -> Path:
    return write_bin_shim(
        csk_home / "global" / "bin",
        command_name,
        runtime_path,
        platform_name=platform_name,
        path_entries=path_entries,
    )


def write_bin_shim(
    bin_dir: Path,
    command_name: str,
    runtime_path: Path,
    *,
    platform_name: str | None = None,
    path_entries: tuple[Path, ...] = (),
) -> Path:
    platform_name = platform_name or ("windows" if os.name == "nt" else "unix")
    bin_dir.mkdir(parents=True, exist_ok=True)
    if platform_name == "windows":
        shim = bin_dir / f"{command_name}.cmd"
        if path_entries:
            prefix = ";".join(_escape_cmd_value(str(path)) for path in path_entries)
            runtime_value = _escape_cmd_value(str(runtime_path))
            shim.write_text(
                "@echo off\r\n"
                "setlocal DisableDelayedExpansion\r\n"
                f'set "PATH={prefix};%PATH%"\r\n'
                f'call "{runtime_value}" %*\r\n'
                "exit /b %ERRORLEVEL%\r\n",
                encoding="utf-8",
            )
        else:
            shim.write_text(
                "@echo off\r\n"
                f"\"{runtime_path}\" %*\r\n",
                encoding="utf-8",
            )
        return shim

    shim = bin_dir / command_name
    if shim.exists() or shim.is_symlink():
        shim.unlink()
    if path_entries:
        prefix = ":".join(str(path) for path in path_entries)
        shim.write_text(
            "#!/bin/sh\n"
            'if [ -n "${PATH:-}" ]; then\n'
            f"  PATH={shlex.quote(prefix)}:\"$PATH\"\n"
            "else\n"
            f"  PATH={shlex.quote(prefix)}\n"
            "fi\n"
            "export PATH\n"
            f"exec {shlex.quote(str(runtime_path))} \"$@\"\n",
            encoding="utf-8",
        )
        shim.chmod(shim.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
        return shim
    target = os.path.relpath(runtime_path, shim.parent)
    shim.symlink_to(target)
    return shim


def _escape_cmd_value(value: str) -> str:
    return value.replace("%", "%%")


def remove_stale_shims(project_root: Path, expected_commands: set[str], *, platform_name: str | None = None) -> None:
    remove_stale_shims_in(project_root / ".agents" / "bin", expected_commands, platform_name=platform_name)


def remove_stale_global_shims(csk_home: Path, expected_commands: set[str], *, platform_name: str | None = None) -> None:
    remove_stale_shims_in(csk_home / "global" / "bin", expected_commands, platform_name=platform_name)


def remove_stale_shims_in(bin_dir: Path, expected_commands: set[str], *, platform_name: str | None = None) -> None:
    platform_name = platform_name or ("windows" if os.name == "nt" else "unix")
    if not bin_dir.exists():
        return
    for child in bin_dir.iterdir():
        if not child.is_file() and not child.is_symlink():
            continue
        command = child.stem if platform_name == "windows" and child.suffix.lower() == ".cmd" else child.name
        if command not in expected_commands:
            child.unlink()
