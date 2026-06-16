from __future__ import annotations

import fnmatch
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlparse


class SourcePolicyError(Exception):
    pass


@dataclass(frozen=True)
class SourcePolicyRule:
    pattern: str
    source_class: str


@dataclass(frozen=True)
class SourcePolicy:
    default_class: str = "internal"
    rules: tuple[SourcePolicyRule, ...] = ()

    def classify(self, source: str | None, git: str | None = None) -> str:
        normalized = normalize_source(git or source or "")
        if not normalized or _is_local_source(git or source or ""):
            return "internal"
        for rule in self.rules:
            if fnmatch.fnmatchcase(normalized, rule.pattern):
                return rule.source_class
        return self.default_class


def parse_source_policy(raw: Any) -> SourcePolicy:
    if raw is None:
        return SourcePolicy()
    if not isinstance(raw, dict):
        raise SourcePolicyError("audit.source_policy must be an object")
    _reject_unknown_fields(raw, {"default_class", "rules"}, "audit.source_policy")
    default_class = raw.get("default_class", "internal")
    _validate_source_class(default_class, "audit.source_policy.default_class")
    rules_raw = raw.get("rules", [])
    if not isinstance(rules_raw, list):
        raise SourcePolicyError("audit.source_policy.rules must be a list")
    rules: list[SourcePolicyRule] = []
    for index, item in enumerate(rules_raw):
        if not isinstance(item, dict):
            raise SourcePolicyError(f"audit.source_policy.rules[{index}] must be an object")
        _reject_unknown_fields(item, {"pattern", "class"}, f"audit.source_policy.rules[{index}]")
        pattern = item.get("pattern")
        source_class = item.get("class")
        if not isinstance(pattern, str) or not pattern:
            raise SourcePolicyError(f"audit.source_policy.rules[{index}].pattern must be a non-empty string")
        _validate_source_class(source_class, f"audit.source_policy.rules[{index}].class")
        rules.append(SourcePolicyRule(pattern=pattern, source_class=source_class))
    return SourcePolicy(default_class=default_class, rules=tuple(rules))


def normalize_source(source: str) -> str:
    if not source:
        return ""
    parsed = urlparse(source)
    if parsed.scheme in {"http", "https", "ssh"} and parsed.hostname:
        return parsed.hostname
    if parsed.scheme == "file":
        return source
    if "@" in source and ":" in source and not source.startswith("/"):
        host_part = source.split("@", 1)[1].split(":", 1)[0]
        return host_part
    return source


def _is_local_source(source: str) -> bool:
    if not source:
        return True
    parsed = urlparse(source)
    if parsed.scheme == "file":
        return True
    if source.startswith(("/", "./", "../", "~/")):
        return True
    return False


def _validate_source_class(value: Any, field: str) -> None:
    if value not in {"internal", "public"}:
        raise SourcePolicyError(f"{field} must be internal or public")


def _reject_unknown_fields(data: dict[str, Any], allowed: set[str], label: str) -> None:
    unknown = sorted(set(data) - allowed)
    if unknown:
        joined = ", ".join(repr(item) for item in unknown)
        raise SourcePolicyError(f"{label} has unsupported field(s): {joined}")
