# CocoaSkill MVP Design

Status: accepted for MVP implementation

This document defines the first implementation scope for `csk`, a local skill
manager for already cloned git repositories containing agent skills.

The full CocoaSkill specification includes registry sources, lockfiles,
signature verification, certificate authorities, source audit, and richer
multi-agent context management. Those features are intentionally out of scope
for this MVP unless explicitly marked as an interface reserved for later.

## Goals

- Provide a single Python script installable into `PATH`.
- Work on macOS, Linux, and Windows using only the Python standard library.
- Install skills from local git repositories under a configured skills root.
- Install the same declared skills into multiple configured projects.
- Keep project installs reproducible by recording resolved git commits and
  content hashes.
- Keep executable scripts out of installed skill context directories.
- Make skill-provided commands available through a stable project-local `PATH`
  layer.
- Avoid mutating source skill repositories during install.
- Refuse installation into projects where generated directories are not ignored
  by git.

## Non-Goals For MVP

- Remote registries.
- Full `Skillspec.yml`.
- YAML parsing or third-party Python dependencies.
- Cryptographic signing, CA chains, revocation, or trust providers.
- Transitive dependency resolution.
- Full source audit.
- Managed root context assembly.
- Automatic shell modification without an explicit one-time shell hook setup.

## Naming

CLI command:

```text
csk
```

Global config:

```text
~/.cocoaskills/config.json
```

Project manifest:

```text
<project>/Skillfile.json
```

Skill command manifest:

```text
<skill-repo>/csk-skill.json
```

Installed skill marker:

```text
<project>/.agents/skills/<skill>/.csk-install.json
```

## Global Config

The global config is JSON to keep the MVP zero-dependency and portable.

Default path:

```text
~/.cocoaskills/config.json
```

Override:

```text
CSK_CONFIG=/path/to/config.json
```

Example:

```json
{
  "schema_version": 1,
  "skills_root": "/Users/iv/agents/skills",
  "preferred_locale": "ru",
  "default_agents": ["codex_cli", "claude_code", "cursor"],
  "adapter_mode": "auto",
  "projects": {
    "partners-app-ios": {
      "path": "/Users/iv/Developer/Wildberries/partners-app-dev",
      "agents": ["codex_cli", "claude_code", "cursor"]
    }
  }
}
```

Fields:

| Field | Required | Description |
| --- | --- | --- |
| `schema_version` | yes | Config schema version. MVP value is `1`. |
| `skills_root` | yes | Directory containing local git repositories for skills. |
| `preferred_locale` | no | Default locale used when a project does not specify one. |
| `default_agents` | no | Agents used for new projects. |
| `adapter_mode` | no | `auto`, `symlink`, or `copy`. Default is `auto`. |
| `projects` | yes | Map of project alias to project config. |

## Project Manifest

Each managed project has a `Skillfile.json` in the git repository root.

Example:

```json
{
  "schema_version": 1,
  "agents": ["codex_cli", "claude_code", "cursor"],
  "locale": "ru",
  "skills": [
    {
      "name": "skill-youtrack",
      "tag": "v1.0.0"
    },
    {
      "name": "product-forensics",
      "source": "skill-product-forensics",
      "tag": "v1.0.1"
    },
    {
      "name": "skill-grafana",
      "branch": "main"
    },
    {
      "name": "logbook",
      "revision": "6deb71a"
    }
  ]
}
```

Fields:

| Field | Required | Description |
| --- | --- | --- |
| `schema_version` | yes | Manifest schema version. MVP value is `1`. |
| `agents` | no | Target agent adapters. Defaults to project config or global defaults. |
| `locale` | no | Project locale. Overrides global `preferred_locale`. |
| `skills` | yes | Skill declarations. |

Each skill declaration requires `name` and exactly one of `tag`, `branch`, or
`revision`.

Optional `source` names the local skill repository directory when it differs
from the installed skill name.

Skill names must be unique within one `Skillfile.json`. Two declarations with
the same `name` are a project error, even if they use different `source`
repositories.

`skills` is required, but an empty array is valid.

`csk project add` creates a minimal manifest when the file is missing:

```json
{
  "schema_version": 1,
  "agents": [],
  "skills": []
}
```

`csk project add` requires the project path to already exist. It fails with a
configuration error instead of creating arbitrary project directories.

## Schema Version Policy

MVP supports only `schema_version: 1`.

This applies to:

- global `config.json`;
- project `Skillfile.json`;
- skill `csk-skill.json`;
- installed `.csk-install.json`.

Unsupported input schema versions fail hard with a message that the file
requires a newer `csk`.

Unsupported global config or project manifest schemas are configuration errors
and return exit code `2`. Unsupported installed marker schemas make the owning
project fail because `csk` cannot safely update or clean it. Unsupported
`csk-skill.json` schemas fail installation for that skill.

## Reference Semantics

`tag`:

- Resolved to a commit from the local skill git repository.
- `csk install` does not fetch.
- `csk upgrade` may see a changed tag only after `csk update`, but mutable tags
  are discouraged.
- If the same tag already appears in an installed marker but resolves locally
  to a different commit, default behavior is to warn and reinstall at the new
  commit.
- With `--strict-tags`, moved tags are rejected and the project fails.

`branch`:

- Allowed for experiments.
- Resolved first as `origin/<branch>` if it exists.
- Falls back to local `<branch>` if no remote tracking ref exists.
- Repositories without remotes are supported through the same local branch
  fallback. Absence of a remote is not an error.
- The installed marker records both the branch name and the exact resolved
  commit.

`revision`:

- Resolved to an exact commit.
- Short SHAs may be accepted if git can resolve them unambiguously.

Source skill repositories must not be checked out or otherwise mutated during
install. `csk` uses a snapshot of a resolved commit, for example via
`git archive <commit>`.

Dirty worktrees in source skill repositories do not affect installation.
Only committed content can be installed.

Submodules are not supported in MVP. If a source skill repository contains
`.gitmodules`, installation of that skill fails with a clear error instead of
silently omitting submodule content from `git archive`.

## Snapshot Cache

`csk` may cache extracted git snapshots to avoid repeated `git archive` work
when multiple projects use the same skill commit.

Suggested cache layout:

```text
~/.cocoaskills/cache/<source>/<commit>/snapshot/
```

The cache is an implementation optimization. The source of truth remains the
resolved git commit, and cached snapshots must be safe to delete at any time.

Manual cache deletion is supported:

```text
rm -rf ~/.cocoaskills/cache
```

A future `csk cache clean` command may wrap this behavior, but it is not part of
MVP.

## CLI

MVP commands:

```text
csk bootstrap
csk install [alias]
csk update
csk upgrade [alias]
csk status [alias]
csk list
csk project add <alias> <path>
csk config show
csk shell-init [zsh|bash|powershell]
csk --help
csk <command> --help
csk --version
```

Flags:

```text
--dry-run
--verbose
--fix-gitignore
--strict-tags
```

Command behavior:

| Command | Behavior |
| --- | --- |
| `bootstrap` | Interactively creates global config, preferred locale, default agents, focused projects, and optionally shell hook instructions. |
| `install` | Applies `Skillfile.json` using current local git refs. It does not fetch. |
| `install <alias>` | Installs one configured project. |
| `update` | Fetches all git repositories under `skills_root`. It does not modify projects. |
| `upgrade` | Runs `update`, then `install`. This is the command that advances branch-based skills to newly fetched commits. |
| `upgrade <alias>` | Runs `update`, then installs one project. |
| `status` | Shows manifest vs installed marker state. |
| `list` | Shows configured projects and their declared skills. |
| `project add` | Adds a project to global config and creates an empty `Skillfile.json` if missing. |
| `config show` | Prints resolved config path and config content. |
| `shell-init` | Prints shell hook code for automatic project-local `PATH` activation. |
| `--help` | Prints top-level command help and local documentation index, then exits. |
| `<command> --help` | Prints command-specific documentation, examples, side effects, and exits. |
| `--version` | Prints the `csk` version and exits. |

`install` and `upgrade` are intentionally different:

- `install` means "apply manifests using already available local refs".
- `upgrade` means "fetch skill repositories and then apply manifests".

`csk install` is suitable for CI because it never fetches or mutates local
skill refs. CI that wants to reject locally moved tags should also use
`--strict-tags`.

There is no `csk uninstall` command in MVP. To uninstall a skill from a project,
remove it from `Skillfile.json` and run `csk install`; cleanup removes the
installed context, command shims, adapter entries, and unreferenced runtime
artifacts.

## Public Interface Documentation

All public `csk` interfaces must be documented before the MVP is considered
complete.

Public interface includes:

- all commands and flags;
- command behavior, side effects, and exit codes;
- global config path and JSON schema;
- project `Skillfile.json` schema;
- skill `csk-skill.json` schema;
- installed `.csk-install.json` marker schema;
- supported environment variables such as `CSK_CONFIG`;
- generated files that users or agents are expected to rely on, including
  `.agents/env.sh`, `.agents/env.ps1`, and `.agents/bin`;
- shell hook behavior;
- stable `status` labels.

Top-level `csk --help` and per-command `csk <command> --help` must match the
documented command surface. Any behavior not documented here or in user-facing
CLI help is not public API and may change without compatibility guarantees.

The shell must be enough to read documentation for all public commands. A user
must be able to discover command usage, flags, examples, generated files,
side effects, and relevant JSON schema details from `csk --help` and
`csk <command> --help` without opening markdown files.

Top-level help should list every command and point users to detailed command
help. Command help should include:

- synopsis;
- purpose;
- options and flags;
- required and optional arguments;
- files read and written;
- side effects;
- exit codes relevant to the command;
- one or more examples.

Adding or changing public behavior requires updating this design or a successor
user-facing documentation file in the same change.

## Bootstrap Flow

`csk bootstrap` asks questions in this order:

1. `skills_root`
2. `preferred_locale`
3. `default_agents`
4. projects to focus on, as an alias/path loop
5. whether to print or install shell hook instructions

The command writes `~/.cocoaskills/config.json` unless `CSK_CONFIG` is set.
It must not overwrite an existing config without explicit confirmation.

## Status Output

Human-readable `csk status` output should be stable enough to test.

Example:

```text
Project partners-app-ios (/Users/iv/Developer/Wildberries/partners-app-dev)
  skill-youtrack        tag v1.0.0      abc123d  up-to-date
  skill-grafana         branch main     f417beb  update-available -> 6078e3b
  logbook               revision 6deb71a          missing
```

Status labels:

| Label | Meaning |
| --- | --- |
| `up-to-date` | Installed marker matches the resolved local ref and content hash. |
| `missing` | Declared in `Skillfile.json` but not installed. |
| `update-available` | Declared ref resolves to a different local commit than the marker. |
| `content-drift` | Marker commit matches, but installed content hash differs. |
| `error` | The skill cannot be resolved or inspected. |

`--json` output is intentionally not part of MVP.

## Exit Codes

| Code | Meaning |
| --- | --- |
| `0` | Command succeeded. Warnings and skipped projects without `Skillfile.json` do not change this. |
| `1` | One or more projects or skills failed, but the command completed for everything else it could process. |
| `2` | Configuration, usage, malformed JSON, unsupported schema, or missing/invalid `skills_root`. |
| `3` | Global lock could not be acquired. |

`csk config show` and `csk list` may run even when `skills_root` is missing.
`csk install`, `csk update`, and `csk upgrade` fail with exit code `2` if
`skills_root` does not exist or contains no git repositories.

## Per-Project Transaction Model

`csk install` processes every configured project unless a single alias is
specified.

If one project fails:

- That project is reported as failed.
- Other projects continue.
- Final process exit code is non-zero.

Within a project, installation is transactional per skill. If a new version
fails to install, the previously installed version must remain usable.

Running `csk install` twice in a row without changes to `Skillfile.json`, local
skill refs, or installed content is a no-op for already up-to-date skills.

If a configured project does not have `Skillfile.json`, `csk install` warns and
skips that project. This is not a failure.

An empty `Skillfile.json` with `"skills": []` is a successful no-op and still
runs cleanup for previously installed skills.

## Gitignore Gate

Before installing into a project, `csk` verifies that generated directories are
ignored by git.

Required entries depend on selected agents:

```gitignore
.agents/
.claude/skills/
.codex/skills/
.gemini/skills/
.cursor/rules/
```

The check should use `git check-ignore` where possible.

If required paths are not ignored:

- Default behavior: skip the project and report the missing entries.
- With `--fix-gitignore`: append a managed block to the root `.gitignore`, then
  re-run the ignore check.

Managed block example:

```gitignore
# CocoaSkill
.agents/
.claude/skills/
.codex/skills/
.gemini/skills/
.cursor/rules/
```

`--fix-gitignore` must not rewrite the entire file and must avoid duplicate
entries.

## Install Layout

Project runtime skill context:

```text
<project>/.agents/skills/<skill-name>/
```

Project command layer:

```text
<project>/.agents/bin/
```

Generated project env files:

```text
<project>/.agents/env.sh
<project>/.agents/env.ps1
```

Global executable runtime store:

```text
~/.cocoaskills/runtime/<skill-name>/<commit>/bin/
```

Installed skill context directories contain skill instructions and operational
context only. Executable scripts declared as commands are installed into the
global runtime store and exposed through `<project>/.agents/bin`.

## Command Shims

Project command shims expose runtime scripts through stable command names.

Unix-like systems:

- `<project>/.agents/bin/<command>` is a symlink to
  `~/.cocoaskills/runtime/<skill>/<commit>/bin/<command>`.
- Runtime script files must be executable. When installing a declared script
  command, `csk` sets user/group/other executable bits as needed.

Windows:

- `<project>/.agents/bin/<command>.cmd` is a wrapper script pointing to the
  runtime store artifact.
- Windows shims are wrappers, not symlinks, because symlinks often require
  Developer Mode or elevated privileges.

Shims are regenerated on every install for declared skills and removed during
cleanup when no installed skill owns that command.

## Agent Adapters

`.agents/skills` is the canonical project-local skill context directory.

Agent adapter paths:

| Agent | Path |
| --- | --- |
| `codex_cli` | `.codex/skills/` |
| `claude_code` | `.claude/skills/` |
| `gemini` | `.gemini/skills/` |
| `cursor` | `.cursor/rules/` |

Adapter behavior:

- `adapter_mode: symlink`: create per-skill directory symlinks.
- `adapter_mode: copy`: copy installed skill context directories.
- `adapter_mode: auto`: prefer symlinks on Unix-like systems and fall back to
  copy when symlinks are unavailable, especially on Windows.

Adapter entries are refreshed on every install. In copy mode, an adapter
directory is replaced when the canonical `.agents/skills/<skill>` context
changes.

Adapter directories may contain user-authored content. `csk` must not delete
entries that it did not create. Each adapter root records csk-owned entries in:

```text
<adapter-root>/.csk-managed.json
```

Cleanup removes only entries recorded in `.csk-managed.json` and no longer
declared by the project manifest. If an expected adapter target already exists
and is not known to be csk-managed, installation fails instead of overwriting
user content.

When `adapter_mode: copy` copies installed skill context directories, the copied
adapter directory may contain `.csk-install.json`. That is acceptable for
debugging consistency, but `csk` always reads installed state only from the
canonical `.agents/skills/<skill>/.csk-install.json`.

Cursor is included in MVP as a simple directory adapter. Rich `.mdc` conversion
is deferred.

## Runtime Whitelist

Default included paths from a skill snapshot:

```text
SKILL.md
agents/
references/
.skill_triggers/
assets/
templates/
examples/
data/
dependencies.json
```

`SKILL.md` is required. If it is missing from the resolved snapshot, the skill
installation fails with a clear error.

Copy rules are authoritative in this order:

1. Copy only files under the default included paths above.
2. Then prune anything matching the always-excluded list below, even if it
   appears under an included root.

The always-excluded list wins for nested paths. For example,
`examples/tests/` is excluded even though `examples/` is an included root.

`locales/` is source metadata for rendering and is not copied into the runtime
skill context by default.

`dependencies.json` is copied as opaque legacy skill metadata. MVP does not
parse it or enforce dependency rules from it.

`scripts/` is not copied into the installed skill context when script commands
are declared through `csk-skill.json` or `agents/runtime.json`. Declared script
commands go to the runtime store instead.

Paths always excluded:

```text
.git/
.github/
.gitlab-ci.yml
.venv/
__pycache__/
*.pyc
node_modules/
tests/
test/
__tests__/
README*
CHANGELOG*
LICENSE*
Makefile
setup.py
pyproject.toml
requirements*.txt
.DS_Store
.gitignore
```

If a legacy skill does not declare script commands, MVP may copy `scripts/`
only if needed for compatibility. New skills should declare commands through
`csk-skill.json`.

## Skill Command Manifest

Optional file in a source skill repository:

```text
csk-skill.json
```

Example:

```json
{
  "schema_version": 1,
  "commands": {
    "ytx": {
      "type": "script",
      "unix_path": "scripts/ytx",
      "win_path": "scripts/ytx.cmd"
    },
    "glab": {
      "type": "system",
      "command": "glab",
      "hint": "Install GitLab CLI"
    }
  }
}
```

Command types:

| Type | Behavior |
| --- | --- |
| `script` | Copy the script from the skill snapshot into the global runtime store, then expose it through project `.agents/bin`. |
| `system` | Verify the command is present in `PATH` using `shutil.which`. Do not execute arbitrary checks from the manifest. |

For `script` commands:

- macOS/Linux use `unix_path`.
- Windows uses `win_path`.
- All declared script commands are required in MVP.
- Missing platform path fails installation for that skill.

For `system` commands:

- Presence check uses `shutil.which(command)`.
- No shell command from the manifest is executed.
- `hint` is displayed on failure.

Fallback for existing skills:

- If `csk-skill.json` is absent, read `agents/runtime.json.commands` as script
  command declarations where possible.
- If both `csk-skill.json` and `agents/runtime.json` exist, `csk-skill.json`
  is authoritative for command installation. `agents/runtime.json` may still be
  copied as skill context, but it is not used to derive command shims.

Command collision policy:

- If two installed skills in the same project export the same command name,
  installation for that project fails.
- The error must name both conflicting skills and the command.

## PATH Activation

`csk install` updates:

```text
<project>/.agents/bin
<project>/.agents/env.sh
<project>/.agents/env.ps1
```

`env.sh` adds project `.agents/bin` to `PATH`.

`env.ps1` adds project `.agents\bin` to `PATH`.

A child process cannot mutate the parent shell environment. Therefore automatic
activation requires a one-time shell hook.

`csk shell-init zsh` and `csk shell-init bash` print a hook that:

- runs on prompt or directory change;
- searches upward for `.agents/env.sh`;
- activates the nearest project env;
- restores previous `PATH` when leaving the project.

`csk shell-init powershell` prints an equivalent PowerShell profile hook using
`.agents/env.ps1`.

`csk bootstrap` should offer to install or print shell hook instructions.

If shell hook is not installed, `csk install` still succeeds but warns that
agent processes launched from the shell may not see project `.agents/bin`.

## Locale Policy

Locale resolution order:

1. `Skillfile.json.locale`
2. Global `preferred_locale`
3. No locale rendering

MVP supports a single selected locale only. There is no fallback chain such as
`["ru", "en"]`.

If a skill does not have locale metadata, `SKILL.md` is copied as-is.

If a skill has `locales/metadata.json` and `.skill_triggers/`, then:

- the selected locale must exist;
- `.skill_triggers/<locale>.md` must exist;
- `SKILL.md` frontmatter description and triggers may be rendered from the
  selected locale;
- `agents/openai.yaml` may be rendered when it matches the existing
  skill-local-install contract.

If locale metadata exists but the selected locale is unsupported, installation
fails for that skill.

## Installed Marker

Each installed skill writes:

```text
<project>/.agents/skills/<skill>/.csk-install.json
```

Example:

```json
{
  "schema_version": 1,
  "name": "skill-youtrack",
  "source": "skill-youtrack",
  "ref_kind": "branch",
  "ref": "main",
  "commit": "abc123def4567890",
  "content_sha256": "sha256:ed25c9a7f83e8bb47a0f9a17fbd351c54b4f17d2a9e7baf7f3406fdb07f45612",
  "locale": "ru",
  "agents": ["codex_cli", "claude_code", "cursor"],
  "commands": ["yt", "ytx"],
  "installed_at": "2026-05-07T18:05:00Z",
  "files": [
    "SKILL.md",
    "agents/openai.yaml",
    "references/usage.md"
  ]
}
```

`content_sha256` is computed over the installed skill context directory, not
over command scripts in the runtime store.

## Hashing

Content hash algorithm:

1. Enumerate files recursively under the installed skill context directory.
2. Exclude `.csk-install.json` from the hash.
3. Sort relative paths lexicographically with `/` separators.
4. For each file, append `relative_path`, NUL byte, and raw file bytes.
5. Append a NUL byte between consecutive file entries.
6. Hash the concatenated payload with SHA-256.
7. Store as `sha256:<hex>`.

No line ending normalization is performed.

## Atomic Install

Per skill:

1. Build the new installed context in a temporary directory under
   `.agents/skills`.
2. Validate required files and compute content hash.
3. Write `.csk-install.json`.
4. Atomically replace the previous installed skill directory when possible.

If replacement fails, the previous installed version must remain usable.

On Windows, directory replacement may require a remove-and-rename fallback.
The implementation must avoid deleting the previous version before the new one
has been fully prepared.

## Cleanup And Garbage Collection

Project cleanup:

- Remove installed skill directories that are no longer declared in
  `Skillfile.json`.
- Remove project command shims for commands no longer exported by declared
  skills.
- Remove stale adapter entries generated by `csk`.

Runtime GC:

- Runtime scripts live under
  `~/.cocoaskills/runtime/<skill-name>/<commit>/bin`.
- After install or upgrade, scan all configured projects and their installed
  `.csk-install.json` files.
- Delete runtime directories that are no longer referenced by any configured
  project.

## Locking

Use a global lock to avoid concurrent installs corrupting shared runtime state:

```text
~/.cocoaskills/.lock
```

MVP can use a simple lock file with timeout and clear stale-lock messaging.

Default lock acquisition timeout is 30 seconds.

On timeout, `csk` exits with code `3` and reports:

- lock path;
- holder PID if known;
- lock creation time if known;
- instruction to remove the lock only if the user has verified that the process
  is stale.

## Security Boundary

MVP safety rules:

- Do not execute arbitrary commands from skill manifests.
- System dependencies are checked only with `shutil.which`.
- All manifest paths must be relative.
- Absolute paths are rejected.
- Path traversal using `..` is rejected.
- Runtime command source paths must resolve inside the skill snapshot.
- Source repositories are not checked out or modified.
- Only committed git content can be installed.

## Future Audit Command

Reserve the command:

```text
csk audit
```

This command is not implemented in MVP.

Future intended behavior:

- Fetch or refresh skill repositories.
- Inspect executable scripts and command manifests.
- Ask an agent or audit engine to analyze scripts for suspicious behavior,
  unsafe dependency usage, credential exfiltration, path traversal, hidden
  network calls, or other risky patterns.
- Produce a structured report with pass/warn/fail findings per skill and per
  project.

The MVP implementation should not build partial audit behavior into `install`.
The command is reserved so the CLI surface can grow without changing the core
install semantics.

## Test Strategy

MVP should have broad automated coverage using temporary fixture repositories.

Required areas:

- Global config read/write and `CSK_CONFIG` override.
- Public CLI help covers every supported command and flag.
- Per-command help exposes shell-readable documentation, examples, side
  effects, files touched, and relevant exit codes.
- `bootstrap` config creation.
- `project add` updates config and creates empty `Skillfile.json`.
- Unsupported schema versions fail as specified.
- Missing `Skillfile.json` warns and skips.
- Empty `Skillfile.json` is a successful no-op and cleans old installs.
- Missing or empty `skills_root` fails install/update/upgrade with exit code
  `2`.
- Duplicate skill names in one project fail.
- `tag`, `branch`, and `revision` resolution.
- Moved tags warn and reinstall by default.
- Moved tags fail with `--strict-tags`.
- Branch resolution works without a remote when a local branch exists.
- `install` does not fetch.
- `update` fetches skill repositories and does not modify projects.
- `upgrade` performs update then install.
- Source skill repositories are not checked out or mutated.
- Repositories with `.gitmodules` fail as unsupported.
- Gitignore gate blocks installation.
- `--fix-gitignore` appends only missing entries.
- Runtime whitelist excludes README, tests, venv, git, and other dev artifacts.
- Nested excluded paths are pruned even under included whitelist roots.
- `dependencies.json` is copied but not parsed.
- Missing `SKILL.md` fails skill installation.
- `csk-skill.json` takes precedence over `agents/runtime.json`.
- Declared script commands are installed to global runtime, not project skill
  context.
- Project `.agents/bin` shims are generated and updated.
- Unix shims are symlinks and runtime scripts are executable.
- Windows shims are `.cmd` wrappers.
- `.agents/env.sh` and `.agents/env.ps1` are generated.
- System dependency checks use `shutil.which`, not shell execution.
- Command collisions fail with clear errors.
- Locale selection from project and global config.
- Locale fallback chains are not applied in MVP.
- Unsupported locale fails when locale metadata exists.
- Atomic install preserves previous version on failure.
- Cleanup removes skills no longer declared.
- Runtime GC removes unreferenced runtime directories.
- Lock contention exits with code `3`.
- `status` output labels are stable.
- `csk install` is idempotent when inputs are unchanged.
- Adapter mode behavior for symlink, copy, and auto fallback.
- Copy-mode adapters refresh when canonical content changes.
- Adapter copy mode may copy `.csk-install.json`, but state reads use the
  canonical marker.
- Windows path and shim behavior through platform abstraction tests.
