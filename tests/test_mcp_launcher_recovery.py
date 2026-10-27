"""Complete CLI recovery preserves launcher trust and reports durable outcomes."""
from __future__ import annotations

import json
import os
import shutil
import signal
import stat
import subprocess
import sys
from pathlib import Path

import pytest

from csk import mcp_launchers as launchers
from csk.mcp_declarations import parse_mcp_servers
from test_mcp_install_acceptance import _case, _snapshot, _assert_seed_installed, SURFACES
from test_mcp_launchers import bundle, declaration


CHILD = r'''
import json, os, signal, sys
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch
from csk import cli, mcp_launchers as launchers
from csk.sources import publish
from test_mcp_launchers import good_verification

request = json.loads(sys.argv[1])
original = publish.TransactionEngine
reached = set()
def factory(home, **kwargs):
    def fault(event, target):
        selected = request.get('target')
        if (event == request.get('point')
            and (selected is None or (target is not None and target.target_class == selected))
            and event not in reached):
            reached.add(event)
            print('RECOVERY_BOUNDARY', event, flush=True)
            if request.get('exit'):
                os._exit(86)
            if request.get('kill'):
                os.kill(os.getpid(), signal.SIGKILL)
            raise OSError('injected cleanup failure')
        if request.get('rollback'):
            if event == 'after_install' and target.target_class == 'mcp-native':
                raise OSError('injected commit failure')
            if event == request['rollback']:
                raise OSError('injected rollback failure')
    return original(home, fault_hook=fault, **kwargs)

def approve(prompt):
    print('RECOVERY_APPROVAL', flush=True)
    return 'yes'

with patch.object(launchers, 'macos_roots', lambda: (Path(request['apps']),)), \
     patch.object(launchers, '_platform', lambda: request.get('platform', 'macos')), \
     patch.object(publish, 'TransactionEngine', factory), \
     patch('builtins.input', approve), \
     patch.object(sys.stdin, 'isatty', lambda: True), \
     patch.object(sys.stdout, 'isatty', lambda: True):
    if request.get('platform') in {'windows', 'linux'}:
        from types import SimpleNamespace
        launchers._windows_roots = lambda: {
            '%PROGRAMFILES%': Path(request['apps']),
            '%PROGRAMFILES(X86)%': Path(request['apps']) / 'unused-x86',
            '%LOCALAPPDATA%': Path(request['apps']) / 'unused-user'}
        launchers._linux_root = lambda *a: Path(request['apps'])
        launchers.linux_uid = lambda: 1000
        launchers.linux_metadata = lambda path: SimpleNamespace(st_uid=0,
            st_mode=0o040755 if path.is_dir() else 0o100755)
        launchers.verify_windows = lambda *a: launchers.WindowsVerification(
            'invalid' if request.get('untrusted') else 'valid', 'Example Inc')
        launchers.linux_package_owned = lambda *a: not request.get('untrusted')
    if not request.get('native'):
        launchers.verify_macos = lambda *a: replace(good_verification(), signature_valid=not request.get('untrusted'))
    if request.get('changed'):
        resolve = launchers.resolve_launcher
        launchers.resolve_launcher = lambda *a, **kw: replace(resolve(*a, **kw), **request['changed'])
    sys.exit(cli.main(request['argv']))
'''


def _invoke(case, apps, argv=None, **fault):
    environment = dict(case.env)
    environment['PYTHONPATH'] += os.pathsep + str(Path(__file__).resolve().parent)
    return subprocess.run(
        [sys.executable, '-c', CHILD, json.dumps(dict(
            apps=str(apps), argv=argv or ['install', 'app', '--fix-gitignore'], **fault,
        ))], cwd=case.project, env=environment, capture_output=True, text=True, timeout=180,
    )


def _setup(root, existing=False, *, ignore_mode='gitignore'):
    case = _case(root / 'case', ignore_mode=ignore_mode)
    case.declare(declaration())
    apps = root / 'Applications'
    bundle(apps)
    argv = ['install', 'app', '--fix-gitignore']
    if existing:
        result = _invoke(case, apps)
        assert result.returncode == 0, result.stdout + result.stderr
        source = case.project / 'source/seed/SKILL.md'
        source.write_bytes(source.read_bytes() + b'\nUpdated seed.\n')
        # Upgrade must enter the recovery transaction with writable shared syntax;
        # the installed gitignore block retains this fixture's existing policy.
        case.skillfile.pop('ignore_mode', None)
        raw = declaration()
        raw['app']['server']['args'] = ['--updated']
        case.declare(raw)
        if os.name != 'nt':
            (case.home / 'state/mcp/consent').chmod(0o750)
        argv[0] = 'upgrade'
    return case, apps, argv


def _journals(case):
    return list((case.home / 'state/transactions/v1').glob('*.json*'))


# Every exposed committed-cleanup boundary reports the installed state and a
# retained recovery journal, including removal tombs. A retry completes cleanup
# without losing prior snapshots. This does not simulate an unwritable disk.
@pytest.mark.parametrize('existing', [False, True])
@pytest.mark.parametrize('point', [
    'before_cleanup', 'cleanup_journaled', 'sidecar_tombed',
    'before_sidecar_mode_writable', 'sidecar_mode_writable',
    'before_journal_tomb', 'journal_tombed', 'before_journal_unlink',
])
def test_cleanup_failure_reports_committed_state(tmp_path, existing, point):
    case, apps, argv = _setup(tmp_path, existing)
    result = _invoke(case, apps, argv, point=point)
    text = result.stdout + result.stderr
    assert result.returncode == 1 and 'RECOVERY_BOUNDARY' in text, text
    assert 'transaction_cleanup_deferred' in text, text
    assert 'Installation committed' in text and 'rerun csk install' in text
    assert 'blocked by' not in text and 'refused:' not in text
    assert 'live=desired' in text and 'mcp-native/' in text and '04-source-record/' in text
    _assert_seed_installed(case)
    assert all((case.project / path).is_file() for _, path in SURFACES)
    assert len(list((case.home / 'source-v1').rglob('record.json'))) == (2 if existing else 1)
    assert _journals(case)
    if os.name != 'nt':
        assert stat.S_IMODE((case.home / 'state/mcp/consent').stat().st_mode) == 0o700
    result = _invoke(case, apps, argv)
    assert result.returncode == 0, result.stdout + result.stderr
    assert not _journals(case)


# A rollback interrupted before/after restoring a target, or while discarding
# rollback sidecars, reports partial state and retains a journal. Retry restores
# the prior generation before installing the valid requested generation.
@pytest.mark.parametrize('existing', [False, True])
@pytest.mark.parametrize('point', [
    'after_restore', 'cleanup_journaled', 'sidecar_tombed',
    'before_sidecar_mode_writable', 'sidecar_mode_writable',
    'before_journal_tomb', 'journal_tombed', 'before_journal_unlink',
])
def test_rollback_failure_reports_recovery_state(tmp_path, point, existing):
    case, apps, argv = _setup(tmp_path, existing=existing)
    result = _invoke(case, apps, argv, rollback=point)
    text = result.stdout + result.stderr
    assert result.returncode == 1, text
    assert 'transaction_rollback_deferred' in text and 'rerun csk install' in text, text
    assert 'injected commit failure' in text and 'injected rollback failure' in text, text
    assert 'journal=' in text and 'live=' in text and _journals(case)
    assert 'blocked by' not in text and 'refused:' not in text
    result = _invoke(case, apps, argv)
    assert result.returncode == 0, result.stdout + result.stderr
    assert not _journals(case)
    _assert_seed_installed(case)


# Actual SIGKILL after the source record bypasses all finally blocks. Invalid
# trust or a changed approval subject must pause replay before native/consent
# writes, retaining byte-identical interrupted state for a trusted retry.
@pytest.mark.skipif(os.name == 'nt', reason='POSIX process-death control')
@pytest.mark.parametrize('change', [
    {'untrusted': True}, {'changed': {'path': '/different/helper'}},
    {'changed': {'publisher': 'different publisher'}},
    {'changed': {'trust': 'unverified'}},
])
@pytest.mark.parametrize('existing', [False, True])
def test_interrupted_launcher_revalidated_before_replay(tmp_path, existing, change):
    case, apps, argv = _setup(tmp_path, existing)
    native_before = {path: (case.project / path).read_bytes() if (case.project / path).exists() else None
                     for _, path in SURFACES}
    killed = _invoke(case, apps, argv, point='after_install', target='04-source-record', kill=True)
    assert killed.returncode == -signal.SIGKILL, killed.stdout + killed.stderr
    assert _journals(case)
    interrupted = _snapshot(case.project, case.home)
    result = _invoke(case, apps, argv, **change)
    text = result.stdout + result.stderr
    expected = 'mcp_launcher_untrusted' if change.get('untrusted') else 'mcp_launcher_changed'
    assert result.returncode == 1 and expected in text, text
    assert _snapshot(case.project, case.home) == interrupted
    assert 'rerun csk install' in text and 'journal=' in text, text
    assert {path: (case.project / path).read_bytes() if (case.project / path).exists() else None
            for _, path in SURFACES} == native_before
    result = _invoke(case, apps, argv)
    assert result.returncode == 0, result.stdout + result.stderr
    assert not _journals(case)
    _assert_seed_installed(case)


# A valid content-only update keeps the same consent subject. The verifier is
# injected here; the separate native tamper case proves invalid signatures
# still block replay. No new approval is requested by the resumed CLI.
@pytest.mark.skipif(os.name == 'nt', reason='POSIX process-death control')
def test_verified_content_update_recovers_without_reapproval(tmp_path):
    case, apps, argv = _setup(tmp_path)
    killed = _invoke(case, apps, argv, point='after_install', target='04-source-record', kill=True)
    assert killed.returncode == -signal.SIGKILL
    helper = apps / 'Example.app/Contents/MacOS/mcp-server'
    original = helper.read_bytes()
    helper.write_bytes(original + b'\nVerified update.\n')
    result = _invoke(case, apps, argv)
    assert result.returncode == 0, result.stdout + result.stderr
    assert 'RECOVERY_APPROVAL' not in result.stdout
    assert not _journals(case)
    _assert_seed_installed(case)


# With unchanged trust the same process-death boundaries resume successfully,
# including crashes after native/consent publication and during final cleanup.
@pytest.mark.skipif(os.name == 'nt', reason='POSIX process-death control')
@pytest.mark.parametrize('existing', [False, True])
@pytest.mark.parametrize('point,target', [
    ('prepared', None), ('after_install', '03-source-tree'),
    ('after_install', '04-source-record'), ('target_committed', 'mcp-consent'),
    ('after_install', 'mcp-native'), ('before_cleanup', None),
])
def test_valid_launcher_crash_recovery(tmp_path, existing, point, target):
    case, apps, argv = _setup(tmp_path, existing)
    killed = _invoke(case, apps, argv, point=point, target=target, kill=True)
    assert killed.returncode == -signal.SIGKILL, killed.stdout + killed.stderr
    assert _journals(case)
    result = _invoke(case, apps, argv)
    assert result.returncode == 0, result.stdout + result.stderr
    assert not _journals(case)
    assert all((case.project / path).is_file() for _, path in SURFACES)
    _assert_seed_installed(case)
    assert len(list((case.home / 'source-v1').rglob('record.json'))) == (2 if existing else 1)


# Optional trust fields omitted on other platforms stay omitted in the durable
# guard. macOS replay validates the entire portable declaration grammar too.
@pytest.mark.skipif(os.name == 'nt', reason='POSIX process-death control')
def test_recovery_preserves_optional_platform_fields(tmp_path):
    case, apps, argv = _setup(tmp_path)
    raw = declaration()
    raw['app']['server']['launcher'].update({
        'windows': {'install_dir': '%PROGRAMFILES%\\Example', 'executable': 'helper.exe', 'publisher': 'Example'},
        'linux': {'path': '/usr/bin/example-helper'},
    })
    case.declare(raw)
    killed = _invoke(case, apps, argv, point='prepared', kill=True)
    assert killed.returncode == -signal.SIGKILL, killed.stdout + killed.stderr
    journal, = _journals(case)
    guard = next(target['publication_recheck']['commit_guard']
                 for target in json.loads(journal.read_bytes())['targets']
                 if 'commit_guard' in (target['publication_recheck'] or {}))
    saved = guard['observations'][0]['launcher']
    assert 'trust' not in saved['windows'] and 'trust' not in saved['linux']
    result = _invoke(case, apps, argv)
    assert result.returncode == 0, result.stdout + result.stderr
    assert not _journals(case)


# A launcher journal without an approved observation cannot reconstruct consent
# from a changed live manifest. Retain it unchanged; restoring its actual saved
# guard (rather than approving a new one) permits the valid recovery control.
@pytest.mark.skipif(os.name == 'nt', reason='POSIX process-death control')
@pytest.mark.parametrize('point,target', [('prepared', None), ('after_install', 'mcp-native'), ('after_install', '04-source-record')])
@pytest.mark.parametrize('previous_launcher', [False, True])
def test_missing_saved_observation_pauses_recovery(tmp_path, point, target, previous_launcher):
    case, apps, argv = _setup(tmp_path, existing=previous_launcher)
    if previous_launcher:
        case.declare({'app': 'https://mcp.example.invalid/mcp'})
    killed = _invoke(case, apps, argv, point=point, target=target, kill=True)
    assert killed.returncode == -signal.SIGKILL
    path, = _journals(case)
    raw = json.loads(path.read_bytes())
    payload = next(target['publication_recheck'] for target in raw['targets']
                   if 'commit_guard' in (target['publication_recheck'] or {}))
    guard = payload.pop('commit_guard')
    path.write_text(json.dumps(raw, ensure_ascii=False, sort_keys=True, separators=(',', ':')) + '\n')
    original = (case.project / 'Skillfile.json').read_bytes()
    case.declare({})
    before = _snapshot(case.project, case.home)
    result = _invoke(case, apps, argv)
    assert result.returncode == 1 and 'mcp_recovery_observation_missing' in result.stdout + result.stderr
    assert _snapshot(case.project, case.home) == before
    assert f'csk recover --rollback {raw["transaction_id"]}' in result.stdout + result.stderr
    (case.project / 'Skillfile.json').write_bytes(original)
    payload['commit_guard'] = guard
    path.write_text(json.dumps(raw, ensure_ascii=False, sort_keys=True, separators=(',', ':')) + '\n')
    result = _invoke(case, apps, argv)
    assert result.returncode == 0, result.stdout + result.stderr
    assert not _journals(case)


@pytest.fixture(scope='module')
def earlier_launcher_source(tmp_path_factory):
    from cli_golden_v1_support import extract_release_source

    return extract_release_source(
        '20a158c2bfa927dcbe8988971c810bf6d1446a43',
        tmp_path_factory.mktemp('earlier-launcher-cli'),
        cwd=Path(__file__).resolve().parents[1],
    )


# The actual earlier CLI saves guardless ledger-only or consent/ledger plans
# when native bytes do not change. Both must pause unchanged even after trust
# is restored; explicit rollback then a fresh plan succeeds. Matching current
# writers save a guard, refuse invalid trust unchanged and recover valid trust.
@pytest.mark.skipif(os.name == 'nt', reason='POSIX process-death control')
@pytest.mark.parametrize('writer', ['earlier', 'current'])
@pytest.mark.parametrize('publisher_change', [False, True])
def test_metadata_only_launcher_recovery(tmp_path, earlier_launcher_source, writer, publisher_change):
    current = Path(__file__).resolve().parents[1] / 'src'
    source = earlier_launcher_source if writer == 'earlier' else current
    case, apps, _ = _setup(tmp_path, ignore_mode=None if writer == 'earlier' else 'gitignore')
    script = "from csk import cli\nprint('WRITER_IMPLEMENTATION', cli.__file__, flush=True)\n" + CHILD.replace(
        "signature_valid=not request.get('untrusted')",
        "signature_valid=not request.get('untrusted'), team_id=a[2].team_id",
    )

    def invoke(implementation, **request):
        environment = dict(case.env, PYTHONPATH=str(implementation) + os.pathsep + str(Path(__file__).resolve().parent))
        payload = {'apps': str(apps), 'argv': ['install', 'app', '--fix-gitignore'], **request}
        result = subprocess.run(
            [sys.executable, '-c', script, json.dumps(payload)],
            cwd=case.project, env=environment, capture_output=True, text=True, timeout=180,
        )
        assert str(implementation / 'csk/cli.py') in result.stdout, result.stdout + result.stderr
        return result

    installed = invoke(source)
    assert installed.returncode == 0, installed.stdout + installed.stderr
    native = {path: (case.project / path).read_bytes() for _, path in SURFACES}
    helper = apps / 'Example.app/Contents/MacOS/mcp-server'
    helper.write_bytes(helper.read_bytes() + b'\nVerified helper update.\n')
    if publisher_change:
        changed = declaration()
        changed['app']['server']['launcher']['macos']['team_id'] = 'OTHER12345'
        case.declare(changed)
    killed = invoke(source, point='prepared', kill=True)
    assert killed.returncode == -signal.SIGKILL, killed.stdout + killed.stderr
    path, = _journals(case)
    raw = json.loads(path.read_bytes())
    assert [target['target_class'] for target in raw['targets']] == (
        ['mcp-consent', 'mcp-ledger'] if publisher_change else ['mcp-ledger']
    )
    guards = [target['publication_recheck']['commit_guard'] for target in raw['targets']
              if 'commit_guard' in (target.get('publication_recheck') or {})]
    assert len(guards) == (0 if writer == 'earlier' else 1)
    ledger = next(target for target in raw['targets'] if target['target_class'] == 'mcp-ledger')
    saved = json.loads(Path(ledger['staged_path']).read_bytes())
    assert saved['entries'] and all(entry['launcher_sha256'] for entry in saved['entries'])
    interrupted = _snapshot(case.project, case.home)
    refused = invoke(current, untrusted=True)
    text = refused.stdout + refused.stderr
    code = 'mcp_recovery_observation_missing' if writer == 'earlier' else 'mcp_launcher_untrusted'
    assert refused.returncode == 1 and code in text, text
    assert _snapshot(case.project, case.home) == interrupted and _journals(case) == [path]
    assert native == {name: (case.project / name).read_bytes() for _, name in SURFACES}
    if writer == 'earlier':
        assert f'csk recover --rollback {raw["transaction_id"]}' in text
        paused = invoke(current)
        assert paused.returncode == 1 and code in paused.stdout + paused.stderr
        assert _snapshot(case.project, case.home) == interrupted and _journals(case) == [path]
        rollback = invoke(current, argv=['recover', '--rollback', raw['transaction_id']])
        assert rollback.returncode == 0 and not _journals(case), rollback.stdout + rollback.stderr
        assert 'Empty directories may remain' in rollback.stdout
        assert 'previous directory modes are not restored' in rollback.stdout
    valid = invoke(current)
    assert valid.returncode == 0 and not _journals(case), valid.stdout + valid.stderr
    assert native == {name: (case.project / name).read_bytes() for _, name in SURFACES}


# The reviewer's real codesign interleaving: verify original and copy, kill
# after the source record, tamper only the copy, refuse replay, restore and
# recover. Neither the original app nor its helper is modified or executed.
@pytest.mark.csk_e2e_native
@pytest.mark.skipif(sys.platform != 'darwin', reason='native macOS signature check')
def test_native_crash_replay_revalidates_launcher(tmp_path):
    app = Path('/Applications/Proxyman.app')
    if not app.is_dir():
        pytest.skip('Proxyman is required for the native positive control')
    case = _case(tmp_path / 'case')
    raw = declaration({'macos': {'bundle_id': 'com.proxyman.NSProxy', 'team_id': '3X57WP8E8V',
                               'executable': 'Contents/MacOS/mcp-server'}})
    case.declare(raw)
    spec = parse_mcp_servers(raw, invocation_scope='project', target_agents=['claude_code'])['app'].server.launcher
    original = launchers.resolve_launcher(spec, roots=(app.parent,))
    apps = tmp_path / 'Applications'
    apps.mkdir()
    copied = apps / app.name
    shutil.copytree(app, copied, symlinks=True)
    assert launchers.resolve_launcher(spec, roots=(apps,)).content_sha256 == original.content_sha256
    killed = _invoke(case, apps, native=True, point='after_install', target='04-source-record', kill=True)
    assert killed.returncode == -signal.SIGKILL, killed.stdout + killed.stderr
    assert not any((case.project / path).exists() for _, path in SURFACES)
    interrupted = _snapshot(case.project, case.home)
    helper = copied / 'Contents/MacOS/mcp-server'
    with helper.open('r+b') as stream:
        stream.seek(4096)
        byte = stream.read(1)
        stream.seek(4096)
        stream.write(bytes([byte[0] ^ 1]))
    with pytest.raises(launchers.McpLauncherError) as failure:
        launchers.resolve_launcher(spec, roots=(apps,))
    assert failure.value.code == 'mcp_launcher_untrusted'
    result = _invoke(case, apps, native=True)
    assert result.returncode == 1 and 'mcp_launcher_untrusted' in result.stdout + result.stderr
    assert _snapshot(case.project, case.home) == interrupted
    assert _journals(case)
    with helper.open('r+b') as stream:
        stream.seek(4096)
        stream.write(byte)
    result = _invoke(case, apps, native=True)
    assert result.returncode == 0, result.stdout + result.stderr
    assert not _journals(case)
    assert all((case.project / path).is_file() for _, path in SURFACES)
    _assert_seed_installed(case)
    assert launchers.resolve_launcher(spec, roots=(app.parent,)) == original


# Explicit rollback uses frozen publication boundaries even though it skips the
# forward launcher guard. A moved project pauses restoration; moving it back
# permits the documented command, then a new install succeeds.
@pytest.mark.skipif(os.name == 'nt', reason='POSIX process-death control')
def test_explicit_rollback_preserves_publication_boundaries(tmp_path):
    case, apps, argv = _setup(tmp_path)
    killed = _invoke(case, apps, argv, point='after_install', target='mcp-native', kill=True)
    assert killed.returncode == -signal.SIGKILL
    path, = _journals(case)
    transaction_id = json.loads(path.read_bytes())['transaction_id']
    moved = case.project.with_name('moved-project')
    case.project.rename(moved)
    before = _snapshot(moved)
    def rollback():
        return subprocess.run([sys.executable, '-m', 'csk', 'recover', '--rollback', transaction_id],
                              cwd=case.home, env=case.env, text=True, capture_output=True, timeout=60)
    refused = rollback()
    assert refused.returncode == 1 and 'source_output_overlap' in refused.stdout + refused.stderr
    assert _journals(case) and _snapshot(moved) == before
    moved.rename(case.project)
    recovered = rollback()
    assert recovered.returncode == 0, recovered.stdout + recovered.stderr
    assert not _journals(case)
    assert not any((case.project / native_path).exists() for _, native_path in SURFACES)
    result = _invoke(case, apps, argv)
    assert result.returncode == 0, result.stdout + result.stderr


# Portable process death after the source record retains real journals. Saved
# Windows/Linux observations are reverified before any replay publication; a
# changed verdict leaves every interrupted byte untouched, while a valid retry
# (or an informational content update) completes without asking consent again.
@pytest.mark.parametrize('platform', ['windows', 'linux'])
@pytest.mark.parametrize('change', ['verdict', 'content', 'unchanged'])
def test_platform_observation_replay(tmp_path, platform, change):
    from dataclasses import asdict
    from test_mcp_launcher_platforms import helper, windows, linux
    case = _case(tmp_path / 'case')
    apps = tmp_path / 'apps'
    path = helper(apps, 'Programs/Example/mcp-server.exe' if platform == 'windows' else 'Example/bin/server')
    spec = windows('%ProgramFiles%') if platform == 'windows' else linux()
    entry = {k: v for k,v in asdict(getattr(spec, platform)).items() if v is not None}
    case.declare({'app': {'server': {'transport': 'stdio', 'launcher': {platform: entry}, 'args': []}}})
    native_before = {rel: (case.project / rel).read_bytes() if (case.project / rel).exists() else None
                     for _, rel in SURFACES}
    killed = _invoke(case, apps, platform=platform, point='after_install', target='04-source-record', exit=True)
    assert killed.returncode == 86 and 'RECOVERY_BOUNDARY' in killed.stdout, killed.stdout + killed.stderr
    assert _journals(case)
    interrupted = _snapshot(case.project, case.home)
    if change == 'content': path.write_bytes(path.read_bytes() + b'\nInformational update.\n')
    result = _invoke(case, apps, platform=platform, untrusted=change == 'verdict')
    if change == 'verdict':
        expected = 'mcp_launcher_untrusted' if platform == 'windows' else 'mcp_launcher_changed'
        assert result.returncode == 1 and expected in result.stdout + result.stderr
        assert _snapshot(case.project, case.home) == interrupted
        assert {rel: (case.project / rel).read_bytes() if (case.project / rel).exists() else None
                for _, rel in SURFACES} == native_before
        result = _invoke(case, apps, platform=platform)
    assert result.returncode == 0, result.stdout + result.stderr
    assert 'RECOVERY_APPROVAL' not in result.stdout
    assert not _journals(case)
    _assert_seed_installed(case)
