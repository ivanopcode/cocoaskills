from __future__ import annotations

import fnmatch
import shutil
from pathlib import Path


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
    "dependencies.json",
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
    for root in sorted(roots):
        src = snapshot / root
        if not src.exists():
            continue
        if _is_excluded(Path(root)):
            continue
        if src.is_file():
            rel = Path(root)
            if _is_excluded_root(rel, exclude_roots):
                continue
            _copy_file(src, destination / rel)
            copied.append(_posix(rel))
            continue
        for file in sorted(path for path in src.rglob("*") if path.is_file()):
            rel = file.relative_to(snapshot)
            if _is_excluded_root(rel, exclude_roots):
                continue
            if _is_excluded(rel):
                continue
            _copy_file(file, destination / rel)
            copied.append(_posix(rel))
    return sorted(copied)


def _copy_file(src: Path, dst: Path) -> None:
    dst.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(src, dst)


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
