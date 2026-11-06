"""Bounded secret-shape recognition and value-free structured findings."""
from __future__ import annotations

import base64
from bisect import bisect_left
from collections import Counter, deque
from collections.abc import Iterator
from dataclasses import replace
import json
import math
import re

from .model import Finding, Location, Severity, Surface, SECRET_REASONS, _ScanLocation

MAX_FINDINGS_PER_FILE = 100
_PATTERNS = (
    ("private-key", re.compile(r"-----BEGIN (?P<kind>(?:(?:RSA|EC|DSA|OPENSSH|ENCRYPTED) )?PRIVATE KEY)-----[\s\S]*?(?:-----END (?P=kind)-----|\Z)")),
    ("aws-access-key", re.compile(r"(?<![A-Za-z0-9])(?:AKIA|ASIA)[A-Z0-9]{16}(?![A-Za-z0-9])")),
    ("github-token", re.compile(r"(?<![A-Za-z0-9_])(?:gh[pousr]_[A-Za-z0-9]{36}|github_pat_[A-Za-z0-9_]{22,255})(?![A-Za-z0-9_])")),
)
_JWT_PART = re.compile(r"[A-Za-z0-9_-]+={0,2}")
_TOKEN = re.compile(r"[A-Za-z0-9_+/-]+={0,2}")
_ASSIGNMENT = re.compile(r'''["']?([A-Za-z_][A-Za-z0-9_]*)["']?\s*([:=])\s*["']?$''')
_JSON_VALUE = re.compile(r'"[^"\n]{1,128}"\s*:\s*"$')
_ESCAPE = re.compile(r"\\(?:x([0-9a-fA-F]{2})|u([0-9a-fA-F]{4}))")
_INTEGRITY = re.compile(r"(?<![A-Za-z0-9_])(?:h1:[A-Za-z0-9+/]{43}=|sha256[-:][A-Za-z0-9+/]{43}=|sha384[-:][A-Za-z0-9+/]{64}|sha512[-:][A-Za-z0-9+/]{86}==)(?![A-Za-z0-9+/=])")
_PUBLIC_PEM = re.compile(r"-----BEGIN (PUBLIC KEY|CERTIFICATE)-----[A-Za-z0-9+/=\r\n ]+-----END \1-----")
# Output candidates include path/URL separators before any lossy transformation.
_OUTPUT_TOKEN = re.compile(r"[A-Za-z0-9_+/.:@?&=%\\-]+")
_COMPONENT = re.compile(r"[A-Za-z0-9_+-]+")
_WORD_COMPONENT = re.compile(r"(?:[a-z]+(?:[-_][a-z]+)*[0-9]*|(?:[A-Z][a-z]+)+[0-9]*|[A-Z]+|[0-9]+)")


def _safe_scan_path(value: str) -> str:
    from .output import safe_path
    return safe_path(value)


def _secret_finding(kind: str, rel_path: str, line: int) -> Finding:
    # rel_path comes only from snapshot traversal, never from a matched value.
    finding_id = f'static.secret.{kind}'
    if finding_id not in SECRET_REASONS:
        raise ValueError('unknown secret kind')
    return Finding(
        id=finding_id, surface=Surface.CODE, category='secret-value',
        severity=Severity.HIGH, location=_ScanLocation(_safe_scan_path(rel_path), (line, line)),
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


def _secret_spans(decoded: str) -> Iterator[tuple[int, int, str]]:
    integrity = list(_INTEGRITY.finditer(decoded))
    for kind, pattern in _PATTERNS:
        integrity_index = 0
        for match in pattern.finditer(decoded):
            while integrity_index < len(integrity) and integrity[integrity_index].end() <= match.start():
                integrity_index += 1
            if (integrity_index < len(integrity) and integrity[integrity_index].start() <= match.start()
                    and match.end() <= integrity[integrity_index].end()):
                continue
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
        try:
            header = json.loads(_base64(header_part))
            json.loads(_base64(payload_part))
            _base64(signature)
        except (ValueError, UnicodeError, RecursionError):
            continue
        if isinstance(header, dict) and isinstance(header.get('alg'), str):
            yield parts[0].start(), part.end(), 'jwt'
    public = sorted([*_PUBLIC_PEM.finditer(decoded), *integrity], key=lambda m: m.start())
    # Source tokens retain their context gates; colon/URL composites also get
    # classified as values, so low-entropy URL prefixes cannot dilute a secret.
    patterns = (_TOKEN, _OUTPUT_TOKEN)
    for pattern in patterns:
        public_index = 0
        for match in pattern.finditer(decoded):
            value = match.group()
            if len(value) < 32:
                continue
            if pattern is _OUTPUT_TOKEN and ':' not in value:
                continue
            while public_index < len(public) and public[public_index].end() <= match.start():
                public_index += 1
            if public_index < len(public) and public[public_index].start() <= match.start():
                continue
            if not _value_context(decoded, match.start(), match.end()):
                continue
            for start, end in _entropy_spans(value):
                yield match.start() + start, match.start() + end, 'high-entropy'


def _value_context(text: str, start: int, end: int) -> bool:
    prefix = text[max(0, start - 128):start]
    assignment = _ASSIGNMENT.search(prefix)
    name = assignment.group(1).lower().replace('_', '') if assignment else ''
    sensitive = any(part in name for part in ('secret', 'token', 'password', 'credential', 'apikey', 'privatekey')) or name == 'value'
    json_value = _JSON_VALUE.search(prefix) is not None
    bearer = re.search(r'\bBearer\s+$', prefix, re.IGNORECASE) is not None
    spaced = (start == 0 or text[start - 1].isspace() or text[start - 1] in '([') and (end == len(text) or text[end].isspace() or text[end] in ']),')
    query = re.search(r'[?&][A-Za-z0-9_]{1,32}=$', prefix) is not None
    return sensitive or json_value or bearer or spaced or query


def _entropy_value(value: str) -> bool:
    if (len(value) < 32 or value.startswith(('sha256-', 'sha384-', 'sha512-'))
            or re.fullmatch(r'[A-Za-z]{2,12}-[A-Za-z0-9-]+', value)
            or _predictable_base64(value)):
        return False
    if not (any(c.islower() for c in value) and any(c.isupper() for c in value) and any(c.isdigit() for c in value)):
        return False
    if len(value) <= 256:
        return _entropy(value) >= 4.5
    # A long wrapper can dilute a secret. Check fixed windows with rolling
    # counts, rather than rescanning every suffix or masking by length alone.
    counts: Counter[str] = Counter()
    weighted = 0.0
    lower = upper = digits = 0
    for i, char in enumerate(value):
        changes = ((char, 1),) if i < 32 else ((char, 1), (value[i - 32], -1))
        for changed, delta in changes:
            old = counts[changed]
            new = old + delta
            weighted += (new * math.log2(new) if new else 0.0) - (old * math.log2(old) if old else 0.0)
            counts[changed] = new
            lower += delta * changed.islower()
            upper += delta * changed.isupper()
            digits += delta * changed.isdigit()
        if i >= 31 and lower and upper and digits and 5 - weighted / 32 >= 4.5:
            if not _predictable_base64(value[i - 31:i + 1]):
                return True
    return False


def _entropy_spans(value: str) -> Iterator[tuple[int, int]]:
    # Ordinary word/number path components delimit candidate values. Random
    # pieces separated by '/' or ':' remain one candidate, even below 32 each.
    if _predictable_base64(value):
        return
    components = list(_COMPONENT.finditer(value))
    start: int | None = None
    end = 0
    runs: list[tuple[int, int]] = []
    for component in components:
        ordinary = _WORD_COMPONENT.fullmatch(component.group()) is not None
        if ordinary:
            if start is not None:
                runs.append((start, end))
                start = None
        else:
            if start is None:
                start = component.start()
            end = component.end()
    if start is not None:
        runs.append((start, end))
    for start, end in runs:
        candidate = value[start:end]
        if _entropy_value(candidate):
            # When the URL's entire address is the secret, its scheme is part
            # of that value too. Ordinary host/path neighbours otherwise stay.
            scheme = re.search(r'https?://$', value[max(0, start - 8):start])
            if scheme is not None and end == len(value):
                start = max(0, start - 8) + scheme.start()
            yield start, end


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
    for start, end, kind in _secret_spans(decoded):
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
        findings.append(finding_limit(_safe_scan_path(rel_path)))
    return findings, _mask(text, offsets, spans)


def mask_output_text(value: str) -> str:
    """Compatibility entry point for the positive output grammar."""
    from .output import safe_value
    return safe_value(value)


def sanitize_findings(findings: list[Finding], values: set[str]) -> tuple[Finding, ...]:
    from .output import withheld
    fragments = {value[i:i + 8].lower() for value in values for i in range(len(value) - 7)}
    def clean(value: str) -> str:
        return withheld(value) if any(value[i:i + 8].lower() in fragments for i in range(len(value) - 7)) else value
    sanitized = []
    for finding in findings:
        location = finding.location
        if location is not None:
            location = replace(location, file=clean(location.file))
        violation = finding.capability_violation
        if violation is not None:
            violation = replace(violation, observed=clean(violation.observed), declared=clean(violation.declared))
        sanitized.append(replace(finding, location=location, capability_violation=violation))
    return tuple(sanitized)
