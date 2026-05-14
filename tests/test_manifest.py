from __future__ import annotations

import pytest

from csk import manifest


def test_manifest_parses_skill_refs(tmp_path):
    parsed = manifest.parse_manifest(
        {
            "schema_version": 1,
            "project": {"alias": "partners-ios"},
            "agents": ["codex_cli"],
            "locale": "ru",
            "skills": [{"name": "skill-a", "source": "repo-a", "tag": "v1"}],
        },
        tmp_path / "Skillfile.json",
    )
    assert parsed.skills[0].name == "skill-a"
    assert parsed.skills[0].source == "repo-a"
    assert parsed.skills[0].ref.kind == "tag"
    assert parsed.project_alias == "partners-ios"


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


def test_manifest_requires_exactly_one_ref(tmp_path):
    with pytest.raises(manifest.ManifestError):
        manifest.parse_manifest(
            {"schema_version": 1, "skills": [{"name": "bad", "tag": "v1", "branch": "main"}]},
            tmp_path / "Skillfile.json",
        )
