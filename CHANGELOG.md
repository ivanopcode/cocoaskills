# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

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

[Unreleased]: https://github.com/ivanopcode/cocoaskills/compare/v0.1.0...HEAD
[0.1.0]: https://github.com/ivanopcode/cocoaskills/releases/tag/v0.1.0
