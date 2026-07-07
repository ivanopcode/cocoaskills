from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import Any

from .audit.capabilities import CapabilityManifest, CapabilityParseError, parse_capabilities
from .identifiers import IDENTIFIER_RULE, is_valid_identifier


SCHEMA_VERSION = 1
SUPPORTED_SCHEMA_VERSIONS = {1, 2, 3, 4, 5}
UPGRADE_HINT = (
    "Upgrade with: pipx upgrade cocoaskills, brew upgrade cocoaskills, "
    "or mise upgrade pipx:cocoaskills."
)

REQUIREMENT_MODES = {"full", "runtime", "context"}
REQUIREMENT_REF_KINDS = {"tag", "revision"}
_RANGE_MARKERS = ("^", "~", ">", "<", "*", " ")

MCP_TRANSPORTS = {"stdio", "http"}
MCP_REQUIRED_IN = {"any", "all"}


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
class SkillRequirement:
    """A self-contained skill-to-skill requirement (dependencies.skills)."""

    name: str
    git: str
    ref_kind: str
    ref_value: str
    mode: str = "full"
    commands: tuple[str, ...] = ()
    source: str = "csk-skill.json"


@dataclass(frozen=True)
class McpServerRequirement:
    """A declared dependency on an MCP server configured in agent environments."""

    name: str
    hint: str
    transport: str | None = None
    required_in: str = "any"
    source: str = "csk-skill.json"


@dataclass(frozen=True)
class SkillSpec:
    commands: dict[str, CommandSpec]
    source_file: str | None
    schema_version: int = SCHEMA_VERSION
    runtime_roots: tuple[str, ...] = ()
    capabilities: CapabilityManifest = field(default_factory=CapabilityManifest.implicit_none)
    dependencies: dict[str, DependencySpec] = field(default_factory=dict)
    requirements: dict[str, SkillRequirement] = field(default_factory=dict)
    mcp_servers: dict[str, McpServerRequirement] = field(default_factory=dict)


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
    if schema >= 2:
        allowed_fields = {"schema_version", "runtime_roots", "commands", "dependencies"}
        if schema >= 3:
            allowed_fields.add("capabilities")
        _reject_unknown_fields(data, allowed_fields, "csk-skill.json")
    if schema >= 3 and "capabilities" not in data:
        raise SkillSpecError(f"csk-skill.json schema v{schema} requires 'capabilities'")
    try:
        capabilities = (
            parse_capabilities(data.get("capabilities")) if schema >= 3 else CapabilityManifest.implicit_none()
        )
    except CapabilityParseError as exc:
        raise SkillSpecError(str(exc)) from exc
    runtime_roots_raw = data["runtime_roots"] if schema >= 2 and "runtime_roots" in data else []
    runtime_roots = _parse_runtime_roots(runtime_roots_raw, snapshot=path.parent) if schema >= 2 else ()
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
            if schema >= 2:
                _reject_unknown_fields(raw, {"type", "unix_path", "win_path"}, f"commands.{name}")
            unix_path = raw.get("unix_path")
            win_path = raw.get("win_path")
            if schema >= 2 and unix_path is None and win_path is None:
                raise SkillSpecError(f"Script command {name!r} requires 'unix_path' or 'win_path'")
            if unix_path is not None:
                unix_path = _validate_relative_path(
                    unix_path,
                    field=f"commands.{name}.unix_path",
                    strict_posix=schema >= 2,
                )
                if schema >= 2:
                    _validate_v2_script_path(path.parent, unix_path, runtime_roots, field=f"commands.{name}.unix_path")
            if win_path is not None:
                win_path = _validate_relative_path(
                    win_path,
                    field=f"commands.{name}.win_path",
                    strict_posix=schema >= 2,
                )
                if schema >= 2:
                    _validate_v2_script_path(path.parent, win_path, runtime_roots, field=f"commands.{name}.win_path")
            commands[name] = CommandSpec(
                name=name,
                type="script",
                unix_path=unix_path,
                win_path=win_path,
                source="csk-skill.json",
            )
        elif command_type == "system":
            if schema >= 2:
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
    dependencies, requirements, mcp_servers = _parse_dependencies(data.get("dependencies"), schema=schema)
    return SkillSpec(
        commands=commands,
        source_file="csk-skill.json",
        schema_version=schema,
        runtime_roots=runtime_roots,
        capabilities=capabilities,
        dependencies=dependencies,
        requirements=requirements,
        mcp_servers=mcp_servers,
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


def _parse_dependencies(
    raw: Any, *, schema: int
) -> tuple[dict[str, DependencySpec], dict[str, SkillRequirement], dict[str, McpServerRequirement]]:
    if raw is None:
        return {}, {}, {}
    if schema < 2:
        raise SkillSpecError("csk-skill.json field 'dependencies' requires schema_version 2 or newer")
    if not isinstance(raw, dict):
        raise SkillSpecError("csk-skill.json field 'dependencies' must be an object")
    if schema < 4 and "skills" in raw:
        raise SkillSpecError("csk-skill.json field 'dependencies.skills' requires schema_version 4")
    if schema < 5 and "mcp_servers" in raw:
        raise SkillSpecError("csk-skill.json field 'dependencies.mcp_servers' requires schema_version 5")
    allowed = {"commands"}
    if schema >= 4:
        allowed.add("skills")
    if schema >= 5:
        allowed.add("mcp_servers")
    _reject_unknown_fields(raw, allowed, "dependencies")
    requirements = _parse_requirements(raw.get("skills"), schema=schema)
    mcp_servers = _parse_mcp_servers(raw.get("mcp_servers"))
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
    return dependencies, requirements, mcp_servers


def _parse_mcp_servers(raw: Any) -> dict[str, McpServerRequirement]:
    if raw is None:
        return {}
    if not isinstance(raw, dict):
        raise SkillSpecError("dependencies.mcp_servers must be an object")
    servers: dict[str, McpServerRequirement] = {}
    for name, entry in raw.items():
        label = f"dependencies.mcp_servers.{name}"
        if not isinstance(name, str) or not name:
            raise SkillSpecError("MCP server names must be non-empty strings")
        if not is_valid_identifier(name):
            raise SkillSpecError(f"MCP server name {name!r} {IDENTIFIER_RULE}")
        if not isinstance(entry, dict):
            raise SkillSpecError(f"{label} must be an object")
        _reject_unknown_fields(entry, {"hint", "transport", "required_in"}, label)
        hint = entry.get("hint")
        if not isinstance(hint, str) or not hint:
            raise SkillSpecError(f"{label} requires a non-empty 'hint' describing how to connect the server")
        transport = entry.get("transport")
        if transport is not None and transport not in MCP_TRANSPORTS:
            raise SkillSpecError(f"{label}.transport must be 'stdio' or 'http'")
        required_in = entry.get("required_in", "any")
        if required_in not in MCP_REQUIRED_IN:
            raise SkillSpecError(f"{label}.required_in must be 'any' or 'all'")
        servers[name] = McpServerRequirement(
            name=name,
            hint=hint,
            transport=transport,
            required_in=required_in,
        )
    return servers


def _parse_requirements(raw: Any, *, schema: int) -> dict[str, SkillRequirement]:
    if raw is None:
        return {}
    if schema < 4:
        raise SkillSpecError("csk-skill.json field 'dependencies.skills' requires schema_version 4")
    if not isinstance(raw, dict):
        raise SkillSpecError("dependencies.skills must be an object")
    requirements: dict[str, SkillRequirement] = {}
    for name, entry in raw.items():
        label = f"dependencies.skills.{name}"
        if not isinstance(name, str) or not name:
            raise SkillSpecError("Skill requirement names must be non-empty strings")
        if not is_valid_identifier(name):
            raise SkillSpecError(f"Skill requirement name {name!r} {IDENTIFIER_RULE}")
        if not isinstance(entry, dict):
            raise SkillSpecError(f"{label} must be an object")
        if "version" in entry:
            raise SkillSpecError(
                f"{label} declares 'version'; version ranges are not supported. "
                "Pin an exact ref: {\"kind\": \"tag\" | \"revision\", \"value\": ...}"
            )
        _reject_unknown_fields(entry, {"git", "ref", "mode", "commands"}, label)

        git = entry.get("git")
        if not isinstance(git, str) or not git:
            raise SkillSpecError(f"{label} requires a non-empty 'git' source URL")

        ref = entry.get("ref")
        if not isinstance(ref, dict):
            raise SkillSpecError(f"{label} requires a 'ref' object with 'kind' and 'value'")
        _reject_unknown_fields(ref, {"kind", "value"}, f"{label}.ref")
        kind = ref.get("kind")
        if kind == "branch":
            raise SkillSpecError(
                f"{label}.ref pins a branch; skill requirements accept exact 'tag' or 'revision' refs only"
            )
        if kind not in REQUIREMENT_REF_KINDS:
            raise SkillSpecError(f"{label}.ref.kind must be 'tag' or 'revision'")
        value = ref.get("value")
        if not isinstance(value, str) or not value:
            raise SkillSpecError(f"{label}.ref.value must be a non-empty string")
        if any(marker in value for marker in _RANGE_MARKERS):
            raise SkillSpecError(
                f"{label}.ref.value {value!r} looks like a version range; "
                "skill requirements accept exact tags or revisions only"
            )

        mode = entry.get("mode", "full")
        if mode not in REQUIREMENT_MODES:
            raise SkillSpecError(f"{label}.mode must be one of full, runtime, or context")

        commands_raw = entry.get("commands")
        commands: tuple[str, ...] = ()
        if commands_raw is not None:
            if mode != "runtime":
                raise SkillSpecError(f"{label}.commands applies to runtime requirements only")
            if not isinstance(commands_raw, list) or not commands_raw:
                raise SkillSpecError(f"{label}.commands must be a non-empty list of command names")
            seen: list[str] = []
            for item in commands_raw:
                if not isinstance(item, str) or not item:
                    raise SkillSpecError(f"{label}.commands entries must be non-empty strings")
                if not is_valid_identifier(item):
                    raise SkillSpecError(f"{label}.commands entry {item!r} {IDENTIFIER_RULE}")
                if item not in seen:
                    seen.append(item)
            commands = tuple(seen)

        requirements[name] = SkillRequirement(
            name=name,
            git=git,
            ref_kind=kind,
            ref_value=value,
            mode=mode,
            commands=commands,
        )
    return requirements


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
