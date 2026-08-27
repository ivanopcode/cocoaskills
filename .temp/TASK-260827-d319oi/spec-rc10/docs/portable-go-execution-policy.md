# Portable `manager-worker-v1` execution policy

This guide is informative. The normative rules are
[`protocol/core.md`](../protocol/core.md) section 4.2.1,
[`profiles/manager.md`](../profiles/manager.md) section 2.2.1,
[`SECURITY.md`](../SECURITY.md), and
[`decisions/0006-portable-manager-worker-execution.md`](../decisions/0006-portable-manager-worker-execution.md).

## What it is

Both compiled-build drivers, local `go-v1` and external `go-repository-v1`, run
under one named execution policy. Protocol 1.0 defines exactly one value:

```json
{"execution_policy": "manager-worker-v1"}
```

The value appears in the build-receipt policy object, in every marker-v3 build
record, and in every claim-v3 driver assertion. It is not a package-visible
option, a host label, or an operator preference.

The process graph is fixed at four nodes:

```text
manager parent
  -> identity-verified manager-owned worker
       -> fingerprinted <GOROOT>/bin/go
            -> fingerprinted regular executables below
               <GOROOT>/pkg/tool/<GOHOSTOS>_<GOHOSTARCH>/
```

The worker is one hidden-mode re-execution of the installed manager executable.
It exists so that every control is applied before a compiler sees a package
byte. One session runs exactly one `go list`, waits for the parent to validate
the complete package graph, accepts exactly one authenticated build permit, and
runs exactly one `go build`.

## For skill authors

Nothing changes. The build command surface is still exactly:

```json
{"type": "build", "driver": "go-v1", "source_dir": "build/cmd/tool"}
```

or, for schema 7:

```json
{"type": "build", "driver": "go-repository-v1", "repository": "tools", "target": "tool"}
```

There is no field for an executable, argument, environment value, flag, output
path, hook, plugin, or generator, and there is no out-of-band way to supply one.
An attempt to influence the execution boundary fails with
`build_execution_package_influence_forbidden` before the worker and before the
compiler start.

## For manager implementers

These controls are mandatory on every supported host. If any of them cannot be
applied, reject the build with `build_execution_control_unavailable` before
starting the worker or Go:

- the fixed offline vendored-Go behavior, argument vectors, environment, and
  canonical working directories;
- the fixed manager-selected process graph: start nothing but the four nodes
  above;
- operation-private user, configuration, cache, temporary, staging, and output
  roots resolved independently of package data;
- a frozen source snapshot that neither the manager nor the worker writes to;
- a manager-derived artifact path, a bounded wall-clock deadline over the whole
  worker domain, bounded and redacted combined output, and one bounded regular
  artifact;
- closed standard input and release of unrelated descriptors or handles;
- worker identity verification before launch, an in-session identity proof bound
  to a fresh nonce, and re-verification of the worker, snapshot, and toolchain
  identities after the last child exits;
- teardown of the complete worker domain before publication;
- exactly the inventory controls this platform provides; and
- exactly one `capability-evidence-v1` record.

### The exhaustive native-control inventory

The normative authority is the `native_control_inventory` section of
[`conformance/v1/vectors/go-host-execution-policy.json`](../conformance/v1/vectors/go-host-execution-policy.json),
version `rc5-native-control-inventory-v1`. It is exhaustive: apply exactly what
it marks available for your platform, and never apply or report anything else.

| Control | macOS | Windows |
|---|---|---|
| `descendant-domain-termination` | available: process group and session teardown | available: Job Object kill-on-close |
| `active-process-count-limit` | unavailable: `no-private-aggregate-domain` | available: Job Object active-process limit |
| `aggregate-memory-limit` | unavailable: `no-private-aggregate-domain` | available: Job Object process and job memory limit |
| `per-file-size-limit` | available: `RLIMIT_FSIZE` | unavailable: `no-private-aggregate-domain` |
| `inherited-handle-restriction` | available: close-on-exec plus explicit descriptor release | available: explicit handle inheritance list |

Probe availability once per operation, before launching the worker. A host
label, a build-time constant, and a configuration file are not probes.

### The closed capability-evidence record

Emit exactly one record per operation:

```json
{
  "record_version": "capability-evidence-v1",
  "execution_policy": "manager-worker-v1",
  "platform": "macos",
  "controls": [
    {"name": "descendant-domain-termination", "availability": "available", "status": "applied", "probed_at": "pre-worker-launch"},
    {"name": "active-process-count-limit", "availability": "unavailable", "status": "unavailable", "probed_at": "pre-worker-launch"},
    {"name": "aggregate-memory-limit", "availability": "unavailable", "status": "unavailable", "probed_at": "pre-worker-launch"},
    {"name": "per-file-size-limit", "availability": "available", "status": "applied", "probed_at": "pre-worker-launch"},
    {"name": "inherited-handle-restriction", "availability": "available", "status": "applied", "probed_at": "pre-worker-launch"}
  ]
}
```

`availability` is `available` or `unavailable`, `status` is `applied` or
`unavailable`, and `probed_at` is `pre-worker-launch`. Each of these is an
error, not a variation:

| Condition | Diagnostic |
|---|---|
| `available` without `applied`, or `unavailable` without `unavailable` | `build_execution_capability_evidence_invalid` |
| a missing, duplicated, extra, or unknown control entry | `build_execution_capability_evidence_invalid` |
| an unknown `record_version` | `build_execution_capability_evidence_invalid` |
| availability not probed for this operation before worker launch | `build_execution_capability_evidence_invalid` |
| a deferred hardened guarantee named as a control entry | `build_execution_hardened_claim_forbidden` |
| an `execution_policy` other than `manager-worker-v1` | `build_execution_hardened_claim_forbidden` |

Report the record in install, dry-run plan, and status results. Never put it in
a cache key, a receipt, an install marker, or a conformance claim.

### The one failure boundary

A mandatory portable control that cannot be applied rejects with
`build_execution_control_unavailable` **before the worker starts**. Nothing else
at this boundary rejects: an unavailable inventory control and a missing
hardened guarantee are reported or simply absent, never a failure.

## What this policy does not promise

Every portable rule is a manager mechanism, and each one stops short of exactly
one kernel-enforced guarantee:

| What the portable policy does | What it is not |
|---|---|
| `network: "none"` — fixed offline Go module, proxy, checksum-database, and VCS configuration, with no network access for dependency resolution or the build | `total-network-denial` — kernel-enforced denial for the worker domain |
| a frozen snapshot nobody writes to, with pre/post identity re-verification | `read-only-source-and-toolchain` — kernel-enforced read-only presentation |
| operation-private write roots and a verified staged artifact | `private-build-root-only-writes` — kernel-enforced confinement of every descendant write |
| parent-enforced deadline, output, and artifact bounds plus available inventory controls | `hard-aggregate-descendant-resource-bounds` |
| a fixed manager-selected graph with per-program identity verification | `exact-executable-allowlisting` — a kernel path allowlist |
| preflight of the mandatory portable controls | `fail-closed-capability-preflight` of the hardened set |

The six right-hand guarantees belong to the hardened execution profile tracked as
`STORY-260728-327soo`. Claiming one under `manager-worker-v1` fails with
`build_execution_hardened_claim_forbidden`; their absence never fails anything.

## Cache, receipt, marker, and claim identity

The execution-policy identity is inside the canonical build input, so it is
covered by the logical cache key, by the exact receipt bytes, and therefore by
`receipt_sha256` in every marker. Marker v3 also records it explicitly; marker
v2 keeps its frozen rc.4 shape and binds it transitively.

Host capability evidence is **not** part of that identity. It is per-operation
reporting state, and putting it in the key would fragment portable cache
identity without adding any guarantee a reader may rely on.

Because the identity is hashed, three inputs that differ only in execution
policy produce three different cache keys. The executable proof is
[`conformance/v1/vectors/go-host-execution-policy.json`](../conformance/v1/vectors/go-host-execution-policy.json),
whose `cache_identity` section carries the portable input, a reserved hardened
input, and the pre-revision input that carried no execution policy at all, each
with its recomputable key. A pre-revision candidate entry therefore misses; it
never aliases.

A future hardened profile must introduce a new execution-policy identity and a
new claim schema version. Claim v3 admits only `manager-worker-v1`, so it cannot
express a hardened claim.
