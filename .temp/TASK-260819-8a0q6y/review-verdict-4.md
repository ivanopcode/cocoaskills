# TASK-260819-8a0q6y ru-root-readme: review verdict 4 (RUN-260819-e71b06)

Verdict: accepted. Route to `done`.

Reviewed: `README.md` (134 lines, 906 words) after the third rework, against
`src/csk/whitelist.py`, `src/csk/adapters.py`, `src/csk/cli.py`,
`src/csk/hybrid.py`, `src/csk/global_install.py`, `src/csk/installer.py`,
`src/csk/shims.py`, `ARCHITECTURE.md`, plus runtime checks of `csk init`,
`csk --version`, `csk add --help`, `csk global add --help`,
`csk global install --help`, `csk hybrid add --help`, and the full suite.

## Previous blocking findings: all fixed

### Verdict 1 finding 1, `csk install --global` — FIXED

`grep -n 'csk install --global' README.md` returns nothing (exit 1).
`README.md:108` reads `csk global install`. Confirmed at runtime:

```
$ uv run csk global install --help
usage: csk global install [-h] [--dry-run] [--verbose] [--strict-tags]
                          [--audit [{advisory,strict}]] [--only NAME] ...
```

### Verdict 1 finding 2, `csk init` gitignore block — FIXED

`README.md:47` enumerates the block verbatim. Re-verified by running `csk init`
in a fresh temp git repo:

```
# CocoaSkill
.agents/
.claude/skills/
.codex/skills/
.cursor/rules/
.gemini/skills/
Skillfile.dev.json
```

The written manifest matches the step description:

```json
{ "schema_version": 1, "project": { "alias": "tmp.6bnnhnv2t2" },
  "agents": ["codex_cli", "claude_code"], "skills": [] }
```

### Verdict 2 finding 3, hybrid adapter placement — FIXED

`README.md:112` reads "Установщик создаёт адаптеры в директориях агентов
проекта (`.claude/skills/`, `.codex/skills/`) и шимы команд в `.agents/bin/`,
но не требует коммитов в git-репозиторий". This matches
`installer.py:1286-1299` (hybrid `AdapterGroup` with `canonical_root=hybrid_skills`
handed to `plan_project_adapter_targets`), `shims.py:447-448`, and
`tests/test_hybrid_scope.py:88-93`.

### Verdict 3 finding 1, whitelist is root-based not extension-based — FIXED

Both load-bearing sentences now describe selection by path. `grep -n 'расширен'
README.md` returns nothing (exit 1).

`README.md:16`: "копирует в контекст агента только `SKILL.md` и объявленные
каталоги (`references/`, `assets/`, `agents/`, `data/`), исключая `tests`,
`README`, файлы сборки и метаданные git".

`README.md:20`: "отбирает только разрешённые каталоги скилла".

This matches `whitelist.py:17-25` (`INCLUDE_ROOTS` = `SKILL.md`, `agents`,
`references`, `.skill_triggers`, `assets`, `templates`, `examples`, `data`,
plus `scripts` under `include_scripts`) and `whitelist.py:27-46`
(`ALWAYS_EXCLUDED` covering `tests`, `README*`, `Makefile`, `pyproject.toml`,
`.git`). The parenthetical names a subset of `INCLUDE_ROOTS`; the claim
"объявленные каталоги" is accurate, and the README is not the schema reference.

### Verdict 3 finding 2, OpenCode and Windsurf receive no adapters — FIXED

`README.md:24` reads "раскладывает скиллы по адаптерам Claude Code, Codex CLI,
Cursor и Gemini, а OpenCode и Windsurf читают канонический каталог
`.agents/skills/` напрямую". This matches `adapters.py:15-25`: `AGENT_PATHS`
covers exactly `codex_cli`, `claude_code`, `gemini`, `cursor`;
`NATIVE_DISCOVERY_AGENTS` is `frozenset({"windsurf", "opencode"})` with the
source comment "discover the canonical .agents/skills/ directory natively".
The definition at `README.md:10` ("шести сред") stays accurate.

### Verdict 3 non-blocking note, runtime claim — ADDRESSED

`README.md:28` now reads "не выступает runtime-средой для агента", which no
longer reads as a contradiction with the shims promised at `README.md:63`.

## Verification performed this cycle

Structure follows `.spec/docs-refresh.md` section README.md exactly: definition
(`:10`), Зачем (`:12`), Почему CocoaSkills а не альтернативы (`:18`, four
alternatives one paragraph each at `:20`, `:22`, `:24`, `:26`, closing "what it
is not" at `:28`), Быстрый старт (`:30`, five numbered steps each ending in
`Результат:`), Режимы установки скиллов (`:73`, three subsections plus
shadowing order at `:122`), Дальше (`:126`, five links). `README.en.md` is
linked at `:8`, first screen.

Blacklist sweep clean. `grep -n '[«»—–]' README.md` and
`grep -nP "[\x{2010}-\x{2015}\x{2212}\x{2E3A}\x{2E3B}]" README.md` both return
nothing. No filler openers, no marketing register, no antithesis constructions,
no closing restatement. The only `!` hits are markdown badge syntax at `:3-6`.
Enumerations stay inline within the two-to-four item allowance; reasoning stays
in prose and the two lists carry parallel facts only. Russian prose names the
actor throughout (установщик копирует, команда записывает, инструмент фиксирует)
and prefers verbs to verbal nouns.

`README.ru.md` stays deleted (`git diff --cached --stat` shows 749 deletions).
No repo file links to it; the only remaining mentions are historical records in
`LOGBOOK.md` and `.spec/docs-refresh.md`.

Commands re-verified at runtime: `csk --version` (0.13.1.dev2), `csk add NAME
--git ... --tag ...`, `csk global add NAME --git ... --tag ...`,
`csk global install`, `csk hybrid add NAME --git ... --tag ... --target ALIAS`
with `--target` documented as "project alias, absolute path, or path glob
(repeatable)".

Paths re-verified: hybrid manifest `~/.cocoaskills/hybrid/Skillfile.json`
(`hybrid.py:18-19,32-38`), global root `~/.cocoaskills/global/`
(`global_install.py:56-57`), project context `.agents/skills/<name>/`.
Shadowing order project > hybrid > global matches `installer.py:388-402` for
the project-over-hybrid half and `ARCHITECTURE.md:131-132` ("Project, hybrid,
and global activation shadow in that order") for the full order.

Link targets `ARCHITECTURE.md`, `SECURITY.md`, `docs/skill-authoring.md`,
`CHANGELOG.md` all exist.

Tests green: `uv run pytest -q` gives 1418 passed, 243 skipped, 0 failed
(213.7s). Log at `.temp/TASK-260819-8a0q6y/pytest-review4-01.log`.

## Non-blocking notes for the follow-up tasks

`README.md:16` is missing the comma that closes the деепричастный оборот before
the next homogeneous predicate: "...и метаданные git и удаляет устаревшие
файлы..." should read "...и метаданные git, и удаляет устаревшие файлы...".
The sentence also carries three predicates plus an inserted participial clause
and would read better split in two. Punctuation nit, not a blacklist hit and
not a factual error; belongs to the `slop-audit` task in the execution plan.

`README.en.md` does not exist yet, so the first-screen link at `README.md:8`
and the install-matrix pointer at `README.md:38` are dead. Expected by the
execution plan ordering; the follow-up `en-readme` task lands the file. The
story must not ship to a public branch before that task completes.

`pyproject.toml:9` still points `readme` at `README.md`, so PyPI would render
Russian today. Switching it to `README.en.md` belongs to the `en-readme` task
per `.spec/docs-refresh.md`.

## Acceptance evidence for the commit-owning mover

Scope of this task in the working tree: `README.md` modified (66 insertions,
770 deletions), `README.ru.md` deleted (749 deletions, staged). No commit was
made by this review. The commit-owning mover commits this scope, then makes the
final Story/Epic `done` transition with `commit_ack=scope_committed`.
