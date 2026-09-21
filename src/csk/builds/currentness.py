"""Read-only currentness classification for installed compiled commands."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Final

from .. import shims
from ..build_repository import GO_REPOSITORY_V1_DRIVER
from ..install_marker import (
    InstallMarkerBuildV5,
    MarkerBuild,
    MarkerRepositoryCommit,
    MarkerRepositoryIdentity,
    MarkerRepositorySubstitution,
)
from ..skillspec import CommandSpec
from ..sources.package_identity import PackageIdentity
from . import cache, metadata, planner
from .source import BuildSourceIdentity


class BuildCurrentnessError(RuntimeError):
    """Stable failure at the read-only build-currentness boundary."""

    def __init__(self, code: str, detail: str):
        self.code = code
        self.detail = detail
        super().__init__(f"{code}: {detail}")


@dataclass(frozen=True)
class BuildStatus:
    """One stable status row for one active or recorded build command."""

    provider: str
    command: str
    label: str
    detail: str
    expected_cache_key: str | None = None
    recorded_cache_key: str | None = None

    @property
    def current(self) -> bool:
        return self.label == "current"

    def to_json(self) -> dict[str, Any]:
        return {
            "command": self.command,
            "current": self.current,
            "detail": self.detail,
            "execution_policy": metadata.PORTABLE_EXECUTION_POLICY,
            "expected_cache_key": self.expected_cache_key,
            "label": self.label,
            "provider": self.provider,
            "recorded_cache_key": self.recorded_cache_key,
        }


def unavailable_status(
    provider: str,
    command: str,
    *,
    label: str,
    detail: str,
    recorded: MarkerBuild | None = None,
) -> BuildStatus:
    return BuildStatus(
        provider=provider,
        command=command,
        label=label,
        detail=detail,
        recorded_cache_key=(recorded.cache_key if recorded is not None else None),
    )


def classify_build(
    *,
    csk_home: Path,
    bin_dir: Path,
    provider: str,
    command: CommandSpec,
    plan: planner.BuildPlan | None,
    recorded: MarkerBuild | None,
    cache_backend: cache.BuildCacheBackend,
    path_entries: tuple[Path, ...],
    boundary_error: tuple[str, str] | None = None,
    platform_name: str | None = None,
) -> BuildStatus:
    """Classify one command from independently derived and protected state.

    The complete planned cache key and receipt comparison is the currentness
    mechanism for target, toolchain, and policy changes.  In particular, an
    input without ``policy.execution_policy = manager-worker-v1`` derives a
    different key and is reported as ``build-input-drift``; capability
    evidence is intentionally absent from this API and from the key.
    """

    recorded_key = recorded.cache_key if recorded is not None else None
    expected_key = plan.cache_key if plan is not None else None

    def result(label: str, detail: str) -> BuildStatus:
        return BuildStatus(
            provider=provider,
            command=command.name,
            label=label,
            detail=detail,
            expected_cache_key=expected_key,
            recorded_cache_key=recorded_key,
        )

    if boundary_error is not None:
        return result(*boundary_error)
    if plan is None:
        return result(
            "build-command-drift",
            "the recorded build command is not active in the current closure",
        )
    if recorded is None:
        return result(
            "missing-build-marker",
            "marker v2 has no build record for the active command",
        )
    if command.driver != plan.driver or recorded.driver != plan.driver:
        return result(
            "unsupported-build-driver",
            f"build driver differs: descriptor={command.driver!r}, "
            f"marker={recorded.driver!r}, planned={plan.driver!r}",
        )
    if recorded.cache_key != plan.cache_key:
        return result(
            "build-input-drift",
            "the marker cache key does not match the complete current "
            "build input (raw source, toolchain, target, and "
            "policy.execution_policy=manager-worker-v1)",
        )

    inspection = plan.inspection
    if inspection.status is cache.CacheEntryStatus.MISS:
        return result("missing-build-artifact", inspection.reason)
    if inspection.status is cache.CacheEntryStatus.CORRUPT:
        return result("corrupt-build-cache", inspection.reason)
    if inspection.status is cache.CacheEntryStatus.UNTRUSTED_PROVENANCE:
        return result("untrusted-build-cache", inspection.reason)
    if inspection.status is cache.CacheEntryStatus.UNSUPPORTED:
        return result("unsupported-build-platform", inspection.reason)

    try:
        activation = shims.select_build_activation(
            csk_home=csk_home,
            command=command,
            marker_build=recorded,
            inspection=inspection,
            platform_name=platform_name,
        )
    except shims.ShimError as exc:
        return result("build-marker-drift", str(exc))

    shim_error = shims.inspect_bin_shim(
        bin_dir,
        command.name,
        activation.artifact_path,
        platform_name=platform_name,
        path_entries=path_entries,
    )
    if shim_error is not None:
        return result("build-shim-drift", shim_error)

    # Planning and classification are separate observations.  Re-read the
    # complete entry with the marker's receipt hash so a disappearing or
    # replaced cache entry cannot inherit a verdict from stale evidence.
    current = cache_backend.inspect(
        cache.CacheExpectation(
            input=plan.input,
            receipt_sha256=recorded.receipt_sha256,
        )
    )
    if not _same_cache_evidence(inspection, current):
        return result(
            "build-state-changed",
            "protected cache evidence changed during read-only status",
        )
    if current.status is not cache.CacheEntryStatus.HIT:
        return result(_cache_label(current.status), current.reason)
    return result(
        "current",
        "marker, complete input, protected receipt/artifact, and managed shim agree",
    )


def _same_cache_evidence(
    planned: cache.CacheInspection,
    current: cache.CacheInspection,
) -> bool:
    return (
        planned.status == current.status
        and planned.receipt == current.receipt
        and planned.receipt_bytes == current.receipt_bytes
        and planned.receipt_sha256 == current.receipt_sha256
        and planned.artifact_path == current.artifact_path
    )


def _cache_label(status: cache.CacheEntryStatus) -> str:
    if status is cache.CacheEntryStatus.MISS:
        return "missing-build-artifact"
    if status is cache.CacheEntryStatus.CORRUPT:
        return "corrupt-build-cache"
    if status is cache.CacheEntryStatus.UNTRUSTED_PROVENANCE:
        return "untrusted-build-cache"
    if status is cache.CacheEntryStatus.UNSUPPORTED:
        return "unsupported-build-platform"
    return "build-state-changed"


@dataclass(frozen=True)
class _ExternalEvidenceContext:
    """One receipt-3 evidence bundle a marker record is compared against."""

    build: metadata.GoRepositoryBuildInput
    wrapped: metadata.SourceAwareBuildInput
    marker_package: PackageIdentity
    receipt_sha256: str
    artifact_sha256: str


@dataclass(frozen=True)
class _ExternalEvidenceRow:
    """One compared field: the record value against the evidence value."""

    field: str
    record_value: Callable[[InstallMarkerBuildV5, _ExternalEvidenceContext], object]
    expected_value: Callable[[InstallMarkerBuildV5, _ExternalEvidenceContext], object]


def _identity_key(
    identity: MarkerRepositoryIdentity | metadata.RepositorySourceIdentity | None,
) -> tuple[str, str] | None:
    if identity is None:
        return None
    return (identity.kind, identity.value)


def _commit_key(
    commit: MarkerRepositoryCommit | metadata.RepositoryLockedCommit | None,
) -> tuple[str, str] | None:
    if commit is None:
        return None
    return (commit.object_format, commit.hex)


def _substitution_key(
    substitution: MarkerRepositorySubstitution | metadata.RepositorySubstitution | None,
) -> tuple[str, tuple[str, str] | None] | None:
    if substitution is None:
        return None
    ref = substitution.ref
    return (substitution.type, None if ref is None else (ref.kind, ref.value))


def _build_source_key(
    build_source: BuildSourceIdentity | None,
) -> tuple[str, str] | None:
    if build_source is None:
        return None
    return (build_source.algorithm, build_source.content_sha256)


# The one declared field table driving external-evidence comparison, in
# record order. The comparison below derives from it, so the table drives
# rather than pins: a new compared field extends the comparison by
# construction.
_EXTERNAL_EVIDENCE_ROWS: Final[tuple[_ExternalEvidenceRow, ...]] = (
    _ExternalEvidenceRow(
        "repository",
        lambda record, ctx: record.repository,
        lambda record, ctx: ctx.build.source.repository,
    ),
    _ExternalEvidenceRow(
        "declared_identity",
        lambda record, ctx: _identity_key(record.declared_identity),
        lambda record, ctx: _identity_key(ctx.build.source.declared.identity),
    ),
    _ExternalEvidenceRow(
        "declared_locked_commit",
        lambda record, ctx: _commit_key(record.declared_locked_commit),
        lambda record, ctx: _commit_key(ctx.build.source.declared.locked_commit),
    ),
    _ExternalEvidenceRow(
        "declared_tag",
        lambda record, ctx: record.declared_tag,
        lambda record, ctx: ctx.build.source.declared.tag,
    ),
    _ExternalEvidenceRow(
        "effective_identity",
        lambda record, ctx: _identity_key(record.effective_identity),
        lambda record, ctx: _identity_key(ctx.build.source.effective.identity),
    ),
    _ExternalEvidenceRow(
        "object_format",
        lambda record, ctx: record.object_format,
        lambda record, ctx: ctx.build.source.effective.object_format,
    ),
    _ExternalEvidenceRow(
        "commit",
        lambda record, ctx: record.commit,
        lambda record, ctx: ctx.build.source.effective.commit,
    ),
    _ExternalEvidenceRow(
        "substituted",
        lambda record, ctx: record.substituted,
        lambda record, ctx: ctx.build.source.effective.substituted,
    ),
    _ExternalEvidenceRow(
        "substitution",
        lambda record, ctx: _substitution_key(record.substitution),
        lambda record, ctx: _substitution_key(ctx.build.source.effective.substitution),
    ),
    _ExternalEvidenceRow(
        "build_source",
        lambda record, ctx: _build_source_key(record.build_source),
        lambda record, ctx: _build_source_key(ctx.build.source.effective.build_source),
    ),
    _ExternalEvidenceRow(
        "descriptor_target",
        lambda record, ctx: record.descriptor_target,
        lambda record, ctx: ctx.build.source.descriptor.target,
    ),
    _ExternalEvidenceRow(
        "execution_policy",
        lambda record, ctx: record.execution_policy,
        lambda record, ctx: ctx.build.policy.execution_policy,
    ),
    _ExternalEvidenceRow(
        "cache_key",
        lambda record, ctx: record.cache_key,
        lambda record, ctx: metadata.source_aware_cache_key(ctx.wrapped),
    ),
    _ExternalEvidenceRow(
        "receipt_sha256",
        lambda record, ctx: record.receipt_sha256,
        lambda record, ctx: ctx.receipt_sha256,
    ),
    _ExternalEvidenceRow(
        "artifact_sha256",
        lambda record, ctx: record.artifact_sha256,
        lambda record, ctx: ctx.artifact_sha256,
    ),
    _ExternalEvidenceRow(
        "artifact_path",
        lambda record, ctx: record.artifact_path,
        lambda record, ctx: ctx.build.artifact_path,
    ),
    _ExternalEvidenceRow(
        "input.package",
        lambda record, ctx: ctx.marker_package,
        lambda record, ctx: ctx.wrapped.package,
    ),
)

EXTERNAL_EVIDENCE_FIELDS: Final[tuple[str, ...]] = tuple(
    row.field for row in _EXTERNAL_EVIDENCE_ROWS
)


def compare_external_build_evidence(
    record: InstallMarkerBuildV5,
    receipt_input: metadata.SourceAwareBuildInput,
    *,
    marker_package: PackageIdentity,
    receipt_bytes: bytes,
    artifact_bytes: bytes,
) -> tuple[str, ...]:
    """Compare one external build record against receipt-3 evidence field by field.

    The record is compared with receipt-3 ``input.build`` under the external
    receipt-input rules, the receipt and artifact hashes with the protected
    bytes, and receipt-3 ``input.package`` with the marker package. Returns the
    compared fields that differ, in table order; an empty tuple means the
    record agrees with the evidence on every compared field.
    """

    if record.driver != GO_REPOSITORY_V1_DRIVER or not isinstance(
        receipt_input.build, metadata.GoRepositoryBuildInput
    ):
        raise BuildCurrentnessError(
            "external_evidence_invalid",
            "external-evidence comparison requires a go-repository-v1 record "
            "and a go-repository-v1 receipt-3 input",
        )
    context = _ExternalEvidenceContext(
        build=receipt_input.build,
        wrapped=receipt_input,
        marker_package=marker_package,
        receipt_sha256=metadata.receipt_sha256(receipt_bytes),
        artifact_sha256=metadata.sha256_identity(artifact_bytes),
    )
    return tuple(
        row.field
        for row in _EXTERNAL_EVIDENCE_ROWS
        if row.record_value(record, context) != row.expected_value(record, context)
    )
