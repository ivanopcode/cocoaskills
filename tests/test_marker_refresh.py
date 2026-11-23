"""Regression and parity tests for refreshing legacy markers on closure metadata drift."""

from __future__ import annotations

import ast
import inspect
import json
import textwrap
from dataclasses import fields as dataclass_fields, replace
from pathlib import Path

import pytest
from conftest import (
    commit_all,
    make_config,
    make_project,
    make_skill_repo,
    run,
    write_files,
    write_skillfile,
)
from test_installer_transactions import (
    _build_skill_files,
    _install_fake_build_pipeline,
    _write_hybrid_manifest,
)

from csk import (
    cli,
    closure,
    config as config_mod,
    global_install,
    hashing,
    hybrid,
    install_marker,
    installer,
    manifest,
    skillspec,
    status,
)
from csk.builds import source as build_source


CAPS = {"exec": "none", "network": "none"}
OLD_INSTALLED_AT = "2020-01-01T00:00:00Z"


def _save_config(monkeypatch: pytest.MonkeyPatch, cfg: config_mod.GlobalConfig) -> None:
    config_mod.save_config(cfg)
    monkeypatch.setenv("CSK_CONFIG", str(cfg.path))


def _fixed_evidence(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        status,
        "_capability_evidence",
        lambda: (
            {
                "record_version": "capability-evidence-v1",
                "execution_policy": "manager-worker-v1",
                "platform": "macos",
                "controls": [],
            },
            None,
        ),
    )


def _consumer_manifest(
    requirements: dict[str, dict[str, object]],
) -> str:
    return json.dumps(
        {
            "schema_version": 4,
            "capabilities": CAPS,
            "dependencies": {"skills": requirements},
        }
    )


def _requirement(
    repo: Path,
    *,
    tag: str = "v1",
    mode: str | None = None,
    commands: list[str] | None = None,
) -> dict[str, object]:
    entry: dict[str, object] = {
        "git": str(repo),
        "ref": {"kind": "tag", "value": tag},
    }
    if mode is not None:
        entry["mode"] = mode
    if commands is not None:
        entry["commands"] = commands
    return entry


def _script_skill_files(command: str = "helper") -> dict[str, str]:
    return {
        "csk-skill.json": json.dumps(
            {
                "schema_version": 2,
                "runtime_roots": ["scripts"],
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


def _write_global_skillfile(
    csk_home: Path,
    entries: list[dict[str, object]],
    *,
    agents: list[str] | None = None,
) -> None:
    root = csk_home / "global"
    root.mkdir(parents=True, exist_ok=True)
    (root / "Skillfile.json").write_text(
        json.dumps(
            {
                "schema_version": 1,
                "agents": agents or ["codex_cli"],
                "skills": entries,
            },
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )


def _read_marker_json(path: Path) -> dict[str, object]:
    data = json.loads(path.read_text(encoding="utf-8"))
    assert isinstance(data, dict)
    return data


def _write_marker_json(path: Path, payload: dict[str, object]) -> None:
    path.write_bytes(install_marker.serialize_install_marker(payload))


def _set_old_installed_at(marker_path: Path) -> bytes:
    payload = _read_marker_json(marker_path)
    payload["installed_at"] = OLD_INSTALLED_AT
    _write_marker_json(marker_path, payload)
    return marker_path.read_bytes()


def test_upgrade_refreshes_build_provider_marker_when_requirers_grow(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    skills_root: Path,
    csk_home: Path,
) -> None:
    # Upgrading with --strict-tags rewrites a build provider's stale requirers without rebuilding and leaves status --check clean.
    project = make_project(tmp_path)
    provider_repo, provider_commit = make_skill_repo(
        skills_root, "provider", _build_skill_files("tool"), tag="v1"
    )
    for consumer in ("consumer-a", "consumer-b", "consumer-c"):
        make_skill_repo(
            skills_root,
            consumer,
            {"csk-skill.json": _consumer_manifest({"provider": _requirement(provider_repo)})},
            tag="v1",
        )
    write_skillfile(
        project,
        {
            "schema_version": 1,
            "agents": ["claude_code"],
            "skills": [{"name": "consumer-a", "tag": "v1"}],
        },
    )
    cfg = make_config(csk_home, skills_root, project, agents=["claude_code"])
    _save_config(monkeypatch, cfg)
    events: list[str] = []
    _install_fake_build_pipeline(monkeypatch, events=events)
    _fixed_evidence(monkeypatch)

    assert cli.main(["install", "app"]) == 0
    assert events == ["build:tool"]
    marker_path = project / ".agents" / "skills" / "provider" / ".csk-install.json"
    assert _read_marker_json(marker_path)["requirers"] == ["consumer-a"]

    write_skillfile(
        project,
        {
            "schema_version": 1,
            "agents": ["claude_code"],
            "skills": [
                {"name": "consumer-a", "tag": "v1"},
                {"name": "consumer-b", "tag": "v1"},
                {"name": "consumer-c", "tag": "v1"},
            ],
        },
    )
    # Positive control: before upgrade, status --check flags the stale requirers as drift.
    assert cli.main(["status", "app", "--check"]) == 1

    assert cli.main(["upgrade", "app", "--strict-tags"]) == 0
    marker = _read_marker_json(marker_path)
    assert marker["commit"] == provider_commit
    assert marker["requirers"] == ["consumer-a", "consumer-b", "consumer-c"]
    assert events == ["build:tool"]

    assert cli.main(["status", "app", "--check"]) == 0
    collected = status.collect_status(cfg)[0]
    assert collected.clean
    provider_row = next(row for row in collected.skills if row.name == "provider")
    assert provider_row.label == "up-to-date"
    assert len(collected.builds) == 1
    assert collected.builds[0].current
    assert collected.builds[0].label == "current"


def test_install_refreshes_build_provider_marker_when_requirers_grow(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    skills_root: Path,
    csk_home: Path,
) -> None:
    # Project csk install rewrites a build provider's marker on requirers growth with a build cache hit and clean status --check.
    project = make_project(tmp_path)
    provider_repo, provider_commit = make_skill_repo(
        skills_root, "provider", _build_skill_files("tool"), tag="v1"
    )
    for consumer in ("consumer-a", "consumer-b", "consumer-c"):
        make_skill_repo(
            skills_root,
            consumer,
            {"csk-skill.json": _consumer_manifest({"provider": _requirement(provider_repo)})},
            tag="v1",
        )
    write_skillfile(
        project,
        {
            "schema_version": 1,
            "agents": ["claude_code"],
            "skills": [{"name": "consumer-a", "tag": "v1"}],
        },
    )
    cfg = make_config(csk_home, skills_root, project, agents=["claude_code"])
    _save_config(monkeypatch, cfg)
    events: list[str] = []
    _install_fake_build_pipeline(monkeypatch, events=events)
    _fixed_evidence(monkeypatch)

    assert cli.main(["install", "app"]) == 0
    assert events == ["build:tool"]

    write_skillfile(
        project,
        {
            "schema_version": 1,
            "agents": ["claude_code"],
            "skills": [
                {"name": "consumer-a", "tag": "v1"},
                {"name": "consumer-b", "tag": "v1"},
                {"name": "consumer-c", "tag": "v1"},
            ],
        },
    )
    assert cli.main(["status", "app", "--check"]) == 1

    assert cli.main(["install", "app"]) == 0
    marker_path = project / ".agents" / "skills" / "provider" / ".csk-install.json"
    marker = _read_marker_json(marker_path)
    assert marker["commit"] == provider_commit
    assert marker["requirers"] == ["consumer-a", "consumer-b", "consumer-c"]
    assert events == ["build:tool"]

    assert cli.main(["status", "app", "--check"]) == 0
    collected = status.collect_status(cfg)[0]
    assert collected.clean
    assert collected.builds[0].current


@pytest.mark.parametrize("subcommand", ["install", "upgrade"])
def test_global_install_and_upgrade_refresh_build_provider_marker_when_requirers_grow(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    skills_root: Path,
    csk_home: Path,
    subcommand: str,
) -> None:
    # Global install and upgrade rewrite a build provider's marker when requirers grow without rebuilding and pass global status --check.
    project = make_project(tmp_path)
    provider_repo, provider_commit = make_skill_repo(
        skills_root, "provider", _build_skill_files("tool"), tag="v1"
    )
    for consumer in ("consumer-a", "consumer-b", "consumer-c"):
        make_skill_repo(
            skills_root,
            consumer,
            {"csk-skill.json": _consumer_manifest({"provider": _requirement(provider_repo)})},
            tag="v1",
        )
    cfg = make_config(csk_home, skills_root, project, agents=["codex_cli"])
    _save_config(monkeypatch, cfg)
    events: list[str] = []
    _install_fake_build_pipeline(monkeypatch, events=events)
    _fixed_evidence(monkeypatch)

    _write_global_skillfile(csk_home, [{"name": "consumer-a", "tag": "v1"}])
    assert cli.main(["global", "install"]) == 0
    assert events == ["build:tool"]

    _write_global_skillfile(
        csk_home,
        [
            {"name": "consumer-a", "tag": "v1"},
            {"name": "consumer-b", "tag": "v1"},
            {"name": "consumer-c", "tag": "v1"},
        ],
    )
    assert cli.main(["global", "status", "--check"]) == 1

    assert cli.main(["global", subcommand]) == 0
    marker_path = global_install.global_skills_root(csk_home) / "provider" / ".csk-install.json"
    marker = _read_marker_json(marker_path)
    assert marker["commit"] == provider_commit
    assert marker["requirers"] == ["consumer-a", "consumer-b", "consumer-c"]
    assert events == ["build:tool"]

    assert cli.main(["global", "status", "--check"]) == 0
    collected = status.collect_global_status(cfg)
    assert collected.clean
    assert len(collected.builds) == 1
    assert collected.builds[0].current


@pytest.mark.parametrize("node_kind", ["project-marker-only", "global-marker-only"])
def test_marker_only_nodes_refresh_on_requirers_change(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    skills_root: Path,
    csk_home: Path,
    node_kind: str,
) -> None:
    # Marker-only project and global provider nodes refresh when requirers change at the same commit without rebuilding.
    project = make_project(tmp_path)
    provider_repo, _ = make_skill_repo(
        skills_root, "provider", _build_skill_files("tool"), tag="v1"
    )
    for consumer in ("consumer-a", "consumer-b"):
        make_skill_repo(
            skills_root,
            consumer,
            {
                "csk-skill.json": _consumer_manifest(
                    {"provider": _requirement(provider_repo, mode="runtime")}
                )
            },
            tag="v1",
        )
    cfg = make_config(csk_home, skills_root, project, agents=["claude_code"])
    _save_config(monkeypatch, cfg)
    events: list[str] = []
    _install_fake_build_pipeline(monkeypatch, events=events)
    _fixed_evidence(monkeypatch)

    if node_kind == "global-marker-only":
        _write_global_skillfile(csk_home, [{"name": "consumer-a", "tag": "v1"}], agents=["claude_code"])
        assert cli.main(["global", "install"]) == 0
        marker_path = global_install.global_skills_root(csk_home) / "provider" / ".csk-install.json"
        assert _read_marker_json(marker_path)["activation"] == {"context": False, "commands": ["tool"]}

        _write_global_skillfile(
            csk_home,
            [{"name": "consumer-a", "tag": "v1"}, {"name": "consumer-b", "tag": "v1"}],
            agents=["claude_code"],
        )
        assert cli.main(["global", "status", "--check"]) == 1
        assert cli.main(["global", "upgrade"]) == 0
        assert _read_marker_json(marker_path)["requirers"] == ["consumer-a", "consumer-b"]
        assert events == ["build:tool"]
        assert cli.main(["global", "status", "--check"]) == 0
        return

    write_skillfile(
        project,
        {
            "schema_version": 1,
            "agents": ["claude_code"],
            "skills": [{"name": "consumer-a", "tag": "v1"}],
        },
    )
    assert cli.main(["install", "app"]) == 0
    marker_path = project / ".agents" / "skills" / "provider" / ".csk-install.json"
    assert _read_marker_json(marker_path)["activation"] == {"context": False, "commands": ["tool"]}

    write_skillfile(
        project,
        {
            "schema_version": 1,
            "agents": ["claude_code"],
            "skills": [
                {"name": "consumer-a", "tag": "v1"},
                {"name": "consumer-b", "tag": "v1"},
            ],
        },
    )
    assert cli.main(["status", "app", "--check"]) == 1
    assert cli.main(["upgrade", "app"]) == 0
    assert _read_marker_json(marker_path)["requirers"] == ["consumer-a", "consumer-b"]
    assert events == ["build:tool"]
    assert cli.main(["status", "app", "--check"]) == 0


@pytest.mark.parametrize("node_kind", ["hybrid-context", "hybrid-marker-only"])
def test_hybrid_nodes_refresh_on_closure_metadata_change_without_rebuilding(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    skills_root: Path,
    csk_home: Path,
    node_kind: str,
) -> None:
    # Hybrid context and hybrid marker-only provider markers omit requirers and refresh on non-requirers closure metadata drift without rebuilding.
    project = make_project(tmp_path)
    provider_repo, _ = make_skill_repo(
        skills_root, "provider", _build_skill_files("tool"), tag="v1"
    )
    mode = "runtime" if node_kind == "hybrid-marker-only" else "full"
    make_skill_repo(
        skills_root,
        "consumer-a",
        {
            "csk-skill.json": _consumer_manifest(
                {"provider": _requirement(provider_repo, mode=mode)}
            )
        },
        tag="v1",
    )
    write_skillfile(project, {"schema_version": 1, "agents": ["claude_code"], "skills": []})
    _write_hybrid_manifest(
        csk_home,
        [{"name": "consumer-a", "tag": "v1", "targets": [str(project)]}],
    )
    cfg = make_config(csk_home, skills_root, project, agents=["claude_code"])
    _save_config(monkeypatch, cfg)
    events: list[str] = []
    _install_fake_build_pipeline(monkeypatch, events=events)
    _fixed_evidence(monkeypatch)

    assert cli.main(["install", "app"]) == 0
    marker_path = hybrid.hybrid_skills_root(csk_home) / "provider" / ".csk-install.json"
    expected_context = node_kind == "hybrid-context"
    initial = _read_marker_json(marker_path)
    assert "requirers" not in initial
    assert initial["activation"] == {
        "context": expected_context,
        "commands": ["tool"],
    }
    assert cli.main(["status", "app", "--check"]) == 0

    drifted = dict(initial)
    drifted["source"] = "stale/provider"
    _write_marker_json(marker_path, drifted)
    # Positive control: non-requirers closure drift on the hybrid marker is caught by status --check.
    assert cli.main(["status", "app", "--check"]) == 1

    assert cli.main(["upgrade", "app"]) == 0
    refreshed = _read_marker_json(marker_path)
    assert refreshed["source"] == initial["source"]
    assert "requirers" not in refreshed
    assert events == ["build:tool"]
    assert cli.main(["status", "app", "--check"]) == 0


def test_hybrid_shared_store_omits_requirers_across_projects_and_migrates_legacy_marker(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    skills_root: Path,
    csk_home: Path,
) -> None:
    # Two projects sharing a hybrid-store skill with different consumers keep the shared marker byte-identical and both statuses clean, while a legacy hybrid marker carrying requirers migrates once without rebuilding and non-hybrid scopes still record requirers.
    project_a = make_project(tmp_path, "project-a")
    project_b = make_project(tmp_path, "project-b")
    provider_repo, _ = make_skill_repo(
        skills_root, "provider", _build_skill_files("tool"), tag="v1"
    )
    local_provider_repo, _ = make_skill_repo(
        skills_root, "local-provider", _script_skill_files("local-tool"), tag="v1"
    )
    for consumer in ("consumer-a", "consumer-b"):
        make_skill_repo(
            skills_root,
            consumer,
            {"csk-skill.json": _consumer_manifest({"provider": _requirement(provider_repo)})},
            tag="v1",
        )
    make_skill_repo(
        skills_root,
        "local-consumer",
        {"csk-skill.json": _consumer_manifest({"local-provider": _requirement(local_provider_repo)})},
        tag="v1",
    )
    write_skillfile(
        project_a,
        {
            "schema_version": 1,
            "agents": ["claude_code"],
            "skills": [{"name": "local-consumer", "tag": "v1"}],
        },
    )
    write_skillfile(project_b, {"schema_version": 1, "agents": ["claude_code"], "skills": []})
    _write_hybrid_manifest(
        csk_home,
        [
            {"name": "consumer-a", "tag": "v1", "targets": [str(project_a), str(project_b)]},
            {"name": "consumer-b", "tag": "v1", "targets": [str(project_b)]},
        ],
    )
    _write_global_skillfile(
        csk_home,
        [{"name": "local-consumer", "tag": "v1"}],
        agents=["claude_code"],
    )
    cfg = replace(
        make_config(csk_home, skills_root, project_a, agents=["claude_code"]),
        projects={
            "app-a": config_mod.ProjectConfig(alias="app-a", path=project_a, agents=["claude_code"]),
            "app-b": config_mod.ProjectConfig(alias="app-b", path=project_b, agents=["claude_code"]),
        },
    )
    _save_config(monkeypatch, cfg)
    events: list[str] = []
    _install_fake_build_pipeline(monkeypatch, events=events)
    _fixed_evidence(monkeypatch)

    assert cli.main(["install", "app-a"]) == 0
    assert cli.main(["install", "app-b"]) == 0
    assert cli.main(["global", "install"]) == 0
    assert events == ["build:tool"]

    hybrid_marker = hybrid.hybrid_skills_root(csk_home) / "provider" / ".csk-install.json"
    hybrid_payload = _read_marker_json(hybrid_marker)
    assert "requirers" not in hybrid_payload
    assert cli.main(["status", "app-a", "--check"]) == 0
    assert cli.main(["status", "app-b", "--check"]) == 0

    # Alternating installs across projects with different consumer sets keep the hybrid marker byte-identical.
    before_bytes = _set_old_installed_at(hybrid_marker)
    for alias in ("app-a", "app-b", "app-a"):
        res = installer.install(cfg, alias=alias)[0]
        assert not res.errors, res.errors
        assert any("provider" in msg and msg.endswith(" up-to-date") for msg in res.messages), res.messages
    assert hybrid_marker.read_bytes() == before_bytes
    assert events == ["build:tool"]
    assert cli.main(["status", "app-a", "--check"]) == 0
    assert cli.main(["status", "app-b", "--check"]) == 0

    # One-time migration: an existing hybrid marker still carrying requirers rewrites once without rebuilding or altering content_sha256.
    original_content_sha = hybrid_payload["content_sha256"]
    legacy_hybrid = _read_marker_json(hybrid_marker)
    legacy_hybrid["requirers"] = ["consumer-a"]
    legacy_hybrid["installed_at"] = OLD_INSTALLED_AT
    _write_marker_json(hybrid_marker, legacy_hybrid)
    assert cli.main(["status", "app-a", "--check"]) == 1
    assert cli.main(["status", "app-b", "--check"]) == 1

    migrated_res = installer.install(cfg, alias="app-a")[0]
    assert not migrated_res.errors, migrated_res.errors
    assert any("provider" in msg and msg.endswith(" installed") for msg in migrated_res.messages), migrated_res.messages
    migrated = _read_marker_json(hybrid_marker)
    assert "requirers" not in migrated
    assert migrated["content_sha256"] == original_content_sha
    assert events == ["build:tool"]
    assert cli.main(["status", "app-a", "--check"]) == 0
    assert cli.main(["status", "app-b", "--check"]) == 0

    post_migration_bytes = _set_old_installed_at(hybrid_marker)
    second_res = installer.install(cfg, alias="app-b")[0]
    assert not second_res.errors, second_res.errors
    assert any("provider" in msg and msg.endswith(" up-to-date") for msg in second_res.messages), second_res.messages
    assert hybrid_marker.read_bytes() == post_migration_bytes
    assert events == ["build:tool"]

    # Positive control: project-local and global markers still record and compare requirers.
    local_marker = project_a / ".agents" / "skills" / "local-provider" / ".csk-install.json"
    global_marker = global_install.global_skills_root(csk_home) / "local-provider" / ".csk-install.json"
    assert _read_marker_json(local_marker)["requirers"] == ["local-consumer"]
    assert _read_marker_json(global_marker)["requirers"] == ["local-consumer"]
    for scope_marker, status_cmd in (
        (local_marker, ["status", "app-a", "--check"]),
        (global_marker, ["global", "status", "--check"]),
    ):
        stale = _read_marker_json(scope_marker)
        stale["requirers"] = ["local-consumer", "ghost-consumer"]
        _write_marker_json(scope_marker, stale)
        assert cli.main(status_cmd) == 1


def test_global_selective_only_preserves_unselected_requirers_and_retention(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    skills_root: Path,
    csk_home: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    # Selective global runs (--only provider / --only consumer) preserve unselected live requirers and <project> when directly declared, keep global status --check clean and retain not-revalidated warnings.
    project = make_project(tmp_path)
    provider_repo, _ = make_skill_repo(skills_root, "provider", _script_skill_files("tool"), tag="v1")
    make_skill_repo(
        skills_root,
        "consumer-a",
        {"csk-skill.json": _consumer_manifest({"provider": _requirement(provider_repo)})},
        tag="v1",
    )
    consumer_b_repo, _ = make_skill_repo(
        skills_root,
        "consumer-b",
        {"csk-skill.json": _consumer_manifest({"provider": _requirement(provider_repo)})},
        tag="v1",
    )
    write_files(
        consumer_b_repo,
        {"csk-skill.json": json.dumps({"schema_version": 4, "capabilities": CAPS})},
    )
    commit_all(consumer_b_repo, "drop provider requirement")
    run(["git", "tag", "v2"], consumer_b_repo)

    cfg = make_config(csk_home, skills_root, project, agents=["codex_cli"])
    _save_config(monkeypatch, cfg)
    global_marker = global_install.global_skills_root(csk_home) / "provider" / ".csk-install.json"

    # Case 1: provider is directly declared alongside two consumers.
    _write_global_skillfile(
        csk_home,
        [
            {"name": "consumer-a", "tag": "v1"},
            {"name": "consumer-b", "tag": "v1"},
            {"name": "provider", "tag": "v1"},
        ],
    )
    assert cli.main(["global", "install"]) == 0
    assert _read_marker_json(global_marker)["requirers"] == ["<project>", "consumer-a", "consumer-b"]
    assert cli.main(["global", "status", "--check"]) == 0

    before_bytes = _set_old_installed_at(global_marker)
    capsys.readouterr()

    # --only provider keeps requirers intact, leaves marker byte-identical, and keeps retention warning alive across repeated runs.
    for _ in range(2):
        assert cli.main(["global", "install", "--only", "provider"]) == 0
        out = capsys.readouterr().out
        assert "consumer-a" in out and "consumer-b" in out and "were not revalidated" in out
        assert global_marker.read_bytes() == before_bytes
        assert _read_marker_json(global_marker)["requirers"] == ["<project>", "consumer-a", "consumer-b"]
        assert cli.main(["global", "status", "--check"]) == 0
        capsys.readouterr()

    # --only consumer-a also preserves both the retained consumer-b and direct <project> requirer.
    assert cli.main(["global", "install", "--only", "consumer-a"]) == 0
    out_consumer = capsys.readouterr().out
    assert "consumer-b" in out_consumer and "were not revalidated" in out_consumer
    assert global_marker.read_bytes() == before_bytes
    assert _read_marker_json(global_marker)["requirers"] == ["<project>", "consumer-a", "consumer-b"]
    assert cli.main(["global", "status", "--check"]) == 0
    capsys.readouterr()

    # Even when --only provider actually rewrites the marker (e.g., new commit on branch), unselected requirers are preserved.
    write_files(provider_repo, {"SKILL.md": "---\nname: test\n---\n\n# Updated\n"})
    new_commit = commit_all(provider_repo, "provider update")
    run(["git", "tag", "v2"], provider_repo)
    _write_global_skillfile(
        csk_home,
        [
            {"name": "consumer-a", "tag": "v1"},
            {"name": "consumer-b", "tag": "v1"},
            {"name": "provider", "tag": "v2"},
        ],
    )
    capsys.readouterr()
    assert cli.main(["global", "upgrade", "--only", "provider"]) == 0
    upgrade_out = capsys.readouterr().out
    assert "consumer-a" in upgrade_out and "consumer-b" in upgrade_out and "were not revalidated" in upgrade_out
    upgraded_marker = _read_marker_json(global_marker)
    assert upgraded_marker["commit"] == new_commit
    assert upgraded_marker["requirers"] == ["<project>", "consumer-a", "consumer-b"]

    # Repeat --only provider after the rewrite: retention still warns about both consumers.
    assert cli.main(["global", "install", "--only", "provider"]) == 0
    repeat_out = capsys.readouterr().out
    assert "consumer-a" in repeat_out and "consumer-b" in repeat_out and "were not revalidated" in repeat_out

    # Case 2: provider is NOT directly declared (only transitively required by consumer-a and consumer-b).
    _write_global_skillfile(
        csk_home,
        [
            {"name": "consumer-a", "tag": "v1"},
            {"name": "consumer-b", "tag": "v1"},
        ],
    )
    assert cli.main(["global", "install"]) == 0
    assert _read_marker_json(global_marker)["requirers"] == ["consumer-a", "consumer-b"]
    transitive_bytes = _set_old_installed_at(global_marker)
    capsys.readouterr()

    assert cli.main(["global", "install", "--only", "consumer-a"]) == 0
    out_transitive = capsys.readouterr().out
    assert "consumer-b" in out_transitive and "were not revalidated" in out_transitive
    assert global_marker.read_bytes() == transitive_bytes
    assert _read_marker_json(global_marker)["requirers"] == ["consumer-a", "consumer-b"]
    assert cli.main(["global", "status", "--check"]) == 0

    # Positive control: a full global install (without --only) shrinks requirers once consumer-b drops provider.
    _write_global_skillfile(
        csk_home,
        [
            {"name": "consumer-a", "tag": "v1"},
            {"name": "consumer-b", "tag": "v2"},
        ],
    )
    assert cli.main(["global", "install"]) == 0
    assert _read_marker_json(global_marker)["requirers"] == ["consumer-a"]
    assert cli.main(["global", "status", "--check"]) == 0


def test_requirers_shrinking_refreshes_marker_and_updates_global_retention(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    skills_root: Path,
    csk_home: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    # Removing a requirer refreshes the provider marker so status --check passes and global selective retention stops warning about the removed requirer.
    project = make_project(tmp_path)
    provider_repo, _ = make_skill_repo(skills_root, "provider", _script_skill_files("tool"), tag="v1")
    make_skill_repo(
        skills_root,
        "consumer-a",
        {"csk-skill.json": _consumer_manifest({"provider": _requirement(provider_repo)})},
        tag="v1",
    )
    consumer_b_repo, _ = make_skill_repo(
        skills_root,
        "consumer-b",
        {"csk-skill.json": _consumer_manifest({"provider": _requirement(provider_repo)})},
        tag="v1",
    )
    # Tag v2 of consumer-b drops its requirement on provider while staying installed globally.
    write_files(
        consumer_b_repo,
        {"csk-skill.json": json.dumps({"schema_version": 4, "capabilities": CAPS})},
    )
    commit_all(consumer_b_repo, "drop provider requirement")
    run(["git", "tag", "v2"], consumer_b_repo)

    cfg = make_config(csk_home, skills_root, project, agents=["codex_cli"])
    _save_config(monkeypatch, cfg)

    # Project shrink check.
    write_skillfile(
        project,
        {
            "schema_version": 1,
            "agents": ["codex_cli"],
            "skills": [
                {"name": "consumer-a", "tag": "v1"},
                {"name": "consumer-b", "tag": "v1"},
            ],
        },
    )
    assert cli.main(["install", "app"]) == 0
    proj_marker = project / ".agents" / "skills" / "provider" / ".csk-install.json"
    assert _read_marker_json(proj_marker)["requirers"] == ["consumer-a", "consumer-b"]

    write_skillfile(
        project,
        {
            "schema_version": 1,
            "agents": ["codex_cli"],
            "skills": [{"name": "consumer-a", "tag": "v1"}],
        },
    )
    assert cli.main(["status", "app", "--check"]) == 1
    assert cli.main(["install", "app"]) == 0
    assert _read_marker_json(proj_marker)["requirers"] == ["consumer-a"]
    assert cli.main(["status", "app", "--check"]) == 0

    # Global shrink + retention check.
    _write_global_skillfile(
        csk_home,
        [
            {"name": "consumer-a", "tag": "v1"},
            {"name": "consumer-b", "tag": "v1"},
            {"name": "provider", "tag": "v1"},
        ],
    )
    assert cli.main(["global", "install"]) == 0
    global_marker = global_install.global_skills_root(csk_home) / "provider" / ".csk-install.json"
    assert _read_marker_json(global_marker)["requirers"] == ["<project>", "consumer-a", "consumer-b"]
    capsys.readouterr()

    # Positive control: while consumer-b still requires provider, --only provider warns about both without shrinking requirers.
    assert cli.main(["global", "install", "--only", "provider"]) == 0
    before_warning = capsys.readouterr().out
    assert "consumer-a" in before_warning and "consumer-b" in before_warning
    assert _read_marker_json(global_marker)["requirers"] == ["<project>", "consumer-a", "consumer-b"]
    assert cli.main(["global", "status", "--check"]) == 0

    _write_global_skillfile(
        csk_home,
        [
            {"name": "consumer-a", "tag": "v1"},
            {"name": "consumer-b", "tag": "v2"},
            {"name": "provider", "tag": "v1"},
        ],
    )
    assert cli.main(["global", "status", "--check"]) == 1
    assert cli.main(["global", "upgrade"]) == 0
    assert _read_marker_json(global_marker)["requirers"] == ["<project>", "consumer-a"]
    assert cli.main(["global", "status", "--check"]) == 0
    capsys.readouterr()

    assert cli.main(["global", "install", "--only", "provider"]) == 0
    after_warning = capsys.readouterr().out
    assert "consumer-a" in after_warning
    assert "consumer-b" not in after_warning
    assert _read_marker_json(global_marker)["requirers"] == ["<project>", "consumer-a"]
    assert cli.main(["global", "status", "--check"]) == 0


@pytest.mark.parametrize(
    "field_name",
    ["requirers", "dependencies", "requirements", "runtime_roots", "source", "git"],
)
def test_closure_metadata_field_drift_refreshes_end_to_end(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    skills_root: Path,
    csk_home: Path,
    field_name: str,
) -> None:
    # Project install refreshes a marker whose single closure-metadata field differs from the current closure at the same commit.
    project = make_project(tmp_path)
    dep_repo, _ = make_skill_repo(skills_root, "dep-skill", _script_skill_files("dep-cmd"), tag="v1")
    subject_repo, _ = make_skill_repo(
        skills_root,
        "subject",
        {
            "csk-skill.json": json.dumps(
                {
                    "schema_version": 4,
                    "capabilities": CAPS,
                    "runtime_roots": ["scripts"],
                    "commands": {
                        "sub-cmd": {
                            "type": "script",
                            "unix_path": "scripts/sub-cmd",
                            "win_path": "scripts/sub-cmd.cmd",
                        }
                    },
                    "dependencies": {
                        "commands": {
                            "dep-cmd": {
                                "type": "skill",
                                "skill": "dep-skill",
                                "command": "dep-cmd",
                                "hint": "install dep-skill",
                            }
                        },
                        "skills": {
                            "dep-skill": _requirement(dep_repo),
                        },
                    },
                }
            ),
            "scripts/sub-cmd": "#!/bin/sh\necho sub\n",
            "scripts/sub-cmd.cmd": "@echo off\r\necho sub\r\n",
        },
        tag="v1",
    )
    write_skillfile(
        project,
        {
            "schema_version": 1,
            "agents": ["claude_code"],
            "skills": [{"name": "subject", "tag": "v1", "git": str(subject_repo)}],
        },
    )
    cfg = make_config(csk_home, skills_root, project, agents=["claude_code"])
    _save_config(monkeypatch, cfg)

    # Positive control: clean install starts up-to-date.
    assert cli.main(["install", "app"]) == 0
    assert cli.main(["status", "app", "--check"]) == 0

    marker_path = project / ".agents" / "skills" / "subject" / ".csk-install.json"
    original = _read_marker_json(marker_path)
    drifted = dict(original)
    if field_name == "requirers":
        drifted["requirers"] = ["<project>", "stale-consumer"]
    elif field_name == "dependencies":
        drifted["dependencies"] = []
    elif field_name == "requirements":
        drifted.pop("requirements", None)
    elif field_name == "runtime_roots":
        drifted["runtime_roots"] = []
    elif field_name == "source":
        drifted["source"] = "stale/subject"
    elif field_name == "git":
        drifted.pop("git", None)
    else:
        raise AssertionError(f"unexpected field: {field_name}")
    _write_marker_json(marker_path, drifted)

    # Positive control: drifted marker is caught by status --check before install.
    assert cli.main(["status", "app", "--check"]) == 1
    before_status = status.collect_status(cfg)[0]
    subject_before = next(row for row in before_status.skills if row.name == "subject")
    assert subject_before.label == "manifest-drift"

    result = installer.install(cfg)[0]
    assert not result.errors, result.errors
    assert any("subject" in msg and msg.endswith(" installed") for msg in result.messages)
    refreshed = _read_marker_json(marker_path)
    assert refreshed.get(field_name) == original.get(field_name)
    assert cli.main(["status", "app", "--check"]) == 0


def test_unchanged_closure_keeps_marker_bytes_identical(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    skills_root: Path,
    csk_home: Path,
) -> None:
    # Negative control: an unchanged closure leaves project, build-provider, hybrid, marker-only and global markers byte-identical (including installed_at) and reports up-to-date.
    project = make_project(tmp_path)
    provider_repo, _ = make_skill_repo(
        skills_root, "provider", _build_skill_files("tool"), tag="v1"
    )
    make_skill_repo(
        skills_root,
        "consumer-a",
        {
            "csk-skill.json": _consumer_manifest(
                {"provider": _requirement(provider_repo, mode="runtime")}
            )
        },
        tag="v1",
    )
    make_skill_repo(skills_root, "hybrid-skill", _script_skill_files("hyb"), tag="v1")
    write_skillfile(
        project,
        {
            "schema_version": 1,
            "agents": ["claude_code"],
            "skills": [{"name": "consumer-a", "tag": "v1"}],
        },
    )
    _write_hybrid_manifest(
        csk_home,
        [{"name": "hybrid-skill", "tag": "v1", "targets": [str(project)]}],
    )
    _write_global_skillfile(
        csk_home,
        [{"name": "consumer-a", "tag": "v1"}],
        agents=["claude_code"],
    )
    cfg = make_config(csk_home, skills_root, project, agents=["claude_code"])
    _save_config(monkeypatch, cfg)
    events: list[str] = []
    _install_fake_build_pipeline(monkeypatch, events=events)
    _fixed_evidence(monkeypatch)

    assert cli.main(["install", "app"]) == 0
    assert cli.main(["global", "install"]) == 0
    assert events == ["build:tool"]

    marker_paths = {
        "project-context": project / ".agents" / "skills" / "consumer-a" / ".csk-install.json",
        "project-marker-only-build": project / ".agents" / "skills" / "provider" / ".csk-install.json",
        "hybrid-context": hybrid.hybrid_skills_root(csk_home) / "hybrid-skill" / ".csk-install.json",
        "global-context": global_install.global_skills_root(csk_home) / "consumer-a" / ".csk-install.json",
        "global-marker-only-build": global_install.global_skills_root(csk_home) / "provider" / ".csk-install.json",
    }
    assert "requirers" not in _read_marker_json(marker_paths["hybrid-context"])
    before_bytes = {label: _set_old_installed_at(path) for label, path in marker_paths.items()}

    proj_result = installer.install(cfg, options=installer.InstallOptions(fetch=True))[0]
    assert not proj_result.errors, proj_result.errors
    for name in ("consumer-a", "provider", "hybrid-skill"):
        assert any(name in msg and msg.endswith(" up-to-date") for msg in proj_result.messages), proj_result.messages

    glob_result = global_install.install(cfg, options=installer.InstallOptions(fetch=True))
    assert not glob_result.errors, glob_result.errors
    for name in ("consumer-a", "provider"):
        assert any(name in msg and msg.endswith(" up-to-date") for msg in glob_result.messages), glob_result.messages

    assert events == ["build:tool"]
    for label, path in marker_paths.items():
        assert path.read_bytes() == before_bytes[label], f"{label} marker bytes changed"
    assert cli.main(["status", "app", "--check"]) == 0
    assert cli.main(["global", "status", "--check"]) == 0

    # Positive control: once requirers actually drift on the same marker, bytes DO update.
    drifted = _read_marker_json(marker_paths["project-marker-only-build"])
    drifted["requirers"] = ["consumer-a", "extra"]
    _write_marker_json(marker_paths["project-marker-only-build"], drifted)
    rerun = installer.install(cfg)[0]
    assert not rerun.errors, rerun.errors
    assert any("provider" in msg and msg.endswith(" installed") for msg in rerun.messages)
    assert _read_marker_json(marker_paths["project-marker-only-build"])["installed_at"] != OLD_INSTALLED_AT


def _parity_fixture(
    tmp_path: Path,
) -> tuple[
    closure.ClosureNode,
    installer.SkillPlan,
    Path,
    Path,
    dict[str, object],
    dict[str, object],
    build_source.BuildSourceIdentity,
    dict[str, install_marker.InstallMarkerBuild],
]:
    commit = "1" * 40
    installed = tmp_path / "installed" / "demo"
    installed.mkdir(parents=True)
    (installed / "SKILL.md").write_text("---\nname: demo\n---\n\n# Demo\n", encoding="utf-8")
    runtime_dir = tmp_path / "runtime" / "demo" / commit
    content_hash = hashing.content_sha256(installed)
    build_src = build_source.BuildSourceIdentity(
        algorithm=hashing.BUILD_SOURCE_ALGORITHM,
        content_sha256="sha256:" + "a" * 64,
    )
    build_record = install_marker.InstallMarkerBuild(
        driver="go-v1",
        cache_key="sha256:" + "b" * 64,
        receipt_sha256="sha256:" + "c" * 64,
        artifact_sha256="sha256:" + "d" * 64,
        artifact_path="bin/tool",
    )
    spec = skillspec.SkillSpec(
        commands={
            "run": skillspec.CommandSpec(
                name="run",
                type="script",
                unix_path="scripts/run",
                win_path="scripts/run.cmd",
            ),
            "tool": skillspec.CommandSpec(
                name="tool",
                type="build",
                driver="go-v1",
                source_dir="build/cmd/tool",
            ),
        },
        source_file="agent-skill.json",
        schema_version=6,
        runtime_roots=("scripts",),
        dependencies={
            "dep-cmd": skillspec.DependencySpec(
                name="dep-cmd",
                type="skill",
                command="dep-cmd",
                hint="install dep",
            )
        },
        requirements={
            "req-skill": skillspec.SkillRequirement(
                name="req-skill",
                git="https://example.invalid/org/req-skill.git",
                ref_kind="tag",
                ref_value="v1",
            )
        },
        build_roots=("build",),
    )
    decl = manifest.SkillDecl(
        name="demo",
        source="demo",
        ref=manifest.SkillRef(kind="tag", value="v1"),
        git="https://example.invalid/org/demo.git",
    )
    resolved = git_ops_ref(commit)
    node = closure.ClosureNode(
        name="demo",
        decl=decl,
        resolved=resolved,
        repo=tmp_path / "repo",
        snapshot=tmp_path / "snapshot",
        spec=spec,
        identity="example.invalid/org/demo",
        substituted="path /local/demo",
        edges=[closure.ActivationEdge(consumer="consumer-a", mode="full")],
    )
    plan = installer.SkillPlan(
        decl=decl,
        resolved=resolved,
        repo=node.repo,
        snapshot=node.snapshot,
        spec=spec,
    )
    activation = {"context": True, "commands": ["run", "tool"]}
    marker = install_marker.InstallMarkerV2(
        name="demo",
        source="demo",
        ref_kind="tag",
        ref="v1",
        commit=commit,
        content_sha256=content_hash,
        locale="en",
        agents=("claude_code",),
        commands=("run", "tool"),
        dependencies=("dep-cmd",),
        skill_schema_version=6,
        runtime_roots=("scripts",),
        installed_at=OLD_INSTALLED_AT,
        files=("SKILL.md",),
        git="https://example.invalid/org/demo.git",
        requirements=("req-skill",),
        mcp_servers=None,
        attestation=None,
        activation=install_marker.MarkerActivation(context=True, commands=("run", "tool")),
        requirers=("consumer-a",),
        substituted="path /local/demo",
        build_roots=("build",),
        builds={"tool": build_record},
        build_source=build_src,
    ).to_json()
    return node, plan, installed, runtime_dir, marker, activation, build_src, {"tool": build_record}


def git_ops_ref(commit: str):
    from csk import git_ops

    return git_ops.ResolvedRef(kind="tag", ref="v1", commit=commit)


def _mutate_marker_field(base: dict[str, object], field_name: str) -> dict[str, object]:
    mutated = json.loads(json.dumps(base))
    if field_name == "name":
        mutated["name"] = "other-demo"
    elif field_name == "source":
        mutated["source"] = "team/demo"
    elif field_name == "ref_kind":
        mutated["ref_kind"] = "branch"
    elif field_name == "ref":
        mutated["ref"] = "v2"
    elif field_name == "commit":
        mutated["commit"] = "2" * 40
    elif field_name == "git":
        mutated["git"] = "https://example.invalid/org/other.git"
    elif field_name == "locale":
        mutated["locale"] = "ru"
    elif field_name == "agents":
        mutated["agents"] = ["codex_cli"]
    elif field_name == "commands":
        mutated["commands"] = ["other-cmd", "run", "tool"]
    elif field_name == "dependencies":
        mutated["dependencies"] = ["other-dep"]
    elif field_name == "skill_schema_version":
        mutated["skill_schema_version"] = 5
        mutated["builds"] = {}
        mutated.pop("build_source", None)
    elif field_name == "runtime_roots":
        mutated["runtime_roots"] = ["bin"]
    elif field_name == "requirements":
        mutated["requirements"] = ["other-req"]
    elif field_name == "activation":
        mutated["activation"] = {"context": False, "commands": ["run", "tool"]}
    elif field_name == "requirers":
        mutated["requirers"] = ["consumer-a", "consumer-b"]
    elif field_name == "substituted":
        mutated["substituted"] = "path /other/demo"
    elif field_name == "installed_at":
        mutated["installed_at"] = "2025-05-05T05:05:05Z"
    elif field_name == "content_sha256":
        mutated["content_sha256"] = "sha256:" + "9" * 64
    elif field_name == "files":
        mutated["files"] = ["SKILL.md", "extra.md"]
    elif field_name == "mcp_servers":
        mutated["mcp_servers"] = {"docs": ["claude_code"]}
    elif field_name == "attestation":
        mutated["attestation"] = {"registry": "https://reg.invalid", "status": "audited"}
    elif field_name == "build_roots":
        mutated["build_roots"] = ["build", "extra-build"]
    elif field_name == "build_source":
        mutated["build_source"] = {
            "algorithm": hashing.BUILD_SOURCE_ALGORITHM,
            "content_sha256": "sha256:" + "f" * 64,
        }
    elif field_name == "builds":
        mutated["builds"]["tool"]["cache_key"] = "sha256:" + "e" * 64
    else:
        raise AssertionError(f"no mutation defined for field {field_name!r}")
    return mutated


@pytest.mark.parametrize(
    "field_name",
    [*install_marker.LEGACY_MARKER_CLOSURE_FIELDS, "files"],
)
def test_shared_closure_field_list_drives_both_status_and_installer(
    tmp_path: Path,
    field_name: str,
) -> None:
    # Every field in LEGACY_MARKER_CLOSURE_FIELDS (plus files) triggers both status non-currentness and installer refresh, with a positive control.
    node, plan, installed, runtime_dir, base_marker, activation, build_src, builds = _parity_fixture(tmp_path)
    marker_path = installed / ".csk-install.json"

    # Positive control: matching marker is current in both status and installer.
    _write_marker_json(marker_path, base_marker)
    clean_inspection = status._inspect_node_marker(
        node,
        installed,
        runtime_dir=runtime_dir,
        effective_locale="en",
        agents=["claude_code"],
        expected_build_source=build_src,
    )
    assert clean_inspection.status.label == "up-to-date"
    assert installer._marker_is_current(
        base_marker,
        installed,
        plan,
        "en",
        ["claude_code"],
        activation=activation,
        requirers=["consumer-a"],
        substituted="path /local/demo",
        builds=builds,
        build_source_identity=build_src,
    )

    mutated = _mutate_marker_field(base_marker, field_name)
    _write_marker_json(marker_path, mutated)
    drifted_inspection = status._inspect_node_marker(
        node,
        installed,
        runtime_dir=runtime_dir,
        effective_locale="en",
        agents=["claude_code"],
        expected_build_source=build_src,
    )
    assert drifted_inspection.status.label != "up-to-date", f"status ignored {field_name}"
    assert not installer._marker_is_current(
        mutated,
        installed,
        plan,
        "en",
        ["claude_code"],
        activation=activation,
        requirers=["consumer-a"],
        substituted="path /local/demo",
        builds=builds,
        build_source_identity=build_src,
    ), f"installer ignored {field_name}"


def test_status_and_installer_marker_field_parity_guard(tmp_path: Path) -> None:
    # Guard: every InstallMarkerV2 field is accounted for, and no field can make status non-current while installer still treats it as up-to-date.
    common_fields = {f.name for f in dataclass_fields(install_marker._InstallMarkerCommon)}
    closure_fields = set(install_marker.LEGACY_MARKER_CLOSURE_FIELDS)
    explicit_non_closure_common = {
        "content_sha256",
        "files",
        "installed_at",
        "mcp_servers",
        "attestation",
    }
    assert closure_fields.isdisjoint(explicit_non_closure_common)
    assert closure_fields | explicit_non_closure_common == common_fields

    node, plan, installed, runtime_dir, base_marker, activation, build_src, builds = _parity_fixture(tmp_path)
    marker_path = installed / ".csk-install.json"

    # Positive control: installed_at alone must not invalidate status or installer.
    ts_only = _mutate_marker_field(base_marker, "installed_at")
    _write_marker_json(marker_path, ts_only)
    assert (
        status._inspect_node_marker(
            node,
            installed,
            runtime_dir=runtime_dir,
            effective_locale="en",
            agents=["claude_code"],
            expected_build_source=build_src,
        ).status.label
        == "up-to-date"
    )
    assert installer._marker_is_current(
        ts_only,
        installed,
        plan,
        "en",
        ["claude_code"],
        activation=activation,
        requirers=["consumer-a"],
        substituted="path /local/demo",
        builds=builds,
        build_source_identity=build_src,
    )

    v2_fields = [
        f.name
        for f in dataclass_fields(install_marker.InstallMarkerV2)
        if f.name != "schema_version"
    ]
    for field_name in v2_fields:
        mutated = _mutate_marker_field(base_marker, field_name)
        _write_marker_json(marker_path, mutated)
        status_current = (
            status._inspect_node_marker(
                node,
                installed,
                runtime_dir=runtime_dir,
                effective_locale="en",
                agents=["claude_code"],
                expected_build_source=build_src,
            ).status.label
            == "up-to-date"
        )
        installer_current = installer._marker_is_current(
            mutated,
            installed,
            plan,
            "en",
            ["claude_code"],
            activation=activation,
            requirers=["consumer-a"],
            substituted="path /local/demo",
            builds=builds,
            build_source_identity=build_src,
        )
        assert not (not status_current and installer_current), (
            f"status marked {field_name!r} non-current while installer reported up-to-date"
        )


def test_status_inspect_node_marker_adds_no_status_only_marker_fields() -> None:
    # Structural guard: status._inspect_node_marker must delegate closure checks to legacy_marker_matches_closure and never read ad-hoc closure fields directly.
    source = textwrap.dedent(inspect.getsource(status._inspect_node_marker))
    tree = ast.parse(source)

    calls_shared_matcher = any(
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "legacy_marker_matches_closure"
        for node in ast.walk(tree)
    )
    assert calls_shared_matcher, (
        "status._inspect_node_marker must call install_marker.legacy_marker_matches_closure"
    )

    direct_marker_attrs = {
        node.attr
        for node in ast.walk(tree)
        if isinstance(node, ast.Attribute)
        and isinstance(node.value, ast.Name)
        and node.value.id in {"marker", "legacy_marker"}
    }
    allowed_direct_attrs = {
        "commit",
        "content_sha256",
        "files",
        "build_roots",
        "build_source",
        "builds",
        "to_json",
    }
    unexpected = direct_marker_attrs - allowed_direct_attrs
    assert not unexpected, (
        f"status._inspect_node_marker read marker field(s) outside the shared closure check: {sorted(unexpected)}"
    )
