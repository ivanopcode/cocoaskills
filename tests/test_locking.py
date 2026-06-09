from __future__ import annotations

import pytest

from csk.locking import GlobalLock, LockError


def test_global_lock_times_out_when_held(tmp_path):
    home = tmp_path / "home"
    with GlobalLock(home, timeout=0.1):
        with pytest.raises(LockError):
            with GlobalLock(home, timeout=0.1):
                pass



def test_stale_lock_from_dead_process_is_broken(tmp_path):
    import json
    import subprocess
    import sys

    proc = subprocess.run([sys.executable, "-c", "import os; print(os.getpid())"], capture_output=True, text=True)
    dead_pid = int(proc.stdout.strip())
    lock_path = tmp_path / ".lock"
    lock_path.write_text(json.dumps({"pid": dead_pid, "created_at": 0}), encoding="utf-8")

    with GlobalLock(tmp_path, timeout=0.5) as lock:
        assert lock.acquired
    assert not lock_path.exists()


def test_lock_held_by_live_process_still_times_out(tmp_path):
    import json
    import os

    import pytest

    lock_path = tmp_path / ".lock"
    lock_path.write_text(json.dumps({"pid": os.getpid(), "created_at": 0}), encoding="utf-8")

    with pytest.raises(LockError):
        with GlobalLock(tmp_path, timeout=0.3):
            pass
    assert lock_path.exists()


def test_corrupt_lock_is_not_broken(tmp_path):
    import pytest

    lock_path = tmp_path / ".lock"
    lock_path.write_text("not json", encoding="utf-8")

    with pytest.raises(LockError):
        with GlobalLock(tmp_path, timeout=0.3):
            pass
    assert lock_path.exists()
