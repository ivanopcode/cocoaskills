from __future__ import annotations

import re
from collections.abc import Iterable
from pathlib import Path
from urllib.parse import urlparse

from .capabilities import CapabilityManifest
from .model import CapabilityViolation, Finding, Location, Severity, Surface


MAX_TEXT_BYTES = 1_000_000
TEXT_SUFFIXES = {
    "",
    ".bash",
    ".cfg",
    ".json",
    ".md",
    ".ps1",
    ".py",
    ".sh",
    ".toml",
    ".txt",
    ".yaml",
    ".yml",
    ".zsh",
}
SECRET_MARKERS = ("TOKEN", "SECRET", "PASSWORD", "PASS", "KEY", "PRIVATE")

_CURL_PIPE_RE = re.compile(r"\b(?:curl|wget)\b[^\n|;]*\|\s*(?:sh|bash|zsh|python)\b")
_PYTHON_SHELL_TRUE_RE = re.compile(r"\bshell\s*=\s*True\b")
_URL_RE = re.compile(r"https?://[A-Za-z0-9._~:/?#\[\]@!$&'()*+,;=%-]+")
_ENV_RE = re.compile(
    r"os\.environ(?:\.get)?\(\s*['\"]([A-Z_][A-Z0-9_]*)['\"]"
    r"|process\.env\.([A-Z_][A-Z0-9_]*)"
    r"|\$([A-Z_][A-Z0-9_]*)"
)
_DANGEROUS_RM_RE = re.compile(r"\brm\s+-rf\s+(?:/|\$HOME|~)(?:\s|$)")


def detect_snapshot(snapshot: Path, capabilities: CapabilityManifest) -> tuple[Finding, ...]:
    findings: list[Finding] = []
    for rel_path, text in _iter_text_files(snapshot):
        findings.extend(_detect_curl_pipe(rel_path, text))
        findings.extend(_detect_python_shell_true(rel_path, text))
        findings.extend(_detect_network_urls(rel_path, text, capabilities))
        findings.extend(_detect_secret_env(rel_path, text, capabilities))
        findings.extend(_detect_dangerous_rm(rel_path, text))
    return tuple(findings)


def _iter_text_files(snapshot: Path) -> Iterable[tuple[str, str]]:
    for path in sorted(snapshot.rglob("*")):
        if not path.is_file():
            continue
        if path.suffix not in TEXT_SUFFIXES:
            continue
        try:
            raw = path.read_bytes()
        except OSError:
            continue
        if len(raw) > MAX_TEXT_BYTES or b"\x00" in raw:
            continue
        try:
            text = raw.decode("utf-8")
        except UnicodeDecodeError:
            continue
        yield path.relative_to(snapshot).as_posix(), text


def _detect_curl_pipe(rel_path: str, text: str) -> list[Finding]:
    return [
        _finding(
            finding_id="static.shell.curl-pipe",
            category="shell-pipe-install",
            severity=Severity.HIGH,
            rel_path=rel_path,
            text=text,
            start=match.start(),
            evidence=match.group(0),
        )
        for match in _CURL_PIPE_RE.finditer(text)
    ]


def _detect_python_shell_true(rel_path: str, text: str) -> list[Finding]:
    return [
        _finding(
            finding_id="static.python.shell-true",
            category="shell-execution",
            severity=Severity.MEDIUM,
            rel_path=rel_path,
            text=text,
            start=match.start(),
            evidence=match.group(0),
        )
        for match in _PYTHON_SHELL_TRUE_RE.finditer(text)
    ]


def _detect_network_urls(rel_path: str, text: str, capabilities: CapabilityManifest) -> list[Finding]:
    findings: list[Finding] = []
    seen_hosts: set[str] = set()
    for match in _URL_RE.finditer(text):
        host = urlparse(match.group(0)).hostname
        if not host or host in seen_hosts or _host_allowed(host, capabilities.network):
            continue
        seen_hosts.add(host)
        findings.append(
            _finding(
                finding_id="static.network.undeclared-host",
                category="network",
                severity=Severity.MEDIUM,
                rel_path=rel_path,
                text=text,
                start=match.start(),
                evidence=match.group(0),
                violation=CapabilityViolation(
                    capability="network",
                    declared=", ".join(capabilities.network) or "none",
                    observed=host,
                ),
            )
        )
    return findings


def _detect_secret_env(rel_path: str, text: str, capabilities: CapabilityManifest) -> list[Finding]:
    findings: list[Finding] = []
    declared = set(capabilities.secrets) | set(capabilities.env_read)
    seen: set[str] = set()
    for match in _ENV_RE.finditer(text):
        name = next(group for group in match.groups() if group)
        if name in seen or name in declared or not any(marker in name for marker in SECRET_MARKERS):
            continue
        seen.add(name)
        findings.append(
            _finding(
                finding_id="static.env.undeclared-secret",
                category="secret-env",
                severity=Severity.MEDIUM,
                rel_path=rel_path,
                text=text,
                start=match.start(),
                evidence=name,
                violation=CapabilityViolation(
                    capability="secrets",
                    declared=", ".join(capabilities.secrets) or "none",
                    observed=name,
                ),
            )
        )
    return findings


def _detect_dangerous_rm(rel_path: str, text: str) -> list[Finding]:
    return [
        _finding(
            finding_id="static.shell.dangerous-rm",
            category="destructive-shell",
            severity=Severity.CRITICAL,
            rel_path=rel_path,
            text=text,
            start=match.start(),
            evidence=match.group(0),
        )
        for match in _DANGEROUS_RM_RE.finditer(text)
    ]


def _host_allowed(host: str, allowed: tuple[str, ...]) -> bool:
    if not allowed:
        return False
    return any(_match_host(host, pattern) for pattern in allowed)


def _match_host(host: str, pattern: str) -> bool:
    if pattern == "*":
        return True
    if pattern.startswith("*."):
        suffix = pattern[1:]
        return host.endswith(suffix) and host != pattern[2:]
    return host == pattern


def _finding(
    *,
    finding_id: str,
    category: str,
    severity: Severity,
    rel_path: str,
    text: str,
    start: int,
    evidence: str,
    violation: CapabilityViolation | None = None,
) -> Finding:
    line = text.count("\n", 0, start) + 1
    return Finding(
        id=finding_id,
        surface=Surface.CODE,
        category=category,
        severity=severity,
        location=Location(rel_path, (line, line)),
        evidence=_short_evidence(evidence),
        detector="static.regex",
        confidence="medium",
        verifiable=True,
        capability_violation=violation,
    )


def _short_evidence(value: str) -> str:
    normalized = " ".join(value.strip().split())
    return normalized[:160]
