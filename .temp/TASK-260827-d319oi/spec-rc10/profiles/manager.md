# Curator Manager Profile 1.0

This document is normative for implementations claiming the **manager**
conformance class. It defines behavior around the portable objects in the
protocol core. It does not prescribe an executable name or machine-home path.

For rc.9, assurance selection and verified-provider behavior are also governed
by [`protocol/assurance.md`](../protocol/assurance.md). Portable remains the
default behavior defined below. A manager MAY omit verified support, but if it
accepts verified selection it MUST implement the complete provider contract,
identity separation, typed records, and fail-closed no-downgrade rules. It MUST
NOT claim verified support merely because portable controls or host primitives
are present.

## 1. Machine state and configuration

Each manager selects its own `<manager-home>`, user-config environment
variable, and system-config path. It MUST document them and MUST keep its
caches, runtime store, audit state, and global/hybrid state below that home.
Different implementations do not share machine-local state by default.

The logical user configuration conforms to `manager-config-v1.schema.json` and
contains a source root, project registrations, manager defaults, source
allowlist, audit policy, and pinned registries. The configuration file SHOULD be
readable and writable only by its owner where the platform supports
permissions.

Managers MUST reject unknown configuration fields and apply schema defaults
before use. `projects` member names, `project_alias`, and `checkout_alias` are
portable identifiers; the latter two default to the member name when absent or
null. They are derived machine matching keys, distinct from the operator-facing
Unicode `Skillfile.json` `project.alias`. Agent lists are sets of portable
identifiers. `preferred_locale: null` means no machine preference.

An audit registry requires `name` and canonical `url`; `public_keys` defaults to
an empty set and `enabled` defaults to true. A registry with no pinned key is
not trusted and produces a warning. Registry record cache TTL defaults to 3600
seconds, offline grace to 604800 seconds, snapshot maximum age to 604800
seconds, and future clock skew to 300 seconds. Zero cache TTL disables fresh
cache hits; zero offline grace disables stale fallback; zero clock skew permits
no future offset. The backend request limit defaults to 1048576 bytes and MUST
NOT exceed 10485760 bytes.

An OPTIONAL system configuration conforms to `system-config-v1.schema.json`
and is merged before parsing the effective user configuration:

1. `locked` contains only `audit_registries`,
   `disable_builtin_registries`, `allowed_sources`, and `audit`. Operator
   credential selections — the `build_ssh` scopes among them — are never
   lockable: section 12.2 makes credential material operator-owned, and a
   system file MUST NOT select or constrain it;
2. a locked key MUST be set by the system file and overrides a user value with
   a warning naming the system file;
3. an unlocked system key is a default and a user value wins;
4. malformed or unreadable enforced configuration fails closed.

Configuration writes MUST use a same-directory temporary file followed by
atomic replacement. Implementations SHOULD serialize concurrent writers with a
per-config lock and MUST never expose a partially written JSON object.

## 2. Installation lifecycle

The manager MUST implement installation as read-only planning, optional private
compilation, and one serialized publication transaction. This lifecycle applies
to project, global, and hybrid installation, upgrade, and repair. Package code
MUST NOT execute during any phase; the closed `go-v1` compiler process graph
below is the only exception to passing package bytes to a trusted tool.

### 2.1 Read-only planning and source gates

For each operation the manager performs these phases in order:

1. load `Skillfile.json`, or skip an absent manifest, and determine effective
   agents and locale;
2. verify generated project and adapter paths are ignored by git, load
   development substitutions, reject them under strict audit, and add
   applicable hybrid declarations;
3. resolve every ref to an immutable raw snapshot and validate its complete
   tree before reading a build-cache entry or starting any Go process;
4. parse and validate both manifest names according to Protocol Core section
   4, validate link-free disjoint runtime roots, build roots, and source
   directories, and derive every artifact-relative path from its command name;
5. exclude every build root as a whole subtree from agent context before
   locale rendering and from commit-keyed runtime copying, identically for a
   real build, cache hit, and dry run;
6. compute `curator-build-source-v1` over every regular file in each fully
   validated raw snapshot, including a package-provided root
   `.csk-install.json`, and freeze that snapshot instance until its last build
   child exits;
7. build the provider-first dependency closure, apply source allowlists and
   snapshot checks, validate every skill package, reject command, shim,
   portable-path, case-folding, and platform-path collisions, and verify system,
   legacy command, and MCP requirements;
8. run source-audit policy, resolve trusted registries and attestations, reject
   revocation or strict unknown results, and apply moved-tag policy; and
9. only after all preceding gates pass, resolve and fingerprint a trusted
   toolchain, derive build inputs and logical cache keys, validate the protected
   cache boundary, and inspect eligible receipts and artifacts read-only.

Snapshot validation MUST reject links, special files, invalid protocol paths,
duplicate encoded paths, and platform path collisions. A build root MUST be a
real link-free directory below the snapshot, MUST NOT be `.`, MUST be disjoint
from every runtime root, and MUST contain the nearest ancestor `go.mod` of each
of its commands' `source_dir` values. Every declared build root MUST be used.
Every module directory declared by a schema-8 build command satisfies the same
directory, link, and `.` rules, MUST contain `go.mod` directly, and MUST be
disjoint from every other declared module directory, every build root, and
every runtime root under both exact and platform-path comparison, as required
by Protocol Core section 4.2.3.
Generated artifacts remain only in manager-owned staging and immutable cache
state and MUST NOT become agent-facing context.

Audit and registry gates therefore precede both persistent cache lookup and
compilation. Neither a cache hit nor a dry run may bypass source validation,
static context exclusion, build-source hashing, closure construction, collision
checks, audit, attestation, revocation, or moved-tag evaluation.

### 2.2 Closed `go-v1` toolchain and process graph

A conforming manager MUST support an operator-trusted Go 1.23 release family
that it has tested against the `go-v1` conformance vectors. It MAY allowlist
additional Go release families only after testing those families against the
same vectors. It MUST reject a pre-1.23, malformed, package-selected, or
otherwise unknown release, including a newer release merely ordered after a
known one. The launcher is selected independently of the package and MUST be
the regular executable `<GOROOT>/bin/go` (`bin/go.exe` on Windows), never a
wrapper, repository file, runtime command, project shim, manifest value, or
executable found through the user `PATH`.

Before entering a package-controlled directory, the manager creates an
operation-private probe root. From a manager-owned empty working directory it
invokes the resolved `go` executable directly with exactly these first three
argument vectors, once per operation:

```text
[<resolved-go>, "telemetry", "off"]
[<resolved-go>, "version"]
[<resolved-go>, "env", "-json", "GOROOT", "GOHOSTOS", "GOHOSTARCH", "GOOS", "GOARCH", "GO386", "GOAMD64", "GOARM", "GOARM64", "GOMIPS", "GOMIPS64", "GOPPC64", "GORISCV64", "GOWASM", "GOTELEMETRY", "GOTELEMETRYDIR"]
```

The manager requires the probed `GOROOT` to equal the independently resolved
root, normalizes the bounded single-line `go version` output, verifies the
native host and target values, and only then fingerprints the selected tree
using `curator-go-toolchain-v1` as defined by Protocol Core section 8.2.

For each active build command, in provider-first closure order and then
Unicode-scalar lexical command order within a node, it invokes exactly these
two further argument vectors from the command's canonical `source_dir`:

```text
[<resolved-go>, "list", "-mod=vendor", "-deps", "-json", "-buildvcs=false", "-compiler=gc", "-pgo=off", "."]
[<resolved-go>, "build", "-mod=vendor", "-trimpath", "-buildvcs=false", "-buildmode=exe", "-compiler=gc", "-pgo=off", "-ldflags=-linkmode=internal -libgcc=none", "-o", <manager-staging-artifact>, "."]
```

These are the only five Go argument-vector forms. The `-ldflags=...` value and
manager-derived staging path are each one argument. The manager MUST NOT use a
shell, command string, response file, package pattern, package-selected
executable, environment value, argument, tag, flag, output path, build recipe,
hook, plugin, generator, overlay, post-build action, or fallback command. The
first three probes read no package byte and run directly from the parent; the
`go list` and `go build` vectors run inside the worker session of section 2.2.1.

The bootstrap environment starts empty except for indispensable operating
system process variables. It sets `GOENV=off`, `GOTOOLCHAIN=local`, `LC_ALL=C`,
and `LANG=C`, contains no inherited `GOROOT` or target, and sets `GOPATH`,
`GOMODCACHE`, `GOCACHE`, `GOTMPDIR`, `HOME`, `XDG_CONFIG_HOME`, `PATH`, and
`TMPDIR` below operation-private roots. `PATH` names a manager-owned empty
directory. On Windows, `APPDATA`, `LOCALAPPDATA`, `USERPROFILE`, `TEMP`, and
`TMP` are also private. The manager MUST verify that `GOTELEMETRY` is `off` and
that `GOTELEMETRYDIR` is below the private platform configuration root.

After the probes, the build environment retains those settings and adds only
the resolved `GOROOT`, native `GOOS` and `GOARCH`, exactly one applicable
trusted architecture-tuning variable returned by the fixed probe, and:

```text
GO111MODULE=on
GOFLAGS=
GOPROXY=off
GOSUMDB=off
GOPRIVATE=
GONOPROXY=none
GONOSUMDB=none
GOVCS=*:off
GOWORK=off
CGO_ENABLED=0
GO_EXTLINK_ENABLED=0
GOEXPERIMENT=
```

No ambient or package Go, compiler, assembler, linker, telemetry,
executable-search, workspace, proxy, authentication, or tuning variable may be
inherited. `GOOS` and `GOARCH` MUST equal the probed host target; cross-builds
are unsupported. Module resolution is vendor-only and networkless. The manager
MUST NOT download, tidy, vendor, generate, test, run, install, or fetch a
package, use a workspace or PGO profile, or switch toolchains. A `go.mod`
version or `toolchain` directive that would require switching to an untrusted or
unallowlisted toolchain is rejected rather than downloaded or approximated.

Below the worker, the manager and the worker start no program other than the
fingerprinted `go` executable, which in turn runs fingerprinted regular
executables below `GOROOT/pkg/tool/<GOHOSTOS>_<GOHOSTARCH>/`. The manager never
selects a shell, VCS client, network helper, external compiler, assembler,
linker, libgcc helper, or any other process for this graph, and package data
cannot add one. This is manager selection plus identity verification, not a
kernel executable allowlist; section 2.2.1 states the boundary exactly. Neither
the manager nor the worker writes to the frozen source snapshot or to `GOROOT`,
and the manager re-verifies both identities after the last child exits.

#### 2.2.1 Portable `manager-worker-v1` worker session

The manager implements exactly the portable execution policy of Protocol Core
section 4.2.1 and applies it identically to `go-v1` and `go-repository-v1` on
macOS and Windows. The worker is one hidden-mode re-execution of the installed
manager executable. In order, one operation:

1. runs the package-independent toolchain probes and freezes the validated
   source snapshot;
2. probes, once for this operation, which controls of the
   `rc5-native-control-inventory-v1` inventory this platform provides, without
   substituting a host label, a cached result, or configuration for the probe;
3. resolves its own executable to a canonical regular installed file, rejects
   symlink, reparse-point, and hard-link substitution, records strong file
   identity, and hashes the bytes;
4. opens anonymous inherited pipes or handles and launches exactly that
   executable in the fixed hidden worker mode, rechecking identity at the launch
   boundary so a replacement race cannot widen the graph;
5. sends one length-bounded canonical request carrying a fresh session nonce,
   the expected executable identity, the canonical working directory, the fixed
   environment, the private roots, the applicable limits, the exact Go and tool
   identities, and both permitted Go argument vectors;
6. requires the worker to prove the same executable identity and hash, release
   unrelated descriptors or handles, close standard input, apply every mandatory
   control and exactly the inventory controls the probe found available, and
   acknowledge the nonce together with its `capability-evidence-v1` record;
7. runs exactly the fixed `go list` vector and returns bounded output and exit
   metadata; the worker cannot proceed to a build on its own;
8. applies every dependency, containment, directive, and native-input rejection
   of section 2.3 to the complete stream;
9. sends exactly one authenticated fixed build permit and runs exactly the fixed
   `go build` vector, tearing the session down on any other message;
10. returns one bounded regular artifact through manager-controlled private
    staging, applies manager-defined permissions, and verifies type, size, link
    safety, identity, and digest without executing it; and
11. re-verifies the worker, source-snapshot, and fingerprinted toolchain
    identities, then terminates and joins the complete worker domain and
    discards all private state before publication.

A failure at any step fails the operation before publication. A pre-launch
identity, substitution, or mandatory-control failure fails before the worker
starts. A protocol, nonce, ordering, message-size, message-kind, or
permit-ordering failure fails before the compiler starts.

The rc.5 native-control inventory of Protocol Core section 4.2.1 is exhaustive
and normative per platform. The manager applies exactly the controls it marks
available for the host platform and MUST NOT apply, name, or report a control
outside it:

| Control | macOS | Windows |
|---|---|---|
| `descendant-domain-termination` | available: process group and session teardown | available: Job Object kill-on-close |
| `active-process-count-limit` | unavailable: `no-private-aggregate-domain` | available: Job Object active-process limit |
| `aggregate-memory-limit` | unavailable: `no-private-aggregate-domain` | available: Job Object process and job memory limit |
| `per-file-size-limit` | available: `RLIMIT_FSIZE` | unavailable: `no-private-aggregate-domain` |
| `inherited-handle-restriction` | available: close-on-exec plus explicit descriptor release | available: explicit handle inheritance list |

The manager emits exactly one `capability-evidence-v1` record per operation,
containing exactly `record_version`, `execution_policy`, `platform`, and
`controls`, with exactly one `{name, availability, status, probed_at}` entry per
inventory control. `availability` is `available` or `unavailable`, `status` is
`applied` or `unavailable`, and `probed_at` is `pre-worker-launch`. An
`available` entry MUST report `applied` and an `unavailable` entry MUST report
`unavailable`; a missing, duplicated, extra, or unknown entry, an unknown
`record_version`, and an availability value that was not probed for this
operation are all `build_execution_capability_evidence_invalid`. A deferred
hardened guarantee named as an entry, or a record `execution_policy` other than
`manager-worker-v1`, is `build_execution_hardened_claim_forbidden`.

That record is result-only. The manager reports it in install, dry-run plan, and
status results and MUST NOT place it into a cache key, receipt input, marker
record, or conformance claim.

The failure boundary is exact. A mandatory portable control that cannot be
applied rejects with `build_execution_control_unavailable` before the worker
starts. An unavailable inventory control, and the absence of any of the six
hardened guarantees deferred to `STORY-260728-327soo`, never reject a portable
build, never produce a diagnostic, and never block publication.

Execution-boundary results use these stable `phase: execution` diagnostics,
which apply to both compiled drivers and take precedence over the generic
schema and descriptor codes of section 11.10:

| Code | State | Severity | Meaning |
|---|---|---|---|
| `build_execution_worker_identity_invalid` | `blocked` | `error` | Worker or toolchain executable identity, substitution, or replacement check fails |
| `build_execution_worker_protocol_invalid` | `blocked` | `error` | Session framing, nonce, ordering, size, message kind, or permit sequence is invalid |
| `build_execution_control_unavailable` | `unsupported` | `error` | A mandatory portable control cannot be applied on this host |
| `build_execution_capability_evidence_invalid` | `corrupt` | `error` | Capability evidence reports an unavailable control as applied or contradicts the applied profile |
| `build_execution_hardened_claim_forbidden` | `unsupported` | `error` | A deferred hardened guarantee is claimed under the portable execution policy |
| `build_execution_package_influence_forbidden` | `unsupported` | `error` | Package data attempts to influence the execution boundary, worker, controls, limits, permits, or publication |

### 2.3 Dependency and artifact preflight

On a real cache miss, the fixed `go list` command MUST complete before the
corresponding `go build`. Its complete JSON stream MUST cover the root and every
active dependency, contain exactly one non-`DepOnly` root named `main`, contain
no incomplete result, `Error`, or `DepsErrors`, and select no tests.

Only a result with both `Standard == true` and `Goroot == true` is a trusted
toolchain package. Its directory and every listed source, module, and embedded
input MUST remain below the fingerprinted `GOROOT`, with the single vendored
exception that `GOROOT/src/vendor` packages reporting `ImportPath` prefix
`vendor/` and `Root == ""` are accepted as trusted when `Standard == true &&
Goroot == true` and the directory is below `GOROOT` (Go 1.25 toolchain quirk).
Every other result, its package directory, module file, active Go file, and
every active embedded input MUST be a regular file below the command's build
root. Escaped, missing, linked, special, or out-of-root input is rejected.

Every result MUST have empty `SysoFiles`. Every non-standard result MUST also
have empty `CgoFiles`, `CFiles`, `CXXFiles`, `MFiles`, `HFiles`, `FFiles`,
`SwigFiles`, and `SwigCXXFiles`, with the narrow vendored assembly exception
that pure Go `SFiles` are allowed only for vendored non-standard packages that
have no `CgoFiles`/`CFiles`/`CXXFiles`/`MFiles`/`HFiles`/`FFiles`/`SwigFiles`/
`SwigCXXFiles`/host objects, where every `SFiles` entry is a regular file
below the build root and the package is already hashed via
`curator-build-source-v1` (e.g. `coder/websocket` masks). Thus cgo, package-
controlled C, C++, Objective-C, Fortran, host objects, and SWIG remain rejected
throughout the active dependency graph, while audited pure Go assembly in
vendored deps is permitted. Each active non-standard `GoFiles` file is scanned
as exact bytes and rejected if it contains `//go:cgo_import_dynamic`, except
for the audited allowlist `golang.org/x/sys` and `golang.org/x/sys/*`
(`zsyscall` trampolines). Both exceptions are scoped to results whose module
carries no replacement. A result whose module carries a replacement, and every
active input below the declared module directory that replacement names, is
held to the unexceptioned rules: `SFiles` MUST be empty and
`//go:cgo_import_dynamic` MUST NOT appear. The exceptions cover widely audited
third-party dependencies a package cannot reasonably fork, and first-party code
in the package's own repository can simply not use those constructs.
`//go:generate` in `GoFiles` is inert — managers MUST NOT run generators and
`go build -mod=vendor` does not execute them; its presence in vendored
`GoFiles` (vendor already materialized) does not fail preflight. Any other
violation fails before `go build`.

A schema-8 build command that declares `modules` is validated in a fixed order,
and every step of it completes before `go build`.

1. Before the fixed `go list`, the manager validates the declaration against
   the frozen snapshot alone: portable relative path, not `.`, unique, a real
   link-free directory strictly inside the snapshot, `go.mod` directly inside
   it, and pairwise disjoint from every other declared module directory, every
   declared build root, and every runtime root under both exact and
   platform-path comparison. `Module.Replace.Dir` and `Module.Replace.GoMod`
   are never evidence that a path exists.
2. The fixed `go list -mod=vendor` runs unchanged. Go reconciles `go.mod`
   against `vendor/modules.txt` itself and fails the command with its own
   inconsistent-vendoring error before any manager check of this section
   applies.
3. After `go list` returns, the manager reads the effective replace set from
   `<build root>/vendor/modules.txt` exactly as Protocol Core section 4.2.3
   defines it, reconciles selection annotations with their unversioned-left
   directive annotations, rejects every versioned replacement side and every
   module-to-module redirect, resolves each directory-form target against the
   build root, and checks the one-to-one correspondence with the declared list
   in both directions.
4. Only then does the scan surface above run with the declared directories
   included and the vendored exceptions withheld from replaced modules.

The manager MUST NOT parse `go.mod`, MUST NOT read `Module.Replace` as the
source of the effective replace set, and MUST NOT reconcile a vendor copy
against the declared directory it was made from. The vendor copy is
authoritative for the build; both copies are already bound by
`curator-build-source-v1`, so a divergence changes the build identity and is
not separately diagnosed.

Module-root results use these stable `phase: preflight` diagnostics:

| Code | State | Severity | Meaning |
|---|---|---|---|
| `build_module_root_declaration_invalid` | `unsupported` | `error` | A declared module directory is `.`, is not a portable relative path, is duplicated, is not a real link-free directory in the snapshot, or has no `go.mod` directly inside it |
| `build_module_root_containment_invalid` | `unsupported` | `error` | A declared module directory equals, contains, or is contained by another declared module directory, a declared build root, or a runtime root, under exact or platform-path comparison |
| `build_module_root_directive_form_unsupported` | `unsupported` | `error` | An effective replacement carries a version on either side, is a module-to-module redirect, or has an unreadable or unreconciled annotation shape |
| `build_module_root_directive_undeclared` | `unsupported` | `error` | An effective replacement names a directory the command does not declare |
| `build_module_root_declaration_unused` | `unsupported` | `error` | A declared module directory is named by no effective replacement |

The fixed build forces the native gc compiler, disabled PGO and cgo, and
internal linking with no libgcc. If internal linking cannot produce the output,
the command fails; there is no external-link or libgcc fallback. The only
output is the manager-derived `bin/<command>` or `bin/<command>.exe` at a
bounded regular file in operation staging. The manager hashes it and applies
manager-defined executable permissions but MUST NOT execute it for validation,
version discovery, smoke testing, post-processing, receipt generation,
rollback, repair, or recovery.

### 2.4 Build cache and dry-run planning

For each active build command, the manager derives the complete build input,
logical cache key, canonical receipt, receipt hash, and artifact-relative path
exactly as specified by Protocol Core section 9. Persistent reuse is permitted
only below an implementation-specific cache boundary resolved independently of
package input and proven manager-created, manager-owned, privately mutable,
contained, regular-file-only, and link-safe. The manager verifies that boundary
and every entry component on each lookup and again under the manager-home
mutation lock. A manager unable to prove the boundary MUST disable persistent
reuse.

A candidate hit is accepted only after the manager recomputes build-source
identity, cache key, complete expected input, exact canonical receipt bytes and
hash, manager-derived artifact path, artifact hash, and byte length without
following links or executing the artifact. Unknown fields or unsupported
receipt, driver, build-source, toolchain, or execution-policy identities are
rejected. An entry whose execution policy is absent or differs from the policy
the manager implements is a miss; the manager rebuilds instead of adopting or
upgrading it, and host capability evidence never participates in that
comparison. Internally
consistent bytes outside the protected boundary are not provenance: a real
operation rebuilds from the revalidated snapshot into new protected state,
dry-run reports `would-rebuild-untrusted-cache`, and status is non-current.

A dry run performs all read-only planning and package-independent toolchain
probes above, rechecks generation digests for every shared target it consulted,
and removes all operation-private state before returning. It reports, per build
command, `cache-hit`, `would-preflight-and-build`,
`would-rebuild-untrusted-cache`, `corrupt`, or `unsupported`. A cache miss is
`would-preflight-and-build` because dry-run MUST NOT run `go list`, `go build`,
a compiler, or a linker.

A dry run acquires no project, cache-build, or manager-home mutation lock and
performs no recovery write, quarantine, permission repair, cache publication,
GC, or other persistent mutation. It MUST leave unchanged every existing or
potential persistent source checkout; snapshot, response, toolchain memo,
module, build, runtime, or audit cache; audit, registry, revocation, and
rollback state; configuration file; lock file; journal; backup; marker; shim;
environment file; context or runtime tree; adapter ledger or mirror; consumer
ledger; and GC metadata. Network reads and removable temporary workspaces are
permitted, but no temporary probe, Go cache, or staging directory may remain
after return. Read-only inspection MUST avoid changing access times where the
platform provides a no-atime facility.

### 2.5 Private build and serialized publication

A real single-project operation holds its project operation lock from planning
through handoff. A multi-project operation acquires project locks by canonical
project identity in unsigned UTF-8 byte order. These locks serialize planning
for the affected project targets but do not authorize shared-state mutation.

After read-only planning, the manager builds every miss and verifies every
artifact and canonical receipt in operation-private staging outside the
manager-home mutation lock. Optional per-key build locks are an optimization;
the manager holds at most one at a time and releases it before acquiring the
home lock. If any preflight or build fails, it removes the operation staging.
No recovery, cache publication, quarantine, permission repair, journal
mutation, target swap, or GC may occur before every current-operation miss is
built and verified. Any preflight or build failure MUST preserve the
installation, consumers, and live caches byte-for-byte as they were when the
operation began.

After all private builds succeed, the manager acquires the exclusive
manager-home mutation lock shared by project, global, hybrid, runtime, adapter,
consumer, build-cache publication, recovery, repair, and GC paths. While
holding it, the manager:

1. recovers every incomplete journal;
2. revalidates the protected cache boundary, every candidate or competing
   winner entry, every shared generation, target owner, and expected preimage
   used by the plan after recovery;
3. releases the lock and restarts from the earliest affected read or build when
   recovery or revalidation changes closure, activation, cache trust, required
   key, target ownership, expected preimage, or any other plan assumption;
4. atomically publishes missing staged entries into protected state as complete
   immutable directories, never merging into an existing entry; and
5. creates one durable rollback journal for the whole manager-home transaction.

If another operation published the same key first, the manager validates the
winner under the lock. It discards a byte-identical staged loser; differing
bytes for one logical key are a determinism or corruption error. A corrupt live
entry may be quarantined or replaced only under this lock from an already
verified staged build. An untrusted old boundary is never made private and then
adopted; a new protected boundary receives freshly rebuilt bytes.

The one journal records a unique transaction identifier, canonical project
identity, and, for every mutable target, its canonical target identifier,
expected generation or preimage digest, backup path, desired digest, and commit
state. Targets commit in these deterministic classes:

1. project and global contexts and markers;
2. runtime, shim, and environment targets;
3. adapter ledgers and hybrid or global mirrors;
4. stale managed removals; and
5. the machine-wide consumer ledger last.

Canonical identifiers are sorted unsigned-bytewise within a class. Backups
remain until all target swaps and the consumer ledger are durable. Recording
the consumer last prevents a failed installation from advertising state that
was not installed and prevents concurrent projects from losing ledger updates.

If cache publication or any target swap fails, the manager keeps the home lock
and restores journaled targets in exact reverse commit order. Before restoring
a target it requires the current digest to equal the journal's desired digest;
a mismatch is implementation corruption and MUST NOT overwrite unknown state.
A transaction-owned cache entry may be removed during rollback only after a
fresh locked mark proves that no valid marker or journal references it.
Retaining an unreferenced immutable entry is always safe. Existing valid cache
entries are never modified during rollback, and the built artifact is never a
rollback program or verifier.

After a successful commit the manager durably removes backups and marks or
removes the journal, then runs runtime, snapshot, and compiled-artifact cache GC
while still holding the home lock. GC failure is reported as a maintenance
warning and does not roll back the successful installation. The manager
releases the manager-home lock before releasing project locks.

Lock ordering is fixed: project operation locks in canonical order, then at
most one optional cache-build lock, which is released, then the manager-home
mutation lock. A process MUST NOT acquire a project or cache-build lock while
holding the home lock. A standalone recovery or standalone GC acquires only the
home lock; recovery within install or repair occurs only after private builds,
under the home lock already ordered after the project locks. Private validation
and compilation may run concurrently for independent projects; shared
publication, commit, rollback, recovery, repair, and GC are serialized.

### 2.6 Recovery and repair

Recovery runs under the manager-home mutation lock before any new shared
mutation. Within install or repair it runs only in the serialized publication
phase after every private build succeeds; it MUST NOT run as a pre-build pass.
It scans every incomplete journal by transaction identifier rather than by the
project that triggered recovery, verifies target preimages and desired digests,
and either completes a wholly uncommitted publication or restores committed
targets in exact reverse order. Backups are retained until recovery or rollback
succeeds. No transaction releases the home lock between its first target swap
and durable success or reverse rollback, so recovery cannot overwrite a later
project's successful commit or lose a consumer-ledger update.

Repair uses the same immutable-source validation, context exclusion,
build-source digest, closure, audit and registry gates, fixed toolchain and
process graph, private build, cache publication, and journaled commit sequence
as install. It MUST rebuild a missing, corrupt, wrong-target, wrong-toolchain,
or untrusted compiled entry from a revalidated snapshot into protected state.
It MUST NOT adopt candidate bytes or authenticate them by changing permissions,
recalculating a marker, or accepting a self-consistent receipt.

An upgrade resolves and fetches only the selected scope's direct declarations
and their transitive dependency closure. An operation over several projects
MUST deduplicate fetches of the same checkout. A global-scope upgrade fetches
only the global dependency closure. A separate update operation MAY fetch all
managed source repositories.

The managed `.gitignore` comment is implementation-specific. Conformance
depends on the generated paths being ignored, not on comment spelling.

## 3. Runtime and project environment

Runtime files are machine-local and keyed by skill name and resolved commit.
Context installs under `.agents/skills/<name>/`; command shims install under
`.agents/bin/`. Runtime-only nodes receive a marker-only directory and are not
mirrored into agent adapters.

Runtime roots are copied atomically. An existing commit-keyed entry MAY be
reused only after verifying that every required path exists. Command launchers
MUST be self-contained: executing a portable direct command location MUST NOT
depend on shell activation or a user profile. A launcher prepends the project
or global command directory, directories containing resolved declared system
dependencies, and any implementation runtime directories required by the
command. It preserves the inherited `PATH`, forwards arguments without
reinterpretation, and returns the child command's exit status.

The launcher rules of this section govern every command other than an enforced
script command. A script command that declares
`execution_policy: "script-worker-v1"` is launched through the enforced worker
of section 3.1 instead: none of the launcher forms below apply to it, its
inherited `PATH` is discarded rather than preserved, and no shell, `.cmd`, or
symlink shim stands between the manager and the interpreter.

Build roots MUST NOT be copied into the commit-keyed runtime store. A build
command launcher targets the immutable compiled-artifact cache entry selected
by the effective plan and marker, never a source-tree output or the
commit-keyed script runtime entry. Before writing a launcher, the manager MUST
revalidate the referenced protected cache boundary, receipt, and artifact.

On Unix a launcher is either a relative symlink when no environment
augmentation is required or an executable POSIX-shell wrapper. On Windows it
is a `.cmd` wrapper that safely quotes the runtime path, disables delayed
expansion while constructing its environment, forwards all arguments, and
returns the child status. Stale launchers owned by the previous plan are
removed.

`.agents/env.sh` and `.agents/env.ps1` prepend `.agents/bin` to `PATH` and set
the portable `CSK_PROJECT_ROOT` to the resolved project root. They MUST locate
themselves rather than rely on the caller's current directory. A manager MAY
also set tool-specific variables.

Project command availability for agents MUST NOT depend on a user shell
profile. The portable direct command locations are
`<project>/.agents/bin/<command>` on Unix and
`<project>\.agents\bin\<command>.cmd` on Windows. A manager SHOULD warn when
prompt-visible skill instructions infer an installed command from its source
runtime path or omit shell-neutral shim resolution.

A manager SHOULD publish forwarding shims for global commands into a safe,
writable directory that is already on the user `PATH`. It MUST NOT overwrite
an unmanaged entry. When no safe directory exists, it retains the canonical
global shims and warns with the canonical location and optional shell
activation path. Forwarding locations and their ownership records are
machine-local and implementation-specific.

### 3.1 Enforced `script-worker-v1` command launch

A script command that declares `execution_policy: "script-worker-v1"` is an
enforced command. Every other script command is declared-only: every script
command of manifest schema 7 and earlier, and every schema-8 script command
that omits the field. Declared-only script commands keep the launcher rules of
section 3 unchanged, and their capability declaration remains an audit surface
that bounds nothing at run time. Build commands are outside this policy
entirely; their install-time execution boundary is section 2.2.1 and their
launcher is unchanged. The manager implements exactly the portable
`script-worker-v1` execution policy of Protocol Core section 4.1.1 and applies
it identically on macOS, Linux, and Windows.

The fixed process graph of an enforced invocation is:

```text
manager parent
  -> identity-verified manager-owned script worker
       -> identity-verified interpreter for the declared identifier
            -> manager-resolved executables named by the `exec` capability
```

The fourth node exists only when `exec` names executables; under `exec: "none"`
the graph is exactly three nodes.

The worker is one hidden-mode re-execution of the installed manager executable,
the same implementation boundary section 2.2.1 uses for builds, and not a
user-visible command surface. No package file, manifest value, environment
value, `PATH` lookup, shell, shebang line, Windows file association, or user
option selects the worker executable, its hidden mode, or the interpreter. The
shebang line and the Windows file association are inert under this policy and
MUST NOT select the executed program.

For each enforced invocation, in order, the manager:

1. resolves the command's declared closed interpreter identifier to an
   operator-trusted executable independently of package data and before
   entering any package-controlled directory, rejecting resolution from the
   repository, a runtime root, `.agents/bin`, the user `PATH`, or a manifest;
2. probes, once for this invocation and before the worker starts, which
   controls of the `script-worker-v1-native-control-inventory-v1` inventory
   this host provides, without substituting a host label, a cached result, or
   configuration for the probe;
3. resolves its own executable to a canonical regular installed file, rejects
   symlink, reparse-point, and hard-link substitution, records strong file
   identity, and hashes the bytes;
4. derives the containment profile from the declared capabilities of Protocol
   Core section 4.3, taking every absent field at its deny-by-default meaning
   under this policy, and rejects any attempt by package data to widen it;
5. launches exactly that executable in the fixed hidden script-worker mode,
   rechecking identity at the launch boundary so a replacement race cannot
   widen the graph;
6. requires the worker to prove the same executable identity and hash, verify
   the resolved interpreter file's identity, release unrelated descriptors and
   handles, bind the standard streams explicitly, apply every mandatory
   portable control and exactly the inventory controls the probe found
   available, and acknowledge a fresh session nonce together with its
   `script-capability-evidence-v1` record;
7. runs the interpreter against the command's commit-keyed runtime entry,
   forwarding arguments without reinterpretation and returning the child's
   exit status; and
8. terminates and joins the complete worker domain before the invocation
   returns.

The capability set is a top-level manifest property, so one skill has exactly
one containment profile and every enforced command in that skill runs under it.
An `exec` name or an `env_read` name that only one command needs widens the
profile of every enforced command in the same skill. This is a property of the
schema 7 capability shape, not of this policy, and this release does not
introduce per-command capabilities.

The following controls are mandatory on every supported host, and they are the
only controls whose absence rejects an invocation:

- an identity-verified manager-owned worker between the manager and the
  interpreter, launched by canonical image path and never through `sh -c`,
  `cmd.exe /c`, PowerShell, a `.cmd` or `.ps1` wrapper, `PATHEXT`, or a file
  association;
- per-invocation identity verification of the resolved interpreter executable
  file, with symlink, reparse-point, and hard-link substitution rejected and
  identity re-checked at the launch boundary;
- a manager-built environment: an empty bootstrap plus manager-set values plus
  exactly the host variables named by `env_read`;
- a manager-built `PATH` containing exactly the resolved interpreter and the
  resolved declared `exec` names, with the inherited `PATH` discarded;
- offline environment configuration plus proxy and resolver scrubbing when the
  derived `network` capability is `none`;
- a manager-selected working directory and operation-private temporary,
  configuration, and cache roots;
- explicit manager-controlled standard-stream binding and release of unrelated
  descriptors and handles before the interpreter starts;
- application of exactly the native controls the inventory below marks
  available for the host platform, and of every `host-conditional` control the
  same probe found present;
- exactly one closed `script-capability-evidence-v1` record per invocation; and
- termination and joining of the complete worker domain before the invocation
  returns.

Protocol Core section 4.1.1 states the criterion for manager ownership of an
environment name and leaves the reserved set to this profile. A name in that
set is manager-owned: an `env_read` entry naming one MUST NOT pass the
inherited value through, and the manager-set value stands. The enumeration
below is the reserved minimum: every conforming manager MUST reserve every name
it lists, and a manager MUST reject an interpreter identifier it has no
reserved set for rather than launch with an unfiltered environment.

Reserved on every platform and for every interpreter identifier: `PATH`,
`HOME`, `TMPDIR`, `TEMP`, `TMP`, `XDG_CONFIG_HOME`, `XDG_CACHE_HOME`,
`XDG_DATA_HOME`, `XDG_STATE_HOME`, `HTTP_PROXY`, `HTTPS_PROXY`, `ALL_PROXY`,
`FTP_PROXY`, `NO_PROXY`, every lowercase spelling of those five proxy names,
`RES_OPTIONS`, `HOSTALIASES`, `LOCALDOMAIN`, every `LD_`-prefixed name,
including `LD_PRELOAD`, `LD_LIBRARY_PATH`, and `LD_AUDIT`, `IFS`, and
`CSK_PROJECT_ROOT`.

Reserved additionally on macOS: every `DYLD_`-prefixed name, including
`DYLD_INSERT_LIBRARIES`, `DYLD_LIBRARY_PATH`, and `DYLD_FRAMEWORK_PATH`.

Reserved additionally on Windows: `USERPROFILE`, `APPDATA`, `LOCALAPPDATA`,
`PATHEXT`, `COMSPEC`, `WINDIR`, and `SYSTEMROOT`. Windows environment-variable
names are matched case-insensitively, so `SystemRoot` and `windir` are the same
reserved names as `SYSTEMROOT` and `WINDIR`; on macOS and Linux the match is
exact, which is why the lowercase proxy spellings are listed separately above.

Reserved additionally for `python3-v1`: every `PYTHON`-prefixed name, including
`PYTHONPATH`, `PYTHONHOME`, `PYTHONSTARTUP`, `PYTHONEXECUTABLE`,
`PYTHONUSERBASE`, `PYTHONNOUSERSITE`, and `PYTHONSAFEPATH`, and
`__PYVENV_LAUNCHER__`.

Reserved additionally for `node-v1`: every `NODE_`- and `NPM_CONFIG_`-prefixed
name, including `NODE_PATH`, `NODE_OPTIONS`, and `NODE_EXTRA_CA_CERTS`.

Each reserved name selects a program, a library or module search path, an
interpreter startup file or option, a temporary or configuration root, or proxy
or resolver configuration, which is exactly the class Protocol Core section
4.1.1 places under manager ownership. The loader, interpreter, and package
manager families are reserved by prefix rather than by name so that a variable
added by a future release of an admitted interpreter is covered without a
revision of this profile.

The criterion of section 4.1.1 continues to govern the names this enumeration
does not reach. A name that meets it is manager-owned even when it is not
listed here: a manager MUST reserve that name, and MUST NOT treat an `env_read`
entry as licensed to pass an inherited value through merely because the name is
unlisted. Such an omission is a defect of this profile, corrected by revising
the enumeration rather than left to per-manager judgement, and it is visible
because a manager MUST report every `env_read` entry it withholds, on this
basis or any other, rather than drop it silently.

A manager that cannot apply all of them MUST reject the invocation with
`script_execution_control_unavailable` before starting the worker or the
interpreter. The manager MUST also preflight the same mandatory set when it
publishes or refreshes the launcher of an enforced command, and MUST reject the
install or update with the same diagnostic rather than publish a shim that can
never run. An install-time preflight is an additional moment, not a substitute:
it MUST NOT stand in for the per-invocation probe.

Three mandatory build controls of section 2.2.1 are deliberately not carried
across, and a manager MUST NOT apply them here. Standard input is not closed: a
script command legitimately reads a pipe or a terminal, and closing it would
break the transparent forwarding this profile requires elsewhere. Instead the
manager binds standard input explicitly, to the intended stream or to the
platform null device, and never to an inherited descriptor it did not intend.
The build's bounded combined output and bounded wall-clock deadline likewise do
not bound an enforced invocation: the output bound applies only to output the
manager itself captures, direct pass-through streaming is permitted when the
manager binds the streams, and any deadline is an operator or invocation-level
bound rather than a control of this policy. Descriptor and handle hygiene is
retained in full.

The `script-worker-v1-native-control-inventory-v1` inventory is exhaustive and
normative per platform. The manager applies exactly the controls it marks
available for the host platform, applies a `host-conditional` control exactly
when the per-invocation probe finds it present, and MUST NOT apply, name, or
report a control outside the inventory:

| Control | macOS | Linux | Windows |
|---|---|---|---|
| `descendant-domain-termination` | available: process group and session teardown | available: process group and session teardown | available: Job Object kill-on-close |
| `active-process-count-limit` | unavailable: `no-private-aggregate-domain` | host-conditional: delegated cgroup v2 `pids.max` | available: Job Object active-process limit |
| `aggregate-memory-limit` | unavailable: `no-private-aggregate-domain` | host-conditional: delegated cgroup v2 `memory.max` | available: Job Object process and job memory limit |
| `per-file-size-limit` | available: `RLIMIT_FSIZE` | available: `RLIMIT_FSIZE` | unavailable: `no-private-aggregate-domain` |
| `inherited-handle-restriction` | available: close-on-exec plus explicit descriptor release | available: close-on-exec plus explicit descriptor release | available: explicit handle inheritance list |
| `descendant-exec-denial` | unavailable: `no-unprivileged-per-process-exec-policy` | host-conditional: Landlock execute right | unavailable: `child-process-policy-requires-appcontainer` |
| `filesystem-write-confinement` | unavailable: `no-unprivileged-filesystem-domain` | host-conditional: Landlock write rights | unavailable: `no-unprivileged-filesystem-domain` |
| `network-isolation-domain` | unavailable: `no-unprivileged-network-domain` | host-conditional: network namespace without interfaces | unavailable: `no-unprivileged-network-domain` |

This inventory is independent of `rc5-native-control-inventory-v1`. Its first
five rows carry the same macOS and Windows verdicts because the underlying host
facts do not depend on what the child is, but the two inventories are versioned
separately and a revision of one MUST NOT re-scope the other.

`availability` is `available`, `host-conditional`, or `unavailable`. An
`available` control MUST be applied and reported `applied`. An `unavailable`
control MUST be neither applied nor reported applied. A `host-conditional`
control is applied and reported `applied` exactly when this invocation's probe
found it present, and reported `unavailable` otherwise; a `host-conditional`
control the probe did not find never rejects the invocation and never produces
a diagnostic. Adding, removing, or re-scoping an entry requires a new inventory
version.

The unavailable-reason vocabulary of this inventory is closed and is exactly
`no-private-aggregate-domain`, `no-unprivileged-per-process-exec-policy`,
`child-process-policy-requires-appcontainer`,
`no-unprivileged-filesystem-domain`, and `no-unprivileged-network-domain`.

The manager emits exactly one `script-capability-evidence-v1` record per
enforced invocation, containing exactly `record_version`, `execution_policy`,
`platform`, and `controls`, with exactly one
`{name, availability, status, probed_at}` entry per inventory control.
`platform` is `macos`, `linux`, or `windows`, and `probed_at` is
`pre-worker-launch`. A missing, duplicated, extra, or unknown entry, an unknown
`record_version`, an availability value that was not probed for this
invocation, a status inconsistent with the availability rules above, and a
control name outside this inventory are all
`script_execution_capability_evidence_invalid`. A guarantee deferred by this
policy or by section 2.2.1 named as an entry, and a record `execution_policy`
other than `script-worker-v1`, are
`script_execution_hardened_claim_forbidden`. The
manager MUST NOT emit a `capability-evidence-v1` record for an enforced script
invocation, and MUST NOT emit a `script-capability-evidence-v1` record for a
build operation.

Availability is probed once, before the worker launches, and is never re-probed
while the command runs, however long it runs; the worker returns the resulting
record when it acknowledges the session nonce. Controls are installed before the
interpreter starts and cannot change afterwards, so a second record for one
invocation would be a contradiction rather than information. A cached,
inherited, or configured result is not a probe, and an install-generation record
replayed at invocation time is a cached result.

That record is result-only. The manager exposes it through dry-run plan and
status results and through an operator-selected diagnostic destination that
package data can never choose. It MUST NOT be written to the command's standard
output or standard error, because those streams belong to the caller's
pipeline, and it MUST NOT enter a cache key, receipt input, marker record, or
conformance claim. Retention is machine-local and operator-configurable, and a
manager SHOULD retain at most the most recent record per command by default.

Every rule this policy enforces is a manager mechanism. The policy does not
provide, and a conforming manager MUST NOT claim, the seven guarantees deferred
by Protocol Core section 4.1.1: `script-total-network-denial`;
`script-network-host-allowlisting`; `script-exact-executable-allowlisting`;
`script-private-runtime-area-only-writes`; `script-read-only-runtime-tree`;
`script-hard-aggregate-descendant-resource-bounds`; and
`script-fail-closed-capability-preflight`. None of the seven names may appear in
the mandatory-control set, the native-control inventory, or a capability
evidence record, and none of them aliases a guarantee deferred by section 2.2.1
or a control of either inventory. Applying a `host-conditional` control on a host
that provides it narrows what that invocation can do; it does not license
claiming the corresponding policy-level guarantee, which stays deferred
precisely because the mechanism varies between hosts.

On macOS and Windows the first release applies no kernel confinement primitive
at all: `exec` and `filesystem` narrowing there are `PATH`, environment,
private-root, and working-directory mechanisms, and a descendant that resolves
an absolute path or opens a path outside the private roots is not stopped by
this policy. On Linux the three `host-conditional` controls narrow the
descendant domain on hosts that provide them, and are absent on hosts that do
not. A manager MUST report which of those it applied for this invocation and
MUST NOT describe any of them as a guarantee of the policy.

There is exactly one failure boundary. A mandatory portable control that cannot
be applied rejects with `script_execution_control_unavailable` before the
worker starts and runs nothing. An unavailable inventory control, a
`host-conditional` control the probe did not find, and the absence of any
deferred guarantee never reject an enforced invocation, never produce a
diagnostic, and never prevent the command from running; a conforming manager
MUST NOT record any of them as applied.

Package-controlled bytes remain command input only. They MUST NOT select or
modify the manager or worker executable, its hidden mode, or its identity; the
interpreter identifier's resolution, path, or identity; the applied controls,
limits, permitted roots, environment values, `PATH` entries, or working
directory; the worker protocol messages; the capability-evidence record or its
destination; or the audit record. The one package-visible field of this policy
is `execution_policy` itself, whose single admissible value can only narrow
what the command may do and can never widen it.

Enforcement is a breaking change for the skills that opt in. Before this
policy, a script inherited the caller's whole environment and `PATH`; under it,
an undeclared variable is absent and an undeclared executable is not on `PATH`.
A manager SHOULD report that difference when an enforced command is first
installed or updated, rather than leaving it to the first invocation.

Enforced-invocation results use these stable `phase: execution` diagnostics. The
first four are the policy-level set of Protocol Core section 4.1.1. The last
three are worker-session codes of this profile: they mirror the equivalent
`build_execution_*` codes of section 2.2.1 and name the session failure modes an
identity-verified worker has and a policy-level description does not enumerate.

| Code | State | Severity | Meaning |
|---|---|---|---|
| `script_execution_control_unavailable` | `unsupported` | `error` | A mandatory portable control cannot be applied on this host, at shim install or update and again before worker launch |
| `script_execution_capability_evidence_invalid` | `corrupt` | `error` | Script capability evidence contradicts the probe, is incomplete, or names a control outside the script inventory |
| `script_execution_hardened_claim_forbidden` | `unsupported` | `error` | An evidence record names a guarantee deferred by this policy or by section 2.2.1, or a foreign execution policy |
| `script_execution_policy_unsupported` | `unsupported` | `error` | A manager that does not implement `script-worker-v1` reads a command that selects it |
| `script_execution_worker_identity_invalid` | `blocked` | `error` | Worker or interpreter executable identity, substitution, or replacement check fails |
| `script_execution_worker_protocol_invalid` | `blocked` | `error` | Session framing, nonce, ordering, size, or message kind is invalid |
| `script_execution_package_influence_forbidden` | `unsupported` | `error` | Package data attempts to influence the worker, interpreter resolution, controls, limits, environment, roots, or evidence |

A manager that does not implement this policy MUST reject such a command with
`script_execution_policy_unsupported`. It MUST NOT install the command
declared-only, downgrade it, or ignore the field, because the resulting shim
would run package code the manifest says is contained.

## 4. Install scopes

### 4.1 Project

Project declarations materialize inside the project checkout. Different
projects may install different commits independently.

### 4.2 Global

Global declarations use a machine-local `Skillfile.json`, context store,
runtime shims, and environment files. Their physical location is
implementation-specific. Global adapters mirror to each selected agent's home
discovery directory. Global command precedence is below project shims and above
the pre-activation system `PATH`.

### 4.3 Hybrid

A hybrid manifest extends Skillfile schema 1 with REQUIRED per-skill `targets`.
A target is a project alias, exact resolved path, or path glob. The glob syntax
is `/`-separated and implements only `*`, `?`, and `**`; matching is
case-sensitive and does not depend on the host OS.

Applicable hybrid declarations join the project closure. Precedence is
project, hybrid, global. Project declarations shadow hybrid declarations with a
message. Hybrid-only closure nodes render once in a machine store with the
machine locale and are mirrored into targeted project adapters without writing
skill context into the project checkout.

## 5. Agent adapters

The standard agent identifiers and discovery surfaces are:

| Agent | Project surface | Global surface |
|---|---|---|
| `claude_code` | `.claude/skills` | `~/.claude/skills` |
| `codex_cli` | `.codex/skills` | `~/.codex/skills` |
| `cursor` | `.cursor/rules` | `~/.cursor/rules` |
| `gemini` | `.gemini/skills` | `~/.gemini/skills` |
| `opencode` | native `.agents/skills` | `~/.agents/skills` |
| `windsurf` | native `.agents/skills` | `~/.agents/skills` |

Adapter entries are symlinks, copies, or automatic symlink-with-copy-fallback.
Every root carries the adapter ledger from Protocol Core section 11. Refresh is
atomic per entry. Unknown agent identifiers produce a warning and no output.

## 6. MCP requirements

MCP verification is read-only. A manager MUST NOT launch, install, enable, or
modify an MCP server while checking a skill requirement.

| Agent | Project surface | User surface |
|---|---|---|
| `claude_code` | `.mcp.json` | `~/.claude.json` |
| `cursor` | `.cursor/mcp.json` | `~/.cursor/mcp.json` |
| `codex_cli` | `.codex/config.toml` | `~/.codex/config.toml` |
| `gemini` | `.gemini/settings.json` | `~/.gemini/settings.json` |
| `opencode` | `opencode.json`, `opencode.jsonc` | `~/.config/opencode/opencode.json(c)` |
| `windsurf` | none | `~/.codeium/windsurf/mcp_config.json` |

JSON surfaces use `mcpServers`, Codex TOML uses `mcp_servers`, and OpenCode uses
`mcp`. A disabled entry does not count. Missing or malformed files configure no
servers and produce a warning naming the file.

`required_in: any` succeeds when at least one target agent configures the
server. `all` requires every target. Failures name missing agents and include
the declared hint. Markers record sorted agents where each server was found.

A configured stdio server whose command is absent from `PATH` produces a
warning when every discovered entry is positively stdio. Project-only server
configuration produces a checkout-trust warning.

## 7. Source audit policy

Source audit is a machine-local policy layer. Detectors and analysis backends
may differ; decisions do not.

For each snapshot a manager computes the raw-tree content hash, runs a static
canary, runs deterministic detectors, optionally invokes a configured backend,
and records findings. A canary failure always blocks. Cloud backends receive
only sources classified public by explicit policy; other egress attempts block.
Secret redaction is REQUIRED before permitted cloud egress.

Decisions are `allow`, `warn`, `block`, or `require_pin`:

- local hash or source revocation always blocks;
- strict mode requires an operator pin for pre-capability schemas;
- a verifiable finding at or above `fail_on` blocks in strict mode;
- advisory mode warns;
- backend failure blocks in strict mode and warns in advisory mode;
- `block` and `require_pin` fail installation.

Pins record content hash, operator identity, reason, and creation time. Pins do
not override local or registry revocation.

Script execution policy is part of the audit record. For every script command
the record carries the effective execution-policy identity or its absence, so a
reviewer can separate enforced commands from declared-only ones and a registry
can gate on the distinction. Two warning classes are REQUIRED, are always `warn`
in every mode, and never block:

- `script-command-declared-only` — a script command that does not declare
  `execution_policy: "script-worker-v1"`. Its capability declaration is
  documentation and bounds nothing at run time. Every script command of manifest
  schema 7 and earlier carries this class.
- `script-command-unfiltered-declared-network` — an enforced script command
  whose `network` capability is a host-glob list. The declared globs are
  recorded and reported; no portable filtering is applied and none is claimed.
  The command is admitted so that the `exec`, `filesystem`, and environment
  controls it can have are still applied.

Neither class is a finding about source content, neither is subject to
`fail_on`, and neither may be reported as an applied control.

## 8. Shell activation

Shell activation is an OPTIONAL convenience for interactive users. A manager
MUST NOT require it for agent command execution and MUST NOT modify a user
shell profile without explicit user action.

Shell activation searches upward from the current directory for the nearest
`.agents/env.sh` or `.agents/env.ps1`. Entering or switching projects restores
the saved pre-project `PATH` before sourcing the new environment. Leaving all
projects restores that `PATH` and clears activation state. Nested projects use
the nearest environment.

Upward search MUST guarantee progress toward a filesystem root. A POSIX hook
MUST treat an empty or non-absolute `PWD` as no active project. Before sourcing
a newly selected project environment, a hook MUST establish an activation or
loading guard so directory changes performed by that environment cannot
re-enter the same activation. A POSIX hook running under Git Bash MUST accept
native Windows drive paths for manager configuration and global-environment
lookup.

Bash integrates through `PROMPT_COMMAND`; zsh integrates through `precmd` and
`chpwd`; PowerShell wraps the existing global `prompt` function while
preserving its output and invokes activation before each prompt. Hook
installation also invokes activation once immediately. Re-loading a hook MUST
NOT stack duplicate wrappers or PATH entries.

A CLI MAY cache generated hook code and print the shell-specific command that
sources it. This avoids starting the manager executable from every new shell.

Global activation is OPTIONAL and enabled by default by conforming CLI
profiles. It is sourced once per global environment version and has lower PATH
precedence than a project environment.

## 9. Idempotent machine bootstrap

A manager SHOULD expose a non-interactive operation that creates its machine
configuration only when it is absent. When the configuration path already
exists, this operation MUST succeed without parsing, rewriting, or changing
the existing file. An explicit overwrite operation and create-if-absent mode
MUST be mutually exclusive.

## 10. Status and garbage collection

Read-only status validates marker schema, recomputes content hashes, reports
manifest and activation drift, and MAY re-resolve registry attestations. A
check mode returns non-zero for drift without mutating state.

For a build-enabled marker, status additionally validates static build-root
context exclusion, recomputes `curator-build-source-v1` from the fully validated
raw snapshot, recomputes every build input and logical key, and validates every
referenced receipt and artifact below a currently verified manager-protected
boundary. A missing raw snapshot, context-visible or runtime-copied build-root
file, untrusted boundary, unsupported driver or toolchain, corrupt receipt or
artifact, wrong native target, or source, key, receipt, artifact-path, or
artifact-hash mismatch makes the installation non-current. Status MUST NOT
execute an artifact or mutate, quarantine, repair, or adopt cache state.

Garbage collection acquires only the manager-home mutation lock and first
revalidates every manager-protected cache boundary it will traverse. It scans
registered consumers and valid markers, marks every runtime and snapshot entry
referenced by an existing consumer, any supported valid marker (currently
marker v1 or marker v2), or an in-flight transaction journal, and marks every
logical build-cache entry referenced by a valid marker v2 or in-flight
transaction journal. It sweeps only unreferenced machine-local entries older
than the manager's documented grace period. Receipt content alone is neither
provenance nor a live reference. An unreadable consumer, marker, journal, or
unprovable cache boundary fails safe: uncertain entries are retained or
conservatively quarantined according to documented local policy and the
uncertainty is reported. GC MUST NOT execute or adopt entry content.

## 11. External repository manager profile

This section is the rc.5 manager profile for schema-7
`go-repository-v1` commands. It extends, but does not reinterpret, sections 1
through 10. Schemas 1 through 6, `go-v1`, build receipt schema 1, marker
schemas 1 and 2, and their cache, toolchain, transaction, status, repair, and
GC behavior retain their rc.4 meaning.

Schema 8 admits the same `go-repository-v1` commands, and a schema-8
installation records marker v4 rather than marker v3 (`protocol/core.md`
section 10). Marker v4 carries marker-v3 meaning unchanged, so every marker-v3
obligation of this profile — shim selection, read-only status validation, and
GC rooting — applies to a valid marker v4 without change.

An rc.5 manager MUST implement the lifecycle below as a closed state machine:

```text
declared
  -> source-resolved
  -> object-proved
  -> snapshot-frozen
  -> independently-audited
  -> cache-planned
  -> privately-built
  -> publication-locked
  -> published-current
```

The manager may reuse a valid protected artifact at `cache-planned` and skip
`privately-built`, but it MUST NOT skip an earlier state. A failure before
`publication-locked` removes operation-private state and leaves live
installation state unchanged. A failure after the first shared-state write is
rolled back under the manager-home mutation lock. A dry run stops after
read-only cache planning. Read-only status starts from installed state and
never advances or repairs this machine.

### 11.1 Trusted Git distribution, child graph, and clean environment

Before reading a package or substitution path, the manager resolves an
operator-trusted Git release family tested against the rc.5 vectors. The
absolute `git` executable, complete `GIT_EXEC_PATH`, HTTPS helper, manager
credential broker, binary SSH wrapper, and OpenSSH executable are selected
only by the manager or operator policy and are fingerprinted or pinned. An
unknown release family is unsupported even when it appears newer.

The permitted child graph is limited to:

- manager to the absolute trusted `git` for private `init`, one exact
  `fetch`, and one read-only `cat-file --batch` session;
- `git fetch` to regular executables below the fingerprinted
  `GIT_EXEC_PATH` needed by that fixed fetch;
- HTTPS to the fingerprinted `git-remote-https` and OPTIONAL manager
  credential broker; and
- SSH to the manager binary wrapper and exactly one operator-trusted OpenSSH
  child.

No shell, arbitrary remote helper, source-repository upload-pack,
maintenance, submodule, hook, filter, LFS, pager, editor, proxy command,
package program, compiler helper outside the already accepted `go-v1` graph,
or produced artifact may start.

Every Git child uses a manager-owned empty working directory and an environment
constructed from empty. It contains only indispensable operating-system
process variables, private home/config/temp paths, normalized locale, and:

```text
GIT_CONFIG_GLOBAL=<manager-owned-zero-byte-file>
GIT_CONFIG_SYSTEM=<manager-owned-zero-byte-file>
GIT_CONFIG_NOSYSTEM=1
GIT_NO_REPLACE_OBJECTS=1
GIT_NO_LAZY_FETCH=1
GIT_OPTIONAL_LOCKS=0
GIT_TERMINAL_PROMPT=0
GIT_PAGER=cat
GIT_PROTOCOL_FROM_USER=0
GIT_LITERAL_PATHSPECS=1
GIT_ATTR_NOSYSTEM=1
GIT_EXEC_PATH=<fingerprinted-git-exec-path>
HOME=<operation-private>/home
XDG_CONFIG_HOME=<operation-private>/config
LC_ALL=C
LANG=C
PATH=<manager-owned-empty-directory>
```

The manager unsets `GIT_DIR`, `GIT_WORK_TREE`, `GIT_COMMON_DIR`,
`GIT_OBJECT_DIRECTORY`, `GIT_ALTERNATE_OBJECT_DIRECTORIES`, `GIT_NAMESPACE`,
`GIT_REPLACE_REF_BASE`, `GIT_ATTR_SOURCE`, `GIT_SSL_NO_VERIFY`,
`GIT_SSH_COMMAND`, `SSH_AUTH_SOCK`, `SSH_ASKPASS`, `GIT_CONFIG_COUNT`, every
`GIT_CONFIG_KEY_*` and `GIT_CONFIG_VALUE_*`, trace variables, HTTP/HTTPS proxy
variables, and every other inherited `GIT_*` or `SSH_*` variable. `PATH` MAY
contain only exact manager-owned helper basenames that the tested Git family
cannot resolve from `GIT_EXEC_PATH`.

HTTPS verifies TLS, follows no redirect, and receives credentials or a proxy
only through manager policy and broker state. For HTTPS,
`GIT_ASKPASS=<manager-broker>` is set.

A manager MAY resolve the secret the broker answers with from an
operator-owned credential store — the operator's platform secret store, or the
credential helper their own Git configuration selects — provided the operator
named the store for that repository through manager configuration, the
resolution happens outside the fetch process graph, and the answer is pinned to
one host. A helper, store, or account selected by repository data, a manifest,
a descriptor, or a substitution remains denied, and a resolved secret MUST NOT
outlive the fetch that needed it. For SSH the environment adds only:

```text
GIT_SSH=<absolute-manager-binary-wrapper>
GIT_SSH_VARIANT=ssh
<manager-private-policy-fd-number>
```

`GIT_PROTOCOL` is absent. Source credentials, SSH state, and broker values
MUST NOT enter a compiler environment, receipt, marker, or diagnostic.

### 11.2 Private initialization and exact acquisition

Only HTTPS, SSH URI, and SCP-like SSH spellings are supported. HTTPS has the
form `https://host/path` and forbids userinfo, password, explicit port, query,
fragment, percent escape, backslash, empty path, and redirects. SSH has the
form `ssh://[user@]host/path` or `[user@]host:path`; its OPTIONAL username is
ASCII `[A-Za-z0-9][A-Za-z0-9._-]{0,63}`. The host is ASCII, has no explicit
port, and is lowercased. The username affects the connection but is removed
from canonical identity.

Repository path components are non-empty, valid Unicode scalar text with no
whitespace, `%`, `?`, `#`, backslash, `.` component, or `..` component.
Leading/trailing slashes are removed, case is preserved, and exactly one
case-sensitive trailing `.git` is removed. The canonical network identity is
`host/path`, at most 4096 Unicode scalar values. SSH is further narrowed by
section 11.3. A network-looking value that fails these rules is invalid and
MUST NOT be reinterpreted as local.

Tags and operator branch refs are valid scalar text whose UTF-8 encoding is 1
through 255 bytes. They have no leading/trailing slash, `//`, trailing `.`,
component beginning `.`, component ending `.lock`, NUL/control/DEL/space,
`~`, `^`, `:`, `?`, `*`, `[`, backslash, `..`, or `@{`, and are not `@`.
The exact constructed `refs/tags/<tag>` or `refs/heads/<branch>` must also pass
the protocol equivalent of `git check-ref-format`. A revision is one full
lowercase object ID for the effective repository object format. No input is
passed as a revision expression.

Each network attempt uses a newly created operation-private bare repository,
zero-entry template directory, and empty hooks directory. The manager invokes
exactly:

```text
[<git>,
 "--git-dir=<operation-private>/repo.git",
 "-c", "init.defaultBranch=curator-invalid",
 "init", "--bare", "--quiet",
 "--template=<operation-private>/empty-template",
 "--object-format=<sha1-or-sha256>",
 "--ref-format=files"]
```

Manager code then opens the result without following links and requires the
requested object format, files refs, a manager-owned contained ordinary bare
repository, and no remote, alternate, promisor, partial-clone, worktree,
replace, graft, shallow, hook, filter, unknown extension, link, reparse escape,
special file, or writable boundary outside the operation principal. It never
repairs or adopts a failed candidate.

All network fetches use one direct validated HTTPS or SSH URL, one fixed
manager destination, closed stdin, bounded diagnostics, and this exact common
argument vector:

```text
[<git>,
 "--git-dir=<operation-private>/repo.git",
 "--no-replace-objects",
 "--no-lazy-fetch",
 "--no-optional-locks",
 "-c", "protocol.allow=never",
 "-c", "protocol.<selected-transport>.allow=always",
 "-c", "protocol.version=0",
 "-c", "credential.helper=",
 "-c", "core.askPass=<manager-broker>",
 "-c", "core.hooksPath=<operation-private>/empty-hooks",
 "-c", "core.fsmonitor=false",
 "-c", "core.untrackedCache=false",
 "-c", "submodule.recurse=false",
 "-c", "fetch.recurseSubmodules=false",
 "-c", "maintenance.auto=false",
 "-c", "fetch.writeCommitGraph=false",
 "-c", "fetch.fsckObjects=true",
 "-c", "transfer.fsckObjects=true",
 "-c", "http.followRedirects=false",
 "-c", "http.sslVerify=true",
 "-c", "http.proxy=",
 "-c", "https.proxy=",
 "fetch", "--quiet", "--atomic", "--no-tags",
 "--no-recurse-submodules", "--no-auto-maintenance",
 "--no-write-fetch-head", "--no-write-commit-graph",
 "--refmap=", "--jobs=1", "--upload-pack=git-upload-pack",
 "--", <validated-url>, <one-manager-refspec>]
```

The other transport and every remote-helper protocol remain denied. There is
no remote name, configured or stdin refspec, `--filter`, server option, prune,
mirror, depth/shallow option, tag auto-follow, or package-selected argument.
`FETCH_HEAD`, remote-tracking refs, local heads/tags, commit graphs, and
maintenance state MUST remain absent.

The sole refspec is:

- untagged unsubstituted declaration:
  `<full-locked-oid>:refs/curator/locked`;
- tagged unsubstituted declaration:
  `refs/tags/<tag>:refs/curator/tag`; or
- operator network substitution: exactly one full revision, exact tag, or
  exact branch source to `refs/curator/effective`.

A tagged declaration MUST use the exact tag ref as its sole acquisition path.
It MUST NOT try the locked object ID first or later, regardless of server
direct-object policy, and MUST NOT fall back to a branch, all-tags fetch, or
alternate ref. Manager code recomputes and peels the lightweight or annotated
tag chain and requires the terminal commit to equal the declared full lock.
A different commit is `build_repository_ref_moved`; a missing ref, nonzero
fetch, or transport failure is `build_repository_source_unavailable`.

An untagged declaration uses only the full locked object ID. A network
substitution uses only its structured ref form and never retries another form
or object format. The resulting recomputed full commit becomes the effective
state.

### 11.3 SSH isolation

The raw SSH repository path is ASCII `[A-Za-z0-9._/-]+` and every component is
non-empty and neither `.` nor `..`. Quotes, whitespace, shell metacharacters,
escapes, and non-ASCII bytes are rejected before Git.

The manager passes a protected read-only policy record through an already-open
descriptor to the binary wrapper. It contains exactly the expected
`[user@]host`, raw path, remote command
`git-upload-pack '<raw-path>'`, operator-trusted SSH executable, empty SSH
configuration, empty global known-hosts file, selected operator known-hosts
file, timeout, and one authentication mode. The wrapper accepts only:

```text
argv[0] = <absolute-manager-binary-wrapper>
argv[1] = <expected-host-or-user@host>
argv[2] = <exact-expected-git-upload-pack-command>
argc    = 3
```

It byte-compares the complete four-value tuple, including `argv[0]`, against
the protected policy record. It rejects a relative, alternate, or aliased
wrapper name, probe options, `-p`, `-4`, `-6`, `-o`, `SendEnv`, extra
operands, and any different host, user, path, or command. It then directly
executes:

```text
[<ssh>,
 "-F", <manager-empty-ssh-config>,
 "-T",
 "-o", "BatchMode=yes",
 "-o", "NumberOfPasswordPrompts=0",
 "-o", "PasswordAuthentication=no",
 "-o", "KbdInteractiveAuthentication=no",
 "-o", "PreferredAuthentications=publickey",
 "-o", "HostbasedAuthentication=no",
 "-o", "GSSAPIAuthentication=no",
 "-o", "StrictHostKeyChecking=yes",
 "-o", "UserKnownHostsFile=<operator-known-hosts>",
 "-o", "GlobalKnownHostsFile=<manager-empty-known-hosts>",
 "-o", "CheckHostIP=no",
 "-o", "VerifyHostKeyDNS=no",
 "-o", "UpdateHostKeys=no",
 "-o", "ForwardAgent=no",
 "-o", "ForwardX11=no",
 "-o", "ClearAllForwardings=yes",
 "-o", "PermitLocalCommand=no",
 "-o", "ProxyCommand=none",
 "-o", "ProxyJump=none",
 "-o", "ControlMaster=no",
 "-o", "ControlPath=none",
 "-o", "ControlPersist=no",
 "-o", "RequestTTY=no",
 "-o", "EscapeChar=none",
 "-o", "EnableEscapeCommandline=no",
 "-o", "CanonicalizeHostname=no",
 "-o", "ConnectionAttempts=1",
 "-o", "ConnectTimeout=<manager-policy-seconds>",
 <one-authentication-tail>,
 <expected-host-or-user@host>,
 <exact-expected-git-upload-pack-command>]
```

The authentication tail is exactly one of:

1. `-o IdentitiesOnly=yes -o IdentityAgent=none -i <operator-identity>` —
   an operator identity file and no agent;
2. `-o IdentitiesOnly=no -o IdentityFile=none -o
   IdentityAgent=<operator-agent-socket>` — an operator agent socket and no
   named identity, so the agent offers each loaded key in turn;
3. `-o IdentitiesOnly=yes -o IdentityAgent=<operator-agent-socket>
   -i <operator-identity>` — the pinned-agent form: the operator agent holds
   the private key and the named identity (conventionally the public half)
   pins which single key the agent offers.

The pinned-agent form is the RECOMMENDED selection when an agent is in use:
it authenticates passphrase-protected keys without a prompt, spends exactly
one server authentication attempt instead of one per loaded key, and
discloses one public key to the destination rather than enumerating every
key the agent holds. In every form the operator names each element
explicitly. No ambient agent socket, default identity, user/system SSH
configuration, prompt, TTY, forwarding, proxy, local command, or control
connection is admitted.

### 11.4 Local-substitution admission

A local substitution runs no Git command from the source repository. The
manager admits only a link-free ordinary non-bare worktree with a real
link-free `.git` directory directly below it. It opens configuration,
files-format refs, loose objects, and pack/index pairs as bounded untrusted
data without following links.

The stable layout failures are:

- regular `.git` gitfile:
  `build_repository_local_gitfile_unsupported`;
- selected bare repository:
  `build_repository_local_bare_unsupported`;
- `.git/commondir`, `.git/worktrees`, or `.git/config.worktree`:
  `build_repository_local_linked_worktree_unsupported`; and
- link, reparse point, special/multiply-linked mutable file, or containment
  escape: `build_repository_local_layout_unsafe`.

The byte-level `.git/config` parser admits this complete data-only subset:

1. The bounded file is valid UTF-8 and consists of LF or CRLF records with an
   OPTIONAL final line ending. A BOM, NUL, lone CR, backslash line
   continuation, or control byte other than TAB inside a value rejects the
   repository.
2. Blank records and records whose first non-space/TAB byte is `#` or `;` are
   ignored. In an assignment, the first `#` or `;` outside double quotes
   starts a comment; comment bytes are not part of the value. A comment marker
   inside quotes is data.
3. A section token and variable token are ASCII case-insensitive and match
   `[A-Za-z][A-Za-z0-9-]*`. A section record is a bracketed section token
   followed by at most one double-quoted UTF-8 subsection. A subsection admits
   only `\\` and `\"` escapes. No legacy dotted subsection syntax is admitted.
4. An assignment contains one variable token, OPTIONAL `=`, and one one-line
   value after surrounding ASCII space/TAB is removed. Omitting `=` means the
   boolean value true. A quoted value consumes exactly one double-quoted
   string and admits only `\\`, `\"`, `\n`, `\t`, and `\b`; after its closing
   quote only space/TAB and a comment are permitted. An unquoted value admits
   no quote, control byte, or backslash escape. Malformed or unsupported
   quoting/escaping rejects the repository.
5. `[include]` and every `[includeIf "..."]` section reject before any path is
   resolved. Section, subsection, and variable matching for admission is
   ASCII case-insensitive; retained inert values remain byte-exact.

In the grammar below `HSP` is one ASCII space or TAB, `token` is
`[A-Za-z][A-Za-z0-9-]*`, `comment` begins with `#` or `;` outside quotes and
continues to the record ending, and `end` is LF, CRLF, or EOF:

```text
blank      = HSP* end
commentary = HSP* comment end
section    = HSP* "[" token (HSP+ quoted-subsection)? "]" HSP* comment? end
bare-key   = HSP* token HSP* comment? end
assignment = HSP* token HSP* "=" HSP* (quoted-value / unquoted-value)? HSP* comment? end
```

`quoted-subsection` and `quoted-value` include their delimiting double quotes.
An unescaped closing quote ends them; their escape sets are exactly those
listed above. `unquoted-value` is the maximal sequence before the first
outside-quotes comment marker, with surrounding HSP removed; it contains only
valid UTF-8 scalar bytes plus TAB and excludes quote, backslash, NUL, CR, LF,
and other controls. `bare-key` is the only no-`=` form and decodes as boolean
true. An empty assignment decodes as the empty string, not as an absent value.
Any record that does not match exactly one production rejects.

The manager reads only these security-relevant keys:

- exactly one `core.repositoryformatversion`, with integer value `0` or `1`;
- exactly one `core.bare`, whose accepted boolean value is false;
- every `extensions.*`;
- every `remote "<name>".promisor`; and
- every `remote "<name>".partialCloneFilter`.

For a boolean key, the only spellings are case-insensitive
`true`/`yes`/`on`/`1` and `false`/`no`/`off`/`0`; an assignment without `=`
means true. A duplicate security-relevant key rejects even when both decoded
values are equal. Duplicate ordinary non-extension keys MAY remain ordered
inert data, but the manager never applies them and never uses first-value,
last-value, or multivalue Git behavior. Other well-formed non-extension keys
are likewise recorded only as inert bytes.

The only admitted format states are:

- SHA-1: `core.repositoryformatversion=0`, `core.bare=false`, and no
  `extensions.*`; or
- SHA-256: `core.repositoryformatversion=1`, `core.bare=false`, exactly one
  `extensions.objectFormat=sha256`, and OPTIONAL exactly one
  `extensions.refStorage=files`.

`extensions.refStorage=reftable`, `extensions.partialClone`,
`extensions.worktreeConfig`, `extensions.compatObjectFormat`, `noop`,
`preciousObjects`, an unknown extension, a true promisor value, or a non-empty
partial-clone filter fails `build_repository_local_format_unsupported`.

`HEAD`, every loose ref below `.git/refs`, and OPTIONAL `.git/packed-refs` are
bounded link-free regular files. `HEAD` is exactly one full lowercase object
ID or `ref: refs/heads/<safe-name>`, followed by exactly one LF, exactly one
CRLF, or EOF; an extra blank record is invalid. A symbolic `HEAD` has exactly
one level: the selected head ref contains one full lowercase object ID with
the same exact terminator rule and is never another symbolic ref.
Every other loose-ref file likewise contains exactly one full lowercase object
ID and exactly one LF, exactly one CRLF, or EOF; symbolic loose refs and extra
records are not admitted.

Every ref name passes section 11.2's safe-ref grammar and the protocol
equivalent of `git check-ref-format`. `packed-refs` has at most one
`# pack-refs with:` header whose unique space-separated traits are drawn only
from `peeled`, `fully-peeled`, and `sorted`. Its remaining records are exactly
`<full-lowercase-id> SP <refname>` with OPTIONAL `^<full-lowercase-id>` only
immediately after a tag record. Duplicate packed names, duplicate or
misplaced peeled records, unsupported headers/traits, wrong-width/case IDs,
malformed lines, and platform-colliding loose paths reject.

Any loose or packed `refs/replace/*` entry rejects whether or not it is
selected or reachable. A unique valid loose ref has normal Git precedence and
shadows the same packed name; otherwise the packed value is used. The parser
does not merge, compare, or select between their object IDs. Contradictory
duplicate loose files or duplicate packed records reject instead of invoking
an implementation-specific choice. The selected committed `HEAD` is effective
input; index, worktree, staged, dirty, and untracked bytes are never source.

The frozen object inventory contains only correctly named loose objects and
matching `pack-<full-hash>.pack`/`.idx` pairs. It rejects alternates,
http-alternates, grafts, shallow state, namespaces, promisor/cruft state,
commit graphs, multi-pack indexes, bitmaps, reverse indexes, keep/mtimes files,
and every unexpected object sidecar. The manager copies the admitted inventory
into inert operation-private state and re-stats and rehashes every source file;
any generation change aborts.

Portable pack admission is exactly pack version 2 or 3 plus index version 2
for the repository hash family. The manager validates:

- `PACK`, big-endian version/count, pack trailer hash, and basename;
- index magic/version, monotonic 256-entry fanout, and final count;
- strictly sorted full-width object names, CRC32 entries, unique resolved
  32/64-bit offsets, dense referenced large-offset table, and CRC32 over each
  packed-entry range; and
- paired pack and index checksums with no omitted/trailing bytes, duplicate
  names/offsets, or cross-family widths.

Any other pack/index form or malformed table fails
`build_repository_local_object_format_unsupported`. The manager then creates a
fresh private bare repository, copies only admitted loose objects and paired
pack/index bytes, writes only manager config, seals the object store read-only,
and applies the common object reader below.

An `OFS_DELTA` base remains within its paired admitted pack. A `REF_DELTA` base
may resolve only from another admitted pack or loose object in the same sealed
private store. Missing, cyclic, oversized, alternate, promisor, source-tree, or
network delta resolution fails complete-source proof; the manager never widens
the store.

### 11.5 Raw-object proof

Network and local sources converge on one manager-created private repository.
After physical admission, the manager launches exactly one child:

```text
[<git>,
 "--git-dir=<operation-private>/repo.git",
 "--no-replace-objects",
 "--no-lazy-fetch",
 "--no-optional-locks",
 "-c", "core.hooksPath=<operation-private>/empty-hooks",
 "-c", "core.fsmonitor=false",
 "-c", "core.untrackedCache=false",
 "-c", "maintenance.auto=false",
 "cat-file", "--batch=%(objectname) %(objecttype) %(objectsize)"]
```

The repository is read-only, the clean environment from section 11.1 remains
in force, the child cannot start another process or use the network, and
stderr, time, object count, individual bytes, aggregate expanded bytes, tree
depth, path length/depth, tag depth, memory, process, and disk use are bounded.
Portable minima are fixed by the rc.5 conformance vectors; stricter manager
policy MUST be documented.

Each stdin request is exactly one full lowercase object ID and LF. Each stdout
response is exactly:

```text
<same-full-id> SP <commit|tag|tree|blob> SP <canonical-decimal-size> LF
<exact-content-bytes> LF
```

The size has no sign or leading zero. The returned ID equals the request.
Missing, ambiguous, excluded, submodule, malformed, truncated, extra, early
EOF, trailing output, nonzero exit, timeout, or limit failure is
`build_repository_incomplete_source`. The argv MUST NOT contain batch-command,
all-object, buffering, symlink-following, ordering, textconv, filter, mailmap,
path, NUL-framing, or transformation options.

For every consumed object, manager code recomputes:

```text
HASH(type || " " || decimal-content-byte-length || NUL || exact-content)
```

using the repository object format and requires equality with both requested
and returned IDs.

Commit and annotated-tag objects use one byte grammar. The bounded object
contains no NUL. Its header block uses LF only, contains no CR, and ends at the
first required exact `LF LF`; a lone final header LF is not a separator. Bytes
after the separator are a bounded opaque message, may contain LF, CR, and
non-UTF-8 bytes, and contain no NUL.

A primary header is `<key> SP <value> LF`. `key` begins with an ASCII letter
and continues with zero or more ASCII letters, digits, or `-`. `value` is a
bounded byte string with no LF, CR, or NUL and may be empty only for an ignored
extra header. Zero or more continuation records
`SP <continuation-bytes> LF` may follow an ignored extra header only;
continuation bytes may be empty but contain no LF, CR, or NUL. A continuation
before a primary header or after a required structural header is invalid.
Ignored extras, including `gpgsig`, `gpgsig-sha256`, and `mergetag`, are
retained as ordered opaque metadata and are never verified or used as
provenance.

A commit has exactly this order:

1. first and unique `tree <full-lowercase-id>`, without continuation;
2. contiguous zero-or-more `parent <full-lowercase-id>` records, without
   continuation and retaining byte order;
3. exactly one non-empty `author` and then exactly one non-empty `committer`,
   without continuation; and
4. zero-or-more extra headers whose keys are not `tree`, `parent`, `author`,
   or `committer`, followed by the exact separator.

An annotated tag has exactly this order:

1. first and unique `object <full-lowercase-id>`;
2. unique `type commit` or `type tag`;
3. unique `tag <safe-tag-name>` using section 11.2's byte grammar;
4. OPTIONAL one non-empty `tagger`; and
5. zero-or-more extra headers whose keys are not `object`, `type`, `tag`, or
   `tagger`, followed by the exact separator.

Required tag headers admit no continuation. When an exact declared tag ref
selects an annotated chain, the outermost annotated tag object's `tag` value
MUST byte-equal the requested tag; nested tag names need only pass the safe-tag
grammar. After recomputation, each target's actual type MUST byte-equal the
declared `type`. `tag` continues peeling and `commit` terminates it; tree/blob
targets are invalid. A lightweight tag points directly to a commit and uses no
annotated-depth slot. A chain has at most 16 annotated tag objects and no
repeated full object ID.

Duplicate, misplaced, malformed, wrong-width, wrong-case, wrong-type, cyclic,
over-depth, outer-tag-name-mismatch, or missing-separator commit/tag data fails
`build_repository_git_object_semantics_invalid`.

The terminal commit selects exactly one tree. A tree is fully consumed as
`<octal-mode> SP <name> NUL <binary-object-id>`. Names are non-empty portable
UTF-8 components with no slash, NUL, dot component, duplicate, or platform
collision. Only tree mode `40000` and blob modes `100644` and `100755` are
accepted. Symlink `120000`, gitlink `160000`, special/unknown modes, and
mode/type mismatch fail before snapshot materialization. The manager proves
every reachable tree/blob locally available and materializes only exact
verified blob bytes as regular files, preserving only the executable-bit
distinction.

Every reachable blob is scanned once before materialization, regardless of
path, mode, extension, descriptor target, or selected build root. A non-empty
blob below 1024 bytes is rejected when it matches the accepted decoder/encoder
family in Git LFS 3.7.1 `lfs/pointer.go`. The compatibility parser:

1. trims only the Go 1.25 `unicode.IsSpace` UTF-8 encodings from both ends;
2. splits on LF, removes one terminal CR per line, and ignores empty lines;
3. splits each non-empty line at its first ASCII space;
4. accepts required keys exactly once in state order `version`, `oid`, `size`,
   with extension lines allowed before any still-pending required key and no
   line after `size`;
5. accepts exactly the current version
   `https://git-lfs.github.com/spec/v1` and aliases
   `https://hawser.github.com/spec/v1` and `http://git-media.io/v/2`;
6. requires `oid` to be `sha256:` plus 64 lowercase hexadecimal bytes;
7. parses `size` exactly as Go base-10 signed-int64 input and requires a
   nonnegative value, so `+1`, `00`, and `01` are accepted;
8. accepts an extension key beginning `ext-`, one ASCII digit, `-`, and one
   or more ASCII word bytes, while retaining later non-space/non-LF suffix
   bytes as part of the raw name; its value uses the exact OID grammar;
9. applies last-value-wins to an identical raw extension key and rejects
   distinct surviving extension keys with the same numeric priority; and
10. rejects the pointer match on a missing/unknown required key, malformed
    value, bad extension, or duplicate priority.

Exact canonical and legacy/noncanonical matches both fail
`build_repository_git_lfs_unsupported`. Zero bytes and 1024 bytes or more are
outside that detector. Unknown versions, malformed, uppercase, or wrong-width
OIDs,
missing required keys, invalid extensions, or distinct duplicate extension
priorities are ordinary blob bytes, not hydration instructions. The manager
MUST NOT invoke Git LFS, a filter, an LFS endpoint, or `.git/lfs`.

### 11.6 Snapshot, audit, and cache ordering

For every active external repository, the manager:

1. completes exact source acquisition and, for a declared tag, exact tag
   equality with the immutable lock;
2. proves the complete object graph and LFS absence;
3. freezes and validates the complete regular-file repository snapshot;
4. computes `curator-build-source-v1` over every file, including
   `skill-build.json` and files outside the selected build root;
5. parses the descriptor and validates the selected target/module containment;
6. applies allowlist, revocation, registry, tag-lock, and audit-policy gates
   independently to the external subject; and
7. only then reads an artifact-cache candidate or starts a compiler child.

Skill evidence never attests an external repository, and external evidence
never attests the skill. Audit identity binds declared and effective source,
object format, full effective commit, source digest, descriptor target,
substitution state, and successful tag assertion when applicable.

The protected snapshot key is the complete effective identity kind/value,
effective object format, full effective commit, and external build-source
digest. Snapshot bytes may be deduplicated only when that complete key is
equal. Audit results remain subject-specific. The artifact key is the SHA-256
of the complete receipt-v2 input. Different declaration, effective source,
substitution, command, target, build root/source directory, native target,
toolchain, or policy revision MUST NOT alias.

Schema-7 mixed plans retain receipt schema 1 and schema-6 skill build-source
meaning for each local `go-v1` command. Each external command has receipt
schema 2 and its own external state. Marker v3 explicitly records the receipt
version for every build; its top-level `build_source` is present exactly when
an active local `go-v1` command requires it. No reader infers a receipt or
marker field from a driver name.

### 11.7 Dry-run, offline, and reporting semantics

Dry-run MAY use network reads and removable operation-private repositories to
resolve, prove, hash, and independently audit exact external snapshots. It MAY
probe the trusted Git and Go distributions and inspect protected state
read-only. It MUST NOT leave a repository, snapshot, audit response, memo,
cache, lock, journal, staging directory, marker, receipt, shim, adapter,
consumer, or configuration mutation and MUST NOT run `go list`, `go build`, a
compiler, linker, signer, or artifact.

A syntax-only offline validation that cannot obtain an exact source emits
`build_repository_unverified_offline` as a warning and claims no source,
audit, cache, receipt, artifact, marker, or installation coverage. Real or
dry-run install/upgrade, update, repair, and coverage-claiming audit fail
`build_repository_source_unavailable` before mutation. A protected snapshot is
not a substitute for an exact-tag assertion during install or repair.

Install/upgrade dry-run reports per command exactly one of:

```text
cache-hit
would-preflight-and-build
would-rebuild-untrusted-cache
corrupt
unsupported
blocked
```

`cache-hit` requires exact source proof, independent audit, and complete
receipt/artifact validation. Install-family dry-run is a coverage-claiming
operation: if exact source is unavailable it emits `blocked` with
`build_repository_source_unavailable`, returns failure, and leaves no
mutation. It MUST NOT emit `unverified-offline` or return warning success.

Only a syntax-only `skill check` may emit state `unverified-offline` with
warning code `build_repository_unverified_offline`; that result makes no
source or lifecycle claim and is not a dry-run installation result. `update`,
real or dry-run `install`/`upgrade`, `repair`, and coverage-claiming `audit`
all use the hard-failure rule before mutation. Multi-project planning
deduplicates one acquisition only when its complete effective source request
and operator policy are equal; it still performs subject-specific audit
decisions and emits one ordered result per command.

### 11.8 Private build, publication, PATH, and rollback

After exact audit, `go-repository-v1` reuses the accepted `go-v1` trusted Go
session, native target, fixed `go list`/`go build` vectors, the portable
`manager-worker-v1` execution policy and worker session of section 2.2.1,
compiler-visible dependency containment, directive/native-input rejection,
internal-link policy, artifact validation, and no-execution rule. Only the
selected external build root is compiler-visible. No external source enters agent context,
commit-keyed runtime copying, a sibling command, or another repository.

All misses build into operation-private staging before shared mutation. The
manager validates each sole manager-derived artifact, generates the canonical
receipt/marker, and then acquires the manager-home mutation lock. Recovery and
revalidation precede immutable snapshot/artifact publication. The existing
journal commits targets in section 2.5 order with the consumer ledger last.
Rollback reverses committed targets while retaining the same lock.

A command shim is manager-owned and resolves exactly to the protected artifact
selected by marker v3. It MUST NOT point into a Git object database, frozen
snapshot, checkout, staging directory, or script runtime. The artifact is one
regular singly linked executable whose path, hash, and size match its receipt.
Environment exposure prepends only the manager-generated bin directory.
Validation uses no shell, executable search, or artifact execution. Repository
data cannot request aliases, sidecars, copies, additional PATH entries,
signing, timestamps, or notarization.

The first-release `go-repository-v1` profile MUST NOT perform manager
post-build signing, timestamping, or notarization. If platform or operator
policy requires local signing, installation MUST fail closed before
publication with phase `signer-policy`, state `unsupported`, severity `error`,
and code `build_repository_signer_policy_unsupported` until a separately
versioned and reviewed signer profile defines the complete signer boundary.
Operator or release-pipeline Apple Developer ID signing/notarization and
Windows Authenticode signing/timestamping remain outside install-time build
receipts, cache identity, manager publication, and this profile.

### 11.9 Read-only status, repair, and garbage collection

Read-only status derives the current manifest and operator substitution plan,
validates marker v3, revalidates protected snapshot/artifact boundaries,
recomputes every available source digest, receipt input/key, receipt hash, and
artifact relationship, and structurally validates shims and PATH exposure. It
MUST NOT contact a remote merely to test availability or tag movement and MUST
NOT fetch, refresh, repair, quarantine, change permissions, compile, sign, or
execute.

Identity, commit, digest, target, substitution, receipt, artifact, shim, or
boundary mismatch is non-current. Missing required protected evidence is not
current. An implementation MAY distinguish `unknown` from drift when evidence
cannot be read, but it MUST NOT report current and checking status returns
nonzero.

Repair repeats the current effective acquisition, object proof, freeze, audit,
cache decision, private build, and serialized publication. An unsubstituted
tagged declaration uses only the exact-tag path. Repair never adopts candidate
bytes by changing permissions, recomputing a marker, or trusting a consistent
receipt.

GC runs under the manager-home mutation lock after revalidating every protected
boundary. Valid marker v3 records mark referenced local skill snapshots,
complete external snapshot keys, receipt/artifact keys, and manager-generated
shim relationships. In-flight journals mark every snapshot/artifact needed for
commit or rollback. Marker v1/v2 roots remain unchanged. Receipt content alone
is not a root. Unreadable markers/journals or unprovable references retain
uncertain entries. GC never repairs permissions, executes, or adopts source or
artifact bytes.

### 11.10 Stable diagnostics and package-controlled behavior

Every structured result has stable `phase`, `state`, `severity`, and `code`
members. `severity` is exactly `warning` or `error`; successful results use
`severity: null` and `code: null`. The table's `State` is the primary state for
syntax-only, install-family planning/execution, update, repair, and
coverage-claiming audit. The following mappings are normative:

| Code | Phase | State | Severity | Meaning |
|---|---|---|---|---|
| `build_repository_schema_invalid` | `schema` | `unsupported` | `error` | Schema-7 object fails its closed schema |
| `build_repository_version_unsupported` | `schema` | `unsupported` | `error` | Manifest, descriptor, receipt, marker, or claim version is unsupported |
| `build_repository_driver_unsupported` | `schema` | `unsupported` | `error` | Driver is not the closed `go-repository-v1` contract |
| `build_repository_descriptor_invalid` | `descriptor` | `unsupported` | `error` | Descriptor target, build root, source directory, or containment is invalid |
| `build_repository_identity_invalid` | `source` | `unsupported` | `error` | URL, transport, object format, lock, tag, branch, or ref grammar is invalid |
| `build_repository_source_unavailable` | `source` | `blocked` | `error` | Exact source or transport is unavailable for a coverage-requiring operation |
| `build_repository_ref_moved` | `source` | `blocked` | `error` | Exact declared tag terminates at a different commit |
| `build_repository_unverified_offline` | `syntax` | `unverified-offline` | `warning` | Syntax-only result with no source or lifecycle coverage |
| `build_repository_incomplete_source` | `object-proof` | `corrupt` | `error` | Object, batch stream, graph, or portable limit proof is incomplete |
| `build_repository_git_object_semantics_invalid` | `object-proof` | `corrupt` | `error` | Recomputed commit, tag, or tree semantics are invalid |
| `build_repository_git_lfs_unsupported` | `object-proof` | `unsupported` | `error` | Reachable blob matches the pinned Git LFS parser family |
| `build_repository_local_gitfile_unsupported` | `source` | `unsupported` | `error` | Local `.git` is a gitfile |
| `build_repository_local_bare_unsupported` | `source` | `unsupported` | `error` | Local selection is bare |
| `build_repository_local_linked_worktree_unsupported` | `source` | `unsupported` | `error` | Local administration uses linked-worktree state |
| `build_repository_local_layout_unsafe` | `source` | `corrupt` | `error` | Local containment, link, ownership, or file-type proof fails |
| `build_repository_local_format_unsupported` | `source` | `unsupported` | `error` | Local config, refs, extension, promisor, or partial-clone state is unsupported |
| `build_repository_local_object_format_unsupported` | `object-proof` | `unsupported` | `error` | Local pack/index/container form is unsupported or malformed |
| `build_repository_credential_policy_invalid` | `transport-policy` | `blocked` | `error` | Credentials, proxy, host verification, or authentication mode is absent or outside operator policy |
| `build_repository_audit_failed` | `audit` | `blocked` | `error` | Independent allowlist, registry, revocation, tag-lock, or audit policy fails |
| `build_repository_protected_boundary_untrusted` | `cache` | `corrupt` | `error` | Snapshot or artifact protection/containment cannot be proved |
| `build_repository_receipt_invalid` | `cache` | `corrupt` | `error` | Receipt version, canonical bytes, input, key, or hash is invalid |
| `build_repository_marker_invalid` | `currentness` | `corrupt` | `error` | Marker v3 structure or relationship is invalid |
| `build_repository_artifact_invalid` | `artifact` | `corrupt` | `error` | Artifact path, type, size, hash, executable state, or shim relation is invalid |
| `build_repository_non_current` | `currentness` | `non-current` | `error` | Read-only status proves drift or missing required evidence |
| `build_repository_currentness_unknown` | `currentness` | `unknown` | `error` | Read-only status cannot prove required protected evidence |
| `build_repository_compiler_policy_violation` | `build` | `blocked` | `error` | Trusted toolchain, dependency, directive, native-input, or fixed process policy fails |
| `build_repository_package_argv_forbidden` | `descriptor` | `unsupported` | `error` | Package data attempts to select a program, argument, flag, hook, recipe, or fallback |
| `build_repository_package_environment_forbidden` | `descriptor` | `unsupported` | `error` | Package data attempts to select compiler or child environment |
| `build_repository_package_output_forbidden` | `descriptor` | `unsupported` | `error` | Package data attempts to select executable name, output path, sidecar, copy, or PATH entry |
| `build_repository_package_credential_forbidden` | `transport-policy` | `blocked` | `error` | Package data attempts to select a credential, proxy, host key, SSH agent, or authentication policy |
| `build_repository_package_signing_forbidden` | `signer-policy` | `blocked` | `error` | Package data attempts to select signing, timestamping, notarization, identity, entitlement, service, or signer argv |
| `build_repository_signer_policy_unsupported` | `signer-policy` | `unsupported` | `error` | Platform requires local signing but no reviewed signer profile exists |
| `build_repository_transaction_failed` | `publication` | `blocked` | `error` | Journal, lock, publication, consumer-last commit, recovery, or rollback fails |

For install-family planning, `state` is one of `cache-hit`,
`would-preflight-and-build`, `would-rebuild-untrusted-cache`, `corrupt`,
`unsupported`, or `blocked`. For syntax-only output the only no-coverage state
is `unverified-offline`. Read-only status applies one exact overlay without
changing `phase`, `severity`, or the most specific code: a proved mismatch uses
`state: non-current`; unreadable or otherwise unprovable required evidence
uses `state: unknown`; a fully proved item uses `state: current`,
`severity: null`, and `code: null`. The generic
`build_repository_non_current` or
`build_repository_currentness_unknown` code is used only when no more specific
stable row explains the status result.

Recognized package-controlled behavior uses the five specific
`build_repository_package_*_forbidden` rows before the generic
`build_repository_schema_invalid` or
`build_repository_descriptor_invalid` row. For example, recognized `argv`,
`env`, output/PATH, credential/host-policy, and signer fields map respectively
to the argv, environment, output, credential, and signing codes even though
the closed schema also rejects those fields. This precedence prevents schema
validation order from changing interoperable diagnostics. A known failure
MUST NOT be collapsed into source unavailability, a cache hit, audit success,
generic fallback, or an untyped message.

Human output renders the stable tuple as
`<command>: <state> [<phase>/<severity>/<code>]` before OPTIONAL sanitized
detail. Structured output carries the tuple as separate members. Diagnostics
MAY include bounded repository, command, phase, and path context but MUST NOT
include credentials, broker output, SSH agent paths, private keys, Git protocol
bytes, object contents, pointer OIDs as fetch instructions, compiler secrets,
or arbitrary remote/package diagnostics without sanitization.

Package, descriptor, repository, substitution, source configuration, and
source output MUST NOT select or extend Git/SSH/HTTPS executables, helpers,
hooks, filters, attributes, alternates, replacements, grafts, promisor/lazy
network reads, credentials, proxy, host verification, compiler program, argv,
environment, target, output, PATH entry, signing identity, signer argv,
timestamp/notary service, fallback, or diagnostic rendering.
