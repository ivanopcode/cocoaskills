"""Executable draft-sources-v1 conformance harness (draft, opt-in).

This module stands up the conformance consumer for the unreleased
skillfile-sources-v1 corpus before any source feature lands, so every later
leaf adds real semantic-case drivers to a harness that already reports
honestly. It reads the suite from ``CSK_DRAFT_SOURCES_SUITE_ROOT`` (a checkout
of ``relux-works/curator-spec`` at the revision pinned in
``.github/ci/draft-sources-suite.json``, pointing at
``conformance/draft-sources-v1``) and:

* authenticates ``index.json``, ``semantic-cases.json`` and
  ``snapshot-cases.json`` against the committed pin digests, failing closed on
  a mismatch or a missing file;
* validates every indexed schema case with ``jsonschema`` Draft 2020-12 over a
  ``referencing`` registry built from ``schemas/v1`` and
  ``schemas/draft-sources-v1``, asserting each expected ``valid`` flag;
* recomputes every snapshot vector's entry hashes, path order and
  ``sha256:CCJ-1`` snapshot digest via ``csk.protocol_json.canonical_bytes``;
* enumerates every semantic case as a parametrized test through the
  ``SEMANTIC_DRIVERS`` registry: a case with no registered driver skips with
  ``not yet implemented: <TASK-ID>`` from ``CASE_OWNERS``, and a case with a
  driver runs it against a production entry point.

Without ``CSK_DRAFT_SOURCES_SUITE_ROOT`` every test in this module skips with
``CSK_DRAFT_SOURCES_SUITE_ROOT is not set``. Later leaves register drivers
with :func:`register_semantic_driver`; a driver must call a production entry
point on a fixture built from the case input and assert the exact expected
outcome, never merely reproduce the expected label.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
from collections.abc import Callable, Collection, Mapping
from copy import deepcopy
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest
from jsonschema import Draft202012Validator
from referencing import Registry, Resource

from csk import git_admission, manifest, protocol_json
from csk.build_repository_pipeline import ExternalBuildError
from csk.sources import _selection_fs
from csk.sources import errors as source_errors
from csk.sources import repository_policy
from csk.sources import transport as source_transport
from csk.sources import lock as source_lock

ROOT_TEXT = os.environ.get("CSK_DRAFT_SOURCES_SUITE_ROOT")
pytestmark = pytest.mark.skipif(not ROOT_TEXT, reason="CSK_DRAFT_SOURCES_SUITE_ROOT is not set")

PIN_PATH = Path(__file__).parents[1] / ".github" / "ci" / "draft-sources-suite.json"
EXPECTED_SUITE_FILES = ("index.json", "semantic-cases.json", "snapshot-cases.json")
DRAFT_2020_12_SCHEMA_ID = "https://json-schema.org/draft/2020-12/schema"

# The corpus at the pinned revision carries 115 schema cases. The task text
# names 102, which predates corpus growth the same way the epic goal text
# names 73 semantic cases while the pinned suite carries 94: the operative
# requirement is "every index.json schema case", so the harness asserts the
# measured inventory, not the stale count.
EXPECTED_SCHEMA_CASE_COUNT = 115
EXPECTED_SCHEMA_CASE_COUNTS = {
    "build-receipt-v3.schema.json": 4,
    "install-marker-v5.schema.json": 38,
    "local-snapshot-v1.schema.json": 3,
    "skillfile-lock-v1.schema.json": 7,
    "skillfile-v2.schema.json": 41,
    "source-audit-v1.schema.json": 4,
    "source-policy-v1.schema.json": 5,
    "source-policy-v2.schema.json": 13,
}
EXPECTED_DRAFT_SCHEMA_NAMES = frozenset(
    {*EXPECTED_SCHEMA_CASE_COUNTS, "source-types-v1.schema.json"}
)
EXPECTED_SEMANTIC_CASE_COUNT = 94
EXPECTED_SNAPSHOT_VECTOR_COUNT = 3

# Owning task per semantic case id. Ownership follows the primary gate under
# test: the leaf that must register the driver. Leaves without semantic cases
# (parse/opt-in, lock model, atomic publish, runtime materialization, CLI
# workflow, corpus closure) are covered by schema cases, unit tests and the
# drivers owned above; the coverage test below fails loudly when the suite
# adds or removes an id, so the mapping cannot drift silently.
CASE_OWNERS: dict[str, str] = {
    # TASK-260916-100uew enforce-local-source-and-output-boundaries.
    "broad-root": "TASK-260916-100uew",
    "managed-source": "TASK-260916-100uew",
    "symlink-managed": "TASK-260916-100uew",
    "case-alias": "TASK-260916-100uew",
    "write-boundary-retarget": "TASK-260916-100uew",
    "root-no-inputs": "TASK-260916-100uew",
    # TASK-260916-2wjh3m expand-deterministic-skill-collections.
    "selector-escape": "TASK-260916-2wjh3m",
    "unknown-alias": "TASK-260916-2wjh3m",
    "missing-excluded-literal": "TASK-260916-2wjh3m",
    "bad-wildcard-member": "TASK-260916-2wjh3m",
    "duplicate-name": "TASK-260916-2wjh3m",
    # TASK-260916-18j5hg resolve-source-closure-and-explicit-refresh.
    "frozen-membership": "TASK-260916-18j5hg",
    "runtime-only-refresh": "TASK-260916-18j5hg",
    "build-only-refresh": "TASK-260916-18j5hg",
    # TASK-260916-sbzutf capture-and-store-local-package-snapshots.
    "capture-mutation": "TASK-260916-sbzutf",
    "frozen-copy-mutation": "TASK-260916-sbzutf",
    "local-git-dirty": "TASK-260916-sbzutf",
    # TASK-260917-34g2lq snapshot-store leaf: moved out of sbzutf by the
    # recorded leaf split (sbzutf keeps capture and revalidation only).
    "missing-snapshot": "TASK-260917-34g2lq",
    # TASK-260916-11yseo bind-source-audit-to-existing-assurance-gates.
    "missing-audit-report": "TASK-260916-11yseo",
    "strict-network-attestation-local": "TASK-260916-11yseo",
    "attestation-evidence-absent": "TASK-260916-11yseo",
    "attestation-evidence-unreadable": "TASK-260916-11yseo",
    "attestation-evidence-malformed": "TASK-260916-11yseo",
    "attestation-evidence-stale": "TASK-260916-11yseo",
    "attestation-evidence-revoked": "TASK-260916-11yseo",
    "attestation-evidence-wrong-name": "TASK-260916-11yseo",
    "attestation-evidence-wrong-repository": "TASK-260916-11yseo",
    "attestation-evidence-wrong-commit": "TASK-260916-11yseo",
    "attestation-evidence-wrong-context": "TASK-260916-11yseo",
    "attestation-evidence-wrong-key": "TASK-260916-11yseo",
    # TASK-260916-fsw7re apply-bounded-authenticated-transport-resolution.
    "fallback-dns": "TASK-260916-fsw7re",
    "fallback-auth-rejected": "TASK-260916-fsw7re",
    "fallback-tls": "TASK-260916-fsw7re",
    "fallback-host-key": "TASK-260916-fsw7re",
    "fallback-integrity": "TASK-260916-fsw7re",
    "fallback-identity": "TASK-260916-fsw7re",
    "fallback-ref-moved": "TASK-260916-fsw7re",
    "fallback-audit": "TASK-260916-fsw7re",
    "fallback-unknown": "TASK-260916-fsw7re",
    "fallback-policy-unreadable": "TASK-260916-fsw7re",
    "fallback-http-404": "TASK-260916-fsw7re",
    "pinned-auth": "TASK-260916-fsw7re",
    # TASK-260916-1iyslr load-machine-owned-repository-endpoint-policy.
    "v2-port-endpoint": "TASK-260916-1iyslr",
    "v2-declared-mirror": "TASK-260916-1iyslr",
    "v2-mirror-first": "TASK-260916-1iyslr",
    "v2-alias-resolution": "TASK-260916-1iyslr",
    "v2-undeclared-mirror": "TASK-260916-1iyslr",
    "v2-alias-unknown": "TASK-260916-1iyslr",
    "v2-alias-mirror-undeclared": "TASK-260916-1iyslr",
    "v2-user-ssh-alias-ignored": "TASK-260916-fsw7re",
    "v2-user-insteadof-ignored": "TASK-260916-fsw7re",
    # TASK-260916-1iyslr load-machine-owned-repository-endpoint-policy.
    "endpoint-identity-mismatch": "TASK-260916-1iyslr",
    "v2-reader-accepts-v1-policy": "TASK-260916-1iyslr",
    "v2-v1-reader-rejects-v2-policy": "TASK-260916-1iyslr",
    "v2-pin-port-mismatch": "TASK-260916-1iyslr",
    "v2-embedded-alias-host": "TASK-260916-1iyslr",
    "v2-alias-auth-mismatch": "TASK-260916-1iyslr",
    "v2-alias-chain": "TASK-260916-1iyslr",
    "v2-double-port": "TASK-260916-1iyslr",
    "v2-spurious-mirror-of": "TASK-260916-1iyslr",
    "v2-mirror-of-mismatch": "TASK-260916-1iyslr",
    # TASK-260916-15nf0l migrate-install-markers-with-full-currentness.
    "attested-network-current": "TASK-260916-15nf0l",
    "legacy-substitution-current": "TASK-260916-15nf0l",
    "legacy-substitution-strict": "TASK-260916-15nf0l",
    "selector-substitution-forbidden": "TASK-260916-15nf0l",
    "local-required-registry": "TASK-260916-15nf0l",
    "external-substitution-strict": "TASK-260916-15nf0l",
    "external-only-current": "TASK-260916-15nf0l",
    "marker-plan-mismatch-registry": "TASK-260916-15nf0l",
    "marker-plan-mismatch-status": "TASK-260916-15nf0l",
    "marker-plan-mismatch-key_id": "TASK-260916-15nf0l",
    "marker-plan-mismatch-substituted": "TASK-260916-15nf0l",
    "marker-plan-mismatch-package": "TASK-260916-15nf0l",
    "marker-plan-mismatch-lock_sha256": "TASK-260916-15nf0l",
    # TASK-260916-341a6q implement-source-aware-build-receipts-and-cache.
    "external-evidence-mismatch-repository": "TASK-260916-341a6q",
    "external-evidence-mismatch-declared_identity": "TASK-260916-341a6q",
    "external-evidence-mismatch-declared_locked_commit": "TASK-260916-341a6q",
    "external-evidence-mismatch-declared_tag": "TASK-260916-341a6q",
    "external-evidence-mismatch-effective_identity": "TASK-260916-341a6q",
    "external-evidence-mismatch-object_format": "TASK-260916-341a6q",
    "external-evidence-mismatch-commit": "TASK-260916-341a6q",
    "external-evidence-mismatch-substituted": "TASK-260916-341a6q",
    "external-evidence-mismatch-substitution": "TASK-260916-341a6q",
    "external-evidence-mismatch-build_source": "TASK-260916-341a6q",
    "external-evidence-mismatch-descriptor_target": "TASK-260916-341a6q",
    "external-evidence-mismatch-execution_policy": "TASK-260916-341a6q",
    "external-evidence-mismatch-cache_key": "TASK-260916-341a6q",
    "external-evidence-mismatch-receipt_sha256": "TASK-260916-341a6q",
    "external-evidence-mismatch-artifact_sha256": "TASK-260916-341a6q",
    "external-evidence-mismatch-artifact_path": "TASK-260916-341a6q",
    "external-evidence-mismatch-input.package": "TASK-260916-341a6q",
    "v2-external-build-mirror-admitted": "TASK-260916-341a6q",
    "v2-external-build-port-refused": "TASK-260916-341a6q",
    "v2-external-build-alias-refused": "TASK-260916-341a6q",
}
assert len(CASE_OWNERS) == EXPECTED_SEMANTIC_CASE_COUNT

SemanticDriver = Callable[[dict[str, Any]], None]
SEMANTIC_DRIVERS: dict[str, SemanticDriver] = {}


def register_semantic_driver(case_id: str, driver: SemanticDriver) -> None:
    """Register the driver that executes one semantic case.

    The driver runs on every harness run once registered, so an unknown id or
    a second driver for the same id fails closed instead of silently
    shadowing the declared owner.
    """
    assert case_id in CASE_OWNERS, f"cannot register a driver for unknown case {case_id!r}"
    assert case_id not in SEMANTIC_DRIVERS, f"duplicate driver for case {case_id!r}"
    SEMANTIC_DRIVERS[case_id] = driver


def _load_pin(path: Path = PIN_PATH) -> dict[str, Any]:
    """Load the committed draft-sources suite pin, or fail closed."""
    assert path.is_file(), f"draft sources suite pin is missing: {path}"
    pin = json.loads(path.read_bytes())
    assert isinstance(pin, dict), "draft sources suite pin must be a JSON object"
    assert set(pin) == {"repository", "revision", "suite_root", "files"}, (
        f"draft sources suite pin declares {sorted(pin)}"
    )
    for field in ("repository", "revision", "suite_root"):
        value = pin[field]
        assert isinstance(value, str) and value.strip(), (
            f"draft sources suite pin field is missing or not a string: {field}"
        )
    files = pin["files"]
    assert isinstance(files, dict), "draft sources suite pin files must be an object"
    assert set(files) == set(EXPECTED_SUITE_FILES), (
        f"draft sources suite pin declares {sorted(files)}"
    )
    for name, digest in files.items():
        assert (
            isinstance(digest, str)
            and digest.startswith("sha256:")
            and len(digest) == len("sha256:") + 64
        ), f"draft sources suite pin digest is malformed for {name}: {digest!r}"
    return pin


def _suite_root() -> Path:
    assert ROOT_TEXT is not None
    root = Path(ROOT_TEXT)
    assert root.is_dir(), f"CSK_DRAFT_SOURCES_SUITE_ROOT is not a directory: {root}"
    return root


def _authenticated_suite_bytes(
    suite_root: Path, name: str, pin: Mapping[str, Any]
) -> bytes:
    """Read one suite file only after its pinned digest agrees with its bytes."""
    assert name in pin["files"], f"draft sources suite pin publishes no {name}"
    path = suite_root / name
    assert path.is_file(), f"draft sources suite publishes no file at {name}"
    raw = path.read_bytes()
    actual = "sha256:" + hashlib.sha256(raw).hexdigest()
    assert actual == pin["files"][name], (
        f"draft sources digest mismatch for {name}: pinned {pin['files'][name]}, "
        f"measured {actual}"
    )
    return raw


def _load_schemas(repository_root: Path) -> tuple[dict[str, Any], Registry]:
    """Build the Draft 2020-12 registry the schema gate validates against.

    Every ``schemas/v1`` document joins the registry so that ``../v1/...``
    references inside the draft schemas resolve; the returned mapping holds
    the draft schemas the index addresses by file name.
    """
    schemas_v1 = repository_root / "schemas" / "v1"
    schemas_draft = repository_root / "schemas" / "draft-sources-v1"
    assert schemas_v1.is_dir(), f"draft sources checkout has no schemas/v1: {repository_root}"
    assert schemas_draft.is_dir(), (
        f"draft sources checkout has no schemas/draft-sources-v1: {repository_root}"
    )
    resources: list[tuple[str, Any]] = []
    schemas: dict[str, Any] = {}
    for directory in (schemas_v1, schemas_draft):
        for path in sorted(directory.glob("*.json")):
            document = json.loads(path.read_bytes())
            Draft202012Validator.check_schema(document)
            assert document.get("$schema") == DRAFT_2020_12_SCHEMA_ID, (
                f"draft sources schema is not Draft 2020-12: {path.name}"
            )
            assert isinstance(document.get("$id"), str) and document["$id"], (
                f"draft sources schema declares no $id: {path.name}"
            )
            resources.append((document["$id"], Resource.from_contents(document)))
            if directory == schemas_draft:
                assert path.name not in schemas, f"duplicate draft schema: {path.name}"
                schemas[path.name] = document
    assert set(schemas) == EXPECTED_DRAFT_SCHEMA_NAMES, (
        f"draft sources checkout publishes {sorted(schemas)}"
    )
    return schemas, Registry().with_resources(resources)


def _check_draft_schema_case(
    entry: dict[str, Any],
    *,
    schemas: Mapping[str, Any],
    registry: Registry,
    suite_root: Path,
) -> None:
    """Assert one indexed case's expected valid flag at the validator entry."""
    schema_name = entry["schema"]
    assert schema_name in schemas, f"draft schema case addresses no schema: {entry!r}"
    relative = Path(entry["instance"])
    assert not relative.is_absolute() and ".." not in relative.parts, (
        f"draft schema case escapes the suite root: {entry!r}"
    )
    instance_path = suite_root / relative
    assert instance_path.is_file(), f"draft schema case publishes no file at {relative}"
    instance = json.loads(instance_path.read_bytes())
    validator = Draft202012Validator(schemas[schema_name], registry=registry)
    actual = validator.is_valid(instance)
    assert actual == entry["valid"], (
        f"draft schema case {schema_name} {relative}: "
        f"expected valid={entry['valid']}, got {actual}"
    )


def _check_snapshot_vector(vector: dict[str, Any]) -> None:
    """Recompute one snapshot vector's entry hashes, order and CCJ-1 digest."""
    vector_id = vector["id"]
    utf8_files: dict[str, str] = vector["utf8_files"]
    inventory: dict[str, Any] = vector["inventory"]
    entries: list[dict[str, Any]] = inventory["files"]
    assert [entry["path"] for entry in entries] == sorted(utf8_files), (
        f"draft snapshot vector {vector_id} inventory order is not the sorted path order"
    )
    for entry in entries:
        raw = utf8_files[entry["path"]].encode("utf-8")
        assert entry["sha256"] == "sha256:" + hashlib.sha256(raw).hexdigest(), (
            f"draft snapshot vector {vector_id} entry digest mismatch for {entry['path']}"
        )
    body = {key: value for key, value in inventory.items() if key != "snapshot"}
    digest = "sha256:" + hashlib.sha256(protocol_json.canonical_bytes(body)).hexdigest()
    assert inventory["snapshot"] == digest, (
        f"draft snapshot vector {vector_id} snapshot digest mismatch: "
        f"expected {inventory['snapshot']}, recomputed {digest}"
    )


def _check_mapping_covers_exactly(
    case_ids: Collection[str], mapping: Mapping[str, str]
) -> None:
    """Assert the owning-task mapping names exactly the suite's case ids."""
    missing = sorted(set(case_ids) - set(mapping))
    extra = sorted(set(mapping) - set(case_ids))
    assert not missing, f"draft semantic cases without an owning task: {missing}"
    assert not extra, f"owning-task entries without a draft semantic case: {extra}"


def _load_suite() -> (
    tuple[list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]], dict[str, Any], Registry]
):
    pin = _load_pin()
    root = _suite_root()
    index = json.loads(_authenticated_suite_bytes(root, "index.json", pin))
    semantic = json.loads(_authenticated_suite_bytes(root, "semantic-cases.json", pin))
    snapshot = json.loads(_authenticated_suite_bytes(root, "snapshot-cases.json", pin))
    schemas, registry = _load_schemas(root.parent.parent)
    return index, semantic, snapshot, schemas, registry


if ROOT_TEXT:
    SCHEMA_CASES, SEMANTIC_CASES, SNAPSHOT_VECTORS, DRAFT_SCHEMAS, SCHEMA_REGISTRY = _load_suite()
else:
    SCHEMA_CASES: list[dict[str, Any]] = []
    SEMANTIC_CASES: list[dict[str, Any]] = []
    SNAPSHOT_VECTORS: list[dict[str, Any]] = []
    DRAFT_SCHEMAS: dict[str, Any] = {}
    SCHEMA_REGISTRY = None  # type: ignore[assignment]


def test_draft_sources_suite_files_authenticate_against_the_committed_pin() -> None:
    pin = _load_pin()
    root = _suite_root()
    assert set(pin["files"]) == set(EXPECTED_SUITE_FILES)
    for name in EXPECTED_SUITE_FILES:
        raw = _authenticated_suite_bytes(root, name, pin)
        assert json.loads(raw) is not None


def test_draft_sources_tampered_index_digest_fails_authentication(tmp_path: Path) -> None:
    pin = _load_pin()
    root = _suite_root()
    tampered = tmp_path / "suite"
    tampered.mkdir()
    for name in EXPECTED_SUITE_FILES:
        (tampered / name).write_bytes(_authenticated_suite_bytes(root, name, pin))
    raw = bytearray((tampered / "index.json").read_bytes())
    raw[len(raw) // 2] ^= 0x01
    (tampered / "index.json").write_bytes(bytes(raw))
    with pytest.raises(AssertionError, match="digest mismatch"):
        _authenticated_suite_bytes(tampered, "index.json", pin)


def test_draft_sources_missing_suite_file_fails_authentication(tmp_path: Path) -> None:
    pin = _load_pin()
    root = _suite_root()
    partial = tmp_path / "suite"
    partial.mkdir()
    for name in ("index.json", "semantic-cases.json"):
        (partial / name).write_bytes(_authenticated_suite_bytes(root, name, pin))
    with pytest.raises(AssertionError, match="publishes no file"):
        _authenticated_suite_bytes(partial, "snapshot-cases.json", pin)


def test_draft_sources_schema_inventory_is_exhaustive() -> None:
    assert len(SCHEMA_CASES) == EXPECTED_SCHEMA_CASE_COUNT
    for entry in SCHEMA_CASES:
        assert set(entry) == {"schema", "instance", "valid"}, entry
        assert isinstance(entry["valid"], bool), entry
    assert len({entry["instance"] for entry in SCHEMA_CASES}) == len(SCHEMA_CASES)
    counts = {
        schema: sum(entry["schema"] == schema for entry in SCHEMA_CASES)
        for schema in EXPECTED_SCHEMA_CASE_COUNTS
    }
    assert counts == EXPECTED_SCHEMA_CASE_COUNTS
    coverage = {
        schema: {entry["valid"] for entry in SCHEMA_CASES if entry["schema"] == schema}
        for schema in EXPECTED_SCHEMA_CASE_COUNTS
    }
    assert set(coverage) == set(DRAFT_SCHEMAS) - {"source-types-v1.schema.json"}
    assert all(values == {True, False} for values in coverage.values())


@pytest.mark.parametrize(
    "entry",
    SCHEMA_CASES,
    ids=[f"{entry['schema']}:{entry['instance']}" for entry in SCHEMA_CASES],
)
def test_draft_sources_schema_case(entry: dict[str, Any]) -> None:
    _check_draft_schema_case(
        entry,
        schemas=DRAFT_SCHEMAS,
        registry=SCHEMA_REGISTRY,
        suite_root=_suite_root(),
    )


@pytest.mark.parametrize(
    "entry",
    [
        entry
        for entry in SCHEMA_CASES
        if entry["schema"] == "skillfile-lock-v1.schema.json"
    ],
    ids=[
        f"{entry['schema']}:{entry['instance']}"
        for entry in SCHEMA_CASES
        if entry["schema"] == "skillfile-lock-v1.schema.json"
    ],
)
def test_draft_sources_skillfile_lock_schema_case_through_production_reader(
    entry: dict[str, Any],
) -> None:
    """Drive every lock/schema-types case through csk's structural reader.

    The pinned schema corpus uses synthetic values and intentionally copies one
    digest across all cases.  The production schema-only entry validates the
    same shape and union arms without misrepresenting those fixtures as real
    lock digest vectors; full reader/digest coverage is supplied by
    ``tests/test_skillfile_lock.py`` fixtures.
    """
    raw = (_suite_root() / Path(entry["instance"])).read_bytes()
    if entry["valid"]:
        parsed = source_lock.read_lock_schema(raw)
        assert parsed.schema_version == 1
    else:
        with pytest.raises(source_errors.SourceError):
            source_lock.read_lock_schema(raw)


@pytest.mark.parametrize("valid", [True, False], ids=["valid-case", "invalid-case"])
def test_draft_sources_flipped_valid_flag_fails_the_schema_gate(valid: bool) -> None:
    entry = deepcopy(next(case for case in SCHEMA_CASES if case["valid"] is valid))
    entry["valid"] = not entry["valid"]
    with pytest.raises(AssertionError, match="expected valid="):
        _check_draft_schema_case(
            entry,
            schemas=DRAFT_SCHEMAS,
            registry=SCHEMA_REGISTRY,
            suite_root=_suite_root(),
        )


def test_draft_sources_snapshot_inventory_is_exhaustive() -> None:
    assert len(SNAPSHOT_VECTORS) == EXPECTED_SNAPSHOT_VECTOR_COUNT
    for vector in SNAPSHOT_VECTORS:
        assert set(vector) == {"id", "inventory", "utf8_files"}, vector["id"]
        assert set(vector["inventory"]) == {"schema_version", "algorithm", "files", "snapshot"}
    assert len({vector["id"] for vector in SNAPSHOT_VECTORS}) == EXPECTED_SNAPSHOT_VECTOR_COUNT
    assert len({vector["inventory"]["snapshot"] for vector in SNAPSHOT_VECTORS}) == (
        EXPECTED_SNAPSHOT_VECTOR_COUNT
    )
    assert len({vector["utf8_files"]["SKILL.md"] for vector in SNAPSHOT_VECTORS}) == 1


@pytest.mark.parametrize(
    "vector",
    SNAPSHOT_VECTORS,
    ids=[vector["id"] for vector in SNAPSHOT_VECTORS],
)
def test_draft_sources_snapshot_vector(vector: dict[str, Any]) -> None:
    _check_snapshot_vector(vector)


def test_draft_sources_mutated_snapshot_content_fails_the_snapshot_gate() -> None:
    vector = deepcopy(SNAPSHOT_VECTORS[0])
    first_path = next(iter(vector["utf8_files"]))
    vector["utf8_files"][first_path] += "mutated"
    with pytest.raises(AssertionError, match="entry digest mismatch"):
        _check_snapshot_vector(vector)


def test_draft_sources_mutated_snapshot_digest_fails_the_snapshot_gate() -> None:
    vector = deepcopy(SNAPSHOT_VECTORS[0])
    vector["inventory"]["snapshot"] = "sha256:" + "0" * 64
    with pytest.raises(AssertionError, match="snapshot digest mismatch"):
        _check_snapshot_vector(vector)


def test_draft_sources_semantic_inventory_is_exhaustive() -> None:
    assert len(SEMANTIC_CASES) == EXPECTED_SEMANTIC_CASE_COUNT
    for case in SEMANTIC_CASES:
        assert set(case) == {"id", "input", "expected"}, case.get("id")
    assert len({case["id"] for case in SEMANTIC_CASES}) == EXPECTED_SEMANTIC_CASE_COUNT


def test_draft_sources_semantic_case_owners_cover_the_suite_exactly() -> None:
    _check_mapping_covers_exactly([case["id"] for case in SEMANTIC_CASES], CASE_OWNERS)


def test_draft_sources_case_id_absent_from_mapping_fails() -> None:
    with pytest.raises(AssertionError, match="without an owning task"):
        _check_mapping_covers_exactly(
            [*(case["id"] for case in SEMANTIC_CASES), "no-such-case"], CASE_OWNERS
        )


def test_draft_sources_mapping_key_absent_from_suite_fails() -> None:
    with pytest.raises(AssertionError, match="without a draft semantic case"):
        _check_mapping_covers_exactly(
            [case["id"] for case in SEMANTIC_CASES],
            {**CASE_OWNERS, "no-such-case": "TASK-260916-12bfrq"},
        )


def test_draft_sources_driver_registration_rejects_unknown_case() -> None:
    with pytest.raises(AssertionError, match="unknown case"):
        register_semantic_driver("no-such-case", lambda case: None)


def test_draft_sources_driver_registration_rejects_duplicate() -> None:
    # A second registration for an already-driven id fails without
    # touching the registry, so no cleanup is needed. (The suite is
    # fully driven; the pre-coverage form picked an undriven case.)
    case_id = next(
        case["id"] for case in SEMANTIC_CASES if case["id"] in SEMANTIC_DRIVERS
    )
    with pytest.raises(AssertionError, match="duplicate driver"):
        register_semantic_driver(case_id, lambda case: None)


@pytest.mark.parametrize(
    "case",
    SEMANTIC_CASES,
    ids=[case["id"] for case in SEMANTIC_CASES],
)
def test_draft_sources_semantic_case(case: dict[str, Any]) -> None:
    case_id = case["id"]
    assert case_id in CASE_OWNERS, f"draft semantic case without an owning task: {case_id}"
    driver = SEMANTIC_DRIVERS.get(case_id)
    if driver is None:
        pytest.skip(f"not yet implemented: {CASE_OWNERS[case_id]}")
    driver(case)


def test_draft_sources_registered_driver_dispatch_through_the_semantic_entry() -> None:
    """A registered driver receives its exact case via the semantic entry.

    Regression for the dispatch gap: the parametrized semantic test ends in
    ``driver(case)``, but with zero drivers registered every case skips and no
    other test proves the call happens. This test registers a driver for a
    real still-undriven case, drives the actual
    :func:`test_draft_sources_semantic_case` entry (not a private helper),
    asserts the exact case object reaches the driver, and proves a raising
    driver fails the entry. It kills the one-case narrowing mutant
    ``if case_id != "<that case>": driver(case)`` in either phase.
    """
    # The suite is fully driven, so the probe driver temporarily
    # replaces the registered one instead of filling an undriven id.
    # (Pop-then-restore keeps the registry identical in every state.)
    case = SEMANTIC_CASES[0]
    real = SEMANTIC_DRIVERS.pop(case["id"], None)
    try:
        received: list[dict[str, Any]] = []

        def _record(driven: dict[str, Any]) -> None:
            received.append(driven)

        register_semantic_driver(case["id"], _record)
        try:
            test_draft_sources_semantic_case(case)
        finally:
            del SEMANTIC_DRIVERS[case["id"]]
        assert received == [case]
        assert received[0] is case

        def _boom(driven: dict[str, Any]) -> None:
            raise AssertionError("driver failure propagates through the semantic entry")

        register_semantic_driver(case["id"], _boom)
        try:
            with pytest.raises(AssertionError, match="driver failure propagates"):
                test_draft_sources_semantic_case(case)
        finally:
            del SEMANTIC_DRIVERS[case["id"]]
    finally:
        if real is not None:
            SEMANTIC_DRIVERS[case["id"]] = real


def _drive_unknown_alias(case: dict[str, Any]) -> None:
    """Drive ``unknown-alias`` through the production Skillfile parser.

    Registered by TASK-260916-2u0v5j (parse/opt-in): an unknown selector alias
    fails at parse time with ``source_alias_unknown``, before any expansion.
    """
    doc = {
        "schema_version": 2,
        "sources": case["input"]["sources"],
        "skills": [
            {"name": "probe", "from": case["input"]["from"], "directory": "."},
        ],
    }
    with pytest.raises(source_errors.SourceError) as excinfo:
        manifest.parse_manifest(doc, Path("Skillfile.json"), allow_schema_2=True)
    assert excinfo.value.code == case["expected"]


register_semantic_driver("unknown-alias", _drive_unknown_alias)


def _policy_case_document(case: dict[str, Any]) -> dict[str, Any]:
    """Build a policy fixture from one transport semantic-case input."""

    case_id = case["id"]
    case_input = case["input"]
    repository = case_input.get("repository", "example.org/kit")

    def endpoint(
        url: str,
        authentication: str = "team-https",
        **extra: Any,
    ) -> dict[str, Any]:
        value: dict[str, Any] = {"url": url, "authentication": authentication}
        value.update(extra)
        return value

    if case_id == "endpoint-identity-mismatch":
        return {
            "schema_version": 1,
            "repositories": {
                repository: {
                    "endpoints": [endpoint(case_input["endpoint"])],
                    "fallback": "none",
                }
            },
        }
    if case_id == "v2-reader-accepts-v1-policy":
        return {
            "schema_version": 1,
            "repositories": {
                repository: {
                    "endpoints": [endpoint("git@example.org:kit.git", "team-ssh")],
                    "fallback": "none",
                }
            },
        }
    if case_id == "v2-v1-reader-rejects-v2-policy":
        return {
            "schema_version": 2,
            "repositories": {
                repository: {
                    "endpoints": [endpoint("https://example.org:8443/kit.git")],
                    "fallback": "none",
                }
            },
        }
    if case_id == "v2-port-endpoint":
        return {
            "schema_version": 2,
            "repositories": {
                repository: {
                    "endpoints": [endpoint(case_input["endpoint"])],
                    "fallback": "none",
                }
            },
        }
    if case_id == "v2-declared-mirror":
        return {
            "schema_version": 2,
            "repositories": {
                repository: {
                    "endpoints": [
                        endpoint(
                            case_input["endpoint"],
                            "mirror-https",
                            mirror_of=case_input["mirror_of"],
                        )
                    ],
                    "fallback": "none",
                }
            },
        }
    if case_id == "v2-mirror-first":
        urls = case_input["endpoints"]
        return {
            "schema_version": 2,
            "repositories": {
                repository: {
                    "endpoints": [
                        endpoint(urls[0], "mirror-https", mirror_of=case_input["mirror_of"]),
                        endpoint(urls[1]),
                    ],
                    "fallback": case_input["fallback"],
                }
            },
        }
    if case_id == "v2-alias-resolution":
        return {
            "schema_version": 2,
            "repositories": {
                repository: {
                    "endpoints": [
                        endpoint(
                            case_input["endpoint"],
                            "team-https",
                            alias=case_input["alias"],
                            mirror_of=case_input["mirror_of"],
                        )
                    ],
                    "fallback": "none",
                }
            },
            "aliases": case_input["aliases"],
        }
    if case_id == "v2-undeclared-mirror":
        endpoint_value = endpoint(case_input["endpoint"])
        return {
            "schema_version": 2,
            "repositories": {
                repository: {"endpoints": [endpoint_value], "fallback": "none"}
            },
        }
    if case_id == "v2-pin-port-mismatch":
        return {
            "schema_version": 2,
            "repositories": {
                repository: {
                    "endpoints": [endpoint(case_input["endpoints"][0])],
                    "pin": case_input["pin"],
                    "fallback": "none",
                }
            },
        }
    if case_id in {
        "v2-alias-unknown",
        "v2-alias-mirror-undeclared",
        "v2-embedded-alias-host",
        "v2-alias-auth-mismatch",
        "v2-alias-chain",
        "v2-double-port",
        "v2-spurious-mirror-of",
        "v2-mirror-of-mismatch",
    }:
        endpoint_value = endpoint(
            case_input["endpoint"],
            case_input.get("endpoint_authentication", "team-https"),
        )
        if "alias" in case_input:
            endpoint_value["alias"] = case_input["alias"]
        if "mirror_of" in case_input and case_input["mirror_of"] is not None:
            endpoint_value["mirror_of"] = case_input["mirror_of"]
        document: dict[str, Any] = {
            "schema_version": 2,
            "repositories": {
                repository: {"endpoints": [endpoint_value], "fallback": "none"}
            },
        }
        if "aliases" in case_input:
            document["aliases"] = case_input["aliases"]
        return document
    raise AssertionError(f"no repository-policy fixture for semantic case {case_id!r}")


def _drive_repository_policy_case_impl(case: dict[str, Any]) -> None:
    """Drive policy semantics through the production parser and resolver."""

    case_id = case["id"]
    case_input = case["input"]
    expected = case["expected"]
    document = _policy_case_document(case)
    if case_id == "v2-reader-accepts-v1-policy":
        parsed = repository_policy.parse_policy(document, reader_revision=2)
        assert parsed.schema_version == 1
        return
    if case_id == "v2-v1-reader-rejects-v2-policy":
        with pytest.raises(repository_policy.RepositoryPolicyError) as excinfo:
            repository_policy.parse_policy(document, reader_revision=1)
        assert excinfo.value.code == expected.split(";", 1)[0]
        return

    if case_id in {
        "v2-port-endpoint",
        "v2-declared-mirror",
        "v2-mirror-first",
        "v2-alias-resolution",
    }:
        parsed = repository_policy.parse_policy(document)
        plan = repository_policy.select_endpoints(parsed, case_input["repository"])
        assert all(item.identity == case_input["repository"] for item in plan.endpoints)
        if case_id == "v2-port-endpoint":
            assert plan.max_attempts == 1
            assert plan.endpoints[0].provenance.url_port == 8443
        elif case_id == "v2-declared-mirror":
            assert plan.endpoints[0].host == "mirror.example.net"
        elif case_id == "v2-mirror-first":
            assert plan.endpoints[0].host == "mirror.example.net"
            assert plan.next_endpoint(case_input["first_failure"]) == plan.endpoints[1]
        else:
            assert plan.endpoints[0].host == "mirror.corp.example"
            assert plan.endpoints[0].port == 8443
        return

    with pytest.raises(repository_policy.RepositoryPolicyError) as excinfo:
        repository_policy.parse_policy(
            document,
            reader_revision=1 if case_id == "endpoint-identity-mismatch" else 2,
        )
    assert excinfo.value.code == expected.split(";", 1)[0]


def _drive_repository_policy_case(case: dict[str, Any]) -> None:
    """Drive one policy case while proving no socket attempt can occur."""

    network_attempts = 0

    def fail_network_attempt(*args: Any, **kwargs: Any) -> Any:
        nonlocal network_attempts
        network_attempts += 1
        raise AssertionError("repository policy validation attempted network I/O")

    with patch.object(socket, "socket", side_effect=fail_network_attempt):
        _drive_repository_policy_case_impl(case)
    assert network_attempts == 0


for _case_id in (
    "endpoint-identity-mismatch",
    "v2-port-endpoint",
    "v2-declared-mirror",
    "v2-mirror-first",
    "v2-alias-resolution",
    "v2-reader-accepts-v1-policy",
    "v2-undeclared-mirror",
    "v2-pin-port-mismatch",
    "v2-alias-unknown",
    "v2-alias-mirror-undeclared",
    "v2-embedded-alias-host",
    "v2-alias-auth-mismatch",
    "v2-alias-chain",
    "v2-double-port",
    "v2-spurious-mirror-of",
    "v2-mirror-of-mismatch",
    "v2-v1-reader-rejects-v2-policy",
):
    register_semantic_driver(_case_id, _drive_repository_policy_case)


def _write_selection_skill(directory: Path, name: str) -> None:
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "SKILL.md").write_text(
        f"---\nname: {name}\ndescription: Conformance fixture {name}\n---\n# {name}\n",
        encoding="utf-8",
    )


def _drive_selector_escape(case: dict[str, Any]) -> None:
    """Drive ``selector-escape`` through the production directory resolver.

    Registered by TASK-260916-2wjh3m (selection): the selector directory is a
    portable path whose leading component is a symlink to a directory outside
    the source root. The escape target exists, yet resolution must fail with
    ``source_selection_invalid`` before any member read.
    """

    if not _selection_fs.supports_descriptor_traversal():
        pytest.skip(_selection_fs.NO_DESCRIPTOR_TRAVERSAL_REASON)

    import tempfile

    from csk.sources import selection as selection_module

    directory = case["input"]["directory"]
    physical = case["input"]["physical"]
    with tempfile.TemporaryDirectory(prefix="csk-selector-escape-") as raw:
        tmp = Path(raw)
        root = tmp / "src"
        root.mkdir()
        outside = tmp / physical
        _write_selection_skill(outside, "review")
        first, _, _ = directory.partition("/")
        # Link the first selector component at the outside top directory.
        link = root / first
        target = tmp / physical.split("/")[0]
        link.symlink_to(target, target_is_directory=True)
        with pytest.raises(source_errors.SourceError) as excinfo:
            selection_module.resolve_selector_directory(root, directory)
        assert excinfo.value.code == case["expected"]


def _drive_missing_excluded_literal(case: dict[str, Any]) -> None:
    """Drive ``missing-excluded-literal`` through collection expansion.

    Registered by TASK-260916-2wjh3m (selection): a missing explicit member
    fails ``source_member_missing`` even though the same name is excluded.
    """

    if not _selection_fs.supports_descriptor_traversal():
        pytest.skip(_selection_fs.NO_DESCRIPTOR_TRAVERSAL_REASON)

    import tempfile

    from csk.sources import selection as selection_module
    from csk.sources.skillfile_v2 import CollectionSelector

    with tempfile.TemporaryDirectory(prefix="csk-missing-excluded-") as raw:
        root = Path(raw) / "src"
        children = case["input"]["children"]
        if isinstance(children, list):
            root.mkdir(parents=True)
            for child in children:
                (root / child).mkdir(parents=True)
        else:
            root.mkdir(parents=True)
        selector = CollectionSelector(
            from_alias="local",
            directory=".",
            include=tuple(case["input"]["include"]),
            exclude=tuple(case["input"]["exclude"]),
        )
        with pytest.raises(source_errors.SourceError) as excinfo:
            selection_module.expand_collection(root, selector)
        assert excinfo.value.code == case["expected"]


def _drive_bad_wildcard_member(case: dict[str, Any]) -> None:
    """Drive ``bad-wildcard-member`` through collection expansion.

    Registered by TASK-260916-2wjh3m (selection): ``"*"`` discovers one valid
    and one invalid candidate; the whole operation fails with
    ``source_member_invalid`` instead of skipping the bad member.
    """

    if not _selection_fs.supports_descriptor_traversal():
        pytest.skip(_selection_fs.NO_DESCRIPTOR_TRAVERSAL_REASON)

    import tempfile

    from csk.sources import selection as selection_module
    from csk.sources.skillfile_v2 import CollectionSelector

    with tempfile.TemporaryDirectory(prefix="csk-bad-wildcard-") as raw:
        root = Path(raw) / "src"
        root.mkdir(parents=True)
        for folder, kind in case["input"]["children"].items():
            member = root / folder
            if kind == "valid":
                _write_selection_skill(member, folder)
            elif kind == "missing-SKILL.md":
                member.mkdir(parents=True)
            else:
                raise AssertionError(f"unknown child kind {kind!r}")
        selector = CollectionSelector(
            from_alias="local",
            directory=".",
            include=tuple(case["input"]["include"]),
            exclude=(),
        )
        with pytest.raises(source_errors.SourceError) as excinfo:
            selection_module.expand_collection(root, selector)
        assert excinfo.value.code == case["expected"]


def _drive_duplicate_name(case: dict[str, Any]) -> None:
    """Drive ``duplicate-name`` through whole-set expansion.

    Registered by TASK-260916-2wjh3m (selection): two folders carry the same
    installed SKILL.md name, so the expanded set fails with
    ``source_name_conflict`` before any publication.
    """

    if not _selection_fs.supports_descriptor_traversal():
        pytest.skip(_selection_fs.NO_DESCRIPTOR_TRAVERSAL_REASON)

    import tempfile

    from csk.sources import selection as selection_module
    from csk.sources.skillfile_v2 import CollectionSelector

    with tempfile.TemporaryDirectory(prefix="csk-duplicate-name-") as raw:
        root = Path(raw) / "src"
        root.mkdir(parents=True)
        folders: list[str] = []
        for member in case["input"]["members"]:
            folders.append(member["folder"])
            _write_selection_skill(root / member["folder"], member["name"])
        selector = CollectionSelector(
            from_alias="local", directory=".", include=tuple(folders), exclude=()
        )
        with pytest.raises(source_errors.SourceError) as excinfo:
            selection_module.expand_selectors([selector], {"local": root})
        assert excinfo.value.code == case["expected"]


register_semantic_driver("selector-escape", _drive_selector_escape)
register_semantic_driver("missing-excluded-literal", _drive_missing_excluded_literal)
register_semantic_driver("bad-wildcard-member", _drive_bad_wildcard_member)
register_semantic_driver("duplicate-name", _drive_duplicate_name)


_TRANSPORT_LOCK = git_admission.LockedCommit("sha1", "0" * 40)


def _transport_snapshot() -> git_admission.Snapshot:
    item = git_admission.SnapshotFile("value", b"value")
    canonical = b"curator-build-source-v1\0value"
    return git_admission.Snapshot(
        object_format="sha1",
        commit=_TRANSPORT_LOCK.hex,
        files=(item,),
        canonical_bytes=canonical,
        digest="sha256:" + hashlib.sha256(canonical).hexdigest(),
    )


def _transport_git(root: Path, mappings: Mapping[str, Path]) -> git_admission.GitTool:
    """Create a trusted Git stand-in that maps only listed test URLs locally."""

    real = Path(shutil.which("git")).resolve()
    version = subprocess.run(
        (os.fspath(real), "--version"),
        stdin=subprocess.DEVNULL,
        capture_output=True,
        check=True,
        timeout=20,
        text=True,
    ).stdout.strip()
    exec_path = Path(
        subprocess.run(
            (os.fspath(real), "--exec-path"),
            stdin=subprocess.DEVNULL,
            capture_output=True,
            check=True,
            timeout=20,
            text=True,
        ).stdout.strip()
    ).resolve()
    root.mkdir(parents=True)
    script = root / "git-wrapper.py"
    wrapper = root / ("git-wrapper.cmd" if os.name == "nt" else "git-wrapper")
    mapping = {url: path.resolve().as_uri() for url, path in mappings.items()}
    script.write_text(
        f"#!{sys.executable}\n"
        "import subprocess, sys\n"
        f"MAPPINGS = {mapping!r}\n"
        "args = [MAPPINGS.get(value, 'protocol.file.allow=always' if value == 'protocol.https.allow=always' else value) for value in sys.argv[1:]]\n"
        f"raise SystemExit(subprocess.run([{os.fspath(real)!r}, *args], check=False).returncode)\n",
        encoding="utf-8",
    )
    if os.name == "nt":
        wrapper.write_text(
            f'@echo off\r\n"{sys.executable}" "{script}" %*\r\n',
            encoding="utf-8",
        )
    else:
        wrapper.write_bytes(script.read_bytes())
        wrapper.chmod(0o700)
    askpass = root / ("askpass.cmd" if os.name == "nt" else "askpass")
    if os.name == "nt":
        askpass.write_text("@echo off\r\nexit /b 1\r\n", encoding="utf-8")
    else:
        askpass.write_text("#!/bin/sh\nexit 1\n", encoding="utf-8")
        askpass.chmod(0o700)
    return git_admission.GitTool(
        executable=wrapper,
        exec_path=exec_path,
        allowed_versions=(version,),
        askpass=askpass,
    )


def _transport_bare(root: Path) -> tuple[Path, str]:
    """Create one local bare repository for real default-attempt drivers."""

    root.mkdir(parents=True)
    work = root / "work"
    bare = root / "remote.git"
    subprocess.run(
        (os.fspath(Path(shutil.which("git")).resolve()), "init", "--quiet", os.fspath(work)),
        stdin=subprocess.DEVNULL,
        capture_output=True,
        check=True,
        timeout=20,
    )
    (work / "README.md").write_bytes(b"draft transport fixture\n")
    git = os.fspath(Path(shutil.which("git")).resolve())
    subprocess.run((git, "-C", os.fspath(work), "add", "--", "README.md"), check=True, timeout=20)
    subprocess.run(
        (
            git,
            "-C",
            os.fspath(work),
            "-c",
            "user.name=Fixture",
            "-c",
            "user.email=fixture@example.test",
            "commit",
            "--quiet",
            "-m",
            "fixture",
        ),
        stdin=subprocess.DEVNULL,
        capture_output=True,
        check=True,
        timeout=20,
    )
    commit = subprocess.run(
        (git, "-C", os.fspath(work), "rev-parse", "HEAD"),
        check=True,
        timeout=20,
        capture_output=True,
        text=True,
    ).stdout.strip()
    subprocess.run(
        (git, "clone", "--quiet", "--bare", os.fspath(work), os.fspath(bare)),
        stdin=subprocess.DEVNULL,
        capture_output=True,
        check=True,
        timeout=20,
    )
    return bare, commit


def _transport_failure_record(failure_class: str, url: str) -> str:
    """Return one complete manager/transport record for call-site injection."""

    records = {
        "dns": f"fatal: unable to access '{url}': Could not resolve host: example.org",
        "auth-rejected": "git@example.org: Permission denied (publickey).",
        "tls": f"fatal: unable to access '{url}': SSL certificate problem: unable to get local issuer certificate",
        "host-key": "Host key verification failed.",
        "integrity": "error: corrupt object deadbeef; fsck failed",
        "identity": f"fatal: repository '{url}/' not found",
        "ref-moved": "fatal: couldn't find remote ref refs/heads/locked",
        "audit": "csk: audit denied",
        "unknown": "csk: transport failure has no classified evidence",
        "http-404": f"fatal: unable to access '{url}': The requested URL returned error: 404",
    }
    return records[failure_class]


def _transport_policy(case: dict[str, Any]) -> repository_policy.RepositoryPolicy:
    case_id = case["id"]
    case_input = case["input"]
    identity = case_input.get("repository", "example.org/kit")
    if case_id in {
        "fallback-dns",
        "fallback-auth-rejected",
        "fallback-tls",
        "fallback-host-key",
        "fallback-integrity",
        "fallback-identity",
        "fallback-ref-moved",
        "fallback-audit",
        "fallback-unknown",
        "fallback-http-404",
        "pinned-auth",
    }:
        first = "https://example.org/kit.git"
        second = "https://example.org/kit"
        entry: dict[str, Any] = {
            "endpoints": [
                {"url": first, "authentication": "first"},
                {"url": second, "authentication": "second"},
            ],
            "fallback": case_input["fallback"],
        }
        if case_id == "pinned-auth":
            entry["pin"] = first
        return repository_policy.parse_policy(
            {
                "schema_version": 1,
                "repositories": {identity: entry},
            },
            reader_revision=1,
        )
    if case_id == "v2-external-build-mirror-admitted":
        return repository_policy.parse_policy(
            {
                "schema_version": 2,
                "repositories": {
                    identity: {
                        "endpoints": [
                            {
                                "url": case_input["endpoint"],
                                "authentication": "team-https",
                                "mirror_of": case_input["mirror_of"],
                            }
                        ],
                        "fallback": "none",
                    }
                },
            },
            reader_revision=2,
        )
    if case_id == "v2-external-build-port-refused":
        return repository_policy.parse_policy(
            {
                "schema_version": 2,
                "repositories": {
                    identity: {
                        "endpoints": [
                            {
                                "url": case_input["endpoint"],
                                "authentication": "team-ssh",
                            }
                        ],
                        "fallback": "none",
                    }
                },
            },
            reader_revision=2,
        )
    if case_id == "v2-external-build-alias-refused":
        return repository_policy.parse_policy(
            {
                "schema_version": 2,
                "repositories": {
                    identity: {
                        "endpoints": [
                            {
                                "url": case_input["endpoint"],
                                "authentication": "team-https",
                                "alias": case_input["alias"],
                                "mirror_of": case_input["mirror_of"],
                            }
                        ],
                        "fallback": "none",
                    }
                },
                "aliases": case_input["aliases"],
            },
            reader_revision=2,
        )
    raise AssertionError(f"no transport policy fixture for {case_id!r}")


def _drive_transport_case(case: dict[str, Any]) -> None:
    case_id = case["id"]
    case_input = case["input"]
    expected = case["expected"]
    calls: list[str] = []

    if case_id == "fallback-policy-unreadable":
        with tempfile.TemporaryDirectory(prefix="csk-policy-case-") as raw_root:
            path = Path(raw_root) / "source-policy.json"
            path.write_bytes(b"{}")
            socket_attempts = 0

            def fail_socket(*args: Any, **kwargs: Any) -> Any:
                nonlocal socket_attempts
                socket_attempts += 1
                raise AssertionError("unreadable policy reached network")

            def unexpected_attempt(**kwargs: Any) -> git_admission.Snapshot:
                calls.append("attempt")
                return _transport_snapshot()

            with (
                patch.object(socket, "socket", side_effect=fail_socket),
                patch.object(
                    Path,
                    "read_bytes",
                    side_effect=PermissionError("policy unreadable"),
                ),
                pytest.raises(source_transport.TransportError) as excinfo,
            ):
                source_transport.acquire(
                    "example.org/kit",
                    _TRANSPORT_LOCK,
                    policy_path=path,
                    attempt=unexpected_attempt,
                )
            assert excinfo.value.code == repository_policy.CODE_POLICY_INVALID
            assert calls == []
            assert socket_attempts == 0
        return

    if case_id in {"v2-user-ssh-alias-ignored", "v2-user-insteadof-ignored"}:
        with tempfile.TemporaryDirectory(prefix="csk-user-config-case-") as raw_root:
            home = Path(raw_root)
            ssh = home / ".ssh"
            ssh.mkdir()
            (ssh / "config").write_text(
                "Host example.org\n  HostName real.example.net\n", encoding="utf-8"
            )
            (home / ".gitconfig").write_text(
                "[url \"https://evil.example.net/\"]\n"
                "    insteadOf = https://example.org/\n",
                encoding="utf-8",
            )
            with patch.dict(os.environ, {"HOME": os.fspath(home)}, clear=False):
                if case_id == "v2-user-ssh-alias-ignored":
                    with pytest.raises(source_transport.TransportError) as excinfo:
                        source_transport.acquire(
                            case_input["repository"],
                            _TRANSPORT_LOCK,
                            attempt=lambda **kwargs: _transport_snapshot(),
                        )
                    assert excinfo.value.code == repository_policy.CODE_ENDPOINT_UNAVAILABLE
                    assert calls == []
                else:
                    declared = case_input["declaration"]
                    declared_identity = repository_policy.canonical_endpoint_identity(
                        declared, revision=case_input["transport_revision"]
                    )
                    bare, commit = _transport_bare(home / "bare")
                    tool = _transport_git(home / "tool", {declared: bare})
                    real_run = git_admission.subprocess.run
                    fetches: list[tuple[object, ...]] = []

                    def record_fetch(*args: Any, **kwargs: Any) -> Any:
                        command = args[0]
                        assert isinstance(command, tuple)
                        if "fetch" in command:
                            fetches.append(command)
                        return real_run(*args, **kwargs)

                    with patch.object(
                        git_admission.subprocess, "run", new=record_fetch
                    ):
                        result = source_transport.acquire(
                            declared_identity,
                            git_admission.LockedCommit("sha1", commit),
                            tool,
                            declaration=declared,
                        )
                    assert result.snapshot.commit == commit
                    assert result.attempt_count == 1
                    assert len(fetches) == 1
                    assert result.attempts[0].endpoint.listed_url == declared
        return

    if case_id in {
        "fallback-dns",
        "fallback-auth-rejected",
        "fallback-tls",
        "fallback-host-key",
        "fallback-integrity",
        "fallback-identity",
        "fallback-ref-moved",
        "fallback-audit",
        "fallback-unknown",
        "fallback-http-404",
        "pinned-auth",
    }:
        policy = _transport_policy(case)
        identity = case_input.get("repository", "example.org/kit")
        plan = repository_policy.select_endpoints(policy, identity)
        with tempfile.TemporaryDirectory(prefix="csk-transport-case-") as raw_root:
            root = Path(raw_root)
            bare, commit = _transport_bare(root / "bare")
            tool = _transport_git(
                root / "tool",
                {plan.endpoints[-1].url: bare},
            )
            real_run = git_admission.subprocess.run
            fetches: list[tuple[object, ...]] = []
            injected = False

            def inject_failure(*args: Any, **kwargs: Any) -> Any:
                nonlocal injected
                command = args[0]
                assert isinstance(command, tuple)
                if "fetch" in command:
                    fetches.append(command)
                    if not injected and plan.endpoints[0].url in command:
                        injected = True
                        record = _transport_failure_record(
                            case_input["first_failure"], plan.endpoints[0].url
                        )
                        raise subprocess.CalledProcessError(
                            128, command, stderr=record.encode("utf-8")
                        )
                return real_run(*args, **kwargs)

            with patch.object(git_admission.subprocess, "run", new=inject_failure):
                if expected == "attempt-second":
                    result = source_transport.acquire_plan(
                        plan,
                        git_admission.LockedCommit("sha1", commit),
                        tool,
                    )
                    assert result.snapshot.commit == commit
                    assert result.attempt_count == 2
                    assert len(fetches) == 2
                    assert result.attempts[0].classification in {
                        "dns",
                        "ssh-auth-rejected",
                    }
                else:
                    with pytest.raises(git_admission.GitAdmissionError) as excinfo:
                        source_transport.acquire_plan(
                            plan,
                            git_admission.LockedCommit("sha1", commit),
                            tool,
                        )
                    error = excinfo.value
                    observed = (
                        error.attempts[0].classification
                        if isinstance(error, source_transport.TransportError)
                        else error.failure_class
                    )
                    assert observed in {
                        case_input["first_failure"],
                        "ssh-auth-rejected",
                        "unclassified",
                    }
                    assert len(fetches) == 1
        return

    policy = _transport_policy(case)
    identity = case_input.get("repository", "example.org/kit")
    plan = repository_policy.select_endpoints(policy, identity)

    if case_id == "v2-external-build-mirror-admitted":
        with tempfile.TemporaryDirectory(prefix="csk-external-mirror-case-") as raw_root:
            root = Path(raw_root)
            bare, commit = _transport_bare(root / "bare")
            tool = _transport_git(root / "tool", {plan.endpoints[0].url: bare})
            result = source_transport.acquire_plan(
                plan,
                git_admission.LockedCommit("sha1", commit),
                tool,
                lane=source_transport.LANE_EXTERNAL_BUILD,
            )
        assert result.snapshot.commit == commit
        assert result.attempt_count == 1
        assert result.attempts[0].endpoint.listed_url == plan.endpoints[0].url
        return

    def attempt(**kwargs: Any) -> git_admission.Snapshot:
        endpoint = kwargs["endpoint"]
        assert isinstance(endpoint, repository_policy.ResolvedEndpoint)
        calls.append(endpoint.url)
        if len(calls) == 1 and case_id != "v2-external-build-mirror-admitted":
            raise source_transport.TransportFailure(
                case_input.get("first_failure", "connection-refused"),
                "classified fixture failure with token=redacted",
            )
        return _transport_snapshot()

    if case_id in {
        "v2-external-build-port-refused",
        "v2-external-build-alias-refused",
    }:
        with pytest.raises(ExternalBuildError) as excinfo:
            source_transport.acquire_plan(
                plan,
                _TRANSPORT_LOCK,
                attempt=attempt,
                lane=source_transport.LANE_EXTERNAL_BUILD,
            )
        assert excinfo.value.code == "build_repository_identity_invalid"
        assert calls == []
        return

    if expected == "attempt-second":
        result = source_transport.acquire_plan(plan, _TRANSPORT_LOCK, attempt=attempt)
        assert result.snapshot.commit == _TRANSPORT_LOCK.hex
        assert result.attempt_count == 2
        assert len(calls) == 2
    else:
        with pytest.raises(source_transport.TransportError) as excinfo:
            source_transport.acquire_plan(plan, _TRANSPORT_LOCK, attempt=attempt)
        if isinstance(excinfo.value, source_transport.TransportFailure):
            assert excinfo.value.failure_class == case_input["first_failure"]
        else:
            assert excinfo.value.attempts[0].classification == case_input["first_failure"]
        assert len(calls) == 1


for _case_id in (
    "fallback-dns",
    "fallback-auth-rejected",
    "fallback-tls",
    "fallback-host-key",
    "fallback-integrity",
    "fallback-identity",
    "fallback-ref-moved",
    "fallback-audit",
    "fallback-unknown",
    "fallback-policy-unreadable",
    "fallback-http-404",
    "pinned-auth",
    "v2-user-ssh-alias-ignored",
    "v2-user-insteadof-ignored",
    "v2-external-build-mirror-admitted",
    "v2-external-build-port-refused",
    "v2-external-build-alias-refused",
):
    register_semantic_driver(_case_id, _drive_transport_case)
# Semantic drivers registered by TASK-260916-100uew (local boundaries).
#
# Each driver builds a real temporary-filesystem fixture from the case
# input, calls a ``csk.sources.boundaries`` production entry point, and
# asserts the exact expected outcome. Imports stay function-local so this
# block appends without touching the shared import header.


def _boundary_fixture(tmp: Path, case_input: dict[str, Any]) -> tuple[Path, Path]:
    """Build (project, home) honouring the case's physical/output spellings."""

    project = tmp / "project"
    home = tmp / "home"
    home.mkdir(parents=True)
    physical = str(case_input.get("physical", ""))
    outputs = [str(output) for output in case_input.get("outputs", [])]
    seeds = [physical, *outputs]
    if not physical:
        planned = str(case_input.get("planned_output", ""))
        publication = str(case_input.get("publication_output", ""))
        seeds = [planned, publication]
    for seed in seeds:
        if not seed:
            continue
        relative = seed.split("/", 1)[1] if "/" in seed else seed
        if relative:
            (project / relative).mkdir(parents=True, exist_ok=True)
    return (project, home)


def _drive_broad_root(case: dict[str, Any]) -> None:
    """Drive ``broad-root`` through the production package check.

    Registered by TASK-260916-100uew (boundaries): a broad alias path
    with a safe selected subdirectory is allowed.
    """

    import tempfile

    from csk.sources import boundaries as boundaries_module
    from csk.sources import repository_policy as policy_module

    case_input = case["input"]
    with tempfile.TemporaryDirectory(prefix="csk-broad-root-") as raw:
        project, home = _boundary_fixture(Path(raw), case_input)
        (project / "agents" / "skills" / "review").mkdir(parents=True, exist_ok=True)
        record = boundaries_module.freeze_boundaries(project, home)
        policy = policy_module.RepositoryPolicy(
            schema_version=1, repositories={}, root_inputs={}
        )
        resolved = boundaries_module.check_selected_package(
            record, project, case_input["directory"], alias="local", policy=policy
        )
        assert case["expected"] == "allow"
        assert resolved.is_dir()


def _drive_managed_source(case: dict[str, Any]) -> None:
    """Drive ``managed-source`` through the production package check.

    Registered by TASK-260916-100uew (boundaries): a selected package
    inside a managed output fails ``source_output_overlap``.
    """

    import tempfile

    from csk.sources import boundaries as boundaries_module
    from csk.sources import repository_policy as policy_module

    case_input = case["input"]
    with tempfile.TemporaryDirectory(prefix="csk-managed-source-") as raw:
        project, home = _boundary_fixture(Path(raw), case_input)
        record = boundaries_module.freeze_boundaries(project, home)
        policy = policy_module.RepositoryPolicy(
            schema_version=1, repositories={}, root_inputs={}
        )
        with pytest.raises(source_errors.SourceError) as excinfo:
            boundaries_module.check_selected_package(
                record, project, case_input["directory"], alias="local", policy=policy
            )
        assert excinfo.value.code == case["expected"]


def _drive_symlink_managed(case: dict[str, Any]) -> None:
    """Drive ``symlink-managed`` through the production package check.

    Registered by TASK-260916-100uew (boundaries): the selected
    directory resolves through a link into a managed output, so it
    fails ``source_output_overlap``.
    """

    import tempfile

    from csk.sources import boundaries as boundaries_module
    from csk.sources import repository_policy as policy_module

    case_input = case["input"]
    with tempfile.TemporaryDirectory(prefix="csk-symlink-managed-") as raw:
        project, home = _boundary_fixture(Path(raw), case_input)
        physical = str(case_input["physical"])
        managed = project / physical.split("/", 1)[1]
        managed.mkdir(parents=True, exist_ok=True)
        directory = str(case_input["directory"])
        first, _, _ = directory.partition("/")
        link = project / first
        if link.exists() or link.is_symlink():
            raise AssertionError(f"fixture collision at {first!r}")
        managed_parent = managed.parent
        link.symlink_to(managed_parent, target_is_directory=True)
        record = boundaries_module.freeze_boundaries(project, home)
        policy = policy_module.RepositoryPolicy(
            schema_version=1, repositories={}, root_inputs={}
        )
        with pytest.raises(source_errors.SourceError) as excinfo:
            boundaries_module.check_selected_package(
                record, project, directory, alias="local", policy=policy
            )
        assert excinfo.value.code == case["expected"]


def _drive_case_alias(case: dict[str, Any]) -> None:
    """Drive ``case-alias`` through the production package check.

    Registered by TASK-260916-100uew (boundaries): on a
    case-insensitive filesystem the case-variant spelling names the
    managed output and fails ``source_output_overlap``. On a
    case-sensitive host the refusal is inapplicable and the driver
    skips with the declared platform bound.
    """

    import tempfile

    from csk.sources import boundaries as boundaries_module
    from csk.sources import repository_policy as policy_module

    case_input = case["input"]
    with tempfile.TemporaryDirectory(prefix="csk-case-alias-") as raw:
        tmp = Path(raw)
        probe = tmp / "CSK-DRIVER-PROBE"
        probe.write_text("x", encoding="utf-8")
        conflates = (tmp / "csk-driver-probe").exists()
        probe.unlink()
        if not conflates:
            pytest.skip(
                "case-alias needs a case-insensitive filesystem (declared platform bound)"
            )
        project, home = _boundary_fixture(tmp, case_input)
        physical = str(case_input["physical"])
        relative = physical.split("/", 1)[1]
        (project / relative).mkdir(parents=True, exist_ok=True)
        first = relative.split("/", 1)[0]
        record = boundaries_module.freeze_boundaries(project, home)
        policy = policy_module.RepositoryPolicy(
            schema_version=1, repositories={}, root_inputs={}
        )
        with pytest.raises(source_errors.SourceError) as excinfo:
            boundaries_module.check_selected_package(
                record, project, relative, alias="local", policy=policy
            )
        assert excinfo.value.code == case["expected"]
        assert first.lower() in excinfo.value.detail.lower()


def _drive_write_boundary_retarget(case: dict[str, Any]) -> None:
    """Drive ``write-boundary-retarget`` through the publication recheck.

    Registered by TASK-260916-100uew (boundaries): the publication
    destination left the planned managed output for an authored tree,
    so the recheck fails ``source_output_overlap``.
    """

    import tempfile

    from csk.sources import boundaries as boundaries_module

    case_input = case["input"]
    with tempfile.TemporaryDirectory(prefix="csk-write-retarget-") as raw:
        project, home = _boundary_fixture(Path(raw), case_input)
        planned = str(case_input["planned_output"]).split("/", 1)[1]
        publication = str(case_input["publication_output"]).split("/", 1)[1]
        (project / planned).mkdir(parents=True, exist_ok=True)
        (project / publication).mkdir(parents=True, exist_ok=True)
        record = boundaries_module.freeze_boundaries(project, home)
        with pytest.raises(source_errors.SourceError) as excinfo:
            boundaries_module.recheck_publication_destination(
                record, project, planned, publication + "/SKILL.md"
            )
        assert excinfo.value.code == case["expected"]


def _drive_root_no_inputs(case: dict[str, Any]) -> None:
    """Drive ``root-no-inputs`` through the production package check.

    Registered by TASK-260916-100uew (boundaries): a root selection
    without ``root_inputs`` fails ``source_output_overlap`` because the
    manager cannot prove separation.
    """

    import tempfile

    from csk.sources import boundaries as boundaries_module
    from csk.sources import repository_policy as policy_module

    case_input = case["input"]
    assert case_input["root_inputs"] is None
    with tempfile.TemporaryDirectory(prefix="csk-root-no-inputs-") as raw:
        project = Path(raw) / "project"
        home = Path(raw) / "home"
        (project / "agents" / "skills" / "review").mkdir(parents=True)
        home.mkdir(parents=True)
        record = boundaries_module.freeze_boundaries(project, home)
        policy = policy_module.RepositoryPolicy(
            schema_version=1, repositories={}, root_inputs={}
        )
        with pytest.raises(source_errors.SourceError) as excinfo:
            boundaries_module.check_selected_package(
                record, project, case_input["directory"], alias="local", policy=policy
            )
        assert excinfo.value.code == case["expected"]


register_semantic_driver("broad-root", _drive_broad_root)
register_semantic_driver("managed-source", _drive_managed_source)
register_semantic_driver("symlink-managed", _drive_symlink_managed)
register_semantic_driver("case-alias", _drive_case_alias)
register_semantic_driver("write-boundary-retarget", _drive_write_boundary_retarget)
register_semantic_driver("root-no-inputs", _drive_root_no_inputs)


# Semantic drivers registered by TASK-260916-sbzutf (local snapshots).
#
# Each driver builds a real temporary-filesystem fixture from the case
# input, calls a ``csk.sources.snapshot`` production entry point, and
# asserts the exact expected outcome. Imports stay function-local so this
# block appends without touching the shared import header.


def _drive_local_git_dirty(case: dict[str, Any]) -> None:
    """Drive ``local-git-dirty`` through production capture.

    Registered by TASK-260916-sbzutf (snapshots): ``.git`` claims HEAD
    bytes A while the working tree carries B plus untracked C, so the
    snapshot covers exactly B and C. No Git process may spawn.
    """

    if not _selection_fs.supports_descriptor_traversal():
        pytest.skip(_selection_fs.NO_DESCRIPTOR_TRAVERSAL_REASON)

    import subprocess
    import tempfile

    from csk.sources import local_snapshot
    from csk.sources import snapshot as snapshot_module

    head = case["input"]["head"]
    filesystem = case["input"]["filesystem"]
    untracked = case["input"]["untracked"]
    with tempfile.TemporaryDirectory(prefix="csk-local-git-dirty-") as raw:
        root = Path(raw) / "src"
        home = Path(raw) / "home"
        home.mkdir(parents=True)
        (root / ".git" / "objects").mkdir(parents=True)
        (root / ".git" / "HEAD").write_text(head, encoding="utf-8")
        (root / "tracked.txt").write_text(filesystem, encoding="utf-8")
        (root / "untracked.txt").write_text(untracked, encoding="utf-8")

        real_popen = subprocess.Popen
        real_run = subprocess.run

        def _forbidden(*args: Any, **kwargs: Any) -> Any:
            raise AssertionError("capture spawned a process during local-git-dirty")

        subprocess.Popen = _forbidden  # type: ignore[assignment]
        subprocess.run = _forbidden  # type: ignore[assignment]
        try:
            package = snapshot_module.capture_package_snapshot(root, ".", home=home)
        finally:
            subprocess.Popen = real_popen
            subprocess.run = real_run

        frozen = package.frozen_files()
        assert set(frozen) == {"tracked.txt", "untracked.txt"}
        assert frozen["tracked.txt"].data == filesystem.encode("utf-8")
        assert frozen["untracked.txt"].data == untracked.encode("utf-8")
        expected = local_snapshot.build_inventory(
            [
                ("tracked.txt", "sha256:" + hashlib.sha256(filesystem.encode("utf-8")).hexdigest(), False),
                ("untracked.txt", "sha256:" + hashlib.sha256(untracked.encode("utf-8")).hexdigest(), False),
            ],
            equivalent=lambda _left, _right: False,
        )
        assert package.inventory["snapshot"] == expected["snapshot"]
        assert case["expected"] == "snapshot-B-and-C"


def _drive_capture_mutation(case: dict[str, Any]) -> None:
    """Drive ``capture-mutation`` through capture plus revalidation.

    Registered by TASK-260916-sbzutf (snapshots): the tree holds A at
    capture and B at revalidation, so revalidation fails with
    ``source_snapshot_changed``.
    """

    if not _selection_fs.supports_descriptor_traversal():
        pytest.skip(_selection_fs.NO_DESCRIPTOR_TRAVERSAL_REASON)

    import tempfile

    from csk.sources import snapshot as snapshot_module
    from csk.sources._selection_fs import (
        PreflightPath,
        PreflightRequest,
        SelectionSession,
    )
    from csk.sources.selection import PRUNED_CHILD_NAMES

    captured_text = case["input"]["captured"]
    mutated_text = case["input"]["after-capture"]
    with tempfile.TemporaryDirectory(prefix="csk-capture-mutation-") as raw:
        root = Path(raw) / "src"
        home = Path(raw) / "home"
        home.mkdir(parents=True)
        (root / "victim.txt").parent.mkdir(parents=True, exist_ok=True)
        (root / "victim.txt").write_text(captured_text, encoding="utf-8")
        session = SelectionSession.open(
            root,
            home,
            managed_names=PRUNED_CHILD_NAMES,
            preflight=PreflightRequest(
                paths=(
                    PreflightPath(
                        (),
                        code="source_selection_invalid",
                        context="Snapshot driver session",
                    ),
                )
            ),
        )
        with session:
            captured = session.capture_tree(
                session.root,
                label="driver",
                code=snapshot_module.CODE_CAPTURE,
                missing_code=snapshot_module.CODE_CAPTURE_ABSENT,
                changed_code=snapshot_module.CODE_CAPTURE_CHANGED,
            )
            (root / "victim.txt").write_text(mutated_text, encoding="utf-8")
            with pytest.raises(source_errors.SourceError) as excinfo:
                snapshot_module.revalidate_capture(
                    session, session.root, captured, label="driver"
                )
        assert excinfo.value.code == case["expected"]


def _drive_frozen_copy_mutation(case: dict[str, Any]) -> None:
    """Drive ``frozen-copy-mutation`` through capture plus verification.

    Registered by TASK-260916-sbzutf (snapshots): the frozen copy holds
    A at audit and B before publication, so verification fails with
    ``source_snapshot_changed``.
    """

    if not _selection_fs.supports_descriptor_traversal():
        pytest.skip(_selection_fs.NO_DESCRIPTOR_TRAVERSAL_REASON)

    import tempfile

    from csk.sources import snapshot as snapshot_module
    from csk.sources.snapshot import FrozenFile

    audited_text = case["input"]["audited"]
    mutated_text = case["input"]["before-publication"]
    with tempfile.TemporaryDirectory(prefix="csk-frozen-copy-mutation-") as raw:
        root = Path(raw) / "src"
        home = Path(raw) / "home"
        home.mkdir(parents=True)
        (root / "victim.txt").parent.mkdir(parents=True, exist_ok=True)
        (root / "victim.txt").write_text(audited_text, encoding="utf-8")
        package = snapshot_module.capture_package_snapshot(root, ".", home=home)
        frozen = package.frozen_files()
        assert frozen["victim.txt"].data == audited_text.encode("utf-8")
        frozen["victim.txt"] = FrozenFile(
            "victim.txt", mutated_text.encode("utf-8"), False
        )
        with pytest.raises(source_errors.SourceError) as excinfo:
            snapshot_module.verify_frozen_copy(
                frozen,
                package.inventory["snapshot"],
                equivalent=package.equivalence.equivalent,
            )
        assert excinfo.value.code == case["expected"]


register_semantic_driver("local-git-dirty", _drive_local_git_dirty)
register_semantic_driver("capture-mutation", _drive_capture_mutation)
register_semantic_driver("frozen-copy-mutation", _drive_frozen_copy_mutation)


# Semantic drivers registered by TASK-260917-34g2lq (snapshot store).
#
# Each driver builds a real temporary-filesystem fixture from the case
# input, calls a ``csk.sources`` production entry point, and asserts the
# exact expected outcome. Imports stay function-local so this block
# appends without touching the shared import header.


def _drive_missing_snapshot(case: dict[str, Any]) -> None:
    """Drive ``missing-snapshot`` through every consumer reader.

    Registered by TASK-260917-34g2lq (store): the lock names a snapshot,
    the store holds nothing, and live bytes sit in the authored tree.
    Every consumer (audit, build, projection, install) fails
    ``source_snapshot_unavailable`` and nothing recreates the snapshot
    from the live bytes. No capture runs here, so the driver is
    host-independent.
    """

    import tempfile

    from csk.sources import consumers as consumers_module

    assert case["input"]["locked"]
    assert case["input"]["snapshot_store"] == "absent"
    live_text = case["input"]["live"]
    with tempfile.TemporaryDirectory(prefix="csk-missing-snapshot-") as raw:
        home = Path(raw) / "home"
        live = Path(raw) / "live" / "pkg"
        live.mkdir(parents=True)
        (live / "SKILL.md").write_text(live_text, encoding="utf-8")
        assert {opener.__name__ for opener in consumers_module.ALL_CONSUMERS} == {
            "open_for_audit",
            "open_for_build",
            "open_for_projection",
            "open_for_install",
        }
        for opener in consumers_module.ALL_CONSUMERS:
            with pytest.raises(source_errors.SourceError) as excinfo:
                opener(home, "review", "local:packages/review")
            assert excinfo.value.code == case["expected"]
        assert not (home / "source-v1").exists()
        assert (live / "SKILL.md").read_text(encoding="utf-8") == live_text


register_semantic_driver("missing-snapshot", _drive_missing_snapshot)


# Semantic drivers registered by TASK-260916-15nf0l (install marker v5).
#
# Each driver builds a real temporary-filesystem fixture from the case input,
# calls a ``csk.install_marker`` or ``csk.dev_substitutions`` production entry
# point, and asserts the exact expected outcome. Imports stay function-local
# so this block appends without touching the shared import header. The ten
# attestation-evidence drivers are shared with TASK-260916-11yseo (which owns
# the assurance binding): they assert this leaf's evidence validator, and the
# later leaf extends rather than re-registers them.


def _marker_v5_fixture_kit() -> dict[str, Any]:
    """Import the marker-v5 production surface for one driver call."""

    from csk import dev_substitutions as dev_substitutions_module
    from csk import install_marker as install_marker_module
    from csk.builds.source import BuildSourceIdentity
    from csk.sources import package_identity as source_package_module

    return {
        "dev_substitutions": dev_substitutions_module,
        "install_marker": install_marker_module,
        "BuildSourceIdentity": BuildSourceIdentity,
        "source_package": source_package_module,
    }


_MARKER_V5_SHA1 = "0123456789abcdef0123456789abcdef01234567"
_MARKER_V5_SHA1_OTHER = "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
_MARKER_V5_REPO = "github.com/example/golden-skills"
_MARKER_V5_REPO_OTHER = "github.com/example/other-skills"
_MARKER_V5_LOCK = "sha256:" + "0" * 64
_MARKER_V5_LOCK_OTHER = "sha256:" + "1" * 64
_MARKER_V5_CONTEXT = "sha256:" + "c" * 64
_MARKER_V5_CONTEXT_OTHER = "sha256:" + "d" * 64
_MARKER_V5_CONTENT = "sha256:" + "a" * 64
_MARKER_V5_KEY = "0123456789abcdef"
_MARKER_V5_KEY_OTHER = "fedcba9876543210"


def _marker_v5_plan(kit: dict[str, Any], **changes: Any) -> Any:
    install_marker = kit["install_marker"]
    source_package = kit["source_package"]
    values: dict[str, Any] = {
        "name": "golden-skill",
        "package": source_package.NetworkGit(
            repository=_MARKER_V5_REPO,
            commit=source_package.LockedCommit(
                object_format="sha1", hex=_MARKER_V5_SHA1
            ),
        ),
        "lock_sha256": _MARKER_V5_LOCK,
        "context_sha256": _MARKER_V5_CONTEXT,
        "content_sha256": _MARKER_V5_CONTENT,
        "locale": None,
        "agents": (),
        "commands": (),
        "dependencies": (),
        "skill_schema_version": 8,
        "runtime_roots": (),
        "build_roots": (),
        "files": ("SKILL.md",),
        "builds": {},
        "requirements": None,
        "mcp_servers": None,
        "activation": None,
        "requirers": None,
        "attestation": install_marker.MarkerAttestation(
            registry="trusted", status="audited", key_id=_MARKER_V5_KEY
        ),
        "substituted": None,
        "build_source": kit["BuildSourceIdentity"](
            algorithm="curator-build-source-v1",
            content_sha256="sha256:" + "b" * 64,
        ),
    }
    values.update(changes)
    return install_marker.MarkerPlan(**values)


def _marker_v5_marker(kit: dict[str, Any], **changes: Any) -> Any:
    install_marker = kit["install_marker"]
    source_package = kit["source_package"]
    values: dict[str, Any] = {
        "name": "golden-skill",
        "package": source_package.NetworkGit(
            repository=_MARKER_V5_REPO,
            commit=source_package.LockedCommit(
                object_format="sha1", hex=_MARKER_V5_SHA1
            ),
        ),
        "lock_sha256": _MARKER_V5_LOCK,
        "content_sha256": _MARKER_V5_CONTENT,
        "locale": None,
        "agents": (),
        "commands": (),
        "dependencies": (),
        "skill_schema_version": 8,
        "runtime_roots": (),
        "build_roots": (),
        "installed_at": "2000-01-01T00:00:00Z",
        "files": ("SKILL.md",),
        "builds": {},
        "build_source": kit["BuildSourceIdentity"](
            algorithm="curator-build-source-v1",
            content_sha256="sha256:" + "b" * 64,
        ),
        "attestation": install_marker.MarkerAttestation(
            registry="trusted", status="audited", key_id=_MARKER_V5_KEY
        ),
    }
    values.update(changes)
    return install_marker.InstallMarkerV5(**values)


def _marker_v5_tree_hash(root: Path) -> str:
    import hashlib
    import os
    import stat as stat_module

    digest = hashlib.sha256()
    entries = sorted(
        root.rglob("*"), key=lambda item: item.relative_to(root).as_posix()
    )
    for entry in entries:
        relative = entry.relative_to(root).as_posix().encode("utf-8")
        try:
            info = entry.lstat()
        except FileNotFoundError:
            continue
        if stat_module.S_ISLNK(info.st_mode):
            digest.update(b"L" + relative + os.readlink(entry).encode("utf-8"))
        elif stat_module.S_ISDIR(info.st_mode):
            digest.update(b"D" + relative)
        elif stat_module.S_ISREG(info.st_mode):
            digest.update(b"F" + relative + entry.read_bytes())
        else:
            digest.update(b"S" + relative)
    return digest.hexdigest()


def _marker_v5_mismatch_kwargs(field: str, kit: dict[str, Any]) -> dict[str, Any]:
    install_marker = kit["install_marker"]
    source_package = kit["source_package"]
    if field == "registry":
        return {
            "attestation": install_marker.MarkerAttestation(
                registry="elsewhere", status="audited", key_id=_MARKER_V5_KEY
            )
        }
    if field == "status":
        return {
            "attestation": install_marker.MarkerAttestation(
                registry="trusted", status="deprecated", key_id=_MARKER_V5_KEY
            )
        }
    if field == "key_id":
        return {
            "attestation": install_marker.MarkerAttestation(
                registry="trusted", status="audited", key_id=_MARKER_V5_KEY_OTHER
            )
        }
    if field == "substituted":
        return {"substituted": "another-operator"}
    if field == "package":
        return {
            "package": source_package.NetworkGit(
                repository=_MARKER_V5_REPO_OTHER,
                commit=source_package.LockedCommit(
                    object_format="sha1", hex=_MARKER_V5_SHA1
                ),
            )
        }
    if field == "lock_sha256":
        return {"lock_sha256": _MARKER_V5_LOCK_OTHER}
    raise AssertionError(f"no mismatch fixture for {field!r}")


def _drive_marker_plan_mismatch(case: dict[str, Any]) -> None:
    """Drive one ``marker-plan-mismatch-*`` case through status evaluation."""

    import tempfile

    kit = _marker_v5_fixture_kit()
    install_marker = kit["install_marker"]
    field = case["input"]["marker_field"]
    assert case["input"]["effective_plan"] == "different"
    assert case["input"]["operation"] == "status"
    assert case["expected"] == "noncurrent-nonzero-no-mutation"
    with tempfile.TemporaryDirectory(prefix="csk-marker-mismatch-") as raw:
        root = Path(raw)
        csk_home = root / "csk-home"
        project = root / "project"
        (csk_home / "source-v1").mkdir(parents=True)
        (csk_home / "source-v1" / "record.json").write_text("{}")
        skills = project / ".agents" / "skills" / "golden-skill"
        skills.mkdir(parents=True)
        (skills / "SKILL.md").write_text("# golden\n")
        plan = _marker_v5_plan(kit)
        marker = _marker_v5_marker(kit, **_marker_v5_mismatch_kwargs(field, kit))
        marker_path = skills / ".csk-install.json"
        marker_path.write_bytes(install_marker.serialize_install_marker(marker.to_json()))
        assert install_marker.compare_marker_plan(marker, plan) == (field,)
        before_home = _marker_v5_tree_hash(csk_home)
        before_project = _marker_v5_tree_hash(project)
        verdict = install_marker.evaluate_marker_status(marker_path, plan)
        assert verdict.current is False
        assert verdict.exit_code != 0
        assert verdict.differences == (field,)
        assert _marker_v5_tree_hash(csk_home) == before_home
        assert _marker_v5_tree_hash(project) == before_project


for _mismatch_case_id in (
    "marker-plan-mismatch-registry",
    "marker-plan-mismatch-status",
    "marker-plan-mismatch-key_id",
    "marker-plan-mismatch-substituted",
    "marker-plan-mismatch-package",
    "marker-plan-mismatch-lock_sha256",
):
    register_semantic_driver(_mismatch_case_id, _drive_marker_plan_mismatch)


def _marker_v5_evidence_payload(**changes: Any) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "name": "golden-skill",
        "repository": _MARKER_V5_REPO,
        "commit": {"object_format": "sha1", "hex": _MARKER_V5_SHA1},
        "context_sha256": _MARKER_V5_CONTEXT,
        "key_id": _MARKER_V5_KEY,
    }
    payload.update(changes)
    return payload


def _drive_attestation_evidence(case: dict[str, Any]) -> None:
    """Drive one ``attestation-evidence-*`` case through the one validator."""

    import tempfile
    from unittest.mock import patch as mock_patch

    kit = _marker_v5_fixture_kit()
    install_marker = kit["install_marker"]
    source_package = kit["source_package"]
    defect = case["input"]["required_registry_evidence"]
    assert case["input"]["marker_summary"] == "apparently-valid"
    assert case["input"]["operations"] == ["install", "status", "repair", "refresh"]
    assert (
        case["expected"]
        == "install-repair-refresh-refuse-preserve-prior-state;status-noncurrent-or-unknown-nonzero-no-mutation"
    )
    expectation = install_marker.AttestationExpectation(
        name="golden-skill",
        repository=_MARKER_V5_REPO,
        commit=source_package.LockedCommit(object_format="sha1", hex=_MARKER_V5_SHA1),
        context_sha256=_MARKER_V5_CONTEXT,
        key_id=_MARKER_V5_KEY,
    )
    expected_codes = {
        "absent": "attestation_evidence_missing",
        "unreadable": "attestation_evidence_unreadable",
        "malformed": "attestation_evidence_malformed",
        "stale": "attestation_evidence_stale",
        "revoked": "attestation_evidence_revoked",
        "wrong-name": "attestation_evidence_mismatch",
        "wrong-repository": "attestation_evidence_mismatch",
        "wrong-commit": "attestation_evidence_mismatch",
        "wrong-context": "attestation_evidence_mismatch",
        "wrong-key": "attestation_evidence_mismatch",
    }
    mutations: dict[str, Any] = {
        "wrong-name": {"name": "other-skill"},
        "wrong-repository": {"repository": _MARKER_V5_REPO_OTHER},
        "wrong-commit": {
            "commit": {"object_format": "sha1", "hex": _MARKER_V5_SHA1_OTHER}
        },
        "wrong-context": {"context_sha256": _MARKER_V5_CONTEXT_OTHER},
        "wrong-key": {"key_id": _MARKER_V5_KEY_OTHER},
    }
    with tempfile.TemporaryDirectory(prefix="csk-attestation-evidence-") as raw:
        root = Path(raw)
        csk_home = root / "csk-home"
        project = root / "project"
        csk_home.mkdir()
        project.mkdir()
        (csk_home / "lock.json").write_text("{}")
        (project / "Skillfile.json").write_text("{}")
        evidence_path = project / "evidence.json"
        fresh = defect != "stale"
        revoked = defect == "revoked"
        if defect == "malformed":
            evidence_path.write_bytes(b"{oops")
        elif defect != "absent":
            payload = _marker_v5_evidence_payload(**mutations.get(defect, {}))
            evidence_path.write_bytes(json.dumps(payload).encode("utf-8"))
        before_home = _marker_v5_tree_hash(csk_home)
        before_project = _marker_v5_tree_hash(project)
        # Install, repair and refresh refuse through the one validator.
        if defect == "unreadable":
            original_read_bytes = Path.read_bytes

            def _refuse(self: Path) -> bytes:
                if self == evidence_path:
                    raise PermissionError("evidence store denied the read")
                return original_read_bytes(self)

            with mock_patch.object(Path, "read_bytes", _refuse):
                with pytest.raises(install_marker.InstallMarkerError) as refused:
                    install_marker.validate_attestation_evidence(
                        evidence_path,
                        expectation,
                        evidence_fresh=fresh,
                        evidence_revoked=revoked,
                    )
        else:
            with pytest.raises(install_marker.InstallMarkerError) as refused:
                install_marker.validate_attestation_evidence(
                    evidence_path,
                    expectation,
                    evidence_fresh=fresh,
                    evidence_revoked=revoked,
                )
        assert refused.value.code == expected_codes[defect]
        assert _marker_v5_tree_hash(csk_home) == before_home
        assert _marker_v5_tree_hash(project) == before_project
        # Status is non-current or unknown, nonzero, and read-only.
        plan = _marker_v5_plan(kit)
        skills = project / ".agents" / "skills" / "golden-skill"
        skills.mkdir(parents=True)
        marker_path = skills / ".csk-install.json"
        marker_path.write_bytes(
            install_marker.serialize_install_marker(
                _marker_v5_marker(kit).to_json()
            )
        )
        assert install_marker.compare_marker_plan(
            _marker_v5_marker(kit), plan
        ) == ()
        before_home = _marker_v5_tree_hash(csk_home)
        before_project = _marker_v5_tree_hash(project)
        if defect == "unreadable":
            with mock_patch.object(Path, "read_bytes", _refuse):
                verdict = install_marker.evaluate_schema2_status(
                    marker_path,
                    plan,
                    evidence_path=evidence_path,
                    evidence_fresh=fresh,
                    evidence_revoked=revoked,
                )
        else:
            verdict = install_marker.evaluate_schema2_status(
                marker_path,
                plan,
                evidence_path=evidence_path,
                evidence_fresh=fresh,
                evidence_revoked=revoked,
            )
        assert verdict.current is False
        assert verdict.exit_code != 0
        assert _marker_v5_tree_hash(csk_home) == before_home
        assert _marker_v5_tree_hash(project) == before_project


for _evidence_case_id in (
    "attestation-evidence-absent",
    "attestation-evidence-unreadable",
    "attestation-evidence-malformed",
    "attestation-evidence-stale",
    "attestation-evidence-revoked",
    "attestation-evidence-wrong-name",
    "attestation-evidence-wrong-repository",
    "attestation-evidence-wrong-commit",
    "attestation-evidence-wrong-context",
    "attestation-evidence-wrong-key",
):
    register_semantic_driver(_evidence_case_id, _drive_attestation_evidence)


def _drive_attested_network_current(case: dict[str, Any]) -> None:
    """Drive ``attested-network-current`` through the full status entry."""

    import tempfile

    kit = _marker_v5_fixture_kit()
    install_marker = kit["install_marker"]
    assert case["input"]["package"] == "network-git"
    assert (
        case["input"]["signed_record"]
        == "valid-fresh-exact-name-repository-commit-context"
    )
    assert case["input"]["marker_attestation"] == "matches-registry-status-key"
    assert case["input"]["operation"] == "status"
    assert case["expected"] == "current-if-all-other-gates-pass"
    with tempfile.TemporaryDirectory(prefix="csk-attested-current-") as raw:
        root = Path(raw)
        plan = _marker_v5_plan(kit)
        marker_path = root / ".csk-install.json"
        marker_path.write_bytes(
            install_marker.serialize_install_marker(_marker_v5_marker(kit).to_json())
        )
        evidence_path = root / "evidence.json"
        evidence_path.write_bytes(
            json.dumps(_marker_v5_evidence_payload()).encode("utf-8")
        )
        verdict = install_marker.evaluate_schema2_status(
            marker_path,
            plan,
            evidence_path=evidence_path,
            evidence_fresh=True,
            evidence_revoked=False,
        )
        assert verdict.current is True
        assert verdict.exit_code == 0


register_semantic_driver("attested-network-current", _drive_attested_network_current)


def _drive_legacy_substitution_current(case: dict[str, Any]) -> None:
    """Drive ``legacy-substitution-current`` through admission plus comparison."""

    import tempfile

    kit = _marker_v5_fixture_kit()
    dev_substitutions = kit["dev_substitutions"]
    install_marker = kit["install_marker"]
    source_package = kit["source_package"]
    assert case["input"]["selector"] == "legacy"
    assert case["input"]["strict_audit"] is False
    assert case["input"]["effective_committed_package"] == "matches-lock"
    assert case["input"]["marker_substituted"] == "matches-operator-identifier"
    assert case["expected"] == "current-if-all-other-gates-pass"
    commit = source_package.LockedCommit(object_format="sha1", hex=_MARKER_V5_SHA1)
    package = source_package.ConfiguredGit(source="golden-skill", commit=commit)
    with tempfile.TemporaryDirectory(prefix="csk-legacy-substitution-") as raw:
        root = Path(raw)
        dev_substitutions.check_source_substitution_admission(
            selector_kind="legacy",
            operator_substitution=True,
            strict_audit=False,
        )
        plan = _marker_v5_plan(
            kit, package=package, attestation=None, substituted="operator-development"
        )
        marker = _marker_v5_marker(
            kit, package=package, attestation=None, substituted="operator-development"
        )
        assert install_marker.compare_marker_plan(marker, plan) == ()
        marker_path = root / ".csk-install.json"
        marker_path.write_bytes(
            install_marker.serialize_install_marker(marker.to_json())
        )
        verdict = install_marker.evaluate_marker_status(marker_path, plan)
        assert verdict.current is True
        assert verdict.exit_code == 0


register_semantic_driver(
    "legacy-substitution-current", _drive_legacy_substitution_current
)


def _drive_legacy_substitution_strict(case: dict[str, Any]) -> None:
    """Drive ``legacy-substitution-strict`` through the planning gate."""

    import sys as sys_module

    kit = _marker_v5_fixture_kit()
    dev_substitutions = kit["dev_substitutions"]
    assert case["input"]["selector"] == "legacy"
    assert case["input"]["strict_audit"] is True
    assert case["input"]["marker_substituted"] == "absent"
    assert case["input"]["operator_substitution"] == "present"
    assert case["expected"] == "reject-before-cache-compiler-publication"
    events: list[str] = []
    state = {"armed": True}

    def hook(event: str, args: object) -> None:
        if state["armed"] and (event == "open" or event.startswith("socket.")):
            events.append(event)

    sys_module.addaudithook(hook)
    try:
        with pytest.raises(dev_substitutions.DevSubstitutionError) as refused:
            dev_substitutions.check_source_substitution_admission(
                selector_kind="legacy",
                operator_substitution=True,
                strict_audit=True,
            )
    finally:
        state["armed"] = False
    assert "strict audit" in str(refused.value)
    assert events == []


register_semantic_driver(
    "legacy-substitution-strict", _drive_legacy_substitution_strict
)


def _drive_selector_substitution_forbidden(case: dict[str, Any]) -> None:
    """Drive ``selector-substitution-forbidden``: reject with no publication."""

    import tempfile

    kit = _marker_v5_fixture_kit()
    dev_substitutions = kit["dev_substitutions"]
    assert case["input"]["selector"] == "from"
    assert case["input"]["operator_substitution"] == "present"
    assert case["expected"] == "reject-no-publication"
    with tempfile.TemporaryDirectory(prefix="csk-selector-substitution-") as raw:
        root = Path(raw)
        csk_home = root / "csk-home"
        project = root / "project"
        csk_home.mkdir()
        project.mkdir()
        (csk_home / "lock.json").write_text("{}")
        (project / "Skillfile.json").write_text("{}")
        before_home = _marker_v5_tree_hash(csk_home)
        before_project = _marker_v5_tree_hash(project)
        with pytest.raises(dev_substitutions.DevSubstitutionError) as refused:
            dev_substitutions.check_source_substitution_admission(
                selector_kind="from",
                operator_substitution=True,
                strict_audit=False,
            )
        assert "forbidden" in str(refused.value)
        assert _marker_v5_tree_hash(csk_home) == before_home
        assert _marker_v5_tree_hash(project) == before_project


register_semantic_driver(
    "selector-substitution-forbidden", _drive_selector_substitution_forbidden
)


def _drive_local_required_registry(case: dict[str, Any]) -> None:
    """Drive ``local-required-registry``: fail with no publication."""

    import tempfile

    kit = _marker_v5_fixture_kit()
    install_marker = kit["install_marker"]
    source_package = kit["source_package"]
    assert case["input"]["package"] == "local-snapshot"
    assert case["input"]["policy"] == "requires-network-attestation"
    assert case["expected"] == "reject-no-publication"
    with tempfile.TemporaryDirectory(prefix="csk-local-registry-") as raw:
        root = Path(raw)
        csk_home = root / "csk-home"
        project = root / "project"
        csk_home.mkdir()
        project.mkdir()
        (csk_home / "lock.json").write_text("{}")
        (project / "Skillfile.json").write_text("{}")
        before_home = _marker_v5_tree_hash(csk_home)
        before_project = _marker_v5_tree_hash(project)
        with pytest.raises(install_marker.InstallMarkerError) as refused:
            install_marker.check_local_registry_requirement(
                source_package.LocalSnapshot(snapshot="sha256:" + "1" * 64),
                network_attestation_required=True,
            )
        assert refused.value.code == "local_registry_attestation_required"
        assert _marker_v5_tree_hash(csk_home) == before_home
        assert _marker_v5_tree_hash(project) == before_project


register_semantic_driver("local-required-registry", _drive_local_required_registry)


def _drive_external_substitution_strict(case: dict[str, Any]) -> None:
    """Drive ``external-substitution-strict`` through the planning gate."""

    import sys as sys_module

    kit = _marker_v5_fixture_kit()
    dev_substitutions = kit["dev_substitutions"]
    assert case["input"]["package"] == "local-snapshot"
    assert case["input"]["external_substituted"] is True
    assert case["input"]["strict_audit"] is True
    assert case["expected"] == "reject-before-cache-compiler-publication"
    events: list[str] = []
    state = {"armed": True}

    def hook(event: str, args: object) -> None:
        if state["armed"] and (event == "open" or event.startswith("socket.")):
            events.append(event)

    sys_module.addaudithook(hook)
    try:
        with pytest.raises(dev_substitutions.DevSubstitutionError) as refused:
            dev_substitutions.check_source_substitution_admission(
                selector_kind="legacy",
                operator_substitution=False,
                external_substitution=True,
                strict_audit=True,
            )
    finally:
        state["armed"] = False
    assert "strict audit" in str(refused.value)
    assert events == []


register_semantic_driver(
    "external-substitution-strict", _drive_external_substitution_strict
)


def _drive_external_only_current(case: dict[str, Any]) -> None:
    """Drive ``external-only-current`` through the build-state check."""

    import tempfile

    from csk.builds import currentness as build_currentness
    from csk.builds import metadata as build_metadata

    kit = _marker_v5_fixture_kit()
    install_marker = kit["install_marker"]
    source_package = kit["source_package"]
    assert case["input"]["package"] == "local-snapshot"
    assert case["input"]["top_level_build_source"] == "absent"
    assert case["input"]["external_record"] == "complete-matches-receipt3-input.build"
    assert case["input"]["receipt_package"] == "matches-marker"
    assert case["input"]["protected_artifact"] == "verified"
    assert case["expected"] == "current-if-all-other-gates-pass"
    with tempfile.TemporaryDirectory(prefix="csk-external-only-") as raw:
        root = Path(raw)
        package = source_package.LocalSnapshot(snapshot=_RECEIPT_V3_PACKAGE)
        # The positive control is genuine end to end: a production install
        # publishes the receipt-3 bytes, the protected artifact, and the
        # cache key, and the record mirrors that evidence exactly. This is
        # the control that keeps the seventeen refusals honest — it passes
        # one mutation away from every case that must refuse.
        store, _, genuine = _receipt_v3_install(root, package, substituted=True)
        assert genuine.receipt is not None and genuine.artifact is not None
        wrapped = build_metadata.read_receipt_v3(genuine.receipt).input
        record = _receipt_v3_genuine_record(kit, genuine, package)
        assert (
            build_currentness.compare_external_build_evidence(
                record,
                wrapped,
                marker_package=package,
                receipt_bytes=genuine.receipt,
                artifact_bytes=genuine.artifact,
            )
            == ()
        )
        hit = store.lookup_receipt_v3(
            genuine.cache_key, wrapped.to_json(), mutate=False
        )
        assert hit is not None
        assert hit.artifact == genuine.artifact
        assert hit.receipt == genuine.receipt
        plan = _marker_v5_plan(
            kit,
            package=package,
            attestation=None,
            build_source=None,
            commands=("tool",),
            builds={"tool": record},
        )
        marker = _marker_v5_marker(
            kit,
            package=package,
            attestation=None,
            builds={"tool": record},
            build_source=None,
            commands=("tool",),
        )
        install_marker.check_external_only_build_state(
            marker, receipt_package=wrapped.package
        )
        assert install_marker.compare_marker_plan(marker, plan) == ()
        marker_path = root / ".csk-install.json"
        marker_path.write_bytes(
            install_marker.serialize_install_marker(marker.to_json())
        )
        verdict = install_marker.evaluate_marker_status(marker_path, plan)
        assert verdict.current is True
        assert verdict.exit_code == 0


register_semantic_driver("external-only-current", _drive_external_only_current)


@pytest.mark.parametrize(
    "entry",
    [
        entry
        for entry in SCHEMA_CASES
        if entry["schema"] == "install-marker-v5.schema.json"
    ],
    ids=[
        f"{entry['schema']}:{entry['instance']}"
        for entry in SCHEMA_CASES
        if entry["schema"] == "install-marker-v5.schema.json"
    ],
)
def test_draft_sources_install_marker_v5_schema_case_through_production_reader(
    entry: dict[str, Any],
) -> None:
    """Drive every marker-v5 schema case through csk's production reader.

    Registered by TASK-260916-15nf0l: valid fixtures must parse to the v5
    model and reach a canonical read-write fixpoint with stable values (the
    pinned fixtures are not key-sorted, so the fixpoint, not fixture bytes,
    is the byte-identity property); invalid fixtures must refuse, including
    the narrowed local attestation/substituted cases and every external
    required-field negative.
    """

    from csk import install_marker as install_marker_module

    raw = (_suite_root() / Path(entry["instance"])).read_bytes()
    if entry["valid"]:
        parsed = install_marker_module.read_install_marker(raw)
        assert isinstance(parsed, install_marker_module.InstallMarkerV5)
        assert parsed.schema_version == 5
        once = install_marker_module.serialize_install_marker(parsed.to_json())
        twice = install_marker_module.serialize_install_marker(
            install_marker_module.read_install_marker(once).to_json()
        )
        assert once == twice
        assert (
            install_marker_module.read_install_marker(once).to_json()
            == parsed.to_json()
        )
    else:
        with pytest.raises(install_marker_module.InstallMarkerError):
            install_marker_module.read_install_marker(raw)


# --- Drivers registered by TASK-260916-341a6q (source-aware build receipts) ---
#
# The seventeen ``external-evidence-mismatch-*`` cases are one declared field
# table driving one comparison: registration below derives the case ids from
# the production table, and the single driver builds the positive control
# (matching receipt-3 hashes and protected artifacts) before applying each
# mutation, then asserts the exact field-level refusal, the non-current
# nonzero status, and a repair that revalidates the exact source and rebuilds
# rather than adopting the record.

_RECEIPT_V3_COMMIT = "0123456789abcdef0123456789abcdef01234567"
_RECEIPT_V3_COMMIT_OTHER = "abcdef0123456789abcdef0123456789abcdef01"
_RECEIPT_V3_REVISION = "1111111111111111111111111111111111111111"
_RECEIPT_V3_REVISION_OTHER = "2222222222222222222222222222222222222222"
_RECEIPT_V3_PACKAGE = "sha256:" + "1" * 64
_RECEIPT_V3_PACKAGE_OTHER = "sha256:" + "2" * 64


def _receipt_v3_snapshot() -> Any:
    from csk.git_admission import Snapshot, SnapshotFile

    files = tuple(
        sorted(
            (
                SnapshotFile("repo/go.mod", b"module example.test/tool\n\ngo 1.25\n"),
                SnapshotFile(
                    "repo/cmd/tool/main.go", b"package main\nfunc main() {}\n"
                ),
                SnapshotFile(
                    "skill-build.json",
                    protocol_json.canonical_bytes(
                        {
                            "schema_version": 1,
                            "targets": {
                                "tool": {
                                    "driver": "go-repository-v1",
                                    "build_root": "repo",
                                    "source_dir": "repo/cmd/tool",
                                }
                            },
                        }
                    ),
                ),
            ),
            key=lambda item: item.path,
        )
    )
    framed = bytearray(b"curator-build-source-v1\0")
    for item in files:
        path = item.path.encode()
        framed.extend(b"F")
        framed.extend(len(path).to_bytes(8, "big"))
        framed.extend(path)
        framed.extend(len(item.content).to_bytes(8, "big"))
        framed.extend(item.content)
    canonical = bytes(framed)
    return Snapshot(
        object_format="sha1",
        commit=_RECEIPT_V3_COMMIT,
        files=files,
        canonical_bytes=canonical,
        digest="sha256:" + hashlib.sha256(canonical).hexdigest(),
        tag_verified=True,
    )


class _ReceiptV3Compiler:
    """A deterministic fake Go compiler with an invocation counter."""

    def __init__(self) -> None:
        from csk.build_repository_pipeline import CompilerIdentity

        self.calls = 0
        self.identity = CompilerIdentity(
            content_sha256="sha256:" + "c" * 64,
            go_version="go version go1.26.1 darwin/arm64",
            go_relpath="bin/go",
            goos="darwin",
            goarch="arm64",
            tuning={"GOARM64": "v8.0"},
        )

    def compile(self, root: Path, source_dir: str, command: str) -> bytes:
        self.calls += 1
        assert command == "tool"
        return b"compiled-tool"


def _receipt_v3_install(
    root: Path, package: Any, *, substituted: bool
) -> tuple[Any, Any, Any]:
    from csk.build_repository_pipeline import (
        DeclaredState,
        DiskProtectedStore,
        EffectiveState,
        Operation,
        PipelineRequest,
        SubstitutionState,
        run_pipeline,
    )

    store = DiskProtectedStore(root / "store")
    compiler = _ReceiptV3Compiler()
    snapshot = _receipt_v3_snapshot()
    declared = DeclaredState(
        repository="tools",
        identity="github.com/example/tools",
        transport="https",
        object_format="sha1",
        commit=_RECEIPT_V3_COMMIT,
        tag="v1.0.0",
    )
    if substituted:
        effective = EffectiveState(
            identity_kind="network-git",
            identity="github.com/example/tools",
            transport="https",
            object_format="sha1",
            commit=_RECEIPT_V3_COMMIT,
            substituted=True,
            substitution=SubstitutionState(
                type="network-git",
                ref_kind="revision",
                ref_value=_RECEIPT_V3_REVISION,
            ),
        )
    else:
        effective = EffectiveState(
            identity_kind="network-git",
            identity="github.com/example/tools",
            transport="https",
            object_format="sha1",
            commit=_RECEIPT_V3_COMMIT,
        )
    result = run_pipeline(
        PipelineRequest(
            operation=Operation.INSTALL,
            command="tool",
            target="tool",
            declared=declared,
            effective=effective,
            acquire=lambda: snapshot,
            audit=lambda subject: None,
            store=store,
            compiler=compiler,
            package=package,
        )
    )
    assert result.receipt is not None and result.artifact is not None
    return store, compiler, result


def _receipt_v3_genuine_record(kit: dict[str, Any], result: Any, package: Any) -> Any:
    from csk.builds import metadata as build_metadata

    install_marker = kit["install_marker"]
    receipt = build_metadata.read_receipt_v3(result.receipt)
    build = receipt.input.build
    assert receipt.input.package == package
    declared = build.source.declared
    effective = build.source.effective
    substitution = effective.substitution
    return install_marker.InstallMarkerBuildV5(
        driver="go-repository-v1",
        receipt_schema_version=3,
        execution_policy="manager-worker-v1",
        cache_key=result.cache_key,
        receipt_sha256=build_metadata.receipt_sha256(result.receipt),
        artifact_sha256="sha256:" + hashlib.sha256(result.artifact).hexdigest(),
        artifact_path=build.artifact_path,
        repository=build.source.repository,
        declared_identity=install_marker.MarkerRepositoryIdentity(
            kind=declared.identity.kind, value=declared.identity.value
        ),
        declared_locked_commit=install_marker.MarkerRepositoryCommit(
            object_format=declared.locked_commit.object_format,
            hex=declared.locked_commit.hex,
        ),
        declared_tag=declared.tag,
        effective_identity=install_marker.MarkerRepositoryIdentity(
            kind=effective.identity.kind, value=effective.identity.value
        ),
        object_format=effective.object_format,
        commit=effective.commit,
        substituted=effective.substituted,
        substitution=(
            None
            if substitution is None
            else install_marker.MarkerRepositorySubstitution(
                type=substitution.type,
                ref=(
                    None
                    if substitution.ref is None
                    else install_marker.MarkerRepositoryRef(
                        kind=substitution.ref.kind,
                        value=substitution.ref.value,
                    )
                ),
            )
        ),
        build_source=effective.build_source,
        descriptor_target=build.source.descriptor.target,
    )


def _receipt_v3_mutate_record(
    kit: dict[str, Any], record: Any, field: str
) -> Any:
    from dataclasses import replace

    install_marker = kit["install_marker"]
    if field == "repository":
        return replace(record, repository="other-tools")
    if field == "declared_identity":
        return replace(
            record,
            declared_identity=install_marker.MarkerRepositoryIdentity(
                kind="network-git", value="github.com/example/other"
            ),
        )
    if field == "declared_locked_commit":
        return replace(
            record,
            declared_locked_commit=install_marker.MarkerRepositoryCommit(
                object_format="sha1", hex=_RECEIPT_V3_COMMIT_OTHER
            ),
        )
    if field == "declared_tag":
        return replace(record, declared_tag="v2.0.0")
    if field == "effective_identity":
        return replace(
            record,
            effective_identity=install_marker.MarkerRepositoryIdentity(
                kind="network-git", value="github.com/example/mirror"
            ),
        )
    if field == "commit":
        return replace(record, commit=_RECEIPT_V3_COMMIT_OTHER)
    if field == "substitution":
        return replace(
            record,
            substitution=install_marker.MarkerRepositorySubstitution(
                type="network-git",
                ref=install_marker.MarkerRepositoryRef(
                    kind="revision", value=_RECEIPT_V3_REVISION_OTHER
                ),
            ),
        )
    if field == "build_source":
        return replace(
            record,
            build_source=kit["BuildSourceIdentity"](
                algorithm="curator-build-source-v1",
                content_sha256="sha256:" + "9" * 64,
            ),
        )
    if field == "descriptor_target":
        return replace(record, descriptor_target="other-target")
    if field == "cache_key":
        return replace(record, cache_key="sha256:" + "f" * 64)
    if field == "receipt_sha256":
        return replace(record, receipt_sha256="sha256:" + "e" * 64)
    if field == "artifact_sha256":
        return replace(record, artifact_sha256="sha256:" + "d" * 64)
    if field == "artifact_path":
        return replace(record, artifact_path="bin/other-tool")
    raise AssertionError(f"no single-field mutation for {field!r}")


def _drive_external_evidence_mismatch(case: dict[str, Any]) -> None:
    """Drive one ``external-evidence-mismatch-*`` case through production."""

    import tempfile
    from dataclasses import replace

    from csk.builds import currentness as build_currentness
    from csk.builds import metadata as build_metadata
    from csk.build_repository_pipeline import (
        DeclaredState,
        DiskProtectedStore,
        EffectiveState,
        Operation,
        PipelineRequest,
        SubstitutionState,
        run_pipeline,
    )

    kit = _marker_v5_fixture_kit()
    install_marker = kit["install_marker"]
    source_package = kit["source_package"]
    field = case["input"]["marker_receipt_or_plan_field"]
    assert case["input"]["comparison"] == "mismatch"
    assert case["input"]["operations"] == ["status", "repair"]
    assert case["input"]["package"] == "local-snapshot"
    assert (
        case["expected"]
        == "status-noncurrent-nonzero;repair-revalidate-exact-source-rebuild-not-adopt"
    )
    assert field in build_currentness.EXTERNAL_EVIDENCE_FIELDS
    package = source_package.LocalSnapshot(snapshot=_RECEIPT_V3_PACKAGE)
    with tempfile.TemporaryDirectory(prefix="csk-evidence-mismatch-") as raw:
        root = Path(raw)
        # The positive control first: a genuine install whose record agrees
        # with the receipt-3 evidence on every compared field, so the refusal
        # below is one mutation away from a case that genuinely passes.
        _, _, genuine = _receipt_v3_install(
            root, package, substituted=(field != "substituted")
        )
        record = _receipt_v3_genuine_record(kit, genuine, package)
        assert genuine.receipt is not None and genuine.artifact is not None
        wrapped = build_metadata.read_receipt_v3(genuine.receipt).input
        assert (
            build_currentness.compare_external_build_evidence(
                record,
                wrapped,
                marker_package=package,
                receipt_bytes=genuine.receipt,
                artifact_bytes=genuine.artifact,
            )
            == ()
        )
        if field == "substituted":
            mutated = replace(
                record,
                substituted=True,
                substitution=install_marker.MarkerRepositorySubstitution(
                    type="network-git",
                    ref=install_marker.MarkerRepositoryRef(
                        kind="revision", value=_RECEIPT_V3_REVISION
                    ),
                ),
            )
            differing = build_currentness.compare_external_build_evidence(
                mutated,
                wrapped,
                marker_package=package,
                receipt_bytes=genuine.receipt,
                artifact_bytes=genuine.artifact,
            )
            assert "substituted" in differing
            assert "substitution" in differing
        elif field == "object_format":
            mutated = replace(
                record,
                object_format="sha256",
                commit="ab" * 32,
                substitution=install_marker.MarkerRepositorySubstitution(
                    type="network-git",
                    ref=install_marker.MarkerRepositoryRef(
                        kind="revision", value="cd" * 32
                    ),
                ),
            )
            differing = build_currentness.compare_external_build_evidence(
                mutated,
                wrapped,
                marker_package=package,
                receipt_bytes=genuine.receipt,
                artifact_bytes=genuine.artifact,
            )
            assert "object_format" in differing
            assert "commit" in differing
        elif field == "execution_policy":
            # No second valid execution policy exists: any differing value
            # refuses at construction, and the status below proves the
            # resulting marker unreadable rather than current.
            with pytest.raises(install_marker.InstallMarkerError):
                replace(record, execution_policy="arbitrary")
            mutated = None
        elif field == "input.package":
            other_wrapped = build_metadata.wrap_receipt_v3_input(
                source_package.LocalSnapshot(snapshot=_RECEIPT_V3_PACKAGE_OTHER),
                wrapped.build,
            )
            differing = build_currentness.compare_external_build_evidence(
                record,
                other_wrapped,
                marker_package=package,
                receipt_bytes=genuine.receipt,
                artifact_bytes=genuine.artifact,
            )
            assert set(differing) == {"input.package", "cache_key"}
            mutated = None
        else:
            mutated = _receipt_v3_mutate_record(kit, record, field)
            assert (
                build_currentness.compare_external_build_evidence(
                    mutated,
                    wrapped,
                    marker_package=package,
                    receipt_bytes=genuine.receipt,
                    artifact_bytes=genuine.artifact,
                )
                == (field,)
            )
        # Status: non-current with a nonzero exit, and read-only.
        csk_home = root / "csk-home"
        project = root / "project"
        (csk_home / "source-v1").mkdir(parents=True)
        (csk_home / "source-v1" / "record.json").write_text("{}")
        skills = project / ".agents" / "skills" / "golden-skill"
        skills.mkdir(parents=True)
        (skills / "SKILL.md").write_text("# golden\n")
        marker_path = skills / ".csk-install.json"
        if field == "input.package":
            plan = _marker_v5_plan(
                kit,
                package=source_package.LocalSnapshot(
                    snapshot=_RECEIPT_V3_PACKAGE_OTHER
                ),
                attestation=None,
                build_source=None,
                commands=("tool",),
                builds={"tool": record},
            )
            marker = _marker_v5_marker(
                kit,
                package=package,
                attestation=None,
                builds={"tool": record},
                build_source=None,
                commands=("tool",),
            )
            marker_path.write_bytes(
                install_marker.serialize_install_marker(marker.to_json())
            )
            assert install_marker.compare_marker_plan(marker, plan) == ("package",)
        elif field in {"execution_policy", "artifact_path"}:
            plan = _marker_v5_plan(
                kit,
                package=package,
                attestation=None,
                build_source=None,
                commands=("tool",),
                builds={"tool": record},
            )
            marker = _marker_v5_marker(
                kit,
                package=package,
                attestation=None,
                builds={"tool": record},
                build_source=None,
                commands=("tool",),
            )
            payload = marker.to_json()
            if field == "execution_policy":
                payload["builds"]["tool"]["execution_policy"] = "arbitrary"
            else:
                payload["builds"]["tool"]["artifact_path"] = "bin/other-tool"
            marker_path.write_bytes(
                install_marker.serialize_install_marker(payload)
            )
            with pytest.raises(install_marker.InstallMarkerError):
                install_marker.read_install_marker(marker_path.read_bytes())
        else:
            assert mutated is not None
            plan = _marker_v5_plan(
                kit,
                package=package,
                attestation=None,
                build_source=None,
                commands=("tool",),
                builds={"tool": record},
            )
            marker = _marker_v5_marker(
                kit,
                package=package,
                attestation=None,
                builds={"tool": mutated},
                build_source=None,
                commands=("tool",),
            )
            marker_path.write_bytes(
                install_marker.serialize_install_marker(marker.to_json())
            )
            assert install_marker.compare_marker_plan(marker, plan) == ("builds",)
        before_home = _marker_v5_tree_hash(csk_home)
        before_project = _marker_v5_tree_hash(project)
        verdict = install_marker.evaluate_marker_status(marker_path, plan)
        assert verdict.current is False
        assert verdict.exit_code != 0
        assert _marker_v5_tree_hash(csk_home) == before_home
        assert _marker_v5_tree_hash(project) == before_project
        # Repair: revalidate the exact locked source and rebuild the exact
        # receipt rather than adopting the mutated record.
        repair_store = DiskProtectedStore(root / "repair-store")
        compiler = _ReceiptV3Compiler()
        snapshot = _receipt_v3_snapshot()
        observed: list[str] = []
        repair_events: list[str] = []
        real_repair_lookup = repair_store.lookup_receipt_v3

        def counting_repair_lookup(*args: Any, **kwargs: Any) -> Any:
            repair_events.append("cache")
            return real_repair_lookup(*args, **kwargs)

        repair_store.lookup_receipt_v3 = counting_repair_lookup  # type: ignore[method-assign]

        def acquire() -> Any:
            observed.append(snapshot.commit)
            repair_events.append("acquire")
            return snapshot

        def audit(subject: Any) -> None:
            repair_events.append("audit")

        repaired = run_pipeline(
            PipelineRequest(
                operation=Operation.REPAIR,
                command="tool",
                target="tool",
                declared=DeclaredState(
                    repository="tools",
                    identity="github.com/example/tools",
                    transport="https",
                    object_format="sha1",
                    commit=_RECEIPT_V3_COMMIT,
                    tag="v1.0.0",
                ),
                effective=(
                    EffectiveState(
                        identity_kind="network-git",
                        identity="github.com/example/tools",
                        transport="https",
                        object_format="sha1",
                        commit=_RECEIPT_V3_COMMIT,
                    )
                    if field == "substituted"
                    else EffectiveState(
                        identity_kind="network-git",
                        identity="github.com/example/tools",
                        transport="https",
                        object_format="sha1",
                        commit=_RECEIPT_V3_COMMIT,
                        substituted=True,
                        substitution=SubstitutionState(
                            type="network-git",
                            ref_kind="revision",
                            ref_value=_RECEIPT_V3_REVISION,
                        ),
                    )
                ),
                acquire=acquire,
                audit=audit,
                store=repair_store,
                compiler=compiler,
                package=package,
            )
        )
        assert observed == [_RECEIPT_V3_COMMIT]
        # Repair audits before its first cache read; a repair-only lookup
        # moved before the audit reorders these events in every driver.
        assert repair_events == ["acquire", "audit", "cache"]
        assert compiler.calls == 1
        assert repaired.receipt == genuine.receipt
        assert repaired.cache_key == genuine.cache_key


def _register_receipt_v3_drivers() -> None:
    from csk.builds import currentness as build_currentness

    for _field in build_currentness.EXTERNAL_EVIDENCE_FIELDS:
        register_semantic_driver(
            f"external-evidence-mismatch-{_field}",
            _drive_external_evidence_mismatch,
        )


_register_receipt_v3_drivers()


@pytest.mark.parametrize(
    "entry",
    [
        entry
        for entry in SCHEMA_CASES
        if entry["schema"] == "build-receipt-v3.schema.json"
    ],
    ids=[
        f"{entry['schema']}:{entry['instance']}"
        for entry in SCHEMA_CASES
        if entry["schema"] == "build-receipt-v3.schema.json"
    ],
)
def test_draft_sources_build_receipt_v3_schema_case_through_production_reader(
    entry: dict[str, Any],
) -> None:
    """Drive every build-receipt-v3 schema case through csk's production reader.

    Registered by TASK-260916-341a6q: the valid fixture must parse to the v3
    model with its pinned cache key and reach a canonical read-write fixpoint
    (the pinned fixtures are not key-sorted, so the fixpoint, not fixture
    bytes, is the byte-identity property); invalid fixtures must refuse.
    """

    from csk.builds import metadata as build_metadata_module

    raw = (_suite_root() / Path(entry["instance"])).read_bytes()
    if entry["valid"]:
        parsed = build_metadata_module.parse_receipt_v3(json.loads(raw))
        assert parsed.schema_version == 3
        assert parsed.cache_key == json.loads(raw)["cache_key"]
        assert parsed.cache_key == build_metadata_module.source_aware_cache_key(
            parsed.input
        )
        once = build_metadata_module.canonical_receipt_v3_bytes(parsed)
        twice = build_metadata_module.canonical_receipt_v3_bytes(
            build_metadata_module.read_receipt_v3(once)
        )
        assert once == twice
        assert build_metadata_module.read_receipt_v3(once) == parsed
    else:
        with pytest.raises(build_metadata_module.BuildMetadataError):
            build_metadata_module.parse_receipt_v3(json.loads(raw))


# Semantic drivers registered by TASK-260916-11yseo (source audit binding).
#
# The ten attestation-evidence drivers and local-required-registry were
# registered by TASK-260916-15nf0l through the same production entry points
# this leaf binds (install_marker.validate_attestation_evidence and
# install_marker.check_local_registry_requirement); registering them again
# would fail on duplication, so this leaf registers only the two cases it
# owns that have no driver yet, and extends the shared family through its
# own binding in tests/test_source_audit.py.


def _drive_missing_audit_report(case: dict[str, Any]) -> None:
    """Drive ``missing-audit-report``: an unreadable report rejects."""

    from csk.audit import pipeline as audit_pipeline_module
    from csk.audit.model import Decision as audit_decision_module
    from csk.sources import package_identity as source_package_module
    from csk.sources import source_audit as source_audit_module

    assert case["input"]["decision"] == "allow"
    assert case["input"]["report"] == "unreadable"
    assert case["expected"] == "reject"
    with tempfile.TemporaryDirectory(prefix="csk-missing-audit-report-") as raw:
        root = Path(raw)
        csk_home = root / "csk-home"
        csk_home.mkdir()
        content = "sha256:" + "a" * 64
        package = source_package_module.LocalSnapshot(snapshot="sha256:" + "1" * 64)
        policy = source_audit_module.SourceAuditPolicy(
            mode="advisory",
            fail_on="high",
            backend="null",
            registry_policy="advisory",
            revocations=(),
            script_policy="manager-worker-v1",
        )
        report = audit_pipeline_module.AuditReport(
            scope="test",
            skill="golden",
            source="local:packages/golden",
            ref_kind="branch",
            ref="main",
            commit="0" * 40,
            schema_version=3,
            source_file=None,
            runtime_roots=(),
            content_sha256=content,
            findings=(),
            decision=audit_decision_module.ALLOW,
            ran_at="2026-09-10T00:00:00Z",
        )
        record = source_audit_module.record_source_audit(
            report,
            csk_home=csk_home,
            package=package,
            git=None,
            policy=policy,
            created_at="2026-09-10T00:00:00Z",
        )
        target = source_audit_module.source_audit_report_path(csk_home, content)
        before = _marker_v5_tree_hash(csk_home)
        original_read_bytes = Path.read_bytes

        def _refuse(self: Path) -> bytes:
            if self == target:
                raise PermissionError("audit store denied the read")
            return original_read_bytes(self)

        with patch.object(Path, "read_bytes", _refuse):
            with pytest.raises(source_audit_module.SourceAuditError) as refused:
                source_audit_module.validate_source_audit(
                    record,
                    csk_home=csk_home,
                    policy=policy,
                    expected_package=package,
                    expected_content_sha256=content,
                )
        assert refused.value.code == source_audit_module.CODE_REPORT_UNREADABLE
        assert _marker_v5_tree_hash(csk_home) == before


register_semantic_driver("missing-audit-report", _drive_missing_audit_report)


def _drive_strict_network_attestation_local(case: dict[str, Any]) -> None:
    """Drive ``strict-network-attestation-local``: fail with no publication."""

    from csk import install_marker as install_marker_module
    from csk.sources import package_identity as source_package_module

    assert case["input"]["kind"] == "local-snapshot"
    assert case["input"]["policy"] == "require-network-attestation"
    assert case["expected"] == "reject"
    with tempfile.TemporaryDirectory(prefix="csk-strict-network-local-") as raw:
        root = Path(raw)
        csk_home = root / "csk-home"
        project = root / "project"
        csk_home.mkdir()
        project.mkdir()
        (csk_home / "lock.json").write_text("{}")
        (project / "Skillfile.json").write_text("{}")
        before_home = _marker_v5_tree_hash(csk_home)
        before_project = _marker_v5_tree_hash(project)
        with pytest.raises(install_marker_module.InstallMarkerError) as refused:
            install_marker_module.check_local_registry_requirement(
                source_package_module.LocalSnapshot(snapshot="sha256:" + "1" * 64),
                network_attestation_required=True,
            )
        assert refused.value.code == "local_registry_attestation_required"
        assert _marker_v5_tree_hash(csk_home) == before_home
        assert _marker_v5_tree_hash(project) == before_project


register_semantic_driver(
    "strict-network-attestation-local", _drive_strict_network_attestation_local
)


# TASK-260916-18j5hg resolve-source-closure-and-explicit-refresh owns
# frozen-membership, runtime-only-refresh and build-only-refresh. Each
# driver builds its fixture from the case input, drives a production
# entry point, and asserts the exact expected outcome.


def _closure_refresh_fixture(
    root: Path,
) -> tuple[Any, Any, Any, Any, Any]:
    """Provision an isolated schema-2 project: (config, project, source, home, skills)."""

    from dataclasses import replace as _replace

    from tests.conftest import make_config, make_project

    from csk import config as _config_module
    from csk import locking as _locking_module

    csk_home = root / ".cocoaskills"
    _locking_module.provision_new_manager_home(csk_home)
    skills_root = root / "skills"
    skills_root.mkdir()
    project = make_project(root)
    source = root / "pkgs"
    source.mkdir()
    base = make_config(csk_home, skills_root, project, agents=["codex_cli"])
    cfg = _replace(
        base,
        experimental=_config_module.ExperimentalConfig(skillfile_sources=True),
    )
    return cfg, project, source, csk_home, skills_root


def _closure_refresh_write_skill(
    directory: Path,
    name: str,
    *,
    manifest_extra: dict[str, Any] | None = None,
    extra_files: dict[str, str | bytes] | None = None,
) -> None:
    from tests.conftest import write_files

    manifest_payload: dict[str, Any] = {"schema_version": 7, "capabilities": {}}
    if manifest_extra:
        manifest_payload.update(manifest_extra)
    files: dict[str, str | bytes] = {
        "SKILL.md": f"---\nname: {name}\ndescription: fixture {name}\n---\n\n# {name}\n",
        "agent-skill.json": json.dumps(manifest_payload),
    }
    if extra_files:
        files.update(extra_files)
    write_files(directory, files)


def _closure_refresh_skillfile(
    project: Path, sources: dict[str, dict[str, Any]], selectors: list[dict[str, Any]]
) -> None:
    from tests.conftest import write_skillfile

    write_skillfile(
        project, {"schema_version": 2, "sources": sources, "skills": selectors}
    )


def _drive_frozen_membership(case: dict[str, Any]) -> None:
    """Drive ``frozen-membership`` through the frozen production paths.

    ``csk`` has no launch command; launch is frozen consumption, and
    its two production paths are the locked install and status. Both
    must use the locked review-only membership while a new member
    directory sits on disk, without enumerating or fetching. The
    collection selector makes the outcome assert the frozen
    membership too: under a rescan ``docs`` would install.
    """

    from csk import installer as _installer_module
    from csk import status as _status_module
    from csk.sources import lock as _lock_module
    from csk.sources import publish as _publish_module
    from csk.sources import transport as _transport_module

    assert case["input"]["locked"] == ["review"]
    assert case["input"]["live"] == ["review", "docs"]
    assert case["input"]["operation"] == "launch"
    assert case["expected"] == "use-locked-review-only"
    if not _selection_fs.supports_descriptor_traversal():
        pytest.skip(_selection_fs.NO_DESCRIPTOR_TRAVERSAL_REASON)
    with tempfile.TemporaryDirectory(prefix="csk-frozen-membership-") as raw:
        root = Path(raw)
        cfg, project, source, _home, _skills = _closure_refresh_fixture(root)
        _closure_refresh_write_skill(source / "coll" / "review", "review")
        _closure_refresh_skillfile(
            project,
            {"local": {"path": os.fspath(source)}},
            [{"from": "local", "directory": "coll", "include": ["*"]}],
        )
        results = _installer_module.install(
            cfg, alias="app", options=_installer_module.InstallOptions()
        )
        assert len(results) == 1 and results[0].status == "ok", results[0].errors
        lock_before = (project / _publish_module.SKILLFILE_LOCK_NAME).read_bytes()
        assert [
            member.name for member in _lock_module.read_lock(lock_before).members
        ] == case["input"]["locked"]

        _closure_refresh_write_skill(source / "coll" / "docs", "docs")

        def _boom(*args: Any, **kwargs: Any) -> Any:
            raise AssertionError("frozen launch must not enumerate or fetch")

        with (
            patch.object(_transport_module, "resolve_ref", _boom),
            patch.object(_transport_module, "acquire_network", _boom),
            patch.object(_publish_module, "expand_selectors", _boom),
            patch.object(_publish_module, "expand_collection", _boom),
        ):
            relaunched = _installer_module.install(
                cfg, alias="app", options=_installer_module.InstallOptions()
            )
            assert len(relaunched) == 1 and relaunched[0].status == "ok", (
                relaunched[0].errors
            )
            assert any(
                "review up-to-date" in message for message in relaunched[0].messages
            )
            assert not (project / ".agents" / "skills" / "docs").exists()
            assert (
                project / _publish_module.SKILLFILE_LOCK_NAME
            ).read_bytes() == lock_before
            collected = _status_module.collect_status(cfg, alias="app")
            assert len(collected) == 1
            assert [skill.name for skill in collected[0].skills] == ["review"]
            assert [skill.label for skill in collected[0].skills] == ["up-to-date"]
            assert collected[0].errors == ()


register_semantic_driver("frozen-membership", _drive_frozen_membership)


def _closure_refresh_context_bytes(project: Path, name: str) -> dict[str, bytes]:
    """Read one installed context tree, excluding the install marker."""

    root = project / ".agents" / "skills" / name
    return {
        path.relative_to(root).as_posix(): path.read_bytes()
        for path in sorted(root.rglob("*"))
        if path.is_file() and path.name != ".csk-install.json"
    }


def _drive_runtime_only_refresh(case: dict[str, Any]) -> None:
    """Drive ``runtime-only-refresh`` through install plus refresh.

    The script changes (B to C) while the projected context stays A:
    after explicit refresh the package identity, the runtime store
    entry and the lock identity are all new.
    """

    from csk import installer as _installer_module
    from csk.sources import lock as _lock_module
    from csk.sources import publish as _publish_module
    from csk.sources.package_identity import package_identity_sha256 as _package_key

    assert case["input"]["context_before"] == "A"
    assert case["input"]["context_after"] == "A"
    assert case["input"]["script_before"] == "B"
    assert case["input"]["script_after"] == "C"
    assert case["expected"] == "new-package-runtime-and-cache-identity"
    if not _selection_fs.supports_descriptor_traversal():
        pytest.skip(_selection_fs.NO_DESCRIPTOR_TRAVERSAL_REASON)
    with tempfile.TemporaryDirectory(prefix="csk-runtime-only-refresh-") as raw:
        root = Path(raw)
        cfg, project, source, csk_home, _skills = _closure_refresh_fixture(root)
        _closure_refresh_write_skill(
            source / "review",
            "review",
            manifest_extra={
                "commands": {"run": {"type": "script", "unix_path": "scripts/run"}},
                "runtime_roots": ["scripts"],
            },
            extra_files={"scripts/run": "#!/bin/sh\necho B\n"},
        )
        _closure_refresh_skillfile(
            project,
            {"local": {"path": os.fspath(source)}},
            [{"name": "review", "from": "local", "directory": "review"}],
        )

        def _install(fetch: bool) -> None:
            results = _installer_module.install(
                cfg, alias="app", options=_installer_module.InstallOptions(fetch=fetch)
            )
            assert len(results) == 1 and results[0].status == "ok", results[0].errors

        _install(False)
        lock_path = project / _publish_module.SKILLFILE_LOCK_NAME
        before = _lock_module.read_lock(lock_path.read_bytes())
        key_before = _package_key(before.members[0].package)
        entry_before = _publish_module.runtime_entry_path(csk_home, "review", key_before)
        assert entry_before.is_dir()
        context_before = _closure_refresh_context_bytes(project, "review")

        (source / "review" / "scripts" / "run").write_text(
            "#!/bin/sh\necho C\n", encoding="utf-8"
        )
        _install(True)

        after = _lock_module.read_lock(lock_path.read_bytes())
        key_after = _package_key(after.members[0].package)
        assert key_after != key_before
        assert after.lock_sha256 != before.lock_sha256
        entry_after = _publish_module.runtime_entry_path(csk_home, "review", key_after)
        assert entry_after.is_dir()
        assert not entry_before.exists()
        assert _closure_refresh_context_bytes(project, "review") == context_before


register_semantic_driver("runtime-only-refresh", _drive_runtime_only_refresh)


def _closure_refresh_stub_build_toolchain() -> Any:
    """Return an undo closure stubbing the go-v1 compiler hermetically."""

    import platform as _platform_module
    from unittest.mock import patch as _mock_patch

    from csk.builds import go_v1 as _go_v1
    from csk.builds import metadata as _build_metadata
    from csk.builds import toolchain as _build_toolchain

    machine = _platform_module.machine().lower()
    if machine in {"arm64", "aarch64"}:
        goarch, tuning = "arm64", {"GOARM64": "v8.0"}
    else:
        goarch, tuning = "amd64", {"GOAMD64": "v1"}
    if sys.platform == "darwin":
        goos = "darwin"
    elif os.name == "nt":
        goos = "windows"
    else:
        goos = "linux"
    host = _build_toolchain.NativeTarget(goos=goos, goarch=goarch, tuning=tuning)

    class _FakeSession:
        target = host
        toolchain = _build_toolchain.ToolchainIdentity(
            algorithm=_build_toolchain.TOOLCHAIN_ALGORITHM,
            content_sha256="sha256:" + "b" * 64,
            go_relpath=_build_toolchain.GO_RELPATH,
            go_version=f"go version go1.25.5 {host.goos}/{host.goarch}",
        )

        def __init__(self, toolchain_config: _build_toolchain.ToolchainConfig):
            self.operation_root = toolchain_config.private_base / "operation"
            self.operation_root.mkdir(mode=0o700)
            self.executable = self.operation_root / "go"
            self.goroot = self.operation_root / "goroot"

        def __enter__(self) -> Any:
            return self

        def __exit__(self, *args: object) -> None:
            return None

    def _fake_build(request: _go_v1.BuildRequest) -> _go_v1.BuildResult:
        marker = (
            request.source_snapshot.path / request.source_dir / "marker.txt"
        ).read_bytes()
        payload = b"#!/bin/sh\necho " + marker.strip() + b"\n"
        artifact_path = request.toolchain_session.operation_root / (
            f"artifact-{request.command}"
        )
        artifact_path.write_bytes(payload)
        artifact_path.chmod(0o700)
        return _go_v1.BuildResult(
            artifact=_go_v1.BuildArtifact(
                staged_path=artifact_path,
                metadata=_go_v1.ArtifactMetadata(
                    path=_build_metadata.derived_artifact_path(
                        request.command, goos=host.goos
                    ),
                    sha256="sha256:" + hashlib.sha256(payload).hexdigest(),
                    size=len(payload),
                ),
            ),
            capability_evidence=_go_v1.CapabilityEvidence(
                record_version="capability-evidence-v1",
                execution_policy="manager-worker-v1",
                platform=host.goos,
                controls=(),
            ),
        )

    patches = (
        _mock_patch.object(
            _build_toolchain,
            "capture_operator_search_path",
            lambda: _build_toolchain.OperatorSearchPath(("/fixture/bin",)),
        ),
        _mock_patch.object(_build_toolchain, "establish_toolchain", _FakeSession),
        _mock_patch.object(_build_toolchain, "preflight_toolchain", lambda config: None),
        _mock_patch.object(_go_v1, "build", _fake_build),
    )
    for entered in patches:
        entered.start()
    return patches


def _drive_build_only_refresh(case: dict[str, Any]) -> None:
    """Drive ``build-only-refresh`` through install plus refresh.

    The build input changes (B to C) while the projected context
    stays A: after explicit refresh the package identity and the
    receipt-3 cache key are both new.
    """

    from dataclasses import replace as _replace

    from csk import install_marker as _install_marker_module
    from csk import installer as _installer_module
    from csk.sources import lock as _lock_module
    from csk.sources import publish as _publish_module
    from csk.sources.package_identity import package_identity_sha256 as _package_key

    assert case["input"]["context_before"] == "A"
    assert case["input"]["context_after"] == "A"
    assert case["input"]["build_before"] == "B"
    assert case["input"]["build_after"] == "C"
    assert case["expected"] == "new-package-and-cache-identity"
    if not _selection_fs.supports_descriptor_traversal():
        pytest.skip(_selection_fs.NO_DESCRIPTOR_TRAVERSAL_REASON)
    with tempfile.TemporaryDirectory(prefix="csk-build-only-refresh-") as raw:
        root = Path(raw)
        base, project, source, _home, _skills = _closure_refresh_fixture(root)
        cfg = _replace(base, audit=_replace(base.audit, enabled=True))
        _closure_refresh_write_skill(
            source / "built",
            "built",
            manifest_extra={
                "commands": {
                    "greet": {
                        "type": "build",
                        "driver": "go-v1",
                        "source_dir": "build/cmd/greet",
                    }
                },
                "build_roots": ["build"],
            },
            extra_files={
                "build/go.mod": "module example.com/greet\n\ngo 1.23\n",
                "build/cmd/greet/main.go": "package main\n\nfunc main() {}\n",
                "build/cmd/greet/marker.txt": "B\n",
            },
        )
        _closure_refresh_skillfile(
            project,
            {"local": {"path": os.fspath(source)}},
            [{"name": "built", "from": "local", "directory": "built"}],
        )
        patches = _closure_refresh_stub_build_toolchain()
        try:

            def _install(fetch: bool) -> None:
                results = _installer_module.install(
                    cfg,
                    alias="app",
                    options=_installer_module.InstallOptions(fetch=fetch),
                )
                assert len(results) == 1 and results[0].status == "ok", (
                    results[0].errors
                )

            _install(False)
            lock_path = project / _publish_module.SKILLFILE_LOCK_NAME
            before = _lock_module.read_lock(lock_path.read_bytes())
            key_before = _package_key(before.members[0].package)
            marker_before = _install_marker_module.read_install_marker(
                (project / ".agents" / "skills" / "built" / ".csk-install.json")
                .read_bytes()
            )
            assert isinstance(marker_before, _install_marker_module.InstallMarkerV5)
            cache_before = marker_before.builds["greet"].cache_key
            context_before = _closure_refresh_context_bytes(project, "built")

            (source / "built" / "build" / "cmd" / "greet" / "marker.txt").write_text(
                "C\n", encoding="utf-8"
            )
            _install(True)

            after = _lock_module.read_lock(lock_path.read_bytes())
            key_after = _package_key(after.members[0].package)
            assert key_after != key_before
            marker_after = _install_marker_module.read_install_marker(
                (project / ".agents" / "skills" / "built" / ".csk-install.json")
                .read_bytes()
            )
            assert isinstance(marker_after, _install_marker_module.InstallMarkerV5)
            assert marker_after.builds["greet"].cache_key != cache_before
            assert _closure_refresh_context_bytes(project, "built") == context_before
        finally:
            for entered in patches:
                entered.stop()


register_semantic_driver("build-only-refresh", _drive_build_only_refresh)
