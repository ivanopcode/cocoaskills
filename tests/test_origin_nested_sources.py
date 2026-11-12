"""Nested source regressions retained from independent origin reconciliation review."""
from conftest import make_config, make_project, run, write_skillfile
from test_git_origin_reconcile import redirecting_repository, _repo_bytes
from csk import cli, git_ops, source_update
import pytest


@pytest.mark.parametrize(('nested', 'command'), [(False, 'update'), (True, 'upgrade'), (True, 'update')])
def test_bulk_update_reconciles_supported_nested_source(
    redirecting_repository, tmp_path, csk_home, skills_root, monkeypatch, capsys, nested, command,
):
    # A supported nested legacy source must receive its declared endpoint during
    # bulk update. Top-level update and nested upgrade are independent positive controls.
    repo, old, declared, commit, requests = redirecting_repository
    source = 'internal/fixture-skill' if nested else 'fixture-skill'
    if nested:
        destination = skills_root / source
        destination.parent.mkdir()
        repo.rename(destination)
        repo = destination
    project = make_project(tmp_path)
    write_skillfile(project, {'schema_version': 1, 'skills': [
        {'name': 'fixture-skill', 'source': source, 'git': declared, 'tag': 'v2'},
    ]})
    cfg = make_config(csk_home, skills_root, project, agents=['codex_cli'])
    monkeypatch.setattr(cli.config, 'load_config', lambda: cfg)
    planned = {item.source: item for item in source_update.plan_update(cfg)}
    assert planned[source].action == 'fetch' and planned[source].endpoint == declared
    assert requests == [], 'planning must not fetch'
    rc = cli.main([command, 'app'] if command == 'upgrade' else [command])
    output = capsys.readouterr()
    stored = run(['git', 'config', '--get', 'remote.origin.url'], repo).stdout.strip()
    assert rc == cli.EXIT_OK, output.err
    assert requests and all(path.startswith('/repo.git/') for path in requests), 'declared source was silently omitted from bulk fetch'
    assert stored == declared
    assert git_ops.resolve_ref(repo, 'tag', 'v2').commit == commit


@pytest.mark.parametrize('source', ['internal/team/fixture-skill', 'internal group/fixture skill'])
@pytest.mark.parametrize('with_url', [False, True])
@pytest.mark.parametrize('allowed', [False, True])
def test_nested_sources_preflight_policy_and_preserve_portable_paths(
    redirecting_repository, tmp_path, csk_home, skills_root, monkeypatch, capsys,
    source, with_url, allowed,
):
    # Full portable paths (including spaces accepted by the manifest) survive
    # selection with/without a URL. A denied nested source is refused by name
    # without network or clone mutation; the allowed top-level clone still fetches.
    from dataclasses import replace
    import shutil
    repo, _, declared, commit, requests = redirecting_repository
    run(['git', 'remote', 'set-url', 'origin', declared], repo)
    nested = skills_root / source
    nested.parent.mkdir(parents=True)
    shutil.copytree(repo, nested)
    nested_url = declared if allowed else 'https://denied.test/repo.git'
    run(['git', 'remote', 'set-url', 'origin', nested_url], nested)
    effective = git_ops.git(repo, ['ls-remote', '--get-url', '--', declared]).stdout.strip()
    run(['git', 'config', f'url.{effective}.insteadOf', nested_url], nested)
    project = make_project(tmp_path)
    declaration = {'name': 'nested', 'source': source, 'tag': 'v2'}
    if with_url:
        declaration['git'] = nested_url
    write_skillfile(project, {'schema_version': 1, 'skills': [declaration]})
    cfg = replace(make_config(csk_home, skills_root, project), allowed_sources=('example.test',))
    monkeypatch.setattr(cli.config, 'load_config', lambda: cfg)
    before = (_repo_bytes(repo), _repo_bytes(nested))
    rc = cli.main(['update'])
    output = capsys.readouterr()
    if allowed:
        assert rc == cli.EXIT_OK, output.err
        assert requests.count('/repo.git/info/refs?service=git-upload-pack') == 2
        assert git_ops.resolve_ref(nested, 'tag', 'v2').commit == commit
    else:
        assert rc == cli.EXIT_PARTIAL_FAIL
        assert f'update refused {source}:' in output.err and 'Source not allowed' in output.err
        assert requests.count('/repo.git/info/refs?service=git-upload-pack') == 1
        assert git_ops.resolve_ref(repo, 'tag', 'v2').commit == commit
        assert _repo_bytes(nested) == before[1]


@pytest.mark.parametrize('layout', ['plain', 'parent-link', 'leaf-link', 'metadata-link', 'missing'])
def test_declared_source_paths_follow_links_like_upgrade(
    redirecting_repository, tmp_path, csk_home, skills_root, monkeypatch, capsys, layout,
):
    # Declared nested paths reach a linked clone the way install and upgrade
    # reach it, and reconcile it; a missing clone is skipped by name, never
    # cloned. "plain" (no link) is the positive control of the same fixture.
    import shutil
    repo, _, declared, commit, requests = redirecting_repository
    outside = tmp_path / 'outside'
    shutil.copytree(repo, outside)
    shutil.rmtree(repo)
    source = 'internal/fixture-skill'
    if layout == 'plain':
        shutil.copytree(outside, skills_root / source)
    elif layout == 'parent-link':
        (skills_root / 'internal').symlink_to(tmp_path, target_is_directory=True)
        source = 'internal/outside'
    elif layout == 'leaf-link':
        (skills_root / 'internal').mkdir()
        (skills_root / source).symlink_to(outside, target_is_directory=True)
    elif layout == 'metadata-link':
        target = skills_root / source
        target.mkdir(parents=True)
        (target / '.git').symlink_to(outside / '.git', target_is_directory=True)
    project = make_project(tmp_path)
    write_skillfile(project, {'schema_version': 1, 'skills': [
        {'name': 'nested', 'source': source, 'git': declared, 'tag': 'v2'},
    ]})
    cfg = make_config(csk_home, skills_root, project)
    monkeypatch.setattr(cli.config, 'load_config', lambda: cfg)
    before = _repo_bytes(outside)
    rc = cli.main(['update'])
    output = capsys.readouterr()
    assert rc == cli.EXIT_OK, output.err
    if layout == 'missing':
        assert f'skipped {source}: no clone under skills_root' in output.out
        assert requests == []
        assert _repo_bytes(outside) == before
        assert not (skills_root / source).exists()
        return
    assert f'fetched {source}' in output.out
    assert requests.count('/repo.git/info/refs?service=git-upload-pack') == 1
    clone = skills_root / source
    assert git_ops.resolve_ref(clone, 'tag', 'v2').commit == commit
    assert run(['git', 'config', '--get', 'remote.origin.url'], clone).stdout.strip() == declared


@pytest.mark.parametrize('declare_embedded', [False, True])
def test_bulk_selection_does_not_crawl_unrelated_or_repository_contents(
    redirecting_repository, tmp_path, csk_home, skills_root, monkeypatch, capsys, declare_embedded,
):
    # Updating a declared clone must not discover repos through an undeclared
    # namespace (including a link inside it) or another repo's working tree. An
    # explicitly declared embedded repository is authorized by its full path,
    # without a recursive scan. Top-level links are clones in their own right
    # (test_symlinked_clone_follows_the_upgrade_policy).
    import shutil
    repo, _, declared, commit, requests = redirecting_repository
    unrelated = skills_root / 'unrelated/hidden'
    unrelated.parent.mkdir()
    shutil.copytree(repo, unrelated)
    embedded = repo / 'embedded'
    shutil.copytree(repo, tmp_path / 'copy')
    shutil.copytree(tmp_path / 'copy', embedded)
    outside = tmp_path / 'outside'
    shutil.copytree(tmp_path / 'copy', outside)
    (skills_root / 'unrelated/shortcut').symlink_to(outside, target_is_directory=True)
    project = make_project(tmp_path)
    declarations = [
        {'name': 'fixture-skill', 'git': declared, 'tag': 'v2'},
    ]
    if declare_embedded:
        declarations.append({'name': 'embedded', 'source': 'fixture-skill/embedded', 'git': declared, 'tag': 'v2'})
    write_skillfile(project, {'schema_version': 1, 'skills': declarations})
    cfg = make_config(csk_home, skills_root, project)
    monkeypatch.setattr(cli.config, 'load_config', lambda: cfg)
    before = [_repo_bytes(path) for path in (unrelated, outside)]
    embedded_before = _repo_bytes(embedded)
    assert cli.main(['update']) == cli.EXIT_OK, capsys.readouterr().err
    assert requests.count('/repo.git/info/refs?service=git-upload-pack') == (2 if declare_embedded else 1)
    assert all(path.startswith('/repo.git/') for path in requests)
    assert git_ops.resolve_ref(repo, 'tag', 'v2').commit == commit
    assert [_repo_bytes(path) for path in (unrelated, outside)] == before
    if declare_embedded:
        assert git_ops.resolve_ref(embedded, 'tag', 'v2').commit == commit
    else:
        assert _repo_bytes(embedded) == embedded_before


@pytest.mark.parametrize('source', ['../outside', '/outside', 'a/../b', 'a//b', 'a\\b', '.git/objects', 'a/.GIT'])
def test_bulk_source_path_guard_rejects_unsafe_paths_before_git(tmp_path, monkeypatch, source):
    # The shared source-path guard validates raw syntax before joining it or
    # calling Git, even without the CLI manifest parser. Valid nested and
    # spaced paths are the adjacent controls.
    def forbidden(*args, **kwargs):
        pytest.fail('Git called for an unsafe source path')
    monkeypatch.setattr(git_ops, '_run', forbidden)
    with pytest.raises(git_ops.GitError, match='portable relative path|Git metadata'):
        git_ops.skill_source_path(tmp_path, source)
    for valid in ('internal/fixture-skill', 'internal group/fixture skill'):
        assert git_ops.skill_source_path(tmp_path, valid) == tmp_path.joinpath(*valid.split('/'))


@pytest.mark.parametrize('selection', ['distinct', 'shared', 'conflict'])
def test_full_source_keys_disambiguate_basename_and_preflight_conflicts(
    redirecting_repository, tmp_path, csk_home, skills_root, monkeypatch, capsys, selection,
):
    # Equal basenames in distinct declared paths each fetch; a shared path fetches
    # once. Contradictory endpoints for that shared path refuse it by name, before any fetch.
    import shutil
    repo, old, declared, commit, requests = redirecting_repository
    first = skills_root / 'a/fixture-skill'
    first.parent.mkdir()
    repo.rename(first)
    second = skills_root / 'b/fixture-skill'
    second.parent.mkdir()
    shutil.copytree(first, second)
    second_source = 'b/fixture-skill' if selection == 'distinct' else 'a/fixture-skill'
    project = make_project(tmp_path)
    write_skillfile(project, {'schema_version': 1, 'skills': [
        {'name': 'first', 'source': 'a/fixture-skill', 'git': declared, 'tag': 'v2'},
        {'name': 'second', 'source': second_source,
         'git': old if selection == 'conflict' else declared, 'tag': 'v2'},
    ]})
    cfg = make_config(csk_home, skills_root, project)
    monkeypatch.setattr(cli.config, 'load_config', lambda: cfg)
    before = (_repo_bytes(first), _repo_bytes(second))
    rc = cli.main(['update'])
    output = capsys.readouterr()
    if selection == 'conflict':
        assert rc == cli.EXIT_PARTIAL_FAIL
        assert 'update refused a/fixture-skill: repository example.test/repo is declared with 2 different URL spellings' in output.err
        assert requests == []
        assert (_repo_bytes(first), _repo_bytes(second)) == before
    else:
        assert rc == cli.EXIT_OK, output.err
        assert requests.count('/repo.git/info/refs?service=git-upload-pack') == (2 if selection == 'distinct' else 1)
        assert git_ops.resolve_ref(first, 'tag', 'v2').commit == commit
        if selection == 'distinct':
            assert git_ops.resolve_ref(second, 'tag', 'v2').commit == commit
        else:
            assert _repo_bytes(second) == before[1]
