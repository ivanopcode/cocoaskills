from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Protocol

from ..capabilities import CapabilityManifest
from ..model import Finding


class AuditBackendError(Exception):
    pass


class AuditCanaryError(AuditBackendError):
    pass


class AuditEgressError(AuditBackendError):
    pass


@dataclass(frozen=True)
class AuditRequest:
    skill: str
    source: str
    commit: str
    content_sha256: str
    files: dict[str, bytes]
    capabilities: CapabilityManifest
    contract_reference: str
    response_schema: dict[str, Any]
    static_findings: tuple[Finding, ...]
    redacted: bool


class AuditBackend(Protocol):
    name: str
    cloud: bool

    def is_available(self) -> bool:
        ...

    def run_canary(self) -> bool:
        ...

    def extract(self, request: AuditRequest, *, timeout: float) -> tuple[Finding, ...]:
        ...
