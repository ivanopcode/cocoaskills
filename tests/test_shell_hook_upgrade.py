from __future__ import annotations

import os
import shlex
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from csk import cli, shell_init

STALE_HOOK = """# stale pre-gate cached hook: sources every env file unconditionally
_csk_auto_env() {
  if [ -f "$PWD/.agents/env.sh" ]; then
    . "$PWD/.agents/env.sh"
  fi
}
"""


def _write_skillfile(project: Path) -> None:
    from conftest import write_skillfile

    write_skillfile(
        project,
        {'schema_version': 1, 'project': {'alias': 'app'}, 'agents': ['codex_cli'], 'skills': []},
    )


def _configure(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, csk_home: Path, skills_root: Path) -> Path:
    from conftest import make_config, make_project

    from csk import config

    project = make_project(tmp_path)
    cfg = make_config(csk_home, skills_root, project, agents=['codex_cli'])
    config.save_config(cfg)
    monkeypatch.setenv('CSK_CONFIG', str(cfg.path))
    return project


def _source_through_cached_hook(
    tmp_path: Path, shell: str, hook: Path, project: Path,
) -> subprocess.CompletedProcess[str]:
    executable = shutil.which(shell)
    if executable is None:
        pytest.skip(f'{shell} unavailable')
    args = [executable, '-dfc'] if shell == 'zsh' else [executable, '--noprofile', '--norc', '-c']
    return subprocess.run(
        [*args, '. "$HOOK"; cd "$PROJECT_PATH"; _csk_auto_env'],
        cwd=project,
        env={**os.environ, 'HOOK': hook.as_posix(), 'PROJECT_PATH': project.as_posix(), 'SHELL': executable},
        text=True, capture_output=True, timeout=60,
    )


@pytest.mark.parametrize('shell', ['bash', 'zsh'])
@pytest.mark.parametrize('lane', ['project', 'global'])
def test_install_refreshes_existing_cached_hook(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, csk_home: Path, skills_root: Path,
    shell: str, lane: str,
) -> None:
    if shutil.which(shell) is None:
        pytest.skip(f'{shell} unavailable')
    project = _configure(tmp_path, monkeypatch, csk_home, skills_root)
    hooks_dir = csk_home / 'hooks'
    hooks_dir.mkdir(parents=True)
    cached = hooks_dir / shell_init._HOOK_FILENAMES[shell]
    cached.write_text(STALE_HOOK)
    assert '_csk_env_approved' not in cached.read_text()

    hostile = tmp_path / 'hostile'
    (hostile / '.agents').mkdir(parents=True)
    marker = tmp_path / 'HOSTILE_RAN'
    (hostile / '.agents/env.sh').write_text(f'touch {shlex.quote(marker.as_posix())}\n')
    before = _source_through_cached_hook(tmp_path, shell, cached, hostile)
    assert before.returncode == 0, before.stderr
    assert marker.exists(), 'stale hook must source unconditionally before the install'
    marker.unlink()

    if lane == 'project':
        _write_skillfile(project)
        assert cli.main(['install', 'app']) == 0
    else:
        assert cli.main(['global', 'init']) == 0
        assert cached.read_text() == STALE_HOOK
        assert cli.main(['global', 'install']) == 0
    assert cached.read_text() == shell_init.shell_init(shell)
    assert '_csk_env_approved' in cached.read_text()

    after = _source_through_cached_hook(tmp_path, shell, cached, hostile)
    assert after.returncode == 0, after.stderr
    assert not marker.exists()
    assert 'not approved' in after.stderr


@pytest.mark.parametrize('lane', ['project', 'global'])
def test_install_never_creates_absent_cached_hook(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, csk_home: Path, skills_root: Path, lane: str,
) -> None:
    project = _configure(tmp_path, monkeypatch, csk_home, skills_root)
    assert not (csk_home / 'hooks').exists()
    if lane == 'project':
        _write_skillfile(project)
        assert cli.main(['install', 'app']) == 0
    else:
        assert cli.main(['global', 'init']) == 0
        assert cli.main(['global', 'install']) == 0
    assert not (csk_home / 'hooks').exists()


def test_install_refreshes_only_existing_shells(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, csk_home: Path, skills_root: Path,
) -> None:
    project = _configure(tmp_path, monkeypatch, csk_home, skills_root)
    hooks_dir = csk_home / 'hooks'
    hooks_dir.mkdir(parents=True)
    cached = hooks_dir / 'csk.bash'
    cached.write_text(STALE_HOOK)
    _write_skillfile(project)
    assert cli.main(['install', 'app']) == 0
    assert cached.read_text() == shell_init.shell_init('bash')
    assert not (hooks_dir / 'csk.zsh').exists()
    assert not (hooks_dir / 'csk.ps1').exists()


def test_install_refreshes_existing_powershell_hook(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, csk_home: Path, skills_root: Path,
) -> None:
    project = _configure(tmp_path, monkeypatch, csk_home, skills_root)
    hooks_dir = csk_home / 'hooks'
    hooks_dir.mkdir(parents=True)
    cached = hooks_dir / 'csk.ps1'
    cached.write_text('# stale\n')
    _write_skillfile(project)
    assert cli.main(['install', 'app']) == 0
    assert cached.read_text() == shell_init.shell_init('powershell')


def test_global_init_does_not_refresh_cached_hook(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, csk_home: Path, skills_root: Path,
) -> None:
    _configure(tmp_path, monkeypatch, csk_home, skills_root)
    hooks_dir = csk_home / 'hooks'
    hooks_dir.mkdir(parents=True)
    cached = hooks_dir / 'csk.bash'
    cached.write_text(STALE_HOOK)
    assert cli.main(['global', 'init']) == 0
    assert cached.read_text() == STALE_HOOK


@pytest.mark.skipif(os.name == 'nt', reason='POSIX directory write bits')
def test_install_refresh_failure_warns_not_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, csk_home: Path, skills_root: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    project = _configure(tmp_path, monkeypatch, csk_home, skills_root)
    hooks_dir = csk_home / 'hooks'
    hooks_dir.mkdir(parents=True)
    cached = hooks_dir / 'csk.bash'
    cached.write_text(STALE_HOOK)
    hooks_dir.chmod(0o555)
    try:
        _write_skillfile(project)
        assert cli.main(['install', 'app']) == 0
    finally:
        hooks_dir.chmod(0o755)
    assert 'could not refresh cached shell hooks' in capsys.readouterr().err
    assert cached.read_text() == STALE_HOOK


@pytest.mark.parametrize('mutant', ['omit-global-refresh'])
def test_upgrade_narrowing_mutants_are_killed(tmp_path: Path, mutant: str) -> None:
    source = Path(__file__).resolve().parents[1]
    checkout = tmp_path / 'mutant'
    shutil.copytree(source / 'src', checkout / 'src')
    shutil.copytree(source / 'tests', checkout / 'tests', ignore=shutil.ignore_patterns('__pycache__'))
    shutil.copy2(source / 'pyproject.toml', checkout / 'pyproject.toml')
    module = checkout / 'src/csk/cli.py'
    text = module.read_text()
    old = ('    result = global_install.install(cfg, options=options, only=_global_only(args))\n'
           '    if not options.dry_run:\n'
           '        _refresh_existing_cached_hooks_best_effort(cfg.path.parent)\n')
    assert text.count(old) == 1
    module.write_text(text.replace(old, '    result = global_install.install(cfg, options=options, only=_global_only(args))\n'))
    test_file = 'tests/test_shell_hook_upgrade.py'
    test = 'test_install_refreshes_existing_cached_hook'
    selection = test + ' and global'
    result = subprocess.run(
        [sys.executable, '-m', 'pytest', '-q', test_file, '-k', selection,
         '--basetemp=' + str(tmp_path / 'mutant-tmp')],
        cwd=checkout, env={**os.environ, 'PYTHONPATH': str(checkout / 'src')},
        capture_output=True, text=True, timeout=300,
    )
    (tmp_path / (mutant + '.log')).write_text(result.stdout + result.stderr)
    assert result.returncode == 1, result.stdout + result.stderr
    assert 'FAILED ' + test_file + '::' + test in result.stdout
