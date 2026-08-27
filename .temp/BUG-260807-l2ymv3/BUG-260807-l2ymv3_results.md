# BUG-260807-l2ymv3 — repair red main after integration merge

Target repository: `/Users/iv/Developer/Wildberries/cocoaskills`
Baseline: `main` @ `3f1a804` (CI run 31196596207, 6 of 14 jobs failed)

## What the red run actually contained

The task described two clusters. The run contains **three**, and two details in
the task description do not match the logs:

| Cluster | Tests | Legs that fail | Owner |
| --- | --- | --- | --- |
| A — global install | 2 in `test_global_install_selection.py` | **all four windows-latest legs** (3.11/3.12/3.13/3.14) | BUG-260807-31psuu |
| B — toolchain deadline | 3 in `test_builds_toolchain.py` | **windows-latest 3.11 and 3.12 only** | BUG-260807-3me1d5 |
| C — go:generate rejection | 2 in `test_protocol_conformance.py` | **ubuntu 3.14 and macos 3.14** | TASK-260807-zna3vh |

Corrections to the task description:

- Cluster A does **not** fail on macos-latest 3.14. That leg's ordinary suite
  passed (`1473 passed, 62 skipped`); it failed on cluster C instead.
- Cluster B does **not** fail on every windows leg — only 3.11 and 3.12. That
  split is the root cause (see below).
- The `skill.command_resolution_contract_missing` warnings in the cluster A
  stdout are a red herring. That warning is driven by SKILL.md text, fires on
  every platform, and is a warning, not an error. The real cause is one line
  further down, on **stderr**: `global: Command 'kept-tool' has no path for
  windows`.

Cluster C is outside this task's scope (`Fix only these two clusters; do not
revert unrelated integration work`) and is reported, not fixed — see the last
section.

## Reproduction on native Windows, before any change

Host `ssh win`, clean clone at `3f1a804`, git configured as CI does
(`core.autocrlf false`, `core.symlinks false`).

```
=== python 3.12 ===
FAILED tests/test_global_install_selection.py::test_global_install_only_preserves_previously_installed_skills
FAILED tests/test_global_install_selection.py::test_global_install_only_rejects_a_command_taken_by_a_retained_skill
FAILED tests/test_builds_toolchain.py::test_exhausted_fingerprint_deadline_names_the_operator_override
FAILED tests/test_builds_toolchain.py::test_a_slow_first_fingerprint_completes_once_the_operator_raises_it
FAILED tests/test_builds_toolchain.py::test_caller_deadline_overrides_an_unusably_small_operator_value
5 failed in 3.64s

=== python 3.14 ===
FAILED tests/test_global_install_selection.py::test_global_install_only_preserves_previously_installed_skills
FAILED tests/test_global_install_selection.py::test_global_install_only_rejects_a_command_taken_by_a_retained_skill
2 failed, 3 passed in 4.39s
```

Both clusters reproduced, including the 3.12-vs-3.14 split that CI shows.

## Cluster B root cause — the deadline clock, not the deadline

Measured on the Windows host itself:

```
Python 3.12.10  monotonic     = GetTickCount64()            resolution 0.015625
Python 3.12.10  perf_counter  = QueryPerformanceCounter()   resolution 1e-07
Python 3.14.4   monotonic     = QueryPerformanceCounter()   resolution 1e-07
Python 3.14.4   perf_counter  = QueryPerformanceCounter()   resolution 1e-07
```

`time.monotonic()` is not the same clock on every platform. On Windows CPython
before 3.13 it is `GetTickCount64()`, whose tick is **15.625 ms**. Two readings
taken inside one tick are equal, so `_check_deadline` computed
`t > t + 0.000001` → `False`. The three tests set the deadline to 1 µs and
fingerprint a small fake `GOROOT` that hashes well inside one tick, so the
deadline was never observed and nothing was raised. CPython 3.13 moved
`time.monotonic()` on Windows to `QueryPerformanceCounter()`, which is exactly
why 3.13 and 3.14 pass and 3.11 and 3.12 do not.

This is a real product defect, not a test artifact: on those interpreters any
`CSK_GO_FINGERPRINT_TIMEOUT` below 15.625 ms was unenforceable, and every
deadline in the module was quantised to that tick. A deadline is a refusal
boundary, and a coarse clock silently rounded it into an admission.

**Fix.** `src/csk/builds/toolchain.py` routes every deadline through one
`_elapsed()` reading `time.perf_counter()` — `QueryPerformanceCounter()` on
every supported Windows CPython, `CLOCK_MONOTONIC` elsewhere. Applied to
`_deadline`, `_check_deadline`, and the `SubprocessProbeRunner` timeout loop
(same module, same clock, same bug class). The direction of the check is
unchanged: exceeding the deadline still refuses the toolchain, never admits it.

**Regression guard.** `test_the_deadline_clock_outresolves_the_deadlines_it_enforces`
pins both halves and is drift-proof: it patches `time.perf_counter` and asserts
`_elapsed()` observes it (so reverting to `monotonic` fails on every platform),
then asserts `get_clock_info("perf_counter")` is monotonic with resolution
≤ 1 µs. The three original tests remain the Windows-specific behavioural guard.

## Cluster A root cause — the fixture, and the test was wrong

`_make_command_skill` in `tests/test_global_install_selection.py` exported a
script command declaring only `unix_path`, then asserted
`cli.main(["global", "install"]) == 0` on any host. On Windows
`shims._command_relative_path` correctly raises
`Command 'kept-tool' has no path for windows`, the install fails, and the CLI
returns 1.

The product is right and the test encoded a wrong expectation.
`docs/mvp-design.md:756-759` states the contract explicitly:

> - macOS/Linux use `unix_path`.
> - Windows uses `win_path`.
> - Missing platform path fails installation for that skill.

Every other command fixture in the suite already declares both paths
(`test_hybrid_scope.py`, `test_closure_install.py`, `test_global_install.py`,
`test_gc.py`, `test_install.py`, …). This fixture, added by BUG-260807-31psuu,
was the only one that did not — it was written and validated on macOS only.

**Fix.** Give the fixture a `win_path` and a matching `.cmd` script, and assert
the shim under the name the host actually publishes, via a local `_shim()`
helper that mirrors the established one in `tests/test_hybrid_scope.py:43`
(`.cmd` suffix on `win32`, bare name on POSIX). No assertion was weakened: the
tests still assert exit 0, still assert the collision is rejected, and still
assert the shim exists.

## Verification

Fix commit `2ade90b`, signed, pushed to
`fix/BUG-260807-l2ymv3-windows-clock-and-shims` and fetched by SHA on the
Windows host.

Native Windows, all five named tests plus the new guard:

```
=== python 3.12 ===                          === python 3.14 ===
test_global_install_only_preserves… PASSED   test_global_install_only_preserves… PASSED
test_global_install_only_rejects…   PASSED   test_global_install_only_rejects…   PASSED
test_exhausted_fingerprint_deadline PASSED   test_exhausted_fingerprint_deadline PASSED
test_a_slow_first_fingerprint…      PASSED   test_a_slow_first_fingerprint…      PASSED
test_caller_deadline_overrides…     PASSED   test_caller_deadline_overrides…     PASSED
test_the_deadline_clock_outresolves PASSED   test_the_deadline_clock_outresolves PASSED
6 passed in 31.82s / exit 0                  6 passed in 15.29s / exit 0
```

Full ordinary suite on the same native Windows host, Python 3.12 — the leg that
carried all five failures:

```
1311 passed, 225 skipped in 932.36s (0:15:32)
=== exit 3.12 : 0 ===
```

Baseline on that host was `5 failed`. Zero failures now, no neighbour broken.

Local macOS, Python 3.14:

```
1474 passed, 62 skipped in 203.26s (0:03:23)     # was 1473; +1 is the new guard
python -m mypy  ->  Success: no issues found in 71 source files
```

Pushed to `main` as `a5dc01aa399a24b3bd26c4b1dfacc6a1215a9606`, confirmed
against `git ls-remote origin main`. Two commits: `2ade90b` (the fix, the SHA
verified on Windows) and `a5dc01a` (logbook only, no code). The logbook entry
was kept as a separate commit so the head verified on Windows stayed intact.

## CI on the pushed head

Run **31204463946** on `a5dc01a`:

| Result | Jobs |
| --- | --- |
| success | `mypy strict`; Tests 3.11/3.12/3.13 on **all three** OSes — including every windows leg that carried clusters A and B |
| failure | Tests 3.14 on macos and ubuntu — **only** in the `Run protocol conformance suite` step |

The `Run ordinary tests` step passed on **all twelve** matrix legs, windows 3.14
included. That is the decisive evidence: both clusters are gone, and the only
failing step left in the whole matrix is the one that runs cluster C.

(The windows 3.14 leg ends as `cancelled`, not `failure`: it was still inside
its ~27-minute `Run protocol conformance suite` step when a later push to
`main` cancelled the run through the workflow's `cancel-in-progress`
concurrency group. Its `Run ordinary tests` step had already passed — which is
the step this task is accountable for.)

```
$ gh run view 31204463946 --json jobs -q \
    '.jobs[] | select(.conclusion=="failure") | "\(.name)", (.steps[] |
     select(.conclusion=="failure") | "  FAILED STEP: \(.name)")'
Tests / Python 3.14 on macos-latest
  FAILED STEP: Run protocol conformance suite
Tests / Python 3.14 on ubuntu-latest
  FAILED STEP: Run protocol conformance suite
```

## Final state: main is green

Run **31212602048** on `7f04ae1` — the first head carrying all three clusters'
fixes — concluded **`success`**:

```
success  Type check / mypy strict
success  Tests / Python 3.11 on ubuntu-latest / macos-latest / windows-latest
success  Tests / Python 3.12 on ubuntu-latest / macos-latest / windows-latest
success  Tests / Python 3.13 on ubuntu-latest / macos-latest / windows-latest
success  Tests / Python 3.14 on ubuntu-latest / macos-latest / windows-latest
success  Build artifacts
```

All twelve matrix legs green, including the four windows legs that carried
clusters A and B and the three 3.14 legs that run the protocol conformance
suite and the native Go E2E selection. `Build artifacts` ran for the first time
since the breakage (it is `needs: test`, so it had been skipped).

## Cluster C — reported, not fixed by this task (out of scope)

`test_rc6_build_driver_rejection_case[attempted-go-generate]` and
`…_compares_every_observed_field[attempted-go-generate]` fail with
`DID NOT RAISE GoV1Error` on both 3.14 legs. Reproduced locally in 1.2 s:

```
CURATOR_CONFORMANCE_ROOT=<spec>/conformance/v1 \
  python -m pytest -q tests/test_protocol_conformance.py -k attempted-go-generate
2 failed, 1 passed
```

Introduced by `70e9ca2` (TASK-260807-zna3vh, "Port spec 0005 vendored Go
boundary exceptions into go-v1"), which deleted the `//go:generate` scan from
`_scan_source_directives` (`src/csk/builds/go_v1.py:1040`) along with the
`go_generator_forbidden` error, replacing it with the comment
`# //go:generate is inert`.

The conformance corpus still requires the rejection, at **both** the CI pin
(`0c81c1f8`) and at spec HEAD `dce6643c`, which is the very commit that adds
decision 0005. Decision 0005 only touched `CHANGELOG.md`,
`decisions/0005-…md` and `profiles/manager.md` — no conformance case was
removed. So bumping the CI pin does **not** fix this; verified directly.

The likely correct fix is narrowing, not restoring. Decision 0005 says:

> `//go:generate` in `GoFiles` is not a build input … The presence of the
> comment in **vendored** `GoFiles` does not fail preflight …

The relaxation is scoped to *vendored* packages. The conformance case writes
the directive into the **root** package (`root/cmd/main.go`), which the
relaxation never covered. The port went broader than the decision and dropped
the check entirely instead of gating it on vendored provenance — the same shape
as the `//go:cgo_import_dynamic` allowlist right above it
(`_allows_cgo_import_dynamic`, `go_v1.py:1032`).

This is a change to another task's landed work, which this task's scope
forbids. Recommendation: reopen TASK-260807-zna3vh (or open a new bug under
STORY-260807-1ubndo) to gate `//go:generate` on vendored provenance and restore
`go_generator_forbidden` for non-vendored `GoFiles`.

**Update.** While this task's CI run was still finishing, another session
pushed exactly that fix on top: `15537bb fix: scope the go:generate exception
to vendored packages` and `7f04ae1 docs(logbook): record the go:generate
scoping regression`. `main` is now `7f04ae1`, with both of this task's commits
intact in its ancestry (`2ade90b`, `a5dc01a`, verified with
`git merge-base --is-ancestor`). That push cancelled run 31204463946 through
the workflow's `cancel-in-progress` concurrency group, which is why its windows
3.14 leg reads `cancelled` rather than `failure` — its ordinary-tests step had
already passed before the cancel. Run **31212602048** on `7f04ae1` is the first
run to carry all three clusters' fixes together.

**Consequence for this task's AC:** clusters A and B were fixed and verified
within scope; cluster C was diagnosed here and fixed by another session on top,
along the exact line recommended above. The AC "CI on main is green" is met at
`7f04ae1` — see "Final state" — but it took both pieces of work, not this task
alone.

## One more thing the red run hides

On windows-latest 3.14 the ordinary suite failed, so every step after it was
skipped: `Run protocol conformance suite`, `Collect accepted Go E2E node IDs`,
and `Run accepted Go E2E selection` never executed at `3f1a804`. On ubuntu and
macos 3.14 the conformance step failed, so the two Go E2E steps never executed
there either. Those steps last ran green at `b04a896` (run 31078339926),
before the `go_v1` port landed. Run 31212602048 is the first since then to
execute all of them, and all three legs passed — so the Go E2E surface is
confirmed healthy at `7f04ae1`, not merely untested.
