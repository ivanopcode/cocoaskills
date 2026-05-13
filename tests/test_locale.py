from __future__ import annotations

import json

import pytest

from csk import locale


def test_locale_renders_skill_frontmatter_and_openai_yaml(tmp_path):
    snapshot = tmp_path / "snapshot"
    installed = tmp_path / "installed"
    (snapshot / "locales").mkdir(parents=True)
    (snapshot / ".skill_triggers").mkdir()
    (installed / "agents").mkdir(parents=True)
    (snapshot / "locales" / "metadata.json").write_text(
        json.dumps(
            {
                "locales": {
                    "ru": {
                        "description": "Описание",
                        "display_name": "Имя",
                        "short_description": "Коротко",
                        "default_prompt": "Используй skill",
                    }
                }
            }
        ),
        encoding="utf-8",
    )
    (snapshot / ".skill_triggers" / "ru.md").write_text("- триггер\n", encoding="utf-8")
    (installed / "SKILL.md").write_text("---\nname: skill\n---\n\n# Body\n", encoding="utf-8")
    (installed / "agents" / "openai.yaml").write_text("interface: {}\n", encoding="utf-8")

    locale.render_locale(snapshot, installed, "ru")
    assert "Описание" in (installed / "SKILL.md").read_text(encoding="utf-8")
    assert "триггер" in (installed / "SKILL.md").read_text(encoding="utf-8")
    assert "default_prompt" in (installed / "agents" / "openai.yaml").read_text(encoding="utf-8")


def test_unsupported_locale_fails(tmp_path):
    snapshot = tmp_path / "snapshot"
    installed = tmp_path / "installed"
    (snapshot / "locales").mkdir(parents=True)
    (snapshot / ".skill_triggers").mkdir()
    installed.mkdir()
    (installed / "SKILL.md").write_text("---\nname: skill\n---\n", encoding="utf-8")
    (snapshot / "locales" / "metadata.json").write_text(json.dumps({"locales": {"en": {}}}), encoding="utf-8")
    with pytest.raises(locale.LocaleError):
        locale.render_locale(snapshot, installed, "ru")

