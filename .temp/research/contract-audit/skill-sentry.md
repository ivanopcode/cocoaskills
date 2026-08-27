# Contract Audit — skill-sentry

Audited skill: `/Users/iv/agents/skills/skill-sentry`
Contract: `/Users/iv/Developer/Wildberries/cocoaskills/.temp/aid/docs/skill-operational-contract.md`

## Top-line summary

**Verdict: Strong.**

- PASS: 24
- PARTIAL: 4
- FAIL: 1
- N/A: 1

The skill is execute-don't-instruct, path resolution is clean, scripts are non-interactive in the agent runtime, secrets live only in keyring, read-only is enforced at the tool layer (GET-only), and deps are declared `type:system`. Weak spots are all minor: no structured-error guided-retry hint, no dedicated agent-facing JSON read command separate from human output, no preview-first machinery (N/A — skill never mutates), and a couple of section-ordering deviations from the canonical contract layout.

---

## LEVEL 1 — SKILL.md (agent behavior)

| Rule | Verdict | Evidence |
|------|---------|----------|
| Default Mode: execute-don't-instruct | PASS | `SKILL.md` "Default Mode > Behavior": "Execute the bundled commands yourself; don't write a shell tutorial." |
| Default Mode: show commands only when asked / setup missing | PARTIAL | "Behavior" covers execute + no-tutorial, but the explicit "show commands only when the user asks for instructions or setup/auth is missing" clause from the contract is not stated verbatim; it's implied by routing setup to `references/auth-config.md`. |
| Path resolution: `<command>` placeholders, not `scripts/x`, not `$VAR` | PASS | Happy path uses `<sentry-api-command>` / `<sentry-cli-auth-command>` placeholders throughout (SKILL.md "Quick Commands", "Resolve Context First"); no bare `scripts/...` or `$VAR` in happy path. |
| Path resolution: computed from SKILL.md dir | PASS | "Command Resolution": "Resolve ... to absolute paths from this SKILL.md's directory: `<skill-dir>/scripts/sentry-api`". |
| Path resolution: explicit unix/windows variants | PASS | "Command Resolution": "macOS / Linux: `<skill-dir>/scripts/sentry-api` ... Windows: same with `.cmd` suffix"; csk-skill.json declares `unix_path`/`win_path`. |
| Bare names only via repo `.agents/bin` bootstrap | PASS | "Command Resolution": "Bare names work only if repo bootstrap exposes them via `.agents/bin/` on `PATH`." |
| Hard happy path: Resolve Context First (single order, no branching) | PASS | "## Resolve Context First" gives one fixed first call `auth status --json` returning server/org/project defaults. |
| Hard happy path: numbered Fast Path for typical request | PARTIAL | "## Quick Commands" provides canonical one-shots per workflow but they are NOT a single numbered Fast Path for the typical request; contract wants a numbered happy path (`## Fast Path: <typical request>`). Routing is table-driven via "Workflow Selection" instead. |
| When to ask: only no-context / unresolvable ambiguity / no auth | PASS | "Resolve Context First": ask only when project is ambiguous; "Authentication": never ask user to paste token; otherwise act. |
| Output contract: mandatory fields enumerated | PASS | "## Final Answer": must include selected project + time window, label every number lifetime vs period, note missing data + which endpoint would fill it. |
| Output contract: don't hide data by default | PASS | "Final Answer": "Return the actual analysis result, not a command recipe"; per-workflow reference Final Answer Contracts enumerate required fields. |
| Output contract: compact agent-facing payload | PARTIAL | SKILL.md "Behavior" keeps tokens raw, and `--compact`/`--extract`/`--format csv` exist in the wrapper, but SKILL.md gives no explicit rule to prefer compact payloads for agent-facing reads vs. detailed user output. |
| Output contract: deterministic / parseable | PASS | Wrapper returns verbatim JSON (sentry_api.py `render_result`); `api.md`: "JSON response body is returned verbatim". |
| No repeated identical reads | PASS | "Behavior": "Don't repeat an identical successful command — reuse the prior result." |
| One completeness-check, no looping | PASS | "Behavior": "Before sending the final answer, compare it against the original request; if incomplete, keep using tools." (single pass, no infinite self-check). |
| Language discipline: final answer in user's language, raw tokens only in English | PASS | "Behavior": "Answer in the user's language. Keep original-language tokens only for command names, flags, query tokens, version strings, URLs, code symbols, and raw output." |
| Language discipline: completeness-check includes language question | PARTIAL | Language rule is present, but the completeness-check ("compare against original request") does not explicitly include the "is all non-literal text in the user's language" sub-check the contract calls for. |
| Section order per contract (setup at BOTTOM) | PASS | Setup/bootstrap is pushed entirely to `references/auth-config.md`; SKILL.md body has no top-level install section — strictly better than "setup at bottom". |
| Section order: frontmatter name/description/triggers | PASS | Frontmatter has `name`, `description`, localizable `triggers` (incl. Russian). |

LEVEL 1 deviations are stylistic: the skill uses a "Workflow Selection" table + "Quick Commands" instead of one canonical numbered `## Fast Path`, and folds Default-Mode sub-clauses tightly. Behavior is correct; the literal section skeleton differs.

## LEVEL 2 — scripts (command hygiene)

| Rule | Verdict | Evidence |
|------|---------|----------|
| Non-interactive: no stdin read / no prompts (auth login may prompt — noted) | PASS | `sentry_api.py` reads/Discover paths never prompt; `secure_auth.read_token_from_user` prompts ONLY for `auth login` and auto-switches to stdin when `not sys.stdin.isatty()` — so it never hangs in a non-interactive runner. Auth login is the documented exception. |
| Non-interactive: params via flags/env/config | PASS | `build_parser` / `build_auth_parser` take everything via argparse flags; defaults resolve from env (`SENTRY_URL/ORG/PROJECT`) and profile (`resolve_runtime`). |
| Deterministic output (no timestamp/random noise) | PASS | `render_result` emits `json.dumps(..., indent=2)` or compact; no timestamps or randomized ordering in main output (grep found no `time()`/`datetime` in output path). |
| Error format: stderr + nonzero + actionable | PASS | `main`/`handle_auth`/`run_sentry_cli` print `SentryApiError` to stderr and `raise SystemExit(1)`; HTTP errors include code+url+body (`perform_get`), MissingToken appends exact `auth login` command (`load_config` → `auth_login_hint`). |
| Error format: structured hint → one guided retry | PARTIAL | Errors are actionable and the auth case returns the exact fix command, but there is no machine-structured hint for field-type/param mismatches (e.g. argparse/HTTP 400) that an agent could do a single guided retry on; failures surface as plain text. |
| Read-only by default | PASS | Wrapper is GET-only: `perform_get` hardcodes `method="GET"`; no POST/PUT/DELETE path exists; SKILL.md "Scope": "Read-only. If asked for a write, refuse." |
| Preview-first for non-destructive mutations | N/A | Skill performs no mutations at all (`api.md`/`auth-config.md`: read-only; no `--apply` surface), so preview-first does not apply. |
| Deps declared `type:system` in csk-skill.json | PASS | csk-skill.json declares `sentry-cli` as `{"type":"system","command":"sentry-cli","hint":"Install sentry-cli and ensure it is available in PATH."}`. |
| Dep checked via `which` with hint, no auto-install | PASS | `external_sentry_cli_path` uses `shutil.which("sentry-cli")` and raises "sentry-cli is not installed or not in PATH." on miss; no `brew install` in any runtime script (grep confirms installs are only in setup tooling). |
| No ad-hoc fallback (jq/grep/python -c/raw REST/keyring) | PASS | All reads go through the `sentry-api` wrapper; `--extract`/`--format csv` replace jq (`api.md`); SKILL.md never instructs raw curl/jq. (Minor: wrapper itself is a thin REST layer by design, which is the intended high-level command, not an ad-hoc fallback.) |
| Secrets only in system keyring | PASS | `secure_auth.require_secure_backend` whitelists OS-native/encrypted backends and BLOCKS `keyring.backends.null/fail` and `keyrings.alt`; `load_config` explicitly ignores `SENTRY_AUTH_TOKEN` env and warns to use secure storage; `auth-config.md`: "Plaintext fallback is not supported." |
| Idempotent (re-login no duplicates) | PASS | `store_token` does keyring `set_password` (overwrite) + read-back verify; re-login overwrites the same service/username, no duplication; `save_profile` overwrites the single profile JSON. |

## LEVEL 3 — tool surface

| Rule | Verdict | Evidence |
|------|---------|----------|
| Meaning-commands / answer-shaped payloads | PARTIAL | The wrapper is a generic REST passthrough (endpoint + params), not per-intent meaning-commands like `board my-tasks`; intent→endpoint mapping lives in `SKILL.md`/references (prompt), not in code. `--extract`/`--field`/`--query` shortcuts shape payloads but the model still picks the endpoint. Contract treats this as the cheapest tier; acceptable but not the strongest. |
| Stable JSON agent-facing reads separate from human output | FAIL | There is no dedicated agent-facing JSON command separate from human-facing output. `auth status` has `--json` vs human text (good), but data reads only emit raw Sentry JSON via the same generic wrapper with no stable, skill-defined answer schema; the contract wants a separate agent-facing emitter for model-consumed reads. |

---

## Notes

- The single FAIL (Level 3 agent-facing JSON command) and the meaning-command PARTIAL are the only architecturally meaningful gaps; both are the "expensive" upper tiers of the contract that it explicitly says not to build prematurely. For a generic REST surface, the verbatim-JSON wrapper is a defensible choice.
- `auth login` interactivity is correctly bounded: it prompts only on a TTY and reads stdin otherwise, so the non-interactive runner won't hang.
- Read-only is enforced in code (GET-only), which is the strongest possible form of the read-only-default rule.
