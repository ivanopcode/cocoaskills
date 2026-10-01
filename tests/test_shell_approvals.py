from __future__ import annotations

import hashlib
import os
import shlex
import shutil
import stat
import subprocess
import sys
from pathlib import Path

import pytest

from csk import cli, env_files, shell_init


def _line(path: Path) -> str:
    return f"{hashlib.sha256(path.read_bytes()).hexdigest()}  {path.resolve()}"


def _store() -> Path:
    return Path.home() / '.cocoaskills/shell/approved'


def _run_hook(tmp_path: Path, shell: str, project: Path, script: str = '', **env: str) -> subprocess.CompletedProcess[str]:
    executable = shutil.which(shell)
    if executable is None:
        pytest.skip(f'{shell} unavailable')
    hook = tmp_path / 'hook.sh'
    hook.write_bytes(shell_init.shell_init(shell).encode('utf-8'))
    args = [executable, '-dfc'] if shell == 'zsh' else [executable, '--noprofile', '--norc', '-c']
    setup = 'cd "$PROJECT_PATH"; '
    if 'PATH' in env:
        env['TEST_PATH'] = env.pop('PATH')
        setup += 'if command -v cygpath >/dev/null 2>&1; then TEST_PATH="$(cygpath -u "$TEST_PATH")"; fi; PATH="$TEST_PATH"; export PATH; '
    return subprocess.run(
        [*args, setup + '. "$HOOK"; ' + script + '\nprintf "active=%s\\n" "${CSK_ACTIVE_ENV-unset}"'],
        cwd=project, env={**os.environ, 'HOOK': hook.as_posix(), 'PROJECT_PATH': project.as_posix(), 'SHELL': executable, **env},
        text=True, capture_output=True, timeout=120,
    )


@pytest.mark.parametrize('shell', ['bash', 'zsh'])
def test_hostile_env_not_sourced_notice_once(tmp_path: Path, shell: str) -> None:
    project = tmp_path / 'hostile project'
    (project / '.agents').mkdir(parents=True)
    env = project / '.agents/env.sh'
    marker = tmp_path / 'executed'
    env.write_bytes(f'touch {shlex.quote(marker.as_posix())}\n'.encode('utf-8'))
    result = _run_hook(tmp_path, shell, project, '_csk_auto_env; _csk_auto_env')
    assert result.returncode == 0, result.stderr
    assert not marker.exists()
    assert result.stdout == 'active=unset\n'
    assert result.stderr == f'csk: skipped {env.resolve()}: not approved (review it, then run: csk shell approve {env.resolve()})\n'


@pytest.mark.parametrize('shell', ['bash', 'zsh'])
def test_csk_written_env_sourced(tmp_path: Path, shell: str) -> None:
    project = tmp_path / 'trusted project'
    env_files.write_env_files(project)
    result = _run_hook(tmp_path, shell, project)
    assert result.returncode == 0, result.stderr
    assert result.stdout == f'active={(project / ".agents/env.sh").resolve()}\n', result.stderr
    assert result.stderr == ''


@pytest.mark.parametrize('shell', ['bash', 'zsh'])
def test_modified_env_skipped_then_approve_allows_revoke_removes(tmp_path: Path, shell: str, capsys: pytest.CaptureFixture[str]) -> None:
    project = tmp_path / 'project'
    env_files.write_env_files(project)
    env = project / '.agents/env.sh'
    marker = tmp_path / 'executed'
    env.write_bytes(f'touch {shlex.quote(marker.as_posix())}\n'.encode('utf-8'))
    rejected = _run_hook(tmp_path, shell, project)
    assert rejected.returncode == 0, rejected.stderr
    assert not marker.exists()
    assert 'csk shell approve ' + str(env.resolve()) in rejected.stderr
    assert cli.main(['shell', 'approve', str(env), '--yes']) == 0
    output = capsys.readouterr().out
    assert str(env.resolve()) in output and hashlib.sha256(env.read_bytes()).hexdigest() in output
    assert env.read_bytes().decode('utf-8') in output
    allowed = _run_hook(tmp_path, shell, project)
    assert allowed.returncode == 0, allowed.stderr
    assert marker.exists()
    assert cli.main(['shell', 'approvals']) == 0
    assert _line(env) in capsys.readouterr().out
    assert cli.main(['shell', 'revoke', str(env)]) == 0
    marker.unlink()
    revoked = _run_hook(tmp_path, shell, project)
    assert revoked.returncode == 0, revoked.stderr
    assert not marker.exists()
    assert str(env.resolve()) not in _store().read_text()


@pytest.mark.parametrize('shell', ['bash', 'zsh'])
def test_digest_tool_absent_skips_even_approved(tmp_path: Path, shell: str) -> None:
    project = tmp_path / 'project'
    env_files.write_env_files(project)
    empty = tmp_path / 'no tools'
    empty.mkdir()
    result = _run_hook(tmp_path, shell, project, '_csk_auto_env', PATH=str(empty))
    assert result.returncode == 0, result.stderr
    assert result.stdout == 'active=unset\n'
    assert result.stderr.count('not approved (review it, then run: csk shell approve ') == 1


@pytest.mark.parametrize('shell', ['bash', 'zsh'])
def test_symlinked_project_realpath_approval(tmp_path: Path, shell: str) -> None:
    project = tmp_path / 'real project'
    env_files.write_env_files(project)
    link = tmp_path / 'linked project'
    try:
        link.symlink_to(project, target_is_directory=True)
    except OSError:
        pytest.skip('symlinks unavailable')
    result = _run_hook(tmp_path, shell, link)
    assert result.returncode == 0, result.stderr
    assert result.stdout == f'active={(project / ".agents/env.sh").resolve()}\n', result.stderr
    assert result.stderr == ''


@pytest.mark.parametrize('shell', ['bash', 'zsh'])
def test_global_hostile_and_modified_files_skipped(tmp_path: Path, shell: str) -> None:
    project = tmp_path / 'project'
    project.mkdir()
    home = tmp_path / 'manager'
    (home / 'global').mkdir(parents=True)
    env = home / 'global/env.sh'
    marker = tmp_path / 'executed'
    env.write_bytes(f'touch {shlex.quote(marker.as_posix())}\n'.encode('utf-8'))
    result = _run_hook(tmp_path, shell, project, CSK_CONFIG=str(home / 'config.json'))
    assert result.returncode == 0, result.stderr
    assert not marker.exists()
    assert 'not approved' in result.stderr
    env_files.write_global_env_files(home)
    allowed = _run_hook(tmp_path, shell, project, 'printf "global=%s\\n" "$CSK_ACTIVE_GLOBAL_ENV"', CSK_CONFIG=str(home / 'config.json'))
    assert allowed.returncode == 0, allowed.stderr
    assert f'global={env.resolve()}' in allowed.stdout
    env.write_bytes(f'touch {shlex.quote(marker.as_posix())}\n'.encode('utf-8'))
    changed = _run_hook(tmp_path, shell, project, CSK_CONFIG=str(home / 'config.json'))
    assert changed.returncode == 0, changed.stderr
    assert not marker.exists()
    assert 'not approved' in changed.stderr


def test_approve_non_tty_refuses_even_with_yes_on_stdin(tmp_path: Path) -> None:
    env = tmp_path / 'env.sh'
    env.write_text('echo hostile\n')
    result = subprocess.run([sys.executable, '-m', 'csk', 'shell', 'approve', str(env)], input='yes\n', text=True, capture_output=True, env={**os.environ, 'PYTHONPATH': str(Path(__file__).resolve().parents[1] / 'src')})
    assert result.returncode == 2
    assert 'interactive terminal' in result.stderr and '--yes' in result.stderr
    assert not _store().exists()


def test_approve_defaults_to_nearest_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    project = tmp_path / 'project'
    (project / '.agents').mkdir(parents=True)
    (project / 'nested').mkdir()
    env = project / '.agents' / ('env.ps1' if os.name == 'nt' else 'env.sh')
    env.write_text('# reviewed\n')
    monkeypatch.chdir(project / 'nested')
    monkeypatch.delenv('SHELL', raising=False)
    monkeypatch.delenv('PSModulePath', raising=False)
    assert cli.main(['shell', 'approve', '--yes']) == 0
    assert _line(env) in _store().read_text()
    assert str(env.resolve()) in capsys.readouterr().out


@pytest.mark.parametrize('shell', ['bash', 'zsh'])
def test_default_approve_uses_hook_logical_ancestor(tmp_path: Path, shell: str) -> None:
    executable = shutil.which(shell)
    if executable is None:
        pytest.skip(f'{shell} unavailable')
    physical = tmp_path / 'physical parent'
    logical = tmp_path / 'logical parent'
    for directory in [physical, logical]:
        (directory / '.agents').mkdir(parents=True)
    (physical / 'nested').mkdir()
    alias = logical / 'alias'
    try:
        alias.symlink_to(physical / 'nested', target_is_directory=True)
    except OSError:
        pytest.skip('directory symlinks unavailable')
    marker = tmp_path / 'logical executed'
    logical_env = logical / '.agents/env.sh'
    physical_env = physical / '.agents/env.sh'
    logical_env.write_bytes(f'touch {shlex.quote(marker.as_posix())}\n'.encode('utf-8'))
    physical_env.write_bytes(b'# different physical ancestor\n')
    rejected = _run_hook(tmp_path, shell, alias)
    assert rejected.returncode == 0, rejected.stderr
    assert str(logical_env.resolve()) in rejected.stderr
    args = [executable, '-dfc'] if shell == 'zsh' else [executable, '--noprofile', '--norc', '-c']
    approved = subprocess.run([*args, 'cd "$PROJECT_PATH"; "$PYTHON" -m csk shell approve --yes'], cwd=alias, env={**os.environ, 'PROJECT_PATH': alias.as_posix(), 'PYTHON': Path(sys.executable).as_posix(), 'PYTHONPATH': str(Path(__file__).resolve().parents[1] / 'src'), 'SHELL': executable}, text=True, capture_output=True, timeout=120)
    assert approved.returncode == 0, approved.stderr
    assert str(logical_env.resolve()) in approved.stdout
    assert _line(logical_env) in _store().read_text()
    assert str(physical_env.resolve()) not in _store().read_text()
    allowed = _run_hook(tmp_path, shell, alias)
    assert allowed.returncode == 0, allowed.stderr
    assert marker.exists()


def test_first_approval_provisions_manager_home_for_native_cache(tmp_path: Path) -> None:
    from csk.builds.cache import CacheEntryStatus, CacheExpectation, cache_for_manager_home
    from test_build_cache_windows import _build_input

    previous_umask = os.umask(0o022)
    try:
        env_files.write_env_files(tmp_path / 'project')
    finally:
        os.umask(previous_umask)
    home = Path.home() / '.cocoaskills'
    if os.name != 'nt':
        assert stat.S_IMODE(home.stat().st_mode) == 0o700
    inspection = cache_for_manager_home(home).inspect(CacheExpectation(_build_input()))
    assert inspection.status == CacheEntryStatus.MISS, inspection


def test_upgrade_auto_approves_final_env_paths(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import protocol_lifecycle_observations as observations

    original = observations._run_cli
    def observed_cli(argv: list[str]) -> tuple[int, str, str]:
        result = original(argv)
        assert result[0] == cli.EXIT_OK, (argv, result)
        return result
    monkeypatch.setattr(observations, '_run_cli', observed_cli)

    root = tmp_path / 'upgrade'
    observed = observations._observe_upgrade_fetch(root, mode='all')
    assert observed['deduplicated'] is True
    lines = _store().read_text().splitlines()
    for project in ['project-one', 'project-two']:
        for name in ['env.sh', 'env.ps1']:
            assert _line(root / project / '.agents' / name) in lines


def test_global_upgrade_auto_approves_final_env_paths(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import protocol_lifecycle_observations as observations

    original = observations._run_cli
    def observed_cli(argv: list[str]) -> tuple[int, str, str]:
        result = original(argv)
        assert result[0] == cli.EXIT_OK, (argv, result)
        return result
    monkeypatch.setattr(observations, '_run_cli', observed_cli)

    root = tmp_path / 'global-upgrade'
    observed = observations._observe_upgrade_fetch(root, mode='global')
    assert observed['fetched'] == ['direct', 'transitive']
    assert observed['excluded'] == ['unrelated']
    lines = _store().read_text().splitlines()
    for name in ['env.sh', 'env.ps1']:
        assert _line(root / 'home' / 'global' / name) in lines


def test_auto_approval_records_both_formats_replaces_digest_and_private_modes(tmp_path: Path) -> None:
    project = tmp_path / 'project'
    env_files.write_env_files(project)
    paths = [project / '.agents/env.sh', project / '.agents/env.ps1']
    assert set(_store().read_text().splitlines()) == {_line(path) for path in paths}
    assert cli.main(['shell', 'revoke', str(paths[0])]) == 0
    paths[0].write_text('# modified\n')
    assert cli.main(['shell', 'approve', str(paths[0]), '--yes']) == 0
    env_files.write_env_files(project)
    assert set(_store().read_text().splitlines()) == {_line(path) for path in paths}
    if os.name != 'nt':
        assert stat.S_IMODE(_store().stat().st_mode) == 0o600
        assert stat.S_IMODE(_store().parent.stat().st_mode) == 0o700


def _shell_tool(shell: str, name: str) -> Path | None:
    executable = shutil.which(shell)
    if executable is None:
        return None
    args = [executable, '-dfc'] if shell == 'zsh' else [executable, '--noprofile', '--norc', '-c']
    # Resolve in the actual shell, including Git Bash's startup PATH. Windows
    # shutil.which may select a PATHEXT batch launcher for a different Perl.
    result = subprocess.run([*args, 'tool="$(command -v "$TOOL")" || exit 44; if command -v cygpath >/dev/null 2>&1; then cygpath -wa "$tool" || exit 45; else printf "%s\\n" "$tool"; fi'], env={**os.environ, 'TOOL': name}, capture_output=True, text=True, timeout=30)
    if result.returncode == 44:
        return None
    assert result.returncode == 0, f'{shell} tool discovery failed for {name}: {result.stderr}'
    return Path(result.stdout.strip())


@pytest.mark.parametrize('shell', ['bash', 'zsh'])
@pytest.mark.parametrize('tool', ['shasum', 'sha256sum', 'openssl'])
def test_digest_tool_fallbacks(tmp_path: Path, shell: str, tool: str) -> None:
    executable = _shell_tool(shell, tool)
    if executable is None:
        pytest.skip(f'{tool} unavailable')
    project = tmp_path / 'project'
    env_files.write_env_files(project)
    tools = tmp_path / 'tools'
    tools.mkdir()
    # Execute real tools at their installed paths. Relocating MSYS executables
    # via symlinks breaks Windows DLL discovery when PATH is isolated.
    names = [tool, 'dirname']
    if tool == 'shasum' and _shell_tool(shell, 'perl'):
        names.append('perl')
    if _shell_tool(shell, 'cygpath'):
        names.append('cygpath')
    for name in names:
        source = _shell_tool(shell, name)
        assert source
        wrapper = tools / name
        wrapper.write_bytes(('#!/bin/sh\nexec ' + shlex.quote(Path(source).as_posix()) + ' "$@"\n').encode('utf-8'))
        wrapper.chmod(0o755)
    probe = {'shasum': 'shasum -a 256', 'sha256sum': 'sha256sum', 'openssl': 'openssl dgst -sha256'}[tool]
    diagnostic = 'if [ -z "${CSK_ACTIVE_ENV:-}" ]; then ' + probe + ' < "$PROBE_ENV" >&2; fi'
    result = _run_hook(tmp_path, shell, project, diagnostic, PATH=str(tools), PROBE_ENV=(project / '.agents/env.sh').as_posix())
    assert result.returncode == 0, result.stderr
    assert result.stdout == f'active={(project / ".agents/env.sh").resolve()}\n', f'{tool}: {executable}\n{result.stderr}'
    assert result.stderr == ''


@pytest.mark.parametrize('answer, expected', [('yes', 0), ('no', 2)])
def test_interactive_approval_confirmation(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, answer: str, expected: int) -> None:
    env = tmp_path / 'env.sh'
    env.write_text('# reviewed\n')
    monkeypatch.setattr(sys.stdin, 'isatty', lambda: True)
    monkeypatch.setattr('builtins.input', lambda _: answer)
    assert cli.main(['shell', 'approve', str(env)]) == expected
    assert _store().exists() == (expected == 0)


def test_approval_captures_reviewed_bytes_not_later_changes(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    env = tmp_path / 'env.sh'
    env.write_text('# reviewed\n')
    reviewed_line = _line(env)
    monkeypatch.setattr(sys.stdin, 'isatty', lambda: True)
    def confirm(_: str) -> str:
        env.write_text('# replaced during review\n')
        return 'yes'
    monkeypatch.setattr('builtins.input', confirm)
    assert cli.main(['shell', 'approve', str(env)]) == 0
    assert _store().read_text() == reviewed_line + '\n'
    assert _line(env) not in _store().read_text()


def test_concurrent_csk_writes_retain_all_approval_lines(tmp_path: Path) -> None:
    root = Path(__file__).resolve().parents[1] / 'src'
    projects = [tmp_path / f'project-{i}' for i in range(8)]
    children = [subprocess.Popen([sys.executable, '-c', 'from pathlib import Path; import sys; from csk.env_files import write_env_files; write_env_files(Path(sys.argv[1]))', str(project)], env={**os.environ, 'PYTHONPATH': str(root)}, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True) for project in projects]
    for child in children:
        stdout, stderr = child.communicate(timeout=120)
        assert child.returncode == 0, stdout + stderr
    assert set(_store().read_text().splitlines()) == {_line(project / '.agents' / name) for project in projects for name in ('env.sh', 'env.ps1')}


@pytest.mark.parametrize('scope', ['project', 'global'])
def test_real_install_approves_final_paths_not_staging(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, csk_home: Path, skills_root: Path, scope: str) -> None:
    from conftest import make_config, make_project, write_skillfile
    from csk import config
    project = make_project(tmp_path)
    cfg = make_config(csk_home, skills_root, project, agents=['codex_cli'])
    config.save_config(cfg)
    monkeypatch.setenv('CSK_CONFIG', str(cfg.path))
    if scope == 'project':
        write_skillfile(project, {'schema_version': 1, 'project': {'alias': 'app'}, 'agents': ['codex_cli'], 'skills': []})
        assert cli.main(['install', 'app']) == 0
        directory = project / '.agents'
    else:
        assert cli.main(['global', 'init']) == 0
        assert cli.main(['global', 'install']) == 0
        directory = csk_home / 'global'
    paths = [directory / 'env.sh', directory / 'env.ps1']
    assert set(_store().read_text().splitlines()) == {_line(path) for path in paths}


@pytest.mark.parametrize('shell', ['bash', 'zsh'])
def test_notice_is_per_digest_and_active_env_is_rechecked(tmp_path: Path, shell: str) -> None:
    project = tmp_path / 'project'
    env_files.write_env_files(project)
    env = project / '.agents/env.sh'
    marker = tmp_path / 'executed'
    script = '''printf 'touch "%s"\\n' "$MARKER" >> "$ENVFILE"
_csk_auto_env
_csk_auto_env
printf '# another digest\\n' >> "$ENVFILE"
_csk_auto_env
_csk_auto_env'''
    result = _run_hook(tmp_path, shell, project, script, MARKER=str(marker), ENVFILE=str(env))
    assert result.returncode == 0, result.stderr
    assert not marker.exists()
    assert result.stdout == 'active=unset\n'
    assert result.stderr.count('not approved') == 2


def test_atomic_store_failure_keeps_previous_approvals(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from csk import shell_approvals
    project = tmp_path / 'project'
    env_files.write_env_files(project)
    original = _store().read_bytes()
    def fail_replace(source: object, target: object) -> None:
        raise OSError('injected replace failure')
    monkeypatch.setattr(shell_approvals.os, 'replace', fail_replace)
    assert cli.main(['shell', 'revoke', str(project / '.agents/env.sh')]) == 2
    assert _store().read_bytes() == original
    assert list(_store().parent.glob('.approved.*')) == [_store().parent / '.approved.lock']


@pytest.mark.parametrize('mutant', ['path-only', 'non-tty-auto-approve', 'physical-ancestor', 'unprovisioned-manager-home'])
def test_narrowing_mutants_are_killed(tmp_path: Path, mutant: str) -> None:
    source = Path(__file__).resolve().parents[1]
    checkout = tmp_path / 'mutant'
    shutil.copytree(source / 'src', checkout / 'src')
    shutil.copytree(source / 'tests', checkout / 'tests', ignore=shutil.ignore_patterns('__pycache__'))
    shutil.copy2(source / 'pyproject.toml', checkout / 'pyproject.toml')
    if mutant == 'path-only':
        module = checkout / 'src/csk/shell_init.py'
        text = module.read_text()
        old = 'if [ "$line" = "$_csk_checked_digest  $_csk_checked_path" ]; then'
        new = 'if [ "${line#*  }" = "$_csk_checked_path" ]; then'
        test = 'test_modified_env_skipped_then_approve_allows_revoke_removes'
    elif mutant == 'non-tty-auto-approve':
        module = checkout / 'src/csk/cli.py'
        text = module.read_text()
        old = 'if not sys.stdin.isatty():\n            raise ValueError("csk shell approve requires an interactive terminal; review the file and pass --yes")'
        new = 'if not sys.stdin.isatty():\n            shell_approvals.record({path: digest})\n            return EXIT_OK'
        test = 'test_approve_non_tty_refuses_even_with_yes_on_stdin'
    elif mutant == 'physical-ancestor':
        module = checkout / 'src/csk/shell_approvals.py'
        text = module.read_text()
        old = 'cwd = logical_cwd'
        new = 'cwd = logical_cwd.resolve()'
        test = 'test_default_approve_uses_hook_logical_ancestor'
    else:
        module = checkout / 'src/csk/shell_approvals.py'
        text = module.read_text()
        old = '        provision_new_manager_home(target.parent.parent)'
        new = '        target.parent.parent.mkdir(parents=True, exist_ok=True)\n' + old
        test = 'test_first_approval_provisions_manager_home_for_native_cache'
    assert text.count(old) == 1
    module.write_text(text.replace(old, new))
    result = subprocess.run([sys.executable, '-m', 'pytest', '-q', 'tests/test_shell_approvals.py', '-k', test, '--basetemp=' + str(tmp_path / 'mutant-tmp')], cwd=checkout, env={**os.environ, 'PYTHONPATH': str(checkout / 'src')}, capture_output=True, text=True, timeout=120)
    assert result.returncode == 1, result.stdout + result.stderr
    assert 'FAILED tests/test_shell_approvals.py::' + test in result.stdout


def test_store_read_failure_refuses_instead_of_replacing_approvals(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from csk import shell_approvals
    project = tmp_path / 'project'
    env_files.write_env_files(project)
    original = _store().read_bytes()
    original_read = Path.read_text
    def denied(path: Path, *args: object, **kwargs: object) -> str:
        if path == shell_approvals.approval_file():
            raise PermissionError('injected approval read denial')
        return original_read(path, *args, **kwargs)
    monkeypatch.setattr(Path, 'read_text', denied)
    assert cli.main(['shell', 'revoke', str(project / '.agents/env.sh')]) == 2
    assert cli.main(['shell', 'approvals']) == 2
    assert _store().read_bytes() == original


def test_non_regular_and_unrepresentable_env_paths_refused(tmp_path: Path) -> None:
    assert cli.main(['shell', 'approve', str(tmp_path), '--yes']) == 2
    if os.name != 'nt':
        env = tmp_path / 'line\nbreak.sh'
        env.write_text('# cannot represent in line store\n')
        assert cli.main(['shell', 'approve', str(env), '--yes']) == 2
    assert not _store().exists()


@pytest.mark.parametrize('operation', ['is_file', 'read_bytes'])
@pytest.mark.parametrize('error_number', [13, 5, 36, 40], ids=['EACCES', 'EIO', 'ENAMETOOLONG', 'ELOOP'])
def test_review_filesystem_failures_refuse_without_approval(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, operation: str, error_number: int, capsys: pytest.CaptureFixture[str]) -> None:
    env = tmp_path / 'env.sh'
    env.write_text('# reviewed\n')
    original = getattr(Path, operation)
    firings = []
    def denied(path: Path, *args: object, **kwargs: object) -> object:
        if path == env.resolve():
            firings.append(error_number)
            raise OSError(error_number, 'injected review refusal')
        return original(path, *args, **kwargs)
    monkeypatch.setattr(Path, operation, denied)
    assert cli.main(['shell', 'approve', str(env), '--yes']) == 2
    assert firings == [error_number]
    assert 'cannot review env file' in capsys.readouterr().err
    assert not _store().exists()
