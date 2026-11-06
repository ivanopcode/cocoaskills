"""Operational SSH display boundaries; probe doubles never sign or connect."""
import json
import os
from dataclasses import replace
from pathlib import Path
import shutil
import subprocess

import pytest

from conftest import make_config
from test_build_ssh import _empty_dev_manifest, _node
from csk import build_ssh, cli, config, git_admission, installer
from csk.build_repository import RepositorySource
from csk.terminal import shell_word, terminal_document, terminal_text


# Each outside field gets an ordinary value and each independent control plant.
# Newlines are data, not trusted diagnostic layout; joiners/ordinary Unicode stay.
VALUES = ['ordinary café', 'ESC-\x1b[31m-tail', 'OSC-\x1b]0;FORGED\x07-tail',
          'BEL-\x07-tail', 'CR-\rremediation: FORGED-tail',
          'LF-\nremediation: FORGED-tail', 'bidi-\u061c\u200e\u200f\u202eFORGED\u202c-tail']
FIELDS = ['scope', 'url', 'identity', 'agent', 'auto_agent', 'env_identity', 'env_agent',
          'env_known_hosts', 'config_identity', 'config_agent', 'config_known_hosts',
          'ssh_path', 'ssh_version', 'agent_listing', 'agent_error']


def assert_display(output):
    assert terminal_document(output) == output
    assert '\nremediation: FORGED' not in output
    assert '\r' not in output and '\x07' not in output


def doctor_operation(tmp_path, monkeypatch, capsys, field, value):
    """Real CLI/config/inspection/rendering; only bounded child probes are doubled."""
    scope = 'example.test/group'
    selected_scope = 'host-' + value + '.test/group' if field == 'scope' else 'https://example.test/' + value if field == 'url' else scope
    paths = {'identity': '/keys/private', 'agent': '/agents/socket', 'known_hosts': '/keys/known_hosts'}
    selected = field.removeprefix('config_').removeprefix('env_')
    if selected in paths:
        paths[selected] += '-' + value
    if field == 'auto_agent':
        paths['agent'] = 'auto'
    root = tmp_path / 'doctor'
    root.mkdir(exist_ok=True)
    cfg = replace(make_config(root, root / 'skills', root / 'project'), build_ssh=(build_ssh.BuildSSHRule(scope, **paths),))
    config.save_config(cfg)
    before = cfg.path.read_bytes()
    ssh = '/tools/ssh-' + value if field == 'ssh_path' else '/tools/ssh'
    version = 'OpenSSH fixture-' + value if field == 'ssh_version' else 'OpenSSH ordinary'
    comment = 'agent-' + value if field == 'agent_listing' else 'ordinary'
    fingerprint = '256 SHA256:' + 'A' * 43
    calls = []
    def probe(argv, **kwargs):
        calls.append((argv, kwargs))
        if '-V' in argv:
            return subprocess.CompletedProcess(argv, 0, '', version + '\n')
        if '-G' in argv:
            return subprocess.CompletedProcess(argv, 0, 'securitykeyprovider internal\n', '')
        if '-F' in argv:
            return subprocess.CompletedProcess(argv, 0, '# Host example.test found: line 1\n', '')
        if '-L' in argv:
            if field == 'agent_error':
                return subprocess.CompletedProcess(argv, 2, '', value)
            return subprocess.CompletedProcess(argv, 0, 'ssh-ed25519 AAAA ' + comment + '\n', '')
        if '-l' in argv:
            label = comment if argv[-1] == '-' else 'ordinary'
            return subprocess.CompletedProcess(argv, 0, fingerprint + ' ' + label + ' (ED25519)\n', '')
        assert '-y' in argv and '-P' in argv
        return subprocess.CompletedProcess(argv, 0, 'ssh-ed25519 AAAA\n', '')
    with monkeypatch.context() as patch:
        patch.setenv('CSK_CONFIG', str(cfg.path))
        for name in ('IDENTITY', 'AGENT', 'KNOWN_HOSTS'):
            patch.delenv('CSK_BUILD_SSH_' + name, raising=False)
        if field.startswith('env_'):
            patch.setenv('CSK_BUILD_SSH_' + selected.upper(), paths[selected])
        if field == 'auto_agent':
            patch.setenv('SSH_AUTH_SOCK', '/agents/socket-' + value)
        patch.setattr(build_ssh, 'resolve_tool', lambda *a, **kw: ssh)
        patch.setattr(build_ssh, '_local_probe', probe)
        args = ['config', 'build-ssh', 'doctor', selected_scope]
        explicit = {}
        if field in {'identity', 'agent'}:
            args += ['--' + field, paths[field]]
            explicit[field] = paths[field]
        assert cli.main(args) == 0
        captured = capsys.readouterr()
        assert captured.err == ''
        # Also exercise the renderer without main's fallback writer: a sink
        # preserving layout cannot repair raw data newlines after interpolation.
        direct = build_ssh.doctor(cfg.build_ssh, selected_scope, **explicit)
        assert captured.out == direct + '\n'
    output = captured.out
    assert 'Build SSH doctor (read-only' in output and 'Local state: unknown;' in output
    assert_display(output)
    if field not in {'agent_listing', 'agent_error'}:
        assert terminal_text(value) in output
    elif field == 'agent_listing' and value == VALUES[0]:
        assert 'Agent keys: 1' in output and 'in agent: yes' in output
    elif field == 'agent_error':
        assert 'Agent keys: unknown' in output and value not in output
    assert cfg.path.read_bytes() == before
    # JSON/config objects retain semantic path values, even when display escapes.
    payload = json.loads(before)['build_ssh'][scope]
    assert payload == paths
    assert build_ssh.serialize_rules(cfg.build_ssh)[scope] == paths
    assert calls and all(set(argv) & {'-V', '-G', '-F', '-L', '-l', '-y'} for argv, _ in calls)
    return output


# The real local refusal is formatted at direct and wrapped install boundaries.
# Raw typed error/source/reason values survive JSON; display cannot forge rows.
# Direct model construction stresses display beyond the repository URL grammar.
@pytest.mark.parametrize('value', VALUES)
def test_preflight_display_and_semantics(tmp_path, monkeypatch, capsys, value):
    identity = 'example.test/group-' + value
    reason = 'key reason https://example.test/' + value
    source = RepositorySource('ssh://git@example.test/' + value, identity, 'ssh')
    creds = git_admission.OperatorSSHCredentials(identity=tmp_path / ('key-' + value))
    tool = git_admission.GitTool(Path('/git'), Path('/exec'), (), ssh=Path('/ssh'), ssh_credentials=creds)
    monkeypatch.setattr(git_admission, 'validate_operator_ssh_credentials', lambda c: c)
    facts = build_ssh.KeyInspection('unusable', reason)
    monkeypatch.setattr(build_ssh, 'inspect_key', lambda *a: facts)
    with pytest.raises(git_admission.GitAdmissionError) as caught:
        git_admission._preflight_ssh(source, tool)
    error = caught.value
    assert error.code == git_admission.SSH_CREDENTIAL_UNUSABLE
    assert error.failure_class == 'auth-unavailable'
    assert identity in str(error) and reason in str(error)
    assert json.loads(json.dumps({'identity': source.identity, 'reason': facts.reason})) == {'identity': identity, 'reason': reason}
    for wrapped in (False, True):
        failure = error
        if wrapped:
            failure = installer.InstallError('external build: ' + str(error))
            failure.__cause__ = error
        output = installer.failure_text(failure, verbose=True)
        assert_display(output)
        assert terminal_text(identity) in output and terminal_text(reason) in output
        assert 'next: run csk config build-ssh doctor ' + shell_word(identity) + ' and select ' in output
        with monkeypatch.context() as patch:
            def dispatch(args):
                raise failure
            patch.setattr(cli, '_dispatch', dispatch)
            assert cli.main(['install']) == (cli.EXIT_CONFIG if wrapped else cli.EXIT_PARTIAL_FAIL)
            captured = capsys.readouterr()
        assert_display(captured.out + captured.err)
    # Unknown evidence cannot become a terminal-safety rejection or a new refusal.
    monkeypatch.setattr(build_ssh, 'inspect_key', lambda *a: replace(facts, state='unknown'))
    git_admission._preflight_ssh(source, tool)

    # Transport diagnostics already escape their fields and own their row layout.
    # A preflight cause must not flatten those separate remediation lines.
    wrapped = installer.source_transport.TransportResolutionError(())
    wrapped.__cause__ = error
    assert installer.failure_text(wrapped) == installer.source_diagnostics.format_transport_exception(wrapped)


# Suggested credential commands survive real shell parsing byte for byte,
# including control-bearing paths/scopes. The private sibling is a real fixture.
@pytest.mark.skipif(os.name != 'posix', reason='POSIX shell command roundtrip')
@pytest.mark.parametrize('value', VALUES)
def test_preflight_commands_copyable(tmp_path, value):
    shell = shutil.which('bash')
    assert shell is not None
    scope = 'example.test/' + value
    private = tmp_path / ('key-' + value)
    private.touch()
    candidates = build_ssh.DiscoveredCandidates(agent_socket='/agent', public_keys=(str(private)+'.pub',))
    commands = build_ssh.candidate_commands(scope, candidates)
    assert len(commands) == 3
    for command in commands:
        assert_display(command)
        result = subprocess.run([shell, '-c', 'csk() { printf "%s\\0" "$@"; }; ' + command], capture_output=True, timeout=10)
        assert result.returncode == 0, result.stderr
        args = result.stdout.decode().split('\0')[:-1]
        assert args[:4] == ['config', 'build-ssh', 'add', scope]
        if '--identity' in args:
            assert args[args.index('--identity')+1] == str(private)


# Missing credentials and saved-rule/dry-run messages use actual selection logic.
# Only credential validation and saving are intercepted: no operator files change.
@pytest.mark.parametrize('value', VALUES)
def test_preflight_selection_messages(tmp_path, monkeypatch, value):
    node = _node('skill-' + value, 'git@example.test:group/tool.git')
    cfg = make_config(tmp_path, tmp_path / 'skills', tmp_path / 'project')
    selected = [(node, 'tool')]
    monkeypatch.setattr(build_ssh, 'discover_candidates', lambda: build_ssh.DiscoveredCandidates())
    def resolve(cfg, **kwargs):
        return installer._resolve_build_ssh_credentials(
            cfg, selected, _empty_dev_manifest(), run_wide=None,
            messages=messages, dry_run=True, **kwargs,
        )
    messages = []
    with pytest.raises(installer.InstallError) as caught:
        resolve(cfg, interactive=False)
    output = str(caught.value)
    assert_display(output)
    assert git_admission.SSH_CREDENTIAL_MISSING in output
    assert repr(terminal_text(node.name)) in output
    assert '\nselect credentials with: csk config build-ssh add example.test/group ' in output

    rule = build_ssh.BuildSSHRule('example.test/group', identity='/keys/private-' + value)
    credentials = git_admission.OperatorSSHCredentials(identity=Path(rule.identity))
    monkeypatch.setattr(git_admission, 'capture_operator_ssh_credentials', lambda **kw: credentials)
    monkeypatch.setattr(installer, '_prompt_build_ssh_rule', lambda *a: (rule, True))
    saved = []
    monkeypatch.setattr(config, 'save_config', saved.append)
    cfg = replace(cfg, path=tmp_path / ('config-' + value + '.json'))
    assert resolve(cfg, interactive=True)[(node.name, 'tool')] == credentials
    assert len(saved) == 1 and saved[0].path == cfg.path and saved[0].build_ssh == (rule,)
    output = '\n'.join(messages)
    assert_display(output)
    assert "external build ssh: example.test/group/tool <- config scope 'example.test/group'" in output
    assert terminal_text(cfg.path) in output


# Validation failures are displayed without allowing a key path to forge rows;
# the successful selection in the preceding table is the positive control.
@pytest.mark.parametrize('value', VALUES)
def test_preflight_selection_validation_display(monkeypatch, value):
    rule = build_ssh.BuildSSHRule('example.test/group', identity='/keys/private-' + value)
    def fail(**kwargs):
        raise git_admission.GitAdmissionError(git_admission.SSH_CREDENTIAL_MISSING, rule.identity)
    monkeypatch.setattr(git_admission, 'capture_operator_ssh_credentials', fail)
    with pytest.raises(installer.InstallError) as caught:
        installer._rule_credentials(rule, 'example.test/group/tool')
    output = str(caught.value)
    assert_display(output)
    assert git_admission.SSH_CREDENTIAL_MISSING in output and terminal_text(rule.identity) in output
