from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
from typing import Any

import pytest

from csk import build_repository, install_marker, protocol_json


ROOT_TEXT = os.environ.get("CURATOR_EXTERNAL_REPOSITORY_CORPUS_ROOT")
pytestmark = pytest.mark.skipif(
    not ROOT_TEXT,
    reason="CURATOR_EXTERNAL_REPOSITORY_CORPUS_ROOT is not set",
)

PROTOCOL = "1.0.0-rc.5"
CORPUS_VERSION = "rc5-external-repository-interop-v1"
ACCEPTED_MANIFEST_SHA256 = (
    "cc9e9c0f93b2497a060a533503a4d030d1a715fe1dd4eb8bf9820168a9257697"
)

# Every binding names a CocoaSkills-native test module.  The shared corpus owns
# the cases and expected outcomes; this table contains no copied golden values
# and imports no Curator implementation package.
BINDINGS = {
    **{case: "tests/test_git_admission.py" for case in (
        "sha1-tag-match-https", "sha1-tag-match-ssh", "sha1-tag-moved",
        "sha1-tag-missing", "sha256-untagged", "untagged-object-missing",
        "clean-git-session", "exact-fetch-closed-shape", "ssh-wrapper-closed-shape",
        "raw-object-reader-closed-shape", "raw-object-malformed", "lfs-pointer",
        "submodule-gitlink", "symbolic-link", "special-file-mode",
        "alternate-object-store", "replace-ref", "graft", "promisor-pack",
        "partial-clone", "gitfile", "linked-worktree", "bare-repository",
        "reftable", "object-link", "filter-config-inert", "credential-helper-inert",
        "pack-v2-sha1", "pack-v3-sha1", "pack-v2-sha256",
        "pack-index-checksum-mismatch",
    )},
    **{case: "tests/test_schema_v7_repository.py" for case in (
        "canonical-https-ssh-scp", "operator-local-identity", "monorepo-root-target",
        "monorepo-nested-target", "local-substitution",
        "network-substitution-revision", "network-substitution-tag",
        "network-substitution-branch", "package-argv-forbidden",
    )},
    **{case: "tests/test_build_repository_pipeline.py" for case in (
        "audit-order-cache-hit", "audit-order-cache-miss", "cache-corrupt-receipt",
        "cache-corrupt-artifact", "protected-offline-reuse", "offline-syntax-only",
        "offline-install-without-snapshot", "package-signing-request",
        "platform-requires-signing",
    )},
    **{case: "tests/test_install_marker_v3.py" for case in (
        "mixed-receipt-v1-v2-marker-v3", "external-receipt-v2-exact-bytes",
        "gc-retains-marker-and-journal-roots",
    )},
    **{case: "tests/test_install_external_repository.py" for case in (
        "status-current", "status-corrupt", "repair-reacquires",
        "shim-path-structural", "path-collision", "shim-collision-rollback",
        "consumer-last-rollback", "truthful-platform-claims",
    )},
}


def _root() -> Path:
    assert ROOT_TEXT is not None
    root = Path(ROOT_TEXT)
    manifest = root / "manifest.json"
    assert manifest.is_file()
    assert hashlib.sha256(manifest.read_bytes()).hexdigest() == ACCEPTED_MANIFEST_SHA256
    return root


def _manifest() -> dict[str, Any]:
    value = json.loads((_root() / "manifest.json").read_bytes())
    assert value["schema_version"] == 1
    assert value["protocol_version"] == PROTOCOL
    assert value["corpus_version"] == CORPUS_VERSION
    return value


def _read(relative: str) -> bytes:
    entries = {entry["path"]: entry for entry in _manifest()["files"]}
    assert relative in entries
    path = Path(relative)
    assert not path.is_absolute() and ".." not in path.parts
    raw = (_root() / path).read_bytes()
    entry = entries[relative]
    assert len(raw) == entry["size"]
    assert "sha256:" + hashlib.sha256(raw).hexdigest() == entry["sha256"]
    return raw


def test_accepted_rc5_corpus_is_authenticated_and_closed() -> None:
    manifest = _manifest()
    paths = [entry["path"] for entry in manifest["files"]]
    assert len(paths) == len(set(paths)) == 18
    for path in paths:
        _read(path)


def test_every_accepted_rc5_case_has_a_cocoaskills_binding() -> None:
    case_manifest = json.loads(_read("case-manifest.json"))
    cases = case_manifest["cases"]
    ids = {case["id"] for case in cases}
    assert len(cases) == len(ids) == len(BINDINGS) == 60
    assert ids == set(BINDINGS)
    assert case_manifest["protocol_version"] == PROTOCOL
    assert case_manifest["manager_adapter"] is None
    assert case_manifest["implementation_neutral"] is True
    assert case_manifest["physical_paths"] == "implementation-specific"
    assert len(case_manifest["architecture_v6_threat_matrix"]) == 18
    assert len(case_manifest["lifecycle_boundaries"]) == 12
    for case in cases:
        assert case["expected"]["outcome"]
        if case["expected"]["outcome"] == "rejected":
            assert case["expected"]["code"] == "manifest_invalid" or case[
                "expected"
            ]["code"].startswith("build_repository_")


def test_shared_source_identity_vectors_use_cocoaskills_parser() -> None:
    vectors = json.loads(_read("vectors/source-identities.json"))
    for vector in vectors["network_cases"]:
        if vector.get("expected_identity") is None:
            with pytest.raises(build_repository.BuildRepositoryError):
                build_repository.parse_repository_source(vector["input"])
        else:
            assert build_repository.parse_repository_source(vector["input"]).identity == vector[
                "expected_identity"
            ]["value"]


def test_shared_receipt_and_marker_bytes_are_exact() -> None:
    receipt = _read("expected/build-receipt-v2.ccj.json")
    assert protocol_json.canonical_bytes(protocol_json.loads_canonical(receipt)) == receipt
    marker = _read("expected/install-marker-v3-mixed-exact.json")
    parsed = install_marker.read_install_marker(marker)
    assert isinstance(parsed, install_marker.InstallMarkerV3)
    assert install_marker.serialize_install_marker(parsed.to_json()) == marker


def test_shared_corpus_contains_no_platform_claim() -> None:
    case_manifest = json.loads(_read("case-manifest.json"))
    claims = [
        case for case in case_manifest["cases"]
        if case["id"] == "truthful-platform-claims"
    ]
    assert len(claims) == 1
    assert claims[0]["expected"] == {
        "linux_excluded": True,
        "native_evidence_required": True,
        "outcome": "no-candidate-claims",
    }
