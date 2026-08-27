# Decision 0009: first-party module roots for local `go-v1` builds

## Status

Accepted 2026-08-22. Amended 2026-08-22 after review of the original text:
the description of the first consumer was factually wrong, and point 5's
security claim overstated what its predicate delivers. The Decision section's
rules are unchanged; point 5 gains the bound that its scoping does not reach
packages presenting no replacement. Context, Rejected alternatives,
Compatibility impact, Security impact, Consequences, and a seventh open
question carry the corrections.

## Context

Section 4.2 admits exactly one module per local build root. The build root
MUST contain `go.mod` directly, that file MUST be the nearest ancestor
`go.mod` of `source_dir`, and an intervening module is invalid. Dependency
resolution is vendor-only and networkless, and `profiles/manager.md` 2.3
requires every non-standard `go list` result — its package directory, module
file, active Go file, and every active embedded input — to be a regular file
below the command's build root.

A repository whose tool module depends on sibling first-party modules cannot
take that shape. The concrete case is `skill-project-management`. On its
`main` branch `tools/board-cli` requires `pkg/board`, `pkg/remoteconfig`, and
`pkg/providerlimits` at `v0.0.0` through directory-form `replace` directives;
`tools/board-tui` requires and replaces `pkg/remoteconfig` alone; and
`pkg/providerlimits` itself requires and replaces `pkg/remoteconfig`. One
repository, several modules released in lockstep, no independent versioning:
this is the ordinary layout for a Go tool repository.

The manifest was never the obstacle. `agent-skill.json` on that branch is
`schema_version 6` with build roots `tools/board-cli` and `tools/board-tui`
and three commands, every one of them `"type": "build"` with driver `go-v1`;
no `"type": "system"` command has ever appeared in that file. What `main`
cannot do is build, for two independent reasons. Its replaced modules are
rejected outright as inconsistent vendor metadata, and it has no vendor tree
to resolve against: `.gitignore` excludes `tools/*/vendor/`, on the recorded
ground that Go auto-enables `-mod=vendor` whenever `vendor/modules.txt`
exists, so a stale vendor tree fails every build instead of falling back.

The revision consumers actually install, `ca5c4fd3`, is a different shape,
and it is the one this decision has to reckon with. There neither build root
carries a `replace` directive. `tools/board-cli/go.mod` requires
`pkg/board v0.0.0` and `pkg/remoteconfig v0.0.0` as ordinary requirements,
`vendor/modules.txt` lists both as ordinary explicit modules with no `=>`
line, and their sources sit under
`vendor/github.com/relux-works/skill-project-management/pkg/`. This is not a
packaging-time rewrite: the committed tree at that revision is already in
that shape, and the installed snapshot is a faithful copy of it. It builds
under `go-v1` today, and it does so by presenting the package's own
first-party code to the manager as indistinguishable from vendored
third-party code. What that costs is taken up under Rejected alternatives.

The mechanics matter here, because they are not what the surface suggests.
`go mod vendor` copies a locally replaced module into `vendor/`, and under
`-mod=vendor` the build reads that copy: the compiled bytes never come from
the replacement directory. For such a package the fixed
`go list -mod=vendor -deps -json` stream reports a package directory below
`<build root>/vendor`, a non-empty `Module.Version`, and a populated
`Module.Replace` whose paths point outside the build root:

```json
{"Path":"../../pkg/lib",
 "Dir":"<snapshot>/pkg/lib",
 "GoMod":"<snapshot>/pkg/lib/go.mod",
 "GoVersion":"1.23"}
```

Those `Replace` paths are derived lexically from the `go.mod` text. Go does
not stat them: under `-mod=vendor` the build still succeeds, and `go list`
still reports the same `Dir`, when the replacement directory does not exist at
all. A manager therefore cannot read `Module.Replace` as evidence about the
tree.

That is why the current total rejection — a non-standard result with a
non-nil `Module.Replace` fails as inconsistent vendor metadata — is right as
far as it goes. Accepting it as-is would mean accepting manager-visible build
metadata whose paths escape the build root, are chosen entirely by the
package, and are checked against nothing. The gap is that the rejection leaves
no way for a package to state, ahead of the build and in a form the manager
validates, which first-party module directories its build root may name.

Two further observations make a validated form possible without new commands.
A module-to-module redirect reports a distinguishable shape — `Replace.Path`
is a module path and `Replace.Version` is set, with no `Dir` or `GoMod` — so
directory-form and redirect replacements are exactly separable from the same
stream. And vendor mode already reconciles `go.mod` against
`vendor/modules.txt`: a replace directive missing from `modules.txt` fails the
fixed `go list` with `inconsistent vendoring`, before `go build`. The complete
effective replace set is thus materialized in a regular file below the build
root that `curator-build-source-v1` already hashes, and no replace directive
can hide from validation by going unused.

## Decision

Admit first-party module roots as an explicit surface that the package
declares and the manager validates. The package never steers the manager; it
states a claim that the manager checks against the tree and the build graph.

1. **Declared surface.** A local build command gains a `modules` list of
   portable relative directory paths. An absent or empty list keeps the exact
   current meaning: a single-module build root. The field arrives with a
   manifest schema bump, coordinated with the `execution_policy` bump from
   decision 0008.

2. **Bijection with replace directives.** For each active build command, the
   set of declared module directories and the set of directory-form `replace`
   directives effective for its build root MUST stand in one-to-one
   correspondence. A directive with no matching declaration rejects; a
   declaration no directive names rejects. This is the entire use the manager
   makes of the `go.mod` text: directives are checked against the
   declaration, never read as instructions.

3. **Admitted directive form.** Only directory form is admitted. The
   replacement target MUST be a relative directory path resolving to the
   declared directory, and the directive MUST carry no version on either
   side. Module-to-module redirects are rejected: they are versioned
   dependency decisions, and versioned resolution stays vendor-only.

4. **Containment.** Each declared module directory MUST be a portable
   relative path other than `.`, MUST name a real, link-free directory
   strictly inside the immutable raw skill snapshot, and MUST contain
   `go.mod` directly. Declared directories MUST be unique and pairwise
   disjoint, and MUST NOT equal, contain, or be contained by any declared
   build root or runtime root. Link-freeness already follows from snapshot
   validation in section 8.1; it is restated here because it is load-bearing.
   The manager MUST validate the declared directory against the snapshot
   itself and MUST NOT treat `Module.Replace.Dir` or `Module.Replace.GoMod`
   as evidence that any path exists.

5. **Scan surface.** Declared module directories join the directive, cgo, and
   assembly scan surface: the `SysoFiles` and cgo/C/C++/Objective-C/Fortran/
   SWIG emptiness rules, the `SFiles` rule, and the exact-bytes
   `//go:cgo_import_dynamic` scan apply to their active inputs exactly as to
   the main module. The vendored exceptions of decision 0005 do NOT extend to
   them, in the declared directory or in the vendor copy. Those exceptions
   were justified by widely audited third-party dependencies that a package
   cannot reasonably fork; first-party code in the package's own repository
   can simply not use the constructs. Accordingly the exceptions are scoped
   to results whose module carries no replacement. That scoping stops the
   newly admitted shape from laundering package-controlled assembly or
   dynamic-import directives into the build under a third-party allowance.
   It does not, and by construction cannot, reach a package that presents no
   replacement at all; open question 7 records what is left open.

6. **External dependencies unchanged.** Every non-standard result whose
   module carries no replacement remains vendor-only and versioned, resolved
   strictly below `<build root>/vendor`. Module roots add no network, no
   proxy, no module cache, and no workspace; `GOPROXY=off`, `GOWORK=off`,
   `GOFLAGS=` (empty), and `CGO_ENABLED=0` are unaffected.

7. **Cache identity unchanged.** `curator-build-source-v1` (section 8.1)
   already binds the fully validated snapshot as a whole, so declared module
   directories are already inputs to the build key. No algorithm, domain
   separator, framing, or receipt identity changes, and an edit under a
   declared module directory already changes the cache key. This is precisely
   what makes the surface safe to widen: the declared set grows, the identity
   does not.

8. **Failure boundary.** All module-root validation occurs before `go build`,
   alongside the existing preflight, with its own diagnostics named in the
   normative change.

## Rejected alternatives

- **Read `replace` directives as implicit manager input.** Accept any
  directory-form replacement inside the snapshot with no declaration.
  Rejected: it lets package-controlled `go.mod` text steer the manager,
  choosing which directories outside the build root the manager treats as
  trusted first-party source. Section 4.2 already forbids a package from
  selecting the manager's build inputs, and the compile-only boundary of
  decision 0004 rests on the manager selecting and validating its own. The
  declaration plus bijection keeps the package stating and the manager
  deciding, which is the same split decisions 0006 and 0008 apply elsewhere.
- **Require repository consolidation.** Tell such repositories to collapse
  into one module. Rejected: this is a packaging shape requirement, and it
  costs third-party adoption for no security gain. The first consumer is the
  evidence. Rather than restructure, it dropped its `replace` directives and
  vendored its own modules — the shape rejected immediately below — and its
  `main` branch has since drifted into a state `go-v1` cannot build at all.
  Demanding that a repository restructure itself to satisfy a packaging tool
  pushes users onto workarounds, not onto the audited path.
- **Drop the `replace` directives and pre-vendor the first-party modules as
  ordinary `v0.0.0` requirements.** No protocol change at all: the package
  ships a `go.mod` requiring its sibling modules by path at `v0.0.0` with no
  replacement, plus a `vendor/` tree holding their sources, so every result
  presents `Module.Replace == nil` and passes today's rules unchanged. This
  works, and the first consumer ships exactly it at `ca5c4fd3`. It is named
  here because it is the status quo this decision displaces, not a
  hypothetical. Rejected on three counts:
  - The packaged tree diverges from the source repository with nothing
    gating the drift. The shipped `go.mod` is not the one developers build
    against, `go mod tidy` in the source tree does not reproduce it, and no
    manager check ties the vendored copy back to the directory it came from
    — none exists, and none can, because the replacement that would name
    that directory has been removed.
  - First-party code enters under decision 0005's vendored exceptions, which
    were written for widely audited third-party dependencies a package
    cannot reasonably fork. Assembly and the `golang.org/x/sys`
    `cgo_import_dynamic` allowlist become available to code the package
    itself writes, and nothing in the fixed `go list` stream lets the
    manager tell the two apart.
  - It is fragile for reasons unrelated to the protocol: a committed vendor
    tree auto-enables `-mod=vendor` for every local build, which is why the
    first consumer's `main` branch now excludes it — and, in doing so, made
    itself unbuildable under `go-v1`.
  The declared-and-validated form costs one schema field and buys a manager
  that knows which directories are first-party.
- **Go workspaces (`go.work`).** Rejected: workspaces are forbidden by
  section 4.2 and disabled by `GOWORK=off`. A workspace file is another piece
  of package-controlled build configuration with nothing to validate it
  against, and it does not compose with vendor mode.
- **Extend decision 0005's vendored exceptions to replaced modules for
  symmetry.** Rejected: that is the laundering path point 5 closes. The
  exceptions are about audited third-party code, not about the `vendor/`
  directory as such.
- **Admit module-to-module redirects alongside directory form.** Rejected: a
  redirect changes which external version is used. The vendor tree already
  records resolved versions, and keeping one source for versioned resolution
  is worth more than the convenience.

## Compatibility impact

- The change is additive. A manifest with no `modules` list keeps its exact
  schema-6 and schema-7 meaning, and no shipped package changes behavior.
- Packages that use `modules` are rejected by older managers as an unknown
  field, which is the correct fail-closed outcome.
- Scoping the decision 0005 exceptions to results without a replacement is
  not a regression for any accepted package: replaced modules are rejected
  outright today, so nothing shipped can depend on the wider reading of a
  *replaced* module. Packages that vendor first-party code with no
  replacement — the first consumer among them — keep the exceptions and are
  unaffected. That continuity is the substance of open question 7, not a
  compatibility break.
- No change to the compiled-artifact cache key, build-receipt identity,
  install markers, or the fixed argument vectors.
- Consumers keep their existing vendor trees and drift gates unchanged;
  vendoring already works with replace directives in place.

## Security impact

The manager's input set does not become package-controlled. Replace
directives are validated against a declaration rather than obeyed, and the
declaration is bounded to link-free directories strictly inside the immutable
snapshot, disjoint from build and runtime roots, each with its own `go.mod`.
The snapshot is fully validated and hashed by `curator-build-source-v1`
before any Go command runs, so every declared directory is already covered by
the build identity.

No new path outside the build root becomes a compiler input: under
`-mod=vendor` the compiled bytes still come from the main module and the
vendor tree below the build root. The process graph, toolchain fingerprinting,
`manager-worker-v1` execution, offline environment, and the ban on executing
the artifact are all unchanged.

Point 5 narrows the current trust boundary rather than widening it, within
the shape it governs: pure Go assembly and the `golang.org/x/sys`
`cgo_import_dynamic` allowlist become unavailable to first-party code routed
through `go mod vendor` *from a declared module root*. The narrowing is
bounded by the predicate it uses. A package that vendors its own modules
without a `replace` directive presents `Module.Replace == nil`, declares no
`modules`, never triggers the bijection, and keeps the exceptions, so
first-party code can still reach the third-party allowance by the route it
uses today. This decision neither opens nor closes that route: it is the
pre-existing state, it is what the first consumer ships at `ca5c4fd3`, and
open question 7 puts it in front of the normative change rather than leaving
it implied.

One residual is recorded honestly. The manager does not reconcile the vendor
copy of a replaced module against its declared directory, and neither does
Go — the two may differ within one snapshot, and a build succeeds even when
the declared directory is absent. Both copies are hashed by the snapshot
identity, and the vendor copy is authoritative for the build. Whether the
manager should reconcile them is open question 1.

## Consequences

- `protocol/core.md` 4.2: the `modules` declaration, the bijection, the
  admitted directive form, containment, and the scan-surface extension.
  Section 8.1 is unchanged and gains a cross-reference.
- `profiles/manager.md` 2.3: validation order, the `Module.Replace` shape
  predicate that separates directory form from redirects, the scoping of the
  decision 0005 exceptions to unreplaced modules, and new diagnostics table
  entries.
- Schemas: manifest bump adding `modules` to the local build command object,
  coordinated with the decision 0008 bump.
- Conformance: positive and negative vectors for acceptance, escape attempts,
  module-to-module redirects, undeclared directives, unused declarations,
  nested modules, build-root and runtime-root overlap, versioned directory
  replacements, and Windows path collisions among declared directories.
- Implementations: Curator (Go) and cocoaskills (Python) implement the same
  wire contract from the shared vectors, proven by cross-implementation CI.
- First consumer: `skill-project-management` restores the vendor trees its
  `main` branch currently lacks, keeps the `replace` directives it already
  has, and declares its module roots, replacing the unreplaced pre-vendored
  shape it ships at `ca5c4fd3`. No repository restructuring is required
  either way.

## Open questions deferred to the normative change

1. Whether the manager reconciles the vendor copy of a replaced module
   against its declared directory, or records any divergence as an audit fact
   and leaves the vendor copy authoritative.
2. Which surface the normative text names authoritative for the effective
   replace set: the per-package `Module.Replace` metadata,
   `vendor/modules.txt`, or the parsed `go.mod`. Go's own vendor-consistency
   check ties all three together, so this is a question of which one
   implementations MUST read.
3. Schema mechanics: `modules` on each build command versus once per build
   root. Commands sharing a build root are already forced into identical
   lists by the bijection, so the choice is ergonomic rather than semantic.
4. Whether a declared module directory may itself contain a `vendor/` tree,
   which under `-mod=vendor` at the build root takes no part in resolution.
5. Exact diagnostic identifiers and their `blocked` or `error` classes.
6. Whether declared directories need a Windows path-collision rule of their
   own beyond the snapshot-wide rule in section 8.1, given that they are also
   compared against build and runtime roots.
7. Whether the manager can, or should, distinguish first-party code vendored
   without a `replace` directive from genuinely third-party vendored code,
   so that decision 0005's exceptions do not apply to the former. Point 5's
   scoping reaches only modules that carry a replacement, and nothing in the
   fixed `go list` stream separates the two cases once the replacement is
   gone — so closing this needs a mechanism, not a predicate. It is a design
   question rather than an editorial one, and it is deliberately left open
   here rather than answered by a decision whose subject is the declared
   form.
