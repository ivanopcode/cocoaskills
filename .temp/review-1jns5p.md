# Review verdict: TASK-260822-1jns5p (troubleshooting-and-releaser-note)

Reviewer run: RUN-260822-5b46b2 (not goal-bound).
Verdict: **changes requested** -> `to-dev`.
Reviewed tree: /Users/iv/Developer/Wildberries/cocoaskills @ 818049c (uncommitted docs work).
Scope reviewed: `docs/troubleshooting.md` (new), `CONTRIBUTING.ru.md` (2 added paragraphs).

## What passes

### Six symptoms present, error strings independently re-verified against `src/csk`

| # | Doc heading | Source of truth | Verified |
|---|---|---|---|
| 1 | `installed_at is not a UTC second timestamp` | `src/csk/install_marker.py:453`; wrapper `install marker {code}: {detail}` at `install_marker.py:129`; surfaced as `invalid install marker {path}: {exc}` at `src/csk/status.py:529` | yes |
| 2 | `unsafe transaction tree entry` | `src/csk/transactions.py:1737` (raised when a tree entry is neither dir nor regular file, i.e. the symlinks a `.venv` carries) | yes |
| 3 | `toolchain_executable_mismatch` + `selected Go executable is not below a GOROOT bin directory` | `src/csk/builds/toolchain.py:1038-1039`; the operator hint rides as `error.add_note(...)` with `PATH="$(go env GOROOT)/bin:$PATH"` | yes |
| 4 | `build_repository_ssh_credential_missing` | `src/csk/git_admission.py:44` | yes |
| 5 | `Cannot resolve tag '...' ... Needed a single revision` | prefix at `src/csk/closure.py:178` (`Cannot resolve {kind} {value!r} for {name} (via {chain}): {exc}`); tail is git's own text, reproduced locally: `git rev-parse --verify refs/tags/definitely-not-a-tag` -> `fatal: Needed a single revision` | yes |
| 6 | `commands are installed in ..., which is not on PATH` | `src/csk/installer.py:596`; the same message already recommends `csk shell-init --install` | yes |

Note: the producer's own results resource cites `src/csk/git_ops.py` for symptom 5. The
`Cannot resolve tag` prefix actually lives in `src/csk/closure.py:178`. The doc text is
correct; only the citation in the results artifact is wrong.

### Remedy commands re-verified against live csk 0.14.1 (`/opt/homebrew/bin/csk`, `csk --version` -> `csk 0.14.1`)

- `csk install`, `csk global install` exist (`csk global --help` lists `install`).
- `csk upgrade`, `csk global upgrade` exist; `csk --help` describes upgrade as
  "Fetch the selected project dependency closure, then install", which matches the doc's
  "скачивает новые теги из удалённого репозитория и запускает установку".
- `csk config build-ssh add <scope> --agent [SOCKET] --identity PATH` matches
  `csk config build-ssh add --help` exactly, including `'auto' adopts SSH_AUTH_SOCK`.
- `csk shell-init --install` matches `csk shell-init --help`
  ("atomically cache the hook under the CocoaSkills home and print the profile source command").
- `~/.cocoaskills/runtime/*/*/.venv` matches the real layout `runtime/<name>/<commit>`
  (`src/csk/global_install.py:676,1136`).

### Releaser note factually correct

- `bump-homebrew-tap` is in `.github/workflows/release.yml:178`, `needs: [build, publish-pypi]`,
  and its condition is `if: >- ${{ always() && ... }}` (lines 186-189).
- `brew trust --tap ivanopcode/csk` runs before `brew install cocoaskills` in
  `.github/workflows/distribution-smoke.yml:347`.

### Other AC / DoD checks

- No README edits: `git status --short README.md` is clean, `git diff HEAD -- README.md` empty.
- No em-dashes, en-dashes or guillemets in either file (`grep -n "[--<<>>]"` equivalent run on both).
- Every code block is introduced by a colon sentence and followed by an interpreting sentence.
- No closing summary paragraph, no antithesis constructions, no marketing register.
- Logbook entry present: `LOGBOOK.md:2139`.
- Tests: the working tree changes nothing under `src/` or `tests/`
  (`git diff --name-only HEAD` returns only `*.md` plus `.gitignore` from a sibling task),
  and nothing in `tests/` references `docs/troubleshooting.md`. No test impact.

## Findings (blocking)

### F1. Heading 9 loses two path segments when rendered (confirmed)

`docs/troubleshooting.md:9`

    ## unsafe transaction tree entry: .../runtime/<skill>/<commit>/.venv/...

`<skill>` and `<commit>` are unbackticked in a heading. CommonMark treats both as raw
inline HTML and passes them through verbatim, verified locally:

    $ .venv/bin/python -c "from markdown_it import MarkdownIt; \
        print(MarkdownIt('commonmark', {'html': True}).render( \
        '## unsafe transaction tree entry: .../runtime/<skill>/<commit>/.venv/...'))"
    <h2>unsafe transaction tree entry: .../runtime/<skill>/<commit>/.venv/...</h2>

GitHub's sanitizer then drops both unknown tags. The heading renders as
`unsafe transaction tree entry: .../runtime///.venv/...` and its anchor collapses to
`#unsafe-transaction-tree-entry-runtimevenv`. The sibling readme-and-changelog task is
expected to link this page, so the broken anchor propagates.

Repo convention is already the fix: every other doc backticks these placeholders
(`docs/mvp-design.md:588,742`, `docs/skill-authoring.md:23`, `docs/audit-design.md:113`).
Remedy: wrap the path in the heading, e.g.

    ## unsafe transaction tree entry: `.../runtime/<skill>/<commit>/.venv/...`

### F2. Releaser note drops the required 0.14.0 evidence and reads as description, not rule

`CONTRIBUTING.ru.md:72`

Current text states the condition as a fact: "Условие `if` этой джобы начинается с
`always()`." The task description and TZ item 8 both require the rule plus the incident:
`if` **обязан** начинаться с `always()`, and the transitive skip already silently killed
the bump on 0.14.0. A releaser refactoring `release.yml` reads the current paragraph as a
description of the status quo and has no signal that dropping `always()` is the known
regression. Prose style also asks for the prescription with its rationale attached.

Remedy: restore the prescriptive form and the 0.14.0 incident.

## Findings (non-blocking, fix while reworking)

### F3. Empty sentence in symptom 6

`docs/troubleshooting.md:59`: "Сообщение сообщает о статусе окружения." is a tautology and
carries no information. The section also loses the load-bearing fact from the TZ draft:
this message is informational, the install succeeded. State that flatly, for example
"Установка завершилась успешно. Сообщение описывает состояние `PATH`."

### F4. Mechanical lead-ins and hollow interpretation sentences

Five of six sections use the same template ("Для X запустите команду:" / "выполните
команду:" / "вызовите команду:"), and several interpreting sentences restate the command
without adding a fact: "Команда удаляет каталоги venv." after `rm -rf`, "Команда передаёт
инсталлятору прямой путь к исполняемому файлу Go." Prose style asks the trailing sentence
to say what the reader should observe.

### F5. Heading 5 breaks the doc's own elision convention

`docs/troubleshooting.md:5` writes `invalid install marker: installed_at is not a UTC
second timestamp` as if contiguous. The real message is
`invalid install marker <path>: install marker install_marker_invalid: installed_at is not
a UTC second timestamp: '<value>'`. The other five headings mark elided variable parts with
`...`; this one should too.

## Rework routing

Status set to `to-dev`. Fix F1 and F2 (blocking), fold in F3-F5, then hand back for a
second reviewer cycle. No code changes are expected; both files stay docs-only.
