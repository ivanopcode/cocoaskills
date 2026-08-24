"""Manifest schema 8: enforced script policy and declared module roots."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from csk import skillspec


def _write_manifest(root: Path, payload: dict[str, Any], name: str = "agent-skill.json") -> None:
    (root / name).write_text(json.dumps(payload), encoding="utf-8")


def _script_files(root: Path) -> None:
    (root / "scripts").mkdir(parents=True, exist_ok=True)
    (root / "scripts" / "helper").write_text("#!/usr/bin/env python3\n", encoding="utf-8")


def _module(root: Path, relative: str, module_path: str) -> None:
    directory = root / Path(relative)
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "go.mod").write_text(f"module {module_path}\n\ngo 1.23\n", encoding="utf-8")


def _build_files(root: Path) -> None:
    (root / "tools" / "cli").mkdir(parents=True, exist_ok=True)
    (root / "tools" / "cli" / "go.mod").write_text("module example.com/cli\n\ngo 1.23\n", encoding="utf-8")
    (root / "tools" / "cli" / "main.go").write_text("package main\n", encoding="utf-8")


def _schema_v8(**changes: Any) -> dict[str, Any]:
    payload: dict[str, Any] = {"schema_version": 8, "capabilities": {}, "commands": {}}
    payload.update(changes)
    return payload


# --- enforced script execution policy -------------------------------------


@pytest.mark.parametrize("interpreter", ["python3-v1", "node-v1"])
def test_schema_v8_parses_the_enforced_script_execution_policy(tmp_path: Path, interpreter: str) -> None:
    _script_files(tmp_path)
    _write_manifest(
        tmp_path,
        _schema_v8(
            runtime_roots=["scripts"],
            commands={
                "helper": {
                    "type": "script",
                    "unix_path": "scripts/helper",
                    "execution_policy": "script-worker-v1",
                    "interpreter": interpreter,
                }
            },
        ),
    )

    spec = skillspec.load_skill_spec(tmp_path)

    assert spec.schema_version == 8
    assert spec.commands["helper"].execution_policy == "script-worker-v1"
    assert spec.commands["helper"].interpreter == interpreter


def test_schema_v8_keeps_an_unmarked_script_command_declared_only(tmp_path: Path) -> None:
    _script_files(tmp_path)
    _write_manifest(
        tmp_path,
        _schema_v8(
            runtime_roots=["scripts"],
            commands={"helper": {"type": "script", "unix_path": "scripts/helper"}},
        ),
    )

    spec = skillspec.load_skill_spec(tmp_path)

    assert spec.commands["helper"].execution_policy is None
    assert spec.commands["helper"].interpreter is None
    assert skillspec.script_execution_policy_rejection(spec) is None


@pytest.mark.parametrize(
    ("extra", "expected"),
    [
        ({"execution_policy": "script-worker-v1"}, "co-required"),
        ({"interpreter": "python3-v1"}, "co-required"),
    ],
)
def test_schema_v8_rejects_a_half_declared_execution_policy(
    tmp_path: Path, extra: dict[str, Any], expected: str
) -> None:
    _script_files(tmp_path)
    command: dict[str, Any] = {"type": "script", "unix_path": "scripts/helper"}
    command.update(extra)
    _write_manifest(tmp_path, _schema_v8(runtime_roots=["scripts"], commands={"helper": command}))

    with pytest.raises(skillspec.SkillSpecError, match=expected):
        skillspec.load_skill_spec(tmp_path)


@pytest.mark.parametrize(
    ("policy", "interpreter"),
    [
        ("manager-worker-v1", "python3-v1"),
        ("script-worker-v2", "python3-v1"),
        ("script-worker-hardened-v1", "python3-v1"),
        ("none", "python3-v1"),
        ("script-worker-v1", "bash-v1"),
        ("script-worker-v1", "powershell-v1"),
        ("script-worker-v1", "python3"),
    ],
)
def test_schema_v8_closes_the_policy_and_interpreter_value_spaces(
    tmp_path: Path, policy: str, interpreter: str
) -> None:
    _script_files(tmp_path)
    _write_manifest(
        tmp_path,
        _schema_v8(
            runtime_roots=["scripts"],
            commands={
                "helper": {
                    "type": "script",
                    "unix_path": "scripts/helper",
                    "execution_policy": policy,
                    "interpreter": interpreter,
                }
            },
        ),
    )

    with pytest.raises(skillspec.SkillSpecError, match="execution_policy|interpreter"):
        skillspec.load_skill_spec(tmp_path)


@pytest.mark.parametrize("field", ["execution_policy", "interpreter"])
def test_schema_v8_rejects_the_policy_fields_on_a_system_command(tmp_path: Path, field: str) -> None:
    _write_manifest(
        tmp_path,
        _schema_v8(commands={"git": {"type": "system", "command": "git", field: "script-worker-v1"}}),
    )

    with pytest.raises(skillspec.SkillSpecError, match="unsupported field"):
        skillspec.load_skill_spec(tmp_path)


@pytest.mark.parametrize("field", ["execution_policy", "interpreter", "modules"])
@pytest.mark.parametrize("schema", [1, 2, 3, 4, 5, 6, 7])
def test_schemas_one_through_seven_reject_the_schema_eight_command_fields(
    tmp_path: Path, schema: int, field: str
) -> None:
    _script_files(tmp_path)
    payload: dict[str, Any] = {
        "schema_version": schema,
        "commands": {"helper": {"type": "script", "unix_path": "scripts/helper", field: "value"}},
    }
    if schema >= 3:
        payload["capabilities"] = {}
    if schema >= 2:
        payload["runtime_roots"] = ["scripts"]
    _write_manifest(tmp_path, payload)

    with pytest.raises(skillspec.SkillSpecError, match="unsupported field"):
        skillspec.load_skill_spec(tmp_path)


@pytest.mark.parametrize("field", ["execution_policy", "interpreter", "modules"])
@pytest.mark.parametrize("schema", [1, 2, 3, 4, 5, 6, 7])
def test_schemas_one_through_seven_reject_the_schema_eight_top_level_fields(
    tmp_path: Path, schema: int, field: str
) -> None:
    payload: dict[str, Any] = {"schema_version": schema, "commands": {}, field: "value"}
    if schema >= 3:
        payload["capabilities"] = {}
    _write_manifest(tmp_path, payload)

    with pytest.raises(skillspec.SkillSpecError, match="unsupported field"):
        skillspec.load_skill_spec(tmp_path)


def test_schema_v8_rejects_modules_at_the_top_level(tmp_path: Path) -> None:
    _write_manifest(tmp_path, _schema_v8(modules=["pkg/board"]))

    with pytest.raises(skillspec.SkillSpecError, match="unsupported field"):
        skillspec.load_skill_spec(tmp_path)


def test_enforced_script_commands_are_refused_rather_than_downgraded(tmp_path: Path) -> None:
    _script_files(tmp_path)
    _write_manifest(
        tmp_path,
        _schema_v8(
            runtime_roots=["scripts"],
            commands={
                "helper": {
                    "type": "script",
                    "unix_path": "scripts/helper",
                    "execution_policy": "script-worker-v1",
                    "interpreter": "python3-v1",
                },
                "plain": {"type": "script", "unix_path": "scripts/helper"},
            },
        ),
    )

    spec = skillspec.load_skill_spec(tmp_path)
    rejection = skillspec.script_execution_policy_rejection(spec)

    assert rejection is not None
    assert rejection.startswith("script_execution_policy_unsupported: ")
    assert "helper (script-worker-v1)" in rejection
    assert "plain" not in rejection
    assert [command.name for command in skillspec.enforced_script_commands(spec)] == ["helper"]


# --- declared module roots -------------------------------------------------


def _module_roots_manifest(modules: list[str] | None, runtime_roots: list[str]) -> dict[str, Any]:
    command: dict[str, Any] = {
        "type": "build",
        "driver": "go-v1",
        "source_dir": "tools/cli",
    }
    if modules is not None:
        command["modules"] = modules
    return _schema_v8(
        build_roots=["tools/cli"],
        runtime_roots=runtime_roots,
        commands={"cli": command},
    )


def test_schema_v8_parses_declared_module_roots(tmp_path: Path) -> None:
    _build_files(tmp_path)
    (tmp_path / "scripts").mkdir()
    _module(tmp_path, "pkg/board", "example.com/board")
    _module(tmp_path, "pkg/remoteconfig", "example.com/remoteconfig")
    _write_manifest(tmp_path, _module_roots_manifest(["pkg/board", "pkg/remoteconfig"], ["scripts"]))

    spec = skillspec.load_skill_spec(tmp_path)

    assert spec.commands["cli"].modules == ("pkg/board", "pkg/remoteconfig")


@pytest.mark.parametrize("modules", [None, []])
def test_an_absent_or_empty_module_list_keeps_the_schema_seven_meaning(
    tmp_path: Path, modules: list[str] | None
) -> None:
    _build_files(tmp_path)
    (tmp_path / "scripts").mkdir()
    _write_manifest(tmp_path, _module_roots_manifest(modules, ["scripts"]))

    spec = skillspec.load_skill_spec(tmp_path)

    assert spec.commands["cli"].modules == ()


def test_an_explicit_null_module_list_is_not_a_spelling_of_absence(tmp_path: Path) -> None:
    _build_files(tmp_path)
    (tmp_path / "scripts").mkdir()
    payload = _module_roots_manifest([], ["scripts"])
    payload["commands"]["cli"]["modules"] = None
    _write_manifest(tmp_path, payload)

    with pytest.raises(skillspec.SkillSpecError, match="modules"):
        skillspec.load_skill_spec(tmp_path)


@pytest.mark.parametrize("field", ["execution_policy", "interpreter"])
def test_an_explicit_null_policy_field_is_not_a_spelling_of_absence(
    tmp_path: Path, field: str
) -> None:
    _script_files(tmp_path)
    command: dict[str, Any] = {"type": "script", "unix_path": "scripts/helper", field: None}
    _write_manifest(tmp_path, _schema_v8(runtime_roots=["scripts"], commands={"helper": command}))

    with pytest.raises(skillspec.SkillSpecError, match="co-required"):
        skillspec.load_skill_spec(tmp_path)


@pytest.mark.parametrize(
    "module",
    [
        ".",
        "/pkg/board",
        "pkg\\board",
        "../pkg/board",
        "pkg/./board",
        "pkg/board/",
        "pkg/con/board",
    ],
)
def test_a_non_portable_module_declaration_is_rejected(tmp_path: Path, module: str) -> None:
    _build_files(tmp_path)
    (tmp_path / "scripts").mkdir()
    _module(tmp_path, "pkg/board", "example.com/board")
    _write_manifest(tmp_path, _module_roots_manifest([module], ["scripts"]))

    with pytest.raises(skillspec.SkillSpecError):
        skillspec.load_skill_spec(tmp_path)


@pytest.mark.parametrize("modules", [["pkg/board", "pkg/board"], [1], "pkg/board", [None], {}])
def test_a_malformed_module_list_is_rejected(tmp_path: Path, modules: Any) -> None:
    _build_files(tmp_path)
    (tmp_path / "scripts").mkdir()
    _module(tmp_path, "pkg/board", "example.com/board")
    _write_manifest(tmp_path, _module_roots_manifest(modules, ["scripts"]))

    with pytest.raises(skillspec.SkillSpecError):
        skillspec.load_skill_spec(tmp_path)


def test_a_module_directory_without_a_direct_go_mod_is_rejected(tmp_path: Path) -> None:
    _build_files(tmp_path)
    (tmp_path / "scripts").mkdir()
    (tmp_path / "pkg" / "board").mkdir(parents=True)
    _write_manifest(tmp_path, _module_roots_manifest(["pkg/board"], ["scripts"]))

    with pytest.raises(skillspec.SkillSpecError, match="build_module_root_declaration_invalid"):
        skillspec.load_skill_spec(tmp_path)


def test_a_missing_module_directory_is_rejected(tmp_path: Path) -> None:
    _build_files(tmp_path)
    (tmp_path / "scripts").mkdir()
    _write_manifest(tmp_path, _module_roots_manifest(["pkg/board"], ["scripts"]))

    with pytest.raises(skillspec.SkillSpecError, match="build_module_root_declaration_invalid"):
        skillspec.load_skill_spec(tmp_path)


def test_nested_declared_module_roots_are_rejected(tmp_path: Path) -> None:
    _build_files(tmp_path)
    (tmp_path / "scripts").mkdir()
    _module(tmp_path, "pkg/board", "example.com/board")
    _module(tmp_path, "pkg/board/codec", "example.com/board/codec")
    _write_manifest(tmp_path, _module_roots_manifest(["pkg/board", "pkg/board/codec"], ["scripts"]))

    with pytest.raises(skillspec.SkillSpecError, match="build_module_root_containment_invalid"):
        skillspec.load_skill_spec(tmp_path)


def test_a_module_root_below_the_build_root_is_rejected(tmp_path: Path) -> None:
    _build_files(tmp_path)
    (tmp_path / "scripts").mkdir()
    _module(tmp_path, "tools/cli/pkg/lib", "example.com/lib")
    _write_manifest(tmp_path, _module_roots_manifest(["tools/cli/pkg/lib"], ["scripts"]))

    with pytest.raises(skillspec.SkillSpecError, match="build_module_root_containment_invalid"):
        skillspec.load_skill_spec(tmp_path)


def test_a_module_root_below_a_runtime_root_is_rejected(tmp_path: Path) -> None:
    _build_files(tmp_path)
    _module(tmp_path, "pkg/board", "example.com/board")
    _write_manifest(tmp_path, _module_roots_manifest(["pkg/board"], ["pkg"]))

    with pytest.raises(skillspec.SkillSpecError, match="build_module_root_containment_invalid"):
        skillspec.load_skill_spec(tmp_path)


def test_case_colliding_module_roots_are_rejected_on_every_host(tmp_path: Path) -> None:
    _build_files(tmp_path)
    (tmp_path / "scripts").mkdir()
    _module(tmp_path, "pkg/board", "example.com/board")
    lower = tmp_path / "pkg" / "board"
    upper = tmp_path / "pkg" / "Board"
    if not upper.exists():
        # A case-insensitive host already resolves both spellings to one
        # directory; the declaration is still two distinct protocol paths.
        upper.mkdir(parents=True, exist_ok=True)
        (upper / "go.mod").write_text("module example.com/upper\n\ngo 1.23\n", encoding="utf-8")
    assert lower.is_dir()
    _write_manifest(tmp_path, _module_roots_manifest(["pkg/Board", "pkg/board"], ["scripts"]))

    with pytest.raises(skillspec.SkillSpecError, match="build_module_root_containment_invalid"):
        skillspec.load_skill_spec(tmp_path)


@pytest.mark.parametrize(
    "command",
    [
        {"type": "script", "unix_path": "scripts/helper", "modules": ["pkg/board"]},
        {"type": "system", "command": "git", "modules": ["pkg/board"]},
    ],
)
def test_modules_are_rejected_on_a_non_build_command(tmp_path: Path, command: dict[str, Any]) -> None:
    _script_files(tmp_path)
    _write_manifest(tmp_path, _schema_v8(runtime_roots=["scripts"], commands={"helper": command}))

    with pytest.raises(skillspec.SkillSpecError, match="unsupported field"):
        skillspec.load_skill_spec(tmp_path)


def test_modules_are_rejected_on_a_repository_build_command(tmp_path: Path) -> None:
    _write_manifest(
        tmp_path,
        _schema_v8(
            build_repositories={
                "tools": {
                    "git": "https://example.com/tools.git",
                    "locked_commit": {"object_format": "sha1", "hex": "a" * 40},
                }
            },
            commands={
                "tool": {
                    "type": "build",
                    "driver": "go-repository-v1",
                    "repository": "tools",
                    "target": "tool",
                    "modules": ["pkg/board"],
                }
            },
        ),
    )

    with pytest.raises(skillspec.SkillSpecError, match="unsupported field"):
        skillspec.load_skill_spec(tmp_path)
