"""Positive output grammar; source text is never copied as diagnostic prose."""
from __future__ import annotations

import re
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .model import Finding

# Every rule is bounded. No entropy, decoding or match-by-match suffix scans.
_WORD = re.compile(r"[a-z]{1,24}(?:[0-9]{1,3})?")
_PLACEHOLDER = re.compile(r"<withheld len=[0-9]{1,10}>")
_SYSTEM_PARTS = frozenset({"Library", "Developer", "CommandLineTools", "Applications", "Xcode.app", "Contents", "MacOS", "Toolchains", "XcodeDefault.xctoolchain", "Platforms", "SDKs", "Users", "Cellar", "SKILL.md", "README.md", "LICENSE", "AGENTS.md"})
_FIXED = frozenset({"none", "repo", "home-config", "shell", "<dynamic>", "<dynamic-url>", "[withheld]", "<withheld>"})
_REASONS = frozenset({
    "scan truncated: per-file finding limit reached", "scan skipped: size exceeds text limit",
    "scan skipped: NUL byte (binary content)", "scan skipped: invalid UTF-8",
    "scan skipped: read error", "scan skipped: directory read error",
    "scan skipped: symbolic link", "scan skipped: non-regular file", "minified or bundled text",
    "Python syntax could not be parsed", "Python parser resource limit reached",
    "URL hostname could not be parsed",
    "scan skipped: manifest JSON could not be parsed within limits",
})
_EVIDENCE = {
    "static.shell.curl-pipe": "curl | sh",
    "static.python.shell-true": "shell=True",
    "static.shell.dangerous-rm": "rm -rf /",
}
_CALLS = frozenset({"subprocess.run", "subprocess.call", "subprocess.check_call", "subprocess.check_output", "subprocess.Popen", "os.system", "requests.get", "requests.post", "requests.put", "requests.delete", "requests.patch", "requests.head", "urllib.request.urlopen", "urllib.request.Request", "open", "Path", "pathlib.Path", "os.remove", "os.unlink", "shutil.rmtree"})


def withheld(value: str) -> str:
    return f"<withheld len={len(value)}>"


def _component(value: str) -> bool:
    if value in _SYSTEM_PARTS:
        return True
    if not value or len(value) > 48:
        return False
    # A word-based filename/label, not arbitrary mixed-case or numeric material.
    parts = re.split(r"[-_.]", value.lstrip('.'))
    return bool(parts) and all(_WORD.fullmatch(part) is not None for part in parts)


def safe_path(value: str) -> str:
    if _PLACEHOLDER.fullmatch(value):
        return value
    if not value or len(value) > 512:
        return withheld(value)
    parts = value.removeprefix('~/').lstrip('/').split('/')
    return value if all(_component(part) for part in parts) else withheld(value)


def safe_value(value: str) -> str:
    """Admit an entire bounded atom, never rescue fragments of a rejected value."""
    if value in _FIXED or _PLACEHOLDER.fullmatch(value):
        return value
    if '/' in value:
        return safe_path(value) if value.startswith(('/', '~/')) else withheld(value)
    # Commands and hostnames use the same lowercase word grammar. Environment
    # names have short uppercase words separated by underscores, with no digits.
    if '.' in value and len(value) <= 253 and all(_component(label) for label in value.split('.')):
        return value
    if len(value) <= 32 and _component(value):
        return value
    if len(value) <= 32 and re.fullmatch(r'[A-Z]{1,7}(?:_[A-Z]{1,7})*', value):
        return value
    return withheld(value)


def protect_finding(finding: Finding) -> None:
    # Called by the frozen Finding constructor, including replacement and cache
    # deserialization. No caller can opt out or attach trusted arbitrary prose.
    from .model import CapabilityViolation, Location, SECRET_REASONS, _ScanLocation

    for name in ('id', 'category', 'detector', 'confidence'):
        object.__setattr__(finding, name, safe_value(getattr(finding, name)))
    if finding.location is not None:
        cls = _ScanLocation if isinstance(finding.location, _ScanLocation) else Location
        object.__setattr__(finding, 'location', cls(safe_path(finding.location.file), finding.location.span))
    violation = finding.capability_violation
    if violation is not None:
        violation = CapabilityViolation(safe_value(violation.capability), safe_value(violation.declared), safe_value(violation.observed))
        object.__setattr__(finding, 'capability_violation', violation)
    evidence = finding.evidence
    if finding.id in SECRET_REASONS:
        return  # The closed secret schema already required its exact fixed reason.
    if finding.id in _EVIDENCE:
        evidence = _EVIDENCE[finding.id]
    elif evidence in _REASONS:
        pass
    elif violation is not None:
        # A fixed diagnostic label plus an allowlisted whole observation. Never
        # retain a URL, source line, parser error or backend-supplied prose.
        prefix = evidence.split(' -> ', 1)[0]
        evidence = f'{prefix} -> {violation.observed}' if prefix in _CALLS else violation.observed
    else:
        evidence = safe_value(evidence)
    object.__setattr__(finding, 'evidence', evidence)
