"""Operator-scoped SSH credential selection for external build repositories.

The global config may carry a ``build_ssh`` object mapping canonical-identity
prefixes to an operator credential selection:

    "build_ssh": {
      "gitlab.example.com": {"identity": "~/.ssh/personal"},
      "gitlab.example.com/portals/infra": {
        "agent": "auto",
        "identity": "~/.ssh/work.pub"
      }
    }

A scope is a segment prefix of the section 6.3 canonical repository identity
(``host/path``): the host is matched lowercase, path segments are matched
case-sensitively, and matching happens only on whole ``/`` boundaries, so
``portals`` never matches ``portals-evil``.  The longest matching scope wins.

Only the operator writes this map, and only into the operator-owned global
config; package and repository data can never select a credential (Curator
core 12.2).  Command-line flags and ``CSK_BUILD_SSH_*`` environment values
keep precedence over every configured scope.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any


class BuildSSHError(ValueError):
    pass


_HOST_RE = re.compile(r"^[a-z0-9](?:[a-z0-9.-]{0,253}[a-z0-9])?$")
_SEGMENT_RE = re.compile(r"^[A-Za-z0-9._-]{1,255}$")
_MAX_SCOPES = 256
_MAX_VALUE_LENGTH = 4096


@dataclass(frozen=True)
class BuildSSHRule:
    """One operator credential selection for a canonical-identity scope."""

    scope: str
    agent: str | None = None
    identity: str | None = None
    known_hosts: str | None = None


def validate_scope(scope: str, label: str = "build_ssh scope") -> str:
    if not isinstance(scope, str) or not scope or len(scope) > _MAX_VALUE_LENGTH:
        raise BuildSSHError(f"{label} must be a non-empty string")
    if scope != scope.strip():
        raise BuildSSHError(f"{label} {scope!r} must not carry surrounding whitespace")
    segments = scope.split("/")
    host = segments[0]
    if not _HOST_RE.fullmatch(host) or ".." in host:
        raise BuildSSHError(
            f"{label} {scope!r} must start with a lowercase host name"
        )
    for segment in segments[1:]:
        if segment in {".", ".."} or not _SEGMENT_RE.fullmatch(segment):
            raise BuildSSHError(
                f"{label} {scope!r} has an invalid path segment {segment!r}"
            )
    return scope


def _string_or_none(value: Any, label: str) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str) or not value or len(value) > _MAX_VALUE_LENGTH:
        raise BuildSSHError(f"{label} must be a non-empty string when present")
    return value


def parse_rules(raw: Any, label: str = "build_ssh") -> tuple[BuildSSHRule, ...]:
    """Parse the config object into validated rules; fail closed on anything odd."""

    if raw is None:
        return ()
    if not isinstance(raw, dict):
        raise BuildSSHError(f"{label} must be an object of scope entries")
    if len(raw) > _MAX_SCOPES:
        raise BuildSSHError(f"{label} supports at most {_MAX_SCOPES} scopes")
    rules: list[BuildSSHRule] = []
    for scope, entry in raw.items():
        scope = validate_scope(scope, f"{label} scope")
        if not isinstance(entry, dict):
            raise BuildSSHError(f"{label}.{scope} must be an object")
        unknown = sorted(set(entry) - {"agent", "identity", "known_hosts"})
        if unknown:
            joined = ", ".join(repr(item) for item in unknown)
            raise BuildSSHError(f"{label}.{scope} has unsupported field(s): {joined}")
        agent = _string_or_none(entry.get("agent"), f"{label}.{scope}.agent")
        identity = _string_or_none(entry.get("identity"), f"{label}.{scope}.identity")
        known_hosts = _string_or_none(
            entry.get("known_hosts"), f"{label}.{scope}.known_hosts"
        )
        if agent is None and identity is None:
            raise BuildSSHError(
                f"{label}.{scope} must select at least one of 'agent' or 'identity'"
            )
        rules.append(
            BuildSSHRule(
                scope=scope, agent=agent, identity=identity, known_hosts=known_hosts
            )
        )
    return tuple(rules)


def serialize_rules(rules: tuple[BuildSSHRule, ...]) -> dict[str, dict[str, str]]:
    data: dict[str, dict[str, str]] = {}
    for rule in rules:
        entry: dict[str, str] = {}
        if rule.agent is not None:
            entry["agent"] = rule.agent
        if rule.identity is not None:
            entry["identity"] = rule.identity
        if rule.known_hosts is not None:
            entry["known_hosts"] = rule.known_hosts
        data[rule.scope] = entry
    return data


def _scope_matches(scope: str, identity: str) -> bool:
    if identity == scope:
        return True
    return identity.startswith(scope + "/")


def match(rules: tuple[BuildSSHRule, ...], canonical_identity: str) -> BuildSSHRule | None:
    """Longest segment-prefix match of a canonical ``host/path`` identity."""

    best: BuildSSHRule | None = None
    for rule in rules:
        if not _scope_matches(rule.scope, canonical_identity):
            continue
        if best is None or len(rule.scope) > len(best.scope):
            best = rule
    return best


def default_scope(canonical_identity: str) -> str:
    """The narrowest scope worth persisting: the repository's namespace."""

    segments = canonical_identity.split("/")
    if len(segments) <= 2:
        return canonical_identity
    return "/".join(segments[:-1])


@dataclass(frozen=True)
class DiscoveredCandidates:
    """Operator credential material visible on this machine.

    Discovery only ever *lists* what exists — an agent socket and public key
    files — so the operator can pick with one keystroke.  Nothing here is used
    without an explicit selection; silent use of everything below ``~/.ssh``
    would hand every operator key to whatever host a skill manifest names.
    """

    agent_socket: str | None = None
    agent_key_count: int | None = None
    public_keys: tuple[str, ...] = ()


def discover_candidates(
    environment: "dict[str, str] | None" = None,
    home: "str | None" = None,
) -> DiscoveredCandidates:
    import os
    import subprocess
    from pathlib import Path

    env = dict(environment) if environment is not None else dict(os.environ)
    agent_socket = env.get("SSH_AUTH_SOCK") or None
    agent_key_count: int | None = None
    if agent_socket:
        try:
            probe = subprocess.run(
                ("ssh-add", "-l"),
                env={"SSH_AUTH_SOCK": agent_socket, "PATH": env.get("PATH", "")},
                capture_output=True,
                text=True,
                timeout=5,
            )
            if probe.returncode == 0:
                agent_key_count = len(
                    [line for line in probe.stdout.splitlines() if line.strip()]
                )
            elif probe.returncode == 1:
                agent_key_count = 0
            else:
                agent_socket = None
        except (OSError, subprocess.SubprocessError):
            # ssh-add being unavailable only degrades the listing, never the
            # selection surfaces themselves.
            agent_key_count = None
    ssh_dir = Path(home).expanduser() / ".ssh" if home else Path.home() / ".ssh"
    public_keys: list[str] = []
    try:
        public_keys = sorted(
            str(path)
            for path in ssh_dir.glob("*.pub")
            if path.is_file()
        )
    except OSError:
        public_keys = []
    return DiscoveredCandidates(
        agent_socket=agent_socket,
        agent_key_count=agent_key_count,
        public_keys=tuple(public_keys),
    )


def candidate_commands(
    scope: str,
    candidates: DiscoveredCandidates,
    limit: int = 3,
) -> list[str]:
    """Ready-to-run ``csk config build-ssh add`` lines for a missing scope."""

    commands: list[str] = []
    pubs = candidates.public_keys[:limit]
    if candidates.agent_socket is not None and pubs:
        commands.append(
            f"csk config build-ssh add {scope} --agent auto --identity {pubs[0]}"
        )
    if candidates.agent_socket is not None:
        commands.append(f"csk config build-ssh add {scope} --agent auto")
    for pub in pubs:
        private = pub[:-4]
        commands.append(f"csk config build-ssh add {scope} --identity {private}")
        if len(commands) >= limit + 1:
            break
    return commands[: limit + 1]
