# TASK-260824-1d7zbo review verdict: changes requested (to-dev)

Reviewer run: RUN-260824-1e957d. Repo: /Users/iv/Developer/Wildberries/cocoaskills
(working tree, uncommitted). Scope reviewed: README.md delta plus the collateral
LOGBOOK.md edit made by the same run.

## What passes

- Placement and length. The block sits directly under the existing SSH quickstart
  in `### Приватные репозитории сборки: за один Enter`, 7 lines including the fence
  (TZ asked for 6-10). README.md:146-152.
- Command verified verbatim against the live CLI:
  `.venv/bin/csk config build-https add --help` accepts
  `--token {git-credentials,keyring}` and a positional `scope`; the documented
  example `csk config build-https add gitlab.example.com/portals/infra --token git-credentials`
  matches the help text example shape exactly.
  Scope grammar checked directly: `build_ssh.validate_scope('gitlab.example.com/portals/infra')`
  returns without raising; `build_https.TOKEN_SOURCES == ('git-credentials', 'keyring')`.
- Environment variable names verified in code, not only in help:
  `src/csk/installer.py:195` `OPERATOR_HTTPS_TOKEN_ENV = "CSK_BUILD_HTTPS_TOKEN"`,
  `src/csk/installer.py:197` `OPERATOR_HTTPS_HOST_ENV = "CSK_BUILD_HTTPS_HOST"`.
  The parenthetical about pinning a host matches `src/csk/installer.py:1360-1361`.
- The "one Enter" claim matches the interactive flow: when the host already has Git
  HTTPS credentials, `reuse your Git HTTPS credentials for <host>` is option 1 and is
  marked the default (`src/csk/installer.py:1278-1290`), and the scope prompt defaults
  to the namespace on empty input. The SSH flow has the same two-prompt shape
  (`src/csk/installer.py:1018,1040`), so the section heading wording stays consistent.
- No secret values anywhere in the example; token is never a flag value.
- Language: Russian throughout, matching the file. No em-dashes, no en-dashes, no
  guillemets in the added lines (checked by grep over README.md:144-163).
- Tests: `.venv/bin/pytest tests/test_build_https.py tests/test_cli.py -q` -> 98 passed
  in 12.27s. Reproduces the implementer's claim. No test in `tests/` reads the repo
  README, so the delta cannot regress the suite.

## Blocking finding 1: the run corrupted an unrelated LOGBOOK.md entry

While adding its own logbook entry, the run ate the first four characters of the
next heading. `git show HEAD:LOGBOOK.md` line 3 reads:

    ## 2026-08-24 - TASK-260824-2h0vjy a byte pin needs a byte-stable checkout

The working tree now has, at LOGBOOK.md:16:

    026-08-24 - TASK-260824-2h0vjy a byte pin needs a byte-stable checkout

The `## 2` prefix is gone. The TASK-260824-2h0vjy record is no longer a heading; it
renders as a body paragraph of the new TASK-260824-1d7zbo entry, so a prior task's
narrative is now attributed to this task and the entry disappears from the document
outline. This is a destructive edit to an existing record, not a formatting nit.

Fix: restore the heading exactly as it is at HEAD, keeping the new entry above it and
a blank line between them. Verify with:

    grep -n "TASK-260824-2h0vjy" LOGBOOK.md
    git diff LOGBOOK.md   # the only removed line should be none

## Blocking finding 2: the closing sentence is a verbatim copy, not a variant

README.md:144 (SSH block) and README.md:152 (new HTTPS block) now end with the same
sentence, character for character, six lines apart:

    Пакет скилла выбрать креды не может: выбор делает только оператор и только явно.

The TZ asked for a closing sentence "в духе существующей", meaning a variant of it.
docs/prose-style.md rejects a closing that restates what was just written; an exact
duplicate inside one section reads as a paste seam. The section now runs
SSH story -> moral -> HTTPS story -> the same moral.

Fix (either is acceptable):
- reword the HTTPS closing so it carries the same rule in its own words, tied to
  HTTPS credentials specifically; or
- keep one shared closing sentence at the end of the section and drop it from the
  SSH paragraph, so the rule is stated once for both transports.

## Non-blocking observations for the story owner

- README.md never links to `docs/external-build-repositories.md`, neither from the SSH
  paragraph nor from the new HTTPS one, and the file is absent from the `## Дальше`
  list at README.md:312-318. TZ acceptance item 1 says README links lead to the new
  sections. This task's own AC does not require a link and the SSH block has the same
  gap, so it is not held against this delta; the story should either add the pointer
  here or record the deferral.
- The working tree carries an unrelated `.gitignore` change (`# CocoaSkill` plus
  `Skillfile.dev.json`, .gitignore:41-42) that is not present at HEAD and belongs to no
  task in this story. It looks like the side effect of running `csk init` inside the
  repo root. Someone should decide whether it ships with the docs PR or gets reverted
  before TASK-260824-3rzggb opens it.

## Verdict

Changes requested. Route to `to-dev`. The README prose is accurate and verified; fix
the LOGBOOK.md heading corruption and the duplicated closing sentence, then return the
task for another review cycle.
