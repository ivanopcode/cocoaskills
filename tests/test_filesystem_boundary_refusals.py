"""BOUNDARY family: no raw OSError escapes a public entry point.

Completeness is over entry points, not seams. Entry points are derived
from the CLI command table (parser choices at runtime) and the modules'
exported API (inspect); the pin tests fail when a command or public
function is added. Beneath each covered entry point, faults are injected
broadly at the os/io layer (every os function via dir(os), plus
io.open/builtins.open and the below-swallow predicates) when any path
argument touches the marker, and the entry point must produce a
structured, domain-typed refusal.

Two driver shapes share the injector. The breadth drivers fire on the
first touching call with four errnos: the per-entry first-touch sample.
Every breadth pair additionally has an ordinal-sweep driver
(BUG-260921-hv5upg) failing the Nth touching call for N = 1..K across
the four-errno matrix, where K is the dry-run touch count for that
(entry point, marker), and asserting the pinned per-ordinal outcome
plus no firing at K+1, so a seam behind an already-guarded one gets
its own firing; a breadth pair without its sweep fails the linkage
test. The seam-level suite (test_filesystem_seam_refusals.py) stays as
a secondary aid: it pins each known seam's exact refusal. Bounds are
stated in ``fs_boundary_support``.
"""

from __future__ import annotations

import errno
import json
import os
from pathlib import Path
from types import ModuleType

import pytest
from conftest import make_config, make_project, make_skill_repo, write_skillfile
import fs_boundary_support
from fs_boundary_support import (
    Touch,
    broad_fault,
    count_touches,
    enumerate_cli_leaves,
    enumerate_public_api,
    is_runtime_path_fast_path,
)

from csk import cli, hybrid, manifest
from csk import config as csk_config
from csk.audit import trust as audit_trust
from csk.audit.model import Decision, TrustRecord, Verdict

ERRNO_CASES = [
    pytest.param(errno.EACCES, id="EACCES"),
    pytest.param(errno.EIO, id="EIO"),
    pytest.param(errno.ENAMETOOLONG, id="ENAMETOOLONG"),
    pytest.param(errno.ELOOP, id="ELOOP"),
]

_TRUST_HASH = "sha256:" + "ab" * 32

_POSIX_ONLY = pytest.mark.skipif(os.name == "nt", reason="POSIX permission bits required")
_NOT_ROOT = pytest.mark.skipif(
    hasattr(os, "geteuid") and os.geteuid() == 0, reason="root bypasses permission bits"
)


def _configured(tmp_path: Path, skills_root: Path, csk_home: Path, monkeypatch) -> Path:
    project = make_project(tmp_path)
    cfg = make_config(csk_home, skills_root, project)
    csk_config.save_config(cfg)
    monkeypatch.setenv("CSK_CONFIG", str(cfg.path))
    monkeypatch.delenv("CSK_SYSTEM_CONFIG", raising=False)
    return project


def _verdict(content_sha256: str) -> Verdict:
    return Verdict(
        schema_version=audit_trust.SCHEMA_VERSION,
        content_sha256=content_sha256,
        skill="skill-a",
        source="skill-a",
        commit="c0ffee",
        backend="null",
        model=None,
        cloud=False,
        prompt_version=audit_trust.PROMPT_VERSION,
        ruleset_version=audit_trust.RULESET_VERSION,
        canary_passed=True,
        findings=(),
        decision=Decision.ALLOW,
        ran_at="2026-09-18T00:00:00+00:00",
        trust=TrustRecord(),
    )


# ---------------------------------------------------------------------------
# Entry-point enumeration pins.
# ---------------------------------------------------------------------------

EXPECTED_CLI_DRAFT_TRUE: set[tuple[str, ...]] = {
    ("add",),
    ("audit",),
    ("bootstrap",),
    ("check",),
    ("config", "build-https", "add"),
    ("config", "build-https", "list"),
    ("config", "build-https", "login"),
    ("config", "build-https", "remove"),
    ("config", "build-ssh", "add"),
    ("config", "build-ssh", "list"),
    ("config", "build-ssh", "remove"),
    ("config", "show"),
    ("gc",),
    ("global", "add"),
    ("global", "init"),
    ("global", "install"),
    ("global", "list"),
    ("global", "remove"),
    ("global", "status"),
    ("global", "update"),
    ("global", "upgrade"),
    ("hybrid", "add"),
    ("hybrid", "list"),
    ("hybrid", "remove"),
    ("hybrid", "status"),
    ("init",),
    ("install",),
    ("list",),
    ("project", "add"),
    ("project", "resolve"),
    ("remove",),
    ("shell-init",),
    ("skill", "check"),
    ("status",),
    ("update",),
    ("upgrade",),
}

EXPECTED_CLI_DRAFT_FALSE: set[tuple[str, ...]] = EXPECTED_CLI_DRAFT_TRUE - {("check",)}

#: CLI leaves with a broad-fault boundary driver in this module. Each
#: reaches a fixed seam in a named module.
COVERED_CLI_LEAVES: set[tuple[str, ...]] = {
    ("add",),
    ("audit",),
    ("bootstrap",),
    ("config", "show"),
    ("hybrid", "status"),
    ("init",),
    ("list",),
    ("project", "add"),
    ("project", "resolve"),
    ("remove",),
    ("status",),
}

#: CLI leaves without a boundary driver, each with its reason. Every one
#: still reaches the draft-decision seam at startup (covered by the seam
#: suite's os.stat driver); the rest of its filesystem paths lie in
#: non-named modules, out of contract per AC (a) "in the named modules".
OUT_OF_SCOPE_CLI: dict[tuple[str, ...], str] = {
    ("check",): "draft schema-2 surface; manifest read covered via list/status",
    ("config", "build-https", "add"): "config load/save paths in non-named config.py",
    ("config", "build-https", "list"): "config load path in non-named config.py",
    ("config", "build-https", "login"): "config + keyring paths in non-named modules",
    ("config", "build-https", "remove"): "config load/save paths in non-named config.py",
    ("config", "build-ssh", "add"): "config load/save paths in non-named config.py",
    ("config", "build-ssh", "list"): "config load path in non-named config.py",
    ("config", "build-ssh", "remove"): "config load/save paths in non-named config.py",
    ("gc",): "runtime/snapshot/build-cache paths in non-named gc.py",
    ("global", "add"): "global decl paths in non-named hybrid/global_install",
    ("global", "init"): "global paths in non-named global_install",
    ("global", "install"): "installer paths non-named; trust via reviewer R3 probes",
    ("global", "list"): "global paths in non-named global_install",
    ("global", "remove"): "global decl paths in non-named hybrid/global_install",
    ("global", "status"): "global + installer paths in non-named modules",
    ("global", "update"): "git fetch paths in non-named installer/global_install",
    ("global", "upgrade"): "installer paths non-named; trust via reviewer R3 probes",
    ("hybrid", "add"): "hybrid decl paths in non-named hybrid.py",
    ("hybrid", "list"): "hybrid decl paths in non-named hybrid.py",
    ("hybrid", "remove"): "hybrid decl paths in non-named hybrid.py",
    ("install",): "installer paths non-named; trust via reviewer R3 probes",
    ("shell-init",): "hook print/install paths in non-named shell_init.py",
    ("skill", "check"): "skill validation paths in non-named skillcheck.py",
    ("update",): "git fetch paths in non-named installer",
    ("upgrade",): "installer paths non-named; trust via reviewer R3 probes",
}

EXPECTED_MANIFEST_API: set[str] = {
    "add_skill_decl",
    "ensure_empty_manifest",
    "ensure_project_manifest",
    "load_manifest",
    "manifest_path",
    "parse_manifest",
    "remove_skill_decl",
}

EXPECTED_TRUST_API: set[str] = {
    "finding_from_payload",
    "load_cached_verdict",
    "load_trust_record",
    "normalize_content_sha256",
    "pin_content_hash",
    "store_verdict",
    "trust_path",
    "verdict_path",
}

#: Public API with a broad-fault boundary driver (filesystem-touching).
COVERED_MANIFEST_API: set[str] = {
    "add_skill_decl",
    "ensure_empty_manifest",
    "ensure_project_manifest",
    "load_manifest",
    "remove_skill_decl",
}

COVERED_TRUST_API: set[str] = {
    "load_cached_verdict",
    "load_trust_record",
    "pin_content_hash",
    "store_verdict",
}

#: Public API without a driver: pure, no filesystem syscalls (verified by
#: reading each body: path joining, JSON-free parsing, name checks).
PURE_API: dict[str, str] = {
    "manifest.manifest_path": "pure path join",
    "manifest.parse_manifest": "pure dict parse, no I/O",
    "trust.finding_from_payload": "pure dict parse, no I/O (TASK-260921-qe62bu replay)",
    "trust.trust_path": "pure path join",
    "trust.verdict_path": "pure path join",
    "trust.normalize_content_sha256": "pure grammar check",
}


def test_entry_pins_cli_leaves():
    assert enumerate_cli_leaves(draft=True) == EXPECTED_CLI_DRAFT_TRUE
    assert enumerate_cli_leaves(draft=False) == EXPECTED_CLI_DRAFT_FALSE
    assert set(OUT_OF_SCOPE_CLI) | COVERED_CLI_LEAVES == EXPECTED_CLI_DRAFT_TRUE
    assert set(OUT_OF_SCOPE_CLI) & COVERED_CLI_LEAVES == set()


def test_entry_pins_public_api():
    assert enumerate_public_api(manifest) == EXPECTED_MANIFEST_API
    assert enumerate_public_api(audit_trust) == EXPECTED_TRUST_API
    assert COVERED_MANIFEST_API | {"manifest_path", "parse_manifest"} == EXPECTED_MANIFEST_API
    assert (
        COVERED_TRUST_API
        | {"finding_from_payload", "normalize_content_sha256", "trust_path", "verdict_path"}
        == EXPECTED_TRUST_API
    )
    assert set(PURE_API) == {
        "manifest.manifest_path",
        "manifest.parse_manifest",
        "trust.finding_from_payload",
        "trust.trust_path",
        "trust.verdict_path",
        "trust.normalize_content_sha256",
    }


#: Fault drivers per covered CLI leaf (linkage: COVERED must equal these keys,
#: so moving a leaf to OUT_OF_SCOPE with its drivers intact fails here).
CLI_FAULT_DRIVERS: dict[tuple[str, ...], tuple[str, ...]] = {
    ("add",): (
        "test_boundary_add_refuses",
        "test_sweep_add_each_ordinal_refuses",
    ),
    ("audit",): (
        "test_boundary_audit_publish_refuses",
        "test_boundary_audit_trust_refuses",
        "test_boundary_audit_allow_refuses",
        "test_sweep_audit_publish_each_ordinal_refuses",
        "test_sweep_audit_trust_each_ordinal_refuses",
        "test_sweep_audit_allow_each_ordinal_refuses",
    ),
    ("bootstrap",): (
        "test_boundary_bootstrap_refuses",
        "test_sweep_bootstrap_each_ordinal_refuses",
        "test_sweep_bootstrap_create_each_ordinal_refuses",
    ),
    ("config", "show"): (
        "test_boundary_config_show_refuses",
        "test_sweep_config_show_each_ordinal_refuses",
    ),
    ("hybrid", "status"): (
        "test_boundary_hybrid_status_degrades",
        "test_sweep_hybrid_status_each_ordinal_degrades",
    ),
    ("init",): (
        "test_boundary_init_refuses",
        "test_sweep_parent_manifest_stat_reached",
        "test_sweep_init_target_each_ordinal_refuses",
    ),
    ("list",): (
        "test_boundary_list_refuses",
        "test_boundary_list_paths_refuses",
        "test_sweep_draft_decision_stat_reached",
        "test_sweep_draft_decision_unmocked_each_ordinal_refuses",
        "test_sweep_list_each_ordinal_refuses",
        "test_sweep_list_paths_each_ordinal_refuses",
    ),
    ("project", "add"): (
        "test_boundary_project_add_refuses",
        "test_sweep_project_add_each_ordinal_refuses",
    ),
    ("project", "resolve"): (
        "test_boundary_project_resolve_refuses",
        "test_sweep_project_resolve_each_ordinal_refuses",
    ),
    ("remove",): (
        "test_boundary_remove_refuses",
        "test_sweep_remove_each_ordinal_refuses",
    ),
    ("status",): (
        "test_boundary_status_all_refuses",
        "test_sweep_status_all_each_ordinal_refuses",
    ),
}

#: Fault drivers per covered API function (same linkage shape as CLI).
API_FAULT_DRIVERS: dict[str, tuple[str, ...]] = {
    "manifest.add_skill_decl": (
        "test_boundary_add_decl_refuses",
        "test_sweep_add_decl_each_ordinal_refuses",
    ),
    "manifest.ensure_empty_manifest": (
        "test_boundary_ensure_empty_refuses",
        "test_sweep_ensure_empty_each_ordinal_refuses",
    ),
    "manifest.ensure_project_manifest": (
        "test_boundary_ensure_project_refuses",
        "test_sweep_ensure_project_each_ordinal_refuses",
    ),
    "manifest.load_manifest": (
        "test_boundary_load_manifest_refuses",
        "test_sweep_load_manifest_each_ordinal_refuses",
    ),
    "manifest.remove_skill_decl": (
        "test_boundary_remove_decl_refuses",
        "test_sweep_remove_decl_each_ordinal_refuses",
    ),
    "trust.load_cached_verdict": (
        "test_boundary_cached_verdict_refuses",
        "test_sweep_cached_verdict_each_ordinal_refuses",
    ),
    "trust.load_trust_record": (
        "test_boundary_trust_record_refuses",
        "test_sweep_trust_record_each_ordinal_refuses",
    ),
    "trust.pin_content_hash": (
        "test_boundary_pin_refuses",
        "test_sweep_pin_each_ordinal_refuses",
    ),
    "trust.store_verdict": (
        "test_boundary_store_verdict_refuses",
        "test_sweep_store_verdict_each_ordinal_refuses",
    ),
}

#: Handler functions in csk.cli per out-of-scope leaf (census: each must hold
#: no filesystem seam; _dispatch inline leaves map to () and are covered by
#: the global _dispatch-has-no-seams assertion).
CLI_HANDLER_MAP: dict[tuple[str, ...], tuple[str, ...]] = {
    ("check",): ("_cmd_check",),
    ("config", "build-https", "add"): ("_cmd_config_build_https",),
    ("config", "build-https", "list"): ("_cmd_config_build_https",),
    ("config", "build-https", "login"): ("_cmd_config_build_https",),
    ("config", "build-https", "remove"): ("_cmd_config_build_https",),
    ("config", "build-ssh", "add"): ("_cmd_config_build_ssh",),
    ("config", "build-ssh", "list"): ("_cmd_config_build_ssh",),
    ("config", "build-ssh", "remove"): ("_cmd_config_build_ssh",),
    ("gc",): (),
    ("global", "add"): ("_dispatch_global",),
    ("global", "init"): ("_dispatch_global",),
    ("global", "install"): ("_dispatch_global", "_cmd_global_install"),
    ("global", "list"): ("_dispatch_global",),
    ("global", "remove"): ("_dispatch_global",),
    ("global", "status"): ("_dispatch_global",),
    ("global", "update"): ("_dispatch_global", "_cmd_global_update"),
    ("global", "upgrade"): ("_dispatch_global", "_cmd_global_install", "_cmd_global_update"),
    ("hybrid", "add"): ("_cmd_hybrid",),
    ("hybrid", "list"): ("_cmd_hybrid",),
    ("hybrid", "remove"): ("_cmd_hybrid",),
    ("install",): ("_cmd_install",),
    ("shell-init",): (),
    ("skill", "check"): ("_cmd_skill_check",),
    ("update",): ("_cmd_update",),
    ("upgrade",): ("_cmd_install",),
}


def test_linkage_covered_entries_have_drivers():
    """COVERED sets equal the driver registries; every named driver exists."""
    assert set(CLI_FAULT_DRIVERS) == COVERED_CLI_LEAVES
    manifest_drivers = {
        name.removeprefix("manifest.") for name in API_FAULT_DRIVERS if name.startswith("manifest.")
    }
    trust_drivers = {
        name.removeprefix("trust.") for name in API_FAULT_DRIVERS if name.startswith("trust.")
    }
    assert manifest_drivers == COVERED_MANIFEST_API
    assert trust_drivers == COVERED_TRUST_API
    module_globals = globals()
    for leaf, tests in CLI_FAULT_DRIVERS.items():
        for name in tests:
            assert callable(module_globals.get(name)), f"{leaf}: missing driver {name}"
    for func, tests in API_FAULT_DRIVERS.items():
        for name in tests:
            assert callable(module_globals.get(name)), f"{func}: missing driver {name}"


#: (entry point, marker) pair each breadth driver covers, keyed by test
#: name. The owner is the CLI leaf tuple or the dotted API function, and
#: must equal the registry key that lists the driver.
BREADTH_PAIRS: dict[str, tuple[object, str]] = {
    "test_boundary_add_refuses": (("add",), "Skillfile"),
    "test_boundary_audit_publish_refuses": (("audit",), "record file"),
    "test_boundary_audit_trust_refuses": (("audit",), "audit dir"),
    "test_boundary_audit_allow_refuses": (("audit",), "audit dir"),
    "test_boundary_bootstrap_refuses": (("bootstrap",), "cfg file"),
    "test_boundary_config_show_refuses": (("config", "show"), "cfg file"),
    "test_boundary_hybrid_status_degrades": (("hybrid", "status"), "marker file"),
    "test_boundary_init_refuses": (("init",), "target dir"),
    "test_boundary_list_refuses": (("list",), "Skillfile"),
    "test_boundary_list_paths_refuses": (("list",), "project dir"),
    "test_boundary_status_all_refuses": (("status",), "Skillfile"),
    "test_boundary_project_add_refuses": (("project", "add"), "fresh dir"),
    "test_boundary_project_resolve_refuses": (("project", "resolve"), "project dir"),
    "test_boundary_remove_refuses": (("remove",), "Skillfile"),
    "test_boundary_trust_record_refuses": ("trust.load_trust_record", "trust file"),
    "test_boundary_cached_verdict_refuses": ("trust.load_cached_verdict", "verdict file"),
    "test_boundary_store_verdict_refuses": ("trust.store_verdict", "verdict parent"),
    "test_boundary_pin_refuses": ("trust.pin_content_hash", "trust parent"),
    "test_boundary_load_manifest_refuses": ("manifest.load_manifest", "Skillfile"),
    "test_boundary_add_decl_refuses": ("manifest.add_skill_decl", "Skillfile"),
    "test_boundary_remove_decl_refuses": ("manifest.remove_skill_decl", "Skillfile"),
    "test_boundary_ensure_empty_refuses": ("manifest.ensure_empty_manifest", "project dir"),
    "test_boundary_ensure_project_refuses": ("manifest.ensure_project_manifest", "project dir"),
}

#: (entry point, marker) pair each sweep driver covers, keyed by test
#: name. Four sweeps have no breadth counterpart (a second branch or a
#: second marker under their entry point); the rest share their pair
#: with the breadth driver on the same marker.
SWEEP_PAIRS: dict[str, tuple[object, str]] = {
    "test_sweep_add_each_ordinal_refuses": (("add",), "Skillfile"),
    "test_sweep_audit_publish_each_ordinal_refuses": (("audit",), "record file"),
    "test_sweep_audit_trust_each_ordinal_refuses": (("audit",), "audit dir"),
    "test_sweep_audit_allow_each_ordinal_refuses": (("audit",), "audit dir"),
    "test_sweep_bootstrap_create_each_ordinal_refuses": (("bootstrap",), "cfg file"),
    "test_sweep_bootstrap_each_ordinal_refuses": (("bootstrap",), "cfg file --if-missing"),
    "test_sweep_config_show_each_ordinal_refuses": (("config", "show"), "cfg file"),
    "test_sweep_hybrid_status_each_ordinal_degrades": (("hybrid", "status"), "marker file"),
    "test_sweep_init_target_each_ordinal_refuses": (("init",), "target dir"),
    "test_sweep_list_each_ordinal_refuses": (("list",), "Skillfile"),
    "test_sweep_list_paths_each_ordinal_refuses": (("list",), "project dir"),
    "test_sweep_status_all_each_ordinal_refuses": (("status",), "Skillfile"),
    "test_sweep_project_add_each_ordinal_refuses": (("project", "add"), "fresh dir"),
    "test_sweep_project_resolve_each_ordinal_refuses": (("project", "resolve"), "project dir"),
    "test_sweep_remove_each_ordinal_refuses": (("remove",), "Skillfile"),
    "test_sweep_draft_decision_stat_reached": (("list",), "cfg file (broken load_config)"),
    "test_sweep_draft_decision_unmocked_each_ordinal_refuses": (
        ("list",),
        "cfg file (malformed, unmocked load)",
    ),
    "test_sweep_parent_manifest_stat_reached": (("init",), "parent Skillfile"),
    "test_sweep_trust_record_each_ordinal_refuses": ("trust.load_trust_record", "trust file"),
    "test_sweep_cached_verdict_each_ordinal_refuses": ("trust.load_cached_verdict", "verdict file"),
    "test_sweep_store_verdict_each_ordinal_refuses": ("trust.store_verdict", "verdict parent"),
    "test_sweep_pin_each_ordinal_refuses": ("trust.pin_content_hash", "trust parent"),
    "test_sweep_load_manifest_each_ordinal_refuses": ("manifest.load_manifest", "Skillfile"),
    "test_sweep_add_decl_each_ordinal_refuses": ("manifest.add_skill_decl", "Skillfile"),
    "test_sweep_remove_decl_each_ordinal_refuses": ("manifest.remove_skill_decl", "Skillfile"),
    "test_sweep_ensure_empty_each_ordinal_refuses": ("manifest.ensure_empty_manifest", "project dir"),
    "test_sweep_ensure_project_each_ordinal_refuses": (
        "manifest.ensure_project_manifest",
        "project dir",
    ),
}


def test_linkage_every_breadth_pair_has_sweep():
    """F1 regression: no (entry point, marker) pair is first-touch-only.

    A breadth driver without a same-pair sweep fails here: the class
    this element exists to remove (``boundary-derivation-first-touch-
    only``) cannot come back as one unswept pair. Pair effectiveness
    (the sweep actually reaching behind the first touch) is proved per
    pair by the behind-guard narrowing mutants in the results note.
    """
    breadth_owner: dict[str, object] = {}
    for leaf, tests in CLI_FAULT_DRIVERS.items():
        for name in tests:
            if name.startswith("test_boundary_"):
                breadth_owner[name] = leaf
    for func, tests in API_FAULT_DRIVERS.items():
        for name in tests:
            if name.startswith("test_boundary_"):
                breadth_owner[name] = func
    assert set(BREADTH_PAIRS) == set(breadth_owner), "breadth registry drifted"
    for name, owner in breadth_owner.items():
        assert BREADTH_PAIRS[name][0] == owner, f"{name}: owner is not {owner}"
    module_globals = globals()
    for name in SWEEP_PAIRS:
        assert callable(module_globals.get(name)), f"missing sweep {name}"
    defined_sweeps = {
        name
        for name, value in module_globals.items()
        if name.startswith("test_sweep_") and callable(value)
    }
    # The synthetic entry proves the injector; it covers no production pair.
    defined_sweeps.discard("test_sweep_reaches_guarded_first_unguarded_second_synthetic")
    assert defined_sweeps == set(SWEEP_PAIRS), "sweep registry drifted"
    for broad, pair in BREADTH_PAIRS.items():
        covering = sorted(name for name, swept in SWEEP_PAIRS.items() if swept == pair)
        assert covering, f"{broad}: pair {pair} has a breadth driver but no sweep"


def test_every_enumerated_leaf_handled():
    """Every derived leaf is fault-driven or census-declared (derived check)."""
    derived_true = enumerate_cli_leaves(draft=True)
    derived_false = enumerate_cli_leaves(draft=False)
    handled = COVERED_CLI_LEAVES | set(OUT_OF_SCOPE_CLI)
    assert handled == derived_true
    assert set(CLI_HANDLER_MAP) == set(OUT_OF_SCOPE_CLI)
    assert handled - {("check",)} == derived_false


def _leaf_branch_calls(leaf: tuple[str, ...]) -> set[str]:
    """Plain-function names called from the dispatch branches handling a leaf.

    Matches ``if`` tests in ``_dispatch`` (and ``_dispatch_global`` for
    ``global`` leaves) whose string literals name the leaf's command, and
    unions the called names. Asserts a branch was found, so a renamed
    command fails here instead of checking nothing.
    """
    import ast

    cli_path = Path(cli.__file__)
    tree = ast.parse(cli_path.read_text(encoding="utf-8"), filename=str(cli_path))
    searches: list[tuple[str, set[str]]] = []
    if leaf[0] == "global":
        searches = [("_dispatch", {"global"}), ("_dispatch_global", {leaf[1]})]
    elif len(leaf) > 2:
        searches = [("_dispatch", {leaf[1]})]
    else:
        searches = [("_dispatch", {leaf[0]})]
    calls: set[str] = set()
    found = False
    for node in ast.walk(tree):
        if not isinstance(node, ast.FunctionDef):
            continue
        for func_name, wanted in searches:
            if node.name != func_name:
                continue
            for sub in ast.walk(node):
                if not isinstance(sub, ast.If):
                    continue
                lits = {
                    const.value
                    for const in ast.walk(sub.test)
                    if isinstance(const, ast.Constant) and isinstance(const.value, str)
                }
                if not (lits & wanted):
                    continue
                found = True
                for call in ast.walk(sub):
                    if isinstance(call, ast.Call) and isinstance(call.func, ast.Name):
                        calls.add(call.func.id)
    assert found, f"{leaf}: no dispatch branch found; census vacuous"
    return calls


def test_census_out_of_scope_handlers_have_no_cli_seams():
    """Out-of-scope handlers hold no direct cli.py filesystem seam.

    A seam added to one of these handlers (rev-2 position D) fails here in
    the boundary family, not only in the seam family's static oracle. The
    hybrid add/list/remove leaves share _cmd_hybrid with status; its one
    seam is status-gated, proved by AST below.

    The mapping itself is checked, not trusted: every mapped handler must
    be defined in ``csk.cli`` and called from its leaf's dispatch branch,
    and a leaf mapped to ``()`` must reach no ``_cmd_*`` from its branch
    (the N1 hatch: ``()`` for a handled leaf fails here).
    """
    import ast

    from fs_seam_support import enumerate_seams

    seams = enumerate_seams("csk.cli")
    by_func: dict[str, list] = {}
    for seam in seams:
        by_func.setdefault(seam.func, []).append(seam)
    assert "_dispatch" not in by_func, "dispatch inline must stay seam-free"
    assert "_dispatch_global" not in by_func, by_func.get("_dispatch_global")

    cli_path = Path(cli.__file__)
    tree = ast.parse(cli_path.read_text(encoding="utf-8"), filename=str(cli_path))
    defined = {
        node.name for node in ast.walk(tree) if isinstance(node, ast.FunctionDef)
    }
    hybrid_gate_proved = False
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == "_cmd_hybrid":
            for sub in ast.walk(node):
                if isinstance(sub, ast.If):
                    try:
                        src = ast.unparse(sub.test)
                    except Exception:
                        continue
                    if "hybrid_command" in src and "status" in src:
                        hybrid_gate_proved = True
    assert hybrid_gate_proved, "_cmd_hybrid status gate not found; census void"

    for leaf, funcs in CLI_HANDLER_MAP.items():
        branch_calls = _leaf_branch_calls(leaf)
        if not funcs:
            hidden = sorted(name for name in branch_calls if name.startswith("_cmd_"))
            assert not hidden, f"{leaf}: () mapping hides handler calls: {hidden}"
            continue
        for func in funcs:
            assert func in defined, f"{leaf}: mapped {func} is not defined in csk.cli"
            assert func in branch_calls, (
                f"{leaf}: mapped {func} is not called from its dispatch branch"
            )
            found = by_func.get(func, [])
            if func == "_cmd_hybrid":
                assert len(found) == 1, f"{leaf}: {func} seam set changed: {found}"
                assert found[0].lineno == 1071 or "read_text" in found[0].op, found
                continue
            assert not found, f"{leaf}: {func} gained a cli.py seam: {found}"


# ---------------------------------------------------------------------------
# Trust API boundary: broad fault beneath each entry point refuses.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("err", ERRNO_CASES)
def test_boundary_trust_record_refuses(monkeypatch, tmp_path, err):
    csk_home = tmp_path / "home"
    path = audit_trust.trust_path(csk_home, _TRUST_HASH)
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps({"schema_version": 1, "pinned": True, "reason": "r"}))
    with (
        broad_fault(monkeypatch, target=path, err=err) as firings,
        pytest.raises(audit_trust.TrustRecordError) as excinfo,
    ):
        audit_trust.load_trust_record(csk_home, _TRUST_HASH)
    assert firings, "broad fault never fired; test is vacuous"
    assert excinfo.value.code == audit_trust.CODE_TRUST_UNREADABLE


@pytest.mark.parametrize("err", ERRNO_CASES)
def test_boundary_cached_verdict_refuses(monkeypatch, tmp_path, err):
    csk_home = tmp_path / "home"
    stored = audit_trust.store_verdict(csk_home, _verdict(_TRUST_HASH))
    with (
        broad_fault(monkeypatch, target=stored, err=err) as firings,
        pytest.raises(audit_trust.TrustRecordError) as excinfo,
    ):
        audit_trust.load_cached_verdict(csk_home, _TRUST_HASH, "null", None)
    assert firings, "broad fault never fired; test is vacuous"
    assert excinfo.value.code == audit_trust.CODE_TRUST_UNREADABLE


@pytest.mark.parametrize("err", ERRNO_CASES)
def test_boundary_store_verdict_refuses(monkeypatch, tmp_path, err):
    csk_home = tmp_path / "home"
    parent = audit_trust.verdict_path(
        csk_home, _TRUST_HASH, "null", None,
        audit_trust.PROMPT_VERSION, audit_trust.RULESET_VERSION,
    ).parent
    with (
        broad_fault(monkeypatch, target=parent, err=err) as firings,
        pytest.raises(audit_trust.TrustRecordError) as excinfo,
    ):
        audit_trust.store_verdict(csk_home, _verdict(_TRUST_HASH))
    assert firings, "broad fault never fired; test is vacuous"
    assert excinfo.value.code == audit_trust.CODE_TRUST_UNWRITABLE


@pytest.mark.parametrize("err", ERRNO_CASES)
def test_boundary_pin_refuses(monkeypatch, tmp_path, err):
    csk_home = tmp_path / "home"
    parent = audit_trust.trust_path(csk_home, _TRUST_HASH).parent
    with (
        broad_fault(monkeypatch, target=parent, err=err) as firings,
        pytest.raises(audit_trust.TrustRecordError) as excinfo,
    ):
        audit_trust.pin_content_hash(csk_home, _TRUST_HASH, reason="r")
    assert firings, "broad fault never fired; test is vacuous"
    assert excinfo.value.code == audit_trust.CODE_TRUST_UNWRITABLE


# ---------------------------------------------------------------------------
# Manifest API boundary.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("err", ERRNO_CASES)
def test_boundary_load_manifest_refuses(monkeypatch, tmp_path, err):
    project = make_project(tmp_path)
    write_skillfile(project, {"schema_version": 1, "skills": []})
    path = project / manifest.MANIFEST_NAME
    with (
        broad_fault(monkeypatch, target=path, err=err) as firings,
        pytest.raises(manifest.ManifestError, match="Cannot read Skillfile at"),
    ):
        manifest.load_manifest(project)
    assert firings, "broad fault never fired; test is vacuous"


@pytest.mark.parametrize("err", ERRNO_CASES)
def test_boundary_add_decl_refuses(monkeypatch, tmp_path, err):
    project = make_project(tmp_path)
    write_skillfile(project, {"schema_version": 1, "skills": []})
    path = project / manifest.MANIFEST_NAME
    with (
        broad_fault(monkeypatch, target=path, err=err) as firings,
        pytest.raises(manifest.ManifestError, match="Cannot read Skillfile at"),
    ):
        manifest.add_skill_decl(project, name="skill-a", ref_kind="tag", ref="v1")
    assert firings, "broad fault never fired; test is vacuous"


@pytest.mark.parametrize("err", ERRNO_CASES)
def test_boundary_remove_decl_refuses(monkeypatch, tmp_path, err):
    project = make_project(tmp_path)
    write_skillfile(project, {"schema_version": 1, "skills": [{"name": "skill-a", "tag": "v1"}]})
    path = project / manifest.MANIFEST_NAME
    with (
        broad_fault(monkeypatch, target=path, err=err) as firings,
        pytest.raises(manifest.ManifestError, match="Cannot read Skillfile at"),
    ):
        manifest.remove_skill_decl(project, "skill-a")
    assert firings, "broad fault never fired; test is vacuous"


@pytest.mark.parametrize("err", ERRNO_CASES)
def test_boundary_ensure_empty_refuses(monkeypatch, tmp_path, err):
    project = make_project(tmp_path)
    with (
        broad_fault(monkeypatch, target=project, err=err) as firings,
        pytest.raises(manifest.ManifestError, match="Cannot access project path"),
    ):
        manifest.ensure_empty_manifest(project)
    assert firings, "broad fault never fired; test is vacuous"


@pytest.mark.parametrize("err", ERRNO_CASES)
def test_boundary_ensure_project_refuses(monkeypatch, tmp_path, err):
    project = make_project(tmp_path)
    with (
        broad_fault(monkeypatch, target=project, err=err) as firings,
        pytest.raises(manifest.ManifestError, match="Cannot access project path"),
    ):
        manifest.ensure_project_manifest(project, alias="app", agents=["codex_cli"])
    assert firings, "broad fault never fired; test is vacuous"


# ---------------------------------------------------------------------------
# CLI boundary: broad fault beneath cli.main refuses without raw escape.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("err", ERRNO_CASES)
def test_boundary_bootstrap_refuses(monkeypatch, tmp_path, capsys, err):
    cfg_path = tmp_path / "cfg" / "config.json"
    monkeypatch.setenv("CSK_CONFIG", str(cfg_path))
    with broad_fault(monkeypatch, target=cfg_path, err=err) as firings:
        code = cli.main(
            ["bootstrap", "--non-interactive", "--skills-root", str(tmp_path / "skills")]
        )
    assert firings, "broad fault never fired; test is vacuous"
    assert code == cli.EXIT_CONFIG
    assert "cannot inspect config" in capsys.readouterr().err


@pytest.mark.parametrize("err", ERRNO_CASES)
def test_boundary_init_refuses(monkeypatch, tmp_path, capsys, err):
    target = tmp_path / "project"
    target.mkdir()
    with broad_fault(monkeypatch, target=target, err=err) as firings:
        code = cli.main(["init", str(target)])
    assert firings, "broad fault never fired; test is vacuous"
    assert code == cli.EXIT_CONFIG
    assert "cannot access target path" in capsys.readouterr().err


@pytest.mark.parametrize("err", ERRNO_CASES)
def test_boundary_config_show_refuses(monkeypatch, tmp_path, skills_root, csk_home, capsys, err):
    project = make_project(tmp_path)
    cfg = make_config(csk_home, skills_root, project)
    csk_config.save_config(cfg)
    monkeypatch.setenv("CSK_CONFIG", str(cfg.path))
    with broad_fault(monkeypatch, target=cfg.path, err=err) as firings:
        code = cli.main(["config", "show"])
    assert firings, "broad fault never fired; test is vacuous"
    assert code == cli.EXIT_CONFIG
    captured = capsys.readouterr()
    assert f"Config path: {cfg.path}" in captured.out
    assert "cannot read config" in captured.err


@pytest.mark.parametrize("err", ERRNO_CASES)
def test_boundary_hybrid_status_degrades(
    monkeypatch, tmp_path, skills_root, csk_home, capsys, err
):
    project = _configured(tmp_path, skills_root, csk_home, monkeypatch)
    assert project.exists()
    hybrid.add_hybrid_decl(
        csk_home, name="skill-conventions", ref_kind="tag", ref="v1", git=None, targets=["app"]
    )
    marker = hybrid.hybrid_skills_root(csk_home) / "skill-conventions" / ".csk-install.json"
    marker.parent.mkdir(parents=True)
    marker.write_text(json.dumps({"commit": "abcdef123456"}), encoding="utf-8")
    with broad_fault(monkeypatch, target=marker, err=err) as firings:
        code = cli.main(["hybrid", "status"])
    assert firings, "broad fault never fired; test is vacuous"
    assert code == cli.EXIT_OK
    assert "[unreadable marker]" in capsys.readouterr().out


@pytest.mark.parametrize("err", ERRNO_CASES)
def test_boundary_list_refuses(monkeypatch, tmp_path, skills_root, csk_home, capsys, err):
    project = make_project(tmp_path)
    write_skillfile(project, {"schema_version": 1, "skills": []})
    cfg_path = csk_home / "config.json"
    cfg_path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "skills_root": str(skills_root),
                "projects": {"app": {"path": str(project), "agents": []}},
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("CSK_CONFIG", str(cfg_path))
    path = project / manifest.MANIFEST_NAME
    with broad_fault(monkeypatch, target=path, err=err) as firings:
        code = cli.main(["list"])
    assert firings, "broad fault never fired; test is vacuous"
    assert code == cli.EXIT_CONFIG
    assert "Cannot read Skillfile at" in capsys.readouterr().err


@pytest.mark.parametrize("err", ERRNO_CASES)
def test_boundary_list_paths_refuses(monkeypatch, tmp_path, skills_root, csk_home, capsys, err):
    project = make_project(tmp_path)
    write_skillfile(project, {"schema_version": 1, "skills": []})
    cfg_path = csk_home / "config.json"
    cfg_path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "skills_root": str(skills_root),
                "projects": {"app": {"path": str(project), "agents": []}},
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("CSK_CONFIG", str(cfg_path))
    with broad_fault(monkeypatch, target=project, err=err) as firings:
        code = cli.main(["list", "--paths"])
    assert firings, "broad fault never fired; test is vacuous"
    assert code == cli.EXIT_CONFIG
    assert "Cannot read Skillfile at" in capsys.readouterr().err


@pytest.mark.parametrize("err", ERRNO_CASES)
def test_boundary_status_all_refuses(monkeypatch, tmp_path, skills_root, csk_home, capsys, err):
    project = make_project(tmp_path)
    write_skillfile(project, {"schema_version": 1, "skills": []})
    cfg_path = csk_home / "config.json"
    cfg_path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "skills_root": str(skills_root),
                "projects": {"app": {"path": str(project), "agents": []}},
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("CSK_CONFIG", str(cfg_path))
    path = project / manifest.MANIFEST_NAME
    with broad_fault(monkeypatch, target=path, err=err) as firings:
        code = cli.main(["status", "--all"])
    assert firings, "broad fault never fired; test is vacuous"
    assert code == cli.EXIT_CONFIG
    assert "Cannot read Skillfile at" in capsys.readouterr().err


@pytest.mark.parametrize("err", ERRNO_CASES)
def test_boundary_project_resolve_refuses(
    monkeypatch, tmp_path, csk_home, skills_root, capsys, err
):
    project = make_project(tmp_path)
    write_skillfile(project, {"schema_version": 1, "skills": []})
    cfg_path = csk_home / "config.json"
    cfg_path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "skills_root": str(skills_root),
                "projects": {"app": {"path": str(project), "agents": ["codex_cli"]}},
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("CSK_CONFIG", str(cfg_path))
    with broad_fault(monkeypatch, target=project, err=err) as firings:
        code = cli.main(["project", "resolve", "app"])
    assert firings, "broad fault never fired; test is vacuous"
    assert code == cli.EXIT_CONFIG
    assert "Cannot access project path" in capsys.readouterr().err


@pytest.mark.parametrize("err", ERRNO_CASES)
def test_boundary_project_add_refuses(monkeypatch, tmp_path, csk_home, skills_root, capsys, err):
    project = make_project(tmp_path)
    cfg = make_config(csk_home, skills_root, project)
    csk_config.save_config(cfg)
    monkeypatch.setenv("CSK_CONFIG", str(cfg.path))
    fresh = tmp_path / "fresh"
    fresh.mkdir()
    with broad_fault(monkeypatch, target=fresh, err=err) as firings:
        code = cli.main(["project", "add", "fresh", str(fresh)])
    assert firings, "broad fault never fired; test is vacuous"
    assert code == cli.EXIT_CONFIG
    assert "Cannot access project path" in capsys.readouterr().err


@pytest.mark.parametrize("err", ERRNO_CASES)
def test_boundary_add_refuses(monkeypatch, tmp_path, skills_root, csk_home, capsys, err):
    # --project app resolves via the config alias, bypassing the
    # project_resolver parent search (non-named module, out of contract).
    project = make_project(tmp_path)
    write_skillfile(project, {"schema_version": 1, "skills": []})
    cfg = make_config(csk_home, skills_root, project)
    csk_config.save_config(cfg)
    monkeypatch.setenv("CSK_CONFIG", str(cfg.path))
    path = project / manifest.MANIFEST_NAME
    with broad_fault(monkeypatch, target=path, err=err) as firings:
        code = cli.main(["add", "skill-a", "--tag", "v1", "--project", "app"])
    assert firings, "broad fault never fired; test is vacuous"
    assert code == cli.EXIT_CONFIG
    assert "Cannot read Skillfile at" in capsys.readouterr().err


@pytest.mark.parametrize("err", ERRNO_CASES)
def test_boundary_remove_refuses(monkeypatch, tmp_path, skills_root, csk_home, capsys, err):
    # --project app resolves via the config alias, bypassing the
    # project_resolver parent search (non-named module, out of contract).
    project = make_project(tmp_path)
    write_skillfile(project, {"schema_version": 1, "skills": [{"name": "skill-a", "tag": "v1"}]})
    cfg = make_config(csk_home, skills_root, project)
    csk_config.save_config(cfg)
    monkeypatch.setenv("CSK_CONFIG", str(cfg.path))
    path = project / manifest.MANIFEST_NAME
    with broad_fault(monkeypatch, target=path, err=err) as firings:
        code = cli.main(["remove", "skill-a", "--project", "app"])
    assert firings, "broad fault never fired; test is vacuous"
    assert code == cli.EXIT_CONFIG
    assert "Cannot read Skillfile at" in capsys.readouterr().err


@pytest.mark.parametrize("err", ERRNO_CASES)
def test_boundary_audit_publish_refuses(
    monkeypatch, tmp_path, skills_root, csk_home, capsys, err
):
    _configured(tmp_path, skills_root, csk_home, monkeypatch)
    record = tmp_path / "record.json"
    record.write_text(json.dumps({"schema_version": 1}), encoding="utf-8")
    with broad_fault(monkeypatch, target=record, err=err) as firings:
        code = cli.main(
            ["audit", "--publish", str(record), "--registry", "https://r.example",
             "--token", "t0ken"]
        )
    assert firings, "broad fault never fired; test is vacuous"
    assert code == cli.EXIT_CONFIG
    assert "cannot read audit record file" in capsys.readouterr().err


@pytest.mark.parametrize("err", ERRNO_CASES)
def test_boundary_audit_trust_refuses(monkeypatch, tmp_path, skills_root, csk_home, capsys, err):
    make_skill_repo(skills_root, "skill-a", tag="v1")
    project = make_project(tmp_path)
    write_skillfile(project, {"schema_version": 1, "skills": [{"name": "skill-a", "tag": "v1"}]})
    cfg_path = csk_home / "config.json"
    cfg_path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "skills_root": str(skills_root),
                "projects": {"app": {"path": str(project), "agents": ["codex_cli"]}},
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("CSK_CONFIG", str(cfg_path))
    audit_dir = csk_home / "audit"
    audit_dir.mkdir(parents=True, exist_ok=True)
    with broad_fault(monkeypatch, target=audit_dir, err=err) as firings:
        code = cli.main(["audit", "app"])
    assert firings, "broad fault never fired; test is vacuous"
    assert code == cli.EXIT_CONFIG
    captured = capsys.readouterr()
    assert (
        audit_trust.CODE_TRUST_UNREADABLE in captured.err
        or audit_trust.CODE_TRUST_UNWRITABLE in captured.err
    )


@pytest.mark.parametrize("err", ERRNO_CASES)
def test_boundary_audit_allow_refuses(
    monkeypatch, tmp_path, skills_root, csk_home, capsys, err
):
    project = make_project(tmp_path)
    cfg = make_config(csk_home, skills_root, project)
    csk_config.save_config(cfg)
    monkeypatch.setenv("CSK_CONFIG", str(cfg.path))
    audit_dir = csk_home / "audit"
    audit_dir.mkdir(parents=True, exist_ok=True)
    with broad_fault(monkeypatch, target=audit_dir, err=err) as firings:
        code = cli.main(["audit", "--allow", _TRUST_HASH, "--reason", "reviewed"])
    assert firings, "broad fault never fired; test is vacuous"
    assert code == cli.EXIT_CONFIG
    assert audit_trust.CODE_TRUST_UNWRITABLE in capsys.readouterr().err


# ---------------------------------------------------------------------------
# Ordinal sweep: fail the Nth touching call for N = 1..K (BUG-260921-hv5upg).
# ---------------------------------------------------------------------------

#: Sweep errno for the injector self-tests below (synthetic + predicates).
#: The pair sweeps run the full errno matrix at every ordinal instead.
_SWEEP_ERR = errno.EACCES

#: Sweep errnos: every (ordinal, errno) combination is exercised, so a
#: guard narrowed to one errno at ordinal >= 2 (review N4, NG-1') dies.
_SWEEP_ERRNOS: tuple[int, ...] = (
    errno.EACCES,
    errno.EIO,
    errno.ENAMETOOLONG,
    errno.ELOOP,
)


def _sweep_sites(touches: list[Touch]) -> list[tuple[str, str, str]]:
    """Pin shape of a dry run: (call name, module, func) per ordinal.

    The module/func pin is what makes a fixture change loud: ordinals
    are relative to the touching-call sequence the fixture produces
    (review N3), so a fixture that shifts which ordinal is which fails
    here instead of silently reassigning expectations.
    """
    return [(str(touch), touch.module, touch.func) for touch in touches]


def _align_sweep_sites(
    touches: list[Touch],
    expected: list[tuple[str, str, str]],
    label: str,
    *,
    allow_platform_variance: bool,
) -> list[int]:
    """Map measured touches to known outcomes without inventing a Win sequence.

    POSIX requires the complete measured sequence, which keeps its touch count
    from silently dropping. Windows uses the current unfaulted run as the
    sequence: each observed touch must map in order to a measured call site,
    while platform-native fast paths may stand in for that site's POSIX call.
    The returned 1-based ordinals select the already-defined outcome for each
    corresponding call site. An empty Windows baseline is always an error.
    """
    actual = _sweep_sites(touches)
    if not allow_platform_variance:
        assert len(actual) == len(expected), f"{label}: touch sequence changed: {actual}"
        for index, (observed, pinned) in enumerate(zip(actual, expected, strict=True)):
            assert observed == pinned, (
                f"{label}: touch sequence changed at {index + 1}: {observed}"
            )
        return list(range(1, len(actual) + 1))

    assert actual, f"{label}: Windows baseline touched nothing; sweep is vacuous"
    aligned_ordinals: list[int] = []
    cursor = 0
    for observed in actual:
        observed_call, observed_module, observed_func = observed
        matches: list[int] = []
        for index in range(cursor, len(expected)):
            pinned_call, pinned_module, pinned_func = expected[index]
            same_site = (observed_module, observed_func) == (pinned_module, pinned_func)
            platform_fast_path = is_runtime_path_fast_path(observed_call)
            if same_site and (observed_call == pinned_call or platform_fast_path):
                matches.append(index)
        assert matches, (
            f"{label}: platform baseline touch has no measured call-site outcome: "
            f"{observed}; remaining measured sites={expected[cursor:]}"
        )
        selected = matches[0]
        aligned_ordinals.append(selected + 1)
        cursor = selected + 1
    return aligned_ordinals


def _assert_sweep_sites(
    touches: list[Touch], expected: list[tuple[str, str, str]], label: str
) -> list[int]:
    """Keep POSIX pins; derive Windows ordinals from the unfaulted baseline.

    A Windows touch dropped from the product also disappears from that
    baseline, so only the fixed POSIX sequences catch a removed refusal
    seam; the Windows measurement guards touches the current product makes.
    """
    return _align_sweep_sites(
        touches,
        expected,
        label,
        allow_platform_variance=os.name == "nt",
    )


def test_platform_sweep_touch_model_aligns_measured_ordinals(monkeypatch):
    """A shorter platform baseline keeps the matching call site's outcome."""
    expected = [
        ("os.lstat", "csk.cli", "_cmd_init"),
        ("os.stat", "csk.cli", "_cmd_init"),
        ("os.stat", "csk.manifest", "_require_project_dir"),
    ]
    observed = [
        Touch("os.stat", module="csk.cli", func="_cmd_init"),
        Touch("os.stat", module="csk.manifest", func="_require_project_dir"),
    ]

    assert _align_sweep_sites(
        observed, expected, "synthetic Windows baseline", allow_platform_variance=True
    ) == [2, 3]

    monkeypatch.setattr(
        fs_boundary_support,
        "RUNTIME_PATH_FAST_PATH_NAMES",
        fs_boundary_support.RUNTIME_PATH_FAST_PATH_NAMES | {"nt._path_exists"},
    )
    resolver_touch = Touch(
        "nt._path_exists", module="csk.cli", func="_cmd_init"
    )
    assert _align_sweep_sites(
        [resolver_touch],
        expected,
        "synthetic Windows resolver",
        allow_platform_variance=True,
    ) == [1]

    with pytest.raises(AssertionError, match="touched nothing"):
        _align_sweep_sites(
            [], expected, "synthetic Windows zero-touch", allow_platform_variance=True
        )


def _check_cli_outcome(
    capsys: pytest.CaptureFixture[str],
    *,
    label: str,
    ordinal: int,
    err: int,
    kind: str,
    payload: str,
    thunk,
) -> None:
    """Assert one firing run's outcome. Kinds: refuse/ok/raw-oserror/raw-runtime."""
    if kind == "raw-oserror":
        with pytest.raises(OSError):
            thunk()
        capsys.readouterr()
        return
    if kind == "raw-runtime":
        with pytest.raises(RuntimeError, match=payload):
            thunk()
        capsys.readouterr()
        return
    code = thunk()
    captured = capsys.readouterr()
    if kind == "refuse":
        assert code == cli.EXIT_CONFIG, (
            f"{label} ord{ordinal} errno={err}: expected refusal, got {code}"
        )
        assert payload in captured.err, (
            f"{label} ord{ordinal} errno={err}: {payload!r} not in stderr: {captured.err!r}"
        )
    elif kind == "ok":
        assert code == cli.EXIT_OK, (
            f"{label} ord{ordinal} errno={err}: expected exit 0, got {code}: {captured.err!r}"
        )
        if payload:
            assert payload in captured.out, (
                f"{label} ord{ordinal} errno={err}: {payload!r} not in stdout: {captured.out!r}"
            )
    else:
        raise AssertionError(f"{label}: unknown sweep kind {kind!r}")


def _sweep_cli(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    *,
    label: str,
    setup,
    sites: list[tuple[str, str, str]],
    expect: dict[int, tuple[str, str]],
    overrides: dict[tuple[int, int], tuple[str, str]] | None = None,
    dry: tuple[int, str, str],
    kp1: tuple[int, str, str],
) -> None:
    """Sweep every (ordinal, errno) of one CLI (entry point, marker) pair.

    ``setup(tag)`` builds a fresh fixture in a tag-disjoint directory
    and returns (marker, argv): sequential blocks share one monkeypatch,
    so overlapping markers would let stale wrappers fire.
    ``sites`` pins the measured POSIX sequence. On Windows the unfaulted run
    supplies the live sequence, which is aligned to those measured call-site
    outcomes; no Windows sequence is typed into the test. Each observed
    ordinal is faulted once for every errno. ``expect[k]`` is the (kind,
    payload) for the matching measured call site (see
    :func:`_check_cli_outcome`); ``overrides[(k, err)]`` replaces it for one
    errno-sensitive combination. ``dry``/``kp1`` are (exit code, stdout
    substring, stderr substring) for the unfaulted run and the K+1 silent run.
    """
    overrides = overrides or {}
    probe, argv = setup("dry")
    with count_touches(monkeypatch, target=probe) as touches:
        code = cli.main(argv)
    captured = capsys.readouterr()
    assert code == dry[0], f"{label} dry run: expected exit {dry[0]}, got {code}"
    if dry[1]:
        assert dry[1] in captured.out, f"{label} dry run stdout: {captured.out!r}"
    if dry[2]:
        assert dry[2] in captured.err, f"{label} dry run stderr: {captured.err!r}"
    expected_ordinals = _assert_sweep_sites(touches, sites, label)
    knob = len(touches)
    assert knob >= 1, f"{label}: dry run touched nothing; sweep is vacuous"

    for k, expected_ordinal in enumerate(expected_ordinals, start=1):
        for err in _SWEEP_ERRNOS:
            kind, payload = overrides.get(
                (expected_ordinal, err), expect[expected_ordinal]
            )
            marker, argv_k = setup(f"k{k}-e{err}")
            with broad_fault(monkeypatch, target=marker, err=err, nth=k) as firings:
                _check_cli_outcome(
                    capsys, label=label, ordinal=k, err=err,
                    kind=kind, payload=payload, thunk=lambda: cli.main(argv_k),
                )
            assert len(firings) == 1, f"{label} ord{k} errno={err}: {firings}"

    marker, argv_kp1 = setup("kp1")
    with broad_fault(
        monkeypatch, target=marker, err=_SWEEP_ERR, nth=knob + 1
    ) as firings:
        code = cli.main(argv_kp1)
    captured = capsys.readouterr()
    assert firings == [], f"{label}: K+1 fired; K={knob} is stale: {firings}"
    assert code == kp1[0], f"{label} K+1: expected exit {kp1[0]}, got {code}"
    if kp1[1]:
        assert kp1[1] in captured.out, f"{label} K+1 stdout: {captured.out!r}"
    if kp1[2]:
        assert kp1[2] in captured.err, f"{label} K+1 stderr: {captured.err!r}"


def _sweep_api(
    monkeypatch: pytest.MonkeyPatch,
    *,
    label: str,
    setup,
    sites: list[tuple[str, str, str]],
    expect: dict[int, tuple[type[Exception], str]],
) -> None:
    """Sweep every (ordinal, errno) of one API (entry point, marker) pair.

    ``setup(tag)`` builds a fresh fixture in a tag-disjoint directory
    and returns (marker, thunk); see :func:`_sweep_cli` for why the
    directories must not overlap.
    ``expect[k]`` is the (exception type, message match) the fault must
    raise at ordinal k for every errno. The dry run and the K+1 run
    must return normally with no firing.
    """
    marker0, thunk0 = setup("dry")
    with count_touches(monkeypatch, target=marker0) as touches:
        thunk0()
    _assert_sweep_sites(touches, sites, label)
    knob = len(touches)
    assert knob >= 1, f"{label}: dry run touched nothing; sweep is vacuous"

    for k in range(1, knob + 1):
        exc_type, match = expect[k]
        for err in _SWEEP_ERRNOS:
            marker, thunk = setup(f"k{k}-e{err}")
            with broad_fault(monkeypatch, target=marker, err=err, nth=k) as firings:
                with pytest.raises(exc_type, match=match):
                    thunk()
            assert len(firings) == 1, f"{label} ord{k} errno={err}: {firings}"

    marker, thunk = setup("kp1")
    with broad_fault(
        monkeypatch, target=marker, err=_SWEEP_ERR, nth=knob + 1
    ) as firings:
        thunk()
    assert firings == [], f"{label}: K+1 fired; K={knob} is stale: {firings}"


def _write_existing_config(path: Path, skills_root: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps({"schema_version": 1, "skills_root": str(skills_root), "projects": {}}),
        encoding="utf-8",
    )


def test_sweep_bootstrap_each_ordinal_refuses(monkeypatch, tmp_path, capsys):
    """Every touching call under bootstrap refuses; K+1 does not fire.

    Setup uses an existing config with ``--if-missing`` plus the draft-env
    bypass, so the unfaulted run touches the marker exactly once (the
    guarded ``path.stat``) and returns early without reaching ``save_config``
    (swept separately by the create-path sweep below). A seam added after
    the stat becomes ordinal 2 and gets its own firing: this is the
    position-B regression test for ``boundary-derivation-first-touch-only``.
    """
    monkeypatch.setenv(csk_config.SKILLFILE_SOURCES_ENV_VAR, "1")

    def setup(tag: str) -> tuple[Path, list[str]]:
        cfg_path = tmp_path / f"bs{tag}" / "cfg" / "config.json"
        _write_existing_config(cfg_path, tmp_path / f"bs{tag}" / "skills")
        monkeypatch.setenv("CSK_CONFIG", str(cfg_path))
        return cfg_path, [
            "bootstrap", "--non-interactive", "--if-missing",
            "--skills-root", str(tmp_path / f"bs{tag}" / "skills"),
        ]

    _sweep_cli(
        monkeypatch, capsys,
        label="bootstrap --if-missing",
        setup=setup,
        sites=[("os.stat", "csk.cli", "_cmd_bootstrap")],
        expect={1: ("refuse", "cannot inspect config")},
        dry=(cli.EXIT_OK, "Kept existing config", ""),
        kp1=(cli.EXIT_OK, "Kept existing config", ""),
    )


def test_sweep_store_verdict_each_ordinal_refuses(monkeypatch, tmp_path):
    """The mkdir/mkdir/write chain under store_verdict refuses at every ordinal."""
    def setup(tag: str):
        home = tmp_path / f"sv{tag}" / "home"
        parent = audit_trust.verdict_path(
            home, _TRUST_HASH, "null", None,
            audit_trust.PROMPT_VERSION, audit_trust.RULESET_VERSION,
        ).parent
        return parent, lambda: audit_trust.store_verdict(home, _verdict(_TRUST_HASH))

    _sweep_api(
        monkeypatch,
        label="store_verdict",
        setup=setup,
        sites=[
            ("os.mkdir", "csk.audit.trust", "store_verdict"),
            ("os.mkdir", "csk.audit.trust", "store_verdict"),
            ("io.open", "csk.audit.trust", "store_verdict"),
        ],
        expect={
            1: (audit_trust.TrustRecordError, audit_trust.CODE_TRUST_UNWRITABLE),
            2: (audit_trust.TrustRecordError, audit_trust.CODE_TRUST_UNWRITABLE),
            3: (audit_trust.TrustRecordError, audit_trust.CODE_TRUST_UNWRITABLE),
        },
    )


def test_sweep_pin_each_ordinal_refuses(monkeypatch, tmp_path):
    """The mkdir/mkdir/write chain under pin_content_hash refuses at every ordinal."""
    def setup(tag: str):
        home = tmp_path / f"pin{tag}" / "home"
        parent = audit_trust.trust_path(home, _TRUST_HASH).parent
        return parent, lambda: audit_trust.pin_content_hash(home, _TRUST_HASH, reason="r")

    _sweep_api(
        monkeypatch,
        label="pin_content_hash",
        setup=setup,
        sites=[
            ("os.mkdir", "csk.audit.trust", "pin_content_hash"),
            ("os.mkdir", "csk.audit.trust", "pin_content_hash"),
            ("io.open", "csk.audit.trust", "pin_content_hash"),
        ],
        expect={
            1: (audit_trust.TrustRecordError, audit_trust.CODE_TRUST_UNWRITABLE),
            2: (audit_trust.TrustRecordError, audit_trust.CODE_TRUST_UNWRITABLE),
            3: (audit_trust.TrustRecordError, audit_trust.CODE_TRUST_UNWRITABLE),
        },
    )


def test_sweep_ensure_empty_each_ordinal_refuses(monkeypatch, tmp_path):
    """The require/present/write chain refuses at every ordinal with its own message."""
    def setup(tag: str):
        project = tmp_path / f"ee{tag}"
        project.mkdir(parents=True, exist_ok=True)
        return project, lambda: manifest.ensure_empty_manifest(project)

    _sweep_api(
        monkeypatch,
        label="ensure_empty_manifest",
        setup=setup,
        sites=[
            ("os.stat", "csk.manifest", "_require_project_dir"),
            ("os.stat", "csk.manifest", "_skillfile_present"),
            ("io.open", "csk.manifest", "_write_skillfile_text"),
        ],
        expect={
            1: (manifest.ManifestError, "Cannot access project path"),
            2: (manifest.ManifestError, "Cannot read Skillfile at"),
            3: (manifest.ManifestError, "Cannot write Skillfile at"),
        },
    )


def test_sweep_add_decl_each_ordinal_refuses(monkeypatch, tmp_path):
    """The read/write pair under add_skill_decl refuses at every ordinal."""
    def setup(tag: str):
        project = tmp_path / f"add{tag}"
        project.mkdir(parents=True, exist_ok=True)
        write_skillfile(project, {"schema_version": 1, "skills": []})
        return (
            project / manifest.MANIFEST_NAME,
            lambda: manifest.add_skill_decl(project, name="skill-a", ref_kind="tag", ref="v1"),
        )

    _sweep_api(
        monkeypatch,
        label="add_skill_decl",
        setup=setup,
        sites=[
            ("io.open", "csk.manifest", "_read_payload"),
            ("io.open", "csk.manifest", "_write_skillfile_text"),
        ],
        expect={
            1: (manifest.ManifestError, "Cannot read Skillfile at"),
            2: (manifest.ManifestError, "Cannot write Skillfile at"),
        },
    )


def test_sweep_draft_decision_stat_reached(monkeypatch, tmp_path, capsys):
    """The draft-decision stat fires when load_config is broken (cli.py:146)."""
    monkeypatch.delenv(csk_config.SKILLFILE_SOURCES_ENV_VAR, raising=False)

    def broken_load_config(*args, **kwargs):
        raise csk_config.ConfigError("broken")

    monkeypatch.setattr(csk_config, "load_config", broken_load_config)

    def setup(tag: str):
        cfg_path = tmp_path / f"dd{tag}" / "config.json"
        cfg_path.parent.mkdir(parents=True, exist_ok=True)
        monkeypatch.setenv("CSK_CONFIG", str(cfg_path))
        return cfg_path, ["list"]

    _sweep_cli(
        monkeypatch, capsys,
        label="draft decision",
        setup=setup,
        sites=[("os.stat", "csk.cli", "_draft_sources_decision")],
        expect={1: ("refuse", "broken")},
        dry=(cli.EXIT_CONFIG, "", "broken"),
        kp1=(cli.EXIT_CONFIG, "", "broken"),
    )


def test_sweep_draft_decision_unmocked_each_ordinal_refuses(monkeypatch, tmp_path, capsys):
    """The draft decision with a really malformed config (no load mock).

    K=3: the draft load read (non-named), the draft stat (named), and
    the dispatch load read (non-named). Ordinals 1-2 refuse via the
    dispatch ``ConfigError`` ("Malformed JSON"); ordinal 3 escapes raw
    from ``config.load_config`` (the ``config.py:236`` bound): declared.
    The mocked sweep above isolates the stat; this one pins the real
    triple so the pair is not mock-only.
    """
    monkeypatch.delenv(csk_config.SKILLFILE_SOURCES_ENV_VAR, raising=False)
    monkeypatch.delenv("CSK_SYSTEM_CONFIG", raising=False)

    def setup(tag: str):
        cfg_path = tmp_path / f"du{tag}" / "config.json"
        cfg_path.parent.mkdir(parents=True, exist_ok=True)
        cfg_path.write_text("{malformed json", encoding="utf-8")
        monkeypatch.setenv("CSK_CONFIG", str(cfg_path))
        return cfg_path, ["list"]

    _sweep_cli(
        monkeypatch, capsys,
        label="draft decision unmocked",
        setup=setup,
        sites=[
            ("io.open", "csk.config", "load_config"),
            ("os.stat", "csk.cli", "_draft_sources_decision"),
            ("io.open", "csk.config", "load_config"),
        ],
        expect={
            1: ("refuse", "Malformed JSON"),
            2: ("refuse", "Malformed JSON"),
            3: ("raw-oserror", ""),
        },
        dry=(cli.EXIT_CONFIG, "", "Malformed JSON"),
        kp1=(cli.EXIT_CONFIG, "", "Malformed JSON"),
    )


def test_sweep_parent_manifest_stat_reached(monkeypatch, tmp_path, capsys):
    """The parent-manifest probe fires when the marker is above the target."""
    monkeypatch.setenv(csk_config.SKILLFILE_SOURCES_ENV_VAR, "1")

    def setup(tag: str):
        root = tmp_path / f"pm{tag}"
        target = root / "project"
        target.mkdir(parents=True, exist_ok=True)
        probed = root / manifest.MANIFEST_NAME
        return probed, ["init", str(target)]

    _sweep_cli(
        monkeypatch, capsys,
        label="parent manifest probe",
        setup=setup,
        sites=[("os.stat", "csk.cli", "_nearest_parent_manifest")],
        expect={1: ("refuse", "Cannot inspect")},
        dry=(cli.EXIT_OK, "Initialized CocoaSkill project", ""),
        kp1=(cli.EXIT_OK, "Initialized CocoaSkill project", ""),
    )


# ---------------------------------------------------------------------------
# Ordinal sweeps for every remaining (entry point, marker) pair (rev 2).
#
# Each sweep below mirrors one breadth driver's fixture on the same marker
# and fires every (ordinal, errno) combination. Ordinals in named modules
# must refuse (or contain, where the guard degrades by design); ordinals
# in non-named modules pin their declared outcome instead of being
# avoided by fixture. See SWEEP_PAIRS below for the pair registry.
# ---------------------------------------------------------------------------


def test_sweep_bootstrap_create_each_ordinal_refuses(monkeypatch, tmp_path, capsys):
    """The bootstrap create path: draft load, draft stat, guard stat, save.

    K=4. Ordinals 1-2 degrade to exit 0 by design (a fault in the draft
    read/stat maps the decision to "unknown" and the create proceeds);
    ordinal 3 is the guarded stat refusing; ordinal 4 lands in non-named
    ``config._write_json_atomic`` (``os.replace``) and escapes raw, which
    is the declared out-of-contract bound, not a refusal.
    """
    monkeypatch.delenv(csk_config.SKILLFILE_SOURCES_ENV_VAR, raising=False)
    monkeypatch.delenv("CSK_SYSTEM_CONFIG", raising=False)

    def setup(tag: str):
        cfg_path = tmp_path / f"bsc{tag}" / "cfg" / "config.json"
        monkeypatch.setenv("CSK_CONFIG", str(cfg_path))
        return cfg_path, [
            "bootstrap", "--non-interactive",
            "--skills-root", str(tmp_path / f"bsc{tag}" / "skills"),
        ]

    _sweep_cli(
        monkeypatch, capsys,
        label="bootstrap create path",
        setup=setup,
        sites=[
            ("io.open", "csk.config", "load_config"),
            ("os.stat", "csk.cli", "_draft_sources_decision"),
            ("os.stat", "csk.cli", "_cmd_bootstrap"),
            ("os.replace", "csk.config", "_write_json_atomic"),
        ],
        expect={
            1: ("ok", "Wrote"),
            2: ("ok", "Wrote"),
            3: ("refuse", "cannot inspect config"),
            4: ("raw-oserror", ""),
        },
        dry=(cli.EXIT_OK, "Wrote", ""),
        kp1=(cli.EXIT_OK, "Wrote", ""),
    )


def test_sweep_init_target_each_ordinal_refuses(monkeypatch, tmp_path, capsys):
    """The init target marker: resolve, guard stat, manifest chain, gitignore.

    Windows ``Path.resolve()`` is instrument-blind: ``ntpath`` keeps its own
    ``_getfinalpathname`` binding, and the discovered ``nt`` wrapper never
    intercepts it. Fixed POSIX pins retain the measured resolve ordinals;
    Windows maps only touches visible to the injector. On POSIX 3.11/3.12,
    ordinal 2 with ELOOP is the errno-sensitive exception (``realpath``
    raises ``RuntimeError`` for a loop, which the guard maps to a refusal).
    The guard stat and the manifest chain refuse. The trailing ordinals
    land in non-named ``gitignore_gate.append_entries`` (unguarded
    ``exists`` + write) and escape raw: declared, out of contract per AC
    (a). The ``resolve()`` call shape differs by interpreter (lstat+stat
    on 3.11/3.12, one lstat on 3.13/3.14). POSIX keeps those sequences pinned;
    Windows derives its sequence from the unfaulted run and maps each
    observed ordinal to its matching call-site outcome.
    """
    import sys

    monkeypatch.delenv(csk_config.SKILLFILE_SOURCES_ENV_VAR, raising=False)
    monkeypatch.delenv("CSK_SYSTEM_CONFIG", raising=False)

    def setup(tag: str):
        target = tmp_path / f"initt{tag}" / "project"
        target.mkdir(parents=True)
        return target, ["init", str(target)]

    if sys.version_info >= (3, 13):
        sites = [
            ("os.lstat", "csk.cli", "_cmd_init"),
            ("os.stat", "csk.cli", "_cmd_init"),
            ("os.stat", "csk.manifest", "_require_project_dir"),
            ("os.stat", "csk.manifest", "_skillfile_present"),
            ("io.open", "csk.manifest", "_write_skillfile_text"),
            ("Path.exists", "csk.gitignore_gate", "append_entries"),
            ("io.open", "csk.gitignore_gate", "append_entries"),
        ]
        expect = {
            1: ("ok", "Initialized"),
            2: ("refuse", "cannot access target path"),
            3: ("refuse", "Cannot access project path"),
            4: ("refuse", "Cannot read Skillfile at"),
            5: ("refuse", "Cannot write Skillfile at"),
            6: ("raw-oserror", ""),
            7: ("raw-oserror", ""),
        }
        overrides: dict[tuple[int, int], tuple[str, str]] = {}
    else:
        sites = [
            ("os.lstat", "csk.cli", "_cmd_init"),
            ("os.stat", "csk.cli", "_cmd_init"),
            ("os.stat", "csk.cli", "_cmd_init"),
            ("os.stat", "csk.manifest", "_require_project_dir"),
            ("os.stat", "csk.manifest", "_skillfile_present"),
            ("io.open", "csk.manifest", "_write_skillfile_text"),
            ("Path.exists", "csk.gitignore_gate", "append_entries"),
            ("io.open", "csk.gitignore_gate", "append_entries"),
        ]
        expect = {
            1: ("ok", "Initialized"),
            2: ("ok", "Initialized"),
            3: ("refuse", "cannot access target path"),
            4: ("refuse", "Cannot access project path"),
            5: ("refuse", "Cannot read Skillfile at"),
            6: ("refuse", "Cannot write Skillfile at"),
            7: ("raw-oserror", ""),
            8: ("raw-oserror", ""),
        }
        overrides = {(2, errno.ELOOP): ("refuse", "cannot access target path")}

    _sweep_cli(
        monkeypatch, capsys,
        label="init target",
        setup=setup,
        sites=sites,
        expect=expect,
        overrides=overrides,
        dry=(cli.EXIT_OK, "Initialized", ""),
        kp1=(cli.EXIT_OK, "Initialized", ""),
    )


def test_sweep_config_show_each_ordinal_refuses(monkeypatch, tmp_path, capsys):
    """The config-show marker: draft load (declared) then the guarded read."""
    monkeypatch.delenv(csk_config.SKILLFILE_SOURCES_ENV_VAR, raising=False)
    monkeypatch.delenv("CSK_SYSTEM_CONFIG", raising=False)

    def setup(tag: str):
        base = tmp_path / f"cs{tag}"
        project = make_project(base)
        home = base / "home"
        home.mkdir(parents=True, exist_ok=True)
        cfg = make_config(home, base / "skills", project)
        csk_config.save_config(cfg)
        monkeypatch.setenv("CSK_CONFIG", str(cfg.path))
        return cfg.path, ["config", "show"]

    _sweep_cli(
        monkeypatch, capsys,
        label="config show",
        setup=setup,
        sites=[
            ("io.open", "csk.config", "load_config"),
            ("io.open", "csk.cli", "_cmd_config_show"),
        ],
        expect={
            1: ("ok", "Config path:"),
            2: ("refuse", "cannot read config"),
        },
        dry=(cli.EXIT_OK, "Config path:", ""),
        kp1=(cli.EXIT_OK, "Config path:", ""),
    )


def test_sweep_hybrid_status_each_ordinal_degrades(monkeypatch, tmp_path, capsys):
    """The hybrid marker read degrades to [unreadable marker] at ordinal 1."""
    monkeypatch.delenv(csk_config.SKILLFILE_SOURCES_ENV_VAR, raising=False)
    monkeypatch.delenv("CSK_SYSTEM_CONFIG", raising=False)

    def setup(tag: str):
        base = tmp_path / f"hy{tag}"
        project = make_project(base)
        home = base / "home"
        home.mkdir(parents=True, exist_ok=True)
        cfg = make_config(home, base / "skills", project)
        csk_config.save_config(cfg)
        monkeypatch.setenv("CSK_CONFIG", str(cfg.path))
        hybrid.add_hybrid_decl(
            home, name="skill-conventions", ref_kind="tag", ref="v1",
            git=None, targets=["app"],
        )
        marker = hybrid.hybrid_skills_root(home) / "skill-conventions" / ".csk-install.json"
        marker.parent.mkdir(parents=True)
        marker.write_text(json.dumps({"commit": "abcdef123456"}), encoding="utf-8")
        return marker, ["hybrid", "status"]

    _sweep_cli(
        monkeypatch, capsys,
        label="hybrid status",
        setup=setup,
        sites=[("io.open", "csk.cli", "_cmd_hybrid")],
        expect={1: ("ok", "[unreadable marker]")},
        dry=(cli.EXIT_OK, "[installed", ""),
        kp1=(cli.EXIT_OK, "[installed", ""),
    )


def test_sweep_list_each_ordinal_refuses(monkeypatch, tmp_path, capsys):
    """The list Skillfile read refuses at ordinal 1."""
    monkeypatch.delenv(csk_config.SKILLFILE_SOURCES_ENV_VAR, raising=False)
    monkeypatch.delenv("CSK_SYSTEM_CONFIG", raising=False)

    def setup(tag: str):
        base = tmp_path / f"li{tag}"
        project = make_project(base)
        write_skillfile(project, {"schema_version": 1, "skills": []})
        home = base / "home"
        home.mkdir(parents=True, exist_ok=True)
        cfg_path = home / "config.json"
        cfg_path.write_text(
            json.dumps({
                "schema_version": 1,
                "skills_root": str(base / "skills"),
                "projects": {"app": {"path": str(project), "agents": []}},
            }),
            encoding="utf-8",
        )
        monkeypatch.setenv("CSK_CONFIG", str(cfg_path))
        return project / manifest.MANIFEST_NAME, ["list"]

    _sweep_cli(
        monkeypatch, capsys,
        label="list",
        setup=setup,
        sites=[("io.open", "csk.manifest", "load_manifest")],
        expect={1: ("refuse", "Cannot read Skillfile at")},
        dry=(cli.EXIT_OK, "no skills declared", ""),
        kp1=(cli.EXIT_OK, "no skills declared", ""),
    )


def test_sweep_list_paths_each_ordinal_refuses(monkeypatch, tmp_path, capsys):
    """The list --paths marker: suffix stat degrades, manifest read refuses."""
    monkeypatch.delenv(csk_config.SKILLFILE_SOURCES_ENV_VAR, raising=False)
    monkeypatch.delenv("CSK_SYSTEM_CONFIG", raising=False)

    def setup(tag: str):
        base = tmp_path / f"lp{tag}"
        project = make_project(base)
        write_skillfile(project, {"schema_version": 1, "skills": []})
        home = base / "home"
        home.mkdir(parents=True, exist_ok=True)
        cfg_path = home / "config.json"
        cfg_path.write_text(
            json.dumps({
                "schema_version": 1,
                "skills_root": str(base / "skills"),
                "projects": {"app": {"path": str(project), "agents": []}},
            }),
            encoding="utf-8",
        )
        monkeypatch.setenv("CSK_CONFIG", str(cfg_path))
        return project, ["list", "--paths"]

    _sweep_cli(
        monkeypatch, capsys,
        label="list --paths",
        setup=setup,
        sites=[
            ("os.stat", "csk.cli", "_project_path_suffix"),
            ("io.open", "csk.manifest", "load_manifest"),
        ],
        expect={
            1: ("ok", "(unreadable)"),
            2: ("refuse", "Cannot read Skillfile at"),
        },
        dry=(cli.EXIT_OK, "no skills declared", ""),
        kp1=(cli.EXIT_OK, "no skills declared", ""),
    )


def test_sweep_status_all_each_ordinal_refuses(monkeypatch, tmp_path, capsys):
    """The status --all Skillfile read refuses at ordinal 1."""
    monkeypatch.delenv(csk_config.SKILLFILE_SOURCES_ENV_VAR, raising=False)
    monkeypatch.delenv("CSK_SYSTEM_CONFIG", raising=False)

    def setup(tag: str):
        base = tmp_path / f"st{tag}"
        project = make_project(base)
        write_skillfile(project, {"schema_version": 1, "skills": []})
        home = base / "home"
        home.mkdir(parents=True, exist_ok=True)
        cfg_path = home / "config.json"
        cfg_path.write_text(
            json.dumps({
                "schema_version": 1,
                "skills_root": str(base / "skills"),
                "projects": {"app": {"path": str(project), "agents": []}},
            }),
            encoding="utf-8",
        )
        monkeypatch.setenv("CSK_CONFIG", str(cfg_path))
        return project / manifest.MANIFEST_NAME, ["status", "--all"]

    _sweep_cli(
        monkeypatch, capsys,
        label="status --all",
        setup=setup,
        sites=[("io.open", "csk.manifest", "load_manifest")],
        expect={1: ("refuse", "Cannot read Skillfile at")},
        dry=(cli.EXIT_OK, "no skills declared", ""),
        kp1=(cli.EXIT_OK, "no skills declared", ""),
    )


def test_sweep_project_resolve_each_ordinal_refuses(monkeypatch, tmp_path, capsys):
    """The project-resolve marker: guard stat refuses, hash probe declared.

    Windows ``Path.resolve()`` is instrument-blind: ``ntpath`` keeps its own
    ``_getfinalpathname`` binding, and the discovered ``nt`` wrapper never
    intercepts it. Fixed POSIX pins retain the measured resolve ordinals;
    Windows maps only touches visible to the injector. Ordinal 1 is the
    guarded ``root.stat``. On POSIX, trailing resolve touches inside
    non-named ``project_resolver.stable_path_hash`` return exit 0 for other
    errnos, while on 3.11 ELOOP raises ``RuntimeError`` ("Symlink loop")
    which escapes raw at ordinal 3: declared, out of contract per AC (a).
    The ``resolve()`` call shape differs by
    interpreter (lstat+stat on 3.11/3.12, one lstat on 3.13/3.14). POSIX
    keeps those measured sequences pinned; Windows derives its sequence
    from the unfaulted run and maps each observed ordinal to its matching
    call-site outcome.
    """
    import sys

    monkeypatch.delenv(csk_config.SKILLFILE_SOURCES_ENV_VAR, raising=False)
    monkeypatch.delenv("CSK_SYSTEM_CONFIG", raising=False)

    def setup(tag: str):
        base = tmp_path / f"pr{tag}"
        project = make_project(base)
        write_skillfile(project, {"schema_version": 1, "skills": []})
        home = base / "home"
        home.mkdir(parents=True, exist_ok=True)
        cfg_path = home / "config.json"
        cfg_path.write_text(
            json.dumps({
                "schema_version": 1,
                "skills_root": str(base / "skills"),
                "projects": {"app": {"path": str(project), "agents": ["codex_cli"]}},
            }),
            encoding="utf-8",
        )
        monkeypatch.setenv("CSK_CONFIG", str(cfg_path))
        return project, ["project", "resolve", "app"]

    if sys.version_info >= (3, 13):
        sites = [
            ("os.stat", "csk.cli", "_render_configured_project_resolution"),
            ("os.lstat", "csk.project_resolver", "stable_path_hash"),
        ]
        expect = {
            1: ("refuse", "Cannot access project path"),
            2: ("ok", "path_hash:"),
        }
        overrides: dict[tuple[int, int], tuple[str, str]] = {}
    else:
        sites = [
            ("os.stat", "csk.cli", "_render_configured_project_resolution"),
            ("os.lstat", "csk.project_resolver", "stable_path_hash"),
            ("os.stat", "csk.project_resolver", "stable_path_hash"),
        ]
        expect = {
            1: ("refuse", "Cannot access project path"),
            2: ("ok", "path_hash:"),
            3: ("ok", "path_hash:"),
        }
        overrides = {(3, errno.ELOOP): ("raw-runtime", "Symlink loop")}

    _sweep_cli(
        monkeypatch, capsys,
        label="project resolve",
        setup=setup,
        sites=sites,
        expect=expect,
        overrides=overrides,
        dry=(cli.EXIT_OK, "path_hash:", ""),
        kp1=(cli.EXIT_OK, "path_hash:", ""),
    )


def test_sweep_project_add_each_ordinal_refuses(monkeypatch, tmp_path, capsys):
    """The project-add fresh dir: require/present/write chain refuses."""
    monkeypatch.delenv(csk_config.SKILLFILE_SOURCES_ENV_VAR, raising=False)
    monkeypatch.delenv("CSK_SYSTEM_CONFIG", raising=False)

    def setup(tag: str):
        base = tmp_path / f"pa{tag}"
        project = make_project(base)
        home = base / "home"
        home.mkdir(parents=True, exist_ok=True)
        cfg = make_config(home, base / "skills", project)
        csk_config.save_config(cfg)
        monkeypatch.setenv("CSK_CONFIG", str(cfg.path))
        fresh = base / "fresh"
        fresh.mkdir()
        return fresh, ["project", "add", "fresh", str(fresh)]

    _sweep_cli(
        monkeypatch, capsys,
        label="project add",
        setup=setup,
        sites=[
            ("os.stat", "csk.manifest", "_require_project_dir"),
            ("os.stat", "csk.manifest", "_skillfile_present"),
            ("io.open", "csk.manifest", "_write_skillfile_text"),
        ],
        expect={
            1: ("refuse", "Cannot access project path"),
            2: ("refuse", "Cannot read Skillfile at"),
            3: ("refuse", "Cannot write Skillfile at"),
        },
        dry=(cli.EXIT_OK, "Added project", ""),
        kp1=(cli.EXIT_OK, "Added project", ""),
    )


def test_sweep_add_each_ordinal_refuses(monkeypatch, tmp_path, capsys):
    """The add Skillfile: read/write pair refuses at each ordinal."""
    monkeypatch.delenv(csk_config.SKILLFILE_SOURCES_ENV_VAR, raising=False)
    monkeypatch.delenv("CSK_SYSTEM_CONFIG", raising=False)

    def setup(tag: str):
        base = tmp_path / f"ad{tag}"
        project = make_project(base)
        write_skillfile(project, {"schema_version": 1, "skills": []})
        home = base / "home"
        home.mkdir(parents=True, exist_ok=True)
        cfg = make_config(home, base / "skills", project)
        csk_config.save_config(cfg)
        monkeypatch.setenv("CSK_CONFIG", str(cfg.path))
        return (
            project / manifest.MANIFEST_NAME,
            ["add", "skill-a", "--tag", "v1", "--project", "app"],
        )

    _sweep_cli(
        monkeypatch, capsys,
        label="add",
        setup=setup,
        sites=[
            ("io.open", "csk.manifest", "_read_payload"),
            ("io.open", "csk.manifest", "_write_skillfile_text"),
        ],
        expect={
            1: ("refuse", "Cannot read Skillfile at"),
            2: ("refuse", "Cannot write Skillfile at"),
        },
        dry=(cli.EXIT_OK, "Added skill", ""),
        kp1=(cli.EXIT_OK, "Added skill", ""),
    )


def test_sweep_remove_each_ordinal_refuses(monkeypatch, tmp_path, capsys):
    """The remove Skillfile: read/write pair refuses at each ordinal."""
    monkeypatch.delenv(csk_config.SKILLFILE_SOURCES_ENV_VAR, raising=False)
    monkeypatch.delenv("CSK_SYSTEM_CONFIG", raising=False)

    def setup(tag: str):
        base = tmp_path / f"rm{tag}"
        project = make_project(base)
        write_skillfile(project, {"schema_version": 1, "skills": [{"name": "skill-a", "tag": "v1"}]})
        home = base / "home"
        home.mkdir(parents=True, exist_ok=True)
        cfg = make_config(home, base / "skills", project)
        csk_config.save_config(cfg)
        monkeypatch.setenv("CSK_CONFIG", str(cfg.path))
        return (
            project / manifest.MANIFEST_NAME,
            ["remove", "skill-a", "--project", "app"],
        )

    _sweep_cli(
        monkeypatch, capsys,
        label="remove",
        setup=setup,
        sites=[
            ("io.open", "csk.manifest", "_read_payload"),
            ("io.open", "csk.manifest", "_write_skillfile_text"),
        ],
        expect={
            1: ("refuse", "Cannot read Skillfile at"),
            2: ("refuse", "Cannot write Skillfile at"),
        },
        dry=(cli.EXIT_OK, "Removed skill", ""),
        kp1=(cli.EXIT_OK, "Removed skill", ""),
    )


def test_sweep_audit_publish_each_ordinal_refuses(monkeypatch, tmp_path, capsys):
    """The audit-publish record read refuses at ordinal 1.

    The unfaulted run itself exits 2 (the probe record fails name
    validation); the sweep distinguishes the fault refusal ("cannot
    read audit record file") from that validation error.
    """
    monkeypatch.delenv(csk_config.SKILLFILE_SOURCES_ENV_VAR, raising=False)
    monkeypatch.delenv("CSK_SYSTEM_CONFIG", raising=False)

    def setup(tag: str):
        base = tmp_path / f"ap{tag}"
        project = make_project(base)
        home = base / "home"
        home.mkdir(parents=True, exist_ok=True)
        cfg = make_config(home, base / "skills", project)
        csk_config.save_config(cfg)
        monkeypatch.setenv("CSK_CONFIG", str(cfg.path))
        record = base / "record.json"
        record.write_text(json.dumps({"schema_version": 1}), encoding="utf-8")
        return record, [
            "audit", "--publish", str(record),
            "--registry", "https://r.example", "--token", "t0ken",
        ]

    _sweep_cli(
        monkeypatch, capsys,
        label="audit publish",
        setup=setup,
        sites=[("io.open", "csk.cli", "_cmd_audit_publish")],
        expect={1: ("refuse", "cannot read audit record file")},
        dry=(cli.EXIT_CONFIG, "", "audit record requires"),
        kp1=(cli.EXIT_CONFIG, "", "audit record requires"),
    )


def test_sweep_audit_trust_each_ordinal_refuses(monkeypatch, tmp_path, capsys):
    """The audit trust store: record read, verdict read, mkdir, write."""
    monkeypatch.delenv(csk_config.SKILLFILE_SOURCES_ENV_VAR, raising=False)
    monkeypatch.delenv("CSK_SYSTEM_CONFIG", raising=False)

    def setup(tag: str):
        base = tmp_path / f"at{tag}"
        sk = base / "skills"
        sk.mkdir(parents=True, exist_ok=True)
        make_skill_repo(sk, "skill-a", tag="v1")
        project = make_project(base)
        write_skillfile(project, {"schema_version": 1,
                                  "skills": [{"name": "skill-a", "tag": "v1"}]})
        home = base / "home"
        home.mkdir(parents=True, exist_ok=True)
        cfg_path = home / "config.json"
        cfg_path.write_text(
            json.dumps({
                "schema_version": 1,
                "skills_root": str(sk),
                "projects": {"app": {"path": str(project), "agents": ["codex_cli"]}},
            }),
            encoding="utf-8",
        )
        monkeypatch.setenv("CSK_CONFIG", str(cfg_path))
        audit_dir = home / "audit"
        audit_dir.mkdir(parents=True, exist_ok=True)
        return audit_dir, ["audit", "app"]

    _sweep_cli(
        monkeypatch, capsys,
        label="audit trust",
        setup=setup,
        sites=[
            ("io.open", "csk.audit.trust", "load_trust_record"),
            ("os.stat", "csk.audit.trust", "_confirm_missing_trust_path"),
            ("os.stat", "csk.audit.trust", "_confirm_missing_trust_path"),
            ("io.open", "csk.audit.trust", "load_cached_verdict"),
            ("os.mkdir", "csk.audit.trust", "store_verdict"),
            ("io.open", "csk.audit.trust", "store_verdict"),
        ],
        expect={
            1: ("refuse", audit_trust.CODE_TRUST_UNREADABLE),
            2: ("refuse", audit_trust.CODE_TRUST_UNREADABLE),
            3: ("refuse", audit_trust.CODE_TRUST_UNREADABLE),
            4: ("refuse", audit_trust.CODE_TRUST_UNREADABLE),
            5: ("refuse", audit_trust.CODE_TRUST_UNWRITABLE),
            6: ("refuse", audit_trust.CODE_TRUST_UNWRITABLE),
        },
        dry=(cli.EXIT_OK, "allow", ""),
        kp1=(cli.EXIT_OK, "allow", ""),
    )


def test_sweep_audit_allow_each_ordinal_refuses(monkeypatch, tmp_path, capsys):
    """The audit --allow pin: mkdir/write pair refuses at each ordinal."""
    monkeypatch.delenv(csk_config.SKILLFILE_SOURCES_ENV_VAR, raising=False)
    monkeypatch.delenv("CSK_SYSTEM_CONFIG", raising=False)

    def setup(tag: str):
        base = tmp_path / f"aa{tag}"
        project = make_project(base)
        home = base / "home"
        home.mkdir(parents=True, exist_ok=True)
        cfg = make_config(home, base / "skills", project)
        csk_config.save_config(cfg)
        monkeypatch.setenv("CSK_CONFIG", str(cfg.path))
        audit_dir = home / "audit"
        audit_dir.mkdir(parents=True, exist_ok=True)
        return audit_dir, ["audit", "--allow", _TRUST_HASH, "--reason", "reviewed"]

    _sweep_cli(
        monkeypatch, capsys,
        label="audit allow",
        setup=setup,
        sites=[
            ("os.mkdir", "csk.audit.trust", "pin_content_hash"),
            ("io.open", "csk.audit.trust", "pin_content_hash"),
        ],
        expect={
            1: ("refuse", audit_trust.CODE_TRUST_UNWRITABLE),
            2: ("refuse", audit_trust.CODE_TRUST_UNWRITABLE),
        },
        dry=(cli.EXIT_OK, "Pinned audit trust", ""),
        kp1=(cli.EXIT_OK, "Pinned audit trust", ""),
    )


def test_sweep_trust_record_each_ordinal_refuses(monkeypatch, tmp_path):
    """The trust-record read refuses at ordinal 1."""
    def setup(tag: str):
        home = tmp_path / f"tr{tag}" / "home"
        path = audit_trust.trust_path(home, _TRUST_HASH)
        path.parent.mkdir(parents=True)
        path.write_text(json.dumps({"schema_version": 1, "pinned": True, "reason": "r"}))
        return path, lambda: audit_trust.load_trust_record(home, _TRUST_HASH)

    _sweep_api(
        monkeypatch,
        label="load_trust_record",
        setup=setup,
        sites=[("io.open", "csk.audit.trust", "load_trust_record")],
        expect={1: (audit_trust.TrustRecordError, audit_trust.CODE_TRUST_UNREADABLE)},
    )


def test_sweep_cached_verdict_each_ordinal_refuses(monkeypatch, tmp_path):
    """The cached-verdict read refuses at ordinal 1."""
    def setup(tag: str):
        home = tmp_path / f"cv{tag}" / "home"
        stored = audit_trust.store_verdict(home, _verdict(_TRUST_HASH))
        return stored, lambda: audit_trust.load_cached_verdict(home, _TRUST_HASH, "null", None)

    _sweep_api(
        monkeypatch,
        label="load_cached_verdict",
        setup=setup,
        sites=[("io.open", "csk.audit.trust", "load_cached_verdict")],
        expect={1: (audit_trust.TrustRecordError, audit_trust.CODE_TRUST_UNREADABLE)},
    )


def test_sweep_load_manifest_each_ordinal_refuses(monkeypatch, tmp_path):
    """The load_manifest read refuses at ordinal 1."""
    def setup(tag: str):
        project = tmp_path / f"lm{tag}"
        project.mkdir(parents=True, exist_ok=True)
        write_skillfile(project, {"schema_version": 1, "skills": []})
        return (
            project / manifest.MANIFEST_NAME,
            lambda: manifest.load_manifest(project),
        )

    _sweep_api(
        monkeypatch,
        label="load_manifest",
        setup=setup,
        sites=[("io.open", "csk.manifest", "load_manifest")],
        expect={1: (manifest.ManifestError, "Cannot read Skillfile at")},
    )


def test_sweep_remove_decl_each_ordinal_refuses(monkeypatch, tmp_path):
    """The read/write pair under remove_skill_decl refuses at every ordinal."""
    def setup(tag: str):
        project = tmp_path / f"rd{tag}"
        project.mkdir(parents=True, exist_ok=True)
        write_skillfile(project, {"schema_version": 1,
                                  "skills": [{"name": "skill-a", "tag": "v1"}]})
        return (
            project / manifest.MANIFEST_NAME,
            lambda: manifest.remove_skill_decl(project, "skill-a"),
        )

    _sweep_api(
        monkeypatch,
        label="remove_skill_decl",
        setup=setup,
        sites=[
            ("io.open", "csk.manifest", "_read_payload"),
            ("io.open", "csk.manifest", "_write_skillfile_text"),
        ],
        expect={
            1: (manifest.ManifestError, "Cannot read Skillfile at"),
            2: (manifest.ManifestError, "Cannot write Skillfile at"),
        },
    )


def test_sweep_ensure_project_each_ordinal_refuses(monkeypatch, tmp_path):
    """The require/present/write chain under ensure_project_manifest refuses."""
    def setup(tag: str):
        project = tmp_path / f"ep{tag}"
        project.mkdir(parents=True, exist_ok=True)
        return project, lambda: manifest.ensure_project_manifest(
            project, alias="app", agents=["codex_cli"])

    _sweep_api(
        monkeypatch,
        label="ensure_project_manifest",
        setup=setup,
        sites=[
            ("os.stat", "csk.manifest", "_require_project_dir"),
            ("os.stat", "csk.manifest", "_skillfile_present"),
            ("io.open", "csk.manifest", "_write_skillfile_text"),
        ],
        expect={
            1: (manifest.ManifestError, "Cannot access project path"),
            2: (manifest.ManifestError, "Cannot read Skillfile at"),
            3: (manifest.ManifestError, "Cannot write Skillfile at"),
        },
    )


# ---------------------------------------------------------------------------
# Harness self-test for the repeat-of class + 3.14 predicate bound.
# ---------------------------------------------------------------------------


def test_sweep_reaches_guarded_first_unguarded_second_synthetic(monkeypatch, tmp_path):
    """Named regression test for boundary-derivation-first-touch-only.

    Synthetic entry with a guarded first seam and an unguarded second seam
    on the same marker, driven through the sweep. Ordinal 1 must refuse
    structurally; ordinal 2 must surface the raw error (the sweep detects
    the unguarded seam instead of stopping at the first touch). Capping the
    sweep at k=1 (the narrowing mutant) leaves ordinal 2 untested and must
    fail the K>=2 assertion.
    """
    marker = tmp_path / "store"
    marker.mkdir()
    target = marker / "file.txt"
    target.write_text("data", encoding="utf-8")

    def synthetic_entry(path: Path) -> str:
        try:
            path.stat()
        except OSError as exc:
            raise ValueError(f"refused-1: {exc}") from exc
        # Second seam, deliberately unguarded.
        path.read_bytes()
        return "ok"

    with count_touches(monkeypatch, target=marker) as touches:
        assert synthetic_entry(target) == "ok"
    knob = len(touches)
    assert knob == 2, f"synthetic must touch twice, got {touches}"

    with broad_fault(monkeypatch, target=marker, err=_SWEEP_ERR, nth=1) as firings:
        with pytest.raises(ValueError, match="refused-1"):
            synthetic_entry(target)
    assert len(firings) == 1

    with broad_fault(monkeypatch, target=marker, err=_SWEEP_ERR, nth=2) as firings:
        with pytest.raises(OSError):
            synthetic_entry(target)
    assert len(firings) == 1

    with broad_fault(monkeypatch, target=marker, err=_SWEEP_ERR, nth=knob + 1) as firings:
        assert synthetic_entry(target) == "ok"
    assert firings == []


def test_runtime_nt_path_predicate_is_counted_and_faulted(monkeypatch, tmp_path):
    """Discovery keeps filesystem predicates and excludes blind helper names."""

    marker = tmp_path / "marker"
    marker.mkdir()
    fake_nt = ModuleType("nt")
    fake_nt._path_exists = lambda path: os.path.exists(path)
    fake_nt._path_normpath = lambda path: os.fspath(path)
    fake_nt._path_splitroot = lambda path: ("", os.fspath(path))
    fake_nt._path_splitroot_ex = lambda path: ("", "", os.fspath(path))
    fake_nt._getfinalpathname = lambda path: os.fspath(path)
    fake_nt._path_x = None
    discovered = fs_boundary_support._discover_path_fast_path_targets(
        [("nt", fake_nt)]
    )
    assert {name for _label, _module, name in discovered} == {"_path_exists"}

    # Unknown future private probes stay discoverable by default.
    fake_nt._path_future_probe = lambda path: os.path.exists(path)
    future_discovered = fs_boundary_support._discover_path_fast_path_targets(
        [("nt", fake_nt)]
    )
    assert {name for _label, _module, name in future_discovered} == {
        "_path_exists",
        "_path_future_probe",
    }
    discovered = tuple(
        target for target in future_discovered if target[2] == "_path_exists"
    )
    monkeypatch.setattr(
        fs_boundary_support,
        "_runtime_path_fast_path_targets",
        lambda: discovered,
    )

    with count_touches(monkeypatch, target=marker) as touches:
        assert fake_nt._path_exists(marker)
    assert [str(touch) for touch in touches] == ["nt._path_exists"]

    with broad_fault(monkeypatch, target=marker, err=errno.EACCES) as firings:
        with pytest.raises(OSError):
            fake_nt._path_exists(marker)
    assert firings == [("nt._path_exists", errno.EACCES)]


def test_touch_matcher_normalizes_parent_components(tmp_path):
    """The matcher keeps normpath coverage without entering patched os.path."""

    target = os.path.normpath(os.fspath(tmp_path))
    candidate = tmp_path / "nested" / ".." / "child"
    assert fs_boundary_support._arg_touches(candidate, target)


@pytest.mark.skipif(
    os.name == "nt",
    reason=(
        "Windows Path.exists uses nt._path_exists, so patching os.stat does not "
        "exercise this POSIX-only premise."
    ),
)
def test_predicates_swallow_os_fault_on_314_and_raise_below_swallow(
    monkeypatch, tmp_path
):
    """3.14 bound: Path.exists/is_mount swallow the os fault; the injector raises below it.

    Proves the mechanism named in the coverage statement: patching os.stat
    alone never reaches the boundary through these predicates on 3.14 (they
    return False), while broad_fault's predicate wrappers raise directly.
    On <=3.13 exists re-raises EACCES, so the first half is 3.14-only.
    """
    import sys

    target = tmp_path / "file.txt"
    target.write_text("data", encoding="utf-8")

    real_stat = os.stat

    def failing_stat(*args, **kwargs):
        raise OSError(errno.EACCES, os.strerror(errno.EACCES))

    monkeypatch.setattr(os, "stat", failing_stat)
    if sys.version_info >= (3, 14):
        assert target.exists() is False
        assert target.is_mount() is False
    else:
        with pytest.raises(OSError):
            target.exists()
    monkeypatch.setattr(os, "stat", real_stat)

    with broad_fault(monkeypatch, target=target, err=_SWEEP_ERR) as firings:
        with pytest.raises(OSError):
            target.exists()
    assert firings == [("Path.exists", _SWEEP_ERR)]

    with broad_fault(monkeypatch, target=target, err=_SWEEP_ERR) as firings:
        with pytest.raises(OSError):
            target.is_mount()
    assert firings == [("Path.is_mount", _SWEEP_ERR)]


def test_glob_resolving_to_marker_fault_is_observable(monkeypatch, tmp_path):
    """N2 mechanism pin: a fault in a glob resolving to the marker shows.

    Where the glob probes the marker (``is_dir`` / ``scandir`` /
    ``exists`` / ``lstat`` depending on version and shape), the fault
    fires; the outcome is either a raw raise (the ``is_dir`` ordinal) or
    a swallowed listing that differs from the unfaulted one. Either way
    a sweep firing there sees a wrong outcome for an unguarded seam (raw
    escape or missing refusal), which is the desired verdict. Where the
    glob never calls the marker it is instrument-blind, pinned per
    shape below as the version bound rather than skipped. On 3.13 the
    class-bound ``scandir`` and literal-name probes are now discovered by
    identity and wrapped. ``parent.glob(name)`` remains blind on 3.12 (no
    ``_PreciseSelector``: the match is pure string comparison over the
    parent's entries), on every OS. That bound is a positive ``touches == []``
    assertion, not a platform skip.

    On Windows, glob/pathlib may retain a builtin path probe on a class at
    import time. The injector finds those class attributes by probe identity
    and wraps them. The Windows touch baseline is measured from current
    product behavior: if a refusal touch disappears from the product, it
    disappears from that baseline too and only the fixed POSIX pins catch
    that regression.
    """
    import sys

    # Disjoint fixtures per shape: sequential fault/count blocks share
    # one monkeypatch, so overlapping markers would let a stale wrapper
    # count (and fire at) the next shape's touches. Every sweep setup
    # uses disjoint per-tag directories for the same reason.
    first = tmp_path / "g1" / "parent"
    first.mkdir(parents=True)
    (first / "child.txt").write_text("x", encoding="utf-8")
    second = tmp_path / "g2" / "parent"
    second.mkdir(parents=True)
    (second / "child.txt").write_text("x", encoding="utf-8")
    shapes = [
        (first, lambda: list(first.glob("*")), set()),
        (second / "child.txt", lambda: list(second.glob("child.txt")), {(3, 12)}),
    ]
    for target, run, blind in shapes:
        with count_touches(monkeypatch, target=target) as touches:
            unfaulted = run()
        assert unfaulted != [], f"{target}: sane fixture"
        if sys.version_info[:2] in blind:
            assert touches == [], (
                f"{target}: unexpectedly visible on {sys.version_info[:2]}"
            )
            continue
        knob = len(touches)
        assert knob >= 1, f"{target}: glob touched nothing"
        for k in range(1, knob + 1):
            with broad_fault(monkeypatch, target=target, err=_SWEEP_ERR, nth=k) as firings:
                try:
                    seen: list | None = run()
                except OSError:
                    seen = None  # raw raise: observable
            assert len(firings) == 1, f"{target} ordinal {k}: {firings}"
            assert seen is None or seen != unfaulted, f"{target} ordinal {k}: fault invisible"
        with broad_fault(
            monkeypatch, target=target, err=_SWEEP_ERR, nth=knob + 1
        ) as firings:
            assert run() == unfaulted
        assert firings == [], f"{target}: K+1 fired"


def test_globber_class_probe_alias_is_discovered_and_observed(monkeypatch, tmp_path):
    """An import-time globber alias is wrapped by identity and invoked."""
    target = tmp_path / "literal-child.txt"
    target.write_text("x", encoding="utf-8")
    bindings = [
        binding
        for binding in fs_boundary_support.GLOB_PROBE_BINDINGS
        if callable(getattr(binding[1], "select_exists", None))
    ]

    if bindings:
        label, cls, _attribute, _descriptor, _probe, _probe_label = bindings[0]
        with count_touches(monkeypatch, target=target) as touches:
            assert list(target.parent.glob(target.name)) == [target]
        observed = {str(touch) for touch in touches}
        assert label in observed, (
            "Path.glob bypassed the identity-discovered class probe: "
            f"binding={label!r}, touches={sorted(observed)!r}"
        )
    else:
        # Older supported interpreters have no class attribute that aliases a
        # discovered OS probe; the public glob behavior remains covered by the
        # end-to-end sweep above.
        assert fs_boundary_support.GLOB_PROBE_BINDINGS == ()


def test_glob_matching_unmarked_only_touches_nothing(monkeypatch, tmp_path):
    """N2 bound pin: a glob that never resolves to the marker is invisible.

    ``parent.glob('other-*')`` scans an unmarked directory and stats only
    unmarked entries, so no touching call is recorded: the instrument is
    blind there by construction, and the coverage statement says so.
    """
    parent = tmp_path / "parent"
    parent.mkdir()
    (parent / "other-1.txt").write_text("x", encoding="utf-8")
    marker = parent / "marker.txt"
    marker.write_text("m", encoding="utf-8")
    with count_touches(monkeypatch, target=marker) as touches:
        assert [path.name for path in parent.glob("other-*")] == ["other-1.txt"]
    assert touches == []


# ---------------------------------------------------------------------------
# F2: ENOTDIR stays absence (stale file-blocked project lists as missing).
# ---------------------------------------------------------------------------


def _stale_enotdir_config(tmp_path: Path, monkeypatch) -> None:
    good = tmp_path / "good"
    good.mkdir()
    write_skillfile(good, {"schema_version": 1, "skills": []})
    afile = tmp_path / "afile"
    afile.write_text("x", encoding="utf-8")
    cfg = tmp_path / "cfg" / "config.json"
    cfg.parent.mkdir()
    cfg.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "skills_root": str(tmp_path / "skills"),
                "projects": {
                    "good": {"path": str(good), "agents": []},
                    "stale": {"path": str(afile / "proj"), "agents": []},
                },
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("CSK_CONFIG", str(cfg))


def test_enotdir_list_marks_stale_project_missing(tmp_path, monkeypatch, capsys):
    _stale_enotdir_config(tmp_path, monkeypatch)
    code = cli.main(["list"])
    captured = capsys.readouterr()
    assert code == cli.EXIT_OK, (code, captured.err)
    assert "Project good" in captured.out and "Project stale" in captured.out
    assert "Skillfile.json missing" in captured.out


def test_enotdir_status_all_marks_stale_project_missing(tmp_path, monkeypatch, capsys):
    _stale_enotdir_config(tmp_path, monkeypatch)
    code = cli.main(["status", "--all"])
    captured = capsys.readouterr()
    assert code == cli.EXIT_OK, (code, captured.err)
    assert "Project good" in captured.out and "Project stale" in captured.out
    assert "Skillfile.json missing" in captured.out


def test_enotdir_load_manifest_is_none(tmp_path):
    afile = tmp_path / "afile"
    afile.write_text("x", encoding="utf-8")
    assert manifest.load_manifest(afile / "proj") is None


def test_enotdir_read_payload_keeps_not_found_message(tmp_path):
    afile = tmp_path / "afile"
    afile.write_text("x", encoding="utf-8")
    project = afile / "proj"
    with pytest.raises(manifest.ManifestError) as excinfo:
        manifest.add_skill_decl(project, name="skill-a", ref_kind="tag", ref="v1")
    assert str(excinfo.value) == (
        f"Skillfile.json not found at {project / manifest.MANIFEST_NAME}; run 'csk init' first"
    )


def test_enotdir_config_show_reports_does_not_exist(monkeypatch, tmp_path, capsys):
    afile = tmp_path / "afile"
    afile.write_text("x", encoding="utf-8")
    cfg_path = afile / "config.json"
    monkeypatch.setenv("CSK_CONFIG", str(cfg_path))
    assert cli.main(["config", "show"]) == cli.EXIT_OK
    captured = capsys.readouterr()
    assert f"Config path: {cfg_path}" in captured.out
    assert "Config does not exist" in captured.out


def test_enotdir_hybrid_marker_is_missing(monkeypatch, tmp_path, skills_root, csk_home, capsys):
    _configured(tmp_path, skills_root, csk_home, monkeypatch)
    hybrid.add_hybrid_decl(
        csk_home, name="skill-conventions", ref_kind="tag", ref="v1", git=None, targets=["app"]
    )
    store = hybrid.hybrid_skills_root(csk_home)
    store.mkdir(parents=True, exist_ok=True)
    blocker = store / "skill-conventions"
    blocker.write_text("not a directory", encoding="utf-8")
    assert cli.main(["hybrid", "status"]) == cli.EXIT_OK
    assert "[missing]" in capsys.readouterr().out


# ---------------------------------------------------------------------------
# Mock-free boundary: real filesystem faults refuse without raw escape.
# ---------------------------------------------------------------------------


@_POSIX_ONLY
@_NOT_ROOT
def test_boundary_chmod0_audit_dir_refuses_csk_audit(
    monkeypatch, tmp_path, csk_home, skills_root, capsys
):
    make_skill_repo(skills_root, "skill-a", tag="v1")
    project = make_project(tmp_path)
    write_skillfile(project, {"schema_version": 1, "skills": [{"name": "skill-a", "tag": "v1"}]})
    cfg_path = csk_home / "config.json"
    cfg_path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "skills_root": str(skills_root),
                "projects": {"app": {"path": str(project), "agents": ["codex_cli"]}},
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("CSK_CONFIG", str(cfg_path))
    first_code = cli.main(["audit", "app"])
    assert first_code in (0, 1)
    capsys.readouterr()
    store_dirs = list((csk_home / "audit").iterdir())
    assert len(store_dirs) == 1
    store_dirs[0].chmod(0)
    try:
        code = cli.main(["audit", "app"])
    finally:
        store_dirs[0].chmod(0o700)
    captured = capsys.readouterr()
    assert code == cli.EXIT_CONFIG
    assert (
        audit_trust.CODE_TRUST_UNREADABLE in captured.err
        or audit_trust.CODE_TRUST_UNWRITABLE in captured.err
    )


@_POSIX_ONLY
@_NOT_ROOT
def test_boundary_chmod0_skillfile_refuses_list(
    monkeypatch, tmp_path, skills_root, csk_home, capsys
):
    project = make_project(tmp_path)
    write_skillfile(project, {"schema_version": 1, "skills": []})
    cfg_path = csk_home / "config.json"
    cfg_path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "skills_root": str(skills_root),
                "projects": {"app": {"path": str(project), "agents": []}},
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("CSK_CONFIG", str(cfg_path))
    skillfile = project / manifest.MANIFEST_NAME
    skillfile.chmod(0)
    try:
        code = cli.main(["list"])
    finally:
        skillfile.chmod(0o600)
    captured = capsys.readouterr()
    assert code == cli.EXIT_CONFIG
    assert "Cannot read Skillfile at" in captured.err
