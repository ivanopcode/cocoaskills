"""Stable error codes for draft skillfile-sources-v1 (opt-in).

Skillfile schema 2 sources report the nine stable error classes required by
protocol skillfile-sources section 5, plus the four repository classes from
repository-transport. Each class is a module constant so that later leaves
(expansion, snapshots, lock, transport) raise identical codes.

The local-snapshot inventory also has a typed path-conflict error. It is kept
separate from the selector/name conflict because it names the two package
relative paths that cannot coexist in one admitted inventory.

This module has no intra-package imports by contract: the selection
import-closure tests pin the exact module set reachable from the
selection entry points, and anything imported here joins that reviewed
tree. Rendering lives in :mod:`csk.sources.diagnostics`; only the
dependency-free reason sanitizer stays here so the status lane can use
it without widening any closure.
"""

from __future__ import annotations

import re
from typing import Final

CODE_ALIAS_UNKNOWN: Final = "source_alias_unknown"
CODE_SELECTION_INVALID: Final = "source_selection_invalid"
CODE_MEMBER_MISSING: Final = "source_member_missing"
CODE_MEMBER_INVALID: Final = "source_member_invalid"
CODE_NAME_CONFLICT: Final = "source_name_conflict"
CODE_OUTPUT_OVERLAP: Final = "source_output_overlap"
CODE_SNAPSHOT_CHANGED: Final = "source_snapshot_changed"
CODE_SNAPSHOT_UNAVAILABLE: Final = "source_snapshot_unavailable"
CODE_LOCK_STALE: Final = "source_lock_stale"
CODE_PATH_CONFLICT: Final = "source_path_conflict"
CODE_INVENTORY_INVALID: Final = "source_inventory_invalid"
CODE_PATH_EQUIVALENCE_INVALID: Final = "source_path_equivalence_invalid"

SOURCE_DIAGNOSTICS: Final[frozenset[str]] = frozenset(
    {
        CODE_ALIAS_UNKNOWN,
        CODE_SELECTION_INVALID,
        CODE_MEMBER_MISSING,
        CODE_MEMBER_INVALID,
        CODE_NAME_CONFLICT,
        CODE_OUTPUT_OVERLAP,
        CODE_SNAPSHOT_CHANGED,
        CODE_SNAPSHOT_UNAVAILABLE,
        CODE_LOCK_STALE,
        CODE_PATH_CONFLICT,
        CODE_INVENTORY_INVALID,
        CODE_PATH_EQUIVALENCE_INVALID,
    }
)

#: The one draft label every user-facing schema-2 surface prints verbatim.
DRAFT_SKILLFILE_SOURCES_LABEL: Final = "draft skillfile-sources-v1 (opt-in)"

# URL credentials are redacted structurally, never by character class: a
# credential is whatever sits between ``://`` and the last ``@`` of the
# URL token (a bare user can itself be a token, and passwords may
# contain ``/``, spaces or ``+``), and query-string credential
# parameters count too. Tokens inside a quoted span (echo sites embed
# declarations via ``{value!r}``) run to the closing quote; unquoted
# tokens stop at whitespace so surrounding prose is never eaten.
_QUOTED_SPAN_RE: Final = re.compile(r"'[^'\n]*'|\"[^\"\n]*\"")
_UNQUOTED_TOKEN_END_RE: Final = re.compile(r"[\s'\"`]")
_QUERY_CREDENTIAL_RE: Final = re.compile(
    r"([?&])([^&#\s'\"`;=]+)=([^&#\s'\"`;)]*)"
)
_CREDENTIAL_QUERY_NAMES: Final = frozenset(
    {
        "token",
        "private_token",
        "access_token",
        "auth_token",
        "auth",
        "password",
        "passwd",
        "pwd",
        "secret",
        "client_secret",
        "api_key",
        "apikey",
        "key",
    }
)
_CREDENTIAL_QUERY_SUBSTRINGS: Final = ("token", "secret")
_REDACTED_USERINFO: Final = "***"
_UNC_PATH_RE: Final = re.compile(r"\\\\[^\s'\"`]+")
# The drive letter must not itself follow a letter, or URL schemes
# (``https:``) read as drive-relative paths and the whole URL redacts.
_DRIVE_PATH_RE: Final = re.compile(r"(?<![A-Za-z])[A-Za-z]:[\\/][^\s'\"`]+")
# An absolute POSIX path starts at a run of slashes that is NOT part of
# a word (so ``and/or`` and ``host/path`` survive), NOT a drive
# spillover (``C:/x`` is claimed by the drive pattern first), NOT
# relative (``./x`` keeps its dot), and NOT a URL scheme: the lookbehind
# also excludes ``:`` and ``/`` so neither slash of ``https://``
# matches even after userinfo redaction rewrites the authority.
_POSIX_ABSPATH_RE: Final = re.compile(r"(?<![A-Za-z0-9_:/.])/+(?!/)[^\s'\"`]+")

_REDACTED_PATH: Final = "<path>"


class SourceError(ValueError):
    """One stable draft-sources diagnostic with a machine-readable code."""

    def __init__(self, code: str, detail: str) -> None:
        super().__init__(f"{code}: {detail}")
        self.code = code
        self.detail = detail


class SourcePathConflictError(SourceError):
    """Two admitted package-relative paths occupy one filesystem name."""

    def __init__(self, first_path: str, second_path: str) -> None:
        self.first_path = first_path
        self.second_path = second_path
        super().__init__(
            CODE_PATH_CONFLICT,
            f"inventory paths {first_path!r} and {second_path!r} collide",
        )


def _redact_span_userinfo(span: str) -> str:
    """Redact userinfo inside one quoted span holding ``://``."""

    parts: list[str] = []
    cursor = 0
    while True:
        scheme = span.find("://", cursor)
        if scheme == -1:
            parts.append(span[cursor:])
            break
        authority_start = scheme + len("://")
        following = span.find("://", authority_start)
        region_end = following if following != -1 else len(span)
        region = span[authority_start:region_end]
        marker = region.rfind("@")
        if marker <= 0:
            parts.append(span[cursor:region_end])
        else:
            parts.append(span[cursor:authority_start])
            parts.append(_REDACTED_USERINFO)
            parts.append(region[marker:])
        cursor = region_end
    return "".join(parts)


def _redact_unquoted_userinfo(text: str) -> str:
    """Redact userinfo in URL tokens outside quoted spans."""

    parts: list[str] = []
    cursor = 0
    while True:
        scheme = text.find("://", cursor)
        if scheme == -1:
            parts.append(text[cursor:])
            break
        authority_start = scheme + len("://")
        token_match = _UNQUOTED_TOKEN_END_RE.search(text, authority_start)
        token_end = token_match.start() if token_match else len(text)
        region = text[authority_start:token_end]
        marker = region.rfind("@")
        if marker <= 0:
            parts.append(text[cursor:token_end])
        else:
            parts.append(text[cursor:authority_start])
            parts.append(_REDACTED_USERINFO)
            parts.append(region[marker:])
        cursor = token_end
    return "".join(parts)


def _is_credential_query_name(name: str) -> bool:
    folded = name.lower()
    if folded in _CREDENTIAL_QUERY_NAMES:
        return True
    return any(part in folded for part in _CREDENTIAL_QUERY_SUBSTRINGS)


def _redact_query_credentials(text: str) -> str:
    def replace(match: re.Match[str]) -> str:
        if _is_credential_query_name(match.group(2)):
            return f"{match.group(1)}{match.group(2)}={_REDACTED_USERINFO}"
        return match.group(0)

    return _QUERY_CREDENTIAL_RE.sub(replace, text)


def sanitize_detail(text: str) -> str:
    """Redact secrets from a diagnostic reason while keeping its subject.

    URL userinfo, query-string credential parameters and
    absolute-path-shaped tokens (POSIX, drive-letter and UNC) are
    replaced; selectors, member names, relative paths and canonical
    ``host/path`` identities are preserved byte-exactly.
    """

    segments: list[str] = []
    cursor = 0
    for span in _QUOTED_SPAN_RE.finditer(text):
        segments.append(_redact_unquoted_userinfo(text[cursor : span.start()]))
        segments.append(_redact_span_userinfo(span.group(0)))
        cursor = span.end()
    segments.append(_redact_unquoted_userinfo(text[cursor:]))
    redacted = _redact_query_credentials("".join(segments))
    redacted = _UNC_PATH_RE.sub(_REDACTED_PATH, redacted)
    redacted = _DRIVE_PATH_RE.sub(_REDACTED_PATH, redacted)
    return _POSIX_ABSPATH_RE.sub(_REDACTED_PATH, redacted)
