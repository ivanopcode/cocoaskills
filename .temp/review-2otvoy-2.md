# TASK-260819-2otvoy re-review: architecture-rationale

Verdict: changes requested (route to `to-dev`).
Reviewer run: RUN-260819-68d8f4. Prior cycle: RUN-260819-1551f9.
Repo: `/Users/iv/Developer/Wildberries/cocoaskills` (uncommitted working tree).

## Prior findings: status

Five of the six blocking findings from RUN-260819-1551f9 are cleanly fixed.

1. Bullet list in Security model: fixed. `ARCHITECTURE.md:390-416` is now four
   prose paragraphs, one per threat, and the list-announcing lead-in is gone.
2. Content-hash claim: mostly fixed. Commit pinning and installed-tree hashing
   are now two separate sentences (`:112-116`) and the parallel statement at
   `:403-407` is correct. One new error was introduced; see finding 1 below.
3. "without duplicating files": fixed. `:44` now claims a single source of
   truth.
4. Protected cache "before binary execution": fixed. `:281` reads "before it
   adopts cached bytes into an active installation", which matches
   `:262-268` and `:312`.
5. Shared template: partially fixed. The trailing ", which <benefit>" clause
   and the repeated middle sentence are gone from all seven paragraphs. The
   uniform opener is not; see finding 3.
6. Line wrapping: fixed. New prose peaks at 86 characters. The pre-change
   document already carried prose at 83, 87, and 100 characters
   (`git show HEAD:ARCHITECTURE.md`), so the added prose is now narrower than
   the existing prose. Same for `SECURITY.md`.
7. Cross-link duplication and the unlabeled boundaries list: addressed.
   `### Enforced boundaries` exists at `:418`, and the second cross-link
   sentence was reworded.

## What holds

All seven rationale paragraphs sit adjacent to their mechanism: whitelist
stripped layout (`:38`), canonical `.agents/skills/` root (`:44`),
content-hashed installs (`:112`), audit gate (`:118`), fail-closed installs
(`:124`), protected build cache (`:278`), manager-owned execution (`:320`).
`## Security model` (`:390`) maps all four listed threats.
`ARCHITECTURE.ru.md` and `SECURITY.ru.md` are deleted; the only remaining
references are historical `LOGBOOK.md` prose and `.spec/docs-refresh.md`.
Both cross-links resolve to the renamed `#security-model` anchor
(`SECURITY.md:3`, `SECURITY.md:159`). Sentence-level comparison of the two
documents finds zero shared sentences. No em-dashes, en-dashes, guillemets,
filler openers, or marketing register in either file; `SECURITY.md:136-138`
even removed a pre-existing em-dash construction.
`tests/test_release_contract.py`: 23 passed.

Factual spot checks that pass: the marker filename `.csk-install.json`
(`hashing.py:24`, `installer.py:2735`); the source allowlist gating clones
(`closure.py:415`, `source_identity.py:119`); the audit gate at stage 5
(`ARCHITECTURE.md:83-85`) preceding copy and compilation; capability
declarations covering filesystem, hosts, executables, and env vars
(`audit/capabilities.py`); signature verification backing the fail-closed
claim (`_ed25519.py`, `audit_registry.py:167`); and cache protection over
ownership, POSIX mode, and Windows DACLs (`builds/cache_posix.py:896-904`,
`builds/cache_windows.py:301-303`).

## Blocking findings

### 1. Ref resolution does not happen "before fetching"

`ARCHITECTURE.md:112-116`: "Stage 3 resolves branch names and mutable tags to
exact immutable commit hashes before fetching."

The order in code is the reverse. `closure.py:226` calls `_ensure_repo`, which
clones (`:309`) or fetches (`git_ops.fetch_repo`, `closure.py:295`) first;
`git_ops.resolve_ref` runs afterwards at `closure.py:236`; the raw snapshot is
taken from the resolved commit at `closure.py:242`. `resolve_ref` itself
requires a populated local repository: it calls `ensure_git_repo` and reads
`refs/remotes/origin/<branch>` or `refs/tags/<tag>` (`git_ops.py:77-92`).
Nothing resolves a ref before a fetch.

The load-bearing property is that resolution precedes materialization, not
that it precedes the fetch. Something like "before the installer takes a raw
snapshot" states the real ordering. The parallel sentence at `:403` already
gets this right by not claiming an ordering it cannot support.

### 2. The adapter paragraph closes by restating its own opening

`ARCHITECTURE.md:44-49`. The first sentence: "CocoaSkills maintains one
canonical `.agents/skills/` root with per-agent adapters." The last sentence:
"Maintaining one canonical root with per-agent adapters keeps skill
definitions unified while delivering instructions to each supported tool
environment." The subject and object are the same words in the same order; the
only new content is "unified", which the opening "single source of truth"
already carried.

The attached prose style instruction rejects "A closing paragraph that
restates the section just written", and the prior cycle's finding 5 called out
exactly this closing-restatement shape. It survived in this paragraph. The
paragraph needs a genuine third sentence: what the adapter layer costs or
guarantees that the first sentence does not say. `:45-47` and `:160` give the
material (an adapter may mirror context by copy, and tracks managed entries per
agent directory).

### 3. Seven paragraphs still open with the same construction

Every rationale paragraph opens `To <purpose>, <subject> <verb>`: `:38`, `:44`,
`:112`, `:118`, `:124`, `:278`, `:320`. Three of the seven use "To prevent";
two of the seven continue "CocoaSkills enforces". Every second sentence is a
bare problem statement in generic present tense. The goal-first opener is
correct style on its own (the guide prescribes "Lead with the goal, then the
mechanism"), but seven consecutive instances read as a filled template rather
than as prose.

The claim in `TASK-260819-2otvoy_results.md` item 5 and `LOGBOOK.md:37`,
"Varied sentence structures across all seven rationale paragraphs to eliminate
shared template phrasing", is not accurate for the openers. Vary three or four
of them: state the mechanism first and let the goal follow, or lead with the
problem.

### 4. The whitelist rationale is inserted in front of the sentence it duplicates

`ARCHITECTURE.md:38-42` claims the stripped layout protects the agent context
window so "only required skill instructions reach the prompt context".
`ARCHITECTURE.md:51` then says "The split keeps the agent window small and
makes activation modes possible". Two adjacent paragraphs carry the same claim.

The insertion also separates `:51` from its referent. "The split" points at the
three-layer bullet list that now ends at `:36`, twelve lines and two paragraphs
earlier. Moving both new paragraphs to after `:56` restores the referent and
lets the whitelist rationale build on the existing sentence instead of
pre-empting it.

## Non-blocking

`ARCHITECTURE.md:3` and `ARCHITECTURE.md:392-393` still carry the same content
in two voices: "For vulnerability reporting procedures and platform hardening
checklists, see SECURITY.md" and "Vulnerability reporting procedures and
platform hardening checklists are detailed in SECURITY.md". The reword
satisfies the letter of the prior finding, but the second one is an agentless
passive where the style guide asks for a named actor, and the content is still
redundant with the header link. Consider dropping the second or pointing it at
something the header does not cover.

`ARCHITECTURE.md:420`, "System integrity relies on enforcing these boundaries
across all operations:", is now a pure list announcer sitting directly under
the `### Enforced boundaries` heading it repeats. The heading does the work.

`ARCHITECTURE.md:321-322`, "Third-party build scripts and custom Makefiles
execute arbitrary code during compilation", is in the indicative and reads for
a moment as a claim about the CocoaSkills pipeline, which the next sentence
contradicts. The other problem statements avoid this (`:126` uses "would
allow"). A conditional or a "in a conventional build" qualifier fixes it.

## Fix list for the next producer

1. `ARCHITECTURE.md:114`: replace "before fetching" with the real ordering
   (resolution follows the clone or fetch and precedes the raw snapshot).
2. `ARCHITECTURE.md:47-49`: replace the closing restatement with a sentence
   that adds information.
3. Vary the opener in three or four of the seven rationale paragraphs
   (`:38`, `:44`, `:112`, `:118`, `:124`, `:278`, `:320`).
4. Move the two paragraphs at `:38-49` below the paragraph at `:51-56`, or
   rewrite `:38-42` so it does not pre-state "the split keeps the agent window
   small".
5. Optional: the three non-blocking items above.
6. Correct the "varied sentence structures" claim in
   `TASK-260819-2otvoy_results.md` and `LOGBOOK.md:37` to match what shipped.

## Verification commands used

    git diff HEAD -- ARCHITECTURE.md SECURITY.md
    git show HEAD:ARCHITECTURE.md | awk 'length>80 && $0 !~ /^\|/ {print NR": "length}'
    awk 'length>80 {print NR": "length}' ARCHITECTURE.md SECURITY.md
    grep -rn "ARCHITECTURE.ru.md\|SECURITY.ru.md\|security-boundaries" --include="*.md" --include="*.html" --include="*.toml" --include="*.py" .
    grep -n "—\|–\|«\|»" ARCHITECTURE.md SECURITY.md
    grep -niE "let's|dive in|powerful|seamless|robust|battle-tested|not just another" ARCHITECTURE.md SECURITY.md
    python3 <sentence-overlap script>   # 0 shared sentences
    sed -n '200,315p' src/csk/closure.py ; sed -n '60,140p' src/csk/git_ops.py
    .venv/bin/python -m pytest tests/test_release_contract.py -q   # 23 passed
