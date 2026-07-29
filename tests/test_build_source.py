from __future__ import annotations

import os
from pathlib import Path

import pytest

from csk import hashing
from csk.builds import source


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


def test_frozen_snapshot_rejects_non_portable_descendant(tmp_path):
    root = tmp_path / "snapshot"
    root.mkdir()
    _write(root, "bad:name", b"unsafe")

    with pytest.raises(source.InvalidSnapshotError, match="non-portable"):
        source.freeze_snapshot(root)


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
