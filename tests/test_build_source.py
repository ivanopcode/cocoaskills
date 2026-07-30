from __future__ import annotations

import ctypes
import os
from collections.abc import Callable
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from csk import hashing
from csk.builds import _windows, source


class _Win32Function:
    def __init__(self, callback: Callable[..., Any]) -> None:
        self._callback = callback
        self.argtypes: object = None
        self.restype: object = None

    def __call__(self, *args: object) -> Any:
        return self._callback(*args)


def _set_windows_stream_name(pointer: object, name: str) -> None:
    data = ctypes.cast(
        pointer,
        ctypes.POINTER(_windows._Win32FindStreamData),
    ).contents
    data.stream_name = name


def _install_windows_stream_api(
    monkeypatch: pytest.MonkeyPatch,
    *,
    find_first: Callable[..., int],
    find_next: Callable[..., int],
    find_close: Callable[..., int],
) -> None:
    kernel32 = SimpleNamespace(
        FindFirstStreamW=_Win32Function(find_first),
        FindNextStreamW=_Win32Function(find_next),
        FindClose=_Win32Function(find_close),
    )

    def load_library(name: str, *, use_last_error: bool) -> object:
        assert name == "kernel32"
        assert use_last_error is True
        return kernel32

    monkeypatch.setattr(ctypes, "WinDLL", load_library, raising=False)


def _write(root: Path, relative: str, content: bytes) -> None:
    path = root / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content)


def test_build_source_hash_matches_shared_empty_binary_ordering_and_marker_vectors():
    assert hashing.build_source_sha256([]) == (
        "sha256:3a518980ed122b2139e46152d9c4dda7426a42572f3235cde8cbe781566f5753"
    )

    records = [
        ("z-binary.bin", b"\x00\xff\x01"),
        ("empty", b""),
        (".csk-install.json", b'{"variant":"fixture"}\n'),
        ("\N{LATIN SMALL LETTER E WITH ACUTE}.txt", b"utf8\n"),
    ]

    assert hashing.build_source_sha256(records) == (
        "sha256:68008c9a1131c1295d78f4f7d184c3df5f7382a88d8d40333be7cf02b2ee4de9"
    )


def test_build_source_framing_separates_legacy_nul_stream_collision():
    one_file = hashing.build_source_sha256([("a", b"x\x00b\x00y")])
    two_files = hashing.build_source_sha256([("a", b"x"), ("b", b"y")])

    assert one_file == "sha256:96e3ed15c69a125ac033997b1f53baababece8a0be0831590f9282431ab6bc85"
    assert two_files == "sha256:15068f03268e971a11b928800ca920c6841dee76747bb3411ae93ff4ab77a334"
    assert one_file != two_files


@pytest.mark.parametrize(
    ("records", "match"),
    [
        ([("same", b"a"), ("same", b"b")], "duplicate"),
        ([("Dir/File", b"a"), ("dir/file", b"b")], "platform"),
        ([("\N{LATIN SMALL LETTER E WITH ACUTE}.txt", b"a"), ("e\u0301.txt", b"b")], "platform"),
        ([("bad:name", b"a")], "non-portable"),
        ([("\udcff", b"a")], "Unicode"),
    ],
)
def test_build_source_hash_rejects_duplicate_invalid_and_platform_colliding_paths(records, match):
    with pytest.raises(hashing.HashingError, match=match):
        hashing.build_source_sha256(records)


def test_frozen_snapshot_identity_includes_root_marker_while_legacy_hash_does_not(tmp_path):
    left = tmp_path / "left"
    right = tmp_path / "right"
    for root in (left, right):
        root.mkdir()
        _write(root, "go.mod", b"module example\n")
    _write(left, ".csk-install.json", b'{"variant":"A"}\n')
    _write(right, ".csk-install.json", b'{"variant":"B"}\n')

    with source.freeze_snapshot(left) as left_snapshot, source.freeze_snapshot(right) as right_snapshot:
        assert left_snapshot.identity.algorithm == hashing.BUILD_SOURCE_ALGORITHM
        assert left_snapshot.identity.content_sha256 != right_snapshot.identity.content_sha256

    assert hashing.content_sha256(left) == hashing.content_sha256(right)


def test_frozen_snapshot_matches_shared_binary_vector_and_ignores_mode_and_timestamp(tmp_path):
    root = tmp_path / "snapshot"
    root.mkdir()
    records = [
        ("z-binary.bin", b"\x00\xff\x01"),
        ("empty", b""),
        (".csk-install.json", b'{"variant":"fixture"}\n'),
        ("\N{LATIN SMALL LETTER E WITH ACUTE}.txt", b"utf8\n"),
    ]
    for relative, content in records:
        _write(root, relative, content)

    with source.freeze_snapshot(root) as frozen:
        assert frozen.path == root.absolute()
        assert frozen.identity.content_sha256 == (
            "sha256:68008c9a1131c1295d78f4f7d184c3df5f7382a88d8d40333be7cf02b2ee4de9"
        )
        binary = root / "z-binary.bin"
        binary.chmod(0o755)
        os.utime(binary, (1_893_456_000, 1_893_456_000))
        frozen.recheck()


def test_frozen_snapshot_path_scan_does_not_use_cached_direntry_stat(tmp_path, monkeypatch):
    root = tmp_path / "snapshot"
    root.mkdir()
    _write(root, "nested/file", b"content")
    original_scandir = os.scandir

    class EntryWithoutPhysicalIdentity:
        def __init__(self, name):
            self.name = name

        def stat(self, *, follow_symlinks=True):
            del follow_symlinks
            raise AssertionError("path scan must use os.lstat for physical identity")

    class ScandirWithoutPhysicalIdentity:
        def __init__(self, names):
            self._entries = [EntryWithoutPhysicalIdentity(name) for name in names]

        def __enter__(self):
            return iter(self._entries)

        def __exit__(self, exc_type, exc, traceback):
            del exc_type, exc, traceback

    def scandir_without_physical_identity(path):
        with original_scandir(path) as entries:
            names = [entry.name for entry in entries]
        return ScandirWithoutPhysicalIdentity(names)

    monkeypatch.setattr(os, "scandir", scandir_without_physical_identity)

    with source.freeze_snapshot(root) as frozen:
        assert frozen.identity.content_sha256 == hashing.build_source_sha256(
            [("nested/file", b"content")]
        )


def test_windows_stream_enumerator_filters_default_stream(tmp_path, monkeypatch):
    calls = 0
    closed = []

    def find_first(path, level, data, flags):
        del path, level, flags
        _set_windows_stream_name(data, "::$DATA")
        return 42

    def find_next(handle, data):
        nonlocal calls
        del handle
        calls += 1
        if calls == 1:
            _set_windows_stream_name(data, ":hidden:$DATA")
            return 1
        return 0

    _install_windows_stream_api(
        monkeypatch,
        find_first=find_first,
        find_next=find_next,
        find_close=lambda handle: closed.append(handle) or 1,
    )
    monkeypatch.setattr(ctypes, "get_last_error", lambda: 38, raising=False)

    assert _windows.named_data_streams(tmp_path / "file") == (":hidden:$DATA",)
    assert closed == [42]


@pytest.mark.parametrize("last_error", [38, 87])
def test_windows_stream_enumerator_accepts_documented_empty_results(
    tmp_path, monkeypatch, last_error
):
    closed = []
    _install_windows_stream_api(
        monkeypatch,
        find_first=lambda *_args: _windows._INVALID_HANDLE_VALUE,
        find_next=lambda *_args: pytest.fail("FindNextStreamW must not be called"),
        find_close=lambda handle: closed.append(handle) or 1,
    )
    monkeypatch.setattr(ctypes, "get_last_error", lambda: last_error, raising=False)

    assert _windows.named_data_streams(tmp_path / "file") == ()
    assert closed == []


def test_windows_stream_enumerator_rejects_unexpected_first_error(tmp_path, monkeypatch):
    closed = []
    _install_windows_stream_api(
        monkeypatch,
        find_first=lambda *_args: _windows._INVALID_HANDLE_VALUE,
        find_next=lambda *_args: pytest.fail("FindNextStreamW must not be called"),
        find_close=lambda handle: closed.append(handle) or 1,
    )
    monkeypatch.setattr(ctypes, "get_last_error", lambda: 5, raising=False)

    with pytest.raises(OSError, match="enumerate") as error:
        _windows.named_data_streams(tmp_path / "file")

    assert error.value.errno == 5
    assert closed == []


def test_windows_stream_enumerator_rejects_unexpected_next_error_and_closes(
    tmp_path, monkeypatch
):
    last_error = 5
    closed = []

    def find_first(_path, _level, data, _flags):
        _set_windows_stream_name(data, "::$DATA")
        return 42

    _install_windows_stream_api(
        monkeypatch,
        find_first=find_first,
        find_next=lambda *_args: 0,
        find_close=lambda handle: closed.append(handle) or 1,
    )
    monkeypatch.setattr(ctypes, "get_last_error", lambda: last_error, raising=False)

    with pytest.raises(OSError, match="enumerate") as error:
        _windows.named_data_streams(tmp_path / "file")

    assert error.value.errno == 5
    assert closed == [42]


def test_windows_stream_enumerator_rejects_find_close_failure(tmp_path, monkeypatch):
    last_error = 38
    closed = []

    def find_first(_path, _level, data, _flags):
        _set_windows_stream_name(data, "::$DATA")
        return 42

    def find_close(handle):
        nonlocal last_error
        closed.append(handle)
        last_error = 5
        return 0

    _install_windows_stream_api(
        monkeypatch,
        find_first=find_first,
        find_next=lambda *_args: 0,
        find_close=find_close,
    )
    monkeypatch.setattr(ctypes, "get_last_error", lambda: last_error, raising=False)

    with pytest.raises(OSError, match="close") as error:
        _windows.named_data_streams(tmp_path / "file")

    assert error.value.errno == 5
    assert closed == [42]


def test_windows_stream_enumerator_preserves_enumeration_error_when_close_also_fails(
    tmp_path, monkeypatch
):
    last_error = 123

    def find_first(_path, _level, data, _flags):
        _set_windows_stream_name(data, "::$DATA")
        return 42

    def find_close(_handle):
        nonlocal last_error
        last_error = 5
        return 0

    _install_windows_stream_api(
        monkeypatch,
        find_first=find_first,
        find_next=lambda *_args: 0,
        find_close=find_close,
    )
    monkeypatch.setattr(ctypes, "get_last_error", lambda: last_error, raising=False)

    with pytest.raises(OSError, match="enumerate") as error:
        _windows.named_data_streams(tmp_path / "file")

    assert error.value.errno == 123
    assert isinstance(error.value.__cause__, OSError)
    assert error.value.__cause__.errno == 5
    assert "close" in str(error.value.__cause__)


def test_frozen_snapshot_recheck_inspects_root_windows_streams(tmp_path, monkeypatch):
    root = tmp_path / "snapshot"
    root.mkdir()
    _write(root, "file", b"content")
    root_stream_present = False
    checks = []

    def reject(_path, relative):
        checks.append(relative)
        if root_stream_present and relative == ".":
            raise source.InvalidSnapshotError("simulated root data stream")

    monkeypatch.setattr(source, "_reject_windows_named_streams", reject)
    frozen = source.freeze_snapshot(root)
    checks.clear()
    root_stream_present = True
    try:
        with pytest.raises(source.SnapshotMutationError, match="data stream"):
            frozen.recheck()
    finally:
        frozen.close()

    assert checks == ["."]


def test_frozen_snapshot_use_rechecks_root_windows_streams_after_callback(
    tmp_path, monkeypatch
):
    root = tmp_path / "snapshot"
    root.mkdir()
    _write(root, "file", b"content")
    root_stream_present = False
    checks = []

    def reject(_path, relative):
        checks.append(relative)
        if root_stream_present and relative == ".":
            raise source.InvalidSnapshotError("simulated root data stream")

    def add_root_stream(_frozen):
        nonlocal root_stream_present
        root_stream_present = True

    monkeypatch.setattr(source, "_reject_windows_named_streams", reject)
    frozen = source.freeze_snapshot(root)
    checks.clear()
    try:
        with pytest.raises(source.SnapshotMutationError, match="data stream"):
            frozen.use(add_root_stream)
    finally:
        frozen.close()

    assert checks.count(".") == 2
    assert checks[0] == "."
    assert checks[-1] == "."


def test_frozen_snapshot_use_rechecks_descendant_windows_streams_after_callback(
    tmp_path, monkeypatch
):
    root = tmp_path / "snapshot"
    root.mkdir()
    _write(root, "file", b"content")
    descendant_stream_present = False
    checks = []

    def reject(_path, relative):
        checks.append(relative)
        if descendant_stream_present and relative == "file":
            raise source.InvalidSnapshotError("simulated descendant data stream")

    def add_descendant_stream(_frozen):
        nonlocal descendant_stream_present
        descendant_stream_present = True

    monkeypatch.setattr(source, "_open_root_fd", lambda _path: None)
    monkeypatch.setattr(source, "_reject_windows_named_streams", reject)
    frozen = source.freeze_snapshot(root)
    checks.clear()
    try:
        with pytest.raises(source.SnapshotMutationError, match="data stream"):
            frozen.use(add_descendant_stream)
    finally:
        frozen.close()

    assert checks == [".", "file", ".", "file"]


def test_frozen_snapshot_rejects_non_portable_descendant(tmp_path):
    root = tmp_path / "snapshot"
    root.mkdir()
    _write(root, "bad:name", b"unsafe")

    with pytest.raises(source.InvalidSnapshotError, match="non-portable"):
        source.freeze_snapshot(root)


@pytest.mark.skipif(os.name != "nt", reason="Windows named data streams are unavailable")
def test_frozen_snapshot_rejects_windows_named_data_stream(tmp_path):
    root = tmp_path / "snapshot"
    root.mkdir()
    file = root / "file"
    file.write_bytes(b"default")
    try:
        Path(f"{file}:hidden").write_bytes(b"hidden")
    except OSError as exc:
        pytest.skip(f"named data streams unavailable: {exc}")

    with pytest.raises(source.InvalidSnapshotError, match="data stream"):
        source.freeze_snapshot(root)


@pytest.mark.skipif(os.name != "nt", reason="Windows named data streams are unavailable")
def test_frozen_snapshot_rejects_windows_named_data_stream_on_root(tmp_path):
    root = tmp_path / "snapshot"
    root.mkdir()
    _write(root, "file", b"default")
    stream = Path(f"{root}:hidden")
    try:
        stream.write_bytes(b"hidden")
    except OSError as exc:
        pytest.skip(f"named data streams unavailable: {exc}")

    with pytest.raises(source.InvalidSnapshotError, match="data stream"):
        source.freeze_snapshot(root)


@pytest.mark.skipif(os.name != "nt", reason="Windows named data streams are unavailable")
def test_frozen_snapshot_use_rejects_windows_root_stream_added_by_callback(tmp_path):
    root = tmp_path / "snapshot"
    root.mkdir()
    _write(root, "file", b"default")
    stream = Path(f"{root}:hidden")
    try:
        stream.write_bytes(b"probe")
        stream.unlink()
    except OSError as exc:
        pytest.skip(f"named data streams unavailable: {exc}")

    frozen = source.freeze_snapshot(root)

    def add_root_stream(_frozen):
        stream.write_bytes(b"hidden")

    try:
        with pytest.raises(source.SnapshotMutationError, match="data stream"):
            frozen.use(add_root_stream)
    finally:
        frozen.close()


@pytest.mark.skipif(os.name != "nt", reason="Windows named data streams are unavailable")
def test_frozen_snapshot_use_rejects_windows_descendant_stream_added_by_callback(tmp_path):
    root = tmp_path / "snapshot"
    root.mkdir()
    file = root / "file"
    file.write_bytes(b"default")
    stream = Path(f"{file}:hidden")
    try:
        stream.write_bytes(b"probe")
        stream.unlink()
    except OSError as exc:
        pytest.skip(f"named data streams unavailable: {exc}")

    frozen = source.freeze_snapshot(root)

    def add_descendant_stream(_frozen):
        stream.write_bytes(b"hidden")

    try:
        with pytest.raises(source.SnapshotMutationError, match="data stream"):
            frozen.use(add_descendant_stream)
    finally:
        frozen.close()


def test_frozen_snapshot_rejects_symbolic_link_descendant_and_root(tmp_path):
    root = tmp_path / "snapshot"
    root.mkdir()
    _write(root, "target", b"target")
    try:
        (root / "link").symlink_to("target")
    except OSError as exc:
        pytest.skip(f"symlinks unavailable: {exc}")

    with pytest.raises(source.InvalidSnapshotError, match="link"):
        source.freeze_snapshot(root)
    with pytest.raises(source.InvalidSnapshotError, match="root"):
        source.freeze_snapshot(root / "link")


@pytest.mark.skipif(not hasattr(os, "mkfifo"), reason="FIFO creation is unavailable")
def test_frozen_snapshot_rejects_special_file(tmp_path):
    root = tmp_path / "snapshot"
    root.mkdir()
    os.mkfifo(root / "pipe")

    with pytest.raises(source.InvalidSnapshotError, match="special file"):
        source.freeze_snapshot(root)


@pytest.mark.parametrize("mutation", ["bytes", "tree", "link"])
def test_frozen_snapshot_use_rechecks_after_last_child_and_rejects_mutation(tmp_path, mutation):
    root = tmp_path / "snapshot"
    root.mkdir()
    _write(root, "file", b"before")
    _write(root, "target", b"target")
    frozen = source.freeze_snapshot(root)

    def mutate(_: source.FrozenSnapshot) -> None:
        if mutation == "bytes":
            _write(root, "file", b"after")
        elif mutation == "tree":
            (root / "empty-directory").mkdir()
        else:
            (root / "file").unlink()
            try:
                (root / "file").symlink_to("target")
            except OSError as exc:
                pytest.skip(f"symlinks unavailable: {exc}")

    try:
        with pytest.raises(source.SnapshotMutationError):
            frozen.use(mutate)
    finally:
        frozen.close()


def test_frozen_snapshot_rejects_root_replacement_even_with_identical_bytes(tmp_path):
    root = tmp_path / "snapshot"
    root.mkdir()
    _write(root, "file", b"same")
    frozen = source.freeze_snapshot(root)
    root.rename(tmp_path / "old")
    root.mkdir()
    _write(root, "file", b"same")

    try:
        with pytest.raises(source.SnapshotMutationError, match="root"):
            frozen.recheck()
    finally:
        frozen.close()


def test_frozen_snapshot_use_rechecks_before_callback(tmp_path):
    root = tmp_path / "snapshot"
    root.mkdir()
    _write(root, "file", b"before")
    frozen = source.freeze_snapshot(root)
    _write(root, "file", b"after")
    called = False

    def callback(_: source.FrozenSnapshot) -> None:
        nonlocal called
        called = True

    try:
        with pytest.raises(source.SnapshotMutationError):
            frozen.use(callback)
    finally:
        frozen.close()

    assert not called
