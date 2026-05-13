# CocoaSkill

[![PyPI](https://img.shields.io/pypi/v/cocoaskill.svg)](https://pypi.org/project/cocoaskill/)
[![Python versions](https://img.shields.io/pypi/pyversions/cocoaskill.svg)](https://pypi.org/project/cocoaskill/)
[![License](https://img.shields.io/pypi/l/cocoaskill.svg)](https://github.com/ivanopcode/cocoaskills/blob/main/LICENSE)
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
pipx install cocoaskill
```

### uv tool

```bash
uv tool install cocoaskill
```

### Homebrew (macOS, Linux)

```bash
brew tap ivanopcode/csk
brew install cocoaskill
```

### mise

```bash
mise use -g pipx:cocoaskill@latest
```

### Convenience install script

```bash
curl -fsSL https://cocoaskills.cc/install.sh | sh
```

The script detects Python, prefers `pipx` or `uv tool`, and falls back to
`pip install --user`. Read it before piping if you do not trust the network.

### Plain pip

```bash
python -m pip install --user cocoaskill
```

## Quick start

1. Pick or create a directory of cloned skill git repositories. Example:
   `~/agents/skills/`.

2. Bootstrap the global config:

   ```bash
   csk bootstrap
   ```

   This writes `~/.cocoaskills/config.json` with your `skills_root`, preferred
   locale, default agents, and a list of managed projects.

3. In each managed project, declare which skills you want:

   ```json
   {
     "schema_version": 1,
     "agents": ["claude_code", "codex_cli", "cursor"],
     "locale": "en",
     "skills": [
       { "name": "skill-youtrack", "tag": "v1.0.0" },
       { "name": "skill-grafana", "branch": "main" }
     ]
   }
   ```

4. Run `csk install` from anywhere. It installs the declared skills into every
   configured project.

## CLI

| Command | Behavior |
|---|---|
| `csk bootstrap` | Interactively create the global config. |
| `csk install [alias]` | Apply `Skillfile.json` using current local git refs. Does not fetch. |
| `csk update` | Fetch all git repositories under `skills_root`. Does not modify projects. |
| `csk upgrade [alias]` | Run `update`, then `install`. |
| `csk status [alias]` | Show manifest vs installed state. |
| `csk list` | List configured projects and declared skills. |
| `csk project add <alias> <path>` | Register a project and create an empty manifest. |
| `csk config show` | Print resolved config path and contents. |
| `csk shell-init [zsh\|bash\|powershell]` | Print shell hook code for auto-`PATH` activation. |
| `csk --version` | Print version and exit. |

Flags shared by `install` and `upgrade`:

- `--dry-run` — plan work without modifying files.
- `--verbose` — print detailed progress.
- `--fix-gitignore` — append the managed CocoaSkill block to `.gitignore`.
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

- [MVP design specification](docs/mvp-design.md) — frozen contract for v0.1
  covering manifests, refs, install pipeline, locking, adapters, security
  boundary, and test surface.
- [CHANGELOG](CHANGELOG.md) — release history in Keep a Changelog format.

## License

Apache-2.0. See [LICENSE](LICENSE).
