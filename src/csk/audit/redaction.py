from __future__ import annotations

import re
from urllib.parse import urlsplit, urlunsplit


URL_RE = re.compile(r"https?://[A-Za-z0-9._~:/?#\[\]@!$&'()*+,;=%-]+")


def scrub_text(value: str) -> str:
    return URL_RE.sub(_scrub_url_match, value)


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
