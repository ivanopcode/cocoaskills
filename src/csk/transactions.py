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
import uuid
from collections.abc import Callable, Mapping
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Literal, Protocol

ABSENT_DIGEST = "absent"
JOURNAL_SCHEMA = "csk-install-transaction-v1"
_TRANSACTION_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
_DIGEST_DOMAIN = b"csk-transaction-target-v1\0"
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
FaultHook = Callable[[str, "JournalTarget | None"], None]


class TransactionError(Exception):
    pass


class TransactionCorruptionError(TransactionError):
    pass


class HomeLockWitness(Protocol):
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


@dataclass(frozen=True)
class TransactionPlan:
    transaction_id: str
    project_identity: str
    targets: tuple[MutableTarget, ...]
    generation_digests: Mapping[str, str] | None = None


@dataclass
class JournalTarget:
    target_class: str
    identifier: str
    live_path: str
    staged_path: str | None
    backup_path: str
    rollback_path: str
    expected_preimage_digest: str | None
    expected_generation: str | None
    generation_path: str | None
    desired_digest: str
    backup_digest: str | None = None
    state: CommitState = "pending"


@dataclass
class Journal:
    schema: str
    transaction_id: str
    project_identity: str
    phase: Phase
    ordered_target_classes: list[str]
    generation_digests: dict[str, str]
    targets: list[JournalTarget]


class TransactionEngine:
    """Durable generic replacement transactions under a caller-held home lock."""

    def __init__(self, csk_home: Path, *, fault_hook: FaultHook | None = None):
        self.home = csk_home.expanduser().resolve(strict=False)
        self.journal_root = self.home / "state" / "transactions" / "v1"
        self._fault_hook = fault_hook
        self._mutex = threading.Lock()

    def prepare(self, lock: HomeLockWitness, plan: TransactionPlan) -> Journal:
        lock.assert_held()
        with self._mutex:
            journal, sources = self._build_journal(plan)
            path = self._journal_path(journal.transaction_id)
            if path.exists():
                raise TransactionError(
                    f"transaction already exists: {journal.transaction_id}"
                )
            self._save_journal(journal, create=True)
            try:
                for target, source in zip(journal.targets, sources, strict=True):
                    if source is None:
                        continue
                    assert target.staged_path is not None
                    _copy_target(source, Path(target.staged_path))
                    if digest_path(Path(target.staged_path)) != target.desired_digest:
                        raise TransactionCorruptionError(
                            f"staged target changed while preparing: {target.target_class}/{target.identifier}"
                        )
                journal.phase = "prepared"
                self._save_journal(journal)
                self._emit("prepared", None)
                return _clone_journal(journal)
            except Exception:
                self._discard_prepared(journal)
                raise

    def commit(self, lock: HomeLockWitness, transaction_id: str) -> None:
        lock.assert_held()
        with self._mutex:
            journal = self._load_journal(transaction_id)
            self._resume(journal)

    def recover(self, lock: HomeLockWitness) -> None:
        """Recover every home journal, regardless of the initiating project."""
        lock.assert_held()
        with self._mutex:
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
        with self._mutex:
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
        live = Path(target.live_path)
        backup = Path(target.backup_path)
        staged = Path(target.staged_path) if target.staged_path is not None else None

        if target.state == "pending":
            backup_digest = digest_path(backup)
            current = digest_path(live)
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
                    _rename_no_replace(live, backup)
                    _sync_tree(backup)
                    captured = digest_path(backup)
                    if captured != current:
                        raise TransactionCorruptionError(
                            f"target changed while backing up: {target.target_class}/{target.identifier}"
                        )
                target.backup_digest = current
                self._emit("after_backup", target)
                target.state = "backed_up"
                self._save_journal(journal)

        if target.state == "backed_up":
            current = digest_path(live)
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
                if staged is None or digest_path(staged) != target.desired_digest:
                    raise TransactionCorruptionError(
                        f"staged target changed: {target.target_class}/{target.identifier}"
                    )
                _rename_no_replace(staged, live)
                _sync_tree(live)
            self._emit("after_install", target)
            target.state = "committed"
            self._save_journal(journal)
            self._emit("target_committed", target)
            return

        if target.state == "committed":
            if digest_path(live) != target.desired_digest:
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
        live = Path(target.live_path)
        backup = Path(target.backup_path)
        rollback = Path(target.rollback_path)
        backup_digest = digest_path(backup)
        current = digest_path(live)
        rollback_digest = digest_path(rollback)

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
            _rename_no_replace(live, rollback)
            rollback_digest = digest_path(rollback)
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
            if digest_path(live) != ABSENT_DIGEST:
                raise TransactionCorruptionError(
                    f"rollback refused to overwrite live bytes: {target.target_class}/{target.identifier}"
                )
            _rename_no_replace(backup, live)
            _sync_tree(live)
        self._emit("after_restore", target)
        target.state = "rolled_back"
        self._save_journal(journal)

    def _cleanup_committed(self, journal: Journal) -> None:
        for target in journal.targets:
            if digest_path(Path(target.live_path)) != target.desired_digest:
                raise TransactionCorruptionError(
                    f"cleanup refused changed target: {target.target_class}/{target.identifier}"
                )
        self._remove_journal(journal)

    def _verify_expected_at(self, target: JournalTarget, root: Path) -> None:
        if target.expected_preimage_digest is not None:
            actual = digest_path(root)
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
        seen_paths: set[Path] = set()
        for index, target in enumerate(targets):
            _validate_text(target.target_class, "target class")
            _validate_text(target.identifier, "target identifier")
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
            live = target.live_path.expanduser().resolve(strict=False)
            if live in seen_paths:
                raise TransactionError(f"multiple targets share live path: {live}")
            seen_paths.add(live)
            generation_path = _validate_generation_path(target.generation_path)
            desired_digest = ABSENT_DIGEST
            staged: Path | None = None
            source: Path | None = None
            if target.desired_path is not None:
                source = target.desired_path.expanduser().resolve(strict=True)
                if source == live:
                    raise TransactionError(f"target stages from its live path: {live}")
                desired_digest = digest_path(source)
                if desired_digest == ABSENT_DIGEST:
                    raise TransactionError(f"desired target is absent: {source}")
                staged = _sidecar(live, plan.transaction_id, index, "desired")
            backup = _sidecar(live, plan.transaction_id, index, "backup")
            rollback = _sidecar(live, plan.transaction_id, index, "rollback")
            for path in (staged, backup, rollback):
                if path is not None and _path_exists(path):
                    raise TransactionCorruptionError(
                        f"unowned transaction sidecar exists: {path}"
                    )
            records.append(
                JournalTarget(
                    target_class=target.target_class,
                    identifier=target.identifier,
                    live_path=str(live),
                    staged_path=str(staged) if staged is not None else None,
                    backup_path=str(backup),
                    rollback_path=str(rollback),
                    expected_preimage_digest=target.expected_preimage_digest,
                    expected_generation=target.expected_generation,
                    generation_path=generation_path,
                    desired_digest=desired_digest,
                )
            )
            sources.append(source)

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
            ),
            sources,
        )

    def _save_journal(self, journal: Journal, *, create: bool = False) -> None:
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
        self._validate_journal(journal)
        if payload != _journal_bytes(journal):
            raise TransactionCorruptionError(
                f"journal is not canonical: {transaction_id}"
            )
        if deleting:
            self._validate_journal_removal_ready(journal)
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
        seen_live_paths: set[Path] = set()
        for index, target in enumerate(journal.targets):
            if not isinstance(target.target_class, str) or not isinstance(
                target.identifier, str
            ):
                raise TransactionCorruptionError("journal target key is invalid")
            _validate_text(target.target_class, "journal target class")
            _validate_text(target.identifier, "journal target identifier")
            key = (target.target_class, target.identifier)
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
            live = Path(target.live_path)
            if (
                not live.is_absolute()
                or Path(os.path.abspath(live)) != live
                or live in seen_live_paths
            ):
                raise TransactionCorruptionError(f"journal live path is invalid: {key}")
            seen_live_paths.add(live)
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

    def _discard_prepared(self, journal: Journal) -> None:
        self._remove_journal(journal)

    def _discard_sidecars(self, journal: Journal) -> None:
        for target in reversed(journal.targets):
            for raw in (target.staged_path, target.backup_path, target.rollback_path):
                if raw is not None:
                    _remove_path(Path(raw))

    def _remove_journal(self, journal: Journal) -> None:
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
            if _read_bounded_regular(path, limit=16 * 1024 * 1024) != expected:
                raise TransactionCorruptionError(
                    f"journal changed before removal: {journal.transaction_id}"
                )
            try:
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
        self._discard_sidecars(journal)
        tomb.unlink()
        _sync_directory(self.journal_root)

    def _finish_journal_removal(self, journal: Journal) -> None:
        self._remove_journal(journal)

    def _validate_journal_removal_ready(self, journal: Journal) -> None:
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

    def _emit(self, point: str, target: JournalTarget | None) -> None:
        if self._fault_hook is not None:
            self._fault_hook(point, target)


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
    _windows_flush_path(
        path,
        desired_access=_GENERIC_WRITE,
        flags_and_attributes=_FILE_FLAG_OPEN_REPARSE_POINT,
        ignored_flush_errors=frozenset(),
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
    }:
        raise ValueError("journal fields are invalid")
    targets_raw = raw["targets"]
    if not isinstance(targets_raw, list):
        raise TypeError("journal targets are invalid")
    targets: list[JournalTarget] = []
    expected_target_fields = set(JournalTarget.__dataclass_fields__)
    for value in targets_raw:
        if not isinstance(value, dict) or set(value) != expected_target_fields:
            raise ValueError("journal target fields are invalid")
        targets.append(JournalTarget(**value))
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
