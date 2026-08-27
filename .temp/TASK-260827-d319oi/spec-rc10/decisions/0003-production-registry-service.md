# Decision 0003: production registry service boundaries

## Context

The registry wire document defined signed objects and HTTP responses, but it
did not fully specify what concurrent pages observe or which state must commit
atomically behind a successful submission. Two services could emit valid JSON
while disagreeing on filter conjunction, artifact identity, cursor stability,
idempotency scope, snapshot freshness, or restore behavior. Those differences
affect security and interoperability but do not require new wire objects.

## Decision

The registry wire protocol remains in `protocol/registry.md`. Production state
and operational guarantees form the normative registry-service profile in
`profiles/registry-service.md`.

An artifact is keyed by name, source identity, commit, and content hash. Query
filters are conjunctive. The first page captures a complete signed snapshot
boundary and every cursor page reads that immutable boundary. Appends,
snapshot metadata, and auditor-scoped idempotency results are one serialized
durable transaction. Snapshot creation time is fixed when the boundary commits
and cannot be refreshed to disguise an old head. Restore is checked against an
authenticated high-water mark outside the primary store.

## Alternatives

- Adding snapshot fields to every page was rejected because opaque authenticated
  cursors can bind the same state without changing deployed response objects.
- Offset pagination over the live projection was rejected because concurrent
  appends can duplicate, omit, or replace results between pages.
- Globally scoped idempotency keys were rejected because one auditor could
  collide with another auditor's request.
- Treating database choice and deployment topology as protocol was rejected.
  The profile specifies externally observable invariants, not storage design.

## Compatibility impact

No deployed filename, signed object, endpoint, or response shape changes. RC
services that used disjunctive filters, live offset pagination, mutable snapshot
timestamps, or global idempotency keys must change behavior before claiming the
registry-service class. Existing readers continue to parse the same JSON.

## Security impact

The decision prevents cross-auditor idempotency denial, pagination races,
partial append visibility, stale-head timestamp refresh, multi-writer forks,
and silent rollback after restore. It also makes durability and resource-limit
assumptions reviewable. It does not make a compromised registry signer honest
and does not add availability or consensus.
