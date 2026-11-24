from __future__ import annotations

import os
import shutil
import stat
from dataclasses import dataclass
from pathlib import Path

from . import git_ops

CACHE_DIRNAME = "cache"
SNAPSHOT_DIRNAME = "snapshot"
REGISTRY_CACHE_DIRNAME = "registry"
RESERVED_CACHE_TOPLEVEL = frozenset({REGISTRY_CACHE_DIRNAME})
IGNORED_METADATA_NAMES = frozenset({".DS_Store"})
SNAPSHOT_ROOT_MARKER_FILES = frozenset({"SKILL.md", "csk-skill.json", "agent-skill.json"})
SNAPSHOT_TMP_PREFIX = ".snapshot-"
SNAPSHOT_TMP_SUFFIX = ".tmp"
_HEX_DIGITS = frozenset("0123456789abcdefABCDEF")


def _snapshot_tmp_name(commit: str) -> str:
    return f"{SNAPSHOT_TMP_PREFIX}{commit}{SNAPSHOT_TMP_SUFFIX}"


def _is_snapshot_tmp_name(name: str) -> bool:
    return (
        len(name) > len(SNAPSHOT_TMP_PREFIX) + len(SNAPSHOT_TMP_SUFFIX)
        and name.startswith(SNAPSHOT_TMP_PREFIX)
        and name.endswith(SNAPSHOT_TMP_SUFFIX)
    )


def _is_git_commit_hex(name: str) -> bool:
    return len(name) in {40, 64} and all(ch in _HEX_DIGITS for ch in name)


def _looks_like_snapshot_content_root(snap_path: Path, parent_name: str) -> bool:
    """Return True when ``snap_path`` looks like an archived skill tree, not a source segment."""
    if _is_git_commit_hex(parent_name):
        return True
    for marker_name in SNAPSHOT_ROOT_MARKER_FILES:
        try:
            info = (snap_path / marker_name).lstat()
        except OSError:
            continue
        if stat.S_ISREG(info.st_mode) and not stat.S_ISLNK(info.st_mode):
            return True
    return False


class SnapshotError(Exception):
    pass


@dataclass(frozen=True)
class SnapshotEntry:
    source: str
    commit: str
    commit_dir: Path
    snapshot_dir: Path
    unexpected_siblings: tuple[str, ...] = ()


def snapshot_dir(csk_home: Path, source: str, commit: str) -> Path:
    return csk_home / CACHE_DIRNAME / source / commit / SNAPSHOT_DIRNAME


def iter_snapshot_entries(
    csk_home: Path,
    *,
    include_reserved: bool = False,
) -> tuple[list[SnapshotEntry], list[str]]:
    """Walk ``cache/<source>/<commit>/snapshot`` without descending into snapshot trees.

    Any directory named ``snapshot`` is treated as a leaf boundary and is never
    descended into, so directories named ``snapshot`` inside a skill's own
    content can never become GC candidates or shadow another commit's snapshot.
    Non-directory or shallow ``snapshot`` entries and symbolic links are
    retained with warnings instead of raising. ``include_reserved`` is for
    read-only lookups only (e.g. legacy runtime validation of a skill literally
    named ``registry``); garbage collection leaves it ``False`` so
    ``cache/registry`` is never pruned.
    """
    cache_root = csk_home / CACHE_DIRNAME
    try:
        root_info = cache_root.lstat()
    except FileNotFoundError:
        return [], []
    except OSError as exc:
        return [], [f"snapshot cache retained: {exc}"]
    if stat.S_ISLNK(root_info.st_mode) or not stat.S_ISDIR(root_info.st_mode):
        return [], [
            f"snapshot cache retained because it is not a real directory: {cache_root}"
        ]

    entries: list[SnapshotEntry] = []
    warnings: list[str] = []
    pending: list[tuple[Path, tuple[str, ...]]] = [(cache_root, ())]
    while pending:
        current, rel_parts = pending.pop()
        try:
            children = sorted(
                current.iterdir(),
                key=lambda item: item.name.encode("utf-8"),
            )
        except OSError as exc:
            warnings.append(f"cannot list snapshot cache directory {current}: {exc}")
            continue

        snapshot_child: tuple[Path, os.stat_result] | None = None
        real_subdirs: list[Path] = []
        staging_dirs: list[Path] = []
        other_names: list[str] = []
        for child in children:
            if not include_reserved and not rel_parts and child.name in RESERVED_CACHE_TOPLEVEL:
                # cache/registry is owned by the audit-registry HTTP cache, not
                # the snapshot cache; never inspect, warn on, or prune it during GC.
                continue
            try:
                info = child.lstat()
            except FileNotFoundError:
                continue
            except OSError as exc:
                warnings.append(f"cannot inspect snapshot cache entry {child}: {exc}")
                other_names.append(child.name)
                continue
            if child.name == SNAPSHOT_DIRNAME:
                snapshot_child = (child, info)
                continue
            if stat.S_ISLNK(info.st_mode):
                warnings.append(
                    f"snapshot cache entry retained because it is a symbolic link: {child}"
                )
                other_names.append(child.name)
                continue
            if stat.S_ISDIR(info.st_mode):
                if _is_snapshot_tmp_name(child.name):
                    staging_dirs.append(child)
                else:
                    real_subdirs.append(child)
                other_names.append(child.name)
                continue
            if child.name in IGNORED_METADATA_NAMES and stat.S_ISREG(info.st_mode):
                continue
            other_names.append(child.name)

        allowed_tmp: str | None = None
        if snapshot_child is not None:
            snap_path, snap_info = snapshot_child
            if stat.S_ISLNK(snap_info.st_mode) or not stat.S_ISDIR(snap_info.st_mode):
                warnings.append(
                    f"snapshot entry retained because it is not a real directory: {snap_path}"
                )
            elif len(rel_parts) < 2:
                warnings.append(
                    "snapshot entry retained because it does not match "
                    f"cache/<source>/<commit>/snapshot layout: {snap_path}"
                )
            else:
                source = "/".join(rel_parts[:-1])
                commit = rel_parts[-1]
                allowed_tmp = _snapshot_tmp_name(commit)
                unexpected = tuple(name for name in other_names if name != allowed_tmp)
                entries.append(
                    SnapshotEntry(
                        source=source,
                        commit=commit,
                        commit_dir=current,
                        snapshot_dir=snap_path,
                        unexpected_siblings=unexpected,
                    )
                )

        for staging_dir in staging_dirs:
            if allowed_tmp is None or staging_dir.name != allowed_tmp:
                warnings.append(
                    f"snapshot staging directory retained without descending: {staging_dir}"
                )

        for subdir in reversed(real_subdirs):
            pending.append((subdir, (*rel_parts, subdir.name)))

    return entries, warnings


def find_snapshot_for_commit(
    csk_home: Path,
    commit: str,
    *,
    preferred_source: str | None = None,
) -> Path | None:
    """Read-only lookup of a retained source snapshot matching ``commit``.

    Uses :func:`iter_snapshot_entries` with ``include_reserved=True`` (without
    filtering on siblings) so skills named ``registry`` and snapshots with
    adjacent metadata or sibling directories are always found. If no primary
    match exists, falls back to walking source paths whose own path contains a
    ``snapshot`` segment, without ever descending into a real skill snapshot
    content tree or following symbolic links.
    """
    entries, _warnings = iter_snapshot_entries(csk_home, include_reserved=True)
    fallback_match: Path | None = None
    for entry in entries:
        if entry.commit != commit:
            continue
        if preferred_source is not None and entry.source == preferred_source:
            return entry.snapshot_dir
        if fallback_match is None:
            fallback_match = entry.snapshot_dir
    if fallback_match is not None:
        return fallback_match

    cache_root = csk_home / CACHE_DIRNAME
    try:
        root_info = cache_root.lstat()
    except OSError:
        return None
    if stat.S_ISLNK(root_info.st_mode) or not stat.S_ISDIR(root_info.st_mode):
        return None

    # Fallback for source paths containing a `snapshot` segment (e.g.
    # snapshot/<skill>/<commit>/snapshot, host/snapshot/repo/<commit>/snapshot,
    # org/team/snapshot/<commit>/snapshot). Never descend into a real skill
    # snapshot root (identified by commit-hex parent or root SKILL.md/manifest)
    # or into staging directories or symbolic links.
    pending: list[tuple[Path, tuple[str, ...]]] = [(cache_root, ())]
    best: Path | None = None
    while pending:
        current, rel_parts = pending.pop()
        try:
            children = sorted(
                current.iterdir(),
                key=lambda item: item.name.encode("utf-8"),
            )
        except OSError:
            continue
        real_subdirs: list[Path] = []
        for child in children:
            try:
                info = child.lstat()
            except OSError:
                continue
            if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
                continue
            if _is_snapshot_tmp_name(child.name):
                continue
            if child.name == SNAPSHOT_DIRNAME:
                if len(rel_parts) >= 2 and rel_parts[-1] == commit:
                    source = "/".join(rel_parts[:-1])
                    if preferred_source is not None and source == preferred_source:
                        return child
                    if best is None:
                        best = child
                    continue
                if len(rel_parts) < 2 or not _looks_like_snapshot_content_root(
                    child, rel_parts[-1]
                ):
                    real_subdirs.append(child)
                continue
            real_subdirs.append(child)
        for subdir in reversed(real_subdirs):
            pending.append((subdir, (*rel_parts, subdir.name)))
    return best


def get_snapshot(csk_home: Path, source: str, repo: Path, commit: str) -> Path:
    target = snapshot_dir(csk_home, source, commit)
    if target.exists():
        return target
    tmp = target.with_name(_snapshot_tmp_name(commit))
    if tmp.exists():
        shutil.rmtree(tmp)
    tmp.mkdir(parents=True, exist_ok=True)
    git_ops.archive(repo, commit, tmp)
    target.parent.mkdir(parents=True, exist_ok=True)
    if target.exists():
        shutil.rmtree(tmp)
        return target
    tmp.rename(target)
    return target
