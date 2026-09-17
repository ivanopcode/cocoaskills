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

## Leaf progress: TASK-260916-1iyslr repository endpoint policy

- `csk.sources.repository_policy`: frozen policy models, hand-written schema 1
  and schema 2 parsing, exact canonical repository identity, URI port parsing,
  mirror and alias admission, exact pin selection, and pure bounded fallback
  classification. Every endpoint is structurally resolved while parsing, so a
  transport caller receives no partially validated candidate.
- `csk.config`: `source_policy_path` locates the operator-owned
  `source-policy.json` beside the configured global config, with the
  `CSK_SOURCE_POLICY` override; `load_source_policy` delegates to the typed
  policy loader.
- `root_inputs` is parsed as a source-alias to non-empty, portable,
  duplicate-free and non-overlapping relative-path tuple. It remains machine
  policy and is not package-provided input.
- `tests/test_repository_policy.py` and the draft-source harness drive the
  indexed policy schema cases, all owned revision-2 structural semantic cases,
  fallback rows, loader faults, and zero-network-attempt assertions.
- Revision 2 rework keeps the canonical-key invariant at the identity boundary,
  types every fallback JSON kind before membership checks, and keeps default
  locator expansion inside the typed policy-loader boundary. Fresh narrowing
  mutants are replayed from the candidate source; all 21 are killed.

## Leaf progress: TASK-260916-2wjh3m selection (uncommitted in story worktree)

- `csk.sources.selection`: `resolve_selector_directory` (`.` selects the
  resolved source root, other portable directories descend from the opened
  root by descriptor-relative names, escape fails `source_selection_invalid`),
  `expand_collection` (immediate child directories only, include literals
  checked for existence `source_member_missing` and directory type
  `source_member_invalid` before exclusions, `*` after pruning of `.git`,
  `.agents`, `.claude`, `.codex`, `.cursor`, `.gemini` by physical/equivalent
  identity, exclusions remove and missing exclusions are harmless, empty
  expansion fails `source_member_invalid`, ascending `os.fsencode` byte
  order), `resolve_individual` (missing or file directory fails
  `source_selection_invalid`, broken SKILL.md or manifest fails
  `source_member_invalid`, SKILL.md name mismatch fails
  `source_selection_invalid`), `expand_selectors` (selector order, each
  collection byte-ordered, exact plus NFD+casefold `source_name_conflict`
  within and across selectors and against caller `reserved_names`, plus
  directly declared skill requirements naming one dependency with different
  canonical repository identities).
- SKILL.md frontmatter (`read_skill_md_name`): `---`-delimited, non-empty
  `name` (portable destination name, at most 128 characters, Unicode admitted)
  and `description` strings through a closed scalar pipeline (trailing `#`
  comments separate on any preceding whitespace outside quoted scalars;
  quoted scalars stay strings even when they spell a non-string token; empty
  values and flow/anchor/tag/block starters are refused; every other plain
  scalar is classified by REGEX ONLY against the YAML 1.2 core schema with
  no numeric conversion of any kind: null/bool/int/float matches are NOT
  strings, everything else is), `triggers` when present a non-empty list of
  non-empty strings; supported YAML subset is single-line scalars, block
  lists, flow lists, and `|`/`>` block scalars. Package rules reuse
  `skillcheck.validate_skill` with no locale; a schema-1 manifest extension
  `name` must equal the SKILL.md name where declared
  (`source_member_invalid`). No frontmatter reader exists in `csk.skillcheck`
  to reuse (it only checks `SKILL.md` presence and delegates to `skillspec`
  manifests), so the selection parser stays the single reader.
- Read boundary: the source root is opened once and every admitted member
  entry is walked, stat-ed, and read through descriptor-relative operations
  before downstream readers run. Symlinks and special files in admitted
  inputs fail `source_member_invalid`; the snapshot-backed validator cannot
  reopen a display path or reach outside bytes. `validate_member_package`,
  `read_skill_md_name` and the manifest identity check receive captured bytes
  or a snapshot-backed path.
- Pruning (`SelectionSession.managed_boundary`): the selected directory's
  opened ancestry is compared by `(st_dev, st_ino)` identity against managed
  roots and the csk home. Managed aliases, case-equivalent spellings and
  linked roots are therefore refused for explicit selections and pruned for
  wildcard discovery. `.GIT` remains authored input on a case-sensitive host;
  on a case-insensitive host it is equivalent to `.git`. Authored near-miss
  names (`agents`, `git`, `.githooks`) are never pruned.
- `tests/test_skillfile_v2_selection.py` (204 tests) drives every entry point,
  including F1 zero-external-read regressions, F2 ancestry/nested-workspace/
  csk-home pruning regressions with a platform-bound skip, and F3 closed-
  pipeline scalar negatives (incl. 5000-digit integers and tab comments)
  plus quoted positives; the harness registers `selector-escape`,
  `missing-excluded-literal`, `bad-wildcard-member`, `duplicate-name` drivers
  on filesystem fixtures.
- Bounds: snapshot/staging trees live under the csk home (`source-v1`
  namespace) and are pruned by home containment, not by extra names;
  directly declared requirement identities with different canonical
  repositories fail here, while ref-vs-ref comparison (needs repository
  access) belongs to the closure validation at publication; the frontmatter
  parser rejects YAML outside
  its documented subset; case-alias pruning tests skip with a named platform
  bound on hosts without a case-insensitive filesystem (and the distinct-
  input control skips on hosts with one). `csk.skillcheck` has no
  frontmatter reader, so the selection parser stays the single reader.
- Revision 2: fixes review F1 (metadata symlink read boundary), F2
  (physical-identity pruning), F3 (frontmatter scalar types).
- Revision 3: closes the repeated classes — F2 managed-descendant aliases
  (subtree containment anchored at base and source root, link chains,
  near-miss authored controls) and F3 YAML core-schema scalars (decimal/hex/
  octal ints, float/exponent/`.inf`/`.nan` forms, null/bool/flow, exact
  `.nan` spellings without sign). Empty frontmatter values stay refused for
  `name`/`description` via the non-empty-string gate.
- Revision 4: closes the classes for the third review cycle — F2 pruning by
  physical ANCESTRY (no anchors: the child resolves with `realpath` and
  prunes when itself or any ancestor up to the filesystem root carries a
  managed-output name; names derived from `csk.adapters`, never a second
  copy; per-ancestor `samefile` probe for actual filesystem
  case-equivalence; containment in the csk home prunes staging/snapshot/
  runtime outputs), with committed `test_nested_managed_descendant_alias_pruned`
  and a csk-home pruning test. F3 frontmatter scalars through a closed
  pipeline (whitespace-aware `#` comment separation outside quoted scalars;
  quoted scalars are strings; empty/unsupported-starter values refused;
  plain scalars classified by REGEX ONLY with no numeric conversion of any
  kind), with committed `test_nonstring_description_still_refused`
  (5000-digit integer, tab comments). Bare `#`-leading values without a
  preceding token are strings per the closed pipeline (no test covers them).
- Revision 5: one boundary predicate and a real tokenizer — F2
  `managed_output_boundary` (physical ancestry plus csk-home containment,
  names from `csk.adapters`) called on every selected package before any
  metadata read: `resolve_individual` and literal includes refuse with
  `source_output_overlap`, `*` discovery prunes silently; committed
  `test_explicit_managed_package_refused` plus explicit/wildcard tables for
  direct, nested-workspace, linked-alias, case-variant (platform bound) and
  csk-home shapes with authored controls. F3 `parse_frontmatter_scalar`
  (explicit state machine: leading space/tab skip, empty/`#`-first refuses,
  single/double-quoted with escapes and trailer rule, indicator starters
  refuse, plain ends at space/tab-`#` and classifies by REGEX ONLY with no
  `int()`/`float()` calls); committed `test_comment_only_description_refused`
  and `test_quoted_description_with_comment_accepted` plus accept/refuse
  tokenizer tables for both required fields through both entry points and a
  direct comment-only probe. Comment-only/empty values are null (refused),
  never strings.
- Revision 6: indentation-aware frontmatter block grammar (F3 fifth cycle) —
  `_parse_frontmatter` rewritten as a block parser that never strips
  indentation before deciding structure: blank/comment lines skipped, tab
  indentation refuses, top-level entries are indent-0 `key:` lines
  (`[A-Za-z0-9_.-]+`, space or end of line after the colon, duplicates
  refuse), empty values with indented children form nested blocks (sequence
  under `triggers` parses to items, anything under `name`/`description`
  refuses, anything under other keys is consumed and ignored so nested keys
  never satisfy root requirements), empty values without children are null,
  `|`/`>` headers accept chomping/indent indicators, flow collections refuse
  for `name`/`description` and are consumed-and-ignored (balanced,
  single-line) for other keys, and an indented line under a scalar-valued key
  is a structural error. The tokenizer additionally refuses `- ` sequence
  entries and plain scalars carrying `: `/`:<TAB>` or ending with `:` before
  core-schema classification; quoted `"nested: mapping"` / `"- item"` stay
  valid strings. Committed `test_non_scalar_or_nested_frontmatter_refused`
  plus structure accept/refuse tables through both entry points.
- Revision 7: column-0 fence and document framing (F3 sixth cycle) —
  `SKILL.md` is read as bytes and decoded as strict UTF-8 (invalid bytes
  refuse); a leading BOM is removed, the text splits on LF with one trailing
  CR stripped per line (CRLF valid, lone CR refuses), line 1 must be exactly
  `---` at column 0 (trailing spaces/tabs only; anything before it refuses),
  and the closing fence is the first later line exactly `---` or `...` at
  column 0. Fence detection never strips leading whitespace, so an indented
  fence-looking line reaches the block grammar (valid inside a `|`/`>` block
  scalar, a structural error elsewhere), and an indent-0 line starting with
  `---`/`...` plus trailing text refuses. Lines after the closing fence are
  the body and are ignored; zero-entry blocks refuse via the required keys.
  Committed `test_indented_fence_cannot_bypass_structure`, the three rev1
  reviewer attacks under their names, and framing accept/refuse decision
  tables (BOM, CRLF, fence trailers, `...` close, body ignored) through both
  entry points; the strip-based-closing narrowing mutant is killed by the
  named regression.
- Revision 8: YAML 1.2 section 8.1 block scalars (F3 seventh cycle) — the
  header parser (`_parse_block_header`) accepts the style indicator, at most
  one chomping and one `1`-`9` indentation indicator in either order, optional
  spaces/tabs, and an optional separated `#` comment (`|#c` refuses, matching
  the plain-scalar comment rule), and passes the triple to the content reader
  instead of discarding it. The content reader (`_consume_block_scalar` /
  `_render_block_scalar`) uses the explicit indicator or the auto-detected
  first-content-line indentation as the baseline, refuses non-empty lines
  below it (indent 0 ends the scalar) and over-indented leading empty lines,
  refuses leading-tab indentation while keeping tabs after spaces as content,
  renders literal preservation and folded joining (adjacent ordinary lines
  join with one space; `k` blanks between ordinary lines render `k` breaks,
  `k + 1` where a more-indented line is adjacent) with clip/strip/keep
  chomping, and stores the exact chomped value (entry points strip installed
  names as before). Committed `test_block_scalar_indentation_refused` and
  `test_block_scalar_header_comment_accepted`, YAML 8.1-8.13 spec-example
  accept/refuse/value tables through both entry points plus byte-exact parser
  probes, header accept/refuse tables, tab-indentation and immediate-fence
  tests; the admit-one-under-indented-line narrowing mutant is killed by the
  named regression through both entries.
- Revision 9: chomping after baseline removal plus a PyYAML differential
  oracle (F3 eighth cycle) — empty scalar content is decided only AFTER
  removing the baseline indentation, so a more-indented all-space line keeps
  its remainder as content, folds as a more-indented line, and is never part
  of trailing-empty-line chomping (`text` + four-space line under two-space
  baseline renders `text\n  \n` under clip); the over-indented-leading-empty
  error now guards auto-detection only (with an explicit indicator leading
  all-space lines are ordinary lines, matching the oracle); the tokenizer
  additionally refuses `,`/`]`/`}` starters, `?` with space/end-of-line, and
  a lone `=` tag handle (YAML `ns-plain-first`). `tests/test_frontmatter_differential.py`
  (730 documents, seed 260916, TEST-ONLY `pyyaml>=6` dev dependency; the
  runtime stays standard-library-only) asserts the full corpus through
  `_parse_frontmatter` (byte-exact values) and `read_skill_md_name`
  (oracle-driven gates) plus a 75-document sample through both public entry
  points, with an explicit 1.1-vs-1.2 type allowlist (9 entries) and a
  boundary table for intended subset strictness (duplicates, tab separation,
  below-baseline comments, anchor/tag values, single-line quoted/flow forms,
  bare `<<`). Committed `test_block_scalar_trailing_space_content_preserved`;
  the admit-one-trailing-whitespace-line mutant is killed by that regression
  and by 22 differential documents.
- Revision 10: YAML source-character gate and Unicode line breaks (F3 ninth
  cycle) — the framed region (opening through closing fence; the body is
  never read) is gated on YAML 1.2 `c-printable` source characters before
  the block grammar runs, with U+FEFF permitted only as the very first file
  character, so raw controls/DEL/C1/noncharacters refuse `source_member_invalid`
  while backslash escape spellings pass and only their decoded values may
  carry controls. NEL/LS/PS end the physical line like LF (plain/block
  mid-value breaks refuse on both sides; quoted mid-value breaks refuse as
  unterminated single-line scalars while the oracle folds NEL or preserves
  LS/PS). The differential corpus grows to 910 documents with a 173-document
  control matrix (per scalar form x per required field: Cc/Cf/Co/Cn draws,
  U+007F/U+0085/U+00A0/U+FEFF/U+FFFE/U+FFFF/U+10FFFF, extra Cc/C1 breadth,
  escaped controls) and three new boundary entries (B11 breaks, B12 mid-text
  BOM, B13 decoded controls in names); the 1.1-vs-1.2 type allowlist is
  unchanged. Committed `test_nonprintable_yaml_source_refused` (reviewer
  rev9), a body-controls-ignored test, and surrogate direct probes; the
  admit-one-U+007F narrowing mutant is killed through both entry points.
  All 36 mutants (35 rerun, 1 new) killed, zero survivors.
- Revision 11: grammar-defined character classes (F3 tenth cycle) — white
  space is U+0020/U+0009 only and line breaks are LF/CRLF at splitting, used
  at every structural decision (blank-line tests, indentation, `#`
  separation, header/trailer whitespace, flow-list emptiness, the
  required-field gate over spaces/tabs/line-breaks); every bare `strip()`/
  `isspace()`/`split()` is gone from the frontmatter path, so standalone
  Zs lines are content (both sides refuse) and NEL/LS/PS are never
  rewritten. One forced exception: a separator ending a block content line
  is that line's preserved break (NEL to LF, LS/PS verbatim), pinned by the
  committed oracle-exact regression and unreachable under any content model.
  The differential corpus grows to 1407 documents with a whitespace matrix
  (16 Zs + NEL/LS/PS + mid-text FEFF at every structural position, both
  required fields) and SB1/SB2 YAML 1.1-vs-1.2 break divergences with spec
  citations replacing B11 (no accepted-value exception remains); the type
  allowlist is unchanged. Committed `test_unicode_non_yaml_blank_line_refused`
  and `test_block_unicode_separator_value_preserved` (reviewer rev10), a
  public-entry refuse/accept table, and trigger-value direct probes; the
  blank-readmit, separator-normalize, and gate-charset narrowing mutants are
  killed by their named tests. All 39 mutants (36 rerun, 3 new) killed, zero
  survivors.
- Revision 12: LF-only block breaks and the installable-name gate (F3
  eleventh cycle) — the preserved-break exception is deleted with no
  replacement: after baseline removal block rendering knows exactly one line
  break (LF), NEL/LS/PS are content bytes of their line in every position
  including trailing, and chomping acts only on trailing empty lines and the
  final LF (clip keeps the separator AND one trailing LF, strip keeps the
  separator only). The rev10 oracle-exact regression is corrected to the
  YAML 1.2 values (same test name); diverged corpus rows become SB1/SB2
  structure-divergence rows citing YAML 1.1 section 5.4 vs YAML 1.2.2
  section 5.4, and the type allowlist is unchanged. The installable-name
  gate (`is_installable_name`) admits exactly NEL among controls (YAML
  content, filesystem-legal, identical to LS/PS); every other control
  still refuses fail-closed. Committed `test_strip_chomping_preserves_unicode_content`
  and `test_block_separator_is_content_in_all_positions` (reviewer rev11)
  plus a both-entry gate table; the reinterpret-trailing-U+2028 and
  admit-U+0080 narrowing mutants are killed by their named tests. All 40
  mutants (38 rerun, 2 new, R11b dropped with its deleted target) killed,
  zero survivors.
- Revision 13: quoted-comment separation and framed-only CR validation (F3
  twelfth cycle plus new F4) — a `#` comment after a quoted or flow token
  needs at least one separating SPACE/TAB (`_check_scalar_trailer` requires
  `cursor > pos`, matching the plain-scalar and block-header rules; PyYAML
  accepts the zero-separator shape, pinned as bound B14 strictness citing
  YAML 1.2 section 6.5), and lone-CR plus printable validation apply to the
  framed region only (`_split_frontmatter_lines` splits raw, fences locate,
  then `_check_framed_no_lone_cr` gates opening-through-closing; body CR is
  ignored). Committed `test_quoted_comment_requires_separation` (oracle pin
  corrected) and `test_body_lone_cr_is_ignored` (reviewer rev12) plus flow
  trailer, separator-table, and body-CR extensions; the differential corpus
  grows to 1440 documents (B14, T1 quoted/header tabs, strict header
  comments, body-cr frame-ok) with the type allowlist unchanged. The admit-
  double-zero-separator and first-body-line-CR narrowing mutants are killed
  by their named tests. All 42 mutants (40 rerun, 2 new) killed, zero
  survivors.
- Revision 14: EOF carriage return is not CRLF (F4 third cycle) — a trailing
  CR is dropped only before a consumed LF, so the final unterminated segment
  keeps a lone CR and `---<CR>` / `...<CR>` at EOF never matches the fence
  grammar (the frontmatter refuses as unclosed with `must close with`; body
  EOF endings still accept). Committed `test_eof_carriage_return_is_not_crlf`
  (reviewer rev13, verbatim) plus a 32-param endings-x-markers-x-regions
  matrix and self-attack rows for empty/only-opening/EOF-no-newline/blank-
  around-fences/CRLF variants; the differential corpus grows to 1448
  documents (6 frame-ok + 2 frame-no EOF rows) with the type allowlist
  unchanged and no new boundary entries. The strip-final-CR narrowing mutant
  is killed through both entries. All 43 mutants (42 rerun, 1 new) killed,
  zero survivors.
- Revision 15: complete YAML 1.2.2 section 5.7 double-quoted escape alphabet
  (F3 fifteenth cycle) — `_DOUBLE_QUOTED_ESCAPES` gains `\/` and backslash +
  literal TAB, and `_chr_from_hex_digits` refuses surrogates alongside
  above-U+10FFFF values (any other escape, truncated hex, or non-hex digit
  still refuses; the c-printable gate keeps running on source text before
  unescaping). The rev9 B6 bound was a spec misreading and is deleted:
  `\/` is a strict exact-agreement row, the literal-TAB spelling a T1
  pinned-agreement row. Committed `test_yaml_double_quote_escape_alphabet`
  (reviewer rev14, verbatim, 42 cases) plus an implementation-independent
  20-row exact-value table through both entries for both fields, name-gate
  and invalid-escape refusal tables, and B15 surrogate-bound rows; the
  differential corpus grows to 1456 documents with the type allowlist
  unchanged and the entry sample stable (105). The drop-`\/` and
  admit-`\q` narrowing mutants are killed by their named tests. All 45
  mutants (43 rerun with identical kill counts, 2 new) killed, zero
  survivors.
- Revision 16: exhaustive physical pre-read walk (F1 second cycle,
  repeat-of revision 1 / F1) — `_reject_links_in_member` is one
  `os.walk(..., followlinks=False)` plus `os.lstat` on every entry with NO
  name pruning at all (pruned names are skipped for `*` discovery, never
  for safety): any symlink anywhere below the member refuses, any
  non-regular non-directory entry (device, fifo, socket) refuses, and every
  directory/regular file `realpath` must stay inside the resolved source
  root — before SKILL.md, manifest, or `skillcheck` reads, so
  `references/.git/leak.md -> /outside` can no longer be opened by the
  downstream `rglob`. Committed `test_pruned_nested_tree_never_read_outside`
  (reviewer rev15, 6 cases) plus a 12-case link-shape table
  (references `.git`/`.agents`/`.codex`, deep-nested, member-root file,
  directory link, each asserting refusal AND zero outside reads on both
  entries) and a regular-`references/.git/notes.md` positive control on
  both entries. The skip-pruned-subtrees narrowing mutant is killed by the
  committed test through both entries. All 46 mutants (45 rerun with
  identical kill counts, F1 rebuilt for the walk, 1 new) killed, zero
  survivors.
- Revision 17: structured refusal for every filesystem shape (F1 third
  cycle, repeat-of revision 15 / F1) — symlink refusal is decided from
  `os.lstat` alone without resolving the target (resolution is
  diagnostic-only behind the never-raising `_diagnostic_resolve`), so
  cyclic links refuse `source_member_invalid` instead of leaking
  `RuntimeError`; every `Path.resolve()`, `os.path.realpath`,
  `os.path.samefile`, `os.lstat`, `is_dir`/`is_file`/`exists`/`is_symlink`,
  `iterdir` and `read_bytes` call on the selection path catches one
  `_FS_ERRORS` tuple (`OSError`, `RuntimeError`, `ValueError`,
  `UnicodeError`) and converts it to the structured `SourceError` naming
  the offending path, including the previously unguarded
  `_resolve_root().is_dir()` check. Committed
  `test_pre_read_walk_structured_refusal` (reviewer rev16, 6 cases with the
  `os.mkfifo`-unavailable platform bound) plus a 4-position cyclic-link
  table (member root, nested, `references/.git`, SKILL.md itself, both
  entries), a direct metadata-gate cyclic test, and an embedded-NUL
  structured-refusal test. The leak-readmitting narrowing mutant (walk
  resolves before refusing AND the outer handler drops exactly
  `RuntimeError`) is killed by 12 cyclic cases through both entries. All
  47 mutants (46 rerun, F1/F2 rebuilt for the audit, 1 new) killed, zero
  survivors.
- Revision 18: structured downstream boundary and fail-closed probes (F1
  fourth cycle, repeat-of revision 16 / F1; F2 second cycle, repeat-of
  revision 4 / F2) — every downstream reader (SKILL.md frontmatter,
  skill manifest loading, `skillcheck.validate_skill` with its
  skillspec/locale/markdown reads) runs inside one `_run_member_reader`
  boundary converting `_FS_ERRORS` to structured
  `SourceError(source_member_invalid, "<member-path>: <cause>")` with the
  exception chained as `__cause__`, so a `PermissionError` inside
  skillcheck's own reads can no longer leak raw; every boundary
  `None`/`continue`/`False`-on-failure fallback (csk-home `realpath`,
  child `realpath` for `*` discovery, the `os.stat`/`os.path.samefile`
  case-equivalence probe) refuses `SourceError(source_output_overlap,
  "<path>: boundary undetermined: <cause>")` instead of reporting
  "outside", with legitimate absence (`FileNotFoundError` /
  `NotADirectoryError`) still skipping. Committed
  `test_filesystem_failure_is_structured_and_closed` (reviewer rev17, 4
  cases: downstream-read denial and identity-probe denial, both entries,
  with exact-code assertions and the case-insensitive platform bound for
  the probe shape). Two narrowing mutants (downstream `PermissionError`
  re-raised; probe `PermissionError` swallowed) are each killed by
  exactly their 2 committed cases. All 51 mutants (49 rerun with
  identical kill counts except F2f 12 -> 14 from the 2 new alias-probe
  cases, F2 rebuilt for the fail-closed probe, R17 disambiguated, 2 new)
  killed, zero survivors.
- Revision 19: error-propagating physical resolution (F2 third cycle,
  repeat-of revision 17 / F2) — every `os.path.realpath` / `Path.resolve()`
  on the selection path is replaced by one `physical_path(path, *,
  code, context)` helper that resolves component by component with
  `os.lstat` / `os.readlink`, propagating every `OSError` except `ENOENT`
  as the caller-chosen `SourceError` (boundary probes refuse
  `source_output_overlap` with `boundary undetermined`, member probes
  refuse `source_member_invalid`, selector probes refuse
  `source_selection_invalid`) with the failing component in the detail and
  the cause chained; loops are refused by a `((dev, ino), remaining)`
  visited set (plus a 40-expansion cap) that allows DAG re-encounters such
  as macOS `/var` as an outer prefix and again inside an absolute link
  target; a first `ENOENT` ends resolution with the tail appended
  literally so fresh csk homes keep working. No `strict=` anywhere.
  Committed `test_home_realpath_failure_is_not_absence` (reviewer rev18,
  PermissionError at the home-alias `lstat`, both entries, with exact-code,
  `boundary undetermined` and cause assertions) plus an intermediate-member
  second-`lstat` fault table (member_invalid), a two-link selector-cycle
  visited-set test, an ELOOP-errno home variant, and a fresh-home positive
  control (10 cases). The home-`realpath`-swallowing narrowing mutant is
  killed by exactly the 4 home cases. All 52 mutants (51 rerun with
  identical kill counts except F2f 14 -> 18 from the 4 new home cases, F2d
  rebuilt for the `package_path` rename, R17c rebuilt for the helper, 1
  new) killed, zero survivors.
- Revision 20: containment by filesystem identity (F2 fourth cycle,
  repeat-of revision 18 / F2) — every lexical containment test
  (`_is_within` via `relative_to`) is replaced by one
  `is_within(child, root, *, code, context)` predicate that resolves both
  paths with `physical_path` (fail-closed, idempotent) and walks the
  physical ancestry asking the filesystem at every ancestor whether it IS
  the root (`os.lstat` identity plus an `os.path.samefile` fallback where
  inode identity is not meaningful); a nonexistent root is compared by
  identity of its deepest existing ancestor plus a case-rule existence
  probe of the lexical tail, and every probe failure refuses with the
  caller-chosen code. Applied to source-root containment, the csk-home
  rule, and (through the ancestry name match plus alias probe) adapter
  roots, `.git`, staging/snapshot trees and wildcard pruning; selector
  resolution now requires the directory before the containment check so a
  missing selector keeps its precise inspection error. Committed
  `test_home_case_equivalent_boundary_refused` (reviewer rev19, both
  entries, identical plus case-variant home spellings) plus a
  Unicode-equivalent home test, a case-variant wildcard-pruning test, an
  existing-home acceptance control, and a 90-cell root-kind x spelling x
  entry-point boundary decision table (102 cases). The lexical-only-home
  narrowing mutant is killed by exactly the 11 cross-spelling cases; two
  further narrowings pin the nonexistent-root branch (killed by the
  fresh-home controls) and the absent-child rule (killed by the single
  predicate contract test). 54 of 55 mutants killed; R19a survives with a
  stated bound (outer-layer swallowing is compensated by the predicate's
  independent fail-closed re-resolution, which raises identically).

- Revision 21: selection traversal is descriptor-based. The source root is
  opened once and every POSIX selector/discovery/read operation descends by
  bare component names with `dir_fd` and `O_NOFOLLOW`; source containment,
  ancestry, cycles and managed-output pruning use identities of opened
  descriptors, not path-string normalization. Downstream readers receive a
  read-only descriptor snapshot after one exhaustive pre-read walk rejects
  links and special files. Windows uses the documented `scandir`/reparse-point
  fallback with no atomic open-time no-follow guarantee. Added the R1-R8
  audit-hook boundary, identity, and structured-error properties and retained
  every reviewer attack test through revision 20.
- Revision 22: the descriptor design is consolidated around the seven review
  mechanisms. Session setup never lists a parent outside the source root and
  closes all temporary ancestor descriptors. `SelectionSession.managed_boundary`
  establishes managed-root identity even for symlink aliases and fails closed
  on an indeterminate probe. POSIX link targets keep native filename semantics
  (a backslash is not rewritten into a separator). The boundary property is
  installed before session setup and consumes the production descriptor seam,
  recording actual parent identities; an independent outside-descriptor read
  attack proves that the oracle fails on a real leak. Snapshot `lstat`/`stat`
  calls are synthetic and backed by captured entries, so downstream
  `skillspec` checks do not reopen the member path. The remaining direct
  installed-name conflict mutant is narrowed to NFC/NFD admission, and the
  reader mutant is narrowed to one `RuntimeError` class at one seam.
- Re-entry (selection semantics): the filesystem access layer is owned by
  TASK-260917-3q5h87 and consumed through its seam only (`SelectionSession`
  open/descend/child-directories/snapshot/managed-boundary/reverify plus the
  `read_regular_path` legacy helpers); `selection.py` reaches no filesystem
  primitive of its own (the static one-seam test proves it) and no longer
  imports `os`/`stat`. AC (f) now covers directly declared skill
  requirements: `SelectedSkill.requirements` carries each member's validated
  manifest requirements, and one requirement name with two known, different
  canonical repository identities fails `source_name_conflict` within a
  collection and across the whole set (identical declarations unify; local
  paths, malformed sources, and ref-vs-ref comparison defer to the closure
  validation at publication). The 46 boundary-mechanics tests moved intact
  to `tests/test_selection_boundary_property.py` under sibling ownership;
  this leaf keeps selection semantics, one representative fixture per
  error-code mapping branch, and the frontmatter language.
- Refresh follow-up on trunk `749dd9f`: the one-seam static test walks the
  selection import closure (`selection`, `_selection_fs`, `errors`,
  `skillfile_v2`) instead of the package directory, so the trunk-added
  `repository_policy.py` reader is out of scope (correction owned by
  TASK-260917-3q5h87, carried here). Full ordinary suite green twice
  (7115 passed, 172 skipped) with pipe capture; a sibling-oracle
  sensitivity to file-redirected stderr under xdist is reported in the
  results, not fixed here.
- Round 2 of the narrowed leaf (revision 25): the import-closure walk
  fails closed (dynamic facilities refused, bare package binds refused
  and widened, level-2 `..sources` resolved, subpackage descent,
  `__init__` seeded) with the miss shapes committed as a synthetic table
  and a shadow end-to-end; the conflict matrix is folded into
  parametrised manifest/hostile/malformed families across entries.
  Production selection is unchanged; full ordinary suite green
  (7240 passed, 172 skipped) with pipe capture.
- Round 3 of the narrowed leaf (revision 26): completeness of the
  scanned set is observed, not enumerated — a fresh subprocess imports
  the selection entry point, drives both public entry points, and
  reports `sys.modules` plus audit `exec`/`open` filenames, each of
  which must be in the static closure. The `exec` signal is what
  catches `runpy` (CPython runs it as `__main__`, never entering
  `sys.modules`). The static enumeration stays as defence in depth;
  the five statically silent families (runpy x3, getattr, star-all)
  fire via the runtime half of the shipped gate. Production selection
  is byte-identical to revision 24; full ordinary suite green
  (7250 passed, 172 skipped) with pipe capture.

## Platform bound: draft schema-2 source selection is POSIX-only

Draft skillfile-sources-v1 (opt-in) source selection requires
descriptor-relative traversal: the source root is opened with
`os.open(root, O_RDONLY | O_DIRECTORY | O_NOFOLLOW)` and every descendant
is opened by bare name with `dir_fd=`. Where the runtime does not provide
that mechanism — Windows (`os.open` cannot open a directory and
`os.supports_dir_fd` is empty), or any POSIX host without `O_DIRECTORY`
and `dir_fd` support for `os.open`/`os.stat`/`os.readlink` — schema-2
selection refuses with `source_selection_invalid` before any traversal
attempt. It never falls back to a path-based walk: a best-effort walk
would silently discard identity-bound descent, `O_NOFOLLOW` and
capability-bound reads while still looking like it worked. Released v1
behaviour is unchanged on every platform. The capability is decided from
the runtime alone (`csk.sources._selection_fs.supports_descriptor_traversal`),
never from the operating system's name.

## Leaf progress: TASK-260917-2b3ia1 POSIX-only bound (uncommitted in story worktree)

- `csk.sources._selection_fs.supports_descriptor_traversal()`: the single
  named capability predicate, decided from `os.supports_dir_fd` membership
  of `os.open`/`os.stat`/`os.readlink` (import-time identities, so
  in-process fault-injection/tracing wrappers never change the answer)
  plus `O_DIRECTORY` availability. `os.lstat` is excluded by measurement:
  on macOS with Python 3.11/3.12 it accepts `dir_fd=` at runtime while
  missing from the set, so requiring it would refuse a healthy POSIX lane.
- `SelectionSession.open` refuses first, before Phase-A preflight, with
  `SourceError(source_selection_invalid, ...)` naming the missing
  mechanisms and the POSIX-only scope; `__cause__` is always `None` (raised,
  never rescued). `source_selection_invalid` because no member is inspected
  (not member codes), no name is compared (not name conflict), no managed
  output is involved (not output overlap) and no snapshot exists yet (not
  snapshot codes); it matches the existing root-open failure code.
- Skips: one autouse fixture per traversal module
  (`tests/test_skillfile_v2_selection.py`,
  `tests/test_selection_boundary_property.py`) skips every test without the
  `posix_traversal_independent` marker (new marker registered in
  `pyproject.toml`) with the single reason
  `NO_DESCRIPTOR_TRAVERSAL_REASON`; `test_differential_entry_points` carries
  an explicit `skipif` on the same predicate; the four selection conformance
  drivers skip the same way. Four pre-existing `os.name` skipifs are removed
  as subsumed. Six pure parser probes plus two pure argument-validation checks
  (selection module) and five static closure probes (boundary module) carry
  the marker and run everywhere.
- Positive tests (`tests/test_posix_selection_bound.py`, 19 tests, never
  skip): the refusal through all four production entry points with an
  injected absent capability (including a nonexistent root and a zero-seam-call
  spy proving before-traversal refusal, and missing/file/NUL roots proving
  never-rescued shape) plus the full 32-document v1 byte-identity corpus
  parsed with the capability forced absent.
- Hosted CI on the Story landing head is what proved the missing bound:
  `Fast ordinary` on `windows-latest` gave 1645 failed / 5478 passed /
  284 skipped (1922 `[Errno 13] Permission denied` on the source root)
  against green macOS and ubuntu. Linux and Windows are proved only by the
  hosted lanes; the Windows lane must be green because of declared skips
  plus the passing positive tests, not because tests disappeared.

## Leaf progress: TASK-260916-fsw7re bounded authenticated transport

- `csk.sources.transport` consumes the policy resolver's immutable
  `ResolutionPlan`, makes at most one call per listed endpoint and at most two
  calls total, passes the remaining lane deadline to each attempt, and never
  retries an endpoint. The default attempt is the existing
  `git_admission.acquire_network` lane; no alternate acquisition path exists.
- Git admission now accepts a manager-approved `NetworkEndpoint` for revision-2
  ports, mirrors and aliases while retaining the clean Git configuration,
  exact-ref fetch, raw-object proof, and broker boundaries. Git stderr is only
  mapped when it carries explicit transport/status evidence; unknown failures
  remain fail-closed.
- `build_repository_pipeline.validate_external_build_endpoint` and the
  transport lane check refuse explicit URL ports and aliases with
  `build_repository_identity_invalid` before the attempt callback. Port-free,
  alias-free mirrors use the ordinary section 11.2 verification path. Installer
  external builds load the machine policy once, create one plan, and pass it to
  the same pipeline.
- `tests/test_sources_transport.py` covers the fallback class table, pin and
  fallback-none bounds, the two-bare-repository locked-commit path, user Git/SSH
  configuration isolation, secret-free diagnostics, fault injection at the
  trusted Git call site, and strict external-build refusal. The draft harness
  registers all 17 cases owned by this leaf and asserts exact attempt counts.
- Revision 3: a plan without a policy entry (single endpoint, authentication
  `None`) keeps the released lane policy byte-identically, including its
  pre-acquisition credential errors; every named policy endpoint resolves its
  own operator provider inside the classified, deadline-bound attempt.
  The two-attempt bound is enforced redundantly by the plan cap, the
  transport slice, and the first-failure-only continuation, so no single
  mutation admits a third attempt or a pinned fallback.
- Revision 4: the fetch classifier partitions stderr into anchored
  transport outcome records (evidence) and everything else (ignored).
  Server relay (`remote:` lines), client-side helper diagnostics, the
  advisory footer, and unknown output carry no outcome; conflict means
  two evidence-bearing records disagreeing, and no evidence at all fails
  closed to unclassified. `fatal: Authentication failed` is
  explicit-rejection evidence, SSH rejection method lists cover every
  registered method name, and the unreachable `returned error: 404`
  alternative is dropped (real 404s render as `repository not found` or
  the RPC-failed frame).
- Revision 5: the envelope splitter matches the tool that wrote the
  bytes. Git echoes an HTTP error body as `remote:` lines splitting on
  LF only, so the classifier splits on LF only and strips one trailing
  CR per line for the CRLF bodies git re-prefixes. CR, VT, FF and other
  non-LF boundaries never tear a prefixed physical line into a bare
  evidence record.
