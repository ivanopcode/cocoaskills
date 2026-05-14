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


def test_cli_install_dot_registers_current_checkout_alias(monkeypatch, tmp_path, csk_home, skills_root):
    make_skill_repo(skills_root, "skill-a", tag="v1")
    project = make_project(tmp_path)
    write_skillfile(
        project,
        {
            "schema_version": 1,
            "project": {"alias": "partners-ios"},
            "agents": ["codex_cli"],
            "skills": [{"name": "skill-a", "tag": "v1"}],
        },
    )
    run(["git", "checkout", "-b", "feature/PMA-23523-install"], project)
    cfg_path = csk_home / "config.json"
    cfg_path.write_text(
        json.dumps({"schema_version": 1, "skills_root": str(skills_root), "projects": {}}),
        encoding="utf-8",
    )
    monkeypatch.setenv("CSK_CONFIG", str(cfg_path))
    monkeypatch.chdir(project)

    code = cli.main(["install", "."])

    assert code == 0
    loaded = config.load_config(cfg_path)
    checkout_alias = next(iter(loaded.projects))
    assert checkout_alias.startswith("partners-ios-pma-23523-")
    assert loaded.projects[checkout_alias].path == project
    assert loaded.projects[checkout_alias].project_alias == "partners-ios"
    assert (project / ".agents" / "skills" / "skill-a" / "SKILL.md").exists()


def test_cli_install_tilde_path_registers_checkout(monkeypatch, tmp_path, csk_home, skills_root):
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

    assert code == 0
    loaded = config.load_config(cfg_path)
    assert loaded.projects["home-app"].path == project
    assert (project / ".agents" / "skills" / "skill-a" / "SKILL.md").exists()


def test_cli_status_dot_uses_current_checkout_without_saving_config(monkeypatch, tmp_path, csk_home, skills_root, capsys):
    make_skill_repo(skills_root, "skill-a", tag="v1")
    project = make_project(tmp_path)
    write_skillfile(
        project,
        {
            "schema_version": 1,
            "project": {"alias": "partners-ios"},
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
    assert "Project partners-ios" in out
    assert "missing" in out
    assert config.load_config(cfg_path).projects == {}


def test_cli_install_dot_dry_run_does_not_save_config(monkeypatch, tmp_path, csk_home, skills_root):
    make_skill_repo(skills_root, "skill-a", tag="v1")
    project = make_project(tmp_path)
    write_skillfile(
        project,
        {
            "schema_version": 1,
            "project": {"alias": "partners-ios"},
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


def test_cli_project_resolve_reports_current_checkout(monkeypatch, tmp_path, csk_home, skills_root, capsys):
    project = make_project(tmp_path)
    write_skillfile(project, {"schema_version": 1, "project": {"alias": "partners-ios"}, "skills": []})
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
    assert "project_alias: partners-ios" in out
    assert "checkout_alias: partners-ios" in out
    assert f"skillfile: {project / 'Skillfile.json'}" in out


def test_cli_project_resolve_configured_alias_reports_git_fields(monkeypatch, tmp_path, csk_home, skills_root, capsys):
    project = make_project(tmp_path)
    write_skillfile(project, {"schema_version": 1, "project": {"alias": "partners-ios"}, "skills": []})
    run(["git", "checkout", "-b", "feature/PMA-23523-resolve"], project)
    cfg_path = csk_home / "config.json"
    cfg_path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "skills_root": str(skills_root),
                "projects": {
                    "partners-ios-pma-23523-test": {
                        "path": str(project),
                        "agents": ["codex_cli"],
                        "project_alias": "partners-ios",
                        "checkout_alias": "partners-ios-pma-23523-test",
                    }
                },
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("CSK_CONFIG", str(cfg_path))

    code = cli.main(["project", "resolve", "partners-ios-pma-23523-test"])
    out = capsys.readouterr().out

    assert code == 0
    assert "branch: feature/PMA-23523-resolve" in out
    assert "task_id: pma-23523" in out
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


def test_cli_list_paths_shows_alias_layers(monkeypatch, tmp_path, csk_home, skills_root, capsys):
    project = make_project(tmp_path)
    cfg_path = csk_home / "config.json"
    cfg_path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "skills_root": str(skills_root),
                "projects": {
                    "partners-ios-pma-23523": {
                        "path": str(project),
                        "agents": ["codex_cli"],
                        "project_alias": "partners-ios",
                        "checkout_alias": "partners-ios-pma-23523",
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
    assert "project_alias=partners-ios" in out
    assert "checkout_alias=partners-ios-pma-23523" in out
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
    lock_path.write_text(json.dumps({"pid": 12345, "created_at": time.time()}), encoding="utf-8")
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
