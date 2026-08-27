# CocoaSkills (csk) — Architecture & Implementation Review

Scope: all of `src/csk/*.py` (~3.3k lines), cross-checked against `docs/mvp-design.md`,
`docs/v0.3-design.md`, `docs/v0.6-design.md`. Every finding below was verified against
the actual code (and, where noted, reproduced). No speculation.

---

## Verdict

**Does it need critical rework: NO.** The layering is sound and the module boundaries
are mostly clean. But there are a few **ship-blocking security/correctness bugs** that
must be fixed before this is safe to run against arbitrary git sources, plus one GC
safety hole that silently breaks previously-working projects. These are localized
fixes, not a rewrite.

---

## Critical

### C1. Path traversal via unvalidated skill `name`, `source`, and command key names
**Files:** `manifest.py:117-140`, `skillspec.py:73-75`, `installer.py:270` / `:275` / `:306`,
`shims.py:36` / `:114-131`, `adapters.py:83-89`

Skill `name`, `source`, and command **key** names are validated only as "non-empty
string". They are never checked for path separators or `..`. They are then used directly
as path components:

- `installer._install_skill_context_to_root`: `target = target_root / plan.decl.name`
  (line 270) and `_replace_dir(tmp, target)` (line 306) — a name like `../../evil`
  renames a directory **outside** `.agents/skills`.
- `shims.install_runtime_command` (line 36): `runtime/<skill_name>/<commit>/bin/<command.name>`
  — command name `../../../../tmp/x` escapes the runtime store.
- `shims.write_bin_shim` (line 114-131): `bin_dir / command_name` — a malicious command
  name writes a `.cmd` (Windows) or symlink (unix) **anywhere** the user can write
  (e.g. overwrite a shell rc, a git hook, `~/.zprofile`).
- `config.skills_root / decl.source` and `snapshot.snapshot_dir(... source ...)` —
  `source` traversal escapes the cache/skills_root.

The command key names come from the **untrusted skill repo's `csk-skill.json`** (skills
are cloned from arbitrary git URLs per the design). Note the *file paths*
(`unix_path`/`win_path`/`runtime_roots`) ARE validated via `_validate_relative_path`,
but the command **key** and skill **name/source** are not. This is an arbitrary-file-write
primitive from a third-party skill.

**Fix:** validate `name`, `source`, and every command key as a single safe path segment
(reject `/`, `\`, `..`, leading `.`, empty). Centralize a `_safe_segment()` helper used by
both `manifest.parse_manifest` and `skillspec._load_csk_skill`.

### C2. Runtime GC deletes runtime still referenced by unregistered (path-installed) projects
**Files:** `gc.py:10-24`, `installer.py:58`, `cli.py:558-576`

`gc.collect_runtime` only scans markers under `global/skills` and the projects present in
`config.projects`. Per the v0.3 design, `csk install .` / `csk install /path` **no longer
persist** the project to `config.json` (`cli._cfg_and_alias_for_target` builds an in-memory
`updated` cfg but never calls `save_config`). So a project installed by path is invisible to
GC on any *later* run. Project `.agents/bin` shims are symlinks into the shared
`~/.cocoaskills/runtime/<skill>/<commit>/bin`. The next `csk install` (of any registered
project) runs `gc.collect_runtime`, sees that commit unreferenced, and `shutil.rmtree`s it —
**dangling the unregistered project's command shims with no warning.**

**Fix:** make GC conservative — either persist path-installs, or have GC also walk known
project roots discovered some other way, or (minimum) skip deletion of any runtime dir
whose commit is referenced by a marker reachable from `cwd` walk-up. At least document and
guard so GC never reaps a commit it cannot prove is unreferenced globally.

### C3. `git clone` argument/transport injection from Skillfile-controlled URL
**File:** `git_ops.py:31-44`

`clone_repo` runs `["git", "clone", remote_url, str(destination)]` with no `--` separator
and no transport allow-list. A `git` URL of `--upload-pack=...` (argument injection, no `--`)
or `ext::sh -c '...'` (git's `ext::` transport = arbitrary command execution) yields RCE.
The URL comes from `Skillfile.json` `git` (and `csk global add --git`). A developer who
clones an untrusted repo and runs `csk install` is owned.

**Fix:** insert `--` before positional args (`["git","clone","--",remote_url,dest]`) and
reject dangerous transports (`ext::`, and arguably `file::`/leading `-`) before cloning.

---

## Should-fix

### S1. Generated `.agents/env.sh` computes the wrong project root under zsh
**File:** `env_files.py:11-17` (consumed by `shell_init._posix_hook`, which advertises zsh)

`env.sh` uses `${BASH_SOURCE[0]}` to locate itself. In zsh `BASH_SOURCE` is empty, so
`dirname ""` = `.` and `CSK_PROJECT_ROOT` resolves to the **parent of the current working
directory**, and `PATH` is pointed at a wrong/nonexistent `.agents/bin`. Reproduced:

```
zsh:  CSK_PROJECT_ROOT=/tmp/cskrev/proj/sub   (wrong)
bash: CSK_PROJECT_ROOT=/tmp/cskrev/proj       (correct)
```

`csk shell-init zsh` is an explicitly supported, advertised path, so zsh users get a broken
PATH. **Fix:** make `env.sh` shell-agnostic, e.g. derive the dir from
`${BASH_SOURCE[0]:-${(%):-%x}}` or have the hook export the resolved dir instead of relying
on `BASH_SOURCE`.

### S2. Duplicated `_install_runtime_commands` between installer and global_install (will drift)
**Files:** `installer.py:232-262` vs `global_install.py:231-261`

These two functions are near-verbatim copies (only `write_project_shim` vs `write_global_shim`
differs). They already encode the same runtime-roots/shim logic twice; any fix to one (e.g. the
C1 validation, or a shim-writing change) silently misses the other. **Fix:** parametrize a
single shared function on the shim-writer callable.

### S3. Stale lock has no liveness detection — every command blocks then fails after a crash
**File:** `locking.py:19-39`

`GlobalLock` is a pure `O_CREAT|O_EXCL` file lock. If a csk process crashes/segfaults, the
`.lock` file is never unlinked (no `try/finally` around the *whole* critical section beyond
the context manager, and no PID-liveness check). Every subsequent `install`/`update`/`global`
command then blocks `CSK_LOCK_TIMEOUT` (default 30s) and fails with `EXIT_LOCK` until the user
manually deletes the file. The error message tells them to remove it manually, so it's a
deliberate trade-off, but a dead-PID check (`os.kill(pid, 0)`) would auto-recover safely.
**Fix:** on `FileExistsError`, read the stored pid and reclaim the lock if that pid is gone.

### S4. Orphaned `.tmp-<pid>` / `.backup-<pid>` dirs accumulate and are never cleaned
**Files:** `installer.py:275-276`, `installer.py:345-357`, `shims.py:58`

`_install_skill_context_to_root` and `_replace_dir` create `.{name}.tmp-{pid}` and
`.{name}.backup-{pid}` siblings. If the process dies mid-`_replace_dir`, these are left
behind. They're only removed when a *same-pid* run repeats (won't happen). Worse,
`_cleanup_removed_skills_root` (installer.py:370) explicitly **skips dotfiles**, so these
orphans are never GC'd and silently pile up under `.agents/skills`. **Fix:** sweep
`.*.tmp-*` / `.*.backup-*` siblings at the start of an install, or use `tempfile.mkdtemp`
in the same parent and clean unconditionally.

---

## Nice-to-have (brief)

- `git_ops._extract_archive` (`:112-128`): on Python 3.12+ the `filter="data"` branch returns
  early and silently *allows* safe symlinks, contradicting the "links unsupported in MVP"
  rejection used on the 3.11 fallback path. Inconsistent behavior across interpreters.
- `hashing.content_sha256` (`:17-25`) builds one `bytearray` of all file contents in memory
  before hashing — O(total size) memory. Fine for small skills, wasteful for large
  `runtime_roots`. Stream into the hash incrementally.
- Content hash ignores the executable bit; a script that loses `+x` won't register as
  `content-drift`.
- `cli.py` (695 lines) mixes argparse construction, dispatch, target resolution, project
  registration, and rendering. Extract target-resolution + rendering to keep it a thin entry
  point. Not urgent — boundaries are still followable.
- Project-scope install (`installer._install_project`) aborts mid-loop on exception, leaving
  adapters unrefreshed and stale shims un-pruned for that run (global install handles partial
  failure deliberately; project install does not mirror that care).
- `gitignore_gate.missing_entries` calls `git check-ignore` which fails in a non-git dir,
  causing install to be blocked as "not ignored" until `--fix-gitignore`. Edge case, covered
  somewhat by the `csk init` warning.

---

## What is well done (balance)

- **Snapshot/runtime store design is clean and content-addressed:** archive → cache by
  `(source, commit)` → runtime by `(skill, commit)`, with atomic `rename`-into-place in
  `snapshot.get_snapshot`, `shims.install_runtime_roots`, and `installer._replace_dir`. The
  happy-path "build in tmp, atomic swap, keep backup, restore on failure" is solid.
- **Idempotency via marker + content hash** (`_marker_is_current`): re-install short-circuits
  to `up-to-date` only when ref/commit/locale/agents AND a recomputed content sha256 match.
  `status`/`global status` reuse the same hash to report `content-drift`. Genuinely robust.
- **File-path validation for commands/runtime_roots is thorough** (`skillspec._validate_relative_path`,
  `_validate_v2_script_path`, `_parse_runtime_roots`): rejects absolute, `..`, non-POSIX,
  overlapping/duplicate roots, and confirms script paths live inside a declared root. Plus the
  `src.relative_to(snapshot.resolve())` guard in `shims`. (The gap is C1 — the *name* keys, not
  the paths.)
- **Tar extraction is defended** (`git_ops._extract_archive`): uses `filter="data"` where
  available and a manual `relative_to` + symlink/hardlink rejection fallback.
- **Atomic, exclusive locking primitive** (`O_CREAT|O_EXCL`) with a helpful contention message,
  and installs/cache/runtime writes are correctly serialized under it (GC for both project and
  global paths runs inside the lock).
- **Global partial-failure handling is deliberate and correct** (`global_install.install`):
  keeps previously-working shims/adapters when a skill fails this attempt, refreshes adapters
  from on-disk state rather than the failed plan set. Thoughtful.
- **Adapter conflict detection** (`adapters._is_unmanaged_conflict` + `.csk-managed.json`):
  refuses to clobber a pre-existing non-csk adapter dir, and tracks managed entries for clean
  removal. Symlink/copy/auto fallback is correct and cross-platform-aware.
- **Schema versioning & forward-compat messaging** across config/manifest/skillspec with an
  explicit upgrade hint; unknown-field rejection on csk-skill v2.
- **Shell env quoting** (`env_files._sh_quote`/`_ps_quote`) is correct for the global env files
  (the bug in S1 is specifically the `BASH_SOURCE` self-location, not the quoting).
