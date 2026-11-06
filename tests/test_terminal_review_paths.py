import json
import unicodedata
from pathlib import Path

import pytest

from conftest import make_config, make_project, make_skill_repo, write_skillfile, commit_all
from test_mcp_project_install import project_cli
from csk import cli, config, git_ops

PLANTS = ['\x1b[31mFORGED', '\x1b]0;FORGED\x07', '\x07FORGED', '\rFORGED', '\nFORGED', '\u202eFORGED\u202c', '\u061c\u200e\u200fFORGED']
UNICODE = ['日本語 café العربية עברית', 'می\u200cروم', 'क्\u200dष', '👩\u200d💻']


def observe(args, capsys):
    rc = cli.main(args)
    captured = capsys.readouterr()
    return rc, captured.out + captured.err


def safe(output):
    bad = [hex(ord(c)) for c in output if (unicodedata.category(c) in {'Cc', 'Cs', 'Zl', 'Zp'} and c != '\n') or c in '\u061c\u200e\u200f\u202a\u202b\u202c\u202d\u202e\u2066\u2067\u2068\u2069']
    assert not bad and '\nFORGED' not in output, (bad, repr(output))


def setup(tmp_path, skills_root, csk_home, monkeypatch):
    project = make_project(tmp_path)
    cfg = make_config(csk_home, skills_root, project)
    config.save_config(cfg)
    monkeypatch.setenv('CSK_CONFIG', str(cfg.path))
    root = csk_home / 'global'
    root.mkdir()
    valid = {'schema_version': 1, 'skills': [{'name': 'demo', 'tag': 'v1'}]}
    for directory in (root, project):
        write_skillfile(directory, valid)
    return cfg, project, root


# Actual command dispatch must escape remote fetch errors in every fetch lane.
# Successful upgrade uses the real local repository with only network fetch replaced.
@pytest.mark.parametrize('command', [['update'], ['upgrade', 'app'], ['global', 'upgrade']])
def test_fetch_lanes(tmp_path, skills_root, csk_home, monkeypatch, capsys, command):
    setup(tmp_path, skills_root, csk_home, monkeypatch)
    make_skill_repo(skills_root, 'demo', tag='v1')
    monkeypatch.setattr(git_ops, 'fetch_repo', lambda *a, **kw: None)
    rc, output = observe(command, capsys)
    assert rc == 0, repr(output)
    safe(output)
    failures = []
    for plant in PLANTS + ['café العربية']:
        def fail(*a, **kw):
            raise git_ops.GitError('remote: ' + plant)
        monkeypatch.setattr(git_ops, 'fetch_repo', fail)
        rc, output = observe(command, capsys)
        assert rc == 1 and 'remote:' in output, (rc, repr(output))
        try:
            safe(output)
        except AssertionError:
            failures.append(repr(output))
    assert not failures, failures


# Config/list/status display must preserve ordinary RTL and legitimate joiners;
# JSON retains the exact raw ref even when it is not an existing repository ref.
def test_ordinary_unicode(tmp_path, skills_root, csk_home, monkeypatch, capsys):
    from csk.terminal import terminal_text
    cfg, project, root = setup(tmp_path, skills_root, csk_home, monkeypatch)
    failures = []
    for text in UNICODE:
        write_skillfile(project, {'schema_version': 1, 'skills': [{'name': 'demo', 'tag': text}]})
        rc, listed = observe(['list'], capsys)
        assert rc == 0
        assert cli.main(['status', 'app', '--json']) == 0
        payload = json.loads(capsys.readouterr().out)
        assert payload[0]['skills'][0]['ref'] == text
        if terminal_text(text) != text or text not in listed:
            failures.append(repr(listed))
    assert not failures, failures


# Unsafe manifest ref display is sanitized while machine status values remain semantic.
def test_list_and_machine_values(tmp_path, skills_root, csk_home, monkeypatch, capsys):
    cfg, project, root = setup(tmp_path, skills_root, csk_home, monkeypatch)
    for plant in PLANTS:
        for directory in (root, project):
            write_skillfile(directory, {'schema_version': 1, 'skills': [{'name': 'demo', 'tag': 'ref-' + plant}]})
        for command in (['list'], ['list', '--paths'], ['global', 'list']):
            rc, output = observe(command, capsys)
            assert rc == 0, repr(output)
            safe(output)
        assert cli.main(['status', 'app', '--json']) == 0
        payload = json.loads(capsys.readouterr().out)
        assert payload[0]['skills'][0]['ref'] == 'ref-' + plant


# Parser rejection must not execute terminal controls from unknown option strings.
@pytest.mark.parametrize('command', [['doctor'], ['mcp', 'doctor'], ['mcp', 'local-target', 'list'], ['mcp', 'local-target', 'add'], ['mcp', 'local-target', 'remove'], ['mcp', 'migrate']])
def test_parser_error_paths(capsys, command):
    failures = []
    for plant in PLANTS:
        rc, output = observe(command + ['--bad-' + plant], capsys)
        assert rc == 2
        try:
            safe(output)
        except AssertionError:
            failures.append(repr(output))
    assert not failures, failures


# All supported MCP subcommands run through their real settings handler in owned fixtures.
# A hostile unknown server field is a genuine parse error; no renderer is mocked.
@pytest.mark.parametrize('command', [['local-target', 'list'], ['local-target', 'add'], ['local-target', 'remove'], ['migrate']])
def test_mcp_semantic_errors(project_cli, capsys, command):
    root, home, declare = project_cli
    declare({})
    rc, output = observe(['mcp', *command, '--project', str(root)], capsys)
    assert rc in (0, 1), repr(output)
    if command == ['local-target', 'add']:
        assert rc == 1 and 'approval' in output
    else:
        assert rc == 0, repr(output)
    safe(output)
    declare({'docs': {'server': {'transport': 'http', 'url': 'https://example.invalid', 'bad-\u202eFORGED\u202c': True}}})
    before = (root / 'Skillfile.json').read_bytes()
    rc, output = observe(['mcp', *command, '--project', str(root)], capsys)
    assert rc == 1 and 'mcp_declaration_invalid' in output, repr(output)
    assert (root / 'Skillfile.json').read_bytes() == before
    safe(output)


# A real missing-SKILL.md error carries a bidi-bearing source path from the package.
# The valid package is installed first; its successor triggers actual validation failure.
@pytest.mark.parametrize('command', [['install', 'app'], ['upgrade', 'app'], ['global', 'install'], ['global', 'upgrade']])
def test_validation_path(tmp_path, skills_root, csk_home, monkeypatch, capsys, command):
    cfg, project, root = setup(tmp_path, skills_root, csk_home, monkeypatch)
    source = 'repo-\u202eFORGED\u202c'
    repo, _ = make_skill_repo(skills_root, source, tag='v1')
    declaration = {'schema_version': 1, 'skills': [{'name': 'demo', 'source': source, 'tag': 'v1'}]}
    for directory in (root, project):
        write_skillfile(directory, declaration)
    monkeypatch.setattr(git_ops, 'fetch_repo', lambda *a, **kw: None)
    rc, output = observe(command, capsys)
    assert rc == 0, repr(output)
    safe(output)
    (repo / 'SKILL.md').unlink()
    (repo / 'README.md').write_text('missing required document\n')
    head = commit_all(repo)
    declaration['skills'][0].pop('tag')
    declaration['skills'][0]['revision'] = head
    for directory in (root, project):
        write_skillfile(directory, declaration)
    rc, output = observe(command, capsys)
    assert rc == 1 and 'Required SKILL.md not found' in output, (rc, repr(output))
    safe(output)


# The three previously omitted Bidi_Control marks escape in display and shell words.
@pytest.mark.parametrize('mark', ['\u061c', '\u200e', '\u200f'])
def test_bidi_marks(mark):
    from csk.terminal import terminal_text, shell_word
    assert mark not in terminal_text('x' + mark)
    assert mark not in shell_word('x' + mark)
