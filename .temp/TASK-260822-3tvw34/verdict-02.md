# TASK-260822-3tvw34 review 2: accepted

Run: RUN-260822-a70c5e (not goal-bound). Scope reviewed: `README.md` and
`CHANGELOG.md` in the working tree of `/Users/iv/Developer/Wildberries/cocoaskills`.
Evidence: `.temp/TASK-260822-3tvw34/verification-02.log`.

## Verdict

Accepted. Both blocking findings from review 1 (RUN-260822-590e14) are fixed
exactly as prescribed, the CHANGELOG half is unchanged from the state review 1
already verified against git, and nothing new regressed.

## W1 closed: compiled commands are qualified as one of two forms

`README.md:146` now opens with:

    Скомпилированные команды скилла могут собираться из отдельно
    запиненного git-репозитория.

That is the prescribed sentence verbatim. It matches `docs/skill-authoring.md:367`
("Скиллы схемы 7 могут также выбирать зафиксированный внешний источник Git") and
no longer contradicts the base schema-6 form, which declares `build_roots` inside
the skill package and a `source_dir` within one of those roots
(`docs/skill-authoring.md:369-380`). `grep -n "скомпилир\|компилир" README.md`
still returns exactly this one line, so the definition a reader takes away is now
the correct one.

## W2 closed: показывает, not считывает

Same line reads "при первой установке `csk` показывает обнаруженные варианты".
That is what the code does: `src/csk/installer.py:961` prints
`Detected candidates:`, `:962-964` prints one numbered line per option with
`   <- default` on the first, `:965` reads one selection. The default option is
built at `:941-949` as `ssh-agent{loaded} + pin {first}`, so the README's
дефолт "агент + пин ключа" is accurate.

## Re-verified, unchanged from review 1

- CHANGELOG rehoming: every former `Unreleased` entry sits under
  `## [0.13.0] - 2026-08-08`. Spot-checked by first-introducing commit and
  `git tag --contains`: `--only` selector -> `7680d8f` -> earliest tag `v0.13.0`;
  `build_roots` -> `dd76b57` -> earliest tags `v0.13.0`, `v0.13.0-rc.1`,
  `v0.13.0-rc.2`. Forward check: `v0.13.0..v0.14.0` holds eight commits and the
  only non-CI/non-docs ones are `e66c0c8`, `b2d3d14`, `d8d3529`, so no
  ex-`Unreleased` entry could belong to 0.14.0.
- 0.14.0 claims: launcher canonicalization, `lib64 -> lib` alias, closure manifest
  naming and the `toolchain_executable_mismatch` remedy trace to `e66c0c8`'s commit
  body; vendor advisory downgrade, `config.add_project` via `dataclasses.replace`,
  `build_ssh` scopes, `csk config build-ssh add/list/remove`, precheck, `--dry-run`
  source table and the bootstrap question trace to `b2d3d14`'s body. Both are in
  `v0.13.0..v0.14.0`.
- 0.14.1 claim traces to `2bb2772`, in `v0.14.0..v0.14.1`.
- Dates match annotated tag dates: `v0.13.0` 2026-08-08, `v0.14.0` 2026-08-21,
  `v0.14.1` 2026-08-22.
- Comparison links form an unbroken chain
  `v0.12.5...v0.13.0...v0.14.0...v0.14.1...main`, no duplicates, no orphans.
- `Unreleased` is empty and that is correct: `git log v0.14.1..HEAD` is empty.
- Troubleshooting link at `README.md:291` in `## Дальше`, formatted like its
  siblings; `docs/troubleshooting.md` exists (4571 bytes, untracked, owned by
  sibling task TASK-260822-1jns5p). Every `](*.md)` target in `README.md` resolves
  on disk.
- Prose style: zero em-dashes, en-dashes or guillemets in any line this task added
  to either file.

## Tests

`src/` and `tests/` are untouched since review 1's full run (1465 passed, 244
skipped in 346s, `.temp/TASK-260822-3tvw34/verification-01.log`). The review-2
delta is two prose edits inside a single README line. No test reads the repository
README or CHANGELOG: every `README.md`/`CHANGELOG` occurrence under `tests/` writes
into `tmp_path` (`tests/test_whitelist.py:11,30,52`,
`tests/test_skillcheck.py:110,327,342`). Nothing to rerun.

## Notes carried forward, no action in this task

- N1 (from review 1): "за один Enter" is two prompts on the happy path. The phrase
  comes from the owner TZ verbatim and the task description names it, and the body
  is honest about both steps, so it stays.
- N2 (from review 1, optional): "В CI то же самое задаётся заранее" keeps the
  passive. The producer left it. Not blocking.
- The rehomed 0.13.0 entries carry em-dashes from their original authoring. Moving
  a heading is not rewriting the entries, so this task's delta does not introduce
  them; a docs-wide prose pass would own that.
- Terminology split persists: `docs/reference.md` and `docs/troubleshooting.md` say
  "компилируемые команды", `docs/skill-authoring.md` and `README.md` say
  "скомпилированные". Predates this task.

## Commit scope for the mover

`README.md` (lines 144-154 and 291) and `CHANGELOG.md` (lines 8-30 and the link
block at the bottom). Every other modified path in the tree belongs to sibling
tasks of STORY-260822-318s44. This reviewer run supplied no `commit_ack`; the
commit-owning mover commits this scope and then makes the final `done` transition
with `commit_ack=scope_committed`.
