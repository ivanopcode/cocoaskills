# Contract Audit: skill-bi

Audited skill: `/Users/iv/agents/skills/skill-bi`
Contract: `/Users/iv/Developer/Wildberries/cocoaskills/.temp/aid/docs/skill-operational-contract.md`
Date: 2026-06-05

## Top-line summary

**Overall compliance: WEAK**

- PASS: 9
- PARTIAL: 7
- FAIL: 9
- N/A: 2

**Critical note on the brief premise.** The task stated the skill "was just migrated to keychain auth (bi-auth command)". This is FALSE for the current state on disk. There is no `bi-auth` command, no keychain/keyring/Secret-Service code anywhere in the skill (`grep` for keychain|keyring|bi-auth|secret|credential returns only one hit: `BI_TOKEN` env in SKILL.md line 51). `csk-skill.json` declares only `bi-query`. Git log shows the latest commit is `651a3a4 Перевести skill-bi на csk-skill schema v2` — there is no keychain migration commit. The skill authenticates via a `--token` CLI flag (required) and a `BI_TOKEN` env fallback. The audit below judges the actual on-disk state.

---

## LEVEL 1 — Agent behavior (SKILL.md)

| Rule | Verdict | Evidence |
|------|---------|----------|
| `## Default Mode` with execute-don't-instruct wording | PASS | SKILL.md:27-30 has `## Default Mode` with "Execute queries yourself and return results directly" and "Show raw curl commands only when the user explicitly asks for instructions or when authentication is missing." |
| Path resolution: absolute from SKILL.md; happy path uses `<command>` placeholders, NOT `scripts/x`, NOT `$VAR`; explicit unix vs windows | PARTIAL | Default Mode correctly says "Resolve command paths from this skill file path" with explicit unix/windows variants (SKILL.md:36-42), BUT the actual happy-path examples use the literal `scripts/...` form the contract forbids: `<skill-path>/scripts/bi-query` (SKILL.md:152, references/api.md:211) instead of a bare `<command>` placeholder. |
| Hard happy path: `## Resolve Context First` (single fixed order, no branching) AND numbered `## Fast Path` | FAIL | There is no `## Resolve Context First` and no numbered `## Fast Path` section anywhere in SKILL.md; the closest is prose `## Running a Query` (SKILL.md:145) with a single example, not a fixed numbered happy path. |
| When-to-ask boundaries (only if no context / unresolvable ambiguity / auth missing) | FAIL | No `when to ask` boundaries are stated anywhere in SKILL.md; the agent is never told when it may stop and ask the user. |
| Output contract: mandatory output fields enumerated; deterministic parseable format; compact agent payload | FAIL | SKILL.md has no output-contract section; it never enumerates mandatory fields the model must not drop, nor specifies a compact agent-facing payload. The script's default human output is a markdown table (bi_query.py:162-166). |
| No repeated identical reads; exactly one completeness-check | PARTIAL | One completeness check is mandated ("do one completeness check against the original user request", SKILL.md:35), but there is no rule against repeating identical successful reads / reusing the first result. |
| Language discipline (final answer fully in user language; English only for literals) | PASS | SKILL.md:31-34 mandates final answer entirely in user's language, no English in headings/summaries, English only for SQL identifiers/event names/URLs. |
| SKILL.md section order matches contract; setup at BOTTOM (flag if setup near top) | PARTIAL | Setup (`## Setup Fallback`) is correctly at the very bottom (SKILL.md:188), but the prescribed order is otherwise not followed: required sections (`Resolve Context First`, `Fast Path`, `Scope Rules`, `Mutations`, `Safety Rules`) are absent, and an `## Authentication` how-to block sits high up (SKILL.md:44), which leans toward tutorial mode. |

---

## LEVEL 2 — Command hygiene (actual scripts)

| Rule | Verdict | Evidence |
|------|---------|----------|
| Non-interactive: no stdin / no prompts (one-time auth login may prompt) | PASS | `bi_query.py` takes everything via argparse flags, never reads stdin or calls `input()`; the wrapper `bi-query` just `exec`s python (bi-query:15). No interactive prompt anywhere. |
| Deterministic output (no random order, no timestamps in main output) | PARTIAL | Table/JSON output is deterministic, BUT the main output line prints `Duration: ...s` (bi_query.py:162), a non-deterministic timing value the contract flags as noise in the main output (progress noise goes to stderr, but duration is on stdout). |
| Error format: stderr, nonzero exit, actionable; structured hint on structured errors | PARTIAL | Errors go to stderr with nonzero exit (`ERROR: ...` + `SystemExit(1)`, bi_query.py:188-190) and HTTP errors include body (line 54), but messages are not actionable (no "what to do next") and there is no structured-hint / guided-retry path for structured errors like field-type mismatch. |
| Read-only by default; mutations preview-first | N/A | The skill is read-only by design (SQL SELECT execution via queue); it has no mutating commands, so preview-first does not apply. |
| Dependencies declared `type: system` in csk-skill.json and checked via `which` with hint; no `brew install` | FAIL | `csk-skill.json` has NO dependencies block at all — `python3` (the hard runtime requirement) is never declared as `type: system`, and nothing checks `shutil.which`. (It correctly does not run `brew install`, but the declaration requirement fails.) |
| No ad-hoc fallback baked into instructions (jq/grep/python -c/raw REST) when a high-level command exists | FAIL | SKILL.md and references push raw REST as a first-class path: the whole `## Three-Step Query Flow` with raw `POST/GET` curl-style endpoints (SKILL.md:111-143), `## Dashboard Navigation` raw GETs (SKILL.md:158-169), and references/api.md:72-76 instructs an ad-hoc `python ... re.findall(r'dataSourceId...')` to scrape dataSourceIds — exactly the ad-hoc fallback the contract forbids (there is no `bi-query` subcommand for dashboards/datasources). |
| Secrets only in system keyring; no plaintext/env-file fallback | FAIL | Token is passed as `--token` (required CLI flag, bi_query.py:171) — lands in shell history and process args — with a documented `BI_TOKEN` environment fallback (SKILL.md:51). No keychain/keyring usage exists. This is the core regression vs. the claimed migration. |
| Idempotent (repeat call/login does not duplicate state) | PASS | `bi-query` is stateless per invocation (submits a fresh queue request each run, bi_query.py:64-75); repeating it creates no duplicated persistent state. |

---

## LEVEL 3 — Tool surface

| Rule | Verdict | Evidence |
|------|---------|----------|
| Meaning-commands / answer-shaped payloads for typical requests | FAIL | The only command is the generic `bi-query --sql <raw SQL>`; there is no meaning-command (e.g. `bi-query dau --event X --days 7`) for the typical requests the triggers advertise ("сколько пользователей", funnels, DAU). The model must hand-author SQL every time — the decision stays in the prompt, not in code. |
| Stable JSON agent-facing reads separate from human output | PARTIAL | A `--format json` flag exists (bi_query.py:157-160) that dumps the raw API response, but it is the same command's mode (not a separate agent-facing command) and emits the raw provider payload rather than a stable, schema-pinned agent shape; the default mode is human markdown table. |

---

## Key remediation priorities

1. **Secrets (Level 2 FAIL).** Implement the claimed keychain auth: a `bi-auth` command that stores the token in macOS Keychain / Secret Service, and have `bi-query` read it from there. Remove the `--token` required flag and the `BI_TOKEN` env fallback from the happy path.
2. **Hard happy path (Level 1 FAIL).** Add `## Resolve Context First` (fixed order) and a numbered `## Fast Path: <typical query>`.
3. **Meaning-commands (Level 3 FAIL).** Add answer-shaped subcommands for DAU/funnel/unique-users so the model is not authoring raw SQL and raw REST.
4. **Kill ad-hoc REST/regex fallback (Level 2 FAIL).** Replace the raw three-step REST flow and the `re.findall` dataSourceId scrape with first-class `bi-query` subcommands (dashboards, datasource).
5. **Declare deps (Level 2 FAIL).** Add `python3` as `type: system` in `csk-skill.json` with a `which` check + hint.
6. **Output contract + when-to-ask (Level 1 FAIL).** Add an enumerated output-contract section and explicit when-to-ask boundaries.
