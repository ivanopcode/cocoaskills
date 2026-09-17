"""The source-v1 snapshot store: stage frozen copies, serve them to consumers.

Epic decision 4 fixes this namespace: ``source-v1`` under the csk home, never
the legacy ``cache/<source>/<commit>`` layout (``csk.snapshot.snapshot_dir``).
The first path component differs, so the two namespaces cannot collide; this
module never imports ``csk.snapshot`` and its on-disk records carry no
``commit`` field, so a snapshot digest (``sha256:``-shaped, like the strings
in the adjacent Git records) has nowhere to be confused into one.

Layout under ``<home>/source-v1/``::

    snapshots/<skill-hex>/<package-hex>/record.json
    snapshots/<skill-hex>/<package-hex>/trees/<digest-hex>/<package files>
    staging/<uuid>/...                  # transient, never served

Key components are SHA-256 hex of the UTF-8 key bytes, so hostile keys
(``..``, separators, ``cache``, case variants of another key) cannot escape
the namespace or conflate with another entry on any host. Tree directories
are named by the snapshot digest they must contain; the record points at one
of them.

Reads are lock-free and create nothing: :func:`lookup_snapshot` verifies the
record, rehashes every tree file, rebuilds the inventory through the
production inventory function and compares the digest. Any failure to serve
the exact locked bytes -- absent record, tampered bytes, unreadable file --
fails ``source_snapshot_unavailable`` and never recreates the snapshot from
live bytes; lookup takes no source path, so recreation is not expressible.

Writes take the manager-home lock (``csk.locking.ManagerHomeLock``, the same
lock the installer uses) and publish by stage-then-rename: bytes land in a
fresh ``staging/<uuid>/`` tree, are re-verified there, then the tree is
renamed into place and the record is replaced atomically (a transient
sharing denial at the replace is retried within a short bound; anything
else refuses immediately). A fault before the record replace rolls
everything back and the store is byte-identical afterwards.

This module is deliberately NOT re-exported from ``csk.sources.__init__``:
the selection import-closure pin
(``test_closure_matches_reviewed_selection_tree``) must keep reviewing
exactly the selection path, and nothing on that path imports this module.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType
from typing import Final, Mapping

from .. import protocol_json
from ..locking import LockError, ManagerHomeLock
from .errors import (
    CODE_SELECTION_INVALID,
    CODE_SNAPSHOT_CHANGED,
    CODE_SNAPSHOT_UNAVAILABLE,
    SourceError,
)
from .local_snapshot import Inventory, build_inventory
from .snapshot import CapturedPackage, FrozenFile, verify_frozen_copy

#: First path component of the store under the csk home (epic decision 4).
STORE_NAMESPACE: Final = "source-v1"

SNAPSHOTS_DIRNAME: Final = "snapshots"
TREES_DIRNAME: Final = "trees"
STAGING_DIRNAME: Final = "staging"
RECORD_FILENAME: Final = "record.json"

#: Record envelope schema tag. Records carry ``snapshot`` digests only;
#: there is intentionally no ``commit`` field to confuse one into.
RECORD_SCHEMA: Final = "csk-source-snapshot-record-v1"
RECORD_VERSION: Final = 1

_RECORD_KEYS: Final = frozenset(
    {"schema", "schema_version", "skill", "package", "snapshot", "inventory"}
)

_SHA256_RE: Final = re.compile(r"^sha256:[0-9a-f]{64}$")

#: Failures that the store maps to structured refusals. Mirrors the tuple
#: the capture layer catches; SourceError is always re-raised before this.
_FS_ERRORS: Final[tuple[type[Exception], ...]] = (
    OSError,
    RuntimeError,
    ValueError,
    UnicodeError,
)

#: Record-replace attempts against a transient sharing denial. Windows has
#: no replace-while-open: a lock-free reader holding ``record.json`` makes
#: the writer's ``os.replace`` fail with ``PermissionError``. Readers hold
#: the record only for one small read, so a short linear backoff outlasts
#: any holder that is already closing (including a descheduled reader on a
#: saturated runner); a still-held file keeps refusing afterwards, and
#: every other failure refuses immediately without retry.
_RECORD_REPLACE_ATTEMPTS: Final = 6
_RECORD_REPLACE_BACKOFF_SECONDS: Final = 0.05


@dataclass(frozen=True)
class StoredSnapshot:
    """One verified frozen copy served from the store.

    ``files`` is an immutable mapping of portable path to frozen bytes plus
    the executable bit from the record. Consumers read these bytes; they
    never receive a path into the store or into the live authored tree.
    """

    skill: str
    package: str
    snapshot: str
    inventory: Inventory
    files: Mapping[str, FrozenFile]


@dataclass(frozen=True)
class _ValidatedRecord:
    snapshot: str
    files: dict[str, tuple[str, bool]]


class _StoreUnavailable(Exception):
    """Internal control flow: the exact locked bytes cannot be served."""


def _exact_equivalent(_left: str, _right: str) -> bool:
    return False


def store_root(home: Path) -> Path:
    """Return the source-v1 store root under one csk home (pure)."""
    return home / STORE_NAMESPACE


def _key_component(value: str, *, kind: str) -> str:
    if type(value) is not str:
        raise TypeError(f"snapshot {kind} must be a string")
    try:
        raw = value.encode("utf-8", errors="strict")
    except UnicodeEncodeError as exc:
        raise SourceError(
            CODE_SELECTION_INVALID,
            f"snapshot {kind} is not valid Unicode",
        ) from exc
    return hashlib.sha256(raw).hexdigest()


def entry_dir(home: Path, skill: str, package: str) -> Path:
    """Return the store entry directory for one (skill, package) key (pure)."""
    return (
        store_root(home)
        / SNAPSHOTS_DIRNAME
        / _key_component(skill, kind="skill name")
        / _key_component(package, kind="package identity")
    )


def _digest_component(digest: str) -> str:
    """Return the hex directory name for one snapshot digest, or refuse."""
    if _SHA256_RE.fullmatch(digest) is None:
        raise SourceError(
            CODE_SNAPSHOT_CHANGED,
            "snapshot digest is malformed",
        )
    return digest[len("sha256:") :]


def _record_path(entry: Path) -> Path:
    return entry / RECORD_FILENAME


def _replace_record(source: Path, destination: Path) -> None:
    """Replace one record file, retrying a transient sharing denial.

    Only ``PermissionError`` is retried: on Windows that is the shape of a
    reader holding the destination open, and the holder always closes
    promptly (one small read, no blocking operation while open). Any other
    failure — absent staging directory, wrong file kind, I/O error —
    propagates immediately and the caller refuses without retry.
    """

    for attempt in range(_RECORD_REPLACE_ATTEMPTS):
        try:
            os.replace(source, destination)
        except PermissionError:
            if attempt + 1 >= _RECORD_REPLACE_ATTEMPTS:
                raise
            time.sleep(_RECORD_REPLACE_BACKOFF_SECONDS * (attempt + 1))
        else:
            return


def _trees_dir(entry: Path) -> Path:
    return entry / TREES_DIRNAME


def _read_record(entry: Path, skill: str, package: str) -> _ValidatedRecord:
    """Read and structurally validate one entry record, failing closed."""
    subject = f"snapshot record for skill {skill!r} package {package!r}"
    path = _record_path(entry)
    if path.is_symlink():
        raise _StoreUnavailable(f"{subject} is a link, not a stored record")
    try:
        with open(path, "rb") as handle:
            raw = handle.read()
    except _FS_ERRORS as exc:
        if not path.exists():
            raise _StoreUnavailable(
                f"snapshot for skill {skill!r} package {package!r} is unavailable: "
                "no locked snapshot is stored"
            ) from exc
        raise _StoreUnavailable(f"{subject} cannot be read: {exc}") from exc
    try:
        text = raw.decode("utf-8", errors="strict")
    except UnicodeError as exc:
        raise _StoreUnavailable(f"{subject} is not valid UTF-8") from exc
    try:
        record: object = json.loads(text)
    except ValueError as exc:
        raise _StoreUnavailable(f"{subject} is not valid JSON") from exc
    if not isinstance(record, dict) or set(record) != set(_RECORD_KEYS):
        raise _StoreUnavailable(f"{subject} has an unsupported shape")
    if record["schema"] != RECORD_SCHEMA:
        raise _StoreUnavailable(f"{subject} names an unsupported schema")
    if type(record["schema_version"]) is not int or record["schema_version"] != RECORD_VERSION:
        raise _StoreUnavailable(f"{subject} names an unsupported record version")
    if record["skill"] != skill or record["package"] != package:
        raise _StoreUnavailable(f"{subject} names a different locked snapshot")
    snapshot = record["snapshot"]
    if not isinstance(snapshot, str) or _SHA256_RE.fullmatch(snapshot) is None:
        raise _StoreUnavailable(f"{subject} names a malformed snapshot digest")
    inventory = record["inventory"]
    if not isinstance(inventory, dict):
        raise _StoreUnavailable(f"{subject} carries a malformed inventory")
    if inventory.get("snapshot") != snapshot:
        raise _StoreUnavailable(f"{subject} disagrees with its own inventory digest")
    files = inventory.get("files")
    if not isinstance(files, list):
        raise _StoreUnavailable(f"{subject} carries a malformed file list")
    wanted: dict[str, tuple[str, bool]] = {}
    for index, item in enumerate(files):
        if not isinstance(item, dict) or set(item) != {"path", "sha256", "executable"}:
            raise _StoreUnavailable(f"{subject} carries a malformed file entry {index}")
        entry_path = item["path"]
        entry_sha = item["sha256"]
        entry_exec = item["executable"]
        if not isinstance(entry_path, str):
            raise _StoreUnavailable(f"{subject} carries a malformed file entry {index}")
        if not isinstance(entry_sha, str) or _SHA256_RE.fullmatch(entry_sha) is None:
            raise _StoreUnavailable(
                f"{subject} carries a malformed file digest {entry_path!r}"
            )
        if type(entry_exec) is not bool:
            raise _StoreUnavailable(
                f"{subject} carries a malformed executable flag {entry_path!r}"
            )
        if entry_path in wanted:
            raise _StoreUnavailable(f"{subject} lists {entry_path!r} twice")
        wanted[entry_path] = (entry_sha, entry_exec)
    return _ValidatedRecord(snapshot=snapshot, files=wanted)


def _walk_tree_files(tree: Path, *, subject: str) -> tuple[dict[str, bytes], set[str]]:
    """Read one stored tree as path bytes, refusing links and special files."""
    found: dict[str, bytes] = {}
    directories: set[str] = set()
    pending: list[Path] = [tree]
    while pending:
        current = pending.pop()
        try:
            with os.scandir(current) as iterator:
                entries = list(iterator)
        except _FS_ERRORS as exc:
            try:
                relative = current.relative_to(tree).as_posix()
            except ValueError:
                relative = current.name
            shown = relative if relative != "." else "."
            raise _StoreUnavailable(
                f"{subject} cannot be listed at {shown!r}: {exc}"
            ) from exc
        for item in entries:
            try:
                is_link = item.is_symlink()
            except _FS_ERRORS as exc:
                raise _StoreUnavailable(f"{subject} cannot be inspected: {exc}") from exc
            if is_link:
                raise _StoreUnavailable(
                    f"{subject} contains a link at {item.name!r}: links are never stored"
                )
            try:
                is_dir = item.is_dir(follow_symlinks=False)
            except _FS_ERRORS as exc:
                raise _StoreUnavailable(f"{subject} cannot be inspected: {exc}") from exc
            if is_dir:
                directories.add(Path(item.path).relative_to(tree).as_posix())
                pending.append(Path(item.path))
                continue
            try:
                is_file = item.is_file(follow_symlinks=False)
            except _FS_ERRORS as exc:
                raise _StoreUnavailable(f"{subject} cannot be inspected: {exc}") from exc
            if not is_file:
                raise _StoreUnavailable(
                    f"{subject} contains a special file at {item.name!r}: "
                    "only regular files are stored"
                )
            relative_path = Path(item.path).relative_to(tree).as_posix()
            if relative_path in found:
                raise _StoreUnavailable(f"{subject} lists {relative_path!r} twice")
            try:
                with open(item.path, "rb") as handle:
                    found[relative_path] = handle.read()
            except _FS_ERRORS as exc:
                raise _StoreUnavailable(
                    f"{subject} cannot be read at {relative_path!r}: {exc}"
                ) from exc
    return found, directories


def _expected_directories(paths: set[str]) -> set[str]:
    expected: set[str] = set()
    for path in paths:
        parts = path.split("/")[:-1]
        for depth in range(1, len(parts) + 1):
            expected.add("/".join(parts[:depth]))
    return expected


def lookup_snapshot(home: Path, skill: str, package: str) -> StoredSnapshot:
    """Serve one locked snapshot's frozen bytes, verified, or refuse.

    A missing or unverifiable locked snapshot fails
    ``source_snapshot_unavailable`` and never recreates the snapshot: this
    function takes no source path, performs no capture, and creates
    nothing -- not even the home directory. Only a non-string key raises
    ``TypeError``; every other failure to serve the exact locked bytes is
    the structured refusal.
    """
    if type(skill) is not str:
        raise TypeError("snapshot skill name must be a string")
    if type(package) is not str:
        raise TypeError("snapshot package identity must be a string")
    try:
        skill_hex = _key_component(skill, kind="skill name")
        package_hex = _key_component(package, kind="package identity")
    except SourceError as exc:
        raise SourceError(
            CODE_SNAPSHOT_UNAVAILABLE,
            f"snapshot for skill {skill!r} package {package!r} is unavailable: "
            "the key cannot name a stored record",
        ) from exc
    entry = store_root(home) / SNAPSHOTS_DIRNAME / skill_hex / package_hex
    subject = f"snapshot for skill {skill!r} package {package!r}"
    try:
        record = _read_record(entry, skill, package)
        tree = _trees_dir(entry) / record.snapshot[len("sha256:") :]
        if tree.is_symlink() or not tree.is_dir():
            raise _StoreUnavailable(
                f"{subject} is unavailable: "
                f"the stored tree for {record.snapshot} is absent"
            )
        found, directories = _walk_tree_files(tree, subject=subject)
        missing = sorted(set(record.files) - set(found))
        if missing:
            raise _StoreUnavailable(f"{subject} is missing stored file {missing[0]!r}")
        extra = sorted(set(found) - set(record.files))
        if extra:
            raise _StoreUnavailable(f"{subject} carries an unrecorded file {extra[0]!r}")
        unexpected_dirs = sorted(directories - _expected_directories(set(record.files)))
        if unexpected_dirs:
            raise _StoreUnavailable(
                f"{subject} carries an unrecorded directory {unexpected_dirs[0]!r}"
            )
        entries: list[tuple[str, str, bool]] = []
        for path in sorted(record.files):
            digest = "sha256:" + hashlib.sha256(found[path]).hexdigest()
            if digest != record.files[path][0]:
                raise _StoreUnavailable(f"{subject} changed on disk at {path!r}")
            entries.append((path, digest, record.files[path][1]))
        try:
            rebuilt = build_inventory(entries, equivalent=_exact_equivalent)
        except SourceError as exc:
            raise _StoreUnavailable(f"{subject} fails inventory verification: {exc}") from exc
        if rebuilt["snapshot"] != record.snapshot:
            raise _StoreUnavailable(f"{subject} fails digest verification")
        files: Mapping[str, FrozenFile] = MappingProxyType(
            {
                path: FrozenFile(
                    path=path, data=found[path], executable=record.files[path][1]
                )
                for path in record.files
            }
        )
        return StoredSnapshot(
            skill=skill,
            package=package,
            snapshot=record.snapshot,
            inventory=rebuilt,
            files=files,
        )
    except _StoreUnavailable as exc:
        raise SourceError(CODE_SNAPSHOT_UNAVAILABLE, str(exc)) from exc


def _created_chain(trees_dir: Path, entry: Path, root: Path) -> list[Path]:
    """Snapshot the chain directories this stage did not find (deepest first).

    Rollback removes exactly these when they are empty afterwards, so a
    pre-existing empty directory (for example ``staging/`` left by an
    earlier successful stage) is never touched.
    """
    candidates = (
        trees_dir,
        entry,
        entry.parent,
        root / SNAPSHOTS_DIRNAME,
        root / STAGING_DIRNAME,
        root,
    )
    return [candidate for candidate in candidates if not candidate.exists()]


def _remove_quietly(path: Path) -> None:
    try:
        if path.is_symlink() or path.is_file():
            path.unlink()
        elif path.is_dir():
            shutil.rmtree(path)
    except OSError:
        pass


def _remove_raising(path: Path) -> None:
    if path.is_symlink() or path.is_file():
        path.unlink()
    elif path.is_dir():
        shutil.rmtree(path)


def _present(path: Path) -> bool:
    return path.exists() or path.is_symlink()


def stage_snapshot(
    home: Path, skill: str, package: str, captured: CapturedPackage
) -> StoredSnapshot:
    """Stage one captured package into the store and serve it back.

    The frozen in-memory copy is verified against the captured digest,
    written to a fresh staging tree, re-verified there, then published by
    rename plus an atomic record replace under the manager-home lock. A
    fault before the record replace rolls everything back and the store is
    byte-identical; a digest mismatch fails ``source_snapshot_changed``
    while an I/O or lock failure fails ``source_snapshot_unavailable``.
    Staging the same bytes twice is idempotent; staging different bytes
    for one key atomically supersedes the old record.
    """
    entry = entry_dir(home, skill, package)
    frozen = captured.frozen_files()
    digest = verify_frozen_copy(
        frozen,
        captured.inventory["snapshot"],
        equivalent=captured.equivalence.equivalent,
    )
    # Well-shaped by construction past verification: the recomputed digest
    # always matches the pattern, and a mismatch already refused above.
    digest_hex = _digest_component(digest)
    try:
        with ManagerHomeLock(home):
            return _stage_locked(home, entry, skill, package, frozen, digest, digest_hex)
    except SourceError:
        raise
    except LockError as exc:
        raise SourceError(
            CODE_SNAPSHOT_UNAVAILABLE,
            f"snapshot for skill {skill!r} package {package!r} is unavailable: "
            f"the store lock cannot be acquired: {exc}",
        ) from exc
    except _FS_ERRORS as exc:
        raise SourceError(
            CODE_SNAPSHOT_UNAVAILABLE,
            f"snapshot for skill {skill!r} package {package!r} is unavailable: "
            f"the store lock cannot be acquired: {exc}",
        ) from exc


def _stage_locked(
    home: Path,
    entry: Path,
    skill: str,
    package: str,
    frozen: dict[str, FrozenFile],
    digest: str,
    digest_hex: str,
) -> StoredSnapshot:
    subject = f"snapshot for skill {skill!r} package {package!r}"
    root = store_root(home)
    staging_root = root / STAGING_DIRNAME / uuid.uuid4().hex
    staged_tree = staging_root / digest_hex
    trees_dir = _trees_dir(entry)
    created_dirs = _created_chain(trees_dir, entry, root)
    final_tree = trees_dir / digest_hex
    moved_aside: Path | None = None
    published_tree: Path | None = None
    record_tmp: Path | None = None
    try:
        try:
            trees_dir.mkdir(parents=True, exist_ok=True)
            staged_tree.mkdir(parents=True, exist_ok=False)
        except _FS_ERRORS as exc:
            raise SourceError(
                CODE_SNAPSHOT_UNAVAILABLE,
                f"{subject} cannot be staged: {exc}",
            ) from exc
        for path in sorted(frozen):
            target = staged_tree / Path(*path.split("/"))
            try:
                target.parent.mkdir(parents=True, exist_ok=True)
                with open(target, "xb") as handle:
                    handle.write(frozen[path].data)
            except _FS_ERRORS as exc:
                raise SourceError(
                    CODE_SNAPSHOT_UNAVAILABLE,
                    f"{subject} cannot be staged at {path!r}: {exc}",
                ) from exc
        try:
            staged_files, _ = _walk_tree_files(staged_tree, subject=subject)
        except _StoreUnavailable as exc:
            raise SourceError(
                CODE_SNAPSHOT_CHANGED,
                f"{subject} changed while staging: {exc}",
            ) from exc
        if set(staged_files) != set(frozen):
            raise SourceError(
                CODE_SNAPSHOT_CHANGED,
                f"{subject} changed while staging: the staged file set differs",
            )
        staged_entries = [
            (
                path,
                "sha256:" + hashlib.sha256(staged_files[path]).hexdigest(),
                frozen[path].executable,
            )
            for path in sorted(frozen)
        ]
        try:
            staged_inventory = build_inventory(staged_entries, equivalent=_exact_equivalent)
        except SourceError as exc:
            raise SourceError(
                CODE_SNAPSHOT_CHANGED,
                f"{subject} changed while staging: {exc}",
            ) from exc
        if staged_inventory["snapshot"] != digest:
            raise SourceError(
                CODE_SNAPSHOT_CHANGED,
                f"{subject} changed while staging: the staged digest differs",
            )
        if _present(final_tree):
            if not _final_tree_matches(final_tree, subject, digest, frozen):
                moved_aside = trees_dir / f".corrupt-{digest_hex}-{uuid.uuid4().hex}"
                try:
                    os.rename(final_tree, moved_aside)
                    os.rename(staged_tree, final_tree)
                except _FS_ERRORS as exc:
                    raise SourceError(
                        CODE_SNAPSHOT_UNAVAILABLE,
                        f"{subject} cannot be published: {exc}",
                    ) from exc
                published_tree = final_tree
        else:
            try:
                os.rename(staged_tree, final_tree)
            except _FS_ERRORS as exc:
                raise SourceError(
                    CODE_SNAPSHOT_UNAVAILABLE,
                    f"{subject} cannot be published: {exc}",
                ) from exc
            published_tree = final_tree
        record_tmp = entry / f".record-{uuid.uuid4().hex}.tmp"
        record: dict[str, object] = {
            "schema": RECORD_SCHEMA,
            "schema_version": RECORD_VERSION,
            "skill": skill,
            "package": package,
            "snapshot": digest,
            "inventory": staged_inventory,
        }
        try:
            payload = protocol_json.canonical_bytes(record)
        except ValueError as exc:
            raise SourceError(
                CODE_SNAPSHOT_UNAVAILABLE,
                f"{subject} cannot be recorded: {exc}",
            ) from exc
        try:
            with open(record_tmp, "xb") as handle:
                handle.write(payload)
            _replace_record(record_tmp, _record_path(entry))
        except _FS_ERRORS as exc:
            raise SourceError(
                CODE_SNAPSHOT_UNAVAILABLE,
                f"{subject} cannot be recorded: {exc}",
            ) from exc
        record_tmp = None
    except BaseException:
        if record_tmp is not None:
            _remove_quietly(record_tmp)
        if published_tree is not None:
            _remove_quietly(published_tree)
        if moved_aside is not None:
            try:
                if not _present(final_tree):
                    os.rename(moved_aside, final_tree)
                else:
                    _remove_quietly(moved_aside)
            except OSError:
                pass
        _remove_quietly(staging_root)
        for candidate in created_dirs:
            try:
                candidate.rmdir()
            except OSError:
                pass
        raise
    try:
        _remove_raising(staging_root)
        if moved_aside is not None:
            _remove_raising(moved_aside)
    except _FS_ERRORS as exc:
        raise SourceError(
            CODE_SNAPSHOT_UNAVAILABLE,
            f"{subject} is published but staging cleanup failed: {exc}",
        ) from exc
    return lookup_snapshot(home, skill, package)


def _final_tree_matches(
    final_tree: Path, subject: str, digest: str, frozen: dict[str, FrozenFile]
) -> bool:
    """Return whether the published tree already holds exactly these bytes."""
    if final_tree.is_symlink() or not final_tree.is_dir():
        return False
    try:
        found, directories = _walk_tree_files(final_tree, subject=subject)
    except _StoreUnavailable:
        return False
    if set(found) != set(frozen):
        return False
    if directories != _expected_directories(set(frozen)):
        return False
    entries = [
        (
            path,
            "sha256:" + hashlib.sha256(found[path]).hexdigest(),
            frozen[path].executable,
        )
        for path in found
    ]
    try:
        rebuilt = build_inventory(entries, equivalent=_exact_equivalent)
    except SourceError:
        return False
    return rebuilt["snapshot"] == digest


__all__ = [
    "RECORD_FILENAME",
    "RECORD_SCHEMA",
    "RECORD_VERSION",
    "SNAPSHOTS_DIRNAME",
    "STAGING_DIRNAME",
    "STORE_NAMESPACE",
    "TREES_DIRNAME",
    "StoredSnapshot",
    "entry_dir",
    "lookup_snapshot",
    "stage_snapshot",
    "store_root",
]
