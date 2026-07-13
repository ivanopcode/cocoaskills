from __future__ import annotations

import json
import ipaddress
import os
import re
import sys
import tempfile
import unicodedata
import urllib.parse
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from . import identifiers, protocol_json
from .audit import backend_config
from .audit.source_policy import SourcePolicy, SourcePolicyError, parse_source_policy


SCHEMA_VERSION = 1
DEFAULT_CONFIG_PATH = Path.home() / ".cocoaskills" / "config.json"
# Enforced machine configuration read before the user config. An organization
# distributes this file through device management; keys it lists under 'locked'
# cannot be overridden from the user config (RFC 0008 section 10).
if os.name == "nt":
    DEFAULT_SYSTEM_CONFIG_PATH = Path(os.environ.get("ProgramData", "C:\\ProgramData")) / "cocoaskills" / "config.json"
else:
    DEFAULT_SYSTEM_CONFIG_PATH = Path("/etc/cocoaskills/config.json")
# Top-level keys an organization may lock from the system config.
LOCKABLE_KEYS = frozenset(
    {"audit_registries", "disable_builtin_registries", "allowed_sources", "audit"}
)
DEFAULT_AGENTS = ["codex_cli"]
DEFAULT_WORKTREE_ALIAS_PATTERN = r"[A-Z]+-[0-9]+"
AUDIT_REVOCATION_HASH_RE = re.compile(r"^(?:sha256:)?[A-Fa-f0-9]{64}$")
PINNED_KEY_RE = re.compile(r"^(?:ed25519:)?[A-Za-z0-9+/]{43}=$")
REGISTRY_HOST_RE = re.compile(
    r"^[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?"
    r"(?:\.[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?)*$"
)
DEFAULT_SNAPSHOT_MAX_AGE_SECONDS = 604_800
DEFAULT_SNAPSHOT_CLOCK_SKEW_SECONDS = 300
DEFAULT_CACHE_TTL_SECONDS = 3_600
DEFAULT_OFFLINE_GRACE_SECONDS = 604_800
MAX_DURATION_SECONDS = 2_147_483_647

MANAGER_KEYS = frozenset(
    {
        "schema_version",
        "skills_root",
        "default_agents",
        "preferred_locale",
        "adapter_mode",
        "worktree_alias_pattern",
        "projects",
        "allowed_sources",
        "audit",
        "audit_registries",
        "disable_builtin_registries",
    }
)


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
    # How registry lookups affect the install: 'advisory' warns on an unknown
    # or unreachable artifact, 'strict' fails it. A revocation always denies.
    registry_policy: str = "advisory"
    snapshot_max_age_seconds: int = DEFAULT_SNAPSHOT_MAX_AGE_SECONDS
    snapshot_clock_skew_seconds: int = DEFAULT_SNAPSHOT_CLOCK_SKEW_SECONDS
    cache_ttl_seconds: int = DEFAULT_CACHE_TTL_SECONDS
    offline_grace_seconds: int = DEFAULT_OFFLINE_GRACE_SECONDS


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


def system_config_path() -> Path | None:
    override = os.environ.get("CSK_SYSTEM_CONFIG")
    if override:
        return Path(override).expanduser()
    if DEFAULT_SYSTEM_CONFIG_PATH.exists():
        return DEFAULT_SYSTEM_CONFIG_PATH
    return None


def load_config(path: Path | None = None) -> GlobalConfig:
    resolved = path or config_path()
    try:
        user_data = protocol_json.loads(resolved.read_bytes())
    except FileNotFoundError as exc:
        raise ConfigError(f"Global config not found: {resolved}") from exc
    except protocol_json.ProtocolJSONError as exc:
        raise ConfigError(f"Malformed JSON in global config {resolved}: {exc}") from exc
    if not isinstance(user_data, dict):
        raise ConfigError("Global config must be a JSON object")

    system_path = system_config_path()
    if system_path is not None:
        try:
            system_data = protocol_json.loads(system_path.read_bytes())
        except FileNotFoundError:
            system_data = None
        except protocol_json.ProtocolJSONError as exc:
            raise ConfigError(f"Malformed JSON in system config {system_path}: {exc}") from exc
        if system_data is not None:
            if not isinstance(system_data, dict):
                raise ConfigError(f"System config {system_path} must be a JSON object")
            user_data = _apply_system_config(system_data, user_data, system_path)

    return parse_config(user_data, resolved)


def _apply_system_config(
    system_data: dict[str, Any], user_data: dict[str, Any], system_path: Path
) -> dict[str, Any]:
    """Overlay the system config, enforcing its locked keys over the user config.

    A key listed under 'locked' takes its value from the system config, and a
    user override of that key is ignored with a warning. Other system keys act
    as defaults the user config may override.
    """
    schema = system_data.get("schema_version")
    if not isinstance(schema, int) or isinstance(schema, bool) or schema != SCHEMA_VERSION:
        raise ConfigError(f"System config {system_path} requires schema_version 1")
    unknown = sorted(set(system_data) - MANAGER_KEYS - {"locked"})
    if unknown:
        joined = ", ".join(repr(item) for item in unknown)
        raise ConfigError(f"System config {system_path} has unsupported field(s): {joined}")
    locked_raw = system_data.get("locked", [])
    if not _is_str_list(locked_raw):
        raise ConfigError(f"System config {system_path} field 'locked' must be a list of strings")
    if len(set(locked_raw)) != len(locked_raw):
        raise ConfigError(f"System config {system_path} field 'locked' must not contain duplicates")
    unsupported_locks = sorted(set(locked_raw) - LOCKABLE_KEYS)
    if unsupported_locks:
        raise ConfigError(f"System config {system_path} cannot lock {unsupported_locks[0]!r}")
    locked = set(locked_raw)
    merged = dict(user_data)
    for key, value in system_data.items():
        if key in {"locked", "schema_version"}:
            continue
        if key in locked:
            if key in user_data and user_data[key] != value:
                print(
                    f"warning: config key {key!r} is locked by {system_path}; "
                    "the user override is ignored",
                    file=sys.stderr,
                )
            merged[key] = value
        else:
            merged.setdefault(key, value)
    for key in locked:
        if key not in system_data:
            raise ConfigError(f"System config {system_path} locks {key!r} but does not set it")
    return merged


def parse_config(data: dict[str, Any], path: Path) -> GlobalConfig:
    if not isinstance(data, dict):
        raise ConfigError("Global config must be a JSON object")
    _reject_unknown_fields(data, set(MANAGER_KEYS), "top level")
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
    if not isinstance(skills_root_raw, str) or not skills_root_raw or len(skills_root_raw) > 4096:
        raise ConfigError("Global config requires non-empty string field 'skills_root'")

    default_agents = data.get("default_agents", DEFAULT_AGENTS)
    default_agents = _identifier_list(default_agents, "default_agents")

    preferred_locale = data.get("preferred_locale")
    if preferred_locale is not None and (
        not isinstance(preferred_locale, str) or not identifiers.is_valid_locale(preferred_locale)
    ):
        raise ConfigError(
            "Global config field 'preferred_locale' must be null or a 1-64 character ASCII locale selector"
        )

    adapter_mode = data.get("adapter_mode", "auto")
    if adapter_mode not in {"auto", "symlink", "copy"}:
        raise ConfigError("Global config field 'adapter_mode' must be auto, symlink, or copy")

    worktree_alias_pattern = data.get("worktree_alias_pattern", DEFAULT_WORKTREE_ALIAS_PATTERN)
    if (
        not isinstance(worktree_alias_pattern, str)
        or not worktree_alias_pattern
        or len(worktree_alias_pattern) > 1024
    ):
        raise ConfigError("Global config field 'worktree_alias_pattern' must be a non-empty string")
    try:
        re.compile(worktree_alias_pattern)
    except re.error as exc:
        raise ConfigError(f"Global config field 'worktree_alias_pattern' is not a valid regex: {exc}") from exc

    audit = _parse_audit_config(data.get("audit"))

    allowed_sources_raw = data.get("allowed_sources", [])
    if (
        not _is_str_list(allowed_sources_raw)
        or any(not item.strip() or len(item) > 4096 for item in allowed_sources_raw)
        or len(set(allowed_sources_raw)) != len(allowed_sources_raw)
    ):
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
        if not isinstance(alias, str) or not identifiers.is_valid_identifier(alias):
            raise ConfigError(f"Project alias {alias!r} must be a portable identifier")
        if not isinstance(raw, dict):
            raise ConfigError(f"Project {alias!r} config must be an object")
        _reject_unknown_fields(
            raw,
            {"path", "agents", "project_alias", "checkout_alias"},
            f"projects.{alias}",
        )
        project_path = raw.get("path")
        if not isinstance(project_path, str) or not project_path or len(project_path) > 4096:
            raise ConfigError(f"Project {alias!r} requires non-empty string field 'path'")
        agents = raw.get("agents", [])
        agents = _identifier_list(agents, f"projects.{alias}.agents")
        project_alias = raw.get("project_alias", alias)
        if project_alias is None:
            project_alias = alias
        if not isinstance(project_alias, str) or not identifiers.is_valid_identifier(project_alias):
            raise ConfigError(f"Project {alias!r} field 'project_alias' must be a portable identifier when present")
        checkout_alias = raw.get("checkout_alias", alias)
        if checkout_alias is None:
            checkout_alias = alias
        if not isinstance(checkout_alias, str) or not identifiers.is_valid_identifier(checkout_alias):
            raise ConfigError(f"Project {alias!r} field 'checkout_alias' must be a portable identifier when present")
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
    _write_json_atomic(config.path, data)


def _write_json_atomic(path: Path, data: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(data, indent=2, sort_keys=True) + "\n"
    descriptor, temporary_name = tempfile.mkstemp(prefix=".config-", suffix=".tmp", dir=path.parent)
    temporary = Path(temporary_name)
    try:
        try:
            os.fchmod(descriptor, 0o600)
        except (AttributeError, OSError):
            pass
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as handle:
            descriptor = -1
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        temporary.unlink(missing_ok=True)


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


def _identifier_list(value: Any, field: str) -> list[str]:
    if (
        not _is_str_list(value)
        or any(not identifiers.is_valid_identifier(item) for item in value)
        or len(set(value)) != len(value)
    ):
        raise ConfigError(f"Global config field '{field}' must contain unique portable identifiers")
    return list(value)


def _bounded_integer(value: Any, field: str, *, minimum: int, maximum: int) -> int:
    if (
        isinstance(value, bool)
        or not isinstance(value, int)
        or value < minimum
        or value > maximum
    ):
        raise ConfigError(
            f"Global config field '{field}' must be an integer between {minimum} and {maximum}"
        )
    return value


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
        _reject_unknown_fields(
            item,
            {"name", "url", "public_keys", "enabled"},
            f"audit_registries[{index}]",
        )
        name = item.get("name")
        if not isinstance(name, str) or not identifiers.is_valid_identifier(name):
            raise ConfigError(f"audit_registries[{index}].name must be a portable identifier")
        raw_url = item.get("url")
        if not isinstance(raw_url, str):
            raise ConfigError(f"audit_registries[{index}] requires an http(s) 'url'")
        url = canonical_registry_url(raw_url, field=f"audit_registries[{index}].url")
        if url in seen:
            raise ConfigError(f"audit_registries[{index}] duplicates url {url!r}")
        seen.add(url)
        keys = item.get("public_keys", [])
        if (
            not _is_str_list(keys)
            or any(PINNED_KEY_RE.fullmatch(key) is None for key in keys)
            or len(set(keys)) != len(keys)
        ):
            raise ConfigError(
                f"audit_registries[{index}].public_keys must contain unique canonical Ed25519 public keys"
            )
        enabled = item.get("enabled", True)
        if not isinstance(enabled, bool):
            raise ConfigError(f"audit_registries[{index}].enabled must be a boolean")
        registries.append(RegistryConfig(name=name, url=url, public_keys=tuple(keys), enabled=enabled))
    return tuple(registries)


def canonical_registry_url(value: str, *, field: str = "registry URL") -> str:
    if not value or len(value) > 4096:
        raise ConfigError(f"{field} must contain at most 4096 characters")
    try:
        parsed = urllib.parse.urlsplit(value)
        port = parsed.port
    except ValueError as exc:
        raise ConfigError(f"{field} is malformed") from exc
    scheme = parsed.scheme.lower()
    host = (parsed.hostname or "").lower()
    if scheme not in {"http", "https"} or not host:
        raise ConfigError(f"{field} requires an http(s) URL with a host")
    if parsed.username is not None or parsed.password is not None or parsed.query or parsed.fragment:
        raise ConfigError(f"{field} must not contain credentials, a query, or a fragment")
    if "%" in value:
        raise ConfigError(f"{field} must not contain percent escapes")
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        address = None
        if len(host) > 253 or REGISTRY_HOST_RE.fullmatch(host) is None:
            raise ConfigError(f"{field} requires a portable ASCII DNS host or IP literal")
    else:
        host = str(address)
    if scheme == "http":
        loopback = address.is_loopback if address is not None else host == "localhost"
        if not loopback:
            raise ConfigError(f"{field} permits plain HTTP only for an explicitly configured loopback host")
    authority = f"[{host}]" if ":" in host else host
    if port is not None and not ((scheme == "https" and port == 443) or (scheme == "http" and port == 80)):
        authority = f"{authority}:{port}"
    if "//" in parsed.path or "\\" in parsed.path:
        raise ConfigError(f"{field} path must not contain doubled separators or backslashes")
    path = parsed.path.rstrip("/")
    if any(
        ord(character) > 0x7F
        or character.isspace()
        or unicodedata.category(character) == "Cc"
        for character in path
    ):
        raise ConfigError(f"{field} path must contain only printable non-space ASCII characters")
    if any(component in {".", ".."} for component in path.strip("/").split("/")):
        raise ConfigError(f"{field} path must not contain dot segments")
    canonical = urllib.parse.urlunsplit((scheme, authority, path, "", ""))
    if len(canonical) > 4096:
        raise ConfigError(f"{field} canonical URL exceeds 4096 characters")
    return canonical


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
            "registry_policy",
            "snapshot_max_age_seconds",
            "snapshot_clock_skew_seconds",
            "cache_ttl_seconds",
            "offline_grace_seconds",
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
    if not isinstance(backend, str) or not backend or len(backend) > 128:
        raise ConfigError("Global config field 'audit.backend' must be a non-empty string of at most 128 characters")

    model = raw.get("model")
    if model is not None and (not isinstance(model, str) or not model or len(model) > 1024):
        raise ConfigError(
            "Global config field 'audit.model' must be a non-empty string of at most 1024 characters when present"
        )

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

    registry_policy = raw.get("registry_policy", "advisory")
    if registry_policy not in {"advisory", "strict"}:
        raise ConfigError("Global config field 'audit.registry_policy' must be advisory or strict")

    snapshot_max_age_seconds = _bounded_integer(
        raw.get("snapshot_max_age_seconds", DEFAULT_SNAPSHOT_MAX_AGE_SECONDS),
        "audit.snapshot_max_age_seconds",
        minimum=1,
        maximum=MAX_DURATION_SECONDS,
    )
    snapshot_clock_skew_seconds = _bounded_integer(
        raw.get("snapshot_clock_skew_seconds", DEFAULT_SNAPSHOT_CLOCK_SKEW_SECONDS),
        "audit.snapshot_clock_skew_seconds",
        minimum=0,
        maximum=MAX_DURATION_SECONDS,
    )
    cache_ttl_seconds = _bounded_integer(
        raw.get("cache_ttl_seconds", DEFAULT_CACHE_TTL_SECONDS),
        "audit.cache_ttl_seconds",
        minimum=0,
        maximum=MAX_DURATION_SECONDS,
    )
    offline_grace_seconds = _bounded_integer(
        raw.get("offline_grace_seconds", DEFAULT_OFFLINE_GRACE_SECONDS),
        "audit.offline_grace_seconds",
        minimum=0,
        maximum=MAX_DURATION_SECONDS,
    )

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
        registry_policy=registry_policy,
        snapshot_max_age_seconds=snapshot_max_age_seconds,
        snapshot_clock_skew_seconds=snapshot_clock_skew_seconds,
        cache_ttl_seconds=cache_ttl_seconds,
        offline_grace_seconds=offline_grace_seconds,
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
    if audit.registry_policy != "advisory":
        data["registry_policy"] = audit.registry_policy
    if audit.snapshot_max_age_seconds != DEFAULT_SNAPSHOT_MAX_AGE_SECONDS:
        data["snapshot_max_age_seconds"] = audit.snapshot_max_age_seconds
    if audit.snapshot_clock_skew_seconds != DEFAULT_SNAPSHOT_CLOCK_SKEW_SECONDS:
        data["snapshot_clock_skew_seconds"] = audit.snapshot_clock_skew_seconds
    if audit.cache_ttl_seconds != DEFAULT_CACHE_TTL_SECONDS:
        data["cache_ttl_seconds"] = audit.cache_ttl_seconds
    if audit.offline_grace_seconds != DEFAULT_OFFLINE_GRACE_SECONDS:
        data["offline_grace_seconds"] = audit.offline_grace_seconds
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
