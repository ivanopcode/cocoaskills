from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

from conftest import make_config, make_project, make_skill_repo, write_skillfile
from csk import hybrid, installer


CAPS = {"exec": "none", "network": "none"}


def _tool_skill_files(command: str = "brief") -> dict:
    return {
        "csk-skill.json": json.dumps(
            {
                "schema_version": 1,
                "commands": {
                    command: {
                        "type": "script",
                        "unix_path": f"scripts/{command}",
                        "win_path": f"scripts/{command}.cmd",
                    }
                },
            }
        ),
        f"scripts/{command}": "#!/bin/sh\necho ok\n",
        f"scripts/{command}.cmd": "@echo off\r\necho ok\r\n",
    }


def _write_hybrid_manifest(csk_home: Path, entries: list[dict]) -> None:
    path = hybrid.hybrid_manifest_path(csk_home)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps({"schema_version": 1, "skills": entries}, indent=2) + "\n", encoding="utf-8"
    )


def _shim(project: Path, name: str) -> Path:
    suffix = ".cmd" if sys.platform == "win32" else ""
    return project / ".agents" / "bin" / f"{name}{suffix}"


def test_hybrid_manifest_roundtrip(csk_home):
    hybrid.add_hybrid_decl(
        csk_home, name="skill-conventions", ref_kind="tag", ref="v1", git=None, targets=["app"]
    )
    decls = hybrid.load_hybrid_decls(csk_home)
    assert len(decls) == 1
    assert decls[0].decl.name == "skill-conventions"
    assert decls[0].targets == ("app",)

    hybrid.add_hybrid_decl(
        csk_home, name="skill-conventions", ref_kind="tag", ref="v2", git=None, targets=["app", "other"]
    )
    decls = hybrid.load_hybrid_decls(csk_home)
    assert len(decls) == 1
    assert decls[0].decl.ref.value == "v2"
    assert decls[0].targets == ("app", "other")

    hybrid.remove_hybrid_decl(csk_home, "skill-conventions")
    assert hybrid.load_hybrid_decls(csk_home) == []


def test_hybrid_manifest_requires_targets(csk_home):
    _write_hybrid_manifest(csk_home, [{"name": "skill-conventions", "tag": "v1"}])
    with pytest.raises(hybrid.HybridError, match="targets"):
        hybrid.load_hybrid_decls(csk_home)


def test_hybrid_skill_activates_for_targeted_project(tmp_path, skills_root, csk_home):
    project = make_project(tmp_path)
    make_skill_repo(skills_root, "skill-conventions", _tool_skill_files(), tag="v1")
    write_skillfile(project, {"schema_version": 1, "agents": ["claude_code"], "skills": []})
    _write_hybrid_manifest(
        csk_home,
        [{"name": "skill-conventions", "tag": "v1", "targets": [str(project)]}],
    )
    cfg = make_config(csk_home, skills_root, project, agents=["claude_code"])

    result = installer.install(cfg)[0]
    assert not result.errors, result.errors
    store_dir = hybrid.hybrid_skills_root(csk_home) / "skill-conventions"
    assert (store_dir / "SKILL.md").exists()
    assert (store_dir / ".csk-install.json").exists()
    # Контекст не материализуется в дереве проекта, только линк в адаптере.
    assert not (project / ".agents" / "skills" / "skill-conventions").exists()
    assert (project / ".claude" / "skills" / "skill-conventions" / "SKILL.md").exists()
    assert _shim(project, "brief").exists()
    summary = "\n".join(result.messages)
    assert "(hybrid)" in summary


def test_hybrid_skill_invisible_for_untargeted_project(tmp_path, skills_root, csk_home):
    project_a = make_project(tmp_path, "project-a")
    project_b = make_project(tmp_path, "project-b")
    make_skill_repo(skills_root, "skill-conventions", _tool_skill_files(), tag="v1")
    for project in (project_a, project_b):
        write_skillfile(project, {"schema_version": 1, "agents": ["claude_code"], "skills": []})
    _write_hybrid_manifest(
        csk_home,
        [{"name": "skill-conventions", "tag": "v1", "targets": [str(project_a)]}],
    )

    result_a = installer.install(make_config(csk_home, skills_root, project_a, agents=["claude_code"]))[0]
    result_b = installer.install(make_config(csk_home, skills_root, project_b, agents=["claude_code"]))[0]
    assert not result_a.errors and not result_b.errors
    assert (project_a / ".claude" / "skills" / "skill-conventions").exists()
    assert not (project_b / ".claude" / "skills" / "skill-conventions").exists()
    assert not _shim(project_b, "brief").exists()


def test_hybrid_target_matches_alias_and_glob(tmp_path, skills_root, csk_home):
    project = make_project(tmp_path)
    make_skill_repo(skills_root, "skill-conventions", _tool_skill_files(), tag="v1")
    write_skillfile(project, {"schema_version": 1, "agents": ["claude_code"], "skills": []})
    cfg = make_config(csk_home, skills_root, project, agents=["claude_code"])

    _write_hybrid_manifest(
        csk_home, [{"name": "skill-conventions", "tag": "v1", "targets": ["app"]}]
    )
    result = installer.install(cfg)[0]
    assert not result.errors, result.errors
    assert (project / ".claude" / "skills" / "skill-conventions").exists()

    glob_target = str(tmp_path / "*roject").replace("\\", "/")
    _write_hybrid_manifest(
        csk_home, [{"name": "skill-conventions", "tag": "v1", "targets": [glob_target]}]
    )
    result = installer.install(cfg)[0]
    assert not result.errors, result.errors
    assert (project / ".claude" / "skills" / "skill-conventions").exists()


def test_project_declaration_shadows_hybrid(tmp_path, skills_root, csk_home):
    project = make_project(tmp_path)
    make_skill_repo(skills_root, "skill-conventions", _tool_skill_files(), tag="v1")
    write_skillfile(
        project,
        {"schema_version": 1, "agents": ["claude_code"], "skills": [{"name": "skill-conventions", "tag": "v1"}]},
    )
    _write_hybrid_manifest(
        csk_home,
        [{"name": "skill-conventions", "tag": "v1", "targets": [str(project)]}],
    )
    cfg = make_config(csk_home, skills_root, project, agents=["claude_code"])

    result = installer.install(cfg)[0]
    assert not result.errors, result.errors
    # Проектная копия материализована в проекте, гибридная не устанавливается.
    assert (project / ".agents" / "skills" / "skill-conventions" / "SKILL.md").exists()
    assert not (hybrid.hybrid_skills_root(csk_home) / "skill-conventions").exists()
    assert any("shadowed by the project declaration" in message for message in result.messages)


def test_removed_hybrid_target_cleans_up_on_reinstall(tmp_path, skills_root, csk_home):
    project = make_project(tmp_path)
    make_skill_repo(skills_root, "skill-conventions", _tool_skill_files(), tag="v1")
    write_skillfile(project, {"schema_version": 1, "agents": ["claude_code"], "skills": []})
    _write_hybrid_manifest(
        csk_home,
        [{"name": "skill-conventions", "tag": "v1", "targets": [str(project)]}],
    )
    cfg = make_config(csk_home, skills_root, project, agents=["claude_code"])
    result = installer.install(cfg)[0]
    assert not result.errors, result.errors
    assert (project / ".claude" / "skills" / "skill-conventions").exists()

    _write_hybrid_manifest(csk_home, [])
    result = installer.install(cfg)[0]
    assert not result.errors, result.errors
    assert not (project / ".claude" / "skills" / "skill-conventions").exists()
    assert not _shim(project, "brief").exists()
    assert not (hybrid.hybrid_skills_root(csk_home) / "skill-conventions").exists()


def test_hybrid_skill_dependencies_stay_in_hybrid_store(tmp_path, skills_root, csk_home):
    project = make_project(tmp_path)
    provider_repo, _ = make_skill_repo(skills_root, "skill-provider", _tool_skill_files("tool"), tag="v1")
    make_skill_repo(
        skills_root,
        "skill-conventions",
        {
            "csk-skill.json": json.dumps(
                {
                    "schema_version": 4,
                    "capabilities": CAPS,
                    "dependencies": {
                        "skills": {
                            "skill-provider": {
                                "git": str(provider_repo),
                                "ref": {"kind": "tag", "value": "v1"},
                            }
                        }
                    },
                }
            )
        },
        tag="v1",
    )
    write_skillfile(project, {"schema_version": 1, "agents": ["claude_code"], "skills": []})
    _write_hybrid_manifest(
        csk_home,
        [{"name": "skill-conventions", "tag": "v1", "targets": [str(project)]}],
    )
    cfg = make_config(csk_home, skills_root, project, agents=["claude_code"])

    result = installer.install(cfg)[0]
    assert not result.errors, result.errors
    store = hybrid.hybrid_skills_root(csk_home)
    assert (store / "skill-conventions" / "SKILL.md").exists()
    assert (store / "skill-provider" / "SKILL.md").exists()
    assert not (project / ".agents" / "skills" / "skill-provider").exists()
    assert (project / ".claude" / "skills" / "skill-provider").exists()
    assert _shim(project, "tool").exists()


def test_hybrid_cli_add_list_remove(tmp_path, skills_root, csk_home, monkeypatch, capsys):
    from csk import cli, config as csk_config

    project = make_project(tmp_path)
    cfg = make_config(csk_home, skills_root, project, agents=["claude_code"])
    csk_config.save_config(cfg)
    monkeypatch.setenv("CSK_CONFIG", str(cfg.path))

    code = cli.main(
        ["hybrid", "add", "skill-conventions", "--tag", "v1", "--target", "app", "--target", str(project)]
    )
    assert code == 0
    decls = hybrid.load_hybrid_decls(csk_home)
    assert decls[0].targets == ("app", str(project))

    code = cli.main(["hybrid", "list"])
    assert code == 0
    assert "skill-conventions" in capsys.readouterr().out

    code = cli.main(["hybrid", "remove", "skill-conventions"])
    assert code == 0
    assert hybrid.load_hybrid_decls(csk_home) == []
