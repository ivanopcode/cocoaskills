# TASK-260821-1h8thl review verdict (round 3): accepted

Reviewer: claude-opus-5, RUN-260821 round 3, 2026-08-21.
Scope reviewed: `docs/skill-authoring.md`, uncommitted working-tree change,
275 insertions / 522 deletions against `0099b25` (`git show HEAD:docs/skill-authoring.md`
recovered to `.temp/rev3-orig.md` for comparison).

## Round-2 findings: all nine closed

Every blocking finding from RUN-260821-cedbab is fixed. Verified individually
against the file:

1. Line 266: `Секция `runtime_roots` перечисляет каталоги только для runtime.`
   The non-word `перегорождает` is gone.
2. Line 203: `- Необязательное поле `transport` документирует транспорт: `stdio`
   или `http`.` Optionality restored, matching `src/csk/skillspec.py:92`
   (`transport: str | None = None`).
3. Line 375: `он не является ключом кэша, квитком, маркером, входом актуальности
   или входом для утверждений`. All five source exclusions present.
4. Line 250: `Репозиторий содержит как минимум следующие файлы:`.
5. Line 383: `Безопасно опубликованная запись без ссылок может остаться для GC
   под блокировкой.` Modality and lock restored, redundant gloss removed.
6. Line 624: `так как ничто не помечает эти файлы как относящиеся только к
   runtime`. `помещает` corrected to `помечает`.
7. Line 605: `Каталоги локалей корректны, когда минимум одна локаль
   присутствует...`. Validity rule, not importance.
8. Lines 470 and 508 both use `инструменты подготовки проекта`. No rotation,
   and `инструменты сборки` is gone.
9. `TASK-260821-1h8thl_results.md` on the board is 3445 bytes of real content.
   The unexpanded `$(cat ...)` string is gone. Every claim in it that I could
   check independently is true.

The four non-blocking round-2 items are also fixed: line 356 code block
indentation is byte-identical to `HEAD` again, line 434 names the contract
(`независимого от оболочки контракта`), line 506 carries `для этого скилла`,
and line 599 carries `который будет отсутствовать после установки`.

## Structural verification

Machine-compared block by block against `git show HEAD:docs/skill-authoring.md`,
after unwrapping hard-wrapped lines (the previous 133-vs-97 paragraph gap was an
artifact of line wrapping, not content loss):

- 272 block-level elements in both files, aligned one to one with no insertions,
  deletions or reorderings;
- 30 headings, 27 fenced code blocks, 117 list items, 1 table, 97 paragraphs,
  identical counts and identical order in both files;
- heading levels match position by position;
- all 27 fenced code blocks are byte-identical to the original.

Compression check: across all 245 non-code blocks, the lowest Russian/English
character ratio is 0.85, and that block is a faithful translation of a short
numbered step. There is no block where prose was silently shortened.

Identifier check: across all 245 non-code blocks, zero inline backtick
identifiers were dropped relative to the aligned English block. The
normative-compression pattern from rounds 1 and 2 does not recur.

## Language and typography

`grep -c '[—–]'` returns 0. `grep -c '[«»]'` returns 0. `grep -ic 'артифакт'`
returns 0.

All 30 headings are Russian; `grep -n '^#'` reports a 31st hit, which is the
`#!/usr/bin/env bash` shebang inside a code block at line 443.

Scanning every prose line for runs of three or more Latin words outside code
spans yields 16 hits, all of them product names (`CocoaSkills`, `Claude Code`,
`Codex CLI`, `Cursor`, `Gemini`, `Mise`, `Make`, `Git`), platform names
(`macOS`, `Windows`, `Linux`), or accepted technical terms (`runtime`, `shim`,
`commit`, `provenance`, `cgo`, `PGO`, `Markdown`, `dry-run`, `status`,
`repair`, `rollback`, `inventory control`). No untranslated English sentence
remains. The AC clause "fully Russian except code, identifiers, and English
technical terms" holds.

Style blacklist sweep found no antithesis constructions, filler openers,
marketing adjectives, or restating closing paragraphs. The two grep hits on
`это не` at lines 348 and 733 are plain negations, not staged contrasts.

## Facts spot-checked against the code

- Go protocol floor 1.23 and accepted family 1.25: `src/csk/builds/toolchain.py:35`
  (`TESTED_GO_FAMILIES = ("1.25",)`), `:854` ("Go release is older than 1.23").
- Resource limits 120 s, 8 МиБ output, 128 МиБ artifact, 512 МиБ per file,
  1 ГиБ disk, 2 ГиБ memory, 64 processes: `src/csk/builds/go_v1.py:270-280`
  (`ResourceLimits`). All seven values match and the binary units are correct
  against the `1024`-based constants.
- GC retention of 24 hours: `src/csk/gc.py:28` (`BUILD_GRACE_SECONDS = 24 * 60 * 60`).
- `shutil.which` presence check: `src/csk/installer.py:2356,2564`.
- Schema versions 1 to 7: `src/csk/skillspec.py:23`
  (`SUPPORTED_SCHEMA_VERSIONS = {1, 2, 3, 4, 5, 6, 7}`).
- `transport` optional with `stdio`/`http`: `src/csk/skillspec.py:40,92,512`.
- `required_in` default `any`: `src/csk/skillspec.py:93,519-521`.
- Identifiers quoted in the doc exist in `src/`:
  `build_execution_control_unavailable` (`builds/go_v1.py:56`),
  `conflicting_skill_manifests` (`skillspec.py:119`),
  `would-preflight-and-build` and `would-rebuild-untrusted-cache`
  (`builds/cache.py:100,102`).

## Links

Inbound references are path-only and still resolve: `README.md:206`,
`README.en.md:419`, `docs/index.html:60`, `docs/sitemap.xml:4`,
`docs/v0.6-design.md:613`. A repo-wide grep for `skill-authoring.md#` finds
nothing and the file contains no internal `](#` links, so anchors are not
load-bearing. `ARCHITECTURE.md` does not reference the file.

Outbound targets all exist: `docs/v0.5-design.md`, `docs/v0.9-design.md`,
`docs/external-build-repositories.md`, and the external curator-spec rc.5 URL.

## Tests

Full suite: `.venv/bin/pytest -q` reports
`1430 passed, 243 skipped, 24 warnings in 270.69s`.

Doc-relevant suites: `.venv/bin/pytest tests/test_release_contract.py
tests/test_skillcheck.py -q` reports `46 passed, 24 warnings in 0.11s`.

## Acceptance evidence for the commit-owning mover

Scope committed by this task is `docs/skill-authoring.md` only. The working tree
also carries `README.md`, `docs/prose-style.md` and `LOGBOOK.md` changes from
sibling tasks in the same story, plus untracked `.research/` and `.spec/` files;
those are not this task's scope and are not covered by this acceptance.

Reviewer-archetype run, so no `commit_ack` is supplied here. The commit-owning
mover commits `docs/skill-authoring.md` and then makes the final Story `done`
transition with `commit_ack=scope_committed`.

## Routing

Status set to `done`. The AC is met: the file is fully Russian except code,
identifiers and English technical terms; structure and heading count are
preserved exactly; there are no em-dashes, en-dashes or guillemets; inbound
links from `README.md` and the other referencing documents still resolve; and
the spot-checked facts match the code.
