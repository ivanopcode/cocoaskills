from __future__ import annotations

import ast
import base64
import hashlib
import io
import json
import os
import re
import shutil
import stat
import subprocess
import sys
import threading
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
    gc,
    git_ops,
    global_install,
    hashing,
    identifiers,
    install_marker,
    installer,
    locking,
    manifest,
    protocol_json,
    skillspec,
    status as status_mod,
    transactions,
    whitelist,
)
from csk.audit import pipeline as audit_pipeline
from csk.builds import metadata
from csk.builds import go_v1
from csk.config import RegistryConfig
from csk.source_identity import SourceIdentityError, parse_source_identity
import protocol_lifecycle_observations as lifecycle_observations
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
    _project_identity_label,
    _record_process_paths,
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


_LIFECYCLE_LITERAL_FIELD_CLASSIFICATION = {
    (
        "provider-first-and-lexical-command-order",
        "ordering",
    ): "semantic label backed by the closure order and UTF-8 command trace",
    (
        "successful-project-survives-other-project-rollback",
        "failing_project",
    ): "selected failing transaction fixture backed by its rollback trace",
    ("project-upgrade", "scope"): "exact project-upgrade CLI branch input",
    ("global-upgrade", "scope"): "exact global-upgrade CLI branch input",
    (
        "compiled-cache-miss-is-read-only",
        "scope",
    ): "multi-project dry-run fixture input",
    ("selected-project-closure", "scope"): "exact project-upgrade CLI branch input",
    (
        "selected-project-closure",
        "selection",
    ): "single selected-project CLI argument",
    ("all-projects-deduplicate", "scope"): "exact project-upgrade CLI branch input",
    (
        "all-projects-deduplicate",
        "selection",
    ): "all-projects CLI argument",
    ("global-closure", "scope"): "exact global-upgrade CLI branch input",
    ("global-closure", "selection"): "global-upgrade CLI argument",
    ("missing-config-if-missing", "force"): "exact bootstrap CLI flag absence",
    (
        "missing-config-if-missing",
        "if_missing",
    ): "exact bootstrap --if-missing CLI flag",
    ("existing-config-if-missing", "force"): "exact bootstrap CLI flag absence",
    (
        "existing-config-if-missing",
        "if_missing",
    ): "exact bootstrap --if-missing CLI flag",
    ("if-missing-with-force", "config"): "usage-error fixture accepts either config state",
    ("if-missing-with-force", "force"): "exact bootstrap --force CLI flag",
    (
        "if-missing-with-force",
        "if_missing",
    ): "exact bootstrap --if-missing CLI flag",
}


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


def test_rc6_lifecycle_literal_answers_are_explicitly_classified() -> None:
    source = Path(__file__).with_name("protocol_lifecycle_observations.py")
    tree = ast.parse(source.read_text(encoding="utf-8"))
    classified: set[tuple[str, str]] = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.Assign) or len(node.targets) != 1:
            continue
        target = node.targets[0]
        if not (
            isinstance(target, ast.Subscript)
            and isinstance(target.value, ast.Name)
            and target.value.id == "observed"
            and isinstance(target.slice, ast.Constant)
            and isinstance(target.slice.value, str)
            and isinstance(node.value, ast.Dict)
        ):
            continue
        case_name = target.slice.value
        for raw_field, expression in zip(node.value.keys, node.value.values):
            field = ast.literal_eval(raw_field)
            try:
                literal = ast.literal_eval(expression)
            except (TypeError, ValueError):
                continue
            if field == "name":
                assert literal == case_name
                continue
            key = (case_name, field)
            assert key in _LIFECYCLE_LITERAL_FIELD_CLASSIFICATION, (
                f"unclassified literal lifecycle answer {case_name}.{field}"
            )
            classified.add(key)
    assert classified == set(_LIFECYCLE_LITERAL_FIELD_CLASSIFICATION)


def test_rc6_lifecycle_observer_rejects_known_lossy_proxy_forms() -> None:
    source = Path(__file__).with_name("protocol_lifecycle_observations.py").read_text(
        encoding="utf-8"
    )
    forbidden = {
        "command[0]": "argv-element-zero-only process observation",
        ".issubset(": "directory-name-set mutation proxy",
        'Path(raw["project_identity"]).name': "journal-owner basename proxy",
        "Path(lock.identity).name": "project-lock basename proxy",
        'path.name.removeprefix("artifact-")': "private-artifact basename proxy",
    }
    for pattern, description in forbidden.items():
        assert pattern not in source, description


def test_rc6_process_path_observer_checks_every_argv_element(
    tmp_path: Path,
) -> None:
    protected = tmp_path / "protected"
    later = protected / "entry" / "bin" / "tool"
    exact = tmp_path / "private" / "artifact"
    observed: list[Path] = []

    _record_process_paths(
        ["/bin/sh", os.fspath(later), os.fspath(exact), "./entry/bin/tool"],
        observed,
        roots=(protected,),
        exact_paths={exact},
        cwd=protected,
    )

    assert observed == [
        later.resolve(strict=False),
        exact.resolve(strict=False),
        later.resolve(strict=False),
    ]


def test_rc6_project_identity_label_rejects_same_basename(
    tmp_path: Path,
) -> None:
    expected = tmp_path / "canonical-owner" / "global"
    wrong = tmp_path / "wrong-owner" / "global"

    assert (
        _project_identity_label(
            locking.canonical_project_identity(expected),
            expected,
            "global",
        )
        == "global"
    )
    assert (
        _project_identity_label(
            locking.canonical_project_identity(wrong),
            expected,
            "global",
        )
        == "unexpected"
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


def _transient_descriptor_rewrite(path: Path, marker: bytes) -> None:
    """Mutate persistent bytes through dir-fd I/O, then restore them exactly."""

    original = path.read_bytes()
    parent_fd = os.open(
        path.parent,
        os.O_RDONLY | getattr(os, "O_DIRECTORY", 0),
    )
    try:
        descriptor = os.open(
            path.name,
            os.O_WRONLY | os.O_APPEND,
            dir_fd=parent_fd,
        )
        try:
            os.write(descriptor, marker)
            os.fsync(descriptor)
            os.ftruncate(descriptor, len(original))
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
    finally:
        os.close(parent_fd)
    assert path.read_bytes() == original


def _transient_file_object_rewrite(
    path: Path,
    marker: bytes,
    *,
    captured_fchmod: Any,
    captured_utime: Any,
) -> None:
    """Restore bytes, mode and timestamps after an unpatched file-object write."""

    original = path.read_bytes()
    original_state = path.stat()
    with io.open(path, "r+b") as stream:
        descriptor = stream.fileno()
        captured_fchmod(
            descriptor,
            stat.S_IMODE(original_state.st_mode) | stat.S_IWUSR,
        )
        stream.seek(0, os.SEEK_END)
        stream.write(marker)
        stream.flush()
        os.fsync(descriptor)
        stream.seek(0)
        stream.write(original)
        stream.truncate(len(original))
        stream.flush()
        os.fsync(descriptor)
        captured_fchmod(descriptor, stat.S_IMODE(original_state.st_mode))
    captured_utime(
        path,
        ns=(original_state.st_atime_ns, original_state.st_mtime_ns),
        follow_symlinks=False,
    )
    restored = path.stat()
    assert path.read_bytes() == original
    assert stat.S_IMODE(restored.st_mode) == stat.S_IMODE(original_state.st_mode)
    assert restored.st_mtime_ns == original_state.st_mtime_ns


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


def test_rc6_private_build_binding_detects_artifact_execution(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    executions = 0
    original = installer._build_private_misses

    def execute_staged_artifacts(*args: Any, **kwargs: Any) -> Any:
        nonlocal executions
        publications = original(*args, **kwargs)
        plans = args[3]
        if {plan.command for plan in plans} != {"golden-tool", "second-tool"}:
            return publications
        for publication in publications.values():
            artifact = publication.artifact_source
            if os.name == "nt":
                original_bytes = artifact.read_bytes()
                original_mode = artifact.stat().st_mode
                artifact.write_text("raise SystemExit(0)\n", encoding="utf-8")
                try:
                    completed = subprocess.run(
                        [sys.executable, os.fspath(artifact)],
                        check=False,
                        capture_output=True,
                    )
                finally:
                    artifact.write_bytes(original_bytes)
                    artifact.chmod(original_mode)
            else:
                completed = subprocess.run(
                    [os.fspath(artifact)],
                    check=False,
                    capture_output=True,
                )
            assert completed.returncode == 0
            executions += 1
        return publications

    clear_manager_lifecycle_observation_cache()
    monkeypatch.setattr(
        installer,
        "_build_private_misses",
        execute_staged_artifacts,
    )
    try:
        _assert_sabotaged_lifecycle_case_differs(
            "private_build_cases",
            "all-misses-stage-and-verify-before-home-lock",
        )
        assert executions == 2
    finally:
        clear_manager_lifecycle_observation_cache()


def test_rc6_gc_binding_detects_guardless_collection(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    scanned_without_lock = 0
    original_collect_runtime = gc.collect_runtime
    original_collect_locked = gc._collect_locked

    class GuardlessWitness:
        def __init__(self, csk_home: Path):
            self.home_identity = transactions.canonical_manager_home_identity(
                csk_home
            )

        @staticmethod
        def assert_held() -> None:
            return None

    def collect_without_manager_lock(
        config_value: config.GlobalConfig,
        csk_home: Path,
        *,
        guard: Any | None = None,
        now: float | None = None,
        build_grace_seconds: float = gc.BUILD_GRACE_SECONDS,
    ) -> gc.GcStats:
        nonlocal scanned_without_lock
        if guard is not None:
            return original_collect_runtime(
                config_value,
                csk_home,
                guard=guard,
                now=now,
                build_grace_seconds=build_grace_seconds,
            )
        scanned_without_lock += 1
        return original_collect_locked(
            config_value,
            csk_home,
            guard=GuardlessWitness(csk_home),
            now=now,
            build_grace_seconds=build_grace_seconds,
        )

    clear_manager_lifecycle_observation_cache()
    monkeypatch.setattr(gc, "collect_runtime", collect_without_manager_lock)
    try:
        _assert_sabotaged_lifecycle_case_differs(
            "gc_cases",
            "locked-mark-and-sweep-compiled-cache",
        )
        assert scanned_without_lock == 5
    finally:
        clear_manager_lifecycle_observation_cache()


@pytest.mark.skipif(
    os.name != "posix",
    reason="the later-argv protected-artifact execution probe uses /bin/sh",
)
def test_rc6_gc_binding_detects_artifact_execution_in_later_argv_element(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    executions = 0
    original_collect_runtime = gc.collect_runtime

    def execute_protected_artifact(
        config_value: config.GlobalConfig,
        csk_home: Path,
        *,
        guard: Any | None = None,
        now: float | None = None,
        build_grace_seconds: float = gc.BUILD_GRACE_SECONDS,
    ) -> gc.GcStats:
        nonlocal executions
        artifacts = sorted(
            path
            for path in (csk_home / "builds" / "go-v1").glob("*/bin/*")
            if path.is_file()
        )
        for artifact in artifacts:
            completed = subprocess.run(
                ["/bin/sh", os.fspath(artifact)],
                check=False,
                capture_output=True,
            )
            assert completed.returncode == 0
            executions += 1
        return original_collect_runtime(
            config_value,
            csk_home,
            guard=guard,
            now=now,
            build_grace_seconds=build_grace_seconds,
        )

    clear_manager_lifecycle_observation_cache()
    monkeypatch.setattr(gc, "collect_runtime", execute_protected_artifact)
    try:
        _assert_sabotaged_lifecycle_case_differs(
            "gc_cases",
            "locked-mark-and-sweep-compiled-cache",
        )
        assert executions > 0
    finally:
        clear_manager_lifecycle_observation_cache()


def test_rc6_gc_binding_detects_in_place_permission_repair(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repairs = 0
    original_collect_runtime = gc.collect_runtime

    def repair_then_restore_rejected_entry(
        config_value: config.GlobalConfig,
        csk_home: Path,
        *,
        guard: Any | None = None,
        now: float | None = None,
        build_grace_seconds: float = gc.BUILD_GRACE_SECONDS,
    ) -> gc.GcStats:
        nonlocal repairs
        rejected = csk_home / "builds" / "go-v1" / ("f" * 64)
        if rejected.is_dir():
            original_mode = stat.S_IMODE(rejected.lstat().st_mode)
            rejected.chmod(original_mode | stat.S_IWUSR)
            rejected.chmod(original_mode)
            repairs += 2
        return original_collect_runtime(
            config_value,
            csk_home,
            guard=guard,
            now=now,
            build_grace_seconds=build_grace_seconds,
        )

    clear_manager_lifecycle_observation_cache()
    monkeypatch.setattr(
        gc,
        "collect_runtime",
        repair_then_restore_rejected_entry,
    )
    try:
        _assert_sabotaged_lifecycle_case_differs(
            "gc_cases",
            "locked-mark-and-sweep-compiled-cache",
        )
        assert repairs >= 2
    finally:
        clear_manager_lifecycle_observation_cache()


def test_rc6_recovery_binding_detects_first_journal_only_scan(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    inventories: list[tuple[str, ...]] = []

    def recover_first_journal_only(
        self: transactions.TransactionEngine,
        lock: Any,
    ) -> None:
        transaction_ids = tuple(self._journal_ids())
        if transaction_ids:
            inventories.append(transaction_ids)
            self.commit(lock, transaction_ids[0])

    clear_manager_lifecycle_observation_cache()
    monkeypatch.setattr(
        transactions.TransactionEngine,
        "recover",
        recover_first_journal_only,
    )
    try:
        _assert_sabotaged_lifecycle_case_differs(
            "recovery_cases",
            "interrupted-global-journal-recovered-by-transaction-id",
        )
        assert any(len(inventory) >= 2 for inventory in inventories)
    finally:
        clear_manager_lifecycle_observation_cache()


def test_rc6_recovery_binding_detects_same_basename_wrong_owner(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    rewrites = 0
    original_save_journal = transactions.TransactionEngine._save_journal

    def save_with_wrong_primary_owner(
        engine: transactions.TransactionEngine,
        journal: transactions.Journal,
        *,
        create: bool = False,
    ) -> None:
        nonlocal rewrites
        original_save_journal(engine, journal, create=create)
        if journal.transaction_id != "transaction-global-17":
            return
        if "wrong-owner" in Path(journal.project_identity).parts:
            return
        path = engine.journal_root / f"{journal.transaction_id}.json"
        raw = json.loads(path.read_text(encoding="utf-8"))
        canonical = Path(raw["project_identity"])
        raw["project_identity"] = os.fspath(
            canonical.parent / "wrong-owner" / canonical.name
        )
        path.write_bytes(
            json.dumps(
                raw,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
            + b"\n"
        )
        rewrites += 1

    clear_manager_lifecycle_observation_cache()
    monkeypatch.setattr(
        transactions.TransactionEngine,
        "_save_journal",
        save_with_wrong_primary_owner,
    )
    try:
        _assert_sabotaged_lifecycle_case_differs(
            "recovery_cases",
            "interrupted-global-journal-recovered-by-transaction-id",
        )
        assert rewrites > 0
    finally:
        clear_manager_lifecycle_observation_cache()


@pytest.mark.skipif(
    os.name != "posix",
    reason="the transient live-entry sabotage targets the POSIX rename seam",
)
def test_rc6_publication_binding_detects_transient_partial_live_entry(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from csk.builds import cache_posix

    exposures = 0
    original = cache_posix._rename_noreplace

    def expose_partial_then_rename(
        source_dir_fd: int,
        source_name: str,
        destination_dir_fd: int,
        destination_name: str,
    ) -> None:
        nonlocal exposures
        if re.fullmatch(r"[0-9a-f]{64}", destination_name):
            os.mkdir(destination_name, mode=0o700, dir_fd=destination_dir_fd)
            entry_fd = os.open(
                destination_name,
                os.O_RDONLY | os.O_DIRECTORY,
                dir_fd=destination_dir_fd,
            )
            try:
                partial_fd = os.open(
                    "partial",
                    os.O_WRONLY | os.O_CREAT | os.O_EXCL,
                    0o600,
                    dir_fd=entry_fd,
                )
                os.close(partial_fd)
                exposures += 1
                os.unlink("partial", dir_fd=entry_fd)
            finally:
                os.close(entry_fd)
            os.rmdir(destination_name, dir_fd=destination_dir_fd)
        original(
            source_dir_fd,
            source_name,
            destination_dir_fd,
            destination_name,
        )

    clear_manager_lifecycle_observation_cache()
    monkeypatch.setattr(
        cache_posix,
        "_rename_noreplace",
        expose_partial_then_rename,
    )
    try:
        _assert_sabotaged_lifecycle_case_differs(
            "cache_publication_cases",
            "publish-complete-immutable-entry-under-home-lock",
        )
        assert exposures > 0
    finally:
        clear_manager_lifecycle_observation_cache()


def test_rc6_publication_binding_detects_alternate_rename_to_live_entry(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    exposures = 0

    if os.name == "posix":
        from csk.builds import cache_posix

        original = cache_posix._rename_noreplace

        def expose_via_rename_then_publish(
            source_dir_fd: int,
            source_name: str,
            destination_dir_fd: int,
            destination_name: str,
        ) -> None:
            nonlocal exposures
            if re.fullmatch(r"[0-9a-f]{64}", destination_name):
                partial_name = f"partial-{destination_name}"
                os.mkdir(partial_name, mode=0o700, dir_fd=source_dir_fd)
                partial_fd = os.open(
                    partial_name,
                    os.O_RDONLY | os.O_DIRECTORY,
                    dir_fd=source_dir_fd,
                )
                try:
                    witness_fd = os.open(
                        "partial",
                        os.O_WRONLY | os.O_CREAT | os.O_EXCL,
                        0o600,
                        dir_fd=partial_fd,
                    )
                    os.close(witness_fd)
                finally:
                    os.close(partial_fd)
                os.rename(
                    partial_name,
                    destination_name,
                    src_dir_fd=source_dir_fd,
                    dst_dir_fd=destination_dir_fd,
                )
                exposures += 1
                exposed_fd = os.open(
                    destination_name,
                    os.O_RDONLY | os.O_DIRECTORY,
                    dir_fd=destination_dir_fd,
                )
                try:
                    os.unlink("partial", dir_fd=exposed_fd)
                finally:
                    os.close(exposed_fd)
                os.rmdir(destination_name, dir_fd=destination_dir_fd)
            original(
                source_dir_fd,
                source_name,
                destination_dir_fd,
                destination_name,
            )

        monkeypatch.setattr(
            cache_posix,
            "_rename_noreplace",
            expose_via_rename_then_publish,
        )
    else:
        from csk.builds import cache_windows

        original_move = cache_windows._move_no_replace

        def expose_via_rename_then_move(
            source: Path,
            destination: Path,
        ) -> None:
            nonlocal exposures
            partial = source.with_name(f"partial-{source.name}")
            partial.mkdir()
            (partial / "partial").write_bytes(b"partial")
            os.rename(partial, destination)
            exposures += 1
            shutil.rmtree(destination)
            original_move(source, destination)

        monkeypatch.setattr(
            cache_windows,
            "_move_no_replace",
            expose_via_rename_then_move,
        )

    clear_manager_lifecycle_observation_cache()
    try:
        _assert_sabotaged_lifecycle_case_differs(
            "cache_publication_cases",
            "publish-complete-immutable-entry-under-home-lock",
        )
        assert exposures > 0
    finally:
        clear_manager_lifecycle_observation_cache()


def test_rc6_cross_project_binding_detects_globally_serialized_private_builds(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = 0
    active = 0
    maximum = 0
    serial = threading.Lock()
    original = installer._build_private_misses

    def globally_serialized_private_builds(*args: Any, **kwargs: Any) -> Any:
        nonlocal active, calls, maximum
        with serial:
            calls += 1
            active += 1
            maximum = max(maximum, active)
            try:
                return original(*args, **kwargs)
            finally:
                active -= 1

    clear_manager_lifecycle_observation_cache()
    monkeypatch.setattr(
        installer,
        "_build_private_misses",
        globally_serialized_private_builds,
    )
    try:
        _assert_sabotaged_lifecycle_case_differs(
            "cross_project_cases",
            "two-project-success-preserves-both-consumers",
        )
        assert calls > 0
        assert maximum == 1
    finally:
        clear_manager_lifecycle_observation_cache()


def test_rc6_cross_project_binding_requires_successful_publish_and_commit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    failures = 0
    original = installer._publish_planned_builds

    def fail_real_cross_project_handoff(*args: Any, **kwargs: Any) -> Any:
        nonlocal failures
        manager_home = Path(args[0])
        if "cross-project/private-overlap/home" in manager_home.as_posix():
            failures += 1
            raise installer.InstallError("observed cross-project handoff failure")
        return original(*args, **kwargs)

    clear_manager_lifecycle_observation_cache()
    monkeypatch.setattr(
        installer,
        "_publish_planned_builds",
        fail_real_cross_project_handoff,
    )
    try:
        _assert_sabotaged_lifecycle_case_differs(
            "cross_project_cases",
            "two-project-success-preserves-both-consumers",
        )
        assert failures > 0
    finally:
        clear_manager_lifecycle_observation_cache()


def test_rc6_identity_binding_detects_operation_side_key_and_receipt_changes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    key_changes = 0
    receipt_changes = 0
    original_plan_builds = installer.build_planner.plan_builds
    original_publish = installer._publish_planned_builds
    original_publication = lifecycle_observations._publication

    def publication_with_changed_operation_identity(
        root: Path,
        build_input: metadata.GoBuildInput,
        payload: bytes,
        *,
        suffix: str,
    ) -> Any:
        scoped_roots = (
            "cache/publish",
            "cross-project/rollback/cache",
            "transactions/commit-order/cache",
        )
        if any(fragment in root.as_posix() for fragment in scoped_roots):
            build_input = replace(
                build_input,
                build_source=replace(
                    build_input.build_source,
                    content_sha256="sha256:" + "e" * 64,
                ),
            )
        return original_publication(
            root,
            build_input,
            payload,
            suffix=suffix,
        )

    def plan_with_changed_operation_identity(*args: Any, **kwargs: Any) -> Any:
        nonlocal key_changes
        plans = original_plan_builds(*args, **kwargs)
        manager_home = Path(kwargs["manager_home"])
        scoped_homes = (
            "cross-project/private-overlap/home",
            "dry-run/compiled/home",
            "gc/mark-sweep/home",
            "status-repair/current/home",
            "status-repair/matrix/",
            "status-repair/repair/",
        )
        if (
            not plans
            or not any(
                fragment in manager_home.as_posix()
                for fragment in scoped_homes
            )
        ):
            return plans
        selected = next(
            (
                index
                for index, plan in enumerate(plans)
                if plan.command == "golden-tool"
            ),
            None,
        )
        if selected is None:
            return plans
        changed_input = replace(
            plans[selected].input,
            build_source=replace(
                plans[selected].input.build_source,
                content_sha256="sha256:" + "e" * 64,
            ),
        )
        key_changes += 1
        changed_plans = list(plans)
        changed_plans[selected] = replace(
            plans[selected],
            input=changed_input,
            cache_key=metadata.cache_key(changed_input),
        )
        return tuple(changed_plans)

    def publish_with_changed_receipt(*args: Any, **kwargs: Any) -> Any:
        nonlocal receipt_changes
        published = original_publish(*args, **kwargs)
        manager_home = Path(args[0])
        if "private-builds/success/home" not in manager_home.as_posix():
            return published
        changed: dict[str, dict[str, Any]] = {}
        for provider, builds in published.items():
            changed[provider] = dict(builds)
            if "golden-tool" not in builds:
                continue
            selected = builds["golden-tool"]
            changed[provider]["golden-tool"] = replace(
                selected,
                marker=replace(
                    selected.marker,
                    receipt_sha256="sha256:" + "e" * 64,
                ),
            )
            receipt_changes += 1
        return changed

    clear_manager_lifecycle_observation_cache()
    monkeypatch.setattr(
        installer.build_planner,
        "plan_builds",
        plan_with_changed_operation_identity,
    )
    monkeypatch.setattr(
        installer,
        "_publish_planned_builds",
        publish_with_changed_receipt,
    )
    monkeypatch.setattr(
        lifecycle_observations,
        "_publication",
        publication_with_changed_operation_identity,
    )
    try:
        _assert_sabotaged_lifecycle_case_differs(
            "cache_publication_cases",
            "publish-complete-immutable-entry-under-home-lock",
        )
        _assert_sabotaged_lifecycle_case_differs(
            "cross_project_cases",
            "two-project-success-preserves-both-consumers",
        )
        _assert_sabotaged_lifecycle_case_differs(
            "dry_run_cases",
            "compiled-cache-miss-is-read-only",
        )
        _assert_sabotaged_lifecycle_case_differs(
            "gc_cases",
            "locked-mark-and-sweep-compiled-cache",
        )
        _assert_sabotaged_lifecycle_case_differs(
            "private_build_cases",
            "all-misses-stage-and-verify-before-home-lock",
        )
        _assert_sabotaged_lifecycle_case_differs(
            "status_cases",
            "compiled-installation-current",
        )
        _assert_sabotaged_lifecycle_case_differs(
            "repair_cases",
            "repair-rebuilds-invalid-compiled-entry",
        )
        _assert_sabotaged_lifecycle_case_differs(
            "cross_project_cases",
            "successful-project-survives-other-project-rollback",
        )
        _assert_sabotaged_lifecycle_case_differs(
            "transaction_cases",
            "deterministic-target-order-and-consumer-last",
        )
        assert key_changes > 0
        assert receipt_changes > 0
    finally:
        clear_manager_lifecycle_observation_cache()


def test_rc6_gc_binding_detects_ignored_registered_consumers(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = 0
    original = gc._load_consumers_strict

    def ignore_after_initial_gc(home: Path) -> list[Path]:
        nonlocal calls
        calls += 1
        return original(home) if calls == 1 else []

    clear_manager_lifecycle_observation_cache()
    monkeypatch.setattr(gc, "_load_consumers_strict", ignore_after_initial_gc)
    try:
        _assert_sabotaged_lifecycle_case_differs(
            "gc_cases",
            "locked-mark-and-sweep-compiled-cache",
        )
        assert calls > 1
    finally:
        clear_manager_lifecycle_observation_cache()


def test_rc6_recovery_binding_detects_missing_target_backup(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    removed: list[Path] = []
    original = transactions.TransactionEngine.commit

    def drop_restored_backup(
        engine: transactions.TransactionEngine,
        lock: Any,
        transaction_id: str,
    ) -> None:
        try:
            original(engine, lock, transaction_id)
        except BaseException:
            if transaction_id == "transaction-global-17":
                journal_path = engine.journal_root / f"{transaction_id}.json"
                raw = json.loads(journal_path.read_text(encoding="utf-8"))
                target = next(
                    item
                    for item in raw["targets"]
                    if item["identifier"] == "machine"
                )
                backup = Path(target["backup_path"])
                if backup.is_dir() and not backup.is_symlink():
                    shutil.rmtree(backup)
                elif backup.exists() or backup.is_symlink():
                    backup.unlink()
                removed.append(backup)
            raise

    clear_manager_lifecycle_observation_cache()
    monkeypatch.setattr(
        transactions.TransactionEngine,
        "commit",
        drop_restored_backup,
    )
    try:
        _assert_sabotaged_lifecycle_case_differs(
            "recovery_cases",
            "interrupted-global-journal-recovered-by-transaction-id",
        )
        assert len(removed) == 1
    finally:
        clear_manager_lifecycle_observation_cache()


@pytest.mark.skipif(
    os.name != "posix",
    reason="the untrusted protected fixture is an executable POSIX artifact",
)
def test_rc6_repair_binding_detects_untrusted_artifact_execution(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    executed: list[tuple[Path, int]] = []
    original = installer._build_private_misses

    def execute_untrusted_before_rebuild(*args: Any, **kwargs: Any) -> Any:
        cfg = args[0]
        home = cfg.path.parent
        if "status-repair/repair/04-untrusted-boundary" in home.as_posix():
            artifacts = sorted((home / "builds" / "go-v1").glob("*/bin/*"))
            for artifact in artifacts:
                completed = subprocess.run(
                    [artifact],
                    check=False,
                    capture_output=True,
                )
                executed.append((artifact, completed.returncode))
        return original(*args, **kwargs)

    clear_manager_lifecycle_observation_cache()
    monkeypatch.setattr(
        installer,
        "_build_private_misses",
        execute_untrusted_before_rebuild,
    )
    try:
        _assert_sabotaged_lifecycle_case_differs(
            "repair_cases",
            "repair-rebuilds-invalid-compiled-entry",
        )
        assert executed
        assert all(returncode == 0 for _path, returncode in executed)
    finally:
        clear_manager_lifecycle_observation_cache()


@pytest.mark.skipif(
    os.name != "posix",
    reason="the relative protected-artifact execution probe uses a POSIX script",
)
def test_rc6_repair_binding_detects_cwd_relative_artifact_execution(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    executions = 0
    original = installer._build_private_misses

    def execute_relative_untrusted_candidate(*args: Any, **kwargs: Any) -> Any:
        nonlocal executions
        config_value = args[0]
        home = config_value.path.parent
        if "status-repair/repair/04-untrusted-boundary" in home.as_posix():
            entries = sorted((home / "builds" / "go-v1").glob("*"))
            for entry in entries:
                artifact = entry / "bin" / "golden-tool"
                if not artifact.is_file():
                    continue
                completed = subprocess.run(
                    ["./bin/golden-tool"],
                    cwd=entry,
                    check=False,
                    capture_output=True,
                )
                assert completed.returncode == 0
                executions += 1
        return original(*args, **kwargs)

    clear_manager_lifecycle_observation_cache()
    monkeypatch.setattr(
        installer,
        "_build_private_misses",
        execute_relative_untrusted_candidate,
    )
    try:
        _assert_sabotaged_lifecycle_case_differs(
            "repair_cases",
            "repair-rebuilds-invalid-compiled-entry",
        )
        assert executions > 0
    finally:
        clear_manager_lifecycle_observation_cache()


def test_rc6_read_only_bindings_detect_low_level_transient_mutations(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    currentness_mutations = 0
    dry_run_mutations = 0
    original_collect = status_mod._collect_resolved_scope
    original_plan = installer.build_planner.plan_builds

    def write_then_remove(parent: Path, name: str) -> None:
        parent.mkdir(parents=True, exist_ok=True)
        parent_state = parent.stat()
        parent_fd = os.open(parent, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
        try:
            descriptor = os.open(
                name,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL,
                0o600,
                dir_fd=parent_fd,
            )
            try:
                os.write(descriptor, b"transient persistent mutation\n")
                os.fsync(descriptor)
            finally:
                os.close(descriptor)
            os.unlink(name, dir_fd=parent_fd)
        finally:
            os.close(parent_fd)
        os.utime(
            parent,
            ns=(parent_state.st_atime_ns, parent_state.st_mtime_ns),
            follow_symlinks=False,
        )

    def mutate_currentness_then_collect(
        config_value: config.GlobalConfig,
        *args: Any,
        **kwargs: Any,
    ) -> Any:
        nonlocal currentness_mutations
        home = config_value.path.parent
        if (
            "status-repair/current/home" in home.as_posix()
            or "status-repair/matrix/" in home.as_posix()
        ):
            write_then_remove(home / "builds", "transient-currentness-write")
            currentness_mutations += 1
        return original_collect(config_value, *args, **kwargs)

    def mutate_dry_run_after_plan(*args: Any, **kwargs: Any) -> Any:
        nonlocal dry_run_mutations
        plans = original_plan(*args, **kwargs)
        home = Path(kwargs["manager_home"])
        if "dry-run/compiled/home" in home.as_posix():
            write_then_remove(home / "builds", "transient-dry-run-write")
            dry_run_mutations += 1
        return plans

    clear_manager_lifecycle_observation_cache()
    monkeypatch.setattr(
        status_mod,
        "_collect_resolved_scope",
        mutate_currentness_then_collect,
    )
    monkeypatch.setattr(
        installer.build_planner,
        "plan_builds",
        mutate_dry_run_after_plan,
    )
    try:
        _assert_sabotaged_lifecycle_case_differs(
            "status_cases",
            "compiled-installation-current",
        )
        _assert_sabotaged_lifecycle_case_differs(
            "status_cases",
            "compiled-currentness-failure-matrix",
        )
        _assert_sabotaged_lifecycle_case_differs(
            "dry_run_cases",
            "compiled-cache-miss-is-read-only",
        )
        assert currentness_mutations >= 15
        assert dry_run_mutations > 0
    finally:
        clear_manager_lifecycle_observation_cache()


def test_rc6_currentness_binding_detects_transient_permission_mutations(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repairs: list[Path] = []
    original = status_mod._collect_resolved_scope

    def transient_permission_repair(
        config_value: config.GlobalConfig,
        *args: Any,
        **kwargs: Any,
    ) -> Any:
        home = config_value.path.parent
        home_text = home.as_posix()
        if (
            "status-repair/current/home" in home_text
            or "status-repair/matrix/" in home_text
        ):
            for entry in sorted((home / "builds" / "go-v1").glob("*")):
                mode = stat.S_IMODE(entry.lstat().st_mode)
                entry.chmod(mode | stat.S_IWUSR)
                entry.chmod(mode)
                repairs.append(entry)
        return original(config_value, *args, **kwargs)

    clear_manager_lifecycle_observation_cache()
    monkeypatch.setattr(
        status_mod,
        "_collect_resolved_scope",
        transient_permission_repair,
    )
    try:
        _assert_sabotaged_lifecycle_case_differs(
            "status_cases",
            "compiled-installation-current",
        )
        _assert_sabotaged_lifecycle_case_differs(
            "status_cases",
            "compiled-currentness-failure-matrix",
        )
        assert len(repairs) >= 15
    finally:
        clear_manager_lifecycle_observation_cache()


def test_rc6_transaction_binding_detects_post_restore_corruption(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    corruptions: list[str] = []
    original = transactions.TransactionEngine._rollback_target

    def corrupt_after_restore(
        engine: transactions.TransactionEngine,
        journal: transactions.Journal,
        target: transactions.JournalTarget,
    ) -> None:
        original(engine, journal, target)
        if journal.transaction_id == "txn-observed-rollback":
            live = Path(target.live_path)
            live.write_text(
                f"corrupted-after-restore:{target.identifier}",
                encoding="utf-8",
            )
            corruptions.append(target.identifier)

    clear_manager_lifecycle_observation_cache()
    monkeypatch.setattr(
        transactions.TransactionEngine,
        "_rollback_target",
        corrupt_after_restore,
    )
    try:
        _assert_sabotaged_lifecycle_case_differs(
            "transaction_cases",
            "reverse-rollback-under-home-lock",
        )
        assert len(corruptions) == 6
    finally:
        clear_manager_lifecycle_observation_cache()


@pytest.mark.skipif(
    os.name != "posix",
    reason="the post-publication child sabotage targets the POSIX rename seam",
)
def test_rc6_publication_binding_detects_transient_live_child_corruption(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from csk.builds import cache_posix

    corruptions = 0
    original = cache_posix._rename_noreplace

    def corrupt_live_child_then_restore(
        source_dir_fd: int,
        source_name: str,
        destination_dir_fd: int,
        destination_name: str,
    ) -> None:
        nonlocal corruptions
        original(
            source_dir_fd,
            source_name,
            destination_dir_fd,
            destination_name,
        )
        if not re.fullmatch(r"[0-9a-f]{64}", destination_name):
            return
        entry_fd = os.open(
            destination_name,
            os.O_RDONLY | os.O_DIRECTORY,
            dir_fd=destination_dir_fd,
        )
        try:
            bin_fd = os.open("bin", os.O_RDONLY | os.O_DIRECTORY, dir_fd=entry_fd)
            try:
                artifact_name = os.listdir(bin_fd)[0]
                read_fd = os.open(artifact_name, os.O_RDONLY, dir_fd=bin_fd)
                try:
                    state = os.fstat(read_fd)
                    original_bytes = os.read(read_fd, state.st_size)
                    original_mode = stat.S_IMODE(state.st_mode)
                    os.fchmod(read_fd, original_mode | stat.S_IWUSR)
                finally:
                    os.close(read_fd)
                write_fd = os.open(artifact_name, os.O_WRONLY, dir_fd=bin_fd)
                try:
                    os.write(write_fd, b"!" * len(original_bytes))
                    os.fsync(write_fd)
                    os.lseek(write_fd, 0, os.SEEK_SET)
                    os.write(write_fd, original_bytes)
                    os.ftruncate(write_fd, len(original_bytes))
                    os.fsync(write_fd)
                    os.fchmod(write_fd, original_mode)
                finally:
                    os.close(write_fd)
                corruptions += 1
            finally:
                os.close(bin_fd)
        finally:
            os.close(entry_fd)

    clear_manager_lifecycle_observation_cache()
    monkeypatch.setattr(
        cache_posix,
        "_rename_noreplace",
        corrupt_live_child_then_restore,
    )
    try:
        _assert_sabotaged_lifecycle_case_differs(
            "cache_publication_cases",
            "publish-complete-immutable-entry-under-home-lock",
        )
        assert corruptions > 0
    finally:
        clear_manager_lifecycle_observation_cache()


def test_rc6_upgrade_dry_run_bindings_detect_transient_persistent_writes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    mutations: list[str] = []
    original_project_install = installer.install
    original_global_install = global_install.install

    def mutate_project_config_then_install(
        config_value: config.GlobalConfig,
        *args: Any,
        **kwargs: Any,
    ) -> Any:
        if "/dry-run/project/" in config_value.path.as_posix():
            _transient_descriptor_rewrite(
                config_value.path,
                b"\ntransient-project-upgrade-dry-run-write\n",
            )
            mutations.append("project")
        return original_project_install(config_value, *args, **kwargs)

    def mutate_global_config_then_install(
        config_value: config.GlobalConfig,
        *args: Any,
        **kwargs: Any,
    ) -> Any:
        if "/dry-run/global/" in config_value.path.as_posix():
            _transient_descriptor_rewrite(
                config_value.path,
                b"\ntransient-global-upgrade-dry-run-write\n",
            )
            mutations.append("global")
        return original_global_install(config_value, *args, **kwargs)

    clear_manager_lifecycle_observation_cache()
    monkeypatch.setattr(installer, "install", mutate_project_config_then_install)
    monkeypatch.setattr(global_install, "install", mutate_global_config_then_install)
    try:
        differences = {
            name
            for name in ("project-upgrade", "global-upgrade")
            if observe_manager_lifecycle_case(
                name,
                MANAGER_LIFECYCLE_VECTORS["compiled_build_fixture"],
            )
            != _lifecycle_case("dry_run_cases", name)
        }
        assert differences == {"project-upgrade", "global-upgrade"}
        assert set(mutations) == {"project", "global"}
    finally:
        clear_manager_lifecycle_observation_cache()


def test_rc6_planning_binding_detects_transient_persistent_writes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    mutations = 0
    original = installer._selected_projects

    def mutate_skillfile_after_selection(
        config_value: config.GlobalConfig,
        alias: str | None,
    ) -> list[config.ProjectConfig]:
        nonlocal mutations
        selected = original(config_value, alias)
        if "/planning/" in config_value.path.as_posix():
            for project in selected:
                skillfile = project.path / "Skillfile.json"
                if skillfile.is_file():
                    _transient_descriptor_rewrite(
                        skillfile,
                        b"\ntransient-planning-gate-write\n",
                    )
                    mutations += 1
        return selected

    clear_manager_lifecycle_observation_cache()
    monkeypatch.setattr(
        installer,
        "_selected_projects",
        mutate_skillfile_after_selection,
    )
    try:
        _assert_sabotaged_lifecycle_case_differs(
            "planning_cases",
            "all-source-and-trust-gates-before-build",
        )
        assert mutations > 0
    finally:
        clear_manager_lifecycle_observation_cache()


def test_rc6_private_failure_binding_detects_transient_persistent_write(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    mutations = 0
    original = installer._build_private_misses

    def mutate_generation_then_build(
        config_value: config.GlobalConfig,
        *args: Any,
        **kwargs: Any,
    ) -> Any:
        nonlocal mutations
        generation = config_value.path.parent / "persistent-generation"
        if "/private-builds/failure/" in generation.as_posix():
            _transient_descriptor_rewrite(
                generation,
                b"\ntransient-private-build-failure-write\n",
            )
            mutations += 1
        return original(config_value, *args, **kwargs)

    clear_manager_lifecycle_observation_cache()
    monkeypatch.setattr(
        installer,
        "_build_private_misses",
        mutate_generation_then_build,
    )
    try:
        _assert_sabotaged_lifecycle_case_differs(
            "private_build_cases",
            "second-build-failure-preserves-persistent-state",
        )
        assert mutations > 0
    finally:
        clear_manager_lifecycle_observation_cache()


@pytest.mark.skipif(
    os.name != "posix",
    reason="captured descriptor aliases target the POSIX no-replace seam",
)
def test_rc6_publication_binding_detects_captured_descriptor_aliases(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from csk.builds import cache_posix

    captured_open = os.open
    captured_write = os.write
    captured_ftruncate = os.ftruncate
    captured_fchmod = os.fchmod
    captured_utime = os.utime
    corruptions = 0
    original = cache_posix._rename_noreplace

    def corrupt_live_child_then_restore(
        source_dir_fd: int,
        source_name: str,
        destination_dir_fd: int,
        destination_name: str,
    ) -> None:
        nonlocal corruptions
        original(
            source_dir_fd,
            source_name,
            destination_dir_fd,
            destination_name,
        )
        if not re.fullmatch(r"[0-9a-f]{64}", destination_name):
            return
        entry_fd = captured_open(
            destination_name,
            os.O_RDONLY | os.O_DIRECTORY,
            dir_fd=destination_dir_fd,
        )
        try:
            bin_fd = captured_open(
                "bin",
                os.O_RDONLY | os.O_DIRECTORY,
                dir_fd=entry_fd,
            )
            try:
                artifact_name = os.listdir(bin_fd)[0]
                read_fd = captured_open(
                    artifact_name,
                    os.O_RDONLY,
                    dir_fd=bin_fd,
                )
                try:
                    state = os.fstat(read_fd)
                    original_bytes = os.read(read_fd, state.st_size)
                    original_mode = stat.S_IMODE(state.st_mode)
                    captured_fchmod(read_fd, original_mode | stat.S_IWUSR)
                finally:
                    os.close(read_fd)
                write_fd = captured_open(
                    artifact_name,
                    os.O_WRONLY,
                    dir_fd=bin_fd,
                )
                try:
                    captured_write(write_fd, b"!" * len(original_bytes))
                    os.fsync(write_fd)
                    os.lseek(write_fd, 0, os.SEEK_SET)
                    captured_write(write_fd, original_bytes)
                    captured_ftruncate(write_fd, len(original_bytes))
                    os.fsync(write_fd)
                    captured_fchmod(write_fd, original_mode)
                finally:
                    os.close(write_fd)
                captured_utime(
                    artifact_name,
                    ns=(state.st_atime_ns, state.st_mtime_ns),
                    dir_fd=bin_fd,
                    follow_symlinks=False,
                )
                corruptions += 1
            finally:
                os.close(bin_fd)
        finally:
            os.close(entry_fd)

    clear_manager_lifecycle_observation_cache()
    monkeypatch.setattr(
        cache_posix,
        "_rename_noreplace",
        corrupt_live_child_then_restore,
    )
    try:
        _assert_sabotaged_lifecycle_case_differs(
            "cache_publication_cases",
            "publish-complete-immutable-entry-under-home-lock",
        )
        assert corruptions > 0
    finally:
        clear_manager_lifecycle_observation_cache()


@pytest.mark.skipif(
    os.name != "posix",
    reason="captured fchmod targets the POSIX no-replace seam",
)
def test_rc6_publication_binding_detects_captured_root_fchmod_restore(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from csk.builds import cache_posix

    captured_open = os.open
    captured_fchmod = os.fchmod
    mutations = 0
    original = cache_posix._rename_noreplace

    def mutate_live_root_then_restore(
        source_dir_fd: int,
        source_name: str,
        destination_dir_fd: int,
        destination_name: str,
    ) -> None:
        nonlocal mutations
        original(
            source_dir_fd,
            source_name,
            destination_dir_fd,
            destination_name,
        )
        if not re.fullmatch(r"[0-9a-f]{64}", destination_name):
            return
        descriptor = captured_open(
            destination_name,
            os.O_RDONLY | os.O_DIRECTORY,
            dir_fd=destination_dir_fd,
        )
        try:
            original_mode = stat.S_IMODE(os.fstat(descriptor).st_mode)
            captured_fchmod(descriptor, original_mode ^ stat.S_IRGRP)
            captured_fchmod(descriptor, original_mode)
            mutations += 1
        finally:
            os.close(descriptor)

    clear_manager_lifecycle_observation_cache()
    monkeypatch.setattr(
        cache_posix,
        "_rename_noreplace",
        mutate_live_root_then_restore,
    )
    try:
        _assert_sabotaged_lifecycle_case_differs(
            "cache_publication_cases",
            "publish-complete-immutable-entry-under-home-lock",
        )
        assert mutations > 0
    finally:
        clear_manager_lifecycle_observation_cache()


def test_rc6_publication_binding_detects_captured_live_name_restore(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured_rename = os.rename
    mutations = 0

    if os.name == "posix":
        from csk.builds import cache_posix

        original = cache_posix._rename_noreplace

        def move_live_name_away_then_restore(
            source_dir_fd: int,
            source_name: str,
            destination_dir_fd: int,
            destination_name: str,
        ) -> None:
            nonlocal mutations
            original(
                source_dir_fd,
                source_name,
                destination_dir_fd,
                destination_name,
            )
            if not re.fullmatch(r"[0-9a-f]{64}", destination_name):
                return
            away_name = f".review-away-{destination_name}"
            captured_rename(
                destination_name,
                away_name,
                src_dir_fd=destination_dir_fd,
                dst_dir_fd=destination_dir_fd,
            )
            captured_rename(
                away_name,
                destination_name,
                src_dir_fd=destination_dir_fd,
                dst_dir_fd=destination_dir_fd,
            )
            mutations += 1

        monkeypatch.setattr(
            cache_posix,
            "_rename_noreplace",
            move_live_name_away_then_restore,
        )
    else:
        from csk.builds import cache_windows

        original_move = cache_windows._move_no_replace

        def move_live_name_away_then_restore_windows(
            source: Path,
            destination: Path,
        ) -> None:
            nonlocal mutations
            original_move(source, destination)
            away = destination.with_name(f".review-away-{destination.name}")
            captured_rename(destination, away)
            captured_rename(away, destination)
            mutations += 1

        monkeypatch.setattr(
            cache_windows,
            "_move_no_replace",
            move_live_name_away_then_restore_windows,
        )

    clear_manager_lifecycle_observation_cache()
    try:
        _assert_sabotaged_lifecycle_case_differs(
            "cache_publication_cases",
            "publish-complete-immutable-entry-under-home-lock",
        )
        assert mutations > 0
    finally:
        clear_manager_lifecycle_observation_cache()


def test_rc6_read_only_bindings_detect_file_object_alias_mutations(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured_fchmod = os.fchmod
    captured_utime = os.utime
    mutations: list[str] = []
    original_project_install = installer.install
    original_global_install = global_install.install
    original_selected_projects = installer._selected_projects
    original_private_builds = installer._build_private_misses

    def rewrite(path: Path, marker: bytes, label: str) -> None:
        _transient_file_object_rewrite(
            path,
            marker,
            captured_fchmod=captured_fchmod,
            captured_utime=captured_utime,
        )
        mutations.append(label)

    def mutate_project_config_then_install(
        config_value: config.GlobalConfig,
        *args: Any,
        **kwargs: Any,
    ) -> Any:
        if "/dry-run/project/" in config_value.path.as_posix():
            rewrite(
                config_value.path,
                b"\nfile-object-project-upgrade-write\n",
                "project-upgrade",
            )
        return original_project_install(config_value, *args, **kwargs)

    def mutate_global_config_then_install(
        config_value: config.GlobalConfig,
        *args: Any,
        **kwargs: Any,
    ) -> Any:
        if "/dry-run/global/" in config_value.path.as_posix():
            rewrite(
                config_value.path,
                b"\nfile-object-global-upgrade-write\n",
                "global-upgrade",
            )
        return original_global_install(config_value, *args, **kwargs)

    def mutate_planning_skillfile(
        config_value: config.GlobalConfig,
        alias: str | None,
    ) -> list[config.ProjectConfig]:
        selected = original_selected_projects(config_value, alias)
        if "/planning/" in config_value.path.as_posix():
            for project in selected:
                skillfile = project.path / "Skillfile.json"
                if skillfile.is_file():
                    rewrite(
                        skillfile,
                        b"\nfile-object-planning-write\n",
                        "planning",
                    )
        return selected

    def mutate_generation_then_build(
        config_value: config.GlobalConfig,
        *args: Any,
        **kwargs: Any,
    ) -> Any:
        generation = config_value.path.parent / "persistent-generation"
        if "/private-builds/failure/" in generation.as_posix():
            rewrite(
                generation,
                b"\nfile-object-private-failure-write\n",
                "private-failure",
            )
        return original_private_builds(config_value, *args, **kwargs)

    clear_manager_lifecycle_observation_cache()
    monkeypatch.setattr(installer, "install", mutate_project_config_then_install)
    monkeypatch.setattr(global_install, "install", mutate_global_config_then_install)
    monkeypatch.setattr(installer, "_selected_projects", mutate_planning_skillfile)
    monkeypatch.setattr(installer, "_build_private_misses", mutate_generation_then_build)
    try:
        expected = {
            ("dry_run_cases", "project-upgrade"),
            ("dry_run_cases", "global-upgrade"),
            ("planning_cases", "all-source-and-trust-gates-before-build"),
            (
                "private_build_cases",
                "second-build-failure-preserves-persistent-state",
            ),
        }
        differences = {
            (cluster, name)
            for cluster, name in expected
            if observe_manager_lifecycle_case(
                name,
                MANAGER_LIFECYCLE_VECTORS["compiled_build_fixture"],
            )
            != _lifecycle_case(cluster, name)
        }
        assert differences == expected
        assert set(mutations) == {
            "project-upgrade",
            "global-upgrade",
            "planning",
            "private-failure",
        }
    finally:
        clear_manager_lifecycle_observation_cache()


def test_rc6_private_failure_binding_watches_project_skillfile(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    mutations = 0
    original = installer._build_private_misses

    def mutate_skillfile_then_build(
        config_value: config.GlobalConfig,
        *args: Any,
        **kwargs: Any,
    ) -> Any:
        nonlocal mutations
        if "/private-builds/failure/" in config_value.path.as_posix():
            project = next(iter(config_value.projects.values())).path
            _transient_descriptor_rewrite(
                project / "Skillfile.json",
                b"\ntransient-private-failure-skillfile-write\n",
            )
            mutations += 1
        return original(config_value, *args, **kwargs)

    clear_manager_lifecycle_observation_cache()
    monkeypatch.setattr(
        installer,
        "_build_private_misses",
        mutate_skillfile_then_build,
    )
    try:
        _assert_sabotaged_lifecycle_case_differs(
            "private_build_cases",
            "second-build-failure-preserves-persistent-state",
        )
        assert mutations > 0
    finally:
        clear_manager_lifecycle_observation_cache()


@pytest.mark.skipif(
    os.name != "posix",
    reason="the captured-callable regression requires POSIX ctime semantics",
)
def test_rc6_persistent_tamper_witness_survives_exact_restoration(
    tmp_path: Path,
) -> None:
    witness = tmp_path / "persistent-witness"
    witness.write_bytes(b"original persistent bytes")
    original = witness.stat()
    before = lifecycle_observations._persistent_tamper_state((witness,))

    _transient_file_object_rewrite(
        witness,
        b"\ntransient write\n",
        captured_fchmod=os.fchmod,
        captured_utime=os.utime,
    )

    restored = witness.stat()
    after = lifecycle_observations._persistent_tamper_state((witness,))
    assert witness.read_bytes() == b"original persistent bytes"
    assert stat.S_IMODE(restored.st_mode) == stat.S_IMODE(original.st_mode)
    assert restored.st_ino == original.st_ino
    assert restored.st_mtime_ns == original.st_mtime_ns
    assert restored.st_ctime_ns != original.st_ctime_ns
    assert after != before


@pytest.mark.parametrize("sabotage", ["zero-fetch", "duplicate-fetch"])
def test_rc6_all_project_upgrade_binding_requires_exact_nonempty_fetch_closure(
    monkeypatch: pytest.MonkeyPatch,
    sabotage: str,
) -> None:
    sabotaged_calls = 0
    original = closure.build_closure

    def alter_all_project_fetch(
        config_value: config.GlobalConfig,
        project_manifest: manifest.ProjectManifest,
        substitutions: dict[str, Any],
        **kwargs: Any,
    ) -> list[closure.ClosureNode]:
        nonlocal sabotaged_calls
        if (
            "/upgrade/all/" not in config_value.path.as_posix()
            or not kwargs.get("fetch_existing", False)
        ):
            return original(
                config_value,
                project_manifest,
                substitutions,
                **kwargs,
            )
        sabotaged_calls += 1
        if sabotage == "zero-fetch":
            kwargs = {**kwargs, "fetch_existing": False}
            return original(
                config_value,
                project_manifest,
                substitutions,
                **kwargs,
            )
        nodes = original(
            config_value,
            project_manifest,
            substitutions,
            **kwargs,
        )
        fetched_repos = kwargs.get("fetched_repos")
        if isinstance(fetched_repos, set) and fetched_repos:
            git_ops.fetch_repo(sorted(fetched_repos, key=os.fspath)[0])
        return nodes

    clear_manager_lifecycle_observation_cache()
    monkeypatch.setattr(closure, "build_closure", alter_all_project_fetch)
    try:
        _assert_sabotaged_lifecycle_case_differs(
            "upgrade_cases",
            "all-projects-deduplicate",
        )
        assert sabotaged_calls == 2
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
