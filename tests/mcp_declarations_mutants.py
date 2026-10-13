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
     [('[A-Za-z0-9._~-]*@" + _SEMVER\n)', '[A-Za-z0-9._~-]*@(?:latest|" + _SEMVER + ")"\n)', 0)],
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
     [('_EVALUATOR_FLAG_PREFIXES = ("-e", "-c", "-p", "--eval", "--print")', '_EVALUATOR_FLAG_PREFIXES = ("-e", "-c", "-p", "--eval")', 0)],
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
     [("        if name.lower() in seen:", "        if name in seen:", 0),
      ("        seen.add(name.lower())", "        seen.add(name)", 0)], "test_case_insensitive_duplicate_names_are_refused"),
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
    # --- revision 2 -------------------------------------------------------------
    ("semver-leading-zero-accepted",
     [('_SEMVER_IDENTIFIER = r"(?:0|[1-9][0-9]*|[0-9]*[A-Za-z-][0-9A-Za-z-]*)"',
       '_SEMVER_IDENTIFIER = r"(?:[0-9]+|[0-9]*[A-Za-z-][0-9A-Za-z-]*)"', 0)],
     "test_npx_semver_spec_invalid_examples_are_refused"),
    ("pep440-local-version-refused",
     [('    r"(?:\\+[a-z0-9]+(?:[-_.][a-z0-9]+)*)?"\n', '    r""\n', 0)],
     "test_uvx_pep440_valid_versions_are_admitted"),
    ("uvx-value-flag-accepted-before-package",
     [("while start < len(args) and args[start] in _UVX_FLAGS:",
       'while start < len(args) and args[start].startswith("-") and args[start] != "--from":', 0)],
     "test_uvx_value_flags_are_refused_before_the_package"),
    ("uvx-admits-one-extra-flag",
     [('_UVX_FLAGS = frozenset({"-q", "--quiet", "--offline", "--no-cache", "-n"})',
       '_UVX_FLAGS = frozenset({"-q", "--quiet", "--offline", "--no-cache", "-n", "--isolated"})', 0)],
     "test_uvx_value_flags_are_refused_before_the_package"),
    ("ipv6-zone-id-accepted",
     [('        if "%" in host:\n', '        if "%" in host and not host.isascii():\n', 0)],
     "test_ipv6_zone_identifiers_and_non_ascii_hosts_are_refused"),
    ("sse-unknown-field-skipped",
     [("        if key not in allowed:\n", '        if key not in allowed and transport != "sse":\n', 0)],
     "test_sse_is_validated_like_http_before_it_is_refused"),
    ("help-url-skipped-on-reference",
     [("    if auth.help_url is not None:\n        problem = _url_problem(auth.help_url, allow_query_fragment=True)",
       "    if auth.help_url is not None and server is not None:\n        problem = _url_problem(auth.help_url, allow_query_fragment=True)", 0)],
     "test_auth_help_url_is_validated_whatever_the_server_form"),
    ("glued-evaluator-form-accepted",
     [("arg.startswith(_EVALUATOR_FLAG_PREFIXES)", "arg in _EVALUATOR_FLAG_PREFIXES", 0)],
     "test_glued_and_combined_evaluator_forms_are_refused"),
    ("deno-eval-subcommand-admitted",
     [('if arg == "eval" or arg.startswith', "if arg.startswith", 0)],
     "test_glued_and_combined_evaluator_forms_are_refused"),
    ("npm-x-alias-admitted",
     [('_NPM_RUNNER_ALIASES = frozenset({"x", "init", "create"})', '_NPM_RUNNER_ALIASES = frozenset({"init", "create"})', 0)],
     "test_npm_x_is_a_refused_runner_alias"),
    ("requirement-all-behaves-as-any",
     [('if requirement.required_in == "all":\n                if missing:', 'if requirement.required_in == "all" and len(satisfied) == 0:\n                if missing:', 0)],
     "test_all_requires_every_target_agent"),
    ('error-path-declaration-index-lost', [('_shape_declaration(name, raw[name], indices[name])', '_shape_declaration(name, raw[name], 0)', 0)], 'test_errors_never_echo_unvalidated_keys_or_names'),
    # --- revision 3 ---
    ('declaration-domain-dropped', [('hashlib.sha256(_DECLARATION_DOMAIN + protocol_json.canonical_bytes(normalized))', 'hashlib.sha256(protocol_json.canonical_bytes(normalized))', 0)], 'test_declaration_digest_uses_its_domain'),
    ('entry-domain-dropped', [('hashlib.sha256(_ENTRY_DOMAIN + canonical)', 'hashlib.sha256(canonical)', 0)], 'test_declaration_digest_uses_its_domain'),
    ('declaration-digest-ignores-auth-kind', [('"auth": {"kind": decl.auth.kind, "help_url": decl.auth.help_url},', '"auth": {"help_url": decl.auth.help_url},', 0)], 'test_declaration_digest_changes_with_every_normalized_field'),
    ('error-path-admits-one-control-bearing-key', [('and _NAME_RE.fullmatch(value)', 'and (_NAME_RE.fullmatch(value) or value == "bad\\x1b[31mkey")', 0)], 'test_errors_never_echo_unvalidated_keys_or_names'),
    ('invalid-name-index-sorted', [('_check_name(name, indices[name])', '_check_name(name, names.index(name))', 0)], 'test_invalid_name_index_is_declared_insertion_index_not_sorted_index'),
    ('capability-agent-index-sorted', [('enumerate(draft.agents if draft.agents is not None else agents)', 'enumerate(agents)', 0)], 'test_capability_errors_report_the_declared_agent_index'),
    ('package-manager-x-alias-admitted', [('_PACKAGE_MANAGER_ALIASES = frozenset({"x", "create"})', '_PACKAGE_MANAGER_ALIASES = frozenset({"create"})', 0)], 'test_panel_runner_aliases_are_refused'),
    ('package-manager-create-alias-admitted', [('_PACKAGE_MANAGER_ALIASES = frozenset({"x", "create"})', '_PACKAGE_MANAGER_ALIASES = frozenset({"x"})', 0)], 'test_panel_runner_aliases_are_refused'),
    ('npm-init-alias-admitted', [('_NPM_RUNNER_ALIASES = frozenset({"x", "init", "create"})', '_NPM_RUNNER_ALIASES = frozenset({"x", "create"})', 0)], 'test_panel_runner_aliases_are_refused'),
    ('bun-x-alias-admitted', [('_BUN_RUNNER_ALIASES = frozenset({"x", "create"})', '_BUN_RUNNER_ALIASES = frozenset({"create"})', 0)], 'test_panel_runner_aliases_are_refused'),
    ('bun-create-alias-admitted', [('_BUN_RUNNER_ALIASES = frozenset({"x", "create"})', '_BUN_RUNNER_ALIASES = frozenset({"x"})', 0)], 'test_panel_runner_aliases_are_refused'),
    ('uv-tool-run-alias-admitted', [('or (base == "uv" and "run" in args)', 'or (base == "uv" and "run" in args and "tool" not in args)', 0)], 'test_panel_runner_aliases_are_refused'),
    ('uv-run-alias-admitted', [('or (base == "uv" and "run" in args)', 'or (base == "uv" and "run" in args and "tool" in args)', 0)], 'test_panel_runner_aliases_are_refused'),
    ('deno-npm-runner-admitted', [('arg.startswith("npm:")', 'arg.startswith("npm:@")', 0)], 'test_panel_runner_aliases_are_refused'),
    ('cmd-runner-alias-admitted', [('base.endswith((".cmd", ".bat"))', 'base.endswith((".bat",))', 0)], 'test_panel_runner_aliases_are_refused'),
    ('bat-runner-alias-admitted', [('base.endswith((".cmd", ".bat"))', 'base.endswith((".cmd",))', 0)], 'test_panel_runner_aliases_are_refused'),
    ('semver-build-leading-zeros-refused', [('r"(?:\\+[0-9A-Za-z-]+(?:\\.[0-9A-Za-z-]+)*)?"', 'rf"(?:\\+{_SEMVER_IDENTIFIER}(?:\\.{_SEMVER_IDENTIFIER})*)?"', 0)], 'test_npx_semver_spec_valid_examples_are_admitted'),
    ('pep440-dev-separators-refused', [('r"(?:[-_.]?dev[-_.]?[0-9]*)?"', 'r"(?:\\.dev[0-9]*)?"', 0)], 'test_uvx_pep440_valid_versions_are_admitted'),
    ('uvx-package-edge-punctuation-admitted', [('[A-Za-z0-9](?:[A-Za-z0-9._-]*[A-Za-z0-9])?', '[A-Za-z0-9._-]+', 0)], 'test_uvx_package_name_must_start_and_end_alphanumeric'),
    ('url-path-backslash-admitted', [('for char in "${}\\\\")', 'for char in "${}")', 0)], 'test_bad_urls_are_refused or test_help_url_refuses_backslash_in_path'),
    ('declaration-cwd-admitted', [('_DECLARATION_FIELDS = frozenset({"server", "reference", "agents", "scope", "auth", "git_policy"})', '_DECLARATION_FIELDS = frozenset({"server", "reference", "agents", "scope", "auth", "git_policy", "cwd"})', 0)], 'cwd-top-field'),
    ('declaration-enabled-admitted', [('_DECLARATION_FIELDS = frozenset({"server", "reference", "agents", "scope", "auth", "git_policy"})', '_DECLARATION_FIELDS = frozenset({"server", "reference", "agents", "scope", "auth", "git_policy", "enabled"})', 0)], 'enabled-top-field'),
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
