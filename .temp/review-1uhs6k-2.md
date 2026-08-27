# TASK-260819-1uhs6k slop-audit: reviewer verdict (cycle 2)

Verdict: **changes requested** -> `to-dev`.
Reviewer: reviewer (claude), run RUN-260819-786c62, 2026-08-19.
Reviewed tree: uncommitted working tree of
`/Users/iv/Developer/Wildberries/cocoaskills` at base commit `b1e05cd`.

The rework closed the evidence-quality gap. Every sweep in
`TASK-260819-1uhs6k_results.md` now reproduces byte for byte on my run, and
the reported test line matches mine. What blocks acceptance is that the
producer fixed the board resource and left the same discredited claims in
`LOGBOOK.md`, which is a committed repository artifact; that the new Russian
CONTRIBUTING bullet asserts a relationship between the two READMEs that the
tree contradicts; and that a punctuation defect explicitly deferred to this
task by an upstream reviewer was never addressed.

Do not re-edit prose that already passes. The three fixes below are the whole
scope of the next cycle.

## What passes (independently re-verified)

Cycle 1's fix at `docs/skill-authoring.md:437` holds. `git diff
docs/skill-authoring.md` shows the em-dash replaced by parentheses, two lines,
no semantic drift.

Every sweep reproduces. Running the exact commands from the outcome resource:

```
$ grep -n -H -- '[—–]' README.md README.en.md ARCHITECTURE.md SECURITY.md CONTRIBUTING.md CONTRIBUTING.ru.md docs/skill-authoring.md docs/prose-style.md
docs/prose-style.md:137:  Bad: "csk — a skill manager".
docs/prose-style.md:165:> CocoaSkills is not just another package manager — it's a powerful,

$ grep -n -H -- '[«»]' README.md README.en.md ARCHITECTURE.md SECURITY.md CONTRIBUTING.md CONTRIBUTING.ru.md docs/skill-authoring.md docs/prose-style.md
docs/prose-style.md:140:  Bad: «Skillfile».
```

The marketing, filler-opener, antithesis, and summary-closer sweeps likewise
return hits only inside `docs/prose-style.md` lines 91-92 and 131-167, which
are the rule text and the worked contrast. The resource's observed counts (2
dashes, 1 guillemet, all inside Bad examples) are correct as stated. Finding 3
from RUN-260819-3e9b0c is resolved for the board resource, and the grep versus
reading breakdown demanded by Finding 4 is present.

Tests are green and the reported figure reproduces. My run of
`.venv/bin/python -m pytest -q` reports
`1418 passed, 243 skipped, 24 warnings in 203.75s (0:03:23)`, exit 0, against
the resource's `1418 passed, 243 skipped, 24 warnings in 206.88s`.

The CONTRIBUTING language policy is now correct on substance and the two files
agree with each other on the source-of-truth rule. `CONTRIBUTING.md:56` and
`CONTRIBUTING.ru.md:59` state the same thing, and the rule they state is true
of the tree: `ARCHITECTURE.ru.md` and `SECURITY.ru.md` are deleted, and the two
surviving Russian translations, `docs/v0.9-design.ru.md:7` and
`docs/v0.11-design.ru.md:7`, both carry a header pointing at the English
original, so the "where one exists" clause holds. `CONTRIBUTING.md:54` and
`CONTRIBUTING.ru.md:57` both carry the `docs/prose-style.md` pointer, closing
Finding 2 from the previous cycle. The first-screen cross-link claim is true:
`README.md:8` links `README.en.md` and `README.en.md:8` links `README.md`.

Scope was respected. `git diff --stat` shows this cycle touched only
`CONTRIBUTING.md` and `CONTRIBUTING.ru.md` (8 lines each); no prose outside the
two CONTRIBUTING files changed.

## Finding 1: LOGBOOK.md still carries the evidence the last review rejected

`LOGBOOK.md:1604` records "Verified test suite clean (1532 passed, 23 release
contract tests passed)" and "Ran automated grep sweeps confirming zero
remaining em-dashes, en-dashes, Russian guillemets, filler openers, marketing
hype, or antithesis constructions across shipped docs".

Both claims are the ones RUN-260819-3e9b0c rejected. The suite reports 1418
passed and 243 skipped, not 1532 passed. The sweeps return 2 dash hits and 1
guillemet hit, not zero; the correct statement is zero hits outside the style
guide's own Bad examples. The producer corrected these in
`TASK-260819-1uhs6k_results.md` and left them standing in `LOGBOOK.md`, which
is tracked and will be committed. The board resource is working state; the
logbook is the durable record, so the wrong number is the version that
survives.

The same entry is also stale on scope. It lists seven audited documents and
omits `CONTRIBUTING.ru.md`, which cycle 1 missed and cycle 2 added, and it says
nothing about the language-policy correction in `CONTRIBUTING.md` and
`CONTRIBUTING.ru.md` that is the entire substance of this cycle.

Fix: rewrite the `LOGBOOK.md:1602` entry. Use the observed test line
(`1418 passed, 243 skipped`), state sweep results as zero hits outside
`docs/prose-style.md` Bad examples, list `CONTRIBUTING.ru.md` in the audited
set, and record the language-policy correction as the cycle-2 change. Keep the
existing heading style; em-dash headings are the file's own convention (53 of
55 entries) and `LOGBOOK.md` is not in this task's audited document set.

## Finding 2: CONTRIBUTING.ru.md calls README.en.md a copy, and it is not

`CONTRIBUTING.ru.md:58` reads:

```
- `README.md` написан на русском языке, `README.en.md` поставляет его английскую копию; каждый файл ссылается на другой на первом экране.
```

`README.en.md` is not a copy of `README.md`. `wc -l` gives 134 lines against
435. `README.md` carries six sections; `README.en.md` carries twenty, adding
the install matrix, skill dependencies, global skills and selective
operations, command manifests, compiled commands, the audit registry, the CLI
reference, and the development and documentation indexes. That asymmetry is
deliberate: `.spec/docs-refresh.md` moves the reference material out of the
Russian README, and `README.md:130` itself describes `README.en.md` as "полная
англоязычная документация, справочник CLI-команд, матрица параметров и
спецификация манифестов". So a shipped document now contradicts another
shipped document in the same audited set.

The practical consequence is a wrong instruction: a contributor following
`CONTRIBUTING.ru.md:58` would either mirror the CLI reference back into
`README.md` or strip it out of `README.en.md`. Separately, "поставляет его
английскую копию" reads as a machine rendering of "is its English parity
file", which `docs/prose-style.md` names directly: если фраза читается как
перевод, перепишите её как самостоятельное русское предложение.

`CONTRIBUTING.md:55` has the milder version of the same defect. "its English
parity file" matches the spec's own wording, so it is defensible, but it
invites the same misreading, and the rework exit criterion is that the two
files agree.

Fix: state the actual relationship in both files. The English README covers
everything the Russian one does and additionally carries the reference
material the root README does not; each file links the other in the first
screen. Write the Russian as a native sentence, not as a rendering of the
English one.

## Finding 3: the punctuation defect deferred to this task was never fixed

`LOGBOOK.md:199`, written by an upstream reviewer in this same epic, says:
"NOTE: `README.md:16` is missing the comma closing the деепричастный оборот
before 'и удаляет устаревшие файлы'. Punctuation nit, deferred to the
`slop-audit` task."

It is still there. `README.md:16` ends:

```
..., исключая `tests`, `README`, файлы сборки и метаданные git и удаляет устаревшие файлы при обновлении состава скиллов.
```

The оборот "исключая ... метаданные git" is not closed, so "и удаляет
устаревшие файлы" parses as a fourth member of the exclusion list rather than
as the third homogeneous predicate alongside "фиксирует" and "копирует". The
English counterpart at `README.en.md:16` punctuates it correctly ("excludes
non-skill files (...), and removes stale files when skill selections change"),
so this is a parity defect as well as a grammar one.

Fix: add the comma before "и удаляет". One character; no other change to the
sentence.

## Non-blocking notes

`CONTRIBUTING.md` lines 55, 56, and 58 are 115, 199, and 97 characters in a
file where every other line is at most 83. Line 58 was also reflowed from two
wrapped lines into one without any content change. I am not rejecting on this:
`README.md` and `README.en.md`, both produced by this epic, use unwrapped
paragraphs throughout, so hard wrapping is not a convention the refresh
enforces. If the next cycle touches these bullets anyway, wrapping them to the
file's own width is the cheap consistent choice.

The outcome resource's fix log cites `CONTRIBUTING.ru.md | 54-58`. The bullets
are at 57-61. Off by three, worth correcting while the resource is being
updated.

`docs/v0.9-design.ru.md` and `docs/v0.11-design.ru.md` survive and are linked
from `docs/index.html:62,64`. `.spec/docs-refresh.md:28` says `docs/*` is
English only while naming only the two internals `.ru` files for removal.
Whether to remove these two RFC translations is an epic-level call, not this
task's. The CONTRIBUTING rule as now written is true of them either way.

## Rework exit criteria

`LOGBOOK.md:1602` states the observed test numbers, the correct sweep
phrasing, the full eight-document audited set, and the cycle-2 CONTRIBUTING
change. `CONTRIBUTING.md` and `CONTRIBUTING.ru.md` describe `README.en.md`
accurately and agree with `README.md:130`. `README.md:16` closes the
деепричастный оборот. The outcome resource is updated with the corrected
`CONTRIBUTING.ru.md` line range and the three fixes. Tests stay green. Nothing
else changes.
