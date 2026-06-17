from __future__ import annotations

import json
import os
import shutil
import stat

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


def _rmtree(path):
    def onerror(func, failing_path, _exc_info):  # noqa: ANN001
        os.chmod(failing_path, stat.S_IWRITE)
        func(failing_path)

    shutil.rmtree(path, onerror=onerror)


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
    from csk import consumers

    project1 = make_project(tmp_path, "p1")
    project2 = make_project(tmp_path, "p2")
    _make_tool_repo(skills_root)
    write_skillfile(project1, {"schema_version": 1, "skills": []})
    write_skillfile(project2, {"schema_version": 1, "skills": [{"name": "skill-tool", "tag": "v1"}]})

    ephemeral = _two_project_config(csk_home, skills_root, project1, project2)
    assert not installer.install(ephemeral, alias="p2")[0].errors
    assert [path.name for path in consumers.load_consumers(csk_home)].count("p2") == 1

    _rmtree(project2)
    persisted = _two_project_config(csk_home, skills_root, project1)
    assert not installer.install(persisted)[0].errors

    runtime_root = csk_home / "runtime" / "skill-tool"
    assert not runtime_root.exists() or not any(runtime_root.iterdir())
    assert all(path.name != "p2" for path in consumers.load_consumers(csk_home))


def test_runtime_gc_prunes_consumer_without_markers(tmp_path, skills_root, csk_home):
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


def test_gc_sweeps_orphan_tmp_dirs_of_dead_processes(tmp_path, skills_root, csk_home):
    import os
    import subprocess
    import sys

    project1 = make_project(tmp_path, "p1")
    _make_tool_repo(skills_root)
    write_skillfile(project1, {"schema_version": 1, "skills": [{"name": "skill-tool", "tag": "v1"}]})
    cfg = _two_project_config(csk_home, skills_root, project1)
    assert not installer.install(cfg)[0].errors

    proc = subprocess.run([sys.executable, "-c", "import os; print(os.getpid())"], capture_output=True, text=True)
    dead_pid = int(proc.stdout.strip())
    skills_dir = project1 / ".agents" / "skills"
    dead_tmp = skills_dir / f".skill-tool.tmp-{dead_pid}"
    dead_backup = skills_dir / f".skill-tool.backup-{dead_pid}"
    live_tmp = skills_dir / f".skill-tool.tmp-{os.getpid()}"
    for orphan in (dead_tmp, dead_backup, live_tmp):
        orphan.mkdir()
        (orphan / "junk.txt").write_text("x", encoding="utf-8")
    runtime_skill_dir = csk_home / "runtime" / "skill-tool"
    runtime_orphan = runtime_skill_dir / f".deadbeef.tmp-{dead_pid}"
    runtime_orphan.mkdir(parents=True)

    assert not installer.install(cfg)[0].errors

    assert not dead_tmp.exists()
    assert not dead_backup.exists()
    assert not runtime_orphan.exists()
    assert live_tmp.exists()  # owner is alive; not ours to delete
    assert (skills_dir / "skill-tool").exists()


def test_snapshot_cache_gc_removes_unreferenced_keeps_referenced(tmp_path, skills_root, csk_home):
    from csk import gc as gc_mod

    project1 = make_project(tmp_path, "p1")
    _make_tool_repo(skills_root)
    write_skillfile(project1, {"schema_version": 1, "skills": [{"name": "skill-tool", "tag": "v1"}]})
    cfg = _two_project_config(csk_home, skills_root, project1)
    assert not installer.install(cfg)[0].errors

    referenced = list((csk_home / "cache" / "skill-tool").iterdir())
    assert referenced  # install populated the snapshot cache

    stale = csk_home / "cache" / "skill-tool" / ("0" * 40) / "snapshot"
    stale.mkdir(parents=True)
    nested_stale = csk_home / "cache" / "internal" / "skill-x" / ("1" * 40) / "snapshot"
    nested_stale.mkdir(parents=True)

    stats = gc_mod.collect_runtime(cfg, csk_home)

    assert stats.snapshots_removed == 2
    assert not stale.parent.exists()
    assert not (csk_home / "cache" / "internal").exists()  # empty parents removed
    assert all(path.exists() for path in referenced)


def test_cli_gc_command_reports_summary(monkeypatch, tmp_path, skills_root, csk_home, capsys):
    import json as json_mod

    from csk import cli

    project1 = make_project(tmp_path, "p1")
    write_skillfile(project1, {"schema_version": 1, "skills": []})
    cfg_path = csk_home / "config.json"
    cfg_path.write_text(
        json_mod.dumps(
            {
                "schema_version": 1,
                "skills_root": str(skills_root),
                "projects": {"p1": {"path": str(project1), "agents": ["codex_cli"]}},
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("CSK_CONFIG", str(cfg_path))

    assert cli.main(["gc"]) == 0
    out = capsys.readouterr().out
    assert "gc: removed" in out
