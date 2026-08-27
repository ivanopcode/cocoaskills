# Review verdict 4: TASK-260822-1jns5p (troubleshooting-and-releaser-note)

Reviewer run: RUN-260822-9eb0cd (not goal-bound).
Verdict: **changes requested** -> `to-dev`.
Reviewed tree: /Users/iv/Developer/Wildberries/cocoaskills @ 818049c (uncommitted docs work).
Reviewed content hashes (files were still being written when this run started; everything
below was verified against these exact bytes):

    8369fd8153bcfb8dab99bc3ba1ebf8b78462603bfa3e75e25f573b9340fe8f00  docs/troubleshooting.md
    7b8309d8deba31e317bf8c5018ffd7226a5a765f94eb7b72f4058784145d7766  CONTRIBUTING.ru.md

Previous cycles: RUN-260822-5b46b2 (F1-F5), RUN-260822-8f8ecf (R1-R3), RUN-260822-0d11f0 (V1-V4).

## Previous findings: all four of cycle 3 closed

### V1 (blocking, closed) marker version band is now correct and independently re-derived

`docs/troubleshooting.md:7` now reads "записан версией csk до 0.12.0 включительно в микросекундном
формате; начиная с 0.12.1 csk пишет метку с точностью до секунды". Re-derived from scratch:

    $ grep -rn "isoformat()" src/csk/*.py
    src/csk/installer.py:3176:            .isoformat()          # single marker writer
    $ git log --oneline -S "microsecond=0" -- src/csk/installer.py
    76f89d8 Consume the authoritative Curator protocol suite
    $ git tag --contains 76f89d8 | sort -V | head -3
    v0.12.1 v0.12.2 v0.12.3

`installer.py:3175` is the only place that truncates the timestamp, and the earliest tag carrying
it is `v0.12.1`. The doc's boundary matches.

The remedy claim also holds: a marker that fails validation does not abort the install.
`_marker_is_current` (`src/csk/installer.py:3237,3266`) catches `InstallMarkerError` from
`parse_install_marker` and returns `False`, so the skill is reinstalled and the marker rewritten.
The old schema version is still accepted (`SUPPORTED_INSTALL_MARKER_SCHEMA_VERSIONS` holds v1, v2
and v3, `src/csk/install_marker.py:29`), so the reader does not hit
`Unsupported installed marker schema` instead.

### V2a / V2b (closed) English technical terms restored

`:11` now says "поверх symlink" and `:47` "по локальным refs". `docs/prose-style.md:126` names both
as must-stay-English, and `docs/reference.md:258` uses `symlinks` untranslated. No Russian rendering
of either term remains in the file.

### V3 (closed in intent, see W1) symptom 5 lead-in no longer invents an index

`:49` no longer says "локальный индекс репозиториев". The replacement introduces a new terminology
defect, filed as W1 below.

### V4 (closed) releaser note calls `publish-testpypi` a job

`CONTRIBUTING.ru.md:72` now reads "на стабильном релизе джоба `publish-testpypi` пропускается".
`.github/workflows/release.yml:64` declares `publish-testpypi:` as a job, and the transitive skip
the paragraph teaches only propagates through `needs`, so the noun now matches the mechanism.

## Independently re-verified this cycle

All six error strings resolve to live code:

| # | Doc heading | Source | Verified |
|---|---|---|---|
| 1 | `installed_at is not a UTC second timestamp` | `src/csk/install_marker.py:453`, surfaced by `src/csk/status.py:529` as `invalid install marker {path}: {exc}` | yes |
| 2 | `unsafe transaction tree entry` | `src/csk/transactions.py:1737`, raised when a tree entry is neither dir nor regular file (the symlinks a `.venv` carries) | yes |
| 3 | `go-v1 toolchain_executable_mismatch: selected Go executable is not below a GOROOT bin directory` | `src/csk/builds/toolchain.py:1037-1039`; `ToolchainError.__init__` formats `go-v1 {code}: {detail}` (`toolchain.py:105`), so the heading is contiguous and verbatim | yes |
| 4 | `build_repository_ssh_credential_missing` | `src/csk/git_admission.py:44`, message assembled at `src/csk/installer.py:1088-1112` | yes |
| 5 | `Cannot resolve tag '...'` | `src/csk/closure.py:178`; tail `Needed a single revision` is git's own | yes |
| 6 | `commands are installed in ..., which is not on PATH` | `src/csk/installer.py:596` | yes |

Remedies against live `csk 0.14.1` (`/opt/homebrew/bin/csk`):

- `csk install --help`: "Apply Skillfile.json using local refs. Missing git URL sources are cloned."
  This backs `:47` (install works from local refs, no fetch into an existing clone).
- `csk upgrade --help`: "Fetch the selected project dependency closure, then install."
  `csk global --help` lists `upgrade` as "fetch global skill sources, then install".
- `csk config build-ssh add --help` matches `:40` flag for flag, including
  "bare flag or 'auto' adopts SSH_AUTH_SOCK".
- `csk shell-init --help`: `--install` "atomically cache the hook under the CocoaSkills home and
  print the profile source command", which is what `:67` states.
- Runtime layout is `runtime/<name>/<commit>` (`src/csk/global_install.py:646,1136`), so the glob
  `~/.cocoaskills/runtime/*/*/.venv` at `:16` is correct.
- TTY split at `:35` matches the precheck: the candidate prompt runs only under `interactive`
  (`src/csk/installer.py:1068-1073`); otherwise the run raises `InstallError` carrying
  `build_repository_ssh_credential_missing` plus the ready `csk config build-ssh add` line
  (`installer.py:1088-1112`).
- `:43` matches `src/csk/cli.py:1017-1018`: the subcommand saves the config itself and prints
  `Configured build-ssh scope {scope}`; no install runs, and the doc now tells the reader to re-run it.

Releaser note facts: `bump-homebrew-tap` at `.github/workflows/release.yml:178`,
`needs: [build, publish-pypi]` at :180, condition `if: >- ${{ always() && ... }}` at :186-189;
`publish-testpypi` is a job at :64 with `if: needs.build.outputs.prerelease == 'true'`, and
`publish-pypi` needs it at :105, which is the transitive chain the note describes.
`brew trust --tap ivanopcode/csk` immediately precedes `brew install cocoaskills` at
`.github/workflows/distribution-smoke.yml:347-348`.

Rendering and style: every `##` heading re-rendered through CommonMark; `<skill>` and `<commit>`
stay escaped inside `<code>`, no other heading carries raw angle brackets. No em-dashes, en-dashes
or guillemets in either file. Each of the five code blocks is introduced by a colon sentence and
followed by an interpreting sentence. No antithesis constructions, filler openers, marketing
register, or closing summary.

Scope and tests: `git diff --name-only HEAD` lists only `*.md` plus `.gitignore` from sibling tasks;
`docs/troubleshooting.md` and `Skillfile.json` are the only new untracked files. Nothing under
`src/` or `tests/` changed, and no test references the docs tree (the single `docs/` hit in
`tests/test_build_repository_pipeline.py:43` is a `docs/secret.txt` fixture path), so the suite is
unaffected. I did not run it. README untouched: `git status --short README.md` and
`git diff HEAD -- README.md` are both empty.

## Findings (blocking)

### W1. `закрытие зависимостей` rotates the repo's defined Russian term for dependency closure

`docs/troubleshooting.md:49`:

    Скачайте новые refs и переустановите закрытие зависимостей:

Every other Russian text in the repo calls this concept `замыкание зависимостей`:

    docs/reference.md:109      транзитивное замыкание зависимостей
    docs/cli.md:485            затягивает свои обязательные зависимости в замыкание исполнения
    docs/skill-authoring.md:147,181
    docs/v0.9-design.ru.md:9,16,18,117,123,140-143,160,177
    CHANGELOG.md:16,20,223

`docs/troubleshooting.md:49` is the only occurrence of `закрытие` in the repo:

    $ grep -rn "замыкани\|закрыти" --include="*.md" . | grep -v "^./.git"
    docs/troubleshooting.md:49:Скачайте новые refs и переустановите закрытие зависимостей:
    ... every other hit is замыкание ...

`docs/prose-style.md` forbids exactly this: "Define a term at first use, then repeat it verbatim.
Never rotate synonyms", and the acceptance criteria require prose-style clean. `закрытие` also
reads as the act of closing rather than the graph closure that `csk upgrade --help` calls the
"dependency closure", so a reader coming from `docs/reference.md:109` meets a second name for the
installer's central concept. This was introduced by the V3 rework; V3 itself (dropping the invented
"индекс репозиториев") is otherwise correct.

Remedy, single word:

    Скачайте новые refs и переустановите замыкание зависимостей:

## Observations (do not change unless you want to)

### W2. The intro band `<=0.12` is coarser than symptom 1's own boundary

`:3` frames the page as symptoms seen when upgrading from csk `<=0.12`, while `:7` now pins the
marker change at 0.12.0 / 0.12.1. The intro covers all six symptoms, not just the marker, and the
wording is the owner TZ's. Fine as is; only worth a touch if you want `<=0.12.0` there.

### W3. The `.venv` remedy does not say the skill will do it again

`:11-19` clears `~/.cocoaskills/runtime/*/*/.venv`, but a skill that still bootstraps its
environment into the runtime tree recreates the condition on its next run. `docs/skill-authoring.md`
already carries that antipattern ("Не пиши в runtime-дерево установленного скилла"). The TZ draft
has no pointer either, so this is a deliberate omission to confirm, not a defect.

## Rework routing

Status set to `to-dev`. Apply W1 (one word at `docs/troubleshooting.md:49`), leave everything else
alone, and hand back for a fifth reviewer cycle. Both files stay docs-only; README stays untouched
(its troubleshooting link belongs to the readme-and-changelog task).

Verification to include in the outcome resource:

    grep -n "замыкание зависимостей" docs/troubleshooting.md
    grep -rn "закрыти" --include="*.md" docs/ | grep -v "^Binary"
