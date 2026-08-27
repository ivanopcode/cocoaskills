# Decision 0005: external build repositories

## Context

Manifest schema 6 can compile only source that belongs to the consuming skill's
validated raw snapshot. It cannot name, lock, audit, or cache compiler input
from a separate Git repository. Extending `go-v1` in place would change its
source identity, receipt, marker, and audit meaning after the rc.4 contract was
fixed.

External source also creates two identities that cannot be collapsed. The
manifest declares a network repository and immutable lock, while an operator
development substitution may select different effective bytes. Recording only
the declaration would misdescribe compiled input; recording only the
substitution would discard declared provenance.

This decision implements the accepted external-build-repository architecture,
revision 6, SHA-256
`2abae77d80eba6789f9911db7e9722595b4f21ba47391ca9eafd0064af03d67e`.

## Decision

### Version boundary

External build repositories require manifest schema 7, build receipt schema 2,
install marker schema 3, `Skillfile.dev.json` schema 2, descriptor
`skill-build.json` schema 1, and the closed driver identifier
`go-repository-v1`. They are a new rc.5 protocol surface.

Manifest schemas 1 through 6, the `build-receipt-v1`, `install-marker-v2`, and
`conformance-claim-v2` schema bytes, marker schema 1, conformance claim schema
1, and the `go-v1` package surface are frozen. A reader or writer MUST NOT
reinterpret, widen, relabel, or infer any rc.5 meaning from an rc.4 object.
Schemas 1 through 6 MUST reject every schema-7 field and `go-repository-v1`.
The single exception is the unreleased execution-policy revision of decision
0006: it adds no package-controlled field, but it does change the generated
`go-v1` receipt example and every `go-v1` logical cache key, which is what
prevents a pre-revision candidate entry from aliasing a portable one.

### Manifest and descriptor ownership

Schema 7 adds a strict top-level `build_repositories` map. Each entry MUST bind
one canonical HTTPS or SSH network identity to an immutable object format and
full lowercase commit object ID and MAY add one safe tag assertion. A command
using `go-repository-v1` MUST select exactly one repository entry and exactly
one logical target from that repository's root `skill-build.json`.

The descriptor filename is manager-neutral. It sits in a source repository that
is generally not owned by the skill author and is read by any conforming
manager, so it names the artifact being described — a skill build — rather than
any one implementation. Schema 7 is unreleased, so exactly one name is defined
and there is no alias to accept or migrate.

The consuming manifest owns the command key. The repository descriptor owns
only `driver`, `build_root`, and `source_dir`. The manager MUST derive the shim
name and the sole artifact-relative path as `bin/<command>` or
`bin/<command>.exe`. Neither the manifest nor descriptor nor repository MAY
select a program, argv, environment, flag, tag passed to the compiler,
toolchain, output name or path, signing identity, hook, plugin, generator,
recipe, post-build action, fallback, or secondary artifact.

A descriptor target MUST select one explicit module root and source directory.
`source_dir` MUST equal or be below `build_root`; `build_root` MUST contain
`go.mod` directly and that file MUST be the nearest ancestor `go.mod` of
`source_dir`. Nested modules are admitted only by explicit target selection.
The manager MUST NOT discover a target, module, command, or output. The whole
repository remains the validation, identity, and audit subject, while only the
selected build root is compiler-visible. External repository bytes MUST NOT
enter agent context or the consuming skill's runtime copy.

### Declared and effective source

Every external command MUST retain both:

- a declared state containing canonical network identity, transport, immutable
  object format and full commit lock, and OPTIONAL tag; and
- an effective state containing the exact identity kind and value, transport
  when applicable, actual object format, full commit, substitution state, and
  `curator-build-source-v1` digest used for the build.

Without substitution, the effective identity, object format, and commit MUST
equal the declaration and `substituted` MUST be false. With an operator
substitution, the declaration MUST remain unchanged and the effective state
MUST name exactly `local-path` or `network-git`. Package data MUST NOT create or
alter a substitution. A substitution MAY replace acquisition only; it MUST NOT
change the repository identifier, logical target, driver, command name,
output, compiler policy, credential, or signing policy. Strict audit MUST
reject substitutions; advisory audit MUST disclose and audit the exact
effective snapshot.

The object format is part of the immutable lock. It MUST be `sha1` with a
40-character full object ID or `sha256` with a 64-character full object ID.
Branches, ranges, abbreviations, `HEAD`, revision expressions, and
package-selected local paths MUST NOT satisfy a declaration.

The full commit object ID is the lock. A declared tag is an additional exact
assertion, not a replacement lock:

- an untagged declaration MUST acquire only the full locked object ID;
- a tagged declaration MUST acquire only `refs/tags/<tag>` in fresh private
  state, recompute and peel the lightweight or annotated-tag chain, and require
  its terminal commit to equal the full lock; and
- neither path MAY fall back to a branch, all-tags fetch, alternate ref, or the
  other acquisition form.

Direct-object server policy MUST NOT change the tagged path. A moved tag MUST
fail as `build_repository_ref_moved`; a missing, inaccessible, or transport-
unavailable exact source MUST fail as
`build_repository_source_unavailable`. A syntax-only offline operation that
cannot obtain the exact source MAY warn
`build_repository_unverified_offline`, but MUST NOT claim source, audit,
receipt, artifact, marker, or installation coverage.

### Raw snapshot and audit boundary

Network and local-substitution acquisition MUST converge on one raw-object
proof. The manager MUST use an operator-trusted, fingerprinted Git release
family and manager-owned configuration, process graph, credentials, transport
policy, private repository, object reader, and resource limits. Repository
data MUST NOT select or extend Git, SSH, HTTPS, credential, proxy, helper,
filter, hook, LFS, submodule, alternate, replace, graft, checkout, archive,
maintenance, or lazy-fetch behavior.

Local substitutions in v1 MUST be ordinary non-bare files-ref worktrees with a
real `.git` directory. A manager MUST parse admitted configuration, refs, and
object containers as bounded untrusted data and MUST NOT execute repository Git
behavior. Gitfiles, linked worktrees, bare repositories, reftable or unknown
extensions, unsafe links/layout, unsupported pack/index forms, alternates,
promisor state, grafts, replace refs, shallow state, optional pack sidecars,
and incomplete objects MUST fail closed.

The manager MUST independently recompute every consumed object ID, parse the
selected commit and tag chain, prove the complete tree/blob graph, reject
gitlinks and every Git LFS pointer form accepted by the pinned
`git-lfs-pointer-parser-v3.7.1` family, and materialize only exact verified blob
bytes. Malformed commit or tag semantics MUST fail as
`build_repository_git_object_semantics_invalid`; missing or incomplete graph
bytes MUST fail as `build_repository_incomplete_source`; a matched LFS pointer
MUST fail as `build_repository_git_lfs_unsupported`. The manager MUST NOT
hydrate or execute any missing content.

Before any artifact-cache lookup or compiler child, including on a claimed
cache hit, the manager MUST freeze and validate the complete effective
regular-file snapshot, compute `curator-build-source-v1`, select and validate
the descriptor target, and audit the external repository independently from
the consuming skill. The audit subject MUST bind declared and effective
identity, object format, full effective commit, source digest, descriptor
target, substitution state, and successful tag assertion when applicable.
Skill evidence MUST NOT attest external source, and external evidence MUST NOT
attest the skill.

Persistent source or artifact reuse is permitted only within a
manager-created, owner-protected, contained, link-safe boundary that is
revalidated before lookup and publication. Receipt and marker hashes are
consistency identifiers, not signatures or provenance. A manager unable to
prove the boundary MUST treat it as a miss or non-current and MUST rebuild from
an exact revalidated snapshot; it MUST NOT repair permissions and adopt
candidate bytes.

### Receipt, marker, status, and lifecycle

External commands use receipt schema 2. Its canonical input MUST bind declared
and effective source state, repository and descriptor target, command,
build-root/source selection, native target, trusted toolchain, and complete
closed policy. A tagged unsubstituted receipt MAY be published only after that
operation proves exact-tag equality with the lock. The receipt MUST NOT contain
a self-asserted trust or `tag_verified` boolean.

Schema 7 MAY mix local `go-v1` and external `go-repository-v1` commands.
`go-v1` commands MUST retain receipt schema 1 and schema-6 build-source
semantics. External commands MUST use receipt schema 2 and their own effective
source state. Both drivers run under the same portable `manager-worker-v1`
execution policy of decision 0006. Marker schema 3 MUST represent both receipt
versions and the execution policy explicitly and MUST NOT infer receipt meaning
from a driver name. Marker v3 top-level
`build_source` is present exactly when at least one active local `go-v1`
command requires the schema-6 skill snapshot identity.

Read-only status MAY prove currentness from an exact protected snapshot,
receipt, marker, artifact, and shim relationship. It MUST NOT contact the
remote merely to retest tag movement and MUST NOT fetch, repair, adopt,
quarantine, compile, sign, or execute. Missing or unprovable protected evidence
MUST NOT be reported current. Install, update, repair, and coverage-claiming
audit MUST obtain and audit the exact effective snapshot before mutation.
Repair of an unsubstituted tagged declaration MUST repeat the exact-tag-only
path; an old snapshot or direct-object fetch MUST NOT substitute for the tag
assertion.

Publication MUST use manager-private staging and the existing serialized
manager-home transaction. The consumer ledger MUST commit last and rollback
MUST reverse committed targets while holding the same lock. Shims MUST resolve
only to the marker-selected protected artifact and MUST NOT point into a Git
object store, source snapshot, checkout, staging directory, or script runtime.
The manager MUST NOT execute a built artifact during validation, installation,
status, repair, rollback, or garbage collection.

### Credential, signing, and future-driver ownership

Credentials, host-verification state, Git/SSH executables, proxy policy,
timeouts, and authentication modes are operator-owned. They MUST NOT appear in
the manifest, descriptor, repository, compiler environment, receipt trust
field, or marker.

The first `go-repository-v1` revision performs no post-build signing,
timestamping, or notarization. Signing identities and notarization credentials
are operator/platform secrets and MUST NOT be selected by package data. A
platform that requires local signing MUST reject the build until a separately
reviewed signer profile defines its fixed tool, process graph, network policy,
identity, cache input, publication, and rollback behavior.

The external Git envelope is not a generic build frontend. Every future
language requires a new closed driver identifier and independent protocol and
security review. The new driver MUST define complete compiler-visible input,
trusted toolchain/sysroot identity, fixed process graph/environment/arguments,
offline dependency and link policy, manager-derived output, signer boundary,
receipt/marker/cache identity, audit ordering, dry-run, rollback, status,
repair, garbage collection, and platform vectors. It MUST reject
package-selected hooks, plugins, macros, generators, annotation processors,
tasks, recipes, response files, linkers, and produced-program execution. A
manager MUST NOT broaden `go-v1` or `go-repository-v1` or provide a generic
fallback.

## Stable failure classes

The following architecture-level outcomes are interoperable semantic classes
and MUST remain distinguishable:

- `build_repository_source_unavailable`;
- `build_repository_ref_moved`;
- `build_repository_unverified_offline`;
- `build_repository_incomplete_source`;
- `build_repository_git_object_semantics_invalid`;
- `build_repository_git_lfs_unsupported`;
- `build_repository_local_gitfile_unsupported`;
- `build_repository_local_bare_unsupported`;
- `build_repository_local_linked_worktree_unsupported`;
- `build_repository_local_layout_unsafe`;
- `build_repository_local_format_unsupported`; and
- `build_repository_local_object_format_unsupported`.

Schema, unsupported-version, unsupported-driver, descriptor, identity,
credential-policy, audit, protected-boundary, receipt, marker, artifact,
currentness, and transaction failures MUST also be typed and fail closed before
the prohibited downstream action. The manager profile defines their CLI
rendering; implementations MUST NOT collapse a known semantic failure into a
cache hit, audit success, source unavailability, or generic fallback.

## Rejected alternatives

- Reusing or widening `go-v1` was rejected because it would change the frozen
  rc.4 source, receipt, marker, and audit contract.
- Tag-only, branch, abbreviated-ID, and raw revision selection were rejected
  because compiler input would be mutable or ambiguous.
- A descriptor-selected binary, output, argv, environment, hook, build system,
  signer, or fallback was rejected because it transfers execution and
  publication control to untrusted source.
- Checkout, archive, filters, submodules, LFS hydration, alternates, promisor
  fetch, source Git configuration, and implicit clone/fetch behavior were
  rejected because they admit unbound objects, transformations, processes,
  credentials, or network reads.
- Receipt-only provenance and permission repair/adoption were rejected because
  an attacker can create internally consistent bytes outside protected state.
- Storing only declared or only effective source was rejected because either
  form loses a required half of the cache and audit identity.
- Install-time signing and generic future-language build systems were rejected
  because their secrets, processes, and compiler execution surfaces are not
  covered by the first driver.

## Compatibility impact

This decision adds only the schema-7/rc.5 surface. All rc.4 artifacts retain
their exact bytes and meanings. A schema-7-aware implementation MUST continue
to read the older supported versions according to their existing contracts,
but MUST write the version required by the active feature and MUST reject
unsupported versions rather than infer or downgrade them.

## Security impact

The trusted computing base expands to the manager's exact Git distribution and
object parser, operator-owned HTTPS/SSH credential and host-verification
policy, protected external snapshot state, and the existing trusted Go
toolchain and transaction boundary. Remote Git data, repository configuration,
objects, descriptor, source, diagnostics, cache candidates, receipts, markers,
and built artifacts remain untrusted. Same-principal, administrator/root,
kernel, or hostile-storage compromise remains outside the local protected-cache
invariant; deployments that cannot accept it MUST disable persistent reuse.
