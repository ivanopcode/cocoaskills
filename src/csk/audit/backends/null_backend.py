from __future__ import annotations

from ..model import Finding
from .base import AuditRequest


class NullBackend:
    name = "null"
    cloud = False

    def is_available(self) -> bool:
        return True

    def run_canary(self) -> bool:
        return True

    def extract(self, request: AuditRequest, *, timeout: float) -> tuple[Finding, ...]:
        return ()
