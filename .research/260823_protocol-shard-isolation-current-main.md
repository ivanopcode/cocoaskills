# TASK-260803-2ol7ok — current-main protocol shard isolation

## Context

This analysis revalidates the previously reviewed six-shard protocol isolation
model against the clean CocoaSkills checkout
[`2bfe3d64e9142d62e8ea3f92558eeee331f4578a`](https://github.com/ivanopcode/cocoaskills/commit/2bfe3d64e9142d62e8ea3f92558eeee331f4578a)
and curator-spec
[`0c81c1f8d5321d822be2a2817b05aea03e656e15`](https://github.com/relux-works/curator-spec/commit/0c81c1f8d5321d822be2a2817b05aea03e656e15).
It changes no product, test, or workflow file.

The exact current-main partition is safe for later one-process-per-shard
execution if every process has a distinct pytest cache and base temp root. The
partition contains 1,045 nodes, 83 atomic clusters, no overlap, and no gap. The
mutable lifecycle observation cache and every non-self-resetting consumer remain
one indivisible 444-node cluster.

## Current-main delta and classification

A fresh collection returned 1,045 unique nodes. Relative to the reviewed
1,043-node package, no node was removed and common-node order is unchanged. Two
nodes were added:

- `test_rc6_current_status_diagnostic_separates_host_drift_from_causal_writes`
  is a read-only diagnostic assertion in the lifecycle region.
- `test_rc6_compiled_current_status_reports_classified_read_only_evidence`
  explicitly clears the manager lifecycle cache and observes the compiled
  fixture. It therefore cannot be split from the cached baseline.

Both are conservatively assigned to `lifecycle-cached-baseline`. The audited
diff from the prior head also adds unrelated Go-E2E fixtures to `conftest.py`
and extends current-status diagnostics in the lifecycle observer. The protocol
adapter file is unchanged. No existing node required a weaker boundary.

The machine-readable classification maps every node to environment, cwd, temp
roots, subprocesses, locks, caches, monkeypatches, platform APIs, order
assumptions, shared external state, source function/line, atomic cluster, and
shard. Its current summary is:

| Footprint | Nodes | Isolation boundary |
| --- | ---: | --- |
| Environment | 683 | Read-only conformance root; function-scoped monkeypatch restoration. |
| cwd | 693 | No process-wide `chdir`; subprocess cwd values are explicit and case-local. |
| Temp roots | 866 | `tmp_path` or collision-resistant lifecycle roots; unique `--basetemp` per shard. |
| Subprocesses | 702 | Explicit argv/env/cwd and case-local persistent roots. |
| Locks | 451 | Locks resolve below fresh manager/project/build roots. |
| Caches | 690 | Immutable schema/manifest caches; mutable lifecycle cache stays atomic or self-resets. |
| Monkeypatches | 733 | Function-scoped pytest monkeypatch or explicit cleanup. |
| Platform APIs | 712 | Host-guarded POSIX/Windows paths; handles are process-local. |
| Order assumptions | 737 | Cache-sensitive baseline remains atomic; sabotage functions clear before and in `finally`. |
| Shared external state | 1,045 | Same authenticated, pinned, read-only conformance root; no live service. |

The four audited harness hashes are:

| File | SHA-256 |
| --- | --- |
| `tests/test_protocol_conformance.py` | `62a4c207bc82b87fa85116c9a10ace4a821f351d373c94eaf767c712ac6da718` |
| `tests/protocol_conformance_adapters.py` | `ee933fa93c93145de754654b1b3e2d17f57d792adfa5170cae32e23ea5701a07` |
| `tests/protocol_lifecycle_observations.py` | `21e08f59921f199362c4ad13cf03ce6f97c7383a98489aebe142e1dd098bf3bd` |
| `tests/conftest.py` | `4c8ce0208c1528dc6af4bee98b62c0535b9ff65d412e275fd9edecde7cba133f` |

## Exact shard manifests and bounds

| Shard | Nodes | Atomic boundary | Local concurrent probe | Hosted Windows attributed time | Bound |
| --- | ---: | --- | ---: | ---: | ---: |
| `p00-contract-and-registry` | 565 | 47 whole function clusters | 565 passed, 3.23s | 0.15m | 5m |
| `p01-lifecycle-cached-baseline` | 444 | One indivisible cache-sharing cluster | 444 passed, 235.74s | 17.79m | 30m |
| `p02-lifecycle-sabotage-a` | 9 | 9 self-resetting functions | 9 passed, 448.43s | 29.05m | 45m |
| `p03-lifecycle-sabotage-b` | 9 | 8 self-resetting functions plus one POSIX probe | 8 passed, 1 skipped, 357.04s | 24.45m | 45m |
| `p04-lifecycle-sabotage-c` | 10 | 9 whole self-resetting functions | 10 passed, 482.03s | 17.28m | 45m |
| `p05-lifecycle-sabotage-d` | 8 | 8 self-resetting functions | 8 passed, 404.74s | 24.73m | 45m |

The Windows attribution assigns each interval between consecutive pytest result
timestamps to the node that ends the interval. It is a conservative planning
estimate, not a claim that the hosted run executed shards. The 45-minute
sabotage bound is 1.55 times the largest observed attributed shard duration;
the smaller p00 and p01 bounds still retain material headroom. The manifest
stores these bounds as `timeout_minutes`, and the verifier rejects drift.

## Validation evidence

Every gate below was run directly; expected-red probes are failures, not passes.

| Gate | Exit | Evidence |
| --- | ---: | --- |
| Required board start mutation | 0 | Task entered `analysis`. |
| System pytest readiness | 1 | Setup failure: system Python 3.14.4 had no pytest; not a pass. |
| Task-local `.[dev]` install | 0 | Non-editable install under `.temp/`; source checkout stayed clean. |
| Fresh protocol collection | 0 | Exactly 1,045 unique nodes. |
| Classification/manifest regeneration | 0 | 1,045 mappings; six shards `565/444/9/9/10/8`; 83 clusters. |
| Positive checkout-aware verifier | 0 | Exact heads, clean checkouts, four hashes, collection, selectors, atomicity, bounds, overlap 0, gap 0. |
| Deliberate one-node gap | 1 | Expected-red; named missing rollback vector `case3`. |
| Deliberate cross-shard overlap | 1 | Expected-red; named the duplicated node and p00/p02. |
| Deliberate timeout drift | 1 | Expected-red; named the p02 `timeout_minutes` mismatch. |
| Deliberate wrong source checkout | 1 | Expected-red; reported expected source HEAD and observed protocol HEAD. |
| macOS six-process shard probe | 0 each | All six exact manifests green with unique pytest cache/base-temp roots. |
| macOS reverse-order p01 repetition | 0 | 444 passed in 168.38s. |
| Hosted run/job API and log retrieval | 0 | Terminal metadata and full Windows protocol log retrieved. |
| Hosted-to-current audited-file diff | 0 | Four harness files at hosted `b1e05cdf` are byte-identical to current `2bfe3d64`. |

## Hosted Windows fact-check and anomaly

The terminal [main canary run 32248828531](https://github.com/ivanopcode/cocoaskills/actions/runs/32248828531)
used head `b1e05cdf58a7786f25c0891907ac0b8ecb3e10d2`. Its
[Windows protocol job 96055135893](https://github.com/ivanopcode/cocoaskills/actions/runs/32248828531/job/96055135893)
concluded `success` with **1,036 passed, 9 skipped in 6,807.51s**. Parsing all
1,045 result timestamps finds 35 intervals longer than 20 seconds, totalling
112.836 minutes, which corroborates the supplied 112.8-minute figure.

The overall workflow conclusion is `failure`, not success, because the separate
macOS Go-E2E job failed. That does not invalidate the terminal Windows protocol
job, but the distinction must remain explicit. The hosted head is also not the
current checkout; the evidence transfers only because `git diff --exit-code`
proved all four audited protocol/harness files byte-identical. No `ssh win` run
was added: the hosted Windows job provides stronger native evidence for the
exact audited bytes, while prior task evidence already records unavailable
non-interactive authentication on the weaker diagnostic host.

## Artifacts and recommendation

- `TASK-260803-2ol7ok_protocol-isolation-classification.json`: exhaustive
  per-node classification and audited hashes.
- `TASK-260803-2ol7ok_protocol-shards.json`: ordered exhaustive node lists,
  selectors, atomic clusters, and bounded timeouts.
- `TASK-260803-2ol7ok_verify-protocol-shards.py`: fail-closed checkout,
  cleanliness, hash, collection, schema, selector, overlap, gap, atomicity, and
  timeout verifier.

Hand these artifacts to independent review. A later workflow change should use
the manifests verbatim, give each process/job a unique pytest cache and base
temp root, require every shard result in a fail-closed aggregate, and refuse to
run when the verifier detects checkout, collection, hash, overlap, gap,
atomicity, selector, or timeout drift. Do not split the 444-node lifecycle
cluster or silently regenerate manifests inside CI.
