# Architecture

For vulnerability reporting procedures and platform hardening checklists, see
[SECURITY.md](SECURITY.md). English is the source of truth.

This document maps the codebase for contributors: the core concepts, the
install pipeline, the module layout, the storage locations, and the security
model. Design decisions live in the RFCs under [docs/](docs/); this
document points at them where relevant.

## Core concepts

CocoaSkills operates on two manifests with distinct ownership:

- `Skillfile.json` describes a project: the skills the project installs
  directly, the agent systems to adapt, and the locale. It is committed to the
  project repository.
- `agent-skill.json` describes a skill node: the commands it exports, the
  capabilities it declares, and the requirements it has on system tools and on
  other skills. It lives in the skill repository; `csk-skill.json` is a
  read-only legacy filename.

A schema-6 skill may materialize three independent layers:

- The prompt context layer: `SKILL.md`, `references/`, and other agent-facing
  files copied into `<project>/.agents/skills/<name>/` and mirrored into agent
  adapter directories. This is what the agent reads.
- The runtime layer: `runtime_roots` copied into the shared runtime store and
  exposed as command shims in `<project>/.agents/bin/`. Agents and humans
  execute these shims explicitly; optional shell activation only adds bare-name
  convenience. Runtime files stay out of the agent context.
- The compiled layer: `build_roots` remain in the validated raw source snapshot
  but are copied into neither prompt context nor the script runtime store. A
  closed `go-v1` driver compiles a declared `source_dir` into a protected,
  immutable manager-home cache entry, and the command shim targets that entry
  directly.

The split keeps the agent window small and makes activation modes possible: a
dependency can contribute commands, context, or both ([RFC 0007](docs/v0.9-design.md)).
Consequently, prompt-visible instructions resolve exported script and compiled
command shims by project/global scope and never address a `runtime_root` or
`build_root` relative to `SKILL.md`. This remains true when an adapter mirrors
context by copy instead of symlink, and it removes any dependency on
zsh/bash/PowerShell profile initialization.

The installer applies a whitelist stripped layout to protect agent context
windows. Repository assets such as tests, CI configurations, and git metadata
increase prompt token count when read by an agent. The whitelist strips
non-essential files during copy so only required skill instructions reach prompt
context.

CocoaSkills maintains one canonical `.agents/skills/` root with per-agent
adapters, unifying skill instructions across multiple AI coding tools. Agent
platforms expect distinct directory paths such as `.claude/skills/` or
`.cursor/rules/`. Each adapter tracks managed entries within its agent directory
and mirrors context files by symlink or copy as required by the target platform.

`skillspec.py` accepts manifest schemas 1 through 6. Schema 6 adds
`build_roots` and the closed build command
`{"type":"build","driver":"go-v1","source_dir":"..."}`. Build roots must
be real, link-free, portable, unique, disjoint from one another and from
runtime roots, and used by a command. Each source directory belongs to exactly
one build root whose direct `go.mod` is the nearest module root. Unknown build
drivers or package-selected build fields fail during parsing; there is no
driver fallback.

## Install pipeline

`csk install` for one project runs these stages in order:

1. Load machine and project configuration (`config.py`, `manifest.py`) and
   verify the gitignore gate before any project write (`gitignore_gate.py`).
2. Load development substitutions (`dev_substitutions.py`) and applicable
   hybrid declarations (`hybrid.py`). Strict audit refuses substitutions.
3. Resolve the transitive closure (`closure.py`): source-allowlist checks,
   exact refs, raw snapshots, one commit and canonical source per skill,
   cycle rejection, activation edges, and provider-before-consumer order.
4. Validate every selected raw snapshot (`skillcheck.py`, `skillspec.py`).
   Build roots remain present for validation and hashing, while whitelist and
   runtime plans exclude them. Detect collisions over the active script and
   compiled launcher namespace.
5. Verify MCP requirements and run source, audit, registry, and trust gates
   over the complete closure (`mcp_configs.py`, `audit/`,
   `audit_registry.py`). These gates precede cache claims and compiler work.
6. Freeze each build provider's validated raw snapshot and compute its
   `curator-build-source-v1` identity (`builds/source.py`). Select and
   fingerprint the operator-trusted Go installation (`builds/toolchain.py`),
   derive the native target and complete `manager-worker-v1` logical input,
   then inspect the protected cache read-only (`builds/planner.py`). Providers
   are planned before consumers; commands within a provider are lexical.
7. For a real cache miss, hold its per-key build lock and compile in an
   operation-private directory outside the manager-home mutation lock.
   `builds/go_v1.py` owns the worker boundary. Cache hits never run
   source-aware Go commands.
8. Plan every materialization target and capture its preimage: prompt context,
   runtime generations, project/global/hybrid compiled and script launchers,
   environment files, adapters, markers, ledgers, stale removals, and consumer
   state.
9. Acquire the manager-home mutation lock. Recover interrupted journals,
   revalidate closure generations, raw source, cache evidence, and target
   preimages, then atomically publish verified cache misses through the native
   backend (`builds/cache_posix.py` or `builds/cache_windows.py`).
10. Commit the complete project transaction (`transactions.py`). The consumer
    target is committed after its providers. A failure rolls every target back
    in reverse order while the lock is held; the former live installation is
    preserved. A safely published but unreferenced immutable cache entry may
    remain for later GC.
11. Run locked fail-safe garbage collection (`gc.py`). Hybrid materialization
    for a targeted project participates in the same project transaction.

Content-hashed installs and pinned commit references preserve reproducibility
across machines. Stage 3 resolves branch names and mutable tags to exact
immutable commit hashes before the installer takes a raw snapshot. Following
materialization, the installer records a SHA-256 digest of the installed file
tree in the marker to detect local workspace modifications.

Stage 5 evaluates every package against static security rules through an audit
gate before materialization. Skill packages can request unsafe MCP servers,
declare broad system permissions, or specify external dependencies. The audit
gate rejects non-compliant closures before the pipeline copies files or launches
compilers.

Fail-closed installs enforce security guarantees across every execution stage.
Fallback mechanisms and permissive error handling would allow unverified skills
to materialize when checks fail. The pipeline terminates immediately whenever a
driver, signature, audit policy, or platform control check fails.

Global installation follows the same ordering in `global_install.py` and uses
one all-or-rollback transaction for its contexts, marker-only nodes, runtimes,
compiled/script launchers, safe user-bin forwarders, environment, adapters,
ledgers, and stale entries. Project, hybrid, and global activation shadow in
that order.

Dry-run diverges after stage 6. It may validate and hash the frozen snapshot,
establish the toolchain identity, derive the native target and cache key, and
inspect protected cache entries. It returns before `go list`, `go build`, any
compiler or linker, a persistent cache/snapshot, a mutation lock or journal,
and all materialization. Stable build results are `cache-hit`,
`would-preflight-and-build`, `would-rebuild-untrusted-cache`, `corrupt`, and
`unsupported`.

`csk status` independently rederives schema-6 build inputs and compares the
raw snapshot, target, toolchain, policy, key, protected receipt/artifact,
marker v2, and managed launcher. `--json` exposes stable build rows and
separate result-only capability evidence; `--check` returns 1 for any
non-current skill or build. Status is read-only. `csk status --attest`
additionally re-checks installed skills against registries (`attest.py`), and
`csk audit` runs the audit stage standalone.

## Module map

| Module | Responsibility |
|---|---|
| `cli.py` | Argument parsing and command dispatch. |
| `config.py` | Machine config and the enforced system-config layer: `skills_root`, default agents, adapter mode, audit settings, `allowed_sources`, `audit_registries`. |
| `manifest.py` | `Skillfile.json` parsing and editing. |
| `skillspec.py` | `agent-skill.json` parsing: commands, runtime/build roots, capabilities, dependencies, requirements, and the closed schema-6 build shape (schemas 1 through 6). |
| `closure.py` | Transitive requirement resolution, unification, cycle detection, activation edges, topological order. |
| `source_identity.py` | Canonical `host/path` identity for git URLs and allowlist matching. |
| `mcp_configs.py` | Read-only resolution of declared MCP server dependencies against agent configuration surfaces, with static availability probes: PATH resolution for stdio commands, disabled-server filtering, and trust-gating hints for project-only declarations. |
| `hybrid.py` | Hybrid-scope manifest and per-project activation targeting. |
| `dev_substitutions.py` | `Skillfile.dev.json` parsing for local provider substitution. |
| `git_ops.py` | Hardened git operations: clone with a protocol allowlist, ref resolution, archive extraction with path checks. |
| `snapshot.py` | Content-addressed raw snapshot cache of skill commits. |
| `builds/source.py` | Link-safe frozen raw snapshots and `curator-build-source-v1` identity. |
| `builds/toolchain.py` | Operator PATH capture, Go bootstrap, native-target derivation, complete `GOROOT` fingerprint, and trusted-family enforcement. |
| `builds/metadata.py` | Portable logical input, `manager-worker-v1` policy, CCJ-1 cache key, canonical receipt, and artifact metadata. |
| `builds/planner.py` | Provider-first planning, generation checks, dry-run records, and read-only cache outcomes. |
| `builds/go_v1.py` | Closed manager/worker execution, fixed Go graph validation/build, bounds, native-control probes, capability evidence, artifact verification, and teardown. |
| `builds/cache.py` | Platform-neutral protected-cache interface and stable hit/miss/corrupt/untrusted outcomes. |
| `builds/cache_posix.py`, `builds/cache_windows.py` | Native ownership/permission or DACL boundaries, immutable publication, quarantine, inspection, and GC. |
| `builds/currentness.py` | Read-only classification of active and recorded compiled commands. |
| `whitelist.py` | Prompt-context copy rules: which skill files reach the agent. |
| `locale.py` | Locale rendering for localized skill metadata. |
| `shims.py` | Script runtime population plus direct protected-artifact launchers for compiled commands. |
| `installer.py` | Project/hybrid planning, private compilation, cache publication, and transactional materialization. |
| `global_install.py`, `global_bins.py` | User-wide skill installs and global command shims. |
| `transactions.py` | Journaled multi-target commit, recovery, target-preimage guards, and reverse rollback. |
| `install_marker.py` | Marker v1/v2 parsing and canonical installed build records. |
| `adapters.py` | Per-agent adapter directories with managed-entry tracking; native-discovery agents (OpenCode, Windsurf) read the canonical directory and skip project mirrors. |
| `status.py` | Manifest versus installed state reporting. |
| `attest.py` | Re-check installed markers against trusted audit registries. |
| `audit_registry.py` | Audit registry client: record verification, deny-wins federation, snapshot checks, lookup cache. |
| `_ed25519.py` | Vendored standard-library Ed25519 signature verification. |
| `gc.py` | Locked fail-safe marking and collection for runtime, snapshots, and protected compiled cache. |
| `consumers.py` | Registry of checkouts that reference the shared stores. |
| `locking.py` | Ordered project/global, per-build-key, and manager-home locks with stale-lock recovery. |
| `hashing.py` | Installed-tree content hashing; separate from raw build-source identity. |
| `identifiers.py` | Safe identifier rules for names that become filesystem paths. |
| `audit/` | Security audit: static detectors, capability checks, policy decisions, trust store, extraction backends. |

## Storage layout

Machine level, under `~/.cocoaskills/`:

```text
config.json                  machine config
cache/<source>/<commit>/     content snapshots of skill commits
runtime/<skill>/<commit>/    runtime files and command entrypoints
builds/go-v1/<hex-key>/      protected immutable compiled receipt and artifact
.builds-staging/             manager-owned cache publication staging
.builds-quarantine/          retired protected entries awaiting safe removal
global/                      user-wide skills, bin, and manifests
hybrid/                      machine-stored skills activated per project
dev/<skill>/                 clones created for git dev substitutions
cache/registry/              disposable audit registry response cache
state/registry/              durable registry rollback and equivocation state
consumers.json               checkouts referencing the shared stores
```

Project level, generated and gitignored:

```text
.agents/skills/<name>/       prompt context plus .csk-install.json marker
.agents/bin/<command>        script or direct protected-artifact launcher
.claude/skills/, .codex/skills/, .cursor/rules/, .gemini/skills/
                             per-agent adapter mirrors
```

OpenCode and Windsurf discover `.agents/skills/` natively and get no mirror
directory; for global installs they are served through `~/.agents/skills/`.

The csk-specific compiled entry is:

```text
<csk-home>/builds/go-v1/<64-lowercase-hex-cache-key>/
  csk-receipt.ccj.json
  bin/<command>              Unix
  bin/<command>.exe          Windows
```

That physical layout is deliberately not portable protocol identity. Portable
state is the complete logical input, cache key, exact canonical receipt bytes,
artifact-relative path, and artifact bytes/hash/size. Physical manager-home
paths, cache/staging/quarantine names, receipt filename, lock names, and native
storage backend remain csk implementation details.

## Schema-6 build contract

Schema 7 composes this same fixed `go-v1` compiler session with an independently
admitted external Git snapshot. Acquisition, raw-object proof, whole-snapshot
validation, external audit, protected-cache lookup, compilation, receipt-v2
publication, and marker-v3 consumer commit occur in that order. The package
cannot insert a hook or script between them. See
[External build repositories](docs/external-build-repositories.md). The native
external-build boundary is qualified only for macOS and Windows; this document
makes no Linux support claim.

This architecture boundary follows the accepted
[rc.5 protocol core](https://github.com/relux-works/curator-spec/blob/v1.0.0-rc.5/protocol/core.md).
Later protocol revisions are outside this document's scope.

### Identity and protected cache

The `curator-build-source-v1` identity hashes the fully validated raw source
snapshot, including build roots and the package-provided descriptor/marker
bytes present in that snapshot. It is different from installed-tree
`content_sha256`, which hashes selected installed content and excludes the
installed marker itself. Neither can substitute for the other.

The complete Go logical input also includes the build root, command, source
directory, native target, full `curator-go-toolchain-v1` identity, fixed Go
policy, and `manager-worker-v1`. Its CCJ-1 digest is the cache key. A receipt
retains that entire input plus the manager-derived artifact-relative path,
SHA-256, and byte length. Its receipt hash is over the exact stored canonical
bytes.

Receipt consistency is not protected-state provenance. A cache reader must
independently rederive the expected input and key and verify a manager-created
ownership, permission/DACL, containment, regular-file, single-link, and
no-follow boundary before it trusts matching receipt and artifact bytes.
Untrusted state is a miss: real install rebuilds into fresh protected state,
dry-run reports `would-rebuild-untrusted-cache`, and status is non-current.
Neither a marker nor matching hashes authenticate an unprotected cache.

Protected build cache entries isolate compiled binaries from local file
modifications. User-writable storage locations allow local scripts to overwrite
binary entrypoints. The manager verifies file ownership, POSIX permissions, and
Windows DACLs before it adopts cached bytes into an active installation.

### Fixed Go and process graph

The protocol floor is Go 1.23, but the manager accepts only its
operator-trusted handoff family. The current csk allowlist is family 1.25. It
builds the native host target only, turns telemetry off into private state, and
uses vendor mode with `GOTOOLCHAIN=local`, `GOENV=off`, `GOWORK=off`,
`CGO_ENABLED=0`, `GO_EXTLINK_ENABLED=0`, PGO off, the gc compiler, and internal
linking without libgcc. Workspaces, cross-compilation, cgo, generators, tests,
plugins, overlays, package-controlled assembly/host objects, external linking,
module downloads, arbitrary arguments, flags, environment, tools, hooks, and
post-build actions are closed out. Another driver fails closed.

`manager-worker-v1` is a normative cache, receipt, marker-currentness, and
claim input, not an operator choice. The four-node graph is fixed:

```text
manager parent
  -> identity-verified manager-owned worker
       -> fingerprinted <GOROOT>/bin/go
            -> fingerprinted regular children below <GOROOT>/pkg/tool/
```

The manager verifies the worker before launch, receives its nonce-bound
identity proof, validates the complete output of one fixed `go list`, grants
one authenticated build permit, and accepts one fixed `go build`. It freezes
the source, fixes the toolchain identity, rechecks source/toolchain/worker
identities after the children exit, and terminates and joins the whole worker
domain before return. Any extra state-machine message or process request tears
the domain down. The manager never runs the artifact during validation,
installation, status, repair, rollback, or GC.

One operation carries manager-owned bounds of 120 seconds, 8 MiB combined
output, 128 MiB artifact, 512 MiB per file, 1 GiB private storage, 2 GiB
memory, and 64 processes. Native facilities determine which file/memory/process
bounds are applied; they are not a hard aggregate descendant guarantee.

Manager-owned execution isolates binary compilation from package-supplied
instructions. In a conventional build, third-party build scripts or custom
Makefiles execute arbitrary host commands. CocoaSkills controls the worker
process hierarchy, toolchain environment, and compiler flags directly without
executing package build hooks.

### Platform controls and evidence

Source-aware compilation is supported on macOS and Windows. Linux is an
explicitly deferred build platform owned by `TASK-260728-1skseh` and
`TASK-260728-1e6811`; `go-v1` fails closed elsewhere before a worker or Go
child. This does not remove Linux support for the script/system portions of
CocoaSkills.

For each source-aware execution operation, csk produces one closed
`capability-evidence-v1` record containing exactly one entry per
`rc5-native-control-inventory-v1` control:

| Control | macOS | Windows |
|---|---|---|
| `descendant-domain-termination` | available: process-group/session teardown | available: Job Object kill-on-close |
| `active-process-count-limit` | unavailable | available: Job Object limit |
| `aggregate-memory-limit` | unavailable | available: Job Object process/job memory limits |
| `per-file-size-limit` | available: `RLIMIT_FSIZE` | unavailable |
| `inherited-handle-restriction` | available: close-on-exec plus descriptor release | available: explicit handle inheritance list |

Each entry records its name, availability, applied/unavailable status, and
`pre-worker-launch` probe timing. An inventory control marked unavailable does
not reject the build. Failure to apply a mandatory portable control does reject
with `build_execution_control_unavailable` before the worker and publishes
nothing. Evidence is result-only and never enters cache identity, receipt,
marker, claim, or currentness.

The portable policy does not provide or claim
`total-network-denial`, `read-only-source-and-toolchain`,
`private-build-root-only-writes`,
`hard-aggregate-descendant-resource-bounds`,
`exact-executable-allowlisting`, or
`fail-closed-capability-preflight`. These are six deferred hardened guarantees,
not alternate names for the portable mechanisms csk does enforce.

### Status, repair, GC, and activation

Marker v2 records the raw build-source identity plus sorted schema-6 build
roots and commands with their driver, cache key, receipt hash, artifact hash,
and artifact path. The execution policy is already transitively bound through
the input/key/receipt and is not a package-settable marker field. Currentness
rederives every build surface rather than trusting the marker.

Ordinary reinstall is repair. On a supported platform, missing, corrupt,
wrong-input, legacy/unsupported-identity, or untrusted candidates are rebuilt
from a freshly frozen and revalidated source; csk never adopts candidate
bytes. A genuinely unsupported platform remains fail-closed. Project, global,
and targeted hybrid repair use the same transaction/rollback rules as initial
installation.

`csk gc` holds the manager-home lock. It marks keys from valid project,
global, hybrid, and registered-consumer marker v2 state plus active transaction
journals, retains everything when a mark source or protected boundary is
uncertain, and removes only validated unreferenced protected entries older than
24 hours. A receipt alone is not a liveness root.

Compiled artifacts stay in the protected cache. Project and targeted hybrid
launchers live in `<project>/.agents/bin`; global launchers live in
`<csk-home>/global/bin`, with safe user-bin forwarders where available. Unix
launchers use `/bin/sh` to `exec` the absolute artifact and pass `"$@"`.
Windows `.cmd` launchers call the quoted absolute `.exe`, pass `%*`, and
preserve its exit status. Agent-facing resolution is project launcher, global
launcher, then validated bare command; activation profiles are optional.

## Security model

The CocoaSkills security model treats skill repositories and network remotes as
untrusted third-party input. Refer to [SECURITY.md](SECURITY.md) for vulnerability
disclosure procedures and platform hardening checklists.

Untrusted skill repositories can request dangerous MCP capabilities, expose
invalid manifests, or declare unsafe tool dependencies. The audit gate evaluates
the transitive dependency graph against static security policies before writing
files to disk or launching compilers. Source allowlists restrict git clone
operations to approved host identity paths.

Remote git branches and tags can be reassigned to malicious commits after initial
inspection. Stage 3 ref resolution pins git references to explicit commit hashes
and stores raw content in content-addressed snapshot paths. Following materialization,
content-hashed install trees record file digests in `.csk-install.json` markers
to detect workspace drift.

Skill repositories can include extraneous documentation, test suites, or hidden
files designed to manipulate agent prompt behavior. The whitelist stripped layout
filters non-essential repository assets during materialization. Only prompt-facing
instructions and references reach the agent context window.

Untrusted packages can attempt command execution during build or runtime phases.
Manager-owned execution enforces a closed Go compilation pipeline with fixed
process bounds, isolated toolchains, and a protected build cache. Package descriptors
cannot execute arbitrary build scripts or inject toolchain options.

### Enforced boundaries

CocoaSkills enforces specific security boundaries across installation,
compilation, and materialization:

- Names that become filesystem paths pass a safe identifier rule
  (`identifiers.py`), so third-party manifests cannot write outside their
  designated directories.
- `git clone` restricts transports through `GIT_ALLOW_PROTOCOL` and separates
  URLs from options, which blocks remote-helper URLs that execute commands.
- Archive extraction rejects path traversal and links.
- Manifests declare and never execute: install hooks, checks, and version
  probes are rejected at parse time.
- The source allowlist (`allowed_sources`) gates every clone by canonical
  `host/path` identity before the first network operation.
- The prompt-context whitelist keeps repository metadata, tests, and build
  files out of the agent window.
- Build-only bytes remain compiler input: they cannot select the worker,
  toolchain program, arguments, environment, working directory, controls,
  limits, output, cache metadata, publication, or activation behavior.
- The protected compiled cache is addressed only by independently derived
  logical inputs. Cache and transaction code refuses ambiguous ownership,
  links, target changes, non-canonical receipts, and inconsistent artifacts;
  self-consistent unprotected bytes are never adopted as provenance.
- The source-aware graph and portable controls are manager-enforced mechanisms,
  with the six hardened guarantees above explicitly outside the provided
  boundary.
- The audit subsystem evaluates every node of the install closure; the
  install decision stays deterministic inside CocoaSkills
  ([RFC 0005](docs/audit-design.md), [RFC 0006](docs/v0.8-design.md)).
- Audit registry records are verified against out-of-band pinned Ed25519 keys
  before they are trusted, federation is deny-wins, and an enforced system
  config layer with locked keys keeps a developer from widening the trust
  boundary ([RFC 0008](docs/v0.11-design.md)).

## Design history

| Document | Scope |
|---|---|
| [docs/mvp-design.md](docs/mvp-design.md) | v0.1 contract: manifests, refs, install pipeline, locking, adapters. |
| [docs/v0.3-design.md](docs/v0.3-design.md) | RFC 0001: `csk init`, explicit `--all`, current-project installs. |
| [docs/v0.4-design.md](docs/v0.4-design.md) | RFC 0002: auto-clone of declared `git` sources. |
| [docs/v0.5-design.md](docs/v0.5-design.md) | RFC 0003: `runtime_roots` for multi-file command runtimes. |
| [docs/v0.6-design.md](docs/v0.6-design.md) | RFC 0004: global skills. |
| [docs/audit-design.md](docs/audit-design.md) | RFC 0005: capability manifests and the deterministic audit gate. |
| [docs/v0.8-design.md](docs/v0.8-design.md) | RFC 0006: audit LLM backends. |
| [docs/v0.9-design.md](docs/v0.9-design.md) | RFC 0007: skill dependencies, activation modes, dev substitutions, source allowlist. |
| [docs/v0.11-design.md](docs/v0.11-design.md) | RFC 0008: audit registry, chain of trust, federation, enforced system config. |

## Testing

Tests live in `tests/` and run with plain `pytest`. Fixtures in
`tests/conftest.py` build throwaway git repositories for skills and projects,
so end-to-end install tests exercise the real pipeline against temporary
stores. Platform-specific expectations, such as symlink shims, carry explicit
platform markers, and script command fixtures ship both `unix_path` and
`win_path` entrypoints so the generic suite runs on Linux, macOS, and Windows.
Source-aware `go-v1` behavior has macOS and Windows lanes; tests on other hosts
assert the intentional fail-closed platform result. Linux source-aware support
remains with `TASK-260728-1skseh` and `TASK-260728-1e6811`.
