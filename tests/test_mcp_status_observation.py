"""Portable transparency/error controls; no reconstruction of hosted Windows state."""
from dataclasses import replace
import json
import os
import stat
from types import SimpleNamespace
from pathlib import Path
import sys

import pytest
from csk import adapters, cli, config
import mcp_status_observation as observation
import test_mcp_status as status
from test_mcp_install_acceptance import ENV_HEADER, _snapshot


@pytest.mark.parametrize('mode', ['auto', 'copy'])
def test_observer_preserves_real_status_verdict_and_state(tmp_path, monkeypatch, capsys, mode):
    # Real copy->link drift stays drift/check1; adjacent fixed-copy stays current/check0.
    # Only this input fixture models device identity, never the observation code.
    case = status._case(tmp_path / 'case')
    config.save_config(replace(config.load_config(Path(case.env['CSK_CONFIG'])), adapter_mode=mode))
    case.declare({'docs': ENV_HEADER})
    device = adapters._device_id
    with monkeypatch.context() as patch:
        for key in list(os.environ):
            if key.startswith('CSK_') and key not in case.env:
                patch.delenv(key)
        for key, value in case.env.items():
            patch.setenv(key, value)
        patch.setattr(sys.stdin, 'isatty', lambda: True)
        patch.setattr(sys.stdout, 'isatty', lambda: True)
        patch.setattr('builtins.input', lambda _: 'yes')
        patch.setattr(adapters, '_device_id', lambda p: device(p) + int(
            any(part.startswith('.csk-source-install-') for part in p.parts)))
        assert cli.main(['install', 'app', '--fix-gitignore']) == 0
    capsys.readouterr()
    before = _snapshot(case.project, case.home, Path(case.env['HOME']))
    plain = status._status(case, '--check', '--json')
    reports = []
    def report(*args):
        reports.append(args)
    scratch = tmp_path / 'observation'
    with observation.Observation(case, scratch, report) as observer:
        observed = status._status(case, '--check', '--json')
    assert (observed.args, observed.returncode, observed.stdout, observed.stderr) == (
        plain.args, plain.returncode, plain.stdout, plain.stderr)
    payload = json.loads(observed.stdout)[0]
    assert observed.returncode == (1 if mode == 'auto' else 0)
    assert payload['mcp'] == status._expected(['current'] * 3)
    assert payload['skills'][0]['label'] == ('drifted' if mode == 'auto' else 'up-to-date')
    assert payload['errors'] == [] and payload['clean'] == (mode == 'copy')
    assert _snapshot(case.project, case.home, Path(case.env['HOME'])) == before
    assert not scratch.exists() and observation._ACTIVE.get() is None
    call = observer.calls[0]
    assert call['before'] == call['after']
    assert call['child']['argv'][1:] == observed.args[3:]
    assert Path(call['child']['argv'][0]).name == '__main__.py'
    rows = call['child']['rows']
    plans = next(r['targets'] for r in rows if r['event'] == 'planned-targets')
    digests = {r['path']: r for r in rows if r['event'] == 'digest'}
    different = []
    for target in plans:
        live, wanted = digests[target['live']], digests[target['wanted']]
        if live['digest'] != wanted['digest']:
            different.append(target)
            assert live['entry']['kind'] == 'directory' and wanted['entry']['kind'] == 'link'
    assert len(different) == (3 if mode == 'auto' else 0)
    assert reports[0][:2] == ('call', 'MCP status observation')
    assert json.loads(reports[0][2])['calls'][0]['exit'] == plain.returncode

    if mode == 'auto':
        # A failing observer must retain the actual drift assertion, still run status,
        # emit its report, and restore context/owned scratch without product writes.
        failure = OSError('planted observation read failure')
        with monkeypatch.context() as patch:
            patch.setattr(observation, '_fixture_state', lambda _: (_ for _ in ()).throw(failure))
            with pytest.raises(BaseExceptionGroup) as caught:
                with observation.Observation(case, scratch, report) as broken:
                    result = status._status(case, '--check', '--json')
                    assert result.returncode == 0, 'original product drift assertion'
        assert any(isinstance(e, AssertionError) and 'original product drift assertion' in str(e)
                   for e in caught.value.exceptions)
        assert failure in caught.value.exceptions
        assert broken.calls[0]['exit'] == 1
        assert not scratch.exists() and observation._ACTIVE.get() is None
        assert _snapshot(case.project, case.home, Path(case.env['HOME'])) == before
        assert 'planted observation read failure' in reports[-1][2]
    else:
        # Python may continue after sitecustomize fails; a missing child record
        # must still fail observation visibly, never manufacture a green test.
        with pytest.raises(ExceptionGroup) as caught:
            with observation.Observation(case, scratch, report) as broken:
                (scratch / 'sitecustomize.py').write_text(
                    "raise RuntimeError('planted child observer failure')\n", encoding='utf-8')
                result = status._status(case, '--check', '--json')
                assert result.returncode == 0
        assert any(isinstance(e, FileNotFoundError) for e in caught.value.exceptions)
        assert 'planted child observer failure' in broken.calls[0]['stderr']
        assert json.loads(result.stdout)[0]['clean'] is True
        assert not scratch.exists() and observation._ACTIVE.get() is None
        assert _snapshot(case.project, case.home, Path(case.env['HOME'])) == before


def test_snapshot_is_bounded_and_does_not_follow_external_link(tmp_path):
    # Synthetic traversal records a link, never foreign contents; oversize is visible.
    root = tmp_path / 'fixture'
    root.mkdir()
    outside = tmp_path / 'outside'
    outside.write_bytes(b'not fixture content')
    (root / 'link').symlink_to(outside)
    result = observation._fixture_state([root])
    assert result['0/link']['kind'] == 'link'
    assert 'bytes_base64' not in result['0/link']
    assert all('sha256' not in row for row in result.values())
    (root / 'valid').write_bytes(b'fixture')
    assert observation._fixture_state([root])['0/valid']['size'] == 7
    (root / 'oversize').write_bytes(b'x' * (64 * 1024 + 1))
    with pytest.raises(ValueError, match='byte limit'):
        observation._fixture_state([root])
    assert outside.read_bytes() == b'not fixture content'


def test_observer_reports_capture_failure_without_original_failure(tmp_path, monkeypatch):
    # Standalone capture failure is not success; cached temp is read, never initialized.
    case = status._case(tmp_path / 'case')
    cached = observation.tempfile.tempdir
    reports = []
    scratch = tmp_path / 'observation'
    with observation.Observation(case, scratch, lambda *args: reports.append(args)) as observer:
        assert observer.rows[0]['cached_tempfile'] is cached
    assert observation.tempfile.tempdir is cached and not scratch.exists()
    assert len(reports[0][2].splitlines()) > 1
    assert json.loads(reports[0][2])['errors'] == []
    original = AssertionError('original body assertion')
    with pytest.raises(AssertionError) as unchanged:
        with observation.Observation(case, scratch, lambda *args: reports.append(args)):
            raise original
    assert unchanged.value is original
    assert not scratch.exists() and observation._ACTIVE.get() is None
    failure = OSError('planted report failure')
    def failed_report(*_):
        raise failure
    with pytest.raises(ExceptionGroup) as caught:
        with observation.Observation(case, scratch, failed_report):
            pass
    assert failure in caught.value.exceptions
    assert not scratch.exists() and observation._ACTIVE.get() is None
    # Explicit sibling-root admission refuses unrelated roots before capture.
    case.home = tmp_path / 'outside'
    with pytest.raises(ValueError, match='synthetic sibling'):
        observation.Observation(case, scratch, failed_report)


class _ReparseView(type(Path())):
    """Local Windows metadata view; all traversal and reads remain real fixture IO."""
    entries = frozenset()

    def lstat(self):
        info = super().lstat()
        if self in self.entries:
            return SimpleNamespace(st_mode=stat.S_IFDIR | 0o755, st_dev=info.st_dev,
                                   st_file_attributes=0x400)
        return info


def test_reparse_snapshot_and_root_admission(tmp_path):
    # Directory reparse entries never expose external bytes, at root or descendant.
    # This is static metadata modeling, not native Windows/concurrent ACL qualification.
    root = tmp_path / 'fixture'
    root.mkdir()
    (root / 'regular').mkdir()
    (root / 'regular' / 'owned').write_bytes(b'owned fixture')
    outside = tmp_path / 'outside'
    outside.mkdir()
    canary = outside / 'canary'
    canary.write_bytes(b'external synthetic canary')
    link = root / 'junction'
    link.symlink_to(outside, target_is_directory=True)
    before = (canary.read_bytes(), canary.stat().st_mode, canary.stat().st_mtime_ns)
    ordinary = observation._fixture_state([root])
    assert ordinary['0/junction']['kind'] == 'link'
    assert ordinary['0/regular/owned']['size'] == len(b'owned fixture')
    # Per-test subclass state cannot leak to other tests or shared pathlib objects.
    class View(_ReparseView):
        entries = frozenset([link])
    captured = observation._fixture_state([View(root)])
    assert captured['0/junction']['kind'] == 'reparse'
    assert not any(k.startswith('0/junction/') for k in captured)
    assert 'bytes_base64' not in captured['0/junction']
    assert captured['0/regular/owned'] == ordinary['0/regular/owned']
    class RootView(_ReparseView):
        entries = frozenset([root])
    assert observation._fixture_state([RootView(root)])['0']['kind'] == 'reparse'
    assert len(observation._fixture_state([RootView(root)])) == 1
    assert (canary.read_bytes(), canary.stat().st_mode, canary.stat().st_mtime_ns) == before

    # Each admitted root rejects both junction metadata and an ordinary symlink;
    # adjacent regular sibling roots remain admissible, with no capture/IO writes.
    siblings = [tmp_path / name for name in ('project', 'manager', 'home')]
    for p in siblings:
        p.mkdir()
    def case_for(roots):
        return SimpleNamespace(project=roots[0], home=roots[1], env={'HOME': roots[2]})
    scratch = tmp_path / 'observation'
    observation.Observation(case_for(siblings), scratch, lambda *_: None)
    for index in range(3):
        class AdmissionView(_ReparseView):
            entries = frozenset([siblings[index]])
        roots = [AdmissionView(p) for p in siblings]
        with pytest.raises(ValueError, match='synthetic sibling'):
            observation.Observation(case_for(roots), scratch, lambda *_: None)
        roots = list(siblings)
        root_link = tmp_path / f'root-link-{index}'
        root_link.symlink_to(outside, target_is_directory=True)
        roots[index] = root_link
        with pytest.raises(ValueError, match='synthetic sibling'):
            observation.Observation(case_for(roots), scratch, lambda *_: None)
    assert not scratch.exists()
    assert (canary.read_bytes(), canary.stat().st_mode, canary.stat().st_mtime_ns) == before


def test_child_envelope_rejects_incomplete_records_and_preserves_failure(tmp_path):
    # Actual reader IO rejects absent/type-invalid/startup-incomplete evidence, keeps
    # the exact original failure, and restores state/context/scratch. No CLI is faked.
    case = status._case(tmp_path / 'case')
    scratch = tmp_path / 'observation'
    before = observation._fixture_state([case.project, case.home, Path(case.env['HOME'])])
    cached = observation.tempfile.tempdir
    def valid():
        return {'pid': os.getpid(), 'argv': ['__main__.py', 'status', 'app'],
                'rows': [{'event': 'child-startup', 'cached_tempfile': None,
                          'temp_env': {k: None for k in ('TMPDIR', 'TEMP', 'TMP')}}],
                'errors': []}
    # Structurally complete IO control; actual child acceptance is separately proved
    # by both real clean/drift transparency cases above.
    with observation.Observation(case, scratch, lambda *_: None) as good:
        with observation.status_capture(case):
            (scratch / 'child-0.json').write_text(json.dumps(valid()), encoding='utf-8')
    assert good.calls[0]['child'] == valid()
    invalid = [[], {'errors': []}]
    for key in valid():
        record = valid()
        del record[key]
        invalid.append(record)
    for key, value in [('pid', True), ('pid', 0), ('argv', 'status'), ('argv', [1]),
                       ('argv', []), ('rows', {}), ('rows', []), ('errors', {}),
                       ('errors', [1])]:
        invalid.append({**valid(), key: value})
    for row in [{}, {'event': 'digest'}, {'event': 'child-startup'},
                {'event': 'child-startup', 'cached_tempfile': 1, 'temp_env': {}},
                {'event': 'child-startup', 'cached_tempfile': None, 'temp_env': []},
                {'event': 'child-startup', 'cached_tempfile': None, 'temp_env': {}},
                {'event': 'child-startup', 'cached_tempfile': None,
                 'temp_env': {'TMPDIR': 1, 'TEMP': None, 'TMP': None}}]:
        invalid.append({**valid(), 'rows': [row]})
    invalid.append({**valid(), 'rows': valid()['rows'] * 2})
    invalid.append({**valid(), 'rows': valid()['rows'] + [{'event': 'x'}] * 400})
    original = AssertionError('original product assertion')
    reports = []
    for record in invalid:
        with pytest.raises(BaseExceptionGroup) as caught:
            with observation.Observation(case, scratch, lambda *a: reports.append(a)) as broken:
                with observation.status_capture(case):
                    (scratch / 'child-0.json').write_text(json.dumps(record), encoding='utf-8')
                    raise original
        assert caught.value.exceptions[0] is original
        assert any(isinstance(e, ValueError) and 'invalid child observation' in str(e)
                   for e in caught.value.exceptions)
        assert 'child' not in broken.calls[0]
        assert not scratch.exists() and observation._ACTIVE.get() is None
        assert observation.tempfile.tempdir is cached
        assert observation._fixture_state([case.project, case.home, Path(case.env['HOME'])]) == before
        assert 'invalid child observation' in reports[-1][2]
    # Well-formed child-reported capture errors remain visible, not capture success.
    with pytest.raises(ExceptionGroup, match='observation failed') as caught:
        with observation.Observation(case, scratch, lambda *_: None):
            with observation.status_capture(case):
                (scratch / 'child-0.json').write_text(json.dumps({**valid(), 'errors': ['child capture failed']}))
    assert any('child observation errors' in str(e) for e in caught.value.exceptions)
    assert not scratch.exists() and observation._ACTIVE.get() is None


def test_child_capture_limits_and_cleanup_keep_original_failure(tmp_path):
    # Actual byte/row bounds and owned cleanup failures remain visible alongside
    # the original error; no product writes or leaked observer context are allowed.
    case = status._case(tmp_path / 'case')
    scratch = tmp_path / 'observation'
    before = observation._fixture_state([case.project, case.home, Path(case.env['HOME'])])
    original = AssertionError('original body failure')
    with pytest.raises(BaseExceptionGroup) as caught:
        with observation.Observation(case, scratch, lambda *_: None) as observer:
            with observation.status_capture(case):
                (scratch / 'child-0.json').write_bytes(b'x' * (observation._LIMIT + 1))
                raise original
    assert caught.value.exceptions[0] is original
    assert any(isinstance(e, ValueError) and 'child observation exceeds byte limit' in str(e)
               for e in caught.value.exceptions)
    assert 'child' not in observer.calls[0]
    assert not scratch.exists() and observation._ACTIVE.get() is None
    recorder = observation._Recorder([])
    for index in range(400):
        recorder.record('bounded', index=index)
    with pytest.raises(ValueError, match='400 records'):
        recorder.record('overflow')
    assert len(recorder.rows) == 400
    failure = PermissionError('owned scratch unlink fault')
    class CleanupView(type(scratch)):
        def unlink(self, *args, **kwargs):
            raise failure
    # This local path view injects the exact owned unlink fault, not pathlib globally.
    try:
        with pytest.raises(BaseExceptionGroup) as caught:
            with observation.Observation(case, CleanupView(scratch), lambda *_: None):
                raise original
        assert caught.value.exceptions[0] is original and failure in caught.value.exceptions
        assert scratch.exists() and observation._ACTIVE.get() is None
        assert observation._fixture_state([case.project, case.home, Path(case.env['HOME'])]) == before
    finally:
        for child in scratch.iterdir():
            child.unlink()
        scratch.rmdir()
    assert not scratch.exists()


def test_fresh_consent_install_ignores_parent_temp_cache_boundary(tmp_path, monkeypatch, request):
    # The real consent child and status share fixture temp settings despite a
    # parent-only cached-root/device model of the recorded Windows C:/D: boundary.
    # Actual transactions and approval run; no shared tempfile cache is patched.
    import tempfile
    from types import SimpleNamespace
    from csk.sources import publish
    case = status._case(tmp_path / 'case')
    case.declare({'docs': ENV_HEADER})
    cached = tempfile.tempdir
    parent_env = {k: os.environ.get(k) for k in ('TMPDIR', 'TEMP', 'TMP')}
    prior = tmp_path / 'parent-cache'
    prior.mkdir()
    temporary, device = publish.tempfile.TemporaryDirectory, adapters._device_id
    parent_allocations = []
    def parent_temporary(*args, **kwargs):
        kwargs.setdefault('dir', prior)
        value = temporary(*args, **kwargs)
        parent_allocations.append(value.name)
        return value
    def parent_device(path):
        return device(path) + int(path.is_relative_to(prior))
    scratch = tmp_path / 'observation'
    with monkeypatch.context() as local:
        local.setattr(publish, 'tempfile', SimpleNamespace(TemporaryDirectory=parent_temporary))
        local.setattr(adapters, '_device_id', parent_device)
        with observation.Observation(case, scratch, request.node.add_report_section) as observer:
            approved = status._approve_install(case)
            assert approved.returncode == 0, approved.stdout + approved.stderr
            assert 'Approve 1 MCP server(s) for app (claude_code,codex_cli,cursor)? [y/N]' in approved.stdout
            before = _snapshot(case.project, case.home, Path(case.env['HOME']))
            checked = status._status(case, '--check', '--json')
            payload = json.loads(checked.stdout)[0]
            assert checked.returncode == 0 and payload['clean'] is True
            assert payload['mcp'] == status._expected(['current'] * 3)
            assert payload['skills'][0]['label'] == 'up-to-date' and payload['errors'] == []
            assert _snapshot(case.project, case.home, Path(case.env['HOME'])) == before
    assert parent_allocations == []
    assert tempfile.tempdir is cached
    assert {k: os.environ.get(k) for k in parent_env} == parent_env
    assert not scratch.exists() and observation._ACTIVE.get() is None
    grant, = (case.home / 'state/mcp/consent').glob('*.json')
    assert len(json.loads(grant.read_bytes())['approved']) == 1
    assert (case.project / '.agents/skills/seed/SKILL.md').read_bytes() == (case.project / 'source/seed/SKILL.md').read_bytes()
    assert len(observer.calls) == 2
    for call in observer.calls:
        child = call['child']
        startup = child['rows'][0]
        assert startup['event'] == 'child-startup'
        assert startup['temp_env'] == {k: case.env[k] for k in ('TMPDIR', 'TEMP', 'TMP')}
        for row in child['rows']:
            if row['event'] == 'link-decision':
                assert Path(row['stage']).is_relative_to(Path(case.env['TMPDIR']))
    install_rows, status_rows = [call['child']['rows'] for call in observer.calls]
    install_links = [r for r in install_rows if r['event'] == 'link-decision']
    status_links = [r for r in status_rows if r['event'] == 'link-decision']
    assert len(install_links) == len(status_links) == 3
    assert {r['live']: r['supported'] for r in install_links} == {
        r['live']: r['supported'] for r in status_links}
    install_plan = next(r for r in install_rows if r['event'] == 'planned-targets')
    status_plan = next(r for r in status_rows if r['event'] == 'planned-targets')
    assert install_plan['adapter_mode'] == status_plan['adapter_mode'] == 'auto'
    def adapter_kinds(plan):
        return {t['live']: t['wanted_entry']['kind'] for t in plan['targets']
                if t['key'][0] == '60-adapter-ledger' and t['kind'] == 'entry'}
    assert len(adapter_kinds(install_plan)) == 3
    assert adapter_kinds(install_plan) == adapter_kinds(status_plan)
    digests = {r['path']: r['digest'] for r in status_rows if r['event'] == 'digest'}
    for target in status_plan['targets']:
        assert digests[target['live']] == digests[target['wanted']]
    assert observer.calls[1]['before'] == observer.calls[1]['after']
    assert publish.tempfile is tempfile and adapters._device_id is device
