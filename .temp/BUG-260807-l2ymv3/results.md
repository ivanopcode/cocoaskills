# BUG-260807-l2ymv3 — repair red main after integration merge

Target repository: `/Users/iv/Developer/Wildberries/cocoaskills` (github.com/ivanopcode/cocoaskills).
Red baseline: `main` at `3f1a804`, CI run `31196596207`, 6 of 14 jobs failed.

## Failure map at the baseline

The task named two clusters. The run actually carries **three**, and the
distribution of the two named ones differs from the description. Corrected map,
read from the job logs:

| Cluster | Tests | Legs that fail |
|---|---|---|
| A — global install selection | `test_global_install_only_preserves_previously_installed_skills`, `test_global_install_only_rejects_a_command_taken_by_a_retained_skill` | windows-latest 3.11, 3.12, 3.13, 3.14 |
| B — toolchain fingerprint deadline | `test_exhausted_fingerprint_deadline_names_the_operator_override`, `test_a_slow_first_fingerprint_completes_once_the_operator_raises_it`, `test_caller_deadline_overrides_an_unusably_small_operator_value` | windows-latest 3.11, 3.12 only |
| C — protocol conformance (**not in the task scope**) | `test_rc6_build_driver_rejection_case[attempted-go-generate]`, `test_rc6_build_driver_rejection_compares_every_observed_field[attempted-go-generate]` | ubuntu-latest 3.14, macos-latest 3.14 |

Two corrections to the task description:

- Cluster A does **not** fail on macos-latest 3.14. That job (`92926189611`)
  failed on cluster C. Cluster A is windows-only, on all four windows legs.
- Cluster B is windows 3.11 and 3.12 only, not every windows leg. 3.13 and
  3.14 pass it, and that split is the root cause (below).

## Reproduction on native Windows, before any change

Host `ssh win` shipped only Python 3.14, which does not reproduce cluster B, so
Python 3.12.10 was installed from python.org (`/quiet InstallAllUsers=0
PrependPath=0`, exit 0) to cover an affected leg. Sources for `3f1a804` were
staged at `C:\Users\admin\BUG-260807-l2ymv3` with venvs for both versions.

```
===== python 312 =====
FAILED tests/test_global_install_selection.py::test_global_install_only_preserves_previously_installed_skills
FAILED tests/test_global_install_selection.py::test_global_install_only_rejects_a_command_taken_by_a_retained_skill
FAILED tests/test_builds_toolchain.py::test_exhausted_fingerprint_deadline_names_the_operator_override
FAILED tests/test_builds_toolchain.py::test_a_slow_first_fingerprint_completes_once_the_operator_raises_it
FAILED tests/test_builds_toolchain.py::test_caller_deadline_overrides_an_unusably_small_operator_value
5 failed in 8.58s

===== python 314 =====
FAILED tests/test_global_install_selection.py::test_global_install_only_preserves_previously_installed_skills
FAILED tests/test_global_install_selection.py::test_global_install_only_rejects_a_command_taken_by_a_retained_skill
2 failed, 3 passed in 7.41s
```

Both clusters reproduce natively and match CI exactly, including the 3.12/3.14
split on cluster B.

## Cluster A — root cause

Not a product defect. `cli.main(["global", "install"])` returns 1 because the
installer correctly refuses the fixture:

```
global: Command 'kept-tool' has no path for windows
```

`tests/test_global_install_selection.py::_make_command_skill` declared a
`script` command carrying only `unix_path`. `shims._command_relative_path`
(`src/csk/shims.py:718-723`) raises `ShimError` when the host platform has no
path for a command, and `skillspec.py:230` shows a schema-2+ command may
legitimately declare `unix_path` **or** `win_path` — a POSIX-only command skill
is a valid authoring choice, and refusing to install it on Windows is the
designed behaviour, not a bug to fix.

The fixture was the thing that was wrong: it asked a POSIX-only skill to install
on every platform. Every other command-skill fixture in the suite already
declares both paths and ships both payloads — `test_closure_install.py:33-48`,
`test_hybrid_scope.py:16-31`, `test_global_install.py`, `test_gc.py`,
`test_activation_modes.py`, `test_audit_cli.py`, `test_skillcheck.py`.

The `skill.command_resolution_contract_missing` warnings in the captured stdout
are unrelated noise. They are warnings, they appear on the green POSIX legs
too, and they never affect the exit code.

Fix — `tests/test_global_install_selection.py`:

- `_make_command_skill` now declares `win_path` and writes the `.cmd` payload,
  matching the rest of the suite.
- the second-version `skill-target` manifest inside
  `test_global_install_only_rejects_a_command_taken_by_a_retained_skill` gets
  the same treatment, so the collision is reached on Windows instead of a
  "no path for windows" refusal.
- three shim assertions moved to a new `_global_shim` helper that appends the
  `.cmd` suffix on Windows, because the published Windows shim is
  `kept-tool.cmd`, not `kept-tool`.

## Cluster B — root cause

A clock-resolution defect in `src/csk/builds/toolchain.py`, fixed at the root.

`_deadline` built `time.monotonic() + timeout` and `_check_deadline` tested
`time.monotonic() > deadline`. `time.monotonic()` is not the same clock on
every leg of the matrix. Measured on the Windows host:

```
py -3.12  monotonic -> GetTickCount64(),          resolution 0.015625
py -3.12  perf_counter -> QueryPerformanceCounter(), resolution 1e-07
py -3.14  monotonic -> QueryPerformanceCounter(), resolution 1e-07
py -3.14  perf_counter -> QueryPerformanceCounter(), resolution 1e-07
```

CPython switched the Windows `time.monotonic()` backend to
`QueryPerformanceCounter` in 3.13. Before that it is `GetTickCount64()`, which
advances once per 15.625 ms. With `CSK_GO_FINGERPRINT_TIMEOUT=0.000001` the
deadline lands inside the current tick, every reading taken during the pass
returns the identical value, `monotonic() > deadline` never becomes true, and
the small fixture GOROOT finishes hashing before the clock moves. The
already-exhausted deadline therefore admitted work it exists to refuse. That is
exactly why 3.11 and 3.12 fail while 3.13 and 3.14 pass.

Fix: one `_elapsed()` helper backed by `time.perf_counter()` — monotonic on
every supported platform, 100 ns on Windows for every supported CPython — used
by `_deadline`, `_check_deadline`, and the subprocess probe deadline in
`SubprocessProbeRunner`, which carried the same latent exposure (a
`process_timeout` could be up to one tick late). The direction of the check is
unchanged: exceeding the deadline still refuses the toolchain, never admits it.

### Regression test

`test_an_exhausted_deadline_is_refused_on_a_coarse_grained_clock` stubs
`toolchain.time.monotonic` to a constant, which is what a sub-tick pass
observes on Windows ≤3.12, and asserts the fingerprint pass still raises
`toolchain_timeout`. It reproduces the Windows-only defect on every platform.
Verified in both directions on macOS 3.14:

- against the fix (`perf_counter`): `1 passed`
- against the old code (`monotonic`): `1 failed — Failed: DID NOT RAISE ToolchainError`

No test was skipped and no assertion was loosened.

## Cluster C — root cause, deliberately not fixed here

`test_rc6_build_driver_rejection_case[attempted-go-generate]` and
`test_rc6_build_driver_rejection_compares_every_observed_field[attempted-go-generate]`
fail with `DID NOT RAISE GoV1Error`. It reproduces locally on macOS 3.14, so it
is not platform-dependent — the protocol-gate step simply only runs on 3.14.

`70e9ca2` (TASK-260807-zna3vh) removed the `//go:generate` scan from
`_scan_source_directives` in `src/csk/builds/go_v1.py`, implementing
curator-spec decision 0005: "`//go:generate` in `GoFiles` is not a build input
... The presence of the comment in vendored `GoFiles` does not fail preflight."
Nothing in `src/` or `tests/` raises `go_generator_forbidden` any more.

But decision 0005 (`curator-spec dce6643`) changed only `profiles/manager.md`,
`CHANGELOG.md`, `decisions/`, and a workflow — it did **not** touch
`conformance/v1/vectors/build-drivers.json`, where rejection case 42 still
reads:

```json
{ "name": "attempted-go-generate",
  "boundary": "compiler-directive",
  "expected": { "error": "go_generator_forbidden", "result": "reject", ... } }
```

So a driver that conforms to decision 0005 cannot satisfy that vector. The
curator reference implementation has the identical contradiction at `124654a`:
`internal/godriver/graph.go` treats `//go:generate` as inert
(`if matched == 2 { /* inert */ }`) while
`internal/godriver/build_conformance_test.go:512` still maps
`"attempted-go-generate": "go_generator_forbidden"`. This is an upstream spec
defect, not a cocoaskills regression.

Not fixed under this task, because every available route is out of scope:

1. Update the spec vector in `curator-spec` and bump the CI `protocol-spec`
   pin. Correct root fix, but it edits the protocol specification in another
   repository and is a spec-owner decision. The current pin `0c81c1f` is not
   even on spec `main` (it is the superseded side branch
   `task/BUG-260731-2rhy74-marker-v2-fixture`; `432eb2e` is the merged form),
   so moving it also drags in `a70965a` "Синхронизировать схему 7" and can
   shift unrelated vectors.
2. Re-add the `//go:generate` scan. Explicitly forbidden by this task's scope
   ("do not revert unrelated integration work") and contradicts the landed
   decision, which exists because real vendored skills (`x/text`, `chroma`,
   `clipperhouse/displaywidth`) carry the directive.
3. Skip or loosen the conformance test. Explicitly forbidden by the AC.

Recommendation: open a task against `curator-spec` to update rejection case
`attempted-go-generate` in `conformance/v1/vectors/build-drivers.json` to match
decision 0005 (remove it, or restate it as "package requires generated output
that is absent from vendor" with a distinct code), cut a spec revision, then
bump the `protocol-spec` ref in `.github/workflows/ci.yml`. Both implementations
need the same pin bump.

## Validation

TO_BE_FILLED

## Net effect on CI

Clusters A and B are fixed, which clears the four windows-latest legs. Cluster C
still fails the 3.14 protocol-gate step on ubuntu-latest and macos-latest, so
`main` is not fully green after this change and the AC "CI on main is green" is
not met by this push alone. Nothing about cluster C is masked or skipped.
