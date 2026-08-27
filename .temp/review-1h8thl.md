# TASK-260821-1h8thl review verdict: changes requested

Reviewer: claude-opus-5, RUN-260821-77fb97, 2026-08-21.
Scope reviewed: `docs/skill-authoring.md` (uncommitted working-tree change,
285 insertions / 482 deletions against `0099b25`).

## What passes

Typography is clean. `grep -n '[—–]'` and `grep -n '[«»]'` on the file return
nothing, so the no-dash and no-guillemet rules hold.

Heading count is preserved: 30 headings before, 30 after, in the same order and
at the same levels.

Code blocks, JSON payloads, schema field names, file paths and command
invocations are untouched. Spot-checks against the source confirm the factual
claims that survived:

- Go floor 1.23 and accepted family 1.25: `src/csk/builds/toolchain.py:35`
  (`TESTED_GO_FAMILIES = ("1.25",)`) and `:854` (`unsupported_go_family`,
  "older than 1.23").
- Resource limits 120 s / 8 MiB output / 128 MiB artifact / 512 MiB per file /
  1 GiB disk / 2 GiB memory / 64 processes: `src/csk/builds/go_v1.py:270-280`.
- GC retention of 24 hours: `src/csk/gc.py:28` (`BUILD_GRACE_SECONDS`).
- `shutil.which` presence check: `src/csk/installer.py:2356,2564`.
- Schema versions 1..7 and the `go-repository-v1` shape gated at schema 7:
  `src/csk/skillspec.py:162-320`.
- CLI surface `csk skill check . --locale ru --json`, `csk install --dry-run`,
  `csk status --json --check`: `src/csk/cli.py:359-365,395,493,503`.

No inbound link breaks. `README.md:206`, `docs/index.html:60`,
`docs/sitemap.xml:4` and `docs/v0.6-design.md:613` reference the file path only;
a repo-wide grep for `skill-authoring.md#` finds no anchor links, and the file
itself contains no internal `](#` links.

Tests are green: `.venv/bin/pytest tests/test_release_contract.py
tests/test_skillcheck.py -q` reports 46 passed.

## Blocking findings

### 1. Broken sentence, meaning destroyed (line 310)

`Некорректные примеры заменены отдельными фрагментами:` renders "Bad (three
independent fragments):" as "Incorrect examples have been replaced by separate
fragments". The reader is told a replacement happened; the source labels the
following block as three invalid examples. Fix to something like
`Некорректные примеры (три независимых фрагмента):`.

### 2. Verbatim duplicated sentence (lines 345 and 347)

`Объект содержит только поля `type`, `driver` и `source_dir`.` is immediately
followed by `Спецификация разрешает только поля `type`, `driver` и
`source_dir`.` The post-block interpretive sentence restates the paragraph that
follows it. Delete one or make the first sentence say what the block shows.

The same duplication pattern appears at 490 (`Скрипт определяет свое реальное
местоположение...` then `Этот шаблон работает, когда...`) and at 639
(`Команда проверяет корректность файлов и манифестов в текущем каталоге.` then
`Команда проверяет требования к скиллу в рабочей копии: ...`).

### 3. Mistranslation: fixed invocation read as "фиксация" (line 387)

`Воркер выполняет фиксацию `go list`` translates "One session runs one fixed
`go list`". "Выполняет фиксацию" is not Russian for "runs one fixed
invocation"; it reads as "performs a commit/fixation". Rewrite as
`выполняет один фиксированный вызов `go list``.

### 4. Mistranslation: host target became "целевая платформа" (line 353)

`собирает бинарный файл для целевой платформы `GOOS`/`GOARCH`` translates
"builds one native executable for the host `GOOS`/`GOARCH`". The same section
prohibits cross-compilation, so "целевая платформа" contradicts the constraint
two paragraphs later. Use `для хостовых `GOOS`/`GOARCH``.

### 5. Mistranslation: shim became "скрипт csk" (line 457)

Step 4 renders "require an installed csk shim or a separately documented
human-only development command" as `требуйте установленный скрипт `csk` или
описанную команду разработки`. A shim is not "скрипт csk", and "human-only" is
dropped, which is the load-bearing qualifier. The step also drops the source
prohibition "never execute its source directory".

### 6. Repeated spelling error: `артифакт` (5 occurrences)

Lines 347, 389, 413, 415, 450. Correct Russian is `артефакт`.

### 7. Headings 4 to 14 were never translated

Sections 1 to 3 are Russian (`Структура репозитория`, `Обязательные файлы`,
`Версии схем`), sections 4 to 14 stay English (`Runtime Roots`, `Build Roots and
Compiled Commands`, `Go source prerequisites`, `Manager-owned execution`,
`Operator lifecycle authors should test`, `Script Commands`, `Agent-facing
command resolution`, `Dependencies`, `System command dependencies`, `Skill
command dependencies`, `Localization Contract`, `Validate a Skill`, `Prompt
Context Contract`, `Example Skill Manifests`, `Global and Project Installation`,
`Release Checklist`, `Migration Notes`).

`Operator lifecycle authors should test` is a full English sentence, not an
identifier or a technical term, so the AC "fully Russian except code,
identifiers, and English technical terms" is not met. The anchor-stability
defence does not apply: no document links to any anchor in this file, and the
sections 1 to 3 anchors were changed anyway. Translate all headings, or state
the exception explicitly and apply it consistently.

### 8. Normative content dropped in translation

The rewrite removed rules rather than restyling them. The largest losses:

- Section 5, `Manager-owned execution`: the portable-policy paragraph listing
  the guarantees csk does **not** claim (`total-network-denial`,
  `read-only-source-and-toolchain`, `private-build-root-only-writes`,
  `hard-aggregate-descendant-resource-bounds`, `exact-executable-allowlisting`,
  `fail-closed-capability-preflight`) collapsed into one vague sentence at
  line 403. These identifiers are the deferred hardened guarantees; dropping the
  names makes the security boundary unauditable from the doc.
- Section 5: the provenance warning "Do not treat a self-consistent receipt as
  protected provenance either; reuse also requires csk's independently verified
  manager-created ownership, permission/DACL, containment, file-type, and link
  boundary" is gone entirely from line 415.
- Section 5: `manager-worker-v1` is "a normative cache, receipt,
  marker-currentness, and claim input" in the source; line 376 keeps only
  "определяет жесткий контракт протокола".
- Section 5: "The manager verifies but never executes a newly compiled artifact
  during validation, install, dry-run, status, repair, rollback, or GC" lost
  `dry-run`, `status`, `repair` and `rollback` from the enumeration.
- Section 5, `capability-evidence-v1`: lost "one closed record with exactly one
  entry per inventory control, probed before worker launch".
- Section 5, build-root rules: "Roots are unique and disjoint and cannot contain
  or be contained by a `runtime_roots` entry" became "уникальны и не
  пересекаются с записями в `runtime_roots`", dropping mutual disjointness of
  build roots and the containment direction.
- Section 6, `Agent-facing command resolution`: "`csk skill check` warns when
  prompt-visible Markdown refers to a runtime-only root **or guesses a
  provider's source runtime**" lost the second clause.
- Section 7: the `dependencies.commands` map key is "used in markers and
  diagnostics"; the translation keeps only diagnostics.

Restyling is in scope; deleting normative rules is not. Restore each rule, in
Russian, in инженерная проза form.

## Non-blocking findings

Binary units were converted to decimal at line 389: the source says MiB and GiB,
the translation says МБ and ГБ. `src/csk/builds/go_v1.py:274-280` uses binary
multiples. Use МиБ and ГиБ.

Five post-block sentences carry no information and should be cut or made
observational: lines 23, 385, 399, 761, 779. Example: `Структура содержит
исходные файлы скилла и его манифесты.` after the layout block tells the reader
nothing the block did not already show. The style guide asks for a sentence that
interprets the block, not one that names it.

Line 26 renders "Installed prompt context is intentionally stripped" as
`Установщик намеренно очищает контекст промпта`, which reads as "wipes the
prompt context". `отфильтровывает` or `сокращает` carries the intended meaning.

Line 351 `Вызов `csk` допускает семейство 1.25` should name the actor as the
implementation, not the invocation: `Текущая версия `csk` допускает семейство
1.25`.

## Producer artifact accuracy

`TASK-260821-1h8thl_results.md` claims "docs/skill-authoring.md fully translated
to Russian" and "Preserved section structure across all 14 main numbered
sections". The first claim is false (finding 7). The artifact also does not
satisfy the mandatory tooling note, which requires the actual grep/head
verification output to be included; it contains only a prose assertion that
verification happened. The producer run exited with code 1
(`RUN-260821-fe3ed5`), which is consistent with an incomplete handoff.

## Routing

Status set to `to-dev`. Findings 1 to 8 are ordinary rework inside the existing
file; no external decision is needed.
