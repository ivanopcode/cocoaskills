"""Schema-8 `script-worker-v1` opt-in parsing and fail-closed admission.

Protocol Core 4.1.1 separates two statements that are easy to conflate. A
schema-8 manifest that selects the policy is a valid document, so the parser
accepts it. csk does not implement the worker, so every surface that would turn
that declaration into an installed shim refuses it with
``script_execution_policy_unsupported`` instead of publishing the uncontained
launcher the manifest says is contained.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from conftest import init_git_repo, commit_all, make_config, make_project, write_files, write_skillfile
from csk import installer, script_policy, skillcheck, shims, skillspec
from csk.skillspec import CommandSpec


def _write_json(path: Path, payload: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")


def _prepare(snapshot: Path) -> None:
    (snapshot / "scripts").mkdir(parents=True, exist_ok=True)
    (snapshot / "scripts" / "tool").write_text("#!/usr/bin/env python3\n", encoding="utf-8")
    (snapshot / "scripts" / "tool.cmd").write_text("@echo off\r\n", encoding="utf-8")
    (snapshot / "SKILL.md").write_text("# demo\n", encoding="utf-8")


def _manifest(command: dict[str, object], **overrides: object) -> dict[str, object]:
    payload: dict[str, object] = {
        "schema_version": 8,
        "capabilities": {"exec": "none", "network": "none"},
        "runtime_roots": ["scripts"],
        "commands": {"tool": command},
    }
    payload.update(overrides)
    return payload


def _enforced(**overrides: object) -> dict[str, object]:
    command: dict[str, object] = {
        "type": "script",
        "unix_path": "scripts/tool",
        "win_path": "scripts/tool.cmd",
        "execution_policy": "script-worker-v1",
        "interpreter": "python3-v1",
    }
    command.update(overrides)
    return command


@pytest.mark.parametrize("interpreter", ["python3-v1", "node-v1"])
def test_schema_v8_parses_the_closed_opt_in_pair(tmp_path: Path, interpreter: str) -> None:
    _prepare(tmp_path)
    _write_json(
        tmp_path / "agent-skill.json",
        _manifest(_enforced(interpreter=interpreter)),
    )

    spec = skillspec.load_skill_spec(tmp_path)

    assert spec.commands["tool"].execution_policy == "script-worker-v1"
    assert spec.commands["tool"].interpreter == interpreter


def test_an_absent_pair_is_declared_only(tmp_path: Path) -> None:
    _prepare(tmp_path)
    _write_json(
        tmp_path / "agent-skill.json",
        _manifest({"type": "script", "unix_path": "scripts/tool"}),
    )

    command = skillspec.load_skill_spec(tmp_path).commands["tool"]

    assert command.execution_policy is None
    assert command.interpreter is None
    assert not script_policy.is_enforced(command)


@pytest.mark.parametrize(
    ("overrides", "message"),
    [
        ({"interpreter": None}, "interpreter"),
        ({"execution_policy": None}, "execution_policy"),
        ({"execution_policy": "script-worker-v2"}, "execution_policy"),
        ({"execution_policy": "manager-worker-v1"}, "execution_policy"),
        ({"interpreter": "bash-v1"}, "interpreter"),
        ({"interpreter": "python3"}, "interpreter"),
    ],
)
def test_the_opt_in_pair_is_co_required_and_closed(
    tmp_path: Path, overrides: dict[str, object], message: str
) -> None:
    _prepare(tmp_path)
    command = _enforced()
    for key, value in overrides.items():
        if value is None:
            command.pop(key)
        else:
            command[key] = value
    _write_json(tmp_path / "agent-skill.json", _manifest(command))

    with pytest.raises(skillspec.SkillSpecError, match=message):
        skillspec.load_skill_spec(tmp_path)


@pytest.mark.parametrize("field", ["execution_policy", "interpreter"])
def test_the_opt_in_fields_are_rejected_at_the_top_level(
    tmp_path: Path, field: str
) -> None:
    _prepare(tmp_path)
    _write_json(
        tmp_path / "agent-skill.json",
        _manifest(_enforced(), **{field: "script-worker-v1"}),
    )

    with pytest.raises(skillspec.SkillSpecError, match=field):
        skillspec.load_skill_spec(tmp_path)


@pytest.mark.parametrize(
    "command",
    [
        {
            "type": "system",
            "command": "git",
            "execution_policy": "script-worker-v1",
            "interpreter": "python3-v1",
        },
        {
            "type": "build",
            "driver": "go-v1",
            "source_dir": "build/cmd/tool",
            "execution_policy": "script-worker-v1",
            "interpreter": "python3-v1",
        },
    ],
)
def test_only_a_script_command_may_carry_the_opt_in(
    tmp_path: Path, command: dict[str, object]
) -> None:
    _prepare(tmp_path)
    _write_json(tmp_path / "agent-skill.json", _manifest(command))

    with pytest.raises(skillspec.SkillSpecError, match="execution_policy"):
        skillspec.load_skill_spec(tmp_path)


@pytest.mark.parametrize("schema", [2, 3, 4, 5, 6, 7])
def test_earlier_schemas_reject_the_opt_in_as_an_unknown_field(
    tmp_path: Path, schema: int
) -> None:
    _prepare(tmp_path)
    payload = _manifest(_enforced())
    payload["schema_version"] = schema
    if schema < 3:
        payload.pop("capabilities")
    _write_json(tmp_path / "agent-skill.json", payload)

    with pytest.raises(skillspec.SkillSpecError, match="execution_policy"):
        skillspec.load_skill_spec(tmp_path)


def test_schema_one_rejects_the_opt_in_as_a_reserved_command_field(
    tmp_path: Path,
) -> None:
    """Schema 1 keeps its deployed extension behavior, so the semantic check
    of this document rejects the field instead of the unknown-field rule."""

    _prepare(tmp_path)
    payload = _manifest(_enforced())
    payload["schema_version"] = 1
    payload.pop("capabilities")
    payload.pop("runtime_roots")
    _write_json(tmp_path / "agent-skill.json", payload)

    with pytest.raises(skillspec.SkillSpecError, match="execution_policy"):
        skillspec.load_skill_spec(tmp_path)


def test_skill_check_refuses_an_enforced_command_with_the_closed_diagnostic(
    tmp_path: Path,
) -> None:
    _prepare(tmp_path)
    _write_json(tmp_path / "agent-skill.json", _manifest(_enforced()))

    issues = skillcheck.validate_skill(tmp_path)

    refusals = [
        issue
        for issue in issues
        if issue.code == script_policy.SCRIPT_EXECUTION_POLICY_UNSUPPORTED
    ]
    assert len(refusals) == 1
    assert refusals[0].severity == "error"
    assert refusals[0].path == "commands.tool.execution_policy"
    assert skillcheck.has_errors(issues)


def test_a_declared_only_schema_eight_script_still_validates(tmp_path: Path) -> None:
    _prepare(tmp_path)
    _write_json(
        tmp_path / "agent-skill.json",
        _manifest({"type": "script", "unix_path": "scripts/tool"}),
    )

    issues = skillcheck.validate_skill(tmp_path)

    assert all(
        issue.code != script_policy.SCRIPT_EXECUTION_POLICY_UNSUPPORTED
        for issue in issues
    )


def test_the_shim_writer_refuses_on_its_own(tmp_path: Path) -> None:
    """Each layer fails the install alone, because neither may be the only gate."""

    _prepare(tmp_path)
    command = CommandSpec(
        name="tool",
        type="script",
        unix_path="scripts/tool",
        win_path="scripts/tool.cmd",
        execution_policy="script-worker-v1",
        interpreter="python3-v1",
    )

    with pytest.raises(script_policy.ScriptPolicyError) as raised:
        shims.install_runtime_command(
            csk_home=tmp_path / "home",
            skill_name="demo",
            commit="a" * 40,
            snapshot=tmp_path,
            command=command,
        )

    assert raised.value.code == script_policy.SCRIPT_EXECUTION_POLICY_UNSUPPORTED
    assert not (tmp_path / "home").exists()


def test_admission_names_the_first_enforced_command_in_lexical_order() -> None:
    commands = {
        name: CommandSpec(
            name=name,
            type="script",
            unix_path=f"scripts/{name}",
            execution_policy="script-worker-v1",
            interpreter="python3-v1",
        )
        for name in ("zeta", "alpha", "mid")
    }
    commands["declared"] = CommandSpec(
        name="declared", type="script", unix_path="scripts/declared"
    )

    with pytest.raises(script_policy.ScriptPolicyError) as raised:
        script_policy.admit(commands)

    assert raised.value.path == "commands.alpha.execution_policy"


def test_admission_passes_a_declared_only_command_set() -> None:
    script_policy.admit(
        {
            "tool": CommandSpec(
                name="tool", type="script", unix_path="scripts/tool"
            )
        }
    )


def test_install_refuses_an_enforced_command_and_publishes_no_shim(
    tmp_path: Path,
    skills_root: Path,
    csk_home: Path,
) -> None:
    """The install preflight is the layer an operator actually meets."""

    repo = init_git_repo(skills_root / "enforced")
    write_files(
        repo,
        {
            "SKILL.md": "---\nname: enforced\n---\n\n# Enforced\n",
            "scripts/tool": "#!/usr/bin/env python3\nprint('tool')\n",
            "scripts/tool.cmd": "@echo off\r\n",
            "agent-skill.json": json.dumps(_manifest(_enforced()), indent=2) + "\n",
        },
    )
    commit_all(repo, "enforced script command")
    project = make_project(tmp_path)
    cfg = make_config(csk_home, skills_root, project, agents=["codex_cli"])
    write_skillfile(
        project,
        {
            "schema_version": 1,
            "agents": ["codex_cli"],
            "skills": [{"name": "enforced", "branch": "main"}],
        },
    )

    result = installer.install(cfg, alias="app")[0]

    assert result.status == "failed"
    assert any(
        script_policy.SCRIPT_EXECUTION_POLICY_UNSUPPORTED in error
        for error in result.errors
    ), result.errors
    assert not (project / ".agents" / "bin" / "tool").exists()
    assert not (project / ".agents" / "skills" / "enforced").exists()
