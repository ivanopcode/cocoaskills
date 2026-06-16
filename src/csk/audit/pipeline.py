from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from .. import installer
from ..config import GlobalConfig

from . import detectors, policy
from .model import Decision, Finding


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
    findings: tuple[Finding, ...]
    decision: Decision
    ran_at: str


def audit_plans(plans: list[installer.SkillPlan], config: GlobalConfig, *, scope: str) -> tuple[AuditReport, ...]:
    reports: list[AuditReport] = []
    ran_at = datetime.now(timezone.utc).isoformat()
    for plan in plans:
        findings = detectors.detect_snapshot(plan.snapshot, plan.spec.capabilities)
        decision = policy.decide(findings, mode=config.audit.mode, fail_on=config.audit.fail_on)
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
                findings=findings,
                decision=decision,
                ran_at=ran_at,
            )
        )
    return tuple(reports)


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
        "decision": report.decision.value,
        "ran_at": report.ran_at,
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
