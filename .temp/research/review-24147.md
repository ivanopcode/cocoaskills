# Adversarial review — PMA-24147 keychain-auth migration (skill-bi)

Worktree: `/Users/iv/Developer/Wildberries/cocoaskills/.temp/PMA-24147/worktree` (branch `feature/oparin/PMA-24147`)
Compared against sibling: `/Users/iv/agents/skills/skill-sentry`

## Verdict

The core migration is solid. Token resolution, keychain service-name matching, wrappers, bootstrap, JSON, and the new tests all work and were verified live. **All 37 unit tests pass.** Round-trip login/resolve/logout through the wrappers works and URL normalization is consistent between `bi-auth login` and `bi-query`. The only real regression is a stale **README.md** that still tells users to use `BI_TOKEN`/`--token`.

---

## Blockers

**B1. README.md still documents the old `BI_TOKEN`/`--token` auth model — directly contradicts the new design.**
`README.md:78` (table row `Bearer-токен | --token | BI_TOKEN`), `README.md:92` (`export BI_TOKEN="<bearer_token>"`), `README.md:95` ("Если токен короткоживущий, пропустите `BI_TOKEN` и передавайте `--token` на сессию").
README has **zero** mention of `bi-auth`, keyring, Keychain, or secure storage (`grep -ci` = 0). This is a shipped false claim: it instructs users to export `BI_TOKEN`, which `bi-query` now explicitly ignores with a warning, and to use `--token`, which is now break-glass-discouraged.
Not in the stated change scope, but it ships in the same skill and the global instructions mandate keeping README consistent with project state.
Fix: rewrite the README Конфигурация/auth section to mirror `SKILL.md` — drop the `--token`/`BI_TOKEN` row, add a `## Аутентификация` section documenting `bi-auth login|status|logout --base-url ...`, secure storage keyed by base URL, `BI_TOKEN` ignored, `--token` break-glass only.

---

## Should-fix

**S1. `references/api.md:156` curl example still uses `-H "Authorization: Bearer $BI_TOKEN"`.**
This is in the manual connector-discovery curl loop, and SKILL.md does legitimately keep `Authorization: Bearer <token>` as the direct-curl header mechanic — so the header itself is fine. The problem is it perpetuates the `$BI_TOKEN` **env var** that the tooling now warns-and-ignores, so a reader is nudged back to `export BI_TOKEN`.
Fix: either keep `$BI_TOKEN` but add a one-line note that this is a manual curl-only variable unrelated to `bi-query` auth, or change to a neutral placeholder like `-H "Authorization: Bearer $TOKEN"` with a comment to read it from `bi-auth`/secure storage. Low effort, removes the contradiction.

**S2. `bi-query` env warning fires even when `--token` is explicitly passed.**
`scripts/bi_query.py` `resolve_token` (≈ lines 263-285): the `external_token_env_present` check + "Ignoring token environment variable(s)…" warning runs **before** the `if explicit_token:` branch. So `BI_TOKEN=x bi-query --token y` prints "Ignoring BI_TOKEN… Use secure storage via bi-auth login" AND then "WARNING: --token is visible…" and uses `y`. The first message is misleading in that path (the env var wasn't the thing being used, and the advice "use bi-auth" is odd when the user explicitly chose break-glass).
Fix: move the `external_token_env_present` check inside the `else`/secure-storage branch, or skip it when `explicit_token` is set. Cosmetic but it's user-facing noise on a security-sensitive command.

---

## Nits

**N1. `bi-catalog.cmd` uses literal `python3` (Windows).**
`scripts/bi-catalog.cmd:4` → `python3 "%SCRIPT_DIR%bi_catalog.py" %*`. On Windows `python3` is usually not a resolvable command (it's `py` or `python`). `bi-query.cmd` and `bi-auth.cmd` were correctly upgraded to the `where py / py -3 … else python` pattern; `bi-catalog.cmd` was intentionally left unchanged (it's plain python3, no venv needed). It still works on macOS/Linux and was out of scope, but the Windows `python3` invocation is a latent bug predating this change. Optional: align it to the same `where py` pattern (without `run_in_skill_venv`, since catalog needs no venv).

**N2. `read_token_from_user` reads stdin silently when not a TTY even without `--stdin`.**
`secure_auth.py:191` `if stdin or not sys.stdin.isatty():`. In an agent/CI context `bi-auth login` without `--stdin` and without a TTY will block on / consume `sys.stdin` instead of erroring. This is copied verbatim from sentry (identical), is arguably intended (pipe-friendly), and is not a regression — flagging only because an agent calling `bi-auth login` non-interactively without piping a token will hang. No fix required unless you want a hard error when neither `--stdin` nor a TTY is present.

**N3. macOS backend allowlist relies on case-folding.**
Real backend module is `keyring.backends.macOS.Keyring` (capital `OS`), allowlist prefix is `keyring.backends.macos.` (lowercase). `backend_is_allowed` lowercases before comparing so it matches — verified live (`backend_secure: true`). The test `test_secure_auth.py:19` sets `__module__ = "keyring.backends.macOS"`. Working as intended, identical to sentry; just noting the implicit case-fold dependency. No action.

**N4. Bootstrap keyring probe diverges from sentry (acceptable).**
`bootstrap_runtime.py` probe runs `python -c "import secure_auth; secure_auth.probe_secure_backend('skill-bi')"` (cwd=`scripts`), whereas sentry runs `sentry_api.py auth status --bootstrap-check`. The BI version is actually cleaner (no CLI roundtrip) and was verified to import and run correctly under the venv. Downside: it does not exercise `bi_auth.py` at all during bootstrap. No action; just be aware bootstrap never smoke-tests the `bi-auth` entrypoint itself.

---

## What I verified (all OK)

- **Token resolution / precedence** (`bi_query.py resolve_token`): explicit `--token` wins (with warning) and skips keyring; otherwise `get_token("skill-bi", base_url)`; `MissingTokenError` → `SystemExit` with `Run: bi-auth login --base-url <url>` hint; `AuthStoreError` → clean `SystemExit`. `BI_TOKEN` no longer used as `--token` default (was `os.environ.get("BI_TOKEN")`, now `None`). No token path reaches argv/logs except the explicit `--token` break-glass, which is warned about.
- **Service-name / base_url normalization match**: live round-trip — `bi-auth login --base-url https://x/` (trailing slash) then `bi_query.resolve_token('https://x')` and `('https://x/')` both return the stored token; `logout` removes it. `service_name` → `normalize_base_url` (`.strip().rstrip('/')`) is the single shared normalizer used by both store and read paths. `resolve_token(args.base_url, …)` vs `run(args.base_url.rstrip('/'), …)` — different strings passed but both normalize identically inside secure_auth, so service names match. Correct.
- **bi_auth.py argparse**: `login`/`status`/`logout` subcommands with `required=True`; `--base-url` on a shared `common` parent (add_help=False); `--stdin` only on `login`; `--bootstrap-check` only on `status`. `resolve_base_url`: flag > `BI_BASE_URL` > error. Handlers correct. Live `bi-auth status` returns valid JSON with `token_present:false` + warning.
- **Wrappers**: `bi-query`/`bi-auth` have correct symlink-resolution loop (matches sentry exactly) and exec `run_in_skill_venv.py <entry>.py`. `.cmd` files use the `where py → py -3 … else python` pattern. `run_in_skill_venv.py` is byte-identical to sentry; rejects path separators in target. `bi-catalog` unchanged (plain `python3 bi_catalog.py`) and still works.
- **bootstrap_runtime.py required_paths**: lists all 13 files (py modules, requirements, all 6 wrapper scripts). Every listed path exists, including `bi-catalog.cmd` and `bi_catalog.py` (verified). Bootstrap runs end-to-end → "skill-bi ready", exit 0. Probe imports `secure_auth` from cwd=`scripts` correctly.
- **runtime_support.py**: `SKILL_NAME = "skill-bi"`, env keys `BI_SKILL_PYTHON`/`SKILL_BI_PYTHON`. Diff vs sentry is exactly those two renames — nothing else. No leftover sentry.
- **secure_auth.py / run_in_skill_venv.py**: byte-identical to sentry (generic, correctly reused).
- **csk-skill.json + agents/runtime.json**: `bi-auth` registered (unix+win paths in csk-skill, command path in runtime.json). Both valid JSON (parsed).
- **Tests**: `python3 -m unittest discover -s tests` → **37 tests, OK**. test_bi_query covers explicit-token-warns, secure-storage resolution (asserts `("skill-bi", url)`), missing-token login hint, BI_TOKEN-ignored-with-warning. test_bi_auth covers base_url flag>env>raise and login/logout handlers. test_secure_auth covers allowed-backend roundtrip, plaintext-backend rejection, and status-excludes-token-value. They test the right behavior.
- **No leftover "sentry"** in any migrated file (scripts, tests, JSON, SKILL.md, references/api.md) — grep clean.
