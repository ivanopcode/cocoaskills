# Decision 0004: compile-only build drivers

## Context

Schemas 1 through 5 can copy script commands or resolve system commands but
cannot produce a native artifact from source. Allowing a package to supply a
shell command, executable, argument vector, environment, plugin, generator, or
build-system recipe would contradict the existing rule that installation does
not execute package-provided code.

Compiled artifacts also cannot reuse the existing `skill + commit` runtime
identity. Compiler-visible inputs include files outside one package directory,
the selected native target and policy, and the exact trusted toolchain. An
internally consistent receipt from an attacker-writable cache is not proof that
the manager produced its artifact.

## Decision

Manifest schema 6 is a declarative compiled-artifact extension. It adds
explicit `build_roots` and one closed command object containing only
`type: "build"`, `driver: "go-v1"`, and `source_dir`. Build roots are dedicated
link-free module roots below the snapshot, cannot be `.`, cannot overlap
runtime roots, and are statically excluded from agent context and runtime
copying before cache lookup or compiler discovery. The same exclusion applies
to real builds, exact cache hits, and dry-runs. Artifacts live only in
manager-owned staging and immutable cache state.

Protocol v1 defines only `go-v1`. It is a fixed native, vendor-only,
networkless, cgo-free Go 1.23+ pipeline using an operator-trusted and
fingerprinted release family. The manager owns the process graph, environment,
arguments, target, output path, dependency preflight, directive checks, and
internal-link policy. It rejects non-standard assembly, host objects,
`//go:cgo_import_dynamic`, external linking, libgcc fallback, PGO, workspaces,
and toolchain switching, and it starts no program outside the fingerprinted Go
launcher and its fingerprinted tool directory. The built artifact is never
executed during manager activity.

**Superseded before publication.** The original clause required the manager to
invoke the fingerprinted Go launcher as its own direct child. That graph left no
place to install controls before compiler code runs. Since neither `1.0.0-rc.4`
nor `1.0.0-rc.5` has been released or pinned, the process graph is amended in
place rather than deprecated: one identity-verified manager-owned worker sits
between the manager parent and the fingerprinted Go launcher, and the operation
declares the named execution policy `manager-worker-v1`. See decision 0006.
The process-graph rule is manager selection plus identity verification, not a
kernel executable allowlist, and the offline `network: "none"` policy is fixed Go
module, proxy, checksum-database, and VCS configuration rather than kernel
network denial; `protocol/core.md` section 4.2.1 names both boundaries and the
guarantees deferred to `STORY-260728-327soo`. Everything else in this decision,
including the exact argument vectors, the environment, the rejection matrix, the
manager-derived output, and the no-execution rule, is unchanged.

Two source/currentness identities remain separate:

- installed-tree `content_sha256` retains its schema 1 through 5 semantics and
  excludes the manager marker;
- `curator-build-source-v1` is domain-separated and length-framed over every
  regular file in the validated raw snapshot, including a package-provided
  root `.csk-install.json`, and is computed before cache lookup.

The build input also binds `curator-go-toolchain-v1`, command/source selection,
native target and tuning, driver revision, the execution-policy identity, and
the complete fixed policy. Its CCJ-1 digest is the logical cache key. Exact canonical receipt bytes bind the
input and manager-derived artifact path/hash/size; their digest supplies
corruption and currentness detection, not authentication. Persistent reuse
requires a separately verified manager-created, owner-protected, link-safe
cache boundary. Untrusted protection forces a rebuild from the revalidated
snapshot, even when every embedded hash matches.

Marker schema 2 records build roots, build-source identity, logical cache keys,
receipt hashes, artifact hashes, and manager-derived artifact-relative paths.
Managers read marker schemas 1 and 2, write v2 after mutation, and may retain a
valid v1 marker as current for schema 1 through 5 packages. Shims for build
commands select immutable cache artifacts rather than the commit-keyed script
runtime store.

A real operation validates and builds misses in operation-private staging
before installation mutation. Independent projects may validate and compile
concurrently, but cache publication, project/global/hybrid/adapter mutation,
consumer update, rollback/recovery, and garbage collection serialize beneath
one manager-home mutation lock. One journal covers the ordered mutable target
set; the consumer ledger commits last; rollback stays locked and proceeds in
reverse commit order. This prevents cross-project lost updates and stale
backup restoration.

Dry-run performs only package-independent toolchain probes and read-only
planning. It does not run `go list`, `go build`, a compiler, or linker; create
Go caches, persistent fingerprint memos, mutation locks, journals, or manager
state; publish/quarantine/repair cache entries; or change agent context,
runtime, markers, shims, adapters, audit/registry state, or consumers.

Only logical identity and validation are portable. Manager-home directories,
physical cache-root and driver-subdirectory names, receipt filenames, lock and
quarantine names, and storage backends are deliberately non-normative. An
illustrative layout such as `<home>/<cache>/go-v1/<key>/bin/<command>` MUST NOT
be treated as a compatibility identifier.

## Rejected alternatives

- Arbitrary package shell commands, executable names, argv/environment arrays,
  output selection, hooks, plugins, generators, tests, and pre/post-build steps
  were rejected because they transfer install-time execution control to the
  package.
- Cargo was rejected for v1 because build scripts and procedural macros execute
  package-selected code. Direct Rust compilation still needs a closed
  dependency and linking contract.
- Zig and Swift were deferred because their build manifests, plugins, macros,
  or compile-time execution require distinct process and side-effect models.
- Make, CMake, Ninja, and Meson were rejected as package-controlled recipe
  frontends rather than fixed compilers.
- Maven, Gradle, MSBuild, and package-oriented Java/Kotlin/.NET compilation were
  deferred because tasks, plugins, annotation processors, analyzers, and source
  generators execute at build time.
- npm lifecycle scripts and Node/TypeScript bundler configuration, loaders, and
  plugins were rejected. A fixed transpiler or Deno subset still needs a closed
  dependency, configuration, and runtime-output model.
- Python package builds were rejected because PEP 517 frontends execute the
  package-selected backend and may resolve dynamic build requirements.
- Direct C/C++ compilers were deferred because a portable driver must own and
  identify source enumeration, preprocessing, sysroots, response files,
  compiler plugins, native dependencies, and linking inputs.
- `skill + commit`, installed-tree `content_sha256`, and receipt-only trust were
  rejected as cache identities because they omit compiler/toolchain policy or
  allow attacker-created self-consistent candidates.
- Standardizing cache directory names and receipt filenames was rejected
  because machine-local layout is an implementation boundary, not wire state.

## Compatibility impact

Schema 1 through 5 manifests and marker-v1 currentness retain their exact
deployed behavior. They reject the schema-6-only field and command surface.
Schema 6 and marker v2 are explicit reader/writer version transitions; readers
never infer them from fields. The build feature is released with a new current
protocol `1.0.0-rc.4` conformance claim schema 2. Conformance claim schema 1
remains frozen as historical rc.3 evidence and cannot claim schema-6 or
build-driver behavior.

## Security impact

The accepted model preserves the no-hooks invariant while admitting untrusted
compiler input. It does not claim that the Go compiler or compiled artifact is
safe. Compiler-input denial of service and vulnerabilities require host
resource limits and sandboxing; decision 0006 defines how much of that the
portable profile actually delivers and what remains deferred. Artifact execution
remains a later user action. Protected local cache state is part of the v1 trusted computing base;
same-principal, administrator/root, kernel, and hostile-storage compromise are
outside that boundary and require cache reuse to be disabled when unacceptable.

## Future-driver rule

A future driver requires a new closed identifier and independent protocol and
security review. The proposal must define a strict manifest surface, complete
compiler-input boundary, trusted toolchain identity, fixed direct process graph
and environment, network/dependency/link policy, manager-derived output,
logical cache/receipt identity, dry-run behavior, lifecycle/rollback rules, and
cross-language conformance vectors. It MUST demonstrate that package-selected
hooks, plugins, generators, macros, build recipes, and produced output cannot
execute during manager activity. No future driver may be enabled by broadening
`go-v1` or adding a generic fallback.
