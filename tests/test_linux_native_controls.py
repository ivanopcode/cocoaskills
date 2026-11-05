"""Internal native controls; this file does not qualify Linux build admission."""
import errno
import os
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from csk import installer
from csk.builds import go_v1 as g


def test_linux_admission_stays_closed(monkeypatch):
    # A working internal adapter must not admit Linux through either public predicate.
    monkeypatch.setattr(sys, "platform", "linux")
    # Simulate the POSIX branch without mutating the host's global os.name.
    monkeypatch.setattr(g, "os", SimpleNamespace(**(vars(os) | {"name": "posix"})))
    assert not installer.supports_external_builds()
    with pytest.raises(g.GoV1Error) as failure:
        g.inventory_platform()
    assert failure.value.code == g.CODE_CONTROL_UNAVAILABLE
    monkeypatch.setattr(sys, "platform", "darwin")
    assert installer.supports_external_builds()
    assert g.inventory_platform() == g.PLATFORM_MACOS


@pytest.mark.skipif(sys.platform != "linux", reason="real Linux controls")
def test_native_linux_launch_observation_and_join(tmp_path, monkeypatch):
    # Real child inherits session, file limit and authenticated FD; no public identity claim.
    platform, probes = g.probe_native_controls(g.ResourceLimits(), _platform="linux")
    domain = g._NativeControlDomain(platform, probes, g.ResourceLimits())
    previous = __import__("resource").getrlimit(__import__("resource").RLIMIT_FSIZE)
    script = '''import os, resource
from csk.builds import go_v1 as g
context = g._consume_worker_launch_context()
assert context.parent_pid == os.getppid()
_, probes = g.probe_native_controls(g.ResourceLimits(), _platform="linux")
assert len(g._observe_native_controls("linux", probes, g.ResourceLimits(), ())) == 3
try:
    g._consume_worker_launch_context()
except g.GoV1Error as exc:
    assert exc.code == g.CODE_WORKER_IDENTITY_INVALID
else:
    raise AssertionError("launch capability replay accepted")
print("observed", flush=True)
import time
time.sleep(30)
'''
    # Identity and argv are explicit test seams; ELF qualification is separate.
    identity = SimpleNamespace(
        path=Path(sys.executable),
        startup=SimpleNamespace(site_root=Path(g.__file__).resolve().parents[2],
                                python_home=Path(sys.base_prefix)),
    )
    monkeypatch.setattr(g, "worker_argv", lambda _: (sys.executable, "-B", "-c", script))
    cache = tmp_path / "worker-cache"
    cache.mkdir()
    process = None
    try:
        process = domain.launch(identity, cache)
        assert process.stdout.readline() == b"observed\n"
        assert len(domain.installed_controls()) == 3
        domain.terminate(process)
        assert domain.terminated
        domain.terminate(process)
        with pytest.raises(ProcessLookupError):
            os.killpg(process.pid, 0)
    finally:
        if process is not None:
            if process.poll() is None:
                process.kill()
            process.wait()
            for stream in (process.stdin, process.stdout, process.stderr):
                stream.close()
        domain.close()
    assert __import__("resource").getrlimit(__import__("resource").RLIMIT_FSIZE) == previous


@pytest.mark.skipif(os.name != "posix", reason="POSIX descriptor capability")
def test_fixed_descriptor_collisions_are_not_overwritten(monkeypatch):
    # Occupied candidate refuses with the exact diagnostic; free candidate succeeds.
    read_fd, write_fd = os.pipe()
    try:
        original = os.fstat(read_fd)
        monkeypatch.setattr(g, "_WORKER_LAUNCH_FDS", (read_fd,))
        with pytest.raises(g.GoV1Error, match="no fixed worker launch descriptor") as failure:
            g._PreparedWorkerLaunch("linux")
        assert failure.value.code == g.CODE_CONTROL_UNAVAILABLE
        assert os.fstat(read_fd) == original
        import fcntl
        # Reserve an actually available high slot, then release it for the allocator.
        candidate = fcntl.fcntl(read_fd, fcntl.F_DUPFD_CLOEXEC, read_fd + 100)
        os.close(candidate)
        monkeypatch.setattr(g, "_WORKER_LAUNCH_FDS", (candidate,))
        capability = g._PreparedWorkerLaunch("linux")
        try:
            # The parent copy stays close-on-exec; only pass_fds admits the child.
            assert not os.get_inheritable(capability._read_fd)
        finally:
            capability.close_parent_copy()
    finally:
        os.close(read_fd)
        os.close(write_fd)


@pytest.mark.skipif(os.name != "posix", reason="POSIX limit installation")
def test_failed_launch_restores_file_limit(monkeypatch):
    # A Popen failure restores the manager soft and hard limits before propagating.
    import resource
    previous = resource.getrlimit(resource.RLIMIT_FSIZE)
    domain = object.__new__(g._NativeControlDomain)
    domain.file_limit = 1024
    def fail(*args, **kwargs):
        raise OSError(errno.ENOENT, "planted launch failure")
    monkeypatch.setattr(g.subprocess, "Popen", fail)
    with pytest.raises(OSError, match="planted launch failure"):
        domain._launch_macos(("missing",), {})
    assert resource.getrlimit(resource.RLIMIT_FSIZE) == previous


@pytest.mark.skipif(sys.platform != "linux", reason="Linux reuse guard")
def test_reaped_worker_never_receives_group_signal(monkeypatch):
    # A potentially reused group is observed only; rejection cannot signal its new owner.
    domain = object.__new__(g._NativeControlDomain)
    process = type("Process", (), {"pid": 12345, "returncode": 0, "wait": lambda *a, **k: 0})()
    signals = []
    monkeypatch.setattr(os, "killpg", lambda pid, sig: signals.append(sig))
    with pytest.raises(g.GoV1Error, match="reaped before domain teardown") as failure:
        domain._terminate_linux(process)
    assert failure.value.code == g.CODE_CONTROL_UNAVAILABLE
    assert signals == [0]
    def absent(pid, sig):
        assert sig == 0
        raise ProcessLookupError()
    monkeypatch.setattr(os, "killpg", absent)
    domain._terminate_linux(process)


@pytest.mark.skipif(sys.platform not in {"darwin", "linux"}, reason="native POSIX crash fixture")
@pytest.mark.parametrize("mode,member_survives", [("baseline", True), ("eof", False)])
def test_manager_sigkill_crash_boundary(tmp_path, mode, member_survives):
    # SIGKILL during a synthetic build kills the group only with the watchdog;
    # setsid escape remains alive. Linux fixture adopts and reaps every descendant.
    import json
    fixture = Path(__file__).with_name("native_control_crash_fixture.py")
    process = subprocess.Popen([sys.executable, str(fixture), str(tmp_path / mode), mode],
                               stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    try:
        output, error = process.communicate(timeout=150)
    except subprocess.TimeoutExpired:
        # The fixture owns its descendants: TERM runs its finally/reap path;
        # killing the fixture outright would abandon its cleanup responsibility.
        process.terminate()
        output, error = process.communicate()
        pytest.fail(f"crash fixture exceeded its budget after cleanup: {error}")
    assert process.returncode == 0, error
    record = json.loads(output)
    # Retain phase timing and liveness observations in pytest's captured evidence.
    print(json.dumps(record, sort_keys=True))
    assert record["before_kill"] == {"worker": True, "member": True, "detached": True}
    assert record["manager_returncode"] == -9
    assert record["member_live"] is member_survives
    assert record["worker_live"] is member_survives
    assert record["detached_live"] is True


@pytest.mark.skipif(os.name != "posix", reason="native POSIX atomic publication")
def test_sigkill_before_publication_never_exposes_partial_artifact(tmp_path):
    # Kill the real publisher at its final rename boundary; a later valid publish
    # must succeed. This covers process death, not power-loss durability.
    import signal
    import select
    from test_build_cache_posix import _new_store, _build_input, _publication, _entry_path, _HeldGuard
    from csk.builds import cache_posix
    from csk.builds.cache import CacheExpectation, CacheEntryStatus, CachePublicationStatus
    home, store = _new_store(tmp_path)
    build_input = _build_input()
    publication, _ = _publication(tmp_path, build_input, b"complete-artifact")
    read_fd, write_fd = os.pipe()
    child = os.fork()
    if child == 0:
        os.close(read_fd)
        def pause(*args, **kwargs):
            os.write(write_fd, b"ready")
            signal.pause()
            raise AssertionError("publisher unexpectedly resumed")
        cache_posix._rename_noreplace = pause
        try:
            store.publish(publication, guard=_HeldGuard())
        finally:
            os._exit(5)
    os.close(write_fd)
    try:
        assert select.select([read_fd], [], [], 10)[0], "publisher never reached rename"
        assert os.read(read_fd, 5) == b"ready"
    finally:
        # This exact, unreaped child pins its PID. If it already exited, reap
        # without signaling; never signal a number after ownership was released.
        joined, status = os.waitpid(child, os.WNOHANG)
        if joined == 0:
            os.kill(child, signal.SIGKILL)
            _, status = os.waitpid(child, 0)
        os.close(read_fd)
    assert os.WTERMSIG(status) == signal.SIGKILL
    assert not _entry_path(home, build_input).exists()
    assert store.inspect(CacheExpectation(input=build_input)).status is CacheEntryStatus.MISS
    assert store.publish(publication, guard=_HeldGuard()).status is CachePublicationStatus.PUBLISHED
    assert store.inspect(CacheExpectation(input=build_input)).status is CacheEntryStatus.HIT
    # Restore only this fixture's sealed directories for test-run cleanup.
    for root, dirs, files in os.walk(tmp_path):
        Path(root).chmod(0o700)
        for name in files:
            (Path(root) / name).chmod(0o600)


@pytest.mark.skipif(sys.platform != "linux", reason="child-only Linux descriptor limits")
def test_descriptor_capacity_refuses_low_hard_limit_and_preserves_limit():
    # Only the fixture child loses capacity; the valid control proves this is
    # capacity rejection rather than an adapter that refuses every launch.
    script = '''import resource
from csk.builds import go_v1 as g
previous = resource.getrlimit(resource.RLIMIT_NOFILE)
c = g._PreparedWorkerLaunch("linux")
second = g._PreparedWorkerLaunch("linux")
assert second._read_fd != c._read_fd
second.close_parent_copy()
c.close_parent_copy()
assert resource.getrlimit(resource.RLIMIT_NOFILE) == previous
resource.setrlimit(resource.RLIMIT_NOFILE, (64,64))
try:
    g._PreparedWorkerLaunch("linux")
except g.GoV1Error as exc:
    assert exc.code == g.CODE_CONTROL_UNAVAILABLE
    assert "descriptor capacity is unavailable" in exc.detail
else:
    raise AssertionError("low descriptor hard limit was accepted")
assert resource.getrlimit(resource.RLIMIT_NOFILE) == (64,64)
'''
    script = "import sys; sys.path.insert(0, " + repr(str(Path(g.__file__).resolve().parents[2])) + ")\n" + script
    result = subprocess.run([sys.executable, "-c", script], capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stderr


@pytest.mark.skipif(os.name != "posix", reason="POSIX restoration control plant")
def test_file_limit_installation_readback_catches_noop(monkeypatch):
    # A successful-but-noop setrlimit plant cannot authorize a worker start.
    import resource
    previous = resource.getrlimit(resource.RLIMIT_FSIZE)
    domain = object.__new__(g._NativeControlDomain)
    domain.file_limit = 1024 if previous[0] != 1024 else 2048
    monkeypatch.setattr(resource, "setrlimit", lambda *args: None)
    def forbidden(*args, **kwargs):
        pytest.fail("worker started after a failed control readback")
    monkeypatch.setattr(g.subprocess, "Popen", forbidden)
    with pytest.raises(OSError, match="installation did not persist"):
        domain._launch_macos(("unused",), {})


@pytest.mark.skipif(sys.platform != "linux", reason="Linux zombie and waitid semantics")
def test_linux_zombie_leader_is_reaped_before_success():
    # WNOWAIT retains the exited child's PID until teardown joins it; no orphan fixture.
    process = subprocess.Popen([sys.executable, "-c", "pass"], start_new_session=True)
    try:
        os.waitid(os.P_PID, process.pid, os.WEXITED | os.WNOWAIT)
        domain = object.__new__(g._NativeControlDomain)
        domain._terminate_linux(process)
        assert process.returncode == 0
        with pytest.raises(ChildProcessError):
            os.waitpid(process.pid, os.WNOHANG)
        with pytest.raises(ProcessLookupError):
            os.killpg(process.pid, 0)
    finally:
        if process.poll() is None:
            process.kill()
        process.wait()


@pytest.mark.skipif(sys.platform != "linux", reason="Linux teardown error precedence")
def test_linux_teardown_preserves_signal_failure_over_join_failure(monkeypatch):
    # An EPERM plant remains the diagnostic even when joining independently fails.
    domain = object.__new__(g._NativeControlDomain)
    def failed_wait(*args, **kwargs):
        raise subprocess.TimeoutExpired("fixture", 1)
    killed = []
    process = type("Process", (), {"pid": 12345, "returncode": None, "wait": failed_wait,
                                    "kill": lambda self: killed.append(self.pid)})()
    monkeypatch.setattr(os, "waitid", lambda *args: None)
    monkeypatch.setattr(os, "getpgid", lambda pid: pid)
    monkeypatch.setattr(os, "getsid", lambda pid: pid)
    def denied(pid, sig):
        raise PermissionError(errno.EPERM, "planted group denial")
    monkeypatch.setattr(os, "killpg", denied)
    with pytest.raises(g.GoV1Error, match="planted group denial") as failure:
        domain._terminate_linux(process)
    assert failure.value.code == g.CODE_CONTROL_UNAVAILABLE
    assert isinstance(failure.value.__cause__, PermissionError)
    assert killed == [12345]


@pytest.mark.skipif(sys.platform != "linux", reason="Linux direct-child rollback")
def test_linux_group_signal_failure_still_reaps_owned_worker(monkeypatch):
    # Denial of group signaling rejects, but does not abandon the direct worker.
    process = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"], start_new_session=True)
    domain = object.__new__(g._NativeControlDomain)
    def denied(pid, sig):
        raise PermissionError(errno.EPERM, "planted group denial")
    monkeypatch.setattr(os, "killpg", denied)
    try:
        with pytest.raises(g.GoV1Error, match="planted group denial") as failure:
            domain._terminate_linux(process)
        assert failure.value.code == g.CODE_CONTROL_UNAVAILABLE
        assert process.returncode == -9
        with pytest.raises(ChildProcessError):
            os.waitpid(process.pid, os.WNOHANG)
    finally:
        if process.poll() is None:
            process.kill()
        process.wait()


@pytest.mark.skipif(sys.platform not in {"darwin", "linux"}, reason="native POSIX pipe watchdog")
@pytest.mark.parametrize("case", ["queued-open", "busy-closed", "blocked-closed"])
def test_orderly_watchdog_shutdown_reads_frame_and_exits_zero(case):
    # Real session leaders retain the frame and exit 0 for open/closed queued
    # input and a blocked reader; bounded artificial reader work is not a stall guarantee.
    script = r"""
import os, sys, time
sys.path.insert(0, sys.argv[1])
from csk.builds import go_v1 as g
watch = g._PosixManagerLossWatch(sys.stdin.buffer, os.getppid())
os.write(1, b'W')
if sys.argv[2] != 'blocked-closed':
    deadline = time.monotonic() + (1.0 if sys.argv[2] == "busy-closed" else .3)
    while time.monotonic() < deadline:
        sum(range(100))
message = g._read_message(sys.stdin.buffer)
watch.close()
assert message == {'kind': 'shutdown', 'nonce': 'test'}
os.write(1, b'frame-read')
"""
    for _ in range(3):
        process = subprocess.Popen([sys.executable, "-B", "-c", script,
                                    str(Path(g.__file__).resolve().parents[2]), case],
                                   stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                   stderr=subprocess.PIPE, start_new_session=True)
        try:
            assert process.stdout.read(1) == b"W"
            g._write_message(process.stdin, {"kind": "shutdown", "nonce": "test"})
            if case != "queued-open":
                process.stdin.close()
            assert process.wait(timeout=30) == 0, process.stderr.read()
            assert process.stdout.read() == b"frame-read"
        finally:
            if process.poll() is None:
                process.kill()
            process.wait()
            for stream in (process.stdin, process.stdout, process.stderr):
                stream.close()


def test_internal_linux_records_do_not_alias_macos(monkeypatch):
    # Same mechanisms today, but changing the macOS row cannot rewrite Linux's row.
    linux = g._control_records("linux")
    assert linux == g._control_records(g.PLATFORM_MACOS)
    assert linux is not g._control_records(g.PLATFORM_MACOS)
    monkeypatch.setattr(g, "_NATIVE_CONTROL_PLATFORMS", {g.PLATFORM_MACOS: {}})
    assert g._control_records("linux") == linux
    assert g._control_records(g.PLATFORM_MACOS) == {}


@pytest.mark.skipif(sys.platform != "linux", reason="native capability probe")
def test_linux_capability_probe_detects_inheritable_allocation(monkeypatch):
    # The probe observes the allocator's output; it cannot repair a planted violation.
    control = g.CONTROL_INHERITED_HANDLE_RESTRICTION
    assert g._probe_linux_control(control, g.ResourceLimits())
    prepare = g._PreparedWorkerLaunch._prepare_linux
    def inheritable(self, record):
        prepare(self, record)
        os.set_inheritable(self._read_fd, True)
    monkeypatch.setattr(g._PreparedWorkerLaunch, "_prepare_linux", inheritable)
    assert not g._probe_linux_control(control, g.ResourceLimits())


@pytest.mark.parametrize("stage", ["capability", "runtime", "watch"])
def test_worker_startup_refusal_is_framed(monkeypatch, stage):
    # Startup rejections use the failure frame and status 3, before any session work.
    import io
    output = io.BytesIO()
    context = g._WorkerLaunchContext(parent_pid=os.getpid(), secret=b"x" * g._WORKER_LAUNCH_SECRET_BYTES)
    def refuse(*args, **kwargs):
        raise g.GoV1Error(g.CODE_CONTROL_UNAVAILABLE, "planted startup refusal")
    monkeypatch.setattr(g, "_consume_worker_launch_context", refuse if stage == "capability" else lambda: context)
    monkeypatch.setattr(g, "worker_runtime_proof", refuse if stage == "runtime" else lambda _: {})
    monkeypatch.setattr(g, "_PosixManagerLossWatch", refuse)
    monkeypatch.setattr(sys, "platform", "linux")
    status = g.run_worker(io.BytesIO(), output, worker_executable=Path(sys.executable))
    assert status == 3
    output.seek(0)
    assert g._read_message(output) == {"kind": "failure", "nonce": "", "failure": {
        "code": g.CODE_CONTROL_UNAVAILABLE, "detail": "planted startup refusal"}}


@pytest.mark.skipif(sys.platform not in {"darwin", "linux"}, reason="POSIX orderly exit")
@pytest.mark.parametrize("exit_code", [0, 3])
def test_orderly_exit_requires_zero_and_retains_linux_child(exit_code):
    # A normal exit is accepted, a nonzero exit is rejected, and Linux's PID
    # remains pinned until domain teardown; neither path signals a reused PID.
    process = subprocess.Popen([sys.executable, "-c", f"raise SystemExit({exit_code})"],
                               start_new_session=True)
    platform = "linux" if sys.platform == "linux" else g.PLATFORM_MACOS
    try:
        if exit_code:
            with pytest.raises(g.GoV1Error, match="did not exit cleanly") as failure:
                g._require_orderly_worker_exit(process, platform)
            assert failure.value.code == g.CODE_WORKER_PROTOCOL_INVALID
        else:
            g._require_orderly_worker_exit(process, platform)
        if platform == "linux":
            assert process.returncode is None
            status = os.waitid(os.P_PID, process.pid, os.WEXITED | os.WNOWAIT)
            assert status.si_status == exit_code
    finally:
        if process.poll() is None:
            process.kill()
        assert process.wait() == exit_code


@pytest.mark.skipif(os.name != "posix", reason="POSIX watchdog stop interleaving")
def test_watchdog_rechecks_stop_after_observation(monkeypatch):
    # A stop set during the last observation suppresses killpg, even with an
    # observation error; this plants the close-vs-poll interleaving without a signal.
    import select
    import threading
    read_fd, write_fd = os.pipe()
    observed = threading.Event()
    release = threading.Event()
    killed = []
    class Poller:
        def register(self, *args):
            pass
        def poll(self, timeout):
            observed.set()
            assert release.wait(5)
            raise OSError("planted observation failure during close")
    monkeypatch.setattr(select, "poll", Poller)
    monkeypatch.setattr(os, "getpgrp", os.getpid)
    monkeypatch.setattr(os, "getsid", lambda _: os.getpid())
    monkeypatch.setattr(os, "killpg", lambda *args: killed.append(args))
    with os.fdopen(read_fd, "rb") as stream:
        watch = g._PosixManagerLossWatch(stream, os.getppid())
        try:
            assert observed.wait(5)
            watch._stop.set()
            release.set()
            watch.close()
            assert killed == []
        finally:
            release.set()
            watch.close()
            os.close(write_fd)


@pytest.mark.skipif(sys.platform not in {"darwin", "linux"}, reason="POSIX manager shutdown")
@pytest.mark.parametrize("exit_code", [0, 3])
def test_manager_keeps_shutdown_channel_open_and_checks_exit(tmp_path, monkeypatch, exit_code):
    # The real manager teardown keeps stdin open through orderly exit, accepts 0,
    # rejects 3, and still completes native domain cleanup without changing the exit code.
    from test_builds_go_v1 import _bare_domain, _bare_client
    # Transport/control test only: identity qualification is a separate contract.
    cache = tmp_path / "worker-cache"
    cache.mkdir()
    identity = SimpleNamespace(verify=lambda: None)
    plan = SimpleNamespace(executable=identity, process_identity=identity, worker_cache=cache)
    platform = "linux" if sys.platform == "linux" else g.PLATFORM_MACOS
    domain = _bare_domain(platform)
    client, _ = _bare_client(plan, domain)
    script = """
import sys
sys.path.insert(0, sys.argv[1])
from csk.builds import go_v1 as g
sys.stdout.buffer.write(b'W')
sys.stdout.buffer.flush()
assert g._read_message(sys.stdin.buffer)['kind'] == 'shutdown'
raise SystemExit(int(sys.argv[2]))
"""
    process = subprocess.Popen([sys.executable, "-c", script,
                                str(Path(g.__file__).resolve().parents[2]), str(exit_code)],
                               stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                               stderr=subprocess.PIPE, start_new_session=True)
    client.process = process
    require_exit = g._require_orderly_worker_exit
    checked = []
    def check(process, platform):
        assert not process.stdin.closed
        checked.append(True)
        require_exit(process, platform)
    monkeypatch.setattr(g, "_require_orderly_worker_exit", check)
    try:
        # Real clients reach teardown only after worker startup/readiness. Do not
        # charge interpreter imports under host load to the shutdown grace.
        assert process.stdout.read(1) == b"W"
        if exit_code:
            with pytest.raises(g.GoV1Error, match="did not exit cleanly") as failure:
                client.teardown()
            assert failure.value.code == g.CODE_WORKER_PROTOCOL_INVALID
        else:
            client.teardown()
        assert checked == [True]
        assert process.returncode == exit_code
        assert process.stdin.closed
        assert domain.terminated
    finally:
        client._deadline_timer.cancel()
        if process.poll() is None:
            process.kill()
        process.wait()
        for stream in (process.stdin, process.stdout, process.stderr):
            stream.close()


@pytest.mark.skipif(sys.platform not in {"darwin", "linux"}, reason="POSIX watchdog grace")
def test_unhandled_pipe_hangup_expires_with_live_manager():
    # A permanently busy reader cannot disable loss cleanup; a shortened test
    # grace expires even while the parent is alive. Orderly cases above are the controls.
    script = """
import os, sys, time
sys.path.insert(0, sys.argv[1])
from csk.builds import go_v1 as g
g._WORKER_SHUTDOWN_GRACE = .1
watch = g._PosixManagerLossWatch(sys.stdin.buffer, os.getppid())
os.write(1, b'W')
time.sleep(30)
"""
    process = subprocess.Popen([sys.executable, "-c", script,
                                str(Path(g.__file__).resolve().parents[2])],
                               stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                               stderr=subprocess.PIPE, start_new_session=True)
    try:
        assert process.stdout.read(1) == b"W"
        process.stdin.close()
        assert process.wait(timeout=10) == -9
    finally:
        if process.poll() is None:
            process.kill()
        process.wait()
        for stream in (process.stdin, process.stdout, process.stderr):
            stream.close()
