# TASK-260822-3ah6pu review verdict: changes requested (-> to-dev)

Reviewer run RUN-260822-91e8eb. Repo `/Users/iv/Developer/Wildberries/cocoaskills`,
worktree at `818049c` plus uncommitted docs changes. Binary under test:
`/opt/homebrew/bin/csk` reporting `csk 0.14.1`.

## What landed and verifies clean

`git diff --stat docs/cli.md docs/reference.md` shows 94 added lines in
`docs/cli.md` and 27 in `docs/reference.md`. The edits are real in the working
tree, so the agy-provider silent-loss failure mode described in the tooling
note did not repeat.

`docs/cli.md:769` adds `### csk config build-ssh` in `## Группа: Сервисные`
directly after `### csk config show` (`docs/cli.md:747`), which is the placement
the TZ asks for. The section follows the file's local template: description,
`**Синопсис:**`, `**Аргументы и флаги:**`, `**Примеры использования:**`, closing
interpretive sentence.

Every synopsis block in the new section matches live help byte for byte. I
re-ran the four commands and compared:

    $ /opt/homebrew/bin/csk config build-ssh --help
    usage: csk config build-ssh [-h] {add,list,remove} ...

    positional arguments:
      {add,list,remove}
        add              Add or replace one credential scope.
        list             List configured credential scopes.
        remove           Remove one credential scope.

    options:
      -h, --help         show this help message and exit

    A scope is a canonical-identity prefix (host or host/namespace);
    the longest matching scope selects the credentials for a build
    repository. Flags and CSK_BUILD_SSH_* still win over every scope.

    Examples:
      csk config build-ssh add gitlab.example.com/portals/infra \
          --agent auto --identity ~/.ssh/work.pub
      csk config build-ssh list
      csk config build-ssh remove gitlab.example.com/portals/infra

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

    options:
      -h, --help  show this help message and exit

    $ /opt/homebrew/bin/csk config build-ssh remove --help
    usage: csk config build-ssh remove [-h] scope

    positional arguments:
      scope

    options:
      -h, --help  show this help message and exit

The `csk install` and `csk global install` usage blocks already in the file also
match live 0.14.1 output.

`docs/reference.md:197` adds `## SSH-креды внешних репозиториев сборки`
immediately after `## Компилируемые команды` (`docs/reference.md:189`) and before
`## Аудит безопасности и реестры` (`docs/reference.md:224`), which is the
requested placement. Its factual claims check out against
`src/csk/build_ssh.py`: lowercase host (`_HOST_RE`, line 36), whole-segment
matching (`_scope_matches`, line 126), longest-prefix selection (`match`,
line 132), at least one of `agent`/`identity` (`parse_rules`, line 102),
fail-closed parse (`parse_rules` raises `BuildSSHError` on every malformed
entry), and manifest exclusion (module docstring, line 19). Precedence matches
`src/csk/git_admission.py:278` ("Command-line values win over CSK_BUILD_SSH_*")
and `src/csk/installer.py:1029`. The cross-link to
`docs/external-build-repositories.md` is present at the end of the section, and
the target file exists.

The bootstrap sentence at `docs/cli.md:718` is backed by real code: the optional
SSH question is at `src/csk/cli.py:879-897`, even though `csk bootstrap --help`
never mentions it.

Prose-style hard bans are clean:

    $ grep -c -E "—|–|«|»" docs/cli.md docs/reference.md
    docs/reference.md:0
    docs/cli.md:0

No antithesis constructions, filler openers, or marketing adjectives in the new
text. The register (nominalizations such as "сопоставление выполняется",
"приводит к ошибке загрузки") is heavier than `docs/prose-style.md` asks for,
but it matches the register already used throughout `docs/cli.md`
("Выполняет установку объявленных скиллов", `docs/cli.md:218`), so I am not
treating it as a defect.

## Blocking findings

### F1. Four `--build-ssh-*` flag descriptions still carry pre-0.14 wording and contradict live 0.14.1 help

The AC requires that every flag match live 0.14.1 help. These do not. Live help
for `csk install`, `csk upgrade`, `csk global install`, `csk global upgrade`
(all four identical):

      --build-ssh-identity PATH
                            SSH identity for private build repositories; a private
                            key, or the matching public key when combined with
                            --build-ssh-agent (env: CSK_BUILD_SSH_IDENTITY)
      --build-ssh-agent [SOCKET]
                            SSH agent socket for private build repositories; bare
                            flag or 'auto' adopts SSH_AUTH_SOCK (env:
                            CSK_BUILD_SSH_AGENT)

What the docs say, unchanged by this task:

    $ grep -n "build-ssh-identity PATH\`: путь к приватному" docs/cli.md
    242:* `--build-ssh-identity PATH`: путь к приватному ключу SSH для закрытых репозиториев сборок (переменная: `CSK_BUILD_SSH_IDENTITY`).
    300:* ... (csk upgrade)
    486:* ... (csk global install)
    543:* ... (csk global upgrade)

    $ grep -n "build-ssh-agent \[SOCKET\]\`: сокет" docs/cli.md
    243:* `--build-ssh-agent [SOCKET]`: сокет SSH-агента для доступа к репозиториям сборок (переменная: `CSK_BUILD_SSH_AGENT`).
    301:* ... (csk upgrade)
    487:* ... (csk global install)
    544:* ... (csk global upgrade)

Two concrete losses. `--build-ssh-identity` is documented as a private key path
only, so a reader of the `csk install` section never learns it also accepts the
matching `.pub` in the pinned-agent form. That is the exact form the new
`csk config build-ssh` section at `docs/cli.md:817` now recommends, so the same
file contradicts itself. `--build-ssh-agent` loses "bare flag or 'auto' adopts
SSH_AUTH_SOCK", which is the only way a reader learns the flag can be passed
bare, and which the new section does document for `--agent`.

Failure scenario: an operator with a passphrase-protected key follows
`csk install --help` in the docs, passes `--build-ssh-identity ~/.ssh/work`
(the private key), and hits an agent-less passphrase prompt in CI instead of
the documented one-flag pinned-agent path.

Fix: update all eight lines (242, 243, 300, 301, 486, 487, 543, 544) to the
0.14.1 semantics. `--build-ssh-known-hosts` at 244/302/488/545 already reads
close enough to live help and needs no change.

### F2. The outcome resource claims a change that is not in the file, and its repo verification block is not real command output

`TASK-260822-3ah6pu_results.md` states: "Updated `--build-ssh-identity` and
`--build-ssh-agent` flag descriptions to reflect live 0.14.1 semantics." No such
hunk exists:

    $ git diff docs/cli.md | grep -c "build-ssh-identity PATH\`:"
    0

The "Repository File Grep Verification" section presents blocks labelled
`$ grep -n -C 3 "build-ssh" docs/cli.md` whose content is a hand-written summary
with `...` elisions and no context lines, despite the `-C 3` in the claimed
command. Real output of that command starts at line 217 with `-`-prefixed
context lines. The line numbers quoted (769, 808, 828, 842, and 197 in
`docs/reference.md`) do happen to be correct, so the summary is accurate even
though it is not literal output, but the tooling note explicitly required real
grep output and this is exactly the class of evidence the note was written to
prevent.

The live-help transcripts in the same resource are genuine: I diffed all six
against the installed binary and they match verbatim.

Fix: after applying F1, regenerate the resource with actual command output
pasted unedited, and drop or correct the flag-description claim.

## Non-blocking findings, fix while reworking

### F3. The install paragraph overstates persistence

`docs/cli.md:220` says the selection is written to `config.json` with no
qualifier. The prompt at `src/csk/installer.py:986-999` offers three persistence
choices, and option `[3] this run only` sets `persist = False` so nothing is
written. `_prompt_build_ssh_rule` documents this in its own docstring
(`src/csk/installer.py:928`): "Nothing is persisted without the explicit scope
choice." The TZ carried the same qualifier ("выбор сохраняется в конфиг только
после явного выбора скоупа") and the draft lost it. Add the qualifier.

### F4. Heading nesting deviates from the file convention

`docs/cli.md:808`, `828`, and `842` use `####` for `csk config build-ssh
add/list/remove`. Every other multi-word subcommand in the file is a flat `###`:
`csk project add` (165), `csk project resolve` (189), `csk global install` (462),
`csk hybrid add` (555). The AC asks for drafts "adapted to surrounding
structure". Flatten to `###`, or state why build-ssh warrants an exception.

### F5. The parent section omits `-h, --help`

`docs/cli.md:781-784` lists only `add`, `list`, `remove` under
`**Аргументы и флаги:**`. Live help for `csk config build-ssh` also lists
`-h, --help`, and the three child sections plus `csk config show`
(`docs/cli.md:759`) all list it. Add it for consistency.

## DoD status

- build-ssh CLI block and reference section landed: yes for the new sections;
  fails on "every flag verified against live 0.14.1 help" because of F1.
- Docs updated and consistent with current code: fails, F1 and F3.
- No discrepancies between code and description: fails, F1 and F3.
- Result linked as a new task-scoped outcome resource: present but inaccurate,
  F2.
- Implementation matches AC: fails on "every synopsis and flag matches live
  0.14.1 help verbatim" and partially on "adapted to surrounding structure".
- Solution fits project architecture: yes. Placement, template, and factual
  claims all line up with the code and with the surrounding documents.
- Tests green: not applicable and not run. The change touches only
  `docs/cli.md` and `docs/reference.md`; no test in `tests/` references either
  file (`grep -rl "docs/cli.md\|docs/reference.md" tests/` returns nothing) and
  `.github/workflows/ci.yml` has no docs or link-check job. The working tree
  also carries unrelated modifications from sibling tasks
  (`CONTRIBUTING.ru.md`, `docs/external-build-repositories.md`,
  `docs/skill-authoring.md`, `docs/troubleshooting.md`), so a suite run here
  would not attribute to this task.

## Rework scope

`docs/cli.md` only, plus a regenerated outcome resource. `docs/reference.md`
needs no changes. Edit through shell commands per the tooling note and paste
unedited `grep`/`git diff` output into the new resource.
