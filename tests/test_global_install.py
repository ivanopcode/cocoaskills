from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from conftest import commit_all, init_git_repo, make_config, make_project, make_skill_repo, run, write_files, write_skillfile
from csk import cli, config, global_install, installer


def _save_config(monkeypatch: pytest.MonkeyPatch, cfg: config.GlobalConfig) -> None:
    config.save_config(cfg)
    monkeypatch.setenv("CSK_CONFIG", str(cfg.path))


def _write_global_skillfile(csk_home: Path, data: dict) -> None:
    root = csk_home / "global"
    root.mkdir(parents=True, exist_ok=True)
    (root / "Skillfile.json").write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")


def test_global_init_uses_config_default_agents(monkeypatch, tmp_path, skills_root, csk_home):
    project = make_project(tmp_path)
    cfg = make_config(csk_home, skills_root, project, agents=["codex_cli", "cursor"])
    _save_config(monkeypatch, cfg)

    assert cli.main(["global", "init"]) == 0

    data = json.loads((csk_home / "global" / "Skillfile.json").read_text(encoding="utf-8"))
    assert data["agents"] == ["codex_cli", "cursor"]
    assert (csk_home / "global" / "skills").is_dir()
    assert (csk_home / "global" / "bin").is_dir()
    assert (csk_home / "global" / "env.sh").exists()
    assert (csk_home / "global" / "env.ps1").exists()


def test_global_add_remove_and_list(monkeypatch, tmp_path, skills_root, csk_home, capsys):
    project = make_project(tmp_path)
    cfg = make_config(csk_home, skills_root, project)
    _save_config(monkeypatch, cfg)

    assert cli.main(["global", "add", "skill-a", "--git", "git@example.com:skill-a.git", "--tag", "v1"]) == 0
    assert cli.main(["global", "list"]) == 0
    listed = capsys.readouterr().out
    assert "skill-a (tag v1)" in listed

    assert cli.main(["global", "remove", "skill-a"]) == 0
    assert cli.main(["global", "remove", "skill-a"]) == cli.EXIT_CONFIG
    err = capsys.readouterr().err
    assert "Global skill not declared: skill-a" in err


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX global shims use symlinks")
def test_global_install_writes_context_adapters_and_runtime_shims(monkeypatch, tmp_path, skills_root, csk_home):
    project = make_project(tmp_path)
    cfg = make_config(csk_home, skills_root, project, agents=["codex_cli", "claude_code"])
    _save_config(monkeypatch, cfg)
    _, commit = make_skill_repo(
        skills_root,
        "skill-tool",
        {
            "csk-skill.json": json.dumps(
                {
                    "schema_version": 2,
                    "runtime_roots": ["scripts"],
                    "commands": {
                        "tool": {
                            "type": "script",
                            "unix_path": "scripts/tool",
                        }
                    },
                }
            ),
            "scripts/tool": "#!/bin/sh\necho global\n",
            "scripts/helper.sh": "helper\n",
            "references/note.md": "note\n",
        },
        tag="v1",
    )
    _write_global_skillfile(
        csk_home,
        {
            "schema_version": 1,
            "agents": ["codex_cli", "claude_code"],
            "skills": [{"name": "skill-tool", "tag": "v1"}],
        },
    )

    assert cli.main(["global", "install"]) == 0

    installed = csk_home / "global" / "skills" / "skill-tool"
    marker = json.loads((installed / ".csk-install.json").read_text(encoding="utf-8"))
    assert marker["commit"] == commit
    assert marker["runtime_roots"] == ["scripts"]
    assert (installed / "references" / "note.md").exists()
    assert not (installed / "scripts").exists()
    runtime_helper = csk_home / "runtime" / "skill-tool" / commit / "scripts" / "helper.sh"
    assert runtime_helper.exists()
    shim = csk_home / "global" / "bin" / "tool"
    assert shim.is_symlink()
    assert shim.resolve() == (csk_home / "runtime" / "skill-tool" / commit / "scripts" / "tool")
    assert (Path.home() / ".codex" / "skills" / "skill-tool").exists()
    assert (Path.home() / ".claude" / "skills" / "skill-tool").exists()


def test_global_install_preserves_unmanaged_adapter_content(monkeypatch, tmp_path, skills_root, csk_home):
    project = make_project(tmp_path)
    cfg = make_config(csk_home, skills_root, project, agents=["claude_code"])
    _save_config(monkeypatch, cfg)
    make_skill_repo(skills_root, "skill-a", tag="v1")
    manual = Path.home() / ".claude" / "skills" / "manual"
    manual.mkdir(parents=True)
    (manual / "SKILL.md").write_text("manual", encoding="utf-8")
    _write_global_skillfile(
        csk_home,
        {"schema_version": 1, "agents": ["claude_code"], "skills": [{"name": "skill-a", "tag": "v1"}]},
    )

    assert cli.main(["global", "install"]) == 0

    assert (manual / "SKILL.md").read_text(encoding="utf-8") == "manual"
    assert (Path.home() / ".claude" / "skills" / ".csk-managed.json").exists()


def test_global_install_dry_run_does_not_write_anywhere(monkeypatch, tmp_path, skills_root, csk_home):
    project = make_project(tmp_path)
    cfg = make_config(csk_home, skills_root, project)
    config.save_config(cfg)
    monkeypatch.setenv("CSK_CONFIG", str(cfg.path))
    source, _ = make_skill_repo(tmp_path / "remote-skills", "skill-a", tag="v1")
    _write_global_skillfile(
        csk_home,
        {
            "schema_version": 1,
            "agents": ["codex_cli"],
            "skills": [{"name": "skill-a", "git": str(source), "tag": "v1"}],
        },
    )

    assert cli.main(["global", "install", "--dry-run"]) == 0

    assert not (skills_root / "skill-a").exists()
    assert not (csk_home / "global" / "skills").exists()
    assert not (csk_home / "global" / "bin").exists()
    assert not (csk_home / "runtime").exists()
    assert not (csk_home / "cache").exists()
    assert not (Path.home() / ".codex").exists()


def test_global_install_is_idempotent(monkeypatch, tmp_path, skills_root, csk_home, capsys):
    project = make_project(tmp_path)
    cfg = make_config(csk_home, skills_root, project)
    _save_config(monkeypatch, cfg)
    make_skill_repo(skills_root, "skill-a", tag="v1")
    _write_global_skillfile(
        csk_home,
        {"schema_version": 1, "agents": ["codex_cli"], "skills": [{"name": "skill-a", "tag": "v1"}]},
    )

    assert cli.main(["global", "install"]) == 0
    capsys.readouterr()
    assert cli.main(["global", "install"]) == 0

    out = capsys.readouterr().out
    assert "skill-a tag v1" in out
    assert "up-to-date" in out


def test_global_install_keeps_installing_available_skills_when_one_fails(
    monkeypatch, tmp_path, skills_root, csk_home, capsys
):
    project = make_project(tmp_path)
    cfg = make_config(csk_home, skills_root, project)
    _save_config(monkeypatch, cfg)
    make_skill_repo(skills_root, "skill-good", tag="v1")
    make_skill_repo(
        skills_root,
        "skill-missing-dep",
        {
            "csk-skill.json": json.dumps(
                {
                    "schema_version": 2,
                    "commands": {
                        "missing-tool": {
                            "type": "system",
                            "command": "__csk_missing_system_dependency__",
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
                {"name": "skill-missing-dep", "tag": "v1"},
            ],
        },
    )

    assert cli.main(["global", "install"]) == cli.EXIT_PARTIAL_FAIL

    captured = capsys.readouterr()
    assert "Missing system command '__csk_missing_system_dependency__'" in captured.err
    assert (csk_home / "global" / "skills" / "skill-good" / ".csk-install.json").exists()
    assert not (csk_home / "global" / "skills" / "skill-missing-dep").exists()


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX global/project shims use symlinks")
def test_project_command_shim_shadows_global_command(monkeypatch, tmp_path, skills_root, csk_home):
    project = make_project(tmp_path)
    cfg = make_config(csk_home, skills_root, project, agents=["codex_cli"])
    _save_config(monkeypatch, cfg)
    make_skill_repo(
        skills_root,
        "skill-tool",
        {
            "csk-skill.json": json.dumps(
                {
                    "schema_version": 1,
                    "commands": {"tool": {"type": "script", "unix_path": "scripts/tool"}},
                }
            ),
            "scripts/tool": "#!/bin/sh\necho global\n",
        },
        tag="global",
    )
    repo = skills_root / "skill-tool"
    write_files(repo, {"scripts/tool": "#!/bin/sh\necho local\n"})
    run(["git", "commit", "-am", "local"], repo)
    run(["git", "tag", "local"], repo)
    _write_global_skillfile(
        csk_home,
        {"schema_version": 1, "agents": ["codex_cli"], "skills": [{"name": "skill-tool", "tag": "global"}]},
    )
    write_skillfile(project, {"schema_version": 1, "agents": ["codex_cli"], "skills": [{"name": "skill-tool", "tag": "local"}]})

    assert cli.main(["global", "install"]) == 0
    assert cli.main(["install", "app"]) == 0

    global_target = (csk_home / "global" / "bin" / "tool").resolve()
    project_target = (project / ".agents" / "bin" / "tool").resolve()
    assert global_target != project_target
    assert (csk_home / "runtime" / "skill-tool").exists()
    global_env = os.environ | {"PATH": f"{csk_home / 'global' / 'bin'}:{os.environ['PATH']}"}
    global_run = subprocess.run(["tool"], check=True, text=True, capture_output=True, env=global_env)
    assert global_run.stdout.strip() == "global"
    project_env = os.environ | {
        "PATH": f"{project / '.agents' / 'bin'}:{csk_home / 'global' / 'bin'}:{os.environ['PATH']}"
    }
    project_run = subprocess.run(["tool"], check=True, text=True, capture_output=True, env=project_env)
    assert project_run.stdout.strip() == "local"


def test_global_status_reports_missing_and_up_to_date(monkeypatch, tmp_path, skills_root, csk_home, capsys):
    project = make_project(tmp_path)
    cfg = make_config(csk_home, skills_root, project)
    _save_config(monkeypatch, cfg)
    make_skill_repo(skills_root, "skill-a", tag="v1")
    _write_global_skillfile(
        csk_home,
        {"schema_version": 1, "agents": ["codex_cli"], "skills": [{"name": "skill-a", "tag": "v1"}]},
    )

    assert cli.main(["global", "status"]) == 0
    assert "missing" in capsys.readouterr().out
    assert cli.main(["global", "install"]) == 0
    assert cli.main(["global", "status"]) == 0
    assert "up-to-date" in capsys.readouterr().out


def test_global_upgrade_fetches_remote_branch_then_installs(monkeypatch, tmp_path, skills_root, csk_home):
    source = init_git_repo(tmp_path / "source-skill")
    write_files(source, {"SKILL.md": "---\nname: skill-a\n---\n\n# one\n"})
    first_commit = commit_all(source, "one")
    remote = tmp_path / "skill-a.git"
    run(["git", "init", "--bare", str(remote)], tmp_path)
    run(["git", "remote", "add", "origin", str(remote)], source)
    run(["git", "push", "-u", "origin", "main"], source)
    run(["git", "symbolic-ref", "HEAD", "refs/heads/main"], remote)
    run(["git", "clone", str(remote), str(skills_root / "skill-a")], tmp_path)

    project = make_project(tmp_path)
    cfg = make_config(csk_home, skills_root, project)
    _save_config(monkeypatch, cfg)
    _write_global_skillfile(
        csk_home,
        {"schema_version": 1, "agents": ["codex_cli"], "skills": [{"name": "skill-a", "branch": "main"}]},
    )

    assert cli.main(["global", "install"]) == 0
    marker_path = csk_home / "global" / "skills" / "skill-a" / ".csk-install.json"
    assert json.loads(marker_path.read_text(encoding="utf-8"))["commit"] == first_commit

    write_files(source, {"SKILL.md": "---\nname: skill-a\n---\n\n# two\n"})
    second_commit = commit_all(source, "two")
    run(["git", "push"], source)

    assert cli.main(["global", "install"]) == 0
    assert json.loads(marker_path.read_text(encoding="utf-8"))["commit"] == first_commit
    assert cli.main(["global", "upgrade"]) == 0
    assert json.loads(marker_path.read_text(encoding="utf-8"))["commit"] == second_commit


def test_runtime_gc_keeps_global_only_runtime(monkeypatch, tmp_path, skills_root, csk_home):
    project = make_project(tmp_path)
    write_skillfile(project, {"schema_version": 1, "skills": []})
    cfg = make_config(csk_home, skills_root, project)
    _save_config(monkeypatch, cfg)
    _, commit = make_skill_repo(
        skills_root,
        "skill-tool",
        {
            "csk-skill.json": json.dumps(
                {
                    "schema_version": 1,
                    "commands": {
                        "tool": {
                            "type": "script",
                            "unix_path": "scripts/tool",
                            "win_path": "scripts/tool.cmd",
                        }
                    },
                }
            ),
            "scripts/tool": "#!/bin/sh\n",
            "scripts/tool.cmd": "@echo off\r\n",
        },
        tag="v1",
    )
    _write_global_skillfile(
        csk_home,
        {"schema_version": 1, "agents": ["codex_cli"], "skills": [{"name": "skill-tool", "tag": "v1"}]},
    )

    assert cli.main(["global", "install"]) == 0
    assert cli.main(["install", "app"]) == 0

    assert (csk_home / "runtime" / "skill-tool" / commit).exists()


def test_global_moved_tag_warns_by_default_and_strict_tags_fail(monkeypatch, tmp_path, skills_root, csk_home):
    project = make_project(tmp_path)
    repo, _ = make_skill_repo(skills_root, "skill-a", tag="v1")
    cfg = make_config(csk_home, skills_root, project)
    _save_config(monkeypatch, cfg)
    _write_global_skillfile(
        csk_home,
        {"schema_version": 1, "agents": ["codex_cli"], "skills": [{"name": "skill-a", "tag": "v1"}]},
    )
    first = global_install.install(cfg)
    assert not first.errors

    write_files(repo, {"SKILL.md": "---\nname: changed\n---\n"})
    commit_all(repo, "move tag")
    run(["git", "tag", "-f", "v1"], repo)

    strict = global_install.install(cfg, options=installer.InstallOptions(strict_tags=True))
    assert strict.errors and "Moved tag" in strict.errors[0]
    relaxed = global_install.install(cfg)
    assert not relaxed.errors
    assert any("Moved tag" in message for message in relaxed.messages)


def test_install_verbose_reports_commands_and_full_commit(monkeypatch, tmp_path, skills_root, csk_home):
    project = make_project(tmp_path)
    _, commit = make_skill_repo(
        skills_root,
        "skill-tool",
        {
            "csk-skill.json": json.dumps(
                {
                    "schema_version": 1,
                    "commands": {
                        "tool": {"type": "script", "unix_path": "scripts/tool", "win_path": "scripts/tool.cmd"}
                    },
                }
            ),
            "scripts/tool": "#!/bin/sh\n",
            "scripts/tool.cmd": "@echo off\r\n",
        },
        tag="v1",
    )
    write_skillfile(project, {"schema_version": 1, "skills": [{"name": "skill-tool", "tag": "v1"}]})
    cfg = make_config(csk_home, skills_root, project)

    result = installer.install(cfg, options=installer.InstallOptions(verbose=True))[0]

    assert not result.errors
    assert any(f"commit {commit}" in message for message in result.messages)
    assert any("command tool -> .agents/bin/tool" in message for message in result.messages)
