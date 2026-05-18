from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


SCHEMA_VERSION = 1
DEFAULT_CONFIG_PATH = Path.home() / ".cocoaskills" / "config.json"
DEFAULT_AGENTS = ["codex_cli"]
DEFAULT_WORKTREE_ALIAS_PATTERN = r"[A-Z]+-[0-9]+"


class ConfigError(Exception):
    pass


@dataclass(frozen=True)
class ProjectConfig:
    alias: str
    path: Path
    agents: list[str] = field(default_factory=list)
    project_alias: str | None = None
    checkout_alias: str | None = None


@dataclass(frozen=True)
class GlobalConfig:
    path: Path
    skills_root: Path
    preferred_locale: str | None
    default_agents: list[str]
    adapter_mode: str
    worktree_alias_pattern: str
    projects: dict[str, ProjectConfig]


def config_path() -> Path:
    override = os.environ.get("CSK_CONFIG")
    if override:
        return Path(override).expanduser()
    return DEFAULT_CONFIG_PATH


def load_config(path: Path | None = None) -> GlobalConfig:
    resolved = path or config_path()
    try:
        data = json.loads(resolved.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise ConfigError(f"Global config not found: {resolved}") from exc
    except json.JSONDecodeError as exc:
        raise ConfigError(f"Malformed JSON in global config {resolved}: {exc}") from exc

    return parse_config(data, resolved)


def parse_config(data: dict[str, Any], path: Path) -> GlobalConfig:
    if not isinstance(data, dict):
        raise ConfigError("Global config must be a JSON object")
    schema = data.get("schema_version")
    if schema != SCHEMA_VERSION:
        raise ConfigError(
            f"Unsupported config schema_version {schema!r}; this config requires a newer csk"
        )

    skills_root_raw = data.get("skills_root")
    if not isinstance(skills_root_raw, str) or not skills_root_raw:
        raise ConfigError("Global config requires non-empty string field 'skills_root'")

    default_agents = data.get("default_agents", DEFAULT_AGENTS)
    if not _is_str_list(default_agents):
        raise ConfigError("Global config field 'default_agents' must be a list of strings")

    preferred_locale = data.get("preferred_locale")
    if preferred_locale is not None and not isinstance(preferred_locale, str):
        raise ConfigError("Global config field 'preferred_locale' must be a string when present")

    adapter_mode = data.get("adapter_mode", "auto")
    if adapter_mode not in {"auto", "symlink", "copy"}:
        raise ConfigError("Global config field 'adapter_mode' must be auto, symlink, or copy")

    worktree_alias_pattern = data.get("worktree_alias_pattern", DEFAULT_WORKTREE_ALIAS_PATTERN)
    if not isinstance(worktree_alias_pattern, str) or not worktree_alias_pattern:
        raise ConfigError("Global config field 'worktree_alias_pattern' must be a non-empty string")
    try:
        re.compile(worktree_alias_pattern)
    except re.error as exc:
        raise ConfigError(f"Global config field 'worktree_alias_pattern' is not a valid regex: {exc}") from exc

    if "projects" not in data:
        raise ConfigError("Global config requires field 'projects'")
    projects_raw = data.get("projects")
    if not isinstance(projects_raw, dict):
        raise ConfigError("Global config field 'projects' must be an object")

    projects: dict[str, ProjectConfig] = {}
    for alias, raw in projects_raw.items():
        if not isinstance(alias, str) or not alias:
            raise ConfigError("Project alias must be a non-empty string")
        if not isinstance(raw, dict):
            raise ConfigError(f"Project {alias!r} config must be an object")
        project_path = raw.get("path")
        if not isinstance(project_path, str) or not project_path:
            raise ConfigError(f"Project {alias!r} requires non-empty string field 'path'")
        agents = raw.get("agents", [])
        if not _is_str_list(agents):
            raise ConfigError(f"Project {alias!r} field 'agents' must be a list of strings")
        project_alias = raw.get("project_alias", alias)
        if project_alias is not None and (not isinstance(project_alias, str) or not project_alias):
            raise ConfigError(f"Project {alias!r} field 'project_alias' must be a non-empty string when present")
        checkout_alias = raw.get("checkout_alias", alias)
        if checkout_alias is not None and (not isinstance(checkout_alias, str) or not checkout_alias):
            raise ConfigError(f"Project {alias!r} field 'checkout_alias' must be a non-empty string when present")
        projects[alias] = ProjectConfig(
            alias=alias,
            path=Path(project_path).expanduser(),
            agents=list(agents),
            project_alias=project_alias,
            checkout_alias=checkout_alias,
        )

    return GlobalConfig(
        path=path,
        skills_root=Path(skills_root_raw).expanduser(),
        preferred_locale=preferred_locale,
        default_agents=list(default_agents),
        adapter_mode=adapter_mode,
        worktree_alias_pattern=worktree_alias_pattern,
        projects=projects,
    )


def save_config(config: GlobalConfig) -> None:
    config.path.parent.mkdir(parents=True, exist_ok=True)
    data = {
        "schema_version": SCHEMA_VERSION,
        "skills_root": str(config.skills_root),
        "preferred_locale": config.preferred_locale,
        "default_agents": config.default_agents,
        "adapter_mode": config.adapter_mode,
        "worktree_alias_pattern": config.worktree_alias_pattern,
        "projects": {
            alias: {
                "path": str(project.path),
                "agents": project.agents,
                "project_alias": project.project_alias or alias,
                "checkout_alias": project.checkout_alias or alias,
            }
            for alias, project in config.projects.items()
        },
    }
    config.path.write_text(json.dumps(data, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def add_project(
    config: GlobalConfig,
    alias: str,
    path: Path,
    agents: list[str] | None = None,
    *,
    project_alias: str | None = None,
    checkout_alias: str | None = None,
) -> GlobalConfig:
    if not alias:
        raise ConfigError("Project alias must be non-empty")
    projects = dict(config.projects)
    projects[alias] = ProjectConfig(
        alias=alias,
        path=path.expanduser(),
        agents=list(agents if agents is not None else config.default_agents),
        project_alias=project_alias or alias,
        checkout_alias=checkout_alias or alias,
    )
    return GlobalConfig(
        path=config.path,
        skills_root=config.skills_root,
        preferred_locale=config.preferred_locale,
        default_agents=list(config.default_agents),
        adapter_mode=config.adapter_mode,
        worktree_alias_pattern=config.worktree_alias_pattern,
        projects=projects,
    )


def validate_skills_root_for_work(config: GlobalConfig) -> None:
    if config.skills_root.exists() and not config.skills_root.is_dir():
        raise ConfigError(f"skills_root exists but is not a directory: {config.skills_root}")
    config.skills_root.mkdir(parents=True, exist_ok=True)


def _is_str_list(value: Any) -> bool:
    return isinstance(value, list) and all(isinstance(item, str) for item in value)
