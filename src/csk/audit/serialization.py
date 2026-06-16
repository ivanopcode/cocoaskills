from __future__ import annotations

import base64
from typing import Any

from . import redaction
from .backends.base import AuditRequest
from .capabilities import CapabilityManifest
from .model import CapabilityViolation, Finding, Location, Severity, Surface


def request_to_payload(request: AuditRequest) -> dict[str, Any]:
    return {
        "schema_version": 1,
        "skill": request.skill,
        "source": request.source,
        "commit": request.commit,
        "content_sha256": request.content_sha256,
        "capabilities": capability_manifest_to_payload(request.capabilities),
        "static_findings": [finding_to_payload(finding) for finding in request.static_findings],
        "files": {path: _file_payload(content) for path, content in sorted(request.files.items())},
        "redacted": request.redacted,
        "contract_reference": request.contract_reference,
        "response_schema": request.response_schema,
    }


def capability_manifest_to_payload(capabilities: CapabilityManifest) -> dict[str, Any]:
    return {
        "network": list(capabilities.network),
        "filesystem": list(capabilities.filesystem) if isinstance(capabilities.filesystem, tuple) else capabilities.filesystem,
        "exec": list(capabilities.exec),
        "secrets": list(capabilities.secrets),
        "env_read": list(capabilities.env_read),
        "prompt_scope": capabilities.prompt_scope,
    }


def finding_to_payload(finding: Finding) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "id": finding.id,
        "surface": finding.surface.value,
        "category": finding.category,
        "severity": finding.severity.value,
        "location": None,
        "evidence": redaction.scrub_text(finding.evidence),
        "detector": finding.detector,
        "confidence": finding.confidence,
        "verifiable": finding.verifiable,
        "capability_violation": None,
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


def finding_from_payload(payload: dict[str, Any]) -> Finding:
    _reject_unknown_fields(
        payload,
        {
            "id",
            "surface",
            "category",
            "severity",
            "location",
            "evidence",
            "detector",
            "confidence",
            "verifiable",
            "capability_violation",
        },
        "finding",
    )
    location = None
    location_raw = payload.get("location")
    if location_raw is not None:
        if not isinstance(location_raw, dict):
            raise ValueError("finding.location must be an object or null")
        _reject_unknown_fields(location_raw, {"file", "span"}, "finding.location")
        file = location_raw.get("file")
        if not isinstance(file, str) or not file:
            raise ValueError("finding.location.file must be a non-empty string")
        span_raw = location_raw.get("span")
        span = None
        if span_raw is not None:
            if not isinstance(span_raw, list) or len(span_raw) != 2 or not all(isinstance(item, int) for item in span_raw):
                raise ValueError("finding.location.span must be a two-integer list or null")
            span = (span_raw[0], span_raw[1])
        location = Location(file=file, span=span)
    violation = None
    violation_raw = payload.get("capability_violation")
    if violation_raw is not None:
        if not isinstance(violation_raw, dict):
            raise ValueError("finding.capability_violation must be an object or null")
        _reject_unknown_fields(violation_raw, {"capability", "declared", "observed"}, "finding.capability_violation")
        violation = CapabilityViolation(
            capability=_required_string(violation_raw, "capability", "finding.capability_violation.capability"),
            declared=_required_string(violation_raw, "declared", "finding.capability_violation.declared"),
            observed=_required_string(violation_raw, "observed", "finding.capability_violation.observed"),
        )
    return Finding(
        id=_required_string(payload, "id", "finding.id"),
        surface=Surface(_required_string(payload, "surface", "finding.surface")),
        category=_required_string(payload, "category", "finding.category"),
        severity=Severity(_required_string(payload, "severity", "finding.severity")),
        location=location,
        evidence=_required_string(payload, "evidence", "finding.evidence"),
        detector=_required_string(payload, "detector", "finding.detector"),
        confidence=_required_string(payload, "confidence", "finding.confidence"),
        verifiable=_required_bool(payload, "verifiable", "finding.verifiable"),
        capability_violation=violation,
    )


def response_findings_from_payload(payload: dict[str, Any]) -> tuple[Finding, ...]:
    _reject_unknown_fields(payload, {"schema_version", "findings"}, "audit backend response")
    if payload.get("schema_version") != 1:
        raise ValueError("audit backend response schema_version must be 1")
    findings = payload.get("findings")
    if not isinstance(findings, list):
        raise ValueError("audit backend response findings must be a list")
    return tuple(finding_from_payload(item) for item in findings)


def _file_payload(content: bytes) -> dict[str, str]:
    if b"\x00" not in content:
        try:
            return {"encoding": "utf-8", "content": content.decode("utf-8")}
        except UnicodeDecodeError:
            pass
    return {"encoding": "base64", "content": base64.b64encode(content).decode("ascii")}


def _required_string(payload: dict[str, Any], key: str, field: str) -> str:
    value = payload.get(key)
    if not isinstance(value, str) or not value:
        raise ValueError(f"{field} must be a non-empty string")
    return value


def _required_bool(payload: dict[str, Any], key: str, field: str) -> bool:
    value = payload.get(key)
    if not isinstance(value, bool):
        raise ValueError(f"{field} must be a boolean")
    return value


def _reject_unknown_fields(data: dict[str, Any], allowed: set[str], label: str) -> None:
    unknown = sorted(set(data) - allowed)
    if unknown:
        joined = ", ".join(repr(item) for item in unknown)
        raise ValueError(f"{label} has unsupported field(s): {joined}")
