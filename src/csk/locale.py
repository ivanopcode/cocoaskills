from __future__ import annotations

import json
from pathlib import Path


class LocaleError(Exception):
    pass


def render_locale(snapshot: Path, installed_dir: Path, locale: str | None) -> None:
    if not locale:
        return
    metadata_path = snapshot / "locales" / "metadata.json"
    triggers_path = snapshot / ".skill_triggers" / f"{locale}.md"
    has_locale_metadata = metadata_path.exists() or (snapshot / ".skill_triggers").exists()
    if not has_locale_metadata:
        return
    if not metadata_path.exists():
        raise LocaleError(f"Locale metadata missing: {metadata_path}")
    if not triggers_path.exists():
        raise LocaleError(f"Trigger catalog for locale {locale!r} missing: {triggers_path}")

    try:
        metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise LocaleError(f"Malformed locale metadata {metadata_path}: {exc}") from exc
    locales = metadata.get("locales") if isinstance(metadata, dict) else None
    if not isinstance(locales, dict) or locale not in locales:
        raise LocaleError(f"Locale {locale!r} is not supported by {metadata_path}")
    locale_data = locales[locale]
    if not isinstance(locale_data, dict):
        raise LocaleError(f"Locale {locale!r} metadata must be an object")

    description = locale_data.get("description")
    if isinstance(description, str) and description.strip():
        _rewrite_skill_frontmatter(installed_dir / "SKILL.md", description.strip(), _parse_triggers(triggers_path))

    openai_path = installed_dir / "agents" / "openai.yaml"
    if openai_path.exists():
        _rewrite_openai_yaml(openai_path, locale_data)


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

