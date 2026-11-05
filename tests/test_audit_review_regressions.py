import base64
import dataclasses
import hashlib
import json
from types import SimpleNamespace as NS

import pytest

from csk.audit import detectors, pipeline
from csk.audit.capabilities import CapabilityManifest
from csk.config import GlobalConfig, AuditConfig


def entropy():
    return ''.join(chr(i) for i in range(65, 91)) + 'abc123'


@pytest.mark.parametrize('placement', ['bare', 'line', 'assignment', 'escaped-json', 'jwt'])
def test_secret_placements(tmp_path, placement):
    # Different encoding/placement must retain blocking findings, not validate credentials.
    value = entropy()
    text = {'bare': value, 'line': '\n' + value + '\n', 'assignment': 'token="' + value + '"',
            'escaped-json': '{"value":"\\u0041KIA' + 'Z' * 16 + '"}'}
    encode = lambda value: base64.urlsafe_b64encode(json.dumps(value).encode()).decode().rstrip('=')
    text['jwt'] = encode({'alg': 'HS256'}) + '.' + encode({'sub': 'fixture'}) + '.' + 's' * 43
    (tmp_path / 'run.js').write_text(text[placement])
    assert any(f.category == 'secret-value' for f in detectors.detect_snapshot(tmp_path, CapabilityManifest()))


@pytest.mark.parametrize('text', [
    'target_event_id=AEV-' + ''.join(chr(i) for i in range(65, 91)) + '123456',
    'fixture="' + base64.b64encode(bytes(range(64))).decode() + '"',
    json.dumps({'integrity': 'sha512-' + base64.b64encode(hashlib.sha512(b'public package').digest()).decode()}),
    '-----BEGIN PUBLIC KEY-----\n' + base64.b64encode(bytes(range(64))).decode() + '\n-----END PUBLIC KEY-----',
])
def test_public_data_is_not_secret(tmp_path, text):
    # Public IDs, fixtures and integrity material cannot independently block a package.
    (tmp_path / 'SKILL.md').write_text(text)
    assert not detectors.detect_snapshot(tmp_path, CapabilityManifest())


@pytest.mark.parametrize('variant', ['filename', 'decoded', 'declaration'])
def test_canary_absent_from_every_pipeline_output(tmp_path, capsys, caplog, variant):
    # Actual pipeline, policy, renderers and persistent cache must never expose the canary.
    value = 'AKIA' + 'CANARYSYNTH12345'
    snapshot = tmp_path / 'package'
    snapshot.mkdir()
    path = value + '.py' if variant == 'filename' else 'run.py'
    text = 'value="' + value + '"\nimport subprocess\nsubprocess.run(["\\x41' + value[1:] + '"])\n'
    (snapshot / path).write_text(text)
    caps = CapabilityManifest(exec=(value,)) if variant == 'declaration' else CapabilityManifest()
    if variant == 'declaration':
        (snapshot / path).write_text(text + 'subprocess.run(["other"])\n')
    store = tmp_path / 'store'
    store.mkdir()
    cfg = GlobalConfig(store / 'config.json', tmp_path, None, [], 'auto', '', {}, audit=AuditConfig(enabled=True, mode='strict'))
    plan = NS(snapshot=snapshot, decl=NS(name='fixture', source='fixture', git=None), resolved=NS(commit='b'*40, kind='commit', ref='b'*40), spec=NS(capabilities=caps, schema_version=3, source_file=None, runtime_roots=()))
    first = pipeline.audit_plans([plan], cfg, scope='fixture')[0]
    second = pipeline.audit_plans([plan], cfg, scope='fixture')[0]
    assert first.decision == 'block'
    assert 'static.secret.aws-access-key' in {f.id for f in first.findings}
    assert second.cache_hit
    outputs = [repr(first.findings), json.dumps([dataclasses.asdict(f) for f in first.findings]),
               json.dumps(pipeline.reports_to_payload((first, second))), pipeline.render_reports((first, second)),
               '\n'.join(pipeline._gate_messages(first)), caplog.text, str(capsys.readouterr())]
    outputs.extend(p.read_text() for p in store.rglob('*.json'))
    assert all(value not in output for output in outputs)


def test_directory_read_failure_is_visible(tmp_path, monkeypatch):
    # Injected traversal refusal produces a coverage finding; accessible control detects the plant.
    directory = tmp_path / 'nested'
    directory.mkdir()
    (directory / 'run.js').write_text('ghp_' + 'a' * 36)
    original = detectors.os.scandir
    def scan(path):
        if path == directory:
            raise PermissionError('private error text')
        return original(path)
    with monkeypatch.context() as patch:
        patch.setattr(detectors.os, 'scandir', scan)
        findings = detectors.detect_snapshot(tmp_path, CapabilityManifest())
    assert [f.evidence for f in findings] == ['scan skipped: directory read error']
    assert any(f.category == 'secret-value' for f in detectors.detect_snapshot(tmp_path, CapabilityManifest()))


def test_curl_segments(tmp_path):
    # Repeated commands without a pipe remain benign; the adjacent real pipe is detected.
    path = tmp_path / 'run.js'
    path.write_text('curl ' * 1000)
    assert not detectors.detect_snapshot(tmp_path, CapabilityManifest())
    path.write_text('curl ' * 1000 + '| sh')
    assert 'static.shell.curl-pipe' in {f.id for f in detectors.detect_snapshot(tmp_path, CapabilityManifest())}


def test_actual_unreadable_directory(tmp_path):
    # Unprivileged POSIX traversal failure cannot erase coverage; restoring access finds the plant.
    import os
    if os.name != 'posix' or os.geteuid() == 0:
        pytest.skip('requires unprivileged POSIX permissions')
    directory = tmp_path / 'nested'
    directory.mkdir()
    (directory / 'run.js').write_text('ghp_' + 'a' * 36)
    try:
        directory.chmod(0)
        findings = detectors.detect_snapshot(tmp_path, CapabilityManifest())
        assert [f.evidence for f in findings] == ['scan skipped: directory read error']
    finally:
        directory.chmod(0o700)
    assert any(f.category == 'secret-value' for f in detectors.detect_snapshot(tmp_path, CapabilityManifest()))
