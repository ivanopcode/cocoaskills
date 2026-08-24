from __future__ import annotations

import copy
import importlib.util
import json
import subprocess
from collections.abc import Iterator
from pathlib import Path
from types import ModuleType
from unittest import mock

import pytest


ROOT = Path(__file__).parents[1]
RESEARCH = ROOT / ".research"
CLASSIFICATION = RESEARCH / "TASK-260803-2ol7ok_protocol-isolation-classification.json"
MANIFEST = RESEARCH / "TASK-260803-2ol7ok_protocol-shards.json"
VERIFIER = RESEARCH / "TASK-260803-2ol7ok_verify-protocol-shards.py"
EXPECTED_TIMEOUTS = {
    "p00-contract-and-registry": 5,
    "p01-lifecycle-cached-baseline": 30,
    "p02-lifecycle-sabotage-a": 45,
    "p03-lifecycle-sabotage-b": 45,
    "p04-lifecycle-sabotage-c": 45,
    "p05-lifecycle-sabotage-d": 45,
}


def _git(checkout: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", "-C", str(checkout), *args],
        capture_output=True,
        text=True,
        check=True,
    )
    return result.stdout.strip()


def _git_fixture(tmp_path: Path) -> tuple[Path, str]:
    checkout = tmp_path / "source"
    checkout.mkdir()
    _git(checkout, "init", "-q")
    _git(checkout, "config", "user.email", "ci@example.com")
    _git(checkout, "config", "user.name", "CI")
    tracked = checkout / "tracked.txt"
    tracked.write_text("base\n", encoding="utf-8")
    _git(checkout, "add", "tracked.txt")
    _git(checkout, "commit", "-q", "-m", "base")
    return checkout, _git(checkout, "rev-parse", "HEAD")


def _load_verifier() -> ModuleType:
    spec = importlib.util.spec_from_file_location("protocol_shard_verifier", VERIFIER)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _manifest() -> dict[str, object]:
    value = json.loads(MANIFEST.read_text(encoding="utf-8"))
    assert isinstance(value, dict)
    return value


def _verified_without_git(module: ModuleType, manifest: Path, collected: Path) -> dict[str, object]:
    with (
        mock.patch.object(module, "verify_source_checkout", return_value="source-head"),
        mock.patch.object(module, "verify_exact_checkout", return_value=module.EXPECTED_PROTOCOL),
        mock.patch.object(module, "verify_audited_hashes"),
    ):
        return module.verify(CLASSIFICATION, manifest, collected, ROOT, ROOT)


@pytest.fixture
def accepted_inputs(tmp_path: Path) -> Iterator[tuple[ModuleType, dict[str, object], Path]]:
    module = _load_verifier()
    manifest = _manifest()
    baseline = manifest["baseline_nodes"]
    assert isinstance(baseline, list)
    collected = tmp_path / "collected.txt"
    collected.write_text("\n".join(baseline) + "\n", encoding="utf-8")
    yield module, manifest, collected


def test_accepted_manifest_is_exhaustive_disjoint_and_bounded() -> None:
    manifest = _manifest()
    baseline = manifest["baseline_nodes"]
    shards = manifest["shards"]
    assert isinstance(baseline, list)
    assert isinstance(shards, list)
    flattened = [node for shard in shards for node in shard["nodes"]]

    assert len(baseline) == 1053
    assert set(flattened) == set(baseline)
    assert len(flattened) == len(set(flattened))
    assert {shard["id"]: shard["timeout_minutes"] for shard in shards} == EXPECTED_TIMEOUTS
    assert {shard["id"]: shard["node_count"] for shard in shards} == {
        "p00-contract-and-registry": 573,
        "p01-lifecycle-cached-baseline": 444,
        "p02-lifecycle-sabotage-a": 9,
        "p03-lifecycle-sabotage-b": 9,
        "p04-lifecycle-sabotage-c": 10,
        "p05-lifecycle-sabotage-d": 8,
    }


def test_verifier_accepts_the_exact_inventory(accepted_inputs: tuple[ModuleType, dict[str, object], Path], tmp_path: Path) -> None:
    module, manifest, collected = accepted_inputs
    manifest_path = tmp_path / "manifest.json"
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

    evidence = _verified_without_git(module, manifest_path, collected)

    assert evidence["ok"] is True
    assert evidence["baseline_nodes"] == 1053
    assert evidence["overlap"] == 0
    assert evidence["gap"] == 0


@pytest.mark.parametrize("mutation, diagnostic", [("duplicate", "duplicate nodes"), ("missing", "partition mismatch"), ("unknown", "partition mismatch")])
def test_verifier_fails_closed_on_inventory_drift(
    accepted_inputs: tuple[ModuleType, dict[str, object], Path],
    tmp_path: Path,
    mutation: str,
    diagnostic: str,
) -> None:
    module, accepted, collected = accepted_inputs
    manifest = copy.deepcopy(accepted)
    shards = manifest["shards"]
    assert isinstance(shards, list)
    first = shards[0]
    assert isinstance(first, dict)
    nodes = first["nodes"]
    assert isinstance(nodes, list)
    if mutation == "duplicate":
        nodes.append(nodes[0])
        first["node_count"] += 1
    else:
        removed = nodes.pop()
        first["node_count"] -= 1
        first["selectors"] = list(dict.fromkeys(node.split("[", 1)[0] for node in nodes))
        if mutation == "unknown":
            unknown = "tests/test_protocol_conformance.py::test_unknown_protocol_node"
            nodes.append(unknown)
            first["node_count"] += 1
            first["selectors"].append(unknown)
        assert removed not in nodes
    manifest_path = tmp_path / f"{mutation}.json"
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

    with pytest.raises(ValueError, match=diagnostic):
        _verified_without_git(module, manifest_path, collected)


def test_selector_rejects_an_unknown_shard() -> None:
    module = _load_verifier()
    with pytest.raises(ValueError, match="unknown or duplicate shard id"):
        module.select_shard(MANIFEST, "p99-not-accepted")


def test_collected_inventory_accepts_windows_powershell_utf16(tmp_path: Path) -> None:
    module = _load_verifier()
    node = "tests/test_protocol_conformance.py::test_windows_encoding"
    collected = tmp_path / "collected.txt"
    collected.write_text(f"{node}\n1 test collected\n", encoding="utf-16")

    assert module.collected_nodes(collected) == [node]


def test_source_checkout_accepts_a_clean_audited_descendant(tmp_path: Path) -> None:
    module = _load_verifier()
    checkout, audit_base = _git_fixture(tmp_path)
    (checkout / "descendant.txt").write_text("descendant\n", encoding="utf-8")
    _git(checkout, "add", "descendant.txt")
    _git(checkout, "commit", "-q", "-m", "descendant")

    with mock.patch.object(module, "EXPECTED_SOURCE", audit_base):
        assert module.verify_source_checkout(checkout) == _git(checkout, "rev-parse", "HEAD")


def test_source_checkout_rejects_a_non_descendant(tmp_path: Path) -> None:
    module = _load_verifier()
    checkout, audit_base = _git_fixture(tmp_path)
    _git(checkout, "checkout", "-q", "--orphan", "unrelated")
    _git(checkout, "rm", "-q", "--cached", "tracked.txt")
    (checkout / "tracked.txt").unlink()
    (checkout / "unrelated.txt").write_text("unrelated\n", encoding="utf-8")
    _git(checkout, "add", "unrelated.txt")
    _git(checkout, "commit", "-q", "-m", "unrelated")

    with (
        mock.patch.object(module, "EXPECTED_SOURCE", audit_base),
        pytest.raises(ValueError, match="not descended from audited base"),
    ):
        module.verify_source_checkout(checkout)


@pytest.mark.parametrize("dirty_kind", ["tracked", "untracked"])
def test_source_checkout_rejects_dirtiness(tmp_path: Path, dirty_kind: str) -> None:
    module = _load_verifier()
    checkout, audit_base = _git_fixture(tmp_path)
    if dirty_kind == "tracked":
        (checkout / "tracked.txt").write_text("dirty\n", encoding="utf-8")
    else:
        (checkout / "untracked.txt").write_text("dirty\n", encoding="utf-8")

    with (
        mock.patch.object(module, "EXPECTED_SOURCE", audit_base),
        pytest.raises(ValueError, match="source checkout is dirty"),
    ):
        module.verify_source_checkout(checkout)
