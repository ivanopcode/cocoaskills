# Curator Protocol conformance

This document is normative. A conformance claim names the protocol release,
conformance classes, implementation version, operating system, and shared-suite
commit or release tag.

## 1. Authority

Conformance is determined by the released normative prose, JSON Schemas, and
vectors in this repository. Reference implementation behavior is informative.
When sources conflict, the release has a specification defect and no
implementation behavior silently resolves it.

## 2. Classes

### Core reader/writer

A core implementation:

1. validates every supported skill schema, Skillfile schema, development
   substitution, build receipt, install marker, claim, and adapter ledger;
2. applies portable identifier, path, source identity, and schema-version
   rejection rules;
3. computes context selection and content hashes exactly;
4. resolves closures, conflicts, cycles, activation, and deterministic order;
5. reads markers written by another core implementation and writes markers
   accepted by it.

### Manager

A manager satisfies the core class and the complete lifecycle, scope, adapter,
MCP, audit-decision, atomicity, shell, status, and garbage-collection rules of
`profiles/manager.md`. It never executes package-provided code at install time.

### Registry client

A registry client validates every registry schema; implements CCJ-1, Ed25519
verification with key-id binding, record matching, deny-wins federation,
snapshot persistence and clock bounds, paginated HTTP, caching, and error
handling.

### Registry service

A registry service implements authenticated submission, countersigning,
auditor-scoped idempotency, snapshot-bound deterministic pagination,
serialized durable append, append-only log, exact Merkle tree, immutable
snapshots, authenticated bundle export/import, verified recovery, and rollback
safe backup/restore according to `profiles/registry-service.md`.

## 3. Shared suite

`conformance/v1/manifest.json` lists every normative vector and SHA-256 digest.
The suite contains:

- valid and invalid examples for every JSON Schema;
- portable identifier, path, and source-identity tables;
- canonical, legacy, dual-file, conflict, and runtime-fallback skill-manifest
  resolution cases;
- context-selection and raw-tree hash fixtures;
- closure graphs including diamond, conflict, cycle, narrowing, and tie order;
- normalized install marker and adapter ledger objects, published in every
  marker role the suite exercises: `expected/marker.json` is byte-frozen
  marker-v1 legacy-read evidence, `expected/marker-v2.json` is the marker-v2
  writer golden for the same shared golden skill, because a manager writes
  marker schema 2 for every schema 1 through 6 installation mutation, and
  `expected/install-marker-v4.json` is the marker-v4 writer golden a manager
  writes for every schema-8 installation mutation;
- CCJ-1 exact bytes and rejection cases;
- valid, forged, wrong-key-id, revoked, and malformed signed records;
- snapshot rollback, freeze, future-skew, incomplete-field, and equivocation
  cases;
- first-use, deleted-after-use, corrupted, and unavailable durable client
  rollback-state cases;
- retry classification and execution bounds, unchanged idempotent POST bytes,
  total deadlines, redirect refusal, cursor cycles, and response limits;
- shell-neutral runtime launchers, idempotent machine bootstrap, closure-scoped
  upgrades, fetch deduplication, and persistent-side-effect-free dry runs;
- SHA-1/SHA-256 tagged and untagged external acquisition over HTTPS and SSH,
  exact-ref failure ordering, and network/local substitutions;
- byte-exact Git config, files-ref, raw commit/tag/tree/blob, pack-v2/v3,
  index-v2, and Git LFS parser-family fixtures, including submodule, link,
  special-file, alternate, replace, graft, promisor, filter, and helper
  boundaries;
- whole-snapshot audit-before-cache/compiler ordering for cache hit/miss,
  source-covering dry-run, audit-only, and repair paths; cache corruption,
  CCJ-1-derived receipt-v1/v2 and marker-v3 hashes, receipt-v1/v2 and
  marker-v2/v3 mixed planning, rollback, status/repair/GC, shim/PATH, offline,
  and signing boundaries;
- schema-8 first-party module-root declaration, containment, effective-replace
  form and bijection cases, including snapshot escapes, nested/build/runtime
  overlap, and Windows path collisions;
- the portable `manager-worker-v1` execution contract: the fixed worker process
  graph and session order, the exact mandatory portable controls, the exhaustive
  versioned `rc5-native-control-inventory-v1` per-platform inventory, the closed
  `capability-evidence-v1` record with its states, probe timing, exposure, and
  contradiction errors, the single failure boundary, the six hardened guarantees
  deferred to `STORY-260728-327soo` with their non-rejection guards, the portable
  mechanism each one is not, worker identity and protocol negatives, closed
  package-influence surfaces, and the distinct portable, reserved-hardened, and
  pre-revision cache identities;
- the `script-worker-v1` opt-in contract: schema-8 and legacy declared-only
  selection, deny-by-default capability derivation, mandatory-control preflight,
  the exhaustive three-platform `script-worker-v1-native-control-inventory-v1`
  inventory (including host-conditional Linux cgroup v2 controls), the closed
  `script-capability-evidence-v1` record and its negative cases, and audit labels
  that distinguish enforced commands from legacy declared-only commands;
- the rc.9 assurance contract: closed portable and verified policies, the
  platform-neutral provider descriptor, exact capability negotiation, typed
  permits, receipts, and checkpoints, cache non-aliasing, fail-closed
  no-downgrade selection, a fully hash-linked positive flow, stable relational
  mutations for every provider/capability/nonce/operation/permit/input/artifact/
  freshness/checkpoint binding, provider packaging denial, and zero released
  verified claims;
- transparency chain, Merkle, bundle, pagination, caching, and deny-wins cases;
- conjunctive registry queries, exact artifact identity, snapshot-bound pages,
  scoped idempotency, concurrent writers, transaction rollback, crash recovery,
  immutable snapshots, restore checkpoints, key rotation, transport bounds,
  rate limiting, and cache-control cases.

Files under `conformance/v1/expected` and
`conformance/v1/fixtures/external-repository` are generated only by
`tools/generate-vectors`. The generator imports no implementation packages.
Updating expected bytes is a protocol change and requires a reviewed diff.
Pack/index negatives carry concrete bytes plus deterministic base-fixture
mutations; harnesses must materialize the mutation and recompute the declared
Git hash-family checksums.

## 4. Execution

From the specification repository:

```text
make validate
make regenerate-check
```

Implementations receive the absolute suite root through
`CURATOR_CONFORMANCE_ROOT`. They MUST NOT substitute repository-local golden
fixtures. Specification CI checks out pinned implementation revisions and
invokes their conformance entrypoints directly; orchestration contains no
expected protocol values.

Specification CI checks out released implementation revisions and executes:

```text
CURATOR_CONFORMANCE_ROOT=<spec>/conformance/v1 go test -v ./internal/interop ./internal/closure ./internal/skillspec
CURATOR_CONFORMANCE_ROOT=<spec>/conformance/v1 python -m pytest -v tests/test_protocol_conformance.py  # manager
CURATOR_CONFORMANCE_ROOT=<spec>/conformance/v1 python -m pytest -v tests/test_protocol_conformance.py  # registry service
```

The platform-neutral specification suite runs on Linux, macOS, and Windows. A
skipped vector is a failure in the specification gate. That execution proves
the corpus itself, not a manager's native driver/platform tuple.
Implementation repositories MAY skip the external suite only when
`CURATOR_CONFORMANCE_ROOT` is absent from a developer checkout; their required
release CI always supplies it.

`.github/workflows/implementations.yml` pins every implementation by full Git
commit ID. A pin may advance only after that implementation has passed the
same released suite in its own required CI. Branch names and mutable tags MUST
NOT be used as implementation pins.

## 5. Claim format

Claims for rc.3, rc.4, and rc.5 conform respectively to claim schemas 1, 2,
and 3. Claim v3 records driver/platform/execution-policy tuples in addition to
the common implementation, class, suite, time, and pass fields. Claim v3 admits
only the portable `manager-worker-v1` execution policy, so a hardened claim
cannot be expressed in this schema version and requires a later one. Schema
validity is not qualification: every emitted tuple requires immutable native
evidence against the exact suite pin. Rc.6 defines no new claim schema and
emits no claim; claim v3 remains historical rc.5 wire evidence. macOS and
Windows qualification remains downstream, and Linux is excluded until
`TASK-260728-1skseh` passes. Claims do not replace release CI evidence or
artifact attestations.

Claim schema 4 was introduced by rc.7 and remains byte-frozen for rc.8 with
disjoint portable and verified assurance branches. Claim schema 5 carries the
same closed assurance shape under the rc.9 protocol identity. Its verified
branch requires provider, capability-receipt, and execution-receipt identities;
portable evidence cannot satisfy it. Rc.9 emits no claim and qualifies no
provider/platform tuple. Platform-neutral suite success is not native provider
evidence.

## 6. Release gate

A protocol release candidate may be published only when:

1. all schemas compile under Draft 2020-12;
2. every example has the expected validation result;
3. two consecutive vector generations are byte-identical;
4. the platform-neutral specification suite passes on Linux, macOS, and
   Windows, while each emitted manager driver/platform tuple has separate
   immutable native evidence;
5. registry-service vectors pass;
6. Markdown links and version references are valid;
7. required security and interoperability review reports have no open critical
   or high findings;
8. the release commit and tag are cryptographically signed.

For a stable version, `python tools/release_gate.py --version <version>` also
requires two schema-valid independent reports, no open critical or high
findings, and no normative diff after either reviewed commit.
