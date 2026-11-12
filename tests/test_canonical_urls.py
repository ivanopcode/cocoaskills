"""Canonical source URLs: one repository, one declared spelling (R5).

The scenario tests use real Git against a loopback dumb-HTTP server that
answers 301 for ``/team/<name>/...`` when ``/team/<name>.git`` exists, like
the field GitLab. ``https://gitlab.test/`` reaches it through a fixture-only
``insteadOf`` rule. The r4 B1 regression witnesses (old worktree registered,
master checkout with origin already ``.git``, dependency with the old
spelling, dependency spelling differing from the project's) must give one
strict, actionable verdict, identical in ``csk update`` and ``csk upgrade``.
"""
from __future__ import annotations

import base64
import http.server
import json
import os
import sys
import tempfile
import threading
from dataclasses import dataclass, replace
from functools import partial
from pathlib import Path

import pytest
from conftest import commit_all, init_git_repo, make_config, make_project, make_skill_repo, run, write_files, write_skillfile

from csk import cli, git_ops

OLD = 'https://gitlab.test/team/{}'
NEW = 'https://gitlab.test/team/{}.git'


@dataclass
class Gitlab:
    served: Path
    sources: Path
    requests: list[str]

    def publish(self, name: str, tag: str, files: dict[str, str] | None = None) -> str:
        """Commit ``files`` to the source of ``name``, tag it and serve it."""
        source = self.sources / name
        if not source.exists():
            init_git_repo(source)
        write_files(source, {'SKILL.md': f'---\nname: {name}\n---\n\n# {name} {tag}\n', **(files or {})})
        commit = commit_all(source)
        run(['git', 'tag', tag], source)
        bare = self.served / 'team' / f'{name}.git'
        if not bare.exists():
            run(['git', 'clone', '--bare', str(source), str(bare)], self.sources)
        else:
            run(['git', 'push', str(bare), 'main', '--tags'], source)
        run(['git', 'update-server-info'], bare)
        return commit

    def clone(self, skills_root: Path, name: str, origin: str) -> Path:
        repo = skills_root / name
        run(['git', 'clone', str(self.served / 'team' / f'{name}.git'), str(repo)], self.sources)
        run(['git', 'remote', 'set-url', 'origin', origin], repo)
        return repo

    def hits(self, name: str) -> list[str]:
        return [path for path in self.requests if path.startswith((f'/team/{name}/', f'/team/{name}.git/'))]


@pytest.fixture
def gitlab(tmp_path, monkeypatch):
    """Loopback-only server; /team/<name>/ answers a real 301 to /team/<name>.git/."""
    served = tmp_path / 'served'
    (served / 'team').mkdir(parents=True)
    sources = tmp_path / 'sources'
    sources.mkdir()
    requests: list[str] = []

    class Handler(http.server.SimpleHTTPRequestHandler):
        def do_GET(self):
            requests.append(self.path)
            parts = self.path.split('/', 3)
            if (
                len(parts) == 4 and parts[1] == 'team' and not parts[2].endswith('.git')
                and (served / 'team' / f'{parts[2]}.git').is_dir()
            ):
                self.send_response(301)
                self.send_header('Location', f'/team/{parts[2]}.git/{parts[3]}')
                self.send_header('Content-Length', '0')
                self.end_headers()
            else:
                super().do_GET()

        def log_message(self, *args):
            pass

    server = http.server.HTTPServer(('127.0.0.1', 0), partial(Handler, directory=str(served)))
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    gitconfig = tmp_path / 'gitconfig'
    gitconfig.write_text(f'[url "http://127.0.0.1:{server.server_port}/"]\n insteadOf = https://gitlab.test/\n')
    monkeypatch.setenv('GIT_CONFIG_GLOBAL', str(gitconfig))
    try:
        yield Gitlab(served, sources, requests)
    finally:
        server.shutdown()
        thread.join(timeout=5)
        server.server_close()
        assert not thread.is_alive()


def _spec(requirements: dict[str, str]) -> str:
    return json.dumps({
        'schema_version': 4, 'capabilities': {'exec': 'none', 'network': 'none'},
        'dependencies': {'skills': {
            name: {'git': url, 'ref': {'kind': 'tag', 'value': 'v2'}, 'mode': 'context'}
            for name, url in requirements.items()
        }},
    })


def _projects(tmp_path, csk_home, skills_root, monkeypatch, **skillfiles):
    """Register one project per keyword (alias -> skills); the first is 'app'."""
    cfg = None
    paths = {}
    for alias, skills in skillfiles.items():
        project = make_project(tmp_path, name=alias)
        write_skillfile(project, {'schema_version': 1, 'skills': skills})
        paths[alias] = project
        if cfg is None:
            cfg = make_config(csk_home, skills_root, project, agents=['codex_cli'])
            if alias != 'app':
                cfg = replace(cfg, projects={alias: replace(cfg.projects['app'], alias=alias)})
        else:
            cfg = replace(cfg, projects={**cfg.projects, alias: replace(next(iter(cfg.projects.values())),
                                                                         alias=alias, path=project)})
    monkeypatch.setattr(cli.config, 'load_config', lambda: cfg)
    return cfg, paths


def _origin(repo: Path) -> str:
    return run(['git', 'config', '--get', 'remote.origin.url'], repo).stdout.strip()


def _conflict(text: str) -> str:
    start = text.index('repository gitlab.test/team/b is declared')
    return text[start:text.index('\n', start)]


def _b_declaration(spelling: str) -> dict:
    return {'name': 'b', 'git': spelling, 'tag': 'v2'}


# r4 B1 witnesses with mixed spellings. Each refuses repository b in both
# commands, before any request to b, with the same message naming every
# spelling and every declaring place; b's origin and refs stay unchanged.
# 'aligned' is the positive control: one spelling everywhere fetches the
# canonical .git URL and rewrites the old origin in both commands.
@pytest.mark.parametrize('scenario', ['old-worktree', 'old-worktree-origin-fixed', 'dependency-differs', 'aligned'])
def test_mixed_spellings_get_one_verdict_in_update_and_upgrade(
    gitlab, tmp_path, csk_home, skills_root, monkeypatch, capsys, scenario,
):
    gitlab.publish('b', 'v1')
    repo_b = gitlab.clone(skills_root, 'b', NEW.format('b') if scenario == 'old-worktree-origin-fixed' else OLD.format('b'))
    newest = gitlab.publish('b', 'v2')
    skills = [_b_declaration(NEW.format('b'))]
    others = {}
    if scenario.startswith('old-worktree'):
        others['old-worktree'] = [_b_declaration(OLD.format('b'))]
    elif scenario == 'dependency-differs':
        gitlab.publish('a', 'v1', {'csk-skill.json': _spec({'b': OLD.format('b')})})
        gitlab.clone(skills_root, 'a', NEW.format('a'))
        skills.insert(0, {'name': 'a', 'git': NEW.format('a'), 'tag': 'v1'})
    else:
        others['dev-worktree'] = [_b_declaration(NEW.format('b'))]
    _, paths = _projects(tmp_path, csk_home, skills_root, monkeypatch, app=skills, **others)
    origin_before = _origin(repo_b)

    update_rc = cli.main(['update'])
    update = capsys.readouterr()
    after_update = (_origin(repo_b), list(gitlab.hits('b')))
    upgrade_rc = cli.main(['upgrade', 'app'])
    upgrade = capsys.readouterr()

    if scenario == 'aligned':
        assert update_rc == cli.EXIT_OK, update.err
        assert upgrade_rc == cli.EXIT_OK, upgrade.err
        assert after_update[0] == NEW.format('b')
        assert gitlab.hits('b') and all(path.startswith('/team/b.git/') for path in gitlab.hits('b'))
        assert git_ops.resolve_ref(repo_b, 'tag', 'v2').commit == newest
        return
    assert update_rc == cli.EXIT_PARTIAL_FAIL and upgrade_rc == cli.EXIT_PARTIAL_FAIL
    assert 'update refused b: repository gitlab.test/team/b is declared with 2 different URL spellings' in update.err
    assert 'Refusing b (via' in upgrade.err
    assert _conflict(update.err) == _conflict(upgrade.err), 'update and upgrade must word one verdict'
    message = _conflict(update.err)
    assert repr(NEW.format('b')) in message and repr(OLD.format('b')) in message
    assert f"{paths['app'] / 'Skillfile.json'} (project app)" in message
    if scenario == 'dependency-differs':
        assert "the skill spec of 'a' at tag v1 (via app -> a -> b)" in message
    else:
        assert f"{paths['old-worktree'] / 'Skillfile.json'} (project old-worktree)" in message
    assert "remove registered projects you no longer use" in message
    assert gitlab.hits('b') == [] and after_update == (origin_before, [])
    assert _origin(repo_b) == origin_before
    with pytest.raises(git_ops.GitError):
        git_ops.resolve_ref(repo_b, 'tag', 'v2')


# r4 B1 witnesses where the only declared spelling redirects: the master
# checkout (origin already .git, Skillfile without it) and a dependency that
# still uses the old spelling. csk fetches the declared URL only, never the
# stored origin, and both commands give csk's own error naming the canonical
# .git form derived from the real 301; the origin keeps its value. Update's
# error also names where the redirecting URL is declared (R6). The
# 'declared-canonical' rows are the positive controls of the same layouts.
@pytest.mark.parametrize('scenario', ['master-checkout', 'dependency-old', 'declared-canonical', 'dependency-canonical'])
@pytest.mark.parametrize('command', ['update', 'upgrade'])
def test_redirecting_declaration_names_the_canonical_url(
    gitlab, tmp_path, csk_home, skills_root, monkeypatch, capsys, scenario, command,
):
    gitlab.publish('b', 'v1')
    repo_b = gitlab.clone(skills_root, 'b', NEW.format('b'))
    newest = gitlab.publish('b', 'v2')
    spelling = OLD.format('b') if scenario in ('master-checkout', 'dependency-old') else NEW.format('b')
    if scenario.startswith('dependency'):
        gitlab.publish('a', 'v1', {'csk-skill.json': _spec({'b': spelling})})
        gitlab.clone(skills_root, 'a', NEW.format('a'))
        skills = [{'name': 'a', 'git': NEW.format('a'), 'tag': 'v1'}]
    else:
        skills = [_b_declaration(spelling)]
    _, paths = _projects(tmp_path, csk_home, skills_root, monkeypatch, master=skills)
    rc = cli.main(['update'] if command == 'update' else ['upgrade', 'master'])
    output = capsys.readouterr()
    if spelling == NEW.format('b'):
        assert rc == cli.EXIT_OK, output.err
        assert git_ops.resolve_ref(repo_b, 'tag', 'v2').commit == newest
        assert all(path.startswith('/team/b.git/') for path in gitlab.hits('b'))
        assert 'declared in:' not in output.err
        return
    assert rc == cli.EXIT_PARTIAL_FAIL
    assert f"declared URL {OLD.format('b')!r} is not canonical" in output.err
    if command == 'update':
        place = (
            "the skill spec of 'a' at tag v1 (via master -> a -> b)" if scenario == 'dependency-old'
            else f"{paths['master'] / 'Skillfile.json'} (project master)"
        )
        assert output.err.rstrip().endswith(f'(declared in: {place})')
    assert "HTTP redirects are refused" in output.err
    assert f"The server redirects to the same repository at {NEW.format('b')!r}" in output.err
    # One refused fetch and one anonymous repeat to read Location; the
    # stored .git origin is never used as a fallback.
    assert gitlab.hits('b') == ['/team/b/info/refs?service=git-upload-pack'] * 2
    assert _origin(repo_b) == NEW.format('b')
    with pytest.raises(git_ops.GitError):
        git_ops.resolve_ref(repo_b, 'tag', 'v2')


# A dependency spec read from cached HEAD, because the declared ref is not
# cached yet, only locates clones: its stale old spelling does not refuse b,
# update fetches the declared .git URL and the newly cached ref, and the
# offline check then agrees. Upgrade (control) reaches the same verdict. The
# refusing counterpart, a stale spelling at a cached declared ref, is the
# 'dependency-differs' witness above.
@pytest.mark.parametrize('command', ['update', 'upgrade'])
def test_dependency_read_from_cached_head_declares_no_spelling(
    gitlab, tmp_path, csk_home, skills_root, monkeypatch, capsys, command,
):
    gitlab.publish('b', 'v1')
    repo_b = gitlab.clone(skills_root, 'b', OLD.format('b'))
    gitlab.publish('b', 'v2')
    gitlab.publish('a', 'v1', {'csk-skill.json': _spec({'b': OLD.format('b')})})
    repo_a = gitlab.clone(skills_root, 'a', NEW.format('a'))
    gitlab.publish('a', 'v2', {'csk-skill.json': _spec({'b': NEW.format('b')})})
    _projects(tmp_path, csk_home, skills_root, monkeypatch, app=[
        {'name': 'a', 'git': NEW.format('a'), 'tag': 'v2'}, _b_declaration(NEW.format('b')),
    ])
    rc = cli.main(['update'] if command == 'update' else ['upgrade', 'app'])
    output = capsys.readouterr()
    assert rc == cli.EXIT_OK, output.err
    assert 'different URL spellings' not in output.err
    assert all(path.startswith('/team/b.git/') for path in gitlab.hits('b'))
    assert _origin(repo_b) == NEW.format('b')
    git_ops.resolve_ref(repo_a, 'tag', 'v2')
    assert cli.main(['check', '--canonical-urls', 'app']) == cli.EXIT_OK, capsys.readouterr().err


# Per-clone failure model in upgrade --all: the project that needs the
# refused repository fails, an unrelated project is still upgraded.
def test_upgrade_all_refuses_only_projects_that_need_the_mixed_repository(
    gitlab, tmp_path, csk_home, skills_root, monkeypatch, capsys,
):
    gitlab.publish('b', 'v1')
    repo_b = gitlab.clone(skills_root, 'b', OLD.format('b'))
    gitlab.publish('b', 'v2')
    gitlab.publish('c', 'v1')
    repo_c = gitlab.clone(skills_root, 'c', OLD.format('c'))
    newest_c = gitlab.publish('c', 'v2')
    _projects(
        tmp_path, csk_home, skills_root, monkeypatch,
        app=[_b_declaration(NEW.format('b'))],
        old=[_b_declaration(OLD.format('b'))],
        other=[{'name': 'c', 'git': NEW.format('c'), 'tag': 'v2'}],
    )
    rc = cli.main(['upgrade', '--all'])
    output = capsys.readouterr()
    assert rc == cli.EXIT_PARTIAL_FAIL
    assert output.err.count('repository gitlab.test/team/b is declared with 2 different URL spellings') == 2
    assert gitlab.hits('b') == [] and _origin(repo_b) == OLD.format('b')
    assert git_ops.resolve_ref(repo_c, 'tag', 'v2').commit == newest_c
    assert _origin(repo_c) == NEW.format('c')


# A missing clone is not created when the registry disagrees on its spelling;
# the same install clones the canonical URL when only one spelling exists.
@pytest.mark.parametrize('mixed', [True, False])
def test_install_does_not_clone_a_repository_with_mixed_spellings(
    gitlab, tmp_path, csk_home, skills_root, monkeypatch, capsys, mixed,
):
    gitlab.publish('b', 'v2')
    others = {'old': [_b_declaration(OLD.format('b'))]} if mixed else {}
    _projects(tmp_path, csk_home, skills_root, monkeypatch, app=[_b_declaration(NEW.format('b'))], **others)
    rc = cli.main(['install', 'app'])
    output = capsys.readouterr()
    if mixed:
        assert rc == cli.EXIT_PARTIAL_FAIL
        assert 'repository gitlab.test/team/b is declared with 2 different URL spellings' in output.err
        assert gitlab.hits('b') == [] and not (skills_root / 'b').exists()
    else:
        assert rc == cli.EXIT_OK, output.err
        assert _origin(skills_root / 'b') == NEW.format('b')


# Global update joins the registry: a project spelling that disagrees with
# the global declaration refuses that repository; one spelling fetches.
@pytest.mark.parametrize('mixed', [True, False])
def test_global_update_applies_the_registry_spelling_rule(
    gitlab, tmp_path, csk_home, skills_root, monkeypatch, capsys, mixed,
):
    gitlab.publish('b', 'v1')
    repo_b = gitlab.clone(skills_root, 'b', OLD.format('b'))
    newest = gitlab.publish('b', 'v2')
    _projects(tmp_path, csk_home, skills_root, monkeypatch,
              app=[_b_declaration(OLD.format('b') if mixed else NEW.format('b'))])
    root = csk_home / 'global'
    root.mkdir()
    write_skillfile(root, {'schema_version': 1, 'skills': [_b_declaration(NEW.format('b'))]})
    rc = cli.main(['global', 'update'])
    output = capsys.readouterr()
    if mixed:
        assert rc != cli.EXIT_OK
        assert 'repository gitlab.test/team/b is declared with 2 different URL spellings' in output.out + output.err
        assert gitlab.hits('b') == [] and _origin(repo_b) == OLD.format('b')
    else:
        assert rc == cli.EXIT_OK, output.err
        assert git_ops.resolve_ref(repo_b, 'tag', 'v2').commit == newest


# Identity rule: .git, trailing slashes, host case and transport are one
# repository, so their spellings disagree; different hosts or paths are
# different repositories and never conflict. Identical spellings agree.
@pytest.mark.parametrize(('first', 'second', 'conflict'), [
    ('https://h.test/team/x.git', 'https://h.test/team/x', True),
    ('https://h.test/team/x', 'https://h.test/team/x/', True),
    ('https://h.test/team/x.git', 'https://H.test/team/x.git', True),
    ('git@h.test:team/x.git', 'https://h.test/team/x.git', True),
    ('https://h.test/team/x.git', 'https://h.test/team/x.git', False),
    ('https://h.test/team/x.git', 'https://other.test/team/x.git', False),
    ('https://h.test/team/x.git', 'https://h.test/team/y', False),
    ('https://h.test/team/x.git', 'https://h.test/Team/x.git', False),
    ('{local}/x', '{local}/x', False),
    ('{local}/x', '{local}/y', False),
    ('{local}/x', '{uri}/x', True),
])
def test_repository_identity_rule(tmp_path, first, second, conflict):
    from csk import canonical_urls

    # Local rows use real absolute paths and file URIs of this platform.
    root = tmp_path.resolve()
    first, second = (
        value.format(local=root, uri=root.as_uri()) for value in (first, second)
    )
    index = canonical_urls.SpellingIndex([
        canonical_urls.Declared(first, 'first place'), canonical_urls.Declared(second, 'second place'),
    ])
    assert bool(index.conflicts()) is conflict
    if conflict:
        (_, message), = index.conflicts()
        assert 'first place' in message and 'second place' in message


# Only a same-endpoint redirect of the discovery request that changes the
# .git suffix or a trailing slash names a canonical URL, and that URL is a
# respelling of the declaration: Location credentials, queries, other paths,
# hosts, ports or schemes never reach the message.
@pytest.mark.parametrize(('declared', 'effective', 'location', 'expected'), [
    ('https://h.test/team/x', 'https://h.test/team/x', '/team/x.git/info/refs?service=git-upload-pack',
     'https://h.test/team/x.git'),
    ('https://h.test/team/x.git', 'https://h.test/team/x.git', '/team/x/info/refs?service=git-upload-pack',
     'https://h.test/team/x'),
    ('https://h.test/team/x/', 'https://h.test/team/x/', 'https://h.test/team/x.git/info/refs',
     'https://h.test/team/x.git'),
    ('https://h.test/team/x', 'http://127.0.0.1:9/team/x', '/team/x.git/info/refs?service=git-upload-pack',
     'https://h.test/team/x.git'),
    ('https://h.test/team/x', 'https://h.test/team/x',
     'https://USER_CANARY:PASS_CANARY@h.test/team/x.git/info/refs?token=QUERY_CANARY', 'https://h.test/team/x.git'),
    ('https://h.test/team/x', 'https://h.test/team/x', '/users/sign_in?token=LOCATION_CANARY', None),
    ('https://h.test/team/x', 'https://h.test/team/x', '/team/y.git/info/refs', None),
    ('https://h.test/team/x', 'https://h.test/team/x', 'https://evil.test/team/x.git/info/refs', None),
    ('https://h.test/team/x', 'https://h.test/team/x', 'http://h.test/team/x.git/info/refs', None),
    ('https://h.test/team/x', 'https://h.test/team/x', 'https://h.test:8443/team/x.git/info/refs', None),
    ('https://h.test/team/x', 'https://h.test/team/x', '/team/x/info/refs?service=git-upload-pack', None),
    ('https://h.test/team/x', 'https://h.test/team/x', None, None),
])
def test_canonical_url_comes_only_from_a_same_repository_redirect(declared, effective, location, expected):
    assert git_ops.canonical_from_redirect(declared, effective, location) == expected
    message = git_ops.redirect_refusal(declared, git_ops.canonical_from_redirect(declared, effective, location))
    assert 'CANARY' not in message
    assert ('same repository at' in message) is (expected is not None)
    assert ('Clone UI or API' in message) is (expected is None)


# The Location probe keeps every transport pin and adds no credentials:
# credential helpers, prompts and extra headers are disabled, redirects stay
# refused, and the curl trace goes to Git's stderr (fd 2), not to a file. A
# failure that is not a redirect never starts the probe (control).
@pytest.mark.parametrize('declared', ['redirecting', 'missing'])
def test_location_probe_is_anonymous_and_only_follows_a_redirect_failure(
    gitlab, tmp_path, monkeypatch, declared,
):
    gitlab.publish('b', 'v1')
    repo_b = gitlab.clone(tmp_path / 'skills', 'b', NEW.format('b'))
    url = OLD.format('b') if declared == 'redirecting' else NEW.format('missing')
    scratch = tmp_path / 'tmpdir'
    scratch.mkdir()
    monkeypatch.setenv('TMPDIR', str(scratch))
    import tempfile
    monkeypatch.setattr(tempfile, 'tempdir', None)
    real_run = git_ops._run
    probes = []

    def observed(command, **kwargs):
        if 'ls-remote' in command and '--heads' in command:
            probes.append((list(command), dict(kwargs['env'])))
        return real_run(command, **kwargs)

    monkeypatch.setattr(git_ops, '_run', observed)
    if declared == 'missing':
        run(['git', 'remote', 'set-url', 'origin', url], repo_b)
    with pytest.raises(git_ops.GitError):
        git_ops.fetch_repo(repo_b, url)
    if declared == 'missing':
        assert probes == []
        return
    (command, env), = probes
    assert command[command.index('--') + 1] == url
    assert 'credential.helper=' in command and 'http.extraHeader=' in command
    assert any(item.startswith('http.http://127.0.0.1:') and item.endswith('.followRedirects=false') for item in command)
    assert env['GIT_TERMINAL_PROMPT'] == '0' and env['GIT_ASKPASS'] == '' and env['GIT_TRACE_REDACT'] == '1'
    assert env['GIT_TRACE_CURL'] == '2'
    assert not list(scratch.iterdir())


# CI lint: a disagreement between the target and a registered project, or a
# cached dependency spec, exits 1 and names both places; one spelling exits 0.
# Without --network nothing is requested.
@pytest.mark.parametrize('case', ['registry', 'dependency', 'aligned'])
def test_check_canonical_urls_reports_disagreements_offline(
    gitlab, tmp_path, csk_home, skills_root, monkeypatch, capsys, case,
):
    gitlab.publish('b', 'v1')
    gitlab.clone(skills_root, 'b', NEW.format('b'))
    skills = [_b_declaration(NEW.format('b'))]
    others = {}
    if case == 'registry':
        others['old'] = [_b_declaration(OLD.format('b'))]
    elif case == 'dependency':
        gitlab.publish('a', 'v1', {'csk-skill.json': _spec({'b': OLD.format('b')})})
        gitlab.clone(skills_root, 'a', NEW.format('a'))
        skills.append({'name': 'a', 'git': NEW.format('a'), 'tag': 'v1'})
    _, paths = _projects(tmp_path, csk_home, skills_root, monkeypatch, app=skills, **others)
    gitlab.requests.clear()
    rc = cli.main(['check', '--canonical-urls', 'app'])
    output = capsys.readouterr()
    assert gitlab.requests == []
    assert 'canonical URLs:' in output.out and 'for project app' in output.out
    if case == 'aligned':
        assert rc == cli.EXIT_OK, output.err
        assert output.err == ''
        return
    assert rc == cli.EXIT_PARTIAL_FAIL
    assert 'error: repository gitlab.test/team/b is declared with 2 different URL spellings' in output.err
    assert str(paths['app'] / 'Skillfile.json') in output.err
    if case == 'registry':
        assert str(paths['old'] / 'Skillfile.json') in output.err
    else:
        assert "the skill spec of 'a' at tag v1 (via app -> a -> b)" in output.err


# Scope: a target check reports disagreements on repositories the target
# declares; --all reports every disagreement in the registry. Dependencies
# that cannot be read from a clone are listed as not checked.
def test_check_canonical_urls_scope_and_not_checked_notes(
    tmp_path, csk_home, skills_root, monkeypatch, capsys,
):
    _projects(
        tmp_path, csk_home, skills_root, monkeypatch,
        app=[_b_declaration(NEW.format('b'))],
        x=[{'name': 'c', 'git': NEW.format('c'), 'tag': 'v1'}],
        y=[{'name': 'c', 'git': OLD.format('c'), 'tag': 'v1'}],
    )
    assert cli.main(['check', '--canonical-urls', 'app']) == cli.EXIT_OK
    output = capsys.readouterr()
    assert 'note: not checked: dependencies of b: no clone under skills_root' in output.out
    assert cli.main(['check', '--canonical-urls', '--all']) == cli.EXIT_PARTIAL_FAIL
    output = capsys.readouterr()
    assert 'repository gitlab.test/team/c is declared with 2 different URL spellings' in output.err
    assert 'gitlab.test/team/b' not in output.err


# --network: a declared URL that answers with the real 301 exits 1 and names
# the canonical .git form; the canonical declaration (control) reports ok.
# --network without --canonical-urls is a usage error.
@pytest.mark.parametrize('spelling', [OLD, NEW])
def test_check_canonical_urls_network_reports_redirects(
    gitlab, tmp_path, csk_home, skills_root, monkeypatch, capsys, spelling,
):
    gitlab.publish('b', 'v1')
    _projects(tmp_path, csk_home, skills_root, monkeypatch, app=[_b_declaration(spelling.format('b'))])
    assert cli.main(['check', '--network', 'app']) == cli.EXIT_CONFIG
    assert '--network requires --canonical-urls' in capsys.readouterr().err
    rc = cli.main(['check', '--canonical-urls', '--network', 'app'])
    output = capsys.readouterr()
    if spelling is NEW:
        assert rc == cli.EXIT_OK, output.err
        assert f"ok: {NEW.format('b')!r} answers without a redirect" in output.out
        assert gitlab.hits('b')[0] == '/team/b.git/info/refs?service=git-upload-pack'
        assert all(path.startswith('/team/b.git/') for path in gitlab.hits('b'))
        return
    assert rc == cli.EXIT_PARTIAL_FAIL
    assert f"declared URL {OLD.format('b')!r} is not canonical" in output.err
    assert f"same repository at {NEW.format('b')!r}" in output.err
    assert gitlab.hits('b') == ['/team/b/info/refs?service=git-upload-pack']


# N1: relative local git paths in Skillfile.dev.json resolve against the
# project root, like its 'path' entries, so upgrade from another directory
# reaches the same substitute; a different relative repository is refused.
@pytest.mark.parametrize('case', ['elsewhere', 'other-repository'])
def test_dev_substitution_git_path_is_project_relative(
    tmp_path, csk_home, skills_root, monkeypatch, capsys, case,
):
    make_skill_repo(tmp_path, 'provider-local', tag='v1')
    make_skill_repo(tmp_path, 'unrelated', tag='v1')
    make_skill_repo(skills_root, 'provider', tag='v1')
    project = make_project(tmp_path)
    (project / '.gitignore').write_text((project / '.gitignore').read_text() + 'Skillfile.dev.json\n')
    write_skillfile(project, {'schema_version': 1, 'skills': [{'name': 'provider', 'tag': 'v1'}]})
    dev = {'substitutions': {'provider': {'git': '../provider-local', 'ref': {'kind': 'tag', 'value': 'v1'}}}}
    (project / 'Skillfile.dev.json').write_text(json.dumps(dev))
    monkeypatch.setattr(cli.config, 'load_config', lambda: make_config(csk_home, skills_root, project))
    monkeypatch.chdir(project)
    assert cli.main(['upgrade', 'app']) == cli.EXIT_OK, capsys.readouterr().err
    elsewhere = tmp_path / 'x/y'
    elsewhere.mkdir(parents=True)
    monkeypatch.chdir(elsewhere)
    if case == 'other-repository':
        dev['substitutions']['provider']['git'] = '../unrelated'
        (project / 'Skillfile.dev.json').write_text(json.dumps(dev))
    rc = cli.main(['upgrade', 'app'])
    output = capsys.readouterr()
    if case == 'elsewhere':
        assert rc == cli.EXIT_OK, output.err
    else:
        assert rc == cli.EXIT_PARTIAL_FAIL
        assert 'do not identify the same repository' in output.err


# N3: install reads refs already in the clone; a newly published tag fails
# with a hint to run upgrade, and upgrade (control) then installs it.
def test_install_missing_tag_suggests_upgrade(gitlab, tmp_path, csk_home, skills_root, monkeypatch, capsys):
    gitlab.publish('b', 'v1')
    gitlab.clone(skills_root, 'b', NEW.format('b'))
    gitlab.publish('b', 'v2')
    _projects(tmp_path, csk_home, skills_root, monkeypatch, app=[_b_declaration(NEW.format('b'))])
    assert cli.main(['install', 'app']) == cli.EXIT_PARTIAL_FAIL
    output = capsys.readouterr()
    assert "Cannot resolve tag 'v2' for b" in output.err
    assert "run 'csk upgrade'" in output.err
    assert cli.main(['upgrade', 'app']) == cli.EXIT_OK, capsys.readouterr().err


def _expected_conflict(spellings: dict[str, list[str]]) -> str:
    from csk import canonical_urls

    return canonical_urls.conflict_message('gitlab.test/team/b', spellings)


# R6 N1: each project in 'upgrade --all' gets the verdict 'upgrade <project>'
# and 'update' give on the state at the start of the command, in any project
# order. 'earlier-root-fails': pa is refused on x before its cached spec of a
# (requiring b's old spelling) is read; 'earlier-root-moves': pa's branch
# moves upstream to the canonical spelling and pa fetches b itself. In both,
# pb is refused on b with the same message in every command, and in
# 'earlier-root-fails' nothing requests b. In 'earlier-root-moves' the
# corrected declaration counts from the next command once a's clone was
# fetched (by update or --all); 'upgrade pb' alone never fetches a, so its
# rerun is still refused (control). The 'pb-first' rows are controls that
# e2c4e9c already passed; the 'pa-first' rows fail on e2c4e9c.
@pytest.mark.parametrize('scenario', ['earlier-root-fails', 'earlier-root-moves'])
@pytest.mark.parametrize('command', ['update', 'upgrade pb', 'upgrade --all pa-first', 'upgrade --all pb-first'])
def test_each_project_gets_the_verdict_it_gets_alone(
    gitlab, tmp_path, csk_home, skills_root, monkeypatch, capsys, scenario, command,
):
    gitlab.publish('b', 'v1')
    repo_b = gitlab.clone(skills_root, 'b', OLD.format('b'))
    gitlab.publish('b', 'v2')
    gitlab.publish('a', 'v1', {'csk-skill.json': _spec({'b': OLD.format('b')})})
    gitlab.clone(skills_root, 'a', NEW.format('a'))
    if scenario == 'earlier-root-fails':
        gitlab.publish('x', 'v1')
        gitlab.clone(skills_root, 'x', NEW.format('x'))
        pa = [{'name': 'x', 'git': NEW.format('x'), 'tag': 'v1'}, {'name': 'a', 'git': NEW.format('a'), 'tag': 'v1'}]
        extra = {'pz': [{'name': 'x', 'git': OLD.format('x'), 'tag': 'v1'}]}
        a_place = "the skill spec of 'a' at tag v1 (via pa -> a -> b)"
    else:
        gitlab.publish('a', 'v2', {'csk-skill.json': _spec({'b': NEW.format('b')})})
        pa = [{'name': 'a', 'git': NEW.format('a'), 'branch': 'main'}]
        extra = {}
        a_place = "the skill spec of 'a' at branch main (via pa -> a -> b)"
    pb = [_b_declaration(NEW.format('b'))]
    order = {'pb': pb, 'pa': pa, **extra} if command.endswith('pb-first') else {'pa': pa, **extra, 'pb': pb}
    _, paths = _projects(tmp_path, csk_home, skills_root, monkeypatch, **order)
    origin_before = _origin(repo_b)

    rc = cli.main(command.split()[:2])
    output = capsys.readouterr()

    assert rc == cli.EXIT_PARTIAL_FAIL
    assert 'repository gitlab.test/team/b is declared' in output.err, 'pb must be refused on b'
    expected = _expected_conflict({
        OLD.format('b'): [a_place], NEW.format('b'): [f"{paths['pb'] / 'Skillfile.json'} (project pb)"],
    })
    assert _conflict(output.err) == expected
    assert ('update refused b: ' if command == 'update' else 'Refusing b (via <project> -> b): ') + expected in output.err
    if scenario == 'earlier-root-fails':
        assert gitlab.hits('b') == [] and _origin(repo_b) == origin_before
        with pytest.raises(git_ops.GitError):
            git_ops.resolve_ref(repo_b, 'tag', 'v2')
        return
    rerun = cli.main(['upgrade', 'pb'])
    rerun_output = capsys.readouterr()
    if command == 'upgrade pb':
        assert rerun == cli.EXIT_PARTIAL_FAIL and _conflict(rerun_output.err) == expected
    else:
        assert rerun == cli.EXIT_OK, rerun_output.err
        assert _origin(repo_b) == NEW.format('b')
        git_ops.resolve_ref(repo_b, 'tag', 'v2')


# R6 N1, install: a clone that an earlier project created in the same
# 'install --all' is checked for later projects as if they had to clone it.
# pa's dependency (read from the clone of a it creates) disagrees with pb on
# b's spelling, so pa is refused and pb installs from b's canonical URL, in
# either order; e2c4e9c instead refused pb (pa first) or let pa use pb's
# fresh clone of b unchecked (pb first).
@pytest.mark.parametrize('order', ['pa-first', 'pb-first'])
def test_install_all_checks_a_clone_an_earlier_project_created(
    gitlab, tmp_path, csk_home, skills_root, monkeypatch, capsys, order,
):
    gitlab.publish('b', 'v2')
    gitlab.publish('a', 'v1', {'csk-skill.json': _spec({'b': OLD.format('b')})})
    pa = [{'name': 'a', 'git': NEW.format('a'), 'tag': 'v1'}]
    pb = [_b_declaration(NEW.format('b'))]
    projects = {'pa': pa, 'pb': pb} if order == 'pa-first' else {'pb': pb, 'pa': pa}
    _, paths = _projects(tmp_path, csk_home, skills_root, monkeypatch, **projects)

    rc = cli.main(['install', '--all'])
    output = capsys.readouterr()

    assert rc == cli.EXIT_PARTIAL_FAIL
    expected = _expected_conflict({
        OLD.format('b'): ["the skill spec of 'a' at tag v1 (via pa -> a -> b)"],
        NEW.format('b'): [f"{paths['pb'] / 'Skillfile.json'} (project pb)"],
    })
    assert f'Refusing b (via <project> -> a -> b): {expected}' in output.err
    assert 'Refusing b (via <project> -> b)' not in output.err
    assert _origin(skills_root / 'b') == NEW.format('b')
    assert gitlab.hits('b') and all(path.startswith('/team/b.git/') for path in gitlab.hits('b'))
    assert (paths['pb'] / '.agents' / 'skills' / 'b').exists()
    assert not (paths['pa'] / '.agents' / 'skills' / 'a').exists()


# R6 N2: 'csk global update' keeps the dependency spellings read from the
# global manifest's cached specs, so it refuses b exactly as 'csk update'
# does, with the same message and no request to b. With one spelling
# (control) both fetch b. The mixed row fails on e2c4e9c.
@pytest.mark.parametrize('mixed', [True, False])
def test_global_update_counts_dependency_spellings_of_the_global_manifest(
    gitlab, tmp_path, csk_home, skills_root, monkeypatch, capsys, mixed,
):
    gitlab.publish('b', 'v1')
    repo_b = gitlab.clone(skills_root, 'b', OLD.format('b'))
    newest = gitlab.publish('b', 'v2')
    gitlab.publish('a', 'v1', {'csk-skill.json': _spec({'b': (OLD if mixed else NEW).format('b')})})
    gitlab.clone(skills_root, 'a', NEW.format('a'))
    cfg = replace(make_config(csk_home, skills_root, make_project(tmp_path)), projects={})
    monkeypatch.setattr(cli.config, 'load_config', lambda: cfg)
    root = csk_home / 'global'
    root.mkdir()
    write_skillfile(root, {'schema_version': 1, 'skills': [
        {'name': 'a', 'git': NEW.format('a'), 'tag': 'v1'}, _b_declaration(NEW.format('b')),
    ]})

    global_rc = cli.main(['global', 'update'])
    global_update = capsys.readouterr()
    global_hits = list(gitlab.hits('b'))
    update_rc = cli.main(['update'])
    update = capsys.readouterr()

    if not mixed:
        assert global_rc == cli.EXIT_OK and update_rc == cli.EXIT_OK, global_update.err + update.err
        assert git_ops.resolve_ref(repo_b, 'tag', 'v2').commit == newest
        assert _origin(repo_b) == NEW.format('b')
        return
    assert global_rc != cli.EXIT_OK and update_rc == cli.EXIT_PARTIAL_FAIL
    assert "the skill spec of 'a' at tag v1 (via global -> a -> b)" in _conflict(update.err)
    assert _conflict(global_update.out + global_update.err) == _conflict(update.err)
    assert global_hits == [] and gitlab.hits('b') == [] and _origin(repo_b) == OLD.format('b')


# Optional R6 item: with no registered project, 'check --canonical-urls --all'
# checks the global Skillfile and its cached dependency specs (exit 1 on a
# disagreement, 0 with one spelling) instead of exiting 2; without a global
# Skillfile it still reports that there is nothing registered (exit 2).
@pytest.mark.parametrize('case', ['mixed', 'aligned', 'nothing'])
def test_check_canonical_urls_all_covers_a_global_only_setup(
    gitlab, tmp_path, csk_home, skills_root, monkeypatch, capsys, case,
):
    gitlab.publish('b', 'v1')
    gitlab.publish('a', 'v1', {'csk-skill.json': _spec({'b': (OLD if case == 'mixed' else NEW).format('b')})})
    gitlab.clone(skills_root, 'a', NEW.format('a'))
    cfg = replace(make_config(csk_home, skills_root, make_project(tmp_path)), projects={})
    monkeypatch.setattr(cli.config, 'load_config', lambda: cfg)
    if case != 'nothing':
        root = csk_home / 'global'
        root.mkdir()
        write_skillfile(root, {'schema_version': 1, 'skills': [
            {'name': 'a', 'git': NEW.format('a'), 'tag': 'v1'}, _b_declaration(NEW.format('b')),
        ]})
    rc = cli.main(['check', '--canonical-urls', '--all'])
    output = capsys.readouterr()
    if case == 'nothing':
        assert rc == cli.EXIT_CONFIG
        assert 'no registered projects' in output.err
        return
    assert 'checked for the global Skillfile' in output.out
    if case == 'aligned':
        assert rc == cli.EXIT_OK, output.err
        return
    assert rc == cli.EXIT_PARTIAL_FAIL
    assert "the skill spec of 'a' at tag v1 (via global -> a -> b)" in _conflict(output.err)


@dataclass
class _AuthServer:
    url: str
    requests: list[dict[str, str | None]]
    challenge: dict[str, bool]


@pytest.fixture
def auth_gitlab(gitlab, tmp_path, monkeypatch):
    """A loopback host that challenges every unauthenticated request with 401.

    Authenticated requests to /team/<n>/ get a real 301 to /team/<n>.git/ with
    a Set-Cookie canary; ``challenge['on'] = False`` drops the 401. The Git
    configuration adds an extra header and a cookie file, both canaries, for
    every request to the host.
    """
    requests: list[dict[str, str | None]] = []
    challenge = {'on': True}
    served = gitlab.served

    class Handler(http.server.SimpleHTTPRequestHandler):
        def do_GET(self):
            requests.append({
                'path': self.path, 'authorization': self.headers.get('Authorization'),
                'cookie': self.headers.get('Cookie'), 'extra': self.headers.get('X-Canary'),
            })
            if challenge['on'] and self.headers.get('Authorization') is None:
                self.send_response(401)
                self.send_header('WWW-Authenticate', 'Basic realm="canary"')
                self.send_header('Content-Length', '0')
                self.end_headers()
                return
            parts = self.path.split('/', 3)
            if len(parts) == 4 and parts[1] == 'team' and not parts[2].endswith('.git'):
                self.send_response(301)
                self.send_header('Location', f'/team/{parts[2]}.git/{parts[3]}')
                self.send_header('Set-Cookie', 'sid=SETCOOKIE_CANARY; Path=/')
                self.send_header('Content-Length', '0')
                self.end_headers()
                return
            super().do_GET()

        def log_message(self, *args):
            pass

    server = http.server.HTTPServer(('127.0.0.1', 0), partial(Handler, directory=str(served)))
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    url = f'http://127.0.0.1:{server.server_port}/'
    cookies = tmp_path / 'cookies.txt'
    cookies.write_text('127.0.0.1\tFALSE\t/\tFALSE\t0\tjar\tCOOKIE_CANARY\n')
    gitconfig = tmp_path / 'auth-gitconfig'
    gitconfig.write_text(
        f'[url "{url}"]\n insteadOf = https://gitlab.test/\n'
        f'[http]\n extraHeader = X-Canary: HEADER_CANARY\n cookieFile = {cookies}\n'
        '[credential]\n helper =\n'
    )
    monkeypatch.setenv('GIT_CONFIG_GLOBAL', str(gitconfig))
    try:
        yield _AuthServer(url, requests, challenge)
    finally:
        server.shutdown()
        thread.join(timeout=5)
        server.server_close()
        assert not thread.is_alive()


def _basic(user: str, password: str) -> str:
    return 'Basic ' + base64.b64encode(f'{user}:{password}'.encode()).decode()


# R6 N3: the probe after a refused redirect, and 'check --network', add no
# credentials: no credential helper, extra header or cookie reaches it, and
# its first request carries no Authorization. The fetch before it (control)
# does send the helper credentials, the header and the cookie, so the
# fixture is effective. The documented exception: Git always lets libcurl
# read ~/.netrc, so a netrc entry answers a 401 from the same host, for the
# probe as for the fetch, on the same path; the canary never reaches output.
@pytest.mark.parametrize('credentials', ['helper', 'netrc'])
@pytest.mark.parametrize('command', ['fetch', 'check-network'])
def test_redirect_probe_adds_no_credentials_and_netrc_answers_only_the_same_host(
    gitlab, auth_gitlab, tmp_path, monkeypatch, credentials, command,
):
    gitlab.publish('b', 'v1')
    repo_b = gitlab.clone(tmp_path / 'skills', 'b', NEW.format('b'))
    home = Path(os.environ['HOME'])
    home.mkdir(parents=True, exist_ok=True)
    if credentials == 'netrc':
        for name in ('.netrc', '_netrc'):
            (home / name).write_text('machine 127.0.0.1 login netrcuser password NETRC_CANARY\n')
            (home / name).chmod(0o600)
        expected_auth = _basic('netrcuser', 'NETRC_CANARY')
    else:
        with open(os.environ['GIT_CONFIG_GLOBAL'], 'a') as config:
            config.write(' helper = "!f() { echo username=helperuser; echo password=HELPER_CANARY; }; f"\n')
        expected_auth = _basic('helperuser', 'HELPER_CANARY')
    probe_start = 0
    if command == 'fetch':
        with pytest.raises(git_ops.GitError) as refused:
            git_ops.fetch_repo(repo_b, OLD.format('b'))
        shown = str(refused.value)
        fetch = auth_gitlab.requests[:2]
        probe_start = 2
        # Control: the fetch sends the configured header, cookie and credentials.
        assert [item['authorization'] for item in fetch] == [None, expected_auth]
        assert all(item['extra'] == 'HEADER_CANARY' for item in fetch)
        assert all('COOKIE_CANARY' in (item['cookie'] or '') for item in fetch)
    else:
        result = git_ops.probe_declared_url(OLD.format('b'))
        shown = repr(result)
    probe = auth_gitlab.requests[probe_start:]
    assert probe and probe[0]['authorization'] is None
    assert all(item['extra'] is None and item['cookie'] is None for item in probe)
    assert all(item['path'] == '/team/b/info/refs?service=git-upload-pack' for item in probe)
    assert 'CANARY' not in shown
    sent = [item['authorization'] for item in probe if item['authorization'] is not None]
    if credentials == 'helper':
        assert sent == []
    else:
        assert all(value == expected_auth for value in sent)
        if sys.platform != 'win32':
            # libcurl answers the challenge from ~/.netrc; the probe then
            # reads the redirect and names the canonical spelling.
            assert sent == [expected_auth]
            assert NEW.format('b') in shown


# R6 N4: the probe's curl trace never reaches the disk. At the moment Git
# finishes the probe, the last point before csk processes the trace (where a
# kill would have left a trace file behind), no file under TMPDIR or the
# test's tree holds the 301's Set-Cookie canary. Positive controls: the probe
# received the canary, and csk still read the Location from the trace (the
# canonical spelling is named). e2c4e9c wrote the trace to a file in TMPDIR.
def test_redirect_probe_writes_no_response_header_to_disk(gitlab, auth_gitlab, tmp_path, monkeypatch):
    auth_gitlab.challenge['on'] = False
    gitlab.publish('b', 'v1')
    repo_b = gitlab.clone(tmp_path / 'skills', 'b', NEW.format('b'))
    scratch = tmp_path / 'tmpdir'
    scratch.mkdir()
    monkeypatch.setenv('TMPDIR', str(scratch))
    monkeypatch.setattr(tempfile, 'tempdir', None)
    real_run = git_ops._run
    on_disk: list[str] = []
    received: list[bool] = []

    def observed(command, **kwargs):
        proc = real_run(command, **kwargs)
        if 'ls-remote' in command and '--heads' in command:
            for root in {tmp_path, Path(tempfile.gettempdir())}:
                for path in root.rglob('*'):
                    if path.is_file() and not path.is_symlink() and b'SETCOOKIE_CANARY' in path.read_bytes():
                        on_disk.append(str(path))
            received.append('SETCOOKIE_CANARY' in (proc.stderr or ''))
        return proc

    monkeypatch.setattr(git_ops, '_run', observed)
    with pytest.raises(git_ops.GitError) as refused:
        git_ops.fetch_repo(repo_b, OLD.format('b'))
    assert on_disk == []
    assert received == [True]
    assert f'same repository at {NEW.format("b")!r}' in str(refused.value)
    assert 'SETCOOKIE_CANARY' not in str(refused.value)


# Docs claim (R6): to connect over SSH while keeping one HTTPS spelling, map
# the spelling with a local 'insteadOf' rule. csk compares declarations
# before Git rewrites them, so update and upgrade fetch over SSH (a fake ssh
# serving the fixture repositories records the connection, and the HTTP
# server sees nothing) and the clone's origin keeps the declared spelling.
# Control: declaring the SSH spelling in one place and the HTTPS spelling in
# another is two spellings and refuses before any connection.
@pytest.mark.skipif(sys.platform == 'win32', reason='POSIX fake ssh transport')
@pytest.mark.parametrize('declared_ssh_elsewhere', [False, True])
@pytest.mark.parametrize('command', ['update', 'upgrade'])
def test_insteadof_to_ssh_keeps_one_https_spelling(
    gitlab, tmp_path, csk_home, skills_root, monkeypatch, capsys, declared_ssh_elsewhere, command,
):
    gitlab.publish('b', 'v1')
    repo_b = gitlab.clone(skills_root, 'b', NEW.format('b'))
    newest = gitlab.publish('b', 'v2')
    fake = tmp_path / 'fake_ssh.py'
    fake.write_text(
        'import os, shlex, subprocess, sys\n'
        'host, remote = sys.argv[1], sys.argv[-1]\n'
        'with open(os.environ["FAKE_SSH_LOG"], "a") as log:\n'
        '    log.write(f"{host} {remote}\\n")\n'
        'verb, path = shlex.split(remote)\n'
        'env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}\n'
        'env["GIT_CONFIG_NOSYSTEM"] = "1"\n'
        'sys.exit(subprocess.call(["git", verb.removeprefix("git-"), path], cwd=os.environ["FAKE_SSH_ROOT"], env=env))\n'
    )
    log = tmp_path / 'ssh.log'
    gitconfig = tmp_path / 'ssh-gitconfig'
    gitconfig.write_text('[url "git@gitlab.test:"]\n insteadOf = https://gitlab.test/\n')
    monkeypatch.setenv('GIT_CONFIG_GLOBAL', str(gitconfig))
    monkeypatch.setenv('GIT_SSH_COMMAND', f'"{sys.executable}" "{fake}"')
    monkeypatch.setenv('GIT_SSH_VARIANT', 'simple')
    monkeypatch.setenv('FAKE_SSH_LOG', str(log))
    monkeypatch.setenv('FAKE_SSH_ROOT', str(gitlab.served))
    other = 'git@gitlab.test:team/b.git' if declared_ssh_elsewhere else NEW.format('b')
    _projects(tmp_path, csk_home, skills_root, monkeypatch,
              app=[_b_declaration(NEW.format('b'))], dev=[_b_declaration(other)])

    rc = cli.main(['update'] if command == 'update' else ['upgrade', 'app'])
    output = capsys.readouterr()

    assert gitlab.requests == []
    if declared_ssh_elsewhere:
        assert rc == cli.EXIT_PARTIAL_FAIL
        assert 'repository gitlab.test/team/b is declared with 2 different URL spellings' in output.err
        assert not log.exists()
        assert _origin(repo_b) == NEW.format('b')
        return
    assert rc == cli.EXIT_OK, output.err
    assert log.read_text().splitlines()[0] == "git@gitlab.test git-upload-pack 'team/b.git'"
    assert git_ops.resolve_ref(repo_b, 'tag', 'v2').commit == newest
    assert _origin(repo_b) == NEW.format('b')
