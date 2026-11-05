from contextlib import ExitStack
from dataclasses import dataclass, replace
from pathlib import Path
import ast
import inspect

import pytest

from csk import build_repository_pipeline as pipeline
from csk.git_admission import GitAdmissionError, SOURCE_UNAVAILABLE
from test_build_repository_pipeline import _Compiler, _request, _snapshot


def _requests(tmp_path, events, compiler):
    result = []
    for name in ('first', 'last'):
        request = _request(tmp_path / name, pipeline.Operation.INSTALL, events, compiler)
        def acquire(name=name):
            events.append('acquire:' + name)
            return _snapshot()
        result.append(replace(request, declared=replace(request.declared, identity=name), effective=replace(request.effective, identity=name), acquire=acquire))
    return result


def _assert_order(tmp_path, monkeypatch, runner):
    events = []
    compiler = _Compiler(events)
    monkeypatch.setattr(pipeline, 'acquisition_execution_boundary', lambda: events.append('boundary'))
    results = runner(_requests(tmp_path, events, compiler))
    assert compiler.calls == 2
    assert all(result.artifact for result in results)
    assert events.index('acquire:last') < events.index('boundary') < events.index('compile-call'), 'acquisitions must finish before execution'
    assert events.index('boundary') < events.index('independent-external-audit')


# The real compiler callback and arbitrary audits wait for both sources and one
# boundary; the paired interleaving plant must fail this same order assertion.
def test_all_sources_before_execution(tmp_path, monkeypatch):
    _assert_order(tmp_path, monkeypatch, pipeline.run_pipelines)


def test_order_checker_rejects_interleaving_plant(tmp_path, monkeypatch):
    def interleaved(requests):
        return [pipeline.run_pipeline(request) for request in requests]
    with pytest.raises(AssertionError, match='acquisitions must finish before execution'):
        _assert_order(tmp_path, monkeypatch, interleaved)


# Last-source refusal prevents even the first audit, compile or cache publication;
# successful two-source execution above is the positive control.
def test_last_source_failure_publishes_nothing(tmp_path, monkeypatch):
    events = []
    compiler = _Compiler(events)
    requests = _requests(tmp_path, events, compiler)
    def unavailable():
        raise GitAdmissionError(SOURCE_UNAVAILABLE, 'last source unavailable')
    requests[-1] = replace(requests[-1], acquire=unavailable)
    monkeypatch.setattr(pipeline, 'acquisition_execution_boundary', lambda: events.append('boundary'))
    with pytest.raises(pipeline.ExternalBuildError) as failure:
        pipeline.run_pipelines(requests)
    assert failure.value.code == SOURCE_UNAVAILABLE
    assert compiler.calls == 0
    assert 'boundary' not in events
    assert 'independent-external-audit' not in events
    assert not list(tmp_path.rglob('external-cache'))


# Cache reuse retains one acquisition per distinct state on each invocation,
# while the populated second invocation performs zero additional compiles.
def test_populated_cache_still_acquires_without_compiling(tmp_path):
    events = []
    compiler = _Compiler(events)
    requests = _requests(tmp_path, events, compiler)
    pipeline.run_pipelines(requests)
    assert compiler.calls == 2
    events.clear()
    results = pipeline.run_pipelines(requests)
    assert [event for event in events if event.startswith('acquire:')] == ['acquire:first', 'acquire:last']
    assert compiler.calls == 2
    assert [result.state for result in results] == ['cache-hit', 'cache-hit']


# Successful validation memoizes the same source for a second command, but each
# requested target still receives its own descriptor/binding validation.
def test_memo_hit(tmp_path):
    events = []
    compiler = _Compiler(events)
    request = _requests(tmp_path, events, compiler)[0]
    with ExitStack() as stack:
        memo = pipeline.AcquisitionMemo()
        pipeline.acquire_pipeline(request, stack, memo)
        pipeline.acquire_pipeline(request, stack, memo)
    assert events.count('acquire:first') == 1
    assert events.count('descriptor-and-target-validation') == 2


# Every state-key dimension distinguishes entries; this key test supplements
# real successful memo hit/miss acquisition below without inventing raw objects.
@pytest.mark.parametrize('field,value', [('identity', 'other'), ('object_format', 'sha256'), ('commit', 'b' * 40)])
def test_memo_state_key_dimensions(tmp_path, field, value):
    request = _request(tmp_path, pipeline.Operation.INSTALL, [], _Compiler([]))
    memo = pipeline.AcquisitionMemo()
    memo.remember(request, _snapshot())
    assert memo.lookup(request) is not None
    assert memo.lookup(replace(request, effective=replace(request.effective, **{field: value}))) is None


def test_memo_tag_key(tmp_path):
    request = _request(tmp_path, pipeline.Operation.INSTALL, [], _Compiler([]))
    memo = pipeline.AcquisitionMemo()
    memo.remember(request, _snapshot())
    assert memo.lookup(replace(request, declared=replace(request.declared, tag='v2'))) is None


def test_memo_distinct_identity_acquires_twice(tmp_path):
    events = []
    requests = _requests(tmp_path, events, _Compiler(events))
    with ExitStack() as stack:
        memo = pipeline.AcquisitionMemo()
        for request in requests:
            pipeline.acquire_pipeline(request, stack, memo)
    assert events.count('acquire:first') == events.count('acquire:last') == 1


# A failed fetch and a failed target validation are retried rather than cached;
# a subsequent valid acquisition proves the memo does not reject everything.
@pytest.mark.parametrize('failure_kind', ['source', 'target'])
def test_memo_does_not_remember_failure(tmp_path, failure_kind):
    events = []
    request = _requests(tmp_path, events, _Compiler(events))[0]
    calls = []
    def acquire():
        calls.append(1)
        if failure_kind == 'source' and len(calls) == 1:
            raise GitAdmissionError(SOURCE_UNAVAILABLE, 'temporary source refusal')
        return _snapshot()
    request = replace(request, acquire=acquire)
    bad = replace(request, target='absent') if failure_kind == 'target' else request
    with ExitStack() as stack:
        memo = pipeline.AcquisitionMemo()
        with pytest.raises(pipeline.ExternalBuildError) as failure:
            pipeline.acquire_pipeline(bad, stack, memo)
        assert failure.value.code == (SOURCE_UNAVAILABLE if failure_kind == 'source' else pipeline.DESCRIPTOR_INVALID)
        pipeline.acquire_pipeline(request, stack, memo)
    assert len(calls) == 2


# This structural scheduling check pins the assurance gates ahead of external
# acquisition in all three callers; it does not qualify configured audit tools.
def test_assurance_gate_precedes_external_acquisition():
    from csk import installer, global_install
    from csk.sources import publish
    for function, gate, external in [
        (installer._install_project_once, 'gate_plans', '_publish_external_builds'),
        (global_install._install_once, 'gate_plans', '_publish_external_builds'),
        (publish._plan_and_publish_schema2, 'run_schema2_audit_gate', 'publish_schema2_external_builds'),
    ]:
        tree = ast.parse(inspect.getsource(function))
        calls = [(node.lineno, node.func.attr if isinstance(node.func, ast.Attribute) else node.func.id)
                 for node in ast.walk(tree) if isinstance(node, ast.Call)
                 and isinstance(node.func, (ast.Attribute, ast.Name))]
        assert min(line for line, name in calls if name == gate) < min(line for line, name in calls if name == external)

# A future close/verify seam refusal must propagate before any arbitrary audit,
# compiler or cache publication; the successful seam is covered above.
def test_boundary_refusal_prevents_execution(tmp_path, monkeypatch):
    events = []
    compiler = _Compiler(events)
    def refuse():
        raise RuntimeError('closure not verified')
    monkeypatch.setattr(pipeline, 'acquisition_execution_boundary', refuse)
    with pytest.raises(RuntimeError, match='closure not verified'):
        pipeline.run_pipelines(_requests(tmp_path, events, compiler))
    assert compiler.calls == 0
    assert 'independent-external-audit' not in events
    assert not list(tmp_path.rglob('external-cache'))

# Exercise the installer scheduling/marker loop with real pipeline validation,
# receipts and cache, but fake network/toolchain; this is not platform qualification.
@pytest.mark.parametrize('last_fails', [False, True])
def test_installer_batches_sources_before_publication(tmp_path, monkeypatch, last_fails):
    from contextlib import nullcontext
    from types import SimpleNamespace
    from csk import installer, build_repository, dev_substitutions
    from conftest import make_config
    events = []
    compiler = _Compiler(events)
    config = make_config(tmp_path / 'home', tmp_path / 'skills', tmp_path / 'project')
    nodes = []
    for name in ('first', 'last'):
        url = f'https://example.test/{name}.git'
        parsed = build_repository.parse_repository_source(url)
        repository = build_repository.BuildRepository('tools', url, parsed.identity, parsed.transport,
                            build_repository.LockedCommit('sha1', _snapshot().commit))
        command = SimpleNamespace(repository='tools', target='tool')
        nodes.append(SimpleNamespace(name=name, repo=tmp_path / name,
                     spec=SimpleNamespace(commands={'tool': command}, build_repositories={'tools': repository})))
    monkeypatch.setattr(installer, 'supports_external_builds', lambda: True)
    monkeypatch.setattr(installer, '_active_external_build_command_names', lambda node: {'tool'})
    selection = {(node.name, 'tool'): None for node in nodes}
    monkeypatch.setattr(installer, '_resolve_build_ssh_credentials', lambda *a, **k: selection)
    monkeypatch.setattr(installer, '_resolve_build_https_credentials', lambda *a, **k: selection)
    @dataclass(frozen=True)
    class ToolStub:
        https_trust_args: tuple = ()
    monkeypatch.setattr(installer, '_external_git_tool', lambda *a, **k: ToolStub())
    monkeypatch.setattr(installer.build_toolchain, 'establish_toolchain', lambda *a, **k: nullcontext(None))
    monkeypatch.setattr(pipeline, 'ExistingGoV1Session', lambda session: compiler)
    def acquire(plan, *args, **kwargs):
        name = 'last' if 'last' in str(plan) else 'first'
        events.append('acquire:' + name)
        if name == 'last' and last_fails:
            raise GitAdmissionError(SOURCE_UNAVAILABLE, 'last source unavailable')
        return SimpleNamespace(snapshot=_snapshot())
    monkeypatch.setattr(installer.source_transport, 'acquire_plan', acquire)
    monkeypatch.setattr(installer, '_external_static_audit', lambda subject: events.append('audit'))
    monkeypatch.setattr(pipeline, 'acquisition_execution_boundary', lambda: events.append('boundary'))
    def install():
        with ExitStack() as stack:
            return installer._publish_external_builds(config, project_root=tmp_path / 'project',
                nodes=nodes, substitutions=dev_substitutions.DevManifest(2, {}, {}),
                operator_search_path=None, stack=stack, dry_run=False, marker_roots=())
    if last_fails:
        with pytest.raises(pipeline.ExternalBuildError) as failure:
            install()
        assert failure.value.code == SOURCE_UNAVAILABLE
        assert compiler.calls == 0
        assert 'audit' not in events
        assert not (config.path.parent / 'external-builds').exists()
    else:
        published, _ = install()
        assert set(published) == {'first', 'last'}
        assert events.index('acquire:last') < events.index('boundary') < events.index('audit') < events.index('compile-call')
        assert published['first']['tool'].marker.declared_identity.value != published['last']['tool'].marker.declared_identity.value
