"""Command materialization for script runtimes and compiled build artifacts.

Two activation paths share one launcher writer. A script command is copied into
a commit-keyed runtime tree below the manager home, and an existing tree is
reused only when every required active path is a contained regular file.
A compiled command is never copied: it is activated directly against the
immutable protected build-cache artifact selected by validated marker and
receipt data. Launchers are manager-generated, self-contained, and independent
of shell profiles.
"""

from __future__ import annotations

import os
import shlex
import shutil
import stat
import sys
from collections.abc import Iterable, Sequence
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

from .builds.cache import CacheEntryStatus, CacheInspection
from .builds.metadata import GO_V1_DRIVER, derived_artifact_path
from .identifiers import IDENTIFIER_RULE, is_valid_identifier, is_valid_portable_path
from .install_marker import InstallMarkerBuild
from .skillspec import CommandSpec

UNIX_PLATFORM = "unix"
WINDOWS_PLATFORM = "windows"
SUPPORTED_PLATFORMS = frozenset({UNIX_PLATFORM, WINDOWS_PLATFORM})

RUNTIME_DIRECTORY = "runtime"
_CMD_SUFFIX = ".cmd"
_WINDOWS_GOOS = "windows"
_SHA256_PREFIX = "sha256:"
_SHA256_DIGITS = frozenset("0123456789abcdef")

# A launcher embeds its target in one quoted cmd or sh word. These scalars end
# that word or the line itself, so a crafted path could append a command.
_FORBIDDEN_LAUNCHER_SCALARS = ('"', "\r", "\n", "\x00")

# Owner execute alone is enough to launch; the cache publishes artifacts 0o500.
_EXECUTE_BITS = stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH


class ShimError(Exception):
    pass


@dataclass(frozen=True)
class BuildCommandActivation:
    """One compiled command bound to a verified immutable cache artifact.

    Only :func:`select_build_activation` should construct this type: it exists
    so a launcher can never be published from an unvalidated physical path.
    """

    command_name: str
    artifact_path: Path
    cache_key: str
    receipt_sha256: str
    artifact_sha256: str
    artifact_size: int
    driver: str = GO_V1_DRIVER

    def __post_init__(self) -> None:
        _require_command_name(self.command_name)
        if self.driver != GO_V1_DRIVER:
            raise ShimError(f"Build activation driver must be {GO_V1_DRIVER!r}, got {self.driver!r}")
        object.__setattr__(self, "artifact_path", Path(self.artifact_path))
        _require_launcher_target(self.artifact_path, subject=f"Build command {self.command_name!r} artifact")
        _require_sha256(self.cache_key, "Build activation cache_key")
        _require_sha256(self.receipt_sha256, "Build activation receipt_sha256")
        _require_sha256(self.artifact_sha256, "Build activation artifact_sha256")
        if (
            not isinstance(self.artifact_size, int)
            or isinstance(self.artifact_size, bool)
            or self.artifact_size < 0
        ):
            raise ShimError(f"Build activation artifact_size must be a non-negative integer, got {self.artifact_size!r}")


def install_runtime_command(
    *,
    csk_home: Path,
    skill_name: str,
    commit: str,
    snapshot: Path,
    command: CommandSpec,
    platform_name: str | None = None,
) -> Path:
    platform = _resolve_platform(platform_name)
    relative = _command_relative_path(command, platform)
    src = (snapshot / relative).resolve()
    try:
        src.relative_to(snapshot.resolve())
    except ValueError as exc:
        raise ShimError(f"Command {command.name!r} path escapes skill snapshot") from exc
    if not src.is_file():
        raise ShimError(f"Command {command.name!r} source file not found: {relative}")
    runtime_path = (
        runtime_directory(csk_home, skill_name, commit)
        / "bin"
        / _runtime_command_filename(command.name, platform)
    )
    runtime_path.parent.mkdir(parents=True, exist_ok=True)
    # Stage and rename so a stale symlink at the destination can never redirect
    # the copy outside the commit-keyed runtime tree.
    staged = _unique_path(runtime_path.parent, f".{runtime_path.name}.tmp-{os.getpid()}")
    try:
        shutil.copy2(src, staged)
        if platform != WINDOWS_PLATFORM:
            _grant_execute(staged)
        os.replace(staged, runtime_path)
    except Exception:
        with suppress(OSError):
            _discard(staged)
        raise
    return runtime_path


def install_runtime_roots(
    *,
    csk_home: Path,
    skill_name: str,
    commit: str,
    snapshot: Path,
    runtime_roots: tuple[str, ...],
    required_commands: Sequence[CommandSpec] = (),
    platform_name: str | None = None,
) -> Path:
    """Materialize a commit-keyed runtime tree, replacing incomplete state.

    A commit key identifies bytes, not completeness: an interrupted install, a
    partial removal, or a replaced entry can leave a directory that exists but
    cannot serve a required command. Reuse therefore verifies every declared
    root and every required active command path before trusting the tree.
    """

    platform = _resolve_platform(platform_name)
    for root in runtime_roots:
        _require_portable_relative(root, f"Runtime root {root!r}")
    required_paths = required_runtime_paths(required_commands, platform_name=platform)
    runtime_dir = runtime_directory(csk_home, skill_name, commit)
    if runtime_state_is_complete(runtime_dir, runtime_roots=runtime_roots, required_paths=required_paths):
        return runtime_dir

    runtime_dir.parent.mkdir(parents=True, exist_ok=True)
    staged = runtime_dir.parent / f".{commit}.tmp-{os.getpid()}"
    if staged.exists() or staged.is_symlink():
        _discard(staged)
    try:
        staged.mkdir(parents=True)
        for root in runtime_roots:
            src = (snapshot / root).resolve()
            try:
                src.relative_to(snapshot.resolve())
            except ValueError as exc:
                raise ShimError(f"Runtime root escapes skill snapshot: {root}") from exc
            if not src.is_dir():
                raise ShimError(f"Runtime root not found: {root}")
            dst = staged / root
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copytree(src, dst, copy_function=shutil.copy2)
        _publish_runtime_directory(
            runtime_dir,
            staged,
            runtime_roots=runtime_roots,
            required_paths=required_paths,
            commit=commit,
        )
    except Exception:
        with suppress(OSError):
            _discard(staged)
        raise
    return runtime_dir


def required_runtime_paths(
    commands: Iterable[CommandSpec],
    *,
    platform_name: str | None = None,
) -> tuple[str, ...]:
    """Return the runtime-relative paths a script activation must be able to run."""

    platform = _resolve_platform(platform_name)
    paths: list[str] = []
    for command in commands:
        if command.type != "script":
            continue
        relative = _command_relative_path(command, platform)
        if relative not in paths:
            paths.append(relative)
    return tuple(paths)


def runtime_state_is_complete(
    runtime_dir: Path,
    *,
    runtime_roots: tuple[str, ...] = (),
    required_paths: tuple[str, ...] = (),
) -> bool:
    """Return whether an existing runtime tree can serve every required path."""

    if not _is_real_directory(runtime_dir):
        return False
    for root in runtime_roots:
        if _contained_entry(runtime_dir, root, directory=True) is None:
            return False
    for relative in required_paths:
        if _contained_entry(runtime_dir, relative, directory=False) is None:
            return False
    return True


def runtime_directory(csk_home: Path, skill_name: str, commit: str) -> Path:
    """Return the commit-keyed runtime directory of one skill."""

    if not is_valid_identifier(skill_name):
        raise ShimError(f"Skill name {skill_name!r} {IDENTIFIER_RULE}")
    if not is_valid_identifier(commit):
        raise ShimError(f"Commit {commit!r} {IDENTIFIER_RULE}")
    return csk_home / RUNTIME_DIRECTORY / skill_name / commit


def runtime_root_command_path(
    *,
    csk_home: Path,
    skill_name: str,
    commit: str,
    command: CommandSpec,
    platform_name: str | None = None,
) -> Path:
    platform = _resolve_platform(platform_name)
    relative = _command_relative_path(command, platform)
    runtime_dir = runtime_directory(csk_home, skill_name, commit)
    runtime_path = (
        _contained_entry(runtime_dir, relative, directory=False)
        if _is_real_directory(runtime_dir)
        else None
    )
    if runtime_path is None:
        raise ShimError(f"Command {command.name!r} runtime file not found: {relative}")
    if platform != WINDOWS_PLATFORM:
        _grant_execute(runtime_path)
    return runtime_path


def select_build_activation(
    *,
    csk_home: Path,
    command: CommandSpec,
    marker_build: InstallMarkerBuild,
    inspection: CacheInspection,
    platform_name: str | None = None,
) -> BuildCommandActivation:
    """Bind one compiled command to its protected cache artifact, or fail.

    Every identity the marker records must agree with the receipt the protected
    cache just re-read, and the physical artifact must still be the contained
    regular file that receipt describes. Nothing here executes the artifact.
    """

    platform = _resolve_platform(platform_name)
    _require_command_name(command.name)
    if command.type != "build":
        raise ShimError(f"Command {command.name!r} is not a build command")
    if command.driver != GO_V1_DRIVER:
        raise ShimError(f"Command {command.name!r} driver must be {GO_V1_DRIVER!r}, got {command.driver!r}")
    if marker_build.driver != GO_V1_DRIVER:
        raise ShimError(
            f"Command {command.name!r} marker driver must be {GO_V1_DRIVER!r}, got {marker_build.driver!r}"
        )
    if inspection.status is not CacheEntryStatus.HIT:
        raise ShimError(
            f"Command {command.name!r} has no protected cache hit: "
            f"{inspection.status.value} ({inspection.reason})"
        )
    receipt = inspection.receipt
    artifact_path = inspection.artifact_path
    receipt_hash = inspection.receipt_sha256
    if receipt is None or receipt_hash is None or artifact_path is None:
        raise ShimError(f"Command {command.name!r} cache hit is missing receipt or artifact state")
    if receipt.input.command != command.name:
        raise ShimError(
            f"Command {command.name!r} receipt was built for command {receipt.input.command!r}"
        )
    goos = receipt.input.target.goos
    if (goos == _WINDOWS_GOOS) != (platform == WINDOWS_PLATFORM):
        raise ShimError(
            f"Command {command.name!r} receipt target {goos!r} does not match {platform} activation"
        )
    expected_relative = derived_artifact_path(command.name, goos=goos)
    if receipt.artifact.path != expected_relative:
        raise ShimError(
            f"Command {command.name!r} receipt artifact path {receipt.artifact.path!r} "
            f"is not the derived path {expected_relative!r}"
        )
    if marker_build.artifact_path != expected_relative:
        raise ShimError(
            f"Command {command.name!r} marker artifact path {marker_build.artifact_path!r} "
            f"is not the derived path {expected_relative!r}"
        )
    if marker_build.cache_key != receipt.cache_key:
        raise ShimError(
            f"Command {command.name!r} marker cache key {marker_build.cache_key} "
            f"does not match receipt cache key {receipt.cache_key}"
        )
    if marker_build.receipt_sha256 != receipt_hash:
        raise ShimError(
            f"Command {command.name!r} marker receipt hash {marker_build.receipt_sha256} "
            f"does not match the inspected receipt hash {receipt_hash}"
        )
    if marker_build.artifact_sha256 != receipt.artifact.sha256:
        raise ShimError(
            f"Command {command.name!r} marker artifact hash {marker_build.artifact_sha256} "
            f"does not match receipt artifact hash {receipt.artifact.sha256}"
        )
    _require_cache_artifact(
        csk_home,
        artifact_path,
        command_name=command.name,
        expected_relative=expected_relative,
        expected_size=receipt.artifact.size,
        platform=platform,
    )
    return BuildCommandActivation(
        command_name=command.name,
        artifact_path=artifact_path,
        cache_key=receipt.cache_key,
        receipt_sha256=receipt_hash,
        artifact_sha256=receipt.artifact.sha256,
        artifact_size=receipt.artifact.size,
    )


def project_bin_dir(project_root: Path) -> Path:
    return project_root / ".agents" / "bin"


def global_bin_dir(csk_home: Path) -> Path:
    return csk_home / "global" / "bin"


def shim_path(bin_dir: Path, command_name: str, *, platform_name: str | None = None) -> Path:
    """Return the single launcher path one command name owns in a bin directory."""

    platform = _resolve_platform(platform_name)
    _require_command_name(command_name)
    if platform == WINDOWS_PLATFORM and not command_name.endswith(_CMD_SUFFIX):
        return bin_dir / f"{command_name}{_CMD_SUFFIX}"
    return bin_dir / command_name


def write_project_shim(
    project_root: Path,
    command_name: str,
    runtime_path: Path,
    *,
    platform_name: str | None = None,
    path_entries: tuple[Path, ...] = (),
) -> Path:
    return write_bin_shim(
        project_bin_dir(project_root),
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
        global_bin_dir(csk_home),
        command_name,
        runtime_path,
        platform_name=platform_name,
        path_entries=path_entries,
    )


def write_project_build_shim(
    project_root: Path,
    activation: BuildCommandActivation,
    *,
    platform_name: str | None = None,
    path_entries: tuple[Path, ...] = (),
) -> Path:
    return activate_build_command(
        project_bin_dir(project_root),
        activation,
        platform_name=platform_name,
        path_entries=path_entries,
    )


def write_global_build_shim(
    csk_home: Path,
    activation: BuildCommandActivation,
    *,
    platform_name: str | None = None,
    path_entries: tuple[Path, ...] = (),
) -> Path:
    return activate_build_command(
        global_bin_dir(csk_home),
        activation,
        platform_name=platform_name,
        path_entries=path_entries,
    )


def activate_build_command(
    bin_dir: Path,
    activation: BuildCommandActivation,
    *,
    platform_name: str | None = None,
    path_entries: tuple[Path, ...] = (),
) -> Path:
    """Publish one launcher for an already validated compiled artifact."""

    if not isinstance(activation, BuildCommandActivation):
        raise ShimError("Build activation must be a BuildCommandActivation")
    return write_bin_shim(
        bin_dir,
        activation.command_name,
        activation.artifact_path,
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
    platform = _resolve_platform(platform_name)
    shim = shim_path(bin_dir, command_name, platform_name=platform)
    target = _require_launcher_target(
        Path(runtime_path),
        subject=f"Command {command_name!r} launcher target",
    )
    entries = _require_path_entries(path_entries, platform=platform)
    bin_dir.mkdir(parents=True, exist_ok=True)
    _clear_shim(shim)
    # Both launchers carry explicit line endings, so newline translation is
    # disabled: a Windows host would otherwise write CR CR LF into a .cmd.
    if platform == WINDOWS_PLATFORM:
        shim.write_text(_windows_launcher(target, entries), encoding="utf-8", newline="")
        return shim
    if entries:
        shim.write_text(_unix_launcher(target, entries), encoding="utf-8", newline="\n")
        _grant_execute(shim)
        return shim
    shim.symlink_to(os.path.relpath(target, shim.parent))
    return shim


def remove_stale_shims(
    project_root: Path,
    expected_commands: set[str],
    *,
    platform_name: str | None = None,
) -> None:
    remove_stale_shims_in(project_bin_dir(project_root), expected_commands, platform_name=platform_name)


def remove_stale_global_shims(
    csk_home: Path,
    expected_commands: set[str],
    *,
    platform_name: str | None = None,
) -> None:
    remove_stale_shims_in(global_bin_dir(csk_home), expected_commands, platform_name=platform_name)


def remove_stale_shims_in(
    bin_dir: Path,
    expected_commands: set[str],
    *,
    platform_name: str | None = None,
) -> None:
    """Remove every launcher in a bin directory that no active command owns.

    Script and compiled commands are indistinguishable here on purpose: one
    name owns one launcher path, whatever produced it.
    """

    platform = _resolve_platform(platform_name)
    if not bin_dir.exists():
        return
    for child in bin_dir.iterdir():
        if not child.is_file() and not child.is_symlink():
            continue
        if _shim_command_name(child, expected_commands, platform=platform) is None:
            child.unlink()


def _shim_command_name(shim: Path, expected_commands: set[str], *, platform: str) -> str | None:
    if shim.name in expected_commands:
        return shim.name
    if (
        platform == WINDOWS_PLATFORM
        and shim.suffix.lower() == _CMD_SUFFIX
        and shim.stem in expected_commands
    ):
        return shim.stem
    return None


def _resolve_platform(platform_name: str | None) -> str:
    if platform_name is None:
        return WINDOWS_PLATFORM if os.name == "nt" else UNIX_PLATFORM
    if platform_name not in SUPPORTED_PLATFORMS:
        raise ShimError(f"Unsupported activation platform {platform_name!r}")
    return platform_name


def _command_relative_path(command: CommandSpec, platform: str) -> str:
    relative = command.win_path if platform == WINDOWS_PLATFORM else command.unix_path
    if not relative:
        raise ShimError(f"Command {command.name!r} has no path for {platform}")
    _require_portable_relative(relative, f"Command {command.name!r} path")
    return relative


def _runtime_command_filename(command_name: str, platform: str) -> str:
    _require_command_name(command_name)
    if platform == WINDOWS_PLATFORM and not command_name.endswith(_CMD_SUFFIX):
        return f"{command_name}{_CMD_SUFFIX}"
    return command_name


def _publish_runtime_directory(
    runtime_dir: Path,
    staged: Path,
    *,
    runtime_roots: tuple[str, ...],
    required_paths: tuple[str, ...],
    commit: str,
) -> None:
    stale: Path | None = None
    if runtime_dir.exists() or runtime_dir.is_symlink():
        if runtime_state_is_complete(
            runtime_dir,
            runtime_roots=runtime_roots,
            required_paths=required_paths,
        ):
            # A concurrent install published complete state first; keep it.
            _discard(staged)
            return
        stale = _unique_path(runtime_dir.parent, f".{commit}.stale-{os.getpid()}")
        os.replace(runtime_dir, stale)
    try:
        staged.rename(runtime_dir)
    except OSError:
        if stale is not None:
            os.replace(stale, runtime_dir)
        raise
    if stale is not None:
        with suppress(OSError):
            _discard(stale)


def _is_real_directory(path: Path) -> bool:
    try:
        info = path.lstat()
    except OSError:
        return False
    return stat.S_ISDIR(info.st_mode)


def _contained_entry(base: Path, relative: str, *, directory: bool) -> Path | None:
    """Resolve one manager-owned relative entry without traversing any link.

    Rejecting a link at every component is what proves containment: no path
    below ``base`` can then name a file outside it.
    """

    _require_portable_relative(relative, f"Runtime path {relative!r}")
    parts = PurePosixPath(relative).parts
    current = base
    for component in parts[:-1]:
        current = current / component
        if not _is_real_directory(current):
            return None
    current = current / parts[-1]
    try:
        info = current.lstat()
    except OSError:
        return None
    if directory:
        return current if stat.S_ISDIR(info.st_mode) else None
    return current if stat.S_ISREG(info.st_mode) else None


def _require_cache_artifact(
    csk_home: Path,
    artifact_path: Path,
    *,
    command_name: str,
    expected_relative: str,
    expected_size: int,
    platform: str,
) -> None:
    if not artifact_path.is_absolute():
        raise ShimError(f"Command {command_name!r} artifact path must be absolute: {artifact_path}")
    expected_parts = PurePosixPath(expected_relative).parts
    if tuple(artifact_path.parts[-len(expected_parts) :]) != expected_parts:
        raise ShimError(
            f"Command {command_name!r} artifact path {artifact_path} does not end in {expected_relative!r}"
        )
    home = Path(csk_home)
    if not artifact_path.is_relative_to(home):
        raise ShimError(
            f"Command {command_name!r} artifact must live below the manager home {home}: {artifact_path}"
        )
    if artifact_path.is_relative_to(home / RUNTIME_DIRECTORY):
        raise ShimError(
            f"Command {command_name!r} compiled artifact must not use the script runtime namespace: {artifact_path}"
        )
    try:
        info = artifact_path.lstat()
    except OSError as exc:
        raise ShimError(f"Command {command_name!r} artifact is unavailable: {artifact_path}") from exc
    if not stat.S_ISREG(info.st_mode):
        raise ShimError(f"Command {command_name!r} artifact is not a regular file: {artifact_path}")
    if info.st_size != expected_size:
        raise ShimError(
            f"Command {command_name!r} artifact size {info.st_size} does not match "
            f"receipt size {expected_size}"
        )
    if platform != WINDOWS_PLATFORM and not stat.S_IMODE(info.st_mode) & stat.S_IXUSR:
        # The cache owns artifact permissions; activation verifies instead of
        # relaxing them, because the published entry is immutable.
        raise ShimError(f"Command {command_name!r} artifact is not owner-executable: {artifact_path}")


def _require_command_name(command_name: str) -> str:
    if not isinstance(command_name, str) or not is_valid_identifier(command_name):
        raise ShimError(f"Command name {command_name!r} {IDENTIFIER_RULE}")
    return command_name


def _require_portable_relative(relative: str, subject: str) -> str:
    if not isinstance(relative, str) or not is_valid_portable_path(relative):
        raise ShimError(f"{subject} is not a portable relative path")
    return relative


def _require_launcher_target(path: Path, *, subject: str) -> Path:
    if not path.is_absolute():
        raise ShimError(f"{subject} must be an absolute path: {str(path)!r}")
    _require_launcher_scalars(str(path), subject=subject)
    return path


def _require_launcher_scalars(text: str, *, subject: str) -> None:
    for scalar in _FORBIDDEN_LAUNCHER_SCALARS:
        if scalar in text:
            raise ShimError(f"{subject} must not contain {scalar!r}: {text!r}")


def _require_path_entries(path_entries: Sequence[Path], *, platform: str) -> tuple[Path, ...]:
    separator = ";" if platform == WINDOWS_PLATFORM else ":"
    entries: list[Path] = []
    for entry in path_entries:
        candidate = Path(entry)
        subject = "Launcher PATH entry"
        if not candidate.is_absolute():
            raise ShimError(f"{subject} must be an absolute path: {str(candidate)!r}")
        _require_launcher_scalars(str(candidate), subject=subject)
        if separator in str(candidate):
            raise ShimError(f"{subject} must not contain {separator!r}: {str(candidate)!r}")
        entries.append(candidate)
    return tuple(entries)


def _windows_launcher(target: Path, path_entries: tuple[Path, ...]) -> str:
    lines = [
        "@echo off",
        "setlocal DisableDelayedExpansion",
        # An inherited ERRORLEVEL *variable* shadows cmd's dynamic exit status,
        # so clear it in the local scope before reading the real value below.
        'set "ERRORLEVEL="',
    ]
    if path_entries:
        prefix = ";".join(_escape_cmd_value(str(entry)) for entry in path_entries)
        lines.append(f'set "PATH={prefix};%PATH%"')
    lines.append(f'call "{_escape_cmd_value(str(target))}" %*')
    lines.append("exit /b %ERRORLEVEL%")
    return "".join(f"{line}\r\n" for line in lines)


def _unix_launcher(target: Path, path_entries: tuple[Path, ...]) -> str:
    prefix = ":".join(str(entry) for entry in path_entries)
    return (
        "#!/bin/sh\n"
        'if [ -n "${PATH:-}" ]; then\n'
        f"  PATH={shlex.quote(prefix)}:\"$PATH\"\n"
        "else\n"
        f"  PATH={shlex.quote(prefix)}\n"
        "fi\n"
        "export PATH\n"
        f"exec {shlex.quote(str(target))} \"$@\"\n"
    )


def _escape_cmd_value(value: str) -> str:
    return value.replace("%", "%%")


def _clear_shim(shim: Path) -> None:
    if shim.is_symlink():
        shim.unlink()
        return
    if shim.is_dir():
        raise ShimError(f"Launcher path is a directory: {shim}")
    if shim.exists():
        shim.unlink()


def _grant_execute(path: Path) -> None:
    """Add execute bits to a real file through a descriptor that follows no link.

    Windows has no POSIX execute bit, so this is a no-op there; a Windows host
    activates commands through ``.cmd`` launchers instead.
    """

    if sys.platform == "win32":
        return
    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC)
    try:
        info = os.fstat(descriptor)
        if not stat.S_ISREG(info.st_mode):
            raise ShimError(f"Executable path is not a regular file: {path}")
        mode = stat.S_IMODE(info.st_mode)
        if mode | _EXECUTE_BITS != mode:
            os.fchmod(descriptor, mode | _EXECUTE_BITS)
    finally:
        os.close(descriptor)


def _unique_path(parent: Path, prefix: str) -> Path:
    for index in range(1000):
        candidate = parent / f"{prefix}-{index}"
        if not candidate.exists() and not candidate.is_symlink():
            return candidate
    raise ShimError(f"Cannot allocate a staging path under {parent}")


def _discard(path: Path) -> None:
    """Remove one staging or superseded path without following a link."""

    if path.is_symlink():
        path.unlink()
        return
    if path.is_dir():
        shutil.rmtree(path)
        return
    path.unlink(missing_ok=True)


def _require_sha256(value: str, subject: str) -> None:
    if (
        not isinstance(value, str)
        or not value.startswith(_SHA256_PREFIX)
        or len(value) != len(_SHA256_PREFIX) + 64
        or not set(value[len(_SHA256_PREFIX) :]) <= _SHA256_DIGITS
    ):
        raise ShimError(f"{subject} must be 'sha256:' and 64 lowercase hexadecimal digits")
