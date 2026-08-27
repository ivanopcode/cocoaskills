# CocoaSkills Documentation Review

Date: 2026-06-09
Scope: README.md, CHANGELOG.md, docs/skill-authoring.md, docs/mvp-design.md,
docs/v0.3..v0.6-design.md, docs/index.html, docs/install.sh — vs implementation
ground truth in src/csk/ (cli.py, skillspec.py, config.py, manifest.py,
global_install.py, installer.py, whitelist.py, shims.py, shell_init.py,
locale.py, adapters.py) at commit 9eb93f9 (post-v0.6.0).

Verdict: docs are largely accurate for the happy path, but the CHANGELOG misses
the shipped v0.6.0 release entirely, and the README + authoring guide combine
into a real install-failure trap around `.skill_triggers/` + `locale`. The CLI
table has concrete omissions (`csk global list`, `csk init` flags,
`shell-init --no-global`), and mvp-design.md is a half-updated "frozen" doc
that contradicts current bootstrap/auto-registration behavior without a
superseded banner.

---

## Critical (doc is wrong / docs combine into broken behavior)

### C1. CHANGELOG does not contain the released v0.6.0

- Evidence: git tag `v0.6.0` points at 14bf2bb "Implement global skills
  support" (and v0.6.0 is on PyPI per the release flow), but all global-skills
  content sits under `## [Unreleased]` in CHANGELOG.md:8-24. The compare link
  (CHANGELOG.md:160) is `Unreleased: v0.5.0...HEAD`; there is no `[0.6.0]`
  section or link. README.md:256 advertises the CHANGELOG as "release history
  in Keep a Changelog format".
- Impact: a user on 0.6.0 finds no release notes for the version they
  installed; "Unreleased" is factually wrong.
- Fix: cut a `## [0.6.0] - 2026-05-27` section from the current Unreleased
  block (global skills, global shims/shell-init, global adapters, runtime GC
  change), add the compare links, keep `[Unreleased]` empty or with post-0.6.0
  changes (58461da Windows GC test fix, 9eb93f9 README title).

### C2. `.skill_triggers/` + `locale` contract is undocumented and the two docs together produce a failing install

- Evidence: locale.py:11-22 — when a project (or global Skillfile) sets
  `locale`, and the skill snapshot contains `.skill_triggers/` (or
  `locales/metadata.json`), csk REQUIRES both `locales/metadata.json` AND
  `.skill_triggers/<locale>.md`, otherwise it raises `LocaleError` and the
  skill install fails. Meanwhile:
  - README.md:114 quick-start example Skillfile sets `"locale": "en"`.
  - docs/skill-authoring.md:11-23 (layout) and :243-247 (prompt context roots)
    recommend shipping `.skill_triggers/` — with zero mention of
    `locales/metadata.json`, the per-locale trigger file, or the failure mode.
  - The whole locale rendering feature (SKILL.md frontmatter description /
    triggers rewriting, `agents/openai.yaml` rewriting) is documented only in
    the frozen mvp-design.md ("Locale Policy") and a CHANGELOG one-liner.
- Impact: a skill author who follows the authoring guide and adds
  `.skill_triggers/` without `locales/metadata.json` breaks installs for every
  project that followed the README quick start (`"locale": "en"`).
- Fix: add a "Locales" section to skill-authoring.md documenting
  `locales/metadata.json` schema (locales.<code>.description, optional
  openai.yaml fields), `.skill_triggers/<locale>.md` format (bullet list,
  code fences ignored), and the hard-fail rule; in README, explain what
  `locale` does and when it can fail.

---

## Should-fix (blocking or materially misleading gaps)

### S1. README CLI table omits `csk global list`

- Evidence: cli.py:255 defines `global list` ("list declared global skills",
  global_install.list_declared). README.md:189-212 CLI table documents every
  other `csk global` subcommand (init/add/remove/install/update/upgrade/status)
  but not `list`. The CHANGELOG Unreleased section even names it
  ("init/add/remove/list/status/install/update/upgrade").
- Fix: add a `csk global list` row.

### S2. README CLI table omits `csk init` flags and `shell-init --no-global`

- Evidence: cli.py:190-193 — `csk init` accepts `[path]`, `--alias`,
  `--agents`, `--no-interactive`; README.md:192 documents only
  `csk init [path]`. cli.py:156 — `shell-init --no-global` (skip global bin
  activation); README.md:211 omits it. README's flag section (214-219) covers
  only install/upgrade flags, so the reader reasonably assumes flag coverage
  is complete.
- Fix: extend the `csk init` row (`csk init [path] [--alias A] [--agents a,b]`)
  and mention `--no-global` on the shell-init row. Optionally note that
  `csk global install/upgrade` accept `--dry-run/--verbose` and that
  `--strict-tags` there is accepted but currently unused (cli.py:260,265).

### S3. mvp-design.md is presented as the frozen contract but is a half-updated mix of v0.1 and later behavior

- Evidence:
  - README.md:13: "The MVP design contract is frozen in docs/mvp-design.md"
    (top of README, before the Documentation section qualifies it as "frozen
    contract for v0.1").
  - mvp-design.md:328: `bootstrap` "Interactively creates global config,
    preferred locale, default agents, focused projects, and optionally shell
    hook instructions" and :429-440 includes "projects to focus on, as an
    alias/path loop" — contradicts current `csk bootstrap` (cli.py:375-398:
    machine config only; CHANGELOG 0.3.0: "no longer prompts for project
    registration").
  - mvp-design.md:331/:377-378: path installs "update global config for that
    checkout" — contradicts v0.3 behavior (CHANGELOG 0.3.0: "path-based
    installs no longer auto-register checkouts"; cli.py
    `_cfg_and_alias_for_target` updates config only in memory, never calls
    `save_config`).
  - Meanwhile mvp-design.md:17-18 was retro-edited to include the v0.4 clone
    feature ("Clone a missing skill repository when a Skillfile declaration
    provides a source git URL"), so the doc looks current while other sections
    are stale. "Status: accepted for MVP implementation" carries no
    superseded notes; RFCs 0001-0004 are properly marked "accepted" but
    mvp-design never points at them.
- Fix: add a status banner to mvp-design.md: "Historical v0.1 contract;
  superseded in parts by RFC 0001 (current-project flow, bootstrap scope),
  RFC 0002 (git clone), RFC 0003 (runtime_roots), RFC 0004 (global skills)",
  and either revert the retro-edit or annotate it. Soften README line 13 to
  "the original MVP contract (v0.1) is documented in...".

### S4. Undocumented prompt-context fallback: command-less skills get `scripts/` copied INTO agent context

- Evidence: installer.py:278 — `include_scripts = not plan.spec.commands and
  (plan.snapshot / "scripts").exists()`; whitelist.copy_context then includes
  the whole `scripts/` tree in `<project>/.agents/skills/<skill>/`.
  skill-authoring.md:25-26 claims "CocoaSkills copies only skill-facing
  content" and :249 says "Runtime-only code should be placed under
  runtime_roots, usually scripts/" — neither doc mentions that a skill without
  a `csk-skill.json`/`runtime.json` command leaks `scripts/` into prompt
  context.
- Impact: skill authors relying on "scripts stay out of context" get the
  opposite behavior the moment they ship no commands manifest.
- Fix: document the fallback in skill-authoring.md section 7 (and/or README
  "Skill command manifests"): "if the skill declares no commands and has a
  `scripts/` directory, it is copied into prompt context for compatibility".

### S5. No end-user documentation for removing a skill / no `csk uninstall`

- Evidence: there is no uninstall command (cli.py); the removal flow (delete
  from Skillfile.json, run `csk install`; cleanup removes context, shims,
  adapters, unreferenced runtime artifacts) is documented only in
  mvp-design.md:381-384. README never mentions it. (Global removal IS
  documented via `csk global remove`.)
- Fix: one sentence in README Quick start or CLI section: "To remove a skill,
  delete its entry from Skillfile.json and re-run `csk install`."

### S6. No complete Skillfile.json field reference in current docs

- Evidence: manifest.py:89-148 enforces rules the README example does not
  convey: exactly one of `tag`/`branch`/`revision` per skill (ManifestError
  otherwise), `source` defaults to `name`, duplicate names rejected, `agents`
  list of strings, optional `project.alias`, optional `locale`. The only full
  reference is in frozen mvp-design.md ("Project Manifest" section). Valid
  agent identifiers (`codex_cli`, `claude_code`, `cursor`, `gemini`,
  adapters.py:9-14) are never enumerated in README, and unknown agent names
  are silently ignored (adapters.py:28-30 `AGENT_PATHS.get`), so a typo like
  `"claude-code"` silently produces no adapter.
- Fix: add a short "Skillfile.json reference" block to README (or a docs page):
  fields, the exactly-one-ref rule, the valid agent identifiers, and the
  silently-ignored-unknown-agent behavior (or make it warn).

---

## Nice-to-have

### N1. index.html stale: no global skills / RFC 0004, brand "CocoaSkill"

- docs/index.html:42 titles the page "CocoaSkill" (README was just renamed to
  CocoaSkills, 9eb93f9); docs links (:53-55) list authoring guide, MVP design,
  RFC 0003 — RFC 0004 (global skills) is missing. Install commands themselves
  are current and consistent with README.

### N2. `CSK_VERSION` pin in install.sh undocumented

- docs/install.sh:5 supports `CSK_VERSION=x.y.z` to pin
  `cocoaskills==VERSION`; README's install section never mentions it.
  Otherwise install.sh matches README claims exactly (pipx → uv tool →
  pip --user fallback, Python >= 3.11 check).

### N3. Environment variables undocumented

- `CSK_CONFIG` (config.py:42, also honored by the shell hooks) and
  `CSK_LOCK_TIMEOUT` (locking.py:58, mentioned only in CHANGELOG 0.1.2) appear
  in no user-facing doc.

### N4. v1 `csk-skill.json` silently ignores `runtime_roots`

- skillspec.py:65-68 — unknown-field rejection and `runtime_roots` parsing are
  v2-only; a v1 manifest containing `runtime_roots` is accepted and the field
  is silently dropped. Authoring guide does not warn that forgetting to bump
  `schema_version` to 2 silently disables the feature.

### N5. `dependencies.json` is a prompt-context root but undocumented

- whitelist.py:21 includes `dependencies.json` in INCLUDE_ROOTS; the authoring
  guide's prompt-context list (section 7) omits it and only mentions the file
  once as a legacy cleanup item (section 11).

### N6. README brand inconsistency

- README body still says "CocoaSkill makes per-project..." (:21), "the
  CocoaSkill generated paths" (:103); CLI self-description is "CocoaSkill
  local skill manager" (cli.py:51). Cosmetic but confusing next to the
  CocoaSkills title.

### N7. System-dependency failure scope slightly overstated in authoring guide

- skill-authoring.md:230-231 says a missing system dependency fails install
  "for that skill" before any writes. For project installs,
  installer._check_system_commands(plans) (installer.py:93) raises before any
  writes and aborts the whole project's install run (all skills), not just the
  one skill. Global install does isolate per skill
  (global_install._plans_with_available_system_commands). Worth one clarifying
  sentence.

---

## Verified accurate (well documented)

- README CLI table semantics for bootstrap/init/install/update/upgrade/status/
  list/project add/project resolve/config show/--version match cli.py exactly,
  including "no target means current project", "--all over registered
  projects", "install clones missing `git` sources but never fetches existing
  repos", and exit codes 0/1/2/3 (cli.py:13-16, installer flow).
- `--dry-run/--verbose/--fix-gitignore (deprecated)/--strict-tags` exist on
  both install and upgrade as documented (cli.py:220-225); `--fix-gitignore`
  deprecation warning is real (deprecation.warn_once, cli.py:468-469).
- README global skills section matches global_install.py: paths
  `~/.cocoaskills/global/{Skillfile.json,skills,bin}`, user-level adapter
  dirs, project shims shadowing global ones (shell_init.py sources global env
  first, then project env prepends PATH).
- `csk global add` flags (`--git`, `--source`, mutually exclusive required
  `--tag/--branch/--revision`) match cli.py:245-252; `global remove` deferred
  cleanup claim matches install-time `_cleanup_removed_skills_root` +
  `remove_stale_global_shims`.
- README csk-skill.json v2 example is valid per skillspec.py; "system commands
  are only checked with shutil.which; csk does not install system tools" is
  accurate (installer._check_system_commands).
- skill-authoring.md runtime_roots rules (relative POSIX, no `..`, no empty
  component, must exist, must be dir, unique after normalization, disjoint,
  case-sensitive) match skillspec._parse_runtime_roots one-for-one; v2 script
  command rules (allowed fields, at least one platform path, must be a file,
  must be inside a runtime root when roots are non-empty) match
  skillspec.py:79-108.
- v1 single-file copy destination `~/.cocoaskills/runtime/<skill>/<commit>/bin/
  <command>` matches shims.install_runtime_command; v2 runtime root copy to
  `~/.cocoaskills/runtime/<skill>/<commit>/` matches shims.install_runtime_roots;
  global vs project install scopes in authoring guide section 9 match
  global_install.global_skills_root and installer paths.
- Forbidden system-command fields list (`install`, `check`, `post_install`,
  `script`, `command_args`) is enforced via the closed allow-list
  (skillspec._reject_unknown_fields), matching guide and CHANGELOG 0.5.0.
- Prompt-context roots and exclusion list in authoring guide section 7 match
  whitelist.INCLUDE_ROOTS/ALWAYS_EXCLUDED (modulo dependencies.json, N5).
- `agents/runtime.json` fallback and `csk-skill.json` precedence match
  skillspec.load_skill_spec.
- Install instructions are mutually consistent across README, install.sh,
  index.html, and the distribution-smoke workflow (pipx / uv tool /
  `brew tap ivanopcode/csk && brew install cocoaskills` / mise
  `pipx:cocoaskills@latest` / pip --user); requires-python >=3.11 matches
  README "Python 3.11+".
- RFCs 0001-0004 carry Status/Date/Target headers and read as accepted design
  records; CHANGELOG 0.1.0-0.5.0 entries match the implementation history.
- skill-authoring migration notes (section 11) cover the agents/runtime.json →
  csk-skill.json v2 path; a v1→v2 "migration guide" is effectively covered by
  sections 3-5 (the delta is schema_version bump + runtime_roots + stricter
  validation), so no separate guide is blocking.
