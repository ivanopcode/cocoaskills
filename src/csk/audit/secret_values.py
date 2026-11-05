"""Bounded secret-shape recognition and value-free structured findings."""
from __future__ import annotations

import base64
from bisect import bisect_left
from collections import Counter
from dataclasses import fields, is_dataclass, replace
from enum import Enum
import json
import math
import re
from typing import Any

from .model import Finding, Location, Severity, Surface

_PATTERNS = (
    ("private-key", re.compile(r"-----BEGIN (?P<kind>(?:(?:RSA|EC|DSA|OPENSSH|ENCRYPTED) )?PRIVATE KEY)-----[\s\S]*?(?:-----END (?P=kind)-----|\Z)")),
    ("aws-access-key", re.compile(r"(?<![A-Za-z0-9])AKIA[A-Z0-9]{16}(?![A-Za-z0-9])")),
    ("github-token", re.compile(r"(?<![A-Za-z0-9_])(?:gh[pousr]_[A-Za-z0-9]{36}|github_pat_[A-Za-z0-9_]{22,255})(?![A-Za-z0-9_])")),
)
_JWT_RUN = re.compile(r"[A-Za-z0-9_.-]+")
_TOKEN = re.compile(r"[A-Za-z0-9_+/-]+={0,2}")
_ASSIGNMENT = re.compile(r'''(?:["']?)([A-Za-z_][A-Za-z0-9_]*)(?:["']?)\s*[:=]\s*["']?$''')
_ESCAPE = re.compile(r"\\(?:x([0-9a-fA-F]{2})|u([0-9a-fA-F]{4}))")
_PUBLIC_PEM = re.compile(r"-----BEGIN (PUBLIC KEY|CERTIFICATE)-----[\s\S]*?(?:-----END \1-----|\Z)")


def _decoded(text: str) -> tuple[str, list[tuple[int, int]]]:
    if "\\" not in text:
        return text, []
    chars: list[str] = []
    offsets: list[tuple[int, int]] = []
    position = 0
    for match in _ESCAPE.finditer(text):
        chars.extend(text[position:match.start()])
        offsets.extend((i, i + 1) for i in range(position, match.start()))
        chars.append(chr(int(match.group(1) or match.group(2), 16)))
        offsets.append((match.start(), match.end()))
        position = match.end()
    chars.extend(text[position:])
    offsets.extend((i, i + 1) for i in range(position, len(text)))
    return "".join(chars), offsets


def detect_and_mask(rel_path: str, text: str, values: set[str] | None = None) -> tuple[list[Finding], str]:
    decoded, offsets = _decoded(text)
    spans: list[tuple[int, int, str]] = []
    for kind, pattern in _PATTERNS:
        spans.extend((m.start(), m.end(), kind) for m in pattern.finditer(decoded))
    # Lex maximal runs once; do not retry a bounded header at every byte of a long identifier.
    for run in _JWT_RUN.finditer(decoded):
        parts = run.group().split('.')
        offset = run.start()
        for index in range(len(parts) - 2):
            header_part, payload_part, signature = parts[index:index + 3]
            start = offset
            offset += len(header_part) + 1
            if not (8 <= len(header_part) <= 2048 and 2 <= len(payload_part) <= 8192 and 16 <= len(signature) <= 2048):
                continue
            try:
                header = json.loads(base64.urlsafe_b64decode(header_part + '=' * (-len(header_part) % 4)))
                payload = json.loads(base64.urlsafe_b64decode(payload_part + '=' * (-len(payload_part) % 4)))
            except (ValueError, UnicodeError, RecursionError):
                continue
            if isinstance(header, dict) and isinstance(header.get('alg'), str) and isinstance(payload, dict):
                spans.append((start, start + len(header_part) + len(payload_part) + len(signature) + 2, 'jwt'))
    public = list(_PUBLIC_PEM.finditer(decoded))
    public_index = 0
    line_start = 0
    previous_end = 0
    for match in _TOKEN.finditer(decoded):
        newline = decoded.rfind("\n", previous_end, match.start())
        if newline >= 0:
            line_start = newline + 1
        previous_end = match.end()
        value = match.group()
        while public_index < len(public) and public[public_index].end() <= match.start():
            public_index += 1
        if public_index < len(public) and public[public_index].start() <= match.start():
            continue
        if not 32 <= len(value) <= 256:
            continue
        # Inspect only a bounded assignment prefix; never include its name in entropy.
        prefix = decoded[max(0, match.start() - 128):match.start()]
        assignment = _ASSIGNMENT.search(prefix)
        sensitive = assignment is not None and any(part in assignment.group(1).lower() for part in ('secret', 'token', 'password', 'credential', 'api_key', 'private_key'))
        sensitive = sensitive or (assignment is not None and assignment.group(1).lower() == 'value')
        bare = (match.start() == line_start and (match.end() == len(decoded) or decoded[match.end()] in '\r\n'))
        if not (sensitive or bare) or not 32 <= len(value) <= 256:
            continue
        if value.startswith(('sha256-', 'sha384-', 'sha512-')) or re.fullmatch(r'[A-Za-z]{2,12}-[A-Za-z0-9-]+', value):
            continue
        if not (any(c.islower() for c in value) and any(c.isupper() for c in value) and any(c.isdigit() for c in value)):
            continue
        entropy = -sum((n / len(value)) * math.log2(n / len(value)) for n in Counter(value).values())
        if entropy >= 4.5:
            spans.append((match.start(), match.end(), 'high-entropy'))
    masked = list(text)
    newlines = [i for i, c in enumerate(text) if c == '\n']
    findings: list[Finding] = []
    covered = -1
    for start, end, kind in sorted(spans, key=lambda span: (span[0], -span[1])):
        if end <= covered:
            continue
        covered = end
        if values is not None:
            values.add(decoded[start:end])
        if offsets:
            start, end = offsets[start][0], offsets[end - 1][1]
        line = bisect_left(newlines, start) + 1
        findings.append(Finding(
            id=f'static.secret.{kind}', surface=Surface.CODE, category='secret-value',
            severity=Severity.HIGH, location=Location(rel_path, (line, line)),
            evidence=f'Potential {kind} value (withheld)', detector='static.secret',
            confidence='medium' if kind == 'high-entropy' else 'high', verifiable=True,
        ))
        masked[start:end] = ['\n' if c == '\n' else 'x' for c in text[start:end]]
    return findings, ''.join(masked)


def sanitize_findings(findings: list[Finding], values: set[str]) -> tuple[Finding, ...]:
    # Sanitize after AST decoding and capability expansion, before any serialization.
    pattern = re.compile('|'.join(re.escape(v) for v in sorted(values, key=len, reverse=True))) if values else None
    def clean(value: Any) -> Any:
        if isinstance(value, Enum):
            return value
        if isinstance(value, str):
            value = pattern.sub('[withheld]', value) if pattern else value
            return detect_and_mask('', value)[1]
        if is_dataclass(value) and not isinstance(value, type):
            return replace(value, **{field.name: clean(getattr(value, field.name)) for field in fields(value)})
        return value
    sanitized = [clean(finding) for finding in findings]
    return tuple(replace(finding, evidence=finding.evidence[:160]) for finding in sanitized)
