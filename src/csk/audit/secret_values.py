"""Bounded secret-shape recognition and value-free structured findings."""
from __future__ import annotations

import base64
from bisect import bisect_left
from collections import Counter, deque
from collections.abc import Iterator
from dataclasses import fields, is_dataclass, replace
from enum import Enum
from functools import lru_cache
import json
import math
import re
from typing import Any

from .model import Finding, Location, Severity, Surface, SECRET_REASONS, _ScanLocation

MAX_FINDINGS_PER_FILE = 100
_PATTERNS = (
    ("private-key", re.compile(r"-----BEGIN (?P<kind>(?:(?:RSA|EC|DSA|OPENSSH|ENCRYPTED) )?PRIVATE KEY)-----[\s\S]*?(?:-----END (?P=kind)-----|\Z)")),
    ("aws-access-key", re.compile(r"(?<![A-Za-z0-9])(?:AKIA|ASIA)[A-Z0-9]{16}(?![A-Za-z0-9])")),
    ("github-token", re.compile(r"(?<![A-Za-z0-9_])(?:gh[pousr]_[A-Za-z0-9]{36}|github_pat_[A-Za-z0-9_]{22,255})(?![A-Za-z0-9_])")),
)
_OUTPUT_PATTERNS = (
    ('private-key', re.compile(_PATTERNS[0][1].pattern, re.IGNORECASE)),
    ('aws-access-key', re.compile(r'(?:AKIA|ASIA)[A-Z0-9]{16}', re.IGNORECASE)),
)
_JWT_PART = re.compile(r"[A-Za-z0-9_-]+={0,2}")
_TOKEN = re.compile(r"[A-Za-z0-9_+/-]+={0,2}")
_ASSIGNMENT = re.compile(r'''["']?([A-Za-z_][A-Za-z0-9_]*)["']?\s*([:=])\s*["']?$''')
_JSON_VALUE = re.compile(r'"[^"\n]{1,128}"\s*:\s*"$')
_ESCAPE = re.compile(r"\\(?:x([0-9a-fA-F]{2})|u([0-9a-fA-F]{4}))")
_PUBLIC_PEM = re.compile(r"-----BEGIN (PUBLIC KEY|CERTIFICATE)-----[\s\S]*?(?:-----END \1-----|\Z)")
# Output candidates include path/URL separators before any lossy transformation.
_OUTPUT_TOKEN = re.compile(r"[A-Za-z0-9_+/.:@?&=%-]+")


def _secret_finding(kind: str, rel_path: str, line: int) -> Finding:
    # rel_path comes only from snapshot traversal, never from a matched value.
    finding_id = f'static.secret.{kind}'
    if finding_id not in SECRET_REASONS:
        raise ValueError('unknown secret kind')
    return Finding(
        id=finding_id, surface=Surface.CODE, category='secret-value',
        severity=Severity.HIGH, location=_ScanLocation(mask_output_text(rel_path), (line, line)),
        evidence=SECRET_REASONS[finding_id], detector='static.secret',
        confidence='medium' if kind == 'high-entropy' else 'high', verifiable=True,
    )


def finding_limit(rel_path: str) -> Finding:
    return Finding(
        id='static.opaque.finding-limit', surface=Surface.CODE, category='opaque',
        severity=Severity.HIGH, location=Location(rel_path),
        evidence='scan truncated: per-file finding limit reached',
        detector='static.opaque', confidence='high', verifiable=True,
    )


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


def _base64(value: str) -> bytes:
    return base64.b64decode(value + '=' * (-len(value) % 4), altchars=b'-_', validate=True)


def _predictable_base64(value: str) -> bool:
    # Ascending/descending byte fixtures have apparent encoded entropy but no secret randomness.
    try:
        raw = _base64(value)
    except ValueError:
        return False
    return len(raw) >= 16 and len({(b - a) % 256 for a, b in zip(raw, raw[1:])}) == 1


def _secret_spans(decoded: str, *, contextual: bool) -> Iterator[tuple[int, int, str]]:
    for kind, pattern in (_PATTERNS if contextual else _OUTPUT_PATTERNS):
        for match in pattern.finditer(decoded):
            yield match.start(), match.end(), kind
    # Lex segments once, retaining only a three-part window. Padding is part of a segment.
    parts: deque[re.Match[str]] = deque(maxlen=3)
    for part in _JWT_PART.finditer(decoded):
        if parts and (part.start() != parts[-1].end() + 1 or decoded[part.start() - 1] != '.'):
            parts.clear()
        parts.append(part)
        if len(parts) != 3:
            continue
        header_part, payload_part, signature = (p.group() for p in parts)
        if not (8 <= len(header_part) <= 2048 and 2 <= len(payload_part) <= 8192 and 16 <= len(signature) <= 2048):
            continue
        if not contextual:
            # Reconstructed prefixes/case changes may invalidate the JSON while
            # still carrying the original token. Output masking needs no validation.
            yield parts[0].start(), part.end(), 'jwt'
            continue
        try:
            header = json.loads(_base64(header_part))
            json.loads(_base64(payload_part))
            _base64(signature)
        except (ValueError, UnicodeError, RecursionError):
            continue
        if isinstance(header, dict) and isinstance(header.get('alg'), str):
            yield parts[0].start(), part.end(), 'jwt'
    if not contextual:
        # Preserve ordinary URL diagnostics, but mask a complete random-looking
        # composite before ':' or '.' can split it into short reportable pieces.
        for match in _OUTPUT_TOKEN.finditer(decoded):
            value = match.group()
            if (len(value) >= 32 and any(c.islower() for c in value)
                    and any(c.isupper() for c in value) and any(c.isdigit() for c in value)
                    and _entropy(value) >= 4.5):
                yield match.start(), match.end(), 'masked-composite'
    public = list(_PUBLIC_PEM.finditer(decoded)) if contextual else []
    public_index = 0
    for match in _TOKEN.finditer(decoded):
        value = match.group()
        if not contextual:
            # Output fields may contain a secret inside a longer path/identifier.
            # Redact whole long runs; source-context and entropy gates apply only
            # to classification, never to whether a value can leave the detector.
            if len(value) >= 32:
                yield match.start(), match.end(), 'masked-token'
            continue
        if not 32 <= len(value) <= 256:
            continue
        if contextual:
            while public_index < len(public) and public[public_index].end() <= match.start():
                public_index += 1
            if public_index < len(public) and public[public_index].start() <= match.start():
                continue
            prefix = decoded[max(0, match.start() - 128):match.start()]
            assignment = _ASSIGNMENT.search(prefix)
            name = assignment.group(1).lower().replace('_', '') if assignment else ''
            sensitive = any(part in name for part in ('secret', 'token', 'password', 'credential', 'apikey', 'privatekey')) or name == 'value'
            json_value = _JSON_VALUE.search(prefix) is not None
            bearer = re.search(r'\bBearer\s+$', prefix, re.IGNORECASE) is not None
            spaced = (match.start() == 0 or decoded[match.start() - 1].isspace() or decoded[match.start() - 1] in '([') and (match.end() == len(decoded) or decoded[match.end()].isspace() or decoded[match.end()] in ']),')
            query = re.search(r'[?&][A-Za-z0-9_]{1,32}=$', prefix) is not None
            if not (sensitive or json_value or bearer or spaced or query):
                continue
            if value.startswith(('sha256-', 'sha384-', 'sha512-')) or re.fullmatch(r'[A-Za-z]{2,12}-[A-Za-z0-9-]+', value):
                continue
            if _predictable_base64(value):
                continue
        if not (any(c.isalpha() for c in value) and any(c.isdigit() for c in value)):
            continue
        if contextual and not (any(c.islower() for c in value) and any(c.isupper() for c in value)):
            continue
        if _entropy(value) >= 4.5:
            yield match.start(), match.end(), 'high-entropy'


def _entropy(value: str) -> float:
    return -sum((n / len(value)) * math.log2(n / len(value)) for n in Counter(value).values())


def _mask(text: str, offsets: list[tuple[int, int]], spans: list[tuple[int, int, str]]) -> str:
    if not spans:
        return text
    masked = list(text)
    for start, end, _ in spans:
        if offsets:
            start, end = offsets[start][0], offsets[end - 1][1]
        masked[start:end] = ['\n' if c == '\n' else 'x' for c in text[start:end]]
    return ''.join(masked)


def detect_and_mask(rel_path: str, text: str, values: set[str] | None = None, *, max_findings: int = MAX_FINDINGS_PER_FILE) -> tuple[list[Finding], str]:
    decoded, offsets = _decoded(text)
    # Stop the producer, not just the returned list. An extra match is the cap witness.
    spans: list[tuple[int, int, str]] = []
    for start, end, kind in _secret_spans(decoded, contextual=True):
        if any(left <= start and end <= right for left, right, _ in spans):
            continue
        spans.append((start, end, kind))
        if len(spans) > max_findings:
            break
    truncated = len(spans) > max_findings
    spans = spans[:max_findings]
    newlines = [i for i, c in enumerate(text) if c == '\n']
    findings: list[Finding] = []
    covered = -1
    for start, end, kind in sorted(spans, key=lambda span: (span[0], -span[1])):
        if end <= covered:
            continue
        covered = end
        if values is not None:
            values.add(decoded[start:end])
        source_start = offsets[start][0] if offsets else start
        findings.append(_secret_finding(kind, rel_path, bisect_left(newlines, source_start) + 1))
    if truncated:
        findings.append(finding_limit(mask_output_text(rel_path)))
    return findings, _mask(text, offsets, spans)


def mask_output_text(value: str) -> str:
    """Mask complete output candidates before parsers can discard their prefixes."""
    decoded, offsets = _decoded(value)
    return _mask(value, offsets, list(_secret_spans(decoded, contextual=False)))


def sanitize_findings(findings: list[Finding], values: set[str]) -> tuple[Finding, ...]:
    # Output redaction is context-free: decoded evidence and metadata have no source context.
    pattern = re.compile('|'.join(re.escape(v) for v in sorted(values, key=len, reverse=True))) if values else None
    @lru_cache(maxsize=256)
    def clean_text(value: str) -> str:
        value = pattern.sub('[withheld]', value) if pattern else value
        return mask_output_text(value)
    def clean(value: Any) -> Any:
        if isinstance(value, Enum):
            return value
        if isinstance(value, str):
            return clean_text(value)
        if is_dataclass(value) and not isinstance(value, type):
            return replace(value, **{field.name: clean(getattr(value, field.name)) for field in fields(value)})
        return value
    # Identifiers and classification fields are fixed detector schema, not input text.
    sanitized = [replace(finding, location=clean(finding.location),
                         evidence=clean_text(finding.evidence),
                         capability_violation=clean(finding.capability_violation)) for finding in findings]
    return tuple(replace(finding, evidence=finding.evidence[:160]) for finding in sanitized)
