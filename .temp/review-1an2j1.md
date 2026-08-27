# TASK-260819-1an2j1 Review Verdict: changes requested

Reviewer run: RUN-260819-76717c. Verdict: `changes_requested` -> `to-dev`.
Scope reviewed: `docs/prose-style.md` (new), `CONTRIBUTING.md` (one-line pointer).
Reference: precondition resource `TASK-260819-1an2j1_prose-style.md`, spec
`TASK-260819-1an2j1_docs-refresh-spec.md`.

## What passes

`docs/prose-style.md` exists at 170 lines, under the 250-line limit. It carries
the English rules, the Russian section (инженерная проза), and the blacklist.
The writer added Bad/Good pairs to every blacklist bullet, which the source
resource did not have; that addition satisfies the "blacklist with examples"
part of the AC and is the main value added over the resource.

A diff against the precondition resource shows the transfer is otherwise
faithful. Typography audit is clean: the only em-dashes (lines 134, 162) and
the only guillemets (line 137) sit inside labeled Bad examples, which is their
intended use. No en-dashes, no ellipses, no exclamation points anywhere.

`CONTRIBUTING.md:54` adds exactly one line under `## Documentation`:
`- All documentation must follow [docs/prose-style.md](docs/prose-style.md).`
The relative link resolves from the repo root.

The factual claim added at line 45 checks out: `csk install` installs into
`.agents/skills/` (`ARCHITECTURE.md:39`, `README.md:63`).

`uv run pytest tests/test_release_contract.py` passes, 23 tests. The doc-writer
reported a full-suite run at 1418 passed / 243 skipped. Nothing in `tests/` or
`.github/workflows/` lints markdown, so this change carries no test surface of
its own.

## Blocking findings

### 1. A binding rule from the source resource was dropped, not moved

`docs/prose-style.md:38-45`. The source resource states two rules about code
blocks:

> Introduce every code or command block with a sentence that ends in a colon and
> says what the block shows. After a non-trivial block, add one sentence that
> interprets the result: what the reader should observe.

The shipped guide keeps the first rule and replaces the second with a worked
demonstration (a `csk install` block followed by an interpreting sentence). The
demonstration shows the pattern; it does not state the rule. A reader of the
guide can no longer cite "interpret the block after it" as binding, and the
downstream slop-audit task (TASK-260819-1uhs6k) has nothing to enforce it
against.

Fix: keep the demonstration and restore the dropped sentence as a rule before
the block.

### 2. A "Good" exemplar violates the guide's own pronoun rule

`docs/prose-style.md:142`:

> Good: "The installer completes in under a second. Its behavior is deterministic."

`docs/prose-style.md:51-52` states: "Use a pronoun only when the referent is in
the same sentence; otherwise repeat the term." The referent of "Its" is "The
installer" in the preceding sentence, so the exemplar breaks the rule it sits
four sections below. The AC requires that "the guide itself violates none of its
own rules", and a Good exemplar is prescriptive prose, not quoted material, so
the exemption that covers the Bad examples does not apply here.

A second problem in the same line: "completes in under a second" is an
unverified performance claim about the project, presented as model prose in a
guide whose Tone section demands flat, checkable statements. A reader copying the
pattern copies the claim.

Fix: rewrite the Good example so it repeats the term and drops the unverified
timing, for example: "The installer is deterministic. The same `Skillfile.json`
produces the same tree."

## Non-blocking, out of this task's scope

`CONTRIBUTING.md` still carries the pre-refresh language policy directly below
the new pointer: "English documents are the source of truth; Russian
translations live next to them with a `.ru.md` suffix". The docs-refresh
language policy flips the root README to Russian and removes the `.ru` internals
docs, so those bullets are now stale. `CONTRIBUTING.ru.md` also exists and did
not receive the pointer. Both fall outside this task's scope line
("CONTRIBUTING.md one-line pointer"); route them through the slop-audit task
TASK-260819-1uhs6k or a follow-up.

`LOGBOOK.md` headings use an em-dash as a date separator, including the three
entries added by this docs-refresh story. The convention predates the style
guide and applies to headings rather than prose, so it is a slop-audit call, not
a rework item here.

## Anomaly worth recording

The doc-writer run RUN-260819-7c6fa0 exited 1, but the work landed intact. The
spawn log shows the provider failed at the permissions layer, not in the work:
`cortex tool write_to_file ... is not a valid artifact path; artifacts must be in
/Users/iv/.gemini/antigravity-cli/brain/...`. Treat an agy non-zero exit on this
board as inconclusive about the work product; verify the working tree before
assuming a failed run produced nothing.

## Acceptance evidence for the commit-owning mover

Not yet acceptable. Nothing from this task should be committed until findings 1
and 2 are resolved and a second reviewer cycle accepts. `docs/prose-style.md` is
currently untracked; the `CONTRIBUTING.md` edit is unstaged. This reviewer run
supplies no `commit_ack`.
