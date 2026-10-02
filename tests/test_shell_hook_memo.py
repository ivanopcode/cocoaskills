from __future__ import annotations

import os
import shlex
import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

import pytest

from csk import cli, env_files
from test_shell_approvals import _run_hook, _shell_tool, _store


@dataclass(frozen=True)
class MemoIdentityCase:
    id: str
    op: str
    expect_project_active: bool
    expect_notice: bool
    notice: str
    evil_cold: bool
    posix_only: bool


def _memo_identity_cases() -> list[MemoIdentityCase]:
    """GENERATED class inventory for S3 hook-warm-path-latency.

    Invariant: the warm hook path meets the decided prompt budget while
    changed identities invalidate authorization. One entry per
    identity-change family the memo key must detect (plus no-change
    controls), crossed with both shells and both env scopes in the
    property test below. Each fragment runs inside ONE shell session
    between two _csk_auto_env calls, so a stale memo is what fails.
    """
    return [
        MemoIdentityCase(
            'rename-preserve-mtime-same-size',
            'cp -p "$ENVFILE" "$WORK/env.orig"; '
            'mv "$EVILFILE" "$ENVFILE"; '
            'touch -r "$WORK/env.orig" "$ENVFILE"',
            False, True, 'not approved', True, False,
        ),
        MemoIdentityCase(
            'rename-new-mtime',
            'mv "$EVILFILE" "$ENVFILE"',
            False, True, 'not approved', True, False,
        ),
        MemoIdentityCase(
            'edit-in-place',
            'printf "# appended\\n" >> "$ENVFILE"',
            False, True, 'not approved', False, False,
        ),
        MemoIdentityCase(
            'touch-bump-same-content',
            'touch "$ENVFILE"',
            True, False, '', False, False,
        ),
        MemoIdentityCase(
            'touch-preserve-noop',
            'touch -r "$ENVFILE" "$ENVFILE"',
            True, False, '', False, False,
        ),
        MemoIdentityCase(
            'chmod-666-store',
            'chmod 666 "$STOREFILE"',
            False, True, 'untrusted approval store', False, True,
        ),
        MemoIdentityCase(
            'store-swap-preserve-mtime',
            'cp -p "$STOREFILE" "$WORK/store.orig"; '
            'cp "$SWAPSTOREFILE" "$WORK/store.new"; '
            'chmod 600 "$WORK/store.new"; '
            'mv "$WORK/store.new" "$STOREFILE"; '
            'touch -r "$WORK/store.orig" "$STOREFILE"',
            False, True, 'not approved', False, False,
        ),
        MemoIdentityCase(
            'store-truncate',
            ': > "$STOREFILE"',
            False, True, 'not approved', False, False,
        ),
        MemoIdentityCase(
            'dir-replace',
            'mv "$STOREDIR" "$WORK/shell.orig"; mkdir -m 700 "$STOREDIR"',
            False, True, 'not approved', False, False,
        ),
        MemoIdentityCase('no-op', ':', True, False, '', False, False),
    ]


@pytest.mark.parametrize('shell', ['bash', 'zsh'])
@pytest.mark.parametrize('scope', ['project', 'global'])
@pytest.mark.parametrize('case', _memo_identity_cases(), ids=[c.id for c in _memo_identity_cases()])
def test_hook_memo_identity_property(
    tmp_path: Path, shell: str, scope: str, case: MemoIdentityCase,
) -> None:
    if shutil.which(shell) is None:
        pytest.skip(f'{shell} unavailable')
    if os.name == 'nt' and case.posix_only:
        pytest.skip('POSIX mode contract')
    digest = next((t for t in ('shasum', 'sha256sum', 'openssl') if _shell_tool(shell, t)), None)
    if digest is None:
        pytest.skip('no digest tool')
    work = tmp_path / 'work'
    work.mkdir()
    marker = tmp_path / 'sourced'
    evil_marker = tmp_path / 'evil-sourced'
    good = f'touch {shlex.quote(marker.as_posix())}\n# good\n'.encode()
    evil_base = f'touch {shlex.quote(evil_marker.as_posix())}\n'.encode()
    assert len(evil_base) < len(good)
    evil = evil_base + b'#' * (len(good) - len(evil_base) - 1) + b'\n'
    assert len(evil) == len(good)
    evil_file = work / 'evil.sh'
    evil_file.write_bytes(evil)

    extra_env: dict[str, str] = {}
    if scope == 'project':
        project = tmp_path / 'project'
        (project / '.agents').mkdir(parents=True)
        env = project / '.agents/env.sh'
        env.write_bytes(good)
        active_var = 'CSK_ACTIVE_ENV'
    else:
        project = tmp_path / 'empty'
        project.mkdir()
        home = tmp_path / 'manager'
        (home / 'global').mkdir(parents=True)
        env = home / 'global/env.sh'
        env.write_bytes(good)
        extra_env['CSK_CONFIG'] = str(home / 'config.json')
        active_var = 'CSK_ACTIVE_GLOBAL_ENV'
    assert cli.main(['shell', 'approve', str(env), '--yes']) == 0
    store = _store()
    swap = work / 'swapstore'
    swap.write_bytes(b'#' * (len(store.read_bytes()) - 1) + b'\n')

    script = (
        '_csk_auto_env; '
        f'printf \'before=%s\\n\' "${{{active_var}-unset}}"; '
        f'{case.op}; '
        '_csk_auto_env; '
        f'printf \'after=%s\\n\' "${{{active_var}-unset}}"'
    )
    result = _run_hook(
        tmp_path, shell, project, script,
        ENVFILE=env.as_posix(), EVILFILE=evil_file.as_posix(),
        EVILMARKER=evil_marker.as_posix(), STOREFILE=store.as_posix(),
        STOREDIR=store.parent.as_posix(), WORK=work.as_posix(),
        SWAPSTOREFILE=swap.as_posix(), **extra_env,
    )
    assert result.returncode == 0, result.stderr
    assert marker.exists(), result.stderr
    lines = dict(
        line.split('=', 1)
        for line in result.stdout.splitlines()
        if line.startswith('before=') or line.startswith('after=')
    )
    assert lines['before'] == str(env.resolve()), result.stdout
    if case.evil_cold:
        assert not evil_marker.exists(), result.stderr
    if scope == 'project':
        if case.expect_project_active:
            assert lines['after'] == str(env.resolve()), result.stdout
        else:
            assert lines['after'] == 'unset', result.stdout
    else:
        # Global envs stay sourced once approved (pre-existing rev5
        # semantics, unchanged by the memo); the notice proves the slow
        # check ran instead of a stale hit.
        assert lines['after'] == str(env.resolve()), result.stdout
    if case.expect_notice:
        assert result.stderr.count(case.notice) == 1, result.stderr
    else:
        assert result.stderr == '', result.stderr


@pytest.mark.parametrize('shell', ['bash', 'zsh'])
def test_hook_warm_path_budget(tmp_path: Path, shell: str) -> None:
    perl = _shell_tool(shell, 'perl')
    if perl is None:
        pytest.skip('perl unavailable for timing')
    digest = next((t for t in ('shasum', 'sha256sum', 'openssl') if _shell_tool(shell, t)), None)
    if digest is None:
        pytest.skip('no digest tool')
    project = tmp_path / 'project'
    env_files.write_env_files(project)
    home = tmp_path / 'manager'
    env_files.write_global_env_files(home)
    config = str(home / 'config.json')

    timing = (
        '_csk_auto_env; '
        'printf \'t0=%s\\n\' "$("$PERL" -MTime::HiRes=time -e \'print time\')"; '
        'i=0; while [ $i -lt 40 ]; do _csk_auto_env; i=$((i+1)); done; '
        'printf \'t1=%s\\n\' "$("$PERL" -MTime::HiRes=time -e \'print time\')"'
    )
    measured = _run_hook(tmp_path, shell, project, timing, PERL=perl.as_posix(), CSK_CONFIG=config)
    assert measured.returncode == 0, measured.stderr
    stamps = {}
    for line in measured.stdout.splitlines():
        if line.startswith('t0=') or line.startswith('t1='):
            key, _, value = line.partition('=')
            stamps[key] = float(value)
    mean_ms = (stamps['t1'] - stamps['t0']) * 1000 / 40
    assert mean_ms < 50.0, f'warm mean {mean_ms:.1f}ms exceeds the 10ms budget with CI margin'

    tools = tmp_path / 'counttools'
    tools.mkdir()
    count = tmp_path / 'execs.log'
    count.write_text('')
    for name in ('ls', 'id', 'tr', 'cmp', 'readlink', 'shasum', 'sha256sum', 'openssl', 'perl', 'cygpath'):
        source = _shell_tool(shell, name)
        if source is None:
            continue
        wrapper = tools / name
        wrapper.write_text(
            '#!/bin/sh\nprintf "%s\\n" "' + name + '" >> ' + shlex.quote(count.as_posix())
            + '\nexec ' + shlex.quote(source.as_posix()) + ' "$@"\n'
        )
        wrapper.chmod(0o755)
    counting = '_csk_auto_env; : > "$COUNT"; i=0; while [ $i -lt 20 ]; do _csk_auto_env; i=$((i+1)); done'
    counted = _run_hook(tmp_path, shell, project, counting, PATH=str(tools), COUNT=count.as_posix(), CSK_CONFIG=config)
    assert counted.returncode == 0, counted.stderr
    execs = count.read_text().split()
    assert execs == ['ls'] * 20, execs


@pytest.mark.parametrize('mutant', ['memo-key-without-inode', 'memo-key-without-store'])
def test_memo_narrowing_mutants_are_killed(tmp_path: Path, mutant: str) -> None:
    source = Path(__file__).resolve().parents[1]
    checkout = tmp_path / 'mutant'
    shutil.copytree(source / 'src', checkout / 'src')
    shutil.copytree(source / 'tests', checkout / 'tests', ignore=shutil.ignore_patterns('__pycache__'))
    shutil.copy2(source / 'pyproject.toml', checkout / 'pyproject.toml')
    hook = checkout / 'src/csk/shell_init.py'
    hook_text = hook.read_text()
    test_file = 'tests/test_shell_hook_memo.py'
    test = 'test_hook_memo_identity_property'
    if mutant == 'memo-key-without-inode':
        old = '  LC_ALL=C ls -ldin -- "$@" 2>/dev/null'
        assert hook_text.count(old) == 1
        hook.write_text(hook_text.replace(old, '  LC_ALL=C ls -ldn -- "$@" 2>/dev/null'))
        selection = test + ' and rename-preserve-mtime-same-size and project'
    else:
        old = '"$HOME/.cocoaskills/shell/approved" "$HOME/.cocoaskills/shell"'
        assert hook_text.count(old) == 2
        hook.write_text(hook_text.replace(old, '"" ""'))
        selection = test + ' and store-swap-preserve-mtime and project'
    result = subprocess.run(
        [sys.executable, '-m', 'pytest', '-q', test_file, '-k', selection,
         '--basetemp=' + str(tmp_path / 'mutant-tmp')],
        cwd=checkout, env={**os.environ, 'PYTHONPATH': str(checkout / 'src')},
        capture_output=True, text=True, timeout=300,
    )
    (tmp_path / (mutant + '.log')).write_text(result.stdout + result.stderr)
    assert result.returncode == 1, result.stdout + result.stderr
    assert 'FAILED ' + test_file + '::' + test in result.stdout
