from __future__ import annotations

from dataclasses import dataclass
from pathlib import PurePosixPath
from typing import Any


class CapabilityParseError(Exception):
    pass


@dataclass(frozen=True)
class CapabilityManifest:
    network: tuple[str, ...] = ()
    filesystem: str | tuple[str, ...] = "repo"
    exec: tuple[str, ...] = ()
    secrets: tuple[str, ...] = ()
    env_read: tuple[str, ...] = ()
    prompt_scope: str | None = None

    @classmethod
    def implicit_none(cls) -> "CapabilityManifest":
        return cls(network=(), filesystem=(), exec=(), secrets=(), env_read=(), prompt_scope=None)


def parse_capabilities(raw: Any) -> CapabilityManifest:
    if raw is None:
        return CapabilityManifest.implicit_none()
    if not isinstance(raw, dict):
        raise CapabilityParseError("capabilities must be an object")
    _reject_unknown_fields(
        raw,
        {"network", "filesystem", "exec", "secrets", "env_read", "prompt_scope"},
        "capabilities",
    )
    network = _parse_none_or_string_list(raw.get("network", "none"), "capabilities.network", host_globs=True)
    filesystem = _parse_filesystem(raw.get("filesystem", "repo"))
    exec_values = _parse_none_or_string_list(raw.get("exec", "none"), "capabilities.exec", executables=True)
    secrets = _parse_none_or_string_list(raw.get("secrets", "none"), "capabilities.secrets")
    env_read = _parse_none_or_string_list(raw.get("env_read", []), "capabilities.env_read", env_vars=True)
    prompt_scope = raw.get("prompt_scope")
    if prompt_scope is not None and (not isinstance(prompt_scope, str) or not prompt_scope.strip()):
        raise CapabilityParseError("capabilities.prompt_scope must be a non-empty string when present")
    return CapabilityManifest(
        network=network,
        filesystem=filesystem,
        exec=exec_values,
        secrets=secrets,
        env_read=env_read,
        prompt_scope=prompt_scope.strip() if isinstance(prompt_scope, str) else None,
    )


def _parse_filesystem(raw: Any) -> str | tuple[str, ...]:
    if isinstance(raw, str) and raw in {"repo", "home-config"}:
        return raw
    values = _parse_none_or_string_list(raw, "capabilities.filesystem", paths=True)
    return values


def _parse_none_or_string_list(
    raw: Any,
    field: str,
    *,
    host_globs: bool = False,
    executables: bool = False,
    env_vars: bool = False,
    paths: bool = False,
) -> tuple[str, ...]:
    if raw == "none":
        return ()
    if not isinstance(raw, list):
        raise CapabilityParseError(f"{field} must be 'none' or a list of strings")
    values: list[str] = []
    for index, value in enumerate(raw):
        if not isinstance(value, str) or not value.strip():
            raise CapabilityParseError(f"{field}[{index}] must be a non-empty string")
        value = value.strip()
        if host_globs:
            _validate_host_glob(value, f"{field}[{index}]")
        if executables:
            _validate_executable(value, f"{field}[{index}]")
        if env_vars:
            _validate_env_var(value, f"{field}[{index}]")
        if paths:
            _validate_path(value, f"{field}[{index}]")
        values.append(value)
    if len(set(values)) != len(values):
        raise CapabilityParseError(f"{field} values must be unique")
    return tuple(values)


def _validate_host_glob(value: str, field: str) -> None:
    if any(ch.isspace() for ch in value) or "/" in value or "\\" in value:
        raise CapabilityParseError(f"{field} must be a host glob, not a URL or path")


def _validate_executable(value: str, field: str) -> None:
    if value.startswith("-") or "/" in value or "\\" in value or any(ch.isspace() for ch in value):
        raise CapabilityParseError(f"{field} must be an executable name, not a path or command line")


def _validate_env_var(value: str, field: str) -> None:
    if not value.replace("_", "A").isalnum() or value[0].isdigit():
        raise CapabilityParseError(f"{field} must be an environment variable name")


def _validate_path(value: str, field: str) -> None:
    if "\x00" in value:
        raise CapabilityParseError(f"{field} must not contain NUL bytes")
    if value.startswith("-"):
        raise CapabilityParseError(f"{field} must not start with '-'")
    if value.startswith("~/"):
        return
    posix = PurePosixPath(value)
    if posix.is_absolute() or (posix.parts and posix.parts[0] == "~"):
        return
    if ".." in posix.parts:
        raise CapabilityParseError(f"{field} must not contain '..'")


def _reject_unknown_fields(data: dict[str, Any], allowed: set[str], label: str) -> None:
    unknown = sorted(set(data) - allowed)
    if unknown:
        joined = ", ".join(repr(item) for item in unknown)
        raise CapabilityParseError(f"{label} has unsupported field(s): {joined}")
