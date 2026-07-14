from __future__ import annotations

from dataclasses import asdict, dataclass
from pathlib import Path

from . import locale, skillspec, whitelist


@dataclass(frozen=True)
class ValidationIssue:
    severity: str
    code: str
    path: str
    message: str


def validate_skill(skill_dir: Path, *, locale_value: str | None = None) -> list[ValidationIssue]:
    issues: list[ValidationIssue] = []
    spec: skillspec.SkillSpec | None = None
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
        spec = skillspec.load_skill_spec(skill_dir)
    except skillspec.SkillSpecError as exc:
        issues.append(
            ValidationIssue(
                "error",
                "skill.spec_invalid",
                _skill_spec_path(skill_dir),
                str(exc),
            )
        )

    if spec is not None:
        issues.extend(_runtime_root_reference_warnings(skill_dir, spec))
        issues.extend(_command_resolution_warnings(skill_dir, spec))

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


def issue_to_dict(issue: ValidationIssue) -> dict[str, str]:
    return asdict(issue)


def format_issue(issue: ValidationIssue) -> str:
    location = f" {issue.path}" if issue.path else ""
    return f"{issue.severity}: {issue.code}{location}: {issue.message}"


def _skill_spec_path(skill_dir: Path) -> str:
    return skillspec.manifest_source_path(skill_dir)


def _runtime_root_reference_warnings(
    skill_dir: Path,
    spec: skillspec.SkillSpec,
) -> list[ValidationIssue]:
    has_provider_runtime = any(dependency.type == "skill" for dependency in spec.dependencies.values()) or any(
        requirement.mode in {"full", "runtime"} for requirement in spec.requirements.values()
    )
    if not spec.runtime_roots and not has_provider_runtime:
        return []
    warnings: list[ValidationIssue] = []
    for path in _prompt_markdown_files(skill_dir):
        text = path.read_text(encoding="utf-8", errors="replace")
        for runtime_root in spec.runtime_roots:
            windows_root = runtime_root.replace("/", "\\")
            tokens = (f"{runtime_root}/", f"{windows_root}\\")
            matched_token = next((token for token in tokens if token in text), None)
            if matched_token is None:
                continue
            warnings.append(
                ValidationIssue(
                    "warning",
                    "skill.runtime_root_in_prompt_context",
                    path.relative_to(skill_dir).as_posix(),
                    f"Prompt-visible text references runtime-only path {matched_token!r}; CocoaSkills removes "
                    "that root from installed skill context. Use exported command placeholders for "
                    "installed execution and keep manifest-relative paths source-checkout-only.",
                )
            )
        if has_provider_runtime and ("scripts/" in text or "scripts\\" in text):
            warnings.append(
                ValidationIssue(
                    "warning",
                    "skill.provider_runtime_path_in_prompt_context",
                    path.relative_to(skill_dir).as_posix(),
                    "Prompt-visible text references a source scripts path while this skill consumes "
                    "another skill's runtime. Resolve the provider's exported command shim instead "
                    "of guessing its source layout.",
                )
            )
    return warnings


def _command_resolution_warnings(
    skill_dir: Path,
    spec: skillspec.SkillSpec,
) -> list[ValidationIssue]:
    script_commands = [command for command in spec.commands.values() if command.type == "script"]
    has_provider_commands = any(dependency.type == "skill" for dependency in spec.dependencies.values()) or any(
        requirement.mode in {"full", "runtime"} for requirement in spec.requirements.values()
    )
    if not script_commands and not has_provider_commands:
        return []

    text = "\n".join(
        path.read_text(encoding="utf-8", errors="replace") for path in _prompt_markdown_files(skill_dir)
    )
    missing: list[str] = []
    if ".agents/bin" not in text and ".agents\\bin" not in text:
        missing.append("project .agents/bin lookup")
    if "global/bin" not in text and "global\\bin" not in text:
        missing.append("CocoaSkills global/bin fallback")
    if "command -v" not in text or "Get-Command" not in text:
        missing.append("validated POSIX and PowerShell bare-command fallbacks")
    if any(command.win_path is not None for command in script_commands) and ".cmd" not in text:
        missing.append("Windows .cmd shim suffix")
    if not missing:
        return []
    return [
        ValidationIssue(
            "warning",
            "skill.command_resolution_contract_missing",
            "SKILL.md",
            "Prompt-visible instructions export managed runtime commands but do not document a shell-neutral "
            f"resolver ({', '.join(missing)}). Agents must resolve project shims first, then global shims, "
            "then a validated bare command; shell profile activation is optional.",
        )
    ]


def _prompt_markdown_files(skill_dir: Path) -> list[Path]:
    markdown_files: list[Path] = []
    for root in sorted(whitelist.INCLUDE_ROOTS):
        candidate = skill_dir / root
        if candidate.is_file() and candidate.suffix.lower() == ".md":
            markdown_files.append(candidate)
        elif candidate.is_dir():
            markdown_files.extend(path for path in candidate.rglob("*.md") if path.is_file())
    return sorted(set(markdown_files))
