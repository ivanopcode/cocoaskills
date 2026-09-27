from __future__ import annotations

import re
from urllib.parse import urlsplit

from .identifiers import is_valid_portable_path

# Canonical source identity for git artifacts: "host/path" with a lowercase
# host, the transport and a trailing ".git" removed, and the path preserved
# case-sensitively. SSH and HTTPS URLs of one repository normalize to one
# identity, so allowlist prefixes and closure unification treat them as equal.

_URL_SCHEMES = {"ssh", "git", "http", "https"}

# scp-style remote: [user@]host:path (no scheme). The host part must look like
# a hostname, which keeps Windows drive paths (C:\x) and plain local paths out.
_SCP_RE = re.compile(r"^(?:[^@/\s]+@)?(?P<host>[A-Za-z0-9][A-Za-z0-9.-]*):(?P<path>[^\\]+)$")
_HOST_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9.-]*$")


class SourceIdentityError(ValueError):
    pass


def canonical_source_identity(url: str) -> str | None:
    """Return the canonical "host/path" identity, or None for local sources.

    Local filesystem paths and file:// URLs carry no network identity; the
    source allowlist gates network fetches only.
    """
    return parse_source_identity(url)


def parse_source_identity(url: str) -> str | None:
    """Parse one source, rejecting malformed network forms instead of treating them as local."""
    value = url.strip()
    if not value:
        return None
    try:
        split = urlsplit(value)
    except ValueError as exc:
        raise SourceIdentityError("invalid source URL") from exc
    scheme = split.scheme.lower()
    if scheme in _URL_SCHEMES:
        if not split.netloc:
            raise SourceIdentityError("network source requires a host")
        if split.password is not None:
            raise SourceIdentityError("network source must not contain a password")
        try:
            port = split.port
        except ValueError as exc:
            raise SourceIdentityError("invalid explicit port in source") from exc
        if port is not None:
            raise SourceIdentityError("network source must not contain an explicit port")
        if split.query:
            raise SourceIdentityError("network source must not contain a query")
        if split.fragment:
            raise SourceIdentityError("network source must not contain a fragment")
        if "%" in value:
            raise SourceIdentityError("network source must not contain percent escapes")
        if "\\" in value:
            raise SourceIdentityError("network source must not contain backslashes")
        host = (split.hostname or "").lower()
        path = split.path
    elif scheme == "file" or value.startswith(("/", "./", "../", "~")):
        return None
    else:
        if "://" in value:
            raise SourceIdentityError("unsupported network source scheme")
        match = _SCP_RE.match(value)
        if match is None:
            if "://" in value or "@" in value or (":" in value and not re.match(r"^[A-Za-z]:[\\/]", value)):
                raise SourceIdentityError("invalid network source")
            return None
        host = match.group("host").lower()
        path = match.group("path")
        if len(host) == 1:
            # Single-letter host is a Windows drive, not a hostname.
            return None
        if any(token in value for token in ("%", "?", "#", "\\")):
            raise SourceIdentityError("invalid SCP source")
    if _HOST_RE.fullmatch(host) is None:
        raise SourceIdentityError("network source has an invalid host")
    path = path.strip("/")
    if path.endswith(".git"):
        path = path[: -len(".git")]
    path = path.rstrip("/")
    if not _valid_repository_path(path):
        raise SourceIdentityError("network source has an invalid repository path")
    canonical = f"{host}/{path}"
    if len(canonical) > 4096:
        raise SourceIdentityError("canonical network source identity exceeds 4096 characters")
    return canonical


def is_canonical_source_identity(value: str) -> bool:
    """Validate the lowercase host/path form used by signed registry records."""
    host, separator, path = value.partition("/")
    return bool(separator and host == host.lower() and _HOST_RE.fullmatch(host) and _valid_repository_path(path))


def _valid_repository_path(path: str) -> bool:
    return bool(
        path
        and is_valid_portable_path(path)
        and not any(character.isspace() or character in "%?#" for character in path)
    )


def matches_prefix(identity: str, prefix: str) -> bool:
    """Segment-aware prefix match: "h/skills" matches "h/skills/x", never "h/skills-evil"."""
    trimmed = prefix.strip().rstrip("/")
    if not trimmed:
        return False
    return identity == trimmed or identity.startswith(trimmed + "/")


def is_allowed(identity: str | None, allowed_prefixes: tuple[str, ...]) -> bool:
    """Check an identity against the machine allowlist.

    An empty allowlist allows every source. A None identity is a local
    filesystem source, which involves no network operation and passes.
    """
    if not allowed_prefixes:
        return True
    if identity is None:
        return True
    return any(matches_prefix(identity, prefix) for prefix in allowed_prefixes)
