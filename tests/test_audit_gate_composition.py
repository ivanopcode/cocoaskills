"""Policy composition uses real snapshots, detectors and the null backend."""
from dataclasses import dataclass, replace
from pathlib import Path
from types import SimpleNamespace

import pytest

from csk.audit import pipeline
from csk.audit.capabilities import CapabilityManifest
from csk.config import AuditConfig, GlobalConfig


@dataclass
class Plan:
    snapshot: Path
    decl: object
    resolved: object
    spec: object


@pytest.fixture
def batch(tmp_path):
    plans = {}
    # The destructive command is detector input only; it is never executed.
    for name, content in [('critical', 'rm -rf /\n'), ('oversized', 'x' * 11000), ('benign', 'echo safe\n')]:
        root = tmp_path / name
        root.mkdir()
        (root / 'payload.sh').write_text(content)
        plans[name] = Plan(
            root, SimpleNamespace(name=name, source='synthetic-' + name, git=None),
            SimpleNamespace(commit='a' * 40, kind='tag', ref='v1'),
            SimpleNamespace(schema_version=3, source_file=None, runtime_roots=(),
                            capabilities=CapabilityManifest.implicit_none()),
        )
    cfg = GlobalConfig(
        path=tmp_path / 'home' / 'config.json', skills_root=tmp_path,
        preferred_locale=None, default_agents=[], adapter_mode='auto',
        worktree_alias_pattern='.*', projects={},
        audit=AuditConfig(enabled=True, mode='strict', fail_on='critical', max_request_bytes=10000),
    )
    return plans, cfg


# Oversize cannot erase an earlier CRITICAL denial or skip a later one. Single
# and mixed benign controls also require real reports, so rejecting all fails.
@pytest.mark.parametrize('names', [
    ('critical',), ('oversized',), ('benign',), ('critical', 'benign'),
    ('critical', 'oversized'), ('oversized', 'critical'),
    ('benign', 'oversized'), ('oversized', 'benign'),
    ('oversized', 'critical', 'benign'),
])
def test_oversize_preserves_batch_decisions(batch, names):
    plans, cfg = batch
    result = pipeline.gate_plans([plans[name] for name in names], cfg, scope='test', record=False)
    assert result.blocked == ('critical' in names or 'oversized' in names)
    assert [r.skill for r in result.reports] == [n for n in names if n != 'oversized']
    if 'critical' in names:
        assert any('critical static.shell.dangerous-rm' in message for message in result.errors)
    elif 'oversized' not in names:
        assert result.errors == ()
    if 'oversized' in names:
        assert any('audit.request.too-large' in message for message in result.errors + result.warnings)


# Source revocation is unconditional, including an unread oversized source and
# both batch orders. A nonmatching source is a valid neighbour under each policy.
@pytest.mark.parametrize('mode,threshold', [('strict', 'off'), ('strict', 'critical'), ('advisory', 'high')])
@pytest.mark.parametrize('names,revoked', [
    (('benign',), 'benign'),
    (('benign', 'oversized'), 'benign'),
    (('oversized', 'benign'), 'benign'),
    (('oversized',), 'oversized'),
    (('oversized', 'benign'), 'oversized'),
    (('benign',), 'unrelated'),
    (('oversized', 'benign'), 'unrelated'),
])
def test_oversize_preserves_source_revocation(batch, mode, threshold, names, revoked):
    plans, cfg = batch
    cfg = replace(cfg, audit=replace(cfg.audit, mode=mode, fail_on=threshold,
                                    revocations=['source:synthetic-' + revoked]))
    result = pipeline.gate_plans([plans[name] for name in names], cfg, scope='test', record=False)
    assert result.blocked == (revoked in names or (mode == 'strict' and 'oversized' in names))
    assert [r.skill for r in result.reports] == [n for n in names if n != 'oversized']
    if revoked in names:
        assert f'audit blocked: {revoked}: source synthetic-{revoked} is revoked' in result.errors
    elif mode != 'strict' or 'oversized' not in names:
        assert result.errors == ()


# Content-bound revocation on an admitted neighbour also survives either order;
# removing it allows advisory oversize; strict unread input remains blocked.
@pytest.mark.parametrize('mode,threshold', [('strict', 'off'), ('strict', 'critical'), ('advisory', 'high')])
@pytest.mark.parametrize('names', [('benign',), ('benign', 'oversized'), ('oversized', 'benign')])
def test_oversize_preserves_content_revocation(batch, mode, threshold, names):
    from csk.hashing import content_sha256
    plans, cfg = batch
    digest = content_sha256(plans['benign'].snapshot)
    cfg = replace(cfg, audit=replace(cfg.audit, mode=mode, fail_on=threshold, revocations=[digest]))
    group = [plans[name] for name in names]
    denied = pipeline.gate_plans(group, cfg, scope='test', record=False)
    assert denied.blocked
    assert f'audit blocked: benign: content hash {digest} is revoked' in denied.errors
    assert [r.skill for r in denied.reports] == ['benign']
    allowed = pipeline.gate_plans(group, replace(cfg, audit=replace(cfg.audit, revocations=[])),
                                  scope='test', record=False)
    assert allowed.blocked == (mode == 'strict' and 'oversized' in names)
    assert [r.skill for r in allowed.reports] == ['benign']


# Source matching works without opening an oversized file. Nonmatching sources
# allow advisory input; any content revocation blocks without inventing a hash.
@pytest.mark.parametrize('revocation,blocked', [
    ('source:synthetic-oversized', True), ('source:synthetic-*', True),
    ('source:unrelated', False), ('sha256:' + 'a' * 64, True),
])
def test_oversize_revocation_without_reads(batch, monkeypatch, revocation, blocked):
    import os
    plans, cfg = batch
    cfg = replace(cfg, audit=replace(cfg.audit, mode='advisory', revocations=[revocation]))
    def forbidden_open(*args, **kwargs):
        pytest.fail('oversized source descriptor opened')
    monkeypatch.setattr(os, 'open', forbidden_open)
    result = pipeline.gate_plans([plans['oversized']], cfg, scope='test', record=False)
    assert result.blocked == blocked
    assert result.reports == ()
    assert any('audit.request.too-large' in message for message in result.errors + result.warnings)
    if blocked and revocation.startswith('source:'):
        assert f'audit blocked: oversized: source {revocation.removeprefix("source:")} is revoked' in result.errors
    elif blocked:
        assert 'audit.request.too-large' in result.errors[0]
    else:
        assert result.errors == ()


# An advisory backend skip cannot bypass known source revocation either. The
# same unavailable backend with a nonmatching revocation remains warning-only.
@pytest.mark.parametrize('names', [('benign', 'oversized'), ('oversized', 'benign')])
@pytest.mark.parametrize('revoked', [True, False])
def test_backend_skip_preserves_source_revocation(batch, names, revoked):
    plans, cfg = batch
    cfg = replace(cfg, audit=replace(cfg.audit, mode='advisory', backend='unavailable',
                                    revocations=['source:synthetic-benign' if revoked else 'source:unrelated']))
    result = pipeline.gate_plans([plans[name] for name in names], cfg, scope='test', record=False)
    assert result.blocked == revoked
    assert any('audit backend failed:' in message for message in result.warnings)
    if revoked:
        assert 'audit blocked: benign: source synthetic-benign is revoked' in result.errors
    else:
        assert result.errors == ()


# Padding cannot hide CRITICAL findings in strict mode, including fail_on=off;
# benign admitted input remains allowed, while benign unread input also blocks.
@pytest.mark.parametrize('threshold', ['off', 'critical', 'high'])
@pytest.mark.parametrize('separate', [False, True])
def test_padded_critical_fails_closed(batch, threshold, separate):
    plans, cfg = batch
    cfg = replace(cfg, audit=replace(cfg.audit, fail_on=threshold))
    assert not pipeline.gate_plans([plans['benign']], cfg, scope='test', record=False).blocked
    critical = plans['critical']
    if separate:
        (critical.snapshot / 'padding').write_text('x' * 11000)
    else:
        (critical.snapshot / 'payload.sh').write_text('rm -rf /\n' + 'x' * 11000)
    for plan in (critical, plans['oversized']):
        result = pipeline.gate_plans([plan], cfg, scope='test', record=False)
        assert result.blocked
        assert 'audit.request.too-large' in result.errors[0]
        assert not result.reports


# A real revoked oversized digest blocks in both modes without reading it at
# gate time; clearing revocations restores advisory (but not strict) permission.
@pytest.mark.parametrize('mode', ['strict', 'advisory'])
@pytest.mark.parametrize('threshold', ['off', 'critical', 'high'])
def test_real_oversized_digest_fails_closed(batch, monkeypatch, mode, threshold):
    from csk.hashing import content_sha256
    plans, cfg = batch
    digest = content_sha256(plans['oversized'].snapshot)
    cfg = replace(cfg, audit=replace(cfg.audit, mode=mode, fail_on=threshold, revocations=[digest]))
    def forbidden(*args, **kwargs):
        pytest.fail('unread input was hashed')
    monkeypatch.setattr(pipeline.hashing, 'content_sha256', forbidden)
    denied = pipeline.gate_plans([plans['oversized']], cfg, scope='test', record=False)
    assert denied.blocked and 'audit.request.too-large' in denied.errors[0]
    allowed = pipeline.gate_plans([plans['oversized']], replace(cfg, audit=replace(cfg.audit, revocations=[])), scope='test', record=False)
    assert allowed.blocked == (mode == 'strict')


# Bad backend configuration is diagnosed before admission; valid configuration
# reaches the size diagnostic, and an admitted benign control is allowed.
def test_backend_resolution_precedes_admission(batch):
    plans, cfg = batch
    bad = replace(cfg, audit=replace(cfg.audit, backend='unavailable', fail_on='off'))
    result = pipeline.gate_plans([plans['oversized']], bad, scope='test', record=False)
    assert result.blocked
    assert 'Unsupported audit backend: unavailable' in result.errors[0]
    assert 'too-large' not in result.errors[0]
    assert not pipeline.gate_plans([plans['benign']], cfg, scope='test', record=False).blocked
