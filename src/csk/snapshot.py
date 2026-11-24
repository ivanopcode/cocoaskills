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
SNAPSHOT_TMP_PREFIX = ".snapshot-"
SNAPSHOT_TMP_SUFFIX = ".tmp"


def _snapshot_tmp_name(commit: str) -> str:
    return f"{SNAPSHOT_TMP_PREFIX}{commit}{SNAPSHOT_TMP_SUFFIX}"


def _is_snapshot_tmp_name(name: str) -> bool:
    return (
        len(name) > len(SNAPSHOT_TMP_PREFIX) + len(SNAPSHOT_TMP_SUFFIX)
        and name.startswith(SNAPSHOT_TMP_PREFIX)
        and name.endswith(SNAPSHOT_TMP_SUFFIX)
    )


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
) -> tuple[list[SnapshotEntry], list[str]]:
    """Walk ``cache/<source>/<commit>/snapshot`` without descending into snapshot trees.

    Any directory named ``snapshot`` is treated as a leaf boundary and is never
    descended into, so directories named ``snapshot`` inside a skill's own
    content can never become GC candidates or shadow another commit's snapshot.
    Non-directory or shallow ``snapshot`` entries and symbolic links are
    retained with warnings instead of raising.
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
            if not rel_parts and child.name in RESERVED_CACHE_TOPLEVEL:
                # cache/registry is owned by the audit-registry HTTP cache, not
                # the snapshot cache; never inspect, warn on, or prune it.
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
