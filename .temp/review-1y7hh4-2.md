# TASK-260819-1y7hh4 Review 2: changes requested

Reviewer run: RUN-260819-04cdf8. Review date: 2026-08-19.
Working tree: /Users/iv/Developer/Wildberries/cocoaskills (uncommitted).
Previous verdict: RUN-260819-c9148b, changes requested, four findings.

## Verdict

changes_requested, routed to `to-dev`. All four findings from RUN-260819-c9148b
are fixed and independently verified. One new defect is introduced in the same
edit: two rows of the CLI reference table carry unescaped pipe characters and
render as corrupted cells on GitHub and on the PyPI page. The fix is four
escapes. Nothing else is outstanding.

## Rework findings, all verified fixed

### 1. LOGBOOK.md mangled duplicate: fixed

`LOGBOOK.md` now holds one intact entry per event. The heading
`## 2026-08-19 - TASK-260819-1y7hh4 English README written, pyproject updated`
appears exactly once (`grep -c` returns 1) at line 39, and its body carries its
code spans intact: "Created `README.en.md` in English with full content parity
to Russian `README.md`". The stripped-identifier duplicate is gone. The only
surviving occurrence of the string "Updated   field from  to ." is inside the
reviewer entry at `LOGBOOK.md:21`, where it is a deliberate quotation of the
corruption, not corruption itself. No other stripped-code-span smell in the
diff.

### 2. `csk hybrid` rows in the CLI table: fixed

`README.en.md:372-375` adds four rows. Every claim checks against the live CLI:

    .venv/bin/csk hybrid --help      -> {add,remove,list,status}
    .venv/bin/csk hybrid add --help  -> [--git GIT] (--tag | --branch | --revision)
                                        --target TARGET   name
                                        --target: project alias, absolute path,
                                        or path glob (repeatable)

The row descriptions match the argparse help strings, including the repeatable
`--target` note.

### 3. Curator Protocol pointer: fixed

`README.en.md:10` restores both halves of the old claim: the link to
`https://github.com/relux-works/curator-spec` and the qualifier that the `csk`
executable, package name, and state directory names are implementation-specific
compatibility names while portable manifest and marker names follow the shared
protocol. This matches `git show HEAD:README.md:18`.

### 4. Marketing adjective in the documentation index: fixed

`README.en.md:414` now reads "Reference documentation and technical
specifications:". A blacklist sweep over the whole file returns zero hits for
comprehensive, powerful, seamless, robust, battle-tested, blazingly.

## New finding (blocking)

### 5. Unescaped pipes break two rows of the CLI reference table

`README.en.md:372` and `README.en.md:377` each contain literal `|` characters
inside a code span, in a two-column GFM table. GFM splits table cells on `|`
before inline parsing, so a pipe inside backticks still splits the cell; the
spec requires `\|` even inside other inline spans. Both rows have more cells
than the header, so the surplus is dropped and the code span never closes.

Reproduced with the repo's own `markdown-it-py`:

    .venv/bin/python -c "from markdown_it import MarkdownIt; ..."

    <td>`csk hybrid add <name> --target &lt;alias</td>
    <td>path</td>
    <td>`csk shell-init [auto</td>
    <td>zsh</td>

The reader of the rendered page sees a command named `` `csk hybrid add <name>
--target <alias `` whose Behavior column reads `path`, and a command named
`` `csk shell-init [auto `` whose Behavior column reads `zsh`. The real
descriptions, including the note that `--target` is repeatable, are dropped
entirely. This is a discrepancy between code and description in the rendered
artifact, and `README.en.md` is what PyPI renders as the long description
(`unzip -p dist/*.whl "*/METADATA" | grep -c "csk hybrid add"` returns 2), so
the corruption ships to the package page as well as to GitHub.

Line 377 is a regression against the previous README, which escaped the same
row correctly: `git show HEAD:README.md:765` reads
`` `csk shell-init [auto\|zsh\|bash\|powershell]` ``. Line 372 is new, and it
is new because the previous review verdict handed the producer that row text
with raw pipes; the producer pasted it as given. The defect is real either way
and must be fixed before human review.

Fix, four escapes:

    README.en.md:372  --target <alias\|path\|glob>
    README.en.md:377  `csk shell-init [auto\|zsh\|bash\|powershell]`

Verify with a cell-count check; every row of a two-column table must split into
exactly four fields:

    grep -n '^|' README.en.md | awk -F'|' 'NF!=4 {print NF": "$0}'

The check must print nothing for `README.en.md`. Note that the same one-liner
reports the three-column tables in `ARCHITECTURE.md:335-341` and
`SECURITY.md:98-104`; those are correct five-field rows and are not defects.

## What passes, re-verified on this tree

Tests. Full suite green:

    .venv/bin/python -m pytest -q
    1418 passed, 243 skipped, 24 warnings in 206.51s

Packaging. `pyproject.toml:9` reads `readme = "README.en.md"`. A fresh build of
the current tree ships it and passes twine:

    .venv/bin/python -m build --outdir .temp/TASK-260819-1y7hh4/dist2  -> exit 0
    tar tzf dist2/*.tar.gz | grep -i README
      cocoaskills-0.13.1.dev2+gb1e05cdf5.d20260819/README.en.md
      cocoaskills-0.13.1.dev2+gb1e05cdf5.d20260819/README.md
    .venv/bin/python -m twine check dist2/*  -> PASSED (wheel and sdist)

`twine check` validates that the long description parses, not that its tables
render sensibly, so it does not catch finding 5.

Links. All 13 relative links in `README.en.md` resolve on disk. The single
anchor `#install-matrix` matches `## Install Matrix` at line 126.

Site links. No `README.ru.md` reference survives outside `LOGBOOK.md` history
and `.spec/docs-refresh.md`, both of which are records that must stay.

Structure and parity. Headings still map one-to-one onto the Russian core
sections, and every reference section named in the task is present: install
matrix, skill dependencies, global skills and selective operations, command
manifests, compiled commands, audit and registries, CLI table, development,
documentation index. The first-screen cross-link to `README.md` is at line 8.

Factual spot checks on content touched or newly read this cycle: schema v7 and
the `go-repository-v1` driver are documented at `README.en.md:288` and match
`skillspec.SUPPORTED_SCHEMA_VERSIONS = {1,...,7}`; `requires-python = ">=3.11"`
in `pyproject.toml:10` matches the Development section's Python 3.11 claim; the
archive and registry limits match `git_ops.py` and `audit_registry.py`.

Prose style. Zero em-dashes, zero en-dashes, zero guillemets, zero antithesis
constructions, zero filler openers, zero exclamation points.

Outcome resource. `TASK-260819-1y7hh4_results.md` was updated this cycle
(18:55) and its claims match what is on disk.

## Recorded so it is not re-litigated

The English README carries a Curator Protocol attribution sentence that the
Russian `README.md` does not (`grep -i curator README.md` is empty). The spec
allows `README.en.md` to carry reference material the root does not, so this is
not a parity failure, but the sentence sits in the definition paragraph rather
than in a reference section. Owner call, story level, not a blocker for this
task.

The install-matrix subsections still present bare command blocks under headings
with no introducing sentence. Judged acceptable last cycle because each heading
names the tool; unchanged.

`README.en.md` remains untracked in git. setuptools includes it by path, so the
build is safe; it only means the file must be added in the story commit.

## Definition of done

- [x] README.en.md parity with Russian core sections plus old-README reference material
- [x] pyproject readme=README.en.md and full pytest suite passes
- [x] docs/ site links updated; no reference to README.ru.md remains
- [ ] Docs updated and consistent with current code (finding 5)
- [ ] No discrepancies between code and description (finding 5)
- [x] Result linked as a new task-scoped outcome resource
- [x] Logbook entry recorded cleanly
- [x] Implementation matches AC
- [x] Solution fits project architecture
- [x] Tests green

## Rework scope for the next producer

Escape four pipe characters, at `README.en.md:372` and `README.en.md:377`. Run
the cell-count check above and paste its (empty) output as evidence. No test run
is required for a change that touches no code path, though rerunning the release
contract tests is cheap insurance. No commit is expected from the producer.
