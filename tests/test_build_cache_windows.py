from __future__ import annotations

import ctypes
import hashlib
import os
import platform
import subprocess
import threading
from collections.abc import Callable, Iterator
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace
from typing import cast

import pytest

from csk.builds import cache_windows
from csk.builds.cache import (
    BuildCacheError,
    CacheConflictError,
    CacheEntryStatus,
    CacheExpectation,
    CachePublication,
    CachePublicationStatus,
    cache_for_manager_home,
    make_publication_source_private,
)
from csk.builds.cache_windows import WindowsBuildCache
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

_WINDOWS_ONLY = pytest.mark.skipif(
    os.name != "nt",
    reason="Windows cache test",
)


class _HeldGuard:
    def assert_held(self) -> None:
        pass


class _ReleasedGuard:
    def assert_held(self) -> None:
        raise RuntimeError("released")


def _build_input(command: str = "golden-tool") -> GoBuildInput:
    machine = platform.machine().lower()
    if machine in {"arm64", "aarch64"}:
        goarch = "arm64"
        tuning = {"GOARM64": "v8.0"}
    else:
        goarch = "amd64"
        tuning = {"GOAMD64": "v1"}
    return GoBuildInput(
        build_source=BuildSourceIdentity(
            algorithm="curator-build-source-v1",
            content_sha256="sha256:" + "b" * 64,
        ),
        build_root="build",
        command=command,
        source_dir=f"build/cmd/{command}",
        target=NativeTarget(goos="windows", goarch=goarch, tuning=tuning),
        toolchain=ToolchainIdentity(
            algorithm=TOOLCHAIN_ALGORITHM,
            content_sha256="sha256:" + "c" * 64,
            go_relpath=GO_RELPATH,
            go_version=f"go version go1.26.1 windows/{goarch}",
        ),
    )


def _protect(path: Path, profile: cache_windows._SecurityProfile) -> None:
    with cache_windows._open_raw_handle(
        path,
        desired_access=(
            cache_windows._READ_CONTROL
            | cache_windows._WRITE_DAC
            | cache_windows._FILE_READ_ATTRIBUTES
        ),
    ) as handle:
        cache_windows._apply_profile_dacl(handle, profile)
    with cache_windows._open_raw_handle(
        path,
        desired_access=(
            cache_windows._READ_CONTROL
            | cache_windows._FILE_READ_ATTRIBUTES
            | cache_windows._FILE_WRITE_ATTRIBUTES
        ),
    ) as handle:
        cache_windows._set_readonly(handle, False)
    with cache_windows._open_raw_handle(
        path,
        desired_access=cache_windows._FILE_ALL_ACCESS,
    ) as handle:
        cache_windows._apply_security_profile(handle, profile)


def _age_entry(path: Path, timestamp: float) -> None:
    _protect(path, cache_windows._MUTABLE_DIRECTORY)
    os.utime(path, (timestamp, timestamp))
    _protect(path, cache_windows._SEALED_ENTRY)


def _new_store(tmp_path: Path) -> tuple[Path, WindowsBuildCache]:
    home = tmp_path / ".cocoaskills"
    home.mkdir()
    _protect(home, cache_windows._MUTABLE_DIRECTORY)
    return home, WindowsBuildCache(home)


def _publication(
    tmp_path: Path,
    build_input: GoBuildInput,
    artifact_bytes: bytes,
) -> tuple[CachePublication, str]:
    digest = hashlib.sha256(artifact_bytes).hexdigest()
    source_root = tmp_path / "private-builds"
    source_root.mkdir(exist_ok=True)
    _protect(source_root, cache_windows._MUTABLE_DIRECTORY)
    source = source_root / f"{build_input.command}-{digest[:12]}.exe"
    source.write_bytes(artifact_bytes)
    _protect(source, cache_windows._MUTABLE_FILE)
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
    return (
        home
        / "builds"
        / "go-v1"
        / cache_key(build_input).removeprefix("sha256:")
    )


def _tree_state(root: Path) -> tuple[tuple[str, str, bytes], ...]:
    records: list[tuple[str, str, bytes]] = []
    for current, directories, files in os.walk(root, followlinks=False):
        current_path = Path(current)
        for name in sorted(directories):
            path = current_path / name
            relative = path.relative_to(root).as_posix()
            records.append((relative, "directory", b""))
        for name in sorted(files):
            path = current_path / name
            relative = path.relative_to(root).as_posix()
            records.append((relative, "file", path.read_bytes()))
    return tuple(sorted(records))


def _make_cleanup_mutable(root: Path) -> None:
    if os.name != "nt" or not root.exists():
        return
    paths: list[Path] = [root]
    for current, directories, files in os.walk(root, followlinks=False):
        current_path = Path(current)
        paths.extend(current_path / name for name in directories)
        paths.extend(current_path / name for name in files)
    for path in paths:
        try:
            profile = (
                cache_windows._MUTABLE_DIRECTORY
                if path.is_dir()
                else cache_windows._MUTABLE_FILE
            )
            _protect(path, profile)
        except (OSError, RuntimeError, cache_windows._UntrustedState):
            pass


def _replace_sealed_file(
    path: Path,
    raw: bytes,
    profile: cache_windows._SecurityProfile,
) -> None:
    _protect(path, cache_windows._MUTABLE_FILE)
    path.write_bytes(raw)
    _protect(path, profile)


@pytest.fixture(autouse=True)
def _restore_windows_test_permissions(tmp_path: Path) -> Iterator[None]:
    yield
    _make_cleanup_mutable(tmp_path)


def test_windows_backend_module_is_import_safe_on_every_host(tmp_path: Path) -> None:
    backend = WindowsBuildCache(tmp_path)
    result = backend.inspect(CacheExpectation(input=_build_input()))
    if os.name == "nt":
        assert result.status in {
            CacheEntryStatus.MISS,
            CacheEntryStatus.UNTRUSTED_PROVENANCE,
        }
    else:
        assert result.status is CacheEntryStatus.UNSUPPORTED


def test_handle_bound_rename_uses_native_relative_no_replace_contract(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    observed: dict[str, object] = {}

    class NativeApi:
        def NtSetInformationFile(
            self,
            source: int,
            _io_status: object,
            buffer: object,
            length: int,
            information_class: int,
        ) -> int:
            info = ctypes.cast(
                buffer,
                ctypes.POINTER(cache_windows._FileRenameInformation),
            ).contents
            observed.update(
                source=source,
                root=info.root_directory,
                replace=info.replace_if_exists,
                name_length=info.file_name_length,
                name=ctypes.string_at(
                    ctypes.addressof(info)
                    + cache_windows._FileRenameInformation.file_name.offset,
                    info.file_name_length,
                ),
                length=length,
                information_class=information_class,
            )
            return 0

        def RtlNtStatusToDosError(self, _status: int) -> int:
            raise AssertionError("successful rename must not translate an error")

    identity = object()
    destination_path = Path("C:/held-quarantine")
    rename_root = SimpleNamespace(
        value=303,
        path=destination_path,
        identity=identity,
        final_path=r"\\?\C:\held-quarantine",
    )

    class HandleContext:
        def __enter__(self) -> object:
            return rename_root

        def __exit__(self, *_args: object) -> None:
            return None

    def open_rename_root(
        path: Path,
        *,
        desired_access: int,
        **_kwargs: object,
    ) -> HandleContext:
        observed.update(root_path=path, root_access=desired_access)
        return HandleContext()

    api = SimpleNamespace(ntdll=NativeApi())
    source = cast(cache_windows._Handle, SimpleNamespace(value=101))
    destination = cast(
        cache_windows._Handle,
        SimpleNamespace(
            value=202,
            path=destination_path,
            identity=identity,
            final_path=r"\\?\C:\held-quarantine",
        ),
    )
    monkeypatch.setattr(cache_windows, "_api", lambda: api)
    monkeypatch.setattr(cache_windows, "_open_raw_handle", open_rename_root)
    monkeypatch.setattr(
        cache_windows,
        "_revalidate_handle",
        lambda handle, *_args, **_kwargs: handle,
    )

    cache_windows._move_handle_no_replace(
        source,
        destination,
        "gc-entry-a1b2",
    )

    expected_name = "gc-entry-a1b2".encode("utf-16-le")
    assert observed == {
        "source": 101,
        "root": 303,
        "root_path": destination_path,
        "root_access": (
            cache_windows._FILE_EXECUTE
            | cache_windows._FILE_READ_ATTRIBUTES
        ),
        "replace": 0,
        "name_length": len(expected_name),
        "name": expected_name,
        "length": (
            ctypes.sizeof(cache_windows._FileRenameInformation)
            + len(expected_name)
        ),
        "information_class": cache_windows._FILE_RENAME_INFORMATION_CLASS,
    }


@_WINDOWS_ONLY
def test_windows_manager_makes_its_own_build_artifact_publishable(
    tmp_path: Path,
) -> None:
    """A compiler writes the artifact; Windows does not make it manager-owned.

    New objects take the process token's owner, which is the Administrators
    group for an elevated administrator, and inherit the parent DACL, so the
    manager has to stamp its own build output before publication accepts it.
    """
    home, store = _new_store(tmp_path)
    build_input = _build_input()
    artifact_bytes = b"compiled windows executable"
    operation_root = tmp_path / "csk-build-operation"
    operation_root.mkdir()
    source = operation_root / "artifact-golden-tool.exe"
    source.write_bytes(artifact_bytes)

    make_publication_source_private(source)

    with cache_windows._open_raw_handle(
        source,
        desired_access=(
            cache_windows._READ_CONTROL | cache_windows._FILE_READ_ATTRIBUTES
        ),
    ) as handle:
        snapshot = cache_windows._security_snapshot(handle)
    assert snapshot.owner_sid == cache_windows._current_user_sid()
    assert snapshot.dacl_present
    assert snapshot.dacl_protected

    digest = hashlib.sha256(artifact_bytes).hexdigest()
    raw = canonical_receipt_bytes(
        build_receipt(
            build_input,
            BuildArtifact(
                path=build_input.artifact_path,
                sha256=f"sha256:{digest}",
                size=len(artifact_bytes),
            ),
        )
    )
    published = store.publish(
        CachePublication(
            input=build_input,
            receipt_bytes=raw,
            artifact_source=source,
        ),
        guard=_HeldGuard(),
    )
    assert published.status is CachePublicationStatus.PUBLISHED
    assert published.receipt_sha256 == receipt_sha256(raw)


@_WINDOWS_ONLY
def test_windows_layout_hit_immutability_reuse_and_read_only_lookup(
    tmp_path: Path,
) -> None:
    home, store = _new_store(tmp_path)
    build_input = _build_input()
    publication, expected_receipt_hash = _publication(
        tmp_path,
        build_input,
        b"verified windows executable",
    )

    before = _tree_state(home)
    miss = store.inspect(CacheExpectation(input=build_input))
    assert miss.status is CacheEntryStatus.MISS
    assert miss.dry_run_outcome == "would-preflight-and-build"
    assert _tree_state(home) == before

    published = store.publish(publication, guard=_HeldGuard())
    assert published.status is CachePublicationStatus.PUBLISHED
    assert published.receipt_sha256 == expected_receipt_hash
    assert isinstance(cache_for_manager_home(home), WindowsBuildCache)

    entry = _entry_path(home, build_input)
    receipt = entry / cache_windows.RECEIPT_FILENAME
    artifact = entry / Path(*build_input.artifact_path.split("/"))
    assert artifact.read_bytes() == b"verified windows executable"
    with pytest.raises(PermissionError):
        artifact.write_bytes(b"mutation")

    state_before_hit = _tree_state(home)
    hit = store.inspect(
        CacheExpectation(
            input=build_input,
            receipt_sha256=expected_receipt_hash,
        )
    )
    assert hit.status is CacheEntryStatus.HIT
    assert hit.reusable
    assert hit.dry_run_outcome == "cache-hit"
    assert hit.receipt_bytes == publication.receipt_bytes
    assert hit.artifact_path == artifact
    assert _tree_state(home) == state_before_hit

    reused = store.publish(publication, guard=_HeldGuard())
    assert reused.status is CachePublicationStatus.REUSED_WINNER
    assert reused.artifact_path == artifact
    assert list((home / cache_windows.STAGING_ROOT_NAME).iterdir()) == []

    for path, profile in (
        (home / "builds", cache_windows._MUTABLE_DIRECTORY),
        (home / "builds" / "go-v1", cache_windows._MUTABLE_DIRECTORY),
        (entry, cache_windows._SEALED_ENTRY),
        (entry / "bin", cache_windows._SEALED_DIRECTORY),
        (receipt, cache_windows._SEALED_RECEIPT),
        (artifact, cache_windows._SEALED_ARTIFACT),
    ):
        with cache_windows._open_raw_handle(
            path,
            desired_access=(
                cache_windows._READ_CONTROL
                | cache_windows._FILE_READ_ATTRIBUTES
            ),
        ) as handle:
            cache_windows._validate_security_profile(
                handle,
                profile,
                str(path),
            )


@_WINDOWS_ONLY
def test_windows_dacl_drift_and_hard_links_never_admit_candidate_bytes(
    tmp_path: Path,
) -> None:
    home, store = _new_store(tmp_path)
    build_input = _build_input()
    publication, _ = _publication(tmp_path, build_input, b"candidate")
    store.publish(publication, guard=_HeldGuard())
    entry = _entry_path(home, build_input)
    artifact = entry / Path(*build_input.artifact_path.split("/"))

    _protect(artifact, cache_windows._MUTABLE_FILE)
    drifted = store.inspect(CacheExpectation(input=build_input))
    assert drifted.status is CacheEntryStatus.UNTRUSTED_PROVENANCE
    assert drifted.receipt_bytes is None
    assert drifted.artifact_path is None

    _protect(artifact, cache_windows._SEALED_ARTIFACT)
    receipt = entry / cache_windows.RECEIPT_FILENAME
    os.link(receipt, home / "receipt-hard-link")
    linked = store.inspect(CacheExpectation(input=build_input))
    assert linked.status is CacheEntryStatus.UNTRUSTED_PROVENANCE
    assert linked.receipt_bytes is None
    assert linked.artifact_path is None

    _protect(home / "receipt-hard-link", cache_windows._MUTABLE_FILE)
    os.unlink(home / "receipt-hard-link")
    _protect(receipt, cache_windows._SEALED_RECEIPT)
    os.link(artifact, home / "artifact-hard-link.exe")
    artifact_linked = store.inspect(CacheExpectation(input=build_input))
    assert artifact_linked.status is CacheEntryStatus.UNTRUSTED_PROVENANCE
    assert artifact_linked.receipt_bytes is None
    assert artifact_linked.artifact_path is None


@pytest.mark.parametrize("target", ["receipt", "artifact"])
@_WINDOWS_ONLY
def test_windows_late_lookup_hard_links_fail_final_validation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    target: str,
) -> None:
    home, store = _new_store(tmp_path)
    build_input = _build_input()
    publication, _ = _publication(tmp_path, build_input, b"candidate")
    store.publish(publication, guard=_HeldGuard())
    late_link = home / f"late-{target}-hard-link"

    if target == "receipt":
        original_read = cache_windows._read_bounded_handle

        def add_link_after_read(
            handle: cache_windows._Handle,
            limit: int,
            label: str,
        ) -> bytes:
            raw = original_read(handle, limit, label)
            if label == "cache receipt" and not late_link.exists():
                os.link(handle.path, late_link)
            return raw

        monkeypatch.setattr(
            cache_windows,
            "_read_bounded_handle",
            add_link_after_read,
        )
    else:
        original_hash = cache_windows._hash_handle

        def add_link_after_hash(
            handle: cache_windows._Handle,
            *,
            expected_size: int,
            label: str,
            error_factory: Callable[[str], Exception],
        ) -> tuple[str, int]:
            result = original_hash(
                handle,
                expected_size=expected_size,
                label=label,
                error_factory=error_factory,
            )
            if label == "cache artifact" and not late_link.exists():
                os.link(handle.path, late_link)
            return result

        monkeypatch.setattr(cache_windows, "_hash_handle", add_link_after_hash)

    result = store.inspect(CacheExpectation(input=build_input))

    assert late_link.exists()
    assert result.status is CacheEntryStatus.UNTRUSTED_PROVENANCE
    assert result.receipt is None
    assert result.receipt_bytes is None
    assert result.artifact_path is None


@pytest.mark.parametrize(
    "case",
    [
        "receipt_hash",
        "receipt_bytes",
        "artifact_hash",
        "artifact_size",
        "artifact_path",
    ],
)
@_WINDOWS_ONLY
def test_windows_lookup_binds_exact_receipt_path_hash_and_size(
    tmp_path: Path,
    case: str,
) -> None:
    home, store = _new_store(tmp_path)
    build_input = _build_input()
    publication, expected_receipt_hash = _publication(
        tmp_path,
        build_input,
        b"candidate bytes",
    )
    store.publish(publication, guard=_HeldGuard())
    entry = _entry_path(home, build_input)
    receipt = entry / cache_windows.RECEIPT_FILENAME
    bin_path = entry / "bin"
    artifact = entry / Path(*build_input.artifact_path.split("/"))
    expectation = CacheExpectation(input=build_input)

    if case == "receipt_hash":
        expectation = CacheExpectation(
            input=build_input,
            receipt_sha256="sha256:" + "0" * 64,
        )
        assert expected_receipt_hash != expectation.receipt_sha256
    elif case == "receipt_bytes":
        _replace_sealed_file(
            receipt,
            publication.receipt_bytes + b" ",
            cache_windows._SEALED_RECEIPT,
        )
    elif case == "artifact_hash":
        _replace_sealed_file(
            artifact,
            b"candidate byteS",
            cache_windows._SEALED_ARTIFACT,
        )
    elif case == "artifact_size":
        _replace_sealed_file(
            artifact,
            b"short",
            cache_windows._SEALED_ARTIFACT,
        )
    else:
        _protect(bin_path, cache_windows._MUTABLE_DIRECTORY)
        os.replace(artifact, bin_path / "unexpected.exe")
        _protect(bin_path / "unexpected.exe", cache_windows._SEALED_ARTIFACT)
        _protect(bin_path, cache_windows._SEALED_DIRECTORY)

    result = store.inspect(expectation)
    assert result.status is CacheEntryStatus.CORRUPT
    assert result.receipt is None
    assert result.receipt_bytes is None
    assert result.artifact_path is None


@_WINDOWS_ONLY
def test_windows_manager_home_permissions_and_owner_drift_fail_closed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    home, store = _new_store(tmp_path)
    build_input = _build_input()
    subprocess.run(
        [
            "icacls",
            str(home),
            "/grant",
            "*S-1-1-0:(OI)(CI)F",
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    result = store.inspect(CacheExpectation(input=build_input))
    assert result.status is CacheEntryStatus.UNTRUSTED_PROVENANCE
    publication, _ = _publication(tmp_path, build_input, b"candidate")
    with pytest.raises(BuildCacheError, match="cache_boundary_untrusted"):
        store.publish(publication, guard=_HeldGuard())

    _protect(home, cache_windows._MUTABLE_DIRECTORY)
    snapshot = cache_windows._SecuritySnapshot(
        owner_sid=cache_windows._ADMINISTRATORS_SID,
        dacl_present=True,
        dacl_protected=True,
        aces=(),
    )
    monkeypatch.setattr(cache_windows, "_security_snapshot", lambda _handle: snapshot)
    with cache_windows._open_raw_handle(  # noqa: SIM117
        home,
        desired_access=(
            cache_windows._READ_CONTROL
            | cache_windows._FILE_READ_ATTRIBUTES
        ),
    ) as handle:
        with pytest.raises(
            cache_windows._UntrustedState,
            match="owner does not match",
        ):
            cache_windows._validate_manager_home_security(handle)
    monkeypatch.undo()


@pytest.mark.parametrize(
    "inheritance_flags",
    [
        "(OI)(CI)(IO)",
        "(OI)(IO)",
        "(CI)(IO)",
    ],
)
@_WINDOWS_ONLY
def test_windows_manager_home_inheritable_untrusted_aces_fail_closed(
    tmp_path: Path,
    inheritance_flags: str,
) -> None:
    home, store = _new_store(tmp_path)
    subprocess.run(
        [
            "icacls",
            str(home),
            "/grant",
            f"*S-1-1-0:{inheritance_flags}F",
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    build_input = _build_input()

    inspection = store.inspect(CacheExpectation(input=build_input))
    publication, _ = _publication(tmp_path, build_input, b"candidate")

    assert inspection.status is CacheEntryStatus.UNTRUSTED_PROVENANCE
    assert inspection.receipt is None
    assert inspection.receipt_bytes is None
    assert inspection.artifact_path is None
    with pytest.raises(BuildCacheError, match="cache_boundary_untrusted"):
        store.publish(publication, guard=_HeldGuard())
    assert not (home / cache_windows.LIVE_ROOT_NAME).exists()
    assert not (home / cache_windows.STAGING_ROOT_NAME).exists()
    assert not (home / cache_windows.QUARANTINE_ROOT_NAME).exists()


def test_windows_manager_home_inherit_only_manager_ace_is_not_effective(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manager_sid = "S-1-5-21-1000"
    snapshot = cache_windows._SecuritySnapshot(
        owner_sid=manager_sid,
        dacl_present=True,
        dacl_protected=False,
        aces=(
            cache_windows._Ace(
                ace_type=cache_windows._ACCESS_ALLOWED_ACE_TYPE,
                flags=(
                    cache_windows._OBJECT_INHERIT_ACE
                    | cache_windows._CONTAINER_INHERIT_ACE
                    | cache_windows._INHERIT_ONLY_ACE
                ),
                mask=cache_windows._MANAGER_HOME_REQUIRED_ACCESS,
                sid=manager_sid,
            ),
        ),
    )
    monkeypatch.setattr(cache_windows, "_current_user_sid", lambda: manager_sid)
    monkeypatch.setattr(
        cache_windows,
        "_security_snapshot",
        lambda _handle: snapshot,
    )
    fake = cast(cache_windows._Handle, object())

    with pytest.raises(
        cache_windows._UntrustedState,
        match="does not grant the manager principal required control",
    ):
        cache_windows._validate_manager_home_security(fake)


@_WINDOWS_ONLY
def test_windows_special_boundary_file_fails_closed(tmp_path: Path) -> None:
    home, store = _new_store(tmp_path)
    builds = home / "builds"
    builds.write_bytes(b"not a directory")
    _protect(builds, cache_windows._MUTABLE_FILE)
    build_input = _build_input()

    result = store.inspect(CacheExpectation(input=build_input))
    assert result.status is CacheEntryStatus.UNTRUSTED_PROVENANCE
    assert result.receipt_bytes is None
    assert result.artifact_path is None

    publication, _ = _publication(tmp_path, build_input, b"candidate")
    published = store.publish(publication, guard=_HeldGuard())
    assert published.status is CachePublicationStatus.PUBLISHED
    assert builds.is_dir()
    assert (
        store.inspect(CacheExpectation(input=build_input)).status
        is CacheEntryStatus.HIT
    )
    quarantined = list((home / cache_windows.QUARANTINE_ROOT_NAME).iterdir())
    assert any(
        path.is_file() and path.read_bytes() == b"not a directory"
        for path in quarantined
    )


def _create_junction(link: Path, target: Path) -> None:
    result = subprocess.run(
        ["cmd", "/d", "/c", "mklink", "/J", str(link), str(target)],
        check=False,
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        raise AssertionError(
            f"cannot create Windows test junction: {result.stdout}{result.stderr}"
        )


@_WINDOWS_ONLY
def test_windows_reparse_escape_and_unverifiable_boundary_fail_closed(
    tmp_path: Path,
) -> None:
    home, store = _new_store(tmp_path)
    outside = tmp_path / "outside"
    outside.mkdir()
    _protect(outside, cache_windows._MUTABLE_DIRECTORY)
    _create_junction(home / "builds", outside)

    build_input = _build_input()
    inspected = store.inspect(CacheExpectation(input=build_input))
    assert inspected.status is CacheEntryStatus.UNTRUSTED_PROVENANCE
    assert inspected.receipt_bytes is None
    assert inspected.artifact_path is None

    publication, _ = _publication(tmp_path, build_input, b"candidate")
    with pytest.raises(BuildCacheError, match="cache_boundary_untrusted"):
        store.publish(publication, guard=_HeldGuard())
    assert list(outside.iterdir()) == []


@_WINDOWS_ONLY
def test_windows_publication_guard_source_links_and_receipt_binding(
    tmp_path: Path,
) -> None:
    home, store = _new_store(tmp_path)
    build_input = _build_input()
    publication, _ = _publication(tmp_path, build_input, b"candidate")

    with pytest.raises(BuildCacheError, match="cache_lock_required"):
        store.publish(publication, guard=_ReleasedGuard())
    assert _tree_state(home) == ()

    hard_link = publication.artifact_source.with_name("artifact-hard-link.exe")
    os.link(publication.artifact_source, hard_link)
    linked_publication = CachePublication(
        input=publication.input,
        receipt_bytes=publication.receipt_bytes,
        artifact_source=hard_link,
    )
    with pytest.raises(BuildCacheError, match="singly linked"):
        store.publish(linked_publication, guard=_HeldGuard())
    assert _tree_state(home) == ()

    other_input = _build_input("other-tool")
    mismatched = CachePublication(
        input=other_input,
        receipt_bytes=publication.receipt_bytes,
        artifact_source=publication.artifact_source,
    )
    with pytest.raises(BuildCacheError, match="cache_publication_invalid"):
        store.publish(mismatched, guard=_HeldGuard())
    assert _tree_state(home) == ()


@_WINDOWS_ONLY
def test_windows_late_publication_source_hard_link_is_rejected(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    home, store = _new_store(tmp_path)
    build_input = _build_input()
    publication, _ = _publication(tmp_path, build_input, b"candidate")
    late_link = publication.artifact_source.with_name(
        "late-source-hard-link.exe"
    )
    original_validate = cache_windows._validate_source_unchanged

    def add_link_before_final_validation(
        source: cache_windows._Handle,
    ) -> None:
        if not late_link.exists():
            os.link(source.path, late_link)
        original_validate(source)

    monkeypatch.setattr(
        cache_windows,
        "_validate_source_unchanged",
        add_link_before_final_validation,
    )

    with pytest.raises(BuildCacheError, match="cache_publication_invalid"):
        store.publish(publication, guard=_HeldGuard())

    assert late_link.exists()
    assert not _entry_path(home, build_input).exists()
    inspection = store.inspect(CacheExpectation(input=build_input))
    assert inspection.status is CacheEntryStatus.MISS
    assert inspection.receipt is None
    assert inspection.receipt_bytes is None
    assert inspection.artifact_path is None


@_WINDOWS_ONLY
def test_windows_atomic_identical_winners_and_different_bytes_conflict(
    tmp_path: Path,
) -> None:
    home, first = _new_store(tmp_path)
    second = WindowsBuildCache(home)
    build_input = _build_input()
    publication, _ = _publication(tmp_path, build_input, b"same bytes")

    with ThreadPoolExecutor(max_workers=2) as executor:
        futures = [
            executor.submit(backend.publish, publication, guard=_HeldGuard())
            for backend in (first, second)
        ]
        statuses = sorted(future.result().status.value for future in futures)
    assert statuses == ["published", "reused-winner"]

    different, _ = _publication(tmp_path, build_input, b"different bytes")
    with pytest.raises(CacheConflictError):
        second.publish(different, guard=_HeldGuard())
    hit = first.inspect(CacheExpectation(input=build_input))
    assert hit.status is CacheEntryStatus.HIT
    assert hit.artifact_path is not None
    assert hit.artifact_path.read_bytes() == b"same bytes"


@_WINDOWS_ONLY
def test_windows_containment_race_returns_no_candidate_bytes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    home, first = _new_store(tmp_path)
    build_input = _build_input()
    original, _ = _publication(tmp_path, build_input, b"original bytes")
    first.publish(original, guard=_HeldGuard())

    receipt_opened = threading.Event()
    release_reader = threading.Event()
    original_read = cache_windows._read_bounded_handle

    def paused_read(
        handle: cache_windows._Handle,
        limit: int,
        label: str,
    ) -> bytes:
        raw = original_read(handle, limit, label)
        if label == "cache receipt" and not receipt_opened.is_set():
            receipt_opened.set()
            if not release_reader.wait(timeout=10):
                raise AssertionError("timed out waiting to resume cache lookup")
        return raw

    monkeypatch.setattr(cache_windows, "_read_bounded_handle", paused_read)
    with ThreadPoolExecutor(max_workers=1) as executor:
        lookup = executor.submit(first.inspect, CacheExpectation(input=build_input))
        assert receipt_opened.wait(timeout=10)
        entry = _entry_path(home, build_input)
        bin_path = entry / "bin"
        artifact = entry / Path(*build_input.artifact_path.split("/"))
        raced_artifact = home / "raced-artifact.exe"
        _protect(bin_path, cache_windows._MUTABLE_DIRECTORY)
        os.replace(artifact, raced_artifact)
        artifact.write_bytes(b"replacement bytes")
        _protect(artifact, cache_windows._SEALED_ARTIFACT)
        _protect(bin_path, cache_windows._SEALED_DIRECTORY)
        release_reader.set()
        result = lookup.result(timeout=10)

    assert result.status is CacheEntryStatus.UNTRUSTED_PROVENANCE
    assert result.receipt is None
    assert result.receipt_bytes is None
    assert result.artifact_path is None


@_WINDOWS_ONLY
def test_windows_quarantine_moves_entry_outside_live_namespace(
    tmp_path: Path,
) -> None:
    home, store = _new_store(tmp_path)
    build_input = _build_input()
    publication, _ = _publication(tmp_path, build_input, b"candidate")
    store.publish(publication, guard=_HeldGuard())

    moved = store.quarantine(cache_key(build_input), guard=_HeldGuard())
    assert moved is not None
    assert moved.parent == home / cache_windows.QUARANTINE_ROOT_NAME
    assert (
        moved / Path(*build_input.artifact_path.split("/"))
    ).read_bytes() == b"candidate"
    assert not _entry_path(home, build_input).exists()
    assert (
        store.inspect(CacheExpectation(input=build_input)).status
        is CacheEntryStatus.MISS
    )


@_WINDOWS_ONLY
def test_windows_gc_removes_only_old_unreferenced_entries(
    tmp_path: Path,
) -> None:
    home, store = _new_store(tmp_path)
    old_input = _build_input("old-tool")
    young_input = _build_input("young-tool")
    old_publication, _ = _publication(tmp_path, old_input, b"old artifact")
    young_publication, _ = _publication(tmp_path, young_input, b"young artifact")
    store.publish(old_publication, guard=_HeldGuard())
    store.publish(young_publication, guard=_HeldGuard())
    old_entry = _entry_path(home, old_input)
    young_entry = _entry_path(home, young_input)
    _age_entry(old_entry, 1.0)

    result = store.collect(set(), older_than=100.0, guard=_HeldGuard())

    assert result.removed == 1
    assert not old_entry.exists()
    assert young_entry.exists()
    assert (
        store.inspect(CacheExpectation(input=young_input)).status
        is CacheEntryStatus.HIT
    )


@_WINDOWS_ONLY
def test_windows_gc_entry_exchange_never_retires_the_replacement(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    home, store = _new_store(tmp_path)
    build_input = _build_input()
    old_publication, _ = _publication(tmp_path, build_input, b"old artifact")
    store.publish(old_publication, guard=_HeldGuard())
    entry = _entry_path(home, build_input)
    _age_entry(entry, 1.0)

    replacement_root = tmp_path / "replacement"
    replacement_root.mkdir()
    replacement_home, replacement_store = _new_store(replacement_root)
    young_publication, _ = _publication(
        replacement_root,
        build_input,
        b"young artifact",
    )
    replacement_store.publish(young_publication, guard=_HeldGuard())
    replacement = _entry_path(replacement_home, build_input)
    detached_old = home / "detached-old-entry"
    original_move = cache_windows._move_aside
    exchanged = False

    def exchange_before_move(
        source_parent: cache_windows._Handle,
        source_name: str,
        destination_parent: cache_windows._Handle,
        prefix: str,
        *,
        missing_ok: bool,
        expected_state: cache_windows._ObjectState | None = None,
    ) -> str | None:
        nonlocal exchanged
        if prefix.startswith("gc-entry-") and not exchanged:
            exchanged = True
            _protect(entry, cache_windows._MUTABLE_DIRECTORY)
            os.replace(entry, detached_old)
            _protect(detached_old, cache_windows._SEALED_ENTRY)
            _protect(replacement, cache_windows._MUTABLE_DIRECTORY)
            os.replace(replacement, entry)
            _protect(entry, cache_windows._SEALED_ENTRY)
        return original_move(
            source_parent,
            source_name,
            destination_parent,
            prefix,
            missing_ok=missing_ok,
            expected_state=expected_state,
        )

    monkeypatch.setattr(cache_windows, "_move_aside", exchange_before_move)

    result = store.collect(set(), older_than=100.0, guard=_HeldGuard())

    assert exchanged
    assert result.removed == 0
    assert any("uncertain entry" in warning for warning in result.warnings)
    assert detached_old.exists()
    assert entry.exists()
    inspection = store.inspect(CacheExpectation(input=build_input))
    assert inspection.status is CacheEntryStatus.HIT
    assert inspection.artifact_path is not None
    assert inspection.artifact_path.read_bytes() == b"young artifact"


@_WINDOWS_ONLY
def test_windows_gc_root_exchange_never_retires_the_replacement(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    home, store = _new_store(tmp_path)
    build_input = _build_input()
    old_publication, _ = _publication(tmp_path, build_input, b"old artifact")
    store.publish(old_publication, guard=_HeldGuard())
    entry = _entry_path(home, build_input)
    _age_entry(entry, 1.0)

    replacement_root = tmp_path / "replacement-root"
    replacement_root.mkdir()
    replacement_home, replacement_store = _new_store(replacement_root)
    young_publication, _ = _publication(
        replacement_root,
        build_input,
        b"young artifact",
    )
    replacement_store.publish(young_publication, guard=_HeldGuard())
    driver = home / cache_windows.LIVE_ROOT_NAME / "go-v1"
    replacement_driver = (
        replacement_home / cache_windows.LIVE_ROOT_NAME / "go-v1"
    )
    detached_driver = home / "detached-old-driver"
    original_move = cache_windows._move_aside
    exchanged = False

    def exchange_root_before_move(
        source_parent: cache_windows._Handle,
        source_name: str,
        destination_parent: cache_windows._Handle,
        prefix: str,
        *,
        missing_ok: bool,
        expected_state: cache_windows._ObjectState | None = None,
    ) -> str | None:
        nonlocal exchanged
        if prefix.startswith("gc-entry-") and not exchanged:
            exchanged = True
            cache_windows._move_no_replace(driver, detached_driver)
            cache_windows._move_no_replace(replacement_driver, driver)
        return original_move(
            source_parent,
            source_name,
            destination_parent,
            prefix,
            missing_ok=missing_ok,
            expected_state=expected_state,
        )

    monkeypatch.setattr(cache_windows, "_move_aside", exchange_root_before_move)

    result = store.collect(set(), older_than=100.0, guard=_HeldGuard())

    assert exchanged
    assert result.removed == 0
    assert result.warnings
    assert (detached_driver / entry.name).exists()
    inspection = store.inspect(CacheExpectation(input=build_input))
    assert inspection.status is CacheEntryStatus.HIT
    assert inspection.artifact_path is not None
    assert inspection.artifact_path.read_bytes() == b"young artifact"


@_WINDOWS_ONLY
def test_windows_gc_destination_root_exchange_retains_exact_entry(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    home, store = _new_store(tmp_path)
    build_input = _build_input()
    publication, _ = _publication(tmp_path, build_input, b"old artifact")
    store.publish(publication, guard=_HeldGuard())
    entry = _entry_path(home, build_input)
    _age_entry(entry, 1.0)

    quarantine = home / cache_windows.QUARANTINE_ROOT_NAME
    detached_quarantine = home / "detached-quarantine"
    replacement_quarantine = home / "replacement-quarantine"
    replacement_quarantine.mkdir()
    _protect(replacement_quarantine, cache_windows._MUTABLE_DIRECTORY)
    real_api = cache_windows._api()
    exchanged = False

    class NativeApi:
        def NtSetInformationFile(self, *args: object) -> int:
            nonlocal exchanged
            if not exchanged:
                exchanged = True
                cache_windows._move_no_replace(quarantine, detached_quarantine)
                cache_windows._move_no_replace(replacement_quarantine, quarantine)
            return int(real_api.ntdll.NtSetInformationFile(*args))

        def RtlNtStatusToDosError(self, status: int) -> int:
            return int(real_api.ntdll.RtlNtStatusToDosError(status))

    api = SimpleNamespace(
        kernel32=real_api.kernel32,
        advapi32=real_api.advapi32,
        ntdll=NativeApi(),
    )
    monkeypatch.setattr(cache_windows, "_api", lambda: api)

    result = store.collect(set(), older_than=100.0, guard=_HeldGuard())

    assert exchanged
    assert result.removed == 0
    assert result.warnings
    assert quarantine.is_dir()
    assert list(quarantine.iterdir()) == []
    retained = list(detached_quarantine.iterdir())
    assert len(retained) == 1
    assert retained[0].name.startswith("gc-entry-")
    artifact = retained[0] / Path(*build_input.artifact_path.split("/"))
    assert artifact.read_bytes() == b"old artifact"


@_WINDOWS_ONLY
def test_windows_security_profile_rejects_synthetic_owner_drift(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    snapshot = cache_windows._SecuritySnapshot(
        owner_sid=cache_windows._ADMINISTRATORS_SID,
        dacl_present=True,
        dacl_protected=True,
        aces=(),
    )
    monkeypatch.setattr(cache_windows, "_security_snapshot", lambda _handle: snapshot)
    fake = cast(cache_windows._Handle, object())
    with pytest.raises(
        cache_windows._UntrustedState,
        match="owner does not match",
    ):
        cache_windows._validate_security_profile(
            fake,
            cache_windows._SEALED_ARTIFACT,
            "cache artifact",
        )
    monkeypatch.undo()
