"""Operator-scoped HTTPS token selection for external build repositories.

The global config may carry a ``build_https`` object mapping canonical-identity
prefixes to an operator token selection:

    "build_https": {
      "gitlab.example.com/portals/infra": {"token": "git-credentials"},
      "gitlab.example.com": {"token": "keyring", "username": "oauth2"},
      "ci.example.com/group": {"token_env": "MY_CI_TOKEN"}
    }

Scopes use exactly the ``build_ssh`` grammar: segment prefixes of the
section 6.3 canonical repository identity (``host/path``), matched on whole
``/`` boundaries, longest match wins.  Only the operator writes this map;
package and repository data can never select a credential (Curator core
12.2), and the map never stores a secret:

- ``token: "git-credentials"`` reads the operator's existing Git HTTPS entry
  for the repository host through the operator's own credential helper;
- ``token: "keyring"`` reads the entry ``csk config build-https login`` stored
  through that same helper under a manager-namespaced username;
- ``token_env`` names an environment variable read at process entry.

Flags do not exist for this surface; the run-wide environment override
``CSK_BUILD_HTTPS_TOKEN`` (with optional ``CSK_BUILD_HTTPS_USERNAME``) keeps
precedence over every configured scope, mirroring the SSH surface.  Because
HTTPS basic auth transmits the token to whichever host a manifest names,
the optional ``CSK_BUILD_HTTPS_HOST`` pins the override to one host;
repositories on any other host resolve as if the override were absent.
"""

from __future__ import annotations

import os
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .build_ssh import default_scope, scope_matches, validate_scope

__all__ = [
    "BuildHTTPSError",
    "BuildHTTPSRule",
    "HostMaterial",
    "TOKEN_SOURCES",
    "default_scope",
    "delete_namespaced_token",
    "discover_host_material",
    "match",
    "namespace_username",
    "parse_rules",
    "read_host_credentials",
    "read_namespaced_token",
    "resolve_operator_home",
    "scope_host",
    "serialize_rules",
    "store_namespaced_token",
]


class BuildHTTPSError(ValueError):
    pass


TOKEN_SOURCES = ("git-credentials", "keyring")

_MAX_SCOPES = 256
_MAX_VALUE_LENGTH = 4096
_DEFAULT_USERNAME = "token"


@dataclass(frozen=True)
class BuildHTTPSRule:
    """One operator token selection for a canonical-identity scope."""

    scope: str
    token: str | None = None
    token_env: str | None = None
    username: str = _DEFAULT_USERNAME


def _string_or_none(value: Any, label: str) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str) or not value or len(value) > _MAX_VALUE_LENGTH:
        raise BuildHTTPSError(f"{label} must be a non-empty string when present")
    return value


def parse_rules(raw: Any, label: str = "build_https") -> tuple[BuildHTTPSRule, ...]:
    """Parse the config object into validated rules; fail closed on anything odd."""

    if raw is None:
        return ()
    if not isinstance(raw, dict):
        raise BuildHTTPSError(f"{label} must be an object of scope entries")
    if len(raw) > _MAX_SCOPES:
        raise BuildHTTPSError(f"{label} supports at most {_MAX_SCOPES} scopes")
    rules: list[BuildHTTPSRule] = []
    for scope, entry in raw.items():
        try:
            scope = validate_scope(scope, f"{label} scope")
        except ValueError as exc:
            raise BuildHTTPSError(str(exc)) from exc
        if not isinstance(entry, dict):
            raise BuildHTTPSError(f"{label}.{scope} must be an object")
        unknown = sorted(set(entry) - {"token", "token_env", "username"})
        if unknown:
            joined = ", ".join(repr(item) for item in unknown)
            raise BuildHTTPSError(f"{label}.{scope} has unsupported field(s): {joined}")
        token = _string_or_none(entry.get("token"), f"{label}.{scope}.token")
        token_env = _string_or_none(entry.get("token_env"), f"{label}.{scope}.token_env")
        username = _string_or_none(entry.get("username"), f"{label}.{scope}.username")
        if token is not None and token not in TOKEN_SOURCES:
            joined = ", ".join(TOKEN_SOURCES)
            raise BuildHTTPSError(
                f"{label}.{scope}.token must be one of {joined}; secrets never "
                "live in the config"
            )
        if (token is None) == (token_env is None):
            raise BuildHTTPSError(
                f"{label}.{scope} must select exactly one of 'token' or 'token_env'"
            )
        if token_env is not None and not token_env.isidentifier():
            raise BuildHTTPSError(
                f"{label}.{scope}.token_env must be an environment variable name"
            )
        rules.append(
            BuildHTTPSRule(
                scope=scope,
                token=token,
                token_env=token_env,
                username=username or _DEFAULT_USERNAME,
            )
        )
    return tuple(rules)


def serialize_rules(rules: tuple[BuildHTTPSRule, ...]) -> dict[str, dict[str, str]]:
    data: dict[str, dict[str, str]] = {}
    for rule in rules:
        entry: dict[str, str] = {}
        if rule.token is not None:
            entry["token"] = rule.token
        if rule.token_env is not None:
            entry["token_env"] = rule.token_env
        if rule.username != _DEFAULT_USERNAME:
            entry["username"] = rule.username
        data[rule.scope] = entry
    return data


def match(rules: tuple[BuildHTTPSRule, ...], canonical_identity: str) -> BuildHTTPSRule | None:
    """Longest segment-prefix match of a canonical ``host/path`` identity."""

    best: BuildHTTPSRule | None = None
    for rule in rules:
        if not scope_matches(rule.scope, canonical_identity):
            continue
        if best is None or len(rule.scope) > len(best.scope):
            best = rule
    return best


# --- operator credential access ---------------------------------------------
#
# Every read goes through the operator's own Git credential machinery
# (``git credential fill|approve|reject``).  That is the one mechanism which
# exists identically on macOS, Windows, and Linux, speaks to whichever helper
# the operator already configured — osxkeychain, wincred, libsecret, GCM — and
# needs no runtime dependency.  The helper is selected by the operator's Git
# configuration, never by a repository or a manifest, and the manager runs it
# outside the fetch: the broker itself only ever reads what the manager put in
# front of it.
#
# Presence probes and reads use the same call; a probe simply discards the
# secret it received.

NAMESPACE_PREFIX = "csk-build-https:"

_GIT_TIMEOUT = 15


def namespace_username(scope: str) -> str:
    """The username under which a manager-stored token lives for ``scope``.

    Storing under a distinct username keeps the manager entry separate from
    the operator's own credential for the same host, so neither overwrites
    the other.
    """

    return f"{NAMESPACE_PREFIX}{scope}"


def _credential_environment(home: str | None) -> dict[str, str]:
    environment = dict(os.environ)
    if home:
        # The fetch owns a private HOME, and a credential helper is configured
        # in the operator's Git configuration, so the operator home is pinned
        # by the manager and restored for this one call.
        environment["HOME"] = home
        environment["USERPROFILE"] = home
    environment["GIT_TERMINAL_PROMPT"] = "0"
    environment["GCM_INTERACTIVE"] = "never"
    return environment


def _run_credential(
    action: str,
    request: dict[str, str],
    git: str | None = None,
    home: str | None = None,
) -> dict[str, str] | None:
    payload = "".join(f"{key}={value}\n" for key, value in request.items()) + "\n"
    try:
        completed = subprocess.run(
            (git or "git", "credential", action),
            input=payload.encode("utf-8"),
            capture_output=True,
            timeout=_GIT_TIMEOUT,
            env=_credential_environment(home),
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if completed.returncode != 0:
        return None
    answer: dict[str, str] = {}
    for line in completed.stdout.decode("utf-8", "replace").splitlines():
        if "=" in line:
            key, _, value = line.partition("=")
            answer[key] = value
    return answer


def read_host_credentials(
    host: str,
    git: str | None = None,
    home: str | None = None,
) -> tuple[str, str] | None:
    """Read the operator's own HTTPS credential for ``host``.

    Returns (username, secret), or None when the operator's helpers hold
    nothing for the host.  Interactive prompting is disabled, so an absent
    credential degrades rather than blocking on a dialog.
    """

    answer = _run_credential("fill", {"protocol": "https", "host": host}, git, home)
    if not answer:
        return None
    password = answer.get("password")
    if not password:
        return None
    return (answer.get("username") or "token", password)


def read_namespaced_token(
    scope: str,
    host: str,
    git: str | None = None,
    home: str | None = None,
) -> str | None:
    """Read the token ``store_namespaced_token`` saved for ``scope``."""

    answer = _run_credential(
        "fill",
        {"protocol": "https", "host": host, "username": namespace_username(scope)},
        git,
        home,
    )
    if not answer:
        return None
    return answer.get("password") or None


def store_namespaced_token(
    scope: str,
    host: str,
    token: str,
    git: str | None = None,
    home: str | None = None,
) -> None:
    """Save a token through the operator's helper, and prove it was saved.

    ``git credential approve`` reports success even when the helper failed to
    persist anything — Git Credential Manager does exactly that when its
    Windows Credential Manager store is unreachable, which is the normal state
    in a session without an interactive desktop logon.  So the write is always
    verified by reading it back; a silent failure must not look like a
    configured scope that later fails mid-install.
    """

    answer = _run_credential(
        "approve",
        {
            "protocol": "https",
            "host": host,
            "username": namespace_username(scope),
            "password": token,
        },
        git,
        home,
    )
    if answer is None or read_namespaced_token(scope, host, git, home) != token:
        raise BuildHTTPSError(
            "your Git credential helper did not persist the token. Configure a "
            "working store — 'git config --global credential.helper osxkeychain' "
            "on macOS, 'libsecret' on Linux, and on Windows either use an "
            "interactive session or 'git config --global credential.credentialStore "
            "dpapi' — or select token_env instead"
        )


def delete_namespaced_token(
    scope: str,
    host: str,
    git: str | None = None,
    home: str | None = None,
) -> bool:
    answer = _run_credential(
        "reject",
        {"protocol": "https", "host": host, "username": namespace_username(scope)},
        git,
        home,
    )
    return answer is not None


def scope_host(scope: str) -> str:
    return scope.split("/", 1)[0]


@dataclass(frozen=True)
class HostMaterial:
    """Presence-only view of operator HTTPS material for one host."""

    host_credentials: bool = False
    host_username: str | None = None
    namespaced_scopes: tuple[str, ...] = field(default_factory=tuple)


def discover_host_material(
    host: str,
    scopes: tuple[str, ...] = (),
    git: str | None = None,
    home: str | None = None,
) -> HostMaterial:
    """List, without retaining, the operator material a host could use."""

    own = read_host_credentials(host, git, home)
    present = tuple(
        scope
        for scope in scopes
        if read_namespaced_token(scope, host, git, home) is not None
    )
    return HostMaterial(
        host_credentials=own is not None,
        host_username=own[0] if own else None,
        namespaced_scopes=present,
    )


def resolve_operator_home() -> str | None:
    """The operator home whose Git configuration selects the helper."""

    try:
        return os.fspath(Path.home())
    except (OSError, RuntimeError):
        return None
