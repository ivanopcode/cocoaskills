"""Installing a schema-8 skill: marker v4 and the fail-closed script policy."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from conftest import make_config, make_project, make_skill_repo, write_skillfile
from test_install import _stub_trusted_toolchain

from csk import installer, skillcheck, skillspec
from csk.builds import go_v1


def _manifest(commands: dict[str, Any], **changes: Any) -> str:
    payload: dict[str, Any] = {
        "schema_version": 8,
        "capabilities": {},
        "runtime_roots": ["scripts"],
        "commands": commands,
    }
    payload.update(changes)
    return json.dumps(payload, indent=2) + "\n"


def _script_command(**extra: Any) -> dict[str, Any]:
    command: dict[str, Any] = {"type": "script", "unix_path": "scripts/tool", "win_path": "scripts/tool.cmd"}
    command.update(extra)
    return command


_SCRIPT_FILES = {
    "scripts/tool": "#!/usr/bin/env python3\nprint('tool')\n",
    "scripts/tool.cmd": "@echo tool\r\n",
}


def test_a_schema_eight_installation_records_marker_v4(tmp_path, skills_root, csk_home) -> None:
    project = make_project(tmp_path)
    make_skill_repo(
        skills_root,
        "skill-tool",
        {"agent-skill.json": _manifest({"tool": _script_command()}), **_SCRIPT_FILES},
        tag="v1",
    )
    write_skillfile(
        project,
        {"schema_version": 1, "agents": ["claude_code"], "skills": [{"name": "skill-tool", "tag": "v1"}]},
    )
    cfg = make_config(csk_home, skills_root, project, agents=["claude_code"])

    result = installer.install(cfg)[0]

    assert not result.errors
    installed = project / ".agents" / "skills" / "skill-tool"
    marker = json.loads((installed / ".csk-install.json").read_text(encoding="utf-8"))
    assert marker["schema_version"] == 4
    assert marker["skill_schema_version"] == 8
    assert marker["commands"] == ["tool"]
    assert marker["builds"] == {}
    assert "build_source" not in marker


def test_a_schema_eight_installation_is_current_on_reinstall(tmp_path, skills_root, csk_home) -> None:
    project = make_project(tmp_path)
    make_skill_repo(
        skills_root,
        "skill-tool",
        {"agent-skill.json": _manifest({"tool": _script_command()}), **_SCRIPT_FILES},
        tag="v1",
    )
    write_skillfile(
        project,
        {"schema_version": 1, "agents": ["claude_code"], "skills": [{"name": "skill-tool", "tag": "v1"}]},
    )
    cfg = make_config(csk_home, skills_root, project, agents=["claude_code"])

    first = installer.install(cfg)[0]
    marker_path = project / ".agents" / "skills" / "skill-tool" / ".csk-install.json"
    installed_at = json.loads(marker_path.read_text(encoding="utf-8"))["installed_at"]
    second = installer.install(cfg)[0]

    assert not first.errors and not second.errors
    assert json.loads(marker_path.read_text(encoding="utf-8"))["installed_at"] == installed_at


def test_an_enforced_script_command_fails_the_install_closed(tmp_path, skills_root, csk_home) -> None:
    project = make_project(tmp_path)
    make_skill_repo(
        skills_root,
        "skill-tool",
        {
            "agent-skill.json": _manifest(
                {
                    "tool": _script_command(
                        execution_policy="script-worker-v1",
                        interpreter="python3-v1",
                    )
                }
            ),
            **_SCRIPT_FILES,
        },
        tag="v1",
    )
    write_skillfile(
        project,
        {"schema_version": 1, "agents": ["claude_code"], "skills": [{"name": "skill-tool", "tag": "v1"}]},
    )
    cfg = make_config(csk_home, skills_root, project, agents=["claude_code"])

    result = installer.install(cfg)[0]

    assert result.errors
    assert any("script_execution_policy_unsupported" in error for error in result.errors)
    # Nothing was published: no shim, no marker, no context copy.
    assert not (project / ".agents" / "skills" / "skill-tool").exists()
    assert not (project / ".agents" / "bin" / "tool").exists()
    assert not (csk_home / "runtime" / "skill-tool").exists()


def test_skill_check_reports_the_unsupported_policy_as_an_error(tmp_path: Path) -> None:
    (tmp_path / "SKILL.md").write_text("---\nname: test\n---\n\n# Test\n", encoding="utf-8")
    (tmp_path / "scripts").mkdir()
    (tmp_path / "scripts" / "tool").write_text("#!/usr/bin/env python3\n", encoding="utf-8")
    (tmp_path / "agent-skill.json").write_text(
        _manifest(
            {
                "tool": {
                    "type": "script",
                    "unix_path": "scripts/tool",
                    "execution_policy": "script-worker-v1",
                    "interpreter": "python3-v1",
                }
            }
        ),
        encoding="utf-8",
    )

    issues = skillcheck.validate_skill(tmp_path)

    assert skillcheck.has_errors(issues)
    reported = [issue for issue in issues if issue.code == "skill.script_execution_policy_unsupported"]
    assert len(reported) == 1
    assert reported[0].severity == "error"
    assert skillspec.SCRIPT_EXECUTION_POLICY_UNSUPPORTED in reported[0].message


def test_the_shim_publication_point_refuses_an_enforced_command(tmp_path, skills_root, csk_home) -> None:
    """The guard holds even when a caller reaches the publisher directly."""
    project = make_project(tmp_path)
    repo, commit = make_skill_repo(
        skills_root,
        "skill-tool",
        {
            "agent-skill.json": _manifest(
                {
                    "tool": _script_command(
                        execution_policy="script-worker-v1",
                        interpreter="python3-v1",
                    )
                }
            ),
            **_SCRIPT_FILES,
        },
        tag="v1",
    )
    spec = skillspec.load_skill_spec(repo)
    plan = installer.SkillPlan(
        decl=type("Decl", (), {"name": "skill-tool", "git": None, "source": "skill-tool"})(),
        resolved=type("Resolved", (), {"kind": "tag", "ref": "v1", "commit": commit})(),
        repo=repo,
        snapshot=repo,
        spec=spec,
    )

    with pytest.raises(installer.InstallError, match="script_execution_policy_unsupported"):
        installer.install_runtime_commands(csk_home, project / "bin", plan)

    assert not (project / "bin").exists()


_MODULE_ROOT_FILES = {
    "tools/cli/go.mod": (
        "module example.com/cli\n\n"
        "go 1.23\n\n"
        "require example.com/board v0.0.0\n\n"
        "replace example.com/board => ../../pkg/board\n"
    ),
    "tools/cli/cmd/cli/main.go": "package main\n\nfunc main() {}\n",
    "tools/cli/vendor/example.com/board/board.go": "package board\n",
    "tools/cli/vendor/modules.txt": (
        "# example.com/board v0.0.0 => ../../pkg/board\n"
        "## explicit; go 1.23\n"
        "example.com/board\n"
        "# example.com/board => ../../pkg/board\n"
    ),
    "pkg/board/go.mod": "module example.com/board\n\ngo 1.23\n",
    "pkg/board/board.go": "package board\n",
}


def _module_root_manifest() -> str:
    return json.dumps(
        {
            "schema_version": 8,
            "capabilities": {},
            "build_roots": ["tools/cli"],
            "commands": {
                "cli": {
                    "type": "build",
                    "driver": "go-v1",
                    "source_dir": "tools/cli/cmd/cli",
                    "modules": ["pkg/board"],
                }
            },
        },
        indent=2,
    ) + "\n"


def test_declared_modules_reach_the_driver_and_the_marker(
    monkeypatch, tmp_path, skills_root, csk_home
) -> None:
    project = make_project(tmp_path)
    make_skill_repo(
        skills_root,
        "skill-build",
        {"agent-skill.json": _module_root_manifest(), **_MODULE_ROOT_FILES},
        tag="v1",
    )
    write_skillfile(project, {"schema_version": 1, "skills": [{"name": "skill-build", "tag": "v1"}]})
    cfg = make_config(csk_home, skills_root, project)
    _stub_trusted_toolchain(monkeypatch)
    observed: list[go_v1.BuildRequest] = []
    stubbed_build = go_v1.build

    def recording_build(request: go_v1.BuildRequest) -> go_v1.BuildResult:
        observed.append(request)
        return stubbed_build(request)

    monkeypatch.setattr(go_v1, "build", recording_build)

    result = installer.install(cfg)[0]

    assert not result.errors
    assert [request.modules for request in observed] == [("pkg/board",)]
    assert observed[0].command_object["modules"] == ["pkg/board"]
    assert observed[0].build_roots == ("tools/cli",)
    marker = json.loads(
        (project / ".agents" / "skills" / "skill-build" / ".csk-install.json").read_text(
            encoding="utf-8"
        )
    )
    assert marker["schema_version"] == 4
    assert marker["skill_schema_version"] == 8
    assert marker["build_roots"] == ["tools/cli"]
    assert list(marker["builds"]) == ["cli"]
    assert marker["builds"]["cli"]["receipt_schema_version"] == 1
    assert marker["builds"]["cli"]["execution_policy"] == "manager-worker-v1"


def test_an_unused_module_declaration_fails_the_manifest(tmp_path, skills_root, csk_home) -> None:
    project = make_project(tmp_path)
    files = dict(_MODULE_ROOT_FILES)
    del files["pkg/board/go.mod"]
    del files["pkg/board/board.go"]
    make_skill_repo(
        skills_root,
        "skill-build",
        {"agent-skill.json": _module_root_manifest(), **files},
        tag="v1",
    )
    write_skillfile(project, {"schema_version": 1, "skills": [{"name": "skill-build", "tag": "v1"}]})
    cfg = make_config(csk_home, skills_root, project)

    result = installer.install(cfg)[0]

    assert result.errors
    assert any("build_module_root_declaration_invalid" in error for error in result.errors)
    assert not (project / ".agents" / "skills" / "skill-build").exists()
