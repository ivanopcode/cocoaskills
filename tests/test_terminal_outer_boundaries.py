import subprocess
import sys

import pytest

from csk import cli, config, dispatch, hybrid
from csk.terminal import terminal_text
from conftest import make_config, make_project, make_skill_repo, write_skillfile


# Real published -I runtime refuses corrupt state safely; stdout remains the exact
# machine launcher path, including hostile path bytes (not a valid publication claim).
@pytest.mark.parametrize('name', ['ordinary café', 'hostile-\x1b]0;FORGED\x07-\u202e', 'hostile-\nremediation: FORGED'])
@pytest.mark.skipif(sys.platform == 'win32', reason='Generated /bin/sh wrapper and control-bearing paths require POSIX')
def test_generated_dispatcher_stderr(tmp_path, name):
    home = tmp_path / name
    script = dispatch.ensure_dispatcher_runtime(home)
    wrapper = home / 'dispatch' / 'dispatcher'
    wrapper.write_text(dispatch.dispatcher_wrapper_text(python=sys.executable, runtime=script))
    wrapper.chmod(0o700)
    (home / 'dispatch' / 'current').symlink_to('not-a-generation')
    result = subprocess.run([str(wrapper), 'resolve', 'demo'], cwd=tmp_path, capture_output=True, text=True)
    assert result.returncode == 1
    assert 'error:' in result.stderr and 'current' in result.stderr
    assert '\nremediation: FORGED' not in result.stderr
    assert terminal_text(result.stderr.rstrip('\n')) == result.stderr.rstrip('\n')
    from csk._dispatch_runtime import failure_launcher_path
    assert result.stdout == f'{failure_launcher_path(str(home), 1)}\n'


# A real child process must exit 1 with useful traceback structure and escaped
# exception/cause/note data. Ordinary messages remain readable; no default traceback leaks.
@pytest.mark.parametrize('message', ['ordinary café', 'remote\x1b]0;FORGED\x07\nremediation: FORGED\u202e'])
def test_uncaught_exception_reporting(message):
    code = '''import sys
from csk import cli
def operation(args):
    try:
        raise ValueError(sys.argv[1])
    except ValueError as cause:
        error = RuntimeError(sys.argv[1])
        error.add_note(sys.argv[1])
        raise error from cause
cli._dispatch = operation
sys.exit(cli.main(['list']))
'''
    result = subprocess.run([sys.executable, '-c', code, message], capture_output=True, text=True)
    assert result.returncode == 1
    assert 'Traceback (most recent call last):' in result.stderr
    assert 'direct cause' in result.stderr
    assert 'RuntimeError: ' + terminal_text(message) in result.stderr
    assert 'ValueError: ' + terminal_text(message) in result.stderr
    assert '\x1b' not in result.stderr and '\u202e' not in result.stderr
    assert '\nremediation: FORGED' not in result.stderr


# Real selection/validation errors retain owned layout but cannot forge repair lines.
@pytest.mark.parametrize('plant', ['ordinary', 'missing\nremediation: FORGED\x1b'])
def test_global_selection_newlines(tmp_path, skills_root, csk_home, monkeypatch, capsys, plant):
    project = make_project(tmp_path)
    cfg = make_config(csk_home, skills_root, project)
    config.save_config(cfg)
    monkeypatch.setenv('CSK_CONFIG', str(cfg.path))
    root = csk_home / 'global'
    root.mkdir()
    make_skill_repo(skills_root, 'demo', tag='v1')
    write_skillfile(root, {'schema_version': 1, 'skills': [{'name': 'demo', 'tag': 'v1'}]})
    assert cli.main(['global', 'install', '--only', 'demo']) == 0
    capsys.readouterr()
    for command in ('install', 'upgrade', 'update'):
        assert cli.main(['global', command, '--only', plant]) == 1
        output = capsys.readouterr()
        assert 'Global skill not declared' in output.err
        assert terminal_text(plant) in output.err
        assert '\nremediation: FORGED' not in output.err and '\x1b' not in output.err


# Genuine malformed hybrid input carries an untrusted path through InstallError.
@pytest.mark.parametrize('name', ['ordinary', 'manager\nremediation: FORGED'])
def test_hybrid_path_newlines(tmp_path, skills_root, monkeypatch, capsys, name):
    if sys.platform == 'win32' and '\n' in name:
        pytest.skip('Windows forbids newline path components; ordinary malformed input remains covered')
    project = make_project(tmp_path)
    write_skillfile(project, {'schema_version': 1, 'skills': []})
    home = tmp_path / name
    home.mkdir()
    cfg = make_config(home, skills_root, project, agents=['codex_cli'])
    config.save_config(cfg)
    monkeypatch.setenv('CSK_CONFIG', str(cfg.path))
    path = hybrid.hybrid_manifest_path(home)
    path.parent.mkdir()
    path.write_text('[]')
    assert cli.main(['install', 'app']) == 1
    output = capsys.readouterr()
    assert 'must contain a JSON object' in output.err
    assert terminal_text(str(path)) in output.err
    assert '\nremediation: FORGED' not in output.err


# The standalone copy and manager share the scalar policy across the full Unicode
# control/format categories, while legitimate joining text remains readable.
def test_standalone_scalar_policy():
    import unicodedata
    from csk._dispatch_runtime import _diagnostic_text
    for code in range(sys.maxunicode + 1):
        char = chr(code)
        if unicodedata.category(char) in {'Cc', 'Cf', 'Cs', 'Zl', 'Zp'}:
            assert _diagnostic_text(char) == terminal_text(char)
    assert _diagnostic_text('می\u200cروم 👩\u200d💻') == 'می\u200cروم 👩\u200d💻'
