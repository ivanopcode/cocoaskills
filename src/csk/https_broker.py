"""The manager credential broker for private HTTPS build-repository fetches.

Git invokes ``GIT_ASKPASS`` twice per authenticated fetch — once for the
username, once for the password — passing one prompt argument.  The manager
writes a private wrapper next to its private SSH wrapper that execs this
module with a pinned state-file path; the wrapper, the state file, and the
fetch environment are all manager-owned, so a repository value can never
select what the broker answers or where the answer goes (the fetch itself is
pinned to one TLS-verified host with redirects disabled).

The state file carries the selection, never a secret:

    {"scope": ..., "host": ..., "source": "keyring"|"git-credentials"|"env",
     "username": ..., "tool": "<absolute secret-store tool>"}

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
    scope = state.get("scope")
    pinned_host = state.get("host")
    source = state.get("source")
    username = state.get("username")
    tool = state.get("tool")
    store = state.get("store")
    if (tool is not None and not isinstance(tool, str)) or (
        store is not None and not isinstance(store, str)
    ):
        return None
    if not all(isinstance(v, str) and v for v in (scope, pinned_host, source, username)):
        return None

    matched = _PROMPT_RE.match(prompt.strip())
    if matched is None:
        return None
    host = _host_of(matched.group("url"))
    if host is None or host != pinned_host:
        return None

    if matched.group("kind") == "Username":
        return username

    if source == "env":
        token = environ.get(TOKEN_ENV)
        return token or None
    if source == "keyring":
        from . import build_https

        return build_https.read_keyring_token(scope, tool, store)
    if source == "git-credentials":
        from . import build_https

        material = build_https.read_git_credentials(pinned_host, tool, store)
        if material is None:
            return None
        return material[1]
    return None


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
