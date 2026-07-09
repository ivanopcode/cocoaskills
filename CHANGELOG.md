# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added

- Added `csk audit --publish <record> --registry <url> --token <token>`, which
  submits a signed audit record to a registry. The token may also come from
  the `CSK_REGISTRY_TOKEN` environment variable.
- Recognized `opencode` and `windsurf` as known agents. Both environments
  discover the canonical `.agents/skills/` directory natively, so no
  project-level mirror is created for them; global installs are additionally
  mirrored into `~/.agents/skills/` when either agent is targeted.
- Added MCP configuration surfaces for the new agents: OpenCode resolves
  against the `mcp` block of `opencode.json` / `opencode.jsonc` in the project
  and `~/.config/opencode/`, honoring `"enabled": false`; Windsurf resolves
  against `~/.codeium/windsurf/mcp_config.json`.

## [0.11.0] - 2026-07-07

### Added

- Added an enforced system configuration layer read before the user config:
  `/etc/cocoaskills/config.json` on Unix and `%ProgramData%\cocoaskills\config.json`
  on Windows. Keys listed under `locked` take their value from the system
  config and cannot be overridden from the user config, so an organization
  distributes registry trust and source policy through device management. An
  unlocked system key acts as a default the user config may override.
- Added the strict registry policy: `audit.registry_policy: strict` fails an
  install when a skill is not audited by any trusted registry, while a
  verified revocation always denies regardless of policy.
- Added `csk status --attest`, which re-checks installed skills against the
  trusted registries so a revocation issued after install surfaces on demand.
- Added registry snapshot verification: before resolving, csk fetches each
  registry's signed snapshot and excludes a registry that serves a tampered
  view (bad signature, a version that moved backward, or a stale snapshot),
  which defends against rollback and freeze. An unreachable snapshot warns
  but does not exclude, since per-record signatures still protect the install.
- Added the audit registry client (RFC 0008, advisory): a machine can pin
  trusted registries in `audit_registries` (name, url, Ed25519 public keys),
  and `csk install` resolves each skill against them by source identity,
  commit, and content hash. A verified `revoked` record denies the install
  under a deny-wins federation rule; a verified `audited` record is recorded
  as an attestation in the install marker. Signatures are verified with a
  vendored, standard-library-only Ed25519 implementation, so the runtime
  keeps no third-party dependency. Lookups cache with a TTL and an offline
  grace window. `disable_builtin_registries` drops the built-in defaults for
  closed networks.

## [0.10.0] - 2026-07-07

### Added

- Added the hybrid install scope: skills declared in
  `~/.cocoaskills/hybrid/Skillfile.json` with per-skill `targets` (project
  alias, absolute path, or path glob) are stored once per machine, activated
  only for targeted projects through managed adapter links and project
  shims, and leave nothing in the target repository. Managed through
  `csk hybrid add/remove/list/status`; shadowing order is project, then
  hybrid, then global; closure resolution and audit gates apply unchanged.
- Added `csk-skill.json` schema v5 with `dependencies.mcp_servers`: a skill
  declares the MCP servers it relies on (`hint` required, optional
  `transport`, `required_in: any|all`), and `csk install` verifies each
  server against the configuration of the target agent environments
  (Claude Code, Codex CLI, Cursor, Gemini) before the skill lands. Install
  markers record where each server was found.

## [0.9.0] - 2026-07-05

### Added

- Added `csk-skill.json` schema v4 with `dependencies.skills`: self-contained
  skill-to-skill requirements (git URL, exact `tag`/`revision` ref, activation
  mode). Branch refs and version ranges on requirements are parse errors
  (RFC 0007, docs/v0.9-design.md).
- Added transitive closure resolution: requirement providers are fetched,
  unified by name to one commit and one canonical source, ordered before
  their consumers, and audited as part of the install. Cycles, version
  conflicts, and source conflicts fail with the requirement chains.
- Added activation modes `full`, `runtime`, and `context`. Command shims and
  prompt context materialize per effective surface; command collisions are
  checked over active shims only, and install markers record the activation,
  the requirers, and any substitution.
- Added a machine-level source allowlist: `allowed_sources` in
  `~/.cocoaskills/config.json` lists canonical `host/path` prefixes and is
  checked before any clone of a declared git source. SSH and HTTPS URLs of
  one repository normalize to one identity.
- Added development substitutions through `Skillfile.dev.json`: a provider is
  replaced by a local checkout path or a git source with any ref kind,
  branches included. `csk install` and `csk status` print active
  substitutions, `csk init` adds the file to the managed `.gitignore` block,
  and strict audit refuses substituted installs.
- Added `csk skill check` to validate intrinsic skill requirements without
  requiring global config or a consuming project.

### Changed

- Installs of skills that declare `dependencies.commands` entries with
  `type: "skill"` now print a migration warning pointing to schema v4
  `dependencies.skills`.
- Relaxed locale installation: if the selected locale is unavailable but the
  skill has another consistent locale catalog, installation falls back to the
  source `SKILL.md` with a warning instead of failing.
- Locale warnings are emitted before the install marker fast-path, so they
  remain visible for up-to-date installs and dry runs.

## [0.8.0] - 2026-06-17

### Security

- Added v0.8 audit backend hardening for LLM-assisted extraction: backend
  findings with `verifiable=false` are report-only and cannot block strict
  installs; Codex local backends must declare `oss=true` and a
  `local_provider`; unsafe Codex argument overrides are rejected.
- Cloud audit backend requests now redact file contents before process
  invocation. Oversized backend requests produce an auditable
  `audit.request.too-large` finding instead of silently truncating skill
  content.
- Added the RFC 0005 audit foundation: `csk-skill.json` schema v3 capability
  manifests, deterministic static audit findings, `csk audit`, install-time
  audit gates, strict `require_pin` handling for undeclared schema v1/v2 skills,
  and a content-addressed verdict cache.
- Skill names, source directory names, and command names are now validated as
  safe identifiers. Previously a command key like `../../x` in a third-party
  `csk-skill.json` could write a shim outside the designated bin directory.
- `git clone` of `Skillfile.json` `git` URLs now rejects option-like URLs,
  separates the URL with `--`, and restricts transports to
  `file/git/http/https/ssh` via `GIT_ALLOW_PROTOCOL`, blocking remote-helper
  URLs such as `ext::sh -c ...` that executed commands during `csk install`.

### Added

- Added typed `audit.backends` config with `null`, `command`, and `codex`
  backends, per-backend timeout plumbing, backend canaries, and request-size
  limits.
- Added a generic `command` audit backend using a stable JSON stdin/stdout
  protocol for local auditor processes.
- Added a first-party `codex` audit backend over `codex exec` with an empty
  working directory, stdin prompt input, JSON schema output, `--ephemeral`,
  `--ignore-rules`, and no web search.
- Added `csk install --audit`, `csk install --audit strict`, and matching
  global install/upgrade flags for one-shot audit gating without changing
  global config.
- Added `csk status --check`, exiting non-zero unless every skill of every
  selected project is up-to-date, and `csk status --json` for machine-readable
  output.
- Added project-scope `csk add <name>` and `csk remove <name>` for editing
  `Skillfile.json` declarations, mirroring `csk global add/remove`.
- Added `csk gc` for explicit garbage collection. The snapshot cache under
  `~/.cocoaskills/cache/` is now collected: entries not referenced by any
  install marker are removed.
- `csk bootstrap` supports scripted setup via `--skills-root`,
  `--preferred-locale`, `--default-agents`, `--non-interactive`, and
  `--force`, and rejects an empty skills_root.
- Runtime GC now tracks unregistered checkouts ('csk install .') through a
  consumer registry at `~/.cocoaskills/consumers.json`, so it no longer
  deletes runtime still referenced by worktree installs. Dead registry entries
  are pruned automatically.

### Changed

- `csk install <target>` with an explicitly requested project now exits `1`
  when the project is refused (gitignore gate, missing Skillfile) instead of
  reporting success; `--all` keeps reporting skips without failing the run.
- `--verbose` now prints the full resolved commit and the shim destination of
  every installed command; `csk global install/upgrade --strict-tags` now
  performs the moved-tag check it advertised.

### Fixed

- `csk global install` now publishes managed forwarding shims into a safe
  PATH-visible user bin when available, so global skill commands can work from
  arbitrary directories without requiring `csk shell-init`.
- Generated `.agents/env.sh` now resolves the project root correctly under
  zsh; previously it fell back to the caller's working directory.
- A missing `git` binary now produces an actionable error instead of a raw
  Python traceback.
- A stale global lock left by a crashed process is now detected via the
  recorded pid and broken safely instead of blocking every command until the
  file is removed by hand.
- Orphaned `.tmp-<pid>` and `.backup-<pid>` directories from interrupted
  installs are swept by GC once the owning process is gone.
- The `status` error label now reports its cause in the table and in
  `--json`; unknown agent names warn instead of being silently ignored;
  project `install --dry-run` no longer creates `skills_root`;
  `schema_version` errors distinguish missing and wrong-type values from
  genuinely newer files.

## [0.6.0] - 2026-05-27

### Added

- Added user-wide global skills under `~/.cocoaskills/global/`, managed through
  `csk global init/add/remove/list/status/install/update/upgrade`.
- Added global command shims under `~/.cocoaskills/global/bin` and shell-init
  activation that exposes global commands everywhere while project-local shims
  shadow them inside checkouts.
- Added user-level global adapters for Claude Code, Codex CLI, Cursor, and
  Gemini with `.csk-managed.json` ownership so handwritten user content is
  preserved.

### Changed

- Runtime GC now scans global skill markers as well as project markers, so
  runtime entries referenced only by global skills are preserved.

## [0.5.0] - 2026-05-19

### Added

- Added `csk-skill.json` schema v2 with `runtime_roots` for multi-file command
  runtimes. Runtime roots are copied to the global runtime store and excluded
  from installed agent prompt context, while sibling files remain available to
  command entrypoints at execution time.
- Hardened `type: system` command validation in schema v2 with a closed
  allow-list of fields (`type`, `command`, `hint`) and explicit rejection of
  `install`, `check`, `post_install`, `script`, and `command_args`.
- Markers now record `skill_schema_version` and `runtime_roots` for
  diagnostics.

### Changed

- Hardened schema v2 `system` command declarations: `csk` validates only
  `type`, `command`, and `hint`, checks presence with `shutil.which`, and never
  runs manifest-provided install or check commands.
- Missing `type: system` dependencies block skill installation before writes
  to runtime store, project context, or shims, leaving any previously installed
  version untouched.
- Schema v1 skills and `agents/runtime.json` fallback remain supported
  unchanged.

## [0.4.0] - 2026-05-18

### Added

- Added optional `git` URLs in `Skillfile.json` skill declarations. When a
  declared local source repository is missing, `csk install` clones the URL into
  `skills_root` before resolving the pinned ref.

## [0.3.0] - 2026-05-18

### Added

- Added `csk init` for one-time per-project setup. It creates
  `Skillfile.json`, writes `project.alias` and `agents`, and appends the
  managed CocoaSkills `.gitignore` block.
- Added `--all` to `install`, `upgrade`, and `status` for explicit
  multi-project operations over registered projects.

### Changed

- `csk install`, `csk status`, and `csk upgrade` without a target now operate
  on the current project resolved by walking up to `Skillfile.json`.
- `csk install .` and path-based installs no longer auto-register checkouts in
  global config.
- `csk bootstrap` now only writes machine-level config and no longer prompts
  for project registration.
- `csk project add` remains the explicit opt-in path for registering projects
  used by `--all`.

## [0.2.1] - 2026-05-15

### Changed

- Added transitional warnings for the planned v0.3.0 current-project install
  model: bare `install`/`status`/`upgrade`, path auto-registration, and
  `--fix-gitignore`.
- Added accepted RFC 0001 documenting the `csk init`, explicit `--all`, and
  current-project install migration plan.

## [0.2.0] - 2026-05-15

### Changed

- Renamed the published distribution package from `cocoaskill` to
  `cocoaskills`. The CLI command remains `csk`, and existing runtime/config
  paths under `~/.cocoaskills/` are unchanged.
- Updated install documentation, install script, distribution smoke tests, and
  Homebrew instructions to use `cocoaskills`.

## [0.1.2] - 2026-05-14

### Added

- Current-checkout install resolution with `csk install .`, `csk status .`,
  and `csk project resolve .`.
- Worktree-aware checkout aliases derived from `Skillfile.json` project aliases,
  branch task ids, and stable path hashes.
- Distribution smoke workflow for published package installs across pipx, uv,
  mise, install.sh, and Homebrew.

### Fixed

- Dry-run installs no longer populate the persistent snapshot cache.
- Lock timeout is testable through `CSK_LOCK_TIMEOUT`.
- Windows smoke tests use native paths where required.
- Tilde paths are recognized as path targets.

## [0.1.1] - 2026-05-13

### Fixed

- Updated the published install script domain to `cocoaskills.org`.

## [0.1.0] - 2026-05-13

Initial public release.

### Added

- `csk` CLI installable from PyPI as `cocoaskill`.
- Project manifest `Skillfile.json` declaring per-project skill dependencies
  with `tag`, `branch`, or `revision` git refs.
- Global config `~/.cocoaskills/config.json` listing managed projects and the
  local `skills_root` containing git repositories.
- Stripped install layout under `<project>/.agents/skills/<skill>/` with
  reproducible content hashing in `.csk-install.json` markers.
- Multi-agent adapters for Claude Code, Codex CLI, Cursor, and Gemini, with
  per-adapter ownership tracking via `.csk-managed.json`.
- Project-local `.agents/bin/` command shim layer with global runtime store at
  `~/.cocoaskills/runtime/<skill>/<commit>/bin/`.
- POSIX `env.sh` and PowerShell `env.ps1` generation plus `csk shell-init` for
  automatic `PATH` activation on directory change.
- Gitignore gate that refuses installation when generated paths are not ignored
  by git, with opt-in `--fix-gitignore` to append a managed block.
- Optional `csk-skill.json` skill command manifest with `script` and `system`
  command types; fallback to legacy `agents/runtime.json`.
- Locale rendering for skills declaring `locales/metadata.json` plus
  `.skill_triggers/<locale>.md`.
- Snapshot cache under `~/.cocoaskills/cache/<source>/<commit>/snapshot/` for
  reuse across projects.
- Global install lock with 30 s timeout and exit code `3` on contention.
- Stable exit codes: `0` success, `1` partial failure, `2` configuration error,
  `3` lock contention.
- Hardened `git archive` extraction with `tarfile` data filter and Python 3.11
  manual path-traversal fallback.
- `--strict-tags` flag rejecting locally moved tags.
- `csk status` with stable labels: `up-to-date`, `missing`, `update-available`,
  `content-drift`, `error`.

[Unreleased]: https://github.com/ivanopcode/cocoaskills/compare/v0.11.0...HEAD
[0.11.0]: https://github.com/ivanopcode/cocoaskills/compare/v0.10.0...v0.11.0
[0.10.0]: https://github.com/ivanopcode/cocoaskills/compare/v0.9.0...v0.10.0
[0.9.0]: https://github.com/ivanopcode/cocoaskills/compare/v0.8.0...v0.9.0
[0.8.0]: https://github.com/ivanopcode/cocoaskills/compare/v0.6.0...v0.8.0
[0.6.0]: https://github.com/ivanopcode/cocoaskills/compare/v0.5.0...v0.6.0
[0.5.0]: https://github.com/ivanopcode/cocoaskills/compare/v0.4.0...v0.5.0
[0.4.0]: https://github.com/ivanopcode/cocoaskills/compare/v0.3.0...v0.4.0
[0.3.0]: https://github.com/ivanopcode/cocoaskills/compare/v0.2.1...v0.3.0
[0.2.1]: https://github.com/ivanopcode/cocoaskills/compare/v0.2.0...v0.2.1
[0.2.0]: https://github.com/ivanopcode/cocoaskills/compare/v0.1.2...v0.2.0
[0.1.2]: https://github.com/ivanopcode/cocoaskills/compare/v0.1.1...v0.1.2
[0.1.1]: https://github.com/ivanopcode/cocoaskills/compare/v0.1.0...v0.1.1
[0.1.0]: https://github.com/ivanopcode/cocoaskills/releases/tag/v0.1.0
