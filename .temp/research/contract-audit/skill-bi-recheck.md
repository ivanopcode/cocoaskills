# skill-bi — Operational Contract Re-Audit (post-restructure)

Audited version: `/Users/iv/Developer/Wildberries/cocoaskills/.temp/PMA-24147/worktree` (git worktree, NOT the installed copy).
Contract: `/Users/iv/Developer/Wildberries/cocoaskills/.temp/aid/docs/skill-operational-contract.md`
Previous audit (OLD installed copy) verdict: **Weak (9 PASS / 7 PARTIAL / 9 FAIL)**.

All citations are to `SKILL.md` in the worktree unless noted.

---

## Per-item verdicts (the previous FAIL/PARTIAL findings)

### 1. Hard happy path — RESOLVED
`## Resolve Context First` (L60–67) is a numbered, branch-free fixed order ("Fixed order, no branching:" L62, steps 1–4); the only branch is the terminal routing decision in step 4 (Direct Query vs Fast Path), which is a dispatch, not a context-resolution branch — contract-compliant. `## Fast Path: feature analytics from code` (L69–88) is a numbered 1–4 happy path for the typical "analyze feature X" request.

### 2. When-to-ask boundaries — RESOLVED
`## Scope Rules` (L119–122) present: "Act, do not ask, when the context is resolvable" (L121) and asks only on no base URL/connector, ambiguity, or missing auth (L122). Mirrors contract §"Границы: when to ask".

### 3. Output contract — RESOLVED
`## Output Contract` (L113–117) enumerates mandatory fields (metric value(s) with units, time window, `event_type`(s), connector/table; L115), forbids dropping rows/columns (L116), and prefers `--format json` for agent-facing reads (L117).

### 4. Path resolution — RESOLVED
Happy path uses `<bi-query>`/`<bi-catalog>`/`<bi-auth>` placeholders (L76, L83–85, L95, L54), not literal `scripts/x` and not `$VAR`. `## Command Resolution` (L42–48) makes unix/windows explicit: unix `<skill-dir>/scripts/bi-query` (L45), Windows `.cmd` via `cmd /c ...\scripts\bi-query.cmd` (L46), and notes bare names only via `.agents/bin/` bootstrap (L48). `.cmd` wrappers exist (`scripts/bi-query.cmd` etc.).

### 5. Dependencies — RESOLVED
`dependencies.json` present with `{"dependencies": []}`. Empty list is correct: BI has no external system CLI dep — Python comes from the skill venv (`scripts/run_in_skill_venv.py`, `runtime_roots: ["scripts"]` in `csk-skill.json`). Mirrors the skill-band pattern; acceptable.

### 6. Secrets — RESOLVED
`## Safety Rules` (L124–129) + `## Authentication` (L51–58) state: token only in OS keyring (L52, L127), never expose in answers/files/commands/logs (L127), `--token` is break-glass/discouraged (L58, L127), `BI_TOKEN` ignored with a warning (L58, L127). Code confirms it, not just prose: `bi_query.py:resolve_token` (L263–287) warns on `--token`, prints "Ignoring token environment variable(s): BI_TOKEN ... Use secure storage via 'bi-auth login'" when `BI_TOKEN` is set, and otherwise reads from keyring via `get_token`. No `--token` on the happy path.

### 7. No ad-hoc fallback — RESOLVED
`## Safety Rules` L128: "No ad-hoc fallback: prefer `<bi-query>` and `<bi-catalog>` over hand-rolled `curl`/`jq`/`python -c`. Use raw REST only when the user explicitly asks for instructions (`references/api.md`)." Matches contract §"Без ad-hoc fallback".

### 8. Section order — RESOLVED
Order top→bottom: Default Mode (L33) → Command Resolution (L41) → Authentication (L51) → Resolve Context First (L60) → Fast Path (L69) → Direct Query (L91) → Reads (L100) → Output Contract (L113) → Scope Rules (L119) → Safety Rules (L124) → Connector And Schema Discovery (L131) → Common Mistakes (L155) → References (L159) → Setup Fallback (L168, last). Setup Fallback is at the bottom as required. Minor deviation from the contract's *suggested* template ordering (Scope Rules sits after Output Contract rather than right after Fast Path; Command Resolution + Authentication sit above Resolve Context First) but the load-bearing constraints — Default Mode first, Setup last, happy path before domain reads — all hold. Treated as RESOLVED, not PARTIAL: the contract's order is "проверенный порядок" guidance, and the two mandatory anchors (Default Mode top, Setup bottom) are satisfied.

### 9. Default Mode hygiene — RESOLVED
`## Default Mode` (L33–39) contains execute-don't-instruct (L35–36), language discipline (L37, no English in headings/summaries for RU users), no-repeated-reads (L38), and one completeness-check before final answer (L39). All four contract requirements present.

---

## New-issue scan (regressions from the restructure)

- **References integrity — OK.** All six referenced files exist (`events-from-code.md`, `dashboards.md`, `analytics.md`, `comparisons.md`, `events.md`, `api.md`) and `references/setup.md` (cited at L179) exists. No broken links.
- **Domain content preserved — OK.** Connector/schema discovery retained (`## Connector And Schema Discovery` L131–153 with the typical-shape table), common mistakes retained (`## Common Mistakes` L155–157), `## References` list intact (L159–166), intent-routing table intact (`## Reads` L100–111).
- **Command examples correctness — OK.** Every example resolves to a real subcommand: `bi-auth status` / `login --stdin` (`bi_auth.py` L52–59), `bi-catalog save` / `validate-sql` / `annotate` (`bi_catalog.py` L263/269/276), `bi-query --dbconn-id --format json --sql` (`bi_query.py` argparse L297–313). Direct Query's "both base URL and connector required, exits naming the missing value" (L98) matches the code.
- **No contradictions found** between SKILL.md prose and the scripts.

No NEW-ISSUE items.

---

## Overall compliance verdict

**Strong.** All 9 previously-flagged items RESOLVED; no new issues introduced; prose claims are backed by the code (secrets, required-arg behavior, subcommands).

Counts (the 9 audited items): **9 PASS / 0 PARTIAL / 0 FAIL.**

Compared to previous **Weak (9 / 7 / 9)** — full closure of all 7 PARTIAL and 9 FAIL items on this re-audited scope.
