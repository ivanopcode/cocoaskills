from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from pathlib import PurePosixPath
from types import MappingProxyType


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


# The static namespace has a closed schema; no free-form reason or capability data.
SECRET_REASONS = MappingProxyType({f"static.secret.{kind}": f"Potential {kind} value (withheld)" for kind in (
    "private-key", "aws-access-key", "github-token", "jwt", "high-entropy",
)})


@dataclass(frozen=True)
class _ScanLocation(Location):
    """A relative scanned-file location, also restored from canonical cache records."""

    def __post_init__(self) -> None:
        # Import lazily: output masking depends on the Finding schema.
        from .secret_values import mask_output_text

        path = PurePosixPath(self.file)
        if (not self.file or path.is_absolute() or ".." in path.parts
                or mask_output_text(self.file) != self.file
                or self.span is None or len(self.span) != 2 or any(type(line) is not int for line in self.span)
                or self.span[0] < 1 or self.span[1] != self.span[0]):
            raise ValueError("invalid secret scan location")


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

    def __post_init__(self) -> None:
        if not (self.id.startswith("static.secret.") or self.detector == "static.secret"
                or self.category == "secret-value"):
            return
        if (self.id not in SECRET_REASONS or self.surface is not Surface.CODE
                or self.category != "secret-value" or self.severity is not Severity.HIGH
                or self.evidence != SECRET_REASONS[self.id] or self.detector != "static.secret"
                or self.confidence != ("medium" if self.id.endswith(".high-entropy") else "high")
                or self.verifiable is not True or self.capability_violation is not None
                or not isinstance(self.location, _ScanLocation)):
            # Never echo rejected input: this error can itself be logged.
            raise ValueError("invalid secret finding schema")


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
