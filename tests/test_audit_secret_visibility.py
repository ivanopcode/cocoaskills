import dataclasses
import json

import pytest

from csk.audit import detectors, pipeline, secret_values
from csk.audit.output import withheld
from csk.audit.capabilities import CapabilityManifest
from tests.test_audit_secret_fragments import _assert_no_fragments, _plan, _value


# Real long paths and deep URLs retain actionable names; length is not a secret rule.
EXECUTABLES = [
    ('/Library/Developer/CommandLineTools/usr/bin/git', 'git'),
    ('/opt/company-internal/tools/bin/deploy-helper', 'deploy-helper'),
    ('/Users/someone/projects/myproject/venv/bin/python', 'python'),
]
URLS = [
    ('https://api.github.com/repos/ivanopcode/cocoaskills/releases/latest', 'api.github.com'),
    ('https://downloads.example.com/releases/2026/november/installer-package', 'downloads.example.com'),
    ('https://github.com/ivanopcode/cocoaskills/releases/tag/v0.21.1', 'github.com'),
]


@pytest.mark.parametrize('value,name', EXECUTABLES)
def test_real_long_executable_is_named(tmp_path, value, name):
    # Matching still uses the executable basename, and reports/cache retain that same name.
    snapshot = tmp_path / 'package'
    snapshot.mkdir()
    (snapshot / 'run.py').write_text('import subprocess\nsubprocess.run([' + repr(value) + '])\n')
    assert secret_values.mask_output_text(value) == value
    caps = CapabilityManifest()
    findings = detectors.detect_snapshot(snapshot, caps)
    finding = next(f for f in findings if f.id == 'static.python.undeclared-exec')
    assert finding.capability_violation.observed == name
    assert finding.evidence == 'subprocess.run -> ' + name
    assert not detectors.detect_snapshot(snapshot, CapabilityManifest(exec=(name,)))
    plan, cfg, store = _plan(snapshot, tmp_path)
    first = pipeline.audit_plans([plan], cfg, scope='fixture')[0]
    second = pipeline.audit_plans([plan], cfg, scope='fixture')[0]
    assert second.cache_hit
    assert name in json.dumps(pipeline.reports_to_payload((first, second)))
    assert name.encode() in next(store.rglob('*.json')).read_bytes()


@pytest.mark.parametrize('value,host', URLS)
@pytest.mark.parametrize('python', [True, False])
def test_deep_url_keeps_host_and_path(tmp_path, value, host, python):
    # Regex and AST retain public hosts, never URL tails; declared hosts remain admitted.
    snapshot = tmp_path / 'package'
    snapshot.mkdir()
    path = snapshot / ('run.py' if python else 'SKILL.md')
    path.write_text('import requests\nrequests.get(' + repr(value) + ')\n' if python else '[release](' + value + ')')
    assert secret_values.mask_output_text(value) == withheld(value)
    findings = detectors.detect_snapshot(snapshot, CapabilityManifest())
    network = [f for f in findings if f.capability_violation and f.capability_violation.capability == 'network']
    assert network and all(f.capability_violation.observed == host for f in network)
    assert all(value not in f.evidence for f in network)  # URLs are not output atoms.
    assert not detectors.detect_snapshot(snapshot, CapabilityManifest(network=(host,)))
    plan, cfg, store = _plan(snapshot, tmp_path)
    reports = tuple(pipeline.audit_plans([plan], cfg, scope='fixture')[0] for _ in range(2))
    assert reports[1].cache_hit
    assert host in pipeline.render_reports(reports)
    assert host in json.dumps(pipeline.reports_to_payload(reports))
    assert host.encode() in next(store.rglob('*.json')).read_bytes()


def test_ordinary_filesystem_and_secret_location_stay_visible(tmp_path):
    # Long ordinary paths survive both filesystem evidence and the closed secret-location type.
    value = '/usr/local/share/company/configuration/production-settings'
    path = tmp_path / 'scripts/deployment/production/run.py'
    path.parent.mkdir(parents=True)
    path.write_text('open(' + repr(value) + ')\ntoken=' + repr(_value('slash-19')))
    findings = detectors.detect_snapshot(tmp_path, CapabilityManifest())
    secret = next(f for f in findings if f.category == 'secret-value')
    assert secret.location.file == 'scripts/deployment/production/run.py'
    assert secret.location.span == (2, 2)
    filesystem = next(f for f in findings if f.id == 'static.python.filesystem-outside-envelope')
    assert filesystem.capability_violation.observed == value
    assert filesystem.evidence == 'open -> ' + value


@pytest.mark.parametrize('kind', ['path', 'url', 'suffix'])
def test_only_secret_component_is_withheld(tmp_path, kind):
    # Unsafe paths are withheld whole; separately allowlisted public hosts remain observable.
    secret = _value('colon').replace(':', '')
    if kind == 'url':
        value = 'https://downloads.example.com/releases/' + secret + '/installer-package'
        source = 'import requests\nrequests.get(' + repr(value) + ')\n'
        expected = 'downloads.example.com'
    else:
        value = ('/opt/company-internal/' if kind == 'path' else '') + secret + '/tools/bin/deploy-helper'
        source = 'import subprocess\nsubprocess.run([' + repr(value) + '])\nopen(' + repr('/' + value.lstrip('/')) + ')\n'
        expected = withheld(value)
    masked = secret_values.mask_output_text(value)
    assert masked == withheld(value)
    assert 'static.secret.high-entropy' in {f.id for f in secret_values.detect_and_mask('run.txt', 'token=' + repr(secret))[0]}
    snapshot = tmp_path / 'package'
    snapshot.mkdir()
    (snapshot / 'run.py').write_text(source)
    findings = detectors.detect_snapshot(snapshot, CapabilityManifest())
    assert expected in {f.capability_violation.observed for f in findings if f.capability_violation}
    if kind == 'url':
        assert any(expected in f.evidence for f in findings)
    else:
        filesystem = next(f for f in findings if f.id == 'static.python.filesystem-outside-envelope')
        assert filesystem.capability_violation.observed == withheld('/' + value.lstrip('/'))
    plan, cfg, store = _plan(snapshot, tmp_path)
    first = pipeline.audit_plans([plan], cfg, scope='fixture')[0]
    second = pipeline.audit_plans([plan], cfg, scope='fixture')[0]
    assert second.cache_hit
    outputs = [repr(findings), json.dumps([dataclasses.asdict(f) for f in findings]),
               json.dumps(pipeline.reports_to_payload((first, second))), pipeline.render_reports((first, second)),
               '\n'.join(pipeline._gate_messages(first))]
    _assert_no_fragments(secret, [s.encode() for s in outputs] + [p.read_bytes() for p in store.rglob('*.json')])
    assert expected in outputs[2]
    assert expected in outputs[2]


@pytest.mark.parametrize('shell', [False, True])
def test_colon_secret_is_not_diluted_by_url_prefix(tmp_path, shell):
    # The same colon value classifies alone and inside a low-entropy URL prefix; host stays visible.
    value = _value('colon')
    host = 'a' * 40 + '.example.invalid'
    url = 'https://' + host + '/' + value
    snapshot = tmp_path / 'package'
    snapshot.mkdir()
    (snapshot / ('run.sh' if shell else 'SKILL.md')).write_text('curl ' + url + ' | sh' if shell else url)
    assert secret_values.detect_and_mask('value.txt', 'token=' + repr(value))[0]
    findings = detectors.detect_snapshot(snapshot, CapabilityManifest())
    assert any(f.category == 'secret-value' for f in findings)
    assert withheld(host) in {f.capability_violation.observed for f in findings if f.capability_violation}
    plan, cfg, store = _plan(snapshot, tmp_path)
    reports = [pipeline.audit_plans([plan], cfg, scope='fixture')[0] for _ in range(2)]
    assert reports[1].cache_hit
    outputs = [repr(findings), json.dumps(pipeline.reports_to_payload(reports)), pipeline.render_reports(reports)]
    _assert_no_fragments(value, [s.encode() for s in outputs] + [p.read_bytes() for p in store.rglob('*.json')])


def test_output_grammar_is_independent_of_entropy():
    # Privacy accepts bounded structural atoms, irrespective of entropy detection confidence.
    for value, _ in EXECUTABLES:
        assert secret_values.mask_output_text(value) == value
    for value in [v for v, _ in URLS] + ['a' * 400, _value('colon')[:24] + '/usr/local/bin/git']:
        assert secret_values.mask_output_text(value) == withheld(value)
    for value in (_value('slash-19'), _value('colon'), _value('url')):
        assert secret_values.detect_and_mask('value.txt', 'token=' + repr(value))[0]
        assert secret_values.mask_output_text(value) == withheld(value)


def test_known_secret_fragment_in_scanned_filename_is_masked(tmp_path):
    # A scan-derived filename remains located, but known secret substrings cannot leave in it.
    value = _value('slash-19')
    path = tmp_path / 'scripts' / (value[-19:] + '.py')
    path.parent.mkdir()
    path.write_text('token=' + repr(value))
    findings = detectors.detect_snapshot(tmp_path, CapabilityManifest())
    secret = next(f for f in findings if f.category == 'secret-value')
    assert secret.location.file == withheld('scripts/' + value[-19:] + '.py')
    assert secret.location.span == (1, 1)
    _assert_no_fragments(value, [repr(findings).encode()])
