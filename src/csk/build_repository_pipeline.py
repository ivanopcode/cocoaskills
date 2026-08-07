from __future__ import annotations

import hashlib
import os
import shutil
import stat
import tempfile
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass, replace
from enum import StrEnum
from pathlib import Path
from typing import NoReturn, Protocol

from . import protocol_json
from .build_repository import BuildRepository, BuildTarget, DESCRIPTOR_NAME, load_skill_build
from .builds import go_v1
from .builds import source as build_source
from .builds import toolchain
from .git_admission import (
    GitAdmissionError,
    OBJECT_SEMANTICS_INVALID,
    REF_MOVED,
    SOURCE_UNAVAILABLE,
    Snapshot,
    SnapshotFile,
)


DESCRIPTOR_INVALID = "build_repository_descriptor_invalid"
AUDIT_BLOCKED = "build_repository_audit_blocked"
RECEIPT_INVALID = "build_repository_receipt_invalid"
ARTIFACT_INVALID = "build_repository_artifact_invalid"
PROTECTED_BOUNDARY_UNTRUSTED = "build_repository_protected_boundary_untrusted"
UNVERIFIED_OFFLINE = "build_repository_unverified_offline"

_MAX_METADATA = 4 << 20
_MAX_ARTIFACT = 1 << 30


class ExternalBuildError(RuntimeError):
    def __init__(self, code: str, detail: str) -> None:
        super().__init__(f"{code}: {detail}")
        self.code = code
        self.detail = detail


class Operation(StrEnum):
    INSTALL = "install"
    DRY_RUN = "dry-run"
    REPAIR = "repair"
    AUDIT = "audit"
    SYNTAX = "syntax"


@dataclass(frozen=True)
class DeclaredState:
    repository: str
    identity: str
    transport: str
    object_format: str
    commit: str
    tag: str | None = None


@dataclass(frozen=True)
class SubstitutionState:
    type: str
    ref_kind: str | None = None
    ref_value: str | None = None


@dataclass(frozen=True)
class EffectiveState:
    identity_kind: str
    identity: str
    transport: str | None
    object_format: str
    commit: str
    substituted: bool = False
    substitution: SubstitutionState | None = None


@dataclass(frozen=True)
class AuditSubject:
    declared: DeclaredState
    effective: EffectiveState
    build_source: str
    descriptor_target: str
    snapshot_root: Path
    tag_verified: bool


@dataclass(frozen=True)
class CompilerIdentity:
    content_sha256: str
    go_version: str
    go_relpath: str
    goos: str
    goarch: str
    tuning: Mapping[str, str]


class GoCompiler(Protocol):
    @property
    def identity(self) -> CompilerIdentity: ...

    def compile(self, root: Path, source_dir: str, command: str) -> bytes: ...


class ExistingGoV1Session:
    """Use one already established local go-v1 toolchain session unchanged."""

    def __init__(self, session: toolchain.ToolchainSession) -> None:
        self._session = session

    @property
    def identity(self) -> CompilerIdentity:
        return CompilerIdentity(
            content_sha256=self._session.toolchain.content_sha256,
            go_version=self._session.toolchain.go_version,
            go_relpath=self._session.toolchain.go_relpath,
            goos=self._session.target.goos,
            goarch=self._session.target.goarch,
            tuning=self._session.target.tuning,
        )

    def compile(self, root: Path, source_dir: str, command: str) -> bytes:
        with build_source.freeze_snapshot(root) as frozen:
            result = go_v1.build(
                go_v1.BuildRequest(
                    toolchain_session=self._session,
                    source_snapshot=frozen,
                    command_object={
                        "type": "build",
                        "driver": "go-v1",
                        "source_dir": source_dir,
                    },
                    build_root=".",
                    source_dir=source_dir,
                    command=command,
                )
            )
            return result.artifact.staged_path.read_bytes()


@dataclass(frozen=True)
class ArtifactHit:
    artifact: bytes
    receipt: bytes


@dataclass(frozen=True)
class PipelineRequest:
    operation: Operation
    command: str
    target: str
    declared: DeclaredState
    effective: EffectiveState
    acquire: Callable[[], Snapshot]
    audit: Callable[[AuditSubject], None]
    store: DiskProtectedStore | None = None
    compiler: GoCompiler | None = None
    offline_snapshot_key: str | None = None
    trace: Callable[[str], None] | None = None


@dataclass(frozen=True)
class PipelineResult:
    state: str
    build_source: str | None = None
    snapshot_key: str | None = None
    cache_key: str | None = None
    code: str | None = None
    artifact: bytes | None = None
    receipt: bytes | None = None
    subject: AuditSubject | None = None


class DiskProtectedStore:
    """Fail-closed immutable external snapshot and receipt-v2 store."""

    def __init__(
        self,
        root: Path,
        *,
        identity_proof: Callable[[os.stat_result, bool], bool] | None = None,
    ) -> None:
        root = Path(root)
        if not root.is_absolute() or Path(os.path.normpath(root)) != root:
            raise ExternalBuildError(PROTECTED_BOUNDARY_UNTRUSTED, "store root must be a clean absolute path")
        self.root = root
        self._identity_proof = identity_proof

    def store_snapshot(self, key: str, snapshot: Snapshot) -> None:
        self._prepare(mutate=True)
        parent = self.root / "snapshots"
        self._protected_dir(parent, create=True)
        final = parent / _key_component(key)
        if final.exists():
            try:
                stored = self.load_snapshot(key, mutate=True)
            except ExternalBuildError:
                # load_snapshot quarantines malformed protected entries. An
                # online admission already has independently validated source,
                # so it can safely republish the exact snapshot.
                pass
            else:
                if stored.digest == snapshot.digest and stored.commit == snapshot.commit:
                    return
                self._quarantine(final)
        stage = Path(tempfile.mkdtemp(prefix=".stage-", dir=parent))
        try:
            snapshot.materialize(stage / "files")
            metadata = protocol_json.canonical_bytes(
                {
                    "key": key,
                    "object_format": snapshot.object_format,
                    "commit": snapshot.commit,
                    "digest": snapshot.digest,
                    "files": [
                        {"path": item.path, "executable": item.executable}
                        for item in snapshot.files
                    ],
                }
            )
            (stage / "snapshot.json").write_bytes(metadata)
            _seal_tree(stage, seal_root=False)
            os.replace(stage, final)
            _seal_root(final)
        finally:
            shutil.rmtree(stage, ignore_errors=True)

    def load_snapshot(self, key: str, *, mutate: bool) -> Snapshot:
        self._prepare(mutate=mutate)
        entry = self.root / "snapshots" / _key_component(key)
        try:
            self._protected_dir(entry.parent, create=False)
            self._protected_dir(entry, create=False)
            metadata = self._read_protected(entry / "snapshot.json", _MAX_METADATA)
            raw = protocol_json.loads_canonical(metadata)
            if protocol_json.canonical_bytes(raw) != metadata or not isinstance(raw, dict):
                raise ValueError("snapshot metadata is not exact canonical JSON")
            files = _read_tree(entry / "files", self._proof, protected=True)
            expected_files = raw.get("files")
            if expected_files != [
                {"path": item.path, "executable": item.executable} for item in files
            ]:
                raise ValueError("snapshot file set differs")
            canonical = _frame_snapshot(files)
            digest = "sha256:" + hashlib.sha256(canonical).hexdigest()
            if raw.get("key") != key or raw.get("digest") != digest:
                raise ValueError("snapshot key or digest differs")
            return Snapshot(
                object_format=_text(raw.get("object_format")),
                commit=_text(raw.get("commit")),
                files=files,
                canonical_bytes=canonical,
                digest=digest,
            )
        except (OSError, ValueError, GitAdmissionError, protocol_json.ProtocolJSONError) as exc:
            self._corrupt(entry, OBJECT_SEMANTICS_INVALID, mutate, exc)

    def lookup_artifact(
        self, key: str, expected_input: Mapping[str, object], *, mutate: bool
    ) -> ArtifactHit | None:
        corruption_code = RECEIPT_INVALID
        entry = self.root / "artifacts" / _key_component(key)
        try:
            self._prepare(mutate=mutate)
            if not entry.exists():
                return None
            self._protected_dir(entry.parent, create=False)
            self._protected_dir(entry, create=False)
            receipt = self._read_protected(entry / "receipt.json", _MAX_METADATA)
            raw = protocol_json.loads_canonical(receipt)
            if protocol_json.canonical_bytes(raw) != receipt or not isinstance(raw, dict):
                raise ValueError("receipt is not exact canonical JSON")
            if set(raw) != {"schema_version", "cache_key", "input", "artifact"}:
                raise ValueError("receipt has an open or incomplete shape")
            if raw.get("schema_version") != 2 or raw.get("cache_key") != key:
                raise ValueError("receipt identity differs")
            if protocol_json.canonical_bytes(raw.get("input")) != protocol_json.canonical_bytes(expected_input):
                raise ValueError("receipt input differs")
            corruption_code = ARTIFACT_INVALID
            artifact = self._read_protected(entry / "artifact", _MAX_ARTIFACT)
            metadata = raw.get("artifact")
            path = _artifact_path(expected_input)
            if not isinstance(metadata, dict) or metadata != {
                "path": path,
                "sha256": "sha256:" + hashlib.sha256(artifact).hexdigest(),
                "size": len(artifact),
            }:
                raise ValueError("artifact metadata differs")
            return ArtifactHit(artifact=artifact, receipt=receipt)
        except FileNotFoundError as exc:
            if not entry.exists():
                return None
            self._corrupt(entry, corruption_code, mutate, exc)
        except (OSError, ValueError, protocol_json.ProtocolJSONError) as exc:
            self._corrupt(entry, corruption_code, mutate, exc)

    def inspect_artifact(self, key: str) -> ArtifactHit | None:
        """Read-only validation of a self-authenticating receipt-v2 entry."""

        entry = self.root / "artifacts" / _key_component(key)
        try:
            self._prepare(mutate=False)
            if not entry.exists():
                return None
            self._protected_dir(entry.parent, create=False)
            self._protected_dir(entry, create=False)
            receipt = self._read_protected(entry / "receipt.json", _MAX_METADATA)
            raw = protocol_json.loads_canonical(receipt)
            if protocol_json.canonical_bytes(raw) != receipt or not isinstance(raw, dict):
                raise ValueError("receipt is not exact canonical JSON")
            input_value = raw.get("input")
            if not isinstance(input_value, dict) or _digest_json(input_value) != key:
                raise ValueError("receipt input does not derive its cache key")
            return self.lookup_artifact(key, input_value, mutate=False)
        except FileNotFoundError as exc:
            if not entry.exists():
                return None
            self._corrupt(entry, RECEIPT_INVALID, False, exc)
        except (OSError, ValueError, protocol_json.ProtocolJSONError) as exc:
            self._corrupt(entry, RECEIPT_INVALID, False, exc)

    def store_artifact(
        self, key: str, input_value: Mapping[str, object], artifact: bytes
    ) -> bytes:
        self._prepare(mutate=True)
        parent = self.root / "artifacts"
        self._protected_dir(parent, create=True)
        final = parent / _key_component(key)
        if final.exists():
            hit = self.lookup_artifact(key, input_value, mutate=True)
            if hit is not None:
                return hit.receipt
        stage = Path(tempfile.mkdtemp(prefix=".stage-", dir=parent))
        try:
            artifact_file = stage / "artifact"
            artifact_file.write_bytes(artifact)
            if os.name != "nt":
                artifact_file.chmod(0o500)
            receipt = protocol_json.canonical_bytes(
                {
                    "schema_version": 2,
                    "cache_key": key,
                    "input": dict(input_value),
                    "artifact": {
                        "path": _artifact_path(input_value),
                        "sha256": "sha256:" + hashlib.sha256(artifact).hexdigest(),
                        "size": len(artifact),
                    },
                }
            )
            (stage / "receipt.json").write_bytes(receipt)
            _seal_tree(stage, seal_root=False)
            os.replace(stage, final)
            _seal_root(final)
            return receipt
        finally:
            shutil.rmtree(stage, ignore_errors=True)

    def _proof(self, info: os.stat_result, directory: bool) -> bool:
        if self._identity_proof is not None:
            return self._identity_proof(info, directory)
        if os.name == "nt":
            # Windows ownership/DACL/reparse validation is path based and is
            # performed by _protected_dir, _read_protected, and _read_tree.
            return directory or info.st_nlink == 1
        if os.name != "posix":
            return False
        return info.st_uid == os.geteuid() and (directory or info.st_nlink == 1)

    def _protected_dir(self, path: Path, *, create: bool) -> None:
        created = create and not path.exists()
        if create:
            path.mkdir(mode=0o700, parents=True, exist_ok=True)
        if os.name == "nt":
            if created:
                _secure_windows_path(path, directory=True, sealed=False)
            _validate_windows_path(path, directory=True)
            return
        info = path.lstat()
        if (
            not stat.S_ISDIR(info.st_mode)
            or stat.S_ISLNK(info.st_mode)
            or stat.S_IMODE(info.st_mode) & 0o022
            or not self._proof(info, True)
        ):
            raise ExternalBuildError(PROTECTED_BOUNDARY_UNTRUSTED, "protected directory cannot be proved private")

    def _read_protected(self, path: Path, maximum: int) -> bytes:
        if os.name == "nt":
            _validate_windows_path(path, directory=False)
        info = path.lstat()
        if (
            not stat.S_ISREG(info.st_mode)
            or stat.S_ISLNK(info.st_mode)
            or info.st_size > maximum
            or not self._proof(info, False)
        ):
            raise ValueError("protected file shape is invalid")
        data = path.read_bytes()
        if len(data) > maximum:
            raise ValueError("protected file exceeds limit")
        return data

    def _prepare(self, *, mutate: bool) -> None:
        self._protected_dir(self.root, create=mutate)

    def _quarantine(self, entry: Path) -> None:
        quarantine = self.root / "quarantine"
        self._protected_dir(quarantine, create=True)
        # Published entries are deliberately non-writable.  The caller is on
        # the mutation path and has already proved the protected parent, so
        # reopen the corrupt directory only long enough to move it out of the
        # live namespace (Darwin refuses the directory rename otherwise).
        if os.name == "nt":
            _secure_windows_path(entry, directory=True, sealed=False)
        else:
            entry.chmod(0o700)
        os.replace(entry, quarantine / f"{entry.name}-{time.time_ns()}")

    def _corrupt(
        self, entry: Path, code: str, mutate: bool, cause: BaseException
    ) -> NoReturn:
        if mutate and entry.exists():
            self._quarantine(entry)
        raise ExternalBuildError(code, f"protected entry invalid: {cause}") from cause


def run_pipeline(request: PipelineRequest) -> PipelineResult:
    mutate = request.operation in {Operation.INSTALL, Operation.REPAIR}
    _trace(request, "exact-source-acquisition")
    try:
        snapshot = request.acquire()
    except GitAdmissionError as acquire_error:
        if acquire_error.code != SOURCE_UNAVAILABLE:
            raise
        if request.operation is Operation.SYNTAX:
            return PipelineResult(state="unverified-offline", code=UNVERIFIED_OFFLINE)
        snapshot = None
        if (
            request.declared.tag is None
            and request.offline_snapshot_key is not None
            and request.store is not None
        ):
            try:
                snapshot = request.store.load_snapshot(
                    request.offline_snapshot_key, mutate=mutate
                )
            except ExternalBuildError:
                snapshot = None
        if snapshot is None:
            raise ExternalBuildError(SOURCE_UNAVAILABLE, "exact external source is unavailable") from acquire_error

    assert snapshot is not None

    _trace(request, "raw-object-identity-and-graph-proof")
    _trace(request, "all-blob-lfs-scan")
    with tempfile.TemporaryDirectory(prefix="csk-external-snapshot-") as raw_root:
        root = Path(raw_root)
        snapshot.materialize(root)
        _trace(request, "immutable-snapshot-materialization")
        _trace(request, "whole-snapshot-validation")
        _validate_materialized(root, snapshot)
        _trace(request, "build-source-digest")
        try:
            descriptor = load_skill_build(root)
            target = descriptor.targets[request.target]
            _validate_target(root, target)
        except (OSError, KeyError, ValueError) as exc:
            raise ExternalBuildError(DESCRIPTOR_INVALID, str(exc)) from exc
        effective = request.effective
        if (
            effective.substituted
            and effective.substitution is not None
            and (
                effective.substitution.type == "local-path"
                or effective.substitution.ref_kind != "revision"
            )
        ):
            effective = replace(
                effective,
                object_format=snapshot.object_format,
                commit=snapshot.commit,
            )
        _trace(request, "descriptor-and-target-validation")
        _validate_binding(request.declared, effective, snapshot)
        snap_key = snapshot_key(effective, snapshot.digest)
        subject = AuditSubject(
            declared=request.declared,
            effective=effective,
            build_source=snapshot.digest,
            descriptor_target=request.target,
            snapshot_root=root,
            tag_verified=request.declared.tag is not None and snapshot.tag_verified,
        )
        _trace(request, "independent-external-audit")
        try:
            request.audit(subject)
        except BaseException as exc:
            raise ExternalBuildError(AUDIT_BLOCKED, "independent external repository audit failed") from exc
        _validate_materialized(root, snapshot)
        if request.operation in {Operation.AUDIT, Operation.SYNTAX}:
            return PipelineResult(
                state="audited" if request.operation is Operation.AUDIT else "source-covered",
                build_source=snapshot.digest,
                snapshot_key=snap_key,
                subject=subject,
            )
        if request.store is None or request.compiler is None:
            raise ExternalBuildError(PROTECTED_BOUNDARY_UNTRUSTED, "protected store and Go compiler session are required")
        if mutate:
            request.store.store_snapshot(snap_key, snapshot)
        input_value = receipt_input(request, effective, target, snapshot.digest, request.compiler.identity)
        key = _digest_json(input_value)
        _trace(request, "artifact-cache-lookup")
        cache_error: ExternalBuildError | None = None
        try:
            hit = request.store.lookup_artifact(key, input_value, mutate=mutate)
        except ExternalBuildError as exc:
            cache_error = exc
            hit = None
        if hit is not None:
            _validate_materialized(root, snapshot)
            return PipelineResult("cache-hit", snapshot.digest, snap_key, key, artifact=hit.artifact, receipt=hit.receipt, subject=subject)
        if request.operation is Operation.DRY_RUN:
            return PipelineResult(
                "corrupt" if cache_error else "would-preflight-and-build",
                snapshot.digest,
                snap_key,
                key,
                code=cache_error.code if cache_error else None,
                subject=subject,
            )
        with tempfile.TemporaryDirectory(prefix="csk-external-build-root-") as view_root:
            view = Path(view_root)
            _copy_build_root(root, target, view)
            source_dir = "." if target.source_dir == target.build_root else target.source_dir.removeprefix(target.build_root + "/")
            _trace(request, "compiler")
            artifact = request.compiler.compile(view, source_dir, request.command)
        if not artifact:
            raise ExternalBuildError(ARTIFACT_INVALID, "compiler returned an empty artifact")
        _validate_materialized(root, snapshot)
        receipt = request.store.store_artifact(key, input_value, artifact)
        _trace(request, "receipt-publication")
        return PipelineResult(
            "would-rebuild-untrusted-cache" if cache_error else "would-preflight-and-build",
            snapshot.digest,
            snap_key,
            key,
            code=cache_error.code if cache_error else None,
            artifact=artifact,
            receipt=receipt,
            subject=subject,
        )


def snapshot_key(effective: EffectiveState, digest: str) -> str:
    return _digest_json(
        {
            "identity": {"kind": effective.identity_kind, "value": effective.identity},
            "object_format": effective.object_format,
            "commit": effective.commit,
            "build_source": digest,
        }
    )


def receipt_input(
    request: PipelineRequest,
    effective: EffectiveState,
    target: BuildTarget,
    digest: str,
    compiler: CompilerIdentity,
) -> dict[str, object]:
    declared: dict[str, object] = {
        "identity": {"kind": "network-git", "value": request.declared.identity},
        "transport": request.declared.transport,
        "locked_commit": {
            "object_format": request.declared.object_format,
            "hex": request.declared.commit,
        },
    }
    if request.declared.tag is not None:
        declared["tag"] = request.declared.tag
    selected: dict[str, object] = {
        "identity": {"kind": effective.identity_kind, "value": effective.identity},
        "object_format": effective.object_format,
        "commit": effective.commit,
        "substituted": effective.substituted,
        "build_source": {
            "algorithm": "curator-build-source-v1",
            "content_sha256": digest,
        },
    }
    if effective.transport is not None:
        selected["transport"] = effective.transport
    if effective.substitution is not None:
        substitution: dict[str, object] = {"type": effective.substitution.type}
        if effective.substitution.ref_kind is not None:
            substitution["ref"] = {
                "kind": effective.substitution.ref_kind,
                "value": effective.substitution.ref_value,
            }
        selected["substitution"] = substitution
    return {
        "schema_version": 2,
        "driver": "go-repository-v1",
        "command": request.command,
        "build_root": target.build_root,
        "source_dir": target.source_dir,
        "source": {
            "repository": request.declared.repository,
            "declared": declared,
            "effective": selected,
            "descriptor": {"path": DESCRIPTOR_NAME, "target": request.target},
        },
        "target": {
            "goos": compiler.goos,
            "goarch": compiler.goarch,
            "tuning": dict(compiler.tuning),
        },
        "toolchain": {
            "algorithm": "curator-go-toolchain-v1",
            "content_sha256": compiler.content_sha256,
            "go_version": compiler.go_version,
            "go_relpath": compiler.go_relpath,
        },
        "policy": {
            "module_mode": "vendor",
            "network": "none",
            "workspace": False,
            "cgo": False,
            "compiler_directives": "reject-nonstandard-cgo-import-dynamic-v1",
            "target_mode": "native",
            "link_mode": "internal",
            "libgcc": "none",
            "package_assembly": False,
            "host_objects": False,
            "telemetry": "off-private",
            "execution_policy": "manager-worker-v1",
            "source_kind": "locked-external-git-v1",
        },
    }


def _validate_binding(declared: DeclaredState, effective: EffectiveState, snapshot: Snapshot) -> None:
    if not all((declared.repository, declared.identity, declared.object_format, declared.commit)):
        raise ExternalBuildError("build_repository_identity_invalid", "declared source is incomplete")
    if effective.identity_kind not in {"network-git", "operator-local-git"}:
        raise ExternalBuildError("build_repository_identity_invalid", "effective identity kind is invalid")
    if effective.object_format != snapshot.object_format or effective.commit != snapshot.commit:
        raise ExternalBuildError("build_repository_identity_invalid", "effective source does not bind admitted snapshot")
    if not effective.substituted:
        if (
            effective.substitution is not None
            or effective.identity_kind != "network-git"
            or effective.identity != declared.identity
            or effective.transport != declared.transport
            or effective.object_format != declared.object_format
            or effective.commit != declared.commit
        ):
            raise ExternalBuildError("build_repository_identity_invalid", "declared/effective source mismatch")
        if declared.tag is not None and not snapshot.tag_verified:
            raise ExternalBuildError(REF_MOVED, "declared tag was not proved in this operation")
    elif effective.substitution is None or effective.substitution.type not in {"local-path", "network-git"}:
        raise ExternalBuildError("build_repository_identity_invalid", "substituted source lacks typed state")


def _validate_materialized(root: Path, expected: Snapshot) -> None:
    files = _read_tree(
        root,
        lambda info, directory: directory or info.st_nlink == 1,
        protected=False,
    )
    canonical = _frame_snapshot(files)
    if (
        files != expected.files
        or canonical != expected.canonical_bytes
        or "sha256:" + hashlib.sha256(canonical).hexdigest() != expected.digest
    ):
        raise ExternalBuildError(OBJECT_SEMANTICS_INVALID, "materialized snapshot differs from admitted bytes")


def _validate_target(root: Path, target: BuildTarget) -> None:
    build_root = root if target.build_root == "." else root.joinpath(*target.build_root.split("/"))
    source = root if target.source_dir == "." else root.joinpath(*target.source_dir.split("/"))
    if not build_root.is_dir() or build_root.is_symlink() or not (build_root / "go.mod").is_file():
        raise ValueError("build_root must be a real directory containing go.mod")
    if not source.is_dir() or source.is_symlink():
        raise ValueError("source_dir must be a real directory")


def _copy_build_root(snapshot_root: Path, target: BuildTarget, view: Path) -> None:
    source = snapshot_root if target.build_root == "." else snapshot_root.joinpath(*target.build_root.split("/"))
    shutil.copytree(source, view, dirs_exist_ok=True, symlinks=False)


def _read_tree(
    root: Path,
    proof: Callable[[os.stat_result, bool], bool],
    *,
    protected: bool,
) -> tuple[SnapshotFile, ...]:
    files: list[SnapshotFile] = []
    # Order by the relative POSIX path, which is the identity the admitted
    # snapshot is framed in (git_admission._prove_repository sorts on the UTF-8
    # bytes of that same path, and UTF-8 preserves code point order).  Sorting
    # Path objects instead is not platform-independent: PurePath compares
    # _parts_normcase, so Windows case-folds the components and every flavour
    # orders "foo/bar" before "foo.go" where the framed bytes order "foo.go"
    # first.  Both divergences make the materialized bytes differ from the
    # admitted bytes for the same commit.
    for path in sorted(root.rglob("*"), key=lambda item: item.relative_to(root).as_posix()):
        info = path.lstat()
        directory = stat.S_ISDIR(info.st_mode)
        if os.name == "nt":
            _validate_windows_path(
                path,
                directory=directory,
                protected=protected,
            )
        if stat.S_ISLNK(info.st_mode) or (not directory and not stat.S_ISREG(info.st_mode)) or not proof(info, directory):
            raise ValueError("tree contains an unproved non-regular entry")
        if not directory:
            files.append(
                SnapshotFile(
                    path=path.relative_to(root).as_posix(),
                    content=path.read_bytes(),
                    executable=bool(info.st_mode & stat.S_IXUSR),
                )
            )
    return tuple(files)


def _frame_snapshot(files: tuple[SnapshotFile, ...]) -> bytes:
    framed = bytearray(b"curator-build-source-v1\0")
    for item in files:
        path = item.path.encode("utf-8")
        framed.extend(b"F")
        framed.extend(len(path).to_bytes(8, "big"))
        framed.extend(path)
        framed.extend(len(item.content).to_bytes(8, "big"))
        framed.extend(item.content)
    return bytes(framed)


def _seal_tree(root: Path, *, seal_root: bool = True) -> None:
    if os.name == "nt":
        for path in sorted(root.rglob("*"), reverse=True):
            _secure_windows_path(path, directory=path.is_dir(), sealed=True)
        if seal_root:
            _secure_windows_path(root, directory=True, sealed=True)
        return
    for path in sorted(root.rglob("*"), reverse=True):
        path.chmod(0o500 if path.is_dir() or path.name == "artifact" else 0o400)
    if seal_root:
        root.chmod(0o500)


def _seal_root(root: Path) -> None:
    if os.name == "nt":
        _secure_windows_path(root, directory=True, sealed=True)
    else:
        root.chmod(0o500)


def _secure_windows_path(path: Path, *, directory: bool, sealed: bool) -> None:
    from .builds import cache_windows

    if directory:
        profile = (
            cache_windows._SEALED_ENTRY
            if sealed
            else cache_windows._MUTABLE_DIRECTORY
        )
    elif sealed and path.name == "artifact":
        profile = cache_windows._SEALED_ARTIFACT
    elif sealed:
        profile = cache_windows._SEALED_RECEIPT
    else:
        profile = cache_windows._MUTABLE_FILE
    with cache_windows._open_raw_handle(
        path, desired_access=cache_windows._FILE_ALL_ACCESS
    ) as handle:
        cache_windows._apply_security_profile(handle, profile)


def _validate_windows_path(
    path: Path,
    *,
    directory: bool,
    protected: bool = True,
) -> None:
    from .builds import cache_windows

    profiles = (
        (cache_windows._MUTABLE_DIRECTORY, cache_windows._SEALED_ENTRY)
        if directory
        else (
            cache_windows._SEALED_ARTIFACT,
            cache_windows._SEALED_RECEIPT,
        )
    )
    last: BaseException | None = None
    try:
        with cache_windows._open_raw_handle(
            path,
            desired_access=(
                cache_windows._GENERIC_READ
                | cache_windows._READ_CONTROL
                | cache_windows._FILE_READ_ATTRIBUTES
            ),
        ) as handle:
            if directory != handle.standard.directory:
                raise ValueError("protected object type differs")
            if not directory:
                cache_windows._require_singly_linked_file(
                    handle, "external protected file"
                )
            if not protected:
                return
            for profile in profiles:
                try:
                    cache_windows._validate_security_profile(
                        handle, profile, "external protected object"
                    )
                    return
                except BaseException as exc:  # private backend typed failures
                    last = exc
    except BaseException as exc:
        last = exc
    raise ValueError(f"Windows protected object is untrusted: {last}")


def _key_component(key: str) -> str:
    value = key.removeprefix("sha256:")
    if len(value) != 64 or any(character not in "0123456789abcdef" for character in value):
        raise ValueError("invalid protected key")
    return value


def _digest_json(value: Mapping[str, object]) -> str:
    return "sha256:" + hashlib.sha256(protocol_json.canonical_bytes(value)).hexdigest()


def _artifact_path(input_value: Mapping[str, object]) -> str:
    command = input_value.get("command")
    target = input_value.get("target")
    if not isinstance(command, str) or not command or "/" in command or "\\" in command:
        raise ValueError("receipt command is invalid")
    suffix = ".exe" if isinstance(target, Mapping) and target.get("goos") == "windows" else ""
    return f"bin/{command}{suffix}"


def _text(value: object) -> str:
    if not isinstance(value, str) or not value:
        raise ValueError("metadata string is absent")
    return value


def _trace(request: PipelineRequest, phase: str) -> None:
    if request.trace is not None:
        request.trace(phase)


def declared_state(repository: BuildRepository) -> DeclaredState:
    return DeclaredState(
        repository=repository.name,
        identity=repository.identity,
        transport=repository.transport,
        object_format=repository.locked_commit.object_format,
        commit=repository.locked_commit.hex,
        tag=repository.tag,
    )
