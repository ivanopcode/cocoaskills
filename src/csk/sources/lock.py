"""Skillfile.lock.json schema 1 model, reader, and portable serializers.

This module has no filesystem or network behaviour.  Callers provide the
already parsed Skillfile and the already resolved closure; the lock records
those values without trying to rediscover them.  The production reader keeps
the validation order explicit: JSON structure, schema shape, cross-field
semantics, digests, and finally current-membership comparison.
"""

from __future__ import annotations

import hashlib
import os
from collections.abc import Iterable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Final, Literal, TypeAlias

from .. import identifiers, protocol_json
from . import package_identity
from .package_identity import exact_str
from .errors import (
    CODE_LOCK_STALE,
    CODE_MEMBER_INVALID,
    CODE_MEMBER_MISSING,
    CODE_NAME_CONFLICT,
    CODE_SELECTION_INVALID,
    SourceError,
)

LOCK_SCHEMA_VERSION: Final = 1
LockSchemaVersion: TypeAlias = Literal[1]
_LOCK_MEMBERS: Final = frozenset(
    {"schema_version", "manifest_sha256", "members", "lock_sha256"}
)
_MEMBER_MEMBERS: Final = frozenset(
    {"name", "selection", "directory", "package", "content_sha256"}
)
_SHA256_PREFIX: Final = "sha256:"
_MAX_SAFE_INTEGER: Final = 9_007_199_254_740_991


def _invalid(detail: str) -> SourceError:
    return SourceError(CODE_SELECTION_INVALID, detail)


def _member_invalid(detail: str) -> SourceError:
    return SourceError(CODE_MEMBER_INVALID, detail)


@dataclass(frozen=True, slots=True)
class LockMember:
    """One resolved closure member in a Skillfile lock."""

    name: str
    selection: int | None
    directory: str
    package: package_identity.PackageIdentity
    content_sha256: str

    def __post_init__(self) -> None:
        # Canonicalize once, then decide and store only the canonical value.
        # Every gate below (identifier, directory, digest) and every later
        # consumer (ordering, hashing, serialization) must see exact str.
        object.__setattr__(self, "name", exact_str(self.name))
        object.__setattr__(self, "directory", exact_str(self.directory))
        object.__setattr__(self, "content_sha256", exact_str(self.content_sha256))
        _validate_member_shape(self)

    def to_json(self) -> dict[str, object]:
        """Return the exact wire fields for this member."""
        return {
            "name": self.name,
            "selection": self.selection,
            "directory": self.directory,
            "package": package_identity.package_identity_to_json(self.package),
            "content_sha256": self.content_sha256,
        }


Member: TypeAlias = LockMember
LockedMember: TypeAlias = LockMember

# Re-export the shared vocabulary at the lock boundary for callers that deal
# with a lock and its package identity together.  These are aliases, not
# duplicate model classes, so downstream marker/receipt/audit modules can
# import the same frozen dataclasses from ``package_identity``.
LockedCommit: TypeAlias = package_identity.LockedCommit
LocalSnapshot: TypeAlias = package_identity.LocalSnapshot
NetworkGit: TypeAlias = package_identity.NetworkGit
ConfiguredGit: TypeAlias = package_identity.ConfiguredGit
PackageIdentity: TypeAlias = package_identity.PackageIdentity


@dataclass(frozen=True, slots=True)
class SkillfileLock:
    """The typed schema 1 lock document.

    ``lock_sha256`` is optional only while a new model is being assembled.  A
    serialized lock always contains the computed digest; a parsed document
    retains the stored value so the reader can report a forged preimage.
    """

    manifest_sha256: str
    members: tuple[LockMember, ...]
    lock_sha256: str | None = None
    schema_version: LockSchemaVersion = LOCK_SCHEMA_VERSION
    _read_tolerant: bool = field(default=False, init=False, repr=False, compare=False)

    def __post_init__(self) -> None:
        object.__setattr__(self, "manifest_sha256", exact_str(self.manifest_sha256))
        if self.lock_sha256 is not None:
            object.__setattr__(self, "lock_sha256", exact_str(self.lock_sha256))
        try:
            members = tuple(self.members)
        except (TypeError, ValueError) as exc:
            raise _member_invalid("lock members must be an iterable of LockMember values") from exc
        for member in members:
            if not isinstance(member, LockMember):
                raise _member_invalid("lock members must be LockMember values")
        object.__setattr__(self, "members", members)

    def to_json(self) -> dict[str, object]:
        """Return the lock document as JSON-compatible typed data."""
        lock_digest = self.lock_sha256
        if lock_digest is None:
            lock_digest = compute_lock_sha256(self)
        return {
            "schema_version": self.schema_version,
            "manifest_sha256": self.manifest_sha256,
            "members": [member.to_json() for member in self.members],
            "lock_sha256": lock_digest,
        }

    def computed_lock_sha256(self) -> str:
        """Recompute the digest over this lock with only its digest omitted."""
        return compute_lock_sha256(self)


LockDocument: TypeAlias = SkillfileLock


@dataclass(frozen=True, slots=True)
class MachinePrivateBinding:
    """Machine-only refresh binding, intentionally absent from lock bytes.

    ``source_location`` is supplied by the caller after physical resolution
    and must be absolute; true canonicity (symlink resolution) needs I/O this
    leaf forbids, so the canonical spelling is the caller's contract.  This
    value may be absolute because it never participates in package or lock
    identity.  ``root_inputs`` remain source-relative portable paths.
    """

    source_location: str | os.PathLike[str]
    root_inputs: tuple[str, ...] = ()
    schema_version: Literal[1] = 1

    def __post_init__(self) -> None:
        if self.schema_version != 1:
            raise _invalid(f"unsupported machine binding schema_version {self.schema_version!r}")
        try:
            location = os.fspath(self.source_location)
        except TypeError as exc:
            raise _invalid("machine binding source_location must be a filesystem path") from exc
        canonical_location = exact_str(location)
        if not isinstance(canonical_location, str) or not Path(canonical_location).is_absolute():
            raise _invalid(
                "machine binding source_location must be an absolute physical path"
                " (the caller supplies the canonical spelling)"
            )
        object.__setattr__(self, "source_location", canonical_location)

        try:
            inputs = tuple(self.root_inputs)
        except (TypeError, ValueError) as exc:
            raise _invalid("machine binding root_inputs must be an iterable of paths") from exc
        canonical_inputs = tuple(exact_str(value) for value in inputs)
        for index, value in enumerate(canonical_inputs):
            if not _is_binding_path(value):
                raise _invalid(
                    f"machine binding root_inputs[{index}] must be a portable relative path"
                )
        if len(set(canonical_inputs)) != len(canonical_inputs):
            raise SourceError(
                CODE_NAME_CONFLICT,
                "machine binding root_inputs must not contain duplicate paths",
            )
        object.__setattr__(self, "root_inputs", tuple(sorted(canonical_inputs, key=_utf8_key)))

    @property
    def physical_source_location(self) -> str:
        """Compatibility spelling for the absolute physical source path."""
        return str(self.source_location)

    @property
    def canonical_source_location(self) -> str:
        """Return the caller-supplied absolute source path (canonical by contract)."""
        return str(self.source_location)

    def to_json(self) -> dict[str, object]:
        """Return the private binding representation."""
        return {
            "schema_version": self.schema_version,
            "source_location": str(self.source_location),
            "root_inputs": list(self.root_inputs),
        }


SourceBinding: TypeAlias = MachinePrivateBinding
MachineSourceBinding: TypeAlias = MachinePrivateBinding


def create_lock(
    manifest: dict[str, Any],
    members: Iterable[LockMember],
) -> SkillfileLock:
    """Create a strict, digest-complete lock from resolved caller values.

    The input closure is sorted by UTF-8 name bytes.  Configured legacy Git
    members are emitted at ``directory: "."`` even when a tolerant reader had
    observed a different legacy member directory.  Network Git members must
    already agree with their package identity directory.
    """
    if not isinstance(manifest, dict):
        raise _invalid("manifest must be the complete parsed Skillfile object")
    try:
        candidates = tuple(members)
    except (TypeError, ValueError) as exc:
        raise _member_invalid("lock members must be an iterable of LockMember values") from exc
    prepared: list[LockMember] = []
    for member in candidates:
        if not isinstance(member, LockMember):
            raise _member_invalid("lock members must be LockMember values")
        if isinstance(member.package, package_identity.ConfiguredGit):
            member = LockMember(
                name=member.name,
                selection=member.selection,
                directory=".",
                package=member.package,
                content_sha256=member.content_sha256,
            )
        prepared.append(member)
    ordered = tuple(sort_lock_members_by_utf8(prepared))
    lock = SkillfileLock(
        manifest_sha256=manifest_sha256(manifest),
        members=ordered,
    )
    _validate_lock(lock, enforce_directory_agreement=True)
    with_digest = SkillfileLock(
        manifest_sha256=lock.manifest_sha256,
        members=lock.members,
        lock_sha256=compute_lock_sha256(lock),
        schema_version=lock.schema_version,
    )
    _validate_lock(with_digest, enforce_directory_agreement=True)
    return with_digest


build_lock = create_lock
make_lock = create_lock


def serialize_lock(
    lock: SkillfileLock,
    *,
    verify_digest: bool = True,
) -> bytes:
    """Serialize a lock as canonical CCJ-1 bytes.

    A model freshly created by :func:`create_lock` is strict-on-write.  A model
    returned by :func:`read_lock` remembers that it may carry the configured-Git
    directory spelling of another conforming manager, so that read → write is
    byte-identical for that interop document.  Network-Git directory agreement
    remains enforced in both cases.
    """
    if not isinstance(lock, SkillfileLock):
        raise _invalid("lock must be a SkillfileLock")
    _validate_lock(
        lock,
        enforce_directory_agreement=not lock._read_tolerant,
    )
    if verify_digest:
        _require_lock_digest(lock)
    try:
        return protocol_json.canonical_bytes(lock.to_json())
    except protocol_json.ProtocolJSONError as exc:
        raise _invalid(f"lock cannot be represented as CCJ-1: {exc}") from exc
    except (OSError, RuntimeError, TypeError, UnicodeError, ValueError) as exc:
        raise _invalid(f"lock serializer failed: {exc}") from exc


serialize_skillfile_lock = serialize_lock


def read_lock(
    raw: bytes | str,
    *,
    current_manifest: dict[str, Any] | None = None,
    current_manifest_sha256: str | None = None,
    resolved_members: Iterable[LockMember | str] | None = None,
    expected_members: Iterable[LockMember | str] | None = None,
    verify_digests: bool = True,
    validate_semantics: bool = True,
) -> SkillfileLock:
    """Read one lock and fail closed in the specified validation order.

    ``verify_digests=False`` and ``validate_semantics=False`` are exposed for
    the specification's schema-case harness.  They perform the production
    structural/schema reader without treating synthetic schema examples as
    real digest vectors.  Ordinary callers keep both defaults enabled.
    """
    if not isinstance(raw, (bytes, str)):
        raise _invalid("lock input must be UTF-8 bytes or text")
    try:
        value = protocol_json.loads(raw)
    except protocol_json.ProtocolJSONError as exc:
        raise _invalid(f"lock is malformed JSON: {exc}") from exc
    except (OSError, RuntimeError, TypeError, UnicodeError, ValueError) as exc:
        raise _invalid(f"lock JSON reader failed: {exc}") from exc

    # The reader deliberately accepts the configured-Git legacy directory
    # spelling used by another conforming manager.  Network-Git agreement and
    # all other cross-field semantics remain enforced by _validate_lock.
    lock = _parse_lock_shape(value)
    if validate_semantics:
        _validate_lock(lock, enforce_directory_agreement=False)
    elif verify_digests:
        raise _invalid("digest verification requires schema and semantic validation")

    if verify_digests:
        _require_lock_digest(lock)
        _validate_current_manifest(
            lock,
            current_manifest=current_manifest,
            current_manifest_sha256=current_manifest_sha256,
        )
        selected_expected = _select_expected_members(resolved_members, expected_members)
        if selected_expected is not None:
            validate_membership(lock, selected_expected)
    elif current_manifest is not None or current_manifest_sha256 is not None:
        # A caller cannot accidentally believe stale checking ran when digest
        # verification was explicitly disabled for schema-only inspection.
        raise _invalid("current manifest requires digest verification")
    if resolved_members is not None and expected_members is not None:
        raise _invalid("resolved_members and expected_members are mutually exclusive")
    return lock


read_skillfile_lock = read_lock


def read_lock_schema(raw: bytes | str) -> SkillfileLock:
    """Run the production reader's structural/schema phase only."""
    return read_lock(raw, verify_digests=False, validate_semantics=False)


def parse_lock(
    value: object,
    *,
    verify_digests: bool = True,
    validate_semantics: bool = True,
    strict: bool = False,
) -> SkillfileLock:
    """Parse already decoded lock data with hand-written schema validation.

    Tolerant by default: the whole read surface accepts the configured-Git
    legacy directory spelling of another conforming manager. Pass
    ``strict=True`` for write-path validation, where a configured-Git member
    must sit at ``directory: "."``.
    """
    lock = _parse_lock_shape(value)
    if validate_semantics:
        _validate_lock(lock, enforce_directory_agreement=strict)
    if verify_digests:
        _require_lock_digest(lock)
    return lock


parse_skillfile_lock = parse_lock


def validate_lock(
    lock: SkillfileLock,
    *,
    current_manifest: dict[str, Any] | None = None,
    current_manifest_sha256: str | None = None,
    resolved_members: Iterable[LockMember | str] | None = None,
) -> None:
    """Validate a parsed lock, including optional stale/membership checks."""
    _validate_lock(lock, enforce_directory_agreement=not lock._read_tolerant)
    _require_lock_digest(lock)
    _validate_current_manifest(
        lock,
        current_manifest=current_manifest,
        current_manifest_sha256=current_manifest_sha256,
    )
    if resolved_members is not None:
        validate_membership(lock, resolved_members)


def validate_membership(
    lock: SkillfileLock,
    resolved_members: Iterable[LockMember | str],
) -> None:
    """Require the lock membership to equal the current resolved membership.

    Each ``LockMember`` item is compared against its same-named lock record
    and each ``str`` item by name, so homogeneous and mixed inputs share one
    record-equality rule.
    """
    if not isinstance(lock, SkillfileLock):
        raise _invalid("lock must be a SkillfileLock")
    try:
        current = tuple(resolved_members)
    except (TypeError, ValueError) as exc:
        raise _member_invalid("resolved membership must be an iterable") from exc
    canonical: list[LockMember | str] = []
    for item in current:
        if isinstance(item, LockMember):
            canonical.append(item)
        elif isinstance(item, str):
            canonical.append(str.__str__(item))
        else:
            raise _member_invalid("resolved membership must contain names or LockMember values")
    names = [item.name if isinstance(item, LockMember) else item for item in canonical]
    if len(set(names)) != len(names):
        raise SourceError(
            CODE_NAME_CONFLICT,
            "resolved membership contains duplicate names",
        )

    actual = tuple(sorted(lock.members, key=lambda item: _utf8_key(item.name)))
    expected_names = set(names)
    actual_names = {member.name for member in actual}
    if expected_names != actual_names:
        missing = sorted(expected_names - actual_names, key=_utf8_key)
        unknown = sorted(actual_names - expected_names, key=_utf8_key)
        parts: list[str] = []
        if missing:
            parts.append(f"missing={missing!r}")
        if unknown:
            parts.append(f"unknown={unknown!r}")
        raise SourceError(
            CODE_MEMBER_MISSING,
            "lock membership does not equal the current resolved membership ("
            + ", ".join(parts)
            + ")",
        )

    by_name = {member.name: member for member in actual}
    for item in canonical:
        if isinstance(item, LockMember) and by_name[item.name] != item:
            raise SourceError(
                CODE_MEMBER_MISSING,
                "lock member records do not equal the current resolved membership",
            )


def manifest_sha256(manifest: dict[str, Any]) -> str:
    """Use the existing Skillfile CCJ-1 manifest digest implementation."""
    if not isinstance(manifest, dict):
        raise _invalid("manifest must be the complete parsed Skillfile object")
    try:
        digest = _skillfile_v2_manifest_sha256(_canonical_json_value(manifest))
    except protocol_json.ProtocolJSONError as exc:
        raise _invalid(f"manifest cannot be represented as CCJ-1: {exc}") from exc
    except (OSError, RuntimeError, TypeError, UnicodeError, ValueError) as exc:
        raise _invalid(f"manifest digest computation failed: {exc}") from exc
    return _require_sha256(digest, "manifest")


def compute_lock_sha256(lock: SkillfileLock) -> str:
    """Compute CCJ-1 SHA-256 with only the top-level lock digest omitted."""
    if not isinstance(lock, SkillfileLock):
        raise _invalid("lock must be a SkillfileLock")
    try:
        payload = {
            "schema_version": lock.schema_version,
            "manifest_sha256": lock.manifest_sha256,
            "members": [member.to_json() for member in lock.members],
        }
        return _SHA256_PREFIX + hashlib.sha256(protocol_json.canonical_bytes(payload)).hexdigest()
    except protocol_json.ProtocolJSONError as exc:
        raise _invalid(f"lock digest preimage is not valid CCJ-1: {exc}") from exc
    except (OSError, RuntimeError, TypeError, UnicodeError, ValueError) as exc:
        raise _invalid(f"lock digest computation failed: {exc}") from exc


lock_sha256 = compute_lock_sha256


def sort_member_names_by_utf8(names: Iterable[str]) -> list[str]:
    """Sort skill names by their exact UTF-8 encoded bytes.

    The lock schema restricts names to portable ASCII identifiers, but keeping
    this ordering seam independent makes its byte-order contract testable for
    the full Unicode family before the identifier gate is applied.
    """
    try:
        values = tuple(names)
    except (TypeError, ValueError) as exc:
        raise _member_invalid("skill names must be an iterable of strings") from exc
    if any(not isinstance(name, str) for name in values):
        raise _member_invalid("skill names must be strings")
    return sorted(values, key=_utf8_key)


def sort_lock_members_by_utf8(members: Iterable[LockMember]) -> list[LockMember]:
    """Sort typed lock members by their exact UTF-8 encoded skill names."""
    try:
        values = tuple(members)
    except (TypeError, ValueError) as exc:
        raise _member_invalid("lock members must be an iterable of LockMember values") from exc
    if any(not isinstance(member, LockMember) for member in values):
        raise _member_invalid("lock members must be LockMember values")
    return sorted(values, key=lambda member: _utf8_key(member.name))


def parse_machine_private_binding(value: object) -> MachinePrivateBinding:
    """Parse the separate machine-owned refresh binding record."""
    if not isinstance(value, dict):
        raise _invalid("machine binding must be an object")
    if set(value) != {"schema_version", "source_location", "root_inputs"}:
        raise _invalid("machine binding has unsupported or missing fields")
    schema_version = value["schema_version"]
    if not isinstance(schema_version, int) or isinstance(schema_version, bool) or schema_version != 1:
        raise _invalid(f"unsupported machine binding schema_version {schema_version!r}")
    source_location = value["source_location"]
    root_inputs = value["root_inputs"]
    if not isinstance(source_location, str) or not isinstance(root_inputs, list):
        raise _invalid("machine binding has an invalid source_location or root_inputs")
    try:
        return MachinePrivateBinding(
            source_location=source_location,
            root_inputs=tuple(root_inputs),
            schema_version=1,
        )
    except SourceError:
        raise
    except (TypeError, ValueError) as exc:
        raise _invalid(f"machine binding is invalid: {exc}") from exc


def read_machine_private_binding(raw: bytes | str) -> MachinePrivateBinding:
    """Read a private binding from UTF-8 protocol JSON bytes."""
    if not isinstance(raw, (bytes, str)):
        raise _invalid("machine binding input must be UTF-8 bytes or text")
    try:
        value = protocol_json.loads(raw)
    except protocol_json.ProtocolJSONError as exc:
        raise _invalid(f"machine binding is malformed JSON: {exc}") from exc
    except (OSError, RuntimeError, TypeError, UnicodeError, ValueError) as exc:
        raise _invalid(f"machine binding JSON reader failed: {exc}") from exc
    return parse_machine_private_binding(value)


def serialize_machine_private_binding(binding: MachinePrivateBinding) -> bytes:
    """Serialize a private binding as canonical CCJ-1 bytes."""
    if not isinstance(binding, MachinePrivateBinding):
        raise _invalid("machine binding must be a MachinePrivateBinding")
    if binding.schema_version != 1:
        raise _invalid(f"unsupported machine binding schema_version {binding.schema_version!r}")
    # Re-run the model's validation before handing bytes to the canonicalizer.
    MachinePrivateBinding(
        source_location=binding.source_location,
        root_inputs=binding.root_inputs,
        schema_version=binding.schema_version,
    )
    try:
        return protocol_json.canonical_bytes(binding.to_json())
    except protocol_json.ProtocolJSONError as exc:
        raise _invalid(f"machine binding cannot be represented as CCJ-1: {exc}") from exc
    except (OSError, RuntimeError, TypeError, UnicodeError, ValueError) as exc:
        raise _invalid(f"machine binding serializer failed: {exc}") from exc


serialize_binding = serialize_machine_private_binding
read_binding = read_machine_private_binding
parse_binding = parse_machine_private_binding


def _parse_lock_shape(value: object) -> SkillfileLock:
    if not isinstance(value, dict):
        raise _invalid("lock must be a JSON object")
    if set(value) != _LOCK_MEMBERS:
        raise _invalid("lock has unsupported or missing top-level fields")
    schema_version = value["schema_version"]
    if (
        not isinstance(schema_version, int)
        or isinstance(schema_version, bool)
        or schema_version != LOCK_SCHEMA_VERSION
    ):
        raise _invalid(f"unsupported lock schema_version {schema_version!r}")
    manifest_digest = value["manifest_sha256"]
    lock_digest = value["lock_sha256"]
    if not package_identity.is_sha256_digest(manifest_digest):
        raise _invalid("lock manifest_sha256 must be a lower-case SHA-256 digest")
    if not package_identity.is_sha256_digest(lock_digest):
        raise _invalid("lock lock_sha256 must be a lower-case SHA-256 digest")
    raw_members = value["members"]
    if not isinstance(raw_members, list):
        raise _invalid("lock members must be an array")
    members = tuple(_parse_member(raw, index) for index, raw in enumerate(raw_members))
    lock = SkillfileLock(
        manifest_sha256=manifest_digest,
        members=members,
        lock_sha256=lock_digest,
        schema_version=LOCK_SCHEMA_VERSION,
    )
    object.__setattr__(lock, "_read_tolerant", True)
    return lock


def _parse_member(value: object, index: int) -> LockMember:
    subject = f"lock members[{index}]"
    if not isinstance(value, dict):
        raise _invalid(f"{subject} must be an object")
    if set(value) != _MEMBER_MEMBERS:
        raise _invalid(f"{subject} has unsupported or missing fields")
    name = value["name"]
    selection = value["selection"]
    directory = value["directory"]
    content_digest = value["content_sha256"]
    if not isinstance(name, str) or not _is_identifier(name):
        raise _invalid(f"{subject}.name must be a portable identifier")
    if selection is not None and (
        not isinstance(selection, int)
        or isinstance(selection, bool)
        or not 0 <= selection <= _MAX_SAFE_INTEGER
    ):
        raise _invalid(f"{subject}.selection must be a non-negative safe integer or null")
    if not _is_lock_directory(directory):
        raise _invalid(f"{subject}.directory must be a portable relative directory")
    if not package_identity.is_sha256_digest(content_digest):
        raise _invalid(f"{subject}.content_sha256 must be a lower-case SHA-256 digest")
    parsed_package = package_identity.parse_package_identity(value["package"])
    try:
        return LockMember(
            name=name,
            selection=selection,
            directory=directory,
            package=parsed_package,
            content_sha256=content_digest,
        )
    except SourceError as exc:
        raise _invalid(f"{subject} is invalid: {exc.detail}") from exc


def _validate_lock(lock: SkillfileLock, *, enforce_directory_agreement: bool) -> None:
    if not isinstance(lock, SkillfileLock):
        raise _invalid("lock must be a SkillfileLock")
    if (
        not isinstance(lock.schema_version, int)
        or isinstance(lock.schema_version, bool)
        or lock.schema_version != LOCK_SCHEMA_VERSION
    ):
        raise _invalid(f"unsupported lock schema_version {lock.schema_version!r}")
    _require_sha256(lock.manifest_sha256, "manifest_sha256")
    if lock.lock_sha256 is not None:
        _require_sha256(lock.lock_sha256, "lock_sha256")
    if not isinstance(lock.members, tuple):
        raise _member_invalid("lock members must be an immutable tuple")
    for member in lock.members:
        _validate_member_shape(member)

    names = [member.name for member in lock.members]
    seen: set[str] = set()
    for name in names:
        if name in seen:
            raise SourceError(CODE_NAME_CONFLICT, f"duplicate lock member name: {name}")
        seen.add(name)
    ordered_names = sorted(names, key=_utf8_key)
    if names != ordered_names:
        raise _member_invalid("lock members must be sorted by UTF-8 skill name bytes")

    selections = sorted(
        member.selection for member in lock.members if member.selection is not None
    )
    if selections != list(range(len(selections))):
        raise _member_invalid("root member selections must be unique and zero-based")

    for member in lock.members:
        if isinstance(member.package, package_identity.NetworkGit):
            if member.package.directory != member.directory:
                raise _member_invalid(
                    f"member {member.name!r} directory disagrees with network-git package directory"
                )
        elif (
            enforce_directory_agreement
            and isinstance(member.package, package_identity.ConfiguredGit)
            and member.directory != "."
        ):
            raise _member_invalid(
                f"configured-git member {member.name!r} must be written at directory '.'"
            )


def _validate_member_shape(member: LockMember) -> None:
    if not isinstance(member, LockMember):
        raise _member_invalid("lock members must be LockMember values")
    if not _is_identifier(member.name):
        raise _member_invalid(f"member name is not a portable identifier: {member.name!r}")
    if member.selection is not None and (
        not isinstance(member.selection, int)
        or isinstance(member.selection, bool)
        or not 0 <= member.selection <= _MAX_SAFE_INTEGER
    ):
        raise _member_invalid(
            f"member {member.name!r} selection must be a non-negative safe integer or null"
        )
    if not _is_lock_directory(member.directory):
        raise _member_invalid(
            f"member {member.name!r} directory is not a portable relative directory"
        )
    if not isinstance(
        member.package,
        (
            package_identity.LocalSnapshot,
            package_identity.NetworkGit,
            package_identity.ConfiguredGit,
        ),
    ):
        raise _member_invalid(f"member {member.name!r} has an unknown package identity type")
    if not package_identity.is_sha256_digest(member.content_sha256):
        raise _member_invalid(f"member {member.name!r} has an invalid content_sha256")


def _require_lock_digest(lock: SkillfileLock) -> None:
    if lock.lock_sha256 is None:
        raise _invalid("lock is missing lock_sha256")
    expected = compute_lock_sha256(lock)
    if lock.lock_sha256 != expected:
        raise _invalid(
            f"lock_sha256 does not match the CCJ-1 preimage: expected {expected}, got {lock.lock_sha256}"
        )


def _validate_current_manifest(
    lock: SkillfileLock,
    *,
    current_manifest: dict[str, Any] | None,
    current_manifest_sha256: str | None,
) -> None:
    if current_manifest is not None and current_manifest_sha256 is not None:
        raise _invalid("current_manifest and current_manifest_sha256 are mutually exclusive")
    if current_manifest is None and current_manifest_sha256 is None:
        return
    if current_manifest is not None:
        if not isinstance(current_manifest, dict):
            raise _invalid("current_manifest must be the complete parsed Skillfile object")
        current_digest = manifest_sha256(current_manifest)
    else:
        assert current_manifest_sha256 is not None
        current_digest = current_manifest_sha256
        if isinstance(current_digest, str):
            current_digest = str.__str__(current_digest)
        if not package_identity.is_sha256_digest(current_digest):
            raise _invalid("current_manifest_sha256 must be a lower-case SHA-256 digest")
    if lock.manifest_sha256 != current_digest:
        raise SourceError(
            CODE_LOCK_STALE,
            f"lock manifest_sha256 {lock.manifest_sha256} does not match current Skillfile {current_digest}",
        )


def _select_expected_members(
    resolved_members: Iterable[LockMember | str] | None,
    expected_members: Iterable[LockMember | str] | None,
) -> Iterable[LockMember | str] | None:
    if resolved_members is not None and expected_members is not None:
        raise _invalid("resolved_members and expected_members are mutually exclusive")
    return resolved_members if resolved_members is not None else expected_members


def _require_sha256(value: object, subject: str) -> str:
    if not package_identity.is_sha256_digest(value):
        raise _invalid(f"{subject} must be a lower-case SHA-256 digest")
    assert isinstance(value, str)
    return value


def _is_identifier(value: object) -> bool:
    return isinstance(value, str) and identifiers.is_valid_identifier(value)


def _is_lock_directory(value: object) -> bool:
    # The lock member directory grammar is exactly the source-types identity
    # directory grammar; one predicate serves both so they cannot drift.
    return package_identity.is_valid_identity_directory(value)


def _is_binding_path(value: object) -> bool:
    if not isinstance(value, str):
        return False
    canonical = str.__str__(value)
    return canonical == "." or identifiers.is_valid_portable_path(canonical)


def _utf8_key(value: object) -> bytes:
    if not isinstance(value, str):
        raise _member_invalid(f"skill name must be a string, got {type(value).__name__}")
    try:
        return str.__str__(value).encode("utf-8", errors="strict")
    except UnicodeEncodeError as exc:
        raise _member_invalid(f"skill name is not valid UTF-8: {value!r}") from exc


def _canonical_json_value(value: Any) -> Any:
    """Rebuild caller-supplied JSON data with exact-``str`` scalars.

    The CCJ-1 sort honors overridable ``__lt__``/``__gt__``/``__eq__`` through
    reflected comparison, so a hostile ``str`` key can reorder (or duplicate)
    the encoded keys and change the digest of an unchanged honest manifest.
    Rebuilding preserves every value bit-for-bit and passes non-JSON types
    through untouched, so ``canonical_bytes`` still refuses exactly what it
    refused before; for exact-``str`` input the rebuilt bytes are identical.
    """
    if isinstance(value, str):
        return str.__str__(value)
    if isinstance(value, dict):
        return {
            _canonical_json_value(key): _canonical_json_value(item)
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [_canonical_json_value(item) for item in value]
    return value


def _skillfile_v2_manifest_sha256(data: dict[str, Any]) -> str:
    # Imported lazily to keep package_identity and lock independently usable
    # during package import and to make the production call site explicit.
    from .skillfile_v2 import manifest_sha256 as compute_manifest_sha256

    return compute_manifest_sha256(data)
