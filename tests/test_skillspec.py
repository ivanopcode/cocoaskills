from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

import pytest

from csk import skillspec


def _write_manifest(tmp_path: Path, payload: dict[str, Any], name: str = "csk-skill.json") -> None:
    (tmp_path / name).write_text(json.dumps(payload), encoding="utf-8")


def _build_files(tmp_path: Path, root: str = "build", source_dir: str = "build/cmd/tool") -> None:
    (tmp_path / root).mkdir(parents=True, exist_ok=True)
    (tmp_path / root / "go.mod").write_text("module example.com/tool\n\ngo 1.23\n", encoding="utf-8")
    (tmp_path / source_dir).mkdir(parents=True, exist_ok=True)
    (tmp_path / source_dir / "main.go").write_text("package main\n", encoding="utf-8")


def _schema_v6_manifest(
    *,
    build_roots: list[str] | None = None,
    commands: dict[str, Any] | None = None,
    runtime_roots: list[str] | None = None,
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "schema_version": 6,
        "capabilities": {},
        "commands": commands or {},
    }
    if build_roots is not None:
        payload["build_roots"] = build_roots
    if runtime_roots is not None:
        payload["runtime_roots"] = runtime_roots
    return payload


def _build_command(source_dir: str = "build/cmd/tool", **extra: Any) -> dict[str, Any]:
    command: dict[str, Any] = {
        "type": "build",
        "driver": "go-v1",
        "source_dir": source_dir,
    }
    command.update(extra)
    return command


def test_agent_skill_json_is_canonical(tmp_path):
    (tmp_path / "agents").mkdir()
    (tmp_path / "agents" / "runtime.json").write_text(
        json.dumps({"commands": {"runtime": "scripts/runtime"}}),
        encoding="utf-8",
    )
    (tmp_path / "agent-skill.json").write_text(
        json.dumps({"schema_version": 1, "commands": {"current": {"type": "system", "command": "current"}}}),
        encoding="utf-8",
    )

    spec = skillspec.load_skill_spec(tmp_path)

    assert spec.source_file == "agent-skill.json"
    assert list(spec.commands) == ["current"]
    assert spec.commands["current"].source == "agent-skill.json"


def test_equal_dual_manifests_select_canonical(tmp_path):
    (tmp_path / "agent-skill.json").write_text(
        '{"schema_version":1,"commands":{}}',
        encoding="utf-8",
    )
    (tmp_path / "csk-skill.json").write_text(
        '{\n  "commands": {},\n  "schema_version": 1\n}',
        encoding="utf-8",
    )

    spec = skillspec.load_skill_spec(tmp_path)

    assert spec.source_file == "agent-skill.json"


def test_conflicting_dual_manifests_fail_closed(tmp_path):
    (tmp_path / "agent-skill.json").write_text(
        json.dumps({"schema_version": 1, "commands": {}}),
        encoding="utf-8",
    )
    (tmp_path / "csk-skill.json").write_text(
        json.dumps({"schema_version": 1, "commands": {"legacy": {"type": "system", "command": "legacy"}}}),
        encoding="utf-8",
    )

    with pytest.raises(skillspec.SkillSpecError, match="conflicting_skill_manifests"):
        skillspec.load_skill_spec(tmp_path)


@pytest.mark.parametrize("invalid_name", ["agent-skill.json", "csk-skill.json"])
def test_invalid_dual_manifest_peer_is_not_ignored(tmp_path, invalid_name):
    valid = json.dumps({"schema_version": 1, "commands": {}})
    for name in ("agent-skill.json", "csk-skill.json"):
        (tmp_path / name).write_text("{" if name == invalid_name else valid, encoding="utf-8")

    with pytest.raises(skillspec.SkillSpecError):
        skillspec.load_skill_spec(tmp_path)


def test_csk_skill_json_takes_precedence_over_runtime_json(tmp_path):
    (tmp_path / "agents").mkdir()
    (tmp_path / "agents" / "runtime.json").write_text(
        json.dumps({"commands": {"legacy": "scripts/legacy"}}),
        encoding="utf-8",
    )
    (tmp_path / "csk-skill.json").write_text(
        json.dumps({"schema_version": 1, "commands": {"new": {"type": "script", "unix_path": "scripts/new"}}}),
        encoding="utf-8",
    )
    spec = skillspec.load_skill_spec(tmp_path)
    assert list(spec.commands) == ["new"]


def test_runtime_json_fallback(tmp_path):
    (tmp_path / "agents").mkdir()
    (tmp_path / "agents" / "runtime.json").write_text(
        json.dumps({"commands": {"legacy": "scripts/legacy"}}),
        encoding="utf-8",
    )
    spec = skillspec.load_skill_spec(tmp_path)
    assert spec.commands["legacy"].unix_path == "scripts/legacy"
    assert spec.commands["legacy"].source == "agents/runtime.json"


def test_csk_skill_schema_v2_parses_runtime_roots(tmp_path):
    (tmp_path / "scripts").mkdir()
    (tmp_path / "scripts" / "tool").write_text("#!/bin/sh\n", encoding="utf-8")
    (tmp_path / "csk-skill.json").write_text(
        json.dumps(
            {
                "schema_version": 2,
                "runtime_roots": ["scripts"],
                "commands": {"tool": {"type": "script", "unix_path": "scripts/tool"}},
            }
        ),
        encoding="utf-8",
    )

    spec = skillspec.load_skill_spec(tmp_path)

    assert spec.schema_version == 2
    assert spec.runtime_roots == ("scripts",)
    assert spec.commands["tool"].unix_path == "scripts/tool"


def test_runtime_root_must_be_relative_directory(tmp_path):
    (tmp_path / "scripts").mkdir()
    (tmp_path / "scripts" / "tool").write_text("#!/bin/sh\n", encoding="utf-8")
    cases = [
        ["/scripts"],
        ["../scripts"],
        ["."],
        ["scripts/./lib"],
        ["scripts/"],
        ["scripts/missing"],
        ["scripts/tool"],
        None,
    ]

    for runtime_roots in cases:
        (tmp_path / "csk-skill.json").write_text(
            json.dumps({"schema_version": 2, "runtime_roots": runtime_roots, "commands": {}}),
            encoding="utf-8",
        )
        with pytest.raises(skillspec.SkillSpecError):
            skillspec.load_skill_spec(tmp_path)


def test_runtime_roots_must_be_disjoint(tmp_path):
    (tmp_path / "scripts" / "lib").mkdir(parents=True)
    (tmp_path / "scripts" / "tool").write_text("#!/bin/sh\n", encoding="utf-8")
    (tmp_path / "csk-skill.json").write_text(
        json.dumps({"schema_version": 2, "runtime_roots": ["scripts", "scripts/lib"], "commands": {}}),
        encoding="utf-8",
    )

    with pytest.raises(skillspec.SkillSpecError, match="disjoint"):
        skillspec.load_skill_spec(tmp_path)


def test_script_command_must_be_inside_runtime_root(tmp_path):
    (tmp_path / "scripts").mkdir()
    (tmp_path / "bin").mkdir()
    (tmp_path / "bin" / "tool").write_text("#!/bin/sh\n", encoding="utf-8")
    (tmp_path / "csk-skill.json").write_text(
        json.dumps(
            {
                "schema_version": 2,
                "runtime_roots": ["scripts"],
                "commands": {"tool": {"type": "script", "unix_path": "bin/tool"}},
            }
        ),
        encoding="utf-8",
    )

    with pytest.raises(skillspec.SkillSpecError, match="not inside any runtime_roots"):
        skillspec.load_skill_spec(tmp_path)


def test_system_command_rejects_install_check_and_post_install_fields(tmp_path):
    for forbidden in ("install", "check", "post_install", "script", "command_args"):
        (tmp_path / "csk-skill.json").write_text(
            json.dumps(
                {
                    "schema_version": 2,
                    "commands": {
                        "tool": {
                            "type": "system",
                            "command": "tool",
                            forbidden: "echo bad",
                        }
                    },
                }
            ),
            encoding="utf-8",
        )
        with pytest.raises(skillspec.SkillSpecError, match=forbidden):
            skillspec.load_skill_spec(tmp_path)


def test_csk_skill_schema_v3_parses_capabilities(tmp_path):
    (tmp_path / "scripts").mkdir()
    (tmp_path / "scripts" / "tool").write_text("#!/bin/sh\n", encoding="utf-8")
    (tmp_path / "csk-skill.json").write_text(
        json.dumps(
            {
                "schema_version": 3,
                "runtime_roots": ["scripts"],
                "capabilities": {
                    "network": ["gitlab.example.com"],
                    "filesystem": "home-config",
                    "exec": ["glab"],
                    "secrets": ["GITLAB_TOKEN"],
                    "env_read": ["HOME"],
                    "prompt_scope": "Reads merge request metadata.",
                },
                "commands": {"tool": {"type": "script", "unix_path": "scripts/tool"}},
            }
        ),
        encoding="utf-8",
    )

    spec = skillspec.load_skill_spec(tmp_path)

    assert spec.schema_version == 3
    assert spec.runtime_roots == ("scripts",)
    assert spec.capabilities.network == ("gitlab.example.com",)
    assert spec.capabilities.filesystem == "home-config"
    assert spec.capabilities.exec == ("glab",)
    assert spec.capabilities.secrets == ("GITLAB_TOKEN",)
    assert spec.capabilities.env_read == ("HOME",)


def test_csk_skill_schema_v3_parses_command_dependencies(tmp_path):
    (tmp_path / "csk-skill.json").write_text(
        json.dumps(
            {
                "schema_version": 3,
                "capabilities": {"network": "none", "exec": "none"},
                "commands": {},
                "dependencies": {
                    "commands": {
                        "wk": {
                            "type": "skill",
                            "skill": "skill-docs",
                            "command": "wk",
                            "hint": "Add skill-docs to Skillfile.json.",
                        },
                        "wiki": {
                            "type": "system",
                            "command": "wiki",
                            "hint": "Install wb-wiki-cli.",
                        },
                    }
                },
            }
        ),
        encoding="utf-8",
    )

    spec = skillspec.load_skill_spec(tmp_path)

    assert spec.dependencies["wk"].type == "skill"
    assert spec.dependencies["wk"].skill == "skill-docs"
    assert spec.dependencies["wk"].command == "wk"
    assert spec.dependencies["wiki"].type == "system"
    assert spec.dependencies["wiki"].command == "wiki"


def test_dependency_rejects_unknown_fields(tmp_path):
    (tmp_path / "csk-skill.json").write_text(
        json.dumps(
            {
                "schema_version": 2,
                "commands": {},
                "dependencies": {
                    "commands": {
                        "wk": {
                            "type": "skill",
                            "skill": "skill-docs",
                            "command": "wk",
                            "install": "echo bad",
                        }
                    }
                },
            }
        ),
        encoding="utf-8",
    )

    with pytest.raises(skillspec.SkillSpecError, match="install"):
        skillspec.load_skill_spec(tmp_path)


def test_csk_skill_schema_v3_requires_capabilities(tmp_path):
    (tmp_path / "csk-skill.json").write_text(
        json.dumps({"schema_version": 3, "commands": {}}),
        encoding="utf-8",
    )

    with pytest.raises(skillspec.SkillSpecError, match="requires 'capabilities'"):
        skillspec.load_skill_spec(tmp_path)


def test_csk_skill_schema_v3_rejects_unknown_capability_fields(tmp_path):
    (tmp_path / "csk-skill.json").write_text(
        json.dumps(
            {
                "schema_version": 3,
                "capabilities": {"network": "none", "post_install": "curl | sh"},
                "commands": {},
            }
        ),
        encoding="utf-8",
    )

    with pytest.raises(skillspec.SkillSpecError, match="post_install"):
        skillspec.load_skill_spec(tmp_path)


def test_rejects_path_traversal(tmp_path):
    (tmp_path / "csk-skill.json").write_text(
        json.dumps({"schema_version": 1, "commands": {"bad": {"type": "script", "unix_path": "../bad"}}}),
        encoding="utf-8",
    )
    with pytest.raises(skillspec.SkillSpecError):
        skillspec.load_skill_spec(tmp_path)


def test_csk_skill_future_schema_fails(tmp_path):
    (tmp_path / "csk-skill.json").write_text(
        json.dumps({"schema_version": 7, "commands": {}}),
        encoding="utf-8",
    )
    with pytest.raises(skillspec.SkillSpecError, match="pipx upgrade cocoaskills"):
        skillspec.load_skill_spec(tmp_path)


def test_csk_skill_schema_must_be_integer(tmp_path):
    (tmp_path / "csk-skill.json").write_text(
        json.dumps({"schema_version": "2", "commands": {}}),
        encoding="utf-8",
    )
    with pytest.raises(skillspec.SkillSpecError, match="schema_version"):
        skillspec.load_skill_spec(tmp_path)


@pytest.mark.parametrize("name", ["../evil", "a/b", "a\\b", "-flag", ".hidden"])
def test_csk_skill_rejects_unsafe_command_names(tmp_path, name):
    (tmp_path / "scripts").mkdir()
    (tmp_path / "scripts" / "tool").write_text("#!/bin/sh\n", encoding="utf-8")
    (tmp_path / "csk-skill.json").write_text(
        json.dumps({"schema_version": 1, "commands": {name: {"type": "script", "unix_path": "scripts/tool"}}}),
        encoding="utf-8",
    )
    with pytest.raises(skillspec.SkillSpecError, match="Command name"):
        skillspec.load_skill_spec(tmp_path)


@pytest.mark.parametrize("name", ["../evil", "a/b", "-flag"])
def test_runtime_fallback_rejects_unsafe_command_names(tmp_path, name):
    (tmp_path / "agents").mkdir()
    (tmp_path / "agents" / "runtime.json").write_text(
        json.dumps({"commands": {name: "scripts/tool"}}),
        encoding="utf-8",
    )
    with pytest.raises(skillspec.SkillSpecError, match="command name"):
        skillspec.load_skill_spec(tmp_path)


def test_command_name_with_cmd_suffix_is_allowed(tmp_path):
    (tmp_path / "scripts").mkdir()
    (tmp_path / "scripts" / "tool.cmd").write_text("@echo off\n", encoding="utf-8")
    (tmp_path / "csk-skill.json").write_text(
        json.dumps({"schema_version": 1, "commands": {"tool.cmd": {"type": "script", "win_path": "scripts/tool.cmd"}}}),
        encoding="utf-8",
    )
    spec = skillspec.load_skill_spec(tmp_path)
    assert "tool.cmd" in spec.commands


@pytest.mark.parametrize("manifest_name", ["agent-skill.json", "csk-skill.json"])
@pytest.mark.parametrize(
    "command",
    [
        {"type": "script", "unix_path": "scripts/tool"},
        {"type": "system", "command": "tool"},
    ],
    ids=["script", "system"],
)
@pytest.mark.parametrize("reserved_field", ["driver", "source_dir"])
def test_schema_v1_rejects_reserved_build_fields(tmp_path, manifest_name, command, reserved_field):
    command = dict(command)
    command[reserved_field] = "reserved"
    _write_manifest(
        tmp_path,
        {"schema_version": 1, "commands": {"tool": command}},
        manifest_name,
    )

    with pytest.raises(skillspec.SkillSpecError, match=reserved_field):
        skillspec.load_skill_spec(tmp_path)


@pytest.mark.parametrize("manifest_name", ["agent-skill.json", "csk-skill.json"])
@pytest.mark.parametrize(
    "command",
    [
        {"type": "script", "unix_path": "scripts/tool"},
        {"type": "system", "command": "tool"},
    ],
    ids=["script", "system"],
)
def test_schema_v1_keeps_unrelated_extension_fields_compatible(tmp_path, manifest_name, command):
    command = dict(command)
    command["vendor_extension"] = {"enabled": True}
    _write_manifest(
        tmp_path,
        {
            "schema_version": 1,
            "vendor_extension": {"enabled": True},
            "commands": {"tool": command},
        },
        manifest_name,
    )

    spec = skillspec.load_skill_spec(tmp_path)

    assert spec.commands["tool"].type == command["type"]


@pytest.mark.parametrize("manifest_name", ["agent-skill.json", "csk-skill.json"])
def test_schema_v6_parses_mixed_commands_and_schema_v5_dependencies(tmp_path, manifest_name):
    _build_files(tmp_path)
    (tmp_path / "scripts").mkdir()
    (tmp_path / "scripts" / "helper").write_text("#!/bin/sh\n", encoding="utf-8")
    payload = _schema_v6_manifest(
        build_roots=["build"],
        runtime_roots=["scripts"],
        commands={
            "build-tool": _build_command(),
            "helper": {"type": "script", "unix_path": "scripts/helper"},
            "git": {"type": "system", "command": "git"},
        },
    )
    payload["capabilities"] = {"network": "none", "exec": ["git"]}
    payload["dependencies"] = {
        "commands": {"git": {"type": "system", "command": "git"}},
        "skills": {
            "provider": {
                "git": "https://example.com/provider.git",
                "ref": {"kind": "revision", "value": "abc123"},
                "mode": "runtime",
                "commands": ["build-tool"],
            }
        },
        "mcp_servers": {"docs": {"hint": "Connect docs.", "transport": "stdio"}},
    }
    _write_manifest(tmp_path, payload, manifest_name)

    spec = skillspec.load_skill_spec(tmp_path)

    assert spec.schema_version == 6
    assert spec.source_file == manifest_name
    assert spec.build_roots == ("build",)
    assert spec.runtime_roots == ("scripts",)
    assert spec.commands["build-tool"] == skillspec.CommandSpec(
        name="build-tool",
        type="build",
        driver="go-v1",
        source_dir="build/cmd/tool",
        source=manifest_name,
    )
    assert spec.commands["helper"].unix_path == "scripts/helper"
    assert spec.commands["git"].command == "git"
    assert spec.capabilities.exec == ("git",)
    assert spec.dependencies["git"].command == "git"
    assert spec.requirements["provider"].commands == ("build-tool",)
    assert spec.mcp_servers["docs"].transport == "stdio"


def test_equal_dual_schema_v6_manifests_select_canonical(tmp_path):
    _build_files(tmp_path)
    payload = _schema_v6_manifest(
        build_roots=["build"],
        commands={"build-tool": _build_command()},
    )
    (tmp_path / "agent-skill.json").write_text(json.dumps(payload, separators=(",", ":")), encoding="utf-8")
    (tmp_path / "csk-skill.json").write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")

    spec = skillspec.load_skill_spec(tmp_path)

    assert spec.source_file == "agent-skill.json"
    assert spec.commands["build-tool"].type == "build"


def test_schema_v6_without_builds_keeps_empty_build_domain(tmp_path):
    _write_manifest(
        tmp_path,
        _schema_v6_manifest(commands={"git": {"type": "system", "command": "git"}}),
    )

    spec = skillspec.load_skill_spec(tmp_path)

    assert spec.build_roots == ()
    assert spec.commands["git"].type == "system"


@pytest.mark.parametrize("manifest_name", ["agent-skill.json", "csk-skill.json"])
@pytest.mark.parametrize(
    ("command", "field"),
    [
        ({"type": "system", "command": "bin/tool"}, "command"),
        ({"type": "system", "command": "tool", "hint": ""}, "hint"),
    ],
    ids=["non-identifier-command", "empty-hint"],
)
def test_schema_v6_rejects_invalid_system_command_fields(
    tmp_path,
    manifest_name,
    command,
    field,
):
    _write_manifest(
        tmp_path,
        _schema_v6_manifest(commands={"tool": command}),
        manifest_name,
    )

    with pytest.raises(skillspec.SkillSpecError, match=rf"commands\.tool\.{field}"):
        skillspec.load_skill_spec(tmp_path)


@pytest.mark.parametrize("manifest_name", ["agent-skill.json", "csk-skill.json"])
def test_schema_v5_keeps_legacy_system_command_shape_compatible(tmp_path, manifest_name):
    _write_manifest(
        tmp_path,
        {
            "schema_version": 5,
            "capabilities": {},
            "commands": {
                "tool": {
                    "type": "system",
                    "command": "bin/tool",
                    "hint": "",
                }
            },
        },
        manifest_name,
    )

    spec = skillspec.load_skill_spec(tmp_path)

    assert spec.commands["tool"].command == "bin/tool"
    assert spec.commands["tool"].hint == ""


@pytest.mark.parametrize(
    ("command", "match"),
    [
        ({"type": "build", "source_dir": "build/cmd/tool"}, "driver"),
        ({"type": "build", "driver": "custom-v1", "source_dir": "build/cmd/tool"}, "driver"),
        ({"type": "build", "driver": "go-v1"}, "source_dir"),
        ({"type": "build", "driver": "go-v1", "source_dir": "."}, "source_dir"),
        (_build_command(unix_path="scripts/tool"), "unix_path"),
        (_build_command(command="tool"), "command"),
        (_build_command(args=[]), "args"),
        (_build_command(env={}), "env"),
        (_build_command(flags=[]), "flags"),
        (_build_command(output="bin/tool"), "output"),
        (_build_command(toolchain="go1.25"), "toolchain"),
        (_build_command(hooks=[]), "hooks"),
        (_build_command(scripts=[]), "scripts"),
        (_build_command(tags=[]), "tags"),
        (_build_command(target="native"), "target"),
    ],
)
def test_schema_v6_build_command_has_closed_shape(tmp_path, command, match):
    _build_files(tmp_path)
    _write_manifest(
        tmp_path,
        _schema_v6_manifest(build_roots=["build"], commands={"build-tool": command}),
    )

    with pytest.raises(skillspec.SkillSpecError, match=match):
        skillspec.load_skill_spec(tmp_path)


@pytest.mark.parametrize(
    ("location", "field", "value"),
    [
        ("top", "build_repositories", {}),
        ("top", "driver", "go-repository-v1"),
        ("top", "repository", "repo"),
        ("top", "target", "tool"),
        ("top", "install", "echo unsafe"),
        ("system", "driver", "go-repository-v1"),
        ("system", "repository", "repo"),
        ("system", "target", "tool"),
    ],
)
def test_schema_v6_rejects_reserved_future_surfaces(tmp_path, location, field, value):
    _build_files(tmp_path)
    payload = _schema_v6_manifest(
        build_roots=["build"],
        commands={
            "build-tool": _build_command(),
            "system-tool": {"type": "system", "command": "tool"},
        },
    )
    if location == "top":
        payload[field] = value
    else:
        payload["commands"]["system-tool"][field] = value
    _write_manifest(tmp_path, payload)

    with pytest.raises(skillspec.SkillSpecError, match=field):
        skillspec.load_skill_spec(tmp_path)


@pytest.mark.parametrize("schema", [1, 2, 3, 4, 5])
def test_schema_v1_through_v5_reject_build_roots(tmp_path, schema):
    payload: dict[str, Any] = {"schema_version": schema, "build_roots": [], "commands": {}}
    if schema >= 3:
        payload["capabilities"] = {}
    _write_manifest(tmp_path, payload)

    with pytest.raises(skillspec.SkillSpecError, match="build_roots"):
        skillspec.load_skill_spec(tmp_path)


@pytest.mark.parametrize("schema", [1, 2, 3, 4, 5])
def test_schema_v1_through_v5_reject_build_commands(tmp_path, schema):
    payload: dict[str, Any] = {
        "schema_version": schema,
        "commands": {"build-tool": _build_command()},
    }
    if schema >= 3:
        payload["capabilities"] = {}
    _write_manifest(tmp_path, payload)

    with pytest.raises(skillspec.SkillSpecError, match="unsupported"):
        skillspec.load_skill_spec(tmp_path)


def test_runtime_fallback_rejects_build_object(tmp_path):
    (tmp_path / "agents").mkdir()
    (tmp_path / "agents" / "runtime.json").write_text(
        json.dumps({"commands": {"build-tool": _build_command()}}),
        encoding="utf-8",
    )

    with pytest.raises(skillspec.SkillSpecError, match="path must be a non-empty string"):
        skillspec.load_skill_spec(tmp_path)


@pytest.mark.parametrize(
    ("roots", "prepare", "match"),
    [
        (["missing"], None, "does not exist"),
        (["build"], "file", "must be a directory"),
        (["."], None, "relative path"),
        (["../build"], None, "relative path"),
        (["build", "build"], "valid", "unique"),
        (["build", "build/nested"], "nested", "disjoint"),
    ],
)
def test_schema_v6_rejects_invalid_build_roots(tmp_path, roots, prepare, match):
    if prepare == "file":
        (tmp_path / "build").write_text("not a directory", encoding="utf-8")
    elif prepare == "valid":
        _build_files(tmp_path)
    elif prepare == "nested":
        _build_files(tmp_path)
        (tmp_path / "build" / "nested").mkdir()
    _write_manifest(tmp_path, _schema_v6_manifest(build_roots=roots))

    with pytest.raises(skillspec.SkillSpecError, match=match):
        skillspec.load_skill_spec(tmp_path)


@pytest.mark.parametrize(
    ("runtime_root", "build_root"),
    [
        ("build", "build"),
        ("build/runtime", "build"),
        ("runtime", "runtime/build"),
    ],
)
def test_schema_v6_build_roots_do_not_overlap_runtime_roots(tmp_path, runtime_root, build_root):
    (tmp_path / runtime_root).mkdir(parents=True)
    (tmp_path / build_root).mkdir(parents=True, exist_ok=True)
    _write_manifest(
        tmp_path,
        _schema_v6_manifest(build_roots=[build_root], runtime_roots=[runtime_root]),
    )

    with pytest.raises(skillspec.SkillSpecError, match="runtime"):
        skillspec.load_skill_spec(tmp_path)


def test_schema_v6_rejects_linked_build_root(tmp_path):
    target = tmp_path / "real-build"
    target.mkdir()
    try:
        (tmp_path / "build").symlink_to(target, target_is_directory=True)
    except OSError as exc:
        pytest.skip(f"symlink unavailable: {exc}")
    _write_manifest(tmp_path, _schema_v6_manifest(build_roots=["build"]))

    with pytest.raises(skillspec.SkillSpecError, match="link-free"):
        skillspec.load_skill_spec(tmp_path)


def test_schema_v6_rejects_linked_build_root_component(tmp_path):
    target = tmp_path / "real-parent"
    (target / "build").mkdir(parents=True)
    try:
        (tmp_path / "linked").symlink_to(target, target_is_directory=True)
    except OSError as exc:
        pytest.skip(f"symlink unavailable: {exc}")
    _write_manifest(tmp_path, _schema_v6_manifest(build_roots=["linked/build"]))

    with pytest.raises(skillspec.SkillSpecError, match="link-free"):
        skillspec.load_skill_spec(tmp_path)


def test_schema_v6_requires_every_build_root_to_be_used(tmp_path):
    _build_files(tmp_path)
    (tmp_path / "other").mkdir()
    (tmp_path / "other" / "go.mod").write_text("module example.com/other\n", encoding="utf-8")
    _write_manifest(
        tmp_path,
        _schema_v6_manifest(
            build_roots=["build", "other"],
            commands={"build-tool": _build_command()},
        ),
    )

    with pytest.raises(skillspec.SkillSpecError, match="other.*not used"):
        skillspec.load_skill_spec(tmp_path)


def test_schema_v6_uses_each_disjoint_build_root(tmp_path):
    _build_files(tmp_path, root="first", source_dir="first/cmd/one")
    _build_files(tmp_path, root="second", source_dir="second")
    _write_manifest(
        tmp_path,
        _schema_v6_manifest(
            build_roots=["first", "second"],
            commands={
                "one": _build_command("first/cmd/one"),
                "two": _build_command("second"),
            },
        ),
    )

    spec = skillspec.load_skill_spec(tmp_path)

    assert spec.build_roots == ("first", "second")
    assert spec.commands["two"].source_dir == "second"


def test_schema_v6_build_command_requires_declared_root(tmp_path):
    _build_files(tmp_path)
    _write_manifest(
        tmp_path,
        _schema_v6_manifest(commands={"build-tool": _build_command()}),
    )

    with pytest.raises(skillspec.SkillSpecError, match="exactly one"):
        skillspec.load_skill_spec(tmp_path)


@pytest.mark.parametrize(
    ("source_dir", "prepare", "match"),
    [
        ("../tool", "valid", "relative path"),
        ("other/tool", "outside", "exactly one"),
        ("build/cmd/missing", "missing", "does not exist"),
        ("build/cmd/tool", "file", "must be a directory"),
        ("build/cmd/tool", "missing-module", "go.mod"),
        ("build/cmd/tool", "nested-module", "intervening"),
    ],
)
def test_schema_v6_rejects_invalid_source_layout(tmp_path, source_dir, prepare, match):
    if prepare == "valid":
        _build_files(tmp_path)
    elif prepare == "outside":
        (tmp_path / "build").mkdir()
        (tmp_path / "build" / "go.mod").write_text("module example.com/root\n", encoding="utf-8")
        (tmp_path / "other" / "tool").mkdir(parents=True)
    elif prepare == "missing":
        (tmp_path / "build").mkdir()
        (tmp_path / "build" / "go.mod").write_text("module example.com/root\n", encoding="utf-8")
    elif prepare == "file":
        (tmp_path / "build" / "cmd").mkdir(parents=True)
        (tmp_path / "build" / "go.mod").write_text("module example.com/root\n", encoding="utf-8")
        (tmp_path / "build" / "cmd" / "tool").write_text("package main\n", encoding="utf-8")
    elif prepare == "missing-module":
        (tmp_path / "build" / "cmd" / "tool").mkdir(parents=True)
    elif prepare == "nested-module":
        _build_files(tmp_path)
        (tmp_path / "build" / "cmd" / "go.mod").write_text("module example.com/nested\n", encoding="utf-8")
    _write_manifest(
        tmp_path,
        _schema_v6_manifest(
            build_roots=["build"],
            commands={"build-tool": _build_command(source_dir)},
        ),
    )

    with pytest.raises(skillspec.SkillSpecError, match=match):
        skillspec.load_skill_spec(tmp_path)


def test_schema_v6_allows_source_dir_equal_to_build_root(tmp_path):
    (tmp_path / "build").mkdir()
    (tmp_path / "build" / "go.mod").write_text("module example.com/tool\n", encoding="utf-8")
    (tmp_path / "build" / "main.go").write_text("package main\n", encoding="utf-8")
    _write_manifest(
        tmp_path,
        _schema_v6_manifest(
            build_roots=["build"],
            commands={"build-tool": _build_command("build")},
        ),
    )

    spec = skillspec.load_skill_spec(tmp_path)

    assert spec.commands["build-tool"].source_dir == "build"


def test_schema_v6_rejects_linked_source_directory(tmp_path):
    (tmp_path / "build" / "cmd").mkdir(parents=True)
    (tmp_path / "build" / "go.mod").write_text("module example.com/tool\n", encoding="utf-8")
    target = tmp_path / "real-tool"
    target.mkdir()
    try:
        (tmp_path / "build" / "cmd" / "tool").symlink_to(target, target_is_directory=True)
    except OSError as exc:
        pytest.skip(f"symlink unavailable: {exc}")
    _write_manifest(
        tmp_path,
        _schema_v6_manifest(build_roots=["build"], commands={"build-tool": _build_command()}),
    )

    with pytest.raises(skillspec.SkillSpecError, match="link-free"):
        skillspec.load_skill_spec(tmp_path)


def test_schema_v6_rejects_linked_go_mod(tmp_path):
    (tmp_path / "build" / "cmd" / "tool").mkdir(parents=True)
    target = tmp_path / "real-go.mod"
    target.write_text("module example.com/tool\n", encoding="utf-8")
    try:
        (tmp_path / "build" / "go.mod").symlink_to(target)
    except OSError as exc:
        pytest.skip(f"symlink unavailable: {exc}")
    _write_manifest(
        tmp_path,
        _schema_v6_manifest(build_roots=["build"], commands={"build-tool": _build_command()}),
    )

    with pytest.raises(skillspec.SkillSpecError, match="real regular file"):
        skillspec.load_skill_spec(tmp_path)


def test_schema_v6_rejects_non_regular_go_mod(tmp_path):
    (tmp_path / "build" / "cmd" / "tool").mkdir(parents=True)
    (tmp_path / "build" / "go.mod").mkdir()
    _write_manifest(
        tmp_path,
        _schema_v6_manifest(build_roots=["build"], commands={"build-tool": _build_command()}),
    )

    with pytest.raises(skillspec.SkillSpecError, match="real regular file"):
        skillspec.load_skill_spec(tmp_path)


def test_schema_v6_build_diagnostics_are_deterministic(tmp_path):
    (tmp_path / "build").mkdir()
    (tmp_path / "build" / "go.mod").write_text("module example.com/tool\n", encoding="utf-8")
    payload = _schema_v6_manifest(
        build_roots=["build"],
        commands={
            "z": {"type": "build", "driver": "bad", "source_dir": "build"},
            "a": {"type": "build", "driver": "bad", "source_dir": "build"},
        },
    )
    _write_manifest(tmp_path, payload)

    messages: set[str] = set()
    for _ in range(10):
        with pytest.raises(skillspec.SkillSpecError) as caught:
            skillspec.load_skill_spec(tmp_path)
        messages.add(str(caught.value))

    assert len(messages) == 1
    assert "Command 'a'" in messages.pop()


def test_schema_v6_candidate_manifest_cases(tmp_path):
    root_text = os.environ.get("CURATOR_SCHEMA_V6_ROOT")
    if root_text is None:
        pytest.skip("CURATOR_SCHEMA_V6_ROOT is not set")
    cases_root = Path(root_text) / "schema-cases"
    suites = {
        "agent-skill-v6": "agent-skill.json",
        "csk-skill-v6": "csk-skill.json",
    }
    case_count = 0
    for suite, manifest_name in suites.items():
        suite_root = cases_root / suite
        assert suite_root.is_dir(), f"missing schema-v6 suite: {suite_root}"
        for case_path in sorted(suite_root.glob("*.json")):
            case_count += 1
            snapshot = tmp_path / suite / case_path.stem
            _build_files(snapshot)
            (snapshot / "scripts").mkdir()
            (snapshot / "scripts" / "tool").write_text("#!/bin/sh\n", encoding="utf-8")
            (snapshot / manifest_name).write_bytes(case_path.read_bytes())
            if case_path.name == "valid.json":
                spec = skillspec.load_skill_spec(snapshot)
                assert spec.source_file == manifest_name
                assert spec.commands["build-tool"].driver == "go-v1"
            else:
                with pytest.raises(skillspec.SkillSpecError):
                    skillspec.load_skill_spec(snapshot)
    assert case_count == 48
