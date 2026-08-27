# TASK-260821-2c7ter review: changes requested

Reviewer run, docs scope only (per the review scope note: the build-ssh WIP in
`src/csk/`, `tests/test_build_ssh.py`, `builds/*`, `Skillfile.json`,
`docs/external-build-repositories.md`, `.gitignore` is out of scope and was not
reviewed).

## Verified green

- `README.en.md` is deleted (`git status` shows `D README.en.md`).
- Link sweep is clean. `grep -rn "README\.en"` over the repo returns hits only
  in `LOGBOOK.md` and `.spec/*.md`, all as historical prose in code spans, not
  as Markdown links. `docs/index.html` and `docs/sitemap.xml` never referenced
  the file.
- `pyproject.toml:9` is `readme = "README.md"`.
- Packaging is green. `python -m build` exits 0 and `twine check` prints
  PASSED for both the wheel and the sdist.
- The Russian `README.md` renders correctly as a long description. A
  markdown-it render keeps all 10 `<details>`/`<summary>` pairs, all install
  code blocks, and the 6-row market table. `readme_renderer.clean.ALLOWED_TAGS`
  permits `details` and `summary`, so the collapsible install options survive
  PyPI sanitizing.
- Full suite green: `1458 passed, 244 skipped, 24 warnings in 243.86s`,
  exit 0 (`.temp/TASK-260821-2c7ter/pytest-full-01.log`). The producer reported
  1430/243; the delta is the foreign build-ssh tests in the tree, not a
  regression.
- The old CLI table is fully absorbed by `docs/cli.md`, including `csk
  --version` (`docs/cli.md:16,23,28`), the `csk hybrid` group, `--all`,
  `--strict-tags`, and exit codes 0-3. No command row fell through.
- Typography is clean: no em-dashes, en-dashes, or guillemets in `README.md`,
  `docs/reference.md`, `docs/cli.md`, `CONTRIBUTING.md`, `CONTRIBUTING.ru.md`.
- Dropping the design-doc links from the README index is safe: every RFC is
  indexed in the `ARCHITECTURE.md` table (`ARCHITECTURE.md:454-462`).

## Blocking findings

### 1. Material with no home, and not listed as dropped (AC)

The AC requires every unique section of `README.en.md` to have a Russian home
in `docs/` or to be listed in the outcome as dropped with a reason. The outcome
resource lists relocations only; it lists zero drops. Five pieces of content
are in neither place:

- Selective-operation semantics. `README.en.md:236-238` stated: "A selected
  skill pulls required dependencies into the execution closure. Unselected
  declarations remain unchanged on disk." This is real behavior
  (`src/csk/global_install.py:76`, `:94`, `:169`). `docs/cli.md:481` describes
  `--only NAME` only as "ограничивает операцию указанным глобальным скиллом",
  which does not tell the reader that requirements still join the closure.
  `docs/reference.md` does not cover `--only` at all.
- `~/.local/bin/` forwarders. `README.en.md:245` stated that `csk global
  install` publishes forwarders into user binary paths. The code does this
  (`src/csk/global_bins.py:315`). No file in `docs/` says so now;
  `docs/reference.md:170-176` lists only the three-step shim lookup chain.
- Global linking into `~/.agents/skills/`. `README.en.md:209-211` stated that
  global skills link there when OpenCode or Windsurf are enabled
  (`src/csk/adapters.py:22-26`). `README.md` "Глобальный режим" says only
  "пользовательские директории агентов в домашнем каталоге", and
  `docs/reference.md` omits it.
- The Curator Protocol attribution paragraph (`README.en.md:10`). The review
  on 2026-08-19 recorded this as an open owner call at story level
  (`LOGBOOK.md:387-391`). Deleting the file resolves it silently. It needs an
  explicit line in the outcome: dropped, with the reason, so the owner decision
  is closed on purpose rather than by omission.
- The `## License` section (`README.en.md:433-435`). `README.md` carries only
  the shields badge. `README.md` is now the PyPI long description, so the
  package page loses the Apache-2.0 statement and the `LICENSE` link that
  `README.en.md` put there.

Fix: relocate the first three into `docs/reference.md` (or `docs/cli.md` for
the `--only` sentence), add a short `## Лицензия` section to `README.md`, and
list the Curator Protocol paragraph plus anything else you decide to drop in
the outcome resource with a one-line reason each.

### 2. CONTRIBUTING language policy does not match reality (AC)

`CONTRIBUTING.md:55` and `CONTRIBUTING.ru.md:58` enumerate the Russian
documents as `README.md`, `docs/skill-authoring.md`, and `docs/cli.md`.
`docs/reference.md`, written in Russian by this same task, is missing from both
lists. The AC requires the policy to match reality in both files.

## Non-blocking findings

- `docs/reference.md` contains zero cross-references. `README.en.md:288`
  pointed at `ARCHITECTURE.md` for the full build contract, storage layout,
  worker handoff protocol, and security boundaries; the compiled-commands
  section in `docs/reference.md:216-224` drops that pointer and links nothing.
  `docs/prose-style.md` asks for a precise cross-reference target.
- `docs/reference.md:176` overstates `csk shell-init --install`: "добавляет
  пути бинарных файлов в переменные окружения". `src/csk/cli.py:634-640` writes
  the hook file and prints the profile line; the reader has to add it.
  `docs/cli.md:779` states this correctly.
- `README.md:7-8` has a double blank line left where the English-version link
  was removed. Cosmetic only; renders identically.

## Verdict

Routed to `to-dev`. Documentation-only rework; no source changes and no commit
expected from the producer. Re-run the full suite after the edits.
