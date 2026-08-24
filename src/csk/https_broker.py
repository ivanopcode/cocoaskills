"""The manager credential broker for private HTTPS build-repository fetches.

Git invokes ``GIT_ASKPASS`` twice per authenticated fetch — once for the
username, once for the password — passing one prompt argument.  The manager
writes a private wrapper next to its private SSH wrapper that execs this
module with a pinned state-file path; the wrapper, the state file, and the
fetch environment are all manager-owned, so a repository value can never
select what the broker answers or where the answer goes (the fetch itself is
pinned to one TLS-verified host with redirects disabled).

The state file carries the selection, never a secret:

    {"host": ..., "username": ...}

For the token itself the manager puts the operator-selected secret in the
fetch environment under ``CSK_HTTPS_BROKER_TOKEN``: reading the operator's
credential helper is the manager's job, done once before the fetch starts and
outside its process graph, so the broker stays a pure, platform-independent
answer function.

For the ``env`` source the manager copies the operator token, captured at
process entry, into the fetch environment under ``CSK_HTTPS_BROKER_TOKEN``;
the other sources are read from the operator's OS secret store at answer
time.  Every mismatch — an unparseable prompt, a host other than the pinned
one, absent material — exits without printing a byte, so the fetch fails
closed with Git's own authentication error.
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path

TOKEN_ENV = "CSK_HTTPS_BROKER_TOKEN"

_PROMPT_RE = re.compile(
    r"^(?P<kind>Username|Password) for '(?P<url>https://[^']+)'"
)


def _host_of(url: str) -> str | None:
    rest = url[len("https://") :]
    rest = rest.split("/", 1)[0]
    if "@" in rest:
        rest = rest.rsplit("@", 1)[1]
    host = rest.split(":", 1)[0].lower()
    return host or None


def answer(state_path: str, prompt: str, environ: dict[str, str]) -> str | None:
    """Return the line to print for ``prompt``, or None to fail closed."""

    try:
        raw = Path(state_path).read_bytes()
        state = json.loads(raw)
    except (OSError, ValueError):
        return None
    if not isinstance(state, dict):
        return None
    pinned_host = state.get("host")
    username = state.get("username")
    if not all(isinstance(v, str) and v for v in (pinned_host, username)):
        return None

    matched = _PROMPT_RE.match(prompt.strip())
    if matched is None:
        return None
    host = _host_of(matched.group("url"))
    if host is None or host != pinned_host:
        return None

    if matched.group("kind") == "Username":
        return username
    return environ.get(TOKEN_ENV) or None


def main(argv: list[str] | None = None) -> int:
    import os

    args = sys.argv[1:] if argv is None else argv
    if len(args) != 2:
        return 1
    line = answer(args[0], args[1], dict(os.environ))
    if line is None:
        return 1
    sys.stdout.write(line + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
