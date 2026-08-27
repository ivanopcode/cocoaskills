# Compliance Backlog — agentic-infra skills vs operational contract

Source: completed audits in this folder (`SUMMARY.md` + per-skill scorecards) and the contract at
`/Users/iv/Developer/Wildberries/cocoaskills/.temp/aid/docs/skill-operational-contract.md`.
Every concrete claim below was re-verified against real code on 2026-06-05. File:line citations are exact.

Audited skill sources:
- gitlab/youtrack/sentry/grafana: `/Users/iv/agents/skills/skill-<name>`
- band: `/Users/iv/Developer/Wildberries/cocoaskills/.temp/skill-band`
- bi (already brought to Strong): `/Users/iv/Developer/Wildberries/cocoaskills/.temp/PMA-24147/worktree`

Verdict matrix (from SUMMARY.md): gitlab / youtrack / sentry / grafana / band = **Strong**; bi = **now Strong** post-restructure. Remaining work is the long tail of PARTIAL/FAIL plus two real P1 bugs.

---

## Verified P1 bugs (must-fix)

### B1 — gitlab `await-pipeline` instructs an interactivity stall
- **Evidence (SKILL.md):** `/Users/iv/agents/skills/skill-gitlab/SKILL.md:249`
  > `- Before starting, ask the user for poll interval and timeout. Suggest defaults: 60s interval, 900s (15min) timeout.`
- **Evidence (command already has defaults):** `/Users/iv/agents/skills/skill-gitlab/scripts/gmr_main.py:1270-1271`
  > `await_parser.add_argument("--interval", type=int, default=60, ...)` / `await_parser.add_argument("--timeout", type=int, default=900, ...)`
- The command is fully non-interactive with sane defaults. The SKILL.md line forces a blocking question before every long poll — directly violates contract §"Неинтерактивность" / §"Границы: when to ask".

### B2 — grafana `analyze.py` masks failed sub-queries as success
- **Evidence:** `/Users/iv/agents/skills/skill-grafana/scripts/analyze.py:33-55`. `json_request` prints `ERROR: ...` to stderr on `URLError` (L48-49) and on `JSONDecodeError` (L54-55) but `return fallback` and the report keeps building. `main()` only ever exits nonzero on argparse errors (`parser.exit(1, ...)` at L250); a failed auth/datasource/network sub-query produces a hollow report at **exit 0**.
- Violates contract §"Формат ошибок" (error → nonzero exit, actionable, don't fake success).

---

## Backlog items (prioritized, deduplicated)

### P1

#### I1. Remove the await-pipeline interactivity ask
- **Skills:** gitlab
- **Category:** bug
- **Current state:** SKILL.md:249 mandates asking the user for interval/timeout; defaults already exist (gmr_main.py:1270-1271). See B1.
- **По феншую:** the model runs `await-pipeline` immediately with built-in defaults; it asks for nothing. The only ask is the contract-allowed "no MR context at all" case (already covered by `## Ask Only If Blocked`).
- **Acceptance criteria:**
  - [ ] SKILL.md:249 line removed (or rewritten to "run with default 60s/900s; pass `--interval`/`--timeout` only if the user specified values").
  - [ ] No remaining SKILL.md instruction tells the model to ask before a read/poll.
  - [ ] Fast Path / await section still documents the defaults so the model knows them.
- **Effort:** S

#### I2. Make analyze.py fail loud on hard sub-query failures
- **Skills:** grafana
- **Category:** bug
- **Current state:** `json_request` swallows URL/JSON errors into `fallback`, report continues, exit 0 (analyze.py:33-55). See B2.
- **По феншую:** a failed auth or datasource fetch yields a nonzero exit with an actionable stderr message; OR, where partial output is genuinely wanted, the failed section is explicitly marked DEGRADED in the report AND a nonzero exit (or machine-readable degraded flag) signals incompleteness so the agent never presents a hollow report as complete.
- **Acceptance criteria:**
  - [ ] Auth/network/parse failure on a required sub-query → nonzero exit with actionable stderr (matches `grafana_query.py` / `grafana_auth.py` style).
  - [ ] If degraded-but-continue is intentional for optional sections, those sections are clearly labeled as failed/degraded in the emitted report.
  - [ ] A smoke run with a deliberately broken datasource exits nonzero (or prints an unmissable degraded marker), not a clean-looking report at exit 0.
- **Effort:** M

#### I3. Add a send gate to band post/dm (outward messaging)
- **Skills:** band
- **Category:** tool-surface (bug-adjacent — outward action with no machine gate)
- **Current state:** `cmd_post` / `cmd_dm` send immediately, no `--apply`/preview param (band_api.py:281, 294). The only guard is a prompt-level confirmation rule in SKILL.md:60-65 (steering, no runtime guarantee). Contract §"Read-only по умолчанию": mutating action needs an explicit flag/confirmation, non-destructive writes preview-first.
- **По феншую:** sending a post/DM requires an explicit intent flag at the tool layer (e.g. `--apply`/`--send`); without it the command prints a preview (target channel/user + resolved message + permalink-to-be) and exits without sending. The agent does preview→inspect→`--apply` in one turn. A messaging send is outward and effectively irreversible, so the gate must be in code, not only in the prompt.
- **Acceptance criteria:**
  - [ ] `post`/`dm` without the send flag print a preview and do NOT hit the network send endpoint.
  - [ ] `post`/`dm` with the explicit flag send and return the existing `permalink`/`post_id` payload.
  - [ ] SKILL.md `## Write Safety` updated to the preview→`--apply` flow; output contract still lists permalink+post_id.
  - [ ] Idempotency note: preview is side-effect-free.
- **Effort:** M
- **Note:** if the team decides prompt-level confirmation is acceptable for a send-only API, downgrade to P2 and keep only the SKILL.md hardening. Recorded here as P1 because it is the only outward-effecting action in the family with no code gate.

### P2

#### I4. Add numbered Fast Path + hard Resolve-Context-First to sentry, grafana, band
- **Skills:** sentry, grafana, band
- **Category:** contract-structure
- **Current state:**
  - sentry: routes via a "Workflow Selection" table + "Quick Commands", no single numbered `## Fast Path` (skill-sentry.md L30).
  - grafana: four Fast Path sections exist but bulleted/prose, not numbered (skill-grafana.md L30); Resolve Context First is present and ok.
  - band: **no** `## Fast Path` at all (FAIL) — branchy `## Workflow Selection` table (skill-band.md L33); `## Resolve Context First` is one prose paragraph, not a numbered no-branch sequence (skill-band.md L32).
- **Template:** bi's now-Strong SKILL.md — `## Resolve Context First` (numbered, branch-free, terminal dispatch only) + `## Fast Path: <typical request>` (numbered 1–4). See bi-recheck.md §1.
- **По феншую:** each skill has a single numbered `## Resolve Context First` startup order and at least one numbered `## Fast Path` for its headline request, replacing/Supplementing the branchy tables so a weak model has one deterministic path.
- **Acceptance criteria:**
  - [ ] sentry, grafana, band each have a numbered `## Fast Path: <typical request>`.
  - [ ] band's `## Resolve Context First` becomes a numbered branch-free order (terminal dispatch only).
  - [ ] grafana Fast Path steps converted to numbered steps.
  - [ ] Existing Workflow Selection tables retained only as secondary routing, not the primary happy path.
- **Effort:** M

#### I5. Declare system dependencies (`type: system`) in youtrack and grafana manifests
- **Skills:** youtrack, grafana (gitlab is the reference; bi/band correctly `[]`)
- **Category:** dependency-declaration
- **Current state:**
  - youtrack `csk-skill.json` has no `dependencies` block (verified — only `commands`). youtrack genuinely shells out to `git`: `ytx.py:446` (`git config user.email` for `--mine`), `setup_support.py:436` (`git rev-parse`). So `git` is a **real** `type:system` dep that must be declared. (Python comes from the skill venv, like band/bi → not a system dep.)
  - grafana `csk-skill.json` has no `dependencies` block (verified). grafana runs entirely through its own Python venv wrapper; it has **no external system CLI** dependency. Per the band/bi precedent (`dependencies.json: {"dependencies": []}`, Python from venv), grafana's correct compliant state is an explicit empty declaration, NOT inventing a `python3` system entry.
  - Reference: gitlab declares `glab` as `{"type":"system","command":"glab","hint":"..."}` in csk-skill.json AND has a `dependencies.json` entry, and checks via `shutil.which`.
- **По феншую:**
  - youtrack: declare `git` as `type:system` in `csk-skill.json` (with `command:"git"` + actionable `hint`), add it to `dependencies.json`, and gate `--mine`/setup git calls behind a `shutil.which("git")` check that emits the hint when missing.
  - grafana: add an explicit `dependencies.json` with `{"dependencies": []}` (matching band/bi) to make the "no system deps, Python from venv" decision intentional and visible. No `type:system` python entry.
- **Acceptance criteria:**
  - [ ] youtrack `csk-skill.json` declares `git` as `type:system` with command+hint.
  - [ ] youtrack `dependencies.json` lists `git`.
  - [ ] youtrack `--mine` path checks `shutil.which("git")` and emits the hint (not a raw traceback) when git is absent.
  - [ ] grafana has an explicit `{"dependencies": []}` (or documented equivalent) confirming no system CLI dep.
  - [ ] Neither skill runs `brew install`/`apt`.
- **Effort:** S (youtrack S–M for the which-gate)

#### I6. Fix section order — move `## Safety Rules` above `## Setup Fallback`
- **Skills:** youtrack, gitlab, grafana
- **Category:** consistency / contract-structure
- **Current state:** Safety Rules sits BELOW Setup Fallback. youtrack: Safety L330 after Setup L279 (skill-youtrack.md PARTIAL). gitlab: Safety L442 after Setup L407 (skill-gitlab.md PARTIAL). grafana: Configuration/secrets policy sits above Resolve Context First and there is no dedicated `## Safety Rules` block lower down (skill-grafana.md PARTIAL). Contract §"Структура SKILL.md": Safety Rules → then setup at the very bottom (before References only).
- **По феншую:** for each skill the order is `... → ## Safety Rules → ## Setup Fallback → ## References`. Setup remains last content (anti-tutorial intent preserved). grafana additionally gets a labeled `## Safety Rules` block carved from `## Configuration`.
- **Acceptance criteria:**
  - [ ] youtrack/gitlab: `## Safety Rules` precedes `## Setup Fallback`.
  - [ ] grafana: secrets/safety policy lives in a `## Safety Rules` block above Setup Fallback.
  - [ ] Setup Fallback remains the last content section (References allowed after).
- **Effort:** S

### P3

#### I7. Emit machine-structured error hints for guided retry
- **Skills:** gitlab, sentry, grafana, band (youtrack already PASS — has structured hints: `field_type_mismatch`, `retry_with_fields`, etc., ytx.py)
- **Category:** consistency / tool-surface
- **Current state:** PARTIAL across the four. Errors are actionable prose with embedded remedies but not a machine-structured object. gitlab (skill-gitlab.md L84), sentry (skill-sentry.md L53), grafana (covered by I2 for the masking case), band (actionable prose, no structured hint). Contract §"Формат ошибок": on a structured error (e.g. field-type mismatch) return a structured hint, agent does ONE guided retry.
- **По феншую:** on structured/recoverable errors (type mismatch, missing required field, bad enum), commands emit a small structured hint object (e.g. `{"error":..., "hint":..., "retry_with":...}`) modeled on youtrack's existing hints, so the agent does one targeted retry instead of guessing flag syntax.
- **Acceptance criteria:**
  - [ ] At least the common structured-error classes per skill emit a structured hint field.
  - [ ] youtrack's hint shape used as the template.
  - [ ] Actionable prose retained for human-facing errors.
- **Effort:** M
- **Note:** assessed as worth a single cross-skill task, NOT acceptable-as-prose, because youtrack already proves the pattern and the contract calls it out explicitly. Lower priority because current prose is actionable.

#### I8. Add agent-facing stable-JSON read for the flagship analyze/REST reads
- **Skills:** grafana (analyze emits Russian markdown only), sentry (generic REST passthrough, no skill-defined answer schema)
- **Category:** tool-surface
- **Current state:**
  - grafana: `grafana-analyze` emits only human-facing Russian markdown; no parallel stable-JSON agent payload (skill-grafana.md L64 PARTIAL). `grafana-query` already has `--compact`/`--extract`/csv.
  - sentry: data reads are a generic REST passthrough with no dedicated agent-facing JSON command / answer schema (skill-sentry.md L67 FAIL); only `auth status --json` is split.
- **По феншую:** grafana-analyze gains an `--emit json` / sidecar stable-JSON mode; sentry grows a small set of meaning-commands (or at minimum a stable agent-facing JSON read) for its headline intents so the model consumes structured payloads, not parsed prose.
- **Acceptance criteria:**
  - [ ] `grafana-analyze --emit json` (or equivalent) produces a stable, schema-pinned payload alongside markdown.
  - [ ] sentry exposes at least one agent-facing stable-JSON read distinct from the raw passthrough (or a meaning-command per headline intent).
  - [ ] Existing human output unaffected.
- **Effort:** L
- **Note:** feature-level / upper-tier of the contract ("don't build prematurely"). Real but lowest priority; split grafana vs sentry if scheduled separately.

#### I9. Remove bi `--token` break-glass flag for full strictness
- **Skills:** bi
- **Category:** consistency
- **Current state:** bi is Strong, but still keeps a `--token` break-glass flag (`bi_query.py:298`, help text "Discouraged break-glass option: the token is visible...", warning at L264-267). band is stricter — no `--token` anywhere; token only via keyring (skill-band.md token section).
- **По феншую:** bi matches band — no `--token` flag at all; token strictly from keyring via `bi-auth`. `BI_TOKEN` env already ignored-with-warning (keep that).
- **Acceptance criteria:**
  - [ ] `--token` argument removed from bi_query.py argparse.
  - [ ] `resolve_token` reads keyring only; `BI_TOKEN` still ignored-with-warning.
  - [ ] SKILL.md Safety Rules no longer mentions a break-glass token flag.
  - [ ] Auth happy path unchanged (`bi-auth login`).
- **Effort:** S
- **Note:** optional hardening; only do it if the team wants band-level strictness. Could be dropped if break-glass is deliberately retained.

---

## Items assessed and merged/dropped

- "Numbered Fast Path missing in sentry/grafana/band" → merged into **I4** (one task, three skills) plus grafana Resolve-Context already ok.
- "type:system missing in youtrack and grafana" → merged into **I5**, but split in spec: youtrack needs a real `git` entry; grafana correctly gets `[]` (do NOT invent a python system dep — Python is venv-provided, per band/bi precedent).
- "Structured error hints across most skills" → kept as **I7** (worth a task, youtrack excluded as already-PASS); priority P3 because current prose is actionable.
- "Agent-facing JSON / meaning-commands sentry + grafana" → kept as **I8** (feature-level, L, P3).
- gitlab/band agent-vs-human JSON split PARTIALs → NOT actionable defects (output is already JSON-only); documented, no task.
- bi residual → **I9** only (the `--token` flag); all other bi FAILs already closed in the worktree (bi-recheck.md: 9/0/0).
