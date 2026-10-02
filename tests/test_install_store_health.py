from __future__ import annotations

import os
import shlex
import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

import pytest

from csk import cli
from test_shell_approvals import _line, _run_hook, _shell_tool, _store


@dataclass(frozen=True)
class StoreHealthState:
    id: str
    setup: str
    expect_recorded: bool
    causes: tuple[str, ...]


def _store_health_states() -> list[StoreHealthState]:
    """GENERATED class inventory for S3 approval-store-health-blocks-install.

    Invariant: install status is independent of approval-store health;
    metadata labels are classified correctly; warnings name the cause.
    One entry per store-health family the trust check classifies, crossed
    with every env-writing lane in the property test below.
    """
    return [
        StoreHealthState('healthy-absent', 'healthy-absent', True, ()),
        StoreHealthState('healthy-present', 'healthy-present', True, ()),
        StoreHealthState('selinux-dot', 'selinux-dot', True, ()),
        StoreHealthState('mode-666-file', 'mode-666-file', False, ('0666', 'chmod 600')),
        StoreHealthState('mode-660-file', 'mode-660-file', False, ('0660', 'chmod 600')),
        StoreHealthState('mode-777-dir', 'mode-777-dir', False, ('0777', 'chmod 700')),
        StoreHealthState('symlink-file', 'symlink-file', False, ('is a symlink',)),
        StoreHealthState('symlink-dir', 'symlink-dir', False, ('is a symlink',)),
        StoreHealthState('control-bytes', 'control-bytes', False, ('control bytes',)),
        StoreHealthState('unreadable-000', 'unreadable-000', False, ('cannot write shell approvals',)),
        StoreHealthState('acl-plus-suffix', 'acl-plus-suffix', False, ('ACL entries',)),
        StoreHealthState('unknown-suffix', 'unknown-suffix', False, ('unrecognized',)),
        StoreHealthState('real-acl-file', 'real-acl-file', False, ('ACL entries',)),
    ]


def _prepare_state(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, state: StoreHealthState,
) -> bytes | None:
    """Arrange the store health state; return pre-lane store bytes (None if absent)."""
    from csk import shell_approvals

    store = _store()
    if state.setup == 'healthy-absent':
        return None
    (tmp_path / 'other.sh').write_text('# unrelated approval\n')
    if state.setup in {'selinux-dot', 'acl-plus-suffix', 'unknown-suffix'}:
        store.parent.mkdir(parents=True, mode=0o700)
        store.write_bytes(b'')
        store.chmod(0o600)
        before = store.read_bytes()
        suffix = {'selinux-dot': '.', 'acl-plus-suffix': '+', 'unknown-suffix': '?'}[state.setup]
        targets = {str(store.absolute()), str(store.parent.absolute())}
        real_run = subprocess.run

        def metadata(args: list[str], **kwargs: object) -> subprocess.CompletedProcess[bytes]:
            result = real_run(args, **kwargs)
            if args[:2] == ['/bin/ls', '-ldn'] and args[-1] in targets:
                fields = result.stdout.split(maxsplit=1)
                result.stdout = fields[0][:10] + suffix.encode() + b' ' + fields[1]
            return result

        monkeypatch.setattr(shell_approvals.subprocess, 'run', metadata)
        return before
    if state.setup == 'healthy-present':
        store.parent.mkdir(parents=True, mode=0o700)
        store.write_bytes((_line(tmp_path / 'other.sh') + '\n').encode())
        store.chmod(0o600)
        return store.read_bytes()
    if state.setup == 'real-acl-file':
        from test_shell_approval_bytes_acl import _acl

        store.parent.mkdir(parents=True, mode=0o700)
        store.write_bytes(b'')
        store.chmod(0o600)
        _acl(store, xattrs=False)
        return store.read_bytes()
    store.parent.mkdir(parents=True, mode=0o700)
    store.write_bytes((_line(tmp_path / 'other.sh') + '\n').encode())
    store.chmod(0o600)
    before = store.read_bytes()
    if state.setup == 'mode-666-file':
        store.chmod(0o666)
    elif state.setup == 'mode-660-file':
        store.chmod(0o660)
    elif state.setup == 'mode-777-dir':
        store.parent.chmod(0o777)
    elif state.setup == 'symlink-file':
        target = tmp_path / 'real-store'
        store.rename(target)
        store.symlink_to(target)
        return target.read_bytes()
    elif state.setup == 'symlink-dir':
        target = tmp_path / 'real-dir'
        store.parent.rename(target)
        store.parent.symlink_to(target, target_is_directory=True)
        return (target / 'approved').read_bytes()
    elif state.setup == 'control-bytes':
        store.write_bytes(before + b'legacy\x07\n')
        before = store.read_bytes()
    elif state.setup == 'unreadable-000':
        store.chmod(0o000)
        if os.access(store, os.R_OK):
            pytest.skip('readable 000 store (superuser)')
    else:
        raise AssertionError(state.setup)
    return before


def _configure(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, csk_home: Path, skills_root: Path) -> Path:
    from conftest import make_config, make_project

    from csk import config

    project = make_project(tmp_path)
    cfg = make_config(csk_home, skills_root, project, agents=['codex_cli'])
    config.save_config(cfg)
    monkeypatch.setenv('CSK_CONFIG', str(cfg.path))
    return project


@pytest.mark.parametrize('state', _store_health_states(), ids=[s.id for s in _store_health_states()])
@pytest.mark.parametrize('lane', ['project-install', 'global-install', 'global-init'])
def test_install_store_health_independence_property(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, csk_home: Path, skills_root: Path,
    capsys: pytest.CaptureFixture[str], state: StoreHealthState, lane: str,
) -> None:
    if os.name == 'nt' and state.setup in {
        'mode-666-file', 'mode-660-file', 'mode-777-dir', 'symlink-file',
        'symlink-dir', 'unreadable-000', 'acl-plus-suffix', 'unknown-suffix', 'selinux-dot',
    }:
        pytest.skip('POSIX store-health contract')
    project = _configure(tmp_path, monkeypatch, csk_home, skills_root)
    before = _prepare_state(tmp_path, monkeypatch, state)
    if lane == 'project-install':
        from conftest import write_skillfile

        target = project / '.agents/env.sh'
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text('# hostile pre-existing env\ntouch /tmp/PWNED\n')
        write_skillfile(project, {'schema_version': 1, 'project': {'alias': 'app'}, 'agents': ['codex_cli'], 'skills': []})
        assert cli.main(['install', 'app']) == 0
        directory = project / '.agents'
    elif lane == 'global-install':
        assert cli.main(['global', 'init']) == 0
        target = csk_home / 'global/env.sh'
        target.write_text('# hostile pre-existing env\ntouch /tmp/PWNED\n')
        assert cli.main(['global', 'install']) == 0
        directory = csk_home / 'global'
    else:
        assert cli.main(['global', 'init']) == 0
        directory = csk_home / 'global'
        target = directory / 'env.sh'
        assert 'PWNED' not in target.read_text()
    err = capsys.readouterr().err
    written = target.read_bytes()
    assert b'Generated by CocoaSkill' in written
    assert b'PWNED' not in written
    if state.expect_recorded:
        assert 'approval not recorded' not in err
        lines = _store().read_text().splitlines()
        for name in ('env.sh', 'env.ps1'):
            assert _line(directory / name) in lines
        if state.setup == 'healthy-present':
            assert _line(tmp_path / 'other.sh') in lines
    else:
        assert err.count('approval not recorded:') >= 1, err
        for cause in state.causes:
            assert cause in err, err
        store = _store()
        if state.setup == 'symlink-file':
            assert store.is_symlink()
            assert (tmp_path / 'real-store').read_bytes() == before
        elif state.setup == 'symlink-dir':
            assert store.parent.is_symlink()
            assert (tmp_path / 'real-dir/approved').read_bytes() == before
        else:
            if state.setup == 'unreadable-000':
                store.chmod(0o600)
            assert store.read_bytes() == before


@pytest.mark.parametrize('shell', ['bash', 'zsh'])
@pytest.mark.parametrize(
    'suffix, trusted', [('.', True), ('+', False)], ids=['selinux-dot', 'acl-plus'],
)
def test_hook_metadata_suffix_shim(tmp_path: Path, shell: str, suffix: str, trusted: bool) -> None:
    from csk import env_files

    if _shell_tool(shell, 'ls') is None:
        pytest.skip('ls undiscoverable')
    digest = next((t for t in ('shasum', 'sha256sum', 'openssl') if _shell_tool(shell, t)), None)
    if digest is None:
        pytest.skip('no digest tool')
    sed_tool = _shell_tool(shell, 'sed')
    if sed_tool is None:
        pytest.skip('sed undiscoverable')
    sed = sed_tool.as_posix()
    project = tmp_path / 'project'
    env_files.write_env_files(project)
    tools = tmp_path / 'shimtools'
    tools.mkdir()
    real_ls = _shell_tool(shell, 'ls')
    assert real_ls
    shim = tools / 'ls'
    shim.write_bytes(
        ('#!/bin/sh\n' + shlex.quote(real_ls.as_posix()) + ' "$@" | ' + shlex.quote(sed)
         + " -E 's/^([-d][-rwxsStT-]{9})[@+]?/\\1" + suffix + "/'\n").encode()
    )
    shim.chmod(0o755)
    names = ['id', 'tr', 'cmp', digest, 'readlink', 'perl', 'cygpath']
    for name in names:
        source = _shell_tool(shell, name)
        if source is None:
            continue
        wrapper = tools / name
        wrapper.write_bytes(('#!/bin/sh\nexec ' + shlex.quote(source.as_posix()) + ' "$@"\n').encode())
        wrapper.chmod(0o755)
    result = _run_hook(tmp_path, shell, project, '_csk_auto_env', PATH=str(tools))
    assert result.returncode == 0, result.stderr
    if trusted:
        assert result.stdout == f'active={(project / ".agents/env.sh").resolve()}\n', result.stderr
        assert result.stderr == ''
    else:
        assert result.stdout == 'active=unset\n'
        assert 'untrusted approval store' in result.stderr
        assert 'ACL entries' in result.stderr


@pytest.mark.parametrize('mutant', ['record-refusal-recouples-install'])
def test_store_health_narrowing_mutants_are_killed(tmp_path: Path, mutant: str) -> None:
    source = Path(__file__).resolve().parents[1]
    checkout = tmp_path / 'mutant'
    shutil.copytree(source / 'src', checkout / 'src', ignore=shutil.ignore_patterns('__pycache__'))
    (checkout / 'tests').mkdir()
    for name in ('conftest.py', 'draft_sources_accounting.py', 'test_adapters.py',
                 'test_build_cache_windows.py',
                 'test_shell_approvals.py', 'test_shell_approval_trust.py',
                 'test_shell_approval_bytes_acl.py', 'test_shell_approval_display.py',
                 'test_shell_hook_upgrade.py', 'test_install_store_health.py',
                 'test_shell_hook_memo.py'):
        shutil.copy2(source / 'tests' / name, checkout / 'tests' / name)
    shutil.copy2(source / 'pyproject.toml', checkout / 'pyproject.toml')
    module = checkout / 'src/csk/env_files.py'
    text = module.read_text()
    old = '    except (shell_approvals.ShellApprovalError, OSError, UnicodeError) as exc:'
    assert text.count(old) == 1
    module.write_text(text.replace(old, '    except OSError as exc:'))
    test_file = 'tests/test_install_store_health.py'
    test = 'test_install_store_health_independence_property'
    # control-bytes runs on every platform (mode states are POSIX-only); on
    # Windows the single fastest lane keeps the nested run small.
    selection = test + ' and (mode-666-file or control-bytes)'
    if os.name == 'nt':
        selection += ' and global-init'
    result = subprocess.run(
        [sys.executable, '-m', 'pytest', '-q', test_file, '-k', selection,
         '--basetemp=' + str(tmp_path / 'mutant-tmp')],
        cwd=checkout, env={**os.environ, 'PYTHONPATH': str(checkout / 'src')},
        capture_output=True, text=True, timeout=300,
    )
    (tmp_path / (mutant + '.log')).write_text(result.stdout + result.stderr)
    assert result.returncode == 1, result.stdout + result.stderr
    assert 'FAILED ' + test_file + '::' + test in result.stdout
