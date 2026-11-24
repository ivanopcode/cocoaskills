from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path

from . import identifiers, protocol_json, whitelist


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


_BLOCK_SCALAR_HEADER_RE = re.compile(r"^[|>](?:[+-]?[1-9]?|[1-9]?[+-]?)(?:[ \t]+#.*)?[ \t]*$")
_FORBIDDEN_PLAIN_KEY_CHARS = frozenset("{}[],&*!|>'\"%@`#")
_OPENAI_LOCALIZED_FIELDS = ("display_name", "short_description", "default_prompt")


@dataclass(frozen=True)
class _MappingEntry:
    key: str
    start: int
    end: int
    content_end: int
    header_colon: int


def analyze_locale(
    snapshot: Path,
    locale: str | None,
    *,
    exclude_roots: tuple[str, ...] = (),
) -> LocaleAnalysis:
    if not locale:
        return LocaleAnalysis(locale_to_render=None)

    metadata_path = snapshot / "locales" / "metadata.json"
    triggers_root = snapshot / ".skill_triggers"
    metadata_visible = not whitelist.is_below_excluded_root(
        Path("locales/metadata.json"),
        exclude_roots,
    )
    triggers_visible = not whitelist.is_below_excluded_root(
        Path(".skill_triggers"),
        exclude_roots,
    )
    has_locale_metadata = (
        metadata_visible and metadata_path.exists()
    ) or (
        triggers_visible and triggers_root.exists()
    )
    if not has_locale_metadata:
        return LocaleAnalysis(locale_to_render=None)

    issues: list[LocaleIssue] = []
    if triggers_visible and triggers_root.exists() and not triggers_root.is_dir():
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

    if not metadata_visible or not metadata_path.exists():
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

    invalid_locale = next(
        (
            key
            for key, value in locales.items()
            if not isinstance(key, str)
            or not identifiers.is_valid_locale(key)
            or not isinstance(value, dict)
        ),
        None,
    )
    if invalid_locale is not None:
        return LocaleAnalysis(
            locale_to_render=None,
            issues=(
                LocaleIssue(
                    "error",
                    "locale.metadata_invalid",
                    "locales/metadata.json",
                    f"Locale metadata {metadata_path} has invalid locale entry {invalid_locale!r}",
                ),
            ),
        )

    trigger_locales: set[str] = set()
    if triggers_visible and triggers_root.exists():
        trigger_locales = {
            path.stem
            for path in triggers_root.glob("*.md")
            if path.is_file()
            and not whitelist.is_below_excluded_root(path.relative_to(snapshot), exclude_roots)
        }
    consistent = {
        key
        for key, value in locales.items()
        if key in trigger_locales
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
        locale_data = locales[locale]
        description = locale_data.get("description")
        has_description = isinstance(description, str) and bool(description.strip())
        if not has_description:
            issues.append(
                LocaleIssue(
                    "warning",
                    "locale.description_missing",
                    "locales/metadata.json",
                    f"Locale {locale!r} in {metadata_path} does not define a non-empty string "
                    "'description'; leaving SKILL.md field 'description' as in source",
                )
            )
        issues.extend(
            _validate_render_targets(
                snapshot,
                locale_data,
                locale=locale,
                validate_skill_frontmatter=has_description,
                exclude_roots=exclude_roots,
            )
        )
        return LocaleAnalysis(locale_to_render=locale, issues=tuple(issues))

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


def render_locale(
    snapshot: Path,
    installed_dir: Path,
    locale: str | None,
    *,
    exclude_roots: tuple[str, ...] = (),
) -> tuple[LocaleIssue, ...]:
    analysis = analyze_locale(snapshot, locale, exclude_roots=exclude_roots)
    if analysis.failed:
        first_error = next(issue for issue in analysis.issues if issue.severity == "error")
        raise LocaleError(first_error.message)
    locale_to_render = analysis.locale_to_render
    if not locale_to_render:
        return analysis.issues
    metadata_path = snapshot / "locales" / "metadata.json"
    triggers_path = snapshot / ".skill_triggers" / f"{locale_to_render}.md"
    metadata = _load_metadata(metadata_path)
    locales = metadata.get("locales") if isinstance(metadata, dict) else None
    if not isinstance(locales, dict) or locale_to_render not in locales:
        raise LocaleError(f"Locale {locale_to_render!r} is not supported by {metadata_path}")
    locale_data = locales[locale_to_render]
    if not isinstance(locale_data, dict):
        raise LocaleError(f"Locale {locale_to_render!r} metadata must be an object")

    issues = list(analysis.issues)
    triggers = _parse_triggers(triggers_path)

    rendered_skill: str | None = None
    description = locale_data.get("description")
    if isinstance(description, str) and description.strip():
        skill_path = installed_dir / "SKILL.md"
        if not skill_path.is_file():
            raise LocaleError(f"Cannot localize missing SKILL.md: {skill_path}")
        try:
            rendered_skill = _render_skill_frontmatter(
                _read_utf8_exact(skill_path),
                description.strip(),
                triggers,
                path_label=str(skill_path),
            )
        except LocaleError as exc:
            if not any(issue.code == "locale.frontmatter_invalid" for issue in issues):
                issues.append(
                    LocaleIssue(
                        "warning",
                        "locale.frontmatter_invalid",
                        "SKILL.md",
                        f"Locale {locale_to_render!r} cannot rewrite SKILL.md frontmatter "
                        f"field 'description' ({exc}); leaving SKILL.md as in source",
                    )
                )

    openai_rel = Path("agents/openai.yaml")
    openai_path = installed_dir / openai_rel
    rendered_openai: str | None = None
    openai_fields = _extract_openai_fields(locale_data)
    if (
        openai_path.is_file()
        and not whitelist.is_below_excluded_root(openai_rel, exclude_roots)
        and openai_fields
    ):
        try:
            rendered_openai = _render_openai_yaml(
                _read_utf8_exact(openai_path),
                openai_fields,
                path_label=str(openai_path),
            )
        except LocaleError as exc:
            if not any(issue.code == "locale.openai_yaml_invalid" for issue in issues):
                fields_list = ", ".join(openai_fields)
                issues.append(
                    LocaleIssue(
                        "warning",
                        "locale.openai_yaml_invalid",
                        "agents/openai.yaml",
                        f"Locale {locale_to_render!r} cannot rewrite agents/openai.yaml "
                        f"field 'interface' ({fields_list}) ({exc}); leaving agents/openai.yaml as in source",
                    )
                )

    if rendered_skill is not None:
        _write_utf8_exact(installed_dir / "SKILL.md", rendered_skill)
    if rendered_openai is not None:
        _write_utf8_exact(openai_path, rendered_openai)
    return tuple(issues)


def _validate_render_targets(
    snapshot: Path,
    locale_data: dict[str, object],
    *,
    locale: str,
    validate_skill_frontmatter: bool,
    exclude_roots: tuple[str, ...],
) -> list[LocaleIssue]:
    issues: list[LocaleIssue] = []
    skill_path = snapshot / "SKILL.md"
    if validate_skill_frontmatter and skill_path.is_file():
        try:
            _parse_skill_frontmatter(
                _read_utf8_exact(skill_path),
                path_label="SKILL.md",
            )
        except LocaleError as exc:
            issues.append(
                LocaleIssue(
                    "warning",
                    "locale.frontmatter_invalid",
                    "SKILL.md",
                    f"Locale {locale!r} cannot rewrite SKILL.md frontmatter "
                    f"field 'description' ({exc}); leaving SKILL.md as in source",
                )
            )
    openai_rel = Path("agents/openai.yaml")
    openai_path = snapshot / openai_rel
    openai_fields = _extract_openai_fields(locale_data)
    if (
        openai_path.is_file()
        and not whitelist.is_below_excluded_root(openai_rel, exclude_roots)
        and openai_fields
    ):
        try:
            _render_openai_yaml(
                _read_utf8_exact(openai_path),
                openai_fields,
                path_label="agents/openai.yaml",
            )
        except LocaleError as exc:
            fields_list = ", ".join(openai_fields)
            issues.append(
                LocaleIssue(
                    "warning",
                    "locale.openai_yaml_invalid",
                    "agents/openai.yaml",
                    f"Locale {locale!r} cannot rewrite agents/openai.yaml "
                    f"field 'interface' ({fields_list}) ({exc}); leaving agents/openai.yaml as in source",
                )
            )
    return issues


def _read_utf8_exact(path: Path) -> str:
    return path.read_bytes().decode("utf-8")


def _write_utf8_exact(path: Path, text: str) -> None:
    path.write_bytes(text.encode("utf-8"))


def _load_metadata(path: Path) -> dict[str, object]:
    try:
        data = protocol_json.loads(protocol_json.read_bytes(path))
    except protocol_json.ProtocolJSONError as exc:
        raise LocaleError(f"Malformed locale metadata {path}: {exc}") from exc
    if not isinstance(data, dict):
        raise LocaleError(f"{path} must contain a JSON object")
    return data


def _parse_triggers(path: Path) -> list[str]:
    triggers: list[str] = []
    in_code = False
    for line in _read_utf8_exact(path).splitlines():
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


def _extract_openai_fields(data: dict[str, object]) -> dict[str, str]:
    extracted: dict[str, str] = {}
    for field in _OPENAI_LOCALIZED_FIELDS:
        value = data.get(field)
        if isinstance(value, str) and value.strip():
            extracted[field] = value.strip()
    return extracted


def _rewrite_skill_frontmatter(path: Path, description: str, triggers: list[str]) -> None:
    rendered = _render_skill_frontmatter(
        _read_utf8_exact(path),
        description,
        triggers,
        path_label=str(path),
    )
    _write_utf8_exact(path, rendered)


def _rewrite_openai_yaml(path: Path, data: dict[str, object]) -> None:
    fields = _extract_openai_fields(data)
    if not fields:
        return
    rendered = _render_openai_yaml(
        _read_utf8_exact(path),
        fields,
        path_label=str(path),
    )
    _write_utf8_exact(path, rendered)


def _detect_newline(lines: list[str]) -> str:
    for line in lines:
        if line.endswith("\r\n"):
            return "\r\n"
        if line.endswith("\n"):
            return "\n"
    return "\n"


def _strip_eol(line: str) -> str:
    if line.endswith("\r\n"):
        return line[:-2]
    if line.endswith("\n") or line.endswith("\r"):
        return line[:-1]
    return line


def _ensure_trailing_eol(lines: list[str], newline: str) -> list[str]:
    if not lines:
        return lines
    if lines[-1].endswith(("\n", "\r")):
        return lines
    return [*lines[:-1], lines[-1] + newline]


def _parse_skill_frontmatter(
    text: str,
    *,
    path_label: str,
) -> tuple[str, list[str], list[_MappingEntry], str, list[str], str]:
    raw_lines = text.splitlines(keepends=True)
    if not raw_lines:
        raise LocaleError(
            f"Cannot localize {path_label}: missing YAML frontmatter opening '---'"
        )
    opening_content = _strip_eol(raw_lines[0])
    if opening_content.rstrip(" \t") != "---":
        raise LocaleError(
            f"Cannot localize {path_label}: missing YAML frontmatter opening '---'"
        )
    end_index: int | None = None
    for index in range(1, len(raw_lines)):
        content = _strip_eol(raw_lines[index])
        if content.startswith("---") and content.rstrip(" \t") == "---":
            end_index = index
            break
    if end_index is None:
        raise LocaleError(
            f"Cannot localize {path_label}: unterminated YAML frontmatter (missing closing '---')"
        )
    frontmatter_lines = raw_lines[1:end_index]
    entries = _parse_block_mapping_spans(
        frontmatter_lines,
        base_indent=0,
        path_label=f"{path_label} frontmatter",
        line_offset=2,
    )
    newline = _detect_newline(raw_lines)
    return (
        raw_lines[0],
        frontmatter_lines,
        entries,
        raw_lines[end_index],
        raw_lines[end_index + 1 :],
        newline,
    )


def _render_skill_frontmatter(
    text: str,
    description: str,
    triggers: list[str],
    *,
    path_label: str,
) -> str:
    opening, frontmatter_lines, entries, closing, body_lines, newline = (
        _parse_skill_frontmatter(text, path_label=path_label)
    )
    replacements: dict[str, list[str]] = {
        "description": [f"description: {_quote(description)}{newline}"],
    }
    if triggers:
        replacements["triggers"] = [
            f"triggers:{newline}",
            *(f"  - {_quote(trigger)}{newline}" for trigger in triggers),
        ]
    updated_frontmatter = _apply_mapping_replacements(
        frontmatter_lines,
        entries,
        replacements,
        newline=newline,
    )
    return "".join([opening, *updated_frontmatter, closing, *body_lines])


def _render_openai_yaml(
    text: str,
    fields: dict[str, str],
    *,
    path_label: str,
) -> str:
    raw_lines = text.splitlines(keepends=True)
    newline = _detect_newline(raw_lines)
    entries = _parse_block_mapping_spans(
        raw_lines,
        base_indent=0,
        path_label=path_label,
        line_offset=1,
    )
    interface_entry = next((entry for entry in entries if entry.key == "interface"), None)
    if interface_entry is None:
        appended = [
            f"interface:{newline}",
            *(
                f"  {key}: {_quote(fields[key])}{newline}"
                for key in _OPENAI_LOCALIZED_FIELDS
                if key in fields
            ),
        ]
        return "".join([*_ensure_trailing_eol(raw_lines, newline), *appended])

    header_content = _strip_eol(raw_lines[interface_entry.start])
    rest = header_content[interface_entry.header_colon + 1 :]
    rest_code = _strip_yaml_comment(rest).strip()
    trailing_lines = raw_lines[interface_entry.content_end : interface_entry.end]

    if rest_code == "{}":
        if interface_entry.content_end > interface_entry.start + 1:
            raise LocaleError(
                f"Cannot localize {path_label}: unexpected lines after 'interface: {{}}'"
            )
        replacement_block = [
            f"interface:{newline}",
            *(
                f"  {key}: {_quote(fields[key])}{newline}"
                for key in _OPENAI_LOCALIZED_FIELDS
                if key in fields
            ),
            *trailing_lines,
        ]
        return "".join(
            [
                *raw_lines[: interface_entry.start],
                *replacement_block,
                *raw_lines[interface_entry.end :],
            ]
        )

    if rest_code != "":
        raise LocaleError(
            f"Cannot localize {path_label}: 'interface' must be a YAML block mapping or '{{}}'"
        )

    body_lines = raw_lines[interface_entry.start + 1 : interface_entry.content_end]
    child_indent = _detect_child_indent(body_lines, parent_indent=0, path_label=path_label)
    if child_indent is None:
        indent_str = "  "
        replacement_block = [
            raw_lines[interface_entry.start]
            if raw_lines[interface_entry.start].endswith(("\n", "\r"))
            else raw_lines[interface_entry.start] + newline,
            *(
                f"{indent_str}{key}: {_quote(fields[key])}{newline}"
                for key in _OPENAI_LOCALIZED_FIELDS
                if key in fields
            ),
            *trailing_lines,
        ]
        return "".join(
            [
                *raw_lines[: interface_entry.start],
                *replacement_block,
                *raw_lines[interface_entry.end :],
            ]
        )

    child_entries = _parse_block_mapping_spans(
        body_lines,
        base_indent=child_indent,
        path_label=f"{path_label} 'interface'",
        line_offset=interface_entry.start + 2,
    )
    indent_str = " " * child_indent
    child_replacements = {
        key: [f"{indent_str}{key}: {_quote(fields[key])}{newline}"]
        for key in _OPENAI_LOCALIZED_FIELDS
        if key in fields
    }
    updated_body = _apply_mapping_replacements(
        body_lines,
        child_entries,
        child_replacements,
        newline=newline,
    )
    header_line = raw_lines[interface_entry.start]
    if not header_line.endswith(("\n", "\r")):
        header_line += newline
    return "".join(
        [
            *raw_lines[: interface_entry.start],
            header_line,
            *updated_body,
            *trailing_lines,
            *raw_lines[interface_entry.end :],
        ]
    )


def _apply_mapping_replacements(
    lines: list[str],
    entries: list[_MappingEntry],
    replacements: dict[str, list[str]],
    *,
    newline: str,
) -> list[str]:
    result: list[str] = []
    handled: set[str] = set()
    cursor = 0
    for entry in entries:
        if entry.start > cursor:
            result.extend(lines[cursor : entry.start])
        if entry.key in replacements:
            result.extend(replacements[entry.key])
            result.extend(lines[entry.content_end : entry.end])
            handled.add(entry.key)
        else:
            result.extend(lines[entry.start : entry.end])
        cursor = entry.end
    if cursor < len(lines):
        result.extend(lines[cursor:])
    missing_keys = [key for key in replacements if key not in handled]
    if missing_keys:
        result = _ensure_trailing_eol(result, newline)
        for key in missing_keys:
            result.extend(replacements[key])
    return result


def _detect_child_indent(
    lines: list[str],
    *,
    parent_indent: int,
    path_label: str,
) -> int | None:
    for raw in lines:
        content = _strip_eol(raw)
        if not content.strip():
            continue
        indent = _leading_space_indent(content, path_label=path_label)
        if content[indent:].startswith("#"):
            continue
        if indent <= parent_indent:
            raise LocaleError(
                f"Cannot localize {path_label}: invalid nested indentation inside 'interface'"
            )
        return indent
    return None


def _leading_space_indent(content: str, *, path_label: str) -> int:
    indent = 0
    while indent < len(content) and content[indent] == " ":
        indent += 1
    if indent < len(content) and content[indent] == "\t":
        raise LocaleError(
            f"Cannot localize {path_label}: tab indentation is not valid YAML"
        )
    return indent


def _parse_block_mapping_spans(
    raw_lines: list[str],
    *,
    base_indent: int,
    path_label: str,
    line_offset: int,
) -> list[_MappingEntry]:
    entries: list[_MappingEntry] = []
    seen_keys: set[str] = set()
    index = 0
    total = len(raw_lines)

    while index < total:
        content = _strip_eol(raw_lines[index])
        if not content.strip():
            index += 1
            continue
        indent = _leading_space_indent(content, path_label=path_label)
        stripped = content[indent:]
        if stripped.startswith("#"):
            index += 1
            continue
        if indent != base_indent:
            raise LocaleError(
                f"Cannot localize {path_label}: unexpected indentation at line {line_offset + index}"
            )
        key, colon_pos = _parse_mapping_key(
            content,
            base_indent=base_indent,
            path_label=path_label,
            line_number=line_offset + index,
        )
        if key in seen_keys:
            raise LocaleError(
                f"Cannot localize {path_label}: duplicate key {key!r} at line {line_offset + index}"
            )
        seen_keys.add(key)

        rest = content[colon_pos + 1 :]
        rest_stripped = rest.lstrip(" \t")
        end_index, content_end = _consume_entry_value(
            raw_lines,
            start_index=index,
            rest=rest,
            rest_stripped=rest_stripped,
            base_indent=base_indent,
            path_label=path_label,
            line_offset=line_offset,
        )
        entries.append(
            _MappingEntry(
                key=key,
                start=index,
                end=end_index,
                content_end=content_end,
                header_colon=colon_pos,
            )
        )
        index = end_index

    return entries


def _parse_mapping_key(
    content: str,
    *,
    base_indent: int,
    path_label: str,
    line_number: int,
) -> tuple[str, int]:
    pos = base_indent
    if pos >= len(content):
        raise LocaleError(
            f"Cannot localize {path_label}: expected mapping key at line {line_number}"
        )
    first = content[pos]
    if first in {"-", "?", "{", "}", "[", "]", "&", "*", "!", "%", "@", "`"}:
        if first == "-" and (pos + 1 == len(content) or content[pos + 1] in {" ", "\t"}):
            raise LocaleError(
                f"Cannot localize {path_label}: expected mapping, got sequence item at line {line_number}"
            )
        if first == "?" and (pos + 1 == len(content) or content[pos + 1] in {" ", "\t"}):
            raise LocaleError(
                f"Cannot localize {path_label}: complex YAML keys are unsupported at line {line_number}"
            )
        if first in {"{", "}", "[", "]", "&", "*", "!", "%", "@", "`"}:
            raise LocaleError(
                f"Cannot localize {path_label}: unsupported YAML mapping syntax at line {line_number}"
            )

    if first == '"':
        end_quote = _scan_double_quoted(content, pos)
        if end_quote is None:
            raise LocaleError(
                f"Cannot localize {path_label}: unterminated quoted key at line {line_number}"
            )
        try:
            key = json.loads(content[pos : end_quote + 1])
        except json.JSONDecodeError as exc:
            raise LocaleError(
                f"Cannot localize {path_label}: invalid quoted key at line {line_number}"
            ) from exc
        if not isinstance(key, str):
            raise LocaleError(
                f"Cannot localize {path_label}: invalid quoted key at line {line_number}"
            )
        colon_pos = end_quote + 1
        while colon_pos < len(content) and content[colon_pos] == " ":
            colon_pos += 1
        if (
            colon_pos >= len(content)
            or content[colon_pos] != ":"
            or (colon_pos + 1 < len(content) and content[colon_pos + 1] not in {" ", "\t"})
        ):
            raise LocaleError(
                f"Cannot localize {path_label}: missing ':' after key at line {line_number}"
            )
        return key, colon_pos

    if first == "'":
        end_quote = _scan_single_quoted(content, pos)
        if end_quote is None:
            raise LocaleError(
                f"Cannot localize {path_label}: unterminated quoted key at line {line_number}"
            )
        key = content[pos + 1 : end_quote].replace("''", "'")
        colon_pos = end_quote + 1
        while colon_pos < len(content) and content[colon_pos] == " ":
            colon_pos += 1
        if (
            colon_pos >= len(content)
            or content[colon_pos] != ":"
            or (colon_pos + 1 < len(content) and content[colon_pos + 1] not in {" ", "\t"})
        ):
            raise LocaleError(
                f"Cannot localize {path_label}: missing ':' after key at line {line_number}"
            )
        return key, colon_pos

    colon_pos = -1
    for idx in range(pos, len(content)):
        if content[idx] == ":" and (
            idx + 1 == len(content) or content[idx + 1] in {" ", "\t"}
        ):
            colon_pos = idx
            break
    if colon_pos <= pos:
        raise LocaleError(
            f"Cannot localize {path_label}: invalid mapping entry at line {line_number}"
        )
    raw_key = content[pos:colon_pos].strip()
    if not raw_key or any(ch in _FORBIDDEN_PLAIN_KEY_CHARS for ch in raw_key) or raw_key == "<<":
        raise LocaleError(
            f"Cannot localize {path_label}: unsupported mapping key {raw_key!r} at line {line_number}"
        )
    return raw_key, colon_pos


def _consume_entry_value(
    raw_lines: list[str],
    *,
    start_index: int,
    rest: str,
    rest_stripped: str,
    base_indent: int,
    path_label: str,
    line_offset: int,
) -> tuple[int, int]:
    total = len(raw_lines)
    if rest_stripped.startswith(("|", ">")):
        if not _BLOCK_SCALAR_HEADER_RE.match(rest_stripped):
            raise LocaleError(
                f"Cannot localize {path_label}: invalid block scalar header at line {line_offset + start_index}"
            )
        index = start_index + 1
        last_non_blank = start_index
        while index < total:
            content = _strip_eol(raw_lines[index])
            if not content.strip():
                index += 1
                continue
            indent = _count_leading_spaces(content)
            if indent <= base_indent:
                break
            last_non_blank = index
            index += 1
        return index, last_non_blank + 1

    # Inline value (or empty rest followed by indented block / sequence).
    state = _FlowScanState()
    if rest_stripped and not rest_stripped.startswith("#"):
        _scan_inline_value_fragment(
            rest_stripped,
            state=state,
            path_label=path_label,
            line_number=line_offset + start_index,
        )

    index = start_index + 1
    last_non_blank = start_index
    nested_block_scalar_parent_indent: int | None = None
    allow_same_indent_seq = not rest_stripped or rest_stripped.startswith("#")

    while index < total:
        content = _strip_eol(raw_lines[index])
        if not content.strip():
            index += 1
            continue
        if nested_block_scalar_parent_indent is not None:
            raw_spaces = _count_leading_spaces(content)
            if raw_spaces > nested_block_scalar_parent_indent:
                last_non_blank = index
                index += 1
                continue
            nested_block_scalar_parent_indent = None

        indent = _leading_space_indent(content, path_label=path_label)
        stripped = content[indent:]
        if indent <= base_indent:
            if (
                allow_same_indent_seq
                and indent == base_indent
                and stripped.startswith("-")
                and (len(stripped) == 1 or stripped[1] in {" ", "\t"})
            ):
                _scan_nested_line(
                    stripped,
                    indent=indent,
                    state=state,
                    path_label=path_label,
                    line_number=line_offset + index,
                )
                nested_block_scalar_parent_indent = _nested_block_scalar_indent(
                    stripped, indent
                )
                last_non_blank = index
                index += 1
                continue
            if state.is_open():
                raise LocaleError(
                    f"Cannot localize {path_label}: unterminated YAML scalar or collection before line {line_offset + index}"
                )
            break

        if stripped.startswith("#") and not state.in_quote:
            index += 1
            continue

        _scan_nested_line(
            stripped,
            indent=indent,
            state=state,
            path_label=path_label,
            line_number=line_offset + index,
        )
        if not state.is_open():
            nested_block_scalar_parent_indent = _nested_block_scalar_indent(
                stripped, indent
            )
        last_non_blank = index
        index += 1

    if state.is_open():
        raise LocaleError(
            f"Cannot localize {path_label}: unterminated YAML scalar or collection starting at line {line_offset + start_index}"
        )
    return index, last_non_blank + 1


@dataclass
class _FlowScanState:
    in_quote: str | None = None
    brace_depth: int = 0
    bracket_depth: int = 0
    started_quoted_or_flow: bool = False
    closed_top_token: bool = False

    def is_open(self) -> bool:
        return (
            self.in_quote is not None
            or self.brace_depth > 0
            or self.bracket_depth > 0
        )


def _scan_inline_value_fragment(
    fragment: str,
    *,
    state: _FlowScanState,
    path_label: str,
    line_number: int,
) -> None:
    if fragment[0] not in {'"', "'", "{", "["}:
        return
    state.started_quoted_or_flow = True
    _scan_yaml_tokens(
        fragment,
        state=state,
        path_label=path_label,
        line_number=line_number,
        enforce_single_top_token=True,
    )


def _scan_nested_line(
    stripped: str,
    *,
    indent: int,
    state: _FlowScanState,
    path_label: str,
    line_number: int,
) -> None:
    del indent
    if state.is_open():
        _scan_yaml_tokens(
            stripped,
            state=state,
            path_label=path_label,
            line_number=line_number,
            enforce_single_top_token=state.started_quoted_or_flow,
        )
        return
    # Independent nested YAML line: check quotes/brackets on this line.
    if stripped.startswith("- ") or stripped == "-":
        rest = stripped[2:].lstrip(" \t") if len(stripped) > 2 else ""
    else:
        colon = _find_mapping_colon(stripped)
        rest = stripped[colon + 1 :].lstrip(" \t") if colon is not None else stripped
    if not rest or rest.startswith("#") or _BLOCK_SCALAR_HEADER_RE.match(rest):
        return
    if rest[0] in {'"', "'", "{", "["}:
        state.started_quoted_or_flow = True
        state.closed_top_token = False
        _scan_yaml_tokens(
            rest,
            state=state,
            path_label=path_label,
            line_number=line_number,
            enforce_single_top_token=True,
        )


def _nested_block_scalar_indent(stripped: str, line_indent: int) -> int | None:
    if stripped.startswith("- "):
        rest = stripped[2:].lstrip(" \t")
        if _BLOCK_SCALAR_HEADER_RE.match(rest):
            return line_indent
        colon = _find_mapping_colon(rest)
        if colon is not None and _BLOCK_SCALAR_HEADER_RE.match(rest[colon + 1 :].lstrip(" \t")):
            return line_indent
        return None
    colon = _find_mapping_colon(stripped)
    if colon is not None and _BLOCK_SCALAR_HEADER_RE.match(stripped[colon + 1 :].lstrip(" \t")):
        return line_indent
    return None


def _find_mapping_colon(text: str) -> int | None:
    if not text:
        return None
    if text[0] == '"':
        end = _scan_double_quoted(text, 0)
        if end is None:
            return None
        pos = end + 1
        while pos < len(text) and text[pos] == " ":
            pos += 1
        if pos < len(text) and text[pos] == ":" and (pos + 1 == len(text) or text[pos + 1] in {" ", "\t"}):
            return pos
        return None
    if text[0] == "'":
        end = _scan_single_quoted(text, 0)
        if end is None:
            return None
        pos = end + 1
        while pos < len(text) and text[pos] == " ":
            pos += 1
        if pos < len(text) and text[pos] == ":" and (pos + 1 == len(text) or text[pos + 1] in {" ", "\t"}):
            return pos
        return None
    for idx, ch in enumerate(text):
        if ch in {"{", "[", "#"}:
            return None
        if ch == ":" and (idx + 1 == len(text) or text[idx + 1] in {" ", "\t"}):
            return idx
    return None


def _scan_yaml_tokens(
    text: str,
    *,
    state: _FlowScanState,
    path_label: str,
    line_number: int,
    enforce_single_top_token: bool,
) -> None:
    idx = 0
    length = len(text)
    while idx < length:
        ch = text[idx]
        if state.in_quote == '"':
            if ch == "\\":
                idx += 2
                continue
            if ch == '"':
                state.in_quote = None
                if enforce_single_top_token and state.brace_depth == 0 and state.bracket_depth == 0:
                    state.closed_top_token = True
            idx += 1
            continue
        if state.in_quote == "'":
            if ch == "'":
                if idx + 1 < length and text[idx + 1] == "'":
                    idx += 2
                    continue
                state.in_quote = None
                if enforce_single_top_token and state.brace_depth == 0 and state.bracket_depth == 0:
                    state.closed_top_token = True
            idx += 1
            continue

        if ch in {" ", "\t"}:
            idx += 1
            continue
        if ch == "#" and (idx == 0 or text[idx - 1] in {" ", "\t"}):
            break
        if state.closed_top_token and enforce_single_top_token:
            raise LocaleError(
                f"Cannot localize {path_label}: unexpected trailing content at line {line_number}"
            )
        if ch == '"':
            state.in_quote = '"'
        elif ch == "'":
            state.in_quote = "'"
        elif ch == "{":
            state.brace_depth += 1
        elif ch == "}":
            if state.brace_depth == 0:
                raise LocaleError(
                    f"Cannot localize {path_label}: unmatched '}}' at line {line_number}"
                )
            state.brace_depth -= 1
            if enforce_single_top_token and state.brace_depth == 0 and state.bracket_depth == 0:
                state.closed_top_token = True
        elif ch == "[":
            state.bracket_depth += 1
        elif ch == "]":
            if state.bracket_depth == 0:
                raise LocaleError(
                    f"Cannot localize {path_label}: unmatched ']' at line {line_number}"
                )
            state.bracket_depth -= 1
            if enforce_single_top_token and state.brace_depth == 0 and state.bracket_depth == 0:
                state.closed_top_token = True
        idx += 1


def _scan_double_quoted(text: str, start: int) -> int | None:
    idx = start + 1
    while idx < len(text):
        ch = text[idx]
        if ch == "\\":
            idx += 2
            continue
        if ch == '"':
            return idx
        idx += 1
    return None


def _scan_single_quoted(text: str, start: int) -> int | None:
    idx = start + 1
    while idx < len(text):
        if text[idx] == "'":
            if idx + 1 < len(text) and text[idx + 1] == "'":
                idx += 2
                continue
            return idx
        idx += 1
    return None


def _count_leading_spaces(text: str) -> int:
    count = 0
    while count < len(text) and text[count] == " ":
        count += 1
    return count


def _strip_yaml_comment(text: str) -> str:
    in_quote: str | None = None
    idx = 0
    while idx < len(text):
        ch = text[idx]
        if in_quote == '"':
            if ch == "\\":
                idx += 2
                continue
            if ch == '"':
                in_quote = None
        elif in_quote == "'":
            if ch == "'":
                if idx + 1 < len(text) and text[idx + 1] == "'":
                    idx += 2
                    continue
                in_quote = None
        else:
            if ch in {'"', "'"}:
                in_quote = ch
            elif ch == "#" and (idx == 0 or text[idx - 1] in {" ", "\t"}):
                return text[:idx]
        idx += 1
    return text


def _quote(value: str) -> str:
    return json.dumps(value, ensure_ascii=False)
