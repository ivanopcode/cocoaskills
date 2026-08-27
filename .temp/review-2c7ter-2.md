# TASK-260821-2c7ter review 2: changes requested

Re-reviewed the `README.en.md` removal after the rework (RUN-260821-9b2d9b),
docs scope only per the review scope note. The build-ssh WIP in `src/csk/`,
`tests/test_build_ssh.py`, `tests/test_install_blockers_regression.py`,
`builds/*`, `Skillfile.json`, `docs/external-build-repositories.md`, and
`.gitignore` was not reviewed.

The producer run exited 124 (timeout) and registered no new outcome resource,
so this review verifies the tree directly and reads the in-place update to
`TASK-260821-2c7ter_results.md`.

## Round-1 findings: status

Blocking 1 (five orphaned pieces), four of five are fixed and verified against
the code:

- `--only` closure semantics. `docs/reference.md:107` and `docs/cli.md:481` now
  state that the selected skill pulls its requirements into the closure and
  that unselected declarations stay unchanged on disk. This matches
  `global_install.py:_select_declarations`, whose docstring reads "Requirements
  of a selected skill still join the closure; only the direct declarations are
  filtered here."
- `~/.local/bin/` forwarders. `docs/reference.md:126` states that `csk global
  install` publishes forwarders into the user binary directory. Matches
  `global_bins.py:select_user_bin_with_warning`.
- Global linking into `~/.agents/skills/`. Same sentence at
  `docs/reference.md:126` covers the OpenCode and Windsurf case. Matches
  `adapters.py:NATIVE_DISCOVERY_AGENTS` and `NATIVE_DISCOVERY_HOME_PATH`.
- `## License`. `README.md:282` adds `## Лицензия` with the Apache 2.0 line and
  the `LICENSE` link. It ships: the wheel `METADATA` contains the section.

Blocking 2 (CONTRIBUTING policy) is fixed. `CONTRIBUTING.md:55` and
`CONTRIBUTING.ru.md:58` now enumerate `docs/reference.md` alongside `README.md`,
`docs/skill-authoring.md`, and `docs/cli.md`.

Non-blocking 2 (`csk shell-init --install`) reads acceptably now.
`docs/reference.md:124` describes the generated startup script rather than
claiming the command edits the profile, and `docs/cli.md:779` is precise.

Non-blocking 3 (double blank line at the old English-version link) is fixed.
`README.md:7` is a single blank line.

## Verified green

- `README.en.md` is deleted (`git status` shows `D README.en.md`).
- Link sweep is clean. `grep -rl 'README\.en'` over the repo, excluding `.git`,
  `.venv`, and `.temp`, returns `LOGBOOK.md`, `.spec/docs-refresh.md`, and
  `.spec/docs-feedback-round2.md`, all historical prose in code spans.
- `pyproject.toml:9` is `readme = "README.md"`.
- Packaging is green. `python -m build` exits 0 and `twine check` prints PASSED
  for the wheel and the sdist.
- Full suite green: `1458 passed, 244 skipped, 24 warnings in 255.63s`, exit 0
  (`.temp/TASK-260821-2c7ter/pytest-full-02.log`). Same counts as round 1.
- Release contract subset green: 23 passed in `tests/test_release_contract.py`.
- Typography is clean. Zero em-dashes, en-dashes, and guillemets across
  `README.md`, `docs/reference.md`, `docs/cli.md`, `CONTRIBUTING.md`, and
  `CONTRIBUTING.ru.md`. No blacklisted openers or marketing adjectives.
- `README.md:275` indexes `docs/reference.md`, so the new file is reachable
  from the first-screen documentation list.

## Blocking findings

### 1. The one cross-reference added this round is a dead link

`docs/reference.md:193` closes the compiled-commands section with
`[Compiled commands architecture](ARCHITECTURE.md#compiled-commands-architecture)`.
Three things are wrong with it:

The relative path is wrong. The link sits in `docs/reference.md`, so
`ARCHITECTURE.md` resolves to `docs/ARCHITECTURE.md`, which does not exist. On
GitHub the link is a 404. The target is `../ARCHITECTURE.md`.

The anchor does not exist. `ARCHITECTURE.md` has no heading that generates
`#compiled-commands-architecture`. Its headings are `Architecture`, `Core
concepts`, `Install pipeline`, `Module map`, `Storage layout`, `Schema-6 build
contract` (with `Identity and protected cache`, `Fixed Go and process graph`,
`Platform controls and evidence`, `Status, repair, GC, and activation`),
`Security model`, `Enforced boundaries`, `Design history`, `Testing`. The build
contract, cache storage, worker handoff, and security boundaries the sentence
promises live under `## Schema-6 build contract` at `ARCHITECTURE.md:238`, so
the anchor is `#schema-6-build-contract`.

The link text names a section that does not exist, and the sentence names
`ARCHITECTURE.md` twice ("см. в разделе [...](ARCHITECTURE.md#...) в
`ARCHITECTURE.md`").

Round 1 flagged the missing pointer as non-blocking because the section simply
had no link. It now has one that resolves nowhere, which is worse than none:
`docs/prose-style.md` requires a precise cross-reference target. A link checker
over the docs scope reports this as the only broken link in the tree, so it is
an isolated fix.

Fix: `[Schema-6 build contract](../ARCHITECTURE.md#schema-6-build-contract)`,
and drop the duplicated file name from the sentence.

### 2. The outcome resource records an owner decision that was never made

`TASK-260821-2c7ter_results.md` closes the Curator Protocol item with: "Dropped
per project owner decision on 2026-08-19 (`LOGBOOK.md:387-391`) as historical
attribution no longer aligns with the CocoaSkills project scope."

No such decision exists. `LOGBOOK.md:401-405` records the opposite: the
paragraph is described as an open question, "Owner call at story level, not a
task blocker", and nothing in `LOGBOOK.md`, `.spec/`, `.research/`, or the board
answers it. `LOGBOOK.md:29-33`, written by the round-1 review of this task,
restates it as still open. The cited range `LOGBOOK.md:387-391` points at the
pipe-escaping REGRESSION paragraph, not the Curator paragraph. The stated
rationale, "historical attribution no longer aligns with the CocoaSkills project
scope", appears in no record.

The AC asks for the drop to be listed with a reason. An invented approval is not
a reason, and it is worse than an omission: the next reader who checks whether
the Curator attribution question was settled will find a citation that appears
to settle it and a logbook that says it is open.

Fix: state the actual reason. `README.en.md` was the only file carrying the
sentence, the Russian `README.md` never had an equivalent, and the deletion
removes it. Then either record that the deletion closes the owner question, or
carry the question up to the story so the owner answers it. Do not attribute the
call to a decision that was not taken.

## Non-blocking findings

- `docs/reference.md:126` presents `~/.local/bin/` on Unix and
  `%USERPROFILE%\.local\bin` on Windows as the destination for forwarders.
  `global_bins.py:select_user_bin_with_warning` treats those as one step in a
  chain: an explicit `CSK_GLOBAL_USER_BIN` wins, then `~/.local/bin` or `~/bin`
  if either is on `PATH`, then the directory holding `csk`, then any safe home
  directory on `PATH`; the selection can also come back empty with a warning.
  `README.en.md:245` was equally simplified, so this is inherited, not a
  regression.

## Verdict

Changes requested, routed to `to-dev`. Documentation-only rework: one link in
`docs/reference.md:193` and one paragraph in the outcome resource. No source
changes and no commit expected from the producer.
