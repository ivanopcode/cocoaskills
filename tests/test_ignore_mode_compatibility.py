"""Legacy field precedence stays consistent across the public project commands."""
from __future__ import annotations

import json

import pytest

from conftest import make_config, make_project, write_skillfile
from csk import adapters, cli, config, dev_substitutions, gitignore_gate, manifest


# Each legacy field wins with/without a managed block; clean installs and explicit migration remain valid controls.
@pytest.mark.parametrize('schema', [2, 3])
@pytest.mark.parametrize('declared', ['exclude', 'gitignore'])
@pytest.mark.parametrize('block', [False, True])
def test_legacy_effective_mode_across_commands(tmp_path, monkeypatch, capsys, schema, declared, block):
    root = make_project(tmp_path, gitignore=False)
    home = tmp_path / 'manager'
    skills = tmp_path / 'skills'
    skills.mkdir()
    cfg = make_config(home, skills, root)
    config.save_config(cfg)
    monkeypatch.setenv('CSK_CONFIG', str(cfg.path))
    monkeypatch.chdir(root)
    document = {'schema_version': schema, 'skills': []}
    write_skillfile(root, document)
    ignore = root / '.gitignore'
    ignore.write_bytes(b'# user policy\r\n')
    entries = adapters.all_gitignore_entries() + [dev_substitutions.DEV_MANIFEST_NAME]
    if block:
        gitignore_gate.append_entries(ignore, entries)

    def command(*args):
        code = cli.main(list(args))
        return code, capsys.readouterr()

    # The same real repository installs and has clean status without a legacy field.
    code, output = command('ignore-mode', 'show', '--json')
    assert code == 0, output
    assert json.loads(output.out)['mode'] == ('gitignore' if block else 'exclude')
    code, output = command('install', 'app')
    assert code == 0, output
    code, output = command('status', 'app', '--check')
    assert code == 0, output

    document['ignore_mode'] = declared
    write_skillfile(root, document)
    # The changed fixture document needs a fresh lock, independent of ignore policy.
    (root / 'Skillfile.lock.json').unlink()
    exclude = gitignore_gate.ignore_path(root, 'exclude')
    exclude.parent.mkdir(exist_ok=True)
    exclude.write_bytes(b'# local user policy\r\n')
    original = gitignore_gate.effective_mode
    calls = []

    def observe(project, field):
        calls.append((project, field))
        return original(project, field)

    monkeypatch.setattr(gitignore_gate, 'effective_mode', observe)

    def policy_bytes():
        return {path: path.read_bytes() if path.exists() else None
                for path in (ignore, exclude, root / 'Skillfile.json', root / 'Skillfile.lock.json')}

    before = policy_bytes()
    for json_output in (True, False):
        calls.clear()
        code, output = command('ignore-mode', 'show', *(['--json'] if json_output else []))
        assert code == 0, output
        if json_output:
            shown = json.loads(output.out)
            assert shown['mode'] == declared
            assert shown['source'] == 'deprecated_field'
            assert shown['deprecated_ignore_mode'] == declared
        else:
            assert f'Ignore mode: {declared} (deprecated field)' in output.out
        assert (root, declared) in calls
        assert output.err.count('not portable') == 1
        assert policy_bytes() == before

    # Check the original field/block combination before install can repair or publish anything.
    calls.clear()
    code, output = command('status', 'app')
    assert code == 0, output
    assert (root, declared) in calls
    assert 'Managed .gitignore block:' not in output.out
    assert policy_bytes() == before

    calls.clear()
    code, output = command('install', 'app')
    assert (root, declared) in calls
    if declared == 'gitignore' and not block:
        assert code == 1, output
        # Schema 2 renders the code on stdout; schema 3 renders the same gate's detail on stderr.
        assert 'Generated CocoaSkill paths are not ignored by git' in output.out + output.err
        assert '.agents/' in output.out + output.err
        assert policy_bytes() == before
        # Repairing the required legacy block permits the same install, with the field unchanged.
        gitignore_gate.append_entries(ignore, entries)
        code, output = command('install', 'app')
    assert code == 0, output
    assert manifest.load_manifest(root).ignore_mode == declared
    if declared == 'exclude':
        assert ignore.read_bytes() == before[ignore]
        assert b'# CocoaSkill project ' in exclude.read_bytes()
    else:
        assert exclude.read_bytes() == before[exclude]

    before = policy_bytes()
    calls.clear()
    code, output = command('status', 'app', '--check')
    assert code == 0, output
    assert (root, declared) in calls
    assert 'Managed .gitignore block:' not in output.out
    assert policy_bytes() == before

    # Explicit migration removes the override; fresh loading/show/status use only the requested local policy.
    target = 'gitignore' if declared == 'exclude' else 'exclude'
    code, output = command('ignore-mode', 'set', target, '--json')
    assert code == 0, output
    assert manifest.load_manifest(root).ignore_mode is None
    code, output = command('ignore-mode', 'show', '--json')
    assert code == 0, output
    shown = json.loads(output.out)
    assert shown['mode'] == target
    assert shown['source'] == 'block'
    assert shown['deprecated_ignore_mode'] is None
    # Removing the member changes the Skillfile digest; refresh it before checking lock drift.
    code, output = command('upgrade', 'app')
    assert code == 0, output
    calls.clear()
    code, output = command('status', 'app', '--check')
    assert code == 0, output
    assert (root, None) in calls


# With no legacy field or managed block, show names the default and remains read-only.
def test_show_default_source(tmp_path, monkeypatch, capsys):
    root = make_project(tmp_path, gitignore=False)
    write_skillfile(root, {'schema_version': 2, 'skills': []})
    monkeypatch.chdir(root)
    monkeypatch.setenv('CSK_CONFIG', str(tmp_path / 'manager/config.json'))
    before = (root / 'Skillfile.json').read_bytes()
    assert cli.main(['ignore-mode', 'show', '--json']) == 0
    assert json.loads(capsys.readouterr().out) == {
        'mode': 'exclude', 'source': 'default', 'deprecated_ignore_mode': None,
    }
    assert (root / 'Skillfile.json').read_bytes() == before
    assert not (root / '.gitignore').exists()


# Source reporting refuses links/nonregular excludes unchanged; a read-only owned block is a valid read control.
@pytest.mark.parametrize('bad', ['symlink', 'directory', 'parent_symlink'])
def test_show_exclude_source_is_project_owned_and_read_only(tmp_path, monkeypatch, capsys, bad):
    root = make_project(tmp_path, gitignore=False)
    write_skillfile(root, {'schema_version': 2, 'skills': []})
    monkeypatch.chdir(root)
    monkeypatch.setenv('CSK_CONFIG', str(tmp_path / 'manager/config.json'))
    exclude = gitignore_gate.ignore_path(root, 'exclude')
    exclude.parent.mkdir(exist_ok=True)
    other = b'# CocoaSkill project ' + b'0' * 64 + b'\n/other/\n# End CocoaSkill project ' + b'0' * 64 + b'\n'
    exclude.write_bytes(other)
    assert cli.main(['ignore-mode', 'show', '--json']) == 0
    assert json.loads(capsys.readouterr().out)['source'] == 'default'
    gitignore_gate.ensure_ignored(root, ['.agents/'], mode='exclude')
    original = exclude.read_bytes()
    target = tmp_path / 'user-data'
    target.write_bytes(original)
    exclude.unlink()
    parent_target = tmp_path / 'user-info'
    if bad == 'symlink':
        exclude.symlink_to(target)
    elif bad == 'parent_symlink':
        exclude.parent.rename(parent_target)
        exclude.parent.symlink_to(parent_target, target_is_directory=True)
    else:
        exclude.mkdir()
    assert cli.main(['ignore-mode', 'show', '--json']) == 2
    output = capsys.readouterr()
    assert 'git_exclude_invalid' in output.err
    assert output.out == ''
    assert target.read_bytes() == original
    if bad == 'symlink':
        assert exclude.is_symlink()
        exclude.unlink()
    elif bad == 'parent_symlink':
        assert exclude.parent.is_symlink()
        exclude.parent.unlink()
        parent_target.rename(exclude.parent)
    else:
        assert exclude.is_dir()
        exclude.rmdir()
    exclude.write_bytes(original)
    exclude.chmod(0o444)
    try:
        assert cli.main(['ignore-mode', 'show', '--json']) == 0
        shown = json.loads(capsys.readouterr().out)
        assert shown['mode'] == 'exclude' and shown['source'] == 'block'
        assert exclude.read_bytes() == original
        assert not (root / '.gitignore').exists()
    finally:
        exclude.chmod(0o644)
