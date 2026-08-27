# TASK-260822-3o5iw2 review round 2: accepted

Reviewer run: RUN-260822-aefcf9. Read-only review; no repository source file was
modified by this run. Scratch fixtures under `.temp/TASK-260822-3o5iw2/verify/`
(gitignored). One project record was written: a reviewer entry at the top of
`LOGBOOK.md`.

## Scope landed

    git diff --stat docs/external-build-repositories.md docs/skill-authoring.md
    docs/external-build-repositories.md |  8 ++++++++
    docs/skill-authoring.md             | 20 +++++++++++++++++++-

`docs/external-build-repositories.md` carries exactly one hunk (`@@ -74,0 +75,8 @@`),
so the three-forms wording and the protocol revision hex in the file header are
untouched. TZ item 5.2 stays deferred as instructed.

Deltas in place:

- `docs/external-build-repositories.md:75` vendor-advisory paragraph, at the end of
  `## Admission and audit order`, directly after the paragraph naming the independent
  external audit.
- `docs/skill-authoring.md:33` subsection `### Чего пакет скилла не делает` with the
  three antipatterns.
- `docs/skill-authoring.md:504` copy-paste resolution template with the `<tool>`
  placeholder, in section 6 under `### Разрешение команд для агента`.
- `docs/skill-authoring.md:820` release checklist item 1 extended.

## Round 1 findings: all four resolved

1. **Advisory-output claim (blocking).** Fixed. The paragraph now reads "CocoaSkills
   does not report such a finding: it neither blocks the install nor appears in install
   output." Verified against code: `_external_static_audit` (`src/csk/installer.py:796`)
   calls `audit_detectors.detect_snapshot`, keeps `HIGH`/`CRITICAL` findings that are not
   vendored inert text, raises `InstallError` on that list, and discards everything else
   without printing, returning, or recording it. The pipeline hook is typed
   `audit: Callable[[AuditSubject], None]` (`src/csk/build_repository_pipeline.py:162`),
   so no advisory surface exists.
2. **`не только ok` parenthetical (blocking).** Fixed. Item 1 now states the exit-code
   fact: `csk skill check` завершается кодом 0 и при предупреждениях, поэтому проверяйте
   вывод, а не код возврата. Verified live below. The antithesis construction is gone.
3. **Line wrapping.** Fixed. `awk 'length>80' docs/external-build-repositories.md` returns
   one line, `171`, which is pre-existing (outside this task's hunk). Every added line is
   at or below 80 columns.
4. **Imperative register.** Fixed. The three bullets read `Не коммитьте`, `Не пишите`,
   `Не держите`, and the intra-bullet clash is gone (`Кеши и окружения держите`). The
   template block keeps its own agent-addressed voice, which is correct: it is text the
   author pastes into their own `SKILL.md`.

## Verification against live 0.14.1

`csk --version` reports `csk 0.14.1` (`/opt/homebrew/bin/csk`).

Lint identifier confirmed at `src/csk/skillcheck.py:178`:
`skill.command_resolution_contract_missing`. The rule
(`_command_resolution_warnings`, `src/csk/skillcheck.py:143`) requires the
prompt-visible Markdown to contain `.agents/bin`, `global/bin`, both `command -v`
and `Get-Command`, and `.cmd` when a managed command is a build command or declares
`win_path`. The doc template supplies all five tokens.

Fixtures were rebuilt from the current doc text (template extracted programmatically
from `docs/skill-authoring.md`, `<tool>` replaced with `mytool`), not reused from
round 1:

    $ csk skill check .temp/TASK-260822-3o5iw2/verify/pos
    /Users/iv/.../verify/pos: ok
    EXIT=0

    $ csk skill check .temp/TASK-260822-3o5iw2/verify/neg
    warning: skill.command_resolution_contract_missing SKILL.md: Prompt-visible
    instructions export managed runtime commands but do not document a shell-neutral
    resolver (project .agents/bin lookup, CocoaSkills global/bin fallback, validated
    POSIX and PowerShell bare-command fallbacks, Windows .cmd shim suffix). ...
    EXIT=0

The negative control prints no `ok` line, so the reworked checklist wording matches
observed behaviour, and `csk skill check --help` confirms "Exit codes: 0 no errors,
1 one or more strict errors".

Claims in the added text checked against source:

- fixed build session runs only `go list` and `go build`: `LIST_ARGUMENTS` and
  `BUILD_ARGUMENT_PREFIX`, `src/csk/builds/go_v1.py:149` and `:159`, both fixed argv
  validated at `:4437`.
- vendor exception scope: `_vendored_inert_text` (`src/csk/installer.py:769`) applies
  only to `HIGH` findings on a regular non-executable file whose path carries `vendor`
  as a non-final segment; `CRITICAL` always blocks. The sentence about what still blocks
  is accurate.
- `binary` command type does not exist: `src/csk/skillspec.py:314` raises
  `Command {name!r} has unsupported type {command_type!r}` for anything outside `script`,
  `system`, `build`.
- `unsafe transaction tree entry`: `src/csk/transactions.py:1737`.
- `agents/runtime.json` is the last manifest fallback: `src/csk/skillspec.py:26` and the
  resolution order at `:123-130`, after `agent-skill.json` and `csk-skill.json`.

Tests: `.venv/bin/pytest tests/test_skillcheck.py tests/test_build_repository_pipeline.py -q`
gives `46 passed`. No test or CI job reads either doc, and `.github/workflows/` holds only
`ci.yml`, `distribution-smoke.yml`, `release.yml` (no links job).

Prose style: no em-dashes, en-dashes, or guillemets on any added line; no trailing
whitespace; the `###` subsection heading matches the file's convention of unnumbered
third-level headings.

## Non-blocking notes for the commit-owning mover

- The implementer's `LOGBOOK.md` entry (the one below the reviewer entry) summarizes the
  vendor paragraph as "non-executable text under `vendor/` is advisory" and quotes the
  checklist as `ноль warnings`. Both describe the pre-rework wording. The substance is
  right; the wording is stale and worth a one-line touch-up when this scope is committed.
- `_vendored_inert_text`'s docstring (`src/csk/installer.py:769`) still says such text
  "stays an advisory finding", which is where the original wrong doc claim came from.
  That is a code comment outside this task's scope; a separate task should correct it.

## Acceptance evidence for the `done` transition

The reviewer does not commit and does not supply `commit_ack`. Scope verified, tests
green, docs consistent with 0.14.1 behaviour. The commit-owning mover can commit
`docs/external-build-repositories.md`, `docs/skill-authoring.md`, and the `LOGBOOK.md`
entries for this task, then make the final transition with `commit_ack=scope_committed`
if the board enforces it.
