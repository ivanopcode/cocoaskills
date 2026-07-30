from __future__ import annotations

import hashlib
import os
import platform
import shutil
import stat
import threading
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from pathlib import Path
from typing import cast

import pytest

from csk import protocol_json
from csk.builds.cache import (
    BuildCacheBackend,
    BuildCacheError,
    CacheConflictError,
    CacheEntryStatus,
    CacheExpectation,
    CacheMutationGuard,
    CachePublication,
    CachePublicationStatus,
    cache_for_manager_home,
)
from csk.builds import cache_posix
from csk.builds.cache_posix import PosixBuildCache
from csk.builds.metadata import (
    BuildArtifact,
    GoBuildInput,
    build_receipt,
    cache_key,
    canonical_receipt_bytes,
    receipt_sha256,
)
from csk.builds.source import BuildSourceIdentity
from csk.builds.toolchain import (
    GO_RELPATH,
    TOOLCHAIN_ALGORITHM,
    NativeTarget,
    ToolchainIdentity,
)


pytestmark = pytest.mark.skipif(os.name != "posix", reason="POSIX cache tests")


class _HeldGuard:
    def assert_held(self) -> None:
        pass


class _ReleasedGuard:
    def assert_held(self) -> None:
        raise RuntimeError("released")


@pytest.fixture(autouse=True)
def _restore_test_permissions(tmp_path: Path) -> Iterator[None]:
    """Let pytest remove intentionally immutable/adversarial test trees."""

    yield
    for root, directories, files in os.walk(tmp_path, topdown=True, followlinks=False):
        root_path = Path(root)
        try:
            root_path.chmod(0o700)
        except OSError:
            pass
        for name in directories:
            path = root_path / name
            try:
                if not path.is_symlink():
                    path.chmod(0o700)
            except OSError:
                pass
        for name in files:
            path = root_path / name
            try:
                if not path.is_symlink():
                    path.chmod(0o600)
            except OSError:
                pass


def _go_target() -> tuple[str, str, dict[str, str]]:
    goos = "darwin" if platform.system() == "Darwin" else "linux"
    machine = platform.machine().lower()
    if machine in {"arm64", "aarch64"}:
        return goos, "arm64", {"GOARM64": "v8.0"}
    if machine in {"x86_64", "amd64"}:
        return goos, "amd64", {"GOAMD64": "v1"}
    pytest.skip(f"unsupported test architecture: {machine}")


def _build_input(command: str = "golden-tool") -> GoBuildInput:
    goos, goarch, tuning = _go_target()
    return GoBuildInput(
        build_source=BuildSourceIdentity(
            algorithm="curator-build-source-v1",
            content_sha256="sha256:" + "b" * 64,
        ),
        build_root="build",
        command=command,
        source_dir=f"build/cmd/{command}",
        target=NativeTarget(goos=goos, goarch=goarch, tuning=tuning),
        toolchain=ToolchainIdentity(
            algorithm=TOOLCHAIN_ALGORITHM,
            content_sha256="sha256:" + "c" * 64,
            go_relpath=GO_RELPATH,
            go_version=f"go version go1.26.1 {goos}/{goarch}",
        ),
    )


def _new_store(tmp_path: Path) -> tuple[Path, PosixBuildCache]:
    home = tmp_path / ".cocoaskills"
    home.mkdir(mode=0o700)
    home.chmod(0o700)
    return home, PosixBuildCache(home)


def _publication(
    tmp_path: Path,
    build_input: GoBuildInput,
    artifact_bytes: bytes,
) -> tuple[CachePublication, str]:
    digest = hashlib.sha256(artifact_bytes).hexdigest()
    source_root = tmp_path / "private-builds"
    source_root.mkdir(mode=0o700, exist_ok=True)
    source_root.chmod(0o700)
    source = source_root / f"{build_input.command}-{digest[:12]}"
    source.write_bytes(artifact_bytes)
    source.chmod(0o700)
    receipt = build_receipt(
        build_input,
        BuildArtifact(
            path=build_input.artifact_path,
            sha256=f"sha256:{digest}",
            size=len(artifact_bytes),
        ),
    )
    raw = canonical_receipt_bytes(receipt)
    return (
        CachePublication(
            input=build_input,
            receipt_bytes=raw,
            artifact_source=source,
        ),
        receipt_sha256(raw),
    )


def _entry_path(home: Path, build_input: GoBuildInput) -> Path:
    key = cache_key(build_input)
    return home / "builds" / "go-v1" / key.removeprefix("sha256:")


def _tree_state(root: Path) -> tuple[tuple[str, str, int, int, str], ...]:
    records: list[tuple[str, str, int, int, str]] = []
    pending = [root]
    while pending:
        path = pending.pop()
        info = path.lstat()
        relative = "." if path == root else path.relative_to(root).as_posix()
        mode = stat.S_IMODE(info.st_mode)
        if stat.S_ISDIR(info.st_mode):
            kind = "directory"
            payload = ""
            try:
                with os.scandir(path) as iterator:
                    children = sorted(
                        (Path(entry.path) for entry in iterator),
                        reverse=True,
                    )
            except PermissionError:
                children = []
                payload = "unreadable"
            pending.extend(children)
        elif stat.S_ISREG(info.st_mode):
            kind = "file"
            payload = hashlib.sha256(path.read_bytes()).hexdigest()
        elif stat.S_ISLNK(info.st_mode):
            kind = "symlink"
            payload = os.readlink(path)
        else:
            kind = "special"
            payload = ""
        records.append((relative, kind, mode, info.st_size, payload))
    return tuple(sorted(records))


def _write_immutable(path: Path, raw: bytes, mode: int) -> None:
    path.write_bytes(raw)
    path.chmod(mode)


def _replace_immutable_file(path: Path, raw: bytes, mode: int) -> None:
    path.chmod(0o600)
    path.write_bytes(raw)
    path.chmod(mode)


def test_csk_layout_publish_hit_immutability_and_identical_reuse(tmp_path: Path) -> None:
    home, store = _new_store(tmp_path)
    build_input = _build_input()
    publication, expected_receipt_hash = _publication(
        tmp_path,
        build_input,
        b"verified executable",
    )

    before = _tree_state(home)
    miss = store.inspect(CacheExpectation(input=build_input))
    assert miss.status is CacheEntryStatus.MISS
    assert miss.dry_run_outcome == "would-preflight-and-build"
    assert _tree_state(home) == before
    assert not (home / "builds").exists()

    assert isinstance(store, BuildCacheBackend)
    assert isinstance(cache_for_manager_home(home), PosixBuildCache)
    published = store.publish(publication, guard=_HeldGuard())
    assert published.status is CachePublicationStatus.PUBLISHED
    assert published.receipt_sha256 == expected_receipt_hash
    assert published.artifact_path == _entry_path(home, build_input) / "bin" / build_input.command
    assert not (home / "cache").exists()

    entry = _entry_path(home, build_input)
    receipt = entry / cache_posix.RECEIPT_FILENAME
    artifact = entry / build_input.artifact_path
    expected_modes = {
        home / "builds": 0o700,
        home / "builds" / "go-v1": 0o700,
        entry: 0o500,
        entry / "bin": 0o500,
        receipt: 0o400,
        artifact: 0o500,
    }
    for path, expected_mode in expected_modes.items():
        assert stat.S_IMODE(path.lstat().st_mode) == expected_mode

    hit = store.inspect(
        CacheExpectation(
            input=build_input,
            receipt_sha256=expected_receipt_hash,
        )
    )
    assert hit.status is CacheEntryStatus.HIT
    assert hit.dry_run_outcome == "cache-hit"
    assert hit.receipt_bytes == publication.receipt_bytes
    assert hit.receipt_sha256 == expected_receipt_hash
    assert hit.artifact_path == artifact
    assert artifact.read_bytes() == b"verified executable"

    reused = store.publish(publication, guard=_HeldGuard())
    assert reused.status is CachePublicationStatus.REUSED_WINNER
    assert reused.artifact_path == artifact


def test_untrusted_boundary_is_rejected_before_receipt_parsing(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    home, store = _new_store(tmp_path)
    build_input = _build_input()
    publication, receipt_hash = _publication(tmp_path, build_input, b"artifact")
    store.publish(publication, guard=_HeldGuard())
    (home / "builds").chmod(0o777)

    def parsing_is_forbidden(*args: object, **kwargs: object) -> object:
        del args, kwargs
        raise AssertionError("candidate receipt parsed before boundary validation")

    monkeypatch.setattr(cache_posix._metadata, "verify_receipt", parsing_is_forbidden)
    before = _tree_state(home)
    result = store.inspect(
        CacheExpectation(input=build_input, receipt_sha256=receipt_hash)
    )
    assert result.status is CacheEntryStatus.UNTRUSTED_PROVENANCE
    assert result.dry_run_outcome == "would-rebuild-untrusted-cache"
    assert _tree_state(home) == before


@pytest.mark.parametrize(
    "case",
    [
        "effective_uid",
        "home_group_writable",
        "manager_home_symlink",
        "builds_symlink",
        "entry_symlink",
        "entry_owner_writable",
        "receipt_owner_writable",
        "artifact_not_executable",
        "receipt_hard_link",
        "artifact_hard_link",
        "receipt_symlink",
        "artifact_symlink",
        "bin_symlink",
        "receipt_fifo",
    ],
)
def test_posix_boundary_modes_types_links_and_no_follow_fail_closed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    case: str,
) -> None:
    home, store = _new_store(tmp_path)
    build_input = _build_input()
    publication, receipt_hash = _publication(tmp_path, build_input, b"artifact")
    store.publish(publication, guard=_HeldGuard())

    entry = _entry_path(home, build_input)
    bin_dir = entry / "bin"
    receipt = entry / cache_posix.RECEIPT_FILENAME
    artifact = entry / build_input.artifact_path
    if case == "effective_uid":
        monkeypatch.setattr(cache_posix, "_effective_uid", lambda: os.geteuid() + 1)
    elif case == "home_group_writable":
        home.chmod(0o770)
    elif case == "manager_home_symlink":
        moved = tmp_path / "moved-manager-home"
        home.rename(moved)
        home.symlink_to(moved, target_is_directory=True)
    elif case == "builds_symlink":
        moved = home / "moved-builds"
        (home / "builds").rename(moved)
        (home / "builds").symlink_to(moved, target_is_directory=True)
    elif case == "entry_symlink":
        moved = home / "moved-entry"
        entry.chmod(0o700)
        entry.rename(moved)
        moved.chmod(0o500)
        entry.symlink_to(moved, target_is_directory=True)
    elif case == "entry_owner_writable":
        entry.chmod(0o700)
    elif case == "receipt_owner_writable":
        receipt.chmod(0o600)
    elif case == "artifact_not_executable":
        artifact.chmod(0o400)
    elif case == "receipt_hard_link":
        os.link(receipt, home / "receipt-hard-link")
    elif case == "artifact_hard_link":
        os.link(artifact, home / "artifact-hard-link")
    elif case == "receipt_symlink":
        external = home / "external-receipt"
        external.write_bytes(publication.receipt_bytes)
        entry.chmod(0o700)
        receipt.unlink()
        receipt.symlink_to(external)
        entry.chmod(0o500)
    elif case == "artifact_symlink":
        external = home / "external-artifact"
        external.write_bytes(b"artifact")
        bin_dir.chmod(0o700)
        artifact.unlink()
        artifact.symlink_to(external)
        bin_dir.chmod(0o500)
    elif case == "bin_symlink":
        moved = home / "moved-bin"
        entry.chmod(0o700)
        bin_dir.chmod(0o700)
        bin_dir.rename(moved)
        bin_dir.symlink_to(moved, target_is_directory=True)
        entry.chmod(0o500)
    elif case == "receipt_fifo":
        entry.chmod(0o700)
        receipt.unlink()
        os.mkfifo(receipt, 0o400)
        entry.chmod(0o500)
    else:
        raise AssertionError(f"unhandled case: {case}")

    result = store.inspect(
        CacheExpectation(input=build_input, receipt_sha256=receipt_hash)
    )
    assert result.status is CacheEntryStatus.UNTRUSTED_PROVENANCE
    assert result.dry_run_outcome == "would-rebuild-untrusted-cache"
    assert result.artifact_path is None


@pytest.mark.parametrize(
    "case",
    [
        "noncanonical_receipt",
        "wrong_artifact_path",
        "artifact_size",
        "artifact_hash",
        "missing_receipt",
        "unexpected_entry",
        "recorded_receipt_hash",
    ],
)
def test_canonical_receipt_derived_path_hash_size_and_exact_contents(
    tmp_path: Path,
    case: str,
) -> None:
    home, store = _new_store(tmp_path)
    build_input = _build_input()
    publication, receipt_hash = _publication(tmp_path, build_input, b"artifact")
    store.publish(publication, guard=_HeldGuard())

    entry = _entry_path(home, build_input)
    receipt = entry / cache_posix.RECEIPT_FILENAME
    artifact = entry / build_input.artifact_path
    expected_hash = receipt_hash
    if case == "noncanonical_receipt":
        _replace_immutable_file(receipt, publication.receipt_bytes + b"\n", 0o400)
    elif case == "wrong_artifact_path":
        payload = protocol_json.loads_canonical(publication.receipt_bytes)
        assert isinstance(payload, dict)
        artifact_record = payload["artifact"]
        assert isinstance(artifact_record, dict)
        artifact_record["path"] = "bin/not-golden-tool"
        _replace_immutable_file(receipt, protocol_json.canonical_bytes(payload), 0o400)
    elif case == "artifact_size":
        _replace_immutable_file(artifact, b"artifact-extra", 0o500)
    elif case == "artifact_hash":
        _replace_immutable_file(artifact, b"ArtifacT", 0o500)
    elif case == "missing_receipt":
        entry.chmod(0o700)
        receipt.unlink()
        entry.chmod(0o500)
    elif case == "unexpected_entry":
        entry.chmod(0o700)
        _write_immutable(entry / "unexpected", b"x", 0o400)
        entry.chmod(0o500)
    elif case == "recorded_receipt_hash":
        expected_hash = "sha256:" + "0" * 64
    else:
        raise AssertionError(f"unhandled case: {case}")

    before = _tree_state(home)
    result = store.inspect(
        CacheExpectation(input=build_input, receipt_sha256=expected_hash)
    )
    assert result.status is CacheEntryStatus.CORRUPT
    assert result.dry_run_outcome == "corrupt"
    assert result.artifact_path is None
    assert _tree_state(home) == before


def test_lookup_binds_the_complete_expected_input(tmp_path: Path) -> None:
    home, store = _new_store(tmp_path)
    original = _build_input()
    publication, _ = _publication(tmp_path, original, b"artifact")
    store.publish(publication, guard=_HeldGuard())

    changed = replace(original, build_root="other-build")
    copied_candidate = _entry_path(home, changed)
    shutil.copytree(_entry_path(home, original), copied_candidate)
    copied_candidate.chmod(0o500)
    (copied_candidate / "bin").chmod(0o500)
    (copied_candidate / cache_posix.RECEIPT_FILENAME).chmod(0o400)
    (copied_candidate / changed.artifact_path).chmod(0o500)

    result = store.inspect(CacheExpectation(input=changed))
    assert result.status is CacheEntryStatus.CORRUPT
    assert "input" in result.reason or "key" in result.reason


@pytest.mark.parametrize("candidate_mode", [0o700, 0o550, 0o000])
def test_self_consistent_untrusted_candidate_is_read_only_then_rebuilt_fresh(
    tmp_path: Path,
    candidate_mode: int,
) -> None:
    home, store = _new_store(tmp_path)
    build_input = _build_input()
    publication, receipt_hash = _publication(
        tmp_path,
        build_input,
        b"attacker-chosen-artifact",
    )

    entry = _entry_path(home, build_input)
    bin_dir = entry / "bin"
    bin_dir.mkdir(parents=True, mode=0o700)
    (home / "builds").chmod(0o700)
    (home / "builds" / "go-v1").chmod(0o700)
    _write_immutable(entry / cache_posix.RECEIPT_FILENAME, publication.receipt_bytes, 0o400)
    _write_immutable(entry / build_input.artifact_path, b"attacker-chosen-artifact", 0o500)
    bin_dir.chmod(0o500)
    entry.chmod(candidate_mode)

    before = _tree_state(home)
    dry_run = store.inspect(
        CacheExpectation(input=build_input, receipt_sha256=receipt_hash)
    )
    assert dry_run.status is CacheEntryStatus.UNTRUSTED_PROVENANCE
    assert dry_run.dry_run_outcome == "would-rebuild-untrusted-cache"
    assert _tree_state(home) == before

    rebuilt = store.publish(publication, guard=_HeldGuard())
    assert rebuilt.status is CachePublicationStatus.PUBLISHED
    hit = store.inspect(
        CacheExpectation(input=build_input, receipt_sha256=receipt_hash)
    )
    assert hit.status is CacheEntryStatus.HIT
    assert stat.S_IMODE(_entry_path(home, build_input).stat().st_mode) == 0o500
    quarantine = home / cache_posix.QUARANTINE_ROOT_NAME
    assert quarantine.is_dir()
    assert any(quarantine.iterdir())


def test_corrupt_candidate_is_read_only_then_rebuilt_from_verified_stage(
    tmp_path: Path,
) -> None:
    home, store = _new_store(tmp_path)
    build_input = _build_input()
    publication, receipt_hash = _publication(tmp_path, build_input, b"artifact")
    store.publish(publication, guard=_HeldGuard())
    artifact = _entry_path(home, build_input) / build_input.artifact_path
    _replace_immutable_file(artifact, b"corrupt!", 0o500)

    before = _tree_state(home)
    dry_run = store.inspect(
        CacheExpectation(input=build_input, receipt_sha256=receipt_hash)
    )
    assert dry_run.status is CacheEntryStatus.CORRUPT
    assert dry_run.dry_run_outcome == "corrupt"
    assert _tree_state(home) == before

    rebuilt = store.publish(publication, guard=_HeldGuard())
    assert rebuilt.status is CachePublicationStatus.PUBLISHED
    hit = store.inspect(
        CacheExpectation(input=build_input, receipt_sha256=receipt_hash)
    )
    assert hit.status is CacheEntryStatus.HIT
    assert hit.artifact_path is not None
    assert hit.artifact_path.read_bytes() == b"artifact"
    assert any((home / cache_posix.QUARANTINE_ROOT_NAME).iterdir())


def test_publication_requires_guard_and_invalid_inputs_leave_no_cache_state(
    tmp_path: Path,
) -> None:
    home, store = _new_store(tmp_path)
    build_input = _build_input()
    publication, _ = _publication(tmp_path, build_input, b"artifact")

    for guard in (
        cast(CacheMutationGuard, None),
        _ReleasedGuard(),
    ):
        before = _tree_state(home)
        with pytest.raises(BuildCacheError):
            store.publish(publication, guard=guard)
        assert _tree_state(home) == before

    source_link = tmp_path / "artifact-link"
    source_link.symlink_to(publication.artifact_source)
    hard_link = tmp_path / "artifact-hard-link"
    os.link(publication.artifact_source, hard_link)
    wrong_size = tmp_path / "artifact-wrong-size"
    wrong_size.write_bytes(b"artifact-extra")
    wrong_size.chmod(0o700)
    wrong_hash = tmp_path / "artifact-wrong-hash"
    wrong_hash.write_bytes(b"ARTIFACT")
    wrong_hash.chmod(0o700)
    invalid = (
        replace(publication, receipt_bytes=publication.receipt_bytes + b"\n"),
        replace(publication, receipt_bytes=b"x" * ((1 << 20) + 1)),
        replace(publication, artifact_source=source_link),
        replace(publication, artifact_source=hard_link),
        replace(publication, artifact_source=wrong_size),
        replace(publication, artifact_source=wrong_hash),
    )
    for candidate in invalid:
        before = _tree_state(home)
        with pytest.raises(BuildCacheError):
            store.publish(candidate, guard=_HeldGuard())
        assert _tree_state(home) == before
        assert store.inspect(CacheExpectation(input=build_input)).status is CacheEntryStatus.MISS


def test_atomic_identical_concurrent_winners_discard_losers(tmp_path: Path) -> None:
    home, store = _new_store(tmp_path)
    build_input = _build_input()
    publication, receipt_hash = _publication(tmp_path, build_input, b"same artifact")
    publishers = 8
    barrier = threading.Barrier(publishers)
    stores = [PosixBuildCache(home) for _ in range(publishers)]

    def publish(candidate_store: PosixBuildCache) -> CachePublicationStatus:
        barrier.wait()
        return candidate_store.publish(publication, guard=_HeldGuard()).status

    with ThreadPoolExecutor(max_workers=publishers) as executor:
        statuses = list(executor.map(publish, stores))

    assert statuses.count(CachePublicationStatus.PUBLISHED) == 1
    assert statuses.count(CachePublicationStatus.REUSED_WINNER) == publishers - 1
    hit = store.inspect(
        CacheExpectation(input=build_input, receipt_sha256=receipt_hash)
    )
    assert hit.status is CacheEntryStatus.HIT


def test_atomic_different_concurrent_winner_is_conflict(tmp_path: Path) -> None:
    home, store = _new_store(tmp_path)
    build_input = _build_input()
    first, _ = _publication(tmp_path, build_input, b"first artifact")
    second, _ = _publication(tmp_path, build_input, b"second artifact")
    barrier = threading.Barrier(2)
    stores = (PosixBuildCache(home), PosixBuildCache(home))

    def publish(args: tuple[PosixBuildCache, CachePublication]) -> object:
        candidate_store, candidate = args
        barrier.wait()
        try:
            return candidate_store.publish(candidate, guard=_HeldGuard())
        except CacheConflictError as exc:
            return exc

    with ThreadPoolExecutor(max_workers=2) as executor:
        outcomes = list(executor.map(publish, zip(stores, (first, second), strict=True)))

    conflicts = [value for value in outcomes if isinstance(value, CacheConflictError)]
    successes = [value for value in outcomes if not isinstance(value, CacheConflictError)]
    assert len(conflicts) == 1
    assert len(successes) == 1
    assert conflicts[0].cache_key == cache_key(build_input)
    hit = store.inspect(CacheExpectation(input=build_input))
    assert hit.status is CacheEntryStatus.HIT
    assert hit.artifact_path is not None
    assert hit.artifact_path.read_bytes() in {b"first artifact", b"second artifact"}


def test_publish_revalidates_input_and_stages_outside_live_namespace(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    home, store = _new_store(tmp_path)
    build_input = _build_input()
    publication, _ = _publication(tmp_path, build_input, b"artifact")
    verification_count = 0
    original_verify = cache_posix._metadata.verify_receipt
    original_rename = cache_posix._rename_noreplace

    def verify(*args: object, **kwargs: object) -> object:
        nonlocal verification_count
        verification_count += 1
        return original_verify(*args, **kwargs)

    def rename_noreplace(
        source_dir_fd: int,
        source_name: str,
        destination_dir_fd: int,
        destination_name: str,
    ) -> None:
        assert verification_count >= 2
        assert os.path.samestat(
            os.fstat(source_dir_fd),
            (home / cache_posix.STAGING_ROOT_NAME).stat(),
        )
        assert os.path.samestat(
            os.fstat(destination_dir_fd),
            (home / "builds" / "go-v1").stat(),
        )
        assert source_name.startswith("entry-")
        assert destination_name == cache_key(build_input).removeprefix("sha256:")
        original_rename(
            source_dir_fd,
            source_name,
            destination_dir_fd,
            destination_name,
        )

    monkeypatch.setattr(cache_posix._metadata, "verify_receipt", verify)
    monkeypatch.setattr(cache_posix, "_rename_noreplace", rename_noreplace)
    result = store.publish(publication, guard=_HeldGuard())

    assert result.status is CachePublicationStatus.PUBLISHED
    assert verification_count >= 3
    assert list((home / cache_posix.STAGING_ROOT_NAME).iterdir()) == []


def test_locked_quarantine_can_move_immutable_entry_for_later_gc(tmp_path: Path) -> None:
    home, store = _new_store(tmp_path)
    build_input = _build_input()
    publication, _ = _publication(tmp_path, build_input, b"artifact")
    store.publish(publication, guard=_HeldGuard())
    key = cache_key(build_input)

    with pytest.raises(BuildCacheError):
        store.quarantine(key, guard=_ReleasedGuard())
    moved = store.quarantine(key, guard=_HeldGuard())
    assert moved is not None
    assert moved.parent == home / cache_posix.QUARANTINE_ROOT_NAME
    assert moved.exists()
    assert store.inspect(CacheExpectation(input=build_input)).status is CacheEntryStatus.MISS
    assert store.quarantine(key, guard=_HeldGuard()) is None
