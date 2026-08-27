# TASK-260821-3s96o5 Review Verdict (cycle 2): changes requested

Reviewer run: RUN-260821-c33c62 (claude-opus-5). Run is not goal-bound
(`task-board spawn goal RUN-260821-c33c62` returned "none (run is not
goal-bound)").

Verdict: **changes_requested -> to-dev**. Every item from the previous
verdict (RUN-260821-82e9d8) is fixed and verified. One factual defect
remains in the new market section: the closing sentence attributes a
capability to `csk` that the code does not have and that `README.md:59`
explicitly denies.

## Previous-cycle items: all fixed

### B1. License badge restored (PASS)

`README.md:5` is back to the shields.io SVG with the LICENSE blob as the
link target, and the line no longer appears in `git diff README.md`:

```
$ grep -n 'img.shields.io' README.md
3:[![PyPI](https://img.shields.io/pypi/v/cocoaskills.svg)](https://pypi.org/project/cocoaskills/)
4:[![Python versions](https://img.shields.io/pypi/pyversions/cocoaskills.svg)](https://pypi.org/project/cocoaskills/)
5:[![License](https://img.shields.io/pypi/l/cocoaskills.svg)](https://github.com/ivanopcode/cocoaskills/blob/main/LICENSE)
```

### B2. Adapter claim corrected (PASS)

`README.md:53` now reads:

> Что делает `csk`: хранит единый `Skillfile.json` и раскладывает скиллы
> по адаптерам Claude Code, Codex CLI, Cursor и Gemini; OpenCode и
> Windsurf читают `.agents/skills/` напрямую.

Matches `src/csk/adapters.py:15-26` (`AGENT_PATHS` = codex_cli,
claude_code, gemini, cursor; `NATIVE_DISCOVERY_AGENTS` = windsurf,
opencode) and `src/csk/cli.py:330-336`.

### N1. Blank line added (PASS)

`docs/prose-style.md:88` is blank; the comparative-overview amendment
starts at line 89 as its own paragraph.

### N2. Test evidence (PASS)

`TASK-260821-3s96o5_results.md` now reports
`1418 passed, 243 skipped, 24 warnings in 272.71s (0:04:32)`, consistent
with an actual run of this tree.

## Blocking finding

### C1. `README.md:39` claims a search capability `csk` does not have

The closing sentence of the market section:

> Существующие инструменты закрывают задачи поиска и проверки навыков,
> но `csk` объединяет эти функции в детерминированный менеджер пакетов
> для закрытого корпоративного контура.

"эти функции" resolves to "задачи поиска и проверки навыков", so the
sentence asserts that `csk` combines skill search with verification.
Three problems:

1. There is no discovery or search surface in the code. `grep -n
   'add_parser(' src/csk/cli.py` lists install, update, upgrade, status,
   add, remove, hybrid, list, project, config, shell, skill check, global
   and friends; `grep -rn 'search' src/csk/cli.py` returns nothing.
   `csk list` enumerates declared skills, not a registry.
2. It contradicts `README.md:59` in the same document: "Инструмент
   CocoaSkills не служит публичным реестром пакетов ... Установщик
   отвечает только за декларативную доставку и локальную раскладку
   файлов скиллов."
3. It inverts the source appendix, which positions CocoaSkills "на
   уровень ниже каталогов и систем поиска" and says the market lacks a
   tool combining distribution, security and quality control *with* a
   deterministic package manager. The distillation turned "csk sits
   below catalogs" into "csk absorbs catalog functions".

This fails the AC "all factual claims about csk match the code" and the
DoD "No discrepancies between code and description". It is the same
class of defect as B2 from the previous cycle.

Fix: keep the one-sentence conclusion required by directive 3, but state
the adjacency rather than absorption. For example:

> Существующие инструменты решают задачи каталогизации и проверки
> навыков; `csk` закрывает соседний уровень: детерминированную и
> воспроизводимую установку скиллов внутри закрытого корпоративного
> контура.

## Non-blocking findings

### N3. `README.md:33` overstates the source allowlist

"Поддержка закрытых источников ограничивает загрузку только
разрешёнными git-репозиториями" reads as an unconditional guarantee.
`src/csk/source_identity.py:119-121` documents the actual behaviour: "An
empty allowlist allows every source." The capability exists; the
restriction applies only once an allowlist is configured. Consider
"позволяет ограничить загрузку разрешёнными git-репозиториями".

### N4. `README.md:30` uses a verbal noun the style guide discourages

"Инструмент обеспечивает воспроизводимое и безопасное управление" is the
"выполняет проверку" pattern the Russian rules reject in favour of a
plain verb. Not an AC failure; fix if the sentence is touched anyway.

## What passes

- Directive 3. `## Рынок и позиция CocoaSkills` (`README.md:18`) sits
  directly after `## Зачем`. GFM table at lines 22-28 covers Vercel
  (skills.sh), SkillKit, Tessl, NVIDIA Skills / SkillSpector and Agent
  Plugins with the four source columns, reworded rather than pasted.
  Six properties as a compact parallel list (lines 32-37).
- Property claims verified against the code: conflict detection
  (`src/csk/closure.py:182`), source allowlist
  (`src/csk/source_identity.py:119`), three-layer materialization
  (`ARCHITECTURE.md:23-35`, prompt context / runtime / compiled), rollback
  (`src/csk/transactions.py:49,492,509`), canonical `.agents/skills/`,
  local audit gate (`ARCHITECTURE.md:117-121`).
- Directive 4. Four alternatives, each a bold lead-in plus exactly two
  points ("Где ломается" / "Что делает `csk`"); the closing
  what-csk-is-not paragraph is preserved verbatim at line 59.
- Directive 5. Five `<details>` blocks at `README.md:65-109`, `pipx`
  first with `open`, each `<summary>` naming the variant. Commands match
  the install matrix in `README.en.md:129-167` exactly: `pipx install
  cocoaskills`, `uv tool install cocoaskills`, `brew tap ivanopcode/csk`
  + `brew install cocoaskills`, `mise use -g pipx:cocoaskills@latest`,
  `python -m pip install --user cocoaskills`. The 3-space indent inside
  list item 1 renders on GitHub and GitLab; ordered-list numbering
  continues correctly at item 2 (`README.md:113`).
- Style amendments present at `docs/prose-style.md:89-94` under "Lists vs
  prose", matching the spec Style notes wording.
- Typography: `grep -n '—\|–\|«\|»' README.md` exits 1 with no output.
  The three hits in `docs/prose-style.md` (144, 147, 172) are the guide's
  own Bad examples and are untouched by the diff.
- Section order matches the round-2 target layout for sections 1-6.
  "Команды" is directive 6 and belongs to the `cli-reference` task.
- Scope: `git diff --stat` touches `README.md` (+85), `docs/prose-style.md`
  (+7), `LOGBOOK.md` (+74, expected) and `docs/skill-authoring.md`, which
  is owned by TASK-260821-1h8thl (status `development`) in the same
  working tree, not by this task.

## Test evidence

Full suite on this working tree, `.venv/bin/python -m pytest -q`:

```
1430 passed, 243 skipped, 24 warnings in 261.08s (0:04:21)
```

Exit code 0. The count differs from the producer report (1430 vs 1418
collected) because the sibling task's work landed in the same tree between
the two runs; the suite is green either way.

## Routing

Status set to `to-dev`. Fix C1 (and N3/N4 while in there), re-verify with
`grep -n 'поиска' README.md` plus a fresh `git diff README.md`, and return
to review. No commit evidence is recorded: this run is a reviewer
archetype and did not accept the work.
