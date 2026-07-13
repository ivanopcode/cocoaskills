from __future__ import annotations

import fnmatch
import os
import shutil
import sys
import unicodedata
from pathlib import Path

from .identifiers import is_valid_portable_path


class WhitelistError(Exception):
    pass


INCLUDE_ROOTS = {
    "SKILL.md",
    "agents",
    "references",
    ".skill_triggers",
    "assets",
    "templates",
    "examples",
    "data",
}

ALWAYS_EXCLUDED = [
    ".git",
    ".github",
    ".gitlab-ci.yml",
    ".venv",
    "__pycache__",
    "*.pyc",
    "node_modules",
    "tests",
    "test",
    "__tests__",
    "README*",
    "CHANGELOG*",
    "LICENSE*",
    "Makefile",
    "setup.py",
    "pyproject.toml",
    "requirements*.txt",
    ".DS_Store",
    ".gitignore",
]


def copy_context(
    snapshot: Path,
    destination: Path,
    *,
    include_scripts: bool = False,
    exclude_roots: tuple[str, ...] = (),
) -> list[str]:
    skill_file = snapshot / "SKILL.md"
    if not skill_file.is_file():
        raise WhitelistError(f"Required SKILL.md not found in skill snapshot: {snapshot}")

    if destination.exists():
        shutil.rmtree(destination)
    destination.mkdir(parents=True)

    roots = set(INCLUDE_ROOTS)
    if include_scripts:
        roots.add("scripts")

    copied: list[str] = []
    platform_paths: dict[str, str] = {}
    for root in sorted(roots):
        src = snapshot / root
        if not src.exists():
            continue
        if src.is_symlink():
            raise WhitelistError(f"symbolic links are not supported in skill context: {root}")
        if _is_excluded(Path(root)):
            continue
        if src.is_file():
            rel = Path(root)
            if _is_excluded_root(rel, exclude_roots):
                continue
            _validate_selected_path(rel, platform_paths)
            _copy_file(src, destination / rel)
            copied.append(_posix(rel))
            continue
        for candidate in src.rglob("*"):
            if candidate.is_symlink():
                raise WhitelistError(
                    f"symbolic links are not supported in skill context: {candidate.relative_to(snapshot).as_posix()}"
                )
        for file in sorted(path for path in src.rglob("*") if path.is_file()):
            rel = file.relative_to(snapshot)
            if _is_excluded_root(rel, exclude_roots):
                continue
            if _is_excluded(rel):
                continue
            _validate_selected_path(rel, platform_paths)
            _copy_file(file, destination / rel)
            copied.append(_posix(rel))
    return sorted(copied)


def _copy_file(src: Path, dst: Path) -> None:
    dst.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(src, dst)


def _validate_selected_path(path: Path, seen: dict[str, str]) -> None:
    relative = path.as_posix()
    if not is_valid_portable_path(relative):
        raise WhitelistError(f"non-portable path in skill context: {relative}")
    key = os.path.normcase(relative)
    if sys.platform in {"darwin", "win32"}:
        key = unicodedata.normalize("NFD", key).casefold()
    previous = seen.get(key)
    if previous is not None and previous != relative:
        raise WhitelistError(f"skill context paths collide on this platform: {previous!r} and {relative!r}")
    seen[key] = relative


def _is_excluded(rel: Path) -> bool:
    parts = rel.parts
    for part in parts:
        for pattern in ALWAYS_EXCLUDED:
            if fnmatch.fnmatchcase(part, pattern):
                return True
    rel_posix = _posix(rel)
    return any(fnmatch.fnmatchcase(rel_posix, pattern) for pattern in ALWAYS_EXCLUDED)


def _is_excluded_root(rel: Path, exclude_roots: tuple[str, ...]) -> bool:
    rel_parts = rel.as_posix().split("/")
    for root in exclude_roots:
        root_parts = root.split("/")
        if len(rel_parts) >= len(root_parts) and rel_parts[: len(root_parts)] == root_parts:
            return True
    return False


def _posix(path: Path) -> str:
    return path.as_posix()
