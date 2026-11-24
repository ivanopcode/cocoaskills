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


def test_selected_locale_falls_back_to_source_when_other_locale_is_consistent(tmp_path):
    snapshot = tmp_path / "snapshot"
    installed = tmp_path / "installed"
    (snapshot / "locales").mkdir(parents=True)
    (snapshot / ".skill_triggers").mkdir()
    installed.mkdir()
    (installed / "SKILL.md").write_text("---\nname: skill\n---\n\n# Source\n", encoding="utf-8")
    (snapshot / "locales" / "metadata.json").write_text(
        json.dumps({"locales": {"ru": {"description": "Описание"}, "en": {"description": "Description"}}}),
        encoding="utf-8",
    )
    (snapshot / ".skill_triggers" / "en.md").write_text("- trigger\n", encoding="utf-8")

    locale.render_locale(snapshot, installed, "ru")

    assert (installed / "SKILL.md").read_text(encoding="utf-8") == "---\nname: skill\n---\n\n# Source\n"


def test_locale_none_skips_localization_validation(tmp_path):
    snapshot = tmp_path / "snapshot"
    installed = tmp_path / "installed"
    (snapshot / ".skill_triggers").mkdir(parents=True)
    installed.mkdir()
    (installed / "SKILL.md").write_text("---\nname: skill\n---\n", encoding="utf-8")
    (snapshot / ".skill_triggers" / "ru.md").write_text("- триггер\n", encoding="utf-8")

    locale.render_locale(snapshot, installed, None)


def test_localized_frontmatter_preserves_non_localized_keys_and_respects_triggers_rule(tmp_path):
    # Localized SKILL.md replaces only description (and triggers when locale supplies them), keeping allowed-tools, license, nested metadata, and unlocalized positive control byte-identical.
    source_skill = (
        "---\n"
        "name: complex-skill\n"
        "license: Apache-2.0\n"
        "summary: It's a plain scalar with [brackets] and apostrophe\n"
        "description: >\n"
        "  First line of folded\n"
        "  multi-line description.\n"
        "allowed-tools:\n"
        "  - Bash\n"
        "  - Read\n"
        "metadata:\n"
        "  author: team-alpha\n"
        "  nested:\n"
        "    tier: 2\n"
        "  notes: |\n"
        "    keep literal line 1\n"
        "    don't touch [brackets]\n"
        "triggers:\n"
        "  - source trigger one\n"
        "  - source trigger two\n"
        "---\n"
        "\n"
        "# Body heading\n"
        "Keep body text untouched.\n"
    )
    snapshot = tmp_path / "snapshot"
    (snapshot / "locales").mkdir(parents=True)
    (snapshot / ".skill_triggers").mkdir()
    (snapshot / "SKILL.md").write_text(source_skill, encoding="utf-8")
    (snapshot / "locales" / "metadata.json").write_text(
        json.dumps({"locales": {"ru": {"description": "Локализованное описание"}}}),
        encoding="utf-8",
    )

    # Case 1: locale supplies triggers -> description and triggers replaced in place; all other keys byte-identical.
    (snapshot / ".skill_triggers" / "ru.md").write_text(
        "- новый триггер\n- второй триггер\n",
        encoding="utf-8",
    )
    installed_with_triggers = tmp_path / "installed-with-triggers"
    installed_with_triggers.mkdir()
    (installed_with_triggers / "SKILL.md").write_text(source_skill, encoding="utf-8")
    locale.render_locale(snapshot, installed_with_triggers, "ru")
    expected_with_triggers = (
        "---\n"
        "name: complex-skill\n"
        "license: Apache-2.0\n"
        "summary: It's a plain scalar with [brackets] and apostrophe\n"
        'description: "Локализованное описание"\n'
        "allowed-tools:\n"
        "  - Bash\n"
        "  - Read\n"
        "metadata:\n"
        "  author: team-alpha\n"
        "  nested:\n"
        "    tier: 2\n"
        "  notes: |\n"
        "    keep literal line 1\n"
        "    don't touch [brackets]\n"
        "triggers:\n"
        '  - "новый триггер"\n'
        '  - "второй триггер"\n'
        "---\n"
        "\n"
        "# Body heading\n"
        "Keep body text untouched.\n"
    )
    assert (installed_with_triggers / "SKILL.md").read_text(encoding="utf-8") == expected_with_triggers

    # Case 2: locale catalog exists but supplies no triggers -> source triggers remain byte-identical.
    (snapshot / ".skill_triggers" / "ru.md").write_text("# No bullets here\n", encoding="utf-8")
    installed_keep_triggers = tmp_path / "installed-keep-triggers"
    installed_keep_triggers.mkdir()
    (installed_keep_triggers / "SKILL.md").write_text(source_skill, encoding="utf-8")
    locale.render_locale(snapshot, installed_keep_triggers, "ru")
    expected_keep_triggers = (
        "---\n"
        "name: complex-skill\n"
        "license: Apache-2.0\n"
        "summary: It's a plain scalar with [brackets] and apostrophe\n"
        'description: "Локализованное описание"\n'
        "allowed-tools:\n"
        "  - Bash\n"
        "  - Read\n"
        "metadata:\n"
        "  author: team-alpha\n"
        "  nested:\n"
        "    tier: 2\n"
        "  notes: |\n"
        "    keep literal line 1\n"
        "    don't touch [brackets]\n"
        "triggers:\n"
        "  - source trigger one\n"
        "  - source trigger two\n"
        "---\n"
        "\n"
        "# Body heading\n"
        "Keep body text untouched.\n"
    )
    assert (installed_keep_triggers / "SKILL.md").read_text(encoding="utf-8") == expected_keep_triggers

    # Positive control: unlocalized install (locale=None) leaves SKILL.md byte-identical.
    installed_unlocalized = tmp_path / "installed-unlocalized"
    installed_unlocalized.mkdir()
    (installed_unlocalized / "SKILL.md").write_text(source_skill, encoding="utf-8")
    locale.render_locale(snapshot, installed_unlocalized, None)
    assert (installed_unlocalized / "SKILL.md").read_text(encoding="utf-8") == source_skill


def test_localized_openai_yaml_preserves_extra_top_level_and_interface_keys(tmp_path):
    # Localized agents/openai.yaml updates only display_name, short_description, and default_prompt, preserving extra top-level and interface keys verbatim.
    snapshot = tmp_path / "snapshot"
    installed = tmp_path / "installed"
    (snapshot / "locales").mkdir(parents=True)
    (snapshot / ".skill_triggers").mkdir()
    (snapshot / "agents").mkdir()
    (installed / "agents").mkdir(parents=True)
    source_openai = (
        "version: 1\n"
        "interface:\n"
        "  icon: ./icon.svg\n"
        "  display_name: Old Name\n"
        "  brand_color: '#112233'\n"
        "  short_description: >\n"
        "    Old short\n"
        "    description\n"
        "  default_prompt: Old prompt\n"
        "policy:\n"
        "  allow_implicit_invocation: true\n"
        "  tags:\n"
        "    - internal\n"
    )
    (snapshot / "SKILL.md").write_text("---\nname: skill\ndescription: Source\n---\n", encoding="utf-8")
    (installed / "SKILL.md").write_text("---\nname: skill\ndescription: Source\n---\n", encoding="utf-8")
    (snapshot / "agents" / "openai.yaml").write_text(source_openai, encoding="utf-8")
    (installed / "agents" / "openai.yaml").write_text(source_openai, encoding="utf-8")
    (snapshot / "locales" / "metadata.json").write_text(
        json.dumps(
            {
                "locales": {
                    "ru": {
                        "description": "Описание",
                        "display_name": "Новое имя",
                        "short_description": "Кратко",
                        "default_prompt": "Запусти скилл",
                    }
                }
            }
        ),
        encoding="utf-8",
    )
    (snapshot / ".skill_triggers" / "ru.md").write_text("- триггер\n", encoding="utf-8")

    locale.render_locale(snapshot, installed, "ru")

    expected_openai = (
        "version: 1\n"
        "interface:\n"
        "  icon: ./icon.svg\n"
        '  display_name: "Новое имя"\n'
        "  brand_color: '#112233'\n"
        '  short_description: "Кратко"\n'
        '  default_prompt: "Запусти скилл"\n'
        "policy:\n"
        "  allow_implicit_invocation: true\n"
        "  tags:\n"
        "    - internal\n"
    )
    assert (installed / "agents" / "openai.yaml").read_text(encoding="utf-8") == expected_openai


@pytest.mark.parametrize(
    "bad_frontmatter",
    [
        "# No frontmatter at all\n",
        "---\nname: unclosed\ndescription: missing delimiter\n",
        "---\nname: bad\nmetadata: {a: [1, 2}\ndescription: old\n---\n",
        "---\nname: bad\ndescription: \"unterminated\n---\n",
        "---\nname: dup\ndescription: first\ndescription: second\n---\n",
        "---\n\tname: tab-indent\n---\n",
        "---\n- sequence-not-mapping\n---\n",
    ],
)
def test_unparseable_frontmatter_refuses_and_leaves_file_untouched(tmp_path, bad_frontmatter):
    # Unparseable SKILL.md frontmatter is refused by both analyze_locale and render_locale, leaving SKILL.md untouched.
    snapshot = tmp_path / "snapshot"
    installed = tmp_path / "installed"
    (snapshot / "locales").mkdir(parents=True)
    (snapshot / ".skill_triggers").mkdir()
    installed.mkdir()
    (snapshot / "SKILL.md").write_text(bad_frontmatter, encoding="utf-8")
    (installed / "SKILL.md").write_text(bad_frontmatter, encoding="utf-8")
    (snapshot / "locales" / "metadata.json").write_text(
        json.dumps({"locales": {"ru": {"description": "Описание"}}}),
        encoding="utf-8",
    )
    (snapshot / ".skill_triggers" / "ru.md").write_text("- триггер\n", encoding="utf-8")

    analysis = locale.analyze_locale(snapshot, "ru")
    assert analysis.failed
    assert analysis.issues[0].code == "locale.frontmatter_invalid"
    assert analysis.issues[0].path == "SKILL.md"

    with pytest.raises(locale.LocaleError):
        locale.render_locale(snapshot, installed, "ru")
    assert (installed / "SKILL.md").read_text(encoding="utf-8") == bad_frontmatter


def test_blank_locale_description_refuses_instead_of_silent_noop(tmp_path):
    # A selected consistent locale with missing or blank description refuses rather than succeeding while writing nothing.
    snapshot = tmp_path / "snapshot"
    installed = tmp_path / "installed"
    (snapshot / "locales").mkdir(parents=True)
    (snapshot / ".skill_triggers").mkdir()
    installed.mkdir()
    source = "---\nname: skill\ndescription: Original\n---\n"
    (snapshot / "SKILL.md").write_text(source, encoding="utf-8")
    (installed / "SKILL.md").write_text(source, encoding="utf-8")
    (snapshot / "locales" / "metadata.json").write_text(
        json.dumps({"locales": {"ru": {"description": "   "}}}),
        encoding="utf-8",
    )
    (snapshot / ".skill_triggers" / "ru.md").write_text("- триггер\n", encoding="utf-8")

    analysis = locale.analyze_locale(snapshot, "ru")
    assert analysis.failed
    assert analysis.issues[0].code == "locale.metadata_invalid"
    with pytest.raises(locale.LocaleError, match="description"):
        locale.render_locale(snapshot, installed, "ru")
    assert (installed / "SKILL.md").read_text(encoding="utf-8") == source


def test_public_install_preserves_frontmatter_and_refuses_unparseable_frontmatter(
    tmp_path, skills_root, csk_home
):
    # Public installer.install preserves non-localized frontmatter and agents/openai.yaml keys on localized install, keeps unlocalized install unchanged, and refuses unparseable frontmatter consistently with skill check.
    from conftest import make_config, make_project, make_skill_repo, write_skillfile
    from csk import installer, skillcheck

    source_skill = (
        "---\n"
        "name: skill-loc\n"
        "license: MIT\n"
        "description: |\n"
        "  English line one\n"
        "  English line two\n"
        "allowed-tools:\n"
        "  - Bash\n"
        "  - Read\n"
        "metadata:\n"
        "  owner: platform\n"
        "triggers:\n"
        "  - source trigger\n"
        "---\n"
        "\n"
        "# Instructions\n"
    )
    source_openai = (
        "version: 1\n"
        "interface:\n"
        "  icon: ./icon.svg\n"
        "  display_name: Old\n"
        "policy:\n"
        "  allow_implicit_invocation: true\n"
    )
    make_skill_repo(
        skills_root,
        "skill-loc",
        {
            "SKILL.md": source_skill,
            "agents/openai.yaml": source_openai,
            "locales/metadata.json": json.dumps(
                {
                    "locales": {
                        "ru": {
                            "description": "Русское описание",
                            "display_name": "Скилл",
                            "short_description": "Кратко",
                            "default_prompt": "Запусти",
                        }
                    }
                }
            ),
            ".skill_triggers/ru.md": "- русский триггер\n",
        },
        tag="v1",
    )

    project_ru = make_project(tmp_path, "proj-ru")
    write_skillfile(project_ru, {"schema_version": 1, "skills": [{"name": "skill-loc", "tag": "v1"}]})
    cfg_ru = make_config(csk_home, skills_root, project_ru)
    res_ru = installer.install(cfg_ru)[0]
    assert not res_ru.errors
    installed_skill = (project_ru / ".agents" / "skills" / "skill-loc" / "SKILL.md").read_text(encoding="utf-8")
    assert "license: MIT\n" in installed_skill
    assert 'description: "Русское описание"\n' in installed_skill
    assert "allowed-tools:\n  - Bash\n  - Read\n" in installed_skill
    assert "metadata:\n  owner: platform\n" in installed_skill
    assert 'triggers:\n  - "русский триггер"\n' in installed_skill
    installed_openai = (
        project_ru / ".agents" / "skills" / "skill-loc" / "agents" / "openai.yaml"
    ).read_text(encoding="utf-8")
    assert "version: 1\n" in installed_openai
    assert "  icon: ./icon.svg\n" in installed_openai
    assert '  display_name: "Скилл"\n' in installed_openai
    assert "policy:\n  allow_implicit_invocation: true\n" in installed_openai

    # Positive control: unlocalized install keeps SKILL.md and agents/openai.yaml byte-identical.
    project_raw = make_project(tmp_path, "proj-raw")
    write_skillfile(project_raw, {"schema_version": 1, "skills": [{"name": "skill-loc", "tag": "v1"}]})
    cfg_raw = make_config(csk_home, skills_root, project_raw)
    cfg_raw = cfg_raw.__class__(**{**cfg_raw.__dict__, "preferred_locale": None})
    res_raw = installer.install(cfg_raw)[0]
    assert not res_raw.errors
    assert (project_raw / ".agents" / "skills" / "skill-loc" / "SKILL.md").read_text(encoding="utf-8") == source_skill
    assert (
        project_raw / ".agents" / "skills" / "skill-loc" / "agents" / "openai.yaml"
    ).read_text(encoding="utf-8") == source_openai

    # Unparseable frontmatter refuses in both skillcheck and installer.install.
    bad_repo, _ = make_skill_repo(
        skills_root,
        "skill-bad-fm",
        {
            "SKILL.md": "---\nname: skill-bad-fm\nmetadata: {broken\n---\n",
            "locales/metadata.json": json.dumps({"locales": {"ru": {"description": "Описание"}}}),
            ".skill_triggers/ru.md": "- триггер\n",
        },
        tag="v1",
    )
    issues = skillcheck.validate_skill(bad_repo, locale_value="ru")
    assert skillcheck.has_errors(issues)
    assert any(issue.code == "locale.frontmatter_invalid" for issue in issues)
    project_bad = make_project(tmp_path, "proj-bad")
    write_skillfile(project_bad, {"schema_version": 1, "skills": [{"name": "skill-bad-fm", "tag": "v1"}]})
    cfg_bad = make_config(csk_home, skills_root, project_bad)
    res_bad = installer.install(cfg_bad)[0]
    assert res_bad.errors
    frontmatter_issue = next(issue for issue in issues if issue.code == "locale.frontmatter_invalid")
    assert any(frontmatter_issue.message in err for err in res_bad.errors)
    assert not (project_bad / ".agents" / "skills" / "skill-bad-fm").exists()
