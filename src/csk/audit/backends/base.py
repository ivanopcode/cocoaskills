from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from ..capabilities import CapabilityManifest
from ..model import Finding


class AuditBackendError(Exception):
    pass


@dataclass(frozen=True)
class AuditRequest:
    files: dict[str, bytes]
    capabilities: CapabilityManifest
    contract_reference: str
    response_schema: dict
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
