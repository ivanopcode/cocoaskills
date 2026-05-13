from __future__ import annotations

import json

from conftest import commit_all, init_git_repo, make_project, make_skill_repo, run, write_files, write_skillfile
from csk import cli, config


def test_full_install_then_upgrade_advances_branch(monkeypatch, tmp_path, skills_root, csk_home):
    project = make_project(tmp_path)
    repo, first_commit = make_skill_repo(skills_root, "skill-a")
    write_skillfile(project, {"schema_version": 1, "skills": [{"name": "skill-a", "branch": "main"}]})
    cfg = config.GlobalConfig(
        path=csk_home / "config.json",
        skills_root=skills_root,
        preferred_locale=None,
        default_agents=["codex_cli"],
        adapter_mode="auto",
        projects={"app": config.ProjectConfig(alias="app", path=project, agents=["codex_cli"])},
    )
    config.save_config(cfg)
    monkeypatch.setenv("CSK_CONFIG", str(cfg.path))

    assert cli.main(["install", "app"]) == 0
    marker_path = project / ".agents" / "skills" / "skill-a" / ".csk-install.json"
    first_marker = json.loads(marker_path.read_text(encoding="utf-8"))
    assert first_marker["commit"] == first_commit

    write_files(repo, {"SKILL.md": "---\nname: skill-a\n---\n\n# changed\n"})
    second_commit = commit_all(repo, "change")

    assert cli.main(["install", "app"]) == 0
    second_marker = json.loads(marker_path.read_text(encoding="utf-8"))
    assert second_marker["commit"] == second_commit


def test_upgrade_fetches_remote_branch_then_installs(monkeypatch, tmp_path, skills_root, csk_home):
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
    write_skillfile(project, {"schema_version": 1, "skills": [{"name": "skill-a", "branch": "main"}]})
    cfg = config.GlobalConfig(
        path=csk_home / "config.json",
        skills_root=skills_root,
        preferred_locale=None,
        default_agents=["codex_cli"],
        adapter_mode="auto",
        projects={"app": config.ProjectConfig(alias="app", path=project, agents=["codex_cli"])},
    )
    config.save_config(cfg)
    monkeypatch.setenv("CSK_CONFIG", str(cfg.path))

    assert cli.main(["install", "app"]) == 0
    marker_path = project / ".agents" / "skills" / "skill-a" / ".csk-install.json"
    assert json.loads(marker_path.read_text(encoding="utf-8"))["commit"] == first_commit

    write_files(source, {"SKILL.md": "---\nname: skill-a\n---\n\n# two\n"})
    second_commit = commit_all(source, "two")
    run(["git", "push"], source)

    assert cli.main(["install", "app"]) == 0
    assert json.loads(marker_path.read_text(encoding="utf-8"))["commit"] == first_commit
    assert cli.main(["upgrade", "app"]) == 0
    assert json.loads(marker_path.read_text(encoding="utf-8"))["commit"] == second_commit


def test_upgrade_without_alias_updates_all_projects(monkeypatch, tmp_path, skills_root, csk_home):
    source = init_git_repo(tmp_path / "source-skill")
    write_files(source, {"SKILL.md": "---\nname: skill-a\n---\n\n# one\n"})
    first_commit = commit_all(source, "one")
    remote = tmp_path / "skill-a.git"
    run(["git", "init", "--bare", str(remote)], tmp_path)
    run(["git", "remote", "add", "origin", str(remote)], source)
    run(["git", "push", "-u", "origin", "main"], source)
    run(["git", "symbolic-ref", "HEAD", "refs/heads/main"], remote)
    run(["git", "clone", str(remote), str(skills_root / "skill-a")], tmp_path)

    project_one = make_project(tmp_path, "project-one")
    project_two = make_project(tmp_path, "project-two")
    for project in (project_one, project_two):
        write_skillfile(project, {"schema_version": 1, "skills": [{"name": "skill-a", "branch": "main"}]})
    cfg = config.GlobalConfig(
        path=csk_home / "config.json",
        skills_root=skills_root,
        preferred_locale=None,
        default_agents=["codex_cli"],
        adapter_mode="auto",
        projects={
            "one": config.ProjectConfig(alias="one", path=project_one, agents=["codex_cli"]),
            "two": config.ProjectConfig(alias="two", path=project_two, agents=["codex_cli"]),
        },
    )
    config.save_config(cfg)
    monkeypatch.setenv("CSK_CONFIG", str(cfg.path))

    assert cli.main(["install"]) == 0
    markers = [
        project_one / ".agents" / "skills" / "skill-a" / ".csk-install.json",
        project_two / ".agents" / "skills" / "skill-a" / ".csk-install.json",
    ]
    assert [json.loads(path.read_text(encoding="utf-8"))["commit"] for path in markers] == [first_commit, first_commit]

    write_files(source, {"SKILL.md": "---\nname: skill-a\n---\n\n# two\n"})
    second_commit = commit_all(source, "two")
    run(["git", "push"], source)

    assert cli.main(["upgrade"]) == 0

    assert [json.loads(path.read_text(encoding="utf-8"))["commit"] for path in markers] == [second_commit, second_commit]
