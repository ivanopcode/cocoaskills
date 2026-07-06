from __future__ import annotations

import re
from urllib.parse import urlsplit

# Canonical source identity for git artifacts: "host/path" with a lowercase
# host, the transport and a trailing ".git" removed, and the path preserved
# case-sensitively. SSH and HTTPS URLs of one repository normalize to one
# identity, so allowlist prefixes and closure unification treat them as equal.

_URL_SCHEMES = {"ssh", "git", "http", "https"}

# scp-style remote: [user@]host:path (no scheme). The host part must look like
# a hostname, which keeps Windows drive paths (C:\x) and plain local paths out.
_SCP_RE = re.compile(r"^(?:[^@/\s]+@)?(?P<host>[A-Za-z0-9][A-Za-z0-9.-]*):(?P<path>[^\\]+)$")


def canonical_source_identity(url: str) -> str | None:
    """Return the canonical "host/path" identity, or None for local sources.

    Local filesystem paths and file:// URLs carry no network identity; the
    source allowlist gates network fetches only.
    """
    value = url.strip()
    if not value:
        return None
    split = urlsplit(value)
    scheme = split.scheme.lower()
    if scheme in _URL_SCHEMES and split.netloc:
        host = (split.hostname or "").lower()
        path = split.path
    elif scheme == "file" or value.startswith(("/", "./", "../", "~")):
        return None
    else:
        match = _SCP_RE.match(value)
        if match is None:
            return None
        host = match.group("host").lower()
        path = match.group("path")
        if len(host) == 1:
            # Single-letter host is a Windows drive, not a hostname.
            return None
    path = path.strip("/")
    if path.endswith(".git"):
        path = path[: -len(".git")]
    path = path.rstrip("/")
    if not host or not path:
        return None
    return f"{host}/{path}"


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
