"""Machine-local source audit (``source-audit-v1``) bound to the assurance gates.

A ``source-audit-v1`` record is a **binding, not a credential**: it carries
digests of a persisted complete audit report and of the trusted machine policy
the decision was taken under, and it authorizes nothing until both are
recomputed and re-validated at use time. A record that is well-formed and
internally consistent, over a report that is absent, unreadable, malformed or
mismatching, is refused. The report is always loaded from the machine-local
audit store under a path derived from the expected content hash, never from a
caller-supplied location, so a package cannot smuggle its own attestation.

This module binds the record to the existing gates without weakening them:

* the existing audit pipeline (findings, canary, operator pins, revocations);
* the existing registry evidence validator (exact name, canonical repository,
  commit and context hash), with freshness and revocation verdicts derived
  from a live ``audit_registry.resolve`` call;
* the existing receipt-3 build input digest for assurance bindings.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Final, Literal, TypeAlias, cast

from .. import audit_registry, hashing, install_marker, protocol_json
from .. import source_identity as source_identity_mod
from ..audit import canary as audit_canary
from ..audit import pipeline as audit_pipeline
from ..audit import policy as audit_policy
from ..audit import trust as audit_trust
from ..audit.model import Decision, Finding, TrustRecord
from ..builds import metadata as build_metadata
from ..config import GlobalConfig, RegistryConfig
from .errors import SourceError
from .package_identity import (
    LockedCommit,
    PackageIdentity,
    is_sha256_digest,
    parse_package_identity,
)

SCHEMA_VERSION: Final = 1
REPORT_SCHEMA_VERSION: Final = 1
STORE_NAMESPACE: Final = "source-audit-v1"

CODE_INVALID: Final = "source_audit_invalid"
CODE_CANARY_FAILED: Final = "source_audit_canary_failed"
CODE_REPORT_MISSING: Final = "source_audit_report_missing"
CODE_REPORT_UNREADABLE: Final = "source_audit_report_unreadable"
CODE_TRUST_UNREADABLE: Final = "source_audit_trust_unreadable"
CODE_REPORT_MALFORMED: Final = "source_audit_report_malformed"
CODE_EVIDENCE_MISMATCH: Final = "source_audit_evidence_mismatch"
CODE_POLICY_MISMATCH: Final = "source_audit_policy_mismatch"
CODE_BINDING_MISMATCH: Final = "source_audit_binding_mismatch"
CODE_DECISION_REFUSED: Final = "source_audit_decision_refused"
CODE_REGISTRY_UNATTESTED: Final = "source_audit_registry_unattested"
CODE_BUILD_INPUT_UNAVAILABLE: Final = "assurance_build_input_unavailable"
CODE_STORE_UNWRITABLE: Final = "source_audit_store_unwritable"

SourceAuditDecision: TypeAlias = Literal["allow", "warn", "block", "require_pin"]
DECISIONS: Final[tuple[str, ...]] = ("allow", "warn", "block", "require_pin")

_TIMESTAMP_RE: Final = re.compile(r"^[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}Z$")

_RECORD_MEMBERS: Final = frozenset(
    {
        "schema_version",
        "package",
        "content_sha256",
        "decision",
        "policy_sha256",
        "evidence_sha256",
        "created_at",
    }
)

_REPORT_MEMBERS: Final = frozenset(
    {
        "schema_version",
        "package",
        "content_sha256",
        "git",
        "decision",
        "report",
        "findings",
        "pins",
        "revocations",
        "script_policy",
        "assurance_policy",
        "canary_passed",
        "ran_at",
    }
)

_ASSURANCE_POLICY_MEMBERS: Final = frozenset({"mode", "fail_on", "backend", "registry_policy"})

_REPORT_SUMMARY_MEMBERS: Final = frozenset(
    {
        "scope",
        "skill",
        "source",
        "ref_kind",
        "ref",
        "commit",
        "schema_version",
        "ran_at",
    }
)

_PINS_MEMBERS: Final = frozenset({"pinned", "pinned_by", "reason"})


def _malformed(detail: str) -> SourceAuditError:
    return SourceAuditError(CODE_REPORT_MALFORMED, detail)


class SourceAuditError(ValueError):
    """One stable source-audit diagnostic with a machine-readable code."""

    def __init__(self, code: str, detail: str) -> None:
        super().__init__(f"{code}: {detail}")
        self.code = code
        self.detail = detail


def _invalid(detail: str) -> SourceAuditError:
    return SourceAuditError(CODE_INVALID, detail)


def _require_timestamp(value: object, subject: str) -> str:
    if not isinstance(value, str) or _TIMESTAMP_RE.fullmatch(value) is None:
        raise _invalid(f"{subject} is not a UTC second timestamp: {value!r}")
    try:
        datetime.strptime(value, "%Y-%m-%dT%H:%M:%SZ")
    except ValueError as exc:
        raise _invalid(f"{subject} is not a valid UTC timestamp: {value!r}") from exc
    return value


def _report_timestamp_to_spec(value: object) -> str:
    """Normalize one pipeline ``ran_at`` to the wire timestamp shape.

    The audit pipeline stamps reports with fractional seconds and a
    numeric UTC offset; the source-audit-v1 schema pins second-Z.
    The record boundary converts, refusing anything that is not a
    UTC-denoting timestamp. The stamp is evidence metadata, never
    identity input: no hash or store key derives from it.
    """

    if not isinstance(value, str):
        raise _invalid(f"audit report ran_at is not a string: {value!r}")
    text = value.strip()
    if text.endswith(("Z", "z")):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError as exc:
        raise _invalid(
            f"audit report ran_at is not a timestamp: {value!r}"
        ) from exc
    if parsed.tzinfo is None or parsed.utcoffset() != timedelta(0):
        raise _invalid(f"audit report ran_at is not UTC: {value!r}")
    return parsed.strftime("%Y-%m-%dT%H:%M:%SZ")


def _require_sha256(value: object, subject: str) -> str:
    if not is_sha256_digest(value):
        raise _invalid(f"{subject} is not a SHA-256 digest: {value!r}")
    assert isinstance(value, str)
    return value


# Trusted-policy enums, mirroring the global-config validation in
# ``csk.config`` exactly: a policy this module cannot construct is a policy the
# machine cannot hold, so unknown values refuse here rather than deciding
# silently as another member.
_AUDIT_MODES: Final = frozenset({"advisory", "strict"})
_FAIL_ONS: Final = frozenset({"off", "low", "medium", "high", "critical"})
_REGISTRY_POLICIES: Final = frozenset({"advisory", "strict"})


def _utc_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _normalize_revocation(item: object) -> str:
    """Normalize one policy revocation to its canonical identity.

    Content hashes normalize through the one shared normalizer, so prefixed
    and bare digests in any letter case denote one identity and carry no case
    into the policy digest; anything else that is not a ``source:`` pattern is
    refused as garbage. The global config admits upper-case digests and the
    revocation matcher normalizes case, so a policy bridged from a loadable
    config must be constructible for every such entry.
    """
    if not isinstance(item, str):
        raise SourceAuditError(
            CODE_INVALID,
            "source audit policy revocations must be strings",
        )
    if item.startswith("source:"):
        if not item.removeprefix("source:").strip():
            raise SourceAuditError(
                CODE_INVALID,
                "source audit policy has an empty source revocation pattern",
            )
        return item
    try:
        return audit_trust.normalize_content_sha256(item)
    except ValueError as exc:
        raise SourceAuditError(
            CODE_INVALID,
            f"source audit policy revocation is neither a source pattern nor a content hash: {item!r}",
        ) from exc


@dataclass(frozen=True, slots=True)
class SourceAuditRecord:
    """One parsed ``source-audit-v1`` binding record."""

    schema_version: int
    package: PackageIdentity
    content_sha256: str
    decision: SourceAuditDecision
    policy_sha256: str
    evidence_sha256: str
    created_at: str

    def to_json(self) -> dict[str, Any]:
        from .package_identity import package_identity_to_json

        return {
            "schema_version": self.schema_version,
            "package": package_identity_to_json(self.package),
            "content_sha256": self.content_sha256,
            "decision": self.decision,
            "policy_sha256": self.policy_sha256,
            "evidence_sha256": self.evidence_sha256,
            "created_at": self.created_at,
        }


def parse_source_audit(value: Any) -> SourceAuditRecord:
    """Parse one ``source-audit-v1`` record with hand-written validation.

    This is the production reader: it mirrors the draft schema exactly
    (required members, no unknown members, ``schema_version`` const 1, the
    four decisions, digest and timestamp shapes) and performs no I/O.
    Parsing never authorizes anything; only :func:`validate_source_audit`
    admits, and only after recomputation under current policy.
    """
    if not isinstance(value, dict):
        raise _invalid("source audit must be an object")
    if set(value) != _RECORD_MEMBERS:
        raise _invalid("source audit has unsupported or missing fields")
    schema_version = value["schema_version"]
    if not isinstance(schema_version, int) or isinstance(schema_version, bool):
        raise _invalid("source audit schema_version must be integer 1")
    if schema_version != SCHEMA_VERSION:
        raise _invalid(f"source audit schema_version must be 1, got {schema_version!r}")
    try:
        package = parse_package_identity(value["package"])
    except SourceError as exc:
        raise _invalid(f"source audit package is invalid: {exc.detail}") from exc
    content_sha256 = _require_sha256(value["content_sha256"], "source audit content_sha256")
    decision = value["decision"]
    if decision not in DECISIONS:
        raise _invalid(f"source audit decision must be one of {', '.join(DECISIONS)}, got {decision!r}")
    policy_sha256 = _require_sha256(value["policy_sha256"], "source audit policy_sha256")
    evidence_sha256 = _require_sha256(value["evidence_sha256"], "source audit evidence_sha256")
    created_at = _require_timestamp(value["created_at"], "source audit created_at")
    return SourceAuditRecord(
        schema_version=SCHEMA_VERSION,
        package=package,
        content_sha256=content_sha256,
        decision=decision,
        policy_sha256=policy_sha256,
        evidence_sha256=evidence_sha256,
        created_at=created_at,
    )


@dataclass(frozen=True, slots=True)
class SourceAuditPolicy:
    """The trusted machine policy a source-audit decision is taken under."""

    mode: str
    fail_on: str
    backend: str
    registry_policy: str
    revocations: tuple[str, ...]
    script_policy: str

    def __post_init__(self) -> None:
        if not isinstance(self.mode, str) or self.mode not in _AUDIT_MODES:
            raise SourceAuditError(
                CODE_INVALID,
                f"source audit policy mode must be advisory or strict, got {self.mode!r}",
            )
        if not isinstance(self.fail_on, str) or self.fail_on not in _FAIL_ONS:
            raise SourceAuditError(
                CODE_INVALID,
                "source audit policy fail_on must be off, low, medium, high or critical, "
                f"got {self.fail_on!r}",
            )
        if not isinstance(self.registry_policy, str) or self.registry_policy not in _REGISTRY_POLICIES:
            raise SourceAuditError(
                CODE_INVALID,
                f"source audit policy registry_policy must be advisory or strict, got {self.registry_policy!r}",
            )
        for field_name in ("backend", "script_policy"):
            value = getattr(self, field_name)
            if not isinstance(value, str) or not value:
                raise SourceAuditError(
                    CODE_INVALID,
                    f"source audit policy {field_name} must be a non-empty string",
                )
        # Duplicate spellings of one identity collapse here, so the digest is a
        # function of the revocation identity set, not of how it was spelled.
        object.__setattr__(
            self,
            "revocations",
            tuple(dict.fromkeys(_normalize_revocation(item) for item in self.revocations)),
        )

    def to_json(self) -> dict[str, Any]:
        return {
            "mode": self.mode,
            "fail_on": self.fail_on,
            "backend": self.backend,
            "registry_policy": self.registry_policy,
            "revocations": sorted(self.revocations),
            "script_policy": self.script_policy,
        }

    def digest(self) -> str:
        """Return the policy digest a record binds in ``policy_sha256``."""
        return "sha256:" + hashlib.sha256(protocol_json.canonical_bytes(self.to_json())).hexdigest()


def policy_from_config(config: GlobalConfig, *, script_policy: str) -> SourceAuditPolicy:
    """Build the current trusted machine policy from the global config."""
    return SourceAuditPolicy(
        mode=config.audit.mode,
        fail_on=config.audit.fail_on,
        backend=config.audit.backend,
        registry_policy=config.audit.registry_policy,
        revocations=tuple(config.audit.revocations),
        script_policy=script_policy,
    )


def source_audit_report_path(csk_home: Path, content_sha256: str) -> Path:
    """Return the derived store path of one persisted audit report.

    The path derives from the expected content hash alone. Validation never
    accepts a caller-supplied report location, so a package cannot redirect
    the binding at its own attestation.
    """
    if not is_sha256_digest(content_sha256):
        raise _invalid(f"report content hash is not a SHA-256 digest: {content_sha256!r}")
    component = content_sha256.replace(":", "-")
    return csk_home / "audit" / STORE_NAMESPACE / component / "report.json"


def evidence_digest(report_bytes: bytes) -> str:
    """Return the evidence digest of persisted report bytes.

    Identity is a function of the raw bytes: nothing normalizes before
    hashing, and the writer stores exactly the bytes the digest covers.
    """
    return "sha256:" + hashlib.sha256(report_bytes).hexdigest()


def _finding_to_payload(finding: Finding) -> dict[str, Any]:
    return audit_pipeline.finding_to_payload(finding)


def _require_ccj_text(value: object, subject: str) -> None:
    """Refuse a finding string that CCJ-1 cannot serialize, naming the field.

    Finding locations derive from real filenames and evidence from detector
    output, so a lone surrogate is a pipeline-produced input, not a caller
    bug; it refuses with a typed, field-naming diagnostic instead of escaping
    as a raw parser error.
    """
    if not isinstance(value, str):
        return
    try:
        value.encode("utf-8")
    except UnicodeEncodeError as exc:
        raise SourceAuditError(
            CODE_INVALID,
            f"source audit {subject} is not CCJ-1 encodable: {exc}",
        ) from exc


def _finding_from_payload(payload: Any) -> Finding:
    if not isinstance(payload, dict):
        raise SourceAuditError(
            CODE_REPORT_MALFORMED,
            "persisted audit report has a malformed finding: not an object",
        )
    try:
        return audit_trust.finding_from_payload(payload)
    except (AttributeError, KeyError, TypeError, ValueError) as exc:
        raise SourceAuditError(
            CODE_REPORT_MALFORMED,
            f"persisted audit report has a malformed finding: {exc}",
        ) from exc


@dataclass(frozen=True, slots=True)
class ValidatedSourceAudit:
    """One source audit admitted after recomputation under current policy."""

    package: PackageIdentity
    content_sha256: str
    decision: Decision
    findings: tuple[Finding, ...]
    evidence_sha256: str
    policy_sha256: str
    warnings: tuple[str, ...]


def record_source_audit(
    report: audit_pipeline.AuditReport,
    *,
    csk_home: Path,
    package: PackageIdentity,
    git: str | None,
    policy: SourceAuditPolicy,
    created_at: str | None = None,
) -> SourceAuditRecord:
    """Persist one complete audit report and bind a record to it.

    Evidence is the persisted complete existing audit report: the pipeline
    finding payloads, the pin state, the effective revocation list and the
    effective script and assurance policy labels. The record carries only
    digests of that evidence and of the policy; it authorizes nothing until
    :func:`validate_source_audit` recomputes both at use time. The
    ``canary_passed`` label records the static canary outcome observed here, at
    record time; use-time validation requires it to be true.
    """
    if report.decision == Decision.CONFIRM:
        raise SourceAuditError(
            CODE_INVALID,
            "source audit cannot bind a confirm decision: re-audit to a terminal decision",
        )
    try:
        decision = Decision(report.decision.value)
    except ValueError as exc:
        raise _invalid(f"audit report decision is not bindable: {report.decision!r}") from exc
    if decision.value not in DECISIONS:
        raise _invalid(f"audit report decision is not bindable: {report.decision!r}")
    record_decision = cast(SourceAuditDecision, decision.value)
    for index, finding in enumerate(report.findings):
        if finding.location is not None:
            _require_ccj_text(finding.location.file, f"finding[{index}].location")
        _require_ccj_text(finding.evidence, f"finding[{index}].evidence")
    ran_at = _report_timestamp_to_spec(report.ran_at)
    envelope: dict[str, Any] = {
        "schema_version": REPORT_SCHEMA_VERSION,
        "package": package.to_json(),
        "content_sha256": report.content_sha256,
        "git": git,
        "decision": decision.value,
        "report": {
            "scope": report.scope,
            "skill": report.skill,
            "source": report.source,
            "ref_kind": report.ref_kind,
            "ref": report.ref,
            "commit": report.commit,
            "schema_version": report.schema_version,
            "ran_at": ran_at,
        },
        "findings": [_finding_to_payload(finding) for finding in report.findings],
        "pins": {
            "pinned": report.trust.pinned,
            "pinned_by": report.trust.pinned_by,
            "reason": report.trust.reason,
        },
        "revocations": sorted(policy.revocations),
        "script_policy": policy.script_policy,
        "assurance_policy": {
            "mode": policy.mode,
            "fail_on": policy.fail_on,
            "backend": policy.backend,
            "registry_policy": policy.registry_policy,
        },
        "canary_passed": audit_canary.run_static_canary(),
        "ran_at": ran_at,
    }
    try:
        raw = protocol_json.canonical_bytes(envelope)
    except protocol_json.ProtocolJSONError as exc:
        raise SourceAuditError(
            CODE_INVALID,
            f"source audit report envelope is not CCJ-1 encodable: {exc}",
        ) from exc
    stamped = created_at if created_at is not None else _utc_now()
    _require_timestamp(stamped, "source audit created_at")
    record = SourceAuditRecord(
        schema_version=SCHEMA_VERSION,
        package=package,
        content_sha256=report.content_sha256,
        decision=record_decision,
        policy_sha256=policy.digest(),
        evidence_sha256=evidence_digest(raw),
        created_at=stamped,
    )
    path = source_audit_report_path(csk_home, report.content_sha256)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(raw)
    except OSError as exc:
        raise SourceAuditError(
            CODE_STORE_UNWRITABLE,
            f"persisted audit report store is not writable for {report.content_sha256}: {path}: {exc}",
        ) from exc
    return record


def _read_report_bytes(csk_home: Path, content_sha256: str) -> bytes:
    path = source_audit_report_path(csk_home, content_sha256)
    try:
        return path.read_bytes()
    except FileNotFoundError as exc:
        # A dangling link reads as ENOENT but the entry exists: a failure,
        # never an absence.
        try:
            path.lstat()
        except FileNotFoundError:
            raise SourceAuditError(
                CODE_REPORT_MISSING,
                f"persisted audit report is absent for {content_sha256}: {path}",
            ) from exc
        except OSError as lstat_exc:
            raise SourceAuditError(
                CODE_REPORT_UNREADABLE,
                f"persisted audit report is unreadable for {content_sha256}: {path}: {lstat_exc}",
            ) from lstat_exc
        raise SourceAuditError(
            CODE_REPORT_UNREADABLE,
            f"persisted audit report is unreadable for {content_sha256}: {path}: dangling link",
        ) from exc
    except OSError as exc:
        raise SourceAuditError(
            CODE_REPORT_UNREADABLE,
            f"persisted audit report is unreadable for {content_sha256}: {path}: {exc}",
        ) from exc


def _parse_report_envelope(raw: bytes) -> dict[str, Any]:
    try:
        value = protocol_json.loads_canonical(raw)
    except protocol_json.ProtocolJSONError as exc:
        raise SourceAuditError(
            CODE_REPORT_MALFORMED,
            f"persisted audit report is malformed: {exc}",
        ) from exc
    if not isinstance(value, dict) or set(value) != _REPORT_MEMBERS:
        raise _malformed("persisted audit report has unsupported or missing fields")
    if value["schema_version"] != REPORT_SCHEMA_VERSION or isinstance(value["schema_version"], bool):
        raise _malformed(
            f"persisted audit report schema_version must be {REPORT_SCHEMA_VERSION}",
        )
    assurance = value["assurance_policy"]
    if (
        not isinstance(assurance, dict)
        or set(assurance) != _ASSURANCE_POLICY_MEMBERS
        or any(not isinstance(item, str) for item in assurance.values())
    ):
        raise _malformed("persisted audit report has a malformed assurance policy")
    if not isinstance(value["revocations"], list) or any(
        not isinstance(item, str) for item in value["revocations"]
    ):
        raise _malformed("persisted audit report has a malformed revocation list")
    if value["decision"] not in DECISIONS:
        raise _malformed(
            f"persisted audit report has a malformed decision: {value['decision']!r}",
        )
    if not isinstance(value["canary_passed"], bool):
        raise _malformed("persisted audit report has a malformed canary flag")
    try:
        _require_timestamp(value["ran_at"], "persisted audit report ran_at")
    except SourceAuditError as exc:
        raise _malformed(exc.detail) from exc
    if not is_sha256_digest(value["content_sha256"]):
        raise _malformed("persisted audit report has a malformed content hash")
    if not isinstance(value["findings"], list):
        raise _malformed("persisted audit report has malformed findings: not a list")
    pins = value["pins"]
    if (
        not isinstance(pins, dict)
        or set(pins) != _PINS_MEMBERS
        or not isinstance(pins["pinned"], bool)
        or (pins["pinned_by"] is not None and not isinstance(pins["pinned_by"], str))
        or (pins["reason"] is not None and not isinstance(pins["reason"], str))
    ):
        raise _malformed("persisted audit report has malformed pins")
    summary = value["report"]
    if not isinstance(summary, dict) or set(summary) != _REPORT_SUMMARY_MEMBERS:
        raise _malformed("persisted audit report has a malformed report summary")
    if any(not isinstance(summary[key], str) for key in _REPORT_SUMMARY_MEMBERS - {"schema_version"}):
        raise _malformed("persisted audit report has a malformed report summary")
    if not isinstance(summary["schema_version"], int) or isinstance(summary["schema_version"], bool):
        raise _malformed("persisted audit report has a malformed skill schema version")
    try:
        _require_timestamp(summary["ran_at"], "persisted audit report summary ran_at")
    except SourceAuditError as exc:
        raise _malformed(exc.detail) from exc
    if not isinstance(value["script_policy"], str) or not value["script_policy"]:
        raise _malformed("persisted audit report has a malformed script policy")
    git = value["git"]
    if git is not None and not isinstance(git, str):
        raise _malformed("persisted audit report has a malformed git identity")
    return value


def _load_trust_record(csk_home: Path, content_sha256: str) -> TrustRecord:
    """Load the pin record, converting an unreadable store to a refusal.

    The one wrapper over both ``load_trust_record`` call sites (the plan
    hook reaches it through :func:`validate_stored_report`): absent stays
    "no pin" inside the reader, while an unreadable trust record refuses
    here with the trust path named.
    """
    try:
        return audit_trust.load_trust_record(csk_home, content_sha256)
    except audit_trust.TrustRecordError as exc:
        raise SourceAuditError(
            CODE_TRUST_UNREADABLE,
            f"persisted trust record is unreadable for {content_sha256}: {exc.detail}",
        ) from exc


def validate_stored_report(
    *,
    package: PackageIdentity,
    content_sha256: str,
    csk_home: Path,
    policy: SourceAuditPolicy,
) -> ValidatedSourceAudit:
    """Validate the machine-stored audit report for one package install.

    The report loads from the derived store path only; a missing, unreadable
    or mismatching report fails, and the decision is recomputed under the
    current trusted machine policy: fresh static canary, current revocation
    list, current pin state, current mode and fail_on. Every persisted label
    the recomputation does not re-derive (the backend, the registry policy,
    the script policy and the record-time canary outcome) must instead match
    the current policy exactly, so this path refuses the same stale-policy
    class the record path refuses. A pin satisfies only the require_pin
    outcome the pipeline already authorizes; it never satisfies canary,
    revocation or a required decision.
    """
    if not audit_canary.run_static_canary():
        raise SourceAuditError(
            CODE_CANARY_FAILED,
            "source audit refused: static audit canary failed",
        )
    if not is_sha256_digest(content_sha256):
        raise _invalid(f"expected content hash is not a SHA-256 digest: {content_sha256!r}")
    raw = _read_report_bytes(csk_home, content_sha256)
    envelope = _parse_report_envelope(raw)
    try:
        stored_package = parse_package_identity(envelope["package"])
    except SourceError as exc:
        raise SourceAuditError(
            CODE_REPORT_MALFORMED,
            f"persisted audit report has a malformed package: {exc.detail}",
        ) from exc
    if stored_package != package or envelope["content_sha256"] != content_sha256:
        raise SourceAuditError(
            CODE_BINDING_MISMATCH,
            "persisted audit report does not bind the expected package and content",
        )
    assurance = envelope["assurance_policy"]
    assert isinstance(assurance, dict)
    if (
        assurance["backend"] != policy.backend
        or assurance["registry_policy"] != policy.registry_policy
        or envelope["script_policy"] != policy.script_policy
    ):
        raise SourceAuditError(
            CODE_POLICY_MISMATCH,
            "persisted audit report was taken under a different machine policy",
        )
    if envelope["canary_passed"] is not True:
        raise SourceAuditError(
            CODE_CANARY_FAILED,
            "persisted audit report records a failed audit canary",
        )
    findings = tuple(_finding_from_payload(item) for item in envelope["findings"])
    summary = envelope["report"]
    assert isinstance(summary, dict)
    try:
        revocation = audit_pipeline.revocation_reason_for(
            revocations=policy.revocations,
            content_sha256=content_sha256,
            source=summary["source"],
            git=envelope["git"],
        )
    except SourceAuditError:
        raise
    except ValueError as exc:
        # Only the stored source strings can fail here (the content hash and
        # the policy entries are validated before use): a source the matcher
        # cannot parse is malformed store data, never a raw crash.
        raise SourceAuditError(
            CODE_REPORT_MALFORMED,
            f"persisted audit report has a malformed source identity: {exc}",
        ) from exc
    if revocation is not None:
        raise SourceAuditError(
            CODE_DECISION_REFUSED,
            f"source audit refused: {revocation} is revoked",
        )
    trust = _load_trust_record(csk_home, content_sha256)
    if policy.mode == "strict" and summary["schema_version"] < 3 and not trust.pinned:
        raise SourceAuditError(
            CODE_DECISION_REFUSED,
            "source audit refused: strict policy requires a pin for this content",
        )
    recomputed = audit_policy.decide(findings, mode=policy.mode, fail_on=policy.fail_on)
    if recomputed == Decision.BLOCK:
        raise SourceAuditError(
            CODE_DECISION_REFUSED,
            "source audit refused: recomputed decision under current policy is block",
        )
    stored_decision = Decision(envelope["decision"])
    if stored_decision == Decision.BLOCK:
        raise SourceAuditError(
            CODE_DECISION_REFUSED,
            "source audit refused: persisted decision is block",
        )
    warnings: list[str] = []
    if recomputed == Decision.WARN or stored_decision == Decision.WARN:
        warnings.append("source audit admitted with warnings")
    return ValidatedSourceAudit(
        package=package,
        content_sha256=content_sha256,
        decision=recomputed,
        findings=findings,
        evidence_sha256=evidence_digest(raw),
        policy_sha256=policy.digest(),
        warnings=tuple(warnings),
    )


def validate_source_audit(
    record: SourceAuditRecord,
    *,
    csk_home: Path,
    policy: SourceAuditPolicy,
    expected_package: PackageIdentity,
    expected_content_sha256: str,
) -> ValidatedSourceAudit:
    """Admit one install only after recomputing the record's binding.

    The record is checked against the expected package and content, the
    machine-stored report is loaded and re-validated under the current
    trusted machine policy, and the record's evidence and policy digests
    must equal the recomputed values. A record over an absent, unreadable,
    malformed or mismatching report is refused; the object is never a
    self-authorizing attestation.
    """
    if record.package != expected_package or record.content_sha256 != expected_content_sha256:
        raise SourceAuditError(
            CODE_BINDING_MISMATCH,
            "source audit record does not bind the expected package and content",
        )
    validated = validate_stored_report(
        package=expected_package,
        content_sha256=expected_content_sha256,
        csk_home=csk_home,
        policy=policy,
    )
    if record.evidence_sha256 != validated.evidence_sha256:
        raise SourceAuditError(
            CODE_EVIDENCE_MISMATCH,
            "source audit record evidence digest does not match the persisted report",
        )
    if record.policy_sha256 != validated.policy_sha256:
        raise SourceAuditError(
            CODE_POLICY_MISMATCH,
            "source audit record was taken under a different machine policy",
        )
    if record.decision != validated.decision.value and not (
        record.decision == "require_pin"
        and validated.decision in (Decision.ALLOW, Decision.WARN)
    ):
        raise SourceAuditError(
            CODE_BINDING_MISMATCH,
            "source audit record decision does not match the persisted report",
        )
    if record.decision == "block":
        raise SourceAuditError(
            CODE_DECISION_REFUSED,
            "source audit refused: record decision is block",
        )
    if record.decision == "require_pin":
        trust = _load_trust_record(csk_home, expected_content_sha256)
        if not trust.pinned:
            raise SourceAuditError(
                CODE_DECISION_REFUSED,
                "source audit refused: record requires a pin that is not present",
            )
    return validated


@dataclass(frozen=True, slots=True)
class AdmittedRegistryEvidence:
    """One registry evidence record admitted with its live verdicts.

    ``attestation`` is the live attestation summary the marker writer needs
    (registry name, status and key id); it travels with the verdict so the
    wiring never re-resolves to recover it.
    """

    evidence: install_marker.RegistryEvidence
    warnings: tuple[str, ...]
    attestation: audit_registry.Attestation


def _live_record_for_attestation(attestation: audit_registry.Attestation) -> audit_registry.Record:
    """Re-parse the live record behind one attestation for member comparison."""
    try:
        return audit_registry.parse_record(attestation.record)
    except audit_registry.RegistryError as exc:
        raise SourceAuditError(
            CODE_REGISTRY_UNATTESTED,
            f"live registry attestation is malformed: {exc}",
        ) from exc


def _require_live_record_match(
    evidence: install_marker.RegistryEvidence,
    record: audit_registry.Record,
    key_id: str | None,
) -> None:
    """Require the evidence to name the live registry record exactly.

    The live record is the independent side: it comes from the registries,
    not from the caller. Every member of the evidence — name, canonical
    repository, commit, context hash and key id — must name that record.
    Content and context are one quantity here: the registry is queried with
    the section-8 content hash and the evidence carries it as the context
    hash. Both sides (validated canonical by ``parse_record`` and
    ``RegistryEvidence``) are already canonical, so exact equality is the
    whole check, key absence included.
    """
    if evidence.name != record.name:
        raise SourceAuditError(
            CODE_REGISTRY_UNATTESTED,
            f"registry evidence name {evidence.name!r} is not attested by the live record for {record.name!r}",
        )
    if evidence.repository != record.source_identity:
        raise SourceAuditError(
            CODE_REGISTRY_UNATTESTED,
            "registry evidence repository is not attested by the live record "
            f"for {record.source_identity!r}",
        )
    if evidence.commit.hex != record.commit:
        raise SourceAuditError(
            CODE_REGISTRY_UNATTESTED,
            "registry evidence commit is not attested by the live record",
        )
    if evidence.context_sha256 != record.content_sha256:
        raise SourceAuditError(
            CODE_REGISTRY_UNATTESTED,
            "registry evidence context hash is not attested by the live record",
        )
    if evidence.key_id != key_id:
        raise SourceAuditError(
            CODE_REGISTRY_UNATTESTED,
            "registry evidence key id is not attested by the live record, including key absence",
        )


def admit_network_git_evidence(
    evidence_path: Path,
    expectation: install_marker.AttestationExpectation,
    *,
    registries: tuple[RegistryConfig, ...],
    fetch: audit_registry.FetchFn,
    excluded_registry_urls: frozenset[str] = frozenset(),
) -> AdmittedRegistryEvidence:
    """Admit network-git registry evidence on exact match plus live backing.

    The one assurance binding for network members: a live
    ``audit_registry.resolve`` over the non-excluded registries derives the
    freshness and revocation verdicts (signature trust included, deny-wins),
    and the existing evidence validator then requires the exact name,
    canonical repository, commit and context hash. Evidence without a live
    attestation is refused, not downgraded.

    The live query identity derives from the expectation alone — repository,
    commit and context hash — so the query cannot be pointed at another
    artifact than the one the evidence must name. After the validator admits
    the evidence against the expectation, the evidence must additionally name
    the live record exactly (name, canonical repository, commit, context hash
    and key id), which binds the caller-supplied pair to the
    registry-supplied record.

    Freshness is deny-wins over every live URL carrying the attestation's
    registry name: registry names are not unique in a loadable config, so
    consulting one URL would make the verdict depend on config order. The
    evidence is fresh only when none of the name's URLs served stale data.
    """
    if not source_identity_mod.is_canonical_source_identity(expectation.repository):
        raise SourceAuditError(
            CODE_INVALID,
            "registry evidence expectation repository is not a canonical source identity: "
            f"{expectation.repository!r}",
        )
    if not isinstance(expectation.commit, LockedCommit):
        raise SourceAuditError(
            CODE_INVALID,
            f"registry evidence expectation commit is not a LockedCommit: {expectation.commit!r}",
        )
    if not is_sha256_digest(expectation.context_sha256):
        raise _invalid(
            "registry evidence expectation context hash is not a SHA-256 digest: "
            f"{expectation.context_sha256!r}"
        )
    live = tuple(
        registry
        for registry in registries
        if registry.enabled and registry.url not in excluded_registry_urls
    )
    resolution = audit_registry.resolve(
        live,
        source_identity=expectation.repository,
        commit=expectation.commit.hex,
        content_sha256=expectation.context_sha256,
        fetch=fetch,
    )
    if resolution.result == audit_registry.RESULT_UNKNOWN or resolution.attestation is None:
        raise SourceAuditError(
            CODE_REGISTRY_UNATTESTED,
            "no trusted registry attests this artifact; required evidence is unbacked",
        )
    revoked = resolution.result == audit_registry.RESULT_REVOKED
    urls = [registry.url for registry in live if registry.name == resolution.attestation.registry]
    stale_urls: Any = getattr(fetch, "stale_urls", set())
    fresh = (
        bool(urls)
        and isinstance(stale_urls, set)
        and all(url not in stale_urls for url in urls)
    )
    evidence = install_marker.validate_attestation_evidence(
        evidence_path,
        expectation,
        evidence_fresh=fresh,
        evidence_revoked=revoked,
    )
    live_record = _live_record_for_attestation(resolution.attestation)
    _require_live_record_match(evidence, live_record, resolution.attestation.key_id)
    warnings = list(resolution.warnings)
    if resolution.result == audit_registry.RESULT_DEPRECATED:
        warnings.append(f"registry: {live_record.name} is marked deprecated")
    return AdmittedRegistryEvidence(
        evidence=evidence,
        warnings=tuple(warnings),
        attestation=resolution.attestation,
    )


def bind_assurance_build_input(receipt_bytes: bytes | None, *, context_sha256: str) -> str:
    """Bind the exact receipt-3 build input digest for assurance artifacts.

    Assurance permits, execution receipts and checkpoints carry the exact
    receipt-3 build input digest in ``build_input_sha256``: SHA-256 over
    CCJ-1 of the whole wrapped input, recomputed here, never copied. The
    context hash is accepted only so the refusal can name it: an
    implementation state unable to bind the receipt input rejects execution
    rather than downgrading to the context-only hash.
    """
    if receipt_bytes is None:
        raise SourceAuditError(
            CODE_BUILD_INPUT_UNAVAILABLE,
            "cannot bind assurance build input: receipt is absent; "
            f"context {context_sha256} cannot substitute for the exact receipt-3 build input",
        )
    try:
        receipt = build_metadata.read_receipt_v3(receipt_bytes)
    except build_metadata.BuildMetadataError as exc:
        raise SourceAuditError(
            CODE_BUILD_INPUT_UNAVAILABLE,
            "cannot bind assurance build input: receipt is not a valid receipt-3; "
            f"context {context_sha256} cannot substitute for the exact receipt-3 build input",
        ) from exc
    return build_metadata.source_aware_cache_key(receipt.input)


def _provider_content_sha256(provider: Any) -> str:
    """Recompute the section-8 content hash over the provider's frozen tree.

    The content identity is observed from the tree the build is planned over,
    never taken from the package claim: the local-snapshot inventory digest
    and the pipeline content hash are different functions, and revocation and
    pins are keyed by the latter. A tree that cannot be hashed refuses the
    plan instead of falling back to the claimed digest.
    """
    try:
        return hashing.content_sha256(provider.snapshot.path)
    except hashing.HashingError as exc:
        raise SourceAuditError(
            CODE_INVALID,
            f"source audit cannot bind the content of provider {provider.name!r}: {exc}",
        ) from exc
    except OSError as exc:
        raise SourceAuditError(
            CODE_INVALID,
            f"source audit cannot read the tree of provider {provider.name!r}: {exc}",
        ) from exc


def source_audit_plan_hook(
    providers: tuple[Any, ...],
    *,
    csk_home: Path,
    policy: SourceAuditPolicy,
) -> None:
    """Audit every local provider before cache reads and compiler execution.

    This hook runs as the ``audit`` argument of ``builds.planner.plan_builds``,
    which invokes it once over the whole active provider set before any
    toolchain probe or cache read, so a refusal structurally precedes cache
    reads, compiler execution and publication. Legacy providers without a
    package stay with the pipeline audit gate; network packages use registry
    evidence instead of this hook. The package identity comes from the
    provider claim and the content hash is recomputed over the frozen tree;
    the stored report must bind both.
    """
    from .package_identity import LocalSnapshot

    for provider in providers:
        package = provider.package
        if package is None:
            continue
        if not isinstance(package, LocalSnapshot):
            raise SourceAuditError(
                CODE_INVALID,
                "source audit plan hook serves local-snapshot packages only; "
                "network-git packages use registry evidence",
            )
        validate_stored_report(
            package=package,
            content_sha256=_provider_content_sha256(provider),
            csk_home=csk_home,
            policy=policy,
        )
