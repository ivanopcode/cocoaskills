from __future__ import annotations

import json
from dataclasses import replace

from conftest import commit_all, make_config, make_project, make_skill_repo, run, write_skillfile
from csk import installer


CAPS = {"exec": "none", "network": "none"}


def _consumer_manifest(requirements: dict, *, commands: dict | None = None, command_deps: dict | None = None) -> str:
    dependencies: dict = {"skills": requirements}
    if command_deps:
        dependencies["commands"] = command_deps
    payload = {"schema_version": 4, "capabilities": CAPS, "dependencies": dependencies}
    if commands:
        payload["commands"] = commands
        payload["runtime_roots"] = ["scripts"]
    return json.dumps(payload)


def _requirement(repo, *, tag="v1", mode=None, commands=None) -> dict:
    entry: dict = {"git": str(repo), "ref": {"kind": "tag", "value": tag}}
    if mode:
        entry["mode"] = mode
    if commands:
        entry["commands"] = commands
    return entry


def _provider_files(command: str = "tool") -> dict:
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


def test_transitive_requirement_installs_provider(tmp_path, skills_root, csk_home):
    project = make_project(tmp_path)
    provider_repo, provider_commit = make_skill_repo(skills_root, "provider", _provider_files(), tag="v1")
    make_skill_repo(
        skills_root,
        "consumer",
        {"csk-skill.json": _consumer_manifest({"provider": _requirement(provider_repo)})},
        tag="v1",
    )
    write_skillfile(
        project,
        {"schema_version": 1, "agents": ["claude_code"], "skills": [{"name": "consumer", "tag": "v1"}]},
    )
    cfg = make_config(csk_home, skills_root, project, agents=["claude_code"])

    result = installer.install(cfg)[0]
    assert not result.errors, result.errors
    marker = json.loads(
        (project / ".agents" / "skills" / "provider" / ".csk-install.json").read_text(encoding="utf-8")
    )
    assert marker["commit"] == provider_commit
    assert marker["requirers"] == ["consumer"]
    summary = "\n".join(result.messages)
    assert "provider" in summary and "via=consumer" in summary


def test_upgrade_fetches_transitive_dependency_closure(
    monkeypatch, tmp_path, skills_root, csk_home
):
    project = make_project(tmp_path)
    provider_repo, _ = make_skill_repo(skills_root, "provider", _provider_files(), tag="v1")
    consumer_repo, _ = make_skill_repo(
        skills_root,
        "consumer",
        {"csk-skill.json": _consumer_manifest({"provider": _requirement(provider_repo)})},
        tag="v1",
    )
    make_skill_repo(skills_root, "unrelated", tag="v1")
    write_skillfile(
        project,
        {"schema_version": 1, "agents": ["claude_code"], "skills": [{"name": "consumer", "tag": "v1"}]},
    )
    cfg = make_config(csk_home, skills_root, project, agents=["claude_code"])
    fetched = []
    monkeypatch.setattr(installer.git_ops, "fetch_repo", fetched.append)

    result = installer.install(cfg, options=installer.InstallOptions(fetch=True))[0]

    assert not result.errors, result.errors
    assert fetched == [consumer_repo, provider_repo]


def test_transitive_source_is_cloned_into_skills_root(tmp_path, skills_root, csk_home):
    project = make_project(tmp_path)
    external_repo, _ = make_skill_repo(tmp_path / "elsewhere", "provider", _provider_files(), tag="v1")
    make_skill_repo(
        skills_root,
        "consumer",
        {"csk-skill.json": _consumer_manifest({"provider": _requirement(external_repo)})},
        tag="v1",
    )
    write_skillfile(
        project,
        {"schema_version": 1, "agents": ["claude_code"], "skills": [{"name": "consumer", "tag": "v1"}]},
    )
    cfg = make_config(csk_home, skills_root, project, agents=["claude_code"])

    result = installer.install(cfg)[0]
    assert not result.errors, result.errors
    assert (skills_root / "provider" / ".git").exists()


def test_conflicting_commits_for_one_name_fail_with_chains(tmp_path, skills_root, csk_home):
    project = make_project(tmp_path)
    provider_repo, _ = make_skill_repo(skills_root, "provider", _provider_files(), tag="v1")
    (provider_repo / "SKILL.md").write_text("---\nname: test\n---\n\n# Changed\n", encoding="utf-8")
    commit_all(provider_repo, "second")
    run(["git", "tag", "v2"], provider_repo)
    make_skill_repo(
        skills_root,
        "consumer-a",
        {"csk-skill.json": _consumer_manifest({"provider": _requirement(provider_repo, tag="v1")})},
        tag="v1",
    )
    make_skill_repo(
        skills_root,
        "consumer-b",
        {"csk-skill.json": _consumer_manifest({"provider": _requirement(provider_repo, tag="v2")})},
        tag="v1",
    )
    write_skillfile(
        project,
        {
            "schema_version": 1,
            "agents": ["claude_code"],
            "skills": [{"name": "consumer-a", "tag": "v1"}, {"name": "consumer-b", "tag": "v1"}],
        },
    )
    cfg = make_config(csk_home, skills_root, project, agents=["claude_code"])

    result = installer.install(cfg)[0]
    assert result.errors
    message = result.errors[0]
    assert "Version conflict for provider" in message
    assert "consumer-a" in message and "consumer-b" in message


def test_two_refs_resolving_to_one_commit_are_compatible(tmp_path, skills_root, csk_home):
    project = make_project(tmp_path)
    provider_repo, provider_commit = make_skill_repo(skills_root, "provider", _provider_files(), tag="v1")
    make_skill_repo(
        skills_root,
        "consumer-a",
        {"csk-skill.json": _consumer_manifest({"provider": _requirement(provider_repo, tag="v1")})},
        tag="v1",
    )
    make_skill_repo(
        skills_root,
        "consumer-b",
        {
            "csk-skill.json": _consumer_manifest(
                {"provider": {"git": str(provider_repo), "ref": {"kind": "revision", "value": provider_commit}}}
            )
        },
        tag="v1",
    )
    write_skillfile(
        project,
        {
            "schema_version": 1,
            "agents": ["claude_code"],
            "skills": [{"name": "consumer-a", "tag": "v1"}, {"name": "consumer-b", "tag": "v1"}],
        },
    )
    cfg = make_config(csk_home, skills_root, project, agents=["claude_code"])

    result = installer.install(cfg)[0]
    assert not result.errors, result.errors


def test_conflicting_source_identities_fail(tmp_path, skills_root, csk_home):
    project = make_project(tmp_path)
    make_skill_repo(skills_root, "provider", _provider_files(), tag="v1")
    make_skill_repo(
        skills_root,
        "consumer-a",
        {
            "csk-skill.json": _consumer_manifest(
                {"provider": {"git": "git@one.example.com:skills/provider.git", "ref": {"kind": "tag", "value": "v1"}}}
            )
        },
        tag="v1",
    )
    make_skill_repo(
        skills_root,
        "consumer-b",
        {
            "csk-skill.json": _consumer_manifest(
                {"provider": {"git": "git@two.example.com:skills/provider.git", "ref": {"kind": "tag", "value": "v1"}}}
            )
        },
        tag="v1",
    )
    write_skillfile(
        project,
        {
            "schema_version": 1,
            "agents": ["claude_code"],
            "skills": [{"name": "consumer-a", "tag": "v1"}, {"name": "consumer-b", "tag": "v1"}],
        },
    )
    cfg = make_config(csk_home, skills_root, project, agents=["claude_code"])

    result = installer.install(cfg)[0]
    assert result.errors
    assert "Source conflict for provider" in result.errors[0]


def test_dependency_cycle_fails(tmp_path, skills_root, csk_home):
    project = make_project(tmp_path)
    repo_a = skills_root / "skill-a"
    repo_b = skills_root / "skill-b"
    make_skill_repo(
        skills_root,
        "skill-a",
        {"csk-skill.json": _consumer_manifest({"skill-b": {"git": str(repo_b), "ref": {"kind": "tag", "value": "v1"}}})},
        tag="v1",
    )
    make_skill_repo(
        skills_root,
        "skill-b",
        {"csk-skill.json": _consumer_manifest({"skill-a": {"git": str(repo_a), "ref": {"kind": "tag", "value": "v1"}}})},
        tag="v1",
    )
    write_skillfile(
        project,
        {"schema_version": 1, "agents": ["claude_code"], "skills": [{"name": "skill-a", "tag": "v1"}]},
    )
    cfg = make_config(csk_home, skills_root, project, agents=["claude_code"])

    result = installer.install(cfg)[0]
    assert result.errors
    assert "Dependency cycle" in result.errors[0]


def test_requirement_command_missing_from_provider_fails(tmp_path, skills_root, csk_home):
    project = make_project(tmp_path)
    provider_repo, _ = make_skill_repo(skills_root, "provider", _provider_files("tool"), tag="v1")
    make_skill_repo(
        skills_root,
        "consumer",
        {
            "csk-skill.json": _consumer_manifest(
                {"provider": _requirement(provider_repo, mode="runtime", commands=["missing"])}
            )
        },
        tag="v1",
    )
    write_skillfile(
        project,
        {"schema_version": 1, "agents": ["claude_code"], "skills": [{"name": "consumer", "tag": "v1"}]},
    )
    cfg = make_config(csk_home, skills_root, project, agents=["claude_code"])

    result = installer.install(cfg)[0]
    assert result.errors
    assert "does not export a script command named 'missing'" in result.errors[0]


def test_disallowed_source_is_never_fetched(tmp_path, skills_root, csk_home):
    project = make_project(tmp_path)
    make_skill_repo(
        skills_root,
        "consumer",
        {
            "csk-skill.json": _consumer_manifest(
                {"provider": {"git": "git@evil.example.com:skills/provider.git", "ref": {"kind": "tag", "value": "v1"}}}
            )
        },
        tag="v1",
    )
    write_skillfile(
        project,
        {"schema_version": 1, "agents": ["claude_code"], "skills": [{"name": "consumer", "tag": "v1"}]},
    )
    cfg = replace(
        make_config(csk_home, skills_root, project, agents=["claude_code"]),
        allowed_sources=("gitlab.example.com/skills/",),
    )

    result = installer.install(cfg)[0]
    assert result.errors
    assert "Source not allowed for provider" in result.errors[0]
    assert not (skills_root / "provider").exists()


def test_local_sources_pass_the_allowlist(tmp_path, skills_root, csk_home):
    project = make_project(tmp_path)
    external_repo, _ = make_skill_repo(tmp_path / "elsewhere", "provider", _provider_files(), tag="v1")
    make_skill_repo(
        skills_root,
        "consumer",
        {"csk-skill.json": _consumer_manifest({"provider": _requirement(external_repo)})},
        tag="v1",
    )
    write_skillfile(
        project,
        {"schema_version": 1, "agents": ["claude_code"], "skills": [{"name": "consumer", "tag": "v1"}]},
    )
    cfg = replace(
        make_config(csk_home, skills_root, project, agents=["claude_code"]),
        allowed_sources=("gitlab.example.com/skills/",),
    )

    result = installer.install(cfg)[0]
    assert not result.errors, result.errors


def test_legacy_command_dependency_is_satisfied_by_transitive_provider(tmp_path, skills_root, csk_home):
    project = make_project(tmp_path)
    provider_repo, _ = make_skill_repo(skills_root, "provider", _provider_files("tool"), tag="v1")
    make_skill_repo(
        skills_root,
        "consumer",
        {
            "csk-skill.json": _consumer_manifest(
                {"provider": _requirement(provider_repo, mode="runtime", commands=["tool"])},
                command_deps={"tool": {"type": "skill", "skill": "provider", "command": "tool"}},
            )
        },
        tag="v1",
    )
    write_skillfile(
        project,
        {"schema_version": 1, "agents": ["claude_code"], "skills": [{"name": "consumer", "tag": "v1"}]},
    )
    cfg = make_config(csk_home, skills_root, project, agents=["claude_code"])

    result = installer.install(cfg)[0]
    assert not result.errors, result.errors
    summary = "\n".join(result.messages)
    assert "migrate to agent-skill.json schema v4 dependencies.skills" in summary


def test_direct_and_transitive_declarations_unify(tmp_path, skills_root, csk_home):
    project = make_project(tmp_path)
    provider_repo, _ = make_skill_repo(skills_root, "provider", _provider_files(), tag="v1")
    make_skill_repo(
        skills_root,
        "consumer",
        {"csk-skill.json": _consumer_manifest({"provider": _requirement(provider_repo, mode="runtime")})},
        tag="v1",
    )
    write_skillfile(
        project,
        {
            "schema_version": 1,
            "agents": ["claude_code"],
            "skills": [{"name": "consumer", "tag": "v1"}, {"name": "provider", "tag": "v1"}],
        },
    )
    cfg = make_config(csk_home, skills_root, project, agents=["claude_code"])

    result = installer.install(cfg)[0]
    assert not result.errors, result.errors
    # The direct entry contributes a full edge: the provider context is materialized.
    assert (project / ".agents" / "skills" / "provider" / "SKILL.md").exists()
