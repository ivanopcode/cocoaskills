from __future__ import annotations

import argparse
import json
import io
import os
import random
import shutil
import subprocess
import sys
import time
from pathlib import Path

import pytest
from conftest import make_project, make_skill_repo, run, write_skillfile

from csk import cli, config, installer, shims, status
from csk.sources import _selection_fs
from csk.sources import diagnostics as source_diagnostics
from csk.sources import errors as source_errors
from csk.sources import store as source_store
from csk.sources import transport as source_transport
from csk.sources.repository_policy import EndpointProvenance


def test_cli_version(capsys):
    code = cli.main(["--version"])
    out = capsys.readouterr().out
    assert code == 0
    assert out.startswith("csk ")


def test_cli_shell_init_install_writes_atomic_cache(monkeypatch, tmp_path, capsys):
    csk_home = tmp_path / "csk home"
    monkeypatch.setenv("CSK_CONFIG", str(csk_home / "config.json"))

    code = cli.main(["shell-init", "zsh", "--install"])

    assert code == 0
    hook_path = csk_home / "hooks" / "csk.zsh"
    assert hook_path.read_text(encoding="utf-8").startswith("# CocoaSkill shell hook\n")
    output = capsys.readouterr().out
    assert f"Wrote {hook_path}" in output
    assert f". '{hook_path}'" in output


def test_cli_shell_init_auto_detects_powershell(monkeypatch, tmp_path, capsys):
    csk_home = tmp_path / "csk home"
    monkeypatch.setenv("CSK_CONFIG", str(csk_home / "config.json"))
    monkeypatch.delenv("SHELL", raising=False)
    monkeypatch.setenv("PSModulePath", "modules")

    code = cli.main(["shell-init", "--install"])

    assert code == 0
    hook_path = csk_home / "hooks" / "csk.ps1"
    assert hook_path.read_text(encoding="utf-8").startswith("# CocoaSkill shell hook\n")
    output = capsys.readouterr().out
    assert f"Wrote {hook_path}" in output
    assert f". '{hook_path}'" in output


def test_cli_help_for_commands(capsys):
    assert cli.main(["--help"]) == 0
    top = capsys.readouterr().out
    assert "install" in top
    assert "skill check" in top
    assert cli.main(["install", "--help"]) == 0
    install_help = capsys.readouterr().out
    assert "--strict-tags" in install_help


def test_cli_project_add_creates_skillfile(monkeypatch, tmp_path, csk_home):
    cfg_path = csk_home / "config.json"
    cfg_path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "skills_root": str(tmp_path / "skills"),
                "projects": {},
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("CSK_CONFIG", str(cfg_path))
    project = make_project(tmp_path)

    code = cli.main(["project", "add", "app", str(project)])

    assert code == 0
    assert (project / "Skillfile.json").exists()
    loaded = config.load_config(cfg_path)
    assert "app" in loaded.projects
    data = json.loads((project / "Skillfile.json").read_text(encoding="utf-8"))
    assert data["project"]["alias"] == "app"
    assert data["agents"] == ["codex_cli"]


def test_cli_skill_check_does_not_require_config(monkeypatch, tmp_path, capsys):
    monkeypatch.setenv("CSK_CONFIG", str(tmp_path / "missing-config.json"))
    skill = tmp_path / "skill"
    skill.mkdir()
    (skill / "SKILL.md").write_text("---\nname: skill\n---\n", encoding="utf-8")

    code = cli.main(["skill", "check", str(skill)])
    captured = capsys.readouterr()

    assert code == 0
    assert captured.out.strip().endswith(": ok")
    assert captured.err == ""


def test_cli_skill_check_non_skill_dir_returns_error(tmp_path, capsys):
    code = cli.main(["skill", "check", str(tmp_path)])
    captured = capsys.readouterr()

    assert code == cli.EXIT_PARTIAL_FAIL
    assert "skill.missing_skill_md" in captured.out


def test_cli_skill_check_json_output(tmp_path, capsys):
    code = cli.main(["skill", "check", str(tmp_path), "--json"])
    captured = capsys.readouterr()

    assert code == cli.EXIT_PARTIAL_FAIL
    data = json.loads(captured.out)
    assert data[0]["severity"] == "error"
    assert data[0]["code"] == "skill.missing_skill_md"


def test_cli_init_creates_skillfile_and_gitignore_in_git_repo(monkeypatch, tmp_path, csk_home, skills_root, capsys):
    project = make_project(tmp_path, gitignore=False)
    cfg_path = csk_home / "config.json"
    cfg_path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "skills_root": str(skills_root),
                "default_agents": ["codex_cli", "cursor"],
                "projects": {},
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("CSK_CONFIG", str(cfg_path))

    code = cli.main(["init", str(project), "--alias", "Demo iOS"])
    captured = capsys.readouterr()

    assert code == 0
    assert captured.err == ""
    data = json.loads((project / "Skillfile.json").read_text(encoding="utf-8"))
    assert data == {
        "schema_version": 1,
        "project": {"alias": "demo-ios"},
        "agents": ["codex_cli", "cursor"],
        "skills": [],
    }
    gitignore = (project / ".gitignore").read_text(encoding="utf-8")
    for entry in [".agents/", ".claude/skills/", ".codex/skills/", ".cursor/rules/", ".gemini/skills/"]:
        assert entry in gitignore


def test_cli_init_non_git_warns_but_creates_project(monkeypatch, tmp_path, capsys):
    project = tmp_path / "plain-project"
    project.mkdir()
    monkeypatch.setenv("CSK_CONFIG", str(tmp_path / "missing-config.json"))
    monkeypatch.chdir(project)

    code = cli.main(["init"])
    captured = capsys.readouterr()

    assert code == 0
    assert "not inside a git repository" in captured.err
    data = json.loads((project / "Skillfile.json").read_text(encoding="utf-8"))
    assert data["project"]["alias"] == "plain-project"
    assert data["agents"] == ["codex_cli"]
    assert ".agents/" in (project / ".gitignore").read_text(encoding="utf-8")


def test_cli_init_is_idempotent_and_does_not_overwrite_skillfile(monkeypatch, tmp_path, csk_home, skills_root):
    project = make_project(tmp_path, gitignore=False)
    write_skillfile(project, {"schema_version": 1, "project": {"alias": "custom"}, "agents": ["gemini"], "skills": []})
    original = (project / "Skillfile.json").read_text(encoding="utf-8")

    assert cli.main(["init", str(project), "--alias", "other"]) == 0
    assert cli.main(["init", str(project), "--alias", "other"]) == 0

    assert (project / "Skillfile.json").read_text(encoding="utf-8") == original
    gitignore = (project / ".gitignore").read_text(encoding="utf-8")
    assert gitignore.count("# CocoaSkill") == 1


def test_cli_init_rejects_nested_project(tmp_path):
    project = make_project(tmp_path)
    write_skillfile(project, {"schema_version": 1, "skills": []})
    nested = project / "Nested"
    nested.mkdir()

    assert cli.main(["init", str(nested)]) == cli.EXIT_CONFIG


def test_cli_init_non_git_then_git_init_leaves_installable_empty_project(monkeypatch, tmp_path, csk_home, skills_root):
    make_skill_repo(skills_root, "unused", tag="v1")
    project = tmp_path / "plain-project"
    project.mkdir()
    assert cli.main(["init", str(project)]) == 0
    run(["git", "init"], project)
    run(["git", "branch", "-M", "main"], project)
    run(["git", "config", "user.name", "Test User"], project)
    run(["git", "config", "user.email", "test@example.com"], project)
    cfg_path = csk_home / "config.json"
    cfg_path.write_text(
        json.dumps({"schema_version": 1, "skills_root": str(skills_root), "projects": {}}),
        encoding="utf-8",
    )
    monkeypatch.setenv("CSK_CONFIG", str(cfg_path))
    monkeypatch.chdir(project)

    assert cli.main(["install"]) == 0


def test_cli_install_dot_uses_current_checkout_without_saving_config(monkeypatch, tmp_path, csk_home, skills_root, capsys):
    make_skill_repo(skills_root, "skill-a", tag="v1")
    project = make_project(tmp_path)
    write_skillfile(
        project,
        {
            "schema_version": 1,
            "project": {"alias": "demo-ios"},
            "agents": ["codex_cli"],
            "skills": [{"name": "skill-a", "tag": "v1"}],
        },
    )
    run(["git", "checkout", "-b", "feature/TASK-4242-install"], project)
    cfg_path = csk_home / "config.json"
    cfg_path.write_text(
        json.dumps({"schema_version": 1, "skills_root": str(skills_root), "projects": {}}),
        encoding="utf-8",
    )
    monkeypatch.setenv("CSK_CONFIG", str(cfg_path))
    monkeypatch.chdir(project)

    code = cli.main(["install", "."])
    out = capsys.readouterr().out

    assert code == 0
    loaded = config.load_config(cfg_path)
    assert loaded.projects == {}
    assert "demo-ios-task-4242-" in out
    assert (project / ".agents" / "skills" / "skill-a" / "SKILL.md").exists()


def test_cli_install_tilde_path_uses_checkout_without_saving_config(monkeypatch, tmp_path, csk_home, skills_root, capsys):
    make_skill_repo(skills_root, "skill-a", tag="v1")
    home_project = tmp_path / "home" / "project"
    project = make_project(home_project.parent, "project")
    write_skillfile(project, {"schema_version": 1, "project": {"alias": "home-app"}, "skills": [{"name": "skill-a", "tag": "v1"}]})
    cfg_path = csk_home / "config.json"
    cfg_path.write_text(
        json.dumps({"schema_version": 1, "skills_root": str(skills_root), "projects": {}}),
        encoding="utf-8",
    )
    monkeypatch.setenv("CSK_CONFIG", str(cfg_path))
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setenv("USERPROFILE", str(tmp_path / "home"))

    code = cli.main(["install", "~/project"])
    out = capsys.readouterr().out

    assert code == 0
    loaded = config.load_config(cfg_path)
    assert loaded.projects == {}
    assert "home-app:" in out
    assert (project / ".agents" / "skills" / "skill-a" / "SKILL.md").exists()


def test_cli_status_dot_uses_current_checkout_without_saving_config(monkeypatch, tmp_path, csk_home, skills_root, capsys):
    make_skill_repo(skills_root, "skill-a", tag="v1")
    project = make_project(tmp_path)
    write_skillfile(
        project,
        {
            "schema_version": 1,
            "project": {"alias": "demo-ios"},
            "skills": [{"name": "skill-a", "tag": "v1"}],
        },
    )
    cfg_path = csk_home / "config.json"
    cfg_path.write_text(
        json.dumps({"schema_version": 1, "skills_root": str(skills_root), "projects": {}}),
        encoding="utf-8",
    )
    monkeypatch.setenv("CSK_CONFIG", str(cfg_path))
    monkeypatch.chdir(project)

    code = cli.main(["status", "."])
    out = capsys.readouterr().out

    assert code == 0
    assert "Project demo-ios" in out
    assert "missing" in out
    assert config.load_config(cfg_path).projects == {}


def test_cli_install_dot_dry_run_does_not_save_config(monkeypatch, tmp_path, csk_home, skills_root):
    make_skill_repo(skills_root, "skill-a", tag="v1")
    project = make_project(tmp_path)
    write_skillfile(
        project,
        {
            "schema_version": 1,
            "project": {"alias": "demo-ios"},
            "skills": [{"name": "skill-a", "tag": "v1"}],
        },
    )
    cfg_path = csk_home / "config.json"
    cfg_path.write_text(
        json.dumps({"schema_version": 1, "skills_root": str(skills_root), "projects": {}}),
        encoding="utf-8",
    )
    monkeypatch.setenv("CSK_CONFIG", str(cfg_path))
    monkeypatch.chdir(project)

    code = cli.main(["install", ".", "--dry-run"])

    assert code == 0
    assert config.load_config(cfg_path).projects == {}
    assert not (project / ".agents").exists()


def test_cli_bare_install_uses_current_project(monkeypatch, tmp_path, csk_home, skills_root, capsys):
    make_skill_repo(skills_root, "skill-a", tag="v1")
    project = make_project(tmp_path)
    write_skillfile(project, {"schema_version": 1, "skills": [{"name": "skill-a", "tag": "v1"}]})
    cfg_path = csk_home / "config.json"
    cfg_path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "skills_root": str(skills_root),
                "projects": {"app": {"path": str(project), "agents": ["codex_cli"]}},
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("CSK_CONFIG", str(cfg_path))
    monkeypatch.chdir(project)

    code = cli.main(["install"])
    captured = capsys.readouterr()

    assert code == 0
    assert captured.err == ""
    assert (project / ".agents" / "skills" / "skill-a" / "SKILL.md").exists()


def test_cli_bare_status_uses_current_project(monkeypatch, tmp_path, csk_home, skills_root, capsys):
    project = make_project(tmp_path)
    write_skillfile(project, {"schema_version": 1, "project": {"alias": "app"}, "skills": []})
    cfg_path = csk_home / "config.json"
    cfg_path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "skills_root": str(skills_root),
                "projects": {"app": {"path": str(project), "agents": ["codex_cli"]}},
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("CSK_CONFIG", str(cfg_path))
    monkeypatch.chdir(project)

    code = cli.main(["status"])
    captured = capsys.readouterr()

    assert code == 0
    assert captured.err == ""
    assert "Project app" in captured.out


def test_cli_bare_upgrade_uses_current_project(monkeypatch, tmp_path, csk_home, skills_root, capsys):
    make_skill_repo(skills_root, "skill-a", tag="v1")
    project = make_project(tmp_path)
    write_skillfile(project, {"schema_version": 1, "skills": [{"name": "skill-a", "tag": "v1"}]})
    cfg_path = csk_home / "config.json"
    cfg_path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "skills_root": str(skills_root),
                "projects": {"app": {"path": str(project), "agents": ["codex_cli"]}},
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("CSK_CONFIG", str(cfg_path))
    monkeypatch.chdir(project)

    code = cli.main(["upgrade"])
    captured = capsys.readouterr()

    assert code == 0
    assert captured.err == ""
    assert (project / ".agents" / "skills" / "skill-a" / "SKILL.md").exists()


def test_cli_install_dot_does_not_auto_register_or_warn(monkeypatch, tmp_path, csk_home, skills_root, capsys):
    make_skill_repo(skills_root, "skill-a", tag="v1")
    project = make_project(tmp_path)
    write_skillfile(project, {"schema_version": 1, "skills": [{"name": "skill-a", "tag": "v1"}]})
    cfg_path = csk_home / "config.json"
    cfg_path.write_text(
        json.dumps({"schema_version": 1, "skills_root": str(skills_root), "projects": {}}),
        encoding="utf-8",
    )
    monkeypatch.setenv("CSK_CONFIG", str(cfg_path))
    monkeypatch.chdir(project)

    code = cli.main(["install", "."])
    err = capsys.readouterr().err

    assert code == 0
    assert err == ""
    assert config.load_config(cfg_path).projects == {}


def test_cli_fix_gitignore_emits_deprecation_warning(monkeypatch, tmp_path, csk_home, skills_root, capsys):
    make_skill_repo(skills_root, "skill-a", tag="v1")
    project = make_project(tmp_path, gitignore=False)
    write_skillfile(project, {"schema_version": 1, "skills": [{"name": "skill-a", "tag": "v1"}]})
    cfg_path = csk_home / "config.json"
    cfg_path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "skills_root": str(skills_root),
                "projects": {"app": {"path": str(project), "agents": ["codex_cli"]}},
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("CSK_CONFIG", str(cfg_path))

    code = cli.main(["install", "app", "--fix-gitignore"])
    err = capsys.readouterr().err

    assert code == 0
    assert "--fix-gitignore: WARNING - deprecated for regular install flows" in err
    assert "prefer 'csk init' once per project" in err
    assert "scheduled for removal in a future release" in err


def test_cli_install_all_uses_registered_projects(monkeypatch, tmp_path, csk_home, skills_root):
    make_skill_repo(skills_root, "skill-a", tag="v1")
    project_one = make_project(tmp_path, "one")
    project_two = make_project(tmp_path, "two")
    for project in (project_one, project_two):
        write_skillfile(project, {"schema_version": 1, "skills": [{"name": "skill-a", "tag": "v1"}]})
    cfg_path = csk_home / "config.json"
    cfg_path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "skills_root": str(skills_root),
                "projects": {
                    "one": {"path": str(project_one), "agents": ["codex_cli"]},
                    "two": {"path": str(project_two), "agents": ["codex_cli"]},
                },
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("CSK_CONFIG", str(cfg_path))

    assert cli.main(["install", "--all"]) == 0

    assert (project_one / ".agents" / "skills" / "skill-a" / "SKILL.md").exists()
    assert (project_two / ".agents" / "skills" / "skill-a" / "SKILL.md").exists()


def test_cli_install_all_rejects_target(monkeypatch, tmp_path, csk_home, skills_root):
    cfg_path = csk_home / "config.json"
    cfg_path.write_text(
        json.dumps({"schema_version": 1, "skills_root": str(skills_root), "projects": {}}),
        encoding="utf-8",
    )
    monkeypatch.setenv("CSK_CONFIG", str(cfg_path))

    assert cli.main(["install", "app", "--all"]) == cli.EXIT_CONFIG


def test_cli_status_all_reports_registered_projects(monkeypatch, tmp_path, csk_home, skills_root, capsys):
    project_one = make_project(tmp_path, "one")
    project_two = make_project(tmp_path, "two")
    for project in (project_one, project_two):
        write_skillfile(project, {"schema_version": 1, "skills": []})
    cfg_path = csk_home / "config.json"
    cfg_path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "skills_root": str(skills_root),
                "projects": {
                    "one": {"path": str(project_one), "agents": ["codex_cli"]},
                    "two": {"path": str(project_two), "agents": ["codex_cli"]},
                },
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("CSK_CONFIG", str(cfg_path))

    assert cli.main(["status", "--all"]) == 0
    out = capsys.readouterr().out

    assert f"Project one ({project_one})" in out
    assert f"Project two ({project_two})" in out


def test_cli_project_resolve_reports_current_checkout(monkeypatch, tmp_path, csk_home, skills_root, capsys):
    project = make_project(tmp_path)
    write_skillfile(project, {"schema_version": 1, "project": {"alias": "demo-ios"}, "skills": []})
    cfg_path = csk_home / "config.json"
    cfg_path.write_text(
        json.dumps({"schema_version": 1, "skills_root": str(skills_root), "projects": {}}),
        encoding="utf-8",
    )
    monkeypatch.setenv("CSK_CONFIG", str(cfg_path))
    monkeypatch.chdir(project)

    code = cli.main(["project", "resolve", "."])
    out = capsys.readouterr().out

    assert code == 0
    assert "project_alias: demo-ios" in out
    assert "checkout_alias: demo-ios" in out
    assert f"skillfile: {project / 'Skillfile.json'}" in out


def test_cli_project_resolve_configured_alias_reports_git_fields(monkeypatch, tmp_path, csk_home, skills_root, capsys):
    project = make_project(tmp_path)
    write_skillfile(project, {"schema_version": 1, "project": {"alias": "demo-ios"}, "skills": []})
    run(["git", "checkout", "-b", "feature/TASK-4242-resolve"], project)
    cfg_path = csk_home / "config.json"
    cfg_path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "skills_root": str(skills_root),
                "projects": {
                    "demo-ios-task-4242-test": {
                        "path": str(project),
                        "agents": ["codex_cli"],
                        "project_alias": "demo-ios",
                        "checkout_alias": "demo-ios-task-4242-test",
                    }
                },
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("CSK_CONFIG", str(cfg_path))

    code = cli.main(["project", "resolve", "demo-ios-task-4242-test"])
    out = capsys.readouterr().out

    assert code == 0
    assert "branch: feature/TASK-4242-resolve" in out
    assert "task_id: task-4242" in out
    assert "path_hash: " in out and "path_hash: \n" not in out


def test_cli_project_resolve_unknown_alias_is_clean_error(monkeypatch, csk_home, skills_root):
    cfg_path = csk_home / "config.json"
    cfg_path.write_text(
        json.dumps({"schema_version": 1, "skills_root": str(skills_root), "projects": {}}),
        encoding="utf-8",
    )
    monkeypatch.setenv("CSK_CONFIG", str(cfg_path))

    assert cli.main(["project", "resolve", "missing"]) == cli.EXIT_CONFIG


def test_cli_install_dot_without_skillfile_returns_clean_config_error(monkeypatch, tmp_path, csk_home, skills_root):
    cfg_path = csk_home / "config.json"
    cfg_path.write_text(
        json.dumps({"schema_version": 1, "skills_root": str(skills_root), "projects": {}}),
        encoding="utf-8",
    )
    monkeypatch.setenv("CSK_CONFIG", str(cfg_path))
    monkeypatch.chdir(tmp_path)

    assert cli.main(["install", "."]) == cli.EXIT_CONFIG


def test_cli_bare_install_without_skillfile_hints_all(monkeypatch, tmp_path, csk_home, skills_root, capsys):
    make_skill_repo(skills_root, "unused", tag="v1")
    project = make_project(tmp_path)
    cfg_path = csk_home / "config.json"
    cfg_path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "skills_root": str(skills_root),
                "projects": {"app": {"path": str(project), "agents": ["codex_cli"]}},
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("CSK_CONFIG", str(cfg_path))
    monkeypatch.chdir(tmp_path)

    assert cli.main(["install"]) == cli.EXIT_CONFIG
    err = capsys.readouterr().err

    assert "no Skillfile.json found" in err
    assert "csk install --all" in err


def test_cli_list_paths_shows_alias_layers(monkeypatch, tmp_path, csk_home, skills_root, capsys):
    project = make_project(tmp_path)
    cfg_path = csk_home / "config.json"
    cfg_path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "skills_root": str(skills_root),
                "projects": {
                    "demo-ios-task-4242": {
                        "path": str(project),
                        "agents": ["codex_cli"],
                        "project_alias": "demo-ios",
                        "checkout_alias": "demo-ios-task-4242",
                    }
                },
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("CSK_CONFIG", str(cfg_path))

    code = cli.main(["list", "--paths"])
    out = capsys.readouterr().out

    assert code == 0
    assert "project_alias=demo-ios" in out
    assert "checkout_alias=demo-ios-task-4242" in out
    assert f"path={project}" in out


def test_cli_list_paths_marks_missing_project_paths(monkeypatch, tmp_path, csk_home, skills_root, capsys):
    cfg_path = csk_home / "config.json"
    missing = tmp_path / "missing-project"
    cfg_path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "skills_root": str(skills_root),
                "projects": {"ghost": {"path": str(missing), "agents": []}},
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("CSK_CONFIG", str(cfg_path))

    code = cli.main(["list", "--paths"])
    out = capsys.readouterr().out

    assert code == 0
    assert f"path={missing} (missing)" in out


def test_cli_missing_config_returns_config_exit(monkeypatch, tmp_path):
    monkeypatch.setenv("CSK_CONFIG", str(tmp_path / "missing.json"))
    assert cli.main(["list"]) == cli.EXIT_CONFIG


def test_cli_unknown_install_alias_returns_clean_config_error(monkeypatch, tmp_path, csk_home, skills_root):
    cfg_path = csk_home / "config.json"
    cfg_path.write_text(
        json.dumps({"schema_version": 1, "skills_root": str(skills_root), "projects": {}}),
        encoding="utf-8",
    )
    monkeypatch.setenv("CSK_CONFIG", str(cfg_path))
    assert cli.main(["install", "missing"]) == cli.EXIT_CONFIG


def test_cli_project_add_requires_existing_path(monkeypatch, tmp_path, csk_home):
    cfg_path = csk_home / "config.json"
    (tmp_path / "skills").mkdir()
    cfg_path.write_text(
        json.dumps({"schema_version": 1, "skills_root": str(tmp_path / "skills"), "projects": {}}),
        encoding="utf-8",
    )
    monkeypatch.setenv("CSK_CONFIG", str(cfg_path))
    assert cli.main(["project", "add", "app", str(tmp_path / "does-not-exist")]) == cli.EXIT_CONFIG


def test_cli_missing_skills_root_returns_config_exit(monkeypatch, tmp_path, csk_home):
    cfg_path = csk_home / "config.json"
    cfg_path.write_text(
        json.dumps({"schema_version": 1, "skills_root": str(tmp_path / "missing"), "projects": {}}),
        encoding="utf-8",
    )
    monkeypatch.setenv("CSK_CONFIG", str(cfg_path))
    monkeypatch.chdir(tmp_path)
    assert cli.main(["install"]) == cli.EXIT_CONFIG


def test_cli_lock_contention_returns_lock_exit(tmp_path, csk_home, skills_root):
    project = make_project(tmp_path)
    make_skill_repo(skills_root, "skill-a", tag="v1")
    write_skillfile(project, {"schema_version": 1, "skills": [{"name": "skill-a", "tag": "v1"}]})
    cfg_path = csk_home / "config.json"
    cfg_path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "skills_root": str(skills_root),
                "projects": {"app": {"path": str(project), "agents": ["codex_cli"]}},
            }
        ),
        encoding="utf-8",
    )
    lock_path = csk_home / ".lock"
    # The holder must be alive, otherwise the stale-lock breaker removes it.
    lock_path.write_text(json.dumps({"pid": os.getpid(), "created_at": time.time()}), encoding="utf-8")
    env = os.environ.copy()
    env["CSK_CONFIG"] = str(cfg_path)
    env["CSK_LOCK_TIMEOUT"] = "0.1"
    env["PYTHONPATH"] = str(Path(__file__).resolve().parents[1] / "src")

    proc = subprocess.run(
        [sys.executable, "-m", "csk", "install", "app"],
        text=True,
        capture_output=True,
        env=env,
        timeout=5,
        check=False,
    )

    assert proc.returncode == cli.EXIT_LOCK
    assert "another csk process holds lock" in proc.stderr


def _register_project(monkeypatch, csk_home, skills_root, project):
    cfg_path = csk_home / "config.json"
    cfg_path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "skills_root": str(skills_root),
                "projects": {"app": {"path": str(project), "agents": ["codex_cli"]}},
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("CSK_CONFIG", str(cfg_path))
    return cfg_path


def test_cli_install_explicit_target_fails_when_skipped(monkeypatch, tmp_path, csk_home, skills_root, capsys):
    project = make_project(tmp_path)  # no Skillfile
    _register_project(monkeypatch, csk_home, skills_root, project)

    code = cli.main(["install", "app"])

    captured = capsys.readouterr()
    assert code == 1
    assert "skipped; nothing installed" in captured.err


def test_cli_status_check_exit_codes(monkeypatch, tmp_path, csk_home, skills_root, capsys):
    project = make_project(tmp_path)
    make_skill_repo(skills_root, "skill-a", tag="v1")
    write_skillfile(project, {"schema_version": 1, "skills": [{"name": "skill-a", "tag": "v1"}]})
    _register_project(monkeypatch, csk_home, skills_root, project)

    assert cli.main(["status", "app", "--check"]) == 1  # not installed yet
    capsys.readouterr()
    assert cli.main(["install", "app"]) == 0
    capsys.readouterr()
    assert cli.main(["status", "app", "--check"]) == 0
    capsys.readouterr()


def test_cli_update_reports_missing_git_actionably(monkeypatch, tmp_path, csk_home, skills_root, capsys):
    from csk import git_ops

    project = make_project(tmp_path)
    make_skill_repo(skills_root, "skill-a", tag="v1")
    write_skillfile(project, {"schema_version": 1, "skills": []})
    _register_project(monkeypatch, csk_home, skills_root, project)

    def boom(cmd, **kwargs):
        raise FileNotFoundError("git")

    monkeypatch.setattr(git_ops.subprocess, "run", boom)

    code = cli.main(["update"])

    captured = capsys.readouterr()
    assert code == 1
    assert "install git" in captured.err


def test_cli_status_json_is_machine_readable(monkeypatch, tmp_path, csk_home, skills_root, capsys):
    project = make_project(tmp_path)
    make_skill_repo(skills_root, "skill-a", tag="v1")
    write_skillfile(project, {"schema_version": 1, "skills": [{"name": "skill-a", "tag": "v1"}]})
    _register_project(monkeypatch, csk_home, skills_root, project)
    assert cli.main(["install", "app"]) == 0
    capsys.readouterr()

    assert cli.main(["status", "app", "--json"]) == 0
    payload = json.loads(capsys.readouterr().out)

    assert payload[0]["alias"] == "app"
    assert payload[0]["clean"] is True
    assert payload[0]["skills"][0]["name"] == "skill-a"
    assert payload[0]["skills"][0]["label"] == "up-to-date"


def test_cli_add_and_remove_edit_project_skillfile(monkeypatch, tmp_path, csk_home, skills_root, capsys):
    project = make_project(tmp_path)
    write_skillfile(project, {"schema_version": 1, "skills": []})
    _register_project(monkeypatch, csk_home, skills_root, project)
    monkeypatch.chdir(project)

    assert cli.main(["add", "skill-a", "--tag", "v1", "--git", "git@example.com:skills/skill-a.git"]) == 0
    data = json.loads((project / "Skillfile.json").read_text(encoding="utf-8"))
    assert data["skills"] == [{"name": "skill-a", "tag": "v1", "git": "git@example.com:skills/skill-a.git"}]

    # Replaces an existing declaration instead of duplicating it.
    assert cli.main(["add", "skill-a", "--branch", "main"]) == 0
    data = json.loads((project / "Skillfile.json").read_text(encoding="utf-8"))
    assert data["skills"] == [{"name": "skill-a", "branch": "main"}]

    assert cli.main(["remove", "skill-a"]) == 0
    data = json.loads((project / "Skillfile.json").read_text(encoding="utf-8"))
    assert data["skills"] == []
    capsys.readouterr()

    # Removing an undeclared skill is a config error.
    assert cli.main(["remove", "skill-a"]) == 2
    assert "not declared" in capsys.readouterr().err


def test_cli_add_rejects_invalid_name_without_writing(monkeypatch, tmp_path, csk_home, skills_root, capsys):
    project = make_project(tmp_path)
    write_skillfile(project, {"schema_version": 1, "skills": []})
    _register_project(monkeypatch, csk_home, skills_root, project)
    monkeypatch.chdir(project)

    assert cli.main(["add", "../evil", "--tag", "v1"]) == 2
    data = json.loads((project / "Skillfile.json").read_text(encoding="utf-8"))
    assert data["skills"] == []


def test_cli_add_via_project_alias(monkeypatch, tmp_path, csk_home, skills_root, capsys):
    project = make_project(tmp_path)
    write_skillfile(project, {"schema_version": 1, "skills": []})
    _register_project(monkeypatch, csk_home, skills_root, project)
    monkeypatch.chdir(tmp_path)  # not inside the project

    assert cli.main(["add", "skill-a", "--tag", "v1", "--project", "app"]) == 0
    data = json.loads((project / "Skillfile.json").read_text(encoding="utf-8"))
    assert data["skills"][0]["name"] == "skill-a"


def test_cli_bootstrap_non_interactive(monkeypatch, tmp_path, capsys):
    cfg_path = tmp_path / "cfg" / "config.json"
    monkeypatch.setenv("CSK_CONFIG", str(cfg_path))

    assert cli.main([
        "bootstrap", "--non-interactive",
        "--skills-root", str(tmp_path / "skills"),
        "--default-agents", "codex_cli,claude_code",
    ]) == 0
    loaded = config.load_config(cfg_path)
    assert loaded.default_agents == ["codex_cli", "claude_code"]
    bootstrap_output = capsys.readouterr().out
    assert "Shell profile changes are not required" in bootstrap_output
    assert "csk shell-init --install" in bootstrap_output
    assert "shell-init bash" not in bootstrap_output

    # Existing config without --force is an error in non-interactive mode.
    assert cli.main(["bootstrap", "--non-interactive", "--skills-root", str(tmp_path / "skills")]) == 2
    assert "--force" in capsys.readouterr().err

    # Empty skills_root is rejected.
    monkeypatch.setenv("CSK_CONFIG", str(tmp_path / "cfg2" / "config.json"))
    assert cli.main(["bootstrap", "--non-interactive"]) == 2
    assert "skills_root" in capsys.readouterr().err


def test_cli_bootstrap_without_tty_requires_non_interactive(tmp_path):
    repo_root = Path(__file__).resolve().parents[1]
    config_path = tmp_path / "config.json"
    env = os.environ.copy()
    env["CSK_CONFIG"] = str(config_path)
    source_path = str(repo_root / "src")
    env["PYTHONPATH"] = os.pathsep.join(
        part for part in (source_path, env.get("PYTHONPATH")) if part
    )

    result = subprocess.run(
        [sys.executable, "-m", "csk", "bootstrap"],
        cwd=repo_root,
        env=env,
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == cli.EXIT_CONFIG, result.stderr
    assert result.stderr.startswith("error:")
    assert "--non-interactive" in result.stderr
    assert "Traceback" not in result.stderr
    assert not config_path.exists()


def test_cli_bootstrap_eof_on_tty_like_stdin_is_structured(monkeypatch, tmp_path, capsys):
    """A stdin that claims to be a TTY but is at end of input refuses cleanly.

    On Windows ``subprocess.DEVNULL`` is the ``NUL`` character device, so
    ``sys.stdin.isatty()`` is true and the TTY gate passes; the first prompt
    then reads end of input. Reproduce that shape on every platform.
    """

    class _TtyAtEof(io.StringIO):
        def isatty(self) -> bool:
            return True

    config_path = tmp_path / "config.json"
    monkeypatch.setenv("CSK_CONFIG", str(config_path))
    monkeypatch.setattr(sys, "stdin", _TtyAtEof(""))

    assert cli.main(["bootstrap"]) == cli.EXIT_CONFIG
    err = capsys.readouterr().err
    assert err.startswith("error:")
    assert "--non-interactive" in err
    assert "Traceback" not in err
    assert not config_path.exists()


def test_cli_bootstrap_if_missing_existing_config_without_tty(tmp_path):
    repo_root = Path(__file__).resolve().parents[1]
    config_path = tmp_path / "config.json"
    config_path.write_text("{}", encoding="utf-8")
    env = os.environ.copy()
    env["CSK_CONFIG"] = str(config_path)
    source_path = str(repo_root / "src")
    env["PYTHONPATH"] = os.pathsep.join(
        part for part in (source_path, env.get("PYTHONPATH")) if part
    )

    result = subprocess.run(
        [sys.executable, "-m", "csk", "bootstrap", "--if-missing"],
        cwd=repo_root,
        env=env,
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == cli.EXIT_OK, result.stderr
    assert result.stdout.startswith("Kept existing config:")
    assert result.stderr == ""


@pytest.mark.skipif(os.name != "posix", reason="closing fd 0 before exec is POSIX-only")
def test_cli_bootstrap_with_closed_stdin_requires_non_interactive(tmp_path):
    repo_root = Path(__file__).resolve().parents[1]
    config_path = tmp_path / "config.json"
    env = os.environ.copy()
    env["CSK_CONFIG"] = str(config_path)
    source_path = str(repo_root / "src")
    env["PYTHONPATH"] = os.pathsep.join(
        part for part in (source_path, env.get("PYTHONPATH")) if part
    )

    result = subprocess.run(
        [sys.executable, "-m", "csk", "bootstrap"],
        cwd=repo_root,
        env=env,
        preexec_fn=lambda: os.close(0),
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == cli.EXIT_CONFIG, result.stderr
    assert result.stderr.startswith("error:")
    assert "--non-interactive" in result.stderr
    assert "Traceback" not in result.stderr
    assert not config_path.exists()


def test_cli_bootstrap_still_prompts_when_stdin_is_tty(monkeypatch, tmp_path):
    config_path = tmp_path / "config.json"
    skills_root = tmp_path / "skills"
    monkeypatch.setenv("CSK_CONFIG", str(config_path))

    class TTYInput:
        def isatty(self):
            return True

    monkeypatch.setattr(cli.sys, "stdin", TTYInput())
    answers = iter([str(skills_root), "ru", "codex_cli", "n"])
    prompts = []

    def answer(prompt=""):
        prompts.append(prompt)
        return next(answers)

    monkeypatch.setattr("builtins.input", answer)

    assert cli.main(["bootstrap"]) == cli.EXIT_OK
    assert prompts == [
        "skills_root: ",
        "preferred_locale [none]: ",
        "default_agents comma-separated [codex_cli]: ",
        "Configure SSH credentials for private build repositories now? [y/N] ",
    ]
    loaded = config.load_config(config_path)
    assert loaded.skills_root == skills_root
    assert loaded.preferred_locale == "ru"
    assert loaded.default_agents == ["codex_cli"]


def test_cli_bootstrap_if_missing_keeps_existing_config(monkeypatch, tmp_path, capsys):
    cfg_path = tmp_path / "cfg" / "config.json"
    cfg_path.parent.mkdir(parents=True)
    cfg_path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "skills_root": str(tmp_path / "existing-skills"),
                "preferred_locale": "ru",
                "default_agents": ["codex_cli"],
                "projects": {},
            },
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    original = cfg_path.read_bytes()
    monkeypatch.setenv("CSK_CONFIG", str(cfg_path))

    assert cli.main(["bootstrap", "--if-missing", "--non-interactive"]) == 0

    assert cfg_path.read_bytes() == original
    assert f"Kept existing config: {cfg_path}" in capsys.readouterr().out


def test_cli_bootstrap_if_missing_creates_absent_config(monkeypatch, tmp_path):
    cfg_path = tmp_path / "cfg" / "config.json"
    skills_root = tmp_path / "skills"
    monkeypatch.setenv("CSK_CONFIG", str(cfg_path))

    assert cli.main(
        [
            "bootstrap",
            "--if-missing",
            "--non-interactive",
            "--skills-root",
            str(skills_root),
        ]
    ) == 0

    assert config.load_config(cfg_path).skills_root == skills_root


def test_cli_bootstrap_if_missing_and_force_are_mutually_exclusive(capsys):
    assert cli.main(["bootstrap", "--if-missing", "--force"]) == 2
    assert "not allowed with argument" in capsys.readouterr().err


def test_cli_install_dry_run_does_not_create_skills_root(monkeypatch, tmp_path, csk_home, capsys):
    project = make_project(tmp_path)
    write_skillfile(project, {"schema_version": 1, "skills": []})
    missing_root = tmp_path / "missing-skills-root"
    _register_project(monkeypatch, csk_home, missing_root, project)

    class ForbiddenLock:
        def __init__(self, _home):
            raise AssertionError("project dry-run must not construct a mutation lock")

    monkeypatch.setattr(cli, "GlobalLock", ForbiddenLock)
    assert cli.main(["install", "app", "--dry-run"]) == 0
    assert not missing_root.exists()


def test_cli_upgrade_dry_run_does_not_create_or_fetch_skills_root(
    monkeypatch, tmp_path, csk_home, capsys
):
    project = make_project(tmp_path)
    write_skillfile(project, {"schema_version": 1, "skills": []})
    missing_root = tmp_path / "missing-skills-root"
    _register_project(monkeypatch, csk_home, missing_root, project)

    def unexpected_fetch(_repo):
        raise AssertionError("dry-run must not fetch")

    class ForbiddenLock:
        def __init__(self, _home):
            raise AssertionError("project dry-run must not construct a mutation lock")

    monkeypatch.setattr(cli, "GlobalLock", ForbiddenLock)
    monkeypatch.setattr(cli.git_ops, "fetch_repo", unexpected_fetch)

    assert cli.main(["upgrade", "app", "--dry-run"]) == 0
    assert not missing_root.exists()
    assert "dry-run; no files modified" in capsys.readouterr().out


def test_cli_upgrade_fetches_only_selected_project_skill_repositories(
    monkeypatch, tmp_path, csk_home, skills_root
):
    project = make_project(tmp_path)
    selected, _ = make_skill_repo(skills_root, "skill-selected", tag="v1")
    make_skill_repo(skills_root, "skill-unrelated", tag="v1")
    write_skillfile(
        project,
        {"schema_version": 1, "skills": [{"name": "skill-selected", "tag": "v1"}]},
    )
    _register_project(monkeypatch, csk_home, skills_root, project)
    fetched: list[Path] = []
    monkeypatch.setattr(cli.git_ops, "fetch_repo", fetched.append)

    assert cli.main(["upgrade", "app"]) == 0

    assert fetched == [selected]


def test_cli_status_error_label_includes_reason(monkeypatch, tmp_path, csk_home, skills_root, capsys):
    project = make_project(tmp_path)
    write_skillfile(project, {"schema_version": 1, "skills": [{"name": "skill-gone", "tag": "v1"}]})
    _register_project(monkeypatch, csk_home, skills_root, project)

    assert cli.main(["status", "app"]) == 0
    out = capsys.readouterr().out
    assert "error" in out
    assert "Not a git repository" in out

    assert cli.main(["status", "app", "--json"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload[0]["skills"][0]["label"] == "error"
    assert "Not a git repository" in payload[0]["skills"][0]["detail"]


def test_unknown_agent_names_warn(monkeypatch, tmp_path, csk_home, skills_root, capsys):
    project = make_project(tmp_path)
    write_skillfile(project, {"schema_version": 1, "agents": ["codex", "codex_cli"], "skills": []})
    _register_project(monkeypatch, csk_home, skills_root, project)

    assert cli.main(["install", "app"]) == 0
    err = capsys.readouterr().err
    assert "unknown agent(s) ignored: codex" in err
    assert "codex_cli" in err  # known list mentioned


# --- Skillfile schema 2 CLI surface (TASK-260916-1lv2ky) ---
#
# Conventions used below: ``check``/``install``/``upgrade``/``status`` are
# driven through the production entry point ``csk.cli.main`` on fixtures;
# every rendered diagnostic is asserted as user-visible text (code line and
# exactly one ``remediation:`` line) with the test's
# secrets absent. Install-path tests need descriptor-relative traversal
# and skip with the repository's named POSIX reason elsewhere, exactly
# like the sibling schema-2 suites; ``check``-path tests run everywhere
# because validation is pure after the declared inputs are read.

_DRAFT_LABEL = "draft skillfile-sources-v1 (opt-in)"


def _require_posix_traversal():
    if not _selection_fs.supports_descriptor_traversal():
        pytest.skip(_selection_fs.NO_DESCRIPTOR_TRAVERSAL_REASON)


def _register_draft_project(
    monkeypatch, csk_home, skills_root, project, alias="app", *, experimental=None
):
    payload = {
        "schema_version": 1,
        "skills_root": str(skills_root),
        "default_agents": ["codex_cli"],
        "projects": {alias: {"path": str(project)}},
    }
    if experimental is not None:
        payload["experimental"] = {"skillfile_sources": experimental}
    cfg_path = csk_home / "config.json"
    cfg_path.write_text(json.dumps(payload), encoding="utf-8")
    monkeypatch.setenv("CSK_CONFIG", str(cfg_path))
    monkeypatch.delenv("CSK_EXPERIMENTAL_SKILLFILE_SOURCES", raising=False)
    monkeypatch.delenv("CSK_SOURCE_POLICY", raising=False)
    return cfg_path


def _write_draft_skill(directory, name, *, bad=None, agent_manifest=None):
    directory.mkdir(parents=True, exist_ok=True)
    if bad == "noname":
        text = "---\ndescription: no name here\n---\n\n# x\n"
    else:
        text = f"---\nname: {name}\ndescription: fixture {name}\n---\n\n# {name}\n"
    (directory / "SKILL.md").write_text(text, encoding="utf-8")
    scripts = directory / "scripts"
    scripts.mkdir(exist_ok=True)
    (scripts / "tool.sh").write_text("#!/bin/sh\necho hi\n", encoding="utf-8")
    payload = (
        agent_manifest
        if agent_manifest is not None
        else {"schema_version": 2, "runtime_roots": ["scripts"]}
    )
    (directory / "agent-skill.json").write_text(json.dumps(payload), encoding="utf-8")


def _remediation_lines(text):
    return [line for line in text.splitlines() if line.startswith("remediation: ")]


def _enable_env_opt_in(monkeypatch):
    monkeypatch.setenv("CSK_EXPERIMENTAL_SKILLFILE_SOURCES", "1")


def test_cli_check_accepts_schema1_without_legacy_setting(monkeypatch, tmp_path, csk_home, skills_root, capsys):
    """The always-available check command accepts a legacy project Skillfile."""

    project = make_project(tmp_path)
    write_skillfile(project, {"schema_version": 1, "skills": []})
    _register_project(monkeypatch, csk_home, skills_root, project)
    monkeypatch.delenv("CSK_EXPERIMENTAL_SKILLFILE_SOURCES", raising=False)

    assert cli.main(["check", "app"]) == 0
    captured = capsys.readouterr()
    assert "schema_version 1 valid (0 skills)" in captured.out
    assert captured.err == ""
    assert cli.main(["--help"]) == 0
    assert "csk check" not in capsys.readouterr().out


def test_cli_check_help_without_legacy_setting(monkeypatch, tmp_path, csk_home, skills_root, capsys):
    project = make_project(tmp_path)
    _register_project(monkeypatch, csk_home, skills_root, project)
    monkeypatch.delenv("CSK_EXPERIMENTAL_SKILLFILE_SOURCES", raising=False)

    assert cli.main(["check", "--help"]) == 0
    assert "usage: csk check" in capsys.readouterr().out


def test_cli_version_omits_removed_label_without_legacy_env(monkeypatch, capsys):
    monkeypatch.delenv("CSK_EXPERIMENTAL_SKILLFILE_SOURCES", raising=False)
    assert cli.main(["--version"]) == 0
    out = capsys.readouterr().out
    assert out.startswith("csk ")
    assert _DRAFT_LABEL not in out


def test_cli_version_omits_removed_label_with_legacy_env(monkeypatch, capsys):
    _enable_env_opt_in(monkeypatch)
    assert cli.main(["--version"]) == 0
    lines = capsys.readouterr().out.splitlines()
    assert len(lines) == 1
    assert _DRAFT_LABEL not in lines[0]


def test_cli_help_omits_removed_label_with_legacy_env(monkeypatch, capsys):
    _enable_env_opt_in(monkeypatch)
    assert cli.main(["--help"]) == 0
    top = capsys.readouterr().out
    assert _DRAFT_LABEL not in top
    assert cli.main(["install", "--help"]) == 0
    install_help = capsys.readouterr().out
    assert _DRAFT_LABEL not in install_help
    assert cli.main(["upgrade", "--help"]) == 0
    upgrade_help = capsys.readouterr().out
    assert _DRAFT_LABEL not in upgrade_help
    assert cli.main(["status", "--help"]) == 0
    status_help = capsys.readouterr().out
    assert _DRAFT_LABEL not in status_help
    assert cli.main(["check", "--help"]) == 0
    check_help = capsys.readouterr().out
    assert _DRAFT_LABEL not in check_help
    assert "Validate a schema-2 Skillfile" in check_help


def test_cli_help_and_check_omit_removed_label_without_legacy_env(monkeypatch, capsys):
    monkeypatch.delenv("CSK_EXPERIMENTAL_SKILLFILE_SOURCES", raising=False)
    for argv in (["--help"], ["install", "--help"], ["upgrade", "--help"], ["status", "--help"]):
        assert cli.main(argv) == 0
        assert _DRAFT_LABEL not in capsys.readouterr().out
    assert cli.main(["check", "--help"]) == 0
    assert "usage: csk check" in capsys.readouterr().out


def test_cli_legacy_env_values_do_not_gate_schema_support(monkeypatch, tmp_path, csk_home, skills_root, capsys):
    """Legacy environment values do not change schema support."""

    project = make_project(tmp_path)
    write_skillfile(project, {"schema_version": 1, "skills": []})
    _register_project(monkeypatch, csk_home, skills_root, project)
    for value in ("2", "true", "yes", ""):
        monkeypatch.setenv("CSK_EXPERIMENTAL_SKILLFILE_SOURCES", value)
        assert cli.main(["check", "app"]) == 0, value
        captured = capsys.readouterr()
        assert "schema_version 1 valid" in captured.out, value
        assert captured.err == "", value
        assert cli.main(["--version"]) == 0
        assert _DRAFT_LABEL not in capsys.readouterr().out, value


def test_cli_legacy_config_key_does_not_gate_schema2(monkeypatch, tmp_path, csk_home, skills_root, capsys):
    """The legacy config key remains accepted but does not gate schema 2."""

    project = make_project(tmp_path)
    write_skillfile(
        project,
        {
            "schema_version": 2,
            "sources": {"local": {"path": "."}},
            "skills": [{"name": "review", "from": "local", "directory": "agents/skills/review"}],
        },
    )
    _register_draft_project(monkeypatch, csk_home, skills_root, project, experimental=True)

    assert cli.main(["check", "app"]) == 0
    out = capsys.readouterr().out
    assert _DRAFT_LABEL not in out
    assert "schema_version 2 valid" in out


def _walk_help_argvs(parser):
    """Return ``[...path, "--help"]`` for every subcommand path in the parser.

    Structural, not textual: the walk visits the live subparser objects,
    so a verb added later appears here without touching any test. Choices
    are deduplicated by object identity so aliases never double-count.
    """

    found = []

    def visit(node, prefix):
        for action in node._actions:
            if not isinstance(action, argparse._SubParsersAction):
                continue
            seen = set()
            for name, sub in action.choices.items():
                if id(sub) in seen:
                    continue
                seen.add(id(sub))
                path = (*prefix, name)
                found.append((*path, "--help"))
                visit(sub, path)

    visit(parser, ())
    return found


def _parser_surface_matrix():
    """Derive root help surfaces from the root parser alone."""

    parser = cli.build_parser()
    return (("--version",), ("--help",), *_walk_help_argvs(parser))


_UNKNOWN_DISPLAY_SURFACES = _parser_surface_matrix()


_UNKNOWN_STATE_PARAMS = [
    "malformed-json",
    "empty-file",
    "not-an-object",
    "invalid-utf8",
    "unknown-field",
    "experimental-not-bool",
    "optin-true-plus-error",
    "optin-false-plus-error",
    "is-directory",
    pytest.param(
        "unreadable",
        marks=[
            pytest.mark.skipif(os.name == "nt", reason="POSIX permission bits required"),
            pytest.mark.skipif(
                hasattr(os, "geteuid") and os.geteuid() == 0,
                reason="root bypasses permission bits",
            ),
        ],
    ),
    "enotdir-parent",
    "system-config-malformed",
]


def _break_registered_config(monkeypatch, tmp_path, project, kind, cfg_path):
    """Rewrite the registered config into one unloadable shape.

    The twelve shapes are the probe states the decision must classify
    unknown: every malformed/unreadable form renders the v1 parser and,
    where a command runs, refuses naming the config.
    """

    if kind == "malformed-json":
        cfg_path.write_text("{malformed", encoding="utf-8")
    elif kind == "empty-file":
        cfg_path.write_text("", encoding="utf-8")
    elif kind == "not-an-object":
        cfg_path.write_text("[1,2]", encoding="utf-8")
    elif kind == "invalid-utf8":
        cfg_path.write_bytes(b'{"schema_version": 1, "x": "\xff"}')
    elif kind == "unknown-field":
        payload = json.loads(cfg_path.read_text(encoding="utf-8"))
        payload["no_such_top_level_key"] = True
        cfg_path.write_text(json.dumps(payload), encoding="utf-8")
    elif kind == "experimental-not-bool":
        payload = json.loads(cfg_path.read_text(encoding="utf-8"))
        payload["experimental"] = {"skillfile_sources": "yes"}
        cfg_path.write_text(json.dumps(payload), encoding="utf-8")
    elif kind == "optin-true-plus-error":
        payload = json.loads(cfg_path.read_text(encoding="utf-8"))
        payload["experimental"] = {"skillfile_sources": True}
        payload["no_such_top_level_key"] = True
        cfg_path.write_text(json.dumps(payload), encoding="utf-8")
    elif kind == "optin-false-plus-error":
        payload = json.loads(cfg_path.read_text(encoding="utf-8"))
        payload["experimental"] = {"skillfile_sources": False}
        payload["no_such_top_level_key"] = True
        cfg_path.write_text(json.dumps(payload), encoding="utf-8")
    elif kind == "is-directory":
        cfg_path.unlink()
        cfg_path.mkdir()
    elif kind == "unreadable":
        cfg_path.chmod(0o000)
    elif kind == "enotdir-parent":
        monkeypatch.setenv("CSK_CONFIG", str(project / "Skillfile.json" / "config.json"))
    elif kind == "system-config-malformed":
        system_path = tmp_path / "system.json"
        system_path.write_text("{ nope", encoding="utf-8")
        monkeypatch.setenv("CSK_SYSTEM_CONFIG", str(system_path))
    else:  # pragma: no cover - parametrize lists every kind
        raise AssertionError(f"unknown config-break kind: {kind}")


@pytest.mark.parametrize("kind", _UNKNOWN_STATE_PARAMS)
@pytest.mark.parametrize(
    "argv",
    _UNKNOWN_DISPLAY_SURFACES,
    ids=[" ".join(item) for item in _UNKNOWN_DISPLAY_SURFACES],
)
def test_cli_root_parser_display_is_config_independent(
    monkeypatch, tmp_path, csk_home, skills_root, capsys, kind, argv
):
    """Every root help surface stays independent of config readability."""

    project = make_project(tmp_path)
    write_skillfile(project, {"schema_version": 1, "skills": []})
    cfg_path = _register_draft_project(monkeypatch, csk_home, skills_root, project)
    monkeypatch.delenv("CSK_SYSTEM_CONFIG", raising=False)
    _break_registered_config(monkeypatch, tmp_path, project, kind, cfg_path)

    broken_code = cli.main(list(argv))
    broken = capsys.readouterr()
    assert _DRAFT_LABEL not in broken.out + broken.err, (kind, argv)

    monkeypatch.setenv("CSK_CONFIG", str(tmp_path / "missing-config.json"))
    monkeypatch.delenv("CSK_SYSTEM_CONFIG", raising=False)
    v1_code = cli.main(list(argv))
    v1 = capsys.readouterr()

    assert (broken_code, broken.out, broken.err) == (v1_code, v1.out, v1.err), (kind, argv)


def test_cli_root_help_surface_enumeration_covers_every_root_verb():
    """Root parser help covers every registered root command."""

    walked = {("--version",), ("--help",), *_walk_help_argvs(cli.build_parser())}
    matrix = set(_UNKNOWN_DISPLAY_SURFACES)
    assert walked <= matrix
    for pinned in (
        ("--version",),
        ("--help",),
        ("install", "--help"),
        ("skill", "check", "--help"),
        ("global", "add", "--help"),
        ("config", "build-https", "login", "--help"),
    ):
        assert pinned in matrix, pinned
    assert ("check", "--help") not in matrix
    assert len(matrix) > 20, "a broken walk must not pass an empty matrix"


@pytest.mark.parametrize("kind", _UNKNOWN_STATE_PARAMS)
def test_cli_commands_parse_before_reporting_unloadable_config(
    monkeypatch, tmp_path, csk_home, skills_root, capsys, kind
):
    """Root commands and standalone check report real config read failures."""

    project = make_project(tmp_path)
    write_skillfile(project, {"schema_version": 1, "skills": []})
    cfg_path = _register_draft_project(monkeypatch, csk_home, skills_root, project)
    monkeypatch.delenv("CSK_SYSTEM_CONFIG", raising=False)
    _break_registered_config(monkeypatch, tmp_path, project, kind, cfg_path)

    trunk_lane = {
        "is-directory": IsADirectoryError,
        "unreadable": PermissionError,
        "enotdir-parent": NotADirectoryError,
    }
    # Windows lane (named platform bound): CPython's _wopen reports a
    # directory as EACCES (PermissionError, not IsADirectoryError) and a
    # file-as-parent as ENOENT (FileNotFoundError, which load_config
    # turns into ConfigError and exit 2, not NotADirectoryError).
    if os.name == "nt" and kind == "is-directory":
        with pytest.raises(PermissionError) as excinfo:
            cli.main(["status", "app"])
        assert "config.json" in str(excinfo.value), kind
    elif os.name == "nt" and kind == "enotdir-parent":
        assert cli.main(["status", "app"]) == 2, kind
        err = capsys.readouterr().err
        assert "config" in err.lower(), (kind, err)
        assert "invalid choice" not in err, (kind, err)
    elif kind in trunk_lane:
        with pytest.raises(trunk_lane[kind]) as excinfo:
            cli.main(["status", "app"])
        assert "config.json" in str(excinfo.value), kind
    else:
        assert cli.main(["status", "app"]) == 2, kind
        err = capsys.readouterr().err
        assert "config" in err.lower(), (kind, err)
        assert "invalid choice" not in err, (kind, err)

    monkeypatch.setenv("CSK_CONFIG", str(tmp_path / "missing-check-config.json"))
    assert cli.main(["check", "app"]) == 2, kind
    check_error = capsys.readouterr().err
    assert "config" in check_error.lower(), (kind, check_error)
    assert "invalid choice" not in check_error, (kind, check_error)


def test_cli_check_exists_without_config_and_root_help_stays_stable(monkeypatch, tmp_path, capsys):
    """Check is available without config while root help stays v1-compatible."""

    monkeypatch.setenv("CSK_CONFIG", str(tmp_path / "missing-config.json"))
    monkeypatch.delenv("CSK_EXPERIMENTAL_SKILLFILE_SOURCES", raising=False)

    assert cli.main(["check", "app"]) == 2
    check_error = capsys.readouterr().err
    assert "config" in check_error.lower()
    assert "invalid choice" not in check_error
    assert cli.main(["--help"]) == 0
    assert "csk check" not in capsys.readouterr().out
    assert cli.main(["--version"]) == 0
    assert _DRAFT_LABEL not in capsys.readouterr().out


_LOADABLE_LOCK_CONFLICT_VALUES = {
    "disable_builtin_registries": (False, True),
    "allowed_sources": (["user.example.com"], ["system.example.com"]),
    "audit": ({"enabled": False}, {"enabled": True}),
    "audit_registries": (
        [{"name": "userreg", "url": "https://user.example.com"}],
        [{"name": "sysreg", "url": "https://system.example.com"}],
    ),
}

_LOADABLE_STATE_PARAMS = [
    "present-no-experimental",
    "optin-false",
    "system-no-conflict",
    pytest.param(
        "symlinked-config",
        marks=pytest.mark.skipif(
            os.name == "nt",
            reason="symlink creation requires privileges on Windows",
        ),
    ),
    *(f"system-locked-conflict-{key}" for key in sorted(config.LOCKABLE_KEYS)),
]

_LOADABLE_KINDS = {
    "present-no-experimental",
    "optin-false",
    "system-no-conflict",
    "symlinked-config",
    *(f"system-locked-conflict-{key}" for key in config.LOCKABLE_KEYS),
}


def _setup_loadable_config(monkeypatch, tmp_path, csk_home, skills_root, project, kind):
    """Write a loadable non-opted-in config state; return the user config path.

    Every state loads successfully and carries no opt-in, so the
    decision is ``disabled`` and the parser is the v1 parser. The
    ``system-locked-conflict-<key>`` family covers every member of
    ``config.LOCKABLE_KEYS`` by construction (see
    ``test_cli_loadable_matrix_covers_every_lockable_key``).
    """

    payload = {
        "schema_version": 1,
        "skills_root": str(skills_root),
        "default_agents": ["codex_cli"],
        "projects": {"app": {"path": str(project)}},
    }
    monkeypatch.delenv("CSK_SYSTEM_CONFIG", raising=False)
    monkeypatch.delenv("CSK_EXPERIMENTAL_SKILLFILE_SOURCES", raising=False)
    monkeypatch.delenv("CSK_SOURCE_POLICY", raising=False)
    cfg_path = csk_home / "config.json"
    if kind == "present-no-experimental":
        cfg_path.write_text(json.dumps(payload), encoding="utf-8")
        monkeypatch.setenv("CSK_CONFIG", str(cfg_path))
        return cfg_path
    if kind == "optin-false":
        payload["experimental"] = {"skillfile_sources": False}
        cfg_path.write_text(json.dumps(payload), encoding="utf-8")
        monkeypatch.setenv("CSK_CONFIG", str(cfg_path))
        return cfg_path
    if kind == "system-no-conflict":
        payload["disable_builtin_registries"] = True
        cfg_path.write_text(json.dumps(payload), encoding="utf-8")
        monkeypatch.setenv("CSK_CONFIG", str(cfg_path))
        system_path = tmp_path / "system.json"
        system_path.write_text(
            json.dumps(
                {
                    "schema_version": 1,
                    "locked": ["disable_builtin_registries"],
                    "disable_builtin_registries": True,
                }
            ),
            encoding="utf-8",
        )
        monkeypatch.setenv("CSK_SYSTEM_CONFIG", str(system_path))
        return cfg_path
    if kind == "symlinked-config":
        cfg_path.write_text(json.dumps(payload), encoding="utf-8")
        link = tmp_path / "linked-config.json"
        if link.exists() or link.is_symlink():
            link.unlink()
        link.symlink_to(cfg_path)
        monkeypatch.setenv("CSK_CONFIG", str(link))
        return cfg_path
    prefix = "system-locked-conflict-"
    if kind.startswith(prefix):
        key = kind[len(prefix):]
        user_value, system_value = _LOADABLE_LOCK_CONFLICT_VALUES[key]
        payload[key] = user_value
        cfg_path.write_text(json.dumps(payload), encoding="utf-8")
        monkeypatch.setenv("CSK_CONFIG", str(cfg_path))
        system_path = tmp_path / "system.json"
        system_path.write_text(
            json.dumps({"schema_version": 1, "locked": [key], key: system_value}),
            encoding="utf-8",
        )
        monkeypatch.setenv("CSK_SYSTEM_CONFIG", str(system_path))
        return cfg_path
    raise AssertionError(f"unknown loadable kind: {kind}")  # pragma: no cover


def test_cli_loadable_matrix_covers_every_lockable_key():
    """The conflict family is derived from config.LOCKABLE_KEYS, not listed.

    A lockable key added later enters the matrix by construction; a
    hand-written list would silently miss it.
    """

    prefix = "system-locked-conflict-"
    covered = {
        item[len(prefix):]
        for item in _LOADABLE_STATE_PARAMS
        if isinstance(item, str) and item.startswith(prefix)
    }
    assert covered == set(config.LOCKABLE_KEYS)
    for key in config.LOCKABLE_KEYS:
        assert key in _LOADABLE_LOCK_CONFLICT_VALUES, key


@pytest.mark.parametrize("kind", _LOADABLE_STATE_PARAMS)
@pytest.mark.parametrize(
    "argv",
    _UNKNOWN_DISPLAY_SURFACES,
    ids=[" ".join(item) for item in _UNKNOWN_DISPLAY_SURFACES],
)
def test_cli_loadable_config_display_is_v1_bytes(
    monkeypatch, tmp_path, csk_home, skills_root, capsys, kind, argv
):
    """Root display surfaces do not depend on loaded config state."""

    project = make_project(tmp_path)
    write_skillfile(project, {"schema_version": 1, "skills": []})
    _setup_loadable_config(monkeypatch, tmp_path, csk_home, skills_root, project, kind)
    capsys.readouterr()

    loaded_code = cli.main(list(argv))
    loaded = capsys.readouterr()
    assert _DRAFT_LABEL not in loaded.out + loaded.err, (kind, argv)

    monkeypatch.setenv("CSK_CONFIG", str(tmp_path / "missing-config.json"))
    monkeypatch.delenv("CSK_SYSTEM_CONFIG", raising=False)
    v1_code = cli.main(list(argv))
    v1 = capsys.readouterr()

    assert (loaded_code, loaded.out, loaded.err) == (v1_code, v1.out, v1.err), (kind, argv)


@pytest.mark.parametrize("key", sorted(config.LOCKABLE_KEYS))
def test_cli_locked_conflict_warning_exactly_once_on_runnable(
    monkeypatch, tmp_path, csk_home, skills_root, capsys, key
):
    """A locked-key conflict warns once at dispatch, never at parse time.

    Revision 5 class test over every lockable key: runnable verbs
    (``status`` and ``list``) emit the trunk warning exactly once,
    naming the key; display surfaces (``--help``, ``--version``,
    ``config show``, ``install --help``) emit nothing. The narrowing
    mutant that lets one key's warning through at parse time fails
    the zero-warning half for that key.
    """

    project = make_project(tmp_path)
    write_skillfile(project, {"schema_version": 1, "skills": []})
    _setup_loadable_config(
        monkeypatch, tmp_path, csk_home, skills_root, project, f"system-locked-conflict-{key}"
    )
    capsys.readouterr()

    for argv in (["status", "app"], ["list"]):
        code = cli.main(argv)
        captured = capsys.readouterr()
        assert code == 0, (key, argv, captured.err)
        assert captured.out != "", (key, argv)
        assert captured.err.count("is locked by") == 1, (key, argv, captured.err)
        assert f"config key {key!r} is locked by" in captured.err, (key, argv, captured.err)

    for argv in (["--help"], ["--version"], ["config", "show"], ["install", "--help"]):
        code = cli.main(argv)
        captured = capsys.readouterr()
        assert code == 0, (key, argv)
        assert captured.err == "", (key, argv, captured.err)


def test_cli_help_skips_config_and_dispatch_warns_once(
    monkeypatch, tmp_path, csk_home, skills_root, capsys
):
    """Help and version skip config loading; runnable commands load once."""

    project = make_project(tmp_path)
    write_skillfile(project, {"schema_version": 1, "skills": []})
    _setup_loadable_config(
        monkeypatch,
        tmp_path,
        csk_home,
        skills_root,
        project,
        "system-locked-conflict-disable_builtin_registries",
    )

    seen: list[bool] = []
    original = config.load_config

    def counting(path=None, *, quiet=False):
        seen.append(quiet)
        return original(path, quiet=quiet)

    monkeypatch.setattr(config, "load_config", counting)

    cli.main(["--version"])
    captured = capsys.readouterr()
    assert seen == [], seen
    assert captured.err == ""

    del seen[:]
    cli.main(["--help"])
    captured = capsys.readouterr()
    assert seen == [], seen
    assert captured.err == ""

    del seen[:]
    assert cli.main(["status", "app"]) == 0
    captured = capsys.readouterr()
    assert seen == [False], seen
    assert captured.err.count("is locked by") == 1, captured.err


def test_cli_check_v1_project_valid(monkeypatch, tmp_path, csk_home, skills_root, capsys):
    project = make_project(tmp_path)
    write_skillfile(project, {"schema_version": 1, "skills": []})
    _register_draft_project(monkeypatch, csk_home, skills_root, project)
    _enable_env_opt_in(monkeypatch)

    assert cli.main(["check", "app"]) == 0
    lines = capsys.readouterr().out.splitlines()
    assert lines == ["app: schema_version 1 valid (0 skills)"]


def test_cli_check_schema2_valid_reports_summary(monkeypatch, tmp_path, csk_home, skills_root, capsys):
    project = make_project(tmp_path)
    write_skillfile(
        project,
        {
            "schema_version": 2,
            "sources": {"local": {"path": "."}},
            "skills": [{"name": "review", "from": "local", "directory": "agents/skills/review"}],
        },
    )
    _register_draft_project(monkeypatch, csk_home, skills_root, project)
    _enable_env_opt_in(monkeypatch)

    assert cli.main(["check", "app"]) == 0
    out = capsys.readouterr().out
    assert "(1 sources, 1 selectors, lock absent, policy absent)" in out


def test_cli_check_missing_skillfile_is_invalid(monkeypatch, tmp_path, csk_home, skills_root, capsys):
    project = make_project(tmp_path)  # no Skillfile
    _register_draft_project(monkeypatch, csk_home, skills_root, project)
    _enable_env_opt_in(monkeypatch)

    assert cli.main(["check", "app"]) == 1
    out = capsys.readouterr().out
    assert _DRAFT_LABEL not in out
    assert "Skillfile.json missing" in out


@pytest.mark.skipif(os.name == "nt", reason="POSIX permission bits required")
@pytest.mark.skipif(
    hasattr(os, "geteuid") and os.geteuid() == 0, reason="root bypasses permission bits"
)
def test_cli_check_unreadable_skillfile_is_structured_refusal(
    monkeypatch, tmp_path, csk_home, skills_root, capsys
):
    """An unreadable Skillfile refuses through check; no raw exception escapes."""

    project = make_project(tmp_path)
    write_skillfile(
        project,
        {
            "schema_version": 2,
            "sources": {"local": {"path": "."}},
            "skills": [{"name": "review", "from": "local", "directory": "agents/skills/review"}],
        },
    )
    _register_draft_project(monkeypatch, csk_home, skills_root, project)
    _enable_env_opt_in(monkeypatch)
    (project / "Skillfile.json").chmod(0o000)

    assert cli.main(["check", "app"]) == 1
    captured = capsys.readouterr()
    assert captured.err.startswith("error: "), captured.err
    assert "Skillfile.json" in captured.err, captured.err
    assert _DRAFT_LABEL not in captured.err, captured.err


def test_cli_check_directory_skillfile_is_structured_refusal(
    monkeypatch, tmp_path, csk_home, skills_root, capsys
):
    """A directory-shaped Skillfile refuses through check; no raw exception escapes."""

    project = make_project(tmp_path)
    write_skillfile(
        project,
        {
            "schema_version": 2,
            "sources": {"local": {"path": "."}},
            "skills": [{"name": "review", "from": "local", "directory": "agents/skills/review"}],
        },
    )
    _register_draft_project(monkeypatch, csk_home, skills_root, project)
    _enable_env_opt_in(monkeypatch)
    (project / "Skillfile.json").unlink()
    (project / "Skillfile.json").mkdir()

    assert cli.main(["check", "app"]) == 1
    captured = capsys.readouterr()
    assert captured.err.startswith("error: "), captured.err
    assert "Skillfile.json" in captured.err, captured.err
    assert _DRAFT_LABEL not in captured.err, captured.err


def test_cli_check_unknown_alias_is_usage_error(monkeypatch, tmp_path, csk_home, skills_root, capsys):
    project = make_project(tmp_path)
    _register_draft_project(monkeypatch, csk_home, skills_root, project)
    _enable_env_opt_in(monkeypatch)

    assert cli.main(["check", "ghost"]) == 2
    assert "Unknown project alias: ghost" in capsys.readouterr().err


def test_cli_check_all_covers_mixed_projects(monkeypatch, tmp_path, csk_home, skills_root, capsys):
    first = make_project(tmp_path, "first")
    write_skillfile(first, {"schema_version": 1, "skills": []})
    second = make_project(tmp_path, "second")
    write_skillfile(
        second,
        {
            "schema_version": 2,
            "sources": {"local": {"path": "."}},
            "skills": [{"name": "review", "from": "local", "directory": "agents/skills/review"}],
        },
    )
    cfg_path = csk_home / "config.json"
    cfg_path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "skills_root": str(skills_root),
                "default_agents": ["codex_cli"],
                "projects": {"one": {"path": str(first)}, "two": {"path": str(second)}},
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("CSK_CONFIG", str(cfg_path))
    _enable_env_opt_in(monkeypatch)

    assert cli.main(["check", "--all"]) == 0
    out = capsys.readouterr().out
    assert "one: schema_version 1 valid" in out
    assert "two: schema_version 2 valid" in out


def _assert_diagnostic(err, *, code, subject, reason):
    assert f"{code}:" in err, err
    assert subject in err, err
    assert reason in err, err
    assert len(_remediation_lines(err)) == 1, err
    assert _DRAFT_LABEL not in err, err


def test_cli_diagnostic_source_alias_unknown(monkeypatch, tmp_path, csk_home, skills_root, capsys):
    project = make_project(tmp_path)
    write_skillfile(
        project,
        {
            "schema_version": 2,
            "sources": {"local": {"path": "."}},
            "skills": [{"name": "x", "from": "ghost", "directory": "."}],
        },
    )
    _register_draft_project(monkeypatch, csk_home, skills_root, project)
    _enable_env_opt_in(monkeypatch)

    assert cli.main(["check", "app"]) == 1
    _assert_diagnostic(
        capsys.readouterr().err,
        code="source_alias_unknown",
        subject="'ghost'",
        reason="unknown source",
    )


def test_cli_diagnostic_source_selection_invalid(monkeypatch, tmp_path, csk_home, skills_root, capsys):
    """The refusal names alias, field and shape; the declaration is gone.

    Before (revision 2)::

        source_selection_invalid: git declaration
        'https://operator:s3cr3t-op-password@git.example.com/team/kit.git'
        must not contain userinfo, a password, or a port

    rendered through a display-time sanitizer as
    ``https://***@git.example.com/team/kit.git``. After (revision 3)
    the parser refuses without echoing, so there is nothing to
    redact: no credential, no URL, no host, and no ``***`` residue.
    """

    password = "s3cr3t-op-password"
    url = f"https://operator:{password}@git.example.com/team/kit.git"
    project = make_project(tmp_path)
    write_skillfile(
        project,
        {
            "schema_version": 2,
            "sources": {"evil": {"git": url, "tag": "v1.0.0"}},
            "skills": [{"name": "x", "from": "evil", "directory": "."}],
        },
    )
    _register_draft_project(monkeypatch, csk_home, skills_root, project)
    _enable_env_opt_in(monkeypatch)

    assert cli.main(["check", "app"]) == 1
    captured = capsys.readouterr()
    _assert_diagnostic(
        captured.err,
        code="source_selection_invalid",
        subject="'evil'",
        reason="userinfo",
    )
    assert "field 'git'" in captured.err, captured.err
    assert password not in captured.err
    assert url not in captured.err
    assert "git.example.com" not in captured.err
    assert "***" not in captured.err


def test_cli_diagnostic_source_selection_invalid_names_alias(monkeypatch, tmp_path, csk_home, skills_root, capsys):
    project = make_project(tmp_path)
    write_skillfile(
        project,
        {
            "schema_version": 2,
            "sources": {"not an alias": {"path": "."}},
            "skills": [{"name": "x", "from": "not an alias", "directory": "."}],
        },
    )
    _register_draft_project(monkeypatch, csk_home, skills_root, project)
    _enable_env_opt_in(monkeypatch)

    assert cli.main(["check", "app"]) == 1
    _assert_diagnostic(
        capsys.readouterr().err,
        code="source_selection_invalid",
        subject="'not an alias'",
        reason="portable identifier",
    )


# The secret class is generated over the WHOLE hostile alphabet.
# Revision 2 fitted its alphabet to the sanitizer (quotes and
# backslashes "stay out so repr keeps single-quote spelling"); every
# character excluded there is REQUIRED in each secret here. Each
# secret is embedded in a template position that guarantees a
# structural refusal, so every member reaches the renderer.
_SECRET_CLASS_REQUIRED_CHARS = ("'", '"', "\\", "\n", "/", " ", "+", "=", ":")
_SECRET_CLASS_SEED = 0xC1A55


def _generated_class_secret(rng, length=32, *, with_newline=True):
    alphabet = (
        "abcdef0123456789/:+= ABCDEFGHJKLMNPQRSTUVWXYZ'\"\\\n?#%@&;_,.~`|"
    )
    if not with_newline:
        alphabet = alphabet.replace("\n", "")
    required = tuple(
        char for char in _SECRET_CLASS_REQUIRED_CHARS if with_newline or char != "\n"
    )
    chars = [rng.choice(alphabet) for _ in range(length)]
    pinned = {at for at, char in enumerate(chars) if char in required}
    for need in required:
        if need in chars:
            continue
        candidates = [at for at in range(length) if at not in pinned]
        at = rng.choice(candidates)
        chars[at] = need
        pinned.add(at)
    return "".join(chars)


def _secret_class_members():
    """(id, url, secret) triples: reviewer repros plus generated members.

    Ids never contain secret material: pytest prints them.
    """

    explicit = [
        # Revision-1 cases, kept as regression pins.
        (
            "https-userinfo-slash",
            "https://operator:abc/def+ghi=@git.example.com/team/kit.git",
            "abc/def+ghi=",
        ),
        (
            "https-userinfo-space",
            "https://operator:pass word@git.example.com/team/kit.git",
            "pass word",
        ),
        ("https-token-user", "https://ghp_abc/Z9x@github.com/o/r.git", "ghp_abc/Z9x"),
        (
            "https-query-denylisted",
            "https://git.example.com/team/kit.git?private_token=QUERYSECRET123",
            "QUERYSECRET123",
        ),
        ("ssh-userinfo", "ssh://deploy:hunter2@corp.example/kit.git", "hunter2"),
        # Revision-2 reproductions: scp has no ``://`` anchor, quotes
        # flip repr quoting, and these query names are outside every
        # denylist.
        (
            "scp-creds",
            "op:hunter2@git.example.com:team/kit.git",
            "hunter2",
        ),
        (
            "scp-token-user-host-refused",
            "ghp_ABC123:x@github.com:o/r.git",
            "ghp_ABC123",
        ),
        (
            "https-userinfo-quotes",
            "https://op:pa'ss\"x@git.example.com/t/k.git",
            "pa'ss\"x",
        ),
        (
            "https-query-sig",
            "https://git.example.com/t/k.git?sig=SIGSECRET",
            "SIGSECRET",
        ),
        (
            "https-query-jwt",
            "https://git.example.com/t/k.git?jwt=JWTSECRET",
            "JWTSECRET",
        ),
        (
            "https-query-pass",
            "https://git.example.com/t/k.git?pass=PASSSECRET",
            "PASSSECRET",
        ),
        (
            "https-query-amz",
            "https://git.example.com/t/k.git?X-Amz-Signature=AMZSECRET",
            "AMZSECRET",
        ),
        (
            "https-query-code",
            "https://git.example.com/t/k.git?code=CODESECRET",
            "CODESECRET",
        ),
        (
            "https-fragment",
            "https://git.example.com/t/k.git#access_token=FRAGSECRET",
            "FRAGSECRET",
        ),
        (
            "https-percent-query",
            "https://git.example.com/t/k.git%3Ftoken=PCTSECRET",
            "PCTSECRET",
        ),
        (
            "https-userinfo-scheme-in-password",
            "https://op:sec://x@git.example.com/t/k.git",
            "sec://x",
        ),
    ]
    members = [(kind, url, secret) for kind, url, secret in explicit]
    rng = random.Random(_SECRET_CLASS_SEED)
    query_names = ["sig", "jwt", "pass", "code", "X-Amz-Signature", "session"]
    for index in range(6):
        # Even indices carry a newline (control-scalar entry
        # refusal); odd indices reach the spelling splitters with
        # hostile quoting intact.
        secret = _generated_class_secret(rng, with_newline=(index % 2 == 0))
        members.append(
            (
                f"gen-https-userinfo-{index}",
                f"https://operator:{secret}@git.example.com/team/kit.git",
                secret,
            )
        )
        members.append(
            (
                f"gen-ssh-userinfo-{index}",
                f"ssh://deploy:{secret}@corp.example/kit.git",
                secret,
            )
        )
        members.append(
            (
                f"gen-scp-{index}",
                f"op:{secret}@git.example.com:team/kit.git",
                secret,
            )
        )
        members.append(
            (
                f"gen-query-{index}",
                f"https://git.example.com/team/kit.git?{query_names[index]}={secret}",
                secret,
            )
        )
    return members


_SECRET_CLASS_MEMBERS = _secret_class_members()
_SECRET_CLASS_IDS = [member[0] for member in _SECRET_CLASS_MEMBERS]

# Every surface that renders a diagnostic: (id, argv, exit code).
_SECRET_CLASS_SURFACES = (
    ("check", ["check", "app"], 1),
    ("install", ["install", "app"], 1),
    ("upgrade", ["upgrade", "app"], 1),
    ("status", ["status", "app"], 2),
    ("status-json", ["status", "app", "--json"], 2),
)
_SECRET_CLASS_SURFACE_IDS = [surface[0] for surface in _SECRET_CLASS_SURFACES]


def _write_secret_skillfile(project, url):
    write_skillfile(
        project,
        {
            "schema_version": 2,
            "sources": {"evil": {"git": url, "tag": "v1.0.0"}},
            "skills": [{"name": "x", "from": "evil", "directory": "."}],
        },
    )
    # Non-vacuity input control: the declaration really entered the
    # pipeline byte-exact, so an absence below is a refused echo.
    stored = json.loads((project / "Skillfile.json").read_text(encoding="utf-8"))
    assert stored["sources"]["evil"]["git"] == url


@pytest.mark.parametrize(
    "member_id,url,secret", _SECRET_CLASS_MEMBERS, ids=_SECRET_CLASS_IDS
)
@pytest.mark.parametrize(
    "surface,argv,expected_exit", _SECRET_CLASS_SURFACES, ids=_SECRET_CLASS_SURFACE_IDS
)
def test_cli_secret_class_never_renders_on_any_surface(
    monkeypatch, tmp_path, csk_home, skills_root, capsys,
    member_id, url, secret, surface, argv, expected_exit,
):
    """CLASS test (repeat-of rev1/secret-echo-userinfo-charclass).

    Every member of the secret-bearing declaration class refuses
    structurally on every diagnostic surface: the refusal names the
    alias, the field and the shape, and the declaration itself never
    renders, in any spelling, quoting or encoding. Parametrised (not
    a loop) so each member fails visibly on its own.
    """

    project = make_project(tmp_path)
    _write_secret_skillfile(project, url)
    _register_draft_project(monkeypatch, csk_home, skills_root, project)
    _enable_env_opt_in(monkeypatch)

    assert cli.main(list(argv)) == expected_exit, (member_id, surface)
    captured = capsys.readouterr()
    combined = captured.out + captured.err
    assert "source_selection_invalid:" in captured.err, (member_id, surface, combined)
    assert "'evil'" in captured.err, (member_id, surface, combined)
    assert len(_remediation_lines(captured.err)) == 1, (member_id, surface, combined)
    assert _DRAFT_LABEL not in captured.err, (member_id, surface, combined)
    assert secret not in combined, (member_id, surface, combined)
    assert repr(secret)[1:-1] not in combined, (member_id, surface, combined)
    assert url not in combined, (member_id, surface, combined)


@pytest.mark.parametrize(
    "member_id,url,secret", _SECRET_CLASS_MEMBERS, ids=_SECRET_CLASS_IDS
)
def test_cli_secret_class_absent_with_sanitizer_disabled(
    monkeypatch, tmp_path, csk_home, skills_root, capsys, member_id, url, secret
):
    """Structural proof: absence holds with the sanitizer stubbed out.

    Revision 2 ran this control expecting PRESENCE (the sanitizer was
    the gate). Revision 3 inverts it: with ``sanitize_detail``
    replaced by identity through ``csk check``, every member is still
    absent, which proves the echo site is gone rather than covered.
    """

    project = make_project(tmp_path)
    _write_secret_skillfile(project, url)
    _register_draft_project(monkeypatch, csk_home, skills_root, project)
    _enable_env_opt_in(monkeypatch)
    monkeypatch.setattr(source_errors, "sanitize_detail", lambda text: text)

    assert cli.main(["check", "app"]) == 1, member_id
    captured = capsys.readouterr()
    combined = captured.out + captured.err
    assert "source_selection_invalid:" in captured.err, (member_id, combined)
    assert secret not in combined, (member_id, combined)
    assert repr(secret)[1:-1] not in combined, (member_id, combined)
    assert url not in combined, (member_id, combined)


def test_cli_valid_token_shaped_username_never_renders(
    monkeypatch, tmp_path, csk_home, skills_root, capsys
):
    """Accepted side of the class: a token-shaped scp username is data.

    ``ghp_...@github.com:o/r.git`` is a legitimate scp declaration (a
    username is indistinguishable from a token), so it validates and
    plans; the username still never renders on any surface it reaches.
    """

    token = "ghp_ABCD1234EFGH5678"
    url = f"{token}@github.com:o/r.git"
    project = make_project(tmp_path)
    _write_secret_skillfile(project, url)
    _register_draft_project(monkeypatch, csk_home, skills_root, project)
    _enable_env_opt_in(monkeypatch)

    assert cli.main(["check", "app"]) == 0
    captured = capsys.readouterr()
    assert token not in captured.out + captured.err, captured
    assert url not in captured.out + captured.err, captured

    assert cli.main(["install", "app"]) == 1  # network bound refusal
    captured = capsys.readouterr()
    assert token not in captured.out + captured.err, captured
    assert url not in captured.out + captured.err, captured


def test_cli_diagnostic_source_member_missing(monkeypatch, tmp_path, csk_home, skills_root, capsys):
    _require_posix_traversal()
    project = make_project(tmp_path)
    _write_draft_skill(project / "agents" / "skills" / "team" / "alpha", "alpha")
    write_skillfile(
        project,
        {
            "schema_version": 2,
            "sources": {"local": {"path": "."}},
            "skills": [
                {"from": "local", "directory": "agents/skills/team", "include": ["ghost"]}
            ],
        },
    )
    _register_draft_project(monkeypatch, csk_home, skills_root, project)
    _enable_env_opt_in(monkeypatch)

    assert cli.main(["install", "app"]) == 1
    _assert_diagnostic(
        capsys.readouterr().err,
        code="source_member_missing",
        subject="'ghost'",
        reason="does not exist",
    )


def test_cli_diagnostic_source_member_invalid(monkeypatch, tmp_path, csk_home, skills_root, capsys):
    _require_posix_traversal()
    project = make_project(tmp_path)
    _write_draft_skill(project / "agents" / "skills" / "bad", "bad", bad="noname")
    write_skillfile(
        project,
        {
            "schema_version": 2,
            "sources": {"local": {"path": "."}},
            "skills": [{"name": "bad", "from": "local", "directory": "agents/skills/bad"}],
        },
    )
    _register_draft_project(monkeypatch, csk_home, skills_root, project)
    _enable_env_opt_in(monkeypatch)

    assert cli.main(["install", "app"]) == 1
    _assert_diagnostic(
        capsys.readouterr().err,
        code="source_member_invalid",
        subject="agents/skills/bad",
        reason="'name'",
    )


def test_cli_diagnostic_source_name_conflict(monkeypatch, tmp_path, csk_home, skills_root, capsys):
    project = make_project(tmp_path)
    write_skillfile(
        project,
        {
            "schema_version": 2,
            "sources": {"local": {"path": "."}},
            "skills": [
                {"name": "dup", "from": "local", "directory": "agents/skills/a"},
                {"name": "dup", "from": "local", "directory": "agents/skills/b"},
            ],
        },
    )
    _register_draft_project(monkeypatch, csk_home, skills_root, project)
    _enable_env_opt_in(monkeypatch)

    assert cli.main(["check", "app"]) == 1
    _assert_diagnostic(
        capsys.readouterr().err,
        code="source_name_conflict",
        subject="dup",
        reason="Duplicate skill name",
    )


def test_cli_install_structural_error_renders_diagnostic(monkeypatch, tmp_path, csk_home, skills_root, capsys):
    """Structural failures via ``install`` render fully, not raw."""

    project = make_project(tmp_path)
    write_skillfile(
        project,
        {
            "schema_version": 2,
            "sources": {"local": {"path": "."}},
            "skills": [
                {"name": "dup", "from": "local", "directory": "agents/skills/a"},
                {"name": "dup", "from": "local", "directory": "agents/skills/b"},
            ],
        },
    )
    _register_draft_project(monkeypatch, csk_home, skills_root, project)
    _enable_env_opt_in(monkeypatch)

    assert cli.main(["install", "app"]) == 1
    _assert_diagnostic(
        capsys.readouterr().err,
        code="source_name_conflict",
        subject="dup",
        reason="Duplicate skill name",
    )


def test_cli_status_structural_error_renders_diagnostic(monkeypatch, tmp_path, csk_home, skills_root, capsys):
    """Structural failures via ``status`` render fully with exit 2."""

    project = make_project(tmp_path)
    write_skillfile(
        project,
        {
            "schema_version": 2,
            "sources": {"local": {"path": "."}},
            "skills": [
                {"name": "dup", "from": "local", "directory": "agents/skills/a"},
                {"name": "dup", "from": "local", "directory": "agents/skills/b"},
            ],
        },
    )
    _register_draft_project(monkeypatch, csk_home, skills_root, project)
    _enable_env_opt_in(monkeypatch)

    assert cli.main(["status", "app"]) == 2
    _assert_diagnostic(
        capsys.readouterr().err,
        code="source_name_conflict",
        subject="dup",
        reason="Duplicate skill name",
    )


def test_cli_diagnostic_source_output_overlap(monkeypatch, tmp_path, csk_home, skills_root, capsys):
    _require_posix_traversal()
    project = make_project(tmp_path)
    _write_draft_skill(project / ".agents" / "evil" / "review", "review")
    write_skillfile(
        project,
        {
            "schema_version": 2,
            "sources": {"evil": {"path": ".agents/evil"}},
            "skills": [{"name": "review", "from": "evil", "directory": "review"}],
        },
    )
    _register_draft_project(monkeypatch, csk_home, skills_root, project)
    _enable_env_opt_in(monkeypatch)

    assert cli.main(["install", "app"]) == 1
    _assert_diagnostic(
        capsys.readouterr().err,
        code="source_output_overlap",
        subject="review",
        reason="managed output",
    )


def test_cli_diagnostic_source_snapshot_changed(monkeypatch, tmp_path, csk_home, skills_root, capsys):
    _require_posix_traversal()
    project = make_project(tmp_path)
    _write_draft_skill(project / "agents" / "skills" / "review", "review")
    write_skillfile(
        project,
        {
            "schema_version": 2,
            "sources": {"local": {"path": "."}},
            "skills": [{"name": "review", "from": "local", "directory": "agents/skills/review"}],
        },
    )
    _register_draft_project(monkeypatch, csk_home, skills_root, project)
    _enable_env_opt_in(monkeypatch)

    assert cli.main(["install", "app"]) == 0
    capsys.readouterr()
    (project / "agents" / "skills" / "review" / "SKILL.md").write_text(
        "---\nname: review\ndescription: MUTATED\n---\n\n# review\n", encoding="utf-8"
    )
    assert cli.main(["install", "app"]) == 1
    _assert_diagnostic(
        capsys.readouterr().err,
        code="source_snapshot_changed",
        subject="'review'",
        reason="changed since the lock",
    )


def test_cli_diagnostic_source_snapshot_unavailable(monkeypatch, tmp_path, csk_home, skills_root, capsys):
    _require_posix_traversal()
    project = make_project(tmp_path)
    _write_draft_skill(project / "agents" / "skills" / "review", "review")
    write_skillfile(
        project,
        {
            "schema_version": 2,
            "sources": {"local": {"path": "."}},
            "skills": [{"name": "review", "from": "local", "directory": "agents/skills/review"}],
        },
    )
    _register_draft_project(monkeypatch, csk_home, skills_root, project)
    _enable_env_opt_in(monkeypatch)

    assert cli.main(["install", "app"]) == 0
    capsys.readouterr()
    shutil.rmtree(project / "agents" / "skills" / "review")
    snapshots = source_store.store_root(csk_home) / source_store.SNAPSHOTS_DIRNAME
    shutil.rmtree(snapshots)
    assert cli.main(["install", "app"]) == 1
    _assert_diagnostic(
        capsys.readouterr().err,
        code="source_snapshot_unavailable",
        subject="'review'",
        reason="unavailable",
    )


def test_cli_diagnostic_source_lock_stale(monkeypatch, tmp_path, csk_home, skills_root, capsys):
    _require_posix_traversal()
    project = make_project(tmp_path)
    _write_draft_skill(project / "agents" / "skills" / "review", "review")
    _write_draft_skill(project / "agents" / "skills" / "extra", "extra")
    write_skillfile(
        project,
        {
            "schema_version": 2,
            "sources": {"local": {"path": "."}},
            "skills": [{"name": "review", "from": "local", "directory": "agents/skills/review"}],
        },
    )
    _register_draft_project(monkeypatch, csk_home, skills_root, project)
    _enable_env_opt_in(monkeypatch)

    assert cli.main(["install", "app"]) == 0
    capsys.readouterr()
    write_skillfile(
        project,
        {
            "schema_version": 2,
            "sources": {"local": {"path": "."}},
            "skills": [
                {"name": "review", "from": "local", "directory": "agents/skills/review"},
                {"name": "extra", "from": "local", "directory": "agents/skills/extra"},
            ],
        },
    )
    assert cli.main(["check", "app"]) == 1
    _assert_diagnostic(
        capsys.readouterr().err,
        code="source_lock_stale",
        subject="manifest_sha256",
        reason="does not match current Skillfile",
    )


def _write_policy_with_decoy(home, repositories):
    decoy = {
        "endpoints": [
            {"url": "https://decoy.example.net/stuff.git", "authentication": "decoy-auth"}
        ],
        "fallback": "none",
    }
    home.joinpath("source-policy.json").write_text(
        json.dumps(
            {
                "schema_version": 2,
                "repositories": {"decoy.example.net/stuff": decoy, **repositories},
            }
        ),
        encoding="utf-8",
    )


def test_cli_diagnostic_repository_policy_invalid(monkeypatch, tmp_path, csk_home, skills_root, capsys):
    project = make_project(tmp_path)
    write_skillfile(
        project,
        {
            "schema_version": 2,
            "sources": {"net": {"repository": "example.org/kit", "tag": "v1.0.0"}},
            "skills": [{"name": "x", "from": "net", "directory": "."}],
        },
    )
    _register_draft_project(monkeypatch, csk_home, skills_root, project)
    _enable_env_opt_in(monkeypatch)
    policy_path = csk_home / "source-policy.json"
    policy_path.write_text("{not protocol json", encoding="utf-8")

    assert cli.main(["check", "app"]) == 1
    err = capsys.readouterr().err
    _assert_diagnostic(
        err,
        code="repository_policy_invalid",
        subject="source policy",
        reason="not valid protocol JSON",
    )
    assert "<path>" in err
    assert str(policy_path) not in err


def test_cli_diagnostic_repository_endpoint_unavailable(monkeypatch, tmp_path, csk_home, skills_root, capsys):
    project = make_project(tmp_path)
    write_skillfile(
        project,
        {
            "schema_version": 2,
            "sources": {"net": {"repository": "example.org/kit", "tag": "v1.0.0"}},
            "skills": [{"name": "x", "from": "net", "directory": "."}],
        },
    )
    _register_draft_project(monkeypatch, csk_home, skills_root, project)
    _enable_env_opt_in(monkeypatch)

    assert cli.main(["check", "app"]) == 1
    _assert_diagnostic(
        capsys.readouterr().err,
        code="repository_endpoint_unavailable",
        subject="example.org/kit",
        reason="no declared or policy endpoint",
    )


def test_cli_diagnostic_repository_mirror_undeclared(monkeypatch, tmp_path, csk_home, skills_root, capsys):
    project = make_project(tmp_path)
    write_skillfile(
        project,
        {
            "schema_version": 2,
            "sources": {"net": {"repository": "example.org/kit", "tag": "v1.0.0"}},
            "skills": [{"name": "x", "from": "net", "directory": "."}],
        },
    )
    _register_draft_project(monkeypatch, csk_home, skills_root, project)
    _enable_env_opt_in(monkeypatch)
    _write_policy_with_decoy(
        csk_home,
        {
            "example.org/kit": {
                "endpoints": [
                    {"url": "https://mirror.example.net/kit.git", "authentication": "a1"}
                ],
                "fallback": "none",
            }
        },
    )

    assert cli.main(["check", "app"]) == 1
    err = capsys.readouterr().err
    _assert_diagnostic(
        err,
        code="repository_mirror_undeclared",
        subject="mirror.example.net",
        reason="without mirror_of",
    )
    assert "decoy.example.net" not in err


def test_cli_diagnostic_repository_alias_unknown(monkeypatch, tmp_path, csk_home, skills_root, capsys):
    project = make_project(tmp_path)
    write_skillfile(
        project,
        {
            "schema_version": 2,
            "sources": {"net": {"repository": "example.org/kit", "tag": "v1.0.0"}},
            "skills": [{"name": "x", "from": "net", "directory": "."}],
        },
    )
    _register_draft_project(monkeypatch, csk_home, skills_root, project)
    _enable_env_opt_in(monkeypatch)
    _write_policy_with_decoy(
        csk_home,
        {
            "example.org/kit": {
                "endpoints": [
                    {
                        "url": "https://example.org/kit.git",
                        "authentication": "a1",
                        "alias": "ghost-alias",
                    }
                ],
                "fallback": "none",
            }
        },
    )

    assert cli.main(["check", "app"]) == 1
    err = capsys.readouterr().err
    _assert_diagnostic(
        err,
        code="repository_alias_unknown",
        subject="ghost-alias",
        reason="unknown alias",
    )
    assert "decoy.example.net" not in err


def _install_draft_review(monkeypatch, tmp_path, csk_home, skills_root):
    """One installed schema-2 project; returns (project, review_skill_dir)."""

    project = make_project(tmp_path)
    skill_dir = project / "agents" / "skills" / "review"
    _write_draft_skill(skill_dir, "review")
    write_skillfile(
        project,
        {
            "schema_version": 2,
            "sources": {"local": {"path": "."}},
            "skills": [{"name": "review", "from": "local", "directory": "agents/skills/review"}],
        },
    )
    _register_draft_project(monkeypatch, csk_home, skills_root, project)
    return project, skill_dir


def test_cli_schema2_commands_work_without_legacy_opt_in(
    monkeypatch, tmp_path, csk_home, skills_root, capsys
):
    _require_posix_traversal()
    _install_draft_review(monkeypatch, tmp_path, csk_home, skills_root)
    cfg = json.loads((csk_home / "config.json").read_text(encoding="utf-8"))
    assert "experimental" not in cfg
    assert "CSK_EXPERIMENTAL_SKILLFILE_SOURCES" not in os.environ

    assert cli.main(["check", "app"]) == 0
    assert "schema_version 2 valid" in capsys.readouterr().out
    assert cli.main(["install", "app"]) == 0
    assert "lock created" in capsys.readouterr().out
    assert cli.main(["upgrade", "app"]) == 0
    assert "review up-to-date" in capsys.readouterr().out
    assert cli.main(["status", "app"]) == 0
    assert "source_lock_stale" not in capsys.readouterr().out


def test_cli_install_schema2_default_on_without_legacy_setting(
    monkeypatch, tmp_path, csk_home, skills_root, capsys
):
    _require_posix_traversal()
    _install_draft_review(monkeypatch, tmp_path, csk_home, skills_root)
    assert "CSK_EXPERIMENTAL_SKILLFILE_SOURCES" not in os.environ
    cfg = json.loads((csk_home / "config.json").read_text(encoding="utf-8"))
    assert "experimental" not in cfg

    assert cli.main(["install", "app"]) == 0
    assert "lock created" in capsys.readouterr().out


def test_cli_schema2_outputs_do_not_print_draft_label(
    monkeypatch, tmp_path, csk_home, skills_root, capsys
):
    _require_posix_traversal()
    _install_draft_review(monkeypatch, tmp_path, csk_home, skills_root)
    for argv in (
        ["check", "app"],
        ["install", "app"],
        ["upgrade", "app"],
        ["status", "app"],
        ["status", "app", "--json"],
    ):
        assert cli.main(argv) == 0, argv
        captured = capsys.readouterr()
        assert _DRAFT_LABEL not in captured.out + captured.err, (argv, captured)


def test_cli_legacy_schema2_switches_are_noops(
    monkeypatch, tmp_path, csk_home, skills_root, capsys
):
    project = make_project(tmp_path)
    write_skillfile(
        project,
        {
            "schema_version": 2,
            "sources": {"local": {"path": "."}},
            "skills": [{"name": "review", "from": "local", "directory": "agents/skills/review"}],
        },
    )
    _write_draft_skill(project / "agents" / "skills" / "review", "review")
    cfg_path = _register_draft_project(monkeypatch, csk_home, skills_root, project)
    monkeypatch.delenv("CSK_EXPERIMENTAL_SKILLFILE_SOURCES", raising=False)
    commands = (
        ("check", "app"),
        ("install", "app"),
        ("upgrade", "app"),
        ("status", "app"),
    )

    # Create the lock before pinning output so install is stable in each mode.
    assert cli.main(["install", "app"]) == 0
    capsys.readouterr()
    expected: dict[tuple[str, ...], tuple[str, str]] = {}
    for argv in commands:
        assert cli.main(list(argv)) == 0, argv
        captured = capsys.readouterr()
        assert _DRAFT_LABEL not in captured.out + captured.err, (argv, captured)
        expected[argv] = (captured.out, captured.err)

    cfg = json.loads(cfg_path.read_text(encoding="utf-8"))
    cfg["experimental"] = {"skillfile_sources": True}
    cfg_path.write_text(json.dumps(cfg), encoding="utf-8")
    for argv in commands:
        assert cli.main(list(argv)) == 0, argv
        configured = capsys.readouterr()
        assert (configured.out, configured.err) == expected[argv]

    del cfg["experimental"]
    cfg_path.write_text(json.dumps(cfg), encoding="utf-8")
    monkeypatch.setenv("CSK_EXPERIMENTAL_SKILLFILE_SOURCES", "1")
    for argv in commands:
        assert cli.main(list(argv)) == 0, argv
        environment = capsys.readouterr()
        assert (environment.out, environment.err) == expected[argv]


def test_cli_install_schema2_success_omits_draft_label(monkeypatch, tmp_path, csk_home, skills_root, capsys):
    _require_posix_traversal()
    _install_draft_review(monkeypatch, tmp_path, csk_home, skills_root)

    assert cli.main(["install", "app"]) == 0
    out = capsys.readouterr().out
    assert _DRAFT_LABEL not in out
    assert "lock created" in out


def test_cli_upgrade_refreshes_stale_lock(monkeypatch, tmp_path, csk_home, skills_root, capsys):
    _require_posix_traversal()
    project, _ = _install_draft_review(monkeypatch, tmp_path, csk_home, skills_root)
    assert cli.main(["install", "app"]) == 0
    capsys.readouterr()
    _write_draft_skill(project / "agents" / "skills" / "extra", "extra")
    write_skillfile(
        project,
        {
            "schema_version": 2,
            "sources": {"local": {"path": "."}},
            "skills": [
                {"name": "review", "from": "local", "directory": "agents/skills/review"},
                {"name": "extra", "from": "local", "directory": "agents/skills/extra"},
            ],
        },
    )
    assert cli.main(["install", "app"]) == 1  # stale lock refuses locked install

    assert cli.main(["upgrade", "app"]) == 0
    out = capsys.readouterr().out
    assert "lock replaced" in out
    assert _DRAFT_LABEL not in out


def test_cli_status_schema2_text_and_json_omit_draft_label(monkeypatch, tmp_path, csk_home, skills_root, capsys):
    _require_posix_traversal()
    _install_draft_review(monkeypatch, tmp_path, csk_home, skills_root)
    assert cli.main(["install", "app"]) == 0
    capsys.readouterr()

    assert cli.main(["status", "app"]) == 0
    text = capsys.readouterr().out
    assert _DRAFT_LABEL not in text
    assert cli.main(["status", "app", "--json"]) == 0
    json_text = capsys.readouterr().out
    assert _DRAFT_LABEL not in json_text
    payload = json.loads(json_text)
    assert "draft_sources" not in payload[0]


def test_cli_schema2_install_uses_skillfile_locale_for_status(
    monkeypatch, tmp_path, csk_home, skills_root, capsys
):
    """Install and read-only status agree on Skillfile.locale without locale files."""

    _require_posix_traversal()
    project, skill_dir = _install_draft_review(monkeypatch, tmp_path, csk_home, skills_root)
    assert not (skill_dir / "locales").exists()
    assert not (skill_dir / ".skill_triggers").exists()
    skillfile_path = project / "Skillfile.json"
    skillfile = json.loads(skillfile_path.read_text(encoding="utf-8"))
    skillfile["locale"] = "ru"
    write_skillfile(project, skillfile)

    assert cli.main(["install", "app"]) == 0
    capsys.readouterr()

    assert cli.main(["status", "app"]) == 0
    status_output = capsys.readouterr().out
    assert "up-to-date" in status_output, status_output
    assert "marker-mismatch" not in status_output, status_output

    check_result = cli.main(["status", "app", "--check"])
    check_output = capsys.readouterr().out
    assert check_result == 0, check_output
    assert "up-to-date" in check_output, check_output

    marker_path = project / ".agents" / "skills" / "review" / ".csk-install.json"
    marker = json.loads(marker_path.read_text(encoding="utf-8"))
    assert marker["locale"] == "ru"
    marker["locale"] = None
    marker_path.write_text(json.dumps(marker, indent=2, sort_keys=True) + "\n", encoding="utf-8")

    mismatch_result = cli.main(["status", "app", "--check"])
    mismatch_output = capsys.readouterr().out
    assert mismatch_result == 1, mismatch_output
    assert "marker-mismatch" in mismatch_output, mismatch_output
    assert "locale" in mismatch_output, mismatch_output


def test_cli_status_check_exit_codes_schema2(monkeypatch, tmp_path, csk_home, skills_root, capsys):
    _require_posix_traversal()
    _project, skill_dir = _install_draft_review(monkeypatch, tmp_path, csk_home, skills_root)
    assert cli.main(["install", "app"]) == 0
    capsys.readouterr()

    assert cli.main(["status", "app", "--check"]) == 0
    capsys.readouterr()
    (skill_dir / "SKILL.md").write_text(
        "---\nname: review\ndescription: MUTATED\n---\n\n# review\n", encoding="utf-8"
    )
    assert cli.main(["status", "app", "--check"]) == 1
    assert "source-changed" in capsys.readouterr().out


def test_cli_status_sanitizes_error_details(monkeypatch, tmp_path, csk_home, skills_root, capsys):
    """Absolute paths in status error rows redact to ``<path>``."""

    _require_posix_traversal()
    project, _ = _install_draft_review(monkeypatch, tmp_path, csk_home, skills_root)
    assert cli.main(["install", "app"]) == 0
    capsys.readouterr()
    lock_path = project / "Skillfile.lock.json"
    lock_path.unlink()
    lock_path.mkdir()

    assert cli.main(["status", "app"]) == 0
    out = capsys.readouterr().out
    error_lines = [line for line in out.splitlines() if "ERROR" in line]
    assert len(error_lines) == 1, out
    assert "source_output_overlap" in error_lines[0], out
    assert "<path>" in error_lines[0], out
    assert str(tmp_path) not in error_lines[0], out


def test_cli_status_lock_stale_renders_remediation_text_and_json(
    monkeypatch, tmp_path, csk_home, skills_root, capsys
):
    """The stale-lock class reaches status with its remediation line."""

    _require_posix_traversal()
    project, _ = _install_draft_review(monkeypatch, tmp_path, csk_home, skills_root)
    assert cli.main(["install", "app"]) == 0
    capsys.readouterr()
    _write_draft_skill(project / "agents" / "skills" / "extra", "extra")
    write_skillfile(
        project,
        {
            "schema_version": 2,
            "sources": {"local": {"path": "."}},
            "skills": [
                {"name": "review", "from": "local", "directory": "agents/skills/review"},
                {"name": "extra", "from": "local", "directory": "agents/skills/extra"},
            ],
        },
    )

    assert cli.main(["status", "app"]) == 0
    out = capsys.readouterr().out
    assert "ERROR source_lock_stale:" in out, out
    assert len(_remediation_lines(out)) == 1, out
    assert "remediation: run csk upgrade to refresh the lock" in out, out

    assert cli.main(["status", "app", "--json"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert len(payload[0]["errors"]) == 1, payload
    row = payload[0]["errors"][0]
    assert row.startswith("source_lock_stale: "), row
    assert row.count("remediation: ") == 1, row
    assert "remediation: run csk upgrade to refresh the lock" in row, row
    assert _DRAFT_LABEL not in row, row


@pytest.mark.parametrize("code", source_diagnostics.STABLE_DIAGNOSTIC_CODES)
@pytest.mark.parametrize("as_json", [False, True])
def test_cli_status_error_row_renders_every_table_code(
    monkeypatch, tmp_path, csk_home, skills_root, capsys, code, as_json
):
    """Every table code renders with remediation through status, text and JSON.

    The evaluated error strings are crafted per code, but everything
    downstream is production: the real collector, the real status row
    mapping, the real renderer and the real text/JSON surfaces.
    """

    project = make_project(tmp_path)
    write_skillfile(
        project,
        {
            "schema_version": 2,
            "sources": {"local": {"path": "."}},
            "skills": [{"name": "review", "from": "local", "directory": "agents/skills/review"}],
        },
    )
    _register_draft_project(monkeypatch, csk_home, skills_root, project)
    _enable_env_opt_in(monkeypatch)

    # Positive control: the same fixture reports currentness unfaulted.
    assert cli.main(["status", "app"]) == 0
    assert "no lock" in capsys.readouterr().out

    item = f"{code}: subject fixture-member failed for a stated reason"
    reached = []

    def crafted(**kwargs):
        reached.append(True)
        assert kwargs["project_path"] == project
        return status.source_publish.Schema2Status(
            members=(), errors=(item,), lock_sha256=None
        )

    monkeypatch.setattr(
        status.source_publish, "evaluate_schema2_installation", crafted
    )
    argv = ["status", "app", "--json"] if as_json else ["status", "app"]
    assert cli.main(argv) == 0
    assert reached, "the crafted evaluation must reach the real call site"
    if as_json:
        payload = json.loads(capsys.readouterr().out)
        assert len(payload[0]["errors"]) == 1, payload
        row = payload[0]["errors"][0]
        assert row.startswith(f"{code}: "), row
        assert "fixture-member" in row, row
        assert "stated reason" in row, row
        assert row.count("remediation: ") == 1, row
        assert _DRAFT_LABEL not in row, row
    else:
        out = capsys.readouterr().out
        assert f"ERROR {code}:" in out, out
        assert "fixture-member" in out, out
        assert "stated reason" in out, out
        assert len(_remediation_lines(out)) == 1, out
        assert _DRAFT_LABEL not in out, out


def test_cli_status_member_detail_sanitizes_paths(
    monkeypatch, tmp_path, csk_home, skills_root, capsys
):
    """Absolute paths in status member details redact to ``<path>``."""

    _require_posix_traversal()
    external = tmp_path / "external-source"
    _write_draft_skill(external / "review", "review")
    project = make_project(tmp_path)
    write_skillfile(
        project,
        {
            "schema_version": 2,
            "sources": {"vendor": {"path": str(external)}},
            "skills": [{"name": "review", "from": "vendor", "directory": "review"}],
        },
    )
    _register_draft_project(monkeypatch, csk_home, skills_root, project)
    _enable_env_opt_in(monkeypatch)
    assert cli.main(["install", "app"]) == 0
    capsys.readouterr()
    shutil.rmtree(external)

    assert cli.main(["status", "app"]) == 0
    out = capsys.readouterr().out
    member_lines = [line for line in out.splitlines() if "review" in line and "lock" in line]
    assert len(member_lines) == 1, out
    assert "<path>" in member_lines[0], out
    assert str(tmp_path) not in member_lines[0], out


def test_cli_network_sources_planned_not_acquired(
    monkeypatch, tmp_path, csk_home, skills_root, capsys
):
    """Check plans network sources; install and upgrade acquire or refuse closed.

    Rewritten for the TASK-260921-qe62bu replay: STORY-260916-mz020a (trunk
    5ed6553) implemented bounded network acquisition, so the draft premise
    "install and upgrade refuse network sources as unimplemented" no longer
    holds. ``check`` still plans without acquiring; ``install`` and
    ``upgrade`` now attempt acquisition through the bounded transport and,
    for this unreachable fixture, refuse fail-closed at trusted Git
    admission instead of ``source_selection_invalid``.
    """

    project = make_project(tmp_path)
    write_skillfile(
        project,
        {
            "schema_version": 2,
            "sources": {
                "local": {"path": "."},
                "team": {"git": "https://git.example.com/team/kit.git", "tag": "v1.4.0"},
                "kit": {
                    "repository": "git.example.com/team/kit",
                    "revision": "0123456789abcdef0123456789abcdef01234567",
                },
            },
            "skills": [
                {"name": "review", "from": "local", "directory": "agents/skills/review"},
                {"name": "kit", "from": "team", "directory": "skills/kit"},
            ],
        },
    )
    _register_draft_project(monkeypatch, csk_home, skills_root, project)
    _enable_env_opt_in(monkeypatch)
    _write_policy_with_decoy(
        csk_home,
        {
            "git.example.com/team/kit": {
                "endpoints": [
                    {
                        "url": "https://git.example.com/team/kit.git",
                        "authentication": "team-https",
                    }
                ],
                "fallback": "none",
            }
        },
    )

    assert cli.main(["check", "app"]) == 0
    out = capsys.readouterr().out
    assert "schema_version 2 valid" in out, out
    assert "network sources planned, not acquired" in out, out

    assert cli.main(["install", "app"]) == 1
    err = capsys.readouterr().err
    assert "build_repository_identity_invalid:" in err, err
    assert "trusted Git admission failed" in err, err

    assert cli.main(["upgrade", "app"]) == 1
    err = capsys.readouterr().err
    assert "build_repository_identity_invalid:" in err, err
    assert "trusted Git admission failed" in err, err


def test_cli_check_reports_lock_and_policy(monkeypatch, tmp_path, csk_home, skills_root, capsys):
    _require_posix_traversal()
    _install_draft_review(monkeypatch, tmp_path, csk_home, skills_root)
    assert cli.main(["install", "app"]) == 0
    capsys.readouterr()
    _write_policy_with_decoy(csk_home, {})

    assert cli.main(["check", "app"]) == 0
    out = capsys.readouterr().out
    assert "lock current" in out
    assert "policy schema 2" in out


def _exhaustion_error():
    def attempt(ordinal, classification):
        return source_transport.AttemptDiagnostic(
            ordinal=ordinal,
            endpoint=EndpointProvenance(
                listed_url="https://example.org/kit.git",
                url_host="example.org",
                url_port=None,
                resolved_host="example.org",
                resolved_port=443,
                alias=None,
                mirror_of=None,
            ),
            classification=classification,
            code="repository_endpoint_unavailable",
        )

    return source_transport.TransportResolutionError(
        (attempt(0, "timeout"), attempt(1, "connection-refused"))
    )


def test_cli_status_transport_exhaustion_renders_attempts(
    monkeypatch, tmp_path, csk_home, skills_root, capsys
):
    """Acquire-shape failures render through ``status`` with classifications."""

    project = make_project(tmp_path)
    write_skillfile(
        project,
        {
            "schema_version": 2,
            "sources": {"local": {"path": "."}},
            "skills": [{"name": "review", "from": "local", "directory": "agents/skills/review"}],
        },
    )
    _register_draft_project(monkeypatch, csk_home, skills_root, project)
    _enable_env_opt_in(monkeypatch)

    # Positive control: the same fixture reports currentness unfaulted.
    assert cli.main(["status", "app"]) == 0
    assert "no lock" in capsys.readouterr().out

    reached = []
    real_evaluate = status.source_publish.evaluate_schema2_installation

    def faulted(*args, **kwargs):
        reached.append(True)
        raise _exhaustion_error()

    monkeypatch.setattr(status.source_publish, "evaluate_schema2_installation", faulted)
    assert cli.main(["status", "app"]) == 2
    assert reached, "the injected fault must reach the real call site"
    err = capsys.readouterr().err
    assert "repository_endpoint_unavailable:" in err, err
    assert "attempt 0=timeout" in err, err
    assert "attempt 1=connection-refused" in err, err
    assert "https://example.org/kit.git" in err, err
    assert len(_remediation_lines(err)) == 1, err
    assert _DRAFT_LABEL not in err, err
    assert real_evaluate is not None


def test_cli_install_transport_exhaustion_renders_attempts(
    monkeypatch, tmp_path, csk_home, skills_root, capsys
):
    """Acquire-shape failures render through ``install`` with classifications."""

    _require_posix_traversal()
    _install_draft_review(monkeypatch, tmp_path, csk_home, skills_root)

    # Positive control: the same fixture installs unfaulted.
    assert cli.main(["install", "app"]) == 0
    capsys.readouterr()

    reached = []

    def faulted(*args, **kwargs):
        reached.append(True)
        raise _exhaustion_error()

    monkeypatch.setattr(installer.source_publish, "install_schema2", faulted)
    assert cli.main(["install", "app"]) == 1
    assert reached, "the injected fault must reach the real call site"
    err = capsys.readouterr().err
    assert "repository_endpoint_unavailable:" in err, err
    assert "attempt 0=timeout" in err, err
    assert "https://example.org/kit.git" in err, err
    assert len(_remediation_lines(err)) == 1, err
    assert _DRAFT_LABEL not in err, err


def _audit_events_during(func):
    """The repository's zero-side-effect counter: open + socket.* audit hook."""

    events = []
    state = {"armed": True}

    def hook(event, args):
        if state["armed"] and (event == "open" or event.startswith("socket.")):
            events.append((event, args))

    sys.addaudithook(hook)
    try:
        result = func()
    finally:
        state["armed"] = False
    return result, events


def _open_paths(events):
    paths = []
    for event, args in events:
        if event == "open":
            paths.append(os.path.realpath(str(args[0])))
    return paths


def test_cli_ordering_malformed_skillfile_precedes_policy_and_io(
    monkeypatch, tmp_path, csk_home, skills_root, capsys
):
    """A schema failure refuses before the policy is read or planned."""

    project = make_project(tmp_path)
    write_skillfile(
        project,
        {
            "schema_version": 2,
            "sources": {"local": {"path": "."}},
            "skills": [{"name": "review", "from": "local", "directory": "agents/skills/review"}],
        },
    )
    cfg_path = _register_draft_project(monkeypatch, csk_home, skills_root, project)
    _enable_env_opt_in(monkeypatch)
    policy_path = csk_home / "source-policy.json"
    policy_path.write_text('{"schema_version": 2, "repositories": {}}', encoding="utf-8")
    # Warm up every lazy import before the hook is installed.
    assert cli.main(["check", "app"]) == 0
    capsys.readouterr()
    write_skillfile(
        project,
        {
            "schema_version": 2,
            "sources": {"local": {"path": "."}},
            "skills": [{"name": "x", "from": "ghost", "directory": "."}],
        },
    )

    (code, _), events = _audit_events_during(lambda: (cli.main(["check", "app"]), None))
    captured = capsys.readouterr()
    assert code == 1
    assert "source_alias_unknown" in captured.err
    assert [event for event, _ in events if event.startswith("socket.")] == []
    allowed = {
        os.path.realpath(cfg_path),
        os.path.realpath(project / "Skillfile.json"),
    }
    opened = _open_paths(events)
    assert opened, "the hook must observe the declared-input reads"
    assert set(opened) <= allowed, opened
    assert os.path.realpath(policy_path) not in opened
    assert os.path.realpath(project / "Skillfile.lock.json") not in opened


def test_cli_ordering_invalid_policy_precedes_planning(
    monkeypatch, tmp_path, csk_home, skills_root, capsys
):
    """An invalid policy refuses before plans run and before any socket."""

    project = make_project(tmp_path)
    write_skillfile(
        project,
        {
            "schema_version": 2,
            "sources": {"net": {"repository": "example.org/kit", "tag": "v1.0.0"}},
            "skills": [{"name": "x", "from": "net", "directory": "."}],
        },
    )
    cfg_path = _register_draft_project(monkeypatch, csk_home, skills_root, project)
    _enable_env_opt_in(monkeypatch)
    policy_path = csk_home / "source-policy.json"
    policy_path.write_text('{"schema_version": 2, "repositories": {}}', encoding="utf-8")
    # Warm up every lazy import before the hook is installed.
    assert cli.main(["check", "app"]) == 1  # no entry: endpoint unavailable
    capsys.readouterr()
    policy_path.write_text("{not protocol json", encoding="utf-8")

    (code, _), events = _audit_events_during(lambda: (cli.main(["check", "app"]), None))
    captured = capsys.readouterr()
    assert code == 1
    assert "repository_policy_invalid" in captured.err
    assert [event for event, _ in events if event.startswith("socket.")] == []
    allowed = {
        os.path.realpath(cfg_path),
        os.path.realpath(project / "Skillfile.json"),
        os.path.realpath(policy_path),
    }
    opened = _open_paths(events)
    assert os.path.realpath(policy_path) in opened, opened
    assert set(opened) <= allowed, opened
    assert os.path.realpath(project / "Skillfile.lock.json") not in opened


def test_cli_launch_layer_imports_no_source_resolution():
    """Launch reaches no source resolution: only inert shared modules.

    ``csk.shims`` transitively imports the marker-v5 support modules
    (constants, pure identity/inventory) through ``csk.install_marker``.
    The resolution, traversal, acquisition and publication modules
    must stay out of that closure, so launch code cannot rescan live
    source inputs however it is called.
    """

    srcdir = Path(cli.__file__).resolve().parent.parent
    snippet = (
        "import json, sys, csk.shims; "
        "print(json.dumps(sorted("
        "m for m in sys.modules if m.startswith('csk.sources'))))"
    )
    proc = subprocess.run(
        [sys.executable, "-c", snippet],
        capture_output=True,
        text=True,
        env={**os.environ, "PYTHONPATH": str(srcdir)},
        check=False,
    )
    assert proc.returncode == 0, proc.stderr
    closure = set(json.loads(proc.stdout))
    assert closure == {
        "csk.sources",
        "csk.sources.errors",
        "csk.sources.local_snapshot",
        "csk.sources.package_identity",
    }, closure
    resolution = {
        "csk.sources.selection",
        "csk.sources._selection_fs",
        "csk.sources.boundaries",
        "csk.sources.publish",
        "csk.sources.snapshot",
        "csk.sources.store",
        "csk.sources.transport",
        "csk.sources.skillfile_v2",
        "csk.sources.lock",
        "csk.sources.consumers",
        "csk.sources.diagnostics",
        "csk.sources.repository_policy",
    }
    assert closure & resolution == set()


def test_cli_launch_schema2_publishes_no_launchers(
    monkeypatch, tmp_path, csk_home, skills_root, capsys
):
    """Context-only schema-2 installs publish no executable launchers."""

    _require_posix_traversal()
    project, _ = _install_draft_review(monkeypatch, tmp_path, csk_home, skills_root)
    assert cli.main(["install", "app"]) == 0
    capsys.readouterr()
    bin_dir = shims.project_bin_dir(project)
    managed = (
        sorted(path for path in bin_dir.rglob("*") if path.is_file())
        if bin_dir.exists()
        else []
    )
    assert managed == [], managed


def test_cli_launch_installed_state_needs_no_live_inputs(
    monkeypatch, tmp_path, csk_home, skills_root, capsys
):
    """Installed context survives deletion of every live schema-2 input."""

    _require_posix_traversal()
    project, _ = _install_draft_review(monkeypatch, tmp_path, csk_home, skills_root)
    assert cli.main(["install", "app"]) == 0
    capsys.readouterr()
    installed = project / ".agents" / "skills" / "review"
    before = {
        path.relative_to(installed): path.read_bytes()
        for path in sorted(installed.rglob("*"))
        if path.is_file() and not path.is_symlink()
    }
    assert before, "the install must publish launch-readable context"

    (project / "Skillfile.json").unlink()
    (project / "Skillfile.lock.json").unlink()
    shutil.rmtree(project / "agents")

    after = {
        path.relative_to(installed): path.read_bytes()
        for path in sorted(installed.rglob("*"))
        if path.is_file() and not path.is_symlink()
    }
    assert after == before
