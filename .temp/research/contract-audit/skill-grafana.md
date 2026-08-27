# Contract Audit: skill-grafana

Audited against `/Users/iv/Developer/Wildberries/cocoaskills/.temp/aid/docs/skill-operational-contract.md`.

Skill root: `/Users/iv/agents/skills/skill-grafana`

## Top-line summary

**Verdict: Strong**

- PASS: 25
- PARTIAL: 4
- FAIL: 1
- N/A: 2

The skill is genuinely execute-don't-instruct, uses `<command>` placeholders, has a hard happy path, read-only design, secure keyring-only secrets, and idempotent auth. The main gaps: system dependencies (`python3`) are not declared as `type: system` in csk-skill.json with a `which` hint, and `analyze.py` swallows failed queries into silent fallbacks instead of erroring (partial-success masking). There is no separate stable agent-facing JSON read for the human-facing analyze report.

---

## LEVEL 1 — SKILL.md (agent behavior)

| Rule | Verdict | Evidence |
|------|---------|----------|
| Default Mode: execute, don't instruct | PASS | `SKILL.md` `## Default Mode` first line: "Execute the bundled commands yourself and return the result" + "Do not answer with a shell tutorial when the skill is installed and authentication exists" (lines 32-34). |
| Path resolution: from SKILL.md path, not CWD | PASS | `## Command Resolution`: "Resolve bundled command paths from this skill file path" and explicit ban "Do not run `scripts/grafana-analyze` ... as paths relative to the current working directory" (lines 49, 60). |
| Happy path uses `<command>` placeholders, not `scripts/x` / `$VAR` | PASS | Fast Path blocks use `<grafana-analyze-command>` / `<grafana-query-command>` placeholders (lines 150, 169, 182, 191); literal `scripts/...` appears only in the explicit prohibition and in the Windows setup fallback at bottom. |
| Explicit unix/windows variants | PASS | `## Command Resolution` maps macOS/Linux to `scripts/grafana-analyze` and Windows to `scripts/grafana-analyze.cmd` per command (lines 51-56). |
| Bare command names only via repo `.agents/bin` | PASS | "Bare `grafana-analyze` ... acceptable only when they resolve through that repo-local `.agents/bin` layer" + "This skill does not provision `PATH` itself" (lines 58-59). |
| Hard happy path: Resolve Context First | PASS | `## Resolve Context First` gives a fixed order: auth check, then platform/project table, then rules (lines 86-113). |
| Hard happy path: numbered/fixed Fast Path | PARTIAL | Four `## Fast Path` sections exist with concrete single commands, but steps are bulleted/prose, not literally numbered as the contract's "пронумерованный happy path" prescribes (lines 145-196); ordering is still unambiguous. |
| When to ask | PASS | Asks only on missing context: ask platform only "If the platform is unclear and the request is platform-specific" (line 112) and ask stream "If the user does not name a stream" (line 134); auth-missing routes to setup, not a question. |
| Output contract: mandatory fields enumerated | PASS | `## Output Contract` enumerates required fields per use-case (platform/project/stream/window; ZBP weight/limits/SLA/trend; namespace responsible/band/telegram) (lines 198-206). |
| Output contract: don't hide data by default | PASS | "Return the result itself, not a command recipe" + "Do not return only the report path" (lines 200, 162). |
| Output contract: compact agent-facing payload | PARTIAL | `grafana_query.py` supports `--compact` / `--extract` for compact reads, but `SKILL.md` Fast Paths don't instruct the agent to prefer compact payloads for agent-facing reads; the analyze report is intentionally verbose human markdown. |
| Output contract: deterministic/parsable | PASS | Query reads emit deterministic JSON/CSV (`grafana_query.py render_result`); analyze emits a stable markdown structure (`analyze.py` print order). |
| No repeated identical reads | PASS | "Do not repeat an identical successful read command. Reuse the first successful result..." (line 44). |
| One completeness-check, no loop | PASS | "Before sending the final answer, compare it against the original user request. If the answer is incomplete, keep using tools" — single pre-final check (line 45). |
| Language discipline | PASS | Lines 40-43: final answer entirely in user's language, no English in headings/summaries when user writes Russian, English only for literal values (UIDs, paths, usernames, URLs). |
| Section order per contract (frontmatter, Default Mode, Resolve Context First, Fast Path, Reads, Safety, setup at BOTTOM, References) | PARTIAL | Order is close and correct in spirit: Default Mode -> Command Resolution -> Configuration -> Resolve Context First -> Fast Paths -> Output Contract -> References -> Setup Fallback (bottom). Deviations: `## Configuration` (incl. defaults table with secrets policy) sits above Resolve Context First rather than as a `## Safety Rules` block lower down, and `## Output Contract` lands after Fast Paths rather than being framed as Mutations/Safety. Setup is correctly last. |
| Setup at bottom | PASS | `## Setup Fallback` is the final section (lines 217-245), gated by "Use this only when installation or auth is missing." |

## LEVEL 2 — scripts (command hygiene)

| Rule | Verdict | Evidence |
|------|---------|----------|
| Non-interactive | PARTIAL | Data/read commands never prompt; but `grafana-auth login` prompts via `getpass.getpass` when stdin is a TTY (`secure_auth.read_token_from_user`, lines 190-197). This is the explicitly-allowed auth-login exception, and a `--stdin` flag + auto non-TTY stdin read exist, so it degrades safely in non-interactive runners. Noted as the documented exception. |
| Deterministic output | PASS | `grafana_query.render_result` produces stable JSON/CSV with no timestamps in the main payload; `grafana_auth status --json` emits a stable dict; ordering is input-driven. |
| Error format: stderr + nonzero + actionable | PARTIAL | `grafana_query.py` and `grafana_auth.py` do this well (errors to stderr, `parser.exit(1, ...)` / `SystemExit(1)`, hints include "Run: grafana-auth login ..." — `grafana_auth.py` line 51, `grafana_query.py` line 314). But `analyze.py` `json_request` prints `ERROR:` to stderr then **returns a fallback and continues with exit 0** (lines 47-55), so a failed sub-query yields a partial report with success exit code instead of a nonzero/actionable failure. |
| Structured hint -> one guided retry | PASS | Missing-token raises with embedded actionable command `Run: grafana-auth login --grafana-url <url>` (`grafana_auth.py` lines 35-51); unsupported-backend error names exactly which backends to configure (`secure_auth.py` lines 96-100). |
| Read-only default | PASS | `grafana_query.py` exposes only GET/read endpoints (`auth check`, `dashboard get/targets`, `influx query`, `prom query/range`) — no write/PUT/DELETE; `SKILL.md` line 35 states "This skill is read-only." |
| Preview-first mutations | N/A | Skill performs no mutations; Grafana POSTs are read queries (`/api/ds/query`, `/api/v1/query`). No `--apply` surface needed. |
| Dependencies declared `type: system` in csk-skill.json + checked via `which` + hint, no brew install | FAIL | `csk-skill.json` declares only `commands` and `runtime_roots`; there is **no `dependencies` block** and `python3` (a hard system dependency — every wrapper does `exec python3 .../run_in_skill_venv.py`) is not declared as `type: system` nor probed via `shutil.which` with a hint. `runtime_support.choose_python_interpreter` searches interpreters and raises an actionable error if none ≥3.10 found, which partially compensates, but the contract's declaration mechanism (csk-skill.json `type: system` + `which` + `hint`) is absent. No `brew install` anywhere (good). |
| No brew install / no self-install of system tools | PASS | No `brew`/`apt`/`port` calls in any script; `bootstrap_runtime` only creates a local `.venv` and `pip install`s `keyring` (a Python dep, not a system tool) (`runtime_support.py` lines 134-143). |
| No ad-hoc fallback (jq/grep/python -c/raw REST) | PASS | `SKILL.md` line 38 forbids external HTTP/pipes/one-liners; query work goes through `grafana-query`. (Self-contained REST inside the skill's own Python is the high-level command, not an ad-hoc agent fallback.) |
| Secrets only in system keyring | PASS | `secure_auth.py` enforces an allow-list of secure backends and **blocks** `keyring.backends.null/fail` and `keyrings.alt` plaintext backends (lines 15-30, 85-101); `grafana_auth.load_grafana_token` ignores `GRAFANA_TOKEN`/`GRAFANA_SERVICE_ACCOUNT_TOKEN` env vars and warns (lines 40-47). No plaintext fallback. |
| Secrets not in history/env/checked-in files | PASS | `SKILL.md` lines 72-73 ban shell history / env secrets / plaintext config; token enters only via keyring or stdin (never argv). |
| Idempotent (re-login no dupes) | PASS | `store_token` overwrites the single `(service, auth_token)` keyring entry and re-reads to verify (`secure_auth.py` lines 120-130); repeated login overwrites, no duplicates; read commands are pure. |

## LEVEL 3 — tool surface

| Rule | Verdict | Evidence |
|------|---------|----------|
| Meaning-commands / answer-shaped payloads | PASS | `grafana-analyze` is a meaning-command: one entrypoint for the "team insights" intent that emits a finished narrative report rather than raw frames the model must interpret; ZBP/namespace Fast Paths bundle the exact PromQL so the model picks an intent, not low-level flags. |
| Stable JSON agent-facing reads separate from human output | PARTIAL | `grafana-query` provides stable machine JSON with `--compact`/`--extract`/`--format csv`, and `grafana-auth status --json` exists — good separation for query reads. But `grafana-analyze`, the flagship read, emits only human-facing Russian markdown with no parallel stable-JSON agent payload; the model must parse prose. |

---

## Notes / concrete remediation

1. **csk-skill.json dependencies (FAIL):** add a `dependencies` entry declaring `python3` (and the `py` launcher on Windows) as `type: system` with a `hint`, mirroring `runtime_support.SUPPORTED_MINORS` (3.10–3.13). The runtime already probes interpreters, so this is mostly a declaration gap, but it is the contract's required mechanism.

2. **analyze.py partial-success masking (PARTIAL error-format):** `json_request` returns `fallback` on `URLError`/JSON errors and the report continues at exit 0. A failed auth or datasource yields a hollow report that looks successful. Either propagate a nonzero exit on hard failures (auth/network) or clearly mark degraded sections, so the agent doesn't present a partial report as complete.

3. **analyze agent-facing JSON (PARTIAL L3):** consider an `--emit json` / sidecar stable-JSON mode for `grafana-analyze` so agent-facing consumption doesn't depend on parsing localized markdown.

4. **Fast Path numbering (PARTIAL L1):** convert the bulleted Fast Path steps to explicit numbered steps to match the contract's "пронумерованный happy path" wording and reduce branching for weak models.

5. **Section framing (PARTIAL L1):** the content maps to the contract but `Configuration`/`Output Contract` aren't labeled as `Scope Rules`/`Mutations`/`Safety Rules`; a relabel would tighten conformance. Setup-at-bottom is already correct.
