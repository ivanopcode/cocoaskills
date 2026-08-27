# TASK-260821-1h8thl review verdict (round 2): changes requested

Reviewer: claude-opus-5, RUN-260821-cedbab, 2026-08-21.
Scope reviewed: `docs/skill-authoring.md`, uncommitted working-tree change,
277 insertions / 524 deletions against `0099b25`. Producer run
RUN-260821-a6c344 (exit 1).

## Round-1 findings: all eight closed

Every blocking finding from RUN-260821-77fb97 is fixed. Verified individually:

1. Line 290 now reads `Некорректные примеры (три независимых фрагмента):`.
2. The duplicated post-block sentences are gone at all three sites
   (`Спецификация разрешает только поля ...` at 323, the `#!/usr/bin/env bash`
   block closer at 460, `csk skill check` at 599).
3. Line 361: `выполняет один фиксированный вызов `go list``.
4. Line 329: `для хостовых `GOOS`/`GOARCH``.
5. Line 427 step 4 keeps the shim, the human-only qualifier, and the
   never-execute-source rule: `требуйте установленного csk shim или отдельно
   задокументированной команды только для человека (без выполнения
   собственного исходного каталога)`.
6. `grep -ic 'артифакт'` returns 0.
7. All 31 heading lines are Russian. Sections 4 to 14 are translated.
8. The dropped normative content is back: the portable-policy non-guarantee
   identifiers (375), the provenance/receipt warning (387), `manager-worker-v1`
   as a normative cache/receipt/marker/claim input (352), the full
   verify-never-execute enumeration including dry-run, status, repair and
   rollback (377), the closed `capability-evidence-v1` record probed before
   worker launch (373), build-root mutual disjointness and containment in both
   directions (308), the second `csk skill check` warning clause about guessing
   a provider's source runtime (438), and the `dependencies.commands` map key
   as a marker and diagnostic input (537).

The non-blocking items are also fixed: line 363 uses `МиБ`/`ГиБ`, line 23
renders as `отфильтровывает`, and the vacuous post-block sentences are cut.

## Structural verification

Machine-compared against `git show HEAD:docs/skill-authoring.md`:

- headings: 31 originally, 31 now, same order and levels;
- paragraphs: 97 originally, 97 now, aligned one to one with no insertions,
  deletions or reorderings;
- bullet and numbered lists: 19 lists and 117 items in both files, matched
  list by list;
- fenced code blocks: 27 in both, byte-identical except one whitespace drift
  (see finding 10).

Typography is clean: `grep -c '[—–]'` and `grep -c '[«»]'` both return 0.
Latin text in prose is confined to identifiers, product names and accepted
technical terms (runtime, shim, commit, ref, workflow, provenance, frontmatter,
nonce). The AC clause "fully Russian except code, identifiers, and English
technical terms" holds.

## Facts spot-checked against the code

- Go protocol floor 1.23 and accepted family 1.25: `src/csk/builds/toolchain.py:35`
  (`TESTED_GO_FAMILIES = ("1.25",)`), `:854` (`unsupported_go_family`).
- Resource limits 120 s, 8 MiB output, 128 MiB artifact, 512 MiB per file,
  1 GiB disk, 2 GiB memory, 64 processes: `src/csk/builds/go_v1.py:270-280`
  (`ResourceLimits`). All seven values match, and the binary units are now
  rendered as `МиБ`/`ГиБ`, consistent with the `1024`-based constants.
- GC retention of 24 hours: `src/csk/gc.py:28` (`BUILD_GRACE_SECONDS`).
- `shutil.which` presence check: `src/csk/installer.py:2356,2564`.
- Schema versions 1 to 7: `src/csk/skillspec.py:23`.
- `required_in` default `any` and the `stdio`/`http` transport set:
  `src/csk/skillspec.py:93,517-521`.
- CLI surface: `csk skill check . --locale ru --json` (`src/csk/cli.py:365-366`),
  `csk install --dry-run` (`:395`), `csk status --json --check` (`:149,159`),
  `csk global install --dry-run` (`:502-503`), `csk global status --json --check`
  (`:493,498`).
- Identifiers quoted in the doc all exist in `src/`: `build_execution_control_unavailable`,
  `conflicting_skill_manifests`, `would-preflight-and-build`,
  `would-rebuild-untrusted-cache`, `capability-evidence-v1`, `manager-worker-v1`,
  `curator-build-source-v1`, `go-repository-v1`, `rc5-native-control-inventory-v1`.

## Links and tests

Inbound references are path-only and still resolve: `README.md:206`,
`README.en.md:419`, `docs/index.html:60`, `docs/sitemap.xml:4`,
`docs/v0.6-design.md:613`. A repo-wide grep for `skill-authoring.md#` finds
nothing and the file contains no internal `](#` links, so anchors are not
load-bearing. `ARCHITECTURE.md` does not reference the file at all. Outbound
targets `docs/v0.5-design.md`, `docs/v0.9-design.md` and
`docs/external-build-repositories.md` all exist.

Tests: `.venv/bin/pytest tests/test_release_contract.py tests/test_skillcheck.py -q`
reports `46 passed, 24 warnings in 0.11s`.

## Blocking findings

### 1. Non-word verb in the section 4 lead sentence (line 266)

`Секция `runtime_roots` перегорождает каталоги только для runtime.` The source
is "`runtime_roots` lists directories that are runtime-only". `перегорождает`
means "partitions off" and does not parse in this sentence; the lead claim of
section 4 is unreadable. Use `перечисляет` or `задает`.

### 2. `transport` lost its optionality (line 203)

`- Поле `transport` документирует транспорт: `stdio` или `http`.` The source
reads "`transport` is optional documentation". Every neighbouring bullet marks
optionality explicitly (`hint` обязательно, `required_in` has a default), so
the omission reads as a required field. `src/csk/skillspec.py:92` defaults it
to `None` and `:516-518` accepts its absence. Restore `необязательное`.

### 3. `capability-evidence-v1` exclusion list shortened (line 375)

`он не является ключом кэша, квитком, маркером или входом для утверждений`
covers four of the five source exclusions: "not a cache-key, receipt, marker,
claim, or currentness input". The currentness input is dropped. This is the
same normative-compression pattern round 1 flagged, in the same paragraph
family. Add `или входом актуальности`.

### 4. "at least" dropped from the schema-6 repository listing (line 250)

`Репозиторий содержит следующие файлы:` renders "Its repository contains at
least:". The translation presents the listing as the exhaustive contents of the
repository. Use `Репозиторий содержит как минимум следующие файлы:`.

### 5. GC retention asserted unconditionally (line 383)

`Безопасно опубликованная запись без ссылок сохраняется для сборщика мусора GC.`
The source is "A safely published unreferenced entry may remain for locked GC".
The translation drops the modality ("may") and the lock ("locked"), turning a
permitted outcome into a guaranteed one, and `сборщика мусора GC` is a
redundant gloss. Rewrite as `может остаться для GC под блокировкой`.

### 6. Wrong verb inverts the legacy-exception rationale (line 624)

`так как ничто не помещает эти файлы в runtime` renders "because nothing marks
those files as runtime-only". `помещает` (puts) instead of `помечает` (marks)
changes the stated cause: the reason is the absence of a runtime-only marking,
not the absence of a copy. Fix to `ничто не помечает эти файлы как относящиеся
только к runtime`.

### 7. "valid" became "important" (line 605)

`Каталоги локалей важны:` renders "Locale catalogs are valid when at least one
locale appears in both". The validity condition survives in the rest of the
sentence, but the lead claim now states importance instead of a validity rule.
Use `Каталоги локалей корректны, когда ...`.

### 8. Terminology rotation on "bootstrap" (lines 470 and 494)

Line 470 renders "installed by the machine or project bootstrap" as
`устанавливаемые в систему или проект инструментами сборки`. Line 494 renders
the same concept as `Инструменты подготовки проекта`. Two problems: the style
guide forbids rotating synonyms for one term, and `инструменты сборки` means
build tools, which is a different thing from bootstrap tooling. Use
`инструменты подготовки проекта` in both places.

### 9. Board outcome resource contains no evidence

`TASK-260821-1h8thl_results.md` on the board is the literal, unexpanded string
`$(cat /Users/iv/Developer/Wildberries/cocoaskills/.temp/TASK-260821-1h8thl_results.md)`.
The heredoc was quoted at the wrong layer, so the command substitution was
never performed. The real write-up exists only at
`.temp/TASK-260821-1h8thl_results.md` in the working tree, which is untracked
and not visible to the board. The mandatory tooling note requires the grep and
head verification output inside the outcome resource, and the DoD requires the
result to be linked as a task-scoped outcome resource. Neither holds. Re-upload
the artifact with `task-board resource update` using `--file`, or a heredoc
whose delimiter is unquoted so substitution runs.

## Non-blocking findings

Line 356: the `text` process-tree block gained one space of indentation on its
third and fourth lines relative to `HEAD` (7 and 12 spaces became 8 and 13).
All other 26 code blocks are byte-identical. The AC asks for code blocks to
stay exactly as they are, so restore the original indentation, but this changes
no meaning.

Line 434: "lacks this shell-neutral contract" became `не хватает этого
контракта`. The anaphora carries the meaning from the preceding sentence, so
the rule survives; naming the contract `shell-neutral` would be closer.

Line 470: `до записи файлов runtime, контекста проекта или shims` drops the
source qualifier "for that skill".

Line 599: `Команда также предупреждает, если доступный промпту Markdown
указывает на каталог только для runtime или сборки` drops "that will be absent
after install", which is the reason the warning exists.

## Assessment

The rework is a large improvement and the document is close to shippable. All
eight round-1 findings are closed, structure is preserved exactly at every
granularity I could measure mechanically, the facts hold against the code, and
the tests pass. What remains is one non-word (finding 1), one inverted verb
(finding 6), four one-clause normative losses (findings 2 to 5), one
terminology rotation (finding 8), and a board artifact that persisted nothing
(finding 9). Findings 1 to 8 are eight single-sentence edits in one file.

## Routing

Status set to `to-dev`. All findings are ordinary rework inside the existing
file plus one board-artifact re-upload; no external decision is needed.
