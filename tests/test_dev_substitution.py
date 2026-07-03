from __future__ import annotations

import json
from dataclasses import replace

from conftest import commit_all, make_config, make_project, make_skill_repo, run, write_skillfile
from csk import cli, installer, status
from csk.config import AuditConfig


CAPS = {"exec": "none", "network": "none"}


def _provider_files() -> dict:
    return {
        "csk-skill.json": json.dumps(
            {"schema_version": 1, "commands": {"tool": {"type": "script", "unix_path": "scripts/tool"}}}
        ),
        "scripts/tool": "#!/bin/sh\necho ok\n",
    }


def _consumer_files(provider_repo) -> dict:
    return {
        "csk-skill.json": json.dumps(
            {
                "schema_version": 4,
                "capabilities": CAPS,
                "dependencies": {
                    "skills": {
                        "provider": {"git": str(provider_repo), "ref": {"kind": "tag", "value": "v1"}}
                    }
                },
            }
        )
    }


def _ignore_dev_manifest(project) -> None:
    gitignore = project / ".gitignore"
    gitignore.write_text(gitignore.read_text(encoding="utf-8") + "Skillfile.dev.json\n", encoding="utf-8")


def _write_dev_manifest(project, payload: dict) -> None:
    (project / "Skillfile.dev.json").write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")


def _setup(tmp_path, skills_root, csk_home):
    project = make_project(tmp_path)
    provider_repo, provider_commit = make_skill_repo(skills_root, "provider", _provider_files(), tag="v1")
    make_skill_repo(skills_root, "consumer", _consumer_files(provider_repo), tag="v1")
    write_skillfile(
        project,
        {"schema_version": 1, "agents": ["claude_code"], "skills": [{"name": "consumer", "tag": "v1"}]},
    )
    cfg = make_config(csk_home, skills_root, project, agents=["claude_code"])
    return project, provider_repo, provider_commit, cfg


def test_path_substitution_reads_the_local_checkout(tmp_path, skills_root, csk_home):
    project, provider_repo, provider_commit, cfg = _setup(tmp_path, skills_root, csk_home)
    dev_checkout = tmp_path / "dev-provider"
    run(["git", "clone", str(provider_repo), str(dev_checkout)], tmp_path)
    run(["git", "config", "user.name", "Dev"], dev_checkout)
    run(["git", "config", "user.email", "dev@example.com"], dev_checkout)
    (dev_checkout / "SKILL.md").write_text("---\nname: test\n---\n\n# Dev state\n", encoding="utf-8")
    dev_commit = commit_all(dev_checkout, "dev iteration")
    assert dev_commit != provider_commit

    _ignore_dev_manifest(project)
    _write_dev_manifest(project, {"substitutions": {"provider": {"path": str(dev_checkout)}}})

    result = installer.install(cfg)[0]
    assert not result.errors, result.errors
    marker = json.loads(
        (project / ".agents" / "skills" / "provider" / ".csk-install.json").read_text(encoding="utf-8")
    )
    assert marker["commit"] == dev_commit
    assert "path" in marker["substituted"]
    summary = "\n".join(result.messages)
    assert "SUBSTITUTION provider" in summary
    assert "SUBSTITUTED" in summary


def test_git_branch_substitution_resolves_the_branch_head(tmp_path, skills_root, csk_home):
    project, provider_repo, provider_commit, cfg = _setup(tmp_path, skills_root, csk_home)
    fork = tmp_path / "fork-provider"
    run(["git", "clone", str(provider_repo), str(fork)], tmp_path)
    run(["git", "config", "user.name", "Dev"], fork)
    run(["git", "config", "user.email", "dev@example.com"], fork)
    run(["git", "checkout", "-b", "feature"], fork)
    (fork / "SKILL.md").write_text("---\nname: test\n---\n\n# Feature\n", encoding="utf-8")
    branch_commit = commit_all(fork, "feature work")

    _ignore_dev_manifest(project)
    _write_dev_manifest(
        project,
        {"substitutions": {"provider": {"git": str(fork), "ref": {"kind": "branch", "value": "feature"}}}},
    )

    result = installer.install(cfg)[0]
    assert not result.errors, result.errors
    marker = json.loads(
        (project / ".agents" / "skills" / "provider" / ".csk-install.json").read_text(encoding="utf-8")
    )
    assert marker["commit"] == branch_commit
    # Dev clones live outside skills_root, so the substitution never shadows
    # the declared source repository.
    assert (csk_home / "dev" / "provider" / ".git").exists()
    assert run(["git", "-C", str(skills_root / "provider"), "rev-parse", "v1"], tmp_path).returncode == 0


def test_substitution_manifest_must_be_gitignored(tmp_path, skills_root, csk_home):
    project, _, _, cfg = _setup(tmp_path, skills_root, csk_home)
    _write_dev_manifest(project, {"substitutions": {"provider": {"path": str(tmp_path)}}})

    result = installer.install(cfg)[0]
    assert result.status == "skipped"
    assert any("Skillfile.dev.json" in message for message in result.messages)


def test_strict_audit_refuses_substituted_installs(tmp_path, skills_root, csk_home):
    project, provider_repo, _, cfg = _setup(tmp_path, skills_root, csk_home)
    _ignore_dev_manifest(project)
    _write_dev_manifest(project, {"substitutions": {"provider": {"path": str(provider_repo)}}})
    strict_cfg = replace(cfg, audit=AuditConfig(enabled=True, mode="strict"))

    result = installer.install(strict_cfg)[0]
    assert result.errors
    assert "strict audit refuses substituted installs" in result.errors[0]


def test_status_prints_active_substitutions(tmp_path, skills_root, csk_home):
    project, provider_repo, _, cfg = _setup(tmp_path, skills_root, csk_home)
    _ignore_dev_manifest(project)
    _write_dev_manifest(project, {"substitutions": {"provider": {"path": str(provider_repo)}}})

    statuses = status.collect_status(cfg)
    assert statuses[0].substitutions
    rendered = status.render_collected(statuses)
    assert "SUBSTITUTION provider" in rendered


def test_init_ignores_the_dev_manifest(tmp_path, monkeypatch):
    root = tmp_path / "fresh"
    root.mkdir()
    run(["git", "init"], root)
    code = cli.main(["init", str(root), "--alias", "fresh"])
    assert code == 0
    assert "Skillfile.dev.json" in (root / ".gitignore").read_text(encoding="utf-8")


def test_malformed_dev_manifest_fails_install(tmp_path, skills_root, csk_home):
    project, _, _, cfg = _setup(tmp_path, skills_root, csk_home)
    _ignore_dev_manifest(project)
    _write_dev_manifest(project, {"substitutions": {"provider": {"path": str(tmp_path), "git": "x"}}})

    result = installer.install(cfg)[0]
    assert result.errors
    assert "exactly one of 'path' or 'git'" in result.errors[0]
