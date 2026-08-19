# Security Policy

For the threat model mapping threats to architecture boundaries, see
[Security model in ARCHITECTURE.md](ARCHITECTURE.md#security-model). English is
the source of truth.

## Supported versions

Security fixes land on the latest release line.

| Version | Supported |
|---|---|
| 0.9.x | yes |
| < 0.9 | no; upgrade to the latest release |

## Reporting a vulnerability

Report vulnerabilities privately through GitHub:
[Security advisories](https://github.com/ivanopcode/cocoaskills/security/advisories/new).
Please keep vulnerability details out of public issues and pull requests until
a fix is released.

Include what you can: the affected version, a reproduction, the impact you
see, and a suggested fix if you have one. The project is maintained on a
best-effort basis; expect an initial response within a week.

## Scope

Reports of particular interest:

- Writing outside designated directories during install (path traversal
  through manifests, archives, or names).
- Command execution through manifest content, git URLs, or skill archives.
- A schema-6 package influencing the manager/worker executable, Go process
  graph, toolchain path, arguments, environment, controls, output, receipt,
  publication, or activation outside its compiler-input role.
- Reuse or activation of a compiled cache entry whose ownership, permission or
  DACL, containment, link safety, receipt, artifact, or currentness cannot be
  independently proven.
- A worker, source snapshot, or fingerprinted toolchain identity changing
  across a source-aware operation without fail-closed teardown.
- Bypasses of the source allowlist, the audit gates, or the trust workflow.
- Prompt-context contamination: repository content reaching the agent window
  past the whitelist.
- Secret exposure in logs, reports, or generated files.

## Compiled command boundary

This section is scoped to the accepted schema-6 boundary in the
[rc.5 protocol core](https://github.com/relux-works/curator-spec/blob/v1.0.0-rc.5/protocol/core.md),
not later protocol revisions.

Schema-6 build source is third-party input. `build_roots` are validated and
hashed in the frozen raw snapshot but excluded from installed prompt context
and script runtime storage. A build command has the closed shape
`{"type":"build","driver":"go-v1","source_dir":"..."}`; no package field
can add a program, argument, environment value, flag, tag, target, toolchain,
output, hook, generator, plugin, or post-build action. An unknown driver fails
closed without fallback.

`go-v1` builds exactly one native `package main` executable with vendor-only
module resolution. The protocol floor is Go 1.23, while this implementation
accepts only the currently operator-trusted Go 1.25 family. Go telemetry uses
private off-state; workspaces, toolchain switching, cross-compilation, cgo,
PGO, package-controlled assembly/host objects, generators, tests, overlays,
plugins, external linking, libgcc fallback, and build-time dependency network
access are rejected.

`manager-worker-v1` is a mandatory logical cache, receipt,
marker-currentness, and claim input, not an operator option. Its process graph
is fixed:

```text
manager parent
  -> identity-verified manager-owned worker
       -> fingerprinted <GOROOT>/bin/go
            -> fingerprinted regular children below <GOROOT>/pkg/tool/
```

The manager verifies the worker before launch and through a fresh nonce-bound
proof. One worker performs one fixed `go list`, waits for full graph
validation and an authenticated permit, then performs one fixed `go build`.
The frozen source, worker, and full toolchain identities are reverified after
execution; the complete worker domain is terminated and joined before return.
An extra message or process request tears down the operation. The manager never
executes the artifact during validation, installation, status, repair,
rollback, or GC.

Manager-owned bounds for one operation are 120 seconds, 8 MiB combined output,
a 128 MiB artifact, 512 MiB per file, 1 GiB private build storage, 2 GiB
memory, and 64 processes. File, memory, and process facilities are applied only
where the inventory below marks them available; these values are not a hard
aggregate descendant guarantee.

The native control inventory is explicit rather than inferred from a platform
name:

| `rc5-native-control-inventory-v1` | macOS | Windows |
|---|---|---|
| `descendant-domain-termination` | process-group/session teardown | Job Object kill-on-close |
| `active-process-count-limit` | unavailable | Job Object limit |
| `aggregate-memory-limit` | unavailable | Job Object process/job memory limits |
| `per-file-size-limit` | `RLIMIT_FSIZE` | unavailable |
| `inherited-handle-restriction` | close-on-exec plus descriptor release | explicit handle inheritance list |

Every source-aware execution operation produces one closed
`capability-evidence-v1` result with one entry per inventory control and the
actual `available/applied` or `unavailable/unavailable` state probed before
worker launch. An unavailable inventory control does not reject a portable
build. A mandatory portable control that cannot be applied rejects with
`build_execution_control_unavailable` before the worker or Go starts and
publishes nothing. Evidence is result-only; it cannot affect or authenticate a
cache key, receipt, marker, claim, or currentness result.

Source-aware compilation is available on macOS and Windows. It fails closed on
other hosts before a worker or Go child starts. Linux source-aware support is
explicitly deferred to `TASK-260728-1skseh` and `TASK-260728-1e6811`; ordinary
script/system skill support on Linux is separate.

The portable policy does not provide or claim these deferred hardened
guarantees:

- `total-network-denial`;
- `read-only-source-and-toolchain`;
- `private-build-root-only-writes`;
- `hard-aggregate-descendant-resource-bounds`;
- `exact-executable-allowlisting`;
- `fail-closed-capability-preflight`.

For example, fixed offline Go configuration is not kernel network isolation,
identity rechecks are not a read-only mount, manager-selected paths are not
descendant write confinement, and manager-owned bounds are not hard aggregate
limits over every descendant.

Logical build inputs, canonical receipt bytes, and artifact-relative
bytes/hash/size form the portability boundary. The physical layout under
`<csk-home>/builds/go-v1/<key>/` (which contains `csk-receipt.ccj.json` and
`bin/<command>` or Windows `bin/<command>.exe`) and the native protection backend do
not. An untrusted boundary is reported as `would-rebuild-untrusted-cache` by
dry-run rather than being adopted.

Installed `content_sha256` is also distinct from raw
`curator-build-source-v1`. A self-consistent receipt and artifact prove only
consistency; protected-state provenance additionally requires the independently
verified manager-created ownership, permission/DACL, containment, file-type,
and link boundary.

Real project/global/hybrid installation publishes verified artifacts and every
activation surface under a journaled all-or-rollback transaction. Status is
read-only. On a supported platform, reinstall repairs missing, corrupt,
wrong-input, legacy/unsupported-identity, or untrusted candidates by rebuilding
into new protected state and never adopts their bytes; an unsupported platform
still fails closed. Locked GC removes only validated, unreferenced entries
older than 24 hours; uncertain state is retained.

## Hardening overview

The threat model treats skill repositories as third-party input. The
boundaries are described in
[ARCHITECTURE.md, Security model](ARCHITECTURE.md#security-model),
and the audit subsystem is specified in
[docs/audit-design.md](docs/audit-design.md) and
[docs/v0.8-design.md](docs/v0.8-design.md).
