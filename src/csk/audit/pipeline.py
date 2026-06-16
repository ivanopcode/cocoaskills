from __future__ import annotations

import fnmatch
import json
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from .. import hashing, installer
from ..config import GlobalConfig

from . import backend_config, canary, detectors, policy, redaction, serialization, trust
from .backends.base import AuditBackendError, AuditCanaryError, AuditEgressError, AuditRequest
from .backends.command_backend import CommandBackend
from .backends.null_backend import NullBackend
from .model import Decision, Finding, Location, Severity, Surface, TrustRecord, Verdict
from .source_policy import normalize_source


@dataclass(frozen=True)
class AuditReport:
    scope: str
    skill: str
    source: str
    ref_kind: str
    ref: str
    commit: str
    schema_version: int
    source_file: str | None
    runtime_roots: tuple[str, ...]
    content_sha256: str
    findings: tuple[Finding, ...]
    decision: Decision
    ran_at: str
    cache_hit: bool = False
    trust: TrustRecord = TrustRecord()
    revoked: bool = False
    revocation: str | None = None


@dataclass(frozen=True)
class GateResult:
    reports: tuple[AuditReport, ...]
    warnings: tuple[str, ...] = ()
    errors: tuple[str, ...] = ()

    @property
    def blocked(self) -> bool:
        return bool(self.errors)


def audit_plans(
    plans: list[installer.SkillPlan],
    config: GlobalConfig,
    *,
    scope: str,
    record: bool = True,
) -> tuple[AuditReport, ...]:
    reports: list[AuditReport] = []
    ran_at = datetime.now(timezone.utc).isoformat()
    static_canary_passed: bool | None = None
    backend_canary_passed: bool | None = None
    for plan in plans:
        content_sha256 = hashing.content_sha256(plan.snapshot)
        trust_record = trust.load_trust_record(config.path.parent, content_sha256)
        revocation = _revocation_reason(config, content_sha256, plan.decl.source, plan.decl.git)
        resolved_backend = _backend_config_for_config(config)
        backend = _backend_for_config(config)
        cached = trust.load_cached_verdict(
            config.path.parent,
            content_sha256,
            backend.name,
            resolved_backend.model,
            trust.PROMPT_VERSION,
            trust.RULESET_VERSION,
        )
        if cached is not None:
            reports.append(
                _report_from_verdict(
                    plan,
                    cached,
                    config,
                    trust_record,
                    revocation=revocation,
                    scope=scope,
                    cache_hit=True,
                )
            )
            continue

        if static_canary_passed is None:
            static_canary_passed = canary.run_static_canary()
        if not static_canary_passed:
            raise AuditCanaryError("Static audit canary failed; audit detectors are not producing expected findings")
        static_findings = detectors.detect_snapshot(plan.snapshot, plan.spec.capabilities)
        _check_cloud_egress(config, resolved_backend, plan)
        request = _build_request(plan, static_findings, backend.cloud, content_sha256)
        request_size = _request_size(request)
        if request_size > config.audit.max_request_bytes:
            backend_findings = ()
            findings = request.static_findings + (
                _too_large_finding(request_size, config.audit.max_request_bytes),
            )
        else:
            if not backend.is_available():
                raise AuditBackendError(f"Audit backend is unavailable: {backend.name}")
            if backend_canary_passed is None:
                backend_canary_passed = backend.run_canary()
            if not backend_canary_passed:
                raise AuditCanaryError(f"Audit backend failed canary check: {backend.name}")
            backend_findings = backend.extract(
                request,
                timeout=resolved_backend.timeout_seconds,
            )
            findings = request.static_findings + tuple(backend_findings)
        decision = _decide(plan, config, findings, trust_record, content_sha256, revocation)
        verdict = Verdict(
            schema_version=trust.SCHEMA_VERSION,
            content_sha256=content_sha256,
            skill=plan.decl.name,
            source=plan.decl.source,
            commit=plan.resolved.commit,
            backend=backend.name,
            model=resolved_backend.model,
            cloud=backend.cloud,
            prompt_version=trust.PROMPT_VERSION,
            ruleset_version=trust.RULESET_VERSION,
            canary_passed=True,
            findings=findings,
            decision=decision,
            ran_at=ran_at,
            trust=trust_record,
        )
        if record:
            trust.store_verdict(config.path.parent, verdict)
        reports.append(
            AuditReport(
                scope=scope,
                skill=plan.decl.name,
                source=plan.decl.source,
                ref_kind=plan.resolved.kind,
                ref=plan.resolved.ref,
                commit=plan.resolved.commit,
                schema_version=plan.spec.schema_version,
                source_file=plan.spec.source_file,
                runtime_roots=plan.spec.runtime_roots,
                content_sha256=content_sha256,
                findings=findings,
                decision=decision,
                ran_at=ran_at,
                cache_hit=False,
                trust=trust_record,
                revoked=revocation is not None,
                revocation=revocation,
            )
        )
    return tuple(reports)


def gate_plans(
    plans: list[installer.SkillPlan],
    config: GlobalConfig,
    *,
    scope: str,
    record: bool = True,
) -> GateResult:
    if not config.audit.enabled:
        return GateResult(reports=())
    try:
        reports = audit_plans(plans, config, scope=scope, record=record)
    except AuditCanaryError as exc:
        return GateResult(reports=(), errors=(f"audit blocked: audit canary failed: {exc}",))
    except AuditEgressError as exc:
        return GateResult(reports=(), errors=(f"audit blocked: {exc}",))
    except AuditBackendError as exc:
        message = f"audit backend failed: {exc}"
        if config.audit.mode == "strict":
            return GateResult(reports=(), errors=(f"audit blocked: {message}",))
        return GateResult(reports=(), warnings=(f"audit warning: {message}; proceeding without audit",))
    warnings: list[str] = []
    errors: list[str] = []
    for report in reports:
        if report.decision == Decision.ALLOW:
            continue
        messages = _gate_messages(report)
        if report.decision in {Decision.BLOCK, Decision.REQUIRE_PIN}:
            errors.extend(messages)
        else:
            warnings.extend(messages)
    return GateResult(reports=reports, warnings=tuple(warnings), errors=tuple(errors))


def reports_to_payload(reports: tuple[AuditReport, ...]) -> dict[str, Any]:
    return {
        "schema_version": 1,
        "reports": [_report_to_payload(report) for report in reports],
    }


def render_reports(reports: tuple[AuditReport, ...]) -> str:
    if not reports:
        return "No skills audited."
    lines: list[str] = []
    for report in reports:
        lines.append(
            f"{report.scope}: {report.skill} {report.ref_kind} {report.ref} "
            f"{report.commit[:7]} {report.decision.value} ({len(report.findings)} finding(s))"
        )
        for finding in report.findings:
            location = ""
            if finding.location is not None:
                line = finding.location.span[0] if finding.location.span else 1
                location = f" {finding.location.file}:{line}"
            evidence = redaction.scrub_text(finding.evidence)
            lines.append(f"  {finding.severity.value:<8} {finding.id}{location} - {evidence}")
    return "\n".join(lines)


def _report_to_payload(report: AuditReport) -> dict[str, Any]:
    return {
        "scope": report.scope,
        "skill": report.skill,
        "source": report.source,
        "ref_kind": report.ref_kind,
        "ref": report.ref,
        "commit": report.commit,
        "schema_version": report.schema_version,
        "source_file": report.source_file,
        "runtime_roots": list(report.runtime_roots),
        "content_sha256": report.content_sha256,
        "decision": report.decision.value,
        "ran_at": report.ran_at,
        "cache_hit": report.cache_hit,
        "revoked": report.revoked,
        "revocation": report.revocation,
        "trust": {
            "pinned": report.trust.pinned,
            "pinned_by": report.trust.pinned_by,
            "reason": report.trust.reason,
        },
        "findings": [_finding_to_payload(finding) for finding in report.findings],
    }


def _finding_to_payload(finding: Finding) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "id": finding.id,
        "surface": finding.surface.value,
        "category": finding.category,
        "severity": finding.severity.value,
        "evidence": redaction.scrub_text(finding.evidence),
        "detector": finding.detector,
        "confidence": finding.confidence,
        "verifiable": finding.verifiable,
    }
    if finding.location is not None:
        payload["location"] = {
            "file": finding.location.file,
            "span": list(finding.location.span) if finding.location.span else None,
        }
    if finding.capability_violation is not None:
        payload["capability_violation"] = {
            "capability": finding.capability_violation.capability,
            "declared": finding.capability_violation.declared,
            "observed": finding.capability_violation.observed,
        }
    return payload


def _backend_config_for_config(config: GlobalConfig) -> backend_config.BackendConfig:
    try:
        return backend_config.resolve_backend_config(
            config.audit.backend,
            config.audit.backends,
            global_model=config.audit.model,
            allow_cloud=config.audit.allow_cloud,
        )
    except backend_config.BackendConfigError as exc:
        raise AuditBackendError(str(exc)) from exc


def _backend_for_config(config: GlobalConfig):
    resolved = _backend_config_for_config(config)
    if isinstance(resolved, backend_config.NullBackendConfig):
        return NullBackend()
    if isinstance(resolved, backend_config.CommandBackendConfig):
        return CommandBackend(resolved)
    raise AuditBackendError(f"Unsupported audit backend: {config.audit.backend}")


def _build_request(
    plan: installer.SkillPlan,
    static_findings: tuple[Finding, ...],
    cloud: bool,
    content_sha256: str,
) -> AuditRequest:
    files: dict[str, bytes] = {}
    redacted = False
    for path in sorted(plan.snapshot.rglob("*")):
        if not path.is_file():
            continue
        rel = path.relative_to(plan.snapshot).as_posix()
        content = path.read_bytes()
        if cloud:
            scrubbed = redaction.scrub_bytes(content)
            if scrubbed != content:
                redacted = True
            content = scrubbed
        files[rel] = content
    findings = static_findings
    if redacted:
        findings = static_findings + (
            Finding(
                id="audit.redaction.applied",
                surface=Surface.MANIFEST,
                category="hygiene",
                severity=Severity.INFO,
                location=None,
                evidence="File contents redacted before cloud backend request",
                detector="audit.redaction",
                confidence="high",
                verifiable=True,
            ),
        )
    return AuditRequest(
        skill=plan.decl.name,
        source=plan.decl.source,
        commit=plan.resolved.commit,
        content_sha256=content_sha256,
        files=files,
        capabilities=plan.spec.capabilities,
        contract_reference="",
        response_schema={},
        static_findings=findings,
        redacted=redacted,
    )


def _request_size(request: AuditRequest) -> int:
    payload = serialization.request_to_payload(request)
    return len(json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8"))


def _too_large_finding(size: int, limit: int) -> Finding:
    return Finding(
        id="audit.request.too-large",
        surface=Surface.MANIFEST,
        category="audit-incomplete",
        severity=Severity.HIGH,
        location=None,
        evidence=f"Audit request is {size} bytes, limit is {limit} bytes",
        detector="audit.request",
        confidence="high",
        verifiable=True,
    )


def _check_cloud_egress(
    config: GlobalConfig,
    resolved_backend: backend_config.BackendConfig,
    plan: installer.SkillPlan,
) -> None:
    if not resolved_backend.cloud:
        return
    source_class = config.audit.source_policy.classify(plan.decl.source, plan.decl.git)
    if source_class != "public":
        raise AuditEgressError(
            f"cloud audit backend {resolved_backend.name} is not allowed for {plan.decl.name}: "
            f"source class is {source_class}"
        )


def _report_from_verdict(
    plan: installer.SkillPlan,
    verdict: Verdict,
    config: GlobalConfig,
    trust_record: TrustRecord,
    revocation: str | None,
    *,
    scope: str,
    cache_hit: bool,
) -> AuditReport:
    decision = _decide(plan, config, verdict.findings, trust_record, verdict.content_sha256, revocation)
    return AuditReport(
        scope=scope,
        skill=plan.decl.name,
        source=plan.decl.source,
        ref_kind=plan.resolved.kind,
        ref=plan.resolved.ref,
        commit=plan.resolved.commit,
        schema_version=plan.spec.schema_version,
        source_file=plan.spec.source_file,
        runtime_roots=plan.spec.runtime_roots,
        content_sha256=verdict.content_sha256,
        findings=verdict.findings,
        decision=decision,
        ran_at=verdict.ran_at,
        cache_hit=cache_hit,
        trust=trust_record,
        revoked=revocation is not None,
        revocation=revocation,
    )


def _gate_messages(report: AuditReport) -> list[str]:
    if report.revoked:
        return [f"audit blocked: {report.skill}: {report.revocation} is revoked"]
    if report.decision == Decision.REQUIRE_PIN:
        return [
            f"audit requires pin: {report.skill}: schema v{report.schema_version} has no capabilities; "
            f"migrate to csk-skill.json schema v3 or run 'csk audit --allow {report.content_sha256} --reason <reason>'"
        ]
    prefix = "audit blocked" if report.decision == Decision.BLOCK else "audit warning"
    if not report.findings:
        return [f"{prefix}: {report.skill}: {report.decision.value}"]
    messages: list[str] = []
    for finding in report.findings:
        location = ""
        if finding.location is not None:
            line = finding.location.span[0] if finding.location.span else 1
            location = f" {finding.location.file}:{line}"
        messages.append(
            f"{prefix}: {report.skill}: {finding.severity.value} {finding.id}{location} - "
            f"{redaction.scrub_text(finding.evidence)}"
        )
    return messages


def _decide(
    plan: installer.SkillPlan,
    config: GlobalConfig,
    findings: tuple[Finding, ...],
    trust_record: TrustRecord,
    content_sha256: str,
    revocation: str | None,
) -> Decision:
    if revocation is not None:
        return Decision.BLOCK
    if config.audit.mode == "strict" and plan.spec.schema_version < 3 and not trust_record.pinned:
        return Decision.REQUIRE_PIN
    return policy.decide(findings, mode=config.audit.mode, fail_on=config.audit.fail_on)


def _revocation_reason(
    config: GlobalConfig,
    content_sha256: str,
    source: str,
    git: str | None,
) -> str | None:
    normalized = trust.normalize_content_sha256(content_sha256)
    for item in config.audit.revocations:
        if item.startswith("source:"):
            pattern = item.removeprefix("source:")
            if _source_revocation_matches(pattern, source, git):
                return f"source {pattern}"
            continue
        if trust.normalize_content_sha256(item) == normalized:
            return f"content hash {content_sha256}"
    return None


def _source_revocation_matches(pattern: str, source: str, git: str | None) -> bool:
    candidates = {source}
    if git:
        candidates.add(git)
        normalized = normalize_source(git)
        if normalized:
            candidates.add(normalized)
    normalized_source = normalize_source(source)
    if normalized_source:
        candidates.add(normalized_source)
    return any(fnmatch.fnmatchcase(candidate, pattern) for candidate in candidates)
