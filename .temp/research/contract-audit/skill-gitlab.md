# Contract Audit: skill-gitlab

Audited against `/Users/iv/Developer/Wildberries/cocoaskills/.temp/aid/docs/skill-operational-contract.md`.

Skill source: `/Users/iv/agents/skills/skill-gitlab/`

## Top-line summary

**Verdict: STRONG**

- PASS: 24
- PARTIAL: 4
- FAIL: 1
- N/A: 0

This skill is one of the most contract-faithful examples available. It nails execute-don't-instruct, placeholder path resolution, deterministic JSON output, preview-first / refusal-guarded mutations, keyring-only secrets, and system-dependency declaration. The few gaps are: a hard-coded interactive ask for `await-pipeline` interval/timeout (FAIL against non-interactivity steering), a section-order deviation (Setup is near the bottom but not last; `## Safety Rules` sits below it), no first-class agent-vs-human output split (all output is already JSON, so the split is moot but the contract's explicit "separate agent-facing command" is not formally realized), and an `auth bootstrap` path that reads stdin/getpass (allowed by the contract as an explicit exception, noted as PARTIAL on strict non-interactivity).

---

## LEVEL 1 — SKILL.md (agent behavior)

### Default Mode: execute, don't instruct
**PASS** — `## Default Mode` opens with "Execute the bundled commands yourself and return the result" and "Do not answer with a shell tutorial when the skill is already installed and authentication exists. Show commands only when the user explicitly asks for setup steps or when setup/auth is missing." (SKILL.md L25-29). Exactly the prescribed cure.

### Path resolution: `<command>` placeholders, not `scripts/x`, not `$VAR`
**PASS** — Happy paths use `<gmr-command>` throughout, never `scripts/gmr` literals or `$VAR`. (e.g. SKILL.md L54-55, L110-138, L164-200).

### Path resolution: absolute from SKILL.md, not relative from CWD
**PASS** — "Resolve command paths from this skill file path... If the skill file path is `/abs/path/to/SKILL.md`, then `<gmr-command>` is `/abs/path/to/scripts/gmr`" and "Do not run `scripts/gmr` as a path relative to the current working directory." (SKILL.md L34-40).

### Path resolution: explicit unix/windows variants
**PASS** — "`/abs/path/to/scripts/gmr` (Unix) or `/abs/path/to/scripts/gmr.cmd` (Windows)" (SKILL.md L36). Matches `csk-skill.json` `unix_path`/`win_path` and the real `gmr.cmd` file.

### Path resolution: bare command only via repo-local `.agents/bin`
**PASS** — "Bare `gmr` is acceptable only when it resolves through that repo-local `.agents/bin` layer" and "This skill does not provision `PATH` itself." (SKILL.md L37-39).

### Hard happy path: Resolve Context First (single ordered resolution)
**PASS** — `## Resolve Context First` (SKILL.md L50) gives a fixed numbered target-choice order (L65-69) plus an explicit host-selection order (L96-102). No branching ambiguity left to the model.

### Hard happy path: numbered Fast Path for typical request
**PASS** — `## Fast Path: MR Status` (SKILL.md L110) gives a numbered 1-5 happy path; `## Project MR Lists` maps each typical prompt to one exact command.

### When-to-ask boundaries
**PASS** — `## Ask Only If Blocked` (SKILL.md L347-357) enumerates exactly the contract's allowed ask conditions (no auth, unresolvable IID, ambiguous host, ambiguous manual job, unrequested write). Matches contract L59-65.

### Output contract: mandatory fields listed
**PASS** — `## Output Contract` (SKILL.md L359-388) enumerates required, non-droppable fields per query type (list/status/review/manual-job), e.g. "include every matching MR iid, title, state, draft flag, source branch, target branch, and updated_at".

### Output contract: deterministic & parseable
**PASS** — All `gmr` reads emit `print_json` with `indent=2` and stable key ordering via `select_*_fields`; no timestamps injected into the primary payload. (gmr_main.py L78-79, L436-575).

### Output contract: compact agent payload
**PASS** — `select_mr_list_fields` / `select_pipeline_fields` / `select_job_fields` project to compact field sets rather than dumping full API objects; bodies shortened to 400 chars (gmr_main.py L86-90, L689). MR `description` is included in `select_mr_fields` but that is the documented status payload, acceptable.

### No repeated identical reads
**PASS** — "Do not repeat an identical successful read command. Reuse the first successful result..." (SKILL.md L32), reinforced by "After the first successful `<gmr-command>` read, reuse the resolved `hostname`, `repo`, `iid`, and `head_pipeline.id`" (SKILL.md L82).

### One completeness-check, no looping
**PASS** — `## Pre-final Check` (SKILL.md L390-405) is a single drafted-vs-request comparison with explicit "Do this once per response. Do not loop on repeated self-checks." Matches contract L84-88.

### Language discipline
**PASS** — "The final answer must be entirely in the user's language" (SKILL.md L31); the Pre-final Check includes "Is every non-literal part of the final answer written in the user's language?" (SKILL.md L401). Matches contract L89-98 including the dedicated completeness question.

### Section order per contract (setup at BOTTOM)
**PARTIAL** — Order is largely correct (Default Mode → Resolve Context First → Fast Path → reads → MR Mutations → Output Contract → Pre-final Check → Setup → Safety). But `## Setup Fallback` (L407) is NOT the last section — `## Safety Rules` (L442) follows it. Contract L181 places setup/bootstrap at the very bottom before only `## References`. Minor: there is also no `## References` section header (refs are inlined via links at L48, L305). Setup is at least correctly demoted away from the top, so no tutorial-mode regression.

---

## LEVEL 2 — actual scripts (command hygiene)

### Non-interactivity (general)
**FAIL** — `await-pipeline` is steered to interactively ask the user: "Before starting, ask the user for poll interval and timeout. Suggest defaults: 60s interval, 900s timeout." (SKILL.md L249). The command itself is non-interactive (has `--interval`/`--timeout` defaults, gmr_main.py L1270-1271), but the SKILL.md instruction forces a blocking user prompt before every long poll, which is exactly the interaction-stall the contract warns against (contract L105-108). The defaults already exist; the skill should just run with them and not mandate an ask.

### Non-interactivity (auth bootstrap stdin/getpass exception)
**PARTIAL** — `bootstrap_glab_auth` calls `getpass.getpass` on a TTY and `sys.stdin.read()` otherwise (gmr_main.py L795-800). This is the contract-sanctioned auth-bootstrap exception, and it is non-interactive in the agent runner (reads stdin rather than prompting), but it does read stdin, so it is not strictly "never reads stdin". Noted as the documented exception, not a real defect.

### Deterministic output
**PASS** — JSON via `print_json`, stable field projections, no random ordering. `await-pipeline` progress lines go to stderr (gmr_main.py L1047-1053), keeping stdout payload clean. The only stderr "noise" is intentional progress, not in the primary output.

### Error format: stderr + nonzero + actionable
**PASS** — `main()` catches `CommandError` and prints to stderr returning exit 1 (gmr_main.py L1324-1327). Messages are actionable, e.g. auth-missing error tells you the exact remedy: "Bootstrap it with: gmr auth bootstrap {hostname}" (gmr_main.py L772-775).

### Error format: structured hint for structured errors / single guided retry
**PARTIAL** — Errors are actionable prose with embedded remedies (e.g. ambiguous manual job lists matching ids, L750-753; "Use --fill or provide --title", L840). But they are plain strings, not a machine-structured hint object. The contract's "returns a structured hint, agent does one guided retry" (L121) is satisfied in spirit (the message names the exact fix) but not as a structured field. Good enough for steering; not formally structured.

### Read-only default + preview-first mutations
**PASS** — Reads never mutate. Writes are explicitly gated: SKILL.md "Do not approve or merge unless the user explicitly asks for that write action" (L340) and `## Safety Rules` treats approve/merge/rebase/note-resolve/manual-run as write actions requiring explicit intent (L448). Note: GitLab MR create/approve/merge have no true `--apply` dry-run, so the contract's `--apply` preview pattern is replaced by hard refusal guards (see below), which is the appropriate substitution for irreversible remote actions.

### Mutation guards (gitlab-specific verification)
**PASS** — `command_mr_merge` actively refuses unsafe merges: refuses draft MRs (gmr_main.py L1127-1128), refuses failed head pipeline (L1132-1133), and requires `--auto-merge` when the pipeline is still running (L1134-1137). `mr create` is non-interactive with `--yes` and post-reads the created MR (L871-889). Approve/merge support `--sha` head-pin guards (L1094, L1124). This is stronger than preview-first.

### Dependencies: type:system in csk-skill.json
**PASS** — `glab` declared `"type": "system"` with `command` and `hint` in `csk-skill.json` (L9-14).

### Dependencies: checked via which with hint
**PASS** — `require_glab()` uses `shutil.which("glab")` and raises an actionable error when missing (gmr_main.py L757-759); `setup_support.py` also checks `shutil.which` and emits the install hint (L199, L210-216).

### Dependencies: skill must NOT brew install glab
**PASS** — No `brew`/`apt`/auto-install anywhere; setup only *reports* missing deps via hint. dependencies.json `install` field is a human instruction string ("install glab and ensure it is available in PATH"), not an executed command. Matches contract L132-134.

### No ad-hoc fallback (does SKILL.md push the model to raw glab/curl/jq when a gmr command exists?)
**PASS** — SKILL.md consistently steers `gmr` first and scopes raw `glab` to cases the wrapper deliberately does not cover: "Prefer `<gmr-command>` for agent-facing MR lists, MR status, pipeline diagnostics..." (L43), and raw `glab` is reserved for rebase/note and "only when you need the raw pipeline view or non-MR pipeline operations" (L256), or "Only if the high-level output is incomplete" (L145). No jq/grep/python-c fallbacks suggested. The CLI-surface reference reinforces "Prefer `gmr` over raw `glab` whenever both can do the job" (cli-surface.md L37).

### Secrets only in system keyring (glab --use-keyring)
**PASS** — `bootstrap_glab_auth` passes `--use-keyring` to `glab auth login` (gmr_main.py L806). Token is fed via `--stdin` (L807, L814), never written to disk or argv. `## Safety Rules`: "Keep GitLab tokens in the OS keyring through `glab auth login --use-keyring`. Do not save GitLab tokens in shell history, env files, or checked-in files." (SKILL.md L444-445). No plaintext fallback.

### Idempotent (repeat auth no dup)
**PASS** — `ensure_glab_auth` only checks `glab auth status` and returns on success without re-login (gmr_main.py L762-775). Re-running `auth bootstrap` re-invokes `glab auth login --hostname` for the same host, which overwrites the existing keyring entry rather than duplicating it (glab keys auth by hostname). Reads are inherently idempotent.

---

## LEVEL 3 — tool surface

### Meaning-commands / answer-shaped payloads
**PASS** — The skill provides intent-level commands rather than forcing the model to compose primitives: `mr list --mine`, `--mine-role assignee|reviewer` (one command per "my MRs / assigned / review queue" intent), `mr status`, `mr review-context`, `mr manual-jobs`, `mr await-pipeline`. Each returns an answer-shaped JSON payload (e.g. `gather_status` pre-extracts failed-job `failure_excerpt` from traces, gmr_main.py L701-728), so the model gets the root cause already mined, not a raw trace to interpret. This is precisely contract L159's meaning-command level.

### Stable JSON agent-facing reads, separate from human output
**PARTIAL** — Every `gmr` read already emits stable, indented, field-projected JSON designed for the model — there is effectively no separate "human-facing" text format to diverge from, so agent-facing stability is achieved. But the contract's explicit pattern (contract L164-167: "a separate command emitting stable JSON, distinct from human-facing output") is not formally realized as two commands; there is one JSON output serving both. In practice this is fine (JSON is the only format), but it means the formal agent/human split does not exist as separate entrypoints.

---

## Concrete fix list (not applied)

1. **`await-pipeline` interactive ask (FAIL):** Remove "Before starting, ask the user for poll interval and timeout" from SKILL.md L249. The command already defaults to 60s/900s; just run it. Keep the ask only if the user gave no MR context at all.
2. **Section order (PARTIAL):** Move `## Safety Rules` above `## Setup Fallback` so Setup is the last content section before references; optionally add an explicit `## References` section at the very bottom.
3. **Structured error hints (PARTIAL):** Consider emitting a small structured hint (e.g. `{"error": ..., "hint": ..., "retry_with": ...}`) on type/flag-mismatch errors so the agent does one guided retry instead of parsing prose.
4. **Agent/human split (PARTIAL):** Not actionable as a defect — output is already JSON-only. Document that the JSON read commands *are* the agent-facing surface.
