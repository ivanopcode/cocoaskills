"""Secret shapes with value-free findings and masking for downstream detectors."""
from __future__ import annotations

from bisect import bisect_left
import math
import re
from collections import Counter

from .model import Finding, Location, Severity, Surface

_PATTERNS = (
    ("private-key", re.compile(r"-----BEGIN (?P<kind>(?:(?:RSA|EC|DSA|OPENSSH|ENCRYPTED) )?PRIVATE KEY)-----[\s\S]*?(?:-----END (?P=kind)-----|\Z)")),
    ("aws-access-key", re.compile(r"(?<![A-Za-z0-9])AKIA[A-Z0-9]{16}(?![A-Za-z0-9])")),
    ("github-token", re.compile(r"(?<![A-Za-z0-9_])(?:gh[pousr]_[A-Za-z0-9]{36}|github_pat_[A-Za-z0-9_]{22,255})(?![A-Za-z0-9_])")),
    ("jwt", re.compile(r"(?<![A-Za-z0-9_-])eyJ[A-Za-z0-9_-]{5,2048}\.[A-Za-z0-9_-]{2,8192}\.[A-Za-z0-9_-]{16,2048}(?![A-Za-z0-9_-])")),
)
# Candidate lengths are bounded; exclude prose, paths and hex digests.
_VALUE = re.compile(r'''(?:["'=:\t ])([A-Za-z0-9_+/=-]{32,256})(?=["'\s,;}]|$)''')


def detect_and_mask(rel_path: str, text: str) -> tuple[list[Finding], str]:
    spans: list[tuple[int, int, str]] = []
    for kind, pattern in _PATTERNS:
        spans.extend((m.start(), m.end(), kind) for m in pattern.finditer(text))
    for match in _VALUE.finditer(text):
        value = match.group(1)
        if not (any(c.islower() for c in value) and any(c.isupper() for c in value) and any(c.isdigit() for c in value)):
            continue
        entropy = -sum((n / len(value)) * math.log2(n / len(value)) for n in Counter(value).values())
        if entropy >= 4.5:
            spans.append((match.start(1), match.end(1), "high-entropy"))
    findings: list[Finding] = []
    masked = list(text)
    newlines = [i for i, c in enumerate(text) if c == "\n"]
    covered = -1
    for start, end, kind in sorted(spans, key=lambda span: (span[0], -span[1])):
        if end <= covered:
            continue
        covered = end
        line = bisect_left(newlines, start) + 1
        findings.append(Finding(
            id=f"static.secret.{kind}", surface=Surface.CODE, category="secret-value",
            severity=Severity.HIGH, location=Location(rel_path, (line, line)),
            evidence=f"Potential {kind} value (withheld)", detector="static.secret",
            confidence="medium" if kind == "high-entropy" else "high", verifiable=True,
        ))
        masked[start:end] = ["\n" if c == "\n" else "x" for c in text[start:end]]
    return findings, "".join(masked)
