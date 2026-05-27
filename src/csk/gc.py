from __future__ import annotations

import json
import shutil
from pathlib import Path

from .config import GlobalConfig


def collect_runtime(config: GlobalConfig, csk_home: Path) -> None:
    referenced: set[tuple[str, str]] = set()
    _collect_markers(csk_home / "global" / "skills", referenced)
    for project in config.projects.values():
        _collect_markers(project.path / ".agents" / "skills", referenced)

    runtime_root = csk_home / "runtime"
    if not runtime_root.exists():
        return
    for skill_dir in runtime_root.iterdir():
        if not skill_dir.is_dir():
            continue
        for commit_dir in skill_dir.iterdir():
            if commit_dir.is_dir() and (skill_dir.name, commit_dir.name) not in referenced:
                shutil.rmtree(commit_dir)


def _collect_markers(skills_root: Path, referenced: set[tuple[str, str]]) -> None:
    if not skills_root.exists():
        return
    for marker in skills_root.glob("*/.csk-install.json"):
        try:
            data = json.loads(marker.read_text(encoding="utf-8"))
        except Exception:
            continue
        name = data.get("name")
        commit = data.get("commit")
        if isinstance(name, str) and isinstance(commit, str):
            referenced.add((name, commit))
