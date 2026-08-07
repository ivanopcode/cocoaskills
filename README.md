# CocoaSkills

[![PyPI](https://img.shields.io/pypi/v/cocoaskills.svg)](https://pypi.org/project/cocoaskills/)
[![Python versions](https://img.shields.io/pypi/pyversions/cocoaskills.svg)](https://pypi.org/project/cocoaskills/)
[![License](https://img.shields.io/pypi/l/cocoaskills.svg)](https://github.com/ivanopcode/cocoaskills/blob/main/LICENSE)
[![CI](https://github.com/ivanopcode/cocoaskills/actions/workflows/ci.yml/badge.svg)](https://github.com/ivanopcode/cocoaskills/actions/workflows/ci.yml)

Translations: [Русский](README.ru.md). English is the source of truth.

`csk` is a local skill manager for AI agent skills. It installs reusable skill
packages from git repositories into your project repositories with
reproducible, content-hashed installs, skill-to-skill dependencies, and
multi-agent support across six environments: Claude Code, Codex CLI, Cursor,
and Gemini via adapter mirrors, plus OpenCode and Windsurf, which discover the
canonical `.agents/skills/` directory natively.

It is an independent Python implementation of the open
[Curator Protocol](https://github.com/relux-works/curator-spec). The `csk`
executable, package name, and existing state directories remain
implementation-specific compatibility names; portable manifest and marker
names follow the shared protocol.

## Why

Managing agent skills across many projects by hand falls apart fast: drift
between machines, no version pinning, README files and tests leaking into the
agent context, no cleanup when a skill is removed.

CocoaSkills makes per-project skill installation declarative and reproducible:

- One `Skillfile.json` per project, committed to version control.
- Pinned git refs (tag / branch / revision) and content-hashed installs.
- Skill-to-skill dependencies: a skill declares the skills it builds on, and
  `csk install` resolves the transitive closure with exact refs and activation
  modes.
- A whitelist-based stripped layout: README, tests, build files, and other
  non-skill content stay out of the agent's context.
- One canonical location (`.agents/skills/`) with per-agent adapter symlinks
  or copies into `.claude/skills/`, `.codex/skills/`, `.cursor/rules/`,
  `.gemini/skills/`. OpenCode and Windsurf read `.agents/skills/` natively,
  so they need no mirror.
- Skill-provided command shims exposed via a project-local `.agents/bin/`
  directory on `PATH`.
- Optional global skills installed once under `~/.cocoaskills/global/` and
  exposed to supported agents outside any project checkout.

## Install

Pick whichever fits your machine. `pipx` is the recommended path on every
platform.

### pipx (recommended)

```bash
pipx install cocoaskills
```

### uv tool

```bash
uv tool install cocoaskills
```

### Homebrew (macOS, Linux)

```bash
brew tap ivanopcode/csk
brew install cocoaskills
```

### mise

```bash
mise use -g pipx:cocoaskills@latest
```

### Convenience install script

```bash
curl -fsSL https://cocoaskills.org/install.sh | sh
```

The script detects Python, prefers `pipx` or `uv tool`, and falls back to
`pip install --user`. Read it before piping if you do not trust the network.

### Plain pip

```bash
python -m pip install --user cocoaskills
```

## Quick start

1. Pick or create a directory for skill git repositories. Example:
   `~/agents/skills/`. Existing local skill repositories are read from this
   directory; missing repositories can be cloned automatically when a skill
   declaration provides `git`.

2. Bootstrap the global config:

   ```bash
   csk bootstrap
   ```

   This writes `~/.cocoaskills/config.json` with your `skills_root`, preferred
   locale, and default agents.

   Repository automation can make this step idempotent without overwriting a
   developer's existing machine config:

   ```bash
   csk bootstrap --if-missing --non-interactive --skills-root ~/.cocoaskills/skills
   csk upgrade .
   ```

3. Initialize CocoaSkills in each project:

   ```bash
   cd /path/to/project
   csk init
   ```

   This creates `Skillfile.json` and adds the CocoaSkills generated paths to
   `.gitignore`.

4. Declare which skills you want:

   ```json
   {
     "schema_version": 1,
     "project": { "alias": "demo-ios" },
     "agents": ["claude_code", "codex_cli", "cursor"],
     "locale": "en",
     "skills": [
       {
         "name": "skill-tracker",
         "git": "git@gitlab.example.com:skills/skill-tracker.git",
         "tag": "v1.0.0"
       },
       {
         "name": "skill-metrics",
         "source": "internal/skill-metrics",
         "branch": "main"
       }
     ]
   }
   ```

   The optional `locale` field only affects skills that ship localized
   metadata (`locales/metadata.json` plus `.skill_triggers/<locale>.md`).
   Skills without localization files install unchanged.

5. Run `csk install` inside the checkout.

For multi-project sync, explicitly register projects with `csk project add` and
run `csk install --all` or `csk upgrade --all`.

## Skill dependencies

Since v0.9.0 a skill can require other skills ([RFC 0007](docs/v0.9-design.md)).
A requirement lives in `agent-skill.json` schema v4 under `dependencies.skills`,
is self-contained (git URL plus an exact `tag` or `revision` ref), and carries
an activation mode:

```json
{
  "schema_version": 4,
  "runtime_roots": ["scripts"],
  "capabilities": { "exec": ["trk", "git"], "network": "none" },
  "commands": {
    "report": { "type": "script", "unix_path": "scripts/report" }
  },
  "dependencies": {
    "skills": {
      "skill-tracker": {
        "git": "git@gitlab.example.com:skills/skill-tracker.git",
        "ref": { "kind": "tag", "value": "v1.4.2" },
        "mode": "runtime",
        "commands": ["trk"]
      }
    }
  }
}
```

Activation modes select what a provider contributes to the consumer:

- `full` (default) activates the provider prompt context and all exported
  commands.
- `runtime` activates commands only; the optional `commands` list narrows the
  activation to the named exports.
- `context` activates the provider prompt context only.

`csk install` resolves the transitive closure: providers are fetched, unified
to one commit and one canonical source per name, ordered before their
consumers, and audited together. Version conflicts, source conflicts, and
dependency cycles fail with the full requirement chains.

A workflow ships as a skill that declares requirements and exports no
commands; a consumer installs the whole composition with a single
`Skillfile.json` entry.

Two supporting mechanisms:

- `Skillfile.dev.json` substitutes providers locally during development: a
  checkout path or a git ref, branches included. The file stays out of version
  control, installs print every active substitution, and strict audit refuses
  substituted installs.
- `allowed_sources` in `~/.cocoaskills/config.json` lists canonical
  `host/path` prefixes and gates every clone. SSH and HTTPS URLs of one
  repository normalize to one identity.

## Global skills

Global skills are user-wide baseline skills. They are installed under
`~/.cocoaskills/global/` and linked into user-level agent directories such as
`~/.claude/skills/` and `~/.codex/skills/`. When OpenCode or Windsurf is among
the target agents, global skills are also linked into `~/.agents/skills/`,
which both discover natively.

```bash
csk global init
csk global add skill-metrics \
  --git git@gitlab.example.com:skills/skill-metrics.git \
  --tag v1.0.0
csk global install
```

Global commands are exposed through `~/.cocoaskills/global/bin`. During
`csk global install`, CocoaSkills also publishes forwarding shims into a safe
user bin that is already on `PATH`, such as `~/.local/bin`, so global commands
work from any directory without per-project activation.

Agent execution never depends on shell profile activation. Installed skills
resolve project shims explicitly from `<repo>/.agents/bin/<command>`
(`<command>.cmd` on Windows), then global shims from
`<csk-home>/global/bin`, and only then a validated bare command. This contract
works unchanged from zsh, bash, PowerShell, Git Bash, CI, and agent processes
that were not launched from an initialized interactive shell.

Generated runtime shims prepend only the paths needed by the installed skill:
the current project/global shim directory, the Python environment running
`csk`, and directories of declared system command dependencies. The inherited
`PATH` remains available, but skill-to-skill calls and Python launchers do not
depend on a shell hook.

On Windows, PowerShell 5.1, PowerShell 7, and `cmd.exe` can all execute the
generated `.cmd` shims directly. Optional directory-change activation is
available for PowerShell and Git Bash; `cmd.exe` has no profile hook and does
not need one for agent execution.

If no safe user bin is available, global install still succeeds and prints a
warning. Agents continue to use the explicit global path. Humans can set
`CSK_GLOBAL_USER_BIN` to a writable PATH directory or invoke the generated shim
explicitly.

Shell activation is optional human convenience for bare project commands and
project-over-global command shadowing. `auto` detects zsh or bash from `SHELL`,
PowerShell on Windows, and Git Bash on Windows before the platform fallback:

```bash
csk shell-init --install
# Or choose explicitly: zsh, bash, powershell
```

The command atomically caches the hook and prints the correctly quoted source
line for `.zshrc`, `.bashrc`, or the PowerShell profile. Never put
`eval "$(csk shell-init ...)"` in a profile: that starts Python for every new
shell. Run `--install` again after upgrading CocoaSkills so the optional cached
hook receives fixes.

Set `CSK_AUTO_ENV=0` before sourcing the optional hook to disable project
directory scanning on an unhealthy or blocking filesystem. Global commands
remain active; project commands remain available by explicit `.agents/bin`
path. Global skills never replace committed project `Skillfile.json`
declarations.

## Hybrid skills

Hybrid skills are stored once per machine and activated for selected projects
only, with nothing committed to the target repositories. The declaration
lives in `~/.cocoaskills/hybrid/Skillfile.json` and names its targets by
project alias, absolute path, or path glob:

```bash
csk hybrid add skill-conventions \
  --git git@gitlab.example.com:skills/skill-conventions.git \
  --tag v1.0.0 \
  --target demo-ios \
  --target "/Users/me/work/*-service"
csk hybrid list
```

`csk install` in a targeted project picks applicable hybrid skills up
automatically: the prompt context materializes once under
`~/.cocoaskills/hybrid/skills/` and reaches the project through managed
adapter links, command shims land in the project `.agents/bin`, and the
dependency closure and audit gates apply exactly as for project skills.
Shadowing order is project, then hybrid, then global. This scope fits skills
a platform team rolls out to selected repositories when committing anything
to those repositories is undesirable.

## Skill command manifests

Skills declare commands, capabilities, and dependencies through
`agent-skill.json`. Schema v2 supports multi-file runtimes: `runtime_roots` are
copied into `~/.cocoaskills/runtime/<skill>/<commit>/` and excluded from agent
prompt context. Schema v3 adds the `capabilities` envelope used by `csk audit`
and strict install gates. Schema v4 adds skill requirements (see
[Skill dependencies](#skill-dependencies)), schema v5 adds MCP server
requirements, and schema v6 adds compiled commands and context-excluded
`build_roots`.

Existing packages named `csk-skill.json` remain readable. New and updated
packages should write only `agent-skill.json`. During a staged rename, both
files may coexist only when their decoded JSON values are equal; conflicting
files fail installation instead of selecting one silently.

```json
{
  "schema_version": 6,
  "runtime_roots": ["scripts"],
  "build_roots": ["build"],
  "capabilities": {
    "network": "none",
    "filesystem": "repo",
    "exec": ["git"],
    "secrets": "none",
    "env_read": [],
    "prompt_scope": "Inspect a repository and produce local reports."
  },
  "commands": {
    "format-report": {
      "type": "script",
      "unix_path": "scripts/format-report",
      "win_path": "scripts/format-report.cmd"
    },
    "repo-report": {
      "type": "build",
      "driver": "go-v1",
      "source_dir": "build/cmd/repo-report"
    },
    "git": {
      "type": "system",
      "command": "git",
      "hint": "Install Git through project bootstrap tooling"
    }
  },
  "dependencies": {
    "commands": {},
    "mcp_servers": {},
    "skills": {}
  }
}
```

`system` commands are only checked with `shutil.which`; CocoaSkills never
installs system tools, and manifests carry no install hooks or version probes.

## Compiled commands (schema 6)

This section describes the accepted schema-6 `go-v1` boundary in the
[rc.5 protocol core](https://github.com/relux-works/curator-spec/blob/v1.0.0-rc.5/protocol/core.md)
and the corresponding landed csk behavior. Later protocol revisions are out of
scope.

Schema 7 adds locked external Git build repositories through
`go-repository-v1`. See
[External build repositories](docs/external-build-repositories.md) for the
complete authoring, audit, cache, activation, repair, and qualification
contract. External builds are supported on macOS and Windows only; Linux
support is not claimed.

The example above is a complete mixed command manifest: `format-report` is a
script runtime, `repo-report` is built from Go source, and `git` is an
operator-provided system requirement in the accepted compatibility location.
New skills put consumed system tools under `dependencies.commands`; csk only
checks their presence and does not create a system-command launcher. The source
tree has this shape:

```text
agent-skill.json
scripts/format-report
scripts/format-report.cmd
build/go.mod
build/cmd/repo-report/main.go
build/vendor/                    checked-in modules when non-standard packages are imported
```

Every `build_roots` entry is a real, link-free, portable relative directory.
Build roots are unique and disjoint, cannot overlap `runtime_roots`, and each
must be used by at least one build command. A `source_dir` is a real, link-free
directory below exactly one build root. That build root contains `go.mod`
directly and must be the nearest module root: an intervening `go.mod` is an
error. Build roots stay in the validated raw source snapshot but are excluded
from installed prompt context and script runtime storage.

A build command has exactly three fields:

```json
{"type":"build","driver":"go-v1","source_dir":"build/cmd/repo-report"}
```

The package cannot select an output path, program, argument, environment
value, build tag, flag, toolchain, target, build script, hook, plugin,
generator, or post-build action. `go-v1` is the only accepted driver; another
driver fails closed without a fallback. The only output is the manager-derived
`bin/<command>` on Unix or `bin/<command>.exe` on Windows.
Failing closed prevents package data from selecting an unimplemented execution
contract or aliasing artifacts built under different cache semantics.

### Fixed `go-v1` contract

The protocol sets Go 1.23 as the minimum family a manager may support. It also
requires a manager to accept only an operator-trusted family for which it has
handoff evidence. The current CocoaSkills implementation accepts Go family
1.25 only. Thus Go 1.23 is the protocol floor, not a claim that this `csk`
accepts every Go family from 1.23 onward.

The selected Go installation must be a fingerprintable, operator-provided
native toolchain. Hashing the complete `GOROOT` is bounded by a deadline of 600
seconds per pass, which operators raise up to 3600 seconds with
`CSK_GO_FINGERPRINT_TIMEOUT` when a cold Go directory reads slowly — typically
on Windows, behind on-access antivirus. Exceeding the deadline refuses the
toolchain with `go-v1 toolchain_timeout` and names that variable in the
reported failure; raising the deadline never admits a toolchain that would
otherwise be refused. CocoaSkills builds exactly one `package main` executable
for the host `GOOS` and `GOARCH`. It switches Go telemetry off, uses private Go
configuration/cache/temporary roots, fixes `GOTOOLCHAIN=local`, `GOENV=off`,
`GOWORK=off`, `CGO_ENABLED=0`, and `GO_EXTLINK_ENABLED=0`, and runs only these
source-aware shapes from the declared `source_dir`:

```text
go list  -mod=vendor -deps -json -buildvcs=false -compiler=gc -pgo=off .
go build -mod=vendor -trimpath -buildvcs=false -buildmode=exe -compiler=gc \
         -pgo=off -ldflags="-linkmode=internal -libgcc=none" -o <private-output> .
```

All non-standard packages must resolve from checked-in vendor data; dependency
downloads and other build-time network access are disabled. Package validation
rejects workspaces and toolchain switching, cross-compilation, cgo, PGO,
generators, tests, plugins, overlays, package-selected assembly or host object
files, external linking, and libgcc fallback. The complete package graph must
contain exactly one non-test root `package main`; standard-library inputs must
come from the fingerprinted `GOROOT`, and every other compiler input must stay
inside the declared build root.

Manager-selected bounds for one operation are 120 seconds wall time, 8 MiB of
combined output, a 128 MiB artifact, 512 MiB per file, 1 GiB of private build
storage, 2 GiB of memory, and 64 active processes. The per-file, memory, and
process bounds are applied only where the native inventory below marks the
corresponding facility available. These bounds do not claim the deferred hard
aggregate descendant guarantee described under [Execution controls](#execution-controls).

Source-aware `go-v1` is supported on macOS and Windows. Other hosts fail closed
before a worker or Go child starts. Linux support is explicitly deferred to
`TASK-260728-1skseh` and `TASK-260728-1e6811`; generic script/system skills and
the rest of CocoaSkills are not reclassified by this source-aware build limit.

### Portable execution boundary

`manager-worker-v1` is the mandatory execution-policy identity. It is a
normative cache, receipt, marker-currentness, and claim input; it is not an
option, host label, operator preference, or package-visible setting. Different
execution-policy identities derive different cache keys.

The process graph is fixed to four nodes:

```text
CocoaSkills manager parent
  -> identity-verified manager-owned worker
       -> fingerprinted <GOROOT>/bin/go
            -> fingerprinted regular children below <GOROOT>/pkg/tool/
```

The hidden worker is an exact manager re-execution, never a manifest-selected
program. The manager verifies its identity before launch; the worker proves
that identity against a fresh session nonce. One session may run exactly one
fixed `go list`, wait for the parent to validate the entire package graph, and
then run exactly one fixed `go build` after an authenticated permit. An extra
message, retry, process, download, generator, test, or run request tears down
the session without authorizing more compiler work.

The source snapshot stays frozen. Its integrity and the worker and complete Go
toolchain identities are reverified after execution. The entire worker domain
is terminated and joined before the operation returns. Only then may the
manager publish a bounded regular artifact. CocoaSkills never executes a newly
built artifact while validating, installing, reporting status, repairing,
rolling back, or collecting garbage. The artifact runs only later, when a user
or agent explicitly invokes its activated command shim.

### Execution controls

Each source-aware execution operation produces exactly one closed
`capability-evidence-v1` result with one entry for every control in
`rc5-native-control-inventory-v1`. Entries record `name`, `availability`,
`status`, and `probed_at: "pre-worker-launch"`; the record also carries its
record version, `manager-worker-v1`, and platform. `status` is `applied` for an
available control and `unavailable` for an unavailable one.

| Inventory control | macOS | Windows |
|---|---|---|
| `descendant-domain-termination` | available: process-group and session teardown | available: Job Object kill-on-close |
| `active-process-count-limit` | unavailable: no private aggregate domain | available: Job Object active-process limit |
| `aggregate-memory-limit` | unavailable: no private aggregate domain | available: Job Object process and job memory limits |
| `per-file-size-limit` | available: `RLIMIT_FSIZE` | unavailable: no private aggregate domain |
| `inherited-handle-restriction` | available: close-on-exec and explicit descriptor release | available: explicit handle inheritance list |

An inventory control marked unavailable does not reject a portable build. A
missing mandatory portable control does: the operation returns
`build_execution_control_unavailable` before the worker or Go starts and
publishes nothing. Capability evidence is result-only. It does not enter the
cache key, receipt, marker, claim, or currentness decision; `csk status`
reports it separately when compiled commands are present.

This portable policy does **not** provide or claim any of these separately
deferred hardened guarantees:

- `total-network-denial`;
- `read-only-source-and-toolchain`;
- `private-build-root-only-writes`;
- `hard-aggregate-descendant-resource-bounds`;
- `exact-executable-allowlisting`;
- `fail-closed-capability-preflight`.

The portable mechanisms above still fail closed when their own mandatory
checks cannot be applied; they are not kernel-enforced versions of those six
hardened guarantees.

### Cache, lifecycle, status, and activation

The logical cache identity includes the complete validated raw source,
declared build root/source directory/command, native target, fingerprinted Go
toolchain, fixed Go policy, and `manager-worker-v1`. Those logical inputs,
canonical receipt bytes, artifact-relative path, and artifact bytes/hash/size
form the portability boundary. CocoaSkills' physical manager-home layout is
implementation-specific:

```text
<csk-home>/builds/go-v1/<64-lowercase-hex-cache-key>/
  csk-receipt.ccj.json
  bin/<command>                Unix
  bin/<command>.exe            Windows
<csk-home>/.builds-staging/
<csk-home>/.builds-quarantine/
```

Do not confuse the installed-tree `content_sha256` with
`curator-build-source-v1`: the first hashes installed content (excluding its
marker), while the second identifies the fully validated raw snapshot and
therefore includes build-only source. Likewise, a receipt whose key, input,
artifact path, hash, and size agree is internally consistent, but that does
not prove protected-state provenance. Persistent reuse also requires the
manager-created ownership, permission/DACL, containment, regular-file, and
link-safety boundary. Receipt hashes are consistency/currentness identifiers,
not signatures, MACs, attestations, or provenance proofs.

Real project and global installs resolve providers before consumers and build
commands lexically within a provider. Validation, dependency closure, source
and audit gates, freezing, toolchain selection, and cache planning precede any
compiler. Cache misses compile in operation-private staging outside the
manager-home mutation lock. Under that lock CocoaSkills recovers interrupted
transactions, revalidates generations and target preimages, publishes an
immutable protected cache winner, and commits materialization atomically.
Project, global, and targeted hybrid surfaces are each all-or-rollback:
contexts, runtimes, compiled/script shims, adapters, environment files,
markers, stale removals, and consumer state either move together or the prior
installation is restored. A safely published but unreferenced immutable cache
entry may remain for later GC.

`csk install --dry-run`, `csk upgrade --dry-run`, and their global forms stop
before mutation and before `go list`, `go build`, a compiler, or a linker. They
may validate and hash the frozen source, establish the trusted toolchain
identity, and inspect the protected cache read-only. They create no persistent
cache, snapshot, mutation lock, or journal. Each build plan reports one of
`cache-hit`, `would-preflight-and-build`,
`would-rebuild-untrusted-cache`, `corrupt`, or `unsupported` together with the
build-source identity, cache key, native target, driver, command, build root,
and source directory.

`csk status --json` and `csk global status --json` report build rows with the
provider, command, label/detail, expected and recorded cache keys,
`manager-worker-v1`, and a separate capability-evidence result.
`--check` exits 1 when any skill or build is non-current. Currentness requires
the active descriptor, raw snapshot, build-source identity, toolchain, native
target, execution policy, cache key, protected receipt/artifact, marker, and
managed shim to agree. Status is read-only and never recreates missing state.
Stable build labels include `current`, `build-command-drift`,
`missing-build-marker`, `unsupported-build-driver`, `build-input-drift`,
`missing-build-artifact`, `corrupt-build-cache`, `untrusted-build-cache`,
`unsupported-build-platform`, `build-marker-drift`, `build-shim-drift`, and
`build-state-changed`.

Repair is ordinary reinstall: rerun `csk install` or `csk global install`.
On a supported platform, missing, corrupt, wrong-input,
legacy/unsupported-identity, or untrusted candidate state is rebuilt from a
freshly frozen and revalidated source into new protected state; csk does not
adopt or patch candidate bytes. A genuinely unsupported platform remains
fail-closed rather than being repaired locally. `csk gc` takes the manager-home
lock, marks schema-6 keys referenced by valid project/global/hybrid markers and
registered-consumer marker roots or live transaction journals, and removes
only protected, provably unreferenced entries older than 24 hours. Uncertain
marker, journal, boundary, or receipt state is retained with a warning rather
than guessed safe to delete.

Activation never copies the compiled artifact into script runtime storage. A
project or targeted hybrid install creates `<project>/.agents/bin/<command>`;
a global install creates `<csk-home>/global/bin/<command>` and, when safe, a
user-bin forwarder. On Unix the managed `/bin/sh` launcher directly `exec`s
the absolute protected-cache artifact and forwards `"$@"`. On Windows the
managed `<command>.cmd` directly calls the quoted absolute `.exe`, forwards
`%*`, and returns its exit status. Agent resolution remains project shim,
global shim, then a validated bare command, so shell-profile activation is not
required.

## Skill audit

`csk audit` runs security checks against the same committed skill snapshot that
`csk install` would use. Static detectors always run. Optional `command` and
`codex` backends extract additional structured findings; the install decision
stays deterministic inside CocoaSkills.

```bash
csk audit
csk audit . --json
csk audit --global
```

Install gates are opt-in per command or through config:

```bash
csk install --audit
csk install --audit strict
csk global install --audit
```

Advisory audit prints warnings and continues. Strict audit blocks findings at
or above the configured threshold. Schema v1/v2 skills declare no
capabilities; strict audit requires migrating them to schema v3 or newer, or
pinning the content hash through the trust workflow when that workflow is
enabled.

Backend safety rules:

- Local `command` backends receive raw skill files and are treated as trusted
  local tools.
- Local `codex` backends require `oss=true` and an explicit `local_provider`.
- Cloud backends require `audit.allow_cloud=true` and a public source policy
  match. File contents are redacted before they are sent to a cloud-capable
  backend.
- Unverifiable backend findings are shown in reports and never block strict
  installs.

## Audit registry

An audit registry serves signed statements that a skill, at a specific commit
and content hash, was audited or revoked ([RFC 0008](docs/v0.11-design.md)). A
machine pins the registries it trusts in `~/.cocoaskills/config.json`:

```json
{
  "audit_registries": [
    {
      "name": "internal",
      "url": "https://registry.example.com",
      "public_keys": ["ed25519:base64key..."]
    }
  ],
  "disable_builtin_registries": false
}
```

`csk install` resolves each skill against the trusted registries and verifies
every record against the pinned keys before trusting it. A verified revocation
in any trusted registry denies the install; a verified audit is recorded as an
attestation in the install marker. Registry lookups are advisory unless a skill
is revoked, and organizations pin only their internal registry with
`disable_builtin_registries`. Signature verification uses a standard-library
Ed25519 implementation, so the runtime keeps no third-party dependency.

Snapshot rollback and equivocation state is keyed by canonical registry URL
under the configuration home (`~/.cocoaskills/state/registry` by default),
outside the disposable response cache.
It survives signing-key rotation and is written atomically before a snapshot is
accepted. Back up this directory with the machine configuration; existing
corruption, deletion after prior use, or an unwritable state directory disables
the affected registry. A protected catalog distinguishes deletion from genuine
first use.
Record reads reject cursor cycles, oversized cursors, more than 10,000 records,
and responses larger than 16 MiB. Network retries have three total attempts and
finite deadlines. GET retries only network failures, `429`, and `503`; record
publication retries only the identical idempotent request. Redirects are
rejected.

For managed fleets, a system configuration at `/etc/cocoaskills/config.json`
(or `%ProgramData%\cocoaskills\config.json` on Windows) is read before the
user config. Keys it lists under `locked` cannot be overridden from the user
config, so registry trust, the source allowlist, and the audit policy can be
distributed through device management. Set `audit.registry_policy` to `strict`
to fail any install that is not audited by a trusted registry, and run
`csk status --attest` to re-check installed skills against the registries.
An auditor submits a signed record with
`csk audit --publish <record> --registry <url> --token <token>`. The production
service, including stable pagination, durable append, backup verification, and
air-gapped bundle import for closed networks, is
[Curator Skill Registry](https://github.com/relux-works/curator-skill-registry).

## CLI

| Command | Behavior |
|---|---|
| `csk bootstrap` | Create machine-level global config; interactive or scripted via `--skills-root`, `--default-agents`, `--non-interactive`, `--force`. `--if-missing` is an idempotent no-op when config already exists and is mutually exclusive with `--force`. |
| `csk init [path]` | Create project `Skillfile.json` and the managed `.gitignore` block. Supports `--alias`, `--agents`, and `--no-interactive` for scripted setup. |
| `csk install [target]` | Apply `Skillfile.json` using current git refs. Missing `git` URL sources are cloned into `skills_root`; existing local repositories are not fetched. No target means current project; `target` may be an alias, `.`, or a project path. `--dry-run` validates and plans compiled cache outcomes without persistent mutation or compiler work. |
| `csk install --audit [strict]` | Run the audit gate for this install only. Without `strict`, audit is advisory and does not change config. |
| `csk install --all` | Install every project explicitly registered in global config. |
| `csk update` | Fetch all git repositories under `skills_root`. Does not modify projects. |
| `csk upgrade [target]` | Fetch only the selected project's direct and transitive skill repositories, then install. `--dry-run` does not update cached repositories or persist files. |
| `csk upgrade --all` | Fetch the union of dependency closures once, then install every registered project. |
| `csk status [target]` | Show manifest vs installed state, including active dev substitutions and compiled-build currentness. `--check` exits non-zero unless every skill and build is current; `--json` includes stable build rows and result-only capability evidence. |
| `csk status --all` | Show status for every registered project. |
| `csk add <name> --tag/--branch/--revision ...` | Add or replace a skill declaration in the project Skillfile; apply with `csk install`. |
| `csk remove <name>` | Remove a skill declaration from the project Skillfile; the next install cleans generated files. |
| `csk gc` | Under the manager-home lock, remove unreferenced runtime and snapshot entries, protected compiled-cache entries older than 24 hours, and dead consumer registry entries. Uncertain protected state is retained. |
| `csk audit [target]` | Run skill security audit for the current project, an alias, `.`, or a project path. Supports `--all`, `--global`, and `--json`. |
| `csk skill check <dir>` | Validate one skill directory without requiring global config or project setup. |
| `csk list [--paths]` | List configured projects and declared skills. |
| `csk project add <alias> <path>` | Register a project for `--all` and create a manifest if missing. |
| `csk project resolve [target]` | Show resolved project alias, checkout alias, Skillfile, and install paths. |
| `csk global init` | Create the user-wide global `Skillfile.json`, global skill context, bin, and env files. |
| `csk global add <name> --tag/--branch/--revision ...` | Add or replace a global skill declaration. |
| `csk global remove <name>` | Remove a global declaration; the next global install cleans generated files. |
| `csk global install` | Install all globally declared skills without fetching. |
| `csk global update` | Fetch source repositories for globally declared skills. |
| `csk global upgrade` | Run global update, then global install. `--dry-run` skips the update and performs a non-persistent install plan. |
| `csk global status` | Show global manifest and compiled-build state; supports `--json` and `--check`. |
| `csk global list` | List global skill declarations. |
| `csk config show` | Print resolved config path and contents. |
| `csk shell-init [auto\|zsh\|bash\|powershell]` | Optionally print shell hook code for human-facing global and project-local auto-`PATH` activation. The default `auto` detects the current environment; `--install` atomically caches it and prints the profile source command; `--no-global` limits activation to project checkouts. Agent execution does not require this hook. |
| `csk --version` | Print version and exit. |

Flags shared by `install` and `upgrade`:

- `--dry-run`: plan work without modifying files.
- `--verbose`: print resolved commits and installed command shims.
- `--fix-gitignore`: deprecated escape hatch; prefer `csk init`.
- `--strict-tags`: fail if a tag was locally moved to another commit.

Exit codes: `0` success, `1` one or more projects or skills failed, `2`
configuration error, `3` lock contention.

## Development

Requires Python 3.11+.

```bash
git clone https://github.com/ivanopcode/cocoaskills.git
cd cocoaskills
python -m venv .venv
source .venv/bin/activate
python -m pip install -e ".[dev]"
python -m pytest
python -m mypy
```

Build artifacts locally:

```bash
python -m build
twine check dist/*
```

The runtime package is stdlib-only. Versioning is driven by `setuptools-scm`
from git tags; the generated `src/csk/_version.py` is not committed.

See [CONTRIBUTING.md](CONTRIBUTING.md) for the contribution workflow, coding
conventions, and the RFC process for design changes.

## Documentation

- [Architecture overview](ARCHITECTURE.md): module map, install pipeline, the
  context/runtime split, storage layout, and security boundaries.
- [Skill dependencies, RFC 0007](docs/v0.9-design.md): schema v4 requirements,
  closure resolution, activation modes, dev substitutions, source allowlist.
  Russian translation: [docs/v0.9-design.ru.md](docs/v0.9-design.ru.md).
- [Skill authoring guide](docs/skill-authoring.md): practical contract for
  authoring CocoaSkills-compatible skill repositories, covering schema v2
  runtime roots, schema v3 capabilities, schema v4 requirements, system
  dependencies, schema v6 compiled commands, audit behavior, and the author
  checklist.
- [Skill security audit, RFC 0005](docs/audit-design.md): schema v3
  capabilities, deterministic audit gates, verdict cache, and trust workflow.
- [Audit LLM backends, RFC 0006](docs/v0.8-design.md): the `command` and
  `codex` audit backends, file-content redaction, timeout plumbing, and
  fail-open/fail-closed behavior.
- [MVP design specification](docs/mvp-design.md): the v0.1 contract; later
  RFCs supersede parts of it.
- [CHANGELOG](CHANGELOG.md): release history in Keep a Changelog format.

## Security

See [SECURITY.md](SECURITY.md) for supported versions and the vulnerability
reporting process. The audit subsystem and its guarantees are described in
[docs/audit-design.md](docs/audit-design.md).

Archive extraction rejects links, unsafe or colliding paths, more than 100,000
entries, or more than 512 MiB of declared file data. Registry reads cap each
response at 16 MiB and each artifact query at 10,000 records.

## License

Apache-2.0. See [LICENSE](LICENSE).
