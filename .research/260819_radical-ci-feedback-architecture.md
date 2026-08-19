# TASK-260819-2j7v0x — Radical CI and developer-feedback architecture

**Decision status:** recommendation ready for independent review. This document
changes no workflow or product code.

## Executive decision

CocoaSkills should keep Python 3.11–3.14 support and build a **tiered,
fail-closed CI graph on standard GitHub-hosted runners**:

1. an always-created, change-aware `CI / fast` pull-request aggregate with a
   p95 feedback SLO of 15 minutes [H];
2. a required `CI / merge` qualification aggregate with deterministic,
   disjoint, exhaustive hybrid job/worker shards and a p95 SLO of 20 minutes
   [H];
3. an exhaustive sharded nightly matrix across every supported Python and all
   three operating systems, plus serial/reverse order canaries; and
4. a pre-publication release qualification gate plus the existing
   post-publication distribution smoke tests.

The critical change is **parallelizing the Windows Python 3.14 protocol and Go
E2E stages**, not dependency caching or dropping interpreter versions. The
current workflow already reduced repeated protocol execution and thereby cut
median raw runner use from **678.3 to 297.98 minutes, a 56.1% reduction [D]**.
Nevertheless, median workflow latency is still **146.05 minutes [M]**. The
Windows/Python 3.14 critical job is now 145.55 minutes median, versus 138.1
minutes in the earlier reviewed baseline (**+5.4% [D]**), because it still runs
ordinary tests, protocol conformance, and Go E2E serially. The current workflow
is at
[`239c807`](https://github.com/ivanopcode/cocoaskills/commit/239c80768bd5e46cd12c5fabb7593a8048ff0611),
and its topology is visible in the
[exact workflow](https://github.com/ivanopcode/cocoaskills/blob/239c80768bd5e46cd12c5fabb7593a8048ff0611/.github/workflows/ci.yml).

Dropping Python 3.11 would save approximately **29.52 raw runner-minutes per
run, 9.9% [D]**, but would save effectively **zero critical-path minutes [D]**
because Windows/Python 3.14 remains the slow job. A Go or Rust rewrite is not a
CI optimization until profiling identifies product CPU as the bottleneck.
Mojo 1.0.0 was released on 2026-08-11, but it is not a viable CocoaSkills
runtime in this decision window: Windows is supported only through WSL rather
than natively, and Python-to-Mojo extension support is explicitly early and
documents active limitations
([Mojo 1.0.0 release](https://github.com/modular/modular/releases/tag/max/v26.5.0),
[system requirements](https://mojolang.org/docs/requirements/),
[Python interoperability](https://mojolang.org/docs/manual/python/mojo-from-python/)).

## Evidence notation and boundary

- **[M] measured** — reproduced from repository state, local commands, or
  GitHub Actions job/step timestamps.
- **[D] derived** — arithmetic over measured inputs; the inputs and formula are
  stated.
- **[H] hypothetical** — a target or forecast that requires a pilot.
- **[O] official constraint** — a current primary-source platform or lifecycle
  fact.

All estimates carry one of these labels. Time-sensitive external facts link to
official primary sources. The source checkout was clean at
`239c80768bd5e46cd12c5fabb7593a8048ff0611`, identical to `origin/main` and the
GitHub `main` head when inspected on 2026-08-19. The prior reviewed baseline is
`TASK-260803-1nj6ar_ci-baseline-topology.md` and its accepted reviewer verdict
on `TASK-260803-1nj6ar`.

The eight comparable current-topology timing runs are successful runs from
2026-08-06 through 2026-08-07. The exact-head
[run 32196682228](https://github.com/ivanopcode/cocoaskills/actions/runs/32196682228)
was still running its Windows protocol step at the 2026-08-19 00:09:01 UTC
evidence cutoff; it is censored and excluded from aggregates rather than
counted as a short run.

## Current baseline

### Topology, checks, and test executions

The current `test` job is a 3 OS × 4 Python matrix:

- operating systems: `ubuntu-latest`, `macos-latest`, `windows-latest`;
- Python: 3.11, 3.12, 3.13, 3.14;
- ordinary tests run in all 12 cells;
- protocol conformance and accepted Go E2E run only in the three Python 3.14
  cells;
- mypy runs independently on Ubuntu/Python 3.12; and
- `Build artifacts` still has `needs: test`, so it waits for every matrix cell.

That produces 14 checks: 12 test matrix checks, typecheck, and build. A live
GitHub ruleset query found that `protect-main` contains only deletion and
non-fast-forward rules; the legacy protection endpoint returned HTTP 404
`Branch not protected`. Therefore no CI status is currently required before a
merge. The public API exposes the
[repository metadata](https://api.github.com/repos/ivanopcode/cocoaskills) and
[active main ruleset](https://api.github.com/repos/ivanopcode/cocoaskills/rulesets/18583550).
This is a correctness gap independent of speed.

Release qualification has a second gap. A `v*` tag starts
[`release.yml`](https://github.com/ivanopcode/cocoaskills/blob/239c80768bd5e46cd12c5fabb7593a8048ff0611/.github/workflows/release.yml),
which builds and publishes to PyPI before any native install matrix. The
[`Distribution Smoke` workflow](https://github.com/ivanopcode/cocoaskills/blob/239c80768bd5e46cd12c5fabb7593a8048ff0611/.github/workflows/distribution-smoke.yml)
starts only after a GitHub release is published or a Release workflow has
succeeded [M]. It covers pipx and uv on all three operating systems plus mise
and `install.sh` where supported, but this is post-publication detection rather
than a pre-publication gate. The target architecture moves an exact-artifact
install subset before publication and retains the current post-publication
oracle.

Exact collection at current head, Python 3.14.4, pytest 9.1.1, and the two CI
protocol pins produced:

| Class | Unique nodes | CI executions/run | Evidence |
| --- | ---: | ---: | --- |
| Ordinary | 1,537 [M] | 18,444 (`1,537 × 12`) [D] | Exact collect with protocol and Go E2E ignored, exit 0 |
| Protocol | 1,045 [M] | 3,135 (`1,045 × 3`) [D] | Exact pinned protocol collect, exit 0 |
| Go E2E | 20 total [M] | 36 (`16 macOS + 16 Windows + 4 Ubuntu`) [D] | Exact marker-specific collects, exit 0 |
| **Total** | **2,602 [M]** | **21,615 [D]** | Full exact collect, exit 0 |

The workflow's pinned-spec SHA (`0c81c1f8…`) and candidate SHA (`432eb2ee…`)
are different commits, but their checked-out `conformance/v1` manifests are
byte-identical and both declare protocol `1.0.0-rc.6` [M]. The two independent
1,045-node collections agree; this report does not mistake a SHA difference for
a protocol-content difference.

Compared with the reviewed `d1c0655` baseline, unique nodes increased from
2,370 to 2,602 (**+9.8% [D]**) while executions fell from 28,440 to 21,615
(**−24.0% [D]**). Protocol was reduced from every Python version to Python
3.14, but Go E2E and 210 ordinary nodes were added.

The current largest files by collection are protocol conformance (1,045),
`test_builds_go_v1.py` (163), `test_skillspec.py` (111), and
`test_transactions.py` (101) [M]. The source tree contains 71 Python files and
45,037 lines; tests contain 67 Python files and 48,889 lines [M]. The protocol
harness alone—test file, adapters, lifecycle observers, and Go E2E—is 14,813
lines [M]. Those sizes matter when considering a language rewrite.

### Recent workflow latency and runner use

“Raw runner-minutes” is the sum of every terminal job's wall duration. It is
not billable cost. Standard hosted runners are free and unlimited for this
public repository [O], although concurrency and resource use still matter; see
GitHub's current
[runner specifications](https://docs.github.com/en/actions/how-tos/write-workflows/choose-where-workflows-run/choose-the-runner-for-a-job).

| Run | Event | SHA | Workflow wall | Raw runner-minutes | Windows 3.14 job | Ordinary / protocol / Go E2E in that job |
| --- | --- | --- | ---: | ---: | ---: | ---: |
| [31212602048](https://github.com/ivanopcode/cocoaskills/actions/runs/31212602048) | push | `7f04ae1` | 146.13 min [M] | 304.98 [M] | 145.67 [M] | 15.63 / 101.47 / 27.15 [M] |
| [31179525490](https://github.com/ivanopcode/cocoaskills/actions/runs/31179525490) | PR | `636ea5d` | 147.95 [M] | 301.05 [M] | 147.48 [M] | 15.23 / 103.42 / 27.53 [M] |
| [31170790762](https://github.com/ivanopcode/cocoaskills/actions/runs/31170790762) | PR | `8e448f6` | 107.43 [M] | 271.97 [M] | 107.03 [M] | 11.83 / 77.30 / 16.70 [M] |
| [31157428890](https://github.com/ivanopcode/cocoaskills/actions/runs/31157428890) | PR | `abcddea` | 180.83 [M] | 331.88 [M] | 180.33 [M] | 21.47 / 130.37 / 27.03 [M] |
| [31154792344](https://github.com/ivanopcode/cocoaskills/actions/runs/31154792344) | PR | `b0b6970` | 156.10 [M] | 298.92 [M] | 155.60 [M] | 16.75 / 110.03 / 27.38 [M] |
| [31146188292](https://github.com/ivanopcode/cocoaskills/actions/runs/31146188292) | PR | `c03436b` | 137.25 [M] | 280.73 [M] | 136.83 [M] | 14.17 / 97.58 / 23.68 [M] |
| [31078339926](https://github.com/ivanopcode/cocoaskills/actions/runs/31078339926) | push | `b04a896` | 141.05 [M] | 297.05 [M] | 140.70 [M] | 16.37 / 102.33 / 20.27 [M] |
| [31070633835](https://github.com/ivanopcode/cocoaskills/actions/runs/31070633835) | PR | `b04a896` | 145.97 [M] | 290.02 [M] | 145.43 [M] | 15.43 / 102.00 / 26.55 [M] |
| **Median** | — | — | **146.05 [M]** | **297.98 [M]** | **145.55 [M]** | **15.53 / 102.17 / 26.79 [M]** |
| **Linear p95** | — | — | **172.18 [D]** | **322.47 [D]** | **171.68 [D]** | **19.82 / 123.25 / 27.48 [D]** |

The CI workflow file is byte-identical from `b04a896` through current head
(`git diff --exit-code`, exit 0), so these eight runs are topology-comparable. Build
adds only 0.30–0.43 minutes [M] after the matrix. The Windows/Python 3.14 cell
therefore explains essentially the entire workflow critical path.

Phase medians and linear p95 across the same eight runs are:

| Phase | Ubuntu median / p95 | macOS median / p95 | Windows median / p95 |
| --- | ---: | ---: | ---: |
| Ordinary, each matrix cell | 3.27 / 5.03 min [M/D] | 3.92 / 4.59 [M/D] | 17.43 / 21.50 [M/D] |
| Protocol, Python 3.14 | 21.67 / 39.87 [M/D] | 24.06 / 26.52 [M/D] | 102.17 / 123.25 [M/D] |
| Go E2E, Python 3.14 | 0.20 / 0.24 [M/D] | 9.81 / 10.80 [M/D] | 26.79 / 27.48 [M/D] |

Package installation medians are 0.27 minutes on Ubuntu, 0.20 on macOS, and
0.62 on Windows; the corresponding p95 values are 0.43, 0.32, and 0.86 [M/D].
Even perfectly eliminating that step would remove less than one critical-path
minute [D]; it cannot turn a 146-minute workflow into a sub-15-minute loop.

### Local feedback

Two standalone executions of the exact current ordinary selector passed locally
on Apple silicon/Python 3.14.4 with **1,475 passed and 62 skipped**: the first
reported 344.69 pytest seconds (345.12 process-wall seconds), and the later run
reported 458.94 pytest seconds; both exited 0 [M]. Each emitted 24 pytest cleanup
warnings for non-empty temporary toolchain directories [M]. The 33.1% timing
spread [D] is itself evidence that one local run is not an operational p95.

The same selection under pytest-xdist 3.8.0 with four workers,
`--dist=loadfile`, a unique OS-level base temp directory, and an isolated pytest
cache passed **1,475/62 in 118.99 seconds, exit 0 [M]**. Relative to the two
sequential observations, that is a **2.90–3.86× observed local speedup range
[D]**. An earlier xdist attempt put `--basetemp` below the Git checkout and
correctly failed one “non-Git directory” test (exit 1); the green rerun
demonstrates both the speed opportunity and the need for semantically isolated
temp roots. No hours-long local protocol run was started.

## Target SLOs and measurement contract

These SLOs are deliberately below the earlier 100-minute interactive goal.
Nightly is exhaustive rather than interactive and has a separate budget.

| Surface | Metric | Target |
| --- | --- | ---: |
| Local focused edit | selection start → result | p50 ≤30 s, p95 ≤90 s [H] |
| Local pre-push | full ordinary on developer OS | p50 ≤2 min, p95 ≤3 min [H] |
| PR fast feedback | event accepted → `CI / fast` terminal | p50 ≤10 min, p95 ≤15 min [H] |
| PR first failing signal | event accepted → first causal failure | p50 ≤5 min, p95 ≤10 min [H] |
| Merge qualification | `CI / fast` green → `CI / merge` terminal | p50 ≤15 min, p95 ≤20 min [H] |
| Release qualification | qualification start → publish eligibility | p50 ≤45 min, p95 ≤60 min [H] |
| Nightly exhaustive | schedule start → exhaustive aggregate | p50 ≤45 min, p95 ≤60 min [H] |

Measure queue time separately from execution, and publish both. Use at least 20
non-cancelled comparable runs for operational p95; cancelled/superseded runs
remain a separate waste cohort. Record per-job and per-node duration, setup,
cache hit, runner image, OS/Python, selected-node count and digest, shard digest,
skip count, retries, and failure classification. A regression is not hidden by
changing cohorts.

## Architecture comparison

The table compares four viable deployment paths and the rewrite alternative.
Only the hosted deterministic-shard design meets the recommended balance today.

| Architecture | PR feedback | Merge wall | Runner use | Correctness risk | Maintenance / migration | Rollback |
| --- | ---: | ---: | ---: | --- | --- | --- |
| **A. Tiered matrices only**: canonical Python on PR, current full topology before merge | 18–23 min [D] from canonical three-OS ordinary p50/p95 plus setup | 146/172 min [M/D] | PR 25–35; merge 298/322 raw min [D/M] | Medium in fast lane; low at merge | Low; 2–4 engineer-days [H] | Restore current single workflow |
| **B. Tiered + deterministic hybrid hosted shards (recommended)** | 8–12 min; p95 ≤15 [H] | 12–20 min; p95 ≤20 [H] | PR 45–90; merge 290–380 raw min [H] | Low after isolation canary; serial/reverse nightly canaries cover order | Medium; 2–4 engineer-weeks [H] | Disable worker/job sharding and require serial aggregate |
| **C. In-job xdist only on standard 3–4 CPU runners** | 6–15 min [H] | 20–50 min [H] | 140–300 raw min [H] | Medium-high until shared state is grouped; default scheduling is not deterministic | Medium; 1–3 engineer-weeks [H] | `-n 0`, retain manifests and serial jobs |
| **D. Ephemeral/JIT 8–32 core fleet or managed larger runners** | 5–12 min [H] | 12–25 min [H] | 120–260 job-min plus paid hardware [H] | Environment drift and public-PR security risk | High; 4–8 engineer-weeks plus operations [H] | Switch `runs-on` back to hosted labels |
| **E. Go/Rust partial or full rewrite** | 15–25 min only after a successful hot-path rewrite [H] | 60–140 min unless harness/fixture work is also done [H] | 230–500 raw min during dual-run migration [H] | High semantic and packaging divergence | Very high; 3–9 engineer-months [H] | Dual implementation, parity oracle, feature flag |

Architecture A is a safe fallback but misses the 15-minute SLO. Architecture C
can help ordinary tests, but its within-run worker limit and state model are a
worse fit than job-level shards. Architecture D becomes attractive only after
organization/runner eligibility and a measured trial. Architecture E is a
product strategy, not the first CI lever.

Absolute false-negative probabilities are not measured, so assigning invented
percentages would be misleading. Use these numeric promotion budgets instead:

| Architecture | Correctness promotion budget | Steady maintenance | Direct runner charge in current public repo |
| --- | --- | ---: | ---: |
| A | Zero fast/full causal disagreements over 20 shadow runs [H]; full merge remains authoritative | ≤0.5 engineer-day/month [H] | $0 [O] |
| B | Zero selection misses in ≥200 PRs; zero shard gaps/overlaps; ≤1% flake/retry over 20 runs [H] | 1–2 engineer-days/month [H] | $0 on standard hosted runners [O] |
| C | Zero serial/parallel disagreements over 20 runs and ≤1% flake/retry [H] | 2–3 engineer-days/month [H] | $0 on standard hosted runners [O] |
| D | Zero hosted/high-core result or skip-set divergence over 10 comparable runs [H] | 3–8 engineer-days/month plus incident duty [H] | $2–$6/change trial budget [H] |
| E | Zero Python/native parity differences across the full recorded corpus and all three OSes before cutover [H] | 4–10 engineer-days/month during dual support [H] | $0 standard-runner charge [O]; 3–9 engineer-month migration [H] |

For the change-aware portion of B, zero misses in 200 observations corresponds
to only an approximately 1.5% one-sided 95% upper bound by the rule of three
[D], not proof of zero risk. That is why merge, nightly, and release remain
exhaustive. Engineering time is quantified instead of converted to currency
because no repository-specific loaded labor rate was supplied.

## Evaluation of the named options

### 1. Reducing supported Python versions

The project explicitly publishes `requires-python = ">=3.11"` and classifiers
for 3.11–3.14 in the
[current `pyproject.toml`](https://github.com/ivanopcode/cocoaskills/blob/239c80768bd5e46cd12c5fabb7593a8048ff0611/pyproject.toml).
Python 3.11 and 3.12 are still in security support through October 2027 and
October 2028; 3.13 and 3.14 are in bugfix support, and 3.15 is prerelease as of
the evidence date [O]. See the Python project’s current
[version-status table](https://devguide.python.org/versions/).

| Support choice | Raw ordinary saving/run | Critical-path saving | Decision |
| --- | ---: | ---: | --- |
| Drop 3.11 | 29.52 min, 9.9% of current total [D] | ~0 min [D] | Reject as a speed tactic; breaking support while Windows 3.14 remains critical |
| Keep only 3.13–3.14 | 57.18 min, 19.2% [D] | ~0 min [D] | Reject; moderate cost gain, no latency gain |
| Keep only 3.14 | 84.38 min, 28.3% [D] | ~0 min [D] | Reject; large compatibility loss, still no critical-path gain |
| Keep support, tier versions | PR avoids 3 older-version cells per OS; merge/nightly retain all [D] | PR improves; merge correctness preserved | **Adopt** |

Reconsider dropping 3.11 only at a declared compatibility boundary, with user
telemetry and migration notice. Do not justify it with CI speed. Add Python 3.15
as an allowed-to-fail nightly preview until its final release and dependency
compatibility are established; promotion is a support-policy decision, not part
of this task.

### 2. Tiered platform/version coverage

Platform coverage is non-negotiable. The latest successful run reported
ordinary skip totals of 89 on Ubuntu, 62 on macOS, and 215 on Windows [M], and
the source has explicit native branches. Interpreter redundancy is much higher
than platform redundancy.

Adopt these tiers:

- **PR fast:** Python 3.14 on Ubuntu, macOS, Windows; all three receive ordinary
  coverage with audited worker groups and high-value platform sentinels. Split
  Windows ordinary into two deterministic macro-shards only if the one-job
  four-worker pilot misses its six-minute p95 target [H].
- **Merge qualification:** ordinary on all 12 OS/Python cells; protocol and Go
  E2E on Python 3.14 across all three OSes, sharded.
- **Nightly:** full ordinary and full protocol across all 12 cells, plus Go E2E,
  serial order canaries, reverse/repetition probes, and cache-cold/cache-warm
  comparison.
- **Release:** at least the merge set plus min/max-Python artifact installation
  on all three OSes and the existing pipx/uv/mise/install.sh channels.

This retains evidence-backed correctness on macOS, Linux, Windows and every
published Python while moving redundant interpreter coverage out of the first
feedback wave.

### 3. Change-aware selection

Use change awareness only as an acceleration layer, never as the sole release
oracle. In the last 100 non-merge commits, 51% touched source, 25% only the
protocol harness plus documentation, 8% other tests/harness, 7% CI/packaging,
6% documentation only, 2% empty tag commits, and 1% other metadata [M]. The
sample is repository-specific and historically biased: docs-only skipping has
limited upside, while protocol-vs-ordinary routing can avoid substantial
irrelevant work.

Start with coarse ownership classes—documentation, packaging/CI,
ordinary Python, protocol harness/spec, and Go E2E—then add exact node mapping
only after shadow evidence. `pytest-testmon` is a useful local/advisory option:
its primary project documentation describes coverage-based dependency tracking
and a persisted test database. That state is valuable for a developer loop but
is not a sufficient cross-platform required gate for this filesystem-, Git-,
and subprocess-heavy suite; use it as one selector input, not the authority
([pytest-testmon project](https://github.com/tarpas/pytest-testmon)).

Required classifier behavior:

1. The workflow and stable aggregate always start. Do not put required checks
   behind workflow-level `paths` filters: GitHub documents that skipped filtered
   workflows can leave required checks pending, and path evaluation has file
   limits ([workflow syntax](https://docs.github.com/en/actions/reference/workflows-and-actions/workflow-syntax),
   [filtering limits](https://docs.github.com/en/actions/how-tos/troubleshoot-workflows)).
2. Compute a NUL-safe three-dot diff from the event base to the tested head,
   including renames, deletions, submodules, workflow files, tracked dependency
   inputs and the protocol pin. Git documents merge-base diff semantics and
   `--name-status -z`
   ([`git diff`](https://git-scm.com/docs/git-diff),
   [diff options](https://git-scm.com/docs/diff-options)).
3. Unknown paths, unavailable base, shallow history, classifier error, or
   oversized/untrusted input fail **open to the full test set**, never to zero.
4. `pyproject.toml`, `.gitignore`, `.github/**`, `tests/conftest.py`, protocol
   pins, schemas, package/build/release code, and shared test helpers always
   select the full affected lane. A `uv.lock` becomes an input only if a
   separate policy decision first approves unignoring and committing it.
5. Source modules select owning tests plus transitive imports and platform
   sentinels. Test changes select the changed atomic cluster and every fixture
   consumer. Deletes/renames select both old and new owners.
6. Persist the changed-file digest, rule version, reason, exact selected node
   IDs and collection digest as an artifact.

Run the classifier in shadow mode for at least **30 days and 200 qualifying PR
runs [H]**. A selection miss means selected tests pass while the full shadow
suite has a causal failure. Zero misses in 200 trials still only gives a rough
95% upper bound near 1.5% [D, rule-of-three], so full merge, nightly and release
gates remain mandatory. Any confirmed miss immediately returns all code changes
to full selection while the map is repaired.

Pytest supports exact node IDs, marks, `--collect-only`, and deselection
[O] ([selection reference](https://docs.pytest.org/en/stable/reference/reference.html),
[collection examples](https://docs.pytest.org/en/stable/example/pythoncollection.html)).
Exit 5 means no tests collected, so a required shard must treat an unexpected
empty selection as failure rather than success
([pytest exit codes](https://docs.pytest.org/en/stable/reference/exit-codes.html)).

### 4. Deterministic sharding and parallel execution

Prefer **deterministic job-level manifests plus audited xdist groups inside
each macro-shard** over default xdist scheduling. GitHub matrix jobs scale
beyond a single 4-CPU Windows runner; xdist's default `load` scheduler explicitly
has no guaranteed order. xdist also performs a full collection on every worker
and requires identical order/count across workers
([distribution modes](https://pytest-xdist.readthedocs.io/en/stable/distribution.html),
[implementation](https://pytest-xdist.readthedocs.io/en/stable/how-it-works.html),
[known limitations](https://pytest-xdist.readthedocs.io/en/stable/known-limitations.html)).

Each checked-in/generated manifest must contain source and protocol identities,
collection SHA-256, timing-snapshot identity, atomic groups, ordered node IDs,
and an expected non-empty node count. Generate it deterministically:

1. group order-sensitive/shared-state nodes atomically;
2. sort groups by descending frozen median duration, then stable group ID;
3. assign each group to the currently lightest shard, tie-breaking by shard ID;
4. do not silently regenerate inside the execution workflow; and
5. verify fresh collection equals the disjoint union of all shards with zero
   gaps, overlaps, split atomic groups, or unknown nodes.

The older `TASK-260803-2ol7ok` audit is supporting evidence, not an accepted
current manifest. At 1,043 protocol nodes it built six shards
`565/442/9/9/10/8`, preserved a 442-node lifecycle-cache cluster, and measured a
macOS serial sum of 1,385.62 seconds versus a 365.20-second parallel critical
shard, a 3.79× wall reduction [M/D]. That task remained blocked pending its
then-current hosted Windows handoff, and the suite is now 1,045 nodes at a later
head. Regenerate, re-audit, and re-canary; never reuse its manifest unchanged.

The current local ordinary xdist run provides a separate 2.90–3.86× observed
speedup range [D], but it does not validate protocol state isolation or hosted
Windows. A
three-job × three-worker protocol layout has nine nominal worker slots; dividing
the current 102.17-minute Windows median by nine gives an **11.35-minute ideal
lower bound [D]**. Collection, setup, atomic-group imbalance, filesystem
contention, and scheduler overhead make **12–15 minutes only a hypothetical
pilot target [H]**, not a forecast backed by Windows evidence. Similarly,
splitting the 26.79-minute Windows Go E2E suite into three isolated groups
targets **7–12 minutes [H]**. Applying the conservative 2.90× local ordinary
factor to the 17.43-minute hosted-Windows median gives a 6.01-minute ideal [D];
the operational target is **≤8 minutes p95 [H]**.

For protocol pilots, use explicit worker counts and `--dist loadgroup` with
`xdist_group` for atomic state; `loadfile` is too coarse for the 1,045-node
single protocol file. For ordinary tests, the measured `loadfile` canary is a
reasonable starting point. Give every worker a unique OS-level writable
temp/cache root. Keep an unsharded nightly oracle because parallel green is not
evidence that serial order remains green. Pytest gives every `tmp_path`
invocation a unique directory, but high-scope xdist fixtures execute once per
worker unless explicit coordination is added
([pytest temporary fixtures](https://docs.pytest.org/en/stable/getting-started.html),
[xdist fixture guidance](https://pytest-xdist.readthedocs.io/en/stable/how-to.html)).

### 5. Dependency caching and fixture redesign

Adopt a pinned `setup-uv` and uv's download cache for modest setup gains, but do
not silently make a lockfile authoritative. At exact head, `.gitignore:16`
intentionally ignores `uv.lock`; `git ls-files` confirms that the local file is
untracked [M]. Preserve that project choice. The near-term CI path may use a
pinned uv version as a faster installer while retaining the current declared
requirements as the resolution input; this improves mechanics, not dependency
reproducibility. Key the dependency-download cache by OS, architecture, Python,
uv version, and tracked dependency metadata such as `pyproject.toml`.

Astral recommends the official `setup-uv` action and documents its built-in
cache support [O]. Astral also defines `uv.lock` as a universal/cross-platform
lock that should be committed for reproducible project installs and recommends
`uv sync --locked --all-extras --dev` in Actions [O]
([GitHub integration](https://docs.astral.sh/uv/guides/integration/github/),
[project lockfile semantics](https://docs.astral.sh/uv/concepts/projects/layout/#the-lockfile),
[locking and syncing](https://docs.astral.sh/uv/concepts/projects/sync/),
[cache semantics](https://docs.astral.sh/uv/concepts/cache/)). That is a viable
second path only after a separate decision to unignore, generate, review, and
commit one universal lock across Python 3.11–3.14 and all three OSes. Only then
should CI use `--locked`, key on `uv.lock`, and make lock changes a full-lane
classifier input.

In either path, cache only immutable dependency/tool downloads. Do not persist
test manager homes, pytest result caches, protocol writable roots, or Go E2E
build/cache state across jobs. The Go build cache is concurrent-safe and
input-aware [O], but CocoaSkills tests the correctness of cache identities
themselves, so test GOCACHE roots must remain isolated
([Go build/test caching](https://go.dev/cmd/go/)).

Fixture redesign has more upside than package caching but must preserve fresh
state. Safe candidates are:

- parse/authenticate immutable vector data once per process;
- create immutable seed Git repositories once, then copy/reflink to unique test
  roots;
- shorten Windows temp roots and record filesystem operations;
- batch repeated pure manifest/schema calculations; and
- preserve a fresh root for every mutation, rollback, lock, permission, and
  tamper-witness case.

Do not session-cache live writable manager homes or split the lifecycle-cache
atomic cluster. Pilot on the top 20 Windows nodes by duration. Accept fixture
work only if Windows protocol p95 improves at least **20% [H]**, all mutation
and repetition canaries remain green, and cleanup warnings do not increase.

### 6. Runner and hardware options

Standard public runners currently provide 4 CPU/16 GiB on Linux and Windows,
and 3 CPU/7 GiB on `macos-latest`; they are free for public repositories [O].
GitHub Free permits 20 concurrent standard jobs and at most five concurrent
macOS jobs [O], which requires planned waves and queue measurement
([runner specifications](https://docs.github.com/en/actions/how-tos/write-workflows/choose-where-workflows-run/choose-the-runner-for-a-job),
[Actions limits](https://docs.github.com/en/actions/reference/limits)).
GitHub matrices allow up to 256 jobs, `max-parallel`, and explicit `fail-fast`
control [O]
([workflow syntax](https://docs.github.com/en/actions/reference/workflows-and-actions/workflow-syntax)).

The repository is public but owned by a GitHub **User**, while managed larger
runners are available to organizations/enterprises on Team or Enterprise Cloud
[O]. They are therefore not currently deployable without ownership/plan change
([managing larger runners](https://docs.github.com/en/actions/how-tos/manage-runners/larger-runners/manage-larger-runners)).

Standard hosted runners have **$0 direct Actions charge for this public
repository [O]**. Larger runners are always billed: GitHub's current list price
for Windows x64 is $0.082/minute at 16 cores and $0.162/minute at 32 cores [O].
A single 15-minute lane would therefore cost $1.23 or $2.43 respectively, and a
30-minute lane $2.46 or $4.86 [D], before Linux/macOS lanes, storage, or retries
([Actions runner pricing](https://docs.github.com/en/billing/reference/actions-runner-pricing)).
Treat **$2–$6 incremental runner cost per qualified change [H]** as a trial
budget, not a promised cost. Self-hosted cost must include VM boot, idle,
security, logging, image maintenance, and failed-run replacement; no credible
repository-specific figure exists yet.

Keep public PRs on GitHub-hosted runners. GitHub warns that fork PRs can execute
dangerous code on a public repository's self-hosted machine. If a high-core
trial is later justified, use a clean one-job ephemeral/JIT VM, no secrets, no
private-network reachability, externalized logs, and destruction attestation.
GitHub recommends ephemeral rather than persistent autoscaling runners and
documents single-job JIT registration
([public-repository warning](https://docs.github.com/en/actions/how-tos/manage-runners/self-hosted-runners/add-runners),
[ephemeral runners](https://docs.github.com/en/actions/reference/runners/self-hosted-runners),
[JIT security](https://docs.github.com/en/actions/reference/security/secure-use)).

The previously inventoried 2-core/4-thread, 8.45 GiB Windows host remains a
diagnostic machine, not a speed candidate. A hardware trial must beat the hosted
shard design over at least 10 comparable runs, include queue/provisioning time
and total cost, and preserve native Windows behavior [H].

### 7. Partial rewrites in Go or Rust

Go and Rust both support native Linux, macOS, and Windows targets [O]
([Go target list](https://go.dev/doc/install/source),
[Rust tier-1 platforms](https://doc.rust-lang.org/rustc/platform-support.html)).
Rust/PyO3 can use a stable-ABI wheel, but broad distribution still requires
platform wheels; maturin explicitly supports and tests Windows, Linux, and
macOS and documents multi-platform/manylinux builds
([maturin tutorial](https://www.maturin.rs/tutorial.html),
[platform support](https://www.maturin.rs/platform_support)).

Neither language changes the measured cause by itself. The slow tests create
Git repositories, files, processes, locks, permissions, build artifacts, and
fresh lifecycle observations. A native implementation can speed product CPU,
but not eliminate required platform I/O or the Python test oracle. With no
profile showing product CPU dominance, a **10× speedup of an assumed 20% CPU
fraction yields only 1.22× end-to-end [H/D, Amdahl]**.

Decision:

- Do not start a rewrite for the CI SLO.
- Permit a two-week spike only if a Windows trace attributes at least 50% of
  protocol wall time to one pure-Python seam [H].
- Prefer a narrow, stable contract boundary already suitable for a worker
  process. Run Python/native implementations in dual mode and byte-compare
  outputs.
- Stop if the spike produces less than 20% end-to-end Windows protocol gain,
  creates platform-specific semantic differences, or expands the release wheel
  matrix without an operational owner [H].
- Roll back through the Python implementation feature flag; never make the
  native path the only correctness oracle during migration.

A full standalone Go/Rust CLI could eventually remove the Python-version
runtime contract, but it would replace rather than optimize CocoaSkills. Its
native artifact matrix and migration risk make it a separate product decision.

### 8. Mojo 1.0 feasibility

Mojo 1.0.0 was released with MAX 26.5 on 2026-08-11 [O], eight days before this
research snapshot
([official release](https://github.com/modular/modular/releases/tag/max/v26.5.0),
[Mojo 1.0.0 notes](https://mojolang.org/releases/v1.0.0/)). It still does not
meet the CocoaSkills gate:

- Windows is WSL-only, not native [O]
  ([requirements](https://mojolang.org/docs/requirements/));
- Python extension support is documented as early and has argument, keyword,
  dependency, property, and conversion limitations [O]
  ([calling Mojo from Python](https://mojolang.org/docs/manual/python/mojo-from-python/));
- precompiled `.mojoc` packages are tied to the exact compiler version [O]
  ([modules and packages](https://mojolang.org/docs/manual/packages/)); and
- the roadmap still lists packaging, stable tooling, testing, benchmarking and
  expanded platform support as ongoing work [O]
  ([roadmap](https://mojolang.org/docs/roadmap/)).

The workload is systems/filesystem orchestration rather than an accelerator
kernel, so Mojo also lacks a demonstrated performance fit. Re-evaluate only
after native Windows CI, mature Python packaging, a supported release policy
across all three OSes, and a benchmark showing at least 20% end-to-end gain [H].
Until then, expected deployable feedback improvement is **0 minutes [D]**:
adoption would require dropping required native Windows correctness or retaining
the Python implementation and its existing test cost. A dual-runtime attempt
would inherit at least the **3–9 engineer-month migration and 4–10
engineer-days/month maintenance envelope [H]** of architecture E while still
requiring the Python/native-Windows oracle; that is dominated by B.

## Recommended target gate architecture

Use two stable required check names, not every matrix child. Matrix children
remain diagnostic; aggregators use `if: always()` and fail unless every expected
job is present and successful. GitHub required checks can be bound to the
GitHub Actions app [O]
([ruleset status checks](https://docs.github.com/en/repositories/configuring-branches-and-merges-in-your-repository/managing-rulesets/available-rules-for-rulesets)).

Because this is a user-owned repository, GitHub merge queue is unavailable;
merge queues require a public repository owned by an organization [O]
([merge-queue availability](https://docs.github.com/en/pull-requests/how-tos/merge-and-close-pull-requests/merging-a-pull-request-with-a-merge-queue)).
Configure a strict main ruleset requiring both aggregates on the current PR
merge SHA. If the repository later moves to an organization, add `merge_group`
to both workflows before enabling merge queue
([required-check guidance](https://docs.github.com/en/pull-requests/how-tos/merge-and-close-pull-requests/troubleshooting-required-status-checks)).

| Stage | Required work | Coverage preserved | Target |
| --- | --- | --- | ---: |
| **Local edit** | `python -m pytest` on explicit impacted node IDs in the prepared environment; full mypy because it is cheap | Developer OS; exact selector recorded | ≤90 s p95 [H] |
| **Local pre-push** | Full ordinary with four workers on developer OS; protocol contract/read-only shard; changed native E2E | Developer OS, fast misuse detection | ≤3 min p95 [H] |
| **PR `CI / fast`** | Always-run classifier/inventory; mypy; build/metadata; full Python 3.14 ordinary on all three OSes with audited worker groups; platform protocol sentinels; small Go E2E smoke; full three-OS heavy macro-shards whenever protocol/core/build/harness inputs change | macOS/Linux/Windows on every PR; unknown changes expand to the full affected canonical suite | ≤15 min p95 [H] |
| **Merge `CI / merge`** | Starts after fast on every non-draft current SHA; ordinary 12-cell matrix with audited workers; three deterministic protocol macro-shards × three worker groups on Python 3.14/all OS; three isolated Go E2E groups/all OS; build consumes all aggregates | Every supported Python and OS before merge; full protocol/native contract on all OS | ≤20 min p95 [H] |
| **Post-merge main** | Re-run canonical three-OS fast set; publish timings/shard evidence; do not mask pre-merge gate | Detect merge/push-only behavior and feed duration model | ≤15 min p95 [H] |
| **Nightly exhaustive** | Full ordinary + protocol on all 12 cells; Go E2E; serial-order samples; reverse/repetition; xdist canary; cold/warm cache; current Python prerelease advisory | Detects interpreter, order, concurrency, cache, and runner-image regressions | ≤60 min p95 [H] |
| **Release qualification** | Fresh merge gate at release SHA plus a successful exact-SHA exhaustive run no older than 24 h; min/max-Python artifact install on all OS; wheel/sdist metadata and checksums; pre-publish pipx/uv smoke; then existing post-publish channels | Published artifacts and all supported OS/Python boundaries | ≤60 min p95 [H] |

The merge workflow should sequence work in two capacity-aware waves so the
documented 20-job total and five-job macOS limits do not create uncontrolled
queueing. The proposed heavy wave uses nine protocol macro-shard jobs; with
ordinary and Go lanes staged around it, no wave may exceed 20 total or five
macOS jobs [D]. Run fast/canonical ordinary first; then heavy shards. Use
`fail-fast: false` inside coverage matrices so one failure does not erase other
platform evidence, while the aggregate itself fails closed. Superseded SHA
runs may still be cancelled by concurrency.

## Migration plan and measurable acceptance gates

### Phase 0 — Instrument and freeze the baseline (about 1 week [H])

- Emit JUnit/per-node durations, queue/setup/test intervals, skip counts,
  runner image, selection and collection digests.
- Separate ordinary, protocol, and Go E2E into independently timed jobs without
  changing coverage.
- Create stable `CI / fast` and `CI / merge` aggregates in observation mode.
- Preserve the eight-run baseline in this report.

**Gate:** 10 comparable shadow runs [H], exact node/execution accounting, and
aggregate parity with the current workflow. No required-check migration yet.

### Phase 1 — Fast PR tier (about 1–2 weeks [H])

- Pin `setup-uv`/uv and use dependency-download-only caches without changing
  the intentionally lockless tracked install contract. Treat a committed
  universal lock as a separate policy/migration decision.
- Run canonical three-OS Python 3.14 ordinary coverage with explicit audited
  worker counts; add a second deterministic Windows macro-shard only if the
  one-job pilot misses six-minute p95 [H].
- Add protocol/platform and Go E2E sentinels.
- Keep full current CI in shadow.

**Gate:** `CI / fast` p95 ≤15 minutes, queue p95 ≤3 minutes, zero selected/full
causal disagreements, and raw fast consumption ≤90 minutes over 20 runs [H].

### Phase 2 — Full deterministic merge shards (about 2–4 weeks [H])

- Regenerate the protocol isolation classification and manifest at the actual
  implementation head (1,045 nodes at this research snapshot).
- Canary three deterministic macro-shards with three audited worker groups each
  on Ubuntu, macOS and hosted Windows with unique OS-level temp/cache roots;
  separately split Go E2E into three isolated groups.
- Prove normal order, reverse/repetition and serial-vs-parallel parity.
- Make build depend on the stable full aggregates, then atomically add both
  stable contexts to the main ruleset.

**Gate:** zero gaps/overlaps/atomic splits; all three platforms green for 10
comparable runs; merge p95 ≤20 minutes; raw p95 ≤380 minutes; flake/retry rate
≤1% [H]. If Windows protocol cannot meet 15 minutes inside this gate, keep the
new graph in shadow and proceed to the optional runner trial; do not weaken
coverage to force the SLO.

### Phase 3 — Change-aware shadow and gradual enablement (30 days and ≥200 PR
runs [H])

- Log selections while still executing full comparison suites.
- Enable docs-only selection first, then test-only, then mapped source changes.
- Never remove full merge/nightly/release gates.

**Gate:** zero confirmed misses, unknown-path fallback 100%, classifier errors
100% full-fallback, and mapping coverage ≥95% [H].

### Phase 4 — Fixture and runner experiments (optional)

- Profile the top 20 Windows protocol/Go nodes.
- Pilot immutable seed fixtures.
- Trial a clean ephemeral high-core Windows runner only if hosted sharding
  still misses the merge SLO.

**Gate:** ≥20% end-to-end improvement, no semantic/skip drift, documented total
cost, and verified destruction/isolation [H].

### Phase 5 — Native-language spike (conditional, separate decision)

Only enter if profiling meets the ≥50% pure-Python CPU threshold. Mojo remains
out of scope until its platform/release gates are satisfied despite its 1.0.0
release.

## Stop and rollback criteria

| Trigger | Stop action | Rollback path |
| --- | --- | --- |
| Collection digest differs; gap, overlap, empty required shard, or atomic group split | Stop shard rollout immediately | Require serial current suite; regenerate and review manifest |
| Any serial/parallel or order/repetition result disagreement | Treat as correctness failure, not flake | `-n 0`, unsharded jobs, isolate shared state |
| Merge p95 >20 min after 10 comparable runs or raw p95 >380 min [H] | Stop promotion; inspect queue, worker contention, and imbalance | Keep new graph shadow-only; require legacy aggregate while tuning or trialing hardware |
| Queue p95 >5 min or macOS second-wave delay >10 min [H] | Reduce concurrent shard count and reorder waves | Lower `max-parallel`, consolidate short shards |
| Any confirmed change-selection miss | Disable selection for all code changes | Full PR/merge selection; repair map in shadow |
| Cache hit changes dependency set, node list, skips, or result | Disable that cache key immediately | Reinstall from tracked declarations with a cold dependency cache; use `uv sync --locked --refresh` only if a committed lock was separately approved |
| Required aggregate can succeed with absent/cancelled/skipped child | Stop ruleset migration | Require old known-good aggregate until fail-closed logic is fixed |
| Self-hosted/JIT runner lacks clean-image, no-secret, network-isolation, log, or destruction evidence | Do not route PR work there | Standard GitHub-hosted runners |
| Rewrite spike gains <20% end-to-end or parity diverges [H] | End spike | Python implementation remains authoritative |
| Mojo lacks native Windows, mature packaging, or measured ≥20% gain [H] | Do not adopt | No Mojo dependency or artifact |

Required-check migration itself must be atomic: first run the new aggregates for
at least seven days [H], then add them to the ruleset while the old aggregate still
runs; remove the old context only after the new contexts are observed green on
the current SHA. GitHub requires checks on the latest commit and documents the
status semantics
([required-check troubleshooting](https://docs.github.com/en/pull-requests/how-tos/merge-and-close-pull-requests/troubleshooting-required-status-checks)).

## Risk and mitigation register

| Risk | Likelihood / impact | Mitigation |
| --- | --- | --- |
| Sharding hides order-dependent or shared-cache behavior | Medium / high | Atomic groups, unique roots, serial and reverse nightly oracles, parity canaries |
| Change map misses a transitive dependency | Medium / high | Full fallback, shadow comparison, zero-miss enablement, full merge/nightly/release |
| More jobs hit account or macOS concurrency limits | High / medium | Two waves, `max-parallel`, queue SLO, three-macro-shard start rather than blind node-count split |
| Cache contaminates mutable test state | Medium / high | Cache dependencies only; tracked-input keys while lockless, lock key only after separate commit decision; isolated manager/GOCACHE/pytest temp roots |
| Required check is skipped or aggregate overlooks a child | Medium / critical | Always-created aggregate, `if: always()`, explicit expected-job manifest, app-bound required contexts |
| Public PR compromises a self-hosted runner | High / critical if persistent | Keep PRs hosted; clean one-job JIT only, no secrets/network, destroy and retain external logs |
| Runner image drift changes timings or native behavior | Medium / medium | Record image; compare cohorts; pinned tool versions; nightly native matrix |
| Suite growth silently consumes latency budget | High / medium | Per-lane budgets, node/duration digest, weekly trend and 10% regression alert [H] |
| Go/Rust rewrite creates two semantic implementations | Medium / high | Narrow seam, dual-run parity, feature flag, separate product decision |
| Mojo platform/interop gaps block native Windows | High / high | Reject until explicit adoption gates are met |

## Fact-check and command ledger

Every validation below ran as a standalone process; no expected-red result is
presented as passing.

| Evidence / command | Exit | Result |
| --- | ---: | --- |
| Required initial board status mutation | 0 | Task remained/entered `analysis` |
| Source `git status`, local/origin/API head checks | 0 | Clean `main`, exact `239c807` identity |
| Prior outcome and reviewer verdict reads | 0 | 2,370-node and 678.3-minute baseline was previously accepted |
| Eight GitHub run/job/step API extractions | 0 | Reproduced current wall, runner-minute, cell, and phase distributions |
| `git diff --exit-code b04a896..HEAD -- .github/workflows/ci.yml` | 0 | Eight runs use the current workflow topology |
| Four initial `python -m pytest --collect-only` commands | 127 each | Failed honestly: this shell has no `python` executable; no counts were taken from them |
| Full current exact collection | 0 | `COLLECTED=2602` |
| Current ordinary exact collection | 0 | `COLLECTED=1537` |
| Current protocol exact collection | 0 | `COLLECTED=1045` |
| Go E2E native / Ubuntu marker collection | 0 / 0 | `16` / `4` nodes |
| Earlier local sequential ordinary test run | 0 | 1,475 passed, 62 skipped in 344.69 pytest seconds / 345.12 process-wall seconds; 24 cleanup warnings |
| Later local sequential ordinary test run | 0 | 1,475 passed, 62 skipped in 458.94 pytest seconds; 24 cleanup warnings |
| First four-worker xdist ordinary canary | 1 | Failed honestly after 129.05 s: repo-nested `--basetemp` invalidated one non-Git-directory test assumption |
| Corrected four-worker xdist ordinary canary | 0 | 1,475 passed, 62 skipped in 118.99 s using an OS-level temp root |
| Initial compact ruleset projection using an invalid jq shape | 1 | Diagnostic failure (`cannot iterate over: null`); no claim taken from it |
| Raw ruleset query and exact ruleset-detail query | 0 / 0 | Active main rules are deletion and non-fast-forward only |
| Legacy main branch-protection query | 1 / HTTP 404 | Expected negative: `Branch not protected`; required checks are absent |
| Repeated current exact-head Actions reads | 0 | Run 32196682228 remained in its Windows protocol step through 00:09:01 UTC and is censored |
| Latest comparable successful-run log extraction | 0 | Reproduced ordinary skip totals (Ubuntu 89, macOS 62, Windows 215) and protocol totals |
| Project history classification | 0 | 100 non-merge commits classified for selector sizing |
| Official Mojo GitHub release API query | 0 | `max/v26.5.0`, Mojo 1.0.0, published 2026-08-11 |
| Spawn status / directive checkpoint reads | 1 / 1 | Recoverable control-plane failure: the injected run ID had no manifest; no research claim depends on it |
| `git ls-files --error-unmatch uv.lock` | 1 | Expected negative: the local `uv.lock` is intentionally not tracked |
| `git check-ignore -v uv.lock` | 0 | `.gitignore:16` is the active ignore rule; lock-authoritative CI remains a separate decision |
| Source/test line-count verifier | 0 | Reproduced 71/45,037 source and 67/48,889 test Python file/line counts |
| First post-revision report audit | 1 | Validator was too strict for reconstructing an exact-second p95 from table rows already rounded to 0.01 minute; report data was not changed |
| Corrected post-revision report audit | 0 | Required sections, five architectures, 76 source links, stale-claim exclusions, and arithmetic all verified |
| Independent eight-run API aggregation replay | 0 | Reproduced workflow, runner, cell, phase, and Python-version summaries |
| First / corrected independent eight-run arithmetic verifier | 1 / 0 | First verifier mistyped two interpolation expectations; corrected gate reproduced every reported median, p95, delta, version saving, and execution count |
| Local sequential/xdist reconciliation arithmetic | 0 | Reproduced 33.1% sequential spread, 2.90–3.86× observed speedup range, and 6.01-minute conservative Windows ideal |
| Stale six-run / pre-1.0 Mojo wording search | 1 | Expected negative: no stale cohort metrics, `1.0.0b2`, or “still/remains beta” wording matched |
| Report acceptance-content audit | 0 | 17 required sections, five architectures, nine primary-source domains, explicit ignored-lock policy, and all evidence labels present |
| Report UTF-8/Markdown hygiene audit | 0 | One H1, terminal newline, and no NUL, tab, or trailing-whitespace defects |
| `git diff --check` after report/logbook edits | 0 | No tracked whitespace errors |

## Primary references

Repository and run evidence is linked inline above. Current external constraints
were checked against these primary sources:

- [Python version status](https://devguide.python.org/versions/)
- [GitHub public runner specifications](https://docs.github.com/en/actions/how-tos/write-workflows/choose-where-workflows-run/choose-the-runner-for-a-job)
- [GitHub Actions limits](https://docs.github.com/en/actions/reference/limits)
- [GitHub workflow matrix and filter semantics](https://docs.github.com/en/actions/reference/workflows-and-actions/workflow-syntax)
- [GitHub self-hosted runner security](https://docs.github.com/en/actions/reference/security/secure-use)
- [pytest selection and exit codes](https://docs.pytest.org/en/stable/reference/reference.html)
- [pytest-xdist distribution modes](https://pytest-xdist.readthedocs.io/en/stable/distribution.html)
- [uv GitHub Actions integration](https://docs.astral.sh/uv/guides/integration/github/)
- [uv universal lockfile semantics](https://docs.astral.sh/uv/concepts/projects/layout/#the-lockfile)
- [Go build/test cache](https://go.dev/cmd/go/)
- [Rust platform support](https://doc.rust-lang.org/rustc/platform-support.html)
- [maturin platform support](https://www.maturin.rs/platform_support)
- [Mojo 1.0.0 release](https://github.com/modular/modular/releases/tag/max/v26.5.0),
  [requirements](https://mojolang.org/docs/requirements/), and
  [roadmap](https://mojolang.org/docs/roadmap/)

## Recommendation in one sentence

Keep the support contract; make fast feedback canonical and change-aware,
make merge correctness exhaustive and deterministically sharded, retain
nightly serial/reverse canaries, and treat caches, hardware, and language
rewrites as measured secondary levers rather than substitutes for the Windows
critical-path redesign.
