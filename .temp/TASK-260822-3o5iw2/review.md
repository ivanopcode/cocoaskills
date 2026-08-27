# TASK-260822-3o5iw2 review verdict: changes requested (to-dev)

Reviewer run: RUN-260822-ebd1e8. Read-only review, no repository files modified
by this run. Scratch fixtures under `.temp/TASK-260822-3o5iw2/` (gitignored).

## Scope actually landed

`git diff --stat docs/external-build-repositories.md docs/skill-authoring.md`

    docs/external-build-repositories.md |  2 ++
    docs/skill-authoring.md             | 20 +++++++++++++++++++-

All four required deltas are present in the working tree, so the earlier
lost-edit failure mode did not repeat:

- `docs/external-build-repositories.md:75` vendor-advisory paragraph, placed at
  the end of `## Admission and audit order`, directly after the paragraph that
  names the independent external audit.
- `docs/skill-authoring.md:33` new subsection `### Чего пакет скилла не делает`
  with the three antipatterns.
- `docs/skill-authoring.md:505` copy-paste resolution template with the
  `<tool>` placeholder, inside section 6, subsection
  `### Разрешение команд для агента`.
- `docs/skill-authoring.md:820` release checklist item 1 extended.

spec#22 wording is untouched: the whole-file diff of
`docs/external-build-repositories.md` is a single two-line insertion, so the
three-forms wording and the protocol revision hex in the header are unchanged.
TZ item 5.2 stays deferred as instructed.

## Verification performed against live 0.14.1

`csk --version` reports `csk 0.14.1` (`/opt/homebrew/bin/csk`).

Lint identifier confirmed in the codebase at
`src/csk/skillcheck.py:178`: `skill.command_resolution_contract_missing`.
The rule (`_command_resolution_warnings`, `src/csk/skillcheck.py:143`) requires
the prompt-visible Markdown to contain `.agents/bin`, `global/bin`, both
`command -v` and `Get-Command`, and `.cmd` when a managed command is a build
command or declares `win_path`. The doc template supplies all five tokens.

The template was extracted verbatim from `docs/skill-authoring.md`, `<tool>`
substituted with a real command name, and checked live against a fixture skill
(`script` command with `unix_path` and `win_path`):

    $ csk skill check .
    /Users/iv/.../fixture-skill: ok
    EXIT=0

Negative control, same fixture with the template removed from `SKILL.md`:

    $ csk skill check neg
    warning: skill.command_resolution_contract_missing SKILL.md: Prompt-visible
    instructions export managed runtime commands but do not document a
    shell-neutral resolver (project .agents/bin lookup, CocoaSkills global/bin
    fallback, validated POSIX and PowerShell bare-command fallbacks, Windows
    .cmd shim suffix). ...
    EXIT=0

Antipattern claims verified against the code:

- `Тип команды binary не существует`: `src/csk/skillspec.py:311` raises
  `Command {name!r} has unsupported type {command_type!r}` for any type outside
  `script`, `system`, and `build`.
- `unsafe transaction tree entry`: `src/csk/transactions.py:1737`.
- `agents/runtime.json` is the last manifest fallback:
  `src/csk/skillspec.py:127`, after `agent-skill.json` and `csk-skill.json`.

Vendor-advisory blocking semantics verified against
`_vendored_inert_text` (`src/csk/installer.py:769`) and `_external_static_audit`
(`src/csk/installer.py:796`): the exception applies only to `HIGH` findings on a
regular non-executable file whose path has `vendor` as a non-final segment;
`CRITICAL` always blocks. The doc sentence about what still blocks is accurate.

Tests: `.venv/bin/pytest tests/test_skillcheck.py -q` gives `23 passed`. No test
or CI job reads either doc (`grep -rln 'skill-authoring|external-build-repositories'
tests/ .github/` is empty), and the repository has no links job in
`.github/workflows/`.

Prose style: no em-dashes, no en-dashes, no guillemets in any added line.

## Findings blocking acceptance

### 1. `docs/external-build-repositories.md:75` states an advisory output that does not exist

The added sentence reads:

    The finding remains in the advisory output.

The external repository static audit produces no output. `_external_static_audit`
(`src/csk/installer.py:796`) calls `audit_detectors.detect_snapshot`, keeps only
`HIGH`/`CRITICAL` findings that are not vendored inert text, raises `InstallError`
when that list is non-empty, and discards every other finding without printing,
returning, or recording it. The pipeline hook type confirms this:
`build_repository_pipeline.py:162` declares `audit: Callable[[AuditSubject], None]`.
The `advisory` audit mode in `src/csk/config.py:97` and the CLI `--audit advisory`
flag belong to the skill audit gate over `plan.snapshot`
(`src/csk/audit/pipeline.py:96`), which never sees the external build repository
snapshot.

A reader following this sentence will look for a suppressed finding in install
output and find nothing. This is the discrepancy the DoD item "no discrepancies
between code and description" is meant to catch. The TZ draft carried the same
claim, so the fix needs a wording decision rather than a mechanical edit.

Recommended fix: drop the sentence, or state the fact accurately, for example
"CocoaSkills does not report such a finding: it neither blocks the install nor
appears in install output." If the owner intends a future advisory surface, that
belongs in a separate task, not in a paragraph describing 0.14.1.

### 2. `docs/skill-authoring.md:820` parenthetical contradicts live 0.14.1 output

The added wording is `(ноль warnings, не только ok)`. Live behaviour: when a
warning fires, `csk skill check` prints only the warning line and does not print
`ok`; when nothing fires, it prints `<path>: ok` and nothing else. An `ok` line
never coexists with a warning, so "не только ok" describes a state the tool
cannot produce, and it reads as an instruction to look past an `ok` line that is
already proof of zero warnings.

The fact actually worth documenting is the exit code. Both runs above exited `0`,
and `csk skill check --help` states `Exit codes: 0 no errors, 1 one or more
strict errors`. A green exit code therefore does not mean zero warnings, which is
exactly the trap the TZ wanted closed.

Recommended fix: replace the parenthetical with the exit-code fact, for example
"`csk skill check` завершается кодом 0 и при предупреждениях, поэтому проверяйте
вывод, а не код возврата."

Secondary point on the same line: `не только ok` is the antithesis construction
that `docs/prose-style.md` blacklists ("не просто X, а Y"). The recommended fix
removes it.

## Findings to fix in the same pass (non-blocking on their own)

### 3. `docs/external-build-repositories.md:75` breaks the file's line wrapping

The added paragraph is one 386-character line. Every prose line in that file
wraps at 80 columns or less; `awk 'length>100' docs/external-build-repositories.md
| wc -l` returns `1`, and that one line is this addition. Rewrap at 80 columns.
`docs/skill-authoring.md` has 152 lines over 100 characters, so the long lines
added there match that file's convention and need no change.

### 4. `docs/skill-authoring.md:35-37` clashes with the document's imperative register

The document addresses the author in the Вы-form throughout: `Используйте`,
`Проверьте`, `Добавьте`, `Не рассчитывайте`, `сообщите`, `остановите`,
`не угадывайте`. The three new bullets use informal singular imperatives:
`Не коммить`, `Не пиши`, `Не держи`. Line 36 mixes both inside one bullet:
`Не пиши в runtime-дерево ... Кеши и окружения держите в директориях
пользователя`. `Не коммить` is also an infinitive standing in for an imperative.

Convert the three bullets to the Вы-form (`Не коммитьте`, `Не пишите`,
`Не держите`), which also resolves the intra-bullet clash. The template block at
505-515 keeps its own agent-addressed voice; it is text the author pastes into
their own `SKILL.md`, so it stays as drafted.

## Routing

Status set to `to-dev`. Findings 1 and 2 are single-sentence rewrites in files
already open in this working tree; findings 3 and 4 are mechanical and should
land in the same pass. Nothing here is a stop-the-line boundary and no external
input is required. After rework, re-verify with:

    grep -n "vendor/" docs/external-build-repositories.md
    awk 'length>100' docs/external-build-repositories.md | wc -l
    grep -n "csk skill check ." docs/skill-authoring.md
    sed -n '33,38p' docs/skill-authoring.md
