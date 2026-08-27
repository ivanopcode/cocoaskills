# BUG-260731-1rldqv — evidence

Repository: `ivanopcode/cocoaskills`
Branch: `task/TASK-260720-3t8nr3-transactional-project-hybrid` (PR 16)
Fix commit: `7a66c73` `fix(installer): make windows project installs transactional`
(signed, `Good "git" signature for oparin@me.com`, ECDSA
`SHA256:V6JiKG7J29mjsvikcLoSVp0bLa77VTsFy12gnLO81cM`), parent `8a02e17`.

## Changed files

```
 LOGBOOK.md                        |  38 ++++++
 src/csk/adapters.py               |   5 +-
 src/csk/builds/cache.py           |  61 +++++++++
 src/csk/builds/cache_windows.py   |  73 ++++++++++
 src/csk/config.py                 |   7 +-
 src/csk/installer.py              |   4 +
 src/csk/locking.py                |  27 +++-
 src/csk/transactions.py           |  44 ++++--
 tests/conftest.py                 |   7 +-
 tests/test_build_cache_posix.py   |  18 +++
 tests/test_build_cache_windows.py |  55 ++++++++
 tests/test_install.py             |  40 ++++--
 tests/test_transactions.py        |  88 +++++++++++++
```

## New regression tests

| Test | Guards |
| --- | --- |
| `test_transactions.py::test_command_shim_tree_digests_and_commits_without_spurious_change` | digests and commits a tree holding `.cmd`, `.exe`, `.bat`, `.com` and an extensionless shim |
| `test_transactions.py::test_shim_digest_survives_the_staging_name_that_hides_its_extension` | identical bytes digest identically under a sidecar name and a `.cmd` name |
| `test_transactions.py::test_entry_target_commits_a_link_that_only_resolves_once_it_is_live` | a staged link whose destination is dangling commits as a traversable directory link |
| `test_build_cache_windows.py::test_windows_manager_makes_its_own_build_artifact_publishable` | a manager-built artifact is stamped manager-owned with a protected DACL and publishes |
| `test_build_cache_posix.py::test_posix_publication_privacy_removes_shared_write_without_touching_owner` | POSIX privacy step clears shared write bits and leaves ownership alone |

## Commands run

All commands were run standalone and their real exit codes are reported.

### Local — macOS 15 (darwin 25.5.0), Python 3.14.4

| Command | Result | Exit |
| --- | --- | --- |
| `python -m mypy` (strict, 67 source files) | `Success: no issues found in 67 source files` | 0 |
| `python -m pytest -q` (full suite, before the fixes, src changes only) | `1136 passed, 99 skipped in 1420.27s` | 0 |
| `python -m pytest -q` (full suite, all fixes and new tests) | `1140 passed, 100 skipped in 1508.52s` | 0 |
| `python -m pytest -q tests/test_locking.py tests/test_config.py tests/test_build_cache_posix.py tests/test_install.py` | `166 passed, 4 skipped in 854.22s` | 0 |
| `python -m pytest -q tests/test_install.py -k schema_v6` | `4 passed, 54 deselected in 89.96s` | 0 |

Logs: `.temp/BUG-260731-1rldqv/logs/macos-full-01.log`, `macos-full-02.log`.

The +4 passed / +1 skipped delta between the two full runs is exactly the five
new tests, one of which is Windows-only.

### windows-latest probe jobs

These ran on scratch branches (`debug/BUG-260731-1rldqv-win-probe`,
`debug/BUG-260731-1rldqv-win-diag`), both deleted after use. Neither probe
branch content is part of the fix commit.

| Run | Content | Result |
| --- | --- | --- |
| `30616637832` job `91111242181` | causes 1 and 2 fixed only | `4 failed, 6 passed, 2 skipped` — all of `test_activation_modes.py` green, the 4 remaining failures are the publication-owner family |
| `30618167325` job `91116128584` | + publication-source privacy | publication-source error gone; failure moves one layer earlier to `cache_boundary_untrusted: manager home owner does not match…` |
| `30619800192` | + manager-home provisioning | both ownership errors gone; failure moves to `Command 'build-tool' receipt target 'linux' does not match windows activation`, i.e. the non-host-faithful stub |
| `30620126160` | complete fix set | **`253 passed, 55 skipped in 4030.63s`, conclusion `success`** over `test_install.py`, `test_status.py`, `test_gc.py`, `test_build_cache_windows.py`, `test_build_cache_posix.py`, `test_transactions.py`, `test_locking.py`, `test_adapters.py`, `test_config.py` |

The staged sequence is itself evidence: each fix removed exactly its own
failure signature and exposed the next one, with no signature reappearing.

Raw probe measurements from job `91111242181` are quoted in
`BUG-260731-1rldqv_root-cause-and-fix.md` section 4.

### PR 16 full matrix

Run `30624304158` on `7a66c73` — 12 test cells, `mypy strict`, build.
Result recorded below once complete.

## Baseline the fix is measured against

Run `30594273278` on `8a02e17`: `windows-latest` 3.11/3.12/3.13 each
`45 failed, 1157 passed, 151 skipped`; 3.14 `34 failed, 1168 passed`. All four
`ubuntu-latest`, all four `macos-latest` and `mypy strict` green. Job logs are
archived in `.temp/BUG-260731-1rldqv/logs/job-9104311743{5,71}.log` and
`job-910431175{03,38}.log`.

The 3.14 cell differs from 3.11–3.13 by exactly the twelve `WinError 5`
failures, which on 3.14 become one `assert False` instead. That is the same
untraversable reparse point reported differently, because `Path.exists()` on
3.14 returns `False` where 3.11 propagates the `PermissionError` — independent
confirmation of root cause 2.
