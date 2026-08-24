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
  for the repository host from the OS secret store (the material the
  ``osxkeychain``/``wincred``/``libsecret`` helpers maintain);
- ``token: "keyring"`` reads the entry ``csk config build-https login`` stored
  under the manager's own service name, keyed by the scope;
- ``token_env`` names an environment variable read at process entry.

Flags do not exist for this surface; the run-wide environment override
``CSK_BUILD_HTTPS_TOKEN`` (with optional ``CSK_BUILD_HTTPS_USERNAME``) keeps
precedence over every configured scope, mirroring the SSH surface.
"""

from __future__ import annotations

import os
import subprocess
import sys
from dataclasses import dataclass, field
from typing import Any

from .build_ssh import default_scope, scope_matches, validate_scope

__all__ = [
    "BuildHTTPSError",
    "BuildHTTPSRule",
    "KEYRING_SERVICE",
    "TOKEN_SOURCES",
    "default_scope",
    "delete_keyring_token",
    "discover_host_material",
    "match",
    "parse_rules",
    "probe_keyring_token",
    "read_keyring_token",
    "resolve_secret_store",
    "resolve_secret_tool",
    "serialize_rules",
    "store_keyring_token",
]


class BuildHTTPSError(ValueError):
    pass


# The manager-owned secret-store service name. Entries are keyed by scope so
# two accounts on one host stay distinct selections.
KEYRING_SERVICE = "csk-build-https"

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


# --- operator secret-store access -------------------------------------------
#
# Presence probes never read the secret; the broker reads it once, inside the
# fetch operation, after the operator's explicit scope selection.  Everything
# shells out to the platform's own tool so the manager keeps zero runtime
# dependencies; an unsupported platform degrades to "absent" and the caller's
# fail-closed message names the working alternatives.


def resolve_secret_tool() -> str | None:
    """Resolve the platform secret-store tool to an absolute path.

    Resolved by the manager, at manager PATH, and pinned into the broker
    state: the fetch environment deliberately carries an empty PATH, so the
    broker must never look a tool up itself.
    """

    import shutil

    if sys.platform == "darwin":
        candidates = ("security",)
        fallbacks = ("/usr/bin/security",)
    elif sys.platform.startswith("linux"):
        candidates = ("secret-tool",)
        fallbacks = ("/usr/bin/secret-tool", "/bin/secret-tool")
    else:
        return None
    for name in candidates:
        found = shutil.which(name)
        if found:
            return os.path.realpath(found)
    for path in fallbacks:
        if os.path.isfile(path) and os.access(path, os.X_OK):
            return path
    return None


def _tool_argv(tool: str | None, default: str) -> str:
    return tool or default


def resolve_secret_store() -> str | None:
    """Resolve the operator's default secret store to an absolute path.

    macOS resolves the login keychain relative to ``HOME``, and the fetch
    environment deliberately owns a private ``HOME`` — so the store, like the
    tool, is resolved by the manager and pinned into the broker state.
    """

    if sys.platform != "darwin":
        return None
    tool = resolve_secret_tool()
    probe = _run_quiet((_tool_argv(tool, "security"), "list-keychains", "-d", "user"))
    if probe is None or probe.returncode != 0:
        return None
    for line in probe.stdout.decode("utf-8", "replace").splitlines():
        candidate = line.strip().strip('"')
        if candidate.endswith("login.keychain-db") or candidate.endswith("login.keychain"):
            return candidate
    return None


def _store_argv(store: str | None) -> tuple[str, ...]:
    return (store,) if store else ()


def _run_quiet(argv: tuple[str, ...]) -> subprocess.CompletedProcess[bytes] | None:
    try:
        return subprocess.run(
            argv,
            capture_output=True,
            timeout=10,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None


@dataclass(frozen=True)
class HostMaterial:
    """Presence-only view of operator HTTPS material for one host."""

    git_credentials: bool = False
    git_username: str | None = None
    keyring_scopes: tuple[str, ...] = field(default_factory=tuple)


def discover_host_material(
    host: str,
    scopes: tuple[str, ...] = (),
    tool: str | None = None,
    store: str | None = None,
) -> HostMaterial:
    """List, without reading, the operator material a host could use."""

    git_credentials = False
    git_username: str | None = None
    if sys.platform == "darwin":
        probe = _run_quiet(
            (
                _tool_argv(tool, "security"),
                "find-internet-password",
                "-s",
                host,
                "-r",
                "htps",
            )
            + _store_argv(store)
        )
        if probe is not None and probe.returncode == 0:
            git_credentials = True
            for line in probe.stdout.decode("utf-8", "replace").splitlines():
                line = line.strip()
                if line.startswith('"acct"') and "=" in line:
                    value = line.split("=", 1)[1].strip()
                    if value.startswith("<blob>"):
                        value = value[len("<blob>") :]
                    git_username = value.strip('"') or None
                    break
    present_scopes = tuple(
        scope for scope in scopes if probe_keyring_token(scope, tool, store)
    )
    return HostMaterial(
        git_credentials=git_credentials,
        git_username=git_username,
        keyring_scopes=present_scopes,
    )


def probe_keyring_token(
    scope: str, tool: str | None = None, store: str | None = None
) -> bool:
    """True when the manager keyring entry for ``scope`` exists (no read)."""

    if sys.platform == "darwin":
        probe = _run_quiet(
            (
                _tool_argv(tool, "security"),
                "find-generic-password",
                "-s",
                KEYRING_SERVICE,
                "-a",
                scope,
            )
            + _store_argv(store)
        )
        return probe is not None and probe.returncode == 0
    if sys.platform.startswith("linux"):
        probe = _run_quiet(
            (
                _tool_argv(tool, "secret-tool"),
                "search",
                "service",
                KEYRING_SERVICE,
                "scope",
                scope,
            )
        )
        return probe is not None and probe.returncode == 0 and bool(probe.stdout.strip())
    return False


def store_keyring_token(scope: str, token: str) -> None:
    """Store one manager keyring entry; the secret travels via stdin only."""

    if sys.platform == "darwin":
        completed = _run_quiet(
            (
                "security",
                "add-generic-password",
                "-U",
                "-s",
                KEYRING_SERVICE,
                "-a",
                scope,
                "-w",
                token,
            )
        )
        # ``security`` has no stdin mode for -w; the argument is visible to
        # `ps` for the call's duration.  Documented; the login flow warns.
        if completed is None or completed.returncode != 0:
            raise BuildHTTPSError("the macOS keychain refused to store the token")
        return
    if sys.platform.startswith("linux"):
        try:
            completed = subprocess.run(
                (
                    "secret-tool",
                    "store",
                    "--label",
                    f"{KEYRING_SERVICE} {scope}",
                    "service",
                    KEYRING_SERVICE,
                    "scope",
                    scope,
                ),
                input=token.encode("utf-8"),
                capture_output=True,
                timeout=10,
                check=False,
            )
        except (OSError, subprocess.SubprocessError) as exc:
            raise BuildHTTPSError("secret-tool is unavailable") from exc
        if completed.returncode != 0:
            raise BuildHTTPSError("the Secret Service refused to store the token")
        return
    raise BuildHTTPSError(
        "manager keyring storage is not supported on this platform; use "
        "token=git-credentials or token_env instead"
    )


def read_keyring_token(
    scope: str, tool: str | None = None, store: str | None = None
) -> str | None:
    """Read the manager keyring entry for ``scope``; None when absent."""

    if sys.platform == "darwin":
        probe = _run_quiet(
            (
                _tool_argv(tool, "security"),
                "find-generic-password",
                "-w",
                "-s",
                KEYRING_SERVICE,
                "-a",
                scope,
            )
            + _store_argv(store)
        )
        if probe is None or probe.returncode != 0:
            return None
        return probe.stdout.decode("utf-8", "replace").rstrip("\n") or None
    if sys.platform.startswith("linux"):
        probe = _run_quiet(
            (
                _tool_argv(tool, "secret-tool"),
                "lookup",
                "service",
                KEYRING_SERVICE,
                "scope",
                scope,
            )
        )
        if probe is None or probe.returncode != 0:
            return None
        return probe.stdout.decode("utf-8", "replace").rstrip("\n") or None
    return None


def delete_keyring_token(scope: str) -> bool:
    if sys.platform == "darwin":
        probe = _run_quiet(
            ("security", "delete-generic-password", "-s", KEYRING_SERVICE, "-a", scope)
        )
        return probe is not None and probe.returncode == 0
    if sys.platform.startswith("linux"):
        probe = _run_quiet(
            ("secret-tool", "clear", "service", KEYRING_SERVICE, "scope", scope)
        )
        return probe is not None and probe.returncode == 0
    return False


def read_git_credentials(
    host: str, tool: str | None = None, store: str | None = None
) -> tuple[str, str] | None:
    """Read the operator's Git HTTPS entry for ``host``: (username, secret).

    This reads the same OS store the Git credential helpers maintain — the
    operator's own material — directly, never by running a configured helper.
    """

    if sys.platform == "darwin":
        secret = _run_quiet(
            (
                _tool_argv(tool, "security"),
                "find-internet-password",
                "-w",
                "-s",
                host,
                "-r",
                "htps",
            )
            + _store_argv(store)
        )
        if secret is None or secret.returncode != 0:
            return None
        material = discover_host_material(host, tool=tool, store=store)
        token = secret.stdout.decode("utf-8", "replace").rstrip("\n")
        if not token:
            return None
        return (material.git_username or "oauth2", token)
    return None
