# CocoaSkills

[![PyPI](https://img.shields.io/pypi/v/cocoaskills.svg)](https://pypi.org/project/cocoaskills/)
[![Python versions](https://img.shields.io/pypi/pyversions/cocoaskills.svg)](https://pypi.org/project/cocoaskills/)
[![License](https://img.shields.io/pypi/l/cocoaskills.svg)](https://github.com/ivanopcode/cocoaskills/blob/main/LICENSE)
[![CI](https://github.com/ivanopcode/cocoaskills/actions/workflows/ci.yml/badge.svg)](https://github.com/ivanopcode/cocoaskills/actions/workflows/ci.yml)

Russian version: [README.md](README.md).

`csk` manages local skill packages for AI agents. The tool downloads skills from git repositories and prepares files for six environments: Claude Code, Codex CLI, Cursor, Gemini, OpenCode, and Windsurf. It is an independent Python implementation of the open [Curator Protocol](https://github.com/relux-works/curator-spec) specification. The `csk` executable, package name, and state directory names remain implementation-specific compatibility names; portable manifest and marker names follow the shared protocol.

## Why

Manual skill management across multiple projects creates drift during team development. File contents on developer machines diverge over time. Unpinned updates break working environments. Auxiliary files, such as README files, tests, and build artifacts, leak into the agent context and consume token limits. Removing a skill from a project configuration leaves unused files on disk.

`csk` addresses these issues through a declarative `Skillfile.json` manifest. The tool pins git repository versions, copies only `SKILL.md` and declared directories (`references/`, `assets/`, `agents/`, `data/`) into the agent context, excludes non-skill files (`tests`, `README`, build artifacts, git metadata), and removes stale files when skill selections change.

## Why CocoaSkills and Not Alternatives

Manual copying or manual symlinks require no system dependencies. This approach breaks during updates because developers must repeat manual copy operations across all projects. Manual copying also imports extra repository files into project checkouts and inflates agent prompt context. `csk` automates downloads, extracts allowed skill directories, and updates files with one command.

Git submodules and git subtrees use built-in git mechanisms. Submodules pull complete repository histories, require git commands during branch switches, and cannot generate per-agent adapters. `csk` downloads repositories into a local cache, extracts only skill files, and builds configurations for each configured agent.

Built-in plugin marketplaces inside specific agents offer one-click installation. These marketplaces bind skills to a single agent and prevent teams from sharing one manifest across different tools. `csk` keeps one `Skillfile.json` in the project repository and populates adapters for Claude Code, Codex CLI, Cursor, and Gemini, while OpenCode and Windsurf read the canonical `.agents/skills/` directory natively.

A shared monorepo directory synced by custom shell scripts centralizes file storage across projects. This approach requires writing and maintaining custom scripts, omits content-hash verification, and fails to manage transitive dependencies. `csk` computes skill dependency graphs, verifies content hashes, and isolates generated files.

CocoaSkills does not act as a public package registry, an agent execution runtime, or an MCP server manager. The tool handles declarative delivery and local file layout for agent skills.

## Quick Start

1. Install CocoaSkills using `pipx`:

   ```bash
   pipx install cocoaskills
   ```

   Result: `csk --version` prints the installed CocoaSkills version. See the [Install matrix](#install-matrix) section for other platforms.

2. Navigate to the project directory and initialize configuration:

   ```bash
   cd /path/to/project
   csk init
   ```

   Result: `csk init` creates `Skillfile.json` with initial project configuration and appends `.agents/`, `.claude/skills/`, `.codex/skills/`, `.cursor/rules/`, `.gemini/skills/`, and `Skillfile.dev.json` to `.gitignore`.

3. Add a skill declaration to the project:

   ```bash
   csk add skill-tracker --git git@gitlab.example.com:skills/skill-tracker.git --tag v1.0.0
   ```

   Result: `csk add` appends the `skill-tracker` entry with repository URL and tag `v1.0.0` to the `skills` array in `Skillfile.json`.

4. Install declared skills:

   ```bash
   csk install
   ```

   Result: `csk install` clones the repository, extracts files into `.agents/skills/skill-tracker/`, builds adapter mirrors, and creates command shims in `.agents/bin/`.

5. Verify skill availability in the target agent:

   ```bash
   claude
   ```

   Result: the agent reads instructions from `.claude/skills/` and applies `skill-tracker` rules in the active session.

## Skill Install Modes

CocoaSkills supports three installation modes based on ownership boundary and file layout requirements.

### Project Mode

Project mode records skills in `Skillfile.json` at the repository root. Developers commit this file to version control. Running `csk install` on any machine deploys an identical set of skills across the team.

A project `Skillfile.json` configuration uses this format:

```json
{
  "schema_version": 1,
  "project": { "alias": "demo-ios" },
  "agents": ["claude_code", "codex_cli", "cursor"],
  "skills": [
    {
      "name": "skill-tracker",
      "git": "git@gitlab.example.com:skills/skill-tracker.git",
      "tag": "v1.0.0"
    }
  ]
}
```

### Global Mode

Global mode installs skills once per machine under `~/.cocoaskills/global/`. Global skills operate across all directories regardless of whether a git repository or `Skillfile.json` exists.

Add a global skill declaration using this command:

```bash
csk global add skill-metrics --git git@gitlab.example.com:skills/skill-metrics.git --tag v2.1.0
```

Running `csk global install` downloads the repository and creates adapters in user agent directories under the home directory.

### Hybrid Mode

Hybrid mode declares skills once per machine in `~/.cocoaskills/hybrid/Skillfile.json` and activates them for target projects matching an alias, path, or glob pattern. The installer creates adapters in project agent directories (`.claude/skills/`, `.codex/skills/`) and command shims in `.agents/bin/`, requiring no git commits in target repositories. Platform teams use hybrid mode to roll out workflow rules to selected checkouts.

Link a hybrid skill to a project alias using this command:

```bash
csk hybrid add workflow-lint --git git@gitlab.example.com:skills/workflow-lint.git --tag v1.2.0 --target "demo-ios"
```

When you execute `csk install` inside the `demo-ios` checkout, the installer evaluates rules in `~/.cocoaskills/hybrid/Skillfile.json`, matches the `demo-ios` alias, and attaches `workflow-lint` to the local agent context.

### Shadowing Order

When skill names collide across installation modes, the installer applies this priority order: project mode overrides hybrid mode, and hybrid mode overrides global mode (`project > hybrid > global`).

## Install Matrix

Choose the package manager that fits your system environment. `pipx` is the recommended choice across all platforms.

### pipx (Recommended)

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

### Convenience Install Script

```bash
curl -fsSL https://cocoaskills.org/install.sh | sh
```

The script detects Python, prefers `pipx` or `uv tool`, and falls back to `pip install --user`. Inspect script contents before executing remote shell commands.

### Plain pip

```bash
python -m pip install --user cocoaskills
```

## Skill Dependencies

A skill package can declare requirements on other skills. Declare skill requirements in `agent-skill.json` schema v4 under `dependencies.skills`. Each dependency entry specifies a git repository URL, an exact `tag` or `revision` ref, and an activation mode:

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

Activation modes control what a provider contributes to a consumer context:

- `full` (default) activates provider prompt context and all exported commands.
- `runtime` activates commands only; the `commands` array narrows activation to named exports.
- `context` activates provider prompt context only.

`csk install` computes the transitive closure, unifies duplicate requirements to a canonical ref, orders providers before consumers, and audits the complete resolution graph. Version mismatches, source URL conflicts, and dependency cycles produce immediate installation errors.

Two mechanisms support local development and security control:

- `Skillfile.dev.json` substitutes skill providers locally during development with a checkout path or git ref. The file is excluded from version control, and strict audit gates reject substituted installations.
- `allowed_sources` in `~/.cocoaskills/config.json` defines allowed `host/path` git prefixes. The installer normalizes SSH and HTTPS URLs to verify source identity.

## Global Skills and Selective Operations

Global skills provide baseline user capabilities across projects. Files live under `~/.cocoaskills/global/` and link into user agent directories such as `~/.claude/skills/` and `~/.codex/skills/`. When OpenCode or Windsurf are enabled, global skills link into `~/.agents/skills/`.

Initialize and populate global configuration:

```bash
csk global init
csk global add skill-metrics --git git@gitlab.example.com:skills/skill-metrics.git --tag v1.0.0
csk global install
```

### Selective Global Operations

`csk global install`, `csk global update`, and `csk global upgrade` accept the `--only <name>` flag to restrict operations to specific declarations:

```bash
csk global install --only skill-metrics
csk global upgrade --only skill-metrics --only skill-lint
```

A selected skill pulls required dependencies into the execution closure. Unselected declarations remain unchanged on disk.

Global commands publish executable shims into `~/.cocoaskills/global/bin/`. `csk global install` also publishes forwarders into user binary paths such as `~/.local/bin/`.

Agent execution resolves project shims (`<repo>/.agents/bin/<command>`), then global shims (`<csk-home>/global/bin/<command>`), and finally validated system commands. Shell profile hooks are optional human conveniences. Configure shell hooks using this command:

```bash
csk shell-init --install
```

Result: the command caches the shell hook and prints the profile sourcing command.

## Skill Command Manifests

Skill packages declare commands, capabilities, and dependencies through `agent-skill.json`. Schema v2 introduces multi-file runtime storage under `runtime_roots`. Schema v3 adds the `capabilities` audit envelope. Schema v4 adds skill dependencies, schema v5 adds MCP server requirements, and schema v6 introduces compiled commands and context-excluded `build_roots`.

A complete mixed command manifest uses this structure:

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

The manifest above configures `format-report` as a script, `repo-report` as a compiled Go tool, and `git` as a required system binary.

## Compiled Commands

Schema v6 supports compiled executables using the `go-v1` driver. Schema v7 adds locked external git build repositories through `go-repository-v1`. For the complete build contract, storage layout, worker handoff protocol, and security boundaries, see [ARCHITECTURE.md](ARCHITECTURE.md).

Build roots isolate source code from prompt context. A build command specifies its driver and source directory:

```json
{"type":"build","driver":"go-v1","source_dir":"build/cmd/repo-report"}
```

The `go-v1` driver uses vendor data for external dependencies and disables build-time network calls. Package validation rejects toolchain switching, cgo, PGO, generators, tests, assembly files, and external linking.

Build operations execute through a manager-owned worker process (`manager-worker-v1`). The manager verifies worker identity before granting build authorization. Compiled artifacts land in protected cache storage under `<csk-home>/builds/go-v1/` and execute via generated shims.

## Skill Security Audit

`csk audit` evaluates security rules against skill snapshots. Static detectors inspect file contents and capability declarations. Optional `command` and `codex` backends provide structured analysis.

Run security audit on the current project:

```bash
csk audit
csk audit . --json
csk audit --global
```

Enforce audit checks during installation:

```bash
csk install --audit
csk install --audit strict
```

Advisory mode prints warnings. Strict mode blocks installation when findings reach or exceed the target risk threshold.

### Audit Registries

Audit registries distribute signed statements verifying skill commits and content hashes. Configure trusted registries in `~/.cocoaskills/config.json`:

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

`csk install` verifies signed records using Ed25519 public keys. Verified revocations block installation.

System configuration at `/etc/cocoaskills/config.json` (or `%ProgramData%\cocoaskills\config.json` on Windows) enforces enterprise defaults. Locked keys in system configuration override user settings.

## CLI Reference

| Command | Behavior |
|---|---|
| `csk bootstrap` | Create machine-level global config; interactive or scripted via `--skills-root`, `--default-agents`, `--non-interactive`, `--force`. `--if-missing` is an idempotent no-op when config already exists. |
| `csk init [path]` | Create project `Skillfile.json` and the managed `.gitignore` block. Supports `--alias`, `--agents`, and `--no-interactive`. |
| `csk install [target]` | Apply `Skillfile.json` using current git refs. Clones missing repositories into `skills_root`. Supports `--dry-run`. |
| `csk install --audit [strict]` | Run audit gate during installation. Advisory by default; `strict` enforces threshold checks. |
| `csk install --all` | Install all registered projects listed in global config. |
| `csk update` | Fetch git repositories under `skills_root` without modifying projects. |
| `csk upgrade [target]` | Fetch selected project skill repositories and run installation. |
| `csk upgrade --all` | Fetch dependency closures and install all registered projects. |
| `csk status [target]` | Report manifest versus installed state, active substitutions, and compiled build status. Supports `--check` and `--json`. |
| `csk status --all` | Report status for all registered projects. |
| `csk add <name> --tag/--branch/--revision ...` | Add or update a skill entry in the project manifest. |
| `csk remove <name>` | Remove a skill entry from the project manifest. |
| `csk gc` | Remove unreferenced runtime entries, expired build caches (>24h), and dead consumer entries under manager lock. |
| `csk audit [target]` | Execute security audit for a project, alias, or path. Supports `--all`, `--global`, and `--json`. |
| `csk skill check <dir>` | Validate a standalone skill directory without requiring global or project setup. |
| `csk list [--paths]` | List registered projects and declared skills. |
| `csk project add <alias> <path>` | Register a project path for multi-project operations. |
| `csk project resolve [target]` | Display resolved project aliases, manifest paths, and target directories. |
| `csk global init` | Create global configuration, context directories, and binary paths. |
| `csk global add <name> --tag/--branch/--revision ...` | Add or update a global skill declaration. |
| `csk global remove <name>` | Remove a global skill declaration. |
| `csk global install` | Install globally declared skills. Supports `--only <name>`. |
| `csk global update` | Fetch git sources for global skills. Supports `--only <name>`. |
| `csk global upgrade` | Fetch git sources and install global skills. Supports `--dry-run` and `--only <name>`. |
| `csk global status` | Report global manifest and compiled build state. Supports `--json` and `--check`. |
| `csk global list` | List global skill declarations. |
| `csk hybrid add <name> --git ... --tag/--branch/--revision --target <alias\|path\|glob>` | Declare or update a hybrid skill binding to project aliases, paths, or globs. `--target` is repeatable. |
| `csk hybrid remove <name>` | Remove a hybrid skill declaration. |
| `csk hybrid list` | List hybrid skill declarations and target bindings. |
| `csk hybrid status` | Report hybrid declarations and installed store state. |
| `csk config show` | Print resolved configuration path and JSON contents. |
| `csk shell-init [auto\|zsh\|bash\|powershell]` | Generate shell hook code for automatic PATH setup. `--install` caches hook code. |
| `csk --version` | Print program version and exit. |

Shared flags for `install` and `upgrade`:

- `--dry-run`: calculate execution plan without modifying files.
- `--verbose`: print resolved commit hashes and installed command shims.
- `--strict-tags`: fail installation if local tag references drift from remote commits.

Exit codes: `0` success, `1` project or skill error, `2` configuration error, `3` lock contention.

## Development

CocoaSkills requires Python 3.11 or newer. Set up a local development environment:

```bash
git clone https://github.com/ivanopcode/cocoaskills.git
cd cocoaskills
python -m venv .venv
source .venv/bin/activate
python -m pip install -e ".[dev]"
python -m pytest
python -m mypy
```

Build distribution packages locally:

```bash
python -m build
twine check dist/*
```

The runtime package relies exclusively on the Python standard library. Version numbers derive from git tags via `setuptools-scm`.

For contribution guidelines and coding standards, see [CONTRIBUTING.md](CONTRIBUTING.md) and [docs/prose-style.md](docs/prose-style.md).

## Documentation

Reference documentation and technical specifications:

- [`ARCHITECTURE.md`](ARCHITECTURE.md): internal architecture, install pipeline, context/runtime split, storage layout, security boundaries.
- [`SECURITY.md`](SECURITY.md): vulnerability reporting process and security boundaries.
- [`docs/skill-authoring.md`](docs/skill-authoring.md): package structure, command manifest schemas, capability definitions, and author checklist.
- [`docs/audit-design.md`](docs/audit-design.md): RFC 0005 security audit engine, capabilities envelope, and verdict caching.
- [`docs/v0.9-design.md`](docs/v0.9-design.md): RFC 0007 skill dependencies, closure resolution, and source allowlist rules.
- [`docs/v0.8-design.md`](docs/v0.8-design.md): RFC 0006 audit backends and content redaction policy.
- [`docs/mvp-design.md`](docs/mvp-design.md): original v0.1 design specification.
- [`docs/external-build-repositories.md`](docs/external-build-repositories.md): RFC 0009 external build repositories specification.
- [`CHANGELOG.md`](CHANGELOG.md): release notes and version history.

## Security

See [SECURITY.md](SECURITY.md) for supported versions and vulnerability reporting procedures. The security audit system and risk boundaries are documented in [docs/audit-design.md](docs/audit-design.md).

Archive extraction rejects symlinks, escaping paths, path collisions, archives exceeding 100,000 files, or uncompressed data exceeding 512 MiB. Registry network calls enforce limits of 16 MiB per response and 10,000 records per query.

## License

Apache-2.0. See [LICENSE](LICENSE).
