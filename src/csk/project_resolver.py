from __future__ import annotations

import hashlib
import re
import subprocess
from dataclasses import dataclass
from pathlib import Path

from . import manifest
from .config import DEFAULT_WORKTREE_ALIAS_PATTERN


class ProjectResolutionError(Exception):
    pass


SHARED_BRANCHES = {"main", "master", "develop", "development", "dev", "trunk"}
ALIAS_CLEAN_RE = re.compile(r"[^a-z0-9._-]+")


@dataclass(frozen=True)
class ResolvedProject:
    project_alias: str
    checkout_alias: str
    root: Path
    skillfile: Path
    branch: str | None
    task_id: str | None
    path_hash: str


def resolve(start: Path, *, worktree_alias_pattern: str = DEFAULT_WORKTREE_ALIAS_PATTERN) -> ResolvedProject:
    root = find_project_root(start)
    project_manifest = manifest.load_manifest(root)
    if project_manifest is None:
        raise ProjectResolutionError(f"Skillfile.json not found at project root: {root}")

    project_alias = _clean_alias(project_manifest.project_alias or root.name)
    if not project_alias:
        raise ProjectResolutionError(f"Cannot derive project alias for {root}")

    branch = git_branch(root)
    task_id = task_id_from_branch(branch, worktree_alias_pattern)
    path_hash = stable_path_hash(root)
    if branch and branch not in SHARED_BRANCHES:
        checkout_alias = f"{project_alias}-{task_id}-{path_hash}" if task_id else f"{project_alias}-worktree-{path_hash}"
    else:
        checkout_alias = project_alias

    return ResolvedProject(
        project_alias=project_alias,
        checkout_alias=checkout_alias,
        root=root,
        skillfile=root / manifest.MANIFEST_NAME,
        branch=branch,
        task_id=task_id,
        path_hash=path_hash,
    )


def find_project_root(start: Path) -> Path:
    current = start.expanduser()
    if not current.is_absolute():
        current = Path.cwd() / current
    if current.is_file():
        current = current.parent
    try:
        current = current.resolve()
    except FileNotFoundError as exc:
        raise ProjectResolutionError(f"project path does not exist: {start}") from exc

    for candidate in (current, *current.parents):
        if (candidate / manifest.MANIFEST_NAME).exists():
            return candidate
    raise ProjectResolutionError(f"Skillfile.json not found at or above: {current}")


def git_branch(project_root: Path) -> str | None:
    for args in (["branch", "--show-current"], ["rev-parse", "--abbrev-ref", "HEAD"]):
        proc = subprocess.run(
            ["git", "-C", str(project_root), *args],
            text=True,
            capture_output=True,
            check=False,
        )
        branch = proc.stdout.strip()
        if proc.returncode == 0 and branch and branch != "HEAD":
            return branch
    return None


def task_id_from_branch(branch: str | None, pattern: str) -> str | None:
    if not branch:
        return None
    match = re.search(pattern, branch, re.IGNORECASE)
    if not match:
        return None
    value = match.group(1) if match.groups() else match.group(0)
    return value.lower()


def stable_path_hash(path: Path) -> str:
    return hashlib.sha1(str(path.resolve()).encode("utf-8")).hexdigest()[:4]


def _clean_alias(value: str) -> str:
    cleaned = ALIAS_CLEAN_RE.sub("-", value.strip().lower()).strip("-")
    return re.sub(r"-{2,}", "-", cleaned)
