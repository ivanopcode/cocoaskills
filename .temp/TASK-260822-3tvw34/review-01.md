# TASK-260822-3tvw34 review 1: changes requested

Scope reviewed: `README.md` and `CHANGELOG.md` in the working tree of
`/Users/iv/Developer/Wildberries/cocoaskills`. Evidence log:
`.temp/TASK-260822-3tvw34/verification-01.log`. Test log:
1465 passed, 244 skipped in 346s (`.venv/bin/pytest -q`).

## Verdict

Changes requested. The whole CHANGELOG half of the task is correct and verified
against git; one factual claim in the new README quickstart contradicts
`docs/skill-authoring.md` and the manifest schema, and one verb misdescribes what
the precheck does. Both are single-sentence fixes in `README.md:146`. Nothing
else needs rework.

## W1 (blocking): the quickstart defines compiled commands as always external

`README.md:146` opens with:

    Скиллы со скомпилированными командами собираются из отдельно
    запиненных git-репозиториев.

That states the external build repository as the universal case. It is one of
two forms. `docs/skill-authoring.md:367-385` documents the base form: schema 6
declares `build_roots` inside the skill package and a build command
`{"type":"build","driver":"go-v1","source_dir":"..."}` whose `source_dir` lives
inside one of those roots. `docs/reference.md:142` says the same
("Схема v6: добавляет компилируемые команды и исключение файлов сборки через
`build_roots`"). The external pinned Git source arrived with schema 7 and is
optional: `docs/skill-authoring.md:367` reads "Скиллы схемы 7 могут также
выбирать зафиксированный внешний источник Git".

Line 146 is the only place in `README.md` that describes compiled commands at
all (`grep -n "скомпилир\|компилир" README.md` returns exactly this line), so it
is the definition a reader takes away. Prose style requires the definition at
first use to be the correct one.

Fix, one sentence, keeps the section inside the 6-8 line budget:

    Скомпилированные команды скилла могут собираться из отдельно
    запиненного git-репозитория.

## W2 (blocking): "считывает" misnames what the precheck does

Same line: "при первой установке `csk` считывает обнаруженные варианты". The
precheck discovers the candidates and prints them as a menu.
`src/csk/installer.py:961-965` prints `Detected candidates:`, one numbered line
per option with `<- default` on the first, then reads one selection. "считывает
обнаруженные варианты" is circular and drops the observable behaviour. The owner
TZ draft had the right verb ("сам покажет обнаруженные варианты").

Fix: `показывает обнаруженные варианты`.

## N1 (note, no action required): "за один Enter" is two Enters

The heading comes from the owner TZ verbatim and the task description names it,
so it stays. Recording the fact for whoever revisits it: the happy path is two
prompts, not one. `src/csk/installer.py:965` reads the candidate choice
(`Select [1-N, empty=1, n=abort]`), `:986-991` then reads the persistence scope
(`[1] <namespace> (default)`). The commit message of `2bb2772` scoped the phrase
to the candidate prompt only. The README body is honest about both steps
("выбираете вариант ... указываете скоуп"), so no reader is misled about the
mechanics.

## N2 (note): passive without an actor in the CI line

"В CI то же самое задаётся заранее" hides the actor, and the actor is the point
of the closing sentence ("выбор делает только оператор"). `docs/prose-style.md`
allows the passive only where the agent is irrelevant. Optional: "В CI оператор
задаёт то же самое заранее". Not blocking.

## Accepted and verified

### CHANGELOG rehoming by tag containment

The producer put every former `Unreleased` entry under a new
`## [0.13.0] - 2026-08-08` heading. That is correct, checked two ways.

Forward: `v0.13.0..v0.14.0` contains exactly eight commits, and the only
non-CI/non-docs ones are `e66c0c8` (go-v1 install blockers), `b2d3d14`
(build-SSH scopes) and `d8d3529` (their tests). Nothing else could have landed
between the tags, so no ex-Unreleased entry can belong to 0.14.0.

Backward, by first commit and `git tag --contains`:

- `--build-ssh-identity` surface -> `76e07f07` -> earliest tag `v0.13.0`
- `CSK_GO_FINGERPRINT_TIMEOUT` -> `15401942` -> earliest tag `v0.13.0`
- `manager-worker-v1` policy -> `495ad021` -> earliest tag `v0.13.0-rc.1`

The `--build-ssh-*` flags under 0.13.0 and the `build_ssh` config scopes under
0.14.0 are distinct changes, not a duplicated entry.

### CHANGELOG 0.14.0 and 0.14.1 claims

Every claim traces to a commit reachable from its tag:

- launcher canonicalization, `lib64 -> lib` alias, closure manifest naming,
  `toolchain_executable_mismatch` hint -> `e66c0c8` (in `v0.13.0..v0.14.0`),
  each named in its commit body
- vendor advisory downgrade, `config.add_project` via `dataclasses.replace`,
  `build_ssh` scopes, `csk config build-ssh add/list/remove`, precheck,
  `--dry-run` source table, bootstrap question -> `b2d3d14` (same range)
- 0.14.1 candidate discovery -> `2bb2772`, in `v0.14.0..v0.14.1`

### Dates and links

Headings match the annotated tag dates exactly, which is this file's existing
convention: `v0.13.0` 2026-08-08, `v0.14.0` 2026-08-21, `v0.14.1` 2026-08-22
(`git for-each-ref --format='%(taggerdate:short)'`). Comparison links form an
unbroken chain `v0.12.5...v0.13.0...v0.14.0...v0.14.1...main` with no duplicate
or orphaned definitions.

`Unreleased` is empty and that is correct: `git log v0.14.1..HEAD` is empty.

### README structure and the CLI example

`### Приватные репозитории сборки: за один Enter` sits at `README.md:144` under
`## Быстрый старт`, correct nesting. The TZ draft's em-dash became a colon,
which `docs/prose-style.md` requires. The new content carries no em-dash, no
en-dash and no guillemets (0 matches over `README.md:144-153` and
`CHANGELOG.md:8-30`).

The command block is byte-identical to the parser's own epilog example
(`src/csk/cli.py:259-260`) and runs on live csk 0.14.1: `--agent` is
`nargs="?", const="auto"`, so `--agent auto` is valid, and `--identity PATH`
accepts the `.pub`.

The troubleshooting link landed at `README.md:291` in `## Дальше`, formatted
like its siblings, and `docs/troubleshooting.md` exists in the tree (4571 bytes,
untracked, owned by sibling task TASK-260822-1jns5p).

## Not blocking, out of this task's scope

`docs/reference.md` and `docs/troubleshooting.md` say "компилируемые команды"
while `docs/skill-authoring.md` says "скомпилированные команды". The README
follows `skill-authoring.md`. The split predates this task; whoever owns the
docs-wide terminology pass should pick one.

## Commit scope for the mover

`README.md` (lines 144-154 and 291) and `CHANGELOG.md` after W1 and W2 are
fixed. Every other modified path in the tree belongs to sibling tasks of
STORY-260822-318s44. This reviewer run supplied no `commit_ack`.

## Verification commands for the next producer

    cd /Users/iv/Developer/Wildberries/cocoaskills
    sed -n '144,154p' README.md
    grep -n "скомпилир\|компилир" README.md docs/skill-authoring.md docs/reference.md
    sed -n '955,970p' src/csk/installer.py
    sed -n '144,153p' README.md | grep -c $'[—–«»]'   # must print 0
