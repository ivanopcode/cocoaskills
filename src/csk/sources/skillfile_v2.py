"""Skillfile schema 2 source model and structural parser (draft, opt-in).

This module implements protocol skillfile-sources section 1 and
repository-transport section 1 for Skillfile schema 2: the optional ``sources``
map of aliases to acquisition objects and the three disjoint ``skills``
element forms. Validation is hand-written and mirrors
``schemas/draft-sources-v1/skillfile-v2.schema.json``; production code never
depends on ``jsonschema``.

Only structural parsing and validation live here. Collection expansion
(filesystem enumeration) belongs to a later leaf, as do snapshots, locks,
policy and transport.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from typing import Any, Final, Literal

from .. import identifiers, protocol_json
from .errors import (
    CODE_ALIAS_UNKNOWN,
    CODE_SELECTION_INVALID,
    SourceError,
)

SkillfileScope = Literal["project", "global", "transitive"]

VALID_SCOPES: Final = ("project", "global", "transitive")

_MAX_DECLARATION_CHARS: Final = 4096
_MAX_REF_CHARS: Final = 255
_MAX_REF_UTF8_BYTES: Final = 255

_HOST_RE: Final = re.compile(r"[A-Za-z0-9][A-Za-z0-9.-]*")
_CANONICAL_HOST_RE: Final = re.compile(r"[a-z0-9][a-z0-9.-]*")
_SSH_USER_RE: Final = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}")
_COMMIT_RE: Final = re.compile(r"[0-9a-f]{40}(?:[0-9a-f]{24})?")
_GIT_REF_FORBIDDEN_RE: Final = re.compile(r"[\x00-\x20\x7f~^:?*\[\\]")
_SSH_PATH_COMPONENT_RE: Final = re.compile(r"[A-Za-z0-9._-]+")

# JSON Schema ``\s`` (ECMA WhiteSpace) used by the ``repository`` pattern. It
# is narrower than Python ``str.isspace`` (no U+001C-U+001F, no U+0085), so it
# is spelled out to mirror the schema exactly.
_ECMA_WHITESPACE: Final = frozenset(
    [
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
    ]
)

_REF_KINDS: Final = ("tag", "branch", "revision")
_LEGACY_SKILL_KEYS: Final = frozenset({"source", "git", "tag", "branch", "revision"})
_INDIVIDUAL_KEYS: Final = frozenset({"name", "from", "directory"})
_COLLECTION_KEYS: Final = frozenset({"from", "directory", "include", "exclude"})


@dataclass(frozen=True)
class PathSource:
    """Local filesystem acquisition: a literal native directory path."""

    path: str


@dataclass(frozen=True)
class GitSource:
    """Network acquisition by endpoint URL plus exactly one structured ref."""

    git: str
    ref_kind: str
    ref_value: str
    identity: str
    transport: str


@dataclass(frozen=True)
class RepositorySource:
    """Network acquisition by already-canonical identity plus one ref."""

    repository: str
    ref_kind: str
    ref_value: str


SourceAcquisition = PathSource | GitSource | RepositorySource


@dataclass(frozen=True)
class IndividualSelector:
    """One ``{name, from, directory}`` skill selector."""

    name: str
    from_alias: str
    directory: str


@dataclass(frozen=True)
class CollectionSelector:
    """One ``{from, directory, include, exclude?}`` skill selector."""

    from_alias: str
    directory: str
    include: tuple[str, ...]
    exclude: tuple[str, ...] = ()


SkillSelector = IndividualSelector | CollectionSelector


def check_scope(scope: str) -> SkillfileScope:
    """Validate a parse scope, failing closed on unknown values."""
    if scope not in VALID_SCOPES:
        raise ValueError(f"unknown Skillfile scope: {scope!r}")
    return scope  # type: ignore[return-value]


def manifest_sha256(data: dict[str, Any]) -> str:
    """Return the CCJ-1 ``sha256:`` digest of the entire parsed Skillfile."""
    digest = hashlib.sha256(protocol_json.canonical_bytes(data)).hexdigest()
    return f"sha256:{digest}"


def is_valid_git_ref_name(value: str) -> bool:
    """Mirror the schema ``gitRefName`` grammar plus the core byte rule."""
    if not isinstance(value, str):
        return False
    if not 1 <= len(value) <= _MAX_REF_CHARS:
        return False
    if len(value.encode("utf-8")) > _MAX_REF_UTF8_BYTES:
        return False
    if value == "@":
        return False
    if value.startswith("/") or value.endswith("/") or value.endswith("."):
        return False
    if "//" in value or ".." in value or "@{" in value:
        return False
    if _GIT_REF_FORBIDDEN_RE.search(value) is not None:
        return False
    for component in value.split("/"):
        if not component or component.startswith(".") or component.endswith(".lock"):
            return False
    return True


def is_valid_commit(value: str) -> bool:
    """Return whether a revision is a full lowercase Git object id."""
    return isinstance(value, str) and _COMMIT_RE.fullmatch(value) is not None


def is_valid_selector_directory(value: str) -> bool:
    """Return whether a selector directory is root or a contained path.

    ``"."`` selects the source root. Any other directory is a portable
    contained path that additionally rejects glob metacharacters.
    """
    if not isinstance(value, str) or not value:
        return False
    if value == ".":
        return True
    if any(character in value for character in "*?[]"):
        return False
    return identifiers.is_valid_portable_path(value)


def parse_sources(
    raw: Any, *, scope: SkillfileScope | str = "project"
) -> dict[str, SourceAcquisition]:
    """Parse the ``sources`` map of aliases to acquisitions.

    The caller distinguishes an absent key (no sources) from a present
    value: an explicit null or any other non-object fails structurally.
    """
    resolved_scope = check_scope(scope)
    if not isinstance(raw, dict):
        raise SourceError(
            CODE_SELECTION_INVALID, "Skillfile field 'sources' must be an object when present"
        )
    sources: dict[str, SourceAcquisition] = {}
    for alias, entry in raw.items():
        if not isinstance(alias, str) or not identifiers.is_valid_identifier(alias):
            raise SourceError(
                CODE_SELECTION_INVALID,
                f"Source alias {alias!r} must be a portable identifier",
            )
        if not isinstance(entry, dict):
            raise SourceError(
                CODE_SELECTION_INVALID, f"Source {alias!r} must be an acquisition object"
            )
        sources[alias] = _parse_acquisition(alias, entry, scope=resolved_scope)
    return sources


def _parse_acquisition(
    alias: str, entry: dict[str, Any], *, scope: SkillfileScope
) -> SourceAcquisition:
    keys = set(entry)
    if "path" in keys:
        unknown = sorted(keys - {"path"})
        if unknown:
            raise SourceError(
                CODE_SELECTION_INVALID,
                f"Source {alias!r} has unsupported field(s): {', '.join(unknown)}",
            )
        if scope == "transitive":
            raise SourceError(
                CODE_SELECTION_INVALID,
                f"Source {alias!r} must not declare a host path in a transitive Skillfile",
            )
        path = entry["path"]
        if (
            not isinstance(path, str)
            or not 1 <= len(path) <= _MAX_DECLARATION_CHARS
            or any(_is_control_scalar(character) for character in path)
        ):
            raise SourceError(
                CODE_SELECTION_INVALID,
                f"Source {alias!r} field 'path' must be a non-empty native path",
            )
        return PathSource(path=path)
    unknown = sorted(keys - {"git", "repository", "tag", "branch", "revision"})
    if unknown:
        raise SourceError(
            CODE_SELECTION_INVALID,
            f"Source {alias!r} has unsupported field(s): {', '.join(unknown)}",
        )
    has_git = "git" in keys
    has_repository = "repository" in keys
    if has_git and has_repository:
        raise SourceError(
            CODE_SELECTION_INVALID,
            f"Source {alias!r} must declare exactly one of 'git' or 'repository'",
        )
    if not has_git and not has_repository:
        raise SourceError(
            CODE_SELECTION_INVALID,
            f"Source {alias!r} must declare 'path', 'git', or 'repository'",
        )
    ref_keys = [key for key in _REF_KINDS if key in keys]
    if len(ref_keys) != 1:
        raise SourceError(
            CODE_SELECTION_INVALID,
            f"Source {alias!r} must specify exactly one of tag, branch, or revision",
        )
    ref_kind = ref_keys[0]
    ref_value = entry[ref_kind]
    _check_ref(alias, ref_kind, ref_value)
    if ref_kind == "branch" and scope != "project":
        raise SourceError(
            CODE_SELECTION_INVALID,
            f"Source {alias!r} declares branch {ref_value!r}, "
            "but branches are admitted only in the root project Skillfile",
        )
    if has_git:
        transport, identity = _parse_git_declaration(alias, entry["git"])
        return GitSource(
            git=entry["git"],
            ref_kind=ref_kind,
            ref_value=ref_value,
            identity=identity,
            transport=transport,
        )
    identity = _parse_repository_declaration(alias, entry["repository"])
    return RepositorySource(repository=identity, ref_kind=ref_kind, ref_value=ref_value)


def _check_ref(alias: str, ref_kind: str, ref_value: Any) -> None:
    if ref_kind == "revision":
        if not isinstance(ref_value, str) or not is_valid_commit(ref_value):
            raise SourceError(
                CODE_SELECTION_INVALID,
                f"Source {alias!r} field 'revision' must be a full lowercase Git object id",
            )
        return
    if (
        not isinstance(ref_value, str)
        or not ref_value
        or not is_valid_git_ref_name(ref_value)
    ):
        raise SourceError(
            CODE_SELECTION_INVALID,
            f"Source {alias!r} field {ref_kind!r} must be a valid Git ref name",
        )


def _parse_git_declaration(alias: str, value: Any) -> tuple[str, str]:
    """Validate the closed core 6.3 endpoint grammar; return transport/identity."""
    if (
        not isinstance(value, str)
        or not 1 <= len(value) <= _MAX_DECLARATION_CHARS
        or any(_is_control_scalar(character) for character in value)
    ):
        raise SourceError(
            CODE_SELECTION_INVALID,
            f"Source {alias!r} field 'git' must be a non-empty endpoint URL",
        )
    if value.startswith("https://"):
        return "https", _git_identity(alias, _split_https(alias, value))
    if value.startswith("ssh://"):
        return "ssh", _git_identity(alias, _split_ssh_uri(alias, value))
    if "://" in value:
        raise SourceError(
            CODE_SELECTION_INVALID,
            f"Source {alias!r} field 'git' must use https, ssh, or scp spelling",
        )
    return "ssh", _git_identity(alias, _split_scp(alias, value))


def _split_https(alias: str, value: str) -> tuple[str, str]:
    rest = value[len("https://") :]
    authority, separator, path = rest.partition("/")
    if not separator or not path:
        raise _git_error(alias, "requires a repository path")
    if "@" in authority or ":" in authority:
        raise _git_error(alias, "must not contain userinfo, a password, or a port")
    _check_host(alias, authority)
    _check_https_path(alias, path)
    return authority, path


def _split_ssh_uri(alias: str, value: str) -> tuple[str, str]:
    rest = value[len("ssh://") :]
    authority, separator, path = rest.partition("/")
    if not separator or not path:
        raise _git_error(alias, "requires a repository path")
    if "@" in authority:
        user, _, host = authority.partition("@")
        if "@" in host or not _SSH_USER_RE.fullmatch(user):
            raise _git_error(alias, "carries an invalid SSH username")
    else:
        host = authority
    if ":" in host:
        raise _git_error(alias, "must not contain a password or an explicit port")
    _check_host(alias, host)
    _check_ssh_path(alias, path)
    return host, path


def _split_scp(alias: str, value: str) -> tuple[str, str]:
    if value.startswith(":") or ":" not in value:
        raise _git_error(alias, "must use https, ssh, or scp spelling")
    head, _, path = value.partition(":")
    if not path:
        raise _git_error(alias, "requires a repository path")
    if "@" in head:
        user, _, host = head.partition("@")
        if "@" in host or not _SSH_USER_RE.fullmatch(user):
            raise _git_error(alias, "carries an invalid SSH username")
    else:
        host = head
    if not host:
        raise _git_error(alias, "requires a host")
    _check_host(alias, host)
    _check_ssh_path(alias, path)
    return host, path


def _git_identity(alias: str, parts: tuple[str, str]) -> str:
    host, path = parts
    canonical_path = path.strip("/")
    if canonical_path.endswith(".git"):
        canonical_path = canonical_path[: -len(".git")]
    canonical_path = canonical_path.rstrip("/")
    if not canonical_path:
        raise SourceError(
            CODE_SELECTION_INVALID,
            f"Source {alias!r} field 'git' has an empty repository path",
        )
    identity = f"{host.lower()}/{canonical_path}"
    if len(identity) > _MAX_DECLARATION_CHARS:
        raise SourceError(
            CODE_SELECTION_INVALID,
            f"Source {alias!r} field 'git' exceeds 4096 characters of canonical identity",
        )
    return identity


def _git_error(alias: str, reason: str) -> SourceError:
    """Refuse a git declaration without echoing it.

    The refusal names the source alias, the field and the shape of
    the violation. The raw declaration is never reproduced: it may
    carry credentials (userinfo, tokens, query parameters) that no
    display-time redaction can reliably remove.
    """

    return SourceError(
        CODE_SELECTION_INVALID, f"Source {alias!r} field 'git' {reason}"
    )


def _check_host(alias: str, host: str) -> None:
    if not host or _HOST_RE.fullmatch(host) is None:
        raise _git_error(alias, "carries an invalid host")


def _check_https_path(alias: str, path: str) -> None:
    for component in path.split("/"):
        if not component or component in {".", ".."}:
            raise _git_error(alias, "carries an empty, dot, or parent path component")
        for character in component:
            if (
                character.isspace()
                or _is_control_scalar(character)
                or character in "%?#\\:"
            ):
                raise _git_error(alias, "carries an invalid repository path")


def _check_ssh_path(alias: str, path: str) -> None:
    for component in path.split("/"):
        if (
            not component
            or component in {".", ".."}
            or _SSH_PATH_COMPONENT_RE.fullmatch(component) is None
        ):
            raise _git_error(alias, "carries an invalid repository path")


def _parse_repository_declaration(alias: str, value: Any) -> str:
    """Validate an already-canonical ``host/path`` identity declaration."""
    if not isinstance(value, str) or not 1 <= len(value) <= _MAX_DECLARATION_CHARS:
        raise SourceError(
            CODE_SELECTION_INVALID,
            f"Source {alias!r} field 'repository' must be a canonical host/path identity",
        )
    host, separator, path = value.partition("/")
    if not separator or not path:
        raise SourceError(
            CODE_SELECTION_INVALID,
            f"Source {alias!r} field 'repository' must be a canonical host/path identity",
        )
    if _CANONICAL_HOST_RE.fullmatch(host) is None:
        raise SourceError(
            CODE_SELECTION_INVALID,
            f"Source {alias!r} field 'repository' carries a non-canonical host",
        )
    for component in path.split("/"):
        if not component or component in {".", ".."}:
            raise SourceError(
                CODE_SELECTION_INVALID,
                f"Source {alias!r} field 'repository' carries an empty, dot, or parent component",
            )
        for character in component:
            if character in _ECMA_WHITESPACE or character in "\\:%?#":
                raise SourceError(
                    CODE_SELECTION_INVALID,
                    f"Source {alias!r} field 'repository' carries an invalid repository path",
                )
    if value.endswith(".git"):
        raise SourceError(
            CODE_SELECTION_INVALID,
            f"Source {alias!r} field 'repository' must already be canonical: "
            "a terminal .git is never declared",
        )
    return value


def _is_control_scalar(character: str) -> bool:
    code = ord(character)
    return code < 0x20 or 0x7F <= code <= 0x9F


def parse_selector(
    raw: dict[str, Any],
    index: int,
    *,
    sources: dict[str, SourceAcquisition],
) -> SkillSelector:
    """Parse one ``skills`` element that carries ``from`` as a selector.

    The caller routes elements without ``from`` to the unchanged legacy
    grammar; mixes of legacy and selector fields fail here.
    """
    keys = set(raw)
    legacy_keys = sorted(keys & _LEGACY_SKILL_KEYS)
    if legacy_keys:
        raise SourceError(
            CODE_SELECTION_INVALID,
            f"Skill selector at index {index} mixes selector and legacy fields: "
            f"{', '.join(legacy_keys)}",
        )
    has_name = "name" in keys
    has_include = "include" in keys
    if has_name and has_include:
        raise SourceError(
            CODE_SELECTION_INVALID,
            f"Skill selector at index {index} mixes individual and collection fields",
        )
    if not has_name and not has_include:
        raise SourceError(
            CODE_SELECTION_INVALID,
            f"Skill selector at index {index} must declare 'name' or 'include'",
        )
    allowed = _INDIVIDUAL_KEYS if has_name else _COLLECTION_KEYS
    unknown = sorted(keys - allowed)
    if unknown:
        raise SourceError(
            CODE_SELECTION_INVALID,
            f"Skill selector at index {index} has unsupported field(s): {', '.join(unknown)}",
        )
    from_alias = raw.get("from")
    if not isinstance(from_alias, str) or not identifiers.is_valid_identifier(from_alias):
        raise SourceError(
            CODE_SELECTION_INVALID,
            f"Skill selector at index {index} field 'from' must be a portable identifier",
        )
    if from_alias not in sources:
        raise SourceError(
            CODE_ALIAS_UNKNOWN,
            f"Skill selector at index {index} names unknown source {from_alias!r}",
        )
    directory = raw.get("directory")
    if not isinstance(directory, str) or not is_valid_selector_directory(directory):
        raise SourceError(
            CODE_SELECTION_INVALID,
            f"Skill selector at index {index} field 'directory' must be '.' "
            "or a portable contained path",
        )
    if has_name:
        name = raw.get("name")
        if not isinstance(name, str) or not identifiers.is_valid_identifier(name):
            raise SourceError(
                CODE_SELECTION_INVALID,
                f"Skill selector at index {index} field 'name' must be a portable identifier",
            )
        return IndividualSelector(name=name, from_alias=from_alias, directory=directory)
    include = _parse_member_list(raw.get("include"), index, field="include", allow_star=True)
    if not include:
        raise SourceError(
            CODE_SELECTION_INVALID,
            f"Skill selector at index {index} field 'include' must be a non-empty unique list",
        )
    exclude = _parse_member_list(raw.get("exclude", []), index, field="exclude", allow_star=False)
    return CollectionSelector(
        from_alias=from_alias, directory=directory, include=tuple(include), exclude=tuple(exclude)
    )


def _parse_member_list(
    raw: Any, index: int, *, field: str, allow_star: bool
) -> list[str]:
    if not isinstance(raw, list):
        raise SourceError(
            CODE_SELECTION_INVALID,
            f"Skill selector at index {index} field {field!r} must be a list",
        )
    for member in raw:
        if not isinstance(member, str):
            raise SourceError(
                CODE_SELECTION_INVALID,
                f"Skill selector at index {index} field {field!r} must contain strings",
            )
        if allow_star and member == "*":
            continue
        if not identifiers.is_valid_identifier(member):
            raise SourceError(
                CODE_SELECTION_INVALID,
                f"Skill selector at index {index} field {field!r} "
                f"carries an invalid member {member!r}",
            )
    if len(set(raw)) != len(raw):
        raise SourceError(
            CODE_SELECTION_INVALID,
            f"Skill selector at index {index} field {field!r} must not contain duplicates",
        )
    return list(raw)
