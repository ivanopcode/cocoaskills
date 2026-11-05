"""Internal native controls; this file does not qualify Linux build admission."""
import errno
import os
import subprocess
import sys
from pathlib import Path

import pytest

from csk import installer
from csk.builds import go_v1 as g


def test_linux_admission_stays_closed(monkeypatch):
    # A working internal adapter must not admit Linux through either public predicate.
    monkeypatch.setattr(sys, "platform", "linux")
    assert not installer.supports_external_builds()
    with pytest.raises(g.GoV1Error) as failure:
        g.inventory_platform()
    assert failure.value.code == g.CODE_CONTROL_UNAVAILABLE
    monkeypatch.setattr(sys, "platform", "darwin")
    assert installer.supports_external_builds()
    assert g.inventory_platform() == g.PLATFORM_MACOS


@pytest.mark.skipif(sys.platform != "linux", reason="real Linux controls")
def test_native_linux_launch_observation_and_join(tmp_path):
    # Real child inherits session, file limit and authenticated FD; no public identity claim.
    platform, probes = g.probe_native_controls(g.ResourceLimits(), _platform="linux")
    domain = g._NativeControlDomain(platform, probes, g.ResourceLimits())
    previous = __import__("resource").getrlimit(__import__("resource").RLIMIT_FSIZE)
    capability = g._PreparedWorkerLaunch(platform)
    options = dict(start_new_session=True, stdout=subprocess.PIPE, close_fds=True)
    capability.add_popen_options(options)
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
    process = None
    try:
        process = domain._launch_macos([sys.executable, "-c", script], options)
        capability.close_parent_copy()
        assert process.stdout.readline() == b"observed\n"
        domain.terminate(process)
        assert domain.terminated
        domain.terminate(process)
        with pytest.raises(ProcessLookupError):
            os.killpg(process.pid, 0)
    finally:
        capability.close_parent_copy()
        if process is not None:
            if process.poll() is None:
                process.kill()
            process.wait()
            process.stdout.close()
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
        monkeypatch.setattr(g, "_WORKER_LAUNCH_FDS", (read_fd + 100,))
        capability = g._PreparedWorkerLaunch("linux")
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
    result = subprocess.run([sys.executable, str(fixture), str(tmp_path / mode), mode],
                            capture_output=True, text=True, timeout=60)
    assert result.returncode == 0, result.stderr
    record = json.loads(result.stdout)
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
    result = subprocess.run([sys.executable, "-c", script], capture_output=True, text=True, timeout=10)
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
    process = type("Process", (), {"pid": 12345, "returncode": None, "wait": failed_wait})()
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
