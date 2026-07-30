from __future__ import annotations

import ctypes
import errno
import hashlib
import json
import os
import re
import shutil
import stat
import struct
import sys
import threading
import unicodedata
import uuid
from collections.abc import Callable, Iterator, Mapping
from contextlib import contextmanager
from dataclasses import asdict, dataclass
from functools import partial
from pathlib import Path, PurePosixPath
from typing import Any, Literal, Protocol

from csk.locking import canonical_manager_home_identity

ABSENT_DIGEST = "absent"
JOURNAL_SCHEMA = "csk-install-transaction-v1"
_TRANSACTION_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
_DIGEST_DOMAIN = b"csk-transaction-target-v1\0"
_ENTRY_CONTENT_DOMAIN = b"csk-transaction-entry-content-v1\0"
_STAGING_PREFIX_DOMAIN = b"csk-transaction-staging-prefix-v1\0"
_STAGING_COPY_CHUNK_SIZE = 32 * 1024
_MOVEFILE_REPLACE_EXISTING = 0x00000001
_MOVEFILE_WRITE_THROUGH = 0x00000008
_GENERIC_READ = 0x80000000
_GENERIC_WRITE = 0x40000000
_FILE_SHARE_READ = 0x00000001
_FILE_SHARE_WRITE = 0x00000002
_FILE_SHARE_DELETE = 0x00000004
_OPEN_EXISTING = 3
_FILE_FLAG_OPEN_REPARSE_POINT = 0x00200000
_FILE_FLAG_BACKUP_SEMANTICS = 0x02000000
_ERROR_ACCESS_DENIED = 5
_ERROR_INVALID_HANDLE = 6
_ERROR_FILE_EXISTS = 80
_ERROR_ALREADY_EXISTS = 183

Phase = Literal["preparing", "prepared", "committing", "cleanup", "rolling_back"]
CommitState = Literal["pending", "backed_up", "committed", "rolled_back"]
CleanupRole = Literal["staged", "backup", "rollback"]
CleanupState = Literal["pending", "tombed", "removed"]
TargetKind = Literal["bytes", "entry"]
CleanupEntryKind = Literal["file", "directory", "link"]
StagingEntryKind = Literal["file", "directory", "link"]
FaultHook = Callable[[str, "JournalTarget | None"], None]


class TransactionError(Exception):
    pass


class TransactionCorruptionError(TransactionError):
    pass


class HomeLockWitness(Protocol):
    @property
    def home_identity(self) -> str: ...

    def assert_held(self) -> None: ...


@dataclass(frozen=True)
class MutableTarget:
    target_class: str
    identifier: str
    live_path: Path
    desired_path: Path | None
    expected_preimage_digest: str | None = None
    expected_generation: str | None = None
    generation_path: str | None = None
    kind: TargetKind = "bytes"


@dataclass(frozen=True)
class TransactionPlan:
    transaction_id: str
    project_identity: str
    targets: tuple[MutableTarget, ...]
    generation_digests: Mapping[str, str] | None = None


@dataclass(frozen=True)
class StagingTreeEntry:
    relative_path: str
    kind: StagingEntryKind
    mode: int
    size: int
    digest: str | None
    link_target: str | None = None
    link_is_directory: bool | None = None


@dataclass
class JournalTarget:
    target_class: str
    identifier: str
    kind: TargetKind
    live_path: str
    staged_path: str | None
    staged_source: str | None
    staging_entries: list[StagingTreeEntry]
    backup_path: str
    rollback_path: str
    expected_preimage_digest: str | None
    expected_generation: str | None
    generation_path: str | None
    desired_digest: str
    staging_index: int = 0
    staging_active: bool = False
    staging_created: bool = False
    staging_bytes: int = 0
    staging_prefix_digest: str | None = None
    staging_write_bytes: int = 0
    staging_write_digest: str | None = None
    staging_finalize_index: int = 0
    staging_finalize_active: bool = False
    backup_digest: str | None = None
    state: CommitState = "pending"


@dataclass(frozen=True)
class CleanupTreeEntry:
    relative_path: str
    kind: CleanupEntryKind
    mode: int
    digest: str | None
    link_target: str | None = None


@dataclass
class JournalCleanupSidecar:
    target_index: int
    role: CleanupRole
    path: str
    tomb_path: str
    expected_digest: str
    entries: list[CleanupTreeEntry]
    state: CleanupState
    writable_index: int = 0
    writable_active: bool = False


@dataclass
class Journal:
    schema: str
    transaction_id: str
    project_identity: str
    phase: Phase
    ordered_target_classes: list[str]
    generation_digests: dict[str, str]
    targets: list[JournalTarget]
    cleanup_sidecars: list[JournalCleanupSidecar]


class TransactionEngine:
    """Durable generic replacement transactions under a caller-held home lock."""

    def __init__(self, csk_home: Path, *, fault_hook: FaultHook | None = None):
        self.home = csk_home.expanduser().resolve(strict=False)
        self.journal_root = self.home / "state" / "transactions" / "v1"
        self._fault_hook = fault_hook
        self._mutex = threading.Lock()
        self._active_witness: HomeLockWitness | None = None
        self._active_home_identity: str | None = None

    def prepare(self, lock: HomeLockWitness, plan: TransactionPlan) -> Journal:
        lock.assert_held()
        with self._mutex, self._witness_scope(lock):
            journal, sources = self._build_journal(plan)
            path = self._journal_path(journal.transaction_id)
            if path.exists():
                raise TransactionError(
                    f"transaction already exists: {journal.transaction_id}"
                )
            self._save_journal(journal, create=True)
            try:
                for target_index, (target, source) in enumerate(
                    zip(journal.targets, sources, strict=True)
                ):
                    if source is None:
                        continue
                    assert target.staged_path is not None
                    self._stage_target(journal, target_index, source)
                    if (
                        _target_digest(target, Path(target.staged_path))
                        != target.desired_digest
                    ):
                        raise TransactionCorruptionError(
                            f"staged target changed while preparing: {target.target_class}/{target.identifier}"
                        )
                for target in journal.targets:
                    target.staged_source = None
                    target.staging_entries = []
                    target.staging_index = 0
                    target.staging_finalize_index = 0
                    target.staging_finalize_active = False
                    self._clear_staging_progress(target)
                journal.phase = "prepared"
                self._save_journal(journal)
                self._emit("prepared", None)
                return _clone_journal(journal)
            except Exception:
                self._discard_prepared(journal)
                raise

    def _stage_target(
        self,
        journal: Journal,
        target_index: int,
        source: Path,
    ) -> None:
        target = journal.targets[target_index]
        if target.staged_path is None or target.staged_source != str(source):
            raise TransactionCorruptionError(
                f"staging source is inconsistent: {target.target_class}/{target.identifier}"
            )
        staged = Path(target.staged_path)
        while target.staging_index < len(target.staging_entries):
            entry = target.staging_entries[target.staging_index]
            target.staging_active = True
            target.staging_created = False
            target.staging_bytes = 0
            target.staging_prefix_digest = None
            target.staging_write_bytes = 0
            target.staging_write_digest = None
            self._save_journal(journal)
            self._emit("before_staging_entry_create", target)
            self._create_staging_entry(source, staged, entry)
            target.staging_created = True
            self._save_journal(journal)
            self._emit("staging_entry_created", target)
            if entry.kind == "file":
                self._copy_staging_file(journal, target, source, staged, entry)
            destination = _staging_entry_path(staged, entry.relative_path)
            _validate_staging_entry_modes(
                destination,
                entry,
                {_staging_construction_mode(entry)},
            )
            target.staging_index += 1
            self._clear_staging_progress(target)
            self._save_journal(journal)
            self._emit("staging_entry_completed", target)
        self._finalize_staging_modes(journal, target)

    def _finalize_staging_modes(
        self,
        journal: Journal,
        target: JournalTarget,
    ) -> None:
        if target.staged_path is None:
            raise TransactionCorruptionError(
                f"staging path is missing: {target.target_class}/{target.identifier}"
            )
        staged = Path(target.staged_path)
        order = [
            entry for entry in reversed(target.staging_entries) if entry.kind != "link"
        ]
        while target.staging_finalize_index < len(order):
            entry = order[target.staging_finalize_index]
            destination = _staging_entry_path(staged, entry.relative_path)
            target.staging_finalize_active = True
            self._save_journal(journal)
            self._emit("before_staging_mode_finalize", target)
            _validate_staging_entry_modes(
                destination,
                entry,
                {_staging_construction_mode(entry), entry.mode},
            )
            _set_entry_mode_durably(destination, entry, entry.mode)
            _validate_staging_entry(destination, entry)
            self._emit("staging_mode_finalized", target)
            target.staging_finalize_index += 1
            target.staging_finalize_active = False
            self._save_journal(journal)
            self._emit("staging_mode_finalize_completed", target)

    def _create_staging_entry(
        self,
        source_root: Path,
        staged_root: Path,
        entry: StagingTreeEntry,
    ) -> None:
        self._assert_mutation_witness()
        source = _staging_entry_path(source_root, entry.relative_path)
        destination = _staging_entry_path(staged_root, entry.relative_path)
        _validate_staging_entry(source, entry)
        if _path_exists(destination):
            raise TransactionCorruptionError(
                f"staging entry exists before creation: {destination}"
            )
        _require_safe_staging_parent(destination.parent)
        if entry.kind == "link":
            if entry.link_target is None:
                raise TransactionCorruptionError(
                    f"staging link has no recorded destination: {destination}"
                )
            destination.symlink_to(
                entry.link_target,
                target_is_directory=bool(entry.link_is_directory),
            )
        elif entry.kind == "directory":
            construction_mode = _staging_construction_mode(entry)
            destination.mkdir(mode=construction_mode)
            destination.chmod(construction_mode)
            _sync_directory(destination)
        else:
            fd = os.open(
                destination,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL,
                0o600,
            )
            try:
                destination.chmod(_staging_construction_mode(entry))
                os.fsync(fd)
            finally:
                os.close(fd)
        _sync_directory(destination.parent)
        self._assert_mutation_witness()

    def _copy_staging_file(
        self,
        journal: Journal,
        target: JournalTarget,
        source_root: Path,
        staged_root: Path,
        entry: StagingTreeEntry,
    ) -> None:
        self._assert_mutation_witness()
        source = _staging_entry_path(source_root, entry.relative_path)
        destination = _staging_entry_path(staged_root, entry.relative_path)
        _validate_staging_entry(source, entry)
        prefix = hashlib.sha256(_STAGING_PREFIX_DOMAIN)
        with (
            source.open("rb") as source_handle,
            destination.open("ab", buffering=0) as destination_handle,
        ):
            while True:
                chunk = source_handle.read(_STAGING_COPY_CHUNK_SIZE)
                if not chunk:
                    break
                prefix.update(chunk)
                target.staging_write_bytes = target.staging_bytes + len(chunk)
                target.staging_write_digest = "sha256:" + prefix.hexdigest()
                self._save_journal(journal)
                self._assert_mutation_witness()
                view = memoryview(chunk)
                while view:
                    written = destination_handle.write(view)
                    if written is None or written <= 0:
                        raise OSError("short staging write")
                    view = view[written:]
                os.fsync(destination_handle.fileno())
                self._assert_mutation_witness()
                self._emit("after_staging_chunk_sync", target)
                target.staging_bytes = target.staging_write_bytes
                target.staging_prefix_digest = target.staging_write_digest
                target.staging_write_bytes = 0
                target.staging_write_digest = None
                self._save_journal(journal)
                self._emit("during_staging_copy", target)
        _validate_staging_entry(source, entry)

    @staticmethod
    def _clear_staging_progress(target: JournalTarget) -> None:
        target.staging_active = False
        target.staging_created = False
        target.staging_bytes = 0
        target.staging_prefix_digest = None
        target.staging_write_bytes = 0
        target.staging_write_digest = None

    def commit(self, lock: HomeLockWitness, transaction_id: str) -> None:
        lock.assert_held()
        with self._mutex, self._witness_scope(lock):
            journal = self._load_journal(transaction_id)
            self._resume(journal)

    def recover(self, lock: HomeLockWitness) -> None:
        """Recover every home journal, regardless of the initiating project."""
        lock.assert_held()
        with self._mutex, self._witness_scope(lock):
            for transaction_id in self._journal_ids():
                journal, deleting = self._load_journal_record(transaction_id)
                try:
                    if deleting:
                        self._finish_journal_removal(journal)
                    else:
                        self._resume(journal)
                except Exception as exc:
                    raise TransactionError(
                        f"recover transaction {transaction_id}: {exc}"
                    ) from exc

    def referenced_generation_digests(self, lock: HomeLockWitness) -> dict[str, str]:
        lock.assert_held()
        with self._mutex, self._witness_scope(lock):
            result: dict[str, str] = {}
            for transaction_id in self._journal_ids():
                journal, deleting = self._load_journal_record(transaction_id)
                if deleting:
                    continue
                for key, digest in journal.generation_digests.items():
                    previous = result.setdefault(key, digest)
                    if previous != digest:
                        raise TransactionCorruptionError(
                            f"generation {key!r} has conflicting journal digests"
                        )
            return dict(sorted(result.items(), key=lambda item: _utf8_key(item[0])))

    def _resume(self, journal: Journal) -> None:
        if journal.phase == "preparing":
            self._discard_prepared(journal)
        elif journal.phase in {"prepared", "committing"}:
            self._commit_journal(journal)
        elif journal.phase == "cleanup":
            self._cleanup_committed(journal)
        elif journal.phase == "rolling_back":
            self._rollback(journal)
        else:
            raise TransactionCorruptionError(
                f"unsupported journal phase: {journal.phase}"
            )

    def _commit_journal(self, journal: Journal) -> None:
        if journal.phase == "prepared":
            journal.phase = "committing"
            self._save_journal(journal)
        try:
            for target in journal.targets:
                self._commit_target(journal, target)
        except Exception as cause:
            journal.phase = "rolling_back"
            self._save_journal(journal)
            try:
                self._rollback(journal)
            except Exception as rollback_error:  # noqa: BLE001 - both failures must remain observable
                raise ExceptionGroup(
                    "commit and rollback failed", [cause, rollback_error]
                )
            raise
        journal.phase = "cleanup"
        self._save_journal(journal)
        self._emit("before_cleanup", None)
        self._cleanup_committed(journal)

    def _commit_target(self, journal: Journal, target: JournalTarget) -> None:
        self._assert_mutation_witness()
        live = Path(target.live_path)
        backup = Path(target.backup_path)
        staged = Path(target.staged_path) if target.staged_path is not None else None

        if target.state == "pending":
            backup_digest = _target_digest(target, backup)
            current = _target_digest(target, live)
            if backup_digest != ABSENT_DIGEST:
                if current not in {ABSENT_DIGEST, target.desired_digest}:
                    raise TransactionCorruptionError(
                        f"target has both backup and unknown live bytes: {target.target_class}/{target.identifier}"
                    )
                self._verify_expected_at(target, backup)
                target.backup_digest = backup_digest
                target.state = "backed_up"
                self._save_journal(journal)
            else:
                self._verify_expected_at(target, live)
                if current != ABSENT_DIGEST:
                    self._assert_mutation_witness()
                    _rename_no_replace(live, backup)
                    _sync_tree(backup)
                    self._assert_mutation_witness()
                    captured = _target_digest(target, backup)
                    if captured != current:
                        raise TransactionCorruptionError(
                            f"target changed while backing up: {target.target_class}/{target.identifier}"
                        )
                target.backup_digest = current
                self._emit("after_backup", target)
                target.state = "backed_up"
                self._save_journal(journal)

        if target.state == "backed_up":
            current = _target_digest(target, live)
            if current == target.desired_digest:
                target.state = "committed"
                self._save_journal(journal)
                self._emit("target_committed", target)
                return
            if current != ABSENT_DIGEST:
                raise TransactionCorruptionError(
                    f"unknown bytes appeared before install: {target.target_class}/{target.identifier}"
                )
            if target.desired_digest != ABSENT_DIGEST:
                if (
                    staged is None
                    or _target_digest(target, staged) != target.desired_digest
                ):
                    raise TransactionCorruptionError(
                        f"staged target changed: {target.target_class}/{target.identifier}"
                    )
                self._assert_mutation_witness()
                _rename_no_replace(staged, live)
                _sync_tree(live)
                self._assert_mutation_witness()
            self._emit("after_install", target)
            target.state = "committed"
            self._save_journal(journal)
            self._emit("target_committed", target)
            return

        if target.state == "committed":
            if _target_digest(target, live) != target.desired_digest:
                raise TransactionCorruptionError(
                    f"committed target changed: {target.target_class}/{target.identifier}"
                )
            return
        raise TransactionCorruptionError(
            f"invalid commit state {target.state}: {target.target_class}/{target.identifier}"
        )

    def _rollback(self, journal: Journal) -> None:
        journal.phase = "rolling_back"
        self._save_journal(journal)
        for target in reversed(journal.targets):
            self._rollback_target(journal, target)
        self._remove_journal(journal)

    def _rollback_target(self, journal: Journal, target: JournalTarget) -> None:
        self._assert_mutation_witness()
        live = Path(target.live_path)
        backup = Path(target.backup_path)
        rollback = Path(target.rollback_path)
        backup_digest = _target_digest(target, backup)
        current = _target_digest(target, live)
        rollback_digest = _target_digest(target, rollback)

        if target.state == "pending" and backup_digest == ABSENT_DIGEST:
            return
        if target.state == "pending":
            if current != ABSENT_DIGEST:
                raise TransactionCorruptionError(
                    f"pending target has both backup and live bytes: "
                    f"{target.target_class}/{target.identifier}"
                )
            self._verify_expected_at(target, backup)
            target.backup_digest = backup_digest
            target.state = "backed_up"
            self._save_journal(journal)
        if target.state == "rolled_back":
            return

        expected_backup = target.backup_digest
        if (
            current != ABSENT_DIGEST
            and expected_backup is not None
            and current == expected_backup
            and backup_digest == ABSENT_DIGEST
            and rollback_digest in {target.desired_digest, ABSENT_DIGEST}
        ):
            target.state = "rolled_back"
            self._save_journal(journal)
            return

        if (
            rollback_digest != ABSENT_DIGEST
            and rollback_digest != target.desired_digest
        ):
            raise TransactionCorruptionError(
                f"rollback sidecar contains unknown bytes: {target.target_class}/{target.identifier}"
            )
        if rollback_digest == ABSENT_DIGEST and current != ABSENT_DIGEST:
            if current != target.desired_digest:
                raise TransactionCorruptionError(
                    f"rollback refused unknown current bytes: {target.target_class}/{target.identifier}"
                )
            self._assert_mutation_witness()
            _rename_no_replace(live, rollback)
            self._assert_mutation_witness()
            rollback_digest = _target_digest(target, rollback)
            if rollback_digest != target.desired_digest:
                raise TransactionCorruptionError(
                    f"rollback captured unknown bytes: {target.target_class}/{target.identifier}"
                )
        elif rollback_digest != ABSENT_DIGEST and current != ABSENT_DIGEST:
            raise TransactionCorruptionError(
                f"rollback found both live and rollback bytes: {target.target_class}/{target.identifier}"
            )
        elif (
            current == ABSENT_DIGEST
            and rollback_digest == ABSENT_DIGEST
            and target.state == "committed"
            and target.desired_digest != ABSENT_DIGEST
        ):
            raise TransactionCorruptionError(
                f"committed target disappeared before rollback: {target.target_class}/{target.identifier}"
            )

        if expected_backup is None:
            expected_backup = backup_digest
            target.backup_digest = backup_digest
        if backup_digest != expected_backup:
            raise TransactionCorruptionError(
                f"backup changed before rollback: {target.target_class}/{target.identifier}"
            )
        if backup_digest != ABSENT_DIGEST:
            if _target_digest(target, live) != ABSENT_DIGEST:
                raise TransactionCorruptionError(
                    f"rollback refused to overwrite live bytes: {target.target_class}/{target.identifier}"
                )
            self._assert_mutation_witness()
            _rename_no_replace(backup, live)
            _sync_tree(live)
            self._assert_mutation_witness()
        self._emit("after_restore", target)
        target.state = "rolled_back"
        self._save_journal(journal)

    def _cleanup_committed(self, journal: Journal) -> None:
        for target in journal.targets:
            if _target_digest(target, Path(target.live_path)) != target.desired_digest:
                raise TransactionCorruptionError(
                    f"cleanup refused changed target: {target.target_class}/{target.identifier}"
                )
        self._remove_journal(journal)

    def _verify_expected_at(self, target: JournalTarget, root: Path) -> None:
        if target.expected_preimage_digest is not None:
            actual = _target_digest(target, root)
            if actual != target.expected_preimage_digest:
                raise TransactionCorruptionError(
                    f"stale preimage for {target.target_class}/{target.identifier}: "
                    f"got {actual}, expected {target.expected_preimage_digest}"
                )
            return
        assert target.expected_generation is not None
        generation = (
            root if target.generation_path is None else root / target.generation_path
        )
        try:
            info = generation.lstat()
            if (
                not stat.S_ISREG(info.st_mode)
                or generation.is_symlink()
                or info.st_size > 1024 * 1024
            ):
                raise OSError("unsafe generation")
            payload = generation.read_text(encoding="utf-8")
        except (OSError, UnicodeError) as exc:
            raise TransactionCorruptionError(
                f"generation is unavailable for {target.target_class}/{target.identifier}"
            ) from exc
        if payload != target.expected_generation:
            raise TransactionCorruptionError(
                f"stale generation for {target.target_class}/{target.identifier}"
            )

    def _build_journal(
        self, plan: TransactionPlan
    ) -> tuple[Journal, list[Path | None]]:
        if not _TRANSACTION_ID.fullmatch(plan.transaction_id):
            raise TransactionError("invalid transaction id")
        _validate_text(plan.project_identity, "project identity")
        if not plan.targets:
            raise TransactionError("transaction has no targets")

        targets = sorted(
            plan.targets,
            key=lambda target: (
                _utf8_key(target.target_class),
                _utf8_key(target.identifier),
            ),
        )
        records: list[JournalTarget] = []
        sources: list[Path | None] = []
        seen_keys: set[tuple[str, str]] = set()
        for index, target in enumerate(targets):
            _validate_text(target.target_class, "target class")
            _validate_text(target.identifier, "target identifier")
            if target.kind not in {"bytes", "entry"}:
                raise TransactionError(
                    f"target kind is invalid: {target.target_class}/{target.identifier}"
                )
            key = (target.target_class, target.identifier)
            if key in seen_keys:
                raise TransactionError(
                    f"duplicate target: {target.target_class}/{target.identifier}"
                )
            seen_keys.add(key)
            if (target.expected_preimage_digest is None) == (
                target.expected_generation is None
            ):
                raise TransactionError(
                    f"target must provide exactly one expected preimage or generation: "
                    f"{target.target_class}/{target.identifier}"
                )
            if target.kind == "entry" and target.expected_preimage_digest is None:
                raise TransactionError(
                    "entry target must provide an expected preimage digest: "
                    f"{target.target_class}/{target.identifier}"
                )
            live = _canonical_target_path(
                target.live_path,
                kind=target.kind,
                strict=False,
            )
            generation_path = _validate_generation_path(target.generation_path)
            if target.kind == "entry" and generation_path is not None:
                raise TransactionError(
                    "entry target cannot use a generation path: "
                    f"{target.target_class}/{target.identifier}"
                )
            desired_digest = ABSENT_DIGEST
            staged: Path | None = None
            source: Path | None = None
            staging_entries: list[StagingTreeEntry] = []
            if target.desired_path is not None:
                source = _canonical_target_path(
                    target.desired_path,
                    kind=target.kind,
                    strict=True,
                )
                if source == live:
                    raise TransactionError(f"target stages from its live path: {live}")
                desired_digest = digest_target(source, kind=target.kind)
                if desired_digest == ABSENT_DIGEST:
                    raise TransactionError(f"desired target is absent: {source}")
                staging_entries = _staging_manifest(source, kind=target.kind)
                if digest_target(source, kind=target.kind) != desired_digest:
                    raise TransactionCorruptionError(
                        f"desired target changed while planning: {source}"
                    )
                staged = _sidecar(live, plan.transaction_id, index, "desired")
            backup = _sidecar(live, plan.transaction_id, index, "backup")
            rollback = _sidecar(live, plan.transaction_id, index, "rollback")
            records.append(
                JournalTarget(
                    target_class=target.target_class,
                    identifier=target.identifier,
                    kind=target.kind,
                    live_path=str(live),
                    staged_path=str(staged) if staged is not None else None,
                    staged_source=str(source) if source is not None else None,
                    staging_entries=staging_entries,
                    backup_path=str(backup),
                    rollback_path=str(rollback),
                    expected_preimage_digest=target.expected_preimage_digest,
                    expected_generation=target.expected_generation,
                    generation_path=generation_path,
                    desired_digest=desired_digest,
                )
            )
            sources.append(source)

        _validate_namespace_independence(
            self.home,
            self.journal_root,
            plan.transaction_id,
            records,
            corruption=False,
        )
        for record in records:
            for raw in (
                record.staged_path,
                record.backup_path,
                record.rollback_path,
            ):
                if raw is None:
                    continue
                path = Path(raw)
                for reserved in (path, _cleanup_tomb(path)):
                    if _path_exists(reserved):
                        raise TransactionCorruptionError(
                            f"unowned transaction sidecar exists: {reserved}"
                        )

        generations = dict(plan.generation_digests or {})
        for generation_key, digest in generations.items():
            _validate_text(generation_key, "generation key")
            _validate_digest(digest, "generation digest")
        ordered_classes = list(dict.fromkeys(target.target_class for target in records))
        return (
            Journal(
                schema=JOURNAL_SCHEMA,
                transaction_id=plan.transaction_id,
                project_identity=plan.project_identity,
                phase="preparing",
                ordered_target_classes=ordered_classes,
                generation_digests=dict(
                    sorted(generations.items(), key=lambda item: _utf8_key(item[0]))
                ),
                targets=records,
                cleanup_sidecars=[],
            ),
            sources,
        )

    def _save_journal(self, journal: Journal, *, create: bool = False) -> None:
        self._assert_mutation_witness()
        self._validate_journal(journal)
        _ensure_safe_directory(self.journal_root)
        path = self._journal_path(journal.transaction_id)
        tomb = self._journal_tomb_path(journal.transaction_id)
        if _path_exists(tomb):
            raise TransactionCorruptionError(
                f"transaction removal is already in progress: {journal.transaction_id}"
            )
        if create and _path_exists(path):
            raise TransactionError(
                f"transaction already exists: {journal.transaction_id}"
            )
        if not create and not _path_exists(path):
            raise TransactionCorruptionError(
                f"transaction journal disappeared: {journal.transaction_id}"
            )
        temporary = (
            self.journal_root
            / f".{journal.transaction_id}.{os.getpid()}.{uuid.uuid4().hex}.tmp"
        )
        payload = _journal_bytes(journal)
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
        fd = os.open(temporary, flags, 0o600)
        try:
            with os.fdopen(fd, "wb", closefd=False) as handle:
                handle.write(payload)
                handle.flush()
                os.fsync(handle.fileno())
        finally:
            os.close(fd)
        try:
            if create:
                try:
                    _durable_journal_publish_no_replace(temporary, path)
                except FileExistsError as exc:
                    raise TransactionError(
                        f"transaction already exists: {journal.transaction_id}"
                    ) from exc
            else:
                _durable_journal_replace(temporary, path)
        finally:
            temporary.unlink(missing_ok=True)
        self._assert_mutation_witness()

    def _load_journal(self, transaction_id: str) -> Journal:
        journal, deleting = self._load_journal_record(transaction_id)
        if deleting:
            raise TransactionError(
                f"transaction removal is in progress: {transaction_id}"
            )
        return journal

    def _load_journal_record(self, transaction_id: str) -> tuple[Journal, bool]:
        if not _TRANSACTION_ID.fullmatch(transaction_id):
            raise TransactionError("invalid transaction id")
        path, deleting = self._journal_record_path(transaction_id)
        try:
            payload = _read_bounded_regular(path, limit=16 * 1024 * 1024)
        except OSError as exc:
            raise TransactionError(f"cannot read journal {transaction_id}") from exc
        try:
            raw = json.loads(payload)
            journal = _journal_from_dict(raw)
        except (json.JSONDecodeError, KeyError, TypeError, ValueError) as exc:
            raise TransactionCorruptionError(
                f"invalid journal {transaction_id}"
            ) from exc
        if journal.transaction_id != transaction_id:
            raise TransactionCorruptionError(
                "journal filename transaction id does not match embedded "
                f"transaction id: {transaction_id} != {journal.transaction_id}"
            )
        self._validate_journal(journal)
        if payload != _journal_bytes(journal):
            raise TransactionCorruptionError(
                f"journal is not canonical: {transaction_id}"
            )
        if deleting:
            self._validate_journal_removal_ready(
                journal,
                require_cleanup_removed=True,
            )
        return journal, deleting

    def _validate_journal(self, journal: Journal) -> None:
        if journal.schema != JOURNAL_SCHEMA or not _TRANSACTION_ID.fullmatch(
            journal.transaction_id
        ):
            raise TransactionCorruptionError("invalid journal schema or transaction id")
        _validate_text(journal.project_identity, "journal project identity")
        if journal.phase not in {
            "preparing",
            "prepared",
            "committing",
            "cleanup",
            "rolling_back",
        }:
            raise TransactionCorruptionError(f"invalid journal phase: {journal.phase}")
        if not isinstance(journal.ordered_target_classes, list) or not all(
            isinstance(value, str) for value in journal.ordered_target_classes
        ):
            raise TransactionCorruptionError("journal target classes are invalid")
        if not isinstance(journal.generation_digests, dict) or not all(
            isinstance(key, str) and isinstance(value, str)
            for key, value in journal.generation_digests.items()
        ):
            raise TransactionCorruptionError("journal generation digests are invalid")
        if list(journal.generation_digests) != sorted(
            journal.generation_digests, key=_utf8_key
        ):
            raise TransactionCorruptionError(
                "journal generation digests are not canonical"
            )
        for generation_key, digest in journal.generation_digests.items():
            _validate_text(generation_key, "journal generation key")
            _validate_digest(digest, "journal generation digest")
        if not isinstance(journal.targets, list) or not journal.targets:
            raise TransactionCorruptionError("journal targets are invalid")
        if not all(
            isinstance(target, JournalTarget)
            and isinstance(target.target_class, str)
            and isinstance(target.identifier, str)
            and target.kind in {"bytes", "entry"}
            for target in journal.targets
        ):
            raise TransactionCorruptionError("journal target key is invalid")
        sorted_targets = sorted(
            journal.targets,
            key=lambda target: (
                _utf8_key(target.target_class),
                _utf8_key(target.identifier),
            ),
        )
        if journal.targets != sorted_targets:
            raise TransactionCorruptionError(
                "journal targets are not deterministically ordered"
            )
        expected_classes = list(
            dict.fromkeys(target.target_class for target in journal.targets)
        )
        if journal.ordered_target_classes != expected_classes:
            raise TransactionCorruptionError("journal target classes are not canonical")
        seen: set[tuple[str, str]] = set()
        for index, target in enumerate(journal.targets):
            if not isinstance(target.target_class, str) or not isinstance(
                target.identifier, str
            ):
                raise TransactionCorruptionError("journal target key is invalid")
            _validate_text(target.target_class, "journal target class")
            _validate_text(target.identifier, "journal target identifier")
            key = (target.target_class, target.identifier)
            if target.kind not in {"bytes", "entry"}:
                raise TransactionCorruptionError(
                    f"journal target kind is invalid: {key}"
                )
            if key in seen:
                raise TransactionCorruptionError(f"duplicate journal target: {key}")
            seen.add(key)
            if not all(
                isinstance(value, str)
                for value in (
                    target.live_path,
                    target.backup_path,
                    target.rollback_path,
                    target.desired_digest,
                )
            ):
                raise TransactionCorruptionError(
                    f"journal target paths are invalid: {key}"
                )
            if target.staged_path is not None and not isinstance(
                target.staged_path, str
            ):
                raise TransactionCorruptionError(
                    f"journal staged path is invalid: {key}"
                )
            if target.staged_source is not None and not isinstance(
                target.staged_source, str
            ):
                raise TransactionCorruptionError(
                    f"journal staging source is invalid: {key}"
                )
            live = Path(target.live_path)
            try:
                canonical_live = _canonical_target_path(
                    live,
                    kind=target.kind,
                    strict=False,
                )
            except TransactionError as exc:
                raise TransactionCorruptionError(
                    f"journal live path is invalid: {key}"
                ) from exc
            if (
                not live.is_absolute()
                or Path(os.path.abspath(live)) != live
                or (canonical_live != live)
            ):
                raise TransactionCorruptionError(f"journal live path is invalid: {key}")
            _validate_digest(target.desired_digest, "desired digest")
            expected_staged = (
                None
                if target.desired_digest == ABSENT_DIGEST
                else str(_sidecar(live, journal.transaction_id, index, "desired"))
            )
            if (
                target.staged_path != expected_staged
                or target.backup_path
                != str(_sidecar(live, journal.transaction_id, index, "backup"))
                or target.rollback_path
                != str(_sidecar(live, journal.transaction_id, index, "rollback"))
            ):
                raise TransactionCorruptionError(
                    f"journal sidecar path is invalid: {key}"
                )
            _validate_staging_record(
                target,
                key,
                preparing=journal.phase == "preparing",
            )
            if target.backup_digest is not None:
                if not isinstance(target.backup_digest, str):
                    raise TransactionCorruptionError(f"backup digest is invalid: {key}")
                _validate_digest(target.backup_digest, "backup digest")
            if target.state not in {"pending", "backed_up", "committed", "rolled_back"}:
                raise TransactionCorruptionError(
                    f"invalid target state: {target.state}"
                )
            if (target.expected_preimage_digest is None) == (
                target.expected_generation is None
            ):
                raise TransactionCorruptionError(
                    f"invalid expectation for journal target: {key}"
                )
            if target.kind == "entry" and target.expected_preimage_digest is None:
                raise TransactionCorruptionError(
                    f"entry journal target has no preimage digest: {key}"
                )
            if target.expected_preimage_digest is not None:
                if not isinstance(target.expected_preimage_digest, str):
                    raise TransactionCorruptionError(
                        f"preimage digest is invalid: {key}"
                    )
                _validate_digest(target.expected_preimage_digest, "preimage digest")
            if target.expected_generation is not None:
                if not isinstance(target.expected_generation, str):
                    raise TransactionCorruptionError(f"generation is invalid: {key}")
                _validate_text(target.expected_generation, "expected generation")
            if target.generation_path is not None and not isinstance(
                target.generation_path, str
            ):
                raise TransactionCorruptionError(f"generation path is invalid: {key}")
            _validate_generation_path(target.generation_path)
            if target.kind == "entry" and target.generation_path is not None:
                raise TransactionCorruptionError(
                    f"entry journal target has a generation path: {key}"
                )
        _validate_namespace_independence(
            self.home,
            self.journal_root,
            journal.transaction_id,
            journal.targets,
            corruption=True,
        )
        states = {target.state for target in journal.targets}
        if journal.phase in {"preparing", "prepared"} and states != {"pending"}:
            raise TransactionCorruptionError(
                f"journal phase {journal.phase} has advanced target state"
            )
        if journal.phase == "committing" and "rolled_back" in states:
            raise TransactionCorruptionError("committing journal has rolled-back state")
        if journal.phase == "cleanup" and states != {"committed"}:
            raise TransactionCorruptionError(
                "cleanup journal has incomplete target state"
            )
        self._validate_cleanup_sidecars(journal)

    def _validate_cleanup_sidecars(self, journal: Journal) -> None:
        if not isinstance(journal.cleanup_sidecars, list):
            raise TransactionCorruptionError("journal cleanup sidecars are invalid")
        if not journal.cleanup_sidecars:
            return
        states = {target.state for target in journal.targets}
        removal_ready = (
            (journal.phase in {"preparing", "prepared"} and states == {"pending"})
            or (journal.phase == "cleanup" and states == {"committed"})
            or (
                journal.phase == "rolling_back" and states <= {"pending", "rolled_back"}
            )
        )
        if not removal_ready:
            raise TransactionCorruptionError(
                "journal cleanup began before target state was removal-ready"
            )
        expected_specs = _cleanup_sidecar_specs(journal.targets)
        if len(journal.cleanup_sidecars) != len(expected_specs):
            raise TransactionCorruptionError(
                "journal cleanup sidecar set is incomplete"
            )
        for cleanup, (target_index, role, expected_path) in zip(
            journal.cleanup_sidecars,
            expected_specs,
            strict=True,
        ):
            if (
                not isinstance(cleanup, JournalCleanupSidecar)
                or not isinstance(cleanup.target_index, int)
                or isinstance(cleanup.target_index, bool)
                or cleanup.target_index != target_index
                or cleanup.role != role
                or not isinstance(cleanup.path, str)
                or cleanup.path != str(expected_path)
                or not isinstance(cleanup.tomb_path, str)
                or cleanup.tomb_path != str(_cleanup_tomb(expected_path))
                or not isinstance(cleanup.expected_digest, str)
            ):
                raise TransactionCorruptionError(
                    "journal cleanup sidecar identity is invalid"
                )
            _validate_digest(cleanup.expected_digest, "cleanup sidecar digest")
            allowed = _allowed_cleanup_digests(
                journal,
                journal.targets[target_index],
                role,
            )
            unfinished_staging = journal.phase == "preparing" and role == "staged"
            if cleanup.expected_digest not in allowed and not unfinished_staging:
                raise TransactionCorruptionError(
                    "journal cleanup sidecar digest is not owned by the "
                    f"transaction: {cleanup.path}"
                )
            if cleanup.state not in {"pending", "tombed", "removed"}:
                raise TransactionCorruptionError(
                    f"journal cleanup state is invalid: {cleanup.path}"
                )
            if (
                not isinstance(cleanup.writable_index, int)
                or isinstance(cleanup.writable_index, bool)
                or cleanup.writable_index < 0
                or not isinstance(cleanup.writable_active, bool)
            ):
                raise TransactionCorruptionError(
                    f"journal cleanup writable progress is invalid: {cleanup.path}"
                )
            _validate_cleanup_manifest_record(
                cleanup.expected_digest,
                cleanup.entries,
                kind=journal.targets[target_index].kind,
            )
            writable_count = len(_cleanup_writable_order(cleanup.entries))
            if cleanup.writable_index > writable_count or (
                cleanup.writable_active and cleanup.writable_index == writable_count
            ):
                raise TransactionCorruptionError(
                    f"journal cleanup writable index is invalid: {cleanup.path}"
                )
            if cleanup.state == "pending" and (
                cleanup.writable_index != 0 or cleanup.writable_active
            ):
                raise TransactionCorruptionError(
                    f"cleanup became writable before it was tombed: {cleanup.path}"
                )
            if cleanup.state == "removed" and (
                cleanup.writable_index != writable_count or cleanup.writable_active
            ):
                raise TransactionCorruptionError(
                    f"removed cleanup has incomplete writable progress: {cleanup.path}"
                )
            if cleanup.expected_digest == ABSENT_DIGEST:
                if cleanup.state != "removed":
                    raise TransactionCorruptionError(
                        f"absent cleanup sidecar is not removed: {cleanup.path}"
                    )
            elif cleanup.state == "pending" and not cleanup.entries:
                raise TransactionCorruptionError(
                    f"pending cleanup sidecar has no entries: {cleanup.path}"
                )

    def _discard_prepared(self, journal: Journal) -> None:
        if not journal.cleanup_sidecars:
            self._validate_preparing_staging(journal)
        self._remove_journal(journal)

    def _validate_preparing_staging(self, journal: Journal) -> None:
        if journal.phase != "preparing":
            return
        for target in journal.targets:
            _validate_preparing_staging_target(target)

    def _prepare_sidecar_cleanup(self, journal: Journal) -> None:
        if journal.cleanup_sidecars:
            return
        self._validate_preparing_staging(journal)
        cleanup_sidecars: list[JournalCleanupSidecar] = []
        for target_index, role, path in _cleanup_sidecar_specs(journal.targets):
            target = journal.targets[target_index]
            tomb = _cleanup_tomb(path)
            if _path_exists(tomb):
                raise TransactionCorruptionError(
                    f"unrecorded cleanup tomb exists: {tomb}"
                )
            actual = _target_digest(target, path)
            allowed = _allowed_cleanup_digests(
                journal,
                journal.targets[target_index],
                role,
            )
            unfinished_staging = journal.phase == "preparing" and role == "staged"
            if actual not in allowed and not unfinished_staging:
                raise TransactionCorruptionError(
                    f"changed or unrecorded cleanup bytes: {path}"
                )
            entries = _cleanup_manifest(path, kind=target.kind)
            if _target_digest(target, path) != actual:
                raise TransactionCorruptionError(
                    f"cleanup sidecar changed while journaling: {path}"
                )
            cleanup_sidecars.append(
                JournalCleanupSidecar(
                    target_index=target_index,
                    role=role,
                    path=str(path),
                    tomb_path=str(tomb),
                    expected_digest=actual,
                    entries=entries,
                    state="removed" if actual == ABSENT_DIGEST else "pending",
                )
            )
        self._validate_preparing_staging(journal)
        journal.cleanup_sidecars = cleanup_sidecars
        self._save_journal(journal)
        self._emit("cleanup_journaled", None)

    def _discard_sidecars(self, journal: Journal, *, persist: bool) -> None:
        for cleanup in journal.cleanup_sidecars:
            if cleanup.state != "removed":
                continue
            path = Path(cleanup.path)
            tomb = Path(cleanup.tomb_path)
            if _path_exists(path) or _path_exists(tomb):
                raise TransactionCorruptionError(
                    "removed cleanup namespace reappeared after ownership was "
                    f"relinquished: {path}"
                )

        for cleanup in journal.cleanup_sidecars:
            self._assert_mutation_witness()
            target = journal.targets[cleanup.target_index]
            path = Path(cleanup.path)
            tomb = Path(cleanup.tomb_path)
            if cleanup.state == "removed":
                continue

            if cleanup.state == "pending":
                current = _target_digest(target, path)
                tomb_digest = _target_digest(target, tomb)
                if current != ABSENT_DIGEST and tomb_digest != ABSENT_DIGEST:
                    raise TransactionCorruptionError(
                        f"cleanup sidecar and tomb both exist: {path}"
                    )
                if current == cleanup.expected_digest:
                    if tomb_digest != ABSENT_DIGEST:
                        raise TransactionCorruptionError(
                            f"unrecorded cleanup tomb bytes: {tomb}"
                        )
                    _validate_cleanup_tree(
                        path,
                        cleanup,
                        kind=target.kind,
                        allow_partial=False,
                    )
                    try:
                        self._assert_mutation_witness()
                        _durable_journal_publish_no_replace(path, tomb)
                    except FileExistsError as exc:
                        raise TransactionCorruptionError(
                            f"cleanup tomb already exists: {tomb}"
                        ) from exc
                    self._emit("sidecar_tombed", target)
                elif current == ABSENT_DIGEST and tomb_digest != ABSENT_DIGEST:
                    _validate_cleanup_tree(
                        tomb,
                        cleanup,
                        kind=target.kind,
                        allow_partial=False,
                    )
                    if tomb_digest != cleanup.expected_digest:
                        raise TransactionCorruptionError(
                            f"changed cleanup bytes: {tomb}"
                        )
                else:
                    raise TransactionCorruptionError(
                        f"changed or missing cleanup bytes: {path}"
                    )
                cleanup.state = "tombed"
                if persist:
                    self._save_journal(journal)

            if cleanup.state == "tombed":
                if _target_digest(target, path) != ABSENT_DIGEST:
                    raise TransactionCorruptionError(
                        f"canonical cleanup sidecar reappeared: {path}"
                    )
                if _path_exists(tomb):
                    self._assert_mutation_witness()
                    self._make_cleanup_tree_writable(
                        journal,
                        cleanup,
                        target,
                        persist=persist,
                    )
                    _validate_cleanup_tree(
                        tomb,
                        cleanup,
                        kind=target.kind,
                        allow_partial=True,
                    )
                    _remove_cleanup_tree(
                        tomb,
                        cleanup,
                        kind=target.kind,
                        on_removed=partial(
                            self._emit,
                            "sidecar_entry_removed",
                            target,
                        ),
                    )
                    self._assert_mutation_witness()
                if _path_exists(tomb):
                    raise TransactionCorruptionError(
                        f"cleanup tomb remains after removal: {tomb}"
                    )
                cleanup.state = "removed"
                if persist:
                    self._save_journal(journal)

    def _make_cleanup_tree_writable(
        self,
        journal: Journal,
        cleanup: JournalCleanupSidecar,
        target: JournalTarget,
        *,
        persist: bool,
    ) -> None:
        order = _cleanup_writable_order(cleanup.entries)
        if not persist and cleanup.writable_index < len(order):
            raise TransactionCorruptionError(
                f"journal tomb has incomplete writable cleanup: {cleanup.path}"
            )
        tomb = Path(cleanup.tomb_path)
        while cleanup.writable_index < len(order):
            entry = order[cleanup.writable_index]
            entry_path = _cleanup_entry_path(tomb, entry.relative_path)
            if not cleanup.writable_active:
                cleanup.writable_active = True
                self._save_journal(journal)
            writable_mode = _cleanup_writable_mode(entry)
            _validate_cleanup_entry_modes(
                entry_path,
                entry,
                {entry.mode, writable_mode},
            )
            self._emit("before_sidecar_mode_writable", target)
            _set_entry_mode_durably(entry_path, entry, writable_mode)
            _validate_cleanup_entry_modes(
                entry_path,
                entry,
                {writable_mode},
            )
            self._emit("sidecar_mode_writable", target)
            cleanup.writable_index += 1
            cleanup.writable_active = False
            self._save_journal(journal)

    def _remove_journal(self, journal: Journal) -> None:
        self._assert_mutation_witness()
        self._validate_journal_removal_ready(journal)
        path = self._journal_path(journal.transaction_id)
        tomb = self._journal_tomb_path(journal.transaction_id)
        path_exists = _path_exists(path)
        tomb_exists = _path_exists(tomb)
        if path_exists and tomb_exists:
            raise TransactionCorruptionError(
                f"transaction has both journal and removal tomb: {journal.transaction_id}"
            )
        expected = _journal_bytes(journal)
        if path_exists:
            if _read_bounded_regular(path, limit=16 * 1024 * 1024) != _journal_bytes(
                journal
            ):
                raise TransactionCorruptionError(
                    f"journal changed before removal: {journal.transaction_id}"
                )
            self._prepare_sidecar_cleanup(journal)
            self._discard_sidecars(journal, persist=True)
            if any(cleanup.state != "removed" for cleanup in journal.cleanup_sidecars):
                raise TransactionCorruptionError(
                    f"transaction cleanup is incomplete: {journal.transaction_id}"
                )
            self._emit("before_journal_tomb", None)
            expected = _journal_bytes(journal)
            if _read_bounded_regular(path, limit=16 * 1024 * 1024) != expected:
                raise TransactionCorruptionError(
                    f"journal changed before removal: {journal.transaction_id}"
                )
            try:
                self._assert_mutation_witness()
                _durable_journal_publish_no_replace(path, tomb)
            except FileExistsError as exc:
                raise TransactionCorruptionError(
                    f"transaction removal tomb already exists: {journal.transaction_id}"
                ) from exc
            self._emit("journal_tombed", None)
            tomb_exists = True
        if not tomb_exists:
            raise TransactionCorruptionError(
                f"transaction journal disappeared during removal: {journal.transaction_id}"
            )
        if _read_bounded_regular(tomb, limit=16 * 1024 * 1024) != expected:
            raise TransactionCorruptionError(
                f"journal tomb changed before removal: {journal.transaction_id}"
            )
        self._discard_sidecars(journal, persist=False)
        self._emit("before_journal_unlink", None)
        self._assert_mutation_witness()
        tomb.unlink()
        _sync_directory(self.journal_root)
        self._assert_mutation_witness()

    def _finish_journal_removal(self, journal: Journal) -> None:
        self._remove_journal(journal)

    def _validate_journal_removal_ready(
        self,
        journal: Journal,
        *,
        require_cleanup_removed: bool = False,
    ) -> None:
        states = {target.state for target in journal.targets}
        if journal.phase in {"preparing", "prepared"}:
            ready = states == {"pending"}
        elif journal.phase == "cleanup":
            ready = states == {"committed"}
        elif journal.phase == "rolling_back":
            ready = states <= {"pending", "rolled_back"}
        else:
            ready = False
        if not ready:
            raise TransactionCorruptionError(
                f"journal is not ready for durable removal: {journal.transaction_id}"
            )
        if require_cleanup_removed and (
            not journal.cleanup_sidecars
            or any(cleanup.state != "removed" for cleanup in journal.cleanup_sidecars)
        ):
            raise TransactionCorruptionError(
                "journal removal tomb does not own completed sidecar cleanup: "
                f"{journal.transaction_id}"
            )

    def _journal_ids(self) -> list[str]:
        try:
            info = self.journal_root.lstat()
        except FileNotFoundError:
            return []
        if not stat.S_ISDIR(info.st_mode) or self.journal_root.is_symlink():
            raise TransactionCorruptionError(
                f"transaction state is not a safe directory: {self.journal_root}"
            )
        records: dict[str, str] = {}
        for path in self.journal_root.iterdir():
            name = path.name
            if name.endswith(".json.delete"):
                transaction_id = name[: -len(".json.delete")]
            elif name.endswith(".json"):
                transaction_id = name[: -len(".json")]
            else:
                continue
            if not path.is_file() or path.is_symlink():
                raise TransactionCorruptionError(
                    f"journal is not a regular file: {path.name}"
                )
            if not _TRANSACTION_ID.fullmatch(transaction_id):
                raise TransactionCorruptionError(
                    f"invalid journal filename: {path.name}"
                )
            previous = records.setdefault(transaction_id, name)
            if previous != name:
                raise TransactionCorruptionError(
                    f"transaction has multiple journal records: {transaction_id}"
                )
        return sorted(records, key=_utf8_key)

    def _journal_record_path(self, transaction_id: str) -> tuple[Path, bool]:
        path = self._journal_path(transaction_id)
        tomb = self._journal_tomb_path(transaction_id)
        path_exists = _path_exists(path)
        tomb_exists = _path_exists(tomb)
        if path_exists and tomb_exists:
            raise TransactionCorruptionError(
                f"transaction has both journal and removal tomb: {transaction_id}"
            )
        if path_exists:
            return path, False
        if tomb_exists:
            return tomb, True
        raise TransactionError(f"cannot read journal {transaction_id}")

    def _journal_path(self, transaction_id: str) -> Path:
        return self.journal_root / f"{transaction_id}.json"

    def _journal_tomb_path(self, transaction_id: str) -> Path:
        return self.journal_root / f"{transaction_id}.json.delete"

    @contextmanager
    def _witness_scope(self, lock: HomeLockWitness) -> Iterator[None]:
        if self._active_witness is not None:
            raise TransactionError("transaction engine lock witness is already active")
        lock.assert_held()
        witness_identity = self._witness_home_identity(lock)
        engine_identity = canonical_manager_home_identity(self.home)
        if witness_identity != engine_identity:
            raise TransactionError(
                "manager-home lock witness identity does not match transaction "
                f"engine home: {witness_identity} != {engine_identity}"
            )
        self._active_witness = lock
        self._active_home_identity = engine_identity
        try:
            yield
            lock.assert_held()
            self._assert_active_witness_home(lock)
        finally:
            self._active_witness = None
            self._active_home_identity = None

    def _assert_mutation_witness(self) -> None:
        if self._active_witness is not None:
            self._active_witness.assert_held()
            self._assert_active_witness_home(self._active_witness)

    @staticmethod
    def _witness_home_identity(lock: HomeLockWitness) -> str:
        witness_identity = lock.home_identity
        if not isinstance(witness_identity, str):
            raise TransactionError("manager-home lock witness identity is invalid")
        return witness_identity

    def _assert_active_witness_home(self, lock: HomeLockWitness) -> None:
        witness_identity = self._witness_home_identity(lock)
        if (
            self._active_home_identity is None
            or witness_identity != self._active_home_identity
        ):
            raise TransactionError(
                "manager-home lock witness identity changed while the transaction "
                "engine was active"
            )

    def _emit(self, point: str, target: JournalTarget | None) -> None:
        if self._fault_hook is not None:
            self._fault_hook(point, target)
        self._assert_mutation_witness()


def digest_target(path: Path, *, kind: TargetKind = "bytes") -> str:
    """Digest a byte tree or one manager-owned final directory entry."""
    if kind not in {"bytes", "entry"}:
        raise TransactionError(f"invalid transaction target kind: {kind}")
    if kind == "bytes":
        return digest_path(path)
    try:
        info = path.lstat()
    except FileNotFoundError:
        return ABSENT_DIGEST
    if not stat.S_ISLNK(info.st_mode):
        return digest_path(path)
    try:
        destination = os.readlink(path)
        encoded = destination.encode("utf-8", errors="strict")
    except (OSError, UnicodeError) as exc:
        raise TransactionError(f"cannot read transaction link target: {path}") from exc
    if not destination or "\x00" in destination:
        raise TransactionError(f"invalid transaction link target: {path}")
    digest = hashlib.sha256(_DIGEST_DOMAIN)
    _write_digest_entry(digest, b"l", "", 0, len(encoded))
    digest.update(encoded)
    current = path.lstat()
    if (
        not stat.S_ISLNK(current.st_mode)
        or not os.path.samestat(info, current)
        or os.readlink(path) != destination
    ):
        raise TransactionCorruptionError(
            f"transaction target changed while digesting: {path}"
        )
    return "sha256:" + digest.hexdigest()


def _target_digest(target: JournalTarget, path: Path) -> str:
    return digest_target(path, kind=target.kind)


def digest_path(path: Path) -> str:
    try:
        info = path.lstat()
    except FileNotFoundError:
        return ABSENT_DIGEST
    digest = hashlib.sha256(_DIGEST_DOMAIN)
    if stat.S_ISREG(info.st_mode):
        _digest_file(digest, path, "", info)
    elif stat.S_ISDIR(info.st_mode) and not path.is_symlink():
        _write_digest_entry(digest, b"d", "", stat.S_IMODE(info.st_mode), 0)
        entries = sorted(
            (entry for entry in path.rglob("*")),
            key=lambda entry: _utf8_key(entry.relative_to(path).as_posix()),
        )
        for entry in entries:
            entry_info = entry.lstat()
            relative = entry.relative_to(path).as_posix()
            if stat.S_ISDIR(entry_info.st_mode) and not entry.is_symlink():
                _write_digest_entry(
                    digest, b"d", relative, stat.S_IMODE(entry_info.st_mode), 0
                )
            elif stat.S_ISREG(entry_info.st_mode):
                _digest_file(digest, entry, relative, entry_info)
            else:
                raise TransactionError(f"unsafe transaction tree entry: {entry}")
    else:
        raise TransactionError(f"unsafe transaction target: {path}")
    return "sha256:" + digest.hexdigest()


def _digest_file(digest: Any, path: Path, relative: str, info: os.stat_result) -> None:
    _write_digest_entry(
        digest, b"f", relative, stat.S_IMODE(info.st_mode), info.st_size
    )
    with path.open("rb") as handle:
        while chunk := handle.read(1024 * 1024):
            digest.update(chunk)
        after = os.fstat(handle.fileno())
    if (
        not os.path.samestat(info, after)
        or after.st_size != info.st_size
        or stat.S_IMODE(after.st_mode) != stat.S_IMODE(info.st_mode)
    ):
        raise TransactionCorruptionError(
            f"transaction target changed while digesting: {path}"
        )
    current = path.lstat()
    if not os.path.samestat(after, current):
        raise TransactionCorruptionError(
            f"transaction target changed while digesting: {path}"
        )


def _entry_content_digest(path: Path, info: os.stat_result) -> str:
    if not stat.S_ISREG(info.st_mode) or path.is_symlink():
        raise TransactionCorruptionError(
            f"transaction entry is not a regular file: {path}"
        )
    digest = hashlib.sha256(_ENTRY_CONTENT_DOMAIN)
    digest.update(struct.pack(">Q", info.st_size))
    with path.open("rb") as handle:
        while chunk := handle.read(1024 * 1024):
            digest.update(chunk)
        after = os.fstat(handle.fileno())
    current = path.lstat()
    if (
        not os.path.samestat(info, after)
        or not os.path.samestat(after, current)
        or after.st_size != info.st_size
        or stat.S_IMODE(after.st_mode) != stat.S_IMODE(info.st_mode)
    ):
        raise TransactionCorruptionError(
            f"transaction entry changed while digesting: {path}"
        )
    return "sha256:" + digest.hexdigest()


def _write_digest_entry(
    digest: Any, kind: bytes, relative: str, mode: int, size: int
) -> None:
    encoded = relative.encode("utf-8", errors="strict")
    digest.update(kind)
    digest.update(struct.pack(">Q", len(encoded)))
    digest.update(encoded)
    digest.update(struct.pack(">IQ", mode, size))


def _copy_target(source: Path, destination: Path) -> None:
    if _path_exists(destination):
        raise TransactionCorruptionError(
            f"transaction staging path exists: {destination}"
        )
    _require_safe_staging_parent(destination.parent)
    info = source.lstat()
    if stat.S_ISREG(info.st_mode):
        shutil.copyfile(source, destination, follow_symlinks=False)
        destination.chmod(stat.S_IMODE(info.st_mode))
        _sync_regular(destination)
    elif stat.S_ISDIR(info.st_mode) and not source.is_symlink():
        shutil.copytree(source, destination, symlinks=True)
        for entry in destination.rglob("*"):
            entry_info = entry.lstat()
            if entry.is_symlink() or not (
                stat.S_ISREG(entry_info.st_mode) or stat.S_ISDIR(entry_info.st_mode)
            ):
                _remove_path(destination)
                raise TransactionError(f"unsafe staged transaction entry: {entry}")
        _sync_tree(destination)
    else:
        raise TransactionError(f"unsafe staged transaction source: {source}")
    _sync_directory(destination.parent)


def _sync_tree(path: Path) -> None:
    info = path.lstat()
    if stat.S_ISLNK(info.st_mode):
        _sync_directory(path.parent)
        return
    if stat.S_ISREG(info.st_mode):
        _sync_regular(path)
        _sync_directory(path.parent)
        return
    for entry in sorted(
        path.rglob("*"), key=lambda value: len(value.parts), reverse=True
    ):
        if entry.is_file() and not entry.is_symlink():
            _sync_regular(entry)
        elif entry.is_dir() and not entry.is_symlink():
            _sync_directory(entry)
    _sync_directory(path)
    _sync_directory(path.parent)


def _is_windows() -> bool:
    return os.name == "nt"


def _sync_regular(path: Path) -> None:
    if _is_windows():
        _windows_sync_regular(path)
        return
    with path.open("rb") as handle:
        os.fsync(handle.fileno())


def _sync_directory(path: Path) -> None:
    if _is_windows():
        _windows_sync_directory(path)
        return
    fd = os.open(path, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _durable_journal_publish_no_replace(source: Path, destination: Path) -> None:
    if _is_windows():
        _windows_move_file(source, destination, _MOVEFILE_WRITE_THROUGH)
    else:
        _native_rename_no_replace(source, destination)
    _sync_directory(destination.parent)
    if source.parent != destination.parent:
        _sync_directory(source.parent)


def _durable_journal_replace(source: Path, destination: Path) -> None:
    if _is_windows():
        _windows_move_file(
            source,
            destination,
            _MOVEFILE_REPLACE_EXISTING | _MOVEFILE_WRITE_THROUGH,
        )
    else:
        os.replace(source, destination)
    _sync_directory(destination.parent)
    if source.parent != destination.parent:
        _sync_directory(source.parent)


def _rename_no_replace(source: Path, destination: Path) -> None:
    try:
        _native_rename_no_replace(source, destination)
    except FileExistsError as exc:
        raise TransactionCorruptionError(
            f"transaction destination exists: {destination}"
        ) from exc
    _sync_directory(destination.parent)
    if source.parent != destination.parent:
        _sync_directory(source.parent)


def _native_rename_no_replace(source: Path, destination: Path) -> None:
    """Atomically rename without replacing a competing destination."""
    if _is_windows():
        _windows_rename_no_replace(source, destination)
        return
    if sys.platform == "darwin":
        _darwin_rename_no_replace(source, destination)
        return
    if sys.platform.startswith("linux"):
        _linux_rename_no_replace(source, destination)
        return
    raise TransactionError(f"atomic no-replace rename is unsupported on {sys.platform}")


def _darwin_rename_no_replace(source: Path, destination: Path) -> None:
    libc: Any = ctypes.CDLL(None, use_errno=True)
    try:
        renamex_np: Any = libc.renamex_np
    except AttributeError as exc:
        raise TransactionError(
            "atomic no-replace rename is unavailable on macOS"
        ) from exc
    renamex_np.argtypes = [ctypes.c_char_p, ctypes.c_char_p, ctypes.c_uint]
    renamex_np.restype = ctypes.c_int
    ctypes.set_errno(0)
    if renamex_np(os.fsencode(source), os.fsencode(destination), 0x00000004) != 0:
        _raise_posix_rename_error(ctypes.get_errno(), source, destination)


def _linux_rename_no_replace(source: Path, destination: Path) -> None:
    libc: Any = ctypes.CDLL(None, use_errno=True)
    try:
        renameat2: Any = libc.renameat2
    except AttributeError as exc:
        raise TransactionError(
            "atomic no-replace rename is unavailable on Linux"
        ) from exc
    renameat2.argtypes = [
        ctypes.c_int,
        ctypes.c_char_p,
        ctypes.c_int,
        ctypes.c_char_p,
        ctypes.c_uint,
    ]
    renameat2.restype = ctypes.c_int
    ctypes.set_errno(0)
    if (
        renameat2(
            -100,
            os.fsencode(source),
            -100,
            os.fsencode(destination),
            0x00000001,
        )
        != 0
    ):
        error_number = ctypes.get_errno()
        if error_number == errno.ENOSYS:
            raise TransactionError(
                "atomic no-replace rename is unavailable on this Linux kernel"
            )
        _raise_posix_rename_error(error_number, source, destination)


def _windows_rename_no_replace(source: Path, destination: Path) -> None:
    _windows_move_file(source, destination, _MOVEFILE_WRITE_THROUGH)


def _windows_move_file(source: Path, destination: Path, flags: int) -> None:
    win_dll: Any = ctypes.WinDLL  # type: ignore[attr-defined]
    get_last_error: Any = ctypes.get_last_error  # type: ignore[attr-defined]
    format_error: Any = ctypes.FormatError  # type: ignore[attr-defined]
    kernel32: Any = win_dll("kernel32", use_last_error=True)
    move_file_ex: Any = kernel32.MoveFileExW
    move_file_ex.argtypes = [ctypes.c_wchar_p, ctypes.c_wchar_p, ctypes.c_uint]
    move_file_ex.restype = ctypes.c_int
    if move_file_ex(str(source), str(destination), flags):
        return
    error_number = int(get_last_error())
    if not flags & _MOVEFILE_REPLACE_EXISTING and error_number in {
        _ERROR_FILE_EXISTS,
        _ERROR_ALREADY_EXISTS,
    }:
        raise FileExistsError(
            errno.EEXIST,
            f"destination already exists: {destination}",
            str(destination),
        )
    raise OSError(
        error_number,
        f"{format_error(error_number)}: {source} -> {destination}",
    )


def _windows_sync_regular(path: Path) -> None:
    try:
        _windows_flush_path(
            path,
            desired_access=_GENERIC_WRITE,
            flags_and_attributes=_FILE_FLAG_OPEN_REPARSE_POINT,
            ignored_flush_errors=frozenset(),
        )
    except OSError as exc:
        if exc.errno != _ERROR_ACCESS_DENIED:
            raise
        _windows_flush_path(
            path,
            desired_access=_GENERIC_READ,
            flags_and_attributes=_FILE_FLAG_OPEN_REPARSE_POINT,
            ignored_flush_errors=frozenset(
                {_ERROR_ACCESS_DENIED, _ERROR_INVALID_HANDLE}
            ),
        )


def _windows_sync_directory(path: Path) -> None:
    _windows_flush_path(
        path,
        desired_access=_GENERIC_READ,
        flags_and_attributes=_FILE_FLAG_BACKUP_SEMANTICS,
        ignored_flush_errors=frozenset({_ERROR_ACCESS_DENIED, _ERROR_INVALID_HANDLE}),
    )


def _windows_flush_path(
    path: Path,
    *,
    desired_access: int,
    flags_and_attributes: int,
    ignored_flush_errors: frozenset[int],
) -> None:
    win_dll: Any = ctypes.WinDLL  # type: ignore[attr-defined]
    get_last_error: Any = ctypes.get_last_error  # type: ignore[attr-defined]
    format_error: Any = ctypes.FormatError  # type: ignore[attr-defined]
    kernel32: Any = win_dll("kernel32", use_last_error=True)
    create_file: Any = kernel32.CreateFileW
    create_file.argtypes = [
        ctypes.c_wchar_p,
        ctypes.c_uint,
        ctypes.c_uint,
        ctypes.c_void_p,
        ctypes.c_uint,
        ctypes.c_uint,
        ctypes.c_void_p,
    ]
    create_file.restype = ctypes.c_void_p
    flush_file_buffers: Any = kernel32.FlushFileBuffers
    flush_file_buffers.argtypes = [ctypes.c_void_p]
    flush_file_buffers.restype = ctypes.c_int
    close_handle: Any = kernel32.CloseHandle
    close_handle.argtypes = [ctypes.c_void_p]
    close_handle.restype = ctypes.c_int

    handle = create_file(
        str(path),
        desired_access,
        _FILE_SHARE_READ | _FILE_SHARE_WRITE | _FILE_SHARE_DELETE,
        None,
        _OPEN_EXISTING,
        flags_and_attributes,
        None,
    )
    if handle in {None, ctypes.c_void_p(-1).value}:
        error_number = int(get_last_error())
        raise OSError(
            error_number,
            f"{format_error(error_number)}: cannot open for durability: {path}",
        )

    flush_error = 0
    if not flush_file_buffers(handle):
        flush_error = int(get_last_error())
    close_error = 0
    if not close_handle(handle):
        close_error = int(get_last_error())
    if flush_error and flush_error not in ignored_flush_errors:
        raise OSError(
            flush_error,
            f"{format_error(flush_error)}: cannot flush for durability: {path}",
        )
    if close_error:
        raise OSError(
            close_error,
            f"{format_error(close_error)}: cannot close durability handle: {path}",
        )


def _raise_posix_rename_error(
    error_number: int, source: Path, destination: Path
) -> None:
    if error_number in {errno.EEXIST, errno.ENOTEMPTY}:
        raise FileExistsError(
            errno.EEXIST,
            os.strerror(errno.EEXIST),
            str(destination),
        )
    if error_number == 0:
        error_number = errno.EIO
    raise OSError(
        error_number,
        f"{os.strerror(error_number)}: {source} -> {destination}",
    )


def _require_safe_staging_parent(path: Path) -> None:
    try:
        info = path.lstat()
    except FileNotFoundError as exc:
        raise TransactionError(
            f"transaction staging parent does not exist: {path}"
        ) from exc
    if not stat.S_ISDIR(info.st_mode) or path.is_symlink():
        raise TransactionCorruptionError(
            f"transaction staging parent is not a safe directory: {path}"
        )


def _remove_path(path: Path) -> None:
    try:
        info = path.lstat()
    except FileNotFoundError:
        return
    if stat.S_ISDIR(info.st_mode) and not path.is_symlink():
        shutil.rmtree(path)
    else:
        path.unlink()
    _sync_directory(path.parent)


def _path_exists(path: Path) -> bool:
    try:
        path.lstat()
    except FileNotFoundError:
        return False
    return True


def _ensure_safe_directory(path: Path) -> None:
    try:
        info = path.lstat()
    except FileNotFoundError:
        parent = path.parent
        if parent == path:
            raise TransactionError(
                f"transaction state has no directory ancestor: {path}"
            )
        _ensure_safe_directory(parent)
        path.mkdir(mode=0o700)
        _sync_directory(path)
        _sync_directory(parent)
        return
    if not stat.S_ISDIR(info.st_mode) or path.is_symlink():
        raise TransactionCorruptionError(
            f"transaction state is not a safe directory: {path}"
        )


def _read_bounded_regular(path: Path, *, limit: int) -> bytes:
    before = path.lstat()
    if not stat.S_ISREG(before.st_mode) or path.is_symlink() or before.st_size > limit:
        raise TransactionCorruptionError(
            f"journal is not a bounded regular file: {path}"
        )
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    fd = os.open(path, flags)
    try:
        after = os.fstat(fd)
        if not stat.S_ISREG(after.st_mode) or not os.path.samestat(before, after):
            raise TransactionCorruptionError(f"journal changed while opening: {path}")
        with os.fdopen(fd, "rb", closefd=False) as handle:
            payload = handle.read(limit + 1)
    finally:
        os.close(fd)
    if len(payload) > limit:
        raise TransactionCorruptionError(f"journal exceeds the size limit: {path}")
    current = path.lstat()
    if not os.path.samestat(after, current):
        raise TransactionCorruptionError(f"journal changed while reading: {path}")
    return payload


def _sidecar(live: Path, transaction_id: str, index: int, suffix: str) -> Path:
    identity = hashlib.sha256(
        b"csk-transaction-sidecar-v1\0" + transaction_id.encode()
    ).hexdigest()[:32]
    return live.parent / f".csk-txn-{identity}-{index:03d}.{suffix}"


def _cleanup_tomb(path: Path) -> Path:
    return path.with_name(f"{path.name}.delete")


def _cleanup_sidecar_specs(
    targets: list[JournalTarget],
) -> list[tuple[int, CleanupRole, Path]]:
    result: list[tuple[int, CleanupRole, Path]] = []
    for target_index in reversed(range(len(targets))):
        target = targets[target_index]
        roles: tuple[tuple[CleanupRole, str | None], ...] = (
            ("staged", target.staged_path),
            ("backup", target.backup_path),
            ("rollback", target.rollback_path),
        )
        for role, raw in roles:
            if raw is not None:
                result.append((target_index, role, Path(raw)))
    return result


def _allowed_cleanup_digests(
    journal: Journal,
    target: JournalTarget,
    role: CleanupRole,
) -> set[str]:
    if journal.phase == "cleanup":
        if role == "backup":
            if target.backup_digest is None:
                raise TransactionCorruptionError(
                    "committed target has no recorded backup digest"
                )
            return {target.backup_digest}
        return {ABSENT_DIGEST}
    if journal.phase in {"preparing", "prepared"}:
        if role == "staged":
            return {ABSENT_DIGEST, target.desired_digest}
        return {ABSENT_DIGEST}
    if journal.phase == "rolling_back":
        if role in {"staged", "rollback"}:
            return {ABSENT_DIGEST, target.desired_digest}
        return {ABSENT_DIGEST}
    raise TransactionCorruptionError(
        f"journal phase cannot own cleanup bytes: {journal.phase}"
    )


def _staging_manifest(path: Path, *, kind: TargetKind) -> list[StagingTreeEntry]:
    try:
        root_info = path.lstat()
    except FileNotFoundError as exc:
        raise TransactionCorruptionError(f"staging source disappeared: {path}") from exc
    if stat.S_ISLNK(root_info.st_mode):
        if kind != "entry":
            raise TransactionError(f"unsafe staged transaction source: {path}")
        return [_staging_tree_entry(path, "", root_info, allow_link=True)]
    if stat.S_ISREG(root_info.st_mode):
        return [_staging_tree_entry(path, "", root_info)]
    if not stat.S_ISDIR(root_info.st_mode) or path.is_symlink():
        raise TransactionError(f"unsafe staged transaction source: {path}")
    entries = [_staging_tree_entry(path, "", root_info)]
    descendants = sorted(
        path.rglob("*"),
        key=lambda entry: _utf8_key(entry.relative_to(path).as_posix()),
    )
    for descendant in descendants:
        entries.append(
            _staging_tree_entry(
                descendant,
                descendant.relative_to(path).as_posix(),
                descendant.lstat(),
            )
        )
    return entries


def _staging_tree_entry(
    path: Path,
    relative_path: str,
    info: os.stat_result,
    *,
    allow_link: bool = False,
) -> StagingTreeEntry:
    if stat.S_ISLNK(info.st_mode):
        if not allow_link or relative_path:
            raise TransactionError(f"unsafe staged transaction entry: {path}")
        try:
            destination = os.readlink(path)
            encoded = destination.encode("utf-8", errors="strict")
        except (OSError, UnicodeError) as exc:
            raise TransactionError(
                f"cannot read staged transaction link: {path}"
            ) from exc
        if not destination or "\x00" in destination:
            raise TransactionError(f"invalid staged transaction link: {path}")
        current = path.lstat()
        if (
            not stat.S_ISLNK(current.st_mode)
            or not os.path.samestat(info, current)
            or os.readlink(path) != destination
        ):
            raise TransactionCorruptionError(
                f"staging entry changed while inspecting: {path}"
            )
        return StagingTreeEntry(
            relative_path=relative_path,
            kind="link",
            mode=0,
            size=len(encoded),
            digest=None,
            link_target=destination,
            link_is_directory=path.is_dir(),
        )
    mode = stat.S_IMODE(info.st_mode)
    if stat.S_ISREG(info.st_mode):
        digest = _entry_content_digest(path, info)
        current = path.lstat()
        if (
            not os.path.samestat(info, current)
            or current.st_size != info.st_size
            or stat.S_IMODE(current.st_mode) != mode
        ):
            raise TransactionCorruptionError(
                f"staging entry changed while inspecting: {path}"
            )
        return StagingTreeEntry(
            relative_path=relative_path,
            kind="file",
            mode=mode,
            size=info.st_size,
            digest=digest,
        )
    if stat.S_ISDIR(info.st_mode) and not path.is_symlink():
        return StagingTreeEntry(
            relative_path=relative_path,
            kind="directory",
            mode=mode,
            size=0,
            digest=None,
        )
    raise TransactionError(f"unsafe staged transaction entry: {path}")


def _staging_construction_mode(entry: StagingTreeEntry) -> int:
    if entry.kind == "link":
        return 0
    if entry.kind == "directory":
        return entry.mode | stat.S_IRUSR | stat.S_IWUSR | stat.S_IXUSR
    return entry.mode | stat.S_IRUSR | stat.S_IWUSR


def _set_entry_mode_durably(
    path: Path,
    entry: StagingTreeEntry | CleanupTreeEntry,
    mode: int,
) -> None:
    if entry.kind == "link":
        _sync_directory(path.parent)
    elif entry.kind == "file":
        no_follow = getattr(os, "O_NOFOLLOW", 0)
        witness_fd = os.open(path, os.O_RDONLY | no_follow)
        sync_fd: int | None = None
        try:
            before = os.fstat(witness_fd)
            try:
                sync_fd = os.open(path, os.O_RDWR | no_follow)
            except PermissionError:
                pass
            path.chmod(mode)
            current = path.lstat()
            if not os.path.samestat(before, current):
                raise TransactionCorruptionError(
                    f"transaction entry changed while setting mode: {path}"
                )
            if sync_fd is None:
                sync_fd = os.open(path, os.O_RDWR | no_follow)
            if not os.path.samestat(before, os.fstat(sync_fd)):
                raise TransactionCorruptionError(
                    f"transaction entry changed while opening for durability: {path}"
                )
            os.fsync(sync_fd)
        finally:
            if sync_fd is not None:
                os.close(sync_fd)
            os.close(witness_fd)
    else:
        path.chmod(mode)
        _sync_directory(path)
    _sync_directory(path.parent)


def _validate_staging_manifest_record(
    expected_digest: str,
    entries: list[StagingTreeEntry],
    *,
    kind: TargetKind,
) -> None:
    if not isinstance(entries, list) or not all(
        isinstance(entry, StagingTreeEntry) for entry in entries
    ):
        raise TransactionCorruptionError("journal staging entries are invalid")
    if expected_digest == ABSENT_DIGEST:
        if entries:
            raise TransactionCorruptionError(
                "absent desired target has staging entries"
            )
        return
    if not entries or entries[0].relative_path != "":
        raise TransactionCorruptionError("staging manifest has no root entry")
    relative_paths: list[str] = []
    by_relative: dict[str, StagingTreeEntry] = {}
    for entry in entries:
        if (
            not isinstance(entry.relative_path, str)
            or entry.kind not in {"file", "directory", "link"}
            or not isinstance(entry.mode, int)
            or isinstance(entry.mode, bool)
            or not 0 <= entry.mode <= 0o7777
            or not isinstance(entry.size, int)
            or isinstance(entry.size, bool)
            or entry.size < 0
        ):
            raise TransactionCorruptionError(
                "journal staging entry metadata is invalid"
            )
        if entry.relative_path:
            relative = PurePosixPath(entry.relative_path)
            if (
                relative.is_absolute()
                or ".." in relative.parts
                or relative.as_posix() != entry.relative_path
            ):
                raise TransactionCorruptionError(
                    "journal staging entry path is invalid"
                )
        if entry.relative_path in by_relative:
            raise TransactionCorruptionError(
                "journal staging entries contain a duplicate path"
            )
        if entry.kind == "file":
            if (
                not isinstance(entry.digest, str)
                or entry.digest == ABSENT_DIGEST
                or entry.link_target is not None
                or entry.link_is_directory is not None
            ):
                raise TransactionCorruptionError(
                    "journal staging file digest is invalid"
                )
            _validate_digest(entry.digest, "staging file digest")
        elif entry.kind == "directory":
            if (
                entry.digest is not None
                or entry.size != 0
                or entry.link_target is not None
                or entry.link_is_directory is not None
            ):
                raise TransactionCorruptionError(
                    "journal staging directory metadata is invalid"
                )
        elif (
            kind != "entry"
            or entry.relative_path != ""
            or entry.mode != 0
            or entry.digest is not None
            or not isinstance(entry.link_target, str)
            or not entry.link_target
            or "\x00" in entry.link_target
            or not isinstance(entry.link_is_directory, bool)
            or entry.size != len(entry.link_target.encode("utf-8", errors="strict"))
        ):
            raise TransactionCorruptionError("journal staging link metadata is invalid")
        relative_paths.append(entry.relative_path)
        by_relative[entry.relative_path] = entry
    if relative_paths != sorted(relative_paths, key=_utf8_key):
        raise TransactionCorruptionError(
            "journal staging entries are not deterministically ordered"
        )
    root = by_relative[""]
    if root.kind in {"file", "link"} and len(entries) != 1:
        raise TransactionCorruptionError(f"staging {root.kind} has descendant entries")
    for relative_path in by_relative:
        if relative_path == "":
            continue
        parent = PurePosixPath(relative_path).parent.as_posix()
        if parent == ".":
            parent = ""
        parent_entry = by_relative.get(parent)
        if parent_entry is None or parent_entry.kind != "directory":
            raise TransactionCorruptionError(
                f"journal staging entry has no directory parent: {relative_path}"
            )


def _validate_staging_record(
    target: JournalTarget,
    key: tuple[str, str],
    *,
    preparing: bool,
) -> None:
    if not isinstance(target.staging_entries, list):
        raise TransactionCorruptionError(f"journal staging entries are invalid: {key}")
    if (
        not isinstance(target.staging_active, bool)
        or not isinstance(target.staging_created, bool)
        or not isinstance(target.staging_finalize_active, bool)
    ):
        raise TransactionCorruptionError(f"journal staging flags are invalid: {key}")
    for value in (
        target.staging_index,
        target.staging_bytes,
        target.staging_write_bytes,
        target.staging_finalize_index,
    ):
        if not isinstance(value, int) or isinstance(value, bool) or value < 0:
            raise TransactionCorruptionError(
                f"journal staging progress is invalid: {key}"
            )
    for digest_value, field in (
        (target.staging_prefix_digest, "staging prefix digest"),
        (target.staging_write_digest, "staging write digest"),
    ):
        if digest_value is not None:
            if not isinstance(digest_value, str) or digest_value == ABSENT_DIGEST:
                raise TransactionCorruptionError(f"journal {field} is invalid: {key}")
            _validate_digest(digest_value, field)

    progress_clear = (
        not target.staging_active
        and not target.staging_created
        and target.staging_bytes == 0
        and target.staging_prefix_digest is None
        and target.staging_write_bytes == 0
        and target.staging_write_digest is None
    )
    finalize_progress_clear = (
        target.staging_finalize_index == 0 and not target.staging_finalize_active
    )
    if target.desired_digest == ABSENT_DIGEST:
        if (
            target.staged_source is not None
            or target.staging_entries
            or target.staging_index != 0
            or not progress_clear
            or not finalize_progress_clear
        ):
            raise TransactionCorruptionError(
                f"removal target has staging progress: {key}"
            )
        return
    if not preparing:
        if (
            target.staged_source is not None
            or target.staging_entries
            or target.staging_index != 0
            or not progress_clear
            or not finalize_progress_clear
        ):
            raise TransactionCorruptionError(
                f"non-preparing target has staging progress: {key}"
            )
        return
    if target.staged_source is None:
        raise TransactionCorruptionError(f"preparing target has no source: {key}")
    source = Path(target.staged_source)
    try:
        canonical_source = _canonical_target_path(
            source,
            kind=target.kind,
            strict=False,
        )
    except TransactionError as exc:
        raise TransactionCorruptionError(
            f"journal staging source is invalid: {key}"
        ) from exc
    if (
        not source.is_absolute()
        or Path(os.path.abspath(source)) != source
        or (canonical_source != source)
    ):
        raise TransactionCorruptionError(f"journal staging source is invalid: {key}")
    _validate_staging_manifest_record(
        target.desired_digest,
        target.staging_entries,
        kind=target.kind,
    )
    entry_count = len(target.staging_entries)
    finalize_count = sum(entry.kind != "link" for entry in target.staging_entries)
    if (
        target.staging_index > entry_count
        or target.staging_finalize_index > finalize_count
    ):
        raise TransactionCorruptionError(f"journal staging index is invalid: {key}")
    if target.staging_index < entry_count and not finalize_progress_clear:
        raise TransactionCorruptionError(
            f"staging mode finalization began before construction completed: {key}"
        )
    if target.staging_index == entry_count:
        if target.staging_active or not progress_clear:
            raise TransactionCorruptionError(
                f"completed staging construction has active copy progress: {key}"
            )
        if (
            target.staging_finalize_active
            and target.staging_finalize_index == finalize_count
        ):
            raise TransactionCorruptionError(
                f"completed staging mode finalization is still active: {key}"
            )
        return
    if not target.staging_active:
        if not progress_clear:
            raise TransactionCorruptionError(
                f"inactive staging target has active progress: {key}"
            )
        return
    entry = target.staging_entries[target.staging_index]
    byte_progress_clear = (
        target.staging_bytes == 0
        and target.staging_prefix_digest is None
        and target.staging_write_bytes == 0
        and target.staging_write_digest is None
    )
    if entry.kind in {"directory", "link"}:
        if not byte_progress_clear:
            raise TransactionCorruptionError(
                f"{entry.kind} staging has byte progress: {key}"
            )
        return
    if not target.staging_created:
        if not byte_progress_clear:
            raise TransactionCorruptionError(
                f"uncreated staging file has byte progress: {key}"
            )
        return
    if target.staging_bytes > entry.size:
        raise TransactionCorruptionError(
            f"staging byte progress exceeds the source: {key}"
        )
    if (target.staging_bytes == 0) != (target.staging_prefix_digest is None):
        raise TransactionCorruptionError(
            f"staging prefix progress is inconsistent: {key}"
        )
    if target.staging_write_bytes == 0:
        if target.staging_write_digest is not None:
            raise TransactionCorruptionError(
                f"staging write progress is inconsistent: {key}"
            )
    else:
        expected_write_bytes = min(
            target.staging_bytes + _STAGING_COPY_CHUNK_SIZE,
            entry.size,
        )
        if (
            target.staging_write_bytes != expected_write_bytes
            or target.staging_write_bytes <= target.staging_bytes
            or target.staging_write_digest is None
        ):
            raise TransactionCorruptionError(
                f"staging write-ahead progress is not canonical: {key}"
            )


def _staging_entry_path(root: Path, relative_path: str) -> Path:
    if not relative_path:
        return root
    return root.joinpath(*PurePosixPath(relative_path).parts)


def _validate_staging_entry(path: Path, expected: StagingTreeEntry) -> None:
    _validate_staging_entry_modes(path, expected, {expected.mode})


def _validate_staging_entry_modes(
    path: Path,
    expected: StagingTreeEntry,
    allowed_modes: set[int],
) -> None:
    try:
        actual = _staging_tree_entry(
            path,
            expected.relative_path,
            path.lstat(),
            allow_link=expected.kind == "link",
        )
    except FileNotFoundError as exc:
        raise TransactionCorruptionError(
            f"recorded staging entry is missing: {path}"
        ) from exc
    if (
        actual.relative_path != expected.relative_path
        or actual.kind != expected.kind
        or actual.size != expected.size
        or actual.digest != expected.digest
        or actual.link_target != expected.link_target
        or actual.mode not in allowed_modes
    ):
        raise TransactionCorruptionError(f"staging entry changed: {path}")


def _validate_preparing_staging_target(target: JournalTarget) -> None:
    if target.desired_digest == ABSENT_DIGEST:
        return
    assert target.staged_path is not None
    staged = Path(target.staged_path)
    try:
        root_info = staged.lstat()
    except FileNotFoundError:
        if (
            target.staging_index != 0
            or target.staging_created
            or target.staging_bytes != 0
            or target.staging_prefix_digest is not None
            or target.staging_write_bytes != 0
            or target.staging_write_digest is not None
            or target.staging_finalize_index != 0
            or target.staging_finalize_active
        ):
            raise TransactionCorruptionError(
                f"preparing target lost recorded staging: "
                f"{target.target_class}/{target.identifier}"
            )
        return

    if (stat.S_ISLNK(root_info.st_mode) and target.kind == "entry") or stat.S_ISREG(
        root_info.st_mode
    ):
        paths = [staged]
    elif stat.S_ISDIR(root_info.st_mode) and not staged.is_symlink():
        paths = [staged, *staged.rglob("*")]
    else:
        raise TransactionCorruptionError(
            f"preparing target has unsafe staging: {staged}"
        )
    positions = {
        entry.relative_path: index for index, entry in enumerate(target.staging_entries)
    }
    finalization_positions = {
        position: finalization_position
        for finalization_position, position in enumerate(
            reversed(
                [
                    index
                    for index, entry in enumerate(target.staging_entries)
                    if entry.kind != "link"
                ]
            )
        )
    }
    seen: set[int] = set()
    for path in paths:
        relative_path = "" if path == staged else path.relative_to(staged).as_posix()
        position = positions.get(relative_path)
        if position is None:
            raise TransactionCorruptionError(
                f"preparing target contains unrecorded staging entry: {path}"
            )
        entry = target.staging_entries[position]
        info = path.lstat()
        if target.staging_index < len(target.staging_entries):
            if position < target.staging_index:
                _validate_staging_entry_modes(
                    path,
                    entry,
                    {_staging_construction_mode(entry)},
                )
            elif position == target.staging_index and target.staging_active:
                _validate_active_staging_entry(target, path, info, entry)
            else:
                raise TransactionCorruptionError(
                    f"preparing target contains staging beyond durable progress: {path}"
                )
        else:
            finalization_position = finalization_positions.get(position)
            if finalization_position is None:
                allowed_modes = {0}
            else:
                if finalization_position < target.staging_finalize_index:
                    allowed_modes = {entry.mode}
                elif (
                    finalization_position == target.staging_finalize_index
                    and target.staging_finalize_active
                ):
                    allowed_modes = {
                        _staging_construction_mode(entry),
                        entry.mode,
                    }
                else:
                    allowed_modes = {_staging_construction_mode(entry)}
            _validate_staging_entry_modes(path, entry, allowed_modes)
        seen.add(position)
    required_entries = min(
        len(target.staging_entries),
        target.staging_index,
    )
    for index in range(required_entries):
        if index not in seen:
            raise TransactionCorruptionError(
                f"preparing target is missing completed staging entry: "
                f"{target.staging_entries[index].relative_path}"
            )
    if (
        target.staging_active
        and target.staging_created
        and target.staging_index not in seen
    ):
        raise TransactionCorruptionError(
            f"preparing target is missing active staging entry: "
            f"{target.target_class}/{target.identifier}"
        )


def _validate_active_staging_entry(
    target: JournalTarget,
    path: Path,
    info: os.stat_result,
    entry: StagingTreeEntry,
) -> None:
    mode = stat.S_IMODE(info.st_mode)
    if entry.kind == "link":
        _validate_staging_entry(path, entry)
        return
    if entry.kind == "directory":
        if (
            not stat.S_ISDIR(info.st_mode)
            or path.is_symlink()
            or (target.staging_created and mode != _staging_construction_mode(entry))
        ):
            raise TransactionCorruptionError(
                f"preparing target contains changed active staging directory: {path}"
            )
        return
    if not stat.S_ISREG(info.st_mode) or path.is_symlink():
        raise TransactionCorruptionError(
            f"preparing target contains changed active staging file: {path}"
        )
    if not target.staging_created:
        if info.st_size != 0:
            raise TransactionCorruptionError(
                f"preparing target contains bytes before durable ownership: {path}"
            )
        return
    if mode != _staging_construction_mode(entry):
        raise TransactionCorruptionError(
            f"preparing target contains changed active staging mode: {path}"
        )
    maximum_size = (
        target.staging_write_bytes
        if target.staging_write_bytes
        else target.staging_bytes
    )
    if not target.staging_bytes <= info.st_size <= maximum_size:
        raise TransactionCorruptionError(
            f"partial staging size is outside durable ownership: {path}"
        )
    assert target.staged_source is not None
    source = _staging_entry_path(
        Path(target.staged_source),
        entry.relative_path,
    )
    _validate_staging_entry(source, entry)
    staged_prefix = _staging_prefix_digest(path, info.st_size)
    source_prefix = _staging_prefix_digest(source, info.st_size)
    _validate_staging_entry(source, entry)
    if staged_prefix != source_prefix:
        raise TransactionCorruptionError(
            f"partial staging bytes changed from durable source prefix: {path}"
        )
    if (
        info.st_size == target.staging_bytes
        and info.st_size > 0
        and staged_prefix != target.staging_prefix_digest
    ):
        raise TransactionCorruptionError(
            f"partial staging bytes changed from durable progress: {path}"
        )
    if (
        target.staging_write_bytes
        and info.st_size == target.staging_write_bytes
        and staged_prefix != target.staging_write_digest
    ):
        raise TransactionCorruptionError(
            f"partial staging bytes changed from write-ahead progress: {path}"
        )


def _staging_prefix_digest(path: Path, length: int) -> str:
    try:
        before = path.lstat()
    except FileNotFoundError as exc:
        raise TransactionCorruptionError(
            f"partial staging entry disappeared: {path}"
        ) from exc
    if not stat.S_ISREG(before.st_mode) or path.is_symlink():
        raise TransactionCorruptionError(
            f"partial staging entry is not a regular file: {path}"
        )
    if length < 0 or length > before.st_size:
        raise TransactionCorruptionError(
            f"partial staging prefix length is invalid: {path}"
        )
    digest = hashlib.sha256(_STAGING_PREFIX_DOMAIN)
    remaining = length
    with path.open("rb") as handle:
        while remaining:
            chunk = handle.read(min(_STAGING_COPY_CHUNK_SIZE, remaining))
            if not chunk:
                raise TransactionCorruptionError(
                    f"partial staging prefix is truncated: {path}"
                )
            digest.update(chunk)
            remaining -= len(chunk)
        after = os.fstat(handle.fileno())
    current = path.lstat()
    if (
        not os.path.samestat(before, after)
        or not os.path.samestat(after, current)
        or after.st_size != before.st_size
        or stat.S_IMODE(after.st_mode) != stat.S_IMODE(before.st_mode)
    ):
        raise TransactionCorruptionError(
            f"partial staging entry changed while reading: {path}"
        )
    return "sha256:" + digest.hexdigest()


def _cleanup_manifest(path: Path, *, kind: TargetKind) -> list[CleanupTreeEntry]:
    try:
        root_info = path.lstat()
    except FileNotFoundError:
        return []
    if stat.S_ISLNK(root_info.st_mode):
        if kind != "entry":
            raise TransactionCorruptionError(f"unsafe cleanup sidecar: {path}")
        return [_cleanup_tree_entry(path, "", root_info, allow_link=True)]
    if stat.S_ISREG(root_info.st_mode):
        return [_cleanup_tree_entry(path, "", root_info)]
    if not stat.S_ISDIR(root_info.st_mode) or path.is_symlink():
        raise TransactionCorruptionError(f"unsafe cleanup sidecar: {path}")
    entries = [_cleanup_tree_entry(path, "", root_info)]
    descendants = sorted(
        path.rglob("*"),
        key=lambda entry: _utf8_key(entry.relative_to(path).as_posix()),
    )
    for descendant in descendants:
        relative = descendant.relative_to(path).as_posix()
        entries.append(
            _cleanup_tree_entry(
                descendant,
                relative,
                descendant.lstat(),
            )
        )
    return entries


def _cleanup_tree_entry(
    path: Path,
    relative_path: str,
    info: os.stat_result,
    *,
    allow_link: bool = False,
) -> CleanupTreeEntry:
    if stat.S_ISLNK(info.st_mode):
        if not allow_link or relative_path:
            raise TransactionCorruptionError(f"unsafe cleanup tree entry: {path}")
        try:
            destination = os.readlink(path)
            destination.encode("utf-8", errors="strict")
        except (OSError, UnicodeError) as exc:
            raise TransactionCorruptionError(
                f"cannot read cleanup link entry: {path}"
            ) from exc
        if not destination or "\x00" in destination:
            raise TransactionCorruptionError(
                f"cleanup link destination is invalid: {path}"
            )
        current = path.lstat()
        if (
            not stat.S_ISLNK(current.st_mode)
            or not os.path.samestat(info, current)
            or os.readlink(path) != destination
        ):
            raise TransactionCorruptionError(
                f"cleanup link changed while inspecting: {path}"
            )
        return CleanupTreeEntry(
            relative_path=relative_path,
            kind="link",
            mode=0,
            digest=None,
            link_target=destination,
        )
    mode = stat.S_IMODE(info.st_mode)
    if stat.S_ISREG(info.st_mode):
        return CleanupTreeEntry(
            relative_path=relative_path,
            kind="file",
            mode=mode,
            digest=_entry_content_digest(path, info),
        )
    if stat.S_ISDIR(info.st_mode) and not path.is_symlink():
        return CleanupTreeEntry(
            relative_path=relative_path,
            kind="directory",
            mode=mode,
            digest=None,
        )
    raise TransactionCorruptionError(f"unsafe cleanup tree entry: {path}")


def _validate_cleanup_manifest_record(
    expected_digest: str,
    entries: list[CleanupTreeEntry],
    *,
    kind: TargetKind,
) -> None:
    if not isinstance(entries, list) or not all(
        isinstance(entry, CleanupTreeEntry) for entry in entries
    ):
        raise TransactionCorruptionError("journal cleanup entries are invalid")
    if expected_digest == ABSENT_DIGEST:
        if entries:
            raise TransactionCorruptionError(
                "absent cleanup sidecar has recorded entries"
            )
        return
    if not entries or entries[0].relative_path != "":
        raise TransactionCorruptionError("cleanup sidecar manifest has no root entry")
    relative_paths: list[str] = []
    by_relative: dict[str, CleanupTreeEntry] = {}
    for entry in entries:
        if (
            not isinstance(entry.relative_path, str)
            or not isinstance(entry.kind, str)
            or entry.kind not in {"file", "directory", "link"}
            or not isinstance(entry.mode, int)
            or isinstance(entry.mode, bool)
            or not 0 <= entry.mode <= 0o7777
        ):
            raise TransactionCorruptionError(
                "journal cleanup entry metadata is invalid"
            )
        if entry.relative_path:
            relative = PurePosixPath(entry.relative_path)
            if (
                relative.is_absolute()
                or ".." in relative.parts
                or relative.as_posix() != entry.relative_path
            ):
                raise TransactionCorruptionError(
                    "journal cleanup entry path is invalid"
                )
        if entry.relative_path in by_relative:
            raise TransactionCorruptionError(
                "journal cleanup entries contain a duplicate path"
            )
        if entry.kind == "file":
            if (
                not isinstance(entry.digest, str)
                or entry.digest == ABSENT_DIGEST
                or entry.link_target is not None
            ):
                raise TransactionCorruptionError(
                    "journal cleanup file digest is invalid"
                )
            _validate_digest(entry.digest, "cleanup file digest")
        elif entry.kind == "directory":
            if entry.digest is not None or entry.link_target is not None:
                raise TransactionCorruptionError(
                    "journal cleanup directory metadata is invalid"
                )
        elif (
            kind != "entry"
            or entry.relative_path != ""
            or entry.mode != 0
            or entry.digest is not None
            or not isinstance(entry.link_target, str)
            or not entry.link_target
            or "\x00" in entry.link_target
        ):
            raise TransactionCorruptionError("journal cleanup link metadata is invalid")
        relative_paths.append(entry.relative_path)
        by_relative[entry.relative_path] = entry
    if relative_paths != sorted(relative_paths, key=_utf8_key):
        raise TransactionCorruptionError(
            "journal cleanup entries are not deterministically ordered"
        )
    root = by_relative[""]
    if root.kind in {"file", "link"} and len(entries) != 1:
        raise TransactionCorruptionError(
            f"journal cleanup {root.kind} has descendant entries"
        )
    for relative_path, entry in by_relative.items():
        if relative_path == "":
            continue
        parent = PurePosixPath(relative_path).parent.as_posix()
        if parent == ".":
            parent = ""
        parent_entry = by_relative.get(parent)
        if parent_entry is None or parent_entry.kind != "directory":
            raise TransactionCorruptionError(
                f"journal cleanup entry has no directory parent: {relative_path}"
            )


def _validate_cleanup_tree(
    path: Path,
    cleanup: JournalCleanupSidecar,
    *,
    kind: TargetKind,
    allow_partial: bool,
) -> None:
    actual_entries = _cleanup_manifest(path, kind=kind)
    expected_by_relative = {entry.relative_path: entry for entry in cleanup.entries}
    allowed_modes = _cleanup_allowed_modes(cleanup)
    for actual in actual_entries:
        expected = expected_by_relative.get(actual.relative_path)
        if expected is None:
            raise TransactionCorruptionError(
                f"unrecorded cleanup bytes: {path / actual.relative_path}"
            )
        if (
            actual.kind != expected.kind
            or actual.digest != expected.digest
            or actual.link_target != expected.link_target
            or actual.mode not in allowed_modes[actual.relative_path]
        ):
            raise TransactionCorruptionError(
                f"changed cleanup bytes: {path / actual.relative_path}"
            )
    if not allow_partial and len(actual_entries) != len(cleanup.entries):
        raise TransactionCorruptionError(f"missing cleanup bytes: {path}")


def _remove_cleanup_tree(
    path: Path,
    cleanup: JournalCleanupSidecar,
    *,
    kind: TargetKind,
    on_removed: Callable[[], None],
) -> None:
    if not _path_exists(path):
        return
    writable_order = _cleanup_writable_order(cleanup.entries)
    if cleanup.writable_index != len(writable_order) or cleanup.writable_active:
        raise TransactionCorruptionError(
            f"cleanup tree is not durably writable: {path}"
        )
    _validate_cleanup_tree(path, cleanup, kind=kind, allow_partial=True)
    expected_by_relative = {entry.relative_path: entry for entry in cleanup.entries}
    allowed_modes = _cleanup_allowed_modes(cleanup)
    root = expected_by_relative[""]
    if root.kind in {"file", "link"}:
        _validate_cleanup_entry_modes(path, root, allowed_modes[""])
        path.unlink()
        _sync_directory(path.parent)
        on_removed()
        return

    remaining = [
        entry for entry in _cleanup_manifest(path, kind=kind) if entry.relative_path
    ]
    remaining.sort(
        key=lambda entry: (
            len(PurePosixPath(entry.relative_path).parts),
            _utf8_key(entry.relative_path),
        ),
        reverse=True,
    )
    for recorded in remaining:
        entry_path = path.joinpath(*PurePosixPath(recorded.relative_path).parts)
        if not _path_exists(entry_path):
            continue
        expected = expected_by_relative[recorded.relative_path]
        _validate_cleanup_entry_modes(
            entry_path,
            expected,
            allowed_modes[recorded.relative_path],
        )
        if recorded.kind == "directory":
            entry_path.rmdir()
        else:
            entry_path.unlink()
        _sync_directory(entry_path.parent)
        on_removed()

    _validate_cleanup_entry_modes(path, root, allowed_modes[""])
    path.rmdir()
    _sync_directory(path.parent)
    on_removed()


def _cleanup_writable_order(
    entries: list[CleanupTreeEntry],
) -> list[CleanupTreeEntry]:
    return sorted(
        (entry for entry in entries if entry.kind != "link"),
        key=lambda entry: (
            len(PurePosixPath(entry.relative_path).parts) if entry.relative_path else 0,
            0 if entry.kind == "directory" else 1,
            _utf8_key(entry.relative_path),
        ),
    )


def _cleanup_writable_mode(entry: CleanupTreeEntry) -> int:
    if entry.kind == "link":
        return 0
    if entry.kind == "directory":
        return entry.mode | stat.S_IRUSR | stat.S_IWUSR | stat.S_IXUSR
    return entry.mode | stat.S_IRUSR | stat.S_IWUSR


def _cleanup_allowed_modes(
    cleanup: JournalCleanupSidecar,
) -> dict[str, set[int]]:
    allowed = {entry.relative_path: {entry.mode} for entry in cleanup.entries}
    order = _cleanup_writable_order(cleanup.entries)
    for index, entry in enumerate(order):
        writable_mode = _cleanup_writable_mode(entry)
        if index < cleanup.writable_index:
            allowed[entry.relative_path] = {writable_mode}
        elif index == cleanup.writable_index and cleanup.writable_active:
            allowed[entry.relative_path] = {entry.mode, writable_mode}
    return allowed


def _cleanup_entry_path(root: Path, relative_path: str) -> Path:
    if not relative_path:
        return root
    return root.joinpath(*PurePosixPath(relative_path).parts)


def _validate_cleanup_entry_modes(
    path: Path,
    expected: CleanupTreeEntry,
    allowed_modes: set[int],
) -> None:
    try:
        actual = _cleanup_tree_entry(
            path,
            expected.relative_path,
            path.lstat(),
            allow_link=expected.kind == "link",
        )
    except FileNotFoundError as exc:
        raise TransactionCorruptionError(
            f"recorded cleanup entry is missing: {path}"
        ) from exc
    if (
        actual.kind != expected.kind
        or actual.digest != expected.digest
        or actual.link_target != expected.link_target
        or actual.mode not in allowed_modes
    ):
        raise TransactionCorruptionError(f"changed cleanup bytes: {path}")


def _canonical_target_path(
    path: Path,
    *,
    kind: TargetKind,
    strict: bool,
) -> Path:
    if kind not in {"bytes", "entry"}:
        raise TransactionError(f"invalid transaction target kind: {kind}")
    try:
        absolute = Path(os.path.abspath(path.expanduser()))
        if kind == "bytes":
            return absolute.resolve(strict=strict)
        if absolute.parent == absolute or not absolute.name:
            raise ValueError("entry target has no final path component")
        parent = absolute.parent.resolve(strict=strict)
        canonical = parent / absolute.name
        if strict:
            canonical.lstat()
        return canonical
    except (OSError, RuntimeError, UnicodeError, ValueError) as exc:
        raise TransactionError(f"cannot resolve transaction target: {path}") from exc


def _validate_namespace_independence(
    manager_home: Path,
    journal_root: Path,
    transaction_id: str,
    targets: list[JournalTarget],
    *,
    corruption: bool,
) -> None:
    namespaces: list[tuple[str, Path, bool]] = [
        ("manager-home lock namespace", manager_home / ".lock", False),
        ("project lock namespace", manager_home / "locks" / "projects", False),
        ("build lock namespace", manager_home / "locks" / "builds", False),
        ("journal state", journal_root, False),
    ]

    def add_mutable_namespace(label: str, path: Path, *, entry: bool) -> None:
        if _is_legacy_home_lock_breaker_namespace(
            manager_home,
            path,
            entry=entry,
        ):
            namespaces.append(("manager-home legacy lock namespace", path, entry))
        namespaces.append((label, path, entry))

    for index, target in enumerate(targets):
        prefix = f"{target.target_class}/{target.identifier}"
        entry = target.kind == "entry"
        expected_paths = {
            "live": Path(target.live_path),
            "backup": _sidecar(
                Path(target.live_path),
                transaction_id,
                index,
                "backup",
            ),
            "rollback": _sidecar(
                Path(target.live_path),
                transaction_id,
                index,
                "rollback",
            ),
        }
        if target.staged_path is not None:
            expected_paths["desired"] = _sidecar(
                Path(target.live_path),
                transaction_id,
                index,
                "desired",
            )
        for role, path in expected_paths.items():
            add_mutable_namespace(f"{prefix} {role}", path, entry=entry)
            if role != "live":
                add_mutable_namespace(
                    f"{prefix} {role} cleanup tomb",
                    _cleanup_tomb(path),
                    entry=entry,
                )

    error_type: type[TransactionError] = (
        TransactionCorruptionError if corruption else TransactionError
    )
    for left_index, (left_label, left_path, left_entry) in enumerate(namespaces):
        for right_label, right_path, right_entry in namespaces[left_index + 1 :]:
            if _namespaces_overlap(
                left_path,
                right_path,
                left_entry=left_entry,
                right_entry=right_entry,
            ):
                raise error_type(
                    "transaction namespace overlap: "
                    f"{left_label} ({left_path}) and "
                    f"{right_label} ({right_path})"
                )


def _namespaces_overlap(
    left: Path,
    right: Path,
    *,
    left_entry: bool,
    right_entry: bool,
) -> bool:
    left_parts = _namespace_parts(left, entry=left_entry)
    right_parts = _namespace_parts(right, entry=right_entry)
    if left_parts == right_parts:
        return True
    if (
        len(left_parts) < len(right_parts)
        and right_parts[: len(left_parts)] == left_parts
    ):
        return True
    if (
        len(right_parts) < len(left_parts)
        and left_parts[: len(right_parts)] == right_parts
    ):
        return True
    try:
        left_info = left.lstat() if left_entry else left.stat()
        right_info = right.lstat() if right_entry else right.stat()
        return os.path.samestat(left_info, right_info)
    except (FileNotFoundError, NotADirectoryError):
        return False
    except OSError as exc:
        raise TransactionError(
            f"cannot validate physical transaction namespaces: {left}, {right}"
        ) from exc


def _is_legacy_home_lock_breaker_namespace(
    manager_home: Path,
    path: Path,
    *,
    entry: bool,
) -> bool:
    home_parts = _namespace_parts(manager_home, entry=False)
    path_parts = _namespace_parts(path, entry=entry)
    return (
        len(path_parts) > len(home_parts)
        and path_parts[: len(home_parts)] == home_parts
        and path_parts[len(home_parts)].startswith(
            _normalize_namespace_component(".lock.stale-")
        )
    )


def _namespace_parts(path: Path, *, entry: bool) -> tuple[str, ...]:
    resolved = _canonical_target_path(
        path,
        kind="entry" if entry else "bytes",
        strict=False,
    )
    return tuple(_normalize_namespace_component(part) for part in resolved.parts)


def _normalize_namespace_component(value: str) -> str:
    if sys.platform == "darwin":
        return unicodedata.normalize("NFC", value).casefold()
    if os.name == "nt":
        return os.path.normcase(unicodedata.normalize("NFC", value))
    return value


def _utf8_key(value: str) -> bytes:
    return value.encode("utf-8", errors="strict")


def _validate_text(value: str, field: str) -> None:
    if not value or "\x00" in value:
        raise TransactionError(f"{field} is invalid")
    _utf8_key(value)


def _validate_digest(value: str, field: str) -> None:
    if value == ABSENT_DIGEST:
        return
    if not re.fullmatch(r"sha256:[0-9a-f]{64}", value):
        raise TransactionError(f"{field} is invalid")


def _validate_generation_path(value: str | None) -> str | None:
    if value is None:
        return None
    path = Path(value)
    if path.is_absolute() or ".." in path.parts or value in {"", "."}:
        raise TransactionError("generation path escapes or does not identify a file")
    return path.as_posix()


def _journal_bytes(journal: Journal) -> bytes:
    return (
        json.dumps(
            asdict(journal), ensure_ascii=False, sort_keys=True, separators=(",", ":")
        ).encode("utf-8")
        + b"\n"
    )


def _journal_from_dict(raw: object) -> Journal:
    if not isinstance(raw, dict) or set(raw) != {
        "schema",
        "transaction_id",
        "project_identity",
        "phase",
        "ordered_target_classes",
        "generation_digests",
        "targets",
        "cleanup_sidecars",
    }:
        raise ValueError("journal fields are invalid")
    targets_raw = raw["targets"]
    if not isinstance(targets_raw, list):
        raise TypeError("journal targets are invalid")
    targets: list[JournalTarget] = []
    expected_target_fields = set(JournalTarget.__dataclass_fields__)
    expected_staging_entry_fields = set(StagingTreeEntry.__dataclass_fields__)
    for value in targets_raw:
        if not isinstance(value, dict) or set(value) != expected_target_fields:
            raise ValueError("journal target fields are invalid")
        staging_entries_raw = value["staging_entries"]
        if not isinstance(staging_entries_raw, list):
            raise TypeError("journal staging entries are invalid")
        staging_entries: list[StagingTreeEntry] = []
        for entry in staging_entries_raw:
            if (
                not isinstance(entry, dict)
                or set(entry) != expected_staging_entry_fields
            ):
                raise ValueError("journal staging entry fields are invalid")
            staging_entries.append(StagingTreeEntry(**entry))
        target_value = dict(value)
        target_value["staging_entries"] = staging_entries
        targets.append(JournalTarget(**target_value))
    cleanup_raw = raw["cleanup_sidecars"]
    if not isinstance(cleanup_raw, list):
        raise TypeError("journal cleanup sidecars are invalid")
    cleanup_sidecars: list[JournalCleanupSidecar] = []
    expected_cleanup_fields = set(JournalCleanupSidecar.__dataclass_fields__)
    expected_entry_fields = set(CleanupTreeEntry.__dataclass_fields__)
    for value in cleanup_raw:
        if not isinstance(value, dict) or set(value) != expected_cleanup_fields:
            raise ValueError("journal cleanup sidecar fields are invalid")
        entries_raw = value["entries"]
        if not isinstance(entries_raw, list):
            raise TypeError("journal cleanup entries are invalid")
        entries: list[CleanupTreeEntry] = []
        for entry in entries_raw:
            if not isinstance(entry, dict) or set(entry) != expected_entry_fields:
                raise ValueError("journal cleanup entry fields are invalid")
            entries.append(CleanupTreeEntry(**entry))
        cleanup_sidecars.append(
            JournalCleanupSidecar(
                target_index=value["target_index"],
                role=value["role"],
                path=value["path"],
                tomb_path=value["tomb_path"],
                expected_digest=value["expected_digest"],
                entries=entries,
                state=value["state"],
                writable_index=value["writable_index"],
                writable_active=value["writable_active"],
            )
        )
    classes = raw["ordered_target_classes"]
    generations = raw["generation_digests"]
    if not isinstance(classes, list) or not all(
        isinstance(value, str) for value in classes
    ):
        raise ValueError("journal classes are invalid")
    if not isinstance(generations, dict) or not all(
        isinstance(key, str) and isinstance(value, str)
        for key, value in generations.items()
    ):
        raise ValueError("journal generations are invalid")
    return Journal(
        schema=_require_str(raw["schema"]),
        transaction_id=_require_str(raw["transaction_id"]),
        project_identity=_require_str(raw["project_identity"]),
        phase=_require_phase(raw["phase"]),
        ordered_target_classes=classes,
        generation_digests=generations,
        targets=targets,
        cleanup_sidecars=cleanup_sidecars,
    )


def _require_str(value: object) -> str:
    if not isinstance(value, str):
        raise TypeError("expected string")
    return value


def _require_phase(value: object) -> Phase:
    if value not in {"preparing", "prepared", "committing", "cleanup", "rolling_back"}:
        raise ValueError("expected phase")
    return value


def _clone_journal(journal: Journal) -> Journal:
    return _journal_from_dict(json.loads(_journal_bytes(journal)))
