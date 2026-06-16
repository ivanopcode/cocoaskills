from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any


DEFAULT_MAX_REQUEST_BYTES = 1_048_576
MAX_MAX_REQUEST_BYTES = 10_485_760
MAX_TIMEOUT_SECONDS = 300.0


class BackendConfigError(Exception):
    pass


@dataclass(frozen=True)
class NullBackendConfig:
    name: str = "null"
    kind: str = "null"
    timeout_seconds: float = 30.0
    cloud: bool = False
    model: str | None = None


@dataclass(frozen=True)
class CommandBackendConfig:
    name: str
    kind: str
    command: tuple[str, ...]
    timeout_seconds: float
    cloud: bool
    env: dict[str, str]
    cwd: Path | None
    model: str | None = None


@dataclass(frozen=True)
class CodexBackendConfig:
    name: str
    kind: str
    timeout_seconds: float
    cloud: bool
    model: str | None
    profile: str | None
    oss: bool
    local_provider: str | None
    sandbox: str
    approval_policy: str
    extra_args: tuple[str, ...]


BackendConfig = NullBackendConfig | CommandBackendConfig | CodexBackendConfig


UNSAFE_CODEX_EXTRA_ARGS = {
    "--sandbox",
    "--ask-for-approval",
    "--search",
    "--cd",
    "--output-schema",
    "--output-last-message",
    "--full-auto",
    "--add-dir",
    "--config",
    "-c",
    "--profile",
    "--model",
    "--oss",
    "--local-provider",
    "--ephemeral",
    "--ignore-rules",
    "--skip-git-repo-check",
    "--resume",
    "--experimental-resume",
    "--enable",
    "--disable",
}


def parse_backend_configs(
    raw_backends: dict[str, Any],
    *,
    global_model: str | None,
    allow_cloud: bool,
) -> dict[str, BackendConfig]:
    parsed: dict[str, BackendConfig] = {}
    for name, raw in raw_backends.items():
        if not isinstance(name, str) or not name:
            raise BackendConfigError("audit.backends keys must be non-empty strings")
        parsed[name] = parse_backend_config(name, raw, global_model=global_model, allow_cloud=allow_cloud)
    return parsed


def resolve_backend_config(
    backend_name: str,
    raw_backends: dict[str, Any],
    *,
    global_model: str | None,
    allow_cloud: bool,
) -> BackendConfig:
    if backend_name == "null" and backend_name not in raw_backends:
        return NullBackendConfig(model=None)
    parsed = parse_backend_configs(raw_backends, global_model=global_model, allow_cloud=allow_cloud)
    try:
        return parsed[backend_name]
    except KeyError as exc:
        raise BackendConfigError(f"Unsupported audit backend: {backend_name}") from exc


def parse_backend_config(
    name: str,
    raw: Any,
    *,
    global_model: str | None,
    allow_cloud: bool,
) -> BackendConfig:
    if not isinstance(raw, dict):
        raise BackendConfigError(f"audit.backends.{name} must be an object")
    kind = raw.get("kind")
    if kind == "null":
        _reject_unknown_fields(raw, {"kind", "timeout_seconds", "cloud"}, f"audit.backends.{name}")
        cloud = _bool_field(raw, "cloud", False, f"audit.backends.{name}.cloud")
        if cloud:
            raise BackendConfigError(f"audit.backends.{name}.cloud must be false for null backend")
        return NullBackendConfig(name=name, timeout_seconds=_timeout(raw, name))
    if kind == "command":
        return _parse_command(name, raw, global_model=global_model, allow_cloud=allow_cloud)
    if kind == "codex":
        return _parse_codex(name, raw, global_model=global_model, allow_cloud=allow_cloud)
    raise BackendConfigError(f"audit.backends.{name}.kind must be null, command, or codex")


def _parse_command(
    name: str,
    raw: dict[str, Any],
    *,
    global_model: str | None,
    allow_cloud: bool,
) -> CommandBackendConfig:
    _reject_unknown_fields(raw, {"kind", "command", "timeout_seconds", "cloud", "env", "cwd", "model"}, f"audit.backends.{name}")
    command = raw.get("command")
    if not _is_str_list(command) or not command:
        raise BackendConfigError(f"audit.backends.{name}.command must be a non-empty list of strings")
    cloud = _bool_field(raw, "cloud", False, f"audit.backends.{name}.cloud")
    _validate_cloud_allowed(name, cloud, allow_cloud)
    env_raw = raw.get("env", {})
    if not isinstance(env_raw, dict) or not all(isinstance(k, str) and isinstance(v, str) for k, v in env_raw.items()):
        raise BackendConfigError(f"audit.backends.{name}.env must be an object of string values")
    cwd_raw = raw.get("cwd")
    if cwd_raw is not None and (not isinstance(cwd_raw, str) or not cwd_raw):
        raise BackendConfigError(f"audit.backends.{name}.cwd must be a non-empty string when present")
    model = _model(raw, global_model, f"audit.backends.{name}.model")
    return CommandBackendConfig(
        name=name,
        kind="command",
        command=tuple(command),
        timeout_seconds=_timeout(raw, name),
        cloud=cloud,
        env=dict(env_raw),
        cwd=Path(cwd_raw).expanduser() if cwd_raw else None,
        model=model,
    )


def _parse_codex(
    name: str,
    raw: dict[str, Any],
    *,
    global_model: str | None,
    allow_cloud: bool,
) -> CodexBackendConfig:
    _reject_unknown_fields(
        raw,
        {
            "kind",
            "timeout_seconds",
            "cloud",
            "model",
            "profile",
            "oss",
            "local_provider",
            "sandbox",
            "approval_policy",
            "extra_args",
        },
        f"audit.backends.{name}",
    )
    cloud = _bool_field(raw, "cloud", False, f"audit.backends.{name}.cloud")
    _validate_cloud_allowed(name, cloud, allow_cloud)
    model = _model(raw, global_model, f"audit.backends.{name}.model")
    profile = _optional_string(raw.get("profile"), f"audit.backends.{name}.profile")
    oss = _bool_field(raw, "oss", False, f"audit.backends.{name}.oss")
    local_provider = _optional_string(raw.get("local_provider"), f"audit.backends.{name}.local_provider")
    sandbox = raw.get("sandbox", "read-only")
    if sandbox != "read-only":
        raise BackendConfigError(f"audit.backends.{name}.sandbox must be read-only")
    approval_policy = raw.get("approval_policy", "never")
    if approval_policy != "never":
        raise BackendConfigError(f"audit.backends.{name}.approval_policy must be never")
    extra_args_raw = raw.get("extra_args", [])
    if not _is_str_list(extra_args_raw):
        raise BackendConfigError(f"audit.backends.{name}.extra_args must be a list of strings")
    if cloud:
        if oss or local_provider is not None:
            raise BackendConfigError(f"audit.backends.{name} must not set oss or local_provider when cloud=true")
        if extra_args_raw:
            raise BackendConfigError(f"audit.backends.{name}.extra_args are not allowed when cloud=true")
    else:
        if not oss or local_provider is None:
            raise BackendConfigError(f"audit.backends.{name} requires oss=true and local_provider when cloud=false")
    _validate_codex_extra_args(tuple(extra_args_raw), f"audit.backends.{name}.extra_args")
    return CodexBackendConfig(
        name=name,
        kind="codex",
        timeout_seconds=_timeout(raw, name, default=60.0),
        cloud=cloud,
        model=model,
        profile=profile,
        oss=oss,
        local_provider=local_provider,
        sandbox=sandbox,
        approval_policy=approval_policy,
        extra_args=tuple(extra_args_raw),
    )


def _validate_cloud_allowed(name: str, cloud: bool, allow_cloud: bool) -> None:
    if cloud and not allow_cloud:
        raise BackendConfigError(f"audit.backends.{name}.cloud requires audit.allow_cloud=true")


def _timeout(raw: dict[str, Any], name: str, *, default: float = 30.0) -> float:
    value = raw.get("timeout_seconds", default)
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise BackendConfigError(f"audit.backends.{name}.timeout_seconds must be a positive number")
    timeout = float(value)
    if timeout < 1 or timeout > MAX_TIMEOUT_SECONDS:
        raise BackendConfigError(f"audit.backends.{name}.timeout_seconds must be between 1 and {int(MAX_TIMEOUT_SECONDS)}")
    return timeout


def _model(raw: dict[str, Any], global_model: str | None, field: str) -> str | None:
    value = raw.get("model", global_model)
    return _optional_string(value, field)


def _optional_string(value: Any, field: str) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str) or not value:
        raise BackendConfigError(f"{field} must be a non-empty string when present")
    return value


def _bool_field(raw: dict[str, Any], key: str, default: bool, field: str) -> bool:
    value = raw.get(key, default)
    if not isinstance(value, bool):
        raise BackendConfigError(f"{field} must be a boolean")
    return value


def _validate_codex_extra_args(args: tuple[str, ...], field: str) -> None:
    for arg in args:
        option = arg.split("=", 1)[0]
        if option in UNSAFE_CODEX_EXTRA_ARGS or arg.startswith("--dangerously-"):
            raise BackendConfigError(f"{field} contains unsafe Codex option: {arg}")


def _is_str_list(value: Any) -> bool:
    return isinstance(value, list) and all(isinstance(item, str) for item in value)


def _reject_unknown_fields(data: dict[str, Any], allowed: set[str], label: str) -> None:
    unknown = sorted(set(data) - allowed)
    if unknown:
        joined = ", ".join(repr(item) for item in unknown)
        raise BackendConfigError(f"{label} has unsupported field(s): {joined}")
