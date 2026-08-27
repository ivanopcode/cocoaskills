# Assurance protocol

This document is normative for protocol 1.0.0-rc.9. It extends, and does not
reinterpret, the portable execution contract in [`core.md`](core.md).

## 1. Closed selection

An operation selects exactly one `assurance-policy-v1` object. If no mode is
specified, the manager MUST select the exact portable object. `verified` MUST
be requested explicitly by configuration or CLI input outside package data.
Skills, manifests, substitutions, dependencies, and build sources MUST NOT
select, weaken, or replace assurance policy.

Unknown modes, policies, provider contracts, capabilities, record versions, or
identity combinations are errors. A verified selection MUST NOT retry under
portable mode. Operators obtain portable behavior only through a separate
portable operation.

## 2. Platform-neutral provider contract

`host-execution-provider-v1` is a request/response contract shared by macOS,
Linux, and Windows. It does not prescribe IPC transport or enforcement
mechanisms. A conforming integration MUST authenticate message framing and bind
every message to a fresh operation nonce. The manager and provider MUST reject
unknown fields, messages, versions, repetitions, reordering, or mismatched
identities.

Before cache lookup or execution, the manager:

1. resolves a separately installed provider from operator configuration;
2. verifies its trusted installation, regular-file identity, signature policy,
   and SHA-256 bytes, then constructs `verified-provider-v1`;
3. performs a fresh health and capability negotiation;
4. validates `provider-capability-receipt-v1`, including freshness and the exact
   ordered capability set;
5. derives the verified cache identity;
6. on a miss, sends one `verified-execution-permit-v1`;
7. accepts publication only after a matching
   `verified-execution-receipt-v1` and local artifact verification.

The provider receipt establishes exactly:

1. `total-network-denial-v1`;
2. `read-only-source-and-toolchain-v1`;
3. `exact-executable-allowlisting-v1`;
4. `private-build-root-only-writes-v1`;
5. `hard-aggregate-descendant-resource-bounds-v1`;
6. `fail-closed-capability-preflight-v1`.

All six MUST be present once, in that order, with status `established`. Partial,
unavailable, advisory, inferred, stale, or portable evidence is not a valid
receipt. `observed_at` MUST precede `expires_at`; negotiation and cache lookup
MUST occur within that interval.

## 3. Identity and hashing

All digests below are `sha256:` identities over CCJ-1 bytes of the complete
typed object. The verified cache input is the object:

```json
{
  "cache_identity": "verified-cache-identity-v1",
  "policy_id": "verified-provider-policy-v1",
  "execution_policy": "verified-provider-execution-v1",
  "provider_contract": "host-execution-provider-v1",
  "provider_id": "example.provider",
  "provider_binary_sha256": "sha256:<64 lowercase hex>",
  "capability_receipt_sha256": "sha256:<64 lowercase hex>",
  "build_input_sha256": "sha256:<64 lowercase hex>"
}
```

No member may be omitted or inferred. Portable cache inputs contain
`manager-worker-v1` and cannot have this shape. A change in mode, policy,
provider contract, provider id, provider bytes, capability receipt, or build
input MUST miss. Readers MUST reject cross-mode adoption, relabeling, or
upgrading.

Permits, receipts, cache identities, and checkpoints have distinct type
identities and canonical shapes. Their digests are not interchangeable even
when they reference the same operation. A provider execution receipt is
necessary verified evidence but does not by itself constitute a conformance
claim.

## 4. Checkpoints and recovery

`verified-execution-checkpoint-v1` records recovery progress under a dedicated
verified-checkpoint namespace. It MUST NOT be stored in or queried as the build
cache. Each later checkpoint names the prior checkpoint digest; the first
`permit-issued` checkpoint uses `null`. A manager may resume only after
revalidating the provider binary, current capability receipt, permit, and chain.
It MUST NOT publish from an `execution-started` checkpoint. An
`execution-succeeded` checkpoint still requires the matching execution receipt
and local artifact verification.

The checkpoint phase/predecessor relation is closed: `permit-issued` is the
first phase and MUST have a `null` predecessor; `execution-started` and
`execution-succeeded` MUST have a digest predecessor. In a complete chain,
those digests MUST be the CCJ-1 SHA-256 of the immediately preceding phase,
in exactly that order. A phase repetition, omission, reordering, foreign
predecessor, or cross-operation predecessor is `verified_checkpoint_invalid`.

Portable journals and verified checkpoints cannot be converted into one
another. Deleting a checkpoint can require a fresh execution; it never changes
cache validity or assurance claims.

## 5. Failure rules

Every failure below occurs before compilation unless the condition can only be
observed while validating a returned receipt. No failure falls back to portable.

| Condition | Error |
| --- | --- |
| verified requested without configured provider | `verified_provider_missing` |
| provider installation, signature, or binary identity invalid | `verified_provider_identity_invalid` |
| provider unhealthy, unreachable, or incompatible | `verified_provider_unavailable` |
| capability receipt missing, stale, partial, unknown, or mismatched | `verified_capabilities_unsatisfied` |
| permit framing, identity, nonce, ordering, or expiry invalid | `verified_permit_invalid` |
| receipt does not match permit, provider, input, capability receipt, or artifact | `verified_execution_receipt_invalid` |
| portable object offered for verified requirement | `assurance_evidence_mismatch` |
| checkpoint aliases cache state or crosses mode/provider identity | `verified_checkpoint_invalid` |

### 5.1 Relational conformance rejections

The generated `assurance-modes-v1` vector contains one hash-linked valid flow
and the following closed, stable mutation names. A conformance runner MUST
apply each mutation to that flow, obtain the stated protocol error, record
`execution_started=false`, and record no fallback mode. Validation of these
candidate evidence bundles occurs before dispatch; none authorizes a worker or
portable retry.

| Mutation name | Required error |
| --- | --- |
| `provider-id-mismatch` | `verified_provider_identity_invalid` |
| `provider-contract-mismatch` | `verified_provider_unavailable` |
| `provider-binary-mismatch` | `verified_provider_identity_invalid` |
| `capability-set-mismatch` | `verified_capabilities_unsatisfied` |
| `capability-receipt-mismatch` | `verified_capabilities_unsatisfied` |
| `nonce-mismatch` | `verified_permit_invalid` |
| `operation-mismatch` | `verified_execution_receipt_invalid` |
| `permit-mismatch` | `verified_execution_receipt_invalid` |
| `build-input-mismatch` | `verified_execution_receipt_invalid` |
| `artifact-mismatch` | `verified_execution_receipt_invalid` |
| `capability-receipt-stale` | `verified_capabilities_unsatisfied` |
| `permit-expired` | `verified_permit_invalid` |
| `checkpoint-chain-mismatch` | `verified_checkpoint_invalid` |
| `portable-fallback-attempt` | `assurance_evidence_mismatch` |

## 6. Packaging and claims

A host provider is a separately installed trusted component. Skills MUST NOT
contain provider executables, shared libraries, native helpers, or any compiled
artifact for any platform. Managers MUST reject such a package before provider
selection. Provider installation and update are operator actions outside skill
installation.

Claim schema 4 has disjoint portable and verified branches. A verified claim
requires immutable native evidence for the exact provider binary and platform.
Schema validity or platform-neutral vector success is not native evidence. Rc.8
emits no provider, platform, or verified conformance claim.
