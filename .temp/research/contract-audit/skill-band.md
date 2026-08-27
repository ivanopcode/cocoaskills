# Contract audit — skill-band

Audited skill: `/Users/iv/Developer/Wildberries/cocoaskills/.temp/skill-band`
Contract: `/Users/iv/Developer/Wildberries/cocoaskills/.temp/aid/docs/skill-operational-contract.md`
Reference module: `skill-sentry/scripts/secure_auth.py`

## Top-line summary

**Overall compliance: Strong.**

- PASS: 27
- PARTIAL: 4
- FAIL: 1
- N/A: 1

**Token handling: Strong.** Token lives only in the OS keyring via the proven `secure_auth` module (byte-identical to skill-sentry). No plaintext/env/argv path: `BAND_TOKEN`/`MM_TOKEN` are explicitly ignored with a stderr notice (`band_api.py:71-80`), there is no `--token` flag anywhere, the keyring backend is allow/block-listed (`secure_auth.py:15-30, 85-101`), and login/status/logout all exist (`band_api.py:324-339`). One soft point: `auth login` reads the token from stdin when stdin is not a TTY (`secure_auth.py:191`), which is the intended non-interactive path but means a piped token can transit a shell pipeline — acceptable and documented as `--stdin`.

**Cross-platform: Strong (Adequate on one edge).** Works on macOS, Linux, Windows. The `band` bash wrapper does full symlink resolution (`scripts/band:4-15`); `band.cmd`/`bootstrap_runtime.cmd` do `py`→`python` fallback (`band.cmd:2-7`); `runtime_support.py` branches venv bin vs Scripts via `is_windows()` (`runtime_support.py:24-36`); config dir uses `APPDATA` on Windows and `XDG_CONFIG_HOME`/`~/.config` elsewhere (`config_store.py:27-32`); `command_path()` appends `.cmd` on `os.name == "nt"` (`band_api.py:59-61`); `chmod 0600` is POSIX-guarded (`config_store.py:111`). Edge: the SKILL.md `auth status` example block is `bash`-fenced and the `<skill-dir>/scripts/band.cmd` Windows variant only appears in `## Command Resolution`, so a Windows local model could still copy the bash form — steering-level, not a code defect.

---

## LEVEL 1 — SKILL.md (agent behavior)

| Rule | Verdict | Evidence |
|------|---------|----------|
| Execute, don't instruct (run bundled command, no shell tutorial) | PASS | `SKILL.md:28` "Execute the bundled `band` command yourself and return the result; do not write a shell tutorial." |
| Show commands only when asked or auth missing | PASS | `SKILL.md:29` restricts command display to explicit instruction requests or missing setup/auth. |
| No `Quick Start` / tutorial-style block | PASS | No `Quick Start`; setup lives in `references/auth-config.md` and is referenced, not front-loaded (`SKILL.md:91`). |
| Path resolution: absolute from SKILL.md dir, not relative to CWD | PASS | `SKILL.md:36` "Resolve `<band-command>` to an absolute path from this SKILL.md's directory". |
| `<command>` placeholders, not literal `scripts/x`, not `$VAR` | PARTIAL | Happy-path table uses bare `band ...` (`SKILL.md:69-79`) rather than the `<band-command>` placeholder defined at line 36; bare `band` is contract-allowed only via `.agents/bin` bootstrap, so the table leans on an unstated assumption. |
| Explicit unix vs windows variants | PASS | `SKILL.md:38-39` gives `scripts/band` (macOS/Linux) and `scripts/band.cmd` (Windows). |
| Hard happy path: single fixed `## Resolve Context First` order | PARTIAL | `## Resolve Context First` exists (`SKILL.md:54-57`) but is one paragraph about base-url precedence, not a numbered no-branch startup sequence. |
| Numbered `## Fast Path` for typical request | FAIL | No `## Fast Path` section; the typical request is served by a branchy `## Workflow Selection` decision table (`SKILL.md:68-79`), which is the multi-choice pattern the contract warns against. |
| When-to-ask boundaries | PARTIAL | `## Write Safety` requires confirmation before send (`SKILL.md:60-65`), but there is no explicit "ask only if no context / ambiguous / auth missing, otherwise act" rule per contract §"Границы". |
| Output contract: mandatory fields enumerated | PASS | `## Output Contract` enumerates permalink+post_id for sends, resolved id(s)+name(s) for lookups (`SKILL.md:81-86`). |
| Deterministic, parseable output | PASS | All verbs emit `json.dumps(..., indent=2)` (`band_api.py:496`); reference states "All verbs print JSON to stdout" (`cli-surface.md:3`). |
| Compact agent payload (no full descriptions unless asked) | PASS | Verb outputs are narrow dicts (`cmd_thread` flattens to 5 fields, `cmd_post` to 4) — `band_api.py:202-215, 281-291`. |
| No repeated identical reads | PASS | `SKILL.md:31` "Do not repeat an identical successful read command; reuse the first result." |
| Exactly one completeness-check | PASS | `SKILL.md:32` single pre-final comparison against the original request. |
| Language discipline | PARTIAL | `SKILL.md:30` mandates user-language answers with literal-token exceptions, but omits the contract's explicit "no English in headings/summaries" clause and the completeness-check language sub-question. |
| Section order (Default → Resolve Context → Fast Path → Scope → Reads → Mutations → Safety → setup bottom → References) | PARTIAL | Order is Default Mode → Command Resolution → Authentication → Resolve Context First → Write Safety → Workflow Selection → Output Contract → References. Resolve-Context is below auth, there is no Fast Path/Scope, and Write Safety (mutations/safety) sits above the reads table — diverges from the prescribed order though setup is correctly at the bottom. |

## LEVEL 2 — command hygiene (scripts)

| Rule | Verdict | Evidence |
|------|---------|----------|
| Non-interactive: no stdin/prompt (except one-time auth login) | PASS | Verbs never read stdin unless `--message-stdin` is explicitly passed (`band_api.py:262-278`); only `auth login` prompts, via `getpass` on a TTY (`secure_auth.py:190-197`). Note: `auth login` also reads stdin when not a TTY (the documented `--stdin` non-interactive path). |
| Deterministic output (no random order / timestamp noise) | PASS | Thread/search/posts preserve server `order` (`band_api.py:202-215`); no timestamps injected into output; `ensure_ascii=False` stable. |
| Error format: stderr + nonzero exit + actionable + structured hint | PASS | Errors print to stderr and `SystemExit(1)` (`band_api.py:497-499`); 401/403/404 carry actionable `auth login` hint (`band_api.py:158-164`); `--data` JSON error names the field (`band_api.py:316-318`). |
| Read-only by default; mutations preview-first / gated | PARTIAL | Mutations exist (`post`, `dm`) and send immediately with no `--apply`/preview gate (`band_api.py:281-309`); contract's preview-first pattern is pushed to the agent as a prompt-level confirmation rule (`SKILL.md:60-65`) rather than a CLI gate. Defensible for a messaging API (no natural dry-run), but not preview-first in code. |
| Dependencies declared `type: system`, checked via `shutil.which`, no `brew install` | N/A→PASS | `dependencies.json` is empty (no system deps) and `csk-skill.json` declares only the script command; the bootstrap still does `shutil.which`-based dependency warnings generically (`bootstrap_runtime.py:35-67`) and never runs an installer. Python `keyring` is a venv pip dep, not a system tool. |
| No ad-hoc fallback (jq/grep/python -c/raw REST) in instructions | PASS | SKILL.md routes everything through `band` verbs; raw REST is a first-class `band api` escape hatch, not an ad-hoc shell fallback (`SKILL.md:78`, `cli-surface.md:46-57`). |
| Idempotent | PASS | `auth login` overwrites the same keyring key and verifies (`secure_auth.py:120-130`); `save_profile` upserts atomically via temp+`os.replace` (`config_store.py:86-117`); reads are side-effect-free. (Note: `post`/`dm` are inherently non-idempotent by domain, expected for a send API.) |

## Token handling (in depth)

| Aspect | Verdict | Evidence |
|--------|---------|----------|
| Token source = OS keyring only | PASS | `get_token` reads exclusively from `keyring` under service `skill-band:<base_url>` (`secure_auth.py:133-139`); `load_config` has no other source (`band_api.py:81-91`). |
| No plaintext / env-file / argv fallback | PASS | `BAND_TOKEN`/`MM_TOKEN` are detected and ignored with a stderr notice (`band_api.py:71-80`); no `--token` flag in any parser; config file stores base_url only (`config_store.py:2, auth-config.md:7`). |
| No token on the command line | PASS | No CLI flag accepts a token; `auth login` takes only `--base-url` and `--stdin` (`band_api.py:328-330`). |
| login / status / logout present | PASS | All three subcommands defined (`band_api.py:328-338`, handlers `band_api.py:394-411`). |
| Keyring backend security-checked (allow/block lists) | PASS | `require_secure_backend` enforces `ALLOWED_BACKEND_PREFIXES` and `BLOCKED_BACKEND_PREFIXES` incl. blocking `keyrings.alt.*`/`null`/`fail` (`secure_auth.py:15-30, 85-101`); `auth login` calls it before storing (`store_token` → `secure_auth.py:124`). |
| Same proven module as skill-sentry | PASS | `diff` reports the two `secure_auth.py` files are IDENTICAL byte-for-byte (verified via `diff -q`). |
| Status never leaks token | PASS | `inspect_status` returns only presence boolean; test asserts the secret is absent from the dict repr (`test_secure_auth.py:62-67`). |

Soft note: non-TTY `auth login` reads the token from stdin (`secure_auth.py:191`). This is the intended `--stdin` non-interactive setup path and avoids argv exposure; the only residual is the operator's own pipeline hygiene. Not a contract violation.

## Cross-platform (in depth)

| Aspect | Verdict | Evidence |
|--------|---------|----------|
| bash wrapper symlink resolution | PASS | `scripts/band:4-15` walks `readlink` chain resolving relative and absolute targets before computing `SCRIPT_DIR`. |
| `band.cmd` py/python fallback | PASS | `band.cmd:2-7` uses `where py` then `py -3` else `python`. |
| `bootstrap_runtime.cmd` fallback | PASS | Same `py`→`python` pattern (`bootstrap_runtime.cmd:2-7`). |
| venv path bin vs Scripts + is_windows | PASS | `venv_python_path` returns `Scripts/python.exe` on Windows, `bin/python` elsewhere (`runtime_support.py:24-36`); Windows adds `py -3.x` interpreter candidates (`runtime_support.py:50-55`). |
| os.name / sys.platform usage consistent | PASS | `command_path` keys off `os.name == "nt"` (`band_api.py:59-61`); `config_store` and `chmod` guard off `os.name` (`config_store.py:27, 111`); `runtime_support` uses `sys.platform.startswith("win")` (`runtime_support.py:24-26`). |
| Cache/config dir resolution (XDG vs LOCALAPPDATA) | PASS | `_config_home` uses `APPDATA` (roaming) on Windows, `XDG_CONFIG_HOME`/`~/.config` on POSIX (`config_store.py:27-32`); matches `auth-config.md:24-30`. Uses APPDATA (roaming) rather than LOCALAPPDATA — appropriate for a server-URL profile. |
| No POSIX-only assumption breaking Windows | PASS | `chmod 0600` is wrapped in `if os.name == "posix"` (`config_store.py:111, 141`); `shlex.split` switches posix mode via `is_windows()` (`runtime_support.py:39-40`). |
| No macOS-only assumption | PASS | Keychain is one of several allowed backends; Secret Service/KWallet/Windows/cryptfile all allow-listed (`secure_auth.py:15-25`). |
| SKILL.md steering surfaces Windows variant | PARTIAL | Windows `.cmd` variant is given in `## Command Resolution` (`SKILL.md:39`), but the `auth status` example and workflow table use bash-form `band ...` only (`SKILL.md:47-49, 69-79`), so a Windows weak model gets no inline `.cmd` reminder at point of use. |

## LEVEL 3 — tool surface

| Rule | Verdict | Evidence |
|------|---------|----------|
| Meaning-commands / answer-shaped payloads for typical requests | PASS | Verbs are intent-shaped (`channel`, `user`, `thread`, `dm`, `post`) returning ready-to-report dicts with `permalink`/ids (`band_api.py:218-309`) rather than raw REST blobs. |
| Stable JSON agent reads separate from human output | PARTIAL | All verbs emit stable JSON (good), but there is no human-vs-agent split: `auth status` has `--json` vs pretty (`band_api.py:354-388`), yet read verbs have a single JSON surface with no distinct agent-facing command. Acceptable since the single surface is already machine-parseable. |

---

## Key takeaways

1. The single real FAIL is the **missing numbered `## Fast Path`** — the contract's strongest anti-regression lever for weak models — replaced by a branchy `## Workflow Selection` table.
2. PARTIALs cluster on **SKILL.md steering form** (placeholder usage, section order, when-to-ask, language clause, Windows variant at point of use), not on the command layer.
3. **Token handling and cross-platform are both code-Strong**: identical-to-sentry `secure_auth`, no token leak surface, full Windows/Linux/macOS branching.
4. Mutations are gated by **prompt-level confirmation, not a CLI `--apply`** — reasonable for a send-only messaging API but worth noting against the literal contract wording.
