from contextlib import ExitStack
from dataclasses import dataclass, replace
from pathlib import Path
import ast
import inspect

import pytest

from csk import build_repository_pipeline as pipeline
from csk.git_admission import GitAdmissionError, SOURCE_UNAVAILABLE
from test_build_repository_pipeline import _Compiler, _request, _snapshot


@pytest.fixture(autouse=True)
def pass_state(monkeypatch):
    monkeypatch.setattr(pipeline, "_PASS_STATE", pipeline.AcquisitionPassState())


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
    monkeypatch.setattr(pipeline, 'acquisition_execution_boundary', lambda: (events.append('boundary'), pipeline._PASS_STATE.close()))
    results = runner(_requests(tmp_path, events, compiler))
    assert compiler.calls == 2
    assert all(result.artifact for result in results)
    assert events.index('acquire:last') < events.index('boundary') < events.index('compile-call'), 'acquisitions must finish before execution'
    assert events.index('boundary') < events.index('independent-external-audit')
    assert events.count('boundary') == 1


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
    monkeypatch.setattr(pipeline, 'acquisition_execution_boundary', lambda: (events.append('boundary'), pipeline._PASS_STATE.close()))
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
@pytest.mark.parametrize('field,value', [('identity_kind', 'operator-local-git'), ('identity', 'other'), ('object_format', 'sha256'), ('commit', 'b' * 40)])
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
    monkeypatch.setattr(pipeline, 'acquisition_execution_boundary', lambda: (events.append('boundary'), pipeline._PASS_STATE.close()))
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

# Real project/global retry loops and schema-2/3 post-external preimage guards
# run a synthetic external build pass; transport is not under test here. A real
# child process is joined by the compiler before any later pass can acquire.
@pytest.mark.parametrize('lane', ['project', 'global', 'schema2', 'schema3', 'multi'])
def test_serial_passes_through_install_paths(tmp_path, csk_home, skills_root, monkeypatch, lane):
    import json
    import os
    import subprocess
    import sys
    from csk import installer, global_install
    from csk.sources import publish
    from conftest import make_config, make_project, write_skillfile
    events = []
    workers = []
    pass_count = 0
    real_boundary = pipeline.acquisition_execution_boundary
    def boundary():
        real_boundary()
        events.append('closed')
    monkeypatch.setattr(pipeline, 'acquisition_execution_boundary', boundary)
    class Compiler(_Compiler):
        def compile(self, root, source_dir, command):
            assert pipeline._PASS_STATE.closed
            assert events[-1] == 'closed' or 'closed' in events
            process = subprocess.Popen([sys.executable, '-c', 'pass'])
            workers.append(process)
            try:
                assert process.wait(timeout=10) == 0
            finally:
                if process.poll() is None:
                    process.kill()
                    process.wait()
            events.append('reaped')
            return super().compile(root, source_dir, command)
    compiler = Compiler(events)
    def external(*args, **kwargs):
        nonlocal pass_count
        pass_count += 1
        events.append('pass-start')
        request = _request(tmp_path / str(pass_count), pipeline.Operation.INSTALL, events, compiler)
        def acquire():
            for worker in workers:
                # waitpid distinguishes a joined child from an unreaped zombie.
                if os.name != 'nt':
                    with pytest.raises(ChildProcessError):
                        os.waitpid(worker.pid, os.WNOHANG)
                assert worker.returncode == 0
            events.append('acquire')
            return _snapshot()
        pipeline.run_pipelines([replace(request, acquire=acquire)])
        return {}, []
    monkeypatch.setattr(installer, '_publish_external_builds', external)
    if lane.startswith('schema'):
        from test_source_install_transactions import _basic_fixture
        project, cfg, _ = _basic_fixture(tmp_path, csk_home)
        if lane == 'schema3':
            path = project / 'Skillfile.json'
            doc = json.loads(path.read_text())
            doc['schema_version'] = 3
            path.write_text(json.dumps(doc))
        def schema_external(*args, **kwargs):
            external(*args, **kwargs)
            return publish.empty_build_outputs(), []
        monkeypatch.setattr(publish, 'publish_schema2_external_builds', schema_external)
        original = publish.digest_live_targets
        drifted = False
        def drift(*args, **kwargs):
            nonlocal drifted
            value = original(*args, **kwargs)
            if pass_count == 1 and not drifted:
                drifted = True
                return {**value, 'changed-after-build': 'changed'}
            return value
        monkeypatch.setattr(publish, 'digest_live_targets', drift)
    else:
        project = make_project(tmp_path)
        write_skillfile(project, {'schema_version': 1, 'skills': []})
        cfg = make_config(csk_home, skills_root, project)
        if lane != 'multi':
            module = global_install if lane == 'global' else installer
            name = '_global_generation_probe' if lane == 'global' else '_project_generation_probe'
            factory = getattr(module, name)
            def probe(*args, **kwargs):
                original = factory(*args, **kwargs)
                class Generation:
                    def capture(self):
                        return {**original.capture(), 'external-generation': 'sha256:' + ('1' if pass_count else '0') * 64}
                return Generation()
            monkeypatch.setattr(module, name, probe)
        else:
            second = make_project(tmp_path / 'second')
            write_skillfile(second, {'schema_version': 1, 'skills': []})
            cfg.projects['second'] = replace(cfg.projects['app'], alias='second', path=second)
    if lane == 'global':
        global_install.init(csk_home)
        result = global_install.install(cfg)
        assert not result.errors, result.errors
    else:
        results = installer.install(cfg)
        assert all(not result.errors for result in results), [r.errors for r in results]
    assert pass_count == 2
    assert compiler.calls == 2
    assert events.count('closed') == 2
    starts = [i for i, event in enumerate(events) if event == 'pass-start']
    for start, end in zip(starts, starts[1:] + [len(events)]):
        chunk = events[start:end]
        assert chunk.index('acquire') < chunk.index('closed') < chunk.index('compile-call')
        assert chunk.count('reaped') == 1
    assert events.index('reaped') < starts[1]


# A no-close implementation is detected by the production execution guard;
# a later pass also refuses before calling its acquisition callback.
def test_skipped_close_plant_and_next_pass_guard(tmp_path, monkeypatch):
    events = []
    requests = _requests(tmp_path, events, _Compiler(events))
    monkeypatch.setattr(pipeline, 'acquisition_execution_boundary', lambda: None)
    with pytest.raises(pipeline.ExternalBuildError, match='boundary did not close'):
        pipeline.run_pipelines(requests)
    assert 'compile-call' not in events
    events.clear()
    with pytest.raises(pipeline.ExternalBuildError, match='previous acquisition pass is not closed or reaped'):
        pipeline.run_pipelines(requests)
    assert events == []


# An unverified worker teardown independently blocks new acquisition.
def test_unreaped_worker_guard(tmp_path):
    pipeline._PASS_STATE.workers_reaped = False
    events = []
    with pytest.raises(pipeline.ExternalBuildError, match='previous acquisition pass is not closed or reaped'):
        pipeline.run_pipelines(_requests(tmp_path, events, _Compiler(events)))
    assert events == []


# Local HEAD is mutable, and transport/endpoint provenance must not be inherited
# from an acquisition through another lane even for identical pinned bytes.
def test_memo_local_and_transport_provenance(tmp_path):
    request = _request(tmp_path, pipeline.Operation.INSTALL, [], _Compiler([]))
    memo = pipeline.AcquisitionMemo()
    memo.remember(request, _snapshot())
    assert memo.lookup(request) is not None
    changed = replace(request, declared=replace(request.declared, transport='ssh'), effective=replace(request.effective, transport='ssh'))
    assert memo.lookup(changed) is None
    from csk.sources.repository_policy import ResolvedEndpoint, EndpointProvenance
    endpoint = ResolvedEndpoint(request.effective.identity, 'https', None,
                EndpointProvenance('https://example.test/tools', 'example.test', None, 'example.test', 443, None, None))
    assert memo.lookup(replace(request, endpoints=(endpoint,))) is None
    local = replace(request, effective=replace(request.effective, identity_kind='operator-local-git'))
    memo.remember(local, _snapshot())
    assert memo.lookup(local) is None


# Offline successes retain the source-failure verdict for memo purposes; binding
# is rechecked even on a successful network memo hit.
def test_offline_fallback_and_memo_binding(tmp_path):
    request = _request(tmp_path, pipeline.Operation.INSTALL, [], _Compiler([]))
    first = pipeline.run_pipeline(request)
    calls = []
    def offline():
        calls.append(1)
        raise GitAdmissionError(SOURCE_UNAVAILABLE, 'offline')
    fallback = replace(request, acquire=offline, offline_snapshot_key=first.snapshot_key)
    with ExitStack() as stack:
        memo = pipeline.AcquisitionMemo()
        pipeline.acquire_pipeline(fallback, stack, memo)
        pipeline.acquire_pipeline(fallback, stack, memo)
        assert len(calls) == 2
        pipeline.acquire_pipeline(request, stack, memo)
        with pytest.raises(pipeline.ExternalBuildError, match='declared/effective source mismatch'):
            pipeline.acquire_pipeline(replace(request, declared=replace(request.declared, identity='other')), stack, memo)

# The production compiler adapter marks workers pending before invoking Go and
# confirms completion only after the synchronous builder returns. This fixture
# joins a real child, but does not qualify native Go resource controls.
@pytest.mark.parametrize('fail', [False, True])
def test_production_compiler_reaping_guard(tmp_path, monkeypatch, fail):
    import os
    import subprocess
    import sys
    from types import SimpleNamespace
    root = tmp_path / 'source'
    root.mkdir()
    _snapshot().materialize(root)
    artifact = tmp_path / 'artifact'
    artifact.write_bytes(b'tool')
    workers = []
    def build(request):
        assert not pipeline._PASS_STATE.workers_reaped
        child = subprocess.Popen([sys.executable, '-c', 'pass'])
        workers.append(child)
        try:
            assert child.wait(timeout=10) == 0
        finally:
            if child.poll() is None:
                child.kill()
                child.wait()
        if fail:
            raise RuntimeError('worker completion unconfirmed')
        return SimpleNamespace(artifact=SimpleNamespace(staged_path=artifact))
    monkeypatch.setattr(pipeline.go_v1, 'build', build)
    state = pipeline._PASS_STATE
    state.start()
    state.close()
    compiler = pipeline.ExistingGoV1Session(None)
    try:
        if fail:
            with pytest.raises(RuntimeError, match='worker completion unconfirmed'):
                compiler.compile(root, '.', 'tool')
        else:
            assert compiler.compile(root, '.', 'tool') == b'tool'
    finally:
        state.active = False
    for child in workers:
        if os.name != 'nt':
            with pytest.raises(ChildProcessError):
                os.waitpid(child.pid, os.WNOHANG)
    assert state.workers_reaped is not fail
    events = []
    if fail:
        with pytest.raises(pipeline.ExternalBuildError, match='previous acquisition pass is not closed or reaped'):
            pipeline.run_pipelines(_requests(tmp_path / 'next', events, _Compiler(events)))
        assert events == []
    else:
        pipeline.run_pipelines(_requests(tmp_path / 'next', events, _Compiler(events)))


# A mutable local selector must be acquired again; the admitted snapshot fixture
# changes HEAD between commands (raw Git identity proof belongs to admission tests).
def test_local_head_is_reacquired(tmp_path):
    request = _request(tmp_path, pipeline.Operation.INSTALL, [], _Compiler([]))
    snapshots = iter([_snapshot(), replace(_snapshot(), commit='b' * 40)])
    request = replace(request, acquire=lambda: next(snapshots), effective=replace(request.effective,
        identity_kind='operator-local-git', substituted=True,
        substitution=pipeline.SubstitutionState('local-path')))
    with ExitStack() as stack:
        memo = pipeline.AcquisitionMemo()
        first = pipeline.acquire_pipeline(request, stack, memo)
        second = pipeline.acquire_pipeline(request, stack, memo)
        assert first.snapshot.commit != second.snapshot.commit

# An earlier unclosed pass refuses even pre-acquisition assurance backends in
# the next real install attempt, including both schema versions and global.
@pytest.mark.parametrize('lane', ['project', 'schema2', 'schema3', 'global'])
def test_unclosed_pass_refuses_before_assurance(tmp_path, csk_home, skills_root, monkeypatch, lane):
    from csk import installer, global_install
    from conftest import make_project, make_config, write_skillfile
    project = make_project(tmp_path)
    version = {'project': 1, 'global': 1, 'schema2': 2, 'schema3': 3}[lane]
    doc = {'schema_version': version, 'skills': []}
    if version > 1:
        doc['sources'] = {'local': {'path': '.'}}
    write_skillfile(project, doc)
    cfg = make_config(csk_home, skills_root, project)
    calls = []
    monkeypatch.setattr(installer.audit_pipeline, 'gate_plans', lambda *a, **k: calls.append('audit'))
    pipeline._PASS_STATE.closed = False
    if lane == 'global':
        global_install.init(csk_home)
        results = [global_install.install(cfg)]
    else:
        results = installer.install(cfg)
    assert all('previous acquisition pass is not closed or reaped' in ' '.join(r.errors) for r in results)
    assert calls == []
