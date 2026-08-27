"""Schema-8 manifest parsing: declared module roots and the script opt-in.

Protocol Core 4.2.3 defines the declaration side of first-party module roots
and 4.1.1 defines the closed script execution-policy opt-in. Both are checked
here, at the manifest layer, because both reject before any Go probe or shim
publication runs.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from csk import skillspec


def _write_json(path: Path, payload: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")


def _module(snapshot: Path, relative: str) -> None:
    directory = snapshot / relative
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "go.mod").write_text(
        f"module example.test/{relative.replace('/', '-')}\ngo 1.25\n",
        encoding="utf-8",
    )


def _prepare(snapshot: Path) -> None:
    (snapshot / "scripts").mkdir(parents=True, exist_ok=True)
    (snapshot / "scripts" / "tool").write_text("#!/bin/sh\n", encoding="utf-8")
    (snapshot / "scripts" / "tool.cmd").write_text("@echo off\r\n", encoding="utf-8")
    (snapshot / "tools" / "cli" / "cmd" / "tool").mkdir(parents=True, exist_ok=True)
    (snapshot / "tools" / "cli" / "go.mod").write_text(
        "module example.test/cli\ngo 1.25\n",
        encoding="utf-8",
    )
    _module(snapshot, "pkg/board")
    _module(snapshot, "pkg/remoteconfig")


def _build_command(**overrides: object) -> dict[str, object]:
    command: dict[str, object] = {
        "type": "build",
        "driver": "go-v1",
        "source_dir": "tools/cli/cmd/tool",
    }
    command.update(overrides)
    return command


def _manifest(
    *,
    command: dict[str, object] | None = None,
    **overrides: object,
) -> dict[str, object]:
    if command is None:
        command = {
            "type": "build",
            "driver": "go-v1",
            "source_dir": "tools/cli/cmd/tool",
            "modules": ["pkg/board", "pkg/remoteconfig"],
        }
    payload: dict[str, object] = {
        "schema_version": 8,
        "capabilities": {},
        "build_roots": ["tools/cli"],
        "commands": {"tool": command},
    }
    payload.update(overrides)
    return payload


def test_schema_v8_parses_declared_module_roots(tmp_path: Path) -> None:
    _prepare(tmp_path)
    _write_json(tmp_path / "agent-skill.json", _manifest())

    spec = skillspec.load_skill_spec(tmp_path)

    assert spec.schema_version == 8
    assert spec.commands["tool"].modules == ("pkg/board", "pkg/remoteconfig")


@pytest.mark.parametrize("declared", [None, []])
def test_absent_and_empty_module_lists_carry_the_schema_six_meaning(
    tmp_path: Path, declared: list[str] | None
) -> None:
    _prepare(tmp_path)
    command: dict[str, object] = {
        "type": "build",
        "driver": "go-v1",
        "source_dir": "tools/cli/cmd/tool",
    }
    if declared is not None:
        command["modules"] = declared
    _write_json(tmp_path / "agent-skill.json", _manifest(command=command))

    spec = skillspec.load_skill_spec(tmp_path)

    assert spec.commands["tool"].modules == ()


@pytest.mark.parametrize("schema", [6, 7])
def test_schema_six_and_seven_reject_modules_as_an_unknown_command_field(
    tmp_path: Path, schema: int
) -> None:
    _prepare(tmp_path)
    payload = _manifest()
    payload["schema_version"] = schema
    _write_json(tmp_path / "agent-skill.json", payload)

    with pytest.raises(skillspec.SkillSpecError, match="'modules'"):
        skillspec.load_skill_spec(tmp_path)


@pytest.mark.parametrize("schema", [1, 2, 3, 4, 5])
def test_schemas_one_through_five_reject_the_whole_build_surface(
    tmp_path: Path, schema: int
) -> None:
    """These schemas reject ``build_roots`` and every build-only field first.

    The version gate is downward and cumulative, so a schema-8 command never
    reaches the ``modules`` check on a manifest that cannot carry a build root
    at all.
    """

    _prepare(tmp_path)
    payload = _manifest()
    payload["schema_version"] = schema
    _write_json(tmp_path / "agent-skill.json", payload)

    with pytest.raises(skillspec.SkillSpecError, match="build_roots"):
        skillspec.load_skill_spec(tmp_path)


def test_top_level_modules_is_an_unknown_field(tmp_path: Path) -> None:
    _prepare(tmp_path)
    _write_json(tmp_path / "agent-skill.json", _manifest(modules=["pkg/board"]))

    with pytest.raises(skillspec.SkillSpecError, match="'modules'"):
        skillspec.load_skill_spec(tmp_path)


@pytest.mark.parametrize(
    "command",
    [
        {"type": "script", "unix_path": "scripts/tool", "modules": ["pkg/board"]},
        {"type": "system", "command": "git", "modules": ["pkg/board"]},
        {
            "type": "build",
            "driver": "go-repository-v1",
            "repository": "tools",
            "target": "tool",
            "modules": ["pkg/board"],
        },
    ],
)
def test_modules_belongs_to_the_local_go_v1_command_only(
    tmp_path: Path, command: dict[str, object]
) -> None:
    _prepare(tmp_path)
    payload = _manifest()
    commands = payload["commands"]
    assert isinstance(commands, dict)
    commands["other"] = command
    _write_json(tmp_path / "agent-skill.json", payload)

    with pytest.raises(skillspec.SkillSpecError):
        skillspec.load_skill_spec(tmp_path)


@pytest.mark.parametrize(
    "value",
    [".", "/pkg/board", "pkg\\board", "../board", "pkg//board", "pkg/./board", 7, None],
)
def test_module_root_must_be_a_portable_relative_directory_path(
    tmp_path: Path, value: object
) -> None:
    _prepare(tmp_path)
    _write_json(
        tmp_path / "agent-skill.json",
        _manifest(command=_build_command(modules=[value])),
    )

    with pytest.raises(
        skillspec.SkillSpecError,
        match=skillspec.MODULE_ROOT_CONTAINMENT_INVALID,
    ):
        skillspec.load_skill_spec(tmp_path)


def test_duplicate_module_roots_are_rejected(tmp_path: Path) -> None:
    _prepare(tmp_path)
    _write_json(
        tmp_path / "agent-skill.json",
        _manifest(command=_build_command(modules=["pkg/board", "pkg/board"])),
    )

    with pytest.raises(
        skillspec.SkillSpecError,
        match=skillspec.MODULE_ROOT_CONTAINMENT_INVALID,
    ):
        skillspec.load_skill_spec(tmp_path)


def test_module_root_must_exist_in_the_snapshot(tmp_path: Path) -> None:
    _prepare(tmp_path)
    _write_json(
        tmp_path / "agent-skill.json",
        _manifest(command=_build_command(modules=["pkg/absent"])),
    )

    with pytest.raises(
        skillspec.SkillSpecError,
        match=skillspec.MODULE_ROOT_CONTAINMENT_INVALID,
    ):
        skillspec.load_skill_spec(tmp_path)


def test_module_root_must_contain_go_mod_directly(tmp_path: Path) -> None:
    _prepare(tmp_path)
    (tmp_path / "pkg" / "plain").mkdir(parents=True)
    _write_json(
        tmp_path / "agent-skill.json",
        _manifest(command=_build_command(modules=["pkg/plain"])),
    )

    with pytest.raises(
        skillspec.SkillSpecError,
        match=skillspec.MODULE_ROOT_CONTAINMENT_INVALID,
    ):
        skillspec.load_skill_spec(tmp_path)


def test_nested_module_roots_are_not_pairwise_disjoint(tmp_path: Path) -> None:
    _prepare(tmp_path)
    _module(tmp_path, "pkg/board/codec")
    _write_json(
        tmp_path / "agent-skill.json",
        _manifest(command=_build_command(modules=["pkg/board", "pkg/board/codec"])),
    )

    with pytest.raises(
        skillspec.SkillSpecError,
        match=skillspec.MODULE_ROOT_CONTAINMENT_INVALID,
    ):
        skillspec.load_skill_spec(tmp_path)


def test_module_root_may_not_be_contained_by_a_build_root(tmp_path: Path) -> None:
    _prepare(tmp_path)
    _module(tmp_path, "tools/cli/pkg/lib")
    _write_json(
        tmp_path / "agent-skill.json",
        _manifest(command=_build_command(modules=["tools/cli/pkg/lib"])),
    )

    with pytest.raises(
        skillspec.SkillSpecError,
        match=skillspec.MODULE_ROOT_CONTAINMENT_INVALID,
    ):
        skillspec.load_skill_spec(tmp_path)


def test_module_root_may_not_be_contained_by_a_runtime_root(tmp_path: Path) -> None:
    _prepare(tmp_path)
    _write_json(
        tmp_path / "agent-skill.json",
        _manifest(
            runtime_roots=["pkg"],
            command=_build_command(modules=["pkg/board"]),
        ),
    )

    with pytest.raises(
        skillspec.SkillSpecError,
        match=skillspec.MODULE_ROOT_CONTAINMENT_INVALID,
    ):
        skillspec.load_skill_spec(tmp_path)


def test_module_roots_colliding_only_under_a_platform_folding_are_rejected(
    tmp_path: Path,
) -> None:
    """Two declarations that differ only by case collide on Windows and macOS.

    The comparison must reject them even on a case-sensitive host, where only
    the fold makes the collision visible.
    """

    _prepare(tmp_path)
    _module(tmp_path, "pkg/board")
    upper = tmp_path / "pkg" / "Board"
    if not upper.exists():
        _module(tmp_path, "pkg/Board")
    _write_json(
        tmp_path / "agent-skill.json",
        _manifest(command=_build_command(modules=["pkg/Board", "pkg/board"])),
    )

    with pytest.raises(
        skillspec.SkillSpecError,
        match=skillspec.MODULE_ROOT_CONTAINMENT_INVALID,
    ):
        skillspec.load_skill_spec(tmp_path)
