"""Data-driven adapters for the shared Curator protocol candidate suite.

These helpers deliberately consume caller-supplied vector values.  They do
not contain a second implementation of the build manager; executable vectors
are routed through CocoaSkills parsers and validators, while declarative
lifecycle vectors are checked through cross-artifact and fail-closed
invariants.
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
from dataclasses import replace
from functools import lru_cache
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from jsonschema.validators import validator_for
from referencing import Registry, Resource

from csk import closure, gc, hashing, install_marker, protocol_json, skillspec, whitelist
from csk.builds import cache, metadata, toolchain
from csk.builds import go_v1


JsonObject = dict[str, Any]


def _decode_records(records: list[JsonObject]) -> list[tuple[str, bytes]]:
    return [
        (record["path"], base64.b64decode(record["content_base64"]))
        for record in records
    ]


@lru_cache(maxsize=None)
def _schema_validator(repository_root: Path, schema_name: str) -> Any:
    schemas_root = repository_root / "schemas" / "v1"
    documents = [
        json.loads(path.read_text(encoding="utf-8"))
        for path in (schemas_root / "common.schema.json", schemas_root / schema_name)
    ]
    registry = Registry().with_resources(
        (document["$id"], Resource.from_contents(document))
        for document in documents
    )
    schema = next(
        document
        for document in documents
        if document["$id"].endswith(f"/{schema_name}")
    )
    validator_type = validator_for(schema)
    validator_type.check_schema(schema)
    return validator_type(schema, registry=registry)


def _materialize_skill_case(snapshot: Path, manifest_name: str, raw: bytes) -> None:
    (snapshot / "build" / "cmd" / "tool").mkdir(parents=True)
    (snapshot / "build" / "go.mod").write_text(
        "module example.com/conformance\n",
        encoding="utf-8",
    )
    (snapshot / "build" / "cmd" / "tool" / "main.go").write_text(
        "package main\nfunc main() {}\n",
        encoding="utf-8",
    )
    (snapshot / "scripts").mkdir()
    (snapshot / "scripts" / "tool").write_text("#!/bin/sh\n", encoding="utf-8")
    (snapshot / manifest_name).write_bytes(raw)


def assert_generated_schema_case(
    repository_root: Path,
    conformance_root: Path,
    entry: JsonObject,
    tmp_path: Path,
) -> None:
    schema_name = entry["schema"]
    instance_path = conformance_root / "schema-cases" / entry["instance"]
    raw = instance_path.read_bytes()
    instance = json.loads(raw)
    expected_valid = entry["valid"]

    schema_valid = _schema_validator(repository_root, schema_name).is_valid(instance)
    semantic_valid = True
    if schema_name == "conformance-claim-v3.schema.json" and schema_valid:
        systems = set(instance["operating_systems"])
        claims = instance["build_drivers"]
        drivers = [claim["driver"] for claim in claims]
        semantic_valid = (
            "linux" not in systems
            and len(drivers) == len(set(drivers))
            and all(set(claim["operating_systems"]) <= systems for claim in claims)
        )
    assert (schema_valid and semantic_valid) is expected_valid

    if schema_name in {"agent-skill-v6.schema.json", "csk-skill-v6.schema.json"}:
        manifest_name = (
            "agent-skill.json"
            if schema_name == "agent-skill-v6.schema.json"
            else "csk-skill.json"
        )
        snapshot = tmp_path / instance_path.parent.name / instance_path.stem
        _materialize_skill_case(snapshot, manifest_name, raw)
        if expected_valid:
            spec = skillspec.load_skill_spec(snapshot)
            assert spec.source_file == manifest_name
            assert spec.schema_version == 6
            assert spec.commands["build-tool"].driver == "go-v1"
        else:
            with pytest.raises(skillspec.SkillSpecError):
                skillspec.load_skill_spec(snapshot)
        return

    if schema_name == "build-receipt-v1.schema.json":
        if expected_valid:
            assert metadata.parse_receipt(instance).schema_version == 1
        else:
            with pytest.raises(metadata.BuildMetadataError):
                metadata.parse_receipt(instance)
        return

    if schema_name == "install-marker-v2.schema.json":
        if expected_valid:
            marker = install_marker.read_install_marker(raw)
            assert isinstance(marker, install_marker.InstallMarkerV2)
            assert marker.to_json() == instance
        else:
            with pytest.raises(install_marker.InstallMarkerError):
                install_marker.read_install_marker(raw)


def assert_build_rejection_case(case: JsonObject) -> None:
    expected = case["expected"]
    assert isinstance(case["name"], str) and case["name"]
    assert isinstance(case["boundary"], str) and case["boundary"]
    assert expected["result"] == "reject"
    assert expected["artifact_executed"] is False
    assert expected["reuse"] is False
    assert isinstance(expected["error"], str) and expected["error"]

    supplied = case.get("input")
    if not isinstance(supplied, dict) or "build_input" not in supplied:
        return

    raw_input = protocol_json.canonical_bytes(supplied["build_input"])
    derived = "sha256:" + hashlib.sha256(raw_input).hexdigest()
    assert derived == supplied["derived_cache_key"]
    with pytest.raises(metadata.BuildMetadataError) as raised:
        metadata.parse_build_input(supplied["build_input"])
    assert raised.value.code == expected["error"]


def assert_build_source_case(
    case: JsonObject,
    all_cases: list[JsonObject],
) -> None:
    assert isinstance(case["name"], str) and case["name"]

    records = case.get("records") or case.get("input_order")
    if records is not None:
        decoded = _decode_records(records)
        assert hashing.build_source_sha256(decoded) == case["content_sha256"]
        assert "sha256:" + hashlib.sha256(
            base64.b64decode(case["preimage_base64"])
        ).hexdigest() == case["content_sha256"]
        return

    if case["name"] == "mode-and-timestamp-are-non-inputs":
        ordered = next(item for item in all_cases if "input_order" in item)
        assert hashing.build_source_sha256(
            _decode_records(ordered["input_order"])
        ) == case["content_sha256"]
        assert len({variant["mode"] for variant in case["variants"]}) == 2
        assert len({variant["mtime"] for variant in case["variants"]}) == 2
        return

    if case["name"] == "invalid-unicode-build-source-path":
        path = base64.b64decode(case["input"]["path_bytes_base64"]).decode(
            "utf-8",
            errors="surrogateescape",
        )
        with pytest.raises(hashing.HashingError):
            hashing.build_source_sha256([(path, b"value")])
        return

    if case["name"] == "duplicate-build-source-path":
        paths = case["input"]["paths"]
        with pytest.raises(hashing.HashingError, match="duplicate"):
            hashing.build_source_sha256(
                [(path, str(index).encode("ascii")) for index, path in enumerate(paths)]
            )
        return

    if case["name"] == "legacy-nul-stream-structural-collision":
        one = hashing.build_source_sha256(_decode_records(case["one_file"]))
        two = hashing.build_source_sha256(_decode_records(case["two_files"]))
        assert [one, two] == case["framed_content_sha256"]
        assert (one == two) is case["framed_hashes_equal"]
        assert case["legacy_streams_equal"] is True
        return

    if case["name"] == "root-marker-bytes-are-build-input":
        hashes = [variant["content_sha256"] for variant in case["variants"]]
        assert len(set(hashes)) == len(hashes) == 2
        assert case["build_source_hashes_equal"] is False
        assert case["legacy_installed_tree_hashes_equal"] is True
        return

    expected = case["expected"]
    assert case["boundary"] == "build-source"
    assert expected["result"] == "reject"
    assert expected["artifact_executed"] is False
    assert expected["reuse"] is False
    assert isinstance(expected["error"], str) and expected["error"]


def _materialize_toolchain_entries(root: Path, entries: list[JsonObject]) -> None:
    for entry in entries:
        path = root / entry["path"]
        if entry["type"] == "directory":
            path.mkdir(parents=True, exist_ok=True)
        elif entry["type"] == "file":
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(base64.b64decode(entry["content_base64"]))
        elif entry["type"] == "symlink":
            path.parent.mkdir(parents=True, exist_ok=True)
            path.symlink_to(entry["target"])
        else:
            raise AssertionError(f"unknown toolchain entry type {entry['type']!r}")


def assert_toolchain_case(
    case: JsonObject,
    all_cases: list[JsonObject],
    tmp_path: Path,
) -> None:
    assert isinstance(case["name"], str) and case["name"]

    if "go_version_stdout_base64" in case:
        stdout = base64.b64decode(case["go_version_stdout_base64"])
        if case.get("result") == "accepted":
            assert toolchain.normalize_go_version(stdout) == case["normalized_go_version"]
        else:
            with pytest.raises(toolchain.ToolchainError) as raised:
                toolchain.normalize_go_version(stdout)
            assert raised.value.code == case["expected"]["error"]

    if "entries" in case:
        preimage = base64.b64decode(case["preimage_base64"])
        assert "sha256:" + hashlib.sha256(preimage).hexdigest() == case["content_sha256"]
        if os.name != "nt":
            root = tmp_path / "goroot"
            root.mkdir()
            _materialize_toolchain_entries(root, case["entries"])
            identity = toolchain.fingerprint_toolchain(
                root.resolve(),
                base64.b64decode(case["go_version_stdout_base64"]),
            )
            assert identity.content_sha256 == case["content_sha256"]
        return

    if case["name"] == "toolchain-mode-and-timestamp-are-non-inputs":
        canonical = next(item for item in all_cases if "entries" in item)
        assert case["content_sha256"] == canonical["content_sha256"]
        assert len({variant["mode"] for variant in case["variants"]}) == 2
        assert len({variant["mtime"] for variant in case["variants"]}) == 2
        return

    if case["name"] == "invalid-unicode-toolchain-path":
        path = base64.b64decode(case["input"]["path_bytes_base64"]).decode(
            "utf-8",
            errors="surrogateescape",
        )
        with pytest.raises(toolchain.ToolchainError) as raised:
            toolchain._protocol_path_bytes(path)
        assert raised.value.code == case["expected"]["error"]
        return

    if case["name"] in {
        "escaping-toolchain-link",
        "absolute-toolchain-link",
        "dangling-toolchain-link",
    } and os.name != "nt":
        root = tmp_path / case["name"]
        (root / "bin").mkdir(parents=True)
        (root / "pkg").mkdir()
        (root / "bin" / "go").write_bytes(b"GO")
        link = root / case["input"]["path"]
        link.parent.mkdir(parents=True, exist_ok=True)
        link.symlink_to(case["input"]["target"])
        with pytest.raises(toolchain.ToolchainError) as raised:
            toolchain.fingerprint_toolchain(
                root.resolve(),
                b"go version go1.25.5 darwin/arm64\n",
            )
        assert raised.value.code == case["expected"]["error"]
        return

    if case.get("result") == "accepted":
        assert isinstance(case["content_sha256"], str)
        return

    expected = case["expected"]
    assert case["boundary"] == "toolchain"
    assert expected["result"] == "reject"
    assert expected["artifact_executed"] is False
    assert expected["reuse"] is False
    assert isinstance(expected["error"], str) and expected["error"]


def _write_package_file(path: Path, content: str = "package main\n") -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


def _assert_standard_library_package(case: JsonObject, tmp_path: Path) -> None:
    build_root = tmp_path / "build"
    source_dir = build_root / "cmd" / "tool"
    goroot = tmp_path / "goroot"
    _write_package_file(source_dir / "main.go")
    _write_package_file(goroot / "src" / "fmt" / "print.go", "package fmt\n")
    (build_root / "go.mod").write_text(
        "module example.com/conformance\n",
        encoding="utf-8",
    )
    packages = [
        {
            "Dir": os.fspath(source_dir),
            "GoFiles": ["main.go"],
            "ImportPath": "example.com/conformance/cmd/tool",
            "Module": {
                "Dir": os.fspath(build_root),
                "GoMod": os.fspath(build_root / "go.mod"),
                "Main": True,
                "Path": "example.com/conformance",
            },
            "Name": case["package"]["name"],
            "Root": os.fspath(build_root),
        },
        {
            "DepOnly": True,
            "Dir": os.fspath(goroot / "src" / "fmt"),
            "GoFiles": ["print.go"],
            "Goroot": True,
            "ImportPath": "fmt",
            "Name": "fmt",
            "Root": os.fspath(goroot),
            "Standard": True,
        },
    ]
    payload = b"\n".join(protocol_json.canonical_bytes(item) for item in packages)
    go_v1.validate_package_graph(
        payload,
        build_root=build_root,
        source_dir=source_dir,
        goroot=goroot,
    )
    assert case["package"]["dependencies"] == ["standard-library"]
    assert case["package"]["main_packages"] == 1


def _assert_vendored_package(
    case: JsonObject,
    vectors: JsonObject,
    conformance_root: Path,
    tmp_path: Path,
) -> None:
    del tmp_path
    fixture = conformance_root / vectors["fixture"]["root"]
    package = case["package"]
    build_root = fixture / package["module_root"]
    manifest_case = next(
        item
        for item in vectors["positive_cases"]
        if "manifest" in item
    )
    build_command = next(
        value
        for value in manifest_case["manifest"]["commands"].values()
        if value["type"] == "build"
    )
    source_dir = fixture / build_command["source_dir"]
    module_path = (build_root / "go.mod").read_text(encoding="utf-8").splitlines()[0].split()[1]
    transitive_dir = build_root / package["transitive_package"]
    vendored_dir = build_root / "vendor" / package["vendored_package"]
    embedded = [Path(value).name for value in package["embedded_inputs"]]
    vendored_module = package["vendored_package"].rsplit("/", 1)[0]
    packages = [
        {
            "Dir": os.fspath(source_dir),
            "GoFiles": ["main.go"],
            "ImportPath": f"{module_path}/cmd/golden-tool",
            "Module": {
                "Dir": os.fspath(build_root),
                "GoMod": os.fspath(build_root / "go.mod"),
                "Main": True,
                "Path": module_path,
            },
            "Name": package["name"],
            "Root": os.fspath(build_root),
        },
        {
            "DepOnly": True,
            "Dir": os.fspath(transitive_dir),
            "EmbedFiles": embedded,
            "GoFiles": ["render.go"],
            "ImportPath": f"{module_path}/{package['transitive_package']}",
            "Module": {
                "Dir": os.fspath(build_root),
                "GoMod": os.fspath(build_root / "go.mod"),
                "Main": True,
                "Path": module_path,
            },
            "Name": Path(package["transitive_package"]).name,
            "Root": os.fspath(build_root),
        },
        {
            "DepOnly": True,
            "Dir": os.fspath(vendored_dir),
            "GoFiles": ["decorate.go"],
            "ImportPath": package["vendored_package"],
            "Module": {"Path": vendored_module, "Version": "v1.0.0"},
            "Name": Path(package["vendored_package"]).name,
            "Root": os.fspath(build_root),
        },
    ]
    payload = b"\n".join(protocol_json.canonical_bytes(item) for item in packages)
    go_v1.validate_package_graph(
        payload,
        build_root=build_root,
        source_dir=source_dir,
        goroot=Path(os.path.abspath(os.__file__)).parent,
    )
    assert package["main_packages"] == 1
    assert package["vendor_mode"] == metadata.FIXED_GO_BUILD_POLICY["module_mode"]


def assert_build_positive_case(
    case: JsonObject,
    vectors: JsonObject,
    conformance_root: Path,
    tmp_path: Path,
) -> None:
    name = case["name"]
    assert case["result"] in {"accepted", "cache-hit", "would-preflight-and-build"}

    if name == "schema-6-mixed-script-and-build-commands":
        fixture = conformance_root / vectors["fixture"]["root"]
        shared_manifest = json.loads(
            (fixture / "agent-skill.json").read_text(encoding="utf-8")
        )
        for field, value in case["manifest"].items():
            assert shared_manifest[field] == value
        spec = skillspec.load_skill_spec(fixture)
        assert spec.schema_version == case["manifest"]["schema_version"]
        assert spec.build_roots == tuple(case["manifest"]["build_roots"])
        assert set(spec.commands) == set(case["manifest"]["commands"])
        return

    if name == "build-root-excluded-from-agent-context":
        fixture = conformance_root / vectors["fixture"]["root"]
        spec = skillspec.load_skill_spec(fixture)
        destination = tmp_path / "context"
        files = whitelist.copy_context(
            fixture,
            destination,
            include_scripts=False,
            exclude_roots=spec.runtime_roots,
            build_roots=spec.build_roots,
        )
        assert files == case["expected_context_files"]
        assert files == vectors["fixture"]["expected_context_files"]
        assert hashing.content_sha256(destination) == vectors["fixture"]["context_sha256"]
        assert not set(case["expected_excluded_files"]) & set(files)
        return

    if name == "valid-standard-library-only-main":
        _assert_standard_library_package(case, tmp_path)
        return

    if name == "valid-vendor-only-main-with-transitive-embed":
        _assert_vendored_package(case, vectors, conformance_root, tmp_path)
        return

    if name == "fixed-environment-and-five-direct-argv-forms":
        argv = case["argv"]
        assert [item["name"] for item in argv] == [item["name"] for item in vectors["argv"]]
        assert argv[2]["argv"][1:] == ["env", "-json", *toolchain.GO_ENV_FIELDS]
        assert argv[3]["argv"][1:] == list(go_v1.LIST_ARGUMENTS)
        assert argv[4]["argv"][1:-2] == list(go_v1.BUILD_ARGUMENT_PREFIX)
        assert argv[4]["argv"][-1] == "."
        assert case["shell_used"] is False
        assert case["artifact_executed"] is False
        assert vectors["fixed_environment"]["CGO_ENABLED"] == "0"
        assert vectors["fixed_environment"]["GOPROXY"] == "off"
        assert vectors["fixed_environment"]["GOWORK"] == "off"
        return

    portable = vectors["portable_identity"]
    build_input = metadata.parse_build_input(portable["build_input"])
    if name == "portable-execution-policy-is-required-input":
        assert build_input.policy.execution_policy == case["execution_policy"]
        assert metadata.cache_key(build_input) == case["cache_key"]
        assert case["package_selectable"] is False
        return

    receipt_raw = base64.b64decode(portable["stored_receipt_base64"])
    receipt = metadata.verify_receipt(
        receipt_raw,
        expected_input=build_input,
        expected_cache_key=portable["cache_key"],
        expected_receipt_sha256=portable["receipt_sha256"],
    )
    if name == "protected-cache-hit":
        inspection = cache.CacheInspection(
            cache.CacheEntryStatus.HIT,
            "shared-vector",
            receipt=receipt,
            receipt_bytes=receipt_raw,
            receipt_sha256=portable["receipt_sha256"],
            artifact_path=tmp_path / receipt.artifact.path,
        )
        assert inspection.dry_run_outcome == case["result"]
        assert inspection.reusable is True
        assert case["protected_boundary_verified"] is True
        return

    if name == "compiler-free-dry-run-miss":
        inspection = cache.CacheInspection(cache.CacheEntryStatus.MISS, "shared-vector")
        assert inspection.dry_run_outcome == case["result"]
        assert inspection.reusable is False
        assert case["persistent_effects"] == []
        assert case["source_aware_go_commands"] == []
        assert case["package_independent_go_commands"] == [
            item["name"] for item in vectors["argv"] if not item["source_aware"]
        ]
        return

    raise AssertionError(f"no positive vector adapter for {name!r}")


def _synthetic_probes(platform: str) -> tuple[go_v1.ControlProbe, ...]:
    records = go_v1._NATIVE_CONTROL_PLATFORMS[platform]
    return tuple(
        go_v1.ControlProbe(
            name=name,
            availability=records[name].availability,
            mechanism=records[name].mechanism,
        )
        for name in go_v1.NATIVE_CONTROL_INVENTORY
    )


def assert_capability_evidence_case(
    case: JsonObject,
    policy: JsonObject,
) -> None:
    platform = go_v1.PLATFORM_MACOS
    probes = _synthetic_probes(platform)
    record = go_v1.capability_evidence_from_mapping(
        policy["capability_evidence_record"]["examples"][platform]
    )

    if case["record_version"] != record.record_version:
        record = replace(record, record_version=case["record_version"])
    if case["record_execution_policy"] != record.execution_policy:
        record = replace(record, execution_policy=case["record_execution_policy"])

    entries = list(record.controls)
    matching = next(
        (index for index, entry in enumerate(entries) if entry.name == case["control"]),
        None,
    )
    if case["entry_count"] == 0:
        assert matching is not None
        entries.pop(matching)
    elif case["entry_count"] == 2:
        assert matching is not None
        entries.append(entries[matching])
    elif matching is None:
        entries[0] = replace(
            entries[0],
            name=case["control"],
            availability=case["availability"],
            status=case["status"],
        )
    else:
        entries[matching] = replace(
            entries[matching],
            availability=case["availability"],
            status=case["status"],
        )
    record = replace(record, controls=tuple(entries))

    if case["record_valid"]:
        go_v1.validate_capability_evidence(record, platform, probes)
        assert case["build_permitted"] is True
    else:
        with pytest.raises(go_v1.GoV1Error) as raised:
            go_v1.validate_capability_evidence(record, platform, probes)
        assert raised.value.code == case["expected_error"]
        assert case["build_permitted"] is False
    assert case["changes_cache_key"] is False


def assert_manager_lifecycle_case(
    cluster: str,
    case: JsonObject,
    lifecycle: JsonObject,
) -> None:
    fixture = lifecycle["compiled_build_fixture"]
    assert isinstance(case["name"], str) and case["name"]

    if "cache_key" in case:
        assert case["cache_key"] == fixture["cache_key"]
    if "logical_cache_key" in case:
        assert case["logical_cache_key"] == fixture["cache_key"]
    if "receipt_sha256" in case:
        assert case["receipt_sha256"] == fixture["receipt_sha256"]

    if cluster == "bootstrap_cases":
        if case["force"] and case["if_missing"]:
            assert case["outcome"] == "usage-error"
        elif case["config"] == "missing":
            assert case["outcome"] == "created"
        else:
            assert case["outcome"] == "unchanged-success"
        return

    if cluster == "build_order_cases":
        nodes = {
            name: SimpleNamespace(name=name, edges=[])
            for name in case["active_build_commands"]
        }
        for edge in case["closure_edges"]:
            nodes[edge["provider"]].edges.append(
                closure.ActivationEdge(consumer=edge["consumer"], mode="full")
            )
        provider_order = [node.name for node in closure._topological_order(nodes)]
        assert provider_order == case["expected_provider_order"]
        build_order = [
            f"{provider}/{command}"
            for provider in provider_order
            for command in sorted(
                case["active_build_commands"][provider],
                key=lambda value: value.encode("utf-8"),
            )
        ]
        assert build_order == case["expected_build_order"]
        return

    if cluster == "cache_publication_cases":
        assert case["result"] in {
            cache.CachePublicationStatus.PUBLISHED.value,
            cache.CachePublicationStatus.REUSED_WINNER.value,
            "reuse-winner",
            "determinism-or-corruption-error",
            "replace-from-verified-staging",
            "rebuild-into-new-protected-state",
        }
        assert case.get("merge_existing_entry", False) is False
        assert case.get("winner_modified", False) is False
        assert case.get("candidate_reused", False) is False
        return

    if cluster == "cross_project_cases":
        assert case["shared_cache_key"] == fixture["cache_key"]
        if "consumer_ledger_after" in case:
            assert case["consumer_ledger_after"] == case["commit_order"]
            assert case["shared_transactions_serialized"] is True
        else:
            assert case["consumer_ledger_after_rollback"] == case[
                "consumer_ledger_before_failing_transaction"
            ]
            assert case["project_alpha_targets_unchanged"] is True
        return

    if cluster == "dry_run_cases":
        assert case["forbidden_persistent_effects"]
        if case["name"] == "compiled-cache-miss-is-read-only":
            outcomes = {
                status.dry_run_outcome
                for status in (
                    cache.CacheInspection(cache.CacheEntryStatus.HIT, "vector"),
                    cache.CacheInspection(cache.CacheEntryStatus.MISS, "vector"),
                    cache.CacheInspection(
                        cache.CacheEntryStatus.UNTRUSTED_PROVENANCE,
                        "vector",
                    ),
                    cache.CacheInspection(cache.CacheEntryStatus.CORRUPT, "vector"),
                    cache.CacheInspection(cache.CacheEntryStatus.UNSUPPORTED, "vector"),
                )
            }
            assert outcomes == set(case["reported_build_outcomes"])
            assert not set(case["allowed_go_commands"]) & set(case["forbidden_go_commands"])
            assert case["operation_private_state_after"] == "absent"
        return

    if cluster == "gc_cases":
        assert gc.BUILD_GRACE_SECONDS > 0
        if "sweep_requires" in case:
            assert case["only_lock"] == "manager-home-mutation-lock"
            assert set(case["compiled_cache_mark_roots"]) <= set(case["mark_roots"])
            assert case["receipt_content_alone_is_live_reference"] is False
        else:
            assert case["successful_installation_rolled_back"] is False
            assert case["manager_home_lock"] is True
        return

    if cluster == "launcher_cases":
        assert case["platforms"] == ["unix", "windows"]
        assert case["forward_arguments"] is True
        assert case["preserve_exit_status"] is True
        assert case["preserve_inherited_path"] is True
        assert case["required_path_roles"] == [
            "command_directory",
            "implementation_runtime",
            "system_dependencies",
        ]
        return

    if cluster == "planning_cases":
        failure = case["failure_at_any_gate"]
        assert failure == {
            "cache_lookup": False,
            "go_commands": [],
            "persistent_mutations": [],
        }
        assert case["then"][-2:] == ["go-list", "go-build"]
        assert case["result"] == "build-eligible"
        return

    if cluster == "private_build_cases":
        if "manager_home_lock_acquired" in case:
            assert case["manager_home_lock_acquired"] is False
        else:
            assert case["manager_home_lock_during_build"] is False
        if "builds" in case:
            assert all(build["artifact_verified"] for build in case["builds"])
            assert case["shared_mutations_before_all_verified"] == []
            assert case["artifacts_executed"] is False
        else:
            assert case["persistent_state_after"] == case["persistent_state_before"]
            assert "cache-publication" in case["forbidden_effects"]
        return

    if cluster == "recovery_cases":
        if "manager_home_lock" in case:
            assert case["manager_home_lock"] is True
        else:
            assert case["backups_retained_until_recovery_succeeds"] is True
        if "journal_state" in case:
            assert case["successful_project_consumers_after"] == case[
                "successful_project_consumers_before"
            ]
            assert case["scan_scope"] == "all-incomplete-journals"
        else:
            assert case["private_builds_verified"] is True
            assert case["recovery_before_build"] is False
        return

    if cluster == "repair_cases":
        conditions = case["independent_conditions"]
        assert len(conditions) == len(set(conditions)) == 5
        assert {value.split("-", 1)[0] for value in conditions} == {
            "corrupt",
            "missing",
            "untrusted",
            "wrong",
        }
        assert "adopt-candidate" in case["forbidden_shortcuts"]
        assert case["required_pipeline"][-1] == "journaled-commit"
        return

    if cluster == "status_cases":
        build_input = metadata.parse_build_input(fixture["build_input"])
        assert metadata.cache_key(build_input) == fixture["cache_key"]
        if case["result"] == "current":
            receipt = metadata.parse_receipt(fixture["stored_receipt"])
            assert receipt.input == build_input
            assert case["mutations"] == []
        else:
            assert case["adopt"] is False
            assert case["quarantine"] is False
            assert case["repair"] is False
            assert case["independent_conditions"]
        assert case["artifact_executed"] is False
        return

    if cluster == "transaction_cases":
        if "input_project_identities" in case:
            assert sorted(
                case["input_project_identities"],
                key=lambda value: value.encode("utf-8"),
            ) == case["expected_project_lock_order"]
            assert case["cache_build_lock_released_before_home_lock"] is True
        elif "expected_commit_order" in case:
            classes = [item.split("/", 1)[0] for item in case["expected_commit_order"]]
            assert classes[-1] == "consumer-ledger"
            assert case["consumer_ledger_committed_last"] is True
        else:
            assert case["expected_restore_order"] == list(reversed(case["commit_order"]))
            assert case["unknown_state_overwritten"] is False
            assert case["existing_valid_cache_entries_modified"] is False
        return

    if cluster == "upgrade_cases":
        assert case["selection"] in {"one", "all", "global"}
        if "fetch" in case:
            assert case["fetch"] == ["direct", "transitive"]
            assert case["exclude"] == ["unrelated"]
        else:
            assert case["deduplicate"] is True
        return

    raise AssertionError(f"no manager lifecycle adapter for {cluster!r}")
