from __future__ import annotations

import json
import os
import shutil
from pathlib import Path
from typing import Callable

import pytest
from conftest import make_config, make_project, make_skill_repo, write_skillfile
from test_installer_transactions import (
    POSIX_BUILD_VECTOR,
    _build_skill_files,
    _install_fake_build_pipeline,
    _native_target,
    _write_hybrid_manifest,
)

from csk import cli, config as config_mod, global_install, install_marker, installer, status
from csk.builds import cache as build_cache
from csk.builds import toolchain as build_toolchain


LEGACY_POLICY_KEY = (
    "sha256:3fcd714a40e8918eb67dbd35d435875dcce6c9047da811a1fa26626e5e57be48"
)
RESERVED_HARDENED_KEY = (
    "sha256:13736230d33ce59de7f7323dcd4cffd510655ad8dabd5ee9e8b6cb182ec70037"
)


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


def _installed_build(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    skills_root: Path,
    csk_home: Path,
    *,
    command: str = "tool",
    install_pipeline: (
        Callable[[pytest.MonkeyPatch, list[str]], None] | None
    ) = None,
) -> tuple[Path, object, list[str], Path, dict[str, object]]:
    project = make_project(tmp_path)
    make_skill_repo(
        skills_root,
        "build-skill",
        _build_skill_files(command),
        tag="v1",
    )
    write_skillfile(
        project,
        {
            "schema_version": 1,
            "skills": [{"name": "build-skill", "tag": "v1"}],
        },
    )
    config = make_config(csk_home, skills_root, project)
    events: list[str] = []
    if install_pipeline is None:
        _install_fake_build_pipeline(monkeypatch, events=events)
    else:
        install_pipeline(monkeypatch, events)
    _fixed_evidence(monkeypatch)
    result = installer.install(config)[0]
    assert not result.errors
    marker_path = (
        project
        / ".agents"
        / "skills"
        / "build-skill"
        / ".csk-install.json"
    )
    marker = json.loads(marker_path.read_text(encoding="utf-8"))
    return project, config, events, marker_path, marker


def _write_marker(path: Path, marker: dict[str, object]) -> None:
    path.write_bytes(install_marker.serialize_install_marker(marker))


def _build_row(config: object) -> tuple[status.ProjectStatus, object]:
    collected = status.collect_status(config)  # type: ignore[arg-type]
    assert len(collected) == 1
    assert len(collected[0].builds) == 1
    return collected[0], collected[0].builds[0]


def test_native_backend_reports_fresh_build_current(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    skills_root: Path,
    csk_home: Path,
) -> None:
    _project, config, _events, _marker_path, marker = _installed_build(
        monkeypatch,
        tmp_path,
        skills_root,
        csk_home,
    )

    project_status, build = _build_row(config)
    payload = status.statuses_to_payload([project_status])[0]

    assert project_status.clean
    assert build.current
    assert build.expected_cache_key == marker["builds"]["tool"]["cache_key"]  # type: ignore[index]
    assert payload["builds"][0]["current"] is True  # type: ignore[index]
    assert (
        build_cache.cache_for_manager_home(csk_home).__class__.__name__
        == ("WindowsBuildCache" if os.name == "nt" else "PosixBuildCache")
    )


@POSIX_BUILD_VECTOR
def test_project_build_status_revalidates_every_current_surface(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    skills_root: Path,
    csk_home: Path,
) -> None:
    project, config, _events, _marker_path, marker = _installed_build(
        monkeypatch,
        tmp_path,
        skills_root,
        csk_home,
    )

    project_status, build = _build_row(config)
    payload = status.statuses_to_payload([project_status])[0]

    assert project_status.clean
    assert build.current
    assert build.label == "current"
    assert build.expected_cache_key == marker["builds"]["tool"]["cache_key"]  # type: ignore[index]
    assert payload["clean"] is True
    assert payload["builds"][0]["execution_policy"] == "manager-worker-v1"  # type: ignore[index]
    assert payload["capability_evidence"] == {
        "record_version": "capability-evidence-v1",
        "execution_policy": "manager-worker-v1",
        "platform": "macos",
        "controls": [],
    }
    assert "CAPABILITY" in status.render_collected([project_status])
    assert not (
        project / ".agents" / "skills" / "build-skill" / "build"
    ).exists()


@pytest.mark.parametrize(
    "non_current_key",
    [LEGACY_POLICY_KEY, RESERVED_HARDENED_KEY],
)
@POSIX_BUILD_VECTOR
def test_legacy_and_reserved_execution_policy_keys_are_non_current(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    skills_root: Path,
    csk_home: Path,
    non_current_key: str,
) -> None:
    _project, config, _events, marker_path, marker = _installed_build(
        monkeypatch,
        tmp_path,
        skills_root,
        csk_home,
    )
    marker["builds"]["tool"]["cache_key"] = non_current_key  # type: ignore[index]
    _write_marker(marker_path, marker)

    project_status, build = _build_row(config)

    assert not project_status.clean
    assert build.label == "build-input-drift"
    assert "policy.execution_policy=manager-worker-v1" in build.detail
    assert non_current_key in status.render_collected([project_status])
    assert status.statuses_to_payload([project_status])[0]["clean"] is False


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("receipt_sha256", "sha256:" + "1" * 64),
        ("artifact_sha256", "sha256:" + "2" * 64),
        ("artifact_path", "bin/not-tool"),
    ],
)
@POSIX_BUILD_VECTOR
def test_marker_receipt_and_artifact_fields_are_currentness_inputs(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    skills_root: Path,
    csk_home: Path,
    field: str,
    value: str,
) -> None:
    _project, config, _events, marker_path, marker = _installed_build(
        monkeypatch,
        tmp_path,
        skills_root,
        csk_home,
    )
    marker["builds"]["tool"][field] = value  # type: ignore[index]
    _write_marker(marker_path, marker)

    project_status, build = _build_row(config)

    assert not project_status.clean
    assert build.label in {"build-marker-drift", "build-state-changed"}


@pytest.mark.parametrize(
    ("mutation", "expected_label"),
    [
        ("build-roots", "build-context-drift"),
        ("driver", "build-marker-drift"),
    ],
)
@POSIX_BUILD_VECTOR
def test_marker_build_boundary_and_driver_are_currentness_inputs(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    skills_root: Path,
    csk_home: Path,
    mutation: str,
    expected_label: str,
) -> None:
    _project, config, _events, marker_path, marker = _installed_build(
        monkeypatch,
        tmp_path,
        skills_root,
        csk_home,
    )
    if mutation == "build-roots":
        marker["build_roots"] = ["other-build-root"]
    else:
        marker["builds"]["tool"]["driver"] = "other-v1"  # type: ignore[index]
    _write_marker(marker_path, marker)

    project_status, build = _build_row(config)

    assert not project_status.clean
    assert build.label == expected_label


@POSIX_BUILD_VECTOR
def test_noncanonical_protected_receipt_is_non_current(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    skills_root: Path,
    csk_home: Path,
) -> None:
    _project, config, _events, _marker_path, marker = _installed_build(
        monkeypatch,
        tmp_path,
        skills_root,
        csk_home,
    )
    build = marker["builds"]["tool"]  # type: ignore[index]
    receipt = (
        csk_home
        / "builds"
        / "go-v1"
        / build["cache_key"].removeprefix("sha256:")
        / "csk-receipt.ccj.json"
    )
    receipt.chmod(0o600)
    receipt.write_bytes(b"{}")
    receipt.chmod(0o400)

    project_status, build_status = _build_row(config)

    assert not project_status.clean
    assert build_status.label == "corrupt-build-cache"


@POSIX_BUILD_VECTOR
def test_context_leak_and_raw_snapshot_drift_are_non_current(
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
    installed = marker_path.parent
    (installed / "build").mkdir()
    (installed / "build" / "leak.go").write_text(
        "package main\n",
        encoding="utf-8",
    )

    project_status, build = _build_row(config)
    assert not project_status.clean
    assert build.label == "build-context-exposed"

    shutil.rmtree(installed / "build")
    snapshot = (
        csk_home
        / "cache"
        / "build-skill"
        / str(marker["commit"])
        / "snapshot"
    )
    source = snapshot / "build" / "cmd" / "tool" / "main.go"
    source.write_text(source.read_text(encoding="utf-8") + "\n// drift\n", encoding="utf-8")

    project_status, build = _build_row(config)
    assert not project_status.clean
    assert build.label == "build-source-drift"
    assert "validated raw snapshot" in build.detail
    assert project_status.path == project


@POSIX_BUILD_VECTOR
def test_runtime_build_root_exposure_is_non_current(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    skills_root: Path,
    csk_home: Path,
) -> None:
    _project, config, _events, _marker_path, marker = _installed_build(
        monkeypatch,
        tmp_path,
        skills_root,
        csk_home,
    )
    runtime_build = (
        csk_home
        / "runtime"
        / "build-skill"
        / str(marker["commit"])
        / "build"
    )
    runtime_build.mkdir(parents=True)
    (runtime_build / "leak.go").write_text(
        "package main\n",
        encoding="utf-8",
    )

    project_status, build = _build_row(config)

    assert not project_status.clean
    assert build.label == "build-runtime-exposed"
    assert "runtime exposes a declared build root" in build.detail


def _different_toolchain(
    monkeypatch: pytest.MonkeyPatch,
    *,
    change_target: bool,
) -> None:
    native = _native_target()
    target = native
    if change_target:
        target = build_toolchain.NativeTarget(
            goos=native.goos,
            goarch="amd64" if native.goarch != "amd64" else "arm64",
            tuning={"GOAMD64": "v1"} if native.goarch != "amd64" else {"GOARM64": "v8.0"},
        )
    identity = build_toolchain.ToolchainIdentity(
        algorithm=build_toolchain.TOOLCHAIN_ALGORITHM,
        content_sha256="sha256:" + "b" * 64,
        go_relpath=build_toolchain.GO_RELPATH,
        go_version=f"go version go1.25.5 {target.goos}/{target.goarch}",
    )

    class Session:
        def __init__(self, config: build_toolchain.ToolchainConfig):
            self.target = target
            self.toolchain = identity
            self.operation_root = config.private_base / "operation"
            self.operation_root.mkdir(mode=0o700)
            self.executable = self.operation_root / "go"
            self.goroot = self.operation_root / "goroot"

        def __enter__(self) -> "Session":
            return self

        def __exit__(self, *_args: object) -> None:
            return None

    monkeypatch.setattr(build_toolchain, "establish_toolchain", Session)


@pytest.mark.parametrize("change_target", [False, True])
@POSIX_BUILD_VECTOR
def test_current_toolchain_and_native_target_are_replanned(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    skills_root: Path,
    csk_home: Path,
    change_target: bool,
) -> None:
    _project, config, _events, _marker_path, _marker = _installed_build(
        monkeypatch,
        tmp_path,
        skills_root,
        csk_home,
    )
    _different_toolchain(monkeypatch, change_target=change_target)

    project_status, build = _build_row(config)

    assert not project_status.clean
    assert build.label == "build-input-drift"


@POSIX_BUILD_VECTOR
def test_corrupt_artifact_is_rebuilt_by_normal_install_without_adoption(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    skills_root: Path,
    csk_home: Path,
) -> None:
    _project, config, events, _marker_path, marker = _installed_build(
        monkeypatch,
        tmp_path,
        skills_root,
        csk_home,
    )
    build = marker["builds"]["tool"]  # type: ignore[index]
    artifact = (
        csk_home
        / "builds"
        / "go-v1"
        / build["cache_key"].removeprefix("sha256:")
        / build["artifact_path"]
    )
    artifact.chmod(0o700)
    artifact.write_bytes(b"untrusted candidate bytes")
    artifact.chmod(0o500)

    project_status, build_status = _build_row(config)
    assert not project_status.clean
    assert build_status.label == "corrupt-build-cache"

    repaired = installer.install(config)[0]

    assert not repaired.errors
    assert events == ["build:tool", "build:tool"]
    assert artifact.read_bytes() != b"untrusted candidate bytes"
    assert _build_row(config)[0].clean


@POSIX_BUILD_VECTOR
def test_missing_untrusted_and_unsupported_cache_state_are_non_current(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    skills_root: Path,
    csk_home: Path,
) -> None:
    _project, config, _events, _marker_path, marker = _installed_build(
        monkeypatch,
        tmp_path,
        skills_root,
        csk_home,
    )
    build = marker["builds"]["tool"]  # type: ignore[index]
    key = build["cache_key"]
    entry = csk_home / "builds" / "go-v1" / key.removeprefix("sha256:")
    entry.chmod(0o700)
    project_status, build_status = _build_row(config)
    assert not project_status.clean
    assert build_status.label == "untrusted-build-cache"

    entry.chmod(0o500)
    backend = build_cache.cache_for_manager_home(csk_home)
    from csk import locking

    with locking.ManagerHomeLock(csk_home) as home_lock:
        assert backend.quarantine(key, guard=home_lock) is not None
    project_status, build_status = _build_row(config)
    assert not project_status.clean
    assert build_status.label == "missing-build-artifact"

    class Unsupported:
        manager_home = csk_home

        def inspect(self, _expectation: object) -> build_cache.CacheInspection:
            return build_cache.CacheInspection(
                status=build_cache.CacheEntryStatus.UNSUPPORTED,
                reason="fixture unsupported platform",
            )

    monkeypatch.setattr(
        status.build_cache,
        "cache_for_manager_home",
        lambda _home: Unsupported(),
    )
    project_status, build_status = _build_row(config)
    assert not project_status.clean
    assert build_status.label == "unsupported-build-platform"


@POSIX_BUILD_VECTOR
def test_managed_shim_and_capability_evidence_are_separate_dimensions(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    skills_root: Path,
    csk_home: Path,
) -> None:
    project, config, _events, _marker_path, _marker = _installed_build(
        monkeypatch,
        tmp_path,
        skills_root,
        csk_home,
    )
    shim = project / ".agents" / "bin" / "tool"
    shim.unlink()
    shim.symlink_to("/bin/false")

    project_status, build = _build_row(config)
    assert not project_status.clean
    assert build.label == "build-shim-drift"

    assert not installer.install(config)[0].errors
    monkeypatch.setattr(
        status,
        "_capability_evidence",
        lambda: (None, "fixture evidence unavailable"),
    )
    project_status, build = _build_row(config)
    assert project_status.clean
    assert build.current
    assert project_status.capability_evidence_error == "fixture evidence unavailable"


@pytest.mark.parametrize("skill_schema", [1, 2, 3, 4, 5])
def test_marker_v1_remains_current_for_skill_schemas_one_through_five(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    skills_root: Path,
    csk_home: Path,
    skill_schema: int,
) -> None:
    project = make_project(tmp_path)
    descriptor: dict[str, object] = {
        "schema_version": skill_schema,
        "commands": {},
    }
    if skill_schema >= 3:
        descriptor["capabilities"] = {"exec": "none", "network": "none"}
    make_skill_repo(
        skills_root,
        "legacy-skill",
        {"agent-skill.json": json.dumps(descriptor)},
        tag="v1",
    )
    write_skillfile(
        project,
        {
            "schema_version": 1,
            "skills": [{"name": "legacy-skill", "tag": "v1"}],
        },
    )
    config = make_config(csk_home, skills_root, project)
    assert not installer.install(config)[0].errors
    marker_path = (
        project
        / ".agents"
        / "skills"
        / "legacy-skill"
        / ".csk-install.json"
    )
    marker = json.loads(marker_path.read_text(encoding="utf-8"))
    marker["schema_version"] = 1
    marker.pop("build_roots")
    marker.pop("builds")
    marker.pop("build_source", None)
    _write_marker(marker_path, marker)

    collected = status.collect_status(config)

    assert collected[0].clean
    assert collected[0].skills[0].label == "up-to-date"

    cached_snapshot = (
        csk_home
        / "cache"
        / "legacy-skill"
        / str(marker["commit"])
        / "snapshot"
    )
    shutil.rmtree(cached_snapshot)
    without_persistent_snapshot = status.collect_status(config)
    assert without_persistent_snapshot[0].clean
    assert not cached_snapshot.exists()


@POSIX_BUILD_VECTOR
def test_missing_raw_snapshot_is_non_current_and_status_does_not_recreate_it(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    skills_root: Path,
    csk_home: Path,
) -> None:
    _project, config, _events, _marker_path, marker = _installed_build(
        monkeypatch,
        tmp_path,
        skills_root,
        csk_home,
    )
    snapshot = (
        csk_home
        / "cache"
        / "build-skill"
        / str(marker["commit"])
        / "snapshot"
    )
    shutil.rmtree(snapshot)

    project_status, build = _build_row(config)

    assert not project_status.clean
    assert build.label == "build-source-unavailable"
    assert not snapshot.exists()


@POSIX_BUILD_VECTOR
def test_global_and_hybrid_build_status_use_their_managed_stores(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    skills_root: Path,
    csk_home: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    project = make_project(tmp_path)
    make_skill_repo(
        skills_root,
        "global-build",
        _build_skill_files("global-tool"),
        tag="v1",
    )
    make_skill_repo(
        skills_root,
        "hybrid-build",
        _build_skill_files("hybrid-tool"),
        tag="v1",
    )
    write_skillfile(project, {"schema_version": 1, "skills": []})
    config = make_config(csk_home, skills_root, project)
    events: list[str] = []
    _install_fake_build_pipeline(monkeypatch, events=events)
    _fixed_evidence(monkeypatch)
    global_root = csk_home / "global"
    global_root.mkdir()
    (global_root / "Skillfile.json").write_text(
        json.dumps(
            {
                "schema_version": 1,
                "skills": [{"name": "global-build", "tag": "v1"}],
            }
        ),
        encoding="utf-8",
    )
    _write_hybrid_manifest(
        csk_home,
        [
            {
                "name": "hybrid-build",
                "tag": "v1",
                "targets": ["app"],
            }
        ],
    )

    assert not global_install.install(config).errors
    assert not installer.install(config)[0].errors

    global_status = status.collect_global_status(config)
    project_status = status.collect_status(config)[0]
    assert global_status.clean
    assert global_status.builds[0].provider == "global-build"
    assert global_status.builds[0].current
    assert project_status.clean
    assert project_status.builds[0].provider == "hybrid-build"
    assert project_status.builds[0].current
    assert (
        csk_home / "hybrid" / "skills" / "hybrid-build" / ".csk-install.json"
    ).exists()
    config_mod.save_config(config)
    monkeypatch.setenv("CSK_CONFIG", str(config.path))
    assert cli.main(["global", "status", "--json", "--check"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["clean"] is True
    assert payload["builds"][0]["label"] == "current"

    global_marker_path = (
        csk_home
        / "global"
        / "skills"
        / "global-build"
        / ".csk-install.json"
    )
    global_marker = json.loads(global_marker_path.read_text(encoding="utf-8"))
    global_marker["builds"]["global-tool"]["cache_key"] = LEGACY_POLICY_KEY
    _write_marker(global_marker_path, global_marker)
    assert cli.main(["global", "status", "--json", "--check"]) == 1
    payload = json.loads(capsys.readouterr().out)
    assert payload["clean"] is False
    assert payload["builds"][0]["label"] == "build-input-drift"


@POSIX_BUILD_VECTOR
def test_cli_check_and_json_fail_for_non_current_build(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    skills_root: Path,
    csk_home: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _project, config, _events, marker_path, marker = _installed_build(
        monkeypatch,
        tmp_path,
        skills_root,
        csk_home,
    )
    config_mod.save_config(config)
    monkeypatch.setenv("CSK_CONFIG", str(config.path))
    marker["builds"]["tool"]["cache_key"] = LEGACY_POLICY_KEY  # type: ignore[index]
    _write_marker(marker_path, marker)

    assert cli.main(["status", "app", "--check"]) == 1
    assert "build-input-drift" in capsys.readouterr().out
    assert cli.main(["status", "app", "--json", "--check"]) == 1
    payload = json.loads(capsys.readouterr().out)
    assert payload[0]["clean"] is False
    assert payload[0]["builds"][0]["label"] == "build-input-drift"
