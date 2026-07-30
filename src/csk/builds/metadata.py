"""Portable logical build metadata for the local ``go-v1`` driver.

This module owns the protocol side of a compiled build: the logical build
input, its CCJ-1 cache key, and build receipt schema 1. Everything here is
portable. Manager-home paths, physical cache-root and driver-directory names,
receipt filenames, and lock names are implementation-specific and never appear
in a hashed input, a receipt, or a marker.
"""

from __future__ import annotations

import hashlib
from collections.abc import Mapping
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Any, Final, Literal

from .. import protocol_json
from ..identifiers import is_valid_identifier, is_valid_portable_path
from .source import BuildSourceIdentity
from .toolchain import (
    GO_RELPATH,
    TOOLCHAIN_ALGORITHM,
    TUNING_VARIABLES,
    NativeTarget,
    ToolchainError,
    ToolchainIdentity,
    parse_normalized_go_version,
)


BuildDriver = Literal["go-v1"]

GO_V1_DRIVER: Final[BuildDriver] = "go-v1"
SUPPORTED_BUILD_DRIVERS: Final[frozenset[str]] = frozenset({GO_V1_DRIVER})

BUILD_SOURCE_ALGORITHM: Final = "curator-build-source-v1"
BUILD_INPUT_SCHEMA_VERSION: Final = 1
RECEIPT_SCHEMA_VERSION: Final = 1
SUPPORTED_RECEIPT_SCHEMA_VERSIONS: Final[frozenset[int]] = frozenset({RECEIPT_SCHEMA_VERSION})

# The execution-policy identity is REQUIRED inside the hashed build input and
# is closed to this single portable value in protocol 1.0. It is never derived
# from a host label, a capability probe, or package data. Because it is hashed,
# an entry produced under another execution contract, or under a pre-revision
# input that carried no execution policy at all, misses instead of aliasing.
PORTABLE_EXECUTION_POLICY: Final = "manager-worker-v1"
SUPPORTED_EXECUTION_POLICIES: Final[frozenset[str]] = frozenset({PORTABLE_EXECUTION_POLICY})

# The eleven policy members a manager fixes for every go-v1 build. They are
# manager-chosen constants: no package, manifest, or host value selects them.
FIXED_GO_BUILD_POLICY: Final[Mapping[str, str | bool]] = MappingProxyType(
    {
        "module_mode": "vendor",
        "network": "none",
        "workspace": False,
        "cgo": False,
        "compiler_directives": "reject-nonstandard-cgo-import-dynamic-v1",
        "target_mode": "native",
        "link_mode": "internal",
        "libgcc": "none",
        "package_assembly": False,
        "host_objects": False,
        "telemetry": "off-private",
    }
)
GO_BUILD_POLICY_MEMBERS: Final[tuple[str, ...]] = (*FIXED_GO_BUILD_POLICY, "execution_policy")

_ARTIFACT_DIRECTORY: Final = "bin"
_WINDOWS_GOOS: Final = "windows"
_WINDOWS_ARTIFACT_SUFFIX: Final = ".exe"
_SHA256_PREFIX: Final = "sha256:"
_SHA256_DIGITS: Final = "0123456789abcdef"

_BUILD_INPUT_MEMBERS: Final[frozenset[str]] = frozenset(
    {
        "schema_version",
        "driver",
        "build_source",
        "build_root",
        "command",
        "source_dir",
        "target",
        "toolchain",
        "policy",
    }
)
_TARGET_MEMBERS: Final[frozenset[str]] = frozenset({"goos", "goarch", "tuning"})
_TOOLCHAIN_MEMBERS: Final[frozenset[str]] = frozenset(
    {"algorithm", "go_relpath", "go_version", "content_sha256"}
)
_IDENTITY_MEMBERS: Final[frozenset[str]] = frozenset({"algorithm", "content_sha256"})
_RECEIPT_MEMBERS: Final[frozenset[str]] = frozenset({"schema_version", "cache_key", "input", "artifact"})
_ARTIFACT_MEMBERS: Final[frozenset[str]] = frozenset({"path", "sha256", "size"})

class BuildMetadataError(RuntimeError):
    """Stable failure while deriving or reading portable build metadata."""

    def __init__(self, code: str, detail: str):
        self.code = code
        self.detail = detail
        super().__init__(f"go-v1 {code}: {detail}")


@dataclass(frozen=True)
class GoBuildPolicy:
    """The fixed go-v1 policy object carried inside the hashed build input.

    Eleven members are manager-chosen protocol constants, so only the
    execution-policy identity is modelled as a field. Constructing a policy
    with an unimplemented execution policy fails instead of producing an input
    that would hash to a key this manager cannot honor.
    """

    execution_policy: str = PORTABLE_EXECUTION_POLICY

    def __post_init__(self) -> None:
        _validate_execution_policy(self.execution_policy)

    def to_json(self) -> dict[str, Any]:
        return {**FIXED_GO_BUILD_POLICY, "execution_policy": self.execution_policy}


@dataclass(frozen=True)
class GoBuildInput:
    """The complete logical input whose CCJ-1 digest is the logical cache key."""

    build_source: BuildSourceIdentity
    build_root: str
    command: str
    source_dir: str
    target: NativeTarget
    toolchain: ToolchainIdentity
    policy: GoBuildPolicy = field(default_factory=GoBuildPolicy)
    driver: str = GO_V1_DRIVER
    schema_version: int = BUILD_INPUT_SCHEMA_VERSION

    def __post_init__(self) -> None:
        if (
            not isinstance(self.schema_version, int)
            or isinstance(self.schema_version, bool)
            or self.schema_version != BUILD_INPUT_SCHEMA_VERSION
        ):
            raise BuildMetadataError(
                "build_input_invalid",
                f"build input schema_version must be {BUILD_INPUT_SCHEMA_VERSION}, got {self.schema_version!r}",
            )
        if not isinstance(self.driver, str) or self.driver not in SUPPORTED_BUILD_DRIVERS:
            raise BuildMetadataError("unsupported_build_driver", f"unsupported build driver {self.driver!r}")
        if not isinstance(self.build_source, BuildSourceIdentity):
            raise BuildMetadataError("build_input_invalid", "build_source must be a BuildSourceIdentity")
        if not isinstance(self.target, NativeTarget):
            raise BuildMetadataError("build_input_invalid", "target must be a NativeTarget")
        if not isinstance(self.toolchain, ToolchainIdentity):
            raise BuildMetadataError("build_input_invalid", "toolchain must be a ToolchainIdentity")
        if not isinstance(self.policy, GoBuildPolicy):
            raise BuildMetadataError("build_input_invalid", "policy must be a GoBuildPolicy")
        _validate_build_source_identity(self.build_source)
        _validate_native_target(self.target)
        _validate_toolchain_identity(self.toolchain, target=self.target)
        _require_portable_path(self.build_root, "build input build_root")
        _require_portable_path(self.source_dir, "build input source_dir")
        _require_identifier(self.command, "build input command")

    @property
    def artifact_path(self) -> str:
        """The only artifact path a manager may accept for this input."""
        return derived_artifact_path(self.command, goos=self.target.goos)

    def to_json(self) -> dict[str, Any]:
        return {
            "build_root": self.build_root,
            "build_source": {
                "algorithm": self.build_source.algorithm,
                "content_sha256": self.build_source.content_sha256,
            },
            "command": self.command,
            "driver": self.driver,
            "policy": self.policy.to_json(),
            "schema_version": self.schema_version,
            "source_dir": self.source_dir,
            "target": {
                "goarch": self.target.goarch,
                "goos": self.target.goos,
                "tuning": dict(self.target.tuning),
            },
            "toolchain": {
                "algorithm": self.toolchain.algorithm,
                "content_sha256": self.toolchain.content_sha256,
                "go_relpath": self.toolchain.go_relpath,
                "go_version": self.toolchain.go_version,
            },
        }


@dataclass(frozen=True)
class BuildArtifact:
    """The one manager-derived artifact an immutable logical entry contains."""

    path: str
    sha256: str
    size: int

    def __post_init__(self) -> None:
        _require_portable_path(self.path, "artifact path", "build_receipt_invalid")
        _require_sha256(self.sha256, "artifact sha256", "build_receipt_invalid")
        if not isinstance(self.size, int) or isinstance(self.size, bool):
            raise BuildMetadataError("build_receipt_invalid", f"artifact size must be an integer, got {self.size!r}")
        if not 0 <= self.size <= protocol_json.MAX_SAFE_INTEGER:
            raise BuildMetadataError("build_receipt_invalid", f"artifact size is outside the safe range: {self.size}")

    def to_json(self) -> dict[str, Any]:
        return {"path": self.path, "sha256": self.sha256, "size": self.size}


@dataclass(frozen=True)
class BuildReceipt:
    """Build receipt schema 1: the entry's key, complete input, and artifact."""

    cache_key: str
    input: GoBuildInput
    artifact: BuildArtifact
    schema_version: int = RECEIPT_SCHEMA_VERSION

    def __post_init__(self) -> None:
        if (
            not isinstance(self.schema_version, int)
            or isinstance(self.schema_version, bool)
            or self.schema_version not in SUPPORTED_RECEIPT_SCHEMA_VERSIONS
        ):
            raise BuildMetadataError(
                "unsupported_receipt_schema",
                f"unsupported build receipt schema_version {self.schema_version!r}",
            )
        if not isinstance(self.input, GoBuildInput):
            raise BuildMetadataError("build_receipt_invalid", "receipt input must be a GoBuildInput")
        if not isinstance(self.artifact, BuildArtifact):
            raise BuildMetadataError("build_receipt_invalid", "receipt artifact must be a BuildArtifact")
        _require_sha256(self.cache_key, "receipt cache_key", "build_receipt_invalid")
        derived = cache_key(self.input)
        if self.cache_key != derived:
            raise BuildMetadataError(
                "cache_key_mismatch",
                f"receipt cache_key {self.cache_key} does not equal the key its input derives ({derived})",
            )
        if self.artifact.path != self.input.artifact_path:
            raise BuildMetadataError(
                "artifact_path_mismatch",
                f"artifact path {self.artifact.path!r} is not the manager-derived "
                f"path {self.input.artifact_path!r}",
            )

    def to_json(self) -> dict[str, Any]:
        return {
            "artifact": self.artifact.to_json(),
            "cache_key": self.cache_key,
            "input": self.input.to_json(),
            "schema_version": self.schema_version,
        }


def derived_artifact_path(command: str, *, goos: str) -> str:
    """Derive the artifact-relative path from the command name alone."""
    _require_identifier(command, "build command")
    suffix = _WINDOWS_ARTIFACT_SUFFIX if goos == _WINDOWS_GOOS else ""
    return f"{_ARTIFACT_DIRECTORY}/{command}{suffix}"


def canonical_input_bytes(build_input: GoBuildInput) -> bytes:
    """Return the exact CCJ-1 bytes the logical cache key is computed over."""
    return _canonical_bytes(build_input.to_json(), "build input")


def cache_key(build_input: GoBuildInput) -> str:
    """Return the logical cache key of one build input."""
    return sha256_identity(canonical_input_bytes(build_input))


def canonical_receipt_bytes(receipt: BuildReceipt) -> bytes:
    """Return the exact bytes a stored receipt must equal."""
    return _canonical_bytes(receipt.to_json(), "build receipt")


def receipt_sha256(raw: bytes) -> str:
    """Return the receipt hash over exact stored receipt bytes."""
    return sha256_identity(raw)


def sha256_identity(raw: bytes) -> str:
    return _SHA256_PREFIX + hashlib.sha256(raw).hexdigest()


def build_receipt(build_input: GoBuildInput, artifact: BuildArtifact) -> BuildReceipt:
    """Bind one artifact to one input under that input's logical cache key."""
    return BuildReceipt(cache_key=cache_key(build_input), input=build_input, artifact=artifact)


def parse_build_input(value: Any) -> GoBuildInput:
    """Read one logical build input, rejecting anything a manager cannot honor."""
    body = _require_object(value, "build input", "build_input_invalid")
    _reject_unknown_members(body, _BUILD_INPUT_MEMBERS, "build input", "build_input_invalid")
    _require_members(body, _BUILD_INPUT_MEMBERS, "build input", "build_input_invalid")

    driver = body["driver"]
    if not isinstance(driver, str) or driver not in SUPPORTED_BUILD_DRIVERS:
        raise BuildMetadataError("unsupported_build_driver", f"unsupported build driver {driver!r}")

    schema_version = body["schema_version"]
    if schema_version != BUILD_INPUT_SCHEMA_VERSION or isinstance(schema_version, bool):
        raise BuildMetadataError(
            "build_input_invalid",
            f"build input schema_version must be {BUILD_INPUT_SCHEMA_VERSION}, got {schema_version!r}",
        )

    return GoBuildInput(
        build_source=_parse_build_source_identity(body["build_source"]),
        build_root=_require_text(body["build_root"], "build input build_root", "build_input_invalid"),
        command=_require_text(body["command"], "build input command", "build_input_invalid"),
        source_dir=_require_text(body["source_dir"], "build input source_dir", "build_input_invalid"),
        target=_parse_native_target(body["target"]),
        toolchain=_parse_toolchain_identity(body["toolchain"]),
        policy=parse_go_build_policy(body["policy"]),
        driver=driver,
        schema_version=schema_version,
    )


def parse_go_build_policy(value: Any) -> GoBuildPolicy:
    """Read the fixed policy object and its required execution-policy identity."""
    body = _require_object(value, "build policy", "build_input_invalid")
    _reject_unknown_members(body, frozenset(GO_BUILD_POLICY_MEMBERS), "build policy", "build_input_invalid")
    for member, expected in FIXED_GO_BUILD_POLICY.items():
        if member not in body:
            raise BuildMetadataError("build_input_invalid", f"build policy is missing required member {member!r}")
        actual = body[member]
        if actual != expected or isinstance(actual, bool) is not isinstance(expected, bool):
            raise BuildMetadataError(
                "build_input_invalid",
                f"build policy {member!r} must be {expected!r}, got {actual!r}",
            )
    if "execution_policy" not in body:
        raise BuildMetadataError(
            "build_input_invalid",
            "build policy is missing the required member 'execution_policy'",
        )
    execution_policy = body["execution_policy"]
    _validate_execution_policy(execution_policy)
    return GoBuildPolicy(execution_policy=execution_policy)


def parse_receipt(value: Any) -> BuildReceipt:
    """Read one decoded build receipt schema 1."""
    body = _require_object(value, "build receipt", "build_receipt_invalid")
    _reject_unknown_members(body, _RECEIPT_MEMBERS, "build receipt", "build_receipt_invalid")
    _require_members(body, _RECEIPT_MEMBERS, "build receipt", "build_receipt_invalid")

    schema_version = body["schema_version"]
    if schema_version not in SUPPORTED_RECEIPT_SCHEMA_VERSIONS or isinstance(schema_version, bool):
        raise BuildMetadataError(
            "unsupported_receipt_schema",
            f"unsupported build receipt schema_version {schema_version!r}",
        )

    return BuildReceipt(
        cache_key=_require_text(body["cache_key"], "receipt cache_key", "build_receipt_invalid"),
        input=parse_build_input(body["input"]),
        artifact=_parse_artifact(body["artifact"]),
        schema_version=schema_version,
    )


def read_receipt(raw: bytes) -> BuildReceipt:
    """Read stored receipt bytes, requiring exact CCJ-1 stored-byte equality."""
    if not isinstance(raw, bytes):
        raise BuildMetadataError("build_receipt_invalid", "stored receipt bytes must be bytes")
    try:
        decoded = protocol_json.loads_canonical(raw)
    except protocol_json.ProtocolJSONError as exc:
        raise BuildMetadataError("receipt_not_canonical", f"stored receipt is not CCJ-1: {exc}") from exc
    receipt = parse_receipt(decoded)
    if canonical_receipt_bytes(receipt) != raw:
        raise BuildMetadataError(
            "receipt_not_canonical",
            "stored receipt bytes do not equal the canonical encoding of their own value",
        )
    return receipt


def verify_receipt(
    raw: bytes,
    *,
    expected_input: GoBuildInput,
    expected_cache_key: str | None = None,
    expected_receipt_sha256: str | None = None,
) -> BuildReceipt:
    """Recanonicalize a stored receipt and bind it to the expected input.

    This is the portable half of protected-entry validation: stored-byte
    canonicality, the receipt's own key, the entire expected input, the
    manager-derived artifact path, and the receipt hash. Boundary ownership,
    link safety, and artifact bytes belong to the cache owner.
    """

    receipt = read_receipt(raw)
    expected_key = cache_key(expected_input)
    if expected_cache_key is not None and expected_cache_key != expected_key:
        raise BuildMetadataError(
            "cache_key_mismatch",
            f"looked-up cache key {expected_cache_key} does not equal the key the expected input derives",
        )
    if receipt.input != expected_input:
        raise BuildMetadataError("cache_input_mismatch", "receipt input does not equal the expected build input")
    if receipt.cache_key != expected_key:
        raise BuildMetadataError(
            "cache_key_mismatch",
            f"receipt cache_key {receipt.cache_key} does not equal the expected key {expected_key}",
        )
    if expected_receipt_sha256 is not None:
        _require_sha256(expected_receipt_sha256, "expected receipt_sha256", "receipt_hash_mismatch")
        actual = receipt_sha256(raw)
        if actual != expected_receipt_sha256:
            raise BuildMetadataError(
                "receipt_hash_mismatch",
                f"stored receipt hashes to {actual}, not the recorded {expected_receipt_sha256}",
            )
    return receipt


def _parse_build_source_identity(value: Any) -> BuildSourceIdentity:
    body = _require_object(value, "build-source identity", "build_input_invalid")
    _reject_unknown_members(body, _IDENTITY_MEMBERS, "build-source identity", "build_input_invalid")
    _require_members(body, _IDENTITY_MEMBERS, "build-source identity", "build_input_invalid")
    return BuildSourceIdentity(
        algorithm=_require_text(body["algorithm"], "build-source algorithm", "build_input_invalid"),
        content_sha256=_require_text(body["content_sha256"], "build-source content_sha256", "build_input_invalid"),
    )


def _parse_toolchain_identity(value: Any) -> ToolchainIdentity:
    body = _require_object(value, "toolchain identity", "build_input_invalid")
    _reject_unknown_members(body, _TOOLCHAIN_MEMBERS, "toolchain identity", "build_input_invalid")
    _require_members(body, _TOOLCHAIN_MEMBERS, "toolchain identity", "build_input_invalid")
    return ToolchainIdentity(
        algorithm=_require_text(body["algorithm"], "toolchain algorithm", "build_input_invalid"),
        content_sha256=_require_text(body["content_sha256"], "toolchain content_sha256", "build_input_invalid"),
        go_relpath=_require_text(body["go_relpath"], "toolchain go_relpath", "build_input_invalid"),
        go_version=_require_text(body["go_version"], "toolchain go_version", "build_input_invalid"),
    )


def _parse_native_target(value: Any) -> NativeTarget:
    body = _require_object(value, "native target", "build_input_invalid")
    _reject_unknown_members(body, _TARGET_MEMBERS, "native target", "build_input_invalid")
    _require_members(body, _TARGET_MEMBERS, "native target", "build_input_invalid")
    tuning = _require_object(body["tuning"], "native target tuning", "build_input_invalid")
    try:
        return NativeTarget(
            goos=_require_text(body["goos"], "native target goos", "build_input_invalid"),
            goarch=_require_text(body["goarch"], "native target goarch", "build_input_invalid"),
            tuning=dict(tuning),
        )
    except ValueError as exc:
        raise BuildMetadataError("build_input_invalid", f"invalid native target: {exc}") from exc


def _parse_artifact(value: Any) -> BuildArtifact:
    body = _require_object(value, "build artifact", "build_receipt_invalid")
    _reject_unknown_members(body, _ARTIFACT_MEMBERS, "build artifact", "build_receipt_invalid")
    _require_members(body, _ARTIFACT_MEMBERS, "build artifact", "build_receipt_invalid")
    return BuildArtifact(
        path=_require_text(body["path"], "artifact path", "build_receipt_invalid"),
        sha256=_require_text(body["sha256"], "artifact sha256", "build_receipt_invalid"),
        size=body["size"],
    )


def _validate_execution_policy(value: Any) -> None:
    if not isinstance(value, str) or not value:
        raise BuildMetadataError("build_input_invalid", f"execution_policy must be a non-empty string, got {value!r}")
    if value not in SUPPORTED_EXECUTION_POLICIES:
        raise BuildMetadataError(
            "build_execution_policy_unsupported",
            f"execution policy {value!r} is not the policy this manager implements",
        )


PORTABLE_GO_BUILD_POLICY: Final = GoBuildPolicy()


def _validate_build_source_identity(identity: BuildSourceIdentity) -> None:
    if identity.algorithm != BUILD_SOURCE_ALGORITHM:
        raise BuildMetadataError(
            "build_input_invalid",
            f"build-source algorithm must be {BUILD_SOURCE_ALGORITHM!r}, got {identity.algorithm!r}",
        )
    _require_sha256(identity.content_sha256, "build-source content_sha256")


def _validate_toolchain_identity(
    identity: ToolchainIdentity,
    *,
    target: NativeTarget,
) -> None:
    if identity.algorithm != TOOLCHAIN_ALGORITHM:
        raise BuildMetadataError(
            "build_input_invalid",
            f"toolchain algorithm must be {TOOLCHAIN_ALGORITHM!r}, got {identity.algorithm!r}",
        )
    if identity.go_relpath != GO_RELPATH:
        raise BuildMetadataError(
            "build_input_invalid",
            f"toolchain go_relpath must be {GO_RELPATH!r}, got {identity.go_relpath!r}",
        )
    try:
        _normalized, _family, version_goos, version_goarch = (
            parse_normalized_go_version(identity.go_version)
        )
    except ToolchainError as exc:
        raise BuildMetadataError(
            "build_input_invalid",
            f"toolchain go_version is malformed: {exc.detail}",
        ) from exc
    if (version_goos, version_goarch) != (target.goos, target.goarch):
        raise BuildMetadataError(
            "build_input_invalid",
            "toolchain go_version target "
            f"{version_goos}/{version_goarch} does not match native target "
            f"{target.goos}/{target.goarch}",
        )
    _require_sha256(identity.content_sha256, "toolchain content_sha256")


def _validate_native_target(target: NativeTarget) -> None:
    if len(target.tuning) != 1:
        raise BuildMetadataError(
            "build_input_invalid",
            "native target tuning must contain exactly one variable",
        )
    _require_identifier(target.goos, "native target goos")
    _require_identifier(target.goarch, "native target goarch")
    expected_name = TUNING_VARIABLES.get(target.goarch)
    if expected_name is None:
        raise BuildMetadataError(
            "build_input_invalid",
            f"unsupported native target architecture {target.goarch!r}",
        )
    name, value = next(iter(target.tuning.items()))
    if name != expected_name:
        raise BuildMetadataError(
            "build_input_invalid",
            f"native target architecture {target.goarch!r} requires tuning "
            f"{expected_name!r}, got {name!r}",
        )
    if not isinstance(value, str) or not value:
        raise BuildMetadataError(
            "build_input_invalid",
            f"native target tuning {name!r} must be a non-empty string, got {value!r}",
        )


def _canonical_bytes(body: dict[str, Any], subject: str) -> bytes:
    try:
        return protocol_json.canonical_bytes(body)
    except protocol_json.ProtocolJSONError as exc:
        raise BuildMetadataError("build_input_invalid", f"{subject} is not representable in CCJ-1: {exc}") from exc


def _require_object(value: Any, subject: str, code: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise BuildMetadataError(code, f"{subject} must be a JSON object, got {type(value).__name__}")
    return value


def _require_members(body: dict[str, Any], members: frozenset[str], subject: str, code: str) -> None:
    missing = sorted(members - set(body))
    if missing:
        raise BuildMetadataError(code, f"{subject} is missing required member(s): {', '.join(missing)}")


def _reject_unknown_members(body: dict[str, Any], members: frozenset[str], subject: str, code: str) -> None:
    unknown = sorted(set(body) - members)
    if unknown:
        raise BuildMetadataError(code, f"{subject} has unknown member(s): {', '.join(unknown)}")


def _require_text(value: Any, subject: str, code: str) -> str:
    if not isinstance(value, str):
        raise BuildMetadataError(code, f"{subject} must be a string, got {type(value).__name__}")
    return value


def _require_sha256(value: str, subject: str, code: str = "build_input_invalid") -> None:
    if (
        not isinstance(value, str)
        or not value.startswith(_SHA256_PREFIX)
        or len(value) != len(_SHA256_PREFIX) + 64
        or any(character not in _SHA256_DIGITS for character in value[len(_SHA256_PREFIX) :])
    ):
        raise BuildMetadataError(code, f"{subject} must be 'sha256:' and 64 lowercase hex digits")


def _require_portable_path(value: str, subject: str, code: str = "build_input_invalid") -> None:
    if not isinstance(value, str) or not is_valid_portable_path(value):
        raise BuildMetadataError(code, f"{subject} is not a portable relative path: {value!r}")


def _require_identifier(value: str, subject: str, code: str = "build_input_invalid") -> None:
    if not isinstance(value, str) or not is_valid_identifier(value):
        raise BuildMetadataError(code, f"{subject} is not a portable identifier: {value!r}")
