from __future__ import annotations

import json
import shutil
from pathlib import Path

from . import consumers
from .config import GlobalConfig


def collect_runtime(config: GlobalConfig, csk_home: Path) -> None:
    referenced: set[tuple[str, str]] = set()
    _collect_markers(csk_home / "global" / "skills", referenced)
    for project in config.projects.values():
        _collect_markers(project.path / ".agents" / "skills", referenced)

    # Checkouts installed without registration ('csk install .') reference
    # runtime through the consumer registry; dead entries are pruned here.
    alive: list[Path] = []
    for consumer in consumers.load_consumers(csk_home):
        if consumer.exists() and _collect_markers(consumer / ".agents" / "skills", referenced):
            alive.append(consumer)
    consumers.replace_consumers(csk_home, alive)

    runtime_root = csk_home / "runtime"
    if not runtime_root.exists():
        return
    for skill_dir in runtime_root.iterdir():
        if not skill_dir.is_dir():
            continue
        for commit_dir in skill_dir.iterdir():
            if commit_dir.is_dir() and (skill_dir.name, commit_dir.name) not in referenced:
                shutil.rmtree(commit_dir)


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
