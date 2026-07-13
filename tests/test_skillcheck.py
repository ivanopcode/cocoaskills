from __future__ import annotations

import json

import pytest

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


@pytest.mark.parametrize("runtime_path", ["scripts/tool", r"scripts\tool.cmd"])
def test_validate_warns_when_prompt_context_references_runtime_root(tmp_path, runtime_path):
    (tmp_path / "SKILL.md").write_text(
        f"---\nname: skill\n---\n\nRun `<skill-dir>/{runtime_path}`.\n",
        encoding="utf-8",
    )
    (tmp_path / "scripts").mkdir()
    (tmp_path / "scripts" / "tool").write_text("#!/bin/sh\n", encoding="utf-8")
    (tmp_path / "csk-skill.json").write_text(
        json.dumps(
            {
                "schema_version": 2,
                "runtime_roots": ["scripts"],
                "commands": {"tool": {"type": "script", "unix_path": "scripts/tool"}},
            }
        ),
        encoding="utf-8",
    )

    issues = skillcheck.validate_skill(tmp_path)

    assert [(issue.severity, issue.code, issue.path) for issue in issues] == [
        ("warning", "skill.runtime_root_in_prompt_context", "SKILL.md"),
        ("warning", "skill.command_resolution_contract_missing", "SKILL.md"),
    ]


def test_validate_ignores_runtime_root_reference_outside_prompt_context(tmp_path):
    (tmp_path / "SKILL.md").write_text(
        "---\nname: skill\n---\n\n"
        "Resolve .agents/bin/tool, then global/bin/tool, then command -v tool or Get-Command tool.\n",
        encoding="utf-8",
    )
    (tmp_path / "README.md").write_text("Develop with scripts/tool.\n", encoding="utf-8")
    (tmp_path / "scripts").mkdir()
    (tmp_path / "scripts" / "tool").write_text("#!/bin/sh\n", encoding="utf-8")
    (tmp_path / "csk-skill.json").write_text(
        json.dumps(
            {
                "schema_version": 2,
                "runtime_roots": ["scripts"],
                "commands": {"tool": {"type": "script", "unix_path": "scripts/tool"}},
            }
        ),
        encoding="utf-8",
    )

    issues = skillcheck.validate_skill(tmp_path)

    assert issues == []


@pytest.mark.parametrize("runtime_path", ["scripts/tool", r"scripts\tool.cmd"])
def test_validate_warns_when_consumer_references_provider_source_runtime(tmp_path, runtime_path):
    (tmp_path / "SKILL.md").write_text(
        f"---\nname: consumer\n---\n\nRun the provider at {runtime_path}.\n",
        encoding="utf-8",
    )
    (tmp_path / "csk-skill.json").write_text(
        json.dumps(
            {
                "schema_version": 2,
                "commands": {},
                "dependencies": {
                    "commands": {
                        "tool": {
                            "type": "skill",
                            "skill": "skill-provider",
                            "command": "tool",
                        }
                    }
                },
            }
        ),
        encoding="utf-8",
    )

    issues = skillcheck.validate_skill(tmp_path)

    assert [(issue.severity, issue.code, issue.path) for issue in issues] == [
        ("warning", "skill.provider_runtime_path_in_prompt_context", "SKILL.md"),
        ("warning", "skill.command_resolution_contract_missing", "SKILL.md"),
    ]


def test_validate_warns_when_managed_command_has_no_shell_neutral_resolver(tmp_path):
    (tmp_path / "SKILL.md").write_text("---\nname: skill\n---\n\nRun tool.\n", encoding="utf-8")
    (tmp_path / "scripts").mkdir()
    (tmp_path / "scripts" / "tool").write_text("#!/bin/sh\n", encoding="utf-8")
    (tmp_path / "scripts" / "tool.cmd").write_text("@echo off\r\n", encoding="utf-8")
    (tmp_path / "csk-skill.json").write_text(
        json.dumps(
            {
                "schema_version": 2,
                "runtime_roots": ["scripts"],
                "commands": {
                    "tool": {
                        "type": "script",
                        "unix_path": "scripts/tool",
                        "win_path": "scripts/tool.cmd",
                    }
                },
            }
        ),
        encoding="utf-8",
    )

    issues = skillcheck.validate_skill(tmp_path)

    assert [(issue.severity, issue.code) for issue in issues] == [
        ("warning", "skill.command_resolution_contract_missing")
    ]
    assert "Windows .cmd shim suffix" in issues[0].message


def test_validate_accepts_cross_platform_shell_neutral_resolver(tmp_path):
    (tmp_path / "SKILL.md").write_text(
        "---\nname: skill\n---\n\n"
        "Resolve .agents/bin/tool (tool.cmd on Windows), then global/bin/tool, "
        "then command -v tool or Get-Command tool.\n",
        encoding="utf-8",
    )
    (tmp_path / "scripts").mkdir()
    (tmp_path / "scripts" / "tool").write_text("#!/bin/sh\n", encoding="utf-8")
    (tmp_path / "scripts" / "tool.cmd").write_text("@echo off\r\n", encoding="utf-8")
    (tmp_path / "csk-skill.json").write_text(
        json.dumps(
            {
                "schema_version": 2,
                "runtime_roots": ["scripts"],
                "commands": {
                    "tool": {
                        "type": "script",
                        "unix_path": "scripts/tool",
                        "win_path": "scripts/tool.cmd",
                    }
                },
            }
        ),
        encoding="utf-8",
    )

    assert skillcheck.validate_skill(tmp_path) == []


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


def test_validate_malformed_locale_metadata_is_error(tmp_path):
    (tmp_path / "SKILL.md").write_text("---\nname: skill\n---\n", encoding="utf-8")
    (tmp_path / "locales").mkdir()
    (tmp_path / ".skill_triggers").mkdir()
    (tmp_path / "locales" / "metadata.json").write_text("{", encoding="utf-8")
    (tmp_path / ".skill_triggers" / "ru.md").write_text("- триггер\n", encoding="utf-8")

    issues = skillcheck.validate_skill(tmp_path, locale_value="ru")

    assert skillcheck.has_errors(issues)
    assert issues[-1].code == "locale.metadata_malformed"
    assert "Malformed locale metadata" in issues[-1].message


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
