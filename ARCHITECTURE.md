# Architecture

Translations: [Русский](ARCHITECTURE.ru.md). English is the source of truth.

This document maps the codebase for contributors: the core concepts, the
install pipeline, the module layout, the storage locations, and the security
boundaries. Design decisions live in the RFCs under [docs/](docs/); this
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

A skill install materializes two independent layers:

- The prompt context layer: `SKILL.md`, `references/`, and other agent-facing
  files copied into `<project>/.agents/skills/<name>/` and mirrored into agent
  adapter directories. This is what the agent reads.
- The runtime layer: `runtime_roots` copied into the shared runtime store and
  exposed as command shims in `<project>/.agents/bin/`. Agents and humans
  execute these shims explicitly; optional shell activation only adds bare-name
  convenience. Runtime files stay out of the agent context.

The split keeps the agent window small and makes activation modes possible: a
dependency can contribute commands, context, or both ([RFC 0007](docs/v0.9-design.md)).
Consequently, prompt-visible instructions resolve exported command shims by
project/global scope and never address a `runtime_root` relative to `SKILL.md`.
This remains true when an adapter mirrors context by copy instead of symlink,
and it removes any dependency on zsh/bash/PowerShell profile initialization.

## Install pipeline

`csk install` for one project runs these stages in order:

1. Load the machine config (`config.py`) and the project manifest
   (`manifest.py`).
2. Check the gitignore gate (`gitignore_gate.py`): generated paths must be
   ignored by git before anything is written.
3. Load development substitutions from `Skillfile.dev.json`
   (`dev_substitutions.py`); strict audit refuses substituted installs. Fold in
   the applicable hybrid-scope skills (`hybrid.py`).
4. Build the dependency closure (`closure.py`): expand `dependencies.skills`
   transitively, gate every clone through the source allowlist
   (`source_identity.py`), resolve exact refs to commits (`git_ops.py`), take
   content snapshots (`snapshot.py`), unify each skill name to one commit and
   one canonical source, reject cycles, and order providers before consumers.
5. Validate each snapshot (`skillcheck.py`, `skillspec.py`) and detect
   collisions over the commands that the closure actually activates.
6. Run the audit gate over the whole closure (`audit/pipeline.py`), verify
   declared MCP servers against the agent configuration surfaces
   (`mcp_configs.py`), and resolve each skill against the trusted audit
   registries (`audit_registry.py`).
7. Materialize each node according to its effective activation: runtime roots
   and shims (`shims.py`), prompt context through the whitelist
   (`whitelist.py`, `locale.py`), and an install marker with the resolved
   commit, content hash, activation, and requirers (`installer.py`).
8. Refresh agent adapters (`adapters.py`), write env files (`env_files.py`),
   remove stale shims and removed skills, and record the checkout in the
   consumer registry (`consumers.py`).
9. Collect garbage in the shared stores (`gc.py`).

`csk status` compares the install markers against freshly resolved refs and
content hashes, and `csk status --attest` re-checks installed skills against
the registries (`attest.py`). `csk audit` runs stage 6 standalone.

## Module map

| Module | Responsibility |
|---|---|
| `cli.py` | Argument parsing and command dispatch. |
| `config.py` | Machine config and the enforced system-config layer: `skills_root`, default agents, adapter mode, audit settings, `allowed_sources`, `audit_registries`. |
| `manifest.py` | `Skillfile.json` parsing and editing. |
| `skillspec.py` | `agent-skill.json` parsing: commands, runtime roots, capabilities, dependencies, requirements (schema v1 through v4). |
| `closure.py` | Transitive requirement resolution, unification, cycle detection, activation edges, topological order. |
| `source_identity.py` | Canonical `host/path` identity for git URLs and allowlist matching. |
| `mcp_configs.py` | Read-only resolution of declared MCP server dependencies against agent configuration surfaces, with static availability probes: PATH resolution for stdio commands, disabled-server filtering, and trust-gating hints for project-only declarations. |
| `hybrid.py` | Hybrid-scope manifest and per-project activation targeting. |
| `dev_substitutions.py` | `Skillfile.dev.json` parsing for local provider substitution. |
| `git_ops.py` | Hardened git operations: clone with a protocol allowlist, ref resolution, archive extraction with path checks. |
| `snapshot.py` | Content-addressed snapshot cache of skill commits. |
| `whitelist.py` | Prompt-context copy rules: which skill files reach the agent. |
| `locale.py` | Locale rendering for localized skill metadata. |
| `shims.py` | Runtime store population and command shim creation. |
| `installer.py` | Project install orchestration and install markers. |
| `global_install.py`, `global_bins.py` | User-wide skill installs and global command shims. |
| `adapters.py` | Per-agent adapter directories with managed-entry tracking; native-discovery agents (OpenCode, Windsurf) read the canonical directory and skip project mirrors. |
| `status.py` | Manifest versus installed state reporting. |
| `attest.py` | Re-check installed markers against trusted audit registries. |
| `audit_registry.py` | Audit registry client: record verification, deny-wins federation, snapshot checks, lookup cache. |
| `_ed25519.py` | Vendored standard-library Ed25519 signature verification. |
| `gc.py` | Garbage collection for the runtime store and snapshot cache. |
| `consumers.py` | Registry of checkouts that reference the shared stores. |
| `locking.py` | Global install lock with stale-lock recovery. |
| `hashing.py` | Content hashing of installed trees. |
| `identifiers.py` | Safe identifier rules for names that become filesystem paths. |
| `audit/` | Security audit: static detectors, capability checks, policy decisions, trust store, extraction backends. |

## Storage layout

Machine level, under `~/.cocoaskills/`:

```text
config.json                  machine config
cache/<source>/<commit>/     content snapshots of skill commits
runtime/<skill>/<commit>/    runtime files and command entrypoints
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
.agents/bin/<command>        shims into the runtime store
.claude/skills/, .codex/skills/, .cursor/rules/, .gemini/skills/
                             per-agent adapter mirrors
```

OpenCode and Windsurf discover `.agents/skills/` natively and get no mirror
directory; for global installs they are served through `~/.agents/skills/`.

## Security boundaries

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
platform markers, and command fixtures ship both `unix_path` and `win_path`
entrypoints so the suite passes on Linux, macOS, and Windows.
