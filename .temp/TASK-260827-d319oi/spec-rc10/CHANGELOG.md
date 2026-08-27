# Changelog

All notable protocol changes are recorded here. Versions follow Semantic
Versioning for the complete specification set.

## 1.0.0-rc.9 - 2026-08-23

### Added

- Added manifest schema 8 as the single shared schema bump for the
  `script-worker-v1` execution policy and first-party module roots.
- Added install marker schema 4, script-worker capability and evidence vectors,
  module-root filesystem/build-graph vectors, and legacy-schema rejection cases.
- Added an implementation coverage contract. Presence of a family in the
  conformance root was never evidence that a pinned implementation read it, and
  both pinned managers exited 0 against the schema-8 surface while consuming
  none of it. `.github/ci/implementation-coverage.tsv` now names the cases each
  pinned implementation must be observed passing against this suite, and
  `tools/implementation_coverage.py` enforces the ledger against that run's own
  `go test -json` and pytest `--junitxml` streams and against this suite's
  published manifest.

### Changed

- Advanced the normative version through claim schema 5, conformance suite
  identity, generated release metadata, and regeneration inventory to rc.9.
- Moved the live candidate-suite pin to `release/1.0.0-rc.9.json`; rc.8 and
  earlier release metadata remain byte-frozen historical evidence.
- Advanced the pinned implementation references in the same commit as the new
  normative bytes, so no interval of `main` pairs schema 8 with pins that do
  not consume it: the Go manager to `a3abcf34` and the Python manager to
  `3ecca1db`, each qualified against this exact suite manifest
  (`sha256:803918bf...`) through its own candidate lane. The registry pin is
  unchanged; schema 8 adds no registry surface.

## 1.0.0-rc.8 - 2026-08-19

### Changed

- Superseded the failed immutable rc.7 publication with rc.8 while preserving
  the rc.7 tag and release metadata bytes as historical evidence.
- Disabled Python bytecode generation for the entire release workflow before
  validation starts, preventing validation imports from dirtying the tagged
  checkout before the clean-tree release gate.
- Advanced the normative version, claim-v4 protocol pins, conformance suite
  identity, generated release metadata, and regeneration inventory to rc.8.

### Compatibility and security

- The portable-default and explicit fail-closed verified assurance contract is
  unchanged. Rc.8 ships no provider implementation and emits no verified
  implementation or platform claim.
- No rc.7 or earlier release metadata is rewritten. The rc.8 metadata pins the
  exact rc.7 metadata digest and signed merged source commit.

## 1.0.0-rc.7 - 2026-08-19

### Added

- Added the closed `portable` and `verified` assurance model. Portable is the
  default CLI-only `portable-cli-policy-v1`; verified is the explicit,
  provider-backed `verified-provider-policy-v1` and never falls back.
- Added the platform-neutral `host-execution-provider-v1` contract shared by
  macOS, Linux, and Windows, plus typed provider descriptors, capability
  receipts, execution permits, execution receipts, checkpoints, and claim v4.
- Added generated valid and invalid schema cases, timestamp and phase-specific
  checkpoint semantics, a fully hash-linked provider evidence flow, stable
  relational rejection mutations, cache non-aliasing, validator mutation
  tests, and rc.7 release gates.
- Added Decision 0007, normative assurance protocol text, operator guidance,
  and migration/security notes.

### Compatibility and security

- Rc.6 and earlier release metadata and wire schemas remain byte-frozen. No
  historical receipt, marker, cache entry, checkpoint, or claim is upgraded.
- Portable evidence cannot satisfy verified requirements. Mode, policy,
  provider contract, provider binary, capability receipt, permit, execution
  receipt, cache identity, checkpoint, and claim identities are disjoint.
- Providers are separately installed trusted host components. Skill-vendored
  provider binaries and every other vendored compiled artifact remain
  prohibited. Rc.7 ships no provider implementation and emits no verified
  platform claim.

## 1.0.0-rc.6 - 2026-07-30

### Added

- Restored the 22 schema-6 compiled manager lifecycle cases for audit and
  provider ordering, read-only dry runs, private staging, protected cache
  publication, concurrent projects, deterministic transactions, recovery,
  currentness, repair, and locked garbage collection.
- Added fail-closed generator, validator, and release-gate coverage for every
  restored lifecycle group and name and for the exact portable
  `manager-worker-v1` build input, cache key, receipt, and artifact identity.
- Added `release/1.0.0-rc.6.json` with the exact regenerated suite-manifest pin
  and an explicit immutable reference to the published rc.5 metadata.
- Added `conformance/v1/expected/marker-v2.json`, the marker-v2 writer golden
  for the shared schema-5 golden skill. Managers write marker schema 2 for
  schema 1 through 6 mutations, so a writer is compared against this file;
  `expected/marker.json` stays byte-frozen marker-v1 legacy-read evidence.
  Generator, validator, and release-gate coverage require the writer golden,
  reject a legacy marker edited in place, and reject a writer golden that
  invents build state or describes a different installation.

### Changed

- The shared-suite manifest and current candidate metadata now identify
  `1.0.0-rc.6`.
- The compiled lifecycle fixture now reuses the published rc.5 portable
  `go-v1` identity instead of the pre-execution-policy rc.4 identity.
- Release validation now rejects a missing or renamed schema-6 artifact, a
  dropped lifecycle case, a stale compiled fixture, changed claim-v1/v2
  history, stale or duplicate suite pins, and any byte change to the published
  rc.5 metadata.

### Compatibility

- Manifest schemas 1 through 7, build receipts v1/v2, install markers v1/v2/v3,
  and claims v1/v2/v3 retain their bytes and meaning. Claim v3 remains bound to
  rc.5; rc.6 defines no new claim schema and emits no conformance claim.
- `release/1.0.0-rc.5.json` remains byte-identical to the signed and published
  rc.5 commit. Rc.6 uses a new metadata file and suite identity; it does not
  rewrite historical release evidence or advance a downstream implementation
  pin.

### Reconciliation

- Reconciled the accepted schema-7 schemas, corpus, documentation, and gates
  against the published rc.6 tree. The published tree already contains the
  accepted bytes, so this integration records the no-loss reconciliation and
  supersedes PR #14 without changing protocol semantics.

### Security

- All restored rejection and lifecycle evidence remains declarative. The
  generator and validator execute no package-provided code, and all build
  rejections continue to require `artifact_executed=false`.

## 1.0.0-rc.5 - 2026-07-28

### Added

- Manifest schema 7, repository-root `skill-build.json` descriptor schema 1,
  development substitution schema 2, build receipt schema 2, install marker
  schema 3, and conformance claim schema 3 for the closed `go-repository-v1`
  driver. The descriptor filename is manager-neutral: it names the artifact a
  skill is built from, not the manager that reads it.
- Platform-neutral shared fixtures and vectors for SHA-1/SHA-256 tagged and
  untagged acquisition, HTTPS/SSH/local identities, exact Git config/refs/raw
  objects/pack/index/LFS bytes, and fail-closed repository features.
- Whole-snapshot audit ordering, cache, offline, mixed-build, rollback,
  status/repair/GC, shim/PATH, signing-boundary, and claim-qualification cases.
- Author/operator guidance and generated rc.5 metadata carrying the exact
  downstream candidate suite-manifest pin.
- The portable `manager-worker-v1` execution policy for `go-v1` and
  `go-repository-v1`: one identity-verified manager-owned worker in the fixed
  process graph, an exact worker session state machine, the mandatory portable
  control set, the exhaustive versioned `rc5-native-control-inventory-v1`
  per-platform inventory, the closed `capability-evidence-v1` reporting record,
  a single explicit failure boundary, and the `build_execution_*` stable
  diagnostics.
- An explicit statement, in normative text and in vectors, of the portable
  mechanism behind each rule and the kernel-enforced guarantee it is not, so
  `network: "none"`, the frozen snapshot, and the fixed manager-selected graph
  can no longer be read as network denial, read-only presentation, or executable
  allowlisting.
- Executable `go-host-execution-policy` vectors covering the worker graph and
  session order, mandatory controls, the per-platform native-control inventory,
  the closed capability-evidence record and its negatives, the six deferred
  hardened guarantees with their non-rejection guards, worker identity and
  protocol negatives, closed package-influence surfaces, and distinct portable,
  reserved-hardened, and pre-revision cache identities.
- Decision 0006 recording the portable execution policy, the rejected
  hardened-Linux-only and direct-Go alternatives, and the deferral of the
  fail-closed profile to `STORY-260728-327soo`.

### Changed

- The shared-suite manifest and repository version metadata now identify
  `1.0.0-rc.5`.
- Claim v3 qualification requires immutable native evidence for every emitted
  driver/platform tuple. This candidate emits no native manager claim; macOS
  and Windows remain pending downstream evidence and Linux remains excluded
  until `TASK-260728-1skseh`.
- `goBuildPolicyV1` and `goRepositoryBuildPolicyV1` require
  `execution_policy: "manager-worker-v1"`; marker-v3 build records and claim-v3
  driver assertions require the same closed constant. Every `go-v1` logical
  cache key and the generated `go-v1` receipt example change accordingly.
- Decision 0004 supersedes its direct-Go process-graph clause before
  publication.

### Compatibility

- Manifest schemas 1 through 6, the `build-receipt-v1`, `install-marker-v2`,
  and `conformance-claim-v2` schema bytes, markers v1/v2, claim v1/v2, and the
  `go-v1` package-controlled surface retain their prior meaning and frozen
  guards. No manifest or descriptor gains a package-controlled field.
- The unreleased execution-policy revision intentionally changes `go-v1` cache
  identity so a pre-revision candidate entry misses instead of aliasing. The
  exact rc.4 candidate key
  `sha256:3fcd714a40e8918eb67dbd35d435875dcce6c9047da811a1fa26626e5e57be48`
  is retained in the suite only as that non-aliasing proof.
- A future hardened execution profile requires a new execution-policy identity
  and a new claim schema version; claim v3 cannot express one.
- Schema-7 mixed builds keep receipt v1 for local `go-v1`, use receipt v2 only
  for `go-repository-v1`, and write marker v3.
- Candidate suite consumption is explicit and digest-pinned; no committed
  downstream released-suite pin is advanced by this candidate metadata.

### Security

- Exact source and raw-object proof, full-snapshot validation, Git LFS
  rejection, and independent external audit precede both artifact-cache lookup
  and compiler execution.
- Compiled builds apply their mandatory controls inside an identity-verified
  manager-owned worker before any package byte reaches a compiler, and reject
  the build before the worker or Go when a mandatory control is unavailable.
- The portable policy does not claim total network denial, kernel-enforced
  read-only source and toolchain, private-build-root-only writes, hard aggregate
  descendant resource bounds, exact executable allowlisting, or fail-closed
  capability preflight. Those six guarantees are deferred to
  `STORY-260728-327soo`, their absence never rejects a portable build, and
  recording them under `manager-worker-v1` is an error.
- Exactly one control set can reject at this boundary: a mandatory portable
  control that cannot be applied rejects before the worker starts. An unavailable
  inventory native control is reported and never rejects, and capability evidence
  stays out of cache, receipt, marker, and claim identity.
- Source hooks, helpers, filters, submodules, LFS hydration, alternates,
  replacements, grafts, promisor/lazy fetch, package PATH/output control,
  package influence over the execution boundary, and install-time signing remain
  outside the closed driver.

## 1.0.0-rc.4 - 2026-07-20

### Added

- Manifest schema 6 with the closed compile-only local `go-v1` build command.
- Build receipt schema 1, install marker schema 2, conformance claim schema 2,
  protected artifact-cache rules, mixed lifecycle planning, and deterministic
  build vectors.

### Compatibility

- Schemas 1 through 5 and marker schema 1 retain their published bytes and
  meanings. Schema 6 is selected only by exact `schema_version`.

### Security

- Package source is compiler input only. Package-controlled recipes, hooks,
  argv, environment, output selection, dynamic/native toolchains, produced
  program execution, and receipt self-attestation are rejected.

## 1.0.0-rc.3 - 2026-07-14

### Added

- Canonical `agent-skill.json` schemas for skill runtime, capability, command,
  skill, and MCP dependency declarations.
- Shared manifest-resolution vectors covering canonical-only, legacy-only,
  equal dual manifests, conflicting dual manifests, invalid-manifest
  fail-closed behavior, and `agents/runtime.json` fallback.

### Changed

- Made `agent-skill.json` the implementation-neutral filename that conforming
  writers emit.
- Reserved `csk-skill.json` as a protocol 1.x read alias and preserved its
  published schema bytes unchanged.
- Required readers to reject unequal dual manifests with
  `conflicting_skill_manifests` instead of choosing one silently.

### Compatibility

- Existing `csk-skill.json` packages remain readable without migration.
- Packages may temporarily ship equal canonical and legacy manifests during a
  staged rollout; `agent-skill.json` is authoritative in that case.
- `agents/runtime.json` remains readable only when neither modern filename is
  present.

### Security

- The rename adds no execution surface. Dual-file ambiguity and attempts to
  hide an invalid manifest behind a fallback now fail closed.

## 1.0.0-rc.2 - 2026-07-13

### Added

- A normative registry-service profile for stable pagination, serialized
  append transactions, durability, recovery, backup/restore, key operations,
  resource controls, health, observability, and an explicit threat model.
- Executable registry-service and registry-client vectors covering conjunctive
  queries, exact artifact identity, snapshot-bound cursors, auditor-scoped
  idempotency, concurrent writers, rollback, recovery, retry safety, and limits.
- A decision record separating the registry HTTP wire contract from production
  service guarantees without changing deployed response objects.
- A machine-validated independent review report format, stable-release gate,
  and release checklist that forbid normative drift after review.
- Manager lifecycle vectors for self-contained command launchers, idempotent
  bootstrap, closure-scoped upgrades, and side-effect-free dry runs.

### Changed

- Defined artifact identity as name, source identity, commit, and content hash,
  preserving evidence when one source and commit produce different content.
- Bound every pagination chain to one immutable signed snapshot boundary.
- Scoped idempotency keys to an auditor and compared the submitted record's
  CCJ-1 digest.
- Required snapshot creation time to remain fixed for one committed boundary
  and registry-service snapshot version to equal log size.
- Defined an external high-water checkpoint as a signed registry snapshot and
  made stable release artifacts conditional on two passing independent reports.
- Added review-report schema v2, requiring separate public reviewer identities
  and explicit non-maintainer/non-author attestations, with executable
  stable-gate regression tests. Draft v1 reports remain readable but are not
  valid stable-release evidence.
- Separated durable client rollback state from disposable response caches and
  required existing corruption and persistence failures to fail closed.
- Made shell activation an explicitly optional interactive convenience and
  required agent command execution to remain independent from user profiles.
- Defined portable direct project-shim locations and safe, non-destructive
  publication of global forwarding shims.
- Clarified finite upward search, activation reentrancy guards, Git Bash
  handling of native Windows paths, and cached hook installation.
- Added manager guidance for warning about prompt-visible runtime source paths
  and missing shell-neutral command resolution.
- Required command launchers to carry their runtime dependency environment on
  Unix and Windows while preserving inherited `PATH`, arguments, and exit
  status.
- Defined selected-closure upgrade behavior, cross-project fetch
  deduplication, create-if-absent bootstrap, and dry-run purity across source,
  cache, security-state, runtime, and project surfaces.
- Accepted GitHub-verified protected-main merge commits as release targets
  while retaining maintainer-signed release tags and exact-target checks.

### Compatibility

- Existing protocol filenames, signed object schemas, endpoints, and response
  shapes are unchanged.
- Registry services must tighten behavior before claiming the production
  registry-service class; existing clients continue to parse the same wire
  objects.

## 1.0.0-rc.1 - 2026-07-13

### Added

- Split normative protocol core, registry, manager profile, and conformance
  documents from the implementation-specific Curator CLI guide.
- Draft 2020-12 JSON Schemas for every versioned wire object and HTTP response.
- Authoritative positive and negative conformance vectors with deterministic
  regeneration.
- Compatibility, security, governance, and release policies.
- Cross-platform CI and shared Go/Python conformance gates.
- A repository-pinned SSH signer allowlist verified by release CI for both the
  release tag and its target commit.
- GitHub Actions dependencies pinned to verified full commit IDs.

### Changed

- Declared machine-home paths, command names, global environment variables,
  cache layouts, and managed comment text implementation-specific.
- Replaced implementation-oracle conformance language with schema, prose, and
  vector authority.
- Defined Curator Canonical JSON 1, complete snapshot validation, Merkle byte
  layout, bundle authentication, HTTP errors and limits, and key rotation.
- Defined deterministic closure ordering and portable Windows path rules.
- Added shared identifier, expanded path, source-identity, and signed-number
  rejection vectors.
- Clarified that project aliases are operator-facing Unicode labels while
  canonical registry source identities remain whitespace-free lowercase-host
  values of bounded length.
- Made paginated record envelopes tolerant of individually malformed object
  candidates so federation can ignore one bad record without dropping a page.
- Aligned manager and system configuration schemas with both implementations:
  strict unknown fields, portable matching aliases, registry key and URL
  validation, explicit defaults, and configurable cache/snapshot time bounds.
- Removed the undefined per-registry `required` flag; strict registry policy is
  the protocol 1.0 fail-closed mechanism for unknown artifacts.

### Compatibility

- Existing deployed wire filenames and `.agents/` layout are preserved.
- The signed JSON profile preserves bytes for all valid pre-RC registry
  objects; previously ambiguous numeric and string forms are now rejected.
