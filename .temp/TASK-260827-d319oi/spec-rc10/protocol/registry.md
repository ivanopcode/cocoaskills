# Curator Audit Registry Protocol 1.0

This document is normative. It defines signed registry objects, federation,
rollback protection, caching, transparency logs, authenticated offline bundles,
and the registry HTTP wire contract. Production services additionally conform
to the [registry-service profile](../profiles/registry-service.md).

## 1. Curator Canonical JSON 1

Curator Canonical JSON 1 (`CCJ-1`) is the signed byte representation. It is a
strict subset of JSON chosen to preserve deployed signatures while eliminating
cross-language number and escaping ambiguity.

Before canonicalization an implementation MUST reject:

- duplicate object keys or invalid UTF-8;
- non-integer numbers;
- negative zero;
- integers outside `[-9007199254740991, 9007199254740991]`;
- lone Unicode surrogates;
- any value that violates the applicable registry schema.

Canonicalization recursively applies these rules:

1. remove the top-level member `sig`; nested `sig` members remain;
2. sort object keys by Unicode scalar value;
3. emit no insignificant whitespace;
4. preserve array order;
5. encode strings as UTF-8, escaping only `"`, `\\`, `\b`, `\f`, `\n`,
   `\r`, `\t`, and remaining U+0000 through U+001F as lowercase `\u00xx`;
6. do not escape `/`, `<`, `>`, `&`, or non-ASCII characters;
7. emit integers in shortest base-10 form, with zero represented as `0`;
8. emit literals as `true`, `false`, and `null`.

`CCJ-1(object)` is the resulting UTF-8 byte string. The conformance suite
contains positive bytes and rejection cases. Signed-object parsers MUST retain
integer precision and MUST NOT round through binary floating point.

## 2. Signature envelope and keys

Signed objects carry:

```json
{
  "sig": {
    "algorithm": "ed25519",
    "key_id": "0123456789abcdef",
    "signature": "<base64>"
  }
}
```

The signature is Ed25519 over `CCJ-1(object)`. `signature` is canonical padded
base64 of exactly 64 decoded bytes. A pinned key is `ed25519:<base64>` where the
decoded key is exactly 32 bytes. `key_id` is the first 16 lowercase hexadecimal
characters of SHA-256 over those raw key bytes.

Verification succeeds only when `algorithm` is `ed25519`, `key_id` matches one
currently pinned key, and that key verifies the signature. Trying unrelated
pinned keys while ignoring `key_id` is non-conforming.

### 2.1 Rotation and revocation

A registry rotates keys by distributing a pin set containing old and new keys,
then signing with the new key only after the overlap configuration is deployed.
After the overlap period, removing the old key revokes trust in objects signed
solely by it. A compromised key is removed immediately. Trust-anchor updates
are out of band and MUST NOT be accepted from `/v1/meta` alone.

Persisted snapshot state is keyed by canonical registry URL, not key id, and
survives rotation. Adding a key MUST NOT reset rollback state. Removing every
key disables trust in that registry and produces a warning.

### 2.2 Canonical registry URLs

A configured registry URL MUST be an absolute `https` URL, except that `http`
is permitted for `localhost` or an IP loopback address. It has no credentials,
query, fragment, percent escape, non-ASCII character, whitespace, control
character, backslash, doubled path separator, or `.`/`..` path component. A DNS
host consists of dot-separated labels of 1 through 63 ASCII letters, digits, or
hyphens; a label starts and ends with a letter or digit, and the complete host
is at most 253 characters. IP literals use their standard textual form.

Canonicalization lowercases the scheme and DNS host, renders IP literals in
their canonical compressed lowercase form, preserves path case, removes an
explicit default port (`443` for HTTPS or `80` for HTTP), and removes trailing
path separators. The empty path and `/` both canonicalize to no path.
Non-default ports and a non-empty deployment prefix are preserved. Duplicate
configured registries are detected after this canonicalization.

## 3. Audit records

Records conform to `audit-record-v1.schema.json`. New writers include
`schema_version: 1`; readers treat an absent version as legacy schema 1 through
protocol 1.x. Required artifact fields are non-empty `name`,
`source_identity`, full lowercase hexadecimal `commit`, `content_sha256`, and
`status`. `status` is `audited`, `revoked`, `deprecated`, or `pending`.

`audit` contains CCJ-1-compatible metadata. `endorsements` retain prior
signature envelopes and an endorser identifier. The outer registry signature
covers endorsements.

A record matches an artifact when either:

- `content_sha256` equals the computed artifact content hash; or
- both canonical `source_identity` and resolved `commit` equal the artifact.

Content equality intentionally permits one audited tree mirrored from another
source. Policy may forbid that through source allowlists.

The registry-service latest-record projection uses the exact artifact key
`(name, source_identity, commit, content_sha256)`. Matching and federation do
not collapse records with different content hashes merely because their source
identity and commit agree.

## 4. Federation

Clients query every enabled registry with at least one pinned key. Responses
are processed in configured registry order and record order. Malformed,
unmatched, or unverifiable records are ignored with a warning.

Resolution is deny-wins:

1. any verified `revoked` record returns revoked immediately;
2. otherwise the first verified `audited` record is the attestation;
3. otherwise the first verified `deprecated` record is reported;
4. otherwise the artifact is unknown.

Revoked always blocks. Deprecated warns. Unknown blocks only under strict
registry policy. Unreachable registries warn and contribute no record; if that
leaves an artifact unknown, strict policy therefore blocks it. Protocol 1.0 has
no per-registry availability quorum. An attestation records registry name,
status, and key id but does not copy authority to the marker.

## 5. Snapshots

Snapshots conform to `registry-snapshot-v1.schema.json`. Every field is
REQUIRED: `schema_version`, `merkle_root`, `log_size`, `head`, `version`,
`created_at`, and `sig`.

- `schema_version` is 1.
- `merkle_root` and `head` are 64 lowercase hexadecimal characters.
- `log_size` and `version` are safe non-negative integers. `version` advances
  on every append and MUST be at least `log_size`.
- `created_at` is canonical RFC 3339 UTC with seconds precision and `Z`.

For one committed boundary, `created_at`, `head`, `merkle_root`, and `log_size`
are immutable. A service MUST NOT refresh `created_at` without advancing the
boundary. A conforming registry-service profile uses `version == log_size`;
registry clients continue to accept the more general schema rule above.

After signature verification, a client rejects a snapshot as tampered when its
version is below the highest accepted version, it is older than the configured
maximum age (default seven days), or it is more than the allowed clock skew in
the future (default five minutes). The highest version is persisted atomically
per registry URL before the snapshot is accepted. This rollback state is
durable security state, not a disposable response cache. Cache cleanup and
garbage collection MUST NOT remove it. An existing malformed or unreadable
state, or failure to persist the new state, makes that registry unavailable;
the client MUST NOT treat either condition as first use. Equal versions are
accepted only when `head`, `merkle_root`, and `log_size` equal the previously
accepted state.

Operators include rollback state in protected machine backup and restore it
without lowering a recorded version. When an operator knows established state
was lost and it cannot be recovered, trust in that registry remains disabled
until an authenticated checkpoint or explicit operator rebootstrap establishes
a new high-water state. Signing-key rotation, response-cache deletion, and
implementation upgrades do not rebootstrap it.

An unreachable snapshot warns but does not by itself invalidate individually
signed records. A reachable invalid snapshot excludes that registry. When all
otherwise trusted registries are excluded as tampered, resolution fails.

## 6. Transparency log and Merkle tree

Log entries conform to `registry-log-entry-v1.schema.json`. Sequence numbers
start at 1 and are contiguous. Genesis previous hash is 64 ASCII zeroes. For
entry `i`:

```text
entry_hash = SHA256(ASCII(prev_hash) || CCJ-1(record))
```

The stored `prev_hash` and lowercase hexadecimal `entry_hash` MUST equal the
recomputed values.

Merkle leaves are the raw 32 bytes decoded from each `entry_hash`, in sequence
order. An empty log has root 64 zeroes. At each level pair adjacent raw hashes
and compute `SHA256(left || right)`; an odd final node pairs with itself. The
single remaining raw hash encoded as lowercase hexadecimal is `merkle_root`.
The snapshot `head` is the final entry hash, or 64 zeroes for an empty log.

The latest sequence entry for an artifact is its current record. History is
append-only; deletion or in-place replacement is forbidden.

## 7. Authenticated offline bundles

Bundles conform to `registry-bundle-v1.schema.json`. The container has no
independent trust anchor. Its authenticity is the composition of:

- every record signature verifying against the configured upstream key set;
- the snapshot signature verifying against that same set;
- records appearing in original log order;
- recomputed chain, head, log size, and Merkle root matching the snapshot;
- `public_key`, when present, matching one of the already pinned keys.

The embedded `public_key` is informational and MUST NOT bootstrap trust. An
importer rejects the whole bundle before mutation when any check fails. It then
countersigns each imported record with its local registry key, preserving the
upstream signature as an `upstream-import` endorsement, and appends in order.
Repeated import of the same upstream `(source_identity, commit,
content_sha256, status, signature)` is idempotent.

## 8. Cache and offline behavior

Record pages cache by normalized request URL and response media type. Snapshot
rollback state is stored outside this cache. A fresh entry (default TTL one
hour) is served without network. On refresh failure a
stale entry MAY be used within the offline grace period (default seven days)
and MUST be marked stale in diagnostics. Past grace, the registry is
unreachable. Invalid signatures and schema failures are never cached as valid.

Cache writes are atomic. Cache keys include every query parameter and page
cursor. Implementations MUST cap one response body at 16 MiB and total records
processed for one artifact and registry at 10,000.

Successful public GET responses SHOULD advertise a bounded freshness lifetime
with `Cache-Control`. A client never serves a cached response past its local
offline grace merely because a larger server freshness lifetime was received.
Authenticated submissions, bearer-token failures, and all non-2xx responses
use `Cache-Control: no-store`.

## 9. HTTP service

Production registries MUST use HTTPS. Plain HTTP is permitted only for an
explicitly configured loopback address. Requests use `Accept:
application/json`; JSON responses use `Content-Type: application/json` and
UTF-8. Unknown fields in response envelopes are ignored unless their schema
says otherwise.

| Endpoint | Method | Success response |
|---|---|---|
| `/health` | GET | `health-response-v1.schema.json` |
| `/v1/meta` | GET | `registry-meta-response-v1.schema.json` |
| `/v1/records` | GET | `records-response-v1.schema.json` |
| `/v1/snapshot` | GET | registry snapshot schema |
| `/v1/log` | GET | `log-response-v1.schema.json` |
| `/v1/records` | POST | `submission-response-v1.schema.json` |

`/v1/records` GET accepts URL-encoded `source_identity`, `commit`, and
`content_sha256`; at least identity plus commit or content hash is REQUIRED. It
also accepts `limit` (default 100, range 1 through 1000) and opaque `cursor`.
When more than one filter form is supplied, all supplied filters are
conjunctive. `source_identity` and `commit` appear together or not at all.
Results contain the latest matching record per exact artifact key,
deterministic by `name`, `source_identity`, `commit`, and `content_sha256`, plus
`next_cursor` string or `null`.
The response schema validates the pagination envelope and requires each item
to be an object. Clients validate each item independently against the audit
record schema; a malformed item is ignored with a warning as required by
federation, without discarding other records from the page.

`/v1/log` accepts non-negative `since`, the same `limit`, and cursor. Entries
are ascending by sequence. A cursor is bound to its original query and expires
no sooner than the advertised cache TTL. The first page of either endpoint
captures one signed snapshot boundary; all cursor pages are evaluated at that
same boundary according to the registry-service profile. Unknown, repeated,
unpaired, or present-but-empty query parameters return `400 invalid_query`.

POST requires `Authorization: Bearer <token>`. Tokens carry at least 128 bits
of entropy; services store only SHA-256 digests and compare in constant time.
The token identifies an auditor and pinned auditor key. The submitted record
must verify against that key. The service preserves the auditor signature as an
endorsement, signs the outer record, and appends it.

Clients SHOULD send `Idempotency-Key` equal to lowercase SHA-256 of the
submitted auditor-signed record's CCJ-1 bytes. The identity is scoped to the
authenticated auditor. A service MUST return the original success response for
the same auditor, key, and canonical body for at least 24 hours, and `409
idempotency_conflict` for the same auditor and key with a different canonical
body. Keys are 1 through 256 visible ASCII characters.

### 9.1 Status and errors

| Status | Meaning |
|---|---|
| 200 | successful read or idempotent replay |
| 201 | record appended |
| 400 | malformed query, JSON, schema, or signature envelope |
| 401 | missing or invalid bearer token |
| 403 | authenticated auditor not authorized |
| 404 | unknown endpoint or cursor |
| 409 | idempotency conflict or non-appendable state |
| 413 | request exceeds configured limit |
| 415 | unsupported media type |
| 429 | rate limit; includes `Retry-After` |
| 500 | internal error without sensitive detail |
| 503 | temporarily unavailable; MAY include `Retry-After` |

Every non-2xx JSON response conforms to `error-response-v1.schema.json`:

```json
{
  "error": {
    "code": "stable_machine_code",
    "message": "operator-readable summary",
    "details": {}
  }
}
```

Clients MUST reject non-2xx responses before parsing a success envelope. They
MUST use bounded connect and read timeouts and MUST NOT include bearer tokens in
cache keys, logs, or diagnostics.

### 9.2 Transport and retries

One request body and one response body are each limited to 16 MiB. POST accepts
only `Content-Type: application/json` with UTF-8 and no `Content-Encoding`
other than `identity`; violations return `415`, or `413` when the decoded size
limit is exceeded. A server streams or bounds an absent or dishonest
`Content-Length` rather than allocating from it.

Clients document finite connection, read, and total-operation deadlines. GET
MAY be retried after network failure, `429`, or `503`. POST MAY be retried only
when it carries the same `Idempotency-Key`, authenticated auditor identity, and
submitted record bytes. A retry never changes query parameters or follows a
pagination cursor with a new first-page request. `Retry-After` is honored when
valid and within the remaining deadline. Other 4xx responses are not retried.
All retry loops have a documented finite attempt bound and use backoff; they
MUST NOT retry indefinitely.

## 10. Publication

An auditor signs a schema-valid record and submits it. Registry countersigning
does not erase auditor provenance. Publication clients validate local JSON,
CCJ-1 constraints, signature envelope, registry URL policy, and response schema
before reporting success.
