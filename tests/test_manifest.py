from __future__ import annotations

import pytest

from csk import manifest


def test_manifest_parses_skill_refs(tmp_path):
    parsed = manifest.parse_manifest(
        {
            "schema_version": 1,
            "project": {"alias": "demo-ios"},
            "agents": ["codex_cli"],
            "locale": "ru",
            "skills": [{"name": "skill-a", "source": "repo-a", "git": "git@example.com:skills/repo-a.git", "tag": "v1"}],
        },
        tmp_path / "Skillfile.json",
    )
    assert parsed.skills[0].name == "skill-a"
    assert parsed.skills[0].source == "repo-a"
    assert parsed.skills[0].git == "git@example.com:skills/repo-a.git"
    assert parsed.skills[0].ref.kind == "tag"
    assert parsed.project_alias == "demo-ios"


def test_manifest_rejects_duplicate_skill_names(tmp_path):
    with pytest.raises(manifest.ManifestError):
        manifest.parse_manifest(
            {
                "schema_version": 1,
                "skills": [
                    {"name": "same", "tag": "v1"},
                    {"name": "same", "source": "other", "tag": "v1"},
                ],
            },
            tmp_path / "Skillfile.json",
        )


@pytest.mark.parametrize("locale", ["../en", "pt_BR", "-en", "русский"])
def test_manifest_rejects_unsafe_locale_selector(tmp_path, locale):
    with pytest.raises(manifest.ManifestError, match="locale"):
        manifest.parse_manifest(
            {"schema_version": 1, "locale": locale, "skills": []},
            tmp_path / "Skillfile.json",
        )


@pytest.mark.parametrize("alias", ["", "x" * 129, "bad\u0001alias"])
def test_manifest_rejects_invalid_project_alias(tmp_path, alias):
    with pytest.raises(manifest.ManifestError, match="project.alias"):
        manifest.parse_manifest(
            {"schema_version": 1, "project": {"alias": alias}, "skills": []},
            tmp_path / "Skillfile.json",
        )


def test_manifest_accepts_operator_facing_project_alias(tmp_path):
    parsed = manifest.parse_manifest(
        {"schema_version": 1, "project": {"alias": "Demo iOS"}, "skills": []},
        tmp_path / "Skillfile.json",
    )
    assert parsed.project_alias == "Demo iOS"


def test_manifest_rejects_unknown_top_level_field(tmp_path):
    with pytest.raises(manifest.ManifestError, match="unsupported field"):
        manifest.parse_manifest(
            {"schema_version": 1, "skills": [], "extension": True},
            tmp_path / "Skillfile.json",
        )


def test_manifest_requires_exactly_one_ref(tmp_path):
    with pytest.raises(manifest.ManifestError):
        manifest.parse_manifest(
            {"schema_version": 1, "skills": [{"name": "bad", "tag": "v1", "branch": "main"}]},
            tmp_path / "Skillfile.json",
        )


def test_manifest_rejects_empty_git_url(tmp_path):
    with pytest.raises(manifest.ManifestError):
        manifest.parse_manifest(
            {"schema_version": 1, "skills": [{"name": "bad", "git": "", "tag": "v1"}]},
            tmp_path / "Skillfile.json",
        )


@pytest.mark.parametrize("name", ["../escape", "a/b", "a\\b", "-flag", ".hidden", ".."])
def test_manifest_rejects_unsafe_skill_names(tmp_path, name):
    with pytest.raises(manifest.ManifestError, match="name"):
        manifest.parse_manifest(
            {"schema_version": 1, "skills": [{"name": name, "tag": "v1"}]},
            tmp_path / "Skillfile.json",
        )


@pytest.mark.parametrize("source", ["../other", "a/../b", "/abs", "a\\b", "a//b"])
def test_manifest_rejects_unsafe_source(tmp_path, source):
    with pytest.raises(manifest.ManifestError, match="source"):
        manifest.parse_manifest(
            {"schema_version": 1, "skills": [{"name": "ok", "source": source, "tag": "v1"}]},
            tmp_path / "Skillfile.json",
        )


def test_manifest_accepts_typical_identifiers(tmp_path):
    parsed = manifest.parse_manifest(
        {
            "schema_version": 1,
            "skills": [
                {"name": "skill-analytics", "tag": "v1"},
                {"name": "skill_x.v2", "source": "repo.v2", "tag": "v1"},
                {"name": "skill-metrics", "source": "internal/skill-metrics", "tag": "v1"},
            ],
        },
        tmp_path / "Skillfile.json",
    )
    assert [skill.name for skill in parsed.skills] == ["skill-analytics", "skill_x.v2", "skill-metrics"]
    assert parsed.skills[2].source == "internal/skill-metrics"


@pytest.mark.parametrize(
    ("payload", "fragment"),
    [
        ({"skills": []}, "missing required field 'schema_version'"),
        ({"schema_version": "1", "skills": []}, "must be an integer"),
        ({"schema_version": 99, "skills": []}, "requires a newer csk"),
    ],
)
def test_manifest_schema_version_errors_are_specific(tmp_path, payload, fragment):
    with pytest.raises(manifest.ManifestError, match=fragment):
        manifest.parse_manifest(payload, tmp_path / "Skillfile.json")
