# Skillfile v2 sources in csk (draft skillfile-sources-v1, opt-in)

Design note for EPIC-260916-34u83e. The epic implements opt-in,
Skillfile schema 2 sources in cocoaskills per curator-spec
`574636785c9da22757095ca279e8a9da801156ec` (skillfile-sources-v1 rev 1,
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

The skillfile-sources conformance corpus is pinned in
`.github/ci/draft-sources-suite.json` and consumed by
`tests/test_draft_sources_conformance.py` via `CSK_DRAFT_SOURCES_SUITE_ROOT`:

- repository: `relux-works/curator-spec`
- revision: `574636785c9da22757095ca279e8a9da801156ec`
- suite root: `conformance/skillfile-sources-v1`
- `index.json`: `sha256:654707af529bffc5104e92e98d4ab6fb910163b187861152bfd7fa769d852575`
  (121 schema cases, both polarities where applicable)
- `semantic-cases.json`: `sha256:12ae1318a25a96a21333bccb65b07fcd70b8bb1bf165bbd4b006f092806488c8`
  (105 semantic cases, all owned via the harness mapping)
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

## Leaf progress: TASK-260916-100uew local source and output boundaries

- `csk.sources.boundaries`: section-2 physical boundary enforcement.
  `freeze_boundaries(source_root, csk_home)` answers every outside
  question and freezes a `BoundaryRecord` (source/home identities, home
  containment, above-root managed ancestry, per-root bindings with
  entry identity plus link-target spelling). `check_selected_package`
  resolves selector directories to physical identity (escape fails
  `source_selection_invalid`; managed/linked/case-alias packages fail
  `source_output_overlap`; a root selection without `root_inputs`
  fails `source_output_overlap`). `prune_discovery_candidates` drops
  managed `*` candidates silently and deterministically;
  `check_declared_inputs` refuses a prune-intersected runtime/build
  input, never installing a smaller set. `validate_root_inputs`
  enforces the operator allowlist (structural rules before any
  filesystem touch; then existing, link-free, output-disjoint,
  readable; physical duplicates fail `source_path_conflict`; SKILL.md
  plus every required input must be covered). `managed_output_table`
  is the closed named set (adapter-derived first components plus
  `.git` plus csk-home containment). `recheck_publication_destination`
  is the exported per-write hook for TASK-260916-17x3o1 (binding
  reverify, link-free ancestry, within-planned containment, admitted
  overwrite refusal; every failure `source_output_overlap`).
- No containment, ancestry or pruning decision compares path strings:
  `_is_within` walks the ancestry asking the filesystem at every
  level, and case-equivalence is a same-parent same-file probe.
  Resolution is component-wise lstat/readlink with a visited set plus
  an expansion cap; loops refuse with the caller code. Every
  filesystem failure becomes a structured `SourceError` naming the
  subject. The module is portable (no POSIX gate): the publication
  recheck must run on every platform.
- `tests/test_source_boundaries.py` (181 tests) drives every entry
  point with S-FS seam-recording plus audit-hook confinement, an
  S-ERRORS fault matrix (10 sites x 5 classes with positive
  controls), and an S-POLICY zero-side-effect counter; the harness
  registers the six owned drivers (broad-root, managed-source,
  symlink-managed, case-alias with a named platform bound,
  write-boundary-retarget, root-no-inputs). All 10 narrowing mutants
  killed, zero survivors.
- Bounds: TOCTOU between validation and capture is closed by snapshot
  revalidation (spec section 3, sibling scope); rollback after a
  recheck refusal belongs to TASK-260916-17x3o1; inode-less
  filesystems use the samefile fallback; the harness registration
  self-tests now address the first still-undriven case so later
  drivers keep landing without edits.

## Leaf progress: TASK-260916-sbzutf local package snapshot capture

- `csk.sources.snapshot` (new): section-3 capture and revalidation.
  `capture_package_snapshot(source_root, directory, home)` opens a
  confined session, descends, and `capture_package` captures working-tree
  bytes (dirty, staged-shadowed and untracked; never Git HEAD, no Git
  process spawned), probes the host filesystem-equivalence relations,
  builds the inventory through `local_snapshot.build_inventory` (called,
  never reimplemented), and runs revalidation 1 before returning.
  `revalidate_capture` re-establishes identities, contents, executable
  bits and the complete admitted path set with a fresh descriptor
  capture; any mismatch fails `source_snapshot_changed` with no retry.
  `verify_frozen_copy` rehashes the frozen copy against the audited
  digest (revalidation 2). Revalidation 3 (publication-write boundary
  recheck) is named as TASK-260916-100uew's, called by TASK-260916-17x3o1.
  Storage and `source_snapshot_unavailable` moved to TASK-260917-34g2lq.
- Capture reads through `SelectionSession.capture_tree`/
  `read_captured_file` (`_selection_fs.py`): admission is decided on the
  opened descriptor (`fstat` the fd); links, FIFOs, sockets, devices,
  hard links, cross-device entries and directories-offered-as-files
  refuse naming their package-relative path. Listing-to-open skew
  (changed identity/type/link-count/device, disappearance mid-walk)
  reports `source_snapshot_changed` via `missing_code`/`changed_code`;
  stable anomalies keep the caller code. File opens are `O_NONBLOCK`
  so a file-to-FIFO swap refuses instead of hanging.
- The equivalence predicate is probed from the capture filesystem
  itself: variant spellings of captured entries open relative to their
  still-open parent descriptors and identities are compared
  (`FilesystemEquivalence` with per-axis known flags; unknown fails
  toward the exact predicate, visibly). Managed subtrees prune through
  the frozen Phase-A record only: a top-level `.git` directory prunes,
  while a `.git` file or nested `.git` captures as ordinary bytes (the
  boundary table owns the file-vs-directory question).
- `tests/test_package_snapshot.py` (169 tests): git-dirty/staged/
  untracked capture incl. real-git and no-spawn tests, special-file
  class refusals, descriptor-admission races (listing spoofs, swaps),
  vectors end to end from disk, differential predicate-vs-host tests,
  17-kind mutation and 8-kind tamper classes, audit-hook confinement
  plus read-only properties, a 4-site x 9-class fault matrix with
  positive controls, and tree-hash transaction checks. The harness
  registers the three owned drivers (local-git-dirty,
  capture-mutation, frozen-copy-mutation); `missing-snapshot` moved to
  TASK-260917-34g2lq. All 15 narrowing mutants killed, zero survivors.
- Bounds: device nodes (needs privilege), NFC/NFD and A/a pairs (need
  a distinguishing filesystem), DirEntry.stat injection (C-level;
  listing lies covered by spoofs), non-OSError walk faults structured
  by the outer boundary for wrapped callers and the walk seam for
  direct ones. Also rides here: the closure pin admits
  `csk.sources.local_snapshot` with its justification (recorded
  orchestrator decision; the instrument, not this leaf, owns it).

## Leaf progress: TASK-260917-34g2lq source-v1 snapshot store and consumers

- `csk.sources.store` (new): the section-3 snapshot store under
  `<home>/source-v1/` (`snapshots/<skill-hex>/<package-hex>/` with
  `record.json` plus `trees/<digest-hex>/`, transient
  `staging/<uuid>/`). Keys are SHA-256 hex of the UTF-8 key bytes, so
  hostile keys cannot escape or conflate on any host. Records carry
  `snapshot` digests only and the module never imports
  `csk.snapshot`, so a digest cannot land in a Git commit field.
  `stage_snapshot` verifies the frozen copy, writes a staging tree,
  re-verifies there, then renames into place plus an atomic record
  replace under `ManagerHomeLock`; pre-commit faults roll back to a
  byte-identical store. `lookup_snapshot` is lock-free, creates
  nothing, rehashes every file, rebuilds the inventory through
  `build_inventory`, and any failure to serve the exact locked bytes
  fails `source_snapshot_unavailable` -- never recreated, since
  lookup takes no source path.
- `csk.sources.consumers` (new): `open_for_audit`, `open_for_build`,
  `open_for_projection`, `open_for_install`, each a one-line
  delegation to `store.lookup_snapshot` with a consumer-prefixed
  detail. Later stories (audit/build materialization, atomic
  install) call these readers; the readers hand in-memory frozen
  bytes, never a path, so TOCTOU between verification and use is
  not expressible. Neither module is re-exported from
  `csk.sources.__init__`, keeping the selection closure pin exact.
- `tests/test_source_snapshot_store.py` (118 tests): namespace
  disjointness over adversarial keys, the no-commit-field JSON scan
  plus import AST pin, the never-recreate class test over all four
  consumers (live bytes present, store stays absent, home never
  created), a 21-shape tamper matrix each driven through all four
  consumers, S-TXN faults at every stage boundary (fresh and seeded,
  incl. heal-path restores) with strict before/after tree hashes, a
  lock-contention timeout proving the reused home lock, post-commit
  cleanup semantics, heal-with-verified-bytes, crash-residue
  inertia, a reader/writer thread run, S-ERRORS matrices with
  positive controls, and runtime host probes (case pairs,
  on-disk modes, symlinks, real-capture integration gated on
  descriptor traversal). The harness registers the owned
  `missing-snapshot` driver across all four consumers. All 6
  narrowing mutants killed, zero survivors.
- Bounds: crash consistency rests on verify-on-read plus idempotent
  re-stage (no fsync); superseded digest trees and foreign crash
  residue are inert until a later leaf's GC; on-disk permission bits
  are not part of the frozen identity; reader/writer races resolve
  to old-or-new complete snapshots, never torn bytes.

## 2026-09-17 xplat note (TASK-260917-34g2lq): hosted-lane bounds

- Revalidation identity is `(st_dev, st_ino)`: on filesystems that
  reuse inode numbers, byte-identical same-mode replacement is
  unobservable and unreported. Integrity-neutral (the digest still
  describes the frozen bytes exactly); the class test probes the
  premise at runtime instead of assuming a new inode.
- The source-v1 record replace retries a transient `PermissionError`
  (Windows sharing denial from a concurrent lock-free reader) within
  a 0.75 s bound; persistent denials and all other faults refuse as
  before with total rollback.
## Leaf progress: TASK-260916-3le0i9 source lock model and validation

- `csk.sources.package_identity` is the shared frozen vocabulary for the
  `local-snapshot`, `network-git`, and `configured-git` source-types arms.
  It serializes only portable package identity and never acquisition
  endpoints or machine paths. Later marker, receipt, and audit leaves import
  these dataclasses from this module.
- `csk.sources.lock` creates and reads schema-1 `Skillfile.lock.json` values.
  It delegates `manifest_sha256` to the existing Skillfile v2 CCJ-1 helper,
  sorts the closure by UTF-8 name bytes, records root selection indices,
  computes `lock_sha256` with only that field omitted, and validates in the
  order structural JSON, schema shape, cross-field semantics, digests, and
  current membership.
- Lock creation is strict for Git directory agreement. The whole read surface
  (`read_lock` and `parse_lock`, with an explicit `strict=True` opt-in for
  write-path validation) accepts the legacy configured-Git member-directory
  spelling used by the pinned conformance fixture and preserves it for
  byte-identical read-to-write interop; network-Git agreement remains
  enforced on read.
- Every `str`-typed model field, membership input, and manifest scalar is
  canonicalized to exact `str` via `str.__str__` (never `str()`) at the trust
  boundary, so ordering, duplicate, directory, membership, and digest gates
  decide from values no subclass override can change.
- `MachinePrivateBinding` is a separate model for absolute physical source
  location (canonical spelling is the caller's contract) and
  source-relative root inputs. It is not a lock field and has no filesystem
  or network side effects.
- `tests/test_skillfile_lock.py` commits generated digest fixtures in
  `tests/fixtures/skillfile-v2/{local,network,hand-authored}` and covers the
  shared identity vocabulary, lock validation, stale membership, ordering,
  machine binding separation, and exact round trips. The draft harness drives
  all seven lock schema cases through `read_lock_schema`; the pinned schema
  cases remain schema-validity fixtures rather than digest vectors.
## Leaf progress: TASK-260916-15nf0l install marker v5 and full currentness

- `csk.install_marker` implements marker schema 5 for schema-2 installations
  at every manifest version 1-8. `package` (the shared
  `csk.sources.package_identity` union, never inferred from a transport
  endpoint) replaces `source`/`git`/`ref_kind`/`ref`/`commit`, and
  `lock_sha256` binds the installed selection; every other member keeps its
  core section 10 meaning, requiredness and canonical set ordering.
  `attestation` and top-level `substituted` are forbidden for
  `local-snapshot` at plan, writer and reader depth. Build records bind
  receipt version 3 with every v3 cross-field rule intact
  (`InstallMarkerBuildV5` shares `_validate_receipted_build` with v3; only
  the bound versions differ). Readers keep the v1-v4 lanes byte-identical
  and unknown versions fail closed. The reader validates the shape of a
  recorded `build_source` only: the pinned corpus carries `build_source`
  with empty `builds`, so exact presence is a plan comparison.
- `MarkerPlan` is the marker-comparable projection of the effective plan
  (owned downstream by resolve-source-closure). `compare_marker_plan` is
  the one comparison, derived from the record's own members: every
  `InstallMarkerV5` member except the constant schema version and the
  install timestamp is compared (the attestation triple expands registry,
  status and key id), so a new record member extends the comparison by
  construction and the growth test fails unless the plan carries it too;
  key and attestation absence are compared values. `validate_attestation_evidence` is the one validator
  driven by the `ATTESTATION_EVIDENCE_DEFECTS` table: absent, unreadable,
  malformed, stale, revoked and the five wrong-field mismatches each refuse
  with their own code, and an unreadable record is never reported as
  absent. Freshness, revocation and signature trust arrive as explicit
  assurance-layer verdicts; marker summaries never authorize anything.
- `csk.dev_substitutions.check_source_substitution_admission` is the pure
  planning gate: new `from` selectors never gain substitution, strict audit
  rejects top-level and external substitution before cache reads, compiler
  execution or publication, and omitting the marker field cannot bypass it
  because the gate takes no marker input. `check_local_registry_requirement`
  fails local content where policy requires a network attestation.
- `evaluate_marker_status` / `evaluate_schema2_status` are the read-only
  status entries (exit 0 current, 1 otherwise); legacy and unknown markers
  never attest schema-2 currency. Legacy lanes (`status`, `gc`,
  `global_install`, `installer` currentness) meet v5 markers without
  crashing and report drift. Stated bounds for later leaves: receipt-3
  `input.build` matching and protected-artifact verification (341a6q), the
  canonical repository binding for configured-git evidence and the
  signature/trust verdicts (11yseo), and the effective-plan wiring (18j5hg).
- `tests/test_install_marker_v5.py` (233 tests) covers the migration table,
  the marker-5 x manifest-1..8 matrix, both driving tables parametrised,
  read-only tree hashes, fault injection at the read seams, and the
  zero-side-effect audit counter. The draft harness drives all 38
  install-marker-v5 schema cases through the production reader with
  expected polarity plus 23 semantic drivers (13 owned, 10 evidence
  drivers shared with 11yseo): 190 passed / 45 skipped at base,
  251 passed / 22 skipped at head.
## Leaf progress: TASK-260916-17x3o1 atomic source install publication

- `csk.sources.publish` publishes schema-2 installs through one engine
  transaction (`TransactionEngine` with a `PreWriteHook`): snapshot
  store staging, then lock (07), member contexts (10), runtime entries
  (20), adapter mirrors plus ledger (60), bindings (05) and removals
  (80) commit under per-write boundary rechecks frozen in opaque
  `publication_recheck` journal payloads, so crash replay re-verifies
  the same boundaries. The hook reuses the S3
  `recheck_publication_destination` for managed destinations, verifies
  frozen ancestor identities (dev/ino) with link-aware descent, and
  defers frozen link roots to the recheck; root and home checks use
  resolved identity like S3 so stable link roots keep working.
  Rollback restores re-run the hook before each restore, journal
  removal re-verifies every target before discarding sidecars (a moved
  ancestor would hide them as absent), and hook-owned targets skip the
  engine's resolve-based path comparison so a managed boundary move
  reads as `source_output_overlap`, never as corruption. Same-code
  commit/rollback groups fold to the single diagnostic with the
  deferred rollback recorded. Rollback rechecks the boundary before
  digesting live (pending-no-op targets return without touching live
  at all), so a move that breaks the rollback's own digests still
  reports the hook refusal; a rollback interrupted after restoring
  live resumes recognized (live at preimage, no backup left) without
  waiving ancestor verification.
- Selection is the selector index's expanded root-selection ordinal per
  member (dense zero-based across the expanded set, matching the lock
  schema); status attributes members by admission matching, never by
  assuming the ordinal indexes selectors. Locked installs and status
  derive members from the lock without expanding selectors, so a
  deleted member directory surfaces as `source_snapshot_unavailable`
  with store fallback instead of a selection failure; filesystem-side
  membership drift is ignored under a lock and joins on refresh.
- Status stages the locked desired state through the install's own
  staging and compares every live target, so marker fields alone never
  attest currency (tampered contexts, retargeted mirrors and stale
  outputs all report drifted); status takes no locks and stages only in
  the system temp directory. Repair revalidates locked bytes, never
  adopts marker claims and never rewrites the lock; refresh reruns all
  gates and replaces lock and markers atomically. Context and binding
  removals derive from live managed state minus desired members;
  runtime removals are exactly the installing project's lock diff
  (old keys minus new keys), never a sweep over the live home, so
  cleanup works with the member directory deleted on disk and never
  touches another installation; status plans no runtime removals at
  all and never reports a foreign entry as drift. The shared
  adapters planner refuses any live adapter ledger bytes csk did not
  write (BUG-260918-wvfoqa): a ledger is adopted only when its full
  bytes strictly validate as the csk ledger document, otherwise the
  install refuses naming the path and the observed shape, on the
  schema-1 and schema-2 lanes alike; every filesystem error at the
  live-digest seams maps to `source_output_overlap` instead of
  escaping raw.
- `tests/test_source_install_transactions.py` (85 tests) covers the
  end-to-end install/status cycle, a fault at every pinned stage
  boundary (selection, capture, re-enumeration, snapshot-store,
  lock-create, prepare, commit-05/07/10/20/60/80, cleanup) with
  before/after tree hashes and verified store residue, the
  between-writes retarget with deferred rollback, the unmanaged
  eight-vector family (link, file, dir, empty dir, case-variant
  marker, casing alias, changed parent, adapter occupants), read-only
  status in eight non-current cases, the five-case repair family,
  refresh gates and cleanup-from-lock, plus the revision-2 families:
  the unregistered-sibling runtime regression (lock-diff removal, no
  live-home sweep), the ledger non-regular-shape class, the
  digest-error classes at install and status seams, the managed-root
  file classes, and the three-shape rollback-side overlap class.
  Narrowing mutants for the revision-2 gates (kept-key removal,
  dir-ledger admission, single-error-class mapping, committed-only
  hook skip, shortened ancestor verification) are each killed by
  exactly their target test with sibling controls green. Stated bounds
  for later leaves: managed-root-as-link installs are skipped by the
  pre-existing gitignore gate (git refuses beyond-symlink probes)
  before publication; NFC/NFD destination collisions are
  unrepresentable (ASCII-only identifier grammar); runtime removal is
  the installing project's own lock diff and unreferenced runtime
  entries remain gc's domain; a foreign file at the ledger path
  refuses in the shared planner on both lanes (BUG-260918-wvfoqa --
  adopted only when its full bytes strictly validate as the csk
  ledger document: protocol-JSON object with exactly the
  schema_version/entries keys, integer schema_version 1, and a
  duplicate-free identifier entry list; residual: a foreign file
  that happens to form a strictly valid ledger is adopted, since
  refusing it would refuse genuine reinstalls); pending-with-backup
  crash windows keep the
  direct live check because pending hook semantics cannot recognize
  backed-up absence. The draft harness stays 251 passed / 22 skipped
  (no owned cases, no sideways movement).
- Casing-alias correction (Linux CI): the context stale sweep skips
  unmanaged non-member siblings instead of refusing the install for
  them. On a case-sensitive host a case variant is a distinct
  directory no publication target addresses; where the filesystem
  conflates the spelling, the member-destination check still refuses.
  Managed non-members are still removed through the transaction, and
  status (which shares the planner) no longer errors on foreign
  siblings. Pinned by
  `test_unmanaged_nonmember_sibling_survives_install`
  (no/foreign/garbage marker class) and narrowing mutant M-E (the
  skip narrowed to marker-absent only, so present-but-foreign
  markers fall through to removal and exactly those two params fail).

## Leaf progress: TASK-260916-nawehj local runtime and command dependencies

- Local acquisition feeds the complete existing package pipeline:
  `csk.sources.publish` stages each member's script runtime from its
  frozen snapshot into the protected store keyed by `(skill name,
  SHA-256(CCJ-1(package)))` in the `source-v1` namespace (members
  with runtime roots stage the roots, rootless members stage each
  command file into the legacy `bin/` single-file layout), local
  go-v1 commands plan through `build_planner.plan_builds` with the
  source-audit hook and compile into the immutable receipt-3 cache,
  external go-repository-v1 commands run the existing repository
  pipeline with the member package bound (receipt-3 lineage), and
  every active command gets a launcher in `.agents/bin` as a new
  `30-shim-canonical` transaction class with journal-carried
  write-time rechecks. Capabilities, dependencies, system-command
  readiness, the script execution policy, toolchain admission and
  the assurance gate all run before any write; no live links and no
  package install hooks are introduced.
- Context projection keeps its eligibility rules (`scripts/` is
  context only when no commands are exported; runtime and build
  roots excluded) and build roots never enter installed script
  runtime: the pure-spec `refuse_build_root_script` predicate
  refuses at plan time and is rechecked at the staging seam.
  Command collisions refuse through the single
  `claim_schema2_command_owner` predicate shared by the plan-time
  script gate and the planning-time owner map. External repository
  substitution stays independent of source substitution per spec
  section 10 (`installer` admits `build_repository_substitutions`
  on schema-2, still refuses `substitutions`, strict audit still
  refuses every substitution), so committed-HEAD admission governs
  external repositories while local path acquisition snapshots
  dirty bytes. `plan_builds(audit=None)` refuses any provider
  carrying a package (`source_audit_required`); the schema-2 call
  site takes the hook as a required parameter.
- `tests/test_source_runtime.py` (30 tests) drives `installer.install`
  (and `cli.main(["upgrade"])` for the refresh twin) on real local
  fixtures: the deciding edit-after-install frozen-bytes proof over
  roots/rootless, the refresh twin under a new store key via the
  production CLI, the context-eligibility matrix, the store-key and
  no-live-links proof, the committed-HEAD external build with dirty
  worktree bytes (marker commit, baked artifact bytes, receipt-3
  input package), the fault-injection/staging-boundary tree-hash
  test with positive control, the destination-became-link
  freeze-to-commit refusal with sentinel intact, and the negative
  family (collisions, build-root scripts, missing system/skill
  dependencies, pruned-path and dotdot command paths, source
  substitution, strict audit). Narrowing mutants M1-M6 (collision
  admission for exactly one name, one error class escaping the
  shim seam, one non-portable shape admitted, one contained path
  admitted, per-member key aliasing, one packaged provider admitted
  without audit) are each killed by exactly their target tests
  with sibling controls green. Cross-leaf fix on the shared
  external-admission path: `_ObjectReader.close()` now closes the
  stdout pipe on every path (pre-existing `ResourceWarning`
  leak, also emitted by the v1 external tests).

## TASK-260916-341a6q: source-aware build receipts (schema 3) and cache

- Receipt 3 is a wrapper, not a replacement: `input =
  {schema_version: 3, package, build}` where `package` is the
  source-types schema 1 identity and `build` is the closed driver
  input byte-for-byte (go-v1 schema 1 or go-repository-v1 schema 2).
  `wrap_receipt_v3_input` in `src/csk/builds/metadata.py` is the one
  wrapper construction, called from the local planner and from the
  repository pipeline; `source_aware_cache_key` is the one key
  computation (SHA-256 over CCJ-1 of the whole input, recomputed,
  never copied); `protocol_json.canonical_bytes` is the one
  serializer.
- The external driver input has a typed read model
  (`GoRepositoryBuildInput` and its source section) mirroring
  `goRepositoryBuildInputV1`; the pipeline constructor
  `receipt_input()` stays the writer of record and a byte-identity
  test pins `parse(receipt_input(...)).to_json()` CCJ-1-equal to
  `receipt_input(...)`.
- Distinct receipt-3 cache namespaces: `builds/go-v1-receipt-v3/`
  beside the untouched `builds/go-v1/` in both cache backends, and
  `artifacts-v3/` beside `artifacts/` in the external protected
  store (snapshots stay shared). A byte-identical driver input
  seeded in a legacy namespace can never satisfy a receipt-3 lookup:
  the lookup never opens the legacy namespace, and the keys also
  separate. Quarantine and collection sweep both namespaces.
- Marker-5 build records keep every `buildRecordV1WithReceiptVersion`
  / `buildRecordV2` field with only `receipt_schema_version` changed
  to 3. Top-level `build_source` is required exactly for active
  local go-v1 records and absent otherwise
  (`install_marker.check_top_level_build_source`, both directions
  tested).
- External currentness is one declared field table
  (`currentness.EXTERNAL_EVIDENCE_FIELDS`, seventeen fields) driving
  one comparison (`compare_external_build_evidence`): record versus
  receipt-3 `input.build`, receipt/artifact hashes versus protected
  bytes, `input.package` versus the marker package. Status goes
  through `evaluate_marker_status` (non-current, nonzero);
  repair re-runs the pipeline from the exact locked source and
  rebuilds rather than adopting the record.
- Strict audit rejects external substitution of a local package
  before cache reads, compiler execution, or publication: the
  planner runs an `audit` hook over the whole provider set before
  any toolchain probe or cache read, and the pipeline keeps its
  audit-before-lookup order. Both are asserted with counters (zero
  cache reads, zero compiler invocations on refusal).
- `builds/metadata.py` and `builds/planner.py` import
  `sources.package_identity` lazily (function level): a top-level
  import loads the sources package for every selection-probe
  consumer and trips the selection boundary instrument's
  runtime-vs-static completeness gate. The typed design is
  unchanged; annotations resolve under `TYPE_CHECKING`.
- Revision 2 (review round 1, F1: `csk gc` destroyed live receipt-3
  entries): cache namespaces are one declared table per family
  (`builds/cache.py` `LOCAL_BUILD_CACHE_NAMESPACES`,
  `build_repository_pipeline.py` `EXTERNAL_ARTIFACT_NAMESPACES` /
  `EXTERNAL_SNAPSHOT_NAMESPACES`). Both protected backends sweep
  and quarantine exactly the declared local namespaces; `gc` marks
  marker-5 references for both record arms through the one
  `_mark_receipted_builds` shared with markers v3/v4, and sweeps
  exactly the declared external namespaces. The invariant is that
  the sweep never walks a namespace the mark cannot populate, and
  growth tests fail when a namespace appears on one side only.
  The same revision fixed the external snapshot sweep, which
  relaxed only the top directory and could not remove the nested
  sealed `files` tree: `_remove_unreferenced_entry` now relaxes
  the proved subtree without following links.

## TASK-260916-11yseo: machine-local source audit bound to the assurance gates

- `source-audit-v1` is a binding, not a credential, and the shape makes
  self-authorization unwritable: `sources/source_audit.py` parses the
  record (`parse_source_audit`, the hand-written production reader
  mirroring the draft schema), but authorization comes only from
  `validate_source_audit` / `validate_stored_report`, which load the
  persisted complete audit report from a machine-store path DERIVED
  from the expected content hash (`csk_home/audit/source-audit-v1/`),
  never from a caller-supplied location. A missing, unreadable,
  malformed or mismatching report fails with distinct codes; missing
  and unreadable are never confused.
- Evidence is the persisted complete existing audit report: pipeline
  finding payloads (via the one serializer), pin state, the effective
  revocation list, and the effective script and assurance policy
  labels. `evidence_sha256` covers the raw stored bytes (identity is a
  function of the bytes; CCJ-1 is the one serializer). `policy_sha256`
  covers the trusted machine policy (`SourceAuditPolicy`, bridged from
  `GlobalConfig` by `policy_from_config`). At use time both digests
  are recomputed and the decision is re-derived under the CURRENT
  policy: fresh static canary, the one revocation matcher (now public
  as `pipeline.revocation_reason_for`), current pin state, current
  mode/fail_on. A block record never authorizes; a pin satisfies only
  require_pin, never canary, revocation or a required gate.
- Local inputs have no network registry identity: no code path builds
  `audit-record-v1` for local content (that module is untouched), and
  `install_marker.check_local_registry_requirement` still refuses
  where policy needs a network attestation, with before/after tree
  hashes proving no publication. Network members use
  `admit_network_git_evidence`, which derives freshness/revocation
  from a live `audit_registry.resolve` (deny-wins, signature trust)
  and then requires the existing validator's exact name, canonical
  repository, commit and context hash; evidence without a live
  attestation is refused, not adopted.
- Assurance bindings use `bind_assurance_build_input`: the exact
  receipt-3 build input digest recomputed via `source_aware_cache_key`
  over real pipeline receipt bytes. Absent or unparseable receipts
  refuse with `assurance_build_input_unavailable`, which names the
  context hash it refuses to return; no verified-provider script
  operation was added and no registry shape changed.
- Audit-before-cache/compiler for local packages runs through the
  `plan_builds(audit=...)` hook (`source_audit_plan_hook`) and is
  asserted with the 341a6q counter instrument (recording cache and
  toolchain fakes plus the shared events list, imported from that
  leaf's test module, not copied): order `audit, toolchain, cache` on
  success and exactly `["audit"]` on refusal.

## TASK-260916-11yseo revision 2: the verified side must be independent of the verifier

Revision 1 review found three instances of one shape: the thing being
verified was supplied by the thing it is verified against. Each fix
makes the independence structural.

- `admit_network_git_evidence` derives the live-resolve identity from
  the expectation alone (no separate source/commit parameters) and
  then requires the evidence to name the live registry record exactly
  (name, canonical repository, commit, key id including absence).
  The live record is registry data; the evidence/expectation pair is
  caller data; the check binds the two.
- `source_audit_plan_hook` recomputes the section-8 content hash over
  the provider's frozen tree (`hashing.content_sha256`, the same
  function the pipeline audits with) instead of reusing the package
  inventory digest. Package identity stays the provider claim checked
  against the stored binding; content identity is observed.
- `validate_stored_report` compares every persisted label the
  recomputation does not re-derive (backend, registry policy, script
  policy) with the current policy and requires the record-time canary
  outcome, so the stored path refuses the same stale-policy class as
  the record path. `record_source_audit` persists the OBSERVED static
  canary outcome instead of a constant.
- `SourceAuditPolicy` normalizes revocation digests through the shared
  normalizer (one identity, no case) and validates the mode/fail_on/
  registry_policy enums structurally, mirroring `config.py`.
- Finding location/evidence strings are checked for CCJ-1
  encodability with field-naming typed refusals; the envelope
  serialization is wrapped so no raw parser error escapes.
- Two review notes fixed leaf-locally: a dangling link at the report
  path reads as unreadable (lstat distinguishes it from absence), and
  a stored source string the revocation matcher cannot parse refuses
  as malformed store data instead of escaping the matcher's raw
  ValueError.

Stated bounds carried into revision 2: the five entry points have no
in-repo caller yet (wiring owned by TASK-260916-nawehj /
TASK-260916-18j5hg; call order `check_local_registry_requirement` ->
`source_audit_plan_hook` / `validate_source_audit` ->
`admit_network_git_evidence` -> `bind_assurance_build_input`); AC (e)
artifacts (permits/receipts/checkpoints) have no producer in csk, so
"rejects execution" is the binder raising; semantic store tamper that
preserves schema validity is undetectable on the recordless
recompute path by construction (no integrity anchor without the
record; the record path refuses it via the evidence digest).

### Revision 3 (TASK-260916-11yseo): the fourth member and the order-dependent verdict

- `_require_live_record_match` compares all five members including
  `evidence.context_sha256 == record.content_sha256`: content and context
  are one quantity (the registry is queried with the section-8 content
  hash; the lock and the conformance adapter name it the context hash).
  The separate `content_sha256` query parameter is gone; the live query
  derives from `expectation.context_sha256`, so it cannot be pointed at
  another artifact than the one the evidence must name. The positive
  fixture now has the live record attest the claimed context.
- Freshness is deny-wins over every live URL carrying the attestation's
  registry name: names are not unique in a loadable config, so consulting
  one URL made the verdict depend on config order. Evidence is fresh only
  when none of the name's URLs served stale data.
- `record_source_audit` wraps the store write (`mkdir` + `write_bytes`)
  in one error boundary raising `source_audit_store_unwritable` naming
  the path; no writer-seam `OSError` escapes raw.
- `audit.trust.load_trust_record` returns the unpinned record for any
  payload that is not an object (and for undecodable bytes), the same
  fail-closed branch unreadable and malformed trust files already take.
- Three review notes closed leaf-locally: a deprecated live attestation
  admits with a deprecation warning; revocation spellings dedupe after
  normalization so the policy digest is a function of the identity set;
  `AdmittedRegistryEvidence` carries the live `attestation` summary
  (registry, status, key id) so the marker wiring never re-resolves.

### Revision 4 (TASK-260916-11yseo): absent trust is no pin, unreadable trust refuses

- `audit.trust.load_trust_record` keeps the existence probe and the read
  under one `except OSError` boundary raising `TrustRecordError` with
  code `audit_trust_unreadable` naming the trust path. Absent still
  returns the empty record; malformed still means no pin; unreadable
  now refuses instead of silently meaning no pin (rev3 treated the
  read seam that way; the stat seam escaped raw).
- `source_audit._load_trust_record` wraps both call sites (the plan
  hook reaches it through `validate_stored_report`) and converts the
  reader error to `source_audit_trust_unreadable` naming the trust
  path and the expected content hash.
- The cross-module seam family (every filesystem seam across
  `trust.py`, `source_audit.py`, `cli.py`, `manifest.py`) is owned by
  `BUG-260918-2krem5` and was deliberately not built here.

### Revision 5 (TASK-260916-11yseo): the third caller gets a boundary

- `audit.pipeline.gate_plans` converts `TrustRecordError` to a blocking
  `GateResult` (`audit blocked: audit_trust_unreadable: ...`) in every
  mode: an unreadable pin store is never an advisory warning and never
  reads as unpinned downstream.
- `TrustRecordError` subclasses `ValueError` (the `SourceAuditError`
  convention), so `csk audit` reaches `cli.main`'s existing `ValueError`
  catch as `error: audit_trust_unreadable: ...`, exit 2. `cli.py` itself
  is untouched: no intermediate `except ValueError` sits between the
  reader and `cli.main` on either path. No other caller of another
  reader was surveyed or changed here; that remains `BUG-260918-2krem5`.

## Leaf progress: TASK-260916-18j5hg source closure and explicit refresh

- The two modes are disjoint capability types in
  `csk.sources.modes`: `ResolvingSources` carries the transport
  grant, workspace, policy path, and the alias/acquisition
  memos; `FrozenSources` carries only home and lock.
  `modes.select` is the single mode-decision point (the
  `install_schema2` inline boolean was routed through it);
  everything downstream dispatches on the type, so a frozen
  call site cannot enumerate, resolve, capture, or lock: the
  capability is not in scope.
- `closure.build_source_closure` extends the existing
  `build_closure` (traversal), `_unify` (identity/commit
  unification), `_topological_order` (cycles fail,
  providers first), and `detect_active_command_collisions`
  to the schema-2 full closure via injected `node_resolver`
  / `error_factory`; transitive requirements resolve through
  the bounded transport, must pin revisions (branches are
  root-only), and conflicting identities fail
  `source_name_conflict`. No second resolver.
- Floating refs resolve through the new `transport.resolve_ref`
  / `resolve_plan` (mirroring `acquire_plan`'s deadline and
  fallback) down to `git_admission.resolve_network_ref`, a
  bounded `ls-remote` for one exact ref sharing the acquisition
  lane's tool validation, credentials, environment, and budget.
  Byte acquisition stays exclusively on
  `transport.acquire_network`; revision pins resolve with zero
  I/O. The installer provisions one Git tool per resolved
  endpoint, mirroring the external lane.
- Lock creation is all-or-nothing across selection, snapshot,
  closure, audit, and publication (each fault leaves the
  previous lock and installed state byte-identical); stale and
  unavailable are distinct refusals that never re-resolve or
  recreate bytes; refresh swaps lock and markers atomically
  after all gates pass; the N3 surfaces
  (`check_top_level_build_source`, `compare_external_build_
  evidence`) are wired to their first production call sites.
- `tests/test_source_closure_refresh.py` (43 tests) drives
  `installer.install`, `status.collect_status`, and the
  closure/resolve seams directly where an install cannot reach
  (revision-pinned cycles are a hash fixed-point; repeated
  roots and transitive branches refuse earlier through an
  install). Narrowing mutants M1-M7 (early lock write, one
  misordered topo pair, one-alias memo bypass, one-member
  silent recreation, one admitted branch, single-lock
  re-resolve, one suffixed ref admission) are each killed by
  exactly their target tests. Closes conformance cases
  `frozen-membership`, `runtime-only-refresh`,
  `build-only-refresh` — the last three undriven cases, so
  the suite is now fully driven (277/277).
- Revision 2 (review rework): the two lanes are separate
  functions with disjoint parameter sets —
  `_install_schema2_frozen` receives home, lock and install
  data only (no fetch flag, tool provider, policy path or
  workspace), while `_install_schema2_resolving` receives
  the resolving mode; the installer decides via
  `modes.select` and passes only the mode. The frozen lane
  consults the store before any live read and never stages
  (`_capture_locked_member` lost its `heal` flag; the
  sibling `[store-heals]` case now refuses
  `source_snapshot_unavailable`); the planner stages the
  lock target in resolving mode only. `closure._unify`
  takes a `ref_comparator` hook so the schema-2 closure
  unifies on resolved commits instead of ref spellings
  (schema 1 resolves through the node repository exactly
  as before). Refusal tests assert the HEAD code
  (`_assert_head_code`), because chained causes embed
  their own codes and a substring assertion passes while
  answering the wrong class. 51 tests; mutants M1-M7
  regenerated plus M-F1 (digest-match heal admission),
  M-F2b (cross-kind admission), MR1 (frozen lock target),
  MR3/MR3b (collapse checks) — all killed, zero survivors.

## Leaf progress: TASK-260916-1lv2ky source workflow and diagnostics (uncommitted in story worktree)

- One label constant (`csk.sources.errors.DRAFT_SKILLFILE_SOURCES_LABEL`)
  printed verbatim by `--version` (opt-in), `install`/`upgrade` schema-2
  messages, `status` text and JSON, every rendered diagnostic, the
  draft-gated help paragraphs and all documents. The manifest opt-in
  hint composes the same constant with identical bytes.
- Diagnostics: `REMEDIATION_BY_CODE` in the new leaf-owned
  `csk.sources.diagnostics` module covers the thirteen stable
  classes; completeness derives from every `CODE_*` constant in
  `errors` and `repository_policy` (13 table keys + 3 explicit
  non-protocol extras), so a fourteenth class fails the test. The
  table lives outside `errors` because the sibling selection
  import-closure tests pin the exact module set reachable from the
  selection entry points, and `errors` must keep zero
  intra-package imports. `format_diagnostic` renders `code:
  reason` / `remediation: ...` / label; `sanitize_detail` (which
  stays in `errors`: stdlib-only) redacts URL userinfo (bare tokens
  included) and POSIX/drive/UNC absolute paths while preserving
  selectors, member names, relative paths and canonical identities.
  Transport exhaustion renders attempt classifications plus the
  involved listed URL; broker detail never echoes (F7 pinned at the
  display).
- Subcommand wiring reuses accepted entries only: `install`/`upgrade`
  via `installer.install` (fetch False/True) into
  `publish.install_schema2`; `status` via `status.collect_status`
  into `publish.evaluate_schema2_installation`;
  `check` = `manifest.load_manifest` (structure),
  `config.load_source_policy` (policy, only when a schema-2 project
  is present), `transport.plan_attempts` per network source (pure
  planning), `publish.read_schema2_lock` + `lock.validate_lock`.
  Rendering routes through `diagnostics.format_exception`,
  `diagnostics.format_transport_exception` and
  `installer._schema2_failure_text`;
  the shared v1 lanes keep `failure_text` untouched. Launch publishes
  no launchers for context-only installs and the shims import closure
  holds no resolution module.
- Deviation from epic decision 5 recorded: acceptance criterion (a)
  requires top-level `csk check`, so the leaf adds one conditional
  subcommand that registers only with the opt-in; without it the
  parser, help texts and the `invalid choice` error equal the
  release. `check` exits 0 valid / 1 invalid / 2 config-or-usage;
  structural failures exit 1 via install/check and 2 via status (the
  accepted leaves' split, documented in help and the operator guide).
- Byte-identity evidence: 55-command golden matrix (stdout, stderr,
  exit code) captured from clean `origin/main` at
  `23a70734a38a65ad1c236a4315a0e2603b1f90b6` under PYTHONHASHSEED
  0/1/42 and asserted after the change; schema failures precede
  policy reads, planning, traversal and sockets (audit-hook
  counters). Docs: new `docs/skillfile-sources.md` operator guide
  (English), RFC 0009 in `docs/v0.16-design.md` indexed in
  ARCHITECTURE.md, Russian updates to README, `docs/cli.md`,
  `docs/reference.md` and one Unreleased CHANGELOG entry, each
  stating no release qualification and no conformance claim.
- Revision 2 (review rework, same worktree): the sanitizer redacts
  userinfo structurally (whatever sits between `://` and the last
  `@` of the URL token, quoted spans included) plus query-string
  credential parameters; the corpus test proves non-vacuity per case
  (secret present with the sanitizer off, absent with it on). The
  second opt-in gate is gone: parse-time shape delegates to
  `config.skillfile_sources_enabled` and fails open on load errors
  (absent config only is v1), so dispatch reports the real config
  error. Status ERROR rows route through `format_diagnostic`;
  member details stay sanitized prose. `check` catches `OSError`
  from the Skillfile read (exit 1, subject named). Network
  acquisition is stated as unimplemented in the guide, the RFC
  bounds, README, `docs/cli.md` and `docs/reference.md`, and the
  `check` summary marks network sources planned, not acquired.
- Revision 3 (review rework, same worktree): the raw-declaration echo
  is gone at the source. `skillfile_v2._git_error` takes the alias,
  not the value, and refuses as `Source 'alias' field 'git'
  <shape>`; the empty-path site drops its echo too. Transport
  exhaustion renders the first endpoint from the production endpoint
  parser (scheme, host, port, path; userinfo dropped by
  construction, unparseable URLs yield no subject). The sanitizer
  stays as a second line of defence with its contract pinned
  (including the two revision-2 surviving mutants, now killed). The
  opt-in decision is one three-state function in `cli.py`
  (`_draft_sources_decision`: enabled/disabled/unknown); display
  surfaces render v1 under unknown while `check` stays parseable so
  dispatch names the unloadable config. Top `--help` under unknown
  lists the neutral `check` row (stated bound); `check --help`
  renders for the explicitly named verb. The revision-2
  `test_cli_malformed_config_keeps_draft_shape` is deleted (it
  asserted the class in the wrong direction); the secret class is a
  parametrised test over the full hostile alphabet through all five
  diagnostic surfaces plus a sanitizer-disabled structural control.
- Revision 4 (orchestrator intervention: the half-state is removed,
  same worktree): under unknown the parser is the released v1 parser.
  `_draft_check_available` is deleted; `check` registers only when the
  draft is enabled, so no `check` row, label or draft paragraph can
  render from an unloadable config on any surface, and `check`
  attempts refuse with the v1 usage error. Where a command runs,
  dispatch still refuses naming the config. The revision-3 neutral-row
  test is deleted (it pinned the withdrawn concession); unknown-state
  coverage is one parametrised test over (surface x config state) with
  the surfaces walked from the live enabled parser (46 surfaces) over
  the twelve unloadable states, each cell byte-compared against the
  absent-config rendering, plus a dispatch-side refusal test. The
  reviewer's 18-state probe re-run against origin/main `2675276`
  shows 166 of 180 cells byte-identical with the 14 diffs exactly the
  intended draft surfaces in the two opted-in states.
- Revision 5 (review rework, same worktree): the parse-time decision
  read is silent and singular. `config.load_config` takes
  `quiet=True` for the decision (`_apply_system_config` keeps the
  merge, drops the locked-key warning), so the warning stays exactly
  where v1 emits it, once at dispatch, never on help, version or
  `config show`. `main` evaluates the decision once and passes it to
  `build_parser(draft=...)`, so `--version` loads once. Coverage is a
  class over loadable non-opted-in states (present, opt-in false,
  system config with and without conflict for every member of
  `LOCKABLE_KEYS` by derivation, symlinked config): display surfaces
  byte-compared against the absent-config rendering, the decision
  asserted byte-silent in every state, runnable verbs asserted to
  warn exactly once. A 51-surface subprocess probe against
  origin/main `2675276` under the locked-conflict state is 51/51
  byte-identical (exit codes included); the same probe against a
  loud-decision copy is 0/51. Stated bounds: a FIFO config blocks at
  parse time (AC cannot classify it); the Windows
  directory/file-as-parent refusal lane differs from POSIX
  (`PermissionError` / exit 2) and is pinned per platform.

- Corpus closure (TASK-260916-2je9f6, same worktree): the semantic
  dispatch wraps every driver run in a production-entry observer (32
  tabled entries plus the four snapshot-store consumer openers); a
  driver that returns without touching any entry fails as hollow,
  naming its case and owner. A deliberately hollow driver is caught
  under all 94 case ids, and the junit artifact carries
  passed/skipped/failed/total per category (schema, snapshot,
  semantic, harness) via a `pytest_runtest_logreport` hook scoped to
  the conformance module. Outcome recording by fixture try/except
  around yield is wrong (outcomes never propagate through the yield;
  proven by a forced-skip probe) and must not come back. The recorded
  coverage statement lives in `docs/skillfile-sources.md`
  ("Conformance coverage"); windows-latest is a declared unsupported
  lane for the draft suite.
- Corpus closure revision 2 (same task): the observer table's
  membership is derived, not typed. Every entry must resolve to at
  least one syntactic call site in `src/csk`
  (`test_draft_sources_production_table_entries_have_production_callers`);
  four entries with zero callers left the table
  (`boundaries.check_selected_package`,
  `selection.resolve_selector_directory`,
  `source_audit.validate_source_audit`, `transport.acquire`) and ten
  cases were re-pointed at the live implementations
  (`selection.managed_output_boundary`,
  `selection.resolve_individual`,
  `source_audit.validate_stored_report`,
  `repository_policy.load_policy`, `transport.plan_attempts` plus
  `transport.acquire_plan`). `root-no-inputs` diverges: the live
  predicate allows the root while the corpus expects
  `source_output_overlap`, because no live path reads
  `policy.root_inputs` (finding against TASK-260916-100uew; the
  driver asserts the live class and pins the corpus side). The hook
  records `skipped` in any phase (a setup-phase skip is the canonical
  marker shape); the hollow family additionally catches an
  error-constructing probe. The observer's guarantee is exactly "at
  least one tabled in-process entry was called" with the
  in-process/table-defined/provenance-blind bounds stated in the
  harness docstring and the docs.
- Corpus closure revision 3 (same task): membership is transitive
  reachability, not one syntactic hop. Every entry must sit on a
  call-graph path from a derived production root: the console-script
  entry point (`pyproject.toml` `[project.scripts]`), the `__main__`
  delegation, and the top-level package API (`__init__` `__all__`
  resolved to its defining function); all three are parsed from
  artifacts, never typed. Per-module `__all__` lists are excluded as
  roots because `boundaries` exports the caller-less
  `check_selected_package`. Nested-function calls attribute to the
  outermost function and callback references in argument position
  (`partial` registration) are edges; cycles terminate on a visited
  set. The transitive check flagged a fifth dead entry in the wild:
  `repository_policy.canonical_endpoint_identity`, whose only
  production caller is the caller-less compat wrapper
  `canonical_repository_identity` (finding against TASK-260916-1iyslr);
  its one driver already touches live planning seams, so no re-point
  was needed. A six-shape CLASS test (dead, direct test-only, one-hop
  and two-hop forwarders, disconnected cycle, self-recursion) pins the
  reviewer's forwarder bypass plus its family.
