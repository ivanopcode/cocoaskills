# TASK-260819-8a0q6y ru-root-readme: review verdict 3 (RUN-260819-1011a0)

Verdict: changes requested. Route to `to-dev`.

Reviewed: `README.md` (134 lines) after the second rework, against
`src/csk/whitelist.py`, `src/csk/adapters.py`, `src/csk/cli.py`,
`src/csk/hybrid.py`, `src/csk/global_install.py`, `src/csk/installer.py`,
`src/csk/shims.py`, `tests/test_adapters.py`, `tests/test_hybrid_scope.py`,
`tests/test_whitelist.py`, plus runtime checks of `csk init`, `csk add`,
`csk add --help`, `csk global add --help`, `csk global install --help`,
`csk hybrid add --help`, direct calls into `whitelist.copy_context` and
`adapters.plan_project_adapter_targets`, and the full suite.

## Previous blocking findings: all fixed

### Verdict 1 finding 1, `csk install --global` — FIXED

`grep -n "csk install --global" README.md` returns nothing (exit 1).
`README.md:108` reads `csk global install`, which exists and accepts the
documented behaviour.

### Verdict 1 finding 2, `csk init` gitignore block — FIXED

`README.md:47` lists the block verbatim. Re-verified by running `csk init` in a
fresh temp git repo:

```
# CocoaSkill
.agents/
.claude/skills/
.codex/skills/
.cursor/rules/
.gemini/skills/
Skillfile.dev.json
```

The written manifest matches the step's description and the JSON example at
`README.md:84-95`:

```json
{ "schema_version": 1, "project": { "alias": "rv3" },
  "agents": ["codex_cli", "claude_code"], "skills": [] }
```

### Verdict 2 finding 3, hybrid adapter placement — FIXED

`README.md:112` now reads:

> Установщик создаёт адаптеры в директориях агентов проекта (`.claude/skills/`,
> `.codex/skills/`) и шимы команд в `.agents/bin/`, но не требует коммитов в
> git-репозиторий.

This matches `tests/test_hybrid_scope.py:88-93`, which asserts the hybrid skill
is absent from `project/.agents/skills/`, present at
`project/.claude/skills/skill-conventions/SKILL.md`, and shimmed at
`project/.agents/bin/brief`.

## New blocking findings

### 1. The whitelist is not an extension list (README.md:16 and README.md:24)

The README states the mechanism twice, in the two load-bearing paragraphs:

- `README.md:16`: "изолирует файлы скиллов по списку разрешённых расширений"
- `README.md:20`: "фильтрует содержимое по списку разрешённых расширений"

`src/csk/whitelist.py` never inspects a file extension. `copy_context` selects
by top-level root name from `INCLUDE_ROOTS` (`SKILL.md`, `agents`, `references`,
`.skill_triggers`, `assets`, `templates`, `examples`, `data`, plus `scripts`
when `include_scripts` is set) and then drops paths matching `ALWAYS_EXCLUDED`
name patterns (`tests`, `README*`, `CHANGELOG*`, `.git`, `Makefile`, and so on).
Selection is by path, not by suffix.

Demonstrated directly against `whitelist.copy_context` with a synthetic
snapshot:

```
$ uv run python -c "...copy_context(snap, dest)..."
copied: ['SKILL.md', 'assets/tool.sh', 'data/table.csv',
         'references/data.bin', 'references/helper.py']
```

Input files were `SKILL.md`, `references/helper.py`, `references/data.bin`,
`assets/tool.sh`, `data/table.csv`, `docs/guide.md`, `tests/test_a.py`,
`README.md`, `notes.md`.

The result contradicts the documented rule in both directions. Four files with
extensions no reader would call "allowed for prompt context" (`.py`, `.bin`,
`.sh`, `.csv`) are copied, because they sit under an included root. Two `.md`
files (`docs/guide.md`, `notes.md`) are dropped, because they sit outside every
included root. A reader who structures a skill by extension gets the wrong
layout.

This is a regression introduced by the rewrite. The pre-rewrite `README.md:36`
described the mechanism correctly: "A whitelist-based stripped layout: README,
tests, build files, and other ...". `ARCHITECTURE.md:46-50` also describes it as
stripping repository assets, not extensions.

Fix: describe the whitelist by what it selects, for example "копирует в контекст
агента только `SKILL.md` и объявленные каталоги (`references/`, `assets/`,
`agents/`, `data/`), исключая `tests`, `README`, файлы сборки и метаданные git".
Correct both `README.md:16` and `README.md:20`.

### 2. OpenCode and Windsurf receive no adapters (README.md:24)

The README states:

> Установщик `csk` хранит единый файл `Skillfile.json` в проекте и раскладывает
> скиллы по адаптерам Claude Code, Codex CLI, Cursor, Gemini, OpenCode и
> Windsurf.

`AGENT_PATHS` (`src/csk/adapters.py:15-20`) covers four agents. `windsurf` and
`opencode` are `NATIVE_DISCOVERY_AGENTS` (`src/csk/adapters.py:25`), documented
in the source comment as agents that "discover the canonical `.agents/skills/`
directory natively" and "need no project-level mirror".
`plan_project_adapter_targets` (`src/csk/adapters.py:161-171`) builds its roots
from `AGENT_PATHS.items()` only, so those two agents can never produce a project
adapter target.

Verified by planning a project install with all six agents requested:

```
planned adapter targets: ['.claude/skills/skill-tracker',
 '.claude/skills/.csk-managed.json', '.codex/skills/skill-tracker',
 '.codex/skills/.csk-managed.json', '.cursor/rules/skill-tracker',
 '.cursor/rules/.csk-managed.json', '.gemini/skills/skill-tracker',
 '.gemini/skills/.csk-managed.json']
gitignore entries: ['.agents/', '.claude/skills/', '.codex/skills/',
 '.cursor/rules/', '.gemini/skills/']
```

`tests/test_adapters.py:73-82` asserts the same contract:

```python
adapters.refresh_adapters(project, ["opencode", "windsurf"], ["skill-a"], "copy")
assert not (project / ".opencode").exists()
assert not (project / ".windsurf").exists()
assert adapters.required_gitignore_entries(["opencode", "windsurf"]) == [".agents/"]
```

A Windsurf or OpenCode user reading `README.md:24` looks for an adapter
directory for their agent and finds none. The definition at `README.md:10`
("подготавливает файлы для шести сред") is accurate and needs no change; only
the adapter claim at `README.md:24` overstates the mechanism.

Fix: name the four adapter directories and state that OpenCode and Windsurf read
the canonical `.agents/skills/` root directly, for example "раскладывает скиллы
по адаптерам Claude Code, Codex CLI, Cursor и Gemini, а OpenCode и Windsurf
читают канонический каталог `.agents/skills/` напрямую".

## Non-blocking notes

`README.md:28` claims CocoaSkills "не исполняет код скиллов во время работы
агента". `README.md:63` promises executable shims in `.agents/bin/`, and
`ARCHITECTURE.md:28-31` states "Agents and humans execute these shims
explicitly". The claim is defensible: the shim is a plain `exec <target> "$@"`
(`src/csk/shims.py:914-921`), so `csk` itself is not in the runtime path, and
the spec asked for "not an agent runtime". A tighter wording ("не выступает
runtime-средой для агента") removes the apparent contradiction with the step
four result two paragraphs earlier.

`README.en.md` does not exist yet, so the first-screen link at `README.md:8` is
dead. Expected by the execution plan ordering; the follow-up `en-readme` task
lands it.

## What passes

Structure follows `.spec/docs-refresh.md` exactly: definition, Зачем, Почему
CocoaSkills а не альтернативы (four alternatives, one paragraph each, closing
"what it is not"), Быстрый старт (five numbered steps, each with `Результат:`),
Режимы установки скиллов (three modes plus shadowing order), Дальше (five
links).

Blacklist sweep clean. `grep -n '[«»—–]' README.md` and
`grep -nP "[\x{2010}-\x{2015}\x{2212}\x{2E3A}\x{2E3B}]" README.md` both return
nothing. No antithesis constructions, no filler openers, no marketing register,
no closing restatement. Enumerations stay within the two-to-four inline limit;
reasoning stays in prose and lists carry parallel facts only. Russian prose names
the actor throughout and prefers verbs to verbal nouns.

`README.ru.md` stays deleted. The only remaining mentions are historical records
in `LOGBOOK.md` and `.spec/docs-refresh.md`, which are not links.

Commands re-verified at runtime: `csk add NAME --git ... --tag ...`,
`csk global add NAME --git ... --tag ...`, `csk global install`,
`csk hybrid add NAME --git ... --tag ... --target ALIAS` (with `--target`
documented as "project alias, absolute path, or path glob (repeatable)"),
`csk --version`. `csk add` writes exactly the object shape shown at
`README.md:88-94`.

Paths re-verified: hybrid manifest `~/.cocoaskills/hybrid/Skillfile.json`
(`hybrid.py:18-19,30-35`), global root `~/.cocoaskills/global/`
(`global_install.py:56-57`), project context `.agents/skills/<name>/`
(`ARCHITECTURE.md:25-27`). Shadowing order project > hybrid > global matches
`installer.py:388-402` for the project-over-hybrid half and the pre-rewrite
`README.md:325` for the full order.

Link targets `ARCHITECTURE.md`, `SECURITY.md`, `docs/skill-authoring.md` and
`CHANGELOG.md` all exist.

Tests green: `uv run pytest -q` gives 1418 passed, 243 skipped, 0 failed
(211.2s). `pyproject.toml` still points `readme` at `README.md`; switching it to
`README.en.md` belongs to the follow-up `en-readme` task.

## Required for acceptance

Fix findings 1 and 2, then return for another review cycle. No commit was made
by this review; acceptance evidence goes to the commit-owning mover.
