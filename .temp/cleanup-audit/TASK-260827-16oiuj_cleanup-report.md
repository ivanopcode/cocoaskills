# CocoaSkills workspace cleanup report

Date: 2026-08-27

## Outcome

- Reduced `cocoaskills/.temp` from 6,092,992 KiB to 99,656 KiB.
- Reduced `cocoaskills-taskboard/.temp` from 1,023,552 KiB to 78,596 KiB.
- Reclaimed 6.617 GiB from the two `.temp` trees.
- Added 39.7 MiB of compressed evidence to canonical task-board resources.
- Net workspace reduction: 6.578 GiB.
- Removed 29 registered worktrees from `cocoaskills/.temp` and the single registered worktree from `cocoaskills-taskboard/.temp`.

## Preserved task evidence

The following outcome resources are attached to `TASK-260827-16oiuj` in `cocoaskills-taskboard/.task-board`:

- `TASK-260827-16oiuj_cocoaskills-temp-evidence-bundle.tar.gz`: 88 curated Markdown, patch, review, research, and result files with original relative paths.
- `TASK-260827-16oiuj_cocoaskills-run-artifacts.tar.gz`: source `spawn-runs`, `logwork`, materialized resources, and the legacy `.temp/tasks.md` plan.
- `TASK-260827-16oiuj_taskboard-temp-artifacts.tar.gz`: snapshot of task-board scratch task artifacts, prompts, logs, resources, and run metadata before cleanup; the duplicate board worktree was excluded.

Canonical task statuses, progress documents, goals, and declared resources remain in `cocoaskills-taskboard/.task-board` and were not deleted directly.

## Retained worktrees

- `BUG-260807-l2ymv3/worktree`: task is still `to-review`; worktree is clean.
- `TASK-260824-2h0vjy/cycle4`: one tracked modification remains in `src/csk/builds/go_v1.py`.
- `TASK-260824-2i5yqw/worktree`: 30 staged or unstaged changes remain.

Disposable `.venv`, `.venv314`, `.mypy_cache`, `.pytest_cache`, `build`, and `dist` directories were removed from retained worktrees only after verifying that Git tracked zero files under each target.

## Live task-board scratch state retained

The task-board `spawn-runs`, `logwork`, `prompts`, `resources`, `changerequests`, and worktree metadata directories remain because a live `spawn-runner` was using `RUN-260826-5ab6b0` during cleanup. They total approximately 76.8 MiB.

## Audit evidence

Exact removal manifests and validation logs remain in `cocoaskills/.temp/cleanup-audit/`. Key files:

- `delete-targets-01.txt`
- `taskboard-delete-targets-01.txt`
- `cache-delete-targets-01.txt`
- `worktree-dirty-audit-01.log`
- `final-audit-01.log`
- `reclaimed-space-01.log`

No pre-existing tracked or untracked changes in the main `cocoaskills` checkout were modified.
