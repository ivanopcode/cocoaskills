# YouTrack Skill — Authentication / Secret-Storage Pattern (Reference)

Researched repo: `/Users/iv/agents/skills/skill-youtrack`

## TL;DR of the architecture

The skill does **not** implement keychain access itself. It is a thin multi-instance
wrapper around the upstream `youtrack-cli` Python package, which stores secrets in the
**OS keyring via the `keyring` PyPI library**, with **Fernet symmetric encryption on top**.
The skill's only job around secrets is to:

1. Namespace the keyring `service` name **per instance** (so multiple YouTrack hosts coexist).
2. Keep **non-secret** config (base URL, username, SSL settings, scoped board ids) in a
   per-instance `.env` file under `~/.config/youtrack-cli/instances/<label>.env`.
3. Strip conflicting `YOUTRACK_*` env vars while a command runs so the keyring is the source of truth.

Two layers matter:
- **Skill layer** (this repo): `scripts/instance_runtime.py`, `scripts/yt_main.py`.
- **Library layer** (vendored in `.venv`): `youtrack_cli/security.py` (`CredentialManager`),
  `youtrack_cli/auth.py` (`AuthManager`), and the `keyring` package backends.

---

## 1. Where the token comes from at runtime (sources + precedence)

There are **two distinct precedences**: instance selection, then credential resolution.

### A. Instance selection (which `.env` + which keyring service to use)
`scripts/instance_runtime.py` → `try_resolve_instance_selection()` (lines 435–471):
1. `--instance <label>` CLI flag.
2. `YOUTRACK_INSTANCE` env var (`INSTANCE_ENV_VAR`, line 24).
3. Pinned "active" instance for this install (`installs/<install_id>.json`).
4. The sole registered instance, if exactly one exists.
5. Otherwise error (asks user to run `auth login`).

### B. Credential resolution (the actual token), per `youtrack_cli/auth.py::AuthManager.load_credentials()` (lines 179–238):
1. **Keyring first** (if `enable_credential_encryption`, default True): reads
   `youtrack_base_url` + `youtrack_token` (+ username, expiry) from the keyring,
   decrypts them. If both base_url and token are present → returns.
2. **Fallback to environment / .env**: `os.getenv("YOUTRACK_BASE_URL")`,
   `YOUTRACK_TOKEN`, `YOUTRACK_USERNAME`, `YOUTRACK_TOKEN_EXPIRY`.
   (`AuthManager.__init__` calls `load_dotenv(self.config_path)`, so the per-instance
   `.env` is loaded into the environment first — but the `.env` normally holds only
   non-secret config; the real token lives in the keyring.)

**Important skill-layer twist:** before the upstream code runs, the skill *deletes* all
`YOUTRACK_*` auth env vars from the process environment (see §7), so the env fallback is
effectively disabled during a wrapped command and the keyring is authoritative.

---

## 2. Exact secret-storage mechanism

### keyring + Fernet, NOT the `security` CLI
`youtrack_cli/security.py::CredentialManager` (lines 200–336):

- Class constants:
  ```python
  KEYRING_SERVICE   = "youtrack-cli"     # overridden per-instance by the skill (see §3 below)
  KEYRING_USERNAME  = "default"
  ENCRYPTION_KEY_NAME = "encryption-key"
  ```
- Each value is **Fernet-encrypted** with a key that is itself stored in the keyring:
  ```python
  def _get_encryption_key(self) -> bytes:
      key_str = keyring.get_password(self.KEYRING_SERVICE, self.ENCRYPTION_KEY_NAME)
      if key_str: return key_str.encode()
      key = Fernet.generate_key()
      keyring.set_password(self.KEYRING_SERVICE, self.ENCRYPTION_KEY_NAME, key.decode())
      return key
  ```
- store/retrieve/delete:
  ```python
  def store_credential(self, key, value):
      encrypted = self.encrypt_credential(value)       # Fernet
      keyring.set_password(self.KEYRING_SERVICE, key, encrypted)
  def retrieve_credential(self, key):
      enc = keyring.get_password(self.KEYRING_SERVICE, key)
      return self.decrypt_credential(enc) if enc else None
  def delete_credential(self, key):
      keyring.delete_password(self.KEYRING_SERVICE, key)
  ```

So the "account/username" passed to `keyring.*_password(service, account)` is **the
credential field name** (e.g. `youtrack_token`, `youtrack_base_url`, `encryption-key`),
NOT a human username. The set of credential keys (mirrored in the skill as `CREDENTIAL_KEYS`,
`instance_runtime.py` lines 38–46):
`youtrack_base_url`, `youtrack_token`, `youtrack_username`, `youtrack_token_expiry`,
`youtrack_verify_ssl`, `youtrack_cert_file`, `youtrack_ca_bundle`, plus `encryption-key`.

### Service/account naming actually used on macOS
- **service** = `youtrack-cli:<label>` (e.g. `youtrack-cli:primary`)
- **account** = one of the credential keys above (`youtrack_token`, etc.)

> On macOS the keyring entry shows up as a generic password whose "Where/Service" is
> `youtrack-cli:<label>` and "Account" is `youtrack_token`. The token value stored there
> is a Fernet ciphertext, not the raw token.

### Does it shell out to `security` (find-generic-password)? NO.
The macOS keyring backend (`keyring/backends/macOS/api.py`) calls the **native Security
framework directly via ctypes**, not the `security` CLI:
```python
_sec  = ctypes.CDLL(find_library('Security'))      # line 25
...
def find_generic_password(kc_name, service, username, not_found_ok=False): ...  # line 141
```
It binds `SecKeychainFindGenericPassword` / `SecKeychainAddGenericPassword` from the C API.
So conceptually it is the macOS Keychain generic-password store (same data as
`security find-generic-password`), but accessed through libSecurity, not by spawning `/usr/bin/security`.

---

## 3. Cross-platform handling

There is **no explicit `sys.platform` / `darwin` / `win32` branching in the skill or in
youtrack-cli**. All OS dispatch is delegated to the `keyring` library, which auto-selects a
backend by `priority()` at import time (`keyring.backends.*`). Backends present in the venv:

| OS | Backend module | Underlying store |
|----|----------------|------------------|
| macOS | `keyring/backends/macOS/__init__.py` + `api.py` | macOS Keychain via native `Security.framework` (ctypes `SecKeychainFindGenericPassword` / `Add...`). **Not** the `security` CLI. |
| Linux (libsecret) | `keyring/backends/libsecret.py` | freedesktop Secret Service via `libsecret` (GObject Introspection). |
| Linux (Secret Service) | `keyring/backends/SecretService.py` | Secret Service D-Bus API (gnome-keyring / KWallet via `secretstorage`). |
| Linux (KDE) | `keyring/backends/kwallet.py` | KWallet. |
| Windows | `keyring/backends/Windows.py` | Windows Credential Manager (Win32 Credential Vault). |
| Fallback / none | `keyring/backends/fail.py`, `null.py`, `chainer.py` | `fail` raises if no usable backend; `chainer` ranks by priority. |

**Implemented vs stubbed:**
- All of the above are **fully implemented by the `keyring` package** (not by this skill).
- The skill itself implements **nothing OS-specific** for secrets — it trusts `keyring`.
- If no keyring backend is usable, `youtrack_cli` falls back to **plain-text file storage**
  (see `AuthManager.save_credentials`, `use_keyring=False` path, auth.py lines 152–174,
  which prints `"Credentials stored in plain text file"`). The skill does not force this path.
- The only platform-aware line in the **skill** is cosmetic — the launcher name in error hints
  (`instance_runtime.py` line 168): `launcher_name = "yt.cmd" if os.name == "nt" else "yt"`.

---

## 4. Setup / login flow (the user-facing commands)

The skill wraps upstream `yt auth login`. **`auth login` requires `--instance <label>`**
(enforced by `resolve_login_instance`, instance_runtime.py lines 488–494).

Documented commands (README.md ~line 247, SKILL.md ~line 320):
```bash
# Basic login (token is prompted, hidden input)
~/agents/skills/skill-youtrack/scripts/yt \
  --instance primary \
  auth login \
  --base-url https://your-youtrack-host

# Login + pin scoped agile boards in one shot
~/agents/skills/skill-youtrack/scripts/yt \
  --instance primary \
  --board-id 83-2561 --board-id agiles/195-1 \
  auth login --base-url https://your-youtrack-host

# Custom CA / self-signed
yt --instance primary auth login --base-url https://host --cert-file /path/cert.pem
yt --instance primary auth login --base-url https://host --ca-bundle /path/ca.pem
yt --instance primary auth login --base-url https://host --no-verify-ssl

# Other lifecycle
yt instances list | current | use <label> | rename <src> <dst>
yt instances scope set <label> 83-2561 195-1 | scope clear <label>
yt --instance primary auth status
yt --instance primary auth logout
```

### What `auth login` does under the hood
Upstream `youtrack_cli/main.py::login()` (lines 1161–1289):
- Options: `--base-url/-u` (`prompt=True`), `--token/-t` (`prompt=True, hide_input=True`),
  `--username/-n`, `--cert-file`, `--ca-bundle`, `--verify-ssl/--no-verify-ssl`.
  → Token is collected via **Click's hidden prompt** (effectively getpass-style) if not passed.
- Calls `auth_manager.verify_credentials(base_url, token, ...)` → `GET /api/users/me`.
- On success calls `auth_manager.save_credentials(...)` which writes the token to the keyring
  (encrypted) and writes non-secret config to the `.env`, plus the marker
  `YOUTRACK_API_KEY=[Stored in keyring]` (auth.py line 149).

### Skill-layer post-login (yt_main.py::handle_auth_login, lines 184–191)
```python
selection = resolve_login_instance(skill_dir, args.instance)
run_upstream(args.forwarded, selection_label=selection.label)  # runs under per-instance keyring service
register_instance(selection.label)                              # add to registry.json
if args.board_ids: set_instance_scoped_board_ids(...)           # persist scoped boards in .env
if not args.no_auto_pin: set_active_instance(...)               # pin as active install-wide
```

### logout (yt_main.py::handle_auth_logout, lines 194–198)
Runs upstream `auth logout` (clears keyring + .env keys), then if the instance is no longer
ready, calls `delete_instance_artifacts()` to also remove the `.env`, raw keyring entries
(including `encryption-key`), unregister it, and clear active-instance references.

---

## 5. File locations & permissions

- **Config root:** `${XDG_CONFIG_HOME:-~/.config}/youtrack-cli/`
  (`instance_runtime.py::config_home/config_root`, lines 73–81; `SERVICE_PREFIX="youtrack-cli"`).
- **Per-instance non-secret config (.env):** `~/.config/youtrack-cli/instances/<label>.env`
  (`config_path_for_label`, line 133). Holds `YOUTRACK_BASE_URL`, `YOUTRACK_USERNAME`,
  `YOUTRACK_VERIFY_SSL`, cert paths, `YOUTRACK_SCOPED_BOARD_IDS`, and the keyring marker
  `YOUTRACK_API_KEY=[Stored in keyring]`. **Never the raw token.**
- **Instance registry:** `~/.config/youtrack-cli/registry.json` (`{"instances": [...]}`).
- **Per-install state (active pin):** `~/.config/youtrack-cli/installs/<install_id>.json`,
  where `install_id = sha256(install_root)[:16]` (`detect_install_context`, lines 141–164).
- **Upstream default config (single-instance, unused by skill):**
  `~/.config/youtrack-cli/.env` (`AuthManager._get_default_config_path`, auth.py lines 83–87).
- **Audit log:** `${XDG_DATA_HOME:-~/.local/share}/youtrack-cli/audit.log`
  (`youtrack_cli/security.py::AuditLogger`, masks tokens via regex).

**File permissions:** NO explicit `chmod 0600` / `os.chmod` anywhere. The `.env` files are
written with default umask via `Path.write_text` (`_save_env`, lines 199–202). Values are
`shlex.quote`-escaped. **The token is never written to these files** — it only lives in the
OS keyring — so the 0600 concern is sidestepped rather than handled. (This is a gap to
consider improving if you replicate the pattern for a plain-file fallback.)

---

## 6. Key files & functions (skill layer)

`scripts/instance_runtime.py`:
- `keychain_service(label)` (line 137): `return f"{SERVICE_PREFIX}:{validate_label(label)}"` → the per-instance service name.
- `activated_keyring_service(label)` (contextmanager, lines 497–513): **the heart of the pattern.**
  ```python
  saved_service = CredentialManager.KEYRING_SERVICE
  saved_env = {k: os.environ.get(k) for k in AUTH_ENV_KEYS}
  CredentialManager.KEYRING_SERVICE = keychain_service(label)   # monkeypatch class attr
  for k in AUTH_ENV_KEYS: os.environ.pop(k, None)               # strip conflicting env
  try: yield
  finally: restore service + env
  ```
  It temporarily overrides the upstream `CredentialManager.KEYRING_SERVICE` class attribute
  to the per-instance value, and removes all `YOUTRACK_*` auth env vars so the keyring wins.
- `activated_auth_manager(...)` (lines 516–532): wraps the above, builds
  `AuthManager(config_path=<instance .env>)`, optionally enforces `load_credentials()` is ready.
- `instance_is_ready(label)` (lines 339–347): readiness = keyring has both
  `youtrack_base_url` and `youtrack_token` for the per-instance service.
- `delete_instance_artifacts` / `_delete_raw_keychain_entry` (lines 628–656): teardown,
  deletes every `CREDENTIAL_KEYS` entry + `ENCRYPTION_KEY_NAME` via `keyring.delete_password`.
- `rename_instance` (lines 659–689): decrypts creds from old service, re-encrypts/stores under
  new service, deletes old, renames `.env`.

`scripts/yt_main.py`:
- `run_upstream(args, selection_label)` (lines 130–148): injects
  `--config <instance .env>` and runs upstream `main` **inside** `activated_keyring_service`.
- `handle_auth_login`, `handle_auth_logout` (see §4).

Library layer (vendored `.venv/lib/python3.13/site-packages/`):
- `youtrack_cli/security.py::CredentialManager` — keyring + Fernet (see §2).
- `youtrack_cli/auth.py::AuthManager.save_credentials / load_credentials / clear_credentials`.
- `keyring/backends/*` — OS backend selection.

---

## 7. Fallback / migration behavior

- **Env vs keyring:** Upstream `load_credentials` tries keyring **first**, then env/.env.
  BUT the skill inverts the usual "env overrides" expectation: `activated_keyring_service`
  **removes** all `YOUTRACK_*` auth env vars (`AUTH_ENV_KEYS`, lines 28–37) during the
  wrapped run, so within the skill the **keyring is authoritative** and ambient env tokens
  cannot leak across instances. The env vars are restored afterward.
- **Keyring → plain file fallback:** If `enable_credential_encryption` is False or no keyring
  backend is usable, `AuthManager.save_credentials` writes the token in **plain text** to the
  `.env` and prints a warning (auth.py lines 152–174). The skill does not deliberately use
  this path, but it exists.
- **Encryption-key bootstrap:** `_get_encryption_key` lazily generates a Fernet key on first
  store and persists it in the keyring under `encryption-key`. If the key is missing,
  decryption silently returns the ciphertext (logs an error) — so deleting `encryption-key`
  effectively bricks the stored token (handled by full teardown in `delete_instance_artifacts`).
- **Rename migration:** `rename_instance` decrypts under the old service and re-stores under
  the new one, then deletes old entries — a clean keyring "move".
- **Registry self-heal:** `load_registry` auto-discovers instances by scanning
  `instances/*.env` filenames and merges them into `registry.json`.

---

## Replication checklist (to adopt this pattern elsewhere)

1. Depend on `keyring` (OS backend) + `cryptography` (Fernet). Let `keyring` pick the backend;
   do **not** branch on `sys.platform` or shell out to `security`.
2. Define a service-name scheme: `"<tool>:<instance-label>"`. Store each credential field as a
   separate keyring entry: `keyring.set_password(service, field_name, fernet_encrypt(value))`.
3. Store the Fernet key itself in the keyring under a fixed account name (`encryption-key`).
4. Keep only **non-secret** config in `~/.config/<tool>/instances/<label>.env`; put the
   keyring marker (`API_KEY=[Stored in keyring]`) there for visibility.
5. Provide a contextmanager that (a) overrides the credential manager's service name to the
   per-instance value and (b) strips conflicting env vars for the duration of the command.
6. `login` subcommand: prompt for token with hidden input, verify against the API, then store
   to keyring. `logout`: delete all keyring fields + the encryption key + the `.env`.
7. If you keep a plain-file fallback, add `os.chmod(path, 0o600)` — youtrack-cli does **not**,
   which is the one weak spot of this reference.
