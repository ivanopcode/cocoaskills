from __future__ import annotations

from .terminal import terminal_text

from dataclasses import asdict, dataclass
from pathlib import Path
from urllib.parse import urlsplit

from . import canonical_urls, git_ops, locale, skillspec, whitelist


@dataclass(frozen=True)
class ValidationIssue:
    severity: str
    code: str
    path: str
    message: str


def validate_skill(
    skill_dir: Path,
    *,
    locale_value: str | None = None,
    network: bool = False,
    check_dependency_urls: bool = True,
) -> list[ValidationIssue]:
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
        rejection = skillspec.script_execution_policy_rejection(spec)
        if rejection is not None:
            issues.append(
                ValidationIssue(
                    "error",
                    "skill.script_execution_policy_unsupported",
                    _skill_spec_path(skill_dir),
                    rejection,
                )
            )
        if check_dependency_urls or network:
            issues.extend(_dependency_url_issues(skill_dir, spec, network=network))
        issues.extend(_runtime_root_reference_warnings(skill_dir, spec))
        issues.extend(_command_resolution_warnings(skill_dir, spec))

    try:
        locale_analysis = locale.analyze_locale(
            skill_dir,
            locale_value,
            exclude_roots=spec.build_roots if spec is not None else (),
        )
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
    location = f" {terminal_text(issue.path)}" if issue.path else ""
    return f"{terminal_text(issue.severity)}: {terminal_text(issue.code)}{terminal_text(location)}: {terminal_text(issue.message)}"


def has_http_repository_urls(skill_dir: Path) -> bool:
    """Return whether the skill manifest declares any valid HTTP(S) repository URL."""
    try:
        spec = skillspec.load_skill_spec(skill_dir)
    except skillspec.SkillSpecError:
        return False
    for name in sorted(spec.requirements):
        try:
            spelling = git_ops.declared_fetch_url(spec.requirements[name].git, skill_dir)
        except git_ops.GitError:
            continue
        if urlsplit(spelling).scheme.lower() in {"http", "https"}:
            return True
    for name in sorted(spec.build_repositories):
        if urlsplit(spec.build_repositories[name].git).scheme.lower() in {"http", "https"}:
            return True
    return False


def _skill_spec_path(skill_dir: Path) -> str:
    return skillspec.manifest_source_path(skill_dir)


def _dependency_url_issues(
    skill_dir: Path,
    spec: skillspec.SkillSpec,
    *,
    network: bool,
) -> list[ValidationIssue]:
    if not spec.requirements and not spec.build_repositories:
        return []
    manifest_path = _skill_spec_path(skill_dir)
    issues: list[ValidationIssue] = []
    index = canonical_urls.SpellingIndex()
    http_fields: dict[str, list[str]] = {}
    for name in sorted(spec.requirements):
        field = f"dependencies.skills.{name}.git"
        raw_url = spec.requirements[name].git
        try:
            spelling = git_ops.declared_fetch_url(raw_url, skill_dir)
        except git_ops.GitError as exc:
            issues.append(
                ValidationIssue(
                    "error",
                    "skill.dependency_url_invalid",
                    manifest_path,
                    f"{field}: {git_ops.display_url(raw_url)!r}: {exc}",
                )
            )
            continue
        index.add(spelling, f"{manifest_path} ({field})")
        if urlsplit(spelling).scheme.lower() in {"http", "https"}:
            http_fields.setdefault(spelling, []).append(field)
    for name in sorted(spec.build_repositories):
        # Build repository URLs already pass build_repository.parse_repository_source
        # during load_skill_spec; they are excluded from SpellingIndex because neither
        # the closure guard nor csk check --canonical-urls indexes build repositories.
        field = f"build_repositories.{name}.git"
        raw_url = spec.build_repositories[name].git
        if urlsplit(raw_url).scheme.lower() in {"http", "https"}:
            http_fields.setdefault(raw_url, []).append(field)
    for _identity, message in index.conflicts():
        issues.append(
            ValidationIssue(
                "error",
                "skill.dependency_url_conflict",
                manifest_path,
                message,
            )
        )
    if not network:
        return issues
    for spelling in sorted(http_fields):
        fields_label = ", ".join(http_fields[spelling])
        shown = git_ops.display_url(spelling)
        try:
            probe = git_ops.probe_declared_url(spelling)
        except git_ops.GitError as exc:
            if exc.code is not None:
                raise
            issues.append(
                ValidationIssue(
                    "warning",
                    "skill.dependency_url_unverified",
                    manifest_path,
                    f"{fields_label}: not verified {shown!r}: {exc}",
                )
            )
            continue
        if probe.outcome == "redirect":
            issues.append(
                ValidationIssue(
                    "error",
                    "skill.dependency_url_redirect",
                    manifest_path,
                    f"{fields_label}: {git_ops.redirect_refusal(spelling, probe.canonical)}",
                )
            )
        elif probe.outcome == "unverified":
            issues.append(
                ValidationIssue(
                    "warning",
                    "skill.dependency_url_unverified",
                    manifest_path,
                    f"{fields_label}: not verified {shown!r}: {probe.detail or 'unknown failure'}",
                )
            )
    return issues


def _runtime_root_reference_warnings(
    skill_dir: Path,
    spec: skillspec.SkillSpec,
) -> list[ValidationIssue]:
    has_provider_runtime = any(dependency.type == "skill" for dependency in spec.dependencies.values()) or any(
        requirement.mode in {"full", "runtime"} for requirement in spec.requirements.values()
    )
    if not spec.runtime_roots and not spec.build_roots and not has_provider_runtime:
        return []
    warnings: list[ValidationIssue] = []
    for path in _prompt_markdown_files(skill_dir, exclude_roots=spec.build_roots):
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
        for build_root in spec.build_roots:
            windows_root = build_root.replace("/", "\\")
            tokens = (f"{build_root}/", f"{windows_root}\\")
            matched_token = next((token for token in tokens if token in text), None)
            if matched_token is None:
                continue
            warnings.append(
                ValidationIssue(
                    "warning",
                    "skill.build_root_in_prompt_context",
                    path.relative_to(skill_dir).as_posix(),
                    f"Prompt-visible text references build-only path {matched_token!r}; CocoaSkills removes "
                    "that root from installed skill context. Use the exported command placeholder for "
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
    managed_commands = [
        command for command in spec.commands.values() if command.type in {"script", "build"}
    ]
    has_provider_commands = any(dependency.type == "skill" for dependency in spec.dependencies.values()) or any(
        requirement.mode in {"full", "runtime"} for requirement in spec.requirements.values()
    )
    if not managed_commands and not has_provider_commands:
        return []

    text = "\n".join(
        path.read_text(encoding="utf-8", errors="replace")
        for path in _prompt_markdown_files(skill_dir, exclude_roots=spec.build_roots)
    )
    missing: list[str] = []
    if ".agents/bin" not in text and ".agents\\bin" not in text:
        missing.append("project .agents/bin lookup")
    if "global/bin" not in text and "global\\bin" not in text:
        missing.append("CocoaSkills global/bin fallback")
    if "command -v" not in text or "Get-Command" not in text:
        missing.append("validated POSIX and PowerShell bare-command fallbacks")
    has_windows_command = any(
        command.type == "build" or command.win_path is not None for command in managed_commands
    )
    if has_windows_command and ".cmd" not in text:
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


def _prompt_markdown_files(
    skill_dir: Path,
    *,
    exclude_roots: tuple[str, ...] = (),
) -> list[Path]:
    markdown_files: list[Path] = []
    for root in sorted(whitelist.INCLUDE_ROOTS):
        candidate = skill_dir / root
        if candidate.is_file() and candidate.suffix.lower() == ".md":
            if not _is_below_excluded_root(candidate, skill_dir, exclude_roots):
                markdown_files.append(candidate)
        elif candidate.is_dir():
            markdown_files.extend(
                path
                for path in candidate.rglob("*.md")
                if path.is_file() and not _is_below_excluded_root(path, skill_dir, exclude_roots)
            )
    return sorted(set(markdown_files))


def _is_below_excluded_root(path: Path, skill_dir: Path, exclude_roots: tuple[str, ...]) -> bool:
    relative_parts = path.relative_to(skill_dir).parts
    for root in exclude_roots:
        root_parts = tuple(root.split("/"))
        if relative_parts[: len(root_parts)] == root_parts:
            return True
    return False
