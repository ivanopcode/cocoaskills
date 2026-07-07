from __future__ import annotations

import base64
import binascii
import hashlib
import json
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from . import _ed25519
from .config import RegistryConfig


# Advisory lookup cache lifetime and the window a stale cache stays usable
# while a registry is unreachable.
DEFAULT_CACHE_TTL_SECONDS = 3600
DEFAULT_OFFLINE_GRACE_SECONDS = 7 * 24 * 3600
# A snapshot older than this is stale, which defends against a registry that
# freezes its view to hide a newer revocation.
DEFAULT_SNAPSHOT_MAX_AGE_SECONDS = 7 * 24 * 3600


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
    for key in ("name", "source_identity", "commit", "content_sha256", "status"):
        if not isinstance(payload.get(key), str) or not payload[key]:
            raise RegistryError(f"audit record requires a non-empty string {key!r}")
    if payload["status"] not in _STATUSES:
        raise RegistryError(f"audit record status {payload['status']!r} is not recognized")
    audit = payload.get("audit", {})
    if not isinstance(audit, dict):
        raise RegistryError("audit record field 'audit' must be an object")
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
    """Signed form: compact sorted JSON of every field except 'sig'.

    The registry service signs the same canonicalization, so both sides agree
    on the exact bytes without a full JSON canonicalization library.
    """
    body = {key: value for key, value in record.items() if key != "sig"}
    return json.dumps(body, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")


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
    return raw


def verify_record(record: Record, pinned_keys: tuple[str, ...]) -> bool:
    """Verify the record signature against any pinned key for its registry."""
    sig = record.raw.get("sig")
    if not isinstance(sig, dict):
        return False
    signature_b64 = sig.get("signature")
    if not isinstance(signature_b64, str):
        return False
    try:
        signature = base64.b64decode(signature_b64, validate=True)
    except (binascii.Error, ValueError):
        return False
    message = canonical_bytes(record.raw)
    for key in pinned_keys:
        try:
            public_key = parse_public_key(key)
        except RegistryError:
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
    if not isinstance(sig, dict) or not isinstance(sig.get("signature"), str):
        return False
    try:
        signature = base64.b64decode(sig["signature"], validate=True)
    except (binascii.Error, ValueError):
        return False
    message = canonical_bytes(snapshot)
    for key in pinned_keys:
        try:
            public_key = parse_public_key(key)
        except RegistryError:
            continue
        if _ed25519.verify(public_key, message, signature):
            return True
    return False


def _parse_iso8601(value: Any) -> float | None:
    if not isinstance(value, str) or not value:
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
        highest = _read_snapshot_version(state_file)
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
        if not isinstance(version, int) or version < highest:
            warnings.append(f"registry {registry.name} snapshot version moved backward; possible rollback")
            unavailable.add(registry.url)
            continue
        created = _parse_iso8601(snapshot.get("created_at"))
        if created is None or now - created > max_age_seconds:
            warnings.append(f"registry {registry.name} snapshot is stale")
            unavailable.add(registry.url)
            continue
        _write_snapshot_version(state_file, version)
    return unavailable, warnings


def _read_snapshot_version(path: Path) -> int:
    if not path.is_file():
        return 0
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return int(data["highest_version"])
    except (ValueError, KeyError, OSError):
        return 0


def _write_snapshot_version(path: Path, version: int) -> None:
    try:
        path.write_text(json.dumps({"highest_version": version}), encoding="utf-8")
    except OSError:
        pass


def http_get_snapshot(url: str) -> dict[str, Any]:
    endpoint = f"{url.rstrip('/')}/v1/snapshot"
    request = urllib.request.Request(endpoint, headers={"Accept": "application/json"})
    try:
        with urllib.request.urlopen(request, timeout=10) as response:  # noqa: S310 - https endpoint from pinned config
            body = response.read()
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        raise RegistryError(str(exc)) from exc
    try:
        data = json.loads(body)
    except ValueError as exc:
        raise RegistryError(f"registry returned invalid JSON: {exc}") from exc
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
    cache_dir.mkdir(parents=True, exist_ok=True)
    clock = time.time() if now is None else now

    def fetch(url: str, source_identity: str, commit: str, content_sha256: str) -> list[dict[str, Any]]:
        query = urllib.parse.urlencode(
            {"source_identity": source_identity, "commit": commit, "content_sha256": content_sha256}
        )
        endpoint = f"{url.rstrip('/')}/v1/records?{query}"
        digest = hashlib.sha256(endpoint.encode("utf-8")).hexdigest()[:16]
        cache_file = cache_dir / f"records-{digest}.json"
        cached = _read_cache(cache_file)
        if cached is not None and clock - cached[0] < ttl_seconds:
            return cached[1]
        try:
            payloads = _http_get_records(endpoint)
        except RegistryError:
            if cached is not None and clock - cached[0] < grace_seconds:
                return cached[1]
            raise
        _write_cache(cache_file, clock, payloads)
        return payloads

    return fetch


def _http_get_records(endpoint: str) -> list[dict[str, Any]]:
    request = urllib.request.Request(endpoint, headers={"Accept": "application/json"})
    try:
        with urllib.request.urlopen(request, timeout=10) as response:  # noqa: S310 - https endpoint from pinned config
            body = response.read()
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        raise RegistryError(str(exc)) from exc
    try:
        data = json.loads(body)
    except ValueError as exc:
        raise RegistryError(f"registry returned invalid JSON: {exc}") from exc
    records = data.get("records") if isinstance(data, dict) else None
    if records is None:
        return []
    if not isinstance(records, list):
        raise RegistryError("registry 'records' must be a list")
    return [item for item in records if isinstance(item, dict)]


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
    return fetched_at, [item for item in records if isinstance(item, dict)]


def _write_cache(path: Path, fetched_at: float, records: list[dict[str, Any]]) -> None:
    try:
        path.write_text(
            json.dumps({"fetched_at": fetched_at, "records": records}, separators=(",", ":")),
            encoding="utf-8",
        )
    except OSError:
        pass
