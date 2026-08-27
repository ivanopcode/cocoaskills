# BUG-260821-p628cq — Review verdict: ACCEPTED

Reviewer run: RUN-260822-76f3ca (reviewer archetype, read-only)
Reviewed repo: `/Users/iv/Developer/Wildberries/cocoaskills` @ `main` (HEAD `0d7806f`), working tree

## Verdict

**accepted** — the AC is met, the fix took the preferred branch, and the tap-bump
semantics are provably unchanged because `.github/workflows/release.yml` was not
touched at all.

## Change under review

Uncommitted working-tree diff, exactly two files:

- `tests/test_homebrew_bump.py` (+12/-1) — `test_release_workflow_bumps_the_tap_on_stable_tags_only`
  no longer asserts the literal substring `if: needs.build.outputs.prerelease == 'false'`.
  It extracts the job-level `if:` block, collapses whitespace, and asserts the four
  semantic pieces: `needs.build.result == 'success'`,
  `needs.publish-pypi.result == 'success'`,
  `needs.build.outputs.prerelease == 'false'`, `always()`.
- `LOGBOOK.md` (+26) — root cause / fix / decision entry.

`.github/workflows/release.yml`: **unchanged**. Confirmed via `git status` and `git diff`.

Pre-existing, NOT part of this change (mtime Aug 21, before the Aug 22 11:37 run):
`.gitignore` (+2, `Skillfile.dev.json`) and untracked `Skillfile.json`. No scope creep
from the implementer.

## AC verification

| AC | Result |
| --- | --- |
| `uv run pytest tests/test_homebrew_bump.py -q` passes | **12 passed**, exit 0 |
| bump-homebrew-tap still runs only for stable tags with prerelease-false semantics | **unchanged** — release.yml not modified |

Full suite as a regression check: `uv run pytest -q -p no:randomly` →
**1458 passed, 244 skipped, 0 failed** in 232s. Log: `.temp/BUG-260821-p628cq/full-pytest-01.log`.
Main is no longer red.

## Design review — was "update the test" the right branch?

Yes. The alternative ("restore a single-line `if:`") would have reverted a deliberate
CI fix. `release.yml` carries its own comment explaining why: `publish-testpypi` is
`if: needs.build.outputs.prerelease == 'true'`, so it is skipped on stable tags, and
`publish-pypi` (`needs: [build, publish-testpypi]`) itself already opts out with
`always()`. Reverting the bump job's guard to a bare single-line condition would have
re-coupled it to the implicit `success()` the surrounding jobs deliberately escape.
The task description named this branch as preferred; the implementer picked it and
left the workflow alone. Correct call.

Asserting on normalized condition content rather than exact formatting is also the
right shape for this test file — the neighbouring `test_homebrew_smoke_lane_waits_for_the_bump`
uses the same "extract job block by regex, then assert on contents" idiom, so this
fits the file's existing architecture rather than inventing a new one.

## Robustness — does the new test still catch real regressions?

Verified by mutating `release.yml` in memory and re-running the extraction
(`.temp/BUG-260821-p628cq/mutation-probe.py`):

| Mutation | Extracted condition | Test outcome |
| --- | --- | --- |
| current (unchanged) | `>- ${{ always() && needs.build.result == 'success' && needs.publish-pypi.result == 'success' && needs.build.outputs.prerelease == 'false' }}` | passes (correct) |
| prerelease guard dropped | `${{ always() && needs.build.result == 'success' }}` | **fails** (correct) |
| reverted to old single-line form | `needs.build.outputs.prerelease == 'false'` | **fails** on `always()` + `.result` (correct) |
| inverted to `prerelease == 'true'` | `... prerelease == 'true' }}` | **fails** (correct) |
| job-level `if:` deleted entirely | `steps.formula.outputs.changed == 'true'` (step-level `if:`) | **fails**, but on the content asserts, not the `is not None` assert |

So the test is not weakened: every semantic regression that matters is still caught.

### Minor, non-blocking observation

In the last row the regex `^\s*if:\s*(.*?)\n(?=\s*\S+:)` falls through to the
step-level `if: steps.formula.outputs.changed == 'true'` when the job-level `if:` is
absent, so the `"bump-homebrew-tap job is missing an if: condition"` message never
fires — the failure surfaces as a confusing content-assert instead. This is a
diagnostic-quality nit only; the test still fails, which is what matters. Anchoring
the search to the job header (e.g. `^    if:` at job indentation) would tighten the
message. Not worth a rework cycle; noted for whoever next touches this file.

## Lint

The repo has no ruff config in `pyproject.toml`, no ruff in `.github/workflows/*.yml`,
and `uv run ruff` is not resolvable in this environment. The implementer's note about a
pre-existing `C408` at line 109 refers to untouched code. Lint is not an enforced gate
here, so this is not a blocker.

## Handoff

Reviewer archetype does not supply `commit_ack`. The change is still uncommitted in the
working tree. The commit-owning mover should commit `tests/test_homebrew_bump.py` and
`LOGBOOK.md` (and only those — leave `.gitignore` / `Skillfile.json` out of this scope),
then make the final `done` transition with `commit_ack=scope_committed`.
