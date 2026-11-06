import json
import sys
import unicodedata

import pytest

from csk import cli, config, git_ops
from csk.terminal import terminal_text
from conftest import make_config, make_project, make_skill_repo, write_skillfile


# Exhaustive Unicode category coverage includes all Bidi_Control characters;
# format characters except legitimate joiners are escaped; multilingual text survives.
def test_unicode_control_categories():
    for code in range(sys.maxunicode + 1):
        char = chr(code)
        if unicodedata.category(char) in {'Cc', 'Cf'} and char not in '\u200c\u200d':
            assert char not in terminal_text(char), hex(code)
    assert terminal_text('日本語 café العربية می\u200cروم 👩\u200d💻') == '日本語 café العربية می\u200cروم 👩\u200d💻'


def assert_safe(output):
    assert all(c == '\n' or unicodedata.category(c) not in {'Cc', 'Cf', 'Zl', 'Zp'} for c in output), repr(output)
    assert '\nFORGED' not in output


# Real CLI dispatch with manifest/Git input plants: no renderer/result mocking.
# Missing sources exercise diagnostics; the ordinary installed package is a positive control.
@pytest.mark.parametrize('plant', ['\x1b[31m\nFORGED', '\u061c\u200e\u200f\u202e\u202c\u200b', 'café'])
def test_cli_boundaries(tmp_path, skills_root, csk_home, monkeypatch, capsys, plant):
    project = make_project(tmp_path)
    cfg = make_config(csk_home, skills_root, project)
    config.save_config(cfg)
    monkeypatch.setenv('CSK_CONFIG', str(cfg.path))
    root = csk_home / 'global'
    root.mkdir()
    make_skill_repo(skills_root, 'demo', tag='v1')
    valid = {'schema_version': 1, 'skills': [{'name': 'demo', 'tag': 'v1'}]}
    for directory in (root, project):
        write_skillfile(directory, valid)
    for args in (['install', 'app'], ['global', 'install'], ['status', 'app']):
        assert cli.main(args) == 0
        captured = capsys.readouterr()
        assert_safe(captured.out + captured.err)

    def fetch(*args, **kwargs):
        raise git_ops.GitError('remote: ' + plant)
    with monkeypatch.context() as patch:
        patch.setattr(git_ops, 'fetch_repo', fetch)
        assert cli.main(['global', 'update']) == 1
        captured = capsys.readouterr()
        assert 'fetch failed demo' in captured.err
        assert terminal_text(plant) in captured.err
        assert_safe(captured.out + captured.err)

    # JSON string controls stay semantic on disk; display keeps document indentation.
    document = json.loads(cfg.path.read_text())
    document['skills_root'] = str(skills_root) + plant
    cfg.path.write_text(json.dumps(document, ensure_ascii=False, indent=2) + '\n')
    assert cli.main(['config', 'show']) == 0
    captured = capsys.readouterr()
    assert '\n  "' in captured.out
    assert_safe(captured.out + captured.err)
    config.save_config(cfg)

    hostile = {'schema_version': 1, 'skills': [{'name': 'demo', 'source': 'missing-' + plant, 'tag': 'v1'}]}
    for directory in (root, project):
        write_skillfile(directory, hostile)
    for args in (['install', 'app'], ['global', 'install'], ['status', 'app', '--check'], ['audit', 'app']):
        assert cli.main(args) != 0
        captured = capsys.readouterr()
        expected = "field 'source' must be a portable relative path" if '\n' in plant else 'repository not found'
        assert expected in (captured.out + captured.err).lower()
        assert_safe(captured.out + captured.err)
