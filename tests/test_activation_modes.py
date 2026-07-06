from __future__ import annotations

import json
import sys

import pytest

from conftest import make_config, make_project, make_skill_repo, write_skillfile
from csk import installer


CAPS = {"exec": "none", "network": "none"}


def _provider_files(*commands: str) -> dict:
    files = {
        "csk-skill.json": json.dumps(
            {
                "schema_version": 1,
                "commands": {
                    name: {
                        "type": "script",
                        "unix_path": f"scripts/{name}",
                        "win_path": f"scripts/{name}.cmd",
                    }
                    for name in commands
                },
            }
        ),
        "references/guide.md": "provider rules\n",
    }
    for name in commands:
        files[f"scripts/{name}"] = "#!/bin/sh\necho ok\n"
        files[f"scripts/{name}.cmd"] = "@echo off\r\necho ok\r\n"
    return files


def _shim(project, name):
    suffix = ".cmd" if sys.platform == "win32" else ""
    return project / ".agents" / "bin" / f"{name}{suffix}"


def _consumer_files(requirements: dict) -> dict:
    return {
        "csk-skill.json": json.dumps(
            {"schema_version": 4, "capabilities": CAPS, "dependencies": {"skills": requirements}}
        )
    }


def _install(tmp_path, skills_root, csk_home, skillfile_skills, repos):
    project = make_project(tmp_path)
    for name, files in repos.items():
        make_skill_repo(skills_root, name, files, tag="v1")
    write_skillfile(project, {"schema_version": 1, "agents": ["claude_code"], "skills": skillfile_skills})
    cfg = make_config(csk_home, skills_root, project, agents=["claude_code"])
    result = installer.install(cfg)[0]
    return project, result


def _requirement(skills_root, name, *, mode=None, commands=None) -> dict:
    entry: dict = {"git": str(skills_root / name), "ref": {"kind": "tag", "value": "v1"}}
    if mode:
        entry["mode"] = mode
    if commands:
        entry["commands"] = commands
    return entry


@pytest.mark.skipif(sys.platform == "win32", reason="Asserts POSIX symlink shims")
def test_runtime_requirement_installs_commands_without_context(tmp_path, skills_root, csk_home):
    project, result = _install(
        tmp_path,
        skills_root,
        csk_home,
        [{"name": "consumer", "tag": "v1"}],
        {
            "provider": _provider_files("tool", "extra"),
            "consumer": _consumer_files(
                {"provider": _requirement(skills_root, "provider", mode="runtime", commands=["tool"])}
            ),
        },
    )
    assert not result.errors, result.errors
    assert (project / ".agents" / "bin" / "tool").is_symlink()
    assert not (project / ".agents" / "bin" / "extra").exists()
    provider_dir = project / ".agents" / "skills" / "provider"
    assert (provider_dir / ".csk-install.json").exists()
    assert not (provider_dir / "SKILL.md").exists()
    assert not (project / ".claude" / "skills" / "provider").exists()
    assert (project / ".claude" / "skills" / "consumer").exists()
    marker = json.loads((provider_dir / ".csk-install.json").read_text(encoding="utf-8"))
    assert marker["activation"] == {"context": False, "commands": ["tool"]}


@pytest.mark.skipif(sys.platform == "win32", reason="Asserts POSIX symlink shims")
def test_runtime_requirement_without_filter_activates_all_exports(tmp_path, skills_root, csk_home):
    project, result = _install(
        tmp_path,
        skills_root,
        csk_home,
        [{"name": "consumer", "tag": "v1"}],
        {
            "provider": _provider_files("tool", "extra"),
            "consumer": _consumer_files({"provider": _requirement(skills_root, "provider", mode="runtime")}),
        },
    )
    assert not result.errors, result.errors
    assert (project / ".agents" / "bin" / "tool").is_symlink()
    assert (project / ".agents" / "bin" / "extra").is_symlink()
    assert not (project / ".claude" / "skills" / "provider").exists()


def test_context_requirement_installs_prompt_context_without_commands(tmp_path, skills_root, csk_home):
    project, result = _install(
        tmp_path,
        skills_root,
        csk_home,
        [{"name": "consumer", "tag": "v1"}],
        {
            "provider": _provider_files("tool"),
            "consumer": _consumer_files({"provider": _requirement(skills_root, "provider", mode="context")}),
        },
    )
    assert not result.errors, result.errors
    provider_dir = project / ".agents" / "skills" / "provider"
    assert (provider_dir / "SKILL.md").exists()
    assert (project / ".claude" / "skills" / "provider").exists()
    assert not _shim(project, "tool").exists()


def test_full_requirement_is_the_default_and_activates_both_surfaces(tmp_path, skills_root, csk_home):
    project, result = _install(
        tmp_path,
        skills_root,
        csk_home,
        [{"name": "consumer", "tag": "v1"}],
        {
            "provider": _provider_files("tool"),
            "consumer": _consumer_files({"provider": _requirement(skills_root, "provider")}),
        },
    )
    assert not result.errors, result.errors
    assert (project / ".agents" / "skills" / "provider" / "SKILL.md").exists()
    assert _shim(project, "tool").exists()


def test_effective_surface_is_the_union_of_edges(tmp_path, skills_root, csk_home):
    project, result = _install(
        tmp_path,
        skills_root,
        csk_home,
        [{"name": "consumer-a", "tag": "v1"}, {"name": "consumer-b", "tag": "v1"}],
        {
            "provider": _provider_files("tool", "extra"),
            "consumer-a": _consumer_files(
                {"provider": _requirement(skills_root, "provider", mode="runtime", commands=["tool"])}
            ),
            "consumer-b": _consumer_files({"provider": _requirement(skills_root, "provider", mode="context")}),
        },
    )
    assert not result.errors, result.errors
    assert (project / ".agents" / "skills" / "provider" / "SKILL.md").exists()
    assert _shim(project, "tool").exists()
    assert not _shim(project, "extra").exists()
    marker = json.loads(
        (project / ".agents" / "skills" / "provider" / ".csk-install.json").read_text(encoding="utf-8")
    )
    assert marker["activation"] == {"context": True, "commands": ["tool"]}
    assert marker["requirers"] == ["consumer-a", "consumer-b"]


def test_inactive_exports_never_collide(tmp_path, skills_root, csk_home):
    project, result = _install(
        tmp_path,
        skills_root,
        csk_home,
        [{"name": "consumer", "tag": "v1"}],
        {
            "provider-one": _provider_files("tool", "alpha"),
            "provider-two": _provider_files("tool", "beta"),
            "consumer": _consumer_files(
                {
                    "provider-one": _requirement(skills_root, "provider-one", mode="runtime", commands=["alpha"]),
                    "provider-two": _requirement(skills_root, "provider-two", mode="runtime", commands=["beta"]),
                }
            ),
        },
    )
    assert not result.errors, result.errors
    assert _shim(project, "alpha").exists()
    assert _shim(project, "beta").exists()
    assert not _shim(project, "tool").exists()


def test_active_command_collision_fails(tmp_path, skills_root, csk_home):
    _, result = _install(
        tmp_path,
        skills_root,
        csk_home,
        [{"name": "consumer", "tag": "v1"}],
        {
            "provider-one": _provider_files("tool"),
            "provider-two": _provider_files("tool"),
            "consumer": _consumer_files(
                {
                    "provider-one": _requirement(skills_root, "provider-one", mode="runtime", commands=["tool"]),
                    "provider-two": _requirement(skills_root, "provider-two", mode="runtime", commands=["tool"]),
                }
            ),
        },
    )
    assert result.errors
    assert "Command collision for 'tool'" in result.errors[0]


def test_removed_requirement_cleans_up_on_reinstall(tmp_path, skills_root, csk_home):
    project, result = _install(
        tmp_path,
        skills_root,
        csk_home,
        [{"name": "consumer", "tag": "v1"}, {"name": "provider", "tag": "v1"}],
        {
            "provider": _provider_files("tool"),
            "consumer": _consumer_files({"provider": _requirement(skills_root, "provider", mode="runtime")}),
        },
    )
    assert not result.errors, result.errors
    write_skillfile(
        project,
        {"schema_version": 1, "agents": ["claude_code"], "skills": [{"name": "provider", "tag": "v1"}]},
    )
    cfg = make_config(csk_home, skills_root, project, agents=["claude_code"])
    result = installer.install(cfg)[0]
    assert not result.errors, result.errors
    assert not (project / ".agents" / "skills" / "consumer").exists()
    assert (project / ".agents" / "skills" / "provider" / "SKILL.md").exists()
