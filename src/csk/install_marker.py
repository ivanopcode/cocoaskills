"""Typed install-marker schemas 1, 2, 3, 4, and 5.

The marker records portable installation state only. Physical build-cache,
receipt, lock, quarantine, and manager-home paths are deliberately absent from
both models.

Schema 5 describes schema-2 (skillfile-sources) installations regardless of
skill manifest version: ``package`` replaces the legacy
``source``/``git``/``ref_kind``/``ref``/``commit`` identity and ``lock_sha256``
binds the installed selection. Readers keep the v1-v4 legacy lanes unchanged;
unknown marker versions fail closed.
"""

from __future__ import annotations

import json
import re
from collections.abc import Mapping
from dataclasses import dataclass, fields as dataclass_fields
from datetime import datetime
from pathlib import Path
from types import MappingProxyType
from typing import Any, Final, Literal, TypeAlias, TypeVar

from . import protocol_json
from .builds.metadata import GO_V1_DRIVER, derived_artifact_path
from .build_repository import GO_REPOSITORY_V1_DRIVER
from .builds.source import BuildSourceIdentity
from .hashing import BUILD_SOURCE_ALGORITHM
from .identifiers import is_valid_identifier, is_valid_locale, is_valid_portable_path
from .sources import errors as source_errors
from .sources import package_identity as source_package


INSTALL_MARKER_V1_SCHEMA_VERSION: Final = 1
INSTALL_MARKER_V2_SCHEMA_VERSION: Final = 2
INSTALL_MARKER_V3_SCHEMA_VERSION: Final = 3
INSTALL_MARKER_V4_SCHEMA_VERSION: Final = 4
INSTALL_MARKER_V5_SCHEMA_VERSION: Final = 5
SUPPORTED_INSTALL_MARKER_SCHEMA_VERSIONS: Final[frozenset[int]] = frozenset(
    {
        INSTALL_MARKER_V1_SCHEMA_VERSION,
        INSTALL_MARKER_V2_SCHEMA_VERSION,
        INSTALL_MARKER_V3_SCHEMA_VERSION,
        INSTALL_MARKER_V4_SCHEMA_VERSION,
        INSTALL_MARKER_V5_SCHEMA_VERSION,
    }
)

_SHA256_PREFIX: Final = "sha256:"
_SHA256_DIGITS: Final = frozenset("0123456789abcdef")
_REF_KINDS: Final[frozenset[str]] = frozenset({"tag", "branch", "revision"})
_ATTESTATION_STATUSES: Final[frozenset[str]] = frozenset({"audited", "deprecated"})
_COMMIT_RE: Final = re.compile(r"^[0-9a-f]{40}(?:[0-9a-f]{24})?$")
_TIMESTAMP_RE: Final = re.compile(
    r"^[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}Z$"
)
_KEY_ID_RE: Final = re.compile(r"^[0-9a-f]{16}$")

_COMMON_REQUIRED_MEMBERS: Final[frozenset[str]] = frozenset(
    {
        "schema_version",
        "name",
        "source",
        "ref_kind",
        "ref",
        "commit",
        "content_sha256",
        "locale",
        "agents",
        "commands",
        "dependencies",
        "skill_schema_version",
        "runtime_roots",
        "installed_at",
        "files",
    }
)
_COMMON_OPTIONAL_MEMBERS: Final[frozenset[str]] = frozenset(
    {
        "git",
        "requirements",
        "mcp_servers",
        "attestation",
        "activation",
        "requirers",
        "substituted",
    }
)
_V1_MEMBERS: Final[frozenset[str]] = _COMMON_REQUIRED_MEMBERS | _COMMON_OPTIONAL_MEMBERS
_V2_REQUIRED_MEMBERS: Final[frozenset[str]] = _COMMON_REQUIRED_MEMBERS | frozenset(
    {"build_roots", "builds"}
)
_V2_MEMBERS: Final[frozenset[str]] = (
    _V2_REQUIRED_MEMBERS | _COMMON_OPTIONAL_MEMBERS | frozenset({"build_source"})
)
_V3_REQUIRED_MEMBERS: Final[frozenset[str]] = _V2_REQUIRED_MEMBERS
_V3_MEMBERS: Final[frozenset[str]] = _V2_MEMBERS
# Marker v4 carries marker-v3 meaning unchanged and only widens the manifest
# band it may describe: schema 8 changes which manifests a marker may describe,
# not what a marker records.
_V4_REQUIRED_MEMBERS: Final[frozenset[str]] = _V3_REQUIRED_MEMBERS
_V4_MEMBERS: Final[frozenset[str]] = _V3_MEMBERS
# Marker v5 drops the legacy source/git/ref_kind/ref/commit identity for the
# typed package union plus the lock binding. Every other member keeps its core
# section 10 meaning, including requiredness and canonical set ordering.
_V5_REQUIRED_MEMBERS: Final[frozenset[str]] = frozenset(
    {
        "schema_version",
        "name",
        "package",
        "lock_sha256",
        "content_sha256",
        "locale",
        "agents",
        "commands",
        "dependencies",
        "skill_schema_version",
        "runtime_roots",
        "build_roots",
        "installed_at",
        "files",
        "builds",
    }
)
_V5_OPTIONAL_MEMBERS: Final[frozenset[str]] = frozenset(
    {
        "build_source",
        "requirements",
        "mcp_servers",
        "attestation",
        "activation",
        "requirers",
        "substituted",
    }
)
_V5_MEMBERS: Final[frozenset[str]] = _V5_REQUIRED_MEMBERS | _V5_OPTIONAL_MEMBERS
_BUILD_SOURCE_MEMBERS: Final[frozenset[str]] = frozenset({"algorithm", "content_sha256"})
_BUILD_RECORD_MEMBERS: Final[frozenset[str]] = frozenset(
    {"driver", "cache_key", "receipt_sha256", "artifact_sha256", "artifact_path"}
)
_V3_BUILD_COMMON_MEMBERS: Final[frozenset[str]] = _BUILD_RECORD_MEMBERS | frozenset(
    {"receipt_schema_version", "execution_policy"}
)
_V3_EXTERNAL_BUILD_MEMBERS: Final[frozenset[str]] = _V3_BUILD_COMMON_MEMBERS | frozenset(
    {
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
    }
)
_V3_EXTERNAL_BUILD_REQUIRED: Final[frozenset[str]] = _V3_EXTERNAL_BUILD_MEMBERS - frozenset(
    {"declared_tag", "substitution"}
)
_IDENTITY_MEMBERS: Final[frozenset[str]] = frozenset({"kind", "value"})
_LOCKED_COMMIT_MEMBERS: Final[frozenset[str]] = frozenset({"object_format", "hex"})
_SUBSTITUTION_MEMBERS: Final[frozenset[str]] = frozenset({"type", "ref"})
_SUBSTITUTION_REF_MEMBERS: Final[frozenset[str]] = frozenset({"kind", "value"})
# Core section 4.2 admits exactly three structured substitution refs: a full
# object ID for the effective repository object format, a tag, or a branch.
_SUBSTITUTION_REF_KINDS: Final[frozenset[str]] = frozenset({"revision", "tag", "branch"})
# A local substitution names an operator working tree; a network substitution
# names another Git remote. The effective identity kind states which one the
# build actually compiled, so the two must agree.
_SUBSTITUTION_IDENTITY_KINDS: Final[Mapping[str, str]] = MappingProxyType(
    {"local-path": "operator-local-git", "network-git": "network-git"}
)
_EXECUTION_POLICY: Final = "manager-worker-v1"
_ACTIVATION_MEMBERS: Final[frozenset[str]] = frozenset({"context", "commands"})
_ATTESTATION_REQUIRED_MEMBERS: Final[frozenset[str]] = frozenset({"registry", "status"})
_ATTESTATION_MEMBERS: Final[frozenset[str]] = (
    _ATTESTATION_REQUIRED_MEMBERS | frozenset({"key_id"})
)


class InstallMarkerError(RuntimeError):
    """Stable failure while reading or constructing an install marker."""

    def __init__(self, code: str, detail: str):
        self.code = code
        self.detail = detail
        super().__init__(f"install marker {code}: {detail}")


@dataclass(frozen=True)
class MarkerActivation:
    context: bool
    commands: tuple[str, ...]

    def __post_init__(self) -> None:
        if not isinstance(self.context, bool):
            raise InstallMarkerError(
                "install_marker_invalid",
                f"activation.context must be a boolean, got {self.context!r}",
            )
        object.__setattr__(
            self,
            "commands",
            _freeze_identifier_set(self.commands, "activation.commands"),
        )

    def to_json(self) -> dict[str, Any]:
        return {"context": self.context, "commands": list(self.commands)}


@dataclass(frozen=True)
class MarkerAttestation:
    registry: str
    status: str
    key_id: str | None = None

    def __post_init__(self) -> None:
        _require_non_empty_string(self.registry, "attestation.registry")
        if not isinstance(self.status, str) or self.status not in _ATTESTATION_STATUSES:
            raise InstallMarkerError(
                "install_marker_invalid",
                f"attestation.status must be audited or deprecated, got {self.status!r}",
            )
        if self.key_id is not None and (
            not isinstance(self.key_id, str) or _KEY_ID_RE.fullmatch(self.key_id) is None
        ):
            raise InstallMarkerError(
                "install_marker_invalid",
                f"attestation.key_id must be 16 lowercase hexadecimal digits, got {self.key_id!r}",
            )

    def to_json(self) -> dict[str, Any]:
        result: dict[str, Any] = {"registry": self.registry, "status": self.status}
        if self.key_id is not None:
            result["key_id"] = self.key_id
        return result


@dataclass(frozen=True)
class InstallMarkerBuild:
    """One schema-2 local ``go-v1`` build record."""

    driver: str
    cache_key: str
    receipt_sha256: str
    artifact_sha256: str
    artifact_path: str

    def __post_init__(self) -> None:
        if self.driver != GO_V1_DRIVER:
            raise InstallMarkerError(
                "install_marker_invalid",
                f"build driver must be {GO_V1_DRIVER!r}, got {self.driver!r}",
            )
        _require_sha256(self.cache_key, "build cache_key")
        _require_sha256(self.receipt_sha256, "build receipt_sha256")
        _require_sha256(self.artifact_sha256, "build artifact_sha256")
        _require_portable_path(self.artifact_path, "build artifact_path")

    def to_json(self) -> dict[str, Any]:
        return {
            "driver": self.driver,
            "cache_key": self.cache_key,
            "receipt_sha256": self.receipt_sha256,
            "artifact_sha256": self.artifact_sha256,
            "artifact_path": self.artifact_path,
        }


@dataclass(frozen=True)
class MarkerRepositoryIdentity:
    kind: str
    value: str

    def __post_init__(self) -> None:
        if not isinstance(self.kind, str) or self.kind not in {
            "network-git",
            "operator-local-git",
        }:
            raise InstallMarkerError("install_marker_invalid", f"repository identity kind is invalid: {self.kind!r}")
        _require_non_empty_string(self.value, "repository identity value")

    def to_json(self) -> dict[str, str]:
        return {"kind": self.kind, "value": self.value}


@dataclass(frozen=True)
class MarkerRepositoryCommit:
    object_format: str
    hex: str

    def __post_init__(self) -> None:
        width = 40 if self.object_format == "sha1" else 64 if self.object_format == "sha256" else 0
        if width == 0 or not isinstance(self.hex, str) or len(self.hex) != width or not set(self.hex) <= _SHA256_DIGITS:
            raise InstallMarkerError("install_marker_invalid", "repository commit must match its sha1 or sha256 object format")

    def to_json(self) -> dict[str, str]:
        return {"object_format": self.object_format, "hex": self.hex}


@dataclass(frozen=True)
class MarkerRepositoryRef:
    kind: str
    value: str

    def __post_init__(self) -> None:
        if not isinstance(self.kind, str) or self.kind not in _SUBSTITUTION_REF_KINDS:
            admitted = ", ".join(sorted(_SUBSTITUTION_REF_KINDS))
            raise InstallMarkerError("install_marker_invalid", f"repository substitution ref kind must be one of {admitted}")
        _require_non_empty_string(self.value, "repository substitution ref value")

    def to_json(self) -> dict[str, str]:
        return {"kind": self.kind, "value": self.value}


@dataclass(frozen=True)
class MarkerRepositorySubstitution:
    type: str
    ref: MarkerRepositoryRef | None = None

    def __post_init__(self) -> None:
        if self.type == "local-path":
            if self.ref is not None:
                raise InstallMarkerError("install_marker_invalid", "local-path substitution must not contain ref")
        elif self.type == "network-git":
            if self.ref is None:
                raise InstallMarkerError("install_marker_invalid", "network-git substitution requires ref")
        else:
            raise InstallMarkerError("install_marker_invalid", f"repository substitution type is invalid: {self.type!r}")

    def to_json(self) -> dict[str, Any]:
        result: dict[str, Any] = {"type": self.type}
        if self.ref is not None:
            result["ref"] = self.ref.to_json()
        return result


def _validate_substitution_agreement(
    substitution: MarkerRepositorySubstitution,
    *,
    effective_identity: MarkerRepositoryIdentity,
    object_format: str,
) -> None:
    """Bind a substitution record to the effective source it claims to describe.

    Core section 4.2 states the two halves separately: a local substitution has
    effective identity kind ``operator-local-git`` and a network substitution
    has ``network-git``, and a structured ``revision`` ref is a full object ID
    *for the effective repository object format*. A marker that satisfies the
    JSON Schema can still contradict either rule, so both are decided here
    rather than left to the schema.
    """

    expected_kind = _SUBSTITUTION_IDENTITY_KINDS[substitution.type]
    if effective_identity.kind != expected_kind:
        raise InstallMarkerError(
            "install_marker_invalid",
            f"{substitution.type} substitution requires effective identity kind "
            f"{expected_kind!r}, got {effective_identity.kind!r}",
        )
    ref = substitution.ref
    if ref is None or ref.kind != "revision":
        return
    width = 40 if object_format == "sha1" else 64
    if len(ref.value) != width or not set(ref.value) <= _SHA256_DIGITS:
        raise InstallMarkerError(
            "install_marker_invalid",
            f"substitution revision must be a full lowercase {object_format} object id",
        )


@dataclass(frozen=True)
class InstallMarkerBuildV3:
    """One marker-v3 local receipt-v1 or external receipt-v2 reference."""

    driver: str
    receipt_schema_version: int
    execution_policy: str
    cache_key: str
    receipt_sha256: str
    artifact_sha256: str
    artifact_path: str
    repository: str | None = None
    declared_identity: MarkerRepositoryIdentity | None = None
    declared_locked_commit: MarkerRepositoryCommit | None = None
    declared_tag: str | None = None
    effective_identity: MarkerRepositoryIdentity | None = None
    object_format: str | None = None
    commit: str | None = None
    substituted: bool | None = None
    substitution: MarkerRepositorySubstitution | None = None
    build_source: BuildSourceIdentity | None = None
    descriptor_target: str | None = None

    def __post_init__(self) -> None:
        _validate_receipted_build(
            self,
            marker_version=INSTALL_MARKER_V3_SCHEMA_VERSION,
            local_receipt_version=1,
            external_receipt_version=2,
        )

    def to_json(self) -> dict[str, Any]:
        return _receipted_build_to_json(self)


def _validate_receipted_build(
    record: InstallMarkerBuildV3 | InstallMarkerBuildV5,
    *,
    marker_version: int,
    local_receipt_version: int,
    external_receipt_version: int,
) -> None:
    """Validate one local or external build record shared by markers v3-v5.

    Marker v5 keeps every v3 cross-field rule intact and only moves the bound
    receipt version to 3, so both record classes validate through this one
    function with their own bound versions.
    """

    if not isinstance(record.receipt_schema_version, int) or isinstance(
        record.receipt_schema_version, bool
    ):
        raise InstallMarkerError(
            "install_marker_invalid", "build receipt_schema_version must be an integer"
        )
    if record.execution_policy != _EXECUTION_POLICY:
        raise InstallMarkerError(
            "install_marker_invalid",
            f"build execution_policy must be {_EXECUTION_POLICY!r}",
        )
    _require_sha256(record.cache_key, "build cache_key")
    _require_sha256(record.receipt_sha256, "build receipt_sha256")
    _require_sha256(record.artifact_sha256, "build artifact_sha256")
    _require_portable_path(record.artifact_path, "build artifact_path")
    external_values = (
        record.repository,
        record.declared_identity,
        record.declared_locked_commit,
        record.declared_tag,
        record.effective_identity,
        record.object_format,
        record.commit,
        record.substituted,
        record.substitution,
        record.build_source,
        record.descriptor_target,
    )
    if record.driver == GO_V1_DRIVER:
        if record.receipt_schema_version != local_receipt_version or any(
            value is not None for value in external_values
        ):
            raise InstallMarkerError(
                "install_marker_invalid",
                f"local go-v1 marker-v{marker_version} build must be receipt "
                f"schema {local_receipt_version} without repository state",
            )
        return
    if record.driver != GO_REPOSITORY_V1_DRIVER or (
        record.receipt_schema_version != external_receipt_version
    ):
        raise InstallMarkerError(
            "install_marker_invalid",
            f"external marker-v{marker_version} build must use go-repository-v1 "
            f"receipt schema {external_receipt_version}",
        )
    if not isinstance(record.substituted, bool):
        raise InstallMarkerError(
            "install_marker_invalid", "external build substituted must be boolean"
        )
    required = (
        record.repository,
        record.declared_identity,
        record.declared_locked_commit,
        record.effective_identity,
        record.object_format,
        record.commit,
        record.build_source,
        record.descriptor_target,
    )
    if any(value is None for value in required):
        raise InstallMarkerError(
            "install_marker_invalid",
            f"external marker-v{marker_version} build is missing repository state",
        )
    assert record.repository is not None
    assert record.declared_identity is not None
    assert record.declared_locked_commit is not None
    assert record.effective_identity is not None
    assert record.object_format is not None
    assert record.build_source is not None
    assert record.descriptor_target is not None
    _require_identifier(record.repository, "external build repository")
    _require_identifier(record.descriptor_target, "external build descriptor_target")
    if not isinstance(record.commit, str):
        raise InstallMarkerError(
            "install_marker_invalid",
            f"external marker-v{marker_version} build commit must be a string",
        )
    MarkerRepositoryCommit(object_format=record.object_format, hex=record.commit)
    _validate_build_source(record.build_source)
    if record.declared_identity.kind != "network-git":
        raise InstallMarkerError(
            "install_marker_invalid",
            "declared repository identity must be network-git",
        )
    if record.substituted != (record.substitution is not None):
        raise InstallMarkerError(
            "install_marker_invalid",
            "substitution must be present exactly when substituted is true",
        )
    if record.substitution is not None:
        _validate_substitution_agreement(
            record.substitution,
            effective_identity=record.effective_identity,
            object_format=record.object_format,
        )
    if not record.substituted and (
        record.effective_identity != record.declared_identity
        or record.object_format != record.declared_locked_commit.object_format
        or record.commit != record.declared_locked_commit.hex
    ):
        raise InstallMarkerError(
            "install_marker_invalid",
            "unsubstituted external source must equal declared state",
        )
    if record.declared_tag is not None:
        _require_non_empty_string(record.declared_tag, "external build declared_tag")


def _receipted_build_to_json(
    record: InstallMarkerBuildV3 | InstallMarkerBuildV5,
) -> dict[str, Any]:
    result: dict[str, Any] = {
        "driver": record.driver,
        "receipt_schema_version": record.receipt_schema_version,
        "execution_policy": record.execution_policy,
        "cache_key": record.cache_key,
        "receipt_sha256": record.receipt_sha256,
        "artifact_sha256": record.artifact_sha256,
        "artifact_path": record.artifact_path,
    }
    if record.driver == GO_REPOSITORY_V1_DRIVER:
        assert record.repository is not None and record.declared_identity is not None
        assert (
            record.declared_locked_commit is not None
            and record.effective_identity is not None
        )
        assert record.object_format is not None and record.commit is not None
        assert record.substituted is not None and record.build_source is not None
        assert record.descriptor_target is not None
        result.update(
            repository=record.repository,
            declared_identity=record.declared_identity.to_json(),
            declared_locked_commit=record.declared_locked_commit.to_json(),
            effective_identity=record.effective_identity.to_json(),
            object_format=record.object_format,
            commit=record.commit,
            substituted=record.substituted,
            build_source={
                "algorithm": record.build_source.algorithm,
                "content_sha256": record.build_source.content_sha256,
            },
            descriptor_target=record.descriptor_target,
        )
        if record.declared_tag is not None:
            result["declared_tag"] = record.declared_tag
        if record.substitution is not None:
            result["substitution"] = record.substitution.to_json()
    return result


@dataclass(frozen=True)
class InstallMarkerBuildV5:
    """One marker-v5 local receipt-v3 or external receipt-v3 reference.

    Every v3 record rule applies unchanged: only the bound receipt version
    moves to 3 for both the local ``go-v1`` and the external
    ``go-repository-v1`` arms. Receipt-3 input binding itself is owned by the
    build-receipts leaf; this record binds the version, hashes, driver and
    execution policy.
    """

    driver: str
    receipt_schema_version: int
    execution_policy: str
    cache_key: str
    receipt_sha256: str
    artifact_sha256: str
    artifact_path: str
    repository: str | None = None
    declared_identity: MarkerRepositoryIdentity | None = None
    declared_locked_commit: MarkerRepositoryCommit | None = None
    declared_tag: str | None = None
    effective_identity: MarkerRepositoryIdentity | None = None
    object_format: str | None = None
    commit: str | None = None
    substituted: bool | None = None
    substitution: MarkerRepositorySubstitution | None = None
    build_source: BuildSourceIdentity | None = None
    descriptor_target: str | None = None

    def __post_init__(self) -> None:
        _validate_receipted_build(
            self,
            marker_version=INSTALL_MARKER_V5_SCHEMA_VERSION,
            local_receipt_version=3,
            external_receipt_version=3,
        )

    def to_json(self) -> dict[str, Any]:
        return _receipted_build_to_json(self)


@dataclass(frozen=True, kw_only=True)
class _InstallMarkerCommon:
    name: str
    source: str
    ref_kind: str
    ref: str
    commit: str
    content_sha256: str
    locale: str | None
    agents: tuple[str, ...]
    commands: tuple[str, ...]
    dependencies: tuple[str, ...]
    skill_schema_version: int
    runtime_roots: tuple[str, ...]
    installed_at: str
    files: tuple[str, ...]
    git: str | None = None
    requirements: tuple[str, ...] | None = None
    mcp_servers: Mapping[str, tuple[str, ...]] | None = None
    attestation: MarkerAttestation | None = None
    activation: MarkerActivation | None = None
    requirers: tuple[str, ...] | None = None
    substituted: str | None = None

    def _validate_common(self, *, maximum_skill_schema: int) -> None:
        _require_identifier(self.name, "name")
        _require_portable_path(self.source, "source")
        if not isinstance(self.ref_kind, str) or self.ref_kind not in _REF_KINDS:
            raise InstallMarkerError(
                "install_marker_invalid",
                f"ref_kind must be tag, branch, or revision, got {self.ref_kind!r}",
            )
        _require_non_empty_string(self.ref, "ref")
        if not isinstance(self.commit, str) or _COMMIT_RE.fullmatch(self.commit) is None:
            raise InstallMarkerError(
                "install_marker_invalid",
                f"commit must be a full lowercase SHA-1 or SHA-256 object id, got {self.commit!r}",
            )
        _require_sha256(self.content_sha256, "content_sha256")
        if self.locale is not None and (
            not isinstance(self.locale, str) or not is_valid_locale(self.locale)
        ):
            raise InstallMarkerError(
                "install_marker_invalid",
                f"locale must be null or a portable locale selector, got {self.locale!r}",
            )
        if (
            not isinstance(self.skill_schema_version, int)
            or isinstance(self.skill_schema_version, bool)
            or not 0 <= self.skill_schema_version <= maximum_skill_schema
        ):
            raise InstallMarkerError(
                "install_marker_invalid",
                "skill_schema_version must be an integer from 0 through "
                f"{maximum_skill_schema}, got {self.skill_schema_version!r}",
            )
        if (
            not isinstance(self.installed_at, str)
            or _TIMESTAMP_RE.fullmatch(self.installed_at) is None
        ):
            raise InstallMarkerError(
                "install_marker_invalid",
                f"installed_at is not a UTC second timestamp: {self.installed_at!r}",
            )
        try:
            datetime.strptime(self.installed_at, "%Y-%m-%dT%H:%M:%SZ")
        except ValueError as exc:
            raise InstallMarkerError(
                "install_marker_invalid",
                f"installed_at is not a valid UTC timestamp: {self.installed_at!r}",
            ) from exc

        object.__setattr__(self, "agents", _freeze_identifier_set(self.agents, "agents"))
        object.__setattr__(self, "commands", _freeze_identifier_set(self.commands, "commands"))
        object.__setattr__(
            self,
            "dependencies",
            _freeze_identifier_set(self.dependencies, "dependencies"),
        )
        object.__setattr__(
            self,
            "runtime_roots",
            _freeze_path_set(self.runtime_roots, "runtime_roots"),
        )
        object.__setattr__(self, "files", _freeze_path_set(self.files, "files"))

        if self.git is not None:
            _require_non_empty_string(self.git, "git")
        if self.requirements is not None:
            object.__setattr__(
                self,
                "requirements",
                _freeze_identifier_set(self.requirements, "requirements"),
            )
        if self.mcp_servers is not None:
            object.__setattr__(
                self,
                "mcp_servers",
                _freeze_mcp_servers(self.mcp_servers),
            )
        if self.attestation is not None and not isinstance(self.attestation, MarkerAttestation):
            raise InstallMarkerError(
                "install_marker_invalid",
                "attestation must be a MarkerAttestation",
            )
        if self.activation is not None and not isinstance(self.activation, MarkerActivation):
            raise InstallMarkerError(
                "install_marker_invalid",
                "activation must be a MarkerActivation",
            )
        if self.requirers is not None:
            object.__setattr__(
                self,
                "requirers",
                _freeze_string_set(self.requirers, "requirers"),
            )
        if self.substituted is not None:
            _require_non_empty_string(self.substituted, "substituted")

    def _common_json(self, schema_version: int) -> dict[str, Any]:
        result: dict[str, Any] = {
            "schema_version": schema_version,
            "name": self.name,
            "source": self.source,
            "ref_kind": self.ref_kind,
            "ref": self.ref,
            "commit": self.commit,
            "content_sha256": self.content_sha256,
            "locale": self.locale,
            "agents": list(self.agents),
            "commands": list(self.commands),
            "dependencies": list(self.dependencies),
            "skill_schema_version": self.skill_schema_version,
            "runtime_roots": list(self.runtime_roots),
            "installed_at": self.installed_at,
            "files": list(self.files),
        }
        if self.git is not None:
            result["git"] = self.git
        if self.requirements is not None:
            result["requirements"] = list(self.requirements)
        if self.mcp_servers is not None:
            result["mcp_servers"] = {
                name: list(agents) for name, agents in self.mcp_servers.items()
            }
        if self.attestation is not None:
            result["attestation"] = self.attestation.to_json()
        if self.activation is not None:
            result["activation"] = self.activation.to_json()
        if self.requirers is not None:
            result["requirers"] = list(self.requirers)
        if self.substituted is not None:
            result["substituted"] = self.substituted
        return result


@dataclass(frozen=True, kw_only=True)
class InstallMarkerV1(_InstallMarkerCommon):
    """Legacy marker schema 1, valid only for skill schemas 0 through 5."""

    schema_version: Literal[1] = INSTALL_MARKER_V1_SCHEMA_VERSION

    def __post_init__(self) -> None:
        if (
            not isinstance(self.schema_version, int)
            or isinstance(self.schema_version, bool)
            or self.schema_version != INSTALL_MARKER_V1_SCHEMA_VERSION
        ):
            raise InstallMarkerError(
                "unsupported_install_marker_schema",
                f"marker v1 schema_version must be 1, got {self.schema_version!r}",
            )
        self._validate_common(maximum_skill_schema=5)

    def to_json(self) -> dict[str, Any]:
        return self._common_json(INSTALL_MARKER_V1_SCHEMA_VERSION)


@dataclass(frozen=True, kw_only=True)
class InstallMarkerV2(_InstallMarkerCommon):
    """Marker schema 2 with local ``go-v1`` build references."""

    build_roots: tuple[str, ...]
    builds: Mapping[str, InstallMarkerBuild]
    build_source: BuildSourceIdentity | None = None
    schema_version: Literal[2] = INSTALL_MARKER_V2_SCHEMA_VERSION

    def __post_init__(self) -> None:
        if (
            not isinstance(self.schema_version, int)
            or isinstance(self.schema_version, bool)
            or self.schema_version != INSTALL_MARKER_V2_SCHEMA_VERSION
        ):
            raise InstallMarkerError(
                "unsupported_install_marker_schema",
                f"marker v2 schema_version must be 2, got {self.schema_version!r}",
            )
        self._validate_common(maximum_skill_schema=6)
        object.__setattr__(
            self,
            "build_roots",
            _freeze_path_set(self.build_roots, "build_roots"),
        )
        object.__setattr__(self, "builds", _freeze_builds(self.builds))

        has_builds = bool(self.builds)
        if has_builds != (self.build_source is not None):
            requirement = "present" if has_builds else "absent"
            raise InstallMarkerError(
                "install_marker_invalid",
                f"build_source must be {requirement} exactly when builds is non-empty",
            )
        if self.build_source is not None:
            _validate_build_source(self.build_source)
        if has_builds:
            if self.skill_schema_version != 6:
                raise InstallMarkerError(
                    "install_marker_invalid",
                    "non-empty marker-v2 builds require skill_schema_version 6",
                )
            if not self.build_roots:
                raise InstallMarkerError(
                    "install_marker_invalid",
                    "non-empty marker-v2 builds require at least one build root",
                )
            for name, build in self.builds.items():
                if name not in self.commands:
                    raise InstallMarkerError(
                        "install_marker_invalid",
                        f"build {name!r} is not present in commands",
                    )
                _validate_build_artifact_path(name, build.artifact_path)

    def to_json(self) -> dict[str, Any]:
        result = self._common_json(INSTALL_MARKER_V2_SCHEMA_VERSION)
        result["build_roots"] = list(self.build_roots)
        result["builds"] = {
            name: build.to_json() for name, build in self.builds.items()
        }
        if self.build_source is not None:
            result["build_source"] = {
                "algorithm": self.build_source.algorithm,
                "content_sha256": self.build_source.content_sha256,
            }
        return result


@dataclass(frozen=True, kw_only=True)
class _InstallMarkerExternalCapable(_InstallMarkerCommon):
    """Shared marker body for local, external, and mixed command sets."""

    schema_version: int
    build_roots: tuple[str, ...]
    builds: Mapping[str, InstallMarkerBuildV3]
    build_source: BuildSourceIdentity | None = None

    def _validate_external_capable(self, *, marker_version: int, skill_schema: int) -> None:
        if self.schema_version != marker_version:
            raise InstallMarkerError(
                "unsupported_install_marker_schema",
                f"marker v{marker_version} schema_version must be {marker_version}, "
                f"got {self.schema_version!r}",
            )
        self._validate_common(maximum_skill_schema=skill_schema)
        if self.skill_schema_version != skill_schema:
            raise InstallMarkerError(
                "install_marker_invalid",
                f"marker v{marker_version} requires skill_schema_version {skill_schema}",
            )
        object.__setattr__(self, "build_roots", _freeze_path_set(self.build_roots, "build_roots"))
        object.__setattr__(self, "builds", _freeze_builds_v3(self.builds))
        local_builds = [build for build in self.builds.values() if build.driver == GO_V1_DRIVER]
        if bool(local_builds) != (self.build_source is not None):
            requirement = "present" if local_builds else "absent"
            raise InstallMarkerError("install_marker_invalid", f"build_source must be {requirement} exactly when local go-v1 builds are present")
        if self.build_source is not None:
            _validate_build_source(self.build_source)
        if local_builds and not self.build_roots:
            raise InstallMarkerError(
                "install_marker_invalid",
                f"local marker-v{marker_version} builds require build_roots",
            )
        for name, build in self.builds.items():
            if name not in self.commands:
                raise InstallMarkerError("install_marker_invalid", f"build {name!r} is not present in commands")
            _validate_build_artifact_path(name, build.artifact_path)

    def _external_capable_json(self, marker_version: int) -> dict[str, Any]:
        result = self._common_json(marker_version)
        result["build_roots"] = list(self.build_roots)
        result["builds"] = {name: build.to_json() for name, build in self.builds.items()}
        if self.build_source is not None:
            result["build_source"] = {
                "algorithm": self.build_source.algorithm,
                "content_sha256": self.build_source.content_sha256,
            }
        return result


@dataclass(frozen=True, kw_only=True)
class InstallMarkerV3(_InstallMarkerExternalCapable):
    """Schema-7 marker supporting local and external compiled commands."""

    schema_version: Literal[3] = INSTALL_MARKER_V3_SCHEMA_VERSION

    def __post_init__(self) -> None:
        self._validate_external_capable(
            marker_version=INSTALL_MARKER_V3_SCHEMA_VERSION,
            skill_schema=7,
        )

    def to_json(self) -> dict[str, Any]:
        return self._external_capable_json(INSTALL_MARKER_V3_SCHEMA_VERSION)


@dataclass(frozen=True, kw_only=True)
class InstallMarkerV4(_InstallMarkerExternalCapable):
    """Schema-8 marker: marker-v3 meaning over a schema-8 manifest.

    An enforced ``script-worker-v1`` command produces no build entry and adds
    no marker member, so the object shape, the build-entry semantics, and the
    top-level ``build_source`` and ``build_roots`` rules are marker-v3's.
    """

    schema_version: Literal[4] = INSTALL_MARKER_V4_SCHEMA_VERSION

    def __post_init__(self) -> None:
        self._validate_external_capable(
            marker_version=INSTALL_MARKER_V4_SCHEMA_VERSION,
            skill_schema=8,
        )

    def to_json(self) -> dict[str, Any]:
        return self._external_capable_json(INSTALL_MARKER_V4_SCHEMA_VERSION)


@dataclass(frozen=True, kw_only=True)
class InstallMarkerV5:
    """Schema-2 installation marker: typed package plus lock binding.

    ``package`` replaces the legacy ``source``/``git``/``ref_kind``/``ref``/
    ``commit`` identity and ``lock_sha256`` binds the installed selection;
    every other member keeps its core section 10 meaning. ``attestation`` and
    top-level ``substituted`` are forbidden for ``local-snapshot`` packages.
    ``skill_schema_version`` is the actual manifest version, 1 through 8:
    schema-2 installations write marker 5 regardless of manifest version.
    """

    name: str
    package: source_package.PackageIdentity
    lock_sha256: str
    content_sha256: str
    locale: str | None
    agents: tuple[str, ...]
    commands: tuple[str, ...]
    dependencies: tuple[str, ...]
    skill_schema_version: int
    runtime_roots: tuple[str, ...]
    build_roots: tuple[str, ...]
    installed_at: str
    files: tuple[str, ...]
    builds: Mapping[str, InstallMarkerBuildV5]
    schema_version: Literal[5] = INSTALL_MARKER_V5_SCHEMA_VERSION
    build_source: BuildSourceIdentity | None = None
    requirements: tuple[str, ...] | None = None
    mcp_servers: Mapping[str, tuple[str, ...]] | None = None
    attestation: MarkerAttestation | None = None
    activation: MarkerActivation | None = None
    requirers: tuple[str, ...] | None = None
    substituted: str | None = None

    def __post_init__(self) -> None:
        if (
            not isinstance(self.schema_version, int)
            or isinstance(self.schema_version, bool)
            or self.schema_version != INSTALL_MARKER_V5_SCHEMA_VERSION
        ):
            raise InstallMarkerError(
                "unsupported_install_marker_schema",
                f"marker v5 schema_version must be 5, got {self.schema_version!r}",
            )
        _require_identifier(self.name, "name")
        if not isinstance(
            self.package,
            (
                source_package.LocalSnapshot,
                source_package.NetworkGit,
                source_package.ConfiguredGit,
            ),
        ):
            raise InstallMarkerError(
                "install_marker_invalid",
                "package must be a local-snapshot, network-git, or configured-git identity",
            )
        _require_sha256(self.lock_sha256, "lock_sha256")
        _require_sha256(self.content_sha256, "content_sha256")
        _validate_optional_locale(self.locale, "locale")
        _require_manifest_schema_version(self.skill_schema_version, "skill_schema_version")
        if (
            not isinstance(self.installed_at, str)
            or _TIMESTAMP_RE.fullmatch(self.installed_at) is None
        ):
            raise InstallMarkerError(
                "install_marker_invalid",
                f"installed_at is not a UTC second timestamp: {self.installed_at!r}",
            )
        try:
            datetime.strptime(self.installed_at, "%Y-%m-%dT%H:%M:%SZ")
        except ValueError as exc:
            raise InstallMarkerError(
                "install_marker_invalid",
                f"installed_at is not a valid UTC timestamp: {self.installed_at!r}",
            ) from exc

        object.__setattr__(self, "agents", _freeze_identifier_set(self.agents, "agents"))
        object.__setattr__(self, "commands", _freeze_identifier_set(self.commands, "commands"))
        object.__setattr__(
            self,
            "dependencies",
            _freeze_identifier_set(self.dependencies, "dependencies"),
        )
        object.__setattr__(
            self,
            "runtime_roots",
            _freeze_path_set(self.runtime_roots, "runtime_roots"),
        )
        object.__setattr__(
            self,
            "build_roots",
            _freeze_path_set(self.build_roots, "build_roots"),
        )
        object.__setattr__(self, "files", _freeze_path_set(self.files, "files"))

        if self.requirements is not None:
            object.__setattr__(
                self,
                "requirements",
                _freeze_identifier_set(self.requirements, "requirements"),
            )
        if self.mcp_servers is not None:
            object.__setattr__(
                self,
                "mcp_servers",
                _freeze_mcp_servers(self.mcp_servers),
            )
        if self.attestation is not None and not isinstance(
            self.attestation, MarkerAttestation
        ):
            raise InstallMarkerError(
                "install_marker_invalid",
                "attestation must be a MarkerAttestation",
            )
        if self.activation is not None and not isinstance(
            self.activation, MarkerActivation
        ):
            raise InstallMarkerError(
                "install_marker_invalid",
                "activation must be a MarkerActivation",
            )
        if self.requirers is not None:
            object.__setattr__(
                self,
                "requirers",
                _freeze_string_set(self.requirers, "requirers"),
            )
        if self.substituted is not None:
            _require_non_empty_string(self.substituted, "substituted")

        if isinstance(self.package, source_package.LocalSnapshot):
            if self.attestation is not None:
                raise InstallMarkerError(
                    "install_marker_invalid",
                    "attestation is forbidden for a local-snapshot package",
                )
            if self.substituted is not None:
                raise InstallMarkerError(
                    "install_marker_invalid",
                    "substituted is forbidden for a local-snapshot package",
                )

        object.__setattr__(self, "builds", _freeze_builds_v5(self.builds))
        # The reader only validates the shape of a recorded build source: the
        # pinned corpus carries markers with a build source but no recorded
        # builds, so the "required exactly for active local go-v1 commands"
        # rule is a plan comparison, not a reader gate.
        if self.build_source is not None:
            _validate_build_source(self.build_source)
        _validate_builds_against_commands(self.builds, self.commands)

    def to_json(self) -> dict[str, Any]:
        result: dict[str, Any] = {
            "schema_version": INSTALL_MARKER_V5_SCHEMA_VERSION,
            "name": self.name,
            "package": source_package.package_identity_to_json(self.package),
            "lock_sha256": self.lock_sha256,
            "content_sha256": self.content_sha256,
            "locale": self.locale,
            "agents": list(self.agents),
            "commands": list(self.commands),
            "dependencies": list(self.dependencies),
            "skill_schema_version": self.skill_schema_version,
            "runtime_roots": list(self.runtime_roots),
            "build_roots": list(self.build_roots),
            "installed_at": self.installed_at,
            "files": list(self.files),
            "builds": {
                name: build.to_json() for name, build in self.builds.items()
            },
        }
        if self.build_source is not None:
            result["build_source"] = {
                "algorithm": self.build_source.algorithm,
                "content_sha256": self.build_source.content_sha256,
            }
        if self.requirements is not None:
            result["requirements"] = list(self.requirements)
        if self.mcp_servers is not None:
            result["mcp_servers"] = {
                name: list(agents) for name, agents in self.mcp_servers.items()
            }
        if self.attestation is not None:
            result["attestation"] = self.attestation.to_json()
        if self.activation is not None:
            result["activation"] = self.activation.to_json()
        if self.requirers is not None:
            result["requirers"] = list(self.requirers)
        if self.substituted is not None:
            result["substituted"] = self.substituted
        return result


MarkerBuild: TypeAlias = InstallMarkerBuild | InstallMarkerBuildV3 | InstallMarkerBuildV5
InstallMarker: TypeAlias = (
    InstallMarkerV1
    | InstallMarkerV2
    | InstallMarkerV3
    | InstallMarkerV4
    | InstallMarkerV5
)


def serialize_install_marker(payload: Mapping[str, Any]) -> bytes:
    """Render one marker payload as the exact `.csk-install.json` bytes.

    The rendering is UTF-8 with LF line endings on every platform. Returning
    bytes keeps the caller on `Path.write_bytes`, because `Path.write_text`
    would translate each LF to `os.linesep` and commit CRLF markers on Windows.
    """
    return (json.dumps(dict(payload), indent=2, sort_keys=True) + "\n").encode("utf-8")


def read_install_marker(raw: bytes | str) -> InstallMarker:
    """Read marker JSON while rejecting duplicate keys and unsafe numbers."""
    try:
        value = protocol_json.loads_canonical(raw)
    except protocol_json.ProtocolJSONError as exc:
        raise InstallMarkerError(
            "install_marker_invalid",
            f"marker is not valid protocol JSON: {exc}",
        ) from exc
    return parse_install_marker(value)


def parse_install_marker(value: Any) -> InstallMarker:
    """Parse one decoded marker schema 1 through 5 and freeze set ordering."""
    body = _require_object(value, "marker")
    schema_version = body.get("schema_version")
    if (
        not isinstance(schema_version, int)
        or isinstance(schema_version, bool)
        or schema_version not in SUPPORTED_INSTALL_MARKER_SCHEMA_VERSIONS
    ):
        raise InstallMarkerError(
            "unsupported_install_marker_schema",
            f"unsupported schema_version {schema_version!r}",
        )
    if schema_version == INSTALL_MARKER_V1_SCHEMA_VERSION:
        _validate_object_shape(body, _COMMON_REQUIRED_MEMBERS, _V1_MEMBERS, "marker v1")
        return InstallMarkerV1(
            **_parse_common(body),
            schema_version=INSTALL_MARKER_V1_SCHEMA_VERSION,
        )

    if schema_version == INSTALL_MARKER_V2_SCHEMA_VERSION:
        _validate_object_shape(body, _V2_REQUIRED_MEMBERS, _V2_MEMBERS, "marker v2")
        return InstallMarkerV2(
            **_parse_common(body),
            build_roots=_parse_array(body["build_roots"], "build_roots"),
            builds=_parse_builds(body["builds"]),
            build_source=(
                _parse_build_source(body["build_source"])
                if "build_source" in body
                else None
            ),
            schema_version=INSTALL_MARKER_V2_SCHEMA_VERSION,
        )

    if schema_version == INSTALL_MARKER_V3_SCHEMA_VERSION:
        _validate_object_shape(body, _V3_REQUIRED_MEMBERS, _V3_MEMBERS, "marker v3")
        return InstallMarkerV3(
            **_parse_common(body),
            build_roots=_parse_array(body["build_roots"], "build_roots"),
            builds=_parse_builds_v3(body["builds"]),
            build_source=(
                _parse_build_source(body["build_source"])
                if "build_source" in body
                else None
            ),
            schema_version=INSTALL_MARKER_V3_SCHEMA_VERSION,
        )

    if schema_version == INSTALL_MARKER_V4_SCHEMA_VERSION:
        _validate_object_shape(body, _V4_REQUIRED_MEMBERS, _V4_MEMBERS, "marker v4")
        return InstallMarkerV4(
            **_parse_common(body),
            build_roots=_parse_array(body["build_roots"], "build_roots"),
            builds=_parse_builds_v3(body["builds"]),
            build_source=(
                _parse_build_source(body["build_source"])
                if "build_source" in body
                else None
            ),
            schema_version=INSTALL_MARKER_V4_SCHEMA_VERSION,
        )

    _validate_object_shape(body, _V5_REQUIRED_MEMBERS, _V5_MEMBERS, "marker v5")
    return InstallMarkerV5(
        **_parse_v5_common(body),
        build_roots=_parse_array(body["build_roots"], "build_roots"),
        builds=_parse_builds_v5(body["builds"]),
        build_source=(
            _parse_build_source(body["build_source"])
            if "build_source" in body
            else None
        ),
        schema_version=INSTALL_MARKER_V5_SCHEMA_VERSION,
    )


def marker_can_be_current(
    marker: InstallMarker,
    *,
    skill_schema_version: int,
) -> bool:
    """Return schema-level currentness compatibility for a parsed marker."""
    if (
        not isinstance(skill_schema_version, int)
        or isinstance(skill_schema_version, bool)
        or marker.skill_schema_version != skill_schema_version
    ):
        return False
    if isinstance(marker, InstallMarkerV1):
        return 0 <= skill_schema_version <= 5
    if isinstance(marker, InstallMarkerV2):
        return 0 <= skill_schema_version <= 6
    if isinstance(marker, InstallMarkerV3):
        return skill_schema_version == 7
    if isinstance(marker, InstallMarkerV4):
        return skill_schema_version == 8
    return 1 <= skill_schema_version <= 8


@dataclass(frozen=True, kw_only=True)
class MarkerPlan:
    """Marker-comparable projection of the schema-2 effective plan.

    The effective plan itself is owned by resolve-source-closure; this frozen
    view holds the expected value of every record member status compares,
    plus the context hash the registry-evidence check binds. ``attestation``
    is present exactly when the plan selects existing registry evidence, and
    equals its registry, status and key id, including key absence. Both
    ``attestation`` and ``substituted`` are forbidden for ``local-snapshot``
    packages, so a valid plan can never publish them onto local content.
    ``installed_at`` is the only record member with no plan expectation: it
    records when the bytes landed, which no plan can predict.
    """

    name: str
    package: source_package.PackageIdentity
    lock_sha256: str
    context_sha256: str
    content_sha256: str
    locale: str | None
    agents: tuple[str, ...]
    commands: tuple[str, ...]
    dependencies: tuple[str, ...]
    skill_schema_version: int
    runtime_roots: tuple[str, ...]
    build_roots: tuple[str, ...]
    files: tuple[str, ...]
    builds: Mapping[str, InstallMarkerBuildV5]
    requirements: tuple[str, ...] | None
    mcp_servers: Mapping[str, tuple[str, ...]] | None
    activation: MarkerActivation | None
    requirers: tuple[str, ...] | None
    attestation: MarkerAttestation | None = None
    substituted: str | None = None
    build_source: BuildSourceIdentity | None = None

    def __post_init__(self) -> None:
        _require_identifier(self.name, "plan name")
        if not isinstance(
            self.package,
            (
                source_package.LocalSnapshot,
                source_package.NetworkGit,
                source_package.ConfiguredGit,
            ),
        ):
            raise InstallMarkerError(
                "install_marker_invalid",
                "plan package must be a local-snapshot, network-git, or configured-git identity",
            )
        _require_sha256(self.lock_sha256, "plan lock_sha256")
        _require_sha256(self.context_sha256, "plan context_sha256")
        _require_sha256(self.content_sha256, "plan content_sha256")
        _validate_optional_locale(self.locale, "plan locale")
        _require_manifest_schema_version(
            self.skill_schema_version, "plan skill_schema_version"
        )
        object.__setattr__(
            self, "agents", _freeze_identifier_set(self.agents, "plan agents")
        )
        object.__setattr__(
            self, "commands", _freeze_identifier_set(self.commands, "plan commands")
        )
        object.__setattr__(
            self,
            "dependencies",
            _freeze_identifier_set(self.dependencies, "plan dependencies"),
        )
        object.__setattr__(
            self,
            "runtime_roots",
            _freeze_path_set(self.runtime_roots, "plan runtime_roots"),
        )
        object.__setattr__(
            self,
            "build_roots",
            _freeze_path_set(self.build_roots, "plan build_roots"),
        )
        object.__setattr__(
            self, "files", _freeze_path_set(self.files, "plan files")
        )
        if self.requirements is not None:
            object.__setattr__(
                self,
                "requirements",
                _freeze_identifier_set(self.requirements, "plan requirements"),
            )
        if self.mcp_servers is not None:
            object.__setattr__(
                self, "mcp_servers", _freeze_mcp_servers(self.mcp_servers)
            )
        if self.activation is not None and not isinstance(
            self.activation, MarkerActivation
        ):
            raise InstallMarkerError(
                "install_marker_invalid",
                "plan activation must be a MarkerActivation",
            )
        if self.requirers is not None:
            object.__setattr__(
                self,
                "requirers",
                _freeze_string_set(self.requirers, "plan requirers"),
            )
        object.__setattr__(self, "builds", _freeze_builds_v5(self.builds))
        _validate_builds_against_commands(self.builds, self.commands)
        if self.attestation is not None and not isinstance(
            self.attestation, MarkerAttestation
        ):
            raise InstallMarkerError(
                "install_marker_invalid",
                "plan attestation must be a MarkerAttestation",
            )
        if self.substituted is not None:
            _require_non_empty_string(self.substituted, "plan substituted")
        if self.build_source is not None:
            _validate_build_source(self.build_source)
        if isinstance(self.package, source_package.LocalSnapshot):
            if self.attestation is not None:
                raise InstallMarkerError(
                    "install_marker_invalid",
                    "plan attestation is forbidden for a local-snapshot package",
                )
            if self.substituted is not None:
                raise InstallMarkerError(
                    "install_marker_invalid",
                    "plan substitution is forbidden for a local-snapshot package",
                )


# Members of InstallMarkerV5 that status never compares: the constant schema
# version, which the reader already pins, and the install timestamp, which
# records when the bytes landed rather than what the plan selected. Every
# other record member is compared. The growth test pins this exclusion set,
# so narrowing it is a conscious edit, not a silent drift.
_MARKER_PLAN_UNCOMPARED_MEMBERS: Final[frozenset[str]] = frozenset(
    {"schema_version", "installed_at"}
)
# The attestation member expands to one row per compared value so key absence
# stays a compared value rather than a skipped member.
_MARKER_PLAN_ATTESTATION_ROWS: Final[tuple[str, str, str]] = (
    "registry",
    "status",
    "key_id",
)
# The one member table driving marker-plan comparison, in record order: every
# InstallMarkerV5 member except the exclusions above. The public row table and
# the comparison below both derive from it, so the table drives rather than
# pins: a new record member extends the comparison by construction, and the
# growth test fails unless the plan carries it too.
_MARKER_PLAN_COMPARED_MEMBERS: Final[tuple[str, ...]] = tuple(
    member.name
    for member in dataclass_fields(InstallMarkerV5)
    if member.name not in _MARKER_PLAN_UNCOMPARED_MEMBERS
)


# The one row table driving marker-plan comparison: the compared members with
# the attestation triple expanded. It covers the six marker-plan-mismatch
# conformance rows (package, lock_sha256, registry, status, key_id,
# substituted) plus every retained core section 10 comparison; any difference
# makes the installation non-current. The comparison test is parametrised over
# this same table, so a new row cannot be added without a case.
MARKER_PLAN_COMPARISON_FIELDS: Final[tuple[str, ...]] = tuple(
    row
    for member in _MARKER_PLAN_COMPARED_MEMBERS
    for row in (
        _MARKER_PLAN_ATTESTATION_ROWS if member == "attestation" else (member,)
    )
)

# Attestation absence marker for the registry/status/key_id triple: absence is
# a compared value, so a plan without attestation differs from a marker with
# one on all three rows, while two absent attestations agree.
_ABSENT_ATTESTATION: Final = object()


def compare_marker_plan(
    marker: InstallMarkerV5, plan: MarkerPlan
) -> tuple[str, ...]:
    """Compare one marker against the effective plan member by member.

    Returns the compared rows that differ, in record order; an empty tuple
    means the marker agrees with the plan on every compared member. Key
    absence is a compared value: a plan key id of ``None`` matches only a
    marker key id of ``None``.
    """

    differing: list[str] = []
    for member in _MARKER_PLAN_COMPARED_MEMBERS:
        if member == "attestation":
            marker_triple = (
                (
                    marker.attestation.registry,
                    marker.attestation.status,
                    marker.attestation.key_id,
                )
                if marker.attestation is not None
                else (
                    _ABSENT_ATTESTATION,
                    _ABSENT_ATTESTATION,
                    _ABSENT_ATTESTATION,
                )
            )
            plan_triple = (
                (
                    plan.attestation.registry,
                    plan.attestation.status,
                    plan.attestation.key_id,
                )
                if plan.attestation is not None
                else (
                    _ABSENT_ATTESTATION,
                    _ABSENT_ATTESTATION,
                    _ABSENT_ATTESTATION,
                )
            )
            differing.extend(
                row
                for row, marker_value, plan_value in zip(
                    _MARKER_PLAN_ATTESTATION_ROWS, marker_triple, plan_triple
                )
                if marker_value != plan_value
            )
        elif getattr(marker, member) != getattr(plan, member):
            differing.append(member)
    return tuple(differing)


def marker_plan_is_current(marker: InstallMarkerV5, plan: MarkerPlan) -> bool:
    """Return whether one marker agrees with the effective plan on every row."""
    return not compare_marker_plan(marker, plan)


@dataclass(frozen=True, kw_only=True)
class RegistryEvidence:
    """One required registry attestation record, as persisted evidence.

    The record binds the exact skill name, canonical repository, commit and
    context hash the registry rules established, plus the optional signing key
    id. Freshness, revocation and signature trust are verdicts of the assurance
    layer, passed explicitly to the validator: this summary never authorizes
    anything on its own.
    """

    name: str
    repository: str
    commit: source_package.LockedCommit
    context_sha256: str
    key_id: str | None = None

    def __post_init__(self) -> None:
        _require_identifier(self.name, "evidence name")
        if not isinstance(self.commit, source_package.LockedCommit):
            raise InstallMarkerError(
                "install_marker_invalid",
                "evidence commit must be a LockedCommit",
            )
        _require_sha256(self.context_sha256, "evidence context_sha256")
        if self.key_id is not None and (
            not isinstance(self.key_id, str) or _KEY_ID_RE.fullmatch(self.key_id) is None
        ):
            raise InstallMarkerError(
                "install_marker_invalid",
                f"evidence key_id must be 16 lowercase hexadecimal digits, got {self.key_id!r}",
            )
        try:
            source_package.NetworkGit(
                repository=self.repository,
                commit=self.commit,
                directory=".",
            )
        except source_errors.SourceError as exc:
            raise InstallMarkerError(
                "install_marker_invalid",
                f"evidence repository is not a canonical repository identity: {exc.detail}",
            ) from exc

    def to_json(self) -> dict[str, Any]:
        result: dict[str, Any] = {
            "name": self.name,
            "repository": self.repository,
            "commit": {
                "object_format": self.commit.object_format,
                "hex": self.commit.hex,
            },
            "context_sha256": self.context_sha256,
        }
        if self.key_id is not None:
            result["key_id"] = self.key_id
        return result


@dataclass(frozen=True, kw_only=True)
class AttestationExpectation:
    """Exact registry evidence the effective plan requires.

    For ``network-git`` plans the repository is derived from the package; for
    legacy ``configured-git`` plans it is the canonical repository the
    registry rules established, which the caller supplies because a configured
    source path alone is not a registry identity.
    """

    name: str
    repository: str
    commit: source_package.LockedCommit
    context_sha256: str
    key_id: str | None = None


# The one defect table driving required-evidence validation. Each defect maps
# to exactly one refusal of validate_attestation_evidence; the evidence test is
# parametrised over this same table, so a new defect cannot be added without a
# fixture and a refusal assertion.
ATTESTATION_EVIDENCE_DEFECTS: Final[tuple[str, ...]] = (
    "absent",
    "unreadable",
    "malformed",
    "stale",
    "revoked",
    "wrong-name",
    "wrong-repository",
    "wrong-commit",
    "wrong-context",
    "wrong-key",
)


def parse_registry_evidence(value: Any) -> RegistryEvidence:
    """Parse one persisted registry evidence record."""

    body = _require_object(value, "registry evidence")
    _validate_object_shape(
        body,
        frozenset({"name", "repository", "commit", "context_sha256"}),
        frozenset({"name", "repository", "commit", "context_sha256", "key_id"}),
        "registry evidence",
    )
    try:
        commit = source_package.parse_locked_commit(body["commit"], subject="evidence commit")
    except source_errors.SourceError as exc:
        raise InstallMarkerError(
            "install_marker_invalid", f"registry evidence is malformed: {exc.detail}"
        ) from exc
    return RegistryEvidence(
        name=body["name"],
        repository=body["repository"],
        commit=commit,
        context_sha256=body["context_sha256"],
        key_id=body.get("key_id"),
    )


def validate_attestation_evidence(
    evidence_path: Path,
    expectation: AttestationExpectation,
    *,
    evidence_fresh: bool,
    evidence_revoked: bool,
) -> RegistryEvidence:
    """Validate required registry evidence against the plan expectation.

    The one evidence validator for the ten attestation-evidence defects:
    missing, unreadable, malformed, stale or mismatching required evidence
    refuses and must never become an unattested successful installation or a
    current status. An unreadable record is never reported as absent. Returns
    the validated record on success.
    """

    try:
        raw = evidence_path.read_bytes()
    except FileNotFoundError as exc:
        raise InstallMarkerError(
            "attestation_evidence_missing",
            f"required registry evidence is absent: {evidence_path}",
        ) from exc
    except OSError as exc:
        raise InstallMarkerError(
            "attestation_evidence_unreadable",
            f"required registry evidence is unreadable: {evidence_path}: {exc}",
        ) from exc
    try:
        value = protocol_json.loads_canonical(raw)
    except protocol_json.ProtocolJSONError as exc:
        raise InstallMarkerError(
            "attestation_evidence_malformed",
            f"required registry evidence is malformed: {exc}",
        ) from exc
    try:
        evidence = parse_registry_evidence(value)
    except InstallMarkerError as exc:
        raise InstallMarkerError(
            "attestation_evidence_malformed",
            f"required registry evidence is malformed: {exc.detail}",
        ) from exc
    if not evidence_fresh:
        raise InstallMarkerError(
            "attestation_evidence_stale",
            "required registry evidence is stale",
        )
    if evidence_revoked:
        raise InstallMarkerError(
            "attestation_evidence_revoked",
            "required registry evidence is revoked",
        )
    if evidence.name != expectation.name:
        raise InstallMarkerError(
            "attestation_evidence_mismatch",
            f"evidence name {evidence.name!r} differs from the plan name {expectation.name!r}",
        )
    if evidence.repository != expectation.repository:
        raise InstallMarkerError(
            "attestation_evidence_mismatch",
            "evidence repository differs from the plan repository",
        )
    if evidence.commit != expectation.commit:
        raise InstallMarkerError(
            "attestation_evidence_mismatch",
            "evidence commit differs from the plan commit",
        )
    if evidence.context_sha256 != expectation.context_sha256:
        raise InstallMarkerError(
            "attestation_evidence_mismatch",
            "evidence context hash differs from the plan context hash",
        )
    if evidence.key_id != expectation.key_id:
        raise InstallMarkerError(
            "attestation_evidence_mismatch",
            "evidence key id differs from the plan key id, including key absence",
        )
    return evidence


def check_local_registry_requirement(
    package: source_package.PackageIdentity,
    *,
    network_attestation_required: bool,
) -> None:
    """Reject local content where policy requires a network attestation.

    Local inputs have no network registry identity and no record is forged for
    them: where policy requires a network attestation that local content
    cannot supply, installation fails with no publication. Not a downgrade,
    not an unattested success.
    """

    if network_attestation_required and isinstance(package, source_package.LocalSnapshot):
        raise InstallMarkerError(
            "local_registry_attestation_required",
            "policy requires a network attestation that local content cannot supply",
        )


def check_top_level_build_source(
    builds: Mapping[str, InstallMarkerBuildV5],
    build_source: BuildSourceIdentity | None,
) -> None:
    """Require top-level build source exactly for active local go-v1 records.

    The top-level ``build_source`` must be present exactly when the builds map
    carries at least one active local ``go-v1`` record, and absent otherwise:
    an external-only build binds its source solely per external record, and an
    empty builds map carries no build source at all. Both directions refuse.
    """

    has_local = any(
        build.driver == GO_V1_DRIVER for build in builds.values()
    )
    if has_local == (build_source is None):
        if has_local:
            raise InstallMarkerError(
                "top_level_build_source_mismatch",
                "top-level build_source is required for active local go-v1 builds",
            )
        raise InstallMarkerError(
            "top_level_build_source_mismatch",
            "top-level build_source must be absent without active local go-v1 builds",
        )


def check_external_only_build_state(
    marker: InstallMarkerV5,
    *,
    receipt_package: source_package.PackageIdentity,
) -> None:
    """Admit the external-only build shape for a local package.

    The marker must describe a local snapshot with no top-level build source
    and no local go-v1 records, and the receipt package must equal the marker
    package. Receipt-3 ``input.build`` matching and protected-artifact
    verification are owned by the build-receipts comparison in
    ``csk.builds.currentness.compare_external_build_evidence``: this check
    binds the marker shape and the package equality only.
    """

    if not isinstance(marker.package, source_package.LocalSnapshot):
        raise InstallMarkerError(
            "external_only_build_mismatch",
            "external-only build state requires a local-snapshot package",
        )
    if marker.build_source is not None:
        raise InstallMarkerError(
            "external_only_build_mismatch",
            "external-only build state requires an absent top-level build source",
        )
    for build_name, record in marker.builds.items():
        if record.driver != GO_REPOSITORY_V1_DRIVER:
            raise InstallMarkerError(
                "external_only_build_mismatch",
                f"external-only build state admits no local record: {build_name!r}",
            )
    if receipt_package != marker.package:
        raise InstallMarkerError(
            "external_only_build_mismatch",
            "receipt package differs from the marker package",
        )


def build_install_marker_v5(
    plan: MarkerPlan,
    *,
    content_sha256: str,
    files: tuple[str, ...] | list[str],
    locale: str | None,
    agents: tuple[str, ...] | list[str],
    commands: tuple[str, ...] | list[str],
    dependencies: tuple[str, ...] | list[str],
    skill_schema_version: int,
    runtime_roots: tuple[str, ...] | list[str],
    build_roots: tuple[str, ...] | list[str],
    installed_at: str,
    builds: Mapping[str, InstallMarkerBuildV5],
    requirements: tuple[str, ...] | list[str] | None = None,
    mcp_servers: Mapping[str, tuple[str, ...] | list[str]] | None = None,
    activation: MarkerActivation | None = None,
    requirers: tuple[str, ...] | list[str] | None = None,
) -> InstallMarkerV5:
    """Write a schema-2 marker from the effective plan (the v5 writer).

    ``package``, ``lock_sha256``, ``attestation``, ``substituted`` and
    ``build_source`` are derived from the plan, never passed alongside it, so
    the writer cannot record a summary the plan did not select. A valid plan
    already forbids attestation and substitution for local snapshots; the
    marker constructor enforces the same rule on the way out.
    """

    return InstallMarkerV5(
        name=plan.name,
        package=plan.package,
        lock_sha256=plan.lock_sha256,
        content_sha256=content_sha256,
        locale=locale,
        agents=tuple(agents),
        commands=tuple(commands),
        dependencies=tuple(dependencies),
        skill_schema_version=skill_schema_version,
        runtime_roots=tuple(runtime_roots),
        build_roots=tuple(build_roots),
        installed_at=installed_at,
        files=tuple(files),
        builds=builds,
        build_source=plan.build_source,
        requirements=tuple(requirements) if requirements is not None else None,
        mcp_servers=(
            {name: tuple(found) for name, found in mcp_servers.items()}
            if mcp_servers is not None
            else None
        ),
        attestation=plan.attestation,
        activation=activation,
        requirers=tuple(requirers) if requirers is not None else None,
        substituted=plan.substituted,
    )


@dataclass(frozen=True)
class MarkerStatusVerdict:
    """Read-only schema-2 status verdict for one installed skill.

    The exit code derives from currency so the two cannot disagree: current
    exits 0, every non-current or unknown state exits nonzero.
    """

    current: bool
    differences: tuple[str, ...] = ()
    detail: str = ""

    @property
    def exit_code(self) -> int:
        return 0 if self.current else 1


def evaluate_marker_status(marker_path: Path, plan: MarkerPlan) -> MarkerStatusVerdict:
    """Evaluate one installed marker against the effective plan, read-only.

    Missing, unreadable, malformed, legacy-lane and unknown-version markers
    are unknown or non-current, never current: old markers stay readable on
    legacy lanes but never attest schema-2 currency, and unknown versions
    fail closed.
    """

    try:
        raw = marker_path.read_bytes()
    except FileNotFoundError:
        return MarkerStatusVerdict(False, (), "install marker is missing")
    except OSError as exc:
        return MarkerStatusVerdict(False, (), f"install marker is unreadable: {exc}")
    try:
        marker = read_install_marker(raw)
    except InstallMarkerError as exc:
        return MarkerStatusVerdict(False, (), f"install marker is not usable: {exc}")
    if not isinstance(marker, InstallMarkerV5):
        return MarkerStatusVerdict(
            False, (), "legacy marker cannot attest schema-2 installation currency"
        )
    differing = compare_marker_plan(marker, plan)
    if differing:
        return MarkerStatusVerdict(
            False,
            differing,
            "marker differs from the effective plan: " + ", ".join(differing),
        )
    return MarkerStatusVerdict(True, (), "")


def evaluate_schema2_status(
    marker_path: Path,
    plan: MarkerPlan,
    *,
    evidence_path: Path | None,
    evidence_fresh: bool,
    evidence_revoked: bool,
    expected_repository: str | None = None,
) -> MarkerStatusVerdict:
    """Evaluate schema-2 installation status: marker, then required evidence.

    The marker comparison runs first; when the plan selects registry evidence,
    the required record must validate or the installation is non-current.
    Repair and refresh reuse the same gates: they revalidate the exact locked
    source and required evidence instead of adopting marker summaries as
    trust. Read-only: this function performs no write.
    """

    verdict = evaluate_marker_status(marker_path, plan)
    if not verdict.current or plan.attestation is None:
        return verdict
    if isinstance(plan.package, source_package.NetworkGit):
        repository = plan.package.repository
        if expected_repository is not None and expected_repository != repository:
            raise InstallMarkerError(
                "install_marker_invalid",
                "explicit repository does not match the network-git package",
            )
    elif isinstance(plan.package, source_package.ConfiguredGit):
        if expected_repository is None:
            raise InstallMarkerError(
                "install_marker_invalid",
                "configured-git evidence needs the registry-established repository",
            )
        repository = expected_repository
    else:
        raise InstallMarkerError(
            "install_marker_invalid",
            "local-snapshot plans select no registry evidence",
        )
    if evidence_path is None:
        return MarkerStatusVerdict(
            False, (), "required registry evidence is absent"
        )
    expectation = AttestationExpectation(
        name=plan.name,
        repository=repository,
        commit=plan.package.commit,
        context_sha256=plan.context_sha256,
        key_id=plan.attestation.key_id,
    )
    try:
        validate_attestation_evidence(
            evidence_path,
            expectation,
            evidence_fresh=evidence_fresh,
            evidence_revoked=evidence_revoked,
        )
    except InstallMarkerError as exc:
        return MarkerStatusVerdict(False, (), f"required registry evidence is not usable: {exc}")
    return verdict


def _parse_common(body: dict[str, Any]) -> dict[str, Any]:
    return {
        "name": body["name"],
        "source": body["source"],
        "ref_kind": body["ref_kind"],
        "ref": body["ref"],
        "commit": body["commit"],
        "content_sha256": body["content_sha256"],
        "locale": body["locale"],
        "agents": _parse_array(body["agents"], "agents"),
        "commands": _parse_array(body["commands"], "commands"),
        "dependencies": _parse_array(body["dependencies"], "dependencies"),
        "skill_schema_version": body["skill_schema_version"],
        "runtime_roots": _parse_array(body["runtime_roots"], "runtime_roots"),
        "installed_at": body["installed_at"],
        "files": _parse_array(body["files"], "files"),
        "git": body.get("git"),
        "requirements": (
            _parse_array(body["requirements"], "requirements")
            if "requirements" in body
            else None
        ),
        "mcp_servers": (
            _parse_mcp_servers(body["mcp_servers"])
            if "mcp_servers" in body
            else None
        ),
        "attestation": (
            _parse_attestation(body["attestation"])
            if "attestation" in body
            else None
        ),
        "activation": (
            _parse_activation(body["activation"])
            if "activation" in body
            else None
        ),
        "requirers": (
            _parse_array(body["requirers"], "requirers")
            if "requirers" in body
            else None
        ),
        "substituted": body.get("substituted"),
    }


def _parse_v5_common(body: dict[str, Any]) -> dict[str, Any]:
    return {
        "name": body["name"],
        "package": _parse_marker_package(body["package"]),
        "lock_sha256": body["lock_sha256"],
        "content_sha256": body["content_sha256"],
        "locale": body["locale"],
        "agents": _parse_array(body["agents"], "agents"),
        "commands": _parse_array(body["commands"], "commands"),
        "dependencies": _parse_array(body["dependencies"], "dependencies"),
        "skill_schema_version": body["skill_schema_version"],
        "runtime_roots": _parse_array(body["runtime_roots"], "runtime_roots"),
        "installed_at": body["installed_at"],
        "files": _parse_array(body["files"], "files"),
        "requirements": (
            _parse_array(body["requirements"], "requirements")
            if "requirements" in body
            else None
        ),
        "mcp_servers": (
            _parse_mcp_servers(body["mcp_servers"])
            if "mcp_servers" in body
            else None
        ),
        "attestation": (
            _parse_attestation(body["attestation"])
            if "attestation" in body
            else None
        ),
        "activation": (
            _parse_activation(body["activation"])
            if "activation" in body
            else None
        ),
        "requirers": (
            _parse_array(body["requirers"], "requirers")
            if "requirers" in body
            else None
        ),
        "substituted": body.get("substituted"),
    }


def _parse_build_source(value: Any) -> BuildSourceIdentity:
    body = _require_object(value, "build_source")
    _validate_object_shape(
        body,
        _BUILD_SOURCE_MEMBERS,
        _BUILD_SOURCE_MEMBERS,
        "build_source",
    )
    identity = BuildSourceIdentity(
        algorithm=body["algorithm"],
        content_sha256=body["content_sha256"],
    )
    _validate_build_source(identity)
    return identity


def _parse_builds(value: Any) -> Mapping[str, InstallMarkerBuild]:
    body = _require_object(value, "builds")
    result: dict[str, InstallMarkerBuild] = {}
    for name, raw in body.items():
        _require_identifier(name, "build name")
        record = _require_object(raw, f"builds.{name}")
        _validate_object_shape(
            record,
            _BUILD_RECORD_MEMBERS,
            _BUILD_RECORD_MEMBERS,
            f"builds.{name}",
        )
        result[name] = InstallMarkerBuild(
            driver=record["driver"],
            cache_key=record["cache_key"],
            receipt_sha256=record["receipt_sha256"],
            artifact_sha256=record["artifact_sha256"],
            artifact_path=record["artifact_path"],
        )
    return result


def _parse_builds_v3(value: Any) -> Mapping[str, InstallMarkerBuildV3]:
    return _parse_receipted_builds(value, InstallMarkerBuildV3)


def _parse_builds_v5(value: Any) -> Mapping[str, InstallMarkerBuildV5]:
    return _parse_receipted_builds(value, InstallMarkerBuildV5)


_ReceiptedBuildT = TypeVar(
    "_ReceiptedBuildT", InstallMarkerBuildV3, InstallMarkerBuildV5
)


def _parse_receipted_builds(
    value: Any,
    record_cls: type[_ReceiptedBuildT],
) -> Mapping[str, _ReceiptedBuildT]:
    body = _require_object(value, "builds")
    result: dict[str, _ReceiptedBuildT] = {}
    for name, raw in body.items():
        _require_identifier(name, "build name")
        record = _require_object(raw, f"builds.{name}")
        driver = record.get("driver")
        if not isinstance(driver, str):
            raise InstallMarkerError("install_marker_invalid", f"builds.{name}.driver must be a string")
        allowed = _V3_EXTERNAL_BUILD_MEMBERS if driver == GO_REPOSITORY_V1_DRIVER else _V3_BUILD_COMMON_MEMBERS
        required = _V3_EXTERNAL_BUILD_REQUIRED if driver == GO_REPOSITORY_V1_DRIVER else _V3_BUILD_COMMON_MEMBERS
        _validate_object_shape(record, required, allowed, f"builds.{name}")
        result[name] = record_cls(
            driver=driver,
            receipt_schema_version=record["receipt_schema_version"],
            execution_policy=record["execution_policy"],
            cache_key=record["cache_key"],
            receipt_sha256=record["receipt_sha256"],
            artifact_sha256=record["artifact_sha256"],
            artifact_path=record["artifact_path"],
            repository=record.get("repository"),
            declared_identity=_parse_repository_identity(record["declared_identity"], "declared_identity") if "declared_identity" in record else None,
            declared_locked_commit=_parse_repository_commit(record["declared_locked_commit"], "declared_locked_commit") if "declared_locked_commit" in record else None,
            declared_tag=record.get("declared_tag"),
            effective_identity=_parse_repository_identity(record["effective_identity"], "effective_identity") if "effective_identity" in record else None,
            object_format=record.get("object_format"),
            commit=record.get("commit"),
            substituted=record.get("substituted"),
            substitution=_parse_repository_substitution(record["substitution"]) if "substitution" in record else None,
            build_source=_parse_build_source(record["build_source"]) if "build_source" in record else None,
            descriptor_target=record.get("descriptor_target"),
        )
    return result


def _parse_repository_identity(value: Any, subject: str) -> MarkerRepositoryIdentity:
    body = _require_object(value, subject)
    _validate_object_shape(body, _IDENTITY_MEMBERS, _IDENTITY_MEMBERS, subject)
    return MarkerRepositoryIdentity(kind=body["kind"], value=body["value"])


def _parse_repository_commit(value: Any, subject: str) -> MarkerRepositoryCommit:
    body = _require_object(value, subject)
    _validate_object_shape(body, _LOCKED_COMMIT_MEMBERS, _LOCKED_COMMIT_MEMBERS, subject)
    return MarkerRepositoryCommit(object_format=body["object_format"], hex=body["hex"])


def _parse_repository_substitution(value: Any) -> MarkerRepositorySubstitution:
    body = _require_object(value, "substitution")
    required = frozenset({"type"})
    _validate_object_shape(body, required, _SUBSTITUTION_MEMBERS, "substitution")
    ref: MarkerRepositoryRef | None = None
    if "ref" in body:
        ref_body = _require_object(body["ref"], "substitution.ref")
        _validate_object_shape(ref_body, _SUBSTITUTION_REF_MEMBERS, _SUBSTITUTION_REF_MEMBERS, "substitution.ref")
        ref = MarkerRepositoryRef(kind=ref_body["kind"], value=ref_body["value"])
    return MarkerRepositorySubstitution(type=body["type"], ref=ref)


def _parse_activation(value: Any) -> MarkerActivation:
    body = _require_object(value, "activation")
    _validate_object_shape(
        body,
        _ACTIVATION_MEMBERS,
        _ACTIVATION_MEMBERS,
        "activation",
    )
    return MarkerActivation(
        context=body["context"],
        commands=_parse_array(body["commands"], "activation.commands"),
    )


def _parse_attestation(value: Any) -> MarkerAttestation:
    body = _require_object(value, "attestation")
    _validate_object_shape(
        body,
        _ATTESTATION_REQUIRED_MEMBERS,
        _ATTESTATION_MEMBERS,
        "attestation",
    )
    return MarkerAttestation(
        registry=body["registry"],
        status=body["status"],
        key_id=body.get("key_id"),
    )


def _parse_mcp_servers(value: Any) -> Mapping[str, tuple[str, ...]]:
    body = _require_object(value, "mcp_servers")
    result: dict[str, tuple[str, ...]] = {}
    for name, agents in body.items():
        _require_identifier(name, "mcp server name")
        result[name] = _parse_array(agents, f"mcp_servers.{name}")
    return result


def _freeze_builds(value: Any) -> Mapping[str, InstallMarkerBuild]:
    if not isinstance(value, Mapping):
        raise InstallMarkerError(
            "install_marker_invalid",
            f"builds must be an object, got {type(value).__name__}",
        )
    result: dict[str, InstallMarkerBuild] = {}
    for name, build in value.items():
        _require_identifier(name, "build name")
        if not isinstance(build, InstallMarkerBuild):
            raise InstallMarkerError(
                "install_marker_invalid",
                f"builds.{name} must be an InstallMarkerBuild",
            )
        result[name] = build
    return MappingProxyType(dict(sorted(result.items())))


def _freeze_builds_v3(value: Any) -> Mapping[str, InstallMarkerBuildV3]:
    if not isinstance(value, Mapping):
        raise InstallMarkerError("install_marker_invalid", f"builds must be an object, got {type(value).__name__}")
    result: dict[str, InstallMarkerBuildV3] = {}
    for name, build in value.items():
        _require_identifier(name, "build name")
        if not isinstance(build, InstallMarkerBuildV3):
            raise InstallMarkerError("install_marker_invalid", f"builds.{name} must be an InstallMarkerBuildV3")
        result[name] = build
    return MappingProxyType(dict(sorted(result.items())))


def _freeze_builds_v5(value: Any) -> Mapping[str, InstallMarkerBuildV5]:
    if not isinstance(value, Mapping):
        raise InstallMarkerError("install_marker_invalid", f"builds must be an object, got {type(value).__name__}")
    result: dict[str, InstallMarkerBuildV5] = {}
    for name, build in value.items():
        _require_identifier(name, "build name")
        if not isinstance(build, InstallMarkerBuildV5):
            raise InstallMarkerError("install_marker_invalid", f"builds.{name} must be an InstallMarkerBuildV5")
        result[name] = build
    return MappingProxyType(dict(sorted(result.items())))


def _parse_marker_package(value: Any) -> source_package.PackageIdentity:
    """Parse the marker-v5 package union through the shared vocabulary.

    Package identity is never inferred from a transport endpoint: the shared
    parser admits only portable identities, and every endpoint-shaped value
    fails here with a reader refusal.
    """

    try:
        return source_package.parse_package_identity(value)
    except source_errors.SourceError as exc:
        raise InstallMarkerError(
            "install_marker_invalid", f"package is invalid: {exc.detail}"
        ) from exc


def _freeze_mcp_servers(value: Any) -> Mapping[str, tuple[str, ...]]:
    if not isinstance(value, Mapping):
        raise InstallMarkerError(
            "install_marker_invalid",
            f"mcp_servers must be an object, got {type(value).__name__}",
        )
    result: dict[str, tuple[str, ...]] = {}
    for name, agents in value.items():
        _require_identifier(name, "mcp server name")
        result[name] = _freeze_identifier_set(agents, f"mcp_servers.{name}")
    return MappingProxyType(dict(sorted(result.items())))


def _freeze_identifier_set(value: Any, subject: str) -> tuple[str, ...]:
    items = _freeze_string_set(value, subject)
    for item in items:
        _require_identifier(item, f"{subject} item")
    return items


def _freeze_path_set(value: Any, subject: str) -> tuple[str, ...]:
    items = _freeze_string_set(value, subject)
    for item in items:
        _require_portable_path(item, f"{subject} item")
    return items


def _freeze_string_set(value: Any, subject: str) -> tuple[str, ...]:
    if not isinstance(value, (list, tuple)):
        raise InstallMarkerError(
            "install_marker_invalid",
            f"{subject} must be an array, got {type(value).__name__}",
        )
    if any(not isinstance(item, str) for item in value):
        raise InstallMarkerError(
            "install_marker_invalid",
            f"{subject} must contain only strings",
        )
    if len(set(value)) != len(value):
        raise InstallMarkerError(
            "install_marker_invalid",
            f"{subject} must not contain duplicates",
        )
    return tuple(sorted(value))


def _parse_array(value: Any, subject: str) -> tuple[str, ...]:
    if not isinstance(value, list):
        raise InstallMarkerError(
            "install_marker_invalid",
            f"{subject} must be an array, got {type(value).__name__}",
        )
    return tuple(value)


def _validate_build_source(identity: Any) -> None:
    if not isinstance(identity, BuildSourceIdentity):
        raise InstallMarkerError(
            "install_marker_invalid",
            "build_source must be a BuildSourceIdentity",
        )
    if identity.algorithm != BUILD_SOURCE_ALGORITHM:
        raise InstallMarkerError(
            "install_marker_invalid",
            f"build_source algorithm must be {BUILD_SOURCE_ALGORITHM!r}",
        )
    _require_sha256(identity.content_sha256, "build_source content_sha256")


def _validate_build_artifact_path(name: str, artifact_path: str) -> None:
    unix_path = derived_artifact_path(name, goos="unix")
    windows_path = derived_artifact_path(name, goos="windows")
    if artifact_path not in {unix_path, windows_path}:
        raise InstallMarkerError(
            "install_marker_invalid",
            f"build {name!r} artifact_path must be {unix_path!r} or {windows_path!r}",
        )


def _validate_builds_against_commands(
    builds: Mapping[str, InstallMarkerBuildV5],
    commands: tuple[str, ...],
) -> None:
    """Bind recorded builds to commands exactly as the v5 record requires.

    The marker and the plan share this rule so a plan can never expect a
    shape the writer could not have written.
    """
    for build_name, build in builds.items():
        if build_name not in commands:
            raise InstallMarkerError(
                "install_marker_invalid",
                f"build {build_name!r} is not present in commands",
            )
        _validate_build_artifact_path(build_name, build.artifact_path)


def _validate_object_shape(
    body: dict[str, Any],
    required: frozenset[str],
    allowed: frozenset[str],
    subject: str,
) -> None:
    missing = sorted(required - set(body))
    if missing:
        raise InstallMarkerError(
            "install_marker_invalid",
            f"{subject} is missing required member(s): {', '.join(missing)}",
        )
    unknown = sorted(set(body) - allowed)
    if unknown:
        raise InstallMarkerError(
            "install_marker_invalid",
            f"{subject} has unknown member(s): {', '.join(unknown)}",
        )


def _require_object(value: Any, subject: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise InstallMarkerError(
            "install_marker_invalid",
            f"{subject} must be an object, got {type(value).__name__}",
        )
    if any(not isinstance(key, str) for key in value):
        raise InstallMarkerError(
            "install_marker_invalid",
            f"{subject} object keys must be strings",
        )
    return value


def _require_non_empty_string(value: Any, subject: str) -> None:
    if not isinstance(value, str) or not value or len(value) > 8192:
        raise InstallMarkerError(
            "install_marker_invalid",
            f"{subject} must be a non-empty string of at most 8192 characters",
        )


def _require_identifier(value: Any, subject: str) -> None:
    if not isinstance(value, str) or not is_valid_identifier(value):
        raise InstallMarkerError(
            "install_marker_invalid",
            f"{subject} is not a portable identifier: {value!r}",
        )


def _require_portable_path(value: Any, subject: str) -> None:
    if not isinstance(value, str) or not is_valid_portable_path(value):
        raise InstallMarkerError(
            "install_marker_invalid",
            f"{subject} is not a portable relative path: {value!r}",
        )


def _require_sha256(value: Any, subject: str) -> None:
    if (
        not isinstance(value, str)
        or not value.startswith(_SHA256_PREFIX)
        or len(value) != len(_SHA256_PREFIX) + 64
        or not set(value[len(_SHA256_PREFIX) :]) <= _SHA256_DIGITS
    ):
        raise InstallMarkerError(
            "install_marker_invalid",
            f"{subject} must be 'sha256:' and 64 lowercase hexadecimal digits",
        )


def _validate_optional_locale(value: Any, subject: str) -> None:
    """Refuse a locale that is neither null nor a portable selector.

    The marker and the plan share this rule so both sides of the comparison
    admit exactly the same values.
    """
    if value is not None and (
        not isinstance(value, str) or not is_valid_locale(value)
    ):
        raise InstallMarkerError(
            "install_marker_invalid",
            f"{subject} must be null or a portable locale selector, got {value!r}",
        )


def _require_manifest_schema_version(value: Any, subject: str) -> None:
    """Refuse a skill manifest version outside 1 through 8.

    Schema-2 installations write marker 5 regardless of manifest version, so
    the marker and the plan pin the same actual manifest version range.
    """
    if (
        not isinstance(value, int)
        or isinstance(value, bool)
        or not 1 <= value <= 8
    ):
        raise InstallMarkerError(
            "install_marker_invalid",
            f"{subject} must be an integer from 1 through 8, got {value!r}",
        )


__all__ = [
    "ATTESTATION_EVIDENCE_DEFECTS",
    "INSTALL_MARKER_V1_SCHEMA_VERSION",
    "INSTALL_MARKER_V2_SCHEMA_VERSION",
    "INSTALL_MARKER_V3_SCHEMA_VERSION",
    "INSTALL_MARKER_V4_SCHEMA_VERSION",
    "INSTALL_MARKER_V5_SCHEMA_VERSION",
    "MARKER_PLAN_COMPARISON_FIELDS",
    "SUPPORTED_INSTALL_MARKER_SCHEMA_VERSIONS",
    "AttestationExpectation",
    "InstallMarker",
    "InstallMarkerBuild",
    "InstallMarkerBuildV3",
    "InstallMarkerBuildV5",
    "InstallMarkerError",
    "InstallMarkerV1",
    "InstallMarkerV2",
    "InstallMarkerV3",
    "InstallMarkerV4",
    "InstallMarkerV5",
    "MarkerAttestation",
    "MarkerPlan",
    "MarkerRepositoryCommit",
    "MarkerRepositoryIdentity",
    "MarkerRepositoryRef",
    "MarkerRepositorySubstitution",
    "MarkerActivation",
    "MarkerBuild",
    "MarkerStatusVerdict",
    "RegistryEvidence",
    "build_install_marker_v5",
    "check_external_only_build_state",
    "check_local_registry_requirement",
    "check_top_level_build_source",
    "compare_marker_plan",
    "evaluate_marker_status",
    "evaluate_schema2_status",
    "marker_can_be_current",
    "marker_plan_is_current",
    "parse_install_marker",
    "parse_registry_evidence",
    "read_install_marker",
    "serialize_install_marker",
    "validate_attestation_evidence",
]
