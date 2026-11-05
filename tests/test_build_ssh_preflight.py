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
        (False, True, 'matching', False, 'ED25519-SK', '$SSH_SK_PROVIDER', 'unusable', 'provider'),
        (False, True, 'matching', False, 'ED25519-SK', 'internal', 'unknown', 'listed'),
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
        expected = {'LC_ALL': 'C'}
        if build_ssh.os.name == 'nt' and build_ssh.os.environ.get('SystemRoot'):
            expected['SystemRoot'] = build_ssh.os.environ['SystemRoot']
        assert kwargs['env'] == expected
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
def test_file_only_menu_uses_private_sibling(monkeypatch, tmp_path):
    private = tmp_path / "work"
    private.touch()
    monkeypatch.setattr(build_ssh, 'discover_candidates', lambda: build_ssh.DiscoveredCandidates(public_keys=(str(private) + '.pub',)))
    answers = iter(['1', '3'])
    monkeypatch.setattr('builtins.input', lambda *a: next(answers))
    rule, persist = installer._prompt_build_ssh_rule('skill', 'command', 'example.test/group/repo')
    assert rule.identity == str(private)
    assert rule.agent is None
    assert not persist


# Help fixtures pin this new surface; a planted unrelated help edit must fail
# the same byte comparison, while the unchanged positive control passes.
@pytest.mark.parametrize('mutate', [False, True])
@pytest.mark.parametrize('command,fixture,marker', [
    (['config', 'build-ssh', 'doctor', '--help'], 'config-build-ssh-doctor-help.stdout', '--all-os'),
    (['config', 'build-ssh', '--help'], 'config-build-ssh-help.stdout', '--agent auto'),
])
def test_doctor_help_golden(monkeypatch, capsys, mutate, command, fixture, marker):
    monkeypatch.setenv('COLUMNS', '80')
    assert cli.main(command) == 0
    actual = capsys.readouterr().out
    expected = (Path(__file__).parent / 'fixtures/cli-golden-v1' / fixture).read_text()
    if mutate:
        assert marker in actual
        actual = actual.replace(marker, '--unrelated', 1)
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
# The fixture setup timeout is not a key-generation latency assertion.
@pytest.mark.parametrize('passphrase,state', [('', 'unknown'), ('fixture-only', 'unusable')])
def test_real_key_metadata(tmp_path, passphrase, state):
    try:
        ssh = Path(build_ssh.resolve_tool('ssh'))
        keygen = build_ssh.resolve_tool('ssh-keygen', executable=str(ssh.parent / 'ssh-keygen'))
    except FileNotFoundError:
        pytest.skip('matching OpenSSH tools unavailable')
    identity = tmp_path / 'key'
    subprocess.run([keygen, '-q', '-t', 'ed25519', '-N', passphrase, '-f', str(identity)], check=True, stdin=subprocess.DEVNULL, capture_output=True, timeout=60)
    result = build_ssh.inspect_key(ssh, identity)
    assert result.state == state
    assert result.passphrase == bool(passphrase)
    assert result.key_kind == 'ED25519'


# Real subprocess output parsing: malformed provider records must stay unknown,
# while a valid absent provider refuses and a built-in provider remains unknown.
# These metadata stubs never request a signature and do not model a real token.
@pytest.mark.skipif(build_ssh.os.name == 'nt', reason='POSIX executable metadata stubs')
@pytest.mark.parametrize('settings,state', [
    ('securitykeyprovider \n', 'unknown'),
    ('securitykeyprovider\n', 'unknown'),
    ('securitykeyprovider\t \n', 'unknown'),
    ('unfamiliar setting\n', 'unknown'),
    ('securitykeyprovider internal\n', 'unknown'),
    ('securitykeyprovider none\n', 'unusable'),
])
def test_provider_output_with_real_metadata_processes(tmp_path, monkeypatch, settings, state):
    import sys
    tools = tmp_path / 'tools'
    tools.mkdir()
    ssh = tools / 'ssh'
    ssh.write_text(
        f'#!{sys.executable}\nimport sys\n'
        f'print({settings!r} if "-G" in sys.argv else "OpenSSH fixture", end="")\n'
    )
    keygen = tools / 'ssh-keygen'
    keygen.write_text(
        f'#!{sys.executable}\nimport sys\n'
        'print("256 SHA256:fixture fixture (ED25519-SK)" if "-l" in sys.argv else "sk-ssh-ed25519@openssh.com AAAA")\n'
    )
    ssh.chmod(0o700)
    keygen.chmod(0o700)
    identity = tmp_path / 'key'
    identity.touch()
    result = build_ssh.inspect_key(ssh, identity)
    assert result.state == state
    assert result.provider == ('none' if state == 'unusable' else 'internal' if 'internal' in settings else 'unknown')
    if state == 'unusable':
        assert result.reason == 'SSH has no configured security-key provider in this lane'
    else:
        # The same parser result reaches doctor and preflight without crashing
        # or refusing. No network/wrapper is executed in this focused control.
        original_resolver = build_ssh.resolve_tool
        def resolve(name, **kwargs):
            return str(ssh) if name == 'ssh' and 'executable' not in kwargs else original_resolver(name, **kwargs)
        monkeypatch.setattr(build_ssh, 'resolve_tool', resolve)
        text = build_ssh.doctor((), identity=str(identity))
        assert 'Local state: unknown' in text
        creds = git_admission.OperatorSSHCredentials(identity=identity)
        monkeypatch.setattr(git_admission, 'validate_operator_ssh_credentials', lambda c: c)
        tool = git_admission.GitTool(Path('/git'), Path('/exec'), (), ssh=ssh, ssh_credentials=creds)
        git_admission._preflight_ssh(parse_repository_source('git@example.test:group/repo.git'), tool)


# Only recognized OpenSSH evidence of ignored private-file permissions refuses.
# A matching loaded agent bypasses that bad file; ambiguous/unreadable output
# stays unknown, and valid file output prevents reject-all success.
@pytest.mark.parametrize('stderr,rc,loaded,state', [
    ('This private key will be ignored.\nLoad key "key": bad permissions', 255, False, 'unusable'),
    ('This private key will be ignored.\nLoad key "key": bad permissions', 255, True, 'unknown'),
    ('bad permissions', 255, False, 'unknown'),
    ('Permission denied', 255, False, 'unknown'),
    ('', 0, False, 'unknown'),
])
def test_private_key_permission_evidence(monkeypatch, stderr, rc, loaded, state):
    def probe(argv, **kwargs):
        if '-l' in argv:
            return subprocess.CompletedProcess(argv, 0, '256 SHA256:fixture key (ED25519)', '')
        if '-L' in argv:
            return subprocess.CompletedProcess(argv, 0, 'ssh-ed25519 AAAA', '')
        return subprocess.CompletedProcess(argv, rc, 'ssh-ed25519 AAAA' if rc == 0 else '', stderr)
    monkeypatch.setattr(build_ssh, '_local_probe', probe)
    result = build_ssh.inspect_key(Path('/tools/ssh'), Path('/keys/key'), Path('/agent') if loaded else None)
    assert result.state == state
    if state == 'unusable':
        assert result.reason == 'private key permissions are too open; SSH will ignore this file'


# Orphan public files remain valid agent pins, but never produce a file-only
# command/menu option; a real private sibling remains available beside them.
@pytest.mark.parametrize('agent', [None, '/agent'])
def test_orphan_public_key_is_not_a_file_candidate(tmp_path, monkeypatch, capsys, agent):
    private = tmp_path / 'present'
    private.touch()
    orphan = tmp_path / 'orphan.pub'
    candidates = build_ssh.DiscoveredCandidates(
        agent_socket=agent, public_keys=(str(orphan), str(private) + '.pub'),
    )
    commands = build_ssh.candidate_commands('example.test/group', candidates)
    assert any(f'--identity {private}' in command for command in commands)
    assert all('--agent' in command for command in commands if str(orphan) in command)
    assert not any(command.endswith(' --identity ' + str(orphan)[:-4]) and '--agent' not in command for command in commands)
    monkeypatch.setattr(build_ssh, 'discover_candidates', lambda: candidates)
    answers = iter(['n'])
    monkeypatch.setattr('builtins.input', lambda *a: next(answers))
    assert installer._prompt_build_ssh_rule('skill', 'command', 'example.test/group/repo') == (None, False)
    lines = capsys.readouterr().out.splitlines()
    assert any(str(private) in line for line in lines)
    assert all('pin ' + str(orphan) in line for line in lines if str(orphan) in line)
    assert any(str(orphan) in line for line in lines) == bool(agent)


# Real OpenSSH establishes the too-open-file refusal; owner-only permissions
# are the valid neighbor. Only generated keys are used, with no signing.
@pytest.mark.skipif(build_ssh.os.name == 'nt', reason='POSIX private-key permission evidence')
@pytest.mark.parametrize('mode,state', [(0o600, 'unknown'), (0o644, 'unusable')])
def test_real_private_key_permissions(tmp_path, mode, state):
    try:
        ssh = Path(build_ssh.resolve_tool('ssh'))
        keygen = build_ssh.resolve_tool('ssh-keygen', executable=str(ssh.parent / 'ssh-keygen'))
    except FileNotFoundError:
        pytest.skip('matching OpenSSH tools unavailable')
    identity = tmp_path / 'generated-key'
    subprocess.run([keygen, '-q', '-t', 'ed25519', '-N', '', '-f', str(identity)], check=True, stdin=subprocess.DEVNULL, capture_output=True, timeout=60)
    identity.chmod(mode)
    result = build_ssh.inspect_key(ssh, identity)
    assert result.state == state
    if state == 'unusable':
        assert result.reason == 'private key permissions are too open; SSH will ignore this file'
