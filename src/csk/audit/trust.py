from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from .model import CapabilityViolation, Decision, Finding, Location, Severity, Surface, TrustRecord, Verdict


SCHEMA_VERSION = 1
PROMPT_VERSION = 1
RULESET_VERSION = 1


def load_cached_verdict(
    csk_home: Path,
    content_sha256: str,
    backend: str,
    model: str | None,
    prompt_version: int = PROMPT_VERSION,
    ruleset_version: int = RULESET_VERSION,
) -> Verdict | None:
    path = verdict_path(csk_home, content_sha256, backend, model, prompt_version, ruleset_version)
    if not path.exists():
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    try:
        return _verdict_from_payload(payload)
    except (KeyError, TypeError, ValueError):
        return None


def store_verdict(csk_home: Path, verdict: Verdict) -> Path:
    path = verdict_path(
        csk_home,
        verdict.content_sha256,
        verdict.backend,
        verdict.model,
        verdict.prompt_version,
        verdict.ruleset_version,
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(_verdict_to_payload(verdict), indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return path


def verdict_path(
    csk_home: Path,
    content_sha256: str,
    backend: str,
    model: str | None,
    prompt_version: int,
    ruleset_version: int,
) -> Path:
    filename = f"{_safe_component(backend)}-{_safe_component(model or 'none')}-p{prompt_version}-r{ruleset_version}.json"
    return csk_home / "audit" / content_sha256 / filename


def _verdict_to_payload(verdict: Verdict) -> dict[str, Any]:
    return {
        "schema_version": SCHEMA_VERSION,
        "content_sha256": verdict.content_sha256,
        "skill": verdict.skill,
        "source": verdict.source,
        "commit": verdict.commit,
        "backend": verdict.backend,
        "model": verdict.model,
        "cloud": verdict.cloud,
        "prompt_version": verdict.prompt_version,
        "ruleset_version": verdict.ruleset_version,
        "canary_passed": verdict.canary_passed,
        "findings": [_finding_to_payload(finding) for finding in verdict.findings],
        "decision": verdict.decision.value,
        "ran_at": verdict.ran_at,
        "trust": {
            "pinned": verdict.trust.pinned,
            "pinned_by": verdict.trust.pinned_by,
            "reason": verdict.trust.reason,
        },
    }


def _verdict_from_payload(payload: dict[str, Any]) -> Verdict:
    if payload.get("schema_version") != SCHEMA_VERSION:
        raise ValueError("unsupported verdict schema")
    trust_raw = payload.get("trust") or {}
    return Verdict(
        schema_version=SCHEMA_VERSION,
        content_sha256=payload["content_sha256"],
        skill=payload["skill"],
        source=payload["source"],
        commit=payload["commit"],
        backend=payload["backend"],
        model=payload.get("model"),
        cloud=bool(payload["cloud"]),
        prompt_version=int(payload["prompt_version"]),
        ruleset_version=int(payload["ruleset_version"]),
        canary_passed=payload.get("canary_passed"),
        findings=tuple(_finding_from_payload(item) for item in payload.get("findings", [])),
        decision=Decision(payload["decision"]),
        ran_at=payload["ran_at"],
        trust=TrustRecord(
            pinned=bool(trust_raw.get("pinned", False)),
            pinned_by=trust_raw.get("pinned_by"),
            reason=trust_raw.get("reason"),
        ),
    )


def _finding_to_payload(finding: Finding) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "id": finding.id,
        "surface": finding.surface.value,
        "category": finding.category,
        "severity": finding.severity.value,
        "location": None,
        "evidence": finding.evidence,
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


def _finding_from_payload(payload: dict[str, Any]) -> Finding:
    location = None
    location_raw = payload.get("location")
    if isinstance(location_raw, dict):
        span_raw = location_raw.get("span")
        span = tuple(span_raw) if isinstance(span_raw, list) and len(span_raw) == 2 else None
        location = Location(file=location_raw["file"], span=span)
    violation = None
    violation_raw = payload.get("capability_violation")
    if isinstance(violation_raw, dict):
        violation = CapabilityViolation(
            capability=violation_raw["capability"],
            declared=violation_raw["declared"],
            observed=violation_raw["observed"],
        )
    return Finding(
        id=payload["id"],
        surface=Surface(payload["surface"]),
        category=payload["category"],
        severity=Severity(payload["severity"]),
        location=location,
        evidence=payload["evidence"],
        detector=payload["detector"],
        confidence=payload["confidence"],
        verifiable=bool(payload["verifiable"]),
        capability_violation=violation,
    )


def _safe_component(value: str) -> str:
    normalized = re.sub(r"[^A-Za-z0-9._-]+", "-", value.strip())
    return normalized.strip("-") or "none"
