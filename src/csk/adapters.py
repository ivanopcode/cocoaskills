from __future__ import annotations

import os
import json
import shutil
from pathlib import Path


AGENT_PATHS = {
    "codex_cli": ".codex/skills",
    "claude_code": ".claude/skills",
    "gemini": ".gemini/skills",
    "cursor": ".cursor/rules",
}


class AdapterError(Exception):
    pass


MANAGED_FILE = ".csk-managed.json"
SCHEMA_VERSION = 1


def required_gitignore_entries(agents: list[str]) -> list[str]:
    entries = [".agents/"]
    for agent in agents:
        rel = AGENT_PATHS.get(agent)
        if rel:
            entries.append(rel + "/")
    return sorted(set(entries))


def all_gitignore_entries() -> list[str]:
    return required_gitignore_entries(sorted(AGENT_PATHS))


def refresh_adapters(project_root: Path, agents: list[str], skill_names: list[str], mode: str) -> None:
    canonical_root = project_root / ".agents" / "skills"
    for agent in agents:
        rel = AGENT_PATHS.get(agent)
        if not rel:
            continue
        adapter_root = project_root / rel
        adapter_root.mkdir(parents=True, exist_ok=True)
        expected = set(skill_names)
        managed = _read_managed(adapter_root)
        for name in managed - expected:
            child = adapter_root / name
            if child.exists() or child.is_symlink():
                _remove_path(child)
        for skill_name in skill_names:
            source = canonical_root / skill_name
            target = adapter_root / skill_name
            if not source.exists():
                continue
            if _is_unmanaged_conflict(target, managed, source):
                raise AdapterError(f"Adapter target already exists and is not managed by csk: {target}")
            _refresh_entry(source, target, mode)
        _write_managed(adapter_root, expected)


def _refresh_entry(source: Path, target: Path, mode: str) -> None:
    if mode == "copy":
        if target.exists() or target.is_symlink():
            _remove_path(target)
        shutil.copytree(source, target, symlinks=True)
        return
    if mode == "symlink":
        if target.exists() or target.is_symlink():
            _remove_path(target)
        target.symlink_to(os.path.relpath(source, target.parent), target_is_directory=True)
        return
    # auto
    try:
        if target.exists() or target.is_symlink():
            _remove_path(target)
        target.symlink_to(os.path.relpath(source, target.parent), target_is_directory=True)
    except OSError:
        if target.exists() or target.is_symlink():
            _remove_path(target)
        shutil.copytree(source, target, symlinks=True)


def _remove_path(path: Path) -> None:
    if path.is_symlink() or path.is_file():
        path.unlink()
    elif path.exists():
        shutil.rmtree(path)


def _read_managed(adapter_root: Path) -> set[str]:
    path = adapter_root / MANAGED_FILE
    if not path.exists():
        return set()
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return set()
    if not isinstance(data, dict) or data.get("schema_version") != SCHEMA_VERSION:
        return set()
    entries = data.get("entries", [])
    if not isinstance(entries, list):
        return set()
    return {entry for entry in entries if isinstance(entry, str)}


def _write_managed(adapter_root: Path, entries: set[str]) -> None:
    path = adapter_root / MANAGED_FILE
    data = {"schema_version": SCHEMA_VERSION, "entries": sorted(entries)}
    path.write_text(json.dumps(data, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def _is_unmanaged_conflict(target: Path, managed: set[str], source: Path) -> bool:
    if not target.exists() and not target.is_symlink():
        return False
    if target.name in managed:
        return False
    if target.is_symlink():
        try:
            return target.resolve() != source.resolve()
        except OSError:
            return True
    return not (target / ".csk-install.json").exists()
