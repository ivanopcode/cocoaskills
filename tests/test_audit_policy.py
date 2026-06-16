from __future__ import annotations

from csk.audit import policy
from csk.audit.model import Decision, Finding, Severity, Surface


def test_strict_policy_ignores_unverifiable_backend_findings():
    finding = Finding(
        id="llm.unverifiable",
        surface=Surface.PROMPT,
        category="semantic-risk",
        severity=Severity.CRITICAL,
        location=None,
        evidence="Model claims a risk but cannot anchor it.",
        detector="codex",
        confidence="medium",
        verifiable=False,
    )

    assert policy.decide((finding,), mode="strict", fail_on="low") == Decision.WARN


def test_strict_policy_blocks_verifiable_backend_findings():
    finding = Finding(
        id="llm.verifiable",
        surface=Surface.PROMPT,
        category="semantic-risk",
        severity=Severity.HIGH,
        location=None,
        evidence="Model anchored the risk to deterministic evidence.",
        detector="codex",
        confidence="medium",
        verifiable=True,
    )

    assert policy.decide((finding,), mode="strict", fail_on="high") == Decision.BLOCK
