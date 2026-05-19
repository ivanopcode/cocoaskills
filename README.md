# CocoaSkill

[![PyPI](https://img.shields.io/pypi/v/cocoaskills.svg)](https://pypi.org/project/cocoaskills/)
[![Python versions](https://img.shields.io/pypi/pyversions/cocoaskills.svg)](https://pypi.org/project/cocoaskills/)
[![License](https://img.shields.io/pypi/l/cocoaskills.svg)](https://github.com/ivanopcode/cocoaskills/blob/main/LICENSE)
[![CI](https://github.com/ivanopcode/cocoaskills/actions/workflows/ci.yml/badge.svg)](https://github.com/ivanopcode/cocoaskills/actions/workflows/ci.yml)

`csk` is a local skill manager for AI agent skills. It installs reusable skill
packages from local git repositories into your project repositories with
reproducible, content-hashed installs and multi-agent adapter support
(Claude Code, Codex CLI, Cursor, Gemini).

The MVP design contract is frozen in [docs/mvp-design.md](docs/mvp-design.md).

## Why

Agent skills are useful, but managing them across many projects by hand falls
apart fast: drift between machines, no version pinning, README files and tests
leaking into the agent context, no cleanup when a skill is removed.

CocoaSkill makes per-project skill installation declarative and reproducible:

- One `Skillfile.json` per project, committed to version control.
- Pinned git refs (tag / branch / revision) and content-hashed installs.
- A whitelist-based stripped layout — README, tests, build files, and other
  non-skill content stay out of the agent's context.
- One canonical location (`.agents/skills/`) with per-agent adapter symlinks
  or copies into `.claude/skills/`, `.codex/skills/`, `.cursor/rules/`,
  `.gemini/skills/`.
- Skill-provided command shims exposed via a project-local `.agents/bin/`
  directory on `PATH`.

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

3. Initialize CocoaSkill in each project:

   ```bash
   cd /path/to/project
   csk init
   ```

   This creates `Skillfile.json` and adds the CocoaSkill generated paths to
   `.gitignore`.

4. Declare which skills you want:

   ```json
   {
     "schema_version": 1,
     "project": { "alias": "partners-ios" },
     "agents": ["claude_code", "codex_cli", "cursor"],
     "locale": "en",
     "skills": [
       {
         "name": "skill-youtrack",
         "git": "git@gitlab.example.com:agentic-infra/skill-youtrack.git",
         "tag": "v1.0.0"
       },
       {
         "name": "skill-grafana",
         "source": "internal/skill-grafana",
         "branch": "main"
       }
     ]
   }
   ```

5. Run `csk install` inside the checkout.

For multi-project sync, explicitly register projects with `csk project add` and
run `csk install --all` or `csk upgrade --all`.

## Skill command manifests

Skills can declare project-local commands through `csk-skill.json`. Schema v2
supports multi-file runtimes: `runtime_roots` are copied into
`~/.cocoaskills/runtime/<skill>/<commit>/` and excluded from agent prompt
context.

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

`system` commands are only checked with `shutil.which`; CocoaSkills does not
install system tools.

## CLI

| Command | Behavior |
|---|---|
| `csk bootstrap` | Interactively create machine-level global config. |
| `csk init [path]` | Create project `Skillfile.json` and the managed `.gitignore` block. |
| `csk install [target]` | Apply `Skillfile.json` using current git refs. Missing `git` URL sources are cloned into `skills_root`; existing local repositories are not fetched. No target means current project; `target` may be an alias, `.`, or a project path. |
| `csk install --all` | Install every project explicitly registered in global config. |
| `csk update` | Fetch all git repositories under `skills_root`. Does not modify projects. |
| `csk upgrade [target]` | Run `update`, then `install`. |
| `csk upgrade --all` | Run `update`, then install every registered project. |
| `csk status [target]` | Show manifest vs installed state. No target means current project. |
| `csk status --all` | Show status for every registered project. |
| `csk list [--paths]` | List configured projects and declared skills. |
| `csk project add <alias> <path>` | Register a project for `--all` and create a manifest if missing. |
| `csk project resolve [target]` | Show resolved project alias, checkout alias, Skillfile, and install paths. |
| `csk config show` | Print resolved config path and contents. |
| `csk shell-init [zsh\|bash\|powershell]` | Print shell hook code for auto-`PATH` activation. |
| `csk --version` | Print version and exit. |

Flags shared by `install` and `upgrade`:

- `--dry-run` — plan work without modifying files.
- `--verbose` — print detailed progress.
- `--fix-gitignore` — deprecated escape hatch; prefer `csk init`.
- `--strict-tags` — fail if a tag was locally moved to another commit.

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

## Documentation

- [Skill authoring guide](docs/skill-authoring.md) — practical contract for
  authoring CocoaSkills-compatible skill repositories, including
  `csk-skill.json` schema v2, `runtime_roots`, system dependencies, and release
  checklist.
- [MVP design specification](docs/mvp-design.md) — frozen contract for v0.1
  covering manifests, refs, install pipeline, locking, adapters, security
  boundary, and test surface.
- [CHANGELOG](CHANGELOG.md) — release history in Keep a Changelog format.

## License

Apache-2.0. See [LICENSE](LICENSE).
