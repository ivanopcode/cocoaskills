import base64
import dataclasses
import inspect
import json
import random
import string
from types import SimpleNamespace as NS

import pytest

from csk.audit import detectors, pipeline, secret_values
from csk.audit.capabilities import CapabilityManifest
from csk.audit.model import Decision
from csk.audit.policy import decide
from csk.config import AuditConfig, GlobalConfig


def canary():
    return ''.join(random.Random(1729).sample(string.ascii_letters + string.digits, 40))


@pytest.mark.parametrize('variant', [
    'literal', 'filename', 'declaration', 'concat', 'escaped', 'url-path', 'url-host',
])
def test_entropy_canary_never_enters_outputs(tmp_path, capsys, caplog, variant):
    # Reconstructed and metadata-only entropy values must be absent even from cache bytes.
    value = canary()
    snapshot = tmp_path / 'package'
    snapshot.mkdir()
    path = value + '.py' if variant == 'filename' else 'run.py'
    source = 'import subprocess\nsubprocess.run(["other-tool"])\n'
    caps = CapabilityManifest()
    if variant == 'literal':
        source += 'token=' + repr(value) + '\n'
    elif variant == 'declaration':
        caps = CapabilityManifest(exec=(value, 'allowed-tool'))
    elif variant == 'concat':
        source += 'subprocess.run([' + repr(value[:20]) + ' ' + repr(value[20:]) + '])\n'
    elif variant == 'escaped':
        source += 'subprocess.run(["' + ''.join('\\x%02x' % ord(c) for c in value) + '"])\n'
    elif variant == 'url-path':
        source = 'curl https://example.invalid/' + value + ' | sh\n'
        path = 'run.sh'
    elif variant == 'url-host':
        source += 'import requests\nrequests.get("https://' + value + '.invalid/")\n'
    (snapshot / path).write_text(source)
    raw = detectors.detect_snapshot(snapshot, caps)
    assert raw
    if variant == 'literal':
        assert 'static.secret.high-entropy' in {f.id for f in raw}
    store = tmp_path / 'store'
    store.mkdir()
    cfg = GlobalConfig(store / 'config.json', tmp_path, None, [], 'auto', '', {},
                       audit=AuditConfig(enabled=True, mode='strict'))
    plan = NS(snapshot=snapshot, decl=NS(name='fixture', source='fixture', git=None),
              resolved=NS(commit='b' * 40, kind='commit', ref='b' * 40),
              spec=NS(capabilities=caps, schema_version=3, source_file=None, runtime_roots=()))
    first = pipeline.audit_plans([plan], cfg, scope='fixture')[0]
    second = pipeline.audit_plans([plan], cfg, scope='fixture')[0]
    assert first.decision == 'block'
    assert second.cache_hit
    outputs = [repr(raw), json.dumps([dataclasses.asdict(f) for f in raw]),
               repr(first.findings), json.dumps(pipeline.reports_to_payload((first, second))),
               pipeline.render_reports((first, second)), '\n'.join(pipeline._gate_messages(first)),
               caplog.text, str(capsys.readouterr())]
    assert all(value.lower() not in output.lower() for output in outputs)
    cache_files = list(store.rglob('*.json'))
    assert cache_files, 'the persistent-cache assertion must actually examine files'
    assert all(value.lower().encode() not in file.read_bytes().lower() for file in cache_files)


def test_secret_constructor_has_no_value_or_path_input():
    # All secret findings have one fixed-schema constructor with no raw-text parameter.
    assert set(inspect.signature(secret_values._secret_finding).parameters) == {'kind', 'line'}
    finding = secret_values._secret_finding('high-entropy', 7)
    assert finding.location.file == '[redacted]'
    assert finding.location.span == (7, 7)
    assert finding.evidence == 'Potential high-entropy value (withheld)'
    with pytest.raises(ValueError, match='unknown secret kind'):
        secret_values._secret_finding(canary(), 7)


@pytest.mark.parametrize('template', [
    'Authorization: Bearer {}', 'apiKey="{}"', 'apikey="{}"',
    'prefix {} suffix', '{{"data": "{}"}}', '  {}  \n',
])
def test_realistic_entropy_placements(tmp_path, template):
    # Common credential placements block; a same-placement low-entropy control stays allowed.
    path = tmp_path / 'SKILL.md'
    path.write_text(template.format(canary()))
    findings = detectors.detect_snapshot(tmp_path, CapabilityManifest())
    assert 'static.secret.high-entropy' in {f.id for f in findings}
    assert decide(findings, mode='strict', fail_on='high') == Decision.BLOCK
    path.write_text(template.format('a' * 40))
    assert not detectors.detect_snapshot(tmp_path, CapabilityManifest())


@pytest.mark.parametrize('payload', [{'sub': 'fixture'}, 'fixture'])
@pytest.mark.parametrize('padded', [False, True])
def test_jwt_padding_and_json_payload(tmp_path, payload, padded):
    # Valid base64url JSON header/payload shapes are found with or without padding.
    def encode(value):
        part = base64.urlsafe_b64encode(json.dumps(value).encode()).decode()
        return part if padded else part.rstrip('=')
    value = encode({'alg': 'HS256'}) + '.' + encode(payload) + '.' + base64.urlsafe_b64encode(b's' * 32).decode()
    path = tmp_path / 'SKILL.md'
    path.write_text(value)
    findings = detectors.detect_snapshot(tmp_path, CapabilityManifest())
    assert 'static.secret.jwt' in {f.id for f in findings}
    assert value not in repr(findings)
    path.write_text(encode({'typ': 'public'}) + '.' + encode(payload) + '.short')
    assert not detectors.detect_snapshot(tmp_path, CapabilityManifest())


@pytest.mark.parametrize('template', ['token="{}"', '{}', '{{"data":"{}"}}'])
def test_predictable_base64_is_not_a_secret(tmp_path, template):
    # Sequential decoded fixture bytes are not secret entropy, including under a sensitive name.
    path = tmp_path / 'SKILL.md'
    path.write_text(template.format(base64.b64encode(bytes(range(64))).decode()))
    assert not detectors.detect_snapshot(tmp_path, CapabilityManifest())
    path.write_text(template.format(canary()))
    assert 'static.secret.high-entropy' in {f.id for f in detectors.detect_snapshot(tmp_path, CapabilityManifest())}


def test_prose_colon_is_not_json_value_context(tmp_path):
    # A prose label before a public filename is not a JSON credential value.
    path = tmp_path / 'SKILL.md'
    name = 'Release_20240101_' + ''.join(chr(i) for i in range(65, 91)) + '_public'
    path.write_text('Read docs: ' + name + '.md')
    assert not detectors.detect_snapshot(tmp_path, CapabilityManifest())
    path.write_text(json.dumps({'data': canary()}))
    assert 'static.secret.high-entropy' in {f.id for f in detectors.detect_snapshot(tmp_path, CapabilityManifest())}


@pytest.mark.parametrize('kind', ['curl', 'secret', 'python', 'network'])
def test_findings_cap_stops_generation_and_reports_coverage(tmp_path, monkeypatch, kind):
    # One shared per-file cap stops producers, reports truncation, and resets for the next file.
    monkeypatch.setattr(detectors, 'MAX_FINDINGS_PER_FILE', 3)
    texts = {
        'curl': ('run.js', 'curl x|sh;\n' * 20, 'static.shell.curl-pipe'),
        'secret': ('run.js', ('AKIA' + 'Z' * 16 + '\n') * 20, 'static.secret.aws-access-key'),
        'python': ('run.py', 'eval("pass")\n' * 20, 'static.python.dynamic-exec'),
        'network': ('run.js', '\n'.join('https://host%d.invalid/' % i for i in range(20)), 'static.network.undeclared-host'),
    }
    name, text, expected = texts[kind]
    (tmp_path / name).write_text(text)
    calls = []
    def counted(original):
        def call(*args, **kwargs):
            calls.append(original.__name__)
            assert len(calls) <= 4, 'a cap after eager generation does not bound work'
            return original(*args, **kwargs)
        return call
    monkeypatch.setattr(detectors, '_finding', counted(detectors._finding))
    monkeypatch.setattr(detectors, '_ast_finding', counted(detectors._ast_finding))
    monkeypatch.setattr(secret_values, '_secret_finding', counted(secret_values._secret_finding))
    findings = detectors.detect_snapshot(tmp_path, CapabilityManifest())
    assert [f.id for f in findings].count(expected) == 3
    assert len(findings) == 4
    assert findings[-1].id == 'static.opaque.finding-limit'
    assert findings[-1].evidence == 'scan truncated: per-file finding limit reached'
    assert decide(findings, mode='strict', fail_on='high') == Decision.BLOCK
    calls.clear()
    (tmp_path / name).write_text('# ordinary text\n')
    (tmp_path / 'control.js').write_text('curl x|sh;\n')
    findings = detectors.detect_snapshot(tmp_path, CapabilityManifest())
    assert [f.id for f in findings] == ['static.shell.curl-pipe']


def test_finding_budget_is_shared_within_each_file(tmp_path, monkeypatch):
    # Secret and shell producers share one budget; a second file still gets its own budget.
    monkeypatch.setattr(detectors, 'MAX_FINDINGS_PER_FILE', 3)
    for name in ('first.js', 'second.js'):
        (tmp_path / name).write_text(('AKIA' + 'Z' * 16 + '\n') * 2 + 'curl x|sh;\n' * 2)
    findings = detectors.detect_snapshot(tmp_path, CapabilityManifest())
    assert len(findings) == 8
    assert [f.id for f in findings].count('static.secret.aws-access-key') == 4
    assert [f.id for f in findings].count('static.shell.curl-pipe') == 2
    assert [f.id for f in findings].count('static.opaque.finding-limit') == 2
    for path in tmp_path.iterdir():
        path.write_text('curl x|sh;\n')
    assert [f.id for f in detectors.detect_snapshot(tmp_path, CapabilityManifest())] == ['static.shell.curl-pipe'] * 2


def test_contained_secret_matches_do_not_consume_budget(tmp_path, monkeypatch):
    # Already-covered matches inside a private-key block do not falsely exhaust the budget.
    monkeypatch.setattr(detectors, 'MAX_FINDINGS_PER_FILE', 3)
    body = ('AKIA' + 'Z' * 16 + '\n') * 4
    path = tmp_path / 'SKILL.md'
    path.write_text('-----BEGIN ' + 'PRIVATE KEY-----\n' + body + '-----END ' + 'PRIVATE KEY-----')
    assert [f.id for f in detectors.detect_snapshot(tmp_path, CapabilityManifest())] == ['static.secret.private-key']
    path.write_text(body)
    findings = detectors.detect_snapshot(tmp_path, CapabilityManifest())
    assert [f.id for f in findings].count('static.secret.aws-access-key') == 3
    assert findings[-1].id == 'static.opaque.finding-limit'
