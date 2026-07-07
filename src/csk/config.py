from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .audit import backend_config
from .audit.source_policy import SourcePolicy, SourcePolicyError, parse_source_policy


SCHEMA_VERSION = 1
DEFAULT_CONFIG_PATH = Path.home() / ".cocoaskills" / "config.json"
DEFAULT_AGENTS = ["codex_cli"]
DEFAULT_WORKTREE_ALIAS_PATTERN = r"[A-Z]+-[0-9]+"
AUDIT_REVOCATION_HASH_RE = re.compile(r"^(?:sha256:)?[A-Fa-f0-9]{64}$")


class ConfigError(Exception):
    pass


@dataclass(frozen=True)
class ProjectConfig:
    alias: str
    path: Path
    agents: list[str] = field(default_factory=list)
    project_alias: str | None = None
    checkout_alias: str | None = None


# Built-in trusted registries shipped with a release. Empty until the central
# registry is deployed and its public key is generated; a real pinned key ships
# with the release that turns this on (RFC 0008).
BUILTIN_REGISTRIES: tuple["RegistryConfig", ...] = ()


@dataclass(frozen=True)
class RegistryConfig:
    name: str
    url: str
    public_keys: tuple[str, ...] = ()
    enabled: bool = True


@dataclass(frozen=True)
class AuditConfig:
    enabled: bool = False
    mode: str = "advisory"
    fail_on: str = "high"
    backend: str = "null"
    model: str | None = None
    allow_cloud: bool = False
    max_request_bytes: int = backend_config.DEFAULT_MAX_REQUEST_BYTES
    backends: dict[str, Any] = field(default_factory=dict)
    grants: list[dict[str, Any]] = field(default_factory=list)
    revocations: list[str] = field(default_factory=list)
    source_policy: SourcePolicy = field(default_factory=SourcePolicy)


@dataclass(frozen=True)
class GlobalConfig:
    path: Path
    skills_root: Path
    preferred_locale: str | None
    default_agents: list[str]
    adapter_mode: str
    worktree_alias_pattern: str
    projects: dict[str, ProjectConfig]
    audit: AuditConfig = field(default_factory=AuditConfig)
    # Canonical "host/path" prefixes the resolver may fetch from. An empty
    # tuple allows every source; organizations pin the list to their hosting.
    allowed_sources: tuple[str, ...] = ()
    # Trusted audit registries, built-in defaults merged with configured
    # entries unless the defaults are disabled (RFC 0008).
    audit_registries: tuple[RegistryConfig, ...] = ()
    disable_builtin_registries: bool = False

    def trusted_registries(self) -> tuple[RegistryConfig, ...]:
        """Effective registries: built-in defaults plus configured entries.

        A configured entry with the same url overrides a built-in one. The
        built-in defaults are dropped when disable_builtin_registries is set.
        """
        by_url: dict[str, RegistryConfig] = {}
        if not self.disable_builtin_registries:
            for entry in BUILTIN_REGISTRIES:
                by_url[entry.url] = entry
        for entry in self.audit_registries:
            by_url[entry.url] = entry
        return tuple(entry for entry in by_url.values() if entry.enabled)


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
    if schema is None:
        raise ConfigError("Global config is missing required field 'schema_version'")
    if not isinstance(schema, int) or isinstance(schema, bool):
        raise ConfigError(f"Global config field 'schema_version' must be an integer, got {schema!r}")
    if schema != SCHEMA_VERSION:
        raise ConfigError(
            f"Unsupported config schema_version {schema}; this config requires a newer csk"
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

    audit = _parse_audit_config(data.get("audit"))

    allowed_sources_raw = data.get("allowed_sources", [])
    if not _is_str_list(allowed_sources_raw) or any(not item.strip() for item in allowed_sources_raw):
        raise ConfigError("Global config field 'allowed_sources' must be a list of non-empty strings")

    audit_registries = _parse_audit_registries(data.get("audit_registries"))
    disable_builtin = data.get("disable_builtin_registries", False)
    if not isinstance(disable_builtin, bool):
        raise ConfigError("Global config field 'disable_builtin_registries' must be a boolean")

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
        audit=audit,
        allowed_sources=tuple(allowed_sources_raw),
        audit_registries=audit_registries,
        disable_builtin_registries=disable_builtin,
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
    audit = _serialize_audit_config(config.audit)
    if audit:
        data["audit"] = audit
    if config.allowed_sources:
        data["allowed_sources"] = list(config.allowed_sources)
    if config.audit_registries:
        data["audit_registries"] = [
            {
                "name": entry.name,
                "url": entry.url,
                "public_keys": list(entry.public_keys),
                "enabled": entry.enabled,
            }
            for entry in config.audit_registries
        ]
    if config.disable_builtin_registries:
        data["disable_builtin_registries"] = True
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
        audit=config.audit,
        allowed_sources=config.allowed_sources,
        audit_registries=config.audit_registries,
        disable_builtin_registries=config.disable_builtin_registries,
    )


def validate_skills_root_for_work(config: GlobalConfig) -> None:
    if config.skills_root.exists() and not config.skills_root.is_dir():
        raise ConfigError(f"skills_root exists but is not a directory: {config.skills_root}")
    config.skills_root.mkdir(parents=True, exist_ok=True)


def _is_str_list(value: Any) -> bool:
    return isinstance(value, list) and all(isinstance(item, str) for item in value)


def _parse_audit_registries(raw: Any) -> tuple[RegistryConfig, ...]:
    if raw is None:
        return ()
    if not isinstance(raw, list):
        raise ConfigError("Global config field 'audit_registries' must be a list")
    registries: list[RegistryConfig] = []
    seen: set[str] = set()
    for index, item in enumerate(raw):
        if not isinstance(item, dict):
            raise ConfigError(f"audit_registries[{index}] must be an object")
        name = item.get("name")
        if not isinstance(name, str) or not name:
            raise ConfigError(f"audit_registries[{index}] requires a non-empty string 'name'")
        url = item.get("url")
        if not isinstance(url, str) or not url.startswith(("http://", "https://")):
            raise ConfigError(f"audit_registries[{index}] requires an http(s) 'url'")
        if url in seen:
            raise ConfigError(f"audit_registries[{index}] duplicates url {url!r}")
        seen.add(url)
        keys = item.get("public_keys", [])
        if not _is_str_list(keys) or any(not key.strip() for key in keys):
            raise ConfigError(f"audit_registries[{index}].public_keys must be a list of non-empty strings")
        enabled = item.get("enabled", True)
        if not isinstance(enabled, bool):
            raise ConfigError(f"audit_registries[{index}].enabled must be a boolean")
        registries.append(RegistryConfig(name=name, url=url, public_keys=tuple(keys), enabled=enabled))
    return tuple(registries)


def _parse_audit_config(raw: Any) -> AuditConfig:
    if raw is None:
        return AuditConfig()
    if not isinstance(raw, dict):
        raise ConfigError("Global config field 'audit' must be an object")
    _reject_unknown_fields(
        raw,
        {
            "enabled",
            "mode",
            "fail_on",
            "backend",
            "model",
            "allow_cloud",
            "max_request_bytes",
            "backends",
            "grants",
            "revocations",
            "source_policy",
        },
        "audit",
    )

    enabled = raw.get("enabled", False)
    if not isinstance(enabled, bool):
        raise ConfigError("Global config field 'audit.enabled' must be a boolean")

    mode = raw.get("mode", "advisory")
    if mode not in {"advisory", "strict"}:
        raise ConfigError("Global config field 'audit.mode' must be advisory or strict")

    fail_on = raw.get("fail_on", "high")
    if fail_on not in {"off", "low", "medium", "high", "critical"}:
        raise ConfigError("Global config field 'audit.fail_on' must be off, low, medium, high, or critical")

    backend = raw.get("backend", "null")
    if not isinstance(backend, str) or not backend:
        raise ConfigError("Global config field 'audit.backend' must be a non-empty string")

    model = raw.get("model")
    if model is not None and (not isinstance(model, str) or not model):
        raise ConfigError("Global config field 'audit.model' must be a non-empty string when present")

    allow_cloud = raw.get("allow_cloud", False)
    if not isinstance(allow_cloud, bool):
        raise ConfigError("Global config field 'audit.allow_cloud' must be a boolean")

    max_request_bytes = raw.get("max_request_bytes", backend_config.DEFAULT_MAX_REQUEST_BYTES)
    if (
        isinstance(max_request_bytes, bool)
        or not isinstance(max_request_bytes, int)
        or max_request_bytes < 1
        or max_request_bytes > backend_config.MAX_MAX_REQUEST_BYTES
    ):
        raise ConfigError(
            "Global config field 'audit.max_request_bytes' must be an integer "
            f"between 1 and {backend_config.MAX_MAX_REQUEST_BYTES}"
        )

    backends = raw.get("backends", {})
    if not isinstance(backends, dict):
        raise ConfigError("Global config field 'audit.backends' must be an object")
    try:
        backend_config.parse_backend_configs(backends, global_model=model, allow_cloud=allow_cloud)
        backend_config.resolve_backend_config(backend, backends, global_model=model, allow_cloud=allow_cloud)
    except backend_config.BackendConfigError as exc:
        raise ConfigError(str(exc)) from exc

    grants = raw.get("grants", [])
    if not isinstance(grants, list) or not all(isinstance(item, dict) for item in grants):
        raise ConfigError("Global config field 'audit.grants' must be a list of objects")

    revocations = raw.get("revocations", [])
    if not _is_str_list(revocations):
        raise ConfigError("Global config field 'audit.revocations' must be a list of strings")
    for index, item in enumerate(revocations):
        _validate_audit_revocation(item, f"audit.revocations[{index}]")

    try:
        source_policy = parse_source_policy(raw.get("source_policy"))
    except SourcePolicyError as exc:
        raise ConfigError(str(exc)) from exc

    return AuditConfig(
        enabled=enabled,
        mode=mode,
        fail_on=fail_on,
        backend=backend,
        model=model,
        allow_cloud=allow_cloud,
        max_request_bytes=max_request_bytes,
        backends=dict(backends),
        grants=list(grants),
        revocations=list(revocations),
        source_policy=source_policy,
    )


def _serialize_audit_config(audit: AuditConfig) -> dict[str, Any]:
    data: dict[str, Any] = {}
    if audit.enabled:
        data["enabled"] = audit.enabled
    if audit.mode != "advisory":
        data["mode"] = audit.mode
    if audit.fail_on != "high":
        data["fail_on"] = audit.fail_on
    if audit.backend != "null":
        data["backend"] = audit.backend
    if audit.model is not None:
        data["model"] = audit.model
    if audit.allow_cloud:
        data["allow_cloud"] = audit.allow_cloud
    if audit.max_request_bytes != backend_config.DEFAULT_MAX_REQUEST_BYTES:
        data["max_request_bytes"] = audit.max_request_bytes
    if audit.backends:
        data["backends"] = audit.backends
    if audit.grants:
        data["grants"] = audit.grants
    if audit.revocations:
        data["revocations"] = audit.revocations
    source_policy = _serialize_source_policy(audit.source_policy)
    if source_policy:
        data["source_policy"] = source_policy
    return data


def _serialize_source_policy(source_policy: SourcePolicy) -> dict[str, Any]:
    data: dict[str, Any] = {}
    if source_policy.default_class != "internal":
        data["default_class"] = source_policy.default_class
    if source_policy.rules:
        data["rules"] = [
            {"pattern": rule.pattern, "class": rule.source_class}
            for rule in source_policy.rules
        ]
    return data


def _validate_audit_revocation(value: str, field: str) -> None:
    if AUDIT_REVOCATION_HASH_RE.fullmatch(value):
        return
    if value.startswith("source:") and value.removeprefix("source:").strip():
        return
    raise ConfigError(f"Global config field '{field}' must be a SHA256 hash or source:<pattern>")


def _reject_unknown_fields(data: dict[str, Any], allowed: set[str], label: str) -> None:
    unknown = sorted(set(data) - allowed)
    if unknown:
        joined = ", ".join(repr(item) for item in unknown)
        raise ConfigError(f"Global config field '{label}' has unsupported field(s): {joined}")
