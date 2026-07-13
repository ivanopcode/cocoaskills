from __future__ import annotations

import json
import os
import shutil
from dataclasses import dataclass
from pathlib import Path

from . import protocol_json, shims
from .identifiers import is_valid_identifier


MANAGED_FILE = ".csk-managed.json"
SCHEMA_VERSION = 1
USER_BIN_ENV = "CSK_GLOBAL_USER_BIN"


@dataclass(frozen=True)
class UserBinSelection:
    path: Path | None
    warning: str | None = None


def refresh_user_bin_shims(
    csk_home: Path,
    expected_commands: set[str],
    *,
    platform_name: str | None = None,
    env: dict[str, str] | None = None,
    home: Path | None = None,
) -> list[str]:
    env = env or dict(os.environ)
    home = home or Path.home()
    platform_name = platform_name or ("windows" if os.name == "nt" else "unix")
    selection = select_user_bin_with_warning(csk_home, env=env, home=home)
    target = selection.path
    if target is None:
        if expected_commands:
            return [
                selection.warning
                or (
                    "global: command shims were installed in ~/.cocoaskills/global/bin, "
                    "but no safe PATH-visible user bin was found; add that directory to PATH, "
                    f"set {USER_BIN_ENV} to a writable PATH directory, or use csk shell-init"
                )
            ]
        return []

    try:
        target.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        if expected_commands:
            return [
                "global: command shims were installed in ~/.cocoaskills/global/bin, "
                f"but {target} could not be created: {exc}; add ~/.cocoaskills/global/bin to PATH, "
                f"set {USER_BIN_ENV} to a writable PATH directory, or use csk shell-init"
            ]
        return []
    managed = _read_managed(target)
    next_managed: set[str] = set()
    messages: list[str] = []
    canonical_bin = csk_home / "global" / "bin"

    for command_name in managed - expected_commands:
        _remove_managed_command(target, command_name, platform_name=platform_name)

    for command_name in sorted(expected_commands):
        canonical = _shim_path(canonical_bin, command_name, platform_name=platform_name)
        published = _shim_path(target, command_name, platform_name=platform_name)
        if _is_unmanaged_conflict(published, command_name, managed, canonical):
            messages.append(
                f"global: command {command_name!r} not published to {target}; "
                f"target exists and is not managed by csk: {published}"
            )
            continue
        shims.write_bin_shim(target, command_name, canonical, platform_name=platform_name)
        next_managed.add(command_name)

    _write_managed(target, next_managed)
    if next_managed:
        messages.append(f"global: command shims published to {target}")
    return messages


def select_user_bin(
    csk_home: Path,
    *,
    env: dict[str, str] | None = None,
    home: Path | None = None,
) -> Path | None:
    return select_user_bin_with_warning(csk_home, env=env, home=home).path


def select_user_bin_with_warning(
    csk_home: Path,
    *,
    env: dict[str, str] | None = None,
    home: Path | None = None,
) -> UserBinSelection:
    env = env or dict(os.environ)
    home = home or Path.home()
    path_dirs = _path_dirs(env.get("PATH", ""))
    explicit = env.get(USER_BIN_ENV)
    if explicit:
        explicit_path = Path(explicit).expanduser()
        if _is_disallowed_bin(explicit_path, csk_home=csk_home):
            return UserBinSelection(
                None,
                "global: command shims were installed in ~/.cocoaskills/global/bin, "
                f"but {USER_BIN_ENV} points to a protected tool-manager or CocoaSkills directory: "
                f"{explicit_path}; choose a normal user bin or use csk shell-init",
            )
        if not _is_writable_or_creatable(explicit_path):
            return UserBinSelection(
                None,
                "global: command shims were installed in ~/.cocoaskills/global/bin, "
                f"but {USER_BIN_ENV} is not writable: {explicit_path}; choose a writable PATH directory "
                "or use csk shell-init",
            )
        return UserBinSelection(explicit_path)

    preferred = [
        home / ".local" / "bin",
        home / "bin",
    ]
    for candidate in preferred:
        if _path_contains(path_dirs, candidate):
            return UserBinSelection(candidate)

    csk_executable = shutil.which("csk", path=env.get("PATH"))
    if csk_executable:
        csk_bin = Path(csk_executable).expanduser().parent
        if _is_safe_home_bin(csk_bin, home=home, csk_home=csk_home):
            return UserBinSelection(csk_bin)

    for candidate in path_dirs:
        if _is_safe_home_bin(candidate, home=home, csk_home=csk_home):
            return UserBinSelection(candidate)
    return UserBinSelection(None)


def _path_dirs(path_env: str) -> list[Path]:
    result: list[Path] = []
    for raw in path_env.split(os.pathsep):
        if raw:
            result.append(Path(raw).expanduser())
    return result


def _path_contains(paths: list[Path], candidate: Path) -> bool:
    candidate_resolved = _resolve_for_compare(candidate)
    return any(_resolve_for_compare(path) == candidate_resolved for path in paths)


def _resolve_for_compare(path: Path) -> Path:
    try:
        return path.resolve()
    except OSError:
        return path.absolute()


def _is_safe_home_bin(path: Path, *, home: Path, csk_home: Path) -> bool:
    if _is_disallowed_bin(path, csk_home=csk_home):
        return False
    try:
        path.resolve().relative_to(home.resolve())
    except ValueError:
        return False
    return path.exists() and os.access(path, os.W_OK)


def _is_writable_or_creatable(path: Path) -> bool:
    if path.exists():
        return path.is_dir() and os.access(path, os.W_OK)
    parent = path.parent
    while not parent.exists() and parent != parent.parent:
        parent = parent.parent
    return parent.exists() and os.access(parent, os.W_OK)


def _is_disallowed_bin(path: Path, *, csk_home: Path) -> bool:
    resolved = _resolve_for_compare(path)
    canonical_global_bin = _resolve_for_compare(csk_home / "global" / "bin")
    if resolved == canonical_global_bin:
        return True
    parts = set(resolved.parts)
    if ".agents" in parts or ".cocoaskills" in parts:
        return True
    if ".venv" in parts or "venv" in parts or "venvs" in parts:
        return True
    if "mise" in parts and ("installs" in parts or "shims" in parts):
        return True
    if path.name == "shims" and {".asdf", ".pyenv", ".rbenv"} & parts:
        return True
    return False


def _shim_path(bin_dir: Path, command_name: str, *, platform_name: str) -> Path:
    if platform_name == "windows" and not command_name.endswith(".cmd"):
        return bin_dir / f"{command_name}.cmd"
    return bin_dir / command_name


def _remove_managed_command(bin_dir: Path, command_name: str, *, platform_name: str) -> None:
    path = _shim_path(bin_dir, command_name, platform_name=platform_name)
    if path.exists() or path.is_symlink():
        path.unlink()


def _read_managed(bin_dir: Path) -> set[str]:
    path = bin_dir / MANAGED_FILE
    if not path.exists():
        return set()
    try:
        data = protocol_json.loads(path.read_bytes())
    except Exception:
        return set()
    if (
        not isinstance(data, dict)
        or set(data) != {"schema_version", "entries"}
        or data.get("schema_version") != SCHEMA_VERSION
    ):
        return set()
    entries = data["entries"]
    if (
        not isinstance(entries, list)
        or any(not isinstance(entry, str) or not is_valid_identifier(entry) for entry in entries)
        or len(entries) != len(set(entries))
    ):
        return set()
    return set(entries)


def _write_managed(bin_dir: Path, entries: set[str]) -> None:
    path = bin_dir / MANAGED_FILE
    data = {"schema_version": SCHEMA_VERSION, "entries": sorted(entries)}
    path.write_text(json.dumps(data, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def _is_unmanaged_conflict(target: Path, command_name: str, managed: set[str], canonical: Path) -> bool:
    if not target.exists() and not target.is_symlink():
        return False
    if command_name in managed:
        return False
    if target.is_symlink():
        try:
            return target.resolve() != canonical.resolve()
        except OSError:
            return True
    return True
