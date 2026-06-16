from __future__ import annotations

import pytest

from csk.audit.capabilities import CapabilityParseError, parse_capabilities
from csk.audit.source_policy import SourcePolicyError, parse_source_policy


def test_parse_capabilities_accepts_declared_envelope():
    manifest = parse_capabilities(
        {
            "network": ["gitlab.example.com", "*.internal.example"],
            "filesystem": ["~/Library/Application Support/tool", "/tmp/tool-cache"],
            "exec": ["glab", "sentry-cli"],
            "secrets": ["GITLAB_TOKEN"],
            "env_read": ["HOME", "PATH"],
            "prompt_scope": "Uses project issue and merge request metadata.",
        }
    )

    assert manifest.network == ("gitlab.example.com", "*.internal.example")
    assert manifest.filesystem == ("~/Library/Application Support/tool", "/tmp/tool-cache")
    assert manifest.exec == ("glab", "sentry-cli")
    assert manifest.secrets == ("GITLAB_TOKEN",)
    assert manifest.env_read == ("HOME", "PATH")
    assert manifest.prompt_scope == "Uses project issue and merge request metadata."


def test_parse_capabilities_rejects_command_line_and_url_values():
    with pytest.raises(CapabilityParseError, match="executable name"):
        parse_capabilities({"exec": ["glab auth status"]})

    with pytest.raises(CapabilityParseError, match="host glob"):
        parse_capabilities({"network": ["https://gitlab.example.com"]})


def test_parse_capabilities_rejects_unknown_fields():
    with pytest.raises(CapabilityParseError, match="unsupported field"):
        parse_capabilities({"network": "none", "install": "curl | sh"})


def test_source_policy_classifies_git_hosts_and_local_sources():
    policy = parse_source_policy(
        {
            "default_class": "internal",
            "rules": [
                {"pattern": "github.com", "class": "public"},
                {"pattern": "*.example.org", "class": "public"},
            ],
        }
    )

    assert policy.classify(None, "git@github.com:ivanopcode/cocoaskills.git") == "public"
    assert policy.classify(None, "https://docs.example.org/repo.git") == "public"
    assert policy.classify(None, "git@gitlab.internal:group/repo.git") == "internal"
    assert policy.classify("/tmp/skill", None) == "internal"
    assert policy.classify("file:///tmp/skill", None) == "internal"


def test_source_policy_rejects_unknown_fields():
    with pytest.raises(SourcePolicyError, match="unsupported field"):
        parse_source_policy({"default_class": "internal", "unknown": True})

    with pytest.raises(SourcePolicyError, match="unsupported field"):
        parse_source_policy({"rules": [{"pattern": "github.com", "class": "public", "extra": True}]})
