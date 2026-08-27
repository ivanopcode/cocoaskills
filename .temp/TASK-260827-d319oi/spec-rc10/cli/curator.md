# Curator CLI guide

This document is informative. It describes the Go reference implementation,
not a conformance requirement. Other managers may use different command names,
flags, output, machine-home paths, and distribution channels.

Curator uses `curator`, defaults its machine home to `~/.curator`, reads user
configuration from `~/.curator/config.json`, and supports overrides through
`CURATOR_CONFIG` and `CURATOR_SYSTEM_CONFIG`. Those names are not protocol wire
identifiers.

## Commands

| Command | Behavior |
|---|---|
| `curator bootstrap [--if-missing]` | Create machine configuration interactively or from flags; keep an existing file unchanged with `--if-missing` |
| `curator init [path]` | Create `Skillfile.json` and ignore generated paths |
| `curator add <name> --tag\|--branch\|--revision <ref>` | Add or replace a direct declaration and install |
| `curator remove <name>` | Remove a declaration |
| `curator install [target] [--all] [--dry-run] [--strict-tags] [--audit [advisory\|strict]]` | Apply the manager lifecycle |
| `curator update` | Fetch configured source repositories |
| `curator upgrade [target] [--all] [--dry-run]` | Fetch only the selected dependency closure, then install |
| `curator status [target] [--all] [--check] [--json] [--attest]` | Report drift and optionally refresh attestations |
| `curator list` | List configured projects and declarations |
| `curator project add\|resolve` | Register projects and resolve ownership of a path |
| `curator config show` | Print effective configuration |
| `curator skill check <dir> [--locale <code>] [--json]` | Validate one package |
| `curator global init\|add\|remove\|list\|status\|install\|update\|upgrade` | Manage global scope |
| `curator hybrid add\|remove\|list\|status` | Manage hybrid scope |
| `curator audit [target] [--all] [--global] [--json]` | Run source audit |
| `curator audit --allow <hash> --reason <text>` | Create an operator pin |
| `curator audit --publish <record> --registry <url>` | Publish an auditor-signed record |
| `curator repair [target] [--all] [--audit [advisory\|strict]]` | rc.5 command contract: reacquire, audit, and restore non-current managed state |
| `curator gc` | Collect unreferenced machine state |
| `curator shell-init [auto\|zsh\|bash\|powershell] [--install] [--no-global]` | Print or cache optional shell integration |

Exit code 0 is success, including a syntax-only check that emitted only
warnings. Exit code 1 is an operation failure, security-policy block, partial
multi-target failure, or `status --check` result containing any non-current or
unknown item. Exit code 2 is invalid command syntax or flag use. Scripts should
use `--json` where available and inspect each result's stable `code` rather
than parse human text.

`bootstrap --if-missing` is intended for repository bootstrap commands. It
returns success without parsing or rewriting an existing configuration and is
mutually exclusive with `--force`.

`upgrade` differs from `update`: upgrade fetches only direct and transitive
sources required by the selected project or global manifest, while update
fetches every repository below `skills_root`. `upgrade --all` deduplicates
repositories shared by project closures. Any install or upgrade `--dry-run`
uses temporary planning state and leaves source checkouts, caches,
configuration, runtime state, and project artifacts unchanged.

## Developer shell

Shell profile setup is optional. Agent instructions can invoke project
commands directly through `.agents/bin/<command>` on Unix or
`.agents\bin\<command>.cmd` on Windows. Global installation publishes
forwarding shims to a safe existing user-bin directory when possible.
Set `CURATOR_GLOBAL_USER_BIN` to a writable directory already on `PATH` when
automatic selection is unavailable.

For interactive bare command names, cache the hook once:

```bash
curator shell-init --install
# Add the source command printed above to .zshrc or .bashrc.
```

On Windows, automatic detection selects PowerShell unless `SHELL` identifies
Git Bash. Add the printed dot-source command to the PowerShell profile only if
interactive activation is wanted:

```powershell
curator shell-init --install
```

The cached hook is sourced without starting Curator on each shell launch.
`CURATOR_AUTO_ENV=0` disables project-directory scanning while retaining
global activation. Curator never edits a shell profile automatically.

## CI example

Pin a released Curator version using the platform package or verified release
artifact, then run the same protocol gates as local development:

```yaml
steps:
  - uses: actions/checkout@v4
  - name: Install Curator v0.1.1
    run: go install github.com/relux-works/curator/cmd/curator@v0.1.1
  - name: Materialize skills
    run: curator install . --strict-tags
  - name: Verify installed state
    run: curator status . --check
```

Organizations normally install enforced source, audit, and registry policy
before this job. CI MUST NOT disable a policy required on developer machines.

## External repository lifecycle

Schema-7 external build repositories use
[manager-profile section 11](../profiles/manager.md#11-external-repository-manager-profile).
This is the rc.5 reference CLI contract; a Curator binary that
claims only rc.4 does not yet expose `repair` or accept schema-7 objects. The
command names below do not change lifecycle ordering:

```bash
# Syntax and schema validation may run without source coverage.
curator skill check ./skill --json

# A dry run may fetch into removable private state and audit exact source, but
# creates no persistent repository, snapshot, receipt, cache, marker, or shim.
curator install . --dry-run --audit strict

# A real install must resolve and independently audit every external source
# before artifact-cache lookup or compilation.
curator install . --audit strict

# Status is read-only and never contacts a remote just to re-test a tag.
curator status . --check --json

# Repair repeats exact acquisition, audit, and transaction publication.
curator repair . --audit strict

# GC retains every marker/journal root and uncertain protected entry.
curator gc
```

For an unsubstituted declaration with `tag`, install and repair fetch only
`refs/tags/<tag>` into the fixed private destination and prove its terminal
commit equals the immutable lock. They never try a direct object ID, branch,
all-tags fetch, or alternate ref. An untagged declaration fetches only its full
locked object ID.

`skill check` may report state `unverified-offline`, severity `warning`, and
code `build_repository_unverified_offline` when it is intentionally
syntax-only and cannot obtain the exact snapshot. It must not report source,
audit, cache, receipt, artifact, marker, or installation coverage.

`install`, `upgrade`, `update`, `repair`, and coverage-claiming `audit` are
never syntax-only, including when `install` or `upgrade` uses `--dry-run`.
Without exact source they report state `blocked`, severity `error`, and code
`build_repository_source_unavailable`, return exit code 1, and make no
mutation. A protected snapshot does not turn that failure into offline warning
success or replace an exact-tag assertion.

Install-family dry-run prints one state per active build command:

```text
golden-tool: cache-hit
golden-tool: would-preflight-and-build
golden-tool: would-rebuild-untrusted-cache
golden-tool: corrupt
golden-tool: unsupported
golden-tool: blocked
```

Only one state is emitted for a command. `cache-hit` means the exact external
snapshot was raw-object proved, hashed, independently audited, and matched to a
valid protected receipt and artifact. `unverified-offline` is not an install,
upgrade, update, repair, or coverage-audit dry-run state.

Human wording may add sanitized detail after the manager profile's stable
tuple. JSON output uses the same states and stable codes from manager-profile
section 11.10. Each ordered result contains `command`, `repository`, `phase`,
`state`, `severity`, and `code`; `severity` and `code` are `null` only for a
successful result. The top-level `status` is exactly `success`, `warning`,
`failed`, or `partial`:

```json
{
  "status": "failed",
  "results": [
    {
      "command": "golden-tool",
      "repository": "golden-tools",
      "phase": "source",
      "state": "blocked",
      "severity": "error",
      "code": "build_repository_ref_moved"
    }
  ]
}
```

Results use planned command order. Human and JSON diagnostics never expose
credentials, private keys, broker output, SSH agent paths, or unsanitized
package/remote output.

Mixed schema-7 installations report local `go-v1` receipt-v1 commands and
external `go-repository-v1` receipt-v2 commands independently. Both run under
the portable `manager-worker-v1` execution policy. Install, dry-run plan, and
status results MAY report the operation's `capability-evidence-v1` record: one
`{name, availability, status, probed_at}` entry for each control of the
exhaustive `rc5-native-control-inventory-v1` inventory. Such capability evidence
is reporting only. It never claims a deferred hardened guarantee, never affects
a cache key, receipt, marker, or claim, and an `unavailable` entry is never a
failure. Execution-boundary failures use the `execution`
phase and the `build_execution_*` codes of the manager profile. A failure in any
planned command prevents publication for the whole operation. Successful
publication is one manager-home transaction with the consumer ledger last;
rollback restores committed targets in reverse order.

For rc.8, omitted assurance selection means `portable`. An implementation that
offers `verified` exposes it only as explicit operator configuration or a CLI
option; package data cannot select it. Before execution it resolves a separately
installed `host-execution-provider-v1`, verifies its configured binary identity,
and validates a fresh complete capability receipt. Any failure is terminal for
that operation and MUST NOT trigger a portable retry. The CLI should display the
selected mode, provider id and binary digest, or the fail-closed diagnostic.
Provider installation is not a skill-install command.
