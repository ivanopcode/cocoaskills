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
A requirement lives in `csk-skill.json` schema v4 under `dependencies.skills`,
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

If no safe user bin is available, the install succeeds and prints a warning.
In that case, add `~/.cocoaskills/global/bin` to `PATH`, set
`CSK_GLOBAL_USER_BIN` to a writable PATH directory, or install the shell hook:

```bash
eval "$(csk shell-init zsh)"
```

Inside a project, the shell hook still matters for project-local command
shadowing: `.agents/bin` shims should come before global shims. Project-local
skills with the same name shadow global skills. Global skills never replace
committed project `Skillfile.json` declarations.

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
`csk-skill.json`. Schema v2 supports multi-file runtimes: `runtime_roots` are
copied into `~/.cocoaskills/runtime/<skill>/<commit>/` and excluded from agent
prompt context. Schema v3 adds the `capabilities` envelope used by `csk audit`
and strict install gates. Schema v4 adds skill requirements (see
[Skill dependencies](#skill-dependencies)).

```json
{
  "schema_version": 4,
  "runtime_roots": ["scripts"],
  "capabilities": {
    "network": ["gitlab.example.com"],
    "filesystem": "repo",
    "exec": ["review-cli"],
    "secrets": "none",
    "env_read": ["HOME"],
    "prompt_scope": "Review merge request metadata and produce local advice."
  },
  "commands": {
    "mr": {
      "type": "script",
      "unix_path": "scripts/mr"
    },
    "review-cli": {
      "type": "system",
      "command": "review-cli",
      "hint": "Install the review CLI through project bootstrap tooling"
    }
  }
}
```

`system` commands are only checked with `shutil.which`; CocoaSkills never
installs system tools, and manifests carry no install hooks or version probes.

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

For managed fleets, a system configuration at `/etc/cocoaskills/config.json`
(or `%ProgramData%\cocoaskills\config.json` on Windows) is read before the
user config. Keys it lists under `locked` cannot be overridden from the user
config, so registry trust, the source allowlist, and the audit policy can be
distributed through device management. Set `audit.registry_policy` to `strict`
to fail any install that is not audited by a trusted registry, and run
`csk status --attest` to re-check installed skills against the registries.
An auditor submits a signed record with
`csk audit --publish <record> --registry <url> --token <token>`. The reference
registry service, including air-gapped bundle export and import for closed
networks, lives at
[cocoaskills-registry](https://github.com/ivanopcode/cocoaskills-registry).

## CLI

| Command | Behavior |
|---|---|
| `csk bootstrap` | Create machine-level global config; interactive or scripted via `--skills-root`, `--default-agents`, `--non-interactive`, `--force`. |
| `csk init [path]` | Create project `Skillfile.json` and the managed `.gitignore` block. Supports `--alias`, `--agents`, and `--no-interactive` for scripted setup. |
| `csk install [target]` | Apply `Skillfile.json` using current git refs. Missing `git` URL sources are cloned into `skills_root`; existing local repositories are not fetched. No target means current project; `target` may be an alias, `.`, or a project path. |
| `csk install --audit [strict]` | Run the audit gate for this install only. Without `strict`, audit is advisory and does not change config. |
| `csk install --all` | Install every project explicitly registered in global config. |
| `csk update` | Fetch all git repositories under `skills_root`. Does not modify projects. |
| `csk upgrade [target]` | Run `update`, then `install`. |
| `csk upgrade --all` | Run `update`, then install every registered project. |
| `csk status [target]` | Show manifest vs installed state, including active dev substitutions. `--check` exits non-zero unless everything is up-to-date; `--json` prints machine-readable output. |
| `csk status --all` | Show status for every registered project. |
| `csk add <name> --tag/--branch/--revision ...` | Add or replace a skill declaration in the project Skillfile; apply with `csk install`. |
| `csk remove <name>` | Remove a skill declaration from the project Skillfile; the next install cleans generated files. |
| `csk gc` | Remove unreferenced runtime entries, snapshot cache entries, and dead consumer registry entries. |
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
| `csk global upgrade` | Run global update, then global install. |
| `csk global status` | Show global manifest vs installed state. |
| `csk global list` | List global skill declarations. |
| `csk config show` | Print resolved config path and contents. |
| `csk shell-init [zsh\|bash\|powershell]` | Print shell hook code for global and project-local auto-`PATH` activation. `--no-global` limits activation to project checkouts. |
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
pytest
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
  dependencies, audit behavior, and the release checklist.
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
