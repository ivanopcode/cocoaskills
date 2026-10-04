from __future__ import annotations

import json

import pytest
from conftest import (
    make_config,
    make_project,
    make_skill_repo,
    write_skillfile,
)

from csk import closure, config, global_install, installer, status
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
