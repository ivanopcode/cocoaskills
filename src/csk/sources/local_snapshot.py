"""Pure local-snapshot-v1 inventory and digest primitives.

This module deliberately receives an already-admitted in-memory collection of
``(path, sha256, executable)`` triples. It does not walk a filesystem, hash
bytes, inspect permissions, normalize Unicode, consult locale, or read any
other process or machine state. Capture and admission decide which files and
which execute-bit values reach this seam.

The ``equivalent`` callback is the caller's filesystem-equivalence contract.
For every unordered pair of distinct path strings, this module calls
``equivalent(first_path, second_path)`` once. Pairs are visited in UTF-8
byte order of the sorted inventory with the smaller path first, so the
naming of a collision never depends on caller order. The callback must
return a real ``bool`` and implement the host's symmetric, transitive
path-equivalence relation without relying on this module to probe or
normalize anything. A true result means the two original spellings cannot
coexist and raises ``SourcePathConflictError`` naming both paths. A raising
callback propagates unwrapped; a non-``bool`` return is refused as
``source_path_equivalence_invalid``.

``str`` subclasses are normalized to exact ``str`` by value on admission, so
validation, ordering, collision checks and the digest all read one identity.
"""

from __future__ import annotations

import hashlib
import re
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass
from typing import Final, TypeAlias, TypedDict

from .. import identifiers, protocol_json
from .errors import (
    CODE_INVENTORY_INVALID,
    CODE_PATH_EQUIVALENCE_INVALID,
    SourceError,
    SourcePathConflictError,
)

LOCAL_SNAPSHOT_SCHEMA_VERSION: Final[int] = 1
LOCAL_SNAPSHOT_ALGORITHM: Final[str] = "curator-local-snapshot-v1"
_SHA256_RE: Final[re.Pattern[str]] = re.compile(r"^sha256:[0-9a-f]{64}$")


class InventoryFile(TypedDict):
    path: str
    sha256: str
    executable: bool


class Inventory(TypedDict):
    schema_version: int
    algorithm: str
    files: list[InventoryFile]
    snapshot: str


@dataclass(frozen=True)
class InventoryEntry:
    """Optional named form of the triple accepted by :func:`build_inventory`."""

    path: str
    sha256: str
    executable: bool


Entry: TypeAlias = tuple[str, str, bool] | InventoryEntry
PathEquivalence: TypeAlias = Callable[[str, str], bool]


def build_inventory(
    entries: Iterable[Entry], *, equivalent: PathEquivalence
) -> Inventory:
    """Build the canonical local-snapshot-v1 inventory from admitted triples.

    ``entries`` is consumed once and never mutated. Each item is a tuple of
    package-relative portable path, already-computed ``sha256:<64 lowercase
    hex>`` content digest, and a strict boolean executable flag. The function
    never computes a content digest because raw bytes and filesystem metadata
    are intentionally outside this seam. ``str`` subclasses are accepted by
    value and normalized to exact ``str`` before validation, so every later
    decision (ordering, collision checks, hashing) reads the same bytes.

    ``equivalent`` is called only with the admitted path strings as exact
    ``str``, once for each unordered pair of distinct paths, visited in UTF-8
    byte order with the smaller path first. It must return ``bool`` and model
    the host filesystem's equivalence relation. If it returns ``True``, both
    paths are rejected as a collision; this function never probes the host or
    resolves either spelling. If it raises, the exception propagates
    unwrapped; a non-``bool`` return is refused as
    ``source_path_equivalence_invalid``.
    """
    if not callable(equivalent):
        raise SourceError(
            CODE_PATH_EQUIVALENCE_INVALID,
            "the path equivalence predicate must be callable",
        )

    prepared: list[tuple[bytes, InventoryFile]] = []
    for index, raw_entry in enumerate(entries):
        raw_path, raw_sha256, raw_executable = _unpack_entry(raw_entry, index)
        if not isinstance(raw_path, str):
            raise SourceError(
                CODE_INVENTORY_INVALID,
                f"inventory entry {index} path must be a string",
            )
        if not isinstance(raw_sha256, str):
            raise SourceError(
                CODE_INVENTORY_INVALID,
                f"inventory entry {index} sha256 must be a string for {raw_path!r}",
            )
        if type(raw_executable) is not bool:
            raise SourceError(
                CODE_INVENTORY_INVALID,
                f"inventory entry {index} executable must be a bool for {raw_path!r}",
            )
        # Normalize str subclasses to exact str by value: ==, .encode and
        # __str__ are all overridable, so str() itself cannot be the
        # normalizer -- it honours an overridden __str__, which may even
        # return a still-hostile subclass instance. str.__str__ bypasses the
        # override and returns the exact-str value, so validation, ordering,
        # collision checks and the hash all read one identity.
        path = str.__str__(raw_path)
        sha256 = str.__str__(raw_sha256)
        executable = raw_executable
        path_bytes = _validate_path(path, index)
        _validate_sha256(sha256, index, path)
        _validate_executable(executable, index, path)
        prepared.append(
            (
                path_bytes,
                {
                    "path": path,
                    "sha256": sha256,
                    "executable": executable,
                },
            )
        )

    prepared.sort(key=lambda item: item[0])
    files = [entry for _, entry in prepared]
    _reject_collisions(files, equivalent)

    inventory: Inventory = {
        "schema_version": LOCAL_SNAPSHOT_SCHEMA_VERSION,
        "algorithm": LOCAL_SNAPSHOT_ALGORITHM,
        "files": files,
        "snapshot": "",
    }
    inventory["snapshot"] = inventory_digest(inventory)
    return inventory


def inventory_digest(inventory: Mapping[str, object]) -> str:
    """Return the ``sha256:`` digest of an inventory's CCJ-1 preimage.

    The preimage is exactly ``schema_version``, ``algorithm`` and ``files``;
    the top-level ``snapshot`` member is deliberately ignored so callers can
    recompute or verify it. Unknown members are refused rather than allowed to
    become an accidental identity input. ``str`` subclasses in the mapping are
    normalized by value before validation and hashing. The returned value
    depends only on the ordered file triples in that fixed preimage.
    """
    body = _preimage(inventory)
    canonical = protocol_json.canonical_bytes(body)
    return "sha256:" + hashlib.sha256(canonical).hexdigest()


def _unpack_entry(raw_entry: object, index: int) -> tuple[object, object, object]:
    if isinstance(raw_entry, InventoryEntry):
        return raw_entry.path, raw_entry.sha256, raw_entry.executable
    if isinstance(raw_entry, Sequence) and not isinstance(raw_entry, (str, bytes, bytearray)):
        if len(raw_entry) == 3:
            path, sha256, executable = raw_entry
            return path, sha256, executable
    raise SourceError(
        CODE_INVENTORY_INVALID,
        f"inventory entry {index} must be a (path, sha256, executable) triple",
    )


def _validate_path(path: str, index: int) -> bytes:
    if not identifiers.is_valid_portable_path(path):
        raise SourceError(
            CODE_INVENTORY_INVALID,
            f"inventory entry {index} has a non-portable path: {path!r}",
        )
    try:
        return path.encode("utf-8", errors="strict")
    except UnicodeEncodeError as exc:
        raise SourceError(
            CODE_INVENTORY_INVALID,
            f"inventory entry {index} has a path that is not valid Unicode: {path!r}",
        ) from exc


def _validate_sha256(sha256: str, index: int, path: str) -> None:
    if _SHA256_RE.fullmatch(sha256) is None:
        raise SourceError(
            CODE_INVENTORY_INVALID,
            f"inventory entry {index} has an invalid sha256 for {path!r}",
        )


def _validate_executable(executable: bool, index: int, path: str) -> None:
    if type(executable) is not bool:
        raise SourceError(
            CODE_INVENTORY_INVALID,
            f"inventory entry {index} executable must be a bool for {path!r}",
        )


def _reject_collisions(files: Sequence[InventoryFile], equivalent: PathEquivalence) -> None:
    for left_index, left in enumerate(files):
        for right in files[left_index + 1 :]:
            left_path = left["path"]
            right_path = right["path"]
            if left_path == right_path:
                raise SourcePathConflictError(left_path, right_path)
            result = equivalent(left_path, right_path)
            if type(result) is not bool:
                raise SourceError(
                    CODE_PATH_EQUIVALENCE_INVALID,
                    f"equivalence predicate returned non-bool for {left_path!r} and {right_path!r}",
                )
            if result:
                raise SourcePathConflictError(left_path, right_path)


def _preimage(inventory: Mapping[str, object]) -> dict[str, object]:
    expected = {"schema_version", "algorithm", "files", "snapshot"}
    required = {"schema_version", "algorithm", "files"}
    keys = set(inventory)
    missing = sorted(required - keys)
    unknown = sorted(keys - expected)
    if missing:
        raise SourceError(
            CODE_INVENTORY_INVALID,
            f"inventory is missing field(s): {', '.join(missing)}",
        )
    if unknown:
        raise SourceError(
            CODE_INVENTORY_INVALID,
            f"inventory has unsupported field(s): {', '.join(unknown)}",
        )

    schema_version = inventory["schema_version"]
    if type(schema_version) is not int or schema_version != LOCAL_SNAPSHOT_SCHEMA_VERSION:
        raise SourceError(
            CODE_INVENTORY_INVALID,
            "inventory schema_version must be 1",
        )
    raw_algorithm = inventory["algorithm"]
    if not isinstance(raw_algorithm, str):
        raise SourceError(
            CODE_INVENTORY_INVALID,
            "inventory algorithm must be curator-local-snapshot-v1",
        )
    # Normalize once, then compare and store the same canonical value. Reading
    # the caller value twice around a compare-then-store lets a stateful
    # __str__ pass the gate with the honest value and poison the preimage with
    # the second one (F2 TOCTOU).
    algorithm = str.__str__(raw_algorithm)
    if algorithm != LOCAL_SNAPSHOT_ALGORITHM:
        raise SourceError(
            CODE_INVENTORY_INVALID,
            "inventory algorithm must be curator-local-snapshot-v1",
        )

    raw_files = inventory["files"]
    if not isinstance(raw_files, list):
        raise SourceError(CODE_INVENTORY_INVALID, "inventory files must be a list")
    staged: list[tuple[bytes, InventoryFile]] = []
    for index, raw_file in enumerate(raw_files):
        if not isinstance(raw_file, Mapping):
            raise SourceError(
                CODE_INVENTORY_INVALID,
                f"inventory file {index} must be an object",
            )
        if set(raw_file) != {"path", "sha256", "executable"}:
            raise SourceError(
                CODE_INVENTORY_INVALID,
                f"inventory file {index} has unsupported or missing fields",
            )
        path = raw_file["path"]
        sha256 = raw_file["sha256"]
        executable = raw_file["executable"]
        if not isinstance(path, str):
            raise SourceError(CODE_INVENTORY_INVALID, f"inventory file {index} path must be a string")
        path = str.__str__(path)
        path_bytes = _validate_path(path, index)
        if not isinstance(sha256, str):
            raise SourceError(CODE_INVENTORY_INVALID, f"inventory file {index} sha256 must be a string")
        sha256 = str.__str__(sha256)
        _validate_sha256(sha256, index, path)
        if type(executable) is not bool:
            raise SourceError(
                CODE_INVENTORY_INVALID,
                f"inventory file {index} executable must be a bool for {path!r}",
            )
        staged.append(
            (
                path_bytes,
                {
                    "path": path,
                    "sha256": sha256,
                    "executable": executable,
                },
            )
        )
    staged.sort(key=lambda item: item[0])
    files = [entry for _, entry in staged]
    return {
        "schema_version": schema_version,
        "algorithm": algorithm,
        "files": files,
    }


__all__ = [
    "Entry",
    "Inventory",
    "InventoryEntry",
    "InventoryFile",
    "LOCAL_SNAPSHOT_ALGORITHM",
    "LOCAL_SNAPSHOT_SCHEMA_VERSION",
    "PathEquivalence",
    "build_inventory",
    "inventory_digest",
]
