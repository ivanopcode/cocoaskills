from __future__ import annotations

from .. import protocol_json

import ast
from bisect import bisect_left
import json
import os
import re
import shlex
import stat
from collections.abc import Iterable
from dataclasses import replace
from itertools import chain, islice
from pathlib import Path
from urllib.parse import urlparse

from .secret_values import MAX_FINDINGS_PER_FILE, detect_and_mask, finding_limit, mask_output_text, sanitize_findings
from .capabilities import CapabilityManifest
from .model import SECRET_REASONS, CapabilityViolation, Finding, Location, Severity, Surface


MAX_TEXT_BYTES = 1_000_000
SECRET_MARKERS = ("TOKEN", "SECRET", "PASSWORD", "PASS", "KEY", "PRIVATE")

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


def detect_snapshot(
    snapshot: Path, capabilities: CapabilityManifest, *, inert_vendor_text: bool = False,
) -> tuple[Finding, ...]:
    findings: list[Finding] = []
    manifest_name = next((name for name in ("agent-skill.json", "csk-skill.json") if (snapshot / name).exists()), None)
    for item in _iter_text_files(snapshot):
        if isinstance(item, Finding):
            findings.extend(sanitize_findings([item], set()))
            continue
        rel_path, text = item
        secret_values: set[str] = set()
        produced = _file_findings(rel_path, text, capabilities, manifest_name, secret_values)
        bounded = list(islice(produced, MAX_FINDINGS_PER_FILE + 1))
        if len(bounded) > MAX_FINDINGS_PER_FILE:
            bounded = bounded[:MAX_FINDINGS_PER_FILE] + [finding_limit(rel_path)]
        if inert_vendor_text and _vendored_inert_text(snapshot / rel_path, rel_path):
            # External builds deny helpers/generators: inert dependency prose is
            # advisory. Secrets and critical findings still block everywhere.
            bounded = [replace(f, severity=Severity.MEDIUM)
                       if f.severity is Severity.HIGH and f.id not in SECRET_REASONS else f
                       for f in bounded]
        findings.extend(sanitize_findings(bounded, secret_values))
    return tuple(findings)


def _vendored_inert_text(path: Path, rel_path: str) -> bool:
    # Policy must use the scanned path, never its possibly withheld display value.
    if "vendor" not in Path(rel_path).parts[:-1]:
        return False
    try:
        info = path.lstat()
    except OSError:
        return False
    return stat.S_ISREG(info.st_mode) and not info.st_mode & 0o111


def _file_findings(rel_path: str, text: str, capabilities: CapabilityManifest, manifest_name: str | None, values: set[str]) -> Iterable[Finding]:
    secrets, _ = detect_and_mask(rel_path, text, values, max_findings=MAX_FINDINGS_PER_FILE)
    yield from secrets
    if any(f.id == 'static.opaque.finding-limit' for f in secrets):
        return
    python_source = Path(rel_path).suffix.lower() in {'.py', '.pyw', '.pyi'} or re.match(r'#![^\n]*\bpython[0-9.]*\b', text) is not None
    tree = _parse_python(text) if python_source else None
    for finding in chain(_detect_curl_pipe(rel_path, text), _detect_python_shell_true(rel_path, text),
                         _detect_network_urls(rel_path, text, capabilities),
                         _detect_secret_env(rel_path, text, capabilities), _detect_dangerous_rm(rel_path, text)):
        yield finding
    if tree is not None:
        yield from _detect_python_ast(rel_path, tree, capabilities)
    yield from _detect_minified_text(rel_path, text)
    if rel_path == manifest_name:
        yield from _detect_manifest(rel_path, text)


def _iter_text_files(snapshot: Path) -> Iterable[tuple[str, str] | Finding]:
    # Runtime roots and legacy script paths are unrestricted by suffix. Scan
    # every regular snapshot file, including custom roots and extensionless scripts.
    for entry in _walk_files(snapshot):
        if isinstance(entry, Finding):
            yield entry
            continue
        path = entry
        rel_path = path.relative_to(snapshot).as_posix()
        try:
            if path.is_symlink():
                yield _opaque_finding(rel_path, "scan skipped: symbolic link")
                continue
            if path.is_dir():
                continue
            metadata = path.stat()
            if not stat.S_ISREG(metadata.st_mode):
                yield _opaque_finding(rel_path, "scan skipped: non-regular file")
                continue
            if metadata.st_size > MAX_TEXT_BYTES:
                yield _opaque_finding(rel_path, "scan skipped: size exceeds text limit")
                continue
            with path.open("rb") as stream:
                raw = protocol_json._read_bounded(stream, MAX_TEXT_BYTES)
        except OSError:
            # Exception messages can contain package-derived data or host paths.
            yield _opaque_finding(rel_path, "scan skipped: read error")
            continue
        if len(raw) > MAX_TEXT_BYTES:
            yield _opaque_finding(rel_path, "scan skipped: size exceeds text limit")
            continue
        if b"\x00" in raw:
            yield _opaque_finding(rel_path, "scan skipped: NUL byte (binary content)")
            continue
        try:
            text = raw.decode("utf-8-sig")
        except UnicodeDecodeError:
            yield _opaque_finding(rel_path, "scan skipped: invalid UTF-8")
            continue
        yield rel_path, text


def _walk_files(snapshot: Path) -> Iterable[Path | Finding]:
    pending = [snapshot]
    while pending:
        directory = pending.pop()
        try:
            with os.scandir(directory) as entries:
                paths = sorted((Path(entry.path) for entry in entries), reverse=True)
        except OSError:
            yield _opaque_finding(directory.relative_to(snapshot).as_posix(), "scan skipped: directory read error")
            continue
        for path in paths:
            if not path.is_symlink() and path.is_dir():
                pending.append(path)
            else:
                yield path


def _detect_curl_pipe(rel_path: str, text: str) -> Iterable[Finding]:
    newlines = [i for i, char in enumerate(text) if char == "\n"]
    # Each segment is inspected once, rather than rescanning its suffix per curl.
    for segment in re.finditer(r"[^|;\n]+", text):
        end = segment.end()
        if end == len(text) or text[end] != '|':
            continue
        target = re.compile(r"\s*(?:sh|bash|zsh|python)\b").match(text, end + 1)
        source = re.search(r"\b(?:curl|wget)\b", segment.group())
        if target and source:
            start = segment.start() + source.start()
            yield (_finding(
                finding_id="static.shell.curl-pipe", category="shell-pipe-install",
                severity=Severity.HIGH, rel_path=rel_path, text=text, start=start,
                evidence=text[start:target.end()],
                line=bisect_left(newlines, start) + 1,
            ))


def _detect_python_shell_true(rel_path: str, text: str) -> Iterable[Finding]:
    for match in _PYTHON_SHELL_TRUE_RE.finditer(text):
        yield _finding(
            finding_id="static.python.shell-true",
            category="shell-execution",
            severity=Severity.MEDIUM,
            rel_path=rel_path,
            text=text,
            start=match.start(),
            evidence=match.group(0),
        )


def _detect_network_urls(rel_path: str, text: str, capabilities: CapabilityManifest) -> Iterable[Finding]:
    seen_hosts: set[str] = set()
    for match in _URL_RE.finditer(text):
        try:
            host = urlparse(match.group(0)).hostname
        except ValueError:
            yield _opaque_finding(rel_path, "URL hostname could not be parsed")
            continue
        if not host or host in seen_hosts or _host_allowed(host, capabilities.network):
            continue
        seen_hosts.add(host)
        safe_host = _safe_host(match.group(0), host)
        yield (
            _finding(
                finding_id="static.network.undeclared-host",
                category="network",
                severity=Severity.MEDIUM,
                rel_path=rel_path,
                text=text,
                start=match.start(),
                evidence=safe_host,
                violation=CapabilityViolation(
                    capability="network",
                    declared=", ".join(capabilities.network) or "none",
                    observed=safe_host,
                ),
            )
        )


def _detect_secret_env(rel_path: str, text: str, capabilities: CapabilityManifest) -> Iterable[Finding]:
    declared = set(capabilities.secrets) | set(capabilities.env_read)
    seen: set[str] = set()
    for match in _ENV_RE.finditer(text):
        name = next(group for group in match.groups() if group)
        if name in seen or name in declared or not any(marker in name for marker in SECRET_MARKERS):
            continue
        seen.add(name)
        yield (
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


def _detect_dangerous_rm(rel_path: str, text: str) -> Iterable[Finding]:
    for match in _DANGEROUS_RM_RE.finditer(text):
        yield _finding(
            finding_id="static.shell.dangerous-rm",
            category="destructive-shell",
            severity=Severity.CRITICAL,
            rel_path=rel_path,
            text=text,
            start=match.start(),
            evidence=match.group(0),
        )


def _parse_python(text: str) -> ast.Module | SyntaxError:
    try:
        return ast.parse(text)
    except SyntaxError:
        return SyntaxError("Python syntax could not be parsed")
    except (RecursionError, MemoryError):
        return SyntaxError("Python parser resource limit reached")


def _detect_python_ast(rel_path: str, tree: ast.Module | SyntaxError, capabilities: CapabilityManifest) -> Iterable[Finding]:
    if isinstance(tree, SyntaxError):
        yield (
            _ast_finding(
                finding_id="static.opaque.python-parse-error",
                category="opaque",
                severity=Severity.HIGH,
                rel_path=rel_path,
                node=tree,
                evidence=tree.msg,
            )
        )
        return
    aliases = _python_aliases(tree)
    for node in ast.walk(tree):
        if isinstance(node, ast.Subscript):
            yield from (_detect_python_env_subscript(rel_path, node, aliases, capabilities))
        if not isinstance(node, ast.Call):
            continue
        call_name = _call_name(node.func, aliases)
        if call_name == "<depth-limit>":
            yield _opaque_finding(rel_path, "Python parser resource limit reached")
            continue
        yield from (_detect_python_exec_call(rel_path, node, call_name, capabilities))
        yield from (_detect_python_network_call(rel_path, node, call_name, capabilities))
        yield from (_detect_python_filesystem_call(rel_path, node, call_name, capabilities))
        yield from (_detect_python_env_call(rel_path, node, call_name, capabilities))
        if call_name in {"eval", "exec", "compile"}:
            yield (
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
            yield (
                _ast_finding(
                    finding_id="static.python.dynamic-import",
                    category="opaque",
                    severity=Severity.HIGH,
                    rel_path=rel_path,
                    node=node,
                    evidence=call_name,
                )
            )


def _detect_python_exec_call(
    rel_path: str,
    node: ast.Call,
    call_name: str,
    capabilities: CapabilityManifest,
) -> list[Finding]:
    if call_name not in {"os.system", "subprocess.call", "subprocess.check_call", "subprocess.check_output", "subprocess.Popen", "subprocess.run"}:
        return []
    argument = _executable_argument(node)
    observed = Path(argument).name if argument else None
    if observed is None:
        observed = "shell" if _keyword_is_true(node, "shell") else "<dynamic>"
    if observed in capabilities.exec:
        return []
    if argument is not None:
        safe_argument = mask_output_text(argument)
        if safe_argument != argument:
            observed = safe_argument
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


def _safe_host(url: str, host: str) -> str:
    # Only the independently allowlisted hostname may leave; never the URL tail.
    try:
        authority = urlparse(url).netloc.rsplit('@', 1)[-1]
    except ValueError:
        return "<withheld>"
    original_host = authority.rsplit(':', 1)[0] if ':' in authority and not authority.startswith('[') else authority
    return host if mask_output_text(original_host) == original_host else mask_output_text(original_host)


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
        try:
            observed = urlparse(url).hostname or "<dynamic-url>"
        except ValueError:
            return [_opaque_finding(rel_path, "URL hostname could not be parsed")]
    if observed != "<dynamic-url>" and _host_allowed(observed, capabilities.network):
        return []
    if url is not None:
        observed = _safe_host(url, observed)
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
    parts: list[str] = []
    while isinstance(node, ast.Attribute):
        parts.append(node.attr)
        if len(parts) > 64:
            return "<depth-limit>"  # No supported detector call has this many attributes.
        node = node.value
    if isinstance(node, ast.Name):
        parts.append(aliases.get(node.id, node.id))
    return '.'.join(reversed(parts))


def _executable_argument(node: ast.Call) -> str | None:
    if not node.args:
        return None
    first = node.args[0]
    if isinstance(first, ast.Constant) and isinstance(first.value, str):
        try:
            parts = shlex.split(first.value)
        except ValueError:
            parts = first.value.split()
        return parts[0] if parts else None
    if isinstance(first, (ast.List, ast.Tuple)) and first.elts:
        head = first.elts[0]
        if isinstance(head, ast.Constant) and isinstance(head.value, str):
            return head.value
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


def _detect_manifest(manifest_name: str, text: str) -> list[Finding]:
    try:
        data = protocol_json.loads_compatible(text)
    except protocol_json.ProtocolJSONError:
        return [_opaque_finding(manifest_name, "scan skipped: manifest JSON could not be parsed within limits")]
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


def _detect_minified_text(rel_path: str, text: str) -> list[Finding]:
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
    line: int | None = None,
) -> Finding:
    line = line if line is not None else text.count("\n", 0, start) + 1
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
    return normalized
