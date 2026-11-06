import os
import random
import re
import sys
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from csk.config import AuditConfig
from csk.audit import pipeline, redaction
from csk.hashing import HashingError
from csk.audit.backends import process
from csk.audit.backends.base import AuditBackendError

OLD_PEM = re.compile(r'-----BEGIN [A-Z ]*PRIVATE KEY-----.*?-----END [A-Z ]*PRIVATE KEY-----', re.S)
OLD_ENV = re.compile(r'''\b([A-Z_]*(?:TOKEN|SECRET|PASSWORD|PASS|KEY|PRIVATE)[A-Z_]*)=([^\s'"`]+)''')


def old_scrub(text):
    text = redaction.URL_RE.sub(redaction._scrub_url_match, text)
    text = OLD_PEM.sub('<redacted-private-key>', text)
    text = redaction.AUTH_HEADER_RE.sub(r'\1<redacted>', text)
    text = OLD_ENV.sub(r'\1=<redacted>', text)
    return redaction.HIGH_ENTROPY_RE.sub('<redacted-secret>', text)


# Ignored END and nested BEGIN may share 5..9 dashes with the next marker.
@pytest.mark.parametrize('dash_count', range(1, 13))
@pytest.mark.parametrize('nested', [False, True])
def test_overlapping_pem(dash_count, nested):
    prefix = '-----BEGIN PRIVATE KEY-----SYNTHSECRET' if nested else ''
    text = prefix + '-----' + ('BEGIN' if nested else 'END') + ' PRIVATE KEY' + '-' * dash_count
    text += 'END PRIVATE KEY-----' if nested else 'BEGIN PRIVATE KEY-----SYNTHSECRET-----END PRIVATE KEY-----'
    assert redaction.scrub_text(text) == old_scrub(text)
    assert redaction.scrub_stderr(text.encode()) == old_scrub(text)
    assert redaction.scrub_text('ordinary text') == 'ordinary text'


def test_generated_redaction_non_disclosure():
    # Bounded legacy oracle: generated overlaps, labels, assignments, quoting and
    # word boundaries. Any secret newly surviving fails, even if output differs.
    rng = random.Random(2026)
    atoms = ['-----BEGIN ', '-----END ', 'PRIVATE KEY', 'RSA PRIVATE KEY',
             ' ', '\n', 'SYNTHSECRET', 'TOKEN=', 'COLOR=', '9KEY=', 'éKEY=', '"', "'", '`']
    atoms += ['-' * n for n in range(1, 13)]
    cases = [''.join(rng.choices(atoms, k=rng.randrange(2, 30))) for _ in range(10000)]
    cases += [a + '-' * n + b for n in range(1, 13)
              for a in ['-----END PRIVATE KEY', '-----BEGIN PRIVATE KEY-----SYNTHSECRET-----BEGIN PRIVATE KEY']
              for b in ['BEGIN PRIVATE KEY-----SYNTHSECRET-----END PRIVATE KEY-----', 'END PRIVATE KEY-----']]
    for text in cases:
        old, new = old_scrub(text), redaction.scrub_text(text)
        assert not ('SYNTHSECRET' not in old and 'SYNTHSECRET' in new), repr(text)
        assert new == old, repr(text)


@dataclass
class Plan:
    snapshot: Path
    schema_version: int = 3

    @property
    def decl(self):
        return SimpleNamespace(name='sample', source='sample', git=None)

    @property
    def spec(self):
        return SimpleNamespace(schema_version=self.schema_version)


# A growth race is rejected before hashing/detectors; the positive control shows
# downstream code reads a private stable copy even if the original then grows.
def test_growth_before_and_after_copy(tmp_path):
    source = tmp_path / 'a'
    source.write_bytes(b'ok')
    original = pipeline._admit_snapshot
    def grow(plan, limit):
        paths = original(plan, limit)
        source.write_bytes(b'x' * 100)
        return paths
    cfg = SimpleNamespace(audit=AuditConfig(max_request_bytes=10))
    with patch.object(pipeline, '_admit_snapshot', side_effect=grow), patch.object(pipeline, '_audit_plans') as downstream:
        with pytest.raises(pipeline.AuditInputTooLarge, match='snapshot grew'):
            pipeline.audit_plans([Plan(tmp_path)], cfg, scope='test')
        downstream.assert_not_called()
    source.write_bytes(b'ok')
    def consume(plans, *args, **kwargs):
        source.write_bytes(b'x' * 100)
        assert plans[0].snapshot != tmp_path
        assert (plans[0].snapshot / 'a').read_bytes() == b'ok'
        return ()
    with patch.object(pipeline, '_audit_plans', side_effect=consume):
        assert pipeline.audit_plans([Plan(tmp_path)], cfg, scope='test') == ()


# Raw admission failure blocks strict mode at every threshold without
# inventing a content hash/report for unread data.
@pytest.mark.parametrize('mode,threshold,blocked', [('strict','high',True), ('strict','critical',True), ('strict','off',True), ('advisory','high',False)])
def test_oversize_gate_policy(tmp_path, mode, threshold, blocked):
    (tmp_path / 'a').write_bytes(b'x'*20)
    cfg = SimpleNamespace(audit=AuditConfig(enabled=True,max_request_bytes=10,mode=mode,fail_on=threshold,revocations=()))
    result = pipeline.gate_plans([Plan(tmp_path)], cfg, scope='test')
    assert result.blocked == blocked
    assert 'audit.request.too-large' in (result.errors + result.warnings)[0]


def test_name_budget_and_special_files(tmp_path):
    # Name bytes count; FIFOs are not opened; symlinks never reach consumers.
    name = tmp_path / ('x' * 100)
    name.write_bytes(b'a')
    with pytest.raises(pipeline.AuditInputTooLarge):
        pipeline._admit_snapshot(Plan(tmp_path), 100)
    assert pipeline._admit_snapshot(Plan(tmp_path), 101) == [name]
    if hasattr(os, 'mkfifo'):
        fifo = tmp_path / 'fifo'
        os.mkfifo(fifo)
        assert fifo not in pipeline._admit_snapshot(Plan(tmp_path), 200)
    link = tmp_path / 'link'
    link.symlink_to(name)
    with pytest.raises(HashingError, match='symbolic links'):
        pipeline.audit_plans([Plan(tmp_path)], SimpleNamespace(audit=AuditConfig(max_request_bytes=500)), scope='test')


# Real children demonstrate bounded stderr retention, stdout overflow refusal,
# a valid response, and timeout. These exercise capture rather than mock it.
def test_bounded_process(tmp_path, monkeypatch):
    def run(code, timeout=5):
        return process.run([sys.executable, '-c', code], input=b'', cwd=tmp_path, env=os.environ.copy(), timeout=timeout)
    # Binary output preserves the exact-byte control on Windows and POSIX.
    assert run("import sys; sys.stdout.buffer.write(b'ok\\n')").stdout == b'ok\n'
    result = run("import sys; sys.stderr.write('x'*100000)")
    assert len(result.stderr) == 2001
    assert redaction.scrub_stderr(result.stderr).startswith('<stderr omitted:')
    monkeypatch.setattr(process, 'MAX_RESPONSE_BYTES', 100)
    with pytest.raises(AuditBackendError, match='exceeds output byte limit'):
        run("print('x'*10000)")
    import subprocess
    with pytest.raises(subprocess.TimeoutExpired):
        run('import time; time.sleep(10)', timeout=0.1)


def test_generated_assignment_variants():
    # Explicit name/value/boundary combinations complement fragment fuzzing;
    # ordinary assignments remain visible while every legacy-hidden marker stays hidden.
    for name in ('TOKEN', 'MY_PRIVATE_KEY', 'COMPASS', 'COLOR', '9KEY', 'éKEY', '_SECRET'):
        for prefix in ('', 'COLOR=', 'x', ' ', '\n', 'TOKEN='):
            for value in ('SYNTHSECRET', 'TOKEN=SYNTHSECRET', '"SYNTHSECRET"', "'SYNTHSECRET'", '`SYNTHSECRET`'):
                text = prefix + name + '=' + value
                assert redaction.scrub_text(text) == old_scrub(text)


@pytest.mark.skipif(not hasattr(os, 'mkfifo'), reason='FIFO replacement requires POSIX')
def test_fifo_replacement_after_admission(tmp_path):
    # A regular path replaced with a FIFO is rejected without blocking on open.
    source = tmp_path / 'a'
    source.write_bytes(b'ok')
    original = pipeline._admit_snapshot
    def replace_with_fifo(plan, limit):
        paths = original(plan, limit)
        source.unlink()
        os.mkfifo(source)
        return paths
    with patch.object(pipeline, '_admit_snapshot', side_effect=replace_with_fifo):
        with pytest.raises(AuditBackendError, match='audit.input.changed: expected regular file'):
            pipeline.audit_plans([Plan(tmp_path)], SimpleNamespace(audit=AuditConfig(max_request_bytes=100)), scope='test')


@pytest.mark.parametrize('kind', ['directory', 'missing', 'fifo'])
def test_non_regular_symlinks_rejected(tmp_path, kind):
    # Copying must not silently drop symlinks that the original hash rejected.
    target = tmp_path / 'target'
    if kind == 'directory':
        target.mkdir()
    elif kind == 'fifo':
        if not hasattr(os, 'mkfifo'):
            pytest.skip('FIFO requires POSIX')
        os.mkfifo(target)
    (tmp_path / 'link').symlink_to(target)
    with pytest.raises(HashingError, match='symbolic links'):
        pipeline._admit_snapshot(Plan(tmp_path), 100)


@pytest.mark.parametrize('threshold', ['off', 'critical', 'high'])
def test_oversize_legacy_still_requires_verified_pin(tmp_path, threshold):
    # Strict legacy admission must not become allowed merely because HIGH is
    # below fail_on; an unread snapshot cannot establish its content-bound pin.
    (tmp_path / 'a').write_bytes(b'x' * 20)
    cfg = SimpleNamespace(audit=AuditConfig(enabled=True, max_request_bytes=10, mode='strict', fail_on=threshold, revocations=()))
    result = pipeline.gate_plans([Plan(tmp_path, schema_version=2)], cfg, scope='test')
    assert result.blocked
    assert 'audit.request.too-large' in result.errors[0]
