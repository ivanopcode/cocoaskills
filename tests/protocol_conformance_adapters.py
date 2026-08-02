"""Data-driven adapters for the shared Curator protocol candidate suite.

These helpers deliberately consume caller-supplied vector values.  They do
not contain a second implementation of the build manager; executable vectors
are routed through CocoaSkills parsers and validators, while lifecycle vectors
are reconstructed from observed CocoaSkills traces and state.
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import stat
import subprocess
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, replace
from functools import lru_cache
from pathlib import Path, PurePosixPath
from types import SimpleNamespace
from typing import Any
from unittest import mock

import pytest
from jsonschema.validators import validator_for
from referencing import Registry, Resource

from csk import (
    closure,
    gc,
    hashing,
    install_marker,
    protocol_json,
    skillspec,
    whitelist,
)
from csk.builds import cache, go_v1, metadata, source, toolchain
from protocol_lifecycle_observations import observe_manager_lifecycle_case

JsonObject = dict[str, Any]


@dataclass(frozen=True)
class _RejectionBinding:
    """Exact declarative condition bound to one independently built fixture."""

    boundary: str
    condition: str | None


_BUILD_REJECTION_BINDINGS: dict[str, _RejectionBinding] = {
    "schema-5-build-command": _RejectionBinding("manifest", "type=build under schema_version=5"),
    "unknown-driver": _RejectionBinding("manifest", "driver is not go-v1"),
    "forbidden-args": _RejectionBinding("manifest", "build command declares args"),
    "forbidden-env": _RejectionBinding("manifest", "build command declares env"),
    "forbidden-output": _RejectionBinding("manifest", "build command declares output"),
    "forbidden-toolchain": _RejectionBinding("manifest", "build command declares toolchain"),
    "forbidden-hooks": _RejectionBinding("manifest", "build command declares hooks"),
    "mixed-script-build-shape": _RejectionBinding("manifest", "one command combines build and script fields"),
    "missing-build-roots": _RejectionBinding("filesystem", "build command has no build_roots"),
    "missing-build-root-directory": _RejectionBinding("filesystem", "declared build root does not exist"),
    "unused-build-root": _RejectionBinding("filesystem", "declared build root contains no command"),
    "overlapping-build-roots": _RejectionBinding("filesystem", "one build root contains another"),
    "runtime-overlapping-build-root": _RejectionBinding("filesystem", "build root overlaps runtime root"),
    "root-build-root": _RejectionBinding("filesystem", "build_root is dot"),
    "build-root-symlink": _RejectionBinding("filesystem", "build root is a symbolic link"),
    "build-root-special-file": _RejectionBinding("filesystem", "build root is a special file"),
    "root-source-dir": _RejectionBinding("filesystem", "source_dir is dot"),
    "escaped-source-dir": _RejectionBinding("filesystem", "source_dir contains parent traversal"),
    "source-outside-root": _RejectionBinding("filesystem", "source_dir is outside every build root"),
    "source-link": _RejectionBinding("filesystem", "source_dir crosses a symbolic link"),
    "source-special-file": _RejectionBinding("filesystem", "source_dir is a special file"),
    "source-not-directory": _RejectionBinding("filesystem", "source_dir is a regular file"),
    "missing-root-go-mod": _RejectionBinding("module", "build root lacks direct go.mod"),
    "nested-module": _RejectionBinding("module", "nearest go.mod intervenes below build root"),
    "non-main-package": _RejectionBinding("dependency-graph", "selected package is a library"),
    "multiple-packages": _RejectionBinding("dependency-graph", "selection yields multiple main packages"),
    "missing-vendored-dependency": _RejectionBinding("dependency-graph", "required module is absent from vendor"),
    "inconsistent-vendor-modules": _RejectionBinding("dependency-graph", "vendor/modules.txt disagrees with go.mod"),
    "workspace-only-dependency": _RejectionBinding("dependency-graph", "dependency resolves only through go.work"),
    "toolchain-switch-request": _RejectionBinding("toolchain", "go.mod requests another toolchain"),
    "unsupported-go-pre-1-23": _RejectionBinding("toolchain", "selected release is older than Go 1.23"),
    "unsupported-go-future-family": _RejectionBinding("toolchain", "selected release family is not allowlisted"),
    "cgo-only-package": _RejectionBinding("dependency-graph", "package has no buildable files with CGO_ENABLED=0"),
    "native-c-input": _RejectionBinding("dependency-graph", "non-standard package has CFiles"),
    "native-cxx-input": _RejectionBinding("dependency-graph", "non-standard package has CXXFiles"),
    "native-swig-input": _RejectionBinding("dependency-graph", "non-standard package has SwigFiles"),
    "root-syso": _RejectionBinding("dependency-graph", "root package has SysoFiles"),
    "transitive-syso": _RejectionBinding("dependency-graph", "transitive package has SysoFiles"),
    "root-assembly-absolute-include": _RejectionBinding("dependency-graph", "root SFiles includes an absolute path"),
    "transitive-assembly-escaping-include": _RejectionBinding("dependency-graph", "transitive SFiles escapes the build root"),
    "escaped-embed-input": _RejectionBinding("dependency-graph", "EmbedFiles resolves outside the build root"),
    "cgo-import-dynamic": _RejectionBinding("compiler-directive", "active GoFiles contains //go:cgo_import_dynamic"),
    "attempted-go-generate": _RejectionBinding("compiler-directive", "package requires go generate output"),
    "default-pgo": _RejectionBinding("compiler-directive", "package attempts default.pgo input"),
    "poisoned-path": _RejectionBinding("process", "host PATH contains compiler tools"),
    "inherited-goflags-toolexec": _RejectionBinding("process", "host GOFLAGS requests toolexec"),
    "inherited-goenv": _RejectionBinding("process", "host GOENV selects a file"),
    "inherited-gowork": _RejectionBinding("process", "host GOWORK selects a workspace"),
    "vcs-metadata": _RejectionBinding("process", "repository VCS metadata would affect output"),
    "repository-local-fake-go": _RejectionBinding("process", "repository supplies a fake go executable"),
    "telemetry-command-failure": _RejectionBinding("process", "go telemetry off exits unsuccessfully"),
    "telemetry-private-dir-escape": _RejectionBinding("process", "GOTELEMETRYDIR escapes operation root"),
    "external-link-required": _RejectionBinding("process", "target requires external linking"),
    "libgcc-fallback-attempt": _RejectionBinding("process", "linker attempts host compiler lookup"),
    "child-outside-goroot-tools": _RejectionBinding("process", "child executable is outside GOROOT/pkg/tool/host"),
    "wrong-go-executable-path": _RejectionBinding("toolchain", "selected executable is not GOROOT/bin/go"),
    "toolchain-digest-mismatch": _RejectionBinding("toolchain", "tree digest changes before child exit"),
    "cache-key-mismatch": _RejectionBinding("cache", "receipt input derives another key"),
    "cache-wrong-target": _RejectionBinding("cache", "receipt target differs"),
    "cache-wrong-toolchain": _RejectionBinding("cache", "receipt toolchain differs"),
    "cache-wrong-policy": _RejectionBinding("cache", "receipt policy differs"),
    "cache-wrong-build-source": _RejectionBinding("cache", "receipt build source differs"),
    "receipt-hash-mismatch": _RejectionBinding("cache", "stored receipt hash differs"),
    "artifact-hash-mismatch": _RejectionBinding("cache", "artifact bytes differ"),
    "artifact-size-mismatch": _RejectionBinding("cache", "artifact length differs"),
    "artifact-path-mismatch": _RejectionBinding("cache", "artifact path is not manager-derived"),
    "noncanonical-receipt-whitespace": _RejectionBinding("cache", "stored receipt is pretty-printed"),
    "noncanonical-receipt-trailing-lf": _RejectionBinding("cache", "stored receipt has terminal LF"),
    "partial-cache-entry": _RejectionBinding("cache", "receipt or artifact is absent"),
    "artifact-link": _RejectionBinding("cache", "artifact is a symbolic link"),
    "artifact-special-file": _RejectionBinding("cache", "artifact is not regular"),
    "concurrent-publisher-different-bytes": _RejectionBinding("cache", "winner differs for the same key"),
    "self-consistent-forged-receipt-outside-protected-state": _RejectionBinding("cache", None),
    "marker-embed-build-source-regression": _RejectionBinding("context", None),
    "build-root-content-in-context": _RejectionBinding("context", "context selector exposes build-root input"),
    "legacy-rc4-input-without-execution-policy": _RejectionBinding("execution-policy", "policy omits the required execution_policy"),
    "reserved-hardened-execution-policy": _RejectionBinding("execution-policy", "policy names the reserved hardened profile"),
}


@dataclass(frozen=True)
class _RejectionTrace:
    """Observed product rejection plus the effects visible at its boundary."""

    error: str
    cache_inspection: cache.CacheInspection | None = None
    cache_lookup_performed: bool = False
    artifact_executions: int = 0
    cache_keys_created: int = 0
    go_commands: tuple[tuple[str, ...], ...] = ()
    extras: JsonObject | None = None

    def expected_fields(self) -> JsonObject:
        observed: JsonObject = {
            "artifact_executed": self.artifact_executions > 0,
            "error": self.error,
            "result": "reject" if self.error else "accept",
            "reuse": (
                self.cache_inspection.reusable
                if self.cache_inspection is not None
                else False
            ),
        }
        if self.extras is not None:
            observed.update(self.extras)
        return observed


def _assert_observed_rejection(expected: JsonObject, trace: _RejectionTrace) -> None:
    observed = trace.expected_fields()
    assert set(observed) == set(expected), (
        f"observed rejection fields {sorted(observed)} do not match vector fields "
        f"{sorted(expected)}"
    )
    assert expected == observed


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


def _make_directory_link(link: Path, target: Path) -> None:
    try:
        link.symlink_to(target, target_is_directory=True)
        return
    except OSError:
        if os.name != "nt":
            raise
    result = subprocess.run(
        ["cmd", "/d", "/c", "mklink", "/J", os.fspath(link), os.fspath(target)],
        check=False,
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        raise AssertionError(
            f"cannot materialize Windows directory link: {result.stdout}{result.stderr}"
        )


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


def _classify_skill_spec_error(
    error: skillspec.SkillSpecError,
    *,
    manifest: JsonObject,
    condition: str,
) -> str:
    """Project an exact CocoaSkills parser failure into protocol vocabulary."""
    detail = str(error)
    if detail == "Command 'tool' has unsupported type 'build'":
        return "build_requires_schema_6"
    if detail == "Command 'tool' field 'driver' must be 'go-v1'":
        return "unsupported_build_driver"
    if detail.startswith("commands.tool has unsupported field(s):"):
        return "manifest_invalid"
    if detail == "commands.tool.source_dir must be below exactly one build_roots entry":
        return (
            "build_roots_required"
            if not manifest.get("build_roots")
            else "build_source_outside_build_root"
        )
    if "build root does not exist" in detail:
        return "build_root_missing"
    if "build root 'unused' is not used" in detail:
        return "build_root_unused"
    if detail.startswith("build roots must be disjoint:"):
        return "build_roots_overlap"
    if detail.startswith("build roots must not overlap runtime roots:"):
        return "build_runtime_roots_overlap"
    if detail.startswith("build_roots[") and "must be a POSIX-style relative path" in detail:
        return "build_root_invalid"
    if "build root must be link-free" in detail:
        return "build_root_link"
    if "build root must be a directory" in detail:
        return "build_root_special_file"
    if detail.startswith("commands.tool.source_dir must be a POSIX-style relative path"):
        return "build_source_outside_build_root"
    if detail.startswith("commands.tool.source_dir must be a relative path"):
        return "build_source_path_escape"
    if "source directory must be link-free" in detail:
        return "build_source_link"
    if "source directory must be a directory" in detail:
        return (
            "build_source_special_file"
            if condition == "source_dir is a special file"
            else "build_source_not_directory"
        )
    if "build root build must contain the nearest go.mod directly" in detail:
        return "build_module_missing"
    if "intervening module" in detail and "is below build root" in detail:
        return "nested_build_module"
    raise AssertionError(f"unrecognized CocoaSkills skill rejection: {detail}")


def _probe_skill_spec_rejection(name: str, tmp_path: Path) -> str:
    binding = _BUILD_REJECTION_BINDINGS[name]
    assert binding.condition is not None
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
        _make_directory_link(build, snapshot / "real-build")
    elif name == "build-root-special-file":
        build = snapshot / "build"
        build.rename(snapshot / "real-build")
        if hasattr(os, "mkfifo"):
            os.mkfifo(build)
        else:
            build.write_bytes(b"not-a-directory")
    elif name in {"source-link", "source-special-file", "source-not-directory"}:
        source_dir = snapshot / "build" / "cmd" / "tool"
        source_dir.rename(source_dir.with_name("real-tool"))
        if name == "source-link":
            _make_directory_link(source_dir, source_dir.with_name("real-tool"))
        elif name == "source-special-file" and hasattr(os, "mkfifo"):
            os.mkfifo(source_dir)
        else:
            source_dir.write_bytes(b"not-a-directory")
    elif name == "missing-root-go-mod":
        (snapshot / "build" / "go.mod").rename(snapshot / "build" / "go.mod.absent")
    elif name == "nested-module":
        (snapshot / "build" / "cmd" / "tool" / "go.mod").write_text(
            "module example.com/nested\n",
            encoding="utf-8",
        )

    try:
        skillspec.load_skill_spec(snapshot)
    except skillspec.SkillSpecError as error:
        return _classify_skill_spec_error(
            error,
            manifest=manifest,
            condition=binding.condition,
        )
    raise AssertionError(f"CocoaSkills accepted rejection fixture {name!r}")


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


class _HeldCacheGuard:
    def assert_held(self) -> None:
        pass


@dataclass(frozen=True)
class _PublishedCacheFixture:
    backend: cache.BuildCacheBackend
    build_input: metadata.GoBuildInput
    artifact_path: Path
    manager_home: Path
    receipt_bytes: bytes


def _protect_windows_cache_path(path: Path, profile: Any) -> None:
    from csk.builds import cache_windows

    with cache_windows._open_raw_handle(
        path,
        desired_access=(
            cache_windows._READ_CONTROL
            | cache_windows._WRITE_DAC
            | cache_windows._FILE_READ_ATTRIBUTES
        ),
    ) as handle:
        cache_windows._apply_profile_dacl(handle, profile)
    with cache_windows._open_raw_handle(
        path,
        desired_access=(
            cache_windows._READ_CONTROL
            | cache_windows._FILE_READ_ATTRIBUTES
            | cache_windows._FILE_WRITE_ATTRIBUTES
        ),
    ) as handle:
        cache_windows._set_readonly(handle, False)
    with cache_windows._open_raw_handle(
        path,
        desired_access=cache_windows._FILE_ALL_ACCESS,
    ) as handle:
        cache_windows._apply_security_profile(handle, profile)


def _make_cache_path_mutable(path: Path, *, directory: bool) -> None:
    if os.name == "nt":
        from csk.builds import cache_windows

        _protect_windows_cache_path(
            path,
            cache_windows._MUTABLE_DIRECTORY
            if directory
            else cache_windows._MUTABLE_FILE,
        )
        return
    path.chmod(0o700 if directory else 0o600)


def _seal_cache_path(path: Path, *, artifact: bool = False) -> None:
    if os.name == "nt":
        from csk.builds import cache_windows

        _protect_windows_cache_path(
            path,
            cache_windows._SEALED_ARTIFACT
            if artifact
            else cache_windows._SEALED_DIRECTORY,
        )
        return
    path.chmod(0o500)


def _relax_cache_tree(root: Path) -> None:
    if not root.exists():
        return
    cleanup_errors: tuple[type[BaseException], ...] = (
        OSError,
        RuntimeError,
        cache.BuildCacheError,
    )
    if os.name == "nt":
        from csk.builds import cache_windows

        cleanup_errors = (*cleanup_errors, cache_windows._UntrustedState)
    paths: list[Path] = [root]
    for current, directories, files in os.walk(root, followlinks=False):
        current_path = Path(current)
        paths.extend(current_path / name for name in directories)
        paths.extend(current_path / name for name in files)
    for path in reversed(paths):
        try:
            if path.is_symlink():
                continue
            _make_cache_path_mutable(path, directory=path.is_dir())
        except cleanup_errors:
            pass


def _publish_cache_fixture(
    tmp_path: Path,
    vectors: JsonObject,
    name: str,
    artifact_bytes: bytes = b"observed cache artifact",
) -> _PublishedCacheFixture:
    fixture_root = tmp_path / f"cache-{name}"
    manager_home = fixture_root / "home"
    manager_home.mkdir(mode=0o700, parents=True)
    cache.provision_manager_home(manager_home)
    source_root = fixture_root / "private-build"
    source_root.mkdir(mode=0o700)
    artifact_source = source_root / "artifact"
    artifact_source.write_bytes(artifact_bytes)
    if os.name != "nt":
        artifact_source.chmod(0o700)
    cache.make_publication_source_private(artifact_source)

    build_input = metadata.parse_build_input(vectors["portable_identity"]["build_input"])
    digest = "sha256:" + hashlib.sha256(artifact_bytes).hexdigest()
    receipt = metadata.build_receipt(
        build_input,
        metadata.BuildArtifact(
            path=build_input.artifact_path,
            sha256=digest,
            size=len(artifact_bytes),
        ),
    )
    receipt_bytes = metadata.canonical_receipt_bytes(receipt)
    backend = cache.cache_for_manager_home(manager_home)
    published = backend.publish(
        cache.CachePublication(
            input=build_input,
            receipt_bytes=receipt_bytes,
            artifact_source=artifact_source,
        ),
        guard=_HeldCacheGuard(),
    )
    inspection = backend.inspect(cache.CacheExpectation(input=build_input))
    assert inspection.status is cache.CacheEntryStatus.HIT
    assert inspection.artifact_path == published.artifact_path
    return _PublishedCacheFixture(
        backend=backend,
        build_input=build_input,
        artifact_path=published.artifact_path,
        manager_home=manager_home,
        receipt_bytes=receipt_bytes,
    )


def _replace_cache_artifact(path: Path, raw: bytes) -> None:
    _make_cache_path_mutable(path, directory=False)
    path.write_bytes(raw)
    _seal_cache_path(path, artifact=True)


def _remove_cache_artifact(path: Path) -> None:
    parent = path.parent
    _make_cache_path_mutable(parent, directory=True)
    _make_cache_path_mutable(path, directory=False)
    path.unlink()
    _seal_cache_path(parent)


def _replace_cache_artifact_with_link(path: Path, target: Path) -> None:
    target.write_bytes(b"linked cache artifact")
    parent = path.parent
    _make_cache_path_mutable(parent, directory=True)
    _make_cache_path_mutable(path, directory=False)
    path.unlink()
    try:
        path.symlink_to(target)
    except OSError:
        if os.name != "nt":
            raise
        os.link(target, path)
        _seal_cache_path(path, artifact=True)
    _seal_cache_path(parent)


def _replace_cache_artifact_with_directory(path: Path) -> None:
    parent = path.parent
    _make_cache_path_mutable(parent, directory=True)
    _make_cache_path_mutable(path, directory=False)
    path.unlink()
    path.mkdir()
    _seal_cache_path(path)
    _seal_cache_path(parent)


def _classify_cache_inspection(
    condition: str,
    inspection: cache.CacheInspection,
) -> str:
    assert inspection.status in {
        cache.CacheEntryStatus.CORRUPT,
        cache.CacheEntryStatus.UNTRUSTED_PROVENANCE,
    }
    assert inspection.reusable is False
    detail = inspection.reason.casefold()
    if condition == "artifact bytes differ":
        assert "artifact" in detail and "hash" in detail
        return "artifact_hash_mismatch"
    if condition == "artifact length differs":
        assert "artifact" in detail and ("size" in detail or "length" in detail)
        return "artifact_size_mismatch"
    if condition == "receipt or artifact is absent":
        assert "artifact" in detail and (
            "absent" in detail
            or "incomplete" in detail
            or "unexpected contents" in detail
        )
        return "cache_entry_incomplete"
    if condition == "artifact is a symbolic link":
        assert "artifact" in detail or "link" in detail
        return "artifact_link"
    if condition == "artifact is not regular":
        assert "artifact" in detail or "regular" in detail
        return "artifact_special_file"
    raise AssertionError(f"unbound cache inspection condition {condition!r}")


def _probe_cache_backend_rejection(
    name: str,
    vectors: JsonObject,
    tmp_path: Path,
) -> _RejectionTrace:
    binding = _BUILD_REJECTION_BINDINGS[name]
    assert binding.condition is not None
    fixture = _publish_cache_fixture(tmp_path, vectors, name)
    original = fixture.artifact_path.read_bytes()
    try:
        if name == "artifact-hash-mismatch":
            _replace_cache_artifact(fixture.artifact_path, b"X" * len(original))
        elif name == "artifact-size-mismatch":
            _replace_cache_artifact(fixture.artifact_path, original + b"X")
        elif name == "partial-cache-entry":
            _remove_cache_artifact(fixture.artifact_path)
        elif name == "artifact-link":
            _replace_cache_artifact_with_link(
                fixture.artifact_path,
                fixture.manager_home.parent / "link-target",
            )
        elif name == "artifact-special-file":
            _replace_cache_artifact_with_directory(fixture.artifact_path)
        else:
            raise AssertionError(f"unbound cache backend fixture {name!r}")
        inspection = fixture.backend.inspect(
            cache.CacheExpectation(input=fixture.build_input)
        )
        return _RejectionTrace(
            _classify_cache_inspection(binding.condition, inspection),
            cache_inspection=inspection,
            cache_lookup_performed=True,
        )
    finally:
        _relax_cache_tree(fixture.manager_home.parent)


def _probe_cache_publication_conflict(
    vectors: JsonObject,
    tmp_path: Path,
) -> _RejectionTrace:
    fixture = _publish_cache_fixture(tmp_path, vectors, "publication-conflict")
    try:
        conflicting = b"different winner bytes"
        source = fixture.manager_home.parent / "private-build" / "conflicting"
        source.write_bytes(conflicting)
        if os.name != "nt":
            source.chmod(0o700)
        cache.make_publication_source_private(source)
        receipt = metadata.build_receipt(
            fixture.build_input,
            metadata.BuildArtifact(
                path=fixture.build_input.artifact_path,
                sha256="sha256:" + hashlib.sha256(conflicting).hexdigest(),
                size=len(conflicting),
            ),
        )
        with pytest.raises(cache.CacheConflictError) as raised:
            fixture.backend.publish(
                cache.CachePublication(
                    input=fixture.build_input,
                    receipt_bytes=metadata.canonical_receipt_bytes(receipt),
                    artifact_source=source,
                ),
                guard=_HeldCacheGuard(),
            )
        return _RejectionTrace(raised.value.code, cache_lookup_performed=True)
    finally:
        _relax_cache_tree(fixture.manager_home.parent)


def _probe_untrusted_candidate(
    vector: JsonObject,
    tmp_path: Path,
) -> _RejectionTrace:
    candidate = vector["candidate"]
    candidate_raw = protocol_json.canonical_bytes(candidate["receipt"])
    candidate_input = metadata.parse_build_input(candidate["receipt"]["input"])
    receipt = metadata.verify_receipt(
        candidate_raw,
        expected_input=candidate_input,
        expected_cache_key=candidate["receipt"]["cache_key"],
        expected_receipt_sha256=candidate["receipt_sha256"],
    )
    artifact_bytes = base64.b64decode(candidate["artifact_bytes_base64"])
    checks = {
        "artifact_hash_matches": (
            "sha256:" + hashlib.sha256(artifact_bytes).hexdigest()
            == receipt.artifact.sha256
        ),
        "artifact_size_matches": len(artifact_bytes) == receipt.artifact.size,
        "cache_key_matches": receipt.cache_key == candidate["receipt"]["cache_key"],
        "input_matches": receipt.input == candidate_input,
        "receipt_hash_matches": (
            "sha256:" + hashlib.sha256(candidate_raw).hexdigest()
            == candidate["receipt_sha256"]
        ),
    }
    assert checks == candidate["internal_checks"]

    manager_home = tmp_path / "cache-untrusted-candidate" / "home"
    entry = (
        manager_home
        / "builds"
        / "go-v1"
        / receipt.cache_key.removeprefix("sha256:")
    )
    artifact = entry / Path(*receipt.artifact.path.split("/"))
    artifact.parent.mkdir(parents=True)
    (entry / "csk-receipt.ccj.json").write_bytes(candidate_raw)
    artifact.write_bytes(artifact_bytes)
    if os.name != "nt":
        artifact.chmod(0o500)
        manager_home.chmod(0o777)
    artifact_stat = artifact.lstat()
    assert candidate["artifact_is_regular_executable"] is (
        stat.S_ISREG(artifact_stat.st_mode)
        and (os.name == "nt" or bool(artifact_stat.st_mode & 0o111))
    )
    backend = cache.cache_for_manager_home(manager_home)
    try:
        inspection = backend.inspect(
            cache.CacheExpectation(
                input=candidate_input,
                receipt_sha256=candidate["receipt_sha256"],
            )
        )
        assert inspection.status is cache.CacheEntryStatus.UNTRUSTED_PROVENANCE
        assert vector["cache_boundary"] == {
            "all_components_link_safe": True,
            "manager_created": False,
            "other_principals_can_write": True,
            "owner_matches_manager_principal": False,
        }
        return _RejectionTrace(
            "untrusted_provenance",
            cache_inspection=inspection,
            cache_lookup_performed=True,
            extras={
                "dry_run": inspection.dry_run_outcome,
                "marker_current": inspection.reusable,
                "real_operation": (
                    "rebuild-from-validated-snapshot-into-protected-state"
                    if inspection.status is cache.CacheEntryStatus.UNTRUSTED_PROVENANCE
                    else ""
                ),
            },
        )
    finally:
        _relax_cache_tree(manager_home.parent)


def _probe_cache_rejection(
    name: str,
    vectors: JsonObject,
    tmp_path: Path,
) -> _RejectionTrace:
    portable = vectors["portable_identity"]
    raw = base64.b64decode(portable["stored_receipt_base64"])
    build_input = metadata.parse_build_input(portable["build_input"])
    if name == "self-consistent-forged-receipt-outside-protected-state":
        vector = next(
            item for item in vectors["rejection_cases"] if item["name"] == name
        )
        return _probe_untrusted_candidate(vector, tmp_path)
    if name == "cache-key-mismatch":
        with pytest.raises(metadata.BuildMetadataError) as raised:
            metadata.verify_receipt(
                raw,
                expected_input=build_input,
                expected_cache_key="sha256:" + "0" * 64,
                expected_receipt_sha256=portable["receipt_sha256"],
            )
        return _RejectionTrace(raised.value.code)
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
        return _RejectionTrace(raised.value.code)
    if name == "receipt-hash-mismatch":
        with pytest.raises(metadata.BuildMetadataError) as raised:
            metadata.verify_receipt(
                raw,
                expected_input=build_input,
                expected_cache_key=portable["cache_key"],
                expected_receipt_sha256="sha256:" + "0" * 64,
            )
        return _RejectionTrace(raised.value.code)
    if name in {"noncanonical-receipt-whitespace", "noncanonical-receipt-trailing-lf"}:
        poisoned = (b" " + raw) if name.endswith("whitespace") else (raw + b"\n")
        with pytest.raises(metadata.BuildMetadataError) as raised:
            metadata.verify_receipt(
                poisoned,
                expected_input=build_input,
                expected_cache_key=portable["cache_key"],
                expected_receipt_sha256="sha256:" + hashlib.sha256(poisoned).hexdigest(),
            )
        return _RejectionTrace(raised.value.code)
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
        return _RejectionTrace(raised.value.code)
    if name in {
        "artifact-hash-mismatch",
        "artifact-size-mismatch",
        "partial-cache-entry",
        "artifact-link",
        "artifact-special-file",
    }:
        return _probe_cache_backend_rejection(name, vectors, tmp_path)
    if name == "concurrent-publisher-different-bytes":
        return _probe_cache_publication_conflict(vectors, tmp_path)
    raise AssertionError(f"unbound cache rejection fixture {name!r}")


@contextmanager
def _projected_darwin_toolchain(
    tmp_path: Path,
    name: str,
    *,
    version_stdout: bytes = b"go version go1.25.5 darwin/arm64\n",
    telemetry_returncode: int = 0,
    telemetry_directory: Path | None = None,
) -> Iterator[tuple[toolchain.ToolchainConfig, toolchain._Host, Path]]:
    """Materialize the portable Darwin toolchain probe on every test host."""

    fixture = tmp_path / f"projected-toolchain-{name}"
    private_base = fixture / "private"
    private_base.mkdir(parents=True)
    repository = fixture / "repository"
    repository.mkdir()
    goroot = fixture / "goroot"
    executable = goroot / "bin" / "go"
    executable.parent.mkdir(parents=True)
    executable.write_bytes(b"\xcf\xfa\xed\xfe" + b"\x00" * 12)
    executable.chmod(0o755)
    runner = _RecordingProbeRunner(
        goroot.resolve(),
        version_stdout=version_stdout,
        telemetry_returncode=telemetry_returncode,
        telemetry_directory=telemetry_directory,
    )
    host = toolchain._Host(goos="darwin", goarch="arm64", windows=False)
    native_path_type = type(executable)
    real_lstat = native_path_type.lstat
    executable_key = os.path.normcase(os.path.abspath(executable))

    def darwin_fixture_lstat(path: Path) -> os.stat_result:
        observed = real_lstat(path)
        if os.path.normcase(os.path.abspath(path)) == executable_key:
            return os.stat_result((observed.st_mode | 0o111, *observed[1:]))
        return observed

    with mock.patch.object(
        native_path_type,
        "lstat",
        autospec=True,
        side_effect=darwin_fixture_lstat,
    ):
        yield (
            toolchain.ToolchainConfig(
                private_base=private_base.resolve(),
                operator_search_path=toolchain.OperatorSearchPath(()),
                forbidden_roots=(repository.resolve(),),
                go_executable=executable.resolve(),
                runner=runner,
            ),
            host,
            goroot.resolve(),
        )


def _probe_toolchain_rejection(
    name: str,
    tmp_path: Path,
) -> _RejectionTrace:
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
        return _RejectionTrace(raised.value.code)

    if name == "unsupported-go-pre-1-23":
        with pytest.raises(toolchain.ToolchainError) as raised:
            toolchain.parse_normalized_go_version(
                "go version go1.22.12 darwin/arm64"
            )
        return _RejectionTrace(raised.value.code)

    if name == "unsupported-go-future-family":
        with (
            _projected_darwin_toolchain(
                tmp_path,
                name,
                version_stdout=b"go version go1.99.0 darwin/arm64\n",
            ) as (config, host, _goroot),
            pytest.raises(toolchain.ToolchainError) as raised,
        ):
            toolchain._establish_toolchain(config, host)
        return _RejectionTrace(raised.value.code)

    if name == "wrong-go-executable-path":
        root = tmp_path / "wrong-go"
        root.mkdir()
        selected = root / "not-go"
        selected.write_bytes(b"GO")
        config = toolchain.ToolchainConfig(
            private_base=root.resolve(),
            operator_search_path=toolchain.OperatorSearchPath(()),
            forbidden_roots=(),
            go_executable=selected.resolve(),
        )
        with pytest.raises(toolchain.ToolchainError) as raised:
            toolchain._select_toolchain(
                config,
                toolchain._Host("darwin", "arm64", False),
                (),
            )
        return _RejectionTrace(raised.value.code)

    assert name == "toolchain-digest-mismatch"
    with _projected_darwin_toolchain(tmp_path, name) as (config, host, goroot):
        session = toolchain._establish_toolchain(config, host)
        try:
            (goroot / "VERSION").write_text("mutated\n", encoding="utf-8")
            with pytest.raises(toolchain.ToolchainError) as raised:
                session.verify()
        finally:
            session.release()
    assert raised.value.code == "toolchain_mutated"
    return _RejectionTrace("toolchain_digest_mismatch")


def _worker_environment_fixture(tmp_path: Path, name: str) -> tuple[JsonObject, Path, tuple[Path, ...]]:
    root = tmp_path / f"worker-environment-{name}"
    private = root / "private"
    goroot = root / "goroot"
    private.mkdir(parents=True)
    goroot.mkdir()
    environment: JsonObject = {
        "GOENV": "off",
        "GOTOOLCHAIN": "local",
        "LC_ALL": "C",
        "LANG": "C",
        "GO111MODULE": "on",
        "GOFLAGS": "",
        "GOPROXY": "off",
        "GOSUMDB": "off",
        "GOPRIVATE": "",
        "GONOPROXY": "none",
        "GONOSUMDB": "none",
        "GOVCS": "*:off",
        "GOWORK": "off",
        "CGO_ENABLED": "0",
        "GO_EXTLINK_ENABLED": "0",
        "GOEXPERIMENT": "",
        "GOROOT": os.fspath(goroot),
        "GOOS": "darwin",
        "GOARCH": "arm64",
        "GOARM64": "v8.0",
    }
    for variable in (
        "GOPATH",
        "GOMODCACHE",
        "GOCACHE",
        "GOTMPDIR",
        "HOME",
        "XDG_CONFIG_HOME",
        "PATH",
        "TMPDIR",
    ):
        environment[variable] = os.fspath(private)
    return environment, goroot, (private,)


def _probe_environment_rejection(name: str, tmp_path: Path) -> _RejectionTrace:
    environment, goroot, private_roots = _worker_environment_fixture(tmp_path, name)
    if name == "poisoned-path":
        (Path(environment["PATH"]) / "cc").write_bytes(b"compiler")
    elif name == "inherited-goflags-toolexec":
        environment["GOFLAGS"] = "-toolexec=/repository/compiler"
    elif name == "inherited-goenv":
        environment["GOENV"] = "/repository/go.env"
    elif name == "inherited-gowork":
        environment["GOWORK"] = "/repository/go.work"
    else:
        raise AssertionError(f"unbound environment rejection {name!r}")
    with pytest.raises(go_v1.GoV1Error) as raised:
        go_v1._validate_worker_environment(
            environment,
            goroot,
            private_roots,
            go_v1.PLATFORM_MACOS,
        )
    assert raised.value.code == go_v1.CODE_WORKER_PROTOCOL_INVALID
    detail = str(raised.value)
    assert (
        "compiler PATH directory is not empty" in detail
        if name == "poisoned-path"
        else f"unexpected {'GOFLAGS' if name == 'inherited-goflags-toolexec' else name.removeprefix('inherited-').upper()}" in detail
    )
    return _RejectionTrace("process_environment_poisoned")


def _probe_vcs_rejection(tmp_path: Path) -> _RejectionTrace:
    private = tmp_path / "vcs-private"
    private.mkdir()
    artifact = private / "bin" / "tool"
    poisoned = tuple(
        argument for argument in go_v1.BUILD_ARGUMENT_PREFIX
        if argument != "-buildvcs=false"
    ) + (os.fspath(artifact), ".")
    with pytest.raises(go_v1.GoV1Error) as raised:
        go_v1._validate_fixed_build_argv(poisoned, artifact, (private,))
    assert raised.value.code == go_v1.CODE_WORKER_PROTOCOL_INVALID
    assert "non-protocol go build vector" in str(raised.value)
    return _RejectionTrace("ambient_vcs_input_forbidden")


def _probe_child_process_rejection(tmp_path: Path) -> _RejectionTrace:
    root = tmp_path / "child-process"
    source_root = (root / "source").resolve()
    goroot = (root / "goroot").resolve()
    private = (root / "private").resolve()
    for directory in (source_root, goroot, private):
        directory.mkdir(parents=True)
    go_executable = goroot / "bin" / "go"
    wrong_tools = goroot / "pkg" / "tool" / "outside"
    process_identity = SimpleNamespace(
        go=SimpleNamespace(path=go_executable),
        tools=SimpleNamespace(path=wrong_tools),
        verify=lambda: None,
    )
    plan = SimpleNamespace(
        process_identity=process_identity,
        go_executable=go_executable,
        goroot=goroot,
        tool_directory=wrong_tools,
        worker_cache=private,
        directory=source_root,
        environment={"GOOS": "darwin", "GOARCH": "arm64"},
        list_argv=go_v1.LIST_ARGUMENTS,
        build_argv=(),
        artifact_path=private / "artifact",
        readonly_roots=(source_root, goroot),
        private_roots=(private,),
        platform=go_v1.PLATFORM_MACOS,
        probes=_synthetic_probes(go_v1.PLATFORM_MACOS),
        limits=go_v1.ResourceLimits(),
    )
    with (
        mock.patch.object(
            go_v1,
            "inventory_platform",
            return_value=go_v1.PLATFORM_MACOS,
        ),
        pytest.raises(go_v1.GoV1Error) as raised,
    ):
        go_v1._WorkerSession._validate_plan(SimpleNamespace(), plan)
    assert raised.value.code == go_v1.CODE_WORKER_IDENTITY_INVALID
    assert "unexpected Go tool directory" in str(raised.value)
    return _RejectionTrace("unexpected_child_process")


def _probe_process_rejection(name: str, tmp_path: Path) -> _RejectionTrace:
    if name in {
        "poisoned-path",
        "inherited-goflags-toolexec",
        "inherited-goenv",
        "inherited-gowork",
    }:
        return _probe_environment_rejection(name, tmp_path)
    if name == "vcs-metadata":
        return _probe_vcs_rejection(tmp_path)
    if name == "repository-local-fake-go":
        repository = tmp_path / "repository"
        (repository / "bin").mkdir(parents=True)
        fake = repository / "bin" / "go"
        fake.write_bytes(b"GO")
        config = toolchain.ToolchainConfig(
            private_base=tmp_path.resolve(),
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
        return _RejectionTrace(raised.value.code)
    if name == "telemetry-command-failure":
        with (
            _projected_darwin_toolchain(
                tmp_path,
                name,
                telemetry_returncode=17,
            ) as (config, host, _goroot),
            pytest.raises(toolchain.ToolchainError) as raised,
        ):
            toolchain._establish_toolchain(config, host)
        return _RejectionTrace(raised.value.code)
    if name == "telemetry-private-dir-escape":
        outside = tmp_path / "outside-telemetry"
        outside.mkdir()
        with (
            _projected_darwin_toolchain(
                tmp_path,
                name,
                telemetry_directory=outside,
            ) as (config, host, _goroot),
            pytest.raises(toolchain.ToolchainError) as raised,
        ):
            toolchain._establish_toolchain(config, host)
        return _RejectionTrace(raised.value.code)
    if name in {"external-link-required", "libgcc-fallback-attempt"}:
        stderr = (
            b"external linking required"
            if name == "external-link-required"
            else b"libgcc fallback"
        )
        with pytest.raises(go_v1.GoV1Error) as raised:
            go_v1._check_process_result(
                go_v1.ProcessResult(stderr=stderr, returncode=1),
                phase="build",
            )
        return _RejectionTrace(raised.value.code)
    if name == "child-outside-goroot-tools":
        return _probe_child_process_rejection(tmp_path)
    raise AssertionError(f"unbound process rejection fixture {name!r}")


def _probe_marker_context_rejection(
    vector: JsonObject,
    tmp_path: Path,
) -> _RejectionTrace:
    supplied = vector["input"]
    directive = supplied["directive"]
    assert isinstance(directive, str)
    go_mod = b"module example.com/embedmarker\n\ngo 1.23\n"
    source_bytes = (
        "package main\n\n"
        "import (\n"
        '\t_ "embed"\n'
        '\t"os"\n'
        ")\n\n"
        f"{directive}\n"
        "var marker []byte\n\n"
        "func main() {\n"
        "\t_, _ = os.Stdout.Write(marker)\n"
        "}\n"
    ).encode()
    manifest = _base_skill_manifest()
    manifest["commands"]["tool"] = dict(supplied["candidate_command"])
    snapshot = tmp_path / "marker-context-manifest"
    _materialize_skill_case(
        snapshot,
        "agent-skill.json",
        protocol_json.canonical_bytes(manifest),
    )
    first_marker = base64.b64decode(
        supplied["variants"][0]["marker_content_base64"]
    )
    (snapshot / ".csk-install.json").write_bytes(first_marker)
    (snapshot / "go.mod").write_bytes(go_mod)
    (snapshot / "main.go").write_bytes(source_bytes)
    try:
        skillspec.load_skill_spec(snapshot)
    except skillspec.SkillSpecError as error:
        observed_error = _classify_skill_spec_error(
            error,
            manifest=manifest,
            condition="root module embeds manager marker",
        )
    else:
        raise AssertionError("CocoaSkills accepted a root build source")

    build_source_hashes: list[str] = []
    legacy_hashes: list[str] = []
    for index, variant in enumerate(supplied["variants"]):
        marker_bytes = base64.b64decode(variant["marker_content_base64"])
        build_source_hash = hashing.build_source_sha256(
            [
                (".csk-install.json", marker_bytes),
                ("go.mod", go_mod),
                ("main.go", source_bytes),
            ]
        )
        build_source_hashes.append(build_source_hash)
        assert variant["build_source"] == {
            "algorithm": hashing.BUILD_SOURCE_ALGORITHM,
            "content_sha256": build_source_hash,
        }
        variant_root = tmp_path / f"marker-context-{index}"
        variant_root.mkdir()
        (variant_root / ".csk-install.json").write_bytes(marker_bytes)
        (variant_root / "go.mod").write_bytes(go_mod)
        (variant_root / "main.go").write_bytes(source_bytes)
        legacy_hash = hashing.content_sha256(variant_root)
        legacy_hashes.append(legacy_hash)
        assert legacy_hash == supplied["legacy_content_sha256"]

    cache_keys: list[str] = []
    go_commands: list[tuple[str, ...]] = []
    return _RejectionTrace(
        observed_error,
        cache_keys_created=len(cache_keys),
        go_commands=tuple(go_commands),
        extras={
            "build_source_hashes_equal": len(set(build_source_hashes)) == 1,
            "cache_keys_created": bool(cache_keys),
            "go_commands": [list(command) for command in go_commands],
            "legacy_content_hashes_equal": len(set(legacy_hashes)) == 1,
        },
    )


def _probe_context_rejection(
    name: str,
    vectors: JsonObject,
    conformance_root: Path,
    tmp_path: Path,
) -> _RejectionTrace:
    vector = next(item for item in vectors["rejection_cases"] if item["name"] == name)
    if name == "marker-embed-build-source-regression":
        return _probe_marker_context_rejection(vector, tmp_path)

    assert name == "build-root-content-in-context"
    fixture = conformance_root / vectors["fixture"]["root"]
    spec = skillspec.load_skill_spec(fixture)
    copied = whitelist.copy_context(
        fixture,
        tmp_path / "context-with-build-root",
        include_scripts=False,
        exclude_roots=spec.runtime_roots,
        build_roots=(),
    )
    visible = [
        path
        for path in copied
        if any(
            path == root or path.startswith(root + "/")
            for root in spec.build_roots
        )
    ]
    assert visible, "condition fixture did not expose build-root content"
    return _RejectionTrace("build_root_visible_in_context")


def _probe_execution_policy_rejection(
    name: str,
    vectors: JsonObject,
) -> _RejectionTrace:
    supplied = next(
        item["input"]
        for item in vectors["rejection_cases"]
        if item["name"] == name
    )
    raw_input = protocol_json.canonical_bytes(supplied["build_input"])
    derived_cache_key = "sha256:" + hashlib.sha256(raw_input).hexdigest()
    assert derived_cache_key == supplied["derived_cache_key"]
    assert supplied["build_input"]["policy"].get("execution_policy") == supplied[
        "execution_policy"
    ]
    cache_lookups: list[metadata.GoBuildInput] = []
    with pytest.raises(metadata.BuildMetadataError) as raised:
        metadata.parse_build_input(supplied["build_input"])
    portable = metadata.parse_build_input(vectors["portable_identity"]["build_input"])
    return _RejectionTrace(
        raised.value.code,
        cache_lookup_performed=bool(cache_lookups),
        extras={
            "aliases_portable_cache_key": (
                derived_cache_key == metadata.cache_key(portable)
            ),
            "cache_lookup_performed": bool(cache_lookups),
            "schema_valid": False,
        },
    )


def _probe_other_rejection(
    name: str,
    boundary: str,
    vectors: JsonObject,
    conformance_root: Path,
    tmp_path: Path,
) -> _RejectionTrace:
    if boundary == "toolchain":
        return _probe_toolchain_rejection(name, tmp_path)
    if boundary == "process":
        return _probe_process_rejection(name, tmp_path)
    if boundary == "cache":
        return _probe_cache_rejection(name, vectors, tmp_path)
    if boundary == "context":
        return _probe_context_rejection(
            name,
            vectors,
            conformance_root,
            tmp_path,
        )
    if boundary == "execution-policy":
        return _probe_execution_policy_rejection(name, vectors)
    raise AssertionError(f"unbound rejection boundary {boundary!r}")


def _observe_build_rejection_case(
    case: JsonObject,
    vectors: JsonObject,
    conformance_root: Path,
    tmp_path: Path,
) -> _RejectionTrace:
    name = case.get("name")
    assert isinstance(name, str) and name in _BUILD_REJECTION_BINDINGS, (
        f"unbound build rejection vector {name!r}"
    )
    binding = _BUILD_REJECTION_BINDINGS[name]
    assert case["boundary"] == binding.boundary
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
        if binding.condition is not None:
            assert case["input"].get("condition") == binding.condition
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
        elif binding.boundary == "execution-policy":
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
        else:
            assert set(case["input"]) == {"condition"}
            assert set(case["expected"]) == {
                "artifact_executed",
                "error",
                "result",
                "reuse",
            }

    if binding.boundary in {"manifest", "filesystem", "module"}:
        return _RejectionTrace(_probe_skill_spec_rejection(name, tmp_path))
    if binding.boundary == "dependency-graph":
        return _RejectionTrace(_probe_package_graph_rejection(name, tmp_path))
    if binding.boundary == "compiler-directive":
        return _RejectionTrace(_probe_compiler_rejection(name, tmp_path))
    return _probe_other_rejection(
        name,
        binding.boundary,
        vectors,
        conformance_root,
        tmp_path,
    )


def assert_build_rejection_case(
    case: JsonObject,
    vectors: JsonObject,
    conformance_root: Path,
    tmp_path: Path,
) -> None:
    trace = _observe_build_rejection_case(
        case,
        vectors,
        conformance_root,
        tmp_path,
    )
    _assert_observed_rejection(case["expected"], trace)


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
    def __init__(
        self,
        goroot: Path,
        *,
        version_stdout: bytes = b"go version go1.25.5 darwin/arm64\n",
        telemetry_returncode: int = 0,
        telemetry_directory: Path | None = None,
    ) -> None:
        self.goroot = goroot
        self.version_stdout = version_stdout
        self.telemetry_returncode = telemetry_returncode
        self.telemetry_directory = telemetry_directory
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
            return toolchain.ProbeResult(returncode=self.telemetry_returncode)
        if arguments == ("version",):
            return toolchain.ProbeResult(stdout=self.version_stdout)
        assert arguments == ("env", "-json", *toolchain.GO_ENV_FIELDS)
        telemetry = self.telemetry_directory or (
            Path(values["XDG_CONFIG_HOME"]) / "go" / "telemetry"
        )
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

    # The vector is only the expectation.  Every value on the other side of
    # this comparison was reconstructed from CocoaSkills operations and state.
    observed = observe_manager_lifecycle_case(name, fixture)
    assert set(observed) == expected_fields, (
        f"observed lifecycle binding {name!r} has unknown or missing fields"
    )
    assert case == observed, (
        f"lifecycle case {name!r} differs from observed CocoaSkills state:\n"
        f"expected={json.dumps(case, ensure_ascii=False, sort_keys=True)}\n"
        f"observed={json.dumps(observed, ensure_ascii=False, sort_keys=True)}"
    )
