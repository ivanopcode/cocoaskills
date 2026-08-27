# Governance and releases

The Curator Protocol is maintained in
`github.com/relux-works/curator-spec`. The repository is the canonical source
for normative prose, schemas, and conformance vectors.

## Changes

Every normative change MUST:

1. describe compatibility and security impact;
2. update all affected prose and JSON Schemas;
3. add or update positive and negative conformance vectors;
4. pass schema, determinism, link, and cross-implementation CI;
5. receive review from a maintainer other than the author when one is
   available.

Implementation behavior alone is not sufficient evidence for a protocol
change. Divergence opens a specification issue; implementations do not silently
become the standard.

## Release process

1. Update `CHANGELOG.md`, version metadata, schemas, and vector manifest.
2. Regenerate vectors twice and prove a clean second run.
3. Pass required checks on the protected default branch.
4. Create an annotated, cryptographically signed `v<version>` tag.
5. Publish a GitHub release containing the normative schemas and conformance
   archive with SHA-256 checksums.

Release CI verifies the tag against `maintainers.allowed_signers` before
packaging. Its exact target must be contained in the protected default branch
and must either carry a signature from the same allowlist or be a
GitHub-verified merge commit created by that protected-branch workflow. The
maintainer-signed tag explicitly authorizes the exact target in both cases.
Changes to the trust file are governance changes and require the same review as
release-policy changes.

Release tags are immutable. A defective release is superseded by a new version
and remains available for audit.

## Independent stable review

Before a stable release, maintainers freeze a commit that already carries the
stable protocol version. One reviewer who is not a project maintainer or author
of the reviewed changes performs the security review. A different independent
reviewer performs the interoperability review. Affiliations and conflicts are
disclosed. Each reviewer explicitly attests that they are not a project
maintainer and did not author or commit the reviewed changes. Reports use
different stable public reviewer contacts, follow
`reviews/review-report-v2.schema.json`, and retain a public source URL
establishing authorship and discussion.

Critical and high findings must be resolved before release. A correction to a
normative file creates a new candidate that must be reviewed again. After a
report's `reviewed_commit`, only review evidence may change before the signed
release tag. `tools/release_gate.py` enforces the review type, result, finding
state, commit ancestry, and frozen normative diff. The operational sequence is
listed in `RELEASE.md`.

## Decision record

Changes that introduce a new schema version, alter signed bytes, or modify
trust semantics MUST include a short decision record under `decisions/`. The
record states context, decision, alternatives, compatibility impact, and
security impact.
