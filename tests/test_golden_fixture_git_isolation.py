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
import subprocess
from pathlib import Path

import pytest

import cli_golden_v1_support as golden
import test_protocol_shards as shards
import test_release_script as release_mod
from conftest import init_git_repo, run
from git_fixture_isolation import (
    GIT_DISCOVERY_ENV_VARS,
    check_own_git_dir,
    discover_git_dir,
    scrub_git_child_env,
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
    # The guard stays reachable: the fixture's own dir (in rev-parse output
    # shape, with a trailing newline) passes.
    check_own_git_dir(own, str(own / ".git") + "\n")


def test_scrub_git_child_env_removes_redirectors_and_pins_ceiling(
    tmp_path: Path,
) -> None:
    env = {var: "/hostile" for var in GIT_DISCOVERY_ENV_VARS}
    env["GIT_CONFIG"] = "/hostile/config"
    env["GIT_CONFIG_COUNT"] = "2"
    env["GIT_CONFIG_KEY_0"] = "user.name"
    env["GIT_CONFIG_VALUE_0"] = "Intruder"
    env["GIT_CONFIG_KEY_1"] = "user.email"
    env["GIT_CONFIG_VALUE_1"] = "intruder@example.test"
    env["GIT_CONFIG_GLOBAL"] = "/hostile/global"
    env["GIT_CONFIG_SYSTEM"] = "/hostile/system"
    env["GIT_NAMESPACE"] = "evil-ns"
    env["PATH"] = "/kept"
    env["GIT_CEILING_DIRECTORIES"] = "/hostile-ceiling"
    repo = tmp_path / "repo"
    scrub_git_child_env(env, repo)
    for var in GIT_DISCOVERY_ENV_VARS:
        assert var not in env
    assert "GIT_CONFIG" not in env
    assert "GIT_CONFIG_COUNT" not in env
    assert "GIT_CONFIG_KEY_0" not in env
    assert "GIT_CONFIG_VALUE_0" not in env
    assert "GIT_CONFIG_KEY_1" not in env
    assert "GIT_CONFIG_VALUE_1" not in env
    assert "GIT_NAMESPACE" not in env
    # The global/system files are replaced by fixture-local paths that do
    # not exist: config reads see nothing, config writes fail closed.
    expected_root = Path(os.path.realpath(repo))
    assert env["GIT_CONFIG_GLOBAL"] == str(
        expected_root / ".git" / "fixture-global-config"
    )
    assert env["GIT_CONFIG_SYSTEM"] == str(
        expected_root / ".git" / "fixture-system-config"
    )
    assert not Path(env["GIT_CONFIG_GLOBAL"]).exists()
    assert not Path(env["GIT_CONFIG_SYSTEM"]).exists()
    assert env["PATH"] == "/kept"
    assert Path(env["GIT_CEILING_DIRECTORIES"]).resolve() == tmp_path.resolve()


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
