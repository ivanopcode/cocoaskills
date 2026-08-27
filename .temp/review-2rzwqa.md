# TASK-260824-2rzwqa review verdict: changes requested (to-dev)

Reviewed diff: `docs/troubleshooting.md` (+46), `docs/reference.md` (+1/-1) in
`/Users/iv/Developer/Wildberries/cocoaskills` on top of 5381317.

## What is correct and verified

Every quoted error string in the new `build_repository_credential_policy_invalid`
entry matches the source verbatim:

| Doc quote | Source |
| --- | --- |
| `names environment variable '<token_env>', which is unset` | `src/csk/installer.py:1206` |
| `selects a stored token, but none is saved` | `src/csk/installer.py:1223` |
| `selects your Git credentials, but no helper holds one for '<host>'` | `src/csk/installer.py:1237` |
| `build_repository_credential_policy_invalid` | `src/csk/git_admission.py:45` |
| `build_repository_source_unavailable` | `src/csk/git_admission.py:32` |
| `http.followRedirects=false` | `src/csk/git_admission.py:1020` |

`csk config build-https login <scope>` is a real command with `scope` as the only
required positional (`.venv/bin/csk config build-https login --help`). Both entries
are findable by the codes required by the AC; the 301 heading carries both the git
error text and `build_repository_source_unavailable`.

The reference.md ordering claim is verified: `src/csk/installer.py:1470` raises the
platform gate at the top of `_publish_external_builds`, before
`_resolve_build_ssh_credentials`, `_resolve_build_https_credentials` and before any
worker starts. The same gate covers both `csk install` (`installer.py:544`) and
`csk global install` (`global_install.py:518`).

No em-dashes, en-dashes or guillemets in either file. Docs-only change; no test
references `docs/troubleshooting.md` or `docs/reference.md`, so the suite is
unaffected.

## Findings that block acceptance

### 1. reference.md: wrong actor and rotated terminology (docs/reference.md:9)

Current: "На Linux скилл с такой командой отклоняет установку до проверки учётных
данных и запуска воркера."

The skill does not reject anything; `_publish_external_builds` in the installer
raises. The prose style guide requires the actor be named correctly ("установщик
копирует файлы", not an inanimate subject doing the installer's work).

The same sentence writes "Скомпилированные команды", while the document's own
opening paragraph (`docs/reference.md:3`) and `docs/troubleshooting.md:3` both say
"компилируемые команды". The style guide forbids rotating synonyms for a defined
term.

Suggested: "Компилируемые команды `go-repository-v1` поддерживаются только на macOS
и Windows. На Linux установщик отклоняет скилл с такой командой до проверки учётных
данных и до запуска воркера."

### 2. troubleshooting.md: token literal in the `token_env` example (docs/troubleshooting.md:52)

Current: ```TOKEN_NAME="token_value" csk install```

The TZ forbids showing secrets in examples. This block puts a token value inline on
the command line, which is also the exact habit the surrounding feature is designed
to avoid (shell history). The placeholder names also break the conventions already
used in these docs: `docs/reference.md:233` and
`docs/external-build-repositories.md:100` use `CI_TOKEN` for the `token_env`
variable, and the rest of `troubleshooting.md` uses angle-bracket placeholders
(`<host>`, `<scope>`, `<key>`).

Suggested: name the variable the way the rest of the docs do and read the value from
somewhere other than the literal, for example
```export CI_TOKEN="$(pass show ci/gitlab)"``` followed by ```csk install```, or at
minimum `CI_TOKEN` with an explicit "значение подставьте из своего хранилища".

### 3. Windows note attributes the re-read to the wrong command (docs/troubleshooting.md:67)

Current: "Установщик csk перечитывает токен после записи и выявляет отказ сохранения."

The write-then-verify lives in `store_namespaced_token`
(`src/csk/build_https.py:275-311`), which runs from `csk config build-https login`,
not from `csk install`. The installer never writes a token. A reader following this
entry will look for the failure during install and not find it.

The note also does not quote the message the operator actually sees, so the entry is
not findable by it: "your Git credential helper did not persist the token. Configure
a working store ... on Windows either use an interactive session or 'git config
--global credential.credentialStore dpapi' — or select token_env instead"
(`src/csk/build_https.py:307-311`).

Suggested: attribute the behaviour to `csk config build-https login`, quote at least
the "did not persist the token" fragment, and keep the two remedies already present.

### 4. 301 entry promises output that csk discards (docs/troubleshooting.md:75)

Current: "Установщик перехватывает сбой `fatal: ... The requested URL returned error:
301` и завершает работу с кодом ошибки `build_repository_source_unavailable`."

`_run_git` calls the fetch with `stderr=subprocess.DEVNULL`
(`src/csk/git_admission.py:1053`), so git's `fatal: ...301` line never reaches the
operator through csk. What csk prints is
`build_repository_source_unavailable: exact external source is unavailable`
(`src/csk/git_admission.py:1064`, `src/csk/build_repository_pipeline.py:462`). The
301 text is what the operator sees when reproducing the fetch with plain git.

Suggested: state that csk reports only `build_repository_source_unavailable: exact
external source is unavailable`, and that a manual `git -c http.followRedirects=false
ls-remote <url>` reproduces the underlying `fatal: ... The requested URL returned
error: 301`. That keeps both search texts in the entry and stops promising output
that does not exist.

Also minor in the same paragraph: "утилита fetch выкачивает репозиторий" calls a git
subcommand a utility. Prefer "установщик выполняет `git fetch` с
`http.followRedirects=false`".

## Non-blocking observations

The numbered list in the credential entry breaks the file's established shape (every
other entry is prose plus one code block), and because the fenced blocks sit at
column 0 the ordered list is split into three separate lists in Markdown. The three
branches are genuinely parallel cases, so a list is defensible under the style guide,
but indenting the blocks under their items would keep it one list. Branch 3 names the
"clone once over HTTPS" remedy in prose without a command, while the TZ asked for a
command per branch; the `login` command shown does cover it.

No logbook entry exists for this task, while the sibling docs tasks
(TASK-260824-1d7zbo, TASK-260824-3gv521) both logged theirs.

## Out-of-scope regression spotted, for the orchestrator

`LOGBOOK.md` is corrupted by a sibling task's edit: the heading
`## 2026-08-24 - TASK-260824-2h0vjy a byte pin needs a byte-stable checkout` lost its
`## 2` prefix and now reads `026-08-24 - TASK-260824-2h0vjy ...` as body text
(introduced by the TASK-260824-1d7zbo entry insertion at the top of the file). Not in
this task's scope; needs a separate fix before the docs scope is committed.
