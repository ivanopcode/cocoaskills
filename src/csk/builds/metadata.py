"""Portable logical build metadata for the local ``go-v1`` driver.

This module owns the protocol side of a compiled build: the logical build
input, its CCJ-1 cache key, and build receipt schema 1, plus the source-aware
build receipt schema 3 wrapper shared by the local ``go-v1`` and external
``go-repository-v1`` arms. Everything here is portable. Manager-home paths,
physical cache-root and driver-directory names, receipt filenames, and lock
names are implementation-specific and never appear in a hashed input, a
receipt, or a marker.
"""

from __future__ import annotations

import hashlib
import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import TYPE_CHECKING, Any, Final, Literal, TypeAlias

from .. import protocol_json
from ..build_repository import (
    DESCRIPTOR_NAME,
    GO_REPOSITORY_V1_DRIVER,
    is_valid_ref_name,
)
from ..identifiers import is_valid_identifier, is_valid_portable_path

if TYPE_CHECKING:
    from ..sources.package_identity import PackageIdentity
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
_CACHE_ARTIFACT_NAME: Final = "artifact"
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


def derived_cache_artifact_name(artifact_path: str) -> str:
    """Name a protected cache entry gives one derived artifact on disk.

    An entry holds a single artifact, so the stem never varies. The suffix has
    to: Windows will not execute a file that carries no executable extension,
    so a launcher aimed at a suffix-less name cannot run it.
    """
    directory, separator, name = artifact_path.partition("/")
    if directory != _ARTIFACT_DIRECTORY or not separator or "/" in name:
        raise BuildMetadataError(
            "build_input_invalid",
            f"artifact path {artifact_path!r} is not manager derived",
        )
    windows = name.endswith(_WINDOWS_ARTIFACT_SUFFIX)
    command = name[: -len(_WINDOWS_ARTIFACT_SUFFIX)] if windows else name
    _require_identifier(command, "build command")
    return _CACHE_ARTIFACT_NAME + (_WINDOWS_ARTIFACT_SUFFIX if windows else "")


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


# ---------------------------------------------------------------------------
# Build receipt schema 3: the source-aware wrapper.
#
# Schema 3 wraps the existing closed driver input byte-for-byte in
# ``input = {schema_version: 3, package, build}`` where ``package`` is the
# source-types schema 1 identity and ``build`` is exactly the go-v1 driver
# input (schema 1) or the go-repository-v1 driver input (schema 2). The
# ``cache_key`` is SHA-256 over CCJ-1 of the whole wrapped input, recomputed
# here, never copied forward from another receipt.
#
# ``wrap_receipt_v3_input`` below is the one wrapper construction shared by the
# local planner and the repository pipeline, and ``source_aware_cache_key`` is
# the one key computation; ``protocol_json.canonical_bytes`` is the one
# serializer. There is no second wrapper, key, or serializer.
# ---------------------------------------------------------------------------

RECEIPT_V3_SCHEMA_VERSION: Final = 3
RECEIPT_V3_INPUT_SCHEMA_VERSION: Final = 3
REPOSITORY_BUILD_INPUT_SCHEMA_VERSION: Final = 2
REPOSITORY_SOURCE_KIND: Final = "locked-external-git-v1"

SUPPORTED_REPOSITORY_BUILD_DRIVERS: Final[frozenset[str]] = frozenset({GO_REPOSITORY_V1_DRIVER})

# The networkGit repository identity grammar, copied verbatim from
# common.schema.json networkSourceIdentity (lengths enforced separately).
_NETWORK_REPOSITORY_IDENTITY_RE: Final = re.compile(
    r"^(?!.*\.git$)[a-z0-9][a-z0-9.-]*/"
    r"(?!(?:\.{1,2})(?:/|$))[^\s%?#\\/:\x00-\x1f\x7f-\x9f]+"
    r"(?:/(?!(?:\.{1,2})(?:/|$))[^\s%?#\\/:\x00-\x1f\x7f-\x9f]+)*$"
)
_NETWORK_IDENTITY_MIN_LENGTH: Final = 3
_NETWORK_IDENTITY_MAX_LENGTH: Final = 4096

_REPOSITORY_TRANSPORTS: Final[frozenset[str]] = frozenset({"https", "ssh"})
_REPOSITORY_OBJECT_FORMATS: Final[frozenset[str]] = frozenset({"sha1", "sha256"})
_REPOSITORY_REF_KINDS: Final[frozenset[str]] = frozenset({"revision", "tag", "branch"})

_REPOSITORY_IDENTITY_MEMBERS: Final[frozenset[str]] = frozenset({"kind", "value"})
_REPOSITORY_LOCKED_COMMIT_MEMBERS: Final[frozenset[str]] = frozenset({"object_format", "hex"})
_REPOSITORY_REF_MEMBERS: Final[frozenset[str]] = frozenset({"kind", "value"})
_REPOSITORY_SUBSTITUTION_MEMBERS: Final[frozenset[str]] = frozenset({"type", "ref"})
_DECLARED_SOURCE_MEMBERS: Final[frozenset[str]] = frozenset(
    {"identity", "transport", "locked_commit", "tag"}
)
_EFFECTIVE_SOURCE_MEMBERS: Final[frozenset[str]] = frozenset(
    {
        "identity",
        "object_format",
        "commit",
        "substituted",
        "build_source",
        "transport",
        "substitution",
    }
)
_REPOSITORY_DESCRIPTOR_MEMBERS: Final[frozenset[str]] = frozenset({"path", "target"})
_REPOSITORY_SOURCE_MEMBERS: Final[frozenset[str]] = frozenset(
    {"repository", "declared", "effective", "descriptor"}
)
_REPOSITORY_BUILD_INPUT_MEMBERS: Final[frozenset[str]] = frozenset(
    {
        "schema_version",
        "driver",
        "source",
        "command",
        "build_root",
        "source_dir",
        "target",
        "toolchain",
        "policy",
    }
)
_REPOSITORY_POLICY_MEMBERS: Final[frozenset[str]] = frozenset(
    (*GO_BUILD_POLICY_MEMBERS, "source_kind")
)
_RECEIPT_V3_INPUT_MEMBERS: Final[frozenset[str]] = frozenset({"schema_version", "package", "build"})


def _package_identity_types() -> tuple[type, ...]:
    # Imported lazily: importing the sources package at this module's import
    # time would load it for every consumer of the build metadata, including
    # paths that never touch source-aware receipts.
    from ..sources.package_identity import ConfiguredGit, LocalSnapshot, NetworkGit

    return (LocalSnapshot, NetworkGit, ConfiguredGit)


@dataclass(frozen=True)
class RepositorySourceIdentity:
    """A network or operator-local repository identity inside a schema-2 input."""

    kind: str
    value: str

    def __post_init__(self) -> None:
        if self.kind == "network-git":
            _require_network_repository_identity(self.value)
        elif self.kind == "operator-local-git":
            _require_sha256(self.value, "operator-local repository identity")
        else:
            raise BuildMetadataError(
                "build_input_invalid",
                "repository identity kind must be 'network-git' or "
                f"'operator-local-git', got {self.kind!r}",
            )

    def to_json(self) -> dict[str, str]:
        return {"kind": self.kind, "value": self.value}


@dataclass(frozen=True)
class RepositoryLockedCommit:
    """One locked object id with its declared object format."""

    object_format: str
    hex: str

    def __post_init__(self) -> None:
        _require_object_format(self.object_format, "locked commit object_format")
        _require_commit_hex(self.hex, self.object_format, "locked commit hex")

    def to_json(self) -> dict[str, str]:
        return {"object_format": self.object_format, "hex": self.hex}


@dataclass(frozen=True)
class RepositoryStructuredRef:
    """One structured repository ref inside a network substitution."""

    kind: str
    value: str

    def __post_init__(self) -> None:
        if self.kind not in _REPOSITORY_REF_KINDS:
            raise BuildMetadataError(
                "build_input_invalid",
                f"repository ref kind must be one of revision, tag, branch, got {self.kind!r}",
            )
        if self.kind == "revision":
            if (
                not isinstance(self.value, str)
                or len(self.value) not in (40, 64)
                or any(character not in _SHA256_DIGITS for character in self.value)
            ):
                raise BuildMetadataError(
                    "build_input_invalid",
                    "repository revision ref must be 40 or 64 lowercase hex digits",
                )
        elif not isinstance(self.value, str) or not is_valid_ref_name(self.value):
            raise BuildMetadataError(
                "build_input_invalid",
                f"repository {self.kind} ref is not a valid git ref name: {self.value!r}",
            )

    def to_json(self) -> dict[str, str]:
        return {"kind": self.kind, "value": self.value}


@dataclass(frozen=True)
class RepositorySubstitution:
    """One typed effective-source substitution: local-path or network-git."""

    type: str
    ref: RepositoryStructuredRef | None = None

    def __post_init__(self) -> None:
        if self.type == "local-path":
            if self.ref is not None:
                raise BuildMetadataError(
                    "build_input_invalid", "local-path substitution must not contain ref"
                )
        elif self.type == "network-git":
            if self.ref is None:
                raise BuildMetadataError(
                    "build_input_invalid", "network-git substitution requires ref"
                )
        else:
            raise BuildMetadataError(
                "build_input_invalid",
                f"repository substitution type must be 'local-path' or 'network-git', "
                f"got {self.type!r}",
            )

    def to_json(self) -> dict[str, Any]:
        result: dict[str, Any] = {"type": self.type}
        if self.ref is not None:
            result["ref"] = self.ref.to_json()
        return result


@dataclass(frozen=True)
class DeclaredRepositorySource:
    """The declared network source a schema-2 input was resolved from."""

    identity: RepositorySourceIdentity
    transport: str
    locked_commit: RepositoryLockedCommit
    tag: str | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.identity, RepositorySourceIdentity):
            raise BuildMetadataError(
                "build_input_invalid", "declared identity must be a RepositorySourceIdentity"
            )
        if self.identity.kind != "network-git":
            raise BuildMetadataError(
                "build_input_invalid", "declared repository identity must be network-git"
            )
        if self.transport not in _REPOSITORY_TRANSPORTS:
            raise BuildMetadataError(
                "build_input_invalid",
                f"declared transport must be 'https' or 'ssh', got {self.transport!r}",
            )
        if not isinstance(self.locked_commit, RepositoryLockedCommit):
            raise BuildMetadataError(
                "build_input_invalid",
                "declared locked_commit must be a RepositoryLockedCommit",
            )
        if self.tag is not None and (
            not isinstance(self.tag, str) or not is_valid_ref_name(self.tag)
        ):
            raise BuildMetadataError(
                "build_input_invalid",
                f"declared tag is not a valid git ref name: {self.tag!r}",
            )

    def to_json(self) -> dict[str, Any]:
        result: dict[str, Any] = {
            "identity": self.identity.to_json(),
            "locked_commit": self.locked_commit.to_json(),
            "transport": self.transport,
        }
        if self.tag is not None:
            result["tag"] = self.tag
        return result


@dataclass(frozen=True)
class EffectiveRepositorySource:
    """The effective admitted source a schema-2 input was built from."""

    identity: RepositorySourceIdentity
    object_format: str
    commit: str
    substituted: bool
    build_source: BuildSourceIdentity
    transport: str | None = None
    substitution: RepositorySubstitution | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.identity, RepositorySourceIdentity):
            raise BuildMetadataError(
                "build_input_invalid",
                "effective identity must be a RepositorySourceIdentity",
            )
        _require_object_format(self.object_format, "effective object_format")
        _require_commit_hex(self.commit, self.object_format, "effective commit")
        if not isinstance(self.substituted, bool):
            raise BuildMetadataError(
                "build_input_invalid", "effective substituted must be a boolean"
            )
        if not isinstance(self.build_source, BuildSourceIdentity):
            raise BuildMetadataError(
                "build_input_invalid", "effective build_source must be a BuildSourceIdentity"
            )
        _validate_build_source_identity(self.build_source)
        if self.identity.kind == "network-git":
            if self.transport not in _REPOSITORY_TRANSPORTS:
                raise BuildMetadataError(
                    "build_input_invalid",
                    "effective network-git source requires transport 'https' or 'ssh'",
                )
        elif self.transport is not None:
            raise BuildMetadataError(
                "build_input_invalid",
                "effective operator-local-git source must not contain transport",
            )
        if not isinstance(self.substitution, (RepositorySubstitution, type(None))):
            raise BuildMetadataError(
                "build_input_invalid",
                "effective substitution must be a RepositorySubstitution",
            )
        if self.substituted != (self.substitution is not None):
            raise BuildMetadataError(
                "build_input_invalid",
                "effective substitution must be present exactly when substituted is true",
            )

    def to_json(self) -> dict[str, Any]:
        result: dict[str, Any] = {
            "build_source": {
                "algorithm": self.build_source.algorithm,
                "content_sha256": self.build_source.content_sha256,
            },
            "commit": self.commit,
            "identity": self.identity.to_json(),
            "object_format": self.object_format,
            "substituted": self.substituted,
        }
        if self.transport is not None:
            result["transport"] = self.transport
        if self.substitution is not None:
            result["substitution"] = self.substitution.to_json()
        return result


@dataclass(frozen=True)
class RepositoryDescriptorSelection:
    """The descriptor path and target a schema-2 input was built from."""

    target: str
    path: str = DESCRIPTOR_NAME

    def __post_init__(self) -> None:
        if self.path != DESCRIPTOR_NAME:
            raise BuildMetadataError(
                "build_input_invalid",
                f"repository descriptor path must be {DESCRIPTOR_NAME!r}, got {self.path!r}",
            )
        _require_identifier(self.target, "repository descriptor target")

    def to_json(self) -> dict[str, str]:
        return {"path": self.path, "target": self.target}


@dataclass(frozen=True)
class RepositorySourceSection:
    """The complete source section of a schema-2 driver input."""

    repository: str
    declared: DeclaredRepositorySource
    effective: EffectiveRepositorySource
    descriptor: RepositoryDescriptorSelection

    def __post_init__(self) -> None:
        _require_identifier(self.repository, "repository source repository")
        if not isinstance(self.declared, DeclaredRepositorySource):
            raise BuildMetadataError(
                "build_input_invalid", "source declared must be a DeclaredRepositorySource"
            )
        if not isinstance(self.effective, EffectiveRepositorySource):
            raise BuildMetadataError(
                "build_input_invalid",
                "source effective must be an EffectiveRepositorySource",
            )
        if not isinstance(self.descriptor, RepositoryDescriptorSelection):
            raise BuildMetadataError(
                "build_input_invalid",
                "source descriptor must be a RepositoryDescriptorSelection",
            )

    def to_json(self) -> dict[str, Any]:
        return {
            "declared": self.declared.to_json(),
            "descriptor": self.descriptor.to_json(),
            "effective": self.effective.to_json(),
            "repository": self.repository,
        }


@dataclass(frozen=True)
class GoRepositoryBuildPolicy:
    """The fixed go-repository-v1 policy object carried inside the hashed input.

    The eleven manager-chosen protocol constants are shared with the local
    driver; the external arm additionally pins ``source_kind``. Only the
    execution-policy identity is modelled as a field.
    """

    execution_policy: str = PORTABLE_EXECUTION_POLICY

    def __post_init__(self) -> None:
        _validate_execution_policy(self.execution_policy)

    def to_json(self) -> dict[str, Any]:
        return {
            **FIXED_GO_BUILD_POLICY,
            "execution_policy": self.execution_policy,
            "source_kind": REPOSITORY_SOURCE_KIND,
        }


@dataclass(frozen=True)
class GoRepositoryBuildInput:
    """The complete external driver input (schema 2) wrapped by receipt 3.

    This is the typed form of the ``goRepositoryBuildInputV1`` wire shape the
    repository pipeline constructs: same members, same execution policy, same
    toolchain and dependency fingerprints. Receipt 3 carries it verbatim inside
    ``build``; parsing it here never alters a byte of that lineage.
    """

    source: RepositorySourceSection
    command: str
    build_root: str
    source_dir: str
    target: NativeTarget
    toolchain: ToolchainIdentity
    policy: GoRepositoryBuildPolicy = field(default_factory=GoRepositoryBuildPolicy)
    driver: str = GO_REPOSITORY_V1_DRIVER
    schema_version: int = REPOSITORY_BUILD_INPUT_SCHEMA_VERSION

    def __post_init__(self) -> None:
        if (
            not isinstance(self.schema_version, int)
            or isinstance(self.schema_version, bool)
            or self.schema_version != REPOSITORY_BUILD_INPUT_SCHEMA_VERSION
        ):
            raise BuildMetadataError(
                "build_input_invalid",
                "build input schema_version must be "
                f"{REPOSITORY_BUILD_INPUT_SCHEMA_VERSION}, got {self.schema_version!r}",
            )
        if not isinstance(self.driver, str) or self.driver not in SUPPORTED_REPOSITORY_BUILD_DRIVERS:
            raise BuildMetadataError(
                "unsupported_build_driver", f"unsupported build driver {self.driver!r}"
            )
        if not isinstance(self.source, RepositorySourceSection):
            raise BuildMetadataError(
                "build_input_invalid", "source must be a RepositorySourceSection"
            )
        if not isinstance(self.target, NativeTarget):
            raise BuildMetadataError("build_input_invalid", "target must be a NativeTarget")
        if not isinstance(self.toolchain, ToolchainIdentity):
            raise BuildMetadataError(
                "build_input_invalid", "toolchain must be a ToolchainIdentity"
            )
        if not isinstance(self.policy, GoRepositoryBuildPolicy):
            raise BuildMetadataError(
                "build_input_invalid", "policy must be a GoRepositoryBuildPolicy"
            )
        _validate_native_target(self.target)
        _validate_toolchain_identity(self.toolchain, target=self.target)
        _require_root_or_portable_path(self.build_root, "build input build_root")
        _require_root_or_portable_path(self.source_dir, "build input source_dir")
        _require_identifier(self.command, "build input command")

    @property
    def artifact_path(self) -> str:
        """The only artifact path a manager may accept for this input."""
        return derived_artifact_path(self.command, goos=self.target.goos)

    def to_json(self) -> dict[str, Any]:
        return {
            "build_root": self.build_root,
            "command": self.command,
            "driver": self.driver,
            "policy": self.policy.to_json(),
            "schema_version": self.schema_version,
            "source": self.source.to_json(),
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


ReceiptV3Build: TypeAlias = GoBuildInput | GoRepositoryBuildInput


@dataclass(frozen=True)
class SourceAwareBuildInput:
    """One receipt-3 wrapped input: ``{schema_version: 3, package, build}``.

    ``build`` is the closed driver input verbatim; ``package`` binds the source
    it was built from. The CCJ-1 digest of the whole object is the receipt-3
    cache key.
    """

    package: PackageIdentity
    build: ReceiptV3Build
    schema_version: int = RECEIPT_V3_INPUT_SCHEMA_VERSION

    def __post_init__(self) -> None:
        if (
            not isinstance(self.schema_version, int)
            or isinstance(self.schema_version, bool)
            or self.schema_version != RECEIPT_V3_INPUT_SCHEMA_VERSION
        ):
            raise BuildMetadataError(
                "build_receipt_invalid",
                "receipt input schema_version must be "
                f"{RECEIPT_V3_INPUT_SCHEMA_VERSION}, got {self.schema_version!r}",
            )
        if not isinstance(self.package, _package_identity_types()):
            raise BuildMetadataError(
                "build_receipt_invalid",
                "receipt input package must be a source-types schema 1 identity",
            )
        if not isinstance(self.build, (GoBuildInput, GoRepositoryBuildInput)):
            raise BuildMetadataError(
                "build_receipt_invalid",
                "receipt input build must be a go-v1 or go-repository-v1 driver input",
            )

    @property
    def command(self) -> str:
        return self.build.command

    @property
    def driver(self) -> str:
        return self.build.driver

    @property
    def target(self) -> NativeTarget:
        return self.build.target

    @property
    def artifact_path(self) -> str:
        return self.build.artifact_path

    def to_json(self) -> dict[str, Any]:
        from ..sources.package_identity import package_identity_to_json

        return {
            "schema_version": self.schema_version,
            "package": package_identity_to_json(self.package),
            "build": self.build.to_json(),
        }


@dataclass(frozen=True)
class BuildReceiptV3:
    """Build receipt schema 3: the wrapped key, complete input, and artifact."""

    cache_key: str
    input: SourceAwareBuildInput
    artifact: BuildArtifact
    schema_version: int = RECEIPT_V3_SCHEMA_VERSION

    def __post_init__(self) -> None:
        if (
            not isinstance(self.schema_version, int)
            or isinstance(self.schema_version, bool)
            or self.schema_version != RECEIPT_V3_SCHEMA_VERSION
        ):
            raise BuildMetadataError(
                "unsupported_receipt_schema",
                f"unsupported build receipt schema_version {self.schema_version!r}",
            )
        if not isinstance(self.input, SourceAwareBuildInput):
            raise BuildMetadataError(
                "build_receipt_invalid", "receipt input must be a SourceAwareBuildInput"
            )
        if not isinstance(self.artifact, BuildArtifact):
            raise BuildMetadataError(
                "build_receipt_invalid", "receipt artifact must be a BuildArtifact"
            )
        _require_sha256(self.cache_key, "receipt cache_key", "build_receipt_invalid")
        derived = source_aware_cache_key(self.input)
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


AnyBuildInput: TypeAlias = GoBuildInput | SourceAwareBuildInput
AnyBuildReceipt: TypeAlias = BuildReceipt | BuildReceiptV3


def wrap_receipt_v3_input(
    package: PackageIdentity,
    build: ReceiptV3Build,
) -> SourceAwareBuildInput:
    """Wrap one closed driver input with its source package.

    This is the one receipt-3 wrapper construction, shared by the local
    planner and the repository pipeline. The driver input is carried verbatim:
    wrapping never parses, alters, or re-derives a byte inside ``build``.
    """

    return SourceAwareBuildInput(package=package, build=build)


def canonical_receipt_v3_input_bytes(build_input: SourceAwareBuildInput) -> bytes:
    """Return the exact CCJ-1 bytes the receipt-3 cache key is computed over."""
    return _canonical_bytes(build_input.to_json(), "receipt-3 input")


def source_aware_cache_key(build_input: SourceAwareBuildInput) -> str:
    """Return the receipt-3 cache key of one wrapped input.

    This is the one receipt-3 key computation, shared by the local planner and
    the repository pipeline: SHA-256 over CCJ-1 of the whole wrapped input,
    recomputed here, never copied forward from another receipt.
    """
    return sha256_identity(canonical_receipt_v3_input_bytes(build_input))


def canonical_receipt_v3_bytes(receipt: BuildReceiptV3) -> bytes:
    """Return the exact bytes a stored receipt-3 must equal."""
    return _canonical_bytes(receipt.to_json(), "build receipt")


def build_receipt_v3(
    build_input: SourceAwareBuildInput, artifact: BuildArtifact
) -> BuildReceiptV3:
    """Bind one artifact to one wrapped input under that input's cache key."""
    return BuildReceiptV3(
        cache_key=source_aware_cache_key(build_input),
        input=build_input,
        artifact=artifact,
    )


def parse_repository_build_input(value: Any) -> GoRepositoryBuildInput:
    """Read one external driver input, rejecting anything a manager cannot honor."""
    body = _require_object(value, "build input", "build_input_invalid")
    _reject_unknown_members(body, _REPOSITORY_BUILD_INPUT_MEMBERS, "build input", "build_input_invalid")
    _require_members(body, _REPOSITORY_BUILD_INPUT_MEMBERS, "build input", "build_input_invalid")

    driver = body["driver"]
    if not isinstance(driver, str) or driver not in SUPPORTED_REPOSITORY_BUILD_DRIVERS:
        raise BuildMetadataError("unsupported_build_driver", f"unsupported build driver {driver!r}")

    schema_version = body["schema_version"]
    if schema_version != REPOSITORY_BUILD_INPUT_SCHEMA_VERSION or isinstance(schema_version, bool):
        raise BuildMetadataError(
            "build_input_invalid",
            "build input schema_version must be "
            f"{REPOSITORY_BUILD_INPUT_SCHEMA_VERSION}, got {schema_version!r}",
        )

    return GoRepositoryBuildInput(
        source=_parse_repository_source(body["source"]),
        command=_require_text(body["command"], "build input command", "build_input_invalid"),
        build_root=_require_text(body["build_root"], "build input build_root", "build_input_invalid"),
        source_dir=_require_text(body["source_dir"], "build input source_dir", "build_input_invalid"),
        target=_parse_native_target(body["target"]),
        toolchain=_parse_toolchain_identity(body["toolchain"]),
        policy=parse_repository_build_policy(body["policy"]),
        driver=driver,
        schema_version=schema_version,
    )


def parse_repository_build_policy(value: Any) -> GoRepositoryBuildPolicy:
    """Read the fixed external policy object and its required execution policy."""
    body = _require_object(value, "build policy", "build_input_invalid")
    _reject_unknown_members(body, _REPOSITORY_POLICY_MEMBERS, "build policy", "build_input_invalid")
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
    if body.get("source_kind") != REPOSITORY_SOURCE_KIND:
        raise BuildMetadataError(
            "build_input_invalid",
            f"build policy source_kind must be {REPOSITORY_SOURCE_KIND!r}, "
            f"got {body.get('source_kind')!r}",
        )
    execution_policy = body["execution_policy"]
    _validate_execution_policy(execution_policy)
    return GoRepositoryBuildPolicy(execution_policy=execution_policy)


def parse_receipt_v3_input(value: Any) -> SourceAwareBuildInput:
    """Read one receipt-3 wrapped input with its package and verbatim build."""
    body = _require_object(value, "receipt input", "build_receipt_invalid")
    _reject_unknown_members(body, _RECEIPT_V3_INPUT_MEMBERS, "receipt input", "build_receipt_invalid")
    _require_members(body, _RECEIPT_V3_INPUT_MEMBERS, "receipt input", "build_receipt_invalid")

    schema_version = body["schema_version"]
    if schema_version != RECEIPT_V3_INPUT_SCHEMA_VERSION or isinstance(schema_version, bool):
        raise BuildMetadataError(
            "build_receipt_invalid",
            "receipt input schema_version must be "
            f"{RECEIPT_V3_INPUT_SCHEMA_VERSION}, got {schema_version!r}",
        )
    from ..sources.errors import SourceError
    from ..sources.package_identity import parse_package_identity

    try:
        package = parse_package_identity(body["package"])
    except SourceError as exc:
        raise BuildMetadataError(
            "build_receipt_invalid", f"receipt input package is invalid: {exc.detail}"
        ) from exc
    return SourceAwareBuildInput(
        package=package,
        build=_parse_receipt_v3_build(body["build"]),
        schema_version=schema_version,
    )


def parse_receipt_v3(value: Any) -> BuildReceiptV3:
    """Read one decoded build receipt schema 3."""
    body = _require_object(value, "build receipt", "build_receipt_invalid")
    _reject_unknown_members(body, _RECEIPT_MEMBERS, "build receipt", "build_receipt_invalid")
    _require_members(body, _RECEIPT_MEMBERS, "build receipt", "build_receipt_invalid")

    schema_version = body["schema_version"]
    if schema_version != RECEIPT_V3_SCHEMA_VERSION or isinstance(schema_version, bool):
        raise BuildMetadataError(
            "unsupported_receipt_schema",
            f"unsupported build receipt schema_version {schema_version!r}",
        )

    return BuildReceiptV3(
        cache_key=_require_text(body["cache_key"], "receipt cache_key", "build_receipt_invalid"),
        input=parse_receipt_v3_input(body["input"]),
        artifact=_parse_artifact(body["artifact"]),
        schema_version=schema_version,
    )


def read_receipt_v3(raw: bytes) -> BuildReceiptV3:
    """Read stored receipt-3 bytes, requiring exact CCJ-1 stored-byte equality."""
    if not isinstance(raw, bytes):
        raise BuildMetadataError("build_receipt_invalid", "stored receipt bytes must be bytes")
    try:
        decoded = protocol_json.loads_canonical(raw)
    except protocol_json.ProtocolJSONError as exc:
        raise BuildMetadataError("receipt_not_canonical", f"stored receipt is not CCJ-1: {exc}") from exc
    receipt = parse_receipt_v3(decoded)
    if canonical_receipt_v3_bytes(receipt) != raw:
        raise BuildMetadataError(
            "receipt_not_canonical",
            "stored receipt bytes do not equal the canonical encoding of their own value",
        )
    return receipt


def read_any_receipt(raw: bytes) -> AnyBuildReceipt:
    """Read stored receipt bytes of any supported schema, requiring CCJ-1.

    The schema-3 reader owns version 3 only; every other version, including
    malformed shapes, stays with the schema-1 reader and its exact errors, so
    legacy receipts keep their byte-identical read behavior.
    """
    if not isinstance(raw, bytes):
        raise BuildMetadataError("build_receipt_invalid", "stored receipt bytes must be bytes")
    try:
        decoded = protocol_json.loads_canonical(raw)
    except protocol_json.ProtocolJSONError:
        return read_receipt(raw)
    if (
        isinstance(decoded, dict)
        and not isinstance(decoded.get("schema_version"), bool)
        and decoded.get("schema_version") == RECEIPT_V3_SCHEMA_VERSION
    ):
        return read_receipt_v3(raw)
    return read_receipt(raw)


def verify_receipt_v3(
    raw: bytes,
    *,
    expected_input: SourceAwareBuildInput,
    expected_cache_key: str | None = None,
    expected_receipt_sha256: str | None = None,
) -> BuildReceiptV3:
    """Recanonicalize a stored receipt-3 and bind it to the expected input.

    This is the portable half of protected-entry validation for the receipt-3
    namespace: stored-byte canonicality, the receipt's own key, the entire
    expected wrapped input, the manager-derived artifact path, and the receipt
    hash. Boundary ownership, link safety, and artifact bytes belong to the
    cache owner.
    """

    receipt = read_receipt_v3(raw)
    expected_key = source_aware_cache_key(expected_input)
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


def cache_key_for_input(build_input: AnyBuildInput) -> str:
    """Return the logical cache key of a schema-1 or receipt-3 build input."""
    if isinstance(build_input, SourceAwareBuildInput):
        return source_aware_cache_key(build_input)
    return cache_key(build_input)


def verify_receipt_for_input(
    raw: bytes,
    *,
    expected_input: AnyBuildInput,
    expected_cache_key: str | None = None,
    expected_receipt_sha256: str | None = None,
) -> AnyBuildReceipt:
    """Verify stored receipt bytes against a schema-1 or receipt-3 input."""
    if isinstance(expected_input, SourceAwareBuildInput):
        return verify_receipt_v3(
            raw,
            expected_input=expected_input,
            expected_cache_key=expected_cache_key,
            expected_receipt_sha256=expected_receipt_sha256,
        )
    return verify_receipt(
        raw,
        expected_input=expected_input,
        expected_cache_key=expected_cache_key,
        expected_receipt_sha256=expected_receipt_sha256,
    )


def _parse_receipt_v3_build(value: Any) -> ReceiptV3Build:
    body = _require_object(value, "receipt build", "build_input_invalid")
    driver = body.get("driver")
    if driver == GO_V1_DRIVER:
        return parse_build_input(value)
    if driver == GO_REPOSITORY_V1_DRIVER:
        return parse_repository_build_input(value)
    raise BuildMetadataError("unsupported_build_driver", f"unsupported build driver {driver!r}")


def _parse_repository_source(value: Any) -> RepositorySourceSection:
    body = _require_object(value, "repository source", "build_input_invalid")
    _reject_unknown_members(body, _REPOSITORY_SOURCE_MEMBERS, "repository source", "build_input_invalid")
    _require_members(body, _REPOSITORY_SOURCE_MEMBERS, "repository source", "build_input_invalid")
    return RepositorySourceSection(
        repository=_require_text(body["repository"], "repository source repository", "build_input_invalid"),
        declared=_parse_declared_source(body["declared"]),
        effective=_parse_effective_source(body["effective"]),
        descriptor=_parse_descriptor_selection(body["descriptor"]),
    )


def _parse_declared_source(value: Any) -> DeclaredRepositorySource:
    body = _require_object(value, "declared source", "build_input_invalid")
    _reject_unknown_members(body, _DECLARED_SOURCE_MEMBERS, "declared source", "build_input_invalid")
    _require_members(
        body,
        frozenset({"identity", "transport", "locked_commit"}),
        "declared source",
        "build_input_invalid",
    )
    tag = body.get("tag")
    if tag is not None and not isinstance(tag, str):
        raise BuildMetadataError("build_input_invalid", "declared tag must be a string")
    return DeclaredRepositorySource(
        identity=_parse_source_identity(body["identity"], "declared identity"),
        transport=_require_text(body["transport"], "declared transport", "build_input_invalid"),
        locked_commit=_parse_locked_commit(body["locked_commit"], "declared locked_commit"),
        tag=tag,
    )


def _parse_effective_source(value: Any) -> EffectiveRepositorySource:
    body = _require_object(value, "effective source", "build_input_invalid")
    _reject_unknown_members(body, _EFFECTIVE_SOURCE_MEMBERS, "effective source", "build_input_invalid")
    _require_members(
        body,
        frozenset({"identity", "object_format", "commit", "substituted", "build_source"}),
        "effective source",
        "build_input_invalid",
    )
    transport = body.get("transport")
    if transport is not None and not isinstance(transport, str):
        raise BuildMetadataError("build_input_invalid", "effective transport must be a string")
    substitution = body.get("substitution")
    return EffectiveRepositorySource(
        identity=_parse_source_identity(body["identity"], "effective identity"),
        object_format=_require_text(body["object_format"], "effective object_format", "build_input_invalid"),
        commit=_require_text(body["commit"], "effective commit", "build_input_invalid"),
        substituted=body["substituted"],
        build_source=_parse_build_source_identity(body["build_source"]),
        transport=transport,
        substitution=(
            _parse_substitution(substitution) if substitution is not None else None
        ),
    )


def _parse_descriptor_selection(value: Any) -> RepositoryDescriptorSelection:
    body = _require_object(value, "repository descriptor", "build_input_invalid")
    _reject_unknown_members(
        body, _REPOSITORY_DESCRIPTOR_MEMBERS, "repository descriptor", "build_input_invalid"
    )
    _require_members(
        body, _REPOSITORY_DESCRIPTOR_MEMBERS, "repository descriptor", "build_input_invalid"
    )
    return RepositoryDescriptorSelection(
        target=_require_text(body["target"], "repository descriptor target", "build_input_invalid"),
        path=_require_text(body["path"], "repository descriptor path", "build_input_invalid"),
    )


def _parse_source_identity(value: Any, subject: str) -> RepositorySourceIdentity:
    body = _require_object(value, subject, "build_input_invalid")
    _reject_unknown_members(body, _REPOSITORY_IDENTITY_MEMBERS, subject, "build_input_invalid")
    _require_members(body, _REPOSITORY_IDENTITY_MEMBERS, subject, "build_input_invalid")
    return RepositorySourceIdentity(
        kind=_require_text(body["kind"], f"{subject} kind", "build_input_invalid"),
        value=_require_text(body["value"], f"{subject} value", "build_input_invalid"),
    )


def _parse_locked_commit(value: Any, subject: str) -> RepositoryLockedCommit:
    body = _require_object(value, subject, "build_input_invalid")
    _reject_unknown_members(body, _REPOSITORY_LOCKED_COMMIT_MEMBERS, subject, "build_input_invalid")
    _require_members(body, _REPOSITORY_LOCKED_COMMIT_MEMBERS, subject, "build_input_invalid")
    return RepositoryLockedCommit(
        object_format=_require_text(body["object_format"], f"{subject} object_format", "build_input_invalid"),
        hex=_require_text(body["hex"], f"{subject} hex", "build_input_invalid"),
    )


def _parse_substitution(value: Any) -> RepositorySubstitution:
    body = _require_object(value, "effective substitution", "build_input_invalid")
    _reject_unknown_members(
        body, _REPOSITORY_SUBSTITUTION_MEMBERS, "effective substitution", "build_input_invalid"
    )
    if "type" not in body:
        raise BuildMetadataError(
            "build_input_invalid", "effective substitution is missing required member 'type'"
        )
    ref = body.get("ref")
    return RepositorySubstitution(
        type=_require_text(body["type"], "effective substitution type", "build_input_invalid"),
        ref=_parse_structured_ref(ref) if ref is not None else None,
    )


def _parse_structured_ref(value: Any) -> RepositoryStructuredRef:
    body = _require_object(value, "substitution ref", "build_input_invalid")
    _reject_unknown_members(body, _REPOSITORY_REF_MEMBERS, "substitution ref", "build_input_invalid")
    _require_members(body, _REPOSITORY_REF_MEMBERS, "substitution ref", "build_input_invalid")
    return RepositoryStructuredRef(
        kind=_require_text(body["kind"], "substitution ref kind", "build_input_invalid"),
        value=_require_text(body["value"], "substitution ref value", "build_input_invalid"),
    )


def _require_network_repository_identity(value: str) -> None:
    if (
        not isinstance(value, str)
        or not _NETWORK_IDENTITY_MIN_LENGTH <= len(value) <= _NETWORK_IDENTITY_MAX_LENGTH
        or _NETWORK_REPOSITORY_IDENTITY_RE.match(value) is None
    ):
        raise BuildMetadataError(
            "build_input_invalid",
            f"network repository identity is not a canonical host/path identity: {value!r}",
        )


def _require_object_format(value: str, subject: str) -> None:
    if not isinstance(value, str) or value not in _REPOSITORY_OBJECT_FORMATS:
        raise BuildMetadataError(
            "build_input_invalid",
            f"{subject} must be 'sha1' or 'sha256', got {value!r}",
        )


def _require_commit_hex(value: str, object_format: str, subject: str) -> None:
    width = 40 if object_format == "sha1" else 64
    if (
        not isinstance(value, str)
        or len(value) != width
        or any(character not in _SHA256_DIGITS for character in value)
    ):
        raise BuildMetadataError(
            "build_input_invalid",
            f"{subject} must be {width} lowercase hex digits for {object_format}",
        )


def _require_root_or_portable_path(value: str, subject: str) -> None:
    if not isinstance(value, str) or (
        value != "." and not is_valid_portable_path(value)
    ):
        raise BuildMetadataError(
            "build_input_invalid", f"{subject} is not '.' or a portable relative path: {value!r}"
        )
