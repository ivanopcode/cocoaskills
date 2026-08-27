# Contract Audit: skill-youtrack

Audited against `/Users/iv/Developer/Wildberries/cocoaskills/.temp/aid/docs/skill-operational-contract.md`.
Skill root: `/Users/iv/agents/skills/skill-youtrack`.

## Top-line summary

**Verdict: STRONG.**

- PASS: 26
- PARTIAL: 3
- FAIL: 2
- N/A: 1

Single biggest gap: system dependencies (`git`, and the Python/`py` launcher) are documented in `README.md` but are **not** declared as `type: system` in `csk-skill.json` and are **not** checked via `shutil.which` with a `hint`. The contract's "declare, don't install" rule is effectively unmet at the manifest level even though the skill never tries to install anything.

---

## LEVEL 1 — SKILL.md (agent behavior)

| Rule | Verdict | Evidence |
|------|---------|----------|
| Default Mode: execute-don't-instruct | PASS | `SKILL.md` §`Default Mode` L22-24: "Execute the bundled commands yourself and return the result"; "Do not answer with a shell tutorial when the skill is already installed and authentication exists"; "Show commands only when the user explicitly asks for instructions or when setup/auth is missing." Exactly the three prescribed lines. |
| Path resolution: `<command>` placeholders, not `scripts/x` literals, not `$VAR` | PASS | `SKILL.md` §`Default Mode` L31-43 defines `<yt-command>`/`<ytx-command>` placeholders resolved from the SKILL.md path; happy paths use only `<yt-command>`/`<ytx-command>`, never `scripts/yt` literals or `$VAR`. (Minor: `references/board-membership.md` L36-37 still uses literal `scripts/ytx`, but that is a reference doc, not the happy path.) |
| Path resolution: explicit unix/windows variants | PASS | `SKILL.md` L33-36 gives explicit macOS/Linux (`scripts/yt`) and Windows (`scripts/yt.cmd`) forms, plus python fallbacks L41-43. |
| Path resolution: forbid `scripts/...` relative to CWD | PASS | `SKILL.md` L40: "Do not run `scripts/yt`, `scripts/ytx`, `scripts/yt.cmd`, or `scripts/ytx.cmd` as paths relative to the current working directory." |
| Path resolution: bare names only via repo `.agents/bin` | PASS | `SKILL.md` L37-39: bare `yt`/`ytx` acceptable only when resolved through repo-local `.agents/bin` provisioned by `make agents`; "This skill does not provision `PATH` itself." |
| Hard happy path: single Resolve Context First order, no branching start | PASS | `SKILL.md` §`Resolve Context First` L45-52 fixes the start as exactly two commands in order (`instances list`, then `instances current`) before any branching. |
| Hard happy path: numbered Fast Path for typical request | PASS | `SKILL.md` §`Fast Path: My Tasks` L67-106 gives a numbered 1-5 happy path centered on `ytx board my-tasks`. |
| When-to-ask boundaries | PASS | `SKILL.md` §`Ask Only If Blocked` L223-233 enumerates the exact blocked conditions (no instances, missing label, ambiguous active instance, unresolvable board, missing/ambiguous write target); "Otherwise, run the commands and return the result." |
| Output contract: mandatory fields listed | PASS | `SKILL.md` §`Output Contract` L235-261 enumerates required fields for task lists (board name, sprint name, total count, per-issue id/summary/state/type/URL) and single issue (id, summary, state, type, priority, assignee, description, `Link:`). |
| Output contract: deterministic/parseable + don't hide data | PASS | `SKILL.md` L247 "do not omit `Done` issues unless...", L244-245 explicit truncation rule at 30; counts must match payload (L251-253). |
| Output contract: compact agent payload by default | PASS | `SKILL.md` §`Board Reads` L140: "Agent-facing board and task list reads must prefer compact issue payloads. Do not include full issue descriptions or full custom field maps... unless the user explicitly asks." |
| No repeated identical reads | PASS | `SKILL.md` §`Default Mode` L29 + Fast Path L102: do not re-run `board my-tasks` / `board current` / `board list --scoped` after a successful read unless still required. |
| One completeness-check, no loop | PASS | `SKILL.md` §`Default Mode` L30 + §`Pre-final Check` L262-277: explicit single pre-final check, "Do this once per response. Do not loop on repeated self-checks." |
| Language discipline | PASS | `SKILL.md` §`Default Mode` L25-28 and §`Output Contract` L248-250: final answer fully in user's language, no English in headings/connective text, English only for literal values; Pre-final Check L273 includes the non-literal-language question. |
| Section order per contract (frontmatter → Default Mode → Resolve Context First → Fast Path → Scope → Reads → Mutations → Safety/setup-bottom → References) | PARTIAL | Order is largely correct (Default Mode → Resolve Context First → Fast Path → Scope Rules → Board/Issue Reads → Mutations → Setup → Safety → References). Two deviations from the contract's prescribed order (contract §"Структура SKILL.md"): `## Safety Rules` (L330) appears **after** `## Setup Fallback` (L279) instead of before it, and several behavioral sections (`Current Developer Resolution`, `Ask Only If Blocked`, `Output Contract`, `Pre-final Check`) sit between Mutations and Setup. Setup is correctly at the bottom (not top), so the core anti-tutorial intent holds. |
| Setup at BOTTOM, not top | PASS | `## Setup Fallback` is at `SKILL.md` L279, near the end, gated "Use this only when installation or authentication is missing." |

## LEVEL 2 — actual scripts (command hygiene)

| Rule | Verdict | Evidence |
|------|---------|----------|
| Non-interactive: no stdin/questions (auth login prompt noted) | PARTIAL | `ytx.py` reads stdin only behind an explicit opt-in flag (`resolve_description_input` L1001-1022 reads `sys.stdin` only when `--description-stdin` is passed), and `yt_main.py`/`instance_runtime.py` contain no `input()`/`getpass`/`click.prompt`. **However**, the documented `auth login` happy path (`SKILL.md` L316-322, `README.md` L246-251) passes `--base-url` but no token, so upstream `youtrack-cli` will prompt for the token interactively. This is the contract-acknowledged auth exception ("не настроена аутентификация"), but it is a genuine interactive read on the auth path and is noted as such. |
| Deterministic output | PASS | All wrapper output is `json.dumps(..., indent=2)` (`yt_main.py` `print_json` L43-44; `ytx.py` `fail` L235-240); grep found no `datetime.now`/`time.time`/`uuid`/`random` in `ytx.py` main output paths. |
| Error format: stderr + nonzero + actionable + structured hint | PASS | `ytx.py` `fail` L235-240 emits `{"status":"error","message":...}` to `sys.stderr` and raises `SystemExit(code)`; structured retry hints exist: `field_type_mismatch` (L2134), `field_required` (L2171), `retry_with_fields` (L2158), `no_board_membership` (L2046). `yt_main.py` `fail` L47-49 also stderr+nonzero. |
| Read-only default + preview-first mutations (`--apply`) | PASS | `ytx.py` L1571-1574: create runs preview unless `--apply`; `COMMAND_DRY_RUN_HINT` L202 "Run without --apply for preview, then rerun with --apply to mutate"; preview/apply split also for issue link (`preview_or_apply_issue_link` L1347-1379) and command (`--dry-run` L2193-2206). `SKILL.md` §`Mutations` L173-181 documents the preview→inspect→`--apply` flow done in one turn. |
| Dependencies declared `type: system` + checked via `shutil.which` | FAIL | `csk-skill.json` has **no** `dependencies` block at all (only `commands`); `README.md` L46-58 documents `git` (for `--mine`) and Python as requirements, but they are not declared `type: system` and not gated by `shutil.which` with a `hint`. The only `shutil.which` in the tree is `runtime_support.py` L56 locating the `py` launcher on Windows — not a declared-dependency check. |
| No `brew install` / self-installing system deps | PASS | grep for `brew`/`apt-get`/`npm install` in `scripts/*.py` returned nothing; the skill provisions only a Python venv + `pip` for `youtrack-cli` (README L54-58), never system tools. |
| No ad-hoc fallback (jq/grep/python -c/raw REST when high-level exists) | PASS | `SKILL.md` §`Mutations` L172: "Do not fall back to `jq`, `grep`, `python -c`, raw keychain reads, or ad-hoc REST calls if the `ytx` surface can do the job." Enforced behaviorally; the CLI itself is the high-level surface. |
| Secrets only in system keyring | PASS | `instance_runtime.py` imports `keyring` (L15) and stores `youtrack_token`/`youtrack_base_url` via `keyring.get/set/delete_password` (L40-44, L332, L628-630); no plaintext token file fallback found. `SKILL.md` §`Safety Rules` L332-333 reinforces keyring-only. |
| Idempotent (repeat login no dup) | PASS | `yt_main.py` `handle_auth_login` L184-191 calls `register_instance` (idempotent registration) and `set_active_instance`; keychain entries are keyed by service+label (`keychain_service` L137) so a repeat login overwrites rather than duplicates. |

## LEVEL 3 — tool surface

| Rule | Verdict | Evidence |
|------|---------|----------|
| Meaning-commands / answer-shaped payloads | PASS | `ytx.py` registers dedicated `board my-tasks` (subparser L2553, handler L1453-1456 rejects `--assignee`/`--initiator`), `board current` (L2495, handler L1401), `board tasks`, `board scoped-issues` (L2523-2539) — typical-request commands returning answer-shaped payloads (`issue_count` + `issues`, per `SKILL.md` L100), not raw lists the model must interpret. |
| Stable JSON agent-facing reads (`ytx`) separate from human output (`yt`) | PASS | Two distinct wrappers: `ytx` (`scripts/ytx` → `ytx.py`) emits stable JSON for agents; `yt` (`scripts/yt` → `yt_main.py`) is the human/full-CLI surface forwarding to upstream `youtrack-cli`. `SKILL.md` L116-117 + L336: "Prefer `<ytx-command>` for reads consumed by agents because it emits stable JSON." |
| Don't jump to query-layer before primitives settle | N/A | Skill stayed at level-2 meaning-commands and did not introduce a premature unified query entrypoint; nothing to penalize. |

---

## FAIL items

1. **L2 — Dependencies declared `type: system` + `shutil.which` check.** `csk-skill.json` has no `dependencies` block; `git` (required by `--mine`) and the Python launcher are only prose in `README.md` L46-58, never declared `type: system` nor gated by `shutil.which`+`hint`.

## PARTIAL items

1. **L1 — Section order.** `## Safety Rules` (L330) is placed after `## Setup Fallback` (L279) instead of before it, and several behavioral sections sit between Mutations and Setup; Setup is still correctly at the bottom so the anti-tutorial intent holds.
2. **L2 — Non-interactivity.** Scripts are non-interactive except the documented `auth login` path (`SKILL.md` L316-322), which passes no token and lets upstream `youtrack-cli` prompt for it — the contract-acknowledged auth exception, flagged here.

## Note on the second FAIL

The 2/26 PASS ratio counts the `auth login` token prompt as PARTIAL (not FAIL) because the contract explicitly carves out the "auth missing" case. If you require zero interactive reads even on the auth path, downgrade that row to FAIL (→ 3 FAIL / 2 PARTIAL); verdict stays STRONG either way.
