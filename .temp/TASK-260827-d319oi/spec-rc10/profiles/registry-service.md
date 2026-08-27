# Curator Registry Service Profile 1.0

This document is normative for implementations claiming the
**registry-service** conformance class. The JSON objects, signatures,
transparency algorithms, bundles, endpoints, status codes, and HTTP headers are
defined by the [registry wire protocol](../protocol/registry.md). This profile
defines the production guarantees behind that wire contract. It does not
prescribe a database, process model, deployment platform, or key provider.

## 1. Durable state and artifact identity

The durable logical state consists of the append-only log, the latest-record
projection, the idempotency ledger, the imported-record ledger, immutable
snapshot boundaries, and the service creation metadata. Signing keys and
auditor credentials MAY live in an external key or secret provider, but their
identifiers and authorization bindings MUST survive restart consistently with
the durable state.

An artifact key is the exact tuple `(name, source_identity, commit,
content_sha256)`. Its current record at log boundary `B` is the matching entry
with the greatest sequence number not greater than `B`. Records with the same
name, source, and commit but different content hashes are distinct artifact
keys and MUST remain visible. This preserves evidence of a source or build
equivocation.

For `GET /v1/records`, all supplied filters are conjunctive. Supplying
`source_identity` requires `commit`, and the pair selects exact equality on
both fields. When `content_sha256` is also supplied, a record MUST satisfy the
identity pair and the content hash. A content hash without an identity pair is
valid. Results are the current records for matching artifact keys, ordered by
`name`, `source_identity`, `commit`, then `content_sha256`, using Unicode scalar
ordering for strings.

## 2. Stable pagination and cursors

The first page of either paginated endpoint MUST capture one committed signed
snapshot boundary. That boundary is the exact tuple `version`, `log_size`,
`head`, `merkle_root`, and `created_at`. Every page reached from its cursor MUST
be evaluated against that same boundary even when later appends commit.

For records, current-record selection ignores entries with `seq` greater than
the boundary `log_size`. For the log, entries satisfy `since < seq <= log_size`
and remain in ascending sequence order. An append after page one MUST NOT add,
remove, replace, or reorder an item in the remaining pages. A terminal page has
`next_cursor: null`; a non-terminal page MUST contain at least one item.

A cursor is opaque to clients and MUST be integrity protected. It is bound to:

1. the endpoint;
2. the percent-decoded effective query, including defaulted `limit`;
3. the complete snapshot boundary;
4. the next position in the deterministic result order;
5. an expiration time.

Changing a filter, `since`, or `limit`; using a cursor on another endpoint;
presenting a malformed or expired cursor; or presenting a cursor whose snapshot
prefix is no longer available returns `404 invalid_cursor`. Cursor state MUST
be bounded and MUST NOT contain credentials or unsigned client-controlled SQL,
paths, or object names. A cursor remains valid for at least the freshness
lifetime advertised on the first page and never longer than the service's
documented cursor-retention limit.

Unknown query parameters, repeated parameters, an unpaired identity or commit,
and present-but-empty filter or cursor values return `400 invalid_query`.

## 3. Append transaction

The service validates authentication, JSON, schema, CCJ-1 constraints,
auditor authorization, and the auditor signature before beginning a mutation.
One accepted submission is then one atomic durable transition:

1. serialize with every other writer;
2. resolve idempotency as described in section 4;
3. read the current committed head and allocate the next contiguous sequence;
4. countersign the record and append the hash-chain entry;
5. update the latest-record projection and immutable snapshot boundary;
6. persist the idempotency response when a key was supplied;
7. commit all effects durably before returning `201`.

No reader may observe a partial transition. If any step fails, none of its
effects may remain. Sequence numbers are contiguous from 1, and committed log
entries are never rewritten or deleted. Derived indexes MAY be rebuilt, but
the log, snapshot boundary, and idempotency response are authoritative and
MUST agree.

Bundle import validates the entire bundle before mutation and commits every
new countersigned record plus every import fingerprint in one transaction.
Failure rolls back the whole import. Re-importing an existing fingerprint has
no effect and does not allocate a sequence.

## 4. Idempotency and concurrent writers

An idempotency identity is `(auditor identity, Idempotency-Key)`. Keys from two
auditors never conflict. A key is 1 through 256 visible ASCII characters
`0x21` through `0x7e`. The compared request digest is lowercase SHA-256 of the
submitted auditor-signed record's CCJ-1 bytes, before registry countersigning.

Within the retention period, the same identity and digest returns the original
success object with `200` and performs no mutation. The same identity with a
different digest returns `409 idempotency_conflict`. The original response,
log append, and ledger row are committed in the same transaction. Retention is
at least 24 hours from the first successful commit and is not shortened by
restart, cleanup, or credential rotation.

All writers, including different threads, processes, replicas, administrative
imports, and API requests, MUST use one serialization mechanism for a canonical
registry URL. A deployment that cannot guarantee single-copy ordering MUST be
read-only or return `503`; it MUST NOT accept divergent heads. A lost response
after commit is resolved by retrying the identical request with the same
idempotency identity.

## 5. Snapshots, durability, and recovery

A registry-service profile snapshot has `version == log_size`. The snapshot
body for a committed boundary is immutable: repeated reads of that boundary
retain its original `created_at`, head, size, and Merkle root. Key rotation MAY
replace only the outer signature. A newly generated timestamp MUST NOT make an
old boundary appear fresh.

Before acknowledging a write, the service MUST use storage durability settings
that survive the failure model documented by the operator. At minimum, process
termination and host restart after a success response MUST preserve the whole
transition. A service that loses durable confirmation returns `503` rather
than success.

At startup and after unclean shutdown, the service MUST verify contiguous
sequences, every previous hash and entry hash, the current head, the Merkle
root, snapshot metadata, latest-record projection, and references from the
idempotency and import ledgers. Recoverable derived indexes MAY be rebuilt from
the log. A mismatch in authoritative state fails readiness and disables writes;
the service MUST NOT truncate or invent history automatically.

## 6. Backup and restore

A backup captures one committed snapshot boundary and all state required to
reconstruct it. The log, snapshot metadata, idempotency ledger, imported-record
ledger, schema version, and service creation metadata MUST be mutually
consistent. Backups containing credentials or signing material MUST be
encrypted and access controlled. Operators MUST keep signing keys recoverable
separately when an external key provider is used.

The operator maintains an authenticated high-water checkpoint outside the
primary store for every canonical registry URL. It contains at least snapshot
version, log size, head, and Merkle root. Restore recomputes the chain and tree,
verifies all ledgers, and compares the result with that checkpoint before the
service becomes ready. A restored state below or inconsistent with the
checkpoint MUST NOT serve the same canonical URL. It must first recover the
missing verified suffix or remain unavailable. Key rotation does not reset the
checkpoint or client rollback state.

The portable checkpoint interchange object is a signed
`registry-snapshot-v1.schema.json` object. Its complete body, including
`created_at`, is retained outside the primary store and verified against the
operator's out-of-band registry key set. A deployment-specific wrapper MAY add
storage metadata, but it does not replace or weaken the signed snapshot.

## 7. Keys and credentials

Production signing keys and bearer-token material MUST be readable only by the
service identity and MUST NOT appear in logs, diagnostics, cursor payloads,
cache keys, backups without encryption, or error details. Token verification
stores only a one-way digest and uses constant-time comparison. Administrative
interfaces use separate authorization from auditor submission.

Rotation follows the overlap sequence in Registry Protocol section 2.1: deploy
the expanded out-of-band pin set, activate the new signer, verify clients, then
retire the old pin. A suspected signing-key compromise stops publication until
new trust anchors are distributed. History is not rewritten. An auditor-key
compromise disables that auditor and publishes corrective records through an
independent authorized auditor where policy requires them.

## 8. Resource and transport controls

The service enforces the wire limits before expensive parsing or cryptography.
It reads at most 16 MiB for one request body, rejects unsupported
`Content-Encoding`, caps pages at 1,000 items and cursors at 4,096 characters,
and applies finite database, signing, and request deadlines. It limits
concurrent work and rate limits by authenticated principal and network source
without placing tokens in the rate-limit key or diagnostics.

HTTPS termination MUST authenticate the configured registry host and preserve
the original scheme and host when deployed behind a proxy. Forwarded headers
are trusted only from explicitly configured proxies. Responses containing
records or snapshots MUST NOT be transformed in a way that changes JSON
values. Error details never expose SQL, filesystem paths, keys, tokens, raw
authorization headers, or unredacted submitted records.

## 9. Health and observability

`GET /health` returns success only when the store is readable, the verified
head matches the immutable snapshot boundary, and required signing and storage
dependencies are available. A non-ready service returns `503` using the
protocol error envelope. Liveness MAY be exposed on a deployment-specific
endpoint outside the protocol.

Audit logs record authentication outcome, auditor identity, idempotency replay
or conflict, committed sequence, status, latency, and stable error code. They
MUST omit credentials and MAY omit or hash artifact identifiers according to
operator privacy policy. Operators document retention, backup frequency,
cursor retention, deadlines, rate limits, durability assumptions, and recovery
objectives.

## 10. Threat model and limits

The profile assumes attackers can submit arbitrary bytes, replay requests,
race writers, mutate caches or backups, interrupt processes, hold stale
cursors, and control an auditor token or auditor key. Validation, scoped
idempotency, serialized transactions, authenticated cursors, immutable
snapshots, external checkpoints, and bounded resources address those threats.

Clients still treat every response as untrusted, verify signatures, and keep
rollback state in protected durable storage outside response caches. A
registry signing key can authorize false records, and a
registry can refuse service. The protocol detects rollback and inconsistent
equal-version snapshots but does not create availability or consensus between
registries. Protocol 1.0 verifies transparency by replaying the append-only log
or an authenticated bundle; it does not define compact inclusion or consistency
proofs. Deployments that require public gossip or multi-party consensus add
those mechanisms without treating them as protocol 1.0 conformance evidence.

## 11. Conformance

A registry-service claim passes the shared schema and cryptographic vectors
plus executable cases for conjunctive lookup, artifact identity, snapshot-bound
pagination, scoped idempotency, concurrent append ordering, transaction
rollback, crash recovery, immutable snapshots, bundle atomicity, backup restore
and rollback refusal, key rotation, and resource limits. Skipping a case is a
failure. Implementation-owned tests may add coverage but cannot replace the
released vectors.
