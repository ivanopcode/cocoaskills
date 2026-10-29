"""Real isolated CLI with portable Linux path observations, never global I/O faults.

macOS cannot create invalid-byte filenames. The proxy supplies the Linux
surrogateescape spelling while all file I/O/digests use actual fixture files.
On Windows only, Linux owner/mode observations are modeled too. This is not
native Linux filesystem qualification.
"""
from __future__ import annotations

import io
import json
import os
from dataclasses import replace
from pathlib import Path
import stat
import subprocess
import sys
import tempfile
import tomllib
from types import SimpleNamespace

import pytest

from test_mcp_install_acceptance import SURFACES, _case, _snapshot


class LinuxPath:
    """Keep fixture file access separate from the observed Linux realpath."""

    def __init__(self, real: Path, spelling: str):
        self.real, self.spelling = real, spelling

    def __str__(self):
        return self.spelling

    def __fspath__(self):
        return str(self.real)

    def __truediv__(self, tail):
        return LinuxPath(self.real / tail, self.spelling + '/' + tail)

    def resolve(self, *, strict=False):
        assert self.real.resolve(strict=strict) == self.real
        return self

    def is_relative_to(self, other):
        return self.real.is_relative_to(other.real)

    def stat(self):
        return self.real.stat()

    @property
    def parent(self):
        return LinuxPath(self.real.parent, self.spelling.rsplit('/', 1)[0])

    def __eq__(self, other):
        return isinstance(other, LinuxPath) and self.real == other.real


def child():
    import hashlib
    from csk import cli, mcp_launchers as gate

    root = Path(os.environ['PATH_FIXTURE_ROOT'])
    # Explicit Linux decoding, independent of the host's filesystem codec.
    name = bytes.fromhex(os.environ['PATH_FIXTURE_HEX']).decode('utf-8', 'surrogateescape')
    observed = LinuxPath(root, str(root.parent / name))
    gate._platform = lambda: 'linux'
    gate._linux_root = lambda unused: observed
    if os.name == 'nt':
        # Windows has no POSIX owner/mode model; only this launcher seam uses it.
        gate.linux_uid = lambda: 1000
        def metadata(path):
            unsafe = os.environ.get('PATH_FIXTURE_UNSAFE')
            writable = (unsafe == 'root' and path.real == root or
                        unsafe == 'parent' and path.real == root / 'App/bin')
            kind = stat.S_IFREG if path.real.is_file() else stat.S_IFDIR
            return SimpleNamespace(st_uid=1000, st_mode=kind | (0o775 if writable else 0o755))
        gate.linux_metadata = metadata

    # This hook observes attempts without replacing subprocess or OS functions.
    executable = observed / 'App/bin/server.exe'
    forbidden = {str(executable), os.fspath(executable)}
    def contains_executable(value):
        if isinstance(value, str):
            return value in forbidden
        if isinstance(value, (list, tuple)):
            return any(contains_executable(item) for item in value)
        return False
    def audit(event, args):
        if event in {'subprocess.Popen', 'os.exec', 'os.posix_spawn'} and contains_executable(args):
            Path(os.environ['PATH_FIXTURE_EXECUTED']).write_text('execution attempted')
            raise AssertionError('launcher execution is forbidden during install')
    sys.addaudithook(audit)

    if os.environ.get('PATH_FIXTURE_APPROVE') == '1':
        class Input(io.StringIO):
            def isatty(self):
                return True
        class Output:
            def isatty(self):
                return True
            def __getattr__(self, name):
                return getattr(sys.__stdout__, name)
        # Only these child-owned streams represent an operator granting consent.
        sys.stdin, sys.stdout = Input('yes\n'), Output()
    print('ORIGIN', cli.__file__, hashlib.sha256(Path(gate.__file__).read_bytes()).hexdigest(), file=sys.stderr)
    if sys.argv[1] == 'execution-control':
        subprocess.run([os.fspath(executable)], check=True)
        raise AssertionError('execution observer did not block the attempt')
    raise SystemExit(cli.main(sys.argv[2:]))


NAMES = [b'invalid-\xff', b'caf\xc3\xa9-\xfe\xff', b'ordinary', 'приложение-📦'.encode()]
IDS = ['invalid-byte', 'mixed-invalid-bytes', 'ordinary', 'non-ascii']


def fixture(tmp_path):
    case = _case(tmp_path / 'case', ignore_mode=None)
    root = tmp_path / 'launcher-root'
    helper = root / 'App/bin/server.exe'
    helper.parent.mkdir(parents=True)
    marker = tmp_path / 'executed'
    helper.write_text('#!/bin/sh\necho forbidden > "' + str(marker) + '"\n')
    for path in [root, root / 'App', helper.parent, helper]:
        path.chmod(0o755)
    case.declare({'app': {'server': {'transport': 'stdio', 'launcher': {'linux': {
        'path': '${XDG_CONFIG_HOME:-~/.config}/App/bin/server.exe', 'trust': 'allow-unverified'}}}}})
    return case, root, marker


def lifecycle_fixture(tmp_path, consumer):
    from csk import config

    case, root, marker = fixture(tmp_path)
    cfg = config.load_config(Path(case.env['CSK_CONFIG']))
    config.save_config(replace(cfg, default_agents=[consumer], projects={
        **cfg.projects, 'app': replace(cfg.projects['app'], agents=[consumer]),
    }))
    case.skillfile['agents'] = [consumer]
    case.skillfile['mcp_servers']['app']['server']['environments'] = [consumer]
    case.declare(case.skillfile['mcp_servers'])
    return case, root, marker


def unselected_state(case, consumer):
    roots = {'claude_code': '.claude', 'codex_cli': '.codex', 'cursor': '.cursor'}
    paths = [path for agent, path in SURFACES if agent != consumer]
    paths.extend(path for agent, path in roots.items() if agent != consumer)
    return {key: value for key, value in _snapshot(case.project).items()
            if any(key[1] == path or key[1].startswith(path + '/') for path in paths)}


def invoke(case, root, marker, name, *, dry=False, unsafe=None, approve=False, execution_control=False):
    env = dict(case.env, PATH_FIXTURE_ROOT=str(root), PATH_FIXTURE_HEX=name.hex(),
               PATH_FIXTURE_EXECUTED=str(marker), PATH_FIXTURE_UNSAFE=unsafe or '',
               PATH_FIXTURE_APPROVE='1' if approve else '0', PYTHONDONTWRITEBYTECODE='1')
    roots = (case.project, case.home, root, Path(case.env['HOME']))
    before = _snapshot(*roots)
    with tempfile.TemporaryDirectory(prefix='cli-') as temp:
        env.update(TMPDIR=temp, TMP=temp, TEMP=temp)
        process = subprocess.Popen([sys.executable, str(Path(__file__).resolve()), 'execution-control' if execution_control else 'child',
            'install', 'app', '--fix-gitignore', *(['--dry-run'] if dry else [])],
            cwd=case.project, env=env, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
        try:
            output, _ = process.communicate(timeout=60)
        except BaseException:
            if process.poll() is None:
                process.kill()
            output, _ = process.communicate()
            print('interrupted child PID', process.pid, 'exit', process.returncode,
                  'snapshot before', before, 'snapshot after', _snapshot(*roots),
                  output.decode('utf-8', 'backslashreplace'))
            raise
    unchanged = _snapshot(*roots) == before
    print('child PID', process.pid, 'exit', process.returncode, 'reaped', process.poll() is not None,
          'snapshot unchanged', unchanged,
          output.decode('utf-8', 'backslashreplace'))
    if execution_control:
        assert marker.read_text() == 'execution attempted'
    else:
        assert not marker.exists(), 'helper/server execution attempted'
    return process.returncode, output, unchanged


# Repaired invalid spellings refuse explicitly; valid neighbors reach consent or
# preview. Unsafe roots/parents retain their mode refusal and no partial writes.
@pytest.mark.parametrize('name', NAMES, ids=IDS)
@pytest.mark.parametrize('unsafe', [None, 'root', 'parent'])
@pytest.mark.parametrize('dry', [False, True], ids=['install', 'dry-run'])
def test_launcher_encoding_cli(tmp_path, name, unsafe, dry):
    case, root, marker = fixture(tmp_path)
    if unsafe:
        (root if unsafe == 'root' else root / 'App/bin').chmod(0o775)
    code, output, unchanged = invoke(case, root, marker, name, dry=dry, unsafe=unsafe)
    invalid = any(0xD800 <= codepoint <= 0xDFFF for codepoint in map(ord, name.decode('utf-8', 'surrogateescape')))
    # Check state even on the old-head silent-success witness, before status.
    assert unchanged
    assert not any((case.project / path).exists() for _, path in SURFACES)
    assert not (case.project / '.agents/skills/seed').exists()
    if unsafe or invalid:
        assert code == 1, output
        assert b'mcp_launcher_path_invalid' in output, output
        assert (b'group/world write is forbidden' if unsafe else b'valid UTF-8') in output
    elif dry:
        assert code == 0, output
        assert b'would: mcp app' in output and b'approval: required' in output
    else:
        assert code == 1 and b'mcp_plan_unapproved' in output, output


# All eight paths/lifecycles use real consent, config/grant/seed/lock publication,
# mode refusal, repair and consent reuse/refusal for one selected consumer each.
# Claude/Codex alternate across the table; unselected surfaces stay unchanged.
# This proves the launcher contract, not an all-consumer transaction cross-product.
@pytest.mark.parametrize('name', NAMES, ids=IDS)
@pytest.mark.parametrize('dry', [False, True], ids=['install', 'dry-run'])
def test_permission_repair_with_existing_consent(tmp_path, name, dry):
    consumer = ('claude_code', 'codex_cli')[(NAMES.index(name) + int(dry)) % 2]
    case, root, marker = lifecycle_fixture(tmp_path, consumer)
    neighbors = unselected_state(case, consumer)
    valid = name in NAMES[2:]
    consent_name = name if valid else b'ordinary'
    code, output, _ = invoke(case, root, marker, consent_name, approve=True)
    surface = dict(SURFACES)[consumer]
    row = f'mcp app -> {consumer} {surface}'.encode()
    assert code == 0 and row + b' (added)' in output, output
    assert b'Approve 1 MCP server(s)' in output
    assert all((case.project / path).exists() == (agent == consumer) for agent, path in SURFACES)
    native_bytes = (case.project / surface).read_bytes()
    native = (json.loads(native_bytes)['mcpServers'] if consumer == 'claude_code'
              else tomllib.loads(native_bytes.decode('utf-8'))['mcp_servers'])
    assert set(native) == {'app'}
    assert native['app']['command'] == str(root.parent / consent_name.decode('utf-8')) + '/App/bin/server.exe'
    seed = (case.project / 'source/seed/SKILL.md').read_bytes()
    assert (case.project / '.agents/skills/seed/SKILL.md').read_bytes() == seed
    adapter = '.claude' if consumer == 'claude_code' else '.codex'
    assert (case.project / adapter / 'skills/seed/SKILL.md').read_bytes() == seed
    assert (case.project / 'Skillfile.lock.json').is_file()
    assert list((case.home / 'source-v1/bindings').rglob('*.json'))
    grants = list((case.home / 'state/mcp/consent').glob('*.json'))
    assert len(grants) == 1 and len(json.loads(grants[0].read_bytes())['approved']) == 1
    ledgers = list((case.home / 'state/mcp/ledger').glob('*.json'))
    assert len(ledgers) == 1
    assert [entry['address']['agent'] for entry in json.loads(ledgers[0].read_bytes())['entries']] == [consumer]
    assert not list((case.home / 'state/transactions').rglob('*.json'))
    assert unselected_state(case, consumer) == neighbors
    root.chmod(0o775)
    code, output, unchanged = invoke(case, root, marker, name, dry=dry, unsafe='root')
    assert unchanged and code == 1 and b'mcp_launcher_path_invalid' in output, output
    assert b'group/world write is forbidden' in output, output
    assert unselected_state(case, consumer) == neighbors
    root.chmod(0o755)
    code, output, unchanged = invoke(case, root, marker, name, dry=dry)
    assert unchanged
    assert unselected_state(case, consumer) == neighbors
    assert b'Approve ' not in output
    if valid:
        assert code == 0 and row in output, output
        assert (b'would: ' + row in output) == dry, output
        assert b'approval: required' not in output and b'mcp_plan_unapproved' not in output
    else:
        assert code == 1 and b'mcp_launcher_path_invalid' in output and b'valid UTF-8' in output, output


# A known fixture-only execution attempt trips the observer before the stub runs;
# adjacent real CLI controls exercise successful installs without tripping it.
def test_execution_observer_control(tmp_path):
    case, root, marker = fixture(tmp_path)
    code, output, unchanged = invoke(case, root, marker, b'ordinary', execution_control=True)
    assert unchanged and code == 1
    assert b'launcher execution is forbidden during install' in output


if __name__ == '__main__':
    child()
