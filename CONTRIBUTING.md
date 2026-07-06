# Contributing

Translations: [Русский](CONTRIBUTING.ru.md). English is the source of truth.

## Development setup

Requires Python 3.11+ and git.

```bash
git clone https://github.com/ivanopcode/cocoaskills.git
cd cocoaskills
python -m venv .venv
source .venv/bin/activate
python -m pip install -e ".[dev]"
pytest
```

The test suite builds throwaway git repositories and runs the real install
pipeline against temporary stores; no network access is required.

## Code conventions

- The codebase type-checks under `mypy` strict mode; run `python -m mypy`
  before pushing. Configuration lives in `pyproject.toml`.
- The runtime package uses the standard library only. New runtime dependencies
  require a design discussion first.
- Match the surrounding code: module layout, error types per module, dataclass
  models, and explicit validation with actionable messages.
- Names that become filesystem paths go through `identifiers.py`.
- Everything that parses third-party input (manifests, archives, URLs)
  validates before acting and never executes manifest-provided code.
- Tests accompany the change. Command fixtures ship both `unix_path` and
  `win_path` entrypoints; platform-specific assertions carry explicit
  platform markers. CI runs Linux, macOS, and Windows across supported
  Python versions.

## Commits and pull requests

- Commit messages: English, imperative mood, a short subject line, and a body
  that explains behavior changes.
- One logical change per commit; tests and docs travel with the change.
- Update `CHANGELOG.md` under `[Unreleased]` (Keep a Changelog format) for
  user-visible changes.

## Design changes

Behavioral or format changes to manifests, resolution, install layout, or the
audit model start as an RFC in `docs/` (`v<target>-design.md`, numbered RFC
titles, a Status header). Implementation follows an accepted RFC. The RFC
history is indexed in [ARCHITECTURE.md](ARCHITECTURE.md#design-history).

## Documentation

- English documents are the source of truth; Russian translations live next
  to them with a `.ru.md` suffix and a header pointing at the original.
- Code blocks in a translation stay identical to the original.
- The style avoids em dashes, guillemet quotes, and rhetorical antitheses;
  state what things are.

## Releases

Maintainers release by updating `CHANGELOG.md` (move `[Unreleased]` into a
version section with compare links) and pushing an annotated `v*` tag. CI
builds the distributions, publishes to PyPI, and creates the GitHub release
with checksums and attestations. The Homebrew formula in
`ivanopcode/homebrew-csk` is bumped after the release.
