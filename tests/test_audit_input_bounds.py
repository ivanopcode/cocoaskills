import io
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from csk.audit import pipeline, redaction
from csk.audit.backends.base import AuditBackendError, AuditRequest
from csk.audit.backends.command_backend import CommandBackend
from csk.audit.capabilities import CapabilityManifest


def plan_at(path):
    return SimpleNamespace(snapshot=path, decl=SimpleNamespace(name='sample', source='sample'),
                           resolved=SimpleNamespace(commit='abc'),
                           spec=SimpleNamespace(capabilities=CapabilityManifest.implicit_none()))


# Oversized sparse and aggregate inputs are refused before hashing or any read.
@pytest.mark.parametrize('parts', [(10001,), (6000, 6000)])
def test_admission_precedes_reads(tmp_path, parts):
    for index, size in enumerate(parts):
        with (tmp_path / str(index)).open('wb') as stream:
            stream.truncate(size)
    with patch.object(Path, 'open', side_effect=AssertionError('content read')), patch.object(
        pipeline.hashing, 'content_sha256', side_effect=AssertionError('hash read')
    ):
        with pytest.raises(AuditBackendError, match='audit.request.too-large'):
            pipeline.audit_plans([plan_at(tmp_path)], SimpleNamespace(
                audit=SimpleNamespace(max_request_bytes=10000)), scope='global')


def test_admitted_request_preserves_local_and_scrubs_cloud(tmp_path):
    # Positive control: admitted data reaches requests; cloud secrets and binary do not.
    (tmp_path / 'a').write_bytes(b'TOKEN=synthetic-secret')
    (tmp_path / 'b').write_bytes(b'\x00')
    local = pipeline._build_request(plan_at(tmp_path), (), False, 'hash', 100)
    assert local.files == {'a': b'TOKEN=synthetic-secret', 'b': b'\x00'}
    cloud = pipeline._build_request(plan_at(tmp_path), (), True, 'hash', 100)
    assert cloud.files == {'a': b'TOKEN=<redacted>', 'b': b'<redacted-binary>'}
    assert cloud.static_findings[0].id == 'audit.redaction.applied'


def test_growth_read_is_bounded(tmp_path):
    # A file growing after admission cannot cause an unbounded request read.
    path = tmp_path / 'a'
    path.write_bytes(b'x' * 1000)
    class BoundedStream(io.BytesIO):
        def read(self, size=-1):
            assert size == 11, 'read must use the remaining budget plus one'
            return super().read(size)

    with patch.object(pipeline, '_admit_snapshot', return_value=[path]), patch.object(
        Path, 'open', return_value=BoundedStream(b'x' * 1000)
    ):
        with pytest.raises(AuditBackendError, match='snapshot grew'):
            pipeline._build_request(plan_at(tmp_path), (), False, 'hash', 10)


# Secret-name substrings retain the old grammar, while ordinary assignments survive.
@pytest.mark.parametrize('name', ['TOKEN', 'MY_SECRET_KEY', 'PASSWORD', '_PRIVATE', 'COMPASS'])
def test_assignment_secret_and_valid_control(name):
    assert redaction.scrub_text(f'{name}=synthetic-secret COLOR=blue') == f'{name}=<redacted> COLOR=blue'


def test_assignment_adversarial_40k_tokens():
    # Scaling is measured separately; this pins the 40k-token functional path.
    assert redaction.scrub_text('KEY ' * 40000 + 'TOKEN=hidden') == 'KEY ' * 40000 + 'TOKEN=<redacted>'
    assert redaction.scrub_text('KEY' * 40000 + '!') == '<redacted-secret>!'


# Oversized stderr never reaches the scrubber, including a cut PEM; short errors
# retain useful diagnostics and redact their complete secrets.
@pytest.mark.parametrize('stderr, expected', [
    (b'-----BEGIN PRIVATE KEY-----\n' + b'x' * 3000 + b'\n-----END PRIVATE KEY-----', '<stderr omitted: exceeds 2000 bytes>'),
    (b'failed TOKEN=hidden', 'failed TOKEN=<redacted>'),
    (b'ordinary failure', 'ordinary failure'),
])
def test_stderr_bounded_before_scrub(tmp_path, stderr, expected):
    backend = CommandBackend(SimpleNamespace(name='test', cloud=False, model=None, cwd=tmp_path, env={}, command=['test']))
    request = AuditRequest(skill='test', source='test', commit='test', content_sha256='test', files={},
        capabilities=CapabilityManifest.implicit_none(), contract_reference='', response_schema={}, static_findings=(), redacted=False)
    with patch('csk.audit.backends.command_backend.process.run', return_value=SimpleNamespace(returncode=1, stderr=stderr)), patch.object(
        redaction, 'scrub_text', wraps=redaction.scrub_text
    ) as scrub:
        with pytest.raises(AuditBackendError, match='exited with code 1') as exc:
            backend.extract(request, timeout=1)
        assert str(exc.value).endswith(expected)
        assert 'hidden' not in str(exc.value)
        assert scrub.call_count == (len(stderr) <= 2000)


def test_nested_assignment_preserves_secret_guarantee():
    # A benign outer assignment must not hide an inner secret assignment.
    assert redaction.scrub_text("COLOR=TOKEN=hidden") == "COLOR=TOKEN=<redacted>"
    assert redaction.scrub_text("COLOR=blue") == "COLOR=blue"


def test_pem_pairs_and_unterminated_blocks():
    # Complete nested blocks redact through the first END, matching prior behavior;
    # incomplete headers remain text and do not prevent later assignment scrubbing.
    header = "-----BEGIN PRIVATE KEY-----\n"
    assert redaction.scrub_text(header * 40000 + "TOKEN=hidden") == header * 40000 + "TOKEN=<redacted>"
    assert redaction.scrub_text(header * 2 + "hidden\n-----END PRIVATE KEY-----") == "<redacted-private-key>"
