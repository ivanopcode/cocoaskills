from __future__ import annotations

import os
import shlex
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from csk import cli
from test_shell_approvals import _line, _run_hook, _shell_tool, _store


SHAPES = ['group-file', 'world-file', 'group-dir', 'world-dir', 'symlink-file', 'symlink-dir', 'foreign-file', 'foreign-dir', 'nonregular-file']
BOUNDARIES = '\n\r\x0b\x0c\x1c\x1d\x1e\x85\u2028\u2029'


def _unsafe_store(tmp_path: Path, shape: str, env: Path) -> tuple[Path, bytes]:
    store = _store()
    store.parent.mkdir(parents=True, mode=0o700)
    store.write_bytes((_line(env) + '\n').encode())
    store.chmod(0o600)
    original = store.read_bytes()
    if shape.endswith('file') and shape.startswith(('group', 'world')):
        store.chmod(0o660 if shape == 'group-file' else 0o666)
    elif shape.endswith('dir') and shape.startswith(('group', 'world')):
        store.parent.chmod(0o770 if shape == 'group-dir' else 0o777)
    elif shape == 'symlink-file':
        target = tmp_path / 'real-store'
        store.rename(target)
        store.symlink_to(target)
    elif shape == 'symlink-dir':
        target = tmp_path / 'real-dir'
        store.parent.rename(target)
        store.parent.symlink_to(target, target_is_directory=True)
    elif shape == 'nonregular-file':
        store.unlink()
        store.mkdir()
    return store, original


@pytest.mark.skipif(os.name == 'nt', reason='POSIX ownership and mode contract')
@pytest.mark.parametrize('shell', ['bash', 'zsh'])
@pytest.mark.parametrize('scope', ['project', 'global'])
@pytest.mark.parametrize('shape', SHAPES)
def test_untrusted_store_never_sources(tmp_path: Path, shell: str, scope: str, shape: str) -> None:
    project = tmp_path / 'project'
    project.mkdir()
    directory = project / '.agents' if scope == 'project' else Path.home() / '.cocoaskills/global'
    directory.mkdir(parents=True)
    env = directory / 'env.sh'
    marker = tmp_path / 'executed'
    env.write_text(f'touch {shlex.quote(str(marker))}\n')
    store, _ = _unsafe_store(tmp_path, shape, env)
    script = '_csk_auto_env; _csk_auto_env'
    if shape.startswith('foreign'):
        # Exercise real ls metadata with a controlled current-uid mismatch.
        # No privilege is required, and no production trust branch is mocked.
        target = store if shape == 'foreign-file' else store.parent
        real_ls = shutil.which('ls')
        assert real_ls
        # Only one object's owner column is changed; the other remains real.
        fixture = tmp_path / 'ls'
        fixture.write_text('#!/bin/sh\n' + shlex.quote(real_ls) + ' "$@" | ' + shlex.quote(shutil.which('awk') or '') + ' -v target=' + shlex.quote(str(target)) + ' -v uid=' + str(os.geteuid() + 1) + ' \'{if ($NF == target) $3 = uid; print}\'\n')
        fixture.chmod(0o755)
        script = f'ls() {{ {shlex.quote(str(fixture))} "$@"; }}; ' + script
        # Install fixture before initial hook activation as well.
        original = project / '.agents/env.sh' if scope == 'project' else env
        assert original == env
        from csk import shell_init
        hook = tmp_path / 'hook.sh'
        hook.write_text(shell_init.shell_init(shell))
        executable = shutil.which(shell)
        if executable is None:
            pytest.skip(f'{shell} unavailable')
        args = [executable, '-dfc'] if shell == 'zsh' else [executable, '--noprofile', '--norc', '-c']
        result = subprocess.run([*args, script.split('; _csk_auto_env')[0] + '; . "$HOOK"; _csk_auto_env; _csk_auto_env'], cwd=project, env={**os.environ, 'PWD': str(project), 'HOOK': str(hook), 'SHELL': executable}, text=True, capture_output=True, timeout=30)
    else:
        result = _run_hook(tmp_path, shell, project, script)
    assert result.returncode == 0, result.stderr
    assert not marker.exists(), result.stderr
    assert result.stderr.count('untrusted approval store') == 1, result.stderr
    assert str(store) in result.stderr
    causes = {
        'group-file': 'has mode -rw-rw---- writable by group or others (fix: chmod 600',
        'world-file': 'has mode -rw-rw-rw- writable by group or others (fix: chmod 600',
        'group-dir': 'has mode drwxrwx--- writable by group or others (fix: chmod 700',
        'world-dir': 'has mode drwxrwxrwx writable by group or others (fix: chmod 700',
        'symlink-file': 'is a symlink',
        'symlink-dir': 'is a symlink',
        'foreign-file': 'is owned by uid',
        'foreign-dir': 'is owned by uid',
        'nonregular-file': 'is not a regular file',
    }
    assert causes[shape] in result.stderr, result.stderr
    if shape not in {'group-file', 'world-file', 'group-dir', 'world-dir'}:
        assert 'chmod 700' not in result.stderr and 'chmod 600' not in result.stderr, result.stderr


@pytest.mark.skipif(os.name == 'nt', reason='POSIX ownership and mode contract')
@pytest.mark.parametrize('shape', SHAPES)
def test_cli_refuses_untrusted_store_without_laundering(tmp_path: Path, shape: str) -> None:
    from csk import shell_approvals
    env = tmp_path / 'env.sh'
    env.write_text('# forged approval\n')
    store, original = _unsafe_store(tmp_path, shape, env)
    before_mode = store.lstat().st_mode
    dir_mode = store.parent.lstat().st_mode
    safe = tmp_path / 'safe.sh'
    safe.write_text('# unrelated\n')
    for argv in [['shell', 'approve', str(safe), '--yes'], ['shell', 'revoke', str(env)], ['shell', 'approvals']]:
        setup = ''
        if shape.startswith('foreign'):
            target = store if shape == 'foreign-file' else store.parent
            setup = 'import os; from pathlib import Path; from csk import shell_approvals; original=os.lstat; target=Path(' + repr(str(target)) + ');\ndef foreign(path, *a, **kw):\n s=original(path,*a,**kw)\n if Path(path)==target:\n  values=list(s); values[4]=os.geteuid()+1; return os.stat_result(values)\n return s\nos.lstat=foreign\n'
        result = subprocess.run([sys.executable, '-c', setup + 'from csk.cli import main; raise SystemExit(main(' + repr(argv) + '))'], env={**os.environ, 'PYTHONPATH': str(Path(__file__).resolve().parents[1] / 'src')}, capture_output=True, text=True, timeout=30)
        assert result.returncode == 2, result.stdout + result.stderr
        assert 'shell_approval_store_untrusted' in result.stderr
        assert 'Traceback' not in result.stderr
        assert store.lstat().st_mode == before_mode
        assert store.parent.lstat().st_mode == dir_mode
        if shape != 'nonregular-file':
            assert store.read_bytes() == original
    assert shell_approvals.approval_file() == store


@pytest.mark.parametrize('separator', list(BOUNDARIES), ids=lambda c: f'U+{ord(c):04X}')
@pytest.mark.parametrize('command', ['approve', 'revoke'])
def test_cli_refuses_every_line_boundary(tmp_path: Path, separator: str, command: str, capsys: pytest.CaptureFixture[str]) -> None:
    path = tmp_path / ('name' + separator + 'end.sh')
    # No filesystem fixture is needed on Windows, where controls are invalid.
    assert cli.main(['shell', command, str(path), *(['--yes'] if command == 'approve' else [])]) == 2
    assert 'shell_approval_path_invalid' in capsys.readouterr().err
    assert not _store().exists()


@pytest.mark.parametrize('separator', list(BOUNDARIES.replace('\n', '')), ids=lambda c: f'U+{ord(c):04X}')
def test_unrelated_updates_preserve_legacy_record_bytes(tmp_path: Path, separator: str) -> None:
    store = _store()
    store.parent.mkdir(parents=True, mode=0o700)
    legacy = ('a' * 64 + '  ' + str(tmp_path / ('name' + separator + 'end.sh')) + '\n').encode()
    store.write_bytes(legacy)
    store.chmod(0o600)
    safe = tmp_path / 'safe.sh'
    safe.write_text('# safe\n')
    if ord(separator) < 32:
        assert cli.main(['shell', 'approve', str(safe), '--yes']) == 2
        assert cli.main(['shell', 'revoke', str(safe)]) == 2
        assert store.read_bytes() == legacy
        return
    assert cli.main(['shell', 'approve', str(safe), '--yes']) == 0
    assert store.read_bytes() == legacy + (_line(safe) + '\n').encode()
    assert cli.main(['shell', 'revoke', str(safe)]) == 0
    assert store.read_bytes() == legacy


@pytest.mark.parametrize('command', ['approve', 'revoke'])
def test_cycle_refused_structurally(tmp_path: Path, command: str) -> None:
    path = tmp_path / 'cycle.sh'
    try:
        path.symlink_to(path)
    except OSError:
        pytest.skip('symlinks unavailable')
    result = subprocess.run([sys.executable, '-m', 'csk', 'shell', command, str(path), *(['--yes'] if command == 'approve' else [])], env={**os.environ, 'PYTHONPATH': str(Path(__file__).resolve().parents[1] / 'src')}, capture_output=True, text=True, timeout=30)
    assert result.returncode == 2, result.stderr
    assert 'shell_approval_path_invalid' in result.stderr
    assert 'Traceback' not in result.stderr


@pytest.mark.parametrize('error', [RuntimeError('cycle'), OSError('resolution denied')])
@pytest.mark.parametrize('default', [False, True])
def test_resolution_errors_use_typed_refusal(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], error: Exception, default: bool) -> None:
    def fail(path: Path, *args: object, **kwargs: object) -> Path:
        raise error
    monkeypatch.setenv('PWD', str(tmp_path.resolve()))
    monkeypatch.setattr(Path, 'resolve', fail)
    argv = ['shell', 'approve', '--yes'] if default else ['shell', 'revoke', str(tmp_path / 'env.sh')]
    assert cli.main(argv) == 2
    assert 'shell_approval_path_invalid' in capsys.readouterr().err
    assert not _store().exists()


@pytest.mark.skipif(os.name == 'nt', reason='POSIX descriptor ownership and modes')
@pytest.mark.parametrize('directory', [False, True])
@pytest.mark.parametrize('field', ['owner', 'mode'])
def test_opened_descriptor_trust_is_checked(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], directory: bool, field: str) -> None:
    from csk import env_files, shell_approvals
    env_files.write_env_files(tmp_path / 'project')
    store = _store()
    before = store.read_bytes()
    target = (store.parent if directory else store).stat()
    original = os.fstat
    firings = []
    def foreign(fd: int) -> os.stat_result:
        info = original(fd)
        if (info.st_dev, info.st_ino) == (target.st_dev, target.st_ino):
            values = list(info)
            values[4 if field == 'owner' else 0] = os.geteuid() + 1 if field == 'owner' else info.st_mode | 0o020
            firings.append(fd)
            return os.stat_result(values)
        return info
    monkeypatch.setattr(shell_approvals.os, 'fstat', foreign)
    assert cli.main(['shell', 'approvals']) == 2
    assert firings
    assert 'shell_approval_store_untrusted' in capsys.readouterr().err
    assert store.read_bytes() == before


@pytest.mark.parametrize('mutant', ['skip-dir', 'splitlines', 'line-boundary-nel', 'resolution-runtime', 'directory-open-absence', 'pre-lock-read'])
def test_trust_narrowing_mutants_are_killed(tmp_path: Path, mutant: str) -> None:
    if os.name == 'nt' and mutant in {'skip-dir', 'directory-open-absence'}:
        pytest.skip('POSIX owner/mode and directory-descriptor contract')
    source = Path(__file__).resolve().parents[1]
    checkout = tmp_path / 'mutant'
    shutil.copytree(source / 'src', checkout / 'src')
    shutil.copytree(source / 'tests', checkout / 'tests', ignore=shutil.ignore_patterns('__pycache__'))
    shutil.copy2(source / 'pyproject.toml', checkout / 'pyproject.toml')
    module = checkout / 'src/csk/shell_approvals.py'
    text = module.read_text()
    if mutant == 'skip-dir':
        old = 'def _check_directory(path: Path) -> None:\n'
        new = old + '    return\n'
        test = 'test_cli_refuses_untrusted_store_without_laundering'
        selection = test + ' and world-dir'
        hook = checkout / 'src/csk/shell_init.py'
        hook_text = hook.read_text()
        hook_old = '  _csk_check_meta "$directory" d "store directory" 700 || return 1\n'
        assert hook_text.count(hook_old) == 1
        hook.write_text(hook_text.replace(hook_old, ''))
        selection += ' or (test_untrusted_store_never_sources and world-dir)'
    elif mutant == 'splitlines':
        old = "            lines = content.split(b'\\n')\n            if lines[-1] == b'':\n                lines.pop()"
        new = "            lines = [line.encode('utf-8') for line in content.decode('utf-8').splitlines()]"
        test = 'test_unrelated_updates_preserve_legacy_record_bytes'
        selection = test
    elif mutant == 'line-boundary-nel':
        old = r'\x1e\x85\u2028'
        new = r'\x1e\u2028'
        test = 'test_cli_refuses_every_line_boundary'
        selection = test + ' and U+0085'
    elif mutant == 'pre-lock-read':
        old = '        _check_directory(target.parent)\n        # A separate leaf lock'
        new = '        _check_directory(target.parent)\n        _approval_bytes()\n        # A separate leaf lock'
        test = 'test_update_reads_store_only_under_real_lock'
        selection = test
    elif mutant == 'directory-open-absence':
        old = '        except OSError as exc:\n            # lstat established presence.'
        new = '        except OSError as exc:\n            if isinstance(exc, FileNotFoundError):\n                raise\n            # lstat established presence.'
        test = 'test_directory_open_failure_never_becomes_absence'
        selection = test
    else:
        old = '    except (RuntimeError, OSError) as exc:\n'
        # Keep downstream review and OSError refusals; allow RuntimeError only.
        new = '    except OSError as exc:\n'
        test = 'test_resolution_errors_use_typed_refusal'
        selection = test + ' and error0'
    assert text.count(old) == (2 if mutant == 'resolution-runtime' else 1)
    module.write_text(text.replace(old, new))
    result = subprocess.run([sys.executable, '-m', 'pytest', '-q', 'tests/test_shell_approval_trust.py', '-k', selection, '--basetemp=' + str(tmp_path / 'mutant-tmp')], cwd=checkout, env={**os.environ, 'PYTHONPATH': str(checkout / 'src')}, capture_output=True, text=True, timeout=120)
    (tmp_path / (mutant + '.log')).write_text(result.stdout + result.stderr)
    assert result.returncode == 1, result.stdout + result.stderr
    assert 'FAILED tests/test_shell_approval_trust.py::' + test in result.stdout
    if mutant == 'skip-dir':
        assert 'FAILED tests/test_shell_approval_trust.py::test_untrusted_store_never_sources' in result.stdout


@pytest.mark.parametrize('shell', ['bash', 'zsh'])
def test_trust_hook_never_starts_python(tmp_path: Path, shell: str) -> None:
    if shutil.which(shell) is None:
        pytest.skip(f'{shell} unavailable')
    from csk import env_files
    project = tmp_path / 'project'
    env_files.write_env_files(project)
    tools = tmp_path / 'tools'
    tools.mkdir()
    marker = tmp_path / 'python-started'
    for name in ['python', 'python3']:
        wrapper = tools / name
        wrapper.write_text('#!/bin/sh\nprintf python > ' + shlex.quote(marker.as_posix()) + '\nexit 99\n')
        wrapper.chmod(0o755)
    # Reuse the isolated real-tool wrappers used by digest fallback tests.
    # PATH passed to _run_hook is one directory, not a native PATH list.
    for name in ['id', 'ls', 'tr', 'cmp', 'dirname', 'shasum', 'sha256sum', 'openssl', 'perl', 'cygpath']:
        executable = _shell_tool(shell, name)
        if executable is None:
            continue
        wrapper = tools / name
        wrapper.write_bytes(('#!/bin/sh\nexec ' + shlex.quote(executable.as_posix()) + ' "$@"\n').encode())
        wrapper.chmod(0o755)
    result = _run_hook(tmp_path, shell, project, '_csk_auto_env; _csk_auto_env', PATH=str(tools))
    assert result.returncode == 0, result.stderr
    assert not marker.exists()
    assert result.stderr == ''
    assert result.stdout == f'active={(project / ".agents/env.sh").resolve()}\n'


@pytest.mark.skipif(os.name == 'nt', reason='POSIX directory descriptor trust')
def test_directory_open_failure_never_becomes_absence(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    from csk import env_files, shell_approvals
    env_files.write_env_files(tmp_path / 'project')
    store = _store()
    before = store.read_bytes()
    original = os.open
    calls = []
    def missing(path: object, flags: int, *args: object, **kwargs: object) -> int:
        if path == store.parent:
            calls.append(path)
            raise FileNotFoundError('injected directory open failure after lstat')
        return original(path, flags, *args, **kwargs)
    monkeypatch.setattr(shell_approvals.os, 'open', missing)
    for argv in [['shell', 'approvals'], ['shell', 'revoke', str(tmp_path / 'other.sh')]]:
        assert cli.main(argv) == 2
        assert 'shell_approval_store_read_failed' in capsys.readouterr().err
        assert store.read_bytes() == before
    assert len(calls) == 2


def test_update_reads_store_only_under_real_lock(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from csk import shell_approvals
    env = tmp_path / 'env.sh'
    env.write_text('# reviewed\n')
    original_lock = shell_approvals._ApprovalLock
    original_read = shell_approvals._approval_bytes
    locks = []
    reads = []
    def tracked_lock(path: Path) -> shell_approvals._ApprovalLock:
        lock = original_lock(path)
        locks.append(lock)
        return lock
    def checked_read() -> bytes:
        held = [lock for lock in locks if lock.acquired]
        assert len(held) == 1, 'approval store read outside the update lock'
        held[0].assert_held()
        reads.append(held[0])
        return original_read()
    monkeypatch.setattr(shell_approvals, '_ApprovalLock', tracked_lock)
    monkeypatch.setattr(shell_approvals, '_approval_bytes', checked_read)
    assert cli.main(['shell', 'approve', str(env), '--yes']) == 0
    assert cli.main(['shell', 'revoke', str(env)]) == 0
    assert len(reads) == 2
    assert _store().read_bytes() == b''
