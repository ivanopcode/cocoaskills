# Security policy

## Reporting

Report suspected vulnerabilities privately to `ivan@relux.works`. Include the
affected protocol section or schema, a minimal reproducer, and the security
impact. Do not include secrets, private registry records, or production keys.

Acknowledgement is targeted within three business days. Coordinated disclosure
and release timing depend on severity and whether deployed implementations need
updates before publication.

## Security model

The protocol treats skill repositories, manifests, registry responses, cache
entries, and bundles as untrusted input. Conforming implementations MUST apply
the parsing limits and validation rules in the normative documents before
using paths, signatures, references, or configuration values.

Repository control includes source, both manifest spellings, a package-provided
root `.csk-install.json`, `go.mod`, `go.sum`, `vendor/`, build constraints,
compiler directives, embedded inputs, assembly, host objects, filenames, and
file sizes. For schema 7 it also includes declared URLs and tags, served refs,
Git configuration and object storage, commit/tag/tree/blob bytes, the root
`skill-build.json`, external source/vendor/embed inputs, and diagnostics.
Build receipts, artifacts, stale markers, and candidate snapshot/cache bytes
are also untrusted parser and hash input until admitted through the protected
cache boundary below.

The protocol provides integrity, provenance, revocation, rollback detection,
and deterministic installation. Source auditing is a review and policy surface.
Capability declarations are a review and policy surface too, except on a script
command that declares `execution_policy: "script-worker-v1"`, where the same
declaration is additionally the input to the manager-mechanism containment
profile of `Enforced script execution boundary` below. Neither form is a kernel
sandbox. A successful audit or registry attestation does not make skill-provided
code safe to execute without the consuming agent's own isolation and
authorization controls.

## Compile-only build boundary

Manifest schema 6 does not introduce package hooks. It admits only untrusted Go
source as compiler input to the closed, manager-owned `go-v1` process described
in `protocol/core.md`. During install, update, repair, status, garbage
collection, and dry-run, a conforming manager:

- MUST NOT invoke a package-provided executable, script, shell command,
  interpreter entry point, shared library, test, package-manager hook, build
  backend, or arbitrary build recipe;
- MUST NOT accept or derive package-controlled executable names, argument
  arrays, environment variables, flags, tags, linker/compiler options,
  toolchains, output names or paths, build scripts, pre/post-build actions, or
  output-selection rules;
- MUST NOT load package-produced code as a compiler or manager plugin, macro,
  annotation processor, source generator, loader, or procedural extension;
- MUST NOT use `sh -c`, `cmd.exe /c`, PowerShell, or any other shell to create a
  build command;
- MUST NOT run `go generate`, `go run`, `go test`, `go install`, `go get`, any
  `go mod` mutation/download command, or a package-selected VCS/network helper;
- MUST NOT invoke or fall back to cgo, an external compiler/linker, libgcc
  discovery, package-controlled assembly, host objects, or a dynamic import
  requested by `//go:cgo_import_dynamic` in active non-standard Go source;
- MUST NOT execute the built output for version discovery, smoke testing,
  post-processing, receipt generation, rollback, validation, or any other
  install-time purpose; and
- MAY perform fixed parsing, hashing, copying, permission changes, and schema
  validation in manager code, and MAY pass validated untrusted source bytes to
  the exact operator-trusted `go-v1` compiler pipeline.

The manager therefore MUST NOT invoke package Make, CMake, Ninja, or Meson
recipes; Cargo build scripts or procedural macros; SwiftPM manifests, plugins,
or macros; Maven or Gradle plugins/tasks; MSBuild tasks or Roslyn generators;
npm lifecycle scripts, Node bundler configuration, loaders, or plugins; Python
PEP 517 build backends; or comparable executable build-system surfaces. There
is no generic-hook fallback when a package cannot fit `go-v1`. A future driver
requires a new closed identifier and independent security review.

`go-v1` forces internal linking and disables libgcc lookup, cgo, PGO,
workspaces, toolchain switching, non-standard assembly, host objects, and the
active non-standard `//go:cgo_import_dynamic` directive. If the native target
or dependency graph cannot build within that surface, installation fails. A
manager MUST reject a build when it cannot apply a mandatory portable control of
`protocol/core.md` section 4.2.1 — the fixed environment, the offline dependency
policy, the frozen snapshot and its identity re-verification, the native target,
or the fixed manager-selected process graph — and it MUST NOT approximate the
contract. That mandatory set is the only cause of rejection at this boundary;
the deferred hardened guarantees below never are.

### Portable execution boundary

Compiled builds run under exactly one named execution policy. Protocol 1.0
defines only the portable `manager-worker-v1` policy of `protocol/core.md`
section 4.2.1. It adds one identity-verified manager-owned worker between the
manager parent and the fingerprinted Go toolchain so that every mandatory
control is installed before any package byte reaches a compiler.

The trusted computing base of that boundary is the installed manager parent and
worker bytes; the worker framing, authentication, and session state machine; the
capability probe and control adapters; the operating-system primitives those
adapters use; the fingerprinted Go binary and `GOROOT` tools; source and
build-root canonicalization, fingerprinting, and input validation; the
operation-private roots and artifact verifier; and the policy, cache, and
receipt canonicalization code. An implementation that launches the worker
through a mutable interpreter or installed package tree adds those to the same
trusted base and MUST say so.

The added threats are worker identity substitution and replacement races,
session framing and replay attacks, out-of-order or duplicated build permits,
and dishonest capability evidence. The contract answers them with pre-launch
identity verification, an in-session identity proof bound to a fresh nonce,
identity re-verification after the last child exits, a state machine that admits
exactly one `go list` and one `go build`, and full worker-domain teardown before
publication.

Package-controlled bytes cannot select or modify the worker executable or its
hidden mode, the Go or tool executable paths, any argument vector, environment
value, working directory, tag, or flag, the applied controls, limits, or
permitted roots, the worker messages or the parent's build permit, or the
graph-validation result, artifact verifier, cache key, receipt, marker, claim,
publication, or artifact execution.

The portable policy is honest about its limits. Every rule it enforces is a
manager mechanism, and each one stops short of exactly one kernel-enforced
guarantee:

| Portable mechanism | Deferred guarantee it is not |
|---|---|
| fixed offline Go module, proxy, checksum-database, and VCS configuration (`network: "none"`) | `total-network-denial` |
| frozen snapshot the manager and worker never write to, with pre/post identity re-verification | `read-only-source-and-toolchain` |
| fixed manager-selected four-node graph with per-program identity verification | `exact-executable-allowlisting` |
| operation-private write roots and a verified private-staging artifact | `private-build-root-only-writes` |
| parent-enforced deadline, combined-output, and artifact bounds plus available inventory controls | `hard-aggregate-descendant-resource-bounds` |
| preflight of the mandatory portable controls | `fail-closed-capability-preflight` |

Those six guarantees belong to the separately tracked hardened execution profile
(`STORY-260728-327soo`) and MUST NOT be claimed by a portable build. Their
absence never rejects a portable build; falsely recording them does. The failure
boundary is exactly one rule: a mandatory portable control that cannot be applied
rejects before the worker starts, and nothing else at this boundary rejects.

Because the execution-policy identity is inside the hashed build input, a
portable artifact, receipt, marker, and claim can never be mistaken for hardened
output. Host capability evidence is one closed `capability-evidence-v1` record
per operation over the exhaustive `rc5-native-control-inventory-v1` inventory. It
is result-only reporting and never enters cache, receipt, marker, or claim
identity, so it cannot become a capability claim a reader would rely on.

### Verified provider boundary

Rc.8 carries the explicit verified assurance contract introduced by rc.7
without changing the portable boundary. The rc.8 publication recovery changes
release automation and version pins, not the provider trust model.
Verified mode trusts a separately installed, signature-policy-checked host
provider and binds its contract, provider id, exact binary SHA-256, fresh
capability receipt, permit, execution receipt, and build input into disjoint
typed identities. Portable observations, receipts, cache hits, journals, and
claims cannot satisfy those fields.

The common `host-execution-provider-v1` contract names outcomes rather than
Linux, macOS, or Windows mechanisms. Native enforcement and qualification stay
with provider implementations. This release ships none and claims none. A
missing, unhealthy, incompatible, stale, partial, or identity-mismatched
provider fails before compilation with no portable retry.

Provider code is outside package trust. Skills MUST NOT vendor provider
executables, native libraries, helpers, or other compiled artifacts. Provider
installation and updates use a separate trusted host-administration channel.
Compromise of a trusted provider, its signing authority, the manager/provider
authentication channel, the operating-system kernel, or the manager principal
is inside the verified trusted computing base.

### Compiler-input trust boundary

Compile-only does not mean trusted source or an invulnerable compiler. Malicious
source may exploit parser/compiler defects or exhaust CPU, memory, disk,
processes, diagnostic output, or filesystem traversal. The selected Go
toolchain, manager, operating system, hashing/canonicalization code, and
manager-owned policy/staging roots are trusted; package source and compiler
diagnostics are not. The manager and worker never write to the frozen source
snapshot and re-verify its identity before publication, standard input is
closed, and stdout/stderr MUST be bounded and redacted before presentation.
Under the portable execution policy, managers MUST apply the mandatory controls
of `protocol/core.md` section 4.2.1 plus exactly the controls that the
exhaustive `rc5-native-control-inventory-v1` inventory marks available for the
host platform, and MUST record the remaining inventory controls as unavailable
rather than as applied. No control outside that inventory is applied or
reported, and an unavailable one is never a rejection. Bounding a compile-only
operation is not containment of a hostile compiler; the hardened profile is what
promises that. The compiled artifact remains untrusted package code until a user
later invokes the installed command under the consuming environment's
authorization and isolation policy.

### Protected-cache trust boundary

Receipt and marker SHA-256 values establish deterministic consistency and
currentness; they are not signatures, MACs, registry attestations, or
independent proof that `go-v1` produced an artifact. Persistent reuse is
permitted only inside manager-created state whose ownership, private
permissions or DACL, containment, file type, and link safety are revalidated
on every lookup and before locked publication/commit. A self-consistent receipt
outside that boundary is attacker-controlled and MUST be treated as a miss.
Managers that cannot enforce the boundary MUST disable persistent reuse and
rebuild from the revalidated snapshot; they MUST NOT repair permissions and
adopt pre-existing candidate bytes.

The v1 boundary assumes an adversary cannot mutate protected state as the
manager's operating-system principal or as a trusted administrator. Arbitrary
same-principal code execution, administrator/root or kernel compromise, and
hostile storage below the operating system are outside this install-time
invariant because they can replace the manager and markers as well as receipts.
Deployments that cannot accept that assumption MUST disable persistent cache
reuse. Authenticated cross-principal artifact provenance would require a future
protocol; a plain receipt hash does not provide it.

## External build repository boundary

Manifest schema 7 introduces external Git only through the closed
`go-repository-v1` driver. It does not make Git configuration, a repository
descriptor, a package manager, or a source build system trusted or executable.
Schemas 1 through 6, `go-v1`, receipt v1, marker v1/v2, and rc.4 retain their
existing security meaning and MUST NOT accept this surface.

### Immutable acquisition and source proof

The manager MUST bind every declaration to both an explicit object format and
a full commit object ID. An OPTIONAL tag is a second exact assertion. A tagged
declaration MUST use only a fresh exact-tag acquisition and MUST recompute and
peel the returned object chain to the locked commit. An untagged declaration
MUST use only the full lock. A manager MUST NOT fall back between those paths
or to a branch, all-tags request, alternate ref, configured refspec, named
remote, clone, checkout, archive, or repository-selected Git command.

Network and local-substitution bytes MUST converge on one manager-owned raw-
object proof before they become a source snapshot. The manager MUST use a
trusted Git distribution, private repository and configuration, fixed child
graph, clean environment, operator-owned transport and credential policy, and
bounded raw-object reader. It MUST NOT inherit or execute source-selected
helpers, hooks, filters, LFS, submodules, alternates, replacements, grafts,
promisor/lazy fetch, maintenance, proxy commands, checkout transforms,
attributes, text conversion, or source Git configuration.

HTTPS MUST verify TLS without redirects. SSH MUST use operator-selected known-
hosts and authentication state through the manager's fixed wrapper and trusted
client. Repository data MUST NOT select an identity, agent, known-hosts file,
proxy/jump/local command, timeout, forwarding, control socket, TTY, SSH option,
or remote command.

The manager MUST independently recompute consumed object IDs, parse the selected
commit and tag chain, prove the complete tree/blob graph, reject unsafe paths,
links, special files, gitlinks, incomplete objects, and every pointer accepted
by the pinned Git LFS parser family, and materialize exact verified blob bytes.
It MUST NOT hydrate a missing object or LFS payload. Local substitutions MUST
use the deliberately narrow ordinary non-bare files-ref layout; unsupported or
unsafe Git administration and object formats MUST fail closed rather than
invoke source-repository Git behavior.

### Descriptor, output, and audit isolation

`skill-build.json` is untrusted compiler-selection data. It MAY select only a
closed driver, explicit build root, and source directory. The manager MUST
enforce the nearest-`go.mod` rule and MUST NOT discover a module or target. The
whole external snapshot is the source-identity and audit subject; only the
selected build root is compiler-visible. External bytes MUST NOT enter agent
context, the consuming skill's runtime copy, another repository's build, a host
module cache, workspace, or network dependency.

The consuming manifest owns the command key. The manager owns the artifact
basename/path, staging path, shim, Go executable and argv, environment, target,
compiler/linker policy, process graph, and output validation. The manifest,
descriptor, repository, and substitution MUST NOT select an executable, argv,
environment, compiler flag or tag, output, toolchain, hook, plugin, generator,
recipe, signer, or fallback. The artifact MUST NOT be executed during manager
activity.

Before an artifact-cache lookup or compiler child, the manager MUST freeze and
validate the complete effective snapshot, compute
`curator-build-source-v1`, select the descriptor target, and audit the external
repository separately from the consuming skill. This ordering applies to real
builds, claimed cache hits, and dry-runs that claim source or audit coverage.
Skill evidence MUST NOT attest external source, and external evidence MUST NOT
attest the skill.

Declared and effective identities MUST both enter the receipt-v2/cache input.
An operator substitution MUST NOT erase the declaration or claim its bytes.
Strict audit MUST reject substitutions; advisory audit MUST disclose them and
audit the exact effective snapshot. A self-asserted receipt/marker trust or
tag-verification field MUST NOT replace acquisition, object proof, audit, or
protected-state admission.

### Offline, status, credential, and signing ownership

A syntax-only offline check unable to obtain an exact snapshot MAY report
`build_repository_unverified_offline`, but MUST NOT claim source, audit, cache,
receipt, marker, artifact, or installation coverage. Install, update, repair,
and coverage-claiming audit MUST fail before mutation when the exact source
cannot be obtained and audited. Read-only status MAY prove currentness from
exact protected snapshot, receipt, marker, artifact, and shim relationships; it
MUST NOT contact the remote merely to retest tag movement and MUST NOT report
missing or unprovable protected evidence as current.

Git/SSH/HTTPS credentials, known-hosts state, authentication mode, proxy
policy, timeout, and executable selection are operator-owned. Package or
repository data MUST NOT select them, and source credentials MUST NOT enter the
compiler environment, receipt trust fields, or marker.

`go-repository-v1` revision 1 performs no post-build signing, timestamping, or
notarization. Signing identities and notarization credentials are operator or
release-pipeline secrets and MUST NOT appear in a manifest, descriptor,
repository, compiler environment, receipt trust field, or marker. A platform
requiring local signing MUST reject the artifact until a separately reviewed
signer profile fixes the signer, identity, argv, options, process/network
policy, cache identity, protected publication, and rollback behavior.

### Closed future-driver admission

The external Git envelope is not a generic build frontend. Each future language
requires a new driver identifier and an independent protocol and security
review of complete compiler-visible input, trusted toolchain/sysroot identity,
fixed process graph/environment/arguments, offline dependency/link policy,
manager-derived output, signer boundary, receipt/marker/cache identity,
audit-before-build ordering, lifecycle, and platform vectors.

A future driver MUST reject package-selected hooks, plugins, macros,
generators, annotation processors, build tasks, recipes, response files,
linkers, and produced-program execution. A manager MUST NOT enable a future
language by widening `go-v1` or `go-repository-v1`, accepting an unknown
driver, or adding a generic fallback.

## Enforced script execution boundary

Script commands have no compile-only boundary. A declared-only script command's
file content is audited, but its execution is a bare launcher `exec`: nothing
prevents a script whose manifest declares `network: "none"` from fetching and
running new code, and the reader of an audit record cannot tell. Manifest
schema 8 closes that gap for the commands that opt in. A script command that
declares `execution_policy: "script-worker-v1"` runs under the portable script
execution policy of `protocol/core.md` section 4.1.1, which turns the section
4.3 capability declaration of that document into a manager-mechanism
containment profile applied before the interpreter starts.

Opt-in is explicit and per command. Schema 7 and earlier script commands, and
schema 8 script commands that omit the field, keep their existing behavior
exactly: declared-only, launcher `exec`, no enforcement and no enforcement
claim. Audit labels every one of them `script-command-declared-only` rather than
silently changing what they do. The single admissible value of the field
can only narrow what a command may do, so a package can opt in to containment
and can never opt out of a manager-owned control, select a control, or widen a
profile.

The process graph is fixed:

```text
manager parent
  -> identity-verified manager-owned script worker
       -> identity-verified interpreter for the declared identifier
            -> manager-resolved executables named by the `exec` capability
```

Under `exec: "none"` the graph is exactly the first three nodes. The worker is
the decision-0006 boundary reused: a hidden re-execution of the installed
manager, selected by no package input and by no shell. The interpreter is named
by a closed identifier in the manifest and resolved by the manager to an
operator-trusted executable; the package supplies neither its path nor its
bytes. The shebang line and the Windows file association are inert and MUST NOT
select the executed program, because both are package-controlled content and
neither exists portably.

### Script trusted computing base

The trusted computing base of this boundary is the installed manager parent and
worker bytes; the worker framing, authentication, and session state machine;
the capability probe and control adapters; the operating-system primitives those
adapters use; the resolved interpreter executable; interpreter resolution,
environment construction, and `PATH` construction; the operation-private roots;
and the capability-evidence and audit-record canonicalization code.

The interpreter's standard library, `site-packages`, `node_modules`, and every
other installed package tree it loads are inside that trusted base and are not
verified. The manager verifies the interpreter executable file's identity per
invocation; it does not fingerprint the interpreter installation, because that
tree is user-writable, is mutated by unrelated package installations, and
legitimately differs between two invocations of the same skill. This is the
same rule `protocol/core.md` already states for any implementation whose worker
graph contains an interpreter and an installed package tree, and it applies to
a manager that is itself distributed as an interpreted package.

The added threats are worker identity substitution and replacement races,
interpreter substitution between resolution and launch, session framing and
replay attacks, and dishonest capability evidence. The contract answers them
with pre-launch identity verification of both the worker and the interpreter
file, an in-session identity proof bound to a fresh nonce, identity re-checks at
the launch boundary, a closed per-invocation evidence record, and full
worker-domain teardown before the invocation returns.

### Enforced script mechanisms and deferred guarantees

The enforced script policy is honest about its limits in exactly the way the
portable build policy is. Every rule it enforces is a manager mechanism, and
each one stops short of a kernel-enforced guarantee that this release does not
provide and MUST NOT claim:

| Portable mechanism | Deferred guarantee it is not |
|---|---|
| offline environment configuration plus proxy and resolver scrubbing when the derived network capability is `none` | `script-total-network-denial` |
| declared `network` host globs recorded and reported, with no portable filtering applied | `script-network-host-allowlisting` |
| manager-built `PATH` over the resolved interpreter and the resolved declared `exec` names, with the inherited `PATH` discarded | `script-exact-executable-allowlisting` |
| operation-private temporary, configuration, and cache roots plus a manager-selected working directory | `script-private-runtime-area-only-writes` |
| per-invocation identity verification of the worker and of the resolved interpreter file, with the interpreter's library and installed package trees declared trusted | `script-read-only-runtime-tree` |
| explicit standard-stream binding, descriptor and handle release, worker-domain teardown, and every inventory control the probe found available | `script-hard-aggregate-descendant-resource-bounds` |
| mandatory-control preflight at install or update and again before every enforced invocation | `script-fail-closed-capability-preflight` |

Those seven guarantees are reserved for a separately named script execution
policy backed by a verified provider, not for this portable one, and MUST NOT
be claimed by an enforced invocation. Their absence never rejects an
invocation; falsely recording them does. None of the seven names aliases a
guarantee deferred by the build policy, a build inventory control, or a script
inventory control, and the build policy's six deferred guarantees may not
appear on a script surface either.

Per-host network filtering is deferred, not partial. Everything available
unprivileged on the supported platforms — proxy variables, `NO_PROXY`, resolver
configuration, interpreter proxy settings — is honored only by cooperating
libraries and is ignored by a raw socket or an IP literal, and no supported host
offers host-granular filtering as a kernel control. Declared host globs are
therefore reporting-only: they configure nothing, they never appear as an
inventory entry or an applied control, and an enforced command that declares
them is admitted under the `script-command-unfiltered-declared-network` audit
warning class rather than rejected, so it still receives the `exec`,
`filesystem`, and environment controls it can have.

Platform reach is deliberately uneven and stated rather than smoothed over.
The mandatory portable set is environment-, process-, and path-level and applies
in full on macOS, Linux, and Windows. Kernel confinement is not part of it. On
macOS and Windows this release applies no kernel confinement primitive to an
enforced invocation at all: `exec` and `filesystem` narrowing there are `PATH`,
environment, private-root, and working-directory mechanisms, and a descendant
that resolves an absolute path or writes outside the private roots is not
stopped by this policy. On Linux, execute-right, write-right, and network-domain
controls are host-conditional — present on some hosts and absent on others of
the same platform — so the manager probes them per invocation, applies exactly
what it found, and records the rest as unavailable. Applying a host-conditional
control narrows that one invocation; it never upgrades the policy-level
guarantee, which stays deferred precisely because the mechanism varies between
hosts. A reader MUST NOT infer from `exec: "none"` or `network: "none"` a
denial that the host does not provide.

Package-controlled bytes cannot select or modify the worker executable or its
hidden mode, the interpreter identifier's resolution or path, any argument
vector the manager builds, environment value, `PATH` entry, working directory,
private root, applied control, or limit, the worker messages, the capability
evidence record or its destination, or the audit record.

### Script evidence and failure boundary

Host capability evidence is exactly one closed `script-capability-evidence-v1`
record per enforced invocation, over the exhaustive
`script-worker-v1-native-control-inventory-v1` inventory. It is a separate
record version over a separate inventory version from the build side on purpose:
a build-motivated revision of either must not silently re-scope what a script
invocation claims, and a script record must never be admissible as build
evidence.

Availability is probed once, before the worker launches, and never re-probed
while the command runs. A cached, inherited, or configured result is not a
probe, and an install-generation record replayed at invocation time is a cached
result, so the manager preflights the mandatory set at install or update as an
additional moment and still probes per invocation.

The record is result-only. It is exposed through dry-run plan and status results
and through an operator-selected diagnostic destination, and it MUST NOT be
written to the command's standard output or standard error — those streams
belong to the caller's pipeline, and evidence on them would be both a corrupted
command output and a package-visible side channel. It MUST NOT enter a cache
key, receipt input, marker record, or conformance claim. Retention is bounded,
machine-local, and operator-configurable by default, because one record per
invocation kept forever is an unbounded log with a privacy surface.

The failure boundary is exactly one rule: a mandatory portable control that
cannot be applied rejects with `script_execution_control_unavailable` before the
worker starts and runs nothing. An unavailable inventory control, a
host-conditional control the probe did not find, and the absence of any deferred
script guarantee never reject an invocation, never produce a diagnostic, and
never prevent the command from running.

Opting in is a breaking change for the skill that does it, and that is the
intent. An undeclared environment variable is absent and an undeclared
executable is not on `PATH`, so a declaration written as documentation stops
being free. Enforcement is never applied implicitly: it is a versioned,
per-command, audit-visible opt-in, and everything that has not opted in carries
a label saying so.

## Registry security state

Client snapshot high-water state is security state. It is stored separately
from disposable registry responses, written atomically before acceptance, and
included in protected machine backups. Existing corruption or a failed write
is fail-closed. Loss of all local state still requires an out-of-band
authenticated checkpoint or explicit operator rebootstrap; signatures alone
cannot prove that a newly presented snapshot is not an old valid view.

Registry trust anchors are distributed out of band. Removing a key from the
pinned set revokes trust in signatures made solely by that key. Key rotation
and incident behavior are defined in `protocol/registry.md`. The production
registry threat model, including replay, rollback, equivocation, cursor abuse,
resource exhaustion, credential compromise, crash recovery, and backup
rollback, is normative in `profiles/registry-service.md`.

## Release review

Stable protocol releases require:

1. schema and vector CI on all supported operating systems;
2. both conforming clients passing the same released vectors;
3. review of changes to canonicalization, hashing, signatures, snapshots,
   transparency logs, source identities, and path handling;
4. a signed release tag and immutable release artifacts.

`1.0.0-rc.2` is not promoted to stable until an independent security review
and an independent interoperability review conform to
`reviews/review-report-v2.schema.json`. Each report identifies the reviewed
candidate commit and a public authorship trail. Stable release CI rejects open
critical or high findings and rejects normative changes made after either
review. See `RELEASE.md` for the complete gate.
