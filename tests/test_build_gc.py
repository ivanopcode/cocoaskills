from __future__ import annotations

import json
import os
import shutil
from pathlib import Path

import pytest
from conftest import make_config, make_project, make_skill_repo, write_skillfile
from test_build_currentness import _installed_build

from csk import gc, install_marker, installer, locking, transactions
from csk.builds import cache_windows
from csk.config import GlobalConfig


POSIX_BUILD_GC = pytest.mark.skipif(
    os.name != "posix",
    reason="Exercises the POSIX protected build cache collector",
)


def _cache_entry(csk_home: Path, marker: dict[str, object]) -> Path:
    record = marker["builds"]["tool"]  # type: ignore[index]
    return (
        csk_home
        / "builds"
        / "go-v1"
        / record["cache_key"].removeprefix("sha256:")
    )


def _without_projects(config: GlobalConfig) -> GlobalConfig:
    return GlobalConfig(
        path=config.path,
        skills_root=config.skills_root,
        preferred_locale=config.preferred_locale,
        default_agents=config.default_agents,
        adapter_mode=config.adapter_mode,
        worktree_alias_pattern=config.worktree_alias_pattern,
        projects={},
        audit=config.audit,
        allowed_sources=config.allowed_sources,
        audit_registries=config.audit_registries,
        disable_builtin_registries=config.disable_builtin_registries,
    )


def _age_cache_entry(entry: Path, timestamp: float) -> None:
    if os.name == "nt":
        from test_build_cache_windows import _protect

        _protect(entry, cache_windows._MUTABLE_DIRECTORY)
        os.utime(entry, (timestamp, timestamp))
        _protect(entry, cache_windows._SEALED_ENTRY)
        return
    os.utime(entry, (timestamp, timestamp), follow_symlinks=False)


def test_build_gc_marks_project_marker_and_applies_grace_before_sweep(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    skills_root: Path,
    csk_home: Path,
) -> None:
    project, config, _events, marker_path, marker = _installed_build(
        monkeypatch,
        tmp_path,
        skills_root,
        csk_home,
    )
    entry = _cache_entry(csk_home, marker)
    _age_cache_entry(entry, 1.0)

    marked = gc.collect_runtime(
        config,
        csk_home,
        now=gc.BUILD_GRACE_SECONDS + 100,
    )
    assert marked.builds_removed == 0
    assert entry.exists()

    shutil.rmtree(marker_path.parent)
    _age_cache_entry(entry, 2.0)
    swept = gc.collect_runtime(
        config,
        csk_home,
        now=gc.BUILD_GRACE_SECONDS + 200,
    )
    assert swept.builds_removed == 1
    assert not entry.exists()

    # A new unreferenced entry remains within the grace window.
    young_skills = tmp_path / "young-skills"
    young_skills.mkdir()
    _project, _config, _events, _marker_path, marker = _installed_build(
        monkeypatch,
        tmp_path / "young",
        young_skills,
        csk_home,
    )
    young_entry = _cache_entry(csk_home, marker)
    shutil.rmtree(_marker_path.parent)
    recent = 10_000.0
    _age_cache_entry(young_entry, recent)
    young = gc.collect_runtime(
        _without_projects(_config),
        csk_home,
        now=recent + gc.BUILD_GRACE_SECONDS - 1,
    )
    assert young.builds_removed == 0
    assert young_entry.exists()
    assert project.exists()


@pytest.mark.parametrize("scope", ["global", "hybrid"])
@POSIX_BUILD_GC
def test_build_gc_marks_valid_global_and_hybrid_marker_v2(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    skills_root: Path,
    csk_home: Path,
    scope: str,
) -> None:
    _project, config, _events, marker_path, marker = _installed_build(
        monkeypatch,
        tmp_path,
        skills_root,
        csk_home,
    )
    entry = _cache_entry(csk_home, marker)
    target = csk_home / scope / "skills" / "build-skill"
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copytree(marker_path.parent, target)
    shutil.rmtree(marker_path.parent)
    os.utime(entry, (1, 1), follow_symlinks=False)

    stats = gc.collect_runtime(
        _without_projects(config),
        csk_home,
        now=gc.BUILD_GRACE_SECONDS + 100,
    )

    assert stats.builds_removed == 0
    assert not stats.warnings
    assert entry.exists()


@POSIX_BUILD_GC
def test_active_transaction_journal_marks_staged_marker_build_key(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    skills_root: Path,
    csk_home: Path,
) -> None:
    _project, config, _events, marker_path, marker = _installed_build(
        monkeypatch,
        tmp_path,
        skills_root,
        csk_home,
    )
    entry = _cache_entry(csk_home, marker)
    desired = tmp_path / "journal-source"
    shutil.copytree(marker_path.parent, desired)
    shutil.rmtree(marker_path.parent)
    live = tmp_path / "journal-live" / "build-skill"
    live.parent.mkdir()
    transaction_id = "txn-build-gc-root"
    engine = transactions.TransactionEngine(csk_home)
    plan = transactions.TransactionPlan(
        transaction_id=transaction_id,
        project_identity=str(tmp_path.resolve()),
        targets=(
            transactions.MutableTarget(
                target_class="10-context",
                identifier="project/build-skill",
                live_path=live,
                desired_path=desired,
                expected_preimage_digest=transactions.ABSENT_DIGEST,
                kind="entry",
            ),
        ),
    )
    os.utime(entry, (1, 1), follow_symlinks=False)

    with locking.ManagerHomeLock(csk_home) as home_lock:
        engine.prepare(home_lock, plan)
        stats = gc.collect_runtime(
            _without_projects(config),
            csk_home,
            guard=home_lock,
            now=gc.BUILD_GRACE_SECONDS + 100,
        )
        assert stats.builds_removed == 0
        assert entry.exists()
        engine.commit(home_lock, transaction_id)


@POSIX_BUILD_GC
def test_valid_journal_target_cannot_mask_other_vanished_context_targets(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    skills_root: Path,
    csk_home: Path,
) -> None:
    _project, config, _events, marker_path, marker = _installed_build(
        monkeypatch,
        tmp_path,
        skills_root,
        csk_home,
    )
    entry = _cache_entry(csk_home, marker)
    targets: list[transactions.MutableTarget] = []
    for scope in ("project", "global", "hybrid"):
        desired = tmp_path / f"journal-{scope}-source"
        shutil.copytree(marker_path.parent, desired)
        live = tmp_path / "journal-live" / scope / "build-skill"
        live.parent.mkdir(parents=True)
        targets.append(
            transactions.MutableTarget(
                target_class="10-context",
                identifier=f"{scope}/build-skill",
                live_path=live,
                desired_path=desired,
                expected_preimage_digest=transactions.ABSENT_DIGEST,
                kind="entry",
            )
        )
    shutil.rmtree(marker_path.parent)
    os.utime(entry, (1, 1), follow_symlinks=False)
    engine = transactions.TransactionEngine(csk_home)
    plan = transactions.TransactionPlan(
        transaction_id="txn-build-gc-mixed-contexts",
        project_identity=str(tmp_path.resolve()),
        targets=tuple(targets),
    )

    with locking.ManagerHomeLock(csk_home) as home_lock:
        engine.prepare(home_lock, plan)
        groups = {
            group.target_identifier: group
            for group in engine.referenced_install_marker_groups(home_lock)
        }
        assert set(groups) == {
            "project/build-skill",
            "global/build-skill",
            "hybrid/build-skill",
        }
        for identifier in ("global/build-skill", "hybrid/build-skill"):
            for path in groups[identifier].paths:
                if path.is_dir():
                    shutil.rmtree(path)
        stats = gc.collect_runtime(
            _without_projects(config),
            csk_home,
            guard=home_lock,
            now=gc.BUILD_GRACE_SECONDS + 100,
        )

    assert stats.builds_removed == 0
    for identifier in ("global/build-skill", "hybrid/build-skill"):
        assert any(
            identifier in warning
            and "no valid install marker generation remains" in warning
            for warning in stats.warnings
        )
    assert entry.exists()


@POSIX_BUILD_GC
def test_active_journal_with_no_remaining_marker_generation_retains_all(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    skills_root: Path,
    csk_home: Path,
) -> None:
    _project, config, _events, marker_path, marker = _installed_build(
        monkeypatch,
        tmp_path,
        skills_root,
        csk_home,
    )
    entry = _cache_entry(csk_home, marker)
    desired = tmp_path / "journal-source"
    shutil.copytree(marker_path.parent, desired)
    shutil.rmtree(marker_path.parent)
    live = tmp_path / "journal-live" / "build-skill"
    live.parent.mkdir()
    engine = transactions.TransactionEngine(csk_home)
    plan = transactions.TransactionPlan(
        transaction_id="txn-build-gc-vanished",
        project_identity=str(tmp_path.resolve()),
        targets=(
            transactions.MutableTarget(
                target_class="10-context",
                identifier="project/build-skill",
                live_path=live,
                desired_path=desired,
                expected_preimage_digest=transactions.ABSENT_DIGEST,
                kind="entry",
            ),
        ),
    )
    os.utime(entry, (1, 1), follow_symlinks=False)

    with locking.ManagerHomeLock(csk_home) as home_lock:
        engine.prepare(home_lock, plan)
        groups = engine.referenced_install_marker_groups(home_lock)
        assert len(groups) == 1
        for path in groups[0].paths:
            if path.is_dir():
                shutil.rmtree(path)
        stats = gc.collect_runtime(
            _without_projects(config),
            csk_home,
            guard=home_lock,
            now=gc.BUILD_GRACE_SECONDS + 100,
        )

    assert stats.builds_removed == 0
    assert any(
        "no valid install marker generation remains" in warning
        for warning in stats.warnings
    )
    assert entry.exists()


@POSIX_BUILD_GC
def test_journal_generation_change_during_mark_retains_all(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    skills_root: Path,
    csk_home: Path,
) -> None:
    _project, config, _events, marker_path, marker = _installed_build(
        monkeypatch,
        tmp_path,
        skills_root,
        csk_home,
    )
    entry = _cache_entry(csk_home, marker)
    desired = tmp_path / "journal-source"
    shutil.copytree(marker_path.parent, desired)
    shutil.rmtree(marker_path.parent)
    live = tmp_path / "journal-live" / "build-skill"
    live.parent.mkdir()
    engine = transactions.TransactionEngine(csk_home)
    plan = transactions.TransactionPlan(
        transaction_id="txn-build-gc-race",
        project_identity=str(tmp_path.resolve()),
        targets=(
            transactions.MutableTarget(
                target_class="10-context",
                identifier="project/build-skill",
                live_path=live,
                desired_path=desired,
                expected_preimage_digest=transactions.ABSENT_DIGEST,
                kind="entry",
            ),
        ),
    )
    os.utime(entry, (1, 1), follow_symlinks=False)

    with locking.ManagerHomeLock(csk_home) as home_lock:
        engine.prepare(home_lock, plan)
        group = engine.referenced_install_marker_groups(home_lock)[0]
        staged = next(path for path in group.paths if path.is_dir())
        original_collect = gc._collect_marker_directory
        raced = False

        def collect_then_remove(
            directory: Path,
            references: gc._References,
            *,
            expected_name: str | None = None,
        ) -> tuple[bool, str | None]:
            nonlocal raced
            result = original_collect(
                directory,
                references,
                expected_name=expected_name,
            )
            if directory == staged and not raced:
                raced = True
                shutil.rmtree(directory)
            return result

        monkeypatch.setattr(gc, "_collect_marker_directory", collect_then_remove)
        stats = gc.collect_runtime(
            _without_projects(config),
            csk_home,
            guard=home_lock,
            now=gc.BUILD_GRACE_SECONDS + 100,
        )

    assert raced
    assert stats.builds_removed == 0
    assert any(
        "generation paths changed while they were scanned" in warning
        for warning in stats.warnings
    )
    assert entry.exists()


@POSIX_BUILD_GC
def test_journal_filesystem_error_is_reported_as_retain_all(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    skills_root: Path,
    csk_home: Path,
) -> None:
    _project, config, _events, marker_path, marker = _installed_build(
        monkeypatch,
        tmp_path,
        skills_root,
        csk_home,
    )
    entry = _cache_entry(csk_home, marker)
    shutil.rmtree(marker_path.parent)
    os.utime(entry, (1, 1), follow_symlinks=False)
    journal_root = csk_home / "state" / "transactions" / "v1"
    journal_root.mkdir(parents=True, exist_ok=True)
    original_iterdir = Path.iterdir

    def fail_journal_listing(path: Path):
        if path == journal_root:
            raise OSError("forced journal listing failure")
        return original_iterdir(path)

    monkeypatch.setattr(Path, "iterdir", fail_journal_listing)

    stats = gc.collect_runtime(
        _without_projects(config),
        csk_home,
        now=gc.BUILD_GRACE_SECONDS + 100,
    )

    assert stats.builds_removed == 0
    assert any("journals are uncertain" in warning for warning in stats.warnings)
    assert any("cannot list transaction state" in warning for warning in stats.warnings)
    assert entry.exists()


@POSIX_BUILD_GC
def test_corrupt_journal_or_marker_retains_uncertain_build_state(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    skills_root: Path,
    csk_home: Path,
) -> None:
    _project, config, _events, marker_path, marker = _installed_build(
        monkeypatch,
        tmp_path,
        skills_root,
        csk_home,
    )
    entry = _cache_entry(csk_home, marker)
    os.utime(entry, (1, 1), follow_symlinks=False)
    marker_path.write_bytes(b"not-json")

    marker_stats = gc.collect_runtime(
        config,
        csk_home,
        now=gc.BUILD_GRACE_SECONDS + 100,
    )
    assert marker_stats.builds_removed == 0
    assert any("mark phase was incomplete" in item for item in marker_stats.warnings)
    assert entry.exists()

    shutil.rmtree(marker_path.parent)
    journal_root = csk_home / "state" / "transactions" / "v1"
    journal_root.mkdir(parents=True, exist_ok=True)
    (journal_root / "corrupt.json").write_text("{}", encoding="utf-8")
    journal_stats = gc.collect_runtime(
        _without_projects(config),
        csk_home,
        now=gc.BUILD_GRACE_SECONDS + 100,
    )
    assert journal_stats.builds_removed == 0
    assert any("journals are uncertain" in item for item in journal_stats.warnings)
    assert entry.exists()


@POSIX_BUILD_GC
def test_unreferenced_corrupt_protected_entry_is_retained_not_adopted(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    skills_root: Path,
    csk_home: Path,
) -> None:
    _project, config, _events, marker_path, marker = _installed_build(
        monkeypatch,
        tmp_path,
        skills_root,
        csk_home,
    )
    entry = _cache_entry(csk_home, marker)
    record = marker["builds"]["tool"]  # type: ignore[index]
    artifact = entry / record["artifact_path"]
    artifact.chmod(0o700)
    artifact.write_bytes(b"corrupt")
    artifact.chmod(0o500)
    shutil.rmtree(marker_path.parent)
    os.utime(entry, (1, 1), follow_symlinks=False)

    stats = gc.collect_runtime(
        _without_projects(config),
        csk_home,
        now=gc.BUILD_GRACE_SECONDS + 100,
    )

    assert stats.builds_removed == 0
    assert any("uncertain entry" in warning for warning in stats.warnings)
    assert entry.exists()


@POSIX_BUILD_GC
def test_corrupt_consumer_registry_fails_safe_before_any_sweep(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    skills_root: Path,
    csk_home: Path,
) -> None:
    _project, config, _events, marker_path, marker = _installed_build(
        monkeypatch,
        tmp_path,
        skills_root,
        csk_home,
    )
    entry = _cache_entry(csk_home, marker)
    shutil.rmtree(marker_path.parent)
    os.utime(entry, (1, 1), follow_symlinks=False)
    (csk_home / "consumers.json").write_text(
        json.dumps({"schema_version": 999, "consumers": []}),
        encoding="utf-8",
    )

    stats = gc.collect_runtime(
        _without_projects(config),
        csk_home,
        now=gc.BUILD_GRACE_SECONDS + 100,
    )

    assert stats.builds_removed == 0
    assert any("consumer registry is uncertain" in item for item in stats.warnings)
    assert entry.exists()


def test_marker_v1_keeps_runtime_and_snapshot_gc_compatibility(
    tmp_path: Path,
    skills_root: Path,
    csk_home: Path,
) -> None:
    project = make_project(tmp_path)
    _repo, commit = make_skill_repo(
        skills_root,
        "legacy-runtime",
        {
            "csk-skill.json": json.dumps(
                {
                    "schema_version": 1,
                    "commands": {
                        "legacy": {
                            "type": "script",
                            "unix_path": "scripts/legacy",
                            "win_path": "scripts/legacy.cmd",
                        }
                    },
                }
            ),
            "scripts/legacy": "#!/bin/sh\n",
            "scripts/legacy.cmd": "@echo off\r\n",
        },
        tag="v1",
    )
    write_skillfile(
        project,
        {
            "schema_version": 1,
            "skills": [{"name": "legacy-runtime", "tag": "v1"}],
        },
    )
    config = make_config(csk_home, skills_root, project)
    assert not installer.install(config)[0].errors
    marker_path = (
        project
        / ".agents"
        / "skills"
        / "legacy-runtime"
        / ".csk-install.json"
    )
    marker = json.loads(marker_path.read_text(encoding="utf-8"))
    marker["schema_version"] = 1
    marker.pop("build_roots")
    marker.pop("builds")
    marker.pop("build_source", None)
    marker_path.write_bytes(install_marker.serialize_install_marker(marker))
    runtime = csk_home / "runtime" / "legacy-runtime" / commit
    snapshot = csk_home / "cache" / "legacy-runtime" / commit / "snapshot"

    stats = gc.collect_runtime(config, csk_home)

    assert stats.runtime_removed == 0
    assert runtime.exists()
    assert snapshot.exists()
