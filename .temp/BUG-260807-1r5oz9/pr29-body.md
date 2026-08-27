Windows external `go-repository-v1` installs from a local exact snapshot aborted with two signatures, both inside local admission.

**Case-folded snapshot order.** `build_repository_pipeline._read_tree` sorted `Path` objects, and `PurePath` compares `_parts_normcase`. That is not the identity the admitted snapshot is framed in — `git_admission._prove_repository` sorts on the UTF-8 bytes of the relative POSIX path. The orders diverge whenever a directory name is a prefix of a sibling file name (`cmd/` before `cmd.go`, on every platform), and the Windows flavour additionally lowercases each component, so the same commit framed different bytes per platform while `_validate_materialized` always compared against the one admitted expectation.

**Private root removal.** `git_admission.admit_local` used a bare `TemporaryDirectory`, so a removal failure escaped as an untyped `PermissionError` and replaced whatever admission diagnostic was propagating. That is what the reported `[WinError 5]` was: a mask. On the operator host the refusal came from a **still-live** `git cat-file --batch` holding the copied `pack-*.idx` mapped — `_ObjectReader.read` short-read its stdout pipe, so the child blocked writing into a full pipe and never exited (BUG-260806-1bwq2z). Windows has no unlink-while-open, so the delete failed.

Removal now unseals the tree, retries with a short backoff, and reports rather than raises; a removal that never succeeds surfaces as `GitAdmissionError build_repository_local_cleanup_failed`. Unsealing is required because moving off `TemporaryDirectory` — whose `_rmtree` resets permissions on refusal — exposes the `0o400`-under-`0o500` seal `_seal_object_store` leaves; that is demonstrated on POSIX and follows by construction on Windows, where the same `chmod` sets `FILE_ATTRIBUTE_READONLY`. **READONLY has not been reproduced natively here** and is not the cause of the reported signature.

## Tests

Both paths are pinned by regression tests that fail on POSIX without the fix:

- `test_materialized_order_matches_admitted_order_for_colliding_names` / `test_path_object_ordering_would_break_the_materialized_digest` — a materialized tree whose names separate under both discarded orders.
- `test_packed_local_admission_leaves_no_private_state` / `test_local_admission_cleanup_failure_is_a_typed_diagnostic` / `test_local_admission_cleanup_failure_cannot_mask_an_admission_diagnostic` / `test_local_admission_retries_a_transient_cleanup_refusal` — local admission of a packed, clone-shaped object store, which no fixture covered before. That layout is what makes `git cat-file` map a pack index inside the private root. The retry test is synthetic and says so: it pins the retry's contract, not a reproduction.

Local: 1340 passed / 243 skipped, `mypy` strict clean.

## Native Windows evidence

Verified on the reproducer host (Windows 10.0.19045.6466, Python 3.14.4, Go 1.25.5, Git 2.50.1.windows.1) with the `TASK-260806-1dyqcj` harness against `skill-bi@e9fa203d` + `bi-cli@e0f05112` from a local exact snapshot: AUDIT → INSTALL → repeat cache-hit → `status --check` → repair → remove all exit as expected, and the unreachable-network probe still fails closed. Neither reported signature appears.

## Land order

**Land this together with #31 (BUG-260806-1bwq2z).** This PR alone stops the masking but does not get Windows through admission — a branch-only wheel now fails with a typed `build_repository_incomplete_source: object reader did not terminate` instead of the bare `[WinError 5]`. #31 removes the cause. Either order, but not this one alone.
