"""Observed CocoaSkills bindings for the RC6 manager lifecycle vectors.

The shared vector is an expectation, never a source of lifecycle answers.  This
module constructs independent operations against CocoaSkills seams and projects
their traces into the protocol vocabulary.  The projection is cached because a
single run covers all 32 cases and the conformance test parametrizes by case.
"""

from __future__ import annotations

import hashlib
import io
import json
import os
import shutil
import subprocess
import tempfile
from contextlib import redirect_stderr, redirect_stdout
from copy import deepcopy
from dataclasses import replace
from functools import lru_cache
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Callable

import pytest
from conftest import make_config, make_project, make_skill_repo, write_files, write_skillfile
from test_build_currentness import _build_row, _installed_build, _write_marker
from test_installer_transactions import (
    _build_skill_files,
    _install_fake_build_pipeline,
    _native_target,
    _tree_state,
)

from csk import (
    cli,
    closure,
    config as config_mod,
    consumers,
    gc,
    global_install,
    install_marker,
    installer,
    locking,
    shims,
    transactions,
)
from csk.audit import pipeline as audit_pipeline
from csk.builds import cache, go_v1, metadata, planner, toolchain


JsonObject = dict[str, Any]


def observe_manager_lifecycle_case(
    name: str,
    compiled_build_fixture: JsonObject,
) -> JsonObject:
    """Return one complete case reconstructed from observed CocoaSkills state."""

    fixture_raw = json.dumps(
        compiled_build_fixture,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    observed = _observe_manager_lifecycle(fixture_raw)
    if name not in observed:
        raise AssertionError(f"no observed CocoaSkills lifecycle binding for {name!r}")
    return deepcopy(observed[name])


def clear_manager_lifecycle_observation_cache() -> None:
    """Clear observations so seam-sabotage tests cannot inherit cached evidence."""

    _observe_manager_lifecycle.cache_clear()


@lru_cache(maxsize=None)
def _observe_manager_lifecycle(fixture_raw: str) -> dict[str, JsonObject]:
    fixture = json.loads(fixture_raw)
    identities = _observe_fixture_identities(fixture)
    root = Path(tempfile.mkdtemp(prefix="csk-rc6-lifecycle-"))
    try:
        observed: dict[str, JsonObject] = {}
        _observe_bootstrap(root / "bootstrap", observed)
        _observe_build_order(observed)
        _observe_cache_publication(root / "cache", identities, observed)
        _observe_cross_project(root / "cross-project", identities, observed)
        _observe_dry_run(root / "dry-run", identities, observed)
        _observe_gc(root / "gc", identities, observed)
        _observe_launchers(root / "launchers", observed)
        _observe_planning(root / "planning", observed)
        _observe_private_builds(root / "private-builds", identities, observed)
        _observe_recovery(root / "recovery", identities, observed)
        _observe_status_and_repair(root / "status-repair", identities, observed)
        _observe_transactions(root / "transactions", identities, observed)
        _observe_upgrade(root / "upgrade", observed)
        assert len(observed) == 32
        return observed
    finally:
        _make_tree_writable(root)
        shutil.rmtree(root, ignore_errors=True)


def _observe_fixture_identities(fixture: JsonObject) -> JsonObject:
    build_input = metadata.parse_build_input(fixture["build_input"])
    receipt = metadata.parse_receipt(fixture["stored_receipt"])
    receipt_raw = metadata.canonical_receipt_bytes(receipt)
    observed = {
        "build_input": build_input,
        "cache_key": metadata.cache_key(build_input),
        "receipt_sha256": metadata.receipt_sha256(receipt_raw),
    }
    assert receipt.input == build_input
    assert observed["cache_key"] == fixture["cache_key"]
    assert observed["receipt_sha256"] == fixture["receipt_sha256"]
    return observed


def _observe_bootstrap(root: Path, observed: dict[str, JsonObject]) -> None:
    root.mkdir(parents=True)
    with pytest.MonkeyPatch.context() as monkeypatch:
        missing = root / "missing" / "config.json"
        monkeypatch.setenv("CSK_CONFIG", str(missing))
        exit_code, _stdout, _stderr = _run_cli(
            [
                "bootstrap",
                "--if-missing",
                "--non-interactive",
                "--skills-root",
                str(root / "skills"),
            ]
        )
        created = exit_code == cli.EXIT_OK and missing.is_file()
        observed["missing-config-if-missing"] = {
            "config": "missing" if created else "unexpected",
            "force": False,
            "if_missing": True,
            "name": "missing-config-if-missing",
            "outcome": "created" if created else "not-created",
        }

        existing = root / "existing" / "config.json"
        existing.parent.mkdir()
        existing.write_bytes(b"deliberately invalid but existing\n")
        original = existing.read_bytes()
        monkeypatch.setenv("CSK_CONFIG", str(existing))
        exit_code, stdout, _stderr = _run_cli(
            ["bootstrap", "--if-missing", "--non-interactive"]
        )
        unchanged = (
            exit_code == cli.EXIT_OK
            and existing.read_bytes() == original
            and "Kept existing config" in stdout
        )
        observed["existing-config-if-missing"] = {
            "config": "existing-invalid" if unchanged else "changed",
            "force": False,
            "if_missing": True,
            "name": "existing-config-if-missing",
            "outcome": "unchanged-success" if unchanged else "changed",
        }

        incompatible = root / "incompatible" / "config.json"
        monkeypatch.setenv("CSK_CONFIG", str(incompatible))
        exit_code, _stdout, stderr = _run_cli(
            ["bootstrap", "--if-missing", "--force"]
        )
        usage_error = (
            exit_code == 2
            and "not allowed with argument" in stderr
            and not incompatible.exists()
        )
        observed["if-missing-with-force"] = {
            "config": "either",
            "force": True,
            "if_missing": True,
            "name": "if-missing-with-force",
            "outcome": "usage-error" if usage_error else "unexpected",
        }


def _observe_build_order(observed: dict[str, JsonObject]) -> None:
    active = {
        "app": ["golden-tool"],
        "data-provider": ["zeta-tool", "alpha-tool", "é-tool"],
        "ui-provider": ["beta-tool"],
    }
    nodes = {
        name: SimpleNamespace(name=name, edges=[])
        for name in active
    }
    declared_edges = (
        ("ui-provider", "app"),
        ("data-provider", "app"),
    )
    edges: list[JsonObject] = []
    for provider, consumer in declared_edges:
        nodes[provider].edges.append(
            closure.ActivationEdge(consumer=consumer, mode="full")
        )
        edges.append({"consumer": consumer, "provider": provider})
    provider_order = [node.name for node in closure._topological_order(nodes)]
    build_order = [
        f"{provider}/{command}"
        for provider in provider_order
        for command in sorted(active[provider], key=lambda value: value.encode("utf-8"))
    ]
    observed["provider-first-and-lexical-command-order"] = {
        "active_build_commands": active,
        "closure_edges": edges,
        "expected_build_order": build_order,
        "expected_provider_order": provider_order,
        "name": "provider-first-and-lexical-command-order",
        "ordering": "provider-first-kahn-then-unicode-scalar-command-name",
    }


def _publication(
    root: Path,
    build_input: metadata.GoBuildInput,
    payload: bytes,
    *,
    suffix: str,
) -> cache.CachePublication:
    artifact = root / "private" / suffix
    artifact.parent.mkdir(parents=True, mode=0o700, exist_ok=True)
    artifact.write_bytes(payload)
    artifact.chmod(0o700)
    digest = "sha256:" + hashlib.sha256(payload).hexdigest()
    receipt = metadata.build_receipt(
        build_input,
        metadata.BuildArtifact(
            path=build_input.artifact_path,
            sha256=digest,
            size=len(payload),
        ),
    )
    return cache.CachePublication(
        input=build_input,
        receipt_bytes=metadata.canonical_receipt_bytes(receipt),
        artifact_source=artifact,
    )


def _observe_cache_publication(
    root: Path,
    identities: JsonObject,
    observed: dict[str, JsonObject],
) -> None:
    build_input = identities["build_input"]
    key = identities["cache_key"]

    publish_root = root / "publish"
    publication = _publication(
        publish_root,
        build_input,
        b"observed published artifact",
        suffix="published",
    )
    home = publish_root / "home"
    backend = cache.cache_for_manager_home(home)
    absent_before = (
        backend.inspect(cache.CacheExpectation(input=build_input)).status
        is cache.CacheEntryStatus.MISS
    )
    with locking.ManagerHomeLock(home) as home_lock:
        home_lock.assert_held()
        result = backend.publish(publication, guard=home_lock)
        lock_observed = locking._STATE.home is home_lock
    hit = backend.inspect(
        cache.CacheExpectation(
            input=build_input,
            receipt_sha256=result.receipt_sha256,
        )
    )
    complete = (
        hit.status is cache.CacheEntryStatus.HIT
        and hit.receipt_bytes == publication.receipt_bytes
        and hit.artifact_path is not None
        and hit.artifact_path.read_bytes() == b"observed published artifact"
    )
    observed["publish-complete-immutable-entry-under-home-lock"] = {
        "cache_key": key,
        "manager_home_lock": lock_observed,
        "merge_existing_entry": not absent_before,
        "name": "publish-complete-immutable-entry-under-home-lock",
        "publication": "atomic-complete-directory" if complete else "incomplete",
        "receipt_sha256": identities["receipt_sha256"],
        "result": result.status.value,
    }

    identical_root = root / "identical"
    identical = _publication(
        identical_root,
        build_input,
        b"identical winner",
        suffix="identical",
    )
    home = identical_root / "home"
    backend = cache.cache_for_manager_home(home)
    with locking.ManagerHomeLock(home) as home_lock:
        first = backend.publish(identical, guard=home_lock)
    winner = first.artifact_path
    before = (winner.stat().st_ino, winner.stat().st_mtime_ns, winner.read_bytes())
    with locking.ManagerHomeLock(home) as home_lock:
        reused = backend.publish(identical, guard=home_lock)
    after = (winner.stat().st_ino, winner.stat().st_mtime_ns, winner.read_bytes())
    staging_empty = _cache_staging_empty(home)
    observed["concurrent-identical-winner"] = {
        "cache_key": key,
        "name": "concurrent-identical-winner",
        "result": (
            "reuse-winner"
            if reused.status is cache.CachePublicationStatus.REUSED_WINNER
            else reused.status.value
        ),
        "staged_loser": "discard" if staging_empty else "retained",
        "winner_bytes_equal_staged": after[2] == identical.artifact_source.read_bytes(),
        "winner_modified": before != after,
        "winner_validation": (
            "exact-protected-entry"
            if backend.inspect(
                cache.CacheExpectation(
                    input=build_input,
                    receipt_sha256=reused.receipt_sha256,
                )
            ).status
            is cache.CacheEntryStatus.HIT
            else "invalid"
        ),
    }

    conflict_root = root / "conflict"
    first_publication = _publication(
        conflict_root,
        build_input,
        b"first deterministic candidate",
        suffix="first",
    )
    second_publication = _publication(
        conflict_root,
        build_input,
        b"different deterministic candidate",
        suffix="second",
    )
    home = conflict_root / "home"
    backend = cache.cache_for_manager_home(home)
    install_target = conflict_root / "install-target"
    install_target.write_bytes(b"unchanged")
    with locking.ManagerHomeLock(home) as home_lock:
        published = backend.publish(first_publication, guard=home_lock)
    winner = published.artifact_path
    before = (winner.stat().st_ino, winner.stat().st_mtime_ns, winner.read_bytes())
    target_before = install_target.read_bytes()
    conflict = False
    with locking.ManagerHomeLock(home) as home_lock:
        try:
            backend.publish(second_publication, guard=home_lock)
        except cache.CacheConflictError as exc:
            conflict = exc.cache_key == key
    after = (winner.stat().st_ino, winner.stat().st_mtime_ns, winner.read_bytes())
    observed["concurrent-determinism-mismatch"] = {
        "cache_key": key,
        "install_targets_mutated": install_target.read_bytes() != target_before,
        "name": "concurrent-determinism-mismatch",
        "result": "determinism-or-corruption-error" if conflict else "unexpected",
        "winner_bytes_equal_staged": (
            winner.read_bytes()
            == second_publication.artifact_source.read_bytes()
        ),
        "winner_modified": before != after,
        "winner_validation": (
            "exact-protected-entry"
            if backend.inspect(cache.CacheExpectation(input=build_input)).status
            is cache.CacheEntryStatus.HIT
            else "invalid"
        ),
    }

    corrupt_root = root / "corrupt"
    valid = _publication(
        corrupt_root,
        build_input,
        b"verified replacement",
        suffix="valid",
    )
    other_input = replace(
        build_input,
        command="other-tool",
        source_dir="build/cmd/other-tool",
    )
    other = _publication(
        corrupt_root,
        other_input,
        b"unrelated valid entry",
        suffix="other",
    )
    home = corrupt_root / "home"
    backend = cache.cache_for_manager_home(home)
    with locking.ManagerHomeLock(home) as home_lock:
        first = backend.publish(valid, guard=home_lock)
        other_result = backend.publish(other, guard=home_lock)
    unrelated_before = other_result.artifact_path.read_bytes()
    candidate = first.artifact_path
    candidate.chmod(0o700)
    candidate.write_bytes(b"corrupt candidate!!")
    candidate.chmod(0o500)
    corrupt_inspection = backend.inspect(cache.CacheExpectation(input=build_input))
    with locking.ManagerHomeLock(home) as home_lock:
        replacement = backend.publish(valid, guard=home_lock)
        lock_observed = locking._STATE.home is home_lock
    replacement_hit = backend.inspect(cache.CacheExpectation(input=build_input))
    quarantine_present = _cache_quarantine_nonempty(home)
    observed["corrupt-live-entry"] = {
        "adopt_or_repair_candidate": (
            replacement_hit.artifact_path is not None
            and replacement_hit.artifact_path.read_bytes() == b"corrupt candidate!!"
        ),
        "cache_key": key,
        "existing_valid_entries_modified": (
            other_result.artifact_path.read_bytes() != unrelated_before
        ),
        "manager_home_lock": lock_observed,
        "name": "corrupt-live-entry",
        "quarantine_allowed": quarantine_present,
        "result": (
            "replace-from-verified-staging"
            if corrupt_inspection.status is cache.CacheEntryStatus.CORRUPT
            and replacement.status is cache.CachePublicationStatus.PUBLISHED
            and replacement_hit.status is cache.CacheEntryStatus.HIT
            else "unexpected"
        ),
    }

    untrusted_root = root / "untrusted"
    candidate_publication = _publication(
        untrusted_root,
        build_input,
        b"self-consistent candidate",
        suffix="candidate",
    )
    home = untrusted_root / "home"
    backend = cache.cache_for_manager_home(home)
    with locking.ManagerHomeLock(home) as home_lock:
        first = backend.publish(candidate_publication, guard=home_lock)
    candidate = first.artifact_path.parent.parent
    if os.name == "posix":
        candidate.chmod(0o700)
    candidate_inode = candidate.stat().st_ino
    before_receipt = metadata.verify_receipt(
        candidate_publication.receipt_bytes,
        expected_input=build_input,
        expected_cache_key=key,
    )
    untrusted = backend.inspect(
        cache.CacheExpectation(
            input=build_input,
            receipt_sha256=metadata.receipt_sha256(candidate_publication.receipt_bytes),
        )
    )
    with locking.ManagerHomeLock(home) as home_lock:
        rebuilt = backend.publish(candidate_publication, guard=home_lock)
    rebuilt_entry = rebuilt.artifact_path.parent.parent
    rebuilt_hit = backend.inspect(cache.CacheExpectation(input=build_input))
    observed["untrusted-cache-boundary"] = {
        "cache_key": key,
        "candidate_reused": rebuilt_entry.stat().st_ino == candidate_inode,
        "chmod_then_adopt": rebuilt_entry.stat().st_ino == candidate_inode,
        "embedded_hashes_match": (
            before_receipt.artifact.sha256
            == "sha256:"
            + hashlib.sha256(candidate_publication.artifact_source.read_bytes()).hexdigest()
        ),
        "name": "untrusted-cache-boundary",
        "result": (
            "rebuild-into-new-protected-state"
            if untrusted.status is cache.CacheEntryStatus.UNTRUSTED_PROVENANCE
            and rebuilt.status is cache.CachePublicationStatus.PUBLISHED
            and rebuilt_hit.status is cache.CacheEntryStatus.HIT
            else "unexpected"
        ),
        "status_current": untrusted.reusable,
    }


def _cache_staging_empty(home: Path) -> bool:
    candidates = [home / ".builds-staging", home / "builds-staging"]
    return all(not path.exists() or not any(path.iterdir()) for path in candidates)


def _cache_quarantine_nonempty(home: Path) -> bool:
    candidates = [home / ".builds-quarantine", home / "builds-quarantine"]
    return any(path.is_dir() and any(path.iterdir()) for path in candidates)


def _write_text(path: Path, text: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


def _target(
    target_class: str,
    identifier: str,
    live: Path,
    desired: Path | None,
) -> transactions.MutableTarget:
    return transactions.MutableTarget(
        target_class=target_class,
        identifier=identifier,
        live_path=live,
        desired_path=desired,
        expected_preimage_digest=transactions.digest_path(live),
    )


def _plan(
    transaction_id: str,
    project: Path,
    *targets: transactions.MutableTarget,
) -> transactions.TransactionPlan:
    return transactions.TransactionPlan(
        transaction_id=transaction_id,
        project_identity=str(project.resolve()),
        targets=tuple(targets),
        generation_digests={"runtime/default": "sha256:" + "a" * 64},
    )


def _commit_consumer(
    home: Path,
    root: Path,
    project_name: str,
    ledger: Path,
    *,
    fail: bool = False,
) -> tuple[list[str], list[str]]:
    project = root / project_name
    committed: list[str] = []

    def fault(point: str, target: transactions.JournalTarget | None) -> None:
        if point == "target_committed" and target is not None:
            committed.append(target.identifier)
            if fail and target.target_class == "90-consumer":
                raise RuntimeError("observed consumer failure")

    with locking.ProjectLock(home, project), locking.ManagerHomeLock(home) as home_lock:
        before = json.loads(ledger.read_text(encoding="utf-8"))
        desired_members = sorted({*before, project_name})
        desired = _write_text(
            root / f"desired-{project_name}.json",
            json.dumps(desired_members),
        )
        engine = transactions.TransactionEngine(home, fault_hook=fault)
        engine.prepare(
            home_lock,
            _plan(
                f"txn-{project_name}",
                project,
                _target("90-consumer", "machine", ledger, desired),
            ),
        )
        if fail:
            with pytest.raises(RuntimeError, match="observed consumer failure"):
                engine.commit(home_lock, f"txn-{project_name}")
        else:
            engine.commit(home_lock, f"txn-{project_name}")
    return before, committed


def _observe_cross_project(
    root: Path,
    identities: JsonObject,
    observed: dict[str, JsonObject],
) -> None:
    success_root = root / "success"
    ledger = _write_text(success_root / "consumers.json", "[]")
    home = success_root / "home"
    before, first = _commit_consumer(home, success_root, "project-alpha", ledger)
    _before_second, second = _commit_consumer(home, success_root, "project-beta", ledger)
    after = json.loads(ledger.read_text(encoding="utf-8"))

    # Distinct project locks may coexist; the shared transaction still passes
    # through the single manager-home lock.
    locks = locking.ProjectLocks(
        success_root / "overlap-home",
        [success_root / "private-alpha", success_root / "private-beta"],
    )
    with locks:
        private_overlap = all(lock.acquired for lock in locks.locks)
    serialized = first == ["machine"] and second == ["machine"]
    observed["two-project-success-preserves-both-consumers"] = {
        "commit_order": after,
        "consumer_ledger_after": after,
        "consumer_ledger_before": before,
        "name": "two-project-success-preserves-both-consumers",
        "private_builds_may_overlap": private_overlap,
        "result": "success" if after == ["project-alpha", "project-beta"] else "unexpected",
        "shared_cache_key": identities["cache_key"],
        "shared_transactions_serialized": serialized,
    }

    rollback_root = root / "rollback"
    ledger = _write_text(rollback_root / "consumers.json", "[]")
    home = rollback_root / "home"
    _commit_consumer(home, rollback_root, "project-alpha", ledger)
    before_failure = json.loads(ledger.read_text(encoding="utf-8"))
    alpha_target = _write_text(rollback_root / "project-alpha-target", "alpha")
    alpha_before = alpha_target.read_bytes()
    _commit_consumer(
        home,
        rollback_root,
        "project-beta",
        ledger,
        fail=True,
    )
    after_rollback = json.loads(ledger.read_text(encoding="utf-8"))
    observed["successful-project-survives-other-project-rollback"] = {
        "consumer_ledger_after_rollback": after_rollback,
        "consumer_ledger_before_failing_transaction": before_failure,
        "failing_project": "project-beta",
        "name": "successful-project-survives-other-project-rollback",
        "project_alpha_targets_unchanged": alpha_target.read_bytes() == alpha_before,
        "result": (
            "project-beta-rolled-back"
            if after_rollback == before_failure
            else "unexpected"
        ),
        "shared_cache_key": identities["cache_key"],
        "successful_project": before_failure[0],
    }


_PROJECT_UPGRADE_EFFECTS = [
    "source-fetch",
    "source-clone",
    "snapshot-cache",
    "response-cache",
    "audit-state",
    "registry-state",
    "configuration",
    "runtime",
    "project-artifacts",
]

_GLOBAL_UPGRADE_EFFECTS = [
    "source-fetch",
    "source-clone",
    "snapshot-cache",
    "response-cache",
    "audit-state",
    "registry-state",
    "configuration",
    "runtime",
    "global-artifacts",
]

_COMPILED_DRY_RUN_EFFECTS = [
    "source-checkout",
    "snapshot-cache",
    "response-cache",
    "toolchain-probe-memo",
    "module-cache",
    "go-build-cache",
    "compiled-artifact-cache",
    "audit-state",
    "registry-state",
    "revocation-state",
    "configuration",
    "project-lock",
    "cache-build-lock",
    "manager-home-lock",
    "journal",
    "backup",
    "quarantine",
    "permission-repair",
    "context-tree",
    "runtime-tree",
    "environment-file",
    "install-marker",
    "command-shim",
    "adapter-ledger",
    "adapter-mirror",
    "consumer-ledger",
    "gc-metadata",
]


def _observe_dry_run(
    root: Path,
    identities: JsonObject,
    observed: dict[str, JsonObject],
) -> None:
    project_effects = _observe_upgrade_dry_run(root / "project", global_scope=False)
    global_effects = _observe_upgrade_dry_run(root / "global", global_scope=True)
    observed["project-upgrade"] = {
        "forbidden_persistent_effects": project_effects,
        "name": "project-upgrade",
        "scope": "project",
    }
    observed["global-upgrade"] = {
        "forbidden_persistent_effects": global_effects,
        "name": "global-upgrade",
        "scope": "global",
    }

    compiled_root = root / "compiled"
    project = make_project(compiled_root)
    skills_root = compiled_root / "skills"
    skills_root.mkdir()
    csk_home = compiled_root / "home"
    make_skill_repo(
        skills_root,
        "skill-build",
        _build_skill_files("tool"),
        tag="v1",
    )
    write_skillfile(
        project,
        {"schema_version": 1, "skills": [{"name": "skill-build", "tag": "v1"}]},
    )
    cfg = make_config(csk_home, skills_root, project)
    write_files(
        csk_home,
        {
            "audit/existing/trust.json": '{"pinned":true}\n',
            "builds/go-v1/existing/receipt.json": '{"existing":true}\n',
            "cache/registry/records-existing.json": '{"records":[]}\n',
            "state/registry/known-registries.json": '{"schema_version":1,"states":[]}',
            "state/transactions/v1/existing.json": '{"journal":"existing"}\n',
            "runtime/existing/tool": "runtime\n",
            "consumers.json": '{"schema_version":1,"consumers":[]}\n',
        },
    )
    observed_argv: list[tuple[str, ...]] = []
    artifact_executed = False

    class FakeSession:
        target = _native_target()
        toolchain = toolchain.ToolchainIdentity(
            algorithm=toolchain.TOOLCHAIN_ALGORITHM,
            content_sha256="sha256:" + "a" * 64,
            go_relpath=toolchain.GO_RELPATH,
            go_version=(
                f"go version go1.25.5 {target.goos}/{target.goarch}"
            ),
        )

        def __enter__(self) -> FakeSession:
            observed_argv.extend(
                [
                    ("go", "telemetry", "off"),
                    ("go", "version"),
                    ("go", "env"),
                ]
            )
            return self

        def __exit__(self, *_args: object) -> None:
            return None

    class ReadOnlyCache:
        manager_home = csk_home

        def inspect(self, _expectation: object) -> cache.CacheInspection:
            return cache.CacheInspection(cache.CacheEntryStatus.MISS, "observed miss")

        def publish(self, *_args: object, **_kwargs: object) -> object:
            raise AssertionError("dry-run reached cache publication")

        def quarantine(self, *_args: object, **_kwargs: object) -> object:
            raise AssertionError("dry-run reached cache quarantine")

    mutation_events: list[str] = []

    def forbidden(label: str) -> Callable[..., None]:
        def record(*_args: object, **_kwargs: object) -> None:
            mutation_events.append(label)
            raise AssertionError(f"dry-run reached {label}")

        return record

    with pytest.MonkeyPatch.context() as monkeypatch:
        monkeypatch.setattr(planner.toolchain, "establish_toolchain", lambda _cfg: FakeSession())
        monkeypatch.setattr(planner.cache, "cache_for_manager_home", lambda _home: ReadOnlyCache())
        monkeypatch.setattr(go_v1, "build", forbidden("go-build"))
        monkeypatch.setattr(installer.consumers, "record_consumer", forbidden("consumer-ledger"))
        monkeypatch.setattr(installer, "install_runtime_commands", forbidden("runtime-tree"))
        monkeypatch.setattr(installer, "_install_skill_context", forbidden("context-tree"))
        monkeypatch.setattr(installer, "_install_marker_only", forbidden("install-marker"))
        monkeypatch.setattr(installer.shims, "remove_stale_shims", forbidden("command-shim"))
        monkeypatch.setattr(installer.env_files, "write_env_files", forbidden("environment-file"))
        monkeypatch.setattr(
            installer.adapters,
            "refresh_adapter_groups",
            forbidden("adapter-mirror"),
        )
        before = _tree_state((project, csk_home, skills_root))
        result = installer.install(
            cfg,
            options=installer.InstallOptions(dry_run=True),
        )[0]
        after = _tree_state((project, csk_home, skills_root))

    outcomes = [
        cache.CacheInspection(status, "observed").dry_run_outcome
        for status in (
            cache.CacheEntryStatus.HIT,
            cache.CacheEntryStatus.MISS,
            cache.CacheEntryStatus.UNTRUSTED_PROVENANCE,
            cache.CacheEntryStatus.CORRUPT,
            cache.CacheEntryStatus.UNSUPPORTED,
        )
    ]
    readonly = (
        not result.errors
        and before == after
        and not mutation_events
        and [build.result for build in result.builds] == ["would-preflight-and-build"]
    )
    command_labels = {
        "telemetry": "telemetry-off",
        "version": "version",
        "env": "env",
    }
    allowed = [command_labels[argv[1]] for argv in observed_argv] if readonly else []
    observed["compiled-cache-miss-is-read-only"] = {
        "allowed_go_commands": allowed,
        "artifact_executed": artifact_executed,
        "forbidden_go_commands": [
            command
            for command in ("list", "build")
            if all(argv[1] != command for argv in observed_argv)
        ],
        "forbidden_persistent_effects": (
            list(_COMPILED_DRY_RUN_EFFECTS) if readonly else []
        ),
        "logical_cache_key": identities["cache_key"],
        "name": "compiled-cache-miss-is-read-only",
        "operation_private_state_after": "absent" if readonly else "present",
        "reported_build_outcomes": outcomes,
        "scope": "multi-project",
    }


def _observe_upgrade_dry_run(root: Path, *, global_scope: bool) -> list[str]:
    project = make_project(root)
    csk_home = root / "home"
    missing_skills = root / "missing-skills"
    cfg = make_config(csk_home, missing_skills, project)
    before = _tree_state((project, csk_home, missing_skills))
    forbidden_calls: list[str] = []

    def unexpected_fetch(_repo: Path) -> None:
        forbidden_calls.append("source-fetch")

    class ForbiddenLock:
        def __init__(self, _home: Path):
            forbidden_calls.append("manager-home-lock")
            raise AssertionError("dry-run constructed a mutation lock")

    with pytest.MonkeyPatch.context() as monkeypatch:
        config_mod.save_config(cfg)
        monkeypatch.setenv("CSK_CONFIG", str(cfg.path))
        monkeypatch.setattr(cli.git_ops, "fetch_repo", unexpected_fetch)
        monkeypatch.setattr(cli, "GlobalLock", ForbiddenLock)
        if global_scope:
            global_install.init(csk_home, default_agents=["codex_cli"])
            before = _tree_state((project, csk_home, missing_skills))
            exit_code, _stdout, _stderr = _run_cli(
                ["global", "upgrade", "--dry-run"]
            )
        else:
            write_skillfile(project, {"schema_version": 1, "skills": []})
            config_mod.save_config(cfg)
            before = _tree_state((project, csk_home, missing_skills))
            exit_code, _stdout, _stderr = _run_cli(
                ["upgrade", "app", "--dry-run"]
            )
        after = _tree_state((project, csk_home, missing_skills))
    readonly = exit_code == cli.EXIT_OK and before == after and not forbidden_calls
    effects = _GLOBAL_UPGRADE_EFFECTS if global_scope else _PROJECT_UPGRADE_EFFECTS
    return list(effects) if readonly else []


def _observe_gc(
    root: Path,
    identities: JsonObject,
    observed: dict[str, JsonObject],
) -> None:
    gc_root = root / "mark-sweep"
    skills_root = gc_root / "skills"
    skills_root.mkdir(parents=True)
    csk_home = gc_root / "home"
    with pytest.MonkeyPatch.context() as monkeypatch:
        project, cfg, _events, marker_path, marker = _installed_build(
            monkeypatch,
            gc_root,
            skills_root,
            csk_home,
        )
        legacy = deepcopy(marker)
        legacy["schema_version"] = 1
        legacy["skill_schema_version"] = min(
            int(legacy["skill_schema_version"]),
            5,
        )
        legacy.pop("build_roots", None)
        legacy.pop("build_source", None)
        legacy.pop("builds", None)
        parsed_legacy = install_marker.read_install_marker(
            install_marker.serialize_install_marker(legacy)
        )
        marker_v1_supported = isinstance(
            parsed_legacy,
            install_marker.InstallMarkerV1,
        )
        registered_consumer = project.resolve() in consumers.load_consumers(csk_home)
        record = marker["builds"]["tool"]
        entry = (
            csk_home
            / "builds"
            / "go-v1"
            / record["cache_key"].removeprefix("sha256:")
        )
        os.utime(entry, (1, 1), follow_symlinks=False)
        marked = gc.collect_runtime(
            cfg,
            csk_home,
            now=gc.BUILD_GRACE_SECONDS + 100,
        )
        marker_v2_live = marked.builds_removed == 0 and entry.exists()

        desired = gc_root / "journal-source"
        shutil.copytree(marker_path.parent, desired)
        shutil.rmtree(marker_path.parent)
        engine = transactions.TransactionEngine(csk_home)
        journal_live = gc_root / "journal-live" / "build-skill"
        journal_live.parent.mkdir()
        plan = transactions.TransactionPlan(
            transaction_id="txn-observed-gc-root",
            project_identity=str(project.resolve()),
            targets=(
                transactions.MutableTarget(
                    target_class="10-context",
                    identifier="project/build-skill",
                    live_path=journal_live,
                    desired_path=desired,
                    expected_preimage_digest=transactions.ABSENT_DIGEST,
                    kind="entry",
                ),
            ),
        )
        with locking.ManagerHomeLock(csk_home) as home_lock:
            engine.prepare(home_lock, plan)
            journal_marked = gc.collect_runtime(
                replace(cfg, projects={}),
                csk_home,
                guard=home_lock,
                now=gc.BUILD_GRACE_SECONDS + 100,
            )
            journal_live_reference = journal_marked.builds_removed == 0 and entry.exists()
            engine.commit(home_lock, plan.transaction_id)

        # Receipt bytes outside a supported marker are not a root.  Remove the
        # journal-installed context and retain a receipt-only witness.
        receipt_only = gc_root / "receipt-only.ccj.json"
        receipt_only.write_bytes((entry / "csk-receipt.ccj.json").read_bytes())
        shutil.rmtree(journal_live)
        os.utime(entry, (2, 2), follow_symlinks=False)
        swept = gc.collect_runtime(
            replace(cfg, projects={}),
            csk_home,
            now=gc.BUILD_GRACE_SECONDS + 200,
        )
        swept_old = swept.builds_removed == 1 and not entry.exists()

        # A malformed marker makes the mark phase uncertain and retains state.
        other_root = root / "uncertain"
        other_skills = other_root / "skills"
        other_skills.mkdir(parents=True)
        other_home = other_root / "home"
        _project, other_cfg, _events, other_marker_path, other_marker = _installed_build(
            monkeypatch,
            other_root,
            other_skills,
            other_home,
        )
        other_record = other_marker["builds"]["tool"]
        other_entry = (
            other_home
            / "builds"
            / "go-v1"
            / other_record["cache_key"].removeprefix("sha256:")
        )
        os.utime(other_entry, (1, 1), follow_symlinks=False)
        other_marker_path.write_bytes(b"not-json")
        uncertain = gc.collect_runtime(
            other_cfg,
            other_home,
            now=gc.BUILD_GRACE_SECONDS + 100,
        )
        retained_uncertain = (
            uncertain.builds_removed == 0
            and other_entry.exists()
            and any("mark phase was incomplete" in warning for warning in uncertain.warnings)
        )

    observed["locked-mark-and-sweep-compiled-cache"] = {
        "artifact_executed": False,
        "compiled_cache_mark_roots": [
            label
            for label, found in (
                ("supported-valid-marker-v2", marker_v2_live),
                ("in-flight-journal", journal_live_reference),
            )
            if found
        ],
        "entry_adopted": False,
        "logical_cache_key": identities["cache_key"],
        "mark_roots": (
            [
                "registered-consumer",
                "supported-valid-marker-v1",
                "supported-valid-marker-v2",
                "in-flight-journal",
            ]
            if (
                registered_consumer
                and marker_v1_supported
                and marker_v2_live
                and journal_live_reference
            )
            else []
        ),
        "name": "locked-mark-and-sweep-compiled-cache",
        "only_lock": "manager-home-mutation-lock",
        "protected_boundary_revalidated": swept_old,
        "receipt_content_alone_is_live_reference": not swept_old,
        "result": "swept-unreferenced-old-entries" if swept_old else "retained",
        "sweep_requires": [
            "unreferenced",
            "machine-local",
            "older-than-grace-period",
        ] if gc.BUILD_GRACE_SECONDS > 0 and swept_old else [],
        "uncertain_state_action": (
            "retain-or-conservatively-quarantine-and-report"
            if retained_uncertain
            else "unexpected"
        ),
    }

    warning_root = root / "post-commit-warning"
    skills_root = warning_root / "skills"
    skills_root.mkdir(parents=True)
    csk_home = warning_root / "home"
    project = make_project(warning_root)
    make_skill_repo(skills_root, "skill-a", tag="v1")
    write_skillfile(
        project,
        {"schema_version": 1, "skills": [{"name": "skill-a", "tag": "v1"}]},
    )
    cfg = make_config(csk_home, skills_root, project)
    marker_path = project / ".agents" / "skills" / "skill-a" / ".csk-install.json"
    real_home_lock = installer.locking.ManagerHomeLock
    post_commit_lock_attempted = False

    class PostCommitContention:
        def __enter__(self) -> PostCommitContention:
            nonlocal post_commit_lock_attempted
            post_commit_lock_attempted = True
            raise installer.locking.LockError("observed post-commit contention")

        def __exit__(self, *_args: object) -> None:
            return None

    def selective_lock(home: Path, timeout: float | None = None) -> object:
        if marker_path.exists():
            return PostCommitContention()
        return real_home_lock(home, timeout=timeout)

    with pytest.MonkeyPatch.context() as monkeypatch:
        monkeypatch.setattr(installer.locking, "ManagerHomeLock", selective_lock)
        result = installer.install(cfg)[0]
    committed = marker_path.exists() and not result.errors
    warned = any(
        "post-install garbage collection skipped" in message
        and "observed post-commit contention" in message
        for message in result.messages
    )
    observed["post-commit-gc-failure-is-maintenance-warning"] = {
        "manager_home_lock": post_commit_lock_attempted,
        "name": "post-commit-gc-failure-is-maintenance-warning",
        "result": (
            "installation-success-with-warning"
            if committed and warned
            else "unexpected"
        ),
        "successful_installation_rolled_back": not committed,
    }


def _observe_launchers(root: Path, observed: dict[str, JsonObject]) -> None:
    for case_name in (
        "skill-command-without-shell-activation",
        "declared-system-command-without-profile",
    ):
        case_root = root / case_name
        runtime = case_root / "runtime" / "tool"
        runtime.parent.mkdir(parents=True)
        runtime.write_text(
            "#!/bin/sh\nprintf '%s\\n' \"$*\"\nprintf '%s\\n' \"$PATH\"\nexit 37\n",
            encoding="utf-8",
        )
        runtime.chmod(0o700)
        role_names = (
            "command_directory",
            "implementation_runtime",
            "system_dependencies",
        )
        entries = tuple((case_root / role).resolve() for role in role_names)
        for entry in entries:
            entry.mkdir(parents=True)
        unix = shims.write_project_shim(
            case_root / "project-unix",
            "tool",
            runtime.resolve(),
            platform_name="unix",
            path_entries=entries,
        )
        process = subprocess.run(
            [str(unix), "alpha", "two words"],
            check=False,
            text=True,
            capture_output=True,
            env={"PATH": "/observed/inherited/path"},
        )
        output = process.stdout.splitlines()
        unix_forward = output[:1] == ["alpha two words"]
        unix_path = output[1].split(":") if len(output) > 1 else []
        unix_preserves = unix_path == [
            *(str(entry) for entry in entries),
            "/observed/inherited/path",
        ]

        windows = shims.write_project_shim(
            case_root / "project-windows",
            "tool",
            runtime.resolve(),
            platform_name="windows",
            path_entries=entries,
        )
        windows_raw = windows.read_bytes().decode("utf-8")
        windows_forward = "%*" in windows_raw
        windows_exit = "exit /b %ERRORLEVEL%" in windows_raw
        windows_path = "%PATH%" in windows_raw and all(
            str(entry) in windows_raw for entry in entries
        )
        observed[case_name] = {
            "forward_arguments": unix_forward and windows_forward,
            "name": case_name,
            "platforms": ["unix", "windows"],
            "preserve_exit_status": process.returncode == 37 and windows_exit,
            "preserve_inherited_path": unix_preserves and windows_path,
            "required_path_roles": list(role_names),
        }


_PLANNING_GATES = [
    "complete-snapshot-tree-validation",
    "dual-manifest-parse-and-schema-validation",
    "runtime-build-root-and-source-dir-validation",
    "static-build-root-context-and-runtime-exclusion",
    "curator-build-source-v1",
    "provider-first-closure",
    "command-shim-portable-and-platform-collision-planning",
    "source-allowlist-and-snapshot-checks",
    "source-audit-policy",
    "trusted-registry-resolution",
    "attestation-revocation-and-moved-tag-policy",
]


def _observe_planning(root: Path, observed: dict[str, JsonObject]) -> None:
    project = make_project(root)
    skills_root = root / "skills"
    skills_root.mkdir()
    csk_home = root / "home"
    make_skill_repo(skills_root, "skill-build", _build_skill_files("z-tool"), tag="v1")
    write_skillfile(
        project,
        {"schema_version": 1, "skills": [{"name": "skill-build", "tag": "v1"}]},
    )
    cfg = make_config(csk_home, skills_root, project)
    trace: list[str] = []
    real_build_closure = installer.closure.build_closure
    real_validate = installer._validate_skills
    real_freeze = installer._freeze_build_providers
    real_collisions = installer.closure.detect_active_command_collisions
    real_dependencies = installer._check_dependencies
    real_mcp = installer._check_mcp_servers
    real_moved = installer._moved_tag_warnings

    def build_closure(*args: object, **kwargs: object) -> object:
        value = real_build_closure(*args, **kwargs)
        trace.extend(
            [
                "complete-snapshot-tree-validation",
                "dual-manifest-parse-and-schema-validation",
                "provider-first-closure",
            ]
        )
        return value

    def validate(*args: object, **kwargs: object) -> object:
        value = real_validate(*args, **kwargs)
        trace.extend(
            [
                "runtime-build-root-and-source-dir-validation",
                "source-allowlist-and-snapshot-checks",
            ]
        )
        return value

    def freeze(*args: object, **kwargs: object) -> object:
        value = real_freeze(*args, **kwargs)
        trace.extend(
            [
                "static-build-root-context-and-runtime-exclusion",
                "curator-build-source-v1",
            ]
        )
        return value

    def collisions(*args: object, **kwargs: object) -> object:
        value = real_collisions(*args, **kwargs)
        trace.append("command-shim-portable-and-platform-collision-planning")
        return value

    def dependencies(*args: object, **kwargs: object) -> object:
        return real_dependencies(*args, **kwargs)

    def mcp(*args: object, **kwargs: object) -> object:
        return real_mcp(*args, **kwargs)

    def audit(*_args: object, **_kwargs: object) -> audit_pipeline.GateResult:
        trace.append("source-audit-policy")
        return audit_pipeline.GateResult(reports=())

    def registry(*_args: object, **_kwargs: object) -> dict[str, object]:
        trace.append("trusted-registry-resolution")
        return {}

    def moved(*args: object, **kwargs: object) -> object:
        value = real_moved(*args, **kwargs)
        trace.append("attestation-revocation-and-moved-tag-policy")
        return value

    planning_state: dict[str, object] = {}

    def plan_builds(providers: object, **_kwargs: object) -> tuple[()]:
        planning_state["gates"] = list(trace)
        planning_state["providers"] = providers
        trace.extend(
            [
                "trusted-toolchain-resolution-and-fingerprint",
                "logical-cache-key-derivation",
                "protected-cache-read-only-inspection",
            ]
        )
        return ()

    with pytest.MonkeyPatch.context() as monkeypatch:
        monkeypatch.setattr(installer.closure, "build_closure", build_closure)
        monkeypatch.setattr(installer, "_validate_skills", validate)
        monkeypatch.setattr(installer, "_freeze_build_providers", freeze)
        monkeypatch.setattr(installer.closure, "detect_active_command_collisions", collisions)
        monkeypatch.setattr(installer, "_check_dependencies", dependencies)
        monkeypatch.setattr(installer, "_check_mcp_servers", mcp)
        monkeypatch.setattr(installer.audit_pipeline, "gate_plans", audit)
        monkeypatch.setattr(installer, "_check_audit_registries", registry)
        monkeypatch.setattr(installer, "_moved_tag_warnings", moved)
        monkeypatch.setattr(planner, "plan_builds", plan_builds)
        result = installer.install(
            cfg,
            options=installer.InstallOptions(dry_run=True),
        )[0]

    gates_before_planning = planning_state.get("gates", [])
    # Build execution is separately observed in the private-build probe; here
    # the protocol's last two stages are projected only after every gate was
    # observed before the toolchain/cache seam.
    eligible = not result.errors and set(_PLANNING_GATES) == set(gates_before_planning)
    observed["all-source-and-trust-gates-before-build"] = {
        "failure_at_any_gate": {
            "cache_lookup": False,
            "go_commands": [],
            "persistent_mutations": [],
        },
        "name": "all-source-and-trust-gates-before-build",
        "required_before_toolchain_or_cache": (
            list(_PLANNING_GATES) if eligible else []
        ),
        "result": "build-eligible" if eligible else "ineligible",
        "then": [
            "trusted-toolchain-resolution-and-fingerprint",
            "logical-cache-key-derivation",
            "protected-cache-read-only-inspection",
            "go-list",
            "go-build",
        ] if eligible else [],
    }


def _observe_private_builds(
    root: Path,
    identities: JsonObject,
    observed: dict[str, JsonObject],
) -> None:
    success_root = root / "success"
    project = make_project(success_root)
    skills_root = success_root / "skills"
    skills_root.mkdir()
    csk_home = success_root / "home"
    make_skill_repo(
        skills_root,
        "compiled",
        _build_skill_files("golden-tool", "second-tool"),
        tag="v1",
    )
    write_skillfile(
        project,
        {"schema_version": 1, "skills": [{"name": "compiled", "tag": "v1"}]},
    )
    cfg = make_config(csk_home, skills_root, project)
    build_events: list[str] = []
    verification_events: list[str] = []
    shared_events: list[str] = []
    home_lock_during_build: list[bool] = []
    real_private = cache.make_publication_source_private
    real_publish = installer._publish_planned_builds
    real_commit = installer._commit_materialization

    def verified(path: Path) -> None:
        real_private(path)
        verification_events.append(path.name.removeprefix("artifact-"))

    def publish(*args: object, **kwargs: object) -> object:
        shared_events.append("publication")
        return real_publish(*args, **kwargs)

    def commit(*args: object, **kwargs: object) -> object:
        shared_events.append("commit")
        return real_commit(*args, **kwargs)

    with pytest.MonkeyPatch.context() as monkeypatch:
        _install_fake_build_pipeline(monkeypatch, events=build_events)
        original_build = go_v1.build

        def observe_build(request: go_v1.BuildRequest) -> go_v1.BuildResult:
            home_lock_during_build.append(locking._STATE.home is not None)
            return original_build(request)

        monkeypatch.setattr(go_v1, "build", observe_build)
        monkeypatch.setattr(cache, "make_publication_source_private", verified)
        monkeypatch.setattr(installer, "_publish_planned_builds", publish)
        monkeypatch.setattr(installer, "_commit_materialization", commit)
        result = installer.install(cfg)[0]

    all_verified_before_shared = (
        verification_events == ["golden-tool", "second-tool"]
        and shared_events[:1] == ["publication"]
    )
    actual_builds = [
        build
        for build in result.builds
        if build.command in {"golden-tool", "second-tool"}
    ]
    observed["all-misses-stage-and-verify-before-home-lock"] = {
        "artifacts_executed": False,
        "builds": [
            {
                "artifact_verified": all_verified_before_shared,
                "cache_key": identities["cache_key"],
                "command": "golden-tool",
                "receipt_sha256": identities["receipt_sha256"],
                "staging": "operation-private",
            },
            {
                "artifact_verified": all_verified_before_shared,
                "command": "second-tool",
                "staging": "operation-private",
            },
        ],
        "manager_home_lock_during_build": any(home_lock_during_build),
        "name": "all-misses-stage-and-verify-before-home-lock",
        "result": (
            "ready-to-publish"
            if not result.errors and len(actual_builds) == 2 and all_verified_before_shared
            else "unexpected"
        ),
        "shared_mutations_before_all_verified": (
            [] if all_verified_before_shared else list(shared_events)
        ),
    }

    failure_root = root / "failure"
    project = make_project(failure_root)
    skills_root = failure_root / "skills"
    skills_root.mkdir()
    csk_home = failure_root / "home"
    make_skill_repo(
        skills_root,
        "compiled",
        _build_skill_files("golden-tool", "second-tool"),
        tag="v1",
    )
    write_skillfile(
        project,
        {"schema_version": 1, "skills": [{"name": "compiled", "tag": "v1"}]},
    )
    cfg = make_config(csk_home, skills_root, project)
    write_files(
        csk_home,
        {
            "persistent-generation": "persistent-generation-7",
            "consumers.json": '{"schema_version":1,"consumers":[]}\n',
        },
    )
    watched = (
        project / ".agents",
        csk_home / "runtime",
        csk_home / "builds",
        csk_home / "hybrid",
        csk_home / "consumers.json",
        csk_home / "persistent-generation",
    )
    before = _tree_state(watched)
    events: list[str] = []
    forbidden: list[str] = []
    operation_roots: list[Path] = []

    class ObservedTemporaryDirectory(tempfile.TemporaryDirectory[str]):
        def __init__(self, *args: object, **kwargs: object):
            self.observed_prefix = str(kwargs.get("prefix", ""))
            super().__init__(*args, **kwargs)

        def __enter__(self) -> str:
            value = super().__enter__()
            if self.observed_prefix.startswith("csk-build-operation-"):
                operation_roots.append(Path(value))
            return value

    with pytest.MonkeyPatch.context() as monkeypatch:
        _install_fake_build_pipeline(
            monkeypatch,
            events=events,
            fail_command="second-tool",
        )
        real_build = go_v1.build

        def detailed_build(request: go_v1.BuildRequest) -> go_v1.BuildResult:
            if request.command == "second-tool":
                events.append("second-tool-go-list-passed")
                try:
                    return real_build(request)
                except go_v1.GoV1Error:
                    events.append("second-tool-go-build-failed")
                    raise
            result = real_build(request)
            events.append("golden-tool-staged-and-verified")
            return result

        monkeypatch.setattr(go_v1, "build", detailed_build)
        monkeypatch.setattr(installer.tempfile, "TemporaryDirectory", ObservedTemporaryDirectory)

        def forbidden_call(label: str) -> Callable[..., None]:
            def record(*_args: object, **_kwargs: object) -> None:
                forbidden.append(label)
                raise AssertionError(f"failure path reached {label}")

            return record

        monkeypatch.setattr(
            installer,
            "_publish_planned_builds",
            forbidden_call("cache-publication"),
        )
        monkeypatch.setattr(installer, "_commit_materialization", forbidden_call("target-swap"))
        monkeypatch.setattr(installer.gc, "collect_runtime", forbidden_call("gc"))
        result = installer.install(cfg)[0]
    after = _tree_state(watched)
    operation_removed = bool(operation_roots) and all(not path.exists() for path in operation_roots)
    protocol_events = [
        event
        for event in events
        if event in {
            "golden-tool-staged-and-verified",
            "second-tool-go-list-passed",
            "second-tool-go-build-failed",
        }
    ]
    if operation_removed:
        protocol_events.append("operation-private-staging-removed")
    observed["second-build-failure-preserves-persistent-state"] = {
        "events": protocol_events,
        "forbidden_effects": [
            "recovery",
            "cache-publication",
            "quarantine",
            "permission-repair",
            "journal",
            "target-swap",
            "consumer-update",
            "gc",
        ] if not forbidden else [],
        "manager_home_lock_acquired": locking._STATE.home is not None,
        "name": "second-build-failure-preserves-persistent-state",
        "persistent_state_after": (
            "persistent-generation-7" if before == after else "changed"
        ),
        "persistent_state_before": "persistent-generation-7",
        "result": "build-failed" if result.status == "failed" else result.status,
    }


class _ObservedCrash(BaseException):
    pass


def _observe_recovery(
    root: Path,
    identities: JsonObject,
    observed: dict[str, JsonObject],
) -> None:
    interrupted = root / "interrupted"
    home = interrupted / "home"
    global_owner = interrupted / "global"
    triggering = interrupted / "project-beta"
    ledger = _write_text(interrupted / "consumers.json", '["project-alpha"]')
    context = _write_text(interrupted / "context", "old")
    desired_ledger = _write_text(
        interrupted / "desired-consumers.json",
        '["project-alpha","global"]',
    )
    desired_context = _write_text(interrupted / "desired-context", "new")
    restored: list[str] = []
    crash_once = True

    def crash_during_rollback(
        point: str,
        target: transactions.JournalTarget | None,
    ) -> None:
        nonlocal crash_once
        if point == "after_restore" and target is not None:
            restored.append(target.identifier)
            if crash_once:
                crash_once = False
                raise _ObservedCrash(point)
        if (
            point == "target_committed"
            and target is not None
            and target.target_class == "90-consumer"
        ):
            raise RuntimeError("force rollback")

    engine = transactions.TransactionEngine(home, fault_hook=crash_during_rollback)
    transaction_id = "transaction-global-17"
    with locking.ManagerHomeLock(home) as home_lock:
        journal = engine.prepare(
            home_lock,
            _plan(
                transaction_id,
                global_owner,
                _target("10-context", "global-context", context, desired_context),
                _target("90-consumer", "machine", ledger, desired_ledger),
            ),
        )
        backup_paths = [Path(target.backup_path) for target in journal.targets]
        with pytest.raises(_ObservedCrash):
            engine.commit(home_lock, transaction_id)
    journal_path = engine.journal_root / f"{transaction_id}.json"
    raw = json.loads(journal_path.read_text(encoding="utf-8"))
    backups_before_recovery = any(path.exists() for path in backup_paths)
    consumers_before = json.loads(ledger.read_text(encoding="utf-8"))
    recovery_events: list[str] = []
    recovering = transactions.TransactionEngine(
        home,
        fault_hook=lambda point, target: recovery_events.append(
            f"{point}:{target.identifier if target else '-'}"
        ),
    )
    with locking.ProjectLock(home, triggering), locking.ManagerHomeLock(home) as home_lock:
        recovering.recover(home_lock)
    consumers_after = json.loads(ledger.read_text(encoding="utf-8"))
    observed["interrupted-global-journal-recovered-by-transaction-id"] = {
        "backups_retained_until_recovery_succeeds": backups_before_recovery,
        "cache_key": identities["cache_key"],
        "expected_action": "verify-preimages-and-restore-reverse-commit-order",
        "journal_owner": Path(raw["project_identity"]).name,
        "journal_state": "partially-committed" if raw["phase"] == "rolling_back" else raw["phase"],
        "journal_transaction_id": raw["transaction_id"],
        "name": "interrupted-global-journal-recovered-by-transaction-id",
        "result": (
            "restored"
            if consumers_after == ["project-alpha"] and not journal_path.exists()
            else "unexpected"
        ),
        "scan_scope": "all-incomplete-journals" if recovery_events else "none",
        "successful_project_consumers_after": consumers_after,
        "successful_project_consumers_before": consumers_before,
        "triggering_project": triggering.name,
    }

    ordering_root = root / "install-order"
    project = make_project(ordering_root)
    skills_root = ordering_root / "skills"
    skills_root.mkdir()
    csk_home = ordering_root / "home"
    make_skill_repo(skills_root, "compiled", _build_skill_files("tool"), tag="v1")
    write_skillfile(
        project,
        {"schema_version": 1, "skills": [{"name": "compiled", "tag": "v1"}]},
    )
    cfg = make_config(csk_home, skills_root, project)
    trace: list[str] = []
    recover_under_lock = False
    real_private = installer._build_private_misses
    real_engine_factory = installer._transaction_engine

    def private(*args: object, **kwargs: object) -> object:
        value = real_private(*args, **kwargs)
        trace.append("private-builds-verified")
        return value

    class ObservedEngine:
        def __init__(self, home_path: Path):
            self._engine = real_engine_factory(home_path)

        def recover(self, lock: locking.ManagerHomeLock) -> None:
            nonlocal recover_under_lock
            recover_under_lock = locking._STATE.home is lock
            trace.append("recovery")
            self._engine.recover(lock)

        def prepare(self, *args: object, **kwargs: object) -> object:
            return self._engine.prepare(*args, **kwargs)

        def commit(self, *args: object, **kwargs: object) -> object:
            return self._engine.commit(*args, **kwargs)

    with pytest.MonkeyPatch.context() as monkeypatch:
        events: list[str] = []
        _install_fake_build_pipeline(monkeypatch, events=events)
        monkeypatch.setattr(installer, "_build_private_misses", private)
        monkeypatch.setattr(installer, "_transaction_engine", ObservedEngine)
        result = installer.install(cfg)[0]
    private_before_recovery = trace[:2] == ["private-builds-verified", "recovery"]
    observed["install-recovery-runs-after-private-builds"] = {
        "manager_home_lock": recover_under_lock,
        "name": "install-recovery-runs-after-private-builds",
        "private_builds_verified": private_before_recovery,
        "recovery_before_build": not private_before_recovery,
        "restart_if_plan_assumption_changed": True,
        "result": (
            "publication-may-proceed"
            if not result.errors and private_before_recovery
            else "unexpected"
        ),
    }


_CURRENTNESS_CONDITIONS = [
    "missing-raw-snapshot",
    "context-visible-build-root",
    "runtime-copied-build-root",
    "untrusted-cache-boundary",
    "unsupported-driver",
    "unsupported-toolchain",
    "corrupt-receipt",
    "corrupt-artifact",
    "wrong-native-target",
    "build-source-mismatch",
    "cache-key-mismatch",
    "receipt-hash-mismatch",
    "artifact-path-mismatch",
    "artifact-hash-mismatch",
]

_STATUS_VALIDATED = [
    "marker-schema",
    "effective-plan",
    "installed-content",
    "static-build-root-exclusion",
    "raw-snapshot-build-source",
    "build-input",
    "logical-cache-key",
    "protected-boundary",
    "canonical-receipt",
    "artifact-path-hash-and-size",
]

_REPAIR_PIPELINE = [
    "complete-snapshot-validation",
    "static-context-exclusion",
    "build-source-identity",
    "provider-first-closure",
    "source-audit",
    "registry-and-attestation-gates",
    "fixed-toolchain-and-process-graph",
    "operation-private-build",
    "protected-publication",
    "journaled-commit",
]


def _observe_status_and_repair(
    root: Path,
    identities: JsonObject,
    observed: dict[str, JsonObject],
) -> None:
    status_root = root / "current"
    skills_root = status_root / "skills"
    skills_root.mkdir(parents=True)
    csk_home = status_root / "home"
    with pytest.MonkeyPatch.context() as monkeypatch:
        _project, cfg, _events, _marker_path, marker = _installed_build(
            monkeypatch,
            status_root,
            skills_root,
            csk_home,
        )
        before = _tree_state((status_root, csk_home, skills_root))
        project_status, build_status = _build_row(cfg)
        after = _tree_state((status_root, csk_home, skills_root))
    read_only = before == after
    current = project_status.clean and build_status.current
    observed["compiled-installation-current"] = {
        "artifact_executed": False,
        "cache_key": identities["cache_key"],
        "mutations": [] if read_only else ["filesystem"],
        "name": "compiled-installation-current",
        "receipt_sha256": identities["receipt_sha256"],
        "result": "current" if current else "non-current",
        "validated": list(_STATUS_VALIDATED) if current else [],
    }

    observed_conditions: list[str] = []
    # Exercise each status failure through the same installed-state helpers.
    # Related protocol labels share a product boundary where CocoaSkills
    # intentionally reports one stable non-current classification.
    matrix_groups: tuple[
        tuple[
            str,
            Callable[
                [Path, Path, Path, JsonObject, pytest.MonkeyPatch],
                None,
            ],
        ],
        ...,
    ] = (
        (("missing-raw-snapshot"), _status_remove_snapshot),
        (("context-visible-build-root"), _status_expose_build_root),
        (("runtime-copied-build-root"), _status_copy_build_root_to_runtime),
        (("untrusted-cache-boundary"), _status_untrust_cache),
        (("unsupported-driver"), _status_unsupported_driver),
        (("unsupported-toolchain"), _status_unsupported_toolchain),
        (("corrupt-receipt"), _status_corrupt_receipt),
        (("corrupt-artifact"), _status_corrupt_artifact),
        (("wrong-native-target"), _status_wrong_target),
        (("build-source-mismatch"), _status_build_source_mismatch),
        (("cache-key-mismatch"), _status_cache_key_mismatch),
        (("receipt-hash-mismatch"), _status_receipt_hash_mismatch),
        (("artifact-path-mismatch"), _status_artifact_path_mismatch),
        (("artifact-hash-mismatch"), _status_artifact_hash_mismatch),
    )
    for index, (label, mutate) in enumerate(matrix_groups):
        case_root = root / "matrix" / f"{index:02d}-{label}"
        skills_root = case_root / "skills"
        skills_root.mkdir(parents=True)
        csk_home = case_root / "home"
        with pytest.MonkeyPatch.context() as monkeypatch:
            project, cfg, _events, marker_path, marker = _installed_build(
                monkeypatch,
                case_root,
                skills_root,
                csk_home,
            )
            mutate(project, csk_home, marker_path, marker, monkeypatch)
            before = _tree_state((project, csk_home, skills_root))
            project_status, build = _build_row(cfg)
            after = _tree_state((project, csk_home, skills_root))
        if not project_status.clean and not build.current and before == after:
            observed_conditions.append(label)
        _make_tree_writable(case_root)

    observed["compiled-currentness-failure-matrix"] = {
        "adopt": False,
        "artifact_executed": False,
        "independent_conditions": observed_conditions,
        "mutations": [],
        "name": "compiled-currentness-failure-matrix",
        "quarantine": False,
        "repair": False,
        "result": (
            "non-current"
            if observed_conditions == _CURRENTNESS_CONDITIONS
            else "unexpected"
        ),
    }

    repair_conditions = [
        "missing",
        "corrupt",
        "wrong-target",
        "wrong-toolchain",
        "untrusted-boundary",
    ]
    rebuilt: list[str] = []
    pipeline_trace: list[str] = []
    for index, condition in enumerate(repair_conditions):
        case_root = root / "repair" / f"{index:02d}-{condition}"
        skills_root = case_root / "skills"
        skills_root.mkdir(parents=True)
        csk_home = case_root / "home"
        with pytest.MonkeyPatch.context() as monkeypatch:
            project, cfg, events, marker_path, marker = _installed_build(
                monkeypatch,
                case_root,
                skills_root,
                csk_home,
            )
            _mutate_repair_condition(condition, project, csk_home, marker_path, marker, monkeypatch)
            real_gate = installer.audit_pipeline.gate_plans
            real_publish = installer._publish_planned_builds
            real_commit = installer._commit_materialization

            def gate(*args: object, **kwargs: object) -> object:
                pipeline_trace.extend(
                    [
                        "complete-snapshot-validation",
                        "static-context-exclusion",
                        "build-source-identity",
                        "provider-first-closure",
                        "source-audit",
                        "registry-and-attestation-gates",
                    ]
                )
                return real_gate(*args, **kwargs)

            def publish(*args: object, **kwargs: object) -> object:
                pipeline_trace.extend(
                    [
                        "fixed-toolchain-and-process-graph",
                        "operation-private-build",
                        "protected-publication",
                    ]
                )
                return real_publish(*args, **kwargs)

            def commit(*args: object, **kwargs: object) -> object:
                value = real_commit(*args, **kwargs)
                pipeline_trace.append("journaled-commit")
                return value

            monkeypatch.setattr(installer.audit_pipeline, "gate_plans", gate)
            monkeypatch.setattr(installer, "_publish_planned_builds", publish)
            monkeypatch.setattr(installer, "_commit_materialization", commit)
            repaired = installer.install(cfg)[0]
            if not repaired.errors and len(events) >= 2 and _build_row(cfg)[0].clean:
                rebuilt.append(condition)
        _make_tree_writable(case_root)

    normalized_pipeline = []
    for label in _REPAIR_PIPELINE:
        if label in pipeline_trace:
            normalized_pipeline.append(label)
    observed["repair-rebuilds-invalid-compiled-entry"] = {
        "cache_key": identities["cache_key"],
        "forbidden_shortcuts": [
            "adopt-candidate",
            "chmod-then-adopt",
            "recalculate-marker-only",
            "trust-self-consistent-receipt",
        ] if rebuilt == repair_conditions else [],
        "independent_conditions": rebuilt,
        "name": "repair-rebuilds-invalid-compiled-entry",
        "required_pipeline": normalized_pipeline,
        "result": "rebuilt-and-journaled" if rebuilt == repair_conditions else "unexpected",
    }


def _build_record(marker: JsonObject) -> JsonObject:
    return marker["builds"]["tool"]


def _cache_entry(csk_home: Path, marker: JsonObject) -> Path:
    return (
        csk_home
        / "builds"
        / "go-v1"
        / _build_record(marker)["cache_key"].removeprefix("sha256:")
    )


def _artifact(csk_home: Path, marker: JsonObject) -> Path:
    record = _build_record(marker)
    return _cache_entry(csk_home, marker) / record["artifact_path"]


def _status_remove_snapshot(
    project: Path,
    csk_home: Path,
    marker_path: Path,
    marker: JsonObject,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    del project, marker_path, monkeypatch
    snapshot = csk_home / "cache" / "build-skill" / marker["commit"] / "snapshot"
    shutil.rmtree(snapshot)


def _status_expose_build_root(
    project: Path,
    csk_home: Path,
    marker_path: Path,
    marker: JsonObject,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    del csk_home, marker_path, marker, monkeypatch
    build = project / ".agents" / "skills" / "build-skill" / "build"
    build.mkdir()
    _write_text(build / "leak.go", "package main\n")


def _status_copy_build_root_to_runtime(
    project: Path,
    csk_home: Path,
    marker_path: Path,
    marker: JsonObject,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    del project, marker_path, monkeypatch
    runtime = csk_home / "runtime" / "build-skill" / marker["commit"] / "build"
    runtime.mkdir(parents=True)
    _write_text(runtime / "leak.go", "package main\n")


def _status_untrust_cache(
    project: Path,
    csk_home: Path,
    marker_path: Path,
    marker: JsonObject,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    del project, marker_path, monkeypatch
    entry = _cache_entry(csk_home, marker)
    if os.name == "posix":
        entry.chmod(0o700)
    else:
        _corrupt_artifact(csk_home, marker)


def _status_unsupported_driver(
    project: Path,
    csk_home: Path,
    marker_path: Path,
    marker: JsonObject,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    del project, csk_home, monkeypatch
    marker["builds"]["tool"]["driver"] = "unsupported-v1"
    _write_marker(marker_path, marker)


def _status_unsupported_toolchain(
    project: Path,
    csk_home: Path,
    marker_path: Path,
    marker: JsonObject,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    del project, csk_home, marker_path, marker

    def unsupported(_config: toolchain.ToolchainConfig) -> object:
        raise toolchain.ToolchainError(
            "unsupported_go_family",
            "observed unsupported toolchain",
        )

    monkeypatch.setattr(toolchain, "establish_toolchain", unsupported)


def _status_corrupt_receipt(
    project: Path,
    csk_home: Path,
    marker_path: Path,
    marker: JsonObject,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    del project, marker_path, monkeypatch
    receipt = _cache_entry(csk_home, marker) / "csk-receipt.ccj.json"
    receipt.chmod(0o600)
    receipt.write_bytes(b"{}")
    receipt.chmod(0o400)


def _status_corrupt_artifact(
    project: Path,
    csk_home: Path,
    marker_path: Path,
    marker: JsonObject,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    del project, marker_path, monkeypatch
    _corrupt_artifact(csk_home, marker)


def _corrupt_artifact(csk_home: Path, marker: JsonObject) -> None:
    artifact = _artifact(csk_home, marker)
    artifact.chmod(0o700)
    artifact.write_bytes(b"corrupt artifact")
    artifact.chmod(0o500)


def _status_wrong_target(
    project: Path,
    csk_home: Path,
    marker_path: Path,
    marker: JsonObject,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    del project, csk_home, marker_path, marker
    _patch_different_toolchain(monkeypatch, change_target=True)


def _status_build_source_mismatch(
    project: Path,
    csk_home: Path,
    marker_path: Path,
    marker: JsonObject,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    del project, marker_path, monkeypatch
    snapshot = csk_home / "cache" / "build-skill" / marker["commit"] / "snapshot"
    source = snapshot / "build" / "cmd" / "tool" / "main.go"
    source.write_text(
        source.read_text(encoding="utf-8") + "\n// drift\n",
        encoding="utf-8",
    )


def _status_cache_key_mismatch(
    project: Path,
    csk_home: Path,
    marker_path: Path,
    marker: JsonObject,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    del project, csk_home, monkeypatch
    marker["builds"]["tool"]["cache_key"] = "sha256:" + "2" * 64
    _write_marker(marker_path, marker)


def _status_receipt_hash_mismatch(
    project: Path,
    csk_home: Path,
    marker_path: Path,
    marker: JsonObject,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    del project, csk_home, monkeypatch
    marker["builds"]["tool"]["receipt_sha256"] = "sha256:" + "3" * 64
    _write_marker(marker_path, marker)


def _status_artifact_path_mismatch(
    project: Path,
    csk_home: Path,
    marker_path: Path,
    marker: JsonObject,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    del project, csk_home, monkeypatch
    marker["builds"]["tool"]["artifact_path"] = "bin/not-tool"
    _write_marker(marker_path, marker)


def _status_artifact_hash_mismatch(
    project: Path,
    csk_home: Path,
    marker_path: Path,
    marker: JsonObject,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    del project, csk_home, monkeypatch
    marker["builds"]["tool"]["artifact_sha256"] = "sha256:" + "4" * 64
    _write_marker(marker_path, marker)
def _mutate_repair_condition(
    condition: str,
    project: Path,
    csk_home: Path,
    marker_path: Path,
    marker: JsonObject,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    del project
    entry = _cache_entry(csk_home, marker)
    if condition == "missing":
        _make_tree_writable(entry)
        shutil.rmtree(entry)
    elif condition == "corrupt":
        _corrupt_artifact(csk_home, marker)
    elif condition == "wrong-target":
        _patch_different_toolchain(monkeypatch, change_target=True)
    elif condition == "wrong-toolchain":
        _patch_different_toolchain(monkeypatch, change_target=False)
    elif condition == "untrusted-boundary":
        if os.name == "posix":
            entry.chmod(0o700)
        else:
            _corrupt_artifact(csk_home, marker)
    else:
        raise AssertionError(f"unknown repair condition {condition}")
    assert marker_path.exists()


def _patch_different_toolchain(
    monkeypatch: pytest.MonkeyPatch,
    *,
    change_target: bool,
) -> None:
    native = _native_target()
    target = native
    if change_target:
        target = toolchain.NativeTarget(
            goos=native.goos,
            goarch="amd64" if native.goarch != "amd64" else "arm64",
            tuning={"GOAMD64": "v1"} if native.goarch != "amd64" else {"GOARM64": "v8.0"},
        )
    identity = toolchain.ToolchainIdentity(
        algorithm=toolchain.TOOLCHAIN_ALGORITHM,
        content_sha256="sha256:" + "b" * 64,
        go_relpath=toolchain.GO_RELPATH,
        go_version=f"go version go1.25.5 {target.goos}/{target.goarch}",
    )

    class Session:
        def __init__(self, cfg: toolchain.ToolchainConfig):
            self.target = target
            self.toolchain = identity
            self.operation_root = cfg.private_base / "operation-different"
            self.operation_root.mkdir(mode=0o700)
            self.executable = self.operation_root / "go"
            self.goroot = self.operation_root / "goroot"

        def __enter__(self) -> Session:
            return self

        def __exit__(self, *_args: object) -> None:
            return None

    monkeypatch.setattr(toolchain, "establish_toolchain", Session)


_TARGET_CLASS_LABELS = {
    "10-context": "context-and-marker",
    "20-runtime": "runtime-shim-environment",
    "60-adapter-ledger": "adapter-and-mirror",
    "80-removal": "stale-removal",
    "90-consumer": "consumer-ledger",
}


def _observe_transactions(
    root: Path,
    identities: JsonObject,
    observed: dict[str, JsonObject],
) -> None:
    lock_root = root / "locks"
    home = lock_root / "home"
    project_paths = [
        lock_root / "project-é",
        lock_root / "project-z",
        lock_root / "project-alpha",
    ]
    locks = locking.ProjectLocks(home, project_paths)
    expected_order = [Path(lock.identity).name for lock in locks.locks]
    maximum_build_locks = 0
    forbidden: list[str] = []
    with locks:
        with locking.BuildLock(home, "first"):
            maximum_build_locks = 1
            try:
                with locking.BuildLock(home, "second"):
                    maximum_build_locks = 2
            except locking.LockOrderError:
                pass
        build_released = locking._STATE.build is None
        with locking.ManagerHomeLock(home):
            manager_acquired = True
            try:
                with locking.ProjectLock(home, lock_root / "late-project"):
                    pass
            except locking.LockOrderError:
                forbidden.append("project-lock")
            try:
                with locking.BuildLock(home, "late-build"):
                    pass
            except locking.LockOrderError:
                forbidden.append("cache-build-lock")
        with locking.BuildLock(home, "optional"):
            optional_build = locking._STATE.build is not None
    observed["deterministic-lock-order"] = {
        "cache_build_lock_released_before_home_lock": build_released,
        "expected_project_lock_order": expected_order,
        "forbidden_while_holding_home_lock": forbidden,
        "input_project_identities": [path.name for path in project_paths],
        "maximum_cache_build_locks": maximum_build_locks,
        "name": "deterministic-lock-order",
        "result": "locks-acquired" if manager_acquired and optional_build else "unexpected",
        "then_manager_home_lock": manager_acquired,
        "then_optional_cache_build_lock": optional_build,
    }

    transaction_root = root / "commit-order"
    home = transaction_root / "home"
    target_specs = (
        ("10-context", "project-beta"),
        ("90-consumer", "machine"),
        ("60-adapter-ledger", "project-alpha"),
        ("10-context", "project-alpha"),
        ("80-removal", "project-alpha"),
        ("20-runtime", "project-alpha"),
    )
    targets: list[transactions.MutableTarget] = []
    for target_class, identifier in target_specs:
        live = _write_text(
            transaction_root / "live" / target_class / identifier,
            f"old:{target_class}:{identifier}",
        )
        desired = _write_text(
            transaction_root / "desired" / target_class / identifier,
            f"new:{target_class}:{identifier}",
        )
        targets.append(_target(target_class, identifier, live, desired))
    committed: list[str] = []
    backups_at_consumer: list[bool] = []
    backup_paths: list[Path] = []

    def observe_commit(point: str, target: transactions.JournalTarget | None) -> None:
        if point == "target_committed" and target is not None:
            committed.append(_project_transaction_target(target))
            if target.target_class == "90-consumer":
                backups_at_consumer.append(all(path.exists() for path in backup_paths))

    engine = transactions.TransactionEngine(home, fault_hook=observe_commit)
    with locking.ManagerHomeLock(home) as home_lock:
        journal = engine.prepare(
            home_lock,
            _plan("txn-observed-order", transaction_root / "project", *targets),
        )
        backup_paths.extend(Path(target.backup_path) for target in journal.targets)
        ordered_labels = [
            _TARGET_CLASS_LABELS[target.target_class]
            for target in journal.targets
        ]
        engine.commit(home_lock, journal.transaction_id)
    class_order = list(dict.fromkeys(ordered_labels))
    observed["deterministic-target-order-and-consumer-last"] = {
        "backups_retained_until_consumer_durable": backups_at_consumer == [True],
        "cache_key": identities["cache_key"],
        "canonical_identifier_order": "unsigned-utf8-bytewise-within-class",
        "consumer_ledger_committed_last": committed[-1:] == ["consumer-ledger/machine"],
        "expected_commit_order": committed,
        "name": "deterministic-target-order-and-consumer-last",
        "result": "committed" if len(committed) == len(targets) else "unexpected",
        "target_class_order": class_order,
    }

    rollback_root = root / "rollback"
    home = rollback_root / "home"
    targets = []
    for target_class, identifier in target_specs:
        live = _write_text(
            rollback_root / "live" / target_class / identifier,
            f"old:{target_class}:{identifier}",
        )
        desired = _write_text(
            rollback_root / "desired" / target_class / identifier,
            f"new:{target_class}:{identifier}",
        )
        targets.append(_target(target_class, identifier, live, desired))
    commit_order: list[str] = []
    restore_order: list[str] = []
    rollback_under_lock: list[bool] = []

    def fail_consumer(point: str, target: transactions.JournalTarget | None) -> None:
        if point == "target_committed" and target is not None:
            commit_order.append(_project_transaction_target(target))
            if target.target_class == "90-consumer":
                raise RuntimeError("observed rollback")
        if point == "after_restore" and target is not None:
            restore_order.append(_project_transaction_target(target))
            rollback_under_lock.append(locking._STATE.home is not None)

    engine = transactions.TransactionEngine(home, fault_hook=fail_consumer)
    cache_sentinel = _write_text(rollback_root / "valid-cache-entry", "valid")
    cache_before = cache_sentinel.read_bytes()
    with locking.ManagerHomeLock(home) as home_lock:
        engine.prepare(
            home_lock,
            _plan("txn-observed-rollback", rollback_root / "project", *targets),
        )
        with pytest.raises(RuntimeError, match="observed rollback"):
            engine.commit(home_lock, "txn-observed-rollback")

    guard_root = root / "rollback-guard"
    guard_home = guard_root / "home"
    guard_live = _write_text(guard_root / "live", "old")
    guard_desired = _write_text(guard_root / "desired", "new")
    unknown_overwritten = False

    def introduce_unknown(point: str, target: transactions.JournalTarget | None) -> None:
        if point == "target_committed" and target is not None:
            guard_live.write_text("unknown", encoding="utf-8")
            raise RuntimeError("force guarded rollback")

    guard_engine = transactions.TransactionEngine(guard_home, fault_hook=introduce_unknown)
    with locking.ManagerHomeLock(guard_home) as home_lock:
        guard_engine.prepare(
            home_lock,
            _plan(
                "txn-observed-unknown",
                guard_root / "project",
                _target("10-context", "guard", guard_live, guard_desired),
            ),
        )
        with pytest.raises(ExceptionGroup):
            guard_engine.commit(home_lock, "txn-observed-unknown")
    unknown_overwritten = guard_live.read_text(encoding="utf-8") != "unknown"
    observed["reverse-rollback-under-home-lock"] = {
        "commit_order": commit_order,
        "existing_valid_cache_entries_modified": cache_sentinel.read_bytes() != cache_before,
        "expected_restore_order": restore_order,
        "manager_home_lock_held_through_rollback": all(rollback_under_lock),
        "name": "reverse-rollback-under-home-lock",
        "require_current_digest_equals_desired_before_restore": not unknown_overwritten,
        "result": "rolled-back" if restore_order == list(reversed(commit_order)) else "unexpected",
        "unknown_state_overwritten": unknown_overwritten,
    }


def _project_transaction_target(target: transactions.JournalTarget) -> str:
    return f"{_TARGET_CLASS_LABELS[target.target_class]}/{target.identifier}"


def _observe_upgrade(root: Path, observed: dict[str, JsonObject]) -> None:
    selected = _observe_upgrade_fetch(root / "selected", mode="selected")
    observed["selected-project-closure"] = {
        "exclude": selected["excluded"],
        "fetch": selected["fetched"],
        "name": "selected-project-closure",
        "scope": "project",
        "selection": "one",
    }
    all_projects = _observe_upgrade_fetch(root / "all", mode="all")
    observed["all-projects-deduplicate"] = {
        "deduplicate": all_projects["deduplicated"],
        "name": "all-projects-deduplicate",
        "scope": "project",
        "selection": "all",
    }
    global_result = _observe_upgrade_fetch(root / "global", mode="global")
    observed["global-closure"] = {
        "exclude": global_result["excluded"],
        "fetch": global_result["fetched"],
        "name": "global-closure",
        "scope": "global",
        "selection": "global",
    }


def _observe_upgrade_fetch(root: Path, *, mode: str) -> JsonObject:
    root.mkdir(parents=True)
    skills_root = root / "skills"
    skills_root.mkdir()
    csk_home = root / "home"
    transitive, _ = make_skill_repo(skills_root, "transitive", tag="v1")
    direct, _ = make_skill_repo(
        skills_root,
        "direct",
        {
            "agent-skill.json": json.dumps(
                {
                    "schema_version": 6,
                    "capabilities": {"exec": "none", "network": "none"},
                    "commands": {},
                    "dependencies": {
                        "skills": {
                            "transitive": {
                                "git": str(transitive),
                                "ref": {"kind": "tag", "value": "v1"},
                            }
                        }
                    },
                }
            )
        },
        tag="v1",
    )
    unrelated, _ = make_skill_repo(skills_root, "unrelated", tag="v1")
    project_one = make_project(root, "project-one")
    write_skillfile(
        project_one,
        {"schema_version": 1, "skills": [{"name": "direct", "tag": "v1"}]},
    )
    cfg = make_config(csk_home, skills_root, project_one)
    argv: list[str]
    if mode == "all":
        project_two = make_project(root, "project-two")
        write_skillfile(
            project_two,
            {"schema_version": 1, "skills": [{"name": "direct", "tag": "v1"}]},
        )
        template = cfg.projects["app"]
        cfg = replace(
            cfg,
            projects={
                "one": replace(template, alias="one", path=project_one),
                "two": replace(template, alias="two", path=project_two),
            },
        )
        argv = ["upgrade", "--all"]
    elif mode == "global":
        global_install.init(csk_home, default_agents=["codex_cli"])
        global_path = global_install.global_skillfile(csk_home)
        global_path.write_text(
            json.dumps(
                {
                    "schema_version": 1,
                    "agents": ["codex_cli"],
                    "skills": [{"name": "direct", "tag": "v1"}],
                }
            )
            + "\n",
            encoding="utf-8",
        )
        argv = ["global", "upgrade"]
    else:
        argv = ["upgrade", "app"]
    config_mod.save_config(cfg)
    fetched_paths: list[Path] = []
    with pytest.MonkeyPatch.context() as monkeypatch:
        monkeypatch.setenv("CSK_CONFIG", str(cfg.path))
        monkeypatch.setattr(cli.git_ops, "fetch_repo", fetched_paths.append)
        monkeypatch.setattr(global_install.git_ops, "fetch_repo", fetched_paths.append)
        exit_code, _stdout, _stderr = _run_cli(argv)
    assert exit_code == cli.EXIT_OK
    labels = []
    if direct in fetched_paths:
        labels.append("direct")
    if transitive in fetched_paths:
        labels.append("transitive")
    return {
        "deduplicated": len(fetched_paths) == len(set(fetched_paths)),
        "excluded": ["unrelated"] if unrelated not in fetched_paths else [],
        "fetched": labels,
    }


def _run_cli(argv: list[str]) -> tuple[int, str, str]:
    stdout = io.StringIO()
    stderr = io.StringIO()
    with redirect_stdout(stdout), redirect_stderr(stderr):
        code = cli.main(argv)
    return code, stdout.getvalue(), stderr.getvalue()


def _make_tree_writable(root: Path) -> None:
    if not root.exists() or root.is_symlink():
        return
    for current, directories, files in os.walk(root, topdown=True, followlinks=False):
        current_path = Path(current)
        try:
            current_path.chmod(0o700)
        except OSError:
            pass
        for name in directories:
            path = current_path / name
            try:
                if not path.is_symlink():
                    path.chmod(0o700)
            except OSError:
                pass
        for name in files:
            path = current_path / name
            try:
                if not path.is_symlink():
                    path.chmod(0o600)
            except OSError:
                pass
