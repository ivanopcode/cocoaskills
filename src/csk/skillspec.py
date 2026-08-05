from __future__ import annotations

import stat
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import Any

from . import protocol_json
from .audit.capabilities import CapabilityManifest, CapabilityParseError, parse_capabilities
from .build_repository import (
    GO_REPOSITORY_V1_DRIVER,
    BuildRepository,
    BuildRepositoryError,
    is_valid_ref_name,
    parse_locked_commit,
    parse_repository_source,
)
from .builds import GO_V1_DRIVER
from .identifiers import IDENTIFIER_RULE, is_valid_identifier, is_valid_portable_path


SCHEMA_VERSION = 1
SUPPORTED_SCHEMA_VERSIONS = {1, 2, 3, 4, 5, 6, 7}
CANONICAL_MANIFEST = "agent-skill.json"
LEGACY_MANIFEST = "csk-skill.json"
RUNTIME_FALLBACK = "agents/runtime.json"
UPGRADE_HINT = (
    "Upgrade with: pipx upgrade cocoaskills, brew upgrade cocoaskills, "
    "or mise upgrade pipx:cocoaskills."
)

REQUIREMENT_MODES = {"full", "runtime", "context"}
REQUIREMENT_REF_KINDS = {"tag", "revision"}
_RANGE_MARKERS = ("^", "~", ">", "<", "*", " ")
_SCHEMA_V1_RESERVED_TOP_LEVEL_FIELDS = frozenset(
    {"build_roots", "build_repositories", "driver", "repository", "target"}
)
_SCHEMA_V1_RESERVED_COMMAND_FIELDS = frozenset({"driver", "source_dir", "repository", "target"})

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
    source: str = CANONICAL_MANIFEST
    driver: str | None = None
    source_dir: str | None = None
    repository: str | None = None
    target: str | None = None


@dataclass(frozen=True)
class DependencySpec:
    name: str
    type: str
    command: str | None = None
    skill: str | None = None
    hint: str | None = None
    source: str = CANONICAL_MANIFEST


@dataclass(frozen=True)
class SkillRequirement:
    """A self-contained skill-to-skill requirement (dependencies.skills)."""

    name: str
    git: str
    ref_kind: str
    ref_value: str
    mode: str = "full"
    commands: tuple[str, ...] = ()
    source: str = CANONICAL_MANIFEST


@dataclass(frozen=True)
class McpServerRequirement:
    """A declared dependency on an MCP server configured in agent environments."""

    name: str
    hint: str
    transport: str | None = None
    required_in: str = "any"
    source: str = CANONICAL_MANIFEST


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
    build_roots: tuple[str, ...] = ()
    build_repositories: dict[str, BuildRepository] = field(default_factory=dict)


def load_skill_spec(snapshot: Path) -> SkillSpec:
    canonical_path = snapshot / CANONICAL_MANIFEST
    legacy_path = snapshot / LEGACY_MANIFEST
    if canonical_path.exists() and legacy_path.exists():
        canonical, canonical_data = _load_skill_manifest(canonical_path)
        _, legacy_data = _load_skill_manifest(legacy_path)
        if not _json_values_equal(canonical_data, legacy_data):
            raise SkillSpecError(
                f"conflicting_skill_manifests: {CANONICAL_MANIFEST} and "
                f"{LEGACY_MANIFEST} contain different JSON values"
            )
        return canonical
    if canonical_path.exists():
        return _load_skill_manifest(canonical_path)[0]
    if legacy_path.exists():
        return _load_skill_manifest(legacy_path)[0]
    runtime_path = snapshot / Path(RUNTIME_FALLBACK)
    if runtime_path.exists():
        return _load_runtime_fallback(runtime_path)
    return SkillSpec(commands={}, source_file=None)


def manifest_source_path(snapshot: Path) -> str:
    """Return the protocol path selected for diagnostics without parsing it."""
    for name in (CANONICAL_MANIFEST, LEGACY_MANIFEST, RUNTIME_FALLBACK):
        if (snapshot / Path(name)).exists():
            return name
    return ""


def _json_values_equal(left: Any, right: Any) -> bool:
    if type(left) is not type(right):
        return False
    if isinstance(left, dict):
        return left.keys() == right.keys() and all(_json_values_equal(left[key], right[key]) for key in left)
    if isinstance(left, list):
        return len(left) == len(right) and all(_json_values_equal(a, b) for a, b in zip(left, right, strict=True))
    return bool(left == right)


def _load_skill_manifest(path: Path) -> tuple[SkillSpec, dict[str, Any]]:
    try:
        data = protocol_json.loads(path.read_bytes())
    except protocol_json.ProtocolJSONError as exc:
        raise SkillSpecError(f"Malformed JSON in {path}: {exc}") from exc
    if not isinstance(data, dict):
        raise SkillSpecError(f"{path} must contain a JSON object")
    source_file = path.name
    schema = data.get("schema_version")
    if not isinstance(schema, int) or isinstance(schema, bool):
        raise SkillSpecError(f"{source_file} field 'schema_version' must be an integer")
    if schema not in SUPPORTED_SCHEMA_VERSIONS:
        raise SkillSpecError(
            f"Unsupported {source_file} schema_version {schema!r}; this skill requires a newer csk. "
            f"{UPGRADE_HINT}"
        )
    if schema == 1:
        reserved_fields = sorted(data.keys() & _SCHEMA_V1_RESERVED_TOP_LEVEL_FIELDS)
        if reserved_fields:
            joined = ", ".join(repr(key) for key in reserved_fields)
            raise SkillSpecError(f"{source_file} has unsupported field(s): {joined}")
    if schema >= 2:
        allowed_fields = {"schema_version", "runtime_roots", "commands", "dependencies"}
        if schema >= 3:
            allowed_fields.add("capabilities")
        if schema >= 6:
            allowed_fields.add("build_roots")
        if schema >= 7:
            allowed_fields.add("build_repositories")
        _reject_unknown_fields(data, allowed_fields, source_file)
    if schema >= 3 and "capabilities" not in data:
        raise SkillSpecError(f"{source_file} schema v{schema} requires 'capabilities'")
    try:
        capabilities = (
            parse_capabilities(data.get("capabilities")) if schema >= 3 else CapabilityManifest.implicit_none()
        )
    except CapabilityParseError as exc:
        raise SkillSpecError(str(exc)) from exc
    runtime_roots_raw = data["runtime_roots"] if schema >= 2 and "runtime_roots" in data else []
    runtime_roots = (
        _parse_runtime_roots(runtime_roots_raw, snapshot=path.parent, source_file=source_file)
        if schema >= 2
        else ()
    )
    build_roots_raw = data["build_roots"] if schema >= 6 and "build_roots" in data else []
    build_roots = (
        _parse_build_roots(
            build_roots_raw,
            snapshot=path.parent,
            runtime_roots=runtime_roots,
            source_file=source_file,
        )
        if schema >= 6
        else ()
    )
    build_repositories = _parse_build_repositories(data.get("build_repositories"), schema=schema)
    commands_raw = data.get("commands", {})
    if not isinstance(commands_raw, dict):
        raise SkillSpecError(f"{source_file} field 'commands' must be an object")
    commands: dict[str, CommandSpec] = {}
    command_items = sorted(commands_raw.items()) if schema >= 6 else commands_raw.items()
    for name, raw in command_items:
        if not isinstance(name, str) or not name:
            raise SkillSpecError("Command names must be non-empty strings")
        if not is_valid_identifier(name):
            raise SkillSpecError(f"Command name {name!r} {IDENTIFIER_RULE}")
        if not isinstance(raw, dict):
            raise SkillSpecError(f"Command {name!r} must be an object")
        if schema == 1:
            reserved_fields = sorted(raw.keys() & _SCHEMA_V1_RESERVED_COMMAND_FIELDS)
            if reserved_fields:
                joined = ", ".join(repr(key) for key in reserved_fields)
                raise SkillSpecError(f"commands.{name} has unsupported field(s): {joined}")
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
                source=source_file,
            )
        elif command_type == "system":
            if schema >= 2:
                _reject_unknown_fields(raw, {"type", "command", "hint"}, f"commands.{name}")
            command = raw.get("command")
            if not isinstance(command, str) or not command:
                raise SkillSpecError(f"System command {name!r} requires non-empty 'command'")
            if schema >= 6 and not is_valid_identifier(command):
                raise SkillSpecError(f"commands.{name}.command system command {command!r} {IDENTIFIER_RULE}")
            hint = raw.get("hint")
            if hint is not None and not isinstance(hint, str):
                raise SkillSpecError(f"System command {name!r} field 'hint' must be a string")
            if schema >= 6 and "hint" in raw and hint == "":
                raise SkillSpecError(f"commands.{name}.hint must be a non-empty string")
            commands[name] = CommandSpec(
                name=name,
                type="system",
                command=command,
                hint=hint,
                source=source_file,
            )
        elif command_type == "build" and schema >= 6 and raw.get("driver") == GO_V1_DRIVER:
            _reject_unknown_fields(raw, {"type", "driver", "source_dir"}, f"commands.{name}")
            driver = raw.get("driver")
            source_dir = _validate_relative_path(
                raw.get("source_dir"),
                field=f"commands.{name}.source_dir",
                strict_posix=True,
            )
            commands[name] = CommandSpec(
                name=name,
                type="build",
                driver=driver,
                source_dir=source_dir,
                source=source_file,
            )
        elif command_type == "build" and schema >= 7 and raw.get("driver") == GO_REPOSITORY_V1_DRIVER:
            _reject_unknown_fields(raw, {"type", "driver", "repository", "target"}, f"commands.{name}")
            repository = raw.get("repository")
            target = raw.get("target")
            if not isinstance(repository, str) or not is_valid_identifier(repository):
                raise SkillSpecError(f"commands.{name}.repository must be a portable repository identifier")
            if not isinstance(target, str) or not is_valid_identifier(target):
                raise SkillSpecError(f"commands.{name}.target must be a portable target identifier")
            commands[name] = CommandSpec(
                name=name,
                type="build",
                driver=GO_REPOSITORY_V1_DRIVER,
                repository=repository,
                target=target,
                source=source_file,
            )
        elif command_type == "build" and schema >= 6:
            expected = (
                f"{GO_V1_DRIVER!r} or {GO_REPOSITORY_V1_DRIVER!r}"
                if schema >= 7
                else repr(GO_V1_DRIVER)
            )
            raise SkillSpecError(f"Command {name!r} field 'driver' must be {expected}")
        else:
            raise SkillSpecError(f"Command {name!r} has unsupported type {command_type!r}")
    if schema >= 6:
        _validate_build_layout(path.parent, build_roots, commands)
    if schema >= 7:
        _validate_repository_commands(build_repositories, commands)
    dependencies, requirements, mcp_servers = _parse_dependencies(
        data.get("dependencies"), schema=schema, source_file=source_file
    )
    return SkillSpec(
        commands=commands,
        source_file=source_file,
        schema_version=schema,
        runtime_roots=runtime_roots,
        build_roots=build_roots,
        capabilities=capabilities,
        dependencies=dependencies,
        requirements=requirements,
        mcp_servers=mcp_servers,
        build_repositories=build_repositories,
    ), data


def _parse_build_repositories(raw: Any, *, schema: int) -> dict[str, BuildRepository]:
    if schema < 7:
        return {}
    if raw is None:
        return {}
    if not isinstance(raw, dict) or not raw:
        raise SkillSpecError("build_repositories must be a non-empty object when present")
    repositories: dict[str, BuildRepository] = {}
    for name in sorted(raw):
        label = f"build_repositories.{name}"
        if not isinstance(name, str) or not is_valid_identifier(name):
            raise SkillSpecError(f"{label} repository name {IDENTIFIER_RULE}")
        entry = raw[name]
        if not isinstance(entry, dict):
            raise SkillSpecError(f"{label} must be an object")
        _reject_unknown_fields(entry, {"git", "locked_commit", "tag"}, label)
        git = entry.get("git")
        if not isinstance(git, str):
            raise SkillSpecError(f"{label}.git must be an HTTPS or SSH repository URL")
        try:
            source = parse_repository_source(git)
            locked_commit = parse_locked_commit(entry.get("locked_commit"), field=f"{label}.locked_commit")
        except BuildRepositoryError as exc:
            raise SkillSpecError(str(exc)) from exc
        tag = entry.get("tag")
        if tag is not None and (not isinstance(tag, str) or not is_valid_ref_name(tag)):
            raise SkillSpecError(f"{label}.tag must be a safe immutable Git tag name")
        repositories[name] = BuildRepository(
            name=name,
            git=git,
            identity=source.identity,
            transport=source.transport,
            locked_commit=locked_commit,
            tag=tag,
        )
    return repositories


def _validate_repository_commands(
    repositories: dict[str, BuildRepository], commands: dict[str, CommandSpec]
) -> None:
    selected: set[str] = set()
    for name in sorted(commands):
        command = commands[name]
        if command.driver != GO_REPOSITORY_V1_DRIVER:
            continue
        assert command.repository is not None
        if command.repository not in repositories:
            raise SkillSpecError(
                f"commands.{name}.repository selects undeclared build repository {command.repository!r}"
            )
        selected.add(command.repository)
    for name in sorted(repositories):
        if name not in selected:
            raise SkillSpecError(
                f"build_repositories.{name} is not selected by any {GO_REPOSITORY_V1_DRIVER} command"
            )


def _load_runtime_fallback(path: Path) -> SkillSpec:
    try:
        data = protocol_json.loads(path.read_bytes())
    except protocol_json.ProtocolJSONError as exc:
        raise SkillSpecError(f"Malformed JSON in {path}: {exc}") from exc
    commands_raw = data.get("commands", {}) if isinstance(data, dict) else {}
    if not isinstance(commands_raw, dict):
        raise SkillSpecError(f"{RUNTIME_FALLBACK} field 'commands' must be an object")
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
            source=RUNTIME_FALLBACK,
        )
    return SkillSpec(commands=commands, source_file=RUNTIME_FALLBACK)


def _parse_dependencies(
    raw: Any, *, schema: int, source_file: str = CANONICAL_MANIFEST
) -> tuple[dict[str, DependencySpec], dict[str, SkillRequirement], dict[str, McpServerRequirement]]:
    if raw is None:
        return {}, {}, {}
    if schema < 2:
        raise SkillSpecError(f"{source_file} field 'dependencies' requires schema_version 2 or newer")
    if not isinstance(raw, dict):
        raise SkillSpecError(f"{source_file} field 'dependencies' must be an object")
    if schema < 4 and "skills" in raw:
        raise SkillSpecError(f"{source_file} field 'dependencies.skills' requires schema_version 4")
    if schema < 5 and "mcp_servers" in raw:
        raise SkillSpecError(f"{source_file} field 'dependencies.mcp_servers' requires schema_version 5")
    allowed = {"commands"}
    if schema >= 4:
        allowed.add("skills")
    if schema >= 5:
        allowed.add("mcp_servers")
    _reject_unknown_fields(raw, allowed, "dependencies")
    requirements = _parse_requirements(raw.get("skills"), schema=schema, source_file=source_file)
    mcp_servers = _parse_mcp_servers(raw.get("mcp_servers"), source_file=source_file)
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
                source=source_file,
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
                source=source_file,
            )
        else:
            raise SkillSpecError(f"Dependency command {name!r} has unsupported type {dependency_type!r}")
    return dependencies, requirements, mcp_servers


def _parse_mcp_servers(
    raw: Any, *, source_file: str = CANONICAL_MANIFEST
) -> dict[str, McpServerRequirement]:
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
            source=source_file,
        )
    return servers


def _parse_requirements(
    raw: Any, *, schema: int, source_file: str = CANONICAL_MANIFEST
) -> dict[str, SkillRequirement]:
    if raw is None:
        return {}
    if schema < 4:
        raise SkillSpecError(f"{source_file} field 'dependencies.skills' requires schema_version 4")
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
            source=source_file,
        )
    return requirements


def _validate_relative_path(value: Any, *, field: str, strict_posix: bool = False) -> str:
    if not isinstance(value, str) or not value:
        raise SkillSpecError(f"{field} must be a non-empty string")
    if strict_posix and ("\\" in value or "//" in value):
        raise SkillSpecError(f"{field} must be a POSIX-style relative path inside the skill repository")
    if strict_posix and any(part in {"", "."} for part in value.split("/")):
        raise SkillSpecError(f"{field} must be a POSIX-style relative path inside the skill repository")
    path = PurePosixPath(value)
    if path.is_absolute() or ".." in path.parts:
        raise SkillSpecError(f"{field} must be a relative path inside the skill repository")
    if not path.parts:
        raise SkillSpecError(f"{field} must be a relative path inside the skill repository")
    if path.as_posix() != value or not is_valid_portable_path(value):
        raise SkillSpecError(f"{field} must be a portable relative path inside the skill repository")
    return value


def _parse_runtime_roots(
    raw: Any, *, snapshot: Path, source_file: str = CANONICAL_MANIFEST
) -> tuple[str, ...]:
    if not isinstance(raw, list):
        raise SkillSpecError(f"{source_file} field 'runtime_roots' must be a list")
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


def _parse_build_roots(
    raw: Any,
    *,
    snapshot: Path,
    runtime_roots: tuple[str, ...],
    source_file: str = CANONICAL_MANIFEST,
) -> tuple[str, ...]:
    if not isinstance(raw, list):
        raise SkillSpecError(f"{source_file} field 'build_roots' must be a list")
    roots: list[str] = []
    for index, value in enumerate(raw):
        field = f"build_roots[{index}]"
        root = _validate_relative_path(value, field=field, strict_posix=True)
        _validate_link_free_directory(snapshot, root, field=field, noun="build root")
        roots.append(root)

    if len(set(roots)) != len(roots):
        raise SkillSpecError("build roots must be unique after normalization")

    overlap = _overlapping_roots(roots)
    if overlap is not None:
        left, right = overlap
        raise SkillSpecError(f"build roots must be disjoint: {left} overlaps {right}")

    for build_root in roots:
        for runtime_root in runtime_roots:
            if _path_contains(build_root, runtime_root) or _path_contains(runtime_root, build_root):
                raise SkillSpecError(
                    f"build roots must not overlap runtime roots: {build_root} overlaps {runtime_root}"
                )
    return tuple(roots)


def _overlapping_roots(roots: list[str] | tuple[str, ...]) -> tuple[str, str] | None:
    sorted_roots = sorted(roots)
    for index, left in enumerate(sorted_roots):
        for right in sorted_roots[index + 1 :]:
            if _path_contains(left, right) or _path_contains(right, left):
                return left, right
    return None


def _validate_build_layout(
    snapshot: Path,
    build_roots: tuple[str, ...],
    commands: dict[str, CommandSpec],
) -> None:
    used_roots: set[str] = set()
    for name in sorted(commands):
        command = commands[name]
        if command.type != "build" or command.driver != GO_V1_DRIVER:
            continue
        source_dir = command.source_dir
        if source_dir is None:
            raise SkillSpecError(f"Command {name!r} field 'source_dir' must be a non-empty string")
        containing_roots = [root for root in build_roots if _path_contains(root, source_dir)]
        if len(containing_roots) != 1:
            raise SkillSpecError(
                f"commands.{name}.source_dir must be below exactly one build_roots entry"
            )
        build_root = containing_roots[0]
        field = f"commands.{name}.source_dir"
        _validate_link_free_directory(snapshot, source_dir, field=field, noun="source directory")
        _validate_nearest_go_module(snapshot, build_root, source_dir, field=field)
        used_roots.add(build_root)

    for index, root in enumerate(build_roots):
        if root not in used_roots:
            raise SkillSpecError(f"build_roots[{index}] build root {root!r} is not used by any build command")


def _validate_link_free_directory(snapshot: Path, rel_path: str, *, field: str, noun: str) -> None:
    current = snapshot
    for component in PurePosixPath(rel_path).parts:
        current /= component
        try:
            info = current.lstat()
        except FileNotFoundError as exc:
            raise SkillSpecError(f"{field} {noun} does not exist: {rel_path}") from exc
        except OSError as exc:
            raise SkillSpecError(f"{field} cannot inspect {noun} {rel_path}: {exc}") from exc
        if stat.S_ISLNK(info.st_mode):
            raise SkillSpecError(f"{field} {noun} must be link-free: {rel_path}")
        if not stat.S_ISDIR(info.st_mode):
            raise SkillSpecError(f"{field} {noun} must be a directory: {rel_path}")


def _validate_nearest_go_module(snapshot: Path, build_root: str, source_dir: str, *, field: str) -> None:
    root_path = PurePosixPath(build_root)
    current = PurePosixPath(source_dir)
    while True:
        module_path = snapshot.joinpath(*current.parts, "go.mod")
        try:
            info = module_path.lstat()
        except FileNotFoundError:
            info = None
        except OSError as exc:
            raise SkillSpecError(f"{field} cannot inspect nearest go.mod: {exc}") from exc
        if info is not None:
            if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
                raise SkillSpecError(
                    f"{field} nearest go.mod must be a real regular file in build root {build_root}"
                )
            if current != root_path:
                raise SkillSpecError(
                    f"{field} intervening module {current.as_posix()}/go.mod is below build root {build_root}"
                )
            return
        if current == root_path:
            raise SkillSpecError(
                f"{field} build root {build_root} must contain the nearest go.mod directly"
            )
        current = current.parent


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
