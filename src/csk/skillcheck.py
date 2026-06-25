from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from . import locale, skillspec


@dataclass(frozen=True)
class ValidationIssue:
    severity: str
    code: str
    path: str
    message: str


def validate_skill(skill_dir: Path, *, locale_value: str | None = None) -> list[ValidationIssue]:
    issues: list[ValidationIssue] = []
    if not (skill_dir / "SKILL.md").is_file():
        issues.append(
            ValidationIssue(
                "error",
                "skill.missing_skill_md",
                "SKILL.md",
                f"Required SKILL.md not found in skill snapshot: {skill_dir}",
            )
        )

    try:
        skillspec.load_skill_spec(skill_dir)
    except skillspec.SkillSpecError as exc:
        issues.append(
            ValidationIssue(
                "error",
                "skill.spec_invalid",
                _skill_spec_path(skill_dir),
                str(exc),
            )
        )

    try:
        locale_analysis = locale.analyze_locale(skill_dir, locale_value)
    except locale.LocaleError as exc:
        issues.append(
            ValidationIssue(
                "error",
                "locale.metadata_malformed",
                "locales/metadata.json",
                str(exc),
            )
        )
    else:
        issues.extend(
            ValidationIssue(issue.severity, issue.code, issue.path, issue.message)
            for issue in locale_analysis.issues
        )
    return issues


def has_errors(issues: list[ValidationIssue]) -> bool:
    return any(issue.severity == "error" for issue in issues)


def _skill_spec_path(skill_dir: Path) -> str:
    if (skill_dir / "csk-skill.json").exists():
        return "csk-skill.json"
    if (skill_dir / "agents" / "runtime.json").exists():
        return "agents/runtime.json"
    return ""
