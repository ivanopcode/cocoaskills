from __future__ import annotations

import json

from conftest import make_config, make_project, make_skill_repo, write_skillfile
from csk import installer


def test_runtime_gc_keeps_referenced_runtime_across_projects(tmp_path, skills_root, csk_home):
    project1 = make_project(tmp_path, "p1")
    project2 = make_project(tmp_path, "p2")
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
    write_skillfile(project1, {"schema_version": 1, "skills": [{"name": "skill-tool", "tag": "v1"}]})
    write_skillfile(project2, {"schema_version": 1, "skills": [{"name": "skill-tool", "tag": "v1"}]})
    cfg = make_config(csk_home, skills_root, project1)
    cfg = type(cfg)(
        path=cfg.path,
        skills_root=cfg.skills_root,
        preferred_locale=cfg.preferred_locale,
        default_agents=cfg.default_agents,
        adapter_mode=cfg.adapter_mode,
        worktree_alias_pattern=cfg.worktree_alias_pattern,
        projects={
            "p1": type(next(iter(cfg.projects.values())))(alias="p1", path=project1, agents=[]),
            "p2": type(next(iter(cfg.projects.values())))(alias="p2", path=project2, agents=[]),
        },
    )
    assert not installer.install(cfg)[0].errors
    runtime_root = csk_home / "runtime" / "skill-tool"
    assert any(runtime_root.iterdir())

    write_skillfile(project1, {"schema_version": 1, "skills": []})
    results = installer.install(cfg)
    assert all(not result.errors for result in results)
    assert any(runtime_root.iterdir())


def _two_project_config(csk_home, skills_root, project1, project2=None):
    from csk.config import GlobalConfig, ProjectConfig

    projects = {"p1": ProjectConfig(alias="p1", path=project1, agents=[])}
    if project2 is not None:
        projects["p2"] = ProjectConfig(alias="p2", path=project2, agents=[])
    return GlobalConfig(
        path=csk_home / "config.json",
        skills_root=skills_root,
        preferred_locale=None,
        default_agents=["codex_cli"],
        adapter_mode="auto",
        worktree_alias_pattern="[A-Z]+-[0-9]+",
        projects=projects,
    )


def _make_tool_repo(skills_root):
    make_skill_repo(
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


def test_runtime_gc_keeps_runtime_of_unregistered_consumer(tmp_path, skills_root, csk_home):
    project1 = make_project(tmp_path, "p1")
    project2 = make_project(tmp_path, "p2")
    _make_tool_repo(skills_root)
    write_skillfile(project1, {"schema_version": 1, "skills": []})
    write_skillfile(project2, {"schema_version": 1, "skills": [{"name": "skill-tool", "tag": "v1"}]})

    # Install p2 the way 'csk install .' does: registered only in-memory.
    ephemeral = _two_project_config(csk_home, skills_root, project1, project2)
    assert not installer.install(ephemeral, alias="p2")[0].errors
    runtime_root = csk_home / "runtime" / "skill-tool"
    assert any(runtime_root.iterdir())

    # A later install from a config that only knows p1 must not collect
    # the runtime still referenced by p2's shims.
    persisted = _two_project_config(csk_home, skills_root, project1)
    assert not installer.install(persisted)[0].errors
    assert any(runtime_root.iterdir())


def test_runtime_gc_prunes_dead_consumers(tmp_path, skills_root, csk_home):
    import shutil

    from csk import consumers

    project1 = make_project(tmp_path, "p1")
    project2 = make_project(tmp_path, "p2")
    _make_tool_repo(skills_root)
    write_skillfile(project1, {"schema_version": 1, "skills": []})
    write_skillfile(project2, {"schema_version": 1, "skills": [{"name": "skill-tool", "tag": "v1"}]})

    ephemeral = _two_project_config(csk_home, skills_root, project1, project2)
    assert not installer.install(ephemeral, alias="p2")[0].errors
    assert [path.name for path in consumers.load_consumers(csk_home)].count("p2") == 1

    shutil.rmtree(project2)
    persisted = _two_project_config(csk_home, skills_root, project1)
    assert not installer.install(persisted)[0].errors

    runtime_root = csk_home / "runtime" / "skill-tool"
    assert not runtime_root.exists() or not any(runtime_root.iterdir())
    assert all(path.name != "p2" for path in consumers.load_consumers(csk_home))


def test_runtime_gc_prunes_consumer_without_markers(tmp_path, skills_root, csk_home):
    import shutil

    from csk import consumers

    project1 = make_project(tmp_path, "p1")
    project2 = make_project(tmp_path, "p2")
    _make_tool_repo(skills_root)
    write_skillfile(project1, {"schema_version": 1, "skills": []})
    write_skillfile(project2, {"schema_version": 1, "skills": [{"name": "skill-tool", "tag": "v1"}]})

    ephemeral = _two_project_config(csk_home, skills_root, project1, project2)
    assert not installer.install(ephemeral, alias="p2")[0].errors

    shutil.rmtree(project2 / ".agents")
    persisted = _two_project_config(csk_home, skills_root, project1)
    assert not installer.install(persisted)[0].errors
    assert all(path.name != "p2" for path in consumers.load_consumers(csk_home))
