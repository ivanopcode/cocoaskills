# TASK-260720-3c0ss2 — Build-source identity and context isolation

## Candidate identity

- Repository: `/Users/iv/Developer/Wildberries/cocoaskills`
- Fast-forwarded clean base: `dd76b570f88339fd1d659c02950e68b17f6ba834`
- Worktree: `/Users/iv/Developer/Wildberries/cocoaskills/.temp/TASK-260720-3c0ss2/worktree`
- Branch: `task/TASK-260720-3c0ss2-build-source-identity`
- Dependency handoff `TASK-260720-z9j4c9`: accepted and present at the base SHA

## Implemented scope

- Added the exact `curator-build-source-v1` incremental framing and public
  in-memory digest helper without altering the legacy `content_sha256` body.
- Added a frozen build-source snapshot boundary that validates every descendant
  without following links, hashes every regular file (including root
  `.csk-install.json`), rejects links/reparse points, special files, invalid or
  duplicate portable paths, and supported-platform collisions, and rechecks the
  retained tree immediately before and after its consumer callback.
- Added static build-root exclusion to context copying, locale discovery and
  rendering, and skill checking.
- Wired declared build roots through install dry-runs, real installs, and
  up-to-date/cache-hit context refreshes so they are neither prompt-visible nor
  runtime-copied.
- Added focused unit and integration tests for framing, traversal, mutation,
  marker semantics, nested context roots, locale isolation, dry-run, real
  install, and up-to-date behavior.

No receipts, cache publication, installer commit behavior, or Go implementation
was added.

## Acceptance evidence

- Accepted rc.5 shared conformance root:
  `/Users/iv/Developer/ReluxWorks/curator/.temp/TASK-260729-3nx97g/worktree/conformance/v1`
- Manifest SHA-256:
  `b6f56aacc0e37dcc6692f73f641bff761e89b645adfe20a47a06d81c6fda204c`
- Shared build-source vector:
  `sha256:27cdcac0734aa3e069e95a10341e89b118a07c60002516e7b401e95477f01332`
- Shared eligible prompt context:
  `["SKILL.md", "assets/prompt.md"]`
- Empty digest:
  `sha256:3a51885eb057203c3099f50fc8e0c7953a607383214df787790508115b784852`
- Binary/empty/marker/order digest:
  `sha256:68008a0cb23020357e0b066b7b20c207b89697d46670bb6e0e2b7c6aca9c12af`

Focused tests also prove structural collision framing, invalid/duplicate and
platform-colliding paths, root and descendant links, a FIFO, root replacement,
file/tree/link mutations, pre-callback and post-callback mutation detection,
mode/mtime non-identity, marker-sensitive build identity, and marker-insensitive
legacy installed-tree identity.

## Gate ledger

| Gate | Result | Exit |
|---|---:|---:|
| Baseline focused pytest | 24 passed | 0 |
| Focused implementation + install integration pytest | 49 passed | 0 |
| rc.5 conformance + focused pytest | 150 passed | 0 |
| Direct shared fixture probe with `PYTHONPATH=src` | exact digest/context match | 0 |
| Strict `python -m mypy` | 57 source files clean | 0 |
| Full `python -m pytest -q` | 595 passed, 19 skipped | 0 |
| `python -m build` | wheel and sdist built | 0 |
| `python -m twine check dist/*` | wheel and sdist passed | 0 |
| Distribution inventory assertion | new module in wheel and sdist | 0 |
| `python -m compileall -q src/csk tests` | no diagnostics | 0 |
| `git diff --check` | no diagnostics | 0 |

There is no configured Ruff, Flake8, or Pylint command in this repository.
Strict mypy plus diff hygiene are the available configured lint/type gates.

## Expected-red and corrected development runs

- The test-first focused run exited 2 before `csk.builds.source` existed. This
  was the expected missing-module red gate.
- The first implementation-focused run exited 1 because a new test used
  `assets/README.md` as an allegedly eligible unrelated asset; the existing
  whitelist deliberately excludes `README*`. The fixture was corrected to
  `assets/guide.md`, after which the focused gate passed.
- The first direct shared-vector probe exited 1 because the inline process did
  not set `PYTHONPATH=src`; the corrected probe exited 0 with the exact accepted
  digest and context list.
- Early strict mypy runs exited 1 first because the generated ignored
  `src/csk/_version.py` was absent and then because new annotations required
  correction. The distribution build generated the version module, annotations
  were corrected, and the exact strict gate subsequently exited 0.

## Changed paths

- `src/csk/builds/source.py` (new)
- `src/csk/builds/__init__.py`
- `src/csk/hashing.py`
- `src/csk/whitelist.py`
- `src/csk/locale.py`
- `src/csk/skillcheck.py`
- `src/csk/installer.py`
- `tests/test_build_source.py` (new)
- `tests/test_whitelist.py`
- `tests/test_skillcheck.py`
- `tests/test_install.py`

The worktree remains unstaged and uncommitted for review.
