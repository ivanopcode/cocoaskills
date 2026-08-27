# skill-gitlab — Authentication / Secret-Storage Pattern (Reference)

Source repo investigated: `/Users/iv/agents/skills/skill-gitlab`
Primary implementation file: `/Users/iv/agents/skills/skill-gitlab/scripts/gmr_main.py`
Docs: `/Users/iv/agents/skills/skill-gitlab/README.md`, `/Users/iv/agents/skills/skill-gitlab/SKILL.md`

## TL;DR of the pattern

The skill **does not implement secret storage itself**. It is a thin wrapper around the
GitLab CLI `glab`. The PAT (Personal Access Token) is stored by `glab` in the **OS-native
keyring** via `glab auth login --use-keyring`. The skill's only job is:

1. Prompt for / read the token,
2. Pipe it into `glab auth login ... --use-keyring --stdin`,
3. Later verify auth via `glab auth status`,
4. Run all GitLab operations through `glab`, which retrieves the token from the keyring on its own.

There is **no Python keychain code, no `security` CLI calls, no `keyring` library, no
platform branching for storage** inside the skill. All of that is delegated to `glab`.
This is the key design decision to replicate: *delegate secret storage to a CLI that already
does keyring integration; never let the token touch disk, env, or shell history yourself.*

---

## 1. Where the token comes from at runtime — sources & precedence

The skill itself reads the raw token in exactly **one** place: `bootstrap_glab_auth()` in
`scripts/gmr_main.py` (lines ~778–815). Sources, in precedence order:

1. **stdin (non-interactive)** — if `sys.stdin.isatty()` is False, the token is read from stdin:
   `token = sys.stdin.read().strip()`. This is the primary path for agents/automation.
2. **Interactive prompt (getpass)** — if stdin is a TTY, it prompts:
   `token = getpass.getpass(f"GitLab PAT for {hostname}: ")` (no echo).

```python
if sys.stdin.isatty():
    token = getpass.getpass(f"GitLab PAT for {hostname}: ")
else:
    token = sys.stdin.read().strip()
if not token:
    raise CommandError("No token provided on stdin or prompt.")
```

After bootstrap, **the skill never reads the token again.** For all real operations the token
is held by `glab`, which fetches it from the OS keyring. So at *operation* time the effective
source precedence is whatever `glab` uses internally:
- OS keyring entry (written by `--use-keyring`), else
- `~/.config/glab-cli/config.yml` plaintext fallback (see §3), and
- `glab` also honors its own env vars like `GITLAB_TOKEN` / `GITLAB_HOST` if set — but the
  **skill does not set or read `GITLAB_TOKEN` itself.**

Env vars the **skill** reads (these tune the *bootstrap*, not the secret):
- `GITLAB_GIT_PROTOCOL` (default `"ssh"`) → `--git-protocol`
- `GITLAB_API_HOST` (default empty) → `--api-host`
- `GITLAB_API_PROTOCOL` (default empty) → `--api-protocol`
- `GITLAB_HOST` — set by the skill *outbound* to `glab` for CLI subcommands, see
  `glab_cli_env()` (line ~209): `env["GITLAB_HOST"] = hostname`. This selects host, not token.

```python
git_protocol = os.environ.get("GITLAB_GIT_PROTOCOL", "ssh")
api_host     = os.environ.get("GITLAB_API_HOST", "")
api_protocol = os.environ.get("GITLAB_API_PROTOCOL", "")
```

## 2. Exact mechanism of secret storage

- **No keychain integration in the skill.** Grep across all `*.py`/`*.sh`/`*.md` for
  `security`, `find-generic-password`, `add-generic-password`, `keyring` (as a Python import),
  `libsecret`, `secret-service` returns **zero code matches** in the skill scripts. The only
  hits are documentation in README.md and the literal flag string `--use-keyring`.
- Storage is performed entirely by `glab auth login --use-keyring`. The skill builds and runs:

```python
cmd = [
    "glab", "auth", "login",
    "--hostname", hostname,
    "--git-protocol", git_protocol,
    "--use-keyring",
    "--stdin",
]
if api_host:
    cmd += ["--api-host", api_host]
if api_protocol:
    cmd += ["--api-protocol", api_protocol]

run_command(cmd, input_text=token)
run_command(["glab", "auth", "status", "--hostname", hostname], capture_output=False)
```

- **Service/account names**: not chosen by the skill — `glab` owns the keyring entry naming.
  (`glab`'s keyring service is internally keyed on the GitLab host; the skill does not set or
  reference any service/account identifiers.)

## 3. Cross-platform handling

**Inside the skill code there are NO platform branches for secret storage.** There is no
`sys.platform` / `darwin` / `win32` switch in `gmr_main.py`. Cross-platform behavior is 100%
delegated to `glab`'s `--use-keyring` implementation. Per README.md (`### Системное хранилище
для токенов`, lines 56–68):

| Platform | Storage backend (provided by `glab --use-keyring`) |
|----------|-----------------------------------------------------|
| macOS    | **Keychain** |
| Linux    | **Secret Service** (GNOME Keyring, KWallet) |
| Windows  | **Windows Credential Manager** |

**Fallback:** On Linux without a graphical session (CI runners, containers) Secret Service may
be unavailable; in that case `glab` falls back to storing config in the plaintext file
`~/.config/glab-cli/config.yml`. This fallback is `glab`'s behavior, not the skill's.

Notes on where `sys.platform`/`win32` *do* appear (NOT auth-related):
- `tests/test_setup_support.py:285` patches `sys.platform` = `win32` — this is for the
  **installer** (`make` vs `py -3 scripts/setup_main.py`), not for secrets.
- README.md/SKILL.md give per-OS install commands for `glab` itself (`brew`, `winget`, `scoop`)
  and per-OS skill-install commands — again installer concerns, not token storage.

Status of implementation: macOS / Linux / Windows are all **handled, but only by virtue of
delegating to `glab`.** The skill implements none of them directly and stubs none of them —
there is simply nothing to stub because storage is out-of-process.

## 4. Setup / login flow (initial token storage)

Exposed as a subcommand of the `gmr` CLI (`scripts/gmr_main.py`, argparse in `build_parser()`):

- `gmr auth bootstrap <target>` → `command_auth_bootstrap` → `bootstrap_glab_auth(target)`
  — stores the token (prompts or reads stdin, then `glab auth login --use-keyring --stdin`).
- `gmr auth ensure <target>` → `command_auth_ensure` → `ensure_glab_auth(hostname)`
  — verifies, does NOT store.
- `gmr auth ensure-mr <MR-URL-or-IID>` → `command_auth_ensure_mr` — verify, resolving host
  from an MR target.

Exact documented commands (SKILL.md lines 429–440):

```bash
# Bootstrap OS-keyring-backed auth:
<gmr-command> auth bootstrap https://gitlab.example.com/

# Verify auth:
<gmr-command> auth ensure https://gitlab.example.com/
glab auth status --hostname gitlab.example.com
```

Non-interactive bootstrap (agent style): pipe the token on stdin, e.g.
`printf '%s' "$PAT" | gmr auth bootstrap https://gitlab.example.com/`
(token is read via `sys.stdin.read()` when stdin is not a TTY).

The verification helper raises a guiding error when auth is missing
(`ensure_glab_auth`, lines ~762–775):

```python
result = subprocess.run(["glab", "auth", "status", "--hostname", hostname],
                        capture_output=True, text=True)
if result.returncode == 0:
    print(f"glab authentication is configured for {hostname}.", file=sys.stderr)
    return
raise CommandError(
    f"glab authentication is missing for {hostname}.\n"
    f"Bootstrap it with: gmr auth bootstrap {hostname}"
)
```

`require_glab()` (lines ~757–759) guards that `glab` exists in PATH before any auth op:
```python
if shutil.which("glab") is None:
    raise CommandError("glab is not installed or not in PATH.")
```

## 5. File locations & permissions

- The skill writes **no** secret/config files and sets **no** file modes (no `0600`/`0o600`
  anywhere in the code — grep confirms zero matches).
- The only on-disk location relevant to secrets is `glab`'s own:
  `~/.config/glab-cli/config.yml` (used only as the non-keyring fallback). Owned and managed
  by `glab`, not the skill.
- The skill's own config/manifest files (`.skill-install.json`, etc.) are installer metadata
  and contain **no secrets**.

## 6. Implementing functions / files (with key lines)

File: `/Users/iv/agents/skills/skill-gitlab/scripts/gmr_main.py`

| Function | Lines (approx) | Role |
|----------|----------------|------|
| `bootstrap_glab_auth(target)` | 778–815 | The whole login/store flow: read token (getpass or stdin) → `glab auth login --use-keyring --stdin` → `glab auth status`. |
| `ensure_glab_auth(hostname)` | 762–775 | Verify auth via `glab auth status --hostname`; raise with bootstrap hint if missing. |
| `require_glab()` | 757–759 | `shutil.which("glab")` PATH guard. |
| `glab_cli_env(hostname)` | 209–212 | Copies `os.environ` and sets `GITLAB_HOST=<hostname>` for outbound `glab` CLI calls. |
| `glab_json(hostname, endpoint, ...)` | 195–206 | Read ops via `glab api --hostname <host>` (auth handled by glab). |
| `parse_target_host(target)` | 220–242 | Extract hostname + host:port authority from a URL/host string. |
| `command_auth_bootstrap` / `command_auth_ensure` / `command_auth_ensure_mr` | 818–829 | argparse entry points. |

Token-reading critical lines (lines ~795–800):
```python
if sys.stdin.isatty():
    token = getpass.getpass(f"GitLab PAT for {hostname}: ")
else:
    token = sys.stdin.read().strip()
if not token:
    raise CommandError("No token provided on stdin or prompt.")
```

`run_command` (lines 56–75) is a thin `subprocess.run` wrapper; `input_text` carries the token
into `glab` stdin without ever echoing or persisting it.

Declared dependency (`dependencies.json`): `glab` must be on PATH; install message
"install glab and ensure it is available in PATH". Enforced at install time by
`ensure_declared_dependencies()` in `scripts/setup_support.py`.

## 7. Fallback / migration behavior & glab reliance

- **glab is the credential store.** The skill relies on `glab`'s built-in keyring support.
  `glab auth login --use-keyring` writes to the OS keyring; every later `glab api` / `glab mr`
  call reads it back. The skill caches nothing.
- **Env override:** the skill sets `GITLAB_HOST` outbound (host selection only). It does not
  inject `GITLAB_TOKEN`; if a user sets `GITLAB_TOKEN` in their env, that is `glab`'s own
  precedence to resolve, not the skill's.
- **Keyring → plaintext fallback:** handled by `glab` (Linux headless → `~/.config/glab-cli/
  config.yml`). The skill neither forces nor blocks this.
- **Bootstrap tuning via env (not secrets):** `GITLAB_GIT_PROTOCOL`, `GITLAB_API_HOST`,
  `GITLAB_API_PROTOCOL`, plus URL-scheme/port auto-detection from the target
  (`parse_target_host` fills `--api-host` from `host:port`, scheme fills `--api-protocol`).
- **Safety rules (SKILL.md 442–446):** keep tokens in the OS keyring via
  `glab auth login --use-keyring`; never save tokens to shell history, env files, or
  checked-in files; do not run `glab auth status --show-token` unless explicitly asked.

## How to replicate this pattern in another skill

1. Pick an underlying CLI that already does OS-keyring credential storage (here: `glab`;
   GitHub equivalent: `gh`). Declare it as a PATH dependency.
2. Provide an `auth bootstrap <host>` subcommand that:
   - reads the secret from `getpass` when interactive, else from stdin (`.strip()`),
   - rejects empty tokens,
   - pipes it via `subprocess` stdin into `<cli> auth login --use-keyring --stdin`
     (plus host/protocol flags), never to disk/env/argv.
3. Provide an `auth ensure <host>` that runs `<cli> auth status --hostname <host>` and raises a
   helpful "bootstrap it with ..." error on failure.
4. Guard with a `which(<cli>)` PATH check.
5. For all real operations, shell out to the CLI and let it fetch the token from the keyring;
   only set host-selection env (e.g. `GITLAB_HOST`), never the token.
6. Do NOT write your own keychain/`security`/`keyring`/platform-branch code — delegate it all.
