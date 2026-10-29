"""Real fixture IO with a local model of DeleteFileW's read-only refusal.

This models only the documented deletion refusal, not Windows ACLs, open-handle
sharing, reparse points or native Windows execution. No shared stdlib is patched.
"""
from __future__ import annotations

import os
import stat
from pathlib import Path

import pytest

import child_cli_environment as harness
from csk.git_admission import _seal_object_store


class WindowsDeletion:
    """Delegate real IO, except DeleteFileW refuses a read-only regular file.

    https://learn.microsoft.com/en-us/windows/win32/api/fileapi/nf-fileapi-deletefilew
    """

    def __init__(self, path, events, denied, retry_error=None, chmod_error=None,
                 restore_error=None):
        self.path = path
        self.events = events
        self.denied = denied
        self.retry_error = retry_error
        self.chmod_error = chmod_error
        self.restore_error = restore_error

    def __str__(self):
        return str(self.path)

    def lstat(self):
        return self.path.lstat()

    def chmod(self, mode):
        info = self.path.lstat()
        self.events.append(("chmod", self.path, mode, info.st_nlink))
        if stat.S_ISREG(info.st_mode) and self.chmod_error is not None:
            raise self.chmod_error
        if stat.S_ISREG(info.st_mode) and not mode & stat.S_IWUSR and self.restore_error is not None:
            raise self.restore_error
        self.path.chmod(mode)

    def iterdir(self):
        for path in self.path.iterdir():
            yield type(self)(path, self.events, self.denied, self.retry_error,
                             self.chmod_error, self.restore_error)

    def rmdir(self):
        self.path.rmdir()

    def unlink(self):
        info = self.path.lstat()
        self.events.append(("unlink", self.path, info.st_mode, info.st_nlink))
        if stat.S_ISREG(info.st_mode):
            if not info.st_mode & stat.S_IWUSR:
                raise self.denied
            if self.retry_error is not None:
                raise self.retry_error
        self.path.unlink()


def snapshot(path):
    info = path.stat()
    return path.read_bytes(), stat.S_IMODE(info.st_mode), info.st_dev, info.st_ino


# Actual production sealing must clean up like writable private files and preserve the body exception.
@pytest.mark.parametrize("sealed", [False, True])
def test_windows_private_object_cleanup(tmp_path, monkeypatch, sealed):
    monkeypatch.setattr(harness, "_WINDOWS", True, raising=False)
    root = tmp_path / "owned"
    objects = root / "objects"
    objects.mkdir(parents=True)
    obj = objects / "object"
    obj.write_bytes(b"private acquired object")
    if sealed:
        _seal_object_store(objects)
    assert obj.stat().st_nlink == 1
    events = []
    denied = PermissionError(13, "DeleteFileW read-only refusal", str(obj))
    body = RuntimeError("original fault")
    with pytest.raises(RuntimeError) as caught:
        with harness.owned_cli_environment(WindowsDeletion(root, events, denied)):
            raise body
    assert caught.value is body and not root.exists()
    file_changes = [e for e in events if e[0] == "chmod" and e[1] == obj]
    assert file_changes == ([("chmod", obj, 0o600, 1)] if sealed else [])


# Shared files never have attributes changed, even in inaccessible parents; writable links still unlink.
@pytest.mark.parametrize("mode", [0o700, 0o500, 0o000])
@pytest.mark.parametrize("readonly", [False, True])
@pytest.mark.parametrize("foreign", [False, True])
def test_windows_shared_inode_boundary(tmp_path, monkeypatch, mode, readonly, foreign):
    monkeypatch.setattr(harness, "_WINDOWS", True, raising=False)
    root = tmp_path / "owned"
    root.mkdir()
    first = (tmp_path if foreign else root) / "first"
    first.write_bytes(b"shared canary")
    first.chmod(0o444 if readonly else 0o600)
    link = root / "link"
    os.link(first, link)
    before = snapshot(first)
    root.chmod(mode)
    events = []
    denied = PermissionError(13, "DeleteFileW shared read-only refusal", str(link))
    try:
        if readonly:
            with pytest.raises(PermissionError) as caught:
                with harness.owned_cli_environment(WindowsDeletion(root, events, denied)):
                    pass
            assert caught.value is denied
            assert root.exists() and snapshot(first) == snapshot(link) == before
            assert first.stat().st_nlink == 2
            assert any("CLI scratch" in note for note in caught.value.__notes__)
        else:
            with harness.owned_cli_environment(WindowsDeletion(root, events, denied)):
                pass
            assert not root.exists()
        assert not any(e[0] == "chmod" and e[1] in (first, link) for e in events)
        if foreign:
            assert snapshot(first) == before
    finally:
        # These are this test's two canary names, after the preservation assertions.
        # Clearing their shared attribute here permits native Windows fixture disposal.
        if first.exists():
            first.chmod(0o600)
        if root.exists():
            root.chmod(0o700)
        for path in (link, first):
            path.unlink(missing_ok=True)


# File and directory symlinks unlink without traversing or chmodding their targets.
def test_windows_symlink_targets_untouched(tmp_path, monkeypatch):
    monkeypatch.setattr(harness, "_WINDOWS", True, raising=False)
    root = tmp_path / "owned"
    root.mkdir()
    target = tmp_path / "target"
    target.mkdir()
    file = target / "canary"
    file.write_bytes(b"symlink canary")
    file.chmod(0o444)
    before = snapshot(file)
    (root / "file-link").symlink_to(file)
    (root / "directory-link").symlink_to(target, target_is_directory=True)
    events = []
    with harness.owned_cli_environment(WindowsDeletion(root, events, PermissionError(13, "denied"))):
        pass
    assert not root.exists() and snapshot(file) == before and target.is_dir()
    assert [e[1] for e in events if e[0] == "chmod"] == [root]


# Refused attribute restoration/retry remains a real failure; an original body fault is also retained.
@pytest.mark.parametrize("stage", ["chmod", "retry", "restore"])
@pytest.mark.parametrize("body_fails", [False, True])
def test_windows_cleanup_failure_is_visible(tmp_path, monkeypatch, stage, body_fails):
    monkeypatch.setattr(harness, "_WINDOWS", True, raising=False)
    root = tmp_path / "owned"
    root.mkdir()
    obj = root / "object"
    obj.write_bytes(b"private")
    _seal_object_store(root)
    before = snapshot(obj)
    events = []
    denied = PermissionError(13, "DeleteFileW read-only refusal", str(obj))
    failure = OSError(5, "injected " + stage, str(obj))
    restoration = OSError(5, "injected mode restoration", str(obj))
    adapter = WindowsDeletion(root, events, denied,
                              retry_error=failure if stage != "chmod" else None,
                              chmod_error=failure if stage == "chmod" else None,
                              restore_error=restoration if stage == "restore" else None)
    body = RuntimeError("original fault")
    expected = BaseExceptionGroup if body_fails or stage == "restore" else OSError
    with pytest.raises(expected) as caught:
        with harness.owned_cli_environment(adapter):
            if body_fails:
                raise body
    if body_fails:
        assert caught.value.exceptions[0] is body
        cleanup = caught.value.exceptions[1]
    else:
        cleanup = caught.value
    if stage == "restore":
        assert cleanup.exceptions == (failure, restoration)
        after = snapshot(obj)
        assert after == (before[0], 0o600, *before[2:])
    else:
        assert cleanup is failure and snapshot(obj) == before
    assert root.exists() and any(str(root) in note for note in cleanup.__notes__)


# A writable file's deletion refusal is not evidence of a read-only attribute problem.
def test_windows_writable_refusal_never_chmodded(tmp_path, monkeypatch):
    monkeypatch.setattr(harness, "_WINDOWS", True, raising=False)
    root = tmp_path / "owned"
    root.mkdir()
    obj = root / "object"
    obj.write_bytes(b"private")
    before = snapshot(obj)
    events = []
    denied = PermissionError(13, "other deletion refusal", str(obj))
    with pytest.raises(PermissionError) as caught:
        with harness.owned_cli_environment(WindowsDeletion(root, events, denied, retry_error=denied)):
            pass
    assert caught.value is denied and snapshot(obj) == before
    assert not any(e[0] == "chmod" and e[1] == obj for e in events)


# A deletion refusal on the POSIX branch propagates without any file chmod fallback.
def test_posix_refusal_never_chmodded(tmp_path, monkeypatch):
    monkeypatch.setattr(harness, "_WINDOWS", False, raising=False)
    root = tmp_path / "owned"
    root.mkdir()
    obj = root / "object"
    obj.write_bytes(b"private")
    _seal_object_store(root)
    events = []
    denied = PermissionError(13, "deletion refused", str(obj))
    before = snapshot(obj)
    with pytest.raises(PermissionError) as caught:
        with harness.owned_cli_environment(WindowsDeletion(root, events, denied)):
            pass
    assert caught.value is denied and snapshot(obj) == before
    assert not any(e[0] == "chmod" and e[1] == obj for e in events)
