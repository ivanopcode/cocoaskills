"""Source-aware build receipts (schema 3) and the receipt-3 cache namespace.

Covers TASK-260916-341a6q: the receipt-3 wrapper over the unchanged driver
inputs, the package-bound cache key, the distinct receipt-3 cache namespaces
(local and external), marker-5 build record retention, the seventeen-field
external-evidence comparison, top-level build-source presence, and
audit-before-cache/compiler ordering. The seventeen conformance cases and the
build-receipt-v3 schema cases are additionally driven through these same
production entry points by tests/test_draft_sources_conformance.py.
"""

from __future__ import annotations

import hashlib
import os
import stat
import sys
import time
from collections.abc import Iterator, Mapping
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest

from csk import gc, install_marker, protocol_json
from csk.config import GlobalConfig
from csk.build_repository import BuildTarget
from csk.build_repository_pipeline import (
    ARTIFACTS_NAMESPACE,
    ARTIFACTS_V3_NAMESPACE,
    AUDIT_BLOCKED,
    EXTERNAL_ARTIFACT_NAMESPACES,
    EXTERNAL_BUILD_IDENTITY_INVALID,
    EXTERNAL_SNAPSHOT_NAMESPACES,
    SNAPSHOTS_NAMESPACE,
    CompilerIdentity,
    DeclaredState,
    DiskProtectedStore,
    EffectiveState,
    ExternalBuildError,
    Operation,
    PipelineRequest,
    SubstitutionState,
    receipt_input,
    run_pipeline,
    snapshot_key,
)
from csk.builds import (
    cache as build_cache,
)
from csk.builds import (
    currentness as build_currentness,
)
from csk.builds import (
    metadata as build_metadata,
)
from csk.builds import (
    planner as build_planner,
)
from csk.builds import (
    source as build_source,
)
from csk.builds import (
    toolchain as build_toolchain,
)
from csk.builds import cache_posix, cache_windows
from csk.builds.cache import (
    LOCAL_BUILD_CACHE_NAMESPACES,
    CacheEntryStatus,
    CacheExpectation,
    CachePublication,
    cache_for_manager_home,
    make_publication_source_private,
    provision_manager_home,
)
from csk.builds.currentness import (
    BuildCurrentnessError,
    EXTERNAL_EVIDENCE_FIELDS,
    compare_external_build_evidence,
)
from csk.builds.metadata import (
    BuildArtifact,
    BuildMetadataError,
    BuildReceiptV3,
    GoBuildInput,
    GoRepositoryBuildInput,
    SourceAwareBuildInput,
)
from csk.builds.source import BuildSourceIdentity
from csk.builds.toolchain import (
    GO_RELPATH,
    TOOLCHAIN_ALGORITHM,
    NativeTarget,
    ToolchainIdentity,
)
from csk.dev_substitutions import (
    DevSubstitutionError,
    check_source_substitution_admission,
)
from csk.git_admission import Snapshot, SnapshotFile
from csk.sources import package_identity as source_package
from csk.sources.local_snapshot import inventory_digest

COMMIT = "0123456789abcdef0123456789abcdef01234567"
COMMIT_OTHER = "abcdef0123456789abcdef0123456789abcdef01"
COMMIT_2 = "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb"
REVISION_REF = "1111111111111111111111111111111111111111"
REVISION_REF_OTHER = "2222222222222222222222222222222222222222"
PACKAGE_DIGEST = "sha256:" + "1" * 64
PACKAGE_DIGEST_OTHER = "sha256:" + "2" * 64
PACKAGE_DIGEST_THIRD = "sha256:" + "3" * 64

# The literal seventeen-field family. The comparison test below is
# parametrised over these literals, and a separate completeness test pins the
# literals against the production table, so a mutant that shrinks the table
# cannot silently vanish the coverage.
LITERAL_EVIDENCE_FIELDS = (
    "repository",
    "declared_identity",
    "declared_locked_commit",
    "declared_tag",
    "effective_identity",
    "object_format",
    "commit",
    "substituted",
    "substitution",
    "build_source",
    "descriptor_target",
    "execution_policy",
    "cache_key",
    "receipt_sha256",
    "artifact_sha256",
    "artifact_path",
    "input.package",
)

# Fields whose single mutation keeps the marker record valid and differs from
# the evidence in exactly that field. ``substituted`` and ``object_format``
# need a coupled valid mutation; ``input.package`` re-derives the cache key by
# design; ``execution_policy`` admits no second valid value and refuses at
# construction instead.
SINGLE_MISMATCH_FIELDS = tuple(
    field
    for field in LITERAL_EVIDENCE_FIELDS
    if field
    not in {"substituted", "object_format", "execution_policy", "input.package"}
)


@pytest.fixture(autouse=True)
def _restore_test_permissions(tmp_path: Path) -> Iterator[None]:
    """Let pytest remove intentionally immutable test trees."""

    yield
    for root, directories, files in os.walk(tmp_path, topdown=True, followlinks=False):
        root_path = Path(root)
        try:
            root_path.chmod(0o700)
        except OSError:
            pass
        for name in directories:
            path = root_path / name
            try:
                if not path.is_symlink():
                    path.chmod(0o700)
            except OSError:
                pass
        for name in files:
            try:
                (root_path / name).chmod(0o700)
            except OSError:
                pass


class _HeldGuard:
    def assert_held(self) -> None:
        pass


def _local_build_input(command: str = "golden-tool") -> GoBuildInput:
    return GoBuildInput(
        build_source=BuildSourceIdentity(
            algorithm="curator-build-source-v1",
            content_sha256="sha256:" + "b" * 64,
        ),
        build_root="build",
        command=command,
        source_dir=f"build/cmd/{command}",
        target=NativeTarget(goos="darwin", goarch="arm64", tuning={"GOARM64": "v8.0"}),
        toolchain=ToolchainIdentity(
            algorithm=TOOLCHAIN_ALGORITHM,
            content_sha256="sha256:" + "c" * 64,
            go_relpath=GO_RELPATH,
            go_version="go version go1.26.1 darwin/arm64",
        ),
    )


def _package(digest: str = PACKAGE_DIGEST) -> source_package.LocalSnapshot:
    return source_package.LocalSnapshot(snapshot=digest)


def _snapshot(*, tag_verified: bool = True) -> Snapshot:
    files = tuple(
        sorted(
            (
                SnapshotFile("repo/go.mod", b"module example.test/tool\n\ngo 1.25\n"),
                SnapshotFile(
                    "repo/cmd/tool/main.go", b"package main\nfunc main() {}\n"
                ),
                SnapshotFile(
                    "skill-build.json",
                    protocol_json.canonical_bytes(
                        {
                            "schema_version": 1,
                            "targets": {
                                "tool": {
                                    "driver": "go-repository-v1",
                                    "build_root": "repo",
                                    "source_dir": "repo/cmd/tool",
                                }
                            },
                        }
                    ),
                ),
            ),
            key=lambda item: item.path,
        )
    )
    framed = bytearray(b"curator-build-source-v1\0")
    for item in files:
        path = item.path.encode()
        framed.extend(b"F")
        framed.extend(len(path).to_bytes(8, "big"))
        framed.extend(path)
        framed.extend(len(item.content).to_bytes(8, "big"))
        framed.extend(item.content)
    canonical = bytes(framed)
    return Snapshot(
        object_format="sha1",
        commit=COMMIT,
        files=files,
        canonical_bytes=canonical,
        digest="sha256:" + hashlib.sha256(canonical).hexdigest(),
        tag_verified=tag_verified,
    )


def _declared(*, tag: str | None = "v1.0.0") -> DeclaredState:
    return DeclaredState(
        repository="tools",
        identity="github.com/example/tools",
        transport="https",
        object_format="sha1",
        commit=COMMIT,
        tag=tag,
    )


def _effective(*, substituted: bool = True) -> EffectiveState:
    if not substituted:
        return EffectiveState(
            identity_kind="network-git",
            identity="github.com/example/tools",
            transport="https",
            object_format="sha1",
            commit=COMMIT,
        )
    return EffectiveState(
        identity_kind="network-git",
        identity="github.com/example/tools",
        transport="https",
        object_format="sha1",
        commit=COMMIT,
        substituted=True,
        substitution=SubstitutionState(
            type="network-git", ref_kind="revision", ref_value=REVISION_REF
        ),
    )


def _compiler_identity() -> CompilerIdentity:
    return CompilerIdentity(
        content_sha256="sha256:" + "c" * 64,
        go_version="go version go1.26.1 darwin/arm64",
        go_relpath="bin/go",
        goos="darwin",
        goarch="arm64",
        tuning={"GOARM64": "v8.0"},
    )


class _Compiler:
    """A deterministic fake Go compiler with an invocation counter."""

    def __init__(self, events: list[str]) -> None:
        self.events = events
        self.calls = 0
        self.identity = _compiler_identity()

    def compile(self, root: Path, source_dir: str, command: str) -> bytes:
        self.calls += 1
        self.events.append("compiler")
        assert command == "tool"
        return b"compiled-tool"


def _install_v3(
    tmp_path: Path,
    *,
    operation: Operation = Operation.INSTALL,
    substituted: bool = True,
    package: source_package.LocalSnapshot | None = None,
    events: list[str] | None = None,
    store_hook: Any = None,
) -> tuple[DiskProtectedStore, _Compiler, Any]:
    """Run the external pipeline with a receipt-3 package through production."""

    events = events if events is not None else []
    store = DiskProtectedStore(tmp_path / "store")
    if store_hook is not None:
        store_hook(store)
    compiler = _Compiler(events)
    snapshot = _snapshot()

    def acquire() -> Snapshot:
        events.append("acquire")
        return snapshot

    def audit(subject: object) -> None:
        events.append("audit")

    request = PipelineRequest(
        operation=operation,
        command="tool",
        target="tool",
        declared=_declared(),
        effective=_effective(substituted=substituted),
        acquire=acquire,
        audit=audit,
        store=store,
        compiler=compiler,
        package=_package() if package is None else package,
    )
    return store, compiler, run_pipeline(request)


def _wrapped_input(result: Any) -> SourceAwareBuildInput:
    assert result.receipt is not None
    return build_metadata.read_receipt_v3(result.receipt).input


def _mirror_external_record(
    record_cls: Any,
    receipt_version: int,
    build: GoRepositoryBuildInput,
    cache_key: str,
    receipt_bytes: bytes,
    artifact_bytes: bytes,
) -> Any:
    """Mirror parsed external evidence into the marker record a plan writes."""

    declared = build.source.declared
    effective = build.source.effective
    substitution = effective.substitution
    return record_cls(
        driver="go-repository-v1",
        receipt_schema_version=receipt_version,
        execution_policy="manager-worker-v1",
        cache_key=cache_key,
        receipt_sha256=build_metadata.receipt_sha256(receipt_bytes),
        artifact_sha256="sha256:" + hashlib.sha256(artifact_bytes).hexdigest(),
        artifact_path=build.artifact_path,
        repository=build.source.repository,
        declared_identity=install_marker.MarkerRepositoryIdentity(
            kind=declared.identity.kind, value=declared.identity.value
        ),
        declared_locked_commit=install_marker.MarkerRepositoryCommit(
            object_format=declared.locked_commit.object_format,
            hex=declared.locked_commit.hex,
        ),
        declared_tag=declared.tag,
        effective_identity=install_marker.MarkerRepositoryIdentity(
            kind=effective.identity.kind, value=effective.identity.value
        ),
        object_format=effective.object_format,
        commit=effective.commit,
        substituted=effective.substituted,
        substitution=(
            None
            if substitution is None
            else install_marker.MarkerRepositorySubstitution(
                type=substitution.type,
                ref=(
                    None
                    if substitution.ref is None
                    else install_marker.MarkerRepositoryRef(
                        kind=substitution.ref.kind, value=substitution.ref.value
                    )
                ),
            )
        ),
        build_source=effective.build_source,
        descriptor_target=build.source.descriptor.target,
    )


def _genuine_record(
    result: Any, package: source_package.LocalSnapshot
) -> install_marker.InstallMarkerBuildV5:
    """Mirror receipt-3 evidence into the marker record planning would write."""

    assert result.receipt is not None and result.artifact is not None
    receipt = build_metadata.read_receipt_v3(result.receipt)
    build = receipt.input.build
    assert isinstance(build, GoRepositoryBuildInput)
    assert receipt.input.package == package
    return _mirror_external_record(
        install_marker.InstallMarkerBuildV5,
        3,
        build,
        result.cache_key,
        result.receipt,
        result.artifact,
    )


def _genuine_legacy_external_record(result: Any) -> install_marker.InstallMarkerBuildV3:
    """Mirror receipt-2 evidence into the legacy marker record a plan writes."""

    assert result.receipt is not None and result.artifact is not None
    raw = protocol_json.loads_canonical(result.receipt)
    assert isinstance(raw, dict) and raw.get("schema_version") == 2
    build = build_metadata.parse_repository_build_input(raw["input"])
    return _mirror_external_record(
        install_marker.InstallMarkerBuildV3,
        2,
        build,
        result.cache_key,
        result.receipt,
        result.artifact,
    )


def _mutate_record(
    record: install_marker.InstallMarkerBuildV5, field: str
) -> install_marker.InstallMarkerBuildV5:
    """Apply the valid single-field mutation for one evidence field."""

    if field == "repository":
        return replace(record, repository="other-tools")
    if field == "declared_identity":
        return replace(
            record,
            declared_identity=install_marker.MarkerRepositoryIdentity(
                kind="network-git", value="github.com/example/other"
            ),
        )
    if field == "declared_locked_commit":
        return replace(
            record,
            declared_locked_commit=install_marker.MarkerRepositoryCommit(
                object_format="sha1", hex=COMMIT_OTHER
            ),
        )
    if field == "declared_tag":
        return replace(record, declared_tag="v2.0.0")
    if field == "effective_identity":
        return replace(
            record,
            effective_identity=install_marker.MarkerRepositoryIdentity(
                kind="network-git", value="github.com/example/mirror"
            ),
        )
    if field == "commit":
        return replace(record, commit=COMMIT_OTHER)
    if field == "substitution":
        return replace(
            record,
            substitution=install_marker.MarkerRepositorySubstitution(
                type="network-git",
                ref=install_marker.MarkerRepositoryRef(
                    kind="revision", value=REVISION_REF_OTHER
                ),
            ),
        )
    if field == "build_source":
        return replace(
            record,
            build_source=BuildSourceIdentity(
                algorithm="curator-build-source-v1",
                content_sha256="sha256:" + "9" * 64,
            ),
        )
    if field == "descriptor_target":
        return replace(record, descriptor_target="other-target")
    if field == "cache_key":
        return replace(record, cache_key="sha256:" + "f" * 64)
    if field == "receipt_sha256":
        return replace(record, receipt_sha256="sha256:" + "e" * 64)
    if field == "artifact_sha256":
        return replace(record, artifact_sha256="sha256:" + "d" * 64)
    if field == "artifact_path":
        return replace(record, artifact_path="bin/other-tool")
    raise AssertionError(f"no single-field mutation for {field!r}")


def _compare(
    record: install_marker.InstallMarkerBuildV5,
    result: Any,
    package: source_package.LocalSnapshot,
    wrapped: SourceAwareBuildInput | None = None,
) -> tuple[str, ...]:
    assert result.receipt is not None and result.artifact is not None
    return compare_external_build_evidence(
        record,
        _wrapped_input(result) if wrapped is None else wrapped,
        marker_package=package,
        receipt_bytes=result.receipt,
        artifact_bytes=result.artifact,
    )


# --- Receipt-3 wrapper and cache key -----------------------------------------


def test_wrap_carries_local_driver_input_byte_identical() -> None:
    build = _local_build_input()
    package = _package()

    wrapped = build_metadata.wrap_receipt_v3_input(package, build)

    assert wrapped.schema_version == 3
    assert wrapped.package == package
    assert wrapped.build == build
    assert protocol_json.canonical_bytes(
        wrapped.to_json()["build"]
    ) == protocol_json.canonical_bytes(build.to_json())
    assert set(wrapped.to_json()) == {"schema_version", "package", "build"}


def test_wrap_carries_external_driver_input_byte_identical(tmp_path: Path) -> None:
    _, _, result = _install_v3(tmp_path)
    assert result.receipt is not None

    build = build_metadata.read_receipt_v3(result.receipt).input.build

    assert isinstance(build, GoRepositoryBuildInput)
    assert build.schema_version == 2
    assert build.driver == "go-repository-v1"
    assert build.policy.execution_policy == "manager-worker-v1"
    expected = receipt_input(
        PipelineRequest(
            operation=Operation.INSTALL,
            command="tool",
            target="tool",
            declared=_declared(),
            effective=_effective(),
            acquire=_snapshot,
            audit=lambda subject: None,
        ),
        _effective(),
        BuildTarget(
            name="tool",
            driver="go-repository-v1",
            build_root="repo",
            source_dir="repo/cmd/tool",
        ),
        result.build_source,
        _compiler_identity(),
    )
    assert protocol_json.canonical_bytes(
        build.to_json()
    ) == protocol_json.canonical_bytes(expected)


@pytest.mark.parametrize(
    "digest", [PACKAGE_DIGEST, PACKAGE_DIGEST_THIRD], ids=["control-package", "fixed-package"]
)
def test_cache_key_is_sha256_over_the_whole_wrapped_input(digest: str) -> None:
    wrapped = build_metadata.wrap_receipt_v3_input(_package(digest), _local_build_input())

    key = build_metadata.source_aware_cache_key(wrapped)

    assert key == "sha256:" + hashlib.sha256(
        protocol_json.canonical_bytes(wrapped.to_json())
    ).hexdigest()
    assert key != "sha256:" + hashlib.sha256(
        protocol_json.canonical_bytes(wrapped.build.to_json())
    ).hexdigest()


def test_receipt_v3_reader_accepts_valid_and_refuses_invalid() -> None:
    package = {"kind": "local-snapshot", "snapshot": PACKAGE_DIGEST}
    valid = {
        "schema_version": 3,
        "cache_key": "sha256:" + "0" * 64,
        "input": {
            "schema_version": 3,
            "package": package,
            "build": _local_build_input().to_json(),
        },
        "artifact": {
            "path": "bin/golden-tool",
            "sha256": "sha256:" + "d" * 64,
            "size": 7,
        },
    }
    wrapped = build_metadata.parse_receipt_v3_input(valid["input"])
    valid["cache_key"] = build_metadata.source_aware_cache_key(wrapped)

    parsed = build_metadata.parse_receipt_v3(valid)

    assert isinstance(parsed, BuildReceiptV3)
    assert parsed.input == wrapped
    assert (
        build_metadata.canonical_receipt_v3_bytes(parsed)
        == protocol_json.canonical_bytes(valid)
    )

    bad_kind = {
        **valid,
        "input": {**valid["input"], "package": {"kind": "git", "snapshot": PACKAGE_DIGEST}},
    }
    with pytest.raises(BuildMetadataError):
        build_metadata.parse_receipt_v3(bad_kind)

    bad_build = {
        **valid,
        "input": {
            **valid["input"],
            "build": {**_local_build_input().to_json(), "driver": "go-v9"},
        },
    }
    with pytest.raises(BuildMetadataError):
        build_metadata.parse_receipt_v3(bad_build)

    unknown_member = {**valid, "unexpected": True}
    with pytest.raises(BuildMetadataError):
        build_metadata.parse_receipt_v3(unknown_member)


def test_wrap_roundtrips_every_package_kind() -> None:
    """The wrapper is package-kind-agnostic across all three identities."""

    build = _local_build_input()
    packages = [
        source_package.LocalSnapshot(snapshot=PACKAGE_DIGEST),
        source_package.NetworkGit(
            repository="github.com/example/tools",
            commit=source_package.LockedCommit(
                object_format="sha256", hex="a" * 64
            ),
        ),
        source_package.ConfiguredGit(
            source="legacy-skills",
            commit=source_package.LockedCommit(
                object_format="sha256", hex="b" * 64
            ),
        ),
    ]
    keys = set()
    for package in packages:
        wrapped = build_metadata.wrap_receipt_v3_input(package, build)
        assert wrapped.package == package
        reparsed = build_metadata.parse_receipt_v3_input(wrapped.to_json())
        assert reparsed == wrapped
        keys.add(build_metadata.source_aware_cache_key(wrapped))
    assert len(keys) == 3


def test_stored_receipt_v3_requires_exact_canonical_bytes() -> None:
    wrapped = build_metadata.wrap_receipt_v3_input(_package(), _local_build_input())
    receipt = build_metadata.build_receipt_v3(
        wrapped,
        BuildArtifact(
            path="bin/golden-tool", sha256="sha256:" + "d" * 64, size=7
        ),
    )
    canonical = build_metadata.canonical_receipt_v3_bytes(receipt)

    assert build_metadata.read_receipt_v3(canonical) == receipt
    with pytest.raises(BuildMetadataError) as excinfo:
        build_metadata.read_receipt_v3(canonical + b" ")
    assert excinfo.value.code == "receipt_not_canonical"


@pytest.mark.parametrize("copied", ["derived-other", "fixed"])
def test_copied_cache_key_is_refused_not_adopted(copied: str) -> None:
    """A cache key copied from another receipt cannot satisfy a receipt-3 read."""

    wrapped = build_metadata.wrap_receipt_v3_input(_package(), _local_build_input())
    other = build_metadata.wrap_receipt_v3_input(_package(PACKAGE_DIGEST_OTHER), _local_build_input())
    if copied == "derived-other":
        copied_key = build_metadata.source_aware_cache_key(other)
    else:
        copied_key = "sha256:" + "f" * 64
    assert copied_key != build_metadata.source_aware_cache_key(wrapped)
    forged = {
        "schema_version": 3,
        "cache_key": copied_key,
        "input": wrapped.to_json(),
        "artifact": {
            "path": "bin/golden-tool",
            "sha256": "sha256:" + "d" * 64,
            "size": 7,
        },
    }

    with pytest.raises(BuildMetadataError) as excinfo:
        build_metadata.parse_receipt_v3(forged)
    assert excinfo.value.code == "cache_key_mismatch"


def test_legacy_receipt_versions_are_unchanged() -> None:
    assert build_metadata.BUILD_INPUT_SCHEMA_VERSION == 1
    assert build_metadata.RECEIPT_SCHEMA_VERSION == 1
    assert build_metadata.REPOSITORY_BUILD_INPUT_SCHEMA_VERSION == 2
    assert build_metadata.RECEIPT_V3_SCHEMA_VERSION == 3
    assert set(build_metadata.SUPPORTED_RECEIPT_SCHEMA_VERSIONS) == {1}


def test_read_any_receipt_dispatches_by_schema_version() -> None:
    legacy = build_metadata.build_receipt(
        _local_build_input(),
        BuildArtifact(path="bin/golden-tool", sha256="sha256:" + "d" * 64, size=7),
    )
    wrapped = build_metadata.wrap_receipt_v3_input(_package(), _local_build_input())
    modern = build_metadata.build_receipt_v3(
        wrapped,
        BuildArtifact(path="bin/golden-tool", sha256="sha256:" + "d" * 64, size=7),
    )

    assert build_metadata.read_any_receipt(
        build_metadata.canonical_receipt_bytes(legacy)
    ) == legacy
    assert build_metadata.read_any_receipt(
        build_metadata.canonical_receipt_v3_bytes(modern)
    ) == modern


# --- Receipt-3 cache namespaces ----------------------------------------------


def _new_backend(tmp_path: Path) -> tuple[Path, Any]:
    home = tmp_path / ".cocoaskills"
    home.mkdir()
    provision_manager_home(home)
    return home, cache_for_manager_home(home)


def _publish_local(
    tmp_path: Path,
    backend: Any,
    build_input: Any,
    artifact_bytes: bytes,
) -> tuple[bytes, str]:
    if isinstance(build_input, SourceAwareBuildInput):
        receipt_object = build_metadata.build_receipt_v3(
            build_input,
            BuildArtifact(
                path=build_input.artifact_path,
                sha256="sha256:" + hashlib.sha256(artifact_bytes).hexdigest(),
                size=len(artifact_bytes),
            ),
        )
        receipt_bytes = build_metadata.canonical_receipt_v3_bytes(receipt_object)
    else:
        receipt_object = build_metadata.build_receipt(
            build_input,
            BuildArtifact(
                path=build_input.artifact_path,
                sha256="sha256:" + hashlib.sha256(artifact_bytes).hexdigest(),
                size=len(artifact_bytes),
            ),
        )
        receipt_bytes = build_metadata.canonical_receipt_bytes(receipt_object)
    source = tmp_path / f"source-{len(artifact_bytes)}-{build_input.command}"
    source.write_bytes(artifact_bytes)
    make_publication_source_private(source)
    backend.publish(
        CachePublication(input=build_input, receipt_bytes=receipt_bytes, artifact_source=source),
        guard=_HeldGuard(),
    )
    return receipt_bytes, build_metadata.receipt_sha256(receipt_bytes)


@pytest.mark.parametrize(
    "digest", [PACKAGE_DIGEST, PACKAGE_DIGEST_OTHER], ids=["fixed-package", "control-package"]
)
def test_legacy_cache_hit_cannot_satisfy_receipt3_lookup(
    tmp_path: Path, digest: str
) -> None:
    """A byte-identical driver input seeded in legacy never satisfies receipt 3."""

    home, backend = _new_backend(tmp_path)
    build = _local_build_input()
    wrapped = build_metadata.wrap_receipt_v3_input(_package(digest), build)
    _publish_local(tmp_path, backend, build, b"legacy executable")

    missed = backend.inspect(CacheExpectation(input=wrapped))

    assert missed.status is CacheEntryStatus.MISS
    assert (home / "builds" / "go-v1").is_dir()
    assert not (home / "builds" / "go-v1-receipt-v3").exists()
    legacy = backend.inspect(CacheExpectation(input=build))
    assert legacy.status is CacheEntryStatus.HIT


@pytest.mark.parametrize(
    "digest", [PACKAGE_DIGEST, PACKAGE_DIGEST_OTHER], ids=["fixed-package", "control-package"]
)
def test_receipt3_entries_live_in_their_own_namespace(
    tmp_path: Path, digest: str
) -> None:
    home, backend = _new_backend(tmp_path)
    build = _local_build_input()
    wrapped = build_metadata.wrap_receipt_v3_input(_package(digest), build)
    receipt_bytes, receipt_hash = _publish_local(tmp_path, backend, wrapped, b"v3 executable")

    hit = backend.inspect(
        CacheExpectation(input=wrapped, receipt_sha256=receipt_hash)
    )

    assert hit.status is CacheEntryStatus.HIT
    assert hit.receipt_bytes == receipt_bytes
    assert hit.receipt_sha256 == receipt_hash
    assert isinstance(hit.receipt, BuildReceiptV3)
    assert hit.receipt.input == wrapped
    assert hit.artifact_path == (
        home
        / "builds"
        / "go-v1-receipt-v3"
        / build_metadata.source_aware_cache_key(wrapped).removeprefix("sha256:")
        / Path(*wrapped.artifact_path.split("/"))
    )
    assert not (home / "builds" / "go-v1").exists()


def test_receipt3_lookup_rejects_a_foreign_receipt_hash(tmp_path: Path) -> None:
    home, backend = _new_backend(tmp_path)
    wrapped = build_metadata.wrap_receipt_v3_input(_package(), _local_build_input())
    _publish_local(tmp_path, backend, wrapped, b"v3 executable")

    inspection = backend.inspect(
        CacheExpectation(input=wrapped, receipt_sha256="sha256:" + "f" * 64)
    )

    assert inspection.status is CacheEntryStatus.CORRUPT


@pytest.mark.parametrize(
    "second", [b"second bytes", b"other-conflict-bytes"], ids=["fixed-size", "control-size"]
)
def test_receipt3_publication_conflict_rolls_back_atomically(
    tmp_path: Path, second: bytes
) -> None:
    home, backend = _new_backend(tmp_path)
    wrapped = build_metadata.wrap_receipt_v3_input(_package(), _local_build_input())
    _publish_local(tmp_path, backend, wrapped, b"first bytes")

    source = tmp_path / "source-conflict"
    source.write_bytes(second)
    make_publication_source_private(source)
    forged = build_metadata.build_receipt_v3(
        wrapped,
        BuildArtifact(
            path=wrapped.artifact_path,
            sha256="sha256:" + hashlib.sha256(second).hexdigest(),
            size=len(second),
        ),
    )
    before = _tree_bytes(home)
    with pytest.raises(build_cache.CacheConflictError):
        backend.publish(
            CachePublication(
                input=wrapped,
                receipt_bytes=build_metadata.canonical_receipt_v3_bytes(forged),
                artifact_source=source,
            ),
            guard=_HeldGuard(),
        )

    assert _tree_bytes(home) == before
    hit = backend.inspect(CacheExpectation(input=wrapped))
    assert hit.status is CacheEntryStatus.HIT
    assert hit.receipt_bytes is not None
    assert (
        build_metadata.read_receipt_v3(hit.receipt_bytes).artifact.sha256
        == "sha256:" + hashlib.sha256(b"first bytes").hexdigest()
    )
    assert list((home / ".builds-staging").iterdir()) == []


def test_receipt3_entries_quarantine_and_collect(tmp_path: Path) -> None:
    home, backend = _new_backend(tmp_path)
    legacy = _local_build_input()
    wrapped = build_metadata.wrap_receipt_v3_input(_package(), _local_build_input("other-tool"))
    _publish_local(tmp_path, backend, legacy, b"legacy executable")
    _publish_local(tmp_path, backend, wrapped, b"v3 executable")
    legacy_key = build_metadata.cache_key(legacy)
    wrapped_key = build_metadata.source_aware_cache_key(wrapped)

    moved = backend.quarantine(wrapped_key, guard=_HeldGuard())

    assert moved is not None
    assert backend.inspect(CacheExpectation(input=wrapped)).status is CacheEntryStatus.MISS
    assert backend.inspect(CacheExpectation(input=legacy)).status is CacheEntryStatus.HIT

    _publish_local(tmp_path, backend, wrapped, b"v3 executable")
    result = backend.collect([legacy_key], older_than=2**62, guard=_HeldGuard())

    assert result.removed == 1
    assert result.warnings == ()
    assert backend.inspect(CacheExpectation(input=wrapped)).status is CacheEntryStatus.MISS
    assert backend.inspect(CacheExpectation(input=legacy)).status is CacheEntryStatus.HIT


def test_external_receipt3_entries_live_below_artifacts_v3(tmp_path: Path) -> None:
    store, _, result = _install_v3(tmp_path)
    assert result.receipt is not None and result.cache_key is not None

    entry = (
        store.root
        / "artifacts-v3"
        / result.cache_key.removeprefix("sha256:")
    )

    assert entry.is_dir()
    assert not (store.root / "artifacts").exists()
    hit = store.lookup_receipt_v3(
        result.cache_key, _wrapped_input(result).to_json(), mutate=False
    )
    assert hit is not None
    assert hit.receipt == result.receipt
    assert hit.artifact == result.artifact


def test_external_legacy_entry_cannot_satisfy_receipt3_lookup(tmp_path: Path) -> None:
    store = DiskProtectedStore(tmp_path / "store")
    compiler = _Compiler([])
    snapshot = _snapshot()
    legacy_request = PipelineRequest(
        operation=Operation.INSTALL,
        command="tool",
        target="tool",
        declared=_declared(),
        effective=_effective(),
        acquire=lambda: snapshot,
        audit=lambda subject: None,
        store=store,
        compiler=compiler,
    )
    legacy = run_pipeline(legacy_request)
    assert legacy.receipt is not None and legacy.cache_key is not None
    assert (store.root / "artifacts").is_dir()

    wrapped = build_metadata.wrap_receipt_v3_input(
        _package(),
        build_metadata.parse_repository_build_input(
            receipt_input(
                legacy_request,
                _effective(),
                BuildTarget(
                    name="tool",
                    driver="go-repository-v1",
                    build_root="repo",
                    source_dir="repo/cmd/tool",
                ),
                legacy.build_source,
                compiler.identity,
            )
        ),
    )

    assert (
        store.lookup_receipt_v3(
            build_metadata.source_aware_cache_key(wrapped),
            wrapped.to_json(),
            mutate=False,
        )
        is None
    )


# --- Marker-5 build records --------------------------------------------------


def test_local_v5_record_retains_every_receipt_version_field() -> None:
    record = install_marker.InstallMarkerBuildV5(
        driver="go-v1",
        receipt_schema_version=3,
        execution_policy="manager-worker-v1",
        cache_key="sha256:" + "4" * 64,
        receipt_sha256="sha256:" + "0" * 64,
        artifact_sha256="sha256:" + "6" * 64,
        artifact_path="bin/golden-tool",
    )

    assert set(record.to_json()) == {
        "driver",
        "receipt_schema_version",
        "execution_policy",
        "cache_key",
        "receipt_sha256",
        "artifact_sha256",
        "artifact_path",
    }
    assert record.to_json()["receipt_schema_version"] == 3


def test_external_v5_record_retains_every_v2_field() -> None:
    build_source_identity = BuildSourceIdentity(
        algorithm="curator-build-source-v1",
        content_sha256="sha256:" + "b" * 64,
    )
    common: dict[str, Any] = {
        "driver": "go-repository-v1",
        "receipt_schema_version": 3,
        "execution_policy": "manager-worker-v1",
        "cache_key": "sha256:" + "4" * 64,
        "receipt_sha256": "sha256:" + "0" * 64,
        "artifact_sha256": "sha256:" + "6" * 64,
        "artifact_path": "bin/golden-tool",
        "repository": "golden-tools",
        "declared_identity": install_marker.MarkerRepositoryIdentity(
            kind="network-git", value="github.com/example/golden-tools"
        ),
        "declared_locked_commit": install_marker.MarkerRepositoryCommit(
            object_format="sha1", hex=COMMIT
        ),
        "effective_identity": install_marker.MarkerRepositoryIdentity(
            kind="network-git", value="github.com/example/golden-tools"
        ),
        "object_format": "sha1",
        "commit": COMMIT,
        "substituted": False,
        "substitution": None,
        "build_source": build_source_identity,
        "descriptor_target": "golden-tool",
    }
    plain = install_marker.InstallMarkerBuildV5(**common)
    assert set(plain.to_json()) == {
        "driver",
        "receipt_schema_version",
        "execution_policy",
        "repository",
        "declared_identity",
        "declared_locked_commit",
        "effective_identity",
        "object_format",
        "commit",
        "substituted",
        "build_source",
        "descriptor_target",
        "cache_key",
        "receipt_sha256",
        "artifact_sha256",
        "artifact_path",
    }

    adorned = install_marker.InstallMarkerBuildV5(
        **{
            **common,
            "declared_tag": "v1.4.0",
            "substituted": True,
            "substitution": install_marker.MarkerRepositorySubstitution(
                type="network-git",
                ref=install_marker.MarkerRepositoryRef(
                    kind="revision", value=REVISION_REF
                ),
            ),
        }
    )
    assert set(adorned.to_json()) == set(plain.to_json()) | {
        "declared_tag",
        "substitution",
    }
    assert adorned.to_json()["receipt_schema_version"] == 3


@pytest.mark.parametrize(
    ("builds", "build_source", "admitted"),
    [
        pytest.param({"tool": "local"}, True, True, id="local-requires-source"),
        pytest.param({"tool": "external"}, False, True, id="external-only-forbids-source"),
        pytest.param({}, False, True, id="empty-forbids-source"),
        pytest.param({"tool": "local"}, False, False, id="local-without-source-refused"),
        pytest.param({"tool": "external"}, True, False, id="external-only-with-source-refused"),
        pytest.param({}, True, False, id="empty-with-source-refused"),
        pytest.param(
            {"tool": "external", "helper": "local"},
            True,
            True,
            id="mixed-requires-source",
        ),
    ],
)
def test_top_level_build_source_exactly_for_local_commands(
    builds: Mapping[str, str], build_source: bool, admitted: bool
) -> None:
    def record(kind: str) -> install_marker.InstallMarkerBuildV5:
        if kind == "local":
            return install_marker.InstallMarkerBuildV5(
                driver="go-v1",
                receipt_schema_version=3,
                execution_policy="manager-worker-v1",
                cache_key="sha256:" + "4" * 64,
                receipt_sha256="sha256:" + "0" * 64,
                artifact_sha256="sha256:" + "6" * 64,
                artifact_path="bin/tool",
            )
        return install_marker.InstallMarkerBuildV5(
            driver="go-repository-v1",
            receipt_schema_version=3,
            execution_policy="manager-worker-v1",
            cache_key="sha256:" + "4" * 64,
            receipt_sha256="sha256:" + "0" * 64,
            artifact_sha256="sha256:" + "6" * 64,
            artifact_path="bin/tool",
            repository="tools",
            declared_identity=install_marker.MarkerRepositoryIdentity(
                kind="network-git", value="github.com/example/tools"
            ),
            declared_locked_commit=install_marker.MarkerRepositoryCommit(
                object_format="sha1", hex=COMMIT
            ),
            effective_identity=install_marker.MarkerRepositoryIdentity(
                kind="network-git", value="github.com/example/tools"
            ),
            object_format="sha1",
            commit=COMMIT,
            substituted=False,
            substitution=None,
            build_source=BuildSourceIdentity(
                algorithm="curator-build-source-v1",
                content_sha256="sha256:" + "b" * 64,
            ),
            descriptor_target="tool",
        )

    mapping = {name: record(kind) for name, kind in builds.items()}
    source = (
        BuildSourceIdentity(
            algorithm="curator-build-source-v1",
            content_sha256="sha256:" + "b" * 64,
        )
        if build_source
        else None
    )

    if admitted:
        install_marker.check_top_level_build_source(mapping, source)
    else:
        with pytest.raises(install_marker.InstallMarkerError) as excinfo:
            install_marker.check_top_level_build_source(mapping, source)
        assert excinfo.value.code == "top_level_build_source_mismatch"


# --- External-evidence comparison --------------------------------------------


def test_evidence_table_matches_the_literal_family() -> None:
    assert EXTERNAL_EVIDENCE_FIELDS == LITERAL_EVIDENCE_FIELDS
    assert len(set(EXTERNAL_EVIDENCE_FIELDS)) == 17


def test_matching_evidence_compares_clean(tmp_path: Path) -> None:
    """The positive control: genuine evidence agrees on every compared field."""

    _, _, result = _install_v3(tmp_path)
    package = _package()
    record = _genuine_record(result, package)

    assert _compare(record, result, package) == ()


@pytest.mark.parametrize("field", SINGLE_MISMATCH_FIELDS)
def test_single_field_mismatch_is_detected(tmp_path: Path, field: str) -> None:
    """Each refusal is one field away from a case that genuinely passes."""

    _, _, result = _install_v3(tmp_path)
    package = _package()
    record = _genuine_record(result, package)
    assert _compare(record, result, package) == ()

    assert _compare(_mutate_record(record, field), result, package) == (field,)


def test_input_package_mismatch_rederives_cache_key(tmp_path: Path) -> None:
    """A package change is detected, and re-derives the bound cache key."""

    _, _, result = _install_v3(tmp_path)
    package = _package()
    record = _genuine_record(result, package)
    assert _compare(record, result, package) == ()

    wrapped = build_metadata.wrap_receipt_v3_input(
        _package(PACKAGE_DIGEST_OTHER), _wrapped_input(result).build
    )

    assert set(_compare(record, result, package, wrapped)) == {
        "input.package",
        "cache_key",
    }


@pytest.mark.parametrize(
    ("field", "mutated"),
    [
        pytest.param(
            "declared_tag",
            "drop",
            id="declared-tag-drop",
        ),
    ],
)
def test_optional_field_absence_is_detected(
    tmp_path: Path, field: str, mutated: str
) -> None:
    _, _, result = _install_v3(tmp_path)
    package = _package()
    record = _genuine_record(result, package)
    assert record.declared_tag == "v1.0.0"

    dropped = replace(record, declared_tag=None)

    assert _compare(dropped, result, package) == (field,)


def test_substituted_flip_is_detected_with_its_substitution(tmp_path: Path) -> None:
    _, _, result = _install_v3(tmp_path, substituted=False)
    package = _package()
    record = _genuine_record(result, package)
    assert record.substituted is False
    assert _compare(record, result, package) == ()

    flipped = replace(
        record,
        substituted=True,
        substitution=install_marker.MarkerRepositorySubstitution(
            type="network-git",
            ref=install_marker.MarkerRepositoryRef(
                kind="revision", value=REVISION_REF
            ),
        ),
    )

    differing = _compare(flipped, result, package)
    assert "substituted" in differing
    assert "substitution" in differing


def test_object_format_change_is_detected_with_its_commit(tmp_path: Path) -> None:
    _, _, result = _install_v3(tmp_path)
    package = _package()
    record = _genuine_record(result, package)
    assert _compare(record, result, package) == ()

    widened = replace(
        record,
        object_format="sha256",
        commit="ab" * 32,
        substitution=install_marker.MarkerRepositorySubstitution(
            type="network-git",
            ref=install_marker.MarkerRepositoryRef(
                kind="revision", value="cd" * 32
            ),
        ),
    )

    differing = _compare(widened, result, package)
    assert "object_format" in differing
    assert "commit" in differing


def test_execution_policy_mismatch_is_unrepresentable_and_refused(
    tmp_path: Path,
) -> None:
    """No second valid execution policy exists: the mismatch refuses at rest."""

    _, _, result = _install_v3(tmp_path)
    record = _genuine_record(result, _package())

    with pytest.raises(install_marker.InstallMarkerError):
        replace(record, execution_policy="arbitrary")


def test_evidence_comparison_rejects_a_local_record(tmp_path: Path) -> None:
    _, _, result = _install_v3(tmp_path)
    local = install_marker.InstallMarkerBuildV5(
        driver="go-v1",
        receipt_schema_version=3,
        execution_policy="manager-worker-v1",
        cache_key="sha256:" + "4" * 64,
        receipt_sha256="sha256:" + "0" * 64,
        artifact_sha256="sha256:" + "6" * 64,
        artifact_path="bin/tool",
    )

    with pytest.raises(BuildCurrentnessError) as excinfo:
        _compare(local, result, _package())
    assert excinfo.value.code == "external_evidence_invalid"


def test_repair_revalidates_exact_source_and_rebuilds(tmp_path: Path) -> None:
    """Repair re-derives from the locked source; it never adopts the record."""

    _, _, installed = _install_v3(tmp_path)
    package = _package()
    record = _genuine_record(installed, package)
    assert _compare(record, installed, package) == ()
    stale = _mutate_record(record, "cache_key")
    assert _compare(stale, installed, package) == ("cache_key",)

    store = DiskProtectedStore(tmp_path / "repair-store")
    events: list[str] = []
    compiler = _Compiler(events)
    snapshot = _snapshot()
    observed: list[str] = []
    _counting_cache_hook(events)(store)

    def acquire() -> Snapshot:
        observed.append(snapshot.commit)
        events.append("acquire")
        return snapshot

    def audit(subject: object) -> None:
        events.append("audit")

    repaired = run_pipeline(
        PipelineRequest(
            operation=Operation.REPAIR,
            command="tool",
            target="tool",
            declared=_declared(),
            effective=_effective(),
            acquire=acquire,
            audit=audit,
            store=store,
            compiler=compiler,
            package=package,
        )
    )

    assert observed == [COMMIT]
    # Repair audits before its first cache read exactly like install does; a
    # repair-only lookup moved before the audit reorders these events.
    assert events == ["acquire", "audit", "cache", "compiler"]
    assert compiler.calls == 1
    assert repaired.receipt == installed.receipt
    assert repaired.cache_key == installed.cache_key
    assert repaired.cache_key != stale.cache_key


def test_repair_refuses_a_tampered_source(tmp_path: Path) -> None:
    store = DiskProtectedStore(tmp_path / "store")
    compiler = _Compiler([])
    genuine = _snapshot()
    tampered = Snapshot(
        object_format=genuine.object_format,
        commit=COMMIT_OTHER,
        files=genuine.files,
        canonical_bytes=genuine.canonical_bytes,
        digest=genuine.digest,
        tag_verified=True,
    )

    with pytest.raises(ExternalBuildError) as excinfo:
        run_pipeline(
            PipelineRequest(
                operation=Operation.REPAIR,
                command="tool",
                target="tool",
                declared=_declared(),
                effective=_effective(),
                acquire=lambda: tampered,
                audit=lambda subject: None,
                store=store,
                compiler=compiler,
                package=_package(),
            )
        )
    assert excinfo.value.code == EXTERNAL_BUILD_IDENTITY_INVALID
    assert compiler.calls == 0


def test_v3_build_refuses_unverified_declared_tag(tmp_path: Path) -> None:
    """Exact-tag provenance holds on the receipt-3 path: no verify, no build."""

    store = DiskProtectedStore(tmp_path / "store")
    compiler = _Compiler([])

    with pytest.raises(ExternalBuildError):
        run_pipeline(
            PipelineRequest(
                operation=Operation.INSTALL,
                command="tool",
                target="tool",
                declared=_declared(tag="v1.0.0"),
                effective=_effective(substituted=False),
                acquire=lambda: _snapshot(tag_verified=False),
                audit=lambda subject: None,
                store=store,
                compiler=compiler,
                package=_package(),
            )
        )
    assert compiler.calls == 0


def test_pipeline_audit_error_is_structured() -> None:
    """A non-substitution audit failure still surfaces as a typed refusal."""

    import tempfile

    with tempfile.TemporaryDirectory(prefix="csk-audit-error-") as raw:
        store = DiskProtectedStore(Path(raw) / "store")
        compiler = _Compiler([])

        def failing_audit(subject: object) -> None:
            raise ValueError("boom")

        with pytest.raises(ExternalBuildError) as excinfo:
            run_pipeline(
                PipelineRequest(
                    operation=Operation.INSTALL,
                    command="tool",
                    target="tool",
                    declared=_declared(),
                    effective=_effective(),
                    acquire=_snapshot,
                    audit=failing_audit,
                    store=store,
                    compiler=compiler,
                    package=_package(),
                )
            )
    assert excinfo.value.code == AUDIT_BLOCKED
    assert isinstance(excinfo.value.__cause__, ValueError)


# --- Audit-before-cache/compiler ordering ------------------------------------


def _counting_cache_hook(events: list[str]) -> Any:
    """Wrap a store's receipt-3 lookup so every cache read is an event."""

    def hook(store: DiskProtectedStore) -> None:
        real_lookup = store.lookup_receipt_v3

        def counting_lookup(*args: Any, **kwargs: Any) -> Any:
            events.append("cache")
            return real_lookup(*args, **kwargs)

        store.lookup_receipt_v3 = counting_lookup  # type: ignore[method-assign]

    return hook


def test_pipeline_audits_before_cache_and_compiler(tmp_path: Path) -> None:
    events: list[str] = []
    _, compiler, result = _install_v3(
        tmp_path, events=events, store_hook=_counting_cache_hook(events)
    )

    assert result.state == "would-preflight-and-build"
    # The whole-snapshot audit precedes the first cache read, which precedes
    # the first compiler invocation; a lookup moved before the audit reorders
    # these events and fails here.
    assert events == ["acquire", "audit", "cache", "compiler"]
    assert compiler.calls == 1


def test_pipeline_strict_refusal_reads_no_cache_and_runs_no_compiler(
    tmp_path: Path,
) -> None:
    store = DiskProtectedStore(tmp_path / "store")
    compiler = _Compiler([])
    cache_reads = 0
    real_lookup = store.lookup_receipt_v3

    def counting_lookup(*args: Any, **kwargs: Any) -> Any:
        nonlocal cache_reads
        cache_reads += 1
        return real_lookup(*args, **kwargs)

    def strict_audit(subject: object) -> None:
        check_source_substitution_admission(
            selector_kind="legacy",
            operator_substitution=False,
            external_substitution=True,
            strict_audit=True,
        )

    store.lookup_receipt_v3 = counting_lookup  # type: ignore[method-assign]
    with pytest.raises(ExternalBuildError) as excinfo:
        run_pipeline(
            PipelineRequest(
                operation=Operation.INSTALL,
                command="tool",
                target="tool",
                declared=_declared(),
                effective=_effective(),
                acquire=_snapshot,
                audit=strict_audit,
                store=store,
                compiler=compiler,
                package=_package(),
            )
        )

    assert excinfo.value.code == AUDIT_BLOCKED
    assert isinstance(excinfo.value.__cause__, DevSubstitutionError)
    assert cache_reads == 0
    assert compiler.calls == 0


class _RecordingCache:
    """A fake backend that records every read-only inspection."""

    def __init__(self, manager_home: Path, events: list[str]) -> None:
        self.manager_home = manager_home
        self._events = events

    def inspect(self, expectation: Any) -> Any:
        self._events.append(f"cache:{expectation.input.command}")
        return build_cache.CacheInspection(
            status=CacheEntryStatus.MISS, reason="recording fixture"
        )

    def publish(self, *args: object, **kwargs: object) -> Any:
        raise AssertionError("planning must not publish cache entries")

    def quarantine(self, *args: object, **kwargs: object) -> Any:
        raise AssertionError("planning must not quarantine cache entries")

    def collect(self, *args: object, **kwargs: object) -> Any:
        raise AssertionError("planning must not collect cache entries")


class _RecordingToolchainSession:
    def __init__(self, events: list[str]) -> None:
        self.target = NativeTarget(
            goos="darwin", goarch="arm64", tuning={"GOARM64": "v8.0"}
        )
        self.toolchain = ToolchainIdentity(
            algorithm=TOOLCHAIN_ALGORITHM,
            content_sha256="sha256:" + "c" * 64,
            go_relpath=GO_RELPATH,
            go_version="go version go1.26.1 darwin/arm64",
        )
        self._events = events

    def __enter__(self) -> _RecordingToolchainSession:
        self._events.append("toolchain")
        return self

    def __exit__(self, *args: object) -> None:
        pass


def _planning_provider(
    snapshot: build_source.FrozenSnapshot,
    package: source_package.LocalSnapshot | None,
    name: str = "provider",
) -> build_planner.BuildProvider:
    return build_planner.BuildProvider(
        name=name,
        snapshot=snapshot,
        commands=(
            build_planner.BuildCommand(
                name="golden-tool",
                driver="go-v1",
                build_root="build",
                source_dir="build/cmd/golden-tool",
            ),
        ),
        package=package,
    )


def _tree_bytes(home: Path) -> list[tuple[str, str]]:
    """Hash every file below a manager home for before/after comparison."""

    entries = []
    for path in sorted(home.rglob("*")):
        if path.is_file() and not path.is_symlink():
            entries.append(
                (
                    path.relative_to(home).as_posix(),
                    hashlib.sha256(path.read_bytes()).hexdigest(),
                )
            )
    return entries


def test_planner_audits_before_cache_reads(tmp_path: Path) -> None:
    root = tmp_path / "root"
    root.mkdir()
    (root / "source.txt").write_text("source", encoding="utf-8")
    manager_home = tmp_path / "manager"
    manager_home.mkdir()
    events: list[str] = []
    backend = _RecordingCache(manager_home, events)

    def establish(config: build_toolchain.ToolchainConfig) -> _RecordingToolchainSession:
        return _RecordingToolchainSession(events)

    def audit(providers: tuple[build_planner.BuildProvider, ...]) -> None:
        events.append("audit")
        assert len(providers) == 1

    with build_source.freeze_snapshot(root) as frozen:
        plans = build_planner.plan_builds(
            (_planning_provider(frozen, _package()),),
            manager_home=manager_home,
            operator_search_path=(),
            cache_backend=backend,  # type: ignore[arg-type]
            establish_toolchain=establish,  # type: ignore[arg-type]
            audit=audit,
        )

    assert len(plans) == 1
    assert events[0] == "audit"
    assert events[1] == "toolchain"
    assert events[2] == "cache:golden-tool"
    assert isinstance(plans[0].input, SourceAwareBuildInput)
    assert plans[0].cache_key == build_metadata.source_aware_cache_key(plans[0].input)
    assert plans[0].to_json()["package"] == {
        "kind": "local-snapshot",
        "snapshot": PACKAGE_DIGEST,
    }


def test_planner_strict_refusal_reads_no_cache(tmp_path: Path) -> None:
    root = tmp_path / "root"
    root.mkdir()
    (root / "source.txt").write_text("source", encoding="utf-8")
    manager_home = tmp_path / "manager"
    manager_home.mkdir()
    events: list[str] = []
    backend = _RecordingCache(manager_home, events)

    def establish(config: build_toolchain.ToolchainConfig) -> _RecordingToolchainSession:
        events.append("toolchain")
        return _RecordingToolchainSession(events)

    def strict_audit(providers: tuple[build_planner.BuildProvider, ...]) -> None:
        events.append("audit")
        check_source_substitution_admission(
            selector_kind="legacy",
            operator_substitution=False,
            external_substitution=True,
            strict_audit=True,
        )

    with build_source.freeze_snapshot(root) as frozen:
        with pytest.raises(DevSubstitutionError):
            build_planner.plan_builds(
                (_planning_provider(frozen, _package()),),
                manager_home=manager_home,
                operator_search_path=(),
                cache_backend=backend,  # type: ignore[arg-type]
                establish_toolchain=establish,  # type: ignore[arg-type]
                audit=strict_audit,
            )

    assert events == ["audit"]


def test_planner_audits_whole_provider_set_once(tmp_path: Path) -> None:
    root = tmp_path / "root"
    root.mkdir()
    (root / "source.txt").write_text("source", encoding="utf-8")
    manager_home = tmp_path / "manager"
    manager_home.mkdir()
    events: list[str] = []
    backend = _RecordingCache(manager_home, events)
    observed: list[tuple[str, ...]] = []

    def establish(config: build_toolchain.ToolchainConfig) -> _RecordingToolchainSession:
        return _RecordingToolchainSession(events)

    def audit(providers: tuple[build_planner.BuildProvider, ...]) -> None:
        events.append("audit")
        observed.append(tuple(provider.name for provider in providers))

    with build_source.freeze_snapshot(root) as frozen:
        plans = build_planner.plan_builds(
            (
                _planning_provider(frozen, _package(), name="provider"),
                _planning_provider(frozen, _package(), name="second"),
            ),
            manager_home=manager_home,
            operator_search_path=(),
            cache_backend=backend,  # type: ignore[arg-type]
            establish_toolchain=establish,  # type: ignore[arg-type]
            audit=audit,
        )

    assert len(plans) == 2
    assert observed == [("provider", "second")]
    assert events[0] == "audit"
    assert [event for event in events if event.startswith("cache:")] == [
        "cache:golden-tool",
        "cache:golden-tool",
    ]


def test_planner_without_package_keeps_legacy_inputs(tmp_path: Path) -> None:
    root = tmp_path / "root"
    root.mkdir()
    (root / "source.txt").write_text("source", encoding="utf-8")
    manager_home = tmp_path / "manager"
    manager_home.mkdir()
    events: list[str] = []
    backend = _RecordingCache(manager_home, events)

    def establish(config: build_toolchain.ToolchainConfig) -> _RecordingToolchainSession:
        return _RecordingToolchainSession(events)

    with build_source.freeze_snapshot(root) as frozen:
        plans = build_planner.plan_builds(
            (_planning_provider(frozen, None),),
            manager_home=manager_home,
            operator_search_path=(),
            cache_backend=backend,  # type: ignore[arg-type]
            establish_toolchain=establish,  # type: ignore[arg-type]
        )

    assert len(plans) == 1
    assert isinstance(plans[0].input, GoBuildInput)
    assert "package" not in plans[0].to_json()


# --- Package invalidation ----------------------------------------------------


def _inventory(files: Mapping[str, str]) -> dict[str, object]:
    return {
        "schema_version": 1,
        "algorithm": "curator-local-snapshot-v1",
        "files": [
            {"path": path, "sha256": digest, "executable": False}
            for path, digest in sorted(files.items())
        ],
    }


def test_v5_records_retain_v3_shapes_with_only_version_changed() -> None:
    """Marker-5 records keep every v3 field; only the version const moves."""

    build_source_identity = BuildSourceIdentity(
        algorithm="curator-build-source-v1",
        content_sha256="sha256:" + "b" * 64,
    )
    local_common: dict[str, Any] = {
        "driver": "go-v1",
        "execution_policy": "manager-worker-v1",
        "cache_key": "sha256:" + "4" * 64,
        "receipt_sha256": "sha256:" + "0" * 64,
        "artifact_sha256": "sha256:" + "6" * 64,
        "artifact_path": "bin/golden-tool",
    }
    external_common: dict[str, Any] = {
        **local_common,
        "driver": "go-repository-v1",
        "repository": "golden-tools",
        "declared_identity": install_marker.MarkerRepositoryIdentity(
            kind="network-git", value="github.com/example/golden-tools"
        ),
        "declared_locked_commit": install_marker.MarkerRepositoryCommit(
            object_format="sha1", hex=COMMIT
        ),
        "declared_tag": "v1.4.0",
        "effective_identity": install_marker.MarkerRepositoryIdentity(
            kind="network-git", value="github.com/example/golden-tools"
        ),
        "object_format": "sha1",
        "commit": COMMIT,
        "substituted": True,
        "substitution": install_marker.MarkerRepositorySubstitution(
            type="network-git",
            ref=install_marker.MarkerRepositoryRef(
                kind="revision", value=REVISION_REF
            ),
        ),
        "build_source": build_source_identity,
        "descriptor_target": "golden-tool",
    }
    local_v3 = install_marker.InstallMarkerBuildV3(
        **local_common, receipt_schema_version=1
    )
    local_v5 = install_marker.InstallMarkerBuildV5(
        **local_common, receipt_schema_version=3
    )
    assert set(local_v5.to_json()) == set(local_v3.to_json())
    assert {
        key: value
        for key, value in local_v5.to_json().items()
        if key != "receipt_schema_version"
    } == {
        key: value
        for key, value in local_v3.to_json().items()
        if key != "receipt_schema_version"
    }
    external_v3 = install_marker.InstallMarkerBuildV3(
        **external_common, receipt_schema_version=2
    )
    external_v5 = install_marker.InstallMarkerBuildV5(
        **external_common, receipt_schema_version=3
    )
    assert set(external_v5.to_json()) == set(external_v3.to_json())
    assert {
        key: value
        for key, value in external_v5.to_json().items()
        if key != "receipt_schema_version"
    } == {
        key: value
        for key, value in external_v3.to_json().items()
        if key != "receipt_schema_version"
    }


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX activation layout")
def test_local_receipt3_build_classifies_current(tmp_path: Path) -> None:
    """A local receipt-3 plan, record, cache entry, and shim agree: current.

    Unix activation only: the darwin fixture classifies under
    ``platform_name="unix"``, and activation requires the artifact to
    be owner-executable as observed through ``st_mode`` — a POSIX
    mechanism Windows does not provide (its ``st_mode`` never
    carries execute bits), so this verdict is unreachable there.
    Same skip as the unix-activation success paths in
    ``test_build_activation.py``.
    """

    from csk import shims
    from csk.builds.currentness import classify_build
    from csk.skillspec import CommandSpec

    csk_home = tmp_path / ".cocoaskills"
    csk_home.mkdir()
    provision_manager_home(csk_home)
    backend = cache_for_manager_home(csk_home)
    build = _local_build_input()
    wrapped = build_metadata.wrap_receipt_v3_input(_package(), build)
    artifact = b"v3 local executable"
    _, receipt_hash = _publish_local(tmp_path, backend, wrapped, artifact)
    inspection = backend.inspect(CacheExpectation(input=wrapped))
    assert inspection.status is CacheEntryStatus.HIT
    assert inspection.artifact_path is not None
    plan = build_planner.BuildPlan(
        provider="provider",
        input=wrapped,
        cache_key=build_metadata.source_aware_cache_key(wrapped),
        inspection=inspection,
    )
    record = install_marker.InstallMarkerBuildV5(
        driver="go-v1",
        receipt_schema_version=3,
        execution_policy="manager-worker-v1",
        cache_key=plan.cache_key,
        receipt_sha256=receipt_hash,
        artifact_sha256="sha256:" + hashlib.sha256(artifact).hexdigest(),
        artifact_path="bin/golden-tool",
    )
    command = CommandSpec(name="golden-tool", type="build", driver="go-v1")
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    shims.write_bin_shim(
        bin_dir, "golden-tool", inspection.artifact_path, platform_name="unix"
    )

    status = classify_build(
        csk_home=csk_home,
        bin_dir=bin_dir,
        provider="provider",
        command=command,
        plan=plan,
        recorded=record,
        cache_backend=backend,
        path_entries=(),
        platform_name="unix",
    )

    assert status.label == "current"


def test_local_receipt3_build_without_owner_execute_classifies_marker_drift(
    tmp_path: Path,
) -> None:
    """A non-owner-executable artifact classifies ``build-marker-drift``.

    The pinning test for the windows-latest ``current`` failure: unix
    activation requires the artifact to be owner-executable as
    observed through ``st_mode``, and Windows ``st_mode`` never
    carries execute bits, so the skipped test above can only report
    this verdict there. The artifact here carries the exact receipt
    bytes and size with mode ``0o644`` — the permission state a
    Windows host always reports — and production ``classify_build``
    maps the activation refusal to ``build-marker-drift`` naming
    the owner-executable cause.

    Everything classified is real: the receipt-3 bytes, the plan,
    the marker record and the ``stat`` observation. Only the cache
    backend is a stub returning the plan's own inspection for the
    re-read, which classification never reaches: activation refuses
    first. Runs on every host, including Windows, where the file
    likewise lacks the bit.
    """

    from csk.builds.currentness import classify_build
    from csk.skillspec import CommandSpec

    csk_home = tmp_path / ".cocoaskills"
    artifact_path = csk_home / "builds" / "entry" / "bin" / "golden-tool"
    artifact_path.parent.mkdir(parents=True)
    artifact = b"v3 local executable"
    artifact_path.write_bytes(artifact)
    artifact_path.chmod(0o644)
    assert not stat.S_IMODE(artifact_path.stat().st_mode) & stat.S_IXUSR
    build = _local_build_input()
    wrapped = build_metadata.wrap_receipt_v3_input(_package(), build)
    receipt_object = build_metadata.build_receipt_v3(
        wrapped,
        BuildArtifact(
            path=wrapped.artifact_path,
            sha256="sha256:" + hashlib.sha256(artifact).hexdigest(),
            size=len(artifact),
        ),
    )
    receipt_bytes = build_metadata.canonical_receipt_v3_bytes(receipt_object)
    receipt_hash = build_metadata.receipt_sha256(receipt_bytes)
    inspection = build_cache.CacheInspection(
        status=build_cache.CacheEntryStatus.HIT,
        reason="exact protected entry",
        receipt=receipt_object,
        receipt_bytes=receipt_bytes,
        receipt_sha256=receipt_hash,
        artifact_path=artifact_path,
    )
    plan = build_planner.BuildPlan(
        provider="provider",
        input=wrapped,
        cache_key=build_metadata.source_aware_cache_key(wrapped),
        inspection=inspection,
    )
    record = install_marker.InstallMarkerBuildV5(
        driver="go-v1",
        receipt_schema_version=3,
        execution_policy="manager-worker-v1",
        cache_key=plan.cache_key,
        receipt_sha256=receipt_hash,
        artifact_sha256="sha256:" + hashlib.sha256(artifact).hexdigest(),
        artifact_path="bin/golden-tool",
    )
    command = CommandSpec(name="golden-tool", type="build", driver="go-v1")

    class _FixedBackend:
        def inspect(self, expectation: object) -> object:
            return inspection

    status = classify_build(
        csk_home=csk_home,
        bin_dir=tmp_path / "bin",
        provider="provider",
        command=command,
        plan=plan,
        recorded=record,
        cache_backend=_FixedBackend(),  # type: ignore[arg-type]
        path_entries=(),
        platform_name="unix",
    )

    assert status.label == "build-marker-drift"
    assert "not owner-executable" in status.detail


def test_runtime_and_build_edits_each_change_package_and_cache_key() -> None:
    build = _local_build_input()
    runtime_digest = "sha256:" + hashlib.sha256(b"skill").hexdigest()
    build_digest = "sha256:" + hashlib.sha256(b"package main").hexdigest()
    base = inventory_digest(
        _inventory({"SKILL.md": runtime_digest, "build/main.go": build_digest})
    )
    runtime_edit = inventory_digest(
        _inventory(
            {
                "SKILL.md": "sha256:" + hashlib.sha256(b"skill!").hexdigest(),
                "build/main.go": build_digest,
            }
        )
    )
    build_edit = inventory_digest(
        _inventory(
            {
                "SKILL.md": runtime_digest,
                "build/main.go": "sha256:" + hashlib.sha256(b"package other").hexdigest(),
            }
        )
    )
    assert len({base, runtime_edit, build_edit}) == 3

    keys = {
        build_metadata.source_aware_cache_key(
            build_metadata.wrap_receipt_v3_input(
                source_package.LocalSnapshot(snapshot=digest), build
            )
        )
        for digest in (base, runtime_edit, build_edit)
    }

    assert len(keys) == 3


# --- GC mark/sweep namespace parity ------------------------------------------
#
# Review rev1 F1: the sweep walked the receipt-3 namespaces while the mark
# contributed no marker-5 reference, so `csk gc` destroyed live receipt-3
# entries by construction. Both sides now derive from one namespace table per
# cache family. These tests pin that triangle through the public
# `gc.collect_runtime`: producers publish exactly the declared namespaces, the
# sweep walks exactly them, and the mark retains a marker-bound entry in each
# of them, so a namespace added to one side only fails here.

POSIX_GC_NAMESPACES = pytest.mark.skipif(
    os.name != "posix",
    reason="Exercises the POSIX protected build cache collector",
)
EXTERNAL_SWEEP_POSIX = pytest.mark.skipif(
    os.name == "nt",
    reason=(
        "External sweep removes sealed entries; sealed-DACL recursive removal "
        "is a named platform bound, verified on POSIX"
    ),
)


def _gc_config(home: Path, tmp_path: Path) -> GlobalConfig:
    return GlobalConfig(
        path=home / "config.json",
        skills_root=tmp_path / "skills-root",
        preferred_locale=None,
        default_agents=["codex_cli"],
        adapter_mode="auto",
        worktree_alias_pattern="[A-Z]+-[0-9]+",
        projects={},
    )


def _write_skill_marker(home: Path, skill: str, marker: Any) -> Path:
    skill_dir = home / "global" / "skills" / skill
    skill_dir.mkdir(parents=True)
    marker_path = skill_dir / ".csk-install.json"
    marker_path.write_bytes(install_marker.serialize_install_marker(marker.to_json()))
    return marker_path


def _local_v5_marker(
    package: Any,
    build: GoBuildInput,
    cache_key: str,
    receipt_bytes: bytes,
    artifact_bytes: bytes,
    artifact_path: str,
    command: str = "golden-tool",
) -> install_marker.InstallMarkerV5:
    return install_marker.InstallMarkerV5(
        name="golden-skill",
        package=package,
        lock_sha256="sha256:" + "5" * 64,
        content_sha256="sha256:" + "6" * 64,
        locale=None,
        agents=(),
        commands=(command,),
        dependencies=(),
        skill_schema_version=8,
        runtime_roots=(),
        build_roots=("build",),
        installed_at="2000-01-01T00:00:00Z",
        files=("SKILL.md",),
        builds={
            command: install_marker.InstallMarkerBuildV5(
                driver="go-v1",
                receipt_schema_version=3,
                execution_policy="manager-worker-v1",
                cache_key=cache_key,
                receipt_sha256=build_metadata.receipt_sha256(receipt_bytes),
                artifact_sha256="sha256:" + hashlib.sha256(artifact_bytes).hexdigest(),
                artifact_path=artifact_path,
            )
        },
        build_source=build.build_source,
    )


def _local_v4_marker(
    build: GoBuildInput,
    cache_key: str,
    receipt_bytes: bytes,
    artifact_bytes: bytes,
    command: str = "golden-tool",
) -> install_marker.InstallMarkerV4:
    return install_marker.InstallMarkerV4(
        name="golden-skill",
        source="github.com/example/golden",
        ref_kind="tag",
        ref="v1",
        commit="a" * 40,
        content_sha256="sha256:" + "6" * 64,
        locale=None,
        agents=(),
        commands=(command,),
        dependencies=(),
        skill_schema_version=8,
        runtime_roots=(),
        build_roots=("build",),
        installed_at="2000-01-01T00:00:00Z",
        files=("SKILL.md",),
        builds={
            command: install_marker.InstallMarkerBuildV3(
                driver="go-v1",
                receipt_schema_version=1,
                execution_policy="manager-worker-v1",
                cache_key=cache_key,
                receipt_sha256=build_metadata.receipt_sha256(receipt_bytes),
                artifact_sha256="sha256:" + hashlib.sha256(artifact_bytes).hexdigest(),
                artifact_path=build.artifact_path,
            )
        },
        build_source=build.build_source,
    )


def _age_entry(entry: Path, days: int = 10) -> None:
    old = time.time() - days * 24 * 3600
    os.utime(entry, (old, old), follow_symlinks=False)


@POSIX_GC_NAMESPACES
def test_gc_retains_marker_v5_bound_receipt_v3_entry(tmp_path: Path) -> None:
    """Review rev1 F1 reproduction: gc keeps a live receipt-3 entry."""

    home, backend = _new_backend(tmp_path)
    build = _local_build_input()
    package = _package()
    wrapped = build_metadata.wrap_receipt_v3_input(package, build)
    artifact = b"receipt-3 executable"
    receipt_bytes, _ = _publish_local(tmp_path, backend, wrapped, artifact)
    key = build_metadata.read_receipt_v3(receipt_bytes).cache_key
    entry = home / "builds" / "go-v1-receipt-v3" / key.removeprefix("sha256:")
    assert entry.is_dir()
    marker_path = _write_skill_marker(
        home,
        "golden-skill",
        _local_v5_marker(
            package, build, key, receipt_bytes, artifact, wrapped.artifact_path
        ),
    )
    readback = install_marker.read_install_marker(marker_path.read_bytes())
    assert readback.builds["golden-tool"].cache_key == key
    # Positive control: an unreferenced aged receipt-3 entry is swept.
    stale_wrapped = build_metadata.wrap_receipt_v3_input(
        package, _local_build_input(command="stale-tool")
    )
    stale_receipt, _ = _publish_local(tmp_path, backend, stale_wrapped, b"stale")
    stale_key = build_metadata.read_receipt_v3(stale_receipt).cache_key
    assert stale_key != key
    stale_entry = (
        home / "builds" / "go-v1-receipt-v3" / stale_key.removeprefix("sha256:")
    )
    _age_entry(entry)
    _age_entry(stale_entry)

    stats = gc.collect_runtime(_gc_config(home, tmp_path), home)

    assert stats.builds_removed == 1
    assert stats.warnings == []
    assert entry.is_dir()
    assert backend.inspect(CacheExpectation(input=wrapped)).status is (
        CacheEntryStatus.HIT
    )
    assert not stale_entry.exists()


@POSIX_GC_NAMESPACES
def test_gc_control_legacy_entry_still_retained(tmp_path: Path) -> None:
    """F1 control: the same run retains a legacy entry bound by a v4 marker."""

    home, backend = _new_backend(tmp_path)
    build = _local_build_input()
    artifact = b"legacy executable"
    receipt_bytes, _ = _publish_local(tmp_path, backend, build, artifact)
    key = build_metadata.read_any_receipt(receipt_bytes).cache_key
    entry = home / "builds" / "go-v1" / key.removeprefix("sha256:")
    assert entry.is_dir()
    _write_skill_marker(
        home, "golden-skill", _local_v4_marker(build, key, receipt_bytes, artifact)
    )
    _age_entry(entry)

    stats = gc.collect_runtime(_gc_config(home, tmp_path), home)

    assert stats.builds_removed == 0
    assert stats.warnings == []
    assert entry.is_dir()
    assert backend.inspect(CacheExpectation(input=build)).status is (
        CacheEntryStatus.HIT
    )


def test_local_namespace_declaration_matches_producers() -> None:
    """Both backends route inputs to exactly the declared local namespaces."""

    legacy = _local_build_input()
    wrapped = build_metadata.wrap_receipt_v3_input(_package(), _local_build_input())
    assert {
        cache_posix._driver_directory_for_input(legacy),
        cache_posix._driver_directory_for_input(wrapped),
    } == set(LOCAL_BUILD_CACHE_NAMESPACES)
    assert {
        cache_windows._driver_directory_for_input(legacy),
        cache_windows._driver_directory_for_input(wrapped),
    } == set(LOCAL_BUILD_CACHE_NAMESPACES)


@POSIX_GC_NAMESPACES
@pytest.mark.parametrize("namespace", list(LOCAL_BUILD_CACHE_NAMESPACES))
def test_gc_marks_and_sweeps_every_declared_local_namespace(
    tmp_path: Path, namespace: str
) -> None:
    """Growth test: every declared local namespace is marked and swept."""

    home, backend = _new_backend(tmp_path)
    legacy = _local_build_input()
    wrapped = build_metadata.wrap_receipt_v3_input(_package(), _local_build_input())
    routed = {
        id(candidate): cache_posix._driver_directory_for_input(candidate)
        for candidate in (legacy, wrapped)
    }
    by_id = {id(legacy): legacy, id(wrapped): wrapped}
    matching = [by_id[key] for key, routed_to in routed.items() if routed_to == namespace]
    assert matching, f"no producer input routes to declared namespace {namespace!r}"
    build_input = matching[0]
    is_wrapped = isinstance(build_input, SourceAwareBuildInput)
    artifact = b"marked executable"
    receipt_bytes, _ = _publish_local(tmp_path, backend, build_input, artifact)
    key = build_metadata.read_any_receipt(receipt_bytes).cache_key
    entry = home / "builds" / namespace / key.removeprefix("sha256:")
    assert entry.is_dir()
    if is_wrapped:
        marker: Any = _local_v5_marker(
            _package(), legacy, key, receipt_bytes, artifact, build_input.artifact_path
        )
    else:
        marker = _local_v4_marker(legacy, key, receipt_bytes, artifact)
    _write_skill_marker(home, "golden-skill", marker)
    if is_wrapped:
        stale_input: Any = build_metadata.wrap_receipt_v3_input(
            _package(), _local_build_input(command="stale-tool")
        )
    else:
        stale_input = _local_build_input(command="stale-tool")
    assert cache_posix._driver_directory_for_input(stale_input) == namespace
    stale_receipt, _ = _publish_local(tmp_path, backend, stale_input, b"stale-bytes")
    stale_key = build_metadata.read_any_receipt(stale_receipt).cache_key
    assert stale_key != key
    stale_entry = home / "builds" / namespace / stale_key.removeprefix("sha256:")
    _age_entry(entry)
    _age_entry(stale_entry)

    stats = gc.collect_runtime(_gc_config(home, tmp_path), home)

    assert stats.builds_removed == 1
    assert stats.warnings == []
    assert entry.is_dir()
    assert backend.inspect(CacheExpectation(input=build_input)).status is (
        CacheEntryStatus.HIT
    )
    assert not stale_entry.exists()


@POSIX_GC_NAMESPACES
def test_gc_sweep_walks_no_undeclared_local_namespace(tmp_path: Path) -> None:
    """A namespace outside the table is never walked, however old."""

    home, _ = _new_backend(tmp_path)
    rogue = home / "builds" / "go-v1-receipt-v9" / ("9" * 64)
    rogue.mkdir(parents=True)
    (rogue / "csk-receipt.ccj.json").write_bytes(b"{}")
    _age_entry(rogue)

    stats = gc.collect_runtime(_gc_config(home, tmp_path), home)

    assert stats.builds_removed == 0
    assert rogue.exists()


def _external_v2_install(store: DiskProtectedStore) -> Any:
    snapshot = _snapshot()
    return run_pipeline(
        PipelineRequest(
            operation=Operation.INSTALL,
            command="tool",
            target="tool",
            declared=_declared(),
            effective=_effective(),
            acquire=lambda: snapshot,
            audit=lambda subject: None,
            store=store,
            compiler=_Compiler([]),
            package=None,
        )
    )


def _external_v3_install(store: DiskProtectedStore, package: Any) -> Any:
    snapshot = _snapshot()
    return run_pipeline(
        PipelineRequest(
            operation=Operation.INSTALL,
            command="tool",
            target="tool",
            declared=_declared(),
            effective=_effective(),
            acquire=lambda: snapshot,
            audit=lambda subject: None,
            store=store,
            compiler=_Compiler([]),
            package=package,
        )
    )


def test_external_namespace_declaration_matches_store_producers(
    tmp_path: Path,
) -> None:
    """The v2 and v3 installs publish exactly the declared external namespaces."""

    v2_store = DiskProtectedStore(tmp_path / "v2store")
    v3_store = DiskProtectedStore(tmp_path / "v3store")
    _external_v2_install(v2_store)
    _external_v3_install(v3_store, _package())
    produced = {
        child.name
        for store in (v2_store, v3_store)
        for child in store.root.iterdir()
        if child.is_dir() and not child.name.startswith(".")
    }

    assert produced == set(EXTERNAL_ARTIFACT_NAMESPACES) | set(
        EXTERNAL_SNAPSHOT_NAMESPACES
    )


def _external_gc_fixture(tmp_path: Path) -> tuple[Path, DiskProtectedStore, dict[str, Any]]:
    """Publish genuine and stale external entries under a gc-visible store."""

    home = tmp_path / ".cocoaskills"
    home.mkdir()
    provision_manager_home(home)
    store = DiskProtectedStore(home / "external-builds")
    snapshot = _snapshot()
    package = _package()
    genuine_v2 = _external_v2_install(store)
    genuine_v3 = _external_v3_install(store, package)
    assert genuine_v2.receipt is not None and genuine_v2.artifact is not None
    assert genuine_v3.receipt is not None and genuine_v3.artifact is not None
    assert genuine_v2.cache_key != genuine_v3.cache_key
    stale_v3 = _external_v3_install(store, _package(PACKAGE_DIGEST_OTHER))
    assert stale_v3.cache_key != genuine_v3.cache_key
    v2_input = protocol_json.loads_canonical(genuine_v2.receipt)["input"]
    assert isinstance(v2_input, dict)
    stale_v2_key = "sha256:" + "f" * 64
    store.store_artifact(stale_v2_key, v2_input, genuine_v2.artifact)
    stale_snapshot_key = "sha256:" + "e" * 64
    store.store_snapshot(stale_snapshot_key, snapshot)
    genuine_snapshot_key = snapshot_key(_effective(), snapshot.digest)
    assert (store.root / SNAPSHOTS_NAMESPACE / genuine_snapshot_key.removeprefix("sha256:")).is_dir()
    legacy_record = _genuine_legacy_external_record(genuine_v2)
    _write_skill_marker(
        home,
        "legacy-skill",
        install_marker.InstallMarkerV4(
            name="legacy-skill",
            source="github.com/example/tools",
            ref_kind="tag",
            ref="v1",
            commit="a" * 40,
            content_sha256="sha256:" + "6" * 64,
            locale=None,
            agents=(),
            commands=("tool",),
            dependencies=(),
            skill_schema_version=8,
            runtime_roots=(),
            build_roots=(),
            installed_at="2000-01-01T00:00:00Z",
            files=("SKILL.md",),
            builds={"tool": legacy_record},
            build_source=None,
        ),
    )
    _write_skill_marker(
        home,
        "golden-skill",
        install_marker.InstallMarkerV5(
            name="golden-skill",
            package=package,
            lock_sha256="sha256:" + "5" * 64,
            content_sha256="sha256:" + "6" * 64,
            locale=None,
            agents=(),
            commands=("tool",),
            dependencies=(),
            skill_schema_version=8,
            runtime_roots=(),
            build_roots=(),
            installed_at="2000-01-01T00:00:00Z",
            files=("SKILL.md",),
            builds={"tool": _genuine_record(genuine_v3, package)},
            build_source=None,
        ),
    )
    return home, store, {
        "retained": {
            ARTIFACTS_NAMESPACE: genuine_v2.cache_key,
            ARTIFACTS_V3_NAMESPACE: genuine_v3.cache_key,
            SNAPSHOTS_NAMESPACE: genuine_snapshot_key,
        },
        "swept": {
            ARTIFACTS_NAMESPACE: stale_v2_key,
            ARTIFACTS_V3_NAMESPACE: stale_v3.cache_key,
            SNAPSHOTS_NAMESPACE: stale_snapshot_key,
        },
    }


def test_gc_marks_every_declared_external_namespace(tmp_path: Path) -> None:
    """Growth test: a marker-bound entry is retained in every namespace."""

    home, store, keys = _external_gc_fixture(tmp_path)
    declared = set(EXTERNAL_ARTIFACT_NAMESPACES) | set(EXTERNAL_SNAPSHOT_NAMESPACES)
    assert set(keys["retained"]) == declared
    assert set(keys["swept"]) == declared

    stats = gc.collect_runtime(_gc_config(home, tmp_path), home)

    for namespace, key in keys["retained"].items():
        assert (store.root / namespace / key.removeprefix("sha256:")).is_dir(), (
            namespace
        )
    if os.name == "nt":
        # Sealed-DACL recursive removal is a named platform bound; the sweep
        # half of this triangle is verified on POSIX below and in the
        # dedicated sweep test.
        return
    for namespace, key in keys["swept"].items():
        assert not (store.root / namespace / key.removeprefix("sha256:")).exists(), (
            namespace
        )
    assert stats.external_builds_removed == 2
    assert stats.external_snapshots_removed == 1
    assert stats.warnings == []


@EXTERNAL_SWEEP_POSIX
def test_gc_sweeps_unreferenced_external_entries(tmp_path: Path) -> None:
    """Unreferenced external entries are collected in every namespace."""

    home = tmp_path / ".cocoaskills"
    home.mkdir()
    provision_manager_home(home)
    store = DiskProtectedStore(home / "external-builds")
    snapshot = _snapshot()
    genuine_v2 = _external_v2_install(store)
    genuine_v3 = _external_v3_install(store, _package())
    assert genuine_v2.receipt is not None and genuine_v3.receipt is not None

    stats = gc.collect_runtime(_gc_config(home, tmp_path), home)

    assert stats.external_builds_removed == 2
    assert stats.external_snapshots_removed == 1
    assert stats.warnings == []
    assert list((store.root / ARTIFACTS_NAMESPACE).iterdir()) == []
    assert list((store.root / ARTIFACTS_V3_NAMESPACE).iterdir()) == []
    assert list((store.root / SNAPSHOTS_NAMESPACE).iterdir()) == []


def test_gc_sweep_walks_no_undeclared_external_namespace(tmp_path: Path) -> None:
    """An external namespace outside the table is never walked."""

    home = tmp_path / ".cocoaskills"
    home.mkdir()
    provision_manager_home(home)
    rogue = home / "external-builds" / "artifacts-v9" / ("9" * 64)
    rogue.mkdir(parents=True)
    (rogue / "receipt.json").write_bytes(b"{}")

    stats = gc.collect_runtime(_gc_config(home, tmp_path), home)

    assert stats.external_builds_removed == 0
    assert stats.external_snapshots_removed == 0
    assert rogue.exists()
