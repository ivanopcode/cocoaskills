from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import Any

from .audit.capabilities import CapabilityManifest, CapabilityParseError, parse_capabilities
from .identifiers import IDENTIFIER_RULE, is_valid_identifier


SCHEMA_VERSION = 1
SUPPORTED_SCHEMA_VERSIONS = {1, 2, 3}
UPGRADE_HINT = (
    "Upgrade with: pipx upgrade cocoaskills, brew upgrade cocoaskills, "
    "or mise upgrade pipx:cocoaskills."
)


class SkillSpecError(Exception):
    pass


@dataclass(frozen=True)
class CommandSpec:
    name: str
    type: str
    command: str | None = None
    unix_path: str | None = None
    win_path: str | None = None
    hint: str | None = None
    source: str = "csk-skill.json"


@dataclass(frozen=True)
class DependencySpec:
    name: str
    type: str
    command: str | None = None
    skill: str | None = None
    hint: str | None = None
    source: str = "csk-skill.json"


@dataclass(frozen=True)
class SkillSpec:
    commands: dict[str, CommandSpec]
    source_file: str | None
    schema_version: int = SCHEMA_VERSION
    runtime_roots: tuple[str, ...] = ()
    capabilities: CapabilityManifest = field(default_factory=CapabilityManifest.implicit_none)
    dependencies: dict[str, DependencySpec] = field(default_factory=dict)


def load_skill_spec(snapshot: Path) -> SkillSpec:
    csk_path = snapshot / "csk-skill.json"
    if csk_path.exists():
        return _load_csk_skill(csk_path)
    runtime_path = snapshot / "agents" / "runtime.json"
    if runtime_path.exists():
        return _load_runtime_fallback(runtime_path)
    return SkillSpec(commands={}, source_file=None)


def _load_csk_skill(path: Path) -> SkillSpec:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise SkillSpecError(f"Malformed JSON in {path}: {exc}") from exc
    if not isinstance(data, dict):
        raise SkillSpecError(f"{path} must contain a JSON object")
    schema = data.get("schema_version")
    if not isinstance(schema, int) or isinstance(schema, bool):
        raise SkillSpecError("csk-skill.json field 'schema_version' must be an integer")
    if schema not in SUPPORTED_SCHEMA_VERSIONS:
        raise SkillSpecError(
            f"Unsupported csk-skill.json schema_version {schema!r}; this skill requires a newer csk. "
            f"{UPGRADE_HINT}"
        )
    if schema in {2, 3}:
        allowed_fields = {"schema_version", "runtime_roots", "commands", "dependencies"}
        if schema == 3:
            allowed_fields.add("capabilities")
        _reject_unknown_fields(data, allowed_fields, "csk-skill.json")
    if schema == 3 and "capabilities" not in data:
        raise SkillSpecError("csk-skill.json schema v3 requires 'capabilities'")
    try:
        capabilities = (
            parse_capabilities(data.get("capabilities")) if schema == 3 else CapabilityManifest.implicit_none()
        )
    except CapabilityParseError as exc:
        raise SkillSpecError(str(exc)) from exc
    runtime_roots_raw = data["runtime_roots"] if schema in {2, 3} and "runtime_roots" in data else []
    runtime_roots = _parse_runtime_roots(runtime_roots_raw, snapshot=path.parent) if schema in {2, 3} else ()
    commands_raw = data.get("commands", {})
    if not isinstance(commands_raw, dict):
        raise SkillSpecError("csk-skill.json field 'commands' must be an object")
    commands: dict[str, CommandSpec] = {}
    for name, raw in commands_raw.items():
        if not isinstance(name, str) or not name:
            raise SkillSpecError("Command names must be non-empty strings")
        if not is_valid_identifier(name):
            raise SkillSpecError(f"Command name {name!r} {IDENTIFIER_RULE}")
        if not isinstance(raw, dict):
            raise SkillSpecError(f"Command {name!r} must be an object")
        command_type = raw.get("type")
        if command_type == "script":
            if schema in {2, 3}:
                _reject_unknown_fields(raw, {"type", "unix_path", "win_path"}, f"commands.{name}")
            unix_path = raw.get("unix_path")
            win_path = raw.get("win_path")
            if schema in {2, 3} and unix_path is None and win_path is None:
                raise SkillSpecError(f"Script command {name!r} requires 'unix_path' or 'win_path'")
            if unix_path is not None:
                unix_path = _validate_relative_path(
                    unix_path,
                    field=f"commands.{name}.unix_path",
                    strict_posix=schema in {2, 3},
                )
                if schema in {2, 3}:
                    _validate_v2_script_path(path.parent, unix_path, runtime_roots, field=f"commands.{name}.unix_path")
            if win_path is not None:
                win_path = _validate_relative_path(
                    win_path,
                    field=f"commands.{name}.win_path",
                    strict_posix=schema in {2, 3},
                )
                if schema in {2, 3}:
                    _validate_v2_script_path(path.parent, win_path, runtime_roots, field=f"commands.{name}.win_path")
            commands[name] = CommandSpec(
                name=name,
                type="script",
                unix_path=unix_path,
                win_path=win_path,
                source="csk-skill.json",
            )
        elif command_type == "system":
            if schema in {2, 3}:
                _reject_unknown_fields(raw, {"type", "command", "hint"}, f"commands.{name}")
            command = raw.get("command")
            if not isinstance(command, str) or not command:
                raise SkillSpecError(f"System command {name!r} requires non-empty 'command'")
            hint = raw.get("hint")
            if hint is not None and not isinstance(hint, str):
                raise SkillSpecError(f"System command {name!r} field 'hint' must be a string")
            commands[name] = CommandSpec(
                name=name,
                type="system",
                command=command,
                hint=hint,
                source="csk-skill.json",
            )
        else:
            raise SkillSpecError(f"Command {name!r} has unsupported type {command_type!r}")
    dependencies = _parse_dependencies(data.get("dependencies"), schema=schema)
    return SkillSpec(
        commands=commands,
        source_file="csk-skill.json",
        schema_version=schema,
        runtime_roots=runtime_roots,
        capabilities=capabilities,
        dependencies=dependencies,
    )


def _load_runtime_fallback(path: Path) -> SkillSpec:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise SkillSpecError(f"Malformed JSON in {path}: {exc}") from exc
    commands_raw = data.get("commands", {}) if isinstance(data, dict) else {}
    if not isinstance(commands_raw, dict):
        raise SkillSpecError("agents/runtime.json field 'commands' must be an object")
    commands: dict[str, CommandSpec] = {}
    for name, rel_path in commands_raw.items():
        if not isinstance(name, str) or not name:
            raise SkillSpecError("Runtime command names must be non-empty strings")
        if not is_valid_identifier(name):
            raise SkillSpecError(f"Runtime command name {name!r} {IDENTIFIER_RULE}")
        if not isinstance(rel_path, str) or not rel_path:
            raise SkillSpecError(f"Runtime command {name!r} path must be a non-empty string")
        _validate_relative_path(rel_path, field=f"commands.{name}")
        commands[name] = CommandSpec(
            name=name,
            type="script",
            unix_path=rel_path,
            win_path=rel_path if rel_path.endswith(".cmd") else None,
            source="agents/runtime.json",
        )
    return SkillSpec(commands=commands, source_file="agents/runtime.json")


def _parse_dependencies(raw: Any, *, schema: int) -> dict[str, DependencySpec]:
    if raw is None:
        return {}
    if schema not in {2, 3}:
        raise SkillSpecError("csk-skill.json field 'dependencies' requires schema_version 2 or newer")
    if not isinstance(raw, dict):
        raise SkillSpecError("csk-skill.json field 'dependencies' must be an object")
    _reject_unknown_fields(raw, {"commands"}, "dependencies")
    commands_raw = raw.get("commands", {})
    if not isinstance(commands_raw, dict):
        raise SkillSpecError("dependencies.commands must be an object")

    dependencies: dict[str, DependencySpec] = {}
    for name, entry in commands_raw.items():
        if not isinstance(name, str) or not name:
            raise SkillSpecError("Dependency command names must be non-empty strings")
        if not is_valid_identifier(name):
            raise SkillSpecError(f"Dependency command name {name!r} {IDENTIFIER_RULE}")
        if not isinstance(entry, dict):
            raise SkillSpecError(f"dependencies.commands.{name} must be an object")
        dependency_type = entry.get("type")
        hint = entry.get("hint")
        if hint is not None and not isinstance(hint, str):
            raise SkillSpecError(f"dependencies.commands.{name}.hint must be a string")
        if dependency_type == "system":
            _reject_unknown_fields(entry, {"type", "command", "hint"}, f"dependencies.commands.{name}")
            command = entry.get("command")
            if not isinstance(command, str) or not command:
                raise SkillSpecError(f"System dependency {name!r} requires non-empty 'command'")
            dependencies[name] = DependencySpec(
                name=name,
                type="system",
                command=command,
                hint=hint,
                source="csk-skill.json",
            )
        elif dependency_type == "skill":
            _reject_unknown_fields(entry, {"type", "skill", "command", "hint"}, f"dependencies.commands.{name}")
            skill = entry.get("skill")
            if not isinstance(skill, str) or not skill:
                raise SkillSpecError(f"Skill dependency {name!r} requires non-empty 'skill'")
            if not is_valid_identifier(skill):
                raise SkillSpecError(f"Skill dependency name {skill!r} {IDENTIFIER_RULE}")
            command = entry.get("command")
            if not isinstance(command, str) or not command:
                raise SkillSpecError(f"Skill dependency {name!r} requires non-empty 'command'")
            if not is_valid_identifier(command):
                raise SkillSpecError(f"Skill dependency command {command!r} {IDENTIFIER_RULE}")
            dependencies[name] = DependencySpec(
                name=name,
                type="skill",
                command=command,
                skill=skill,
                hint=hint,
                source="csk-skill.json",
            )
        else:
            raise SkillSpecError(f"Dependency command {name!r} has unsupported type {dependency_type!r}")
    return dependencies


def _validate_relative_path(value: Any, *, field: str, strict_posix: bool = False) -> str:
    if not isinstance(value, str) or not value:
        raise SkillSpecError(f"{field} must be a non-empty string")
    normalized = value.rstrip("/")
    if not normalized:
        raise SkillSpecError(f"{field} must be a non-empty string")
    if strict_posix and ("\\" in normalized or "//" in normalized):
        raise SkillSpecError(f"{field} must be a POSIX-style relative path inside the skill repository")
    if strict_posix and any(part in {"", "."} for part in normalized.split("/")):
        raise SkillSpecError(f"{field} must be a POSIX-style relative path inside the skill repository")
    path = PurePosixPath(normalized)
    if path.is_absolute() or ".." in path.parts:
        raise SkillSpecError(f"{field} must be a relative path inside the skill repository")
    if not path.parts:
        raise SkillSpecError(f"{field} must be a relative path inside the skill repository")
    return path.as_posix()


def _parse_runtime_roots(raw: Any, *, snapshot: Path) -> tuple[str, ...]:
    if not isinstance(raw, list):
        raise SkillSpecError("csk-skill.json field 'runtime_roots' must be a list")
    roots: list[str] = []
    for index, value in enumerate(raw):
        root = _validate_relative_path(value, field=f"runtime_roots[{index}]", strict_posix=True)
        root_path = snapshot / root
        if not root_path.exists():
            raise SkillSpecError(f"runtime root does not exist: {root}")
        if not root_path.is_dir():
            raise SkillSpecError(f"runtime root must be a directory: {root}")
        roots.append(root)

    if len(set(roots)) != len(roots):
        raise SkillSpecError("runtime roots must be unique after normalization")

    sorted_roots = sorted(roots)
    for left_index, left in enumerate(sorted_roots):
        for right in sorted_roots[left_index + 1 :]:
            if _path_contains(left, right) or _path_contains(right, left):
                container, contained = (left, right) if _path_contains(left, right) else (right, left)
                raise SkillSpecError(f"runtime roots must be disjoint: {container} contains {contained}")
    return tuple(roots)


def _validate_v2_script_path(snapshot: Path, rel_path: str, runtime_roots: tuple[str, ...], *, field: str) -> None:
    script_path = snapshot / rel_path
    if not script_path.exists():
        raise SkillSpecError(f"{field} source file not found: {rel_path}")
    if not script_path.is_file():
        raise SkillSpecError(f"{field} must point to a file: {rel_path}")
    if runtime_roots and not any(_path_contains(root, rel_path) for root in runtime_roots):
        raise SkillSpecError(f'command path "{rel_path}" is not inside any runtime_roots')


def _path_contains(root: str, rel_path: str) -> bool:
    root_parts = PurePosixPath(root).parts
    path_parts = PurePosixPath(rel_path).parts
    return len(path_parts) >= len(root_parts) and path_parts[: len(root_parts)] == root_parts


def _reject_unknown_fields(data: dict[str, Any], allowed: set[str], label: str) -> None:
    unknown = sorted(set(data) - allowed)
    if unknown:
        joined = ", ".join(repr(item) for item in unknown)
        raise SkillSpecError(f"{label} has unsupported field(s): {joined}")
