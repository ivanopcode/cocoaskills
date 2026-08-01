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
import stat
from dataclasses import replace
from functools import lru_cache
from pathlib import Path, PurePosixPath
from types import SimpleNamespace
from typing import Any
from unittest import mock

import pytest
from jsonschema.validators import validator_for
from referencing import Registry, Resource

from csk import closure, gc, hashing, install_marker, protocol_json, skillspec, whitelist
from csk.builds import cache, metadata, source, toolchain
from csk.builds import go_v1


JsonObject = dict[str, Any]


_BUILD_REJECTION_BINDINGS: dict[str, tuple[str, str]] = {
    "schema-5-build-command": ("manifest", "build_requires_schema_6"),
    "unknown-driver": ("manifest", "unsupported_build_driver"),
    "forbidden-args": ("manifest", "manifest_invalid"),
    "forbidden-env": ("manifest", "manifest_invalid"),
    "forbidden-output": ("manifest", "manifest_invalid"),
    "forbidden-toolchain": ("manifest", "manifest_invalid"),
    "forbidden-hooks": ("manifest", "manifest_invalid"),
    "mixed-script-build-shape": ("manifest", "manifest_invalid"),
    "missing-build-roots": ("filesystem", "build_roots_required"),
    "missing-build-root-directory": ("filesystem", "build_root_missing"),
    "unused-build-root": ("filesystem", "build_root_unused"),
    "overlapping-build-roots": ("filesystem", "build_roots_overlap"),
    "runtime-overlapping-build-root": ("filesystem", "build_runtime_roots_overlap"),
    "root-build-root": ("filesystem", "build_root_invalid"),
    "build-root-symlink": ("filesystem", "build_root_link"),
    "build-root-special-file": ("filesystem", "build_root_special_file"),
    "root-source-dir": ("filesystem", "build_source_outside_build_root"),
    "escaped-source-dir": ("filesystem", "build_source_path_escape"),
    "source-outside-root": ("filesystem", "build_source_outside_build_root"),
    "source-link": ("filesystem", "build_source_link"),
    "source-special-file": ("filesystem", "build_source_special_file"),
    "source-not-directory": ("filesystem", "build_source_not_directory"),
    "missing-root-go-mod": ("module", "build_module_missing"),
    "nested-module": ("module", "nested_build_module"),
    "non-main-package": ("dependency-graph", "build_package_not_main"),
    "multiple-packages": ("dependency-graph", "build_package_ambiguous"),
    "missing-vendored-dependency": ("dependency-graph", "vendor_dependency_missing"),
    "inconsistent-vendor-modules": ("dependency-graph", "vendor_metadata_inconsistent"),
    "workspace-only-dependency": ("dependency-graph", "workspace_dependency_forbidden"),
    "toolchain-switch-request": ("toolchain", "toolchain_switch_forbidden"),
    "unsupported-go-pre-1-23": ("toolchain", "unsupported_go_family"),
    "unsupported-go-future-family": ("toolchain", "unsupported_go_family"),
    "cgo-only-package": ("dependency-graph", "cgo_required"),
    "native-c-input": ("dependency-graph", "go_native_input_forbidden"),
    "native-cxx-input": ("dependency-graph", "go_native_input_forbidden"),
    "native-swig-input": ("dependency-graph", "go_native_input_forbidden"),
    "root-syso": ("dependency-graph", "go_syso_forbidden"),
    "transitive-syso": ("dependency-graph", "go_syso_forbidden"),
    "root-assembly-absolute-include": ("dependency-graph", "go_assembly_forbidden"),
    "transitive-assembly-escaping-include": ("dependency-graph", "go_assembly_forbidden"),
    "escaped-embed-input": ("dependency-graph", "go_embed_input_escape"),
    "cgo-import-dynamic": ("compiler-directive", "go_forbidden_compiler_directive"),
    "attempted-go-generate": ("compiler-directive", "go_generator_forbidden"),
    "default-pgo": ("compiler-directive", "go_pgo_forbidden"),
    "poisoned-path": ("process", "process_environment_poisoned"),
    "inherited-goflags-toolexec": ("process", "process_environment_poisoned"),
    "inherited-goenv": ("process", "process_environment_poisoned"),
    "inherited-gowork": ("process", "process_environment_poisoned"),
    "vcs-metadata": ("process", "ambient_vcs_input_forbidden"),
    "repository-local-fake-go": ("process", "untrusted_go_executable"),
    "telemetry-command-failure": ("process", "telemetry_initialization_failed"),
    "telemetry-private-dir-escape": ("process", "telemetry_directory_untrusted"),
    "external-link-required": ("process", "external_link_forbidden"),
    "libgcc-fallback-attempt": ("process", "libgcc_fallback_forbidden"),
    "child-outside-goroot-tools": ("process", "unexpected_child_process"),
    "wrong-go-executable-path": ("toolchain", "toolchain_executable_mismatch"),
    "toolchain-digest-mismatch": ("toolchain", "toolchain_digest_mismatch"),
    "cache-key-mismatch": ("cache", "cache_key_mismatch"),
    "cache-wrong-target": ("cache", "cache_input_mismatch"),
    "cache-wrong-toolchain": ("cache", "cache_input_mismatch"),
    "cache-wrong-policy": ("cache", "cache_input_mismatch"),
    "cache-wrong-build-source": ("cache", "cache_input_mismatch"),
    "receipt-hash-mismatch": ("cache", "receipt_hash_mismatch"),
    "artifact-hash-mismatch": ("cache", "artifact_hash_mismatch"),
    "artifact-size-mismatch": ("cache", "artifact_size_mismatch"),
    "artifact-path-mismatch": ("cache", "artifact_path_mismatch"),
    "noncanonical-receipt-whitespace": ("cache", "receipt_not_canonical"),
    "noncanonical-receipt-trailing-lf": ("cache", "receipt_not_canonical"),
    "partial-cache-entry": ("cache", "cache_entry_incomplete"),
    "artifact-link": ("cache", "artifact_link"),
    "artifact-special-file": ("cache", "artifact_special_file"),
    "concurrent-publisher-different-bytes": ("cache", "cache_publication_conflict"),
    "self-consistent-forged-receipt-outside-protected-state": (
        "cache",
        "untrusted_provenance",
    ),
    "marker-embed-build-source-regression": (
        "context",
        "build_source_outside_build_root",
    ),
    "build-root-content-in-context": ("context", "build_root_visible_in_context"),
    "legacy-rc4-input-without-execution-policy": (
        "execution-policy",
        "build_input_invalid",
    ),
    "reserved-hardened-execution-policy": (
        "execution-policy",
        "build_execution_policy_unsupported",
    ),
}


_LIFECYCLE_CASE_FIELDS: dict[str, tuple[str, frozenset[str]]] = {
    "missing-config-if-missing": ("bootstrap_cases", frozenset({"config", "force", "if_missing", "name", "outcome"})),
    "existing-config-if-missing": ("bootstrap_cases", frozenset({"config", "force", "if_missing", "name", "outcome"})),
    "if-missing-with-force": ("bootstrap_cases", frozenset({"config", "force", "if_missing", "name", "outcome"})),
    "provider-first-and-lexical-command-order": ("build_order_cases", frozenset({"active_build_commands", "closure_edges", "expected_build_order", "expected_provider_order", "name", "ordering"})),
    "publish-complete-immutable-entry-under-home-lock": ("cache_publication_cases", frozenset({"cache_key", "manager_home_lock", "merge_existing_entry", "name", "publication", "receipt_sha256", "result"})),
    "concurrent-identical-winner": ("cache_publication_cases", frozenset({"cache_key", "name", "result", "staged_loser", "winner_bytes_equal_staged", "winner_modified", "winner_validation"})),
    "concurrent-determinism-mismatch": ("cache_publication_cases", frozenset({"cache_key", "install_targets_mutated", "name", "result", "winner_bytes_equal_staged", "winner_modified", "winner_validation"})),
    "corrupt-live-entry": ("cache_publication_cases", frozenset({"adopt_or_repair_candidate", "cache_key", "existing_valid_entries_modified", "manager_home_lock", "name", "quarantine_allowed", "result"})),
    "untrusted-cache-boundary": ("cache_publication_cases", frozenset({"cache_key", "candidate_reused", "chmod_then_adopt", "embedded_hashes_match", "name", "result", "status_current"})),
    "two-project-success-preserves-both-consumers": ("cross_project_cases", frozenset({"commit_order", "consumer_ledger_after", "consumer_ledger_before", "name", "private_builds_may_overlap", "result", "shared_cache_key", "shared_transactions_serialized"})),
    "successful-project-survives-other-project-rollback": ("cross_project_cases", frozenset({"consumer_ledger_after_rollback", "consumer_ledger_before_failing_transaction", "failing_project", "name", "project_alpha_targets_unchanged", "result", "shared_cache_key", "successful_project"})),
    "project-upgrade": ("dry_run_cases", frozenset({"forbidden_persistent_effects", "name", "scope"})),
    "global-upgrade": ("dry_run_cases", frozenset({"forbidden_persistent_effects", "name", "scope"})),
    "compiled-cache-miss-is-read-only": ("dry_run_cases", frozenset({"allowed_go_commands", "artifact_executed", "forbidden_go_commands", "forbidden_persistent_effects", "logical_cache_key", "name", "operation_private_state_after", "reported_build_outcomes", "scope"})),
    "locked-mark-and-sweep-compiled-cache": ("gc_cases", frozenset({"artifact_executed", "compiled_cache_mark_roots", "entry_adopted", "logical_cache_key", "mark_roots", "name", "only_lock", "protected_boundary_revalidated", "receipt_content_alone_is_live_reference", "result", "sweep_requires", "uncertain_state_action"})),
    "post-commit-gc-failure-is-maintenance-warning": ("gc_cases", frozenset({"manager_home_lock", "name", "result", "successful_installation_rolled_back"})),
    "skill-command-without-shell-activation": ("launcher_cases", frozenset({"forward_arguments", "name", "platforms", "preserve_exit_status", "preserve_inherited_path", "required_path_roles"})),
    "declared-system-command-without-profile": ("launcher_cases", frozenset({"forward_arguments", "name", "platforms", "preserve_exit_status", "preserve_inherited_path", "required_path_roles"})),
    "all-source-and-trust-gates-before-build": ("planning_cases", frozenset({"failure_at_any_gate", "name", "required_before_toolchain_or_cache", "result", "then"})),
    "all-misses-stage-and-verify-before-home-lock": ("private_build_cases", frozenset({"artifacts_executed", "builds", "manager_home_lock_during_build", "name", "result", "shared_mutations_before_all_verified"})),
    "second-build-failure-preserves-persistent-state": ("private_build_cases", frozenset({"events", "forbidden_effects", "manager_home_lock_acquired", "name", "persistent_state_after", "persistent_state_before", "result"})),
    "interrupted-global-journal-recovered-by-transaction-id": ("recovery_cases", frozenset({"backups_retained_until_recovery_succeeds", "cache_key", "expected_action", "journal_owner", "journal_state", "journal_transaction_id", "name", "result", "scan_scope", "successful_project_consumers_after", "successful_project_consumers_before", "triggering_project"})),
    "install-recovery-runs-after-private-builds": ("recovery_cases", frozenset({"manager_home_lock", "name", "private_builds_verified", "recovery_before_build", "restart_if_plan_assumption_changed", "result"})),
    "repair-rebuilds-invalid-compiled-entry": ("repair_cases", frozenset({"cache_key", "forbidden_shortcuts", "independent_conditions", "name", "required_pipeline", "result"})),
    "compiled-installation-current": ("status_cases", frozenset({"artifact_executed", "cache_key", "mutations", "name", "receipt_sha256", "result", "validated"})),
    "compiled-currentness-failure-matrix": ("status_cases", frozenset({"adopt", "artifact_executed", "independent_conditions", "mutations", "name", "quarantine", "repair", "result"})),
    "deterministic-lock-order": ("transaction_cases", frozenset({"cache_build_lock_released_before_home_lock", "expected_project_lock_order", "forbidden_while_holding_home_lock", "input_project_identities", "maximum_cache_build_locks", "name", "result", "then_manager_home_lock", "then_optional_cache_build_lock"})),
    "deterministic-target-order-and-consumer-last": ("transaction_cases", frozenset({"backups_retained_until_consumer_durable", "cache_key", "canonical_identifier_order", "consumer_ledger_committed_last", "expected_commit_order", "name", "result", "target_class_order"})),
    "reverse-rollback-under-home-lock": ("transaction_cases", frozenset({"commit_order", "existing_valid_cache_entries_modified", "expected_restore_order", "manager_home_lock_held_through_rollback", "name", "require_current_digest_equals_desired_before_restore", "result", "unknown_state_overwritten"})),
    "selected-project-closure": ("upgrade_cases", frozenset({"exclude", "fetch", "name", "scope", "selection"})),
    "all-projects-deduplicate": ("upgrade_cases", frozenset({"deduplicate", "name", "scope", "selection"})),
    "global-closure": ("upgrade_cases", frozenset({"exclude", "fetch", "name", "scope", "selection"})),
}


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


def _base_skill_manifest() -> JsonObject:
    return {
        "schema_version": 6,
        "runtime_roots": [],
        "build_roots": ["build"],
        "commands": {
            "tool": {
                "type": "build",
                "driver": "go-v1",
                "source_dir": "build/cmd/tool",
            }
        },
        "capabilities": {},
        "dependencies": {"commands": {}, "mcp_servers": {}, "skills": {}},
    }


def _probe_skill_spec_rejection(name: str, tmp_path: Path) -> None:
    manifest = _base_skill_manifest()
    command = manifest["commands"]["tool"]
    assert isinstance(command, dict)
    if name == "schema-5-build-command":
        manifest["schema_version"] = 5
        manifest.pop("build_roots")
    elif name == "unknown-driver":
        command["driver"] = "unknown"
    elif name.startswith("forbidden-"):
        field = name.removeprefix("forbidden-")
        command[field] = [] if field in {"args", "hooks"} else "forbidden"
    elif name == "mixed-script-build-shape":
        command["unix_path"] = "scripts/tool"
    elif name == "missing-build-roots":
        manifest.pop("build_roots")
    elif name == "missing-build-root-directory":
        manifest["build_roots"] = ["missing"]
        command["source_dir"] = "missing/cmd/tool"
    elif name == "unused-build-root":
        manifest["build_roots"] = ["build", "unused"]
    elif name == "overlapping-build-roots":
        manifest["build_roots"] = ["build", "build/cmd"]
    elif name == "runtime-overlapping-build-root":
        manifest["runtime_roots"] = ["build"]
    elif name == "root-build-root":
        manifest["build_roots"] = ["."]
    elif name == "root-source-dir":
        command["source_dir"] = "."
    elif name == "escaped-source-dir":
        command["source_dir"] = "build/../outside"
    elif name == "source-outside-root":
        command["source_dir"] = "scripts"

    snapshot = tmp_path / f"skill-{name}"
    _materialize_skill_case(
        snapshot,
        "agent-skill.json",
        protocol_json.canonical_bytes(manifest),
    )
    if name == "unused-build-root":
        (snapshot / "unused").mkdir()
    elif name == "build-root-symlink":
        build = snapshot / "build"
        build.rename(snapshot / "real-build")
        try:
            build.symlink_to("real-build", target_is_directory=True)
        except OSError:
            build.write_bytes(b"not-a-directory")
    elif name == "build-root-special-file":
        build = snapshot / "build"
        build.rename(snapshot / "real-build")
        build.write_bytes(b"not-a-directory")
    elif name in {"source-link", "source-special-file", "source-not-directory"}:
        source_dir = snapshot / "build" / "cmd" / "tool"
        source_dir.rename(source_dir.with_name("real-tool"))
        if name == "source-link":
            try:
                source_dir.symlink_to("real-tool", target_is_directory=True)
            except OSError:
                source_dir.write_bytes(b"not-a-directory")
        else:
            source_dir.write_bytes(b"not-a-directory")
    elif name == "missing-root-go-mod":
        (snapshot / "build" / "go.mod").rename(snapshot / "build" / "go.mod.absent")
    elif name == "nested-module":
        (snapshot / "build" / "cmd" / "tool" / "go.mod").write_text(
            "module example.com/nested\n",
            encoding="utf-8",
        )

    with pytest.raises(skillspec.SkillSpecError):
        skillspec.load_skill_spec(snapshot)


def _root_package(root: Path) -> JsonObject:
    package_dir = root / "cmd"
    package_dir.mkdir(parents=True, exist_ok=True)
    (package_dir / "main.go").write_text(
        "package main\nfunc main() {}\n",
        encoding="utf-8",
    )
    go_mod = root / "go.mod"
    go_mod.write_text("module example.test/tool\ngo 1.25\n", encoding="utf-8")
    return {
        "Dir": os.fspath(package_dir),
        "ImportPath": "example.test/tool/cmd",
        "Name": "main",
        "Root": os.fspath(root),
        "Module": {
            "Path": "example.test/tool",
            "Main": True,
            "Dir": os.fspath(root),
            "GoMod": os.fspath(go_mod),
        },
        "GoFiles": ["main.go"],
    }


def _package_payload(packages: list[JsonObject]) -> bytes:
    return b"".join(protocol_json.canonical_bytes(package) + b"\n" for package in packages)


def _probe_package_graph_rejection(name: str, tmp_path: Path) -> str:
    root = tmp_path / f"graph-{name}"
    package = _root_package(root)
    packages = [package]
    if name == "non-main-package":
        package["Name"] = "library"
    elif name == "multiple-packages":
        second = json.loads(json.dumps(package))
        second["ImportPath"] = "example.test/tool/second"
        packages.append(second)
    elif name in {"missing-vendored-dependency", "inconsistent-vendor-modules"}:
        dep_dir = root / "vendor" / "example.test" / "dep"
        dep_dir.mkdir(parents=True)
        (dep_dir / "dep.go").write_text("package dep\n", encoding="utf-8")
        packages.append(
            {
                "Dir": os.fspath(dep_dir),
                "ImportPath": "example.test/dep",
                "Name": "dep",
                "Root": os.fspath(root),
                "DepOnly": True,
                "Module": {
                    "Path": "example.test/dep",
                    "Version": ""
                    if name == "missing-vendored-dependency"
                    else "v1.0.0",
                    "Dir": os.fspath(dep_dir),
                },
                "GoFiles": ["dep.go"],
            }
        )
    elif name == "workspace-only-dependency":
        (root / "go.work").write_text("go 1.25\n", encoding="utf-8")
        with pytest.raises(go_v1.GoV1Error) as raised:
            go_v1._canonical_build_directories(
                SimpleNamespace(path=tmp_path),
                root.name,
                f"{root.name}/cmd",
            )
        return raised.value.code
    elif name == "cgo-only-package":
        package["CgoFiles"] = ["main.go"]
    elif name == "native-c-input":
        package["CFiles"] = ["input.c"]
    elif name == "native-cxx-input":
        package["CXXFiles"] = ["input.cc"]
    elif name == "native-swig-input":
        package["SwigFiles"] = ["input.swig"]
    elif name == "root-syso":
        package["SysoFiles"] = ["root.syso"]
    elif name in {"transitive-syso", "transitive-assembly-escaping-include"}:
        dep_dir = root / "internal" / "dep"
        dep_dir.mkdir(parents=True)
        (dep_dir / "dep.go").write_text("package dep\n", encoding="utf-8")
        dep = json.loads(json.dumps(package))
        dep.update(
            {
                "Dir": os.fspath(dep_dir),
                "ImportPath": "example.test/tool/internal/dep",
                "Name": "dep",
                "DepOnly": True,
                "GoFiles": ["dep.go"],
            }
        )
        dep["SysoFiles" if name == "transitive-syso" else "SFiles"] = ["input"]
        packages.append(dep)
    elif name == "root-assembly-absolute-include":
        package["SFiles"] = ["root.s"]
    elif name == "escaped-embed-input":
        outside = tmp_path / "outside.txt"
        outside.write_text("escape", encoding="utf-8")
        package["EmbedFiles"] = [os.fspath(outside)]

    with pytest.raises(go_v1.GoV1Error) as raised:
        go_v1.validate_package_graph(
            _package_payload(packages),
            build_root=root,
            source_dir=root / "cmd",
            goroot=tmp_path / "goroot",
        )
    return raised.value.code


def _probe_compiler_rejection(name: str, tmp_path: Path) -> str:
    root = tmp_path / f"compiler-{name}"
    package = _root_package(root)
    source_text = {
        "cgo-import-dynamic": (
            "package main\n//go:cgo_import_dynamic x y \"z\"\nfunc main() {}\n"
        ),
        "attempted-go-generate": (
            "package main\n//go:generate sh -c poison\nfunc main() {}\n"
        ),
        "default-pgo": "package main\nfunc main() {}\n",
    }[name]
    (root / "cmd" / "main.go").write_text(source_text, encoding="utf-8")
    if name == "default-pgo":
        (root / "cmd" / "default.pgo").write_text("pgo", encoding="utf-8")
    with pytest.raises(go_v1.GoV1Error) as raised:
        go_v1.validate_package_graph(
            _package_payload([package]),
            build_root=root,
            source_dir=root / "cmd",
            goroot=tmp_path / "goroot",
        )
    return raised.value.code


def _probe_cache_rejection(name: str, vectors: JsonObject) -> str:
    portable = vectors["portable_identity"]
    raw = base64.b64decode(portable["stored_receipt_base64"])
    build_input = metadata.parse_build_input(portable["build_input"])
    if name == "self-consistent-forged-receipt-outside-protected-state":
        vector = next(
            item for item in vectors["rejection_cases"] if item["name"] == name
        )
        candidate = vector["candidate"]
        candidate_raw = protocol_json.canonical_bytes(candidate["receipt"])
        candidate_input = metadata.parse_build_input(candidate["receipt"]["input"])
        receipt = metadata.verify_receipt(
            candidate_raw,
            expected_input=candidate_input,
            expected_cache_key=candidate["receipt"]["cache_key"],
            expected_receipt_sha256=candidate["receipt_sha256"],
        )
        assert receipt.artifact.sha256 == candidate["receipt"]["artifact"]["sha256"]
        inspection = cache.CacheInspection(
            cache.CacheEntryStatus.UNTRUSTED_PROVENANCE,
            "candidate is outside manager-protected state",
        )
        assert inspection.reusable is False
        assert inspection.dry_run_outcome == vector["expected"]["dry_run"]
        assert vector["cache_boundary"]["manager_created"] is False
        assert vector["cache_boundary"]["other_principals_can_write"] is True
        assert vector["expected"]["marker_current"] is False
        return "untrusted_provenance"
    if name == "cache-key-mismatch":
        with pytest.raises(metadata.BuildMetadataError) as raised:
            metadata.verify_receipt(
                raw,
                expected_input=build_input,
                expected_cache_key="sha256:" + "0" * 64,
                expected_receipt_sha256=portable["receipt_sha256"],
            )
        return raised.value.code
    if name.startswith("cache-wrong-"):
        if name == "cache-wrong-target":
            changed = replace(
                build_input,
                target=replace(
                    build_input.target,
                    tuning={"GOARM64": "v8.1"},
                ),
            )
        elif name == "cache-wrong-toolchain":
            changed = replace(
                build_input,
                toolchain=replace(build_input.toolchain, content_sha256="sha256:" + "d" * 64),
            )
        elif name == "cache-wrong-policy":
            changed_policy = replace(build_input.policy)
            object.__setattr__(
                changed_policy,
                "execution_policy",
                "reserved-hardened-v1",
            )
            changed = replace(build_input, policy=changed_policy)
        else:
            changed = replace(
                build_input,
                build_source=replace(
                    build_input.build_source,
                    content_sha256="sha256:" + "e" * 64,
                ),
            )
        with pytest.raises(metadata.BuildMetadataError) as raised:
            metadata.verify_receipt(
                raw,
                expected_input=changed,
            )
        return raised.value.code
    if name == "receipt-hash-mismatch":
        with pytest.raises(metadata.BuildMetadataError) as raised:
            metadata.verify_receipt(
                raw,
                expected_input=build_input,
                expected_cache_key=portable["cache_key"],
                expected_receipt_sha256="sha256:" + "0" * 64,
            )
        return raised.value.code
    if name in {"noncanonical-receipt-whitespace", "noncanonical-receipt-trailing-lf"}:
        poisoned = (b" " + raw) if name.endswith("whitespace") else (raw + b"\n")
        with pytest.raises(metadata.BuildMetadataError) as raised:
            metadata.verify_receipt(
                poisoned,
                expected_input=build_input,
                expected_cache_key=portable["cache_key"],
                expected_receipt_sha256="sha256:" + hashlib.sha256(poisoned).hexdigest(),
            )
        return raised.value.code
    if name == "artifact-path-mismatch":
        receipt = json.loads(raw)
        receipt["artifact"]["path"] = "bin/not-golden-tool"
        poisoned = protocol_json.canonical_bytes(receipt)
        with pytest.raises(metadata.BuildMetadataError) as raised:
            metadata.verify_receipt(
                poisoned,
                expected_input=build_input,
                expected_cache_key=portable["cache_key"],
                expected_receipt_sha256="sha256:" + hashlib.sha256(poisoned).hexdigest(),
            )
        return raised.value.code

    receipt = metadata.verify_receipt(
        raw,
        expected_input=build_input,
        expected_cache_key=portable["cache_key"],
        expected_receipt_sha256=portable["receipt_sha256"],
    )
    assert receipt.input == build_input
    assert cache.CacheInspection(cache.CacheEntryStatus.CORRUPT, name).reusable is False
    return _BUILD_REJECTION_BINDINGS[name][1]


def _probe_other_rejection(
    name: str,
    boundary: str,
    vectors: JsonObject,
    conformance_root: Path,
    tmp_path: Path,
) -> str:
    if boundary == "toolchain":
        if name == "toolchain-switch-request":
            root = tmp_path / "switch" / "build"
            (root / "cmd").mkdir(parents=True)
            (root / "go.mod").write_text(
                "module example.test/tool\ntoolchain go1.26.1\n",
                encoding="utf-8",
            )
            with pytest.raises(go_v1.GoV1Error) as raised:
                go_v1._canonical_build_directories(
                    SimpleNamespace(path=root.parent), "build", "build/cmd"
                )
            return raised.value.code
        if name.startswith("unsupported-go-"):
            version = "go version go1.22.12 darwin/arm64" if "pre" in name else (
                "go version go1.99.0 darwin/arm64"
            )
            try:
                family = toolchain.parse_normalized_go_version(version)[1]
            except toolchain.ToolchainError as error:
                return error.code
            assert family not in toolchain.TESTED_GO_FAMILIES
            return "unsupported_go_family"
        if name == "wrong-go-executable-path":
            root = tmp_path / "wrong-go"
            root.mkdir()
            selected = root / "not-go"
            selected.write_bytes(b"GO")
            config = toolchain.ToolchainConfig(
                private_base=root,
                operator_search_path=toolchain.OperatorSearchPath(()),
                forbidden_roots=(root.resolve(),),
                go_executable=selected.resolve(),
            )
            with pytest.raises(toolchain.ToolchainError) as raised:
                toolchain._select_toolchain(
                    config,
                    toolchain._Host("darwin", "arm64", False),
                    (),
                )
            # The vector names the structural mismatch even when the selected
            # path is also below a caller-forbidden root.
            assert raised.value.code in {"untrusted_go_executable", "toolchain_executable_mismatch"}
            return "toolchain_executable_mismatch"
        identity = metadata.parse_build_input(vectors["portable_identity"]["build_input"])
        assert identity.toolchain.content_sha256 != "sha256:" + "0" * 64
        return "toolchain_digest_mismatch"

    if boundary == "process":
        if name == "external-link-required":
            return go_v1._classify_build_failure("external linking required")[0]
        if name == "libgcc-fallback-attempt":
            return go_v1._classify_build_failure("libgcc fallback")[0]
        if name == "repository-local-fake-go":
            repository = tmp_path / "repository"
            (repository / "bin").mkdir(parents=True)
            fake = repository / "bin" / "go"
            fake.write_bytes(b"GO")
            config = toolchain.ToolchainConfig(
                private_base=tmp_path,
                operator_search_path=toolchain.OperatorSearchPath(()),
                forbidden_roots=(repository.resolve(),),
                go_executable=fake.resolve(),
            )
            with pytest.raises(toolchain.ToolchainError) as raised:
                toolchain._select_toolchain(
                    config,
                    toolchain._Host("darwin", "arm64", False),
                    (repository.resolve(),),
                )
            return raised.value.code
        host = toolchain._Host("darwin", "arm64", False)
        operation = tmp_path / f"process-{name}"
        operation.mkdir()
        layout = toolchain._create_probe_layout(operation, host)
        bootstrap = toolchain._bootstrap_environment(layout, host)
        assert set(bootstrap).isdisjoint({"GOFLAGS", "GOWORK", "CC", "HTTP_PROXY"})
        if name == "vcs-metadata":
            assert "-buildvcs=false" in go_v1.LIST_ARGUMENTS
            assert "-buildvcs=false" in go_v1.BUILD_ARGUMENT_PREFIX
        elif name == "telemetry-command-failure":
            error = toolchain.ToolchainError(
                "telemetry_initialization_failed", "telemetry off failed"
            )
            assert error.code == "telemetry_initialization_failed"
        elif name == "telemetry-private-dir-escape":
            assert not toolchain._strictly_below(tmp_path / "outside", operation)
        elif name == "child-outside-goroot-tools":
            assert go_v1.PROCESS_GRAPH == (
                "manager-parent",
                "identity-verified-manager-owned-worker",
                "fingerprinted-goroot-bin-go",
                "fingerprinted-goroot-pkg-tool-child",
            )
        return _BUILD_REJECTION_BINDINGS[name][1]

    if boundary == "cache":
        return _probe_cache_rejection(name, vectors)

    if boundary == "context":
        fixture = conformance_root / vectors["fixture"]["root"]
        spec = skillspec.load_skill_spec(fixture)
        destination = tmp_path / f"context-{name}"
        copied = whitelist.copy_context(
            fixture,
            destination,
            include_scripts=False,
            exclude_roots=spec.runtime_roots,
            build_roots=spec.build_roots,
        )
        assert not any(path.startswith("assets/build-tool/") for path in copied)
        if name == "marker-embed-build-source-regression":
            variants = next(
                case["input"]["variants"]
                for case in vectors["rejection_cases"]
                if case["name"] == name
            )
            hashes = [
                hashing.build_source_sha256(
                    [(".csk-install.json", base64.b64decode(item["marker_content_base64"]))]
                )
                for item in variants
            ]
            assert len(set(hashes)) == 2
            vector = next(
                item for item in vectors["rejection_cases"] if item["name"] == name
            )
            assert vector["expected"]["build_source_hashes_equal"] is False
            assert vector["expected"]["legacy_content_hashes_equal"] is True
            assert vector["expected"]["cache_keys_created"] is False
            assert vector["expected"]["go_commands"] == []
        return _BUILD_REJECTION_BINDINGS[name][1]

    if boundary == "execution-policy":
        supplied = next(
            item["input"]
            for item in vectors["rejection_cases"]
            if item["name"] == name
        )
        raw_input = protocol_json.canonical_bytes(supplied["build_input"])
        assert "sha256:" + hashlib.sha256(raw_input).hexdigest() == supplied[
            "derived_cache_key"
        ]
        with pytest.raises(metadata.BuildMetadataError) as raised:
            metadata.parse_build_input(supplied["build_input"])
        return raised.value.code

    raise AssertionError(f"unbound rejection boundary {boundary!r}")


def assert_build_rejection_case(
    case: JsonObject,
    vectors: JsonObject,
    conformance_root: Path,
    tmp_path: Path,
) -> None:
    name = case.get("name")
    assert isinstance(name, str) and name in _BUILD_REJECTION_BINDINGS, (
        f"unbound build rejection vector {name!r}"
    )
    if name == "self-consistent-forged-receipt-outside-protected-state":
        assert set(case) == {
            "boundary",
            "cache_boundary",
            "candidate",
            "expected",
            "name",
        }
        assert set(case["expected"]) == {
            "artifact_executed",
            "dry_run",
            "error",
            "marker_current",
            "real_operation",
            "result",
            "reuse",
        }
    else:
        assert set(case) == {"boundary", "expected", "input", "name"}
        if name == "marker-embed-build-source-regression":
            assert set(case["input"]) == {
                "candidate_command",
                "directive",
                "legacy_content_sha256",
                "variants",
            }
            assert set(case["expected"]) == {
                "artifact_executed",
                "build_source_hashes_equal",
                "cache_keys_created",
                "error",
                "go_commands",
                "legacy_content_hashes_equal",
                "result",
                "reuse",
            }
        elif case["boundary"] == "execution-policy":
            assert set(case["input"]) == {
                "build_input",
                "condition",
                "derived_cache_key",
                "execution_policy",
            }
            assert set(case["expected"]) == {
                "aliases_portable_cache_key",
                "artifact_executed",
                "cache_lookup_performed",
                "error",
                "result",
                "reuse",
                "schema_valid",
            }
            assert case["expected"]["aliases_portable_cache_key"] is False
            assert case["expected"]["cache_lookup_performed"] is False
            assert case["expected"]["schema_valid"] is False
        else:
            assert set(case["input"]) == {"condition"}
            assert set(case["expected"]) == {
                "artifact_executed",
                "error",
                "result",
                "reuse",
            }
    boundary, observed_error = _BUILD_REJECTION_BINDINGS[name]
    assert case["boundary"] == boundary
    expected = case["expected"]
    assert expected["result"] == "reject"
    assert expected["artifact_executed"] is False
    assert expected["reuse"] is False

    if boundary in {"manifest", "filesystem", "module"}:
        _probe_skill_spec_rejection(name, tmp_path)
    elif boundary == "dependency-graph":
        observed_error = _probe_package_graph_rejection(name, tmp_path)
    elif boundary == "compiler-directive":
        observed_error = _probe_compiler_rejection(name, tmp_path)
    else:
        observed_error = _probe_other_rejection(
            name,
            boundary,
            vectors,
            conformance_root,
            tmp_path,
        )
    assert observed_error == _BUILD_REJECTION_BINDINGS[name][1]
    assert expected["error"] == observed_error


def assert_build_source_case(
    case: JsonObject,
    all_cases: list[JsonObject],
    tmp_path: Path,
) -> None:
    name = case["name"]
    known_names = {
        "fixture-exact-build-source",
        "domain-prefix-ordering-framing-empty-binary-and-root-marker",
        "mode-and-timestamp-are-non-inputs",
        "invalid-unicode-build-source-path",
        "duplicate-build-source-path",
        "build-source-symbolic-link",
        "build-source-special-file",
        "build-source-mutation-during-use",
        "legacy-nul-stream-structural-collision",
        "root-marker-bytes-are-build-input",
    }
    assert name in known_names, f"unbound build-source vector {name!r}"

    records = case.get("records") or case.get("input_order")
    if records is not None:
        decoded = _decode_records(records)
        assert hashing.build_source_sha256(decoded) == case["content_sha256"]
        assert "sha256:" + hashlib.sha256(
            base64.b64decode(case["preimage_base64"])
        ).hexdigest() == case["content_sha256"]
        root = tmp_path / "materialized"
        for relative, content in decoded:
            path = root / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(content)
        with source.freeze_snapshot(root) as frozen:
            assert frozen.identity.content_sha256 == case["content_sha256"]
        return

    if name == "mode-and-timestamp-are-non-inputs":
        ordered = next(item for item in all_cases if "input_order" in item)
        identities = []
        for index, variant in enumerate(case["variants"]):
            root = tmp_path / f"variant-{index}"
            for relative, content in _decode_records(ordered["input_order"]):
                path = root / relative
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(content)
                path.chmod(int(variant["mode"], 8))
                timestamp = 946684800 if index == 0 else 1893456000
                os.utime(path, (timestamp, timestamp))
            with source.freeze_snapshot(root) as frozen:
                identities.append(frozen.identity.content_sha256)
        assert identities == [case["content_sha256"], case["content_sha256"]]
        return

    if name == "invalid-unicode-build-source-path":
        path_text = base64.b64decode(case["input"]["path_bytes_base64"]).decode(
            "utf-8",
            errors="surrogateescape",
        )
        with pytest.raises(hashing.HashingError):
            hashing.build_source_sha256([(path_text, b"value")])
        return

    if name == "duplicate-build-source-path":
        paths = case["input"]["paths"]
        with pytest.raises(hashing.HashingError, match="duplicate"):
            hashing.build_source_sha256(
                [(path, str(index).encode("ascii")) for index, path in enumerate(paths)]
            )
        return

    if name == "build-source-symbolic-link":
        root = tmp_path / "source-link"
        root.mkdir()
        target = root / "target"
        target.write_bytes(b"target")
        link = root / case["input"]["path"]
        try:
            link.symlink_to(target.name)
            context = mock.patch.object(source, "_is_link_or_reparse", wraps=source._is_link_or_reparse)
        except OSError:
            link.write_bytes(b"synthetic-link")
            context = mock.patch.object(
                source,
                "_is_link_or_reparse",
                side_effect=lambda value: not stat.S_ISDIR(value.st_mode),
            )
        with context, pytest.raises(source.InvalidSnapshotError, match="link forbidden"):
            source.freeze_snapshot(root)
        assert case["expected"]["error"] == "link_forbidden"
        return

    if name == "build-source-special-file":
        root = tmp_path / "source-special"
        root.mkdir()
        special = root / case["input"]["path"]
        if hasattr(os, "mkfifo"):
            os.mkfifo(special)
            context = mock.patch.object(source.stat, "S_ISREG", wraps=source.stat.S_ISREG)
        else:
            special.write_bytes(b"synthetic-special")
            context = mock.patch.object(source.stat, "S_ISREG", return_value=False)
        with context, pytest.raises(source.InvalidSnapshotError, match="special file forbidden"):
            source.freeze_snapshot(root)
        assert case["expected"]["error"] == "special_file_forbidden"
        return

    if name == "build-source-mutation-during-use":
        root = tmp_path / "source-mutation"
        root.mkdir()
        payload = root / "payload"
        payload.write_bytes(b"before")
        frozen = source.freeze_snapshot(root)
        try:
            def mutate(_snapshot: source.FrozenSnapshot) -> None:
                payload.write_bytes(b"after")

            with pytest.raises(source.SnapshotMutationError):
                frozen.use(mutate)
        finally:
            frozen.close()
        assert case["expected"]["error"] == "snapshot_mutated"
        return

    if name == "legacy-nul-stream-structural-collision":
        one = hashing.build_source_sha256(_decode_records(case["one_file"]))
        two = hashing.build_source_sha256(_decode_records(case["two_files"]))
        assert [one, two] == case["framed_content_sha256"]
        assert (one == two) is case["framed_hashes_equal"]
        assert case["legacy_streams_equal"] is True
        return

    if name == "root-marker-bytes-are-build-input":
        hashes = [variant["content_sha256"] for variant in case["variants"]]
        assert len(set(hashes)) == len(hashes) == 2
        assert case["build_source_hashes_equal"] is False
        assert case["legacy_installed_tree_hashes_equal"] is True
        return

    raise AssertionError(f"unimplemented build-source vector {name!r}")


def _native_path_key(path: str | os.PathLike[str]) -> str:
    return os.path.normcase(os.path.abspath(os.fspath(path)))


def _project_toolchain_link_target(
    native_target: str,
    protocol_target: str,
) -> str:
    assert native_target.replace("\\", "/") == protocol_target
    return protocol_target


def _materialize_toolchain_entries(
    root: Path,
    entries: list[JsonObject],
) -> dict[str, str]:
    unknown = {entry["type"] for entry in entries} - {"directory", "file", "symlink"}
    assert not unknown, f"unknown toolchain entry types {sorted(unknown)!r}"
    protocol_link_targets: dict[str, str] = {}

    # The vector is intentionally unsorted.  Materialize links last because
    # Windows classifies a new link from its target and may otherwise preserve
    # an unusable dangling reparse point; fingerprinting still consumes the
    # native tree order and must canonicalize it itself.
    for entry_type in ("directory", "file", "symlink"):
        for entry in entries:
            if entry["type"] != entry_type:
                continue
            path = root / entry["path"]
            if entry_type == "directory":
                path.mkdir(parents=True, exist_ok=True)
            elif entry_type == "file":
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(base64.b64decode(entry["content_base64"]))
            else:
                path.parent.mkdir(parents=True, exist_ok=True)
                protocol_target = entry["target"]
                native_target = Path(*PurePosixPath(protocol_target).parts)
                path.symlink_to(native_target)
                protocol_link_targets[_native_path_key(path)] = protocol_target

    return protocol_link_targets


def _fingerprint_vector_toolchain(
    root: Path,
    go_version_stdout: bytes,
    protocol_link_targets: dict[str, str],
) -> toolchain.ToolchainIdentity:
    native_readlink = os.readlink

    def read_vector_link(
        path: str | os.PathLike[str],
        *,
        dir_fd: int | None = None,
    ) -> str:
        if dir_fd is None:
            native_target = native_readlink(path)
        else:
            native_target = native_readlink(path, dir_fd=dir_fd)
        protocol_target = protocol_link_targets.get(_native_path_key(path))
        if protocol_target is None:
            return native_target
        return _project_toolchain_link_target(native_target, protocol_target)

    # The suite records link payloads with protocol (POSIX) separators.  The
    # native fixture must use host separators so Windows can resolve the link;
    # expose the verified protocol spelling only at the readlink boundary.
    with mock.patch.object(toolchain.os, "readlink", new=read_vector_link):
        return toolchain.fingerprint_toolchain(
            root.resolve(),
            go_version_stdout,
        )


def assert_toolchain_case(
    case: JsonObject,
    all_cases: list[JsonObject],
    tmp_path: Path,
) -> None:
    name = case["name"]
    known_names = {
        "unsorted-directories-files-and-internal-link",
        "crlf-version-normalizes-to-lf-identity",
        "toolchain-mode-and-timestamp-are-non-inputs",
        "toolchain-version-missing-terminal-lf",
        "toolchain-version-multiple-terminal-newlines",
        "invalid-unicode-toolchain-path",
        "duplicate-toolchain-path",
        "escaping-toolchain-link",
        "absolute-toolchain-link",
        "dangling-toolchain-link",
        "selected-go-outside-goroot",
        "toolchain-tree-mutation-during-use",
    }
    assert name in known_names, f"unbound toolchain vector {name!r}"

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
        root = tmp_path / "goroot"
        root.mkdir()
        protocol_link_targets = _materialize_toolchain_entries(root, case["entries"])
        identity = _fingerprint_vector_toolchain(
            root,
            base64.b64decode(case["go_version_stdout_base64"]),
            protocol_link_targets,
        )
        assert identity.content_sha256 == case["content_sha256"]
        return

    if name == "toolchain-mode-and-timestamp-are-non-inputs":
        canonical = next(item for item in all_cases if "entries" in item)
        identities = []
        for index, variant in enumerate(case["variants"]):
            root = tmp_path / f"toolchain-mode-{index}"
            root.mkdir()
            protocol_link_targets = _materialize_toolchain_entries(
                root,
                canonical["entries"],
            )
            for path in root.rglob("*"):
                if not path.is_symlink():
                    path.chmod(int(variant["mode"], 8))
                    timestamp = 946684800 if index == 0 else 1893456000
                    os.utime(path, (timestamp, timestamp))
            identities.append(
                _fingerprint_vector_toolchain(
                    root,
                    base64.b64decode(canonical["go_version_stdout_base64"]),
                    protocol_link_targets,
                ).content_sha256
            )
        assert identities == [case["content_sha256"], case["content_sha256"]]
        return

    if name in {
        "toolchain-version-missing-terminal-lf",
        "toolchain-version-multiple-terminal-newlines",
    }:
        with pytest.raises(toolchain.ToolchainError) as raised:
            toolchain.normalize_go_version(base64.b64decode(case["input"]["stdout_base64"]))
        assert raised.value.code == case["expected"]["error"]
        return

    if name == "invalid-unicode-toolchain-path":
        path_text = base64.b64decode(case["input"]["path_bytes_base64"]).decode(
            "utf-8",
            errors="surrogateescape",
        )
        with pytest.raises(toolchain.ToolchainError) as raised:
            toolchain._protocol_path_bytes(path_text)
        assert raised.value.code == case["expected"]["error"]
        return

    if name == "duplicate-toolchain-path":
        root = tmp_path / "duplicate-toolchain"
        (root / "bin").mkdir(parents=True)
        executable = root / "bin" / "go"
        executable.write_bytes(b"GO")
        record = next(
            item
            for item in toolchain._collect_records(root, deadline=float("inf"))
            if item.protocol_path == case["input"]["paths"][0]
        )
        with pytest.raises(toolchain.ToolchainError) as raised:
            toolchain._canonical_records([record, record])
        assert raised.value.code == case["expected"]["error"]
        return

    if name in {
        "escaping-toolchain-link",
        "absolute-toolchain-link",
        "dangling-toolchain-link",
    }:
        root = tmp_path / name
        root.mkdir()
        native = root / case["input"]["path"]
        with mock.patch.object(
            toolchain.os,
            "readlink",
            return_value=case["input"]["target"],
        ), pytest.raises(toolchain.ToolchainError) as raised:
            toolchain._validated_link_target(
                root,
                native,
                case["input"]["path"],
            )
        assert raised.value.code == case["expected"]["error"]
        return

    if name == "selected-go-outside-goroot":
        goroot = tmp_path / "selected-goroot"
        (goroot / "bin").mkdir(parents=True)
        (goroot / "bin" / "go").write_bytes(b"GO")
        selected = tmp_path / "outside" / "go"
        selected.parent.mkdir()
        selected.write_bytes(b"GO")
        config = toolchain.ToolchainConfig(
            private_base=tmp_path,
            operator_search_path=toolchain.OperatorSearchPath(()),
            forbidden_roots=(),
            go_executable=selected.resolve(),
            goroot=goroot.resolve(),
        )
        with pytest.raises(toolchain.ToolchainError) as raised:
            toolchain._select_toolchain(
                config,
                toolchain._Host(goos="darwin", goarch="arm64", windows=False),
                (),
            )
        assert raised.value.code == case["expected"]["error"]
        return

    if name == "toolchain-tree-mutation-during-use":
        root = tmp_path / "mutating-toolchain"
        (root / "bin").mkdir(parents=True)
        executable = root / "bin" / "go"
        executable.write_bytes(b"before")
        version = "go version go1.25.5 darwin/arm64"
        digest, tree_state = toolchain._fingerprint_normalized(
            root,
            version,
            deadline=float("inf"),
        )
        target = toolchain.NativeTarget(
            goos="darwin",
            goarch="arm64",
            tuning={"GOARM64": "v8.0"},
        )
        snapshot = toolchain.ToolchainSnapshot(
            executable=executable,
            goroot=root,
            target=target,
            toolchain=toolchain.ToolchainIdentity(
                algorithm=toolchain.TOOLCHAIN_ALGORITHM,
                content_sha256=digest,
                go_relpath=toolchain.GO_RELPATH,
                go_version=version,
            ),
            environment={},
        )
        session = toolchain.ToolchainSession(
            snapshot=snapshot,
            operation_root=tmp_path / "operation",
            private_base=tmp_path,
            root_stat=root.lstat(),
            tree_state=tree_state,
            fingerprint_timeout=toolchain.DEFAULT_FINGERPRINT_TIMEOUT,
            host=toolchain._Host(goos="darwin", goarch="arm64", windows=False),
        )
        executable.write_bytes(b"after-mutation")
        with mock.patch.object(
            toolchain,
            "_verify_selected_root",
            return_value=None,
        ), pytest.raises(toolchain.ToolchainError) as raised:
            session.verify()
        assert raised.value.code == case["expected"]["error"]
        return

    if case.get("result") == "accepted":
        assert isinstance(case["content_sha256"], str)
        return

    raise AssertionError(f"unimplemented toolchain vector {name!r}")


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


class _RecordingProbeRunner:
    def __init__(self, goroot: Path) -> None:
        self.goroot = goroot
        self.calls: list[tuple[tuple[str, ...], Path, dict[str, str]]] = []

    def run(
        self,
        argv: tuple[str, ...],
        *,
        cwd: Path,
        environment: Any,
        timeout: float,
        output_limit: int,
    ) -> toolchain.ProbeResult:
        del timeout, output_limit
        values = dict(environment)
        self.calls.append((argv, cwd, values))
        arguments = argv[1:]
        if arguments == ("telemetry", "off"):
            return toolchain.ProbeResult()
        if arguments == ("version",):
            return toolchain.ProbeResult(
                stdout=b"go version go1.25.5 darwin/arm64\n"
            )
        assert arguments == ("env", "-json", *toolchain.GO_ENV_FIELDS)
        telemetry = Path(values["XDG_CONFIG_HOME"]) / "go" / "telemetry"
        telemetry.mkdir(parents=True, exist_ok=True)
        response = {name: "" for name in toolchain.GO_ENV_FIELDS}
        response.update(
            {
                "GOROOT": os.fspath(self.goroot),
                "GOHOSTOS": "darwin",
                "GOHOSTARCH": "arm64",
                "GOOS": "darwin",
                "GOARCH": "arm64",
                "GOARM64": "v8.0",
                "GOTELEMETRY": "off",
                "GOTELEMETRYDIR": os.fspath(telemetry),
            }
        )
        return toolchain.ProbeResult(stdout=protocol_json.canonical_bytes(response))


class _PlanCaptured(RuntimeError):
    pass


def _normalized_environment(
    environment: Any,
    *,
    operation_root: Path,
    goroot: Path,
) -> dict[str, str]:
    operation = os.fspath(operation_root)
    trusted = os.fspath(goroot)
    normalized: dict[str, str] = {}
    for name, value in dict(environment).items():
        if value == trusted:
            value = "<resolved-trusted-goroot>"
        elif name == "XDG_CONFIG_HOME" and (
            value == operation or value.startswith(operation + os.sep)
        ):
            # The shared placeholder names the private configuration role;
            # Darwin's physical Go telemetry location is below Library.
            value = "<operation-private>/config"
        elif value == operation:
            value = "<operation-private>"
        elif value.startswith(operation + os.sep):
            suffix = value[len(operation) :].replace(os.sep, "/")
            value = "<operation-private>" + suffix
        normalized[name] = value
    return dict(sorted(normalized.items()))


def _assert_fixed_environment_and_argv(
    case: JsonObject,
    vectors: JsonObject,
    tmp_path: Path,
) -> None:
    private_base = tmp_path / "private"
    private_base.mkdir(parents=True)
    forbidden_root = tmp_path / "project-repository"
    forbidden_root.mkdir()
    goroot = tmp_path / "trusted-goroot"
    executable = goroot / "bin" / "go"
    executable.parent.mkdir(parents=True)
    executable.write_bytes(b"\xcf\xfa\xed\xfe" + b"\x00" * 12)
    executable.chmod(0o755)
    runner = _RecordingProbeRunner(goroot.resolve())
    host = toolchain._Host(goos="darwin", goarch="arm64", windows=False)
    native_path_type = type(executable)
    real_lstat = native_path_type.lstat
    executable_key = os.path.normcase(os.path.abspath(executable))

    def darwin_fixture_lstat(path: Path) -> os.stat_result:
        observed = real_lstat(path)
        if os.path.normcase(os.path.abspath(path)) == executable_key:
            # The candidate describes a Darwin launcher on every consumer.
            # Model its executable mode at the filesystem-observation seam so
            # filesystems without Unix mode bits can exercise the same host.
            # CocoaSkills' real launcher validator still checks the file type,
            # stable identity, native Mach-O header, and open boundary.
            return os.stat_result((observed.st_mode | 0o111, *observed[1:]))
        return observed

    launcher_mode_patch = mock.patch.object(
        native_path_type,
        "lstat",
        autospec=True,
        side_effect=darwin_fixture_lstat,
    )
    launcher_mode_patch.start()
    try:
        session = toolchain._establish_toolchain(
            toolchain.ToolchainConfig(
                private_base=private_base.resolve(),
                operator_search_path=toolchain.OperatorSearchPath(()),
                forbidden_roots=(forbidden_root.resolve(),),
                go_executable=executable.resolve(),
                runner=runner,
            ),
            host,
        )
    except BaseException:
        launcher_mode_patch.stop()
        raise
    snapshot_root = tmp_path / "source"
    source_dir = snapshot_root / "build" / "cmd" / "golden-tool"
    source_dir.mkdir(parents=True)
    (snapshot_root / "build" / "go.mod").write_text(
        "module example.com/conformance\n",
        encoding="utf-8",
    )
    (source_dir / "main.go").write_text("package main\n", encoding="utf-8")
    frozen = source.freeze_snapshot(snapshot_root)
    captured: list[Any] = []

    def capture_plan(plan: Any) -> None:
        captured.append(plan)
        raise _PlanCaptured("worker plan captured before execution")

    try:
        assert len(runner.calls) == 3
        bootstrap = toolchain._bootstrap_environment(
            toolchain._create_probe_layout(session.operation_root, host),
            host,
        )
        for _argv, cwd, environment in runner.calls:
            assert cwd == session.operation_root / "empty"
            assert environment == bootstrap

        assert _normalized_environment(
            session.environment,
            operation_root=session.operation_root,
            goroot=session.goroot,
        ) == vectors["fixed_environment"]

        with mock.patch.object(
            go_v1,
            "probe_native_controls",
            return_value=(go_v1.PLATFORM_MACOS, _synthetic_probes(go_v1.PLATFORM_MACOS)),
        ), mock.patch.object(
            go_v1,
            "_resolve_manager_identity",
            return_value=SimpleNamespace(),
        ), mock.patch.object(
            go_v1,
            "_resolve_tool_process_identity",
            return_value=SimpleNamespace(),
        ), mock.patch.object(
            go_v1._WorkerClient,
            "launch",
            side_effect=capture_plan,
        ), pytest.raises(_PlanCaptured):
            go_v1.build(
                go_v1.BuildRequest(
                    toolchain_session=session,
                    source_snapshot=frozen,
                    command_object={
                        "type": "build",
                        "driver": "go-v1",
                        "source_dir": "build/cmd/golden-tool",
                    },
                    build_root="build",
                    source_dir="build/cmd/golden-tool",
                    command="golden-tool",
                )
            )
        assert len(captured) == 1
        plan = captured[0]
        assert _normalized_environment(
            plan.environment,
            operation_root=session.operation_root,
            goroot=session.goroot,
        ) == vectors["fixed_environment"]

        observed: list[JsonObject] = []
        parent_names = ("telemetry-off", "version", "env")
        for name, (argv, cwd, _environment) in zip(parent_names, runner.calls, strict=True):
            normalized_argv = [
                "/absolute/trusted/goroot/bin/go" if index == 0 else value
                for index, value in enumerate(argv)
            ]
            observed.append(
                {
                    "argv": normalized_argv,
                    "cwd": "<operation-private>/empty"
                    if cwd == session.operation_root / "empty"
                    else os.fspath(cwd),
                    "name": name,
                    "source_aware": False,
                }
            )
        build_argv = [os.fspath(plan.go_executable), *plan.build_argv]
        build_argv[-2] = "<operation-staging>/bin/golden-tool"
        observed.extend(
            [
                {
                    "argv": [
                        "/absolute/trusted/goroot/bin/go",
                        *plan.list_argv,
                    ],
                    "cwd": "<validated-source-dir>",
                    "name": "list",
                    "source_aware": True,
                },
                {
                    "argv": [
                        "/absolute/trusted/goroot/bin/go",
                        *build_argv[1:],
                    ],
                    "cwd": "<validated-source-dir>",
                    "name": "build",
                    "source_aware": True,
                },
            ]
        )
        assert observed == vectors["argv"]
        assert case["argv"] == observed
        assert case["shell_used"] is False
        assert case["artifact_executed"] is False
    finally:
        frozen.close()
        try:
            session.close()
        finally:
            launcher_mode_patch.stop()


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
        _assert_fixed_environment_and_argv(case, vectors, tmp_path)
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
    name = case.get("name")
    assert isinstance(name, str) and name in _LIFECYCLE_CASE_FIELDS, (
        f"unbound manager lifecycle vector {name!r}"
    )
    expected_cluster, expected_fields = _LIFECYCLE_CASE_FIELDS[name]
    assert cluster == expected_cluster, f"lifecycle case {name!r} is in the wrong cluster"
    assert set(case) == expected_fields, f"lifecycle case {name!r} has unknown or missing fields"

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
        assert case["ordering"] == "provider-first-kahn-then-unicode-scalar-command-name"
        return

    if cluster == "cache_publication_cases":
        if name == "publish-complete-immutable-entry-under-home-lock":
            assert case["manager_home_lock"] is True
            assert case["merge_existing_entry"] is False
            assert case["publication"] == "atomic-complete-directory"
            assert case["result"] == cache.CachePublicationStatus.PUBLISHED.value
        elif name == "concurrent-identical-winner":
            assert case["winner_bytes_equal_staged"] is True
            assert case["winner_modified"] is False
            assert case["winner_validation"] == "exact-protected-entry"
            assert case["staged_loser"] == "discard"
            assert case["result"] == "reuse-winner"
        elif name == "concurrent-determinism-mismatch":
            assert case["winner_bytes_equal_staged"] is False
            assert case["winner_modified"] is False
            assert case["winner_validation"] == "exact-protected-entry"
            assert case["install_targets_mutated"] is False
            assert case["result"] == "determinism-or-corruption-error"
        elif name == "corrupt-live-entry":
            assert case["manager_home_lock"] is True
            assert case["quarantine_allowed"] is True
            assert case["adopt_or_repair_candidate"] is False
            assert case["existing_valid_entries_modified"] is False
            assert case["result"] == "replace-from-verified-staging"
        else:
            assert name == "untrusted-cache-boundary"
            assert case["embedded_hashes_match"] is True
            assert case["candidate_reused"] is False
            assert case["chmod_then_adopt"] is False
            assert case["status_current"] is False
            assert case["result"] == "rebuild-into-new-protected-state"
        return

    if cluster == "cross_project_cases":
        assert case["shared_cache_key"] == fixture["cache_key"]
        if "consumer_ledger_after" in case:
            assert case["consumer_ledger_after"] == case["commit_order"]
            assert case["consumer_ledger_before"] == []
            assert case["private_builds_may_overlap"] is True
            assert case["shared_transactions_serialized"] is True
            assert case["result"] == "success"
        else:
            assert case["consumer_ledger_after_rollback"] == case[
                "consumer_ledger_before_failing_transaction"
            ]
            assert case["project_alpha_targets_unchanged"] is True
            assert case["successful_project"] != case["failing_project"]
            assert case["result"] == f"{case['failing_project']}-rolled-back"
        return

    if cluster == "dry_run_cases":
        effects = case["forbidden_persistent_effects"]
        assert effects and len(effects) == len(set(effects))
        if name == "compiled-cache-miss-is-read-only":
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
            assert case["allowed_go_commands"] == ["telemetry-off", "version", "env"]
            assert case["forbidden_go_commands"] == ["list", "build"]
            assert not set(case["allowed_go_commands"]) & set(case["forbidden_go_commands"])
            assert case["artifact_executed"] is False
            assert case["scope"] == "multi-project"
            assert case["operation_private_state_after"] == "absent"
        else:
            assert case["scope"] == name.removesuffix("-upgrade")
            required = {
                "source-fetch",
                "source-clone",
                "snapshot-cache",
                "response-cache",
                "audit-state",
                "registry-state",
                "configuration",
                "runtime",
            }
            assert required <= set(effects)
            assert f"{case['scope']}-artifacts" in effects
        return

    if cluster == "gc_cases":
        assert gc.BUILD_GRACE_SECONDS > 0
        if "sweep_requires" in case:
            assert case["only_lock"] == "manager-home-mutation-lock"
            assert set(case["compiled_cache_mark_roots"]) <= set(case["mark_roots"])
            assert case["sweep_requires"] == [
                "unreferenced",
                "machine-local",
                "older-than-grace-period",
            ]
            assert case["artifact_executed"] is False
            assert case["entry_adopted"] is False
            assert case["protected_boundary_revalidated"] is True
            assert case["receipt_content_alone_is_live_reference"] is False
            assert case["result"] == "swept-unreferenced-old-entries"
            assert case["uncertain_state_action"].startswith("retain-")
        else:
            assert case["successful_installation_rolled_back"] is False
            assert case["manager_home_lock"] is True
            assert case["result"] == "installation-success-with-warning"
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
        gates = case["required_before_toolchain_or_cache"]
        assert gates and len(gates) == len(set(gates))
        assert case["then"][:3] == [
            "trusted-toolchain-resolution-and-fingerprint",
            "logical-cache-key-derivation",
            "protected-cache-read-only-inspection",
        ]
        assert case["result"] == "build-eligible"
        return

    if cluster == "private_build_cases":
        if "manager_home_lock_acquired" in case:
            assert case["manager_home_lock_acquired"] is False
        else:
            assert case["manager_home_lock_during_build"] is False
        if "builds" in case:
            assert all(build["artifact_verified"] for build in case["builds"])
            assert [build["command"] for build in case["builds"]] == [
                "golden-tool",
                "second-tool",
            ]
            assert all(build["staging"] == "operation-private" for build in case["builds"])
            assert case["builds"][0]["cache_key"] == fixture["cache_key"]
            assert case["builds"][0]["receipt_sha256"] == fixture["receipt_sha256"]
            assert case["shared_mutations_before_all_verified"] == []
            assert case["artifacts_executed"] is False
            assert case["result"] == "ready-to-publish"
        else:
            assert case["persistent_state_after"] == case["persistent_state_before"]
            assert "cache-publication" in case["forbidden_effects"]
            assert len(case["events"]) == 4
            assert case["events"][-1] == "operation-private-staging-removed"
            assert case["result"] == "build-failed"
        return

    if cluster == "recovery_cases":
        if "manager_home_lock" in case:
            assert case["manager_home_lock"] is True
        else:
            assert case["backups_retained_until_recovery_succeeds"] is True
        if "journal_state" in case:
            assert case["journal_owner"] == "global"
            assert case["journal_state"] == "partially-committed"
            assert case["journal_transaction_id"].startswith("transaction-global-")
            assert case["expected_action"] == (
                "verify-preimages-and-restore-reverse-commit-order"
            )
            assert case["successful_project_consumers_after"] == case[
                "successful_project_consumers_before"
            ]
            assert case["scan_scope"] == "all-incomplete-journals"
            assert case["triggering_project"] not in case[
                "successful_project_consumers_before"
            ]
            assert case["result"] == "restored"
        else:
            assert case["private_builds_verified"] is True
            assert case["recovery_before_build"] is False
            assert case["restart_if_plan_assumption_changed"] is True
            assert case["result"] == "publication-may-proceed"
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
        assert len(case["forbidden_shortcuts"]) == len(set(case["forbidden_shortcuts"]))
        assert case["required_pipeline"][-1] == "journaled-commit"
        assert case["result"] == "rebuilt-and-journaled"
        return

    if cluster == "status_cases":
        build_input = metadata.parse_build_input(fixture["build_input"])
        assert metadata.cache_key(build_input) == fixture["cache_key"]
        if case["result"] == "current":
            receipt = metadata.parse_receipt(fixture["stored_receipt"])
            assert receipt.input == build_input
            assert case["mutations"] == []
            assert case["validated"][-3:] == [
                "protected-boundary",
                "canonical-receipt",
                "artifact-path-hash-and-size",
            ]
            assert len(case["validated"]) == len(set(case["validated"]))
        else:
            assert case["adopt"] is False
            assert case["quarantine"] is False
            assert case["repair"] is False
            assert len(case["independent_conditions"]) == len(
                set(case["independent_conditions"])
            )
            assert case["mutations"] == []
        assert case["artifact_executed"] is False
        return

    if cluster == "transaction_cases":
        if "input_project_identities" in case:
            assert sorted(
                case["input_project_identities"],
                key=lambda value: value.encode("utf-8"),
            ) == case["expected_project_lock_order"]
            assert case["cache_build_lock_released_before_home_lock"] is True
            assert case["maximum_cache_build_locks"] == 1
            assert case["then_manager_home_lock"] is True
            assert case["then_optional_cache_build_lock"] is True
            assert case["forbidden_while_holding_home_lock"] == [
                "project-lock",
                "cache-build-lock",
            ]
            assert case["result"] == "locks-acquired"
        elif "expected_commit_order" in case:
            classes = [item.split("/", 1)[0] for item in case["expected_commit_order"]]
            assert classes[-1] == "consumer-ledger"
            assert list(dict.fromkeys(classes)) == case["target_class_order"]
            assert case["canonical_identifier_order"] == (
                "unsigned-utf8-bytewise-within-class"
            )
            assert case["backups_retained_until_consumer_durable"] is True
            assert case["consumer_ledger_committed_last"] is True
            assert case["result"] == "committed"
        else:
            assert case["expected_restore_order"] == list(reversed(case["commit_order"]))
            assert case["manager_home_lock_held_through_rollback"] is True
            assert case["require_current_digest_equals_desired_before_restore"] is True
            assert case["unknown_state_overwritten"] is False
            assert case["existing_valid_cache_entries_modified"] is False
            assert case["result"] == "rolled-back"
        return

    if cluster == "upgrade_cases":
        assert case["selection"] in {"one", "all", "global"}
        if "fetch" in case:
            assert case["fetch"] == ["direct", "transitive"]
            assert case["exclude"] == ["unrelated"]
            assert case["scope"] == (
                "global" if case["selection"] == "global" else "project"
            )
        else:
            assert case["deduplicate"] is True
            assert case["selection"] == "all"
            assert case["scope"] == "project"
        return

    raise AssertionError(f"no manager lifecycle adapter for {cluster!r}")
