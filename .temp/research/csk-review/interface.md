# CocoaSkills (csk) Public Interface Review

Date: 2026-06-09
Scope: CLI surface, on-disk schemas (Skillfile.json, csk-skill.json v1/v2, .csk-install.json, config.json), exit codes, output formats, error UX, footguns, missing essentials.
Method: full read of `src/csk/cli.py`, `config.py`, `manifest.py`, `skillspec.py`, `status.py`, plus `installer.py`, `global_install.py`, `gc.py`, `shims.py`, `adapters.py`, `git_ops.py`, `locking.py`, `gitignore_gate.py`, `project_resolver.py`, `snapshot.py`, `deprecation.py`; design docs (`docs/mvp-design.md`, `docs/v0.3-design.md` ... `docs/v0.6-design.md`); README CLI table. Behavioral claims verified against code or by running the CLI in a sandbox (missing-git case reproduced live).

Verdict: **critical rework needed: no.** The core surface is coherent and disciplined, but there is one critical correctness footgun (GC vs unregistered checkouts) and a batch of should-fix items that ought to land before the interface ossifies — exit-code semantics, no-op flags, machine-readable output, and a few schema error-message traps.

---

## 1. Critical findings

### C1. Runtime GC deletes runtime dirs still referenced by unregistered checkouts (silent command breakage)

**Severity: Critical.**

Evidence chain:

- `csk install` / `csk install .` in an unregistered project resolves the checkout and adds it to the config **in memory only** — v0.3 deliberately removed auto-register (`docs/v0.3-design.md` "Auto-Register Removed"; `cli.py:568-576` calls `config.add_project` but `save_config` is never called on this path; the only `save_config` callers are `bootstrap` at `cli.py:395` and `project add` at `cli.py:449`).
- Project command shims are **symlinks into the shared runtime store**: `shims.write_project_shim` → symlink to `~/.cocoaskills/runtime/<skill>/<commit>/...` (`shims.py:106-131`, `installer.py:232-262`).
- GC runs unconditionally after every non-dry-run install — project and global (`installer.py:57-58`, `cli.py:507-508`) — and computes the referenced set **only** from `config.projects` paths plus the global scope (`gc.py:10-24`).

Consequence: install skill X in a worktree or any project never passed through `csk project add` (a first-class use case — the whole `checkout_alias`/worktree machinery exists for this), then later run `csk install --all`, `csk install <other-project>`, or `csk global install` — GC deletes `~/.cocoaskills/runtime/<skill>/<commit>` because no registered marker references it. The unregistered checkout's `.agents/bin/*` shims now dangle; its commands break silently with no csk-visible error. The breakage is masked whenever both checkouts happen to pin the same commit, which makes it intermittent and hard to diagnose.

Suggestion (pick one):

1. Track installed checkouts in a separate registry (e.g. `~/.cocoaskills/installs.json`, append-on-install, prune-when-path-gone) used only by GC — keeps the v0.3 "projects map is user-managed" decision intact.
2. Or make GC age-based for unreferenced commit dirs (delete only if older than N days) instead of immediate.
3. Or stop symlinking shims into runtime and copy command payloads project-locally; runtime store becomes a pure cache.

The test `tests/test_gc.py::test_runtime_gc_keeps_referenced_runtime_across_projects` covers only registered projects, so this gap is untested by design.

---

## 2. Should-fix findings

### S2. Missing `git` binary produces a raw Python traceback

**Severity: Should-fix.** Verified live: with git absent from PATH, `csk init <dir>` crashes with a `FileNotFoundError` traceback from `cli.py:626` (`_is_inside_git_worktree` → `subprocess.run(["git", ...])`), exit code 1. The catch list in `main()` (`cli.py:33-40`) covers neither `OSError`/`FileNotFoundError` nor `git_ops.GitError`. For a tool whose every operation depends on git, this is the first thing a user on a fresh machine hits. Suggestion: preflight `shutil.which("git")` once in `main()` (or catch `FileNotFoundError` around git invocations) and print `error: git executable not found on PATH; install git and retry` → exit 2.

### S3. `csk install` exits 0 when the project was refused/skipped

**Severity: Should-fix.** When the gitignore gate refuses installation, `_install_project` sets `status="skipped"`, appends a *message* (stdout), and returns no errors (`installer.py:82-87`); `_cmd_install` derives the exit code only from `result.errors` (`cli.py:477-484`). So `csk install` against a project whose `.gitignore` lacks the managed entries prints "Generated CocoaSkill paths are not ignored by git ... skipped" and **exits 0**. Same for "Skillfile.json not found; skipped" when explicitly targeting one project. CI cannot distinguish "installed" from "refused". Exit-code semantics are exactly the part of the interface that ossifies first. Suggestion: a directly-targeted project that is skipped should exit 1 (keep exit-0 skips for `--all` sweeps if desired, or introduce a distinct code). Also the `GitignoreError` message names the missing entries but gives no next step — append `hint: run 'csk init' to add the managed .gitignore block`.

### S4. `csk status` always exits 0, even on `missing` / `content-drift` / `error`

**Severity: Should-fix.** `cli.py:291-294` returns `EXIT_OK` unconditionally; labels live only in the text (`status.py`). There is no way to use status as a sync check in CI/hooks without parsing the human-format table. Suggestion: `csk status --check` (or default nonzero when any skill is not `up-to-date`), mirrored on `csk global status`.

### S5. No machine-readable output while the text format ossifies

**Severity: Should-fix.** `--json` was an explicit MVP non-goal (`docs/mvp-design.md:465`), but the project is now at v0.6 with `status`/`list`/`project resolve` emitting column-aligned, 7-char-truncated-commit text (`status.py:54`, `cli.py:648-665`) that scripts will start parsing. Adding `--json` to `status`, `list`, `project resolve`, and `global status` *before* third parties scrape the text is cheap now and expensive later. The internal data model (`SkillStatus`, `ResolvedProject`, markers) already has everything needed.

### S6. `--verbose` is a documented no-op

**Severity: Should-fix.** `InstallOptions.verbose` is defined (`installer.py:26`), plumbed from four CLI flags (`cli.py:223,259,264`), and **never read anywhere** in `src/csk/` (verified by grep). README documents it as "print detailed progress". Shipping a dead flag teaches users to distrust the surface. Either implement minimal verbose output or drop the flag before 1.0.

### S7. `--strict-tags` on `csk global install`/`global upgrade` is accepted but does nothing

**Severity: Should-fix.** Help text is honest ("accepted for symmetry; currently unused", `cli.py:260,265`), but `global_install.install` performs no moved-tag check at all (no reference to `strict_tags` or `_moved_tag_warnings` in `global_install.py`), and the README lists `--strict-tags` as a shared flag with no caveat. This is a security-relevant flag (moved-tag detection) that silently doesn't protect the global scope. Implement the check (the marker data needed is identical) or reject the flag.

### S8. Scope asymmetry: `csk global add/remove` exist, project-scope `csk add/remove` do not

**Severity: Should-fix.** The only way to declare a project skill is hand-editing `Skillfile.json`; the global scope gets validated `add`/`remove` with replace semantics and pre-write `parse_manifest` validation (`global_install.py:57-106`). The pleasant UX (`csk global add skill-x --git ... --tag v1`) should exist for the primary scope too: `csk add <name> --git ... --tag v1 [target]` / `csk remove <name>`. Relatedly, `csk project add` has no inverse — unregistering a project requires hand-editing `~/.cocoaskills/config.json` (`cli.py:112-135` defines only `add` and `resolve`). Add `csk project remove <alias>`.

### S9. Snapshot cache is never garbage-collected; no `csk gc` command

**Severity: Should-fix.** GC covers only `~/.cocoaskills/runtime` (`gc.py:16-24`). `~/.cocoaskills/cache/<source>/<commit>/snapshot` (`snapshot.py:13-31`) accumulates one full tree per installed commit forever. There is also no user-facing command to trigger or inspect cleanup. Suggestion: extend `collect_runtime` to prune cache snapshots not referenced by any marker, and expose `csk gc [--dry-run]` so behavior is observable and documented.

### S10. `status` "error" label swallows the cause

**Severity: Should-fix.** Every failure path in `_skill_status` is `except Exception: return ... "error"` (`status.py:64-65, 71-73, 79-81`; same pattern in `global_install.py:309-333`). A user whose skill repo is missing from `skills_root`, or whose tag doesn't exist, sees a bare `error` with no path, no reason, no next step — while the underlying `GitError`/`InstallError` messages are actually good ("Skill repository not found for X: <path>", `installer.py:166`). Carry the message into the status line (e.g. `error: skill repository not found: <path>`).

### S11. Wrong-direction `schema_version` error messages in config and Skillfile parsing

**Severity: Should-fix.** `config.py:64-67` and `manifest.py:93-96` reject any value `!= 1` with "...requires a newer csk". This message is wrong in the two most common cases: the field is **missing** (hand-written file → `schema_version None`) or the wrong **type** (`"1"` string). A user who typos the field is told to upgrade csk, which cannot help. `skillspec.py:57-64` already does this right (type check + distinct message + upgrade hint). Align config/manifest: distinguish "missing/invalid field (expected integer 1)" from "version N is newer than supported". Note also the strategy asymmetry: Skillfile v1 ignores unknown fields (forward-compatible additive evolution — good) while csk-skill v2 rejects unknown fields (`skillspec.py:66, 219-223`), meaning any additive csk-skill field forces v3 and a hard break for older csk. That tradeoff is defensible for an executable-command schema, but it should be a documented policy, not an accident.

### S12. `csk bootstrap` accepts empty `skills_root` and writes a cwd-relative config; no non-interactive mode

**Severity: Should-fix.** `cli.py:382` takes `input("skills_root: ").strip()` with no validation; empty input becomes `Path("")` → serialized as `"."` — a relative `skills_root` silently resolved against whatever cwd each later command runs from. Validate non-empty + absolutize. Also `bootstrap` is the only setup path and is interactive-only (`csk init` has `--no-interactive`, bootstrap has nothing), so scripted machine setup (dotfiles, CI) must hand-write `config.json`. Add `csk bootstrap --skills-root PATH [--default-agents ...] [--non-interactive]`.

### S13. Unknown agent names are silently ignored end-to-end

**Severity: Should-fix.** `csk init --agents codex` (typo for `codex_cli`) is accepted (`cli.py:605-609` only splits/strips), `required_gitignore_entries` skips unknown names (`adapters.py:25-31`), and `_refresh_adapters` `continue`s past them (`adapters.py:71-74`). Result: project installs "successfully" but no agent adapter is ever created and no warning is printed. The valid set is a closed enum (`AGENT_PATHS`, 4 entries). Validate agent names at `init`/`project add`/manifest parse and fail with the list of supported values.

### S14. `csk install --dry-run` modifies the filesystem (creates `skills_root`); global dry-run doesn't

**Severity: Should-fix (small but a stated-contract violation).** For project install/update/upgrade, `validate_skills_root_for_work` runs unconditionally and `mkdir -p`s `skills_root` (`cli.py:296-298`, `config.py:188-191`), while the global path explicitly skips it for dry-run (`cli.py:344-346`). Help text promises "plan work without modifying files". Apply the global-style gate to the project path.

---

## 3. Nice-to-have findings

### N1. `csk update` scope asymmetry under the same verb

`csk update` fetches **every** git repo under `skills_root` regardless of any project (`git_ops.fetch_all`), so `csk upgrade <one-project>` fetches the whole machine's skill repos; `csk global update` fetches only declared skills (`global_install.py:194-209`). Same verb, different scoping rule. Consider `csk update [target]` fetching only the target's declared sources, with `--all` for the current behavior.

### N2. `content-drift` is silently overwritten on the next install

Local edits inside `.agents/skills/<name>/` flip the marker hash; `_marker_is_current` returns False and the next `csk install` regenerates the directory, destroying the edits, with the message just saying "installed" (`installer.py:269-307, 329-330`). The dirs are documented as csk-owned, so this is defensible — but printing `warning: <skill> had local modifications; overwritten` would save someone's afternoon.

### N3. Relative directory targets are misparsed as aliases

`csk install subdir` → "Unknown project alias: subdir" because `_looks_like_path` requires `./`, `../`, `~`, or absolute (`cli.py:586-591`). Add a hint ("did you mean ./subdir?") or fall back to path interpretation when a directory of that name exists.

### N4. Stale lock never self-heals

The lock records `pid` (`locking.py:26`) but timeout handling only tells the user to remove the file manually (`locking.py:42-54`). A liveness check (`os.kill(pid, 0)` / process-exists on Windows) could safely reclaim locks from crashed processes.

### N5. Dead/odd flags

`csk init --no-interactive` is documented as "accepted for scripting; prompts are not used" (`cli.py:193`) — a no-op kept for symmetry; fine short-term, but decide before 1.0. `--fix-gitignore` deprecation is handled well (stderr warning once, `deprecation.py`).

### N6. No `fish` in `shell-init`

`cli.py:155` allows `zsh|bash|powershell` only. fish is common among the target audience.

### N7. Lockfile semantics deferred — branch refs are non-reproducible across machines

Deliberate MVP non-goal (`docs/mvp-design.md:8-11`). Today the resolved commit is recorded only in the gitignored `.csk-install.json`, so two machines installing the same Skillfile with a `branch` ref can get different commits with no shared record. When the lockfile lands, a committed `Skillfile.lock.json` mapping decl → commit is the natural shape; worth reserving the filename now.

### N8. Missing introspection commands

No `csk info <skill>` / available-tags listing (user must run git in `skills_root` manually), no `csk doctor`. Low priority, but typical of mature dependency-manager surfaces.

### N9. `csk status` of a project whose path vanished says "Skillfile.json missing"

`status.py:41-43` — technically true, but "project path does not exist: <path>" (which `csk list --paths` already detects via the `(missing)` suffix, `cli.py:516`) would be more accurate.

---

## 4. What is well designed

- **Exit-code contract**: small, meaningful set (0/1/2/3) documented in README *and* per-command epilogs (`cli.py:83, 209-210`); argparse usage errors land on 2, consistent with config errors.
- **stdout/stderr discipline**: results to stdout, every diagnostic (`error:`, fetch failures, deprecation warnings, non-git-worktree warning) to stderr — consistent across `cli.py` (`41, 44, 461, 483, 492, 506`, `deprecation.py:22`).
- **Idempotent installs**: marker comparison covers ref kind/value, commit, locale, agents, and a content hash (`installer.py:310-330`) — `csk install` twice is a fast no-op; drift triggers exact regeneration.
- **Crash-safe mutations**: temp-dir + rename + backup for skill context (`installer.py:343-357`), tmp+rename for snapshots (`snapshot.py:21-31`) and runtime roots (`shims.py:57-81`); partial global failure deliberately preserves previously working shims/adapters with explanatory comments (`global_install.py:174-184`).
- **Concurrency**: single global lock around all mutating commands (`cli.py:298, 347`), O_CREAT|O_EXCL portable implementation, lock-holder pid/timestamp in the timeout message, `CSK_LOCK_TIMEOUT` override (`locking.py`).
- **Security posture**: path-traversal rejection in csk-skill paths (`skillspec.py:160-175`), snapshot-escape checks in shims (`shims.py:29-32`), safe tar extraction with `filter="data"` plus a manual fallback for 3.11 (`git_ops.py:112-128`), gitignore gate refusing to generate non-ignored trees.
- **csk-skill.json v2 validation quality**: typed schema_version check with a distinct unsupported-version message *and* concrete upgrade commands (`skillspec.py:11-14, 60-64`), field-precise errors (`commands.NAME.unix_path ...`), unknown-field rejection naming the offending keys, runtime_roots existence/disjointness/uniqueness checks.
- **Error messages with next steps** in key flows: walk-up failure prints three concrete hints including `--all` with project count (`cli.py:594-602`); global commands print "Run 'csk global init' first."; `csk init` outside git explains exactly what was written and what to do.
- **Versioned everything**: config, Skillfile, csk-skill, install marker, and `.csk-managed.json` all carry `schema_version` from day one — the hooks for evolution exist even where the messaging needs work (S11).
- **Deterministic output**: sorted iteration for fetches (`git_ops.py:89`), sorted JSON serialization (`sort_keys=True` everywhere), manifest-order status lines.
- **Testability hooks**: `CSK_CONFIG` env override, `deprecation.reset_for_tests`, extensive test suite (20 test modules) covering locking, gc, gitignore gate, e2e.

## 5. Summary table

| # | Severity | Finding |
|---|---|---|
| C1 | Critical | GC deletes runtime dirs referenced by unregistered checkouts → silent shim breakage (gc.py:10-24, cli.py:568) |
| S2 | Should-fix | Missing git → raw traceback (cli.py:33-40, 626) |
| S3 | Should-fix | Skipped/refused install exits 0 (installer.py:82-87) |
| S4 | Should-fix | `csk status` always exits 0; no `--check` |
| S5 | Should-fix | No `--json` for status/list/resolve while text format ossifies |
| S6 | Should-fix | `--verbose` is a no-op everywhere (installer.py:26) |
| S7 | Should-fix | Global `--strict-tags` accepted but unimplemented; README unqualified |
| S8 | Should-fix | No project-scope `csk add/remove`; no `csk project remove` |
| S9 | Should-fix | Cache never GC'd; no `csk gc` command (snapshot.py, gc.py) |
| S10 | Should-fix | status "error" label hides the cause (status.py:64-81) |
| S11 | Should-fix | Misleading schema_version errors in config/manifest; mixed unknown-field policy |
| S12 | Should-fix | bootstrap: empty skills_root → relative "."; no non-interactive mode (cli.py:382) |
| S13 | Should-fix | Unknown agent names silently ignored (adapters.py:25-31, 71-74) |
| S14 | Should-fix | Project `install --dry-run` mkdirs skills_root; global doesn't (cli.py:296-298 vs 344-346) |
| N1-N9 | Nice-to-have | update-scope asymmetry, drift overwrite warning, relative-target hint, stale-lock liveness, dead flags, fish, lockfile reservation, info/doctor, status wording |
