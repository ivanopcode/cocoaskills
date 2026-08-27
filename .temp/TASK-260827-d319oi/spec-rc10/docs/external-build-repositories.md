# External build repository authoring and operations

This guide is informative. The normative contracts are
[`protocol/core.md`](../protocol/core.md),
[`profiles/manager.md`](../profiles/manager.md), and
[`decisions/0005-external-build-repositories.md`](../decisions/0005-external-build-repositories.md).

## Package authoring

Schema 7 keeps repository identity in `build_repositories`, the command name in
the consuming manifest, and logical target selection in the repository-root
`skill-build.json` descriptor:

```json
{
  "schema_version": 7,
  "capabilities": {},
  "build_repositories": {
    "golden-tools": {
      "git": "https://github.com/example/golden-tools.git",
      "locked_commit": {
        "object_format": "sha1",
        "hex": "0123456789abcdef0123456789abcdef01234567"
      },
      "tag": "v1.4.0"
    }
  },
  "commands": {
    "golden-tool": {
      "type": "build",
      "driver": "go-repository-v1",
      "repository": "golden-tools",
      "target": "golden-tool"
    }
  }
}
```

```json
{
  "schema_version": 1,
  "targets": {
    "golden-tool": {
      "driver": "go-repository-v1",
      "build_root": ".",
      "source_dir": "cmd/golden-tool"
    }
  }
}
```

Use a full lowercase SHA-1 or SHA-256 commit ID. A tag is an additional exact
assertion, not the source lock: the manager fetches only that tag and requires
its terminal commit to equal `locked_commit`. Without a tag, the manager
fetches only the full locked object ID. There is no branch, abbreviated-ID,
all-refs, direct-ID fallback for tagged sources, or repository-selected
refspec.

The descriptor cannot name an executable, output, argv, environment, hook,
plugin, generator, signer, or fallback. The command key determines the
manager-derived artifact basename. External repository files never enter agent
context or the consuming skill runtime copy.

## Development substitutions

`Skillfile.dev.json` schema 2 may replace one declared repository with either a
network ref or an ordinary local Git worktree:

```json
{
  "schema_version": 2,
  "substitutions": {},
  "build_repository_substitutions": {
    "golden-skill": {
      "golden-tools": {
        "path": "../golden-tools"
      }
    }
  }
}
```

A local substitution is deliberately narrow. It must be a non-bare worktree
with a direct, link-free `.git` directory, files-format refs, admitted config,
and complete SHA-1 or SHA-256 loose objects or pack-v2/v3 plus index-v2 pairs.
Gitfiles, linked worktrees, reftable, alternates, replace refs, grafts,
promisor/partial-clone state, optional pack sidecars, links, and special files
fail closed. The manager parses and inertly copies source Git data; it never
executes source hooks, filters, helpers, Git LFS, upload-pack, or maintenance.

Declared and effective identities both remain in receipt v2 and marker v3.
Strict audit rejects substitutions. Advisory audit discloses and independently
audits the exact effective snapshot.

## Operator requirements

Before enabling `go-repository-v1`, an operator supplies:

- a fingerprinted Git release family covered by the rc.5 vectors;
- HTTPS TLS and credential policy, or a trusted OpenSSH client, known-hosts
  state, authentication state, and manager wrapper;
- protected snapshot, receipt, artifact, journal, and marker storage;
- resource limits at least as permissive as the shared-suite fixtures; and
- an audit policy that treats the consuming skill and every effective external
  repository as separate subjects.

Every operation proves the full raw-object snapshot, scans every blob for the
pinned Git LFS parser family, materializes and validates exact regular-file
bytes, computes the build-source digest, and audits that frozen snapshot before
artifact-cache lookup or compiler execution. This order also applies to a
claimed cache hit, cache miss, source-covering dry run, repair, and
coverage-claiming audit. Dry-run and audit-only paths publish no mutation;
syntax-only validation remains a separate non-covering operation.

Receipt v1 and v2 `cache_key` values are recomputed as SHA-256 of CCJ-1
`input`, and marker v3 `receipt_sha256` is recomputed from the complete CCJ-1
receipt. Consumers must verify both relationships rather than accepting the
fields as opaque digests.

Both drivers compile inside the portable `manager-worker-v1` execution policy
described in
[`docs/portable-go-execution-policy.md`](portable-go-execution-policy.md). The
policy identity is part of the hashed receipt input, so it is also part of every
cache key, receipt hash, marker record, and claim. Host capability evidence is
reporting state and never enters that identity.

Syntax-only offline validation may return
`build_repository_unverified_offline` without claiming source coverage.
Install, update, repair, and coverage-claiming audit fail
`build_repository_source_unavailable` before mutation when exact source and
audit evidence are unavailable. Read-only status does not contact the remote
merely to re-test a tag.

The first driver revision performs no post-build signing. A package signing
request fails `build_repository_package_signing_forbidden`; a platform that
requires local signing fails `build_repository_signer_policy_unsupported`
until a separately reviewed signer profile exists.

## Shared-suite consumption and claims

The executable corpus is under
[`conformance/v1`](../conformance/v1). Implementations receive that directory
through `CURATOR_CONFORMANCE_ROOT` and must execute every applicable case
without skip or xfail. The exact candidate protocol pin is
`candidate_protocol_pin.manifest_sha256` in
[`release/1.0.0-rc.6.json`](../release/1.0.0-rc.6.json); consumers must compare
that value with the SHA-256 of `conformance/v1/manifest.json`.

Schema-valid claim-v3 bytes remain historical rc.5 claim bytes, not rc.6
platform evidence. Rc.6 defines no new claim schema and emits no claim. A
future driver/platform tuple requires a versioned claim schema plus immutable
native evidence against the pinned suite. macOS and Windows external-driver
qualification remains pending downstream native runs, and Linux is explicitly
excluded until `TASK-260728-1skseh` passes.
