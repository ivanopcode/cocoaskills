"""Closed source-aware ``go-v1`` compiler boundary.

The package-independent Go selection and fingerprinting probes live in
``csk.builds.toolchain``.  This module consumes that frozen session and runs
the only two source-aware commands through one identity-verified hidden-mode
re-execution of the installed manager.

The worker's whole startup trusted base is closed before any worker code runs.
The manager binds the launcher, the interpreter and its link chain, the actual
native process and Python runtime images, the complete installed package tree,
and every mutable Python startup component beside them.  Windows virtual
environments are resolved through a bound ``pyvenv.cfg`` to their physical base
interpreter, DLL, and standard library.  The worker then launches with the one
fixed site-disabled argument vector so that no ``.pth`` hook,
``sitecustomize``, ``usercustomize``, per-user site directory, or
launcher-directory import can execute at all.  It proves its native images,
isolation flags, import path, and complete loaded-module set back to the
manager, and any component inserted or mutated across the launch boundary is a
detected worker-identity change.
"""

from __future__ import annotations

import base64
import binascii
import ctypes
import errno
import hashlib
import hmac
import json
import os
import queue
import secrets
import shutil
import signal
import stat
import struct
import subprocess
import sys
import tempfile
import threading
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from pathlib import Path
from types import MappingProxyType
from typing import Any, BinaryIO, Final, Protocol, cast

from ..identifiers import is_valid_identifier, is_valid_portable_path
from . import source, toolchain


# Stable execution-boundary diagnostics from Protocol Core 4.2.1.
CODE_WORKER_IDENTITY_INVALID: Final = "build_execution_worker_identity_invalid"
CODE_WORKER_PROTOCOL_INVALID: Final = "build_execution_worker_protocol_invalid"
CODE_CONTROL_UNAVAILABLE: Final = "build_execution_control_unavailable"
CODE_CAPABILITY_EVIDENCE_INVALID: Final = (
    "build_execution_capability_evidence_invalid"
)
CODE_HARDENED_CLAIM_FORBIDDEN: Final = (
    "build_execution_hardened_claim_forbidden"
)
CODE_PACKAGE_INFLUENCE_FORBIDDEN: Final = (
    "build_execution_package_influence_forbidden"
)

EXECUTION_POLICY: Final = "manager-worker-v1"
NATIVE_CONTROL_INVENTORY_VERSION: Final = "rc5-native-control-inventory-v1"
CAPABILITY_EVIDENCE_VERSION: Final = "capability-evidence-v1"
PROBE_TIMING: Final = "pre-worker-launch"

PLATFORM_MACOS: Final = "macos"
PLATFORM_WINDOWS: Final = "windows"
AVAILABILITY_AVAILABLE: Final = "available"
AVAILABILITY_UNAVAILABLE: Final = "unavailable"
STATUS_APPLIED: Final = "applied"
STATUS_UNAVAILABLE: Final = "unavailable"
UNAVAILABLE_REASON_NO_PRIVATE_AGGREGATE_DOMAIN: Final = (
    "no-private-aggregate-domain"
)

CONTROL_DESCENDANT_DOMAIN_TERMINATION: Final = "descendant-domain-termination"
CONTROL_ACTIVE_PROCESS_COUNT_LIMIT: Final = "active-process-count-limit"
CONTROL_AGGREGATE_MEMORY_LIMIT: Final = "aggregate-memory-limit"
CONTROL_PER_FILE_SIZE_LIMIT: Final = "per-file-size-limit"
CONTROL_INHERITED_HANDLE_RESTRICTION: Final = "inherited-handle-restriction"

NATIVE_CONTROL_INVENTORY: Final[tuple[str, ...]] = (
    CONTROL_DESCENDANT_DOMAIN_TERMINATION,
    CONTROL_ACTIVE_PROCESS_COUNT_LIMIT,
    CONTROL_AGGREGATE_MEMORY_LIMIT,
    CONTROL_PER_FILE_SIZE_LIMIT,
    CONTROL_INHERITED_HANDLE_RESTRICTION,
)

MANDATORY_CONTROLS: Final[tuple[str, ...]] = (
    "fixed-offline-vendored-go",
    "fixed-argument-vectors",
    "fixed-empty-environment",
    "fixed-manager-selected-process-graph",
    "identity-verified-manager-owned-worker",
    "pre-launch-worker-identity-verification",
    "post-exec-identity-reverification",
    "frozen-source-snapshot-integrity",
    "manager-private-staging-roots",
    "manager-derived-output-path",
    "bounded-wall-clock-deadline",
    "bounded-combined-output",
    "bounded-artifact-size",
    "closed-standard-input-and-descriptors",
    "worker-domain-teardown",
    "no-artifact-execution",
    "inventory-native-controls-applied",
    "closed-capability-evidence-record",
)

DEFERRED_HARDENED_GUARANTEES: Final[tuple[str, ...]] = (
    "total-network-denial",
    "read-only-source-and-toolchain",
    "private-build-root-only-writes",
    "hard-aggregate-descendant-resource-bounds",
    "exact-executable-allowlisting",
    "fail-closed-capability-preflight",
)

PROCESS_GRAPH: Final[tuple[str, ...]] = (
    "manager-parent",
    "identity-verified-manager-owned-worker",
    "fingerprinted-goroot-bin-go",
    "fingerprinted-goroot-pkg-tool-child",
)

SESSION_STATES: Final[tuple[str, ...]] = (
    "parent-package-independent-toolchain-probe",
    "parent-native-control-availability-probe",
    "parent-worker-identity-verification",
    "worker-launch",
    "worker-identity-proof-and-nonce-acknowledgement",
    "worker-control-application-and-evidence",
    "worker-fixed-go-list",
    "parent-complete-package-graph-validation",
    "parent-authenticated-build-permit",
    "worker-fixed-go-build",
    "parent-artifact-verification",
    "parent-post-exec-identity-reverification",
    "worker-domain-teardown",
)

LIST_ARGUMENTS: Final[tuple[str, ...]] = (
    "list",
    "-mod=vendor",
    "-deps",
    "-json",
    "-buildvcs=false",
    "-compiler=gc",
    "-pgo=off",
    ".",
)
BUILD_ARGUMENT_PREFIX: Final[tuple[str, ...]] = (
    "build",
    "-mod=vendor",
    "-trimpath",
    "-buildvcs=false",
    "-buildmode=exe",
    "-compiler=gc",
    "-pgo=off",
    "-ldflags=-linkmode=internal -libgcc=none",
    "-o",
)

WORKER_MODE: Final = "__csk-go-worker-v1"

# The one fixed interpreter argument vector for the hidden worker.  ``-S`` and
# ``-s`` keep every mutable ``site`` startup component -- ``.pth`` hooks,
# ``sitecustomize``, ``usercustomize``, and the per-user site directory -- from
# executing at all, ``-P`` keeps the launcher directory off ``sys.path``, and
# ``-B`` keeps the worker from writing bytecode.  The worker proves all four
# flags before the manager accepts it.
WORKER_LAUNCH_FLAGS: Final[tuple[str, ...]] = ("-S", "-s", "-B", "-P")

_PROTOCOL_VERSION: Final = "csk-go-worker-v1"
_MAX_PROTOCOL_FRAME: Final = 64 * 1024 * 1024
_SESSION_TOKEN_LENGTH: Final = 64
_WORKER_LAUNCH_SECRET_BYTES: Final = 32
_WORKER_LAUNCH_FDS: Final[tuple[int, ...]] = (
    4095,
    8191,
    16_383,
    32_767,
    65_535,
)
_WORKER_LAUNCH_MAGIC: Final = b"csk-go-launch-v1"
_WORKER_LAUNCH_RECORD_DOMAIN: Final = b"csk-go-launch-record-v1\x00"
_WORKER_LAUNCH_REQUEST_DOMAIN: Final = b"csk-go-launch-request-v1\x00"
_WORKER_LAUNCH_READY_DOMAIN: Final = b"csk-go-launch-ready-v1\x00"
_WORKER_LAUNCH_TRANSPORT: Final = "inherited-anonymous-pipe"
_MAX_WINDOWS_HANDLE_SCAN: Final = 1 << 16
_MAX_MANAGER_EXECUTABLE_BYTES: Final = 512 * 1024 * 1024
_MAX_MANAGER_PACKAGE_FILES: Final = 10_000
_MAX_MANAGER_PACKAGE_BYTES: Final = 512 * 1024 * 1024
_MAX_TOOL_EXECUTABLES: Final = 1_024
_MAX_TOOL_TREE_BYTES: Final = 4 * 1024 * 1024 * 1024
_MAX_STARTUP_HOOKS: Final = 512
_MAX_STARTUP_HOOK_BYTES: Final = 4 * 1024 * 1024
_MAX_INTERPRETER_CONFIGURATION_BYTES: Final = 64 * 1024
_MAX_RUNTIME_TREES: Final = 4
_MAX_RUNTIME_FILES: Final = 20_000
_MAX_RUNTIME_BYTES: Final = 1024 * 1024 * 1024
_MAX_RUNTIME_ARCHIVES: Final = 16
_MAX_RUNTIME_ARCHIVE_BYTES: Final = 512 * 1024 * 1024
_MAX_WORKER_RUNTIME_MODULES: Final = 4_096
_RUNTIME_FILE_SUFFIXES: Final[tuple[str, ...]] = (
    ".py",
    ".pyc",
    ".so",
    ".pyd",
    ".dll",
)
_RUNTIME_IGNORED_NAMES: Final[tuple[str, ...]] = (
    "site-packages",
    "dist-packages",
)
_STARTUP_HOOK_MODULES: Final[tuple[str, ...]] = (
    "sitecustomize.py",
    "usercustomize.py",
)
_STARTUP_PREFIX_CONFIGURATION: Final[tuple[str, ...]] = (
    "pyvenv.cfg",
    "python._pth",
    "pybuilddir.txt",
)
_WORKER_STDERR_LIMIT: Final = 64 * 1024
_WORKER_SHUTDOWN_GRACE: Final = 5.0
_PERMIT_DOMAIN: Final = b"csk-go-build-permit\x00"
_TREE_IDENTITY_DOMAIN: Final = b"csk-go-execution-tree-v1\x00"
_STARTUP_IDENTITY_DOMAIN: Final = b"csk-go-worker-startup-v1\x00"
_WORKER_LAUNCH_LOCK = threading.Lock()
_MACOS_IDENTITY_GUARD_LOCK = threading.Lock()
_MACOS_IDENTITY_FD_HEADROOM: Final = 256
_MACOS_FIXED_DESCRIPTOR_CAPACITY: Final = _WORKER_LAUNCH_FDS[-1] + 1
_MAX_RETAINED_IDENTITY_PATHS: Final = 50_000

_MESSAGE_KINDS: Final[frozenset[str]] = frozenset(
    {
        "request",
        "ready",
        "list",
        "list-result",
        "permit",
        "build-result",
        "shutdown",
        "failure",
    }
)


class GoV1Error(RuntimeError):
    """Stable, machine-testable failure at the source-aware Go boundary."""

    def __init__(self, code: str, detail: str):
        self.code = code
        self.detail = detail
        super().__init__(f"go-v1 {code}: {detail}")


@dataclass(frozen=True)
class ResourceLimits:
    """Manager-owned portable bounds for one worker operation."""

    timeout_seconds: float = 120.0
    output_bytes: int = 8 * 1024 * 1024
    artifact_bytes: int = 128 * 1024 * 1024
    file_bytes: int = 512 * 1024 * 1024
    disk_bytes: int = 1024 * 1024 * 1024
    memory_bytes: int = 2 * 1024 * 1024 * 1024
    processes: int = 64


@dataclass(frozen=True)
class ArtifactMetadata:
    path: str
    sha256: str
    size: int


@dataclass(frozen=True)
class BuildArtifact:
    """Verified private output; this module never executes it."""

    staged_path: Path
    metadata: ArtifactMetadata


@dataclass(frozen=True)
class CapabilityEvidenceEntry:
    name: str
    availability: str
    status: str
    probed_at: str

    def to_dict(self) -> dict[str, str]:
        return {
            "name": self.name,
            "availability": self.availability,
            "status": self.status,
            "probed_at": self.probed_at,
        }


@dataclass(frozen=True)
class CapabilityEvidence:
    """The single result-only ``capability-evidence-v1`` record."""

    record_version: str
    execution_policy: str
    platform: str
    controls: tuple[CapabilityEvidenceEntry, ...]

    def to_dict(self) -> dict[str, object]:
        return {
            "record_version": self.record_version,
            "execution_policy": self.execution_policy,
            "platform": self.platform,
            "controls": [entry.to_dict() for entry in self.controls],
        }


@dataclass(frozen=True)
class BuildResult:
    artifact: BuildArtifact
    capability_evidence: CapabilityEvidence


@dataclass(frozen=True)
class BuildRequest:
    """Manager-owned inputs plus the exact package command surface."""

    toolchain_session: toolchain.ToolchainSession
    source_snapshot: source.FrozenSnapshot
    command_object: Mapping[str, object]
    build_root: str
    source_dir: str
    command: str
    limits: ResourceLimits = field(default_factory=ResourceLimits)


@dataclass(frozen=True)
class ControlProbe:
    name: str
    availability: str
    mechanism: str
    probed_at: str = PROBE_TIMING

    def to_dict(self) -> dict[str, str]:
        return {
            "name": self.name,
            "availability": self.availability,
            "mechanism": self.mechanism,
            "probed_at": self.probed_at,
        }


@dataclass(frozen=True)
class _InventoryRecord:
    availability: str
    mechanism: str = ""
    unavailable_reason: str = ""


_NATIVE_CONTROL_PLATFORMS: Final[
    Mapping[str, Mapping[str, _InventoryRecord]]
] = MappingProxyType(
    {
        PLATFORM_MACOS: MappingProxyType(
            {
                CONTROL_DESCENDANT_DOMAIN_TERMINATION: _InventoryRecord(
                    AVAILABILITY_AVAILABLE,
                    "process-group-and-session-teardown",
                ),
                CONTROL_ACTIVE_PROCESS_COUNT_LIMIT: _InventoryRecord(
                    AVAILABILITY_UNAVAILABLE,
                    unavailable_reason=UNAVAILABLE_REASON_NO_PRIVATE_AGGREGATE_DOMAIN,
                ),
                CONTROL_AGGREGATE_MEMORY_LIMIT: _InventoryRecord(
                    AVAILABILITY_UNAVAILABLE,
                    unavailable_reason=UNAVAILABLE_REASON_NO_PRIVATE_AGGREGATE_DOMAIN,
                ),
                CONTROL_PER_FILE_SIZE_LIMIT: _InventoryRecord(
                    AVAILABILITY_AVAILABLE,
                    "rlimit-fsize",
                ),
                CONTROL_INHERITED_HANDLE_RESTRICTION: _InventoryRecord(
                    AVAILABILITY_AVAILABLE,
                    "close-on-exec-and-explicit-descriptor-release",
                ),
            }
        ),
        PLATFORM_WINDOWS: MappingProxyType(
            {
                CONTROL_DESCENDANT_DOMAIN_TERMINATION: _InventoryRecord(
                    AVAILABILITY_AVAILABLE,
                    "job-object-kill-on-close",
                ),
                CONTROL_ACTIVE_PROCESS_COUNT_LIMIT: _InventoryRecord(
                    AVAILABILITY_AVAILABLE,
                    "job-object-active-process-limit",
                ),
                CONTROL_AGGREGATE_MEMORY_LIMIT: _InventoryRecord(
                    AVAILABILITY_AVAILABLE,
                    "job-object-process-and-job-memory-limit",
                ),
                CONTROL_PER_FILE_SIZE_LIMIT: _InventoryRecord(
                    AVAILABILITY_UNAVAILABLE,
                    unavailable_reason=UNAVAILABLE_REASON_NO_PRIVATE_AGGREGATE_DOMAIN,
                ),
                CONTROL_INHERITED_HANDLE_RESTRICTION: _InventoryRecord(
                    AVAILABILITY_AVAILABLE,
                    "explicit-handle-inheritance-list",
                ),
            }
        ),
    }
)


def inventory_platform() -> str:
    """Return the exact inventory platform, or fail closed for every other host."""

    if sys.platform == "darwin":
        return PLATFORM_MACOS
    if os.name == "nt":
        return PLATFORM_WINDOWS
    raise GoV1Error(
        CODE_CONTROL_UNAVAILABLE,
        "rc5-native-control-inventory-v1 covers exactly macOS and Windows",
    )


def evidence_from_applied(
    platform: str,
    probes: Sequence[ControlProbe],
    applied: Sequence[str],
) -> CapabilityEvidence:
    applied_set = frozenset(applied)
    return CapabilityEvidence(
        record_version=CAPABILITY_EVIDENCE_VERSION,
        execution_policy=EXECUTION_POLICY,
        platform=platform,
        controls=tuple(
            CapabilityEvidenceEntry(
                name=probe.name,
                availability=probe.availability,
                status=(
                    STATUS_APPLIED
                    if (
                        probe.availability == AVAILABILITY_AVAILABLE
                        and probe.name in applied_set
                    )
                    else STATUS_UNAVAILABLE
                ),
                probed_at=probe.probed_at,
            )
            for probe in probes
        ),
    )


def validate_capability_evidence(
    record: CapabilityEvidence,
    platform: str,
    probes: Sequence[ControlProbe],
) -> None:
    """Enforce the vector's eight closed consistency rules."""

    if record.execution_policy != EXECUTION_POLICY:
        raise GoV1Error(
            CODE_HARDENED_CLAIM_FORBIDDEN,
            "capability evidence does not carry the portable execution policy",
        )
    if any(
        entry.name in DEFERRED_HARDENED_GUARANTEES
        for entry in record.controls
    ):
        raise GoV1Error(
            CODE_HARDENED_CLAIM_FORBIDDEN,
            "capability evidence claims a deferred hardened guarantee",
        )
    if record.record_version != CAPABILITY_EVIDENCE_VERSION:
        raise GoV1Error(
            CODE_CAPABILITY_EVIDENCE_INVALID,
            f"unknown capability evidence version {record.record_version!r}",
        )
    if record.platform != platform or platform not in _NATIVE_CONTROL_PLATFORMS:
        raise GoV1Error(
            CODE_CAPABILITY_EVIDENCE_INVALID,
            "capability evidence platform differs from the probed platform",
        )

    probed: dict[str, str] = {}
    for probe in probes:
        if probe.name in probed:
            raise GoV1Error(
                CODE_CAPABILITY_EVIDENCE_INVALID,
                f"native control {probe.name!r} was probed more than once",
            )
        probed[probe.name] = probe.availability

    seen: set[str] = set()
    for entry in record.controls:
        if entry.name not in NATIVE_CONTROL_INVENTORY:
            raise GoV1Error(
                CODE_CAPABILITY_EVIDENCE_INVALID,
                f"control {entry.name!r} is outside the native inventory",
            )
        if entry.name in seen:
            raise GoV1Error(
                CODE_CAPABILITY_EVIDENCE_INVALID,
                f"capability evidence duplicates {entry.name!r}",
            )
        seen.add(entry.name)
        if entry.probed_at != PROBE_TIMING:
            raise GoV1Error(
                CODE_CAPABILITY_EVIDENCE_INVALID,
                f"control {entry.name!r} has an unknown probe timing",
            )
        if probed.get(entry.name) != entry.availability:
            raise GoV1Error(
                CODE_CAPABILITY_EVIDENCE_INVALID,
                f"control {entry.name!r} reports unprobed availability",
            )
        if (
            entry.availability == AVAILABILITY_AVAILABLE
            and entry.status != STATUS_APPLIED
        ):
            raise GoV1Error(
                CODE_CAPABILITY_EVIDENCE_INVALID,
                f"available control {entry.name!r} is not reported applied",
            )
        if (
            entry.availability == AVAILABILITY_UNAVAILABLE
            and entry.status != STATUS_UNAVAILABLE
        ):
            raise GoV1Error(
                CODE_CAPABILITY_EVIDENCE_INVALID,
                f"unavailable control {entry.name!r} is reported applied",
            )
        if entry.availability not in {
            AVAILABILITY_AVAILABLE,
            AVAILABILITY_UNAVAILABLE,
        }:
            raise GoV1Error(
                CODE_CAPABILITY_EVIDENCE_INVALID,
                f"control {entry.name!r} has unknown availability",
            )
    if seen != set(NATIVE_CONTROL_INVENTORY):
        raise GoV1Error(
            CODE_CAPABILITY_EVIDENCE_INVALID,
            "capability evidence must contain exactly one entry per native control",
        )


def capability_evidence_from_mapping(
    value: Mapping[str, object],
) -> CapabilityEvidence:
    try:
        return _capability_evidence_from_mapping(value)
    except GoV1Error as exc:
        if exc.code != CODE_WORKER_PROTOCOL_INVALID:
            raise
        raise GoV1Error(
            CODE_CAPABILITY_EVIDENCE_INVALID,
            "capability evidence does not have the closed record shape",
        ) from exc


def _capability_evidence_from_mapping(
    value: Mapping[str, object],
) -> CapabilityEvidence:
    _require_exact_keys(
        value,
        {"record_version", "execution_policy", "platform", "controls"},
        "capability evidence",
    )
    raw_controls = _require_list(value.get("controls"), "capability evidence controls")
    controls: list[CapabilityEvidenceEntry] = []
    for raw in raw_controls:
        item = _require_mapping(raw, "capability evidence control")
        _require_exact_keys(
            item,
            {"name", "availability", "status", "probed_at"},
            "capability evidence control",
        )
        controls.append(
            CapabilityEvidenceEntry(
                name=_require_string(item.get("name"), "control name"),
                availability=_require_string(
                    item.get("availability"),
                    "control availability",
                ),
                status=_require_string(item.get("status"), "control status"),
                probed_at=_require_string(
                    item.get("probed_at"),
                    "control probe timing",
                ),
            )
        )
    return CapabilityEvidence(
        record_version=_require_string(
            value.get("record_version"),
            "evidence record version",
        ),
        execution_policy=_require_string(
            value.get("execution_policy"),
            "evidence execution policy",
        ),
        platform=_require_string(value.get("platform"), "evidence platform"),
        controls=tuple(controls),
    )


def validate_package_graph(
    payload: bytes,
    *,
    build_root: Path,
    source_dir: Path,
    goroot: Path,
) -> None:
    """Parse the complete ``go list`` stream and constrain every active input."""

    try:
        _validate_package_graph(
            payload,
            build_root=build_root,
            source_dir=source_dir,
            goroot=goroot,
        )
    except GoV1Error as exc:
        if exc.code != CODE_WORKER_PROTOCOL_INVALID:
            raise
        raise GoV1Error(
            "go_list_malformed",
            "go list returned a value with an invalid field type",
        ) from exc


def _validate_package_graph(
    payload: bytes,
    *,
    build_root: Path,
    source_dir: Path,
    goroot: Path,
) -> None:
    packages = _decode_json_stream(payload)
    if not packages:
        raise GoV1Error("go_list_incomplete", "go list returned no packages")

    seen: set[str] = set()
    roots: list[Mapping[str, object]] = []
    for item in packages:
        import_path = _optional_string(item.get("ImportPath"))
        if not import_path or import_path in seen:
            raise GoV1Error(
                "go_list_incomplete",
                "go list returned an empty or duplicate import path",
            )
        seen.add(import_path)
        if not _optional_bool(item.get("DepOnly")):
            roots.append(item)

    if not roots:
        raise GoV1Error(
            "build_package_not_main",
            "go list returned no root package",
        )
    if len(roots) != 1:
        raise GoV1Error(
            "build_package_ambiguous",
            "go list returned more than one non-DepOnly package",
        )
    root = roots[0]
    if (
        _optional_string(root.get("Name")) != "main"
        or _optional_string(root.get("Dir")) != os.fspath(source_dir)
    ):
        raise GoV1Error(
            "build_package_not_main",
            "the selected root is not the canonical main package",
        )

    has_vendored_module = False
    for item in packages:
        module = _optional_mapping(item.get("Module"))
        if module is not None and not _optional_bool(module.get("Main")):
            has_vendored_module = True
        if (
            _optional_bool(item.get("Incomplete"))
            or item.get("Error") is not None
            or bool(_optional_list(item.get("DepsErrors")))
        ):
            raise GoV1Error(
                "go_list_incomplete",
                f"package {_optional_string(item.get('ImportPath'))!r} "
                "is incomplete or carries load errors",
            )
        if _optional_string(item.get("ForTest")):
            raise GoV1Error(
                "go_test_input_forbidden",
                "go list selected a test package",
            )
        _validate_package_inputs(
            item,
            build_root=build_root,
            goroot=goroot,
        )

    if has_vendored_module:
        modules = build_root / "vendor" / "modules.txt"
        try:
            _validate_regular_absolute(
                modules,
                build_root,
                allow_toolchain_links=False,
            )
        except (OSError, ValueError) as exc:
            raise GoV1Error(
                "vendor_metadata_inconsistent",
                "vendored graph lacks a regular in-root vendor/modules.txt",
            ) from exc


def _decode_json_stream(payload: bytes) -> list[Mapping[str, object]]:
    if not payload:
        raise GoV1Error(
            "go_list_incomplete",
            "go list returned an empty package stream",
        )
    try:
        text = payload.decode("utf-8", errors="strict")
    except UnicodeDecodeError as exc:
        raise GoV1Error(
            "go_list_malformed",
            "go list output is not valid UTF-8",
        ) from exc

    decoder = json.JSONDecoder(object_pairs_hook=_closed_object)
    offset = 0
    result: list[Mapping[str, object]] = []
    while True:
        while offset < len(text) and text[offset].isspace():
            offset += 1
        if offset == len(text):
            break
        try:
            value, offset = decoder.raw_decode(text, offset)
        except (json.JSONDecodeError, ValueError) as exc:
            raise GoV1Error(
                "go_list_malformed",
                "go list returned an invalid or incomplete JSON stream",
            ) from exc
        result.append(_require_mapping(value, "go list package"))
    return result


def _validate_package_inputs(
    item: Mapping[str, object],
    *,
    build_root: Path,
    goroot: Path,
) -> None:
    import_path = _optional_string(item.get("ImportPath"))
    trusted_standard = (
        _optional_bool(item.get("Standard"))
        and _optional_bool(item.get("Goroot"))
    )
    trusted_root = goroot if trusted_standard else build_root
    module = _optional_mapping(item.get("Module"))

    if trusted_standard:
        if module is not None:
            raise GoV1Error(
                "go_standard_input_escape",
                f"standard package {import_path!r} carries module metadata",
            )
        if _optional_string(item.get("Root")) != os.fspath(goroot):
            raise GoV1Error(
                "go_standard_input_escape",
                f"standard package {import_path!r} has an unexpected Root",
            )
    else:
        _validate_module(item, module, build_root)
        item_root = _optional_string(item.get("Root"))
        if item_root:
            try:
                _validate_directory(
                    Path(item_root),
                    build_root,
                    allow_toolchain_links=False,
                )
            except (OSError, ValueError) as exc:
                raise GoV1Error(
                    "go_source_input_escape",
                    f"package {import_path!r} Root escapes the build root",
                ) from exc

    package_dir = Path(_optional_string(item.get("Dir")))
    try:
        _validate_directory(
            package_dir,
            trusted_root,
            allow_toolchain_links=trusted_standard,
        )
    except (OSError, ValueError) as exc:
        code = (
            "go_standard_input_escape"
            if trusted_standard
            else "go_source_input_escape"
        )
        raise GoV1Error(
            code,
            f"package {import_path!r} has an invalid directory",
        ) from exc

    if _string_list(item.get("SysoFiles"), "SysoFiles"):
        raise GoV1Error(
            "go_syso_forbidden",
            f"package {import_path!r} contains SysoFiles",
        )

    if not trusted_standard:
        native_fields = (
            "CgoFiles",
            "CFiles",
            "CXXFiles",
            "MFiles",
            "HFiles",
            "FFiles",
            "SwigFiles",
            "SwigCXXFiles",
        )
        for field_name in native_fields:
            if not _string_list(item.get(field_name), field_name):
                continue
            if field_name == "CgoFiles":
                raise GoV1Error(
                    "cgo_required",
                    f"package {import_path!r} contains active cgo input",
                )
            raise GoV1Error(
                "go_native_input_forbidden",
                f"package {import_path!r} contains {field_name}",
            )
        if _string_list(item.get("SFiles"), "SFiles"):
            raise GoV1Error(
                "go_assembly_forbidden",
                f"package {import_path!r} contains non-standard assembly",
            )

    active_fields = ["GoFiles", "CompiledGoFiles"]
    if trusted_standard:
        active_fields.extend(
            [
                "CgoFiles",
                "CFiles",
                "CXXFiles",
                "MFiles",
                "HFiles",
                "FFiles",
                "SFiles",
                "SwigFiles",
                "SwigCXXFiles",
            ]
        )
    go_files = frozenset(_string_list(item.get("GoFiles"), "GoFiles"))
    active: list[str] = []
    for field_name in active_fields:
        active.extend(_string_list(item.get(field_name), field_name))
    for name in dict.fromkeys(active):
        try:
            path = _validate_regular_input(
                package_dir,
                name,
                trusted_root,
                allow_toolchain_links=trusted_standard,
            )
        except (OSError, ValueError) as exc:
            code = (
                "go_standard_input_escape"
                if trusted_standard
                else "go_source_input_escape"
            )
            raise GoV1Error(
                code,
                f"package {import_path!r} has an invalid source input",
            ) from exc
        if not trusted_standard and name in go_files:
            _scan_source_directives(path, import_path)

    for name in _string_list(item.get("EmbedFiles"), "EmbedFiles"):
        try:
            _validate_regular_input(
                package_dir,
                name,
                trusted_root,
                allow_toolchain_links=trusted_standard,
            )
        except (OSError, ValueError) as exc:
            code = (
                "go_standard_input_escape"
                if trusted_standard
                else "go_embed_input_escape"
            )
            raise GoV1Error(
                code,
                f"package {import_path!r} has an escaped embed input",
            ) from exc

    if not trusted_standard:
        pgo = package_dir / "default.pgo"
        try:
            pgo_stat = pgo.lstat()
        except FileNotFoundError:
            pass
        except OSError as exc:
            raise GoV1Error(
                "go_pgo_forbidden",
                f"cannot safely inspect default.pgo for {import_path!r}",
            ) from exc
        else:
            if stat.S_ISREG(pgo_stat.st_mode):
                raise GoV1Error(
                    "go_pgo_forbidden",
                    f"package {import_path!r} contains default.pgo",
                )


def _validate_module(
    item: Mapping[str, object],
    module: Mapping[str, object] | None,
    build_root: Path,
) -> None:
    import_path = _optional_string(item.get("ImportPath"))
    if (
        module is None
        or not _optional_string(module.get("Path"))
        or module.get("Error") is not None
        or module.get("Replace") is not None
    ):
        raise GoV1Error(
            "vendor_metadata_inconsistent",
            f"non-standard package {import_path!r} has invalid module metadata",
        )
    module_dir_value = _optional_string(module.get("Dir"))
    go_mod_value = _optional_string(module.get("GoMod"))
    module_dir = Path(module_dir_value)
    go_mod = Path(go_mod_value)
    if _optional_bool(module.get("Main")):
        if module_dir != build_root or go_mod != build_root / "go.mod":
            raise GoV1Error(
                "nested_build_module",
                f"package {import_path!r} resolves through a nested main module",
            )
        try:
            _validate_regular_absolute(
                go_mod,
                build_root,
                allow_toolchain_links=False,
            )
        except (OSError, ValueError) as exc:
            raise GoV1Error(
                "build_module_missing",
                "main module go.mod is invalid",
            ) from exc
        return

    vendor_root = build_root / "vendor"
    item_dir = Path(_optional_string(item.get("Dir")))
    if (
        not _optional_string(module.get("Version"))
        or not _strictly_below(item_dir, vendor_root)
    ):
        raise GoV1Error(
            "vendor_dependency_missing",
            f"package {import_path!r} is not resolved from vendor",
        )
    for candidate_value in (module_dir_value, go_mod_value):
        if not candidate_value:
            continue
        candidate = Path(candidate_value)
        try:
            _validate_contained_path(
                candidate,
                vendor_root,
                allow_toolchain_links=False,
            )
        except (OSError, ValueError) as exc:
            raise GoV1Error(
                "go_module_input_escape",
                "vendored module metadata escapes the vendor tree",
            ) from exc


def _scan_source_directives(path: Path, import_path: str) -> None:
    try:
        with path.open("rb", buffering=0) as handle:
            carry = b""
            while True:
                chunk = handle.read(64 * 1024)
                if not chunk:
                    return
                window = carry + chunk
                if b"//go:cgo_import_dynamic" in window:
                    raise GoV1Error(
                        "go_forbidden_compiler_directive",
                        f"package {import_path!r} contains "
                        "//go:cgo_import_dynamic",
                    )
                if b"//go:generate" in window:
                    raise GoV1Error(
                        "go_generator_forbidden",
                        f"package {import_path!r} contains an active generator",
                    )
                carry = window[-31:]
    except GoV1Error:
        raise
    except (OSError, ValueError) as exc:
        raise GoV1Error(
            "go_source_unreadable",
            f"cannot read active Go file in {import_path!r}",
        ) from exc


def _validate_directory(
    path: Path,
    root: Path,
    *,
    allow_toolchain_links: bool,
) -> None:
    _validate_contained_path(
        path,
        root,
        allow_toolchain_links=allow_toolchain_links,
    )
    value = path.lstat()
    if allow_toolchain_links and stat.S_ISLNK(value.st_mode):
        value = path.stat()
    if not stat.S_ISDIR(value.st_mode) or (
        not allow_toolchain_links and stat.S_ISLNK(value.st_mode)
    ):
        raise ValueError("path is not a real directory")


def _validate_regular_input(
    directory: Path,
    name: str,
    root: Path,
    *,
    allow_toolchain_links: bool,
) -> Path:
    if not name or "\x00" in name:
        raise ValueError("empty or NUL-containing input name")
    path = Path(name)
    if not path.is_absolute():
        path = directory.joinpath(*name.replace("\\", "/").split("/"))
    _validate_regular_absolute(
        path,
        root,
        allow_toolchain_links=allow_toolchain_links,
    )
    return path


def _validate_regular_absolute(
    path: Path,
    root: Path,
    *,
    allow_toolchain_links: bool,
) -> None:
    _validate_contained_path(
        path,
        root,
        allow_toolchain_links=allow_toolchain_links,
    )
    value = path.lstat()
    if allow_toolchain_links and stat.S_ISLNK(value.st_mode):
        resolved = path.resolve(strict=True)
        if not _same_or_below(resolved, root):
            raise ValueError("toolchain link escapes GOROOT")
        value = path.stat()
    if not stat.S_ISREG(value.st_mode) or (
        not allow_toolchain_links and stat.S_ISLNK(value.st_mode)
    ):
        raise ValueError("path is not a regular input")


def _validate_contained_path(
    path: Path,
    root: Path,
    *,
    allow_toolchain_links: bool,
) -> None:
    if not path.is_absolute() or not _same_or_below(path, root):
        raise ValueError("path is outside its trusted root")
    if Path(os.path.normpath(os.fspath(path))) != path:
        raise ValueError("path is not canonical")
    if allow_toolchain_links:
        resolved = path.resolve(strict=True)
        if not _same_or_below(resolved, root):
            raise ValueError("resolved path escapes its trusted root")


def _same_or_below(path: Path, root: Path) -> bool:
    try:
        return os.path.commonpath(
            (os.path.normcase(os.fspath(path)), os.path.normcase(os.fspath(root)))
        ) == os.path.normcase(os.fspath(root))
    except ValueError:
        return False


def _strictly_below(path: Path, root: Path) -> bool:
    return path != root and _same_or_below(path, root)


@dataclass(frozen=True)
class ProcessRequest:
    executable: Path
    identity: _ToolProcessIdentity
    arguments: tuple[str, ...]
    cwd: Path
    environment: Mapping[str, str]
    timeout_seconds: float
    output_limit: int


@dataclass(frozen=True)
class ProcessResult:
    stdout: bytes = b""
    stderr: bytes = b""
    returncode: int = 0
    started: int = 1
    timed_out: bool = False
    overflow: bool = False
    start_failed: bool = False
    detail: str = ""


class ProcessExecutor(Protocol):
    """The worker's only direct child-process creation boundary."""

    def run(self, request: ProcessRequest) -> ProcessResult: ...


class SubprocessProcessExecutor:
    """Start one absolute trusted Go launcher with no shell and closed stdin."""

    def run(self, request: ProcessRequest) -> ProcessResult:
        if request.executable != request.identity.go.path:
            raise GoV1Error(
                CODE_WORKER_IDENTITY_INVALID,
                "worker attempted to start a program outside the frozen Go graph",
            )
        request.identity.verify()

        def verified(result: ProcessResult) -> ProcessResult:
            request.identity.verify()
            return result

        if (
            not request.executable.is_absolute()
            or request.output_limit <= 0
            or request.timeout_seconds <= 0
        ):
            return verified(
                ProcessResult(
                    returncode=-1,
                    start_failed=True,
                    detail="the fixed Go process request is malformed",
                )
            )
        try:
            process = subprocess.Popen(
                (os.fspath(request.executable), *request.arguments),
                cwd=request.cwd,
                env=dict(request.environment),
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                close_fds=True,
                shell=False,
            )
        except OSError as exc:
            return verified(
                ProcessResult(
                    returncode=-1,
                    start_failed=True,
                    detail=str(exc),
                )
            )

        stdout_pipe = process.stdout
        stderr_pipe = process.stderr
        if stdout_pipe is None or stderr_pipe is None:
            process.kill()
            process.wait()
            return verified(
                ProcessResult(
                    returncode=-1,
                    start_failed=True,
                    detail="cannot capture the fixed Go process output",
                )
            )

        stdout = bytearray()
        stderr = bytearray()
        budget = request.output_limit
        budget_lock = threading.Lock()
        overflow = threading.Event()
        reader_errors: list[BaseException] = []

        def drain(stream: BinaryIO, destination: bytearray) -> None:
            nonlocal budget
            try:
                while True:
                    chunk = stream.read(16 * 1024)
                    if not chunk:
                        return
                    with budget_lock:
                        accepted = min(len(chunk), budget)
                        destination.extend(chunk[:accepted])
                        budget -= accepted
                        if accepted != len(chunk):
                            overflow.set()
                            return
            except BaseException as exc:
                with budget_lock:
                    reader_errors.append(exc)

        stdout_thread = threading.Thread(
            target=drain,
            args=(stdout_pipe, stdout),
            daemon=True,
        )
        stderr_thread = threading.Thread(
            target=drain,
            args=(stderr_pipe, stderr),
            daemon=True,
        )
        stdout_thread.start()
        stderr_thread.start()

        deadline = time.monotonic() + request.timeout_seconds
        timed_out = False
        while process.poll() is None:
            if overflow.is_set():
                process.kill()
                break
            if time.monotonic() >= deadline:
                timed_out = True
                process.kill()
                break
            time.sleep(0.005)
        try:
            returncode = process.wait(timeout=_WORKER_SHUTDOWN_GRACE)
        except subprocess.TimeoutExpired:
            process.kill()
            returncode = process.wait()
            timed_out = True
        stdout_thread.join(_WORKER_SHUTDOWN_GRACE)
        stderr_thread.join(_WORKER_SHUTDOWN_GRACE)
        stdout_pipe.close()
        stderr_pipe.close()
        if reader_errors and not (timed_out or overflow.is_set()):
            return verified(
                ProcessResult(
                    stdout=bytes(stdout),
                    stderr=bytes(stderr),
                    returncode=returncode,
                    detail="cannot drain the fixed Go process output",
                )
            )
        return verified(
            ProcessResult(
                stdout=bytes(stdout),
                stderr=bytes(stderr),
                returncode=returncode,
                timed_out=timed_out,
                overflow=overflow.is_set(),
                detail=(
                    ""
                    if returncode == 0
                    else f"fixed Go process exited with status {returncode}"
                ),
            )
        )


@dataclass(frozen=True)
class _ExecutableIdentity:
    path: Path
    sha256: str
    size: int
    device: int
    inode: int
    mode: int

    def to_dict(self) -> dict[str, object]:
        return {
            "path": os.fspath(self.path),
            "sha256": self.sha256,
            "size": self.size,
            "device": self.device,
            "inode": self.inode,
            "mode": self.mode,
        }

    def verify(self) -> None:
        current = _resolve_executable_identity(self.path)
        if current != self:
            raise GoV1Error(
                CODE_WORKER_IDENTITY_INVALID,
                f"frozen executable was replaced: {self.path}",
            )

    def matches_mapping(self, value: Mapping[str, object]) -> None:
        actual = _executable_identity_from_mapping(value, "executable identity")
        if actual != self:
            raise GoV1Error(
                CODE_WORKER_IDENTITY_INVALID,
                "worker identity proof differs from the pre-launch identity",
            )


@dataclass(frozen=True)
class _TreeEntryIdentity:
    path: str
    kind: str
    sha256: str
    size: int
    device: int
    inode: int
    mode: int

    def to_dict(self) -> dict[str, object]:
        return {
            "path": self.path,
            "kind": self.kind,
            "sha256": self.sha256,
            "size": self.size,
            "device": self.device,
            "inode": self.inode,
            "mode": self.mode,
        }


@dataclass(frozen=True)
class _TreeIdentity:
    path: Path
    sha256: str
    total_size: int
    root_device: int
    root_inode: int
    root_mode: int
    entries: tuple[_TreeEntryIdentity, ...]
    executable_files: bool
    label: str
    max_files: int
    max_bytes: int
    ignored_names: tuple[str, ...]
    included_file_suffixes: tuple[str, ...]

    def to_dict(self) -> dict[str, object]:
        return {
            "path": os.fspath(self.path),
            "sha256": self.sha256,
            "total_size": self.total_size,
            "root_device": self.root_device,
            "root_inode": self.root_inode,
            "root_mode": self.root_mode,
            "entries": [entry.to_dict() for entry in self.entries],
        }

    def verify(self) -> None:
        current = _resolve_identity_tree(
            self.path,
            executable_files=self.executable_files,
            label=self.label,
            max_files=self.max_files,
            max_bytes=self.max_bytes,
            ignored_names=self.ignored_names,
            included_file_suffixes=self.included_file_suffixes,
        )
        if current != self:
            raise GoV1Error(
                CODE_WORKER_IDENTITY_INVALID,
                f"{self.label} was replaced or mutated",
            )

    def watch_paths(self) -> tuple[Path, ...]:
        return (
            self.path,
            *(
                self.path.joinpath(*entry.path.split("/"))
                for entry in self.entries
            ),
        )


@dataclass(frozen=True)
class _InterpreterLinkIdentity:
    path: Path
    target: str
    device: int
    inode: int

    def to_dict(self) -> dict[str, object]:
        return {
            "path": os.fspath(self.path),
            "target": self.target,
            "device": self.device,
            "inode": self.inode,
        }


@dataclass(frozen=True)
class _InterpreterRuntimeIdentity:
    """Native images and physical installation data used by one interpreter."""

    python_home: Path
    configuration: _ExecutableIdentity | None
    base_executable: _ExecutableIdentity
    process_image: _ExecutableIdentity
    runtime_image: _ExecutableIdentity

    def to_dict(self) -> dict[str, object]:
        return {
            "python_home": os.fspath(self.python_home),
            "configuration": (
                None
                if self.configuration is None
                else self.configuration.to_dict()
            ),
            "base_executable": self.base_executable.to_dict(),
            "process_image": self.process_image.to_dict(),
            "runtime_image": self.runtime_image.to_dict(),
        }

    def watch_paths(self) -> tuple[Path, ...]:
        files = (
            self.base_executable.path,
            self.process_image.path,
            self.runtime_image.path,
            *(
                ()
                if self.configuration is None
                else (self.configuration.path,)
            ),
        )
        return (
            self.python_home,
            *(path.parent for path in files),
            *files,
        )


@dataclass(frozen=True)
class _InterpreterIdentity:
    invocation_path: Path
    links: tuple[_InterpreterLinkIdentity, ...]
    executable: _ExecutableIdentity
    runtime: _InterpreterRuntimeIdentity

    def to_dict(self) -> dict[str, object]:
        return {
            "invocation_path": os.fspath(self.invocation_path),
            "links": [link.to_dict() for link in self.links],
            "executable": self.executable.to_dict(),
            "runtime": self.runtime.to_dict(),
        }

    def verify(self) -> None:
        if _resolve_interpreter_identity(self.invocation_path) != self:
            raise GoV1Error(
                CODE_WORKER_IDENTITY_INVALID,
                "the installed manager interpreter was replaced",
            )

    def watch_paths(self) -> tuple[Path, ...]:
        return (
            self.executable.path,
            self.executable.path.parent,
            *(link.path.parent for link in self.links),
            *self.runtime.watch_paths(),
        )


@dataclass(frozen=True)
class _StartupIdentity:
    """Every mutable Python startup component the worker launch can reach.

    ``-S -s`` keeps these components from executing, and this identity binds
    them so that inserting or mutating one across the launch boundary is a
    detected worker-identity change rather than unverified startup code.
    """

    site_root: Path
    stdlib_root: Path
    python_home: Path
    site_device: int
    site_inode: int
    site_mode: int
    stdlib_device: int
    stdlib_inode: int
    stdlib_mode: int
    runtime_trees: tuple[_TreeIdentity, ...]
    archive_slots: tuple[Path, ...]
    archives: tuple[_ExecutableIdentity, ...]
    hooks: tuple[_TreeEntryIdentity, ...]
    sha256: str

    def to_dict(self) -> dict[str, object]:
        return {
            "site_root": os.fspath(self.site_root),
            "stdlib_root": os.fspath(self.stdlib_root),
            "python_home": os.fspath(self.python_home),
            "site_device": self.site_device,
            "site_inode": self.site_inode,
            "site_mode": self.site_mode,
            "stdlib_device": self.stdlib_device,
            "stdlib_inode": self.stdlib_inode,
            "stdlib_mode": self.stdlib_mode,
            "runtime_trees": [
                runtime_tree.to_dict()
                for runtime_tree in self.runtime_trees
            ],
            "archive_slots": [
                os.fspath(path) for path in self.archive_slots
            ],
            "archives": [archive.to_dict() for archive in self.archives],
            "hooks": [hook.to_dict() for hook in self.hooks],
            "sha256": self.sha256,
        }

    def watch_paths(self) -> tuple[Path, ...]:
        return (
            self.site_root,
            self.python_home,
            *(runtime_tree.path.parent for runtime_tree in self.runtime_trees),
            *(path.parent for path in self.archive_slots),
            *(
                path
                for runtime_tree in self.runtime_trees
                for path in runtime_tree.watch_paths()
            ),
            *(archive.path for archive in self.archives),
            *(Path(hook.path) for hook in self.hooks),
        )


@dataclass(frozen=True)
class _ManagerIdentity:
    launcher: _ExecutableIdentity
    interpreter: _InterpreterIdentity
    package_tree: _TreeIdentity
    startup: _StartupIdentity

    @property
    def path(self) -> Path:
        return self.launcher.path

    def to_dict(self) -> dict[str, object]:
        return {
            "launcher": self.launcher.to_dict(),
            "interpreter": self.interpreter.to_dict(),
            "package_tree": self.package_tree.to_dict(),
            "startup": self.startup.to_dict(),
        }

    def verify(self) -> None:
        current = _resolve_manager_identity(self.path)
        if current != self:
            raise GoV1Error(
                CODE_WORKER_IDENTITY_INVALID,
                "the installed manager worker TCB was replaced",
            )

    def matches_mapping(self, value: Mapping[str, object]) -> None:
        actual = _manager_identity_from_mapping(value)
        if actual != self:
            raise GoV1Error(
                CODE_WORKER_IDENTITY_INVALID,
                "worker TCB proof differs from the pre-launch identity",
            )

    def watch_paths(self) -> tuple[Path, ...]:
        return (
            self.launcher.path,
            self.launcher.path.parent,
            *self.interpreter.watch_paths(),
            *self.package_tree.watch_paths(),
            *self.startup.watch_paths(),
        )


@dataclass(frozen=True)
class _WorkerLaunchContext:
    """Out-of-band capability proving the manager created this re-execution."""

    parent_pid: int
    secret: bytes
    transport: str = _WORKER_LAUNCH_TRANSPORT

    def public_dict(self) -> dict[str, object]:
        return {
            "parent_pid": self.parent_pid,
            "transport": self.transport,
        }


def _worker_launch_record(secret: bytes, parent_pid: int) -> bytes:
    if len(secret) != _WORKER_LAUNCH_SECRET_BYTES or parent_pid <= 1:
        raise GoV1Error(
            CODE_WORKER_IDENTITY_INVALID,
            "manager-owned worker launch capability is malformed",
        )
    body = _WORKER_LAUNCH_MAGIC + struct.pack(">Q", parent_pid) + secret
    digest = hashlib.sha256(
        _WORKER_LAUNCH_RECORD_DOMAIN + body
    ).digest()
    return body + digest


def _parse_worker_launch_record(record: bytes) -> _WorkerLaunchContext:
    digest_size = hashlib.sha256().digest_size
    expected_length = (
        len(_WORKER_LAUNCH_MAGIC)
        + 8
        + _WORKER_LAUNCH_SECRET_BYTES
        + digest_size
    )
    if len(record) != expected_length:
        raise GoV1Error(
            CODE_WORKER_IDENTITY_INVALID,
            "manager-owned worker launch capability has an invalid length",
        )
    body = record[:-digest_size]
    expected_digest = hashlib.sha256(
        _WORKER_LAUNCH_RECORD_DOMAIN + body
    ).digest()
    if (
        not body.startswith(_WORKER_LAUNCH_MAGIC)
        or not hmac.compare_digest(record[-digest_size:], expected_digest)
    ):
        raise GoV1Error(
            CODE_WORKER_IDENTITY_INVALID,
            "manager-owned worker launch capability is invalid",
        )
    offset = len(_WORKER_LAUNCH_MAGIC)
    parent_pid = struct.unpack(">Q", body[offset : offset + 8])[0]
    secret = body[offset + 8 :]
    if parent_pid != os.getppid() or parent_pid <= 1:
        raise GoV1Error(
            CODE_WORKER_IDENTITY_INVALID,
            "hidden worker is not a direct child of its launch manager",
        )
    return _WorkerLaunchContext(parent_pid=parent_pid, secret=secret)


def _write_launch_record_fd(descriptor: int, record: bytes) -> None:
    offset = 0
    while offset != len(record):
        written = os.write(descriptor, record[offset:])
        if written <= 0:
            raise OSError("cannot write manager-owned launch capability")
        offset += written


class _PreparedWorkerLaunch:
    """One inheritable, non-argv, non-environment launch capability."""

    def __init__(self, platform: str):
        self.platform = platform
        self.secret = secrets.token_bytes(_WORKER_LAUNCH_SECRET_BYTES)
        self._read_fd = -1
        self._windows_handle = 0
        record = _worker_launch_record(self.secret, os.getpid())
        if platform == PLATFORM_MACOS:
            self._prepare_macos(record)
        elif platform == PLATFORM_WINDOWS:
            self._prepare_windows(record)
        else:
            raise GoV1Error(
                CODE_CONTROL_UNAVAILABLE,
                "manager-owned launch capability is unsupported on this host",
            )

    def _prepare_macos(self, record: bytes) -> None:
        read_fd, write_fd = os.pipe()
        try:
            _write_launch_record_fd(write_fd, record)
        finally:
            os.close(write_fd)
        selected = -1
        try:
            for candidate in _WORKER_LAUNCH_FDS:
                try:
                    os.fstat(candidate)
                except OSError as exc:
                    if exc.errno != errno.EBADF:
                        continue
                else:
                    continue
                try:
                    os.dup2(read_fd, candidate, inheritable=True)
                except OSError:
                    continue
                selected = candidate
                break
            if selected < 0:
                raise GoV1Error(
                    CODE_CONTROL_UNAVAILABLE,
                    "no fixed worker launch descriptor is available",
                )
            os.close(read_fd)
            self._read_fd = selected
        except BaseException:
            if read_fd >= 0:
                os.close(read_fd)
            raise

    def _prepare_windows(self, record: bytes) -> None:
        import msvcrt

        windows_runtime = cast(Any, msvcrt)
        read_fd, write_fd = os.pipe()
        try:
            _write_launch_record_fd(write_fd, record)
        finally:
            os.close(write_fd)
        try:
            handle = int(windows_runtime.get_osfhandle(read_fd))
            if handle <= 0 or handle >= _MAX_WINDOWS_HANDLE_SCAN:
                raise OSError("worker launch handle is outside the scan bound")
            cast(Any, os).set_handle_inheritable(handle, True)
            self._read_fd = read_fd
            self._windows_handle = handle
        except BaseException:
            os.close(read_fd)
            raise

    def add_popen_options(self, options: dict[str, object]) -> None:
        if self.platform == PLATFORM_MACOS:
            options["pass_fds"] = (self._read_fd,)
            return
        startup_info = cast(Any, subprocess).STARTUPINFO()
        startup_info.lpAttributeList = {
            "handle_list": [self._windows_handle]
        }
        options["startupinfo"] = startup_info

    def close_parent_copy(self) -> None:
        if self._read_fd < 0:
            return
        try:
            os.close(self._read_fd)
        finally:
            self._read_fd = -1
            self._windows_handle = 0


def _consume_worker_launch_context() -> _WorkerLaunchContext:
    """Consume the manager-only inherited capability before hidden dispatch."""

    platform = inventory_platform()
    if platform == PLATFORM_MACOS:
        record = _read_posix_worker_launch_record()
    else:
        record = _read_windows_worker_launch_record()
    return _parse_worker_launch_record(record)


def _read_posix_worker_launch_record() -> bytes:
    expected = (
        len(_WORKER_LAUNCH_MAGIC)
        + 8
        + _WORKER_LAUNCH_SECRET_BYTES
        + hashlib.sha256().digest_size
    )
    records: list[bytes] = []
    for descriptor in _WORKER_LAUNCH_FDS:
        try:
            info = os.fstat(descriptor)
            if not stat.S_ISFIFO(info.st_mode):
                continue
            os.set_blocking(descriptor, False)
            payload = bytearray()
            while len(payload) != expected:
                chunk = os.read(descriptor, expected - len(payload))
                if not chunk:
                    break
                payload.extend(chunk)
            trailing = os.read(descriptor, 1)
            if trailing:
                continue
            if bytes(payload).startswith(_WORKER_LAUNCH_MAGIC):
                records.append(bytes(payload))
        except OSError:
            continue
        finally:
            try:
                os.close(descriptor)
            except OSError:
                pass
    if len(records) != 1:
        raise GoV1Error(
            CODE_WORKER_IDENTITY_INVALID,
            "hidden mode lacks one manager-owned launch capability",
        )
    return records[0]


def _read_windows_worker_launch_record() -> bytes:
    expected = (
        len(_WORKER_LAUNCH_MAGIC)
        + 8
        + _WORKER_LAUNCH_SECRET_BYTES
        + hashlib.sha256().digest_size
    )
    kernel32 = _windows_kernel32()
    get_file_type = kernel32.GetFileType
    get_file_type.argtypes = [ctypes.c_void_p]
    get_file_type.restype = ctypes.c_uint32
    peek = kernel32.PeekNamedPipe
    peek.argtypes = [
        ctypes.c_void_p,
        ctypes.c_void_p,
        ctypes.c_uint32,
        ctypes.POINTER(ctypes.c_uint32),
        ctypes.POINTER(ctypes.c_uint32),
        ctypes.POINTER(ctypes.c_uint32),
    ]
    peek.restype = ctypes.c_int
    candidates: list[int] = []
    for value in range(4, _MAX_WINDOWS_HANDLE_SCAN, 4):
        handle = ctypes.c_void_p(value)
        if get_file_type(handle) != 3:
            continue
        prefix = ctypes.create_string_buffer(len(_WORKER_LAUNCH_MAGIC))
        read = ctypes.c_uint32()
        available = ctypes.c_uint32()
        if (
            peek(
                handle,
                prefix,
                len(prefix),
                ctypes.byref(read),
                ctypes.byref(available),
                None,
            )
            and available.value == expected
            and prefix.raw[: read.value] == _WORKER_LAUNCH_MAGIC
        ):
            candidates.append(value)
    if len(candidates) != 1:
        raise GoV1Error(
            CODE_WORKER_IDENTITY_INVALID,
            "hidden mode lacks one manager-owned launch capability",
        )
    handle_value = candidates[0]
    read_file = kernel32.ReadFile
    read_file.argtypes = [
        ctypes.c_void_p,
        ctypes.c_void_p,
        ctypes.c_uint32,
        ctypes.POINTER(ctypes.c_uint32),
        ctypes.c_void_p,
    ]
    read_file.restype = ctypes.c_int
    buffer = ctypes.create_string_buffer(expected)
    read = ctypes.c_uint32()
    try:
        if not read_file(
            ctypes.c_void_p(handle_value),
            buffer,
            expected,
            ctypes.byref(read),
            None,
        ) or read.value != expected:
            raise ctypes.WinError(  # type: ignore[attr-defined]
                ctypes.get_last_error()  # type: ignore[attr-defined]
            )
        return bytes(buffer.raw)
    except OSError as exc:
        raise GoV1Error(
            CODE_WORKER_IDENTITY_INVALID,
            "cannot read the manager-owned launch capability",
        ) from exc
    finally:
        _close_windows_handle(handle_value)


def _launch_authenticator(
    secret: bytes,
    domain: bytes,
    nonce: str,
    payload: Mapping[str, object],
) -> str:
    if len(secret) != _WORKER_LAUNCH_SECRET_BYTES:
        raise GoV1Error(
            CODE_WORKER_IDENTITY_INVALID,
            "manager-owned launch secret is malformed",
        )
    try:
        encoded = json.dumps(
            payload,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
        ).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise GoV1Error(
            CODE_WORKER_PROTOCOL_INVALID,
            "worker authentication payload cannot be encoded",
        ) from exc
    digest = hmac.new(secret, digestmod=hashlib.sha256)
    digest.update(domain)
    digest.update(nonce.encode("ascii"))
    digest.update(struct.pack(">Q", len(encoded)))
    digest.update(encoded)
    return digest.hexdigest()


@dataclass(frozen=True)
class _ToolProcessIdentity:
    go: _ExecutableIdentity
    tools: _TreeIdentity

    def to_dict(self) -> dict[str, object]:
        return {
            "go": self.go.to_dict(),
            "tools": self.tools.to_dict(),
        }

    def verify(self) -> None:
        current = _resolve_tool_process_identity(self.go.path, self.tools.path)
        if current != self:
            raise GoV1Error(
                CODE_WORKER_IDENTITY_INVALID,
                "the frozen Go process graph was replaced or mutated",
            )

    def watch_paths(self) -> tuple[Path, ...]:
        return (
            self.go.path,
            self.go.path.parent,
            *self.tools.watch_paths(),
        )


def _resolve_executable_identity(
    path: Path,
    *,
    label: str = "executable",
) -> _ExecutableIdentity:
    return _resolve_file_identity(
        path,
        executable=True,
        label=label,
        max_bytes=_MAX_MANAGER_EXECUTABLE_BYTES,
    )


def _resolve_file_identity(
    path: Path,
    *,
    executable: bool,
    label: str,
    max_bytes: int,
) -> _ExecutableIdentity:
    if not path.is_absolute():
        raise GoV1Error(
            CODE_WORKER_IDENTITY_INVALID,
            f"{label} path is not absolute",
        )
    try:
        initial = path.lstat()
    except (OSError, ValueError) as exc:
        raise GoV1Error(
            CODE_WORKER_IDENTITY_INVALID,
            f"{label} is unavailable",
        ) from exc
    if (
        _is_link_or_reparse(initial)
        or not stat.S_ISREG(initial.st_mode)
        or initial.st_size < (1 if executable else 0)
        or initial.st_size > max_bytes
        or getattr(initial, "st_nlink", 1) != 1
        or (
            executable
            and os.name != "nt"
            and not (initial.st_mode & 0o111)
        )
    ):
        raise GoV1Error(
            CODE_WORKER_IDENTITY_INVALID,
            f"{label} is not a canonical single-link regular file",
        )
    try:
        canonical = path.resolve(strict=True)
    except (OSError, RuntimeError) as exc:
        raise GoV1Error(
            CODE_WORKER_IDENTITY_INVALID,
            f"{label} cannot be canonicalized",
        ) from exc
    if canonical != path:
        raise GoV1Error(
            CODE_WORKER_IDENTITY_INVALID,
            f"{label} path contains a link",
        )
    if os.name == "nt":
        from ._windows import named_data_streams

        try:
            if named_data_streams(path):
                raise GoV1Error(
                    CODE_WORKER_IDENTITY_INVALID,
                    f"{label} carries a named data stream",
                )
        except OSError as exc:
            raise GoV1Error(
                CODE_WORKER_IDENTITY_INVALID,
                f"{label} data streams cannot be inspected",
            ) from exc

    digest = hashlib.sha256()
    try:
        with path.open("rb", buffering=0) as handle:
            opened = os.fstat(handle.fileno())
            if (
                not stat.S_ISREG(opened.st_mode)
                or (opened.st_dev, opened.st_ino)
                != (initial.st_dev, initial.st_ino)
                or opened.st_size != initial.st_size
            ):
                raise GoV1Error(
                    CODE_WORKER_IDENTITY_INVALID,
                    f"{label} changed while opening",
                )
            remaining = opened.st_size
            while remaining:
                chunk = handle.read(min(remaining, 1024 * 1024))
                if not chunk:
                    raise GoV1Error(
                        CODE_WORKER_IDENTITY_INVALID,
                        f"{label} shrank while hashing",
                    )
                remaining -= len(chunk)
                digest.update(chunk)
            if handle.read(1):
                raise GoV1Error(
                    CODE_WORKER_IDENTITY_INVALID,
                    f"{label} grew while hashing",
                )
    except GoV1Error:
        raise
    except OSError as exc:
        raise GoV1Error(
            CODE_WORKER_IDENTITY_INVALID,
            f"{label} cannot be read",
        ) from exc
    after = path.lstat()
    if (
        (after.st_dev, after.st_ino) != (initial.st_dev, initial.st_ino)
        or after.st_size != initial.st_size
        or stat.S_IMODE(after.st_mode) != stat.S_IMODE(initial.st_mode)
        or _is_link_or_reparse(after)
    ):
        raise GoV1Error(
            CODE_WORKER_IDENTITY_INVALID,
            f"{label} changed while hashing",
        )
    return _ExecutableIdentity(
        path=path,
        sha256="sha256:" + digest.hexdigest(),
        size=initial.st_size,
        device=initial.st_dev,
        inode=initial.st_ino,
        mode=stat.S_IMODE(initial.st_mode),
    )


def _executable_identity_from_mapping(
    value: Mapping[str, object],
    label: str,
) -> _ExecutableIdentity:
    _require_exact_keys(
        value,
        {"path", "sha256", "size", "device", "inode", "mode"},
        label,
    )
    return _ExecutableIdentity(
        path=Path(_require_string(value.get("path"), f"{label} path")),
        sha256=_require_string(value.get("sha256"), f"{label} digest"),
        size=_require_int(value.get("size"), f"{label} size"),
        device=_require_int(value.get("device"), f"{label} device"),
        inode=_require_int(value.get("inode"), f"{label} inode"),
        mode=_require_int(value.get("mode"), f"{label} mode"),
    )


def _resolve_identity_tree(
    path: Path,
    *,
    executable_files: bool,
    label: str,
    max_files: int,
    max_bytes: int,
    ignored_names: tuple[str, ...] = (),
    included_file_suffixes: tuple[str, ...] = (),
) -> _TreeIdentity:
    if not path.is_absolute():
        raise GoV1Error(
            CODE_WORKER_IDENTITY_INVALID,
            f"{label} path is not absolute",
        )
    try:
        root_stat = path.lstat()
        canonical = path.resolve(strict=True)
    except (OSError, RuntimeError) as exc:
        raise GoV1Error(
            CODE_WORKER_IDENTITY_INVALID,
            f"{label} is unavailable",
        ) from exc
    if (
        canonical != path
        or _is_link_or_reparse(root_stat)
        or not stat.S_ISDIR(root_stat.st_mode)
    ):
        raise GoV1Error(
            CODE_WORKER_IDENTITY_INVALID,
            f"{label} is not a canonical link-free directory",
        )
    _reject_identity_named_streams(path, label)

    entries: list[_TreeEntryIdentity] = []
    total_size = 0

    def descend(directory: Path, prefix: str) -> None:
        nonlocal total_size
        try:
            with os.scandir(directory) as iterator:
                names = sorted(
                    (entry.name for entry in iterator),
                    key=lambda item: item.encode("utf-8", errors="strict"),
                )
        except (OSError, UnicodeEncodeError) as exc:
            raise GoV1Error(
                CODE_WORKER_IDENTITY_INVALID,
                f"{label} cannot be enumerated",
            ) from exc
        for name in names:
            if name in ignored_names:
                continue
            relative = f"{prefix}/{name}" if prefix else name
            if not is_valid_portable_path(relative):
                raise GoV1Error(
                    CODE_WORKER_IDENTITY_INVALID,
                    f"{label} contains a non-portable path",
                )
            absolute = directory / name
            try:
                info = absolute.lstat()
            except OSError as exc:
                raise GoV1Error(
                    CODE_WORKER_IDENTITY_INVALID,
                    f"{label} changed while enumerating",
                ) from exc
            selected_file = (
                not included_file_suffixes
                or name.casefold().endswith(included_file_suffixes)
            )
            if _is_link_or_reparse(info):
                try:
                    linked_directory = absolute.is_dir()
                except OSError as exc:
                    raise GoV1Error(
                        CODE_WORKER_IDENTITY_INVALID,
                        f"{label} link cannot be classified",
                    ) from exc
                if linked_directory or selected_file:
                    raise GoV1Error(
                        CODE_WORKER_IDENTITY_INVALID,
                        f"{label} contains an importable link or reparse point",
                    )
                continue
            _reject_identity_named_streams(absolute, f"{label} {relative!r}")
            if stat.S_ISDIR(info.st_mode):
                try:
                    if absolute.resolve(strict=True) != absolute:
                        raise OSError("directory path contains a link")
                except (OSError, RuntimeError) as exc:
                    raise GoV1Error(
                        CODE_WORKER_IDENTITY_INVALID,
                        f"{label} directory changed while opening",
                    ) from exc
                entries.append(
                    _TreeEntryIdentity(
                        path=relative,
                        kind="directory",
                        sha256="",
                        size=0,
                        device=info.st_dev,
                        inode=info.st_ino,
                        mode=stat.S_IMODE(info.st_mode),
                    )
                )
                descend(absolute, relative)
                continue
            if not stat.S_ISREG(info.st_mode):
                raise GoV1Error(
                    CODE_WORKER_IDENTITY_INVALID,
                    f"{label} contains a special file",
                )
            if not selected_file:
                continue
            identity = _resolve_file_identity(
                absolute,
                executable=executable_files,
                label=f"{label} file {relative!r}",
                max_bytes=max_bytes,
            )
            total_size += identity.size
            if len(entries) + 1 > max_files or total_size > max_bytes:
                raise GoV1Error(
                    CODE_WORKER_IDENTITY_INVALID,
                    f"{label} exceeds its identity bound",
                )
            entries.append(
                _TreeEntryIdentity(
                    path=relative,
                    kind="file",
                    sha256=identity.sha256,
                    size=identity.size,
                    device=identity.device,
                    inode=identity.inode,
                    mode=identity.mode,
                )
            )

    descend(path, "")
    file_count = sum(entry.kind == "file" for entry in entries)
    if file_count == 0 or file_count > max_files:
        raise GoV1Error(
            CODE_WORKER_IDENTITY_INVALID,
            f"{label} does not contain a bounded regular-file tree",
        )
    entries.sort(key=lambda item: item.path.encode("utf-8"))
    digest = hashlib.sha256()
    digest.update(_TREE_IDENTITY_DOMAIN)
    for entry in entries:
        encoded = json.dumps(
            entry.to_dict(),
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        digest.update(struct.pack(">Q", len(encoded)))
        digest.update(encoded)
    try:
        after = path.lstat()
    except OSError as exc:
        raise GoV1Error(
            CODE_WORKER_IDENTITY_INVALID,
            f"{label} disappeared while hashing",
        ) from exc
    if (
        (after.st_dev, after.st_ino) != (root_stat.st_dev, root_stat.st_ino)
        or stat.S_IMODE(after.st_mode) != stat.S_IMODE(root_stat.st_mode)
        or _is_link_or_reparse(after)
    ):
        raise GoV1Error(
            CODE_WORKER_IDENTITY_INVALID,
            f"{label} changed while hashing",
        )
    return _TreeIdentity(
        path=path,
        sha256="sha256:" + digest.hexdigest(),
        total_size=total_size,
        root_device=root_stat.st_dev,
        root_inode=root_stat.st_ino,
        root_mode=stat.S_IMODE(root_stat.st_mode),
        entries=tuple(entries),
        executable_files=executable_files,
        label=label,
        max_files=max_files,
        max_bytes=max_bytes,
        ignored_names=ignored_names,
        included_file_suffixes=included_file_suffixes,
    )


def _tree_identity_from_mapping(
    value: Mapping[str, object],
    *,
    executable_files: bool,
    label: str,
    max_files: int,
    max_bytes: int,
    ignored_names: tuple[str, ...] = (),
    included_file_suffixes: tuple[str, ...] = (),
) -> _TreeIdentity:
    _require_exact_keys(
        value,
        {
            "path",
            "sha256",
            "total_size",
            "root_device",
            "root_inode",
            "root_mode",
            "entries",
        },
        label,
    )
    raw_entries = _require_list(value.get("entries"), f"{label} entries")
    if not raw_entries or len(raw_entries) > max_files * 2:
        raise GoV1Error(
            CODE_WORKER_PROTOCOL_INVALID,
            f"{label} entry set is not bounded",
        )
    entries: list[_TreeEntryIdentity] = []
    for raw in raw_entries:
        entry = _require_mapping(raw, f"{label} entry")
        _require_exact_keys(
            entry,
            {"path", "kind", "sha256", "size", "device", "inode", "mode"},
            f"{label} entry",
        )
        relative = _require_string(entry.get("path"), f"{label} entry path")
        if not is_valid_portable_path(relative):
            raise GoV1Error(
                CODE_WORKER_PROTOCOL_INVALID,
                f"{label} carries a non-portable path",
            )
        kind = _require_string(entry.get("kind"), f"{label} entry kind")
        if kind not in {"directory", "file"}:
            raise GoV1Error(
                CODE_WORKER_PROTOCOL_INVALID,
                f"{label} carries an unknown entry kind",
            )
        entries.append(
            _TreeEntryIdentity(
                path=relative,
                kind=kind,
                sha256=_require_string(
                    entry.get("sha256"),
                    f"{label} entry digest",
                ),
                size=_require_int(entry.get("size"), f"{label} entry size"),
                device=_require_int(
                    entry.get("device"),
                    f"{label} entry device",
                ),
                inode=_require_int(
                    entry.get("inode"),
                    f"{label} entry inode",
                ),
                mode=_require_int(entry.get("mode"), f"{label} entry mode"),
            )
        )
    if len({entry.path for entry in entries}) != len(entries):
        raise GoV1Error(
            CODE_WORKER_PROTOCOL_INVALID,
            f"{label} carries duplicate paths",
        )
    result = _TreeIdentity(
        path=Path(_require_string(value.get("path"), f"{label} path")),
        sha256=_require_string(value.get("sha256"), f"{label} digest"),
        total_size=_require_int(
            value.get("total_size"),
            f"{label} total size",
        ),
        root_device=_require_int(
            value.get("root_device"),
            f"{label} root device",
        ),
        root_inode=_require_int(
            value.get("root_inode"),
            f"{label} root inode",
        ),
        root_mode=_require_int(
            value.get("root_mode"),
            f"{label} root mode",
        ),
        entries=tuple(entries),
        executable_files=executable_files,
        label=label,
        max_files=max_files,
        max_bytes=max_bytes,
        ignored_names=ignored_names,
        included_file_suffixes=included_file_suffixes,
    )
    if (
        result.total_size < 0
        or result.total_size > max_bytes
        or sum(entry.kind == "file" for entry in result.entries) > max_files
    ):
        raise GoV1Error(
            CODE_WORKER_PROTOCOL_INVALID,
            f"{label} exceeds its identity bound",
        )
    return result


def _canonical_runtime_directory(path: Path, label: str) -> Path:
    try:
        info = path.lstat()
        canonical = path.resolve(strict=True)
    except (OSError, RuntimeError) as exc:
        raise GoV1Error(
            CODE_WORKER_IDENTITY_INVALID,
            f"{label} is unavailable",
        ) from exc
    if (
        canonical != path
        or _is_link_or_reparse(info)
        or not stat.S_ISDIR(info.st_mode)
    ):
        raise GoV1Error(
            CODE_WORKER_IDENTITY_INVALID,
            f"{label} is not one canonical directory",
        )
    return canonical


def _resolve_configuration_identity(
    path: Path,
) -> tuple[_ExecutableIdentity, str]:
    identity = _resolve_file_identity(
        path,
        executable=False,
        label="installed manager pyvenv configuration",
        max_bytes=_MAX_INTERPRETER_CONFIGURATION_BYTES,
    )
    try:
        payload = path.read_bytes()
    except OSError as exc:
        raise GoV1Error(
            CODE_WORKER_IDENTITY_INVALID,
            "installed manager pyvenv configuration cannot be read",
        ) from exc
    current = _resolve_file_identity(
        path,
        executable=False,
        label="installed manager pyvenv configuration",
        max_bytes=_MAX_INTERPRETER_CONFIGURATION_BYTES,
    )
    if current != identity:
        raise GoV1Error(
            CODE_WORKER_IDENTITY_INVALID,
            "installed manager pyvenv configuration changed while reading",
        )
    try:
        text = payload.decode("utf-8-sig", errors="strict")
    except UnicodeDecodeError as exc:
        raise GoV1Error(
            CODE_WORKER_IDENTITY_INVALID,
            "installed manager pyvenv configuration is not UTF-8",
        ) from exc
    if "\x00" in text:
        raise GoV1Error(
            CODE_WORKER_IDENTITY_INVALID,
            "installed manager pyvenv configuration contains a NUL",
        )
    if (
        len(payload) != identity.size
        or "sha256:" + hashlib.sha256(payload).hexdigest() != identity.sha256
    ):
        raise GoV1Error(
            CODE_WORKER_IDENTITY_INVALID,
            "installed manager pyvenv configuration changed while reading",
        )
    return identity, text


def _pyvenv_home(text: str) -> Path:
    homes: list[str] = []
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if "=" not in line:
            raise GoV1Error(
                CODE_WORKER_IDENTITY_INVALID,
                "installed manager pyvenv configuration is malformed",
            )
        key, value = line.split("=", 1)
        if key.strip().casefold() == "home":
            homes.append(value.strip())
    if len(homes) != 1 or not homes[0]:
        raise GoV1Error(
            CODE_WORKER_IDENTITY_INVALID,
            "installed manager pyvenv configuration has no unique home",
        )
    result = Path(homes[0])
    if not result.is_absolute():
        raise GoV1Error(
            CODE_WORKER_IDENTITY_INVALID,
            "installed manager pyvenv home is not absolute",
        )
    return _canonical_runtime_directory(
        result,
        "installed manager base Python home",
    )


def _resolve_windows_interpreter_runtime(
    executable: _ExecutableIdentity,
) -> _InterpreterRuntimeIdentity:
    prefix = executable.path.parent.parent
    configuration_path = prefix / "pyvenv.cfg"
    configuration: _ExecutableIdentity | None = None
    if configuration_path.exists():
        configuration, configuration_text = (
            _resolve_configuration_identity(configuration_path)
        )
        python_home = _pyvenv_home(configuration_text)
    else:
        python_home = _canonical_runtime_directory(
            executable.path.parent,
            "installed manager base Python home",
        )
    base_path = python_home / "python.exe"
    base_executable = (
        executable
        if base_path == executable.path
        else _resolve_executable_identity(
            base_path,
            label="installed manager base Python interpreter",
        )
    )
    try:
        names = sorted(
            (
                entry.name
                for entry in os.scandir(python_home)
                if entry.is_file(follow_symlinks=False)
            ),
            key=lambda item: item.encode("utf-8", errors="strict"),
        )
    except (OSError, UnicodeEncodeError) as exc:
        raise GoV1Error(
            CODE_WORKER_IDENTITY_INVALID,
            "cannot inspect the installed manager Python runtime images",
        ) from exc
    candidates = []
    for name in names:
        folded = name.casefold()
        if not (folded.startswith("python") and folded.endswith(".dll")):
            continue
        version = folded.removeprefix("python").removesuffix(".dll")
        if len(version) >= 2 and version.isdigit():
            candidates.append(python_home / name)
    if len(candidates) != 1:
        raise GoV1Error(
            CODE_WORKER_IDENTITY_INVALID,
            "cannot resolve exactly one installed manager Python runtime DLL",
        )
    return _InterpreterRuntimeIdentity(
        python_home=python_home,
        configuration=configuration,
        base_executable=base_executable,
        # A standard Windows venv python.exe is a launcher process.  Execute
        # the identity-bound base interpreter directly so the authenticated
        # worker remains the manager's direct child and its reported process
        # image is the one that actually hosts Python.
        process_image=base_executable,
        runtime_image=_resolve_executable_identity(
            candidates[0],
            label="installed manager Python runtime image",
        ),
    )


def _resolve_macos_interpreter_runtime(
    executable: _ExecutableIdentity,
) -> _InterpreterRuntimeIdentity:
    app_layout = (
        executable.path.name == "Python"
        and executable.path.parent.name == "MacOS"
        and executable.path.parent.parent.name == "Contents"
        and executable.path.parent.parent.parent.name == "Python.app"
        and executable.path.parent.parent.parent.parent.name == "Resources"
    )
    python_home = _canonical_runtime_directory(
        (
            executable.path.parents[4]
            if app_layout
            else executable.path.parent.parent
        ),
        "installed manager base Python home",
    )
    framework_runtime = python_home / "Python"
    framework_process = (
        python_home
        / "Resources"
        / "Python.app"
        / "Contents"
        / "MacOS"
        / "Python"
    )
    if framework_runtime.is_file():
        runtime_image = _resolve_executable_identity(
            framework_runtime,
            label="installed manager Python runtime image",
        )
        process_image = (
            executable
            if app_layout
            else (
                _resolve_executable_identity(
                    framework_process,
                    label="installed manager Python process image",
                )
                if framework_process.is_file()
                else executable
            )
        )
    else:
        library = python_home / "lib"
        candidates: list[Path] = []
        try:
            entries = os.scandir(library)
        except FileNotFoundError:
            entries = None
        except OSError as exc:
            raise GoV1Error(
                CODE_WORKER_IDENTITY_INVALID,
                "cannot inspect the installed manager Python runtime images",
            ) from exc
        if entries is not None:
            with entries:
                for entry in entries:
                    folded = entry.name.casefold()
                    if (
                        entry.is_file(follow_symlinks=False)
                        and folded.startswith("libpython")
                        and folded.endswith(".dylib")
                    ):
                        candidates.append(Path(entry.path))
        candidates.sort(key=lambda item: os.fspath(item).encode("utf-8"))
        if len(candidates) > 1:
            raise GoV1Error(
                CODE_WORKER_IDENTITY_INVALID,
                "installed manager Python runtime image is ambiguous",
            )
        runtime_image = (
            executable
            if not candidates
            else _resolve_executable_identity(
                candidates[0],
                label="installed manager Python runtime image",
            )
        )
        process_image = executable
    return _InterpreterRuntimeIdentity(
        python_home=python_home,
        configuration=None,
        base_executable=executable,
        process_image=process_image,
        runtime_image=runtime_image,
    )


def _resolve_interpreter_runtime(
    executable: _ExecutableIdentity,
) -> _InterpreterRuntimeIdentity:
    if os.name == "nt":
        return _resolve_windows_interpreter_runtime(executable)
    if sys.platform == "darwin":
        return _resolve_macos_interpreter_runtime(executable)
    raise GoV1Error(
        CODE_CONTROL_UNAVAILABLE,
        "manager interpreter identity is unsupported on this host",
    )


def _resolve_interpreter_identity(path: Path) -> _InterpreterIdentity:
    if not path.is_absolute():
        raise GoV1Error(
            CODE_WORKER_IDENTITY_INVALID,
            "manager interpreter path is not absolute",
        )
    invocation_path = Path(os.path.abspath(os.fspath(path)))
    current = invocation_path
    links: list[_InterpreterLinkIdentity] = []
    for _ in range(16):
        try:
            info = current.lstat()
        except OSError as exc:
            raise GoV1Error(
                CODE_WORKER_IDENTITY_INVALID,
                "manager interpreter is unavailable",
            ) from exc
        if not stat.S_ISLNK(info.st_mode):
            if _is_link_or_reparse(info):
                raise GoV1Error(
                    CODE_WORKER_IDENTITY_INVALID,
                    "manager interpreter uses an unsupported reparse point",
                )
            executable = _resolve_executable_identity(
                current.resolve(strict=True),
                label="manager interpreter",
            )
            return _InterpreterIdentity(
                invocation_path=invocation_path,
                links=tuple(links),
                executable=executable,
                runtime=_resolve_interpreter_runtime(executable),
            )
        try:
            target = os.readlink(current)
        except OSError as exc:
            raise GoV1Error(
                CODE_WORKER_IDENTITY_INVALID,
                "manager interpreter link cannot be read",
            ) from exc
        links.append(
            _InterpreterLinkIdentity(
                path=current,
                target=target,
                device=info.st_dev,
                inode=info.st_ino,
            )
        )
        target_path = Path(target)
        current = (
            target_path
            if target_path.is_absolute()
            else current.parent / target_path
        )
        current = Path(os.path.abspath(os.fspath(current)))
    raise GoV1Error(
        CODE_WORKER_IDENTITY_INVALID,
        "manager interpreter has an excessive link chain",
    )


def _interpreter_identity_from_mapping(
    value: Mapping[str, object],
) -> _InterpreterIdentity:
    _require_exact_keys(
        value,
        {"invocation_path", "links", "executable", "runtime"},
        "manager interpreter identity",
    )
    links: list[_InterpreterLinkIdentity] = []
    for raw in _require_list(
        value.get("links"),
        "manager interpreter links",
    ):
        link = _require_mapping(raw, "manager interpreter link")
        _require_exact_keys(
            link,
            {"path", "target", "device", "inode"},
            "manager interpreter link",
        )
        links.append(
            _InterpreterLinkIdentity(
                path=Path(
                    _require_string(
                        link.get("path"),
                        "manager interpreter link path",
                    )
                ),
                target=_require_string(
                    link.get("target"),
                    "manager interpreter link target",
                ),
                device=_require_int(
                    link.get("device"),
                    "manager interpreter link device",
                ),
                inode=_require_int(
                    link.get("inode"),
                    "manager interpreter link inode",
                ),
            )
        )
    if len(links) > 16:
        raise GoV1Error(
            CODE_WORKER_PROTOCOL_INVALID,
            "manager interpreter link chain is excessive",
        )
    raw_runtime = _require_mapping(
        value.get("runtime"),
        "manager interpreter runtime identity",
    )
    _require_exact_keys(
        raw_runtime,
        {
            "python_home",
            "configuration",
            "base_executable",
            "process_image",
            "runtime_image",
        },
        "manager interpreter runtime identity",
    )
    raw_configuration = raw_runtime.get("configuration")
    configuration = (
        None
        if raw_configuration is None
        else _executable_identity_from_mapping(
            _require_mapping(
                raw_configuration,
                "manager interpreter configuration",
            ),
            "manager interpreter configuration",
        )
    )
    runtime = _InterpreterRuntimeIdentity(
        python_home=Path(
            _require_string(
                raw_runtime.get("python_home"),
                "manager interpreter Python home",
            )
        ),
        configuration=configuration,
        base_executable=_executable_identity_from_mapping(
            _require_mapping(
                raw_runtime.get("base_executable"),
                "manager base interpreter",
            ),
            "manager base interpreter",
        ),
        process_image=_executable_identity_from_mapping(
            _require_mapping(
                raw_runtime.get("process_image"),
                "manager interpreter process image",
            ),
            "manager interpreter process image",
        ),
        runtime_image=_executable_identity_from_mapping(
            _require_mapping(
                raw_runtime.get("runtime_image"),
                "manager interpreter runtime image",
            ),
            "manager interpreter runtime image",
        ),
    )
    if (
        not runtime.python_home.is_absolute()
        or (
            runtime.configuration is not None
            and not runtime.configuration.path.is_absolute()
        )
        or any(
            not item.path.is_absolute()
            for item in (
                runtime.base_executable,
                runtime.process_image,
                runtime.runtime_image,
            )
        )
    ):
        raise GoV1Error(
            CODE_WORKER_PROTOCOL_INVALID,
            "manager interpreter runtime identity has a relative path",
        )
    return _InterpreterIdentity(
        invocation_path=Path(
            _require_string(
                value.get("invocation_path"),
                "manager interpreter invocation path",
            )
        ),
        links=tuple(links),
        executable=_executable_identity_from_mapping(
            _require_mapping(
                value.get("executable"),
                "manager interpreter executable",
            ),
            "manager interpreter executable",
        ),
        runtime=runtime,
    )


def _manager_interpreter_path(launcher: Path) -> Path:
    if os.name == "nt":
        prefix = launcher.parent.parent
        candidates = (
            prefix / "Scripts" / "python.exe",
            prefix / "python.exe",
        )
        existing = [path for path in candidates if path.is_file()]
        if len(existing) != 1:
            raise GoV1Error(
                CODE_WORKER_IDENTITY_INVALID,
                "cannot resolve the installed manager interpreter",
            )
        return existing[0]
    try:
        with launcher.open("rb", buffering=0) as stream:
            first_line = stream.readline(4097)
    except OSError as exc:
        raise GoV1Error(
            CODE_WORKER_IDENTITY_INVALID,
            "cannot read the installed manager launcher",
        ) from exc
    if (
        not first_line.startswith(b"#!")
        or len(first_line) > 4096
        or not first_line.endswith(b"\n")
    ):
        raise GoV1Error(
            CODE_WORKER_IDENTITY_INVALID,
            "installed manager launcher has no fixed interpreter",
        )
    raw = first_line[2:-1]
    if raw.endswith(b"\r"):
        raw = raw[:-1]
    try:
        value = raw.decode("utf-8", errors="strict")
    except UnicodeDecodeError as exc:
        raise GoV1Error(
            CODE_WORKER_IDENTITY_INVALID,
            "installed manager interpreter path is not UTF-8",
        ) from exc
    if not value or any(character.isspace() for character in value):
        raise GoV1Error(
            CODE_WORKER_IDENTITY_INVALID,
            "installed manager interpreter is not one fixed path",
        )
    result = Path(value)
    if not result.is_absolute():
        raise GoV1Error(
            CODE_WORKER_IDENTITY_INVALID,
            "installed manager interpreter path is not absolute",
        )
    if not result.name.lower().startswith("python"):
        # A shell-wrapper launcher would put an interpreter of another program
        # into the fixed four-node process graph and hide the real Python
        # invocation from the manager, so it fails closed here.
        raise GoV1Error(
            CODE_WORKER_IDENTITY_INVALID,
            "installed manager launcher does not exec a Python interpreter",
        )
    return result


def _manager_package_root(launcher: Path) -> Path:
    if launcher.parent.name.lower() not in {"bin", "scripts"}:
        raise GoV1Error(
            CODE_WORKER_IDENTITY_INVALID,
            "installed manager launcher is outside a fixed Python prefix",
        )
    prefix = launcher.parent.parent
    candidates: list[Path] = []
    windows_candidate = prefix / "Lib" / "site-packages" / "csk"
    if windows_candidate.exists():
        candidates.append(windows_candidate)
    for library_name in ("lib", "lib64"):
        library = prefix / library_name
        try:
            children = list(os.scandir(library))
        except FileNotFoundError:
            continue
        except OSError as exc:
            raise GoV1Error(
                CODE_WORKER_IDENTITY_INVALID,
                "cannot inspect the installed manager Python prefix",
            ) from exc
        for child in children:
            if not child.name.startswith("python"):
                continue
            candidate = Path(child.path) / "site-packages" / "csk"
            if candidate.exists():
                candidates.append(candidate)
    distinct = tuple(dict.fromkeys(candidates))
    if len(distinct) != 1:
        raise GoV1Error(
            CODE_WORKER_IDENTITY_INVALID,
            "cannot resolve exactly one installed csk package tree",
        )
    package_root = distinct[0]
    required = (
        package_root / "__init__.py",
        package_root / "cli.py",
        package_root / "builds" / "go_v1.py",
    )
    if not all(path.is_file() for path in required):
        raise GoV1Error(
            CODE_WORKER_IDENTITY_INVALID,
            "installed manager package tree is incomplete",
        )
    return package_root


def _manager_stdlib_root(
    interpreter: _InterpreterIdentity,
    site_root: Path,
) -> Path:
    """Derive the one standard-library root the fixed worker launch loads from.

    ``-S`` removes ``site`` from the launch, so the worker's import roots are
    exactly this directory tree plus the manager-owned package tree.  The root
    is derived from the canonical interpreter executable, never from an
    environment value or a ``PATH`` lookup.
    """

    candidates: list[Path] = []
    if _interpreter_runtime_platform(interpreter.runtime) == PLATFORM_WINDOWS:
        candidates.append(
            _manager_windows_stdlib_root(interpreter.runtime)
        )
    else:
        for library_name in ("lib", "lib64"):
            candidates.append(
                interpreter.runtime.python_home
                / library_name
                / site_root.parent.name
            )
    existing = [
        candidate
        for candidate in dict.fromkeys(candidates)
        if candidate.is_dir()
    ]
    if len(existing) != 1:
        raise GoV1Error(
            CODE_WORKER_IDENTITY_INVALID,
            "cannot resolve exactly one installed manager standard library",
        )
    return existing[0]


def _manager_windows_stdlib_root(
    runtime: _InterpreterRuntimeIdentity,
) -> Path:
    return runtime.python_home / "Lib"


def _interpreter_runtime_platform(
    runtime: _InterpreterRuntimeIdentity,
) -> str:
    if runtime.runtime_image.path.suffix.casefold() == ".dll":
        return PLATFORM_WINDOWS
    return PLATFORM_MACOS


def _manager_runtime_roots(
    stdlib_root: Path,
    interpreter: _InterpreterIdentity,
) -> tuple[Path, ...]:
    if _interpreter_runtime_platform(
        interpreter.runtime
    ) == PLATFORM_WINDOWS:
        # Windows CPython adds its installation prefix itself to sys.path in
        # addition to Lib, DLLs, and the versioned archive slot.  Bind the
        # complete importable Python home so that accepting that unavoidable
        # prefix never admits an unverified root-level module or namespace
        # package.  Disabled site-package trees remain excluded by the fixed
        # runtime-tree policy below.
        return (interpreter.runtime.python_home,)
    return (stdlib_root,)


def _resolve_runtime_archives(
    archive_slots: Sequence[Path],
) -> tuple[_ExecutableIdentity, ...]:
    archives: list[_ExecutableIdentity] = []
    if len(archive_slots) > _MAX_RUNTIME_ARCHIVES:
        raise GoV1Error(
            CODE_WORKER_IDENTITY_INVALID,
            "the installed manager runtime archive set is unbounded",
    )
    for slot in archive_slots:
        try:
            slot.lstat()
        except FileNotFoundError:
            continue
        except OSError as exc:
            raise GoV1Error(
                CODE_WORKER_IDENTITY_INVALID,
                "cannot inspect the installed manager runtime archives",
            ) from exc
        archives.append(
            _resolve_file_identity(
                slot,
                executable=False,
                label=(
                    "installed manager runtime archive "
                    f"{slot.name}"
                ),
                max_bytes=_MAX_RUNTIME_ARCHIVE_BYTES,
            )
        )
    archives.sort(key=lambda item: os.fspath(item.path).encode("utf-8"))
    return tuple(archives)


def _runtime_archive_slots(
    stdlib_root: Path,
    interpreter: _InterpreterIdentity,
) -> tuple[Path, ...]:
    if _interpreter_runtime_platform(
        interpreter.runtime
    ) == PLATFORM_WINDOWS:
        runtime_name = interpreter.runtime.runtime_image.path.name.casefold()
        version = (
            runtime_name.removeprefix("python").removesuffix(".dll")
        )
        if (
            not runtime_name.startswith("python")
            or not runtime_name.endswith(".dll")
            or len(version) < 2
            or not version.isdigit()
        ):
            raise GoV1Error(
                CODE_WORKER_IDENTITY_INVALID,
                "Python runtime DLL has no fixed archive slot",
            )
        return (
            interpreter.runtime.python_home / f"python{version}.zip",
        )
    version_root = stdlib_root.name.casefold()
    if not version_root.startswith("python"):
        raise GoV1Error(
            CODE_WORKER_IDENTITY_INVALID,
            "standard library root has no fixed Python archive slot",
        )
    version = version_root.removeprefix("python")
    if not version or any(
        character not in "0123456789."
        for character in version
    ):
        raise GoV1Error(
            CODE_WORKER_IDENTITY_INVALID,
            "standard library version is not canonical",
        )
    return (stdlib_root.parent / f"python{version.replace('.', '')}.zip",)


def _startup_hook_roots(
    launcher: Path,
    interpreter: _InterpreterIdentity,
    site_root: Path,
    stdlib_root: Path,
) -> tuple[Path, ...]:
    return tuple(
        dict.fromkeys(
            (
                launcher.parent.parent,
                interpreter.executable.path.parent,
                interpreter.invocation_path.parent,
                interpreter.runtime.python_home,
                interpreter.runtime.base_executable.path.parent,
                interpreter.runtime.process_image.path.parent,
                interpreter.runtime.runtime_image.path.parent,
                site_root,
                stdlib_root,
                stdlib_root / "site-packages",
            )
        )
    )


def _is_startup_configuration_name(name: str) -> bool:
    folded = name.casefold()
    if folded in {
        candidate.casefold()
        for candidate in _STARTUP_PREFIX_CONFIGURATION
    }:
        return True
    if not (
        folded.startswith("python")
        and folded.endswith("._pth")
    ):
        return False
    version = folded.removeprefix("python").removesuffix("._pth")
    return bool(version) and version.isdigit()


def _resolve_startup_identity(
    launcher: Path,
    interpreter: _InterpreterIdentity,
    site_root: Path,
) -> _StartupIdentity:
    stdlib_root = _manager_stdlib_root(interpreter, site_root)
    python_home = interpreter.runtime.python_home
    runtime_roots = _manager_runtime_roots(stdlib_root, interpreter)
    runtime_trees = tuple(
        _resolve_identity_tree(
            root,
            executable_files=False,
            label="installed manager Python runtime",
            max_files=_MAX_RUNTIME_FILES,
            max_bytes=_MAX_RUNTIME_BYTES,
            ignored_names=_RUNTIME_IGNORED_NAMES,
            included_file_suffixes=_RUNTIME_FILE_SUFFIXES,
        )
        for root in runtime_roots
    )
    if not runtime_trees or len(runtime_trees) > _MAX_RUNTIME_TREES:
        raise GoV1Error(
            CODE_WORKER_IDENTITY_INVALID,
            "the installed manager Python runtime root set is not bounded",
        )
    archive_slots = _runtime_archive_slots(stdlib_root, interpreter)
    archives = _resolve_runtime_archives(archive_slots)
    hooks: list[_TreeEntryIdentity] = []
    seen: set[Path] = set()
    for root in _startup_hook_roots(
        launcher,
        interpreter,
        site_root,
        stdlib_root,
    ):
        try:
            children = sorted(entry.name for entry in os.scandir(root))
        except FileNotFoundError:
            continue
        except OSError as exc:
            raise GoV1Error(
                CODE_WORKER_IDENTITY_INVALID,
                "cannot inspect the installed manager startup component set",
            ) from exc
        for name in children:
            if not (
                name.endswith(".pth")
                or name in _STARTUP_HOOK_MODULES
                or _is_startup_configuration_name(name)
            ):
                continue
            component = root / name
            if component in seen:
                continue
            seen.add(component)
            if len(hooks) >= _MAX_STARTUP_HOOKS:
                raise GoV1Error(
                    CODE_WORKER_IDENTITY_INVALID,
                    "the installed manager startup component set is unbounded",
                )
            identity = _resolve_file_identity(
                component,
                executable=False,
                label=f"installed manager startup component {name}",
                max_bytes=_MAX_STARTUP_HOOK_BYTES,
            )
            hooks.append(
                _TreeEntryIdentity(
                    path=component.as_posix(),
                    kind="file",
                    sha256=identity.sha256,
                    size=identity.size,
                    device=identity.device,
                    inode=identity.inode,
                    mode=identity.mode,
                )
            )
    hooks.sort(key=lambda item: item.path.encode("utf-8"))
    site_stat = _startup_root_stat(site_root, "installed manager site root")
    stdlib_stat = _startup_root_stat(
        stdlib_root,
        "installed manager standard library",
    )
    digest = hashlib.sha256()
    digest.update(_STARTUP_IDENTITY_DOMAIN)
    for payload in (
        {
            "site_root": site_root.as_posix(),
            "python_home": python_home.as_posix(),
            "site_device": site_stat.st_dev,
            "site_inode": site_stat.st_ino,
            "site_mode": stat.S_IMODE(site_stat.st_mode),
            "stdlib_root": stdlib_root.as_posix(),
            "stdlib_device": stdlib_stat.st_dev,
            "stdlib_inode": stdlib_stat.st_ino,
            "stdlib_mode": stat.S_IMODE(stdlib_stat.st_mode),
        },
        *(runtime_tree.to_dict() for runtime_tree in runtime_trees),
        *(
            {"archive_slot": archive_slot.as_posix()}
            for archive_slot in archive_slots
        ),
        *(archive.to_dict() for archive in archives),
        *(hook.to_dict() for hook in hooks),
    ):
        encoded = json.dumps(
            payload,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        digest.update(struct.pack(">Q", len(encoded)))
        digest.update(encoded)
    return _StartupIdentity(
        site_root=site_root,
        stdlib_root=stdlib_root,
        python_home=python_home,
        site_device=site_stat.st_dev,
        site_inode=site_stat.st_ino,
        site_mode=stat.S_IMODE(site_stat.st_mode),
        stdlib_device=stdlib_stat.st_dev,
        stdlib_inode=stdlib_stat.st_ino,
        stdlib_mode=stat.S_IMODE(stdlib_stat.st_mode),
        runtime_trees=runtime_trees,
        archive_slots=archive_slots,
        archives=archives,
        hooks=tuple(hooks),
        sha256="sha256:" + digest.hexdigest(),
    )


def _startup_root_stat(path: Path, label: str) -> os.stat_result:
    try:
        info = path.lstat()
    except OSError as exc:
        raise GoV1Error(
            CODE_WORKER_IDENTITY_INVALID,
            f"{label} is unavailable",
        ) from exc
    if _is_link_or_reparse(info) or not stat.S_ISDIR(info.st_mode):
        raise GoV1Error(
            CODE_WORKER_IDENTITY_INVALID,
            f"{label} is not a canonical directory",
        )
    return info


def _startup_identity_from_mapping(
    value: Mapping[str, object],
    interpreter: _InterpreterIdentity,
) -> _StartupIdentity:
    _require_exact_keys(
        value,
        {
            "site_root",
            "stdlib_root",
            "python_home",
            "site_device",
            "site_inode",
            "site_mode",
            "stdlib_device",
            "stdlib_inode",
            "stdlib_mode",
            "runtime_trees",
            "archive_slots",
            "archives",
            "hooks",
            "sha256",
        },
        "manager startup identity",
    )
    raw_hooks = _require_list(
        value.get("hooks"),
        "manager startup components",
    )
    if len(raw_hooks) > _MAX_STARTUP_HOOKS:
        raise GoV1Error(
            CODE_WORKER_PROTOCOL_INVALID,
            "manager startup component set is not bounded",
        )
    hooks: list[_TreeEntryIdentity] = []
    for item in raw_hooks:
        entry = _require_mapping(item, "manager startup component")
        _require_exact_keys(
            entry,
            {"path", "kind", "sha256", "size", "device", "inode", "mode"},
            "manager startup component",
        )
        if _require_string(entry.get("kind"), "startup component kind") != "file":
            raise GoV1Error(
                CODE_WORKER_PROTOCOL_INVALID,
                "manager startup component is not a regular file entry",
            )
        hooks.append(
            _TreeEntryIdentity(
                path=_require_string(
                    entry.get("path"),
                    "startup component path",
                ),
                kind="file",
                sha256=_require_string(
                    entry.get("sha256"),
                    "startup component digest",
                ),
                size=_require_int(entry.get("size"), "startup component size"),
                device=_require_int(
                    entry.get("device"),
                    "startup component device",
                ),
                inode=_require_int(
                    entry.get("inode"),
                    "startup component inode",
                ),
                mode=_require_int(entry.get("mode"), "startup component mode"),
            )
        )
    raw_runtime_trees = _require_list(
        value.get("runtime_trees"),
        "manager runtime trees",
    )
    if (
        not raw_runtime_trees
        or len(raw_runtime_trees) > _MAX_RUNTIME_TREES
    ):
        raise GoV1Error(
            CODE_WORKER_PROTOCOL_INVALID,
            "manager runtime root set is not bounded",
        )
    runtime_trees = tuple(
        _tree_identity_from_mapping(
            _require_mapping(item, "manager runtime tree"),
            executable_files=False,
            label="installed manager Python runtime",
            max_files=_MAX_RUNTIME_FILES,
            max_bytes=_MAX_RUNTIME_BYTES,
            ignored_names=_RUNTIME_IGNORED_NAMES,
            included_file_suffixes=_RUNTIME_FILE_SUFFIXES,
        )
        for item in raw_runtime_trees
    )
    raw_archives = _require_list(
        value.get("archives"),
        "manager runtime archives",
    )
    if len(raw_archives) > _MAX_RUNTIME_ARCHIVES:
        raise GoV1Error(
            CODE_WORKER_PROTOCOL_INVALID,
            "manager runtime archive set is not bounded",
        )
    archives = tuple(
        _executable_identity_from_mapping(
            _require_mapping(item, "manager runtime archive"),
            "manager runtime archive",
        )
        for item in raw_archives
    )
    archive_slots = tuple(
        Path(item)
        for item in _string_list(
            value.get("archive_slots"),
            "manager runtime archive slots",
        )
    )
    stdlib_root = Path(
        _require_string(value.get("stdlib_root"), "standard library root")
    )
    python_home = Path(
        _require_string(value.get("python_home"), "Python home")
    )
    expected_runtime_roots = _manager_runtime_roots(
        stdlib_root,
        interpreter,
    )
    if (
        tuple(tree.path for tree in runtime_trees) != expected_runtime_roots
        or python_home != interpreter.runtime.python_home
        or archive_slots != _runtime_archive_slots(
            stdlib_root,
            interpreter,
        )
        or any(archive.path not in archive_slots for archive in archives)
    ):
        raise GoV1Error(
            CODE_WORKER_PROTOCOL_INVALID,
            "manager runtime identity carries inconsistent roots",
        )
    return _StartupIdentity(
        site_root=Path(_require_string(value.get("site_root"), "site root")),
        stdlib_root=stdlib_root,
        python_home=python_home,
        site_device=_require_int(value.get("site_device"), "site root device"),
        site_inode=_require_int(value.get("site_inode"), "site root inode"),
        site_mode=_require_int(value.get("site_mode"), "site root mode"),
        stdlib_device=_require_int(
            value.get("stdlib_device"),
            "standard library device",
        ),
        stdlib_inode=_require_int(
            value.get("stdlib_inode"),
            "standard library inode",
        ),
        stdlib_mode=_require_int(
            value.get("stdlib_mode"),
            "standard library mode",
        ),
        runtime_trees=runtime_trees,
        archive_slots=archive_slots,
        archives=archives,
        hooks=tuple(hooks),
        sha256=_require_string(value.get("sha256"), "startup identity digest"),
    )


def _resolve_manager_identity(
    launcher: Path,
    *,
    loaded_package_root: Path | None = None,
    running_interpreter: Path | None = None,
) -> _ManagerIdentity:
    launcher_identity = _resolve_executable_identity(
        launcher,
        label="installed manager launcher",
    )
    interpreter_path = _manager_interpreter_path(launcher)
    interpreter = _resolve_interpreter_identity(interpreter_path)
    if running_interpreter is not None:
        actual = Path(os.path.abspath(os.fspath(running_interpreter)))
        if (
            actual != interpreter.invocation_path
            and actual.resolve(strict=True) != interpreter.executable.path
            and actual.resolve(strict=True)
            != interpreter.runtime.base_executable.path
        ):
            raise GoV1Error(
                CODE_WORKER_IDENTITY_INVALID,
                "worker is running under an unexpected interpreter",
            )
    package_root = _manager_package_root(launcher)
    if loaded_package_root is not None:
        try:
            loaded = loaded_package_root.resolve(strict=True)
        except (OSError, RuntimeError) as exc:
            raise GoV1Error(
                CODE_WORKER_IDENTITY_INVALID,
                "worker loaded package tree cannot be canonicalized",
            ) from exc
        if loaded != package_root:
            raise GoV1Error(
                CODE_WORKER_IDENTITY_INVALID,
                "worker loaded an unexpected csk package tree",
            )
    package_tree = _resolve_identity_tree(
        package_root,
        executable_files=False,
        label="installed manager package tree",
        max_files=_MAX_MANAGER_PACKAGE_FILES,
        max_bytes=_MAX_MANAGER_PACKAGE_BYTES,
        ignored_names=("__pycache__",),
    )
    return _ManagerIdentity(
        launcher=launcher_identity,
        interpreter=interpreter,
        package_tree=package_tree,
        startup=_resolve_startup_identity(
            launcher,
            interpreter,
            package_root.parent,
        ),
    )


def _resolve_worker_manager_identity(launcher: Path) -> _ManagerIdentity:
    return _resolve_manager_identity(
        launcher,
        loaded_package_root=Path(__file__).resolve(strict=True).parents[1],
        running_interpreter=Path(sys.executable),
    )


def _manager_executable_from_argv0(
    value: str,
    *,
    _windows: bool | None = None,
) -> Path:
    """Recover the fixed console executable name from interpreter-owned argv0."""

    result = Path(os.path.abspath(value))
    windows = os.name == "nt" if _windows is None else _windows
    if windows and not result.suffix:
        # distlib's Windows console-script __main__ removes the .exe suffix
        # from sys.argv[0].  Restore only that fixed sibling suffix; resolution
        # and identity verification still fail closed on absence or replacement.
        return result.with_name(result.name + ".exe")
    return result


def _manager_identity_from_mapping(
    value: Mapping[str, object],
) -> _ManagerIdentity:
    _require_exact_keys(
        value,
        {"launcher", "interpreter", "package_tree", "startup"},
        "manager identity",
    )
    interpreter = _interpreter_identity_from_mapping(
        _require_mapping(
            value.get("interpreter"),
            "manager interpreter",
        )
    )
    return _ManagerIdentity(
        launcher=_executable_identity_from_mapping(
            _require_mapping(value.get("launcher"), "manager launcher"),
            "manager launcher",
        ),
        interpreter=interpreter,
        package_tree=_tree_identity_from_mapping(
            _require_mapping(
                value.get("package_tree"),
                "manager package tree",
            ),
            executable_files=False,
            label="installed manager package tree",
            max_files=_MAX_MANAGER_PACKAGE_FILES,
            max_bytes=_MAX_MANAGER_PACKAGE_BYTES,
            ignored_names=("__pycache__",),
        ),
        startup=_startup_identity_from_mapping(
            _require_mapping(
                value.get("startup"),
                "manager startup identity",
            ),
            interpreter,
        ),
    )


def _resolve_tool_process_identity(
    go_executable: Path,
    tool_directory: Path,
) -> _ToolProcessIdentity:
    return _ToolProcessIdentity(
        go=_resolve_executable_identity(
            go_executable,
            label="fingerprinted GOROOT/bin/go",
        ),
        tools=_resolve_identity_tree(
            tool_directory,
            executable_files=True,
            label="fingerprinted GOROOT tool directory",
            max_files=_MAX_TOOL_EXECUTABLES,
            max_bytes=_MAX_TOOL_TREE_BYTES,
            ignored_names=(),
        ),
    )


def _tool_process_identity_from_mapping(
    value: Mapping[str, object],
) -> _ToolProcessIdentity:
    _require_exact_keys(
        value,
        {"go", "tools"},
        "Go process identity",
    )
    return _ToolProcessIdentity(
        go=_executable_identity_from_mapping(
            _require_mapping(value.get("go"), "Go executable identity"),
            "Go executable identity",
        ),
        tools=_tree_identity_from_mapping(
            _require_mapping(value.get("tools"), "Go tool tree identity"),
            executable_files=True,
            label="fingerprinted GOROOT tool directory",
            max_files=_MAX_TOOL_EXECUTABLES,
            max_bytes=_MAX_TOOL_TREE_BYTES,
            ignored_names=(),
        ),
    )


def _reject_identity_named_streams(path: Path, label: str) -> None:
    if os.name != "nt":
        return
    from ._windows import named_data_streams

    try:
        streams = named_data_streams(path)
    except OSError as exc:
        raise GoV1Error(
            CODE_WORKER_IDENTITY_INVALID,
            f"{label} data streams cannot be inspected",
        ) from exc
    if streams:
        raise GoV1Error(
            CODE_WORKER_IDENTITY_INVALID,
            f"{label} carries a named data stream",
        )


class _IdentityMutationGuard:
    """Detect macOS identity changes and lock Windows identities during use."""

    def __init__(self, platform: str, paths: Sequence[Path]):
        self.platform = platform
        self.paths = tuple(dict.fromkeys(paths))
        if (
            not self.paths
            or len(self.paths) > _MAX_RETAINED_IDENTITY_PATHS
        ):
            raise GoV1Error(
                CODE_CONTROL_UNAVAILABLE,
                "execution identity set is outside its retention bound",
            )
        self._mac_queue: Any | None = None
        self._mac_fds: list[int] = []
        self._mac_paths: dict[int, Path] = {}
        self._mac_previous_nofile_limit: tuple[int, int] | None = None
        self._mac_lock_held = False
        self._windows_handles: list[int] = []
        self._closed = False
        self._close_error: BaseException | None = None
        try:
            if platform == PLATFORM_MACOS:
                _MACOS_IDENTITY_GUARD_LOCK.acquire()
                self._mac_lock_held = True
                self._setup_macos()
            elif platform == PLATFORM_WINDOWS:
                self._setup_windows()
            else:
                raise GoV1Error(
                    CODE_CONTROL_UNAVAILABLE,
                    "identity mutation guard is unsupported on this host",
                )
        except BaseException as exc:
            self._release_handles()
            if isinstance(exc, GoV1Error):
                raise
            raise GoV1Error(
                CODE_CONTROL_UNAVAILABLE,
                "cannot retain the execution identity set before worker launch",
            ) from exc

    def _setup_macos(self) -> None:
        import select

        self._mac_previous_nofile_limit = (
            _ensure_macos_identity_descriptor_capacity(len(self.paths))
        )
        queue_value = select.kqueue()
        self._mac_queue = queue_value
        changes: list[Any] = []
        event_mask = (
            select.KQ_NOTE_DELETE
            | select.KQ_NOTE_WRITE
            | select.KQ_NOTE_EXTEND
            | select.KQ_NOTE_LINK
            | select.KQ_NOTE_RENAME
            | select.KQ_NOTE_REVOKE
        )
        for path in self.paths:
            info = path.lstat()
            if _is_link_or_reparse(info):
                continue
            flags = os.O_EVTONLY | getattr(os, "O_CLOEXEC", 0)
            flags |= getattr(os, "O_NOFOLLOW", 0)
            descriptor = os.open(path, flags)
            self._mac_fds.append(descriptor)
            self._mac_paths[descriptor] = path
            changes.append(
                select.kevent(
                    descriptor,
                    filter=select.KQ_FILTER_VNODE,
                    flags=(
                        select.KQ_EV_ADD
                        | select.KQ_EV_ENABLE
                        | select.KQ_EV_CLEAR
                    ),
                    fflags=event_mask,
                )
            )
        if not changes:
            raise OSError("identity set has no retainable paths")
        queue_value.control(changes, 0, 0)

    def _setup_windows(self) -> None:
        for path in self.paths:
            info = path.lstat()
            if _is_link_or_reparse(info):
                continue
            self._windows_handles.append(
                _open_windows_identity_handle(
                    path,
                    directory=stat.S_ISDIR(info.st_mode),
                )
            )
        if not self._windows_handles:
            raise OSError("identity set has no retainable paths")

    def close(self) -> None:
        if self._closed:
            if self._close_error is not None:
                raise self._close_error
            return
        self._closed = True
        failure: BaseException | None = None
        try:
            self._verify_retained_identity()
        except BaseException as exc:
            failure = exc
        release_error = self._release_handles()
        if failure is None:
            failure = release_error
        elif release_error is not None:
            failure.add_note(str(release_error))
        self._close_error = failure
        if failure is not None:
            raise failure

    def verify(self) -> None:
        if self._closed:
            if self._close_error is not None:
                raise self._close_error
            raise GoV1Error(
                CODE_CONTROL_UNAVAILABLE,
                "execution identity guard is already closed",
            )
        self._verify_retained_identity()

    def _verify_retained_identity(self) -> None:
        if self._mac_queue is None:
            return
        try:
            events = self._mac_queue.control(
                None,
                max(1, len(self._mac_fds) * 2),
                0,
            )
        except BaseException as exc:
            failure = GoV1Error(
                CODE_CONTROL_UNAVAILABLE,
                "cannot verify the retained macOS execution identity set",
            )
            failure.__cause__ = exc
            raise failure
        if events:
            changed = sorted(
                {
                    os.fspath(self._mac_paths.get(event.ident, Path("?")))
                    for event in events
                }
            )
            raise GoV1Error(
                CODE_WORKER_IDENTITY_INVALID,
                "an execution identity changed while the worker ran: "
                + ", ".join(changed[:8]),
            )

    def _release_handles(self) -> BaseException | None:
        failure: BaseException | None = None
        for descriptor in self._mac_fds:
            try:
                os.close(descriptor)
            except OSError as exc:
                if failure is None:
                    failure = exc
        self._mac_fds.clear()
        self._mac_paths.clear()
        if self._mac_queue is not None:
            try:
                self._mac_queue.close()
            except OSError as exc:
                if failure is None:
                    failure = exc
            self._mac_queue = None
        if self._mac_previous_nofile_limit is not None:
            try:
                _restore_macos_identity_descriptor_capacity(
                    self._mac_previous_nofile_limit
                )
            except OSError as exc:
                if failure is None:
                    failure = exc
            self._mac_previous_nofile_limit = None
        if self._mac_lock_held:
            _MACOS_IDENTITY_GUARD_LOCK.release()
            self._mac_lock_held = False
        retained: list[int] = []
        for handle in self._windows_handles:
            try:
                _close_windows_handle(handle)
            except OSError as exc:
                retained.append(handle)
                if failure is None:
                    failure = exc
        self._windows_handles = retained
        if failure is not None and not isinstance(failure, GoV1Error):
            wrapped = GoV1Error(
                CODE_CONTROL_UNAVAILABLE,
                "cannot release the retained execution identity set",
            )
            wrapped.__cause__ = failure
            return wrapped
        return failure


def _ensure_macos_identity_descriptor_capacity(
    path_count: int,
) -> tuple[int, int] | None:
    import resource

    if path_count <= 0 or path_count > _MAX_RETAINED_IDENTITY_PATHS:
        raise OSError("macOS identity descriptor count is outside its bound")
    previous = resource.getrlimit(resource.RLIMIT_NOFILE)
    # The retained vnode descriptors and the fixed out-of-band worker
    # capability share the same descriptor table.  Keep every fixed candidate
    # legal so collision fallback remains available after the identity set is
    # retained.
    required = max(
        path_count + _MACOS_IDENTITY_FD_HEADROOM,
        _MACOS_FIXED_DESCRIPTOR_CAPACITY,
    )
    hard = previous[1]
    if hard != resource.RLIM_INFINITY and required > hard:
        raise OSError("macOS identity descriptor hard limit is insufficient")
    if previous[0] >= required:
        return None
    resource.setrlimit(resource.RLIMIT_NOFILE, (required, hard))
    current = resource.getrlimit(resource.RLIMIT_NOFILE)
    if current[0] < required:
        try:
            resource.setrlimit(resource.RLIMIT_NOFILE, previous)
        finally:
            raise OSError(
                "macOS identity descriptor soft limit could not be raised"
            )
    return previous


def _restore_macos_identity_descriptor_capacity(
    previous: tuple[int, int],
) -> None:
    import resource

    resource.setrlimit(resource.RLIMIT_NOFILE, previous)


def _open_windows_identity_handle(path: Path, *, directory: bool) -> int:
    kernel32 = _windows_kernel32()
    create_file = kernel32.CreateFileW
    create_file.argtypes = [
        ctypes.c_wchar_p,
        ctypes.c_uint32,
        ctypes.c_uint32,
        ctypes.c_void_p,
        ctypes.c_uint32,
        ctypes.c_uint32,
        ctypes.c_void_p,
    ]
    create_file.restype = ctypes.c_void_p
    generic_read = 0x80000000
    file_share_read = 0x00000001
    open_existing = 3
    file_flag_backup_semantics = 0x02000000
    file_flag_open_reparse_point = 0x00200000
    flags = file_flag_open_reparse_point
    if directory:
        flags |= file_flag_backup_semantics
    value = create_file(
        os.fspath(path),
        generic_read,
        file_share_read,
        None,
        open_existing,
        flags,
        None,
    )
    invalid = ctypes.c_void_p(-1).value
    if not value or value == invalid:
        raise ctypes.WinError(ctypes.get_last_error())  # type: ignore[attr-defined]
    return int(value)


def _is_link_or_reparse(value: os.stat_result) -> bool:
    if stat.S_ISLNK(value.st_mode):
        return True
    reparse = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
    return bool(getattr(value, "st_file_attributes", 0) & reparse)


def _write_message(stream: BinaryIO, message: Mapping[str, object]) -> None:
    try:
        payload = json.dumps(
            message,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
        ).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise GoV1Error(
            CODE_WORKER_PROTOCOL_INVALID,
            "session message cannot be encoded",
        ) from exc
    if not payload or len(payload) > _MAX_PROTOCOL_FRAME:
        raise GoV1Error(
            CODE_WORKER_PROTOCOL_INVALID,
            "outgoing session frame exceeds the protocol bound",
        )
    try:
        stream.write(struct.pack(">I", len(payload)))
        stream.write(payload)
        stream.flush()
    except (OSError, ValueError) as exc:
        raise GoV1Error(
            CODE_WORKER_PROTOCOL_INVALID,
            "cannot write the worker session channel",
        ) from exc


def _read_message(stream: BinaryIO) -> Mapping[str, object]:
    header = _read_exact(stream, 4)
    length = struct.unpack(">I", header)[0]
    if length == 0 or length > _MAX_PROTOCOL_FRAME:
        raise GoV1Error(
            CODE_WORKER_PROTOCOL_INVALID,
            f"session frame length {length} is outside the bound",
        )
    payload = _read_exact(stream, length)
    try:
        message = json.loads(
            payload.decode("utf-8", errors="strict"),
            object_pairs_hook=_closed_object,
        )
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
        raise GoV1Error(
            CODE_WORKER_PROTOCOL_INVALID,
            "session frame is not one strict JSON message",
        ) from exc
    result = _require_mapping(message, "session message")
    kind = _require_string(result.get("kind"), "session message kind")
    if kind not in _MESSAGE_KINDS:
        raise GoV1Error(
            CODE_WORKER_PROTOCOL_INVALID,
            f"unknown session message kind {kind!r}",
        )
    return result


def _read_exact(stream: BinaryIO, length: int) -> bytes:
    result = bytearray()
    try:
        while len(result) != length:
            chunk = stream.read(length - len(result))
            if not chunk:
                raise GoV1Error(
                    CODE_WORKER_PROTOCOL_INVALID,
                    "worker session channel closed inside a frame",
                )
            result.extend(chunk)
    except GoV1Error:
        raise
    except (OSError, ValueError) as exc:
        raise GoV1Error(
            CODE_WORKER_PROTOCOL_INVALID,
            "cannot read the worker session channel",
        ) from exc
    return bytes(result)


def _closed_object(
    pairs: list[tuple[str, object]],
) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"duplicate JSON field {key!r}")
        result[key] = value
    return result


def _build_permit(secret: str, nonce: str, argv: Sequence[str]) -> str:
    try:
        key = bytes.fromhex(secret)
    except ValueError as exc:
        raise GoV1Error(
            CODE_WORKER_PROTOCOL_INVALID,
            "session secret is malformed",
        ) from exc
    digest = hmac.new(key, digestmod=hashlib.sha256)
    digest.update(_PERMIT_DOMAIN)
    digest.update(nonce.encode("ascii"))
    digest.update(b"\x00")
    digest.update("\x00".join(argv).encode("utf-8"))
    return digest.hexdigest()


def _result_to_mapping(result: ProcessResult) -> dict[str, object]:
    return {
        "stdout": base64.b64encode(result.stdout).decode("ascii"),
        "stderr": base64.b64encode(result.stderr).decode("ascii"),
        "returncode": result.returncode,
        "started": result.started,
        "timed_out": result.timed_out,
        "overflow": result.overflow,
        "start_failed": result.start_failed,
        "detail": result.detail,
    }


def _result_from_mapping(value: Mapping[str, object]) -> ProcessResult:
    _require_exact_keys(
        value,
        {
            "stdout",
            "stderr",
            "returncode",
            "started",
            "timed_out",
            "overflow",
            "start_failed",
            "detail",
        },
        "worker process result",
    )
    try:
        stdout = base64.b64decode(
            _require_string(value.get("stdout"), "result stdout"),
            validate=True,
        )
        stderr = base64.b64decode(
            _require_string(value.get("stderr"), "result stderr"),
            validate=True,
        )
    except (ValueError, binascii.Error) as exc:
        raise GoV1Error(
            CODE_WORKER_PROTOCOL_INVALID,
            "worker result output is not canonical base64",
        ) from exc
    return ProcessResult(
        stdout=stdout,
        stderr=stderr,
        returncode=_require_int(value.get("returncode"), "result returncode"),
        started=_require_int(value.get("started"), "result started count"),
        timed_out=_require_bool(value.get("timed_out"), "result timeout flag"),
        overflow=_require_bool(value.get("overflow"), "result overflow flag"),
        start_failed=_require_bool(
            value.get("start_failed"),
            "result start failure flag",
        ),
        detail=_require_string(value.get("detail"), "result detail"),
    )


@dataclass(frozen=True)
class _WorkerPlan:
    executable: _ManagerIdentity
    process_identity: _ToolProcessIdentity
    go_executable: Path
    goroot: Path
    tool_directory: Path
    worker_cache: Path
    directory: Path
    environment: Mapping[str, str]
    list_argv: tuple[str, ...]
    build_argv: tuple[str, ...]
    artifact_path: Path
    readonly_roots: tuple[Path, ...]
    private_roots: tuple[Path, ...]
    platform: str
    probes: tuple[ControlProbe, ...]
    limits: ResourceLimits


class _WorkerSession:
    def __init__(
        self,
        input_stream: BinaryIO,
        output_stream: BinaryIO,
        *,
        executor: ProcessExecutor,
        worker_executable: Path,
        identity_resolver: Callable[[Path], _ManagerIdentity],
        control_observer: Callable[
            [str, Sequence[ControlProbe], ResourceLimits, Sequence[BinaryIO]],
            tuple[str, ...],
        ],
        runtime_proof: Mapping[str, object],
        launch_context: _WorkerLaunchContext,
    ):
        self._input = input_stream
        self._output = output_stream
        self._executor = executor
        self._worker_executable = worker_executable
        self._identity_resolver = identity_resolver
        self._control_observer = control_observer
        self._runtime_proof = dict(runtime_proof)
        self._launch_context = launch_context
        self._nonce = ""
        self._secret = ""
        self._plan: _WorkerPlan | None = None
        self._manager_identity: _ManagerIdentity | None = None
        self._started = 0

    @property
    def nonce(self) -> str:
        return self._nonce

    def run(self) -> None:
        self._accept()
        self._serve_list()
        self._serve_build()
        self._await_shutdown()

    def _accept(self) -> None:
        message = _read_message(self._input)
        _require_exact_keys(
            message,
            {"kind", "nonce", "manager_auth", "request"},
            "request message",
        )
        if message.get("kind") != "request":
            raise GoV1Error(
                CODE_WORKER_PROTOCOL_INVALID,
                "the session must open with one request",
            )
        nonce = _require_string(message.get("nonce"), "session nonce")
        if len(nonce) != _SESSION_TOKEN_LENGTH:
            raise GoV1Error(
                CODE_WORKER_PROTOCOL_INVALID,
                "session nonce is malformed",
            )
        try:
            bytes.fromhex(nonce)
        except ValueError as exc:
            raise GoV1Error(
                CODE_WORKER_PROTOCOL_INVALID,
                "session nonce is malformed",
            ) from exc
        self._nonce = nonce
        request = _require_mapping(message.get("request"), "worker request")
        manager_auth = _require_string(
            message.get("manager_auth"),
            "manager launch authenticator",
        )
        expected_auth = _launch_authenticator(
            self._launch_context.secret,
            _WORKER_LAUNCH_REQUEST_DOMAIN,
            nonce,
            request,
        )
        if not hmac.compare_digest(manager_auth, expected_auth):
            raise GoV1Error(
                CODE_WORKER_IDENTITY_INVALID,
                "worker request is not authenticated by its launch manager",
            )
        plan, secret, expected_identity = _plan_from_request(request)
        self._secret = secret

        actual_identity = self._identity_resolver(self._worker_executable)
        expected_identity.matches_mapping(actual_identity.to_dict())
        validate_worker_runtime(
            self._runtime_proof,
            actual_identity,
            expected_launch_context=self._launch_context,
        )
        self._validate_plan(plan)
        applied = self._control_observer(
            plan.platform,
            plan.probes,
            plan.limits,
            (self._input, self._output),
        )
        evidence = evidence_from_applied(plan.platform, plan.probes, applied)
        validate_capability_evidence(evidence, plan.platform, plan.probes)
        self._manager_identity = actual_identity
        self._plan = plan
        ready: dict[str, object] = {
            "identity": actual_identity.to_dict(),
            "runtime": self._runtime_proof,
            "applied": list(applied),
            "evidence": evidence.to_dict(),
        }
        ready["worker_auth"] = _launch_authenticator(
            self._launch_context.secret,
            _WORKER_LAUNCH_READY_DOMAIN,
            self._nonce,
            ready,
        )
        _write_message(
            self._output,
            {"kind": "ready", "nonce": self._nonce, "ready": ready},
        )

    def _validate_plan(self, plan: _WorkerPlan) -> None:
        if plan.platform != inventory_platform():
            raise GoV1Error(
                CODE_CAPABILITY_EVIDENCE_INVALID,
                "worker request platform is not this host",
            )
        _validate_probe_set(plan.platform, plan.probes)
        if len(plan.readonly_roots) != 2 or not plan.private_roots:
            raise GoV1Error(
                CODE_WORKER_PROTOCOL_INVALID,
                "worker request does not carry the closed root set",
            )
        source_root, goroot = plan.readonly_roots
        if (
            not source_root.is_absolute()
            or not goroot.is_absolute()
            or goroot != plan.goroot
        ):
            raise GoV1Error(
                CODE_WORKER_PROTOCOL_INVALID,
                "worker request roots are not absolute manager roots",
            )
        expected_go = goroot / "bin" / (
            "go.exe" if plan.platform == PLATFORM_WINDOWS else "go"
        )
        if plan.go_executable != expected_go:
            raise GoV1Error(
                CODE_WORKER_IDENTITY_INVALID,
                "worker request selects a non-fingerprinted Go launcher",
            )
        if (
            plan.process_identity.go.path != plan.go_executable
            or plan.process_identity.tools.path != plan.tool_directory
        ):
            raise GoV1Error(
                CODE_WORKER_IDENTITY_INVALID,
                "worker request process paths differ from their frozen identities",
            )
        if (
            not plan.worker_cache.is_absolute()
            or plan.worker_cache not in plan.private_roots
        ):
            raise GoV1Error(
                CODE_WORKER_PROTOCOL_INVALID,
                "worker bytecode cache is not one manager-private root",
            )
        goos = plan.environment.get("GOOS", "")
        goarch = plan.environment.get("GOARCH", "")
        if (
            plan.tool_directory
            != goroot / "pkg" / "tool" / f"{goos}_{goarch}"
        ):
            raise GoV1Error(
                CODE_WORKER_IDENTITY_INVALID,
                "worker request selects an unexpected Go tool directory",
            )
        if (
            not plan.directory.is_absolute()
            or not _same_or_below(plan.directory, source_root)
        ):
            raise GoV1Error(
                CODE_WORKER_PROTOCOL_INVALID,
                "worker directory escapes the frozen source snapshot",
            )
        for root in plan.private_roots:
            if (
                not root.is_absolute()
                or _same_or_below(root, source_root)
                or _same_or_below(source_root, root)
                or _same_or_below(root, goroot)
                or _same_or_below(goroot, root)
            ):
                raise GoV1Error(
                    CODE_WORKER_PROTOCOL_INVALID,
                    "worker private roots overlap source or GOROOT",
                )
        if plan.list_argv != LIST_ARGUMENTS:
            raise GoV1Error(
                CODE_WORKER_PROTOCOL_INVALID,
                "worker request carries a non-protocol go list vector",
            )
        _validate_fixed_build_argv(
            plan.build_argv,
            plan.artifact_path,
            plan.private_roots,
        )
        _validate_worker_environment(
            plan.environment,
            plan.goroot,
            plan.private_roots,
            plan.platform,
        )
        _normalize_limits(plan.limits)
        plan.process_identity.verify()

    def _expect(self, kind: str, fields: set[str]) -> Mapping[str, object]:
        message = _read_message(self._input)
        _require_exact_keys(message, {"kind", "nonce", *fields}, f"{kind} message")
        if message.get("kind") != kind:
            raise GoV1Error(
                CODE_WORKER_PROTOCOL_INVALID,
                f"session expected {kind!r}, got {message.get('kind')!r}",
            )
        if message.get("nonce") != self._nonce:
            raise GoV1Error(
                CODE_WORKER_PROTOCOL_INVALID,
                "session message carries a replayed or unknown nonce",
            )
        return message

    def _serve_list(self) -> None:
        self._expect("list", set())
        plan = self._required_plan()
        result = self._run_go(plan.list_argv)
        _write_message(
            self._output,
            {
                "kind": "list-result",
                "nonce": self._nonce,
                "result": _result_to_mapping(result),
            },
        )

    def _serve_build(self) -> None:
        message = self._expect("permit", {"permit"})
        plan = self._required_plan()
        permit = _require_string(message.get("permit"), "build permit")
        expected = _build_permit(self._secret, self._nonce, plan.build_argv)
        if not hmac.compare_digest(permit, expected):
            raise GoV1Error(
                CODE_WORKER_PROTOCOL_INVALID,
                "build permit is not bound to this session and build vector",
            )
        result = self._run_go(plan.build_argv)
        _write_message(
            self._output,
            {
                "kind": "build-result",
                "nonce": self._nonce,
                "result": _result_to_mapping(result),
            },
        )

    def _await_shutdown(self) -> None:
        self._expect("shutdown", set())

    def _run_go(self, arguments: tuple[str, ...]) -> ProcessResult:
        plan = self._required_plan()
        manager_identity = self._required_manager_identity()
        manager_identity.verify()
        plan.process_identity.verify()
        result = self._executor.run(
            ProcessRequest(
                executable=plan.go_executable,
                identity=plan.process_identity,
                arguments=arguments,
                cwd=plan.directory,
                environment=plan.environment,
                timeout_seconds=plan.limits.timeout_seconds,
                output_limit=plan.limits.output_bytes,
            )
        )
        manager_identity.verify()
        plan.process_identity.verify()
        if result.started < 1:
            raise GoV1Error(
                CODE_WORKER_IDENTITY_INVALID,
                "worker process executor did not account for its start attempt",
            )
        self._started += result.started
        return replace(result, started=self._started)

    def _required_manager_identity(self) -> _ManagerIdentity:
        if self._manager_identity is None:
            raise GoV1Error(
                CODE_WORKER_PROTOCOL_INVALID,
                "worker session has no accepted manager identity",
            )
        return self._manager_identity

    def _required_plan(self) -> _WorkerPlan:
        if self._plan is None:
            raise GoV1Error(
                CODE_WORKER_PROTOCOL_INVALID,
                "worker session has no accepted request",
            )
        return self._plan


def run_worker(
    input_stream: BinaryIO | None = None,
    output_stream: BinaryIO | None = None,
    *,
    executor: ProcessExecutor | None = None,
    worker_executable: Path | None = None,
    identity_resolver: Callable[[Path], _ManagerIdentity] | None = None,
    control_observer: Callable[
        [str, Sequence[ControlProbe], ResourceLimits, Sequence[BinaryIO]],
        tuple[str, ...],
    ]
    | None = None,
    runtime_proof: Mapping[str, object] | None = None,
    _launch_context: _WorkerLaunchContext | None = None,
) -> int:
    """Run the fixed hidden worker session and return its process exit code."""

    launch_context = (
        _consume_worker_launch_context()
        if _launch_context is None
        else _launch_context
    )
    startup = (
        worker_runtime_proof(launch_context)
        if runtime_proof is None
        else runtime_proof
    )
    actual_input = (
        cast(BinaryIO, sys.stdin.buffer)
        if input_stream is None
        else input_stream
    )
    actual_output = (
        cast(BinaryIO, sys.stdout.buffer)
        if output_stream is None
        else output_stream
    )
    executable = worker_executable
    if executable is None:
        executable = _manager_executable_from_argv0(sys.argv[0])
    session = _WorkerSession(
        actual_input,
        actual_output,
        executor=executor or SubprocessProcessExecutor(),
        worker_executable=executable,
        identity_resolver=identity_resolver or _resolve_worker_manager_identity,
        control_observer=control_observer or _observe_native_controls,
        runtime_proof=startup,
        launch_context=launch_context,
    )
    try:
        session.run()
    except BaseException as exc:
        failure = (
            exc
            if isinstance(exc, GoV1Error)
            else GoV1Error(
                CODE_WORKER_PROTOCOL_INVALID,
                "worker session failed closed",
            )
        )
        try:
            _write_message(
                actual_output,
                {
                    "kind": "failure",
                    "nonce": session.nonce,
                    "failure": {
                        "code": failure.code,
                        "detail": failure.detail,
                    },
                },
            )
        except BaseException:
            pass
        return 3
    return 0


def _plan_from_request(
    request: Mapping[str, object],
) -> tuple[_WorkerPlan, str, _ManagerIdentity]:
    _require_exact_keys(
        request,
        {
            "version",
            "secret",
            "identity",
            "process_identity",
            "go_executable",
            "goroot",
            "tool_directory",
            "worker_cache",
            "directory",
            "environment",
            "list_argv",
            "build_argv",
            "artifact_path",
            "readonly_roots",
            "private_roots",
            "platform",
            "probes",
            "limits",
        },
        "worker request",
    )
    if request.get("version") != _PROTOCOL_VERSION:
        raise GoV1Error(
            CODE_WORKER_PROTOCOL_INVALID,
            "worker request has an unknown protocol version",
        )
    secret = _require_string(request.get("secret"), "session secret")
    if len(secret) != _SESSION_TOKEN_LENGTH:
        raise GoV1Error(
            CODE_WORKER_PROTOCOL_INVALID,
            "session secret is malformed",
        )
    try:
        bytes.fromhex(secret)
    except ValueError as exc:
        raise GoV1Error(
            CODE_WORKER_PROTOCOL_INVALID,
            "session secret is malformed",
        ) from exc
    identity = _manager_identity_from_mapping(
        _require_mapping(
            request.get("identity"),
            "expected worker identity",
        )
    )
    process_identity = _tool_process_identity_from_mapping(
        _require_mapping(
            request.get("process_identity"),
            "expected Go process identity",
        )
    )
    raw_environment = _require_mapping(
        request.get("environment"),
        "worker environment",
    )
    environment = {
        _require_string(key, "environment name"): _require_string(
            value,
            f"environment value for {key!r}",
        )
        for key, value in raw_environment.items()
    }
    probes = tuple(
        _probe_from_mapping(_require_mapping(item, "native control probe"))
        for item in _require_list(request.get("probes"), "native control probes")
    )
    limits = _limits_from_mapping(
        _require_mapping(request.get("limits"), "resource limits")
    )
    return (
        _WorkerPlan(
            executable=identity,
            process_identity=process_identity,
            go_executable=Path(
                _require_string(request.get("go_executable"), "Go executable")
            ),
            goroot=Path(_require_string(request.get("goroot"), "GOROOT")),
            tool_directory=Path(
                _require_string(request.get("tool_directory"), "Go tool directory")
            ),
            worker_cache=Path(
                _require_string(
                    request.get("worker_cache"),
                    "worker bytecode cache",
                )
            ),
            directory=Path(
                _require_string(request.get("directory"), "worker directory")
            ),
            environment=MappingProxyType(environment),
            list_argv=tuple(
                _string_list(request.get("list_argv"), "go list argv")
            ),
            build_argv=tuple(
                _string_list(request.get("build_argv"), "go build argv")
            ),
            artifact_path=Path(
                _require_string(request.get("artifact_path"), "artifact path")
            ),
            readonly_roots=tuple(
                Path(item)
                for item in _string_list(
                    request.get("readonly_roots"),
                    "readonly roots",
                )
            ),
            private_roots=tuple(
                Path(item)
                for item in _string_list(
                    request.get("private_roots"),
                    "private roots",
                )
            ),
            platform=_require_string(request.get("platform"), "worker platform"),
            probes=probes,
            limits=limits,
        ),
        secret,
        identity,
    )


def _plan_request_mapping(
    plan: _WorkerPlan,
    secret: str,
) -> dict[str, object]:
    return {
        "version": _PROTOCOL_VERSION,
        "secret": secret,
        "identity": plan.executable.to_dict(),
        "process_identity": plan.process_identity.to_dict(),
        "go_executable": os.fspath(plan.go_executable),
        "goroot": os.fspath(plan.goroot),
        "tool_directory": os.fspath(plan.tool_directory),
        "worker_cache": os.fspath(plan.worker_cache),
        "directory": os.fspath(plan.directory),
        "environment": dict(plan.environment),
        "list_argv": list(plan.list_argv),
        "build_argv": list(plan.build_argv),
        "artifact_path": os.fspath(plan.artifact_path),
        "readonly_roots": [os.fspath(root) for root in plan.readonly_roots],
        "private_roots": [os.fspath(root) for root in plan.private_roots],
        "platform": plan.platform,
        "probes": [probe.to_dict() for probe in plan.probes],
        "limits": _limits_to_mapping(plan.limits),
    }


def _probe_from_mapping(value: Mapping[str, object]) -> ControlProbe:
    _require_exact_keys(
        value,
        {"name", "availability", "mechanism", "probed_at"},
        "native control probe",
    )
    return ControlProbe(
        name=_require_string(value.get("name"), "native control name"),
        availability=_require_string(
            value.get("availability"),
            "native control availability",
        ),
        mechanism=_require_string(
            value.get("mechanism"),
            "native control mechanism",
        ),
        probed_at=_require_string(
            value.get("probed_at"),
            "native control probe timing",
        ),
    )


def _limits_to_mapping(limits: ResourceLimits) -> dict[str, object]:
    return {
        "timeout_milliseconds": round(limits.timeout_seconds * 1000),
        "output_bytes": limits.output_bytes,
        "artifact_bytes": limits.artifact_bytes,
        "file_bytes": limits.file_bytes,
        "disk_bytes": limits.disk_bytes,
        "memory_bytes": limits.memory_bytes,
        "processes": limits.processes,
    }


def _limits_from_mapping(value: Mapping[str, object]) -> ResourceLimits:
    _require_exact_keys(
        value,
        {
            "timeout_milliseconds",
            "output_bytes",
            "artifact_bytes",
            "file_bytes",
            "disk_bytes",
            "memory_bytes",
            "processes",
        },
        "resource limits",
    )
    milliseconds = _require_int(
        value.get("timeout_milliseconds"),
        "timeout milliseconds",
    )
    return ResourceLimits(
        timeout_seconds=milliseconds / 1000,
        output_bytes=_require_int(value.get("output_bytes"), "output limit"),
        artifact_bytes=_require_int(
            value.get("artifact_bytes"),
            "artifact limit",
        ),
        file_bytes=_require_int(value.get("file_bytes"), "file limit"),
        disk_bytes=_require_int(value.get("disk_bytes"), "disk limit"),
        memory_bytes=_require_int(value.get("memory_bytes"), "memory limit"),
        processes=_require_int(value.get("processes"), "process limit"),
    )


_FILE_LIMIT_LOCK: Final = threading.Lock()


def probe_native_controls(
    limits: ResourceLimits,
    *,
    _platform: str | None = None,
    _native_probe: Callable[[str, ResourceLimits], bool] | None = None,
) -> tuple[str, tuple[ControlProbe, ...]]:
    """Probe each platform-available inventory control once before launch."""

    normalized = _normalize_limits(limits)
    selected_platform = (
        inventory_platform() if _platform is None else _platform
    )
    records = _NATIVE_CONTROL_PLATFORMS.get(selected_platform)
    if records is None:
        raise GoV1Error(
            CODE_CONTROL_UNAVAILABLE,
            "the native-control inventory has no record for this host",
        )
    probe = _native_probe or _probe_native_control
    result: list[ControlProbe] = []
    for name in NATIVE_CONTROL_INVENTORY:
        record = records[name]
        # Every inventory control is measured on this host for this operation,
        # including the ones the inventory expects to be unavailable.  A cached,
        # inherited, configured, or host-label result is never a probe.
        try:
            measured = probe(name, normalized)
        except BaseException as exc:
            raise GoV1Error(
                CODE_CONTROL_UNAVAILABLE,
                f"cannot probe native control {name!r}",
            ) from exc
        if measured and record.availability == AVAILABILITY_UNAVAILABLE:
            raise GoV1Error(
                CODE_CAPABILITY_EVIDENCE_INVALID,
                f"probed native control {name!r} contradicts the inventory",
            )
        if not measured:
            if record.availability == AVAILABILITY_AVAILABLE:
                raise GoV1Error(
                    CODE_CONTROL_UNAVAILABLE,
                    f"native control {name!r} is unavailable on this host",
                )
            result.append(
                ControlProbe(
                    name=name,
                    availability=AVAILABILITY_UNAVAILABLE,
                    mechanism="",
                )
            )
            continue
        result.append(
            ControlProbe(
                name=name,
                availability=AVAILABILITY_AVAILABLE,
                mechanism=record.mechanism,
            )
        )
    probes = tuple(result)
    _validate_probe_set(selected_platform, probes)
    return selected_platform, probes


def _validate_probe_set(
    platform: str,
    probes: Sequence[ControlProbe],
) -> None:
    records = _NATIVE_CONTROL_PLATFORMS.get(platform)
    if records is None or len(probes) != len(NATIVE_CONTROL_INVENTORY):
        raise GoV1Error(
            CODE_CAPABILITY_EVIDENCE_INVALID,
            "native-control probe set is not the exhaustive inventory",
        )
    for index, probe in enumerate(probes):
        expected_name = NATIVE_CONTROL_INVENTORY[index]
        record = records[expected_name]
        if (
            probe.name != expected_name
            or probe.probed_at != PROBE_TIMING
            or probe.availability != record.availability
            or (
                probe.availability == AVAILABILITY_AVAILABLE
                and probe.mechanism != record.mechanism
            )
            or (
                probe.availability == AVAILABILITY_UNAVAILABLE
                and probe.mechanism
            )
        ):
            raise GoV1Error(
                CODE_CAPABILITY_EVIDENCE_INVALID,
                f"native-control probe {index} contradicts the inventory",
            )


def _probe_native_control(name: str, limits: ResourceLimits) -> bool:
    platform = inventory_platform()
    if platform == PLATFORM_MACOS:
        return _probe_macos_control(name, limits)
    return _probe_windows_control(name, limits)


_MACOS_AGGREGATE_FACILITY: Final[Mapping[str, tuple[str, str]]] = MappingProxyType(
    {
        CONTROL_ACTIVE_PROCESS_COUNT_LIMIT: (
            "kern.maxprocperuid",
            "kern.procpergroup_max",
        ),
        CONTROL_AGGREGATE_MEMORY_LIMIT: (
            "hw.memsize",
            "kern.memorystatus_pergroup_limit",
        ),
    }
)


def _macos_sysctl_present(name: str) -> bool:
    """Ask this host whether it implements one named control facility."""

    libc = ctypes.CDLL(None, use_errno=True)
    query = libc.sysctlbyname
    query.argtypes = [
        ctypes.c_char_p,
        ctypes.c_void_p,
        ctypes.POINTER(ctypes.c_size_t),
        ctypes.c_void_p,
        ctypes.c_size_t,
    ]
    query.restype = ctypes.c_int
    length = ctypes.c_size_t(0)
    ctypes.set_errno(0)
    if query(name.encode("ascii"), None, ctypes.byref(length), None, 0) == 0:
        return True
    code = ctypes.get_errno()
    if code in {errno.ENOENT, errno.EINVAL}:
        return False
    raise OSError(code, os.strerror(code), name)


def _probe_macos_aggregate_domain(name: str) -> bool:
    """Measure whether this host offers a private aggregate worker domain.

    The manager owns a descendant domain (a private session and process group),
    but bounding the aggregate process count or memory of that domain needs a
    domain-scoped kernel facility.  This probe reads a bound the host does
    implement, so that an absent answer is a measured absence rather than a
    broken probe, and then asks the host for the domain-scoped facility itself.
    Darwin answers that no such name exists, which is the measured
    ``no-private-aggregate-domain`` result; a host that grew the facility would
    measure available here.
    """

    host_scoped, domain_scoped = _MACOS_AGGREGATE_FACILITY[name]
    if not _macos_sysctl_present(host_scoped):
        raise OSError(
            errno.ENODEV,
            f"host does not answer for {host_scoped}",
            host_scoped,
        )
    os.killpg(os.getpgid(0), 0)
    return _macos_sysctl_present(domain_scoped)


def _probe_macos_control(name: str, limits: ResourceLimits) -> bool:
    if name in _MACOS_AGGREGATE_FACILITY:
        return _probe_macos_aggregate_domain(name)
    if name == CONTROL_DESCENDANT_DOMAIN_TERMINATION:
        group = os.getpgid(0)
        os.killpg(group, 0)
        return True
    if name == CONTROL_PER_FILE_SIZE_LIMIT:
        import resource

        wanted = _wanted_file_limit(limits)
        with _FILE_LIMIT_LOCK:
            previous = resource.getrlimit(resource.RLIMIT_FSIZE)
            try:
                resource.setrlimit(
                    resource.RLIMIT_FSIZE,
                    (wanted, previous[1]),
                )
                applied = resource.getrlimit(resource.RLIMIT_FSIZE)
            finally:
                resource.setrlimit(resource.RLIMIT_FSIZE, previous)
        return applied[0] == wanted
    if name == CONTROL_INHERITED_HANDLE_RESTRICTION:
        read_fd, write_fd = os.pipe()
        try:
            os.set_inheritable(read_fd, False)
            return not os.get_inheritable(read_fd)
        finally:
            os.close(read_fd)
            os.close(write_fd)
    raise ValueError(f"no macOS mechanism for inventory control {name!r}")


def _wanted_file_limit(limits: ResourceLimits) -> int:
    import resource

    _, hard = resource.getrlimit(resource.RLIMIT_FSIZE)
    if hard == resource.RLIM_INFINITY:
        return limits.file_bytes
    return min(limits.file_bytes, hard)


class _JobBasicLimitInformation(ctypes.Structure):
    _fields_ = [
        ("per_process_user_time_limit", ctypes.c_longlong),
        ("per_job_user_time_limit", ctypes.c_longlong),
        ("limit_flags", ctypes.c_uint32),
        ("minimum_working_set_size", ctypes.c_size_t),
        ("maximum_working_set_size", ctypes.c_size_t),
        ("active_process_limit", ctypes.c_uint32),
        ("affinity", ctypes.c_size_t),
        ("priority_class", ctypes.c_uint32),
        ("scheduling_class", ctypes.c_uint32),
    ]


class _IoCounters(ctypes.Structure):
    _fields_ = [
        ("read_operation_count", ctypes.c_ulonglong),
        ("write_operation_count", ctypes.c_ulonglong),
        ("other_operation_count", ctypes.c_ulonglong),
        ("read_transfer_count", ctypes.c_ulonglong),
        ("write_transfer_count", ctypes.c_ulonglong),
        ("other_transfer_count", ctypes.c_ulonglong),
    ]


class _JobExtendedLimitInformation(ctypes.Structure):
    _fields_ = [
        ("basic_limit_information", _JobBasicLimitInformation),
        ("io_info", _IoCounters),
        ("process_memory_limit", ctypes.c_size_t),
        ("job_memory_limit", ctypes.c_size_t),
        ("peak_process_memory_used", ctypes.c_size_t),
        ("peak_job_memory_used", ctypes.c_size_t),
    ]


class _JobBasicAccountingInformation(ctypes.Structure):
    _fields_ = [
        ("total_user_time", ctypes.c_longlong),
        ("total_kernel_time", ctypes.c_longlong),
        ("this_period_total_user_time", ctypes.c_longlong),
        ("this_period_total_kernel_time", ctypes.c_longlong),
        ("total_page_fault_count", ctypes.c_uint32),
        ("total_processes", ctypes.c_uint32),
        ("active_processes", ctypes.c_uint32),
        ("total_terminated_processes", ctypes.c_uint32),
    ]


_JOB_OBJECT_LIMIT_ACTIVE_PROCESS: Final = 0x00000008
_JOB_OBJECT_LIMIT_PROCESS_MEMORY: Final = 0x00000100
_JOB_OBJECT_LIMIT_JOB_MEMORY: Final = 0x00000200
_JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE: Final = 0x00002000
_JOB_OBJECT_BASIC_ACCOUNTING_INFORMATION: Final = 1
_JOB_OBJECT_EXTENDED_LIMIT_INFORMATION: Final = 9
_CREATE_SUSPENDED: Final = 0x00000004
_TH32CS_SNAPTHREAD: Final = 0x00000004
_THREAD_SUSPEND_RESUME: Final = 0x0002
_INVALID_HANDLE_VALUE: Final = ctypes.c_void_p(-1).value
_ERROR_INSUFFICIENT_BUFFER: Final = 122


class _ThreadEntry32(ctypes.Structure):
    _fields_ = [
        ("size", ctypes.c_uint32),
        ("usage", ctypes.c_uint32),
        ("thread_id", ctypes.c_uint32),
        ("owner_process_id", ctypes.c_uint32),
        ("base_priority", ctypes.c_long),
        ("delta_priority", ctypes.c_long),
        ("flags", ctypes.c_uint32),
    ]


def _windows_kernel32() -> Any:
    return ctypes.WinDLL("kernel32", use_last_error=True)  # type: ignore[attr-defined]


def _windows_job_flags_for_control(name: str) -> int:
    if name == CONTROL_DESCENDANT_DOMAIN_TERMINATION:
        return _JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
    if name == CONTROL_ACTIVE_PROCESS_COUNT_LIMIT:
        return _JOB_OBJECT_LIMIT_ACTIVE_PROCESS
    if name == CONTROL_AGGREGATE_MEMORY_LIMIT:
        return (
            _JOB_OBJECT_LIMIT_PROCESS_MEMORY
            | _JOB_OBJECT_LIMIT_JOB_MEMORY
        )
    raise ValueError(f"no Windows Job Object flag for {name!r}")


def _create_windows_job(limits: ResourceLimits, flags: int) -> int:
    kernel32 = _windows_kernel32()
    create_job = kernel32.CreateJobObjectW
    create_job.argtypes = [ctypes.c_void_p, ctypes.c_wchar_p]
    create_job.restype = ctypes.c_void_p
    set_information = kernel32.SetInformationJobObject
    set_information.argtypes = [
        ctypes.c_void_p,
        ctypes.c_int,
        ctypes.c_void_p,
        ctypes.c_uint32,
    ]
    set_information.restype = ctypes.c_int
    handle_value = create_job(None, None)
    if not handle_value:
        raise ctypes.WinError(ctypes.get_last_error())  # type: ignore[attr-defined]
    handle = int(handle_value)
    information = _JobExtendedLimitInformation()
    information.basic_limit_information.limit_flags = flags
    if flags & _JOB_OBJECT_LIMIT_ACTIVE_PROCESS:
        information.basic_limit_information.active_process_limit = limits.processes
    if flags & _JOB_OBJECT_LIMIT_PROCESS_MEMORY:
        information.process_memory_limit = limits.memory_bytes
    if flags & _JOB_OBJECT_LIMIT_JOB_MEMORY:
        information.job_memory_limit = limits.memory_bytes
    if not set_information(
        handle,
        _JOB_OBJECT_EXTENDED_LIMIT_INFORMATION,
        ctypes.byref(information),
        ctypes.sizeof(information),
    ):
        error = ctypes.get_last_error()  # type: ignore[attr-defined]
        _close_windows_handle(handle)
        raise ctypes.WinError(error)  # type: ignore[attr-defined]
    return handle


def _close_windows_handle(handle: int) -> None:
    if not handle:
        return
    kernel32 = _windows_kernel32()
    close_handle = kernel32.CloseHandle
    close_handle.argtypes = [ctypes.c_void_p]
    close_handle.restype = ctypes.c_int
    if not close_handle(handle):
        raise ctypes.WinError(ctypes.get_last_error())  # type: ignore[attr-defined]


def _terminate_windows_job(handle: int, timeout: float) -> None:
    kernel32 = _windows_kernel32()
    terminate = kernel32.TerminateJobObject
    terminate.argtypes = [ctypes.c_void_p, ctypes.c_uint32]
    terminate.restype = ctypes.c_int
    query = kernel32.QueryInformationJobObject
    query.argtypes = [
        ctypes.c_void_p,
        ctypes.c_int,
        ctypes.c_void_p,
        ctypes.c_uint32,
        ctypes.c_void_p,
    ]
    query.restype = ctypes.c_int
    if not terminate(handle, 3):
        raise ctypes.WinError(ctypes.get_last_error())  # type: ignore[attr-defined]
    deadline = time.monotonic() + timeout
    while True:
        accounting = _JobBasicAccountingInformation()
        if not query(
            handle,
            _JOB_OBJECT_BASIC_ACCOUNTING_INFORMATION,
            ctypes.byref(accounting),
            ctypes.sizeof(accounting),
            None,
        ):
            raise ctypes.WinError(ctypes.get_last_error())  # type: ignore[attr-defined]
        if accounting.active_processes == 0:
            return
        if time.monotonic() >= deadline:
            raise TimeoutError("Windows worker job did not become empty")
        time.sleep(0.005)


def _probe_windows_handle_list() -> bool:
    kernel32 = _windows_kernel32()
    initialize = kernel32.InitializeProcThreadAttributeList
    initialize.argtypes = [
        ctypes.c_void_p,
        ctypes.c_uint32,
        ctypes.c_uint32,
        ctypes.POINTER(ctypes.c_size_t),
    ]
    initialize.restype = ctypes.c_int
    delete = kernel32.DeleteProcThreadAttributeList
    delete.argtypes = [ctypes.c_void_p]
    size = ctypes.c_size_t()
    initialize(None, 1, 0, ctypes.byref(size))
    if (
        ctypes.get_last_error() != _ERROR_INSUFFICIENT_BUFFER  # type: ignore[attr-defined]
        or size.value == 0
    ):
        return False
    storage = ctypes.create_string_buffer(size.value)
    if not initialize(ctypes.byref(storage), 1, 0, ctypes.byref(size)):
        raise ctypes.WinError(ctypes.get_last_error())  # type: ignore[attr-defined]
    delete(ctypes.byref(storage))
    return True


def _probe_windows_per_file_size(limits: ResourceLimits) -> bool:
    """Measure whether this host offers an enforceable per-file size bound.

    The mechanism a per-file size limit needs is the POSIX ``RLIMIT_FSIZE``
    facility.  The probe first creates and releases a real private worker job,
    so that an absent answer is a measured absence rather than a broken probe,
    and then asks the platform C runtime for the ``setrlimit`` entry point.
    Windows answers that the entry point does not exist, which is the measured
    ``no-private-aggregate-domain`` result.
    """

    handle = _create_windows_job(
        limits,
        _windows_job_flags_for_control(CONTROL_DESCENDANT_DOMAIN_TERMINATION),
    )
    _close_windows_handle(handle)
    runtime = ctypes.CDLL("ucrtbase", use_errno=True)
    try:
        getattr(runtime, "setrlimit")
    except AttributeError:
        return False
    return True


def _probe_windows_control(name: str, limits: ResourceLimits) -> bool:
    if name in {
        CONTROL_DESCENDANT_DOMAIN_TERMINATION,
        CONTROL_ACTIVE_PROCESS_COUNT_LIMIT,
        CONTROL_AGGREGATE_MEMORY_LIMIT,
    }:
        handle = _create_windows_job(
            limits,
            _windows_job_flags_for_control(name),
        )
        _close_windows_handle(handle)
        return True
    if name == CONTROL_INHERITED_HANDLE_RESTRICTION:
        return _probe_windows_handle_list()
    if name == CONTROL_PER_FILE_SIZE_LIMIT:
        return _probe_windows_per_file_size(limits)
    raise ValueError(f"no Windows mechanism for inventory control {name!r}")


class _NativeControlDomain:
    def __init__(
        self,
        platform: str,
        probes: Sequence[ControlProbe],
        limits: ResourceLimits,
    ):
        _validate_probe_set(platform, probes)
        self.platform = platform
        self.controls = tuple(
            probe.name
            for probe in probes
            if probe.availability == AVAILABILITY_AVAILABLE
        )
        self.limits = limits
        self.installed = False
        self.terminated = False
        self.job_handle = 0
        self.file_limit = 0
        self.launch_secret = b""
        if platform == PLATFORM_MACOS:
            if CONTROL_PER_FILE_SIZE_LIMIT in self.controls:
                self.file_limit = _wanted_file_limit(limits)
        elif platform == PLATFORM_WINDOWS:
            flags = 0
            for name in self.controls:
                if name == CONTROL_INHERITED_HANDLE_RESTRICTION:
                    continue
                flags |= _windows_job_flags_for_control(name)
            if flags:
                try:
                    self.job_handle = _create_windows_job(limits, flags)
                except BaseException as exc:
                    raise GoV1Error(
                        CODE_CONTROL_UNAVAILABLE,
                        "cannot create the private Windows worker job",
                    ) from exc
        else:
            raise GoV1Error(
                CODE_CONTROL_UNAVAILABLE,
                "native control domain is unsupported on this host",
            )

    def launch(
        self,
        identity: _ManagerIdentity,
        worker_cache: Path,
    ) -> subprocess.Popen[bytes]:
        _verify_empty_worker_cache(worker_cache)
        environment = _indispensable_worker_environment(
            self.platform,
            worker_cache,
            identity.startup.site_root,
            identity.startup.python_home,
        )
        argv = worker_argv(identity)
        kwargs: dict[str, object] = {
            "cwd": identity.path.parent,
            "env": environment,
            "stdin": subprocess.PIPE,
            "stdout": subprocess.PIPE,
            "stderr": subprocess.PIPE,
            "close_fds": True,
            "shell": False,
        }
        capability: _PreparedWorkerLaunch | None = None
        try:
            with _WORKER_LAUNCH_LOCK:
                capability = _PreparedWorkerLaunch(self.platform)
                capability.add_popen_options(kwargs)
                try:
                    if self.platform == PLATFORM_MACOS:
                        kwargs["start_new_session"] = (
                            CONTROL_DESCENDANT_DOMAIN_TERMINATION
                            in self.controls
                        )
                        process = self._launch_macos(argv, kwargs)
                    else:
                        kwargs["creationflags"] = _CREATE_SUSPENDED
                        process = subprocess.Popen(
                            argv,
                            **cast(Any, kwargs),
                        )
                        self._attach_and_resume_windows(process)
                finally:
                    capability.close_parent_copy()
        except GoV1Error:
            self.close()
            raise
        except BaseException as exc:
            self.close()
            raise GoV1Error(
                CODE_WORKER_IDENTITY_INVALID,
                "cannot launch the identity-verified manager worker",
            ) from exc
        if capability is None:
            raise GoV1Error(
                CODE_WORKER_IDENTITY_INVALID,
                "worker launch did not create an authentication capability",
            )
        self.launch_secret = capability.secret
        self.installed = True
        return process

    def _launch_macos(
        self,
        argv: Sequence[str],
        kwargs: Mapping[str, object],
    ) -> subprocess.Popen[bytes]:
        if not self.file_limit:
            return subprocess.Popen(
                tuple(argv),
                **cast(Any, dict(kwargs)),
            )
        import resource

        with _FILE_LIMIT_LOCK:
            previous = resource.getrlimit(resource.RLIMIT_FSIZE)
            try:
                resource.setrlimit(
                    resource.RLIMIT_FSIZE,
                    (self.file_limit, previous[1]),
                )
                process = subprocess.Popen(
                    tuple(argv),
                    **cast(Any, dict(kwargs)),
                )
            except BaseException:
                resource.setrlimit(resource.RLIMIT_FSIZE, previous)
                raise
            try:
                resource.setrlimit(resource.RLIMIT_FSIZE, previous)
            except BaseException as exc:
                process.kill()
                process.wait()
                raise GoV1Error(
                    CODE_CONTROL_UNAVAILABLE,
                    "cannot restore manager RLIMIT_FSIZE after worker launch",
                ) from exc
        return process

    def _attach_and_resume_windows(
        self,
        process: subprocess.Popen[bytes],
    ) -> None:
        try:
            kernel32 = _windows_kernel32()
            process_handle = int(process._handle)  # type: ignore[attr-defined]
            if self.job_handle:
                assign = kernel32.AssignProcessToJobObject
                assign.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
                assign.restype = ctypes.c_int
                if not assign(self.job_handle, process_handle):
                    raise ctypes.WinError(  # type: ignore[attr-defined]
                        ctypes.get_last_error()  # type: ignore[attr-defined]
                    )
            _resume_windows_process(process.pid)
        except BaseException as exc:
            process.kill()
            process.wait()
            raise GoV1Error(
                CODE_CONTROL_UNAVAILABLE,
                "cannot install the Windows control domain before worker execution",
            ) from exc

    def installed_controls(self) -> tuple[str, ...]:
        return self.controls if self.installed else ()

    def terminate(
        self,
        process: subprocess.Popen[bytes] | None,
    ) -> None:
        failure: BaseException | None = None
        if self.platform == PLATFORM_WINDOWS and self.job_handle:
            try:
                _terminate_windows_job(
                    self.job_handle,
                    _WORKER_SHUTDOWN_GRACE,
                )
                _close_windows_handle(self.job_handle)
            except BaseException as exc:
                failure = exc
            else:
                self.job_handle = 0
        elif (
            self.platform == PLATFORM_MACOS
            and process is not None
            and process.pid > 0
        ):
            group_absent = False
            try:
                already_exited = process.poll() is not None
            except BaseException as exc:
                failure = exc
                already_exited = False
            if failure is None and already_exited:
                try:
                    os.killpg(process.pid, 0)
                except ProcessLookupError:
                    group_absent = True
                except OSError as exc:
                    failure = exc
            if failure is None and not group_absent:
                try:
                    os.killpg(process.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                except OSError as exc:
                    failure = exc
        if process is not None:
            try:
                process.wait(timeout=_WORKER_SHUTDOWN_GRACE)
            except subprocess.TimeoutExpired as exc:
                if failure is None:
                    failure = exc
                try:
                    process.kill()
                except OSError as kill_error:
                    if failure is None:
                        failure = kill_error
                try:
                    process.wait(timeout=_WORKER_SHUTDOWN_GRACE)
                except BaseException as wait_error:
                    if failure is None:
                        failure = wait_error
            except BaseException as exc:
                if failure is None:
                    failure = exc
        if (
            failure is None
            and self.platform == PLATFORM_MACOS
            and process is not None
            and process.pid > 0
        ):
            deadline = time.monotonic() + _WORKER_SHUTDOWN_GRACE
            while True:
                try:
                    os.killpg(process.pid, 0)
                except ProcessLookupError:
                    break
                except OSError as exc:
                    failure = exc
                    break
                if time.monotonic() >= deadline:
                    failure = TimeoutError(
                        "macOS worker process group did not become empty"
                    )
                    break
                time.sleep(0.005)
        if failure is not None:
            if (
                self.platform == PLATFORM_MACOS
                and process is not None
                and process.returncode is None
            ):
                try:
                    process.kill()
                    process.wait(timeout=_WORKER_SHUTDOWN_GRACE)
                except BaseException as cleanup_error:
                    failure.add_note(
                        f"direct worker cleanup also failed: {cleanup_error}"
                    )
            raise GoV1Error(
                CODE_CONTROL_UNAVAILABLE,
                "cannot prove complete worker-domain termination and join",
            ) from failure
        self.terminated = True

    def close(self) -> None:
        self.launch_secret = b""
        if self.job_handle:
            try:
                _close_windows_handle(self.job_handle)
            except BaseException as exc:
                raise GoV1Error(
                    CODE_CONTROL_UNAVAILABLE,
                    "cannot release the private Windows worker job",
                ) from exc
            self.job_handle = 0


def _resume_windows_process(pid: int) -> None:
    kernel32 = _windows_kernel32()
    snapshot_fn = kernel32.CreateToolhelp32Snapshot
    snapshot_fn.argtypes = [ctypes.c_uint32, ctypes.c_uint32]
    snapshot_fn.restype = ctypes.c_void_p
    first = kernel32.Thread32First
    first.argtypes = [ctypes.c_void_p, ctypes.POINTER(_ThreadEntry32)]
    first.restype = ctypes.c_int
    following = kernel32.Thread32Next
    following.argtypes = [ctypes.c_void_p, ctypes.POINTER(_ThreadEntry32)]
    following.restype = ctypes.c_int
    open_thread = kernel32.OpenThread
    open_thread.argtypes = [ctypes.c_uint32, ctypes.c_int, ctypes.c_uint32]
    open_thread.restype = ctypes.c_void_p
    resume = kernel32.ResumeThread
    resume.argtypes = [ctypes.c_void_p]
    resume.restype = ctypes.c_uint32
    snapshot_value = snapshot_fn(_TH32CS_SNAPTHREAD, 0)
    if snapshot_value == _INVALID_HANDLE_VALUE:
        raise ctypes.WinError(ctypes.get_last_error())  # type: ignore[attr-defined]
    snapshot = int(snapshot_value)
    resumed = 0
    try:
        entry = _ThreadEntry32()
        entry.size = ctypes.sizeof(entry)
        more = bool(first(snapshot, ctypes.byref(entry)))
        while more:
            if entry.owner_process_id == pid:
                thread_value = open_thread(
                    _THREAD_SUSPEND_RESUME,
                    False,
                    entry.thread_id,
                )
                if not thread_value:
                    raise ctypes.WinError(  # type: ignore[attr-defined]
                        ctypes.get_last_error()  # type: ignore[attr-defined]
                    )
                thread = int(thread_value)
                try:
                    previous = resume(thread)
                    if previous == 0xFFFFFFFF:
                        raise ctypes.WinError(  # type: ignore[attr-defined]
                            ctypes.get_last_error()  # type: ignore[attr-defined]
                        )
                    resumed += 1
                finally:
                    _close_windows_handle(thread)
            more = bool(following(snapshot, ctypes.byref(entry)))
    finally:
        _close_windows_handle(snapshot)
    if resumed == 0:
        raise OSError("suspended worker has no resumable thread")


def worker_argv(identity: _ManagerIdentity) -> tuple[str, ...]:
    """Return the one fixed hidden-worker argument vector.

    The program is the installed manager launcher, executed by its own bound
    interpreter with the fixed manager-owned isolation flags.  Nothing in a
    package file, manifest, descriptor, environment value, ``PATH`` lookup,
    shell, or user option contributes to this vector.
    """

    interpreter = (
        identity.interpreter.runtime.base_executable.path
        if _interpreter_runtime_platform(identity.interpreter.runtime)
        == PLATFORM_WINDOWS
        else identity.interpreter.invocation_path
    )
    return (
        os.fspath(interpreter),
        *WORKER_LAUNCH_FLAGS,
        os.fspath(identity.launcher.path),
        WORKER_MODE,
    )


def _indispensable_worker_environment(
    platform: str,
    worker_cache: Path,
    site_root: Path,
    python_home: Path,
) -> dict[str, str]:
    result = {
        "PYTHONDONTWRITEBYTECODE": "1",
        "PYTHONNOUSERSITE": "1",
        "PYTHONHOME": os.fspath(python_home),
        "PYTHONPATH": os.fspath(site_root),
        "PYTHONPYCACHEPREFIX": os.fspath(worker_cache),
    }
    if platform == PLATFORM_MACOS:
        return result
    # CPython's Windows pathlib startup requires USERPROFILE even when the
    # worker never reads user configuration.  Bind it to the manager-owned
    # empty cache instead of inheriting an operator value.
    result["USERPROFILE"] = os.fspath(worker_cache)
    result.update(
        {
            name: value
            for name in ("SYSTEMROOT", "WINDIR")
            if (value := os.environ.get(name))
        }
    )
    return result


def _verify_empty_worker_cache(worker_cache: Path) -> None:
    try:
        cache_info = worker_cache.lstat()
        cache_entries = list(worker_cache.iterdir())
    except OSError as exc:
        raise GoV1Error(
            CODE_CONTROL_UNAVAILABLE,
            "worker bytecode cache is unavailable",
        ) from exc
    if (
        not worker_cache.is_absolute()
        or _is_link_or_reparse(cache_info)
        or not stat.S_ISDIR(cache_info.st_mode)
        or cache_entries
    ):
        raise GoV1Error(
            CODE_CONTROL_UNAVAILABLE,
            "worker bytecode cache is not a private empty directory",
        )


def _observe_native_controls(
    platform: str,
    probes: Sequence[ControlProbe],
    limits: ResourceLimits,
    protocol_streams: Sequence[BinaryIO],
) -> tuple[str, ...]:
    _validate_probe_set(platform, probes)
    applied: list[str] = []
    for probe in probes:
        if probe.availability == AVAILABILITY_UNAVAILABLE:
            continue
        name = probe.name
        if platform == PLATFORM_MACOS:
            if name == CONTROL_DESCENDANT_DOMAIN_TERMINATION:
                if os.getpgid(0) != os.getpid():
                    raise GoV1Error(
                        CODE_CAPABILITY_EVIDENCE_INVALID,
                        "worker is not leader of its private process group",
                    )
            elif name == CONTROL_PER_FILE_SIZE_LIMIT:
                import resource

                if (
                    resource.getrlimit(resource.RLIMIT_FSIZE)[0]
                    != _wanted_file_limit(limits)
                ):
                    raise GoV1Error(
                        CODE_CAPABILITY_EVIDENCE_INVALID,
                        "worker did not inherit the probed file-size limit",
                    )
            elif name == CONTROL_INHERITED_HANDLE_RESTRICTION:
                _release_protocol_descriptors(protocol_streams)
            else:
                raise GoV1Error(
                    CODE_CAPABILITY_EVIDENCE_INVALID,
                    f"no macOS observation for {name!r}",
                )
        else:
            _observe_windows_control(name, limits, protocol_streams)
        applied.append(name)
    return tuple(applied)


_WORKER_RUNTIME_FLAGS: Final[tuple[str, ...]] = (
    "no_site",
    "no_user_site",
    "safe_path",
    "dont_write_bytecode",
)
_WORKER_RUNTIME_MODULES: Final[tuple[str, ...]] = (
    "csk",
    "csk.cli",
    "csk.builds.go_v1",
)


class _DlInfo(ctypes.Structure):
    _fields_ = [
        ("filename", ctypes.c_char_p),
        ("base", ctypes.c_void_p),
        ("symbol_name", ctypes.c_char_p),
        ("symbol_address", ctypes.c_void_p),
    ]


def _canonical_loaded_image(path: Path, label: str) -> Path:
    if not path.is_absolute():
        raise GoV1Error(
            CODE_WORKER_IDENTITY_INVALID,
            f"{label} is not absolute",
        )
    try:
        return path.resolve(strict=True)
    except (OSError, RuntimeError) as exc:
        raise GoV1Error(
            CODE_WORKER_IDENTITY_INVALID,
            f"{label} cannot be canonicalized",
        ) from exc


def _macos_loaded_process_image() -> Path:
    process = ctypes.CDLL(None)
    image_count = process._dyld_image_count
    image_count.argtypes = []
    image_count.restype = ctypes.c_uint32
    image_name = process._dyld_get_image_name
    image_name.argtypes = [ctypes.c_uint32]
    image_name.restype = ctypes.c_char_p
    if image_count() < 1:
        raise GoV1Error(
            CODE_WORKER_IDENTITY_INVALID,
            "worker process image is unavailable",
        )
    raw = image_name(0)
    if not raw:
        raise GoV1Error(
            CODE_WORKER_IDENTITY_INVALID,
            "worker process image is unavailable",
        )
    return _canonical_loaded_image(
        Path(os.fsdecode(raw)),
        "worker process image",
    )


def _macos_loaded_python_runtime_image() -> Path:
    process = ctypes.CDLL(None)
    lookup = process.dladdr
    lookup.argtypes = [ctypes.c_void_p, ctypes.POINTER(_DlInfo)]
    lookup.restype = ctypes.c_int
    info = _DlInfo()
    address = ctypes.cast(ctypes.pythonapi.Py_GetVersion, ctypes.c_void_p)
    if not lookup(address, ctypes.byref(info)) or not info.filename:
        raise GoV1Error(
            CODE_WORKER_IDENTITY_INVALID,
            "worker Python runtime image is unavailable",
        )
    return _canonical_loaded_image(
        Path(os.fsdecode(info.filename)),
        "worker Python runtime image",
    )


def _windows_loaded_image(handle: int, label: str) -> Path:
    kernel32 = _windows_kernel32()
    get_name = kernel32.GetModuleFileNameW
    get_name.argtypes = [
        ctypes.c_void_p,
        ctypes.c_wchar_p,
        ctypes.c_uint32,
    ]
    get_name.restype = ctypes.c_uint32
    capacity = 512
    while capacity <= 32_768:
        buffer = ctypes.create_unicode_buffer(capacity)
        length = int(get_name(handle or None, buffer, capacity))
        if not length:
            raise GoV1Error(
                CODE_WORKER_IDENTITY_INVALID,
                f"{label} is unavailable",
            )
        if length < capacity - 1:
            return _canonical_loaded_image(Path(buffer.value), label)
        capacity *= 2
    raise GoV1Error(
        CODE_WORKER_IDENTITY_INVALID,
        f"{label} path is unbounded",
    )


def _worker_native_runtime_proof() -> dict[str, str]:
    platform = inventory_platform()
    if platform == PLATFORM_MACOS:
        process_image = _macos_loaded_process_image()
        runtime_image = _macos_loaded_python_runtime_image()
    else:
        process_image = _windows_loaded_image(0, "worker process image")
        runtime_image = _windows_loaded_image(
            int(ctypes.pythonapi._handle),
            "worker Python runtime image",
        )
    return {
        "process_image": os.fspath(process_image),
        "runtime_image": os.fspath(runtime_image),
    }


def worker_runtime_proof(
    launch_context: _WorkerLaunchContext,
) -> dict[str, object]:
    """Snapshot the complete startup and import surface of this worker."""

    modules: list[dict[str, object]] = []
    for name in sorted(sys.modules):
        module = sys.modules.get(name)
        if module is None:
            continue
        origin = getattr(module, "__file__", None)
        if not isinstance(origin, str) or not origin:
            continue
        modules.append({"name": name, "path": os.path.abspath(origin)})
    return {
        "executable": os.path.abspath(sys.executable),
        "argv0": os.fspath(_manager_executable_from_argv0(sys.argv[0])),
        "launch": launch_context.public_dict(),
        "flags": {
            name: int(getattr(sys.flags, name, 0))
            for name in _WORKER_RUNTIME_FLAGS
        },
        "native": _worker_native_runtime_proof(),
        "path": [entry for entry in sys.path],
        "modules": modules,
    }


def validate_worker_runtime(
    runtime: Mapping[str, object],
    identity: _ManagerIdentity,
    *,
    expected_launch_context: _WorkerLaunchContext | None = None,
    expected_parent_pid: int | None = None,
) -> None:
    """Prove the worker's whole startup TCB is the bound manager TCB."""

    _require_exact_keys(
        runtime,
        {
            "executable",
            "argv0",
            "launch",
            "flags",
            "native",
            "path",
            "modules",
        },
        "worker runtime proof",
    )
    launch = _require_mapping(
        runtime.get("launch"),
        "worker launch proof",
    )
    _require_exact_keys(
        launch,
        {"parent_pid", "transport"},
        "worker launch proof",
    )
    parent_pid = _require_int(
        launch.get("parent_pid"),
        "worker launch parent pid",
    )
    transport = _require_string(
        launch.get("transport"),
        "worker launch transport",
    )
    if (
        parent_pid <= 1
        or transport != _WORKER_LAUNCH_TRANSPORT
        or (
            expected_parent_pid is not None
            and parent_pid != expected_parent_pid
        )
        or (
            expected_launch_context is not None
            and launch != expected_launch_context.public_dict()
        )
    ):
        raise GoV1Error(
            CODE_WORKER_IDENTITY_INVALID,
            "worker launch proof is not the manager-owned capability",
        )
    flags = _require_mapping(runtime.get("flags"), "worker interpreter flags")
    _require_exact_keys(
        flags,
        set(_WORKER_RUNTIME_FLAGS),
        "worker interpreter flags",
    )
    for name in _WORKER_RUNTIME_FLAGS:
        if not _require_int(flags.get(name), f"worker {name} flag"):
            raise GoV1Error(
                CODE_WORKER_IDENTITY_INVALID,
                f"worker did not run with the fixed {name} isolation flag",
            )
    native = _require_mapping(
        runtime.get("native"),
        "worker native runtime proof",
    )
    _require_exact_keys(
        native,
        {"process_image", "runtime_image"},
        "worker native runtime proof",
    )
    process_image = Path(
        _require_string(
            native.get("process_image"),
            "worker process image",
        )
    )
    runtime_image = Path(
        _require_string(
            native.get("runtime_image"),
            "worker Python runtime image",
        )
    )
    if (
        process_image != identity.interpreter.runtime.process_image.path
        or runtime_image != identity.interpreter.runtime.runtime_image.path
    ):
        raise GoV1Error(
            CODE_WORKER_IDENTITY_INVALID,
            "worker loaded an interpreter image outside the bound identity",
        )
    executable = Path(
        _require_string(runtime.get("executable"), "worker interpreter")
    )
    if executable not in {
        identity.interpreter.invocation_path,
        identity.interpreter.executable.path,
        identity.interpreter.runtime.base_executable.path,
    }:
        raise GoV1Error(
            CODE_WORKER_IDENTITY_INVALID,
            "worker ran under an interpreter outside the bound identity",
        )
    if Path(_require_string(runtime.get("argv0"), "worker argv0")) != (
        identity.launcher.path
    ):
        raise GoV1Error(
            CODE_WORKER_IDENTITY_INVALID,
            "worker entry point is not the installed manager launcher",
        )
    startup = identity.startup
    for entry in _string_list(runtime.get("path"), "worker import path"):
        if not _permitted_worker_import_root(Path(entry), identity):
            raise GoV1Error(
                CODE_WORKER_IDENTITY_INVALID,
                f"worker import path escapes the bound manager TCB: {entry}",
            )
    raw_modules = _require_list(runtime.get("modules"), "worker modules")
    if not raw_modules or len(raw_modules) > _MAX_WORKER_RUNTIME_MODULES:
        raise GoV1Error(
            CODE_WORKER_PROTOCOL_INVALID,
            "worker module proof is not bounded",
        )
    package_entries = {
        entry.path for entry in identity.package_tree.entries
    }
    runtime_files = {
        runtime_tree.path.joinpath(*entry.path.split("/"))
        for runtime_tree in startup.runtime_trees
        for entry in runtime_tree.entries
        if entry.kind == "file"
    }
    names: list[str] = []
    for raw in raw_modules:
        module = _require_mapping(raw, "worker module")
        _require_exact_keys(module, {"name", "path"}, "worker module")
        names.append(_require_string(module.get("name"), "worker module name"))
        origin = Path(_require_string(module.get("path"), "worker module path"))
        if origin == identity.launcher.path:
            # ``__main__`` is the bound installed manager launcher itself.
            continue
        try:
            launcher_relative = origin.relative_to(identity.launcher.path)
        except ValueError:
            launcher_relative = None
        if (
            launcher_relative is not None
            and launcher_relative.as_posix() == "__main__.py"
        ):
            # Windows console entry points are a bound PE launcher with one
            # appended ``__main__.py`` zip member.  The fixed interpreter
            # executes that exact bound member under the isolation flags.
            continue
        if _same_or_below(origin, identity.package_tree.path):
            # The manager-owned package tree is bound entry by entry, so a file
            # inserted beside it cannot pass as manager code.
            relative = origin.relative_to(identity.package_tree.path).as_posix()
            if relative in package_entries:
                continue
        elif origin in runtime_files:
            continue
        elif any(
            _same_or_below(origin, archive.path)
            for archive in startup.archives
        ):
            continue
        raise GoV1Error(
            CODE_WORKER_IDENTITY_INVALID,
            f"worker loaded code outside the bound manager TCB: {origin}",
        )
    missing = [name for name in _WORKER_RUNTIME_MODULES if name not in names]
    if missing or len(names) != len(set(names)):
        raise GoV1Error(
            CODE_WORKER_IDENTITY_INVALID,
            "worker module proof is not the fixed manager module set",
        )


def _permitted_worker_import_root(
    entry: Path,
    identity: _ManagerIdentity,
) -> bool:
    startup = identity.startup
    if not os.fspath(entry) or not entry.is_absolute():
        return False
    if entry == identity.launcher.path:
        return True
    if entry == startup.site_root or any(
        entry == runtime_tree.path
        or _same_or_below(entry, runtime_tree.path)
        for runtime_tree in startup.runtime_trees
    ):
        return True
    return entry in startup.archive_slots or any(
        entry == archive.path for archive in startup.archives
    )


def _release_protocol_descriptors(streams: Sequence[BinaryIO]) -> None:
    for stream in streams:
        try:
            descriptor = stream.fileno()
        except (AttributeError, OSError):
            continue
        try:
            os.set_inheritable(descriptor, False)
            if os.get_inheritable(descriptor):
                raise OSError("protocol descriptor remains inheritable")
        except OSError as exc:
            raise GoV1Error(
                CODE_CAPABILITY_EVIDENCE_INVALID,
                "worker cannot close protocol descriptors across exec",
            ) from exc


def _observe_windows_control(
    name: str,
    limits: ResourceLimits,
    protocol_streams: Sequence[BinaryIO],
) -> None:
    if name == CONTROL_INHERITED_HANDLE_RESTRICTION:
        _release_protocol_descriptors(protocol_streams)
        return
    kernel32 = _windows_kernel32()
    current = kernel32.GetCurrentProcess
    current.restype = ctypes.c_void_p
    in_job = ctypes.c_int()
    is_in_job = kernel32.IsProcessInJob
    is_in_job.argtypes = [
        ctypes.c_void_p,
        ctypes.c_void_p,
        ctypes.POINTER(ctypes.c_int),
    ]
    is_in_job.restype = ctypes.c_int
    if not is_in_job(current(), None, ctypes.byref(in_job)) or not in_job.value:
        raise GoV1Error(
            CODE_CAPABILITY_EVIDENCE_INVALID,
            "worker is not inside the manager-owned Job Object",
        )
    query = kernel32.QueryInformationJobObject
    query.argtypes = [
        ctypes.c_void_p,
        ctypes.c_int,
        ctypes.c_void_p,
        ctypes.c_uint32,
        ctypes.c_void_p,
    ]
    query.restype = ctypes.c_int
    information = _JobExtendedLimitInformation()
    if not query(
        None,
        _JOB_OBJECT_EXTENDED_LIMIT_INFORMATION,
        ctypes.byref(information),
        ctypes.sizeof(information),
        None,
    ):
        raise GoV1Error(
            CODE_CAPABILITY_EVIDENCE_INVALID,
            "worker cannot inspect its Job Object limits",
        )
    flags = information.basic_limit_information.limit_flags
    wanted = _windows_job_flags_for_control(name)
    if flags & wanted != wanted:
        raise GoV1Error(
            CODE_CAPABILITY_EVIDENCE_INVALID,
            f"worker Job Object lacks {name!r}",
        )
    if (
        name == CONTROL_ACTIVE_PROCESS_COUNT_LIMIT
        and information.basic_limit_information.active_process_limit
        != limits.processes
    ):
        raise GoV1Error(
            CODE_CAPABILITY_EVIDENCE_INVALID,
            "worker Job Object has the wrong process limit",
        )
    if name == CONTROL_AGGREGATE_MEMORY_LIMIT and (
        information.process_memory_limit != limits.memory_bytes
        or information.job_memory_limit != limits.memory_bytes
    ):
        raise GoV1Error(
            CODE_CAPABILITY_EVIDENCE_INVALID,
            "worker Job Object has the wrong memory limits",
        )


# A later resource, termination, or join failure must never rewrite the public
# diagnostic of an execution-boundary violation that was already established.
_FAILURE_PRECEDENCE: Final[tuple[str, ...]] = (
    CODE_WORKER_IDENTITY_INVALID,
    CODE_HARDENED_CLAIM_FORBIDDEN,
    CODE_PACKAGE_INFLUENCE_FORBIDDEN,
    CODE_CAPABILITY_EVIDENCE_INVALID,
    CODE_WORKER_PROTOCOL_INVALID,
    CODE_CONTROL_UNAVAILABLE,
)


def _failure_rank(error: BaseException) -> int:
    if isinstance(error, GoV1Error) and error.code in _FAILURE_PRECEDENCE:
        return _FAILURE_PRECEDENCE.index(error.code)
    if isinstance(error, GoV1Error):
        return len(_FAILURE_PRECEDENCE)
    return len(_FAILURE_PRECEDENCE) + 1


def _dominant_failure(
    established: BaseException | None,
    candidate: BaseException,
) -> BaseException:
    """Keep the strongest diagnostic and record the weaker one as a note."""

    if established is None:
        return candidate
    if _failure_rank(candidate) < _failure_rank(established):
        candidate.add_note(str(established))
        return candidate
    established.add_note(str(candidate))
    return established


class _WorkerClient:
    def __init__(
        self,
        plan: _WorkerPlan,
        process: subprocess.Popen[bytes],
        domain: _NativeControlDomain,
        identity_guard: _IdentityMutationGuard,
        nonce: str,
        secret: str,
        launch_secret: bytes,
        evidence: CapabilityEvidence,
        deadline: float,
    ):
        self.plan = plan
        self.process = process
        self.domain = domain
        self.identity_guard = identity_guard
        self.nonce = nonce
        self.secret = secret
        self.launch_secret = launch_secret
        self.evidence = evidence
        self.deadline = deadline
        self.finished = False
        self._expired = threading.Event()
        self._expiration_error: BaseException | None = None
        self._lifecycle_lock = threading.Lock()
        self.stderr = bytearray()
        self.stderr_overflow = threading.Event()
        stderr_pipe = process.stderr
        if stderr_pipe is None:
            raise GoV1Error(
                CODE_WORKER_PROTOCOL_INVALID,
                "worker diagnostic pipe is unavailable",
            )
        self._stderr_thread = threading.Thread(
            target=self._drain_stderr,
            args=(stderr_pipe,),
            daemon=True,
        )
        self._stderr_thread.start()
        self._deadline_timer = threading.Timer(
            max(0.0, deadline - time.monotonic()),
            self._expire_worker_domain,
        )
        self._deadline_timer.daemon = True
        self._deadline_timer.start()

    @classmethod
    def launch(
        cls,
        plan: _WorkerPlan,
    ) -> _WorkerClient:
        nonce = secrets.token_hex(32)
        secret = secrets.token_hex(32)
        plan.executable.verify()
        plan.process_identity.verify()
        identity_guard = _IdentityMutationGuard(
            plan.platform,
            (
                *plan.executable.watch_paths(),
                *plan.process_identity.watch_paths(),
                plan.worker_cache,
            ),
        )
        plan.executable.verify()
        plan.process_identity.verify()
        domain: _NativeControlDomain | None = None
        process: subprocess.Popen[bytes] | None = None
        client: _WorkerClient | None = None
        try:
            domain = _NativeControlDomain(
                plan.platform,
                plan.probes,
                plan.limits,
            )
            process = domain.launch(plan.executable, plan.worker_cache)
            identity_guard.verify()
            launch_secret = domain.launch_secret
            if len(launch_secret) != _WORKER_LAUNCH_SECRET_BYTES:
                raise GoV1Error(
                    CODE_WORKER_IDENTITY_INVALID,
                    "worker launch capability was not retained by the manager",
                )
            installed = domain.installed_controls()
            evidence = evidence_from_applied(
                plan.platform,
                plan.probes,
                installed,
            )
            validate_capability_evidence(evidence, plan.platform, plan.probes)
            client = cls(
                plan,
                process,
                domain,
                identity_guard,
                nonce,
                secret,
                launch_secret,
                evidence,
                time.monotonic()
                + plan.limits.timeout_seconds
                + _WORKER_SHUTDOWN_GRACE,
            )
            request = _plan_request_mapping(plan, secret)
            client._send(
                {
                    "kind": "request",
                    "nonce": nonce,
                    "manager_auth": _launch_authenticator(
                        launch_secret,
                        _WORKER_LAUNCH_REQUEST_DOMAIN,
                        nonce,
                        request,
                    ),
                    "request": request,
                }
            )
            ready_message = client._receive("ready")
            _require_exact_keys(
                ready_message,
                {"kind", "nonce", "ready"},
                "worker ready message",
            )
            ready = _require_mapping(
                ready_message.get("ready"),
                "worker ready proof",
            )
            _require_exact_keys(
                ready,
                {
                    "identity",
                    "runtime",
                    "applied",
                    "evidence",
                    "worker_auth",
                },
                "worker ready proof",
            )
            worker_auth = _require_string(
                ready.get("worker_auth"),
                "worker launch authenticator",
            )
            authenticated_ready = dict(ready)
            del authenticated_ready["worker_auth"]
            expected_worker_auth = _launch_authenticator(
                launch_secret,
                _WORKER_LAUNCH_READY_DOMAIN,
                nonce,
                authenticated_ready,
            )
            if not hmac.compare_digest(worker_auth, expected_worker_auth):
                raise GoV1Error(
                    CODE_WORKER_IDENTITY_INVALID,
                    "worker proof is not authenticated by the launch capability",
                )
            plan.executable.matches_mapping(
                _require_mapping(
                    ready.get("identity"),
                    "worker identity proof",
                )
            )
            validate_worker_runtime(
                _require_mapping(
                    ready.get("runtime"),
                    "worker runtime proof",
                ),
                plan.executable,
                expected_parent_pid=os.getpid(),
            )
            worker_evidence = capability_evidence_from_mapping(
                _require_mapping(
                    ready.get("evidence"),
                    "worker capability evidence",
                )
            )
            validate_capability_evidence(
                worker_evidence,
                plan.platform,
                plan.probes,
            )
            if worker_evidence != evidence:
                raise GoV1Error(
                    CODE_CAPABILITY_EVIDENCE_INVALID,
                    "worker evidence differs from manager-installed controls",
                )
            applied = tuple(
                _string_list(ready.get("applied"), "worker applied controls")
            )
            _match_applied_controls(applied, evidence)
            plan.executable.verify()
            plan.process_identity.verify()
            identity_guard.verify()
            return client
        except BaseException as primary:
            try:
                if client is not None:
                    client.teardown()
                else:
                    if domain is not None:
                        domain.terminate(process)
                        domain.close()
                    identity_guard.close()
            except BaseException as exc:
                # Teardown still has to complete, but it must not replace the
                # diagnostic that rejected this launch.
                primary.add_note(f"worker-domain teardown also failed: {exc}")
            raise

    def list(self) -> ProcessResult:
        self.identity_guard.verify()
        self._send({"kind": "list", "nonce": self.nonce})
        result = self._receive_result("list-result", expected_started=1)
        self.identity_guard.verify()
        return result

    def build(self) -> ProcessResult:
        self.identity_guard.verify()
        self._send(
            {
                "kind": "permit",
                "nonce": self.nonce,
                "permit": _build_permit(
                    self.secret,
                    self.nonce,
                    self.plan.build_argv,
                ),
            }
        )
        result = self._receive_result("build-result", expected_started=2)
        self.identity_guard.verify()
        return result

    def _receive_result(
        self,
        kind: str,
        *,
        expected_started: int,
    ) -> ProcessResult:
        message = self._receive(kind)
        _require_exact_keys(message, {"kind", "nonce", "result"}, f"{kind} message")
        result = _result_from_mapping(
            _require_mapping(message.get("result"), f"{kind} result")
        )
        _validate_started_count(result.started, expected_started)
        if len(result.stdout) + len(result.stderr) > self.plan.limits.output_bytes:
            raise GoV1Error(
                "process_output_limit",
                "worker result exceeds the manager output bound",
            )
        return result

    def _send(self, message: Mapping[str, object]) -> None:
        if self._expired.is_set() or time.monotonic() >= self.deadline:
            raise GoV1Error(
                "process_timeout",
                "worker domain exceeded its wall-clock deadline",
            )
        stdin_pipe = self.process.stdin
        if stdin_pipe is None:
            raise GoV1Error(
                CODE_WORKER_PROTOCOL_INVALID,
                "worker request pipe is unavailable",
            )
        _write_message(cast(BinaryIO, stdin_pipe), message)

    def _receive(self, expected_kind: str) -> Mapping[str, object]:
        stdout_pipe = self.process.stdout
        if stdout_pipe is None:
            raise GoV1Error(
                CODE_WORKER_PROTOCOL_INVALID,
                "worker response pipe is unavailable",
            )
        remaining = self.deadline - time.monotonic()
        if remaining <= 0:
            raise GoV1Error(
                "process_timeout",
                "worker domain exceeded its wall-clock deadline",
            )
        result_queue: queue.Queue[
            Mapping[str, object] | BaseException
        ] = queue.Queue(maxsize=1)

        def read() -> None:
            try:
                result_queue.put(
                    _read_message(cast(BinaryIO, stdout_pipe))
                )
            except BaseException as exc:
                result_queue.put(exc)

        reader = threading.Thread(target=read, daemon=True)
        reader.start()
        try:
            result = result_queue.get(timeout=remaining)
        except queue.Empty as exc:
            raise GoV1Error(
                "process_timeout",
                "worker domain exceeded its wall-clock deadline",
            ) from exc
        if isinstance(result, BaseException):
            if isinstance(result, GoV1Error):
                raise result
            raise GoV1Error(
                CODE_WORKER_PROTOCOL_INVALID,
                "cannot read worker response",
            ) from result
        kind = result.get("kind")
        if kind == "failure":
            _require_exact_keys(
                result,
                {"kind", "nonce", "failure"},
                "worker failure message",
            )
            failure = _require_mapping(
                result.get("failure"),
                "worker failure",
            )
            _require_exact_keys(
                failure,
                {"code", "detail"},
                "worker failure",
            )
            raise GoV1Error(
                _require_string(failure.get("code"), "worker failure code"),
                _require_string(
                    failure.get("detail"),
                    "worker failure detail",
                ),
            )
        if kind != expected_kind or result.get("nonce") != self.nonce:
            raise GoV1Error(
                CODE_WORKER_PROTOCOL_INVALID,
                f"worker sent {kind!r}; expected {expected_kind!r}",
            )
        return result

    def _drain_stderr(self, stream: BinaryIO) -> None:
        try:
            while True:
                chunk = stream.read(4096)
                if not chunk:
                    return
                remaining = _WORKER_STDERR_LIMIT - len(self.stderr)
                self.stderr.extend(chunk[:remaining])
                if len(chunk) > remaining:
                    self.stderr_overflow.set()
        except OSError:
            return

    def teardown(self) -> None:
        with self._lifecycle_lock:
            if self.finished:
                return
            self._deadline_timer.cancel()
            failure = self._expiration_error

            def record(error: BaseException) -> None:
                nonlocal failure
                failure = _dominant_failure(failure, error)

            stdin_pipe = self.process.stdin
            if stdin_pipe is not None:
                try:
                    _write_message(
                        cast(BinaryIO, stdin_pipe),
                        {"kind": "shutdown", "nonce": self.nonce},
                    )
                except BaseException:
                    pass
                try:
                    stdin_pipe.close()
                except OSError:
                    pass

            stdout_pipe = self.process.stdout
            drained = threading.Event()

            def drain_stdout() -> None:
                if stdout_pipe is not None:
                    try:
                        while stdout_pipe.read(16 * 1024):
                            pass
                    except OSError:
                        pass
                drained.set()

            drain_thread = threading.Thread(target=drain_stdout, daemon=True)
            drain_thread.start()
            drained.wait(_WORKER_SHUTDOWN_GRACE)
            try:
                self.domain.terminate(self.process)
            except BaseException as exc:
                record(exc)
            if stdout_pipe is not None:
                try:
                    stdout_pipe.close()
                except OSError:
                    pass
            stderr_pipe = self.process.stderr
            if stderr_pipe is not None:
                try:
                    stderr_pipe.close()
                except OSError:
                    pass
            self._stderr_thread.join(_WORKER_SHUTDOWN_GRACE)
            try:
                self.domain.close()
            except BaseException as exc:
                record(exc)
            try:
                self.plan.executable.verify()
                self.plan.process_identity.verify()
                _verify_empty_worker_cache(self.plan.worker_cache)
            except BaseException as exc:
                record(exc)
            try:
                self.identity_guard.close()
            except BaseException as exc:
                record(exc)
            if failure is not None:
                if isinstance(failure, GoV1Error):
                    raise failure
                raise GoV1Error(
                    CODE_CONTROL_UNAVAILABLE,
                    "worker-domain teardown did not complete",
                ) from failure
            self.finished = True

    def _expire_worker_domain(self) -> None:
        with self._lifecycle_lock:
            if self.finished:
                return
            self._expired.set()
            try:
                self.domain.terminate(self.process)
            except BaseException as exc:
                self._expiration_error = exc


def _match_applied_controls(
    applied: Sequence[str],
    evidence: CapabilityEvidence,
) -> None:
    if len(applied) != len(set(applied)):
        raise GoV1Error(
            CODE_CAPABILITY_EVIDENCE_INVALID,
            "worker reports a native control more than once",
        )
    reported = frozenset(applied)
    expected = frozenset(
        entry.name
        for entry in evidence.controls
        if entry.status == STATUS_APPLIED
    )
    if reported != expected:
        raise GoV1Error(
            CODE_CAPABILITY_EVIDENCE_INVALID,
            "worker applied-control report contradicts its evidence",
        )


def _validate_started_count(actual: int, expected: int) -> None:
    if actual != expected:
        raise GoV1Error(
            CODE_WORKER_IDENTITY_INVALID,
            f"worker started {actual} programs; expected exactly {expected}",
        )


def build(
    request: BuildRequest,
    *,
    _native_probe: Callable[[str, ResourceLimits], bool] | None = None,
    _state_observer: Callable[[str], None] | None = None,
    _manager_executable: Path | None = None,
) -> BuildResult:
    """Run one closed portable manager-worker-v1 compile operation."""

    _validate_package_command_surface(request)
    limits = _normalize_limits(request.limits)
    _verify_frozen_inputs(
        request.source_snapshot,
        request.toolchain_session,
        "before source-aware preflight",
    )
    source_root, build_root, source_dir = _canonical_build_directories(
        request.source_snapshot,
        request.build_root,
        request.source_dir,
    )
    _emit_state(_state_observer, SESSION_STATES[0])

    platform, probes = probe_native_controls(
        limits,
        _native_probe=_native_probe,
    )
    _emit_state(_state_observer, SESSION_STATES[1])

    executable_path = (
        _running_manager_executable()
        if _manager_executable is None
        else _manager_executable
    )
    identity = _resolve_manager_identity(executable_path)
    tool_directory = (
        request.toolchain_session.goroot
        / "pkg"
        / "tool"
        / (
            f"{request.toolchain_session.target.goos}_"
            f"{request.toolchain_session.target.goarch}"
        )
    )
    process_identity = _resolve_tool_process_identity(
        request.toolchain_session.executable,
        tool_directory,
    )
    _emit_state(_state_observer, SESSION_STATES[2])

    stage: Path | None = None
    worker_cache: Path | None = None
    client: _WorkerClient | None = None
    result: BuildResult | None = None
    operation_error: BaseException | None = None
    try:
        stage = Path(
            tempfile.mkdtemp(
                prefix=".csk-go-build-",
                dir=request.toolchain_session.operation_root,
            )
        )
        stage.chmod(0o700)
        worker_cache = Path(
            tempfile.mkdtemp(
                prefix=".csk-go-worker-pycache-",
                dir=request.toolchain_session.operation_root,
            )
        )
        worker_cache.chmod(0o700)
        bin_dir = stage / "bin"
        bin_dir.mkdir(mode=0o700)
        artifact_name = request.command + (
            ".exe" if platform == PLATFORM_WINDOWS else ""
        )
        artifact_path = bin_dir / artifact_name
        artifact_rel = f"bin/{artifact_name}"
        build_argv = (
            *BUILD_ARGUMENT_PREFIX,
            os.fspath(artifact_path),
            ".",
        )
        private_roots = (
            *_private_roots(
                request.toolchain_session.environment,
                stage,
            ),
            worker_cache,
        )
        plan = _WorkerPlan(
            executable=identity,
            process_identity=process_identity,
            go_executable=request.toolchain_session.executable,
            goroot=request.toolchain_session.goroot,
            tool_directory=tool_directory,
            worker_cache=worker_cache,
            directory=source_dir,
            environment=request.toolchain_session.environment,
            list_argv=LIST_ARGUMENTS,
            build_argv=build_argv,
            artifact_path=artifact_path,
            readonly_roots=(source_root, request.toolchain_session.goroot),
            private_roots=private_roots,
            platform=platform,
            probes=probes,
            limits=limits,
        )
        client = _WorkerClient.launch(plan)
        _emit_state(_state_observer, SESSION_STATES[3])
        _emit_state(_state_observer, SESSION_STATES[4])
        _emit_state(_state_observer, SESSION_STATES[5])

        list_result = client.list()
        _emit_state(_state_observer, SESSION_STATES[6])
        _check_process_result(list_result, phase="list")
        _verify_frozen_inputs(
            request.source_snapshot,
            request.toolchain_session,
            "during fixed go list",
        )
        _verify_private_state(
            request.toolchain_session.operation_root,
            limits.disk_bytes,
        )
        validate_package_graph(
            list_result.stdout,
            build_root=build_root,
            source_dir=source_dir,
            goroot=request.toolchain_session.goroot,
        )
        _emit_state(_state_observer, SESSION_STATES[7])

        _emit_state(_state_observer, SESSION_STATES[8])
        build_result = client.build()
        _emit_state(_state_observer, SESSION_STATES[9])
        _check_process_result(build_result, phase="build")
        _verify_frozen_inputs(
            request.source_snapshot,
            request.toolchain_session,
            "during fixed go build",
        )
        _verify_private_state(
            request.toolchain_session.operation_root,
            limits.disk_bytes,
        )

        artifact = _verify_artifact(
            stage,
            artifact_path,
            artifact_rel,
            limits.artifact_bytes,
            platform,
        )
        _emit_state(_state_observer, SESSION_STATES[10])
        _verify_frozen_inputs(
            request.source_snapshot,
            request.toolchain_session,
            "before artifact acceptance",
        )
        identity.verify()
        process_identity.verify()
        _emit_state(_state_observer, SESSION_STATES[11])
        result = BuildResult(
            artifact=BuildArtifact(
                staged_path=artifact_path,
                metadata=artifact,
            ),
            capability_evidence=client.evidence,
        )
    except BaseException as exc:
        operation_error = exc
    finally:
        teardown_error: BaseException | None = None
        if client is not None:
            try:
                client.teardown()
                _emit_state(_state_observer, SESSION_STATES[12])
            except BaseException as exc:
                teardown_error = exc
        if teardown_error is not None:
            operation_error = _dominant_failure(operation_error, teardown_error)
        if (operation_error is not None or result is None) and stage is not None:
            shutil.rmtree(stage, ignore_errors=True)
        if worker_cache is not None:
            shutil.rmtree(worker_cache, ignore_errors=True)
    if operation_error is not None:
        raise operation_error.with_traceback(operation_error.__traceback__)
    if result is None:
        raise GoV1Error(
            CODE_WORKER_PROTOCOL_INVALID,
            "worker operation produced no result",
        )
    return result


def _emit_state(
    observer: Callable[[str], None] | None,
    state: str,
) -> None:
    if observer is not None:
        observer(state)


def _running_manager_executable() -> Path:
    """Resolve only the actual process entry point, never PATH or environment."""

    return _manager_executable_from_argv0(sys.argv[0])


def _validate_package_command_surface(request: BuildRequest) -> None:
    if not isinstance(request.command_object, Mapping):
        raise GoV1Error(
            CODE_PACKAGE_INFLUENCE_FORBIDDEN,
            "the package build-command surface was not presented",
        )
    keys: list[str] = []
    for key in request.command_object:
        if not isinstance(key, str):
            raise GoV1Error(
                CODE_PACKAGE_INFLUENCE_FORBIDDEN,
                "package build command has a non-string field",
            )
        keys.append(key)
    extra = sorted(set(keys) - {"type", "driver", "source_dir"})
    if extra:
        surfaces = {
            "executable": "worker, Go launcher, or GOROOT tool program",
            "program": "worker, Go launcher, or GOROOT tool program",
            "argv": "Go list or build argument vector",
            "args": "Go list or build argument vector",
            "arguments": "Go list or build argument vector",
            "env": "worker or compiler environment value",
            "environment": "worker or compiler environment value",
            "output": "staging, artifact, shim, or install destination",
            "output_path": "staging, artifact, shim, or install destination",
            "flags": "compiler, linker, tag, or toolchain flag",
            "hooks": "pre-build, post-build, or lifecycle hook",
            "plugins": "compiler, linker, or manager plugin",
            "generators": "source generator, macro, or code-producing step",
        }
        surface = surfaces.get(
            extra[0],
            "execution boundary, worker, controls, limits, or publication",
        )
        raise GoV1Error(
            CODE_PACKAGE_INFLUENCE_FORBIDDEN,
            f"package field {extra[0]!r} selects the {surface}",
        )
    if set(keys) != {"type", "driver", "source_dir"}:
        raise GoV1Error(
            CODE_PACKAGE_INFLUENCE_FORBIDDEN,
            "package build command is not the exact closed surface",
        )
    if (
        request.command_object.get("type") != "build"
        or request.command_object.get("driver") != "go-v1"
        or request.command_object.get("source_dir") != request.source_dir
    ):
        raise GoV1Error(
            CODE_PACKAGE_INFLUENCE_FORBIDDEN,
            "package build command differs from the validated go-v1 command",
        )
    if not is_valid_identifier(request.command):
        raise GoV1Error(
            "invalid_build_request",
            "command name is not a portable output component",
        )


def _normalize_limits(limits: ResourceLimits) -> ResourceLimits:
    defaults = ResourceLimits()
    values = ResourceLimits(
        timeout_seconds=(
            defaults.timeout_seconds
            if limits.timeout_seconds == 0
            else limits.timeout_seconds
        ),
        output_bytes=(
            defaults.output_bytes
            if limits.output_bytes == 0
            else limits.output_bytes
        ),
        artifact_bytes=(
            defaults.artifact_bytes
            if limits.artifact_bytes == 0
            else limits.artifact_bytes
        ),
        file_bytes=(
            defaults.file_bytes
            if limits.file_bytes == 0
            else limits.file_bytes
        ),
        disk_bytes=(
            defaults.disk_bytes
            if limits.disk_bytes == 0
            else limits.disk_bytes
        ),
        memory_bytes=(
            defaults.memory_bytes
            if limits.memory_bytes == 0
            else limits.memory_bytes
        ),
        processes=(
            defaults.processes
            if limits.processes == 0
            else limits.processes
        ),
    )
    if (
        not 0.001 <= values.timeout_seconds <= defaults.timeout_seconds
        or not 1 <= values.output_bytes <= defaults.output_bytes
        or not 1 <= values.artifact_bytes <= defaults.artifact_bytes
        or not (
            values.artifact_bytes
            <= values.file_bytes
            <= defaults.file_bytes
        )
        or not (
            values.artifact_bytes
            <= values.disk_bytes
            <= defaults.disk_bytes
        )
        or not 1 <= values.memory_bytes <= defaults.memory_bytes
        or not 1 <= values.processes <= defaults.processes
    ):
        raise GoV1Error(
            "invalid_resource_limits",
            "go-v1 resource limits are outside manager bounds",
        )
    return values


def _canonical_build_directories(
    snapshot: source.FrozenSnapshot,
    build_root_value: str,
    source_dir_value: str,
) -> tuple[Path, Path, Path]:
    if (
        not is_valid_portable_path(build_root_value)
        or not is_valid_portable_path(source_dir_value)
    ):
        raise GoV1Error(
            "invalid_build_request",
            "build root and source directory must be portable relative paths",
        )
    try:
        source_root = snapshot.path.resolve(strict=True)
    except (OSError, RuntimeError) as exc:
        raise GoV1Error(
            "invalid_build_request",
            "frozen source root cannot be canonicalized",
        ) from exc
    build_root = source_root.joinpath(*build_root_value.split("/"))
    source_dir = source_root.joinpath(*source_dir_value.split("/"))
    for path, label in (
        (build_root, "build root"),
        (source_dir, "source directory"),
    ):
        try:
            value = path.lstat()
            resolved = path.resolve(strict=True)
        except (OSError, RuntimeError) as exc:
            raise GoV1Error(
                "invalid_build_request",
                f"{label} is unavailable",
            ) from exc
        if (
            _is_link_or_reparse(value)
            or not stat.S_ISDIR(value.st_mode)
            or resolved != path
        ):
            raise GoV1Error(
                "invalid_build_request",
                f"{label} is not canonical and link-free",
            )
    if not _same_or_below(source_dir, build_root):
        raise GoV1Error(
            "invalid_build_request",
            "source directory is outside its build root",
        )
    go_mod = build_root / "go.mod"
    try:
        go_mod_stat = go_mod.lstat()
    except OSError as exc:
        raise GoV1Error(
            "build_module_missing",
            "build root lacks go.mod",
        ) from exc
    if _is_link_or_reparse(go_mod_stat) or not stat.S_ISREG(go_mod_stat.st_mode):
        raise GoV1Error(
            "build_module_missing",
            "build root go.mod is not a regular file",
        )
    _reject_workspace_and_toolchain_directives(build_root, go_mod)
    return source_root, build_root, source_dir


def _reject_workspace_and_toolchain_directives(
    build_root: Path,
    go_mod: Path,
) -> None:
    try:
        for directory, names, files in os.walk(
            build_root,
            topdown=True,
            followlinks=False,
        ):
            for name in names:
                child = Path(directory) / name
                if _is_link_or_reparse(child.lstat()):
                    raise OSError("link inside frozen build root")
            if "go.work" in files:
                raise GoV1Error(
                    "workspace_dependency_forbidden",
                    "build root contains a forbidden go.work",
                )
        payload = go_mod.read_bytes()
    except GoV1Error:
        raise
    except OSError as exc:
        raise GoV1Error(
            "workspace_dependency_forbidden",
            "cannot prove workspace exclusion",
        ) from exc
    for line in payload.splitlines():
        fields = line.split()
        if fields and fields[0] == b"toolchain":
            raise GoV1Error(
                "toolchain_switch_forbidden",
                "go.mod contains a package-selected toolchain directive",
            )


def _verify_frozen_inputs(
    snapshot: source.FrozenSnapshot,
    session: toolchain.ToolchainSession,
    phase: str,
) -> None:
    try:
        snapshot.recheck()
        session.verify()
    except (source.BuildSourceError, toolchain.ToolchainError) as exc:
        raise GoV1Error(
            CODE_WORKER_IDENTITY_INVALID,
            f"frozen source or toolchain identity changed {phase}",
        ) from exc


def _private_roots(
    environment: Mapping[str, str],
    stage: Path,
) -> tuple[Path, ...]:
    names = (
        "GOPATH",
        "GOMODCACHE",
        "GOCACHE",
        "GOTMPDIR",
        "HOME",
        "XDG_CONFIG_HOME",
        "PATH",
        "TMPDIR",
        "APPDATA",
        "LOCALAPPDATA",
        "USERPROFILE",
        "TEMP",
        "TMP",
    )
    result: list[Path] = []
    seen: set[Path] = set()
    for name in names:
        value = environment.get(name)
        if not value:
            continue
        path = Path(value)
        if path not in seen:
            seen.add(path)
            result.append(path)
    if stage not in seen:
        result.append(stage)
    return tuple(result)


def _validate_fixed_build_argv(
    argv: Sequence[str],
    artifact_path: Path,
    private_roots: Sequence[Path],
) -> None:
    expected = (*BUILD_ARGUMENT_PREFIX, os.fspath(artifact_path), ".")
    if tuple(argv) != expected or not artifact_path.is_absolute():
        raise GoV1Error(
            CODE_WORKER_PROTOCOL_INVALID,
            "worker request carries a non-protocol go build vector",
        )
    if not any(_strictly_below(artifact_path, root) for root in private_roots):
        raise GoV1Error(
            CODE_WORKER_PROTOCOL_INVALID,
            "manager-derived output escapes private staging",
        )


def _validate_worker_environment(
    environment: Mapping[str, str],
    goroot: Path,
    private_roots: Sequence[Path],
    platform: str,
) -> None:
    required = {
        "GOENV": "off",
        "GOTOOLCHAIN": "local",
        "LC_ALL": "C",
        "LANG": "C",
        "GO111MODULE": "on",
        "GOFLAGS": "",
        "GOPROXY": "off",
        "GOSUMDB": "off",
        "GOPRIVATE": "",
        "GONOPROXY": "none",
        "GONOSUMDB": "none",
        "GOVCS": "*:off",
        "GOWORK": "off",
        "CGO_ENABLED": "0",
        "GO_EXTLINK_ENABLED": "0",
        "GOEXPERIMENT": "",
        "GOROOT": os.fspath(goroot),
    }
    for name, expected in required.items():
        if environment.get(name) != expected:
            raise GoV1Error(
                CODE_WORKER_PROTOCOL_INVALID,
                f"compiler environment has unexpected {name}",
            )
    expected_goos = "darwin" if platform == PLATFORM_MACOS else "windows"
    goarch = environment.get("GOARCH", "")
    if environment.get("GOOS") != expected_goos or not goarch:
        raise GoV1Error(
            CODE_WORKER_PROTOCOL_INVALID,
            "compiler environment does not carry the native target",
        )
    tuning_name = toolchain.TUNING_VARIABLES.get(goarch)
    if tuning_name is None or not environment.get(tuning_name):
        raise GoV1Error(
            CODE_WORKER_PROTOCOL_INVALID,
            "compiler environment lacks the exact native tuning variable",
        )

    private_names = {
        "GOPATH",
        "GOMODCACHE",
        "GOCACHE",
        "GOTMPDIR",
        "HOME",
        "XDG_CONFIG_HOME",
        "PATH",
        "TMPDIR",
    }
    platform_names: set[str] = set()
    optional_names: set[str] = set()
    if platform == PLATFORM_WINDOWS:
        platform_names = {
            "APPDATA",
            "LOCALAPPDATA",
            "USERPROFILE",
            "TEMP",
            "TMP",
        }
        optional_names = {"SYSTEMROOT", "WINDIR"}
    for name in private_names | platform_names:
        value = environment.get(name)
        if not value or Path(value) not in private_roots:
            raise GoV1Error(
                CODE_WORKER_PROTOCOL_INVALID,
                f"compiler environment {name} is not operation-private",
            )
        try:
            private_stat = Path(value).lstat()
        except OSError as exc:
            raise GoV1Error(
                CODE_WORKER_PROTOCOL_INVALID,
                f"compiler environment {name} is unavailable",
            ) from exc
        if _is_link_or_reparse(private_stat) or not stat.S_ISDIR(
            private_stat.st_mode
        ):
            raise GoV1Error(
                CODE_WORKER_PROTOCOL_INVALID,
                f"compiler environment {name} is not a real directory",
            )
    path_value = Path(environment["PATH"])
    try:
        if any(path_value.iterdir()):
            raise GoV1Error(
                CODE_WORKER_PROTOCOL_INVALID,
                "compiler PATH directory is not empty",
            )
    except OSError as exc:
        raise GoV1Error(
            CODE_WORKER_PROTOCOL_INVALID,
            "compiler PATH directory cannot be inspected",
        ) from exc

    allowed = (
        set(required)
        | {"GOOS", "GOARCH", tuning_name}
        | private_names
        | platform_names
        | optional_names
    )
    if not set(environment).issubset(allowed):
        extra = sorted(set(environment) - allowed)
        raise GoV1Error(
            CODE_WORKER_PROTOCOL_INVALID,
            f"compiler environment carries forbidden variables {extra!r}",
        )
    if any(
        "\x00" in name or "\x00" in value
        for name, value in environment.items()
    ):
        raise GoV1Error(
            CODE_WORKER_PROTOCOL_INVALID,
            "compiler environment contains NUL",
        )


def _verify_private_state(operation_root: Path, limit: int) -> None:
    total = 0
    try:
        for directory, names, files in os.walk(
            operation_root,
            topdown=True,
            followlinks=False,
        ):
            directory_path = Path(directory)
            for name in names:
                item = directory_path / name
                value = item.lstat()
                if _is_link_or_reparse(value) or not stat.S_ISDIR(value.st_mode):
                    raise GoV1Error(
                        "private_build_special_file",
                        "Go created a special path in private state",
                    )
            for name in files:
                item = directory_path / name
                value = item.lstat()
                if _is_link_or_reparse(value) or not stat.S_ISREG(value.st_mode):
                    raise GoV1Error(
                        "private_build_special_file",
                        "Go created a special file in private state",
                    )
                if value.st_size > limit - total:
                    raise GoV1Error(
                        "process_disk_limit",
                        "Go private state exceeded its disk bound",
                    )
                total += value.st_size
    except GoV1Error:
        raise
    except OSError as exc:
        raise GoV1Error(
            "private_build_unreadable",
            "cannot inspect operation-private Go state",
        ) from exc


def _check_process_result(result: ProcessResult, *, phase: str) -> None:
    if result.timed_out:
        raise GoV1Error(
            "process_timeout",
            f"fixed go {phase} exceeded its deadline",
        )
    if result.overflow:
        raise GoV1Error(
            "process_output_limit",
            f"fixed go {phase} exceeded its output bound",
        )
    if not result.start_failed and result.returncode == 0:
        return
    stderr = result.stderr.decode("utf-8", errors="replace")
    if phase == "list":
        code, detail = _classify_list_failure(stderr)
    else:
        code, detail = _classify_build_failure(stderr)
    raise GoV1Error(code, detail)


def _classify_list_failure(stderr: str) -> tuple[str, str]:
    if any(
        needle in stderr
        for needle in (
            "inconsistent vendoring",
            "not marked as explicit in vendor/modules.txt",
        )
    ):
        return (
            "vendor_metadata_inconsistent",
            "go list rejected inconsistent vendor metadata",
        )
    if any(
        needle in stderr
        for needle in (
            "cannot find module providing package",
            "import lookup disabled by -mod=vendor",
        )
    ):
        return (
            "vendor_dependency_missing",
            "go list could not resolve a vendored dependency",
        )
    if any(
        needle in stderr
        for needle in (
            "go.mod requires go >=",
            "requires go >=",
            "toolchain not available",
        )
    ):
        return (
            "toolchain_switch_forbidden",
            "module requests another toolchain",
        )
    if "go.work" in stderr or "workspace" in stderr:
        return (
            "workspace_dependency_forbidden",
            "module depends on a forbidden workspace",
        )
    if (
        "no Go files" in stderr
        or "build constraints exclude all Go files" in stderr
    ):
        return ("cgo_required", "package has no buildable non-cgo files")
    return ("go_list_failed", "fixed go list command failed")


def _classify_build_failure(stderr: str) -> tuple[str, str]:
    if (
        "requires external linking" in stderr
        or "external linking required" in stderr
    ):
        return (
            "external_link_forbidden",
            "internal-only Go build failed",
        )
    if "libgcc" in stderr or "gcc" in stderr:
        return (
            "libgcc_fallback_forbidden",
            "Go build attempted a forbidden host-linker fallback",
        )
    return ("go_build_failed", "fixed go build command failed")


def _verify_artifact(
    stage: Path,
    artifact_path: Path,
    artifact_rel: str,
    limit: int,
    platform: str,
) -> ArtifactMetadata:
    try:
        stage_entries = list(stage.iterdir())
        bin_entries = list((stage / "bin").iterdir())
    except OSError as exc:
        raise GoV1Error(
            "artifact_output_invalid",
            "build staging cannot be enumerated",
        ) from exc
    if stage_entries != [stage / "bin"] or bin_entries != [artifact_path]:
        raise GoV1Error(
            "artifact_output_invalid",
            "build did not produce exactly one manager-derived output",
        )
    try:
        initial = artifact_path.lstat()
    except OSError as exc:
        raise GoV1Error(
            "artifact_output_invalid",
            "manager-derived output is missing",
        ) from exc
    if (
        _is_link_or_reparse(initial)
        or not stat.S_ISREG(initial.st_mode)
        or getattr(initial, "st_nlink", 1) != 1
    ):
        raise GoV1Error(
            "artifact_special_file",
            "staged output is not a single-link regular file",
        )
    if initial.st_size < 0 or initial.st_size > limit:
        raise GoV1Error(
            "artifact_size_limit",
            "staged output exceeds its artifact bound",
        )
    try:
        artifact_path.chmod(0o700)
        permissioned = artifact_path.lstat()
    except OSError as exc:
        raise GoV1Error(
            "artifact_permissions_failed",
            "cannot apply manager artifact permissions",
        ) from exc
    if (
        (permissioned.st_dev, permissioned.st_ino)
        != (initial.st_dev, initial.st_ino)
        or permissioned.st_size != initial.st_size
        or not stat.S_ISREG(permissioned.st_mode)
    ):
        raise GoV1Error(
            "artifact_mutated",
            "staged output changed while applying permissions",
        )
    digest = hashlib.sha256()
    header = b""
    try:
        with artifact_path.open("rb", buffering=0) as handle:
            opened = os.fstat(handle.fileno())
            if (
                (opened.st_dev, opened.st_ino)
                != (initial.st_dev, initial.st_ino)
                or opened.st_size != initial.st_size
            ):
                raise GoV1Error(
                    "artifact_mutated",
                    "staged output changed while opening",
                )
            remaining = opened.st_size
            while remaining:
                chunk = handle.read(min(remaining, 1024 * 1024))
                if not chunk:
                    raise GoV1Error(
                        "artifact_mutated",
                        "staged output shrank while hashing",
                    )
                if not header:
                    header = chunk[:4]
                remaining -= len(chunk)
                digest.update(chunk)
            if handle.read(1):
                raise GoV1Error(
                    "artifact_mutated",
                    "staged output grew while hashing",
                )
    except GoV1Error:
        raise
    except OSError as exc:
        raise GoV1Error(
            "artifact_unreadable",
            "cannot hash staged output",
        ) from exc
    if not _native_executable_header(header, platform):
        raise GoV1Error(
            "artifact_output_invalid",
            "staged output is not a native executable",
        )
    after = artifact_path.lstat()
    if (
        (after.st_dev, after.st_ino) != (initial.st_dev, initial.st_ino)
        or after.st_size != initial.st_size
    ):
        raise GoV1Error(
            "artifact_mutated",
            "staged output changed during verification",
        )
    return ArtifactMetadata(
        path=artifact_rel,
        sha256="sha256:" + digest.hexdigest(),
        size=initial.st_size,
    )


def _native_executable_header(header: bytes, platform: str) -> bool:
    if platform == PLATFORM_WINDOWS:
        return header.startswith(b"MZ")
    if len(header) < 4:
        return False
    return header in {
        b"\xfe\xed\xfa\xce",
        b"\xfe\xed\xfa\xcf",
        b"\xce\xfa\xed\xfe",
        b"\xcf\xfa\xed\xfe",
        b"\xca\xfe\xba\xbe",
        b"\xbe\xba\xfe\xca",
        b"\xca\xfe\xba\xbf",
        b"\xbf\xba\xfe\xca",
    }


def _require_exact_keys(
    value: Mapping[str, object],
    expected: set[str],
    label: str,
) -> None:
    actual = set(value)
    if actual != expected:
        raise GoV1Error(
            CODE_WORKER_PROTOCOL_INVALID,
            f"{label} fields are not closed: got {sorted(actual)!r}, "
            f"want {sorted(expected)!r}",
        )


def _require_mapping(
    value: object,
    label: str,
) -> Mapping[str, object]:
    if not isinstance(value, Mapping) or not all(
        isinstance(key, str) for key in value
    ):
        raise GoV1Error(
            CODE_WORKER_PROTOCOL_INVALID,
            f"{label} must be an object with string fields",
        )
    return cast(Mapping[str, object], value)


def _optional_mapping(value: object) -> Mapping[str, object] | None:
    if value is None:
        return None
    return _require_mapping(value, "go list object")


def _require_list(value: object, label: str) -> list[object]:
    if not isinstance(value, list):
        raise GoV1Error(
            CODE_WORKER_PROTOCOL_INVALID,
            f"{label} must be a list",
        )
    return cast(list[object], value)


def _optional_list(value: object) -> list[object]:
    if value is None:
        return []
    return _require_list(value, "go list array")


def _require_string(value: object, label: str) -> str:
    if not isinstance(value, str):
        raise GoV1Error(
            CODE_WORKER_PROTOCOL_INVALID,
            f"{label} must be a string",
        )
    return value


def _optional_string(value: object) -> str:
    if value is None:
        return ""
    return _require_string(value, "go list string")


def _require_bool(value: object, label: str) -> bool:
    if not isinstance(value, bool):
        raise GoV1Error(
            CODE_WORKER_PROTOCOL_INVALID,
            f"{label} must be a boolean",
        )
    return value


def _optional_bool(value: object) -> bool:
    if value is None:
        return False
    return _require_bool(value, "go list boolean")


def _require_int(value: object, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise GoV1Error(
            CODE_WORKER_PROTOCOL_INVALID,
            f"{label} must be an integer",
        )
    return value


def _string_list(value: object, label: str) -> list[str]:
    if value is None:
        return []
    result = _require_list(value, label)
    return [_require_string(item, f"{label} item") for item in result]


__all__ = [
    "AVAILABILITY_AVAILABLE",
    "AVAILABILITY_UNAVAILABLE",
    "ArtifactMetadata",
    "BUILD_ARGUMENT_PREFIX",
    "BuildArtifact",
    "BuildRequest",
    "BuildResult",
    "CAPABILITY_EVIDENCE_VERSION",
    "CODE_CAPABILITY_EVIDENCE_INVALID",
    "CODE_CONTROL_UNAVAILABLE",
    "CODE_HARDENED_CLAIM_FORBIDDEN",
    "CODE_PACKAGE_INFLUENCE_FORBIDDEN",
    "CODE_WORKER_IDENTITY_INVALID",
    "CODE_WORKER_PROTOCOL_INVALID",
    "CapabilityEvidence",
    "CapabilityEvidenceEntry",
    "ControlProbe",
    "DEFERRED_HARDENED_GUARANTEES",
    "EXECUTION_POLICY",
    "GoV1Error",
    "LIST_ARGUMENTS",
    "MANDATORY_CONTROLS",
    "NATIVE_CONTROL_INVENTORY",
    "NATIVE_CONTROL_INVENTORY_VERSION",
    "PLATFORM_MACOS",
    "PLATFORM_WINDOWS",
    "PROBE_TIMING",
    "PROCESS_GRAPH",
    "ProcessExecutor",
    "ProcessRequest",
    "ProcessResult",
    "ResourceLimits",
    "SESSION_STATES",
    "STATUS_APPLIED",
    "STATUS_UNAVAILABLE",
    "SubprocessProcessExecutor",
    "WORKER_MODE",
    "build",
    "capability_evidence_from_mapping",
    "evidence_from_applied",
    "inventory_platform",
    "probe_native_controls",
    "run_worker",
    "validate_capability_evidence",
    "validate_package_graph",
]
