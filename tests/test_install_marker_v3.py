from __future__ import annotations

import hashlib
import json
import os

import pytest

from csk import gc as gc_module, install_marker, protocol_json, shims
from csk import build_repository_pipeline
from csk.build_repository_pipeline import receipt_input
from csk.build_repository_pipeline import CompilerIdentity, DeclaredState, EffectiveState, PipelineRequest, Operation
from csk.build_repository import BuildTarget
from csk.skillspec import CommandSpec


SHA1 = "0123456789abcdef0123456789abcdef01234567"


def _external_build() -> install_marker.InstallMarkerBuildV3:
    return install_marker.InstallMarkerBuildV3(
        driver="go-repository-v1",
        receipt_schema_version=2,
        execution_policy="manager-worker-v1",
        repository="golden-tools",
        declared_identity=install_marker.MarkerRepositoryIdentity("network-git", "github.com/example/golden-tools"),
        declared_locked_commit=install_marker.MarkerRepositoryCommit("sha1", SHA1),
        declared_tag="v1.4.0",
        effective_identity=install_marker.MarkerRepositoryIdentity("network-git", "github.com/example/golden-tools"),
        object_format="sha1",
        commit=SHA1,
        substituted=False,
        build_source=install_marker.BuildSourceIdentity(
            algorithm="curator-build-source-v1",
            content_sha256="sha256:" + "b" * 64,
        ),
        descriptor_target="golden-tool",
        cache_key="sha256:4abc903bde7d8d9f65d32fd276f37dadccc88eb28bbaf693106dcebc4a19107a",
        receipt_sha256="sha256:0f8f910a2b6ba9b35531bb232cb2890e11eb55a64ba01bcdd2d93d5ea421d0e0",
        artifact_sha256="sha256:" + "6" * 64,
        artifact_path="bin/golden-tool",
    )


def _base(**changes: object) -> dict[str, object]:
    payload: dict[str, object] = {
        "name": "golden-skill",
        "source": "golden-skill",
        "ref_kind": "revision",
        "ref": SHA1,
        "commit": SHA1,
        "content_sha256": "sha256:e2bd2941476b164099dc773d7448c638ff99849f8dce76e7b105f215fcdf31be",
        "locale": None,
        "agents": ("codex_cli",),
        "commands": ("golden-tool", "local-helper"),
        "dependencies": (),
        "skill_schema_version": 7,
        "runtime_roots": (),
        "installed_at": "2000-01-01T00:00:00Z",
        "files": (".skill_triggers/en.md", "SKILL.md", "references/notes.md"),
        "activation": install_marker.MarkerActivation(True, ("golden-tool",)),
        "requirers": ("<project>",),
        "build_roots": ("build",),
        "build_source": install_marker.BuildSourceIdentity(
            algorithm="curator-build-source-v1",
            content_sha256="sha256:" + "b" * 64,
        ),
        "builds": {
            "golden-tool": _external_build(),
            "local-helper": install_marker.InstallMarkerBuildV3(
                driver="go-v1",
                receipt_schema_version=1,
                execution_policy="manager-worker-v1",
                cache_key="sha256:" + "1" * 64,
                receipt_sha256="sha256:" + "e" * 64,
                artifact_sha256="sha256:" + "d" * 64,
                artifact_path="bin/local-helper",
            ),
        },
    }
    payload.update(changes)
    return payload


def test_marker_v3_mixed_vector_round_trips_exact_fields_and_bytes() -> None:
    marker = install_marker.InstallMarkerV3(**_base())
    raw = install_marker.serialize_install_marker(marker.to_json())
    decoded = json.loads(raw)
    assert decoded["schema_version"] == 3
    assert decoded["builds"]["local-helper"] == {
        "driver": "go-v1",
        "receipt_schema_version": 1,
        "execution_policy": "manager-worker-v1",
        "cache_key": "sha256:" + "1" * 64,
        "receipt_sha256": "sha256:" + "e" * 64,
        "artifact_sha256": "sha256:" + "d" * 64,
        "artifact_path": "bin/local-helper",
    }
    assert decoded["builds"]["golden-tool"] == _external_build().to_json()
    assert install_marker.read_install_marker(raw).to_json() == marker.to_json()
    assert raw.endswith(b"\n") and b"\r" not in raw


@pytest.mark.parametrize(
    "mutate",
    [
        lambda build: {**build, "driver": "go-v1"},
        lambda build: {**build, "receipt_schema_version": 1},
        lambda build: {key: value for key, value in build.items() if key != "execution_policy"},
    ],
)
def test_marker_v3_external_receipt_state_never_aliases_local(mutate) -> None:
    payload = install_marker.InstallMarkerV3(**_base()).to_json()
    payload["builds"]["golden-tool"] = mutate(payload["builds"]["golden-tool"])
    with pytest.raises(install_marker.InstallMarkerError):
        install_marker.parse_install_marker(payload)


def test_external_receipt_v2_rc5_vector_cache_key_and_hash() -> None:
    request = PipelineRequest(
        operation=Operation.DRY_RUN,
        command="golden-tool",
        target="golden-tool",
        declared=DeclaredState("golden-tools", "github.com/example/golden-tools", "https", "sha1", SHA1, "v1.4.0"),
        effective=EffectiveState("network-git", "github.com/example/golden-tools", "https", "sha1", SHA1),
        acquire=lambda: (_ for _ in ()).throw(AssertionError()),
        audit=lambda subject: None,
    )
    compiler = CompilerIdentity(
        content_sha256="sha256:" + "c" * 64,
        go_version="go version go1.26.1 darwin/arm64",
        go_relpath="bin/go",
        goos="darwin",
        goarch="arm64",
        tuning={"GOARM64": "v8.0"},
    )
    value = receipt_input(
        request,
        request.effective,
        BuildTarget("golden-tool", "go-repository-v1", ".", "cmd/golden-tool"),
        "sha256:" + "b" * 64,
        compiler,
    )
    key = "sha256:" + hashlib.sha256(protocol_json.canonical_bytes(value)).hexdigest()
    assert key == "sha256:4abc903bde7d8d9f65d32fd276f37dadccc88eb28bbaf693106dcebc4a19107a"
    receipt = protocol_json.canonical_bytes(
        {
            "schema_version": 2,
            "cache_key": key,
            "input": value,
            "artifact": {"path": "bin/golden-tool", "sha256": "sha256:" + "6" * 64, "size": 1234567},
        }
    )
    assert "sha256:" + hashlib.sha256(receipt).hexdigest() == "sha256:0f8f910a2b6ba9b35531bb232cb2890e11eb55a64ba01bcdd2d93d5ea421d0e0"


def test_external_activation_checks_hash_link_count_and_manager_path(tmp_path) -> None:
    home = tmp_path / "home"
    artifact = home / "external-builds" / "artifacts" / ("4" * 64) / "artifact"
    artifact.parent.mkdir(parents=True)
    artifact.write_bytes(b"artifact")
    artifact.chmod(0o500)
    digest = "sha256:" + hashlib.sha256(b"artifact").hexdigest()
    receipt_value = {
        "schema_version": 2,
        "cache_key": "sha256:" + "4" * 64,
        "input": {
            "schema_version": 2,
            "driver": "go-repository-v1",
            "command": "tool",
            "target": {"goos": "darwin"},
        },
        "artifact": {"path": "bin/tool", "sha256": digest, "size": 8},
    }
    receipt = protocol_json.canonical_bytes(receipt_value)
    marker = install_marker.InstallMarkerBuildV3(
        driver="go-repository-v1",
        receipt_schema_version=2,
        execution_policy="manager-worker-v1",
        repository="tools",
        declared_identity=install_marker.MarkerRepositoryIdentity("network-git", "example.com/tools"),
        declared_locked_commit=install_marker.MarkerRepositoryCommit("sha1", "a" * 40),
        effective_identity=install_marker.MarkerRepositoryIdentity("network-git", "example.com/tools"),
        object_format="sha1",
        commit="a" * 40,
        substituted=False,
        build_source=install_marker.BuildSourceIdentity("curator-build-source-v1", "sha256:" + "b" * 64),
        descriptor_target="tool",
        cache_key="sha256:" + "4" * 64,
        receipt_sha256="sha256:" + hashlib.sha256(receipt).hexdigest(),
        artifact_sha256=digest,
        artifact_path="bin/tool",
    )
    activation = shims.select_external_build_activation(
        csk_home=home,
        command=CommandSpec("tool", "build", driver="go-repository-v1", repository="tools", target="tool"),
        marker_build=marker,
        receipt_bytes=receipt,
        artifact_path=artifact,
    )
    assert activation.artifact_path == artifact
    alias = artifact.with_name("alias")
    os.link(artifact, alias)
    with pytest.raises(shims.ShimError, match="singly linked"):
        shims.select_external_build_activation(
            csk_home=home,
            command=CommandSpec("tool", "build", driver="go-repository-v1", repository="tools", target="tool"),
            marker_build=marker,
            receipt_bytes=receipt,
            artifact_path=artifact,
        )


def test_windows_external_activation_resolves_the_suffixed_cache_artifact(tmp_path) -> None:
    home = tmp_path / "home"
    entry = home / "external-builds" / "artifacts" / ("5" * 64)
    entry.mkdir(parents=True)
    artifact = entry / "artifact.exe"
    artifact.write_bytes(b"MZ-artifact")
    digest = "sha256:" + hashlib.sha256(b"MZ-artifact").hexdigest()
    receipt = protocol_json.canonical_bytes(
        {
            "schema_version": 2,
            "cache_key": "sha256:" + "5" * 64,
            "input": {
                "schema_version": 2,
                "driver": "go-repository-v1",
                "command": "tool",
                "target": {"goos": "windows"},
            },
            "artifact": {"path": "bin/tool.exe", "sha256": digest, "size": 11},
        }
    )
    marker = install_marker.InstallMarkerBuildV3(
        driver="go-repository-v1",
        receipt_schema_version=2,
        execution_policy="manager-worker-v1",
        repository="tools",
        declared_identity=install_marker.MarkerRepositoryIdentity("network-git", "example.com/tools"),
        declared_locked_commit=install_marker.MarkerRepositoryCommit("sha1", "a" * 40),
        effective_identity=install_marker.MarkerRepositoryIdentity("network-git", "example.com/tools"),
        object_format="sha1",
        commit="a" * 40,
        substituted=False,
        build_source=install_marker.BuildSourceIdentity("curator-build-source-v1", "sha256:" + "b" * 64),
        descriptor_target="tool",
        cache_key="sha256:" + "5" * 64,
        receipt_sha256="sha256:" + hashlib.sha256(receipt).hexdigest(),
        artifact_sha256=digest,
        artifact_path="bin/tool.exe",
    )
    command = CommandSpec("tool", "build", driver="go-repository-v1", repository="tools", target="tool")

    activation = shims.select_external_build_activation(
        csk_home=home,
        command=command,
        marker_build=marker,
        receipt_bytes=receipt,
        artifact_path=artifact,
        platform_name=shims.WINDOWS_PLATFORM,
    )
    assert activation.artifact_path == artifact

    # The suffix-less name is what the launcher used to be pointed at, and
    # Windows cannot execute it.
    unsuffixed = entry / "artifact"
    unsuffixed.write_bytes(b"MZ-artifact")
    with pytest.raises(shims.ShimError, match="manager-derived external cache path"):
        shims.select_external_build_activation(
            csk_home=home,
            command=command,
            marker_build=marker,
            receipt_bytes=receipt,
            artifact_path=unsuffixed,
            platform_name=shims.WINDOWS_PLATFORM,
        )


def test_external_gc_uses_only_marker_derived_snapshot_and_artifact_roots(tmp_path) -> None:
    home = tmp_path / "home"
    root = home / "external-builds"
    live_build = "sha256:" + "1" * 64
    dead_build = "sha256:" + "2" * 64
    live_snapshot = "sha256:" + "3" * 64
    dead_snapshot = "sha256:" + "4" * 64
    for kind, keys in (
        ("artifacts", (live_build, dead_build)),
        ("snapshots", (live_snapshot, dead_snapshot)),
    ):
        for key in keys:
            entry = root / kind / key.removeprefix("sha256:")
            entry.mkdir(parents=True)
            entry.chmod(0o500)
    root.chmod(0o700)
    (root / "artifacts").chmod(0o700)
    (root / "snapshots").chmod(0o700)
    if os.name == "nt":
        build_repository_pipeline._secure_windows_path(
            root, directory=True, sealed=False
        )
        for kind in ("artifacts", "snapshots"):
            parent = root / kind
            build_repository_pipeline._secure_windows_path(
                parent, directory=True, sealed=False
            )
            for entry in parent.iterdir():
                build_repository_pipeline._secure_windows_path(
                    entry, directory=True, sealed=True
                )
    references = gc_module._References(
        external_builds={live_build}, external_snapshots={live_snapshot}
    )
    assert gc_module._collect_external_cache(home, references) == (1, 1)
    assert (root / "artifacts" / ("1" * 64)).is_dir()
    assert (root / "snapshots" / ("3" * 64)).is_dir()
