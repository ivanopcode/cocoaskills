# TASK-260728-3kuxg7 developer outcome

## Result

Implementation, documentation, independent rc.5 corpus consumption, local
validation, and native macOS and Windows qualification are complete. The
Windows host returned after the earlier evidence-backed outage; native defects
were repaired and the final authenticated platform gate passed.

## Exact revisions and corpus

- CocoaSkills base HEAD: `0471df5ca1f737b4212a70040583126aa4ae1ae2`
- CocoaSkills 176-file candidate content digest (sorted path plus Git blob
  identity): `ccb741ccfd163982525a86a3ef50735719173a461d6766a3d75207e1b78b4955`
- Curator reference checkout: `74fe162415d800cd0a6975313827f9dc8594d299`
- curator-spec `v1.0.0-rc.5`: `f5d7673039226ab81de2f4f87e2155ae995c4df3`
- rc.5 conformance manifest SHA-256: `b6f56aacc0e37dcc6692f73f641bff761e89b645adfe20a47a06d81c6fda204c`
- external corpus manifest SHA-256: `cc9e9c0f93b2497a060a533503a4d030d1a715fe1dd4eb8bf9820168a9257697`
- current rc.6 regression manifest SHA-256: `12e58b82579645ba1ccafba49d3e2dd3216005ddf37ae63c68a9fafd46773071`
- external corpus: 60 cases, 18 threat vectors, 12 lifecycle boundaries;
  the csk consumer imports no Curator implementation package.

## Delivered behavior

- Schema-7 external Git repository acquisition/admission is wired into project
  and global install transactions with exact revision/tag enforcement.
- Whole-snapshot validation and static audit precede cache lookup and compile.
- Receipt-v2 artifacts and marker-v3 state use protected storage, read-only
  currentness checks, corruption quarantine/repair, offline immutable snapshot
  reuse, direct managed shims, and declaration-driven uninstall.
- Existing transaction rollback/crash/collision/consumer-last behavior is
  shared with external activation; shared vectors bind all required cases.
- Documentation covers schema 7, exact identity, audit/build/install order,
  project/global activation, repair, offline behavior, and uninstall. It warns
  against script wrappers and PATH hacks. Linux is explicitly not claimed.

## Local gates

- `uv run --frozen mypy src/csk`: exit 0, 71 source files.
- Reviewer-rework focused pipeline/install suite: exit 0, 19 passed in 2.24s.
- Final authenticated rc.5/rc.6 suite after reviewer rework: exit 0, 312
  passed, 1 skipped in 75.76s.
- Status/cache regression set: exit 0, 43 passed in 25.01s.
- Final external pipeline/lifecycle set: exit 0, 18 passed in 2.25s.
- `uv run --frozen python -m build`: exit 0; sdist and wheel built.
- `uv run --frozen twine check dist/*`: exit 0, all distributions passed.
- `git diff --check`: exit 0.
- Pytest emitted pre-existing temporary-directory cleanup warnings after green
  runs; no test or command exit status was changed by those warnings.

## Native macOS (`ssh relux`)

- macOS 15.7.4, build 24G517; Darwin 24.6.0 x86_64.
- Python 3.12.13; Apple Git 2.50.1; Go 1.25.5; uv 0.11.29;
  staged csk qualification build 0.0.0rc5.
- Authenticated manifest hashes matched the pins above.
- `mypy src/csk`: exit 0, 71 source files.
- External pipeline/lifecycle/corpus subset after Darwin publication repair:
  exit 0, 23 passed in 7.98s.
- Broader schema-7, Git admission, audit/cache, marker, project/global install,
  transaction, and schemas-1-6 regression gate after reviewer rework: exit 0,
  312 passed, 1 skipped in 208.28s.
- An initial staging build without `.git` failed before tests (exit 1) because
  setuptools-scm had no version; rerun used explicit staged version 0.0.0rc5.
- An incorrectly configured broad run pointed rc.6 tests at rc.5 and was
  interrupted (exit 130). A corrected `-x` run exposed Darwin refusing rename
  of a pre-sealed directory (exit 1 after 301 passed/32 skipped). The code was
  repaired to atomically rename the private root before sealing it, then both
  native gates above passed.

## Native Windows (`ssh win`)

- Windows 10 Pro 10.0.19045.6466 (OS API 10.0.19045.0), amd64.
- Python 3.14.4; Git 2.50.1.windows.1; Go 1.25.5 windows/amd64;
  uv 0.11.29; staged csk qualification build 0.0.0rc5.
- Authenticated rc.5 external, rc.5 schema, and current schema-regression
  manifest hashes matched the pins above on the Windows filesystem.
- Initial authoritative runtime suite: exit 1, 21 failed, 273 passed, 18
  skipped in 257.86s. It exposed POSIX-only fake Git/SSH helpers, an incorrect
  sealed-cache owner requirement on fresh materialization, and readonly being
  set before the Windows DACL profile could be applied.
- Rework retained strict owner/DACL checks for protected cache entries, uses
  raw Windows handles to reject reparse/type/link attacks during fresh
  materialization, applies security before readonly state, and makes test Git
  and SSH helpers native. Focused rerun: exit 0, 46 passed in 43.31s.
- Final schema-7, Git admission, audit/cache, marker, project/global install,
  transaction, and schemas-1-6 regression gate after reviewer rework: exit 0,
  295 passed, 18 platform-appropriate skips in 266.54s.
- The native selection covers exact tag match/move/missing and inaccessible
  source; audit-before-cache/build ordering; project/global managed-PATH
  activation; protected offline reuse, corruption, quarantine and repair;
  rollback/crash/collision/uninstall; and schemas 1-6 regression behavior.
- Native mypy exited 1 with 113 platform-stub diagnostics in pre-existing
  POSIX-only modules (`fcntl`, `resource`, `os.fchmod`, and related APIs).
  This is recorded as an anomaly, not converted to a pass. The authoritative
  71-file source/lint gate passed locally and on macOS; the Windows runtime and
  DACL gates above passed independently.

The earlier host outage remains valid negative history: three `ssh win`
attempts exited 255 while Tailscale reported `mbpro-win` offline. No result was
claimed until the host returned and the native suites above ran. Linux remains
excluded and no Linux support is claimed.

## Reviewer-requested source-admission rework

The reviewer demonstrated that the acquisition boundary caught
`BaseException` and could convert `GitAdmissionError(INCOMPLETE_SOURCE)` into a
successful protected offline cache hit. `run_pipeline` now permits offline
reuse only for the production acquisition contract's explicit
`GitAdmissionError(SOURCE_UNAVAILABLE)` code. Other Git admission codes and
non-recoverable exceptions propagate unchanged, and protected snapshot-load
handling catches only the typed `ExternalBuildError` corruption boundary.

Regression coverage creates a valid protected snapshot, then raises
`INCOMPLETE_SOURCE` for a malformed fetched graph and proves that no cache hit
or additional compilation occurs. Existing genuine offline-reuse tests now use
the same typed unavailable-source condition emitted by `acquire_network`.
