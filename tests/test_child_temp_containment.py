"""CLI process death leaves acquisition state only in its fixture's lifetime."""
from __future__ import annotations

import os
from pathlib import Path

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
import hashlib, json, os, signal, subprocess, sys
from pathlib import Path
import pytest
from test_mcp_install_acceptance import _case

records = []
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

def teardown_module():
    assert len(records) == 3
    assert len({r['workspace'] for r in records}) == 3
    assert all(not Path(r['workspace']).exists() for r in records)
    assert all(Path(r['skillfile']).read_text() == r['skillfile_bytes'] for r in records)
'''


# Real dry-run acquisition workspace death (SIGKILL and nested crash), normal cleanup and independent fixture teardown stay inside owned temp state.
def test_acquisition_children_are_fixture_owned(pytester, monkeypatch):
    tests = Path(__file__).resolve().parent
    pytester.makeconftest((tests / "conftest.py").read_text())
    pytester.makepyfile("CHILD = " + repr(CHILD) + "\n" + INNER)
    shared = pytester.path / "shared-temp"
    shared.mkdir()
    canary = shared / "outside-canary"
    canary.write_bytes(b"outside unchanged")
    canary.chmod(0o444)
    before = (canary.read_bytes(), canary.stat().st_mode)
    for name in ("TMPDIR", "TEMP", "TMP"):
        monkeypatch.setenv(name, str(shared))
    monkeypatch.setenv("OUTSIDE_CANARY", str(canary))
    monkeypatch.setenv("PYTHONPATH", os.pathsep.join((str(tests.parent / "src"), str(tests))))
    result = pytester.runpytest_subprocess("-q", "-s", "--basetemp=" + str(pytester.path / "owned-session"))
    result.assert_outcomes(passed=3)
    assert not list(shared.glob(".csk-source-acquired-*"))
    assert (canary.read_bytes(), canary.stat().st_mode) == before
