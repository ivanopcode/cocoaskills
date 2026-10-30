"""Fixture lifetime controls; no native processes or shared stdlib patches."""
from __future__ import annotations

import ast
import os
import tempfile
from pathlib import Path

import pytest

from child_cli_environment import child_cli_environment, owned_cli_environment
pytest_plugins = ("pytester",)


# An unused context must restore its caller without touching a nonexistent root at teardown.
def test_unused_owner_performs_no_teardown_probe(tmp_path: Path) -> None:
    class ProbePath(type(tmp_path)):
        denied = False

        def lstat(self):
            assert not self.denied, "unused scratch was probed after the body"
            return super().lstat()

    root = ProbePath(tmp_path / "unused")
    before = {key: os.environ.get(key) for key in ("TMPDIR", "TMP", "TEMP")}
    cached = tempfile.tempdir
    with owned_cli_environment(root):
        root.denied = True
    assert not (tmp_path / "unused").exists()
    assert {key: os.environ.get(key) for key in before} == before
    assert tempfile.tempdir == cached
    # A nested unused context must not leak its owner into the autouse caller.
    assert Path(child_cli_environment()["TMPDIR"]) == tmp_path / "cli-tmp"


# The REAL autouse fixture cleans allocated scratch after body patches restore,
# including on body failure; unused bodies allocate nothing and leave parent temp/cache intact.
@pytest.mark.parametrize("body_fails", [False, True])
def test_real_fixture_cleanup_outlives_body_monkeypatch(pytester, monkeypatch, tmp_path, body_fails):
    # A parent need not export TMPDIR; pytest's owned path anchors this sandbox.
    monkeypatch.delenv("TMPDIR", raising=False)
    parent_temp = {key: os.environ.get(key) for key in ("TMPDIR", "TEMP", "TMP")}
    parent_cache = tempfile.tempdir
    body = r'''
import os, tempfile
from pathlib import Path
import pytest
import child_cli_environment as harness
from child_cli_environment import child_cli_environment
records = []

def fail_cleanup(_path):
    raise AssertionError('body patch leaked into allocated scratch cleanup')

def test_allocate(monkeypatch, tmp_path):
    before = {k: os.environ[k] for k in ('TMPDIR', 'TEMP', 'TMP')}
    cached = tempfile.tempdir
    env = child_cli_environment()
    root = Path(env['TMPDIR'])
    (root / 'scratch').write_bytes(b'owned scratch')
    assert root.is_relative_to(tmp_path) and root.is_dir()
    assert {k: os.environ[k] for k in before} == before
    assert tempfile.tempdir == cached
    records.append((root, harness._remove_tree, before, cached))
    monkeypatch.setattr(harness, '_remove_tree', fail_cleanup)
    assert harness._remove_tree is fail_cleanup
    if BODY_FAILS:
        raise RuntimeError('intentional body fault')

def test_after_cleanup(tmp_path):
    root, original, before, cached = records[0]
    assert not root.exists(), 'allocated CLI scratch leaked past fixture teardown'
    assert harness._remove_tree is original
    assert {k: os.environ[k] for k in before} == before
    assert tempfile.tempdir == cached
    assert not (tmp_path / 'cli-tmp').exists(), 'unused owner allocated scratch'
'''
    tests = Path(__file__).parent
    pytester.makeconftest((tests / "conftest.py").read_text())
    pytester.makepyfile("BODY_FAILS = " + repr(body_fails) + "\n" + body)
    # This child gets a fresh private temp/cache/home too; it finishes before
    # the outer context removes its scratch, even for the expected failed body.
    with tempfile.TemporaryDirectory(prefix="lf-", dir=tmp_path) as directory:
        with monkeypatch.context() as patch:
            for name in ("TMPDIR", "TEMP", "TMP", "HOME", "USERPROFILE", "XDG_CONFIG_HOME",
                         "XDG_CACHE_HOME", "XDG_DATA_HOME", "XDG_STATE_HOME", "APPDATA",
                         "LOCALAPPDATA", "CSK_HOME", "UV_CACHE_DIR"):
                patch.setenv(name, directory)
            patch.setenv("PYTHONPATH", os.pathsep.join((str(tests.parent / "src"), str(tests))))
            result = pytester.runpytest_subprocess("-q", "-m", "not csk_e2e_native")
    assert {key: os.environ.get(key) for key in parent_temp} == parent_temp
    assert tempfile.tempdir == parent_cache
    result.assert_outcomes(passed=1 if body_fails else 2, failed=int(body_fails))
    assert result.ret == int(body_fails)
    if body_fails:
        assert 'RuntimeError: intentional body fault' in result.stdout.str()
    assert 'body patch leaked' not in result.stdout.str()


# Every verbatim-conftest copy must carry all directly imported local support modules.
@pytest.mark.parametrize("consumer", ["test_git_session_isolation.py", "mcp_ownership_mutants.py", "repointed_tags_mutants.py"])
def test_copied_conftest_dependency_inventory(consumer: str) -> None:
    tests = Path(__file__).parent
    tree = ast.parse((tests / 'conftest.py').read_bytes())
    required = {'conftest.py'} | {
        node.module + '.py' for node in tree.body
        if isinstance(node, ast.ImportFrom) and node.module
        and (tests / (node.module + '.py')).is_file()
    } | {
        alias.name + '.py' for node in tree.body if isinstance(node, ast.Import)
        for alias in node.names if (tests / (alias.name + '.py')).is_file()
    }
    source = ast.parse((tests / consumer).read_bytes())
    inventories = [set(n.value for n in node.elts if isinstance(n, ast.Constant) and isinstance(n.value, str))
                   for node in ast.walk(source) if isinstance(node, ast.Tuple)]
    assert any(required <= inventory for inventory in inventories), (consumer, required)


# Cleanup failures retain the body error and restore the enclosing owner;
# the caller, not a fallback/suppression, then disposes of this known scratch.
@pytest.mark.parametrize("body_fails", [False, True])
def test_cleanup_failure_restores_outer_owner(tmp_path, monkeypatch, body_fails):
    import child_cli_environment as harness

    root = tmp_path / "failed-cleanup"
    failure = OSError("exact cleanup fault")
    body = RuntimeError("exact body fault")

    def refuse(_root):
        raise failure

    expected = BaseExceptionGroup if body_fails else OSError
    try:
        with monkeypatch.context() as patch:
            patch.setattr(harness, "_remove_tree", refuse)
            with pytest.raises(expected) as caught:
                with owned_cli_environment(root):
                    assert Path(child_cli_environment()["TMPDIR"]) == root
                    (root / "canary").write_bytes(b"preserved on refusal")
                    if body_fails:
                        raise body
            if body_fails:
                assert caught.value.exceptions == (body, failure)
            else:
                assert caught.value is failure
            assert (root / "canary").read_bytes() == b"preserved on refusal"
            assert Path(child_cli_environment()["TMPDIR"]) == tmp_path / "cli-tmp"
    finally:
        harness._remove_tree(root)
    assert not root.exists()
