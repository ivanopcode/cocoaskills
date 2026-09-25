"""Executable skillfile-sources-v1 conformance harness (accepted corpus, opt-in).

This module stands up the conformance consumer for the accepted
skillfile-sources-v1 corpus, so every later
leaf adds real semantic-case drivers to a harness that already reports
honestly. It reads the suite from ``CSK_DRAFT_SOURCES_SUITE_ROOT`` (a checkout
of ``relux-works/curator-spec`` at the revision pinned in
``.github/ci/draft-sources-suite.json``, pointing at
``conformance/skillfile-sources-v1``) and:

* authenticates ``index.json``, ``semantic-cases.json`` and
  ``snapshot-cases.json`` against the committed pin digests, failing closed on
  a mismatch or a missing file;
* validates every indexed schema case with ``jsonschema`` Draft 2020-12 over a
  ``referencing`` registry built from ``schemas/v1`` and
  ``schemas/skillfile-sources-v1``, asserting each expected ``valid`` flag;
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
outcome, never merely reproduce the expected label. The semantic dispatch
enforces that contract: every driver run is wrapped in a production-entry
observer, and a driver that returns without touching any entry fails as
hollow, naming its case and owning task. The last test in this file reports
passed/skipped/failed/total per category (schema, snapshot, semantic,
harness) into the junit artifact.

The observer's guarantee is exactly "at least one tabled in-process
entry was called", and three bounds come with it: it sees in-process
calls only (a driver that shells out to the real CLI counts as
hollow); it is table-defined (real but untabled production code
counts as hollow); it is provenance-blind (one tabled call of any
kind, on any argument, satisfies it, so a tribute call followed by a
label assertion passes). The table's membership is itself observed,
not inferred: every entry must have executed during the traced CLI
scenarios (see ``_PRODUCTION_ENTRY_POINTS``), so a dead entry,
however many test-only callers, forwarders, uncalled nested bodies
or references-as-data point at it, fails a test instead of vouching
for drivers. An uncalled function is never observed, whatever the
source around it says.

Where the product itself refuses a scenario, the gate certifies what
runs instead of failing the run: on a platform without external-build
support (``csk.installer.supports_external_builds`` is false) the
external-build scenario asserts the product's structured refusal and
the membership check covers the entries the runnable scenarios
reach. The entries the refused scenario alone covers are excluded
from the check, and the excluded set is derived, never enumerated:
``tests/draft_sources_observed_labels.json`` records the
machine-observed scenario labels per tabled entry, and an entry is
excluded only when every recorded label names a refused scenario. The
record is self-checking on capable hosts (the live labels must equal
it exactly, entry sets and label sets both) and a tabled entry with
no record fails on every lane, so the derivation cannot silently
swallow a new entry; an empty label set is refused at write and at
load, since an empty set is a subset of every lane. The pin
obligation is derived from the real platform, never from the live
predicate, so no ambient variable can remove it. Regenerate the
record on a capable host with ``CSK_REGENERATE_OBSERVED_LABELS=1``;
simulate the refused lane anywhere with
``CSK_SIMULATE_NO_EXTERNAL_BUILDS=1``, which adds that lane's run
beside the native one — forcing the same predicate the product
calls, scoped to the added run — while the capable-lane pin still
runs on a capable host.

The draft lanes run on ubuntu-latest and macos-latest only. windows-latest
is a declared unsupported lane for the draft suite: draft schema-2 source
selection is POSIX-only (descriptor-relative traversal), so the traversal
drivers skip there by platform bound rather than running.
"""

from __future__ import annotations

import contextlib
import functools
import hashlib
import importlib
import io
import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import tomllib
from collections.abc import Callable, Collection, Iterator, Mapping
from copy import deepcopy
from pathlib import Path
from types import CodeType
from typing import Any
from unittest.mock import patch

import pytest
from jsonschema import Draft202012Validator
from referencing import Registry, Resource

from csk import git_admission, installer, manifest, protocol_json
from csk.build_repository_pipeline import ExternalBuildError
from csk.sources import _selection_fs
from csk.sources import errors as source_errors
from csk.sources import repository_policy
from csk.sources import transport as source_transport
from csk.sources import lock as source_lock
from draft_sources_accounting import OUTCOMES as _HOOK_OUTCOMES
from draft_sources_accounting import REPORT_CATEGORIES as _REPORT_CATEGORIES
from draft_sources_accounting import check_accounting as _check_outcome_accounting

ROOT_TEXT = os.environ.get("CSK_DRAFT_SOURCES_SUITE_ROOT")
pytestmark = pytest.mark.skipif(not ROOT_TEXT, reason="CSK_DRAFT_SOURCES_SUITE_ROOT is not set")

PIN_PATH = Path(__file__).parents[1] / ".github" / "ci" / "draft-sources-suite.json"
EXPECTED_SUITE_FILES = ("index.json", "semantic-cases.json", "snapshot-cases.json")
DRAFT_2020_12_SCHEMA_ID = "https://json-schema.org/draft/2020-12/schema"

# The accepted corpus at the pinned revision carries 121 schema cases. The
# operative requirement is "every index.json schema case", so the harness
# asserts the measured inventory.
EXPECTED_SCHEMA_CASE_COUNT = 121
EXPECTED_SCHEMA_CASE_COUNTS = {
    "build-receipt-v3.schema.json": 4,
    "install-marker-v5.schema.json": 44,
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
EXPECTED_SEMANTIC_CASE_COUNT = 105
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
    # TASK-260925-38kn57 pins and drives the newly accepted corpus vectors.
    "attestation-evidence-revoked-identity-commit-advisory": "TASK-260925-38kn57",
    "git-missing-snapshot-fetches-locked-commit": "TASK-260925-38kn57",
    "git-moved-tag-replays-locked-commit": "TASK-260925-38kn57",
    "git-moved-tag-replays-locked-commit-through-mirror": "TASK-260925-38kn57",
    "global-schema2-without-profile-lock-refused": "TASK-260925-38kn57",
    "missing-snapshot-unreachable-source": "TASK-260925-38kn57",
    "path-missing-snapshot-drifted-bytes": "TASK-260925-38kn57",
    "path-missing-snapshot-identical-bytes": "TASK-260925-38kn57",
    "project-schema2-without-profile-lock-accepted": "TASK-260925-38kn57",
    "v2-refresh-current-endpoint-existing-checkout": "TASK-260925-38kn57",
    "v2-scp-alias-port-refused": "TASK-260925-38kn57",
    "v2-ssh-uri-alias-port": "TASK-260925-38kn57",
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


# Production entry points a semantic driver must reach, as
# (defining module, attribute) pairs. The semantic dispatch wraps every
# entry below while a driver runs and records the call sites the driver
# actually touched; a driver that returns without touching any of them
# fails as hollow (see ``test_draft_sources_semantic_case``). Fixture
# plumbing is deliberately absent: error and data classes, fixture
# writers, hash helpers, platform probes, path helpers and the git
# test-double tool. Touching only plumbing is exactly the hollow shape
# the gate must catch, so none of it may count as a call site.
#
# Membership rule (checked, not asserted): every entry below must have
# EXECUTED during the traced CLI scenarios
# (``_observed_scenario_entries``), which run the product's own
# install, upgrade, status and check paths under a call tracer
# starting at the derived console-script entry point. The evidence is
# an execution record, not a reachability argument: an entry with no
# execution fails
# ``test_draft_sources_production_table_entries_have_production_callers``
# instead of quietly vouching for drivers. The check reads no
# production source at all and keys on code-object identity, so no
# source shape — test-only caller, forwarding chain, nested body or
# method, reference passed as data, called or not — can certify an
# entry; only the entry's own code object executing counts.
# Removed entries stay removed:
# ``csk.sources.boundaries:check_selected_package``,
# ``csk.sources.selection:resolve_selector_directory``,
# ``csk.sources.source_audit:validate_source_audit``,
# ``csk.sources.transport:acquire``,
# ``csk.sources.repository_policy:canonical_endpoint_identity`` (its
# only production caller is the caller-less compat wrapper
# ``canonical_repository_identity``) and
# ``csk.install_marker:validate_attestation_evidence`` (production
# never executes it: the one production caller passes
# ``evidence_path=None``, which returns before the validator, and the
# other caller has no production callers itself; finding against
# TASK-260916-11yseo, and its drivers still touch observed marker
# entries, so no driver changed).
_PRODUCTION_ENTRY_POINTS: tuple[tuple[str, str], ...] = (
    ("csk.build_repository_pipeline", "run_pipeline"),
    ("csk.builds.currentness", "compare_external_build_evidence"),
    ("csk.builds.metadata", "parse_receipt_v3"),
    ("csk.builds.metadata", "read_receipt_v3"),
    ("csk.builds.metadata", "source_aware_cache_key"),
    ("csk.dev_substitutions", "check_source_substitution_admission"),
    ("csk.install_marker", "check_local_registry_requirement"),
    ("csk.install_marker", "compare_marker_plan"),
    ("csk.install_marker", "evaluate_marker_status"),
    ("csk.install_marker", "evaluate_schema2_status"),
    ("csk.install_marker", "read_install_marker"),
    ("csk.installer", "install"),
    ("csk.manifest", "parse_manifest"),
    ("csk.sources.boundaries", "freeze_boundaries"),
    ("csk.sources.boundaries", "recheck_publication_destination"),
    ("csk.sources.lock", "read_lock"),
    ("csk.sources.repository_policy", "load_policy"),
    ("csk.sources.repository_policy", "parse_policy"),
    ("csk.sources.repository_policy", "select_endpoints"),
    ("csk.sources.selection", "expand_collection"),
    ("csk.sources.selection", "expand_selectors"),
    ("csk.sources.selection", "managed_output_boundary"),
    ("csk.sources.selection", "resolve_individual"),
    ("csk.sources.snapshot", "capture_package_snapshot"),
    ("csk.sources.snapshot", "revalidate_capture"),
    ("csk.sources.snapshot", "verify_frozen_copy"),
    ("csk.sources.source_audit", "record_source_audit"),
    ("csk.sources.source_audit", "validate_stored_report"),
    ("csk.sources.transport", "acquire_plan"),
    ("csk.sources.transport", "plan_attempts"),
    ("csk.status", "collect_status"),
)

# Observed production call sites per semantic case id, filled by the
# semantic dispatch as drivers run. A case id appears here only after its
# driver returned having touched at least one entry above.
CASE_CALL_SITES: dict[str, list[str]] = {}


def _recording_entry(
    site: str, original: Callable[..., Any], observed: list[str]
) -> Callable[..., Any]:
    """Wrap one production entry so its calls append ``site`` to ``observed``."""

    @functools.wraps(original)
    def _call(*args: Any, **kwargs: Any) -> Any:
        observed.append(site)
        return original(*args, **kwargs)

    return _call


@contextlib.contextmanager
def _observe_production_entries() -> Iterator[list[str]]:
    """Record every production entry call made inside the window.

    Each tabled entry is replaced by a delegating wrapper for the
    duration of the window; the snapshot-store consumer openers (reached
    through the ``ALL_CONSUMERS`` list rather than by attribute) are
    wrapped by replacing the list. Everything is restored even when the
    body raises, and a partially installed window is unwound before the
    error escapes, so a failure here can neither leak wrappers into the
    next test nor hide behind them.
    """
    observed: list[str] = []
    installed: list[tuple[Any, str, Any]] = []
    consumers_module: Any = None
    original_consumers: Any = None
    try:
        for module_name, attribute in _PRODUCTION_ENTRY_POINTS:
            module = importlib.import_module(module_name)
            original = getattr(module, attribute)
            assert callable(original), (
                f"entry point is not callable: {module_name}:{attribute}"
            )
            setattr(
                module,
                attribute,
                _recording_entry(f"{module_name}:{attribute}", original, observed),
            )
            installed.append((module, attribute, original))
        consumers_module = importlib.import_module("csk.sources.consumers")
        original_consumers = consumers_module.ALL_CONSUMERS
        consumers_module.ALL_CONSUMERS = tuple(
            _recording_entry(
                f"csk.sources.consumers:{opener.__name__}", opener, observed
            )
            for opener in original_consumers
        )
    except BaseException:
        for module, attribute, original in reversed(installed):
            setattr(module, attribute, original)
        if consumers_module is not None and original_consumers is not None:
            consumers_module.ALL_CONSUMERS = original_consumers
        raise
    try:
        yield observed
    finally:
        for module, attribute, original in reversed(installed):
            setattr(module, attribute, original)
        if consumers_module is not None and original_consumers is not None:
            consumers_module.ALL_CONSUMERS = original_consumers


# Per-test outcomes for the category counts live in
# ``tests/draft_sources_accounting.py``: only a pytest hook can observe
# outcomes, and hooks cannot live in a test module, so the
# ``pytest_runtest_logreport`` hook in ``tests/conftest.py`` records
# there while the accounting test at the end of this file reads back.
# (Imported at the top of this module next to the other helpers.)

# Observed production-entry membership.
#
# The static call-graph gate was removed in the intervention round:
# three reviews laundered a dead entry through it (test-only caller,
# test-only forwarder, uncalled nested bodies and references passed
# as data), because a sound static call graph for Python is not
# obtainable. This gate observes instead of inferring: it runs the
# CLI paths the product offers (install, upgrade, status, check) on
# hermetic fixtures under a call tracer, and every tabled entry must
# have executed. Membership keys on code-object identity: a
# same-named nested function or method is a different code object
# and cannot stand in for the entry. An uncalled function is never
# observed, whatever the source around it says, so no source shape
# can forge membership.


def _derive_console_root(repo_root: Path) -> tuple[str, str, Callable[..., Any]]:
    """Resolve the installed console-script entry to its callable.

    Reads ``[project.scripts]`` from ``pyproject.toml`` (failing
    closed when the file, the table, or the ``csk`` target is missing
    or malformed), imports the named ``module:attr``, and returns
    ``(module, attr, callable)``. The traced scenarios start at this
    callable, so the gate observes the product's own entry point
    rather than a function the gate chose. A rename the manifest does
    not follow breaks loudly here instead of tracing a stale import.
    """
    pyproject = repo_root / "pyproject.toml"
    assert pyproject.is_file(), f"console-script manifest is missing: {pyproject}"
    scripts = (
        tomllib.loads(pyproject.read_text(encoding="utf-8"))
        .get("project", {})
        .get("scripts", {})
    )
    assert isinstance(scripts, dict) and "csk" in scripts, (
        f"console-script manifest names no csk entry point: {pyproject}"
    )
    target = scripts["csk"]
    assert isinstance(target, str) and ":" in target.split()[0], (
        f"csk console script names no module:function target: {target!r}"
    )
    ref = target.split()[0].split("[")[0]
    module_name, _, attr = (part.strip() for part in ref.partition(":"))
    assert module_name and attr, (
        f"csk console script names no module:function target: {target!r}"
    )
    module = importlib.import_module(module_name)
    candidate = getattr(module, attr, None)
    assert callable(candidate), (
        f"csk console script names no callable: {target!r}"
    )
    return module_name, attr, candidate


_ObservedCodeCount = tuple[CodeType, int]
_ObservedCodeLabels = tuple[CodeType, set[str]]


class _ProductionCallRecorder:
    """Record executed code objects under ``src/csk`` via ``settrace``.

    The trace function records the code object of every ``call``
    event whose file lives under the production tree and ignores
    everything else (the tracer itself lives in this test module, so
    it can never record itself). Records are keyed by ``id(code)``
    and retain that code object as the value, preventing id reuse
    while the record is alive. Consumers confirm ``record[0] is
    code``; they never rely on ``CodeType`` value equality, which
    can make byte-identical functions from different files compare
    equal. A nested function, method, or any other same-named code
    object therefore cannot certify the untouched module-level
    entry (F-2 / gate-table-admits-dead-entry). Subprocesses are not traced: only
    in-process execution counts, which is exactly the hollow gate's
    definition of a production touch. Threads are not traced either:
    production spawns threads only around real compiler runs, and
    every scenario stubs the compiler, so the traced runs are
    single-threaded. ``run`` restores the previous trace function
    even when the body raises.
    """

    def __init__(self, src_root: Path) -> None:
        self._prefix = os.fspath(src_root) + os.sep
        self.counts: dict[int, _ObservedCodeCount] = {}
        self.labels: dict[int, _ObservedCodeLabels] = {}

    def _trace(self, frame: Any, event: str, arg: Any) -> Any:
        if event == "call":
            filename = frame.f_code.co_filename
            if filename.startswith(self._prefix) and filename.endswith(".py"):
                code = frame.f_code
                code_id = id(code)
                count_record = self.counts.get(code_id)
                if count_record is None:
                    self.counts[code_id] = (code, 1)
                else:
                    recorded_code, count = count_record
                    assert recorded_code is code, (
                        "live code-object id was reused in the execution record"
                    )
                    self.counts[code_id] = (recorded_code, count + 1)
                labels_record = self.labels.get(code_id)
                if labels_record is None:
                    self.labels[code_id] = (code, {self._label})
                else:
                    recorded_code, labels = labels_record
                    assert recorded_code is code, (
                        "live code-object id was reused in the label record"
                    )
                    labels.add(self._label)
        return self._trace

    def run(self, label: str, func: Callable[..., Any], *args: Any) -> Any:
        """Run ``func(*args)`` under the tracer, attributing calls to ``label``."""
        self._label = label
        previous = sys.gettrace()
        sys.settrace(self._trace)
        try:
            return func(*args)
        finally:
            sys.settrace(previous)

    _label: str = ""


# Recorded scenario coverage per tabled entry ("the golden"). The file
# is machine-written on a capable host
# (``CSK_REGENERATE_OBSERVED_LABELS=1``) from the live traced record,
# never hand-maintained: ``entries`` maps ``"module:attr"`` to the
# sorted scenario labels that executed it, ``scenarios`` lists every
# scenario label the run knows. Where a scenario is refused by
# product design, an entry is excluded from the membership check only
# when every recorded label names a refused scenario.
OBSERVED_LABELS_PATH = Path(__file__).with_name("draft_sources_observed_labels.json")
SIMULATE_NO_EXTERNAL_BUILDS_ENV = "CSK_SIMULATE_NO_EXTERNAL_BUILDS"
REGENERATE_OBSERVED_LABELS_ENV = "CSK_REGENERATE_OBSERVED_LABELS"

#: Whether the REAL platform publishes external builds, asked of the
#: product predicate once at import — before any fixture or monkeypatch
#: can run. The capable-lane pin obligation (F-1 /
#: simulation-disarms-capable-pin) reads this capture, never the live
#: predicate: simulation may add the refused lane, but no ambient
#: variable and no patch may remove the pin a capable host owes. This
#: is the same predicate the installer calls, not a retyped rule.
_HOST_SUPPORTS_EXTERNAL_BUILDS: bool = installer.supports_external_builds()


def _load_observed_labels(
    path: Path | None = None,
) -> tuple[dict[str, tuple[str, ...]], tuple[str, ...]]:
    """Load the recorded entry-to-scenario coverage, failing closed.

    Refuses a missing file, a misshapen document, an unknown or
    duplicated scenario label, and — the laundering shape — an empty
    label set, which is a subset of every lane and would exclude its
    entry everywhere without ever executing it. The default path is
    read from the module global at call time (not bound as a
    default) so the wrong-golden regression test can plant a
    hostile record exactly as a stale file on disk would appear.
    """
    resolved = OBSERVED_LABELS_PATH if path is None else path
    assert resolved.is_file(), f"observed-labels record is missing: {resolved}"
    raw = json.loads(resolved.read_bytes())
    assert isinstance(raw, dict), "observed-labels record must be a JSON object"
    assert set(raw) == {"entries", "scenarios"}, (
        f"observed-labels record declares {sorted(raw)}"
    )
    entries_raw = raw["entries"]
    scenarios_raw = raw["scenarios"]
    assert isinstance(entries_raw, dict) and entries_raw, (
        "observed-labels record entries must be a non-empty object"
    )
    assert (
        isinstance(scenarios_raw, list)
        and scenarios_raw
        and all(isinstance(label, str) and label for label in scenarios_raw)
    ), "observed-labels record scenarios must be a non-empty string list"
    assert len(set(scenarios_raw)) == len(scenarios_raw), (
        "observed-labels record scenarios repeat a label"
    )
    known = set(scenarios_raw)
    entries: dict[str, tuple[str, ...]] = {}
    for site, labels in entries_raw.items():
        assert isinstance(site, str) and ":" in site, (
            f"observed-labels record names no module:attr entry: {site!r}"
        )
        assert (
            isinstance(labels, list)
            and labels
            and all(isinstance(label, str) and label for label in labels)
        ), (
            f"observed-labels record covers {site} with no scenario: "
            "an empty set would exclude the entry on every lane; "
            "regenerate on a capable host instead of recording it"
        )
        assert set(labels) <= known, (
            f"observed-labels record covers {site} with an unknown "
            f"scenario: {sorted(set(labels) - known)}"
        )
        assert len(set(labels)) == len(labels), (
            f"observed-labels record repeats a scenario for {site}"
        )
        entries[site] = tuple(sorted(labels))
    return entries, tuple(sorted(scenarios_raw))


def _write_observed_labels(
    path: Path,
    table: tuple[tuple[str, str], ...],
    live_labels: Mapping[tuple[str, str], set[str]],
    scenarios: Collection[str],
) -> None:
    """Record the live traced coverage for the table, deterministically.

    Every tabled entry must have executed under at least one scenario:
    a dead entry is refused here rather than recorded with an empty
    set, which would exclude it on every lane. Entries the tracer saw
    outside the table are not recorded; the record certifies the
    table, not the trace.
    """
    entries: dict[str, list[str]] = {}
    for module_name, attribute in table:
        site = f"{module_name}:{attribute}"
        labels = sorted(live_labels.get((module_name, attribute), ()))
        assert labels, (
            f"observed-labels regen refuses to record {site} with no "
            "covering scenario: remove the dead entry or cover it, do "
            "not record an empty set"
        )
        entries[site] = labels
    payload = {"entries": entries, "scenarios": sorted(scenarios)}
    path.write_text(
        json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )


def _assert_labels_agree(
    table: tuple[tuple[str, str], ...],
    live_labels: Mapping[tuple[str, str], set[str]],
    golden_entries: Mapping[str, tuple[str, ...]],
    *,
    ran: Collection[str],
    refused: Collection[str],
    golden_scenarios: Collection[str],
) -> None:
    """Fail unless the live record matches the recorded coverage exactly.

    Three equalities, narrowest last: the scenario universe the run
    knows (ran plus refused) must equal the recorded one, the tabled
    entry set must equal the recorded one, and every entry's live
    label set must equal its recorded label set. Comparing label
    sets, not just entry sets, is what keeps a narrowed record (one
    label dropped) from certifying a drifted product.
    """
    live_universe = set(ran) | set(refused)
    recorded_universe = set(golden_scenarios)
    assert live_universe == recorded_universe, (
        "observed scenarios disagree with the recorded universe: "
        f"unrecorded {sorted(live_universe - recorded_universe)}, "
        f"unrun {sorted(recorded_universe - live_universe)}; regenerate "
        "the record on a capable host"
    )
    tabled = {f"{module_name}:{attribute}" for module_name, attribute in table}
    recorded = set(golden_entries)
    assert tabled == recorded, (
        "tabled entries disagree with the recorded coverage: "
        f"unrecorded {sorted(tabled - recorded)}, "
        f"removed {sorted(recorded - tabled)}; regenerate the record "
        "on a capable host"
    )
    drifted = [
        f"{site}: live {sorted(live_labels.get((module_name, attribute), ()))} "
        f"!= recorded {sorted(golden_entries[site])}"
        for module_name, attribute in table
        for site in (f"{module_name}:{attribute}",)
        if set(live_labels.get((module_name, attribute), ()))
        != set(golden_entries[site])
    ]
    assert not drifted, (
        f"live scenario coverage drifted for {len(drifted)} tabled "
        f"entr{'y' if len(drifted) == 1 else 'ies'}: {drifted[:5]}; "
        "regenerate the record on a capable host"
    )


def _external_builds_unavailable_reason() -> str:
    """Declared reason for the refused external-build lane.

    Names the measured platform and the product rule that causes the
    refusal, both asked of the product (``sys.platform`` as observed,
    the message constant the installer itself raises), never retyped.
    """
    return (
        "external-build scenario refused by product design on this "
        f"platform (sys.platform={sys.platform}): "
        f"{installer.EXTERNAL_BUILDS_UNSUPPORTED_MESSAGE}"
    )


def _descriptor_traversal_unavailable_reason() -> str:
    """Name the platform and product capability behind traversal skips."""
    return (
        f"sys.platform={sys.platform}: "
        f"{_selection_fs.NO_DESCRIPTOR_TRAVERSAL_REASON}"
    )


def _case_alias_unavailable_reason() -> str:
    """Name the platform and filesystem capability behind case-alias skips."""
    return (
        f"sys.platform={sys.platform}: case-alias requires a case-insensitive "
        "filesystem to prove physical casing equivalence"
    )


@contextlib.contextmanager
def _observed_scenario_env(home: Path, config_path: Path) -> Iterator[None]:
    """Isolate the process environment for one traced scenario.

    Points the manager home, the user home and the config locator at
    the scenario fixture, clears the source-policy override and the
    env opt-in (the fixture config carries the opt-in), and neuters
    the ambient ssh-agent. Everything is restored afterwards, so a
    scenario can neither read the real user home nor leak its
    pointers into the next test.
    """
    saved = dict(os.environ)
    os.environ["CSK_CONFIG"] = os.fspath(config_path)
    os.environ.pop("CSK_SOURCE_POLICY", None)
    os.environ.pop("CSK_EXPERIMENTAL_SKILLFILE_SOURCES", None)
    os.environ["HOME"] = os.fspath(home)
    os.environ["USERPROFILE"] = os.fspath(home)
    os.environ["SSH_AUTH_SOCK"] = "/nonexistent-agent.sock"
    try:
        yield
    finally:
        os.environ.clear()
        os.environ.update(saved)


def _observed_gate_home(root: Path) -> tuple[Path, Path]:
    """Provision an isolated manager home and skills root under ``root``."""

    from csk import locking as _locking_module

    csk_home = root / ".cocoaskills"
    _locking_module.provision_new_manager_home(csk_home)
    skills_root = root / "skills"
    skills_root.mkdir()
    return csk_home, skills_root


def _observed_gate_save_config(
    csk_home: Path,
    skills_root: Path,
    projects: Mapping[str, Path],
    *,
    audit_enabled: bool = False,
) -> Path:
    """Write a draft-enabled manager config registering ``projects``."""

    from dataclasses import replace as _replace

    from csk import config as _config_module

    base = _config_module.GlobalConfig(
        path=csk_home / "config.json",
        skills_root=skills_root,
        preferred_locale="ru",
        default_agents=["codex_cli"],
        adapter_mode="auto",
        worktree_alias_pattern="[A-Z]+-[0-9]+",
        projects={
            alias: _config_module.ProjectConfig(
                alias=alias, path=path, agents=["codex_cli"]
            )
            for alias, path in projects.items()
        },
        experimental=_config_module.ExperimentalConfig(skillfile_sources=True),
    )
    if audit_enabled:
        base = _replace(base, audit=_replace(base.audit, enabled=True))
    _config_module.save_config(base)
    return csk_home / "config.json"


def _observed_gate_external_repo(repo_dir: Path) -> tuple[Path, str]:
    """Create a local git repo carrying a go-repository-v1 target.

    Returns the repo directory and its HEAD commit. The repo is an
    ordinary working-tree checkout (not bare): the acquisition
    fixture clones it bare, the way the transport drivers do.
    """

    from tests.conftest import commit_all, init_git_repo, write_files

    repository = init_git_repo(repo_dir)
    write_files(
        repository,
        {
            "skill-build.json": json.dumps(
                {
                    "schema_version": 1,
                    "targets": {
                        "external-tool": {
                            "driver": "go-repository-v1",
                            "build_root": ".",
                            "source_dir": "cmd/external-tool",
                        }
                    },
                }
            ),
            "go.mod": "module example.test/external-tool\n\ngo 1.25\n",
            "cmd/external-tool/main.go": "package main\nfunc main() {}\n",
            "README.md": "external tool\n",
        },
    )
    return repository, commit_all(repository, "external tool")


def _observed_gate_stub_external_build() -> Any:
    """Stub the trusted toolchain for external-build scenarios.

    Mirrors ``test_install._stub_trusted_toolchain`` without depending
    on a sibling test module: the operator search path and the
    toolchain session are faked, and the go-v1 build writes a small
    shell artifact instead of invoking a compiler. Returns started
    patches; the caller stops them. Unlike
    ``_closure_refresh_stub_build_toolchain`` the fake build reads no
    marker file, so it serves repository builds too.
    """

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
            content_sha256="sha256:" + "a" * 64,
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
        payload = (
            "#!/bin/sh\n" f"printf '%s\\n' {request.command}\n"
        ).encode()
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


def _observed_scenario_local_lifecycle(
    root: Path, recorder: _ProductionCallRecorder, main: Callable[..., Any]
) -> None:
    """Run install, status, check, upgrade, install on a local project.

    The project selects a collection and an individual member from a
    local source; the upgrade adds a member, so the run covers the
    initial, refresh and locked paths. Every command must succeed:
    a scenario that errors proves nothing about the entries it
    skipped, so a nonzero exit fails the gate loudly.
    """

    from tests.conftest import make_project

    csk_home, skills_root = _observed_gate_home(root)
    project = make_project(root)
    source = root / "pkgs"
    _closure_refresh_write_skill(source / "coll" / "review", "review")
    _closure_refresh_write_skill(source / "solo", "solo")
    _closure_refresh_skillfile(
        project,
        {"local": {"path": os.fspath(source)}},
        [
            {"from": "local", "directory": "coll", "include": ["*"]},
            {"name": "solo", "from": "local", "directory": "solo"},
        ],
    )
    config_path = _observed_gate_save_config(
        csk_home, skills_root, {"app": project}
    )
    with _observed_scenario_env(root / "home", config_path):
        assert recorder.run("local", main, ["install", "app"]) == 0
        assert recorder.run("local", main, ["status", "app"]) == 0
        assert recorder.run("local", main, ["check", "app"]) == 0
        _closure_refresh_write_skill(source / "coll" / "docs", "docs")
        assert recorder.run("local", main, ["upgrade", "app"]) == 0
        assert recorder.run("local", main, ["install", "app"]) == 0


def _observed_scenario_planned_check(
    root: Path, recorder: _ProductionCallRecorder, main: Callable[..., Any]
) -> None:
    """Run check on a repository source with a matching policy entry.

    Planning is pure and network-free: the run enters the policy and
    transport planning seams without fetching anything.
    """

    from tests.conftest import make_project

    csk_home, skills_root = _observed_gate_home(root)
    project = make_project(root)
    _closure_refresh_skillfile(
        project,
        {"net": {"repository": "example.org/kit", "tag": "v1.0.0"}},
        [{"name": "x", "from": "net", "directory": "."}],
    )
    (csk_home / "source-policy.json").write_text(
        json.dumps(
            {
                "schema_version": 2,
                "repositories": {
                    "example.org/kit": {
                        "endpoints": [
                            {
                                "url": "https://example.org/kit.git",
                                "authentication": "team-https",
                            }
                        ],
                        "fallback": "none",
                    }
                },
            }
        ),
        encoding="utf-8",
    )
    config_path = _observed_gate_save_config(
        csk_home, skills_root, {"netapp": project}
    )
    with _observed_scenario_env(root / "home", config_path):
        assert recorder.run("plan", main, ["check", "netapp"]) == 0


def _observed_scenario_substituted_install(
    root: Path, recorder: _ProductionCallRecorder, main: Callable[..., Any]
) -> None:
    """Run install with a dev manifest carrying a build substitution.

    The substitution is external (not an operator source override)
    and audit is not strict, so the admission gate is entered and
    the install succeeds.
    """

    from tests.conftest import make_project

    csk_home, skills_root = _observed_gate_home(root)
    project = make_project(root)
    source = root / "pkgs"
    _closure_refresh_write_skill(source / "coll" / "review", "review")
    _closure_refresh_skillfile(
        project,
        {"local": {"path": os.fspath(source)}},
        [{"from": "local", "directory": "coll", "include": ["*"]}],
    )
    (project / "Skillfile.dev.json").write_text(
        json.dumps(
            {
                "schema_version": 2,
                "substitutions": {},
                "build_repository_substitutions": {
                    "review": {"tools": {"path": "../external-tool"}}
                },
            }
        ),
        encoding="utf-8",
    )
    config_path = _observed_gate_save_config(
        csk_home, skills_root, {"subapp": project}
    )
    with _observed_scenario_env(root / "home", config_path):
        assert recorder.run("substitution", main, ["install", "subapp"]) == 0


def _observed_scenario_local_build(
    root: Path, recorder: _ProductionCallRecorder, main: Callable[..., Any]
) -> None:
    """Run install and status on a member with local go-v1 builds.

    Audit is enabled, so the install records source audits and the
    planning hook re-validates them; the compiler is stubbed, so no
    toolchain runs. Covers the audit, receipt and cache-key seams.
    """

    from tests.conftest import make_project

    csk_home, skills_root = _observed_gate_home(root)
    project = make_project(root)
    source = root / "pkgs"
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
    config_path = _observed_gate_save_config(
        csk_home, skills_root, {"app": project}, audit_enabled=True
    )
    patches = _closure_refresh_stub_build_toolchain()
    try:
        with _observed_scenario_env(root / "home", config_path):
            assert recorder.run("local-build", main, ["install", "app"]) == 0
            assert recorder.run("local-build", main, ["status", "app"]) == 0
    finally:
        for entered in patches:
            entered.stop()


@contextlib.contextmanager
def _observed_external_build_ready(root: Path) -> Iterator[None]:
    """Provision the external-build fixture with stubs and env active.

    The member's repository is declared but unsubstituted, so an
    install plans and acquires it; acquisition is hermetic because
    the git tool is the drivers' local-bare broker, and the compiler
    is stubbed. Shared by the supported scenario (install and status
    succeed) and the refused one (the product's structured refusal is
    asserted): the fixture is identical either way, only the platform
    admission differs. No network, no real home, no prompts.
    """

    from unittest.mock import patch as _mock_patch

    from tests.conftest import make_project

    git_url = "https://example.test/external-tool.git"
    csk_home, skills_root = _observed_gate_home(root)
    project = make_project(root)
    source = root / "pkgs"
    external, commit = _observed_gate_external_repo(root / "external-tool")
    skill_dir = source / "ext"
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text(
        "---\nname: ext\ndescription: fixture ext\n---\n\n# ext\n",
        encoding="utf-8",
    )
    (skill_dir / "agent-skill.json").write_text(
        json.dumps(
            {
                "schema_version": 7,
                "capabilities": {},
                "build_repositories": {
                    "tools": {
                        "git": git_url,
                        "locked_commit": {
                            "object_format": "sha1",
                            "hex": commit,
                        },
                    }
                },
                "commands": {
                    "external-tool": {
                        "type": "build",
                        "driver": "go-repository-v1",
                        "repository": "tools",
                        "target": "external-tool",
                    }
                },
            }
        ),
        encoding="utf-8",
    )
    _closure_refresh_skillfile(
        project,
        {"local": {"path": os.fspath(source)}},
        [{"name": "ext", "from": "local", "directory": "ext"}],
    )
    config_path = _observed_gate_save_config(
        csk_home, skills_root, {"app": project}, audit_enabled=True
    )
    bare = root / "bare.git"
    git = os.fspath(Path(shutil.which("git")).resolve())
    subprocess.run(
        (git, "clone", "--quiet", "--bare", os.fspath(external), os.fspath(bare)),
        stdin=subprocess.DEVNULL,
        capture_output=True,
        check=True,
        timeout=30,
    )
    tool = _transport_git(root / "tool", {git_url: bare})
    patches = _observed_gate_stub_external_build()
    tool_patch = _mock_patch.object(
        installer, "_external_git_tool", lambda *_args, **_kwargs: tool
    )
    tool_patch.start()
    try:
        with _observed_scenario_env(root / "home", config_path):
            yield
    finally:
        tool_patch.stop()
        for entered in patches:
            entered.stop()


def _assert_external_build_refusal(exit_code: int, stderr_text: str) -> None:
    """Assert an install exit is the structured external-build refusal.

    Pins the exit code, the error class and the product's own refusal
    text, all asked of the product (the class constant, the message
    constant the installer raises), so a differently failing install
    cannot pose as the deferred-qualification refusal.
    """
    assert exit_code == 1, (
        "external-build install on a platform without support must "
        f"exit 1, got {exit_code}"
    )
    assert source_errors.CODE_MEMBER_INVALID in stderr_text, (
        "external-build refusal names no "
        f"{source_errors.CODE_MEMBER_INVALID}: {stderr_text!r}"
    )
    assert installer.EXTERNAL_BUILDS_UNSUPPORTED_MESSAGE in stderr_text, (
        f"external-build refusal carries no product refusal text: {stderr_text!r}"
    )


def _observed_scenario_external_build(
    root: Path, recorder: _ProductionCallRecorder, main: Callable[..., Any]
) -> None:
    """Run install and status on a member with external builds.

    Runs only where the product admits external builds. Covers the
    pipeline, acquisition and external currentness seams. Every
    command must succeed: a scenario that errors proves nothing about
    the entries it skipped, so a nonzero exit fails the gate loudly.
    """
    with _observed_external_build_ready(root):
        assert recorder.run("external-build", main, ["install", "app"]) == 0
        assert recorder.run("external-build", main, ["status", "app"]) == 0


def _observed_scenario_external_build_refused(
    root: Path, recorder: _ProductionCallRecorder, main: Callable[..., Any]
) -> None:
    """Run install where the product refuses external builds by design.

    Same fixture as the supported scenario; the install must exit 1
    with the structured deferred-qualification refusal (class plus
    product text), which is this scenario's evidence on the refused
    lane. The partial trace still records the entries the refusing
    run executes; the entries past the refusal stay unobserved and
    are excluded by recorded coverage, never by enumeration.
    """
    with _observed_external_build_ready(root):
        stderr = io.StringIO()
        with contextlib.redirect_stderr(stderr):
            exit_code = recorder.run("external-build", main, ["install", "app"])
        _assert_external_build_refusal(exit_code, stderr.getvalue())


def _run_observed_scenarios(
    repo_root: Path,
    *,
    refused_lane: bool = False,
) -> tuple[
    dict[int, _ObservedCodeCount],
    dict[int, _ObservedCodeLabels],
    tuple[str, str],
    frozenset[str],
    frozenset[str],
]:
    """Run every traced CLI scenario for one lane; return the record plus ran/refused.

    Skips (rather than failing) where the platform provides no
    descriptor-relative traversal: selection, capture and audit all
    descend that way, so the scenarios cannot run without it. Each
    scenario gets an isolated fixture tree; every command must exit
    as its scenario demands, so a broken fixture or a production
    regression fails here instead of certifying a partial execution
    record. The lane is selected explicitly, never by branching on
    the live predicate: the refused lane forces the product's own
    support predicate false for the duration of the run (scoped, so
    the forcing cannot leak into any pin decision) and the
    external-build scenario asserts the structured refusal instead
    of succeeding. The native lane always matches the real platform;
    the simulated refused lane is an additional run, never a
    replacement. Returns the per-code-object call counts, the
    per-code-object scenario labels, the derived ``(module, attr)``
    console root the runs started at, and the ran and refused
    scenario label sets.
    """

    if not _selection_fs.supports_descriptor_traversal():
        pytest.skip(_descriptor_traversal_unavailable_reason())
    module_name, attr, main = _derive_console_root(repo_root)
    recorder = _ProductionCallRecorder(repo_root / "src")
    ran: set[str] = set()
    refused: set[str] = set()
    with tempfile.TemporaryDirectory(prefix="csk-observed-gate-") as raw:
        base = Path(raw)
        _observed_scenario_local_lifecycle(base / "local", recorder, main)
        ran.add("local")
        _observed_scenario_planned_check(base / "plan", recorder, main)
        ran.add("plan")
        _observed_scenario_substituted_install(
            base / "substitution", recorder, main
        )
        ran.add("substitution")
        _observed_scenario_local_build(base / "local-build", recorder, main)
        ran.add("local-build")
        if refused_lane:
            with patch.object(
                installer, "supports_external_builds", return_value=False
            ):
                _observed_scenario_external_build_refused(
                    base / "external-build", recorder, main
                )
            refused.add("external-build")
        else:
            _observed_scenario_external_build(
                base / "external-build", recorder, main
            )
            ran.add("external-build")
    assert recorder.counts, "the traced scenarios recorded no production calls"
    return (
        recorder.counts,
        recorder.labels,
        (module_name, attr),
        frozenset(ran),
        frozenset(refused),
    )


def _entry_code_object(module_name: str, attribute: str) -> CodeType | None:
    """Resolve a tabled entry to its code object, or None when unobservable.

    The observed membership check keys on this identity: the entry
    counts as executed only when THIS code object ran under the
    tracer. A same-named nested function or method owns a different
    code object and can never stand in for it (F-2 /
    gate-table-admits-dead-entry).
    """
    try:
        module = importlib.import_module(module_name)
    except ImportError:
        return None
    code = getattr(getattr(module, attribute, None), "__code__", None)
    return code if isinstance(code, CodeType) else None


def _project_table_labels(
    table: tuple[tuple[str, str], ...],
    code_labels: Mapping[int, _ObservedCodeLabels],
) -> dict[tuple[str, str], set[str]]:
    """Project the code-keyed label record onto table entries.

    Each entry resolves to its own code object (see
    ``_entry_code_object``); an unresolvable entry projects to
    nothing, so the pin and the regen writer fail closed on it
    exactly as the membership check does.
    """
    projected: dict[tuple[str, str], set[str]] = {}
    for module_name, attribute in table:
        code = _entry_code_object(module_name, attribute)
        record = code_labels.get(id(code)) if code is not None else None
        if record is not None and record[0] is code:
            projected[(module_name, attribute)] = set(record[1])
    return projected


def _check_table_observed(
    table: tuple[tuple[str, str], ...],
    observed: Mapping[int, _ObservedCodeCount],
    refused: Collection[str],
) -> None:
    """Fail naming every tabled entry neither executed nor excluded.

    Each entry must first name a real callable resolving to a code
    object: a typo, a nonexistent module, or a name without code
    fails here, not as a mystery unobserved entry. An entry that
    resolves but whose code never executed fails as missing,
    whatever source points at it — a test-only caller, a forwarder,
    a nested body or a reference passed as data, called or not,
    never executes the entry's own code object. The check reads no
    production source, so there is no token to preserve and no graph
    to launder through.

    Where scenarios were refused by product design, an entry the
    runnable scenarios never executed is excluded only when the
    recorded coverage proves every covering scenario was refused;
    the excluded set is derived from that record, never enumerated,
    and an entry with no record is required on every lane, so a new
    tabled entry cannot be silently swallowed. With nothing refused
    the record is not consulted, so the first regen needs no record
    to compare against.
    """
    unresolvable: list[str] = []
    for module_name, attribute in table:
        try:
            module = importlib.import_module(module_name)
        except ImportError:
            unresolvable.append(f"{module_name}:{attribute} (no such module)")
            continue
        if not callable(getattr(module, attribute, None)):
            unresolvable.append(
                f"{module_name}:{attribute} (names no callable)"
            )
            continue
        if _entry_code_object(module_name, attribute) is None:
            unresolvable.append(
                f"{module_name}:{attribute} (names no code object)"
            )
    assert not unresolvable, (
        "tabled production entries naming no production callable: "
        f"{unresolvable}; fix the table"
    )
    golden = _load_observed_labels()[0] if refused else {}
    refused_set = set(refused)
    missing: list[str] = []
    excluded: list[str] = []
    for module_name, attribute in table:
        site = f"{module_name}:{attribute}"
        code = _entry_code_object(module_name, attribute)
        record = observed.get(id(code)) if code is not None else None
        if record is not None and record[0] is code:
            continue
        covered = golden.get(site)
        if covered and set(covered) <= refused_set:
            excluded.append(site)
            continue
        missing.append(site)
    if excluded:
        reason = _external_builds_unavailable_reason()
        for site in excluded:
            print(f"draft-sources table {site} <- EXCLUDED ({reason})")
    detail = (
        "tabled production entries never executed by the traced CLI "
        f"scenarios: {missing}; "
    )
    if refused:
        detail += (
            f"excluded by recorded coverage: {excluded or 'none'} "
            f"({_external_builds_unavailable_reason()}); "
        )
    detail += (
        "re-point their drivers at entries the product executes, remove "
        "the entries, or (new production entries) regenerate the "
        "recorded coverage on a capable host"
    )
    assert not missing, detail


_ObservedRecord = tuple[
    dict[int, _ObservedCodeCount],
    dict[int, _ObservedCodeLabels],
    tuple[str, str],
    frozenset[str],
    frozenset[str],
]

_OBSERVED_SCENARIO_CACHE: _ObservedRecord | None = None
_SIMULATED_REFUSED_CACHE: _ObservedRecord | None = None
_SIMULATED_REFUSED_COMPUTED: bool = False


def _capable_pin_required() -> bool:
    """Whether this process owes the capable-lane golden pin.

    Derived only from the REAL platform (the import-time capture),
    never from the live predicate or a regeneration environment
    variable: no ambient variable and no patch may remove it (F-1 /
    simulation-disarms-capable-pin).
    """
    return _HOST_SUPPORTS_EXTERNAL_BUILDS


def _pin_native_labels_against_golden(
    code_labels: Mapping[int, _ObservedCodeLabels],
    *,
    ran: Collection[str],
    refused: Collection[str],
) -> None:
    """Pin the native capable record against the recorded coverage.

    No-op unless ``_capable_pin_required``: on an incapable host the
    native record is partial and cannot re-approve the whole. The
    projection resolves each tabled entry to its own code object, so
    the pin compares exactly what the membership check observes.
    """
    if not _capable_pin_required():
        return
    golden_entries, golden_scenarios = _load_observed_labels()
    _assert_labels_agree(
        _PRODUCTION_ENTRY_POINTS,
        _project_table_labels(_PRODUCTION_ENTRY_POINTS, code_labels),
        golden_entries,
        ran=ran,
        refused=refused,
        golden_scenarios=golden_scenarios,
    )


def _observed_scenario_entries(*, pin: bool = True) -> _ObservedRecord:
    """Return the NATIVE traced execution record, computing it once per process.

    The scenarios are the slow part of the gate; the membership test
    and every control param share one record. The record depends on
    nothing the tests mutate (scenarios never consult the table and
    run outside the hollow observer), so sharing cannot mask a
    failure: a mutant that weakens the check still dies in the
    control that plants what the check must reject. The native lane
    always matches the real platform, and on a capable host the
    record is pinned against the recorded coverage before it is
    shared, so every consumer certifies an agreed record. The pin
    obligation is derived from the real platform, never from the
    live predicate, so simulation cannot remove it; see
    ``_simulated_refused_entries`` for the added refused lane.
    """
    global _OBSERVED_SCENARIO_CACHE
    if _OBSERVED_SCENARIO_CACHE is None:
        repo_root = Path(__file__).parents[1]
        counts, labels, root, ran, refused = _run_observed_scenarios(
            repo_root, refused_lane=not _HOST_SUPPORTS_EXTERNAL_BUILDS
        )
        _OBSERVED_SCENARIO_CACHE = (counts, labels, root, ran, refused)
    if pin:
        _, labels, _, ran, refused = _OBSERVED_SCENARIO_CACHE
        _pin_native_labels_against_golden(labels, ran=ran, refused=refused)
    return _OBSERVED_SCENARIO_CACHE


def _simulated_refused_entries() -> _ObservedRecord | None:
    """Run the simulated refused lane when requested, once per process.

    Returns None unless ``CSK_SIMULATE_NO_EXTERNAL_BUILDS=1`` on a
    capable host: simulation ADDS the refused-lane run beside the
    native capable run (which stays pinned). On an incapable host
    the native record is already the refused lane, so there is
    nothing to add.
    """
    global _SIMULATED_REFUSED_CACHE, _SIMULATED_REFUSED_COMPUTED
    if not _SIMULATED_REFUSED_COMPUTED:
        _SIMULATED_REFUSED_COMPUTED = True
        if (
            os.environ.get(SIMULATE_NO_EXTERNAL_BUILDS_ENV) == "1"
            and _HOST_SUPPORTS_EXTERNAL_BUILDS
        ):
            repo_root = Path(__file__).parents[1]
            _SIMULATED_REFUSED_CACHE = _run_observed_scenarios(
                repo_root, refused_lane=True
            )
    return _SIMULATED_REFUSED_CACHE


def _review_probe_test_only_caller(*args: Any, **kwargs: Any) -> Any:
    """A test-only caller of the dead entry, never called by the product.

    Exists so the laundering-shape CLASS test plants a REAL caller
    (this function forwards to the dead entry and would work if
    called) and the observed gate still rejects it: defined and even
    callable is not executed.
    """

    from csk.sources.boundaries import check_selected_package

    return check_selected_package(*args, **kwargs)


def _review_probe_outer_with_uncalled_nested(*args: Any, **kwargs: Any) -> Any:
    """An outer function whose nested dead-entry call never runs.

    The nested body calls the dead entry, exactly the shape that
    laundered the static gate; the outer itself is never executed by
    the traced scenarios, so neither is the body.
    """

    def never_called_review_probe(*nested_args: Any, **nested_kwargs: Any) -> Any:
        from csk.sources.boundaries import check_selected_package

        return check_selected_package(*nested_args, **nested_kwargs)

    raise AssertionError(
        "the uncalled-nested probe must never run; "
        f"its nested body is reachable only through it ({len(args)} args)"
    )


def _review_probe_reference_holder() -> str:
    """A function referencing the dead entry as data, never calling it.

    The reference (``str`` of the function object) is the second
    shape that laundered the static gate; the holder is never
    executed by the traced scenarios.
    """

    from csk.sources.boundaries import check_selected_package

    return str(check_selected_package)

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
    schemas_draft = repository_root / "schemas" / "skillfile-sources-v1"
    assert schemas_v1.is_dir(), f"draft sources checkout has no schemas/v1: {repository_root}"
    assert schemas_draft.is_dir(), (
        f"skillfile-sources checkout has no schemas/skillfile-sources-v1: {repository_root}"
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
        assert set(entry) in (
            {"schema", "instance", "valid"},
            {"schema", "instance", "valid", "comment"},
        ), entry
        assert isinstance(entry["valid"], bool), entry
        if "comment" in entry:
            assert isinstance(entry["comment"], str), entry
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


# Semantic drivers that call another repository test use the capture fixture
# owned by their corpus node so nested CLI assertions inspect real output.
_CURRENT_SEMANTIC_CAPTURE: pytest.CaptureFixture[str] | None = None


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
def test_draft_sources_semantic_case(
    case: dict[str, Any], capsys: pytest.CaptureFixture[str]
) -> None:
    case_id = case["id"]
    assert case_id in CASE_OWNERS, f"draft semantic case without an owning task: {case_id}"
    driver = SEMANTIC_DRIVERS.get(case_id)
    if driver is None:
        pytest.skip(f"not yet implemented: {CASE_OWNERS[case_id]}")
    global _CURRENT_SEMANTIC_CAPTURE
    previous_capture = _CURRENT_SEMANTIC_CAPTURE
    _CURRENT_SEMANTIC_CAPTURE = capsys
    try:
        with _observe_production_entries() as observed:
            driver(case)
    finally:
        _CURRENT_SEMANTIC_CAPTURE = previous_capture
    if not observed:
        pytest.fail(
            f"hollow driver for {case_id} (owner {CASE_OWNERS[case_id]}): the "
            "driver returned without calling any production entry point, so "
            "it reproduces the expected label without executing production "
            "code; drive resolve, install, refresh or launch against a "
            "fixture built from the case input"
        )
    CASE_CALL_SITES[case_id] = sorted(set(observed))


def test_draft_sources_registered_driver_dispatch_through_the_semantic_entry(
    capsys: pytest.CaptureFixture[str],
) -> None:
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
    # The probe touches one cheap production entry so the hollow-driver
    # gate lets it through; a probe that touches nothing is exactly what
    # test_draft_sources_hollow_driver_is_caught_by_name covers. The
    # recorded call sites are preserved the same way: the probe run
    # would otherwise overwrite the real driver's sites for this id in
    # the report printed at the end of the module.
    case = SEMANTIC_CASES[0]
    real = SEMANTIC_DRIVERS.pop(case["id"], None)
    real_sites = CASE_CALL_SITES.pop(case["id"], None)
    try:
        received: list[dict[str, Any]] = []

        def _record(driven: dict[str, Any]) -> None:
            repository_policy.parse_policy({"schema_version": 1, "repositories": {}})
            received.append(driven)

        register_semantic_driver(case["id"], _record)
        try:
            test_draft_sources_semantic_case(case, capsys)
        finally:
            del SEMANTIC_DRIVERS[case["id"]]
        assert received == [case]
        assert received[0] is case

        def _boom(driven: dict[str, Any]) -> None:
            raise AssertionError("driver failure propagates through the semantic entry")

        register_semantic_driver(case["id"], _boom)
        try:
            with pytest.raises(AssertionError, match="driver failure propagates"):
                test_draft_sources_semantic_case(case, capsys)
        finally:
            del SEMANTIC_DRIVERS[case["id"]]
    finally:
        if real is not None:
            SEMANTIC_DRIVERS[case["id"]] = real
        CASE_CALL_SITES.pop(case["id"], None)
        if real_sites is not None:
            CASE_CALL_SITES[case["id"]] = real_sites


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
    if case_id in {"v2-ssh-uri-alias-port", "v2-scp-alias-port-refused"}:
        endpoint_value = endpoint(
            case_input["endpoint"],
            case_input.get("endpoint_authentication", "team-ssh"),
            alias=case_input["alias"],
            mirror_of=case_input["mirror_of"],
        )
        return {
            "schema_version": 2,
            "repositories": {
                repository: {"endpoints": [endpoint_value], "fallback": "none"}
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
        "v2-scp-alias-port-refused",
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
        "v2-ssh-uri-alias-port",
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
        elif case_id == "v2-alias-resolution":
            assert plan.endpoints[0].host == "mirror.corp.example"
            assert plan.endpoints[0].port == 8443
        else:
            alias = case_input["aliases"][case_input["alias"]]
            assert plan.endpoints[0].host == alias["host"]
            assert plan.endpoints[0].port == alias["port"]
            assert plan.max_attempts == 1
            connection = source_transport._connection_target(plan.endpoints[0])
            assert connection.remote_url == (
                f"ssh://git@{alias['host']}:{alias['port']}/kit.git"
            )
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
    "v2-ssh-uri-alias-port",
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
    "v2-scp-alias-port-refused",
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
    """Drive ``selector-escape`` through the production individual resolver.

    Registered by TASK-260916-2wjh3m (selection), re-pointed by
    TASK-260916-2je9f6: the selector directory is a portable path
    whose leading component is a symlink to a directory outside the
    source root. The escape target exists, yet resolution must fail
    with ``source_selection_invalid`` before any member read. The
    previous ``selection.resolve_selector_directory`` has zero
    production callers; ``resolve_individual`` is the live
    single-directory path (reached from ``expand_selectors``), and the
    escape fails in its preflight, before the member name is checked.
    """

    if not _selection_fs.supports_descriptor_traversal():
        pytest.skip(_descriptor_traversal_unavailable_reason())

    import tempfile

    from csk.sources import selection as selection_module
    from csk.sources import skillfile_v2 as skillfile_module

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
            selection_module.resolve_individual(
                root,
                skillfile_module.IndividualSelector(
                    name="review", from_alias="local", directory=directory
                ),
            )
        assert excinfo.value.code == case["expected"]


def _drive_missing_excluded_literal(case: dict[str, Any]) -> None:
    """Drive ``missing-excluded-literal`` through collection expansion.

    Registered by TASK-260916-2wjh3m (selection): a missing explicit member
    fails ``source_member_missing`` even though the same name is excluded.
    """

    if not _selection_fs.supports_descriptor_traversal():
        pytest.skip(_descriptor_traversal_unavailable_reason())

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
        pytest.skip(_descriptor_traversal_unavailable_reason())

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
        pytest.skip(_descriptor_traversal_unavailable_reason())

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
        # Re-pointed by TASK-260916-2je9f6: ``transport.acquire`` has
        # zero production callers. The live seam for an unreadable
        # policy file is ``repository_policy.load_policy`` (reached
        # from production via ``transport.resolve_ref`` and
        # ``config``), which fails ``repository_policy_invalid``
        # before any network I/O.
        with tempfile.TemporaryDirectory(prefix="csk-policy-case-") as raw_root:
            path = Path(raw_root) / "source-policy.json"
            path.write_bytes(b"{}")
            socket_attempts = 0

            def fail_socket(*args: Any, **kwargs: Any) -> Any:
                nonlocal socket_attempts
                socket_attempts += 1
                raise AssertionError("unreadable policy reached network")

            with (
                patch.object(socket, "socket", side_effect=fail_socket),
                patch.object(
                    Path,
                    "read_bytes",
                    side_effect=PermissionError("policy unreadable"),
                ),
                pytest.raises(repository_policy.RepositoryPolicyError) as excinfo,
            ):
                repository_policy.load_policy(path)
            assert excinfo.value.code == repository_policy.CODE_POLICY_INVALID
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
                    # Re-pointed by TASK-260916-2je9f6:
                    # ``transport.acquire`` has zero production callers.
                    # ``transport.plan_attempts`` is the live planning
                    # seam (called from production by the installer and
                    # the CLI); with no policy entry and no declared
                    # URL it fails ``repository_endpoint_unavailable``
                    # without consulting the forged user config.
                    with pytest.raises(
                        repository_policy.RepositoryPolicyError
                    ) as excinfo:
                        source_transport.plan_attempts(
                            case_input["repository"], None, None
                        )
                    assert excinfo.value.code == repository_policy.CODE_ENDPOINT_UNAVAILABLE
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
                        # Re-pointed by TASK-260916-2je9f6:
                        # ``transport.acquire`` has zero production
                        # callers; plan then acquire through the two
                        # live seams production uses.
                        plan = source_transport.plan_attempts(
                            declared_identity, declared, None
                        )
                        result = source_transport.acquire_plan(
                            plan,
                            git_admission.LockedCommit("sha1", commit),
                            tool,
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


def _live_boundary_answer(project: Path, home: Path, directory: str) -> str | None:
    """Run the production managed-output predicate over a real session.

    Opens a descriptor-confined ``SelectionSession`` the way production
    selection does (``PRUNED_CHILD_NAMES``, the fixture home, the
    package path preflighted), descends to the package, and returns
    what ``csk.sources.selection.managed_output_boundary`` (which
    delegates to ``SelectionSession.managed_boundary``) answers:
    ``None`` for allow, a reason for ``source_output_overlap`` (the
    mapping production applies at ``selection.py`` resolve/expand
    time). Re-pointed by TASK-260916-2je9f6: the previous
    ``boundaries.check_selected_package`` path has zero production
    callers.
    """

    from csk.sources import _selection_fs as selection_fs_module
    from csk.sources import selection as selection_module

    components = () if directory == "." else tuple(directory.split("/"))
    with selection_fs_module.SelectionSession.open(
        project,
        home,
        managed_names=selection_module.PRUNED_CHILD_NAMES,
        preflight=selection_fs_module.PreflightRequest(
            paths=(
                selection_fs_module.PreflightPath(
                    components,
                    code=source_errors.CODE_SELECTION_INVALID,
                    context=f"Selector directory {directory!r} cannot be resolved",
                ),
            )
        ),
    ) as session:
        if directory == ".":
            node = session.root
        else:
            node = session.descend(
                session.root,
                list(components),
                code=source_errors.CODE_SELECTION_INVALID,
                missing_code=source_errors.CODE_MEMBER_MISSING,
                context=f"Selector directory {directory!r} cannot be resolved",
            ).directory
        return selection_module.managed_output_boundary(node, session=session)


def _drive_broad_root(case: dict[str, Any]) -> None:
    """Drive ``broad-root`` through the production boundary predicate.

    Registered by TASK-260916-100uew (boundaries), re-pointed by
    TASK-260916-2je9f6: a broad alias path with a safe selected
    subdirectory is allowed by ``managed_output_boundary``.
    """

    import tempfile

    if not _selection_fs.supports_descriptor_traversal():
        pytest.skip(_descriptor_traversal_unavailable_reason())
    case_input = case["input"]
    with tempfile.TemporaryDirectory(prefix="csk-broad-root-") as raw:
        project, home = _boundary_fixture(Path(raw), case_input)
        (project / "agents" / "skills" / "review").mkdir(parents=True, exist_ok=True)
        reason = _live_boundary_answer(project, home, case_input["directory"])
        assert case["expected"] == "allow"
        assert reason is None


def _drive_managed_source(case: dict[str, Any]) -> None:
    """Drive ``managed-source`` through the production boundary predicate.

    Registered by TASK-260916-100uew (boundaries), re-pointed by
    TASK-260916-2je9f6: a selected package inside a managed output is
    refused by ``managed_output_boundary`` with ``source_output_overlap``.
    """

    import tempfile

    if not _selection_fs.supports_descriptor_traversal():
        pytest.skip(_descriptor_traversal_unavailable_reason())
    case_input = case["input"]
    with tempfile.TemporaryDirectory(prefix="csk-managed-source-") as raw:
        project, home = _boundary_fixture(Path(raw), case_input)
        reason = _live_boundary_answer(project, home, case_input["directory"])
        assert case["expected"] == source_errors.CODE_OUTPUT_OVERLAP
        assert reason is not None, (
            f"{case_input['directory']!r} was allowed by the production predicate"
        )
        assert ".agents" in reason, reason


def _drive_symlink_managed(case: dict[str, Any]) -> None:
    """Drive ``symlink-managed`` through the production boundary predicate.

    Registered by TASK-260916-100uew (boundaries), re-pointed by
    TASK-260916-2je9f6: the selected directory resolves through a link
    into a managed output, so ``managed_output_boundary`` refuses it
    with ``source_output_overlap``.
    """

    import tempfile

    if not _selection_fs.supports_descriptor_traversal():
        pytest.skip(_descriptor_traversal_unavailable_reason())
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
        reason = _live_boundary_answer(project, home, directory)
        assert case["expected"] == source_errors.CODE_OUTPUT_OVERLAP
        assert reason is not None, (
            f"{directory!r} was allowed by the production predicate"
        )
        assert ".agents" in reason, reason


def _drive_case_alias(case: dict[str, Any]) -> None:
    """Drive ``case-alias`` through the production boundary predicate.

    Registered by TASK-260916-100uew (boundaries), re-pointed by
    TASK-260916-2je9f6: on a case-insensitive filesystem the
    case-variant spelling names the managed output and
    ``managed_output_boundary`` refuses it with
    ``source_output_overlap``. On a case-sensitive host the refusal is
    inapplicable and the driver skips with the declared platform bound.
    """

    import tempfile

    if not _selection_fs.supports_descriptor_traversal():
        pytest.skip(_descriptor_traversal_unavailable_reason())
    case_input = case["input"]
    with tempfile.TemporaryDirectory(prefix="csk-case-alias-") as raw:
        tmp = Path(raw)
        probe = tmp / "CSK-DRIVER-PROBE"
        probe.write_text("x", encoding="utf-8")
        conflates = (tmp / "csk-driver-probe").exists()
        probe.unlink()
        if not conflates:
            pytest.skip(_case_alias_unavailable_reason())
        project, home = _boundary_fixture(tmp, case_input)
        physical = str(case_input["physical"])
        relative = physical.split("/", 1)[1]
        (project / relative).mkdir(parents=True, exist_ok=True)
        reason = _live_boundary_answer(project, home, relative)
        assert case["expected"] == source_errors.CODE_OUTPUT_OVERLAP
        assert reason is not None, (
            f"{relative!r} was allowed by the production predicate"
        )
        assert ".agents" in reason, reason


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
    """Drive ``root-no-inputs`` through the live selection entry point.

    Registered by TASK-260916-100uew (boundaries), re-pointed by
    BUG-260922-1o40hs: the previous ``boundaries.check_selected_package``
    path has zero production callers, so the driver now resolves the
    root through ``selection.resolve_individual`` with no allowlist. A
    root selection without ``root_inputs`` fails
    ``source_output_overlap`` because the manager cannot prove
    separation. The refusal precedes member validation, so the fixture
    carries no SKILL.md.
    """

    import tempfile

    from csk.sources import selection as selection_module
    from csk.sources import skillfile_v2 as skillfile_module

    if not _selection_fs.supports_descriptor_traversal():
        pytest.skip(_descriptor_traversal_unavailable_reason())
    case_input = case["input"]
    assert case_input["root_inputs"] is None
    assert case_input["directory"] == "."
    assert case["expected"] == source_errors.CODE_OUTPUT_OVERLAP
    with tempfile.TemporaryDirectory(prefix="csk-root-no-inputs-") as raw:
        project = Path(raw) / "project"
        (project / "agents" / "skills" / "review").mkdir(parents=True)
        with pytest.raises(source_errors.SourceError) as excinfo:
            selection_module.resolve_individual(
                project,
                skillfile_module.IndividualSelector(
                    name="review", from_alias="local", directory="."
                ),
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
        pytest.skip(_descriptor_traversal_unavailable_reason())

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
        pytest.skip(_descriptor_traversal_unavailable_reason())

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
        pytest.skip(_descriptor_traversal_unavailable_reason())

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


# Corpus replay vectors are driven through the existing CLI/install regressions.
# The called tests exercise ``csk install`` -> ``installer.install`` and assert
# lock bytes, installed content and transport/ref-resolution behavior. Their
# pytest capture fixture is supplied by the semantic corpus node.
def _invoke_source_closure_test(function_name: str) -> None:
    capture = _CURRENT_SEMANTIC_CAPTURE
    assert capture is not None, "semantic corpus capture fixture is unavailable"
    test_directory = Path(__file__).parent
    inserted_test_path = os.fspath(test_directory) not in sys.path
    if inserted_test_path:
        sys.path.insert(0, os.fspath(test_directory))
    try:
        closure_tests = importlib.import_module("test_source_closure_refresh")
    finally:
        if inserted_test_path:
            sys.path.remove(os.fspath(test_directory))
    task_temp = Path(__file__).parents[1] / ".temp" / "TASK-260925-38kn57" / "case-runs"
    task_temp.mkdir(parents=True, exist_ok=True)
    from csk import locking as locking_module

    with tempfile.TemporaryDirectory(
        prefix="semantic-", dir=os.fspath(task_temp)
    ) as raw:
        root = Path(raw)
        skills_root = root / "skills"
        skills_root.mkdir()
        csk_home = root / ".cocoaskills"
        locking_module.provision_new_manager_home(csk_home)
        monkeypatch = pytest.MonkeyPatch()
        try:
            getattr(closure_tests, function_name)(
                root, skills_root, csk_home, monkeypatch, capture
            )
        finally:
            monkeypatch.undo()


def _drive_git_replay_vector(case: dict[str, Any]) -> None:
    case_id = case["id"]
    if case_id == "git-missing-snapshot-fetches-locked-commit":
        assert case["expected"] == "fetch-locked-commit;install;lock-byte-identical"
        test_name = "test_cli_committed_git_lock_fetches_locked_commit_after_tag_moves"
    elif case_id == "git-moved-tag-replays-locked-commit":
        assert "no-tag-resolution" in case["expected"]
        test_name = "test_cli_committed_git_lock_fetches_locked_commit_after_tag_moves"
    elif case_id == "git-moved-tag-replays-locked-commit-through-mirror":
        assert "from-listed-mirror" in case["expected"]
        test_name = "test_cli_committed_git_lock_replays_through_listed_mirror_after_tag_moves"
    elif case_id == "missing-snapshot-unreachable-source":
        assert case["expected"] == "source_snapshot_unavailable"
        test_name = "test_cli_committed_git_lock_unreachable_remote_is_unavailable"
    elif case_id == "path-missing-snapshot-drifted-bytes":
        assert case["expected"] == "source_snapshot_changed"
        test_name = "test_cli_committed_path_lock_drift_refuses_changed_without_storing_bytes"
    elif case_id == "path-missing-snapshot-identical-bytes":
        assert case["expected"] == "install;lock-byte-identical"
        test_name = "test_cli_committed_path_lock_installs_from_empty_home_and_status_is_current"
    else:
        raise AssertionError(f"no replay driver for {case_id!r}")
    _invoke_source_closure_test(test_name)


for _case_id in (
    "git-missing-snapshot-fetches-locked-commit",
    "git-moved-tag-replays-locked-commit",
    "git-moved-tag-replays-locked-commit-through-mirror",
    "missing-snapshot-unreachable-source",
    "path-missing-snapshot-drifted-bytes",
    "path-missing-snapshot-identical-bytes",
):
    register_semantic_driver(_case_id, _drive_git_replay_vector)


def _drive_global_schema2_refusal(case: dict[str, Any]) -> None:
    """Drive the global-scope refusal through the real global-install CLI."""

    assert case["input"]["scope"] == "machine-global"
    assert case["input"]["stored_scope_schema_version"] == 1
    assert case["expected"] == "upgrade-error;machine-global-scope-remains-schema-1"
    capture = _CURRENT_SEMANTIC_CAPTURE
    assert capture is not None, "semantic corpus capture fixture is unavailable"
    test_directory = Path(__file__).parent
    inserted_test_path = os.fspath(test_directory) not in sys.path
    if inserted_test_path:
        sys.path.insert(0, os.fspath(test_directory))
    try:
        global_tests = importlib.import_module("test_global_install")
    finally:
        if inserted_test_path:
            sys.path.remove(os.fspath(test_directory))
    task_temp = Path(__file__).parents[1] / ".temp" / "TASK-260925-38kn57" / "case-runs"
    task_temp.mkdir(parents=True, exist_ok=True)
    from csk import locking as locking_module

    with tempfile.TemporaryDirectory(
        prefix="global-scope-", dir=os.fspath(task_temp)
    ) as raw:
        root = Path(raw)
        skills_root = root / "skills"
        skills_root.mkdir()
        csk_home = root / ".cocoaskills"
        locking_module.provision_new_manager_home(csk_home)
        monkeypatch = pytest.MonkeyPatch()
        try:
            global_tests.test_global_install_keeps_schema_1_scope_and_plain_refusal(
                monkeypatch, root, skills_root, csk_home, capture
            )
        finally:
            monkeypatch.undo()


register_semantic_driver(
    "global-schema2-without-profile-lock-refused", _drive_global_schema2_refusal
)


def _drive_project_schema2_acceptance(case: dict[str, Any]) -> None:
    """Drive project schema-2 acceptance through the production parser."""

    from csk import manifest as manifest_module

    capabilities = case["input"]["manager_capabilities"]
    assert capabilities == {"profile_locks": False, "skillfile_sources_v1": True}
    assert case["input"]["scope"] == "project"
    assert case["input"]["stored_scope_schema_version"] == 1
    assert case["expected"] == "accept-schema-2"
    parsed = manifest_module.parse_manifest(
        {"schema_version": case["input"]["skillfile_schema_version"], "sources": {}, "skills": []},
        Path("Skillfile.json"),
        scope="project",
    )
    assert parsed.schema_version == 2


register_semantic_driver(
    "project-schema2-without-profile-lock-accepted", _drive_project_schema2_acceptance
)


def _drive_current_refresh_endpoint(case: dict[str, Any]) -> None:
    """Drive the accepted current-endpoint case through CLI upgrade."""

    case_input = case["input"]
    assert case_input["operation"] == "explicit-refresh"
    assert case_input["existing_checkout"] is True
    assert case_input["transport_revision"] == 2
    assert case_input["repository"] == "example.org/kit"
    assert case_input["stored_origin"] == "https://old-mirror.example.net/kit.git"
    assert case_input["current_endpoint"] == "https://example.org/kit.git"
    assert case_input["current_alias"] == {
        "host": "current-mirror.example.net",
        "port": 8443,
        "authentication": "team-https",
    }
    assert case_input["current_mirror_of"] == case_input["repository"]
    assert case["expected"] == (
        "fetch-current-resolved-endpoint-current-mirror.example.net:8443;"
        "never-fetch-stored-origin;verify-locked-content;"
        "replace-lock-atomically-on-success"
    )
    _invoke_source_closure_test(
        "test_v2_refresh_current_endpoint_existing_checkout_uses_alias_and_replaces_lock"
    )


register_semantic_driver(
    "v2-refresh-current-endpoint-existing-checkout", _drive_current_refresh_endpoint
)


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
    if case["id"] == "attestation-evidence-revoked-identity-commit-advisory":
        assert case["input"]["revocation_match"] == (
            "canonical-repository-and-commit-only;name-and-content-hash-differ"
        )
        mutations["revoked"] = {
            "name": "different-skill-name",
            "context_sha256": _MARKER_V5_CONTEXT_OTHER,
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
    "attestation-evidence-revoked-identity-commit-advisory",
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
        == "valid-fresh-exact-name-repository-commit-raw-package-tree-hash"
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
            json.dumps(
                _marker_v5_evidence_payload(context_sha256=_MARKER_V5_CONTENT)
            ).encode("utf-8")
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
    """Drive ``missing-audit-report``: an unreadable report rejects.

    Re-pointed by TASK-260916-2je9f6: ``validate_source_audit`` has
    zero production callers. ``validate_stored_report`` is the live
    core it wraps (reached from production via
    ``source_audit_plan_hook``) and raises the same
    ``source_audit_report_unreadable`` for the denied read.
    """

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
        source_audit_module.record_source_audit(
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
                source_audit_module.validate_stored_report(
                    package=package,
                    content_sha256=content,
                    csk_home=csk_home,
                    policy=policy,
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
        pytest.skip(_descriptor_traversal_unavailable_reason())
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
        pytest.skip(_descriptor_traversal_unavailable_reason())
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
        pytest.skip(_descriptor_traversal_unavailable_reason())
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


# Corpus-closure instruments registered by TASK-260916-2je9f6.
#
# The hollow-driver gate lives in the semantic dispatch above: a driver
# that returns without calling any production entry point fails naming
# its case and owner. The tests below prove the gate fires (a
# deliberately hollow driver and an error-constructing driver each fail
# under every registered case id), prove the observer is not blind (a
# genuine entry call is recorded), prove the table's membership is
# derived (every entry resolves to a production call site), prove a
# setup-phase skip records as skipped (hook unit plus an end-to-end
# probe), and report passed/skipped/failed/total per category into the
# junit artifact. The category report must stay the last test in this
# file so the collector has seen every other test of the module.


def _deliberately_hollow_driver(case: dict[str, Any]) -> None:
    """Reproduce the expected label without touching production code.

    Positive control for the hollow-driver gate: it returns normally, so
    a label-only green would count it, while never reaching a production
    entry point.
    """

    assert isinstance(case["expected"], str)


def _error_constructing_hollow_driver(case: dict[str, Any]) -> None:
    """Construct the expected error without touching production code.

    Second positive control for the hollow-driver gate: it builds the
    expected ``SourceError`` itself and asserts its code, so a
    label-only green would count it, while never reaching a production
    entry point. Error and data classes are fixture plumbing, not
    entries; a table widened with ``SourceError`` lets this driver
    through, which is exactly the M3 mutant this family kills.
    """

    constructed = source_errors.SourceError(
        case["expected"], "constructed by the probe, never raised by production"
    )
    assert constructed.code == case["expected"]


def test_draft_sources_hollow_driver_is_caught_by_name(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """A hollow driver fails the semantic entry under every case id.

    For each registered case the real driver is temporarily replaced by
    each hollow probe in turn (pop-then-restore keeps the registry
    identical in every state) and the actual semantic entry must fail
    naming that case. Covering every id kills the narrowing mutant that
    admits a hollow driver for exactly one case; the error-constructing
    probe kills the table-widening mutant that admits ``SourceError``
    as an entry.
    """
    for case in SEMANTIC_CASES:
        real = SEMANTIC_DRIVERS.pop(case["id"])
        try:
            for probe in (
                _deliberately_hollow_driver,
                _error_constructing_hollow_driver,
            ):
                register_semantic_driver(case["id"], probe)
                try:
                    with pytest.raises(
                        pytest.fail.Exception,
                        match=f"hollow driver for {case['id']}",
                    ):
                        test_draft_sources_semantic_case(case, capsys)
                finally:
                    del SEMANTIC_DRIVERS[case["id"]]
        finally:
            SEMANTIC_DRIVERS[case["id"]] = real


def test_draft_sources_entry_observation_sees_a_real_production_call() -> None:
    """The observer records a genuine production entry call.

    Guards the hollow-driver gate against blindness: if the observer
    missed real calls, every driven case would fail as hollow.
    """
    observed = _observe_production_entries()
    with observed as sites:
        repository_policy.parse_policy({"schema_version": 1, "repositories": {}})
    assert "csk.sources.repository_policy:parse_policy" in sites


def test_draft_sources_accounting_flags_a_dropped_outcome() -> None:
    """The accounting helper fails closed on a collector gap.

    A collector that silently dropped one test would print category
    counts that do not add up to the run; the helper must name the
    dropped test instead. Dropping exactly one kills the narrowing
    mutant that tolerates a single missing outcome.
    """
    collected = [
        "tests/test_draft_sources_conformance.py::test_draft_sources_schema_case[one]",
        "tests/test_draft_sources_conformance.py::test_draft_sources_category_counts_recorded_in_junit",
    ]
    with pytest.raises(AssertionError, match="outcome collector missed 1"):
        _check_outcome_accounting(collected, {}, collected[1])


def _report_table_observation(
    tag: str,
    table: tuple[tuple[str, str], ...],
    code_counts: Mapping[int, _ObservedCodeCount],
    code_labels: Mapping[int, _ObservedCodeLabels],
    root: tuple[str, str],
) -> None:
    """Print the per-entry execution report for one lane record."""
    for module_name, attribute in table:
        code = _entry_code_object(module_name, attribute)
        count_record = code_counts.get(id(code)) if code is not None else None
        count = (
            count_record[1]
            if count_record is not None and count_record[0] is code
            else 0
        )
        if count:
            assert code is not None
            labels_record = code_labels.get(id(code))
            assert labels_record is not None and labels_record[0] is code
            scenarios = ",".join(sorted(labels_record[1]))
            print(
                f"draft-sources table {module_name}:{attribute} <- observed "
                f"x{count} in [{scenarios}] from {root[0]}:{root[1]} [{tag}]"
            )
        else:
            print(
                f"draft-sources table {module_name}:{attribute} <- UNOBSERVED "
                f"[{tag}]"
            )


def test_draft_sources_production_table_entries_have_production_callers() -> None:
    """Every tabled entry executed during the traced CLI scenarios.

    The allowlist is the hollow gate's whole definition of a
    production entry point, so its membership is observed, not
    typed: each entry must have executed while the product's own
    install, upgrade, status and check paths ran under the call
    tracer (see ``_observed_scenario_entries``). A tabled entry
    with no execution fails here naming every unobserved entry,
    instead of quietly vouching for drivers that execute
    production-uncalled code. Where the product refuses a scenario
    by design, entries covered only by refused scenarios are
    excluded by recorded coverage (see ``_check_table_observed``),
    and every other entry must still have executed. With
    ``CSK_SIMULATE_NO_EXTERNAL_BUILDS=1`` on a capable host the
    added refused record is certified as well, while the native
    capable record stays pinned — simulation adds evidence without
    removing any. Planting one dead entry kills this test (M4); a
    mutant that exempts one planted entry from the check dies in
    the laundering-shape test (M-new); a production mutant that
    removes a live call dies here behaviorally, when its scenario
    fails (M5).
    """
    observed, labels, root, _, refused = _observed_scenario_entries()
    _report_table_observation(
        "native", _PRODUCTION_ENTRY_POINTS, observed, labels, root
    )
    _check_table_observed(_PRODUCTION_ENTRY_POINTS, observed, refused)
    simulated = _simulated_refused_entries()
    if simulated is not None:
        sim_observed, sim_labels, sim_root, _, sim_refused = simulated
        _report_table_observation(
            "simulated-refused",
            _PRODUCTION_ENTRY_POINTS,
            sim_observed,
            sim_labels,
            sim_root,
        )
        _check_table_observed(_PRODUCTION_ENTRY_POINTS, sim_observed, sim_refused)


_LAUNDERING_SHAPES = (
    "dead",
    "direct-test-only",
    "forwarder",
    "uncalled-nested",
    "reference-as-data",
)


@contextlib.contextmanager
def _plant_laundering_shape(
    tmp_path: Path, shape: str
) -> Iterator[tuple[str, str]]:
    """Materialize one laundering shape; yield the planted table entry.

    Every shape plants a REAL, importable, callable function the
    traced scenarios never execute: ``dead`` is the production dead
    entry itself; ``direct-test-only`` forwards to it from a
    test-only caller; ``forwarder`` forwards from its own module on
    ``sys.path`` (removed afterwards); ``uncalled-nested`` hides the
    call in a nested body; ``reference-as-data`` holds the function
    object without calling it. Each shape exhibits the trigger that
    laundered one round of the static gate; the observed gate must
    reject every one of them as never executed.
    """
    if shape == "dead":
        yield ("csk.sources.boundaries", "check_selected_package")
        return
    if shape == "direct-test-only":
        yield (__name__, "_review_probe_test_only_caller")
        return
    if shape == "forwarder":
        (tmp_path / "review_only_forwarder.py").write_text(
            "from csk.sources.boundaries import check_selected_package\n"
            "def test_only_forwarder(*args, **kwargs):\n"
            "    return check_selected_package(*args, **kwargs)\n",
            encoding="utf-8",
        )
        importlib.invalidate_caches()
        sys.path.insert(0, os.fspath(tmp_path))
        try:
            module = importlib.import_module("review_only_forwarder")
            assert callable(module.test_only_forwarder)
            yield ("review_only_forwarder", "test_only_forwarder")
        finally:
            sys.path.remove(os.fspath(tmp_path))
            sys.modules.pop("review_only_forwarder", None)
        return
    if shape == "uncalled-nested":
        yield (__name__, "_review_probe_outer_with_uncalled_nested")
        return
    if shape == "reference-as-data":
        yield (__name__, "_review_probe_reference_holder")
        return
    raise AssertionError(f"unknown laundering shape: {shape!r}")


def _assert_table_rejected_as_never_executed(
    table: tuple[tuple[str, str], ...],
    observed: Mapping[int, _ObservedCodeCount],
    refused: Collection[str],
    site: str,
) -> None:
    """Require rejection from the never-executed branch, naming ``site``.

    Matching the entry name alone cannot tell the unresolvable
    rejection from the never-executed one, so a control proving
    "rejected as unexecuted" must pin the branch (F-3 /
    laundering-control-wrong-branch). The unresolvable rejection is
    pinned from the other side by
    ``test_draft_sources_unresolvable_entry_rejected_as_unresolvable``.
    """
    with pytest.raises(AssertionError) as excinfo:
        _check_table_observed(table, observed, refused)
    message = str(excinfo.value)
    assert "never executed" in message, (
        f"expected the never-executed rejection for {site}, got: {message}"
    )
    assert site in message, (
        f"expected the rejection to name {site}, got: {message}"
    )
    assert "naming no production callable" not in message, (
        f"rejection for {site} came from the unresolvable branch: {message}"
    )


@pytest.mark.parametrize("shape", _LAUNDERING_SHAPES)
def test_draft_sources_test_only_shapes_cannot_launder_a_dead_entry(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, shape: str
) -> None:
    """No laundering shape certifies an unexecuted table entry (the CLASS test).

    For each shape — a dead production entry, a real test-only
    caller, a real forwarder in its own module, a real outer
    function with an uncalled nested dead-entry call, a real
    function holding the dead entry as data — the planted entry is
    importable and callable yet never executed by the traced
    scenarios, so the membership check must reject it from the
    never-executed branch, by name. The planted entry's
    resolvability is asserted first: without it the control could
    silently degrade into asserting the unresolvable branch (F-3 /
    laundering-control-wrong-branch). Under observation every shape
    collapses to the same fact (never executed, never certified);
    that is the design working, not a weaker test. A mutant that
    exempts one planted entry dies on that entry's param (M-new).
    The planted entry has no recorded coverage, so it is required —
    never excluded — on every lane, refused ones included; with
    ``CSK_SIMULATE_NO_EXTERNAL_BUILDS=1`` on a capable host the
    added refused record is checked as well.
    """
    observed, _, _, _, refused = _observed_scenario_entries()
    records = [(observed, refused)]
    simulated = _simulated_refused_entries()
    if simulated is not None:
        records.append((simulated[0], simulated[4]))
    module = sys.modules[__name__]
    with _plant_laundering_shape(tmp_path, shape) as planted:
        site = f"{planted[0]}:{planted[1]}"
        assert _entry_code_object(*planted) is not None, (
            f"laundering probe {site} names no resolvable callable; "
            "the control must reject it as never executed, not as "
            "unresolvable"
        )
        monkeypatch.setattr(
            module,
            "_PRODUCTION_ENTRY_POINTS",
            _PRODUCTION_ENTRY_POINTS + (planted,),
        )
        for rec_observed, rec_refused in records:
            _assert_table_rejected_as_never_executed(
                _PRODUCTION_ENTRY_POINTS, rec_observed, rec_refused, site
            )


def test_draft_sources_unresolvable_entry_rejected_as_unresolvable() -> None:
    """An unresolvable entry fires the unresolvable branch, never the other one.

    F-3 / laundering-control-wrong-branch, pinned from the other
    side: a tabled entry naming no production callable must be
    rejected as unresolvable, so a removed callable can never
    satisfy the never-executed branch the laundering CLASS test
    requires. A mutant that drops the resolvability assert lets the
    entry fall through to the never-executed branch and dies here
    (M-f3-narrow).
    """
    observed, _, _, _, refused = _observed_scenario_entries()
    table = _PRODUCTION_ENTRY_POINTS + (
        ("csk.sources.boundaries", "no_such_entry_probe"),
    )
    with pytest.raises(AssertionError) as excinfo:
        _check_table_observed(table, observed, refused)
    message = str(excinfo.value)
    assert "naming no production callable" in message, (
        f"expected the unresolvable rejection, got: {message}"
    )
    assert "no_such_entry_probe" in message, (
        f"expected the rejection to name the probe, got: {message}"
    )
    assert "never executed" not in message, (
        f"unresolvable entry was rejected as never executed: {message}"
    )


_COLLISION_SHAPES = (
    "nested-function",
    "method",
    "nested-in-method",
    "cross-file-byte-identical-clone",
)

_COLLISION_SOURCES: dict[str, str] = {
    "nested-function": (
        'def target():\n'
        '    raise AssertionError("module-level target must never run")\n'
        "\n"
        "\n"
        "def outer():\n"
        "    def target():\n"
        '        return "nested"\n'
        "    return target()\n"
    ),
    "method": (
        'def target():\n'
        '    raise AssertionError("module-level target must never run")\n'
        "\n"
        "\n"
        "class Holder:\n"
        "    def target(self):\n"
        '        return "method"\n'
    ),
    "nested-in-method": (
        'def target():\n'
        '    raise AssertionError("module-level target must never run")\n'
        "\n"
        "\n"
        "class Holder:\n"
        "    def run(self):\n"
        "        def target():\n"
        '            return "nested-in-method"\n'
        "        return target()\n"
    ),
    "cross-file-byte-identical-clone": (
        "def target(value):\n"
        "    return value + 1\n"
    ),
}


@pytest.mark.parametrize("shape", _COLLISION_SHAPES)
def test_draft_sources_same_name_code_cannot_certify_an_unexecuted_entry(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    shape: str,
) -> None:
    """Executing a same-named code object certifies nothing (F-2 CLASS test).

    For each collision shape — a nested function, a method, a
    function nested in a method, or a byte-identical clone loaded
    from another file — the colliding code executes while the
    tabled module-level ``target`` does not. The clone parameter
    proves the code objects are distinct but value-equal, the case
    a ``dict[CodeType, ...]`` misses. The membership check must
    reject the entry from the never-executed branch. The recorder
    is asserted non-blind (a silent tracer would pass vacuously).
    A narrowing mutant that restores value-keyed records dies on
    the cross-file clone parameter specifically (M-f2-narrow).
    """
    module_name = f"collision_probe_{shape.replace('-', '_')}"
    if shape == "cross-file-byte-identical-clone":
        source_root = tmp_path / "src"
        package = source_root / module_name
        package.mkdir(parents=True)
        (package / "__init__.py").write_text("", encoding="utf-8")
        source = _COLLISION_SOURCES[shape]
        (package / "dead.py").write_text(source, encoding="utf-8")
        (package / "live.py").write_text(source, encoding="utf-8")
        dead_module_name = f"{module_name}.dead"
        live_module_name = f"{module_name}.live"
        entry = (dead_module_name, "target")
    else:
        source_root = tmp_path
        (source_root / f"{module_name}.py").write_text(
            _COLLISION_SOURCES[shape], encoding="utf-8"
        )
        dead_module_name = module_name
        live_module_name = module_name
        entry = (module_name, "target")
    importlib.invalidate_caches()
    sys.path.insert(0, os.fspath(source_root))
    try:
        module = importlib.import_module(dead_module_name)
        live_module = importlib.import_module(live_module_name)
        assert _entry_code_object(*entry) is not None, (
            f"collision probe {entry[0]}:{entry[1]} is unresolvable; "
            "the control must reject it as never executed, not as "
            "unresolvable"
        )
        recorder = _ProductionCallRecorder(source_root)
        if shape == "cross-file-byte-identical-clone":
            dead_code = module.target.__code__
            live_code = live_module.target.__code__
            assert dead_code is not live_code
            assert dead_code == live_code, (
                "the clone control must reproduce CodeType value equality"
            )
            assert recorder.run("collision", live_module.target, 41) == 42
            live_entry = (live_module_name, "target")
            assert _project_table_labels(
                (entry, live_entry), recorder.labels
            ) == {live_entry: {"collision"}}
        elif shape == "nested-function":
            assert recorder.run("collision", module.outer) == "nested"
        elif shape == "method":
            assert recorder.run("collision", module.Holder().target) == "method"
        elif shape == "nested-in-method":
            assert recorder.run("collision", module.Holder().run) == (
                "nested-in-method"
            )
        else:
            raise AssertionError(f"unknown collision shape: {shape!r}")
        assert recorder.counts, (
            "the tracer recorded no calls: the namesake ran (its "
            "return value proves it), so an empty record means a "
            "blind tracer, not a rejected entry"
        )
        _assert_table_rejected_as_never_executed(
            (entry,), recorder.counts, (), f"{entry[0]}:{entry[1]}"
        )
        if shape == "cross-file-byte-identical-clone":
            live_entry = (live_module_name, "target")
            _report_table_observation(
                "clone-probe",
                (entry, live_entry),
                recorder.counts,
                recorder.labels,
                (module_name, "target"),
            )
            report = capsys.readouterr().out
            assert f"{entry[0]}:{entry[1]} <- UNOBSERVED" in report
            assert f"{live_entry[0]}:{live_entry[1]} <- observed" in report
    finally:
        sys.path.remove(os.fspath(source_root))
        for imported in (dead_module_name, live_module_name, module_name):
            sys.modules.pop(imported, None)


def test_draft_sources_observed_labels_agree_with_live_record() -> None:
    """The recorded coverage matches the live traced record, exactly.

    On a capable host the live entry set, label sets and scenario
    universe must equal the recorded ones (entry sets alone are not
    enough; see the drift test below), which is what keeps the
    exclusion derivation honest: the refused lane trusts this record,
    and this test re-proves it on every capable run. With
    ``CSK_REGENERATE_OBSERVED_LABELS=1`` the test writes the record
    from the live trace instead (capable host, single worker, then
    rerun without the variable to verify); on a host without support
    it skips with the declared platform reason, since a partial
    record cannot re-approve the whole. Simulation never skips it:
    the simulated lane is added beside the pinned native run, and
    the skip is derived from the real platform, not the predicate.
    """
    if os.environ.get(REGENERATE_OBSERVED_LABELS_ENV) == "1":
        if not _selection_fs.supports_descriptor_traversal():
            pytest.fail(
                "cannot regenerate observed labels without "
                "descriptor-relative traversal"
            )
        if not _HOST_SUPPORTS_EXTERNAL_BUILDS:
            pytest.fail(
                "cannot regenerate observed labels on a host without "
                "external-build support; rerun on a capable host"
            )
        _, code_labels, _, ran, refused = _observed_scenario_entries(pin=False)
        _write_observed_labels(
            OBSERVED_LABELS_PATH,
            _PRODUCTION_ENTRY_POINTS,
            _project_table_labels(_PRODUCTION_ENTRY_POINTS, code_labels),
            set(ran) | set(refused),
        )
        print(
            "draft-sources observed labels regenerated at "
            f"{OBSERVED_LABELS_PATH}; rerun without "
            f"{REGENERATE_OBSERVED_LABELS_ENV} to verify"
        )
        return
    if not _HOST_SUPPORTS_EXTERNAL_BUILDS:
        pytest.skip(_external_builds_unavailable_reason())
    _observed_scenario_entries()
    print("draft-sources observed labels agree with the live record")


def test_draft_sources_simulation_cannot_disarm_the_capable_pin(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Blinding the predicate never removes the capable-lane pin (F-1 test).

    With ``CI=true`` and ``CSK_SIMULATE_NO_EXTERNAL_BUILDS=1`` set
    and the support predicate forced false — the old fixture's
    blinding mechanism, now inert — a wrong golden must still fail
    the capable-lane pin against the native record. The pin
    obligation is derived from the real platform
    (``_HOST_SUPPORTS_EXTERNAL_BUILDS``), never from the live
    predicate. Skips on a host without support, with the declared
    platform reason: there is no capable pin to disarm. A mutant
    that re-adds the live predicate to the pin condition admits
    exactly the blinded state and dies here (M-f1-narrow).
    """
    if not _HOST_SUPPORTS_EXTERNAL_BUILDS:
        pytest.skip(_external_builds_unavailable_reason())
    _, live_labels, _, ran, refused = _observed_scenario_entries()
    golden_entries, golden_scenarios = _load_observed_labels()
    candidates = [
        site for site, labels in golden_entries.items() if len(labels) > 1
    ]
    assert candidates, (
        "the recorded coverage names no multi-scenario entry; the "
        "wrong-golden probe needs one"
    )
    victim = candidates[0]
    wrong = {
        "entries": {
            site: (["external-build"] if site == victim else list(labels))
            for site, labels in golden_entries.items()
        },
        "scenarios": list(golden_scenarios),
    }
    wrong_path = tmp_path / "wrong_observed_labels.json"
    wrong_path.write_text(json.dumps(wrong), encoding="utf-8")
    monkeypatch.setenv("CI", "true")
    monkeypatch.setenv(SIMULATE_NO_EXTERNAL_BUILDS_ENV, "1")
    monkeypatch.setattr(installer, "supports_external_builds", lambda: False)
    monkeypatch.setattr(
        sys.modules[__name__], "OBSERVED_LABELS_PATH", wrong_path
    )
    with pytest.raises(AssertionError) as excinfo:
        _pin_native_labels_against_golden(live_labels, ran=ran, refused=refused)
    assert victim in str(excinfo.value), (
        f"expected the pin to name the narrowed entry {victim}, got: "
        f"{excinfo.value}"
    )


def test_draft_sources_regenerate_environment_does_not_disarm_capable_pin(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Only the golden-writing test may bypass the capable pin.

    A filtered run can set the regeneration variable without running
    the writer. Even then, every other capable-host test must keep its
    pin. This narrows the wrong golden to one label for a multi-label
    entry, sets the ambient variable, and requires the pin to reject
    that exact entry. A mutant that consults the variable in
    ``_capable_pin_required`` dies here (M-f1-regen-narrow).
    """
    if not _HOST_SUPPORTS_EXTERNAL_BUILDS:
        pytest.skip(_external_builds_unavailable_reason())
    monkeypatch.setenv(REGENERATE_OBSERVED_LABELS_ENV, "1")
    _, live_labels, _, ran, refused = _observed_scenario_entries()
    golden_entries, golden_scenarios = _load_observed_labels()
    candidates = [
        (site, labels)
        for site, labels in golden_entries.items()
        if len(labels) > 1
    ]
    assert candidates, (
        "the recorded coverage names no multi-scenario entry; the "
        "wrong-golden probe needs one"
    )
    victim, labels = candidates[0]
    wrong = {
        "entries": {
            site: (list(labels[:1]) if site == victim else list(site_labels))
            for site, site_labels in golden_entries.items()
        },
        "scenarios": list(golden_scenarios),
    }
    wrong_path = tmp_path / "wrong_observed_labels.json"
    wrong_path.write_text(json.dumps(wrong), encoding="utf-8")
    monkeypatch.setattr(
        sys.modules[__name__], "OBSERVED_LABELS_PATH", wrong_path
    )
    with pytest.raises(AssertionError) as excinfo:
        _pin_native_labels_against_golden(live_labels, ran=ran, refused=refused)
    assert victim in str(excinfo.value), (
        f"expected the pin to name the narrowed entry {victim}, got: "
        f"{excinfo.value}"
    )


def test_draft_sources_label_agreement_rejects_label_drift() -> None:
    """The agreement compares label sets, not just entry sets.

    Drops every label but one from a multi-scenario entry and
    requires the agreement to fail naming it: a mutant that compares
    entry sets only (or tolerates one dropped label) admits the
    drifted record and dies here. Pure — no scenarios run — so it
    holds on every lane, refused ones included.
    """
    golden_entries, golden_scenarios = _load_observed_labels()
    candidates = [
        (module_name, attribute)
        for module_name, attribute in _PRODUCTION_ENTRY_POINTS
        if len(golden_entries[f"{module_name}:{attribute}"]) > 1
    ]
    assert candidates, (
        "the recorded coverage names no multi-scenario entry; the "
        "drift probe needs one"
    )
    victim = candidates[0]
    live = {
        (module_name, attribute): set(
            golden_entries[f"{module_name}:{attribute}"]
        )
        for module_name, attribute in _PRODUCTION_ENTRY_POINTS
    }
    assert len(live[victim]) > 1
    live[victim] = {sorted(live[victim])[0]}
    with pytest.raises(AssertionError) as excinfo:
        _assert_labels_agree(
            _PRODUCTION_ENTRY_POINTS,
            live,
            golden_entries,
            ran=set(golden_scenarios),
            refused=set(),
            golden_scenarios=golden_scenarios,
        )
    assert f"{victim[0]}:{victim[1]}" in str(excinfo.value)


def test_draft_sources_platform_bound_skip_reasons_name_platform() -> None:
    """Every declared platform skip names its runtime and required capability."""
    descriptor_reason = _descriptor_traversal_unavailable_reason()
    assert f"sys.platform={sys.platform}" in descriptor_reason
    assert "O_DIRECTORY" in descriptor_reason and "dir_fd" in descriptor_reason
    case_reason = _case_alias_unavailable_reason()
    assert f"sys.platform={sys.platform}" in case_reason
    assert "case-alias" in case_reason and "case-insensitive filesystem" in case_reason


def test_draft_sources_external_build_support_agrees_with_product(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The support predicate and the product's refusal agree both ways.

    The native record always matches the real platform, and the
    natural predicate value must match the cached native record
    (supported ran, or refused); forcing the predicate false on an
    identical fresh fixture must produce the structured refusal
    (exit 1, class, product text) — the same forcing the refused
    lane applies, scoped to its own run. Simulation adds a refused
    record beside the native one; this agreement still compares the
    native record. A product change that refuses where the predicate
    admits (or admits where it refuses) fails here rather than
    certifying the wrong lane.
    """
    if not _selection_fs.supports_descriptor_traversal():
        pytest.skip(_descriptor_traversal_unavailable_reason())
    natural = installer.supports_external_builds()
    _, _, _, ran, refused = _observed_scenario_entries()
    assert ("external-build" in ran) == natural, (
        f"predicate says supported={natural} but the cached record ran "
        f"{sorted(ran)} / refused {sorted(refused)}"
    )
    assert ("external-build" in refused) == (not natural), (
        f"predicate says supported={natural} but the cached record ran "
        f"{sorted(ran)} / refused {sorted(refused)}"
    )
    monkeypatch.setattr(installer, "supports_external_builds", lambda: False)
    repo_root = Path(__file__).parents[1]
    _, _, main = _derive_console_root(repo_root)
    recorder = _ProductionCallRecorder(repo_root / "src")
    _observed_scenario_external_build_refused(tmp_path / "probe", recorder, main)


def test_draft_sources_console_root_matches_the_installed_entry_point(
    tmp_path: Path,
) -> None:
    """The traced root is the manifest's console-script target.

    Positive control: on the real tree the derivation returns the
    installed entry callable, and on fixture manifests the outcome
    follows the content — a missing manifest, a manifest without a
    ``csk`` script, a target naming no importable module, and a
    target naming no callable all fail loudly — so the gate cannot
    trace a stale or hardcoded entry while the manifest says
    otherwise.
    """
    repo_root = Path(__file__).parents[1]
    module_name, attr, main = _derive_console_root(repo_root)
    assert callable(main)
    assert getattr(importlib.import_module(module_name), attr) is main
    with pytest.raises(AssertionError, match="console-script manifest is missing"):
        _derive_console_root(tmp_path / "absent")
    (tmp_path / "pyproject.toml").write_text(
        "[project]\nname = \"fixture\"\n", encoding="utf-8"
    )
    with pytest.raises(AssertionError, match="names no csk entry point"):
        _derive_console_root(tmp_path)
    (tmp_path / "pyproject.toml").write_text(
        "[project]\nname = \"fixture\"\n[project.scripts]\n"
        'csk = "csk.nonexistent:main"\n',
        encoding="utf-8",
    )
    with pytest.raises(ImportError):
        _derive_console_root(tmp_path)
    (tmp_path / "pyproject.toml").write_text(
        "[project]\nname = \"fixture\"\n[project.scripts]\n"
        'csk = "csk.cli:nonexistent_entry"\n',
        encoding="utf-8",
    )
    with pytest.raises(AssertionError, match="names no callable"):
        _derive_console_root(tmp_path)


def test_draft_sources_setup_phase_skip_is_recorded_as_skipped() -> None:
    """The accounting hook records a setup-phase skip as skipped.

    Drives the production call site (``pytest_runtest_logreport`` in
    ``tests/conftest.py``) with a real setup-phase skip report and
    asserts the outcome records as ``skipped``. A skip marker or a
    fixture-level skip never reaches the call phase, so without the
    setup branch the category counts would report the test as a
    dropped outcome instead of a skip. The node id is synthetic: the
    completeness check only requires collected tests, so the extra
    recording cannot disturb the real tallies.
    """
    from _pytest.reports import TestReport

    from conftest import pytest_runtest_logreport

    nodeid = (
        "tests/test_draft_sources_conformance.py"
        "::test_draft_sources_hook_unit_probe"
    )
    _HOOK_OUTCOMES.pop(nodeid, None)
    report = TestReport(
        nodeid=nodeid,
        location=("tests/test_draft_sources_conformance.py", 0, "hook unit probe"),
        keywords={},
        outcome="skipped",
        longrepr=(str(Path("tests/test_draft_sources_conformance.py")), 0, "unit probe"),
        when="setup",
    )
    pytest_runtest_logreport(report)
    assert _HOOK_OUTCOMES.get(nodeid) == "skipped", (
        f"setup-phase skip for {nodeid} recorded as "
        f"{_HOOK_OUTCOMES.get(nodeid)!r}, not skipped"
    )


@pytest.mark.skip(
    reason="accounting probe: a setup-phase skip must appear in the junit counts"
)
def test_draft_sources_setup_phase_skip_probe() -> None:
    """Prove a setup-phase skip reaches the category counts.

    The skip marker raises in the setup phase, so the body never runs
    (it fails if it does). The category-counts test below asserts this
    node id recorded as skipped: end-to-end proof that a driver that
    skips in setup appears in the counts rather than tripping the
    dropped-outcome guard.
    """
    pytest.fail("setup-skip probe body must not run")


def _assert_all_categories_collected(
    tallies: Mapping[str, Mapping[str, int]],
) -> None:
    """Fail when a report category collected no tests.

    The junit artifact carries per-category passed/skipped/failed/total
    for every bucket in ``_REPORT_CATEGORIES``; a bucket with zero
    collected tests means a whole parametrization (or the harness
    itself) went missing, and recording zeros for it would certify an
    artifact that silently omits a category. Every full-module run with
    the suite root set collects all four buckets (the corpus
    inventories pin 115/3/94; the harness bucket holds this module's
    own tests), so an empty bucket is either a collection regression
    or a partial (``-k``) run, and both must fail loudly here rather
    than print partial counts. A bucket absent from the mapping fails
    with ``KeyError`` naming it; an empty bucket with ``AssertionError``.
    """
    for category in _REPORT_CATEGORIES:
        tally = tallies[category]
        assert tally["total"] > 0, (
            f"report category {category!r} collected no tests: "
            "the junit per-category counts require a full-module run "
            "(no -k filter); an empty bucket is a collection regression"
        )


def _full_category_tallies() -> dict[str, dict[str, int]]:
    """Return tallies with every report bucket non-empty."""
    return {
        bucket: {"passed": 1, "skipped": 0, "failed": 0, "total": 1}
        for bucket in _REPORT_CATEGORIES
    }


@pytest.mark.parametrize("category", _REPORT_CATEGORIES)
def test_draft_sources_category_presence_assertion_fails_on_a_missing_category(
    category: str,
) -> None:
    """An empty report bucket fails the presence assertion naming it.

    CLASS test over the four report buckets: each param empties exactly
    one bucket (the silently-missing-category shape the junit counts
    must refuse) and the helper must raise naming that bucket. A mutant
    that admits exactly one empty bucket dies on that bucket's param
    (M8); the other params still pass under it.
    """
    tallies = _full_category_tallies()
    tallies[category] = {"passed": 0, "skipped": 0, "failed": 0, "total": 0}
    with pytest.raises(
        AssertionError, match=f"category '{category}' collected no tests"
    ):
        _assert_all_categories_collected(tallies)


def test_draft_sources_category_presence_assertion_passes_when_every_category_is_collected() -> None:
    """The presence assertion accepts fully collected tallies.

    Positive control for the missing-category gate: all four buckets
    non-empty must not raise, so the gate cannot refuse a healthy
    full-module run.
    """
    _assert_all_categories_collected(_full_category_tallies())


def test_draft_sources_category_counts_recorded_in_junit(
    request: pytest.FixtureRequest, record_property: Callable[[str, object], None]
) -> None:
    """Report passed/skipped/failed/total per category into junit.

    Must stay the last test in this file: it checks the autouse
    collector saw every other collected test of this module exactly once
    (which requires module-coherent scheduling, one worker for the whole
    module, as in CI and with --dist=loadfile), fails when any of the
    four categories collected no tests, and records the tallies
    as junit properties on this test case, so the junit artifact carries
    the per-category counts for the schema, snapshot, semantic and
    harness buckets.
    """
    collected = [
        item.nodeid
        for item in request.session.items
        if item.nodeid.split("::")[0].endswith("test_draft_sources_conformance.py")
    ]
    probe = (
        "tests/test_draft_sources_conformance.py"
        "::test_draft_sources_setup_phase_skip_probe"
    )
    assert probe in collected, "setup-skip probe was not collected"
    assert _HOOK_OUTCOMES.get(probe) == "skipped", (
        f"setup-phase skip probe recorded as {_HOOK_OUTCOMES.get(probe)!r}, "
        "not skipped; a driver that skips in setup must appear in the counts"
    )
    tallies = _check_outcome_accounting(
        collected, _HOOK_OUTCOMES, request.node.nodeid
    )
    _assert_all_categories_collected(tallies)
    for category in _REPORT_CATEGORIES:
        tally = tallies[category]
        record_property(f"draft_sources_{category}_passed", str(tally["passed"]))
        record_property(f"draft_sources_{category}_skipped", str(tally["skipped"]))
        record_property(f"draft_sources_{category}_failed", str(tally["failed"]))
        record_property(f"draft_sources_{category}_total", str(tally["total"]))
        print(
            f"draft-sources {category}: "
            f"{tally['passed']}/{tally['skipped']}/{tally['failed']}/"
            f"{tally['total']} passed/skipped/failed/total"
        )
    for case_id in sorted(CASE_CALL_SITES):
        print(f"draft-sources call sites for {case_id}: {', '.join(CASE_CALL_SITES[case_id])}")
