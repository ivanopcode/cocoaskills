"""Run C1 narrowing mutants in isolated copies, without Git or network use.

Usage: python tests/audit_secrets_mutants.py [name-substring ...]
With arguments, only mutants whose name contains one of the substrings run
(the control run always runs first).
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
TEST = "tests/test_audit_secrets.py"
REVIEW_TEST = "tests/test_audit_secrets_review.py"
MATRIX_TEST = "tests/test_token_redaction_matrix.py"
CLOSURE_TEST = "tests/test_usage_error_closure.py"
UE = "src/csk/usage_errors.py"
REFUSAL = 'action=_RefuseTokenAction, nargs="?", help=argparse.SUPPRESS,'
ENV_CALL = "env = backend_environment(COMMAND_REQUIRED_ENV, overrides=self.config.env)"

# Each mutation retains a narrower version of a security gate.
MUTANTS = (
    ("command-admits-registry-token-only", "src/csk/audit/backends/command_backend.py", [
        (ENV_CALL, ENV_CALL + '\n            env.update({name: value for name, value in __import__("os").environ.items() if name == "CSK_REGISTRY_TOKEN"})', 0),
     ], "audit_backend_git_auth_boundary and command"),
    ("codex-admits-registry-token-only", "src/csk/audit/backends/codex_backend.py", [
        ("env=backend_environment(CODEX_REQUIRED_ENV)", 'env=backend_environment(CODEX_REQUIRED_ENV) | {name: value for name, value in __import__("os").environ.items() if name == "CSK_REGISTRY_TOKEN"}', 0),
     ], "audit_backend_git_auth_boundary and codex"),
    ("file-size-admit-one-extra-byte", "src/csk/registry_token.py", [
        ("if info.st_size > MAX_TOKEN_FILE_BYTES:", "if info.st_size > MAX_TOKEN_FILE_BYTES + 1:", 0),
     ], "token_file_refuses_over_64_kib_before_read"),
    ("file-growth-admit-one-extra-byte", "src/csk/registry_token.py", [
        ("if len(raw) > MAX_TOKEN_FILE_BYTES:", "if len(raw) > MAX_TOKEN_FILE_BYTES + 1:", 0),
     ], "token_file_bounds_read_if_file_grows_after_fstat"),
    ("file-type-directories-only", "src/csk/registry_token.py", [
        ("not stat.S_ISREG(checked.st_mode)", "stat.S_ISDIR(checked.st_mode)", 0),
        ("if not stat.S_ISREG(info.st_mode):", "if stat.S_ISDIR(info.st_mode):", 0),
     ], "token_file_refuses_fifo_without_opening_it"),
    ("file-type-follow-symlinks", "src/csk/registry_token.py", [
        ("path.lstat()", "path.stat()", 0),
        ('getattr(os, "O_NOFOLLOW", None)', 'None', 0),
     ], "token_file_refuses_symlink"),
    ("file-type-no-open-symlink-gate", "src/csk/registry_token.py", [
        ('getattr(os, "O_NOFOLLOW", None)', 'None', 0),
        ('(checked.st_dev, checked.st_ino) != (info.st_dev, info.st_ino)', 'checked.st_dev != info.st_dev', 0),
     ], "token_file_refuses_symlink_swapped_before_open"),
    ("explicit-file-env-fallback", "src/csk/registry_token.py", [
        ('raise ValueError(f"cannot read --token-file as a regular UTF-8 file: {reason}") from exc',
         'return os.environ.get("CSK_REGISTRY_TOKEN", "").strip()', 0),
        ('raise ValueError("cannot read --token-file as a regular UTF-8 file") from exc',
         'return os.environ.get("CSK_REGISTRY_TOKEN", "").strip()', 0),
        ('    if not token:', '    token = token or os.environ.get("CSK_REGISTRY_TOKEN", "").strip()\n    if not token:', 0),
     ], "explicit_token_file_never_falls_back_to_environment"),
    ("file-type-open-directories-only", "src/csk/registry_token.py", [
        ("if not stat.S_ISREG(info.st_mode):", "if stat.S_ISDIR(info.st_mode):", 0),
        ('if (not info.st_ino or', 'if (not stat.S_ISFIFO(info.st_mode) and (not info.st_ino or', 0),
        ('stat.S_IFMT(info.st_mode)):', 'stat.S_IFMT(info.st_mode))):', 0),
     ], "token_file_checks_open_file_type"),
    ("file-permissions-others-only", "src/csk/registry_token.py", [
        ("(stat.S_IRGRP | stat.S_IROTH)", "stat.S_IROTH", 0),
     ], "token_file_refuses_shared_read_permissions"),
    ("empty-token-permitted", "src/csk/registry_token.py", [
        ("    if not token:", "    if token is None:", 0),
        ('r"[A-Za-z0-9\\-._~+/=]+"', 'r"[A-Za-z0-9\\-._~+/=]*"', 0),
     ], "token_file_refuses_unusable_input and empty"),
    ("file-permissions-group-only", "src/csk/registry_token.py", [
        ("(stat.S_IRGRP | stat.S_IROTH)", "stat.S_IRGRP", 0),
     ], "token_file_refuses_shared_read_permissions"),
    ("file-permissions-path-only", "src/csk/registry_token.py", [
        ("info = os.fstat(stream.fileno())", "info = checked", 0),
     ], "token_file_checks_open_file_permissions"),
    ("identity-device-only", "src/csk/registry_token.py", [
        ('(checked.st_dev, checked.st_ino) != (info.st_dev, info.st_ino)', 'checked.st_dev != info.st_dev', 0),
     ], "token_file_refuses_regular_replacement_without_nofollow"),
    ("token-grammar-allows-one-space", "src/csk/registry_token.py", [
        ('r"[A-Za-z0-9\\-._~+/=]+"', 'r"[A-Za-z0-9\\-._~+/= ]+"', 0),
     ], "publish_refuses_invalid_token_before_request"),
    ("http-error-discloses-valueerror-only", "src/csk/cli.py", [
        ('    except Exception:\n        # HTTP exceptions', '    except Exception as exc:\n        if isinstance(exc, ValueError):\n            raise\n        # HTTP exceptions', 0),
     ], "publish_http_exception_never_echoes_header"),
    ("abbreviation-audit-only", "src/csk/cli.py", [
        ('kwargs["allow_abbrev"] = False', 'kwargs["allow_abbrev"] = kwargs.get("prog", "").endswith(" audit")', 0),
     ], "publish_does_not_accept_abbreviated_registry"),
    ("path-expansion-runtime-unshaped", "src/csk/registry_token.py", [
        ('except (UnicodeError, RuntimeError) as exc:', 'except UnicodeError as exc:', 0),
     ], "token_path_expansion_error_is_shaped"),
    ("command-token-blacklist-only", "src/csk/audit/backends/command_backend.py", [
        (ENV_CALL, 'env = {name: value for name, value in __import__("os").environ.items() if not name.endswith("_TOKEN")}', 0),
     ], "backend_child_environment_is_allowlisted and extract-command"),
    ("codex-token-blacklist-only", "src/csk/audit/backends/codex_backend.py", [
        ("env=backend_environment(CODEX_REQUIRED_ENV)", 'env={name: value for name, value in __import__("os").environ.items() if not name.endswith("_TOKEN")}', 0),
     ], "backend_child_environment_is_allowlisted and extract-codex"),
    ("command-filter-parent-only", "src/csk/audit/backends/command_backend.py", [
        (ENV_CALL, "env = backend_environment(COMMAND_REQUIRED_ENV)\n            env.update(self.config.env)", 0),
     ], "backend_child_environment_is_allowlisted and extract-command"),
    ("env-token-exclusion-only", "src/csk/audit/backends/environment.py", [
        ('security_name.endswith(("_TOKEN", "_KEY"))', 'security_name.endswith("_KEY")', 0),
     ], "backend_required_variables_cannot_allow_secrets and TOKEN"),
    ("env-key-exclusion-only", "src/csk/audit/backends/environment.py", [
        ('security_name.endswith(("_TOKEN", "_KEY"))', 'security_name.endswith("_TOKEN")', 0),
     ], "backend_required_variables_cannot_allow_secrets and KEY"),
    ("env-ssh-socket-exclusion-only", "src/csk/audit/backends/environment.py", [
        (' or security_name == "SSH_AUTH_SOCK"', '', 0),
     ], "backend_required_variables_cannot_allow_secrets and SSH_AUTH_SOCK"),
    ("env-git-exclusion-only", "src/csk/audit/backends/environment.py", [
        (' or security_name.startswith("GIT_")', '', 0),
     ], "backend_required_variables_cannot_allow_secrets and GIT_CONFIG_GLOBAL"),
    ("locale-secret-exemption", "src/csk/audit/backends/environment.py", [
        ('if security_name.endswith(("_TOKEN", "_KEY")) or security_name == "SSH_AUTH_SOCK" or security_name.startswith("GIT_"):',
         'if not security_name.startswith("LC_") and (security_name.endswith(("_TOKEN", "_KEY")) or security_name == "SSH_AUTH_SOCK" or security_name.startswith("GIT_")):', 0),
     ], "backend_child_environment_is_allowlisted and extract"),
    # ---- rev 5: closed usage-error output.  Each mutant keeps the closure in
    # place and weakens it to admit exactly one member of the class it rejects.
    ("error-prints-raw-message", "src/csk/cli.py", [
        ("        text = usage_errors.closed_message(self, message, _STATIC_USAGE_MESSAGES)",
         "        text = message", 0),
     ], "fuzz or allowlisted_template"),
    ("error-closed-for-root-parser-only", "src/csk/cli.py", [
        ("        text = usage_errors.closed_message(self, message, _STATIC_USAGE_MESSAGES)",
         "        text = usage_errors.closed_message(self, message, _STATIC_USAGE_MESSAGES) if self.prog == \"csk\" else message", 0),
     ], "rev6_ignored_explicit or rev6_each_argparse"),
    ("error-closed-for-depth-less-than-two-only", "src/csk/cli.py", [
        ("        text = usage_errors.closed_message(self, message, _STATIC_USAGE_MESSAGES)",
         "        text = usage_errors.closed_message(self, message, _STATIC_USAGE_MESSAGES) if self.prog.count(' ') < 2 else message", 0),
     ], "rev6_ignored_explicit or rev6_each_argparse"),
    ("error-exit-code-changed", "src/csk/cli.py", [
        ("        self.exit(EXIT_CONFIG, f\"{self.prog}: error: {text}\\n\")",
         "        self.exit(1, f\"{self.prog}: error: {text}\\n\")", 0),
     ], "allowlisted_template"),
    ("check-parser-not-shared-class", "src/csk/cli.py", [
        ("    parser: argparse.ArgumentParser = _CskArgumentParser(\n        prog=\"csk check\",",
         "    parser: argparse.ArgumentParser = argparse.ArgumentParser(\n        prog=\"csk check\",", 0),
     ], "shared_error_path"),
    ("template-unrecognized-echoes-value", UE, [
        ("        return head + VALUE_NOT_SHOWN", "        return message", 0),
     ], "unrecognized or template_rebuilds"),
    ("template-unexpected-option-echoes-value", UE, [
        ("        return head + VALUE_NOT_SHOWN", "        return message", 1),
     ], "template_rebuilds"),
    ("template-invalid-choice-echoes-value", UE, [
        ("                return f\"{prefix}invalid choice: {VALUE_NOT_SHOWN} (choose from {choices})\"",
         "                return f\"{prefix}{inner}\"", 0),
     ], "invalid_choice or template_rebuilds"),
    ("template-typed-value-echoes-value", UE, [
        ("            return f\"{prefix}invalid {type_name} value: {VALUE_NOT_SHOWN}\"",
         "            return prefix + inner", 0),
     ], "typed_value or template_rebuilds"),
    ("template-ignored-explicit-echoes-value", UE, [
        ("        return f\"{prefix}ignored explicit argument {VALUE_NOT_SHOWN}\"",
         "        return prefix + inner", 0),
     ], "ignored_explicit or template_rebuilds"),
    ("template-ambiguous-echoes-value", UE, [
        ("            return f\"{head}{VALUE_NOT_SHOWN} could match {', '.join(matches)}\"",
         "            return message", 0),
     ], "ambiguous or template_rebuilds"),
    ("generic-fallback-prints-raw-message", UE, [
        ("    return GENERIC_MESSAGE if rebuilt is None else rebuilt",
         "    return message if rebuilt is None else rebuilt", 0),
     ], "no_template"),
    ("static-message-prefix-match", UE, [
        ("    if message in static_messages:\n        return message",
         "    if any(message.startswith(item) for item in static_messages):\n        return message", 0),
     ], "no_template or static_texts"),
    ("static-inner-prefix-match", UE, [
        ("    if _ARITY_RE.fullmatch(inner) or inner in static_messages:",
         "    if _ARITY_RE.fullmatch(inner) or any(inner.startswith(item) for item in static_messages):", 0),
     ], "no_template or static_texts"),
    ("arity-match-not-fullmatch", UE, [
        ("    if _ARITY_RE.fullmatch(inner) or inner in static_messages:",
         "    if _ARITY_RE.match(inner) or inner in static_messages:", 0),
     ], "no_template or static_texts"),
    ("required-names-unvalidated", UE, [
        ("        if required and all(item in names for item in required):",
         "        if required:", 0),
     ], "no_template or static_texts"),
    ("ambiguous-matches-unvalidated", UE, [
        ("        if all(item in known for item in matches):", "        if True:", 0),
     ], "imitating"),
    ("ambiguous-split-at-first-separator", UE, [
        ("        matches = message.rsplit(\" could match \", 1)[1].split(\", \")",
         "        matches = message.split(\" could match \", 1)[1].split(\", \")", 0),
     ], "imitating"),
    ("not-allowed-with-name-unvalidated", UE, [
        ("    if inner.startswith(other) and inner[len(other) :] in names:", "    if inner.startswith(other):", 0),
     ], "imitating"),
    ("installer-error-echoes-env-name", "src/csk/installer.py", [
        ("                f\"variable configured for build_https scope {rule.scope!r} is \"\n                \"not set\"",
         "                f\"variable {rule.token_env!r} configured for build_https scope {rule.scope!r} is \"\n                \"not set\"", 0),
     ], "installer_does_not_echo"),
    ("token-env-validation-dropped", "src/csk/cli.py", [
        ("    if _TOKEN_ENV_NAME_RE.fullmatch(value) is None:", "    if False:", 0),
     ], "token_env_invalid"),
    ("token-env-match-not-fullmatch", "src/csk/cli.py", [
        ("    if _TOKEN_ENV_NAME_RE.fullmatch(value) is None:",
         "    if _TOKEN_ENV_NAME_RE.match(value) is None:", 0),
     ], "token_env_invalid"),
    ("env-regex-allows-dash", "src/csk/cli.py", [
        ('_TOKEN_ENV_NAME_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]{0,127}")',
         '_TOKEN_ENV_NAME_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_-]{0,127}")', 0),
     ], "token_env_invalid"),
    ("env-regex-allows-digit-first", "src/csk/cli.py", [
        ('_TOKEN_ENV_NAME_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]{0,127}")',
         '_TOKEN_ENV_NAME_RE = re.compile(r"[A-Za-z0-9_]{1,128}")', 0),
     ], "token_env_invalid"),
    # Rev-6: panel survivors narrowed by hostile value shape, depth and NAME.
    ('raw-echo-if-c0', UE, [("    if message in static_messages:", '    if message in static_messages or any(ord(c) < 32 and c not in "\\n\\t" for c in message):', 0)], "rev6_hostile_values"),
    ('raw-echo-if-ansi', UE, [("    if message in static_messages:", '    if message in static_messages or "\\x1b" in message:', 0)], "rev6_hostile_values"),
    ('raw-echo-if-surrogate', UE, [("    if message in static_messages:", '    if message in static_messages or any("\\ud800" <= c <= "\\udfff" for c in message):', 0)], "rev6_hostile_values"),
    ('raw-echo-if-line-separators', UE, [("    if message in static_messages:", '    if message in static_messages or any(c in message for c in "\\u0085\\u2028\\u2029"):', 0)], "rev6_hostile_values"),
    ('raw-echo-if-bidi', UE, [("    if message in static_messages:", '    if message in static_messages or any(c in message for c in "\\u202e\\u2066"):', 0)], "rev6_hostile_values"),
    ('raw-echo-if-multiline', UE, [("    if message in static_messages:", '    if message in static_messages or "\\n" in message:', 0)], "rev6_hostile_values"),
    ('raw-echo-if-very-long', UE, [("    if message in static_messages:", '    if message in static_messages or len(message) > 4096:', 0)], "rev6_hostile_values"),
    ('fallback-raw-if-multiline', UE, [("    return GENERIC_MESSAGE if rebuilt is None else rebuilt", '    return (message if "\\n" in message else GENERIC_MESSAGE) if rebuilt is None else rebuilt', 0)], "rev6_hostile_unmatched"),
    ('fallback-raw-if-ansi', UE, [("    return GENERIC_MESSAGE if rebuilt is None else rebuilt", '    return (message if "\\x1b" in message else GENERIC_MESSAGE) if rebuilt is None else rebuilt', 0)], "rev6_hostile_unmatched"),
    ('installer-error-echoes-lowercase-env', "src/csk/installer.py", [('                "not set"\n            )', '                "not set" + (" (" + rule.token_env + ")" if rule.token_env.islower() else "")\n            )', 0)], "installer_does_not_echo"),
    ('installer-error-echoes-mixed-env', "src/csk/installer.py", [('                "not set"\n            )', '                "not set" + (" (" + rule.token_env + ")" if not rule.token_env.islower() and not rule.token_env.isupper() else "")\n            )', 0)], "installer_does_not_echo"),
    ("template-ambiguous-echoes-nonascii", UE, [
        ("            return f\"{head}{VALUE_NOT_SHOWN} could match {', '.join(matches)}\"",
         "            typed = message[len(head):message.rindex(' could match ')]\n            return f\"{head}{typed if not typed.isascii() else VALUE_NOT_SHOWN} could match {', '.join(matches)}\"", 0),
     ], "rev6_every_template"),
    ("template-typed-echoes-dash", UE, [
        ("            return f\"{prefix}invalid {type_name} value: {VALUE_NOT_SHOWN}\"",
         "            raw = inner.split(' value: ', 1)[1]\n            return prefix + inner if raw.startswith(\"'-\") else f\"{prefix}invalid {type_name} value: {VALUE_NOT_SHOWN}\"", 0),
     ], "rev6_every_template"),
    ("template-unexpected-echoes-equals", UE, [
        ("        return head + VALUE_NOT_SHOWN", "        return message if '=' in message else head + VALUE_NOT_SHOWN", 1),
     ], "rev6_every_template"),
)


def _replace_occurrence(source: str, before: str, after: str, index: int) -> str:
    pieces = source.split(before)
    if len(pieces) <= index + 1:
        raise AssertionError("mutation anchor is missing")
    return before.join(pieces[:index + 1]) + after + before.join(pieces[index + 1:])


def main() -> int:
    with tempfile.TemporaryDirectory(prefix="csk-c1-mutants-") as tmp:
        sandbox = Path(tmp)
        shutil.copytree(ROOT / "src", sandbox / "src", ignore=shutil.ignore_patterns("__pycache__"))
        (sandbox / "tests").mkdir()
        for name in ("test_audit_secrets.py", "test_audit_secrets_review.py", "test_audit_publish.py",
                     "test_token_redaction_matrix.py", "test_usage_error_closure.py"):
            shutil.copyfile(ROOT / "tests" / name, sandbox / "tests" / name)
        # Carry conftest and all support modules, including transitive imports.
        # A new fixture helper must not require updating this harness's manifest.
        for helper in (ROOT / "tests").glob("*.py"):
            if not helper.name.startswith("test_") and not helper.name.endswith("_test.py"):
                shutil.copyfile(helper, sandbox / "tests" / helper.name)
        shutil.copyfile(ROOT / "CHANGELOG.md", sandbox / "CHANGELOG.md")
        (sandbox / "tests" / "fixtures").mkdir()
        shutil.copyfile(ROOT / "tests" / "fixtures" / "c1-redaction-matrix.json",
                        sandbox / "tests" / "fixtures" / "c1-redaction-matrix.json")
        (sandbox / "pytest.ini").write_text("[pytest]\npythonpath = src\n", encoding="utf-8")
        (sandbox / "pytest-tmp").mkdir()
        # Sandbox-local TMPDIR: pytest's garbage-dir cleanup warnings mention
        # "error" and would otherwise misclassify kills (kill detection scans
        # stdout for " failed" without " error").
        env = dict(os.environ, PYTEST_DISABLE_PLUGIN_AUTOLOAD="1", PYTHONDONTWRITEBYTECODE="1",
                   TMPDIR=str(sandbox / "pytest-tmp"))

        def run(expression: str | None = None, timeout: int = 120) -> subprocess.CompletedProcess[str]:
            argv = [sys.executable, "-m", "pytest", "-q", "--tb=no", "-r", "f", "-p", "no:cacheprovider", TEST, REVIEW_TEST,
                   MATRIX_TEST, CLOSURE_TEST]
            if expression:
                argv += ["-k", expression]
            return subprocess.run(argv, cwd=sandbox, env=env, capture_output=True, text=True, timeout=timeout)

        control = run(timeout=300)
        if control.returncode != 0:
            print(f"CONTROL setup failure: pytest exit {control.returncode}; no mutants run or counted as killed",
                  file=sys.stderr, flush=True)
            print(control.stdout, end="", file=sys.stderr, flush=True)
            print(control.stderr, end="", file=sys.stderr, flush=True)
            return 1
        print("CONTROL:", control.stdout.strip(), flush=True)
        wanted = sys.argv[1:]
        selected = [entry for entry in MUTANTS
                    if not wanted or any(bit in entry[0] for bit in wanted)]
        print(f"MUTANTS: {len(selected)}/{len(MUTANTS)} selected", flush=True)
        for name, file, replacements, expression in selected:
            target = sandbox / file
            original = target.read_text(encoding="utf-8")
            mutated = original
            for before, after, index in replacements:
                mutated = _replace_occurrence(mutated, before, after, index)
            try:
                target.write_text(mutated, encoding="utf-8")
                result = run(expression)
                # Exit 1 is an assertion failure. Collection/startup errors do
                # not count as killing a mutant.
                killed = result.returncode == 1 and " failed" in result.stdout and " error" not in result.stdout
                print(name + ": " + ("KILLED" if killed else "NOT KILLED") + "; " + result.stdout.strip().splitlines()[-1], flush=True)
                print(f"  pytest exit: {result.returncode}", flush=True)
                for line in result.stdout.splitlines():
                    if line.startswith("FAILED "):
                        print("  " + line, flush=True)
                if not killed:
                    return 1
            finally:
                target.write_text(original, encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
