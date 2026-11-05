# Set up SSH access to private build repositories

Configure credentials before your first `csk install`. Operators and agents
setting up csk should perform this step immediately after installing csk.
SSH remains the default transport. HTTPS is an explicit operator option.

A key loaded into an agent is managed by the person who owns the computer.
csk does not start, fill or manage any agent. An automation agent must use
the credential selection the operator provides.

## First setup

1. Obtain the provider's SSH clone URL. Register your public key with an
   account or deploy key that can read the repository. Keep the private key
   on your computer.
2. Verify the server's host-key fingerprint through a trusted provider
   channel. Record the verified key in `~/.ssh/known_hosts`. csk requires
   an existing trusted host key and never accepts a new key interactively.
3. Choose one credential mode below. For a key already loaded in your
   agent, pin its public key and save a scope:

   ```sh
   csk config build-ssh add git.example.com/team \
       --agent auto --identity ~/.ssh/build_key.pub
   csk config build-ssh list
   ```

   `add` prints `Configured build-ssh scope git.example.com/team`.
   The selection lives in your `~/.cocoaskills/config.json`.
4. Run `csk install` in the project. For failures, follow
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
| Agent | `--agent auto` uses the current `SSH_AUTH_SOCK`; `--agent /absolute/agent.sock` selects a specific socket. | Loaded keys are offered in turn; too many can exhaust the server's authentication budget. |
| Agent plus pinned `.pub` | Load the matching private key into the selected agent first. Export only its public key to the named file. | The public file pins one identity; it cannot sign by itself. |
| File | Name the private key, without `.pub`. The file must be usable without a passphrase prompt. | No agent is used. Encrypted files need the agent route in the current implementation. |

csk does not import `~/.ssh/config`, `core.sshCommand` or ambient
`GIT_SSH_COMMAND` for these fetches. Put your selected OpenSSH client first
on `PATH` before starting csk. The current SSH options include
`BatchMode=yes` and `NumberOfPasswordPrompts=0`; there is no supported
attended passphrase mode.

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
with the task-sized `ssh-add -t` command above, then pin its public key:

```sh
csk config build-ssh add git.example.com/team \
    --agent auto --identity ~/.ssh/build_key.pub
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

Verification: these Linux setup steps were not run in the SSH research.
No Linux route has been verified on a real hardware token.

## Windows

Use absolute Windows paths in PowerShell. A file-mode selection looks like:

```powershell
csk config build-ssh add git.example.com/team --identity "$HOME\.ssh\build_key"
```

This requires an unencrypted private file and an admitted OpenSSH client.
The Windows csk SSH lane has not been verified end to end.

Windows OpenSSH's agent service uses a named pipe. The current csk
implementation requires `SSH_AUTH_SOCK` for `auto` and admits a filesystem
socket, so the native service and 1Password's Windows pipe are not usable
through that selection. Starting the service does not fix this csk limit.
Microsoft documents agent setup in its
[OpenSSH key management guide](https://learn.microsoft.com/en-us/windows-server/administration/openssh/openssh_keymanagement).
Git Bash, native OpenSSH and WSL have different environments; do not infer
compatibility from a successful ordinary Git command. See
[Windows agent refusal](troubleshooting.md#ssh-windows-agent-limit).

Verification: Windows file, passphrase-agent and hardware routes were not
run. None has been verified on a real hardware token.

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
The Windows named-pipe limitation above still applies; the Linux build
platform gate still applies.

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
