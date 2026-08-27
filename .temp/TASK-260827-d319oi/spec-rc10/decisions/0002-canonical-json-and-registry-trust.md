# Decision 0002: canonical JSON and registry trust

## Context

The deployed registry draft signed compact, key-sorted JSON, but left number
precision, escaping, key selection, Merkle bytes, snapshots, and offline
bundles partly implementation-defined. Adopting RFC 8785 verbatim would change
bytes for some already valid objects and invalidate existing signatures.

## Decision

Registry schema 1 uses Curator Canonical JSON 1 (CCJ-1), the strictly specified
safe-integer subset in `protocol/registry.md`. Only the outer `sig` member is
excluded, keys use Unicode-scalar order, and strings use the documented minimal
escapes. Ed25519 verification is bound to the advertised key id and the
out-of-band pin set.

Transparency entries hash ASCII previous-hash bytes followed by CCJ-1 record
bytes. Merkle nodes hash raw decoded child hashes. Snapshot rollback state is
keyed by canonical registry URL and survives key rotation. Bundles have no
self-authenticating key: every record, snapshot, chain, and Merkle value is
verified against an existing upstream pin before any mutation.

## Alternatives

- RFC 8785 was rejected for schema 1 because its numeric and string rules would
  change deployed signature bytes. A future schema may adopt it with an
  explicit migration.
- Treating either implementation's serializer as canonical was rejected
  because it would make conformance language-dependent.
- Trusting a bundle's embedded public key was rejected because an attacker
  could replace both payload and key.

## Compatibility impact

Objects already using compact sorted JSON, safe integers, and the documented
escapes retain identical signed bytes. Ambiguous fractions, unsafe integers,
negative zero, malformed Unicode, duplicate keys, and noncanonical signature
envelopes are rejected. This is an intentional release-candidate tightening.

## Security impact

The decision removes cross-language signature ambiguity, prevents key-id
confusion, detects rollback and equal-version equivocation, and prevents an
offline bundle from bootstrapping its own trust anchor. Implementations still
need independent review of parsers, cache atomicity, and cryptographic use.
