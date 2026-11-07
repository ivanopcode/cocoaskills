# Set up SSH access to private build repositories

Configure credentials before your first `csk install`. Operators and agents
setting up csk should create the global config, then configure credentials
immediately after installing csk.
SSH remains the default transport. HTTPS is an explicit operator option.

A key loaded into an agent is managed by the person who owns the computer.
csk does not start, fill or manage any agent. An automation agent must use
the credential selection the operator provides.

## First setup

1. Create the global configuration before `config build-ssh add` or `list`:

   ```sh
   csk bootstrap --if-missing --non-interactive --skills-root ~/skills --default-agents codex_cli
   ```

   Choose your skill-repository directory for `--skills-root`. On Windows,
   use `"$HOME\skills"` for that path in PowerShell. `--if-missing` preserves
   an existing config; on a new computer this creates
   `~/.cocoaskills/config.json`. Without it, `add` and `list` exit with
   `Global config not found`. Doctor can inspect SSH state without config;
   `add` and `list` require bootstrap.
2. Obtain the provider's SSH clone URL. Register your public key with an
   account or deploy key that can read the repository. Keep the private key
   on your computer.
3. Verify the server's host-key fingerprint through a trusted provider
   channel. Record the verified key in `~/.ssh/known_hosts`. csk requires
   an existing trusted host key and never accepts a new key interactively.
4. Choose one credential mode below. For a key already loaded in your
   agent, prefer its private identity path when that file exists:

   ```sh
   csk config build-ssh add git.example.com/team \
       --agent auto --identity ~/.ssh/build_key
   csk config build-ssh list
   ```

   For an unencrypted private file without an agent, use this alternative:

   ```sh
   csk config build-ssh add git.example.com/team --identity ~/.ssh/build_key
   csk config build-ssh list
   ```

   `add` prints `Configured build-ssh scope git.example.com/team`.
   The selection lives in your `~/.cocoaskills/config.json`.
5. In an existing Git project, run `csk init --no-interactive` if it has no
   `Skillfile.json`, then declare the skills you need before installing them:

   ```sh
   csk install
   ```

   An empty Skillfile installs no skills; it only checks the empty project.
   For failures, follow
   [SSH troubleshooting](troubleshooting.md#ssh-build-repository-failures).
   Successful ordinary `git clone` alone does not prove this setup: csk
   isolates fetches from your SSH and Git configuration.

To use another verified host-key file, include `--known-hosts` in the same
`add` command. Use an absolute path. The default file is the operator's
`~/.ssh/known_hosts`; csk copies it for the fetch without changing it.

## Scopes and precedence

A scope is a prefix of the canonical identity, such as
`git.example.com/team/tools`. Omit `git@`, the URL scheme and the clone
URL's `.git` suffix. Use a lowercase host; path segments are case-sensitive.
The longest matching scope wins on whole `/` boundaries:

| Configured scope | Repository identity | Result |
| --- | --- | --- |
| `git.example.com` | `git.example.com/team/tools` | Host rule matches. |
| `git.example.com/team` | `git.example.com/team/tools` | Team rule wins over the host rule. |
| `git.example.com/team/tools` | `git.example.com/team/tools` | Repository rule wins over both. |
| `git.example.com/team` | `git.example.com/team-other/tools` | Team rule does not match. |

Each rule supplies its own selection. Choose the narrowest useful scope.
Command flags `--build-ssh-*` override `CSK_BUILD_SSH_*` environment values;
both override configured scopes. Clear stale overrides before diagnosing a
scope. Packages cannot choose your credentials.

To remove a stored selection, run:

```sh
csk config build-ssh remove git.example.com/team
```

This removes the configuration entry. It does not unload the agent key.

## Choose a mode

These commands are alternatives; each `add` replaces the same scope:

```sh
csk config build-ssh add git.example.com/team --agent auto
csk config build-ssh add git.example.com/team --agent auto --identity ~/.ssh/build_key.pub
csk config build-ssh add git.example.com/team --identity ~/.ssh/build_key
```

| Mode | Requirement | Limit |
| --- | --- | --- |
| Agent | `--agent auto` uses `SSH_AUTH_SOCK` on POSIX or the standard service pipe on Windows; `--agent /absolute/agent.sock` selects a POSIX socket. | Loaded keys are offered in turn; too many can exhaust the server's authentication budget. |
| Agent plus pinned `.pub` | Load the matching private key into the selected agent first. Export only its public key to the named file. | The public file pins one identity; it cannot sign by itself. |
| File | Name the private key, without `.pub`. The file must be usable without a passphrase prompt. | No agent is used. Encrypted files need the agent route in the current implementation. |

csk does not import `~/.ssh/config`, `core.sshCommand` or ambient
`GIT_SSH_COMMAND` for these fetches. Put your selected OpenSSH client first
on `PATH` before starting csk. The current SSH options include
`BatchMode=yes` and `NumberOfPasswordPrompts=0`; there is no supported
attended passphrase mode.

When a private file exists, prefer combining `--agent auto` with
`--identity ~/.ssh/build_key`. SSH can use the loaded agent key, or a
usable plain file if that key is absent. An encrypted private file still
requires its matching key loaded while SSH prompts are disabled.
The `.pub` form remains appropriate for keys held only by an agent.

## Terminal credential selection

Pre-configuration is the repeatable route for operators and automation.
If an SSH build repository has no matching selection and `csk install` has
interactive stdin and stdout, csk offers a credential-selection menu. It lists
detected candidates and asks which key/agent to use and whether to save the
scope. Enter chooses the first candidate; another Enter accepts the default
scope. Discovery does not prove that a candidate can sign. Without an
interactive terminal, missing credentials fail with
`build_repository_ssh_credential_missing` and setup commands.

This menu selects credentials; it does not request an SSH passphrase, PIN or
token touch. SSH still uses `BatchMode=yes` and `NumberOfPasswordPrompts=0`.
Loading or unlocking an agent remains the operator's task.

## Local checks

The merged implementation includes a read-only doctor and local preflight.
For older installed versions, check that `csk config build-ssh --help` lists
`doctor`:

```sh
csk config build-ssh doctor git.example.com/team/tools
csk config build-ssh doctor git.example.com/team/tools --all-os
```

Doctor reports the selected identity and agent, SSH path/version, key
metadata, terminal attachment, agent key count/membership when established,
and local host-key presence. Terminal attachment does not enable prompts.
A host-key record in `known_hosts` does not verify the server fingerprint.
`--identity PATH` and `--agent SOCKET` override the inspected selection.
Environment selections override configured scopes. `--all-os` includes
instructions for every OS. Missing or invalid config produces a notice;
doctor continues with explicit options and environment without creating or
repairing config. Closed stdin or stderr does not prevent diagnostics.
Doctor makes no network connection, requests no signatures, prompts for
nothing and changes no agent state. It does not check repository read
permissions or prove that a token can sign.

The preflight inspects local metadata before wrapper creation or network traffic.
A proven encrypted key file with no selected agent, or with an agent confirmed
not to hold the matching key, is refused locally. An unavailable or ambiguous
agent leaves membership `unknown`; it does not prove the key is absent.
The same rule applies on Windows and POSIX. A proven unusable
selection produces `build_repository_ssh_credential_unusable` with a reason
and a doctor command. Inconclusive metadata remains `unknown`; it does not
prove either readiness or failure. An agent listing alone never proves
signing ability. The preflight classifies a refusal as `auth-unavailable`,
so an explicitly declared transport fallback can remain eligible.
It does not create a fallback or switch to HTTPS automatically.

On POSIX, preflight checks file facts with `stat`: an operator-owned regular
identity with group or other permission bits is too open for direct use.
Diagnostic text alone cannot establish this refusal. Correct the file's
permissions under your policy, or select a capable agent holding the matching
key. csk never changes key permissions. An unavailable or ambiguous selected
agent leaves membership unknown; its socket name cannot prove inability
to sign or trigger a refusal.

Fingerprint inspection and empty-passphrase loading are independent probes.
The latter covers security-key containers and legacy PEM files even when
fingerprint discovery fails. A separate scrubbed SSH settings probe checks
security-key provider support without using a token. Proven provider absence
refuses an SK identity even if the agent lists its key. Unfamiliar provider
output remains `unknown`. None of these checks proves hardware signing.

The diagnostics preserve attempt classifications and print
`next:` actions, plus fixed `hint:` text for recognized signing or key-loading
failures, including agent refusal, a missing device, invalid signing format,
an unavailable PIN, an unloaded public-key pin and an inaccessible identity.
Transport actions and hints carry their attempt number, such as
`next: attempt 2:` and `hint: attempt 2:`. Timeout errors retain recognized
hints too. Hints do not change the failure class or fallback rules. See
[preflight refusals](troubleshooting.md#build_repository_ssh_credential_unusable)
and [diagnostic actions](troubleshooting.md#ssh-diagnostic-actions).
An `ssh-auth-rejected` result can mean local key unusability or denied
repository access; check both. For `unclassified`, review
`csk config build-ssh list` and reproduce the selected Git/SSH command
locally with stderr attached. `csk --verbose` shows the safe cause chain,
without adding raw SSH output. Older releases may still show a generic
private-Git failure.

## Agent lifetime and access

Choose a lifetime that fits the task and is not longer than that task.
For an OpenSSH agent, load a named key with `ssh-add -t`. For example,
if the task needs at most fifteen minutes:

```sh
ssh-add -t 15m ~/.ssh/build_key
ssh-add -l
```

`ssh-add` asks for an encrypted key's passphrase outside csk. `-l` lists
loaded fingerprints; it does not test signing. Lifetime control is a
separate security matter for the operator. Custom agents may use their
own expiry and approval controls; verify those controls instead of
assuming they implement OpenSSH's `-t` behavior.

While a key sits in an agent, processes of the same user can ask the agent
to sign, independently of csk. Agent confirmation policies may restrict
requests. csk's scopes and public-key pinning do not isolate the agent
from other processes. To remove just this key when finished, run:

```sh
ssh-add -d ~/.ssh/build_key.pub
```

This targets one identity and leaves other loaded keys alone.
See the [OpenSSH `ssh-add` manual](https://man.openbsd.org/ssh-add).

## macOS: plain key on disk

Use file mode for an existing unencrypted software key:

```sh
csk config build-ssh add git.example.com/team --identity ~/.ssh/build_key
```

Protect the file with owner-only access. Do not remove a passphrase merely
to use this example. For an encrypted key, use the next route.

Verification: an unencrypted software key succeeded with Apple OpenSSH in
a local SSH test. This route has not been verified with a real hardware token.

## macOS: passphrase key in an agent

Use the operator's existing OpenSSH agent. Load the private key yourself
with the task-sized `ssh-add -t` command above, then select its private path:

```sh
csk config build-ssh add git.example.com/team \
    --agent auto --identity ~/.ssh/build_key
```

Start csk from the environment containing that agent's `SSH_AUTH_SOCK`.
When the lifetime expires, the operator must load the key again.
csk does not unlock keys or store passphrases in Keychain.

Verification: a loaded software key succeeded through an agent in local
macOS tests. This route has not been verified with a real hardware token.

## macOS: hardware security key

OpenSSH security keys use `ED25519-SK` or `ECDSA-SK` identities. The file
on disk is a stub; the token performs signing. Loading the stub into an
agent can remove its passphrase step, but token touch may still be required.
An unattended job cannot rely on somebody touching a token.

Use an OpenSSH build with built-in FIDO support and a compatible agent.
The macOS system agent failed security-key signing in software-provider
tests, even after accepting the key. Apple's SSH client also ignored SK
identities without a provider. csk currently has no provider-selection flag;
settings in `~/.ssh/config` do not reach its isolated fetch.

If you choose Homebrew OpenSSH, install it and select its binaries:

```sh
brew install openssh
export PATH="$(brew --prefix openssh)/bin:$PATH"
ssh -V
```

This selects the client; it does not replace an already running agent.
See the [Homebrew formula](https://formulae.brew.sh/formula/openssh).
An operator choosing a new task-local OpenSSH agent can start one explicitly:

```sh
eval "$(ssh-agent -s)"
ssh-add -t 15m ~/.ssh/build_key_sk
csk config build-ssh add git.example.com/team \
    --agent auto --identity ~/.ssh/build_key_sk.pub
```

Replace `15m` with the task's actual lifetime. Preserve the old agent
selection if other work depends on it. The operator owns the new agent
and its cleanup. csk does not create it.

A file-only SK stub without a passphrase signed in software-provider tests.
A PIN-required key can still need interaction under SSH batch options;
agent signing may fail when the agent cannot obtain a PIN. Resident keys
and biometric verification have additional token-specific requirements.
Do not treat these cases as unattended support. See
[hardware and agent failures](troubleshooting.md#ssh-hardware-and-agent-failures).

Verification: no route here has been verified on a real hardware token.
Apple SSH behavior was measured with a software FIDO provider. Homebrew
client and agent behavior, USB access, touch, PIN, resident keys and biometric
verification remain unverified on real hardware.

## Linux

The current implementation refuses external build publication on Linux.
Credential configuration does not lift that platform gate. See
[platform refusal](troubleshooting.md#ssh-build-platform-limit).

For an operator preparing an OpenSSH agent, reuse an existing compatible
socket or explicitly start a task-local agent with `eval "$(ssh-agent -s)"`.
Load the named key with `ssh-add -t`, then save the agent plus pinned `.pub`
selection above. File mode uses an unencrypted private file. Hardware keys
need a client and agent with FIDO support; desktop agents differ.

Verification: Linux software metadata and refusal checks have been verified
with real OpenSSH and generated keys. These include legacy PEM and
software-provider-generated SK containers, closed-stream doctor checks,
agent enumeration and local host-key lookup. They do not qualify external
builds or hardware signing. No Linux route has been verified
on a real hardware token; hardware and custom-agent routes remain unverified.

## Windows

Current main supports private SSH repositories on Windows with native
OpenSSH. Use a csk version containing that support. Use absolute paths in
PowerShell. For an existing unencrypted file, select:

```powershell
csk config build-ssh add git.example.com/team --identity "$HOME\.ssh\build_key"
```

For an operator-managed service agent holding the key, select:

```powershell
csk config build-ssh add git.example.com/team --agent auto --identity "$HOME\.ssh\build_key"
```

On Windows, `--agent auto` selects the standard `\\.\pipe\openssh-ssh-agent`
pipe without requiring `SSH_AUTH_SOCK`; ambient `SSH_AUTH_SOCK` is ignored. Only that standard pipe is admitted;
csk does not start the service or load keys. Microsoft documents operator
setup in its
[OpenSSH key management guide](https://learn.microsoft.com/en-us/windows-server/administration/openssh/openssh_keymanagement).
A `.pub` pin remains valid for a key held only in the agent.

Paths containing spaces are not yet supported by the current wrapper.
Use paths without spaces until the wrapper fix lands. Git Bash, native
OpenSSH and WSL have different environments; ordinary Git success does not
prove that csk selected the same client and credential. See
[Windows agent troubleshooting](troubleshooting.md#ssh-windows-agent-limit).
Doctor uses the same Windows `auto` selection as build admission, including
when an ambient `SSH_AUTH_SOCK` points elsewhere.

Verification: native Windows software-key and standard service-agent paths
have loopback coverage with real Git and SSH. Encrypted file-only keys are
refused locally with `build_repository_ssh_credential_unusable`, before
wrapper creation or network traffic. Hardware tokens, PIN/Hello dialogs and custom-agent
integration remain unverified. No Windows route has been verified on a real
hardware token.

## Custom agents, including 1Password

On macOS or Linux, enable and unlock the agent using its own instructions.
Export the public key to a `.pub` file. Configure csk with the agent's
absolute socket path, including quotes if the path contains spaces:

```sh
csk config build-ssh add git.example.com/team \
    --agent /absolute/path/to/agent.sock --identity ~/.ssh/build_key.pub
```

Alternatively, set `SSH_AUTH_SOCK` to that socket before launching csk and
use `--agent auto`. Configuring `IdentityAgent` only in `~/.ssh/config` is
insufficient. 1Password documents its socket locations and approval settings
in [its SSH setup guide](https://www.1password.dev/ssh/get-started).
Its approval dialog belongs to the operator's agent. Automation requires
an operator-approved policy that can satisfy signing requests.
On Windows, only the standard agent pipe is admitted. A custom agent using
that pipe must be configured by its owner; 1Password integration remains
unverified. The Windows path-with-spaces limitation and Linux build platform
gate still apply.

Verification: csk integration with 1Password and other custom agents was
not run. No custom-agent route has been verified on a real hardware token.

## Explicit HTTPS option

Keep SSH unless you deliberately choose HTTPS. For an existing HTTPS
endpoint, configure the operator's Git credential source:

```sh
csk config build-https add git.example.com/team --token-source git-credentials
```

This selects credentials; it does not rewrite an SSH declaration into HTTPS.
For an SSH-declared repository, choose an explicit per-repository HTTPS
endpoint in operator source policy. There is no automatic host-wide switch.
See [transport policy](skillfile-sources.md) and
[HTTPS credentials](external-build-repositories.md#private-https-build-repositories).
No HTTPS route has been verified with a real hardware token in this research.
