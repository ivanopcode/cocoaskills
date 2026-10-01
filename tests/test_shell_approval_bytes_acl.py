from __future__ import annotations

import os
import shlex
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from csk import cli
from test_shell_approvals import _line, _run_hook, _store


def _fixture(tmp_path: Path) -> tuple[Path, Path, Path, Path, bytes]:
    project = tmp_path / 'project'
    (project / '.agents').mkdir(parents=True)
    marker = tmp_path / 'executed'
    env = project / '.agents/env.sh'
    env.write_text('printf hostile > ' + shlex.quote(marker.as_posix()) + '\n')
    store = _store()
    store.parent.mkdir(parents=True, mode=0o700)
    line = _line(env).encode()
    store.write_bytes(line + b'\n')
    store.chmod(0o600)
    return project, env, marker, store, line


@pytest.mark.parametrize('shell', ['bash', 'zsh'])
@pytest.mark.parametrize('shape', ['nul-prefix', 'nul-digest', 'nul-path', 'crlf', 'utf8bom', 'bad-digest', 'no-newline'])
def test_record_exact_bytes(tmp_path: Path, shell: str, shape: str) -> None:
    project, env, marker, store, line = _fixture(tmp_path)
    data = {'nul-prefix': b'\0' + line + b'\n', 'nul-digest': line[:20] + b'\0' + line[20:] + b'\n',
            'nul-path': line[:70] + b'\0' + line[70:] + b'\n', 'crlf': line + b'\r\n',
            'utf8bom': b'\xef\xbb\xbf' + line + b'\n', 'bad-digest': b'f' * 64 + line[64:] + b'\n', 'no-newline': line}[shape]
    store.write_bytes(data)
    result = _run_hook(tmp_path, shell, project)
    assert result.returncode == 0, result.stderr
    assert marker.exists() == (shape == 'no-newline'), result


@pytest.mark.parametrize('control', [value for value in range(32) if value != 10], ids=lambda v: f'C0-{v:02x}')
@pytest.mark.parametrize('shell', ['bash', 'zsh'])
def test_c0_store_untrusted_for_both_readers(tmp_path: Path, shell: str, control: int, capsys: pytest.CaptureFixture[str]) -> None:
    project, env, marker, store, line = _fixture(tmp_path)
    # A valid record elsewhere in the store must not survive the unsafe bytes.
    original = line + b'\n' + b'legacy' + bytes([control]) + b'\n'
    store.write_bytes(original)
    inode = store.stat().st_ino
    for argv in [['shell', 'approvals'], ['shell', 'approve', str(env), '--yes'], ['shell', 'revoke', str(env)]]:
        assert cli.main(argv) == 2
        assert 'shell_approval_store_untrusted' in capsys.readouterr().err
        assert store.read_bytes() == original
        assert store.stat().st_ino == inode
    result = _run_hook(tmp_path, shell, project, '_csk_auto_env; _csk_auto_env')
    assert result.returncode == 0, result.stderr
    assert not marker.exists(), result
    assert result.stderr.count('untrusted approval store') == 1


@pytest.mark.parametrize('control', [value for value in range(32) if value != 10], ids=lambda v: f'C0-{v:02x}')
@pytest.mark.parametrize('shell', ['bash', 'zsh'])
def test_c0_store_never_sources(tmp_path: Path, shell: str, control: int) -> None:
    project, env, marker, store, line = _fixture(tmp_path)
    store.write_bytes(line + b'\nlegacy' + bytes([control]) + b'\n')
    result = _run_hook(tmp_path, shell, project)
    assert result.returncode == 0, result.stderr
    assert not marker.exists(), result
    assert 'untrusted approval store' in result.stderr


def test_nul_path_record_revoke_removes_authorization(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    project, env, marker, store, line = _fixture(tmp_path)
    original = line[:70] + b'\0' + line[70:] + b'\n'
    store.write_bytes(original)
    # Revision 4 explicitly refuses mutation of an untrusted store.
    assert cli.main(['shell', 'revoke', str(env)]) == 2
    assert 'shell_approval_store_untrusted' in capsys.readouterr().err
    assert store.read_bytes() == original
    result = _run_hook(tmp_path, 'bash', project)
    assert result.returncode == 0, result.stderr
    assert not marker.exists(), result


def _acl(target: Path, *, xattrs: bool) -> bytes:
    if sys.platform != 'darwin':
        pytest.skip('real everyone-write ACL setup requires macOS chmod +a')
    subprocess.run(['/usr/bin/xattr', '-w', 'com.cocoaskills.test', 'trust', str(target)] if xattrs else ['/usr/bin/xattr', '-c', str(target)], check=True, capture_output=True)
    result = subprocess.run(['/bin/chmod', '+a', 'everyone allow write', str(target)], capture_output=True)
    if result.returncode:
        pytest.skip(f'macOS filesystem does not support chmod +a: {result.stderr!r}')
    metadata = subprocess.run(['/bin/ls', '-ldne', str(target)], capture_output=True, check=True).stdout
    assert b'ABCDEFAB-CDEF-ABCD-EFAB-CDEF0000000C allow ' in metadata or b'everyone allow ' in metadata
    assert b'allow add_file' in metadata if target.is_dir() else b'allow write' in metadata
    assert target.stat().st_mode & 0o022 == 0
    return metadata


@pytest.mark.parametrize('shell', ['bash', 'zsh'])
@pytest.mark.parametrize('directory', [False, True], ids=['file', 'dir'])
@pytest.mark.parametrize('xattrs', [False, True], ids=['bare-acl', 'xattr-acl'])
def test_acl_world_writable_store(tmp_path: Path, shell: str, directory: bool, xattrs: bool) -> None:
    project, env, marker, store, line = _fixture(tmp_path)
    _acl(store.parent if directory else store, xattrs=xattrs)
    result = _run_hook(tmp_path, shell, project, '_csk_auto_env; _csk_auto_env')
    assert result.returncode == 0, result.stderr
    assert not marker.exists(), result
    assert result.stderr.count('untrusted approval store') == 1


@pytest.mark.parametrize('command', ['approvals', 'approve', 'revoke'])
@pytest.mark.parametrize('directory', [False, True], ids=['file', 'dir'])
@pytest.mark.parametrize('xattrs', [False, True], ids=['bare-acl', 'xattr-acl'])
def test_acl_store_cli_refuses_without_laundering(tmp_path: Path, command: str, directory: bool, xattrs: bool, capsys: pytest.CaptureFixture[str]) -> None:
    project, env, marker, store, line = _fixture(tmp_path)
    target = store.parent if directory else store
    metadata = _acl(target, xattrs=xattrs)
    before = store.read_bytes()
    inode = store.stat().st_ino
    safe = tmp_path / 'safe.sh'
    safe.write_text('# safe\n')
    argv = ['shell', command] + ([str(safe), '--yes'] if command == 'approve' else [str(env)] if command == 'revoke' else [])
    assert cli.main(argv) == 2
    assert 'shell_approval_store_untrusted' in capsys.readouterr().err
    assert store.read_bytes() == before
    assert store.stat().st_ino == inode
    assert subprocess.run(['/bin/ls', '-ldne', str(target)], capture_output=True, check=True).stdout == metadata


@pytest.mark.skipif(os.name == 'nt', reason='POSIX ls metadata contract')
@pytest.mark.parametrize('suffix', ['+', '?', '@+', '@@', '', '@'], ids=['acl', 'unknown', 'mixed', 'double', 'plain', 'xattr'])
@pytest.mark.parametrize('directory', [False, True], ids=['file', 'dir'])
@pytest.mark.parametrize('reader', ['cli', 'hook'])
def test_metadata_suffix_positive_verification(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], suffix: str, directory: bool, reader: str) -> None:
    from csk import shell_approvals
    project, env, marker, store, line = _fixture(tmp_path)
    target = store.parent if directory else store
    real_run = subprocess.run
    def metadata(args: list[str], **kwargs: object) -> subprocess.CompletedProcess[bytes]:
        actual = [*args]
        if actual[:2] == ['/bin/ls', '-ldne']:
            actual[1] = '-ldn'
        result = real_run(actual, **kwargs)
        if args[:2] in [['/bin/ls', '-ldn'], ['/bin/ls', '-ldne']] and args[-1] == str(target):
            fields = result.stdout.split(maxsplit=1)
            result.stdout = fields[0][:10] + suffix.encode() + b' ' + fields[1]
        return result
    monkeypatch.setattr(shell_approvals.subprocess, 'run', metadata)
    trusted = suffix in {'', '@'}
    if reader == 'cli':
        assert cli.main(['shell', 'approvals']) == (0 if trusted else 2)
        if not trusted:
            assert 'shell_approval_store_untrusted' in capsys.readouterr().err
        return
    # Same metadata shape via a real shell command, without changing the gate.
    ls = shutil.which('ls')
    assert ls
    setup = 'ls() { local text mode; if [ "$1" = -ldne ]; then shift; set -- -ldn "$@"; fi; text="$(' + shlex.quote(ls) + ' "$@")" || return; mode="${text%% *}"; '
    setup += 'if [ "${@: -1}" = ' + shlex.quote(target.as_posix()) + ' ]; then text="${mode:0:10}' + suffix + '${text#"$mode"}"; fi; printf "%s\\n" "$text"; }; '
    from csk import shell_init
    hook = tmp_path / 'hook.sh'
    hook.write_text(shell_init.shell_init('bash'))
    result = real_run(['bash', '--noprofile', '--norc', '-c', setup + '. "$HOOK"'], cwd=project, env={**os.environ, 'HOOK': str(hook)}, capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stderr
    assert marker.exists() == trusted, result


@pytest.mark.skipif(os.name == 'nt', reason='POSIX absolute ls contract')
@pytest.mark.parametrize('failure', ['unavailable', 'timeout', 'nonzero', 'empty', 'malformed', 'nonzero-xattr', 'multiline-xattr'])
def test_unverifiable_metadata_never_rewrites(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], failure: str) -> None:
    from csk import shell_approvals
    project, env, marker, store, line = _fixture(tmp_path)
    before = store.read_bytes()
    inode = store.stat().st_ino
    def unknown(args: list[str], **kwargs: object) -> subprocess.CompletedProcess[bytes]:
        assert args[:2] in [['/bin/ls', '-ldn'], ['/bin/ls', '-ldne']]
        assert Path(args[-1]).is_absolute()
        if failure == 'unavailable':
            raise FileNotFoundError('ls unavailable')
        if failure == 'timeout':
            raise subprocess.TimeoutExpired(args, 10)
        if failure in {'nonzero-xattr', 'multiline-xattr'}:
            mode = b'drwx------@' if Path(args[-1]).is_dir() else b'-rw-------@'
            output = mode + b' 1 ' + str(os.geteuid()).encode() + b' 0 0 Jan 1 00:00 target\n'
            if args[1] == '-ldn':
                return subprocess.CompletedProcess(args, 1 if failure == 'nonzero-xattr' else 0, output + (b'unknown extra metadata\n' if failure == 'multiline-xattr' else b''), b'')
            return subprocess.CompletedProcess(args, 0, output, b'')
        return subprocess.CompletedProcess(args, 1 if failure == 'nonzero' else 0, b'' if failure == 'empty' else b'unknown metadata', b'')
    monkeypatch.setattr(shell_approvals.subprocess, 'run', unknown)
    for argv in [['shell', 'approvals'], ['shell', 'approve', str(env), '--yes'], ['shell', 'revoke', str(env)]]:
        assert cli.main(argv) == 2
        assert 'shell_approval_store_untrusted' in capsys.readouterr().err
        assert store.read_bytes() == before
        assert store.stat().st_ino == inode


@pytest.mark.parametrize('shell', ['bash', 'zsh'])
@pytest.mark.parametrize('tool', ['tr', 'cmp'])
@pytest.mark.parametrize('failure', ['missing', 'fails'])
def test_byte_verification_tools_required(tmp_path: Path, shell: str, tool: str, failure: str) -> None:
    from test_shell_approvals import _shell_tool
    project, env, marker, store, line = _fixture(tmp_path)
    tools = tmp_path / 'tools'
    tools.mkdir()
    for name in ['id', 'ls', 'tr', 'cmp', 'shasum', 'sha256sum', 'openssl', 'perl', 'cygpath']:
        if name == tool and failure == 'missing':
            continue
        executable = _shell_tool(shell, name)
        if executable is None:
            continue
        wrapper = tools / name
        wrapper.write_text('#!/bin/sh\n' + ('exit 77\n' if name == tool else 'exec ' + shlex.quote(executable.as_posix()) + ' "$@"\n'))
        wrapper.chmod(0o755)
    result = _run_hook(tmp_path, shell, project, '_csk_auto_env', PATH=str(tools))
    assert result.returncode == 0, result.stderr
    assert not marker.exists(), result
    assert 'untrusted approval store' in result.stderr


@pytest.mark.parametrize('control', [value for value in range(32) if value != 10], ids=lambda v: f'C0-{v:02x}')
@pytest.mark.parametrize('command', ['approve', 'revoke'])
def test_control_path_cannot_create_untrusted_store(tmp_path: Path, control: int, command: str, capsys: pytest.CaptureFixture[str]) -> None:
    path = tmp_path / ('name' + chr(control) + 'end.sh')
    assert cli.main(['shell', command, str(path), *(['--yes'] if command == 'approve' else [])]) == 2
    assert 'shell_approval_path_invalid' in capsys.readouterr().err
    assert not _store().exists()


@pytest.mark.parametrize('mutant', ['allow-nul', 'allow-tab', 'allow-file-acl', 'allow-dir-acl', 'allow-unknown-suffix', 'allow-failed-initial-xattr'])
def test_byte_acl_narrowing_mutants_are_killed(tmp_path: Path, mutant: str) -> None:
    if os.name == 'nt' and mutant in {'allow-file-acl', 'allow-dir-acl', 'allow-unknown-suffix', 'allow-failed-initial-xattr'}:
        pytest.skip('POSIX metadata guard')
    if mutant in {'allow-file-acl', 'allow-dir-acl'} and sys.platform != 'darwin':
        pytest.skip('real macOS chmod +a narrowing proof')
    source = Path(__file__).resolve().parents[1]
    checkout = tmp_path / 'mutant'
    shutil.copytree(source / 'src', checkout / 'src')
    shutil.copytree(source / 'tests', checkout / 'tests', ignore=shutil.ignore_patterns('__pycache__'))
    shutil.copy2(source / 'pyproject.toml', checkout / 'pyproject.toml')
    module = checkout / 'src/csk/shell_approvals.py'
    hook = checkout / 'src/csk/shell_init.py'
    text = module.read_text()
    hook_text = hook.read_text()
    if mutant in {'allow-nul', 'allow-tab'}:
        value = 0 if mutant == 'allow-nul' else 9
        old = 'value < 32 and value != 10'
        assert text.count(old) == 1
        text = text.replace(old, f'value < 32 and value not in (10, {value})')
        old_hook = r"tr -d '\000-\011\013-\037'"
        new_hook = r"tr -d '\001-\011\013-\037'" if value == 0 else r"tr -d '\000-\010\013-\037'"
        assert hook_text.count(old_hook) == 1
        hook_text = hook_text.replace(old_hook, new_hook)
        test = 'test_c0_store_untrusted_for_both_readers'
        selection = '(' + test + ' or test_c0_store_never_sources) and ' + ('C0-00' if value == 0 else 'C0-09')
        if value == 0:
            selection += ' or (test_record_exact_bytes and nul)'
    elif mutant == 'allow-failed-initial-xattr':
        old = "if result.returncode != 0 or result.stdout.count(b'\\n') != 1:"
        assert text.count(old) == 1
        text = text.replace(old, "if (result.returncode != 0 and not result.stdout.split()[0].endswith(b'@')) or result.stdout.count(b'\\n') != 1:")
        test = 'test_unverifiable_metadata_never_rewrites'
        selection = test + ' and nonzero-xattr'
    else:
        old = "prefix + rb'[r-][w-][xsS-][r-]-[xsS-][r-]-[xtT-]@?'"
        assert text.count(old) == 1
        if mutant == 'allow-unknown-suffix':
            text = text.replace(old, "prefix + rb'[r-][w-][xsS-][r-]-[xsS-][r-]-[xtT-][@?]?' ")
            suffix = '?'
            test = 'test_metadata_suffix_positive_verification'
            selection = test + ' and file and unknown'
        else:
            directory = mutant == 'allow-dir-acl'
            text = text.replace(old, "prefix + (rb'[r-][w-][xsS-][r-]-[xsS-][r-]-[xtT-][@+]?' if directory == " + repr(directory) + " else rb'[r-][w-][xsS-][r-]-[xsS-][r-]-[xtT-]@?')")
            old_lines = 'result.stdout.count(b"\\n") == 1'
            assert text.count(old_lines) == 1
            text = text.replace(old_lines, '(directory == ' + repr(directory) + ' or result.stdout.count(b"\\n") == 1)')
            start = hook_text.index('  metadata="$(LC_ALL=C ls -ldn -- "' + ('$directory' if directory else '$store') + '"')
            end = hook_text.index('  [ "$owner" = "$uid" ]', start)
            section = hook_text[start:end]
            section = section.replace("case \"$metadata\" in *'\n'*) return 1 ;; esac", ':')
            hook_text = hook_text[:start] + section + hook_text[end:]
            suffix = '+'
            test = 'test_acl_store_cli_refuses_without_laundering'
            selection = test + (' and dir' if directory else ' and file')
            selection += ' or (test_acl_world_writable_store and ' + ('dir' if directory else 'file') + ')'
        prefix = 'd' if mutant == 'allow-dir-acl' else '-'
        old_hook = prefix + '[r-][w-][xsS-][r-]-[xsS-][r-]-[xtT-]@)'
        new_hook = old_hook[:-1] + '|' + prefix + '[r-][w-][xsS-][r-]-[xsS-][r-]-[xtT-]' + ('[?]' if suffix == '?' else '+') + ')'
        assert hook_text.count(old_hook) == 1
        hook_text = hook_text.replace(old_hook, new_hook)
    module.write_text(text)
    hook.write_text(hook_text)
    result = subprocess.run([sys.executable, '-m', 'pytest', '-q', 'tests/test_shell_approval_bytes_acl.py', '-k', selection, '--basetemp=' + str(tmp_path / 'mutant-tmp')], cwd=checkout, env={**os.environ, 'PYTHONPATH': str(checkout / 'src')}, capture_output=True, text=True, timeout=120)
    (tmp_path / (mutant + '.log')).write_text(result.stdout + result.stderr)
    assert result.returncode == 1, result.stdout + result.stderr
    assert 'FAILED tests/test_shell_approval_bytes_acl.py::' + test in result.stdout
    if mutant == 'allow-unknown-suffix':
        assert result.stdout.count('FAILED tests/test_shell_approval_bytes_acl.py::test_metadata_suffix_positive_verification') == 2
    if mutant in {'allow-nul', 'allow-tab'}:
        assert 'FAILED tests/test_shell_approval_bytes_acl.py::test_c0_store_never_sources' in result.stdout
    # Bash NUL normalization varies by version; the whole-store C0 guard
    # must still be killed through both readers on every supported shell.
    if mutant in {'allow-file-acl', 'allow-dir-acl'}:
        assert 'FAILED tests/test_shell_approval_bytes_acl.py::test_acl_world_writable_store' in result.stdout
