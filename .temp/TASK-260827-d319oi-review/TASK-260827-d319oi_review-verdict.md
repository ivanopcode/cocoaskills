# TASK-260827-d319oi — reviewer verdict: BLOCKED (stop-the-line)

Verdict: **blocked**, on a concrete integration-binding blocker that no producer
can clear from inside the assigned worktree.

**The engineering work is correct and independently verified.** It is blocked on
*where it lives*, not on *what it does*. Nothing below asks for the pin move to
be redone.

---

## 1. The blocker: the Change Request is bound to the wrong repository

`CR-TASK-260827-d319oi-1` reports `Repository delta: empty`. That is not a
producer omission — it is structurally guaranteed.

| Fact | Value |
| --- | --- |
| Story worktree | `.temp/STORY-260824-3rzqxr/worktree` |
| Worktree's git common dir | `/Users/iv/Developer/Wildberries/cocoaskills-taskboard/.git` |
| Worktree's origin | `git@github.com:ivanopcode/cocoaskills-taskboard.git` |
| Task scope files (`src/csk/…`, `tests/…`) | **do not exist** in that worktree |
| Repo that actually owns the scope | `/Users/iv/Developer/Wildberries/cocoaskills` |
| That repo's origin | `https://github.com/ivanopcode/cocoaskills.git` |
| No `.gitmodules`, no repo binding in `task-board.config.json` | confirmed |

Evidence:

```
$ git rev-parse --git-common-dir
/Users/iv/Developer/Wildberries/cocoaskills-taskboard/.git
$ ls src/csk
ls: src/csk: No such file or directory
$ git status --short          # story worktree
(clean)
```

The story worktree is a checkout of the **board bookkeeping repo**. The task's
scope lives in a **different repository**. A CR computed over the story worktree
can therefore only ever be empty, no matter what the producer does.

The producer's changes are real and present, uncommitted on `main` of the *csk*
repo:

```
$ git -C /Users/iv/Developer/Wildberries/cocoaskills status --short
 M .research/TASK-260803-2ol7ok_protocol-isolation-classification.json
 M LOGBOOK.md
 M src/csk/build_repository.py
 M tests/test_build_metadata.py
 M tests/test_protocol_conformance.py
 M tests/test_rc5_external_repository_conformance.py
 M tests/test_schema_v7_repository.py
```

## 2. Why not `accepted`, and why not `to-dev`

- **Not `accepted`.** The CR guidance says accepting requires stating why *no
  repository change* was the right outcome. It was not: this task's entire
  deliverable is a file change. Accepting parks a base-identical tree as the
  accepted revision; the orchestrator would checkpoint/integrate nothing while
  the real, correct work sits uncommitted in another repo's `main` working tree.
  The board would record a shipped rc.10 acceptance that no commit backs. That
  is precisely the silent-loss failure mode `TASK-260827-d319oi_tooling-note.md`
  was written about.
- **Not `to-dev`.** A respawned producer lands in the same taskboard worktree,
  where `src/csk/build_repository.py` does not exist. It cannot do the work
  there, and re-editing the csk repo out of band reproduces this same empty CR.
  That is an unbreakable loop at the producer's authority level, not ordinary
  rework.

## 3. The exact decision needed

How STORY-260824-3rzqxr's integration scope binds to a repository. Concretely,
one of:

- **(a)** Rebind the story worktree/integration scope to the `cocoaskills` repo
  and recompute the CR there against base `9164604`. *Recommended* — the delta
  is already complete and verified; this needs no code work, only a correct
  binding.
- **(b)** Orchestrator captures the existing csk working-tree delta directly as
  the accepted artifact (out-of-worktree checkpoint), and records that this
  story's leaves target `cocoaskills`, not the board repo.
- **(c)** Declare the board-repo worktree correct and restate what this leaf was
  supposed to change inside it — which contradicts the task scope as written.

This is an ownership/orchestration decision, not an implementation choice.

---

## 4. Work verification — all AC met on the csk delta

Every ground-truth fact re-derived by me from the local spec checkout at
`/Users/iv/Developer/ReluxWorks/curator-spec`, HEAD `b8b03d5`:

```
$ git tag --points-at HEAD                     -> v1.0.0-rc.10
$ shasum -a 256 conformance/v1/manifest.json
803918bf8672f76cf990985e51db213b826674cd5bb54fbf47731b8404b44403
$ git diff --stat v1.0.0-rc.9 b8b03d5
 profiles/manager.md | 26 ++++++++++++++++++++------
$ grep protocol_version conformance/v1/manifest.json  -> "1.0.0-rc.9"
$ ls release/                                  -> rc.5 rc.6 rc.7 rc.8 rc.9 (no rc.10)
```

### The central judgement call is right

The task warned: *do not blind-replace strings*. The producer did not. It found
that **rc.10 republishes the rc.9 corpus byte for byte** (#22 touched
`profiles/manager.md` only), so the accepted revision (`1.0.0-rc.10`) and the
corpus identity (`1.0.0-rc.9`) are two different facts. It split them into
`PROTOCOL_VERSION` and a new `CONFORMANCE_CORPUS_PROTOCOL_VERSION`, and asserts
both *plus* their inequality, so a future pin move that collapses the two goes
red instead of drifting. Verified correct against the corpus.

A blind replace would also have corrupted
`tests/test_protocol_conformance.py:984`. That line asserts the claim-v3 schema
const is `1.0.0-rc.5` — and at rc.10 it genuinely still is:

```
schemas/v1/conformance-claim-v1..v5 protocol_version consts:
  rc.3, rc.4, rc.5, rc.8, rc.9
```

Correctly left alone. Likewise `tests/test_rc5_external_repository_conformance.py`
keeps `PROTOCOL = "1.0.0-rc.5"` because its own pinned digest authenticates bytes
that declare rc.5 — advancing it would assert what the pinned bytes contradict.

### Corpus-derived counts — all three verified exactly

| Assertion | Claimed | Counted in rc.10 corpus | Match |
| --- | ---: | ---: | :---: |
| `test_released_accepted_schema_cases` case_count | 103 | 74 v7 + 13 skill-build-v1 + 16 skillfile-dev-v2 = 103 | yes |
| `..._schemas_1_through_6_...` case_count | 144 | 12 suites x (1 valid + 7 invalid-v7 + 4 invalid-v8) = 144 | yes |
| `..._schemas_1_through_6_...` rejected | 132 | 144 - 12 valid = 132 | yes |
| claim versions v1..v5 pairs | rc.3/rc.4/rc.5/rc.8/rc.9 | identical | yes |

### No fail-closed check weakened — the opposite, proven by negative evidence

The one deletion is the module-level `pytest.skip` in
`tests/test_protocol_conformance.py` that routed an rc.5 root past the **entire**
conformance module. Removing it means a wrong root now hits the digest assert in
`_root()`. I proved this bites by pointing the env at a real rc.5 corpus
(`git archive v1.0.0-rc.5`, digest `b6f56aac…`) — read-only, no repo mutation:

```
### NEGATIVE: rc.5 root against conformance module (must FAIL, not skip) ###
    assert digest == EXPECTED_CANDIDATE_MANIFEST_SHA256
E   AssertionError: assert 'sha256:b6f56aacc0e37dcc6692f73f641bff761e89b645adfe20a47a06d81c6fda204c'
              == 'sha256:803918bf8672f76cf990985e51db213b826674cd5bb54fbf47731b8404b44403'
ERROR tests/test_protocol_conformance.py - AssertionError: assert 'sha256:b6f...
!!!!!!!!!!!!!!!!!!!! Interrupted: 1 error during collection !!!!!!!!!!!!!!!!!!!!
1 error in 0.24s

### NEGATIVE: rc.5 root against schema_v7 corpus test (must FAIL) ###
FAILED tests/test_schema_v7_repository.py::test_released_accepted_schema_cases
1 failed, 42 deselected, 28 warnings in 0.19s
```

Before the change that first case was a silent whole-module skip. It is now a
loud collection error. Strictly stronger.

### Test runs — with the corpus envs actually SET

The orchestrator's full-suite run (1797 passed / 245 skipped) exercises these
corpus tests only as *skips* unless the env vars are set. I re-ran them bound to
the real rc.10 corpus so the assertions genuinely executed:

```
$ CURATOR_CONFORMANCE_ROOT=…/curator-spec/conformance/v1 \
  CURATOR_SCHEMA_V7_ROOT=…/curator-spec/conformance/v1 \
  .venv/bin/pytest tests/test_schema_v7_repository.py \
      tests/test_rc5_external_repository_conformance.py -q -p no:randomly
43 passed, 5 skipped, 28 warnings in 0.42s

$ … -k "accepted_contract_pin or released_accepted_schema_cases or schemas_1_through_6"
3 passed, 40 deselected, 28 warnings in 0.20s     # executed, not skipped

$ … tests/test_protocol_conformance.py -k "claim_versions_remain_separate or claim_v3_schema_stays"
2 passed, 1051 deselected, 28 warnings in 0.18s

$ … tests/test_build_metadata.py -k "manifest or sha or protocol or marker"
30 passed, 30 deselected, 28 warnings in 0.22s
```

Full-suite line accepted from the orchestrator's run on this exact tree, not
re-run by me (per `TASK-260827-d319oi_review-budget.md`):

```
1797 passed, 245 skipped, 28 warnings in 444.91s (0:07:24)
```

### Sweep, ledger, docs

- Residual `b6f56aac` / `rc.5` occurrences are all prose: header comments
  describing the *retired* pin, LOGBOOK history, the research ledger, plus the
  two corpus-accurate assertions above. No live pin left at rc.5.
- Audited-hash ledger re-derived and accurate:
  recorded `666c712d…` == `shasum -a 256 tests/test_protocol_conformance.py`.
- `docs/` untouched: `git status --porcelain -- docs/` is empty.
- `.github/workflows/ci.yml` `RELEASED_SUITE_PIN` stays at `0ed5c691` (rc.9).
  Correct and not a gap: rc.10's corpus is byte-identical to rc.9's, and the
  released-suite pin is a separate fact from the accepted revision. Noted, not
  a finding.

## 5. Summary

AC satisfied on the delta as it exists in `/Users/iv/Developer/Wildberries/cocoaskills`.
If the binding decision in §3 is resolved as option (a) or (b), this work should
be accepted as-is with no further producer cycle.

Reviewer artifacts (read-only): `.temp/TASK-260827-d319oi-review/` in the csk repo.
