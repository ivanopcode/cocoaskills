from __future__ import annotations

import re
from urllib.parse import urlsplit, urlunsplit


URL_RE = re.compile(r"https?://[A-Za-z0-9._~:/?#\[\]@!$&'()*+,;=%-]+")
ENV_ASSIGNMENT_RE = re.compile(r"\b([A-Z_]*(?:TOKEN|SECRET|PASSWORD|PASS|KEY|PRIVATE)[A-Z_]*)=([^\s'\"`]+)")
PEM_RE = re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----.*?-----END [A-Z ]*PRIVATE KEY-----", re.DOTALL)
AUTH_HEADER_RE = re.compile(r"\b(Authorization:\s*(?:Bearer|Basic)\s+)[A-Za-z0-9._~+/=-]+", re.IGNORECASE)
HIGH_ENTROPY_RE = re.compile(r"\b[A-Za-z0-9+/=_-]{40,}\b")


def scrub_text(value: str) -> str:
    value = URL_RE.sub(_scrub_url_match, value)
    value = PEM_RE.sub("<redacted-private-key>", value)
    value = AUTH_HEADER_RE.sub(r"\1<redacted>", value)
    value = ENV_ASSIGNMENT_RE.sub(r"\1=<redacted>", value)
    value = HIGH_ENTROPY_RE.sub("<redacted-secret>", value)
    return value


def scrub_bytes(value: bytes) -> bytes:
    if b"\x00" in value:
        return b"<redacted-binary>"
    try:
        text = value.decode("utf-8")
    except UnicodeDecodeError:
        return b"<redacted-binary>"
    return scrub_text(text).encode("utf-8")


def _scrub_url_match(match: re.Match[str]) -> str:
    raw = match.group(0)
    try:
        parts = urlsplit(raw)
    except ValueError:
        return "<redacted-url>"
    netloc = parts.netloc
    if "@" in netloc:
        netloc = netloc.rsplit("@", 1)[1]
    scrubbed = urlunsplit((parts.scheme, netloc, parts.path, "", ""))
    if parts.query:
        scrubbed += "?<redacted>"
    if parts.fragment:
        scrubbed += "#<redacted>"
    return scrubbed
