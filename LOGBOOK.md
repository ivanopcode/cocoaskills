# Logbook

## 2026-07-30 — TASK-260720-2x6mjn planning boundary

Independent review found that sharing the compiled-build closure with the
existing mutating global-install loop materialized undeclared context-only
providers, published their inactive commands, and allowed same-name inactive
commands to shadow one another. Running the toolchain/cache planner on real
project and global installs also made those established install paths require
Go before any downstream code consumed the resulting plan.

The implementation now keeps validated closure, source, toolchain, and cache
planning on the read-only dry-run path. Real global materialization remains
declaration-driven, and real project/global installs do not invoke compiled
build planning. TASK-260720-3t8nr3 owns connecting these plans to compilation
and materialization.

Regression coverage asserts that context-only transitive providers never
appear in `global/skills`, `global/bin`, or runtime storage during a real
global install, and that real project/global installs succeed without Go on
`PATH`.

Dry-run build planning intentionally fails closed when the captured operator
`PATH` has no trusted Go executable. The failure aborts the whole project or
global build plan, returns no partial build rows, and preserves every watched
filesystem surface. Both scopes now pin that behavior with host-independent
tests, and the unrelated schema-v6 build-root lifecycle test uses a
deterministic fake trusted toolchain instead of inheriting a contributor
machine's Go installation.
