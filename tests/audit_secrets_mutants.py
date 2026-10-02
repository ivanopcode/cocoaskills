"""Run C1 narrowing mutants in isolated copies, without Git or network use.

Usage: python tests/audit_secrets_mutants.py
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
    ("refuse-only-build-https", "src/csk/cli.py", [
        (REFUSAL, 'nargs="?", help=argparse.SUPPRESS,', 1),
        ('and option not in safe_token_options', 'and option not in safe_token_options and arguments[:1] != ["audit"]', 0)],
     "token_option_is_refused_before_dispatch and audit"),
    ("refuse-only-audit", "src/csk/cli.py", [
        (REFUSAL, 'nargs="?", help=argparse.SUPPRESS,', 0),
        ('and option not in safe_token_options', 'and option not in safe_token_options and arguments[:2] != ["config", "build-https"]', 0)],
     "token_option_is_refused_before_dispatch and build-https"),
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
    ("token-prefix-allows-tok-only", "src/csk/cli.py", [
        ('and option not in safe_token_options', 'and option not in safe_token_options and option != "--tok"', 0),
     ], "token_prefix_never_discloses_on_any_subcommand"),
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
        for name in ("test_audit_secrets.py", "test_audit_secrets_review.py", "test_audit_publish.py", "conftest.py", "draft_sources_accounting.py"):
            shutil.copyfile(ROOT / "tests" / name, sandbox / "tests" / name)
        (sandbox / "pytest.ini").write_text("[pytest]\npythonpath = src\n", encoding="utf-8")
        env = dict(os.environ, PYTEST_DISABLE_PLUGIN_AUTOLOAD="1", PYTHONDONTWRITEBYTECODE="1")

        def run(expression: str | None = None) -> subprocess.CompletedProcess[str]:
            argv = [sys.executable, "-m", "pytest", "-q", "--tb=no", "-r", "f", "-p", "no:cacheprovider", TEST, REVIEW_TEST]
            if expression:
                argv += ["-k", expression]
            return subprocess.run(argv, cwd=sandbox, env=env, capture_output=True, text=True, timeout=60)

        control = run()
        print("CONTROL:", control.stdout.strip().splitlines()[-1], flush=True)
        if control.returncode != 0:
            return 1
        for name, file, replacements, expression in MUTANTS:
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
