from __future__ import annotations

import json
import os
import shutil
import sys
from pathlib import Path

from . import protocol_json
from .identifiers import is_valid_identifier


AGENT_PATHS = {
    "codex_cli": ".codex/skills",
    "claude_code": ".claude/skills",
    "gemini": ".gemini/skills",
    "cursor": ".cursor/rules",
}

# Agents that discover the canonical .agents/skills/ directory natively.
# They need no project-level mirror; global installs are mirrored into
# ~/.agents/skills so these agents see them outside any project checkout.
NATIVE_DISCOVERY_AGENTS = frozenset({"windsurf", "opencode"})
NATIVE_DISCOVERY_HOME_PATH = ".agents/skills"


def known_agents() -> set[str]:
    return set(AGENT_PATHS) | set(NATIVE_DISCOVERY_AGENTS)


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


def warn_unknown_agents(agents: list[str]) -> None:
    unknown = sorted({agent for agent in agents if agent not in known_agents()})
    if unknown:
        print(
            "warning: unknown agent(s) ignored: "
            + ", ".join(unknown)
            + "; known agents: "
            + ", ".join(sorted(known_agents())),
            file=sys.stderr,
        )


def refresh_adapters(project_root: Path, agents: list[str], skill_names: list[str], mode: str) -> None:
    refresh_adapter_groups(project_root, agents, [(project_root / ".agents" / "skills", skill_names)], mode)


def refresh_adapter_groups(
    project_root: Path,
    agents: list[str],
    groups: list[tuple[Path, list[str]]],
    mode: str,
) -> None:
    """Mirror skills from several canonical roots into the agent directories.

    All groups share one managed-entries ledger per adapter root, so entries
    that fall out of every group are removed in the same pass.
    """
    warn_unknown_agents(agents)
    adapter_roots = {
        agent: project_root / rel
        for agent, rel in AGENT_PATHS.items()
    }
    _refresh_adapter_groups(adapter_roots, agents, groups, mode)


def refresh_global_adapters(
    csk_home: Path,
    agents: list[str],
    skill_names: list[str],
    mode: str,
    *,
    home: Path | None = None,
) -> None:
    canonical_root = csk_home / "global" / "skills"
    user_home = home or Path.home()
    adapter_roots = {
        agent: user_home / rel
        for agent, rel in AGENT_PATHS.items()
    }
    for agent in NATIVE_DISCOVERY_AGENTS:
        adapter_roots[agent] = user_home / NATIVE_DISCOVERY_HOME_PATH
    _refresh_adapters(canonical_root, adapter_roots, agents, skill_names, mode)


def _refresh_adapters(
    canonical_root: Path,
    adapter_roots: dict[str, Path],
    agents: list[str],
    skill_names: list[str],
    mode: str,
) -> None:
    _refresh_adapter_groups(adapter_roots, agents, [(canonical_root, skill_names)], mode)


def _refresh_adapter_groups(
    adapter_roots: dict[str, Path],
    agents: list[str],
    groups: list[tuple[Path, list[str]]],
    mode: str,
) -> None:
    expected: set[str] = set()
    for _, names in groups:
        expected.update(names)
    for agent in agents:
        adapter_root = adapter_roots.get(agent)
        if adapter_root is None:
            continue
        adapter_root.mkdir(parents=True, exist_ok=True)
        managed = _read_managed(adapter_root)
        for name in managed - expected:
            child = adapter_root / name
            if child.exists() or child.is_symlink():
                _remove_path(child)
        for canonical_root, skill_names in groups:
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
        data = protocol_json.loads(path.read_bytes())
    except Exception:
        return set()
    if (
        not isinstance(data, dict)
        or set(data) != {"schema_version", "entries"}
        or data.get("schema_version") != SCHEMA_VERSION
    ):
        return set()
    entries = data["entries"]
    if (
        not isinstance(entries, list)
        or any(not isinstance(entry, str) or not is_valid_identifier(entry) for entry in entries)
        or len(entries) != len(set(entries))
    ):
        return set()
    return set(entries)


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
