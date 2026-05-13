from __future__ import annotations

import pytest

from csk.locking import GlobalLock, LockError


def test_global_lock_times_out_when_held(tmp_path):
    home = tmp_path / "home"
    with GlobalLock(home, timeout=0.1):
        with pytest.raises(LockError):
            with GlobalLock(home, timeout=0.1):
                pass

