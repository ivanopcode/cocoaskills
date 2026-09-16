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
from collections.abc import Callable, Collection, Mapping
from copy import deepcopy
from pathlib import Path
from typing import Any

import pytest
from jsonschema import Draft202012Validator
from referencing import Registry, Resource

from csk import protocol_json

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
    "missing-snapshot": "TASK-260916-sbzutf",
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
    "v2-port-endpoint": "TASK-260916-fsw7re",
    "v2-declared-mirror": "TASK-260916-fsw7re",
    "v2-mirror-first": "TASK-260916-fsw7re",
    "v2-alias-resolution": "TASK-260916-fsw7re",
    "v2-undeclared-mirror": "TASK-260916-fsw7re",
    "v2-alias-unknown": "TASK-260916-fsw7re",
    "v2-alias-mirror-undeclared": "TASK-260916-fsw7re",
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
    case_id = SEMANTIC_CASES[0]["id"]
    register_semantic_driver(case_id, lambda case: None)
    try:
        with pytest.raises(AssertionError, match="duplicate driver"):
            register_semantic_driver(case_id, lambda case: None)
    finally:
        del SEMANTIC_DRIVERS[case_id]


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
    other test proves the call happens. This test registers a driver for the
    real ``broad-root`` case, drives the actual
    :func:`test_draft_sources_semantic_case` entry (not a private helper),
    asserts the exact case object reaches the driver, and proves a raising
    driver fails the entry. It kills the one-case narrowing mutant
    ``if case_id != \"broad-root\": driver(case)`` in either phase.
    """
    case = next(entry for entry in SEMANTIC_CASES if entry["id"] == "broad-root")
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
