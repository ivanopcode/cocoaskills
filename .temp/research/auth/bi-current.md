# skill-bi — Current Auth / Secret Storage (as-is)

Repo investigated: `/Users/iv/agents/skills/skill-bi`
Date: 2026-06-04

## TL;DR

The BI skill has **no secret storage whatsoever**. The bearer token is a plain
**required CLI argument** (`--token`) passed straight to `bi_query.py`, which puts it
into an `Authorization: Bearer <token>` HTTP header. There is **no env-var reading**,
**no config file**, **no keychain**, **no prompt**. The `BI_TOKEN` env var mentioned
in `SKILL.md` is documentation-only — **no code reads it** (the doc lies). The token
is therefore exposed on the command line (process table / shell history / agent logs)
on every invocation.

Sibling skills (`skill-sentry`, `skill-grafana`) already implement the target
keychain pattern via a shared `secure_auth.py` (Python `keyring`) + a venv. That is
the migration template.

---

## 1. Where the token comes from today (sources + precedence)

There is exactly **one** source. No precedence chain exists.

| Source | Status | Where |
|--------|--------|-------|
| `--token` CLI flag | **THE ONLY SOURCE**, `required=True` | `scripts/bi_query.py:171` |
| `BI_TOKEN` env var | **Documented but NOT implemented** | `SKILL.md:51` claims it; no `os.environ`/`os.getenv` anywhere in code |
| Config file | none | — |
| Hardcoded | none (only example `eyJ...` in docstring) | `scripts/bi_query.py:7` |
| Interactive prompt | none | no `getpass`/`input()` |
| Keychain | none | no `keyring`, no `security` CLI |

Grep confirmation: the only `token` references in code are the `--token` argparse
arg and the function params that thread it into the header. **Zero** `os.environ`,
`os.getenv`, `getpass`, `input(`, `keyring`, `security`, `.env`, config-file reads
in the entire skill.

`--dbconn-id` defaults to `1097`; `--token` and `--sql` are the only required args.

## 2. Is there any secret storage / plaintext?

- **No storage at all.** Nothing is persisted by the skill.
- The token lives only in the argv of the `bi-query` process for the lifetime of
  that process.
- It is **plaintext on the command line** — visible in `ps`, shell history, and
  (critically for an agent skill) in any command/tool-call log the agent emits.

## 3. What the BI backend actually needs

Two-layer bearer auth against `https://bi.wb.ru` (`BASE_URL`, `bi_query.py:23`).
Built in `_request()` (`scripts/bi_query.py:28-61`):

```python
headers: dict[str, str] = {"Authorization": f"Bearer {token}"}
if queue_key:
    headers["X-Authorization"] = f"Bearer {queue_key}"
```

- `Authorization: Bearer <token>` — the **user-supplied** token, sent on every call.
- `X-Authorization: Bearer <queue_key>` — a **per-query** key returned by the queue
  POST (`result["key"]`, `bi_query.py:75`). This is NOT a stored secret; it is an
  ephemeral handle obtained from step 1 and reused for status/result polling.

No user/password, no cookie, no session login. Pure bearer token. HTTP layer is
stdlib `urllib.request` (no `requests` dependency).

Three-step queue flow (all carry the bearer token):
1. `POST /bi/v2/queue/dbconn/{dbconn_id}` -> `submit_query` (`bi_query.py:64-75`)
2. `GET /bi/queue/dbconn/{dbconn_id}/request/{request_id}/status` -> `poll_status` (`:78-96`)
3. `GET /cache-puller/api/v1/cache/result/request/{request_id}` -> `fetch_result` (`:99-102`)

## 4. All files / functions involved in auth + config loading

Auth (token) handling — **all in one file**, `scripts/bi_query.py`:

| Function | Lines | Role |
|----------|-------|------|
| `_request(url, *, token, queue_key, payload, method)` | 28-61 | Builds `Authorization`/`X-Authorization` headers; does the HTTP call |
| `submit_query(token, dbconn_id, sql)` | 64-75 | Step 1 POST; returns `(request_id, queue_key)` |
| `poll_status(token, queue_key, ...)` | 78-96 | Step 2 polling |
| `fetch_result(token, queue_key, request_id)` | 99-102 | Step 3 result fetch |
| `run(token, dbconn_id, sql, ...)` | 125-166 | Orchestrates the 3 steps |
| `main(argv)` | 169-194 | argparse; `--token` `required=True` (line 171); calls `run()` |

Config loading: **there is none** for credentials. The `setup_*` scripts only deal
with skill installation/localization, not secrets:

- `scripts/setup_main.py` — install CLI entrypoint (`make install`); never touches token.
- `scripts/setup_support.py` — copies skill into a repo's `.agents/skills/`,
  renders localized metadata, writes `.skill-install.json` manifest. No secret logic.
- `scripts/bootstrap_runtime.py` — only checks that `bi_query.py`, `bi-query`,
  `bi-query.cmd` exist. **No venv, no keyring install.**

## 5. Command structure + how creds are resolved

Single command, declared in `csk-skill.json` and `agents/runtime.json`:

- `csk-skill.json`: `schema_version: 2`, `runtime_roots: ["scripts"]`, command
  `bi-query` -> `unix_path: scripts/bi-query`, `win_path: scripts/bi-query.cmd`.
- Wrappers:
  - `scripts/bi-query` (bash) — resolves symlinks, then
    `exec python3 "$SCRIPT_DIR/bi_query.py" "$@"`. **No venv.** (contrast: sentry
    execs `run_in_skill_venv.py`.)
  - `scripts/bi-query.cmd` (Windows) — `python3 "%SCRIPT_DIR%bi_query.py" %*`.
- Credential resolution: **none**. The wrappers pass argv straight through; the
  caller (the agent / user) must supply `--token` literally. There is no
  `bi-catalog` or other entrypoint — only `bi-query`.

## 6. csk-skill.json (schema v2 / runtime_roots) — what's exposed

```json
{
  "schema_version": 2,
  "runtime_roots": ["scripts"],
  "commands": {
    "bi-query": {
      "type": "script",
      "unix_path": "scripts/bi-query",
      "win_path": "scripts/bi-query.cmd"
    }
  }
}
```

Only `bi-query` is exposed. No auth/login command is registered (sentry exposes a
separate `sentry-cli-auth`; grafana exposes `grafana-auth`). To add keychain login
we'd register a second command here, e.g. `bi-auth`.

## 7. Target pattern (sentry/grafana) + gaps to close

### Reference implementation (already in this skills monorepo)

`skill-sentry` / `skill-grafana` share an identical `scripts/secure_auth.py`
(Python `keyring`-based). Key API (`skill-sentry/scripts/secure_auth.py`):

- `store_token(service_prefix, base_url, token)` — writes to OS keyring after
  verifying the backend is a real secure one (`require_secure_backend` allow/block
  lists: macOS Keychain, Windows Credential Locker, Secret Service/KWallet,
  cryptfile, etc.; blocks `keyring.backends.null/fail` and `keyrings.alt`).
- `get_token(service_prefix, base_url)` -> raises `MissingTokenError` if absent.
- `delete_token(...)`, `inspect_status(...)` (with optional write/read `probe`),
  `read_token_from_user(prompt, *, stdin)` (getpass or stdin pipe),
  `external_token_env_present(*names)`.
- Service key format: `"{service_prefix}:{normalized_base_url}"`,
  username `"auth_token"`.

Wiring (`skill-sentry/scripts/sentry_api.py`):
- `SERVICE_PREFIX = "skill-sentry"`.
- On run: env token vars are detected and **explicitly ignored with a warning**
  ("Use secure storage via 'auth login'") — they are NOT used as a fallback.
- `auth_token = get_token(SERVICE_PREFIX, profile.base_url)`; on miss it tells the
  user to run `... auth login`.
- Header built as `"Authorization": f"Bearer {config.auth_token}"`.
- A `handle_auth()` dispatch implements `auth login|status|logout`.
- Runs inside a per-skill venv: wrapper `scripts/sentry-api` execs
  `run_in_skill_venv.py sentry_api.py "$@"`; `scripts/requirements.txt` pins
  `keyring>=25,<26`; bootstrap creates/uses the venv.

macOS `security` CLI is the raw equivalent (`keyring` wraps it):
`security add-generic-password -s "<service>" -a auth_token -w "<token>" -U`
and `security find-generic-password -s "<service>" -a auth_token -w`.
Note: `glab-keychain-auth/SKILL.md` deliberately avoids manual `security` flows in
favor of the tool's built-in keyring — same philosophy: don't hand-roll, use a
keyring abstraction.

### Current gaps & risks (skill-bi specifically)

1. **Token on the command line.** `--token` is required argv -> leaks to `ps`,
   shell history, and agent tool-call logs. This is the headline risk.
2. **No secret storage.** Nothing persisted; token re-supplied every call,
   maximizing leak surface.
3. **`BI_TOKEN` doc is a lie.** `SKILL.md:51` says token can be "stored in
   environment as `BI_TOKEN`" but no code reads it -> broken/misleading contract.
4. **No venv / no `keyring` dependency.** `bootstrap_runtime.py` does not create a
   venv or install anything; `requirements.txt` doesn't exist. Must be added.
5. **No auth subcommand** registered in `csk-skill.json` (no login/status/logout).
6. **README.md is the GitLab template stub** — zero real auth docs; SKILL.md is the
   only real doc and it's inaccurate.

---

## What must change (concrete edits)

Port the sentry/grafana keychain pattern into skill-bi.

1. **Add `scripts/secure_auth.py`** — copy verbatim from
   `skill-sentry/scripts/secure_auth.py` (shared, skill-agnostic; takes
   `service_prefix` + `base_url`).

2. **Add a venv runner + deps** (mirror sentry):
   - `scripts/run_in_skill_venv.py` (copy from skill-sentry).
   - `scripts/requirements.txt` with `keyring>=25,<26`.
   - Update `scripts/bootstrap_runtime.py` to create the venv and `pip install -r
     requirements.txt` (replace the current existence-only check), matching
     sentry's bootstrap; also require the new files (`run_in_skill_venv.py`,
     `requirements.txt`, `secure_auth.py`).

3. **Rewire `scripts/bi_query.py`:**
   - Add `SERVICE_PREFIX = "skill-bi"` and `BASE_URL` as the service base.
   - Import `get_token` (and friends) from `secure_auth`.
   - Make `--token` **optional**; resolution order: explicit `--token` (discouraged,
     keep for break-glass) -> else `get_token(SERVICE_PREFIX, BASE_URL)`.
   - Detect any token env vars and ignore-with-warning (sentry style); do NOT use
     env as a silent fallback. Decide whether to honor `BI_TOKEN` at all — cleanest
     is to drop it and warn.
   - On `MissingTokenError`, print a hint: run `bi-auth login`.

4. **Add an auth command** `scripts/bi_auth.py` with `login` / `status` / `logout`
   subcommands using `store_token` / `inspect_status` / `delete_token` and
   `read_token_from_user` (getpass + `--stdin` pipe). Add wrappers
   `scripts/bi-auth` (bash, execing `run_in_skill_venv.py bi_auth.py "$@"`) and
   `scripts/bi-auth.cmd`.

5. **Update wrappers** `scripts/bi-query` / `scripts/bi-query.cmd` to run through
   `run_in_skill_venv.py bi_query.py "$@"` (so `keyring` is importable).

6. **Update `csk-skill.json`** — register the new `bi-auth` script command
   (unix_path/win_path) alongside `bi-query`. Update `agents/runtime.json` likewise.

7. **Fix docs:**
   - `SKILL.md` Authentication section: remove the false `BI_TOKEN`-env claim;
     document `bi-auth login` keychain flow; mark `--token` as break-glass only.
   - Replace the GitLab-template `README.md` with real install/auth/usage docs and
     the mandated "Tools" section.

8. **Tests:** add a `tests/test_secure_auth.py` (copy from skill-sentry, which
   already has matching coverage) and update `tests/test_bi_query.py` —
   `--token` is no longer required, and the keyring path must be mocked.

### Migration smell-test
Once done, `bi-query --sql "..."` should work with **no** `--token` after a one-time
`bi-auth login`, and the token must never appear in argv/logs again.
