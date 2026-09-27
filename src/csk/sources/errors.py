"""Stable error codes for Skillfile schema 2 source handling.

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

# URL redaction follows one bounded grammar: an RFC 3986 scheme starts a token;
# the token ends at whitespace, or at the matching quote when the scheme is
# immediately quoted; authority, query and fragment then have separate bounds.
_URL_SCHEME_RE: Final = re.compile(r"[A-Za-z][A-Za-z0-9+.-]*://")
_URL_AUTHORITY_END_RE: Final = re.compile(r"[/?#]")
_URL_TEXT_QUOTES: Final = frozenset("'\"`")
_SCP_WORD_RE: Final = re.compile(r"\S+")
_SCP_USERNAME_RE: Final = re.compile(r"[A-Za-z0-9._-]+")
_SCP_REMOTE_RE: Final = re.compile(r"[A-Za-z0-9][A-Za-z0-9.-]*:\S+")
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
        "sig",
        "signature",
        "x-amz-signature",
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


def _is_credential_query_name(name: str) -> bool:
    folded = name.casefold()
    if folded in _CREDENTIAL_QUERY_NAMES:
        return True
    return any(part in folded for part in _CREDENTIAL_QUERY_SUBSTRINGS)


def _url_token_end(
    text: str,
    scheme_start: int,
    authority_start: int,
    next_scheme_start: int | None,
) -> int:
    """Return one URL's boundary without consuming a later scheme occurrence."""

    line_end = len(text)
    for line_break in ("\n", "\r"):
        position = text.find(line_break, scheme_start)
        if position != -1:
            line_end = min(line_end, position)

    token_end = line_end
    if next_scheme_start is not None:
        token_end = min(token_end, next_scheme_start)
    for position in range(scheme_start, token_end):
        if text[position].isspace():
            token_end = position
            break

    quote = text[scheme_start - 1] if scheme_start else ""
    if quote in _URL_TEXT_QUOTES:
        quote_end = text.find(quote, scheme_start, token_end)
        while quote_end != -1:
            # Apostrophe is an RFC 3986 userinfo sub-delimiter. If another
            # authority @ follows it, this is credential data, not the quote
            # which closes the diagnostic's quoted URL.
            if quote == "'":
                authority_end_match = _URL_AUTHORITY_END_RE.search(
                    text, authority_start, token_end
                )
                authority_end = (
                    authority_end_match.start()
                    if authority_end_match
                    else token_end
                )
                if (
                    quote_end < authority_end
                    and "@" in text[quote_end + 1 : authority_end]
                ):
                    quote_end = text.find(quote, quote_end + 1, token_end)
                    continue
            token_end = quote_end
            break
    return token_end


def _redact_url_query(token: str, authority_start: int) -> str:
    question = token.find("?", authority_start)
    fragment = token.find("#", authority_start)
    if question == -1 or (fragment != -1 and fragment < question):
        return token
    query_end = token.find("#", question + 1)
    if query_end == -1:
        query_end = len(token)

    query = token[question + 1 : query_end]
    pairs: list[str] = []
    for pair in query.split("&"):
        name, separator, value = pair.partition("=")
        if separator and _is_credential_query_name(name):
            pairs.append(f"{name}={_REDACTED_USERINFO}")
        else:
            pairs.append(pair)
    return token[: question + 1] + "&".join(pairs) + token[query_end:]


def _redact_url_token(token: str, authority_start: int) -> str:
    authority_end_match = _URL_AUTHORITY_END_RE.search(token, authority_start)
    authority_end = (
        authority_end_match.start() if authority_end_match else len(token)
    )
    authority = token[authority_start:authority_end]
    marker = authority.rfind("@")
    if marker >= 0:
        token = (
            token[:authority_start]
            + _REDACTED_USERINFO
            + authority[marker:]
            + token[authority_end:]
        )
    return _redact_url_query(token, authority_start)


def _redact_url_tokens(text: str) -> str:
    parts: list[str] = []
    cursor = 0
    schemes = tuple(_URL_SCHEME_RE.finditer(text))
    for index, scheme in enumerate(schemes):
        next_scheme_start = (
            schemes[index + 1].start() if index + 1 < len(schemes) else None
        )
        authority_start = scheme.end() - scheme.start()
        token_end = _url_token_end(
            text, scheme.start(), scheme.end(), next_scheme_start
        )
        parts.extend(
            (
                text[cursor : scheme.start()],
                _redact_url_token(text[scheme.start() : token_end], authority_start),
            )
        )
        cursor = token_end
    parts.append(text[cursor:])
    return "".join(parts)


def _redact_scp_word(word: str) -> str:
    """Redact only a whole whitespace-delimited scp-like credential word."""

    if "://" in word:
        return word
    marker = word.rfind("@")
    if marker <= 0:
        return word
    userinfo = word[:marker]
    remote = word[marker + 1 :]
    username, separator, _password = userinfo.partition(":")
    if (
        not separator
        or _SCP_USERNAME_RE.fullmatch(username) is None
        or not _SCP_REMOTE_RE.fullmatch(remote)
    ):
        return word
    return f"{_REDACTED_USERINFO}@{remote}"


def _redact_scp_credentials(text: str) -> str:
    return _SCP_WORD_RE.sub(lambda match: _redact_scp_word(match.group(0)), text)


def sanitize_credentials(text: str) -> str:
    """Structurally redact URL userinfo and query-string credentials."""
    return _redact_scp_credentials(_redact_url_tokens(text))


def sanitize_detail(text: str) -> str:
    """Redact secrets and absolute paths while keeping the diagnostic subject.

    URL userinfo, query-string credential parameters and
    absolute-path-shaped tokens (POSIX, drive-letter and UNC) are
    replaced; selectors, member names, relative paths and canonical
    ``host/path`` identities are preserved byte-exactly.
    """

    redacted = sanitize_credentials(text)
    redacted = _UNC_PATH_RE.sub(_REDACTED_PATH, redacted)
    redacted = _DRIVE_PATH_RE.sub(_REDACTED_PATH, redacted)
    return _POSIX_ABSPATH_RE.sub(_REDACTED_PATH, redacted)
