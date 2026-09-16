# Skillfile v2 sources in csk (draft skillfile-sources-v1, opt-in)

Design note for EPIC-260916-34u83e. The epic implements opt-in,
draft-labelled Skillfile schema 2 sources in cocoaskills per curator-spec
`8ba9c235ec5be00d52378479516c82386fd0c178` (skillfile-sources-v1 rev 1,
repository-transport-v1/v2). Frozen v1 wire schemas, release artifacts,
`RELEASED_SUITE_PIN` and `.github/ci/candidate-suite.json` stay byte-unchanged.

## Epic decisions

The seven csk decisions fixed for this epic, verbatim:

1. Opt-in gate: schema_version 2 is accepted only when the global config
   (`~/.cocoaskills/config.json`, `CSK_CONFIG`) declares
   `"experimental": {"skillfile_sources": true}` or the environment sets
   `CSK_EXPERIMENTAL_SKILLFILE_SOURCES=1`. Without opt-in a schema-2 Skillfile
   fails with the existing unsupported-schema error text plus exactly one hint
   line naming the opt-in. Every user-facing surface labels the feature
   `draft skillfile-sources-v1 (opt-in)`.
2. Machine policy: `source-policy.json` lives next to the global config
   (`~/.cocoaskills/source-policy.json`, override `CSK_SOURCE_POLICY`).
   Invalid or unreadable policy fails `repository_policy_invalid` before any
   network I/O and is never treated as absent.
3. Lock: `Skillfile.lock.json` next to `Skillfile.json` (skillfile-lock
   schema 1). Machine-private bindings live under the csk home, never in the
   lock.
4. Snapshot store and runtime keys: source-v1 namespace under the csk home;
   never the legacy `cache/<source>/<commit>` layout.
5. CLI mapping: `csk install` = initial resolve+install or locked install;
   `csk upgrade` = explicit refresh; `csk status` = read-only currentness; no
   new top-level commands unless protocol section 5 requires one.
6. Tests never touch the network or the real user home; use temporary homes
   (`CSK_CONFIG`, `CSK_SOURCE_POLICY`) and local bare Git repositories.
7. Harness skips are declared: `not yet implemented: <TASK-ID>` for cases owned
   by a later task, `CSK_DRAFT_SOURCES_SUITE_ROOT is not set` when the suite is
   absent, and a named platform bound for platform-specific controls.

## Module map

Planned module ownership per story. Existing modules are extended where the
surface already exists; new `csk.source_*` modules carry the source-only
surface so the v1 paths stay byte-identical.

- STORY-260916-14tjpk skillfile-v2-model-and-selection: `csk.skillspec`
  (Skillfile v2 model and opt-in parse), `csk.config` (experimental opt-in),
  deterministic skill-collection expansion, and the
  `tests/test_draft_sources_conformance.py` harness with its
  `.github/ci/draft-sources-suite.json` pin and CI lane.
- STORY-260916-vhwogf machine-repository-policy-and-transport: machine-owned
  repository endpoint policy load (`source-policy.json`, `CSK_SOURCE_POLICY`)
  and bounded authenticated transport resolution (endpoints, fallback,
  v2 ports/mirrors/aliases).
- STORY-260916-175xdq safe-immutable-local-acquisition: local source and
  output boundary enforcement plus local package snapshot capture and store
  under the source-v1 namespace.
- STORY-260916-3umxi8 package-lock-and-frozen-resolution: `Skillfile.lock.json`
  model and validation, source closure resolution, and explicit refresh.
- STORY-260916-3uifk2 marker-migration-and-atomic-installation: install-marker
  v4-to-v5 migration with full currentness, and atomic source installs and
  lock publication.
- STORY-260916-mz020a source-audit-runtime-and-build-integration: local
  runtime and command dependency materialization, source-aware build receipts
  and cache, and source audit bound to the existing assurance gates.
- STORY-260916-29xsps source-cli-and-executable-conformance: source workflow
  and actionable diagnostics on `csk install` / `csk upgrade` / `csk status`,
  and draft-source conformance corpus closure in CI.

## Suite pin

The draft conformance corpus is pinned in
`.github/ci/draft-sources-suite.json` and consumed by
`tests/test_draft_sources_conformance.py` via `CSK_DRAFT_SOURCES_SUITE_ROOT`:

- repository: `relux-works/curator-spec`
- revision: `8ba9c235ec5be00d52378479516c82386fd0c178`
- suite root: `conformance/draft-sources-v1`
- `index.json`: `sha256:c1c2e60a595107a79aaefdbd7822d95279cc792cc8cdc969ded36522663e246f`
  (115 schema cases over 8 draft schemas, both polarities each)
- `semantic-cases.json`: `sha256:552c1eed16d2d6bb37b0a5726b9420d20e4dc1d5e97e34cfa4ee6182bcb90ce0`
  (94 semantic cases, all owned via the harness mapping)
- `snapshot-cases.json`: `sha256:1922256efe21f934b667ec913f34a2af3b16004ecb00c63ee8712eacaf347999`
  (3 snapshot vectors)

The harness authenticates these three digests before reading a single suite
byte; the fast (`fast_draft_sources`, pull_request) and merge
(`merge_draft_sources`, push to main) CI lanes check out the pinned revision
into `protocol-spec-draft/` on ubuntu-latest and macos-latest and upload the
harness junit xml as evidence.

## Leaf progress: TASK-260916-2u0v5j parse/opt-in (landed in story worktree)

- `csk.sources.errors`: the nine stable `source_*` codes as constants plus
  `SourceError(code, detail)` (`ValueError`, message `"<code>: <detail>"`).
- `csk.sources.skillfile_v2`: frozen `PathSource` / `GitSource` (with derived
  canonical identity and transport) / `RepositorySource` acquisitions and
  `IndividualSelector` / `CollectionSelector`; hand-written structural
  validation mirroring `skillfile-v2.schema.json` (closed core 6.3 endpoint
  grammar, Git ref-name grammar, 40/64-hex revisions, already-canonical
  `repository` with terminal `.git` refused, literal native `path`,
  disjoint skills forms, contained directories, literal include/exclude).
  Scope `project` admits branch refs; `global` and `transitive` refuse them,
  and `transitive` additionally refuses host paths. `manifest_sha256` is
  `sha256:` over CCJ-1 of the entire parsed object.
- `csk.config`: `experimental.skillfile_sources` flag (absent key stays
  absent on save) and `skillfile_sources_enabled(config=None)`; the env var
  `CSK_EXPERIMENTAL_SKILLFILE_SOURCES=1` enables the feature on its own.
- `csk.manifest`: `parse_manifest` / `load_manifest` accept
  `allow_schema_2` (`None` consults the env var only; config-aware callers
  pass the resolved value) and `scope`; schema 2 without the opt-in keeps
  the existing unsupported-schema line plus exactly one hint line.
  `global_install` parses with `scope="global"`. Schema 1 behavior,
  precedence and error text are unchanged.
- `tests/test_skillfile_v2.py` drives all 41 `schema-cases/skillfile-v2`
  cases through `parse_manifest` (inline mirror plus an authoritative replay
  when the suite is checked out); `tests/test_manifest.py` carries a 32-doc
  v1 byte-identity corpus over opt-in on/off against the
  `tests/fixtures/v1_identity_baseline.json` fixture captured from base
  `53638fa`; the harness registers the `unknown-alias` driver at parse level.
- Revision 2: legacy validation split into `_legacy_skill_name` and
  `_finish_legacy_skill` with the duplicate check between them (v1
  precedence byte-identical); `_parse_v2` branches on `"sources" in data`
  and `parse_sources` rejects explicit null like every other non-object.
- Known bounds for later leaves: policy-alias cross-checks
  (`v2-embedded-alias-host` family), collection expansion, and config-aware
  opt-in wiring in installer/CLI entry points.
