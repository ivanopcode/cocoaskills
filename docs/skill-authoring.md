# CocoaSkills Skill Authoring Guide

This guide defines the recommended contract for CocoaSkills-compatible skill
repositories. It is the practical author-facing companion to
[RFC 0003](v0.5-design.md).

## 1. Repository Layout

Recommended layout:

```text
skill-example/
  SKILL.md
  agent-skill.json
  agents/
  references/
  assets/
  templates/
  examples/
  data/
  scripts/
  .skill_triggers/
```

Installed prompt context is intentionally stripped. CocoaSkills copies only
skill-facing content into `<project>/.agents/skills/<skill>/`.

Runtime-only files belong in `runtime_roots` and are copied to:

```text
~/.cocoaskills/runtime/<skill>/<commit>/
```

Do not rely on README files, tests, build files, virtualenvs, package metadata,
or CI files being available to the agent after install.

## 2. Required Files

Every installable skill must contain:

```text
SKILL.md
```

`SKILL.md` is the agent-facing contract. It should describe how the agent uses
the skill, which commands are available, and what inputs/outputs those commands
expect.

If the skill exports commands, add:

```text
agent-skill.json
```

`agent-skill.json` is the machine-readable runtime manifest. It declares command
entrypoints exported by the skill and dependencies consumed by the skill.

## 3. `agent-skill.json` Schema Versions

### Schema v1

Schema v1 is the compatibility mode:

```json
{
  "schema_version": 1,
  "commands": {
    "tool": {
      "type": "script",
      "unix_path": "scripts/tool"
    }
  }
}
```

Use schema v1 only for single-file scripts that do not depend on sibling files.
CocoaSkills copies each script command as one file into:

```text
~/.cocoaskills/runtime/<skill>/<commit>/bin/<command>
```

### Schema v2

Schema v2 is the runtime format for multi-file command skills:

```json
{
  "schema_version": 2,
  "runtime_roots": ["scripts"],
  "commands": {
    "tool": {
      "type": "script",
      "unix_path": "scripts/tool"
    }
  }
}
```

Use schema v2 when a command depends on sibling files, libraries, Python
modules, shell helpers, or other runtime assets.

### Schema v3

Schema v3 adds an explicit capability envelope for audit:

```json
{
  "schema_version": 3,
  "runtime_roots": ["scripts"],
  "capabilities": {
    "network": ["gitlab.example.com"],
    "filesystem": "repo",
    "exec": ["glab"],
    "secrets": "none",
    "env_read": ["HOME"],
    "prompt_scope": "Read merge request metadata and prepare local review output."
  },
  "commands": {
    "review": {
      "type": "script",
      "unix_path": "scripts/review"
    }
  },
  "dependencies": {
    "commands": {
      "glab": {
        "type": "system",
        "command": "glab",
        "hint": "Install GitLab CLI through project bootstrap tooling"
      }
    }
  }
}
```

Use schema v3 for skills that should pass strict audit. Schema v1 and v2 remain
installable, but strict audit treats them as undeclared: the skill must either
move to schema v3 or be explicitly pinned by content hash when the trust
workflow is used.

Capability fields:

- `network`: `"none"` or host globs the skill code may contact.
- `filesystem`: `"repo"`, `"home-config"`, or explicit paths.
- `exec`: `"none"` or executable names the skill may call.
- `secrets`: `"none"` or secret/keyring names the skill may read.
- `env_read`: environment variables the skill may read.
- `prompt_scope`: one sentence describing what the prompt is allowed to ask the
  agent to do.

### Schema v4

Schema v4 adds skill-to-skill requirements. A skill declares the skills it
builds on under `dependencies.skills`; csk resolves the transitive closure and
installs the providers. The full design is
[RFC 0007](v0.9-design.md).

```json
{
  "schema_version": 4,
  "runtime_roots": ["scripts"],
  "capabilities": {
    "exec": ["trk", "git"],
    "network": "none"
  },
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
    },
    "commands": {
      "git": { "type": "system", "command": "git" }
    }
  }
}
```

Requirement rules:

- `git` and `ref` are required; an entry is self-contained.
- `ref.kind` is `tag` or `revision`. Branch refs and version ranges are parse
  errors.
- `mode` selects what the provider contributes to the consumer:
  `full` (default) activates the prompt context and all exported commands,
  `runtime` activates commands only, `context` activates the prompt context
  only. The optional `commands` list narrows a `runtime` requirement to the
  named exports.
- Within one install closure a skill name resolves to one commit and one
  canonical source; disagreeing requirements fail the install.
- A workflow is a skill that declares requirements and exports no commands.
  Consumers install it with a single `Skillfile.json` entry.
- For development, `Skillfile.dev.json` next to the project `Skillfile.json`
  substitutes providers locally (a `path` to a checkout, or `git` with any ref
  kind, branches included). The file belongs to the managed `.gitignore`
  block; strict audit fails while substitutions are active.
- Organizations restrict where skills may be fetched from with
  `allowed_sources` in `~/.cocoaskills/config.json`: a list of canonical
  `host/path` prefixes checked before any clone or fetch.

### Schema v5

Schema v5 adds MCP server dependencies. A skill declares the MCP servers it
relies on under `dependencies.mcp_servers`; `csk install` verifies that each
server is configured in the target agent environments before the skill
lands. csk never provisions MCP servers, the check is read-only.

```json
{
  "schema_version": 5,
  "capabilities": { "exec": "none", "network": "none" },
  "dependencies": {
    "mcp_servers": {
      "sheets": {
        "hint": "Add the sheets MCP server to your agent configuration.",
        "transport": "http",
        "required_in": "any"
      }
    }
  }
}
```

MCP dependency rules:

- `hint` is required and tells the operator how to connect the server.
- `transport` is optional documentation: `stdio` or `http`.
- `required_in` selects the check semantics: `any` (default) requires the
  server in at least one target agent environment, `all` requires it in
  every one.
- Configuration surfaces checked per agent: Claude Code (`<project>/.mcp.json`,
  `~/.claude.json`), Codex CLI (`~/.codex/config.toml`), Cursor
  (`<project>/.cursor/mcp.json`, `~/.cursor/mcp.json`), Gemini
  (`~/.gemini/settings.json`). Missing or malformed files count as
  configuring no servers.
- A failed check stops the install with the hint; install markers record
  where each server was found.

## 4. Runtime Roots

`runtime_roots` lists directories that are runtime-only. CocoaSkills copies
them into the global runtime store and excludes them from installed prompt
context.

Rules:

- `runtime_roots` is optional. Default: `[]`.
- Each root is a relative POSIX path inside the skill repository.
- No leading `/`.
- No `..`.
- No empty path component.
- The root must exist.
- The root must be a directory.
- Roots must be unique after stripping trailing slashes.
- Roots must be disjoint: `["scripts", "scripts/lib"]` is invalid.
- Comparison is case-sensitive.

Good:

```json
{
  "schema_version": 2,
  "runtime_roots": ["scripts"]
}
```

Bad:

```json
{"runtime_roots": ["/scripts"]}
{"runtime_roots": ["../scripts"]}
{"runtime_roots": ["scripts", "scripts/lib"]}
```

## 5. Script Commands

Script commands expose skill-owned executables through project-local
`<project>/.agents/bin`.

Example:

```json
{
  "schema_version": 2,
  "runtime_roots": ["scripts"],
  "commands": {
    "mr": {
      "type": "script",
      "unix_path": "scripts/mr",
      "win_path": "scripts/gmr.cmd"
    }
  }
}
```

Schema v2 rules:

- Allowed fields: `type`, `unix_path`, `win_path`.
- At least one platform path is required.
- Paths must be relative POSIX paths.
- Paths must not contain `..`.
- If `runtime_roots` is non-empty, every script path must be inside one of
  those roots.
- The path must exist and must be a file.

### Agent-facing command resolution

`runtime_roots` are intentionally absent from installed prompt context. A
`SKILL.md` or prompt-visible reference must therefore never assume that a
manifest path such as `scripts/tool` exists next to the installed skill.

Define placeholders such as `<tool-command>` and resolve each exported command
once, before its first invocation:

1. When `.csk-install.json` is present next to `SKILL.md`, search upward from
   the current working directory, then from the physical `SKILL.md` path, for
   the nearest `<ancestor>/.agents/bin/<command>` (`.cmd` on Windows).
2. If there is no project shim, use `<csk-home>/global/bin/<command>`
   (`<command>.cmd` on Windows), where `<csk-home>` is the parent of
   `CSK_CONFIG` or `~/.cocoaskills` by default.
3. Use a bare command only as a final fallback after `command -v` or
   `Get-Command` confirms it exists.
4. When `.csk-install.json` is absent, treat the skill as a source checkout:
   read the platform entrypoint from `agent-skill.json` and resolve that path
   relative to the physical skill directory.
5. If no declared command can be found, report an incomplete installation and
   stop. Do not guess a runtime path or execute one relative to the current
   working directory.

The search from both the working directory and the physical skill path matters:
agent adapters can be symlinks or copies, while project-local commands must
still shadow global commands. Do not derive the command solely as a fixed
number of `..` components from an adapter-visible `SKILL.md` path.

Apply the same rule to workflow skills that consume commands from
`dependencies.skills` or legacy `dependencies.commands` entries. Their own
`runtime_roots` may be empty, but provider source paths are still unavailable
after installation.

Shell activation is never a prerequisite for agent execution. Authors must
keep the explicit project/global resolver even when their own interactive shell
already exposes the command through `PATH`; `csk skill check` warns when a
managed command lacks this shell-neutral contract.

`csk skill check` warns when prompt-visible Markdown refers to a runtime-only
root or guesses a provider's source runtime. Human-only source development
commands belong in `README.md`, which is not copied into prompt context.

Command entrypoints should resolve their own directory before loading sibling
files. For POSIX shell scripts:

```bash
#!/usr/bin/env bash
set -euo pipefail

source_path="${BASH_SOURCE[0]}"
while [[ -L "$source_path" ]]; do
  target_path="$(readlink "$source_path")"
  if [[ "$target_path" == /* ]]; then
    source_path="$target_path"
  else
    source_path="$(cd -P -- "$(dirname -- "$source_path")" && pwd)/$target_path"
  fi
done

script_dir="$(cd -P -- "$(dirname -- "$source_path")" && pwd)"
exec python3 "$script_dir/main.py" "$@"
```

This pattern works when project `.agents/bin/<command>` is a symlink to the
runtime store.

## 6. Dependencies

`commands` is only for commands exported by the current skill. Dependencies
belong under `dependencies.commands`.

Do not declare a command in `commands` merely because the skill calls it. That
turns the command name into an exported CocoaSkills command and can collide with
the skill that actually provides it.

### System command dependencies

System command dependencies are commands the skill needs but does not own and
that are installed by the machine or project bootstrap.

Example:

```json
{
  "schema_version": 2,
  "dependencies": {
    "commands": {
      "review-cli": {
        "type": "system",
        "command": "review-cli",
        "hint": "Install the review CLI through project bootstrap tooling"
      }
    }
  }
}
```

Rules:

- Allowed fields: `type`, `command`, `hint`.
- `command` is required.
- `hint` is optional.
- CocoaSkills checks presence with `shutil.which(command)`.
- CocoaSkills never installs system dependencies.
- CocoaSkills never executes manifest-provided checks.

Forbidden fields:

- `install`
- `check`
- `post_install`
- `script`
- `command_args`

If a system dependency is missing, `csk install` fails before writing runtime
files, project context, or shims for that skill.

Project bootstrap tooling owns system dependencies. In demo-ios this means
Mise, Make, or a project bootstrap script, not the skill manager.

### Skill command dependencies

Skill command dependencies are commands exported by another skill in the same
`Skillfile.json`.

Example:

```json
{
  "schema_version": 2,
  "dependencies": {
    "commands": {
      "wk": {
        "type": "skill",
        "skill": "skill-docs",
        "command": "wk",
        "hint": "Add skill-docs to Skillfile.json before this skill."
      }
    }
  }
}
```

Rules:

- Allowed fields: `type`, `skill`, `command`, `hint`.
- `skill` is the provider skill name from `Skillfile.json`. Consumers must
  declare the provider under this exact canonical name.
- `command` is the script command exported by that provider skill.
- The `dependencies.commands` map key is a local dependency id used in markers
  and diagnostics. For skill command dependencies, keep it equal to `command`
  unless a dependency needs a distinct local name.
- `hint` is optional.
- The provider skill must be in the same install plan.
- The provider must export the requested command as a `script` command.
- Skill command dependencies are not installed as new shims by the consuming
  skill and do not participate in command collision detection.

Good:

```json
{
  "schema_version": 2,
  "commands": {},
  "dependencies": {
    "commands": {
      "wk": {
        "type": "skill",
        "skill": "skill-docs",
        "command": "wk"
      }
    }
  }
}
```

Bad:

```json
{
  "schema_version": 2,
  "commands": {
    "review-cli": {
      "type": "system",
      "command": "review-cli",
      "hint": "This is a dependency, not an export."
    }
  }
}
```

Legacy manifests with `type: system` entries under `commands` remain accepted
for compatibility, but new skills should use `dependencies.commands`.

## 7. Localization Contract

Localization is optional. If the skill ships no `locales/metadata.json` and no
`.skill_triggers/` directory, installs are unaffected regardless of the
project's `locale` setting.

Once the skill ships either of them, a locale is considered consistent only
when it appears in both:

- `locales/metadata.json` with a `locales.<locale>` object (its `description`
  replaces the `SKILL.md` frontmatter description);
- `.skill_triggers/<locale>.md` with the trigger catalog for that locale.

At least one consistent locale is required when localization is present. If the
selected locale is missing but another locale is consistent, CocoaSkills
installs the source `SKILL.md` with a warning instead of failing.

## 8. Validate a Skill

Use `csk skill check` before tagging a skill:

```bash
csk skill check .
csk skill check . --locale ru
csk skill check . --json
```

The command validates intrinsic skill requirements in the working tree:
`SKILL.md`, `agent-skill.json`, runtime roots, command shape, and locale catalog
consistency. It also warns when prompt-visible Markdown points into a
runtime-only source directory that will be absent after install. It does not
require `~/.cocoaskills/config.json`, `Skillfile.json`, or project setup.

`csk skill check` reads the working tree as-is. `csk install` validates the
committed git snapshot resolved from a consuming project's `Skillfile.json`, so
uncommitted local files can make the two commands differ.

System command presence is environment-specific and remains an install-time
check. `csk skill check` validates that `dependencies.commands` entries are
declared correctly, but it does not require system commands to exist on the
author's machine.

Locale catalogs are valid when at least one locale appears in both
`locales/metadata.json` and `.skill_triggers/<locale>.md`. When a selected
locale is missing but another locale is consistent, CocoaSkills installs the
source `SKILL.md` with a warning instead of failing.

Do not add `dependencies.json`. It is no longer copied by CocoaSkills.
Dependencies belong in `agent-skill.json` under `dependencies.commands`.

## 9. Prompt Context Contract

Agent-facing files should be placed in prompt context roots:

- `SKILL.md`
- `agents/`
- `references/`
- `.skill_triggers/`
- `assets/`
- `templates/`
- `examples/`
- `data/`

Runtime-only code should be placed under `runtime_roots`, usually `scripts/`.

One legacy exception: a skill that declares no commands at all gets its
`scripts/` directory copied into prompt context, because nothing marks those
files as runtime-only. Declare commands (or schema v2 `runtime_roots`) to keep
scripts out of the agent's context window.

Do not assume these are copied into prompt context:

- `README*`
- `CHANGELOG*`
- `LICENSE*`
- `tests/`
- `.github/`
- `.gitlab-ci.yml`
- `.venv/`
- `node_modules/`
- `pyproject.toml`
- `requirements*.txt`
- `Makefile`

If the agent needs operational information, put it in `SKILL.md` or
`references/`, not in `README.md`.

## 10. Example Skill Manifests

### skill-tracker

```json
{
  "schema_version": 2,
  "runtime_roots": ["scripts"],
  "commands": {
    "trk": {
      "type": "script",
      "unix_path": "scripts/trk"
    },
    "trkx": {
      "type": "script",
      "unix_path": "scripts/trkx"
    }
  }
}
```

### skill-review

```json
{
  "schema_version": 2,
  "runtime_roots": ["scripts"],
  "commands": {
    "mr": {
      "type": "script",
      "unix_path": "scripts/mr"
    }
  },
  "dependencies": {
    "commands": {
      "review-cli": {
        "type": "system",
        "command": "review-cli",
        "hint": "Install the review CLI through project bootstrap tooling"
      }
    }
  }
}
```

### skill-monitor

```json
{
  "schema_version": 2,
  "runtime_roots": ["scripts"],
  "commands": {
    "monitor-api": {
      "type": "script",
      "unix_path": "scripts/monitor-api"
    },
    "monitor-cli-auth": {
      "type": "script",
      "unix_path": "scripts/monitor-cli-auth"
    }
  },
  "dependencies": {
    "commands": {
      "monitor-cli": {
        "type": "system",
        "command": "monitor-cli",
        "hint": "Install the monitor CLI through project bootstrap tooling"
      }
    }
  }
}
```

## 11. Global and Project Installation

Skill authors do not need a separate manifest for global use. The same
`SKILL.md` and `agent-skill.json` are valid when the skill is installed:

- into a project through `csk install`;
- globally through `csk global install`.

Global installation changes only the target scope:

```text
project scope: <project>/.agents/skills/<skill>/
global scope:  ~/.cocoaskills/global/skills/<skill>/
runtime:       ~/.cocoaskills/runtime/<skill>/<commit>/
```

The runtime store is shared. A project can pin a different commit of the same
skill; project-local commands and agent adapters shadow global ones inside that
checkout.

Do not make a skill depend on being global. Project `Skillfile.json`
declarations remain the source of truth for project behavior.

## 12. Release Checklist

Before tagging a skill release:

1. Validate the working tree with `csk skill check . --locale <locale>`.
2. Validate `agent-skill.json` by running `csk install` in a real project or
   disposable fixture project.
3. Confirm runtime roots are absent from `.agents/skills/<skill>/`.
4. Confirm runtime files are present under
   `~/.cocoaskills/runtime/<skill>/<commit>/`.
5. Confirm project commands are available through `.agents/bin`.
6. Confirm command resolution still works when `.agents/bin` is not already on
   `PATH`, including copied adapter mode.
7. Confirm `SKILL.md` and prompt-visible references contain no executable path
   into a runtime root.
8. Confirm missing system dependencies fail with a clear hint.
9. Confirm `SKILL.md` and `references/` contain all agent-facing instructions.
10. Tag the skill repository.
11. Update consuming project `Skillfile.json` to the new tag.
12. Run `csk install` and `csk status`.

## 13. Migration Notes

For existing skills with `csk-skill.json`:

1. Rename it to `agent-skill.json` without changing the JSON value.
2. Update documentation and automation to emit only `agent-skill.json`.
3. If a staged rollout temporarily needs both files, keep their decoded JSON
   values equal. CocoaSkills rejects a mismatch with
   `conflicting_skill_manifests`.

The legacy filename remains readable throughout protocol 1.x, so consumers do
not need a flag day.

For existing skills with `agents/runtime.json`:

1. Add `agent-skill.json` schema v2.
2. Keep `agents/runtime.json` during the first rollout.
3. Release and consume the new tag in a real project.
4. After observation, remove legacy `agents/runtime.json`.

`agents/runtime.json` is read only when neither `agent-skill.json` nor the
legacy `csk-skill.json` exists.
