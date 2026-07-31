from __future__ import annotations

import hashlib
import json
import os
import shutil
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

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


@dataclass(frozen=True)
class AdapterGroup:
    canonical_root: Path
    skill_names: tuple[str, ...]


@dataclass(frozen=True)
class AdapterTarget:
    target_class: str
    identifier: str
    live_path: Path
    kind: Literal["bytes", "entry"]
    desired_kind: str
    skill_name: str | None = None
    canonical_root: Path | None = None
    expected_entries: tuple[str, ...] = ()


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


def plan_project_adapter_targets(
    project_root: Path,
    agents: list[str],
    groups: list[AdapterGroup],
) -> tuple[AdapterTarget, ...]:
    expected = {
        name
        for group in groups
        for name in group.skill_names
    }
    roots = {
        agent: project_root / relative
        for agent, relative in AGENT_PATHS.items()
        if agent in agents
    }
    targets: list[AdapterTarget] = []
    for agent in sorted(roots):
        adapter_root = roots[agent]
        root_key = hashlib.sha256(
            str(adapter_root.absolute()).encode("utf-8")
        ).hexdigest()
        managed = _read_managed(adapter_root)
        for name in sorted(managed - expected):
            live = adapter_root / name
            if not live.exists() and not live.is_symlink():
                continue
            targets.append(
                AdapterTarget(
                    target_class="80-removal",
                    identifier=f"adapter/{root_key}/{name}",
                    live_path=live,
                    kind="entry",
                    desired_kind="remove",
                    skill_name=name,
                )
            )
        for group in groups:
            for name in sorted(group.skill_names):
                canonical = group.canonical_root / name
                live = adapter_root / name
                if _is_unmanaged_conflict(live, managed, canonical):
                    raise AdapterError(
                        "Adapter target already exists and is not managed by "
                        f"csk: {live}"
                    )
                targets.append(
                    AdapterTarget(
                        target_class="60-adapter-ledger",
                        identifier=f"{root_key}/entry/{name}",
                        live_path=live,
                        kind="entry",
                        desired_kind="mirror",
                        skill_name=name,
                        canonical_root=group.canonical_root,
                    )
                )
        targets.append(
            AdapterTarget(
                target_class="60-adapter-ledger",
                identifier=f"{root_key}/ledger",
                live_path=adapter_root / MANAGED_FILE,
                kind="bytes",
                desired_kind="ledger",
                expected_entries=tuple(sorted(expected)),
            )
        )
    return tuple(targets)


def stage_project_adapter_targets(
    stage_root: Path,
    targets: tuple[AdapterTarget, ...],
    *,
    source_roots: dict[Path, Path],
    mode: str,
) -> dict[tuple[str, str], Path | None]:
    desired: dict[tuple[str, str], Path | None] = {}
    for index, target in enumerate(targets):
        key = (target.target_class, target.identifier)
        if target.desired_kind == "remove":
            desired[key] = None
            continue
        staged = stage_root / f"{index:04d}"
        staged.parent.mkdir(parents=True, exist_ok=True)
        if target.desired_kind == "ledger":
            staged.write_text(
                json.dumps(
                    {
                        "schema_version": SCHEMA_VERSION,
                        "entries": list(target.expected_entries),
                    },
                    indent=2,
                    sort_keys=True,
                )
                + "\n",
                encoding="utf-8",
            )
            desired[key] = staged
            continue
        if (
            target.desired_kind != "mirror"
            or target.skill_name is None
            or target.canonical_root is None
        ):
            raise AdapterError(
                f"invalid staged adapter target: {target.identifier}"
            )
        source_root = source_roots.get(target.canonical_root)
        if source_root is None:
            raise AdapterError(
                f"adapter source root was not staged: "
                f"{target.canonical_root}"
            )
        source = source_root / target.skill_name
        if not source.exists():
            raise AdapterError(
                f"adapter source was not staged: {source}"
            )
        canonical = target.canonical_root / target.skill_name
        selected_mode = mode
        if mode == "auto":
            selected_mode = (
                "symlink"
                if _transaction_links_supported(stage_root, target.live_path)
                else "copy"
            )
        if selected_mode == "symlink":
            staged.symlink_to(
                os.path.relpath(canonical, target.live_path.parent),
                target_is_directory=True,
            )
        else:
            shutil.copytree(source, staged, symlinks=True)
        desired[key] = staged
    return desired


def _transaction_links_supported(stage_root: Path, live_path: Path) -> bool:
    live_directory = _nearest_existing_directory(live_path.parent)
    try:
        same_device = _device_id(stage_root) == _device_id(live_directory)
    except OSError:
        return False
    # Auto mode must not probe by writing into the live project before the
    # transaction commits. A successful probe on the same filesystem is the
    # capability witness; otherwise the staged adapter safely falls back to a
    # copy. Explicit symlink mode remains explicit.
    return same_device and _link_probe(stage_root)


def _device_id(path: Path) -> int:
    return path.stat().st_dev


def _nearest_existing_directory(path: Path) -> Path:
    current = path
    while True:
        try:
            current.lstat()
        except FileNotFoundError:
            parent = current.parent
            if parent == current:
                return current
            current = parent
            continue
        if current.is_symlink() or not current.is_dir():
            return current.parent
        return current


def _link_probe(directory: Path) -> bool:
    if not directory.exists() or directory.is_symlink():
        return False
    probe = directory / f".csk-symlink-probe-{os.getpid()}"
    try:
        probe.unlink(missing_ok=True)
        # Adapter entries are always directory links, and Windows treats a
        # directory link as a distinct object type, so the probe has to create
        # the same kind of link the adapter will.
        probe.symlink_to(".", target_is_directory=True)
        probe.unlink()
    except OSError:
        try:
            probe.unlink(missing_ok=True)
        except OSError:
            pass
        return False
    return True


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
    except (OSError, protocol_json.ProtocolJSONError):
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
