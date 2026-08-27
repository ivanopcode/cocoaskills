# Decision 0007: portable and verified assurance modes

Status: accepted for 1.0.0-rc.7 and carried by 1.0.0-rc.8 and 1.0.0-rc.9

## Context

Release 1.0.0-rc.6 defines the portable `manager-worker-v1` execution policy
and explicitly does not establish six provider-enforced guarantees. The
hardened-profile draft explored those guarantees, but coupled them to one host
design and Linux-specific mechanisms. It is reviewed input, not a release
surface.

The protocol needs one model that is implementable by the CLI today and can
later admit separately installed host enforcement on macOS, Linux, and Windows.
It must not turn observations, portable evidence, or a cache hit into proof of a
stronger execution boundary.

## Decision

The closed assurance modes are `portable` and `verified`.

- `portable` is the default. Its policy identity is `portable-cli-policy-v1`
  and its execution-policy identity remains `manager-worker-v1`. It requires no
  provider and records only guarantees that the CLI establishes.
- `verified` is explicit. Its policy identity is
  `verified-provider-policy-v1`, its execution-policy identity is
  `verified-provider-execution-v1`, and it requires the versioned
  `host-execution-provider-v1` contract. A missing, unhealthy, incompatible,
  expired, or incomplete provider negotiation rejects before execution. There
  is no fallback or downgrade.

The common contract is platform-neutral. A provider descriptor identifies its
operating system as `macos`, `linux`, or `windows`, but the protocol messages,
capability identifiers, ordering, hashes, permits, receipts, and failure rules
are identical. This decision specifies no platform mechanism and makes no
provider or platform conformance claim.

Providers are separately installed trusted host components. Their signed
binary identity is verified by the manager's installation trust policy and
bound into every verified record. A provider executable or library MUST NOT be
stored in a skill package. The existing global prohibition on vendored compiled
artifacts applies without exception.

The following identities are closed and non-aliasing:

| Kind | Portable | Verified |
| --- | --- | --- |
| mode | `portable` | `verified` |
| policy | `portable-cli-policy-v1` | `verified-provider-policy-v1` |
| execution policy | `manager-worker-v1` | `verified-provider-execution-v1` |
| provider contract | none | `host-execution-provider-v1` |
| capability receipt | `capability-evidence-v1` (informational) | `provider-capability-receipt-v1` (required proof) |
| permit | portable worker permit, not a wire claim | `verified-execution-permit-v1` |
| execution receipt | portable build receipt schema 1 or 2 | `verified-execution-receipt-v1` |
| checkpoint | portable transaction journal | `verified-execution-checkpoint-v1` |
| claim | claim schemas 1 through 3 | claim schema 4 verified branch |

The verified logical cache identity includes the policy identity, execution
policy, provider contract, provider id, provider binary digest, capability
receipt digest, and ordinary build input. It can never equal a portable cache
key. Checkpoints are recovery state, not cache evidence: they are stored in a
separate namespace and MUST NOT authorize cache reuse or satisfy a claim.

Provider capability negotiation returns exactly the six versioned capabilities
in the normative order, all `established`, bound to a fresh nonce and a bounded
validity interval. The manager validates the complete receipt before cache
lookup or execution, constructs exactly one typed permit, and accepts a success
only with a typed execution receipt matching the permit, provider, capability
receipt, operation, build input, and artifact. Any mismatch fails closed.

Claim schema 4 expresses portable and verified assurance as disjoint branches.
Portable evidence cannot populate the verified branch. Rc.8 publishes the
schema and vectors but emits no claim, because no native provider qualification
is released by this task.

## Rejected alternatives

- Reusing `manager-worker-v1` for verified mode was rejected because it would
  silently widen a historical identity.
- Falling back to portable after verified preflight was rejected because the
  requested guarantees would change without operator consent.
- Vendoring a provider with a skill was rejected because package-controlled
  native code cannot be part of the trusted host boundary.
- Encoding Linux namespaces, macOS sandbox profiles, or Windows job objects in
  the common contract was rejected because mechanisms belong to separately
  qualified provider implementations.
- Treating a checkpoint or portable cache entry as provider evidence was
  rejected because recovery state and reusable output do not prove enforcement.

## Compatibility and migration

All rc.6 and earlier schemas, release metadata, receipts, markers, claims, and
identities retain their bytes and meanings. Existing installations continue in
portable mode unless an operator explicitly selects verified mode and installs
a compatible provider. Historical objects are never upgraded in place. A mode,
provider, contract, capability, or policy change produces a cache miss and new
receipts. Downgrading requires a new explicit portable operation and produces
portable identities only.
