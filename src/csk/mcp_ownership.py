"""Ownership ledger, consent store and reconcile planner for MCP entries.

The planner is pure logic over opaque inputs (addresses and digests). The two
manager-home stores live under ``<csk home>/state/mcp/{ledger,consent}/``:

* the ledger records which native entries csk wrote, so a later install may
  touch exactly those and nothing else; this module parses and encodes it, the
  install transaction publishes it;
* the consent store records which declarations the operator approved; its
  only write path is :func:`record_consent`.

Both stores are canonical CCJ-1 plus a line feed, read without following links
and refused on any deviation. A ledger that cannot be read is "ownership
unknown", never "owns nothing": only a missing file means the latter.
No secret value ever enters either file; they hold names, addresses, digests
and capability strings.
"""

from __future__ import annotations

import hashlib
import os
import re
import stat
import tempfile
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Final, Literal

from . import locking, protocol_json

SCHEMA_VERSION: Final = 1
CODE_OWNERSHIP_UNKNOWN: Final = "mcp_ownership_unknown"
CODE_ENTRY_CONFLICT: Final = "mcp_entry_conflict"
CODE_ENTRY_DRIFT: Final = "mcp_entry_drift"
CODE_NATIVE_UNREADABLE: Final = "mcp_native_config_unreadable"
CODE_SCOPE_MISMATCH: Final = "mcp_scope_mismatch"
CODE_DECLARATION_INVALID: Final = "mcp_declaration_invalid"
# Every unreadable or invalid store refuses with CODE_OWNERSHIP_UNKNOWN. A merge
# that would outgrow the size limit refuses with CODE_CONSENT_STORE_FULL instead,
# so M3b can report it with its remedy: delete the project's consent file.
CODE_CONSENT_STORE_FULL: Final = "mcp_consent_store_full"

_SCOPES: Final = frozenset({"project"})
_CONSUMER_RE: Final = re.compile(r"p-[0-9a-f]{40}")
_SHA256_RE: Final = re.compile(r"[0-9a-f]{64}")
_STORE_LIMIT: Final = 4 * 1024 * 1024
_CONSUMER_DOMAIN: Final = b"csk-mcp-consumer-v1\0"
_CONSENT_DOMAIN: Final = b"csk-mcp-consent-v1\0"
_LEDGER_FIELDS: Final = frozenset({"schema_version", "consumer", "scope", "entries"})
_ENTRY_FIELDS: Final = frozenset(
    {
        "address",
        "written_sha256",
        "declaration_sha256",
        "capability",
        "transaction_id",
        "created_file",
    }
)
_ADDRESS_FIELDS: Final = frozenset({"agent", "relpath", "container", "name"})
_CONSENT_FIELDS: Final = frozenset({"schema_version", "consumer", "approved"})


class McpOwnershipError(ValueError):
    """A refusal with a stable ``code`` (decisions A8)."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


# --- identity and paths -------------------------------------------------


def consumer_id(project_identity: str, scope: Literal["project"]) -> str:
    """Return the opaque consumer id for ``locking.canonical_project_identity``."""
    if scope not in _SCOPES:
        raise McpOwnershipError(CODE_SCOPE_MISMATCH, f"unsupported consumer scope: {scope!r}")
    try:
        material = (
            _CONSUMER_DOMAIN
            + scope.encode("utf-8")
            + b"\0"
            + project_identity.encode("utf-8", errors="strict")
        )
    except UnicodeEncodeError as exc:
        raise McpOwnershipError(
            CODE_DECLARATION_INVALID, "project identity is not valid UTF-8"
        ) from exc
    return "p-" + hashlib.sha256(material).hexdigest()[:40]


def ledger_path(csk_home: Path, consumer: str) -> Path:
    return _store_path(csk_home, "ledger", consumer)


def consent_path(csk_home: Path, consumer: str) -> Path:
    return _store_path(csk_home, "consent", consumer)


def _store_path(csk_home: Path, store: str, consumer: str) -> Path:
    _require_consumer(consumer)
    return csk_home / "state/mcp" / store / f"{consumer}.json"


def _require_consumer(consumer: str) -> None:
    # The id becomes a file name, so anything but the generated shape is refused
    # before it can name a path.
    if not isinstance(consumer, str) or _CONSUMER_RE.fullmatch(consumer) is None:
        raise McpOwnershipError(CODE_DECLARATION_INVALID, "consumer id is malformed")


# --- ledger -------------------------------------------------------------


@dataclass(frozen=True)
class EntryAddress:
    agent: str
    relpath: str
    container: str
    name: str


@dataclass(frozen=True)
class LedgerEntry:
    address: EntryAddress
    written_sha256: str
    declaration_sha256: str
    capability: str
    transaction_id: str
    created_file: bool


@dataclass(frozen=True)
class Ledger:
    consumer: str
    scope: str
    entries: tuple[LedgerEntry, ...]


@dataclass(frozen=True)
class LedgerAbsent:
    pass


@dataclass(frozen=True)
class LedgerUnknown:
    reason: str


def _address_key(address: EntryAddress) -> tuple[str, str, str, str]:
    return (address.agent, address.relpath, address.name, address.container)


def _unknown(message: str) -> McpOwnershipError:
    return McpOwnershipError(CODE_OWNERSHIP_UNKNOWN, message)


def _text(value: object, label: str) -> str:
    if (
        type(value) is not str
        or not value
        or any(ord(char) < 0x20 or ord(char) == 0x7F for char in value)
    ):
        raise _unknown(f"{label} must be a non-empty string without control characters")
    return value


def _caller_digest(value: object, label: str) -> str:
    if type(value) is not str or _SHA256_RE.fullmatch(value) is None:
        raise McpOwnershipError(CODE_DECLARATION_INVALID, f"{label} must be 64 lowercase hex characters")
    return value


def _digest(value: object, label: str) -> str:
    if type(value) is not str or _SHA256_RE.fullmatch(value) is None:
        raise _unknown(f"{label} must be 64 lowercase hex characters")
    return value


def _relpath(value: object) -> str:
    text = _text(value, "relpath")
    if text.startswith("/") or "\\" in text or ".." in text.split("/") or ":" in text.split("/")[0]:
        raise _unknown("relpath must be a relative POSIX path without parent components")
    return text


def _validated_address(address: EntryAddress) -> EntryAddress:
    return EntryAddress(
        agent=_text(address.agent, "agent"),
        relpath=_relpath(address.relpath),
        container=_text(address.container, "container"),
        name=_text(address.name, "name"),
    )


def _validated_ledger(ledger: Ledger) -> Ledger:
    _require_consumer_or_unknown(ledger.consumer)
    if ledger.scope not in _SCOPES:
        raise _unknown("ledger scope is unsupported")
    seen: set[EntryAddress] = set()
    entries: list[LedgerEntry] = []
    for entry in ledger.entries:
        address = _validated_address(entry.address)
        if address in seen:
            raise _unknown("ledger lists one address twice")
        seen.add(address)
        if type(entry.created_file) is not bool:
            raise _unknown("created_file must be a boolean")
        entries.append(
            LedgerEntry(
                address=address,
                written_sha256=_digest(entry.written_sha256, "written_sha256"),
                declaration_sha256=_digest(entry.declaration_sha256, "declaration_sha256"),
                capability=_text(entry.capability, "capability"),
                transaction_id=_text(entry.transaction_id, "transaction_id"),
                created_file=entry.created_file,
            )
        )
    entries.sort(key=lambda item: _address_key(item.address))
    return Ledger(consumer=ledger.consumer, scope=ledger.scope, entries=tuple(entries))


def _require_consumer_or_unknown(consumer: str) -> None:
    if not isinstance(consumer, str) or _CONSUMER_RE.fullmatch(consumer) is None:
        raise _unknown("consumer id is malformed")


def encode_ledger(ledger: Ledger) -> bytes:
    """Return the deterministic CCJ-1 bytes plus a line feed."""
    checked = _validated_ledger(ledger)
    document: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "consumer": checked.consumer,
        "scope": checked.scope,
        "entries": [
            {
                "address": {
                    "agent": entry.address.agent,
                    "relpath": entry.address.relpath,
                    "container": entry.address.container,
                    "name": entry.address.name,
                },
                "written_sha256": entry.written_sha256,
                "declaration_sha256": entry.declaration_sha256,
                "capability": entry.capability,
                "transaction_id": entry.transaction_id,
                "created_file": entry.created_file,
            }
            for entry in checked.entries
        ],
    }
    return _canonical_line(document)


def parse_ledger(data: bytes, *, expected_consumer: str) -> Ledger:
    """Strictly parse ledger bytes; any deviation raises ``mcp_ownership_unknown``."""
    if isinstance(data, bytes) and len(data) > _STORE_LIMIT:
        raise _unknown("store exceeds the size limit")
    document = _decode_canonical(data)
    if set(document) != _LEDGER_FIELDS:
        raise _unknown("ledger has missing or unknown fields")
    _require_schema(document)
    consumer = document["consumer"]
    if consumer != expected_consumer:
        raise _unknown("ledger belongs to a different consumer")
    raw_entries = document["entries"]
    if type(raw_entries) is not list:
        raise _unknown("ledger entries must be a list")
    entries: list[LedgerEntry] = []
    for raw in raw_entries:
        if type(raw) is not dict or set(raw) != _ENTRY_FIELDS:
            raise _unknown("ledger entry has missing or unknown fields")
        raw_address = raw["address"]
        if type(raw_address) is not dict or set(raw_address) != _ADDRESS_FIELDS:
            raise _unknown("ledger address has missing or unknown fields")
        created = raw["created_file"]
        if type(created) is not bool:
            raise _unknown("created_file must be a boolean")
        entries.append(
            LedgerEntry(
                address=EntryAddress(
                    agent=raw_address["agent"],
                    relpath=raw_address["relpath"],
                    container=raw_address["container"],
                    name=raw_address["name"],
                ),
                written_sha256=raw["written_sha256"],
                declaration_sha256=raw["declaration_sha256"],
                capability=raw["capability"],
                transaction_id=raw["transaction_id"],
                created_file=created,
            )
        )
    scope = document["scope"]
    if type(scope) is not str:
        raise _unknown("ledger scope must be a string")
    ledger = _validated_ledger(Ledger(consumer=expected_consumer, scope=scope, entries=tuple(entries)))
    # Re-encoding rejects every spelling the encoder would not have produced:
    # entry order, whitespace, key order, a missing line feed.
    if encode_ledger(ledger) != data:
        raise _unknown("ledger is not in canonical form")
    return ledger


def load_ledger(csk_home: Path, consumer: str) -> Ledger | LedgerAbsent | LedgerUnknown:
    """Load the ledger without following links; read failures are never absence."""
    try:
        data = _read_store(csk_home, "ledger", consumer)
        if data is None:
            return LedgerAbsent()
        return parse_ledger(data, expected_consumer=consumer)
    except McpOwnershipError as exc:
        return LedgerUnknown(reason=str(exc))


# --- reconcile planner --------------------------------------------------


@dataclass(frozen=True)
class Desired:
    address: EntryAddress
    entry_sha256: str
    declaration_sha256: str
    capability: str


ObservedState = Literal["absent", "present", "surface_absent", "unreadable", "malformed"]
ActionKind = Literal["add", "restore", "replace", "remove", "keep", "skip_drift", "release", "forget"]


@dataclass(frozen=True)
class Observed:
    address: EntryAddress
    state: ObservedState
    entry_sha256: str | None


@dataclass(frozen=True)
class Action:
    kind: ActionKind
    address: EntryAddress


@dataclass(frozen=True)
class Refusal:
    code: str
    address: EntryAddress
    detail: str


@dataclass(frozen=True)
class Warning:  # noqa: A001 - public name frozen by the leaf brief
    code: str
    address: EntryAddress
    detail: str


@dataclass(frozen=True)
class ReconcilePlan:
    actions: tuple[Action, ...]
    refusals: tuple[Refusal, ...]
    warnings: tuple[Warning, ...]

    @property
    def executable(self) -> bool:
        return not self.refusals


def reconcile(
    desired: Iterable[Desired],
    observed: Mapping[EntryAddress, Observed],
    ledger: Ledger | LedgerAbsent | LedgerUnknown,
) -> ReconcilePlan:
    """Plan the native-entry changes; all-or-nothing when any refusal exists."""
    wanted: dict[EntryAddress, Desired] = {}
    for item in desired:
        if item.address in wanted:
            raise McpOwnershipError(CODE_DECLARATION_INVALID, "one address is desired twice")
        wanted[item.address] = item
    for address in wanted:
        _observation(observed, address)

    if isinstance(ledger, LedgerUnknown):
        # Nothing is known about what csk owns, so nothing may be written or removed.
        refusals = tuple(
            Refusal(CODE_OWNERSHIP_UNKNOWN, address, f"ownership ledger is unknown: {ledger.reason}")
            for address in sorted(wanted, key=_address_key)
        )
        return ReconcilePlan(actions=(), refusals=refusals, warnings=())

    owned: dict[EntryAddress, LedgerEntry] = (
        {entry.address: entry for entry in ledger.entries} if isinstance(ledger, Ledger) else {}
    )
    actions: list[Action] = []
    refusals_out: list[Refusal] = []
    warnings: list[Warning] = []

    for address in sorted(set(wanted) | set(owned), key=_address_key):
        seen = _observation(observed, address)
        want = wanted.get(address)
        entry = owned.get(address)
        if seen.state in ("unreadable", "malformed"):
            refusals_out.append(
                Refusal(CODE_NATIVE_UNREADABLE, address, f"native config is {seen.state}")
            )
            continue
        present = seen.state == "present"
        if want is not None:
            if not present:
                actions.append(Action("restore" if entry is not None else "add", address))
            elif entry is None:
                # D6: an equal foreign entry is still foreign.
                refusals_out.append(
                    Refusal(CODE_ENTRY_CONFLICT, address, "a native entry with this name already exists")
                )
            elif seen.entry_sha256 == entry.written_sha256:
                same = want.entry_sha256 == entry.written_sha256
                actions.append(Action("keep" if same else "replace", address))
            else:
                actions.append(Action("skip_drift", address))
                warnings.append(
                    Warning(CODE_ENTRY_DRIFT, address, "owned entry was modified; left untouched")
                )
        elif entry is not None:
            if not present:
                actions.append(Action("forget", address))
            elif seen.entry_sha256 == entry.written_sha256:
                actions.append(Action("remove", address))
            else:
                actions.append(Action("release", address))
                warnings.append(
                    Warning(CODE_ENTRY_DRIFT, address, "modified entry is no longer managed; left untouched")
                )
    return ReconcilePlan(
        actions=tuple(actions), refusals=tuple(refusals_out), warnings=tuple(warnings)
    )


def _observation(observed: Mapping[EntryAddress, Observed], address: EntryAddress) -> Observed:
    try:
        seen = observed[address]
    except KeyError:
        raise KeyError(f"no observation for {address}") from None
    if seen.address != address:
        raise ValueError(f"observation is keyed under a different address: {address}")
    if seen.state != "present" and seen.entry_sha256 is not None:
        raise ValueError(f"only a present entry can carry a digest: {address}")
    return seen


def next_ledger(
    consumer: str,
    scope: str,
    previous: Ledger | LedgerAbsent,
    plan: ReconcilePlan,
    desired: Iterable[Desired],
    transaction_id: str,
    created_files: Iterable[tuple[str, str]],
) -> Ledger:
    """Return the ledger that follows from executing ``plan``."""
    if isinstance(previous, LedgerUnknown):
        raise _unknown(f"cannot derive a ledger from an unknown one: {previous.reason}")
    if not plan.executable:
        raise McpOwnershipError(
            plan.refusals[0].code, "a plan with refusals has no next ledger"
        )
    if isinstance(previous, Ledger):
        if previous.consumer != consumer or previous.scope != scope:
            raise _unknown("previous ledger belongs to a different consumer")
        entries = {entry.address: entry for entry in previous.entries}
    else:
        entries = {}
    wanted = {item.address: item for item in desired}
    created = set(created_files)
    for action in plan.actions:
        address = action.address
        old = entries.get(address)
        if action.kind in ("add", "restore", "replace"):
            want = wanted.get(address)
            if want is None:
                raise McpOwnershipError(CODE_DECLARATION_INVALID, "planned write has no declaration")
            entries[address] = LedgerEntry(
                address=address,
                written_sha256=want.entry_sha256,
                declaration_sha256=want.declaration_sha256,
                capability=want.capability,
                transaction_id=transaction_id,
                created_file=(address.agent, address.relpath) in created
                or (old is not None and old.created_file),
            )
        elif action.kind in ("keep", "skip_drift"):
            if old is None:
                raise _unknown("plan keeps an entry the ledger does not own")
        else:
            if old is None:
                raise _unknown("plan drops an entry the ledger does not own")
            del entries[address]
    return _validated_ledger(Ledger(consumer=consumer, scope=scope, entries=tuple(entries.values())))


# --- consent ------------------------------------------------------------


@dataclass(frozen=True)
class ConsentSubject:
    name: str
    agents: tuple[str, ...]
    scope: str
    declaration_sha256: str
    capabilities: tuple[str, ...]


def consent_key(consumer: str, subject: ConsentSubject) -> str:
    """Return the digest an approval is bound to.

    Every component of the subject and the consumer is part of the key, so a
    wider agent set, a new declaration digest or a changed writer capability
    needs a fresh approval. Agents and capabilities are sets: their order does
    not matter.
    """
    _require_consumer(consumer)
    document = {
        "consumer": consumer,
        "name": subject.name,
        "agents": sorted(set(subject.agents)),
        "scope": subject.scope,
        "declaration_sha256": _caller_digest(subject.declaration_sha256, "declaration digest"),
        "capabilities": sorted(set(subject.capabilities)),
    }
    try:
        payload = protocol_json.canonical_bytes(document)
    except protocol_json.ProtocolJSONError as exc:
        raise McpOwnershipError(CODE_DECLARATION_INVALID, f"consent subject is not encodable: {exc}") from exc
    return hashlib.sha256(_CONSENT_DOMAIN + payload).hexdigest()


def encode_consent(consumer: str, approved: Iterable[str]) -> bytes:
    _require_consumer_or_unknown(consumer)
    keys = sorted({_digest(key, "consent key") for key in approved})
    return _canonical_line(
        {"schema_version": SCHEMA_VERSION, "consumer": consumer, "approved": keys}
    )


def parse_consent(data: bytes, *, expected_consumer: str) -> frozenset[str]:
    if isinstance(data, bytes) and len(data) > _STORE_LIMIT:
        raise _unknown("store exceeds the size limit")
    document = _decode_canonical(data)
    if set(document) != _CONSENT_FIELDS:
        raise _unknown("consent store has missing or unknown fields")
    _require_schema(document)
    if document["consumer"] != expected_consumer:
        raise _unknown("consent store belongs to a different consumer")
    raw = document["approved"]
    if type(raw) is not list:
        raise _unknown("approved must be a list")
    keys = [_digest(item, "consent key") for item in raw]
    approved = frozenset(keys)
    if len(approved) != len(keys):
        raise _unknown("consent store lists one key twice")
    if encode_consent(expected_consumer, approved) != data:
        raise _unknown("consent store is not in canonical form")
    return approved


def load_consent(csk_home: Path, consumer: str) -> frozenset[str]:
    """Return the approved keys; a missing store is empty, an unreadable one raises."""
    data = _read_store(csk_home, "consent", consumer)
    if data is None:
        return frozenset()
    return parse_consent(data, expected_consumer=consumer)


def record_consent(csk_home: Path, consumer: str, keys: Iterable[str]) -> None:
    """Merge ``keys`` into the approved set with a private atomic write.

    Callers invoke this only after an interactive approval; the module has no
    other write path. The merge is a read-modify-write, so it runs under the
    manager-home lock to keep concurrent approvals from dropping each other's
    keys. That lock is not reentrant: call this outside any held manager-home
    lock (a project lock may be held). An unreadable or invalid existing store
    refuses and is left untouched, as does a merge that would exceed the size
    limit: that refusal carries ``mcp_consent_store_full`` and leaves the
    existing file byte-identical, so this writer never publishes a store the
    loader would refuse.
    """
    _require_consumer(consumer)
    added = frozenset(_caller_digest(key, "consent key") for key in keys)
    with locking.ManagerHomeLock(csk_home):
        existing = load_consent(csk_home, consumer)
        merged = existing | added
        if merged == existing:
            return
        encoded = encode_consent(consumer, merged)
        if len(encoded) > _STORE_LIMIT:
            raise McpOwnershipError(
                CODE_CONSENT_STORE_FULL,
                "consent store is full: the merged store would exceed the size limit",
            )
        directory = _ensure_private_directory(csk_home)
        _write_private_atomic(directory, f"{consumer}.json", encoded)


def unapproved(
    consumer: str, subjects: Iterable[ConsentSubject], approved: frozenset[str]
) -> tuple[ConsentSubject, ...]:
    return tuple(subject for subject in subjects if consent_key(consumer, subject) not in approved)


# --- store plumbing -----------------------------------------------------


def _canonical_line(document: dict[str, Any]) -> bytes:
    try:
        return protocol_json.canonical_bytes(document) + b"\n"
    except protocol_json.ProtocolJSONError as exc:
        raise _unknown(f"document is not CCJ-1 representable: {exc}") from exc


def _decode_canonical(data: bytes) -> dict[str, Any]:
    if not isinstance(data, bytes) or not data.endswith(b"\n"):
        raise _unknown("store must be CCJ-1 bytes followed by one line feed")
    try:
        document = protocol_json.loads_canonical(data)
    except protocol_json.ProtocolJSONError as exc:
        raise _unknown(f"store is not valid CCJ-1: {exc}") from exc
    if type(document) is not dict:
        raise _unknown("store must be a JSON object")
    if protocol_json.canonical_bytes(document) + b"\n" != data:
        raise _unknown("store is not in canonical form")
    return document


def _require_schema(document: dict[str, Any]) -> None:
    version = document["schema_version"]
    if type(version) is not int or version != SCHEMA_VERSION:
        raise _unknown("unsupported schema_version")


def _is_link(info: os.stat_result) -> bool:
    attributes = getattr(info, "st_file_attributes", 0)
    return stat.S_ISLNK(info.st_mode) or bool(attributes & stat.FILE_ATTRIBUTE_REPARSE_POINT)


def _read_store(csk_home: Path, store: str, consumer: str) -> bytes | None:
    """Read one store file; ``None`` only when it is genuinely absent."""
    _require_consumer(consumer)
    parts = ("state", "mcp", store, f"{consumer}.json")
    current = csk_home
    info: os.stat_result | None = None
    for index, part in enumerate(parts):
        current = current / part
        try:
            info = os.lstat(current)
        except FileNotFoundError:
            return None
        except OSError as exc:
            raise _unknown(f"cannot inspect {part}: {exc.strerror or exc}") from exc
        if _is_link(info):
            raise _unknown(f"{part} is a link or reparse point")
        expect_dir = index < len(parts) - 1
        if expect_dir != stat.S_ISDIR(info.st_mode) or (not expect_dir and not stat.S_ISREG(info.st_mode)):
            raise _unknown(f"{part} has an unexpected file type")
    assert info is not None
    if info.st_size > _STORE_LIMIT:
        raise _unknown("store exceeds the size limit")
    flags = (
        os.O_RDONLY
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_NOFOLLOW", 0)
        | getattr(os, "O_BINARY", 0)
    )
    try:
        descriptor = os.open(current, flags)
    except OSError as exc:
        raise _unknown(f"cannot open store: {exc.strerror or exc}") from exc
    try:
        opened = os.fstat(descriptor)
        if not stat.S_ISREG(opened.st_mode) or not os.path.samestat(info, opened):
            raise _unknown("store changed while opening")
        with os.fdopen(descriptor, "rb", closefd=False) as handle:
            payload = handle.read(_STORE_LIMIT + 1)
    except OSError as exc:
        raise _unknown(f"cannot read store: {exc.strerror or exc}") from exc
    finally:
        os.close(descriptor)
    if len(payload) > _STORE_LIMIT:
        raise _unknown("store exceeds the size limit")
    return payload


def _ensure_private_directory(csk_home: Path) -> Path:
    """Create the MCP-owned directories (0700 on POSIX), refusing any link on the way.

    ``state`` belongs to other subsystems: it is created when missing, but an
    existing one keeps its mode and owner untouched. ``state/mcp`` and the two
    store directories are MCP-owned: missing ones are created 0700, existing ones
    owned by the current user are narrowed to 0700 when broader, and existing ones
    owned by someone else are refused. Narrowing and ownership are POSIX-only; on
    Windows the manager-home provisioning covers privacy.
    """
    _ensure_component(csk_home / "state", "state", owned=False)
    mcp = csk_home / "state" / "mcp"
    _ensure_component(mcp, "mcp", owned=True)
    _ensure_component(mcp / "consent", "consent", owned=True)
    _ensure_component(mcp / "ledger", "ledger", owned=True)
    return mcp / "consent"


def _ensure_component(current: Path, part: str, *, owned: bool) -> None:
    try:
        info = os.lstat(current)
    except FileNotFoundError:
        try:
            current.mkdir(mode=0o700)
        except OSError as exc:
            raise _unknown(f"cannot create {part}: {exc.strerror or exc}") from exc
        if os.name != "nt":
            os.chmod(current, 0o700)
        return
    except OSError as exc:
        raise _unknown(f"cannot inspect {part}: {exc.strerror or exc}") from exc
    if _is_link(info) or not stat.S_ISDIR(info.st_mode):
        raise _unknown(f"{part} is not a plain directory")
    if owned:
        _narrow_owned_directory(current, part, info)


def _narrow_owned_directory(path: Path, part: str, info: os.stat_result) -> None:
    """Narrow one MCP-owned directory to 0700 on POSIX; refuse a foreign owner."""
    if os.name == "nt":
        return
    if info.st_uid != os.geteuid():
        raise _unknown(f"{part} is not owned by the current user")
    if stat.S_IMODE(info.st_mode) & 0o077:
        os.chmod(path, 0o700)


def _write_private_atomic(directory: Path, name: str, payload: bytes) -> None:
    descriptor, temporary_name = tempfile.mkstemp(prefix=".consent-", suffix=".tmp", dir=directory)
    temporary = Path(temporary_name)
    try:
        try:
            os.fchmod(descriptor, 0o600)
        except (AttributeError, OSError):
            pass  # mkstemp already creates 0600; Windows has no fchmod
        with os.fdopen(descriptor, "wb") as handle:
            descriptor = -1
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, directory / name)
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        temporary.unlink(missing_ok=True)
    if os.name != "nt":
        directory_fd = os.open(directory, os.O_RDONLY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
