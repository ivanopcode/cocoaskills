from __future__ import annotations

import base64
import hashlib
import json
import os
import urllib.request
from copy import deepcopy
from dataclasses import replace
from functools import lru_cache
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from csk import (
    audit_registry,
    closure,
    config,
    git_ops,
    hashing,
    identifiers,
    install_marker,
    installer,
    locking,
    manifest,
    protocol_json,
    skillspec,
    whitelist,
)
from csk.audit import pipeline as audit_pipeline
from csk.builds import metadata
from csk.builds import go_v1
from csk.config import RegistryConfig
from csk.source_identity import SourceIdentityError, parse_source_identity
from protocol_conformance_adapters import (
    _BUILD_REJECTION_BINDINGS,
    _LIFECYCLE_CASE_FIELDS,
    _project_toolchain_link_target,
    assert_build_positive_case,
    assert_build_rejection_case,
    assert_build_source_case,
    assert_capability_evidence_case,
    assert_generated_schema_case,
    assert_manager_lifecycle_case,
    assert_toolchain_case,
)
from protocol_lifecycle_observations import (
    clear_manager_lifecycle_observation_cache,
    observe_manager_lifecycle_case,
)


ROOT_TEXT = os.environ.get("CURATOR_CONFORMANCE_ROOT")
pytestmark = pytest.mark.skipif(not ROOT_TEXT, reason="CURATOR_CONFORMANCE_ROOT is not set")

EXPECTED_CANDIDATE_MANIFEST_SHA256 = (
    "sha256:12e58b82579645ba1ccafba49d3e2dd3216005ddf37ae63c68a9fafd46773071"
)
EXPECTED_CANDIDATE_PROTOCOL_VERSION = "1.0.0-rc.6"
EXPECTED_BUILD_DRIVER_FILES = (
    "build-input.ccj.json",
    "build-source-sha256.txt",
    "build-source.preimage.bin",
    "cache-key.txt",
    "context_files.json",
    "context_sha256.txt",
    "marker.json",
    "receipt-sha256.txt",
    "receipt.ccj.json",
    "toolchain-sha256.txt",
    "toolchain.preimage.bin",
)
IN_SCOPE_SCHEMA_NAMES = frozenset(
    {
        "agent-skill-v6.schema.json",
        "build-receipt-v1.schema.json",
        "conformance-claim-v1.schema.json",
        "conformance-claim-v2.schema.json",
        "conformance-claim-v3.schema.json",
        "csk-skill-v6.schema.json",
        "install-marker-v2.schema.json",
    }
)
MANAGER_LIFECYCLE_CLUSTERS = (
    "bootstrap_cases",
    "build_order_cases",
    "cache_publication_cases",
    "cross_project_cases",
    "dry_run_cases",
    "gc_cases",
    "launcher_cases",
    "planning_cases",
    "private_build_cases",
    "recovery_cases",
    "repair_cases",
    "status_cases",
    "transaction_cases",
    "upgrade_cases",
)


def _root() -> Path:
    assert ROOT_TEXT is not None
    root = Path(ROOT_TEXT)
    manifest = root / "manifest.json"
    assert manifest.is_file(), f"invalid conformance root: {root}"
    digest = "sha256:" + hashlib.sha256(manifest.read_bytes()).hexdigest()
    assert digest == EXPECTED_CANDIDATE_MANIFEST_SHA256
    return root


def _read_manifest_entry(
    root: Path,
    inventory: dict[str, str],
    relative: str,
) -> bytes:
    """Read one suite file only after its manifest membership and bytes agree."""
    assert relative in inventory, f"candidate manifest publishes no {relative}"
    relative_path = Path(relative)
    assert not relative_path.is_absolute()
    assert ".." not in relative_path.parts
    path = root / relative_path
    assert path.is_file(), f"candidate suite publishes no file at {relative}"
    raw = path.read_bytes()
    actual = "sha256:" + hashlib.sha256(raw).hexdigest()
    assert actual == inventory[relative], f"candidate digest mismatch for {relative}"
    return raw


@lru_cache(maxsize=1)
def _manifest_inventory() -> dict[str, str]:
    manifest = json.loads((_root() / "manifest.json").read_bytes())
    assert set(manifest) == {"files", "generated_at", "generator", "protocol_version"}
    assert manifest["protocol_version"] == EXPECTED_CANDIDATE_PROTOCOL_VERSION
    inventory: dict[str, str] = {}
    for entry in manifest["files"]:
        assert set(entry) == {"path", "sha256"}
        relative = entry["path"]
        assert isinstance(relative, str) and relative
        assert relative not in inventory, f"duplicate candidate manifest path: {relative}"
        assert not Path(relative).is_absolute() and ".." not in Path(relative).parts
        digest = entry["sha256"]
        assert isinstance(digest, str) and digest.startswith("sha256:")
        assert len(digest) == len("sha256:") + 64
        inventory[relative] = digest
    return inventory


def _authenticated_bytes(relative: str) -> bytes:
    if relative == "manifest.json":
        raw = (_root() / relative).read_bytes()
        assert "sha256:" + hashlib.sha256(raw).hexdigest() == (
            EXPECTED_CANDIDATE_MANIFEST_SHA256
        )
        return raw
    return _read_manifest_entry(_root(), _manifest_inventory(), relative)


def _json(relative: str) -> Any:
    return json.loads(_authenticated_bytes(relative))


def _golden_bytes(relative: str) -> bytes:
    """Read one golden as bytes, failing closed when the suite omits it."""
    return _authenticated_bytes(relative)


def _repository_root() -> Path:
    repository = _root().parent.parent
    assert (repository / "schemas" / "v1").is_dir()
    assert (repository / "release" / "1.0.0-rc.6.json").is_file()
    return repository


BUILD_DRIVER_VECTORS = _json("vectors/build-drivers.json") if ROOT_TEXT else {}
HOST_POLICY_VECTORS = (
    _json("vectors/go-host-execution-policy.json") if ROOT_TEXT else {}
)
CLAIM_QUALIFICATION_VECTORS = (
    _json("vectors/conformance-claim-v3-qualification.json") if ROOT_TEXT else {}
)
MANAGER_LIFECYCLE_VECTORS = (
    _json("vectors/manager-lifecycle.json") if ROOT_TEXT else {}
)
SCHEMA_CASES = (
    [
        entry
        for entry in _json("schema-cases/index.json")
        if entry["schema"] in IN_SCOPE_SCHEMA_NAMES
    ]
    if ROOT_TEXT
    else []
)


def _authenticate_rc6_scope() -> None:
    """Authenticate every rc.6 artifact before an adapter can consume it."""
    if not ROOT_TEXT:
        return
    required = {
        "vectors/build-drivers.json",
        "vectors/go-host-execution-policy.json",
        "vectors/conformance-claim-v3-qualification.json",
        "vectors/manager-lifecycle.json",
        "schema-cases/index.json",
        *(f"schema-cases/{entry['instance']}" for entry in SCHEMA_CASES),
        *(f"expected/build-driver/{name}" for name in EXPECTED_BUILD_DRIVER_FILES),
    }
    fixture_files = {
        path
        for path in _manifest_inventory()
        if path.startswith("fixtures/go-build-skill/")
    }
    assert fixture_files, "candidate manifest omits the go-build fixture"
    required.update(fixture_files)
    for relative in sorted(required, key=lambda value: value.encode("utf-8")):
        _authenticated_bytes(relative)


_authenticate_rc6_scope()
MANAGER_LIFECYCLE_CASES = (
    [
        (cluster, case)
        for cluster in MANAGER_LIFECYCLE_CLUSTERS
        for case in MANAGER_LIFECYCLE_VECTORS[cluster]
    ]
    if ROOT_TEXT
    else []
)


def _scalar_leaf_paths(
    value: Any,
    path: tuple[str | int, ...] = (),
) -> list[tuple[str | int, ...]]:
    if isinstance(value, dict):
        return [
            leaf
            for key in sorted(value)
            for leaf in _scalar_leaf_paths(value[key], (*path, key))
        ]
    if isinstance(value, list):
        return [
            leaf
            for index, item in enumerate(value)
            for leaf in _scalar_leaf_paths(item, (*path, index))
        ]
    return [path]


LIFECYCLE_SCALAR_MUTATIONS = [
    (cluster, case["name"], path)
    for cluster, case in MANAGER_LIFECYCLE_CASES
    for path in _scalar_leaf_paths(case)
]


def _mutation_id(item: tuple[str, str, tuple[str | int, ...]]) -> str:
    cluster, name, path = item
    rendered = ".".join(str(part) for part in path)
    return f"{cluster}:{name}:{rendered}"


def _mutate_scalar(value: Any) -> Any:
    if isinstance(value, bool):
        return not value
    if isinstance(value, int):
        return value + 1
    if isinstance(value, str):
        return value + "-mutated"
    raise AssertionError(f"unsupported lifecycle scalar leaf {value!r}")


def _mutate_path(value: Any, path: tuple[str | int, ...]) -> None:
    target = value
    for part in path[:-1]:
        target = target[part]
    leaf = path[-1]
    target[leaf] = _mutate_scalar(target[leaf])


CACHE_IDENTITY_CASES = (
    [
        (name, value)
        for name, value in BUILD_DRIVER_VECTORS.get("cache_identity", {}).items()
        if name != "aliases"
    ]
    if ROOT_TEXT
    else []
)
FAILURE_BOUNDARY_CASES = (
    list(HOST_POLICY_VECTORS.get("failure_boundary", {}).items())
    if ROOT_TEXT
    else []
)


def _policy_probes(platform: str) -> tuple[go_v1.ControlProbe, ...]:
    records = go_v1._NATIVE_CONTROL_PLATFORMS[platform]
    return tuple(
        go_v1.ControlProbe(
            name=name,
            availability=records[name].availability,
            mechanism=records[name].mechanism,
        )
        for name in go_v1.NATIVE_CONTROL_INVENTORY
    )


def test_rc6_candidate_manifest_and_release_record_are_exact_non_release_evidence() -> None:
    manifest_raw = _golden_bytes("manifest.json")
    manifest_digest = "sha256:" + hashlib.sha256(manifest_raw).hexdigest()
    manifest = json.loads(manifest_raw)
    release = json.loads(
        (_repository_root() / "release" / "1.0.0-rc.6.json").read_text(
            encoding="utf-8"
        )
    )

    assert manifest_digest == EXPECTED_CANDIDATE_MANIFEST_SHA256
    assert manifest["protocol_version"] == EXPECTED_CANDIDATE_PROTOCOL_VERSION
    assert release["protocol_version"] == EXPECTED_CANDIDATE_PROTOCOL_VERSION
    assert release["candidate_protocol_pin"] == {
        "manifest_sha256": EXPECTED_CANDIDATE_MANIFEST_SHA256,
        "suite_root": "conformance/v1",
    }
    assert (
        release["downstream_consumption"]["required_manifest_sha256"]
        == EXPECTED_CANDIDATE_MANIFEST_SHA256
    )
    assert release["downstream_consumption"]["environment"] == "CURATOR_CONFORMANCE_ROOT"
    assert release["downstream_consumption"]["committed_release_pin_advanced"] is False
    assert release["claim_v3"]["claims_emitted"] == []
    assert release["claim_v3"]["rc6_claim_schema"] is None


def test_rc6_in_scope_vector_inventory_is_exhaustive() -> None:
    schema_counts = {
        schema: sum(entry["schema"] == schema for entry in SCHEMA_CASES)
        for schema in IN_SCOPE_SCHEMA_NAMES
    }
    assert schema_counts == {
        "agent-skill-v6.schema.json": 24,
        "build-receipt-v1.schema.json": 18,
        "conformance-claim-v1.schema.json": 2,
        "conformance-claim-v2.schema.json": 7,
        "conformance-claim-v3.schema.json": 13,
        "csk-skill-v6.schema.json": 24,
        "install-marker-v2.schema.json": 14,
    }
    assert len(SCHEMA_CASES) == 102
    assert len(BUILD_DRIVER_VECTORS["positive_cases"]) == 8
    assert len(BUILD_DRIVER_VECTORS["rejection_cases"]) == 77
    assert set(_BUILD_REJECTION_BINDINGS) == {
        case["name"] for case in BUILD_DRIVER_VECTORS["rejection_cases"]
    }
    assert len(BUILD_DRIVER_VECTORS["build_source_cases"]) == 10
    assert len(BUILD_DRIVER_VECTORS["toolchain_cases"]) == 12
    assert len(HOST_POLICY_VECTORS["mandatory_controls"]) == 18
    assert len(HOST_POLICY_VECTORS["identity_and_protocol_cases"]) == 14
    assert len(HOST_POLICY_VECTORS["package_influence_cases"]) == 8
    assert len(HOST_POLICY_VECTORS["capability_evidence_cases"]) == 11
    assert len(HOST_POLICY_VECTORS["deferred_capability_rejection_guards"]) == 6
    assert len(FAILURE_BOUNDARY_CASES) == 3
    assert len(MANAGER_LIFECYCLE_CASES) == 32
    assert set(_LIFECYCLE_CASE_FIELDS) == {
        case["name"] for _cluster, case in MANAGER_LIFECYCLE_CASES
    }
    assert len(EXPECTED_BUILD_DRIVER_FILES) == 11
    assert len(CLAIM_QUALIFICATION_VECTORS["rules"]) == 4
    assert len(CLAIM_QUALIFICATION_VECTORS["platforms"]) == 3
    assert set(MANAGER_LIFECYCLE_VECTORS) == {
        *MANAGER_LIFECYCLE_CLUSTERS,
        "compiled_build_fixture",
        "schema_version",
    }
    expected_dir = _root() / "expected" / "build-driver"
    assert tuple(sorted(path.name for path in expected_dir.iterdir())) == tuple(
        sorted(EXPECTED_BUILD_DRIVER_FILES)
    )


def test_rc6_host_policy_identity_matches_build_driver_contract() -> None:
    assert HOST_POLICY_VECTORS["schema_version"] == 1
    assert HOST_POLICY_VECTORS["protocol_version"] == EXPECTED_CANDIDATE_PROTOCOL_VERSION
    assert HOST_POLICY_VECTORS["execution_policy"] == go_v1.EXECUTION_POLICY
    for name, host_identity in HOST_POLICY_VECTORS["cache_identity"].items():
        if name == "aliases":
            assert host_identity is BUILD_DRIVER_VECTORS["cache_identity"][name]
            continue
        driver_identity = BUILD_DRIVER_VECTORS["cache_identity"][name]
        assert host_identity == {
            field: driver_identity[field]
            for field in ("cache_key", "execution_policy", "input", "schema_valid")
        }
    assert BUILD_DRIVER_VECTORS["cache_identity"]["reserved_hardened"][
        "hardened_profile_owner"
    ] == HOST_POLICY_VECTORS["hardened_profile_owner"]
    assert HOST_POLICY_VECTORS["process_graph"] == list(go_v1.PROCESS_GRAPH)
    assert HOST_POLICY_VECTORS["session_states"] == list(go_v1.SESSION_STATES)
    guarantee_names = [
        item["name"]
        for item in HOST_POLICY_VECTORS["deferred_hardened_guarantees"]
    ]
    assert guarantee_names == list(go_v1.DEFERRED_HARDENED_GUARANTEES)


@pytest.mark.parametrize(
    "entry",
    SCHEMA_CASES,
    ids=lambda entry: f"{entry['schema']}:{entry['instance']}",
)
def test_rc6_generated_schema_case_is_consumed(
    entry: dict[str, Any],
    tmp_path: Path,
) -> None:
    assert_generated_schema_case(
        _repository_root(),
        _root(),
        entry,
        tmp_path,
    )


@pytest.mark.parametrize("name", EXPECTED_BUILD_DRIVER_FILES)
def test_rc6_expected_build_driver_artifact_matches_manifest(name: str) -> None:
    relative = f"expected/build-driver/{name}"
    raw = _golden_bytes(relative)
    manifest_files = {
        item["path"]: item["sha256"] for item in _json("manifest.json")["files"]
    }
    assert relative in manifest_files
    assert "sha256:" + hashlib.sha256(raw).hexdigest() == manifest_files[relative]


def test_rc6_expected_build_driver_tree_is_byte_exact() -> None:
    expected = "expected/build-driver"
    portable = BUILD_DRIVER_VECTORS["portable_identity"]
    fixture = BUILD_DRIVER_VECTORS["fixture"]
    source_case = next(
        case
        for case in BUILD_DRIVER_VECTORS["build_source_cases"]
        if case["name"] == "fixture-exact-build-source"
    )
    toolchain_case = next(
        case
        for case in BUILD_DRIVER_VECTORS["toolchain_cases"]
        if "entries" in case
    )

    assert _golden_bytes(f"{expected}/build-input.ccj.json") == base64.b64decode(
        portable["build_input_ccj_base64"]
    )
    assert _golden_bytes(f"{expected}/receipt.ccj.json") == base64.b64decode(
        portable["stored_receipt_base64"]
    )
    assert json.loads(_golden_bytes(f"{expected}/marker.json")) == portable["marker"]
    assert _golden_bytes(f"{expected}/build-source.preimage.bin") == base64.b64decode(
        source_case["preimage_base64"]
    )
    assert _golden_bytes(f"{expected}/toolchain.preimage.bin") == base64.b64decode(
        toolchain_case["preimage_base64"]
    )
    assert json.loads(_golden_bytes(f"{expected}/context_files.json")) == fixture[
        "expected_context_files"
    ]
    assert _golden_bytes(f"{expected}/build-source-sha256.txt").decode().strip() == fixture[
        "build_source"
    ]["content_sha256"]
    assert _golden_bytes(f"{expected}/toolchain-sha256.txt").decode().strip() == toolchain_case[
        "content_sha256"
    ]
    assert _golden_bytes(f"{expected}/context_sha256.txt").decode().strip() == fixture[
        "context_sha256"
    ]
    assert _golden_bytes(f"{expected}/cache-key.txt").decode().strip() == (
        "sha256:529370122ae11e2e961d5265b1a020e046bcd43165b2eb96b05e73a51187ac9b"
    )
    assert _golden_bytes(f"{expected}/receipt-sha256.txt").decode().strip() == (
        "sha256:919fbbad8e6ce95532219fd952c2309d0d7026f85209650508fd6834af4020cd"
    )


@pytest.mark.parametrize(
    "case",
    BUILD_DRIVER_VECTORS.get("positive_cases", []),
    ids=lambda case: case["name"],
)
def test_rc6_build_driver_positive_case(
    case: dict[str, Any],
    tmp_path: Path,
) -> None:
    assert_build_positive_case(case, BUILD_DRIVER_VECTORS, _root(), tmp_path)


@pytest.mark.parametrize(
    "case",
    BUILD_DRIVER_VECTORS.get("rejection_cases", []),
    ids=lambda case: case["name"],
)
def test_rc6_build_driver_rejection_case(
    case: dict[str, Any],
    tmp_path: Path,
) -> None:
    assert_build_rejection_case(case, BUILD_DRIVER_VECTORS, _root(), tmp_path)


@pytest.mark.parametrize(
    "case",
    BUILD_DRIVER_VECTORS.get("build_source_cases", []),
    ids=lambda case: case["name"],
)
def test_rc6_build_source_case(case: dict[str, Any], tmp_path: Path) -> None:
    assert_build_source_case(
        case,
        BUILD_DRIVER_VECTORS["build_source_cases"],
        tmp_path,
    )


@pytest.mark.parametrize(
    "case",
    BUILD_DRIVER_VECTORS.get("toolchain_cases", []),
    ids=lambda case: case["name"],
)
def test_rc6_toolchain_case(case: dict[str, Any], tmp_path: Path) -> None:
    assert_toolchain_case(case, BUILD_DRIVER_VECTORS["toolchain_cases"], tmp_path)


@pytest.mark.parametrize(
    ("name", "identity"),
    CACHE_IDENTITY_CASES,
    ids=[item[0] for item in CACHE_IDENTITY_CASES],
)
def test_rc6_execution_policy_cache_identity_is_non_alias(
    name: str,
    identity: dict[str, Any],
) -> None:
    assert BUILD_DRIVER_VECTORS["cache_identity"]["aliases"] is False
    canonical_key = "sha256:" + hashlib.sha256(
        protocol_json.canonical_bytes(identity["input"])
    ).hexdigest()
    assert canonical_key == identity["cache_key"]
    if identity["schema_valid"]:
        parsed = metadata.parse_build_input(identity["input"])
        assert parsed.policy.execution_policy == identity["execution_policy"]
        assert metadata.cache_key(parsed) == identity["cache_key"]
    else:
        rejection = next(
            case
            for case in BUILD_DRIVER_VECTORS["rejection_cases"]
            if (case.get("input") or {}).get("derived_cache_key")
            == identity["cache_key"]
        )
        with pytest.raises(metadata.BuildMetadataError) as raised:
            metadata.parse_build_input(identity["input"])
        assert raised.value.code == rejection["expected"]["error"]
    assert name in {"legacy_rc4_without_execution_policy", "portable", "reserved_hardened"}


def test_rc6_execution_policy_cache_keys_are_three_distinct_identities() -> None:
    keys = [identity["cache_key"] for _name, identity in CACHE_IDENTITY_CASES]
    assert len(keys) == len(set(keys)) == 3


@pytest.mark.parametrize(
    "case",
    HOST_POLICY_VECTORS.get("mandatory_controls", []),
    ids=lambda case: case["name"],
)
def test_rc6_mandatory_portable_control(case: dict[str, Any]) -> None:
    assert case["name"] in go_v1.MANDATORY_CONTROLS
    assert case["portable"] is True
    assert case["hardened_guarantee"] is False
    assert case["enforced"] == "always"
    assert isinstance(case["requirement"], str) and case["requirement"]
    assert isinstance(case["scope"], str) and case["scope"]


@pytest.mark.parametrize(
    "case",
    HOST_POLICY_VECTORS.get("identity_and_protocol_cases", []),
    ids=lambda case: case["name"],
)
def test_rc6_identity_and_protocol_failure_is_closed(case: dict[str, Any]) -> None:
    assert case["expected_error"] in {
        go_v1.CODE_WORKER_IDENTITY_INVALID,
        go_v1.CODE_WORKER_PROTOCOL_INVALID,
        go_v1.CODE_CONTROL_UNAVAILABLE,
    }
    assert case["published"] is False
    assert isinstance(case["worker_started"], bool)
    assert isinstance(case["compiler_started"], bool)


@pytest.mark.parametrize(
    "case",
    HOST_POLICY_VECTORS.get("package_influence_cases", []),
    ids=lambda case: case["name"],
)
def test_rc6_package_influence_failure_is_closed(case: dict[str, Any]) -> None:
    assert case["expected_error"] == go_v1.CODE_PACKAGE_INFLUENCE_FORBIDDEN
    assert case["manifest_field"] is None
    assert case["descriptor_field"] is None
    assert case["worker_started"] is False
    assert case["compiler_started"] is False
    assert case["published"] is False
    assert isinstance(case["surface"], str) and case["surface"]


@pytest.mark.parametrize(
    "case",
    HOST_POLICY_VECTORS.get("capability_evidence_cases", []),
    ids=lambda case: case["name"],
)
def test_rc6_capability_evidence_case(case: dict[str, Any]) -> None:
    assert_capability_evidence_case(case, HOST_POLICY_VECTORS)


@pytest.mark.parametrize(
    "case",
    HOST_POLICY_VECTORS.get("deferred_capability_rejection_guards", []),
    ids=lambda case: case["name"],
)
def test_rc6_deferred_hardened_capability_guard(case: dict[str, Any]) -> None:
    name = case["name"]
    assert name in go_v1.DEFERRED_HARDENED_GUARANTEES
    assert case["in_mandatory_controls"] is False
    assert case["in_native_control_inventory"] is False
    assert case["in_capability_evidence_record"] is False
    assert case["build_permitted_when_absent"] is True
    assert case["portable_rejection_code"] is None

    platform = go_v1.PLATFORM_MACOS
    record = go_v1.capability_evidence_from_mapping(
        HOST_POLICY_VECTORS["capability_evidence_record"]["examples"][platform]
    )
    probes = _policy_probes(platform)
    go_v1.validate_capability_evidence(record, platform, probes)
    entries = list(record.controls)
    entries[0] = replace(entries[0], name=name)
    with pytest.raises(go_v1.GoV1Error) as raised:
        go_v1.validate_capability_evidence(
            replace(record, controls=tuple(entries)),
            platform,
            probes,
        )
    assert raised.value.code == go_v1.CODE_HARDENED_CLAIM_FORBIDDEN


@pytest.mark.parametrize(
    ("name", "outcome"),
    FAILURE_BOUNDARY_CASES,
    ids=[item[0] for item in FAILURE_BOUNDARY_CASES],
)
def test_rc6_failure_boundary_outcome(name: str, outcome: dict[str, Any]) -> None:
    if outcome["rejects_build"]:
        assert outcome["expected_error"] == go_v1.CODE_CONTROL_UNAVAILABLE
        assert outcome["fails_before"] == "worker-launch"
        assert outcome["published"] is False
    else:
        assert outcome["expected_error"] is None
        assert outcome["fails_before"] is None
        assert outcome["published"] is True
    assert name in {
        "missing_deferred_hardened_capability",
        "missing_mandatory_portable_control",
        "unavailable_inventory_native_control",
    }


@pytest.mark.parametrize(
    "control",
    HOST_POLICY_VECTORS.get("native_control_inventory", {}).get("controls", []),
    ids=lambda control: control["name"],
)
def test_rc6_native_control_inventory_entry_is_exact(control: dict[str, Any]) -> None:
    assert control["name"] in go_v1.NATIVE_CONTROL_INVENTORY
    assert control["applied_when_available"] is True
    assert control["hardened_guarantee"] is False
    for platform in (go_v1.PLATFORM_MACOS, go_v1.PLATFORM_WINDOWS):
        expected = control["platforms"][platform]
        actual = go_v1._NATIVE_CONTROL_PLATFORMS[platform][control["name"]]
        assert actual.availability == expected["availability"]
        assert (actual.mechanism or None) == expected["mechanism"]
        assert (actual.unavailable_reason or None) == expected["unavailable_reason"]


def test_rc6_native_control_inventory_is_closed_and_exhaustive() -> None:
    inventory = HOST_POLICY_VECTORS["native_control_inventory"]
    assert inventory["version"] == go_v1.NATIVE_CONTROL_INVENTORY_VERSION
    assert inventory["exhaustive"] is True
    assert inventory["platforms"] == [go_v1.PLATFORM_MACOS, go_v1.PLATFORM_WINDOWS]
    assert [item["name"] for item in inventory["controls"]] == list(
        go_v1.NATIVE_CONTROL_INVENTORY
    )
    assert len(inventory["controls"]) == 5


def test_rc6_claim_versions_remain_separate() -> None:
    claims = [
        _json(f"schema-cases/conformance-claim-v{version}/valid.json")
        for version in (1, 2, 3)
    ]
    assert [(claim["schema_version"], claim["protocol_version"]) for claim in claims] == [
        (1, "1.0.0-rc.3"),
        (2, "1.0.0-rc.4"),
        (3, "1.0.0-rc.5"),
    ]
    assert "build_drivers" not in claims[0]
    assert "build_drivers" not in claims[1]
    assert claims[2]["build_drivers"]


def test_rc6_claim_v3_schema_stays_on_rc5_and_requires_build_drivers() -> None:
    schema = json.loads(
        (
            _repository_root()
            / "schemas"
            / "v1"
            / "conformance-claim-v3.schema.json"
        ).read_text(encoding="utf-8")
    )
    assert schema["properties"]["schema_version"]["const"] == 3
    assert schema["properties"]["protocol_version"]["const"] == "1.0.0-rc.5"
    assert "build_drivers" in schema["required"]


@pytest.mark.parametrize(
    "rule",
    CLAIM_QUALIFICATION_VECTORS.get("rules", []),
    ids=lambda rule: rule["name"],
)
def test_rc6_claim_qualification_rule(rule: dict[str, Any]) -> None:
    assert CLAIM_QUALIFICATION_VECTORS["protocol_version"] == "1.0.0-rc.6"
    assert CLAIM_QUALIFICATION_VECTORS["claim_schema_version"] == 3
    assert CLAIM_QUALIFICATION_VECTORS["candidate_claims_emitted"] == []
    if rule["name"] == "schema-valid-is-not-qualified":
        assert rule["required"] == "native-driver-platform-evidence"
    elif rule["name"] == "driver-platform-subset":
        assert rule["required"] == "each driver platform is also top-level evidenced"
    elif rule["name"] == "no-generic-driver":
        assert rule["allowed_drivers"] == ["go-repository-v1", "go-v1"]
    elif rule["name"] == "no-unevidenced-platform":
        assert rule["required"] == "every emitted tuple has immutable passing evidence"
    else:
        raise AssertionError(f"unknown claim qualification rule {rule['name']!r}")


@pytest.mark.parametrize(
    "platform",
    CLAIM_QUALIFICATION_VECTORS.get("platforms", []),
    ids=lambda platform: platform["name"],
)
def test_rc6_claim_platform_qualification(platform: dict[str, Any]) -> None:
    if platform["name"] == "linux":
        assert platform == {
            "name": "linux",
            "status": "excluded",
            "until_task": "TASK-260728-1skseh",
        }
    else:
        assert platform["name"] in {"macos", "windows"}
        assert platform["status"] == "pending-downstream-native-evidence"


@pytest.mark.parametrize(
    ("cluster", "case"),
    MANAGER_LIFECYCLE_CASES,
    ids=[f"{cluster}:{case['name']}" for cluster, case in MANAGER_LIFECYCLE_CASES],
)
def test_rc6_manager_lifecycle_case(
    cluster: str,
    case: dict[str, Any],
) -> None:
    assert_manager_lifecycle_case(cluster, case, MANAGER_LIFECYCLE_VECTORS)


@pytest.mark.parametrize(
    ("cluster", "case_name", "path"),
    LIFECYCLE_SCALAR_MUTATIONS,
    ids=[_mutation_id(item) for item in LIFECYCLE_SCALAR_MUTATIONS],
)
def test_rc6_every_lifecycle_scalar_leaf_is_mutation_sensitive(
    cluster: str,
    case_name: str,
    path: tuple[str | int, ...],
) -> None:
    assert len(LIFECYCLE_SCALAR_MUTATIONS) == 378
    case = deepcopy(
        next(
            item
            for item in MANAGER_LIFECYCLE_VECTORS[cluster]
            if item["name"] == case_name
        )
    )
    _mutate_path(case, path)
    with pytest.raises(AssertionError):
        assert_manager_lifecycle_case(
            cluster,
            case,
            MANAGER_LIFECYCLE_VECTORS,
        )


def test_rc6_manifest_entry_authentication_rejects_mutated_bytes(
    tmp_path: Path,
) -> None:
    relative = "vectors/build-drivers.json"
    raw = _authenticated_bytes(relative)
    root = tmp_path / "candidate"
    path = root / relative
    path.parent.mkdir(parents=True)
    path.write_bytes(raw)
    inventory = {relative: "sha256:" + hashlib.sha256(raw).hexdigest()}
    assert _read_manifest_entry(root, inventory, relative) == raw
    path.write_bytes(raw + b" ")
    with pytest.raises(AssertionError, match="digest mismatch"):
        _read_manifest_entry(root, inventory, relative)


def test_rc6_build_rejection_binding_is_mutation_sensitive(tmp_path: Path) -> None:
    case = deepcopy(
        next(
            item
            for item in BUILD_DRIVER_VECTORS["rejection_cases"]
            if item["name"] == "unknown-driver"
        )
    )
    case["expected"]["error"] = "definitely-not-the-product-error"
    with pytest.raises(AssertionError):
        assert_build_rejection_case(case, BUILD_DRIVER_VECTORS, _root(), tmp_path)


def test_rc6_fixed_environment_binding_is_mutation_sensitive(tmp_path: Path) -> None:
    vectors = deepcopy(BUILD_DRIVER_VECTORS)
    vectors["fixed_environment"]["GOENV"] = "poisoned"
    case = next(
        item
        for item in vectors["positive_cases"]
        if item["name"] == "fixed-environment-and-five-direct-argv-forms"
    )
    with pytest.raises(AssertionError):
        assert_build_positive_case(case, vectors, _root(), tmp_path)


def test_rc6_all_five_argv_records_are_mutation_sensitive(tmp_path: Path) -> None:
    vectors = deepcopy(BUILD_DRIVER_VECTORS)
    case = next(
        item
        for item in vectors["positive_cases"]
        if item["name"] == "fixed-environment-and-five-direct-argv-forms"
    )
    case["argv"][0]["argv"][2] = "on"
    with pytest.raises(AssertionError):
        assert_build_positive_case(case, vectors, _root(), tmp_path)


def test_rc6_darwin_launcher_fixture_does_not_require_host_chmod(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    launcher = tmp_path / "trusted-goroot" / "bin" / "go"
    launcher_key = os.path.normcase(os.path.abspath(launcher))
    real_chmod = Path.chmod

    def preserve_unexecutable_launcher(
        path: Path,
        mode: int,
        *args: Any,
        **kwargs: Any,
    ) -> None:
        if os.path.normcase(os.path.abspath(path)) == launcher_key:
            return
        real_chmod(path, mode, *args, **kwargs)

    monkeypatch.setattr(Path, "chmod", preserve_unexecutable_launcher)
    case = next(
        item
        for item in BUILD_DRIVER_VECTORS["positive_cases"]
        if item["name"] == "fixed-environment-and-five-direct-argv-forms"
    )

    assert_build_positive_case(case, BUILD_DRIVER_VECTORS, _root(), tmp_path)

    assert launcher.stat().st_mode & 0o111 == 0


def test_rc6_toolchain_fixture_materializes_targets_before_internal_links(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    case = next(
        item
        for item in BUILD_DRIVER_VECTORS["toolchain_cases"]
        if "entries" in item
    )
    real_symlink_to = Path.symlink_to
    observed_targets: list[Path] = []

    def require_materialized_target(
        path: Path,
        target: str | Path,
        target_is_directory: bool = False,
    ) -> None:
        assert isinstance(target, Path)
        native_target = Path(target)
        if not native_target.is_absolute():
            native_target = path.parent / native_target
        assert native_target.exists(), f"link target was not materialized: {target}"
        observed_targets.append(native_target)
        real_symlink_to(
            path,
            target,
            target_is_directory=target_is_directory,
        )

    monkeypatch.setattr(Path, "symlink_to", require_materialized_target)

    assert_toolchain_case(
        case,
        BUILD_DRIVER_VECTORS["toolchain_cases"],
        tmp_path,
    )

    assert len(observed_targets) == sum(
        entry["type"] == "symlink" for entry in case["entries"]
    )


def test_rc6_toolchain_fixture_projects_native_link_target_to_protocol_bytes() -> None:
    case = next(
        item
        for item in BUILD_DRIVER_VECTORS["toolchain_cases"]
        if "entries" in item
    )
    link = next(entry for entry in case["entries"] if entry["type"] == "symlink")
    protocol_target = link["target"]
    native_target = protocol_target.replace("/", "\\")

    assert (
        _project_toolchain_link_target(native_target, protocol_target)
        == protocol_target
    )
    with pytest.raises(AssertionError):
        _project_toolchain_link_target(native_target + "-other", protocol_target)


@pytest.mark.parametrize(
    "field",
    [
        "manager_home_lock_held_through_rollback",
        "require_current_digest_equals_desired_before_restore",
    ],
)
def test_rc6_rollback_contract_is_mutation_sensitive(field: str) -> None:
    case = deepcopy(
        next(
            item
            for item in MANAGER_LIFECYCLE_VECTORS["transaction_cases"]
            if item["name"] == "reverse-rollback-under-home-lock"
        )
    )
    case[field] = False
    with pytest.raises(AssertionError):
        assert_manager_lifecycle_case(
            "transaction_cases",
            case,
            MANAGER_LIFECYCLE_VECTORS,
        )


def test_rc6_lifecycle_binding_rejects_unknown_fields() -> None:
    case = deepcopy(MANAGER_LIFECYCLE_VECTORS["bootstrap_cases"][0])
    case["future_unbound_field"] = True
    with pytest.raises(AssertionError, match="unknown or missing fields"):
        assert_manager_lifecycle_case(
            "bootstrap_cases",
            case,
            MANAGER_LIFECYCLE_VECTORS,
        )


def test_rc6_lifecycle_binding_follows_cocoaskills_ordering_seam(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    original = closure._topological_order

    def reverse_only_protocol_fixture(nodes: dict[str, Any]) -> list[Any]:
        ordered = original(nodes)
        if set(nodes) == {"app", "data-provider", "ui-provider"}:
            return list(reversed(ordered))
        return ordered

    clear_manager_lifecycle_observation_cache()
    monkeypatch.setattr(closure, "_topological_order", reverse_only_protocol_fixture)
    case = deepcopy(MANAGER_LIFECYCLE_VECTORS["build_order_cases"][0])
    try:
        with pytest.raises(AssertionError):
            assert_manager_lifecycle_case(
                "build_order_cases",
                case,
                MANAGER_LIFECYCLE_VECTORS,
            )
    finally:
        clear_manager_lifecycle_observation_cache()


def _lifecycle_case(cluster: str, name: str) -> dict[str, Any]:
    return deepcopy(
        next(
            item
            for item in MANAGER_LIFECYCLE_VECTORS[cluster]
            if item["name"] == name
        )
    )


def _assert_sabotaged_lifecycle_case_differs(
    cluster: str,
    name: str,
) -> None:
    case = _lifecycle_case(cluster, name)
    fixture = MANAGER_LIFECYCLE_VECTORS["compiled_build_fixture"]
    observed = observe_manager_lifecycle_case(name, fixture)
    assert observed != case
    with pytest.raises(AssertionError):
        assert_manager_lifecycle_case(
            cluster,
            case,
            MANAGER_LIFECYCLE_VECTORS,
        )


def test_rc6_planning_binding_detects_omitted_skill_validation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = 0

    def omit_validation(*_args: object, **_kwargs: object) -> list[object]:
        nonlocal calls
        calls += 1
        return []

    clear_manager_lifecycle_observation_cache()
    monkeypatch.setattr(installer, "_validate_skills", omit_validation)
    try:
        _assert_sabotaged_lifecycle_case_differs(
            "planning_cases",
            "all-source-and-trust-gates-before-build",
        )
        assert calls > 0
    finally:
        clear_manager_lifecycle_observation_cache()


def test_rc6_private_failure_binding_detects_transient_home_lock(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = 0
    original = installer._build_private_misses

    def acquire_transient_home_lock(*args: Any, **kwargs: Any) -> Any:
        nonlocal calls
        config_value = args[0]
        plans = args[3]
        commands = {plan.command for plan in plans}
        if commands == {"golden-tool", "second-tool"}:
            calls += 1
            with locking.ManagerHomeLock(config_value.path.parent):
                pass
        return original(*args, **kwargs)

    clear_manager_lifecycle_observation_cache()
    monkeypatch.setattr(
        installer,
        "_build_private_misses",
        acquire_transient_home_lock,
    )
    try:
        _assert_sabotaged_lifecycle_case_differs(
            "private_build_cases",
            "second-build-failure-preserves-persistent-state",
        )
        assert calls >= 2
    finally:
        clear_manager_lifecycle_observation_cache()


def test_rc6_repair_binding_detects_omitted_audit_gate(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = 0

    def omit_audit(*_args: object, **_kwargs: object) -> audit_pipeline.GateResult:
        nonlocal calls
        calls += 1
        return audit_pipeline.GateResult(reports=())

    clear_manager_lifecycle_observation_cache()
    monkeypatch.setattr(installer.audit_pipeline, "gate_plans", omit_audit)
    try:
        _assert_sabotaged_lifecycle_case_differs(
            "repair_cases",
            "repair-rebuilds-invalid-compiled-entry",
        )
        assert calls > 0
    finally:
        clear_manager_lifecycle_observation_cache()


def test_rc6_recovery_binding_detects_omitted_generation_guard(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = 0

    def omit_guard(*_args: object, **_kwargs: object) -> None:
        nonlocal calls
        calls += 1

    clear_manager_lifecycle_observation_cache()
    monkeypatch.setattr(installer, "_assert_generation_current", omit_guard)
    try:
        _assert_sabotaged_lifecycle_case_differs(
            "recovery_cases",
            "install-recovery-runs-after-private-builds",
        )
        assert calls > 0
    finally:
        clear_manager_lifecycle_observation_cache()


def test_shared_fixture_legacy_marker_v1_stays_readable() -> None:
    """A marker-v1 installation written before schema 6 must still be read.

    `expected/marker.json` is the suite's frozen legacy-read evidence, not a
    writer golden: a manager writes marker schema 2 for every schema 1 through
    6 mutation, so this file may only ever be parsed, never reproduced.
    """
    legacy = install_marker.read_install_marker(_golden_bytes("expected/marker.json"))
    assert isinstance(legacy, install_marker.InstallMarkerV1)
    assert legacy.schema_version == 1
    assert legacy.skill_schema_version == 5
    assert legacy.to_json() == _json("expected/marker.json")


def test_shared_fixture_context_hash_and_marker(tmp_path: Path) -> None:
    fixture = _root() / "fixtures" / "skill"
    expected_files = _json("expected/context_files.json")
    expected_hash = (_root() / "expected" / "context_sha256.txt").read_text(encoding="utf-8").strip()
    # Writers emit marker schema 2 for skill schemas 1 through 6, so the writer
    # golden is the separately published marker-v2 fixture. A root without it
    # fails here rather than silently comparing schema-2 output to the frozen
    # schema-1 legacy-read evidence.
    expected_marker_bytes = _golden_bytes("expected/marker-v2.json")
    expected_marker = json.loads(expected_marker_bytes.decode("utf-8"))
    assert expected_marker["schema_version"] == 2

    spec = skillspec.load_skill_spec(fixture)
    destination = tmp_path / "context"
    files = whitelist.copy_context(
        fixture,
        destination,
        include_scripts=not any(command.type == "script" for command in spec.commands.values()),
        exclude_roots=spec.runtime_roots,
    )
    assert files == expected_files
    assert hashing.content_sha256(destination) == expected_hash

    commit = expected_marker["commit"]
    plan = installer.SkillPlan(
        decl=manifest.SkillDecl(
            name="golden-skill",
            source="golden-skill",
            ref=manifest.SkillRef("revision", commit),
        ),
        resolved=git_ops.ResolvedRef("revision", commit, commit),
        repo=fixture,
        snapshot=fixture,
        spec=spec,
    )
    marker = installer._marker_payload(
        plan,
        None,
        ["codex_cli"],
        content_hash=expected_hash,
        files=list(reversed(files)),
        activation={"context": True, "commands": ["golden-tool"]},
        requirers=["<project>"],
        substituted=None,
    )
    marker["installed_at"] = expected_marker["installed_at"]
    assert marker == expected_marker
    # The bytes this manager would commit to `.csk-install.json` are the
    # published golden bytes, and the suite's own bytes read back as the same
    # marker-v2 record.
    assert install_marker.serialize_install_marker(marker) == expected_marker_bytes
    published = install_marker.read_install_marker(expected_marker_bytes)
    assert isinstance(published, install_marker.InstallMarkerV2)
    assert published.to_json() == marker


@pytest.mark.parametrize(
    "case",
    _json("vectors/skill-manifest-resolution.json") if ROOT_TEXT else [],
)
def test_skill_manifest_resolution_vectors(case: dict[str, Any], tmp_path: Path) -> None:
    for relative, content in case["files"].items():
        path = tmp_path / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")

    if "error" in case:
        with pytest.raises(skillspec.SkillSpecError) as caught:
            skillspec.load_skill_spec(tmp_path)
        if case["error"] == "conflicting_skill_manifests":
            assert case["error"] in str(caught.value)
        return

    spec = skillspec.load_skill_spec(tmp_path)
    assert spec.source_file == case["expected_source"]
    assert sorted(spec.commands) == case["expected_commands"]


@pytest.mark.parametrize("case", _json("vectors/canonical-valid.json") if ROOT_TEXT else [])
def test_ccj_positive_vectors(case: dict[str, Any]) -> None:
    assert audit_registry.canonical_bytes(case["input"]).decode("utf-8") == case["canonical_utf8"]


@pytest.mark.parametrize("case", _json("vectors/canonical-invalid.json") if ROOT_TEXT else [])
def test_ccj_rejection_vectors(case: dict[str, str]) -> None:
    with pytest.raises(audit_registry.RegistryError):
        audit_registry.load_protocol_json(case["input_text"])


@pytest.mark.parametrize("case", _json("vectors/source-identities.json") if ROOT_TEXT else [])
def test_source_identity_vectors(case: dict[str, Any]) -> None:
    if "error" in case:
        with pytest.raises(SourceIdentityError):
            parse_source_identity(case["input"])
    else:
        assert parse_source_identity(case["input"]) == case["identity"]


@pytest.mark.parametrize("case", _json("vectors/identifiers.json") if ROOT_TEXT else [])
def test_identifier_vectors(case: dict[str, Any]) -> None:
    assert identifiers.is_valid_identifier(case["input"]) is case["valid"]


@pytest.mark.parametrize("case", _json("vectors/locale-selectors.json") if ROOT_TEXT else [])
def test_locale_selector_vectors(case: dict[str, Any]) -> None:
    assert identifiers.is_valid_locale(case["input"]) is case["valid"]


@pytest.mark.parametrize("case", _json("vectors/manager-config.json") if ROOT_TEXT else [])
def test_manager_config_vectors(case: dict[str, Any], tmp_path: Path) -> None:
    if not case["valid"]:
        with pytest.raises(config.ConfigError):
            config.parse_config(case["input"], tmp_path / "config.json")
        return
    parsed = config.parse_config(case["input"], tmp_path / "config.json")
    expected = case["expected"]
    assert parsed.default_agents == expected["default_agents"]
    assert parsed.adapter_mode == expected["adapter_mode"]
    assert [item.url for item in parsed.audit_registries] == expected["registry_urls"]
    if "project_alias" in expected:
        assert parsed.projects["app"].project_alias == expected["project_alias"]
        assert parsed.projects["app"].checkout_alias == expected["checkout_alias"]
    assert parsed.audit.snapshot_max_age_seconds == expected["snapshot_max_age_seconds"]
    assert parsed.audit.snapshot_clock_skew_seconds == expected["snapshot_clock_skew_seconds"]
    assert parsed.audit.cache_ttl_seconds == expected["cache_ttl_seconds"]
    assert parsed.audit.offline_grace_seconds == expected["offline_grace_seconds"]
    assert parsed.audit.max_request_bytes == expected["max_request_bytes"]


@pytest.mark.parametrize("case", _json("vectors/portable-paths.json") if ROOT_TEXT else [])
def test_portable_path_vectors(case: dict[str, Any]) -> None:
    assert identifiers.is_valid_portable_path(case["input"]) is case["valid"]


def test_closure_order_and_cycle_vectors() -> None:
    cases = {case["name"]: case for case in _json("vectors/closures.json")}
    diamond = cases["deterministic-diamond"]
    nodes = {name: SimpleNamespace(name=name, edges=[]) for name in diamond["nodes"]}
    for consumer, provider in diamond["edges"]:
        nodes[provider].edges.append(closure.ActivationEdge(consumer=consumer, mode="full"))
    assert [node.name for node in closure._topological_order(nodes)] == diamond["expected_provider_order"]

    cycle = cases["cycle"]
    cyclic = {name: SimpleNamespace(name=name, edges=[]) for name in {item for edge in cycle["edges"] for item in edge}}
    for consumer, provider in cycle["edges"]:
        cyclic[provider].edges.append(closure.ActivationEdge(consumer=consumer, mode="full"))
    with pytest.raises(closure.ClosureError, match="cycle"):
        closure._topological_order(cyclic)


def test_shared_registry_signatures_and_deny_wins() -> None:
    key = (_root() / "expected" / "registry" / "pinned_key.txt").read_text(encoding="utf-8").strip()
    audited_payload = _json("expected/registry/record_audited.json")
    revoked_payload = _json("expected/registry/record_revoked.json")
    forged_payload = _json("expected/registry/record_forged.json")
    wrong_key_payload = _json("expected/registry/record_wrong_key_id.json")
    audited = audit_registry.parse_record(audited_payload)
    revoked = audit_registry.parse_record(revoked_payload)
    assert audit_registry.verify_record(audited, (key,))
    assert audit_registry.verify_record(revoked, (key,))
    assert not audit_registry.verify_record(audit_registry.parse_record(forged_payload), (key,))
    assert not audit_registry.verify_record(audit_registry.parse_record(wrong_key_payload), (key,))

    registries = (
        RegistryConfig("first", "https://one.example", (key,)),
        RegistryConfig("second", "https://two.example", (key,)),
    )

    def fetch(url: str, source_identity: str, commit: str, content_sha256: str) -> list[dict[str, Any]]:
        return [audited_payload] if url == "https://one.example" else [revoked_payload]

    result = audit_registry.resolve(
        registries,
        source_identity=audited.source_identity,
        commit=audited.commit,
        content_sha256=audited.content_sha256,
        fetch=fetch,
    )
    assert result.result == audit_registry.RESULT_REVOKED


def test_shared_registry_snapshot_signature() -> None:
    key = (_root() / "expected" / "registry" / "pinned_key.txt").read_text(encoding="utf-8").strip()
    snapshot = _json("expected/registry/snapshot.json")
    assert audit_registry.verify_snapshot(snapshot, (key,))


REGISTRY_CLIENT_VECTOR = _json("vectors/registry-client.json") if ROOT_TEXT else {}


@pytest.mark.parametrize("case", REGISTRY_CLIENT_VECTOR.get("retry_cases", []))
def test_registry_client_retry_vectors(case: dict[str, Any]) -> None:
    assert (
        audit_registry.retry_permitted(case["method"], case["outcome"], case["idempotency_key"])
        is case["retry_permitted"]
    )


def test_registry_client_retry_execution_policy(monkeypatch: pytest.MonkeyPatch) -> None:
    policy = REGISTRY_CLIENT_VECTOR["retry_policy"]
    assert audit_registry.MAX_HTTP_ATTEMPTS == policy["max_attempts"]
    assert audit_registry.GET_TOTAL_DEADLINE_SECONDS == policy["get_total_deadline_seconds"]
    assert audit_registry.POST_TOTAL_DEADLINE_SECONDS == policy["post_total_deadline_seconds"]
    assert policy["follow_redirects"] is False
    assert audit_registry._RejectRegistryRedirect().redirect_request() is None

    class UnavailableResponse:
        status = 503
        headers = {"Content-Type": "application/json", "Retry-After": "0"}

        def __enter__(self) -> UnavailableResponse:
            return self

        def __exit__(self, *_args: Any) -> None:
            return None

    requests: list[urllib.request.Request] = []

    def unavailable(request: urllib.request.Request, *, timeout: float) -> UnavailableResponse:
        assert timeout > 0
        requests.append(request)
        return UnavailableResponse()

    monkeypatch.setattr(audit_registry, "_open_registry_request", unavailable)
    monkeypatch.setattr(audit_registry.time, "sleep", lambda _seconds: None)
    with pytest.raises(audit_registry.RegistryError, match="HTTP 503"):
        audit_registry.http_get_snapshot("https://registry.example")
    assert len(requests) == policy["max_attempts"]

    record = (_root() / "expected" / "registry" / "record_audited.json").read_bytes()
    requests.clear()
    with pytest.raises(audit_registry.RegistryError, match="HTTP 503"):
        audit_registry.http_publish_record("https://registry.example", "secret-token", record)
    assert len(requests) == policy["max_attempts"]
    bodies = [request.data for request in requests]
    keys = [
        next(
            value
            for name, value in request.header_items()
            if name.lower() == "idempotency-key"
        )
        for request in requests
    ]
    assert all(body == record for body in bodies)
    assert keys[0] and all(key == keys[0] for key in keys)


def _conformance_key() -> tuple[Ed25519PrivateKey, str]:
    private = Ed25519PrivateKey.generate()
    public = private.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)
    return private, "ed25519:" + base64.b64encode(public).decode("ascii")


def _conformance_snapshot(version: int, head: str) -> dict[str, Any]:
    return {
        "schema_version": 1,
        "merkle_root": "b" * 64,
        "log_size": version,
        "head": head,
        "version": version,
        "created_at": "2026-07-13T00:00:00Z",
    }


def _sign_conformance_snapshot(
    private: Ed25519PrivateKey, body: dict[str, Any]
) -> dict[str, Any]:
    public = private.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)
    signed = dict(body)
    signed["sig"] = {
        "key_id": hashlib.sha256(public).hexdigest()[:16],
        "algorithm": "ed25519",
        "signature": base64.b64encode(private.sign(audit_registry.canonical_bytes(body))).decode("ascii"),
    }
    return signed


@pytest.mark.parametrize("case", REGISTRY_CLIENT_VECTOR.get("snapshot_transitions", []))
def test_registry_client_snapshot_transition_vectors(case: dict[str, Any], tmp_path: Path) -> None:
    assert REGISTRY_CLIENT_VECTOR["state_key"] == "canonical_registry_url"
    assert REGISTRY_CLIENT_VECTOR["key_rotation_resets_state"] is False
    old_private, old_pin = _conformance_key()
    new_private, new_pin = _conformance_key()
    registry_url = "https://registry.example/curator"
    initial = RegistryConfig("before-rotation", registry_url, (old_pin, new_pin))
    stored = _sign_conformance_snapshot(
        old_private, _conformance_snapshot(case["stored_version"], "a" * 64)
    )
    now = audit_registry._parse_iso8601("2026-07-13T01:00:00Z")
    assert now is not None
    unavailable, warnings = audit_registry.check_snapshots(
        (initial,), tmp_path, fetch_snapshot=lambda _url: stored, now=now
    )
    assert unavailable == set()
    assert warnings == []

    candidate_body = _conformance_snapshot(case["candidate_version"], "a" * 64)
    if not case["same_body"] and case["candidate_version"] == case["stored_version"]:
        candidate_body["head"] = "c" * 64
    candidate = _sign_conformance_snapshot(new_private, candidate_body)
    rotated = RegistryConfig("after-rotation", registry_url, (old_pin, new_pin))
    unavailable, _ = audit_registry.check_snapshots(
        (rotated,), tmp_path, fetch_snapshot=lambda _url: candidate, now=now
    )
    assert (registry_url not in unavailable) is case["accepted"]


@pytest.mark.parametrize("case", REGISTRY_CLIENT_VECTOR.get("pagination_rejections", []))
def test_registry_client_pagination_rejection_vectors(
    case: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    calls = 0

    class FakeResponse:
        status = 200
        headers = {"Content-Type": "application/json"}

        def __init__(self, body: bytes):
            self.body = body

        def __enter__(self) -> FakeResponse:
            return self

        def __exit__(self, *_args: Any) -> None:
            return None

        def read(self, size: int = -1) -> bytes:
            return self.body if size < 0 else self.body[:size]

    def fake_urlopen(request: urllib.request.Request, timeout: int = 0) -> FakeResponse:
        nonlocal calls
        del request, timeout
        calls += 1
        if case["error"] == "pagination_cycle":
            body = json.dumps({"records": [], "next_cursor": "same"}).encode()
        elif case["error"] == "invalid_cursor":
            body = json.dumps(
                {"records": [], "next_cursor": "x" * case["characters"]}
            ).encode()
        elif case["error"] == "record_limit":
            remaining = case["records"] - (calls - 1) * 1000
            page_size = min(remaining, 1000)
            next_cursor = f"page-{calls + 1}" if remaining > page_size else None
            body = json.dumps(
                {"records": [{} for _ in range(page_size)], "next_cursor": next_cursor}
            ).encode()
        elif case["error"] == "body_limit":
            body = b"x" * case["bytes"]
        else:
            raise AssertionError(f"unknown pagination vector error {case['error']!r}")
        return FakeResponse(body)

    monkeypatch.setattr(audit_registry, "_open_registry_request", fake_urlopen)
    expected = {
        "pagination_cycle": "repeated a pagination cursor",
        "invalid_cursor": "next_cursor",
        "record_limit": "10000-record limit",
        "body_limit": "exceeds 16 MiB",
    }[case["error"]]
    with pytest.raises(audit_registry.RegistryError, match=expected):
        audit_registry._http_get_records("https://registry.example/v1/records?limit=1000")


@pytest.mark.parametrize("case", REGISTRY_CLIENT_VECTOR.get("rollback_state_cases", []))
def test_registry_client_rollback_state_vectors(case: dict[str, Any], tmp_path: Path) -> None:
    private, pin = _conformance_key()
    registry_url = "https://registry.example/state"
    registry = RegistryConfig("state-case", registry_url, (pin,))
    snapshot = _sign_conformance_snapshot(private, _conformance_snapshot(8, "a" * 64))
    state_dir = tmp_path / "state"
    now = audit_registry._parse_iso8601("2026-07-13T01:00:00Z")
    assert now is not None
    if case["state"] == "unavailable":
        state_dir.write_text("not a directory", encoding="utf-8")
    if case["state"] in {"malformed", "deleted"}:
        unavailable, warnings = audit_registry.check_snapshots(
            (registry,), state_dir, fetch_snapshot=lambda _url: snapshot, now=now
        )
        assert unavailable == set()
        assert warnings == []
        state_files = list(state_dir.glob("snapshot-*.json"))
        assert len(state_files) == 1
        if case["state"] == "malformed":
            state_files[0].write_text("{broken", encoding="utf-8")
        else:
            state_files[0].unlink()
    unavailable, _ = audit_registry.check_snapshots(
        (registry,), state_dir, fetch_snapshot=lambda _url: snapshot, now=now
    )
    assert (registry_url not in unavailable) is case["accepted"]
