from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any


SCHEMA_VERSION = 1


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
class SkillSpec:
    commands: dict[str, CommandSpec]
    source_file: str | None


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
    if schema != SCHEMA_VERSION:
        raise SkillSpecError(
            f"Unsupported csk-skill.json schema_version {schema!r}; this skill requires a newer csk"
        )
    commands_raw = data.get("commands", {})
    if not isinstance(commands_raw, dict):
        raise SkillSpecError("csk-skill.json field 'commands' must be an object")
    commands: dict[str, CommandSpec] = {}
    for name, raw in commands_raw.items():
        if not isinstance(name, str) or not name:
            raise SkillSpecError("Command names must be non-empty strings")
        if not isinstance(raw, dict):
            raise SkillSpecError(f"Command {name!r} must be an object")
        command_type = raw.get("type")
        if command_type == "script":
            unix_path = raw.get("unix_path")
            win_path = raw.get("win_path")
            if unix_path is not None:
                _validate_relative_path(unix_path, field=f"commands.{name}.unix_path")
            if win_path is not None:
                _validate_relative_path(win_path, field=f"commands.{name}.win_path")
            commands[name] = CommandSpec(
                name=name,
                type="script",
                unix_path=unix_path,
                win_path=win_path,
                source="csk-skill.json",
            )
        elif command_type == "system":
            command = raw.get("command")
            if not isinstance(command, str) or not command:
                raise SkillSpecError(f"System command {name!r} requires non-empty 'command'")
            commands[name] = CommandSpec(
                name=name,
                type="system",
                command=command,
                hint=raw.get("hint") if isinstance(raw.get("hint"), str) else None,
                source="csk-skill.json",
            )
        else:
            raise SkillSpecError(f"Command {name!r} has unsupported type {command_type!r}")
    return SkillSpec(commands=commands, source_file="csk-skill.json")


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


def _validate_relative_path(value: Any, *, field: str) -> None:
    if not isinstance(value, str) or not value:
        raise SkillSpecError(f"{field} must be a non-empty string")
    path = PurePosixPath(value)
    if path.is_absolute() or ".." in path.parts:
        raise SkillSpecError(f"{field} must be a relative path inside the skill repository")

