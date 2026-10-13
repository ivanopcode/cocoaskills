"""Run narrowing mutants of src/csk/mcp_ownership.py in an isolated copy.

Each mutant keeps its gate in place and weakens it to admit exactly one member
of the class the gate must reject; a named test in tests/test_mcp_ownership.py
must fail. A mutant whose named test still passes is reported as a survivor.

Usage: python tests/mcp_ownership_mutants.py
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
TARGET = "src/csk/mcp_ownership.py"
TEST = "tests/test_mcp_ownership.py"

# (name, [(anchor, replacement)], failing test id fragment that must appear, posix_only)
MUTANTS: tuple[tuple[str, list[tuple[str, str]], str, bool], ...] = (
    (
        "equal-foreign-entry-is-claimed",
        [(
            "            elif entry is None:\n                # D6",
            '            elif entry is None and seen.entry_sha256 == want.entry_sha256:\n'
            '                actions.append(Action("keep", address))\n'
            "            elif entry is None:\n                # D6",
        )],
        "test_reconcile_truth_table[d-present-unowned-equal]",
        False,
    ),
    (
        "drift-on-owned-entry-is-repaired-by-replace",
        [('actions.append(Action("skip_drift", address))', 'actions.append(Action("replace", address))')],
        "test_reconcile_truth_table[d-present-owned-drift]",
        False,
    ),
    (
        "drifted-undesired-entry-is-removed-not-released",
        [('actions.append(Action("release", address))', 'actions.append(Action("remove", address))')],
        "test_reconcile_truth_table[u-present-edited]",
        False,
    ),
    (
        "skip-drift-updates-the-ledger-digest",
        [('if action.kind in ("add", "restore", "replace"):', 'if action.kind in ("add", "restore", "replace", "skip_drift"):')],
        "test_next_ledger_table[skip-drift-unchanged]",
        False,
    ),
    (
        "unknown-ledger-plan-emits-actions",
        [(
            "return ReconcilePlan(actions=(), refusals=refusals, warnings=())",
            'return ReconcilePlan(actions=tuple(Action("add", a) for a in sorted(wanted, key=_address_key)), refusals=refusals, warnings=())',
        )],
        "test_reconcile_unknown_ledger_refuses_every_desired_address_and_plans_no_action",
        False,
    ),
    (
        "plan-with-conflict-refusal-is-executable",
        [("return not self.refusals", "return all(r.code == CODE_ENTRY_CONFLICT for r in self.refusals)")],
        "test_reconcile_truth_table[d-present-unowned-different]",
        False,
    ),
    (
        "plan-with-unreadable-refusal-is-executable",
        [("return not self.refusals", "return all(r.code == CODE_NATIVE_UNREADABLE for r in self.refusals)")],
        "test_reconcile_truth_table[d-unreadable-unowned]",
        False,
    ),
    (
        "plan-with-ownership-unknown-refusal-is-executable",
        [("return not self.refusals", "return all(r.code == CODE_OWNERSHIP_UNKNOWN for r in self.refusals)")],
        "test_reconcile_unknown_ledger_refuses_every_desired_address_and_plans_no_action",
        False,
    ),
    (
        "next-ledger-accepts-conflict-refusal",
        [("if not plan.executable:", "if any(r.code != CODE_ENTRY_CONFLICT for r in plan.refusals):")],
        "test_plan_is_not_executable_for_any_refusal_code[mcp_entry_conflict]",
        False,
    ),
    (
        "consent-key-omits-the-agent-set",
        [('"agents": sorted(set(subject.agents)),', '"agents": [],')],
        "test_consent_key_changes_with_every_subject_component[agent-added]",
        False,
    ),
    (
        "consent-key-omits-the-declaration-digest",
        [('"declaration_sha256": _caller_digest(subject.declaration_sha256, "declaration digest"),', '"declaration_sha256": "",')],
        "test_consent_key_changes_with_every_subject_component[digest]",
        False,
    ),
    (
        "consent-key-omits-the-capabilities",
        [('"capabilities": sorted(set(subject.capabilities)),', '"capabilities": [],')],
        "test_consent_key_changes_with_every_subject_component[capability-changed]",
        False,
    ),
    (
        "consent-key-omits-the-scope",
        [('"scope": subject.scope,', '"scope": "project",')],
        "test_consent_key_changes_with_every_subject_component[scope]",
        False,
    ),
    (
        "consent-key-omits-the-consumer",
        [('        "consumer": consumer,\n        "name"', '        "consumer": "",\n        "name"')],
        "test_consent_key_changes_with_the_consumer_and_is_order_independent",
        False,
    ),
    (
        "ledger-parser-accepts-a-consumer-mismatch",
        [
            ("    if consumer != expected_consumer:", "    if False:"),
            ("Ledger(consumer=expected_consumer, scope=scope", "Ledger(consumer=consumer, scope=scope"),
        ],
        "test_parse_ledger_refuses[wrong-consumer]",
        False,
    ),
    (
        "ledger-parser-accepts-a-duplicate-address",
        [("        if address in seen:\n            raise", "        if False:\n            raise")],
        "test_parse_ledger_refuses[duplicate-address]",
        False,
    ),
    (
        "ledger-parser-accepts-uppercase-digests",
        [('re.compile(r"[0-9a-f]{64}")', 're.compile(r"[0-9a-fA-F]{64}")')],
        "test_parse_ledger_refuses[uppercase-digest]",
        False,
    ),
    (
        "ledger-parser-accepts-non-canonical-entry-order",
        [("    if encode_ledger(ledger) != data:", "    if False:")],
        "test_parse_ledger_refuses[unsorted-entries]",
        False,
    ),
    (
        "unreadable-native-config-is-treated-as-absent",
        [('if seen.state in ("unreadable", "malformed"):', 'if seen.state == "malformed":')],
        "test_reconcile_truth_table[d-unreadable-unowned]",
        False,
    ),
    (
        "keep-replaces-regardless-of-the-desired-digest",
        [("same = want.entry_sha256 == entry.written_sha256", "same = True")],
        "test_reconcile_truth_table[d-present-owned-same-desired-new]",
        False,
    ),
    (
        "next-ledger-drops-created-file-carry-over",
        [("                or (old is not None and old.created_file),", "                ,")],
        "test_next_ledger_table[restore-carries-created]",
        False,
    ),
    (
        "unreadable-ledger-is-reported-as-absent",
        [("        return LedgerUnknown(reason=str(exc))", "        return LedgerAbsent()")],
        "test_load_ledger_reports_invalid_content_as_unknown_not_absent",
        False,
    ),
    (
        "link-check-only-on-the-final-component",
        [('        if _is_link(info):\n            raise _unknown(f"{part} is a link', '        if _is_link(info) and index == len(parts) - 1:\n            raise _unknown(f"{part} is a link')],
        "test_load_ledger_refuses_a_symlinked_directory_component[state]",
        False,
    ),
    (
        "final-file-link-is-followed",
        [
            ('        if _is_link(info):\n            raise _unknown(f"{part} is a link', '        if _is_link(info) and index < len(parts) - 1:\n            raise _unknown(f"{part} is a link'),
            ('        | getattr(os, "O_NOFOLLOW", 0)\n        | getattr(os, "O_BINARY", 0)\n    )\n    try:\n        descriptor', '        | getattr(os, "O_BINARY", 0)\n    )\n    try:\n        descriptor'),
        ],
        "test_load_ledger_refuses_a_symlinked_file",
        False,
    ),
    (
        "consent-merge-drops-existing-keys",
        [("merged = existing | added", "merged = added")],
        "test_record_consent_round_trip_and_merge_never_drops_keys",
        False,
    ),
    (
        "invalid-consent-store-is-treated-as-empty-on-record",
        [(
            "        existing = load_consent(csk_home, consumer)\n",
            "        try:\n            existing = load_consent(csk_home, consumer)\n"
            "        except McpOwnershipError:\n            existing = frozenset()\n",
        )],
        "test_record_consent_refuses_an_invalid_existing_store_and_leaves_it_alone",
        False,
    ),
    (
        "consent-record-without-the-manager-home-lock",
        [("    with locking.ManagerHomeLock(csk_home):", '    with __import__("contextlib").nullcontext():')],
        "test_record_consent_takes_the_manager_home_lock_and_so_cannot_nest_inside_it",
        False,
    ),
    (
        "consent-file-is-group-readable",
        [("os.fchmod(descriptor, 0o600)", "os.fchmod(descriptor, 0o640)")],
        "test_record_consent_creates_private_files_and_directories",
        True,
    ),
    (
        "consent-directories-are-group-readable",
        [
            ("current.mkdir(mode=0o700)", "current.mkdir(mode=0o755)"),
            ("os.chmod(current, 0o700)", "os.chmod(current, 0o755)"),
        ],
        "test_record_consent_creates_private_files_and_directories",
        True,
    ),
    (
        "record-consent-bound-check-removed",
        [("if len(encoded) > _STORE_LIMIT:", "if False:")],
        "test_record_consent_refuses_a_store_over_the_size_limit_and_leaves_the_existing_file_alone",
        False,
    ),
    (
        "parse-consent-loses-its-bound",
        [(
            "def parse_consent(data: bytes, *, expected_consumer: str) -> frozenset[str]:\n"
            "    if isinstance(data, bytes) and len(data) > _STORE_LIMIT:",
            "def parse_consent(data: bytes, *, expected_consumer: str) -> frozenset[str]:\n    if False:",
        )],
        "test_parse_consent_refuses_input_over_the_size_limit",
        False,
    ),
    (
        "existing-consent-directory-is-not-narrowed",
        [("if stat.S_IMODE(info.st_mode) & 0o077:", "if False:")],
        "test_record_consent_narrows_existing_mcp_owned_directories_and_leaves_state_alone",
        True,
    ),
)


def _replace(source: str, before: str, after: str) -> str:
    if source.count(before) != 1:
        raise AssertionError(f"mutation anchor must occur exactly once, found {source.count(before)}: {before!r}")
    return source.replace(before, after)


def main() -> int:
    with tempfile.TemporaryDirectory(prefix="csk-mcp-ownership-mutants-") as tmp:
        sandbox = Path(tmp)
        shutil.copytree(ROOT / "src", sandbox / "src", ignore=shutil.ignore_patterns("__pycache__"))
        (sandbox / "tests").mkdir()
        for name in ("test_mcp_ownership.py", "conftest.py", "draft_sources_accounting.py"):
            shutil.copyfile(ROOT / "tests" / name, sandbox / "tests" / name)
        (sandbox / "pytest.ini").write_text("[pytest]\npythonpath = src\n", encoding="utf-8")
        env = dict(os.environ, PYTEST_DISABLE_PLUGIN_AUTOLOAD="1", PYTHONDONTWRITEBYTECODE="1")

        def run() -> subprocess.CompletedProcess[str]:
            argv = [sys.executable, "-m", "pytest", "-q", "--tb=no", "-r", "f", "-p", "no:cacheprovider", TEST]
            return subprocess.run(argv, cwd=sandbox, env=env, capture_output=True, text=True, timeout=300)

        control = run()
        print("CONTROL:", control.stdout.strip().splitlines()[-1], flush=True)
        if control.returncode != 0:
            return 1
        target = sandbox / TARGET
        original = target.read_text(encoding="utf-8")
        killed_count = 0
        survivors: list[str] = []
        for name, replacements, expected, posix_only in MUTANTS:
            if posix_only and os.name == "nt":
                print(f"{name}: SKIPPED (POSIX modes only)", flush=True)
                continue
            mutated = original
            for before, after in replacements:
                mutated = _replace(mutated, before, after)
            try:
                target.write_text(mutated, encoding="utf-8")
                result = run()
            finally:
                target.write_text(original, encoding="utf-8")
            failed = [line[len("FAILED "):].split(" - ")[0] for line in result.stdout.splitlines() if line.startswith("FAILED ")]
            hit = [item for item in failed if expected in item]
            # Exit 1 is an assertion failure; collection or startup errors do not count.
            killed = result.returncode == 1 and " error" not in result.stdout and bool(hit)
            killed_count += killed
            print(f"{name}: {'KILLED' if killed else 'SURVIVED'} by {expected} ({len(failed)} failing)", flush=True)
            if not killed:
                survivors.append(name)
        total = sum(1 for m in MUTANTS if not (m[3] and os.name == "nt"))
        print(f"RATIO: {killed_count} of {total} mutants killed", flush=True)
        for name in survivors:
            print(f"SURVIVOR: {name}", flush=True)
        return 1 if survivors else 0


if __name__ == "__main__":
    raise SystemExit(main())
