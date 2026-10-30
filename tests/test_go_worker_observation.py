"""Portable observations only; no native worker/domain/Go execution."""
from contextvars import ContextVar
from types import SimpleNamespace
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import threading
import time

import pytest

from csk.builds import go_v1
from go_worker_observation import CacheAudit, Recorder


# A valid real client send must expose the missing request boundary without exposing its frame.
def test_request_boundary_is_observable(monkeypatch):
    rows=[]
    sink=ContextVar('test_sink',default=None)
    sink.set(lambda stage, **values: rows.append((stage,values)))
    monkeypatch.setattr(go_v1,'_OBSERVATION_SINK',sink,raising=False)
    client=object.__new__(go_v1._WorkerClient)
    client.process=SimpleNamespace(stdin=io.BytesIO(),pid=73,returncode=None)
    client._expired=threading.Event();client.deadline=go_v1.time.monotonic()+10
    frame={'kind':'request','nonce':'secret-canary'}
    client._send(frame)
    client.process.stdin.seek(0)
    assert go_v1._read_message(client.process.stdin)==frame
    assert [stage for stage,_ in rows]==['request-send']
    assert 'secret-canary' not in repr(rows)


@pytest.fixture
def recorder(tmp_path):
    anchor = tmp_path / "trace"
    anchor.mkdir(mode=0o700)
    sink = anchor / "trace.json"
    with sink.open("x+b") as stream:
        yield Recorder(anchor, sink, stream)


# Invalid fields/paths never reach serialization; valid rows exercise event and byte bounds.
@pytest.mark.parametrize("budget", [False, True])
def test_closed_schema_caps_and_secret_exclusion(recorder, budget):
    recorder.record("secret-canary", argv="secret-canary")
    recorder.record("node-start", relative="secret-canary")
    recorder.record("node-start", deadline=float("nan"))
    recorder.record("node-start", operation=["write"])
    recorder.record("node-start", deadline=10**1000)
    for _ in range(200):
        if budget:
            recorder.record("client-deadline", deadline=123.0, remaining=-2.0)
        else:
            recorder.record("node-start")
    assert recorder.flush()
    raw = recorder.sink.read_bytes()
    payload = json.loads(raw)
    assert len(raw) <= 16 * 1024
    assert 0 < len(payload["events"]) <= 128
    assert payload["dropped"] == 205 - len(payload["events"])
    if budget:
        assert all(row["deadline"] == 123 for row in payload["events"])
        assert len(payload["events"]) < 128
    else:
        assert len(payload["events"]) == 128
    assert b"secret-canary" not in raw


# A bounded scheduling pause exposes two real callers crossing the final event slot.
def test_concurrent_recorder_cap(recorder, monkeypatch):
    for _ in range(127):
        recorder.record("node-start")
    barrier = threading.Barrier(2)
    original_encode = recorder._encode
    def paused_encode(events=None):
        try:
            barrier.wait(timeout=0.1)
        except threading.BrokenBarrierError:
            pass  # serialization makes the second caller wait outside encoding
        return original_encode(events)
    with monkeypatch.context() as patch:
        patch.setattr(recorder, "_encode", paused_encode)
        threads = [threading.Thread(target=lambda: recorder.record("node-end")) for _ in range(2)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(2)
            assert not thread.is_alive()
    assert len(recorder.events) == 128
    assert recorder.dropped == 1
    assert recorder.flush()
    assert len(recorder.sink.read_bytes()) <= 16 * 1024


# Static outside/linked/replaced sinks preserve bytes, modes and inode. Windows
# cannot unlink this open x+b stream: plant alternate namespace entries instead;
# the replacement case then exercises a real path/fd mismatch, not a rename race.
# POSIX retains the actual same-name replacement; exclusive x+b is the control.
@pytest.mark.parametrize("kind", ["outside", "symlink", "hardlink", "ancestor-link", "replaced", "closed"])
def test_unsafe_sink_is_not_written(kind, recorder, tmp_path):
    outside = tmp_path / "outside"
    outside.write_bytes(b"outside-canary")
    outside.chmod(0o444)
    before = outside.stat()
    original_fd = recorder.descriptor
    try:
        if kind == "outside":
            recorder.sink = outside
            recorder.descriptor = os.open(outside, os.O_RDONLY)
        elif kind == "symlink":
            recorder.sink = recorder.anchor / "linked.json"
            recorder.sink.symlink_to(outside)
            assert recorder.sink.is_symlink()
        elif kind == "hardlink":
            recorder.sink = recorder.anchor / "shared.json"
            os.link(outside, recorder.sink)
            assert recorder.sink.stat().st_nlink == 2
        elif kind == "ancestor-link":
            (recorder.anchor / "link").symlink_to(tmp_path, target_is_directory=True)
            recorder.sink = recorder.anchor / "link" / "outside"
        elif kind == "replaced":
            if os.name == "nt":
                recorder.sink = recorder.anchor / "replacement.json"
            else:
                recorder.sink.unlink()
            recorder.sink.write_bytes(b"replacement")
            descriptor = os.fstat(original_fd)
            candidate = recorder.sink.stat()
            assert (candidate.st_dev, candidate.st_ino) != (descriptor.st_dev, descriptor.st_ino)
        else:
            recorder.descriptor = os.dup(original_fd)
            os.close(recorder.descriptor)
        recorder.record("node-start")
        assert not recorder.flush()
        assert recorder.sink_failures == 1
        after = outside.stat()
        assert outside.read_bytes() == b"outside-canary"
        assert (after.st_dev, after.st_ino, after.st_mode) == (before.st_dev, before.st_ino, before.st_mode)
        if kind == "replaced":
            assert recorder.sink.read_bytes() == b"replacement"
    finally:
        if kind == "outside":
            os.close(recorder.descriptor)


# A failed observer/sink cannot alter body exception, cause/context, cleanup or success.
@pytest.mark.parametrize("failure", ["none", "recorder", "sink"])
def test_node_failure_identity_and_success(recorder, monkeypatch, failure):
    primary = RuntimeError("secret-canary")
    cause, context = LookupError("cause"), ValueError("context")
    primary.__cause__, primary.__context__ = cause, context
    if failure == "recorder":
        def fail(*args, **kwargs):
            raise KeyboardInterrupt("observer only")
        monkeypatch.setattr(recorder, "record", fail)
    elif failure == "sink":
        recorder.descriptor = -1
    cleaned = []
    with pytest.raises(RuntimeError) as caught:
        with recorder.node():
            try:
                raise primary
            finally:
                cleaned.append(True)
    assert caught.value is primary
    assert primary.__cause__ is cause and primary.__context__ is context
    assert cleaned == [True] and go_v1._OBSERVATION_SINK.get() is None
    with recorder.node():
        value = 73
    assert value == 73
    if failure == "none":
        raw = recorder.sink.read_bytes()
        assert b"secret-canary" not in raw
        assert json.loads(raw)["events"][-1]["stage"] == "node-end"


def _client(deadline):
    client = object.__new__(go_v1._WorkerClient)
    client.process = SimpleNamespace(stdin=io.BytesIO(), stdout=io.BytesIO(), pid=73, returncode=None)
    client._expired = threading.Event()
    client.deadline = deadline
    client.nonce = "fixture-nonce"
    return client


# Exhausted/equal ORIGINAL budgets refuse without I/O/reset; live send/receive still validate nonce.
@pytest.mark.parametrize("remaining", [-1.0, 0.0, 10.0])
def test_original_budget_and_protocol_controls(recorder, monkeypatch, remaining):
    monkeypatch.setattr(go_v1, "time", SimpleNamespace(monotonic=lambda: 100.0))
    client = _client(100.0 + remaining)
    message = {"kind": "request", "nonce": client.nonce}
    go_v1._write_message(client.process.stdout, {"kind": "ready", "nonce": client.nonce})
    client.process.stdout.seek(0)
    with recorder.node():
        if remaining <= 0:
            for action in (lambda: client._send(message), lambda: client._receive("ready")):
                with pytest.raises(go_v1.GoV1Error) as caught:
                    action()
                assert caught.value.code == "process_timeout"
                assert str(caught.value) == "go-v1 process_timeout: worker domain exceeded its wall-clock deadline"
            assert client.process.stdin.getvalue() == b""
            assert client.process.stdout.tell() == 0
        else:
            client._send(message)
            assert client._receive("ready")["nonce"] == client.nonce
            go_v1._write_message(client.process.stdout, {"kind": "ready", "nonce": "wrong"})
            client.process.stdout.seek(0)
            # The original valid frame is already consumed; test the planted mismatch separately.
            go_v1._read_message(client.process.stdout)
            with pytest.raises(go_v1.GoV1Error) as caught:
                client._receive("ready")
            assert caught.value.code == go_v1.CODE_WORKER_PROTOCOL_INVALID
    assert client.deadline == 100.0 + remaining
    rows = [row for row in recorder.events if "deadline" in row]
    assert rows and all(row["deadline"] == client.deadline for row in rows)
    assert all(row["remaining"] == remaining for row in rows)


# Default None is inert, and even a cancellation-raising callback cannot veto a valid send.
def test_default_disabled_and_failing_callback(monkeypatch):
    client = _client(time.monotonic() + 10)
    frame = {"kind": "request", "nonce": client.nonce}
    assert go_v1._OBSERVATION_SINK.get() is None
    class UnavailableState:
        @property
        def pid(self):
            pytest.fail("disabled observation inspected process state")
    go_v1._observe("client-deadline", process=UnavailableState(), deadline=123)
    client._send(frame)
    def fail(*args, **kwargs):
        raise KeyboardInterrupt("observer only")
    token = go_v1._OBSERVATION_SINK.set(fail)
    try:
        client._send(frame)
    finally:
        go_v1._OBSERVATION_SINK.reset(token)
    client.process.stdin.seek(0)
    assert go_v1._read_message(client.process.stdin) == frame
    assert go_v1._read_message(client.process.stdin) == frame


# Expiry has no callback dependency: an observer blocked on another thread cannot delay termination.
def test_expiry_terminates_while_observer_is_blocked():
    entered, release, terminated = threading.Event(), threading.Event(), threading.Event()
    client = _client(time.monotonic())
    client.finished = False
    client._lifecycle_lock = threading.Lock()
    client._expiration_error = None
    client.domain = SimpleNamespace(terminate=lambda process: terminated.set())
    def blocked():
        def sink(*args, **kwargs):
            entered.set()
            assert release.wait(2)
        token = go_v1._OBSERVATION_SINK.set(sink)
        try:
            go_v1._observe("ready-wait")
        finally:
            go_v1._OBSERVATION_SINK.reset(token)
    observer = threading.Thread(target=blocked)
    observer.start()
    try:
        assert entered.wait(2)
        # Same-thread callback would deadlock this applied negative control.
        calls = []
        token = go_v1._OBSERVATION_SINK.set(lambda *a, **k: calls.append(a[0]))
        try:
            client._expire_worker_domain()
        finally:
            go_v1._OBSERVATION_SINK.reset(token)
        assert terminated.is_set() and client._expired.is_set()
        assert calls == [], "expiry observer dependency"
        # Applied observer plant proves this checker detects that exact violation.
        go_v1._OBSERVATION_SINK.set(lambda *a, **k: calls.append(a[0]))
        try:
            go_v1._observe("teardown-start")
            with pytest.raises(AssertionError, match="expiry observer dependency"):
                assert calls == [], "expiry observer dependency"
        finally:
            go_v1._OBSERVATION_SINK.set(None)
        assert observer.is_alive()
    finally:
        release.set()
        observer.join(2)
    assert not observer.is_alive()


# A portable event adapter exercises the real refusal; only numeric index/flags escape.
def test_guard_event_correlation_preserves_refusal(recorder, tmp_path):
    guard = object.__new__(go_v1._IdentityMutationGuard)
    path = tmp_path / "secret-canary"
    guard.paths = (path,)
    guard._mac_fds = [73]
    guard._mac_paths = {73: path}
    guard._mac_queue = SimpleNamespace(control=lambda *args: [SimpleNamespace(ident=73, fflags=2)])
    with recorder.node():
        with pytest.raises(go_v1.GoV1Error) as caught:
            guard._verify_retained_identity()
    assert caught.value.code == go_v1.CODE_WORKER_IDENTITY_INVALID
    assert str(path) in str(caught.value)
    row = next(row for row in recorder.events if row["stage"] == "identity-event")
    assert row["path_index"] == 0 and row["event_flags"] == 2
    assert next(row for row in recorder.events if row["stage"] == "identity-events")["event_count"] == 1
    assert b"secret-canary" not in recorder.sink.read_bytes()
    guard._mac_queue.control = lambda *args: []
    guard._verify_retained_identity()  # nearby valid event-free guard


_AUDIT_CHILD = r'''
import sys
# Bootstrap dependencies must not write into the source checkout or interpreter.
sys.dont_write_bytecode = True
import os, json, hashlib, threading
from pathlib import Path
sys.path.insert(0, sys.argv[1])
from go_worker_observation import Recorder, CacheAudit
import go_worker_observation
helper=Path(go_worker_observation.__file__).resolve()
assert helper == Path(sys.argv[1]).resolve()/'go_worker_observation.py'
anchor, root, unrelated = map(Path, sys.argv[2:5])
with (anchor/'child.json').open('x+b') as sink:
    trace=Recorder(anchor, anchor/'child.json', sink, role='test-child')
    audit=CacheAudit(trace, root, {'sources/__pycache__': ('module',)})
    audit.install_in_owned_child(owner_pid=int(sys.argv[5]))
    sys.dont_write_bytecode = bool(sys.flags.dont_write_bytecode)
    sys.path.insert(0, str(root/'sources'))
    import module
    assert module.VALUE == 73
    cache=root/'sources'/'__pycache__'
    audit.post_state(cache)
    audit.action('mkdir', cache, lambda: cache.mkdir(exist_ok=True))
    target=cache/'module.cpython-312.pyc'
    audit.action('write', target, lambda: target.write_bytes(b'fixture'))
    audit.action('rename', target, lambda: target.rename(cache/'module.cpython-312.pyc.123'))
    audit.post_state(cache/'module.cpython-312.pyc.123')
    # Applied failing action: pre-op intent exists, completion must not.
    failures=[]
    def fail():
        try:
            os.mkdir(cache)  # actual audited operation rejects the existing path
        except FileExistsError as error:
            failures.append(error)
            raise
    try:
        audit.action('mkdir', cache, fail)
    except FileExistsError as error:
        assert error is failures[0]
    else:
        raise AssertionError('plant did not reject')
    before=sum(row['stage']=='cache-intent' and row.get('operation')=='write' for row in trace.events)
    start=threading.Barrier(2)
    def write(tag):
        start.wait()
        (cache/('module.cpython-312.pyc.'+tag)).write_bytes(b'fixture')
    threads=[threading.Thread(target=write,args=(tag,)) for tag in ('1001','1002')]
    for thread in threads: thread.start()
    for thread in threads:
        thread.join(2)
        assert not thread.is_alive()
    for tag in ('1001','1002'):
        assert (cache/('module.cpython-312.pyc.'+tag)).read_bytes()==b'fixture'
    after=sum(row['stage']=='cache-intent' and row.get('operation')=='write' for row in trace.events)
    assert after-before==2
    (unrelated/'secret-canary').write_bytes(b'outside')
    assert audit.relative(str(unrelated/'secret-canary')) is None
    assert trace.flush()
    print(json.dumps(dict(pid=os.getpid(),ppid=os.getppid(),no_site=sys.flags.no_site,
                         bytecode=sys.dont_write_bytecode,events=len(trace.events),
                         helper_path=str(helper),helper_sha256=hashlib.sha256(helper.read_bytes()).hexdigest(),
                         trace=json.loads(trace._encode()))))
'''


# Explicit -S bootstrap observes real cold import/write/rename intent; -B suppresses only import writes.
@pytest.mark.parametrize("no_bytecode", [False, True])
def test_owned_child_audit_bootstrap_and_intent(tmp_path, no_bytecode):
    root, anchor, unrelated = (tmp_path / name for name in ("package", "trace", "unrelated"))
    for path in (root, anchor, unrelated):
        path.mkdir(mode=0o700)
    (root / "sources").mkdir()
    (root / "sources" / "module.py").write_text("VALUE = 73\n")
    args = [sys.executable, "-S"] + (["-B"] if no_bytecode else [])
    args += ["-c", _AUDIT_CHILD, str(Path(__file__).parent), str(anchor), str(root), str(unrelated), str(os.getpid())]
    # The enclosing runner provides private HOME/TMPDIR and no credentials.
    env = {key: value for key, value in os.environ.items() if key not in {"PYTHONDONTWRITEBYTECODE", "PYTHONPATH"}}
    process = subprocess.Popen(args, env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    try:
        stdout, stderr = process.communicate(timeout=10)
    finally:
        if process.poll() is None:
            process.kill()
        process.wait()
    assert process.returncode == 0, stderr
    print("AUDIT_CHILD", stdout.strip(), "exit", process.returncode)
    raw = (anchor / "child.json").read_bytes()
    events = json.loads(raw)["events"]
    assert b"secret-canary" not in raw and str(tmp_path).encode() not in raw
    assert all(row["pid"] == process.pid and row["ppid"] == os.getpid() for row in events)
    first_post = next(index for index, row in enumerate(events) if row["stage"] == "cache-post")
    imports = [row for row in events[:first_post] if row["stage"] == "cache-intent"]
    assert bool(imports) is (not no_bytecode)
    assert events[first_post]["present"] is (not no_bytecode)
    assert {row["operation"] for row in events if row["stage"] == "cache-intent"} == {"mkdir", "write", "rename"}
    assert sum(row["stage"] == "cache-completed" for row in events) == 3
    assert sum(row["stage"] == "cache-intent" and row["operation"] == "mkdir" for row in events) >= 2
    assert any(row["stage"] == "cache-post" and row.get("ino", 0) > 0 for row in events)
    assert all(row.get("coverage") in {None, "python-audit-intent-only", "explicit-post-state", "action-returned"} for row in events)


# Shared-runner installation and linked cache referents are refused; normal allowlisted path works.
def test_audit_scope_and_link_negative_controls(recorder, tmp_path):
    root, outside = tmp_path / "package", tmp_path / "outside"
    root.mkdir(); outside.mkdir()
    (root / "sources").mkdir()
    audit = CacheAudit(recorder, root, {"sources/__pycache__": ("module",)})
    with pytest.raises(ValueError, match="owned child"):
        audit.install_in_owned_child(owner_pid=os.getpid())
    assert not audit.enabled
    cache = root / "sources" / "__pycache__"
    assert audit.relative(str(cache)) == "sources/__pycache__"
    cache.symlink_to(outside, target_is_directory=True)
    before = outside.stat()
    assert audit.relative(str(cache / "module.cpython-312.pyc")) is None
    audit.post_state(cache)
    assert outside.stat() == before
    assert recorder.events == []


# Optional action completion cannot replace the actual return or primary cancellation.
def test_action_observer_failure_preserves_body(recorder, tmp_path, monkeypatch):
    root = tmp_path / "package"
    root.mkdir()
    cache = root / "__pycache__"
    audit = CacheAudit(recorder, root, {"__pycache__": ("module",)})
    def fail_record(*args, **kwargs):
        raise OSError("unavailable recorder")
    monkeypatch.setattr(recorder, "record", fail_record)
    assert audit.action("mkdir", cache, lambda: cache.mkdir()) is None
    primary = KeyboardInterrupt("body cancellation")
    def fail_body():
        raise primary
    with pytest.raises(KeyboardInterrupt) as caught:
        audit.action("mkdir", cache, fail_body)
    assert caught.value is primary and cache.is_dir()


_TRANSPORT_CHILD = r'''
import sys, json, struct
while True:
    size=sys.stdin.buffer.read(4)
    if not size: break
    request=json.loads(sys.stdin.buffer.read(struct.unpack('>I',size)[0]))
    kind=request['kind']
    if kind=='shutdown': break
    if kind=='request': response={'kind':'ready','nonce':request['nonce']}
    else:
        response={'kind':'list-result' if kind=='list' else 'build-result',
                  'nonce':request['nonce'],
                  'result':dict(stdout='',stderr='',returncode=0,started=1 if kind=='list' else 2,
                                timed_out=False,overflow=False,start_failed=False,detail='')}
    payload=json.dumps(response).encode()
    sys.stdout.buffer.write(struct.pack('>I',len(payload))+payload);sys.stdout.buffer.flush()
'''


# Real tiny pipes/timer/client parsing/teardown work with observation; no native control claim.
def test_real_transport_client_lifecycle(recorder, tmp_path):
    process = subprocess.Popen([sys.executable, "-S", "-B", "-c", _TRANSPORT_CHILD],
                               stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    cache = tmp_path / "worker-cache"
    cache.mkdir()
    actions = []
    def terminate(child):
        if child.poll() is None:
            child.kill()
        child.wait()
        actions.append("terminated")
    domain = SimpleNamespace(terminate=terminate, close=lambda: actions.append("closed"))
    identity = SimpleNamespace(verify=lambda: None)
    plan = SimpleNamespace(executable=identity, process_identity=identity,
                           worker_cache=cache, build_argv=("build",),
                           limits=go_v1.ResourceLimits())
    guard = SimpleNamespace(verify=lambda: None, close=lambda: actions.append("guard-closed"))
    client = None
    lock_states = []
    original_observe = recorder.observe
    def inspect_observe(stage, **values):
        if stage in {"teardown-start", "teardown-end"}:
            lock_states.append(client._lifecycle_lock.locked())
        original_observe(stage, **values)
    recorder.observe = inspect_observe
    try:
        with recorder.node():
            deadline = time.monotonic() + 10
            client = go_v1._WorkerClient(plan, process, domain, guard, "nonce", "a" * 64,
                                         b"unused", None, deadline)
            client._send({"kind": "request", "nonce": "nonce"})
            assert client._receive("ready")["kind"] == "ready"
            assert client.list().started == 1
            assert client.build().started == 2
            client.teardown()
            go_v1._observe("before-cleanup")
            retained = json.loads(recorder.sink.read_bytes())["events"]
            assert retained[-1]["stage"] == "before-cleanup"
            assert actions == ["terminated", "closed", "guard-closed"]
            assert not client._deadline_timer.is_alive() or client._deadline_timer.finished.is_set()
            assert client.deadline == deadline
    finally:
        if client is not None:
            client._deadline_timer.cancel()
            client._deadline_timer.join(2)
        if process.poll() is None:
            process.kill()
        process.wait()
        for pipe in (process.stdin, process.stdout, process.stderr):
            if pipe is not None:
                pipe.close()
    print("TRANSPORT_CHILD", process.pid, "exit", process.returncode)
    stages = [row["stage"] for row in recorder.events]
    for stage in ("client-deadline", "request-send", "ready-wait", "list-send", "list-result", "build-permit", "build-result", "teardown-end"):
        assert stage in stages
    timed = [row for row in recorder.events if "deadline" in row]
    assert all(row["deadline"] == deadline and row["worker_pid"] == process.pid for row in timed)
    assert all(first["remaining"] >= second["remaining"] for first, second in zip(timed, timed[1:]))
    assert next(row for row in recorder.events if row["stage"] == "teardown-end")["exit_code"] is not None
    assert not client._stderr_thread.is_alive()
    assert lock_states == [False, False]
    print("TRANSPORT_TRACE", recorder._encode().decode())


# Before-launch persistence distinguishes a launch stall; domain errors retain identity and cleanup.
def test_domain_launch_before_deadline_and_failure_identity(recorder, tmp_path, monkeypatch):
    primary = RuntimeError("secret-canary")
    closed = []
    identity = SimpleNamespace(verify=lambda: None, watch_paths=lambda: ())
    plan = SimpleNamespace(executable=identity, process_identity=identity,
                           platform=go_v1.PLATFORM_MACOS, worker_cache=tmp_path,
                           probes=(), limits=go_v1.ResourceLimits())
    class Guard:
        def __init__(self, *args): pass
        def verify(self): pass
        def close(self): closed.append("guard")
    class Domain:
        def __init__(self, *args): pass
        def launch(self, *args):
            rows = json.loads(recorder.sink.read_bytes())["events"]
            assert rows[-1]["stage"] == "domain-launch"
            assert not any("deadline" in row for row in rows)
            raise primary
        def terminate(self, process):
            assert process is None
            closed.append("domain")
        def close(self): closed.append("closed")
    monkeypatch.setattr(go_v1, "_IdentityMutationGuard", Guard)
    monkeypatch.setattr(go_v1, "_NativeControlDomain", Domain)
    with recorder.node():
        with pytest.raises(RuntimeError) as caught:
            go_v1._WorkerClient.launch(plan)
    assert caught.value is primary
    assert closed == ["domain", "closed", "guard"]
    assert [row["stage"] for row in recorder.events if row["stage"].startswith("launch")] == ["launch-start", "launch-failure"]
    assert b"secret-canary" not in recorder.sink.read_bytes()


# The actual build finally flushes before removing its private stage/cache, preserving a launch error.
@pytest.mark.parametrize("observation", ["normal", "sink-failure", "recorder-failure"])
def test_build_failure_flushes_before_owned_cleanup(recorder, tmp_path, monkeypatch, observation):
    operation = tmp_path / "operation"
    operation.mkdir()
    primary = RuntimeError("secret-canary")
    source = tmp_path / "source"
    source.mkdir()
    identity = SimpleNamespace(verify=lambda: None)
    session = SimpleNamespace(operation_root=operation, environment={}, goroot=tmp_path,
                              executable=tmp_path / "go", target=SimpleNamespace(goos="darwin", goarch="arm64"))
    request = SimpleNamespace(toolchain_session=session, source_snapshot=None, build_root="build",
                              source_dir="build/cmd", command="fixture", limits=go_v1.ResourceLimits())
    # Preflight adapters only: this test exercises real staging/finally, never a native domain.
    for name in ("_validate_package_command_surface", "_verify_frozen_inputs", "_validate_declared_module_roots"):
        monkeypatch.setattr(go_v1, name, lambda *args: None)
    monkeypatch.setattr(go_v1, "_canonical_build_directories", lambda *args: (source, source, source))
    monkeypatch.setattr(go_v1, "probe_native_controls", lambda *a, **k: (go_v1.PLATFORM_MACOS, ()))
    monkeypatch.setattr(go_v1, "_resolve_manager_identity", lambda *a: identity)
    monkeypatch.setattr(go_v1, "_resolve_tool_process_identity", lambda *a: identity)
    monkeypatch.setattr(go_v1, "_private_roots", lambda *a: ())
    def fail_launch(plan):
        assert plan.worker_cache.is_dir()
        raise primary
    monkeypatch.setattr(go_v1._WorkerClient, "launch", fail_launch)
    if observation == "sink-failure":
        recorder.descriptor = -1
    elif observation == "recorder-failure":
        def fail_record(*args, **kwargs):
            raise OSError("recorder unavailable")
        monkeypatch.setattr(recorder, "record", fail_record)
    original_observe = recorder.observe
    found = []
    def inspect_observe(stage, **values):
        if stage == "before-cleanup":
            found.extend(path.name for path in operation.iterdir())
        return original_observe(stage, **values)
    monkeypatch.setattr(recorder, "observe", inspect_observe)
    with recorder.node():
        with pytest.raises(RuntimeError) as caught:
            go_v1.build(request, _manager_executable=tmp_path / "manager")
    assert caught.value is primary
    assert len(found) == 2
    assert any(name.startswith(".csk-go-build-") for name in found)
    assert any(name.startswith(".csk-go-worker-pycache-") for name in found)
    assert list(operation.iterdir()) == []
    if observation == "normal":
        assert any(row["stage"] == "before-cleanup" for row in json.loads(recorder.sink.read_bytes())["events"])
    elif observation == "sink-failure":
        assert recorder.sink_failures > 0
    else:
        assert recorder.events == []


# A scoped real CPython callback at the unchanged encoding boundary cannot exceed
# the cap; normal rows and the existing two-thread control remain independent.
@pytest.mark.parametrize("entry", ["record", "snapshot", "flush"])
def test_real_callback_reentry_bound(recorder, entry):
    from go_worker_observation import Recorder as RecorderType
    for _ in range(127):
        recorder.record("node-start")
    previous = sys.getprofile()
    applied = []
    target = RecorderType._encode.__code__ if entry != "snapshot" else json.dumps.__code__
    def callback(frame, event, arg):
        if previous:
            previous(frame, event, arg)
        if not applied and event == "call" and frame.f_code is target:
            applied.append(True)
            recorder.record("node-end")
    sys.setprofile(callback)
    try:
        if entry == "record":
            recorder.record("failure")
        elif entry == "snapshot":
            payload = recorder._encode()
            assert len(payload) <= recorder.MAX_BYTES
        else:
            assert recorder.flush()
    finally:
        sys.setprofile(previous)
    assert sys.getprofile() is previous and applied == [True]
    assert recorder.flush()
    data = json.loads(recorder.sink.read_bytes())
    print("REENTRY", entry, "rows", len(data["events"]), "dropped", data["dropped"], "applied", len(applied))
    assert recorder.dropped == 1
    assert len(data["events"]) == (128 if entry == "record" else 127)
    assert data["dropped"] == 1 and len(recorder.sink.read_bytes()) <= recorder.MAX_BYTES


# Snapshot/flush callbacks have an explicit refusal, not deadlock or a successful
# overlapping write; one subsequent ordinary snapshot/flush remains valid.
@pytest.mark.parametrize("nested", ["snapshot", "flush"])
def test_reentrant_snapshot_and_flush_refuse(recorder, nested):
    recorder.record("node-start")
    previous = sys.getprofile()
    applied, refusals = [], []
    def callback(frame, event, arg):
        if previous:
            previous(frame, event, arg)
        if not applied and event == "call" and frame.f_code is json.dumps.__code__:
            applied.append(True)
            if nested == "snapshot":
                try:
                    recorder._encode()
                except ValueError:
                    refusals.append(True)
            else:
                refusals.append(recorder.flush() is False)
    sys.setprofile(callback)
    try:
        assert recorder.flush()
    finally:
        sys.setprofile(previous)
    assert sys.getprofile() is previous and applied == [True] and refusals == [True]
    assert recorder.flush()
    data = json.loads(recorder.sink.read_bytes())
    assert len(data["events"]) == 1
    assert data["dropped" if nested == "snapshot" else "sink_failures"] == 1
    assert len(recorder._encode()) <= recorder.MAX_BYTES


# Optional post-state failures preserve absent/present controls and exact namespace,
# even when fallback accounting also fails; no audit hook enters pytest.
@pytest.mark.parametrize("accounting_fault", [False, True])
def test_post_state_optional_faults(recorder, tmp_path, monkeypatch, accounting_fault):
    root = tmp_path / "package"; root.mkdir()
    audit = CacheAudit(recorder, root, {"__pycache__": ("module",)})
    cache = root / "__pycache__"
    audit.post_state(cache)
    assert recorder.events[-1]["present"] is False
    cache.mkdir(); audit.post_state(cache)
    assert recorder.events[-1]["present"] is True
    before = cache.stat()
    error, accounting = OSError("record fault"), RuntimeError("accounting fault")
    def fail(*args, **kwargs): raise error
    def fail_drop(): raise accounting
    monkeypatch.setattr(recorder, "record", fail)
    if accounting_fault: monkeypatch.setattr(recorder, "drop", fail_drop)
    audit.post_state(cache)
    after = cache.stat()
    assert (before.st_dev, before.st_ino, before.st_mode) == (after.st_dev, after.st_ino, after.st_mode)
    if not accounting_fault: assert recorder.dropped == 1


# Both optional faults leave the real callable's return, exact error/cause/context,
# cancellation and finally cleanup intact. Callable execution is never suppressed.
@pytest.mark.parametrize("accounting_fault", [False, True])
def test_action_optional_faults(recorder, tmp_path, monkeypatch, accounting_fault):
    root = tmp_path / "package"; root.mkdir()
    audit = CacheAudit(recorder, root, {"__pycache__": ("module",)})
    cache = root / "__pycache__"
    assert audit.action("mkdir", cache, cache.mkdir) is None
    sentinel, effects = object(), []
    error, accounting = OSError("record fault"), RuntimeError("accounting fault")
    def fail(*args, **kwargs): raise error
    def fail_drop(): raise accounting
    monkeypatch.setattr(recorder, "record", fail)
    if accounting_fault: monkeypatch.setattr(recorder, "drop", fail_drop)
    def success():
        effects.append("returned")
        return sentinel
    assert audit.action("write", cache, success) is sentinel
    assert effects == ["returned"]
    for primary in (RuntimeError("body"), KeyboardInterrupt("cancel")):
        cause, context = LookupError("cause"), ValueError("context")
        primary.__cause__, primary.__context__ = cause, context
        cleaned = []
        def body():
            try: raise primary
            finally: cleaned.append(True)
        with pytest.raises(type(primary)) as caught: audit.action("write", cache, body)
        assert caught.value is primary and primary.__cause__ is cause and primary.__context__ is context
        assert cleaned == [True]


AUDIT_FAULT_CHILD = r'''
import sys
sys.dont_write_bytecode=True
import os,json
from pathlib import Path
sys.path.insert(0,sys.argv[1])
from go_worker_observation import Recorder,CacheAudit
base=Path(sys.argv[2]);root=base/'package';root.mkdir()
for name in ('normal','single','double'): (root/name).mkdir()
anchor=base/'trace';anchor.mkdir(mode=0o700)
with (anchor/'data.json').open('x+b') as stream:
 recorder=Recorder(anchor,anchor/'data.json',stream,role='test-child')
 audit=CacheAudit(recorder,root,{name+'/__pycache__':('module',) for name in ('normal','single','double')})
 audit.install_in_owned_child(owner_pid=int(sys.argv[3]))
 normal=root/'normal'/'__pycache__';os.mkdir(normal)
 assert recorder.events[-1]['stage']=='cache-intent'
 recording=OSError('diagnostic record failure');accounting=RuntimeError('diagnostic accounting failure')
 def bad_record(*args,**kwargs):raise recording
 def bad_drop():raise accounting
 recorder.record=bad_record
 single=root/'single'/'__pycache__';os.mkdir(single)
 assert single.is_dir() and recorder.dropped==1
 recorder.drop=bad_drop
 double=root/'double'/'__pycache__';caught=None
 try:os.mkdir(double)
 except BaseException as error:caught=error
 sentinel=object();returned=None
 try:returned=audit.action('write',double,lambda:sentinel)
 except BaseException as error:
  if caught is None:caught=error
 # Coverage emission itself is optional after the ownership checks. This second
 # child-only hook is installed last; all observed IO already completed above.
 additional=CacheAudit(recorder,root,{'double/__pycache__':('module',)})
 coverage_error=None
 try:additional.install_in_owned_child(owner_pid=int(sys.argv[3]))
 except BaseException as error:coverage_error=error
 print(json.dumps(dict(pid=os.getpid(),ppid=os.getppid(),no_site=bool(sys.flags.no_site),no_bytecode=bool(sys.dont_write_bytecode),
    normal=normal.is_dir(),single=single.is_dir(),double=double.is_dir(),
    preserved_return=returned is sentinel,escaped=caught is not None,coverage_escaped=coverage_error is not None)))
'''


# A non-removable hook is confined to one owned -S -B child: actual mkdir state
# and action return survive record+accounting faults, with healthy/single controls.
def test_owned_child_optional_accounting_faults(tmp_path):
    import go_worker_observation as obs
    command = [sys.executable, "-S", "-B", "-c", AUDIT_FAULT_CHILD,
               str(Path(obs.__file__).parent), str(tmp_path), str(os.getpid())]
    child = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    try:
        out, err = child.communicate(timeout=10)
    finally:
        if child.poll() is None: child.kill()
        child.wait()
    assert child.returncode == 0, err
    data = json.loads(out)
    print("AUDIT_FAULT_CHILD", json.dumps(data), "exit", child.returncode)
    assert data['pid'] == child.pid and data['ppid'] == os.getpid()
    assert data['no_site'] and data['no_bytecode'] and data['normal'] and data['single']
    assert data['double'] and data['preserved_return'] and not data['escaped'] and not data['coverage_escaped']


# Default-disabled actual finished teardown must not inspect observation-only state;
# enabled teardown remains a nearby control with the correct cached expiry flag.
def test_disabled_teardown_expiry_inspection(monkeypatch):
    calls = []
    class Flag(threading.Event):
        def is_set(self):
            calls.append(True)
            return super().is_set()
    client = object.__new__(go_v1._WorkerClient)
    client._expired = Flag(); client.finished = True
    client._lifecycle_lock = threading.Lock()
    client.process = SimpleNamespace(pid=73, returncode=0); client.deadline = 10.0
    sink = ContextVar("disabled-teardown", default=None)
    monkeypatch.setattr(go_v1, "_OBSERVATION_SINK", sink)
    assert client.teardown() is None and calls == []
    rows = []; token = sink.set(lambda stage, **values: rows.append((stage, values)))
    try: assert client.teardown() is None
    finally: sink.reset(token)
    assert calls == [True] and [row[0] for row in rows] == ["teardown-start", "teardown-end"]
    assert rows[-1][1]["expired"] is False


# Append-mode fd is unsupported: preserve prior valid JSON and outside inode/mode;
# the documented x+b private descriptor still flushes normally afterward.
def test_append_sink_refuses_before_write(recorder, tmp_path):
    recorder.record("node-start"); assert recorder.flush()
    before = recorder.sink.read_bytes()
    canary = tmp_path / "outside"; canary.write_bytes(b"outside-canary"); canary.chmod(0o444)
    identity = canary.stat(); original = recorder.descriptor
    append = os.open(recorder.sink, os.O_RDWR | os.O_APPEND)
    try:
        recorder.descriptor = append; recorder.record("node-end")
        assert recorder.flush() is False
        assert recorder.sink.read_bytes() == before
        assert canary.read_bytes() == b"outside-canary"
        after = canary.stat()
        assert (after.st_dev, after.st_ino, after.st_mode) == (identity.st_dev, identity.st_ino, identity.st_mode)
    finally:
        recorder.descriptor = original; os.close(append)
    assert recorder.flush() and json.loads(recorder.sink.read_bytes())["events"][-1]["stage"] == "node-end"


# Direct append-stream construction also refuses before touching prior safe bytes;
# the fixture's original x+b stream is the portable admitted positive control.
def test_append_stream_admission(recorder):
    recorder.record("node-start"); assert recorder.flush()
    before = recorder.sink.read_bytes()
    with recorder.sink.open("a+b") as stream:
        unsupported = Recorder(recorder.anchor, recorder.sink, stream)
        unsupported.record("node-end")
        assert not unsupported.flush() and unsupported.sink_failures == 1
    assert recorder.sink.read_bytes() == before
    recorder.record("node-end"); assert recorder.flush()



# The same unsafe-sink assertion detects a deliberately bypassed identity gate.
# Writes stay on the owned original fd; candidate/outside bytes stay untouched.
def test_sink_identity_control_is_sensitive(recorder, tmp_path, monkeypatch):
    original = recorder.sink
    replacement = recorder.anchor / "different.json"
    replacement.write_bytes(b"replacement canary")
    before = replacement.stat()
    recorder.sink = replacement
    recorder.record("node-start")
    assert not recorder.flush()
    with monkeypatch.context() as patch:
        applied = []
        def bypass(): applied.append(True)
        patch.setattr(recorder, "_check_sink", bypass)
        with pytest.raises(AssertionError, match="unsafe sink admitted"):
            assert not recorder.flush(), "unsafe sink admitted"
        assert applied == [True]
    assert replacement.read_bytes() == b"replacement canary"
    after = replacement.stat()
    assert (before.st_mode, before.st_mtime_ns, before.st_ino) == (after.st_mode, after.st_mtime_ns, after.st_ino)
    recorder.sink = original
    assert recorder.flush() and json.loads(original.read_bytes())["events"][-1]["stage"] == "node-start"
