# TASK-260819-1y7hh4 Review Verdict: changes requested

Reviewer run: RUN-260819-c9148b. Review date: 2026-08-19.
Working tree: /Users/iv/Developer/Wildberries/cocoaskills (uncommitted).

## Verdict

changes_requested, routed to `to-dev`. The primary deliverables are correct and
independently verified. One shipped artifact in the same diff is corrupted and
must be repaired before the story can go to human review. Two smaller fidelity
gaps are listed with it.

## What passes (verified independently, not taken from the producer report)

Packaging. `pyproject.toml:9` reads `readme = "README.en.md"`. The build backend
is setuptools, not hatchling, so the spec warning about hatchling excludes does
not apply. A real build confirms the file ships and the PyPI page is English:

    .venv/bin/python -m build --outdir .temp/TASK-260819-1y7hh4/dist   -> exit 0
    tar tzf dist/*.tar.gz | grep -i readme
      cocoaskills-0.13.1.dev2+gb1e05cdf5.d20260819/README.en.md
      cocoaskills-0.13.1.dev2+gb1e05cdf5.d20260819/README.md
    unzip -p dist/*.whl "*/METADATA" | grep -c "csk\` manages local skill packages"  -> 1
    .venv/bin/python -m twine check dist/*   -> PASSED (wheel and sdist)

Tests. Full suite green on this tree:

    .venv/bin/python -m pytest -q
    1418 passed, 243 skipped, 24 warnings in 225.23s

Site links. `docs/index.html` and `docs/sitemap.xml` never referenced
`README.ru.md` (`git show HEAD:docs/index.html | grep README` is empty), so the
"no changes needed" outcome is correct, not a skipped step. The only surviving
`README.ru.md` strings in the repo are historical LOGBOOK entries and
`.spec/docs-refresh.md`, which are records and must stay.

Structure and parity. `README.en.md` headings map one-to-one onto the Russian
core sections (`Why`/`Зачем`, `Why CocoaSkills and Not Alternatives`/`Почему
CocoaSkills, а не альтернативы`, `Quick Start`/`Быстрый старт`, `Skill Install
Modes`/`Режимы установки скиллов` with the same three subsections and the same
shadowing order), and adds every reference section the task named: install
matrix, skill dependencies, global skills and selective operations, command
manifests, compiled commands overview, audit and registries, CLI table,
development, documentation index. The first-screen cross-link is present at
`README.en.md:8`.

Link integrity. All 13 relative links in `README.en.md` resolve on disk. The one
anchor link `#install-matrix` matches `## Install Matrix`.

Factual accuracy, checked against source rather than against the old README:

- `csk init` gitignore claim matches `cli.py:872`, which passes
  `adapters.all_gitignore_entries()` plus `dev_substitutions.DEV_MANIFEST_NAME`.
- Six environments matches `adapters.AGENT_PATHS` (4 adapter agents) plus
  `NATIVE_DISCOVERY_AGENTS = {windsurf, opencode}`.
- Schema versions 1 through 7 matches `skillspec.SUPPORTED_SCHEMA_VERSIONS`.
- `csk hybrid add --target` matches `csk hybrid add --help`.
- `csk global install/update/upgrade --only` matches the CLI help.
- Archive and registry limits match `git_ops.MAX_ARCHIVE_ENTRIES = 100_000`,
  `git_ops.MAX_ARCHIVE_BYTES = 512 * 1024 * 1024`,
  `audit_registry.MAX_RESPONSE_BYTES = 16 * 1024 * 1024`,
  `audit_registry.MAX_RECORDS_PER_QUERY = 10_000`.
- The `csk gc` 24-hour claim matches `gc.BUILD_GRACE_SECONDS = 24 * 60 * 60`.
- The `ARCHITECTURE.md` cross-reference is honest: that file does carry the
  build contract, storage layout, the `manager-worker-v1` boundary, and the
  security model.

Prose style. Zero em-dashes, zero en-dashes, zero guillemets, zero antithesis
constructions, zero filler openers, zero exclamation points in prose. Bullet
lists carry parallel enumerable facts (activation modes, shared flags, doc
index), not argument structure.

## Findings

### 1. LOGBOOK.md carries a shell-mangled duplicate of this task's entry (blocking)

`LOGBOOK.md:11-18` repeats the heading written at `LOGBOOK.md:3` and its body,
but every backticked identifier was consumed by unquoted shell expansion before
the text reached the file. The duplicate reads:

    Created  in English with full content parity to Russian  and restored
    reference material from the original README (... linking , security audit ...)
    Applied style guide (): active voice, ...
    Updated   field from  to . Verified full test suite ...

Every file name, field name, and path is gone. This is exactly the failure mode
the attached tooling note warned about: a heredoc written with an unquoted
delimiter, so `` `README.en.md` `` and friends were run as commands and replaced
with empty strings. The entry is information-free and would land in the commit,
degrading the durable engineering record.

Fix: delete `LOGBOOK.md:11-18`. The correct entry at lines 3 through 9 already
says everything and is intact. Use a quoted heredoc delimiter (`<<'EOF'`) for
the verification.

`README.en.md` and `pyproject.toml` were checked for the same corruption and are
clean; `grep -n '``' README.en.md` returns only real fenced blocks.

### 2. CLI reference omits the `csk hybrid` command group

`README.en.md:342-376` lists every top-level command group except `hybrid`, even
though the same document documents hybrid mode as one of the three first-class
install modes (`README.en.md:110`) and `csk --help` exposes
`csk hybrid {add,remove,list,status}`. The old README had the same gap, so this
is inherited rather than introduced, but the new document promotes hybrid mode
to a headline concept, which turns the omission into a visible discrepancy
between code and description.

Fix: add four rows to the table, matching the style of the `csk global` rows:
`csk hybrid add <name> --git ... --tag/--branch/--revision --target <alias|path|glob>`
(the `--target` flag is repeatable), `csk hybrid remove <name>`,
`csk hybrid list`, `csk hybrid status`.

### 3. The Curator Protocol claim lost its source pointer

`README.en.md:10` states that `csk` "is an independent Python implementation of
the open Curator Protocol specification" with no link. The old README carried
the same claim with a link to `https://github.com/relux-works/curator-spec` plus
the qualifier that the `csk` executable, package name, and state directory names
remain implementation-specific compatibility names while portable manifest and
marker names follow the shared protocol. The claim is now the only unverifiable
assertion on the first screen, and the style guide requires cross-references to
name a precise target.

Fix: restore the link, and either restore the compatibility-names sentence or
drop the protocol claim from the opening paragraph. Note that the Russian
`README.md` carries no equivalent sentence; if the claim stays in the English
file, the parity question is worth a one-line decision in the story.

### 4. Minor: marketing adjective in the documentation index lead-in

`README.en.md:411` opens the documentation index with "Comprehensive guides and
technical specifications:". The style guide blacklists marketing adjectives
applied to the project. "Reference documents:" or a lead-in that says what the
list contains carries the same information without the register.

## Not a defect, recorded so it is not re-litigated

The install matrix subsections present bare command blocks under headings with
no introducing sentence. The style guide asks for a sentence ending in a colon
before each block. The old README used exactly this shape for the same section,
and each heading names the tool, so the heading carries the introduction. Left
as is.

`README.en.md` is untracked in git. The sdist build above confirms setuptools
includes the readme file by path regardless of git tracking, so this does not
threaten a release. It only means the file must be added in the story commit.

## Definition of done

- [x] README.en.md parity with Russian core sections plus old-README reference material
- [x] pyproject readme=README.en.md and full pytest suite passes
- [x] docs/ site links updated; no reference to README.ru.md remains
- [ ] Docs updated and consistent with current code (finding 2)
- [ ] No discrepancies between code and description (finding 2)
- [x] Result linked as a new task-scoped outcome resource
- [ ] Logbook entry recorded cleanly (finding 1)
- [x] Implementation matches AC
- [x] Solution fits project architecture
- [x] Tests green

## Rework scope for the next producer

Repair `LOGBOOK.md` (finding 1), add the `csk hybrid` rows (finding 2), restore
the Curator Protocol link or drop the sentence (finding 3), and reword the
documentation index lead-in (finding 4). Rerun the full suite and re-request
review. No code changes are required and no commit is expected from the
producer.
