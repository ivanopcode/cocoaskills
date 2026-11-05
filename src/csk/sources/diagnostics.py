"""User-visible rendering for the stable draft-sources diagnostics.

The thirteen protocol classes (nine source classes from
skillfile-sources section 5, four repository classes from
repository-transport) render through one declared table and one
renderer. The table lives here rather than in
:mod:`csk.sources.errors` because it references constants from both
error modules, and ``errors`` must keep zero intra-package imports:
the selection import-closure tests pin the exact module set reachable
from the selection entry points.

:func:`format_diagnostic` turns one (code, reason) pair into the
user-visible two-line shape ``code: reason`` / ``remediation: ...``.
Reasons pass through
:func:`csk.sources.errors.sanitize_detail`, so a rendered diagnostic
names its subject but never a secret.
"""

from __future__ import annotations

from typing import Final

from . import errors as source_errors
from . import repository_policy

#: The thirteen stable protocol classes this leaf renders, referenced by
#: constant (never by string literal) so a rename breaks loudly. Order
#: follows the protocol section 5 listing, then repository-transport.
STABLE_DIAGNOSTIC_CODES: Final[tuple[str, ...]] = (
    source_errors.CODE_ALIAS_UNKNOWN,
    source_errors.CODE_SELECTION_INVALID,
    source_errors.CODE_MEMBER_MISSING,
    source_errors.CODE_MEMBER_INVALID,
    source_errors.CODE_NAME_CONFLICT,
    source_errors.CODE_OUTPUT_OVERLAP,
    source_errors.CODE_SNAPSHOT_CHANGED,
    source_errors.CODE_SNAPSHOT_UNAVAILABLE,
    source_errors.CODE_LOCK_STALE,
    repository_policy.CODE_POLICY_INVALID,
    repository_policy.CODE_ENDPOINT_UNAVAILABLE,
    repository_policy.CODE_MIRROR_UNDECLARED,
    repository_policy.CODE_ALIAS_UNKNOWN,
)

#: Source codes that are intentionally NOT protocol-stable renderings.
#: The completeness test derives every ``CODE_*`` constant from the two
#: error modules and requires each to be either a remediation-table key
#: or a member of this set, so a fourteenth class fails the test
#: instead of being silently unrendered.
NON_PROTOCOL_SOURCE_CODES: Final[frozenset[str]] = frozenset(
    {
        source_errors.CODE_PATH_CONFLICT,
        source_errors.CODE_INVENTORY_INVALID,
        source_errors.CODE_PATH_EQUIVALENCE_INVALID,
    }
)

#: One remediation action per stable class. Values are single lines
#: without the ``remediation:`` prefix; the renderer adds it.
REMEDIATION_BY_CODE: Final[dict[str, str]] = {
    source_errors.CODE_ALIAS_UNKNOWN: (
        "declare the alias under Skillfile 'sources' or fix the selector 'from' value"
    ),
    source_errors.CODE_SELECTION_INVALID: (
        "fix the Skillfile source or selector declaration and rerun"
    ),
    source_errors.CODE_MEMBER_MISSING: (
        "add the missing member to the source or drop the selector"
    ),
    source_errors.CODE_MEMBER_INVALID: "fix the member package contents and rerun",
    source_errors.CODE_NAME_CONFLICT: "rename one skill so installed names are unique",
    source_errors.CODE_OUTPUT_OVERLAP: (
        "move the source outside managed outputs or choose a non-overlapping destination"
    ),
    source_errors.CODE_SNAPSHOT_CHANGED: (
        "run csk upgrade to refresh the lock, or restore the locked bytes"
    ),
    source_errors.CODE_SNAPSHOT_UNAVAILABLE: (
        "restore the source named above and rerun csk install"
    ),
    source_errors.CODE_LOCK_STALE: "run csk upgrade to refresh the lock",
    repository_policy.CODE_POLICY_INVALID: (
        "fix source-policy.json against the schema and rerun"
    ),
    repository_policy.CODE_ENDPOINT_UNAVAILABLE: (
        "verify the listed endpoint and its operator-selected authentication provider"
    ),
    repository_policy.CODE_MIRROR_UNDECLARED: (
        "declare the mirror in source-policy.json with its mirror_of entry"
    ),
    repository_policy.CODE_ALIAS_UNKNOWN: (
        "declare the alias in source-policy.json aliases or fix the endpoint reference"
    ),
}


# Class actions are separate from protocol codes and stderr hints so additions
# do not change transport classification or fallback policy.
NEXT_BY_FAILURE_CLASS: Final[dict[str, str]] = {
    "dns": "check DNS for the repository host and rerun csk install",
    "connection-refused": "check the repository host, port and network access",
    "timeout": "check network access and whether SSH is waiting for a passphrase, PIN or key touch; retry when ready",
    "endpoint-unavailable": "check the listed repository endpoint and retry",
    "auth-unavailable": "select usable credentials with csk config build-ssh add or csk config build-https add",
    "auth-rejected": "verify the selected account has read access to the repository",
    "ssh-auth-rejected": "the key may be unusable locally (a passphrase key may not be loaded in a capable agent), or the server may deny repository read access; review csk config build-ssh list and check both key usability and account access",
    "http-401": "check the selected HTTPS credential and repository read access",
    "http-403": "grant the selected account repository read access or select an authorized credential",
    "host-key": "verify the host fingerprint with the repository operator and update the selected known_hosts file",
    "tls": "verify the server certificate and the selected trust configuration",
    "integrity": "restore the locked repository objects and rerun csk install",
    "identity": "check the declared repository identity and endpoint mapping",
    "ref-moved": "restore the locked ref or run csk upgrade after reviewing the change",
    "redirect": "declare the intended endpoint explicitly in source-policy.json",
    "audit": "resolve the repository audit refusal before retrying",
    "revocation": "replace the revoked repository authorization before retrying",
    "canary": "investigate the repository canary refusal before retrying",
    "assurance": "satisfy the required repository assurance level before retrying",
    "capability": "select an endpoint with the required repository capability",
    "policy-unreadable": "restore a readable source-policy.json and rerun csk install",
    "http-404": "verify the repository URL and the selected account's read access",
    "malformed-response": "check the repository service response before retrying",
    "partial-response": "check the repository service and network before retrying",
    "unclassified": "review csk config build-ssh list, then check the selected Git/SSH command locally with stderr attached; csk --verbose shows the safe cause chain",
}
for _status in ("http-502", "http-503", "http-504"):
    NEXT_BY_FAILURE_CLASS[_status] = "check repository service availability and retry"

HINT_TEXT: Final[dict[str, str]] = {
    "ssh-pinned-key-not-loaded": "SSH reported failure loading a public-key pin; load the matching key into a capable agent or select the matching private identity path with csk config build-ssh add",
    "ssh-agent-refused": "SSH reported agent refusal; check whether the agent supports this key; for a PIN-required key use the file route with a usable provider rather than relying on a client terminal",
    "ssh-device-missing": "SSH reported a missing device; connect the security key and retry",
    "ssh-token-not-signed": "SSH reported invalid signing format; touch the security key when required and check that a usable security-key provider is selected",
    "ssh-pin-unavailable": "SSH reported an unavailable PIN; use a PIN-capable attended file route or an agent configured with askpass",
    "ssh-identity-inaccessible": "SSH reported an inaccessible identity; correct the selected identity path with csk config build-ssh add",

    "ssh-signing-failed": "SSH reported a signing failure; check the selected key, agent and any required PIN or touch",
    "ssh-key-load-failed": "SSH reported a key-loading failure; check the selected key format and unlock method",
}


def transport_next_steps(
    failure_class: object, hints: object = (), *, ordinal: int | None = None
) -> str:
    """Use only fixed text; malformed hints and raw child output never render."""
    safe_class = failure_class if type(failure_class) is str else "unclassified"
    action = NEXT_BY_FAILURE_CLASS.get(safe_class, NEXT_BY_FAILURE_CLASS["unclassified"])
    label = f"attempt {ordinal}: " if type(ordinal) is int else ""
    lines = [f"next: {label}{action}"]
    if type(hints) is tuple:
        lines.extend(
            f"hint: {label}{HINT_TEXT[hint]}"
            for hint in hints if type(hint) is str and hint in HINT_TEXT
        )
    return "\n".join(lines)


def format_diagnostic(code: str, reason: str) -> str:
    """Render one stable class as a reason and remediation line."""

    try:
        remediation = REMEDIATION_BY_CODE[code]
    except KeyError as exc:
        raise ValueError(f"unknown schema-2 source diagnostic code: {code}") from exc
    return (
        f"{code}: {source_errors.sanitize_detail(reason)}\n"
        f"remediation: {remediation}"
    )


def format_exception(exc: BaseException) -> str | None:
    """Render a source/policy diagnostic, or ``None`` when not one.

    Only :class:`csk.sources.errors.SourceError` and
    ``RepositoryPolicyError`` with a stable table code render here.
    Anything else (including ``ValueError`` from the released v1
    paths) returns ``None`` so the caller keeps its existing
    rendering byte-identical.
    """

    if isinstance(exc, source_errors.SourceError):
        if exc.code not in REMEDIATION_BY_CODE:
            return None
        return format_diagnostic(exc.code, exc.detail)
    if isinstance(exc, repository_policy.RepositoryPolicyError):
        if exc.code not in REMEDIATION_BY_CODE:
            return None
        return format_diagnostic(exc.code, exc.detail)
    return None


def _structural_endpoint_subject(listed_url: str) -> str | None:
    """Render one endpoint URL from its parsed parts, never verbatim.

    The production endpoint parser owns the grammar, so this render
    cannot drift from it: scheme, host, port and path come from the
    parsed value while userinfo, query and fragment are dropped by
    construction. An unparseable URL yields no subject rather than a
    raw echo.
    """

    try:
        parsed = repository_policy.parse_endpoint_url(listed_url)
    except repository_policy.RepositoryPolicyError:
        return None
    port = "" if parsed.port is None else f":{parsed.port}"
    return f"{parsed.transport}://{parsed.host}{port}/{parsed.path}"


def transport_failure_reason(exc: BaseException) -> str:
    """Build the display reason for a transport refusal (duck-typed).

    The transport module is deliberately not imported here: the callers
    (``cli``, ``installer``) already hold the real ``TransportError``
    type for the ``isinstance`` gate. Attempt classifications are the
    reason; the first attempt's endpoint names the involved subject
    structurally (parsed scheme, host and path), never as the raw
    listed string.
    """

    classifications: list[str] = []
    first_url: str | None = None
    attempts = getattr(exc, "attempts", ())
    if isinstance(attempts, tuple):
        for item in attempts:
            classification = getattr(item, "classification", None)
            if isinstance(classification, str) and classification:
                classifications.append(
                    f"attempt {getattr(item, 'ordinal', '?')}={classification}"
                )
            if first_url is None:
                candidate = getattr(
                    getattr(item, "endpoint", None), "listed_url", None
                )
                if isinstance(candidate, str) and candidate:
                    first_url = candidate
    if classifications:
        reason = "repository endpoint unavailable: " + ", ".join(classifications)
        if first_url is not None:
            subject = _structural_endpoint_subject(first_url)
            if subject is not None:
                reason += f" for {subject}"
        return reason
    cause = exc.__cause__
    if isinstance(
        cause, (source_errors.SourceError, repository_policy.RepositoryPolicyError)
    ):
        return cause.detail
    failure_class = getattr(exc, "failure_class", None)
    if isinstance(failure_class, str) and failure_class:
        return f"transport failure class {failure_class}"
    return "transport refusal recorded without attempt detail"


def format_transport_exception(exc: BaseException) -> str | None:
    """Render a transport refusal with a stable code, or ``None``.

    The caller gates on the real ``TransportError`` type; this function
    additionally requires a stable table code (so the released-lane
    ``build_repository_transport_*`` codes keep their existing
    rendering) and a transport-module origin.
    """

    code = None
    try:
        code = getattr(exc, "code", None)
        if type(code) is not str or code not in REMEDIATION_BY_CODE:
            return None
        transport_module = __name__.rsplit(".", 1)[0] + ".transport"
        if type(exc).__module__ != transport_module:
            return None
        rendered = format_diagnostic(code, transport_failure_reason(exc))
        attempts = getattr(exc, "attempts", ())
        if type(attempts) is tuple:
            for index, attempt in enumerate(attempts, 1):
                ordinal = getattr(attempt, "ordinal", index)
                rendered += "\n" + transport_next_steps(
                    getattr(attempt, "classification", "unclassified"),
                    getattr(attempt, "hints", ()),
                    ordinal=ordinal if type(ordinal) is int else index,
                )
        return rendered
    except Exception:  # A malformed diagnostic must not mask the original refusal.
        if type(code) is str and code in REMEDIATION_BY_CODE:
            return format_diagnostic(code, "transport refusal recorded without attempt detail")
        return None
