from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

from . import protocol_json


class LocaleError(Exception):
    pass


@dataclass(frozen=True)
class LocaleIssue:
    severity: str
    code: str
    path: str
    message: str


@dataclass(frozen=True)
class LocaleAnalysis:
    locale_to_render: str | None
    issues: tuple[LocaleIssue, ...] = ()

    @property
    def failed(self) -> bool:
        return any(issue.severity == "error" for issue in self.issues)


def analyze_locale(snapshot: Path, locale: str | None) -> LocaleAnalysis:
    if not locale:
        return LocaleAnalysis(locale_to_render=None)

    metadata_path = snapshot / "locales" / "metadata.json"
    triggers_root = snapshot / ".skill_triggers"
    has_locale_metadata = metadata_path.exists() or triggers_root.exists()
    if not has_locale_metadata:
        return LocaleAnalysis(locale_to_render=None)

    issues: list[LocaleIssue] = []
    if triggers_root.exists() and not triggers_root.is_dir():
        return LocaleAnalysis(
            locale_to_render=None,
            issues=(
                LocaleIssue(
                    "error",
                    "locale.triggers_not_directory",
                    ".skill_triggers",
                    f"Locale trigger catalog must be a directory: {triggers_root}",
                ),
            ),
        )

    if not metadata_path.exists():
        return LocaleAnalysis(
            locale_to_render=None,
            issues=(
                LocaleIssue(
                    "error",
                    "locale.metadata_missing",
                    "locales/metadata.json",
                    f"Locale metadata missing: {metadata_path}",
                ),
            ),
        )

    metadata = _load_metadata(metadata_path)
    locales = metadata.get("locales") if isinstance(metadata, dict) else None
    if not isinstance(locales, dict):
        return LocaleAnalysis(
            locale_to_render=None,
            issues=(
                LocaleIssue(
                    "error",
                    "locale.metadata_invalid",
                    "locales/metadata.json",
                    f"Locale metadata {metadata_path} must contain object field 'locales'",
                ),
            ),
        )

    trigger_locales: set[str] = set()
    if triggers_root.exists():
        trigger_locales = {path.stem for path in triggers_root.glob("*.md") if path.is_file()}
    consistent = {
        key
        for key, value in locales.items()
        if isinstance(key, str) and isinstance(value, dict) and key in trigger_locales
    }
    if not consistent:
        return LocaleAnalysis(
            locale_to_render=None,
            issues=(
                LocaleIssue(
                    "error",
                    "locale.no_consistent_catalog",
                    "locales/metadata.json",
                    "Locale metadata and trigger catalogs have no matching supported locale",
                ),
            ),
        )

    if locale in consistent:
        return LocaleAnalysis(locale_to_render=locale)

    available = ", ".join(sorted(consistent))
    issues.append(
        LocaleIssue(
            "warning",
            "locale.selected_unavailable",
            "locales/metadata.json",
            f"Locale {locale!r} is not fully available; using source SKILL.md without localized rendering. "
            f"Available locale catalogs: {available}",
        )
    )
    return LocaleAnalysis(locale_to_render=None, issues=tuple(issues))


def render_locale(snapshot: Path, installed_dir: Path, locale: str | None) -> None:
    analysis = analyze_locale(snapshot, locale)
    if analysis.failed:
        first_error = next(issue for issue in analysis.issues if issue.severity == "error")
        raise LocaleError(first_error.message)
    locale_to_render = analysis.locale_to_render
    if not locale_to_render:
        return
    metadata_path = snapshot / "locales" / "metadata.json"
    triggers_path = snapshot / ".skill_triggers" / f"{locale_to_render}.md"
    metadata = _load_metadata(metadata_path)
    locales = metadata.get("locales") if isinstance(metadata, dict) else None
    if not isinstance(locales, dict) or locale_to_render not in locales:
        raise LocaleError(f"Locale {locale_to_render!r} is not supported by {metadata_path}")
    locale_data = locales[locale_to_render]
    if not isinstance(locale_data, dict):
        raise LocaleError(f"Locale {locale_to_render!r} metadata must be an object")

    description = locale_data.get("description")
    if isinstance(description, str) and description.strip():
        _rewrite_skill_frontmatter(installed_dir / "SKILL.md", description.strip(), _parse_triggers(triggers_path))

    openai_path = installed_dir / "agents" / "openai.yaml"
    if openai_path.exists():
        _rewrite_openai_yaml(openai_path, locale_data)


def _load_metadata(path: Path) -> dict[str, object]:
    try:
        data = protocol_json.loads(path.read_bytes())
    except protocol_json.ProtocolJSONError as exc:
        raise LocaleError(f"Malformed locale metadata {path}: {exc}") from exc
    if not isinstance(data, dict):
        raise LocaleError(f"{path} must contain a JSON object")
    return data


def _parse_triggers(path: Path) -> list[str]:
    triggers: list[str] = []
    in_code = False
    for line in path.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if stripped.startswith("```"):
            in_code = not in_code
            continue
        if in_code:
            continue
        if stripped.startswith("- "):
            value = stripped[2:].strip()
            if value:
                triggers.append(value.strip("\"'"))
    return triggers


def _rewrite_skill_frontmatter(path: Path, description: str, triggers: list[str]) -> None:
    text = path.read_text(encoding="utf-8")
    lines = text.splitlines()
    if not lines or lines[0].strip() != "---":
        return
    try:
        end_index = next(index for index in range(1, len(lines)) if lines[index].strip() == "---")
    except StopIteration:
        return
    frontmatter = lines[1:end_index]
    body = lines[end_index + 1 :]
    name_line = next((line for line in frontmatter if line.startswith("name:")), None)
    rendered = ["---"]
    if name_line:
        rendered.append(name_line)
    rendered.append(f"description: {_quote(description)}")
    if triggers:
        rendered.append("triggers:")
        for trigger in triggers:
            rendered.append(f"  - {_quote(trigger)}")
    rendered.append("---")
    rendered.extend(body)
    path.write_text("\n".join(rendered) + "\n", encoding="utf-8")


def _rewrite_openai_yaml(path: Path, data: dict[str, object]) -> None:
    fields = {
        "display_name": data.get("display_name"),
        "short_description": data.get("short_description"),
        "default_prompt": data.get("default_prompt"),
    }
    if not all(isinstance(value, str) and value for value in fields.values()):
        return
    path.write_text(
        "interface:\n"
        f"  display_name: {_quote(str(fields['display_name']))}\n"
        f"  short_description: {_quote(str(fields['short_description']))}\n"
        f"  default_prompt: {_quote(str(fields['default_prompt']))}\n",
        encoding="utf-8",
    )


def _quote(value: str) -> str:
    return json.dumps(value, ensure_ascii=False)
