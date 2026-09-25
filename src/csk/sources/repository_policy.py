"""Operator-owned repository endpoint policy and pure transport planning.

The policy is machine-owned input.  This module validates the complete JSON
document, including every endpoint property, before returning a resolution
plan to a transport implementation.  It deliberately does not resolve
credentials or perform network I/O: authentication values are opaque names
for an operator-owned broker.

The portable repository identity is always ``host/path``.  Endpoint ports,
mirrors, aliases and their sanitized provenance stay in connection objects
and never become part of that identity.
"""

from __future__ import annotations

import re
import stat
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Final, Literal

from .. import identifiers, protocol_json


CODE_POLICY_INVALID: Final = "repository_policy_invalid"
CODE_ENDPOINT_UNAVAILABLE: Final = "repository_endpoint_unavailable"
CODE_ALIAS_UNKNOWN: Final = "repository_alias_unknown"
CODE_MIRROR_UNDECLARED: Final = "repository_mirror_undeclared"

POLICY_SCHEMA_V1: Final = 1
POLICY_SCHEMA_V2: Final = 2
TRANSPORT_REVISION_V1: Final = 1
TRANSPORT_REVISION_V2: Final = 2

HTTPS: Final[Literal["https"]] = "https"
SSH: Final[Literal["ssh"]] = "ssh"
Transport = Literal["https", "ssh"]

FALLBACK_NONE: Final = "none"
FALLBACK_AVAILABILITY_AUTH: Final = "availability-auth"

# These are the only positively classified failures that can open the second
# endpoint.  The names are the stable failure classes from repository-transport
# section 2; every other value is fail-closed.
AVAILABILITY_AUTH_FAILURES: Final[frozenset[str]] = frozenset(
    {
        "dns",
        "connection-refused",
        "timeout",
        "endpoint-unavailable",
        "http-502",
        "http-503",
        "http-504",
        "auth-unavailable",
        "auth-rejected",
        "ssh-auth-rejected",
        "http-401",
        "http-403",
    }
)

# Kept as a named set for callers that want to expose the fail-closed table in
# diagnostics.  Unknown values are intentionally not included and remain
# forbidden through ``classify_failure``.
FORBIDDEN_FALLBACK_FAILURES: Final[frozenset[str]] = frozenset(
    {
        "tls",
        "host-key",
        "integrity",
        "identity",
        "ref-moved",
        "audit",
        "revocation",
        "canary",
        "assurance",
        "capability",
        "policy-unreadable",
        "http-404",
        "redirect",
        "malformed-response",
        "partial-response",
    }
)

_HOST_RE: Final[re.Pattern[str]] = re.compile(r"^[A-Za-z0-9][A-Za-z0-9.-]*$")
_CANONICAL_HOST_RE: Final[re.Pattern[str]] = re.compile(r"^[a-z0-9][a-z0-9.-]*$")
_ALIAS_NAME_RE: Final[re.Pattern[str]] = re.compile(r"^[a-z0-9][a-z0-9.-]*$")
_SSH_USER_RE: Final[re.Pattern[str]] = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
_SSH_PATH_COMPONENT_RE: Final[re.Pattern[str]] = re.compile(r"^[A-Za-z0-9._-]+$")
_ASCII_DIGITS_RE: Final[re.Pattern[str]] = re.compile(r"^[0-9]+$")
_MAX_POLICY_STRING_LENGTH: Final = 4096
_MAX_ALIAS_LENGTH: Final = 253

# JSON Schema's ECMAScript whitespace set.  Python's ``str.isspace`` has a
# few extra code points, so using this explicit set keeps the hand parser
# aligned with the published schema.
_ECMA_WHITESPACE: Final[frozenset[str]] = frozenset(
    {
        "\t",
        "\n",
        "\x0b",
        "\x0c",
        "\r",
        " ",
        "\u00a0",
        "\u1680",
        "\u2000",
        "\u2001",
        "\u2002",
        "\u2003",
        "\u2004",
        "\u2005",
        "\u2006",
        "\u2007",
        "\u2008",
        "\u2009",
        "\u200a",
        "\u2028",
        "\u2029",
        "\u202f",
        "\u205f",
        "\u3000",
        "\ufeff",
    }
)


class RepositoryPolicyError(ValueError):
    """One stable repository-policy diagnostic."""

    def __init__(self, code: str, detail: str) -> None:
        super().__init__(f"{code}: {detail}")
        self.code = code
        self.detail = detail


@dataclass(frozen=True)
class EndpointURL:
    """The parsed, non-secret parts of one policy endpoint URL."""

    url: str
    transport: Transport
    host: str
    path: str
    port: int | None = None
    username: str | None = None

    @property
    def identity(self) -> str:
        """Return this URL's canonical identity with its port removed."""

        return _identity_from_url_parts(self.host, self.path)


@dataclass(frozen=True)
class RepositoryEndpoint:
    """One explicitly listed endpoint and its operator-selected provider."""

    url: str
    authentication: str | None
    mirror_of: str | None = None
    alias: str | None = None


@dataclass(frozen=True)
class RepositoryEntry:
    """Policy for one exact canonical repository identity."""

    identity: str
    endpoints: tuple[RepositoryEndpoint, ...]
    fallback: str
    pin: str | None = None


@dataclass(frozen=True)
class HostAlias:
    """One concrete operator-owned connection host alias."""

    name: str
    host: str
    authentication: str
    port: int | None = None


@dataclass(frozen=True)
class RepositoryPolicy:
    """Validated machine-owned policy data."""

    schema_version: int
    repositories: dict[str, RepositoryEntry]
    root_inputs: dict[str, tuple[str, ...]] = field(default_factory=dict)
    aliases: dict[str, HostAlias] = field(default_factory=dict)


@dataclass(frozen=True)
class EndpointProvenance:
    """Sanitized connection diagnostics, separate from portable identity."""

    listed_url: str
    url_host: str
    url_port: int | None
    resolved_host: str
    resolved_port: int
    alias: str | None
    mirror_of: str | None
    # Keep this separate from ``resolved_port`` so a strict lane can refuse an
    # alias-selected port rather than treating it as an indistinguishable
    # transport default.
    alias_port: int | None = None


@dataclass(frozen=True)
class ResolvedEndpoint:
    """An endpoint ready for a later transport broker to attempt."""

    identity: str
    transport: Transport
    authentication: str | None
    provenance: EndpointProvenance

    @property
    def url(self) -> str:
        return self.provenance.listed_url

    @property
    def host(self) -> str:
        return self.provenance.resolved_host

    @property
    def port(self) -> int:
        return self.provenance.resolved_port


@dataclass(frozen=True)
class ResolutionPlan:
    """Pure ordered endpoint selection for one logical repository."""

    identity: str
    endpoints: tuple[ResolvedEndpoint, ...]
    fallback: str
    pinned: bool

    @property
    def max_attempts(self) -> int:
        """Return the maximum number of transport attempts for this plan."""

        if self.pinned or self.fallback != FALLBACK_AVAILABILITY_AUTH:
            return min(1, len(self.endpoints))
        return min(2, len(self.endpoints))

    def next_endpoint(
        self,
        failure_class: str,
        *,
        failed_index: int = 0,
    ) -> ResolvedEndpoint | None:
        """Return the one permitted alternate, or ``None``.

        The plan never exposes more than one alternate and a pinned plan can
        never fall back, even if its source entry says ``availability-auth``.
        """

        if failed_index != 0 or len(self.endpoints) < 2:
            return None
        if not fallback_permitted(
            self.fallback, failure_class, pinned=self.pinned
        ):
            return None
        return self.endpoints[1]


def classify_failure(failure_class: object) -> Literal["availability-auth", "forbidden"]:
    """Classify one already positively classified transport failure."""

    if isinstance(failure_class, str) and failure_class in AVAILABILITY_AUTH_FAILURES:
        return FALLBACK_AVAILABILITY_AUTH
    return "forbidden"


def fallback_permitted(
    fallback: object,
    failure_class: object,
    *,
    pinned: bool = False,
) -> bool:
    """Return whether the failure opens the second listed endpoint."""

    return (
        not pinned
        and fallback == FALLBACK_AVAILABILITY_AUTH
        and classify_failure(failure_class) == FALLBACK_AVAILABILITY_AUTH
    )


# Friendly aliases used by transport callers and tests; all route through the
# one pure decision function above.
can_fallback = fallback_permitted
allows_fallback = fallback_permitted


def validate_canonical_identity(value: Any, *, field: str = "repository identity") -> str:
    """Validate and return an exact lowercase ``host/path`` identity."""

    if not isinstance(value, str) or not 1 <= len(value) <= _MAX_POLICY_STRING_LENGTH:
        raise _invalid(f"{field} must be a canonical host/path identity")
    host, separator, path = value.partition("/")
    if not separator or not _CANONICAL_HOST_RE.fullmatch(host):
        raise _invalid(f"{field} must start with a lowercase host")
    if path.endswith(".git"):
        raise _invalid(f"{field} must not carry a terminal '.git' suffix")
    if not _valid_repository_path(path):
        raise _invalid(f"{field} has an invalid repository path")
    return value


def parse_endpoint_url(
    value: Any,
    *,
    revision: int = TRANSPORT_REVISION_V2,
    field: str = "endpoint URL",
) -> EndpointURL:
    """Parse one schema-1 or schema-2 endpoint URL without network access."""

    if (
        isinstance(revision, bool)
        or not isinstance(revision, int)
        or revision not in {TRANSPORT_REVISION_V1, TRANSPORT_REVISION_V2}
    ):
        raise _invalid(f"unsupported transport reader revision {revision!r}")
    if (
        not isinstance(value, str)
        or not 1 <= len(value) <= _MAX_POLICY_STRING_LENGTH
        or any(_is_control(character) for character in value)
    ):
        raise _invalid(f"{field} must be a non-empty endpoint URL")

    if value.startswith("https://"):
        rest = value[len("https://") :]
        authority, separator, path = rest.partition("/")
        if not separator or not authority:
            raise _invalid(f"{field} must contain a repository path")
        if "@" in authority:
            raise _invalid(f"{field} HTTPS authority must not contain userinfo")
        host, port = _split_host_port(
            authority,
            allow_port=revision == TRANSPORT_REVISION_V2,
            field=field,
        )
        _validate_https_path(path, field)
        return EndpointURL(
            url=value,
            transport=HTTPS,
            host=host.lower(),
            path=path,
            port=port,
        )

    if value.startswith("ssh://"):
        rest = value[len("ssh://") :]
        authority, separator, path = rest.partition("/")
        if not separator or not authority:
            raise _invalid(f"{field} must contain a repository path")
        username: str | None = None
        if "@" in authority:
            if authority.count("@") != 1:
                raise _invalid(f"{field} carries an invalid SSH username")
            username, authority = authority.split("@", 1)
            if _SSH_USER_RE.fullmatch(username) is None:
                raise _invalid(f"{field} carries an invalid SSH username")
        host, port = _split_host_port(
            authority,
            allow_port=revision == TRANSPORT_REVISION_V2,
            field=field,
        )
        _validate_ssh_path(path, field)
        return EndpointURL(
            url=value,
            transport=SSH,
            host=host.lower(),
            path=path,
            port=port,
            username=username,
        )

    if "://" in value:
        raise _invalid(f"{field} must use https, ssh, or scp spelling")

    # SCP spelling has no port position.  Everything after the first colon is
    # its path, so ``host:2222/repo.git`` is a repository under ``2222``.
    head, separator, path = value.partition(":")
    if not separator or not head or not path:
        raise _invalid(f"{field} must use https, ssh, or scp spelling")
    username = None
    if "@" in head:
        if head.count("@") != 1:
            raise _invalid(f"{field} carries an invalid SSH username")
        username, head = head.split("@", 1)
        if _SSH_USER_RE.fullmatch(username) is None:
            raise _invalid(f"{field} carries an invalid SSH username")
    if _HOST_RE.fullmatch(head) is None:
        raise _invalid(f"{field} carries an invalid host")
    _validate_ssh_path(path, field)
    return EndpointURL(
        url=value,
        transport=SSH,
        host=head.lower(),
        path=path,
        username=username,
    )


def canonical_endpoint_identity(
    value: Any,
    *,
    revision: int = TRANSPORT_REVISION_V2,
) -> str:
    """Canonicalize a policy endpoint, stripping its port before identity."""

    return parse_endpoint_url(value, revision=revision).identity


def canonical_repository_identity(
    value: Any,
    *,
    revision: int = TRANSPORT_REVISION_V2,
) -> str:
    """Compatibility spelling for :func:`canonical_endpoint_identity`."""

    return canonical_endpoint_identity(value, revision=revision)


canonical_identity = canonical_endpoint_identity


def parse_root_inputs(raw: Any) -> dict[str, tuple[str, ...]]:
    """Parse the policy's source-alias to portable root-input allowlist."""

    if raw is None:
        return {}
    if not isinstance(raw, dict):
        raise _invalid("root_inputs must be an object when present")
    result: dict[str, tuple[str, ...]] = {}
    for alias, values in raw.items():
        if not isinstance(alias, str) or not identifiers.is_valid_identifier(alias):
            raise _invalid(f"root_inputs key {alias!r} must be a portable identifier")
        if not isinstance(values, list) or not values:
            raise _invalid(f"root_inputs.{alias} must be a non-empty list")
        parsed: list[str] = []
        seen: set[str] = set()
        for index, value in enumerate(values):
            if not isinstance(value, str) or not identifiers.is_valid_portable_path(value):
                raise _invalid(
                    f"root_inputs.{alias}[{index}] must be a portable relative path"
                )
            if value in seen:
                raise _invalid(f"root_inputs.{alias} contains duplicate path {value!r}")
            seen.add(value)
            parsed.append(value)
        _reject_overlapping_paths(parsed, f"root_inputs.{alias}")
        result[alias] = tuple(parsed)
    return result


def parse_policy(
    raw: Any,
    *,
    reader_revision: int | None = None,
    transport_revision: int | None = None,
    supported_revision: int | None = None,
    revision: int | None = None,
) -> RepositoryPolicy:
    """Parse source-policy schema 1 or 2 with a closed reader revision.

    A revision-2 reader accepts schema 1 with revision-1 endpoint semantics.
    A revision-1 reader rejects schema 2, including its otherwise valid
    schema-1-shaped documents.
    """

    effective_revision = _reader_revision(
        reader_revision,
        transport_revision=transport_revision,
        supported_revision=supported_revision,
        revision=revision,
    )
    if not isinstance(raw, dict):
        raise _invalid("source policy must be a JSON object")
    schema = raw.get("schema_version")
    if not isinstance(schema, int) or isinstance(schema, bool):
        raise _invalid("source policy requires an integer schema_version")
    if schema not in {POLICY_SCHEMA_V1, POLICY_SCHEMA_V2}:
        raise _invalid(f"unsupported source policy schema_version {schema!r}")
    if schema > effective_revision:
        raise _invalid(
            f"source policy schema_version {schema} is not supported by revision {effective_revision} reader"
        )

    allowed_top = {"schema_version", "repositories", "root_inputs"}
    if schema == POLICY_SCHEMA_V2:
        allowed_top.add("aliases")
    _reject_unknown(raw, allowed_top, "source policy")

    repositories_raw = raw.get("repositories")
    if not isinstance(repositories_raw, dict):
        raise _invalid("source policy requires 'repositories' as an object")

    aliases: dict[str, HostAlias] = {}
    if schema == POLICY_SCHEMA_V2:
        if "aliases" in raw and raw["aliases"] is None:
            raise _invalid("aliases must be an object when present")
        aliases = _parse_aliases(raw["aliases"]) if "aliases" in raw else {}

    if "root_inputs" in raw and raw["root_inputs"] is None:
        raise _invalid("root_inputs must be an object when present")
    root_inputs = parse_root_inputs(raw["root_inputs"]) if "root_inputs" in raw else {}
    repositories: dict[str, RepositoryEntry] = {}
    for identity, entry_raw in repositories_raw.items():
        identity = validate_canonical_identity(identity, field="repository key")
        if not isinstance(entry_raw, dict):
            raise _invalid(f"repository {identity!r} must be an object")
        endpoint_allowed = {"url", "authentication"}
        if schema == POLICY_SCHEMA_V2:
            endpoint_allowed.update({"mirror_of", "alias"})
        _reject_unknown(
            entry_raw,
            {"endpoints", "pin", "fallback"},
            f"repository {identity!r}",
        )
        endpoints_raw = entry_raw.get("endpoints")
        if not isinstance(endpoints_raw, list) or not 1 <= len(endpoints_raw) <= 2:
            raise _invalid(
                f"repository {identity!r}.endpoints must contain one or two endpoints"
            )
        endpoints: list[RepositoryEndpoint] = []
        seen_urls: set[str] = set()
        for index, endpoint_raw in enumerate(endpoints_raw):
            endpoint = _parse_endpoint(
                endpoint_raw,
                identity=identity,
                aliases=aliases,
                schema=schema,
                allowed_fields=endpoint_allowed,
                field=f"repository {identity!r}.endpoints[{index}]",
            )
            if endpoint.url in seen_urls:
                raise _invalid(
                    f"repository {identity!r}.endpoints repeats URL {endpoint.url!r}"
                )
            seen_urls.add(endpoint.url)
            endpoints.append(endpoint)

        fallback = entry_raw.get("fallback")
        if not isinstance(fallback, str) or fallback not in {
            FALLBACK_NONE,
            FALLBACK_AVAILABILITY_AUTH,
        }:
            raise _invalid(
                f"repository {identity!r}.fallback must be 'none' or 'availability-auth'"
            )

        pin: str | None = None
        if "pin" in entry_raw:
            pin = entry_raw["pin"]
            parse_endpoint_url(
                pin,
                revision=schema,
                field=f"repository {identity!r}.pin",
            )
            if pin not in seen_urls:
                raise _invalid(
                    f"repository {identity!r}.pin must exactly equal a listed URL"
                )

        repositories[identity] = RepositoryEntry(
            identity=identity,
            endpoints=tuple(endpoints),
            fallback=fallback,
            pin=pin,
        )

    try:
        # Direct callers may provide a Python value rather than bytes decoded
        # by ``load_policy``.  Keep the production parser on the same
        # portable JSON surface and turn malformed values into the stable
        # policy diagnostic instead of leaking a type or Unicode exception.
        # This check follows field validation so a malformed field reports
        # its repository and name at the structural boundary first.
        protocol_json.validate_canonical(raw)
    except Exception as exc:
        raise _invalid(f"source policy is not portable JSON: {exc}") from exc

    return RepositoryPolicy(
        schema_version=schema,
        repositories=repositories,
        root_inputs=root_inputs,
        aliases=aliases,
    )


# The descriptive name is useful at call sites that read a source-policy file
# rather than a generic JSON value.
parse_source_policy = parse_policy
parse_repository_policy = parse_policy


def resolve_endpoint(
    identity: str,
    endpoint: RepositoryEndpoint,
    *,
    aliases: dict[str, HostAlias] | None = None,
    revision: int = TRANSPORT_REVISION_V2,
) -> ResolvedEndpoint:
    """Resolve one already-parsed endpoint through the section-5 checks."""

    key = validate_canonical_identity(identity)
    alias_table = aliases or {}
    parsed = parse_endpoint_url(endpoint.url, revision=revision)
    if endpoint.authentication is not None:
        _validate_authentication(endpoint.authentication, field="endpoint authentication")
    key_host, key_path = key.split("/", 1)

    selected_alias: HostAlias | None = None
    if revision == TRANSPORT_REVISION_V1:
        if endpoint.alias is not None or endpoint.mirror_of is not None:
            raise _invalid("schema-1 endpoint carries revision-2 properties")
        if parsed.identity != key:
            raise _invalid(
                f"endpoint {endpoint.url!r} canonicalizes to {parsed.identity!r}, not {key!r}"
            )
        resolved_host = parsed.host
        resolved_port = parsed.port
        if resolved_port is None:
            resolved_port = 443 if parsed.transport == HTTPS else 22
    else:
        if parsed.host in alias_table:
            raise _invalid(
                f"endpoint URL host {parsed.host!r} must not embed an alias name"
            )
        if endpoint.alias is not None:
            selected_alias = alias_table.get(endpoint.alias)
            if selected_alias is None:
                raise RepositoryPolicyError(
                    CODE_ALIAS_UNKNOWN,
                    f"endpoint {endpoint.url!r} names unknown alias {endpoint.alias!r}",
                )
            _ensure_concrete_alias_target(alias_table, selected_alias)
            if parsed.host != key_host:
                raise _invalid(
                    f"mirror URL {endpoint.url!r} must not be combined with alias {endpoint.alias!r}"
                )
            if (
                parsed.transport == SSH
                and not endpoint.url.startswith("ssh://")
                and selected_alias.port is not None
            ):
                raise _invalid(
                    "SCP endpoint spelling cannot carry an alias-selected SSH port; "
                    "use an ssh:// endpoint"
                )
            if parsed.port is not None and selected_alias.port is not None:
                raise _invalid("URL port and alias port must not both be present")
            if selected_alias.authentication != endpoint.authentication:
                raise _invalid(
                    f"alias {endpoint.alias!r} authentication must equal endpoint authentication"
                )

        resolved_host = selected_alias.host if selected_alias is not None else parsed.host
        if selected_alias is not None:
            resolved_port = (
                selected_alias.port
                if selected_alias.port is not None
                else parsed.port
            )
        else:
            resolved_port = parsed.port
        if resolved_port is None:
            resolved_port = 443 if parsed.transport == HTTPS else 22
        parsed_path = parsed.identity.split("/", 1)[1]
        if parsed_path != key_path:
            raise _invalid(
                f"endpoint {endpoint.url!r} path does not match repository {key!r}"
            )
        _check_mirror_predicate(
            identity=key,
            resolved_host=resolved_host,
            mirror_of=endpoint.mirror_of,
        )

    provenance = EndpointProvenance(
        listed_url=endpoint.url,
        url_host=parsed.host,
        url_port=parsed.port,
        resolved_host=resolved_host,
        resolved_port=resolved_port,
        alias=endpoint.alias,
        mirror_of=endpoint.mirror_of,
        alias_port=selected_alias.port if selected_alias is not None else None,
    )
    return ResolvedEndpoint(
        identity=key,
        transport=parsed.transport,
        authentication=endpoint.authentication,
        provenance=provenance,
    )


def select_endpoints(policy: RepositoryPolicy, identity: str) -> ResolutionPlan:
    """Select and resolve one exact policy entry in list order."""

    key = validate_canonical_identity(identity)
    entry = policy.repositories.get(key)
    if entry is None:
        raise RepositoryPolicyError(
            CODE_ENDPOINT_UNAVAILABLE,
            f"no machine endpoint policy entry for {key!r}",
        )
    revision = policy.schema_version
    resolved = tuple(
        resolve_endpoint(key, endpoint, aliases=policy.aliases, revision=revision)
        for endpoint in entry.endpoints
    )
    if entry.pin is None:
        selected = resolved
        pinned = False
    else:
        selected = tuple(endpoint for endpoint in resolved if endpoint.url == entry.pin)
        if len(selected) != 1:
            # This also protects callers that manually construct a model
            # instead of using ``parse_policy``.
            raise _invalid(
                f"repository {key!r}.pin must exactly equal one listed URL"
            )
        pinned = True
    return ResolutionPlan(
        identity=key,
        endpoints=selected,
        fallback=entry.fallback,
        pinned=pinned,
    )


def resolve_repository(
    policy: RepositoryPolicy | None,
    identity: str,
    *,
    declared_url: str | None = None,
    declared_authentication: str | None = None,
    revision: int = TRANSPORT_REVISION_V1,
) -> ResolutionPlan:
    """Resolve a logical repository, including revision-1 absence semantics.

    A missing policy entry with a declared URL creates exactly one attempt and
    no fallback.  A logical declaration without an entry has no safe endpoint
    and returns ``repository_endpoint_unavailable``.
    """

    key = validate_canonical_identity(identity)
    if policy is not None and key in policy.repositories:
        return select_endpoints(policy, key)
    if declared_url is None:
        raise RepositoryPolicyError(
            CODE_ENDPOINT_UNAVAILABLE,
            f"no declared or policy endpoint for {key!r}",
        )
    parsed = parse_endpoint_url(declared_url, revision=revision, field="declared endpoint")
    if parsed.identity != key:
        raise _invalid(
            f"declared endpoint {declared_url!r} canonicalizes to {parsed.identity!r}, not {key!r}"
        )
    endpoint = RepositoryEndpoint(
        url=declared_url,
        authentication=declared_authentication,
    )
    resolved = resolve_endpoint(key, endpoint, revision=revision)
    return ResolutionPlan(
        identity=key,
        endpoints=(resolved,),
        fallback=FALLBACK_NONE,
        pinned=False,
    )


resolve_repository_endpoints = select_endpoints
resolve_policy_entry = select_endpoints


def load_policy(
    path: Path | None = None,
    *,
    reader_revision: int | None = None,
    transport_revision: int | None = None,
    supported_revision: int | None = None,
    revision: int | None = None,
) -> RepositoryPolicy | None:
    """Load the operator policy, distinguishing absence from read failure."""

    try:
        # Keep both the default locator and explicit path expansion inside the
        # typed policy boundary.  A locator failure is not a missing policy.
        resolved = path if path is not None else _default_policy_path()
        resolved = resolved.expanduser()
    except Exception as exc:
        raise RepositoryPolicyError(
            CODE_POLICY_INVALID,
            f"cannot resolve source policy path: {exc}",
        ) from exc
    try:
        info = resolved.lstat()
    except FileNotFoundError:
        return None
    except Exception as exc:
        raise RepositoryPolicyError(
            CODE_POLICY_INVALID,
            f"cannot inspect source policy {resolved}: {exc}",
        ) from exc
    if stat.S_ISLNK(info.st_mode):
        raise RepositoryPolicyError(
            CODE_POLICY_INVALID,
            f"source policy must not be a symbolic link: {resolved}",
        )
    if not stat.S_ISREG(info.st_mode):
        raise RepositoryPolicyError(
            CODE_POLICY_INVALID,
            f"source policy is not a regular file: {resolved}",
        )
    try:
        raw = resolved.read_bytes()
    except Exception as exc:
        raise RepositoryPolicyError(
            CODE_POLICY_INVALID,
            f"cannot read source policy {resolved}: {exc}",
        ) from exc
    try:
        document = protocol_json.loads(raw)
    except Exception as exc:
        raise RepositoryPolicyError(
            CODE_POLICY_INVALID,
            f"source policy {resolved} is not valid protocol JSON: {exc}",
        ) from exc
    try:
        return parse_policy(
            document,
            reader_revision=reader_revision,
            transport_revision=transport_revision,
            supported_revision=supported_revision,
            revision=revision,
        )
    except RepositoryPolicyError:
        raise
    except Exception as exc:
        raise RepositoryPolicyError(
            CODE_POLICY_INVALID,
            f"source policy {resolved} could not be validated: {exc}",
        ) from exc


load_source_policy = load_policy
load_repository_policy = load_policy


def _parse_aliases(raw: Any) -> dict[str, HostAlias]:
    if raw is None:
        return {}
    if not isinstance(raw, dict):
        raise _invalid("aliases must be an object when present")
    aliases: dict[str, HostAlias] = {}
    for name, entry in raw.items():
        _validate_alias_name(name, field="alias name")
        if not isinstance(entry, dict):
            raise _invalid(f"alias {name!r} must be an object")
        _reject_unknown(entry, {"host", "port", "authentication"}, f"alias {name!r}")
        host = entry.get("host")
        host = _validate_alias_host(host, field=f"alias {name!r}.host")
        authentication = _validate_authentication(
            entry.get("authentication"), field=f"alias {name!r}.authentication"
        )
        port = (
            _parse_alias_port(entry["port"], field=f"alias {name!r}.port")
            if "port" in entry
            else None
        )
        aliases[name] = HostAlias(
            name=name,
            host=host,
            authentication=authentication,
            port=port,
        )
    for alias in aliases.values():
        _ensure_concrete_alias_target(aliases, alias)
    return aliases


def _ensure_concrete_alias_target(
    aliases: dict[str, HostAlias], alias: HostAlias
) -> None:
    if alias.host in aliases:
        raise _invalid(
            f"alias {alias.name!r} must target a concrete host, not alias {alias.host!r}"
        )


def _parse_endpoint(
    raw: Any,
    *,
    identity: str,
    aliases: dict[str, HostAlias],
    schema: int,
    allowed_fields: set[str],
    field: str,
) -> RepositoryEndpoint:
    if not isinstance(raw, dict):
        raise _invalid(f"{field} must be an object")
    _reject_unknown(raw, allowed_fields, field)
    url = raw.get("url")
    parsed = parse_endpoint_url(url, revision=schema, field=f"{field}.url")
    url = parsed.url
    authentication = _validate_authentication(
        raw.get("authentication"), field=f"{field}.authentication"
    )
    mirror_of: str | None = None
    alias_name: str | None = None
    if schema == POLICY_SCHEMA_V2:
        if "mirror_of" in raw:
            mirror_of = validate_canonical_identity(
                raw["mirror_of"], field=f"{field}.mirror_of"
            )
        if "alias" in raw:
            alias_name = _validate_alias_name(raw["alias"], field=f"{field}.alias")

    endpoint = RepositoryEndpoint(
        url=url,
        authentication=authentication,
        mirror_of=mirror_of,
        alias=alias_name,
    )
    # Run the same production resolution gate during parsing, before the
    # policy can be handed to a transport caller.  It also enforces the exact
    # path and mirror/alias predicates for every endpoint, not only selected
    # or pinned endpoints.
    resolve_endpoint(identity, endpoint, aliases=aliases, revision=schema)
    return endpoint


def _check_mirror_predicate(
    *,
    identity: str,
    resolved_host: str,
    mirror_of: str | None,
) -> None:
    key_host = identity.split("/", 1)[0]
    is_mirror = resolved_host.lower() != key_host
    if is_mirror and mirror_of is None:
        raise RepositoryPolicyError(
            CODE_MIRROR_UNDECLARED,
            f"resolved host {resolved_host!r} differs from {key_host!r} without mirror_of",
        )
    if is_mirror and mirror_of is not None and mirror_of != identity:
        raise _invalid("mirror_of must equal the entry key exactly")
    if not is_mirror and mirror_of is not None:
        raise _invalid("mirror_of is forbidden when the resolved host equals the key host")


def _split_host_port(
    authority: str,
    *,
    allow_port: bool,
    field: str,
) -> tuple[str, int | None]:
    if authority.count(":") > 1:
        raise _invalid(f"{field} carries an invalid host or port")
    if ":" not in authority:
        host = authority
        port = None
    else:
        host, raw_port = authority.split(":", 1)
        if not allow_port:
            raise _invalid(f"{field} must not contain an explicit port")
        port = _parse_port_text(raw_port, field=field)
    if _HOST_RE.fullmatch(host) is None:
        raise _invalid(f"{field} carries an invalid host")
    return host, port


def _parse_port_text(raw: str, *, field: str) -> int:
    if _ASCII_DIGITS_RE.fullmatch(raw) is None:
        raise _invalid(f"{field} carries an invalid decimal port")
    if len(raw) > 1 and raw.startswith("0"):
        raise _invalid(f"{field} port must not contain leading zeroes")
    if len(raw) > 5:
        raise _invalid(f"{field} port must be between 1 and 65535")
    port = int(raw)
    if not 1 <= port <= 65535:
        raise _invalid(f"{field} port must be between 1 and 65535")
    return port


def _parse_alias_port(raw: Any, *, field: str) -> int | None:
    if raw is None:
        raise _invalid(f"{field} must be an integer between 1 and 65535")
    if isinstance(raw, bool) or not isinstance(raw, int) or not 1 <= raw <= 65535:
        raise _invalid(f"{field} must be an integer between 1 and 65535")
    return raw


def _validate_authentication(value: Any, *, field: str) -> str:
    if not isinstance(value, str) or not identifiers.is_valid_identifier(value):
        raise _invalid(f"{field} must be a portable opaque provider identifier")
    return value


def _validate_alias_name(value: Any, *, field: str) -> str:
    if (
        not isinstance(value, str)
        or not 1 <= len(value) <= _MAX_ALIAS_LENGTH
        or _ALIAS_NAME_RE.fullmatch(value) is None
    ):
        raise _invalid(f"{field} must be a lowercase alias name")
    return value


def _validate_alias_host(value: Any, *, field: str) -> str:
    if (
        not isinstance(value, str)
        or not 1 <= len(value) <= _MAX_ALIAS_LENGTH
        or _ALIAS_NAME_RE.fullmatch(value) is None
    ):
        raise _invalid(f"{field} must be a lowercase concrete host")
    return value


def _validate_https_path(path: str, field: str) -> None:
    if not _valid_repository_path(path):
        raise _invalid(f"{field} carries an invalid HTTPS repository path")


def _validate_ssh_path(path: str, field: str) -> None:
    parts = path.split("/")
    if not parts or any(
        not part
        or part in {".", ".."}
        or _SSH_PATH_COMPONENT_RE.fullmatch(part) is None
        for part in parts
    ):
        raise _invalid(f"{field} carries an invalid SSH repository path")


def _valid_repository_path(path: str) -> bool:
    parts = path.split("/")
    if not parts or any(not part or part in {".", ".."} for part in parts):
        return False
    for part in parts:
        for character in part:
            if (
                character in _ECMA_WHITESPACE
                or _is_control(character)
                or character in "\\:%?#"
            ):
                return False
    return True


def _identity_from_url_parts(host: str, path: str) -> str:
    canonical_path = path
    if canonical_path.endswith(".git"):
        canonical_path = canonical_path[: -len(".git")]
    if not _valid_repository_path(canonical_path):
        raise _invalid("endpoint canonicalizes to an empty or invalid repository path")
    identity = f"{host.lower()}/{canonical_path}"
    return validate_canonical_identity(identity)


def _reject_overlapping_paths(paths: list[str], field: str) -> None:
    components = sorted((tuple(path.split("/")), path) for path in paths)
    for index, (left_parts, left) in enumerate(components):
        for right_parts, right in components[index + 1 :]:
            if _parts_contain(left_parts, right_parts) or _parts_contain(
                right_parts, left_parts
            ):
                raise _invalid(f"{field} contains overlapping paths {left!r} and {right!r}")


def _parts_contain(root: tuple[str, ...], path: tuple[str, ...]) -> bool:
    return len(path) >= len(root) and path[: len(root)] == root


def _is_control(character: str) -> bool:
    code = ord(character)
    return code < 0x20 or 0x7F <= code <= 0x9F


def _reader_revision(
    reader_revision: Any,
    *,
    transport_revision: int | None,
    supported_revision: int | None,
    revision: int | None,
) -> int:
    values = [
        value
        for value in (
            reader_revision,
            transport_revision,
            supported_revision,
            revision,
        )
        if value is not None
    ]
    if any(isinstance(value, bool) or not isinstance(value, int) for value in values):
        raise _invalid("transport reader revision must be integer 1 or 2")
    if transport_revision is not None and supported_revision is not None:
        if transport_revision != supported_revision:
            raise _invalid("transport reader revision arguments disagree")
    selected_values = {
        value
        for value in (transport_revision, supported_revision, revision)
        if value is not None
    }
    if len(selected_values) > 1:
        raise _invalid("transport reader revision arguments disagree")
    if reader_revision is not None and selected_values:
        selected = next(iter(selected_values))
        if selected != reader_revision:
            raise _invalid("transport reader revision arguments disagree")
    elif reader_revision is not None:
        selected = reader_revision
    else:
        selected = next(iter(selected_values), TRANSPORT_REVISION_V2)
    if selected not in {TRANSPORT_REVISION_V1, TRANSPORT_REVISION_V2}:
        raise _invalid(f"unsupported transport reader revision {selected!r}")
    return selected


def _reject_unknown(data: dict[str, Any], allowed: set[str], field: str) -> None:
    # JSON object keys are strings, but keeping the public parser structured
    # for direct Python callers avoids a raw ``TypeError`` on a malformed
    # mapping with a non-string key.
    unknown = [key for key in data if key not in allowed]
    if unknown:
        joined = ", ".join(repr(item) for item in sorted(unknown, key=repr))
        raise _invalid(f"{field} has unsupported field(s): {joined}")


def _invalid(detail: str) -> RepositoryPolicyError:
    return RepositoryPolicyError(CODE_POLICY_INVALID, detail)


def _default_policy_path() -> Path:
    # Keep the locator in config.py as the public configuration surface, but
    # import it lazily so config.py can offer a convenience loader without a
    # module cycle during package import.
    from ..config import source_policy_path

    return source_policy_path()
