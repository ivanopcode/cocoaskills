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

Revision 5 proves the production wiring and the tripwire surfaces
directly: ordinary tests in the REAL session (not a sub-pytest) assert the
session marker, the pinned environment and the ceiling; a layout matrix
(normal checkout and linked worktree) drives one tripwire case per watched
field; read-only git activity must not trip it; and the stated bounds
(shared tags/branches/packed-refs, reflogs) are pinned as non-detections.

The sub-pytest tests drive a real sub-pytest session (a throwaway conftest
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


def _mut_config(checkout: Path) -> None:
    _g(checkout, "config", "tripwire.key", "corrupted")


def _mut_config_worktree(checkout: Path) -> None:
    _g(checkout, "config", "--worktree", "tripwire.key", "corrupted")


def _mut_head(checkout: Path) -> None:
    _g(checkout, "switch", "-q", "other")


def _mut_head_target(checkout: Path) -> None:
    _g(checkout, "commit", "-q", "--allow-empty", "-m", "moved")


def _mut_hooks(checkout: Path) -> None:
    hooks = _common_dir(checkout) / "hooks"
    hooks.mkdir(exist_ok=True)
    (hooks / "pre-commit").write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")


def _mut_exclude(checkout: Path) -> None:
    info = _common_dir(checkout) / "info"
    info.mkdir(exist_ok=True)
    with (info / "exclude").open("a", encoding="utf-8") as handle:
        handle.write("tripwire-ignore\n")


def _mut_index(checkout: Path) -> None:
    _g(checkout, "add", "pending.txt")


FIELD_MUTATIONS: dict[str, Callable[[Path], None]] = {
    "config": _mut_config,
    "config.worktree": _mut_config_worktree,
    "HEAD": _mut_head,
    "HEAD-target": _mut_head_target,
    "hooks": _mut_hooks,
    "info/exclude": _mut_exclude,
    "index": _mut_index,
}


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


def test_tripwire_watches_a_bare_repository(tmp_path: Path) -> None:
    bare = tmp_path / "bare.git"
    bare.mkdir()
    _g(bare, "init", "-q", "--bare", "-b", "main")
    before = gsi.snapshot_repositories([bare])
    assert list(before) == [str(bare.resolve())] or list(before) == [str(bare)]
    only = next(iter(before.values()))
    assert only["fields"]["index"] is None
    _g(bare, "config", "tripwire.key", "corrupted")
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
    """``git status``/``git diff`` refresh stat data and must not trip it.

    The tracked file is touched first so the index entry is stale; the
    control asserts git really rewrote the raw index bytes, i.e. the case
    exercises exactly what a stat/mtime-based index watch false-positives on.
    """
    checkout = _make_checkout(tmp_path, layout)
    tracked = checkout / "tracked.txt"
    stat = tracked.stat()
    os.utime(tracked, ns=(stat.st_atime_ns, stat.st_mtime_ns + 7_000_000_000))
    index_file = Path(
        os.path.normpath(os.path.join(checkout, _g(checkout, "rev-parse", "--git-path", "index").stdout.strip()))
    )
    raw_before = index_file.read_bytes()
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

    assert index_file.read_bytes() != raw_before, "control: git did not refresh the index"
    assert gsi.changed_fields(before, _snapshot(checkout)) == []


def test_tripwire_ignores_read_only_git_activity_in_the_enclosing_checkout() -> None:
    """The same, against the checkout this suite really runs in.

    Runs in the real session, so the session teardown tripwire is the
    witness too: a stat-sensitive watch would fail the whole session here.
    """
    before = gsi.snapshot_repositories([TESTS_DIR])
    if not before:
        pytest.skip("tests run from an export with no enclosing repository")
    root = next(iter(before))
    _g(Path(root), "status", "--porcelain")
    _g(Path(root), "diff", "--stat")
    assert gsi.changed_fields(before, gsi.snapshot_repositories([TESTS_DIR])) == []


@pytest.mark.parametrize("layout", LAYOUTS)
def test_tripwire_bound_shared_refs_and_reflogs_are_not_watched(
    tmp_path: Path, layout: str
) -> None:
    """Stated bound, pinned: other branches, tags, packed-refs and sibling work.

    Sibling worktrees and other agents change these legitimately; watching
    them would fail concurrent runs. A new tag, a new branch, packed refs
    and (linked layout) a commit in a sibling worktree on its own branch
    are not reported; the checkout's own HEAD and branch value are.
    """
    checkout = _make_checkout(tmp_path, layout)
    before = _snapshot(checkout)

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


def test_unreadable_is_not_absent(tmp_path: Path) -> None:
    """A failed read is reported as unreadable, never as a missing file."""
    assert gsi._read_bytes(tmp_path / "missing") is None
    directory = tmp_path / "is-a-directory"
    directory.mkdir()
    value = gsi._read_bytes(directory)
    assert isinstance(value, tuple) and value[0] == gsi.UNREADABLE

    checkout = _make_checkout(tmp_path / "repo", "normal") if (tmp_path / "repo").mkdir() is None else tmp_path
    before = _snapshot(checkout)
    (checkout / ".git" / "index").write_bytes(b"not an index")
    after = _snapshot(checkout)
    index = next(iter(after.values()))["fields"]["index"]
    assert isinstance(index, tuple) and index[0] == gsi.UNREADABLE, index
    assert {name for _, name in gsi.changed_fields(before, after)} == {"index"}


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
