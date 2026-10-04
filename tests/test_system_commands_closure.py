from __future__ import annotations

import json
import os

import pytest
from conftest import (
    make_config,
    make_project,
    make_skill_repo,
    write_skillfile,
)

from csk import cli, closure, config, global_install, installer, status
from csk.builds import planner as build_planner


def _write_global_skillfile(csk_home, data: dict) -> None:
    root = csk_home / "global"
    root.mkdir(parents=True, exist_ok=True)
    (root / "Skillfile.json").write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")


def _save_config(monkeypatch: pytest.MonkeyPatch, cfg: config.GlobalConfig) -> None:
    config.save_config(cfg)
    monkeypatch.setenv("CSK_CONFIG", str(cfg.path))


def test_project_reports_every_missing_system_command_in_one_diagnostic(
    tmp_path, skills_root, csk_home
):
    project = make_project(tmp_path)
    make_skill_repo(
        skills_root,
        "skill-legacy",
        {
            "csk-skill.json": json.dumps(
                {
                    "schema_version": 2,
                    "commands": {
                        "tool-a": {
                            "type": "system",
                            "command": "__csk_missing_tool_alpha__",
                            "hint": "install alpha",
                        }
                    },
                }
            ),
        },
        tag="v1",
    )
    make_skill_repo(
        skills_root,
        "skill-explicit",
        {
            "csk-skill.json": json.dumps(
                {
                    "schema_version": 2,
                    "commands": {},
                    "dependencies": {
                        "commands": {
                            "tool-b": {
                                "type": "system",
                                "command": "__csk_missing_tool_beta__",
                                "hint": "install beta",
                            }
                        }
                    },
                }
            ),
        },
        tag="v1",
    )
    write_skillfile(
        project,
        {
            "schema_version": 1,
            "skills": [
                {"name": "skill-legacy", "tag": "v1"},
                {"name": "skill-explicit", "tag": "v1"},
            ],
        },
    )
    cfg = make_config(csk_home, skills_root, project)

    result = installer.install(cfg)[0]

    assert result.status == "failed"
    assert len(result.errors) == 1
    diagnostic = result.errors[0]
    assert "Missing system command '__csk_missing_tool_alpha__' for skill-legacy" in diagnostic
    assert "install alpha" in diagnostic
    assert "Missing system command '__csk_missing_tool_beta__' for skill-explicit" in diagnostic
    assert "install beta" in diagnostic
    assert not (project / ".agents" / "skills" / "skill-legacy").exists()
    assert not (project / ".agents" / "skills" / "skill-explicit").exists()


def test_project_system_check_runs_before_builds_and_publication(
    monkeypatch, tmp_path, skills_root, csk_home
):
    project = make_project(tmp_path)
    make_skill_repo(
        skills_root,
        "skill-build",
        {
            "agent-skill.json": json.dumps(
                {
                    "schema_version": 6,
                    "build_roots": ["build"],
                    "commands": {
                        "tool": {
                            "type": "build",
                            "driver": "go-v1",
                            "source_dir": "build/cmd/tool",
                        }
                    },
                    "capabilities": {},
                }
            ),
            "build/go.mod": "module example.com/tool\n\ngo 1.23\n",
            "build/cmd/tool/main.go": "package main\n\nfunc main() {}\n",
        },
        tag="v1",
    )
    make_skill_repo(
        skills_root,
        "skill-bad",
        {
            "csk-skill.json": json.dumps(
                {
                    "schema_version": 2,
                    "commands": {
                        "missing": {
                            "type": "system",
                            "command": "__csk_missing_build_gate_tool__",
                        }
                    },
                }
            ),
        },
        tag="v1",
    )
    write_skillfile(
        project,
        {
            "schema_version": 1,
            "skills": [
                {"name": "skill-build", "tag": "v1"},
                {"name": "skill-bad", "tag": "v1"},
            ],
        },
    )
    cfg = make_config(csk_home, skills_root, project)

    def unexpected_freeze(_nodes, _stack):
        raise AssertionError("system check must run before build-provider freezing")

    def unexpected_validate(_plans, _locale):
        raise AssertionError("system check must run before skill validation")

    def unexpected_plan(*_args, **_kwargs):
        raise AssertionError("system check must run before build planning")

    def unexpected_publish(*_args, **_kwargs):
        raise AssertionError("system check must run before build-repository fetch")

    monkeypatch.setattr(installer, "_freeze_build_providers", unexpected_freeze)
    monkeypatch.setattr(installer, "_validate_skills", unexpected_validate)
    monkeypatch.setattr(build_planner, "plan_builds", unexpected_plan)
    monkeypatch.setattr(installer, "_publish_external_builds", unexpected_publish)

    result = installer.install(cfg)[0]

    assert result.status == "failed"
    assert len(result.errors) == 1
    assert "Missing system command '__csk_missing_build_gate_tool__' for skill-bad" in result.errors[0]
    assert result.builds == []
    assert not (project / ".agents" / "skills" / "skill-build").exists()
    assert not (project / ".agents" / "skills" / "skill-bad").exists()


def test_project_dry_run_and_status_report_same_missing(
    tmp_path, skills_root, csk_home
):
    project = make_project(tmp_path)
    for name, tool in (
        ("skill-one", "__csk_missing_dry_alpha__"),
        ("skill-two", "__csk_missing_dry_beta__"),
    ):
        make_skill_repo(
            skills_root,
            name,
            {
                "csk-skill.json": json.dumps(
                    {
                        "schema_version": 2,
                        "commands": {"t": {"type": "system", "command": tool}},
                    }
                ),
            },
            tag="v1",
        )
    write_skillfile(
        project,
        {
            "schema_version": 1,
            "skills": [
                {"name": "skill-one", "tag": "v1"},
                {"name": "skill-two", "tag": "v1"},
            ],
        },
    )
    cfg = make_config(csk_home, skills_root, project)

    dry = installer.install(cfg, options=installer.InstallOptions(dry_run=True))[0]

    assert dry.status == "failed"
    assert len(dry.errors) == 1
    assert "__csk_missing_dry_alpha__" in dry.errors[0]
    assert "__csk_missing_dry_beta__" in dry.errors[0]
    assert not (project / ".agents" / "skills" / "skill-one").exists()

    collected = status.collect_status(cfg, alias="app")[0]

    assert not collected.clean
    assert collected.errors
    assert "__csk_missing_dry_alpha__" in collected.errors[0]
    assert "__csk_missing_dry_beta__" in collected.errors[0]
    rendered = status.render_collected([collected])
    assert "__csk_missing_dry_alpha__" in rendered
    assert "__csk_missing_dry_beta__" in rendered


def test_global_reports_every_missing_system_command_in_one_diagnostic(
    monkeypatch, tmp_path, skills_root, csk_home
):
    project = make_project(tmp_path)
    cfg = make_config(csk_home, skills_root, project)
    _save_config(monkeypatch, cfg)
    for name, tool in (
        ("skill-ga", "__csk_missing_global_alpha__"),
        ("skill-gb", "__csk_missing_global_beta__"),
    ):
        make_skill_repo(
            skills_root,
            name,
            {
                "csk-skill.json": json.dumps(
                    {
                        "schema_version": 2,
                        "commands": {
                            "t": {
                                "type": "system",
                                "command": tool,
                                "hint": f"install {name}",
                            }
                        },
                    }
                ),
            },
            tag="v1",
        )
    _write_global_skillfile(
        csk_home,
        {
            "schema_version": 1,
            "agents": ["codex_cli"],
            "skills": [
                {"name": "skill-ga", "tag": "v1"},
                {"name": "skill-gb", "tag": "v1"},
            ],
        },
    )

    def unexpected_plan(*_args, **_kwargs):
        raise AssertionError("global system check must run before build planning")

    monkeypatch.setattr(build_planner, "plan_builds", unexpected_plan)

    result = global_install.install(cfg)

    assert result.status == "failed"
    assert len(result.errors) == 1
    assert "Missing system command '__csk_missing_global_alpha__' for skill-ga" in result.errors[0]
    assert "Missing system command '__csk_missing_global_beta__' for skill-gb" in result.errors[0]
    assert "install skill-ga" in result.errors[0]
    assert "install skill-gb" in result.errors[0]
    assert result.builds == []
    assert not (csk_home / "global" / "skills" / "skill-ga").exists()
    assert not (csk_home / "global" / "skills" / "skill-gb").exists()


def test_global_dry_run_and_status_report_same_missing(
    monkeypatch, tmp_path, skills_root, csk_home
):
    project = make_project(tmp_path)
    cfg = make_config(csk_home, skills_root, project)
    _save_config(monkeypatch, cfg)
    for name, tool in (
        ("skill-gone", "__csk_missing_gdry_alpha__"),
        ("skill-gtwo", "__csk_missing_gdry_beta__"),
    ):
        make_skill_repo(
            skills_root,
            name,
            {
                "csk-skill.json": json.dumps(
                    {
                        "schema_version": 2,
                        "commands": {"t": {"type": "system", "command": tool}},
                    }
                ),
            },
            tag="v1",
        )
    _write_global_skillfile(
        csk_home,
        {
            "schema_version": 1,
            "agents": ["codex_cli"],
            "skills": [
                {"name": "skill-gone", "tag": "v1"},
                {"name": "skill-gtwo", "tag": "v1"},
            ],
        },
    )

    dry = global_install.install(cfg, options=installer.InstallOptions(dry_run=True))

    assert dry.status == "failed"
    assert len(dry.errors) == 1
    assert "__csk_missing_gdry_alpha__" in dry.errors[0]
    assert "__csk_missing_gdry_beta__" in dry.errors[0]

    collected = status.collect_global_status(cfg)

    assert not collected.clean
    assert collected.errors
    assert "__csk_missing_gdry_alpha__" in collected.errors[0]
    assert "__csk_missing_gdry_beta__" in collected.errors[0]


def test_global_only_limits_system_check_to_selected_closure(
    monkeypatch, tmp_path, skills_root, csk_home
):
    project = make_project(tmp_path)
    cfg = make_config(csk_home, skills_root, project, agents=["codex_cli"])
    _save_config(monkeypatch, cfg)
    make_skill_repo(skills_root, "skill-good", tag="v1")
    make_skill_repo(
        skills_root,
        "skill-bad",
        {
            "csk-skill.json": json.dumps(
                {
                    "schema_version": 2,
                    "commands": {
                        "t": {
                            "type": "system",
                            "command": "__csk_missing_only_tool__",
                        }
                    },
                }
            ),
        },
        tag="v1",
    )
    _write_global_skillfile(
        csk_home,
        {
            "schema_version": 1,
            "agents": ["codex_cli"],
            "skills": [
                {"name": "skill-good", "tag": "v1"},
                {"name": "skill-bad", "tag": "v1"},
            ],
        },
    )

    selected_good = global_install.install(cfg, only=["skill-good"])

    assert not selected_good.errors
    assert (csk_home / "global" / "skills" / "skill-good").exists()

    selected_bad = global_install.install(cfg, only=["skill-bad"])

    assert selected_bad.status == "failed"
    assert len(selected_bad.errors) == 1
    assert "__csk_missing_only_tool__" in selected_bad.errors[0]

    full = global_install.install(cfg)

    assert full.status == "failed"
    assert len(full.errors) == 1
    assert "__csk_missing_only_tool__" in full.errors[0]


def test_closure_missing_check_kills_stop_at_first_mutant(tmp_path, skills_root, csk_home):
    project = make_project(tmp_path)
    for name, tool in (
        ("skill-mone", "__csk_missing_mutant_alpha__"),
        ("skill-mtwo", "__csk_missing_mutant_beta__"),
    ):
        make_skill_repo(
            skills_root,
            name,
            {
                "csk-skill.json": json.dumps(
                    {
                        "schema_version": 2,
                        "commands": {"t": {"type": "system", "command": tool}},
                    }
                ),
            },
            tag="v1",
        )
    write_skillfile(
        project,
        {
            "schema_version": 1,
            "skills": [
                {"name": "skill-mone", "tag": "v1"},
                {"name": "skill-mtwo", "tag": "v1"},
            ],
        },
    )
    cfg = make_config(csk_home, skills_root, project)

    from csk import manifest as manifest_mod

    loaded = manifest_mod.load_manifest(project)
    assert loaded is not None
    try:
        closure.build_closure(cfg, loaded, {})
    except closure.MissingSystemCommandsError as exc:
        assert len(exc.missing) == 2
        assert "__csk_missing_mutant_alpha__" in str(exc)
        assert "__csk_missing_mutant_beta__" in str(exc)
    else:
        raise AssertionError("expected MissingSystemCommandsError for two missing tools")


MATRIX_ALPHA = "__csk_matrix_alpha__"
MATRIX_BETA = "__csk_matrix_beta__"


def _write_matrix_skill_repos(skills_root) -> None:
    make_skill_repo(
        skills_root,
        "skill-matrix-one",
        {
            "csk-skill.json": json.dumps(
                {
                    "schema_version": 2,
                    "commands": {
                        "tool-a": {
                            "type": "system",
                            "command": MATRIX_ALPHA,
                            "hint": "install matrix alpha",
                        }
                    },
                }
            ),
        },
        tag="v1",
    )
    make_skill_repo(
        skills_root,
        "skill-matrix-two",
        {
            "csk-skill.json": json.dumps(
                {
                    "schema_version": 2,
                    "commands": {},
                    "dependencies": {
                        "commands": {
                            "tool-b": {
                                "type": "system",
                                "command": MATRIX_BETA,
                                "hint": "install matrix beta",
                            }
                        }
                    },
                }
            ),
        },
        tag="v1",
    )


def test_schema1_all_surfaces_report_same_missing(
    monkeypatch, tmp_path, skills_root, csk_home
):
    """Every schema-1 surface refuses with the same aggregate diagnostic.

    Project/global install, dry-run and status share the one
    collect_missing_system_commands gate: all six report both missing tools
    with hints in a single error and refuse (failed / not clean).
    """

    project = make_project(tmp_path)
    _write_matrix_skill_repos(skills_root)
    declarations = [
        {"name": "skill-matrix-one", "tag": "v1"},
        {"name": "skill-matrix-two", "tag": "v1"},
    ]
    write_skillfile(project, {"schema_version": 1, "skills": declarations})
    cfg = make_config(csk_home, skills_root, project)
    _save_config(monkeypatch, cfg)
    _write_global_skillfile(
        csk_home,
        {"schema_version": 1, "agents": ["codex_cli"], "skills": declarations},
    )

    surfaces: list[tuple[str, str]] = []

    installed = installer.install(cfg)[0]
    assert installed.status == "failed"
    assert len(installed.errors) == 1
    surfaces.append(("project-install", installed.errors[0]))

    dry = installer.install(cfg, options=installer.InstallOptions(dry_run=True))[0]
    assert dry.status == "failed"
    assert len(dry.errors) == 1
    surfaces.append(("project-dry", dry.errors[0]))

    collected = status.collect_status(cfg, alias="app")[0]
    assert not collected.clean
    assert collected.errors
    surfaces.append(("project-status", collected.errors[0]))

    global_result = global_install.install(cfg)
    assert global_result.status == "failed"
    assert len(global_result.errors) == 1
    surfaces.append(("global-install", global_result.errors[0]))

    global_dry = global_install.install(
        cfg, options=installer.InstallOptions(dry_run=True)
    )
    assert global_dry.status == "failed"
    assert len(global_dry.errors) == 1
    surfaces.append(("global-dry", global_dry.errors[0]))

    global_status = status.collect_global_status(cfg)
    assert not global_status.clean
    assert global_status.errors
    surfaces.append(("global-status", global_status.errors[0]))

    assert len(surfaces) == 6
    for name, diagnostic in surfaces:
        assert MATRIX_ALPHA in diagnostic, name
        assert MATRIX_BETA in diagnostic, name
        assert "install matrix alpha" in diagnostic, name
        assert "install matrix beta" in diagnostic, name


def _make_status_matrix_tool(directory, name: str):
    directory.mkdir(parents=True, exist_ok=True)
    filename = f"{name}.cmd" if os.name == "nt" else name
    tool = directory / filename
    tool.write_text(
        "@echo off\necho ok\n" if os.name == "nt" else "#!/bin/sh\necho ok\n",
        encoding="utf-8",
    )
    tool.chmod(0o755)
    return tool


def test_schema2_all_surfaces_report_same_missing(
    monkeypatch, tmp_path, skills_root, csk_home
):
    """Every schema-2 project surface refuses with the same aggregate diagnostic.

    Install a path-source skill with two explicit system dependencies while
    both tools resolve, then drop the tools directory from PATH: project
    install, dry-run and status --check all refuse with both tools and hints.
    Global schema-2 stays unsupported (covered by existing unsupported tests).
    """

    from conftest import write_files
    from test_source_runtime import (
        _require_selection,
        _skill_files,
        _v2_config,
        _write_skillfile_v2,
    )

    _require_selection()
    project = make_project(tmp_path)
    tools = tmp_path / "matrix-bin"
    for tool_name in (MATRIX_ALPHA, MATRIX_BETA):
        _make_status_matrix_tool(tools, tool_name)
    monkeypatch.setenv(
        "PATH", str(tools) + os.pathsep + os.environ.get("PATH", "")
    )
    source = tmp_path / "pkgs"
    write_files(
        source / "needy",
        _skill_files(
            "needy",
            "need",
            "#!/bin/sh\necho need\n",
            runtime_roots=True,
            dependencies={
                f"dep{i}": {
                    "type": "system",
                    "command": tool,
                    "hint": f"install matrix {tool}",
                }
                for i, tool in enumerate((MATRIX_ALPHA, MATRIX_BETA))
            },
        ),
    )
    _write_skillfile_v2(project, source, [("needy", "needy")])
    cfg = _v2_config(csk_home, skills_root, project)
    config.save_config(cfg)
    monkeypatch.setenv("CSK_CONFIG", str(cfg.path))
    monkeypatch.chdir(project)

    installed = installer.install(cfg, alias="app")[0]
    assert installed.status == "ok", installed.errors

    monkeypatch.setenv("PATH", os.environ.get("PATH", "").replace(
        str(tools) + os.pathsep, ""
    ).replace(str(tools), ""))

    surfaces: list[tuple[str, str]] = []

    reinstall = installer.install(cfg, alias="app")[0]
    assert reinstall.status == "failed"
    assert reinstall.errors
    surfaces.append(("install", reinstall.errors[0]))

    dry = installer.install(
        cfg, alias="app", options=installer.InstallOptions(dry_run=True)
    )[0]
    assert dry.status == "failed"
    assert dry.errors
    surfaces.append(("dry", dry.errors[0]))

    collected = status.collect_status(cfg, alias="app")[0]
    assert not collected.clean, collected
    assert collected.errors
    surfaces.append(("status", collected.errors[0]))

    assert len(surfaces) == 3
    for name, diagnostic in surfaces:
        assert MATRIX_ALPHA in diagnostic, (name, diagnostic)
        assert MATRIX_BETA in diagnostic, (name, diagnostic)
        assert f"install matrix {MATRIX_ALPHA}" in diagnostic, (name, diagnostic)
        assert f"install matrix {MATRIX_BETA}" in diagnostic, (name, diagnostic)


@pytest.mark.parametrize("scope", ["project", "global"])
def test_duplicate_executable_keeps_useful_hint(
    scope, monkeypatch, tmp_path, skills_root, csk_home
):
    """A hintless legacy entry plus a hinted explicit entry keeps the hint.

    Regression for duplicate-system-command-hint-loss: the same missing
    executable declared as a legacy system command (no hint) and as an
    explicit system dependency (with hint) must report the useful hint.
    """

    project = make_project(tmp_path)
    make_skill_repo(
        skills_root,
        "skill-dup",
        {
            "csk-skill.json": json.dumps(
                {
                    "schema_version": 4,
                    "capabilities": {},
                    "commands": {
                        "legacy": {
                            "type": "system",
                            "command": "__csk_dup_tool__",
                        }
                    },
                    "dependencies": {
                        "commands": {
                            "explicit": {
                                "type": "system",
                                "command": "__csk_dup_tool__",
                                "hint": "explicit repair hint",
                            }
                        }
                    },
                }
            ),
        },
        tag="v1",
    )
    make_skill_repo(
        skills_root,
        "skill-other",
        {
            "csk-skill.json": json.dumps(
                {
                    "schema_version": 2,
                    "commands": {
                        "t": {
                            "type": "system",
                            "command": "__csk_dup_other__",
                            "hint": "other hint",
                        }
                    },
                }
            ),
        },
        tag="v1",
    )
    declarations = [
        {"name": "skill-dup", "tag": "v1"},
        {"name": "skill-other", "tag": "v1"},
    ]
    write_skillfile(project, {"schema_version": 1, "skills": declarations})
    cfg = make_config(csk_home, skills_root, project)
    _save_config(monkeypatch, cfg)
    _write_global_skillfile(
        csk_home,
        {"schema_version": 1, "agents": ["codex_cli"], "skills": declarations},
    )

    if scope == "project":
        result = installer.install(cfg)[0]
        assert result.status == "failed"
        assert len(result.errors) == 1
        diagnostic = result.errors[0]
    else:
        result = global_install.install(cfg)
        assert result.status == "failed"
        assert len(result.errors) == 1
        diagnostic = result.errors[0]

    assert "__csk_dup_tool__" in diagnostic
    assert "explicit repair hint" in diagnostic
    assert "__csk_dup_other__" in diagnostic


def test_duplicate_executable_merges_two_distinct_hints(
    tmp_path, skills_root, csk_home
):
    """Two distinct hints for one executable are both preserved."""

    from csk import skillspec

    spec = skillspec.SkillSpec(
        commands={
            "legacy": skillspec.CommandSpec(
                name="legacy", type="system", command="__csk_dup_merge__",
                hint="legacy hint",
            )
        },
        source_file="csk-skill.json",
        dependencies={
            "explicit": skillspec.DependencySpec(
                name="explicit", type="system", command="__csk_dup_merge__",
                hint="explicit hint",
            )
        },
    )
    missing = closure.missing_system_commands_for_spec("skill-merge", spec)
    assert len(missing) == 1
    assert missing[0].command == "__csk_dup_merge__"
    assert missing[0].hint is not None
    assert "legacy hint" in missing[0].hint
    assert "explicit hint" in missing[0].hint
    diagnostic = closure.format_missing_system_commands(missing)
    assert "legacy hint" in diagnostic
    assert "explicit hint" in diagnostic


@pytest.mark.parametrize("state", ["missing-marker", "source-drift"])
def test_schema2_noncurrent_status_reports_missing_system_commands(
    state, monkeypatch, tmp_path, skills_root, csk_home, capsys
):
    """Non-current schema-2 status still reports every missing system command.

    Regression for schema2-status-gate-bypass (panels' reproduction): install
    a path-source skill while both tools resolve, then break currency (remove
    the install marker, or drift the live source) and remove the tools from
    PATH. Dry-run and status --check report the same missing commands, and
    the non-current member diagnostic is retained alongside them.
    """

    from conftest import write_files
    from test_source_runtime import (
        _require_selection,
        _skill_files,
        _v2_config,
        _write_skillfile_v2,
    )

    _require_selection()
    project = make_project(tmp_path)
    tools = tmp_path / "noncurrent-bin"
    for tool_name in (MATRIX_ALPHA, MATRIX_BETA):
        _make_status_matrix_tool(tools, tool_name)
    monkeypatch.setenv(
        "PATH", str(tools) + os.pathsep + os.environ.get("PATH", "")
    )
    source = tmp_path / "pkgs"
    write_files(
        source / "needy",
        _skill_files(
            "needy",
            "need",
            "#!/bin/sh\necho need\n",
            runtime_roots=True,
            dependencies={
                f"dep{i}": {
                    "type": "system",
                    "command": tool,
                    "hint": f"install matrix {tool}",
                }
                for i, tool in enumerate((MATRIX_ALPHA, MATRIX_BETA))
            },
        ),
    )
    _write_skillfile_v2(project, source, [("needy", "needy")])
    cfg = _v2_config(csk_home, skills_root, project)
    config.save_config(cfg)
    monkeypatch.setenv("CSK_CONFIG", str(cfg.path))
    monkeypatch.chdir(project)

    assert cli.main(["install"]) == 0, capsys.readouterr()
    capsys.readouterr()
    if state == "missing-marker":
        (project / ".agents/skills/needy/.csk-install.json").unlink()
    else:
        (source / "needy/SKILL.md").write_text("# changed source\n")
    monkeypatch.setenv(
        "PATH",
        os.environ.get("PATH", "")
        .replace(str(tools) + os.pathsep, "")
        .replace(str(tools), ""),
    )

    assert cli.main(["install", "--dry-run"]) == 1
    output = capsys.readouterr()
    diagnostic = output.out + output.err
    if state == "missing-marker":
        assert MATRIX_ALPHA in diagnostic, diagnostic
        assert MATRIX_BETA in diagnostic, diagnostic

    assert cli.main(["status", "--check"]) == 1
    output = capsys.readouterr()
    diagnostic = output.out + output.err
    assert MATRIX_ALPHA in diagnostic, diagnostic
    assert MATRIX_BETA in diagnostic, diagnostic
    assert f"install matrix {MATRIX_ALPHA}" in diagnostic, diagnostic
    assert f"install matrix {MATRIX_BETA}" in diagnostic, diagnostic
    expected_detail = (
        "install marker is missing"
        if state == "missing-marker"
        else "changed since the lock was written"
    )
    assert expected_detail in diagnostic, diagnostic


@pytest.mark.parametrize("scope", ["project", "global"])
def test_duplicate_executable_keeps_substring_hints(
    scope, monkeypatch, tmp_path, skills_root, csk_home
):
    """Substring hints for one executable are distinct instructions, all kept.

    Regression for duplicate-system-command-hint-loss (panels' reproduction):
    legacy "Do not install via apt" plus explicit "install via apt" on the
    same missing executable must both survive, in declaration order.
    Deduplication is exact equality after whitespace normalization only.
    """

    project = make_project(tmp_path)
    make_skill_repo(
        skills_root,
        "skill-overlap",
        {
            "csk-skill.json": json.dumps(
                {
                    "schema_version": 4,
                    "capabilities": {},
                    "commands": {
                        "legacy": {
                            "type": "system",
                            "command": "__csk_hint_overlap__",
                            "hint": "Do not install via apt",
                        }
                    },
                    "dependencies": {
                        "commands": {
                            "explicit": {
                                "type": "system",
                                "command": "__csk_hint_overlap__",
                                "hint": "install via apt",
                            }
                        }
                    },
                }
            ),
        },
        tag="v1",
    )
    declarations = [{"name": "skill-overlap", "tag": "v1"}]
    write_skillfile(project, {"schema_version": 1, "skills": declarations})
    cfg = make_config(csk_home, skills_root, project)
    _save_config(monkeypatch, cfg)
    _write_global_skillfile(
        csk_home,
        {"schema_version": 1, "agents": ["codex_cli"], "skills": declarations},
    )

    if scope == "project":
        result = installer.install(cfg)[0]
        assert result.status == "failed"
        assert len(result.errors) == 1
        diagnostic = result.errors[0]
    else:
        result = global_install.install(cfg)
        assert result.status == "failed"
        assert len(result.errors) == 1
        diagnostic = result.errors[0]

    assert "Do not install via apt; install via apt" in diagnostic, diagnostic


def test_duplicate_executable_collapses_whitespace_equal_hints():
    """Hints equal after whitespace normalization collapse to first spelling."""

    from csk import skillspec

    spec = skillspec.SkillSpec(
        commands={
            "legacy": skillspec.CommandSpec(
                name="legacy",
                type="system",
                command="__csk_dup_ws__",
                hint="install foo",
            )
        },
        source_file="csk-skill.json",
        dependencies={
            "explicit": skillspec.DependencySpec(
                name="explicit",
                type="system",
                command="__csk_dup_ws__",
                hint="  install   foo  ",
            )
        },
    )
    missing = closure.missing_system_commands_for_spec("skill-ws", spec)
    assert len(missing) == 1
    assert missing[0].hint == "install foo"
