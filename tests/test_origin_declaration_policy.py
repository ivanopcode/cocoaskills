"""Source-policy regressions retained from independent review and strengthened."""
from dataclasses import replace
import json

import pytest
from conftest import make_config, make_project, make_skill_repo, run, write_skillfile
from test_git_origin_reconcile import redirecting_repository, _repo_bytes
from csk import cli, git_ops


def _project(tmp_path, csk_home, skills_root, monkeypatch, url, tag='v1'):
    project = make_project(tmp_path)
    write_skillfile(project, {'schema_version': 1, 'skills': [
        {'name': 'fixture-skill', 'git': url, 'tag': tag},
    ]})
    cfg = make_config(csk_home, skills_root, project, agents=['codex_cli'])
    monkeypatch.setattr(cli.config, 'load_config', lambda: cfg)
    return project, cfg


@pytest.mark.parametrize('cached', [False, True])
def test_review_allowlist_reapplied_to_declared_url(
    redirecting_repository, tmp_path, csk_home, skills_root, monkeypatch, capsys, cached,
):
    # Both a new clone and a cached clone must reject a currently disallowed
    # declared source before HTTP. Existing successful upgrade is the positive control.
    repo, old, declared, _, requests = redirecting_repository
    project, cfg = _project(tmp_path, csk_home, skills_root, monkeypatch, declared, 'v2')
    cfg = replace(cfg, allowed_sources=('allowed.test/team',))
    monkeypatch.setattr(cli.config, 'load_config', lambda: cfg)
    if not cached:
        repo.rename(tmp_path / 'parked-clone')
    before = _repo_bytes(repo) if cached else None
    rc = cli.main(['upgrade', 'app'])
    output = capsys.readouterr()
    assert requests == [], 'disallowed declared endpoint was contacted'
    assert rc == cli.EXIT_PARTIAL_FAIL
    assert 'Source not allowed' in output.err
    assert not (project / '.agents/skills/fixture-skill').exists()
    if cached:
        assert _repo_bytes(repo) == before


@pytest.mark.parametrize('lane', ['install', 'upgrade', 'global-install', 'global-upgrade'])
def test_review_changed_repository_refused_for_existing_clone(
    redirecting_repository, tmp_path, csk_home, skills_root, monkeypatch, capsys, lane,
):
    # A changed repository under the same key must not install old cached bytes.
    # Upgrade cases are nearby controls for the same mismatch on a fetching path.
    repo, old, declared, _, requests = redirecting_repository
    changed = 'https://other.test/intended.git'
    project, cfg = _project(tmp_path, csk_home, skills_root, monkeypatch, changed)
    if lane.startswith('global-'):
        destination = csk_home / 'global'
        destination.mkdir()
        write_skillfile(destination, {'schema_version': 1, 'agents': ['codex_cli'], 'skills': [
            {'name': 'fixture-skill', 'git': changed, 'tag': 'v1'},
        ]})
        argv = ['global', lane.removeprefix('global-')]
    else:
        argv = [lane, 'app']
    before = _repo_bytes(repo)
    rc = cli.main(argv)
    output = capsys.readouterr()
    installed = list(project.glob('.agents/skills/fixture-skill/SKILL.md'))
    installed += list((csk_home / 'global').glob('skills/**/SKILL.md'))
    assert requests == []
    assert _repo_bytes(repo) == before
    assert rc == cli.EXIT_PARTIAL_FAIL, 'changed declaration accepted using old repository'
    assert 'do not identify the same repository' in output.err
    assert not installed


def test_review_bulk_update_uses_corrected_declaration(
    redirecting_repository, tmp_path, csk_home, skills_root, monkeypatch, capsys,
):
    # Whole-root update must honor the registered project's corrected endpoint.
    # The same loopback fixture's upgrade test is the positive control.
    repo, old, declared, commit, requests = redirecting_repository
    _project(tmp_path, csk_home, skills_root, monkeypatch, declared, 'v2')
    rc = cli.main(['update'])
    output = capsys.readouterr()
    assert rc == cli.EXIT_OK, 'bulk update still fetches stored redirecting origin'
    assert requests and all(p.startswith('/repo.git/') for p in requests)
    assert run(['git', 'config', '--get', 'remote.origin.url'], repo).stdout.strip() == declared
    assert git_ops.resolve_ref(repo, 'tag', 'v2').commit == commit


def test_review_bulk_update_refuses_changed_identity_before_http(
    redirecting_repository, tmp_path, csk_home, skills_root, monkeypatch, capsys,
):
    # Whole-root update must reject a changed declaration before fetching the old repo.
    repo, old, declared, _, requests = redirecting_repository
    _project(tmp_path, csk_home, skills_root, monkeypatch, 'https://other.test/intended.git')
    before = _repo_bytes(repo)
    rc = cli.main(['update'])
    output = capsys.readouterr()
    assert requests == [], 'bulk update contacted old origin without checking declaration'
    assert rc == cli.EXIT_PARTIAL_FAIL
    assert 'do not identify the same repository' in output.err
    assert _repo_bytes(repo) == before


@pytest.mark.parametrize('allowed', [True, False])
def test_review_transitive_declared_endpoint_and_policy(
    redirecting_repository, tmp_path, csk_home, skills_root, monkeypatch, capsys, allowed,
):
    # Real transitive fetch must use the corrected endpoint only when allowed;
    # the allowed case controls the otherwise identical forbidden network case.
    repo, old, declared, commit, requests = redirecting_repository
    consumer, _ = make_skill_repo(skills_root, 'consumer', {
        'csk-skill.json': json.dumps({
            'schema_version': 4, 'capabilities': {'exec': 'none', 'network': 'none'},
            'dependencies': {'skills': {'fixture-skill': {
                'git': declared, 'ref': {'kind': 'tag', 'value': 'v2'}, 'mode': 'context',
            }}},
        }),
    }, tag='v1')
    project = make_project(tmp_path)
    write_skillfile(project, {'schema_version': 1, 'skills': [{'name': 'consumer', 'tag': 'v1'}]})
    cfg = replace(make_config(csk_home, skills_root, project, agents=['codex_cli']),
                  allowed_sources=('example.test' if allowed else 'allowed.test/team',))
    monkeypatch.setattr(cli.config, 'load_config', lambda: cfg)
    rc = cli.main(['upgrade', 'app'])
    output = capsys.readouterr()
    if allowed:
        assert rc == cli.EXIT_OK, output.err
        assert requests and all(p.startswith('/repo.git/') for p in requests)
        assert git_ops.resolve_ref(repo, 'tag', 'v2').commit == commit
    else:
        assert requests == [], 'disallowed transitive declaration was contacted'
        assert rc == cli.EXIT_PARTIAL_FAIL
        assert 'Source not allowed' in output.err


def test_review_username_token_does_not_enter_git_argv(
    redirecting_repository, tmp_path, csk_home, skills_root, monkeypatch, capsys,
):
    # HTTPS userinfo may hold a token without a password. It must not enter
    # discovery, fetch, URL-specific pins, or set-url argv; only a synthetic marker is used.
    repo, old, declared, _, requests = redirecting_repository
    token_url = declared.replace('https://', 'https://REVIEW_TOKEN_CANARY@')
    effective = git_ops.git(repo, ['ls-remote', '--get-url', '--', declared]).stdout.strip()
    run(['git', 'config', f'url.{effective}.insteadOf', token_url], repo)
    _project(tmp_path, csk_home, skills_root, monkeypatch, token_url, 'v2')
    before = _repo_bytes(repo)
    real_run = git_ops._run
    leaked = []

    def capture(command, **kwargs):
        if any('REVIEW_TOKEN_CANARY' in arg for arg in command):
            leaked.append('fetch' if 'fetch' in command else 'discovery-or-persistence')
        return real_run(command, **kwargs)

    monkeypatch.setattr(git_ops, '_run', capture)
    rc = cli.main(['upgrade', 'app'])
    output = capsys.readouterr()
    assert leaked == [], 'HTTPS userinfo token entered child-process argv'
    assert 'REVIEW_TOKEN_CANARY' not in output.out + output.err
    assert rc == cli.EXIT_PARTIAL_FAIL
    assert 'must not contain userinfo' in output.err
    assert requests == [] and _repo_bytes(repo) == before


def test_review_remote_absence_does_not_ignore_declaration(redirecting_repository):
    # A cached clone without remotes cannot silently accept an unchecked URL.
    repo, _, _, _, requests = redirecting_repository
    run(['git', 'remote', 'remove', 'origin'], repo)
    before = _repo_bytes(repo)
    with pytest.raises(git_ops.GitError, match='no fetch remote'):
        git_ops.fetch_repo(repo, 'https://other.test/intended.git')
    assert requests == []
    assert _repo_bytes(repo) == before


@pytest.mark.parametrize('second_changed', [False, True])
def test_review_shared_fetch_dedup_does_not_skip_second_declaration(
    redirecting_repository, tmp_path, csk_home, skills_root, monkeypatch, capsys, second_changed,
):
    # Sharing a fetched cache is valid for two matching declarations, but a
    # second project's different repository must still be rejected independently.
    repo, old, declared, _, requests = redirecting_repository
    project, cfg = _project(tmp_path, csk_home, skills_root, monkeypatch, declared, 'v2')
    second = make_project(tmp_path, name='second-project')
    second_url = 'https://other.test/intended.git' if second_changed else declared
    write_skillfile(second, {'schema_version': 1, 'skills': [
        {'name': 'fixture-skill', 'git': second_url, 'tag': 'v2'},
    ]})
    cfg = replace(cfg, projects={
        **cfg.projects,
        'z-second': replace(cfg.projects['app'], alias='z-second', path=second),
    })
    monkeypatch.setattr(cli.config, 'load_config', lambda: cfg)
    rc = cli.main(['upgrade', '--all'])
    output = capsys.readouterr()
    second_installed = (second / '.agents/skills/fixture-skill/SKILL.md').exists()
    assert (project / '.agents/skills/fixture-skill/SKILL.md').exists()
    if second_changed:
        assert rc == cli.EXIT_PARTIAL_FAIL, 'fetch dedup silently accepted second repository'
        assert 'do not identify the same repository' in output.err
        assert not second_installed
    else:
        assert rc == cli.EXIT_OK, output.err
        assert second_installed


@pytest.mark.parametrize('spelling', ['absolute', 'file-url', 'relative', 'dot-relative'])
def test_review_unchanged_local_source_survives_first_clone_normalization(
    tmp_path, monkeypatch, spelling,
):
    # Git normalizes relative local clone URLs into absolute origin paths.
    # The unchanged declaration must still fetch; absolute/file URLs are controls.
    source, commit = make_skill_repo(tmp_path, 'local-source', tag='v1')
    declared = {'absolute': str(source), 'file-url': source.as_uri(),
                'relative': 'local-source', 'dot-relative': './local-source'}[spelling]
    monkeypatch.chdir(tmp_path)
    repo = tmp_path / 'clone'
    git_ops.clone_repo(declared, repo)
    stored = run(['git', 'config', '--get', 'remote.origin.url'], repo).stdout.strip()
    # Existing stored-remote behavior remains a valid positive control.
    git_ops.fetch_repo(repo)
    git_ops.fetch_repo(repo, declared)
    assert git_ops.resolve_ref(repo, 'tag', 'v1').commit == commit


@pytest.mark.parametrize('lane', ['install', 'global-install'])
@pytest.mark.parametrize('allowed', [True, False])
@pytest.mark.parametrize('with_url', [True, False])
def test_offline_cached_install_rechecks_policy_with_valid_control(
    redirecting_repository, tmp_path, csk_home, skills_root, monkeypatch, capsys, lane, allowed, with_url,
):
    # An unchanged, cached declaration installs offline only while authorized;
    # a policy change refuses without rewriting/fetching the clone or publishing a skill.
    repo, _, declared, _, requests = redirecting_repository
    project, cfg = _project(tmp_path, csk_home, skills_root, monkeypatch, declared)
    declaration = {'name': 'fixture-skill', 'tag': 'v1', **({'git': declared} if with_url else {})}
    write_skillfile(project, {'schema_version': 1, 'skills': [declaration]})
    cfg = replace(cfg, allowed_sources=('example.test' if allowed else 'denied.test',))
    monkeypatch.setattr(cli.config, 'load_config', lambda: cfg)
    destination = project / '.agents/skills/fixture-skill'
    argv = ['install', 'app']
    if lane == 'global-install':
        root = csk_home / 'global'
        root.mkdir()
        write_skillfile(root, {'schema_version': 1, 'agents': ['codex_cli'], 'skills': [declaration]})
        argv = ['global', 'install']
        destination = root / 'skills/fixture-skill'
    before = _repo_bytes(repo)
    rc = cli.main(argv)
    output = capsys.readouterr()
    assert requests == [] and _repo_bytes(repo) == before
    if allowed:
        assert rc == cli.EXIT_OK, output.err
        assert (destination / 'SKILL.md').is_file()
    else:
        assert rc == cli.EXIT_PARTIAL_FAIL
        assert 'Source not allowed' in output.err
        assert not destination.exists()


@pytest.mark.parametrize('scope', ['project', 'global', 'hybrid', 'transitive'])
@pytest.mark.parametrize('conflict', [False, True])
def test_bulk_update_preflights_all_declared_endpoints(
    redirecting_repository, tmp_path, csk_home, skills_root, monkeypatch, capsys, scope, conflict,
):
    # Project/global/hybrid/transitive endpoints are all collected before any
    # fetch. Contradictory same-identity spellings refuse (no guessed .git winner);
    # identical declarations fetch once from the corrected URL.
    repo, old, declared, commit, requests = redirecting_repository
    project, cfg = _project(tmp_path, csk_home, skills_root, monkeypatch, declared, 'v2')
    other_url = old if conflict else declared
    declaration = {'name': 'fixture-skill', 'git': other_url, 'tag': 'v1'}
    if scope == 'project':
        second = make_project(tmp_path, name='second')
        write_skillfile(second, {'schema_version': 1, 'skills': [declaration]})
        cfg = replace(cfg, projects={**cfg.projects, 'second': replace(cfg.projects['app'], alias='second', path=second)})
    elif scope == 'global':
        root = csk_home / 'global'
        root.mkdir()
        write_skillfile(root, {'schema_version': 1, 'skills': [declaration]})
    elif scope == 'hybrid':
        second = make_project(tmp_path, name='second')
        write_skillfile(second, {'schema_version': 1, 'skills': []})
        cfg = replace(cfg, projects={**cfg.projects, 'second': replace(cfg.projects['app'], alias='second', path=second)})
        root = csk_home / 'hybrid'
        root.mkdir()
        write_skillfile(root, {'schema_version': 1, 'skills': [{**declaration, 'targets': ['second']}]})
    else:
        make_skill_repo(skills_root, 'consumer', {'agent-skill.json': json.dumps({
            'schema_version': 6, 'capabilities': {}, 'dependencies': {'skills': {'fixture-skill': {
                'git': other_url, 'ref': {'kind': 'tag', 'value': 'v1'},
            }}},
        })}, tag='v1')
        write_skillfile(project, {'schema_version': 1, 'skills': [
            {'name': 'fixture-skill', 'git': declared, 'tag': 'v2'},
            {'name': 'consumer', 'tag': 'v1'},
        ]})
    monkeypatch.setattr(cli.config, 'load_config', lambda: cfg)
    before = {child.name: _repo_bytes(child) for child in skills_root.iterdir()}
    rc = cli.main(['update'])
    output = capsys.readouterr()
    if conflict:
        assert rc == cli.EXIT_PARTIAL_FAIL
        assert 'Conflicting declared endpoints' in output.err
        assert requests == []
        assert {child.name: _repo_bytes(child) for child in skills_root.iterdir()} == before
    else:
        assert rc == cli.EXIT_OK, output.err
        assert requests and all(path.startswith('/repo.git/') for path in requests)
        assert requests.count('/repo.git/info/refs?service=git-upload-pack') == 1
        assert git_ops.resolve_ref(repo, 'tag', 'v2').commit == commit


@pytest.mark.parametrize('spelling', ['relative', 'dot-relative', 'file-url'])
@pytest.mark.parametrize('different', [False, True])
def test_local_reconciliation_fetches_new_objects_and_refuses_other_repository(tmp_path, monkeypatch, spelling, different):
    # Relative clone normalization preserves a real subsequent fetch of a new
    # commit/tag, while a different local repository cannot replace cached refs.
    from conftest import commit_all
    source, _ = make_skill_repo(tmp_path, 'source', tag='v1')
    other, _ = make_skill_repo(tmp_path, 'other', tag='v1')
    monkeypatch.chdir(tmp_path)
    declared = {'relative': 'source', 'dot-relative': './source', 'file-url': source.as_uri()}[spelling]
    repo = tmp_path / 'clone'
    git_ops.clone_repo(declared, repo)
    (source / 'SKILL.md').write_text('# New bytes\n')
    commit = commit_all(source)
    run(['git', 'tag', 'v2'], source)
    before = _repo_bytes(repo)
    if different:
        with pytest.raises(git_ops.GitError, match='do not identify the same repository'):
            git_ops.fetch_repo(repo, str(other))
        assert _repo_bytes(repo) == before
    else:
        git_ops.fetch_repo(repo, declared)
        assert git_ops.resolve_ref(repo, 'tag', 'v2').commit == commit
        assert git_ops.resolve_ref(repo, 'branch', 'main').commit == commit
        # A second fetch cannot reinterpret a persisted relative URL inside the clone.
        git_ops.fetch_repo(repo)


@pytest.mark.parametrize('declaration', [None, 'self', 'other'])
def test_remote_less_local_repository_requires_proven_local_identity(tmp_path, declaration):
    # An undeclared local repository or explicit self-source is valid without
    # remotes; an unrelated local path is not provenance for this repository.
    repo, _ = make_skill_repo(tmp_path, 'source', tag='v1')
    url = None if declaration is None else str(repo if declaration == 'self' else tmp_path / 'other')
    before = _repo_bytes(repo)
    if declaration == 'other':
        with pytest.raises(git_ops.GitError, match='no fetch remote'):
            git_ops.fetch_repo(repo, url)
    else:
        git_ops.fetch_repo(repo, url)
    assert _repo_bytes(repo) == before


@pytest.mark.parametrize('operation', ['clone', 'validate', 'fetch'])
@pytest.mark.parametrize('userinfo', ['SYNTHETIC_TOKEN@', 'user:SYNTHETIC_PASSWORD@'])
def test_http_userinfo_refuses_before_any_git_child(tmp_path, monkeypatch, operation, userinfo):
    # Synthetic credential-bearing declarations cannot reach even discovery
    # argv. Existing credential-free HTTPS and SSH username cases are controls.
    repo, _ = make_skill_repo(tmp_path, 'source', tag='v1')
    run(['git', 'remote', 'add', 'origin', 'https://example.test/repo'], repo)
    before = _repo_bytes(repo)
    destination = tmp_path / 'clone'

    def forbidden(*args, **kwargs):
        pytest.fail('Git child started for a credential-bearing declaration')

    monkeypatch.setattr(git_ops, '_run', forbidden)
    url = f'https://{userinfo}example.test/repo.git'
    with pytest.raises(git_ops.GitError, match='must not contain userinfo') as caught:
        if operation == 'clone':
            git_ops.clone_repo(url, destination)
        elif operation == 'validate':
            git_ops.validate_repo_source(repo, url)
        else:
            git_ops.fetch_repo(repo, url)
    assert 'SYNTHETIC_' not in str(caught.value)
    assert _repo_bytes(repo) == before
    assert not destination.exists()


@pytest.mark.parametrize('allowed', [False, True])
def test_bulk_update_gates_even_unreferenced_remotes_before_first_fetch(
    redirecting_repository, tmp_path, csk_home, skills_root, monkeypatch, capsys, allowed,
):
    # A late disallowed stored remote stops the entire preflight before an
    # earlier authorized clone fetches; the same setup with allowed origins works.
    repo, _, declared, _, requests = redirecting_repository
    _, cfg = _project(tmp_path, csk_home, skills_root, monkeypatch, declared)
    cfg = replace(cfg, allowed_sources=('example.test',))
    monkeypatch.setattr(cli.config, 'load_config', lambda: cfg)
    spare, _ = make_skill_repo(skills_root, 'z-spare', tag='spare-v1')
    url = declared if allowed else 'https://denied.test/repo.git'
    run(['git', 'remote', 'add', 'origin', url], spare)
    # Even a faulty implementation can only contact loopback, never denied.test.
    effective = git_ops.git(repo, ['ls-remote', '--get-url', '--', declared]).stdout.strip()
    run(['git', 'config', f'url.{effective}.insteadOf', url], spare)
    before = {child.name: _repo_bytes(child) for child in skills_root.iterdir()}
    rc = cli.main(['update'])
    output = capsys.readouterr()
    if allowed:
        assert rc == cli.EXIT_OK, output.err
        assert requests and all(path.startswith('/repo.git/') for path in requests)
    else:
        assert rc == cli.EXIT_PARTIAL_FAIL
        assert 'Source not allowed' in output.err
        assert requests == []
        assert {child.name: _repo_bytes(child) for child in skills_root.iterdir()} == before


@pytest.mark.parametrize('operation', ['clone', 'fetch'])
def test_credential_bearing_effective_rewrite_cannot_reach_transport(tmp_path, monkeypatch, operation):
    # A clean declaration cannot smuggle HTTPS userinfo through operator URL
    # rewriting into fetch/clone argv or URL-specific trust options.
    repo, _ = make_skill_repo(tmp_path, 'source', tag='v1')
    url = 'https://example.test/repo.git'
    run(['git', 'remote', 'add', 'origin', url], repo)
    config = tmp_path / 'rewrite.gitconfig'
    config.write_text('[url "https://SYNTHETIC_TOKEN@example.test/repo.git"]\n insteadOf = https://example.test/repo.git\n')
    monkeypatch.setenv('GIT_CONFIG_GLOBAL', str(config))
    real_run = git_ops._run
    before = _repo_bytes(repo)

    def guarded(command, **kwargs):
        assert all('SYNTHETIC_TOKEN' not in arg for arg in command)
        assert 'fetch' not in command and 'clone' not in command
        return real_run(command, **kwargs)

    monkeypatch.setattr(git_ops, '_run', guarded)
    with pytest.raises(git_ops.GitError, match='must not contain userinfo'):
        if operation == 'clone':
            git_ops.clone_repo(url, tmp_path / 'clone')
        else:
            git_ops.fetch_repo(repo, url)
    assert _repo_bytes(repo) == before
    assert not (tmp_path / 'clone').exists()


def test_dev_substitution_userinfo_refused_before_reporting_or_acquisition(
    tmp_path, skills_root, csk_home, monkeypatch,
):
    # A synthetic token in a dev endpoint is rejected before progress messages
    # or acquisition, preserving the declared clone. Valid dev Git endpoints
    # are exercised by test_git_branch_substitution_resolves_the_branch_head.
    from csk import installer
    from test_dev_substitution import _setup, _ignore_dev_manifest, _write_dev_manifest

    project, provider, _, cfg = _setup(tmp_path, skills_root, csk_home)
    _ignore_dev_manifest(project)
    _write_dev_manifest(project, {'substitutions': {'provider': {
        'git': 'https://SYNTHETIC_TOKEN@example.test/repo.git',
        'ref': {'kind': 'tag', 'value': 'v1'},
    }}})
    before = _repo_bytes(provider)
    real_run = git_ops._run

    def guarded(command, **kwargs):
        assert all('SYNTHETIC_TOKEN' not in arg for arg in command)
        assert 'fetch' not in command and 'clone' not in command
        return real_run(command, **kwargs)

    monkeypatch.setattr(git_ops, '_run', guarded)
    result = installer.install(cfg)[0]
    assert len(result.errors) == 1
    assert 'must not contain userinfo' in result.errors[0]
    assert 'SYNTHETIC_TOKEN' not in '\n'.join(result.messages + result.errors)
    assert _repo_bytes(provider) == before
    assert not (csk_home / 'dev').exists()
    assert not (project / '.agents/skills/provider').exists()
