# TASK-260819-8a0q6y ru-root-readme: review verdict 2 (RUN after rework)

Verdict: changes requested. Route to `to-dev`.

Reviewed: `README.md` (885 words) after the rework for RUN-260819-4f2d74,
against `src/csk/cli.py`, `src/csk/adapters.py`, `src/csk/hybrid.py`,
`src/csk/global_install.py`, `src/csk/installer.py`, `src/csk/shims.py`,
`tests/test_hybrid_scope.py`, plus runtime checks of `csk init`,
`csk global install --help` and `csk hybrid add --help`, plus the full suite.

## Previous blocking findings

### 1. `csk install --global` — FIXED

`grep -n "csk install --global" README.md` returns nothing (exit 1).
`README.md:108` now reads `csk global install`, which exists:

```
$ csk global install --help
usage: csk global install [-h] [--dry-run] [--verbose] [--strict-tags]
                          [--audit [{advisory,strict}]] [--only NAME] ...
```

The claim that the command writes adapters into the user home agent
directories matches `adapters.plan_global_adapter_targets`
(`src/csk/adapters.py:202-226`), which roots every adapter at
`Path.home() / AGENT_PATHS[agent]`.

### 2. `csk init` gitignore block — FIXED

`README.md:47` now lists the block verbatim. Verified by running `csk init`
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

The README enumerates exactly these entries. The written manifest
(`schema_version: 1`, `project.alias`, `agents: [codex_cli, claude_code]`,
`skills: []`) is covered by the step's "манифест `Skillfile.json` с начальной
конфигурацией проекта".

### 3. Hybrid mode claim — PARTIALLY FIXED, still blocking

The commit half is now correct: `README.md:112` says the installer does not
require commits to the target repository. The replacement clause introduces a
new factual error about placement:

> Установщик создаёт адаптеры и шимы в директории `.agents/` целевого проекта

Only the shims land in `.agents/`. Adapters for a hybrid skill land in the
per-agent directories, not in `.agents/`. `tests/test_hybrid_scope.py:91-93`
asserts exactly this:

```python
# Контекст не материализуется в дереве проекта, только линк в адаптере.
assert not (project / ".agents" / "skills" / "skill-conventions").exists()
assert (project / ".claude" / "skills" / "skill-conventions" / "SKILL.md").exists()
assert _shim(project, "brief").exists()   # project/.agents/bin/brief
```

The planner confirms the split: `installer.py:1285-1299` builds the hybrid
`AdapterGroup` with `canonical_root=hybrid_skills`
(`~/.cocoaskills/hybrid/skills/`) and hands it to
`adapters.plan_project_adapter_targets`, whose roots are
`project_root / AGENT_PATHS[agent]` (`.claude/skills/`, `.codex/skills/`,
`.cursor/rules/`, `.gemini/skills/`). Shims go to `project/.agents/bin`
(`src/csk/shims.py:447-448`, `tests/test_hybrid_scope.py:43-45`).

A reader following the current sentence looks for the hybrid skill under
`.agents/` and finds nothing there.

Suggested replacement for the second sentence of `README.md:112`:

> Установщик создаёт адаптеры в директориях агентов проекта (`.claude/skills/`,
> `.codex/skills/`) и шимы команд в `.agents/bin/`, но не требует коммитов в
> git-репозиторий.

## What passes

Structure follows `.spec/docs-refresh.md` exactly: definition, Зачем, Почему
CocoaSkills а не альтернативы (four alternatives, one paragraph each, closing
"what it is not"), Быстрый старт (five numbered steps, each with `Результат:`),
Режимы установки скиллов (three modes plus shadowing order), Дальше (five
links). `README.en.md` is linked on the first screen.

Blacklist sweep clean. `grep -n '[«»—–]'` and a Unicode dash sweep
(`grep -P "[\x{2010}-\x{2015}\x{2212}]"`) both return nothing. No antithesis
constructions, no filler openers, no marketing register, no closing
restatement. Lists carry parallel enumerable items only.

`README.ru.md` stays deleted; the only remaining mentions are historical
records in `LOGBOOK.md` and `.spec/docs-refresh.md`, which are not links.

Facts re-verified: `csk hybrid add` flags match `--git`, one of
`--tag`/`--branch`/`--revision`, repeatable `--target` accepting alias,
absolute path, or glob. Hybrid manifest path `~/.cocoaskills/hybrid/Skillfile.json`
matches `hybrid.py:18-19,30-35`. Global root `~/.cocoaskills/global/` matches
`global_install.py:56-57`. Shadowing order project > hybrid > global matches
`installer.py:394-401`. Six agent environments match `AGENT_PATHS` plus
`NATIVE_DISCOVERY_AGENTS` (`adapters.py:15-25`).

The non-blocking note from the previous cycle is addressed: step 4 now
qualifies shims as "для скиллов с командами".

Tests green: `uv run pytest -q` gives 1418 passed, 243 skipped, 0 failed
(216.7s).

## Still expected, not blocking

The first-screen link to `README.en.md` is dead until the follow-up
`en-readme` task lands. Expected by the execution plan ordering.

## Required for acceptance

Fix the placement clause in `README.md:112`, then return for another review
cycle. No commit was made by this review.
