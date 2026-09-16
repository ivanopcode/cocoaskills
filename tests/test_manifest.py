from __future__ import annotations

import copy
import json
from pathlib import Path

import pytest

from csk import manifest

BASELINE_PATH = Path(__file__).parent / "fixtures" / "v1_identity_baseline.json"
BASELINE_OID = "53638fa1787ef40eace13783614e7975207a289c"


def _load_v1_baseline():
    """Load the independently captured schema-1 baseline fixture.

    The fixture was generated from the base revision's manifest module
    (``git show <base>:src/csk/manifest.py`` loaded under a private module
    name) with a fixed ``Skillfile.json`` path, so error text is comparable
    byte for byte. Regenerate with ``/tmp/gen_v1_baseline.py``.
    """
    data = json.loads(BASELINE_PATH.read_bytes())
    assert data["base_oid"] == BASELINE_OID, data.get("base_oid")
    assert data["path"] == "Skillfile.json"
    assert len(data["corpus"]) >= 20
    return data["corpus"]


def _candidate_model_fields(parsed):
    return {
        "project_alias": parsed.project_alias,
        "agents": list(parsed.agents),
        "locale": parsed.locale,
        "skills": [
            {
                "name": skill.name,
                "source": skill.source,
                "ref": {"kind": skill.ref.kind, "value": skill.ref.value},
                "git": skill.git,
            }
            for skill in parsed.skills
        ],
    }


def _assert_matches_baseline(payload, expected, path, **kwargs):
    try:
        parsed = manifest.parse_manifest(copy.deepcopy(payload), path, **kwargs)
    except manifest.ManifestError as exc:
        assert expected["status"] == "error", f"candidate raised {exc!r} for {payload!r}"
        assert str(exc) == expected["error"], payload
        return
    assert expected["status"] == "ok", f"candidate parsed {payload!r}, baseline refused"
    assert _candidate_model_fields(parsed) == expected["model"], payload
    assert parsed.schema_version == 1
    assert parsed.sources == {} and parsed.selectors == []
    assert parsed.manifest_sha256 is None


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


def test_manifest_v1_byte_identical_with_opt_in_on_and_off(monkeypatch):
    """Schema 1 outcomes match the base-revision baseline under every opt-in state.

    Each corpus entry compares the candidate's exact error text (or parsed
    model fields) against the fixture captured from the base module, with the
    opt-in explicitly off, explicitly on, and defaulted under both env states.
    """
    corpus = _load_v1_baseline()
    path = Path("Skillfile.json")
    for entry in corpus:
        payload, expected = entry["payload"], entry["expected"]
        monkeypatch.delenv("CSK_EXPERIMENTAL_SKILLFILE_SOURCES", raising=False)
        _assert_matches_baseline(payload, expected, path, allow_schema_2=False)
        _assert_matches_baseline(payload, expected, path, allow_schema_2=None)
        monkeypatch.setenv("CSK_EXPERIMENTAL_SKILLFILE_SOURCES", "1")
        _assert_matches_baseline(payload, expected, path, allow_schema_2=True)
        _assert_matches_baseline(payload, expected, path, allow_schema_2=None)


def test_manifest_v1_baseline_covers_duplicate_combos():
    """The byte-identity corpus pins duplicate-plus-invalid source/git/ref cases."""
    ids = {entry["id"] for entry in _load_v1_baseline()}
    for required in (
        "duplicate-invalid-source-escape",
        "duplicate-invalid-source-abs",
        "duplicate-invalid-source-empty",
        "duplicate-invalid-git-empty",
        "duplicate-missing-ref",
        "duplicate-double-ref",
        "duplicate-empty-ref",
        "duplicate-unknown-field",
    ):
        assert required in ids, required


@pytest.mark.parametrize("source", ["../escape", "/abs", ""])
def test_manifest_v1_duplicate_precedes_invalid_source(tmp_path, source):
    """A duplicate name wins over an invalid source (legacy precedence)."""
    with pytest.raises(manifest.ManifestError) as excinfo:
        manifest.parse_manifest(
            {
                "schema_version": 1,
                "skills": [
                    {"name": "same", "tag": "v1"},
                    {"name": "same", "source": source, "tag": "v1"},
                ],
            },
            tmp_path / "Skillfile.json",
        )
    assert str(excinfo.value) == "Duplicate skill name in Skillfile: same"


def test_manifest_v1_duplicate_precedes_invalid_git(tmp_path):
    """A duplicate name wins over an invalid git field (legacy precedence)."""
    with pytest.raises(manifest.ManifestError) as excinfo:
        manifest.parse_manifest(
            {
                "schema_version": 1,
                "skills": [
                    {"name": "same", "tag": "v1"},
                    {"name": "same", "git": "", "tag": "v1"},
                ],
            },
            tmp_path / "Skillfile.json",
        )
    assert str(excinfo.value) == "Duplicate skill name in Skillfile: same"


@pytest.mark.parametrize(
    "second",
    [
        {"name": "same"},
        {"name": "same", "tag": "v1", "branch": "main"},
        {"name": "same", "tag": ""},
    ],
    ids=["missing-ref", "double-ref", "empty-ref"],
)
def test_manifest_v1_duplicate_precedes_invalid_ref(tmp_path, second):
    """A duplicate name wins over invalid ref fields (legacy precedence)."""
    with pytest.raises(manifest.ManifestError) as excinfo:
        manifest.parse_manifest(
            {"schema_version": 1, "skills": [{"name": "same", "tag": "v1"}, second]},
            tmp_path / "Skillfile.json",
        )
    assert str(excinfo.value) == "Duplicate skill name in Skillfile: same"


def test_manifest_v1_unknown_field_precedes_duplicate(tmp_path):
    """Unknown skill fields still win over the duplicate check (legacy order)."""
    with pytest.raises(manifest.ManifestError) as excinfo:
        manifest.parse_manifest(
            {
                "schema_version": 1,
                "skills": [
                    {"name": "same", "tag": "v1"},
                    {"name": "same", "tag": "v1", "extra": True},
                ],
            },
            tmp_path / "Skillfile.json",
        )
    assert str(excinfo.value) == "Skill declaration at index 1 has unsupported field(s): extra"
