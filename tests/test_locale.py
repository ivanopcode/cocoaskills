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
def test_unsupported_frontmatter_warns_and_leaves_file_untouched(tmp_path, bad_frontmatter):
    # Unsupported SKILL.md frontmatter emits a warning naming locale and field without failing, and render_locale leaves SKILL.md byte-for-byte unchanged instead of falling back to a lossy rebuild.
    snapshot = tmp_path / "snapshot"
    installed = tmp_path / "installed"
    (snapshot / "locales").mkdir(parents=True)
    (snapshot / ".skill_triggers").mkdir()
    installed.mkdir()
    raw_bytes = bad_frontmatter.encode("utf-8")
    (snapshot / "SKILL.md").write_bytes(raw_bytes)
    (installed / "SKILL.md").write_bytes(raw_bytes)
    (snapshot / "locales" / "metadata.json").write_text(
        json.dumps({"locales": {"ru": {"description": "Описание"}}}),
        encoding="utf-8",
    )
    (snapshot / ".skill_triggers" / "ru.md").write_text("- триггер\n", encoding="utf-8")

    analysis = locale.analyze_locale(snapshot, "ru")
    assert not analysis.failed
    assert analysis.locale_to_render == "ru"
    assert len(analysis.issues) == 1
    assert analysis.issues[0].severity == "warning"
    assert analysis.issues[0].code == "locale.frontmatter_invalid"
    assert analysis.issues[0].path == "SKILL.md"
    assert "'ru'" in analysis.issues[0].message
    assert "description" in analysis.issues[0].message

    issues = locale.render_locale(snapshot, installed, "ru")
    assert any(issue.code == "locale.frontmatter_invalid" and issue.severity == "warning" for issue in issues)
    assert (installed / "SKILL.md").read_bytes() == raw_bytes


@pytest.mark.parametrize(
    "locale_entry,expect_openai_localized",
    [
        ({"description": "   "}, False),
        ({"display_name": "Имя"}, True),
    ],
)
def test_missing_or_blank_locale_description_warns_and_preserves_source_skill_md(
    tmp_path, locale_entry, expect_openai_localized
):
    # A selected locale with missing or blank description warns instead of refusing, keeps SKILL.md byte-identical to source, and still localizes agents/openai.yaml when openai fields are present.
    snapshot = tmp_path / "snapshot"
    installed = tmp_path / "installed"
    (snapshot / "locales").mkdir(parents=True)
    (snapshot / ".skill_triggers").mkdir()
    (snapshot / "agents").mkdir()
    (installed / "agents").mkdir(parents=True)
    source_skill = b"---\nname: skill\nlicense: MIT\ndescription: Original\n---\nbody\n"
    source_openai = b"version: 1\ninterface:\n  icon: ./icon.svg\n  display_name: Old\n"
    (snapshot / "SKILL.md").write_bytes(source_skill)
    (installed / "SKILL.md").write_bytes(source_skill)
    (snapshot / "agents" / "openai.yaml").write_bytes(source_openai)
    (installed / "agents" / "openai.yaml").write_bytes(source_openai)
    (snapshot / "locales" / "metadata.json").write_text(
        json.dumps({"locales": {"ru": locale_entry}}),
        encoding="utf-8",
    )
    (snapshot / ".skill_triggers" / "ru.md").write_text("- триггер\n", encoding="utf-8")

    analysis = locale.analyze_locale(snapshot, "ru")
    assert not analysis.failed
    assert analysis.locale_to_render == "ru"
    assert len(analysis.issues) == 1
    assert analysis.issues[0].severity == "warning"
    assert analysis.issues[0].code == "locale.description_missing"
    assert "'ru'" in analysis.issues[0].message
    assert "description" in analysis.issues[0].message

    issues = locale.render_locale(snapshot, installed, "ru")
    assert any(issue.code == "locale.description_missing" for issue in issues)
    assert (installed / "SKILL.md").read_bytes() == source_skill
    if expect_openai_localized:
        assert (installed / "agents" / "openai.yaml").read_bytes() == (
            'version: 1\ninterface:\n  icon: ./icon.svg\n  display_name: "Имя"\n'.encode("utf-8")
        )
    else:
        assert (installed / "agents" / "openai.yaml").read_bytes() == source_openai


@pytest.mark.parametrize(
    "bad_openai",
    [
        "interface: {display_name: Old, icon: ./icon.svg}\n",
        "interface: not-a-mapping\n",
    ],
)
def test_unsupported_openai_yaml_warns_and_preserves_source_bytes(tmp_path, bad_openai):
    # Unsupported agents/openai.yaml forms (such as a flow-mapping interface) emit a warning naming locale and field without refusing, localize SKILL.md, and leave agents/openai.yaml byte-identical to source.
    snapshot = tmp_path / "snapshot"
    installed = tmp_path / "installed"
    (snapshot / "locales").mkdir(parents=True)
    (snapshot / ".skill_triggers").mkdir()
    (snapshot / "agents").mkdir()
    (installed / "agents").mkdir(parents=True)
    source_skill = b"---\nname: skill\ndescription: Source\n---\nbody\n"
    raw_openai = bad_openai.encode("utf-8")
    (snapshot / "SKILL.md").write_bytes(source_skill)
    (installed / "SKILL.md").write_bytes(source_skill)
    (snapshot / "agents" / "openai.yaml").write_bytes(raw_openai)
    (installed / "agents" / "openai.yaml").write_bytes(raw_openai)
    (snapshot / "locales" / "metadata.json").write_text(
        json.dumps({"locales": {"ru": {"description": "Описание", "display_name": "Имя"}}}),
        encoding="utf-8",
    )
    (snapshot / ".skill_triggers" / "ru.md").write_text("- триггер\n", encoding="utf-8")

    analysis = locale.analyze_locale(snapshot, "ru")
    assert not analysis.failed
    assert analysis.locale_to_render == "ru"
    assert len(analysis.issues) == 1
    assert analysis.issues[0].severity == "warning"
    assert analysis.issues[0].code == "locale.openai_yaml_invalid"
    assert analysis.issues[0].path == "agents/openai.yaml"
    assert "'ru'" in analysis.issues[0].message
    assert "interface" in analysis.issues[0].message

    issues = locale.render_locale(snapshot, installed, "ru")
    assert any(issue.code == "locale.openai_yaml_invalid" for issue in issues)
    assert (installed / "agents" / "openai.yaml").read_bytes() == raw_openai
    assert 'description: "Описание"\n' in (installed / "SKILL.md").read_text(encoding="utf-8")


def test_crlf_line_endings_preserved_byte_for_byte_in_skill_md_and_openai_yaml(tmp_path):
    # Localized rewriting preserves CRLF line endings byte-for-byte in SKILL.md and agents/openai.yaml (and LF in LF files) without newline translation.
    snapshot = tmp_path / "snapshot-crlf"
    installed = tmp_path / "installed-crlf"
    (snapshot / "locales").mkdir(parents=True)
    (snapshot / ".skill_triggers").mkdir()
    (snapshot / "agents").mkdir()
    (installed / "agents").mkdir(parents=True)
    source_skill = (
        b"---\r\n"
        b"name: crlf-skill\r\n"
        b"license: MIT\r\n"
        b"description: Source description\r\n"
        b"allowed-tools:\r\n"
        b"  - Bash\r\n"
        b"---\r\n"
        b"\r\n"
        b"# Body\r\n"
        b"line two\r\n"
    )
    source_openai = (
        b"version: 1\r\n"
        b"interface:\r\n"
        b"  icon: ./icon.svg\r\n"
        b"  display_name: Old Name\r\n"
        b"policy:\r\n"
        b"  allow_implicit_invocation: true\r\n"
    )
    (snapshot / "SKILL.md").write_bytes(source_skill)
    (installed / "SKILL.md").write_bytes(source_skill)
    (snapshot / "agents" / "openai.yaml").write_bytes(source_openai)
    (installed / "agents" / "openai.yaml").write_bytes(source_openai)
    (snapshot / "locales" / "metadata.json").write_text(
        json.dumps(
            {
                "locales": {
                    "ru": {
                        "description": "Описание",
                        "display_name": "Скилл",
                        "short_description": "Кратко",
                        "default_prompt": "Запусти",
                    }
                }
            }
        ),
        encoding="utf-8",
    )
    (snapshot / ".skill_triggers" / "ru.md").write_bytes(b"- trigger-ru\r\n")

    issues = locale.render_locale(snapshot, installed, "ru")
    assert issues == ()

    out_skill = (installed / "SKILL.md").read_bytes()
    assert out_skill == (
        "---\r\n"
        "name: crlf-skill\r\n"
        "license: MIT\r\n"
        'description: "Описание"\r\n'
        "allowed-tools:\r\n"
        "  - Bash\r\n"
        "triggers:\r\n"
        '  - "trigger-ru"\r\n'
        "---\r\n"
        "\r\n"
        "# Body\r\n"
        "line two\r\n"
    ).encode("utf-8")
    assert out_skill.count(b"\n") == out_skill.count(b"\r\n")

    out_openai = (installed / "agents" / "openai.yaml").read_bytes()
    assert out_openai == (
        "version: 1\r\n"
        "interface:\r\n"
        "  icon: ./icon.svg\r\n"
        '  display_name: "Скилл"\r\n'
        '  short_description: "Кратко"\r\n'
        '  default_prompt: "Запусти"\r\n'
        "policy:\r\n"
        "  allow_implicit_invocation: true\r\n"
    ).encode("utf-8")
    assert out_openai.count(b"\n") == out_openai.count(b"\r\n")

    # Positive control: LF-only source files stay pure LF with zero CR bytes.
    lf_snapshot = tmp_path / "snapshot-lf"
    lf_installed = tmp_path / "installed-lf"
    (lf_snapshot / "locales").mkdir(parents=True)
    (lf_snapshot / ".skill_triggers").mkdir()
    (lf_snapshot / "agents").mkdir()
    (lf_installed / "agents").mkdir(parents=True)
    lf_skill = source_skill.replace(b"\r\n", b"\n")
    lf_openai = source_openai.replace(b"\r\n", b"\n")
    (lf_snapshot / "SKILL.md").write_bytes(lf_skill)
    (lf_installed / "SKILL.md").write_bytes(lf_skill)
    (lf_snapshot / "agents" / "openai.yaml").write_bytes(lf_openai)
    (lf_installed / "agents" / "openai.yaml").write_bytes(lf_openai)
    (lf_snapshot / "locales" / "metadata.json").write_bytes(
        (snapshot / "locales" / "metadata.json").read_bytes()
    )
    (lf_snapshot / ".skill_triggers" / "ru.md").write_bytes(b"- trigger-ru\n")
    locale.render_locale(lf_snapshot, lf_installed, "ru")
    assert b"\r" not in (lf_installed / "SKILL.md").read_bytes()
    assert b"\r" not in (lf_installed / "agents" / "openai.yaml").read_bytes()


def test_public_install_preserves_frontmatter_and_warns_without_refusing_unsupported_forms(
    tmp_path, skills_root, csk_home
):
    # Public installer.install preserves non-localized frontmatter and agents/openai.yaml keys, keeps unlocalized installs unchanged, preserves CRLF, and warns (never refuses) while preserving source bytes when locale description is missing or YAML forms are unsupported.
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

    # Unsupported frontmatter, flow-mapping openai.yaml, and missing locale description warn in both skillcheck and installer.install without refusing, preserving source bytes.
    bad_skill_bytes = b"---\nname: skill-bad-fm\nmetadata: {broken\n---\n# Keep body\n"
    bad_openai_bytes = b"interface: {display_name: SourceName}\n"
    bad_repo, _ = make_skill_repo(
        skills_root,
        "skill-bad-fm",
        {
            "SKILL.md": bad_skill_bytes,
            "agents/openai.yaml": bad_openai_bytes,
            "locales/metadata.json": json.dumps(
                {"locales": {"ru": {"description": "Описание", "display_name": "Имя"}}}
            ),
            ".skill_triggers/ru.md": "- триггер\n",
        },
        tag="v1",
    )
    issues = skillcheck.validate_skill(bad_repo, locale_value="ru")
    assert not skillcheck.has_errors(issues)
    assert [(issue.severity, issue.code) for issue in issues] == [
        ("warning", "locale.frontmatter_invalid"),
        ("warning", "locale.openai_yaml_invalid"),
    ]
    project_bad = make_project(tmp_path, "proj-bad")
    write_skillfile(project_bad, {"schema_version": 1, "skills": [{"name": "skill-bad-fm", "tag": "v1"}]})
    cfg_bad = make_config(csk_home, skills_root, project_bad)
    res_bad = installer.install(cfg_bad)[0]
    assert not res_bad.errors
    assert any(
        "skill-bad-fm" in msg and "'ru'" in msg and "description" in msg
        for msg in res_bad.messages
    )
    assert any(
        "skill-bad-fm" in msg and "'ru'" in msg and "interface" in msg
        for msg in res_bad.messages
    )
    assert (project_bad / ".agents" / "skills" / "skill-bad-fm" / "SKILL.md").read_bytes() == bad_skill_bytes
    assert (
        project_bad / ".agents" / "skills" / "skill-bad-fm" / "agents" / "openai.yaml"
    ).read_bytes() == bad_openai_bytes

    # Locale without description warns on SKILL.md, leaves SKILL.md as in source, and still localizes agents/openai.yaml.
    no_desc_skill_bytes = b"---\nname: skill-no-desc\ndescription: English source\n---\n"
    no_desc_openai_bytes = b"interface:\n  display_name: Old\n"
    make_skill_repo(
        skills_root,
        "skill-no-desc",
        {
            "SKILL.md": no_desc_skill_bytes,
            "agents/openai.yaml": no_desc_openai_bytes,
            "locales/metadata.json": json.dumps({"locales": {"ru": {"display_name": "Имя"}}}),
            ".skill_triggers/ru.md": "- триггер\n",
        },
        tag="v1",
    )
    project_no_desc = make_project(tmp_path, "proj-no-desc")
    write_skillfile(
        project_no_desc,
        {"schema_version": 1, "skills": [{"name": "skill-no-desc", "tag": "v1"}]},
    )
    cfg_no_desc = make_config(csk_home, skills_root, project_no_desc)
    res_no_desc = installer.install(cfg_no_desc)[0]
    assert not res_no_desc.errors
    assert any(
        "skill-no-desc" in msg and "'ru'" in msg and "description" in msg
        for msg in res_no_desc.messages
    )
    assert (
        project_no_desc / ".agents" / "skills" / "skill-no-desc" / "SKILL.md"
    ).read_bytes() == no_desc_skill_bytes
    assert (
        project_no_desc / ".agents" / "skills" / "skill-no-desc" / "agents" / "openai.yaml"
    ).read_bytes() == 'interface:\n  display_name: "Имя"\n'.encode("utf-8")


def test_non_utf8_skill_md_and_openai_yaml_warn_and_preserve_raw_bytes(
    tmp_path, skills_root, csk_home
):
    # Non-UTF-8 bytes in agents/openai.yaml or SKILL.md emit warnings in skillcheck and render_locale without raising UnicodeDecodeError, leaving raw bytes untouched while still localizing valid companion files.
    from conftest import make_config, make_project, make_skill_repo, write_skillfile
    from csk import installer, skillcheck

    # Case 1: valid SKILL.md with non-UTF-8 agents/openai.yaml (e.g. Latin-1 "Café").
    snap1 = tmp_path / "snap-nonutf8-openai"
    inst1 = tmp_path / "inst-nonutf8-openai"
    (snap1 / "locales").mkdir(parents=True)
    (snap1 / ".skill_triggers").mkdir()
    (snap1 / "agents").mkdir()
    (inst1 / "agents").mkdir(parents=True)
    raw_openai = b"interface:\n  display_name: Caf\xe9\n"
    raw_skill = b"---\nname: skill-latin1\ndescription: Source\n---\nbody\n"
    (snap1 / "SKILL.md").write_bytes(raw_skill)
    (inst1 / "SKILL.md").write_bytes(raw_skill)
    (snap1 / "agents" / "openai.yaml").write_bytes(raw_openai)
    (inst1 / "agents" / "openai.yaml").write_bytes(raw_openai)
    (snap1 / "locales" / "metadata.json").write_text(
        json.dumps({"locales": {"ru": {"description": "Описание", "display_name": "Имя"}}}),
        encoding="utf-8",
    )
    (snap1 / ".skill_triggers" / "ru.md").write_text("- триггер\n", encoding="utf-8")

    check1 = skillcheck.validate_skill(snap1, locale_value="ru")
    assert not skillcheck.has_errors(check1)
    assert [(i.severity, i.code, i.path) for i in check1] == [
        ("warning", "locale.openai_yaml_invalid", "agents/openai.yaml"),
    ]
    issues1 = locale.render_locale(snap1, inst1, "ru")
    assert any(i.code == "locale.openai_yaml_invalid" and i.severity == "warning" for i in issues1)
    assert (inst1 / "agents" / "openai.yaml").read_bytes() == raw_openai
    assert 'description: "Описание"\n' in (inst1 / "SKILL.md").read_text(encoding="utf-8")

    # Case 2: non-UTF-8 SKILL.md with valid agents/openai.yaml.
    snap2 = tmp_path / "snap-nonutf8-skill"
    inst2 = tmp_path / "inst-nonutf8-skill"
    (snap2 / "locales").mkdir(parents=True)
    (snap2 / ".skill_triggers").mkdir()
    (snap2 / "agents").mkdir()
    (inst2 / "agents").mkdir(parents=True)
    raw_bad_skill = b"---\nname: skill-latin1\ndescription: Caf\xe9\n---\nbody\n"
    valid_openai = b"interface:\n  display_name: Old\n"
    (snap2 / "SKILL.md").write_bytes(raw_bad_skill)
    (inst2 / "SKILL.md").write_bytes(raw_bad_skill)
    (snap2 / "agents" / "openai.yaml").write_bytes(valid_openai)
    (inst2 / "agents" / "openai.yaml").write_bytes(valid_openai)
    (snap2 / "locales" / "metadata.json").write_bytes(
        (snap1 / "locales" / "metadata.json").read_bytes()
    )
    (snap2 / ".skill_triggers" / "ru.md").write_text("- триггер\n", encoding="utf-8")

    check2 = skillcheck.validate_skill(snap2, locale_value="ru")
    assert not skillcheck.has_errors(check2)
    assert [(i.severity, i.code, i.path) for i in check2] == [
        ("warning", "locale.frontmatter_invalid", "SKILL.md"),
    ]
    issues2 = locale.render_locale(snap2, inst2, "ru")
    assert any(i.code == "locale.frontmatter_invalid" and i.severity == "warning" for i in issues2)
    assert (inst2 / "SKILL.md").read_bytes() == raw_bad_skill
    assert (inst2 / "agents" / "openai.yaml").read_bytes() == (
        'interface:\n  display_name: "Имя"\n'.encode("utf-8")
    )

    # Case 3: end-to-end installer.install succeeds with warning and preserves non-UTF-8 openai.yaml.
    repo_dir, _ = make_skill_repo(
        skills_root,
        "skill-nonutf8",
        {
            "SKILL.md": raw_skill,
            "locales/metadata.json": json.dumps(
                {"locales": {"ru": {"description": "Описание", "display_name": "Имя"}}}
            ),
            ".skill_triggers/ru.md": "- триггер\n",
        },
        tag="v0",
    )
    (repo_dir / "agents").mkdir(exist_ok=True)
    (repo_dir / "agents" / "openai.yaml").write_bytes(raw_openai)
    from conftest import commit_all, run

    commit_all(repo_dir, "add latin1 openai.yaml")
    run(["git", "tag", "v1"], repo_dir)
    project = make_project(tmp_path, "proj-nonutf8")
    write_skillfile(project, {"schema_version": 1, "skills": [{"name": "skill-nonutf8", "tag": "v1"}]})
    cfg = make_config(csk_home, skills_root, project)
    res = installer.install(cfg)[0]
    assert not res.errors
    assert any(
        "skill-nonutf8" in msg and "'ru'" in msg and "interface" in msg
        for msg in res.messages
    )
    assert (project / ".agents" / "skills" / "skill-nonutf8" / "agents" / "openai.yaml").read_bytes() == raw_openai
    assert 'description: "Описание"\n' in (
        project / ".agents" / "skills" / "skill-nonutf8" / "SKILL.md"
    ).read_text(encoding="utf-8")
