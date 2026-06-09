from __future__ import annotations

import json
import re
import shutil
from pathlib import Path

from . import consumers
from .config import GlobalConfig
from .locking import _pid_alive


# Interrupted installs leave .<name>.tmp-<pid> / .<name>.backup-<pid> entries
# behind. They are safe to delete once the owning process is gone.
_ORPHAN_RE = re.compile(r"^\..+\.(tmp|backup)-(\d+)$")


def collect_runtime(config: GlobalConfig, csk_home: Path) -> None:
    referenced: set[tuple[str, str]] = set()
    _collect_markers(csk_home / "global" / "skills", referenced)
    sweep_orphans(csk_home / "global" / "skills")
    for project in config.projects.values():
        _collect_markers(project.path / ".agents" / "skills", referenced)
        sweep_orphans(project.path / ".agents" / "skills")

    # Checkouts installed without registration ('csk install .') reference
    # runtime through the consumer registry; dead entries are pruned here.
    alive: list[Path] = []
    for consumer in consumers.load_consumers(csk_home):
        if consumer.exists() and _collect_markers(consumer / ".agents" / "skills", referenced):
            alive.append(consumer)
            sweep_orphans(consumer / ".agents" / "skills")
    consumers.replace_consumers(csk_home, alive)

    runtime_root = csk_home / "runtime"
    if not runtime_root.exists():
        return
    for skill_dir in runtime_root.iterdir():
        if not skill_dir.is_dir():
            continue
        sweep_orphans(skill_dir)
        for commit_dir in skill_dir.iterdir():
            if commit_dir.is_dir() and (skill_dir.name, commit_dir.name) not in referenced:
                shutil.rmtree(commit_dir)


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


def _collect_markers(skills_root: Path, referenced: set[tuple[str, str]]) -> bool:
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
    return found
