from __future__ import annotations

import json
import os
import shutil
import stat

from conftest import make_config, make_project, make_skill_repo, write_skillfile

from csk import gc, installer


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
    def onerror(func, failing_path, _exc_info):
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

    proc = subprocess.run(
        [sys.executable, "-c", "import os; print(os.getpid())"],
        check=True,
        capture_output=True,
        text=True,
    )
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


def test_sweep_orphans_matches_legacy_and_indexed_runtime_names_only(
    monkeypatch,
    tmp_path,
):
    dead_pid = 424242
    live_pid = 434343
    directory = tmp_path / "orphans"
    directory.mkdir()
    dead_names = {
        f".tool.tmp-{dead_pid}",
        f".tool.backup-{dead_pid}",
        f".tool.tmp-{dead_pid}-0",
        f".tool.backup-{dead_pid}-19",
        f".commit.stale-{dead_pid}-7",
    }
    live_names = {
        f".tool.tmp-{live_pid}-0",
        f".tool.backup-{live_pid}",
        f".commit.stale-{live_pid}-2",
    }
    unrelated_names = {
        f".tool.tmp-{dead_pid}-suffix",
        f".tool.stale-{dead_pid}-01",
        f".tool.stale-{dead_pid}",
        f".tool.other-{dead_pid}-0",
        f"tool.tmp-{dead_pid}-0",
    }
    for name in dead_names | live_names | unrelated_names:
        (directory / name).mkdir()
    monkeypatch.setattr(gc, "_pid_alive", lambda pid: pid == live_pid)

    gc.sweep_orphans(directory)

    assert all(not (directory / name).exists() for name in dead_names)
    assert all((directory / name).exists() for name in live_names)
    assert all((directory / name).exists() for name in unrelated_names)


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


def test_successful_install_runs_snapshot_and_remaining_orphan_gc(
    tmp_path, skills_root, csk_home
):
    import subprocess
    import sys

    project = make_project(tmp_path)
    make_skill_repo(skills_root, "skill-a", tag="v1")
    write_skillfile(
        project,
        {
            "schema_version": 1,
            "skills": [{"name": "skill-a", "tag": "v1"}],
        },
    )
    cfg = make_config(csk_home, skills_root, project)
    assert not installer.install(cfg)[0].errors

    stale_snapshot = (
        csk_home / "cache" / "stale-source" / ("0" * 40) / "snapshot"
    )
    stale_snapshot.mkdir(parents=True)
    process = subprocess.run(
        [sys.executable, "-c", "import os; print(os.getpid())"],
        check=True,
        capture_output=True,
        text=True,
    )
    dead_pid = int(process.stdout.strip())
    global_orphan = (
        csk_home / "global" / "skills" / f".skill.tmp-{dead_pid}"
    )
    runtime_orphan = (
        csk_home
        / "runtime"
        / "skill-a"
        / f".runtime.tmp-{dead_pid}"
    )
    global_orphan.mkdir(parents=True)
    runtime_orphan.mkdir(parents=True)

    result = installer.install(cfg)[0]

    assert not result.errors
    assert not stale_snapshot.parent.exists()
    assert not global_orphan.exists()
    assert not runtime_orphan.exists()


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


def test_collect_snapshots_preserves_live_snapshot_containing_snapshot_dirs(
    tmp_path, skills_root, csk_home, monkeypatch, capsys
):
    # A live snapshot whose tree contains top-level snapshot/ and nested foo/snapshot/ survives _collect_snapshots, post-install GC, and public `csk gc`, while an unreferenced snapshot beside it is collected.
    from csk import cli

    # Function-level regression witness + positive control.
    live_snap = csk_home / "cache" / "src" / "c1" / "snapshot"
    (live_snap / "snapshot").mkdir(parents=True)
    (live_snap / "snapshot" / "a.txt").write_text("top-level", encoding="utf-8")
    (live_snap / "foo" / "snapshot").mkdir(parents=True)
    (live_snap / "foo" / "snapshot" / "b.txt").write_text("nested", encoding="utf-8")
    (live_snap / "SKILL.md").write_text("---\nname: src\n---\n", encoding="utf-8")

    stale_snap = csk_home / "cache" / "src" / "c2" / "snapshot"
    stale_snap.mkdir(parents=True)
    (stale_snap / "SKILL.md").write_text("---\nname: src\n---\n", encoding="utf-8")

    warnings: list[str] = []
    removed = gc._collect_snapshots(csk_home, {("src", "c1")}, warnings=warnings)
    assert removed == 1
    assert warnings == []
    assert (live_snap / "SKILL.md").is_file()
    assert (live_snap / "snapshot" / "a.txt").is_file()
    assert (live_snap / "foo" / "snapshot" / "b.txt").is_file()
    assert not stale_snap.parent.exists()

    # End-to-end witness through installer (post-install GC) and public `csk gc`.
    project = make_project(tmp_path, "snap-proj")
    make_skill_repo(
        skills_root,
        "skill-snap",
        {
            "csk-skill.json": json.dumps(
                {
                    "schema_version": 1,
                    "commands": {
                        "snaptool": {
                            "type": "script",
                            "unix_path": "scripts/tool",
                            "win_path": "scripts/tool.cmd",
                        }
                    },
                }
            ),
            "scripts/tool": "#!/bin/sh\n",
            "scripts/tool.cmd": "@echo off\r\n",
            "snapshot/data.txt": "inside top-level snapshot dir\n",
            "foo/snapshot/deep.txt": "inside nested snapshot dir\n",
        },
        tag="v1",
    )
    write_skillfile(project, {"schema_version": 1, "skills": [{"name": "skill-snap", "tag": "v1"}]})
    cfg_path = csk_home / "config.json"
    cfg_path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "skills_root": str(skills_root),
                "projects": {"snap-proj": {"path": str(project), "agents": ["codex_cli"]}},
            }
        ),
        encoding="utf-8",
    )
    cfg = make_config(csk_home, skills_root, project)
    assert not installer.install(cfg)[0].errors

    live_commits = list((csk_home / "cache" / "skill-snap").iterdir())
    assert len(live_commits) == 1
    installed_snap = live_commits[0] / "snapshot"
    assert (installed_snap / "snapshot" / "data.txt").is_file()
    assert (installed_snap / "foo" / "snapshot" / "deep.txt").is_file()

    extra_stale = csk_home / "cache" / "skill-snap" / ("0" * 40) / "snapshot"
    extra_stale.mkdir(parents=True)

    monkeypatch.setenv("CSK_CONFIG", str(cfg_path))
    assert cli.main(["gc"]) == 0
    _ = capsys.readouterr()
    assert (installed_snap / "SKILL.md").is_file()
    assert (installed_snap / "snapshot" / "data.txt").is_file()
    assert (installed_snap / "foo" / "snapshot" / "deep.txt").is_file()
    assert not extra_stale.parent.exists()


def test_collect_snapshots_retains_shallow_snapshot_dirs_with_warnings(
    tmp_path, skills_root, csk_home
):
    # Shallow `cache/snapshot` and `cache/<x>/snapshot` entries do not raise ValueError, are retained with warnings in stats.warnings, and do not block collecting valid unreferenced snapshots.
    top_level = csk_home / "cache" / "snapshot"
    top_level.mkdir(parents=True)
    (top_level / "keep.txt").write_text("top", encoding="utf-8")

    one_below = csk_home / "cache" / "onelevel" / "snapshot"
    one_below.mkdir(parents=True)
    (one_below / "keep.txt").write_text("one", encoding="utf-8")

    stale = csk_home / "cache" / "valid-source" / "deadbeef" / "snapshot"
    stale.mkdir(parents=True)

    project = make_project(tmp_path, "shallow-proj")
    write_skillfile(project, {"schema_version": 1, "skills": []})
    cfg = _two_project_config(csk_home, skills_root, project)

    stats = gc.collect_runtime(cfg, csk_home)

    assert stats.snapshots_removed == 1
    assert not stale.parent.exists()
    assert (top_level / "keep.txt").is_file()
    assert (one_below / "keep.txt").is_file()
    assert any(str(top_level) in w for w in stats.warnings)
    assert any(str(one_below) in w for w in stats.warnings)


def test_collect_snapshots_retains_sources_with_snapshot_path_segment_with_warning(
    tmp_path, skills_root, csk_home
):
    # Sources whose path contains a `snapshot` segment (including host/snapshot/repo and deeper org/team/snapshot/repo) are retained with warnings, live sibling sources under the same host/org survive, and unreferenced sibling/normal snapshots are still collected.
    shallow_source = csk_home / "cache" / "snapshot" / "skill-a" / "c1" / "snapshot"
    shallow_source.mkdir(parents=True)
    (shallow_source / "SKILL.md").write_text("a", encoding="utf-8")

    host_snapshot_repo = csk_home / "cache" / "host" / "snapshot" / "repo" / "c2" / "snapshot"
    host_snapshot_repo.mkdir(parents=True)
    (host_snapshot_repo / "SKILL.md").write_text("b", encoding="utf-8")
    host_sibling_live = csk_home / "cache" / "host" / "other-repo" / "c2live" / "snapshot"
    host_sibling_live.mkdir(parents=True)
    (host_sibling_live / "SKILL.md").write_text("live-sibling", encoding="utf-8")
    host_sibling_stale = csk_home / "cache" / "host" / "stale-repo" / "c2stale" / "snapshot"
    host_sibling_stale.mkdir(parents=True)

    deep_live = csk_home / "cache" / "org" / "team" / "snapshot" / "c3" / "snapshot"
    deep_live.mkdir(parents=True)
    (deep_live / "SKILL.md").write_text("c", encoding="utf-8")

    deep_with_sibling = csk_home / "cache" / "corp" / "eng" / "snapshot" / "repo" / "c4" / "snapshot"
    deep_with_sibling.mkdir(parents=True)
    (deep_with_sibling / "SKILL.md").write_text("d", encoding="utf-8")
    deep_sibling_live = csk_home / "cache" / "corp" / "eng" / "other-repo" / "c4live" / "snapshot"
    deep_sibling_live.mkdir(parents=True)
    (deep_sibling_live / "SKILL.md").write_text("corp-live-sibling", encoding="utf-8")
    deep_sibling_stale = csk_home / "cache" / "corp" / "eng" / "stale-repo" / "c4stale" / "snapshot"
    deep_sibling_stale.mkdir(parents=True)

    stale_normal = csk_home / "cache" / "normal-src" / "c5" / "snapshot"
    stale_normal.mkdir(parents=True)

    warnings: list[str] = []
    removed = gc._collect_snapshots(
        csk_home,
        {
            ("host/other-repo", "c2live"),
            ("org/team/snapshot", "c3"),
            ("corp/eng/other-repo", "c4live"),
        },
        warnings=warnings,
    )

    assert removed == 3
    assert not host_sibling_stale.parent.exists()
    assert not deep_sibling_stale.parent.exists()
    assert not stale_normal.parent.exists()
    assert (shallow_source / "SKILL.md").is_file()
    assert (host_snapshot_repo / "SKILL.md").is_file()
    assert (host_sibling_live / "SKILL.md").is_file()
    assert (deep_live / "SKILL.md").is_file()
    assert (deep_with_sibling / "SKILL.md").is_file()
    assert (deep_sibling_live / "SKILL.md").is_file()
    assert len(warnings) == 4


def test_collect_snapshots_never_touches_or_prunes_registry_cache(csk_home):
    # The audit registry cache under cache/registry is skipped by snapshot enumeration and never deleted or pruned, even when it holds nested directories named snapshot, while a stale snapshot beside it is removed.
    registry_dir = csk_home / "cache" / "registry"
    (registry_dir / "nested" / "c1" / "snapshot").mkdir(parents=True)
    (registry_dir / "nested" / "c1" / "snapshot" / "data.json").write_text("{}", encoding="utf-8")
    (registry_dir / "records-abc.json").write_text("{}", encoding="utf-8")

    stale = csk_home / "cache" / "stale-src" / "c2" / "snapshot"
    stale.mkdir(parents=True)

    warnings: list[str] = []
    removed = gc._collect_snapshots(csk_home, set(), warnings=warnings)

    assert removed == 1
    assert warnings == []
    assert not stale.parent.exists()
    assert (registry_dir / "records-abc.json").is_file()
    assert (registry_dir / "nested" / "c1" / "snapshot" / "data.json").is_file()


def test_validate_legacy_runtime_ignores_nested_snapshot_content_decoy(tmp_path, csk_home):
    # Legacy runtime validation enumerates only top-level cache/<source>/<commit>/snapshot entries and never adopts a matching <commit>/snapshot directory nested inside another snapshot's content.
    from csk import published_trees

    manifest = json.dumps(
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
    )
    bin_name = "tool.cmd" if os.name == "nt" else "tool"

    # Create a decoy <commit>/snapshot nested inside another snapshot's tree.
    other_snap = csk_home / "cache" / "other" / "c_other" / "snapshot"
    decoy = other_snap / "c_target" / "snapshot"
    (decoy / "scripts").mkdir(parents=True)
    (decoy / "csk-skill.json").write_text(manifest, encoding="utf-8")
    (decoy / "scripts" / "tool").write_text("#!/bin/sh\n", encoding="utf-8")
    (decoy / "scripts" / "tool.cmd").write_text("@echo off\r\n", encoding="utf-8")

    runtime_entry = csk_home / "runtime" / "my-skill" / "c_target"
    (runtime_entry / "bin").mkdir(parents=True)
    (runtime_entry / "bin" / bin_name).write_text("#!/bin/sh\n", encoding="utf-8")

    # Negative case: with only the nested decoy present, validation must report missing inventory.
    try:
        published_trees.validate_legacy_runtime(runtime_entry, home=csk_home)
    except published_trees.RuntimeInventoryUnavailable:
        pass
    else:
        raise AssertionError("expected RuntimeInventoryUnavailable for nested decoy")

    # Positive control: adding the real cache/<source>/<commit>/snapshot allows validation to succeed.
    real_snap = csk_home / "cache" / "my-skill" / "c_target" / "snapshot"
    (real_snap / "scripts").mkdir(parents=True)
    (real_snap / "csk-skill.json").write_text(manifest, encoding="utf-8")
    (real_snap / "scripts" / "tool").write_text("#!/bin/sh\n", encoding="utf-8")
    (real_snap / "scripts" / "tool.cmd").write_text("@echo off\r\n", encoding="utf-8")
    published_trees.validate_legacy_runtime(runtime_entry, home=csk_home)


def test_collect_snapshots_retains_symlinked_snapshot_and_warns(tmp_path, csk_home):
    # A symlinked snapshot entry is retained with a warning and its target outside cache is untouched, while a real unreferenced snapshot beside it is removed.
    outside = tmp_path / "outside-target"
    outside.mkdir()
    marker = outside / "important.txt"
    marker.write_text("keep", encoding="utf-8")

    linked_commit = csk_home / "cache" / "linked-src" / "c1"
    linked_commit.mkdir(parents=True)
    (linked_commit / "snapshot").symlink_to(outside)

    real_stale = csk_home / "cache" / "real-src" / "c2" / "snapshot"
    real_stale.mkdir(parents=True)

    warnings: list[str] = []
    removed = gc._collect_snapshots(csk_home, set(), warnings=warnings)

    assert removed == 1
    assert not real_stale.parent.exists()
    assert marker.read_text(encoding="utf-8") == "keep"
    assert (linked_commit / "snapshot").is_symlink()
    assert any("not a real directory" in w for w in warnings)


def test_collect_snapshots_does_not_descend_into_staging_tmp_dirs(csk_home):
    # Interrupted `.snapshot-<commit>.tmp` staging directories containing a nested `snapshot/` tree are never descended into or treated as snapshot candidates, while a real unreferenced snapshot beside them is removed.
    orphan_tmp = csk_home / "cache" / "src" / "c1" / ".snapshot-c1.tmp"
    (orphan_tmp / "snapshot").mkdir(parents=True)
    (orphan_tmp / "snapshot" / "inside.txt").write_text("staging", encoding="utf-8")

    real_stale = csk_home / "cache" / "src" / "c2" / "snapshot"
    real_stale.mkdir(parents=True)

    warnings: list[str] = []
    removed = gc._collect_snapshots(csk_home, set(), warnings=warnings)

    assert removed == 1
    assert not real_stale.parent.exists()
    assert (orphan_tmp / "snapshot" / "inside.txt").is_file()
    assert any(str(orphan_tmp) in w for w in warnings)
