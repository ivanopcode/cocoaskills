# Review verdict 2: TASK-260822-1jns5p (troubleshooting-and-releaser-note)

Reviewer run: RUN-260822-8f8ecf (not goal-bound).
Verdict: **changes requested** -> `to-dev`.
Reviewed tree: /Users/iv/Developer/Wildberries/cocoaskills @ 818049c (uncommitted docs work).
Scope reviewed: `docs/troubleshooting.md` (new, 67 lines), `CONTRIBUTING.ru.md` (2 added paragraphs).
Previous cycle: RUN-260822-5b46b2 (`TASK-260822-1jns5p_review-verdict.md`, findings F1-F5).

## Previous findings: all five closed

### F1 (blocking, closed) heading placeholders now survive rendering

`docs/troubleshooting.md:9` is now `## unsafe transaction tree entry: `.../runtime/<skill>/<commit>/.venv/...``.
Re-rendered every `##` heading of the file through the same CommonMark parser used to prove
the defect:

    $ .venv/bin/python -c "from markdown_it import MarkdownIt; ..."
    <h2>invalid install marker ... installed_at is not a UTC second timestamp</h2>
    <h2>unsafe transaction tree entry: <code>.../runtime/&lt;skill&gt;/&lt;commit&gt;/.venv/...</code></h2>
    <h2>toolchain_executable_mismatch: ... selected Go executable is not below a GOROOT bin directory</h2>
    <h2>build_repository_ssh_credential_missing</h2>
    <h2>Cannot resolve tag '...' ... Needed a single revision</h2>
    <h2>commands are installed in .../.agents/bin, which is not on PATH</h2>

`<skill>` and `<commit>` are now escaped inside `<code>`; the anchor keeps both segments.
No other heading carries raw angle brackets.

### F2 (blocking, closed) releaser note is prescriptive and carries the 0.14.0 incident

`CONTRIBUTING.ru.md:72` now reads "Выражение `if` этой джобы обязано начинаться с `always()`"
and cites "это уже отменило публикацию на релизе 0.14.0". Facts re-verified:
`.github/workflows/release.yml:178` declares `bump-homebrew-tap`, `needs: [build, publish-pypi]`
at :180, and the condition at :186-189 is `if: >- ${{ always() && ... }}`. The second paragraph
matches `.github/workflows/distribution-smoke.yml:347-348` (`brew trust --tap ivanopcode/csk`
immediately before `brew install cocoaskills`).

### F3-F5 (non-blocking, closed)

- `:59` tautology replaced with "Установка завершилась успешно. Агентские скиллы вызывают шимы
  напрямую по абсолютным путям, поэтому добавление `.agents/bin` в `PATH` опционально."
- Lead-ins now vary across the six sections (Запустите / Очистите / Укажите / Привяжите /
  Обновите / настройте).
- `:5` now uses the elision convention: `invalid install marker ... installed_at is not a UTC
  second timestamp`.

## Independently re-verified this cycle

All six error strings still resolve to real code:

| # | Doc heading | Source | Verified |
|---|---|---|---|
| 1 | `installed_at is not a UTC second timestamp` | `src/csk/install_marker.py:453`, surfaced by `src/csk/status.py:529` as `invalid install marker {path}: {exc}` | yes |
| 2 | `unsafe transaction tree entry` | `src/csk/transactions.py:1737` | yes |
| 3 | `selected Go executable is not below a GOROOT bin directory` | `src/csk/builds/toolchain.py:1038-1039` | yes |
| 4 | `build_repository_ssh_credential_missing` | `src/csk/git_admission.py:44`, message assembled at `src/csk/installer.py:1090` | yes |
| 5 | `Cannot resolve tag '...'` | `src/csk/closure.py:178`; the tail `Needed a single revision` is git's own | yes |
| 6 | `commands are installed in ..., which is not on PATH` | `src/csk/installer.py:596` | yes |

Remedy commands against live `csk 0.14.1` (`/opt/homebrew/bin/csk`):

- `csk install` help: "Apply Skillfile.json using local refs. Missing git URL sources are cloned."
  This is the exact basis for the doc's claim at `:47` that install works from local refs.
- `csk upgrade` help: "Fetch the selected project dependency closure, then install."
- `csk global --help` lists `install`, `update`, `upgrade`.
- `csk config build-ssh add --help` matches the documented flags verbatim, including
  "bare flag or 'auto' adopts SSH_AUTH_SOCK".
- `csk shell-init --help --install`: "atomically cache the hook under the CocoaSkills home and
  print the profile source command", which matches the doc's closing sentence at `:67`.
- Runtime layout `~/.cocoaskills/runtime/<name>/<commit>` confirmed at
  `src/csk/global_install.py:646,676`; the glob `runtime/*/*/.venv` is correct.
- Fail-closed text at `src/csk/installer.py:1104-1107` prints exactly
  `csk config build-ssh add <scope> --agent auto --identity ~/.ssh/<key>.pub` when no candidate
  is discovered, which is the command the doc reproduces.
- Invalid marker handling: `src/csk/installer.py:3370` treats an unreadable marker as absent, so
  the claim at `:7` that a plain reinstall rewrites it holds.

Other checks: no em-dashes, en-dashes, or guillemets in either file; `README.md` untouched
(`git status --short README.md` and `git diff HEAD -- README.md` both empty); `git diff
--name-only HEAD` lists only `*.md` plus `.gitignore` from a sibling task, so no `src/` or
`tests/` impact and no test references `docs/troubleshooting.md`; the repo has no link-check
workflow (`.github/workflows/` is `ci.yml`, `distribution-smoke.yml`, `release.yml`), so the
new unlinked page breaks no CI gate; logbook entries present at `LOGBOOK.md:2139` and `:2189`.

Note on the previous run: the agy implementer exited 1, but the log shows `error: context
canceled` after the handoff, and the edits plus the updated `_results.md` did land. The non-zero
exit is a runtime artifact, not lost work.

## Findings (blocking)

### R1. The `build_repository_ssh_credential_missing` remedy attributes the fix to the wrong
actor and omits the step that actually resolves the failure (confirmed)

`docs/troubleshooting.md:43`, after the `csk config build-ssh add` block:

    Инсталлятор сохранит запись в `~/.cocoaskills/config.json` и повторит сборку с указанным ключом.

Both halves are false for the path the section documents.

`_cmd_config_build_ssh` in `src/csk/cli.py:1017` writes the config itself
(`config.save_config(replace(cfg, build_ssh=others + (rule,)))`), prints `Configured build-ssh
scope {scope}` and returns. No installer runs, and nothing repeats the build. Reproduced against
a throwaway config so the operator config stayed untouched:

    $ TMP=$(mktemp -d); printf '{"schema_version": 1, "skills_root": "%s/skills", "projects": {}}' "$TMP" > "$TMP/config.json"
    $ CSK_CONFIG="$TMP/config.json" csk config build-ssh add gitlab.example.com/portals/infra \
        --agent auto --identity ~/.ssh/example.pub
    Configured build-ssh scope gitlab.example.com/portals/infra
    exit=0
    $ python3 -m json.tool "$TMP/config.json"
    { ... "build_ssh": {"gitlab.example.com/portals/infra": {"agent": "auto", "identity": "/Users/iv/.ssh/example.pub"}} ... }

The installer only persists a scope on the interactive path
(`src/csk/installer.py:1082-1087`, `build_ssh scope ... saved to {config.path}`), which is the
branch the reader is explicitly not on: this section exists for the non-TTY failure. Five of the
six sections end with a command the reader runs and a true statement of what they observe; this
one ends by promising an automatic rebuild that never happens, and never tells the reader to
re-run the install. A CI operator following the page verbatim configures the scope and waits.

Remedy: name the acting command and state the remaining step, for example

    Команда записывает скоуп в `~/.cocoaskills/config.json` и печатает
    `Configured build-ssh scope <scope>`. Повторите установку: инсталлятор возьмёт креды
    из скоупа.

## Findings (non-blocking, fold into the same rework)

### R2. Heading 3 marks an elision that does not exist and drops the message prefix

`docs/troubleshooting.md:21`:

    ## toolchain_executable_mismatch: ... selected Go executable is not below a GOROOT bin directory

`ToolchainError.__str__` is `f"go-v1 {code}: {detail}"` (`src/csk/builds/toolchain.py:105`), so the
real line is contiguous:

    go-v1 toolchain_executable_mismatch: selected Go executable is not below a GOROOT bin directory

Nothing sits between the code and the detail, so the `...` invents variable text, and the dropped
`go-v1 ` prefix is part of every emitted message. An operator pasting their exact error into the
page search misses this heading. The other five headings use `...` correctly, for genuinely
elided paths and values. Remedy: use the verbatim string, prefix included, with no `...`.

### R3. Two interpreting sentences still name a non-actor

- `:35` "Интерактивный терминал `csk` выводит меню обнаруженных кандидатов" makes the terminal the
  subject. csk prompts when stdin is a TTY; prose-style asks for the software as the subject.
  "в неинтерактивном окружении вызов падает" is also vaguer than the section warrants: the install
  fails with this exact error.
- `:55` "Процесс выкачает свежие теги из удалённого репозитория и завершит установку" names
  "Процесс". `csk upgrade` is the actor, and its own help states the two steps
  ("Fetch the selected project dependency closure, then install").

## Rework routing

Status set to `to-dev`. Fix R1 (blocking), fold in R2 and R3, then hand back for a third reviewer
cycle. Both files stay docs-only; no code or test changes are expected, and README stays untouched
(its troubleshooting link belongs to the readme-and-changelog task).
