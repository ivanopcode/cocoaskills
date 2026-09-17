"""Bounded authenticated acquisition for Skillfile source repositories.

The policy module decides which concrete endpoints are eligible.  This module
performs the bounded act of trying them.  It never reparses policy between
attempts and every default attempt goes through :func:`git_admission.acquire_network`.
"""

from __future__ import annotations

import inspect
import re
import time
from collections.abc import Callable
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Literal, Protocol

from .. import git_admission
from ..build_repository import LockedCommit, RepositorySource
from ..git_admission import GitAdmissionError, GitTool, Limits, Snapshot
from . import repository_policy
from .repository_policy import (
    EndpointProvenance,
    RepositoryPolicy,
    ResolutionPlan,
    ResolvedEndpoint,
)

CODE_ENDPOINT_UNAVAILABLE: str = repository_policy.CODE_ENDPOINT_UNAVAILABLE
CODE_POLICY_INVALID: str = repository_policy.CODE_POLICY_INVALID
LANE_SKILLFILE: Literal["skillfile-source"] = "skillfile-source"
LANE_EXTERNAL_BUILD: Literal["external-build"] = "external-build"
_DEFAULT_LIMITS = Limits()

_SAFE_CODE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,127}$")
_SAFE_CLASS = re.compile(r"^[a-z0-9][a-z0-9-]{0,63}$")
_TRANSPORT_CODE_PREFIX = "build_repository_transport_"
_KNOWN_SAFE_CODES = frozenset(
    {
        CODE_ENDPOINT_UNAVAILABLE,
        CODE_POLICY_INVALID,
        repository_policy.CODE_ALIAS_UNKNOWN,
        repository_policy.CODE_MIRROR_UNDECLARED,
        git_admission.IDENTITY_INVALID,
        git_admission.SOURCE_UNAVAILABLE,
        git_admission.REF_MOVED,
        git_admission.INCOMPLETE_SOURCE,
        git_admission.OBJECT_SEMANTICS_INVALID,
        git_admission.SSH_CREDENTIAL_MISSING,
        git_admission.CREDENTIAL_POLICY_INVALID,
    }
)

# The policy plan is the attempt plan.  Keeping one type at this seam prevents
# a transport implementation from growing a second endpoint-selection path.
AttemptPlan = ResolutionPlan


class AttemptCallable(Protocol):
    def __call__(
        self,
        *,
        endpoint: ResolvedEndpoint,
        lock: LockedCommit,
        tool: GitTool | None,
        tag: str | None,
        limits: Limits,
    ) -> Snapshot: ...


class ToolProvider(Protocol):
    def __call__(self, endpoint: ResolvedEndpoint) -> GitTool: ...


def _call_tool_provider(
    provider: ToolProvider, endpoint: ResolvedEndpoint, limits: Limits
) -> GitTool:
    """Resolve one endpoint's broker with the remaining shared budget.

    The one-argument form is retained for existing callers and test seams.
    New manager providers may accept ``limits``, ``deadline`` or
    ``timeout_seconds``; inspection selects that form without catching a
    provider's own ``TypeError`` and accidentally retrying it.
    """

    try:
        parameters = inspect.signature(provider).parameters
    except (TypeError, ValueError):
        return provider(endpoint)
    if "limits" in parameters or any(
        parameter.kind is inspect.Parameter.VAR_KEYWORD
        for parameter in parameters.values()
    ):
        return provider(endpoint, limits=limits)  # type: ignore[call-arg]
    if "deadline" in parameters:
        return provider(endpoint, deadline=limits.deadline)  # type: ignore[call-arg]
    if "timeout_seconds" in parameters:
        return provider(
            endpoint, timeout_seconds=limits.timeout_seconds  # type: ignore[call-arg]
        )
    return provider(endpoint)


def _safe_class(value: object) -> str:
    if isinstance(value, str) and _SAFE_CLASS.fullmatch(value) is not None:
        return value
    return "unclassified"


def _safe_code(value: object, default: str) -> str:
    if isinstance(value, str) and _SAFE_CODE.fullmatch(value) is not None:
        if value in _KNOWN_SAFE_CODES:
            return value
        if value.startswith(_TRANSPORT_CODE_PREFIX) and _SAFE_CLASS.fullmatch(
            value.removeprefix(_TRANSPORT_CODE_PREFIX)
        ):
            return value
    return default


@dataclass(frozen=True)
class AttemptDiagnostic:
    """Machine-private, credential-free provenance for one attempt."""

    ordinal: int
    endpoint: EndpointProvenance
    classification: str
    code: str

    @property
    def failure_class(self) -> str:
        return self.classification

    def as_dict(self) -> dict[str, object]:
        """Return the sanitized diagnostic shape used by machine logs."""

        return {
            "attempt": self.ordinal,
            "endpoint": {
                "listed_url": self.endpoint.listed_url,
                "url_host": self.endpoint.url_host,
                "url_port": self.endpoint.url_port,
                "alias_port": self.endpoint.alias_port,
                "resolved_host": self.endpoint.resolved_host,
                "resolved_port": self.endpoint.resolved_port,
                "alias": self.endpoint.alias,
                "mirror_of": self.endpoint.mirror_of,
            },
            "classification": self.classification,
            "code": self.code,
        }


@dataclass(frozen=True)
class AcquisitionResult:
    """The verified snapshot and sanitized attempt evidence."""

    snapshot: Snapshot
    attempts: tuple[AttemptDiagnostic, ...]

    @property
    def attempt_count(self) -> int:
        return len(self.attempts)


class TransportError(GitAdmissionError):
    """A structured refusal produced by the transport boundary."""

    def __init__(
        self,
        code: str,
        detail: str,
        *,
        attempts: tuple[AttemptDiagnostic, ...] = (),
        public_detail: str | None = None,
    ) -> None:
        safe_code = _safe_code(code, "build_repository_transport_failure")
        # ``detail`` is intentionally not copied into the public exception.
        # Callers may have received an error string from a credential broker or
        # a Git process.  Diagnostics use stable classifications instead.
        message = f"transport refusal ({safe_code})"
        if public_detail:
            message += f": {public_detail}"
        super().__init__(safe_code, message)
        self.detail = message
        self.attempts = attempts
        self.raw_detail_present = bool(detail)


class TransportFailure(TransportError):
    """A single classified failure at the acquisition call site."""

    def __init__(
        self,
        failure_class: object,
        detail: str = "",
        *,
        code: str | None = None,
    ) -> None:
        safe_class = _safe_class(failure_class)
        self.original: GitAdmissionError | None = None
        super().__init__(_failure_code(safe_class, code), detail)
        self.failure_class = safe_class


class TransportResolutionError(TransportError):
    """The bounded endpoint list was exhausted without a verified snapshot."""

    def __init__(self, attempts: tuple[AttemptDiagnostic, ...]) -> None:
        self.diagnostics = attempts
        classes = ", ".join(
            f"attempt {item.ordinal}={item.classification}" for item in attempts
        ) or "no attempts"
        super().__init__(
            CODE_ENDPOINT_UNAVAILABLE,
            "repository endpoint unavailable",
            attempts=attempts,
            public_detail=(
                f"{classes}; remediation: verify the listed endpoint and its "
                "operator-selected authentication provider"
            ),
        )


def plan_attempts(
    identity: str,
    declaration: str | None = None,
    policy: RepositoryPolicy | None = None,
    *,
    declared_url: str | None = None,
    declared_authentication: str | None = None,
    revision: int = repository_policy.TRANSPORT_REVISION_V2,
) -> AttemptPlan:
    """Resolve one policy document into one immutable ordered attempt plan."""

    selected_url = declared_url if declared_url is not None else declaration
    return repository_policy.resolve_repository(
        policy,
        identity,
        declared_url=selected_url,
        declared_authentication=declared_authentication,
        revision=revision,
    )


def fallback_admits(
    fallback: object, failure_class: object, *, pinned: bool = False
) -> bool:
    """Expose the policy layer's fail-closed fallback decision."""

    return repository_policy.fallback_permitted(
        fallback, _safe_class(failure_class), pinned=pinned
    )


def _connection_target(endpoint: ResolvedEndpoint) -> git_admission.NetworkEndpoint:
    parsed = repository_policy.parse_endpoint_url(endpoint.url)
    default_port = 443 if parsed.transport == "https" else 22
    remote_url = endpoint.url
    generated_ssh_uri = False
    if endpoint.host != parsed.host or endpoint.port != (parsed.port or default_port):
        user = f"{parsed.username}@" if parsed.username is not None else ""
        path = parsed.path
        port = "" if endpoint.port == default_port else f":{endpoint.port}"
        if parsed.transport == "https":
            remote_url = f"https://{endpoint.host}{port}/{path}"
        elif parsed.url.startswith("ssh://") or endpoint.port != default_port:
            # URI spelling is the only way to carry a selected SSH port.  A
            # scp endpoint without a selected port keeps its relative path
            # semantics when only its host is replaced.
            remote_url = f"ssh://{user}{endpoint.host}{port}/{path}"
            generated_ssh_uri = True
        else:
            remote_url = f"{user}{endpoint.host}:{path}"
    ssh_host = (
        f"{parsed.username}@{endpoint.host}"
        if parsed.username is not None
        else endpoint.host
    )
    repository_path = parsed.path
    # ``parsed.transport`` is the literal ``ssh``/``https`` label; use the
    # spelling of the URL to decide whether Git expects a leading SSH slash.
    if parsed.transport == "ssh" and (
        endpoint.url.startswith("ssh://") or generated_ssh_uri
    ):
        repository_path = f"/{parsed.path}"
    git_port: int | None = None
    if parsed.transport == "ssh":
        # Git itself emits ``-p <port>`` only for an explicit SSH URI.  Keep
        # that syntactic fact separate from the resolved connection port so
        # the wrapper pins the exact argv shape without treating scp's
        # ``host:2222/repo`` path component as a port.
        remote_parsed = repository_policy.parse_endpoint_url(remote_url)
        git_port = remote_parsed.port
    return git_admission.NetworkEndpoint(
        listed_url=endpoint.url,
        remote_url=remote_url,
        identity=endpoint.identity,
        transport=endpoint.transport,
        host=endpoint.host,
        port=endpoint.port,
        ssh_host=ssh_host,
        repository_path=repository_path,
        connect_port=endpoint.port,
        git_port=git_port,
    )


def default_attempt(
    *,
    endpoint: ResolvedEndpoint,
    lock: LockedCommit,
    tool: GitTool | None,
    tag: str | None,
    limits: Limits,
) -> Snapshot:
    """Run exactly one attempt through the trusted Git admission lane."""

    if tool is None:
        raise TransportFailure(
            "identity",
            "a trusted Git tool is required",
            code=git_admission.IDENTITY_INVALID,
        )
    source = RepositorySource(
        git=endpoint.url,
        identity=endpoint.identity,
        transport=endpoint.transport,
    )
    try:
        return git_admission.acquire_network(
            source,
            lock,
            tool,
            tag=tag,
            limits=limits,
            connection=_connection_target(endpoint),
        )
    except GitAdmissionError as exc:
        failure_class = _classify_git_error(exc)
        code = _failure_code(failure_class, exc.code)
        failure = TransportFailure(
            failure_class,
            "trusted Git admission failed",
            code=code,
        )
        failure.original = _original_failure(exc, failure_class, code)
        raise failure from exc


def _classify_git_error(error: GitAdmissionError) -> str:
    classified = getattr(error, "failure_class", None)
    if isinstance(classified, str) and classified:
        return _safe_class(classified)
    if error.code in {
        git_admission.SSH_CREDENTIAL_MISSING,
        git_admission.CREDENTIAL_POLICY_INVALID,
    }:
        return "auth-unavailable"
    if error.code == git_admission.REF_MOVED:
        return "ref-moved"
    if error.code in {
        git_admission.OBJECT_SEMANTICS_INVALID,
        git_admission.INCOMPLETE_SOURCE,
    }:
        return "integrity"
    if error.code == git_admission.IDENTITY_INVALID:
        return "identity"
    return "unclassified"


def _failure_code(failure_class: str, code: str | None) -> str:
    """Keep fail-closed failures out of the pipeline's offline cache path."""

    if code is not None and not (
        code == git_admission.SOURCE_UNAVAILABLE
        and repository_policy.classify_failure(failure_class) == "forbidden"
    ):
        return code
    if repository_policy.classify_failure(failure_class) == "availability-auth":
        return git_admission.SOURCE_UNAVAILABLE
    return "build_repository_transport_" + failure_class.replace("-", "_")


def _original_failure(
    error: GitAdmissionError, failure_class: str, code: str
) -> GitAdmissionError:
    return GitAdmissionError(
        code,
        "trusted Git admission failed",
        failure_class=failure_class,
    )


def _normalise_failure(error: BaseException) -> TransportFailure:
    if isinstance(error, TransportFailure):
        return error
    if isinstance(error, GitAdmissionError):
        failure_class = _classify_git_error(error)
        code = _failure_code(failure_class, error.code)
        failure = TransportFailure(
            failure_class,
            "trusted Git admission failed",
            code=code,
        )
        failure.original = _original_failure(error, failure_class, code)
        return failure
    return TransportFailure(
        "unclassified",
        "attempt raised an unclassified exception",
        code="build_repository_transport_failure",
    )


def _raise_first_failure(failure: TransportFailure) -> None:
    if failure.original is not None:
        raise failure.original
    raise failure


def acquire_plan(
    plan: AttemptPlan,
    lock: LockedCommit,
    tool: GitTool | None = None,
    *,
    tag: str | None = None,
    limits: Limits = _DEFAULT_LIMITS,
    attempt: AttemptCallable | None = None,
    tool_for_endpoint: ToolProvider | None = None,
    lane: Literal["skillfile-source", "external-build"] = LANE_SKILLFILE,
    clock: Callable[[], float] = time.monotonic,
) -> AcquisitionResult:
    """Acquire from a resolved plan with one bounded attempt per endpoint.

    ``clock`` is the monotonic source for the one shared deadline.  It
    defaults to :func:`time.monotonic`, so every existing call site is
    unchanged; tests inject a driven clock to prove the total-bounding
    property without racing a wall clock.
    """

    if lane == LANE_EXTERNAL_BUILD:
        # Imported lazily to keep the pipeline independent from this transport
        # implementation while sharing one strict section-7 gate.
        from ..build_repository_pipeline import validate_external_build_endpoint

        for endpoint in plan.endpoints:
            validate_external_build_endpoint(endpoint)

    runner = default_attempt if attempt is None else attempt
    deadline = (
        limits.deadline
        if limits.deadline is not None
        else clock() + limits.timeout_seconds
    )
    limits = replace(limits, deadline=deadline)
    diagnostics: list[AttemptDiagnostic] = []
    maximum = min(plan.max_attempts, 2, len(plan.endpoints))
    for index, endpoint in enumerate(plan.endpoints[:maximum]):
        remaining = deadline - clock()
        if remaining <= 0:
            raise TransportResolutionError(tuple(diagnostics))
        attempt_limits = replace(limits, timeout_seconds=remaining)
        try:
            # Provider selection is part of the endpoint attempt.  It can
            # inspect operator-owned broker material, so it must be typed by
            # the same boundary as Git acquisition and receive the one
            # remaining deadline.
            selected_tool = (
                _call_tool_provider(tool_for_endpoint, endpoint, attempt_limits)
                if tool_for_endpoint is not None
                else tool
            )
            if deadline - clock() <= 0:
                raise GitAdmissionError(
                    git_admission.SOURCE_UNAVAILABLE,
                    "endpoint provider exceeded the transport deadline",
                    failure_class="timeout",
                )
            snapshot = runner(
                endpoint=endpoint,
                lock=lock,
                tool=selected_tool,
                tag=tag,
                limits=attempt_limits,
            )
            if clock() > deadline:
                raise TransportFailure(
                    "timeout",
                    "endpoint acquisition completed after the transport deadline",
                    code=git_admission.SOURCE_UNAVAILABLE,
                )
        except Exception as error:  # noqa: BLE001 - every lane failure is typed
            failure = _normalise_failure(error)
            failure_class = _safe_class(failure.failure_class)
            diagnostics.append(
                AttemptDiagnostic(
                    ordinal=index + 1,
                    endpoint=endpoint.provenance,
                    classification=failure_class,
                    code=_safe_code(failure.code, "build_repository_transport_failure"),
                )
            )
            alternate = plan.next_endpoint(
                failure_class,
                failed_index=index,
            )
            if alternate is not None and index == 0:
                continue
            if repository_policy.classify_failure(failure_class) == "forbidden":
                _raise_first_failure(failure)
            raise TransportResolutionError(tuple(diagnostics))
        diagnostics.append(
            AttemptDiagnostic(
                ordinal=index + 1,
                endpoint=endpoint.provenance,
                classification="success",
                code="ok",
            )
        )
        return AcquisitionResult(snapshot=snapshot, attempts=tuple(diagnostics))
    raise TransportResolutionError(tuple(diagnostics))


def acquire(
    plan_or_identity: AttemptPlan | str,
    lock: LockedCommit,
    tool: GitTool | None = None,
    *,
    policy: RepositoryPolicy | None = None,
    declaration: str | None = None,
    declared_url: str | None = None,
    declared_authentication: str | None = None,
    policy_path: Path | None = None,
    tag: str | None = None,
    limits: Limits = _DEFAULT_LIMITS,
    attempt: AttemptCallable | None = None,
    tool_for_endpoint: ToolProvider | None = None,
    lane: Literal["skillfile-source", "external-build"] = LANE_SKILLFILE,
) -> AcquisitionResult:
    """Resolve policy once, then perform bounded acquisition."""

    if isinstance(plan_or_identity, ResolutionPlan):
        plan = plan_or_identity
    else:
        selected_policy = policy
        try:
            if selected_policy is None and policy_path is not None:
                selected_policy = repository_policy.load_policy(
                    policy_path,
                    reader_revision=repository_policy.TRANSPORT_REVISION_V2,
                )
            plan = plan_attempts(
                plan_or_identity,
                declaration,
                selected_policy,
                declared_url=declared_url,
                declared_authentication=declared_authentication,
            )
        except repository_policy.RepositoryPolicyError as error:
            raise TransportError(
                error.code,
                "source policy could not produce an attempt plan",
            ) from error
        except (TypeError, ValueError) as error:
            raise TransportError(
                CODE_POLICY_INVALID,
                "source policy could not produce an attempt plan",
            ) from error
    return acquire_plan(
        plan,
        lock,
        tool,
        tag=tag,
        limits=limits,
        attempt=attempt,
        tool_for_endpoint=tool_for_endpoint,
        lane=lane,
    )


resolve_and_acquire = acquire
acquire_network = acquire
