from __future__ import annotations

import re
from urllib.parse import urlsplit, urlunsplit


URL_RE = re.compile(r"https?://[A-Za-z0-9._~:/?#\[\]@!$&'()*+,;=%-]+")
ENV_ASSIGNMENT_RE = re.compile(r"\b([A-Z_]++)=")
ENV_VALUE_RE = re.compile(r"[^\s'\"`]+")
PEM_RE = re.compile(r"-----(BEGIN|END) [A-Z ]*PRIVATE KEY-----")
AUTH_HEADER_RE = re.compile(r"\b(Authorization:\s*(?:Bearer|Basic)\s+)[A-Za-z0-9._~+/=-]+", re.IGNORECASE)
HIGH_ENTROPY_RE = re.compile(r"\b[A-Za-z0-9+/=_-]{40,}\b")


def scrub_text(value: str) -> str:
    value = URL_RE.sub(_scrub_url_match, value)
    value = _scrub_pem(value)
    value = AUTH_HEADER_RE.sub(r"\1<redacted>", value)
    value = _scrub_assignments(value)
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


def _scrub_assignments(value: str) -> str:
    # Name scanning and fixed-marker checks are linear. Only secret assignments
    # consume values: benign OUTER=TOKEN=secret must still redact the inner name.
    pieces: list[str] = []
    end = 0
    for match in ENV_ASSIGNMENT_RE.finditer(value):
        if match.start() < end:
            continue
        if not any(marker in match[1] for marker in ("TOKEN", "SECRET", "PASSWORD", "PASS", "KEY", "PRIVATE")):
            continue
        secret = ENV_VALUE_RE.match(value, match.end())
        if secret is None:
            continue
        pieces.append(value[end:match.end()])
        pieces.append("<redacted>")
        end = secret.end()
    pieces.append(value[end:])
    return "".join(pieces)


def _scrub_pem(value: str) -> str:
    # Pair the first BEGIN with the next END in one pass, including nested
    # BEGIN markers. Repeated unterminated blocks must not rescan the suffix.
    pieces: list[str] = []
    start: int | None = None
    end = 0
    for match in PEM_RE.finditer(value):
        if match[1] == "BEGIN" and start is None:
            start = match.start()
        elif match[1] == "END" and start is not None:
            pieces.extend((value[end:start], "<redacted-private-key>"))
            end = match.end()
            start = None
    pieces.append(value[end:])
    return "".join(pieces)
