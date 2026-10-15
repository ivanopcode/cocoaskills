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

Revision 6 cuts the tripwire to the surfaces that guard the original risk
-- a fixture writing ``user.*``/``commit.gpgsign`` into the enclosing
config -- and proves each fully: the common ``config`` and ``config.worktree``
(parsed key/value multisets minus ``branch.*``/``remote.*``), this checkout's
``HEAD`` and the branch it points to. Hooks, ``info/exclude`` and the index
are stated bounds, pinned here as non-detections. Every read distinguishes
missing from unreadable (one test per field). A sub-pytest copies the REAL
``tests/conftest.py`` and session module verbatim into a throwaway
repository to prove the failure path: a ``user.email`` write fails the
session, a ``branch.x.remote`` write passes it.

The other sub-pytest tests drive a throwaway conftest sharing the same
``session_isolated`` implementation: hostile parent variables must not reach
grandchildren, a deliberate config write to the enclosing repo must fail the
session, and a separator-bearing tmp root must fail it clearly.
"""

from __future__ import annotations

import os
import random
import re
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Callable

import pytest

import git_session_isolation as gsi

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
    exact = shells["python child"]
    home, xdg = exact.get("HOME"), exact.get("XDG_CONFIG_HOME")
    assert home and home == xdg and "csk-git-session-env" in home
    assert Path(exact["GIT_CONFIG_GLOBAL"]).read_bytes() == b""
    if "sh -c env" in shells:
        # MSYS/Cygwin sh on Windows rewrites path-valued variables to
        # POSIX form (/c/...), so the sh leg pins by substring: both home
        # variables still name the session-private dir, exactly once each.
        sh_home = shells["sh -c env"].get("HOME") or ""
        sh_xdg = shells["sh -c env"].get("XDG_CONFIG_HOME") or ""
        assert sh_home.count("csk-git-session-env") == 1, sh_home
        assert sh_xdg.count("csk-git-session-env") == 1, sh_xdg
        assert "global-config" in (shells["sh -c env"].get("GIT_CONFIG_GLOBAL") or "")
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


# ---------------------------------------------------------------------------
# Revision 5: production wiring, ceiling, and the layout-aware tripwire.
# ---------------------------------------------------------------------------

PINNED_GIT_VARS = {
    "GIT_CONFIG_NOSYSTEM",
    "GIT_CONFIG_GLOBAL",
    "GIT_CEILING_DIRECTORIES",
}

LAYOUTS = ("normal", "linked")

_IDENTITY = (
    "-c", "user.name=Tripwire Test",
    "-c", "user.email=tripwire@example.invalid",
    "-c", "commit.gpgsign=false",
)


def _g(cwd: Path, *args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    """Raw git in ``cwd`` with the session environment inherited unchanged."""
    proc = subprocess.run(
        ["git", *_IDENTITY, *args],
        cwd=cwd,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=120,
        check=False,
    )
    if check:
        assert proc.returncode == 0, f"git {args} in {cwd}: {proc.stderr}"
    return proc


def _common_dir(checkout: Path) -> Path:
    out = _g(checkout, "rev-parse", "--git-common-dir").stdout.strip()
    return Path(os.path.normpath(os.path.join(checkout, out)))


def _git_dir(checkout: Path) -> Path:
    return Path(_g(checkout, "rev-parse", "--absolute-git-dir").stdout.strip())


def _make_checkout(tmp_path: Path, layout: str) -> Path:
    """A repository with one commit; ``layout`` picks the checkout under test.

    ``linked``: the returned checkout is a linked worktree of ``main`` (the
    board layout), whose private git dir sits under ``main/.git/worktrees``
    while branches, tags, hooks and ``config`` live in the common dir.
    """
    main = tmp_path / "main"
    main.mkdir()
    _g(main, "init", "-q", "-b", "main")
    (main / "tracked.txt").write_text("tracked\n", encoding="utf-8")
    _g(main, "add", "tracked.txt")
    _g(main, "commit", "-q", "-m", "init")
    _g(main, "branch", "other")
    _g(main, "config", "extensions.worktreeConfig", "true")
    checkout = main
    if layout == "linked":
        checkout = tmp_path / "linked"
        _g(main, "worktree", "add", "-q", "-b", "linked-branch", str(checkout))
    (checkout / "pending.txt").write_text("pending\n", encoding="utf-8")
    return checkout


def _snapshot(path: Path) -> dict[str, dict]:
    snap = gsi.snapshot_repositories([path])
    assert len(snap) == 1, snap
    return snap


def _branch(checkout: Path) -> str:
    return _g(checkout, "symbolic-ref", "--short", "HEAD").stdout.strip()


def _mut_config(checkout: Path) -> None:
    _g(checkout, "config", "user.email", "tripwire@example.invalid")


def _mut_config_worktree(checkout: Path) -> None:
    _g(checkout, "config", "--worktree", "user.email", "tripwire@example.invalid")


def _mut_head(checkout: Path) -> None:
    _g(checkout, "switch", "-q", "other")


def _mut_head_target(checkout: Path) -> None:
    _g(checkout, "commit", "-q", "--allow-empty", "-m", "moved")


FIELD_MUTATIONS: dict[str, Callable[[Path], None]] = {
    "config": _mut_config,
    "config.worktree": _mut_config_worktree,
    "HEAD": _mut_head,
    "HEAD-target": _mut_head_target,
}

#: Config writes the tripwire MUST report: the original bug's keys plus
#: names that merely look like the legitimately shared sections.
GUARDED_KEYS = (
    ("user.name", "Golden Test"),
    ("user.email", "golden@example.com"),
    ("commit.gpgsign", "false"),
    ("core.autocrlf", "false"),
    ("branchy.key", "corrupted"),
    ("remotes.key", "corrupted"),
    ("core.branch", "corrupted"),
    ("other.remote", "corrupted"),
    ("custom.remote.url", "corrupted"),
    ("custom.branch.name", "corrupted"),
)


def test_field_mutation_table_covers_every_watched_field() -> None:
    """A watched field with no tripwire case would be an unproven surface."""
    assert set(FIELD_MUTATIONS) == set(gsi.WATCHED_FIELDS)
    assert len(gsi.WATCHED_FIELDS) == len(set(gsi.WATCHED_FIELDS))


def test_real_session_isolation_is_wired(
    tmp_path_factory: pytest.TempPathFactory,
) -> None:
    """The REAL session fixture ran: marker, pinned three, ceiling = basetemp.

    Ordinary test, no sub-pytest. Removing the autouse fixture from
    ``tests/conftest.py`` drops the marker; changing the ceiling pin breaks
    the equality; a leaked ``GIT_*`` breaks the exact-set check.
    """
    assert os.environ.get(gsi.SESSION_MARKER) == "1"
    git_vars = {key for key in os.environ if key.upper().startswith("GIT_")}
    assert git_vars == PINNED_GIT_VARS
    basetemp = Path(tmp_path_factory.getbasetemp()).resolve()
    assert os.environ["GIT_CEILING_DIRECTORIES"] == str(basetemp)
    assert os.environ["GIT_CONFIG_NOSYSTEM"] == "1"
    assert Path(os.environ["GIT_CONFIG_GLOBAL"]).read_bytes() == b""
    # HOME itself is re-pointed per test by the ``stable_env`` autouse
    # fixture; the session-private directory survives in XDG_CONFIG_HOME.
    assert "csk-git-session-env" in os.environ["XDG_CONFIG_HOME"]
    assert Path(os.environ["GIT_CONFIG_GLOBAL"]).parent == Path(os.environ["XDG_CONFIG_HOME"])


def test_real_session_git_refuses_uninitialised_directory(tmp_path: Path) -> None:
    """Git run in an un-inited fixture directory refuses; it never climbs out."""
    bare_dir = tmp_path / "uninitialised"
    bare_dir.mkdir()
    proc = _g(bare_dir, "rev-parse", "--git-dir", check=False)
    assert proc.returncode != 0, proc.stdout
    assert "not a git repository" in proc.stderr


def test_ceiling_stops_discovery_at_the_pinned_directory(tmp_path: Path) -> None:
    """Un-inited directory under a sentinel repository: git refuses, sentinel untouched.

    Drives the production environment builder with the ceiling inside a
    sentinel repository and hostile ``GIT_DIR``/``GIT_WORK_TREE`` in the
    parent. The control proves the sentinel IS discoverable without the
    ceiling, so the refusal is the ceiling's doing.
    """
    sentinel = (tmp_path / "sentinel").resolve()
    sentinel.mkdir()
    _g(sentinel, "init", "-q", "-b", "main")
    ceiling = sentinel / "ceil"
    work = ceiling / "work"
    work.mkdir(parents=True)
    private = tmp_path / "private"
    private.mkdir()
    (private / "global-config").write_bytes(b"")
    config_before = (sentinel / ".git" / "config").read_bytes()

    hostile = {
        **os.environ,
        "GIT_DIR": str(sentinel / ".git"),
        "GIT_WORK_TREE": str(sentinel),
    }
    snapshot_of_base = dict(hostile)
    env = gsi.session_git_environment(hostile, private, ceiling=ceiling)
    assert hostile == snapshot_of_base, "the builder mutated its input"
    assert "GIT_DIR" not in env and "GIT_WORK_TREE" not in env

    def run_git(use_env: dict[str, str], *args: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            ["git", *args],
            cwd=work,
            env=use_env,
            capture_output=True,
            text=True,
            timeout=60,
            check=False,
        )

    uncapped = {k: v for k, v in env.items() if k != "GIT_CEILING_DIRECTORIES"}
    control = run_git(uncapped, "rev-parse", "--absolute-git-dir")
    assert control.returncode == 0, control.stderr
    assert Path(control.stdout.strip()).resolve() == (sentinel / ".git").resolve()

    refused = run_git(env, "rev-parse", "--git-dir")
    assert refused.returncode != 0, refused.stdout
    write = run_git(env, "config", "tripwire.written", "yes")
    assert write.returncode != 0, write.stdout
    assert (sentinel / ".git" / "config").read_bytes() == config_before


# ---------------------------------------------------------------------------
# Revision 6: the cut tripwire -- every watched field fully proven.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("layout", LAYOUTS)
@pytest.mark.parametrize("field", gsi.WATCHED_FIELDS)
def test_tripwire_detects_each_watched_field(
    tmp_path: Path, layout: str, field: str
) -> None:
    """One tripwire case per watched field, in both checkout layouts.

    Exactly the mutated field is reported: a blinded reader reports nothing
    and fails, and a mutation that also moves a sibling field would show up
    as an extra name.
    """
    checkout = _make_checkout(tmp_path, layout)
    before = _snapshot(checkout)
    FIELD_MUTATIONS[field](checkout)
    after = _snapshot(checkout)

    assert {name for _, name in gsi.changed_fields(before, after)} == {field}
    rendered = "\n".join(gsi.diff_snapshots(before, after))
    assert field in rendered, rendered


@pytest.mark.parametrize("layout", LAYOUTS)
@pytest.mark.parametrize(("key", "value"), GUARDED_KEYS)
def test_tripwire_guards_user_and_lookalike_config_keys(
    tmp_path: Path, layout: str, key: str, value: str
) -> None:
    """``user.*`` (the original bug) and prefix lookalikes of the shared sections trip it.

    Kills "user.* excluded" and an exclusion matching ``branchy.``/
    ``remotes.``/``core.branch`` by substring or loose prefix.
    """
    checkout = _make_checkout(tmp_path, layout)
    before = _snapshot(checkout)
    _g(checkout, "config", key, value)
    after = _snapshot(checkout)

    assert {name for _, name in gsi.changed_fields(before, after)} == {"config"}
    assert key in "\n".join(gsi.diff_snapshots(before, after))


@pytest.mark.parametrize("layout", LAYOUTS)
def test_tripwire_ignores_legitimate_sibling_branch_and_remote_config(
    tmp_path: Path, layout: str
) -> None:
    """``push -u``, ``fetch``, ``remote add`` and upstream wiring do not trip it.

    Sibling worktrees and other agents append ``branch.*``/``remote.*`` to
    the shared config all day; failing the run on that was the rev-5 false
    trip. The control proves the raw common config really changed.
    """
    checkout = _make_checkout(tmp_path, layout)
    remote = tmp_path / "origin.git"
    remote.mkdir()
    _g(remote, "init", "-q", "--bare", "-b", "main")
    config_file = _common_dir(checkout) / "config"
    raw_before = config_file.read_bytes()
    before = _snapshot(checkout)

    _g(checkout, "remote", "add", "origin", str(remote))
    _g(checkout, "push", "-q", "-u", "origin", "HEAD")
    _g(checkout, "fetch", "-q", "origin")
    _g(checkout, "branch", "--set-upstream-to=origin/" + _branch(checkout))
    _g(checkout, "config", "--worktree", "branch.worktree-only.remote", "origin")
    _g(checkout, "config", "remote.pushdefault", "origin")

    assert config_file.read_bytes() != raw_before, "control: the raw config did not change"
    assert gsi.changed_fields(before, _snapshot(checkout)) == []


def test_tripwire_config_is_a_multiset_not_a_set_or_a_sequence(tmp_path: Path) -> None:
    """Order is irrelevant; multiplicity is not."""
    checkout = _make_checkout(tmp_path, "normal")
    _g(checkout, "config", "--add", "tripwire.k", "1")
    _g(checkout, "config", "--add", "tripwire.k", "2")
    config_file = _common_dir(checkout) / "config"
    before = _snapshot(checkout)

    text = config_file.read_text(encoding="utf-8")
    assert "\tk = 1\n\tk = 2\n" in text
    config_file.write_text(text.replace("\tk = 1\n\tk = 2\n", "\tk = 2\n\tk = 1\n"), encoding="utf-8")
    assert config_file.read_text(encoding="utf-8") != text, "control: the file was not reordered"
    assert gsi.changed_fields(before, _snapshot(checkout)) == [], "reordering must not trip"

    reordered = _snapshot(checkout)
    _g(checkout, "config", "--add", "tripwire.k", "1")
    assert {n for _, n in gsi.changed_fields(reordered, _snapshot(checkout))} == {"config"}


def test_tripwire_sees_a_value_change_of_an_existing_key(tmp_path: Path) -> None:
    """The watch compares values, not just key names."""
    checkout = _make_checkout(tmp_path, "normal")
    _g(checkout, "config", "user.email", "one@example.invalid")
    before = _snapshot(checkout)
    _g(checkout, "config", "user.email", "two@example.invalid")

    assert {n for _, n in gsi.changed_fields(before, _snapshot(checkout))} == {"config"}


@pytest.mark.parametrize("layout", LAYOUTS)
def test_tripwire_resolves_git_dir_and_common_dir_through_git(
    tmp_path: Path, layout: str
) -> None:
    checkout = _make_checkout(tmp_path, layout)
    nested = checkout / "deep" / "er"
    nested.mkdir(parents=True)
    for start in (checkout, nested, nested / "not-yet-created"):
        located = gsi.find_enclosing_repo(start)
        assert located is not None, start
        root, git_dir, common = located
        assert root is not None and root.resolve() == checkout.resolve()
        assert git_dir.resolve() == _git_dir(checkout).resolve()
        assert common.resolve() == _common_dir(checkout).resolve()
    if layout == "linked":
        assert git_dir.resolve() != common.resolve()
        assert common.resolve() == (tmp_path / "main" / ".git").resolve()
        assert git_dir.parent.name == "worktrees"
    else:
        assert git_dir.resolve() == common.resolve()
    record = next(iter(_snapshot(checkout).values()))
    assert Path(record["gitdir"]).resolve() == git_dir.resolve()
    assert Path(record["common"]).resolve() == common.resolve()


def test_tripwire_reports_changed_common_identity_with_unchanged_fields(tmp_path: Path) -> None:
    checkout = _make_checkout(tmp_path, "linked")
    git_dir = _git_dir(checkout)
    common = _common_dir(checkout)
    before = _snapshot(checkout)
    replacement = tmp_path / "replacement.git"
    shutil.copytree(common, replacement)
    (git_dir / "commondir").write_text(str(replacement.resolve()) + "\n", encoding="utf-8")
    after = _snapshot(checkout)

    old, new = next(iter(before.values())), next(iter(after.values()))
    assert old["fields"] == new["fields"]
    assert old["gitdir"] == new["gitdir"]
    assert Path(new["common"]).resolve() == replacement.resolve()
    assert {name for _, name in gsi.changed_fields(before, after)} == {"common"}


def test_tripwire_reports_a_changed_gitdir_identity(tmp_path: Path) -> None:
    """Re-pointing the checkout at another git dir is a breach even if every field matches."""
    checkout = _make_checkout(tmp_path, "normal")
    before = _snapshot(checkout)
    moved = tmp_path / "moved.git"
    (checkout / ".git").rename(moved)
    (checkout / ".git").write_text(f"gitdir: {moved}\n", encoding="utf-8")
    after = _snapshot(checkout)

    assert {name for _, name in gsi.changed_fields(before, after)} == {"gitdir", "common"}


def test_tripwire_watches_a_bare_repository(tmp_path: Path) -> None:
    bare = tmp_path / "bare.git"
    bare.mkdir()
    _g(bare, "init", "-q", "--bare", "-b", "main")
    before = gsi.snapshot_repositories([bare])
    assert list(before) == [str(bare.resolve())] or list(before) == [str(bare)]
    _g(bare, "config", "user.email", "tripwire@example.invalid")
    after = gsi.snapshot_repositories([bare])
    assert {name for _, name in gsi.changed_fields(before, after)} == {"config"}


def test_tripwire_outside_any_repository_snapshots_nothing(tmp_path: Path) -> None:
    probe = subprocess.run(
        ["git", "rev-parse", "--git-dir"],
        cwd=tmp_path,
        env=gsi._discovery_env(),
        capture_output=True,
        text=True,
        check=False,
    )
    if probe.returncode == 0:
        pytest.skip("the temp root sits inside a repository on this host")
    assert gsi.find_enclosing_repo(tmp_path) is None
    assert gsi.snapshot_repositories([tmp_path]) == {}


@pytest.mark.parametrize("layout", LAYOUTS)
def test_tripwire_ignores_read_only_git_activity(tmp_path: Path, layout: str) -> None:
    """``git status``/``git diff`` refresh stat data and must not trip it."""
    checkout = _make_checkout(tmp_path, layout)
    tracked = checkout / "tracked.txt"
    stat = tracked.stat()
    os.utime(tracked, ns=(stat.st_atime_ns, stat.st_mtime_ns + 7_000_000_000))
    before = _snapshot(checkout)

    for args in (
        ("status",),
        ("status", "--porcelain"),
        ("diff",),
        ("diff", "--stat"),
        ("diff", "--cached"),
        ("log", "--oneline"),
        ("branch", "--list"),
        ("ls-files", "--stage"),
        ("rev-parse", "HEAD"),
    ):
        _g(checkout, *args)

    assert gsi.changed_fields(before, _snapshot(checkout)) == []


def test_tripwire_ignores_read_only_git_activity_in_the_enclosing_checkout() -> None:
    """The same, against the checkout this suite really runs in.

    Runs in the real session, so the session teardown tripwire is the
    witness too: a false trip would fail the whole session here.
    """
    before = gsi.snapshot_repositories([TESTS_DIR])
    if not before:
        pytest.skip("tests run from an export with no enclosing repository")
    root = next(iter(before))
    _g(Path(root), "status", "--porcelain")
    _g(Path(root), "diff", "--stat")
    assert gsi.changed_fields(before, gsi.snapshot_repositories([TESTS_DIR])) == []


@pytest.mark.parametrize("layout", LAYOUTS)
def test_tripwire_bounds_are_not_watched(tmp_path: Path, layout: str) -> None:
    """Stated bounds, pinned as non-detections.

    Hooks, ``info/exclude`` and the index (cut in rev 6) and other
    branches, tags, packed-refs, reflogs and sibling work (shared state that
    concurrent legitimate git activity changes) are not reported. The
    checkout's own ``HEAD`` and branch value still are -- see
    ``test_tripwire_detects_each_watched_field``.
    """
    checkout = _make_checkout(tmp_path, layout)
    before = _snapshot(checkout)

    hooks = _common_dir(checkout) / "hooks"
    hooks.mkdir(exist_ok=True)
    (hooks / "pre-commit").write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    info = _common_dir(checkout) / "info"
    info.mkdir(exist_ok=True)
    with (info / "exclude").open("a", encoding="utf-8") as handle:
        handle.write("tripwire-ignore\n")
    _g(checkout, "add", "pending.txt")
    _g(checkout, "tag", "shared-tag")
    _g(checkout, "branch", "shared-branch")
    _g(checkout, "pack-refs", "--all")
    _g(checkout, "reflog", "expire", "--expire=now", "--all")
    if layout == "linked":
        sibling = tmp_path / "sibling"
        _g(tmp_path / "main", "worktree", "add", "-q", "-b", "sibling-branch", str(sibling))
        _g(sibling, "commit", "-q", "--allow-empty", "-m", "sibling work")
        _g(tmp_path / "main", "commit", "-q", "--allow-empty", "-m", "main work")

    assert gsi.changed_fields(before, _snapshot(checkout)) == []


# --- unreadable is never absent: one test per remaining field -------------


def _deny_reads_of(monkeypatch: pytest.MonkeyPatch, name: str) -> None:
    """Make ``Path.read_bytes`` raise ``PermissionError`` for files called ``name``."""
    real = Path.read_bytes

    def read_bytes(self: Path) -> bytes:
        if self.name == name:
            raise PermissionError(13, "Permission denied", str(self))
        return real(self)

    monkeypatch.setattr(Path, "read_bytes", read_bytes)


def _assert_unreadable_fails(field: str, checkout: Path) -> None:
    """Both snapshots equally unreadable still reports the field and a diff."""
    before = _snapshot(checkout)
    after = _snapshot(checkout)
    value = next(iter(after.values()))["fields"][field]
    assert isinstance(value, tuple) and value[0] == gsi.UNREADABLE, value
    assert before == after
    assert {name for _, name in gsi.changed_fields(before, after)} == {field}
    rendered = "\n".join(gsi.diff_snapshots(before, after))
    assert "UNREADABLE" in rendered and field in rendered, rendered
    assert "Permission denied" in rendered or field == "HEAD-target", rendered


@pytest.mark.parametrize("layout", LAYOUTS)
@pytest.mark.parametrize("field", ("config", "config.worktree", "HEAD"))
def test_unreadable_file_field_is_not_absent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, layout: str, field: str
) -> None:
    checkout = _make_checkout(tmp_path, layout)
    _g(checkout, "config", "--worktree", "tripwire.key", "present")
    _deny_reads_of(monkeypatch, field)
    _assert_unreadable_fails(field, checkout)


def test_unreadable_vs_missing_file_reads(tmp_path: Path) -> None:
    assert gsi._read_bytes(tmp_path / "missing") is None
    assert gsi._read_config(tmp_path / "missing") == []
    directory = tmp_path / "is-a-directory"
    directory.mkdir()
    for value in (gsi._read_bytes(directory), gsi._read_config(directory)):
        assert isinstance(value, tuple) and value[0] == gsi.UNREADABLE, value


def test_malformed_config_is_unreadable_not_empty(tmp_path: Path) -> None:
    """A config git cannot parse is UNREADABLE, never ``[]`` (an empty config)."""
    broken = tmp_path / "config"
    broken.write_text("[core\n\tbare = false\n", encoding="utf-8")
    value = gsi._read_config(broken)
    assert isinstance(value, tuple) and value[0] == gsi.UNREADABLE, value
    empty = tmp_path / "empty-config"
    empty.write_bytes(b"")
    assert gsi._read_config(empty) == []


@pytest.mark.parametrize("layout", LAYOUTS)
def test_broken_branch_ref_is_unreadable_not_unborn(tmp_path: Path, layout: str) -> None:
    """A corrupt branch ref is not an unborn HEAD (``rev-parse`` exit 1 cannot tell)."""
    checkout = _make_checkout(tmp_path, layout)
    before = _snapshot(checkout)
    ref = _common_dir(checkout) / "refs" / "heads" / _branch(checkout)
    ref.write_text("not a ref\n", encoding="utf-8")
    after = _snapshot(checkout)

    value = next(iter(after.values()))["fields"]["HEAD-target"]
    assert isinstance(value, tuple) and value[0] == gsi.UNREADABLE, value
    assert {name for _, name in gsi.changed_fields(before, after)} == {"HEAD-target"}
    assert {name for _, name in gsi.changed_fields(after, after)} == {"HEAD-target"}


def test_unborn_and_detached_head_are_absence_not_unreadable(tmp_path: Path) -> None:
    unborn = tmp_path / "unborn"
    unborn.mkdir()
    _g(unborn, "init", "-q", "-b", "main")
    fields = next(iter(_snapshot(unborn).values()))["fields"]
    assert fields["HEAD-target"] is None
    assert fields["HEAD"] == b"ref: refs/heads/main\n"

    (tmp_path / "repo").mkdir()
    checkout = _make_checkout(tmp_path / "repo", "normal")
    before = _snapshot(checkout)
    _g(checkout, "switch", "-q", "--detach", "HEAD")
    after = _snapshot(checkout)
    assert next(iter(after.values()))["fields"]["HEAD-target"] is None
    assert {name for _, name in gsi.changed_fields(before, after)} == {"HEAD", "HEAD-target"}
    _g(checkout, "commit", "-q", "--allow-empty", "-m", "detached move")
    assert {name for _, name in gsi.changed_fields(after, _snapshot(checkout))} == {"HEAD"}


def test_malformed_head_is_unreadable_head_target(tmp_path: Path) -> None:
    checkout = _make_checkout(tmp_path, "normal")
    git_dir = _git_dir(checkout)
    (git_dir / "HEAD").write_text("garbage\n", encoding="utf-8")
    value = gsi._read_head_target(git_dir)
    assert isinstance(value, tuple) and value[0] == gsi.UNREADABLE, value


@pytest.mark.parametrize(
    ("failing", "returncode", "stderr"),
    (
        ("symbolic-ref", 128, b"fatal: boom"),
        ("for-each-ref", 128, b"fatal: boom"),
        # Older gits let ``symbolic-ref`` succeed on a broken target ref and
        # only ``for-each-ref`` notices, with exit 0 and a warning.
        ("for-each-ref", 0, b"warning: ignoring broken ref refs/heads/main boom"),
    ),
)
def test_failed_git_child_is_unreadable_head_target(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    failing: str,
    returncode: int,
    stderr: bytes,
) -> None:
    """Each git child behind HEAD-target maps its failure to UNREADABLE, never to ``None``."""
    checkout = _make_checkout(tmp_path, "normal")
    real = gsi._git

    def flaky(args, *, cwd=None):  # type: ignore[no-untyped-def]
        if failing in args:
            return subprocess.CompletedProcess(args, returncode, stdout=b"", stderr=stderr)
        return real(args, cwd=cwd)

    monkeypatch.setattr(gsi, "_git", flaky)
    value = gsi._read_head_target(_git_dir(checkout))
    assert isinstance(value, tuple) and value[0] == gsi.UNREADABLE, value
    assert "boom" in value[1]


def test_unrecognised_discovery_failure_is_loud(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Only "not a git repository" means absence; anything else raises."""

    def refuse(args, *, cwd=None):  # type: ignore[no-untyped-def]
        return subprocess.CompletedProcess(
            args, 128, stdout=b"", stderr=b"fatal: detected dubious ownership"
        )

    monkeypatch.setattr(gsi, "_git", refuse)
    with pytest.raises(RuntimeError, match="dubious ownership"):
        gsi.find_enclosing_repo(tmp_path)


# --- the REAL tests/conftest.py failure path ------------------------------

REAL_CONFTEST_FILES = (
    "conftest.py",
    "git_session_isolation.py",
    "git_fixture_isolation.py",
    "draft_sources_accounting.py",
)

REAL_WRITE_TEST = '''\
import subprocess
from pathlib import Path


def test_writes_config():
    repo = Path(__file__).resolve().parents[1]
    for key, value in {pairs!r}:
        proc = subprocess.run(
            ["git", "config", key, value],
            cwd=repo,
            capture_output=True,
            text=True,
            timeout=60,
        )
        assert proc.returncode == 0, proc.stderr
'''

REAL_BREAK_REF_TEST = '''\
from pathlib import Path


def test_breaks_the_branch_ref():
    repo = Path(__file__).resolve().parents[1]
    (repo / ".git" / "refs" / "heads" / "main").write_text("not a ref\\n", encoding="utf-8")
'''


def _real_conftest_case(tmp_path: Path, test_source: str) -> Path:
    """A throwaway repository holding the REAL conftest and session module, verbatim.

    Nothing is stubbed: ``tests/conftest.py`` and ``git_session_isolation``
    (plus the two helper modules the conftest imports) are copied byte for
    byte, so the session fixture, its ``pytest.fail`` wiring and the
    tripwire are exactly the production ones.
    """
    case = tmp_path / "case"
    tests = case / "tests"
    tests.mkdir(parents=True)
    for args in (
        ("init", "-q", "-b", "main"),
        ("-c", "user.name=Case", "-c", "user.email=case@example.invalid",
         "-c", "commit.gpgsign=false", "commit", "-q", "--allow-empty", "-m", "base"),
    ):
        proc = subprocess.run(
            ["git", *args], cwd=case, capture_output=True, text=True, timeout=60, check=False
        )
        assert proc.returncode == 0, proc.stderr
    for name in REAL_CONFTEST_FILES:
        (tests / name).write_bytes((TESTS_DIR / name).read_bytes())
        assert (tests / name).read_bytes() == (TESTS_DIR / name).read_bytes()
    (tests / "test_case.py").write_text(test_source, encoding="utf-8")
    return case


def _run_real_case(case: Path) -> subprocess.CompletedProcess[str]:
    src = TESTS_DIR.parent / "src"
    existing = os.environ.get("PYTHONPATH")
    pythonpath = os.pathsep.join([str(src), existing] if existing else [str(src)])
    return _run_sub_pytest(case, "tests", env_extra={"PYTHONPATH": pythonpath})


def test_real_conftest_fails_the_session_on_a_user_config_write(tmp_path: Path) -> None:
    """A ``user.email`` write into the enclosing repo fails the REAL session.

    Kills a swallowed breach: ``pytest.fail`` in ``tests/conftest.py``
    replaced by a no-op, the tripwire result ignored in ``session_isolated``,
    or the except/wiring dropped. The test body itself passes, so the
    failure can only come from the session fixture.
    """
    case = _real_conftest_case(
        tmp_path,
        REAL_WRITE_TEST.format(pairs=[("user.email", "breach@example.invalid")]),
    )

    proc = _run_real_case(case)

    combined = proc.stdout + proc.stderr
    assert proc.returncode != 0, combined
    assert "1 passed" in combined, combined
    assert "TRIPWIRE" in combined and "user.email" in combined, combined
    assert "breach@example.invalid" in (case / ".git" / "config").read_text(encoding="utf-8")


def test_real_conftest_passes_a_branch_and_remote_only_write(tmp_path: Path) -> None:
    """The same session passes when only ``branch.*``/``remote.*`` change."""
    case = _real_conftest_case(
        tmp_path,
        REAL_WRITE_TEST.format(
            pairs=[("branch.x.remote", "origin"), ("remote.origin.url", "/nowhere")]
        ),
    )

    proc = _run_real_case(case)

    combined = proc.stdout + proc.stderr
    assert proc.returncode == 0, combined
    assert "1 passed" in combined and "TRIPWIRE" not in combined, combined
    assert "branch" in (case / ".git" / "config").read_text(encoding="utf-8")


def test_real_conftest_fails_the_session_on_an_unreadable_branch_ref(tmp_path: Path) -> None:
    """A read that fails mid-session fails the REAL session; it is not read as absent."""
    case = _real_conftest_case(tmp_path, REAL_BREAK_REF_TEST)

    proc = _run_real_case(case)

    combined = proc.stdout + proc.stderr
    assert proc.returncode != 0, combined
    assert "1 passed" in combined, combined
    assert "TRIPWIRE" in combined and "UNREADABLE" in combined, combined
    assert "HEAD-target" in combined, combined


@pytest.mark.parametrize("phase", ["start", "end"])
def test_real_conftest_reports_unreadable_config_as_tripwire(tmp_path: Path, phase: str) -> None:
    """Real git discovery cannot read a config directory on any supported OS."""
    replace_config = '''\
    config = Path(__file__).resolve().parents[1] / ".git" / "config"
    config.unlink()
    config.mkdir()
'''
    source = "from pathlib import Path\n\ndef test_unreadable_config():\n"
    source += replace_config if phase == "end" else "    pass\n"
    case = _real_conftest_case(tmp_path, source)
    if phase == "start":
        config = case / ".git" / "config"
        config.unlink()
        config.mkdir()

    proc = _run_real_case(case)

    combined = proc.stdout + proc.stderr
    assert proc.returncode != 0, combined
    assert "TRIPWIRE" in combined and "UNREADABLE" in combined, combined
    assert "config" in combined, combined
    assert "RuntimeError:" not in combined and "Traceback" not in combined, combined
    if phase == "end":
        assert "1 passed" in combined, combined


@pytest.mark.skipif(os.name == "nt", reason="POSIX permissions; config-directory case covers Windows")
def test_real_conftest_reports_permission_denied_config_as_tripwire(tmp_path: Path) -> None:
    if os.geteuid() == 0:
        pytest.skip("root can read mode-0 files")
    source = '''\
from pathlib import Path

def test_unreadable_config():
    (Path(__file__).resolve().parents[1] / ".git" / "config").chmod(0)
'''
    case = _real_conftest_case(tmp_path, source)
    try:
        proc = _run_real_case(case)
    finally:
        (case / ".git" / "config").chmod(0o600)
    combined = proc.stdout + proc.stderr
    assert proc.returncode != 0, combined
    assert "1 passed" in combined, combined
    assert "TRIPWIRE" in combined and "UNREADABLE" in combined, combined
    assert "Permission denied" in combined, combined
    assert "RuntimeError:" not in combined and "Traceback" not in combined, combined
