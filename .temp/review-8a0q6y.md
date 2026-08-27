# TASK-260819-8a0q6y ru-root-readme: review verdict

Verdict: changes requested. Route to `to-dev`.

Reviewed: `README.md` (rewritten, 887 words), deletion of `README.ru.md`,
repo-wide link sweep, CLI surface in `src/csk/cli.py`, `src/csk/adapters.py`,
`src/csk/hybrid.py`, `src/csk/manifest.py`, `src/csk/installer.py`,
`src/csk/shims.py`, full test suite.

## What passes

Structure follows the spec outline in `.spec/docs-refresh.md` exactly:
definition, Зачем, Почему CocoaSkills а не альтернативы (four alternatives,
one paragraph each, closing "what it is not"), Быстрый старт (five numbered
steps), Режимы установки скиллов (three modes plus shadowing order), Дальше
(five links). `README.en.md` is linked in the first screen.

Style blacklist sweep is clean. Zero guillemets, zero em-dashes or en-dashes
(`grep -n '[«»—–]' README.md` returns nothing), no filler openers, no
marketing register, no antithesis constructions, no closing restatement. Lists
carry parallel enumerable items only; reasoning stays in prose. Russian prose
names the actor throughout (установщик копирует, команда записывает) and uses
verbs rather than verbal nouns.

`README.ru.md` is deleted (`git status` shows `D`). No repo file links to it.
The only remaining mentions are historical records in `LOGBOOK.md` and
`.spec/docs-refresh.md`, which are not links.

Hybrid mode is documented as shipped, with a worked `csk hybrid add` example.
The example matches the parser: `--git`, one of `--tag`/`--branch`/`--revision`
(mutually exclusive, required), `--target` (repeatable, required) accepting
alias, absolute path, or glob (`src/csk/cli.py:188-200`). The manifest path
`~/.cocoaskills/hybrid/Skillfile.json` matches `src/csk/hybrid.py:36`.
Shadowing order project > hybrid > global matches
`src/csk/installer.py:394-396` and the pre-rewrite README.

Facts verified against code: `schema_version: 1` matches
`src/csk/manifest.py:13`; the `project.alias` / `agents` / `skills` shape
matches `ensure_project_manifest`; six agent environments match
`AGENT_PATHS` plus `NATIVE_DISCOVERY_AGENTS` in `src/csk/adapters.py:15-25`;
`csk add NAME --git ... --tag ...` matches `src/csk/cli.py:170-177`;
`csk global add skill-metrics --git ... --tag ...` matches
`src/csk/cli.py:477-484`; the global root `~/.cocoaskills/global/` matches the
`csk global` epilog.

Tests green: `uv run pytest -q` gives 1418 passed, 243 skipped, 0 failed
(242s). No README-dependent test regressed; `pyproject.toml` still points
`readme` at `README.md`, and switching it to `README.en.md` belongs to the
follow-up `en-readme` task.

## Blocking findings

### 1. `csk install --global` does not exist (README.md, Глобальный режим)

The README states:

> Команда `csk install --global` скачивает репозиторий и записывает адаптеры
> в пользовательские директории агентов в домашнем каталоге.

`csk install` has no `--global` flag. Verified at runtime:

```
$ csk install --global --help
usage: csk install [-h] [--all] [--dry-run] [--verbose] [--fix-gitignore]
                   [--strict-tags] [--audit [{advisory,strict}]] [target]
```

The parser is defined in `_add_install` (`src/csk/cli.py:369-406`) and declares
no global selector. The correct command is `csk global install`
(`src/csk/cli.py:502`), also shown in the subcommand epilog at
`src/csk/cli.py:470`. A reader copy-pasting the documented command gets an
argparse error. Fix: replace with `csk global install`.

### 2. `csk init` observable result misstates the gitignore block (README.md, step 2)

The README states the step "добавляет пути `.agents/` и `.claude/` в файл
`.gitignore`". `csk init` calls `adapters.all_gitignore_entries()`
(`src/csk/cli.py:870-873`), which emits an entry for every known agent path
regardless of the selected agents. Verified by running `csk init` in an empty
git repo:

```
# CocoaSkill
.agents/
.claude/skills/
.codex/skills/
.cursor/rules/
.gemini/skills/
Skillfile.dev.json
```

Two errors: the entry is `.claude/skills/`, not `.claude/`, and five other
entries are omitted. The step also does not mention that `csk init` writes the
`agents` list into the manifest (default `codex_cli`, `claude_code` from
config). Fix: state the block as written, or describe it as a CocoaSkills
gitignore block covering `.agents/` and the per-agent adapter directories.

### 3. Hybrid mode claim contradicts the installer (README.md, Гибридный режим)

The README states:

> Установщик не вносит изменений в файлы проекта и не требует коммитов в
> git-репозиторий.

The first half is wrong. A hybrid install materializes prompt context once
under `~/.cocoaskills/hybrid/skills/`, then reaches the project through managed
adapter links, and command shims land in the project `.agents/bin`
(`src/csk/shims.py:447-448`, project bin dir; pre-rewrite README, Hybrid
skills). Files inside the project tree are written; they are gitignored, so
nothing is committed. The spec wording is "nothing is committed to target
repos". Fix: keep the commit claim, drop or correct the "no changes to project
files" claim.

## Non-blocking notes

Step 4 lists "создает исполняемые шимы в `.agents/bin/`" as an unconditional
observable result. Shims exist only for skills that declare commands in
`agent-skill.json`; a plain instruction skill produces none. Consider
qualifying it.

The opening definition paragraph enumerates four capabilities (loading, content
hash build, transitive dependencies, adapter layout). The spec asks for "no
feature list" in section 1. Borderline; readable as-is, but a tighter first
paragraph would match the spec more closely.

The link to `README.en.md` in the first screen is dead until the follow-up
`en-readme` task lands. Expected by the execution plan ordering, flagged so the
story is not accepted with a broken link in the wild.

## Required for acceptance

Fix findings 1, 2, and 3, then return for another review cycle. No commit was
made by this review; acceptance evidence goes to the commit-owning mover.
