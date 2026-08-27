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
