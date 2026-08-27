# Release checklist

This checklist is normative release procedure. `GOVERNANCE.md` defines who may
approve changes; CI enforces the mechanically verifiable items.

## Release target provenance

- [ ] Repository merge settings enable squash merging and disable both rebase
  merging and merge commits. A maintainer runs
  `tools/verify_release_merge_policy.py` before integrating a release pull
  request so policy drift blocks integration without requiring a broad
  maintainer token in Actions.
- [ ] Release pull requests are squash-merged through GitHub after required
  review and checks. Do not use rebase-and-merge for a release target: GitHub
  recreates each rebased commit without commit signature verification.
- [ ] The resulting squash commit reports a valid GitHub signature and passes
  the main-branch Release target provenance job, which runs
  `tools/verify_release_commit.py`, before an annotated signed tag is created.

## Candidate

- [ ] All normative changes have compatibility and security impact notes.
- [ ] Structured wire changes use a new schema version and include a migration
  note. No deployed protocol identifier is removed or silently reinterpreted;
  legacy aliases remain covered by compatibility tests.
- [ ] `make validate` passes on Linux, macOS, and Windows.
- [ ] `make regenerate-check` proves byte-identical generated vectors.
- [ ] `release/<version>.json` pins the exact SHA-256 of the generated suite
  manifest, and every downstream candidate run verifies that digest before
  consuming `CURATOR_CONFORMANCE_ROOT`.
- [ ] Go Curator, the independent Python manager, and Curator Skill Registry
  pass the same full-commit-pinned suite without skips on all three operating
  systems.
- [ ] Implementation pins refer to commits whose own required CI is green.
- [ ] `CHANGELOG.md`, `COMPATIBILITY.md`, `SECURITY.md`, and version metadata
  describe the candidate.
- [ ] A driver/platform tuple is listed only under a claim schema that admits
  the candidate protocol version and with immutable native passing evidence.
  Schema examples and platform-neutral corpus runs are not native evidence;
  rc.8 defines claim schema 4 but emits no claim.
- [ ] Every emitted execution-policy identity is one the release actually
  implements. `release/<version>.json` records the portable policy, records that
  no hardened profile is claimed, and names the story that owns the deferred
  hardened guarantees. No candidate advertises a guarantee from that deferred
  set.
- [ ] An execution-policy revision proves non-aliasing: the affected logical
  cache keys change, the superseded key is retained only as negative evidence,
  and no prior candidate entry, receipt, marker, or claim is upgraded in place.
- [ ] Assurance-mode releases prove that portable is the default, verified is
  explicit and fail-closed, provider/capability/permit/receipt/cache/checkpoint/
  claim identities do not alias, and no verified platform is claimed without
  immutable native provider evidence.
- [ ] The assurance relational vector's valid chain passes, every stable named
  mutation is rejected before execution without fallback, capability and
  execution intervals are ordered, and all checkpoint predecessors obey their
  phase and chain rules.
- [ ] Host providers are installed separately from skills, and the complete
  release corpus preserves the prohibition on vendored compiled artifacts.

## Stable 1.0.0

- [ ] The stable-version candidate commit contains no placeholder review
  evidence and is frozen for independent review.
- [ ] An independent security reviewer publishes
  `reviews/1.0.0/security.json` with conclusion `pass`.
- [ ] An independent interoperability reviewer publishes
  `reviews/1.0.0/interoperability.json` with conclusion `pass`.
- [ ] The two reports identify different stable reviewer contacts, and each
  reviewer attests that they are neither a project maintainer nor an author or
  committer of the reviewed changes.
- [ ] Neither report has an open critical or high finding.
- [ ] Only `reviews/` changed after each report's `reviewed_commit`.
- [ ] The release commit and annotated `v1.0.0` tag verify against
  `maintainers.allowed_signers`.
- [ ] Release archives, SHA-256 checksums, and build-provenance attestations are
  present and immutable.

Run the local release gate before signing a tag:

```text
python tools/release_gate.py --version 1.0.0 --commit HEAD
```
