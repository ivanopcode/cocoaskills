from __future__ import annotations

import shutil
from pathlib import Path

from . import git_ops


class SnapshotError(Exception):
    pass


def snapshot_dir(csk_home: Path, source: str, commit: str) -> Path:
    return csk_home / "cache" / source / commit / "snapshot"


def get_snapshot(csk_home: Path, source: str, repo: Path, commit: str) -> Path:
    target = snapshot_dir(csk_home, source, commit)
    if target.exists():
        return target
    tmp = target.with_name(f".snapshot-{commit}.tmp")
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

