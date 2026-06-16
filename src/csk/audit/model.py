from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum


class Severity(StrEnum):
    INFO = "info"
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    CRITICAL = "critical"


class Surface(StrEnum):
    CODE = "code"
    PROMPT = "prompt"
    MANIFEST = "manifest"


class Decision(StrEnum):
    ALLOW = "allow"
    WARN = "warn"
    CONFIRM = "confirm"
    BLOCK = "block"
    REQUIRE_PIN = "require_pin"


@dataclass(frozen=True)
class Location:
    file: str
    span: tuple[int, int] | None = None


@dataclass(frozen=True)
class CapabilityViolation:
    capability: str
    declared: str
    observed: str


@dataclass(frozen=True)
class Finding:
    id: str
    surface: Surface
    category: str
    severity: Severity
    location: Location | None
    evidence: str
    detector: str
    confidence: str
    verifiable: bool
    capability_violation: CapabilityViolation | None = None


@dataclass(frozen=True)
class TrustRecord:
    pinned: bool = False
    pinned_by: str | None = None
    reason: str | None = None


@dataclass(frozen=True)
class Verdict:
    schema_version: int
    content_sha256: str
    skill: str
    source: str
    commit: str
    backend: str
    model: str | None
    cloud: bool
    prompt_version: int
    ruleset_version: int
    canary_passed: bool | None
    findings: tuple[Finding, ...]
    decision: Decision
    ran_at: str
    trust: TrustRecord
