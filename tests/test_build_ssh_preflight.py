"""Local SSH metadata and read-only guidance; no real token coverage."""
from pathlib import Path
from types import SimpleNamespace
import subprocess

import pytest

from csk import build_ssh, cli, config, git_admission, installer
from csk.build_repository import LockedCommit, parse_repository_source
from csk.sources import diagnostics, transport


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
        (False, False, None, False, 'ED25519-SK', 'none', 'unusable', 'provider'),
        (False, False, None, False, 'ED25519-SK', 'internal', 'unknown', 'does not establish'),
        (False, True, 'matching', False, 'ED25519-SK', 'none', 'unusable', 'provider'),
        (False, True, 'matching', False, 'ED25519-SK', 'internal', 'unknown', 'does not establish'),
    ],
)
def test_key_evidence(monkeypatch, encrypted, agent, listing, public, kind, provider, state, reason):
    calls = []
    def probe(argv, **kwargs):
        calls.append(argv)
        if '-L' in argv:
            return None if listing is None else subprocess.CompletedProcess(argv, 1 if listing == 'empty' else 0, 'The agent has no identities.\n' if listing == 'empty' else 'ssh-ed25519 AAAA', '')
        if '-l' in argv:
            return subprocess.CompletedProcess(argv, 0, f'256 SHA256:AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA label ({kind})', '')
        if '-y' in argv:
            return None if encrypted is None else subprocess.CompletedProcess(argv, 255 if encrypted or public else 0, '', f'Load key "{argv[-1]}": invalid format\n' if public else f'Load key "{argv[-1]}": incorrect passphrase supplied to decrypt private key\n' if encrypted else '')
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
        # The local refusal also carries the class used by install's fixed advice;
        # the unknown control below still reaches the wrapper rather than refusing.
        assert error.value.failure_class == 'auth-unavailable'
        rendered = installer.failure_text(error.value)
        assert diagnostics.transport_next_steps('auth-unavailable') in rendered
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


# A socket pathname does not attest its implementation or signing behavior.
# Both listed hardware identities stay unknown; no real token is exercised.
@pytest.mark.parametrize('socket,state', [('/private/tmp/com.apple.launchd.test/Listeners', 'unknown'), ('/tmp/operator-agent', 'unknown')])
def test_macos_hardware_agent_mask(monkeypatch, socket, state):
    monkeypatch.setattr(build_ssh.sys, 'platform', 'darwin')
    def probe(argv, **kwargs):
        if '-l' in argv:
            return subprocess.CompletedProcess(argv, 0, '256 SHA256:AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA label (ED25519-SK)', '')
        if '-L' in argv:
            return subprocess.CompletedProcess(argv, 0, 'sk-ssh-ed25519@openssh.com AAAA', '')
        return None
    monkeypatch.setattr(build_ssh, '_local_probe', probe)
    result = build_ssh.inspect_key(Path('/tools/ssh'), Path('/keys/token'), Path(socket))
    assert result.state == state
    assert result.loaded is True
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


# Real subprocess output parsing: malformed or duplicate provider records stay unknown,
# while a valid absent provider refuses and a built-in provider remains unknown.
# These metadata stubs never request a signature and do not model a real token.
@pytest.mark.skipif(build_ssh.os.name == 'nt', reason='POSIX executable metadata stubs')
@pytest.mark.parametrize('settings,state,provider', [
    ('securitykeyprovider \n', 'unknown', 'unknown'),
    ('securitykeyprovider\n', 'unknown', 'unknown'),
    ('securitykeyprovider\t \n', 'unknown', 'unknown'),
    ('unfamiliar setting\n', 'unknown', 'unknown'),
    ('securitykeyprovider internal\nsecuritykeyprovider none\n', 'unknown', 'unknown'),
    ('securitykeyprovider none\nsecuritykeyprovider internal\n', 'unknown', 'unknown'),
    ('securitykeyprovider unfamiliar\nsecuritykeyprovider none\n', 'unknown', 'unknown'),
    ('securitykeyprovider none\nsecuritykeyprovider unfamiliar\n', 'unknown', 'unknown'),
    ('securitykeyprovider internal\u2028securitykeyprovider none\n', 'unknown', 'unknown'),
    ('securitykeyprovider none\u2028securitykeyprovider internal\n', 'unknown', 'unknown'),
    ('securitykeyprovider none\nsecuritykeyprovider none\n', 'unknown', 'unknown'),
    ('securitykeyprovider internal extra\nsecuritykeyprovider none\n', 'unknown', 'unknown'),
    ('securitykeyprovider none\x00\n', 'unknown', 'unknown'),
    ('securitykeyprovider none \n', 'unknown', 'unknown'),
    ('securitykeyprovider internal\n', 'unknown', 'internal'),
    ('securitykeyprovider none\n', 'unusable', 'none'),
    ('securitykeyprovider none\r\n', 'unknown', 'unknown'),
])
def test_provider_output_with_real_metadata_processes(tmp_path, monkeypatch, settings, state, provider):
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
        'print("256 SHA256:AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA fixture (ED25519-SK)" if "-l" in sys.argv else "sk-ssh-ed25519@openssh.com AAAA")\n'
    )
    ssh.chmod(0o700)
    keygen.chmod(0o700)
    identity = tmp_path / 'key'
    identity.touch()
    identity.chmod(0o600)
    result = build_ssh.inspect_key(ssh, identity)
    assert result.state == state
    assert result.provider == provider
    original_resolver = build_ssh.resolve_tool
    def resolve(name, **kwargs):
        return str(ssh) if name == 'ssh' and 'executable' not in kwargs else original_resolver(name, **kwargs)
    monkeypatch.setattr(build_ssh, 'resolve_tool', resolve)
    text = build_ssh.doctor((), identity=str(identity))
    assert f'Local state: {state}' in text
    creds = git_admission.OperatorSSHCredentials(identity=identity)
    monkeypatch.setattr(git_admission, 'validate_operator_ssh_credentials', lambda c: c)
    tool = git_admission.GitTool(Path('/git'), Path('/exec'), (), ssh=ssh, ssh_credentials=creds)
    source = parse_repository_source('git@example.test:group/repo.git')
    if state == 'unusable':
        assert result.reason == 'SSH has no configured security-key provider in this lane'
        with pytest.raises(git_admission.GitAdmissionError, match='build_repository_ssh_credential_unusable') as error:
            git_admission._preflight_ssh(source, tool)
        assert result.reason in str(error.value)
    else:
        # The same parser result reaches doctor and preflight without crashing
        # or refusing. No network/wrapper is executed in this focused control.
        if provider == 'unknown':
            assert result.reason == 'SSH security-key provider output is unfamiliar'
        git_admission._preflight_ssh(source, tool)


def _permission_error():
    return (
        "@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@\n"
        "@         WARNING: UNPROTECTED PRIVATE KEY FILE!          @\n"
        "@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@\n"
        "Permissions 0644 for '/keys/key' are too open.\n"
        "It is required that your private key files are NOT accessible by others.\n"
        "This private key will be ignored.\n"
        'Load key "/keys/key": bad permissions\n'
    )


# Permission diagnostic text alone cannot refuse: only independent stat facts
# can prove this condition. Real file-mode controls below prove refusal still works.
@pytest.mark.parametrize('stderr,rc,loaded,state', [
    (_permission_error(), 255, False, 'unknown'),
    (_permission_error(), 255, True, 'unknown'),
    ('bad permissions', 255, False, 'unknown'),
    ('Permission denied', 255, False, 'unknown'),
    ('', 0, False, 'unknown'),
])
def test_private_key_permission_evidence(monkeypatch, stderr, rc, loaded, state):
    def probe(argv, **kwargs):
        if '-l' in argv:
            return subprocess.CompletedProcess(argv, 0, '256 SHA256:AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA key (ED25519)', '')
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


# Generated keys cover both filename witnesses, absent/present public siblings,
# and real POSIX modes through inspection, doctor and preflight. No user key,
# server or token is involved; credential normalization is outside this test.
@pytest.mark.skipif(build_ssh.os.name != 'posix', reason='POSIX file permission rule')
@pytest.mark.parametrize('name', ['key', 'MODE', 'model-MODE-key', '0644'])
@pytest.mark.parametrize('public', [False, True])
@pytest.mark.parametrize('mode,state', [(0o600, 'unknown'), (0o640, 'unusable'), (0o644, 'unusable'), (0o400, 'unknown')])
def test_real_private_key_permissions(tmp_path, monkeypatch, name, public, mode, state):
    try:
        ssh = Path(build_ssh.resolve_tool('ssh'))
        keygen = build_ssh.resolve_tool('ssh-keygen', executable=str(ssh.parent / 'ssh-keygen'))
    except FileNotFoundError:
        pytest.skip('matching OpenSSH tools unavailable')
    identity = tmp_path / name
    subprocess.run([keygen, '-q', '-t', 'ed25519', '-N', '', '-f', str(identity)], check=True, stdin=subprocess.DEVNULL, capture_output=True, timeout=60)
    if not public:
        Path(str(identity) + '.pub').unlink()
    identity.chmod(mode)
    result = build_ssh.inspect_key(ssh, identity)
    assert result.state == state
    assert f'Local state: {state}' in build_ssh.doctor((), identity=str(identity))
    creds = git_admission.OperatorSSHCredentials(identity=identity)
    monkeypatch.setattr(git_admission, 'validate_operator_ssh_credentials', lambda c: c)
    tool = git_admission.GitTool(Path('/git'), Path('/exec'), (), ssh=ssh, ssh_credentials=creds)
    source = parse_repository_source('git@example.test:group/repo.git')
    if state == 'unusable':
        assert result.reason == 'private key permissions are too open; SSH will ignore this file'
        with pytest.raises(git_admission.GitAdmissionError, match='build_repository_ssh_credential_unusable') as error:
            git_admission._preflight_ssh(source, tool)
        assert result.reason in str(error.value)
    else:
        git_admission._preflight_ssh(source, tool)


# Seeded, bounded mutations check a refusal invariant, not token usability.
# Mutate raw provider lines without retaining the exact absent-provider record;
# the exact record and normal multi-setting output are nearby refusal controls.
def test_provider_mutations_never_refuse(monkeypatch):
    import random
    rng = random.Random(20261106)
    exact = 'securitykeyprovider none'
    alphabet = ' abXYZ09\t\r\n\x00\x1b\u2028\u0085'
    mutations = {exact + ' ', exact + '\t', exact + '\r'}
    for _ in range(2000):
        index = rng.randrange(len(exact) + 1)
        char = rng.choice(alphabet)
        value = rng.choice((exact[:index] + char + exact[index:], exact[:index] + exact[index + 1:], exact[:index] + char + exact[index + 1:]))
        if exact not in value.split('\n'):
            mutations.add(value)
    for index in range(128):
        mutations.add(f'ambiguous-{index}\n' + exact)
        mutations.add(exact + f'\ncontradictory-{index}')
    assert 400 <= len(mutations) <= 2003
    output = ''
    def probe(argv, **kwargs):
        return subprocess.CompletedProcess(argv, 0, output if '-G' in argv else '256 SHA256:' + 'A' * 43 + ' fixture (ED25519-SK)\n', '')
    monkeypatch.setattr(build_ssh, '_local_probe', probe)
    for output in sorted(mutations):
        verdict = build_ssh.inspect_key(Path('/tools/ssh'), Path('/keys/key'))
        assert verdict.state == 'unknown', repr(output)
        assert verdict.reason == 'SSH security-key provider output is unfamiliar'
    for output in (exact, exact + '\n', 'hostname example.invalid\n' + exact + '\n'):
        verdict = build_ssh.inspect_key(Path('/tools/ssh'), Path('/keys/key'))
        assert verdict.state == 'unusable'
        assert verdict.reason == 'SSH has no configured security-key provider in this lane'


# Ambiguous fingerprint/agent rows and wrapped error text cannot prove failure.
# Valid encrypted controls refuse; permission text without stat facts and malformed evidence stay unknown.
# This exercises metadata only; it does not assert agent signing capability.
@pytest.mark.parametrize('evidence', ['fingerprint', 'empty-agent', 'agent-fingerprints', 'passphrase', 'permissions'])
def test_other_evidence_mutations_never_refuse(monkeypatch, evidence):
    fingerprint = '256 SHA256:' + 'A' * 43 + ' fixture (ED25519)\n'
    error = 'Load key "/keys/key": incorrect passphrase supplied to decrypt private key\n'
    seed = _permission_error() if evidence == 'permissions' else error
    mutation = None
    def probe(argv, **kwargs):
        if '-L' in argv:
            return subprocess.CompletedProcess(argv, 1 if evidence == 'empty-agent' else 0, mutation if evidence == 'empty-agent' and mutation is not None else 'The agent has no identities.\n' if evidence == 'empty-agent' else 'ssh-ed25519 AAAA\n', '')
        if '-l' in argv:
            value = mutation if mutation is not None and (evidence == 'fingerprint' or evidence == 'agent-fingerprints' and argv[-1] == '-') else fingerprint
            return subprocess.CompletedProcess(argv, 0, value, '')
        return subprocess.CompletedProcess(argv, 255, '', mutation if mutation is not None and evidence in {'passphrase', 'permissions'} else seed)
    monkeypatch.setattr(build_ssh, '_local_probe', probe)
    agent = Path('/agent') if evidence in {'empty-agent', 'agent-fingerprints'} else None
    # A valid agent listing matches the key and remains unknown; other valid
    # controls prove encrypted-file refusal; permission text alone stays unknown.
    control = build_ssh.inspect_key(Path('/tools/ssh'), Path('/keys/key'), agent)
    assert control.state == ('unknown' if evidence in {'agent-fingerprints', 'permissions'} else 'unusable')
    for index in range(256):
        raw = fingerprint if evidence in {'fingerprint', 'agent-fingerprints'} else 'The agent has no identities.\n' if evidence == 'empty-agent' else seed
        mutation = f'ambiguous-{index}:\n' + raw
        verdict = build_ssh.inspect_key(Path('/tools/ssh'), Path('/keys/key'), agent)
        assert verdict.state == 'unknown', (evidence, index)
        mutation = raw + f'contradictory-{index}\n'
        verdict = build_ssh.inspect_key(Path('/tools/ssh'), Path('/keys/key'), agent)
        assert verdict.state == 'unknown', (evidence, index)


# A filename suffix is not proof of public-only content: a private file can be
# named .pub. Exact keygen failure still refuses the public-only neighbor.
@pytest.mark.parametrize('failed,state', [(False, 'unknown'), (True, 'unusable')])
def test_public_suffix_requires_unusable_file_evidence(monkeypatch, failed, state):
    def probe(argv, **kwargs):
        if '-l' in argv:
            return subprocess.CompletedProcess(argv, 0, '256 SHA256:' + 'A' * 43 + ' fixture (ED25519)\n', '')
        return subprocess.CompletedProcess(argv, 255 if failed else 0, '' if failed else 'ssh-ed25519 AAAA\n', 'Load key "/keys/key.pub": invalid format\n' if failed else '')
    monkeypatch.setattr(build_ssh, '_local_probe', probe)
    verdict = build_ssh.inspect_key(Path('/tools/ssh'), Path('/keys/key.pub'))
    assert verdict.state == state
    if failed:
        assert verdict.reason == 'a public key alone cannot sign and no matching agent key is available'


# Synthetic ownership/platform controls cover states that cannot be created by
# an unprivileged worker. Real owned-file modes are exercised above.
@pytest.mark.parametrize('platform,owner,mode,expected', [
    ('posix', 42, 0o600, False), ('posix', 42, 0o400, False),
    ('posix', 42, 0o640, True), ('posix', 42, 0o644, True),
    ('posix', 42, 0o601, True), ('posix', 43, 0o644, None),
    ('nt', 42, 0o644, None),
])
def test_permission_stat_contract(monkeypatch, platform, owner, mode, expected):
    identity = Path('/fixture')
    def stat(path):
        assert path == identity
        assert platform == 'posix'
        return SimpleNamespace(st_uid=owner, st_mode=0o100000 | mode)
    monkeypatch.setattr(build_ssh, 'os', SimpleNamespace(name=platform, stat=stat, getuid=lambda: 42))
    assert build_ssh._identity_permissions(identity) is expected


# Missing/inaccessible stat data cannot fabricate a refusal; the safe-mode
# positive control is in the contract table and real-file cases above.
@pytest.mark.parametrize('error', [FileNotFoundError(), PermissionError()])
def test_unavailable_permission_stat_is_unknown(monkeypatch, error):
    identity = Path('/fixture')
    def stat(path):
        raise error
    monkeypatch.setattr(build_ssh, 'os', SimpleNamespace(name='posix', stat=stat))
    assert build_ssh._identity_permissions(identity) is None


# Permission facts survive failed/noisy fingerprints. An agent that may still
# hold the key keeps the outcome unknown; a proven empty agent cannot sign it.
@pytest.mark.skipif(build_ssh.os.name != 'posix', reason='POSIX stat evidence')
@pytest.mark.parametrize('agent,state', [(None, 'unusable'), ('empty', 'unusable'), ('unknown', 'unknown')])
@pytest.mark.parametrize('fingerprint', [None, 'warning', 'failed'])
def test_permissions_independent_of_fingerprint(tmp_path, monkeypatch, agent, state, fingerprint):
    identity = tmp_path / 'model-MODE-key'
    identity.touch(mode=0o644)
    identity.chmod(0o644)
    def probe(argv, **kwargs):
        if '-L' in argv:
            return subprocess.CompletedProcess(argv, 1, 'The agent has no identities.\n', '') if agent == 'empty' else None
        if fingerprint is None:
            return None
        return subprocess.CompletedProcess(argv, 0 if fingerprint == 'warning' else 255, '256 SHA256:' + 'A' * 43 + ' fixture (ED25519)\n', _permission_error())
    monkeypatch.setattr(build_ssh, '_local_probe', probe)
    result = build_ssh.inspect_key(Path('/tools/ssh'), identity, Path('/agent') if agent else None)
    assert result.state == state
    if state == 'unusable':
        assert result.reason == 'private key permissions are too open; SSH will ignore this file'


# The old MODE-substitution bug must not let text about another file override
# a real safe-mode identity, even when that other filename is the octal mode.
@pytest.mark.skipif(build_ssh.os.name != 'posix', reason='POSIX stat evidence')
@pytest.mark.parametrize('name', ['MODE', 'model-MODE-key', '0644'])
@pytest.mark.parametrize('reported', ['0644', 'other', 'model-MODE-key'])
def test_permission_diagnostic_cannot_replace_path(tmp_path, monkeypatch, name, reported):
    identity = tmp_path / name
    identity.touch(mode=0o600)
    identity.chmod(0o600)
    diagnostic = _permission_error().replace('/keys/key', str(tmp_path / reported))
    def probe(argv, **kwargs):
        if '-l' in argv:
            return subprocess.CompletedProcess(argv, 0, '256 SHA256:' + 'A' * 43 + ' fixture (ED25519)\n', '')
        return subprocess.CompletedProcess(argv, 255, '', diagnostic)
    monkeypatch.setattr(build_ssh, '_local_probe', probe)
    assert build_ssh.inspect_key(Path('/tools/ssh'), identity).state == 'unknown'
