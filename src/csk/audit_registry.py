from __future__ import annotations

import base64
import binascii
import hashlib
import json
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from . import _ed25519
from .config import RegistryConfig
from .identifiers import is_valid_identifier, is_valid_portable_path


# Advisory lookup cache lifetime and the window a stale cache stays usable
# while a registry is unreachable.
DEFAULT_CACHE_TTL_SECONDS = 3600
DEFAULT_OFFLINE_GRACE_SECONDS = 7 * 24 * 3600
# A snapshot older than this is stale, which defends against a registry that
# freezes its view to hide a newer revocation.
DEFAULT_SNAPSHOT_MAX_AGE_SECONDS = 7 * 24 * 3600
DEFAULT_SNAPSHOT_CLOCK_SKEW_SECONDS = 5 * 60
MAX_SAFE_INTEGER = 9_007_199_254_740_991
MAX_RESPONSE_BYTES = 16 * 1024 * 1024
MAX_PAGE_SIZE = 1000
MAX_RECORDS_PER_QUERY = 10_000


# Client side of the audit registry (RFC 0008). This module verifies signed
# audit records against out-of-band pinned keys and combines results from
# several trusted registries under a deny-wins rule. It performs no network
# access itself; a fetch callable supplies raw record payloads, so the network
# transport and the trust logic stay separate and testable.

STATUS_AUDITED = "audited"
STATUS_REVOKED = "revoked"
STATUS_DEPRECATED = "deprecated"
STATUS_PENDING = "pending"
_STATUSES = {STATUS_AUDITED, STATUS_REVOKED, STATUS_DEPRECATED, STATUS_PENDING}

# Result of combining every trusted registry.
RESULT_REVOKED = "revoked"
RESULT_AUDITED = "audited"
RESULT_DEPRECATED = "deprecated"
RESULT_UNKNOWN = "unknown"


class RegistryError(Exception):
    pass


def _object_without_duplicates(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise RegistryError(f"duplicate JSON object key: {key!r}")
        result[key] = value
    return result


def _parse_json_integer(value: str) -> int:
    parsed = int(value)
    if not -MAX_SAFE_INTEGER <= parsed <= MAX_SAFE_INTEGER:
        raise RegistryError(f"JSON integer outside the safe range: {value}")
    return parsed


def _reject_json_number(value: str) -> None:
    raise RegistryError(f"protocol JSON does not allow non-integer number {value!r}")


def load_protocol_json(raw: bytes | str) -> Any:
    """Decode the protocol JSON subset without losing signed-value precision."""
    if (isinstance(raw, bytes) and raw.startswith(b"\xef\xbb\xbf")) or (
        isinstance(raw, str) and raw.startswith("\ufeff")
    ):
        raise RegistryError("protocol JSON must not contain a byte-order mark")
    try:
        value = json.loads(
            raw,
            object_pairs_hook=_object_without_duplicates,
            parse_int=_parse_json_integer,
            parse_float=_reject_json_number,
            parse_constant=_reject_json_number,
        )
    except RegistryError:
        raise
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise RegistryError(f"registry returned invalid JSON: {exc}") from exc
    _validate_ccj(value)
    return value


@dataclass(frozen=True)
class Record:
    name: str
    source_identity: str
    commit: str
    content_sha256: str
    status: str
    audit: dict[str, Any]
    raw: dict[str, Any]

    @property
    def key_id(self) -> str | None:
        sig = self.raw.get("sig")
        return sig.get("key_id") if isinstance(sig, dict) else None


@dataclass(frozen=True)
class Attestation:
    registry: str
    status: str
    key_id: str | None
    record: dict[str, Any]


@dataclass(frozen=True)
class Resolution:
    result: str
    attestation: Attestation | None
    warnings: tuple[str, ...] = ()


# fetch(url, source_identity, commit, content_sha256) -> list of raw record dicts
FetchFn = Callable[[str, str, str, str], list[dict[str, Any]]]


def parse_record(payload: dict[str, Any]) -> Record:
    if not isinstance(payload, dict):
        raise RegistryError("audit record must be a JSON object")
    allowed = {
        "schema_version",
        "name",
        "source_identity",
        "commit",
        "content_sha256",
        "status",
        "audit",
        "endorsements",
        "sig",
    }
    unknown = set(payload) - allowed
    if unknown:
        raise RegistryError(f"audit record has unknown fields: {', '.join(sorted(unknown))}")
    if payload.get("schema_version", 1) != 1:
        raise RegistryError("audit record schema_version must be 1")
    for key in ("name", "source_identity", "commit", "content_sha256", "status"):
        if not isinstance(payload.get(key), str) or not payload[key]:
            raise RegistryError(f"audit record requires a non-empty string {key!r}")
    if not is_valid_identifier(payload["name"]):
        raise RegistryError("audit record name is not a portable identifier")
    identity_host, separator, identity_path = payload["source_identity"].partition("/")
    if (
        not separator
        or re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9.-]*", identity_host) is None
        or not is_valid_portable_path(identity_path)
    ):
        raise RegistryError("audit record source_identity is not canonical")
    if re.fullmatch(r"[0-9a-f]{40}(?:[0-9a-f]{24})?", payload["commit"]) is None:
        raise RegistryError("audit record commit must be a full lowercase object id")
    if re.fullmatch(r"sha256:[0-9a-f]{64}", payload["content_sha256"]) is None:
        raise RegistryError("audit record content_sha256 is malformed")
    if payload["status"] not in _STATUSES:
        raise RegistryError(f"audit record status {payload['status']!r} is not recognized")
    audit = payload.get("audit", {})
    if not isinstance(audit, dict):
        raise RegistryError("audit record field 'audit' must be an object")
    if not isinstance(payload.get("sig"), dict):
        raise RegistryError("audit record requires a signature envelope")
    _signature_envelope(payload["sig"])
    endorsements = payload.get("endorsements", [])
    if not isinstance(endorsements, list) or any(
        not isinstance(item, dict)
        or set(item) != {"endorser", "sig"}
        or not isinstance(item.get("endorser"), str)
        or not item["endorser"]
        or not isinstance(item.get("sig"), dict)
        for item in endorsements
    ):
        raise RegistryError("audit record endorsements are malformed")
    for endorsement in endorsements:
        _signature_envelope(endorsement["sig"])
    _validate_ccj(payload)
    return Record(
        name=payload["name"],
        source_identity=payload["source_identity"],
        commit=payload["commit"],
        content_sha256=payload["content_sha256"],
        status=payload["status"],
        audit=audit,
        raw=payload,
    )


def canonical_bytes(record: dict[str, Any]) -> bytes:
    """Return Curator Canonical JSON 1 bytes for a signed object."""
    body = {key: value for key, value in record.items() if key != "sig"}
    _validate_ccj(body)
    return json.dumps(body, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")


def _validate_ccj(value: Any) -> None:
    if value is None or isinstance(value, bool):
        return
    if isinstance(value, int):
        if not -MAX_SAFE_INTEGER <= value <= MAX_SAFE_INTEGER:
            raise RegistryError(f"CCJ-1 integer outside safe range: {value}")
        return
    if isinstance(value, float):
        raise RegistryError("CCJ-1 numbers must be integers")
    if isinstance(value, str):
        if any(0xD800 <= ord(character) <= 0xDFFF for character in value):
            raise RegistryError("CCJ-1 strings must not contain lone surrogates")
        return
    if isinstance(value, list):
        for item in value:
            _validate_ccj(item)
        return
    if isinstance(value, dict):
        for key, item in value.items():
            if not isinstance(key, str):
                raise RegistryError("CCJ-1 object keys must be strings")
            _validate_ccj(key)
            _validate_ccj(item)
        return
    raise RegistryError(f"CCJ-1 does not support {type(value).__name__}")


def parse_public_key(value: str) -> bytes:
    """Decode a pinned key of the form 'ed25519:<base64>' into 32 raw bytes."""
    text = value.strip()
    if text.startswith("ed25519:"):
        text = text[len("ed25519:") :]
    try:
        raw = base64.b64decode(text, validate=True)
    except (binascii.Error, ValueError) as exc:
        raise RegistryError(f"invalid public key encoding: {value!r}") from exc
    if len(raw) != 32:
        raise RegistryError(f"ed25519 public key must be 32 bytes, got {len(raw)}")
    if base64.b64encode(raw).decode("ascii") != text:
        raise RegistryError("ed25519 public key must use canonical padded base64")
    return raw


def _signature_envelope(value: Any) -> tuple[str, bytes]:
    if not isinstance(value, dict) or set(value) != {"algorithm", "key_id", "signature"}:
        raise RegistryError("signature envelope has invalid fields")
    key_id = value.get("key_id")
    encoded = value.get("signature")
    if value.get("algorithm") != "ed25519" or not isinstance(key_id, str) or not isinstance(encoded, str):
        raise RegistryError("signature envelope is malformed")
    if re.fullmatch(r"[0-9a-f]{16}", key_id) is None:
        raise RegistryError("signature key_id must be 16 lowercase hexadecimal characters")
    try:
        signature = base64.b64decode(encoded, validate=True)
    except (binascii.Error, ValueError) as exc:
        raise RegistryError("signature is not canonical base64") from exc
    if len(signature) != 64 or base64.b64encode(signature).decode("ascii") != encoded:
        raise RegistryError("signature must be canonical padded base64 for 64 bytes")
    return key_id, signature


def verify_record(record: Record, pinned_keys: tuple[str, ...]) -> bool:
    """Verify the record signature against any pinned key for its registry."""
    sig = record.raw.get("sig")
    if not isinstance(sig, dict):
        return False
    try:
        key_id, signature = _signature_envelope(sig)
    except RegistryError:
        return False
    try:
        message = canonical_bytes(record.raw)
    except RegistryError:
        return False
    for key in pinned_keys:
        try:
            public_key = parse_public_key(key)
        except RegistryError:
            continue
        if hashlib.sha256(public_key).hexdigest()[:16] != key_id:
            continue
        if _ed25519.verify(public_key, message, signature):
            return True
    return False


def _record_matches(record: Record, source_identity: str, commit: str, content_sha256: str) -> bool:
    if record.content_sha256 == content_sha256:
        return True
    return record.source_identity == source_identity and record.commit == commit


def resolve(
    registries: tuple[RegistryConfig, ...],
    *,
    source_identity: str,
    commit: str,
    content_sha256: str,
    fetch: FetchFn,
) -> Resolution:
    """Combine verified records from every trusted registry (deny-wins).

    A revocation from any registry wins. Otherwise a valid audited record
    authorizes. Otherwise the artifact is unknown. Records that fail signature
    verification are ignored with a warning.
    """
    warnings: list[str] = []
    audited: Attestation | None = None
    deprecated: Attestation | None = None
    for registry in registries:
        if not registry.public_keys:
            warnings.append(f"registry {registry.name} has no pinned keys; its records are not trusted")
            continue
        try:
            payloads = fetch(registry.url, source_identity, commit, content_sha256)
        except RegistryError as exc:
            warnings.append(f"registry {registry.name} unavailable: {exc}")
            continue
        stale_urls: Any = getattr(fetch, "stale_urls", set())
        if isinstance(stale_urls, set) and registry.url in stale_urls:
            warnings.append(f"registry {registry.name} records came from a stale offline cache")
        for payload in payloads:
            try:
                record = parse_record(payload)
            except RegistryError as exc:
                warnings.append(f"registry {registry.name} returned a malformed record: {exc}")
                continue
            if not _record_matches(record, source_identity, commit, content_sha256):
                continue
            if not verify_record(record, registry.public_keys):
                warnings.append(
                    f"registry {registry.name} record for {record.name} failed signature verification"
                )
                continue
            attestation = Attestation(
                registry=registry.name,
                status=record.status,
                key_id=record.key_id,
                record=record.raw,
            )
            if record.status == STATUS_REVOKED:
                return Resolution(RESULT_REVOKED, attestation, tuple(warnings))
            if record.status == STATUS_AUDITED and audited is None:
                audited = attestation
            elif record.status == STATUS_DEPRECATED and deprecated is None:
                deprecated = attestation
    if audited is not None:
        return Resolution(RESULT_AUDITED, audited, tuple(warnings))
    if deprecated is not None:
        return Resolution(RESULT_DEPRECATED, deprecated, tuple(warnings))
    return Resolution(RESULT_UNKNOWN, None, tuple(warnings))


def verify_snapshot(snapshot: dict[str, Any], pinned_keys: tuple[str, ...]) -> bool:
    sig = snapshot.get("sig")
    try:
        key_id, signature = _signature_envelope(sig)
    except RegistryError:
        return False
    try:
        message = canonical_bytes(snapshot)
    except RegistryError:
        return False
    for key in pinned_keys:
        try:
            public_key = parse_public_key(key)
        except RegistryError:
            continue
        if hashlib.sha256(public_key).hexdigest()[:16] != key_id:
            continue
        if _ed25519.verify(public_key, message, signature):
            return True
    return False


def _parse_iso8601(value: Any) -> float | None:
    if not isinstance(value, str) or re.fullmatch(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z", value) is None:
        return None
    text = value.replace("Z", "+00:00")
    try:
        from datetime import datetime

        return datetime.fromisoformat(text).timestamp()
    except ValueError:
        return None


def check_snapshots(
    registries: tuple[RegistryConfig, ...],
    cache_dir: Path,
    *,
    fetch_snapshot: Callable[[str], dict[str, Any]],
    now: float,
    max_age_seconds: int = DEFAULT_SNAPSHOT_MAX_AGE_SECONDS,
) -> tuple[set[str], list[str]]:
    """Verify each registry snapshot; return the URLs to treat as tampered.

    A registry is excluded when its snapshot signature does not verify, its
    version moved backward (rollback), or its reachable snapshot is older than
    the maximum age (freeze). An unreachable snapshot warns but does not
    exclude the registry, because per-record signatures and deny-wins
    revocation still protect the install. The highest accepted version is
    persisted per registry, so a later rollback is detected across runs.
    """
    cache_dir.mkdir(parents=True, exist_ok=True)
    unavailable: set[str] = set()
    warnings: list[str] = []
    for registry in registries:
        if not registry.public_keys:
            continue
        state_file = cache_dir / f"snapshot-{hashlib.sha256(registry.url.encode()).hexdigest()[:16]}.json"
        state = _read_snapshot_state(state_file)
        try:
            snapshot = fetch_snapshot(registry.url)
        except RegistryError as exc:
            warnings.append(f"registry {registry.name} snapshot unavailable: {exc}")
            continue
        if not verify_snapshot(snapshot, registry.public_keys):
            warnings.append(f"registry {registry.name} snapshot signature failed verification")
            unavailable.add(registry.url)
            continue
        version = snapshot.get("version")
        log_size = snapshot.get("log_size")
        head = snapshot.get("head")
        merkle_root = snapshot.get("merkle_root")
        schema_version = snapshot.get("schema_version")
        if (
            schema_version != 1
            or not isinstance(version, int)
            or isinstance(version, bool)
            or not 0 <= version <= MAX_SAFE_INTEGER
            or not isinstance(log_size, int)
            or isinstance(log_size, bool)
            or not 0 <= log_size <= version
            or not isinstance(head, str)
            or re.fullmatch(r"[0-9a-f]{64}", head) is None
            or not isinstance(merkle_root, str)
            or re.fullmatch(r"[0-9a-f]{64}", merkle_root) is None
        ):
            warnings.append(f"registry {registry.name} snapshot is malformed")
            unavailable.add(registry.url)
            continue
        highest = state["highest_version"]
        if version < highest:
            warnings.append(f"registry {registry.name} snapshot version moved backward; possible rollback")
            unavailable.add(registry.url)
            continue
        if version == highest and state.get("head") and (
            state["head"] != head
            or state.get("merkle_root") != merkle_root
            or state.get("log_size") != log_size
        ):
            warnings.append(f"registry {registry.name} snapshot changed without advancing version; possible equivocation")
            unavailable.add(registry.url)
            continue
        created = _parse_iso8601(snapshot.get("created_at"))
        if created is None or now - created > max_age_seconds:
            warnings.append(f"registry {registry.name} snapshot is stale")
            unavailable.add(registry.url)
            continue
        if created > now + DEFAULT_SNAPSHOT_CLOCK_SKEW_SECONDS:
            warnings.append(f"registry {registry.name} snapshot timestamp is too far in the future")
            unavailable.add(registry.url)
            continue
        _write_snapshot_state(
            state_file,
            {"highest_version": version, "head": head, "merkle_root": merkle_root, "log_size": log_size},
        )
    return unavailable, warnings


def _read_snapshot_state(path: Path) -> dict[str, Any]:
    if not path.is_file():
        return {"highest_version": 0}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            raise ValueError("invalid snapshot state")
        highest = data["highest_version"]
        if not isinstance(highest, int) or isinstance(highest, bool) or highest < 0:
            raise ValueError("invalid highest version")
        state: dict[str, Any] = {"highest_version": highest}
        for key in ("head", "merkle_root"):
            value = data.get(key)
            if value is not None:
                if not isinstance(value, str):
                    raise ValueError(f"invalid snapshot state {key}")
                state[key] = value
        log_size = data.get("log_size")
        if log_size is not None:
            if not isinstance(log_size, int) or isinstance(log_size, bool) or log_size < 0:
                raise ValueError("invalid snapshot state log_size")
            state["log_size"] = log_size
        return state
    except (TypeError, ValueError, KeyError, OSError):
        return {"highest_version": 0}


def _write_snapshot_state(path: Path, state: dict[str, Any]) -> None:
    temporary = path.with_name(path.name + ".tmp")
    try:
        temporary.write_text(json.dumps(state, sort_keys=True), encoding="utf-8")
        temporary.replace(path)
    except OSError:
        temporary.unlink(missing_ok=True)


def http_get_snapshot(url: str) -> dict[str, Any]:
    endpoint = f"{url.rstrip('/')}/v1/snapshot"
    request = urllib.request.Request(endpoint, headers={"Accept": "application/json"})
    data = _request_json(request, timeout=10, label="snapshot")
    if not isinstance(data, dict):
        raise RegistryError("snapshot must be a JSON object")
    return data


def make_http_fetch(
    cache_dir: Path,
    *,
    ttl_seconds: int = DEFAULT_CACHE_TTL_SECONDS,
    grace_seconds: int = DEFAULT_OFFLINE_GRACE_SECONDS,
    now: float | None = None,
) -> FetchFn:
    """Build a fetch callable that queries /v1/records with an on-disk cache.

    A fresh cache entry is served without a network call. A stale entry is
    refreshed; when the registry is unreachable the stale entry is reused
    within the offline grace window, otherwise RegistryError is raised.
    """
    return _HTTPFetch(cache_dir, ttl_seconds, grace_seconds, now)


class _HTTPFetch:
    def __init__(self, cache_dir: Path, ttl_seconds: int, grace_seconds: int, now: float | None):
        self.cache_dir = cache_dir
        self.ttl_seconds = ttl_seconds
        self.grace_seconds = grace_seconds
        self.fixed_now = now
        self.stale_urls: set[str] = set()
        cache_dir.mkdir(parents=True, exist_ok=True)

    def __call__(self, url: str, source_identity: str, commit: str, content_sha256: str) -> list[dict[str, Any]]:
        clock = time.time() if self.fixed_now is None else self.fixed_now
        query = urllib.parse.urlencode(
            {
                "source_identity": source_identity,
                "commit": commit,
                "content_sha256": content_sha256,
                "limit": MAX_PAGE_SIZE,
            }
        )
        endpoint = f"{url.rstrip('/')}/v1/records?{query}"
        digest = hashlib.sha256(endpoint.encode("utf-8")).hexdigest()[:16]
        cache_file = self.cache_dir / f"records-{digest}.json"
        cached = _read_cache(cache_file)
        if cached is not None and clock - cached[0] < self.ttl_seconds:
            self.stale_urls.discard(url)
            return cached[1]
        try:
            payloads = _http_get_records(endpoint)
        except RegistryError:
            if cached is not None and clock - cached[0] < self.grace_seconds:
                self.stale_urls.add(url)
                return cached[1]
            raise
        self.stale_urls.discard(url)
        _write_cache(cache_file, clock, payloads)
        return payloads


def _http_get_records(endpoint: str) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    cursor: str | None = None
    seen_cursors: set[str] = set()
    while True:
        page_url = _url_with_cursor(endpoint, cursor)
        request = urllib.request.Request(page_url, headers={"Accept": "application/json"})
        data = _request_json(request, timeout=10, label="records")
        if not isinstance(data, dict) or set(data) != {"records", "next_cursor"}:
            raise RegistryError("registry records response requires only 'records' and 'next_cursor'")
        page = data["records"]
        next_cursor = data["next_cursor"]
        if not isinstance(page, list) or len(page) > MAX_PAGE_SIZE:
            raise RegistryError("registry 'records' must be a list of at most 1000 records")
        if next_cursor is not None and (not isinstance(next_cursor, str) or not next_cursor or len(next_cursor) > 4096):
            raise RegistryError("registry 'next_cursor' must be null or a non-empty string")
        for item in page:
            if not isinstance(item, dict):
                raise RegistryError("registry record page contains a non-object")
            parse_record(item)
            records.append(item)
            if len(records) > MAX_RECORDS_PER_QUERY:
                raise RegistryError("registry record query exceeded the 10000-record limit")
        if next_cursor is None:
            return records
        if next_cursor in seen_cursors:
            raise RegistryError("registry repeated a pagination cursor")
        seen_cursors.add(next_cursor)
        cursor = next_cursor


def _url_with_cursor(endpoint: str, cursor: str | None) -> str:
    split = urllib.parse.urlsplit(endpoint)
    query = [(key, value) for key, value in urllib.parse.parse_qsl(split.query) if key != "cursor"]
    if cursor is not None:
        query.append(("cursor", cursor))
    return urllib.parse.urlunsplit((split.scheme, split.netloc, split.path, urllib.parse.urlencode(query), split.fragment))


def _request_json(request: urllib.request.Request, *, timeout: int, label: str) -> Any:
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:  # noqa: S310 - operator-configured registry
            status = getattr(response, "status", 200)
            if not isinstance(status, int) or not 200 <= status < 300:
                raise RegistryError(f"registry returned HTTP {status} for {label}")
            headers = getattr(response, "headers", None)
            content_type = headers.get("Content-Type", "") if headers is not None else ""
            if content_type.split(";", 1)[0].strip().lower() != "application/json":
                raise RegistryError(f"registry returned unsupported Content-Type for {label}")
            body = response.read(MAX_RESPONSE_BYTES + 1)
    except urllib.error.HTTPError as exc:
        raise RegistryError(f"registry returned HTTP {exc.code} for {label}") from exc
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        raise RegistryError(str(exc)) from exc
    if len(body) > MAX_RESPONSE_BYTES:
        raise RegistryError(f"registry {label} response exceeds 16 MiB")
    return load_protocol_json(body)


def http_publish_record(base_url: str, token: str, record_json: bytes) -> dict[str, Any]:
    """Validate and submit one signed record with the protocol idempotency key."""
    payload = load_protocol_json(record_json)
    if not isinstance(payload, dict):
        raise RegistryError("record must be a JSON object")
    parse_record(payload)
    canonical = canonical_bytes(payload)
    endpoint = f"{base_url.rstrip('/')}/v1/records"
    request = urllib.request.Request(
        endpoint,
        data=record_json,
        method="POST",
        headers={
            "Accept": "application/json",
            "Content-Type": "application/json",
            "Authorization": f"Bearer {token}",
            "Idempotency-Key": hashlib.sha256(canonical).hexdigest(),
        },
    )
    response = _request_json(request, timeout=15, label="record submission")
    if (
        not isinstance(response, dict)
        or set(response) != {"seq", "entry_hash"}
        or not isinstance(response.get("seq"), int)
        or isinstance(response.get("seq"), bool)
        or not 1 <= response["seq"] <= MAX_SAFE_INTEGER
        or not isinstance(response.get("entry_hash"), str)
        or re.fullmatch(r"[0-9a-f]{64}", response["entry_hash"]) is None
    ):
        raise RegistryError("registry returned a malformed submission response")
    return response


def _read_cache(path: Path) -> tuple[float, list[dict[str, Any]]] | None:
    if not path.is_file():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        fetched_at = float(data["fetched_at"])
        records = data["records"]
    except (ValueError, KeyError, OSError):
        return None
    if not isinstance(records, list):
        return None
    validated: list[dict[str, Any]] = []
    try:
        for item in records:
            if not isinstance(item, dict):
                return None
            parse_record(item)
            validated.append(item)
    except RegistryError:
        return None
    return fetched_at, validated


def _write_cache(path: Path, fetched_at: float, records: list[dict[str, Any]]) -> None:
    temporary = path.with_name(path.name + ".tmp")
    try:
        temporary.write_text(
            json.dumps({"fetched_at": fetched_at, "records": records}, separators=(",", ":")),
            encoding="utf-8",
        )
        temporary.replace(path)
    except OSError:
        temporary.unlink(missing_ok=True)
