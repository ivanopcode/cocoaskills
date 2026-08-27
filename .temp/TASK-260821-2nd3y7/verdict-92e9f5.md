# TASK-260821-2nd3y7 review verdict: accepted

Reviewer run: RUN-260821-92e9f5. Date: 2026-08-21.
Verdict: **accepted** -> `done`. No `commit_ack` supplied (reviewer archetype).
Previous verdict: RUN-260821-28ccd5 (changes_requested, B1-B4 blocking).

Evidence log: `.temp/TASK-260821-2nd3y7/review-92e9f5-evidence.log`
(also attached as `TASK-260821-2nd3y7_review-evidence-92e9f5.log`).

## Scope reviewed

Working tree `/Users/iv/Developer/Wildberries/cocoaskills`, untracked
`docs/cli.md` (798 lines, was 744) and `README.md` `## Команды`
(lines 199-271, before `## Дальше`).

## Prior blocking findings: all fixed

**B1 grammar.** `grep -n 'приватного ключу\|проверка аудита перед'` returns
zero hits. Every `--build-ssh-identity` entry now reads "путь к приватному
ключу SSH"; every `--audit` entry reads "запускает проверку аудита перед
установкой".

**B2 term spelling.** `grep -n 'скилы\|скилов\|скилам'` returns zero hits.
The document uses "скилл"/"скиллы" throughout.

**B3 `csk --version`.** New `### csk` section at line 9 documents the entry
point: synopsis `usage: csk [-h] [--version] {bootstrap,init,...} ...`,
`-h`/`--help`, `--version` ("выводит версию csk и завершает работу"), and the
example `csk --version`. Matches live top-level help verbatim.

**B4 shared flags.** `--dry-run`, `--verbose`, `--strict-tags`, `--audit`,
and the three `--build-ssh-*` flags now carry byte-identical descriptions in
all four commands (`install` 235-242, `upgrade` 293-300, `global install`
477-484, `global upgrade` 534-541). Defaults and env vars survive in every
copy: `--audit` states `(по умолчанию: advisory)` in all four; each
`--build-ssh-*` entry keeps `CSK_BUILD_SSH_IDENTITY`, `CSK_BUILD_SSH_AGENT`,
`CSK_BUILD_SSH_KNOWN_HOSTS`, plus the `~/.ssh/known_hosts` default. The
narrowed paraphrase "выводит расширенный лог сборок" is gone; all four say
"выводит подробный ход выполнения". A script diffing every `default:`/`env:`
token in live help against the corresponding doc section reports no losses.

**N1 ё.** Consistent: "отчета"/"отчет" only, "рассчитывает" only. No mixed pairs.

**N2 gc snapshots.** Line 688 now reads "Удаляет неиспользуемые
runtime-директории, snapshot-записи, просроченный кэш сборок (старше 24
часов) и устаревшие записи реестра клиентов."

## Independent verification this round

**Synopsis fidelity: 29 of 29 verbatim, 0 mismatches.** A script extracted
every `**Синопсис:**` block and compared it, whitespace-normalized, against
the `usage:` block of the matching `.venv/bin/csk ... --help`.

**Command coverage: complete.** The CLI exposes 28 leaf commands (12
top-level, `skill check`, 8 `global`, 4 `hybrid`, 2 `project`, `config show`).
`docs/cli.md` documents all 28 plus the `csk` entry point; the README groups
list all 28. Note: `csk shell-init {auto,zsh,bash,powershell}` is a positional
choice, not a subcommand group, and the doc correctly treats it as such.

**Flag coverage: bidirectional, no gaps.** For every section, the set of
`--flags` in live help equals the set documented. The single reported delta
(`csk update` gaining `--all/--prune/--tags`) is a false positive from the
help's prose line "git fetch --all --tags --prune"; `csk update` really takes
only `-h`.

**Factual spot checks: pass.** Exit codes 0/1/2/3 match `EXIT_OK`,
`EXIT_PARTIAL_FAIL`, `EXIT_CONFIG`, `EXIT_LOCK` at `src/csk/cli.py:39-42`.
`csk status --check` returning 1 matches `src/csk/cli.py:620`.
`--build-ssh-known-hosts` default and `CSK_REGISTRY_TOKEN` match live help.

**Typography and blacklist: pass.** Zero em-dashes, en-dashes, or guillemets
in `docs/cli.md` and in `README.md:199-272`. No antithesis constructions,
filler openers, marketing adjectives, or closing summary paragraph.

**README section: pass.** Five `<details>`/`<summary>` blocks, well formed,
summaries exactly `Проект`, `Скиллы и зависимости`, `Global и Hybrid`,
`Сборки и аудит`, `Сервисные`; each a bash code block of `command  # что делает`
lines; link `[docs/cli.md](docs/cli.md)` present; placed before `## Дальше`.

**Tests green.** `.venv/bin/python -m pytest tests/test_release_contract.py
tests/test_cli.py -q` -> `76 passed, 24 warnings in 11.51s`, exit 0.

## Non-blocking findings

**NB1. `--only` still rotates three ways, and one variant is wrong.**
Live help gives `--only NAME` the identical text in `global install`,
`global update`, and `global upgrade`: "restrict the operation to this
declared global skill (repeatable); its required skills still join the
closure, every other installed skill is left untouched". The doc paraphrases
it three ways:

- 481 (`global install`): ограничивает операцию указанным глобальным скиллом (повторяемый флаг).
- 506 (`global update`): ограничивает операцию указанным скиллом (повторяемый флаг).
- 539 (`global upgrade`): обновляет только один указанный скилл (повторяемый флаг).

Line 539 contradicts itself in one sentence (only one skill, repeatable) and
narrows the semantics. Fix: reuse line 481's wording in all three, and
consider adding the closure behavior, which all three currently drop.
Below the blocking bar: the flag exists, the synopsis is verbatim, and the
"(повторяемый флаг)" fact survives everywhere. Fold into the next docs touch.

**NB2. Minor wording drift on equivalent entries.** `--revision` is "хеш
коммита git-репозитория" under `csk add` and "хеш коммита" under
`global add`/`hybrid add`; `--source` is "имя локальной директории источника
в каталоге `skills_root`" vs "имя локальной директории в `skills_root`".
Harmless abbreviation, but the same one-flag-one-description rule applies.
`--all`, `--check`, and `--json` legitimately differ per command because live
help differs.

**NB3. Round-2 evidence was not recorded.** `TASK-260821-2nd3y7_results.md`
still carries round-1 numbers ("17 top-level commands", "65 passed in
25.85s") and was not updated by the rework run; `LOGBOOK.md` has no round-2
entry for this task. The mandatory tooling note requires grep/head
verification output in the outcome resource. Not blocking, because this
review independently verified every edit landed in the working tree, but the
producer should refresh the artifact. This reviewer's evidence log is
attached in its place.

## Handoff to the commit-owning mover

`docs/cli.md` and the `README.md` `## Команды` section are accepted as is.
Commit scope for this task: `docs/cli.md` (new file) and the `README.md`
`## Команды` block. The `## Дальше` list still links `README.en.md`; that is
`drop-en-readme`'s scope, not a defect here.
