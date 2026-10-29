"""CLI process death leaves acquisition state only in its fixture's lifetime."""
from __future__ import annotations

import os
import tempfile
from pathlib import Path

import pytest

from child_cli_environment import owned_cli_environment

pytest_plugins = ("pytester",)

CHILD = r'''
import hashlib, json, os, signal, sys
from pathlib import Path
from csk import cli, installer
select = installer.source_modes.select
def selected(**kwargs):
    mode = select(**kwargs)
    workspace = kwargs['workspace']
    (workspace / 'witness').write_bytes(b'acquired')
    print(json.dumps({'workspace': str(workspace), 'pid': os.getpid(), 'implementation': installer.__file__, 'sha256': hashlib.sha256(Path(installer.__file__).read_bytes()).hexdigest(), 'temp_env': [os.environ[name] for name in ('TMPDIR', 'TEMP', 'TMP')], 'home': os.environ['HOME'], 'config': os.environ['CSK_CONFIG']}), flush=True)
    if sys.argv[1] == 'kill':
        os.kill(os.getpid(), signal.SIGTERM if os.name == 'nt' else signal.SIGKILL)
    if sys.argv[1] == 'crash':
        os._exit(86)
    return mode
installer.source_modes.select = selected
sys.exit(cli.main(['install', 'app', '--fix-gitignore', '--dry-run']))
'''

INNER = r'''
import hashlib, json, os, signal, stat, subprocess, sys, tempfile
from pathlib import Path
import pytest
from test_mcp_install_acceptance import _case

from child_cli_environment import child_cli_environment, owned_cli_environment
records = []
cleanup_records = []
cache_records = []
# A naturally cold parent cache stays valid across actual autouse-fixture teardown; no prewarming or cache reset.
def test_cold_cache_first(tmp_path):
    print('FIXTURE_SESSION_PID', os.getpid(), flush=True)
    assert tempfile.tempdir is None
    inherited = {name: os.environ[name] for name in ('TMPDIR', 'TEMP', 'TMP')}
    with tempfile.TemporaryDirectory() as directory:
        assert Path(directory).parent == Path(inherited['TMPDIR'])
    case = _case(tmp_path / 'cache-case')
    root = Path(case.env['TMPDIR'])
    assert root.is_relative_to(tmp_path) and root != Path(inherited['TMPDIR'])
    (root / 'first-fixture').write_bytes(b'owned')
    cache_records.append((tempfile.gettempdir(), root, inherited))

def test_cold_cache_next(tmp_path):
    cached, previous, inherited = cache_records[0]
    assert not previous.exists()
    assert {name: os.environ[name] for name in inherited} == inherited
    assert tempfile.gettempdir() == cached and Path(cached).is_dir()
    with tempfile.TemporaryDirectory() as directory:
        assert Path(directory).parent == Path(cached)
    case = _case(tmp_path / 'cache-case')
    root = Path(case.env['TMPDIR'])
    assert root != previous and root.is_dir()

# Independent fixtures cannot remove a sibling's canary, and teardown must remove the previous child's workspace.
@pytest.mark.parametrize('mode', ['normal', 'kill', 'crash'])
def test_cli(tmp_path, mode):
    for previous in records:
        assert not Path(previous['workspace']).exists(), 'fixture teardown retained acquisition workspace'
    case = _case(tmp_path / 'case')
    canary = Path(os.environ['OUTSIDE_CANARY'])
    before = (canary.read_bytes(), canary.stat().st_mode)
    command = [sys.executable, '-c', CHILD, mode]
    if mode == 'crash':
        # A nested CLI inherits exactly the environment passed to its launcher.
        command = [sys.executable, '-c', 'import subprocess,sys; p=subprocess.run(sys.argv[1:],capture_output=True,text=True,timeout=45); print(p.stdout,end=""); sys.exit(p.returncode)', *command]
    process = subprocess.Popen(command, cwd=case.project, env=dict(case.env), stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    try:
        stdout, stderr = process.communicate(timeout=60)
    finally:
        if process.poll() is None:
            process.kill()
            process.wait()
    expected = (signal.SIGTERM if os.name == 'nt' else -signal.SIGKILL) if mode == 'kill' else 86 if mode == 'crash' else 0
    assert process.returncode == expected, stdout + stderr
    record = next(json.loads(line) for line in stdout.splitlines() if line.startswith('{"workspace"'))
    implementation = Path(case.env['PYTHONPATH']) / 'csk/installer.py'
    assert Path(record['implementation']) == implementation
    assert record['sha256'] == hashlib.sha256(implementation.read_bytes()).hexdigest()
    assert record['temp_env'] == [str(Path(record['workspace']).parent)] * 3
    assert record['home'] == case.env['HOME'] and record['config'] == case.env['CSK_CONFIG']
    record['launcher_pid'] = process.pid
    record['skillfile'] = str(case.project / 'Skillfile.json')
    record['skillfile_bytes'] = (case.project / 'Skillfile.json').read_text()
    records.append(record)
    print('CHILD_RECORD', json.dumps(record), flush=True)
    workspace = Path(record['workspace'])
    assert (canary.read_bytes(), canary.stat().st_mode) == before
    assert mode == 'normal' or workspace.is_relative_to(tmp_path), 'acquisition workspace escaped owning test subtree'
    if mode != 'normal':
        assert (workspace / 'witness').read_bytes() == b'acquired'
    else:
        assert not workspace.exists(), 'normal installer exit must clean immediately'


# Child defaults are confined without changing the parent env/cache; explicit caller overrides remain authoritative in a fresh child.
def test_explicit_child_environment(tmp_path):
    before = dict(os.environ)
    cached = tempfile.gettempdir()
    env = child_cli_environment()
    root = Path(env['TMPDIR'])
    assert root.is_relative_to(tmp_path)
    assert [env[k] for k in ('TMPDIR', 'TEMP', 'TMP')] == [str(root)] * 3
    explicit = tmp_path / 'explicit'
    explicit.mkdir()
    env.update({k: str(explicit) for k in ('TMPDIR', 'TEMP', 'TMP')})
    env.update(HOME=str(explicit / 'h'), USERPROFILE=str(explicit / 'h'), CSK_CONFIG=str(explicit / 'config'))
    process = subprocess.Popen([sys.executable, '-c', 'import os,json,tempfile; print(json.dumps(dict(temp=tempfile.gettempdir(), home=os.environ["HOME"], config=os.environ["CSK_CONFIG"])))'], env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    try:
        out, err = process.communicate(timeout=30)
    finally:
        if process.poll() is None:
            process.kill()
            process.wait()
    print('EXPLICIT_CHILD_PID', process.pid, process.returncode, flush=True)
    assert process.returncode == 0, err
    assert json.loads(out) == dict(temp=str(explicit), home=env['HOME'], config=env['CSK_CONFIG'])
    assert dict(os.environ) == before and tempfile.gettempdir() == cached


def teardown_module():
    assert len(records) == 3
    assert len({r['workspace'] for r in records}) == 3
    assert all(not Path(r['workspace']).exists() for r in records)
    assert all(Path(r['skillfile']).read_text() == r['skillfile_bytes'] for r in records)
'''


# Real dry-run acquisition workspace death (SIGKILL and nested crash), normal cleanup and independent fixture teardown stay inside owned temp state.
def test_acquisition_children_are_fixture_owned(pytester, monkeypatch):
    _run_owned_session(pytester, monkeypatch, INNER, passed=6)


def _run_owned_session(pytester, monkeypatch, body, *, passed):
    tests = Path(__file__).resolve().parent
    pytester.makeconftest((tests / "conftest.py").read_text())
    pytester.makepyfile("CHILD = " + repr(CHILD) + "\n" + body)
    # Explicit dir neither initializes nor replaces the parent's tempfile cache.
    shared = Path(tempfile.mkdtemp(prefix="p-", dir=os.environ.get("TMPDIR") or tempfile.gettempdir()))
    with owned_cli_environment(shared):
        canary = shared / "outside-canary"
        canary.write_bytes(b"outside unchanged")
        if os.name == "posix":
            canary.chmod(0o444)
        before = (canary.read_bytes(), canary.stat().st_mode, canary.stat().st_ino)
        with monkeypatch.context() as mp:
            for name in ("TMPDIR", "TEMP", "TMP"):
                mp.setenv(name, str(shared))
            for name in ("HOME", "USERPROFILE", "XDG_CONFIG_HOME", "XDG_CACHE_HOME", "XDG_DATA_HOME", "CSK_HOME", "UV_CACHE_DIR"):
                path = shared / name.lower()
                path.mkdir(exist_ok=True)
                mp.setenv(name, str(path))
            mp.setenv("OUTSIDE_CANARY", str(canary))
            mp.setenv("PYTHONPATH", os.pathsep.join((str(tests.parent / "src"), str(tests))))
            result = pytester.runpytest_subprocess("-q", "-s", "-m", "not csk_e2e_native", "--basetemp=" + str(pytester.path / "owned-session"))
        result.assert_outcomes(passed=passed)
        assert not list(shared.glob(".csk-source-acquired-*"))
        assert (canary.read_bytes(), canary.stat().st_mode, canary.stat().st_ino) == before
    assert not shared.exists()


# POSIX permission bits and Unix-domain sockets have their own executable platform contract.
@pytest.mark.skipif(os.name != "posix", reason="POSIX directory permissions and AF_UNIX contract")
def test_posix_cleanup_and_socket_lifetime(pytester, monkeypatch):
    _run_owned_session(pytester, monkeypatch, POSIX, passed=9)


POSIX = r'''
import os, stat, subprocess, sys, tempfile
from pathlib import Path
import pytest
from child_cli_environment import child_cli_environment, owned_cli_environment
cleanup_records = []

# With a cold cache, the parent executes the existing socket integrity gate under the caller's short temp root.
def test_cold_parent_socket_refusal(tmp_path):
    print('FIXTURE_SESSION_PID', os.getpid(), flush=True)
    assert tempfile.tempdir is None
    from test_package_snapshot import test_admitted_special_files_and_links_refuse_naming_the_path as check
    try:
        check(tmp_path, 'unix-socket')
    except pytest.skip.Exception as exc:
        pytest.fail('parent lost socket coverage: ' + str(exc))
    print('PARENT_SOCKET_REFUSAL_ASSERTED', tempfile.gettempdir(), flush=True)

# A single hardlink (without enumeration-order helpers) survives teardown unchanged for writable, 0500 and 0000 owning dirs.
@pytest.mark.parametrize('mode', [0o700, 0o500, 0])
def test_single_hardlink_cleanup(tmp_path, mode):
    root = Path(child_cli_environment()['TMPDIR'])
    outside = Path(os.environ['OUTSIDE_CANARY']).parent / ('hard-' + str(mode))
    outside.write_bytes(b'outside hardlink')
    outside.chmod(0o444)
    before = (outside.read_bytes(), outside.stat().st_mode, outside.stat().st_ino)
    owned = root / 'restricted'
    owned.mkdir()
    os.link(outside, owned / 'only-file')
    assert (owned / 'only-file').stat().st_ino == before[2]
    owned.chmod(mode)
    sibling = tmp_path / 'keep'
    sibling.write_bytes(b'sibling')
    cleanup_records.append((root, outside, before, sibling))

def test_single_hardlink_after_teardown():
    for root, outside, before, sibling in cleanup_records:
        after = (outside.read_bytes(), outside.stat().st_mode, outside.stat().st_ino)
        print('HARDLINK_RECORD', root, before, after, flush=True)
        assert after == before and not root.exists()
        assert sibling.read_bytes() == b'sibling'

# Exclusive readonly files can be unlinked; file/directory symlinks never grant permission changes to their referents, even on body failure.
@pytest.mark.parametrize('mode', [0o700, 0o500, 0])
def test_exception_and_symlink_cleanup(tmp_path, mode):
    outside = tmp_path / 'outside'
    outside.mkdir()
    target = outside / 'target'
    target.write_bytes(b'outside target')
    target.chmod(0o444)
    before = (target.read_bytes(), target.stat().st_mode, target.stat().st_ino)
    outside.chmod(0o500)
    directory_before = (outside.stat().st_mode, outside.stat().st_ino)
    root = tmp_path / 'owned'
    failure = RuntimeError('exact fixture exception')
    fd = None
    try:
        with pytest.raises(RuntimeError) as caught:
            with owned_cli_environment(root):
                env = child_cli_environment()
                assert Path(env['TMPDIR']) == root
                (root / 'file-link').symlink_to(target)
                (root / 'directory-link').symlink_to(outside, target_is_directory=True)
                d = root / 'restricted'
                d.mkdir()
                (d / 'exclusive').write_bytes(b'owned readonly')
                (d / 'exclusive').chmod(0o444)
                fd = os.open(d / 'exclusive', os.O_RDONLY)
                file_before = os.fstat(fd)
                d.chmod(mode)
                raise failure
        assert caught.value is failure and not root.exists()
        assert (target.read_bytes(), target.stat().st_mode, target.stat().st_ino) == before
        assert (outside.stat().st_mode, outside.stat().st_ino) == directory_before
        assert os.fstat(fd).st_mode == file_before.st_mode and os.fstat(fd).st_ino == file_before.st_ino
        assert os.read(fd, 100) == b'owned readonly'
    finally:
        if fd is not None:
            os.close(fd)
    outside.chmod(0o700)

# A fresh child's ordinary short temp default must execute the existing Unix-socket package-member refusal, never convert it to a skip.
def test_fresh_child_socket_refusal():
    code = """
import pytest, tempfile
from pathlib import Path
from test_package_snapshot import test_admitted_special_files_and_links_refuse_naming_the_path as check
with tempfile.TemporaryDirectory(prefix='p-') as root:
    try: check(Path(root), 'unix-socket')
    except pytest.skip.Exception as exc:
        print('SOCKET_SKIP', exc, flush=True)
        raise SystemExit(77)
print('SOCKET_REFUSAL_ASSERTED', flush=True)
"""
    process = subprocess.Popen([sys.executable, '-c', code], stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    try:
        out, err = process.communicate(timeout=30)
    finally:
        if process.poll() is None:
            process.kill()
            process.wait()
    print('SOCKET_CHILD_PID', process.pid, process.returncode, out, flush=True)
    assert process.returncode == 0, out + err
    assert 'SOCKET_REFUSAL_ASSERTED' in out

'''
