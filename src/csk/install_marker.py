"""Typed install-marker schemas 1 and 2.

The marker records portable installation state only. Physical build-cache,
receipt, lock, quarantine, and manager-home paths are deliberately absent from
both models.
"""

from __future__ import annotations

import json
import re
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from types import MappingProxyType
from typing import Any, Final, Literal, TypeAlias

from . import protocol_json
from .builds.metadata import GO_V1_DRIVER, derived_artifact_path
from .builds.source import BuildSourceIdentity
from .hashing import BUILD_SOURCE_ALGORITHM
from .identifiers import is_valid_identifier, is_valid_locale, is_valid_portable_path


INSTALL_MARKER_V1_SCHEMA_VERSION: Final = 1
INSTALL_MARKER_V2_SCHEMA_VERSION: Final = 2
SUPPORTED_INSTALL_MARKER_SCHEMA_VERSIONS: Final[frozenset[int]] = frozenset(
    {INSTALL_MARKER_V1_SCHEMA_VERSION, INSTALL_MARKER_V2_SCHEMA_VERSION}
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
_BUILD_SOURCE_MEMBERS: Final[frozenset[str]] = frozenset({"algorithm", "content_sha256"})
_BUILD_RECORD_MEMBERS: Final[frozenset[str]] = frozenset(
    {"driver", "cache_key", "receipt_sha256", "artifact_sha256", "artifact_path"}
)
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


InstallMarker: TypeAlias = InstallMarkerV1 | InstallMarkerV2


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
    """Parse one decoded marker schema 1 or 2 and canonicalize set ordering."""
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
    return 0 <= skill_schema_version <= 6


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


__all__ = [
    "INSTALL_MARKER_V1_SCHEMA_VERSION",
    "INSTALL_MARKER_V2_SCHEMA_VERSION",
    "SUPPORTED_INSTALL_MARKER_SCHEMA_VERSIONS",
    "InstallMarker",
    "InstallMarkerBuild",
    "InstallMarkerError",
    "InstallMarkerV1",
    "InstallMarkerV2",
    "MarkerActivation",
    "MarkerAttestation",
    "marker_can_be_current",
    "parse_install_marker",
    "read_install_marker",
]
