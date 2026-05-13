from __future__ import annotations

import json
import sys

import pytest

from conftest import commit_all, make_config, make_project, make_skill_repo, run, write_files, write_skillfile
from csk import config, installer, snapshot


@pytest.mark.skipif(sys.platform == "win32", reason="Asserts POSIX symlink shim in .agents/bin")
def test_install_declared_script_to_runtime_not_skill_context(tmp_path, skills_root, csk_home):
    project = make_project(tmp_path)
    make_skill_repo(
        skills_root,
        "skill-tool",
        {
            "csk-skill.json": json.dumps(
                {"schema_version": 1, "commands": {"tool": {"type": "script", "unix_path": "scripts/tool"}}}
            ),
            "scripts/tool": "#!/bin/sh\necho tool\n",
            "README.md": "no\n",
            "tests/test_bad.py": "no\n",
        },
        tag="v1",
    )
    write_skillfile(project, {"schema_version": 1, "agents": ["claude_code"], "skills": [{"name": "skill-tool", "tag": "v1"}]})
    cfg = make_config(csk_home, skills_root, project, agents=["claude_code"])

    result = installer.install(cfg)[0]
    assert not result.errors
    installed = project / ".agents" / "skills" / "skill-tool"
    marker = json.loads((installed / ".csk-install.json").read_text(encoding="utf-8"))
    assert marker["commands"] == ["tool"]
    assert not (installed / "scripts" / "tool").exists()
    assert not (installed / "README.md").exists()
    runtime = csk_home / "runtime" / "skill-tool" / marker["commit"] / "bin" / "tool"
    assert runtime.exists()
    assert (project / ".agents" / "bin" / "tool").is_symlink()
    assert (project / ".claude" / "skills" / "skill-tool").exists()


def test_install_is_idempotent_for_unchanged_inputs(tmp_path, skills_root, csk_home):
    project = make_project(tmp_path)
    make_skill_repo(skills_root, "skill-a", tag="v1")
    write_skillfile(project, {"schema_version": 1, "skills": [{"name": "skill-a", "tag": "v1"}]})
    cfg = make_config(csk_home, skills_root, project)

    first = installer.install(cfg)[0]
    second = installer.install(cfg)[0]

    assert not first.errors
    assert not second.errors
    assert any("up-to-date" in message for message in second.messages)


def test_dry_run_does_not_modify_project_or_cache(tmp_path, skills_root, csk_home):
    project = make_project(tmp_path)
    make_skill_repo(skills_root, "skill-a", tag="v1")
    write_skillfile(project, {"schema_version": 1, "skills": [{"name": "skill-a", "tag": "v1"}]})
    cfg = make_config(csk_home, skills_root, project)

    result = installer.install(cfg, options=installer.InstallOptions(dry_run=True))[0]

    assert not result.errors
    assert any("dry-run" in message for message in result.messages)
    assert not (project / ".agents").exists()
    assert not (csk_home / "cache").exists()


def test_cleanup_removes_undeclared_skill_and_runtime(tmp_path, skills_root, csk_home):
    project = make_project(tmp_path)
    make_skill_repo(
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
    write_skillfile(project, {"schema_version": 1, "skills": [{"name": "skill-tool", "tag": "v1"}]})
    cfg = make_config(csk_home, skills_root, project)
    assert not installer.install(cfg)[0].errors

    write_skillfile(project, {"schema_version": 1, "skills": []})
    assert not installer.install(cfg)[0].errors
    assert not (project / ".agents" / "skills" / "skill-tool").exists()
    assert not (project / ".agents" / "bin" / "tool").exists()
    assert not (csk_home / "runtime" / "skill-tool").exists() or not any((csk_home / "runtime" / "skill-tool").iterdir())


def test_marker_schema_mismatch_fails_cleanly(tmp_path, skills_root, csk_home):
    project = make_project(tmp_path)
    make_skill_repo(skills_root, "skill-a", tag="v1")
    write_skillfile(project, {"schema_version": 1, "skills": [{"name": "skill-a", "tag": "v1"}]})
    cfg = make_config(csk_home, skills_root, project)
    assert not installer.install(cfg)[0].errors

    marker_path = project / ".agents" / "skills" / "skill-a" / ".csk-install.json"
    marker = json.loads(marker_path.read_text(encoding="utf-8"))
    marker["schema_version"] = 2
    marker_path.write_text(json.dumps(marker, indent=2) + "\n", encoding="utf-8")

    result = installer.install(cfg)[0]

    assert result.errors
    assert "Unsupported installed marker schema" in result.errors[0]


def test_snapshot_cache_reused_for_same_skill_commit_across_projects(tmp_path, skills_root, csk_home):
    project_one = make_project(tmp_path, "project-one")
    project_two = make_project(tmp_path, "project-two")
    _, commit = make_skill_repo(skills_root, "skill-a", tag="v1")
    for project in (project_one, project_two):
        write_skillfile(project, {"schema_version": 1, "skills": [{"name": "skill-a", "tag": "v1"}]})
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

    first = installer.install(cfg, alias="one")[0]
    snap = snapshot.snapshot_dir(csk_home, "skill-a", commit)
    assert not first.errors
    assert snap.exists()
    before = snap.stat().st_mtime_ns

    second = installer.install(cfg, alias="two")[0]

    assert not second.errors
    assert snap.stat().st_mtime_ns == before
    assert (project_two / ".agents" / "skills" / "skill-a").exists()


def test_moved_tag_warns_by_default_and_strict_tags_fail(tmp_path, skills_root, csk_home):
    project = make_project(tmp_path)
    repo, _ = make_skill_repo(skills_root, "skill-a", tag="v1")
    write_skillfile(project, {"schema_version": 1, "skills": [{"name": "skill-a", "tag": "v1"}]})
    cfg = make_config(csk_home, skills_root, project)
    assert not installer.install(cfg)[0].errors

    write_files(repo, {"SKILL.md": "---\nname: changed\n---\n"})
    commit_all(repo, "move tag")
    run(["git", "tag", "-f", "v1"], repo)

    strict = installer.install(cfg, options=installer.InstallOptions(strict_tags=True))[0]
    assert strict.errors
    relaxed = installer.install(cfg)[0]
    assert not relaxed.errors
    assert any("Moved tag" in message for message in relaxed.messages)


def test_failed_update_preserves_previous_install(tmp_path, skills_root, csk_home):
    project = make_project(tmp_path)
    repo, _ = make_skill_repo(skills_root, "skill-a")
    write_skillfile(project, {"schema_version": 1, "skills": [{"name": "skill-a", "branch": "main"}]})
    cfg = make_config(csk_home, skills_root, project)
    assert not installer.install(cfg)[0].errors
    installed = project / ".agents" / "skills" / "skill-a" / "SKILL.md"
    assert installed.exists()

    (repo / "SKILL.md").unlink()
    commit_all(repo, "remove skill")
    failed = installer.install(cfg)[0]
    assert failed.errors
    assert installed.exists()


def test_gitignore_gate_skips_project_without_failure(tmp_path, skills_root, csk_home):
    project = make_project(tmp_path, gitignore=False)
    make_skill_repo(skills_root, "skill-a", tag="v1")
    write_skillfile(project, {"schema_version": 1, "skills": [{"name": "skill-a", "tag": "v1"}]})
    cfg = make_config(csk_home, skills_root, project)

    result = installer.install(cfg)[0]

    assert result.status == "skipped"
    assert not result.errors
    assert not (project / ".agents").exists()


def test_gitignore_fix_allows_install(tmp_path, skills_root, csk_home):
    project = make_project(tmp_path, gitignore=False)
    make_skill_repo(skills_root, "skill-a", tag="v1")
    write_skillfile(project, {"schema_version": 1, "skills": [{"name": "skill-a", "tag": "v1"}]})
    cfg = make_config(csk_home, skills_root, project)

    result = installer.install(cfg, options=installer.InstallOptions(fix_gitignore=True))[0]

    assert not result.errors
    assert (project / ".agents" / "skills" / "skill-a").exists()
    assert ".agents/" in (project / ".gitignore").read_text(encoding="utf-8")


def test_gitmodules_rejected(tmp_path, skills_root, csk_home):
    project = make_project(tmp_path)
    make_skill_repo(skills_root, "skill-a", {".gitmodules": "[submodule]\n"}, tag="v1")
    write_skillfile(project, {"schema_version": 1, "skills": [{"name": "skill-a", "tag": "v1"}]})
    cfg = make_config(csk_home, skills_root, project)

    result = installer.install(cfg)[0]

    assert result.errors
    assert "Submodules" in result.errors[0]


def test_system_command_missing_fails(tmp_path, skills_root, csk_home):
    project = make_project(tmp_path)
    make_skill_repo(
        skills_root,
        "skill-system",
        {
            "csk-skill.json": json.dumps(
                {
                    "schema_version": 1,
                    "commands": {
                        "missing": {
                            "type": "system",
                            "command": "definitely-missing-csk-test-command",
                        }
                    },
                }
            )
        },
        tag="v1",
    )
    write_skillfile(project, {"schema_version": 1, "skills": [{"name": "skill-system", "tag": "v1"}]})
    cfg = make_config(csk_home, skills_root, project)

    result = installer.install(cfg)[0]

    assert result.errors
    assert "Missing system command" in result.errors[0]


def test_command_collision_fails(tmp_path, skills_root, csk_home):
    project = make_project(tmp_path)
    for name in ("skill-one", "skill-two"):
        make_skill_repo(
            skills_root,
            name,
            {
                "csk-skill.json": json.dumps(
                    {"schema_version": 1, "commands": {"tool": {"type": "script", "unix_path": "scripts/tool"}}}
                ),
                "scripts/tool": "#!/bin/sh\n",
            },
            tag="v1",
        )
    write_skillfile(
        project,
        {
            "schema_version": 1,
            "skills": [{"name": "skill-one", "tag": "v1"}, {"name": "skill-two", "tag": "v1"}],
        },
    )
    cfg = make_config(csk_home, skills_root, project)

    result = installer.install(cfg)[0]

    assert result.errors
    assert "Command collision" in result.errors[0]


def test_missing_skillfile_warns_and_skips(tmp_path, skills_root, csk_home):
    project = make_project(tmp_path)
    cfg = make_config(csk_home, skills_root, project)

    result = installer.install(cfg)[0]

    assert result.status == "skipped"
    assert not result.errors
    assert "Skillfile.json not found" in result.messages[0]


def test_agents_change_updates_marker(tmp_path, skills_root, csk_home):
    project = make_project(tmp_path)
    make_skill_repo(skills_root, "skill-a", tag="v1")
    write_skillfile(project, {"schema_version": 1, "agents": ["codex_cli"], "skills": [{"name": "skill-a", "tag": "v1"}]})
    cfg = make_config(csk_home, skills_root, project)
    assert not installer.install(cfg)[0].errors

    write_skillfile(project, {"schema_version": 1, "agents": ["claude_code"], "skills": [{"name": "skill-a", "tag": "v1"}]})
    assert not installer.install(cfg)[0].errors
    marker = json.loads((project / ".agents" / "skills" / "skill-a" / ".csk-install.json").read_text(encoding="utf-8"))
    assert marker["agents"] == ["claude_code"]
