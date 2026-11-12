"""Whole-root ``csk update`` over existing clones: R3 review regressions.

The B1, B2, N2 and N3 cases reproduce the R3 review probes; every one except
the marked positive controls fails on R3 (e44f9a3). The per-clone refusal
cases pin this revision's contract: a refusal fails only its own clone. Only
the CLI and repository state are used, so the file runs against any revision.
"""
from dataclasses import replace
import json
import shutil
import sys

import pytest
from conftest import commit_all, make_config, make_project, make_skill_repo, run, write_files, write_skillfile
from test_git_origin_reconcile import redirecting_repository, _repo_bytes
from csk import cli, git_ops

INFO_REFS = '/repo.git/info/refs?service=git-upload-pack'


def _register(tmp_path, csk_home, skills_root, monkeypatch, skills, **overrides):
    project = make_project(tmp_path)
    write_skillfile(project, {'schema_version': 1, 'skills': skills})
    cfg = replace(make_config(csk_home, skills_root, project, agents=['codex_cli']), **overrides)
    monkeypatch.setattr(cli.config, 'load_config', lambda: cfg)
    return project, cfg


def _publish_v3(tmp_path):
    """Advance the served repository past every tag the clones already have."""
    source = tmp_path / 'source'
    write_files(source, {'SKILL.md': '---\nname: fixture-skill\n---\n\n# three\n'})
    commit = commit_all(source)
    run(['git', 'tag', 'v3'], source)
    remote = tmp_path / 'served/repo.git'
    run(['git', 'push', str(remote), 'main', '--tags'], source)
    run(['git', 'update-server-info'], remote)
    return commit


def _spec_requiring(name, url, tag):
    return json.dumps({
        'schema_version': 4, 'capabilities': {'exec': 'none', 'network': 'none'},
        'dependencies': {'skills': {name: {'git': url, 'ref': {'kind': 'tag', 'value': tag}, 'mode': 'context'}}},
    })


def _assert_reconciled(rc, output, repo, declared, commit, requests):
    assert rc == cli.EXIT_OK, output.err
    assert requests.count(INFO_REFS) == 1 and all(path.startswith('/repo.git/') for path in requests)
    assert run(['git', 'config', '--get', 'remote.origin.url'], repo).stdout.strip() == declared
    assert git_ops.resolve_ref(repo, 'tag', 'v2').commit == commit


# B1: declared sources without a clone are not fetch candidates. Update skips
# them with a notice, never clones them, and still reconciles the present
# clone. "control" (nothing absent) and "consumer-control" (a cached consumer
# spec without dependencies) are the positive controls of the same setup.
@pytest.mark.parametrize('extra', [
    'control', 'uninstalled-git', 'uninstalled-urlless', 'uninstalled-nested',
    'consumer-control', 'uncloned-dependency',
])
def test_update_skips_declarations_without_clone_and_fetches_the_rest(
    redirecting_repository, tmp_path, csk_home, skills_root, monkeypatch, capsys, extra,
):
    repo, _, declared, commit, requests = redirecting_repository
    skills = [{'name': 'fixture-skill', 'git': declared, 'tag': 'v2'}]
    absent = {
        'uninstalled-git': ('newskill', {'name': 'newskill', 'git': 'https://example.test/newskill.git', 'tag': 'v1'}),
        'uninstalled-urlless': ('localonly', {'name': 'localonly', 'tag': 'v1'}),
        'uninstalled-nested': ('team/nested', {
            'name': 'nested', 'source': 'team/nested', 'git': 'https://example.test/nested.git', 'tag': 'v1',
        }),
    }.get(extra)
    if absent is not None:
        skills.append(absent[1])
    if extra in ('consumer-control', 'uncloned-dependency'):
        files = {}
        if extra == 'uncloned-dependency':
            files['csk-skill.json'] = _spec_requiring('dep', 'https://example.test/dep.git', 'v1')
            absent = ('dep', None)
        make_skill_repo(skills_root, 'consumer', files, tag='v1')
        skills.append({'name': 'consumer', 'tag': 'v1'})
    _register(tmp_path, csk_home, skills_root, monkeypatch, skills)
    rc = cli.main(['update'])
    output = capsys.readouterr()
    _assert_reconciled(rc, output, repo, declared, commit, requests)
    assert 'fetched fixture-skill' in output.out
    if absent is not None:
        assert f'skipped {absent[0]}: no clone under skills_root' in output.out
        assert not (skills_root / absent[0]).exists(), 'update must not acquire missing sources'
    else:
        assert 'skipped' not in output.out


# B1, permanent case: a nested project source that another skill also
# requires by name unifies with the project node, so update fetches
# team/fixture-skill and never demands skills_root/fixture-skill. The
# preceding upgrade is the in-test control that the layout is valid.
def test_update_resolves_requirement_to_the_unified_nested_source(
    redirecting_repository, tmp_path, csk_home, skills_root, monkeypatch, capsys,
):
    repo, _, declared, _, requests = redirecting_repository
    nested = skills_root / 'team/fixture-skill'
    nested.parent.mkdir()
    repo.rename(nested)
    make_skill_repo(skills_root, 'consumer', {'csk-skill.json': _spec_requiring('fixture-skill', declared, 'v2')}, tag='v1')
    project, _ = _register(tmp_path, csk_home, skills_root, monkeypatch, [
        {'name': 'fixture-skill', 'source': 'team/fixture-skill', 'git': declared, 'tag': 'v2'},
        {'name': 'consumer', 'tag': 'v1'},
    ])
    assert cli.main(['upgrade', 'app']) == cli.EXIT_OK, capsys.readouterr().err
    assert not (skills_root / 'fixture-skill').exists()
    newest = _publish_v3(tmp_path)
    requests.clear()
    rc = cli.main(['update'])
    output = capsys.readouterr()
    assert rc == cli.EXIT_OK, output.err
    assert 'fetched team/fixture-skill' in output.out and 'skipped' not in output.out
    assert requests.count(INFO_REFS) == 1
    assert git_ops.resolve_ref(nested, 'tag', 'v3').commit == newest
    assert not (skills_root / 'fixture-skill').exists()


# B2: a symlinked clone in skills_root is updated like install and upgrade use
# it: undeclared through its stored origin, declared through the reconciled
# endpoint. The upgrade lane is the control for the same symlink.
@pytest.mark.parametrize('lane', ['update-undeclared', 'update-declared', 'upgrade-declared'])
def test_symlinked_clone_follows_the_upgrade_policy(
    redirecting_repository, tmp_path, csk_home, skills_root, monkeypatch, capsys, lane,
):
    repo, _, declared, commit, requests = redirecting_repository
    target = tmp_path / 'elsewhere/fixture-skill'
    target.parent.mkdir()
    repo.rename(target)
    (skills_root / 'fixture-skill').symlink_to(target, target_is_directory=True)
    if lane == 'update-undeclared':
        run(['git', 'remote', 'set-url', 'origin', declared], target)
        _register(tmp_path, csk_home, skills_root, monkeypatch, [])
    else:
        _register(tmp_path, csk_home, skills_root, monkeypatch, [{'name': 'fixture-skill', 'git': declared, 'tag': 'v2'}])
    rc = cli.main(['upgrade', 'app'] if lane.startswith('upgrade') else ['update'])
    output = capsys.readouterr()
    _assert_reconciled(rc, output, target, declared, commit, requests)
    if lane.startswith('update'):
        assert 'fetched fixture-skill' in output.out
    assert (skills_root / 'fixture-skill').is_symlink()


# B2 boundary: following a symlink is no weaker than upgrade. The same policy
# and identity checks refuse the linked clone without traffic or mutation,
# while an unrelated healthy clone is still fetched.
@pytest.mark.parametrize('case', ['changed-identity', 'denied-stored-origin'])
def test_symlinked_clone_keeps_identity_and_policy_refusals(
    redirecting_repository, tmp_path, csk_home, skills_root, monkeypatch, capsys, case,
):
    repo, _, declared, commit, requests = redirecting_repository
    linked = tmp_path / 'elsewhere/linked'
    linked.parent.mkdir()
    shutil.copytree(repo, linked)
    (skills_root / 'linked').symlink_to(linked, target_is_directory=True)
    effective = git_ops.git(repo, ['ls-remote', '--get-url', '--', declared]).stdout.strip()
    skills = [{'name': 'fixture-skill', 'git': declared, 'tag': 'v2'}]
    overrides = {}
    if case == 'changed-identity':
        skills.append({'name': 'linked', 'git': 'https://other.test/intended.git', 'tag': 'v2'})
    else:
        denied = 'https://denied.test/repo.git'
        run(['git', 'remote', 'set-url', 'origin', denied], linked)
        # Even a faulty implementation could reach only loopback, never denied.test.
        run(['git', 'config', f'url.{effective}.insteadOf', denied], linked)
        overrides['allowed_sources'] = ('example.test',)
    _register(tmp_path, csk_home, skills_root, monkeypatch, skills, **overrides)
    before = _repo_bytes(linked)
    rc = cli.main(['update'])
    output = capsys.readouterr()
    assert rc == cli.EXIT_PARTIAL_FAIL
    reason = 'do not identify the same repository' if case == 'changed-identity' else 'Source not allowed'
    assert 'update refused linked:' in output.err and reason in output.err
    assert _repo_bytes(linked) == before
    assert requests.count(INFO_REFS) == 1, 'only the healthy clone may be fetched'
    assert git_ops.resolve_ref(skills_root / 'fixture-skill', 'tag', 'v2').commit == commit


# Decision: a reconcile or policy refusal fails that clone only. It is named
# on stderr, gets no traffic and keeps its bytes; the unrelated healthy clone
# is still reconciled, and the command exits nonzero. N1: a stored origin with
# HTTP(S) userinfo is one such per-clone refusal and is never echoed. In the
# conflict case 'other' is a different repository (example.test/other) whose
# two projects disagree on its spelling; the canonical-URL rule refuses that
# repository only.
@pytest.mark.parametrize('case', ['identity', 'policy', 'conflict', 'stored-userinfo'])
def test_refusal_fails_only_that_clone(
    redirecting_repository, tmp_path, csk_home, skills_root, monkeypatch, capsys, case,
):
    repo, old, declared, commit, requests = redirecting_repository
    other = skills_root / 'other'
    shutil.copytree(repo, other)
    effective = git_ops.git(repo, ['ls-remote', '--get-url', '--', declared]).stdout.strip()
    skills = [{'name': 'fixture-skill', 'git': declared, 'tag': 'v2'}]
    overrides = {}
    if case == 'identity':
        skills.append({'name': 'other', 'git': 'https://other.test/intended.git', 'tag': 'v2'})
    elif case == 'policy':
        denied = 'https://denied.test/repo.git'
        run(['git', 'remote', 'set-url', 'origin', denied], other)
        run(['git', 'config', f'url.{effective}.insteadOf', denied], other)
        overrides['allowed_sources'] = ('example.test',)
    elif case == 'conflict':
        run(['git', 'remote', 'set-url', 'origin', 'https://example.test/other'], other)
        second = make_project(tmp_path, name='second')
        write_skillfile(second, {'schema_version': 1, 'skills': [
            {'name': 'other', 'git': 'https://example.test/other', 'tag': 'v2'},
        ]})
        skills.append({'name': 'other', 'git': 'https://example.test/other.git', 'tag': 'v2'})
    else:
        stored = 'https://SYNTHETIC_USER@example.test/repo.git'
        run(['git', 'remote', 'set-url', 'origin', stored], other)
        run(['git', 'config', f'url.{effective}.insteadOf', stored], other)
    _, cfg = _register(tmp_path, csk_home, skills_root, monkeypatch, skills, **overrides)
    if case == 'conflict':
        cfg = replace(cfg, projects={**cfg.projects, 'second': replace(cfg.projects['app'], alias='second', path=second)})
        monkeypatch.setattr(cli.config, 'load_config', lambda: cfg)
    before = _repo_bytes(other)
    rc = cli.main(['update'])
    output = capsys.readouterr()
    reason = {
        'identity': 'do not identify the same repository', 'policy': 'Source not allowed',
        'conflict': 'is declared with 2 different URL spellings', 'stored-userinfo': 'must not contain userinfo',
    }[case]
    assert rc == cli.EXIT_PARTIAL_FAIL
    assert 'update refused other:' in output.err and reason in output.err
    assert 'SYNTHETIC_USER' not in output.out + output.err
    assert _repo_bytes(other) == before
    assert not any(path.startswith('/repo/') for path in requests)
    assert requests.count(INFO_REFS) == 1, 'the refused clone must not be fetched'
    assert 'fetched fixture-skill' in output.out
    assert git_ops.resolve_ref(repo, 'tag', 'v2').commit == commit


# N2: relative local declarations resolve against the project directory in
# every lane, so the invocation directory cannot change what update fetches
# or what install clones. A different relative repository is still refused.
@pytest.mark.parametrize('case', ['update-elsewhere', 'upgrade-elsewhere', 'install-elsewhere', 'other-repository'])
def test_relative_local_declaration_is_project_relative(
    tmp_path, csk_home, skills_root, monkeypatch, capsys, case,
):
    upstream, _ = make_skill_repo(tmp_path, 'upstream', tag='v1')
    make_skill_repo(tmp_path, 'unrelated', tag='v1')
    elsewhere = tmp_path / 'a/b'
    elsewhere.mkdir(parents=True)
    project, _ = _register(tmp_path, csk_home, skills_root, monkeypatch, [
        {'name': 'local-skill', 'git': '../upstream', 'tag': 'v1'},
    ])
    clone = skills_root / 'local-skill'
    monkeypatch.chdir(elsewhere if case == 'install-elsewhere' else project)
    assert cli.main(['install', 'app']) == cli.EXIT_OK, capsys.readouterr().err
    stored = run(['git', 'config', '--get', 'remote.origin.url'], clone).stdout.strip()
    assert (clone / stored).resolve() == upstream.resolve()
    write_files(upstream, {'SKILL.md': '---\nname: test\n---\n\n# v2\n'})
    newest = commit_all(upstream)
    run(['git', 'tag', 'v2'], upstream)
    if case == 'other-repository':
        write_skillfile(project, {'schema_version': 1, 'skills': [
            {'name': 'local-skill', 'git': '../unrelated', 'tag': 'v1'},
        ]})
    monkeypatch.chdir(elsewhere)
    before = _repo_bytes(clone)
    rc = cli.main(['upgrade', 'app'] if case == 'upgrade-elsewhere' else ['update'])
    output = capsys.readouterr()
    if case == 'other-repository':
        assert rc == cli.EXIT_PARTIAL_FAIL
        assert 'update refused local-skill:' in output.err and 'do not identify the same repository' in output.err
        assert _repo_bytes(clone) == before
    else:
        assert rc == cli.EXIT_OK, output.err
        assert git_ops.resolve_ref(clone, 'tag', 'v2').commit == newest


# N3: an undeclared clone whose directory name is not portable is still
# fetched through its stored origin, and the portable neighbour is fetched
# too. POSIX only: Windows cannot create these names.
@pytest.mark.skipif(sys.platform == 'win32', reason='names are not creatable on Windows')
@pytest.mark.parametrize('name', ['aux.old', 'trailing.', 'colon:name'])
def test_non_portable_clone_name_does_not_abort_update(tmp_path, csk_home, skills_root, monkeypatch, capsys, name):
    clones = {}
    for label in (name, 'healthy'):
        upstream, _ = make_skill_repo(tmp_path / 'upstreams', label, tag='v1')
        clones[label] = (upstream, skills_root / label)
        run(['git', 'clone', str(upstream), str(skills_root / label)], tmp_path)
        write_files(upstream, {'SKILL.md': '---\nname: test\n---\n\n# v2\n'})
        commit_all(upstream)
        run(['git', 'tag', 'v2'], upstream)
    _register(tmp_path, csk_home, skills_root, monkeypatch, [])
    rc = cli.main(['update'])
    output = capsys.readouterr()
    assert rc == cli.EXIT_OK, output.err
    for label, (upstream, clone) in clones.items():
        assert f'fetched {label}' in output.out
        assert git_ops.resolve_ref(clone, 'tag', 'v2').commit == git_ops.resolve_ref(upstream, 'tag', 'v2').commit


# An unreadable dependency spec at the cached declared ref refuses only that
# clone, by name and without traffic; the healthy clone is still reconciled.
# When the declared ref is not cached yet, cached HEAD is inspected best
# effort only, so its unreadable spec does not block fetching the new ref.
# "valid-control" is the same layout with a readable spec.
@pytest.mark.parametrize('case', ['declared-ref', 'head-fallback', 'valid-control'])
def test_unreadable_dependency_spec_refuses_only_its_clone(
    redirecting_repository, tmp_path, csk_home, skills_root, monkeypatch, capsys, case,
):
    repo, _, declared, commit, requests = redirecting_repository
    spec = _spec_requiring('fixture-skill', declared, 'v2') if case == 'valid-control' else '{'
    upstream, _ = make_skill_repo(tmp_path / 'upstreams', 'consumer', {'csk-skill.json': spec}, tag='v1')
    consumer = skills_root / 'consumer'
    run(['git', 'clone', str(upstream), str(consumer)], tmp_path)
    write_files(upstream, {'SKILL.md': '---\nname: test\n---\n\n# v9\n'})
    newest = commit_all(upstream)
    run(['git', 'tag', 'v9'], upstream)
    _register(tmp_path, csk_home, skills_root, monkeypatch, [
        {'name': 'fixture-skill', 'git': declared, 'tag': 'v2'},
        {'name': 'consumer', 'tag': 'v9' if case == 'head-fallback' else 'v1'},
    ])
    before = _repo_bytes(consumer)
    rc = cli.main(['update'])
    output = capsys.readouterr()
    assert requests.count(INFO_REFS) == 1
    assert git_ops.resolve_ref(repo, 'tag', 'v2').commit == commit
    if case == 'declared-ref':
        assert rc == cli.EXIT_PARTIAL_FAIL
        assert 'update refused consumer: cannot read its dependency declarations at cached commit' in output.err
        assert _repo_bytes(consumer) == before
    else:
        assert rc == cli.EXIT_OK, output.err
        assert 'fetched consumer' in output.out
        assert git_ops.resolve_ref(consumer, 'tag', 'v9').commit == newest
