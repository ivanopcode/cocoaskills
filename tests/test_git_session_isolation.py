"""Session-level git isolation covers every child process however spawned.

Revision 4 of BUG-261004-473myt replaces the AST static gate -- a denylist
of spawn spellings the rev-3 panels proved cannot be complete -- with two
closed mechanisms in ``git_session_isolation`` (wired through the autouse
session fixture in ``tests/conftest.py``):

1. The session fixture removes every ``GIT_*`` variable from the pytest
   process and pins session-private git inputs, so every child inherits
   them through ``subprocess``, ``sh -c``, ``os.exec*``,
   ``posix_spawn`` or an absolute-path git alike.
2. The tripwire snapshots each repository enclosing the test run (and the
   session tmp root) at session start and fails the session with a diff on
   any change, detecting writes no prevention caught.

Each test below drives a real sub-pytest session (a throwaway conftest
sharing the same ``session_isolated`` implementation): hostile parent
variables must not reach grandchildren, a deliberate config write to the
enclosing repo must fail the session, and a separator-bearing tmp root
must fail it clearly.
"""

from __future__ import annotations

import os
import random
import re
import subprocess
import sys
from pathlib import Path

import pytest

TESTS_DIR = Path(__file__).resolve().parent

SUB_CONFTEST = """\
import sys

sys.path.insert(0, {tests_dir!r})

import pytest

import git_session_isolation


@pytest.fixture(scope="session", autouse=True)
def _sub_git_session_isolation(tmp_path_factory):
    try:
        with git_session_isolation.session_isolated(tmp_path_factory):
            yield
    except git_session_isolation.TripwireBreach as exc:
        pytest.fail(str(exc), pytrace=False)
"""

CHILD_ENV_TEST = '''\
import os
import subprocess
import sys
from pathlib import Path

ALLOWED_GIT_VARS = {
    "GIT_CONFIG_NOSYSTEM",
    "GIT_CONFIG_GLOBAL",
    "GIT_CEILING_DIRECTORIES",
}


def _child_env(argv):
    proc = subprocess.run(argv, capture_output=True, text=True, timeout=60, check=True)
    env = {}
    for line in proc.stdout.splitlines():
        if "=" in line:
            key, value = line.split("=", 1)
            env[key] = value
    return env


def test_child_processes_inherit_scrubbed_environment():
    # Control: without the session fixture this marker is absent and the
    # test fails here instead of passing vacuously.
    assert os.environ.get("CSK_GIT_SESSION_ISOLATED") == "1"
    try:
        shells = {"sh -c env": _child_env(["sh", "-c", "env"])}
    except (FileNotFoundError, OSError, subprocess.CalledProcessError):
        # No POSIX shell on this host (Windows): the python leg below
        # still proves raw-subprocess inheritance.
        shells = {}
    probe = "import os\\nfor k in sorted(os.environ): print(k + '=' + os.environ[k])"
    shells["python child"] = _child_env([sys.executable, "-c", probe])
    assert shells, "no child process could be spawned"
    for label, child in shells.items():
        leaked = sorted(
            key
            for key in child
            if key.upper().startswith("GIT_") and key not in ALLOWED_GIT_VARS
        )
        assert leaked == [], label
        assert child.get("GIT_CONFIG_NOSYSTEM") == "1", label
        assert child.get("GIT_CEILING_DIRECTORIES"), label
        assert child.get("CSK_GIT_SESSION_ISOLATED") == "1", label
        flat = "\\n".join(f"{key}={value}" for key, value in child.items())
        assert "hostile" not in flat, label
        home, xdg = child.get("HOME"), child.get("XDG_CONFIG_HOME")
        assert home and home == xdg and "csk-git-session-env" in home, label
        assert Path(child["GIT_CONFIG_GLOBAL"]).read_bytes() == b"", label
'''

WRITER_TEST = '''\
import subprocess
from pathlib import Path


def test_deliberate_config_write():
    repo = Path(__file__).resolve().parent
    proc = subprocess.run(
        ["git", "config", "tripwire.corrupted", "yes"],
        cwd=repo,
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert proc.returncode == 0, proc.stderr
'''

PASS_TEST = '''\
def test_nothing():
    assert True
'''


def _run_sub_pytest(
    case: Path, *args: str, env_extra: dict[str, str] | None = None
) -> subprocess.CompletedProcess[str]:
    """Run one throwaway pytest session in ``case`` and return it completed."""
    env = dict(os.environ)
    if env_extra:
        env.update(env_extra)
    env.pop("PYTEST_ADDOPTS", None)
    return subprocess.run(
        [sys.executable, "-m", "pytest", "-p", "no:cacheprovider", "-q", *args],
        cwd=case,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=180,
        env=env,
        check=False,
    )


def _parse_git_help_environment() -> set[str]:
    """Parse ``GIT_*`` names from ``git help environment`` where it exists.

    Best effort: without manual pages the command fails and the curated
    inventory below is the whole net.
    """
    try:
        completed = subprocess.run(
            ["git", "help", "environment"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=20,
            env={**os.environ, "GIT_PAGER": "cat", "MANPAGER": "cat", "PAGER": "cat"},
            check=False,
        )
    except (OSError, ValueError):
        return set()
    text = (completed.stdout or "") + "\n" + (completed.stderr or "")
    return set(re.findall(r"GIT_[A-Z0-9_]+", text))


def _hostile_names() -> list[str]:
    """Every documented ``GIT_*`` name plus seeded random ones."""
    from test_golden_fixture_git_isolation import DOCUMENTED_GIT_ENVIRONMENT_NAMES

    curated = {
        name for name in DOCUMENTED_GIT_ENVIRONMENT_NAMES if name.startswith("GIT_")
    }
    fuzz = random.Random(261004473)
    invented = {
        "GIT_CONFIG_FUTURE_CHANNEL",
        "GIT_TRACE_FUTURE",
        *(
            f"GIT_SESSION_FUZZ_{index:02d}_{fuzz.randrange(1 << 30):08x}"
            for index in range(16)
        ),
    }
    return sorted(curated | _parse_git_help_environment() | invented)


def test_session_env_scrubs_every_git_variable_from_all_children(
    tmp_path: Path,
) -> None:
    """No parent ``GIT_*`` reaches any grandchild, however it is spawned.

    A hostile parent environment -- every ``git help environment`` name,
    the curated git-docs inventory, random ``GIT_*`` names, plus hostile
    ``HOME``/``XDG_CONFIG_HOME`` -- feeds a real sub-pytest session whose
    only test spawns ``sh -c 'env'`` (raw, not through any runner) and a
    python child, asserting from inside them that nothing hostile leaked.
    """
    hostile = {name: f"hostile-leak-{name}" for name in _hostile_names()}
    assert len(hostile) > 80, "the hostile net collapsed"
    hostile["HOME"] = str(tmp_path / "hostile-home")
    hostile["XDG_CONFIG_HOME"] = str(tmp_path / "hostile-xdg")
    case = tmp_path / "case"
    case.mkdir()
    (case / "conftest.py").write_text(
        SUB_CONFTEST.format(tests_dir=str(TESTS_DIR)), encoding="utf-8"
    )
    (case / "test_child_env.py").write_text(CHILD_ENV_TEST, encoding="utf-8")

    proc = _run_sub_pytest(case, env_extra=hostile)

    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "1 passed" in proc.stdout, proc.stdout + proc.stderr


def test_tripwire_fails_session_when_enclosing_repo_config_changes(
    tmp_path: Path,
) -> None:
    """A deliberate config write to the enclosing repo fails the session.

    The sub-pytest session runs inside its own fresh repository; its only
    test writes one config key there through a raw ``git config`` (no
    redirector needed -- the write is local). The tripwire must fail the
    session with a diff naming the change.
    """
    case = tmp_path / "case"
    case.mkdir()
    setup = subprocess.run(
        ["git", "init", "-q", "-b", "main"],
        cwd=case,
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    assert setup.returncode == 0, setup.stderr
    identity = subprocess.run(
        ["git", "config", "user.name", "Tripwire Setup"],
        cwd=case,
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    assert identity.returncode == 0, identity.stderr
    (case / "conftest.py").write_text(
        SUB_CONFTEST.format(tests_dir=str(TESTS_DIR)), encoding="utf-8"
    )
    (case / "test_writer.py").write_text(WRITER_TEST, encoding="utf-8")

    proc = _run_sub_pytest(case)

    combined = proc.stdout + proc.stderr
    assert proc.returncode != 0, combined
    assert "TRIPWIRE" in combined, combined
    assert "corrupted" in combined, combined
    assert "corrupted" in (case / ".git" / "config").read_text(encoding="utf-8")


def test_colon_ceiling_path_fails_session_clearly(tmp_path: Path) -> None:
    """A separator-bearing session tmp root fails fast with its reason."""
    import git_session_isolation

    with pytest.raises(
        git_session_isolation.CeilingUnsafeError, match="GIT_CEILING_DIRECTORIES"
    ):
        git_session_isolation.ensure_ceiling_safe("some:thing")
    with pytest.raises(
        git_session_isolation.CeilingUnsafeError, match="path-list separator"
    ):
        git_session_isolation.ensure_ceiling_safe("some:thing")
    assert git_session_isolation.ensure_ceiling_safe(str(tmp_path)) == str(tmp_path)
    if os.name == "nt":
        assert git_session_isolation.ensure_ceiling_safe("C:\\Users\\x") == "C:\\Users\\x"

    nasty = tmp_path / f"colon{os.pathsep}dir"
    nasty.mkdir()
    case = tmp_path / "case"
    case.mkdir()
    (case / "conftest.py").write_text(
        SUB_CONFTEST.format(tests_dir=str(TESTS_DIR)), encoding="utf-8"
    )
    (case / "test_nothing.py").write_text(PASS_TEST, encoding="utf-8")

    proc = _run_sub_pytest(case, "--basetemp", str(nasty / "base"))

    combined = proc.stdout + proc.stderr
    assert proc.returncode != 0, combined
    assert "GIT_CEILING_DIRECTORIES" in combined, combined
