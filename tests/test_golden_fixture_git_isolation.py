"""A test fixture can never change git state outside its own repository.

Regression suite for BUG-261004-473myt: the golden fixture's git helper
inherited ``os.environ``, so an ambient ``GIT_DIR`` (or a replay copy without
its own ``.git`` nested inside a checkout) redirected ``_init_repo``'s config
writes into the enclosing checkout's ``.git/config`` -- which then refused a
landing as an unsigned commit from ``golden@example.com``.

Revision 2 broadens the invariant to every git redirector, not just the six
repository-discovery variables: the config-destination family (``GIT_CONFIG``,
``GIT_CONFIG_COUNT`` pairs, ``GIT_CONFIG_GLOBAL``, ``GIT_CONFIG_SYSTEM``) and
``GIT_NAMESPACE`` are neutralized too, and every test helper that runs git
against a fixture repository routes through the one shared runner in
``git_fixture_isolation`` (wiring proven by
``test_fixture_git_runner_guard.py`` plus the R3 entry-point matrix below).

Revision 3 replaces the denylist with an allowlist: the shared runner
constructs every fixture git child environment from an allowlist only
(``PATH``/tmp/Windows baseline inherited; locale, home, empty global
config, empty template, ceiling pinned), attests that the fixture owns a
real ``.git`` directory (never following a gitfile pointer), and routes
owned repositories through explicit ``--git-dir``/``--work-tree`` flags.
The regression net is the full documented ``GIT_*`` inventory plus fuzz
names (``test_fixture_git_sweep_environment_variable_preserves_external_state``)
and the panels' reproductions as named tests.

Each test builds a sentinel repository, snapshots its git state as raw bytes
(never ``git config --get``, whose global fallback would mask a clobbered
local file), runs a fixture helper under hostile conditions, and proves the
snapshot is byte-identical afterwards. Redirectors that corrupt fixture reads
rather than redirecting writes (``GIT_CONFIG_COUNT``, ``GIT_CONFIG_GLOBAL``,
``GIT_CONFIG_SYSTEM``) are additionally pinned by fixture-side assertions --
commit authorship, build success -- because no sentinel snapshot can observe a
read corruption.
"""

from __future__ import annotations

import os
import random
import re
import subprocess
from pathlib import Path

import pytest

import cli_golden_v1_support as golden
import test_protocol_shards as shards
import test_release_script as release_mod
from conftest import init_git_repo, run
from git_fixture_isolation import (
    GIT_DISCOVERY_ENV_VARS,
    build_fixture_git_env,
    check_own_git_dir,
    discover_git_dir,
)

#: Every redirector the shared runner neutralizes: six repository-discovery
#: variables, the config-destination family, and the ref namespace.
ALL_REDIRECTORS: tuple[str, ...] = (
    *GIT_DISCOVERY_ENV_VARS,
    "GIT_CONFIG",
    "GIT_CONFIG_COUNT",
    "GIT_CONFIG_GLOBAL",
    "GIT_CONFIG_SYSTEM",
    "GIT_NAMESPACE",
)


def _write_hostile_global_config(path: Path, hooks_dir: Path) -> Path:
    """Write a hostile global/system config file with a failing commit hook.

    ``core.hooksPath`` is honored from every config level, needs no editor,
    keyring, or network, and fails ``git commit`` deterministically when it
    leaks: the pre-commit hook exits 1. Local fixture config never sets
    ``core.hooksPath``, so only the runner's override can silence this file.
    """
    hooks_dir.mkdir(parents=True, exist_ok=True)
    hook = hooks_dir / "pre-commit"
    hook.write_text("#!/bin/sh\nexit 1\n", encoding="utf-8")
    hook.chmod(0o755)
    path.write_text(
        f"[core]\n\thooksPath = {path.parent / hooks_dir.name}\n",
        encoding="utf-8",
    )
    return path


def _hostile_env(var: str, sentinel: Path, tmp_path: Path) -> dict[str, str]:
    git_dir = sentinel / ".git"
    if var == "GIT_CONFIG":
        return {"GIT_CONFIG": str(git_dir / "config")}
    if var == "GIT_CONFIG_COUNT":
        # A two-pair overlay: the kill is the fixture commit author -- COUNT
        # pairs behave like -c and beat the fixture's own local config.
        return {
            "GIT_CONFIG_COUNT": "2",
            "GIT_CONFIG_KEY_0": "user.name",
            "GIT_CONFIG_VALUE_0": "Intruder",
            "GIT_CONFIG_KEY_1": "user.email",
            "GIT_CONFIG_VALUE_1": "intruder@example.test",
        }
    if var in ("GIT_CONFIG_GLOBAL", "GIT_CONFIG_SYSTEM"):
        poison = _write_hostile_global_config(
            tmp_path / f"hostile-{var.lower()}", tmp_path / f"hostile-{var.lower()}-hooks"
        )
        return {var: str(poison)}
    if var == "GIT_NAMESPACE":
        return {"GIT_NAMESPACE": "evil-ns"}
    return {
        var: {
            "GIT_DIR": str(git_dir),
            "GIT_WORK_TREE": str(sentinel),
            "GIT_INDEX_FILE": str(git_dir / "index"),
            "GIT_COMMON_DIR": str(git_dir),
            "GIT_OBJECT_DIRECTORY": str(git_dir / "objects"),
            "GIT_ALTERNATE_OBJECT_DIRECTORIES": str(git_dir / "objects"),
        }[var]
    }


def _make_sentinel(root: Path) -> Path:
    """Create a sentinel repo with committed state, then snapshot it.

    Built with the hardened ``conftest`` helpers in the ambient (clean)
    environment, before the test installs anything hostile. Automatic
    maintenance and gc are disabled so no background git job can rewrite the
    object store mid-test and flake the snapshot comparison.
    """
    sentinel = init_git_repo(root / "sentinel")
    run(["git", "config", "sentinel.marker", "present"], sentinel)
    run(["git", "config", "maintenance.auto", "false"], sentinel)
    run(["git", "config", "gc.auto", "0"], sentinel)
    (sentinel / "sentinel.txt").write_text("sentinel\n", encoding="utf-8")
    run(["git", "add", "."], sentinel)
    run(["git", "commit", "-m", "sentinel"], sentinel)
    config = (sentinel / ".git" / "config").read_bytes()
    assert b"marker = present" in config, "sentinel marker did not land in its own config"
    return sentinel


def _snapshot(sentinel: Path) -> dict[str, bytes]:
    """Snapshot every file under the sentinel's ``.git`` as raw bytes."""
    git_dir = sentinel / ".git"
    return {
        path.relative_to(git_dir).as_posix(): path.read_bytes()
        for path in sorted(git_dir.rglob("*"))
        if path.is_file()
    }


def _assert_sentinel_unchanged(sentinel: Path, snapshot: dict[str, bytes]) -> None:
    assert _snapshot(sentinel) == snapshot


def _tracked_files(repo: Path) -> set[str]:
    return set(
        run(["git", "ls-tree", "-r", "HEAD", "--name-only"], repo).stdout.split()
    )


def _commit_author(repo: Path) -> str:
    return run(["git", "log", "-1", "--format=%an|%ae", "HEAD"], repo).stdout.strip()


@pytest.mark.parametrize("var", ALL_REDIRECTORS, ids=ALL_REDIRECTORS)
def test_golden_fixture_build_ignores_hostile_git_discovery_env(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, var: str
) -> None:
    sentinel = _make_sentinel(tmp_path)
    snapshot = _snapshot(sentinel)
    for key, value in _hostile_env(var, sentinel, tmp_path).items():
        monkeypatch.setenv(key, value)

    # The real entry point: the full pristine fixture, both repositories.
    paths = golden.build_pristine_fixture(tmp_path / "fixture")

    _assert_sentinel_unchanged(sentinel, snapshot)
    # The writes landed in the fixture, not merely nowhere.
    assert b"Golden Test" in (paths["project"] / ".git" / "config").read_bytes()
    assert b"Golden Test" in (
        paths["skills_root"] / "demo-skill" / ".git" / "config"
    ).read_bytes()
    # The fixture commits hold exactly the fixture files: a hostile work
    # tree or index must not smuggle foreign content into them either.
    assert _tracked_files(paths["project"]) == {"Skillfile.json", ".gitignore"}
    assert _tracked_files(paths["skills_root"] / "demo-skill") == {"SKILL.md"}
    # Read-corruption redirectors (COUNT pairs, global/system hooks) cannot
    # change authorship either: the committed identity is the fixture's own.
    assert _commit_author(paths["project"]) == "Golden Test|golden@example.com"
    assert _commit_author(paths["skills_root"] / "demo-skill") == (
        "Golden Test|golden@example.com"
    )
    # A leaked namespace would file the fixture branches under
    # refs/namespaces/ instead of refs/heads/ on git builds that remap
    # local refs; the loose head must exist exactly where git owns it.
    assert (paths["project"] / ".git" / "refs" / "heads" / "main").is_file()
    assert (
        paths["skills_root"] / "demo-skill" / ".git" / "refs" / "heads" / "main"
    ).is_file()


def _attempt_config_write(runner: str, nested: Path) -> int | None:
    """Drive one hardened runner's config write; return exit code when it cannot raise."""
    if runner == "golden":
        with pytest.raises(AssertionError):
            golden.run_git(["config", "user.name", "Intruder"], nested)
        return None
    if runner == "conftest":
        with pytest.raises(AssertionError):
            run(["git", "config", "user.name", "Intruder"], nested)
        return None
    if runner == "shards":
        with pytest.raises(subprocess.CalledProcessError):
            shards._git(nested, "config", "user.name", "Intruder")
        return None
    if runner == "release":
        completed = release_mod._git(nested, "config", "user.name", "Intruder")
        assert completed.returncode != 0, completed.stdout
        return completed.returncode
    raise AssertionError(f"unknown runner {runner!r}")


@pytest.mark.parametrize(
    "runner", ["golden", "conftest", "shards", "release"]
)
@pytest.mark.parametrize("var", [None, "GIT_CONFIG"], ids=["clean", "GIT_CONFIG"])
def test_git_config_cannot_escape_to_outer_repo_without_own_git_dir(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, runner: str, var: str | None
) -> None:
    # The replay-copy shape from the incident: a fixture directory nested
    # inside a checkout, without its own .git. Without the ceiling the config
    # write traverses up and lands in the outer repository; with GIT_CONFIG
    # inherited it lands there even when discovery is contained.
    sentinel = _make_sentinel(tmp_path)
    snapshot = _snapshot(sentinel)
    if var is not None:
        for key, value in _hostile_env(var, sentinel, tmp_path).items():
            monkeypatch.setenv(key, value)
    nested = sentinel / "replay-copy"
    nested.mkdir()

    _attempt_config_write(runner, nested)

    _assert_sentinel_unchanged(sentinel, snapshot)


@pytest.mark.parametrize("var", ["GIT_CONFIG", "GIT_DIR"], ids=["GIT_CONFIG", "GIT_DIR"])
def test_golden_run_git_explicit_redirector_override_cannot_escape(
    tmp_path: Path, var: str
) -> None:
    # The recording review's free hunt: an explicit per-call override, not
    # just ambient inheritance, redirected golden config writes into the
    # sentinel. The scrub runs after the env merge, so overrides are
    # neutralized exactly like ambient variables.
    sentinel = _make_sentinel(tmp_path)
    snapshot = _snapshot(sentinel)
    paths = golden.build_pristine_fixture(tmp_path / "fixture")

    golden.run_git(
        ["config", "probe.explicit", "present"],
        paths["project"],
        env=_hostile_env(var, sentinel, tmp_path),
    )

    _assert_sentinel_unchanged(sentinel, snapshot)
    local_config = (paths["project"] / ".git" / "config").read_bytes()
    assert b"[probe]" in local_config
    assert b"explicit = present" in local_config


def test_runner_neutralizes_alternate_object_directories(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Alternates are read-only: no write test can observe them. The probe is
    # a read -- a sentinel-only object must stay unknown inside the fixture
    # even while the hostile variable is ambient.
    sentinel = _make_sentinel(tmp_path)
    sentinel_oid = run(["git", "rev-parse", "HEAD"], sentinel).stdout.strip()
    fixture = init_git_repo(tmp_path / "fixture")
    (fixture / "own.txt").write_text("own\n", encoding="utf-8")
    run(["git", "add", "own.txt"], fixture)
    run(["git", "commit", "-m", "own"], fixture)
    own_oid = run(["git", "rev-parse", "HEAD"], fixture).stdout.strip()
    assert own_oid != sentinel_oid
    monkeypatch.setenv(
        "GIT_ALTERNATE_OBJECT_DIRECTORIES", str(sentinel / ".git" / "objects")
    )

    # Positive control: the fixture's own object resolves.
    assert run(["git", "cat-file", "-e", own_oid], fixture, check=False).returncode == 0
    # The sentinel-only object must not resolve through the hostile alternate.
    assert (
        run(["git", "cat-file", "-e", sentinel_oid], fixture, check=False).returncode
        != 0
    )


def _r3_hostile_env(shape: str, sentinel: Path) -> dict[str, str]:
    if shape == "index":
        return {"GIT_INDEX_FILE": str(sentinel / ".git" / "index")}
    if shape == "repo":
        return {
            "GIT_DIR": str(sentinel / ".git"),
            "GIT_WORK_TREE": str(sentinel),
        }
    raise AssertionError(f"unknown R3 shape {shape!r}")


def _plant_matching_paths(sentinel: Path) -> None:
    """Plant the R3 helpers' fixture paths in the sentinel worktree.

    Panel-A shape: an existing checkout already contains matching paths, so
    a redirected add stages them instead of failing on a missing pathspec.
    The fixture must never commit there. Untracked worktree files sit outside
    the .git snapshot; only a redirected git write can trip the comparison.
    """
    (sentinel / "README.md").write_bytes(b"sentinel checkout\n")
    (sentinel / ".gitattributes").write_text("*.md text\n", encoding="utf-8")
    (sentinel / "bin").mkdir(exist_ok=True)
    (sentinel / "bin" / "tool").write_bytes(b"sentinel tool\n")
    (sentinel / "empty").write_bytes(b"")


def _run_r3_helper(helper: str, case_root: Path) -> tuple[Path, str]:
    """Drive one real init/add/commit fixture entry point; return (repo, oid)."""
    if helper == "admission":
        from test_git_admission import _fixture

        built = _fixture(case_root, "sha1")
        return built.work, built.commit
    if helper == "transport":
        from test_sources_transport import _bare_repository

        bare, commit = _bare_repository(case_root)
        return case_root / "work", commit
    if helper == "ssh":
        from test_git_admission_ssh import _fixture_repository

        bare, commit = _fixture_repository(case_root)
        return case_root / "work", commit
    if helper == "draft":
        from test_draft_sources_conformance import _transport_bare

        bare, commit = _transport_bare(case_root)
        return case_root / "work", commit
    raise AssertionError(f"unknown R3 helper {helper!r}")


@pytest.mark.parametrize(
    "helper", ["admission", "transport", "ssh", "draft"]
)
@pytest.mark.parametrize("shape", ["index", "repo"], ids=["index", "repo"])
def test_init_add_commit_helpers_cannot_change_external_git_state(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, helper: str, shape: str
) -> None:
    # R3 (recording review): init/add/commit-only helpers never call
    # git config but still must not change git state outside their own
    # fixture -- GIT_INDEX_FILE redirected add into an external index and
    # GIT_DIR/GIT_WORK_TREE redirected commits into external refs.
    sentinel = _make_sentinel(tmp_path)
    if shape == "repo":
        _plant_matching_paths(sentinel)
    snapshot = _snapshot(sentinel)
    for key, value in _r3_hostile_env(shape, sentinel).items():
        monkeypatch.setenv(key, value)

    # An unisolated helper may crash AFTER corrupting the sentinel (panel B:
    # the redirected add rewrites the external index then the commit fails
    # because that index references foreign objects). The sentinel assertion
    # runs regardless so the red proves the corruption, not just the crash.
    work: Path | None = None
    commit: str | None = None
    error: Exception | None = None
    try:
        work, commit = _run_r3_helper(helper, tmp_path / "case")
    except (subprocess.CalledProcessError, AssertionError) as exc:
        error = exc

    _assert_sentinel_unchanged(sentinel, snapshot)
    assert error is None, f"fixture helper failed under hostile env: {error!r}"
    assert work is not None and commit is not None
    # The fixture itself was built: a 40-hex sha1 commit whose tree the
    # hardened reader resolves inside the fixture, not the sentinel.
    assert len(commit) == 40 and all(c in "0123456789abcdef" for c in commit)
    assert run(["git", "rev-parse", "HEAD"], work).stdout.strip() == commit


def test_admission_fixture_attributes_blob_is_lf_normalized(tmp_path: Path) -> None:
    # Pin for the Windows byte-frame fix: the admission fixture's
    # .gitattributes blob must be LF on every platform. Text-mode writes
    # emit CRLF working bytes on Windows, which the runner (correctly)
    # stores unconverted once the platform system autocrlf is out of the
    # picture; byte-exact admission frames then mismatch their constants.
    # Vacuous on POSIX (working bytes are LF there anyway); red on Windows
    # without the bytes write.
    import git_fixture_isolation as isolation

    from test_git_admission import _fixture

    run_fixture_git = getattr(isolation, "run_fixture_git", None)
    if run_fixture_git is None:
        pytest.skip("the shared runner does not exist on this revision")
    built = _fixture(tmp_path / "case", "sha1")
    # Bytes, not text: universal-newline decoding would translate a CRLF
    # blob back to LF and the pin would pass while broken on Windows.
    blob = run_fixture_git(
        ["cat-file", "-p", "HEAD:.gitattributes"], cwd=built.work, text=False
    ).stdout

    assert blob == b"README.md filter=evil text eol=crlf export-ignore\n"
    assert b"\r" not in blob


def test_bare_remote_symbolic_ref_ignores_hostile_git_dir(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Bare repositories have no .git subdirectory; the attestation compares
    # the discovered git dir against the bare directory itself.
    source = init_git_repo(tmp_path / "source")
    bare = tmp_path / "remote.git"
    assert run(["git", "init", "--bare", str(bare)], tmp_path).returncode == 0
    sentinel = _make_sentinel(tmp_path)
    snapshot = _snapshot(sentinel)
    monkeypatch.setenv("GIT_DIR", str(sentinel / ".git"))
    monkeypatch.setenv("GIT_WORK_TREE", str(sentinel))

    assert (
        run(["git", "symbolic-ref", "HEAD", "refs/heads/main"], bare).returncode == 0
    )

    _assert_sentinel_unchanged(sentinel, snapshot)
    assert (bare / "HEAD").read_text(encoding="utf-8").strip() == "ref: refs/heads/main"
    assert source.is_dir()


def test_linked_worktree_read_passes_attestation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # A linked worktree carries a .git FILE (gitdir pointer), not a
    # directory; the attestation resolves the pointer instead of refusing.
    sentinel = _make_sentinel(tmp_path)
    snapshot = _snapshot(sentinel)
    outer = init_git_repo(tmp_path / "outer")
    (outer / "seed.txt").write_text("seed\n", encoding="utf-8")
    run(["git", "add", "seed.txt"], outer)
    run(["git", "commit", "-m", "seed"], outer)
    worktree = outer / ".claude" / "worktrees" / "wt1"
    worktree.parent.mkdir(parents=True, exist_ok=True)
    run(["git", "worktree", "add", "--detach", str(worktree), "HEAD"], outer)
    assert (worktree / ".git").is_file()
    monkeypatch.setenv("GIT_DIR", str(sentinel / ".git"))

    completed = run(["git", "rev-parse", "--git-dir"], worktree)

    assert completed.returncode == 0
    assert "wt1" in completed.stdout
    _assert_sentinel_unchanged(sentinel, snapshot)


def test_linked_worktree_read_with_unicode_separator_path_passes_attestation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The gitdir pointer may itself contain U+2028: the attestation must
    # split the pointer file on newlines only, never on unicode line
    # separators (str.splitlines truncates the path and false-refuses).
    sentinel = _make_sentinel(tmp_path)
    snapshot = _snapshot(sentinel)
    outer = init_git_repo(tmp_path / ("outer\u2028dir"))
    (outer / "seed.txt").write_text("seed\n", encoding="utf-8")
    run(["git", "add", "seed.txt"], outer)
    run(["git", "commit", "-m", "seed"], outer)
    worktree = outer / ".claude" / "worktrees" / "wt1"
    worktree.parent.mkdir(parents=True, exist_ok=True)
    run(["git", "worktree", "add", "--detach", str(worktree), "HEAD"], outer)
    assert "\u2028" in (worktree / ".git").read_bytes().decode("utf-8")
    monkeypatch.setenv("GIT_DIR", str(sentinel / ".git"))

    completed = run(["git", "rev-parse", "--git-dir"], worktree)

    assert completed.returncode == 0
    assert "wt1" in completed.stdout
    _assert_sentinel_unchanged(sentinel, snapshot)


def test_c_flag_config_attests_the_target_repo(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # With -C the discovery directory is the flag target, not the cwd: the
    # runner attests the target even when the cwd itself is not a repository.
    repo = init_git_repo(tmp_path / "repo")
    sentinel = _make_sentinel(tmp_path)
    snapshot = _snapshot(sentinel)
    monkeypatch.setenv("GIT_DIR", str(sentinel / ".git"))

    assert (
        run(["git", "-C", str(repo), "config", "probe.cflag", "present"], tmp_path)
        .returncode
        == 0
    )

    _assert_sentinel_unchanged(sentinel, snapshot)
    local_config = (repo / ".git" / "config").read_bytes()
    assert b"[probe]" in local_config
    assert b"cflag = present" in local_config


def test_check_own_git_dir_rejects_a_foreign_git_dir(tmp_path: Path) -> None:
    own = tmp_path / "own"
    foreign = tmp_path / "sentinel" / ".git"
    with pytest.raises(AssertionError, match="refusing to write git config outside"):
        check_own_git_dir(own, str(foreign))
    with pytest.raises(AssertionError, match="refusing to write git config outside"):
        check_own_git_dir(own, "not-a-git-dir")
    # A gitfile pointer is never followed as proof of ownership, even when
    # discovery reports exactly the pointer target.
    gitfile = tmp_path / "linked"
    gitfile.mkdir()
    (gitfile / ".git").write_text(f"gitdir: {foreign}\n", encoding="utf-8")
    with pytest.raises(AssertionError, match="never followed"):
        check_own_git_dir(gitfile, str(foreign))
    # The guard stays reachable: a real .git directory (in rev-parse output
    # shape, with a trailing newline) passes.
    (own / ".git").mkdir(parents=True)
    check_own_git_dir(own, str(own / ".git") + "\n")


def test_build_fixture_git_env_inherits_only_the_allowlist(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Revision 3 replaces the denylist scrub with an allowlist build: every
    # ambient GIT_* name -- documented or future -- is dropped unless the
    # runner pins it or the caller passes it through the explicit allowlist.
    hostile_ambient = {
        **{var: "/hostile" for var in GIT_DISCOVERY_ENV_VARS},
        "GIT_CONFIG": "/hostile/config",
        "GIT_CONFIG_COUNT": "2",
        "GIT_CONFIG_KEY_0": "user.name",
        "GIT_CONFIG_VALUE_0": "Intruder",
        "GIT_CONFIG_PARAMETERS": "'core.hooksPath=/hostile/hooks'",
        "GIT_CONFIG_GLOBAL": "/hostile/global",
        "GIT_CONFIG_SYSTEM": "/hostile/system",
        "GIT_CONFIG_NOSYSTEM": "0",
        "GIT_TEMPLATE_DIR": "/hostile/template",
        "GIT_NAMESPACE": "evil-ns",
        "GIT_TRACE": "/hostile/trace",
        "GIT_TRACE2_EVENT": "/hostile/trace2",
        "GIT_CEILING_DIRECTORIES": "/hostile-ceiling",
        "GIT_AUTHOR_NAME": "Intruder",
        "GIT_FUTURE_REDIRECTOR": "/hostile/future",
        "HOME": "/hostile/home",
        "XDG_CONFIG_HOME": "/hostile/xdg",
    }
    for key, value in hostile_ambient.items():
        monkeypatch.setenv(key, value)
    repo = tmp_path / "repo"
    explicit = {
        "GIT_AUTHOR_NAME": "Explicit",
        "GIT_COMMITTER_EMAIL": "explicit@example.test",
        "GIT_SSH": "/explicit/ssh",
        "PATH": "/explicit-path",
        # Redirectors in explicit entries are dropped, never honored.
        "GIT_DIR": "/explicit-evil",
        "GIT_CONFIG": "/explicit-evil",
        "GIT_CONFIG_PARAMETERS": "'user.name=ExplicitEvil'",
        "GIT_TEMPLATE_DIR": "/explicit-evil",
        "GIT_TRACE": "/explicit-evil",
        # Runner pins always win over explicit restatements.
        "HOME": "/explicit-evil",
        "XDG_CONFIG_HOME": "/explicit-evil",
        "GIT_CONFIG_GLOBAL": "/explicit-evil",
        "GIT_CONFIG_NOSYSTEM": "0",
        "GIT_CEILING_DIRECTORIES": "/explicit-evil",
        "GIT_FUTURE_REDIRECTOR": "/explicit-evil",
    }

    env = build_fixture_git_env(repo, explicit)

    for var in GIT_DISCOVERY_ENV_VARS:
        assert var not in env
    for var in (
        "GIT_CONFIG",
        "GIT_CONFIG_COUNT",
        "GIT_CONFIG_KEY_0",
        "GIT_CONFIG_VALUE_0",
        "GIT_CONFIG_PARAMETERS",
        "GIT_CONFIG_SYSTEM",
        "GIT_NAMESPACE",
        "GIT_TRACE",
        "GIT_TRACE2_EVENT",
        "GIT_FUTURE_REDIRECTOR",
    ):
        assert var not in env
    # GIT_TEMPLATE_DIR is pinned (not dropped): the hostile ambient and
    # explicit values must both be gone, replaced by the private empty dir.
    assert env["GIT_TEMPLATE_DIR"] not in ("/hostile/template", "/explicit-evil")
    # The explicit allowlist survives: identity and operational entries.
    assert env["GIT_AUTHOR_NAME"] == "Explicit"
    assert env["GIT_COMMITTER_EMAIL"] == "explicit@example.test"
    assert env["GIT_SSH"] == "/explicit/ssh"
    assert env["PATH"] == "/explicit-path"
    # The runner pins: private home, empty global config, empty template,
    # ceiling above the discovery root, pinned locale, no terminal prompt.
    private_home = Path(env["HOME"])
    assert env["XDG_CONFIG_HOME"] == str(private_home)
    assert env["GIT_CONFIG_NOSYSTEM"] == "1"
    global_config = Path(env["GIT_CONFIG_GLOBAL"])
    assert global_config.parent == private_home
    assert global_config.read_bytes() == b""
    template = Path(env["GIT_TEMPLATE_DIR"])
    assert template.parent == private_home
    assert template.is_dir()
    assert list(template.iterdir()) == []
    assert private_home.parent != repo
    assert env["GIT_CEILING_DIRECTORIES"] == str(Path(os.path.realpath(repo)).parent)
    assert env["LANG"] == "C"
    assert env["LC_ALL"] == "C"
    assert env["GIT_TERMINAL_PROMPT"] == "0"
    # Without explicit entries the ambient baseline is inherited: PATH when
    # the ambient environment carries one, nothing GIT_* shaped ever.
    ambient_path = os.environ.get("PATH")
    plain = build_fixture_git_env(repo)
    if ambient_path is None:
        assert "PATH" not in plain
    else:
        assert plain["PATH"] == ambient_path
    pinned_git_vars = {
        "GIT_CONFIG_NOSYSTEM",
        "GIT_CONFIG_GLOBAL",
        "GIT_TEMPLATE_DIR",
        "GIT_CEILING_DIRECTORIES",
        "GIT_TERMINAL_PROMPT",
    }
    leaked = [key for key in plain if key.startswith("GIT_") and key not in pinned_git_vars]
    assert leaked == []


def test_conftest_init_git_repo_ignores_hostile_git_dir(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    sentinel = _make_sentinel(tmp_path)
    snapshot = _snapshot(sentinel)
    monkeypatch.setenv("GIT_DIR", str(sentinel / ".git"))

    repo = init_git_repo(tmp_path / "repo")

    _assert_sentinel_unchanged(sentinel, snapshot)
    assert b"Test User" in (repo / ".git" / "config").read_bytes()


def test_shards_git_fixture_ignores_hostile_git_dir(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    sentinel = _make_sentinel(tmp_path)
    snapshot = _snapshot(sentinel)
    monkeypatch.setenv("GIT_DIR", str(sentinel / ".git"))

    shard_root = tmp_path / "shard-fixture"
    shard_root.mkdir()
    checkout, head = shards._git_fixture(shard_root)

    _assert_sentinel_unchanged(sentinel, snapshot)
    assert head == shards._git(checkout, "rev-parse", "HEAD")
    assert b"CI" in (checkout / ".git" / "config").read_bytes()


def test_init_git_repo_with_unicode_separator_path_succeeds(tmp_path: Path) -> None:
    # The probe failure shape: a fixture path holding U+2028 must init and
    # pass the guard. Red on Windows before discover_git_dir decoded git's
    # output as UTF-8 (cp1252 mojibake false-refused); green after.
    repo = init_git_repo(tmp_path / "outer\u2028dir")

    assert b"Test User" in (repo / ".git" / "config").read_bytes()


def test_discover_git_dir_decodes_unicode_paths_as_utf8(tmp_path: Path) -> None:
    repo = tmp_path / "outer\u2028dir"
    repo.mkdir()
    run(["git", "init"], repo)

    discovered = discover_git_dir(repo)

    assert "\u2028" in discovered
    check_own_git_dir(repo, discovered)


def test_check_own_git_dir_refuses_a_misdecoded_git_dir(tmp_path: Path) -> None:
    # Fail-closed property, deterministic on every platform: if git's UTF-8
    # path output is ever decoded with the wrong codec, the guard refuses
    # instead of treating the mojibake as a match.
    own = tmp_path / "outer\u2028dir"
    misdecoded = str(own / ".git").encode("utf-8").decode("cp1252")
    assert misdecoded != str(own / ".git")
    with pytest.raises(AssertionError, match="refusing to write git config outside"):
        check_own_git_dir(own, misdecoded)


def test_release_git_config_ignores_hostile_git_dir(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    init = release_mod._git(repo, "init", "--initial-branch=main", "--quiet")
    assert init.returncode == 0, init.stderr
    release_mod._assert_own_repo(repo)
    sentinel = _make_sentinel(tmp_path)
    snapshot = _snapshot(sentinel)
    monkeypatch.setenv("GIT_DIR", str(sentinel / ".git"))

    written = release_mod._git(repo, "config", "user.name", "Release Test")

    assert written.returncode == 0, written.stderr
    _assert_sentinel_unchanged(sentinel, snapshot)
    assert b"Release Test" in (repo / ".git" / "config").read_bytes()


def _resolve_git_target_or_skip() -> object:
    import git_fixture_isolation as isolation

    parser = getattr(isolation, "_resolve_git_target", None)
    if parser is None:
        pytest.skip("the shared runner does not exist on this revision")
    return parser


def test_resolve_git_target_prefers_explicit_discovery_flags(tmp_path: Path) -> None:
    parser = _resolve_git_target_or_skip()
    assert parser(["-C", str(tmp_path), "status"], None) == (
        str(tmp_path),
        str(tmp_path),
    )
    assert parser(["--git-dir", str(tmp_path), "update-ref"], tmp_path) == (
        str(tmp_path),
        str(tmp_path),
    )
    assert parser([f"--git-dir={tmp_path}", "update-ref"], None) == (
        str(tmp_path),
        str(tmp_path),
    )
    # -c pairs are consumed as config, never mistaken for discovery.
    assert parser(["-c", "user.name=X", "commit"], tmp_path) == (None, str(tmp_path))
    # rev-parse's own --git-dir report flag is a subcommand argument, not a
    # discovery redirect: parsing stops at the subcommand.
    assert parser(["rev-parse", "--git-dir"], tmp_path) == (None, str(tmp_path))
    # Everything past -- is a pathspec, never a flag.
    assert parser(["add", "--", "-C"], tmp_path) == (None, str(tmp_path))
    with pytest.raises(AssertionError):
        parser(["-C"], tmp_path)


def _write_corrupting_hook(hooks_dir: Path, target_config: Path) -> Path:
    """Write a pre-commit hook that appends a marker section to ``target_config``.

    Exits 0 so the victim commit succeeds: the corruption, not a failure, is
    the signal. The panel probes (probe.py, panel-a-probe.py) use exactly
    this shape through ``GIT_CONFIG_PARAMETERS`` and ``GIT_TEMPLATE_DIR``.
    """
    hooks_dir.mkdir(parents=True, exist_ok=True)
    hook = hooks_dir / "pre-commit"
    quoted = "'" + str(target_config).replace("'", "'\"'\"'") + "'"
    hook.write_text(
        "#!/bin/sh\nprintf '\\n[review]\\n\\tcorrupted = yes\\n' >> " + quoted + "\n",
        encoding="utf-8",
    )
    hook.chmod(0o755)
    return hook


def _write_corrupting_template(template_dir: Path, target_config: Path) -> Path:
    """Write a hostile ``git init`` template installing a corrupting hook."""
    _write_corrupting_hook(template_dir / "hooks", target_config)
    return template_dir


#: The review panels' hostile channels, as (mode, env-builder) pairs:
#: ``GIT_CONFIG_PARAMETERS`` with a hook, ``GIT_TEMPLATE_DIR`` with a hook,
#: and the seven trace destinations pointed at the sentinel config.
PANEL_MODES: tuple[str, ...] = (
    "parameters",
    "template",
    "GIT_TRACE",
    "GIT_TRACE_SETUP",
    "GIT_TRACE_PERFORMANCE",
    "GIT_TRACE_REFS",
    "GIT_TRACE2",
    "GIT_TRACE2_EVENT",
    "GIT_TRACE2_PERF",
)


def _panel_hostile_env(
    mode: str, sentinel: Path, tmp_path: Path
) -> dict[str, str]:
    config = sentinel / ".git" / "config"
    if mode == "parameters":
        hooks = tmp_path / "panel-hooks"
        _write_corrupting_hook(hooks, config)
        return {"GIT_CONFIG_PARAMETERS": f"'core.hooksPath={hooks}'"}
    if mode == "template":
        template = tmp_path / "panel-template"
        _write_corrupting_template(template, config)
        return {"GIT_TEMPLATE_DIR": str(template)}
    return {mode: str(config)}


def _run_panel_entrypoint(entrypoint: str, tmp_path: Path) -> list[Path]:
    """Drive one real fixture-building entrypoint; return its repositories."""
    if entrypoint == "golden":
        paths = golden.build_pristine_fixture(tmp_path / "fixture")
        return [paths["project"], paths["skills_root"] / "demo-skill"]
    if entrypoint == "conftest":
        repo = init_git_repo(tmp_path / "fixture")
        (repo / "file.txt").write_text("panel\n", encoding="utf-8")
        run(["git", "add", "."], repo)
        run(["git", "commit", "-m", "panel"], repo)
        return [repo]
    raise AssertionError(f"unknown entrypoint {entrypoint!r}")


@pytest.mark.parametrize("entrypoint", ["golden", "conftest"])
@pytest.mark.parametrize("mode", PANEL_MODES, ids=PANEL_MODES)
def test_fixture_git_environment_allowlist_preserves_external_config(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, entrypoint: str, mode: str
) -> None:
    """No inherited channel may write git state outside the fixture repo.

    Regression for BUG-261004-473myt revision 2
    (``config-destination-redirector``): the denylist kept
    ``GIT_CONFIG_PARAMETERS``, ``GIT_TEMPLATE_DIR`` and the trace
    destinations, so hooks or diagnostic appends wrote external config while
    local git-dir attestation succeeded. Each case replays the panels'
    reproduction -- ``probe.py`` (golden builder) and ``panel-a-probe.py``
    (conftest init/commit) -- through the real entrypoint, with the sentinel
    config as the corruption target.
    """
    sentinel = _make_sentinel(tmp_path)
    snapshot = _snapshot(sentinel)
    for key, value in _panel_hostile_env(mode, sentinel, tmp_path).items():
        monkeypatch.setenv(key, value)

    repos = _run_panel_entrypoint(entrypoint, tmp_path)

    _assert_sentinel_unchanged(sentinel, snapshot)
    for repo in repos:
        local_config = (repo / ".git" / "config").read_bytes()
        if entrypoint == "golden":
            assert b"Golden Test" in local_config
            assert _commit_author(repo) == "Golden Test|golden@example.com"
        else:
            assert b"Test User" in local_config
            assert _commit_author(repo) == "Test User|test@example.com"
        if mode == "template":
            # Fixture-side pin, strong on every platform: the hostile
            # template must not be installed, hooks or not.
            assert not (repo / ".git" / "hooks" / "pre-commit").exists()
        if mode == "parameters":
            # Fixture-side pin: no inherited hooks path reaches the build.
            probe = run(
                ["git", "config", "--get", "core.hooksPath"], repo, check=False
            )
            assert probe.returncode != 0, probe.stdout


@pytest.mark.parametrize("runner", ["golden", "conftest"])
def test_fixture_git_rejects_foreign_gitfile_write(
    tmp_path: Path, runner: str
) -> None:
    """A foreign gitfile pointer can never authorize a fixture write.

    Regression for BUG-261004-473myt revision 2
    (``foreign-gitfile-self-attestation``): the attestation resolved the
    caller-controlled ``gitdir:`` pointer as the expected directory and
    compared discovery against that same foreign pointer, so self-minted
    evidence authorized writes to the sentinel's ``.git/config``. The panel
    reproduction (``probe.py`` gitfile mode) is the golden leg; the conftest
    leg proves the shared runner refuses, not just one helper's backstop.
    """
    sentinel = _make_sentinel(tmp_path)
    snapshot = _snapshot(sentinel)
    fixture = tmp_path / "fixture"
    fixture.mkdir()
    (fixture / ".git").write_text(f"gitdir: {sentinel / '.git'}\n", encoding="utf-8")

    if runner == "golden":
        with pytest.raises(AssertionError, match="refusing"):
            golden.run_git(["config", "review.corrupted", "yes"], fixture)
    else:
        with pytest.raises(AssertionError, match="refusing"):
            run(["git", "config", "review.corrupted", "yes"], fixture)

    _assert_sentinel_unchanged(sentinel, snapshot)


def test_gitfile_checkout_allows_read_only_commands(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Read-only access through a gitfile checkout keeps working.

    The gitfile refusal above covers mutating commands only: legitimate
    read-only worktree access (``rev-parse`` here, the shape
    ``test_install.py`` relies on) runs unattested under the allowlist
    environment. A hostile ``GIT_DIR`` is dropped, so the read resolves the
    checkout's own pointer instead of the intruder's repository.
    """
    sentinel = _make_sentinel(tmp_path)
    snapshot = _snapshot(sentinel)
    outer = init_git_repo(tmp_path / "outer")
    (outer / "seed.txt").write_text("seed\n", encoding="utf-8")
    run(["git", "add", "seed.txt"], outer)
    run(["git", "commit", "-m", "seed"], outer)
    worktree = outer / ".claude" / "worktrees" / "wt1"
    worktree.parent.mkdir(parents=True, exist_ok=True)
    run(["git", "worktree", "add", "--detach", str(worktree), "HEAD"], outer)
    assert (worktree / ".git").is_file()
    monkeypatch.setenv("GIT_DIR", str(sentinel / ".git"))

    git_dir = run(["git", "rev-parse", "--git-dir"], worktree)
    head = run(["git", "rev-parse", "HEAD"], worktree)

    assert "wt1" in git_dir.stdout
    assert head.stdout.strip() == run(["git", "rev-parse", "HEAD"], outer).stdout.strip()
    _assert_sentinel_unchanged(sentinel, snapshot)
    with pytest.raises(AssertionError, match="refusing"):
        run(["git", "config", "review.corrupted", "yes"], worktree)
    _assert_sentinel_unchanged(sentinel, snapshot)


def test_runner_passes_tuple_argv_to_subprocess(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Seam transparency: tests that observe the production subprocess seam
    # assert tuple commands (the convention of every fixture helper this
    # runner replaces). A list argv trips those observers when a fixture
    # helper runs inside a patched window (transport relay tests).
    import git_fixture_isolation as isolation

    run_fixture_git = getattr(isolation, "run_fixture_git", None)
    if run_fixture_git is None:
        pytest.skip("the shared runner does not exist on this revision")
    repo = tmp_path / "repo"
    repo.mkdir()
    seen: dict[str, object] = {}
    real_run = subprocess.run

    def capture(*args: object, **kwargs: object) -> object:
        seen["argv"] = args[0]
        return real_run(*args, **kwargs)

    monkeypatch.setattr(subprocess, "run", capture)
    run_fixture_git(["init", "--quiet"], cwd=repo)

    assert isinstance(seen["argv"], tuple)


def test_runner_routes_owned_repos_through_explicit_git_dir(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Owned repositories get explicit --git-dir/--work-tree routing.

    A normal ``.git``-directory repository is attested and then routed via
    prepended ``--git-dir``/``--work-tree``; a bare repository via
    ``--git-dir`` alone. Argv that already routes explicitly (``-C``) is
    attested, never rewritten, and gitfile reads carry no injected flags.
    """
    import git_fixture_isolation as isolation

    run_fixture_git = getattr(isolation, "run_fixture_git", None)
    if run_fixture_git is None:
        pytest.skip("the shared runner does not exist on this revision")
    repo = init_git_repo(tmp_path / "repo")
    bare = tmp_path / "remote.git"
    assert run(["git", "init", "--bare", str(bare)], tmp_path).returncode == 0
    seen: dict[str, object] = {}
    real_run = subprocess.run

    def capture(*args: object, **kwargs: object) -> object:
        seen["argv"] = args[0]
        return real_run(*args, **kwargs)

    monkeypatch.setattr(subprocess, "run", capture)

    run_fixture_git(["config", "probe.owned", "present"], cwd=repo)
    argv = seen["argv"]
    assert isinstance(argv, tuple), argv
    assert argv[1] == f"--git-dir={repo / '.git'}", argv
    assert argv[2] == f"--work-tree={repo}", argv
    assert b"owned = present" in (repo / ".git" / "config").read_bytes()

    run_fixture_git(["config", "probe.owned", "present"], cwd=bare)
    argv = seen["argv"]
    assert isinstance(argv, tuple), argv
    assert argv[1] == f"--git-dir={bare}", argv
    assert not [token for token in argv if str(token).startswith("--work-tree")], argv

    run_fixture_git(["-C", str(repo), "config", "probe.flag", "present"], cwd=tmp_path)
    argv = seen["argv"]
    assert isinstance(argv, tuple), argv
    assert not [token for token in argv if str(token).startswith("--git-dir=")], argv

    outer = init_git_repo(tmp_path / "outer")
    (outer / "seed.txt").write_text("seed\n", encoding="utf-8")
    run(["git", "add", "seed.txt"], outer)
    run(["git", "commit", "-m", "seed"], outer)
    worktree = outer / "wt"
    run(["git", "worktree", "add", "--detach", str(worktree), "HEAD"], outer)
    run_fixture_git(["rev-parse", "--git-dir"], cwd=worktree)
    argv = seen["argv"]
    assert isinstance(argv, tuple), argv
    # The bare --git-dir report flag is the subcommand's own argument, not
    # an injected route: only the --git-dir=<abs> shape counts.
    assert not [token for token in argv if str(token).startswith("--git-dir=")], argv


def test_git_subcommand_parser_finds_the_mutating_decision(tmp_path: Path) -> None:
    """The read-only/mutating classification sees past pre-subcommand flags."""
    import git_fixture_isolation as isolation

    parser = getattr(isolation, "_git_subcommand", None)
    if parser is None:
        pytest.skip("the shared runner does not exist on this revision")
    assert parser(["-C", str(tmp_path), "log", "--oneline"]) == "log"
    assert parser(["-c", "user.name=X", "show", "HEAD"]) == "show"
    assert parser(["--git-dir", str(tmp_path), "config", "x.y"]) == "config"
    assert parser(["rev-parse", "--git-dir"]) == "rev-parse"
    assert parser(["--version"]) is None
    assert parser(["--exec-path"]) is None
    assert parser([]) is None
    assert parser(["add", "--", "-C"]) == "add"
    with pytest.raises(AssertionError):
        parser(["-C"])


#: Curated inventory of environment variables git consults, from the git
#: documentation ENVIRONMENT section (git v2.54, the same source panel A
#: extracted its 73 ``GIT_*`` names from), plus the config-file carriers
#: (``HOME``, ``XDG_CONFIG_HOME``) and transport proxy inputs git honors.
#: The sweep unions this with names parsed from ``git help environment``
#: where manual pages exist, so a newer git can only widen the net.
DOCUMENTED_GIT_ENVIRONMENT_NAMES: tuple[str, ...] = (
    "EMAIL",
    "GIT_ALTERNATE_OBJECT_DIRECTORIES",
    "GIT_ASKPASS",
    "GIT_ATTR_SOURCE",
    "GIT_AUTHOR_DATE",
    "GIT_AUTHOR_EMAIL",
    "GIT_AUTHOR_NAME",
    "GIT_CEILING_DIRECTORIES",
    "GIT_COMMITTER_DATE",
    "GIT_COMMITTER_EMAIL",
    "GIT_COMMITTER_NAME",
    "GIT_COMMON_DIR",
    "GIT_CONFIG",
    "GIT_CONFIG_COUNT",
    "GIT_CONFIG_GLOBAL",
    "GIT_CONFIG_KEY_0",
    "GIT_CONFIG_NOSYSTEM",
    "GIT_CONFIG_PARAMETERS",
    "GIT_CONFIG_SYSTEM",
    "GIT_CONFIG_VALUE_0",
    "GIT_CURL_VERBOSE",
    "GIT_DIFF_OPTS",
    "GIT_DIR",
    "GIT_DISCOVERY_ACROSS_FILESYSTEM",
    "GIT_EDITOR",
    "GIT_EXEC_PATH",
    "GIT_EXTERNAL_DIFF",
    "GIT_FLUSH",
    "GIT_GLOB_PATHSPECS",
    "GIT_GRAFT_FILE",
    "GIT_HTTP_PROXY",
    "GIT_HTTPS_PROXY",
    "GIT_ICASE_PATHSPECS",
    "GIT_INDEX_FILE",
    "GIT_INDEX_VERSION",
    "GIT_INTERNAL_SUPER_PREFIX",
    "GIT_LITERAL_PATHSPECS",
    "GIT_MERGE_AUTOEDIT",
    "GIT_MERGE_VERBOSITY",
    "GIT_NAMESPACE",
    "GIT_NOGLOB_PATHSPECS",
    "GIT_NOTES_DISPLAY_REF",
    "GIT_NOTES_REF",
    "GIT_NOTES_REWRITE_MODE",
    "GIT_NOTES_REWRITE_REF",
    "GIT_OBJECT_DIRECTORY",
    "GIT_OPTIONAL_LOCKS",
    "GIT_PAGER",
    "GIT_PREFIX",
    "GIT_PROGRESS_DELAY",
    "GIT_PROXY_COMMAND",
    "GIT_QUARANTINE_PATH",
    "GIT_REDIRECT_STDERR",
    "GIT_REDIRECT_STDIN",
    "GIT_REDIRECT_STDOUT",
    "GIT_REFLOG_ACTION",
    "GIT_REPLACE_REF_BASE",
    "GIT_SEQUENCE_EDITOR",
    "GIT_SHALLOW_FILE",
    "GIT_SHOW_SUCCESS",
    "GIT_SSH",
    "GIT_SSH_COMMAND",
    "GIT_SSH_VARIANT",
    "GIT_SSL_CAINFO",
    "GIT_SSL_CAPATH",
    "GIT_SSL_NO_VERIFY",
    "GIT_TEMPLATE_DIR",
    "GIT_TERMINAL_PROMPT",
    "GIT_TRACE",
    "GIT_TRACE2",
    "GIT_TRACE2_DST_DEBUG",
    "GIT_TRACE2_EVENT",
    "GIT_TRACE2_PERF",
    "GIT_TRACE_CURL",
    "GIT_TRACE_PACKET",
    "GIT_TRACE_PERFORMANCE",
    "GIT_TRACE_REFS",
    "GIT_TRACE_SETUP",
    "GIT_TRACE_SHALLOW",
    "GIT_WORK_TREE",
    "HOME",
    "XDG_CONFIG_HOME",
    "http_proxy",
    "https_proxy",
    "no_proxy",
)

#: Plausible-future channel names plus seeded random ``GIT_*`` names: the
#: allowlist drops unknown names by construction, so any value aimed at the
#: sentinel must be neutralized.
_SWEEP_RANDOM = random.Random(261004473)
SWEEP_FUZZ_NAMES: tuple[str, ...] = (
    "GIT_CONFIG_FUTURE_CHANNEL",
    "GIT_TEMPLATE_FUTURE",
    "GIT_TRACE_FUTURE",
    *(
        f"GIT_FIXTURE_FUZZ_{index:02d}_{_SWEEP_RANDOM.randrange(1 << 30):08x}"
        for index in range(16)
    ),
)


def _discover_documented_git_env_names() -> set[str]:
    """Parse ``GIT_*`` names from ``git help environment`` where it exists.

    Best effort: without manual pages (this machine included) the command
    fails and the curated inventory above is the whole net. Runs through
    the shared runner, so this file keeps its zero-bypass scan.
    """
    import git_fixture_isolation as isolation

    try:
        completed = isolation.run_fixture_git(
            ["help", "environment"], check=False, timeout=20
        )
    except (OSError, ValueError):
        return set()
    text = (completed.stdout or "") + "\n" + (completed.stderr or "")
    return set(re.findall(r"GIT_[A-Z0-9_]+", text))


_SWEEP_DISCOVERED = _discover_documented_git_env_names()
SWEEP_NAMES: tuple[str, ...] = tuple(
    sorted(set(DOCUMENTED_GIT_ENVIRONMENT_NAMES) | _SWEEP_DISCOVERED)
    + list(SWEEP_FUZZ_NAMES)
)

_TRACE_DESTINATION_VARS = frozenset(
    {
        "GIT_TRACE",
        "GIT_TRACE_SETUP",
        "GIT_TRACE_PERFORMANCE",
        "GIT_TRACE_REFS",
        "GIT_TRACE_PACKET",
        "GIT_TRACE_CURL",
        "GIT_TRACE_SHALLOW",
        "GIT_TRACE2",
        "GIT_TRACE2_EVENT",
        "GIT_TRACE2_PERF",
        "GIT_TRACE2_DST_DEBUG",
        "GIT_REDIRECT_STDIN",
        "GIT_REDIRECT_STDOUT",
        "GIT_REDIRECT_STDERR",
    }
)

_PROGRAM_SELECTOR_VARS = frozenset(
    {
        "GIT_SSH",
        "GIT_SSH_COMMAND",
        "GIT_ASKPASS",
        "GIT_EDITOR",
        "GIT_SEQUENCE_EDITOR",
        "GIT_PAGER",
        "GIT_EXTERNAL_DIFF",
        "GIT_PROXY_COMMAND",
    }
)


def _write_hostile_home(home: Path, target_config: Path) -> Path:
    """Write a hostile home dir: global config with hooks and identity."""
    hooks = home / "hooks"
    _write_corrupting_hook(hooks, target_config)
    (home / ".config" / "git").mkdir(parents=True, exist_ok=True)
    global_config = (
        "[user]\n\tname = Intruder\n\temail = intruder@example.test\n"
        f"[core]\n\thooksPath = {hooks}\n"
    )
    (home / ".gitconfig").write_text(global_config, encoding="utf-8")
    (home / ".config" / "git" / "config").write_text(global_config, encoding="utf-8")
    return home


def _write_hostile_program(path: Path, target_config: Path) -> Path:
    """Write a hostile helper program that corrupts the sentinel if executed."""
    quoted = "'" + str(target_config).replace("'", "'\"'\"'") + "'"
    path.write_text(
        "#!/bin/sh\nprintf '\\n[review]\\n\\tcorrupted = yes\\n' >> "
        + quoted
        + "\nexit 0\n",
        encoding="utf-8",
    )
    path.chmod(0o755)
    return path


def _sweep_hostile_env(
    var: str, sentinel: Path, tmp_path: Path
) -> dict[str, str]:
    """Map one swept variable to hostile values aimed at the sentinel."""
    config = sentinel / ".git" / "config"
    if var in _TRACE_DESTINATION_VARS or var in (
        "GIT_CONFIG",
        "GIT_CONFIG_GLOBAL",
        "GIT_CONFIG_SYSTEM",
        "GIT_SSL_CAINFO",
        "GIT_SSL_CAPATH",
        "GIT_GRAFT_FILE",
        "GIT_SHALLOW_FILE",
        "GIT_ATTR_SOURCE",
    ):
        return {var: str(config)}
    if var in ("GIT_CONFIG_COUNT", "GIT_CONFIG_KEY_0", "GIT_CONFIG_VALUE_0"):
        return _hostile_env("GIT_CONFIG_COUNT", sentinel, tmp_path)
    if var in ALL_REDIRECTORS:
        return _hostile_env(var, sentinel, tmp_path)
    if var == "GIT_CONFIG_PARAMETERS":
        hooks = tmp_path / "sweep-hooks"
        _write_corrupting_hook(hooks, config)
        return {var: f"'core.hooksPath={hooks}'"}
    if var == "GIT_TEMPLATE_DIR":
        template = tmp_path / "sweep-template"
        _write_corrupting_template(template, config)
        return {var: str(template)}
    if var == "GIT_CEILING_DIRECTORIES":
        return {var: "/"}
    if var == "GIT_CONFIG_NOSYSTEM":
        return {var: "0"}
    if var in ("GIT_AUTHOR_NAME", "GIT_COMMITTER_NAME"):
        return {var: "Intruder"}
    if var in ("GIT_AUTHOR_EMAIL", "GIT_COMMITTER_EMAIL", "EMAIL"):
        return {var: "intruder@example.test"}
    if var in ("GIT_AUTHOR_DATE", "GIT_COMMITTER_DATE"):
        return {var: "2005-05-05T05:05:05+00:00"}
    if var in ("HOME", "XDG_CONFIG_HOME"):
        home = _write_hostile_home(tmp_path / "sweep-home", config)
        return {var: str(home if var == "HOME" else home / ".config")}
    if var in _PROGRAM_SELECTOR_VARS:
        program = _write_hostile_program(tmp_path / "sweep-program", config)
        return {var: str(program)}
    if var in ("GIT_EXEC_PATH",):
        return {var: str(tmp_path / "sweep-exec")}
    # Generic probe: any path-interpreted variable appends to or reads the
    # sentinel config, and any other interpretation fails the build or the
    # authorship assertion when it leaks.
    return {var: str(config)}


@pytest.mark.parametrize("var", SWEEP_NAMES, ids=SWEEP_NAMES)
def test_fixture_git_sweep_environment_variable_preserves_external_state(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, var: str
) -> None:
    """Every documented GIT_* name plus fuzz names leaves the sentinel alone.

    Generated regression net for BUG-261004-473myt revision 3: each swept
    variable is injected into the parent environment with a hostile value
    aimed at the sentinel (its config as an append target, a corrupting
    hook, a hostile home), then ``golden._init_repo`` plus add/commit --
    init (template), config writes (config channels), commit (hooks), every
    command (traces) -- runs through the real entrypoint. The sentinel's
    git store and worktree must be byte-identical afterwards, and the
    fixture must carry exactly its own identity and files.
    """
    sentinel = _make_sentinel(tmp_path)
    snapshot = _snapshot(sentinel)
    worktree_before = (sentinel / "sentinel.txt").read_bytes()
    for key, value in _sweep_hostile_env(var, sentinel, tmp_path).items():
        monkeypatch.setenv(key, value)

    fixture = tmp_path / "fixture"
    golden._init_repo(fixture)
    (fixture / "file.txt").write_text("sweep\n", encoding="utf-8")
    golden.run_git(["add", "."], fixture)
    golden.run_git(["commit", "-m", "sweep"], fixture)

    _assert_sentinel_unchanged(sentinel, snapshot)
    assert (sentinel / "sentinel.txt").read_bytes() == worktree_before
    assert b"Golden Test" in (fixture / ".git" / "config").read_bytes()
    assert _commit_author(fixture) == "Golden Test|golden@example.com"
    assert _tracked_files(fixture) == {"file.txt"}
