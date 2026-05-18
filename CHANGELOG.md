# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

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

[Unreleased]: https://github.com/ivanopcode/cocoaskills/compare/v0.3.0...HEAD
[0.3.0]: https://github.com/ivanopcode/cocoaskills/compare/v0.2.1...v0.3.0
[0.2.1]: https://github.com/ivanopcode/cocoaskills/compare/v0.2.0...v0.2.1
[0.2.0]: https://github.com/ivanopcode/cocoaskills/compare/v0.1.2...v0.2.0
[0.1.2]: https://github.com/ivanopcode/cocoaskills/compare/v0.1.1...v0.1.2
[0.1.1]: https://github.com/ivanopcode/cocoaskills/compare/v0.1.0...v0.1.1
[0.1.0]: https://github.com/ivanopcode/cocoaskills/releases/tag/v0.1.0
