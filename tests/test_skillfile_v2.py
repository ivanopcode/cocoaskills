"""Skillfile schema 2 source parsing.

Every test drives the production entry point
:csk.manifest.parse_manifest: (or :csk.config.skillfile_sources_enabled: for
legacy switch compatibility). The ``corpus_*`` cases mirror
``schema-cases/skillfile-v2/`` one by one, and
:test_skillfile_v2_conformance_corpus_matches_index: replays the authoritative
files when ``CSK_DRAFT_SOURCES_SUITE_ROOT`` is set.
"""

from __future__ import annotations

import copy
import hashlib
import json
import os
import re
from pathlib import Path
from typing import Any

import pytest

from csk import config as config_module
from csk import manifest, protocol_json
from csk.sources import errors as source_errors
from csk.sources import skillfile_v2

def _base_doc() -> dict[str, Any]:
    """Mirror corpus ``valid-mixed.json``; negatives mutate one field."""
    return {
        "schema_version": 2,
        "sources": {
            "local": {"path": "."},
            "team": {"repository": "example.org/kit", "tag": "v1.0.0"},
        },
        "skills": [
            {"name": "review", "from": "local", "directory": "agents/skills/review"},
            {
                "from": "team",
                "directory": "skills",
                "include": ["*"],
                "exclude": ["release"],
            },
            {"name": "legacy", "source": "team/legacy", "tag": "v1"},
        ],
    }


def _mutated(mutate: Any) -> dict[str, Any]:
    doc = _base_doc()
    mutate(doc)
    return doc


# ---------------------------------------------------------------------------
# Default schema support and legacy switch compatibility
# ---------------------------------------------------------------------------


def test_schema_2_accepted_without_legacy_opt_in(tmp_path, monkeypatch):
    monkeypatch.delenv("CSK_EXPERIMENTAL_SKILLFILE_SOURCES", raising=False)
    parsed = manifest.parse_manifest(_base_doc(), tmp_path / "Skillfile.json")
    assert parsed.schema_version == 2
    assert parsed.manifest_sha256 is not None


def test_schema_2_empty_skills_accepted_without_legacy_opt_in(tmp_path):
    parsed = manifest.parse_manifest(
        {"schema_version": 2, "skills": []}, tmp_path / "Skillfile.json", allow_schema_2=False
    )
    assert parsed.schema_version == 2
    assert parsed.skills == []


def test_schema_2_legacy_opt_out_is_ignored(tmp_path, monkeypatch):
    monkeypatch.setenv("CSK_EXPERIMENTAL_SKILLFILE_SOURCES", "1")
    parsed = manifest.parse_manifest(
        _base_doc(), tmp_path / "Skillfile.json", allow_schema_2=False
    )
    assert parsed.schema_version == 2


def test_schema_2_parses_with_legacy_config_flag(tmp_path):
    parsed = manifest.parse_manifest(
        _base_doc(), tmp_path / "Skillfile.json", allow_schema_2=True
    )
    assert parsed.schema_version == 2
    assert set(parsed.sources) == {"local", "team"}
    assert len(parsed.selectors) == 2
    assert [decl.name for decl in parsed.skills] == ["legacy"]


def test_schema_2_parses_with_legacy_environment_switch(tmp_path, monkeypatch):
    monkeypatch.setenv("CSK_EXPERIMENTAL_SKILLFILE_SOURCES", "1")
    parsed = manifest.parse_manifest(_base_doc(), tmp_path / "Skillfile.json")
    assert parsed.schema_version == 2


def test_schema_2_parses_with_legacy_config_switch(tmp_path, monkeypatch):
    monkeypatch.delenv("CSK_EXPERIMENTAL_SKILLFILE_SOURCES", raising=False)
    cfg = config_module.parse_config(
        {
            "schema_version": 1,
            "skills_root": str(tmp_path / "skills"),
            "projects": {},
            "experimental": {"skillfile_sources": True},
        },
        tmp_path / "config.json",
    )
    assert config_module.skillfile_sources_enabled(cfg) is True
    parsed = manifest.parse_manifest(
        _base_doc(),
        tmp_path / "Skillfile.json",
        allow_schema_2=config_module.skillfile_sources_enabled(cfg),
    )
    assert parsed.schema_version == 2


def test_schema_1_ignores_legacy_switch(tmp_path):
    parsed = manifest.parse_manifest(
        {"schema_version": 1, "skills": [{"name": "a", "tag": "v1"}]},
        tmp_path / "Skillfile.json",
        allow_schema_2=True,
    )
    assert parsed.schema_version == 1
    assert parsed.manifest_sha256 is None


@pytest.mark.parametrize("schema", [True, 2.0, "2"])
def test_schema_2_lookalikes_keep_type_error_without_hint(tmp_path, schema):
    with pytest.raises(manifest.ManifestError) as excinfo:
        manifest.parse_manifest(
            {"schema_version": schema, "skills": []},
            tmp_path / "Skillfile.json",
            allow_schema_2=True,
        )
    assert "must be an integer" in str(excinfo.value)
    assert "hint:" not in str(excinfo.value)


def test_other_schema_versions_keep_single_line_error(tmp_path):
    with pytest.raises(manifest.ManifestError) as excinfo:
        manifest.parse_manifest(
            {"schema_version": 99, "skills": []}, tmp_path / "Skillfile.json"
        )
    assert str(excinfo.value) == (
        "Unsupported Skillfile schema_version 99; this Skillfile requires a newer csk"
    )


def test_load_manifest_parses_schema_2_through_file(tmp_path, monkeypatch):
    monkeypatch.delenv("CSK_EXPERIMENTAL_SKILLFILE_SOURCES", raising=False)
    (tmp_path / "Skillfile.json").write_text(json.dumps(_base_doc()), encoding="utf-8")
    parsed = manifest.load_manifest(tmp_path)
    assert parsed is not None and parsed.schema_version == 2


# ---------------------------------------------------------------------------
# Corpus positives, driven through parse_manifest
# ---------------------------------------------------------------------------


def _valid_relative() -> dict[str, Any]:
    return {
        "schema_version": 2,
        "sources": {
            "local": {"path": "../shared"},
            "team": {"repository": "example.org/kit", "tag": "v1.0.0"},
        },
        "skills": [{"name": "review", "from": "local", "directory": "."}],
    }


def _valid_absolute() -> dict[str, Any]:
    doc = _valid_relative()
    doc["sources"]["local"] = {"path": "/work/skills"}
    return doc


def _valid_ssh() -> dict[str, Any]:
    doc = _valid_relative()
    doc["sources"]["local"] = {"git": "git@example.org:kit.git", "branch": "main"}
    return doc


def _valid_https() -> dict[str, Any]:
    doc = _valid_relative()
    doc["sources"]["local"] = {
        "git": "https://example.org/kit.git",
        "revision": "0" * 40,
    }
    return doc


def _valid_legacy_only() -> dict[str, Any]:
    return {"schema_version": 2, "skills": [{"name": "review", "tag": "v1"}]}


@pytest.mark.parametrize(
    "case_id,doc",
    [
        ("valid-mixed", _base_doc()),
        ("valid-relative", _valid_relative()),
        ("valid-absolute", _valid_absolute()),
        ("valid-ssh", _valid_ssh()),
        ("valid-https", _valid_https()),
        ("valid-legacy-only", _valid_legacy_only()),
    ],
    ids=[
        "valid-mixed",
        "valid-relative",
        "valid-absolute",
        "valid-ssh",
        "valid-https",
        "valid-legacy-only",
    ],
)
def test_skillfile_v2_corpus_positive_parses(tmp_path, case_id, doc):
    parsed = manifest.parse_manifest(
        copy.deepcopy(doc), tmp_path / "Skillfile.json", allow_schema_2=True
    )
    assert parsed.schema_version == 2
    assert parsed.manifest_sha256 is not None


def test_skillfile_v2_mixed_model_shape(tmp_path):
    parsed = manifest.parse_manifest(_base_doc(), tmp_path / "Skillfile.json", allow_schema_2=True)
    local = parsed.sources["local"]
    team = parsed.sources["team"]
    assert isinstance(local, skillfile_v2.PathSource) and local.path == "."
    assert isinstance(team, skillfile_v2.RepositorySource)
    assert team.repository == "example.org/kit"
    assert (team.ref_kind, team.ref_value) == ("tag", "v1.0.0")
    individual, collection = parsed.selectors
    assert isinstance(individual, skillfile_v2.IndividualSelector)
    assert (individual.name, individual.from_alias, individual.directory) == (
        "review",
        "local",
        "agents/skills/review",
    )
    assert isinstance(collection, skillfile_v2.CollectionSelector)
    assert collection.include == ("*",) and collection.exclude == ("release",)


def test_skillfile_v2_ssh_model_shape(tmp_path):
    parsed = manifest.parse_manifest(_valid_ssh(), tmp_path / "Skillfile.json", allow_schema_2=True)
    local = parsed.sources["local"]
    assert isinstance(local, skillfile_v2.GitSource)
    assert local.transport == "ssh"
    assert local.identity == "example.org/kit"
    assert (local.ref_kind, local.ref_value) == ("branch", "main")


def test_skillfile_v2_legacy_only_has_empty_sources(tmp_path):
    parsed = manifest.parse_manifest(
        _valid_legacy_only(), tmp_path / "Skillfile.json", allow_schema_2=True
    )
    assert parsed.sources == {} and parsed.selectors == []
    assert [decl.name for decl in parsed.skills] == ["review"]


# ---------------------------------------------------------------------------
# Corpus negatives, driven through parse_manifest (all selection_invalid)
# ---------------------------------------------------------------------------

_DIRECTORY_NEGATIVES = [
    "../review",
    "a/../b",
    "/review",
    "a//b",
    "a/./b",
    "a\\b",
    "a./b",
    "a /b",
    "C:review",
    "a/*",
    "a/CON",
    "a/..",
    "a/",
    "./review",
]


def _corpus_negative_cases() -> list[tuple[str, Any]]:
    cases: list[tuple[str, Any]] = [
        ("invalid-mixed-ref", lambda doc: doc["skills"][0].update({"tag": "v1"})),
        ("invalid-mixed-source", lambda doc: doc["skills"][0].update({"source": "other"})),
        (
            "invalid-mixed-git",
            lambda doc: doc["skills"][0].update({"git": "https://example.org/kit"}),
        ),
        ("invalid-mixed-collection", lambda doc: doc["skills"][0].update({"include": ["*"]})),
        ("invalid-unknown", lambda doc: doc["skills"][0].update({"extra": True})),
        ("invalid-recursive", lambda doc: doc["skills"][1].update({"include": ["**"]})),
        ("invalid-partial", lambda doc: doc["skills"][1].update({"include": ["rev*"]})),
        ("invalid-path", lambda doc: doc["skills"][1].update({"include": ["a/b"]})),
        ("invalid-empty", lambda doc: doc["skills"][1].update({"include": []})),
        ("invalid-duplicate", lambda doc: doc["skills"][1].update({"include": ["a", "a"]})),
        (
            "invalid-wildcard-exclude",
            lambda doc: doc["skills"][1].update({"exclude": ["*"]}),
        ),
        (
            "invalid-source-0",
            lambda doc: doc["sources"].update({"local": {"path": ".", "tag": "v1"}}),
        ),
        (
            "invalid-source-1",
            lambda doc: doc["sources"].update(
                {"local": {"path": ".", "git": "https://example.org/kit", "tag": "v1"}}
            ),
        ),
        (
            "invalid-source-2",
            lambda doc: doc["sources"].update({"local": {"git": "https://example.org/kit"}}),
        ),
        (
            "invalid-source-3",
            lambda doc: doc["sources"].update(
                {"local": {"git": "https://example.org/kit", "tag": "v1", "branch": "main"}}
            ),
        ),
        (
            "invalid-source-4",
            lambda doc: doc["sources"].update(
                {
                    "local": {
                        "git": "https://example.org/kit",
                        "repository": "example.org/kit",
                        "tag": "v1",
                    }
                }
            ),
        ),
        (
            "invalid-source-5",
            lambda doc: doc["sources"].update(
                {"local": {"git": "https://user:secret@example.org/kit", "tag": "v1"}}
            ),
        ),
        (
            "invalid-source-6",
            lambda doc: doc["sources"].update(
                {"local": {"git": "https://example.org:443/kit", "tag": "v1"}}
            ),
        ),
        (
            "invalid-source-7",
            lambda doc: doc["sources"].update({"local": {"repository": "../kit", "tag": "v1"}}),
        ),
        (
            "invalid-source-8",
            lambda doc: doc["sources"].update(
                {"local": {"repository": "example.org/a/../b", "tag": "v1"}}
            ),
        ),
        ("invalid-source-9", lambda doc: doc["sources"].update({"local": {"path": ""}})),
    ]
    for number, directory in enumerate(_DIRECTORY_NEGATIVES):
        cases.append(
            (
                f"invalid-directory-{number}",
                (lambda value: lambda doc: doc["skills"][0].update({"directory": value}))(
                    directory
                ),
            )
        )
    return cases


@pytest.mark.parametrize(
    "case_id,mutate",
    _corpus_negative_cases(),
    ids=[case for case, _ in _corpus_negative_cases()],
)
def test_skillfile_v2_corpus_negative_fails(tmp_path, case_id, mutate):
    with pytest.raises(source_errors.SourceError) as excinfo:
        manifest.parse_manifest(_mutated(mutate), tmp_path / "Skillfile.json", allow_schema_2=True)
    assert excinfo.value.code == source_errors.CODE_SELECTION_INVALID


def test_skillfile_v2_corpus_negative_count_pins_inventory():
    assert len(_corpus_negative_cases()) == 35


def test_skillfile_v2_conformance_corpus_matches_index(tmp_path):
    """Replay the authoritative schema files through parse_manifest.

    Skips without a suite checkout; the inline corpus above keeps the same
    coverage inside the repository.
    """
    root_text = os.environ.get("CSK_DRAFT_SOURCES_SUITE_ROOT")
    if not root_text:
        pytest.skip("CSK_DRAFT_SOURCES_SUITE_ROOT is not set")
    root = Path(root_text)
    index = json.loads((root / "index.json").read_bytes())
    entries = [entry for entry in index if entry["schema"] == "skillfile-v2.schema.json"]
    assert len(entries) == 41
    for entry in entries:
        data = json.loads((root / entry["instance"]).read_bytes())
        try:
            manifest.parse_manifest(
                data, tmp_path / "Skillfile.json", allow_schema_2=True, scope="project"
            )
        except (manifest.ManifestError, source_errors.SourceError):
            assert entry["valid"] is False, entry["instance"]
        else:
            assert entry["valid"] is True, entry["instance"]


# ---------------------------------------------------------------------------
# Acquisition grammar (AC b): refs, git endpoints, repository, path, aliases
# ---------------------------------------------------------------------------


def _with_team_source(acquisition: dict[str, Any]) -> dict[str, Any]:
    doc = _base_doc()
    doc["sources"]["team"] = acquisition
    return doc


@pytest.mark.parametrize("revision", ["0" * 40, "abcdef0123456789" * 4])
def test_skillfile_v2_revision_lengths_parse(tmp_path, revision):
    parsed = manifest.parse_manifest(
        _with_team_source({"git": "https://example.org/kit", "revision": revision}),
        tmp_path / "Skillfile.json",
        allow_schema_2=True,
    )
    team = parsed.sources["team"]
    assert isinstance(team, skillfile_v2.GitSource) and team.ref_value == revision


@pytest.mark.parametrize(
    "revision",
    ["0" * 39, "0" * 41, "0" * 63, "0" * 65, "A" * 40, "0" * 39 + "G", "", 123, None],
)
def test_skillfile_v2_bad_revision_fails(tmp_path, revision):
    with pytest.raises(source_errors.SourceError) as excinfo:
        manifest.parse_manifest(
            _with_team_source({"git": "https://example.org/kit", "revision": revision}),
            tmp_path / "Skillfile.json",
            allow_schema_2=True,
        )
    assert excinfo.value.code == source_errors.CODE_SELECTION_INVALID


@pytest.mark.parametrize(
    "ref", ["v1.0.0", "a", "a/b", "a-b_c.d", "release-ü", "v1.0.0-rc.1+meta"]
)
def test_skillfile_v2_tag_grammar_positives(tmp_path, ref):
    manifest.parse_manifest(
        _with_team_source({"git": "https://example.org/kit", "tag": ref}),
        tmp_path / "Skillfile.json",
        allow_schema_2=True,
    )


@pytest.mark.parametrize(
    "ref",
    [
        "",
        "/a",
        "a/",
        "a//b",
        "a..b",
        "a@{b",
        "@",
        ".a",
        "a/.b",
        "a.lock",
        "a.lock/b",
        "a b",
        "a~b",
        "a^b",
        "a:b",
        "a?b",
        "a*b",
        "a[b",
        "a\\b",
        "a.",
        "x" * 256,
        "é" * 200,
        123,
        None,
    ],
)
@pytest.mark.parametrize("kind", ["tag", "branch"])
def test_skillfile_v2_ref_grammar_negatives(tmp_path, kind, ref):
    with pytest.raises(source_errors.SourceError) as excinfo:
        manifest.parse_manifest(
            _with_team_source({"git": "https://example.org/kit", kind: ref}),
            tmp_path / "Skillfile.json",
            allow_schema_2=True,
        )
    assert excinfo.value.code == source_errors.CODE_SELECTION_INVALID


@pytest.mark.parametrize(
    "url,transport,identity",
    [
        ("https://example.org/kit", "https", "example.org/kit"),
        ("https://example.org/Kit.git", "https", "example.org/Kit"),
        ("https://example.org/répö/a", "https", "example.org/répö/a"),
        ("ssh://example.org/kit.git", "ssh", "example.org/kit"),
        ("ssh://deploy@example.org/kit", "ssh", "example.org/kit"),
        ("git@example.org:kit.git", "ssh", "example.org/kit"),
        ("example.org:kit/sub", "ssh", "example.org/kit/sub"),
        ("git@GitHub.com:Org/Repo.git", "ssh", "github.com/Org/Repo"),
    ],
)
def test_skillfile_v2_git_endpoint_positives(tmp_path, url, transport, identity):
    parsed = manifest.parse_manifest(
        _with_team_source({"git": url, "tag": "v1"}),
        tmp_path / "Skillfile.json",
        allow_schema_2=True,
    )
    team = parsed.sources["team"]
    assert isinstance(team, skillfile_v2.GitSource)
    assert team.transport == transport and team.identity == identity


@pytest.mark.parametrize(
    "url",
    [
        "git://example.org/kit",
        "http://example.org/kit",
        "file:///kit",
        "ftp://example.org/kit",
        "HTTPS://example.org/kit",
        "https://u@example.org/kit",
        "https://u:p@example.org/kit",
        "https://example.org:443/kit",
        "https://example.org/kit?x=1",
        "https://example.org/kit#frag",
        "https://example.org/kit%20x",
        "https://example.org/kit\\x",
        "https://example.org/",
        "https://example.org",
        "https://example.org/a/../b",
        "https://example.org/a/./b",
        "https://example.org//a",
        "https://bad host/kit",
        "ssh://example.org:22/kit",
        "ssh://u:p@example.org/kit",
        "ssh://example.org/",
        "ssh://@example.org/kit",
        "example.org:",
        ":kit",
        "example.org:a:b",
        "git@example.org:",
        " https://example.org/kit",
        "",
    ],
)
def test_skillfile_v2_git_endpoint_negatives(tmp_path, url):
    with pytest.raises(source_errors.SourceError) as excinfo:
        manifest.parse_manifest(
            _with_team_source({"git": url, "tag": "v1"}),
            tmp_path / "Skillfile.json",
            allow_schema_2=True,
        )
    assert excinfo.value.code == source_errors.CODE_SELECTION_INVALID


@pytest.mark.parametrize(
    "identity",
    ["example.org/kit", "h/a/b/c", "example.org/Kit", "example.org/a.git/b"],
)
def test_skillfile_v2_repository_positives(tmp_path, identity):
    parsed = manifest.parse_manifest(
        _with_team_source({"repository": identity, "tag": "v1"}),
        tmp_path / "Skillfile.json",
        allow_schema_2=True,
    )
    team = parsed.sources["team"]
    assert isinstance(team, skillfile_v2.RepositorySource) and team.repository == identity


@pytest.mark.parametrize(
    "identity",
    [
        "Example.org/kit",
        "example.org:1/kit",
        "user@example.org/kit",
        "example.org/kit.git",
        "example.org/",
        "example.org",
        "/kit",
        "../kit",
        "example.org/a/../b",
        "example.org/./a",
        "example.org//a",
        "example.org/a b",
        "example.org/a?b",
        "example.org/a#b",
        "example.org/" + "a" * 4096,
        "",
        123,
        None,
    ],
)
def test_skillfile_v2_repository_negatives(tmp_path, identity):
    with pytest.raises(source_errors.SourceError) as excinfo:
        manifest.parse_manifest(
            _with_team_source({"repository": identity, "tag": "v1"}),
            tmp_path / "Skillfile.json",
            allow_schema_2=True,
        )
    assert excinfo.value.code == source_errors.CODE_SELECTION_INVALID


@pytest.mark.parametrize(
    "path",
    [".", "..", "../shared", "/work/skills", "~/skills", "$HOME/skills", "C:\\skills", "rel/dir"],
)
def test_skillfile_v2_native_path_literals_parse(tmp_path, path):
    """Native paths are stored literally: no tilde/env/shell expansion."""
    parsed = manifest.parse_manifest(
        _with_team_source({"path": path}),
        tmp_path / "Skillfile.json",
        allow_schema_2=True,
    )
    team = parsed.sources["team"]
    assert isinstance(team, skillfile_v2.PathSource) and team.path == path


@pytest.mark.parametrize("path", ["", "a\x01b", "a\x7fb", "a" * 4097, 123, None])
def test_skillfile_v2_native_path_negatives(tmp_path, path):
    with pytest.raises(source_errors.SourceError) as excinfo:
        manifest.parse_manifest(
            _with_team_source({"path": path}),
            tmp_path / "Skillfile.json",
            allow_schema_2=True,
        )
    assert excinfo.value.code == source_errors.CODE_SELECTION_INVALID


@pytest.mark.parametrize("alias", ["a", "A", "a.b_c-d", "x" * 128])
def test_skillfile_v2_alias_positives(tmp_path, alias):
    doc = {"schema_version": 2, "sources": {alias: {"path": "."}}, "skills": []}
    parsed = manifest.parse_manifest(doc, tmp_path / "Skillfile.json", allow_schema_2=True)
    assert set(parsed.sources) == {alias}


@pytest.mark.parametrize(
    "alias", ["Bad Name", "-x", "con", "COM1", "a" * 129, "", 1, None]
)
def test_skillfile_v2_alias_negatives(tmp_path, alias):
    with pytest.raises(source_errors.SourceError) as excinfo:
        manifest.parse_manifest(
            {"schema_version": 2, "sources": {alias: {"path": "."}}, "skills": []},
            tmp_path / "Skillfile.json",
            allow_schema_2=True,
        )
    assert excinfo.value.code == source_errors.CODE_SELECTION_INVALID


@pytest.mark.parametrize(
    "sources",
    [
        [],
        {"local": []},
        {"local": {}},
        {"local": {"tag": "v1"}},
        {"local": {"repository": "example.org/kit"}},
        {"local": {"git": "https://example.org/kit", "tag": "v1", "bogus": 1}},
        {"local": {"path": ".", "bogus": 1}},
        {"local": {"repository": "example.org/kit", "tag": 123}},
    ],
)
def test_skillfile_v2_sources_structural_negatives(tmp_path, sources):
    with pytest.raises(source_errors.SourceError) as excinfo:
        manifest.parse_manifest(
            {"schema_version": 2, "sources": sources, "skills": []},
            tmp_path / "Skillfile.json",
            allow_schema_2=True,
        )
    assert excinfo.value.code == source_errors.CODE_SELECTION_INVALID


def test_skillfile_v2_sources_absent_parses(tmp_path):
    """A missing sources key parses as no sources."""
    parsed = manifest.parse_manifest(
        {"schema_version": 2, "skills": []}, tmp_path / "Skillfile.json", allow_schema_2=True
    )
    assert parsed.sources == {}


def test_skillfile_v2_sources_empty_object_parses(tmp_path):
    """An explicit empty sources map parses as no sources."""
    parsed = manifest.parse_manifest(
        {"schema_version": 2, "sources": {}, "skills": []},
        tmp_path / "Skillfile.json",
        allow_schema_2=True,
    )
    assert parsed.sources == {}


def test_skillfile_v2_sources_null_fails(tmp_path):
    """An explicit null sources value fails structurally (unlike absent)."""
    with pytest.raises(source_errors.SourceError) as excinfo:
        manifest.parse_manifest(
            {"schema_version": 2, "sources": None, "skills": []},
            tmp_path / "Skillfile.json",
            allow_schema_2=True,
        )
    assert excinfo.value.code == source_errors.CODE_SELECTION_INVALID


def test_skillfile_v2_sources_absent_and_empty_parse(tmp_path):
    for sources in (None, {}):
        doc: dict[str, Any] = {"schema_version": 2, "skills": []}
        if sources is not None:
            doc["sources"] = sources
        parsed = manifest.parse_manifest(doc, tmp_path / "Skillfile.json", allow_schema_2=True)
        assert parsed.sources == {}


# ---------------------------------------------------------------------------
# Selector forms (AC c), directories (AC d), include/exclude (AC e)
# ---------------------------------------------------------------------------


def test_skillfile_v2_unknown_alias_fails_with_stable_code(tmp_path):
    doc = _base_doc()
    doc["skills"][0]["from"] = "absent"
    with pytest.raises(source_errors.SourceError) as excinfo:
        manifest.parse_manifest(doc, tmp_path / "Skillfile.json", allow_schema_2=True)
    assert excinfo.value.code == source_errors.CODE_ALIAS_UNKNOWN
    assert "absent" in excinfo.value.detail


def test_skillfile_v2_alias_match_is_case_sensitive(tmp_path):
    doc = _base_doc()
    doc["skills"][0]["from"] = "Local"
    with pytest.raises(source_errors.SourceError) as excinfo:
        manifest.parse_manifest(doc, tmp_path / "Skillfile.json", allow_schema_2=True)
    assert excinfo.value.code == source_errors.CODE_ALIAS_UNKNOWN


@pytest.mark.parametrize(
    "element",
    [
        {"from": "local", "directory": "."},
        {"from": "local", "directory": ".", "name": "x", "include": ["*"]},
        {"from": "local", "directory": ".", "name": "x", "tag": "v1"},
        {"from": "local", "directory": ".", "name": "x", "branch": "main"},
        {"from": "local", "directory": ".", "name": "x", "revision": "0" * 40},
        {"from": "local", "directory": ".", "name": "x", "source": "other"},
        {"from": "local", "directory": ".", "name": "x", "git": "https://example.org/k"},
        {"from": "local", "directory": ".", "name": "x", "extra": True},
        {"from": "local", "directory": ".", "include": ["*"], "name": "x", "exclude": []},
        {"from": "local", "directory": ".", "include": ["*"], "bogus": 1},
        {"from": 123, "directory": ".", "name": "x"},
        {"from": "bad name", "directory": ".", "name": "x"},
        {"from": "local", "directory": ".", "name": "bad name"},
        {"from": "local", "directory": ".", "name": 123},
        {"from": "local", "name": "x"},
        {"from": "local", "directory": 123, "name": "x"},
        {"from": "local", "directory": ".", "include": "a"},
        {"from": "local", "directory": ".", "include": [1]},
        {"from": "local", "directory": ".", "include": ["a", "a"]},
        {"from": "local", "directory": ".", "include": ["*", "*"]},
        {"from": "local", "directory": ".", "include": ["*"], "exclude": "a"},
        {"from": "local", "directory": ".", "include": ["*"], "exclude": ["a", "a"]},
        {"from": "local", "directory": ".", "include": ["*"], "exclude": ["**"]},
        {"from": "local", "directory": ".", "include": ["*"], "exclude": ["a/b"]},
        {"from": "local", "directory": ".", "include": ["*"], "exclude": [1]},
    ],
)
def test_skillfile_v2_selector_negatives(tmp_path, element):
    doc = {"schema_version": 2, "sources": {"local": {"path": "."}}, "skills": [element]}
    with pytest.raises(source_errors.SourceError) as excinfo:
        manifest.parse_manifest(doc, tmp_path / "Skillfile.json", allow_schema_2=True)
    assert excinfo.value.code == source_errors.CODE_SELECTION_INVALID


@pytest.mark.parametrize("directory", ["", "*", "a?", "a[0]", "a]", 123, None])
def test_skillfile_v2_directory_extra_negatives(tmp_path, directory):
    doc = _base_doc()
    doc["skills"][0]["directory"] = directory
    with pytest.raises(source_errors.SourceError) as excinfo:
        manifest.parse_manifest(doc, tmp_path / "Skillfile.json", allow_schema_2=True)
    assert excinfo.value.code == source_errors.CODE_SELECTION_INVALID


def test_skillfile_v2_name_without_from_uses_legacy_grammar(tmp_path):
    doc = {
        "schema_version": 2,
        "sources": {"local": {"path": "."}},
        "skills": [{"name": "review", "directory": "."}],
    }
    with pytest.raises(manifest.ManifestError, match="unsupported field"):
        manifest.parse_manifest(doc, tmp_path / "Skillfile.json", allow_schema_2=True)


def test_skillfile_v2_non_object_element_fails(tmp_path):
    doc = {"schema_version": 2, "skills": ["review"]}
    with pytest.raises(manifest.ManifestError, match="must be an object"):
        manifest.parse_manifest(doc, tmp_path / "Skillfile.json", allow_schema_2=True)


def test_skillfile_v2_duplicate_individuals_conflict(tmp_path):
    doc = {
        "schema_version": 2,
        "sources": {"local": {"path": "."}},
        "skills": [
            {"name": "review", "from": "local", "directory": "."},
            {"name": "review", "from": "local", "directory": "other"},
        ],
    }
    with pytest.raises(source_errors.SourceError) as excinfo:
        manifest.parse_manifest(doc, tmp_path / "Skillfile.json", allow_schema_2=True)
    assert excinfo.value.code == source_errors.CODE_NAME_CONFLICT


@pytest.mark.parametrize("legacy_first", [True, False])
def test_skillfile_v2_mixed_legacy_selector_duplicate_conflicts(tmp_path, legacy_first):
    legacy = {"name": "review", "tag": "v1"}
    selector = {"name": "review", "from": "local", "directory": "."}
    skills = [legacy, selector] if legacy_first else [selector, legacy]
    doc = {"schema_version": 2, "sources": {"local": {"path": "."}}, "skills": skills}
    with pytest.raises(source_errors.SourceError) as excinfo:
        manifest.parse_manifest(doc, tmp_path / "Skillfile.json", allow_schema_2=True)
    assert excinfo.value.code == source_errors.CODE_NAME_CONFLICT


def test_skillfile_v2_legacy_legacy_duplicate_keeps_manifest_text(tmp_path):
    doc = {
        "schema_version": 2,
        "skills": [
            {"name": "review", "tag": "v1"},
            {"name": "review", "tag": "v2"},
        ],
    }
    with pytest.raises(manifest.ManifestError) as excinfo:
        manifest.parse_manifest(doc, tmp_path / "Skillfile.json", allow_schema_2=True)
    assert not isinstance(excinfo.value, source_errors.SourceError)
    assert str(excinfo.value) == "Duplicate skill name in Skillfile: review"


def test_skillfile_v2_legacy_extension_fields_still_apply(tmp_path):
    doc = {
        "schema_version": 2,
        "skills": [{"name": "review", "tag": "v1", "targets": ["x"]}],
    }
    parsed = manifest.parse_manifest(
        doc,
        tmp_path / "Skillfile.json",
        allow_schema_2=True,
        skill_extension_fields={"targets"},
    )
    assert [decl.name for decl in parsed.skills] == ["review"]


# ---------------------------------------------------------------------------
# Scope (AC f), top level, manifest hash (AC g)
# ---------------------------------------------------------------------------


def test_skillfile_v2_branch_admitted_in_project_scope(tmp_path):
    manifest.parse_manifest(_valid_ssh(), tmp_path / "Skillfile.json", allow_schema_2=True)


@pytest.mark.parametrize("scope", ["global", "transitive"])
def test_skillfile_v2_branch_refused_outside_project(tmp_path, scope):
    with pytest.raises(source_errors.SourceError) as excinfo:
        manifest.parse_manifest(
            _valid_ssh(), tmp_path / "Skillfile.json", allow_schema_2=True, scope=scope
        )
    assert excinfo.value.code == source_errors.CODE_SELECTION_INVALID
    assert "branch" in excinfo.value.detail


def test_skillfile_v2_path_refused_in_transitive_scope(tmp_path):
    with pytest.raises(source_errors.SourceError) as excinfo:
        manifest.parse_manifest(
            _base_doc(), tmp_path / "Skillfile.json", allow_schema_2=True, scope="transitive"
        )
    assert excinfo.value.code == source_errors.CODE_SELECTION_INVALID


def test_skillfile_v2_path_admitted_in_global_scope(tmp_path):
    parsed = manifest.parse_manifest(
        _base_doc(), tmp_path / "Skillfile.json", allow_schema_2=True, scope="global"
    )
    assert isinstance(parsed.sources["local"], skillfile_v2.PathSource)


def test_skillfile_v2_legacy_branch_admitted_in_global_scope(tmp_path):
    doc = {"schema_version": 2, "skills": [{"name": "review", "branch": "main"}]}
    parsed = manifest.parse_manifest(
        doc, tmp_path / "Skillfile.json", allow_schema_2=True, scope="global"
    )
    assert parsed.skills[0].ref.kind == "branch"


def test_skillfile_v2_unknown_scope_fails_closed(tmp_path):
    with pytest.raises(ValueError, match="unknown Skillfile scope"):
        manifest.parse_manifest(
            _base_doc(), tmp_path / "Skillfile.json", allow_schema_2=True, scope="nope"
        )
    with pytest.raises(ValueError, match="unknown Skillfile scope"):
        skillfile_v2.parse_sources({}, scope="nope")


def test_skillfile_v2_top_level_unknown_field_fails(tmp_path):
    doc = _base_doc()
    doc["extension"] = True
    with pytest.raises(manifest.ManifestError, match="unsupported field"):
        manifest.parse_manifest(doc, tmp_path / "Skillfile.json", allow_schema_2=True)


def test_skillfile_v2_requires_skills_list(tmp_path):
    with pytest.raises(manifest.ManifestError, match="requires field 'skills'"):
        manifest.parse_manifest(
            {"schema_version": 2, "sources": {}}, tmp_path / "Skillfile.json", allow_schema_2=True
        )


@pytest.mark.parametrize(
    "doc",
    [
        {"schema_version": 2, "skills": [], "agents": ["bad name"]},
        {"schema_version": 2, "skills": [], "locale": "pt_BR"},
        {"schema_version": 2, "skills": [], "project": {"alias": ""}},
    ],
)
def test_skillfile_v2_shared_fields_match_schema_1_text(tmp_path, doc):
    with pytest.raises(manifest.ManifestError) as excinfo_v2:
        manifest.parse_manifest(dict(doc), tmp_path / "Skillfile.json", allow_schema_2=True)
    legacy = dict(doc)
    legacy["schema_version"] = 1
    with pytest.raises(manifest.ManifestError) as excinfo_v1:
        manifest.parse_manifest(legacy, tmp_path / "Skillfile.json")
    assert str(excinfo_v2.value) == str(excinfo_v1.value)


def test_skillfile_v2_manifest_sha256_is_ccj1_digest(tmp_path):
    doc = _base_doc()
    parsed = manifest.parse_manifest(
        copy.deepcopy(doc), tmp_path / "Skillfile.json", allow_schema_2=True
    )
    assert parsed.manifest_sha256 is not None
    assert re.fullmatch(r"sha256:[0-9a-f]{64}", parsed.manifest_sha256)
    expected = "sha256:" + hashlib.sha256(protocol_json.canonical_bytes(doc)).hexdigest()
    assert parsed.manifest_sha256 == expected


def test_skillfile_v2_manifest_sha256_stable_over_key_order(tmp_path):
    doc = _base_doc()
    reordered = json.loads(json.dumps(doc, sort_keys=True))
    first = manifest.parse_manifest(doc, tmp_path / "a.json", allow_schema_2=True)
    second = manifest.parse_manifest(reordered, tmp_path / "b.json", allow_schema_2=True)
    assert first.manifest_sha256 == second.manifest_sha256


def test_skillfile_v2_manifest_sha256_changes_with_content(tmp_path):
    first = manifest.parse_manifest(
        _base_doc(), tmp_path / "a.json", allow_schema_2=True
    )
    changed = _base_doc()
    changed["skills"][0]["directory"] = "other"
    second = manifest.parse_manifest(changed, tmp_path / "b.json", allow_schema_2=True)
    assert first.manifest_sha256 != second.manifest_sha256
