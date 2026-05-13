from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path

from conftest import make_project, make_skill_repo, write_skillfile
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
