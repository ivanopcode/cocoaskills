"""Portable package identities for draft ``skillfile-sources-v1`` locks.

The values in this module are the shared source identity vocabulary.  They are
deliberately separate from acquisition endpoints and from machine-local source
bindings: an identity contains only the bytes or the immutable repository
coordinates that another manager can reproduce.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from typing import Final, Literal, TypeAlias

from .. import identifiers, protocol_json
from .errors import CODE_SELECTION_INVALID, SourceError

SHA1: Final = "sha1"
SHA256: Final = "sha256"
_SHA256_RE: Final = re.compile(r"^sha256:[0-9a-f]{64}$")
_REPOSITORY_HOST_RE: Final = re.compile(r"^[a-z0-9][a-z0-9.-]*$")
_ECMA_WHITESPACE: Final = frozenset(
    {
        "\t",
        "\n",
        "\x0b",
        "\x0c",
        "\r",
        " ",
        "\u00a0",
        "\u1680",
        "\u2000",
        "\u2001",
        "\u2002",
        "\u2003",
        "\u2004",
        "\u2005",
        "\u2006",
        "\u2007",
        "\u2008",
        "\u2009",
        "\u200a",
        "\u2028",
        "\u2029",
        "\u202f",
        "\u205f",
        "\u3000",
        "\ufeff",
    }
)

ObjectFormat: TypeAlias = Literal["sha1", "sha256"]


def exact_str(value: object) -> object:
    """Return the exact-``str`` value of ``value`` when it is a string.

    ``str(x)`` is not normalization: on a ``str`` subclass it invokes an
    overridden ``__str__`` and may return a hostile subclass instance carrying
    a different value. ``str.__str__`` bypasses the override and yields the
    exact stored value, so benign subclasses normalize by value and hostile
    ones cannot smuggle anything. Non-string values pass through untouched so
    the caller's gate refuses them with its own typed error.
    """
    if isinstance(value, str):
        return str.__str__(value)
    return value


def _invalid(detail: str) -> SourceError:
    return SourceError(CODE_SELECTION_INVALID, detail)


def is_sha256_digest(value: object) -> bool:
    """Return whether ``value`` is a lower-case, prefixed SHA-256 digest."""
    return isinstance(value, str) and _SHA256_RE.fullmatch(value) is not None


@dataclass(frozen=True, slots=True)
class LockedCommit:
    """One Git object id with its declared object format."""

    object_format: ObjectFormat
    hex: str

    def __post_init__(self) -> None:
        # Canonicalize once, then decide and store only the canonical value:
        # length, character and equality checks below must never dispatch to
        # an overridable str method.
        object.__setattr__(self, "object_format", exact_str(self.object_format))
        object.__setattr__(self, "hex", exact_str(self.hex))
        if self.object_format not in (SHA1, SHA256):
            raise _invalid(
                f"commit object_format must be 'sha1' or 'sha256', got {self.object_format!r}"
            )
        expected_length = 40 if self.object_format == SHA1 else 64
        if (
            not isinstance(self.hex, str)
            or len(self.hex) != expected_length
            or any(character not in "0123456789abcdef" for character in self.hex)
        ):
            raise _invalid(
                f"{self.object_format} commit hex must be {expected_length} lower-case hexadecimal characters"
            )

    def to_json(self) -> dict[str, str]:
        """Return the source-types schema representation."""
        return {"object_format": self.object_format, "hex": self.hex}


@dataclass(frozen=True, slots=True)
class LocalSnapshot:
    """Content identity for an admitted local snapshot."""

    snapshot: str

    @property
    def kind(self) -> Literal["local-snapshot"]:
        return "local-snapshot"

    def __post_init__(self) -> None:
        object.__setattr__(self, "snapshot", exact_str(self.snapshot))
        if not is_sha256_digest(self.snapshot):
            raise _invalid(f"local-snapshot snapshot is not a SHA-256 digest: {self.snapshot!r}")

    def to_json(self) -> dict[str, object]:
        """Return the source-types schema representation."""
        return {"kind": self.kind, "snapshot": self.snapshot}


@dataclass(frozen=True, slots=True)
class NetworkGit:
    """Portable identity for a canonical network Git repository."""

    repository: str
    commit: LockedCommit
    directory: str = "."

    @property
    def kind(self) -> Literal["network-git"]:
        return "network-git"

    def __post_init__(self) -> None:
        object.__setattr__(self, "repository", exact_str(self.repository))
        object.__setattr__(self, "directory", exact_str(self.directory))
        if not _is_repository_identity(self.repository):
            raise _invalid(
                f"network-git repository must be a canonical host/path identity: {self.repository!r}"
            )
        if not isinstance(self.commit, LockedCommit):
            raise _invalid("network-git commit must be a LockedCommit")
        if not is_valid_identity_directory(self.directory):
            raise _invalid(
                f"network-git directory must be a portable relative directory: {self.directory!r}"
            )

    def to_json(self) -> dict[str, object]:
        """Return the source-types schema representation."""
        return {
            "kind": self.kind,
            "repository": self.repository,
            "commit": self.commit.to_json(),
            "directory": self.directory,
        }


@dataclass(frozen=True, slots=True)
class ConfiguredGit:
    """Portable identity for a legacy configured-root Git repository."""

    source: str
    commit: LockedCommit
    directory: Literal["."] = "."

    @property
    def kind(self) -> Literal["configured-git"]:
        return "configured-git"

    def __post_init__(self) -> None:
        object.__setattr__(self, "source", exact_str(self.source))
        object.__setattr__(self, "directory", exact_str(self.directory))
        if not _is_portable_path(self.source):
            raise _invalid(
                f"configured-git source must be a portable relative path: {self.source!r}"
            )
        if not isinstance(self.commit, LockedCommit):
            raise _invalid("configured-git commit must be a LockedCommit")
        if not isinstance(self.directory, str) or self.directory != ".":
            raise _invalid("configured-git directory must be '.'")

    def to_json(self) -> dict[str, object]:
        """Return the source-types schema representation."""
        return {
            "kind": self.kind,
            "source": self.source,
            "commit": self.commit.to_json(),
            "directory": self.directory,
        }


PackageIdentity: TypeAlias = LocalSnapshot | NetworkGit | ConfiguredGit

# The explicit aliases keep the vocabulary readable at downstream call sites:
# all of them name the same frozen dataclasses, not dict-shaped identities.
LocalSnapshotPackage: TypeAlias = LocalSnapshot
NetworkGitPackage: TypeAlias = NetworkGit
ConfiguredGitPackage: TypeAlias = ConfiguredGit
LocalSnapshotIdentity: TypeAlias = LocalSnapshot
NetworkGitIdentity: TypeAlias = NetworkGit
ConfiguredGitIdentity: TypeAlias = ConfiguredGit
Commit: TypeAlias = LockedCommit


def is_valid_identity_directory(value: object) -> bool:
    """Mirror source-types ``directory`` without normalising its scalars."""
    if not isinstance(value, str):
        return False
    canonical = str.__str__(value)
    if canonical == ".":
        return True
    return _is_portable_path(canonical) and not any(
        character in canonical for character in "*?[]"
    )


def package_identity_to_json(identity: PackageIdentity) -> dict[str, object]:
    """Encode one typed identity without exposing machine-private bindings."""
    if isinstance(identity, LocalSnapshot):
        return identity.to_json()
    if isinstance(identity, NetworkGit):
        return identity.to_json()
    if isinstance(identity, ConfiguredGit):
        return identity.to_json()
    raise _invalid(f"unsupported package identity type: {type(identity).__name__}")


def parse_locked_commit(value: object, *, subject: str = "commit") -> LockedCommit:
    """Parse the common schema ``lockedCommit`` definition."""
    if not isinstance(value, dict):
        raise _invalid(f"{subject} must be an object")
    if set(value) != {"object_format", "hex"}:
        raise _invalid(f"{subject} has unsupported or missing fields")
    object_format = value["object_format"]
    commit_hex = value["hex"]
    if object_format not in (SHA1, SHA256) or not isinstance(commit_hex, str):
        raise _invalid(f"{subject} has an invalid object format or hex")
    try:
        return LockedCommit(object_format=object_format, hex=commit_hex)
    except SourceError as exc:
        raise _invalid(f"{subject} is invalid: {exc.detail}") from exc


def parse_package_identity(value: object) -> PackageIdentity:
    """Parse and validate one source-types schema 1 package union."""
    if not isinstance(value, dict):
        raise _invalid("package identity must be an object")
    kind = value.get("kind")
    if isinstance(kind, str):
        kind = str.__str__(kind)
    if kind == "local-snapshot":
        if set(value) != {"kind", "snapshot"}:
            raise _invalid("local-snapshot package has unsupported or missing fields")
        try:
            return LocalSnapshot(snapshot=value["snapshot"])
        except SourceError as exc:
            raise _invalid(f"local-snapshot package is invalid: {exc.detail}") from exc

    if kind == "network-git":
        if set(value) != {"kind", "repository", "commit", "directory"}:
            raise _invalid("network-git package has unsupported or missing fields")
        try:
            return NetworkGit(
                repository=value["repository"],
                commit=parse_locked_commit(value["commit"], subject="network-git commit"),
                directory=value["directory"],
            )
        except SourceError as exc:
            raise _invalid(f"network-git package is invalid: {exc.detail}") from exc

    if kind == "configured-git":
        if set(value) != {"kind", "source", "commit", "directory"}:
            raise _invalid("configured-git package has unsupported or missing fields")
        try:
            return ConfiguredGit(
                source=value["source"],
                commit=parse_locked_commit(value["commit"], subject="configured-git commit"),
                directory=value["directory"],
            )
        except SourceError as exc:
            raise _invalid(f"configured-git package is invalid: {exc.detail}") from exc

    raise _invalid(f"package identity kind is unsupported: {kind!r}")


package_identity_from_json = parse_package_identity
package_from_json = parse_package_identity
package_to_json = package_identity_to_json


def package_identity_bytes(identity: PackageIdentity) -> bytes:
    """Return the CCJ-1 bytes that identify one package."""
    return protocol_json.canonical_bytes(package_identity_to_json(identity))


def package_identity_sha256(identity: PackageIdentity) -> str:
    """Return a portable digest for the package identity itself."""
    return "sha256:" + hashlib.sha256(package_identity_bytes(identity)).hexdigest()


def configured_git_for_legacy(
    skill_name: str,
    commit: LockedCommit,
    *,
    source: str | None = None,
) -> ConfiguredGit:
    """Build the configured-root identity used by a legacy entry.

    Legacy entries use the selected repository root (``directory: "."``).
    Their configured source defaults to the skill name exactly when no explicit
    source-relative configured-root path is available.
    """
    if not identifiers.is_valid_identifier(skill_name):
        raise _invalid(f"legacy skill name is not a portable identifier: {skill_name!r}")
    selected_source = skill_name if source is None else source
    return ConfiguredGit(source=selected_source, commit=commit)


legacy_configured_git = configured_git_for_legacy


def _is_portable_path(value: object) -> bool:
    return isinstance(value, str) and identifiers.is_valid_portable_path(value)


def _is_repository_identity(value: object) -> bool:
    """Mirror source-types ``repository`` without accepting endpoint syntax."""
    if not isinstance(value, str):
        return False
    canonical = str.__str__(value)
    if not 1 <= len(canonical) <= 4096:
        return False
    host, separator, path = canonical.partition("/")
    if not separator or _REPOSITORY_HOST_RE.fullmatch(host) is None or not path:
        return False
    for component in path.split("/"):
        if not component or component in {".", ".."}:
            return False
        if any(character in _ECMA_WHITESPACE or character in "\\:%?#" for character in component):
            return False
    return True
