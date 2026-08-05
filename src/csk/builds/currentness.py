"""Read-only currentness classification for installed compiled commands."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .. import shims
from ..install_marker import MarkerBuild
from ..skillspec import CommandSpec
from . import cache, metadata, planner


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
