# Compatibility policy

The Curator Protocol versions documents, schemas, and conformance vectors as
one release. Semantic versions apply to this complete set.

## Stability

- A release candidate may tighten or correct behavior before `1.0.0` when the
  change is recorded in `CHANGELOG.md` and represented by a vector.
- A stable patch release clarifies prose or adds vectors that do not reject an
  object accepted by the preceding patch.
- A stable minor release may add OPTIONAL fields and new schema versions. Old
  readers MUST continue to reject unsupported schema versions explicitly.
- A stable major release may change required behavior or remove deprecated
  features.

Every JSON wire object carrying `schema_version` is governed by its own schema
series. Implementations MUST reject an unsupported version and MUST NOT guess
its meaning. Unknown-field behavior is defined per schema; it is never inferred
from the protocol release number.

An incompatible structured-wire change requires a new `schema_version`, a new
schema file, positive and negative vectors for both versions, and a migration
note in `CHANGELOG.md`. A release never redefines old schema bytes in place.
Changes to query evaluation, transaction guarantees, durability, or recovery
may tighten a conformance profile without changing a JSON object. Such a
service advertises the production profile only after it passes that profile's
executable vectors.

## Compatibility identifiers

The following deployed names are reserved across protocol 1.x and MUST NOT be
renamed by a conforming implementation when reading or writing shared project
state:

```text
Skillfile.json
Skillfile.dev.json
agent-skill.json
csk-skill.json
.csk-install.json
.csk-managed.json
.agents/
CSK_PROJECT_ROOT
```

`CSK_PROJECT_ROOT` is written by the portable project environment files. All
other command names, machine-home paths, global environment variables, managed
comment text, cache layouts, and executable distribution mechanisms are
tool-specific and are not wire identifiers.

## Deprecation

A feature is deprecated only when the changelog names its replacement and the
earliest release in which removal is permitted. Stable features receive at
least one minor release of overlap before removal. Security-critical behavior
may be disabled sooner through a published advisory.

The legacy `csk-skill.json` filename, `agents/runtime.json` manifest, and skill
command dependency form remain readable in protocol 1.x. Writers MUST use
`agent-skill.json` and `dependencies.skills`. When both modern filenames exist,
readers accept them only when their decoded JSON values are equal and otherwise
fail with `conflicting_skill_manifests`.

Registry RC.2 preserves every endpoint and response envelope from RC.1. It
defines multiple supplied filters as conjunctive, treats content hash as part
of exact artifact identity, and binds pagination to one snapshot. Clients that
already bounded responses and treated cursors as opaque require no wire
migration. Registry services must complete their state and index migration
before claiming the `registry-service` class.

Independent review evidence uses `reviews/review-report-v2.schema.json` for a
stable 1.0.0 release. Schema v2 adds explicit non-maintainer and non-author
attestations; producers migrate by adding both fields with truthful boolean
values. The original schema v1 remains available for draft evidence, but a v1
report is not accepted by the stable release gate.

Protocol rc.4 introduced manifest schema 6, the closed local `go-v1` driver,
build receipt v1, install marker v2, and claim v2. Their schema bytes, driver
meaning, package-controlled surface, and marker relationships are frozen. An
rc.5 or rc.6 reader continues to accept them and never treats local `go-v1`
input as an external repository.

Protocol rc.5 adds manifest schema 7, `skill-build.json` schema 1,
`Skillfile.dev.json` schema 2, the closed `go-repository-v1` driver, build
receipt v2, install marker v3, and claim v3. A schema-7 local `go-v1` command
still uses receipt v1; an external command uses receipt v2; marker v3 records
local, external, or mixed commands with those explicit receipt versions.
Schemas 1 through 6, receipt v1, markers v1/v2, claim v1/v2, and the rc.4
conformance bytes do not acquire repository fields or new semantics.

Protocol rc.5 also names the execution policy under which both compiled drivers
run. `go-v1` and `go-repository-v1` declare the portable `manager-worker-v1`
policy of `protocol/core.md` section 4.2.1. The published rc.5 bytes freeze that
identity. Rc.6 reuses it without widening the policy or changing manifest
schemas 1 through 7, build receipts v1/v2, install markers v1/v2/v3, or claims
v1/v2/v3, and it adds no package-controlled field to any manifest or
descriptor.

It is deliberately not byte neutral for `go-v1` cache identity. The generated
`go-v1` receipt example and every `go-v1` logical cache key change, because the
execution-policy identity is inside the canonical build input. A pre-revision
candidate entry therefore misses instead of aliasing a portable entry, and a
future hardened execution profile must use a new execution-policy identity and a
new claim schema version rather than widening the closed `manager-worker-v1`
constant. Marker v3 build records and claim v3 driver assertions state the
policy explicitly; marker v2 binds it transitively through its recorded cache
key and receipt hash.

The per-platform native-control inventory and the capability-evidence record are
versioned separately from the execution policy. `rc5-native-control-inventory-v1`
is the exhaustive authority for which native controls a portable manager applies,
and `capability-evidence-v1` is the closed record that reports them. Extending or
re-scoping the inventory requires a new inventory version and a protocol
revision, but not a new execution-policy identity, because neither the inventory
nor the evidence record enters a build input, an artifact, a cache key, a
receipt, a marker, or a claim. An unavailable inventory control never rejects a
build; only a missing mandatory portable control does.

Candidate downstream runs consume rc.6 through an explicitly supplied
`CURATOR_CONFORMANCE_ROOT` and verify the manifest digest recorded in
`release/1.0.0-rc.6.json`. The published
`release/1.0.0-rc.5.json` remains byte-frozen historical evidence. The rc.6
candidate identity does not advance a repository's committed released-suite
pin and is not a platform claim, signature, or attestation.

## Rc.7 assurance modes

Rc.7 adds new objects without changing any rc.6 or earlier wire object. The
default remains portable `manager-worker-v1`, now named by the enclosing
`portable-cli-policy-v1` assurance policy. Existing installations, cache
entries, receipts, markers, and claims keep their bytes and meaning.

Verified mode is an explicit new branch: `verified-provider-policy-v1`,
`verified-provider-execution-v1`, and `host-execution-provider-v1`. Its
provider binary digest and capability receipt enter the verified cache input.
Consequently portable, pre-policy, different-provider, changed-provider, and
changed-capability inputs miss rather than alias. Checkpoints use a separate
typed namespace and are never cache or claim evidence.

Migration requires no action for portable users. An operator may install a
qualified provider separately and opt into verified mode. If provider preflight
fails, the operation fails before execution; it is never retried as portable.
Returning to portable mode is a new explicit operation that produces portable
identities. No old object is relabeled or upgraded in place.

## Rc.8 publication recovery

Rc.8 supersedes the failed immutable rc.7 publication without changing the
assurance contract or adding an implementation claim. The rc.7 tag and
`release/1.0.0-rc.7.json` remain immutable historical evidence. Current
candidate version fields, claim-v4 protocol pins, suite manifest identity, and
release metadata advance to rc.8; verified implementation and platform claim
sets remain empty.

## Rc.9 schema-8 candidate

Rc.9 introduces manifest schema 8 and install marker schema 4. Schema 8 is the
single manifest-version bump for this revision: it carries both the
`script-worker-v1` execution-policy fields and Decision 0009 first-party module
roots. Module roots therefore do not consume a separate sequential manifest
version. Schemas 1 through 7 continue to reject the schema-8 fields, and rc.8
and earlier release metadata remain byte-frozen while rc.9 owns the live suite
manifest pin.

A schema-8 manifest that declares neither new field is a schema-7 manifest with
a later version number. `execution_policy` is OPTIONAL and its absence means
declared-only on every schema, so every existing script command keeps its exact
schema-7 meaning; the field's value space is closed at `script-worker-v1`, and
a package chooses only whether a command is enforced, never how. `interpreter`
is co-required with it: declaring one without the other is an invalid manifest
rather than a defaulted one, so no existing manifest acquires enforcement by
omission. `modules` is likewise OPTIONAL, and a command with an absent or empty
list MUST have an empty effective replace set -- the schema-6 and schema-7 rule
unchanged -- so an existing local `go-v1` command is unaffected by the addition.

Install marker schema 4 is written for skills installed from a schema-8
manifest. Marker schemas 1 through 3 keep their exact bytes and their existing
readers, and a manager reads back an older marker without rewriting it.

Rc.9 supersedes rc.8 as the live suite manifest pin without changing one byte
of rc.8. `release/1.0.0-rc.8.json` and the `v1.0.0-rc.8` tag stay exactly as
published; `release/1.0.0-rc.9.json` records rc.8's metadata digest as its
historical predecessor rather than replacing it. A consumer pinned to rc.8
therefore keeps its qualification and advances by an explicit pin change, never
implicitly.
