"""New platform observations exercise existing consent/status contracts."""
from __future__ import annotations

from dataclasses import replace
from types import SimpleNamespace

import pytest

from csk import mcp_launchers as gate, mcp_project
from test_mcp_project_install import project_cli, install
from test_mcp_launcher_lifecycle import approve, status
from test_mcp_install_acceptance import _snapshot
from test_mcp_launcher_platforms import helper, windows, linux

pytestmark = pytest.mark.usefixtures('stable_env')


@pytest.fixture(params=[('windows', 'verified'), ('windows', 'signed-user-writable'),
    ('windows', 'unverified'), ('linux', 'verified'), ('linux', 'unverified')])
def platform_cli(project_cli, tmp_path, monkeypatch, request):
    from dataclasses import asdict
    platform, level = request.param
    root, home, declare = project_cli
    apps = tmp_path / 'apps'
    path = helper(apps, 'Programs/Example/mcp-server.exe' if platform == 'windows' else 'Example/bin/server')
    monkeypatch.setattr(gate, '_platform', lambda: platform)
    monkeypatch.setattr(gate, '_windows_roots', lambda: {
        '%PROGRAMFILES%': apps if level == 'verified' else tmp_path / 'machine',
        '%PROGRAMFILES(X86)%': tmp_path / 'machine-x86',
        '%LOCALAPPDATA%': tmp_path / 'user' if level == 'verified' else apps})
    monkeypatch.setattr(gate, '_linux_root', lambda *a: apps)
    monkeypatch.setattr(gate, 'linux_uid', lambda: 1000)
    monkeypatch.setattr(gate, 'linux_metadata', lambda p: SimpleNamespace(st_uid=0,
        st_mode=(0o100755 if p == path else 0o040755)))
    state = {'bad': False}
    monkeypatch.setattr(gate, 'verify_windows', lambda *a: gate.WindowsVerification(
        'invalid' if state['bad'] else 'unsigned' if level == 'unverified' else 'valid', 'Example Inc'))
    monkeypatch.setattr(gate, 'linux_package_owned', lambda *a: level == 'verified' and not state['bad'])
    spec = windows('%ProgramFiles%' if level == 'verified' else '%LOCALAPPDATA%') if platform == 'windows' else linux()
    entry = getattr(spec, platform)
    raw = {'app': {'server': {'transport': 'stdio', 'launcher': {
        platform: {k: v for k,v in asdict(entry).items() if v is not None}}, 'args': []}}}
    declare(raw)
    return root, home, path, raw, state, platform, level


# Every new platform/trust subject installs with one local consent. Same/content
# changes remain clean; path/publisher changes and downgrades are unapproved.
@pytest.mark.parametrize('change', ['same', 'content', 'path', 'publisher', 'downgrade'])
def test_platform_consent_status(platform_cli, monkeypatch, capsys, change):
    root, home, path, raw, _, platform, level = platform_cli
    prompts = approve(monkeypatch)
    assert install() == 0
    output = capsys.readouterr().out
    expected = {'verified': 'per-machine installation' if platform == 'windows' else 'system package manager',
        'signed-user-writable': 'its owner can replace it',
        'unverified': 'cannot tell an update from a replacement'}[level]
    assert expected in output
    plan = mcp_project.plan_project(root, ['claude_code', 'codex_cli', 'cursor'], raw, home)
    original = plan.declarations['app'].server.resolved_launcher
    current = {'same': original, 'content': replace(original, content_sha256='a' * 64),
        'path': replace(original, path=str(path.with_name('other'))),
        'publisher': replace(original, publisher='different publisher'),
        'downgrade': replace(original, trust='unverified')}[change]
    monkeypatch.setattr(gate, 'resolve_launcher', lambda *a, **kw: current)
    before = _snapshot(root, home)
    unchanged = change in {'same', 'content'} or change == 'downgrade' and level == 'unverified'
    expected = 'updated (informational)' if change == 'content' else 'current' if unchanged else 'unapproved'
    assert status(capsys, '--check') == (0 if unchanged else 1, [expected] * 3)
    assert _snapshot(root, home) == before
    assert install() == 0
    capsys.readouterr()
    assert len(prompts) == (1 if unchanged else 2)


# Windows invalid/tampered signatures become untrusted even with the unsigned
# override; Linux package loss becomes unapproved and retains native/store bytes.
def test_platform_verdict_downgrade(platform_cli, monkeypatch, capsys):
    root, home, _, _, state, platform, level = platform_cli
    approve(monkeypatch)
    assert install() == 0
    capsys.readouterr()
    before = _snapshot(root, home)
    state['bad'] = True
    expected = 'untrusted' if platform == 'windows' else 'unapproved' if level == 'verified' else 'current'
    assert status(capsys, '--check') == (0 if expected == 'current' else 1, [expected] * 3)
    assert _snapshot(root, home) == before
