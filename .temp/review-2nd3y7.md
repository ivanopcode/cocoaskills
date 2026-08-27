# TASK-260821-2nd3y7 review verdict: changes requested

Reviewer run: RUN-260821-28ccd5. Date: 2026-08-21.
Verdict: **changes_requested** -> `to-dev`.

## What was verified

Working tree `/Users/iv/Developer/Wildberries/cocoaskills`, untracked
`docs/cli.md` (744 lines) and the `## Команды` section in `README.md`
(lines 199-271, placed before `## Дальше`).

**Synopsis fidelity: passes.** A machine diff extracted all 28 `**Синопсис:**`
blocks from `docs/cli.md` and compared each, whitespace-normalized, against the
`usage:` line of the corresponding `.venv/bin/csk ... --help` invocation.
Result: `mismatches: 0 of 28`. Every documented synopsis is verbatim.

**Command coverage: passes for leaf commands.** The CLI exposes 28 leaf
commands (12 top-level, `skill check`, 8 `global`, 4 `hybrid`, 2 `project`,
`config show`). `docs/cli.md` documents 28; the README `Команды` groups list
the same 28 across the five required groups. All five `<details>`/`<summary>`
blocks are well formed and the `docs/cli.md` link is present.

**Typography: passes.** `grep -n '—\|–\|«\|»'` returns zero hits in
`docs/cli.md` and zero in `README.md:199-272`.

**Factual spot checks: pass.** Exit codes 0/1/2/3 match `csk install --help`.
The `csk gc` 24-hour build-cache grace is real: `BUILD_GRACE_SECONDS = 24 * 60 * 60`
at `src/csk/gc.py:28`. `CSK_REGISTRY_TOKEN` matches `csk audit --help`.

**Tests: green.** `.venv/bin/python -m pytest tests/test_release_contract.py
tests/test_cli.py -q` -> `76 passed, 24 warnings in 16.29s`, exit 0.

## Blocking findings

### B1. Broken Russian grammar in three flag descriptions

- `docs/cli.md:263` and `docs/cli.md:494`: "путь к приватного ключу SSH".
  Wrong case agreement. Should be "путь к приватному ключу SSH", or better,
  the same wording used at line 209.
- `docs/cli.md:492`: "запускает проверка аудита перед установкой". Wrong case
  on the object. Line 208 has the correct form: "запускает проверку аудита".

These are in a document whose acceptance criterion binds it to
`docs/prose-style.md`. Ungrammatical published prose fails that.

### B2. The document misspells its own defined term

- `docs/cli.md:104`: "проверяет установленные скилы через доверенные реестры аудита".
- `docs/cli.md:425`: "Устанавливает глобальные скилы из `~/.cocoaskills/global/Skillfile.json`".

Everywhere else the document uses "скилл"/"скиллы". `docs/prose-style.md`
requires a term to be defined once and then repeated verbatim; two occurrences
of "скилы" break that and read as typos.

### B3. `csk --version` is dropped, so the absorption is incomplete

The task requires `docs/cli.md` to absorb and supersede the CLI table in
`README.en.md`. That table documents `csk --version` (`README.en.md:378`), and
`--version` is in the live top-level help:

```
usage: csk [-h] [--version] {bootstrap,init,...} ...
options:
  -h, --help  show this help message and exit
  --version   print csk version and exit
```

`grep -n version docs/cli.md` returns nothing. `docs/cli.md` has no section for
the `csk` entry point itself and no `--version` entry. `README.md:111` already
tells the reader to run `csk --version` in the quick start, so the reference
does not document a command the README instructs the reader to run.

This matters now because the next task in the story (`drop-en-readme`) deletes
`README.en.md`. Once that lands, `--version` is documented nowhere.

Fix: add a short top-level section (for example `### csk`) with the synopsis
`csk [-h] [--version] <command> ...`, `-h`/`--help`, and `--version`.

### B4. Shared flags are paraphrased four different ways, and defaults and env vars are lost

`--dry-run`, `--verbose`, `--strict-tags`, `--audit`, and the three
`--build-ssh-*` flags are identical across `install`, `upgrade`,
`global install`, and `global upgrade`. The document gives each a different
Russian description in each of the four places:

| Flag | install (204-211) | upgrade (258-265) | global install (436-443) | global upgrade (489-496) |
| --- | --- | --- | --- | --- |
| `--dry-run` | рассчитывает план действий без изменения файлов на диске | выполняет планирование без перезаписи файлов на диске | выполняет проверку плана установки без записи файлов | выполняет расчет плана обновления без записи файлов |
| `--verbose` | выводит подробный ход установки и хеши коммитов | выводит детализированный лог выполнения | выводит подробный процесс генерации файлов | выводит расширенный лог сборок |
| `--strict-tags` | завершает работу ошибкой при локальном смещении тегов | завершает работу ошибкой при сдвиге тегов | блокирует установку при смещении тегов | блокирует установку при расхождении тегов |
| `--audit` | ... (по умолчанию: `advisory`) | default dropped | ... (по умолчанию: `advisory`) | default dropped, plus B1 grammar |
| `--build-ssh-identity` | ... (переменная: `CSK_BUILD_SSH_IDENTITY`) | env var dropped | env var dropped | env var dropped |
| `--build-ssh-agent` | ... (переменная: `CSK_BUILD_SSH_AGENT`) | env var dropped | env var dropped | env var dropped |
| `--build-ssh-known-hosts` | ... (переменная: `CSK_BUILD_SSH_KNOWN_HOSTS`) | env var dropped | env var dropped | env var dropped |

Three problems:

1. `docs/prose-style.md` forbids rotating synonyms. One flag, one description.
2. Information is lost. `csk upgrade --help` and `csk global upgrade --help`
   both print `(default mode: advisory)`; the document omits it. All four
   commands print `env: CSK_BUILD_SSH_IDENTITY`, `env: CSK_BUILD_SSH_AGENT`,
   `env: CSK_BUILD_SSH_KNOWN_HOSTS`; the document keeps them only under
   `install`. `.spec/docs-feedback-round2.md` directive 6 makes `docs/cli.md`
   the single source for flags, so a reader who lands on `csk global upgrade`
   never learns the env vars exist.
3. One paraphrase narrows the meaning. `csk global upgrade --verbose` prints
   `print detailed progress`, not build logs; "выводит расширенный лог сборок"
   at line 490 invents a scope the flag does not have.

Fix: write each shared flag once and reuse that exact sentence in all four
places, keeping the default and the env var in every copy. A shared
"Общие флаги установки" subsection referenced from the four commands also
works.

## Non-blocking findings

- **N1.** `ё` is used inconsistently: "отчёта" at line 619 vs "отчет" at 634;
  "рассчитывает" at 204 vs "расчет" at 489. Pick one convention.
- **N2.** The `csk gc` description at line 641 lists runtime entries, build
  cache, and registry entries. `csk --help` also lists snapshot entries:
  "Remove unreferenced runtime, snapshot, and protected build-cache entries
  plus dead consumer registry entries." Add snapshots.
- **N3.** `TASK-260821-2nd3y7_results.md` asserts "verified via `head`/`grep`"
  and "Automated prose style check script passed clean (0 errors)" but carries
  no command output. The task's mandatory tooling note requires the actual
  grep/head verification output in the outcome resource. B1 and B2 are exactly
  the class of defect a real style check would have surfaced, which suggests
  the check was not run against the final file.

## What is not in question

The structure, the group split, the synopsis extraction, the README section
placement and rendering, and the test suite are all correct. The rework is
confined to flag-description text in `docs/cli.md` plus one new top-level
section for `csk --version`. No README changes are required.
