"""Bounded diagnostics for one synthetic inline-install/child-status fixture.

No production hook or verdict change. The child keeps ``python -m csk``;
fixture-local sitecustomize changes import timing/PYTHONPATH only. Observation
errors are raised after the original test body, together with its failure.
"""
from __future__ import annotations

import atexit
import base64
from contextlib import ExitStack, contextmanager
from contextvars import ContextVar
import hashlib
import json
import os
from pathlib import Path
import stat
import tempfile
from unittest.mock import patch

_ACTIVE = ContextVar('mcp_status_observation', default=None)
_KEY = 'PYTEST_CSK_STATUS_OBSERVATION'
_LIMIT = 512 * 1024


def _shape(path):
    try:
        info = path.lstat()
    except FileNotFoundError:
        return {'kind': 'absent'}
    # Ordinary symlinks stay links; other reparse entries (notably Windows
    # directory junctions) are opaque, never directories to traverse/read.
    if stat.S_ISLNK(info.st_mode):
        kind = 'link'
    elif getattr(info, 'st_file_attributes', 0) & stat.FILE_ATTRIBUTE_REPARSE_POINT:
        kind = 'reparse'
    else:
        kind = 'directory' if stat.S_ISDIR(info.st_mode) else 'file' if stat.S_ISREG(info.st_mode) else 'special'
    row = {'kind': kind, 'mode': info.st_mode, 'device': info.st_dev}
    if kind == 'link':
        row['destination'] = os.readlink(path)
    return row


def _fixture_state(roots):
    """Never follow links or inspect Git internals; cap count and byte capture."""
    result = {}
    remaining = _LIMIT
    def visit(path, label):
        nonlocal remaining
        if len(result) >= 256:
            raise ValueError('synthetic fixture observation exceeds 256 entries')
        row = _shape(path)
        result[label] = row
        if row['kind'] == 'directory':
            for child in sorted(path.iterdir()):
                if child.name != '.git':
                    visit(child, label + '/' + child.name)
        elif row['kind'] == 'file':
            size = path.lstat().st_size
            if size > min(64 * 1024, remaining):
                raise ValueError('synthetic fixture observation exceeds byte limit')
            data = path.read_bytes()
            remaining -= len(data)
            row.update(size=len(data), sha256=hashlib.sha256(data).hexdigest(),
                       bytes_base64=base64.b64encode(data).decode('ascii'))
    for index, root in enumerate(roots):
        visit(root, str(index))
    return result


class _Recorder:
    def __init__(self, roots):
        self.roots = [p if isinstance(p, Path) else Path(p) for p in roots]
        self.rows = []
        self.errors = []
        self.stages = []

    def capture(self, operation):
        try:
            return operation()
        except Exception as exc:
            self.errors.append(exc)
            return None

    def record(self, event, **data):
        if len(self.rows) >= 400:
            raise ValueError('status observation exceeds 400 records')
        self.rows.append({'event': event, **data})

    def contains(self, path):
        return any(path.is_relative_to(root) for root in self.roots + self.stages)

    def attach(self, stack):
        from csk import adapters
        from csk.sources import publish
        links, device = adapters._transaction_links_supported, adapters._device_id
        stage, digest = publish.stage_schema2_desired, publish.digest_target
        current = []
        def observed_device(path):
            value = device(path)
            if current:
                self.capture(lambda: current[-1]['devices'].append({'path': str(path), 'value': value}))
            return value
        def observed_links(staging, live):
            if not live.is_relative_to(self.roots[0]):
                return links(staging, live)
            row = {'stage': str(staging), 'live': str(live), 'devices': []}
            current.append(row)
            try:
                value = links(staging, live)
            finally:
                current.pop()
            self.capture(lambda: self.record('link-decision', **row, supported=value,
                                             installed=_shape(live), cached_tempfile=tempfile.tempdir,
                                             temp_env={k: os.environ.get(k) for k in ('TMPDIR', 'TEMP', 'TMP')}))
            return value
        def observed_stage(staging, *args, **kwargs):
            if kwargs['project_path'] != self.roots[0] or kwargs['home'] != self.roots[1]:
                return stage(staging, *args, **kwargs)
            self.stages.append(staging)
            value = stage(staging, *args, **kwargs)
            self.capture(lambda: self.record('planned-targets', adapter_mode=kwargs['adapter_mode'],
                targets=[{'key': [s.target_class, s.identifier], 'live': str(s.live_path),
                          'kind': s.kind, 'wanted': str(value.desired[(s.target_class, s.identifier)]),
                          'wanted_entry': _shape(value.desired[(s.target_class, s.identifier)])
                          if value.desired[(s.target_class, s.identifier)] is not None else None}
                         for s in kwargs['specs']]))
            return value
        def observed_digest(path, **kwargs):
            value = digest(path, **kwargs)
            if self.contains(path):
                self.capture(lambda: self.record('digest', path=str(path), target_kind=kwargs,
                                                 entry=_shape(path), digest=value))
            return value
        for module, name, value in [(adapters, '_device_id', observed_device),
                (adapters, '_transaction_links_supported', observed_links),
                (publish, 'stage_schema2_desired', observed_stage), (publish, 'digest_target', observed_digest)]:
            stack.enter_context(patch.object(module, name, value))


def _child():
    """Called only by the private bootstrap; missing/corrupt sidecar fails parent capture."""
    settings = json.loads(Path(os.environ[_KEY]).read_text(encoding='utf-8'))
    recorder = _Recorder(settings['roots'])
    recorder.capture(lambda: recorder.record('child-startup', cached_tempfile=tempfile.tempdir,
        temp_env={k: os.environ.get(k) for k in ('TMPDIR', 'TEMP', 'TMP')}))
    stack = ExitStack()
    def finish():
        stack.close()
        payload = {'pid': os.getpid(), 'argv': __import__('sys').argv,
                   'rows': recorder.rows, 'errors': [repr(e) for e in recorder.errors]}
        Path(settings['output']).write_text(json.dumps(payload), encoding='utf-8')
    atexit.register(finish)
    recorder.capture(lambda: recorder.attach(stack))


class Observation(_Recorder):
    def __init__(self, case, scratch, report):
        super().__init__([case.project, case.home, case.env['HOME']])
        # Static synthetic sibling directories only, never symlink/reparse roots.
        if any(p.parent != case.project.parent or _shape(p)['kind'] != 'directory' for p in self.roots):
            raise ValueError('status observation requires synthetic sibling fixture roots')
        self.case, self.scratch, self.report = case, scratch, report
        self.calls = []
        self.stack = ExitStack()

    def __enter__(self):
        from csk import config
        self.scratch.mkdir()
        helper = str(Path(__file__).resolve().parent)
        (self.scratch / 'sitecustomize.py').write_text(
            f'import sys\nsys.path.insert(0, {helper!r})\nfrom mcp_status_observation import _child\n_child()\n', encoding='utf-8')
        self.token = _ACTIVE.set(self)
        self.capture(lambda: self.record('parent', pid=os.getpid(), cached_tempfile=tempfile.tempdir,
            temp_env={k: os.environ.get(k) for k in ('TMPDIR', 'TEMP', 'TMP')},
            adapter_mode=config.load_config(Path(self.case.env['CSK_CONFIG'])).adapter_mode,
            state=_fixture_state(self.roots)))
        self.capture(lambda: self.attach(self.stack))
        return self

    def __exit__(self, kind, original, tb):
        _ACTIVE.reset(self.token)
        self.capture(self.stack.close)
        self.capture(lambda: self.record('final-state', state=_fixture_state(self.roots)))
        self.capture(lambda: self.report('call', 'MCP status observation', json.dumps({
            'rows': self.rows, 'calls': self.calls, 'errors': [repr(e) for e in self.errors]}, ensure_ascii=True, indent=2)))
        # Children have joined; unlink only our known scratch files, never recurse.
        self.capture(lambda: [p.unlink() for p in self.scratch.iterdir()])
        self.capture(self.scratch.rmdir)
        if self.errors:
            raise BaseExceptionGroup('MCP status observation failed',
                                     ([original] if original is not None else []) + self.errors)
        return False


def _validate_child(payload):
    """Require bounded capture structure and startup, not a substitute CLI verdict."""
    def require(condition):
        if not condition:
            raise ValueError('invalid child observation envelope or startup')
    def text(value, limit=4096):
        return isinstance(value, str) and len(value) <= limit
    require(isinstance(payload, dict) and {'pid', 'argv', 'rows', 'errors'} <= payload.keys())
    require(type(payload['pid']) is int and 0 < payload['pid'] < 2**63)
    argv, rows, errors = payload['argv'], payload['rows'], payload['errors']
    require(isinstance(argv, list) and 1 <= len(argv) <= 128 and all(text(a) and a for a in argv))
    require(isinstance(rows, list) and 1 <= len(rows) <= 400)
    require(all(isinstance(r, dict) and text(r.get('event'), 80) and r['event'] for r in rows))
    require(isinstance(errors, list) and len(errors) <= 400 and all(text(e) for e in errors))
    require(rows[0]['event'] == 'child-startup' and
            sum(r['event'] == 'child-startup' for r in rows) == 1)
    startup = rows[0]
    require({'cached_tempfile', 'temp_env'} <= startup.keys())
    require(startup['cached_tempfile'] is None or text(startup['cached_tempfile']))
    env = startup['temp_env']
    require(isinstance(env, dict) and set(env) == {'TMPDIR', 'TEMP', 'TMP'})
    require(all(v is None or text(v) for v in env.values()))
    return payload


@contextmanager
def status_capture(case):
    observer = _ACTIVE.get()
    if observer is None or observer.case is not case:
        yield None
        return
    index = len(observer.calls)
    output = observer.scratch / f'child-{index}.json'
    settings = observer.scratch / f'settings-{index}.json'
    call = {'before': observer.capture(lambda: _fixture_state(observer.roots))}
    observer.calls.append(call)
    observer.capture(lambda: settings.write_text(json.dumps({'roots': list(map(str, observer.roots)),
        'output': str(output)}), encoding='utf-8'))
    env = {**case.env, _KEY: str(settings),
           'PYTHONPATH': str(observer.scratch) + os.pathsep + case.env.get('PYTHONPATH', ''),
           'PYTHONDONTWRITEBYTECODE': '1'}
    class Capture:
        def __init__(self):
            self.env = env
        def result(self, value):
            def save():
                if len(value.stdout) + len(value.stderr) > _LIMIT:
                    raise ValueError('status output exceeds observation limit')
                call.update(argv=value.args, exit=value.returncode, stdout=value.stdout, stderr=value.stderr)
            observer.capture(save)
    try:
        yield Capture()
    finally:
        def read_child():
            if output.stat().st_size > _LIMIT:
                raise ValueError('child observation exceeds byte limit')
            child = _validate_child(json.loads(output.read_text(encoding='utf-8')))
            if child['errors']:
                raise ValueError('child observation errors: ' + repr(child['errors']))
            call['child'] = child
        observer.capture(read_child)
        call['after'] = observer.capture(lambda: _fixture_state(observer.roots))
