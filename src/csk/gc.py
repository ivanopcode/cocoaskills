from __future__ import annotations

import json
import re
import shutil
from dataclasses import dataclass
from pathlib import Path

from . import consumers
from .config import GlobalConfig
from .locking import _pid_alive


# Interrupted installs leave .<name>.tmp-<pid> / .<name>.backup-<pid> entries
# behind. They are safe to delete once the owning process is gone.
_ORPHAN_RE = re.compile(r"^\..+\.(tmp|backup)-(\d+)$")


@dataclass
class GcStats:
    runtime_removed: int = 0
    snapshots_removed: int = 0
    consumers_pruned: int = 0


def collect_runtime(config: GlobalConfig, csk_home: Path) -> GcStats:
    stats = GcStats()
    referenced: set[tuple[str, str]] = set()
    referenced_snapshots: set[tuple[str, str]] = set()
    _collect_markers(csk_home / "global" / "skills", referenced, referenced_snapshots)
    sweep_orphans(csk_home / "global" / "skills")
    _collect_markers(csk_home / "hybrid" / "skills", referenced, referenced_snapshots)
    sweep_orphans(csk_home / "hybrid" / "skills")
    for project in config.projects.values():
        _collect_markers(project.path / ".agents" / "skills", referenced, referenced_snapshots)
        sweep_orphans(project.path / ".agents" / "skills")

    # Checkouts installed without registration ('csk install .') reference
    # runtime through the consumer registry; dead entries are pruned here.
    alive: list[Path] = []
    known = consumers.load_consumers(csk_home)
    for consumer in known:
        if consumer.exists() and _collect_markers(consumer / ".agents" / "skills", referenced, referenced_snapshots):
            alive.append(consumer)
            sweep_orphans(consumer / ".agents" / "skills")
    stats.consumers_pruned = len(known) - len(alive)
    consumers.replace_consumers(csk_home, alive)

    stats.snapshots_removed = _collect_snapshots(csk_home, referenced_snapshots)

    runtime_root = csk_home / "runtime"
    if not runtime_root.exists():
        return stats
    for skill_dir in runtime_root.iterdir():
        if not skill_dir.is_dir():
            continue
        sweep_orphans(skill_dir)
        for commit_dir in skill_dir.iterdir():
            if commit_dir.is_dir() and (skill_dir.name, commit_dir.name) not in referenced:
                shutil.rmtree(commit_dir)
                stats.runtime_removed += 1
    return stats


def _collect_snapshots(csk_home: Path, referenced: set[tuple[str, str]]) -> int:
    cache_root = csk_home / "cache"
    if not cache_root.exists():
        return 0
    removed = 0
    # Layout: cache/<source>/<commit>/snapshot, where <source> may be nested.
    for snapshot_dir in sorted(cache_root.rglob("snapshot")):
        if not snapshot_dir.is_dir():
            continue
        commit_dir = snapshot_dir.parent
        source = commit_dir.parent.relative_to(cache_root).as_posix()
        if (source, commit_dir.name) in referenced:
            continue
        shutil.rmtree(commit_dir)
        removed += 1
        parent = commit_dir.parent
        while parent != cache_root and not any(parent.iterdir()):
            parent.rmdir()
            parent = parent.parent
    return removed


def sweep_orphans(directory: Path) -> None:
    if not directory.exists():
        return
    for child in directory.iterdir():
        match = _ORPHAN_RE.match(child.name)
        if not match:
            continue
        if _pid_alive(int(match.group(2))):
            continue
        if child.is_dir() and not child.is_symlink():
            shutil.rmtree(child, ignore_errors=True)
        else:
            try:
                child.unlink()
            except OSError:
                pass


def _collect_markers(
    skills_root: Path,
    referenced: set[tuple[str, str]],
    referenced_snapshots: set[tuple[str, str]] | None = None,
) -> bool:
    if not skills_root.exists():
        return False
    found = False
    for marker in skills_root.glob("*/.csk-install.json"):
        try:
            data = json.loads(marker.read_text(encoding="utf-8"))
        except Exception:
            continue
        name = data.get("name")
        commit = data.get("commit")
        if isinstance(name, str) and isinstance(commit, str):
            referenced.add((name, commit))
            found = True
            if referenced_snapshots is not None:
                source = data.get("source")
                referenced_snapshots.add((source if isinstance(source, str) and source else name, commit))
    return found
