#!/usr/bin/env python3
"""Fail-closed verifier and selector for the accepted Windows protocol shards."""

from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
import sys
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any


TASK_ID = "TASK-260803-2ol7ok"
EXPECTED_SOURCE = "2bfe3d64e9142d62e8ea3f92558eeee331f4578a"
EXPECTED_PROTOCOL = "0c81c1f8d5321d822be2a2817b05aea03e656e15"
EXPECTED_TIMEOUT_MINUTES = {
    "p00-contract-and-registry": 5,
    "p01-lifecycle-cached-baseline": 30,
    "p02-lifecycle-sabotage-a": 45,
    "p03-lifecycle-sabotage-b": 45,
    "p04-lifecycle-sabotage-c": 45,
    "p05-lifecycle-sabotage-d": 45,
}


def fail(message: str) -> None:
    raise ValueError(message)


def load_object(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        fail(f"{path}: expected a JSON object")
    return value


def exact_nodes(values: Any, label: str) -> list[str]:
    if not isinstance(values, list) or not all(isinstance(item, str) for item in values):
        fail(f"{label}: expected a list of node-id strings")
    duplicates = sorted(node for node, count in Counter(values).items() if count != 1)
    if duplicates:
        fail(f"{label}: duplicate nodes: {duplicates[:5]}")
    if any(not node.startswith("tests/test_protocol_conformance.py::") for node in values):
        fail(f"{label}: contains an out-of-suite node")
    return values


def collected_nodes(path: Path) -> list[str]:
    data = path.read_bytes()
    if data.startswith((b"\xff\xfe", b"\xfe\xff")):
        text = data.decode("utf-16")
    else:
        text = data.decode("utf-8-sig")
    return exact_nodes(
        [
            line.strip()
            for line in text.splitlines()
            if line.startswith("tests/test_protocol_conformance.py::")
        ],
        str(path),
    )


def git_output(checkout: Path, *args: str) -> str:
    if not checkout.is_dir():
        fail(f"checkout does not exist or is not a directory: {checkout}")
    result = subprocess.run(
        ["git", "-C", str(checkout), *args],
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        detail = result.stderr.strip() or result.stdout.strip() or f"exit {result.returncode}"
        fail(f"git {' '.join(args)} failed for {checkout}: {detail}")
    return result.stdout.strip()


def verify_exact_checkout(checkout: Path, expected: str, label: str) -> str:
    actual = git_output(checkout, "rev-parse", "HEAD")
    if actual != expected:
        fail(f"{label} checkout HEAD mismatch: expected {expected}, found {actual}")
    dirty = git_output(checkout, "status", "--porcelain", "--untracked-files=all")
    if dirty:
        fail(f"{label} checkout is dirty: {dirty.splitlines()[0]}")
    return actual


def verify_source_checkout(checkout: Path) -> str:
    """Accept clean descendants of the audit base while pinning audited bytes."""
    actual = git_output(checkout, "rev-parse", "HEAD")
    result = subprocess.run(
        ["git", "-C", str(checkout), "merge-base", "--is-ancestor", EXPECTED_SOURCE, actual],
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        fail(
            "source checkout is not descended from audited base: "
            f"base={EXPECTED_SOURCE} head={actual}"
        )
    dirty = git_output(checkout, "status", "--porcelain", "--untracked-files=all")
    if dirty:
        fail(f"source checkout is dirty: {dirty.splitlines()[0]}")
    return actual


def verify_audited_hashes(checkout: Path, hashes: Any) -> None:
    if not isinstance(hashes, dict) or not hashes:
        fail("classification.audited_files_sha256 must be a non-empty object")
    for relative, expected in sorted(hashes.items()):
        if not isinstance(relative, str) or not isinstance(expected, str):
            fail("classification.audited_files_sha256 contains a malformed entry")
        candidate = (checkout / relative).resolve()
        root = checkout.resolve()
        if root not in candidate.parents or not candidate.is_file():
            fail(f"audited file is missing or escapes source checkout: {relative}")
        actual = hashlib.sha256(candidate.read_bytes()).hexdigest()
        if actual != expected:
            fail(f"audited file hash mismatch: {relative}")


def verify(
    classification_path: Path,
    manifest_path: Path,
    collected_path: Path | None,
    source_checkout: Path,
    protocol_checkout: Path,
) -> dict[str, Any]:
    classification = load_object(classification_path)
    manifest = load_object(manifest_path)
    for label, document in (("classification", classification), ("manifest", manifest)):
        if document.get("schema_version") != 1:
            fail(f"{label}: unsupported schema_version")
        if document.get("task_id") != TASK_ID:
            fail(f"{label}: wrong task_id")
        if document.get("source_commit") != EXPECTED_SOURCE:
            fail(f"{label}: wrong source_commit")
        if document.get("protocol_commit") != EXPECTED_PROTOCOL:
            fail(f"{label}: wrong protocol_commit")

    source_head = verify_source_checkout(source_checkout)
    protocol_head = verify_exact_checkout(protocol_checkout, EXPECTED_PROTOCOL, "protocol")
    verify_audited_hashes(source_checkout, classification.get("audited_files_sha256"))

    baseline = exact_nodes(manifest.get("baseline_nodes"), "manifest.baseline_nodes")
    if manifest.get("baseline_count") != len(baseline):
        fail("manifest.baseline_count does not equal baseline_nodes length")
    if len(baseline) != 1045:
        fail(f"manifest baseline must contain 1045 nodes, found {len(baseline)}")
    if collected_path is not None and collected_nodes(collected_path) != baseline:
        fail("fresh collection differs from the pinned ordered baseline")

    shards = manifest.get("shards")
    if not isinstance(shards, list) or not shards:
        fail("manifest.shards must be a non-empty list")
    shard_ids: set[str] = set()
    assignments: dict[str, str] = {}
    cluster_shards: dict[str, set[str]] = defaultdict(set)
    flattened: list[str] = []
    selector_errors: list[str] = []
    for shard in shards:
        if not isinstance(shard, dict) or not isinstance(shard.get("id"), str):
            fail("every shard must be an object with a string id")
        shard_id = shard["id"]
        if shard_id in shard_ids:
            fail(f"duplicate shard id: {shard_id}")
        shard_ids.add(shard_id)
        if shard.get("timeout_minutes") != EXPECTED_TIMEOUT_MINUTES.get(shard_id):
            fail(f"shard {shard_id}: timeout_minutes mismatch")
        nodes = exact_nodes(shard.get("nodes"), f"shard {shard_id}")
        if shard.get("node_count") != len(nodes):
            fail(f"shard {shard_id}: node_count mismatch")
        selectors = shard.get("selectors")
        expected_selectors = list(dict.fromkeys(node.split("[", 1)[0] for node in nodes))
        if selectors != expected_selectors:
            selector_errors.append(shard_id)
        flattened.extend(nodes)
        for node in nodes:
            if node in assignments:
                fail(f"overlap: {node} occurs in {assignments[node]} and {shard_id}")
            assignments[node] = shard_id
        clusters = shard.get("atomic_clusters")
        if not isinstance(clusters, list) or not all(isinstance(item, str) for item in clusters):
            fail(f"shard {shard_id}: invalid atomic_clusters")
        duplicate_clusters = sorted(
            cluster for cluster, count in Counter(clusters).items() if count != 1
        )
        if duplicate_clusters:
            fail(f"shard {shard_id}: duplicate atomic clusters: {duplicate_clusters[:5]}")
        for cluster in clusters:
            cluster_shards[cluster].add(shard_id)

    missing = sorted(set(baseline) - set(flattened))
    extra = sorted(set(flattened) - set(baseline))
    if missing or extra:
        fail(f"partition mismatch: missing={missing[:5]} extra={extra[:5]}")
    if shard_ids != set(EXPECTED_TIMEOUT_MINUTES):
        fail("manifest shard inventory differs from bounded timeout policy")
    if selector_errors:
        fail(
            "selectors do not exactly cover function clusters in shards: "
            + ", ".join(selector_errors)
        )

    rows = classification.get("nodes")
    if not isinstance(rows, list) or len(rows) != len(baseline):
        fail("classification.nodes does not cover the baseline cardinality")
    row_by_node: dict[str, dict[str, Any]] = {}
    for row in rows:
        if not isinstance(row, dict) or not isinstance(row.get("node_id"), str):
            fail("classification row is malformed")
        node = row["node_id"]
        if node in row_by_node:
            fail(f"classification duplicate: {node}")
        row_by_node[node] = row
        if row.get("shard") != assignments.get(node):
            fail(f"classification/manifest shard mismatch for {node}")
        footprint = row.get("footprint")
        expected_keys = set(classification.get("category_definitions", {}))
        if not isinstance(footprint, dict) or set(footprint) != expected_keys:
            fail(f"classification footprint categories are incomplete for {node}")
        if not all(isinstance(value, bool) for value in footprint.values()):
            fail(f"classification footprint is not boolean for {node}")
    if set(row_by_node) != set(baseline):
        fail("classification has a gap or out-of-baseline node")

    atomic = classification.get("atomic_clusters")
    if not isinstance(atomic, dict) or set(atomic) != set(cluster_shards):
        fail("atomic cluster inventory differs between classification and manifests")
    cluster_by_node: dict[str, str] = {}
    for cluster, record in atomic.items():
        if cluster_shards[cluster] and len(cluster_shards[cluster]) != 1:
            fail(f"atomic cluster split across shards: {cluster}")
        if not isinstance(record, dict):
            fail(f"atomic cluster record is malformed: {cluster}")
        members = exact_nodes(record.get("members"), f"atomic cluster {cluster}")
        for node in members:
            if node in cluster_by_node:
                fail(
                    f"atomic cluster overlap: {node} occurs in "
                    f"{cluster_by_node[node]} and {cluster}"
                )
            cluster_by_node[node] = cluster
        member_shards = {assignments.get(node) for node in members}
        if None in member_shards or len(member_shards) != 1:
            fail(f"atomic cluster member gap/split: {cluster}")
        if member_shards != cluster_shards[cluster]:
            fail(f"atomic cluster declaration mismatch: {cluster}")
    if set(cluster_by_node) != set(baseline):
        missing_clusters = sorted(set(baseline) - set(cluster_by_node))
        extra_clusters = sorted(set(cluster_by_node) - set(baseline))
        fail(
            "atomic cluster partition mismatch: "
            f"missing={missing_clusters[:5]} extra={extra_clusters[:5]}"
        )
    for node, row in row_by_node.items():
        if row.get("atomic_cluster") != cluster_by_node[node]:
            fail(f"classification/atomic-cluster mismatch for {node}")

    return {
        "ok": True,
        "task_id": TASK_ID,
        "source_audit_commit": EXPECTED_SOURCE,
        "source_checkout_head": source_head,
        "protocol_checkout_head": protocol_head,
        "baseline_nodes": len(baseline),
        "classified_nodes": len(row_by_node),
        "shards": {shard["id"]: shard["node_count"] for shard in shards},
        "atomic_clusters": len(atomic),
        "overlap": 0,
        "gap": 0,
    }


def select_shard(manifest_path: Path, shard_id: str) -> list[str]:
    manifest = load_object(manifest_path)
    shards = manifest.get("shards")
    if not isinstance(shards, list):
        fail("manifest.shards must be a list")
    matches = [shard for shard in shards if isinstance(shard, dict) and shard.get("id") == shard_id]
    if len(matches) != 1:
        fail(f"unknown or duplicate shard id: {shard_id}")
    return exact_nodes(matches[0].get("nodes"), f"shard {shard_id}")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--classification", type=Path, required=True)
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--collected", type=Path)
    parser.add_argument("--source-checkout", type=Path, required=True)
    parser.add_argument("--protocol-checkout", type=Path, required=True)
    parser.add_argument("--shard-id")
    parser.add_argument("--nodeids-out", type=Path)
    args = parser.parse_args()
    try:
        if (args.shard_id is None) != (args.nodeids_out is None):
            fail("--shard-id and --nodeids-out must be supplied together")
        evidence = verify(
            args.classification,
            args.manifest,
            args.collected,
            args.source_checkout,
            args.protocol_checkout,
        )
        if args.shard_id is not None and args.nodeids_out is not None:
            nodes = select_shard(args.manifest, args.shard_id)
            args.nodeids_out.write_text("\n".join(nodes) + "\n", encoding="utf-8")
            evidence["selected_shard"] = args.shard_id
            evidence["selected_nodes"] = len(nodes)
        print(json.dumps(evidence, sort_keys=True))
    except (OSError, json.JSONDecodeError, ValueError) as error:
        print(f"verification failed: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
