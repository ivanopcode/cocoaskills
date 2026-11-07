"""Real OpenSSH/container evidence; software FIDO is not token qualification."""
from __future__ import annotations

import json
import os
from pathlib import Path
import shutil
import socket
import subprocess
import shlex
import sys
import threading
import tempfile
import time

import pytest

from csk import build_ssh, cli, config, git_admission
from csk.build_repository import LockedCommit, parse_repository_source


@pytest.fixture(scope='module')
def real_ssh():
    try:
        ssh = Path(build_ssh.resolve_tool('ssh'))
        suffix = '.exe' if ssh.suffix.lower() == '.exe' else ''
        build_ssh.resolve_tool('ssh-keygen', executable=str(ssh.parent / ('ssh-keygen' + suffix)))
    except (OSError, ValueError):
        pytest.skip('matching real OpenSSH binaries unavailable')
    return ssh


def generate(ssh, path, *options):
    suffix = '.exe' if ssh.suffix.lower() == '.exe' else ''
    command = [str(ssh.parent / ('ssh-keygen' + suffix)), '-q', '-f', str(path), *options]
    environment = {'LC_ALL': 'C', **({'SystemRoot': os.environ['SystemRoot']} if 'SystemRoot' in os.environ else {})}
    # Fixture startup is independent of the product runner and never inherits
    # Windows system trust. The generated key is the only input being inspected.
    with tempfile.TemporaryDirectory(dir=path.parent) as program_data:
        if os.name == 'nt':
            environment['PROGRAMDATA'] = program_data
        result = subprocess.run(command, stdin=subprocess.DEVNULL, capture_output=True, timeout=60, env=environment)
    assert result.returncode == 0, result.stderr.decode(errors='replace')


def private_acl_on_public(private):
    if os.name == 'nt':
        # Only fixture-owned generated files: public siblings inherit broad ACLs
        # that make native keygen stop before it can report their actual format.
        powershell = Path(os.environ['SystemRoot']) / 'System32/WindowsPowerShell/v1.0/powershell.exe'
        environment = {'SystemRoot': os.environ['SystemRoot'],
                       'CSK_TEST_PRIVATE': str(private), 'CSK_TEST_PUBLIC': str(private) + '.pub'}
        result = subprocess.run([str(powershell), '-NoProfile', '-NonInteractive', '-Command',
                                 'Get-Acl -LiteralPath $env:CSK_TEST_PRIVATE | Set-Acl -LiteralPath $env:CSK_TEST_PUBLIC'],
                                env=environment, stdin=subprocess.DEVNULL, capture_output=True, timeout=60)
        assert result.returncode == 0, result.stderr.decode(errors='replace')


@pytest.fixture(scope='module')
def software_sk(real_ssh, tmp_path_factory):
    if os.name != 'posix':
        pytest.skip('software middleware build uses the POSIX shared-library ABI')
    compiler = shutil.which('cc')
    if compiler is None:
        pytest.skip('C compiler unavailable for software FIDO fixture')
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
    root = tmp_path_factory.mktemp('software-fido')
    public = Ed25519PrivateKey.generate().public_key().public_bytes_raw()
    (root / 'public.inc').write_text(','.join(str(byte) for byte in public))
    source = Path(__file__).parent / 'fixtures' / 'ssh-sk-metadata'
    provider = root / 'provider.so'
    trace = root / 'calls.log'
    command = [compiler, '-dynamiclib' if sys.platform == 'darwin' else '-shared', '-fPIC', '-I', str(source), '-I', str(root), '-DTRACE_PATH=' + json.dumps(str(trace)), str(source / 'provider.c'), '-o', str(provider)]
    subprocess.run(command, check=True, capture_output=True, timeout=60)
    keys = {}
    for label, password in [('plain', ''), ('encrypted', 'fixture-only')]:
        path = root / label
        generate(real_ssh, path, '-t', 'ed25519-sk', '-w', str(provider), '-N', password)
        keys[label] = path
    assert trace.read_text().splitlines() == ['enroll', 'enroll']
    return keys, trace


# Real -G must stay noninteractive with closed stdin. A poisoned inherited
# provider/system-data root is scrubbed, and an absent root still starts native
# Windows SSH; -T removes the real warning that metadata stubs missed.
@pytest.mark.parametrize('program_data', [None, '/forbidden-system-data'])
def test_real_ssh_provider_probe(real_ssh, monkeypatch, program_data):
    monkeypatch.setenv('SSH_SK_PROVIDER', '/forbidden-provider')
    if program_data is None:
        monkeypatch.delenv('PROGRAMDATA', raising=False)
    else:
        monkeypatch.setenv('PROGRAMDATA', program_data)
    result = build_ssh._local_probe((str(real_ssh), '-G', '-T', '-F', os.devnull, 'example.invalid'))
    assert result is not None and result.returncode == 0
    assert result.stderr == ''
    records = [line for line in result.stdout.splitlines() if line.startswith('securitykeyprovider ')]
    assert len(records) == 1
    expected = 'none' if records[0] in {'securitykeyprovider none', 'securitykeyprovider $SSH_SK_PROVIDER'} else 'internal' if records[0] == 'securitykeyprovider internal' else 'unknown'
    assert build_ssh._security_key_provider(real_ssh) == expected
    assert '/forbidden-provider' not in result.stdout


# Enroll real SK containers with a disposable software middleware, then ask real
# keygen about them. Inspection must not re-enter even the software provider.
def test_real_security_key_passphrase(real_ssh, software_sk):
    keys, trace = software_sk
    before = trace.read_bytes()
    encrypted = build_ssh.inspect_key(real_ssh, keys['encrypted'])
    assert encrypted.state == 'unusable'
    assert encrypted.key_kind == 'ED25519-SK'
    assert encrypted.passphrase is True
    assert encrypted.reason.startswith('key has a passphrase')
    text = build_ssh.doctor((), identity=str(keys['encrypted']))
    assert 'ED25519-SK; passphrase: set; in agent: no' in text
    assert 'Local state: unusable; key has a passphrase' in text
    plain = build_ssh.inspect_key(real_ssh, keys['plain'])
    assert plain.passphrase is False
    assert plain.state == ('unusable' if plain.provider == 'none' else 'unknown')
    assert trace.read_bytes() == before


# A real encrypted legacy PEM has no public sibling: -l cannot establish its
# fingerprint but the independent -y empty-passphrase error still proves refusal.
@pytest.mark.parametrize('password,state', [('', 'unknown'), ('fixture-only', 'unusable')])
def test_real_legacy_pem_without_public(real_ssh, tmp_path, password, state):
    identity = tmp_path / 'legacy'
    generate(real_ssh, identity, '-t', 'rsa', '-b', '2048', '-m', 'PEM', '-N', password)
    identity.with_suffix('.pub').unlink()
    facts = build_ssh.inspect_key(real_ssh, identity)
    assert facts.state == state
    assert facts.passphrase is bool(password)
    if password:
        assert facts.reason.startswith('key has a passphrase')


# Default-mode .pub pins must give actionable pin advice rather than calling a
# public file a private key with bad permissions; an actual private .pub works.
@pytest.mark.parametrize('password', ['', 'fixture-only'])
def test_real_public_pin_reason(real_ssh, password, tmp_path):
    key = tmp_path / 'key'
    generate(real_ssh, key, '-t', 'ed25519', '-N', password)
    private_acl_on_public(key)
    public = key.with_suffix('.pub')
    facts = build_ssh.inspect_key(real_ssh, public)
    assert facts.state == 'unusable'
    assert 'public-key pins need a matching agent' in facts.reason or 'public key alone cannot sign' in facts.reason
    assert 'private key permissions' not in facts.reason
    private = tmp_path / 'private.pub'
    generate(real_ssh, private, '-t', 'ed25519', '-N', '')
    private_acl_on_public(private)
    assert build_ssh.inspect_key(real_ssh, private).state == 'unknown'
    public_facts = build_ssh.inspect_key(real_ssh, Path(str(private) + '.pub'))
    assert public_facts.state == 'unusable'
    assert 'public-key pins need a matching agent' in public_facts.reason or 'public key alone cannot sign' in public_facts.reason


# Windows ACL failure is not public-file or passphrase proof. Deliberately grant
# read access to Everyone on a generated .pub, then remove that ambiguity using
# its generated private sibling's ACL. Never alter an operator file or agent.
@pytest.mark.skipif(os.name != 'nt', reason='native Windows ACL evidence; POSIX modes covered separately')
def test_real_public_pin_acl_ambiguity(real_ssh, tmp_path):
    key = tmp_path / 'key'
    generate(real_ssh, key, '-t', 'ed25519', '-N', '')
    public = key.with_suffix('.pub')
    icacls = Path(os.environ['SystemRoot']) / 'System32/icacls.exe'
    result = subprocess.run([str(icacls), str(public), '/grant', '*S-1-1-0:R'],
                            stdin=subprocess.DEVNULL, capture_output=True, timeout=60)
    assert result.returncode == 0, result.stderr.decode(errors='replace')
    probe = build_ssh._local_probe((str(real_ssh.with_name('ssh-keygen.exe')), '-y', '-P', '', '-f', str(public)))
    assert probe is not None and probe.returncode == 255 and 'bad permissions' in probe.stderr
    assert build_ssh.inspect_key(real_ssh, public).state == 'unknown'
    plain = build_ssh.inspect_key(real_ssh, key)
    assert plain.state == 'unknown' and plain.passphrase is False
    private_acl_on_public(key)
    facts = build_ssh.inspect_key(real_ssh, public)
    assert facts.state == 'unusable'
    assert facts.reason == 'a public key alone cannot sign and no matching agent key is available'


# Preserve the software SK public-pin control beside its Windows-capable
# software-key equivalent above; this does not qualify a physical token.
@pytest.mark.parametrize('kind', ['plain', 'encrypted'])
def test_real_security_key_public_pin(real_ssh, software_sk, kind):
    keys, _ = software_sk
    facts = build_ssh.inspect_key(real_ssh, keys[kind].with_suffix('.pub'))
    assert facts.state == 'unusable'
    assert 'public-key pins need a matching agent' in facts.reason or 'public key alone cannot sign' in facts.reason


# Native paths with spaces must retain their spelling through real keygen's
# escaped diagnostic. An encrypted no-agent key refuses before wrapper creation;
# the ordinary key reaches the wrapper control. No transport or agent is used.
@pytest.mark.parametrize('password', ['', 'fixture-only'])
def test_real_file_preflight_before_wrapper(real_ssh, tmp_path, monkeypatch, password):
    identity = tmp_path / 'key with spaces'
    generate(real_ssh, identity, '-t', 'ed25519', '-N', password)
    facts = build_ssh.inspect_key(real_ssh, identity)
    assert facts.passphrase is bool(password)
    assert facts.key_kind == 'ED25519'
    assert facts.state == ('unusable' if password else 'unknown')
    doctor = build_ssh.doctor((), identity=str(identity))
    assert 'Key: ED25519; passphrase: ' + ('set' if password else 'not set') in doctor
    source = parse_repository_source('git@example.test:group/repo.git')
    credentials = git_admission.OperatorSSHCredentials(identity=identity)
    tool = git_admission.GitTool(Path('/git'), Path('/exec'), (), ssh=real_ssh, ssh_credentials=credentials)
    monkeypatch.setattr(git_admission, 'validate_git_tool', lambda *a, **k: None)
    monkeypatch.setattr(git_admission, 'validate_operator_ssh_credentials', lambda c: c)
    def wrapper(*a, **k):
        raise RuntimeError('wrapper reached')
    monkeypatch.setattr(git_admission, '_materialize_ssh_wrapper', wrapper)
    if password:
        with pytest.raises(git_admission.GitAdmissionError) as caught:
            git_admission.acquire_network(source, LockedCommit('sha1', '0' * 40), tool)
        assert caught.value.code == git_admission.SSH_CREDENTIAL_UNUSABLE
        assert 'example.test/group/repo' in str(caught.value)
        assert 'key has a passphrase' in str(caught.value)
    else:
        with pytest.raises(RuntimeError, match='wrapper reached'):
            git_admission.acquire_network(source, LockedCommit('sha1', '0' * 40), tool)


# The real admission entry points must refuse an encrypted SK before a logging
# SSH transport shim sees a connection. A plain key reaches the identical shim.
@pytest.mark.skipif(os.name != 'posix', reason='POSIX executable transport shim')
@pytest.mark.parametrize('resolve', [False, True])
@pytest.mark.parametrize('selection', ['encrypted', 'plain'])
def test_security_key_refuses_before_transport(real_ssh, software_sk, tmp_path, resolve, selection):
    from test_git_admission_ssh import _tool
    keys, trace = software_sk
    before = trace.read_bytes()
    if selection == 'plain' and build_ssh._security_key_provider(real_ssh) != 'none':
        pytest.skip('real SSH has a provider; the plain SK selection is not proven unusable')
    tools = tmp_path / 'tools'
    tools.mkdir()
    ssh = tools / 'ssh'
    log = tools / 'network.log'
    ssh.write_text(f'#!{sys.executable}\nimport os,sys\nif "-G" in sys.argv or "-V" in sys.argv: os.execv({str(real_ssh)!r}, [{str(real_ssh)!r}, *sys.argv[1:]])\nwith open({str(log)!r}, "a") as stream: stream.write("connection\\n")\nsys.exit(97)\n')
    ssh.chmod(0o700)
    (tools / 'ssh-keygen').symlink_to(real_ssh.parent / 'ssh-keygen')
    source = parse_repository_source('git@example.test:group/repo.git')
    plain = tmp_path / 'plain'
    generate(real_ssh, plain, '-t', 'ed25519', '-N', '')
    known = tmp_path / 'known_hosts'
    known.write_text('example.test ' + plain.with_suffix('.pub').read_text())
    def admit(identity):
        tool = _tool(ssh, git_admission.OperatorSSHCredentials(identity=identity, known_hosts=known))
        if resolve:
            return git_admission.resolve_network_ref(source, 'branch', 'main', tool)
        return git_admission.acquire_network(source, LockedCommit('sha1', '0' * 40), tool)
    with pytest.raises(git_admission.GitAdmissionError) as caught:
        admit(keys[selection])
    assert caught.value.code == git_admission.SSH_CREDENTIAL_UNUSABLE
    assert 'example.test/group/repo' in str(caught.value)
    assert ('key has a passphrase' if selection == 'encrypted' else 'no configured security-key provider') in str(caught.value)
    assert 'next: run csk config build-ssh doctor' in str(caught.value)
    assert not log.exists()
    with pytest.raises(git_admission.GitAdmissionError) as control:
        admit(plain)
    assert control.value.code != git_admission.SSH_CREDENTIAL_UNUSABLE
    assert log.exists(), str(control.value)
    assert log.read_text().splitlines() == ['connection']
    assert trace.read_bytes() == before


# Doctor needs neither global config nor private-byte reads in Python. Missing
# and malformed config remain visible but cannot hide explicit local diagnostics.
@pytest.mark.parametrize('config_bytes', [None, b'{', b'valid'])
def test_doctor_without_working_config(tmp_path, monkeypatch, capsys, config_bytes):
    path = tmp_path / 'config.json'
    if config_bytes == b'valid':
        from conftest import make_config
        (tmp_path / 'skills').mkdir()
        (tmp_path / 'project').mkdir()
        config.save_config(make_config(tmp_path, tmp_path / 'skills', tmp_path / 'project'))
    elif config_bytes is not None:
        path.write_bytes(config_bytes)
    monkeypatch.setenv('CSK_CONFIG', str(path))
    before = path.read_bytes() if path.exists() else None
    before_files = {str(p): (p.stat().st_size, p.stat().st_mode, p.stat().st_mtime_ns) for p in tmp_path.rglob('*') if p.is_file()}
    assert cli.main(['config', 'build-ssh', 'doctor', '--all-os', '--identity', str(tmp_path / 'missing')]) == 0
    text = capsys.readouterr().out
    assert 'identity file is missing' in text
    assert 'Terminal:' in text and 'Host key:' in text and 'Agent keys:' in text
    assert 'macOS:' in text and 'Windows (' in text
    assert ('Config: unavailable or invalid' in text) == (config_bytes in (None, b'{'))
    assert (path.read_bytes() if path.exists() else None) == before
    assert {str(p): (p.stat().st_size, p.stat().st_mode, p.stat().st_mtime_ns) for p in tmp_path.rglob('*') if p.is_file()} == before_files


# Real exec with fd 0 closed gives CPython sys.stdin=None, unlike /dev/null.
# Both must print diagnostics without a traceback; only generated keys are used.
@pytest.mark.skipif(os.name != 'posix', reason='POSIX closed-fd exec witness')
@pytest.mark.parametrize('closed_stdin', [False, True])
def test_doctor_closed_stdin(real_ssh, tmp_path, closed_stdin):
    identity = tmp_path / 'key'
    generate(real_ssh, identity, '-t', 'ed25519', '-N', '')
    environment = {name: value for name, value in os.environ.items()
                   if not name.startswith('CSK_BUILD_SSH_') and name not in {'GIT_DIR', 'GIT_WORK_TREE', 'SSH_AUTH_SOCK'}}
    environment['CSK_CONFIG'] = str(tmp_path / 'absent.json')
    environment['PYTHONPATH'] = str(Path(__file__).resolve().parents[1] / 'src')
    command = [sys.executable, '-m', 'csk', 'config', 'build-ssh', 'doctor', '--identity', str(identity)]
    if closed_stdin:
        command = [sys.executable, '-c', 'import os,sys; os.close(0); os.execv(sys.argv[1], sys.argv[1:])', *command]
    result = subprocess.run(command, stdin=subprocess.DEVNULL, capture_output=True, text=True, env=environment, timeout=60)
    assert result.returncode == 0, result.stderr
    assert 'Traceback' not in result.stderr
    assert 'Terminal: not attached; SSH prompts remain disabled' in result.stdout
    assert 'Key: ED25519; passphrase: not set; in agent: no' in result.stdout
    assert 'Local state: unknown; local metadata does not establish that the key is unusable' in result.stdout


# A present terminal input must not dereference an absent stderr stream.
# This unit control models stream presence, not a real terminal or signing.
@pytest.mark.parametrize('stderr_present', [False, True])
def test_doctor_terminal_stream_presence(monkeypatch, stderr_present):
    from types import SimpleNamespace
    stream = SimpleNamespace(isatty=lambda: True)
    with monkeypatch.context() as patch:
        patch.setattr(build_ssh.sys, 'stdin', stream)
        patch.setattr(build_ssh.sys, 'stderr', stream if stderr_present else None)
        patch.setattr(build_ssh, 'resolve_tool', lambda name: str(Path('/unavailable/ssh')))
        patch.setattr(build_ssh, '_local_probe', lambda *args, **kwargs: None)
        text = build_ssh.doctor(())
    expected = 'attached' if stderr_present else 'not attached'
    assert f'Terminal: {expected}; SSH prompts remain disabled' in text


# Unresolvable home expansion remains unknown for either selected path; valid
# ordinary paths in the nearby CLI tests still reach real metadata inspection.
@pytest.mark.skipif(os.name != 'posix', reason='POSIX named-user home expansion')
@pytest.mark.parametrize('option', ['--identity', '--agent'])
def test_doctor_unresolvable_home(option, capsys):
    assert cli.main(['config', 'build-ssh', 'doctor', '--identity', '/missing', option, '~csk-no-such-user-109739/key']) == 0
    text = capsys.readouterr().out
    assert 'home expansion failed' in text
    assert 'Local state: unknown' in text


# Local host-key presence has a found/not-found control and never contacts that
# host. Missing, directory and garbage identities get distinct unknown reasons.
def test_doctor_local_facts(real_ssh, tmp_path):
    key = tmp_path / 'key'
    generate(real_ssh, key, '-t', 'ed25519', '-N', '')
    known = tmp_path / 'known_hosts'
    known.write_text('example.test ' + key.with_suffix('.pub').read_text())
    assert build_ssh._host_key_status(real_ssh, 'example.test/group', str(known)).startswith('found in ')
    assert build_ssh._host_key_status(real_ssh, 'other.test/group', str(known)).startswith('not found in ')
    directory = tmp_path / 'directory'
    directory.mkdir()
    garbage = tmp_path / 'garbage'
    garbage.write_text('not a key')
    garbage.chmod(0o600)
    for identity, reason in [(tmp_path / 'missing', 'is missing'), (directory, 'not a regular file'), (garbage, 'format is unrecognized')]:
        facts = build_ssh.inspect_key(real_ssh, identity)
        assert facts.state == 'unknown'
        assert reason in facts.reason
    assert build_ssh.inspect_key(real_ssh, key).state == 'unknown'


# Apple's real client must ignore the unencrypted SK pin with its scrubbed
# default provider, while a generated plain key is offered to the same server.
# The loopback server rejects unsigned offers; no signature/token is requested.
# Paramiko is a required dev dependency, so CI cannot skip a missing observer.
@pytest.mark.skipif(sys.platform != 'darwin', reason='Apple OpenSSH absence semantics')
def test_apple_ssh_ignores_sk_without_provider(software_sk, tmp_path):
    import paramiko
    ssh = Path('/usr/bin/ssh')
    if build_ssh._security_key_provider(ssh) != 'none':
        pytest.skip('system SSH has a built-in provider; absent-provider route not applicable')
    keys, trace = software_sk
    before = trace.read_bytes()
    offers = []
    failures = []
    class Server(paramiko.ServerInterface):
        def get_allowed_auths(self, username):
            return 'publickey'
        def check_auth_publickey(self, username, key):
            offers.append(key.get_name())
            return paramiko.AUTH_FAILED
    listener = socket.socket()
    listener.bind(('127.0.0.1', 0))
    listener.listen(2)
    listener.settimeout(15)
    port = listener.getsockname()[1]
    host = paramiko.RSAKey.generate(2048)
    known = tmp_path / 'known_hosts'
    known.write_text(f'[127.0.0.1]:{port} {host.get_name()} {host.get_base64()}\n')
    def serve():
        try:
            for _ in range(2):
                client, _ = listener.accept()
                connection = paramiko.Transport(client)
                try:
                    connection.add_server_key(host)
                    connection.start_server(server=Server())
                    connection.accept(10)
                finally:
                    connection.close()
        except Exception as exc:
            failures.append(exc)
        finally:
            listener.close()
    thread = threading.Thread(target=serve, daemon=True)
    thread.start()
    def connect(identity):
        return subprocess.run([str(ssh), '-vvv', '-T', '-F', os.devnull, '-o', 'BatchMode=yes', '-o', 'NumberOfPasswordPrompts=0', '-o', 'IdentitiesOnly=yes', '-o', 'IdentityAgent=none', '-o', 'StrictHostKeyChecking=yes', '-o', 'UserKnownHostsFile='+str(known), '-o', 'GlobalKnownHostsFile='+os.devnull, '-i', str(identity), '-p', str(port), 'fixture@127.0.0.1'], env={'LC_ALL':'C'}, stdin=subprocess.DEVNULL, capture_output=True, timeout=15)
    sk = connect(keys['plain'])
    assert sk.returncode == 255
    assert b'ignoring authenticator-hosted key' in sk.stderr
    assert not offers
    plain = tmp_path / 'plain'
    generate(ssh, plain, '-t', 'ed25519', '-N', '')
    result = connect(plain)
    thread.join(15)
    assert result.returncode == 255
    assert not thread.is_alive() and not failures
    assert offers == ['ssh-ed25519']
    assert trace.read_bytes() == before


# An isolated test-owned agent reports zero then one key without doctor loading
# or removing anything. The generated plain identity is the membership control.
@pytest.mark.skipif(os.name != 'posix', reason='private UNIX agent fixture')
def test_doctor_counts_private_agent(real_ssh, tmp_path, request):
    directory = tempfile.TemporaryDirectory(prefix='sk-agent-')
    agent = Path(directory.name) / 'agent.sock'
    process = subprocess.Popen([str(real_ssh.parent / 'ssh-agent'), '-D', '-a', str(agent)], stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, env={'LC_ALL':'C'})
    recorded_pid = process.pid
    def cleanup():
        assert process.pid == recorded_pid
        if process.poll() is None:
            process.terminate()
        process.communicate(timeout=10)
        directory.cleanup()
    request.addfinalizer(cleanup)
    deadline = time.monotonic() + 5
    while not agent.exists() and time.monotonic() < deadline:
        assert process.poll() is None
        time.sleep(0.01)
    assert agent.exists()
    key = tmp_path / 'key'
    generate(real_ssh, key, '-t', 'ed25519', '-N', '')
    plain = build_ssh.inspect_key(real_ssh, key)
    assert plain.state == 'unknown' and plain.passphrase is False
    empty = build_ssh.inspect_key(real_ssh, key, agent)
    assert empty.agent_key_count == 0 and empty.loaded is False
    public = key.with_suffix('.pub')
    public_facts = build_ssh.inspect_key(real_ssh, public, agent)
    assert public_facts.state == 'unusable'
    assert 'public-key pins need a matching agent' in public_facts.reason or 'public key alone cannot sign' in public_facts.reason
    add = str(real_ssh.parent / 'ssh-add')
    subprocess.run([add, '-t', '60', str(key)], env={'LC_ALL':'C', 'SSH_AUTH_SOCK':str(agent)}, stdin=subprocess.DEVNULL, capture_output=True, check=True, timeout=10)
    loaded = build_ssh.inspect_key(real_ssh, key, agent)
    assert loaded.agent_key_count == 1 and loaded.loaded is True
    assert build_ssh.inspect_key(real_ssh, public, agent).state == 'unknown'
    other = tmp_path / 'other'
    generate(real_ssh, other, '-t', 'ed25519', '-N', '')
    absent = build_ssh.inspect_key(real_ssh, other.with_suffix('.pub'), agent)
    assert absent.state == 'unusable'
    assert 'public-key pins need a matching agent' in absent.reason or 'public key alone cannot sign' in absent.reason
    before = build_ssh._local_probe((add, '-L'), agent=agent)
    text = build_ssh.doctor((), identity=str(key), agent=str(agent))
    after = build_ssh._local_probe((add, '-L'), agent=agent)
    assert 'Agent keys: 1' in text and 'in agent: yes' in text
    assert before is not None and after is not None and before.stdout == after.stdout
    # The same real encrypted file follows the Windows matrix rule: a loaded
    # matching agent permits it; absent/empty agents refuse before admission.
    subprocess.run([str(real_ssh.parent / 'ssh-keygen'), '-p', '-P', '', '-N', 'fixture-only', '-f', str(key)],
                   env={'LC_ALL': 'C'}, stdin=subprocess.DEVNULL, capture_output=True, check=True, timeout=10)
    source = parse_repository_source('git@example.test:group/repo.git')
    credentials = git_admission.OperatorSSHCredentials(identity=key, agent_socket=agent)
    tool = git_admission.GitTool(Path('/git'), Path('/exec'), (), ssh=real_ssh, ssh_credentials=credentials)
    loaded = build_ssh.inspect_key(real_ssh, key, agent)
    assert loaded.state == 'unknown' and loaded.passphrase is True and loaded.loaded is True
    git_admission._preflight_ssh(source, tool)
    from dataclasses import replace
    without_agent = replace(tool, ssh_credentials=replace(credentials, agent_socket=None))
    with pytest.raises(git_admission.GitAdmissionError) as absent:
        git_admission._preflight_ssh(source, without_agent)
    assert absent.value.code == git_admission.SSH_CREDENTIAL_UNUSABLE
    assert absent.value.failure_class == 'auth-unavailable'
    assert 'key has a passphrase and no matching agent key is available' in str(absent.value)
    subprocess.run([add, '-d', str(public)], env={'LC_ALL':'C', 'SSH_AUTH_SOCK':str(agent)},
                   stdin=subprocess.DEVNULL, capture_output=True, check=True, timeout=10)
    empty = build_ssh.inspect_key(real_ssh, key, agent)
    assert empty.state == 'unusable' and empty.loaded is False and empty.agent_key_count == 0
    with pytest.raises(git_admission.GitAdmissionError) as emptied:
        git_admission._preflight_ssh(source, tool)
    assert emptied.value.code == absent.value.code
    assert emptied.value.failure_class == absent.value.failure_class
    assert str(emptied.value) == str(absent.value)


# Existing private siblings are the first agent suggestion; an orphan public
# record still permits the valid agent-only pin without inventing a private file.
@pytest.mark.parametrize('private_exists', [False, True])
def test_agent_candidate_prefers_private_path(tmp_path, private_exists):
    key = tmp_path / 'key'
    if private_exists:
        key.touch()
    candidates = build_ssh.DiscoveredCandidates(agent_socket='/agent', public_keys=(str(key)+'.pub',))
    first = build_ssh.candidate_commands('example.test/group', candidates)[0]
    assert shlex.split(first)[-2:] == ['--identity', str(key) + ('' if private_exists else '.pub')]
