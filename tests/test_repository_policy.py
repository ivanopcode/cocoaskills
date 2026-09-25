"""Repository transport policy parsing and pure endpoint resolution."""

from __future__ import annotations

import errno
import json
import os
from pathlib import Path
from typing import Any

import pytest

from csk import config
from csk.sources import repository_policy as policy


REPOSITORY = "example.org/kit"
SUITE_ROOT = os.environ.get("CSK_DRAFT_SOURCES_SUITE_ROOT")


def _endpoint(
    url: str = "https://example.org/kit.git",
    *,
    authentication: str = "team-https",
    **extra: Any,
) -> dict[str, Any]:
    value: dict[str, Any] = {"url": url, "authentication": authentication}
    value.update(extra)
    return value


def _document(
    endpoints: list[dict[str, Any]] | None = None,
    *,
    schema_version: int = 2,
    fallback: Any = "none",
    pin: str | None = None,
    aliases: dict[str, Any] | None = None,
    root_inputs: dict[str, Any] | None = None,
) -> dict[str, Any]:
    entry: dict[str, Any] = {
        "endpoints": endpoints or [_endpoint()],
        "fallback": fallback,
    }
    if pin is not None:
        entry["pin"] = pin
    document: dict[str, Any] = {
        "schema_version": schema_version,
        "repositories": {REPOSITORY: entry},
    }
    if aliases is not None:
        document["aliases"] = aliases
    if root_inputs is not None:
        document["root_inputs"] = root_inputs
    return document


def _schema_cases() -> list[dict[str, Any]]:
    if not SUITE_ROOT:
        return []
    root = Path(SUITE_ROOT)
    index = json.loads((root / "index.json").read_bytes())
    return [
        entry
        for entry in index
        if entry["schema"] in {"source-policy-v1.schema.json", "source-policy-v2.schema.json"}
    ]


@pytest.mark.parametrize(
    "entry",
    _schema_cases(),
    ids=[entry["instance"] for entry in _schema_cases()],
)
def test_source_policy_schema_cases_drive_production_parser(
    entry: dict[str, Any],
) -> None:
    """Drive every indexed source-policy case through ``parse_policy``."""

    if not SUITE_ROOT:
        pytest.skip("CSK_DRAFT_SOURCES_SUITE_ROOT is not set")
    instance = Path(SUITE_ROOT) / entry["instance"]
    document = json.loads(instance.read_bytes())
    try:
        policy.parse_policy(document)
    except policy.RepositoryPolicyError as exc:
        actual = False
        assert exc.code == policy.CODE_POLICY_INVALID
    else:
        actual = True
    assert actual is entry["valid"]


def test_source_policy_schema_case_inventory_is_the_two_indexed_families() -> None:
    if not SUITE_ROOT:
        pytest.skip("CSK_DRAFT_SOURCES_SUITE_ROOT is not set")
    cases = _schema_cases()
    assert len(cases) == 18
    assert {entry["schema"] for entry in cases} == {
        "source-policy-v1.schema.json",
        "source-policy-v2.schema.json",
    }


@pytest.mark.parametrize("schema_version", [0, 3, "2", True], ids=["zero", "future", "string", "bool"])
def test_unknown_or_wrong_policy_schema_version_is_invalid(schema_version: Any) -> None:
    document = _document(schema_version=2)
    document["schema_version"] = schema_version
    with pytest.raises(policy.RepositoryPolicyError) as excinfo:
        policy.parse_policy(document)
    assert excinfo.value.code == policy.CODE_POLICY_INVALID


def test_v2_port_is_stripped_before_identity_and_kept_in_provenance() -> None:
    parsed = policy.parse_policy(
        _document([_endpoint("https://example.org:8443/kit.git")])
    )
    plan = policy.select_endpoints(parsed, REPOSITORY)
    selected = plan.endpoints[0]
    assert selected.identity == REPOSITORY
    assert selected.provenance.url_port == 8443
    assert selected.provenance.resolved_host == "example.org"
    assert selected.provenance.resolved_port == 8443
    assert selected.provenance.alias is None
    assert selected.provenance.mirror_of is None


def test_authentication_is_an_opaque_operator_identifier() -> None:
    parsed = policy.parse_policy(
        _document([_endpoint(authentication="operator-provider-1")])
    )
    selected = policy.select_endpoints(parsed, REPOSITORY).endpoints[0]
    assert selected.authentication == "operator-provider-1"


@pytest.mark.parametrize(
    "authentication", ["/bin/sh", "provider command", "provider;command"], ids=["path", "spaces", "shell"]
)
def test_authentication_cannot_be_a_package_command_or_path(authentication: str) -> None:
    with pytest.raises(policy.RepositoryPolicyError) as excinfo:
        policy.parse_policy(_document([_endpoint(authentication=authentication)]))
    assert excinfo.value.code == policy.CODE_POLICY_INVALID


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        ("https://Example.org:1/a/kit.git", "example.org/a/kit"),
        ("https://example.org:65535/kit.git", REPOSITORY),
        ("ssh://git@example.org:2222/a/kit.git", "example.org/a/kit"),
        ("git@Example.org:2222/kit.git", "example.org/2222/kit"),
    ],
    ids=["https-min-port", "https-max-port", "ssh-uri-port", "scp-port-is-path"],
)
def test_canonical_endpoint_identity_strips_only_uri_ports(url: str, expected: str) -> None:
    assert policy.canonical_endpoint_identity(url) == expected


@pytest.mark.parametrize("revision", [True, False, 1.0, "2"], ids=["true", "false", "float", "string"])
def test_endpoint_parser_rejects_non_integer_revision(revision: Any) -> None:
    with pytest.raises(policy.RepositoryPolicyError) as excinfo:
        policy.parse_endpoint_url("https://example.org/kit.git", revision=revision)
    assert excinfo.value.code == policy.CODE_POLICY_INVALID


@pytest.mark.parametrize(
    "url",
    [
        "https://example.org:0/kit.git",
        "https://example.org:01/kit.git",
        "https://example.org:00/kit.git",
        "https://example.org:65536/kit.git",
        "ssh://example.org:0001/kit.git",
        "ssh://example.org:65536/kit.git",
    ],
    ids=[
        "zero",
        "leading-zero",
        "double-zero",
        "https-overflow",
        "ssh-leading-zero",
        "ssh-overflow",
    ],
)
def test_v2_rejects_zero_leading_zero_and_out_of_range_ports(url: str) -> None:
    with pytest.raises(policy.RepositoryPolicyError) as excinfo:
        policy.parse_policy(_document([_endpoint(url)]))
    assert excinfo.value.code == policy.CODE_POLICY_INVALID


def test_schema_1_endpoint_ports_remain_forbidden() -> None:
    with pytest.raises(policy.RepositoryPolicyError) as excinfo:
        policy.parse_policy(
            _document(
                [_endpoint("https://example.org:443/kit.git")], schema_version=1
            )
        )
    assert excinfo.value.code == policy.CODE_POLICY_INVALID


def test_endpoint_identity_mismatch_fails_before_selection() -> None:
    with pytest.raises(policy.RepositoryPolicyError) as excinfo:
        policy.parse_policy(
            _document(
                [_endpoint("https://evil.org/kit")],
                schema_version=1,
            )
        )
    assert excinfo.value.code == policy.CODE_POLICY_INVALID


@pytest.mark.parametrize("schema_version", [1, 2], ids=["schema-1", "schema-2"])
@pytest.mark.parametrize(
    "entry_point",
    [
        "parse_policy",
        "validate_canonical_identity",
        "resolve_endpoint",
        "select_endpoints",
        "resolve_repository",
    ],
    ids=["parse", "validate", "endpoint", "select", "repository"],
)
@pytest.mark.parametrize(
    "repository_key",
    ["example.org/kit.git", "example.org/a/kit.git"],
    ids=["single-component", "nested-path"],
)
def test_noncanonical_terminal_git_key(
    schema_version: int, entry_point: str, repository_key: str
) -> None:
    """Every public key-taking path refuses an already noncanonical key."""

    repository_path = repository_key.split("/", 1)[1]
    endpoint_url = f"https://example.org/{repository_path}.git"
    with pytest.raises(policy.RepositoryPolicyError) as excinfo:
        if entry_point == "parse_policy":
            policy.parse_policy(
                {
                    "schema_version": schema_version,
                    "repositories": {
                        repository_key: {
                            "endpoints": [_endpoint(endpoint_url)],
                            "fallback": "none",
                        }
                    },
                }
            )
        elif entry_point == "validate_canonical_identity":
            policy.validate_canonical_identity(repository_key)
        elif entry_point == "resolve_endpoint":
            policy.resolve_endpoint(
                repository_key,
                policy.RepositoryEndpoint(
                    url=endpoint_url,
                    authentication="team-https",
                ),
                revision=schema_version,
            )
        elif entry_point == "select_endpoints":
            candidate = policy.RepositoryPolicy(
                schema_version=schema_version,
                repositories={
                    repository_key: policy.RepositoryEntry(
                        identity=repository_key,
                        endpoints=(
                            policy.RepositoryEndpoint(
                                url=endpoint_url,
                                authentication="team-https",
                            ),
                        ),
                        fallback="none",
                    )
                },
            )
            policy.select_endpoints(candidate, repository_key)
        elif entry_point == "resolve_repository":
            candidate = policy.RepositoryPolicy(
                schema_version=schema_version,
                repositories={},
            )
            policy.resolve_repository(
                candidate,
                repository_key,
                declared_url=endpoint_url,
            )
        else:
            raise AssertionError(f"unknown entry point {entry_point!r}")
    assert excinfo.value.code == policy.CODE_POLICY_INVALID


def test_repeated_url_is_rejected_even_when_authentication_differs() -> None:
    with pytest.raises(policy.RepositoryPolicyError) as excinfo:
        policy.parse_policy(
            _document(
                [
                    _endpoint("https://example.org/kit.git", authentication="first"),
                    _endpoint("https://example.org/kit.git", authentication="second"),
                ]
            )
        )
    assert excinfo.value.code == policy.CODE_POLICY_INVALID


@pytest.mark.parametrize(
    "pin",
    [
        "https://example.org:8443/kit.git",
        "https://Example.org/kit.git",
        "https://example.org/kit.gitx",
        "https://example.org/kit.git/",
    ],
    ids=["port-only", "case-only", "one-character", "trailing-separator"],
)
def test_pin_requires_exact_listed_url_including_port(pin: str) -> None:
    with pytest.raises(policy.RepositoryPolicyError) as excinfo:
        policy.parse_policy(
            _document(
                [_endpoint("https://example.org/kit.git")],
                pin=pin,
            )
        )
    assert excinfo.value.code == policy.CODE_POLICY_INVALID


def test_pin_selects_one_endpoint_and_disables_fallback() -> None:
    first = _endpoint("https://example.org:8443/kit.git")
    second = _endpoint("ssh://example.org:2222/kit.git", authentication="team-ssh")
    parsed = policy.parse_policy(
        _document(
            [first, second],
            fallback="availability-auth",
            pin=second["url"],
        )
    )
    plan = policy.select_endpoints(parsed, REPOSITORY)
    assert [item.url for item in plan.endpoints] == [second["url"]]
    assert plan.pinned
    assert plan.next_endpoint("dns") is None


def test_unpinned_endpoints_keep_operator_list_order() -> None:
    first = _endpoint("https://example.org/kit.git")
    second = _endpoint("ssh://example.org/kit.git", authentication="team-ssh")
    parsed = policy.parse_policy(
        _document([first, second], fallback="availability-auth")
    )
    plan = policy.select_endpoints(parsed, REPOSITORY)
    assert [item.url for item in plan.endpoints] == [first["url"], second["url"]]
    assert plan.next_endpoint("dns") == plan.endpoints[1]
    assert plan.next_endpoint("tls") is None


def test_none_fallback_allows_only_the_first_listed_endpoint() -> None:
    parsed = policy.parse_policy(
        _document(
            [
                _endpoint("https://example.org/kit.git"),
                _endpoint("ssh://example.org/kit.git", authentication="team-ssh"),
            ],
            fallback="none",
        )
    )
    plan = policy.select_endpoints(parsed, REPOSITORY)
    assert plan.max_attempts == 1
    assert plan.next_endpoint("dns") is None


def test_v2_reader_accepts_schema_1_with_revision_1_semantics() -> None:
    document = {
        "schema_version": 1,
        "repositories": {
            REPOSITORY: {
                "endpoints": [_endpoint("git@example.org:kit.git")],
                "fallback": "none",
            }
        },
    }
    parsed = policy.parse_policy(document, reader_revision=2)
    assert parsed.schema_version == 1
    assert policy.select_endpoints(parsed, REPOSITORY).endpoints[0].port == 22


@pytest.mark.parametrize(
    ("fallback", "valid"),
    [
        ("none", True),
        ("availability-auth", True),
        (None, False),
        (False, False),
        (1, False),
        (1.5, False),
        ([], False),
        ({}, False),
        ("any-error", False),
    ],
    ids=[
        "none",
        "availability-auth",
        "null",
        "boolean",
        "integer",
        "number",
        "array",
        "object",
        "unknown-string",
    ],
)
@pytest.mark.parametrize("schema_version", [1, 2], ids=["schema-1", "schema-2"])
def test_malformed_fallback_typed(
    fallback: Any, valid: bool, schema_version: int
) -> None:
    document = _document(schema_version=schema_version, fallback=fallback)
    if valid:
        parsed = policy.parse_policy(document)
        assert parsed.repositories[REPOSITORY].fallback == fallback
        return

    with pytest.raises(policy.RepositoryPolicyError) as excinfo:
        policy.parse_policy(document)
    assert excinfo.value.code == policy.CODE_POLICY_INVALID
    assert REPOSITORY in excinfo.value.detail
    assert ".fallback" in excinfo.value.detail


def test_parser_rejects_non_portable_direct_python_values() -> None:
    document = _document()
    document["repositories"][REPOSITORY]["unexpected"] = object()
    with pytest.raises(policy.RepositoryPolicyError) as excinfo:
        policy.parse_policy(document)
    assert excinfo.value.code == policy.CODE_POLICY_INVALID


def test_endpoint_resolution_rejects_alias_chain_for_manually_built_models() -> None:
    aliases = {
        "first": policy.HostAlias(
            name="first", host="second", authentication="team-https"
        ),
        "second": policy.HostAlias(
            name="second", host="example.org", authentication="team-https"
        ),
    }
    endpoint = policy.RepositoryEndpoint(
        url="https://example.org/kit.git",
        authentication="team-https",
        alias="first",
        mirror_of=REPOSITORY,
    )
    with pytest.raises(policy.RepositoryPolicyError) as excinfo:
        policy.resolve_endpoint(REPOSITORY, endpoint, aliases=aliases)
    assert excinfo.value.code == policy.CODE_POLICY_INVALID


def test_embedded_alias_host_is_rejected_even_when_mirror_is_attested() -> None:
    with pytest.raises(policy.RepositoryPolicyError) as excinfo:
        policy.parse_policy(
            _document(
                [
                    _endpoint(
                        "ssh://git@corp-mirror/kit.git",
                        mirror_of=REPOSITORY,
                    )
                ],
                aliases={
                    "corp-mirror": {
                        "host": "mirror.corp.example",
                        "authentication": "team-ssh",
                    }
                },
            )
        )
    assert excinfo.value.code == policy.CODE_POLICY_INVALID


def test_v1_reader_rejects_schema_2_before_network_selection() -> None:
    with pytest.raises(policy.RepositoryPolicyError) as excinfo:
        policy.parse_policy(_document(), reader_revision=1)
    assert excinfo.value.code == policy.CODE_POLICY_INVALID


def test_reader_revision_alias_matches_explicit_reader_revision() -> None:
    document = _document(schema_version=1)
    assert policy.parse_policy(document, revision=2).schema_version == 1
    with pytest.raises(policy.RepositoryPolicyError) as excinfo:
        policy.parse_policy(document, revision=0)
    assert excinfo.value.code == policy.CODE_POLICY_INVALID


def test_conflicting_reader_revision_arguments_are_invalid() -> None:
    with pytest.raises(policy.RepositoryPolicyError) as excinfo:
        policy.parse_policy(_document(schema_version=1), reader_revision=1, revision=2)
    assert excinfo.value.code == policy.CODE_POLICY_INVALID


@pytest.mark.parametrize(
    ("document", "code"),
    [
        (
            _document(
                [_endpoint("https://evil.example.net/kit.git")],
            ),
            policy.CODE_MIRROR_UNDECLARED,
        ),
        (
            _document(
                [_endpoint(alias="absent-alias")],
                aliases={},
            ),
            policy.CODE_ALIAS_UNKNOWN,
        ),
        (
            _document(
                [_endpoint(alias="corp-mirror")],
                aliases={
                    "corp-mirror": {
                        "host": "mirror.corp.example",
                        "authentication": "team-https",
                    }
                },
            ),
            policy.CODE_MIRROR_UNDECLARED,
        ),
        (
            _document(
                [_endpoint("ssh://git@corp-mirror/kit.git")],
                aliases={
                    "corp-mirror": {
                        "host": "mirror.corp.example",
                        "authentication": "team-ssh",
                    }
                },
            ),
            policy.CODE_POLICY_INVALID,
        ),
        (
            _document(
                [_endpoint(alias="corp-mirror")],
                aliases={
                    "corp-mirror": {
                        "host": "example.org",
                        "authentication": "other-provider",
                    }
                },
            ),
            policy.CODE_POLICY_INVALID,
        ),
        (
            _document(
                [_endpoint("https://example.org:8443/kit.git", alias="corp-mirror")],
                aliases={
                    "corp-mirror": {
                        "host": "example.org",
                        "port": 9443,
                        "authentication": "team-https",
                    }
                },
            ),
            policy.CODE_POLICY_INVALID,
        ),
        (
            _document(
                [_endpoint(alias="first")],
                aliases={
                    "first": {"host": "second", "authentication": "team-https"},
                    "second": {"host": "example.org", "authentication": "team-https"},
                },
            ),
            policy.CODE_POLICY_INVALID,
        ),
        (
            _document(
                [_endpoint(mirror_of=REPOSITORY)],
            ),
            policy.CODE_POLICY_INVALID,
        ),
        (
            _document(
                [
                    _endpoint(
                        "https://mirror.example.net/kit.git",
                        mirror_of="other.example/kit",
                    )
                ],
            ),
            policy.CODE_POLICY_INVALID,
        ),
        (
            _document(
                [
                    _endpoint(
                        "https://mirror.example.net/kit.git",
                        alias="corp-mirror",
                        mirror_of=REPOSITORY,
                    )
                ],
                aliases={
                    "corp-mirror": {
                        "host": "another.example",
                        "authentication": "team-https",
                    }
                },
            ),
            policy.CODE_POLICY_INVALID,
        ),
    ],
    ids=[
        "undeclared-mirror",
        "unknown-alias",
        "alias-mirror-undeclared",
        "embedded-alias-host",
        "alias-auth-mismatch",
        "double-port",
        "alias-chain",
        "spurious-mirror-of",
        "mirror-of-mismatch",
        "mirror-url-with-alias",
    ],
)
def test_v2_structural_refusals_are_typed_before_selection(
    document: dict[str, Any], code: str
) -> None:
    with pytest.raises(policy.RepositoryPolicyError) as excinfo:
        policy.parse_policy(document)
    assert excinfo.value.code == code


def test_scp_endpoint_refuses_alias_selected_ssh_port() -> None:
    """SCP spelling has no port slot, so alias ports require an SSH URI."""

    document = _document(
        [
            _endpoint(
                "git@example.org:kit.git",
                authentication="team-ssh",
                alias="ssh-mirror",
                mirror_of=REPOSITORY,
            )
        ],
        aliases={
            "ssh-mirror": {
                "host": "mirror.example.net",
                "port": 2222,
                "authentication": "team-ssh",
            }
        },
    )
    with pytest.raises(policy.RepositoryPolicyError) as refused:
        policy.parse_policy(document)
    assert refused.value.code == policy.CODE_POLICY_INVALID
    assert "use an ssh:// endpoint" in refused.value.detail


def test_declared_mirror_may_be_first_and_uses_normal_fallback_order() -> None:
    mirror = _endpoint(
        "https://mirror.example.net/kit.git", mirror_of=REPOSITORY, authentication="mirror"
    )
    canonical = _endpoint("https://example.org/kit.git")
    parsed = policy.parse_policy(
        _document([mirror, canonical], fallback="availability-auth")
    )
    plan = policy.select_endpoints(parsed, REPOSITORY)
    assert plan.endpoints[0].host == "mirror.example.net"
    assert plan.next_endpoint("dns") == plan.endpoints[1]


@pytest.mark.parametrize(
    "failure_class",
    [
        "dns",
        "connection-refused",
        "timeout",
        "endpoint-unavailable",
        "http-502",
        "http-503",
        "http-504",
        "auth-unavailable",
        "auth-rejected",
        "http-401",
        "http-403",
        "ssh-auth-rejected",
    ],
    ids=lambda value: f"allowed-{value}",
)
def test_fallback_classification_allows_each_availability_auth_row(
    failure_class: str,
) -> None:
    assert policy.classify_failure(failure_class) == "availability-auth"
    assert policy.fallback_permitted("availability-auth", failure_class)


@pytest.mark.parametrize(
    "failure_class",
    [
        "tls",
        "host-key",
        "integrity",
        "identity",
        "ref-moved",
        "audit",
        "revocation",
        "canary",
        "assurance",
        "capability",
        "policy-unreadable",
        "http-404",
        "redirect",
        "malformed-response",
        "partial-response",
    ],
    ids=lambda value: f"forbidden-{value}",
)
def test_fallback_classification_forbids_each_fail_closed_row(
    failure_class: str,
) -> None:
    assert policy.classify_failure(failure_class) == "forbidden"
    assert not policy.fallback_permitted("availability-auth", failure_class)


@pytest.mark.parametrize(
    "failure_class", ["unknown", "partial-response", "made-up"], ids=lambda value: f"unclassified-{value}"
)
def test_fallback_classification_forbids_unclassified_failures(
    failure_class: str,
) -> None:
    assert policy.classify_failure(failure_class) == "forbidden"
    assert not policy.fallback_permitted("availability-auth", failure_class)


def test_fallback_none_and_pinned_plans_never_open_an_alternate() -> None:
    assert not policy.fallback_permitted("none", "dns")
    assert not policy.fallback_permitted("availability-auth", "dns", pinned=True)


def test_root_inputs_are_parsed_as_portable_alias_relative_paths() -> None:
    parsed = policy.parse_policy(
        _document(
            root_inputs={"project": ["SKILL.md", "agent-skill.json", "references"]}
        )
    )
    assert parsed.root_inputs == {
        "project": ("SKILL.md", "agent-skill.json", "references")
    }


@pytest.mark.parametrize(
    "root_inputs",
    [
        {"project": []},
        {"project": ["SKILL.md", "SKILL.md"]},
        {"project": ["references", "references/docs"]},
        {"project": ["../SKILL.md"]},
        {"project": ["/SKILL.md"]},
        {"project": ["SKILL.md\\x"]},
    ],
    ids=["empty", "duplicate", "overlap", "parent", "absolute", "backslash"],
)
def test_root_inputs_reject_unsafe_or_overlapping_entries(
    root_inputs: dict[str, Any],
) -> None:
    with pytest.raises(policy.RepositoryPolicyError) as excinfo:
        policy.parse_policy(_document(root_inputs=root_inputs))
    assert excinfo.value.code == policy.CODE_POLICY_INVALID


def test_source_policy_locator_follows_config_and_explicit_override(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config_path = tmp_path / "managed" / "config.json"
    monkeypatch.setenv("CSK_CONFIG", str(config_path))
    monkeypatch.delenv("CSK_SOURCE_POLICY", raising=False)
    assert config.source_policy_path() == config_path.parent / "source-policy.json"

    override = tmp_path / "ci-policy.json"
    monkeypatch.setenv("CSK_SOURCE_POLICY", str(override))
    assert config.source_policy_path() == override

    override.write_bytes(json.dumps(_document()).encode("utf-8"))
    loaded = config.load_source_policy()
    assert loaded is not None
    assert loaded.repositories[REPOSITORY].endpoints[0].url == "https://example.org/kit.git"


@pytest.mark.parametrize(
    ("loader", "environment_variable"),
    [
        (policy.load_policy, config.SOURCE_POLICY_ENV_VAR),
        (policy.load_policy, "CSK_CONFIG"),
        (config.load_source_policy, config.SOURCE_POLICY_ENV_VAR),
        (config.load_source_policy, "CSK_CONFIG"),
    ],
    ids=[
        "policy-source-policy",
        "policy-config",
        "config-source-policy",
        "config-config",
    ],
)
def test_locator_failure_typed(
    loader: Any,
    environment_variable: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Locator expansion failures stay inside the policy error boundary."""

    valid_path = tmp_path / "source-policy.json"
    valid_path.write_bytes(json.dumps(_document()).encode("utf-8"))
    assert loader(valid_path) is not None

    monkeypatch.delenv(config.SOURCE_POLICY_ENV_VAR, raising=False)
    monkeypatch.delenv("CSK_CONFIG", raising=False)
    if environment_variable == config.SOURCE_POLICY_ENV_VAR:
        monkeypatch.setenv(
            environment_variable,
            "~review_nonexistent_user_2741/source-policy.json",
        )
    else:
        monkeypatch.setenv(
            environment_variable,
            "~review_nonexistent_user_2741/config.json",
        )

    with pytest.raises(policy.RepositoryPolicyError) as excinfo:
        loader()
    assert excinfo.value.code == policy.CODE_POLICY_INVALID


@pytest.mark.parametrize(
    ("loader", "environment_variable"),
    [
        (policy.load_policy, config.SOURCE_POLICY_ENV_VAR),
        (policy.load_policy, "CSK_CONFIG"),
        (config.load_source_policy, config.SOURCE_POLICY_ENV_VAR),
        (config.load_source_policy, "CSK_CONFIG"),
    ],
    ids=[
        "policy-source-policy",
        "policy-config",
        "config-source-policy",
        "config-config",
    ],
)
def test_injected_locator_expansion_failure_is_typed(
    loader: Any,
    environment_variable: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    policy_path = tmp_path / "source-policy.json"
    config_path = tmp_path / "config.json"
    policy_path.write_bytes(json.dumps(_document()).encode("utf-8"))
    monkeypatch.delenv(config.SOURCE_POLICY_ENV_VAR, raising=False)
    monkeypatch.delenv("CSK_CONFIG", raising=False)
    target = policy_path if environment_variable == config.SOURCE_POLICY_ENV_VAR else config_path
    monkeypatch.setenv(environment_variable, str(target))
    original_expanduser = Path.expanduser
    reached = False

    def fail_expanduser(candidate: Path) -> Path:
        nonlocal reached
        if candidate == target:
            reached = True
            raise RuntimeError("injected path expansion failure")
        return original_expanduser(candidate)

    monkeypatch.setattr(Path, "expanduser", fail_expanduser)
    with pytest.raises(policy.RepositoryPolicyError) as excinfo:
        loader()
    assert reached
    assert excinfo.value.code == policy.CODE_POLICY_INVALID


def test_missing_policy_is_absent_but_malformed_policy_is_invalid(tmp_path: Path) -> None:
    missing = tmp_path / "missing.json"
    assert policy.load_policy(missing) is None

    malformed = tmp_path / "malformed.json"
    malformed.write_bytes(b"{")
    with pytest.raises(policy.RepositoryPolicyError) as excinfo:
        policy.load_policy(malformed)
    assert excinfo.value.code == policy.CODE_POLICY_INVALID


@pytest.mark.parametrize(
    "content",
    [b"", b'{"schema_version": 1, "repositories": {}}\n', b'{"schema_version": 1, "repositories": {}, "schema_version": 1}'],
    ids=["empty", "minimal-valid", "duplicate-key"],
)
def test_loader_rejects_empty_or_noncanonical_policy_bytes(
    tmp_path: Path, content: bytes
) -> None:
    path = tmp_path / "source-policy.json"
    path.write_bytes(content)
    if content == b'{"schema_version": 1, "repositories": {}}\n':
        assert policy.load_policy(path) is not None
        return
    with pytest.raises(policy.RepositoryPolicyError) as excinfo:
        policy.load_policy(path)
    assert excinfo.value.code == policy.CODE_POLICY_INVALID


@pytest.mark.parametrize(
    "failure_type",
    [PermissionError, FileNotFoundError, NotADirectoryError, IsADirectoryError],
    ids=["permission", "missing-after-stat", "not-directory", "directory"],
)
def test_loader_read_error_is_not_treated_as_absent(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    failure_type: type[OSError],
) -> None:
    path = tmp_path / "source-policy.json"
    path.write_bytes(json.dumps(_document()).encode("utf-8"))
    assert policy.load_policy(path) is not None
    original_read_bytes = Path.read_bytes

    def deny_read(candidate: Path) -> bytes:
        if candidate == path:
            raise failure_type("injected policy read failure")
        return original_read_bytes(candidate)

    monkeypatch.setattr(Path, "read_bytes", deny_read)
    with pytest.raises(policy.RepositoryPolicyError) as excinfo:
        policy.load_policy(path)
    assert excinfo.value.code == policy.CODE_POLICY_INVALID


def test_loader_lstat_error_is_structured(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "source-policy.json"
    path.write_bytes(json.dumps(_document()).encode("utf-8"))
    reached = False
    original_lstat = Path.lstat

    def deny_lstat(candidate: Path) -> Any:
        nonlocal reached
        if candidate == path:
            reached = True
            raise OSError(errno.ELOOP, "injected policy stat symlink loop")
        return original_lstat(candidate)

    monkeypatch.setattr(Path, "lstat", deny_lstat)
    with pytest.raises(policy.RepositoryPolicyError) as excinfo:
        policy.load_policy(path)
    assert reached
    assert excinfo.value.code == policy.CODE_POLICY_INVALID


def test_loader_runtime_error_at_lstat_is_structured(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "source-policy.json"
    path.write_bytes(json.dumps(_document()).encode("utf-8"))
    original_lstat = Path.lstat
    reached = False

    def deny_lstat(candidate: Path) -> Any:
        nonlocal reached
        if candidate == path:
            reached = True
            raise RuntimeError("injected resolution loop")
        return original_lstat(candidate)

    monkeypatch.setattr(Path, "lstat", deny_lstat)
    with pytest.raises(policy.RepositoryPolicyError) as excinfo:
        policy.load_policy(path)
    assert reached
    assert excinfo.value.code == policy.CODE_POLICY_INVALID


def test_loader_rejects_a_path_with_embedded_nul_as_structured_policy_error() -> None:
    with pytest.raises(policy.RepositoryPolicyError) as excinfo:
        policy.load_policy(Path("source-policy\x00.json"))
    assert excinfo.value.code == policy.CODE_POLICY_INVALID


def test_loader_rejects_invalid_utf8_as_structured_policy_error(tmp_path: Path) -> None:
    path = tmp_path / "source-policy.json"
    path.write_bytes(b"\xff")
    with pytest.raises(policy.RepositoryPolicyError) as excinfo:
        policy.load_policy(path)
    assert excinfo.value.code == policy.CODE_POLICY_INVALID


def test_loader_parse_error_is_structured_at_protocol_json_call_site(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "source-policy.json"
    path.write_bytes(json.dumps(_document()).encode("utf-8"))
    assert policy.load_policy(path) is not None
    reached = False

    def deny_parse(raw: bytes | str) -> Any:
        nonlocal reached
        reached = True
        raise RuntimeError("injected policy parse failure")

    monkeypatch.setattr(policy.protocol_json, "loads", deny_parse)
    with pytest.raises(policy.RepositoryPolicyError) as excinfo:
        policy.load_policy(path)
    assert reached
    assert excinfo.value.code == policy.CODE_POLICY_INVALID


def test_loader_validation_error_is_structured_at_parser_call_site(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "source-policy.json"
    path.write_bytes(json.dumps(_document()).encode("utf-8"))
    assert policy.load_policy(path) is not None
    reached = False

    def deny_validation(raw: Any, **kwargs: Any) -> policy.RepositoryPolicy:
        nonlocal reached
        reached = True
        raise RuntimeError("injected policy validation failure")

    monkeypatch.setattr(policy, "parse_policy", deny_validation)
    with pytest.raises(policy.RepositoryPolicyError) as excinfo:
        policy.load_policy(path)
    assert reached
    assert excinfo.value.code == policy.CODE_POLICY_INVALID


def test_schema_two_rejects_explicit_null_alias_port() -> None:
    with pytest.raises(policy.RepositoryPolicyError) as excinfo:
        policy.parse_policy(
            _document(
                [_endpoint(alias="corp-mirror")],
                aliases={
                    "corp-mirror": {
                        "host": "mirror.example.net",
                        "authentication": "team-https",
                        "port": None,
                    }
                },
            )
        )
    assert excinfo.value.code == policy.CODE_POLICY_INVALID


def test_symlinked_policy_is_not_followed_or_treated_as_absent(tmp_path: Path) -> None:
    target = tmp_path / "target.json"
    target.write_text(json.dumps(_document()), encoding="utf-8")
    linked = tmp_path / "source-policy.json"
    linked.symlink_to(target)
    with pytest.raises(policy.RepositoryPolicyError) as excinfo:
        policy.load_policy(linked)
    assert excinfo.value.code == policy.CODE_POLICY_INVALID


def test_logical_repository_without_policy_entry_is_unavailable() -> None:
    parsed = policy.parse_policy({"schema_version": 1, "repositories": {}})
    with pytest.raises(policy.RepositoryPolicyError) as excinfo:
        policy.resolve_repository(parsed, REPOSITORY)
    assert excinfo.value.code == policy.CODE_ENDPOINT_UNAVAILABLE


def test_declared_git_without_policy_entry_has_one_attempt_and_no_fallback() -> None:
    parsed = policy.parse_policy({"schema_version": 1, "repositories": {}})
    plan = policy.resolve_repository(
        parsed,
        REPOSITORY,
        declared_url="https://example.org/kit.git",
    )
    assert plan.max_attempts == 1
    assert plan.fallback == "none"
    assert plan.endpoints[0].identity == REPOSITORY
    assert plan.endpoints[0].authentication is None


def test_manually_constructed_resolution_plan_keeps_the_two_endpoint_cap() -> None:
    endpoint = policy.ResolvedEndpoint(
        identity=REPOSITORY,
        transport=policy.HTTPS,
        authentication=None,
        provenance=policy.EndpointProvenance(
            listed_url="https://example.org/kit.git",
            url_host="example.org",
            url_port=None,
            resolved_host="example.org",
            resolved_port=443,
            alias=None,
            mirror_of=None,
        ),
    )
    plan = policy.ResolutionPlan(
        identity=REPOSITORY,
        endpoints=(endpoint, endpoint, endpoint),
        fallback=policy.FALLBACK_AVAILABILITY_AUTH,
        pinned=False,
    )
    assert plan.max_attempts == 2
