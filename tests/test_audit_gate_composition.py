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
    assert result.blocked == ('critical' in names)
    assert [r.skill for r in result.reports] == [n for n in names if n != 'oversized']
    if 'critical' in names:
        assert any('critical static.shell.dangerous-rm' in message for message in result.errors)
    else:
        assert result.errors == ()
    if 'oversized' in names:
        assert any('audit.request.too-large' in message for message in result.warnings)


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
    assert result.blocked == (revoked in names)
    assert [r.skill for r in result.reports] == [n for n in names if n != 'oversized']
    if revoked in names:
        assert f'audit blocked: {revoked}: source synthetic-{revoked} is revoked' in result.errors
    else:
        assert result.errors == ()


# Content-bound revocation on an admitted neighbour also survives either order;
# removing that revocation restores a warning-only result with the benign report.
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
    assert not allowed.blocked
    assert [r.skill for r in allowed.reports] == ['benign']


# Source matching works without opening an oversized file. Nonmatching sources
# and content-only revocations do not invent an identity for unread bytes.
@pytest.mark.parametrize('revocation,blocked', [
    ('source:synthetic-oversized', True), ('source:synthetic-*', True),
    ('source:unrelated', False), ('sha256:' + 'a' * 64, False),
])
def test_oversize_revocation_without_reads(batch, monkeypatch, revocation, blocked):
    import os
    plans, cfg = batch
    cfg = replace(cfg, audit=replace(cfg.audit, revocations=[revocation]))
    def forbidden_open(*args, **kwargs):
        pytest.fail('oversized source descriptor opened')
    monkeypatch.setattr(os, 'open', forbidden_open)
    result = pipeline.gate_plans([plans['oversized']], cfg, scope='test', record=False)
    assert result.blocked == blocked
    assert result.reports == ()
    assert any('audit.request.too-large' in message for message in result.warnings)
    if blocked:
        assert f'audit blocked: oversized: source {revocation.removeprefix("source:")} is revoked' in result.errors
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
