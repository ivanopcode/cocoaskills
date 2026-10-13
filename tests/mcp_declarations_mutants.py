"""Run M1 narrowing mutants against src/csk/mcp_declarations.py in isolated copies.

Each mutant keeps its gate in place and weakens it to admit one member of the
class it must refuse; a named test selected by `-k` must then fail.

Usage: python tests/mcp_declarations_mutants.py
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
TEST = "tests/test_mcp_declarations.py"
MODULE = "src/csk/mcp_declarations.py"

# (name, [(anchor, replacement, occurrence index)], -k expression)
MUTANTS = (
    ("args-expansion-refused-only-as-prefix",
     [('if "${" in arg:', 'if arg.startswith("${"):', 0)], "test_hostile_args_are_refused"),
    ("npx-accepts-latest-tag",
     [('r"@(?:0|[1-9][0-9]*)\\.(?:0|[1-9][0-9]*)\\.(?:0|[1-9][0-9]*)"',
       'r"@(?:latest|(?:0|[1-9][0-9]*)\\.(?:0|[1-9][0-9]*)\\.(?:0|[1-9][0-9]*))"', 0)],
     "test_npx_requires_an_exact_version"),
    ("npx-admits-one-extra-flag",
     [('_NPX_FLAGS = frozenset({"-y", "--yes", "-q", "--quiet"})',
       '_NPX_FLAGS = frozenset({"-y", "--yes", "-q", "--quiet", "-p"})', 0)],
     "test_npx_requires_an_exact_version"),
    ("header-duplicates-case-sensitive",
     [("        if folded in seen:\n            raise _invalid(header_path", "        if header.name in seen:\n            raise _invalid(header_path", 0),
      ("        seen.add(folded)\n        if header.prefix", "        seen.add(header.name)\n        if header.prefix", 0)],
     "test_header_negatives and duplicate"),
    ("routes-secret-ignores-header-refs",
     [("routes_secret=bool(server.env_names) or bool(server.headers),", "routes_secret=bool(server.env_names),", 0)],
     "test_authority_classification"),
    ("reserved-env-case-sensitive",
     [("    upper = name.upper()\n", "    upper = name\n", 0)], "test_reserved_env_names_are_refused_in_any_case"),
    ("reserved-env-drops-python-prefix",
     [('"PYTHON", "CSK_"', '"CSK_"', 0)], "test_reserved_env_names_are_refused_in_any_case"),
    ("reference-accepted-silently-with-explicit-agents",
     [("    if draft.is_reference:\n        raise _fail(", "    if draft.is_reference and draft.agents is None:\n        raise _fail(", 0),
      ("        assert draft.server is not None\n        declarations[", "        if draft.is_reference:\n            continue\n        assert draft.server is not None\n        declarations[", 0)],
     "reference"),
    ("shared-git-policy-accepted-for-http",
     [('if draft.git_policy == "shared":', 'if draft.git_policy == "shared" and draft.transport == "stdio":', 0)],
     "shared"),
    ("agents-subset-check-skipped-for-known-agents",
     [("if agent not in known or agent not in targets:", "if agent not in known:", 0)], "not-a-target or one-of-two"),
    ("sse-accepted",
     [('    if draft.transport == "sse":\n        raise _fail("mcp_capability_unsupported"', '    if draft.transport == "sse" and draft.is_reference:\n        raise _fail("mcp_capability_unsupported"', 0)],
     "sse-refused"),
    ("scope-mismatch-ignores-user-assertions",
     [("if draft.scope is not None and draft.scope != invocation_scope:", 'if draft.scope not in (None, "user") and draft.scope != invocation_scope:', 0)],
     "user-in-project or test_slice1_vector"),
    ("command-denylist-case-sensitive",
     [("    base = command.lower()\n", "    base = command\n", 0)], "test_shells_and_indirection_are_refused"),
    ("evaluator-flag-print-admitted",
     [('{"-e", "--eval", "-c", "-p", "--print", "eval"}', '{"-e", "--eval", "-c", "-p", "eval"}', 0)],
     "test_evaluator_arguments_are_refused"),
    ("url-query-admitted",
     [('not allow_query_fragment and ("?" in url or "#" in url)', 'not allow_query_fragment and "#" in url', 0)],
     "test_bad_urls_are_refused"),
    ("url-braces-admitted",
     [('for char in "${}\\\\")', 'for char in "$\\\\")', 0)], "test_bad_urls_are_refused"),
    ("bearer-admitted-on-x-prefixed-headers",
     [('if header.prefix == "bearer" and folded != "authorization":', 'if header.prefix == "bearer" and folded != "authorization" and not folded.startswith("x-"):', 0)],
     "bearer-on-custom-header"),
    ("proxy-prefix-refused-only-for-authorization",
     [('or folded.startswith("proxy-")', 'or folded == "proxy-authorization"', 0)], "forbidden"),
    ("format-characters-admitted",
     [('in ("Cc", "Cf", "Cs")', 'in ("Cc", "Cs")', 0)], "bidi or zero-width or bom or test_bad_urls"),
    ("args-count-bound-off-by-one",
     [("if len(args) > _MAX_ARGS:", "if len(args) > _MAX_ARGS + 1:", 0)], "test_args_count_and_length_bounds"),
    ("url-length-bound-off-by-one",
     [("if len(url) > _MAX_URL:", "if len(url) > _MAX_URL + 1:", 0)], "test_url_length_boundary"),
    ("windows-reserved-com9-admitted",
     [("com[1-9]", "com[1-8]", 0)], "test_bad_names_are_refused"),
    ("duplicate-names-case-sensitive",
     [("        folded = draft.name.lower()\n", "        folded = draft.name\n", 0)], "test_case_insensitive_duplicate_names_are_refused"),
    ("env-duplicates-case-sensitive",
     [("        folded = name.upper()\n        if folded in seen:", "        folded = name\n        if folded in seen:", 0)],
     "test_env_names_are_unique_ignoring_case"),
    ("declaration-digest-ignores-help-url",
     [('"auth": {"kind": decl.auth.kind, "help_url": decl.auth.help_url},', '"auth": {"kind": decl.auth.kind},', 0)],
     "test_declaration_digest_changes_with_every_normalized_field"),
    ("declaration-digest-ignores-agents",
     [('        "agents": list(decl.agents),\n', "", 0)], "test_declaration_digest"),
    ("codex-treats-authorization-as-bearer",
     [('if header.prefix == "bearer":\n            entry["bearer', 'if header.name.lower() == "authorization":\n            entry["bearer', 0)],
     "test_golden_native_entries"),
    ("cursor-uses-claude-interpolation",
     [('reference = "${%s}" if agent == "claude_code" else "${env:%s}"', 'reference = "${%s}"', 0)], "test_golden_native_entries"),
    ("requirement-ignores-origin-for-transport",
     [('if entry.origin == "planned" and requirement.transport is not None:', "if requirement.transport is not None:", 0)],
     "test_transport_mismatch_applies_only_to_planned_entries"),
    ("requirement-disabled-entry-satisfies",
     [('if server.state == "active":', 'if server.state != "rejected":', 0)], "test_inactive_entries_never_satisfy"),
    ("requirement-rejected-entry-satisfies",
     [('if server.state == "active":', 'if server.state != "disabled":', 0)], "test_inactive_entries_never_satisfy"),
    ("ambient-read-hidden-from-the-import-scan",
     [('    drafts = _step1_shape(raw)\n', '    __import__("os").getcwd()\n    drafts = _step1_shape(raw)\n', 0)],
     "test_public_api_touches_no_ambient_state"),
    ("requirement-all-behaves-as-any",
     [('if requirement.required_in == "all":\n                if missing:', 'if requirement.required_in == "all" and len(satisfied) == 0:\n                if missing:', 0)],
     "test_all_requires_every_target_agent"),
)

# Survivors by construction: the mutated gate is shadowed by a second gate, so
# no input separates the mutant from the original. They are reported, not counted.
EQUIVALENT_MUTANTS = (
    # Userinfo without a password ("user@host") is still refused: the host
    # grammar rejects "@" independently of the explicit userinfo check.
    ("url-userinfo-without-password-admitted",
     [('if "@" in authority:', 'if ":" in authority.partition("@")[0] and "@" in authority:', 0)], "test_bad_urls_are_refused"),
)


def _replace_occurrence(source: str, before: str, after: str, index: int) -> str:
    pieces = source.split(before)
    if len(pieces) <= index + 1:
        raise AssertionError(f"mutation anchor is missing: {before!r}")
    return before.join(pieces[: index + 1]) + after + before.join(pieces[index + 1 :])


def main() -> int:
    with tempfile.TemporaryDirectory(prefix="csk-m1-mutants-") as tmp:
        sandbox = Path(tmp)
        shutil.copytree(ROOT / "src", sandbox / "src", ignore=shutil.ignore_patterns("__pycache__"))
        (sandbox / "tests" / "fixtures").mkdir(parents=True)
        shutil.copyfile(ROOT / TEST, sandbox / TEST)
        shutil.copytree(ROOT / "tests" / "fixtures" / "mcp", sandbox / "tests" / "fixtures" / "mcp")
        (sandbox / "pytest.ini").write_text("[pytest]\npythonpath = src\n", encoding="utf-8")
        env = dict(os.environ, PYTEST_DISABLE_PLUGIN_AUTOLOAD="1", PYTHONDONTWRITEBYTECODE="1")

        def run(expression: str | None = None) -> subprocess.CompletedProcess[str]:
            argv = [sys.executable, "-m", "pytest", "-q", "--tb=no", "-r", "f", "-p", "no:cacheprovider", TEST]
            if expression:
                argv += ["-k", expression]
            return subprocess.run(argv, cwd=sandbox, env=env, capture_output=True, text=True, timeout=120)

        control = run()
        print("CONTROL:", control.stdout.strip().splitlines()[-1], flush=True)
        if control.returncode != 0:
            return 1
        target = sandbox / MODULE
        original = target.read_text(encoding="utf-8")
        killed_count = 0
        survivors: list[str] = []
        for name, replacements, expression in MUTANTS:
            mutated = original
            for before, after, index in replacements:
                mutated = _replace_occurrence(mutated, before, after, index)
            try:
                target.write_text(mutated, encoding="utf-8")
                result = run(expression)
            finally:
                target.write_text(original, encoding="utf-8")
            # Exit 1 is an assertion failure; collection or startup errors do not kill a mutant.
            killed = result.returncode == 1 and " failed" in result.stdout and " error" not in result.stdout
            failing = [line[len("FAILED ") :] for line in result.stdout.splitlines() if line.startswith("FAILED ")]
            print(f"{name}: {'KILLED' if killed else 'SURVIVED'}; exit {result.returncode}; "
                  f"first failing: {failing[0] if failing else '-'}", flush=True)
            if killed:
                killed_count += 1
            else:
                survivors.append(name)
        for name, replacements, expression in EQUIVALENT_MUTANTS:
            mutated = original
            for before, after, index in replacements:
                mutated = _replace_occurrence(mutated, before, after, index)
            try:
                target.write_text(mutated, encoding="utf-8")
                result = run(expression)
            finally:
                target.write_text(original, encoding="utf-8")
            outcome = "SURVIVED (equivalent, expected)" if result.returncode == 0 else "UNEXPECTEDLY KILLED"
            print(f"{name}: {outcome}; exit {result.returncode}", flush=True)
            if result.returncode != 0:
                survivors.append(name)
        print(f"MUTANTS KILLED: {killed_count} of {len(MUTANTS)}", flush=True)
        return 0 if not survivors else 1


if __name__ == "__main__":
    raise SystemExit(main())
