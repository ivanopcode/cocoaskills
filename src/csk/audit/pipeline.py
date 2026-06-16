from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from .. import hashing, installer
from ..config import GlobalConfig

from . import detectors, policy, trust
from .backends.base import AuditBackendError, AuditRequest
from .backends.null_backend import NullBackend
from .model import Decision, Finding, TrustRecord, Verdict


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
    for plan in plans:
        content_sha256 = hashing.content_sha256(plan.snapshot)
        trust_record = trust.load_trust_record(config.path.parent, content_sha256)
        revoked = _is_revoked(config, content_sha256)
        backend = _backend_for_config(config)
        cached = trust.load_cached_verdict(
            config.path.parent,
            content_sha256,
            backend.name,
            config.audit.model,
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
                    revoked=revoked,
                    scope=scope,
                    cache_hit=True,
                )
            )
            continue

        static_findings = detectors.detect_snapshot(plan.snapshot, plan.spec.capabilities)
        if not backend.is_available():
            raise AuditBackendError(f"Audit backend is unavailable: {backend.name}")
        canary_passed = backend.run_canary()
        if not canary_passed:
            raise AuditBackendError(f"Audit backend failed canary check: {backend.name}")
        backend_findings = backend.extract(
            _build_request(plan, static_findings, backend.cloud),
            timeout=30,
        )
        findings = tuple(static_findings) + tuple(backend_findings)
        decision = _decide(plan, config, findings, trust_record, content_sha256)
        verdict = Verdict(
            schema_version=trust.SCHEMA_VERSION,
            content_sha256=content_sha256,
            skill=plan.decl.name,
            source=plan.decl.source,
            commit=plan.resolved.commit,
            backend=backend.name,
            model=config.audit.model,
            cloud=backend.cloud,
            prompt_version=trust.PROMPT_VERSION,
            ruleset_version=trust.RULESET_VERSION,
            canary_passed=canary_passed,
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
                revoked=revoked,
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
    reports = audit_plans(plans, config, scope=scope, record=record)
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
            lines.append(f"  {finding.severity.value:<8} {finding.id}{location} - {finding.evidence}")
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
        "evidence": finding.evidence,
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


def _backend_for_config(config: GlobalConfig) -> NullBackend:
    if config.audit.backend != "null":
        raise AuditBackendError(f"Unsupported audit backend: {config.audit.backend}")
    return NullBackend()


def _build_request(plan: installer.SkillPlan, static_findings: tuple[Finding, ...], cloud: bool) -> AuditRequest:
    return AuditRequest(
        files={},
        capabilities=plan.spec.capabilities,
        contract_reference="",
        response_schema={},
        static_findings=static_findings,
        redacted=cloud,
    )


def _report_from_verdict(
    plan: installer.SkillPlan,
    verdict: Verdict,
    config: GlobalConfig,
    trust_record: TrustRecord,
    revoked: bool,
    *,
    scope: str,
    cache_hit: bool,
) -> AuditReport:
    decision = _decide(plan, config, verdict.findings, trust_record, verdict.content_sha256)
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
        revoked=revoked,
    )


def _gate_messages(report: AuditReport) -> list[str]:
    if report.revoked:
        return [f"audit blocked: {report.skill}: content hash {report.content_sha256} is revoked"]
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
            f"{prefix}: {report.skill}: {finding.severity.value} {finding.id}{location} - {finding.evidence}"
        )
    return messages


def _decide(
    plan: installer.SkillPlan,
    config: GlobalConfig,
    findings: tuple[Finding, ...],
    trust_record: TrustRecord,
    content_sha256: str,
) -> Decision:
    if _is_revoked(config, content_sha256):
        return Decision.BLOCK
    if config.audit.mode == "strict" and plan.spec.schema_version < 3 and not trust_record.pinned:
        return Decision.REQUIRE_PIN
    return policy.decide(findings, mode=config.audit.mode, fail_on=config.audit.fail_on)


def _is_revoked(config: GlobalConfig, content_sha256: str) -> bool:
    normalized = trust.normalize_content_sha256(content_sha256)
    for item in config.audit.revocations:
        try:
            if trust.normalize_content_sha256(item) == normalized:
                return True
        except ValueError:
            continue
    return False
