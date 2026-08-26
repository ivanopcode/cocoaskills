# External build repositories

CocoaSkills implements the Curator Protocol `1.0.0-rc.10` schema-8
`go-repository-v1` boundary. It builds an executable from a separately locked
Git repository while keeping the skill package unable to select credentials,
Git configuration, hooks, compiler flags, output paths, wrappers, or signing.

The accepted protocol revision is
`b8b03d597ac83d158a0eadd9d0b25d2e883de1a3`; its `conformance/v1/manifest.json`
SHA-256 is
`803918bf8672f76cf990985e51db213b826674cd5bb54fbf47731b8404b44403`.
The external-repository corpus is supplied to tests independently, so the csk
consumer imports no Curator implementation package or internal fixture value.

## Skill declaration

An `agent-skill.json` schema-7 or schema-8 declaration binds a canonical network identity,
an exact Git object ID, and optionally an exact tag:

```json
{
  "schema_version": 7,
  "capabilities": {},
  "build_repositories": {
    "golden-tools": {
      "git": "https://github.com/example/golden-tools.git",
      "locked_commit": {
        "object_format": "sha1",
        "hex": "0123456789abcdef0123456789abcdef01234567"
      },
      "tag": "v1.4.0"
    }
  },
  "commands": {
    "golden-tool": {
      "type": "build",
      "driver": "go-repository-v1",
      "repository": "golden-tools",
      "target": "golden-tool"
    }
  }
}
```

The referenced repository contains a closed `skill-build.json` descriptor:

```json
{
  "schema_version": 1,
  "targets": {
    "golden-tool": {
      "driver": "go-repository-v1",
      "build_root": ".",
      "source_dir": "cmd/golden-tool"
    }
  }
}
```

`build_root` must contain `go.mod`. Only that root is exposed to the compiler.
The output is always the manager-derived `bin/golden-tool` on macOS or
`bin/golden-tool.exe` on Windows. Arbitrary argv, environment, output, hook,
plugin, generator, credential, helper, filter, and signing fields are rejected.

## Admission and audit order

Every install reacquires the exact object or tag through an operator-selected
Git executable. CocoaSkills clears ambient Git configuration and helper state,
uses one exact refspec, proves raw object identities and the reachable graph,
rejects LFS pointers, submodules, links and special modes, materializes and
rehashes the complete snapshot, validates `skill-build.json`, and runs the
independent external audit before any protected artifact lookup or compiler
call.

Non-executable text below `vendor/` in a third-party module (such as a
dependency's README or Makefile) does not block installation: the fixed
build session runs only `go list` and `go build`, hooks and generators are
forbidden, so such text is never executed. CocoaSkills does not report such a
finding: it neither blocks the install nor appears in install output.
Executable files below `vendor/` and any critical findings still block as
before.

## Private HTTPS build repositories

A private HTTPS repository fetch authenticates through the **manager
credential broker**, the slot the manager profile describes as an OPTIONAL
member of the allowed process graph. The manager launches the broker itself:
a private wrapper beside the SSH wrapper, pinned to one host, named by both
`GIT_ASKPASS` and `core.askPass`. A repository can neither select credentials
nor divert them. The fetch goes to a single TLS-verified URL with redirects
disabled, and the broker answers only the two prompts Git asks and only for
the pinned host; any other prompt exits without printing a byte.

The config stores the token source, never a token:

```json
"build_https": {
  "gitlab.example.com/portals/infra": {"token": "git-credentials"},
  "gitlab.example.com/vendor": {"token": "keyring", "username": "oauth2"},
  "ci.example.com": {"token_env": "CI_TOKEN"}
}
```

Scopes use the `build_ssh` grammar: segment prefixes of the canonical
identity, matched on `/` boundaries, longest match wins. Three sources
exist:

| Source | What it reads | Who it suits |
| --- | --- | --- |
| `git-credentials` | the operator's existing HTTPS entry, the one their own credential helper already serves | anyone who has cloned over HTTPS once: no new secret is created |
| `keyring` | the token `csk config build-https login <scope>` stores through that same helper under a namespaced username | operators with neither SSH nor HTTPS history |
| `token_env` | an environment variable read at process entry | CI and headless runs |

The manager reads the credentials, not the broker. The read happens before
the fetch, outside its process graph, through `git credential
fill|approve|reject`. That is the one mechanism which exists identically on
macOS, Windows and Linux, speaks to whichever helper the operator already
configured (`osxkeychain`, `wincred`, `libsecret`, GCM), and needs no runtime
dependency. The operator's Git configuration selects the helper, never a
repository or a manifest. Interactive prompting is disabled
(`GIT_TERMINAL_PROMPT=0`, `GCM_INTERACTIVE=never`), so an absent credential
degrades instead of hanging the install on a dialog.

A token saved through `build-https login` lives under the username
`csk-build-https:<scope>`, separate from the operator's own entry for the
same host, so neither overwrites the other.

Manage the scopes with the subcommands:

```sh
csk config build-https add gitlab.example.com/portals/infra --token git-credentials
csk config build-https login gitlab.example.com/vendor      # hidden PAT input
csk config build-https list
csk config build-https remove gitlab.example.com/vendor     # also drops the keyring entry
```

`CSK_BUILD_HTTPS_TOKEN` (with the optional `CSK_BUILD_HTTPS_USERNAME`)
overrides every scope for one run, exactly as `CSK_BUILD_SSH_*` overrides the
SSH scopes. A token is never accepted as a flag. The unpinned override trusts
the entire closure: HTTPS basic auth transmits the token to whichever host a
manifest names, so every HTTPS build repository host in the closure can
receive it. Set `CSK_BUILD_HTTPS_HOST` to pin the override to one host; a
repository on any other host then resolves as if the override were absent.
Use the unpinned form only when every build repository host in the closure is
trusted.

As on the SSH surface, a precheck before the first fetch lists the detected
candidates on a terminal (the existing Git credentials for the host, or a new
PAT entered on the spot) and saves a choice only after an explicit scope
selection. A missing selection is not an error for HTTPS: anonymous HTTPS
stays a first-class transport, and a public repository fetches exactly as
before.

The fetch environment is deliberately clean (an empty `PATH`, a private
`HOME`), so the manager performs the helper read at its own `PATH` and
`HOME`, with the absolute path of the Git executable it already admitted. The
broker receives only the result and stays a pure answer function, identical
on every platform.

The token never lands in the config, a flag, a log, or a diagnostic: it lives
only in the environment of the fetch children. `token_value` is excluded from
`repr`; spec 11.1 forbids broker values in receipts, markers and diagnostics.

## Private SSH build repositories

An SSH build repository fetch runs in a private empty `HOME` with an empty
`PATH` and no inherited agent socket, so it never adopts the operator's
`~/.ssh/config`, ambient `GIT_SSH_COMMAND`, or a repository-selected wrapper.
Credentials therefore have to be named explicitly. Nothing is inherited
implicitly, and an SSH source with no selection fails closed with
`build_repository_ssh_credential_missing` before any launcher, snapshot, or
cache artifact is written.

| Surface | Flag | Environment variable |
| --- | --- | --- |
| Identity | `--build-ssh-identity PATH` | `CSK_BUILD_SSH_IDENTITY` |
| Agent socket | `--build-ssh-agent [SOCKET]` | `CSK_BUILD_SSH_AGENT` |
| Host keys | `--build-ssh-known-hosts PATH` | `CSK_BUILD_SSH_KNOWN_HOSTS` |

Flags win over the environment. `--build-ssh-agent` with no value, or the value
`auto`, adopts the operator's live `SSH_AUTH_SOCK`. Host keys default to the
operator home's `.ssh/known_hosts` because the fetch pins
`StrictHostKeyChecking=yes`; the file is copied into the private root, so a
fetch cannot rewrite operator state. All three accept symbolic links and are
admitted as their resolved targets.

Three selections are accepted:

```sh
# Unencrypted key on disk.
csk install --build-ssh-identity ~/.ssh/id_ed25519

# Agent holds the key, and the public key pins which agent key is offered.
# Prefer this for passphrase-protected keys.
csk install --build-ssh-agent --build-ssh-identity ~/.ssh/id_ed25519.pub

# Agent only. Every loaded key is offered in turn, so a populated agent can
# exhaust the server's MaxAuthTries budget before reaching the right one.
csk install --build-ssh-agent
```

### Persistent scoped selection

Instead of repeating flags or environment values, the operator may store the
selection in the global config, keyed by a canonical-identity prefix:

```json
"build_ssh": {
  "gitlab.example.com": {"identity": "~/.ssh/personal"},
  "gitlab.example.com/portals/infra": {
    "agent": "auto",
    "identity": "~/.ssh/work.pub"
  }
}
```

A scope is a segment prefix of the schema-7 or schema-8 canonical repository identity
(`host/path`): matching happens only on whole `/` boundaries, and the longest
matching scope wins, so a key granted to one namespace never reaches a
repository outside it. Flags win over `CSK_BUILD_SSH_*`, and both win over
every configured scope.

A scope needs at least one of `agent` or `identity`; each alone is a complete
selection:

- `{"agent": "auto"}`: agent-only. The install adopts the operator's live
  `SSH_AUTH_SOCK` at run time and the agent signs with its loaded keys in
  turn. No key file is named, so a populated agent can exhaust the server's
  `MaxAuthTries` budget before reaching the right key.
- `{"identity": "~/.ssh/key"}`: identity-file only, for an unencrypted key
  on disk (`IdentityAgent=none`).
- both: pinned-agent form. The third canonical authentication-tail form,
  RECOMMENDED, per curator-spec#22. The agent holds the private key and the
  named `.pub` pins which single key is offered.

Manage the map with:

```sh
csk config build-ssh add gitlab.example.com/portals/infra \
    --agent auto --identity ~/.ssh/work.pub
csk config build-ssh list
csk config build-ssh remove gitlab.example.com/portals/infra
```

Before any fetch, the install resolves credentials for every declared SSH
build repository. On an operator terminal an unmatched repository prompts with
a menu of **detected candidates** (the live agent socket with its loaded key
count and the `.pub` files below `~/.ssh`), so the usual answer is a single
Enter on the default "agent + pinned key" entry. Discovery only lists what
exists; nothing is ever used without the operator's explicit selection, and
nothing persists without the explicit scope choice. A non-interactive run
fails closed with `build_repository_ssh_credential_missing` and ready-to-run
`csk config build-ssh add` commands built from the same detected candidates. `csk install --dry-run`
reports which source (flags, environment, or a config scope) covered each
repository.

CocoaSkills writes a private wrapper carrying one pinned `ssh` argv and points
`GIT_SSH_COMMAND` at it. The wrapper refuses to run unless Git hands it exactly
the host and `git-upload-pack` invocation that argv was pinned to, so no
repository value can add an option, change the host, or reach another path. The
operator's own `ssh` on `PATH` is used unchanged and never has to be shadowed.

For a declared tag, the fetched tag must still terminate at `locked_commit`.
A moved tag, missing tag/object, inaccessible source, malformed raw object, or
failed audit stops without publishing a shim or marker. An untagged source may
reuse the exact protected snapshot recorded by an existing marker when the
network is unavailable; a tagged source always requires a fresh tag proof.

## Build, cache, and lifecycle

The fixed Go contract is the same `manager-worker-v1` session documented in the
main README: native toolchain, vendored modules, no network, no workspace, no
cgo, internal linking, and manager-derived output. External builds use a
receipt-v2 cache below `<csk-home>/external-builds`; schema-7 installations use
marker v3 and schema-8 installations use marker v4; both may contain local
receipt-v1 and external receipt-v2 commands together.

Project install publishes `.agents/bin/<command>`; global install publishes
`<csk-home>/global/bin/<command>`. Both managed launchers point directly at the
validated protected artifact, preserve arguments and exit status, and retain
the inherited PATH. Do not copy a compiled artifact into `scripts`, add a
hand-written wrapper, or prepend a private cache directory to PATH. Agents
already resolve project then global managed shims. For optional interactive
bare commands, use the documented `csk shell-init --install` hook.

Run ordinary lifecycle commands:

```text
csk install
csk install --dry-run
csk status
csk install                 # repair/reinstall
csk global install
csk global status
csk gc
```

A global Skillfile that mixes reachable and unreachable repositories does not
have to be installed as a whole: `csk global install --only <name>` (repeatable)
restricts the run to one declaration and its required closure, so an unselected
private repository is never cloned or fetched, and installed skills outside the
selection keep their markers, shims, and adapter entries. Combine it with the
operator SSH options above to install exactly the private build repository the
operator holds credentials for.

To uninstall, remove the skill declaration from the project or global
`Skillfile.json` and run the matching install command; reconciliation removes
the stale marker and shim transactionally.

Dry-run still acquires, proves, validates, audits, and inspects the candidate
cache, but does not compile or mutate. A corrupt receipt, artifact, or snapshot
is never patched or adopted: a mutating install quarantines it and rebuilds
from a newly proved source. Project/global marker and shim publication uses the
existing transaction engine, so a build, collision, crash, or consumer-marker
failure leaves the prior complete installation current or recoverable.

## Toolchain fingerprint deadline

Every build session hashes the complete selected GOROOT to pin the toolchain
identity, and each hashing pass is bounded by a deadline. Reading a cold Go
installation is much slower than reading a warm one, especially on Windows,
where on-access antivirus scans each file the first time it is touched. When
the pass does not finish in time the install fails closed with:

```text
csk install
app: go-v1 toolchain_timeout: toolchain fingerprint deadline exceeded
hashing the Go toolchain did not finish in time; set
CSK_GO_FINGERPRINT_TIMEOUT to a larger number of seconds (default 600, maximum
3600) on hosts where a cold GOROOT reads slowly, for example behind on-access
antivirus
```

The first line is the cross-implementation protocol string and never changes;
the remedy follows it. Operators raise the bound with
`CSK_GO_FINGERPRINT_TIMEOUT`, a number of seconds:

```bash
CSK_GO_FINGERPRINT_TIMEOUT=1800 csk install
```

The default is 600 seconds and the accepted range is 0 exclusive to 3600
seconds inclusive; a larger value is clamped to 3600 and a missing, empty, or
unparseable value falls back to the default rather than failing the install.
The deadline is a liveness bound, not a trust decision: raising it never admits
a toolchain that would otherwise be refused, and it can never be removed
entirely. Callers embedding CocoaSkills set the same bound in code through
`ToolchainConfig(fingerprint_timeout=...)` or the `timeout` argument of
`fingerprint_toolchain`, which take precedence over the environment.

Prefer raising this bound over retrying a failed install. A retry only appears
to help because the first attempt warmed the operating system cache.

## Development substitutions

`Skillfile.dev.json` schema 2 may replace one declared repository for local
development without changing the package declaration:

```json
{
  "schema_version": 2,
  "substitutions": {},
  "build_repository_substitutions": {
    "golden-skill": {
      "golden-tools": {"path": "../golden-tools"}
    }
  }
}
```

A network substitution instead declares `git` plus one typed `revision` or
`tag`. Local selection admits a narrow ordinary `.git` layout and records a
host-path-free operator-local identity. Substitution state is explicit in
receipt v2 and marker v3 and never aliases the declared source. Strict audit
refuses substituted installs. Keep `Skillfile.dev.json` ignored; csk verifies
that boundary before use.

## Platform qualification

`go-repository-v1` is supported and qualified only on native macOS and Windows
hosts. Linux support is deliberately deferred and is not implied by generic
CocoaSkills script/system-command support. Platform evidence must record the
exact OS, architecture, Python, Git, Go, csk, Curator consumer, protocol commit,
and corpus manifest used by the run.
