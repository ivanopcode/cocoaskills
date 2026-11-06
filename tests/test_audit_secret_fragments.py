import dataclasses
import json
import random
import string
from types import SimpleNamespace as NS
from pathlib import Path

import pytest

from csk.audit import detectors, pipeline, secret_values, serialization, trust
from csk.audit.capabilities import CapabilityManifest
from csk.audit.model import CapabilityViolation, Finding, Location
from csk.config import AuditConfig, GlobalConfig


def _value(shape):
    raw = ''.join(random.Random(2027).sample(string.ascii_letters + string.digits, 48))
    if shape.startswith('slash-'):
        suffix = int(shape.split('-')[1])
        return raw[:-suffix] + '/' + raw[-suffix:]
    if shape == 'colon':
        return raw[:24] + ':' + raw[24:]
    return 'https://' + raw[:20] + '/' + raw[20:]


def _assert_no_fragments(value, outputs):
    # Absence of every length-eight window implies absence of every longer substring.
    windows = {value[i:i + 8].lower().encode() for i in range(len(value) - 7)}
    for output in outputs:
        assert not any(window in output.lower() for window in windows), 'secret fragment escaped'


@dataclasses.dataclass
class _Plan:
    # Match the production plan's replaceable dataclass contract used by bounded private copies.
    snapshot: Path
    decl: NS
    resolved: NS
    spec: NS


def _plan(snapshot, tmp_path, caps=CapabilityManifest()):
    store = tmp_path / 'store'
    store.mkdir()
    cfg = GlobalConfig(store / 'config.json', tmp_path, None, [], 'auto', '', {},
                       audit=AuditConfig(enabled=True, mode='strict'))
    plan = _Plan(snapshot=snapshot, decl=NS(name='fixture', source='fixture', git=None),
              resolved=NS(commit='b' * 40, kind='commit', ref='b' * 40),
              spec=NS(capabilities=caps, schema_version=3, source_file=None, runtime_roots=()))
    return plan, cfg, store


@pytest.mark.parametrize('shape', ['slash-31', 'slash-19', 'slash-11', 'slash-8', 'colon', 'url'])
@pytest.mark.parametrize('call', ['list', 'string', 'system', 'network'])
@pytest.mark.parametrize('literal', [False, True])
def test_reconstructed_secret_fragments_never_reach_outputs(tmp_path, capsys, caplog, shape, call, literal):
    # Covers basename/host extraction both with and without a literal secret in the file.
    # Synthetic values are built at runtime; the package's calls are never executed.
    value = _value(shape)
    snapshot = tmp_path / 'package'
    snapshot.mkdir()
    split = len(value) // 2
    joined = repr(value[:split]) + ' ' + repr(value[split:])
    expressions = {
        'list': 'subprocess.run([' + joined + '])',
        'string': 'subprocess.run(' + joined + ' " --flag")',
        'system': 'os.system(' + joined + ' " --flag")',
        'network': 'requests.get(' + repr('https://') + ' ' + joined + ')',
    }
    source = 'import subprocess, os, requests\n' + expressions[call] + '\n'
    if literal:
        source += 'token=' + repr(value) + '\n'
    (snapshot / 'run.py').write_text(source)
    raw = detectors.detect_snapshot(snapshot, CapabilityManifest())
    expected = 'static.python.undeclared-network' if call == 'network' else 'static.python.undeclared-exec'
    assert expected in {f.id for f in raw}
    if literal and shape.startswith('slash-'):
        secret = next(f for f in raw if f.id == 'static.secret.high-entropy')
        assert secret.location == type(secret.location)('run.py', (3, 3))
    plan, cfg, store = _plan(snapshot, tmp_path)
    first = pipeline.audit_plans([plan], cfg, scope='fixture')[0]
    second = pipeline.audit_plans([plan], cfg, scope='fixture')[0]
    assert second.cache_hit
    assert first.findings
    outputs = [repr(raw), json.dumps([dataclasses.asdict(f) for f in raw]),
               json.dumps([serialization.finding_to_payload(f) for f in raw]),
               json.dumps(pipeline.reports_to_payload((first, second))),
               pipeline.render_reports((first, second)), '\n'.join(pipeline._gate_messages(first)),
               caplog.text, str(capsys.readouterr())]
    _assert_no_fragments(value, [s.encode() for s in outputs])
    caches = list(store.rglob('*.json'))
    assert caches, 'must examine actual cache bytes'
    _assert_no_fragments(value, [path.read_bytes() for path in caches])


def test_fragment_checker_detects_partial_canary():
    # The checker catches a planted eight-byte suffix even when the full value is absent.
    value = _value('slash-11')
    _assert_no_fragments(value, [b'ordinary output'])
    with pytest.raises(AssertionError, match='secret fragment escaped'):
        _assert_no_fragments(value, [value[-8:].encode()])


@pytest.mark.parametrize('field', ['id', 'category', 'detector', 'confidence', 'surface', 'severity', 'verifiable', 'evidence', 'location', 'capability_violation'])
def test_secret_type_rejects_value_derived_fields(field):
    # Both direct construction and replace reject free-form secret fields before persistence.
    original = secret_values._secret_finding('high-entropy', 'scripts/run.py', 4)
    value = _value('slash-19')
    replacement = value
    if field == 'location':
        replacement = Location(value, (4, 4))
    elif field == 'capability_violation':
        replacement = CapabilityViolation('exec', value, value)
    with pytest.raises(ValueError, match='^invalid secret finding schema$'):
        dataclasses.replace(original, **{field: replacement})
    kwargs = {f.name: getattr(original, f.name) for f in dataclasses.fields(original)}
    kwargs[field] = replacement
    with pytest.raises(ValueError, match='^invalid secret finding schema$'):
        Finding(**kwargs)
    assert dataclasses.replace(original) == original


@pytest.mark.parametrize('reader', [serialization.finding_from_payload, trust.finding_from_payload])
@pytest.mark.parametrize('field', ['evidence', 'location', 'capability_violation'])
def test_secret_payload_rejects_free_text(reader, field):
    # Backend and persisted-payload readers cannot restore forged secret text.
    original = secret_values._secret_finding('high-entropy', 'scripts/run.py', 4)
    payload = serialization.finding_to_payload(original)
    value = _value('slash-19')
    payload[field] = value if field == 'evidence' else (
        {'file': value, 'span': [4, 4]} if field == 'location' else
        {'capability': 'exec', 'declared': value, 'observed': value})
    with pytest.raises(ValueError, match='^invalid secret (finding schema|scan location)$'):
        reader(payload)
    # Cache records retain their scan-derived path; external backends cannot assert provenance.
    valid = serialization.finding_to_payload(original)
    assert trust.finding_from_payload(valid) == original
    with pytest.raises(ValueError, match='^invalid secret finding schema$'):
        serialization.finding_from_payload(valid)


def test_normal_executable_and_host_controls(tmp_path):
    # Output masking must not break raw capability matching or ordinary actionable observations.
    path = tmp_path / 'run.py'
    path.write_text('import subprocess, requests\nsubprocess.run(["/usr/bin/custom-tool"])\nrequests.get("https://example.invalid/path")\n')
    findings = detectors.detect_snapshot(tmp_path, CapabilityManifest())
    assert {f.capability_violation.observed for f in findings if f.capability_violation} == {'custom-tool', 'example.invalid'}
    assert not detectors.detect_snapshot(tmp_path, CapabilityManifest(exec=('custom-tool',), network=('example.invalid',)))


def test_backend_cannot_persist_forged_secret(tmp_path, monkeypatch, caplog, capsys):
    # A backend echo is rejected at construction, before any report/cache write; null still works.
    from csk.audit.backends.null_backend import NullBackend

    snapshot = tmp_path / 'package'
    snapshot.mkdir()
    (snapshot / 'run.py').write_text('print("ordinary")\n')
    plan, cfg, store = _plan(snapshot, tmp_path)
    value = _value('slash-19')
    def forged(*args, **kwargs):
        original = secret_values._secret_finding('high-entropy', 'run.py', 1)
        return (dataclasses.replace(original, evidence=value),)
    with monkeypatch.context() as patch:
        patch.setattr(NullBackend, 'extract', forged)
        with pytest.raises(ValueError, match='^invalid secret finding schema$'):
            pipeline.audit_plans([plan], cfg, scope='fixture')
    assert not list(store.rglob('*.json'))
    _assert_no_fragments(value, [(caplog.text + str(capsys.readouterr())).encode()])
    report = pipeline.audit_plans([plan], cfg, scope='fixture')[0]
    assert report.decision == 'allow'
    assert list(store.rglob('*.json'))


def test_sensitive_string_keeps_generic_security_findings(tmp_path):
    # Withholding reconstructed-string evidence must not suppress curl-pipe detection.
    value = _value('url')
    source = 'command = "curl " ' + repr(value[:24]) + ' ' + repr(value[24:]) + ' " | sh"\n'
    (tmp_path / 'run.py').write_text(source)
    findings = detectors.detect_snapshot(tmp_path, CapabilityManifest())
    curl = next(f for f in findings if f.id == 'static.shell.curl-pipe')
    assert 'curl' in curl.evidence and '| sh' in curl.evidence
    _assert_no_fragments(value, [repr(findings).encode()])
    (tmp_path / 'run.py').write_text('command = "curl example.invalid | sh"\n')
    curl = next(f for f in detectors.detect_snapshot(tmp_path, CapabilityManifest()) if f.id == 'static.shell.curl-pipe')
    assert curl.evidence == 'curl | sh'  # Source prose is no longer an admitted evidence shape.


@pytest.mark.parametrize('prefix', ['AKIA', 'ASIA'])
def test_access_key_prefixes_and_near_matches(tmp_path, prefix):
    # Both long-lived and temporary AWS key shapes block; wrong lengths/boundaries do not.
    path = tmp_path / 'SKILL.md'
    value = prefix + 'Z' * 16
    path.write_text('# fixture\n' + value)
    findings = detectors.detect_snapshot(tmp_path, CapabilityManifest())
    secret = next(f for f in findings if f.id == 'static.secret.aws-access-key')
    assert secret.location.file == 'SKILL.md'
    assert secret.location.span == (2, 2)
    _assert_no_fragments(value, [repr(findings).encode()])
    for near in (prefix + 'Z' * 15, prefix + 'Z' * 17, 'x' + value, value + 'x'):
        path.write_text(near)
        assert not detectors.detect_snapshot(tmp_path, CapabilityManifest())
