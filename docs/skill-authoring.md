# CocoaSkills Skill Authoring Guide

This guide defines the recommended contract for CocoaSkills-compatible skill
repositories. It is the practical author-facing companion to
[RFC 0003](v0.5-design.md).

## 1. Repository Layout

Recommended layout:

```text
skill-example/
  SKILL.md
  csk-skill.json
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
csk-skill.json
```

`csk-skill.json` is the machine-readable runtime manifest. It declares command
entrypoints and system dependencies.

## 3. `csk-skill.json` Schema Versions

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

Schema v2 is the preferred format for internal skills:

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
    "gmr": {
      "type": "script",
      "unix_path": "scripts/gmr",
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

## 6. System Dependencies

System dependencies are commands the skill needs but does not own.

Example:

```json
{
  "schema_version": 2,
  "commands": {
    "glab": {
      "type": "system",
      "command": "glab",
      "hint": "Install GitLab CLI through project bootstrap tooling"
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

Project bootstrap tooling owns system dependencies. In partners-ios this means
Mise, Make, or a project bootstrap script, not the skill manager.

## 7. Prompt Context Contract

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

## 8. Recommended Internal Skill Manifests

### skill-youtrack

```json
{
  "schema_version": 2,
  "runtime_roots": ["scripts"],
  "commands": {
    "yt": {
      "type": "script",
      "unix_path": "scripts/yt"
    },
    "ytx": {
      "type": "script",
      "unix_path": "scripts/ytx"
    }
  }
}
```

### skill-gitlab

```json
{
  "schema_version": 2,
  "runtime_roots": ["scripts"],
  "commands": {
    "gmr": {
      "type": "script",
      "unix_path": "scripts/gmr"
    },
    "glab": {
      "type": "system",
      "command": "glab",
      "hint": "Install GitLab CLI through project bootstrap tooling"
    }
  }
}
```

### skill-sentry

```json
{
  "schema_version": 2,
  "runtime_roots": ["scripts"],
  "commands": {
    "sentry-api": {
      "type": "script",
      "unix_path": "scripts/sentry-api"
    },
    "sentry-cli-auth": {
      "type": "script",
      "unix_path": "scripts/sentry-cli-auth"
    },
    "sentry-cli": {
      "type": "system",
      "command": "sentry-cli",
      "hint": "Install Sentry CLI through project bootstrap tooling"
    }
  }
}
```

## 9. Global and Project Installation

Skill authors do not need a separate manifest for global use. The same
`SKILL.md` and `csk-skill.json` are valid when the skill is installed:

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

## 10. Release Checklist

Before tagging a skill release:

1. Validate `csk-skill.json` by running `csk install` in a real project or
   disposable fixture project.
2. Confirm runtime roots are absent from `.agents/skills/<skill>/`.
3. Confirm runtime files are present under
   `~/.cocoaskills/runtime/<skill>/<commit>/`.
4. Confirm project commands are available through `.agents/bin`.
5. Confirm missing system dependencies fail with a clear hint.
6. Confirm `SKILL.md` and `references/` contain all agent-facing instructions.
7. Tag the skill repository.
8. Update consuming project `Skillfile.json` to the new tag.
9. Run `csk install` and `csk status`.

## 11. Migration Notes

For existing skills with `agents/runtime.json`:

1. Add `csk-skill.json` schema v2.
2. Keep `agents/runtime.json` during the first rollout.
3. Release and consume the new tag in a real project.
4. After observation, remove legacy `agents/runtime.json` and
   `dependencies.json` if they are no longer needed.

`csk-skill.json` takes precedence over `agents/runtime.json` when both exist.
