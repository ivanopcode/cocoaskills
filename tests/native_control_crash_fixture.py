import ctypes, json, os, signal, subprocess, sys, threading, time
from pathlib import Path

# Bind synthetic child imports to this checkout, independent of pytest launcher.
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

root = Path(sys.argv[1]).resolve()
mode = sys.argv[2]
role = sys.argv[3] if len(sys.argv) > 3 else 'fixture'

def write(name, value):
    temporary = root / f'{name}.{os.getpid()}.tmp'
    temporary.write_text(json.dumps(value))
    temporary.replace(root / name)

def live(pid):
    try:
        if sys.platform == 'linux':
            return Path(f'/proc/{pid}/stat').read_text().split(') ')[1][0] != 'Z'
        result = subprocess.run(['ps', '-o', 'stat=', '-p', str(pid)],
                                capture_output=True, text=True, check=False)
        status = result.stdout.strip()
        return bool(status) and not status.startswith('Z')
    except (OSError, ProcessLookupError):
        return False

if role == 'worker':
    parent = os.getppid()
    if mode in ('pdeath', 'both'):
        libc = ctypes.CDLL(None, use_errno=True)
        assert libc.prctl(1, signal.SIGKILL, 0, 0, 0) == 0
        assert os.getppid() == parent
    children = []
    for detached in (False, True):
        child = os.fork()
        if child == 0:
            if detached:
                os.setsid()
            write('detached' if detached else 'member', os.getpid())
            time.sleep(60)
            os._exit(0)
        children.append(child)
    if mode in ('eof', 'both'):
        from csk.builds.go_v1 import _PosixManagerLossWatch
        watch = _PosixManagerLossWatch(sys.stdin.buffer, parent)
    write('ready', {'worker': os.getpid(), 'sid': os.getsid(0), 'pgid': os.getpgrp()})
    time.sleep(60)
    for child in children:
        os.waitpid(child, 0)
elif role == 'manager':
    from csk.builds.go_v1 import _NativeControlDomain
    domain = object.__new__(_NativeControlDomain)
    domain.file_limit = 1024 * 1024
    p = domain._launch_macos([sys.executable, __file__, str(root), mode, 'worker'],
        {'stdin': subprocess.PIPE, 'start_new_session': True, 'close_fds': True})
    write('worker', p.pid)
    p.wait()
else:
    root.mkdir(parents=True, exist_ok=True)
    if sys.platform == 'linux':
        libc = ctypes.CDLL(None, use_errno=True)
        assert libc.prctl(36, 1, 0, 0, 0) == 0  # Fixture alone adopts its descendants.
    manager = subprocess.Popen([sys.executable, __file__, str(root), mode, 'manager'])
    pids = []
    try:
        deadline = time.monotonic() + 30
        while not all((root / name).exists() for name in ('ready', 'member', 'detached')):
            if manager.poll() is not None or time.monotonic() > deadline:
                raise RuntimeError('worker readiness failed')
            time.sleep(.01)
        pids = [json.loads((root / name).read_text()) for name in ('worker', 'member', 'detached')]
        manager.kill()
        manager.wait(timeout=5)
        time.sleep(.5)
        result = dict(zip(('worker_live', 'member_live', 'detached_live'), map(live, pids)))
        result.update(mode=mode, platform=sys.platform, manager_returncode=manager.returncode,
                      launch=json.loads((root / 'ready').read_text()))
        print(json.dumps(result), flush=True)
    finally:
        if manager.poll() is None:
            manager.kill()
            manager.wait()
        for name in ('worker', 'member', 'detached'):
            try:
                pid = json.loads((root / name).read_text())
                if pid not in pids:
                    pids.append(pid)
            except (OSError, ValueError):
                pass
        for pid in pids:
            try:
                os.kill(pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
        if sys.platform == 'linux':
            for pid in pids:
                try:
                    os.waitpid(pid, 0)
                except ChildProcessError:
                    # A failed startup may have been joined by its own parent.
                    pass
