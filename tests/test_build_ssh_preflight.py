"""Local SSH metadata and read-only guidance; no real token coverage."""
from pathlib import Path
from types import SimpleNamespace
import subprocess

import pytest

from csk import build_ssh, cli, config, git_admission, installer
from csk.build_repository import LockedCommit, parse_repository_source
from csk.sources import transport


# Each case pairs refusal evidence with usable/unknown controls. No signatures
# are requested: mock metadata does not prove a real agent can sign.
@pytest.mark.parametrize(
    "encrypted,agent,listing,public,kind,provider,state,reason",
    [
        (True, False, None, False, 'ED25519', 'internal', 'unusable', 'passphrase'),
        (False, False, None, False, 'ED25519', 'internal', 'unknown', 'does not establish'),
        (None, False, None, False, 'ED25519', 'internal', 'unknown', 'does not establish'),
        (True, True, 'matching', False, 'ED25519', 'internal', 'unknown', 'listed'),
        (True, True, 'empty', False, 'ED25519', 'internal', 'unusable', 'passphrase'),
        (True, True, None, False, 'ED25519', 'internal', 'unknown', 'unknown'),
        (False, False, None, True, 'ED25519', 'internal', 'unusable', 'public key alone'),
        (False, True, 'matching', True, 'ED25519', 'internal', 'unknown', 'listed'),
        (False, False, None, False, 'ED25519-SK', '$SSH_SK_PROVIDER', 'unusable', 'provider'),
        (False, False, None, False, 'ED25519-SK', 'internal', 'unknown', 'does not establish'),
    ],
)
def test_key_evidence(monkeypatch, encrypted, agent, listing, public, kind, provider, state, reason):
    calls = []
    def probe(argv, **kwargs):
        calls.append(argv)
        if '-L' in argv:
            return None if listing is None else subprocess.CompletedProcess(argv, 1 if listing == 'empty' else 0, 'ssh-ed25519 AAAA', '')
        if '-l' in argv:
            return subprocess.CompletedProcess(argv, 0, f'256 SHA256:match label ({kind})', '')
        if '-y' in argv:
            return None if encrypted is None else subprocess.CompletedProcess(argv, 255 if encrypted else 0, '', 'incorrect passphrase' if encrypted else '')
        return subprocess.CompletedProcess(argv, 0, f'securitykeyprovider {provider}\n', '')
    monkeypatch.setattr(build_ssh, '_local_probe', probe)
    result = build_ssh.inspect_key(Path('/tools/ssh'), Path('/keys/key.pub' if public else '/keys/key'), Path('/agent') if agent else None)
    assert result.state == state
    assert reason in result.reason
    assert all(Path(argv[0]).parent == Path('/tools') for argv in calls)
    assert all('-P' in argv and argv[argv.index('-P') + 1] == '' for argv in calls if '-y' in argv)


# Failed probes cannot fabricate refusal, and the runner scrubs ambient secret,
# askpass and agent settings while disabling stdin and bounding each call.
def test_probe_failure_and_environment(monkeypatch):
    monkeypatch.setenv('SSH_ASKPASS', '/forbidden')
    monkeypatch.setenv('SSH_AUTH_SOCK', '/forbidden')
    def run(argv, **kwargs):
        assert kwargs['stdin'] == subprocess.DEVNULL
        assert kwargs['timeout'] == 5
        assert kwargs['env'] == {'LC_ALL': 'C'}
        raise subprocess.TimeoutExpired(argv, 5)
    monkeypatch.setattr(build_ssh, 'resolve_tool', lambda *a, **k: '/tools/ssh-keygen')
    monkeypatch.setattr(subprocess, 'run', run)
    assert build_ssh.inspect_key(Path('/tools/ssh'), Path('/keys/key')).state == 'unknown'


# A planted unusable verdict blocks both acquisition entry points before wrapper
# creation or network. The unknown control reaches the wrapper unchanged.
@pytest.mark.parametrize('resolve', [False, True])
@pytest.mark.parametrize('state', ['unusable', 'unknown'])
def test_preflight_precedes_network(monkeypatch, tmp_path, resolve, state):
    source = parse_repository_source('git@example.test:group/repo.git')
    creds = git_admission.OperatorSSHCredentials(identity=tmp_path / 'key')
    tool = git_admission.GitTool(Path('/git'), Path('/exec'), (), ssh=Path('/ssh'), ssh_credentials=creds)
    monkeypatch.setattr(git_admission, 'validate_git_tool', lambda *a, **k: None)
    monkeypatch.setattr(git_admission, 'validate_operator_ssh_credentials', lambda c: c)
    monkeypatch.setattr(build_ssh, 'inspect_key', lambda *a: build_ssh.KeyInspection(state, 'planted passphrase'))
    def wrapper(*a, **k):
        raise RuntimeError('wrapper reached')
    monkeypatch.setattr(git_admission, '_materialize_ssh_wrapper', wrapper)
    if state == 'unusable':
        with pytest.raises(git_admission.GitAdmissionError) as error:
            if resolve:
                git_admission.resolve_network_ref(source, 'branch', 'main', tool)
            else:
                git_admission.acquire_network(source, LockedCommit('sha1', '0' * 40), tool)
        assert error.value.code == git_admission.SSH_CREDENTIAL_UNUSABLE
        assert 'example.test/group/repo' in str(error.value)
        assert 'reason: planted passphrase' in str(error.value)
        assert 'next: run csk config build-ssh doctor' in str(error.value)
        assert transport._classify_git_error(error.value) == 'auth-unavailable'
        assert error.value.code in transport._KNOWN_SAFE_CODES
    else:
        with pytest.raises(RuntimeError, match='wrapper reached'):
            if resolve:
                git_admission.resolve_network_ref(source, 'branch', 'main', tool)
            else:
                git_admission.acquire_network(source, LockedCommit('sha1', '0' * 40), tool)


# Doctor is observational: forbid config writes, prompts and any probe beyond
# the bounded metadata runner. OS filtering and token caveats remain visible.
@pytest.mark.parametrize('platform,label', [('darwin', 'macOS:'), ('linux', 'Linux ('), ('win32', 'Windows (')])
def test_doctor_read_only_and_os_filter(monkeypatch, capsys, platform, label):
    monkeypatch.setattr(build_ssh.sys, 'platform', platform)
    monkeypatch.setattr(config, 'load_config', lambda: SimpleNamespace(build_ssh=()))
    def forbidden(*a, **k):
        pytest.fail('doctor attempted mutation or interaction')
    monkeypatch.setattr(config, 'save_config', forbidden)
    monkeypatch.setattr('builtins.input', forbidden)
    monkeypatch.setattr(build_ssh, 'resolve_tool', lambda *a: '/tools/ssh')
    monkeypatch.setattr(build_ssh, '_local_probe', lambda *a, **k: None)
    assert cli.main(['config', 'build-ssh', 'doctor']) == 0
    text = capsys.readouterr().out
    assert label in text
    assert text.count('macOS:') + text.count('Linux (') + text.count('Windows (') == 1
    assert 'unverified on a real token' in text
    assert 'ssh-add -t 1h' in text
    assert 'separate security matter' in text
    assert cli.main(['config', 'build-ssh', 'doctor', '--all-os']) == 0
    text = capsys.readouterr().out
    assert all(label in text for label in ['macOS:', 'Linux (', 'Windows ('])
    assert 'ssh-add -d <key>.pub' in text


# Regression: file-only menu choices must select the private sibling; an agent
# choice is separately allowed to pin the public file in test_build_ssh.py.
def test_file_only_menu_uses_private_sibling(monkeypatch):
    monkeypatch.setattr(build_ssh, 'discover_candidates', lambda: build_ssh.DiscoveredCandidates(public_keys=('/keys/work.pub',)))
    answers = iter(['1', '3'])
    monkeypatch.setattr('builtins.input', lambda *a: next(answers))
    rule, persist = installer._prompt_build_ssh_rule('skill', 'command', 'example.test/group/repo')
    assert rule.identity == '/keys/work'
    assert rule.agent is None
    assert not persist


# Help fixtures pin this new surface; a planted unrelated help edit must fail
# the same byte comparison, while the unchanged positive control passes.
@pytest.mark.parametrize('mutate', [False, True])
def test_doctor_help_golden(monkeypatch, capsys, mutate):
    monkeypatch.setenv('COLUMNS', '80')
    assert cli.main(['config', 'build-ssh', 'doctor', '--help']) == 0
    actual = capsys.readouterr().out
    expected = (Path(__file__).parent / 'fixtures/cli-golden-v1/config-build-ssh-doctor-help.stdout').read_text()
    if mutate:
        actual = actual.replace('--all-os', '--unrelated', 1)
        assert actual != expected
    else:
        assert actual == expected


# Apple launchd agent masks a listed hardware identity; a separate capable
# agent remains unknown rather than being rejected on OS alone.
@pytest.mark.parametrize('socket,state', [('/private/tmp/com.apple.launchd.test/Listeners', 'unusable'), ('/tmp/operator-agent', 'unknown')])
def test_macos_hardware_agent_mask(monkeypatch, socket, state):
    monkeypatch.setattr(build_ssh.sys, 'platform', 'darwin')
    def probe(argv, **kwargs):
        if '-l' in argv:
            return subprocess.CompletedProcess(argv, 0, '256 SHA256:test label (ED25519-SK)', '')
        if '-L' in argv:
            return subprocess.CompletedProcess(argv, 0, 'sk-ssh-ed25519@openssh.com AAAA', '')
        return None
    monkeypatch.setattr(build_ssh, '_local_probe', probe)
    result = build_ssh.inspect_key(Path('/tools/ssh'), Path('/keys/token'), Path(socket))
    assert result.state == state
    assert ('masks' in result.reason) == (state == 'unusable')


# Real OpenSSH metadata probes distinguish a throwaway encrypted key from its
# plain positive control without a server, terminal, agent or signature.
@pytest.mark.parametrize('passphrase,state', [('', 'unknown'), ('fixture-only', 'unusable')])
def test_real_key_metadata(tmp_path, passphrase, state):
    try:
        ssh = Path(build_ssh.resolve_tool('ssh'))
        keygen = build_ssh.resolve_tool('ssh-keygen', executable=str(ssh.parent / 'ssh-keygen'))
    except FileNotFoundError:
        pytest.skip('matching OpenSSH tools unavailable')
    identity = tmp_path / 'key'
    subprocess.run([keygen, '-q', '-t', 'ed25519', '-N', passphrase, '-f', str(identity)], check=True, stdin=subprocess.DEVNULL, capture_output=True, timeout=10)
    result = build_ssh.inspect_key(ssh, identity)
    assert result.state == state
    assert result.passphrase == bool(passphrase)
    assert result.key_kind == 'ED25519'
