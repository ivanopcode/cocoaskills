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
