# TASK-260824-1d7zbo review verdict: accepted

Reviewer run: cycle 2 (rework review after RUN-260824-1e957d requested changes).
Repo: /Users/iv/Developer/Wildberries/cocoaskills, branch `main`, working tree
uncommitted. Scope reviewed: README.md delta plus the collateral LOGBOOK.md edit
made by the same task.

## Prior blocking findings: both fixed

### Finding 1: LOGBOOK.md heading corruption -> fixed

The previous run ate the `## 2` prefix of the `TASK-260824-2h0vjy` heading. It is
restored and the diff is now purely additive:

    $ git diff LOGBOOK.md | grep -c '^-[^-]'
    0
    $ grep -n "TASK-260824-2h0vjy" LOGBOOK.md | head -5
    26:## 2026-08-24 - TASK-260824-2h0vjy a byte pin needs a byte-stable checkout
    62:## 2026-08-24 - TASK-260824-2h0vjy the audited protocol surface is a merge-tier tripwire

Zero removed lines, heading intact, the new entry sits above it with a blank line
separator. The document outline is whole again.

### Finding 2: duplicated closing sentence -> fixed

The run took the second remediation option: one shared closing sentence for both
transports. `git diff README.md` shows the sentence removed from the end of the SSH
paragraph and re-added once at the end of the whole section (README.md:161). The
section now reads SSH story, HTTPS story, one shared rule. No paste seam, no
restatement.

### Non-blocking pointer item -> also addressed

The reviewer note asked for the missing `docs/external-build-repositories.md`
pointer. It landed in two places: inline at README.md:161 and in the `## Дальше`
list at README.md:314. TZ acceptance item 1 (README links lead to the new sections)
is now satisfied.

## Acceptance criteria

Placement and tone. The block sits directly under the SSH quickstart inside
`### Приватные репозитории сборки: за один Enter`, README.md:153-161, 9 lines
including fence and blank lines. TZ asked for 6-10. Tone matches the SSH paragraph:
same register, same "В CI ..." shape, same code-block-after-colon pattern.

Command matches live help verbatim. Verified by parsing the documented string
through the actual CLI parser, not just by eyeballing help text:

    $ .venv/bin/python -c "from csk import cli; p = cli.build_parser(); \
      print(vars(p.parse_args(['config','build-https','add', \
      'gitlab.example.com/portals/infra','--token','git-credentials'])))"
    {'version': False, 'command': 'config', 'config_command': 'build-https',
     'build_https_command': 'add', 'scope': 'gitlab.example.com/portals/infra',
     'token': 'git-credentials', 'token_env': None, 'username': None}

Scope grammar accepted: `build_ssh.validate_scope('gitlab.example.com/portals/infra')`
returns without raising. `build_https.TOKEN_SOURCES == ('git-credentials', 'keyring')`,
so `git-credentials` is a real source. `.venv/bin/csk config build-https add --help`
confirms the `[--token {git-credentials,keyring}] ... scope` signature.

Environment variable names correct, verified in source rather than only in help:
`src/csk/installer.py:195` `OPERATOR_HTTPS_TOKEN_ENV = "CSK_BUILD_HTTPS_TOKEN"`,
`src/csk/installer.py:197` `OPERATOR_HTTPS_HOST_ENV = "CSK_BUILD_HTTPS_HOST"`. The
parenthetical about different hosts matches the help text, which states that
`CSK_BUILD_HTTPS_TOKEN` reaches every HTTPS host in the closure unless
`CSK_BUILD_HTTPS_HOST` pins it to one.

The "один Enter" claim holds. `src/csk/installer.py:1278-1290`: when the host
already has Git HTTPS credentials, `reuse your Git HTTPS credentials for <host>`
is option 1 and carries the `<- default` marker; the prompt accepts empty input as
1. The README qualifies the claim with "если вы уже клонируете по HTTPS", which is
exactly the `material.host_credentials` branch that produces that option.

No secret values. The only token-adjacent tokens in the block are the source name
`git-credentials` and the two env variable names. Token is never a flag value; the
CLI does not accept one.

Russian per file language, prose style clean. `sed -n '144,165p' README.md |
grep '—\|–\|«\|»'` returns nothing: no em-dashes, no en-dashes, no guillemets. No
antithesis construction, no filler opener, no marketing adjective, no closing
restatement. Each code block is introduced by a sentence ending in a colon.

Links resolve. Every `.md` target referenced from README.md was checked against the
filesystem; all 8 resolve, including the newly added
`docs/external-build-repositories.md` (17065 bytes, present).

Tests green:

    $ .venv/bin/pytest tests/test_build_https.py tests/test_cli.py -q
    98 passed, 28 warnings in 13.95s

No test in `tests/` reads the repository README, so a docs-only delta cannot regress
the suite. The `README` hits in `tests/` are fixture file names inside synthetic
skill packages.

## Fit with the project

The delta is additive documentation inside an existing section, using the shape the
surrounding README already uses. It documents a feature that is already merged into
`main` (`feat/build-https-broker`) and already covered in
`docs/external-build-repositories.md`, `docs/cli.md`, and `docs/reference.md`. The
README block is a pointer plus a minimal path, which is the right altitude for a
quickstart.

## Non-blocking observation for the story owner

The working tree still carries an unrelated `.gitignore` change (`# CocoaSkill` plus
`Skillfile.dev.json`, .gitignore:41-42) that is not present at HEAD and belongs to no
task in this story. It looks like the side effect of running `csk init` inside the
repo root. It was flagged in the previous cycle and is still unowned. Someone should
decide whether it ships with the docs PR or gets reverted before the story's commit
task opens.

## Verdict

Accepted. Acceptance evidence above is for the commit-owning mover: this reviewer run
supplies no `commit_ack`.
