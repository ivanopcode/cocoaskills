# TASK-260819-1uhs6k slop-audit: reviewer verdict

Verdict: **changes requested** -> `to-dev`.
Reviewer: reviewer (claude), run RUN-260819-3e9b0c, 2026-08-19.
Reviewed tree: uncommitted working tree of
`/Users/iv/Developer/Wildberries/cocoaskills` at base commit `b1e05cd`.

The primary acceptance criterion holds: I re-ran the blacklist sweeps
independently and found zero hits outside the style guide's own Bad
examples. The rework below is about audit coverage and evidence quality,
not about rewriting prose. Do not re-edit prose that already passes.

## What passes (independently verified)

The one fix the producer applied is correct and meaning-preserving.
`docs/skill-authoring.md:437` replaced an em-dash with parentheses inside
the `curator-build-source-v1` bullet; `git diff docs/skill-authoring.md`
shows a two-line change and no semantic drift.

The punctuation sweep is clean. `grep -n -- '[—–]'` over the
seven shipped docs returns exactly two hits, `docs/prose-style.md:137` and
`docs/prose-style.md:165`; `grep -n -- '[«»]'` returns one hit,
`docs/prose-style.md:140`. All three sit inside the guide's own Bad
examples and must stay.

The phrase sweep is clean. Case-insensitive greps for marketing register
(`powerful`, `seamless`, `robust`, `blazingly`, `game-changer`,
`comprehensive`, `effortless`, and the Russian equivalents), filler
openers (`let's`, `dive in`, `in today's world`, `it should be noted`,
`stoit otmetit`, `vazhno ponimat`, `davayte razberyomsya`), summary
closers (`in summary`, `in conclusion`, `v itoge`, `podvodya itog`,
`takim obrazom`), and antithesis constructions (`not just`, `isn't just`,
`ne prosto`, `isn't about`, `not only ... but also`) return hits only in
`docs/prose-style.md` lines 131-167, which are the blacklist definition
and the worked contrast.

Tests are green. `.venv/bin/python -m pytest -q` finished
`1418 passed, 243 skipped, 24 warnings in 209.49s`, exit 0.

Cross-doc integrity holds. `pyproject.toml:9` reads
`readme = "README.en.md"`. No shipped doc, `docs/index.html`, or
`docs/sitemap.xml` references the removed `README.ru.md`,
`ARCHITECTURE.ru.md`, or `SECURITY.ru.md`. The
`ARCHITECTURE.md#security-model` anchor referenced from `SECURITY.md:4`
and `SECURITY.md:160` resolves to `ARCHITECTURE.md:387`.

Factual spot-checks against the code hold. The six agent environments
named in `README.md:10` and `README.en.md:10` match `src/csk/cli.py:300`
and `src/csk/cli.py:333`. The marker filename in `ARCHITECTURE.md:402`
matches `src/csk/install_marker.py:685` and `src/csk/gc.py:341`. The
shadowing order in `README.md:124` and `README.en.md:124` matches
`src/csk/installer.py:396` and is unchanged from the pre-refresh README.

## Finding 1: CONTRIBUTING states a language policy this epic abolished

`CONTRIBUTING.md:55` says "English documents are the source of truth;
Russian translations live next to them with a `.ru.md` suffix and a header
pointing at the original." That rule is now false for the repository's
primary entry point. `README.md` is Russian under the plain name and
`README.en.md` carries the suffix, so the English document is the one with
the suffix and the Russian one is not a translation living next to an
original. A contributor following `CONTRIBUTING.md:55` would recreate
`README.ru.md`, which this epic deleted. The rule is also false for
`ARCHITECTURE.md` and `SECURITY.md`, which are English-only by the new
policy with their `.ru.md` variants deleted.

The actual language policy exists only in `.spec/docs-refresh.md:25-40`,
which is an untracked spec file, so no shipped document records it. The
final audit's own DoD requires docs consistent with current state and no
discrepancies between description and reality, so this belongs to this
task.

Fix: rewrite the `## Documentation` bullets in `CONTRIBUTING.md:54-59` to
state the shipped policy. `README.md` is Russian and `README.en.md` is its
English parity file, each linking the other in the first screen;
`ARCHITECTURE.md`, `SECURITY.md`, and `CONTRIBUTING.md` are English and
the source of truth; a Russian translation, where one exists, uses the
`.ru.md` suffix and a header pointing at the original. Keep the existing
pointer to `docs/prose-style.md`.

## Finding 2: CONTRIBUTING.ru.md was not audited and now diverges

`CONTRIBUTING.ru.md` is tracked, shipped, and linked from
`CONTRIBUTING.md:3`, and it was not in the audited set. I swept it myself:
`grep -n -- '[—–«»]' CONTRIBUTING.ru.md` returns nothing,
so it carries no blacklist hits.

It has diverged in content. `CONTRIBUTING.md:54` gained the line
"All documentation must follow docs/prose-style.md" and
`CONTRIBUTING.ru.md:57` did not, so the translation no longer mirrors the
original, which breaks the rule stated at `CONTRIBUTING.md:57` and
`CONTRIBUTING.ru.md:59`. `CONTRIBUTING.ru.md:57-58` also repeats the stale
language policy from Finding 1 in Russian.

Fix: add the `docs/prose-style.md` pointer to `CONTRIBUTING.ru.md:57` and
apply the same language-policy correction as Finding 1, in Russian, per
the инженерная проза rules in `docs/prose-style.md`.

## Finding 3: outcome-resource evidence does not reproduce

`TASK-260819-1uhs6k_results.md` reports `.venv/bin/pytest`:
"1532 passed in 10.96s". Neither number reproduces here. My run of the
same suite reports 1418 passed and 243 skipped in 209.49s, so the reported
figure matches neither the pass count nor the collected total (1661) nor a
plausible wall time for this suite. The suite is green either way, so this
does not block on test health; it blocks because the audit's evidence
cannot be trusted as a record of what was run.

The same resource reports "Em-dashes in prose: 0 remaining hits in shipped
docs" and "Russian guillemets: 0 remaining hits in shipped docs". The
actual counts are 2 and 1 respectively, all inside the guide's Bad
examples at `docs/prose-style.md:137`, `:140`, and `:165`. The correct
statement is zero hits outside the style guide's own Bad examples. As
written, the report does not describe what the command returned, so a
reviewer cannot tell the sweep from an assertion.

The binding tooling note attached to this task requires grep or head
verification output to be included in the outcome resource. The resource
contains prose descriptions of commands ("Clean match via grep -n -C 3
...") and no command output.

Fix: rewrite the outcome resource with the literal commands and their
literal output pasted in, and state counts as observed rather than as
zero.

## Finding 4: four blacklist categories were never assessed

The report's verification sections cover dashes, guillemets, filler
openers, marketing hype, antithesis, and summary closers. Every one of
those is a fixed-string grep. The blacklist in `docs/prose-style.md:129-159`
has eight entries. Untouched are "Chains of triple enumerations and
adjective triples", "A closing paragraph that restates the section just
written" beyond the literal string "In summary", "Bullet lists that carry
reasoning instead of parallel facts", and marketing adjectives beyond the
four literals. The prose rules in `docs/prose-style.md:9-127` (voice,
paragraph shape, terminology repetition, lists versus prose) were not
assessed at all.

I read `README.md`, `README.en.md`, and the ARCHITECTURE.md rationale and
Security model additions looking for these, and did not find a violation I
would reject on. The inline enumerations throughout are two-to-four
concrete items, which `docs/prose-style.md:83` explicitly permits, and
they are pervasive in the pre-existing text as well. `ARCHITECTURE.md:417`
("CocoaSkills enforces specific security boundaries across installation,
compilation, and materialization:") is a weak lead-in where "specific"
carries nothing, but the sentence introduces a genuinely enumerable list
and I am not rejecting on it.

Fix: no prose changes required. State in the outcome resource which
categories were checked by grep, which were checked by reading, and what
was read, so the audit's coverage is legible.

## Observation, not a blocker

`docs/v0.9-design.ru.md` and `docs/v0.11-design.ru.md` survive and are
linked from `docs/v0.9-design.md:7`, `docs/v0.11-design.md:7`, and
`docs/index.html:62,64`. `.spec/docs-refresh.md:28` says `docs/*` is
English only, but the same section names only `ARCHITECTURE.ru.md` and
`SECURITY.ru.md` for removal, so these two RFC translations are a gray
area. Whether to remove them is an epic-level call, not this task's.
Whatever CONTRIBUTING.md ends up saying in Finding 1 must be true of these
two files as well.

## Anomaly for the record

The producer run reported test evidence that does not reproduce. Combined
with the tooling note on this board, which exists because an earlier run
handed off a complete checklist while its file edits were silently lost,
this is the second instance of a producer's self-reported evidence not
matching the tree. Reviewers on this board should re-run producer-claimed
commands rather than reading the claim.

## Rework exit criteria

`CONTRIBUTING.md` and `CONTRIBUTING.ru.md` state the shipped language
policy and agree with each other. The outcome resource carries literal
command output for every sweep, correct observed counts, and an explicit
statement of which blacklist categories were greppable and which were read
for. Tests stay green. No prose outside the two CONTRIBUTING files
changes.
