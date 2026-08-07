"""Selective global install: --only narrows the closure and the fetch set."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from conftest import (
    commit_all,
    make_config,
    make_project,
    make_skill_repo,
    run,
    write_files,
)

from csk import cli, config, git_ops, global_install


def _save_config(monkeypatch: pytest.MonkeyPatch, cfg: config.GlobalConfig) -> None:
    config.save_config(cfg)
    monkeypatch.setenv("CSK_CONFIG", str(cfg.path))


def _write_global_skillfile(csk_home: Path, names: list[str]) -> None:
    root = csk_home / "global"
    root.mkdir(parents=True, exist_ok=True)
    (root / "Skillfile.json").write_text(
        json.dumps(
            {
                "schema_version": 1,
                "agents": ["codex_cli"],
                "skills": [{"name": name, "tag": "v1"} for name in names],
            },
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )


def _make_plain_skill(skills_root: Path, name: str) -> None:
    make_skill_repo(
        skills_root,
        name,
        {"csk-skill.json": json.dumps({"schema_version": 2})},
        tag="v1",
    )


def _make_command_skill(skills_root: Path, name: str, command: str) -> None:
    make_skill_repo(
        skills_root,
        name,
        {
            "csk-skill.json": json.dumps(
                {
                    "schema_version": 2,
                    "runtime_roots": ["scripts"],
                    "commands": {
                        command: {
                            "type": "script",
                            "unix_path": f"scripts/{command}",
                        }
                    },
                }
            ),
            f"scripts/{command}": f"#!/bin/sh\necho {name}\n",
        },
        tag="v1",
    )


def _record_git_traffic(monkeypatch: pytest.MonkeyPatch) -> dict[str, list[str]]:
    traffic: dict[str, list[str]] = {"fetch": [], "clone": []}
    real_fetch = git_ops.fetch_repo
    real_clone = git_ops.clone_repo

    def recording_fetch(repo, *args, **kwargs):
        traffic["fetch"].append(Path(repo).name)
        return real_fetch(repo, *args, **kwargs)

    def recording_clone(url, destination, *args, **kwargs):
        traffic["clone"].append(Path(destination).name)
        return real_clone(url, destination, *args, **kwargs)

    monkeypatch.setattr(git_ops, "fetch_repo", recording_fetch)
    monkeypatch.setattr(git_ops, "clone_repo", recording_clone)
    return traffic


def test_global_upgrade_only_fetches_the_requested_skill(
    monkeypatch, tmp_path, skills_root, csk_home
):
    project = make_project(tmp_path)
    cfg = make_config(csk_home, skills_root, project, agents=["codex_cli"])
    _save_config(monkeypatch, cfg)
    for name in ("skill-a", "skill-b", "skill-c"):
        _make_plain_skill(skills_root, name)
    _write_global_skillfile(csk_home, ["skill-a", "skill-b", "skill-c"])

    traffic = _record_git_traffic(monkeypatch)

    assert cli.main(["global", "upgrade", "--only", "skill-a"]) == 0

    assert set(traffic["fetch"]) == {"skill-a"}
    installed = csk_home / "global" / "skills"
    assert (installed / "skill-a").is_dir()
    assert not (installed / "skill-b").exists()
    assert not (installed / "skill-c").exists()


def test_global_upgrade_without_only_still_fetches_every_declared_skill(
    monkeypatch, tmp_path, skills_root, csk_home
):
    project = make_project(tmp_path)
    cfg = make_config(csk_home, skills_root, project, agents=["codex_cli"])
    _save_config(monkeypatch, cfg)
    for name in ("skill-a", "skill-b"):
        _make_plain_skill(skills_root, name)
    _write_global_skillfile(csk_home, ["skill-a", "skill-b"])

    traffic = _record_git_traffic(monkeypatch)

    assert cli.main(["global", "upgrade"]) == 0

    assert set(traffic["fetch"]) == {"skill-a", "skill-b"}


def test_global_install_only_never_clones_an_unselected_source(
    monkeypatch, tmp_path, skills_root, csk_home
):
    """The private-repository failure from issue 27.

    An unselected declaration pointing at an unreachable repository must not
    be cloned, so it cannot fail the run the operator actually asked for.
    """

    project = make_project(tmp_path)
    cfg = make_config(csk_home, skills_root, project, agents=["codex_cli"])
    _save_config(monkeypatch, cfg)
    _make_plain_skill(skills_root, "skill-wanted")
    root = csk_home / "global"
    root.mkdir(parents=True, exist_ok=True)
    (root / "Skillfile.json").write_text(
        json.dumps(
            {
                "schema_version": 1,
                "agents": ["codex_cli"],
                "skills": [
                    {"name": "skill-wanted", "tag": "v1"},
                    {
                        "name": "skill-private",
                        "tag": "v1",
                        "git": "ssh://git@unreachable.invalid/skills/skill-private.git",
                    },
                ],
            },
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )

    traffic = _record_git_traffic(monkeypatch)

    assert cli.main(["global", "install", "--only", "skill-wanted"]) == 0

    assert traffic["clone"] == []
    assert (csk_home / "global" / "skills" / "skill-wanted").is_dir()


def test_global_install_only_installs_the_required_closure(
    monkeypatch, tmp_path, skills_root, csk_home
):
    project = make_project(tmp_path)
    cfg = make_config(csk_home, skills_root, project, agents=["codex_cli"])
    _save_config(monkeypatch, cfg)
    _make_plain_skill(skills_root, "skill-provider")
    make_skill_repo(
        skills_root,
        "skill-consumer",
        {
            "csk-skill.json": json.dumps(
                {
                    "schema_version": 4,
                    "capabilities": {"exec": "none", "network": "none"},
                    "dependencies": {
                        "skills": {
                            "skill-provider": {
                                "git": str(skills_root / "skill-provider"),
                                "ref": {"kind": "tag", "value": "v1"},
                                "mode": "context",
                            }
                        }
                    },
                }
            )
        },
        tag="v1",
    )
    _make_plain_skill(skills_root, "skill-unrelated")
    _write_global_skillfile(
        csk_home, ["skill-consumer", "skill-provider", "skill-unrelated"]
    )

    traffic = _record_git_traffic(monkeypatch)

    assert cli.main(["global", "upgrade", "--only", "skill-consumer"]) == 0

    assert set(traffic["fetch"]) == {"skill-consumer", "skill-provider"}
    installed = csk_home / "global" / "skills"
    assert (installed / "skill-consumer").is_dir()
    assert (installed / "skill-provider").is_dir()
    assert not (installed / "skill-unrelated").exists()


def test_global_install_only_preserves_previously_installed_skills(
    monkeypatch, tmp_path, skills_root, csk_home
):
    project = make_project(tmp_path)
    cfg = make_config(csk_home, skills_root, project, agents=["codex_cli"])
    _save_config(monkeypatch, cfg)
    _make_command_skill(skills_root, "skill-kept", "kept-tool")
    _make_command_skill(skills_root, "skill-target", "target-tool")
    _write_global_skillfile(csk_home, ["skill-kept", "skill-target"])

    assert cli.main(["global", "install"]) == 0
    installed = csk_home / "global" / "skills"
    kept_marker = json.loads(
        (installed / "skill-kept" / ".csk-install.json").read_text(
            encoding="utf-8"
        )
    )
    kept_commit = kept_marker["commit"]

    assert cli.main(["global", "install", "--only", "skill-target"]) == 0

    assert (installed / "skill-kept").is_dir()
    assert (installed / "skill-target").is_dir()
    assert (csk_home / "global" / "bin" / "kept-tool").exists()
    assert (csk_home / "global" / "bin" / "target-tool").exists()
    assert (
        csk_home / "runtime" / "skill-kept" / kept_commit
    ).is_dir()
    assert (Path.home() / ".codex" / "skills" / "skill-kept").exists()
    assert (Path.home() / ".codex" / "skills" / "skill-target").exists()


def test_global_install_only_rejects_a_command_taken_by_a_retained_skill(
    monkeypatch, tmp_path, skills_root, csk_home
):
    project = make_project(tmp_path)
    cfg = make_config(csk_home, skills_root, project, agents=["codex_cli"])
    _save_config(monkeypatch, cfg)
    _make_command_skill(skills_root, "skill-kept", "shared-tool")
    _make_plain_skill(skills_root, "skill-target")
    _write_global_skillfile(csk_home, ["skill-kept", "skill-target"])
    assert cli.main(["global", "install"]) == 0

    # skill-target v2 starts exporting a command skill-kept already owns.
    target_repo = skills_root / "skill-target"
    write_files(
        target_repo,
        {
            "csk-skill.json": json.dumps(
                {
                    "schema_version": 2,
                    "runtime_roots": ["scripts"],
                    "commands": {
                        "shared-tool": {
                            "type": "script",
                            "unix_path": "scripts/shared-tool",
                        }
                    },
                }
            ),
            "scripts/shared-tool": "#!/bin/sh\necho skill-target\n",
        },
    )
    commit_all(target_repo, "export shared-tool")
    run(["git", "tag", "v2"], target_repo)
    _write_global_skillfile(csk_home, ["skill-kept"])
    root = csk_home / "global"
    payload = json.loads((root / "Skillfile.json").read_text(encoding="utf-8"))
    payload["skills"].append({"name": "skill-target", "tag": "v2"})
    (root / "Skillfile.json").write_text(
        json.dumps(payload, indent=2) + "\n", encoding="utf-8"
    )

    result = global_install.install(cfg, only=["skill-target"])

    assert result.failed
    assert any(
        "Command collision for 'shared-tool'" in error
        for error in result.errors
    )
    assert (csk_home / "global" / "bin" / "shared-tool").exists()


def test_global_install_only_warns_about_retained_dependents(
    monkeypatch, tmp_path, skills_root, csk_home, capsys
):
    project = make_project(tmp_path)
    cfg = make_config(csk_home, skills_root, project, agents=["codex_cli"])
    _save_config(monkeypatch, cfg)
    _make_plain_skill(skills_root, "skill-provider")
    make_skill_repo(
        skills_root,
        "skill-consumer",
        {
            "csk-skill.json": json.dumps(
                {
                    "schema_version": 4,
                    "capabilities": {"exec": "none", "network": "none"},
                    "dependencies": {
                        "skills": {
                            "skill-provider": {
                                "git": str(skills_root / "skill-provider"),
                                "ref": {"kind": "tag", "value": "v1"},
                                "mode": "context",
                            }
                        }
                    },
                }
            )
        },
        tag="v1",
    )
    _write_global_skillfile(csk_home, ["skill-consumer", "skill-provider"])
    assert cli.main(["global", "install"]) == 0
    capsys.readouterr()

    assert cli.main(["global", "install", "--only", "skill-provider"]) == 0

    out = capsys.readouterr().out
    assert "selective run over skill-provider" in out
    assert "skill-consumer" in out
    assert "were not revalidated" in out
    assert (csk_home / "global" / "skills" / "skill-consumer").is_dir()


def test_global_install_without_only_reports_no_selective_scope(
    monkeypatch, tmp_path, skills_root, csk_home, capsys
):
    project = make_project(tmp_path)
    cfg = make_config(csk_home, skills_root, project, agents=["codex_cli"])
    _save_config(monkeypatch, cfg)
    _make_plain_skill(skills_root, "skill-a")
    _write_global_skillfile(csk_home, ["skill-a"])

    assert cli.main(["global", "install"]) == 0

    assert "selective run" not in capsys.readouterr().out


def test_global_install_only_rejects_an_undeclared_name(
    monkeypatch, tmp_path, skills_root, csk_home, capsys
):
    project = make_project(tmp_path)
    cfg = make_config(csk_home, skills_root, project, agents=["codex_cli"])
    _save_config(monkeypatch, cfg)
    _make_plain_skill(skills_root, "skill-a")
    _write_global_skillfile(csk_home, ["skill-a"])

    code = cli.main(["global", "install", "--only", "skill-missing"])

    assert code != 0
    error = capsys.readouterr().err
    assert "skill-missing" in error
    assert "skill-a" in error


def test_global_update_only_fetches_the_requested_skill(
    monkeypatch, tmp_path, skills_root, csk_home
):
    project = make_project(tmp_path)
    cfg = make_config(csk_home, skills_root, project, agents=["codex_cli"])
    _save_config(monkeypatch, cfg)
    for name in ("skill-a", "skill-b"):
        _make_plain_skill(skills_root, name)
    _write_global_skillfile(csk_home, ["skill-a", "skill-b"])

    traffic = _record_git_traffic(monkeypatch)

    assert cli.main(["global", "update", "--only", "skill-b"]) == 0

    assert traffic["fetch"] == ["skill-b"]


def test_global_install_only_accepts_repeated_selectors(
    monkeypatch, tmp_path, skills_root, csk_home
):
    project = make_project(tmp_path)
    cfg = make_config(csk_home, skills_root, project, agents=["codex_cli"])
    _save_config(monkeypatch, cfg)
    for name in ("skill-a", "skill-b", "skill-c"):
        _make_plain_skill(skills_root, name)
    _write_global_skillfile(csk_home, ["skill-a", "skill-b", "skill-c"])

    traffic = _record_git_traffic(monkeypatch)

    assert (
        cli.main(
            ["global", "upgrade", "--only", "skill-a", "--only", "skill-c"]
        )
        == 0
    )

    assert set(traffic["fetch"]) == {"skill-a", "skill-c"}
    installed = csk_home / "global" / "skills"
    assert (installed / "skill-a").is_dir()
    assert (installed / "skill-c").is_dir()
    assert not (installed / "skill-b").exists()


def test_global_install_without_only_still_removes_undeclared_skills(
    monkeypatch, tmp_path, skills_root, csk_home
):
    project = make_project(tmp_path)
    cfg = make_config(csk_home, skills_root, project, agents=["codex_cli"])
    _save_config(monkeypatch, cfg)
    _make_plain_skill(skills_root, "skill-a")
    _make_plain_skill(skills_root, "skill-b")
    _write_global_skillfile(csk_home, ["skill-a", "skill-b"])
    assert cli.main(["global", "install"]) == 0

    _write_global_skillfile(csk_home, ["skill-a"])

    assert cli.main(["global", "install"]) == 0

    installed = csk_home / "global" / "skills"
    assert (installed / "skill-a").is_dir()
    assert not (installed / "skill-b").exists()
