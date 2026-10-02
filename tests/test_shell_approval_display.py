from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from test_shell_approvals import _run_hook, _store


def _approve_bytes(args: list[str], cwd: Path) -> subprocess.CompletedProcess[bytes]:
    root = Path(__file__).resolve().parents[1] / 'src'
    return subprocess.run(
        [sys.executable, '-m', 'csk', *args], cwd=cwd,
        env={**os.environ, 'PYTHONPATH': str(root)}, capture_output=True, timeout=60,
    )


def _render_terminal_lines(data: bytes) -> list[str]:
    """Minimal VT model: LF advances, CR returns to column 0, glyphs overwrite."""
    rows: list[list[str]] = [[]]
    row, col = 0, 0
    for char in data.decode('utf-8'):
        if char == '\n':
            rows.append([])
            row += 1
            col = 0
        elif char == '\r':
            col = 0
        else:
            line = rows[row]
            while len(line) <= col:
                line.append(' ')
            line[col] = char
            col += 1
    return [''.join(cells).rstrip() for cells in rows]


def test_approve_escapes_hidden_payload_line(tmp_path: Path) -> None:
    project = tmp_path / 'proj'
    (project / '.agents').mkdir(parents=True)
    marker = tmp_path / 'PWNED'
    evil = f'touch "{marker}"' + '; : hidden'
    benign = '# set up local tooling paths' + ' ' * 40
    env = project / '.agents/env.sh'
    env.write_bytes(f'{evil}\r{benign}\n'.encode())
    completed = _approve_bytes(['shell', 'approve', '--yes'], project)
    assert completed.returncode == 0, completed.stderr
    out = completed.stdout
    assert b'\r' not in out
    assert b'shown escaped' in out
    visible = '\n'.join(_render_terminal_lines(out))
    assert 'PWNED' in visible
    assert marker.exists() is False
    assert _store().exists()


def test_approve_escapes_terminal_escapes(tmp_path: Path) -> None:
    project = tmp_path / 'proj'
    (project / '.agents').mkdir(parents=True)
    marker = tmp_path / 'PWNED_A'
    env = project / '.agents/env.sh'
    env.write_bytes(
        b'echo harmless\ntouch "' + str(marker).encode() + b'"\n\x1b[1A\x1b[2K\r# nothing to see\n'
    )
    completed = _approve_bytes(['shell', 'approve', '--yes'], project)
    assert completed.returncode == 0, completed.stderr
    out = completed.stdout
    assert b'\x1b' not in out
    assert b'\r' not in out
    assert b'shown escaped' in out
    assert b'\\x1b[1A\\x1b[2K\\x0d' in out


def test_approve_escapes_del_and_c1(tmp_path: Path) -> None:
    project = tmp_path / 'proj'
    (project / '.agents').mkdir(parents=True)
    env = project / '.agents/env.sh'
    env.write_bytes('payload\x7f here\u0085 done\n'.encode('utf-8'))
    completed = _approve_bytes(['shell', 'approve', '--yes'], project)
    assert completed.returncode == 0, completed.stderr
    out = completed.stdout
    assert b'\x7f' not in out
    assert '\u0085'.encode('utf-8') not in out
    assert b'\\x7f' in out
    assert b'\\u0085' in out
    assert b'shown escaped' in out


def test_approve_leaves_clean_content_byte_identical(tmp_path: Path) -> None:
    project = tmp_path / 'proj'
    (project / '.agents').mkdir(parents=True)
    env = project / '.agents/env.sh'
    content = '# reviewed\n\techo "caf\u00e9 \u0421\u043a\u0438\u043b\u043b"\n'
    env.write_text(content, encoding='utf-8')
    completed = _approve_bytes(['shell', 'approve', '--yes'], project)
    assert completed.returncode == 0, completed.stderr
    assert content.encode('utf-8') in completed.stdout
    assert b'shown escaped' not in completed.stdout


@pytest.mark.parametrize('shell', ['bash', 'zsh'])
def test_hook_notice_remedy_is_single_quoted_exact(tmp_path: Path, shell: str) -> None:
    project = tmp_path / 'project'
    (project / '.agents').mkdir(parents=True)
    env = project / '.agents/env.sh'
    env.write_text('echo hi\n')
    result = _run_hook(tmp_path, shell, project)
    assert result.returncode == 0, result.stderr
    match = re.search(r'then run: (csk shell approve .*)\)\n', result.stderr)
    assert match is not None, result.stderr
    assert match.group(1) == f"csk shell approve '{env.resolve()}'"


METACHAR_DIRS = [
    ('sub;touch PWNED2;#', 'semicolon'),
    ('sub;>PWNED2;#', 'semicolon-nospace'),
    ('a&&touch PWNED2', 'andand'),
    ('a|touch PWNED2', 'pipe'),
    ('$(touch PWNED2)', 'dollarsub'),
    ('`touch PWNED2`', 'backtick'),
    ("a'b", 'squote'),
    ('a"b', 'dquote'),
    ('a b', 'space'),
    ('a*b', 'glob'),
    ('$HOME', 'dollarhome'),
    ('a\nb', 'newline'),
    ('a\x1bb', 'esc'),
]


@pytest.mark.parametrize('shell', ['bash', 'zsh'])
@pytest.mark.parametrize('dirname, case', [pytest.param(d, c, id=c) for d, c in METACHAR_DIRS])
def test_hook_notice_remedy_pastes_safely(tmp_path: Path, shell: str, dirname: str, case: str) -> None:
    if os.name == 'nt' and case in {'pipe', 'glob', 'dquote', 'semicolon-nospace', 'newline', 'esc'}:
        pytest.skip('Win32 reserves this character in file names')
    executable = shutil.which(shell)
    if executable is None:
        pytest.skip(f'{shell} unavailable')
    project = tmp_path / 'proj'
    evil_dir = project / dirname
    (evil_dir / '.agents').mkdir(parents=True)
    env = evil_dir / '.agents/env.sh'
    env.write_text('echo hi\n')
    marker = tmp_path / 'PWNED2'
    result = _run_hook(tmp_path, shell, evil_dir)
    assert result.returncode == 0, result.stderr
    assert not marker.exists()
    assert result.stderr.count('\n') == 1, result.stderr
    assert '\x1b' not in result.stderr
    match = re.search(r'then run: (csk shell approve .*)\)\n', result.stderr)
    assert match is not None, result.stderr
    suggested = match.group(1)
    assert suggested.startswith('csk shell approve ')
    stub_dir = tmp_path / 'bin'
    stub_dir.mkdir()
    captured = tmp_path / 'argv.txt'
    stub = stub_dir / 'csk'
    stub.write_text(f"#!/bin/sh\nprintf '%s\\n' \"$@\" > {captured}\n")
    stub.chmod(0o755)
    pasted = subprocess.run(
        [executable, '-c', suggested], cwd=tmp_path,
        env={**os.environ, 'PATH': f'{stub_dir}:{os.environ["PATH"]}'},
        capture_output=True, timeout=30,
    )
    assert pasted.returncode == 0, pasted.stderr
    assert not marker.exists(), f'pasting the printed remedy ran attacker text: {suggested!r}'
    argv = captured.read_text().splitlines()
    assert argv[:2] == ['shell', 'approve']
    assert len(argv) == 3
    if case in {'newline', 'esc'}:
        assert argv[2] == str(env.resolve()).replace('\n', '?').replace('\x1b', '?')
    else:
        assert argv[2] == str(env.resolve())


@pytest.mark.parametrize('shell', ['bash', 'zsh'])
def test_display_and_quote_helper_through_shell(tmp_path: Path, shell: str) -> None:
    if shutil.which(shell) is None:
        pytest.skip(f'{shell} unavailable')
    project = tmp_path / 'project'
    project.mkdir()
    script = (
        '_csk_display_and_quote "a;b"; printf \'d1=[%s] r1=[%s]\\n\' "$_csk_display" "$_csk_remedy"; '
        '_csk_display_and_quote "a\'b"; printf \'d2=[%s] r2=[%s]\\n\' "$_csk_display" "$_csk_remedy"; '
        '_csk_display_and_quote ""; printf \'d3=[%s] r3=[%s]\\n\' "$_csk_display" "$_csk_remedy"; '
        '_csk_display_and_quote "$(printf \'a\\nb\\033c\\td\')"; printf \'d4=[%s] r4=[%s]\\n\' "$_csk_display" "$_csk_remedy"'
    )
    result = _run_hook(tmp_path, shell, project, script)
    assert result.returncode == 0, result.stderr
    assert 'd1=[a;b] r1=[\'a;b\']' in result.stdout
    assert 'd2=[a\'b] r2=[\'a\'\\\'\'b\']' in result.stdout
    assert 'd3=[] r3=[\'\']' in result.stdout
    assert 'd4=[a?b?c?d] r4=[\'a?b?c?d\']' in result.stdout


@pytest.mark.parametrize('mutant', ['escape-cr-only', 'quote-spaces-only'])
def test_display_narrowing_mutants_are_killed(tmp_path: Path, mutant: str) -> None:
    source = Path(__file__).resolve().parents[1]
    checkout = tmp_path / 'mutant'
    shutil.copytree(source / 'src', checkout / 'src')
    shutil.copytree(source / 'tests', checkout / 'tests', ignore=shutil.ignore_patterns('__pycache__'))
    shutil.copy2(source / 'pyproject.toml', checkout / 'pyproject.toml')
    test_file = 'tests/test_shell_approval_display.py'
    if mutant == 'escape-cr-only':
        module = checkout / 'src/csk/shell_approvals.py'
        text = module.read_text()
        old = '_ESCAPED_CONTROL_CODES = frozenset([code for code in range(0x20) if code not in (0x09, 0x0A)] + [0x7F])'
        assert text.count(old) == 1
        module.write_text(text.replace(old, '_ESCAPED_CONTROL_CODES = frozenset([0x0D])'))
        test = 'test_approve_escapes_terminal_escapes'
        selection = test
    else:
        hook = checkout / 'src/csk/shell_init.py'
        hook_text = hook.read_text()
        old_init = '  _csk_remedy="\'"'
        new_init = '  _csk_remedy=""; case "$1" in *" "*) _csk_remedy="\'" ;; esac'
        assert hook_text.count(old_init) == 1
        hook_text = hook_text.replace(old_init, new_init)
        old_close = '  _csk_remedy="${_csk_remedy}\'"'
        new_close = '  case "$1" in *" "*) _csk_remedy="${_csk_remedy}\'" ;; esac'
        assert hook_text.count(old_close) == 1
        hook.write_text(hook_text.replace(old_close, new_close))
        test = 'test_hook_notice_remedy_pastes_safely'
        selection = test + ' and (semicolon-nospace or squote)'
    result = subprocess.run(
        [sys.executable, '-m', 'pytest', '-q', test_file, '-k', selection,
         '--basetemp=' + str(tmp_path / 'mutant-tmp')],
        cwd=checkout, env={**os.environ, 'PYTHONPATH': str(checkout / 'src')},
        capture_output=True, text=True, timeout=180,
    )
    (tmp_path / (mutant + '.log')).write_text(result.stdout + result.stderr)
    assert result.returncode == 1, result.stdout + result.stderr
    assert 'FAILED ' + test_file + '::' + test in result.stdout
