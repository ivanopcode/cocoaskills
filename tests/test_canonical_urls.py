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
import subprocess
import sys
import tempfile
import threading
from dataclasses import dataclass, field, replace
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
    # Raw header lines added to every 301, as a server might send them.
    redirect_headers: list[bytes] = field(default_factory=list)
    # Status of that answer: 301 unless a test makes the route fail otherwise.
    redirect_status: dict[str, int] = field(default_factory=lambda: {'code': 301})

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
    redirect_headers: list[bytes] = []
    redirect_status = {'code': 301}

    class Handler(http.server.SimpleHTTPRequestHandler):
        def do_GET(self):
            requests.append(self.path)
            parts = self.path.split('/', 3)
            if (
                len(parts) == 4 and parts[1] == 'team' and not parts[2].endswith('.git')
                and (served / 'team' / f'{parts[2]}.git').is_dir()
            ):
                self.send_response(redirect_status['code'])
                self.send_header('Location', f'/team/{parts[2]}.git/{parts[3]}')
                self.send_header('Content-Length', '0')
                self._headers_buffer.extend(redirect_headers)
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
        yield Gitlab(served, sources, requests, redirect_headers, redirect_status)
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
    # One refused fetch and one repeat without credentials to read Location;
    # the stored .git origin is never used as a fallback.
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
def test_location_probe_adds_no_credentials_and_only_follows_a_redirect_failure(
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
    # Status of the authenticated answer on /team/<n>/: 301, or 500 (R9).
    answer: dict[str, int]


@pytest.fixture
def auth_gitlab(gitlab, tmp_path, monkeypatch):
    """A loopback host that challenges every unauthenticated request with 401.

    Authenticated requests to /team/<n>/ get a real 301 to /team/<n>.git/ with
    a Set-Cookie canary (``answer['status'] = 500`` makes it a 500 with the
    same headers); ``challenge['on'] = False`` drops the 401. The Git
    configuration adds an extra header and a cookie file, both canaries, for
    every request to the host.
    """
    requests: list[dict[str, str | None]] = []
    challenge = {'on': True}
    answer = {'status': 301}
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
                self.send_response(answer['status'])
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
    # Forward slashes: Git config treats a backslash as an escape, so a
    # Windows path written as is ('D:\a\...') makes the whole file invalid
    # and every Git command fails before it sends a request.
    gitconfig.write_text(
        f'[url "{url}"]\n insteadOf = https://gitlab.test/\n'
        f'[http]\n extraHeader = X-Canary: HEADER_CANARY\n cookieFile = {cookies.as_posix()}\n'
        '[credential]\n helper =\n'
    )
    # The fixture is effective only if Git can read this file.
    assert run(['git', 'config', '--file', str(gitconfig), '--get', 'http.cookieFile'],
               tmp_path).stdout.strip() == cookies.as_posix()
    monkeypatch.setenv('GIT_CONFIG_GLOBAL', str(gitconfig))
    try:
        yield _AuthServer(url, requests, challenge, answer)
    finally:
        server.shutdown()
        thread.join(timeout=5)
        server.server_close()
        assert not thread.is_alive()


def _basic(user: str, password: str) -> str:
    return 'Basic ' + base64.b64encode(f'{user}:{password}'.encode()).decode()


# R6 N3: the probe after a refused redirect, and 'check --network', add no
# credentials: no credential helper, extra header or cookie reaches it, and
# its first request carries no Authorization. The fetch or clone before it
# (control) does send the helper credentials, the header and the cookie, so
# the fixture is effective. The documented exception: Git always lets
# libcurl read ~/.netrc, so a netrc entry answers a 401, for the probe as for
# the fetch, on the same path (a second HTTP request); the canary never
# reaches output. R9: the same holds for the repeat after a failure that csk
# does not classify ('server-error': the authenticated answer is a 500, so
# Git reports 'unable to access ... returned error: 500'), and that repeat
# finds no redirect. This fixture has one host, so it does not show the
# same-host limit: that comes from the followRedirects=false pin, which
# test_location_probe_adds_no_credentials_and_only_follows_a_redirect_failure
# asserts.
@pytest.mark.parametrize('answer', ['redirect', 'server-error'])
@pytest.mark.parametrize('credentials', ['helper', 'netrc'])
@pytest.mark.parametrize('command', ['fetch', 'clone', 'check-network'])
def test_redirect_probe_adds_no_credentials_and_netrc_answers_on_the_same_path(
    gitlab, auth_gitlab, tmp_path, monkeypatch, credentials, command, answer,
):
    auth_gitlab.answer['status'] = 301 if answer == 'redirect' else 500
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
    if command in ('fetch', 'clone'):
        with pytest.raises(git_ops.GitError) as refused:
            if command == 'fetch':
                git_ops.fetch_repo(repo_b, OLD.format('b'))
            else:
                git_ops.clone_repo(OLD.format('b'), tmp_path / 'fresh' / 'b')
        shown = str(refused.value)
        fetch = auth_gitlab.requests[:2]
        probe_start = 2
        # Control: the fetch or clone sends the configured header, cookie and credentials.
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
            assert (NEW.format('b') in shown) is (answer == 'redirect')
    if answer == 'server-error':
        assert 'not canonical' not in shown


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
            stderr = proc.stderr or b''
            received.append(b'SETCOOKIE_CANARY' in (stderr if isinstance(stderr, bytes) else stderr.encode()))
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


# R7 N1: the redirect probe reads Git's stderr as bytes, splits it on LF
# before decoding each line with replacement, and runs without the user's
# trace settings. So a non-UTF-8 byte in a 301 header, a bare CR followed by
# text that looks like a Git message, GIT_TRACE_BARE (no timestamp prefix) or
# a brief trace2 target on stderr leave the verdict unchanged: 'check
# --network' exits 1 and, like the fetch and the clone of 'csk install',
# names the canonical spelling. A brief trace2 target set in Git's global
# configuration (which the probe keeps) only adds lines that the classifier
# ignores. No trace line or header text reaches the output and nothing is
# written to TMPDIR.
# The bare-CR rows depend on libcurl. Older releases (8.7.1, Apple Git)
# pass the header and Git reports the 301. Newer ones (8.23.0, as linked by
# Homebrew Git 2.56 and, by the CI evidence, Git for Windows 2.56) refuse
# the response after the status line ('Carriage return found in header'),
# so Git reports that libcurl error instead; csk then repeats the request
# once and the server's 301 status line in the trace decides (R8). On such a
# libcurl these rows failed at 314bba8 for fetch and check-network.
# Witnesses that fail on bd9eef1 (strict decoding, universal newlines,
# GIT_TRACE_BARE kept): check-network with the non-UTF-8 or CR header (exit
# 0, "not verified") or GIT_TRACE_BARE, and fetch with the non-UTF-8 header
# or GIT_TRACE_BARE (no canonical spelling). Controls that bd9eef1 passes:
# 'plain', the trace2 rows, and fetch with the CR header (on older libcurl).
_PROBE_HAZARDS = {
    'plain': (b'', {}, ''),
    'non-utf8-header': (b'X-Odd: caf\xe9\r\n', {}, ''),
    'bare-cr-header': (b"X-Cr: a\rfatal: Authentication failed for 'https://gitlab.test/team/b/'\r\n", {}, ''),
    'trace-bare-env': (b'', {'GIT_TRACE_BARE': '1'}, ''),
    'trace2-brief-env': (b'', {'GIT_TRACE2': '2', 'GIT_TRACE2_BRIEF': '1'}, ''),
    'trace2-brief-config': (b'', {}, '[trace2]\n normalTarget = 2\n normalBrief = true\n'),
}


@pytest.mark.parametrize('hazard', list(_PROBE_HAZARDS))
@pytest.mark.parametrize('command', ['check-network', 'fetch', 'clone'])
def test_redirect_probe_verdict_ignores_header_bytes_and_trace_settings(
    gitlab, tmp_path, csk_home, skills_root, monkeypatch, capsys, hazard, command,
):
    extra, environment, gitconfig = _PROBE_HAZARDS[hazard]
    gitlab.publish('b', 'v1')
    repo_b = gitlab.clone(tmp_path / 'skills', 'b', NEW.format('b'))
    _projects(tmp_path, csk_home, skills_root, monkeypatch, app=[_b_declaration(OLD.format('b'))])
    gitlab.redirect_headers.append(extra)
    scratch = tmp_path / 'tmpdir'
    scratch.mkdir()
    monkeypatch.setenv('TMPDIR', str(scratch))
    monkeypatch.setattr(tempfile, 'tempdir', None)
    for key, value in environment.items():
        monkeypatch.setenv(key, value)
    with open(os.environ['GIT_CONFIG_GLOBAL'], 'a') as config:
        config.write(gitconfig)

    if command == 'check-network':
        rc = cli.main(['check', '--canonical-urls', '--network', 'app'])
        output = capsys.readouterr()
        shown = output.out + output.err
        assert rc == cli.EXIT_PARTIAL_FAIL, shown
        assert 'not verified' not in shown
    elif command == 'fetch':
        with pytest.raises(git_ops.GitError) as refused:
            git_ops.fetch_repo(repo_b, OLD.format('b'))
        shown = str(refused.value)
    else:
        with pytest.raises(git_ops.GitError) as refused:
            git_ops.clone_repo(OLD.format('b'), tmp_path / 'fresh' / 'b')
        shown = str(refused.value)
        assert not (tmp_path / 'fresh' / 'b').exists()
    assert f"declared URL {OLD.format('b')!r} is not canonical" in shown
    assert f"same repository at {NEW.format('b')!r}" in shown, shown
    for trace in ('Recv header', 'Send header', '== Info', 'http.c:', 'X-Odd', 'X-Cr', 'caf'):
        assert trace not in shown
    assert gitlab.hits('b')[-1] == '/team/b/info/refs?service=git-upload-pack'
    assert not list(scratch.iterdir())


# R8: when Git reports an HTTP failure that csk does not classify, the fetch
# and the clone repeat the request once (its credentials: the server-error
# rows of the R6 N3 test), and only a 3xx status of the final answer to the
# discovery request makes it a redirect.
# A 500 that carries a same-repository Location is not a redirect, and
# neither is a 500 with a later header that only looks like a 301 status
# line (libcurl 8.7.1 and 8.23.0 both pass 'HTTP/1.1 301 Moved: x' into the
# trace, and Git reports the 500), also when a header before it quotes Git's
# request marker '=> Send header'. The fetch and the clone keep their own
# error after exactly one repeat, and 'check --network' reports the URL as
# not verified, exit 0. The plain 301 row is the positive control of the
# same route and the same request count. The forged rows need libcurl to pass
# the forged header into the trace, as both releases do today;
# test_only_the_final_answer_to_the_discovery_request_decides checks the
# parser on fixed traces, independent of libcurl.
_ANSWERS = {
    'redirect': (301, []),
    'server-error': (500, []),
    'server-error-forged-status': (500, [b'HTTP/1.1 301 Moved: x\r\n']),
    'server-error-forged-request': (500, [b'X-Q: => Send header: GET /\r\n', b'HTTP/1.1 301 Moved: x\r\n']),
}


@pytest.mark.parametrize('answer', list(_ANSWERS))
@pytest.mark.parametrize('command', ['check-network', 'fetch', 'clone'])
def test_only_a_3xx_status_line_makes_an_unclassified_failure_a_redirect(
    gitlab, tmp_path, csk_home, skills_root, monkeypatch, capsys, answer, command,
):
    status, extra = _ANSWERS[answer]
    gitlab.publish('b', 'v1')
    repo_b = gitlab.clone(tmp_path / 'skills', 'b', NEW.format('b'))
    _projects(tmp_path, csk_home, skills_root, monkeypatch, app=[_b_declaration(OLD.format('b'))])
    gitlab.redirect_status['code'] = status
    gitlab.redirect_headers.extend(extra)
    destination = tmp_path / 'fresh' / 'b'
    if command == 'check-network':
        rc = cli.main(['check', '--canonical-urls', '--network', 'app'])
        output = capsys.readouterr()
        shown = output.out + output.err
    else:
        with pytest.raises(git_ops.GitError) as failed:
            if command == 'fetch':
                git_ops.fetch_repo(repo_b, OLD.format('b'))
            else:
                git_ops.clone_repo(OLD.format('b'), destination)
        shown = str(failed.value)
    assert gitlab.hits('b') == ['/team/b/info/refs?service=git-upload-pack'] * (1 if command == 'check-network' else 2)
    assert not destination.exists()
    if answer == 'redirect':
        assert f"declared URL {OLD.format('b')!r} is not canonical" in shown
        assert f"same repository at {NEW.format('b')!r}" in shown
        assert command != 'check-network' or rc == cli.EXIT_PARTIAL_FAIL
        return
    assert 'not canonical' not in shown and 'HTTP redirect' not in shown
    if command == 'check-network':
        assert rc == cli.EXIT_OK, shown
        assert f"warning: not verified {OLD.format('b')!r}: unclassified" in shown
    elif command == 'fetch':
        assert shown.endswith(': unclassified; verify the declared endpoint and authentication')
    else:
        assert shown.startswith(f"git clone failed for {OLD.format('b')} -> {destination}: ")
        assert shown.endswith('The requested URL returned error: 500')


def _trace(*events: tuple[str, str], message: str) -> bytes:
    """Probe stderr as Git writes it: timestamped curl trace records, then Git's message."""
    lines = [f'12:00:00.000001 http.c:953              {marker}: {text}'.rstrip() for marker, text in events]
    return ('\n'.join([*lines, message]) + '\n').encode()


_GET = ('=> Send header', 'GET /team/b/info/refs?service=git-upload-pack HTTP/1.1')
_CONNECT = ('=> Send header', 'CONNECT example.test:443 HTTP/1.1')
_SENT = ('=> Send header', '')
_END = ('<= Recv header', '')


def _recv(text: str) -> tuple[str, str]:
    return ('<= Recv header', text)


_LOC = _recv('Location: /team/b.git/info/refs?service=git-upload-pack')
_REFUSED = "fatal: unable to access 'https://example.test/team/b/': Carriage return found in header"
_REDIRECT = "fatal: unable to access 'https://example.test/team/b/': The requested URL returned error: 301"
_ERROR_500 = "fatal: unable to access 'https://example.test/team/b/': The requested URL returned error: 500"
_TUNNEL_302 = "fatal: unable to access 'https://example.test/team/b/': CONNECT tunnel failed, response 302"
_FORGED = _recv('HTTP/1.1 301 Moved: x')
_TRACES = {
    # Redirects: the 3xx is the final answer to the discovery GET.
    'redirect': ('canonical', [_GET, _SENT, _recv('HTTP/1.1 301 Moved Permanently'), _LOC, _END], _REDIRECT),
    'refused-after-status': ('canonical', [_GET, _SENT, _recv('HTTP/1.1 301 Moved Permanently'), _LOC], _REFUSED),
    'continue-then-redirect': ('canonical', [_GET, _SENT, _recv('HTTP/1.1 100 Continue'), _END,
                                      _recv('HTTP/1.1 301 Moved Permanently'), _LOC], _REFUSED),
    'hints-then-redirect': ('canonical', [_GET, _SENT, _recv('HTTP/1.1 103 Early Hints'), _recv('Link: </x>; rel=preload'),
                                   _END, _recv('HTTP/1.1 301 Moved Permanently'), _LOC], _REFUSED),
    'netrc-then-redirect': ('canonical', [_GET, _SENT, _recv('HTTP/1.1 401 Unauthorized'), _END, _GET,
                                   ('=> Send header', 'Authorization: Basic <redacted>'), _SENT,
                                   _recv('HTTP/1.1 301 Moved Permanently'), _LOC], _REFUSED),
    'tunnel-then-redirect': ('canonical', [_CONNECT, _SENT, _recv('HTTP/1.1 200 Connection established'), _END,
                                    _GET, _SENT, _recv('HTTP/1.1 301 Moved Permanently'), _LOC], _REFUSED),
    # A redirect whose own answer has no Location: the 401's earlier one does not count.
    'stale-location': ('generic', [_GET, _SENT, _recv('HTTP/1.1 401 Unauthorized'), _LOC, _END, _GET, _SENT,
                                   _recv('HTTP/1.1 301 Moved Permanently')], _REFUSED),
    # Not redirects.
    'hints-forged-then-500': ('unverified', [_GET, _SENT, _recv('HTTP/1.1 103 Early Hints'), _FORGED, _END,
                                      _recv('HTTP/1.1 500 Internal Server Error'), _LOC, _END], _ERROR_500),
    'hints-forged-then-200': ('unverified', [_GET, _SENT, _recv('HTTP/1.1 103 Early Hints'), _FORGED, _END,
                                      _recv('HTTP/1.1 200 OK'), _LOC], _REFUSED),
    'hints-refused': ('unverified', [_GET, _SENT, _recv('HTTP/1.1 103 Early Hints'), _FORGED, _LOC], _REFUSED),
    'connect-302-relative': ('unverified', [_CONNECT, _SENT, _recv('HTTP/1.1 302 Found'), _LOC, _END], _TUNNEL_302),
    'connect-302-absolute': ('unverified', [_CONNECT, _SENT, _recv('HTTP/1.1 302 Found'),
                                     _recv('Location: http://portal.example/login'), _END], _TUNNEL_302),
    'proxy-auth-then-connect-302': ('unverified', [_CONNECT, _SENT, _recv('HTTP/1.1 407 Proxy Authentication Required'),
                                            _END, _CONNECT, _SENT, _recv('HTTP/1.1 302 Found'), _LOC, _END],
                                    _TUNNEL_302),
    'answer-to-post': ('unverified', [_GET, _SENT, _recv('HTTP/1.1 200 OK'), _END,
                               ('=> Send header', 'POST /team/b/git-upload-pack HTTP/1.1'), _SENT,
                               _recv('HTTP/1.1 301 Moved Permanently'), _LOC], _REFUSED),
    'forged-request-marker': ('unverified', [_GET, _SENT, _recv('HTTP/1.1 500 Internal Server Error'),
                                      _recv('X-Q: => Send header: ' + _GET[1]), _FORGED, _LOC], _REFUSED),
    'no-access-frame': ('unverified', [_GET, _SENT, _recv('HTTP/1.1 301 Moved Permanently'), _LOC],
                        'error: RPC failed; curl 56 Recv failure: Connection reset by peer'),
}


# R9 B1: only the final, non-1xx answer to Git's discovery request (GET
# .../info/refs?service=git-upload-pack) decides a redirect, read from fixed
# traces in the record format Git writes (captured from Git 2.54/libcurl
# 8.7.1 and Git 2.56/libcurl 8.23.0), so this holds whatever libcurl does.
# A proxy's answer to CONNECT (with a relative or an absolute Location, also
# after a 407), an interim 1xx block and the header that ends it, a forged
# status line inside a 1xx or after a quoted request marker, a 3xx answer to
# a later POST, and a trace that never reaches a final answer are not
# redirects. Positive controls: a 301 after 100 Continue, after a 103 with a
# Link header, after a 401 that netrc answered, and through a CONNECT tunnel,
# each with the canonical spelling; a Location from an earlier answer is not
# carried into a final 301 that has none ('stale-location': a redirect, but
# no spelling is named). 'no-access-frame' pins that 'check
# --network' needs the same Git frame (an unclassified 'unable to access')
# that the fetch and the clone need before they repeat.
@pytest.mark.parametrize('case', list(_TRACES))
def test_only_the_final_answer_to_the_discovery_request_decides(monkeypatch, case):
    expected, events, message = _TRACES[case]
    url = 'https://example.test/team/b'

    def fake_run(command, **kwargs):
        if '--get-url' in command:
            return subprocess.CompletedProcess(command, 0, url + '\n', '')
        assert 'ls-remote' in command and '--heads' in command
        return subprocess.CompletedProcess(command, 128, b'', _trace(*events, message=message))

    monkeypatch.setattr(git_ops, '_run', fake_run)
    result = git_ops.probe_declared_url(url)
    if expected == 'canonical':
        assert result == git_ops.RedirectProbe('redirect', canonical=url + '.git')
    elif expected == 'generic':
        assert result == git_ops.RedirectProbe('redirect', canonical=None)
    else:
        assert result.outcome == 'unverified' and result.canonical is None


# R9 N2: only an 'unable to access' failure that csk does not classify, or
# Git's own redirect record, starts the one repeat after a failed fetch or
# clone. A classified access failure (DNS, TLS, 503 on https://), an
# unclassified failure without that frame, the same frame relayed as a
# 'remote:' line, and a redirect frame that conflicts with an authentication
# record start none. Git's stderr is injected, and so is the repeat, so no
# host is contacted. The two repeating rows are the positive controls.
_FAILURES = {
    'refused-response': ("fatal: unable to access 'https://example.test/team/b/': "
                         "Carriage return found in header", 1),
    'redirect': ("fatal: unable to access 'https://example.test/team/b/': The requested URL returned error: 301", 1),
    'dns': ("fatal: unable to access 'https://example.test/team/b/': Could not resolve host: example.test", 0),
    'tls': ("fatal: unable to access 'https://example.test/team/b/': SSL certificate problem: "
            "unable to get local issuer certificate", 0),
    'http-503': ("fatal: unable to access 'https://example.test/team/b/': The requested URL returned error: 503", 0),
    'rpc-failure': ('error: RPC failed; curl 56 Recv failure: Connection reset by peer\n'
                    'fatal: expected flush after ref listing', 0),
    'relayed': ("remote: fatal: unable to access 'https://example.test/team/b/': "
                "Carriage return found in header", 0),
    'conflict': ("fatal: unable to access 'https://example.test/team/b/': The requested URL returned error: 301\n"
                 "fatal: Authentication failed for 'https://example.test/team/b/'", 0),
}


@pytest.mark.parametrize('case', list(_FAILURES))
@pytest.mark.parametrize('command', ['fetch', 'clone'])
def test_only_an_unclassified_access_failure_starts_the_repeat(tmp_path, monkeypatch, command, case):
    stderr, repeats = _FAILURES[case]
    url = 'https://example.test/team/b'
    repo = init_git_repo(tmp_path / 'clone')
    run(['git', 'remote', 'add', 'origin', url + '.git'], repo)
    real_run = git_ops._run
    probes = []

    def failing(command_line, **kwargs):
        if 'ls-remote' in command_line and '--heads' in command_line:
            probes.append(command_line)
            return subprocess.CompletedProcess(command_line, 128, b'', b'fatal: probe refused\n')
        if command in command_line:
            return subprocess.CompletedProcess(command_line, 128, '', stderr + '\n')
        return real_run(command_line, **kwargs)

    monkeypatch.setattr(git_ops, '_run', failing)
    with pytest.raises(git_ops.GitError) as failed:
        if command == 'fetch':
            git_ops.fetch_repo(repo, url)
        else:
            git_ops.clone_repo(url, tmp_path / 'fresh')
    assert len(probes) == repeats
    assert ('not canonical' in str(failed.value)) is (case == 'redirect')


LOCATION = b'Location: /team/b.git/info/refs?service=git-upload-pack\r\n'
CLOSE = b'Content-Length: 0\r\nConnection: close\r\n\r\n'
BARE_CR = b'X-Cr: a\rb\r\n'
FORGED_STATUS = b'HTTP/1.1 301 Moved: x\r\n'
MOVED = b'HTTP/1.1 301 Moved Permanently\r\n' + LOCATION
# (route, redirect?, canned answer). 'origin': the declared server, reached
# directly; 'proxy': http.proxy answers CONNECT itself; 'tunnel': the proxy
# accepts CONNECT and relays to a TLS origin, which gives the answer.
_RAW = {
    'continue-301': ('origin', True, b'HTTP/1.1 100 Continue\r\n\r\n' + MOVED + CLOSE),
    'continue-301-cr': ('origin', True, b'HTTP/1.1 100 Continue\r\n\r\n' + MOVED + BARE_CR + CLOSE),
    'hints-301-cr': ('origin', True, b'HTTP/1.1 103 Early Hints\r\nLink: </x>; rel=preload\r\n\r\n'
                     + MOVED + BARE_CR + CLOSE),
    'hints-forged-500': ('origin', False, b'HTTP/1.1 103 Early Hints\r\n' + FORGED_STATUS + b'\r\n'
                         b'HTTP/1.1 500 Internal Server Error\r\n' + LOCATION + CLOSE),
    'hints-forged-200-cr': ('origin', False, b'HTTP/1.1 103 Early Hints\r\n' + FORGED_STATUS + b'\r\n'
                            b'HTTP/1.1 200 OK\r\nContent-Type: application/x-git-upload-pack-advertisement\r\n'
                            + LOCATION + BARE_CR + CLOSE),
    'connect-302-relative': ('proxy', False, b'HTTP/1.1 302 Found\r\n' + LOCATION + CLOSE),
    'connect-302-absolute': ('proxy', False, b'HTTP/1.1 302 Found\r\nLocation: http://portal.example/login\r\n' + CLOSE),
    'tunnel-301': ('tunnel', True, MOVED + CLOSE),
    'tunnel-301-cr': ('tunnel', True, MOVED + BARE_CR + CLOSE),
}


def _read_head(conn) -> bytes | None:
    data = b''
    while b'\r\n\r\n' not in data:
        chunk = conn.recv(65536)
        if not chunk:
            return None
        data += chunk
    return data


class _RawEndpoint:
    """Loopback endpoint giving every request one canned answer, optionally over TLS or as a CONNECT tunnel."""

    def __init__(self, answer: bytes, *, tls=None, tunnel_to: int | None = None):
        import socket

        self.answer, self.tls, self.tunnel_to = answer, tls, tunnel_to
        self.requests: list[str] = []
        self.sock = socket.create_server(('127.0.0.1', 0))
        self.port = self.sock.getsockname()[1]
        threading.Thread(target=self._serve, daemon=True).start()

    def _serve(self):
        while True:
            try:
                conn, _ = self.sock.accept()
            except OSError:
                return
            threading.Thread(target=self._handle, args=(conn,), daemon=True).start()

    def _handle(self, conn):
        import socket

        try:
            if self.tls is not None:
                conn = self.tls.wrap_socket(conn, server_side=True)
            head = _read_head(conn)
            if head is None:
                return
            self.requests.append(head.split(b'\r\n', 1)[0].decode('latin-1'))
            if self.tunnel_to is not None and head.startswith(b'CONNECT '):
                upstream = socket.create_connection(('127.0.0.1', self.tunnel_to))
                conn.sendall(b'HTTP/1.1 200 Connection established\r\n\r\n')
                relay = threading.Thread(target=self._copy, args=(upstream, conn), daemon=True)
                relay.start()
                self._copy(conn, upstream)
                relay.join(timeout=10)
                return
            conn.sendall(self.answer)
        except OSError:
            pass
        finally:
            conn.close()

    @staticmethod
    def _copy(source, target):
        try:
            while chunk := source.recv(65536):
                target.sendall(chunk)
        except OSError:
            pass
        finally:
            for side in (source, target):
                try:
                    side.shutdown(2)
                except OSError:
                    pass

    def close(self):
        self.sock.close()


def _tls_origin(tmp_path, host: str):
    """A server TLS context for ``host`` and the CA file that trusts it (generated, test-only)."""
    import ssl
    from datetime import UTC, datetime, timedelta

    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import ec
    from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID

    now = datetime.now(UTC)
    ca_key, leaf_key = ec.generate_private_key(ec.SECP256R1()), ec.generate_private_key(ec.SECP256R1())
    ca_name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, 'R9 test root')])
    ca = (x509.CertificateBuilder().subject_name(ca_name).issuer_name(ca_name).public_key(ca_key.public_key())
          .serial_number(1).not_valid_before(now - timedelta(hours=1)).not_valid_after(now + timedelta(days=1))
          .add_extension(x509.BasicConstraints(ca=True, path_length=None), critical=True)
          .add_extension(x509.KeyUsage(digital_signature=True, content_commitment=False, key_encipherment=False,
                                       data_encipherment=False, key_agreement=False, key_cert_sign=True,
                                       crl_sign=True, encipher_only=False, decipher_only=False), critical=True)
          .sign(ca_key, hashes.SHA256()))
    leaf = (x509.CertificateBuilder().subject_name(x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, host)]))
            .issuer_name(ca_name).public_key(leaf_key.public_key()).serial_number(2)
            .not_valid_before(now - timedelta(hours=1)).not_valid_after(now + timedelta(days=1))
            .add_extension(x509.SubjectAlternativeName([x509.DNSName(host)]), critical=False)
            .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
            .add_extension(x509.ExtendedKeyUsage([ExtendedKeyUsageOID.SERVER_AUTH]), critical=False)
            .sign(ca_key, hashes.SHA256()))
    pem = serialization.Encoding.PEM
    ca_file, chain, key = tmp_path / 'r9-ca.pem', tmp_path / 'r9-leaf.pem', tmp_path / 'r9-leaf.key'
    ca_file.write_bytes(ca.public_bytes(pem))
    chain.write_bytes(leaf.public_bytes(pem))
    key.write_bytes(leaf_key.private_bytes(pem, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()))
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.load_cert_chain(chain, key)
    key.unlink()
    return context, ca_file


# R9 B1 with real Git: the same rule as the fixed-trace test above, end to
# end, for the fetch, the clone and 'check --network'. The declared server
# reached directly answers after an interim response (100 Continue, or a 103
# whose first header is a forged 'HTTP/1.1 301 Moved: x'), or a proxy
# (http.proxy, https:// declaration) refuses CONNECT with a 302 carrying a
# relative or an absolute Location. Only the declared server's own final 3xx
# is a redirect: the 301 after 100 and after a 103 with a Link header, also
# with a later bare-CR header, and the 301 a TLS origin sends through an
# accepted CONNECT tunnel, are redirects with the canonical spelling
# (positive controls; libcurl 8.23.0 refuses the bare-CR rows after the
# status line, libcurl 8.7.1 passes them and Git reports the 301). The other
# rows keep their own error, after at most one repeat.
@pytest.mark.parametrize('case', list(_RAW))
@pytest.mark.parametrize('command', ['check-network', 'fetch', 'clone'])
def test_redirect_verdict_comes_only_from_the_declared_servers_final_answer(
    gitlab, tmp_path, csk_home, skills_root, monkeypatch, capsys, case, command,
):
    from csk import git_trust

    route, redirect, answer = _RAW[case]
    gitlab.publish('b', 'v1')
    repo_b = gitlab.clone(tmp_path / 'skills', 'b', NEW.format('b'))
    _projects(tmp_path, csk_home, skills_root, monkeypatch, app=[_b_declaration(OLD.format('b'))])
    for key in ('http_proxy', 'https_proxy', 'HTTP_PROXY', 'HTTPS_PROXY', 'all_proxy', 'ALL_PROXY',
                'no_proxy', 'NO_PROXY'):
        monkeypatch.delenv(key, raising=False)
    endpoints = []
    if route == 'origin':
        origin = _RawEndpoint(answer)
        endpoints.append(origin)
        config = f'[url "http://127.0.0.1:{origin.port}/"]\n insteadOf = https://gitlab.test/\n'
        answered = origin
    elif route == 'proxy':
        proxy = _RawEndpoint(answer)
        endpoints.append(proxy)
        config = f'[http]\n proxy = http://127.0.0.1:{proxy.port}\n'
        answered = proxy
    else:
        context, ca_file = _tls_origin(tmp_path, 'gitlab.test')
        monkeypatch.setenv('CSK_GIT_CA_BUNDLE', str(ca_file))
        git_trust._VALIDATED.clear()
        origin = _RawEndpoint(answer, tls=context)
        proxy = _RawEndpoint(b'', tunnel_to=origin.port)
        endpoints += [origin, proxy]
        config = f'[http]\n proxy = http://127.0.0.1:{proxy.port}\n'
        answered = origin
    gitconfig = tmp_path / 'r9-gitconfig'
    gitconfig.write_text(config)
    monkeypatch.setenv('GIT_CONFIG_GLOBAL', str(gitconfig))
    destination = tmp_path / 'fresh' / 'b'
    try:
        if command == 'check-network':
            rc = cli.main(['check', '--canonical-urls', '--network', 'app'])
            output = capsys.readouterr()
            shown = output.out + output.err
        else:
            with pytest.raises(git_ops.GitError) as failed:
                if command == 'fetch':
                    git_ops.fetch_repo(repo_b, OLD.format('b'))
                else:
                    git_ops.clone_repo(OLD.format('b'), destination)
            shown = str(failed.value)
    finally:
        for endpoint in endpoints:
            endpoint.close()
    expected_line = 'CONNECT gitlab.test:443 HTTP/1.1' if route == 'proxy' else \
        'GET /team/b/info/refs?service=git-upload-pack HTTP/1.1'
    assert set(answered.requests) == {expected_line}, answered.requests
    if command == 'check-network':
        assert len(answered.requests) == 1
    elif case == 'hints-forged-200-cr':
        # libcurl 8.7.1 passes the bare CR and Git reports a protocol error
        # without the 'unable to access' frame, so it makes no repeat.
        assert len(answered.requests) in (1, 2)
    else:
        assert len(answered.requests) == 2
    assert not destination.exists()
    if redirect:
        assert f"declared URL {OLD.format('b')!r} is not canonical" in shown, shown
        assert f"same repository at {NEW.format('b')!r}" in shown
        assert command != 'check-network' or rc == cli.EXIT_PARTIAL_FAIL
        return
    assert 'not canonical' not in shown and 'HTTP redirect' not in shown, shown
    if command == 'check-network':
        assert rc == cli.EXIT_OK, shown
        assert f"warning: not verified {OLD.format('b')!r}" in shown
    elif command == 'fetch':
        assert shown.endswith('; verify the declared endpoint and authentication')
    else:
        assert shown.startswith(f"git clone failed for {OLD.format('b')} -> {destination}: ")


# R7 N2: one project's install verdict does not depend on the declaration
# order in its Skillfile. 'app' declares b (.git) and a, and a's spec at v1
# requires b with the old spelling; nothing is cloned yet. In both orders,
# also as --dry-run, the command refuses b with the same conflict line and
# installs nothing. bd9eef1 installed with mixed spellings when b came first
# (install and install --dry-run, b-first rows). 'upgrade' already refused
# in both orders (control), and with one spelling (aligned control) every
# command succeeds in both orders and b's clone keeps the .git origin.
@pytest.mark.parametrize('mixed', [True, False])
@pytest.mark.parametrize('order', ['b-first', 'a-first'])
@pytest.mark.parametrize('command', ['install', 'install --dry-run', 'upgrade'])
def test_project_verdict_does_not_depend_on_declaration_order(
    gitlab, tmp_path, csk_home, skills_root, monkeypatch, capsys, mixed, order, command,
):
    gitlab.publish('b', 'v2')
    gitlab.publish('a', 'v1', {'csk-skill.json': _spec({'b': (OLD if mixed else NEW).format('b')})})
    declarations = {'a': {'name': 'a', 'git': NEW.format('a'), 'tag': 'v1'}, 'b': _b_declaration(NEW.format('b'))}
    names = ['b', 'a'] if order == 'b-first' else ['a', 'b']
    _, paths = _projects(tmp_path, csk_home, skills_root, monkeypatch, app=[declarations[n] for n in names])

    rc = cli.main([*command.split(), 'app'])
    output = capsys.readouterr()

    installed = paths['app'] / '.agents' / 'skills'
    if not mixed:
        assert rc == cli.EXIT_OK, output.err
        assert 'is declared with 2 different URL spellings' not in output.err
        if command != 'install --dry-run':
            assert (installed / 'a').exists() and (installed / 'b').exists()
            assert _origin(skills_root / 'b') == NEW.format('b')
        return
    assert rc == cli.EXIT_PARTIAL_FAIL, output.err
    expected = _expected_conflict({
        OLD.format('b'): ["the skill spec of 'a' at tag v1 (via app -> a -> b)"],
        NEW.format('b'): [f"{paths['app'] / 'Skillfile.json'} (project app)"],
    })
    assert _conflict(output.err) == expected
    assert not (installed / 'a').exists() and not (installed / 'b').exists()
