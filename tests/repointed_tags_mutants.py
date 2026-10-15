"""Run bounded fetch-policy mutants against production CLI tests in copies."""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / ".temp" / "repointed-tags-mutants"
MUTANTS = (
    ("non-forced-fetch", "src/csk/git_ops.py", (
        ('"fetch", "--force", "--prune", "--", remote,', '"fetch", "--prune", "--", remote,'),
        ('f"+refs/heads/*:refs/remotes/{remote}/*"', 'f"refs/heads/*:refs/remotes/{remote}/*"'),
        ('"+refs/tags/*:refs/tags/*"', '"refs/tags/*:refs/tags/*"'),
    ), "test_cli_repointed_tags"),
    ("legacy-fetch-all", "src/csk/git_ops.py", (
        ('"fetch", "--force", "--prune", "--", remote,\n        f"+refs/heads/*:refs/remotes/{remote}/*",\n        "+refs/tags/*:refs/tags/*",',
         '"fetch", "--all", "--tags", "--prune",'),
    ), "test_cli_repointed_tags"),
    ("strict-admits-v1-only", "src/csk/installer.py", (
        ('if warnings:\n        raise InstallError("moved_tag: "',
         'if warnings and not all(": v1 " in warning for warning in warnings):\n        raise InstallError("moved_tag: "'),
    ), "test_cli_strict_refuses_single_moved_v1"),
    ("ambiguous-admits-first-remote", "src/csk/git_ops.py", (
        ('elif len(remotes) == 1:', 'elif len(remotes) >= 1:'),
    ), "test_fetch_remote_ownership and ambiguous"),
    ("remote-option-reinterpretation", "src/csk/git_ops.py", (
        ('"fetch", "--force", "--prune", "--", remote,',
         '"fetch", "--force", "--prune", remote,'),
    ), ""),
)


def main() -> int:
    OUT.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="sandbox-", dir=OUT) as tmp:
        sandbox = Path(tmp)
        shutil.copytree(ROOT / "src", sandbox / "src", ignore=shutil.ignore_patterns("__pycache__"))
        (sandbox / "tests").mkdir()
        (sandbox / "temp").mkdir()
        for name in ("conftest.py", "draft_sources_accounting.py", "test_repointed_tags.py"):
            shutil.copyfile(ROOT / "tests" / name, sandbox / "tests" / name)
        (sandbox / "pytest.ini").write_text("[pytest]\npythonpath = src\n", encoding="utf-8")
        env = dict(os.environ, PYTEST_DISABLE_PLUGIN_AUTOLOAD="1", PYTHONDONTWRITEBYTECODE="1")
        env.pop("PYTHONPATH", None)

        def check(name: str, selection: str = "") -> subprocess.CompletedProcess[str]:
            args = [sys.executable, "-m", "pytest", "-q", "--tb=short", "-r", "f",
                    "-p", "no:cacheprovider", "tests/test_repointed_tags.py",
                    "--basetemp", str(sandbox / "temp" / name)]
            if selection:
                args += ["-k", selection]
            result = subprocess.run(args, cwd=sandbox, env=env, capture_output=True,
                                    text=True, timeout=180)
            (OUT / f"{name}.log").write_text(result.stdout + result.stderr, encoding="utf-8")
            return result

        baseline = check("baseline")
        print(f"baseline: exit={baseline.returncode}", flush=True)
        if baseline.returncode:
            return 1
        failed = False
        for name, filename, replacements, selection in MUTANTS:
            path = sandbox / filename
            original = path.read_text(encoding="utf-8")
            changed = original
            for before, after in replacements:
                if changed.count(before) != 1:
                    raise AssertionError(f"{name}: mutation anchor count is not one")
                changed = changed.replace(before, after, 1)
            path.write_text(changed, encoding="utf-8")
            try:
                result = check(name, selection)
            finally:
                path.write_text(original, encoding="utf-8")
            names = [line for line in result.stdout.splitlines() if line.startswith("FAILED ")]
            killed = result.returncode == 1 and bool(names) and "ERROR " not in result.stdout
            if name == "remote-option-reinterpretation":
                # Dropping only '--' must break exactly the hyphen-name cases;
                # ordinary origin/sole remotes and all older tests stay green.
                actual = {line.split(" ")[1] for line in names}
                expected = {
                    "tests/test_repointed_tags.py::test_cli_remote_name_is_positional"
                    f"[leading-hyphen-{scope}]" for scope in ("project", "global")
                }
                killed &= actual == expected
            print(f"{name}: {'KILLED' if killed else 'SURVIVOR/INVALID'} exit={result.returncode}", flush=True)
            for line in names:
                print(f"  {line}", flush=True)
            failed |= not killed
        return int(failed)


if __name__ == "__main__":
    raise SystemExit(main())
