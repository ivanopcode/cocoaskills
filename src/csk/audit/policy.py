from __future__ import annotations

from .model import Decision, Finding, Severity


_SEVERITY_RANK = {
    Severity.INFO: 0,
    Severity.LOW: 1,
    Severity.MEDIUM: 2,
    Severity.HIGH: 3,
    Severity.CRITICAL: 4,
}


def decide(findings: tuple[Finding, ...], *, mode: str, fail_on: str) -> Decision:
    if not findings:
        return Decision.ALLOW
    if mode != "strict" or fail_on == "off":
        return Decision.WARN
    threshold = _threshold_rank(fail_on)
    if any(_SEVERITY_RANK[finding.severity] >= threshold for finding in findings if finding.verifiable):
        return Decision.BLOCK
    return Decision.WARN


def _threshold_rank(value: str) -> int:
    try:
        return _SEVERITY_RANK[Severity(value)]
    except ValueError:
        return _SEVERITY_RANK[Severity.HIGH]
