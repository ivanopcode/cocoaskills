from __future__ import annotations

import pytest

from csk import config
from csk.source_identity import (
    SourceIdentityError,
    canonical_source_identity,
    is_allowed,
    is_canonical_source_identity,
    matches_prefix,
)


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        ("git@gitlab.example.com:skills/skill-wiki.git", "gitlab.example.com/skills/skill-wiki"),
        ("https://gitlab.example.com/skills/skill-wiki", "gitlab.example.com/skills/skill-wiki"),
        ("https://gitlab.example.com/skills/skill-wiki.git", "gitlab.example.com/skills/skill-wiki"),
        ("ssh://git@gitlab.example.com/skills/skill-wiki.git", "gitlab.example.com/skills/skill-wiki"),
        ("git@GitLab.Example.com:Skills/Skill-Wiki.git", "gitlab.example.com/Skills/Skill-Wiki"),
        ("https://gitlab.example.com/skills/skill-wiki/", "gitlab.example.com/skills/skill-wiki"),
    ],
)
def test_canonical_identity_normalizes_transports(url, expected):
    assert canonical_source_identity(url) == expected


def test_ssh_and_https_of_one_repository_share_identity():
    ssh = canonical_source_identity("git@gitlab.example.com:skills/skill-wiki.git")
    https = canonical_source_identity("https://gitlab.example.com/skills/skill-wiki")
    assert ssh == https


@pytest.mark.parametrize(
    "url",
    [
        "git@gitlab.example.com:skills/a b",
        "git@gitlab.example.com:skills/a#fragment",
        "https://gitlab.example.com/skills/a b",
    ],
)
def test_network_identity_rejects_noncanonical_repository_path(url):
    with pytest.raises(SourceIdentityError):
        canonical_source_identity(url)


def test_canonical_source_identity_shape():
    assert is_canonical_source_identity("gitlab.example.com/skills/文書")
    assert not is_canonical_source_identity("GitLab.example.com/skills/a")
    assert not is_canonical_source_identity("gitlab.example.com/skills/a b")


@pytest.mark.parametrize(
    "url",
    [
        "/abs/path/to/repo",
        "./relative/repo",
        "../relative/repo",
        "~/repo",
        "file:///abs/path/repo",
        "",
        "   ",
        "C:\\repos\\skill",
    ],
)
def test_local_sources_have_no_identity(url):
    assert canonical_source_identity(url) is None


def test_prefix_match_is_segment_aware():
    assert matches_prefix("gitlab.example.com/skills/skill-wiki", "gitlab.example.com/skills/")
    assert matches_prefix("gitlab.example.com/skills/skill-wiki", "gitlab.example.com/skills")
    assert matches_prefix("gitlab.example.com/skills", "gitlab.example.com/skills")
    assert not matches_prefix("gitlab.example.com/skills-evil/x", "gitlab.example.com/skills")
    assert not matches_prefix("gitlab.example.com/skills/skill-wiki", "")


def test_empty_allowlist_allows_everything():
    assert is_allowed("evil.example.com/x/y", ())
    assert is_allowed(None, ())


def test_allowlist_gates_network_identities_only():
    allowed = ("gitlab.example.com/skills/",)
    assert is_allowed("gitlab.example.com/skills/skill-wiki", allowed)
    assert not is_allowed("evil.example.com/skills/skill-wiki", allowed)
    # Local filesystem sources involve no network operation and pass.
    assert is_allowed(None, allowed)


def test_config_parses_allowed_sources(tmp_path):
    data = {
        "schema_version": 1,
        "skills_root": str(tmp_path / "skills"),
        "projects": {},
        "allowed_sources": ["gitlab.example.com/skills/", "gitlab.example.com/workflows/"],
    }
    cfg = config.parse_config(data, tmp_path / "config.json")
    assert cfg.allowed_sources == (
        "gitlab.example.com/skills/",
        "gitlab.example.com/workflows/",
    )


def test_config_rejects_malformed_allowed_sources(tmp_path):
    data = {
        "schema_version": 1,
        "skills_root": str(tmp_path / "skills"),
        "projects": {},
        "allowed_sources": ["ok", ""],
    }
    with pytest.raises(config.ConfigError, match="allowed_sources"):
        config.parse_config(data, tmp_path / "config.json")


def test_config_roundtrips_allowed_sources(tmp_path):
    data = {
        "schema_version": 1,
        "skills_root": str(tmp_path / "skills"),
        "projects": {},
        "allowed_sources": ["gitlab.example.com/skills/"],
    }
    cfg = config.parse_config(data, tmp_path / "config.json")
    config.save_config(cfg)
    reloaded = config.load_config(tmp_path / "config.json")
    assert reloaded.allowed_sources == ("gitlab.example.com/skills/",)
