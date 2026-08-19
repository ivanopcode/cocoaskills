# CI fast feedback

## Goal

Provide actionable pull-request feedback in minutes without reducing the full
compatibility, conformance, or platform evidence produced after merge.

## Baseline

The accepted 2026-08-19 research baseline is recorded in
`.research/260819_radical-ci-feedback-architecture.md`. The current workflow
runs the full Python 3.11-3.14 by macOS/Ubuntu/Windows matrix for every pull
request. Python 3.14 jobs additionally serialize the full protocol and Go E2E
suites. The measured median/p95 workflow wall time is 146.05/172.18 minutes;
Windows Python 3.14 is the critical path.

## Fast pull-request lane

The pull-request lane must expose one stable, fail-closed aggregate named
`CI / fast`. It must include:

- strict mypy validation;
- distribution build and metadata validation;
- ordinary tests on Python 3.14 on Ubuntu, macOS, and Windows;
- bounded and explicit pytest-xdist parallelism for ordinary tests, using
  isolated runner-temporary roots;
- deterministic protocol sentinels selected by exact node IDs or a checked-in
  manifest, not broad `-k` heuristics;
- a deterministic, small Go E2E smoke selection with the existing candidate
  checkout authentication and platform semantics;
- cancellation of superseded work.

The aggregate must run with `if: always()` and fail when any required child is
failed, cancelled, skipped unexpectedly, or missing. The expected hosted
critical path must be documented from the accepted baseline and be at most 20
minutes before hosted canary qualification.

## Full merge/main lane

Pushes to `main` must retain the current full evidence:

- ordinary tests for Python 3.11, 3.12, 3.13, and 3.14 on Ubuntu, macOS, and
  Windows;
- the full protocol conformance suite on Python 3.14 on every platform;
- the accepted Go E2E selection and artifacts on Python 3.14 on every platform;
- strict mypy and distribution build/metadata validation.

The lane must expose one stable, fail-closed aggregate named `CI / merge`.
Fast-lane work must not duplicate the full matrix on `main`.

## Safety constraints

- Do not drop a supported Python version or platform.
- Do not change product behavior, release workflows, branch protection, or
  publishing.
- Preserve pinned external repository revisions and candidate authentication.
- Preserve exact Go E2E platform markers and evidence artifacts.
- Keep test selection auditable: collect and compare exact node IDs for every
  new sentinel or smoke selection.
- Unknown or ambiguous event conditions must fail closed.

## Validation

Before handoff, validate YAML syntax and GitHub Actions expressions, collect the
ordinary/protocol/Go selections, run the new fast selections locally where the
host platform permits, and document any hosted-only validation still requiring
a pushed canary. No commit or push is part of this task.
