from __future__ import annotations

import json

from csk import skillcheck


def test_validate_clean_skill(tmp_path):
    (tmp_path / "SKILL.md").write_text("---\nname: skill\n---\n", encoding="utf-8")

    issues = skillcheck.validate_skill(tmp_path, locale_value="ru")

    assert issues == []


def test_validate_missing_skill_md_is_error(tmp_path):
    issues = skillcheck.validate_skill(tmp_path)

    assert skillcheck.has_errors(issues)
    assert issues[0].code == "skill.missing_skill_md"
    assert issues[0].path == "SKILL.md"


def test_validate_system_command_shape_without_environment_presence_check(tmp_path):
    (tmp_path / "SKILL.md").write_text("---\nname: skill\n---\n", encoding="utf-8")
    (tmp_path / "csk-skill.json").write_text(
        json.dumps(
            {
                "schema_version": 2,
                "commands": {
                    "missing": {
                        "type": "system",
                        "command": "definitely-missing-csk-test-command",
                    }
                },
            }
        ),
        encoding="utf-8",
    )

    issues = skillcheck.validate_skill(tmp_path)

    assert issues == []


def test_validate_locale_falls_back_when_selected_catalog_missing_but_other_locale_is_consistent(tmp_path):
    (tmp_path / "SKILL.md").write_text("---\nname: skill\n---\n", encoding="utf-8")
    (tmp_path / "locales").mkdir()
    (tmp_path / ".skill_triggers").mkdir()
    (tmp_path / "locales" / "metadata.json").write_text(
        json.dumps({"locales": {"ru": {"description": "Описание"}, "en": {"description": "Description"}}}),
        encoding="utf-8",
    )
    (tmp_path / ".skill_triggers" / "en.md").write_text("- trigger\n", encoding="utf-8")

    issues = skillcheck.validate_skill(tmp_path, locale_value="ru")

    assert not skillcheck.has_errors(issues)
    assert [(issue.severity, issue.code) for issue in issues] == [
        ("warning", "locale.selected_unavailable")
    ]


def test_validate_locale_metadata_without_catalogs_is_error(tmp_path):
    (tmp_path / "SKILL.md").write_text("---\nname: skill\n---\n", encoding="utf-8")
    (tmp_path / "locales").mkdir()
    (tmp_path / "locales" / "metadata.json").write_text(
        json.dumps({"locales": {"ru": {"description": "Описание"}}}),
        encoding="utf-8",
    )

    issues = skillcheck.validate_skill(tmp_path, locale_value="ru")

    assert skillcheck.has_errors(issues)
    assert issues[-1].code == "locale.no_consistent_catalog"


def test_validate_locale_catalogs_without_metadata_is_error(tmp_path):
    (tmp_path / "SKILL.md").write_text("---\nname: skill\n---\n", encoding="utf-8")
    (tmp_path / ".skill_triggers").mkdir()
    (tmp_path / ".skill_triggers" / "ru.md").write_text("- триггер\n", encoding="utf-8")

    issues = skillcheck.validate_skill(tmp_path, locale_value="ru")

    assert skillcheck.has_errors(issues)
    assert issues[-1].code == "locale.metadata_missing"


def test_validate_locale_trigger_catalog_file_is_error(tmp_path):
    (tmp_path / "SKILL.md").write_text("---\nname: skill\n---\n", encoding="utf-8")
    (tmp_path / "locales").mkdir()
    (tmp_path / "locales" / "metadata.json").write_text(
        json.dumps({"locales": {"ru": {"description": "Описание"}}}),
        encoding="utf-8",
    )
    (tmp_path / ".skill_triggers").write_text("", encoding="utf-8")

    issues = skillcheck.validate_skill(tmp_path, locale_value="ru")

    assert skillcheck.has_errors(issues)
    assert issues[-1].code == "locale.triggers_not_directory"


def test_validate_locale_none_does_not_validate_localization(tmp_path):
    (tmp_path / "SKILL.md").write_text("---\nname: skill\n---\n", encoding="utf-8")
    (tmp_path / ".skill_triggers").mkdir()
    (tmp_path / ".skill_triggers" / "ru.md").write_text("- триггер\n", encoding="utf-8")

    issues = skillcheck.validate_skill(tmp_path, locale_value=None)

    assert issues == []
