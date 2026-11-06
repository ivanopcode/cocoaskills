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
import os
import subprocess
import sys
from pathlib import Path
from dataclasses import dataclass, replace
from typing import Any

from .tool_paths import resolve_tool


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


def scope_matches(scope: str, identity: str) -> bool:
    """True when ``scope`` covers ``identity`` on whole ``/`` boundaries."""

    if identity == scope:
        return True
    return identity.startswith(scope + "/")


_scope_matches = scope_matches


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
                (resolve_tool("ssh-add", search_path=env.get("PATH")), "-l"),
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
        private = _private_identity_candidate(pub)
        if private is None:
            continue
        commands.append(f"csk config build-ssh add {scope} --identity {private}")
        if len(commands) >= limit + 1:
            break
    return commands[: limit + 1]


def _private_identity_candidate(public_key: str) -> str | None:
    """List only existing private siblings; public-only agent pins stay valid."""
    private = public_key[:-4]
    try:
        return private if Path(private).is_file() else None
    except OSError:
        return None


@dataclass(frozen=True)
class KeyInspection:
    """Local evidence only; unknown never establishes an unusable credential."""

    state: str = "unknown"
    reason: str = "local checks were inconclusive"
    key_kind: str = "unknown"
    passphrase: bool | None = None
    loaded: bool | None = None
    provider: str = "unknown"


def _local_probe(
    argv: tuple[str, ...], *, agent: Path | None = None, data: str | None = None,
) -> subprocess.CompletedProcess[str] | None:
    # No inherited askpass, provider, config or agent selection. These commands
    # inspect metadata and public identities; none requests a signature.
    environment = {"LC_ALL": "C"}
    if os.name == "nt" and os.environ.get("SystemRoot"):
        environment["SystemRoot"] = os.environ["SystemRoot"]
    if agent is not None:
        environment["SSH_AUTH_SOCK"] = str(agent)
    try:
        executable = resolve_tool(Path(argv[0]).name, executable=argv[0])
        result = subprocess.run(
            (executable, *argv[1:]), input=data.encode("utf-8") if data is not None else None, stdin=subprocess.DEVNULL if data is None else None,
            capture_output=True, timeout=5, env=environment,
        )
        return subprocess.CompletedProcess(
            result.args, result.returncode, result.stdout.decode("utf-8"), result.stderr.decode("utf-8"),
        )
    except (OSError, subprocess.SubprocessError, UnicodeError):
        return None


def _key_fingerprints(output: str) -> list[str] | None:
    """Accept complete OpenSSH SHA256 fingerprint records, never partial rows."""
    rows = output.removesuffix("\n").split("\n")
    pattern = r"[1-9][0-9]* (SHA256:[A-Za-z0-9+/]{43}) [^\r\n]* \((?:RSA|DSA|ECDSA|ED25519|ECDSA-SK|ED25519-SK)\)"
    matches = [re.fullmatch(pattern, row) for row in rows]
    return [match[1] for match in matches if match] if all(matches) else None


def inspect_key(
    ssh: Path, identity: Path | None, agent: Path | None = None,
) -> KeyInspection:
    """Inspect with siblings of the admitted ssh; never open private material.

    This describes the pinned non-prompting lane, not eventual server acceptance.
    Missing tools, unfamiliar output and unavailable agents remain unknown.
    """
    if identity is None:
        return KeyInspection(reason="no identity pin; agent signing ability is unknown")
    suffix = ".exe" if ssh.suffix.lower() == ".exe" else ""
    keygen = str(ssh.parent / ("ssh-keygen" + suffix))
    add = str(ssh.parent / ("ssh-add" + suffix))
    info = _local_probe((keygen, "-l", "-f", str(identity)))
    admitted = _key_fingerprints(info.stdout) if info is not None and info.returncode == 0 and not info.stderr else None
    if admitted is None or len(admitted) != 1:
        return KeyInspection()
    assert info is not None
    fingerprint = admitted[0]
    kind = info.stdout.removesuffix("\n").rsplit("(", 1)[-1][:-1]
    loaded = None
    if agent is not None:
        listing = _local_probe((add, "-L"), agent=agent)
        if listing is not None and listing.returncode == 1 and listing.stdout == "The agent has no identities.\n" and not listing.stderr:
            loaded = False
        elif listing is not None and listing.returncode == 0 and not listing.stderr:
            fingerprints = _local_probe((keygen, "-l", "-f", "-"), data=listing.stdout)
            records = _key_fingerprints(fingerprints.stdout) if fingerprints is not None and fingerprints.returncode == 0 and not fingerprints.stderr else None
            if records is not None and len(records) == len(listing.stdout.removesuffix("\n").split("\n")):
                loaded = fingerprint in records
    provider = "unknown"
    security_key = "-SK" in kind.upper()
    if security_key:
        settings = _local_probe((str(ssh), "-G", "-F", os.devnull, "example.invalid"))
        # No token/provider state is inferred from normalized values or socket names.
        # Only the exact absent-provider record is affirmative refusal evidence.
        records = [line for line in settings.stdout.split("\n") if "securitykeyprovider" in line.lower()] if settings is not None else []
        if settings is not None and settings.returncode == 0 and not settings.stderr:
            rows = settings.stdout.removesuffix("\n").split("\n")
            canonical = all(re.fullmatch(r"[a-z][a-z0-9]* [^\x00-\x1f\x7f]+", row) for row in rows) and not any(
                char.isspace() and char not in " \n" for char in settings.stdout
            )
            if canonical and records == ["securitykeyprovider none"]:
                return KeyInspection(state="unusable", reason="SSH has no configured security-key provider in this lane", key_kind=kind, loaded=loaded, provider="none")
            if canonical and records == ["securitykeyprovider internal"]:
                provider = "internal"
        reason = "local metadata does not establish that the key is unusable" if provider == "internal" else "SSH security-key provider output is unfamiliar"
        return KeyInspection(reason=reason, key_kind=kind, loaded=loaded, provider=provider)
    private = identity
    decrypted = _local_probe((keygen, "-y", "-P", "", "-f", str(private)))
    passphrase = None
    bad_permissions = False
    invalid_public = False
    if decrypted is not None:
        error = f'Load key "{private}": '
        passphrase_error = error + "incorrect passphrase supplied to decrypt private key\n"
        permission_error = (
            "@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@\n"
            "@         WARNING: UNPROTECTED PRIVATE KEY FILE!          @\n"
            "@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@\n"
            f"Permissions MODE for '{private}' are too open.\n"
            "It is required that your private key files are NOT accessible by others.\n"
            "This private key will be ignored.\n" + error + "bad permissions\n"
        )
        failed = decrypted.returncode == 255 and not decrypted.stdout
        bad_permissions = failed and any(
            re.fullmatch(re.escape(form).replace("MODE", r"0[0-7]{3,4}"), decrypted.stderr)
            for form in (permission_error, permission_error.replace("\n", "\r\n"))
        )
        if decrypted.returncode == 0:
            passphrase = False
        elif failed and decrypted.stderr in {passphrase_error, passphrase_error.replace("\n", "\r\n")}:
            passphrase = True
        invalid_public = failed and decrypted.stderr in {
            error + suffix + ending for suffix in ("invalid format", "error in libcrypto") for ending in ("\n", "\r\n")
        }
    facts = KeyInspection(key_kind=kind, passphrase=passphrase, loaded=loaded, provider=provider)
    if loaded:
        return replace(facts, state="unknown", reason="key is listed in the agent; signing ability is unverified")
    if agent is not None and loaded is None:
        return replace(facts, reason="agent availability or key membership is unknown")
    if str(identity).endswith(".pub") and (invalid_public or bad_permissions):
        return replace(facts, state="unusable", reason="a public key alone cannot sign and no matching agent key is available")
    if bad_permissions:
        return replace(facts, state="unusable", reason="private key permissions are too open; SSH will ignore this file")
    if passphrase:
        return replace(facts, state="unusable", reason="key has a passphrase and no matching agent key is available; SSH prompts are disabled")
    return replace(facts, state="unknown", reason="local metadata does not establish that the key is unusable")


def doctor(
    rules: tuple[BuildSSHRule, ...], scope: str | None = None, *,
    identity: str | None = None, agent: str | None = None, all_os: bool = False,
) -> str:
    """Render local observations and operator instructions, without mutation."""
    rule = match(rules, scope) if scope else None
    identity = identity or os.environ.get("CSK_BUILD_SSH_IDENTITY") or (rule.identity if rule else None)
    agent = agent or os.environ.get("CSK_BUILD_SSH_AGENT") or (rule.agent if rule else None)
    socket = os.environ.get("SSH_AUTH_SOCK") if agent == "auto" else agent
    lines = [
        "Build SSH doctor (read-only; no network, prompts, signatures or agent changes)",
        f"Repository/scope: {scope or '(none selected)'}",
        f"OS: {sys.platform}",
        f"Identity: {identity or '(none selected)'}",
        f"Agent: {socket or '(none selected or auto unavailable)'}",
    ]
    try:
        ssh = Path(resolve_tool("ssh"))
    except (OSError, ValueError):
        lines.append("ssh: unavailable; local state unknown")
    else:
        version = _local_probe((str(ssh), "-V"))
        lines.append(f"ssh: {ssh} ({version.stderr.strip() if version else 'version unknown'})")
        if identity:
            facts = inspect_key(ssh, Path(identity).expanduser(), Path(socket).expanduser() if socket else None)
            lines.extend([
                f"Key: {facts.key_kind}; passphrase: {facts.passphrase}; in agent: {facts.loaded}",
                f"Security-key provider: {facts.provider}",
                f"Local state: {facts.state}; {facts.reason}",
            ])
    lines.extend([
        "SSH prompts remain disabled in csk; a passphrase key needs a matching capable agent.",
        "The agent and its keys are managed by the person who owns the computer; csk does not manage them.",
        "Choose a lifetime that fits the task and is no longer than the task needs.",
        "Controlling that lifetime is a separate security matter for the operator.",
        "Examples use 1h; replace it with your chosen task lifetime.",
        "Prefer --identity <private-key> together with --agent auto: the private path works with the key loaded or absent (a plain key can sign directly).",
        "Plain key: csk config build-ssh add <scope> --identity <private-key>",
        "Passphrase key: load into your capable agent using the steps below, then select --agent auto --identity <private-key>.",
        "Custom agent, e.g. 1Password (unverified): unlock/configure it in its own UI, then:",
        "  csk config build-ssh add <scope> --agent <socket> --identity <key>.pub",
        "A .pub only pins an agent key; it cannot sign by itself. Custom-agent support for FIDO/PIN is unverified.",
    ])
    if all_os or sys.platform == "darwin":
        lines.extend([
            "macOS:",
            "  Plain/passphrase key, operator-managed agent: ssh-add -t 1h <private-key>",
            "  Optional Keychain storage: ssh-add --apple-use-keychain -t 1h <private-key>",
            "  FIDO without PIN (unverified on a real token): use Homebrew OpenSSH and its agent:",
            "    brew install openssh",
            '    eval "$("$(brew --prefix openssh)/bin/ssh-agent" -s)"',
            '    "$(brew --prefix openssh)/bin/ssh-add" -t 1h <private-key>',
            "  Ensure csk resolves Homebrew ssh, not Apple ssh; Apple SSH needs a provider that this lane does not configure.",
            "  The macOS system agent cannot sign with USB keys; adding the key there masks the file route.",
            "  Undo that masking yourself: ssh-add -d <key>.pub",
            "  FIDO with PIN (unverified on a real token): agent signing needs askpass/display configured by its owner; a terminal alone does not help.",
            "  File PIN entry needs a terminal and is unsuitable for this non-prompting lane; use an alternative credential if needed.",
        ])
    if all_os or sys.platform.startswith("linux"):
        lines.extend([
            "Linux (routes unverified here):",
            '  Plain/passphrase key: eval "$(ssh-agent -s)"',
            "  ssh-add -t 1h <private-key>",
            "  FIDO without PIN (unverified on a real token): use an OpenSSH build with built-in FIDO and its capable agent; same lifetime command, touch per connection.",
            "  FIDO with PIN (unverified on a real token): the agent needs owner-configured askpass/display; otherwise choose another credential for non-prompting runs.",
            "  Desktop/custom agents may differ. Linux external builds are not currently admitted; source SSH uses the shared lane.",
        ])
    if all_os or sys.platform == "win32":
        lines.extend([
            "Windows (all routes unverified; current csk agent-pipe/wrapper support is incomplete):",
            "  Plain key: select the private file; verify your OpenSSH build separately.",
            "  Passphrase key, operator setup in elevated PowerShell:",
            "    Get-Service ssh-agent | Set-Service -StartupType Manual",
            "    Start-Service ssh-agent",
            "  Then unelevated: ssh-add -t 1h <private-key>",
            "  Verify your agent supports the requested lifetime before relying on it.",
            "  csk cannot currently select the Windows service pipe through --agent; these are preparation steps, not a working csk agent route.",
            "  FIDO without PIN: OpenSSH/token support and console integration need real-token verification.",
            "  FIDO with PIN: PIN/dialog and agent behavior need real-token verification; do not assume success.",
            "  Resident-key ssh-add -K is unsupported in documented Win32-OpenSSH versions.",
        ])
    lines.append("Hardware routes are unverified on a real token; software-provider evidence does not establish token compatibility.")
    return "\n".join(lines)
