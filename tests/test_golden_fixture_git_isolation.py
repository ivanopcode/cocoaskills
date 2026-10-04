"""A test fixture can never change git state outside its own repository.

Regression suite for BUG-261004-473myt: the golden fixture's git helper
inherited ``os.environ``, so an ambient ``GIT_DIR`` (or a replay copy without
its own ``.git`` nested inside a checkout) redirected ``_init_repo``'s config
writes into the enclosing checkout's ``.git/config`` -- which then refused a
landing as an unsigned commit from ``golden@example.com``.

Each test builds a sentinel repository, snapshots its git state as raw bytes
(never ``git config --get``, whose global fallback would mask a clobbered
local file), runs a fixture helper under hostile conditions, and proves the
snapshot is byte-identical afterwards.
"""

from __future__ import annotations

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


def _hostile_value(var: str, sentinel: Path) -> str:
    git_dir = sentinel / ".git"
    return {
        "GIT_DIR": str(git_dir),
        "GIT_WORK_TREE": str(sentinel),
        "GIT_INDEX_FILE": str(git_dir / "index"),
        "GIT_COMMON_DIR": str(git_dir),
        "GIT_OBJECT_DIRECTORY": str(git_dir / "objects"),
        "GIT_ALTERNATE_OBJECT_DIRECTORIES": str(git_dir / "objects"),
    }[var]


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


def _snapshot(sentinel: Path) -> tuple[bytes, bytes | None, set[str]]:
    git_dir = sentinel / ".git"
    config = (git_dir / "config").read_bytes()
    index_path = git_dir / "index"
    index = index_path.read_bytes() if index_path.exists() else None
    objects = {
        path.relative_to(git_dir / "objects").as_posix()
        for path in (git_dir / "objects").rglob("*")
        if path.is_file()
    }
    return config, index, objects


def _assert_sentinel_unchanged(
    sentinel: Path, snapshot: tuple[bytes, bytes | None, set[str]]
) -> None:
    config, index, objects = snapshot
    assert (sentinel / ".git" / "config").read_bytes() == config
    index_path = sentinel / ".git" / "index"
    if index is None:
        assert not index_path.exists()
    else:
        assert index_path.read_bytes() == index
    assert {
        path.relative_to(sentinel / ".git" / "objects").as_posix()
        for path in (sentinel / ".git" / "objects").rglob("*")
        if path.is_file()
    } == objects


def _tracked_files(repo: Path) -> set[str]:
    return set(
        run(["git", "ls-tree", "-r", "HEAD", "--name-only"], repo).stdout.split()
    )


@pytest.mark.parametrize("var", GIT_DISCOVERY_ENV_VARS, ids=GIT_DISCOVERY_ENV_VARS)
def test_golden_fixture_build_ignores_hostile_git_discovery_env(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, var: str
) -> None:
    sentinel = _make_sentinel(tmp_path)
    snapshot = _snapshot(sentinel)
    monkeypatch.setenv(var, _hostile_value(var, sentinel))

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
def test_git_config_cannot_escape_to_outer_repo_without_own_git_dir(
    tmp_path: Path, runner: str
) -> None:
    # The replay-copy shape from the incident: a fixture directory nested
    # inside a checkout, without its own .git. Without the ceiling the config
    # write traverses up and lands in the outer repository.
    sentinel = _make_sentinel(tmp_path)
    snapshot = _snapshot(sentinel)
    nested = sentinel / "replay-copy"
    nested.mkdir()

    _attempt_config_write(runner, nested)

    _assert_sentinel_unchanged(sentinel, snapshot)


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
    env["PATH"] = "/kept"
    env["GIT_CEILING_DIRECTORIES"] = "/hostile-ceiling"
    scrub_git_child_env(env, tmp_path / "repo")
    for var in GIT_DISCOVERY_ENV_VARS:
        assert var not in env
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
