# TASK-260822-3ah6pu review verdict: accepted

Reviewer run RUN-260822-14536f. Repo `/Users/iv/Developer/Wildberries/cocoaskills`
at `818049c` plus uncommitted docs changes. Binary under test
`/opt/homebrew/bin/csk` reporting `csk 0.14.1`. This is the second review cycle;
the first (RUN-260822-91e8eb) returned changes requested with five findings.

## Verdict

Accepted. All five findings from the previous cycle are fixed and independently
verified. One cosmetic typo remains, recorded below as a non-blocking note.

## Prior findings, re-verified

### F1 (blocking, fixed): stale `--build-ssh-*` flag descriptions

All four flag tables now carry 0.14.1 semantics, and no pre-0.14 wording
survives anywhere in the file:

    $ grep -n "путь к приватному ключу SSH\|сокет SSH-агента для доступа" docs/cli.md
    (exit 1)

    $ grep -n '`--build-ssh-identity PATH`' docs/cli.md
    242:* `--build-ssh-identity PATH`: файл идентичности SSH для приватных репозиториев сборки; приватный ключ или соответствующий публичный ключ в сочетании с `--build-ssh-agent` (переменная: `CSK_BUILD_SSH_IDENTITY`).
    300:* `--build-ssh-identity PATH`: файл идентичности SSH для приватных репозиториев сборки; приватный ключ или соответствующий публичный ключ в сочетании с `--build-ssh-agent` (переменная: `CSK_BUILD_SSH_IDENTITY`).
    486:* `--build-ssh-identity PATH`: файл идентичности SSH для приватных репозиториев сборки; приватный ключ или соответствующий публичный ключ в сочетании с `--build-ssh-agent` (переменная: `CSK_BUILD_SSH_IDENTITY`).
    543:* `--build-ssh-identity PATH`: файл идентичности SSH для приватных репозиториев сборки; приватный ключ или соответствующий публичный ключ в сочетании с `--build-ssh-agent` (переменная: `CSK_BUILD_SSH_IDENTITY`).

    $ grep -n '`--build-ssh-agent \[SOCKET\]`' docs/cli.md
    243:* `--build-ssh-agent [SOCKET]`: сокет SSH-агента для приватных репозиториев сборки; голый флаг или `auto` берёт `SSH_AUTH_SOCK` (переменная: `CSK_BUILD_SSH_AGENT`).
    301:* `--build-ssh-agent [SOCKET]`: сокет SSH-агента для приватных репозиториев сборки; голый флаг или `auto` берёт `SSH_AUTH_SOCK` (переменная: `CSK_BUILD_SSH_AGENT`).
    487:* `--build-ssh-agent [SOCKET]`: сокет SSH-агента для приватных репозиториев сборки; голый флаг или `auto` берёт `SSH_AUTH_SOCK` (переменная: `CSK_BUILD_SSH_AGENT`).
    544:* `--build-ssh-agent [SOCKET]`: сокет SSH-агента для приватных репозиториев сборки; голый флаг или `auto` берёт `SSH_AUTH_SOCK` (переменная: `CSK_BUILD_SSH_AGENT`).

Lines 242/243 cover `csk install`, 300/301 `csk upgrade`, 486/487
`csk global install`, 543/544 `csk global upgrade`. I re-read live help for all
four commands; the `--build-ssh-*` option blocks are byte-identical across them:

      --build-ssh-identity PATH
                            SSH identity for private build repositories; a private
                            key, or the matching public key when combined with
                            --build-ssh-agent (env: CSK_BUILD_SSH_IDENTITY)
      --build-ssh-agent [SOCKET]
                            SSH agent socket for private build repositories; bare
                            flag or 'auto' adopts SSH_AUTH_SOCK (env:
                            CSK_BUILD_SSH_AGENT)

The Russian rendering carries both facts the old text dropped: identity also
accepts the matching public key when combined with the agent flag, and the agent
flag accepts a bare form or `auto`. The self-contradiction between the flag table
and the new `csk config build-ssh` section is gone.

### F2 (blocking, fixed): outcome resource evidence

`TASK-260822-3ah6pu_results.md` was regenerated. I extracted every claimed
command block from the resource and diffed it against real output.

The repository verification blocks are literal, unedited output:

    $ grep -n -C 3 "build-ssh" docs/cli.md > /tmp/real_grep.txt
    $ diff /tmp/claimed_grep.txt /tmp/real_grep.txt && echo "GREP BLOCK IDENTICAL"
    GREP BLOCK IDENTICAL   (160 lines both sides)

    $ git diff docs/cli.md docs/reference.md > /tmp/real_diff.txt
    $ diff /tmp/claimed_diff.txt /tmp/real_diff.txt && echo "DIFF BLOCK IDENTICAL"
    DIFF BLOCK IDENTICAL   (209 lines both sides)

All six live-help transcripts in the resource are genuine. I re-ran each command
against the installed binary and compared the captured stdout:

    MATCH   ### `csk config build-ssh --help`
    MATCH   ### `csk config build-ssh add --help`
    MATCH   ### `csk config build-ssh list --help`
    MATCH   ### `csk config build-ssh remove --help`
    MATCH   ### `csk install --help`
    MATCH   ### `csk bootstrap --help`

The resource's overview section no longer claims anything absent from the diff.
Its "Flag Descriptions" bullet now corresponds to a real hunk.

### F3 (minor, fixed): persistence qualifier

`docs/cli.md:220` now reads "выбор сохраняется в конфиг только после явного
выбора скоупа", and `docs/cli.md:466` carries the same qualifier for
`csk global install`. This matches `_prompt_build_ssh_rule`
(`src/csk/installer.py:928`, "Nothing is persisted without the explicit scope
choice") and the option `[3] this run only` branch at
`src/csk/installer.py:998-999`, which sets `persist = False`.

### F4 (minor, fixed): heading nesting

The three subcommand sections are flat `###`, matching every other multi-word
subcommand in the file:

    747:### csk config show
    769:### csk config build-ssh
    809:### csk config build-ssh add
    829:### csk config build-ssh list
    843:### csk config build-ssh remove
    858:### csk shell-init

### F5 (minor, fixed): `-h, --help` in the parent section

`docs/cli.md:783` lists `-h`, `--help` alongside `add`, `list`, and `remove`,
matching live help and the sibling sections.

## Independent verification of this cycle

Every synopsis block in the new sections is byte-identical to live 0.14.1 help.
I compared all four:

    $ /opt/homebrew/bin/csk config build-ssh --help
    usage: csk config build-ssh [-h] {add,list,remove} ...

    positional arguments:
      {add,list,remove}
        add              Add or replace one credential scope.
        list             List configured credential scopes.
        remove           Remove one credential scope.

    options:
      -h, --help         show this help message and exit

    $ /opt/homebrew/bin/csk config build-ssh add --help
    usage: csk config build-ssh add [-h] [--agent [SOCKET]] [--identity PATH]
                                    [--known-hosts PATH]
                                    scope

    positional arguments:
      scope               canonical-identity prefix, e.g. gitlab.example.com/group

    options:
      -h, --help          show this help message and exit
      --agent [SOCKET]    use the operator ssh-agent; bare flag or 'auto' adopts
                          SSH_AUTH_SOCK
      --identity PATH     identity file; a .pub pins which agent key is offered
      --known-hosts PATH  known_hosts override for this scope

    $ /opt/homebrew/bin/csk config build-ssh list --help
    usage: csk config build-ssh list [-h]

    $ /opt/homebrew/bin/csk config build-ssh remove --help
    usage: csk config build-ssh remove [-h] scope

Factual claims in the new prose trace to source:

- Candidate menu content, "агент с числом загруженных ключей и публичные ключи
  `*.pub` из `~/.ssh`": `discover_candidates` probes `ssh-add -l` for
  `agent_key_count` (`src/csk/build_ssh.py:180-201`) and globs `*.pub` from the
  operator `.ssh` directory (`src/csk/build_ssh.py:202-209`); the menu is built
  at `src/csk/installer.py:939-963`.
- Fail-closed remedy, "выводит готовые команды `csk config build-ssh add`,
  сформированные из найденных кандидатов": `src/csk/installer.py:1088-1111`
  raises with `SSH_CREDENTIAL_MISSING` and appends `candidate_commands(...)`
  built from `discover_candidates()` (`src/csk/build_ssh.py:224-236`).
- Bootstrap optional SSH question: `src/csk/cli.py:879-897` prompts
  "Configure SSH credentials for private build repositories now? [y/N]" and the
  scope, agent, and identity follow-ups. The claim is accurate even though
  `csk bootstrap --help` does not mention it.
- `docs/reference.md` is unchanged since the previous cycle, where its claims
  were traced to `src/csk/build_ssh.py` (`_HOST_RE`, `_scope_matches`, `match`,
  `parse_rules`) and `src/csk/git_admission.py:278`. The cross-link to
  `docs/external-build-repositories.md` is present and the target exists.

Prose-style hard bans are clean in both files:

    $ grep -c -E "—|–|«|»" docs/cli.md docs/reference.md
    docs/cli.md:0
    docs/reference.md:0

No antithesis constructions, filler openers, or marketing adjectives in the new
text.

## Non-blocking notes

### N1. One stray space before a semicolon at `docs/cli.md:220`

    $ sed -n '220p' docs/cli.md | python3 -c "..."
    'ключи `*.pub` из `~/.ssh`) ; выбор сохра'
    ['0x29', '0x20', '0x3b']

A plain ASCII space sits between `)` and `;`. It renders visibly. This is the
only typographic defect in the 129 added lines; a scan of the added text for
double spaces, space before punctuation, missing space after punctuation, and
trailing whitespace found nothing else. It does not violate any rule in
`docs/prose-style.md` and does not change what the reader learns, so it does not
block acceptance. Whoever next touches the file should apply:

    perl -pi -e 's/\Q`~\/.ssh`) ;\E/`~\/.ssh`);/' docs/cli.md

### N2. The three leaf sections omit the example block

`csk config build-ssh add`, `list`, and `remove` are the only 3 of 33 `###`
sections in `docs/cli.md` without a `**Пример использования:**` block and its
closing interpretive sentence. The parent `csk config build-ssh` section carries
one `**Примеры использования:**` block covering all four invocations, so the
information is present and duplicating it per leaf would be noise. I read this as
a defensible adaptation of the template rather than a defect, and I am recording
it so a later editor does not "fix" it without knowing it was deliberate.

## DoD status

- build-ssh CLI block and reference section landed, every flag verified against
  live 0.14.1 help with literal outputs in the outcome: pass.
- Docs updated and consistent with current code: pass.
- No discrepancies between code and description: pass.
- Result linked as a new task-scoped outcome resource: pass.
  `TASK-260822-3ah6pu_results.md` was regenerated this cycle and its evidence
  blocks are verifiable byte for byte.
- Important findings recorded in logbook: pass. Appended an acceptance entry to
  `LOGBOOK.md` alongside the prior verdict and rework entries.
- Implementation matches AC: pass. Drafts landed and adapted to the surrounding
  structure, every synopsis and flag matches live 0.14.1 help, prose-style clean,
  cross-link present.
- Solution fits project architecture: pass. Placement, section template, and
  factual claims line up with the code and the surrounding documents.
- Tests green: not applicable and not run. The change touches only
  `docs/cli.md` and `docs/reference.md`. No test references either file
  (`grep -rln "docs/cli\|docs/reference" tests/` returns only
  `tests/test_build_repository_pipeline.py`, whose two hits are an unrelated
  `docs/secret.txt` snapshot fixture), `.github/workflows/ci.yml` has no docs or
  link-check job, and no markdown in the repo anchors into either file. The
  working tree also carries unrelated modifications from sibling tasks
  (`CONTRIBUTING.ru.md`, `docs/external-build-repositories.md`,
  `docs/skill-authoring.md`, `docs/troubleshooting.md`, `LOGBOOK.md`), so a suite
  run here would not attribute to this task.

## Handoff to the commit-owning mover

This reviewer run supplies no `commit_ack`. Acceptance evidence is this
document. Commit scope for the task is `docs/cli.md` and `docs/reference.md`
only; the other modified files in the working tree belong to sibling tasks.
