from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path

from conftest import make_project, make_skill_repo, run, write_skillfile
from csk import cli, config


def test_cli_version(capsys):
    code = cli.main(["--version"])
    out = capsys.readouterr().out
    assert code == 0
    assert out.startswith("csk ")


def test_cli_help_for_commands(capsys):
    assert cli.main(["--help"]) == 0
    top = capsys.readouterr().out
    assert "install" in top
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
    capsys.readouterr()

    # Existing config without --force is an error in non-interactive mode.
    assert cli.main(["bootstrap", "--non-interactive", "--skills-root", str(tmp_path / "skills")]) == 2
    assert "--force" in capsys.readouterr().err

    # Empty skills_root is rejected.
    monkeypatch.setenv("CSK_CONFIG", str(tmp_path / "cfg2" / "config.json"))
    assert cli.main(["bootstrap", "--non-interactive"]) == 2
    assert "skills_root" in capsys.readouterr().err


def test_cli_install_dry_run_does_not_create_skills_root(monkeypatch, tmp_path, csk_home, capsys):
    project = make_project(tmp_path)
    write_skillfile(project, {"schema_version": 1, "skills": []})
    missing_root = tmp_path / "missing-skills-root"
    _register_project(monkeypatch, csk_home, missing_root, project)

    assert cli.main(["install", "app", "--dry-run"]) == 0
    assert not missing_root.exists()


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
