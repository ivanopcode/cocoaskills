from __future__ import annotations

import ast
import json
import re
import shlex
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
COMMON_SYSTEM_COMMANDS = {
    "bash",
    "brew",
    "curl",
    "git",
    "make",
    "pip",
    "python",
    "python3",
    "rm",
    "sh",
    "ssh",
    "sudo",
    "zsh",
}
OPAQUE_PREFIXES = ("agents/", "references/", "scripts/")


def detect_snapshot(snapshot: Path, capabilities: CapabilityManifest) -> tuple[Finding, ...]:
    findings: list[Finding] = []
    for rel_path, text in _iter_text_files(snapshot):
        findings.extend(_detect_curl_pipe(rel_path, text))
        findings.extend(_detect_python_shell_true(rel_path, text))
        findings.extend(_detect_network_urls(rel_path, text, capabilities))
        findings.extend(_detect_secret_env(rel_path, text, capabilities))
        findings.extend(_detect_dangerous_rm(rel_path, text))
        if rel_path.endswith(".py"):
            findings.extend(_detect_python_ast(rel_path, text, capabilities))
        findings.extend(_detect_minified_text(rel_path, text))
    findings.extend(_detect_manifest(snapshot))
    findings.extend(_detect_opaque_files(snapshot))
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


def _detect_python_ast(rel_path: str, text: str, capabilities: CapabilityManifest) -> list[Finding]:
    try:
        tree = ast.parse(text)
    except SyntaxError as exc:
        return [
            _ast_finding(
                finding_id="static.opaque.python-parse-error",
                category="opaque",
                severity=Severity.HIGH,
                rel_path=rel_path,
                node=exc,
                evidence=str(exc),
            )
        ]
    aliases = _python_aliases(tree)
    findings: list[Finding] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Subscript):
            findings.extend(_detect_python_env_subscript(rel_path, node, aliases, capabilities))
        if not isinstance(node, ast.Call):
            continue
        call_name = _call_name(node.func, aliases)
        findings.extend(_detect_python_exec_call(rel_path, node, call_name, capabilities))
        findings.extend(_detect_python_network_call(rel_path, node, call_name, capabilities))
        findings.extend(_detect_python_filesystem_call(rel_path, node, call_name, capabilities))
        findings.extend(_detect_python_env_call(rel_path, node, call_name, capabilities))
        if call_name in {"eval", "exec", "compile"}:
            findings.append(
                _ast_finding(
                    finding_id="static.python.dynamic-exec",
                    category="opaque",
                    severity=Severity.HIGH,
                    rel_path=rel_path,
                    node=node,
                    evidence=call_name,
                )
            )
        if call_name in {"__import__", "importlib.import_module"} and not _first_arg_constant_string(node):
            findings.append(
                _ast_finding(
                    finding_id="static.python.dynamic-import",
                    category="opaque",
                    severity=Severity.HIGH,
                    rel_path=rel_path,
                    node=node,
                    evidence=call_name,
                )
            )
    return findings


def _detect_python_exec_call(
    rel_path: str,
    node: ast.Call,
    call_name: str,
    capabilities: CapabilityManifest,
) -> list[Finding]:
    if call_name not in {"os.system", "subprocess.call", "subprocess.check_call", "subprocess.check_output", "subprocess.Popen", "subprocess.run"}:
        return []
    observed = _observed_executable(node)
    if observed is None:
        observed = "shell" if _keyword_is_true(node, "shell") else "<dynamic>"
    if observed in capabilities.exec:
        return []
    return [
        _ast_finding(
            finding_id="static.python.undeclared-exec",
            category="capability-escalation",
            severity=Severity.HIGH,
            rel_path=rel_path,
            node=node,
            evidence=f"{call_name} -> {observed}",
            violation=CapabilityViolation(
                capability="exec",
                declared=", ".join(capabilities.exec) or "none",
                observed=observed,
            ),
        )
    ]


def _detect_python_network_call(
    rel_path: str,
    node: ast.Call,
    call_name: str,
    capabilities: CapabilityManifest,
) -> list[Finding]:
    if call_name not in {
        "requests.delete",
        "requests.get",
        "requests.patch",
        "requests.post",
        "requests.put",
        "urllib.request.urlopen",
        "urllib.request.Request",
    }:
        return []
    url = _first_arg_constant_string(node)
    if url is None:
        observed = "<dynamic-url>"
    else:
        observed = urlparse(url).hostname or "<dynamic-url>"
    if observed != "<dynamic-url>" and _host_allowed(observed, capabilities.network):
        return []
    return [
        _ast_finding(
            finding_id="static.python.undeclared-network",
            category="capability-escalation",
            severity=Severity.MEDIUM,
            rel_path=rel_path,
            node=node,
            evidence=f"{call_name} -> {observed}",
            violation=CapabilityViolation(
                capability="network",
                declared=", ".join(capabilities.network) or "none",
                observed=observed,
            ),
        )
    ]


def _detect_python_filesystem_call(
    rel_path: str,
    node: ast.Call,
    call_name: str,
    capabilities: CapabilityManifest,
) -> list[Finding]:
    if call_name not in {"open", "Path", "pathlib.Path", "os.remove", "os.unlink", "shutil.rmtree"}:
        return []
    path = _first_arg_constant_string(node)
    if path is None or _filesystem_allowed(path, capabilities.filesystem):
        return []
    return [
        _ast_finding(
            finding_id="static.python.filesystem-outside-envelope",
            category="capability-escalation",
            severity=Severity.MEDIUM,
            rel_path=rel_path,
            node=node,
            evidence=f"{call_name} -> {path}",
            violation=CapabilityViolation(
                capability="filesystem",
                declared=_format_filesystem(capabilities.filesystem),
                observed=path,
            ),
        )
    ]


def _detect_python_env_call(
    rel_path: str,
    node: ast.Call,
    call_name: str,
    capabilities: CapabilityManifest,
) -> list[Finding]:
    if call_name not in {"os.environ.get", "os.getenv"}:
        return []
    name = _first_arg_constant_string(node)
    declared = set(capabilities.secrets) | set(capabilities.env_read)
    if name is None or name in declared or not any(marker in name for marker in SECRET_MARKERS):
        return []
    return [
        _ast_finding(
            finding_id="static.python.undeclared-secret",
            category="secret-env",
            severity=Severity.MEDIUM,
            rel_path=rel_path,
            node=node,
            evidence=name,
            violation=CapabilityViolation(
                capability="secrets",
                declared=", ".join(capabilities.secrets) or "none",
                observed=name,
            ),
        )
    ]


def _detect_python_env_subscript(
    rel_path: str,
    node: ast.Subscript,
    aliases: dict[str, str],
    capabilities: CapabilityManifest,
) -> list[Finding]:
    if _call_name(node.value, aliases) != "os.environ":
        return []
    name = _constant_string_slice(node.slice)
    declared = set(capabilities.secrets) | set(capabilities.env_read)
    if name is None or name in declared or not any(marker in name for marker in SECRET_MARKERS):
        return []
    return [
        _ast_finding(
            finding_id="static.python.undeclared-secret",
            category="secret-env",
            severity=Severity.MEDIUM,
            rel_path=rel_path,
            node=node,
            evidence=name,
            violation=CapabilityViolation(
                capability="secrets",
                declared=", ".join(capabilities.secrets) or "none",
                observed=name,
            ),
        )
    ]


def _python_aliases(tree: ast.AST) -> dict[str, str]:
    aliases: dict[str, str] = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.asname:
                    aliases[alias.asname] = alias.name
                else:
                    root = alias.name.split(".", 1)[0]
                    aliases[root] = root
        elif isinstance(node, ast.ImportFrom) and node.module:
            for alias in node.names:
                name = alias.asname or alias.name
                aliases[name] = f"{node.module}.{alias.name}"
    return aliases


def _call_name(node: ast.AST, aliases: dict[str, str]) -> str:
    if isinstance(node, ast.Name):
        return aliases.get(node.id, node.id)
    if isinstance(node, ast.Attribute):
        parent = _call_name(node.value, aliases)
        return f"{parent}.{node.attr}" if parent else node.attr
    return ""


def _observed_executable(node: ast.Call) -> str | None:
    if not node.args:
        return None
    first = node.args[0]
    if isinstance(first, ast.Constant) and isinstance(first.value, str):
        try:
            parts = shlex.split(first.value)
        except ValueError:
            parts = first.value.split()
        return Path(parts[0]).name if parts else None
    if isinstance(first, (ast.List, ast.Tuple)) and first.elts:
        head = first.elts[0]
        if isinstance(head, ast.Constant) and isinstance(head.value, str):
            return Path(head.value).name
    return None


def _first_arg_constant_string(node: ast.Call) -> str | None:
    if not node.args:
        return None
    first = node.args[0]
    return first.value if isinstance(first, ast.Constant) and isinstance(first.value, str) else None


def _keyword_is_true(node: ast.Call, name: str) -> bool:
    return any(keyword.arg == name and isinstance(keyword.value, ast.Constant) and keyword.value.value is True for keyword in node.keywords)


def _constant_string_slice(node: ast.AST) -> str | None:
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    return None


def _filesystem_allowed(path: str, allowed: str | tuple[str, ...]) -> bool:
    if not path.startswith(("/", "~/")):
        return True
    if allowed == "home-config":
        return path.startswith("~/") or path.startswith(str(Path.home()))
    if allowed == "repo":
        return False
    return any(path == prefix or path.startswith(prefix.rstrip("/") + "/") for prefix in allowed)


def _format_filesystem(value: str | tuple[str, ...]) -> str:
    return value if isinstance(value, str) else ", ".join(value)


def _ast_finding(
    *,
    finding_id: str,
    category: str,
    severity: Severity,
    rel_path: str,
    node: ast.AST | SyntaxError,
    evidence: str,
    violation: CapabilityViolation | None = None,
) -> Finding:
    line = getattr(node, "lineno", 1) or 1
    return Finding(
        id=finding_id,
        surface=Surface.CODE,
        category=category,
        severity=severity,
        location=Location(rel_path, (line, line)),
        evidence=_short_evidence(evidence),
        detector="static.python",
        confidence="medium",
        verifiable=True,
        capability_violation=violation,
    )


def _detect_manifest(snapshot: Path) -> list[Finding]:
    manifest_name = next(
        (name for name in ("agent-skill.json", "csk-skill.json") if (snapshot / name).exists()),
        None,
    )
    if manifest_name is None:
        return []
    path = snapshot / manifest_name
    if not path.exists():
        return []
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return []
    commands = data.get("commands", {}) if isinstance(data, dict) else {}
    if not isinstance(commands, dict):
        return []
    findings: list[Finding] = []
    for name in sorted(commands):
        if name in COMMON_SYSTEM_COMMANDS:
            findings.append(
                Finding(
                    id="static.manifest.command-shadows-system",
                    surface=Surface.MANIFEST,
                    category="hygiene",
                    severity=Severity.MEDIUM,
                    location=Location(manifest_name),
                    evidence=name,
                    detector="static.manifest",
                    confidence="high",
                    verifiable=True,
                )
            )
    return findings


def _detect_opaque_files(snapshot: Path) -> list[Finding]:
    findings: list[Finding] = []
    for path in sorted(snapshot.rglob("*")):
        if not path.is_file():
            continue
        rel_path = path.relative_to(snapshot).as_posix()
        if not rel_path.startswith(OPAQUE_PREFIXES):
            continue
        try:
            raw = path.read_bytes()
        except OSError:
            continue
        if b"\x00" in raw:
            findings.append(_opaque_finding(rel_path, "binary content"))
    return findings


def _detect_minified_text(rel_path: str, text: str) -> list[Finding]:
    if not rel_path.startswith(OPAQUE_PREFIXES):
        return []
    if len(text) < 10_000:
        return []
    lines = text.splitlines() or [text]
    if len(lines) <= 3 or max(len(line) for line in lines) > 5_000:
        return [_opaque_finding(rel_path, "minified or bundled text")]
    return []


def _opaque_finding(rel_path: str, evidence: str) -> Finding:
    return Finding(
        id="static.opaque.unanalyzable-artifact",
        surface=Surface.CODE,
        category="opaque",
        severity=Severity.HIGH,
        location=Location(rel_path),
        evidence=evidence,
        detector="static.opaque",
        confidence="high",
        verifiable=True,
    )


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
