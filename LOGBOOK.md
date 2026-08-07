# Logbook

## 2026-08-07 — BUG-260807-29evfj a relaxation is only as safe as the scope it is written to

`70e9ca2` ported the four vendored exceptions of `curator-spec` decision 0005
into `go-v1` and got three of them right.  Point 4 it implemented in the one
form the decision names and rejects.  The decision relaxes `//go:generate` for
*vendored* `GoFiles` — "the presence of the comment in vendored `GoFiles` does
not fail preflight" — and its Alternatives section says outright: "Broad
`if false` for `SFiles`/`go:generate` in both managers: rejected, expands trust
boundary beyond the vendored, audited cases."  The port deleted the needle from
`_scan_source_directives` entirely and left a comment claiming the directive is
"not scanned for at all".  A first-party `cmd/main.go` carrying
`//go:generate sh -c poison` became a clean package.

The protocol caught it before a human did.  `attempted-go-generate` in
`conformance/v1/vectors/build-drivers.json` expects `reject` with
`go_generator_forbidden`, and CI run 31204463946 turned it red on every
Python 3.14 leg with `DID NOT RAISE`.  Worth naming the temptation that
follows: a red conformance case after a deliberate behaviour change reads like
a stale vector, and the cheap green is to bump the `curator-spec` pin.  The
vector was right and the implementation was wrong.  The pin is the thing that
made the over-broad relaxation visible at all — bumping it would have bought a
green run by deleting the only witness.

The fix (`src/csk/builds/go_v1.py:832`, `:1044`) restores the scan and gates it
on `_strictly_below(package_dir, build_root / "vendor")`, the same predicate
that already scoped the `SFiles` exception inline four lines up; it is now
computed once and shared.  That mirrors `_allows_cgo_import_dynamic`, which
scopes point 3 to `golang.org/x/sys` by import path.  All four exceptions now
hang off an explicit vendored-or-allowlisted predicate rather than off the
absence of a check, so the next reader can see the boundary instead of
inferring it from a deleted line.  `skill-project-management` — the skill the
decision was written for — still passes: `tools/board-tui` carries 13 vendored
`GoFiles` with `//go:generate` and preflight accepts every one.

The general shape: when a spec grants an exception, port the *scope* first and
the *relaxation* second.  Dropping the check is never the same change as
narrowing it, and the diff that removes a rule looks smaller than the one that
qualifies it precisely when it is larger.

## 2026-08-07 — BUG-260807-l2ymv3 a deadline is only as real as the clock that reads it

`time.monotonic()` is not the same clock on every platform.  Windows CPython
before 3.13 backs it with `GetTickCount64()`, whose tick is 15.625 ms; CPython
3.13 moved it to `QueryPerformanceCounter()`.  Two readings inside one tick
compare equal, so `_check_deadline` evaluated `t > t + 0.000001` as false and
the go-v1 fingerprint deadline was never observed.  A fingerprint pass over a
small GOROOT finishes well inside one tick, so an already-exhausted deadline
read as unreached and admitted work it exists to refuse.  Measured on the host:
Python 3.12.10 reports `monotonic` resolution `0.015625` and `perf_counter`
resolution `1e-07`; Python 3.14.4 reports `1e-07` for both.  That split is
exactly why CI failed on windows 3.11 and 3.12 and passed on 3.13 and 3.14.

The module now routes every deadline — `_deadline`, `_check_deadline`, and the
`SubprocessProbeRunner` timeout loop — through one `_elapsed()` reading
`time.perf_counter()`, which is `QueryPerformanceCounter()` on every supported
Windows CPython and `CLOCK_MONOTONIC` elsewhere.  The consequence is wider than
the three red tests: on those interpreters any `CSK_GO_FINGERPRINT_TIMEOUT`
below 15.625 ms was silently unenforceable and every deadline was quantised to
the tick.  A deadline is a refusal boundary; a coarse clock must never round it
into an admission.  Anywhere else in this codebase that measures a short
duration should read `perf_counter`, not `monotonic`.

The same red run carried a second, unrelated Windows failure worth naming: a
`global install` fixture exported a script command with `unix_path` only and
asserted exit 0 on any host.  `docs/mvp-design.md` states that a missing
platform path fails installation, and the shim writer enforces it, so the test
was wrong and the product was right.  Both defects reached `main` because their
branches were delivered as branch pushes without pull requests to save CI
budget — neither had ever run on a non-macOS leg.

## 2026-08-07 — BUG-260807-1it17m external artifacts take the name their receipt declares

The protected external-build store wrote every artifact to a fixed literal
`artifact`, while `_artifact_path` already declared `bin/<command>.exe` in the
receipt for a `windows` target.  The generated `.cmd` launcher calls the stored
path, and Windows will not execute a file with no executable extension, so a
native Windows `go-repository-v1` install produced a launcher that could not
run — the build, snapshot, cache key, and argument forwarding were all correct.

Four resolvers derived that literal independently: the store's read side, the
installer's published path, `status`, and the shim's manager-derived path
check.  A new `builds.metadata.derived_cache_artifact_name` takes the receipt's
artifact path and returns `artifact` or `artifact.exe`; all four now go through
it, so the store name and the receipt cannot drift apart.  macOS and Linux keep
`artifact`.  An existing Windows entry is quarantined and rebuilt on the next
`install` or `repair`, which is harmless because no Windows entry could ever
have produced a runnable launcher.

Sealing no longer recognizes the artifact by name.  `_seal_tree` preserves the
execute bit the staged file already carries on POSIX and takes the artifact's
name from the caller on Windows.  That also repairs a latent defect: an
executable file inside a snapshot was demoted to `0o400` by sealing and then
failed `load_snapshot`'s `executable` metadata comparison.

Verified on the native Windows host (Windows 10 19045.6466, Go 1.25.5
windows/amd64, Python 3.14.4) with a real Go build and no stubbed compiler:
`install` exit 0, the entry holds `artifact.exe` (a 2,308,608-byte `MZ` image),
the `.cmd` names it, `--help` exits 0, and `status` is clean.  The same harness
on macOS arm64 keeps `artifact` and still launches.

## 2026-08-04 — BUG-260803-2sqyqy current-status observer gains causal provenance

Hosted Windows Python 3.12 at signed head `a361899d` returned the correct
`compiled-installation-current` product verdict and all ten validation
witnesses, but the conformance adapter emitted `mutations=[filesystem]` from
its legacy unclassified `before != after` comparison.  Unlike the independently
isolated negative-currentness cases, that current-state observation exposed no
snapshot or causal diagnostic, so host metadata drift and a CocoaSkills write
were indistinguishable.

The current-state observer now watches the exact project, manager-home, and
skills roots, records every protected-root mutation call with sanitized causal
provenance, and derives the filesystem mutation verdict from the same classified
snapshot model as the negative cases.  Any causal write or protected-state
drift remains fail-closed.  Directory-only Windows `.git` file-attribute drift
and observer-owned native ChangeTime remain preserved in diagnostics without
being mislabeled as product writes.  Focused tests pin both the host-only and
causal-write signals and require the compiled-current observation to publish
read-only provenance.

## 2026-08-01 — BUG-260801-1iu1ln observed rc.6 lifecycle bindings

The cycle-2 scalar audit exposed 104 lifecycle mutations that survived the
declarative adapter. The replacement binding reconstructs all 32 lifecycle
cases from CocoaSkills cache, transaction, locking, installer/planner,
currentness/status, recovery/repair, launcher, GC, bootstrap, and upgrade
seams, then compares the complete observed objects with the authenticated
candidate vector. The 378-leaf mutation test proves expectation-side equality
sensitivity; it does not by itself prove product-seam sensitivity. A fail-closed
field classification plus the initial seven independent sabotage tests make
omitted skill validation, transient private-build home locking, omitted repair
audit, omitted generation-current enforcement, private-artifact execution,
guardless GC, and first-journal-only recovery change the observed result.

The cycle-4 review found three remaining lossy projections adjacent to those
seams. Process observation now scans every argv element through both `run` and
`Popen`, so a protected GC artifact invoked through an interpreter is visible.
GC adoption evidence compares the complete rejected-entry tree and records
in-place permission repair attempts even when the original mode is restored.
Recovery binds each transaction ID to its exact canonical project identity,
derives the cache key from restored state, and labels the triggering project
only from the exact lock identity. Filesystem basenames and directory-name sets
are no longer used as lifecycle answers. The private-build persistent generation
is read from state rather than repeated as a literal, and transaction lock/order
labels are projected only after exact identity/order checks. The first ten
product-seam sabotage probes plus fail-closed literal/proxy classification now
cover these properties while the 378-leaf expectation mutation audit remains
exhaustive.

Cycle-5 review exposed seven more causal gaps. Atomic publication observes the
exact live cache destination around the platform no-replace primitive. The
cross-project case drives two private-build calls concurrently and compares
their shared `BuildInput` and derived cache key before the publish handoff.
GC tests the consumer registry as an independent mark root rather than letting
a configured-project marker mask it. Recovery verifies every primary target's
exact class, identifier, live path, backup path, retained backup digest, and
preimage digest before recovery begins.

Repair observes protected candidate execution at process boundaries and rejects
candidate adoption, in-place permission changes, and self-consistent receipt
trust unless a fresh private build follows the full gate/publish/commit
pipeline. Both clean status and every currentness-matrix row record transient
persistent mutation attempts even when bytes and modes are restored before the
call returns. Rollback compares the exact bytes and mode of every live target
with its pre-commit tree state after reverse-order restoration. Seven exact
reviewer sabotages pin those properties, bringing the independent sabotage
total to seventeen without weakening the exhaustive 378-leaf audit.

Cycle-6 review found that those claims still exceeded the observed boundary.
The cross-project case now drives two real installs all the way through
successful cache publication and materialization commit, reads both consumer
records and the shared protected cache entry from that same run, and tolerates
the normal optimistic-generation retry without replacing it with a synthetic
consumer transaction. Distinct first cache-key builds prove private work can
overlap while the actual publish and commit calls prove manager-home
serialization and exact project order.

Normative cache keys and receipt hashes are no longer returned from the
authenticated fixture as scenario answers. Publication, cross-project success
and rollback, dry-run, GC, private build, status, repair, and deterministic
transaction order derive them from their own plans, markers, protected-cache
inspections, publications, or repaired state and compare the resulting input
to the normative identity. One operation-side sabotage changes those seams
while leaving the fixture untouched and makes every corresponding complete
case differ.

Process observation resolves every path-like argv element against the
effective subprocess `cwd`, including inherited cwd. Persistent-state
observation covers the high-level `Path` mutations and descriptor-relative
low-level open, write, unlink/remove, mkdir/rmdir, rename/replace, link,
symlink, truncate, timestamp, and permission families. Atomic-publication
evidence independently observes all supported namespace operations targeting
the exact live cache destination, including alternate `os.rename` moves on
POSIX and Windows. The five cycle-6 regressions preserve the exact surviving
handoff, identity, cwd-relative execution, transient low-level mutation, and
alternate live-destination probes in addition to the earlier seventeen.

Binding the vector revealed three product mismatches. Project and global
install ran recovery before private builds despite the publication-phase-only
ordering; those pre-build passes are removed while the existing locked recovery
inside each materialization attempt remains. Status now treats a declared build
root copied into commit-keyed runtime state as non-current. Global upgrade now
passes fetch intent into closure construction, so transitive repositories are
updated as well as direct declarations. Focused regressions cover each change.

This repair changes no protocol pin, schema, tag, release, claim, or CI
curator-spec reference.

## 2026-08-01 — TASK-260720-akf5kh schema-6 documentation boundary

The documentation treats three distinctions as non-negotiable. Go 1.23 is the
protocol floor, while the current csk operator-trusted allowlist contains only
family 1.25. Logical build identity and canonical receipt/artifact data are
portable, while the manager-home cache layout and native protection backend
are csk-specific. Finally, a self-consistent receipt proves consistency but
does not establish protected-state provenance; ownership, permission/DACL,
containment, file type, and link safety still have to be independently proven.

The source-aware platform statement is deliberately narrower than the package
platform statement. `go-v1` is available on macOS and Windows and fails closed
elsewhere; Linux source-aware work is explicitly owned by
`TASK-260728-1skseh` and `TASK-260728-1e6811`. Generic script/system behavior
is not reclassified by that limit.

Portable `manager-worker-v1` mechanisms are documented without promoting them
to the six deferred hardened guarantees. In particular, offline Go settings
are not kernel network denial, identity rechecks are not read-only mounts,
manager-selected write targets are not descendant write confinement, and the
manager's numeric bounds are not hard aggregate descendant limits. No new
release, tag, policy pin, signature, review, or interoperability claim is made
for the documentation handoff.

## 2026-08-01 — TASK-260720-g7kgox atomic global builds

Global install now follows the same planning and publication boundary as a
project install. A global-scoped project lock spans closure resolution, trust
gates, build planning, and private compilation; per-key build locks serialize
cache misses; and the manager-home lock is acquired only for recovery,
generation and target-preimage revalidation, protected cache publication, and
the durable materialization commit. Global upgrade retains its update lock for
fetching, releases it, and then enters this install flow, so compilation never
runs beneath the manager-home lock.

The materialization transaction covers every global install surface: closure
contexts and marker-only nodes, script runtimes, compiled and script launchers,
PATH-visible user-bin forwarders and their ledger, shell environment files,
agent adapters and their ledgers, and stale contexts, runtimes, launchers, and
adapter entries. All desired bytes are prepared in an operation-private home;
environment files and launchers embed the final manager-home paths rather than
their staging paths. POSIX user-bin links are staged with a destination
relative to the eventual live user-bin directory, which keeps the link valid
after the transaction moves it out of staging. Native global adapter planning
also deduplicates the shared `~/.agents/skills` root used by Windsurf and
OpenCode.

Real global installs now consume the schema-6 closure and shared build planner,
compile cache misses privately, publish verified artifacts under the home lock,
write marker v2 build receipts, and activate build shims from the protected
cache. Context-only transitive nodes are materialized according to their
activation edges, while inactive commands remain absent. The old per-skill
partial-write loop is gone: source and dependency diagnostics are still
collected per declaration, but any error stops before builds or
materialization and preserves the previous global install.

Dry-run keeps the read-only two-attempt generation protocol and constructs no
project, build, or manager-home lock. Its generation includes global shims and
environment files plus hybrid, configured-project, and consumer marker roots,
so a concurrent reference change restarts planning before runtime pruning can
act on a stale view.

Regression vectors cover provider-first build ordering, home-lock exclusion,
marker v2 and build-root filtering, canonical and user-bin activation,
build/publication preservation, every ordered global transaction target class,
dry-run byte purity, Windows user-bin staging, native-adapter root
deduplication, upgrade lock/argument/exit behavior, and the existing script and
system-only flows.

## 2026-07-31 — BUG-260731-1rldqv Windows transactional install

Every `windows-latest` cell of PR 16 failed while every POSIX cell stayed
green. Four distinct Windows facts, all of them platform behaviour rather than
transaction-engine logic:

`os.stat` synthesizes the execute bits from the file name extension for
`.bat`, `.cmd`, `.com` and `.exe`, and only from a path — `os.fstat` has no
path and cannot. Every command shim therefore reported `0o777` through `lstat`
and `0o666` through the handle used to read its bytes, and the digest guard
read that as the target changing mid-digest. The digest payload had the same
defect in latent form: it hashed the synthesized mode, so identical bytes
digested differently under a staging sidecar name and under the live `.cmd`
name. Digests and guards now use the permission identity that Windows can
actually hold, which is a no-op on POSIX.

Windows records a symlink's type on the reparse point, and a file link that
lands on a directory cannot be traversed at all. A staged adapter link is
dangling by construction — its destination is relative to the live location —
so deriving the type by resolving the destination produced a file link. Type
now comes from the link's own `FILE_ATTRIBUTE_DIRECTORY`, which is stable while
the destination is missing, so it is also compared during staging validation.

New Windows objects belong to the token's *owner*, which is the Administrators
group for an elevated administrator, and inherit the containing DACL. The
manager therefore never owned the home it created nor the artifact its own
compiler produced, and the protected build cache correctly rejected both. POSIX
hands the manager that state for free. The manager now establishes it: it
provisions a manager home at creation, and makes a freshly compiled artifact
private before offering it for publication. Neither guard was relaxed, and an
established home is never re-provisioned, so real ownership drift still fails
closed. A Windows home created by an elevated shell before this change stays
Administrators-owned and will fail closed; adopting such a home would blunt the
drift guard, so that repair is left as an explicit product decision.

None of this was reachable on `main`, whose installer only plans builds. The
regression entered with the commit that made `installer.install` compile and
publish.

### Namespace validation cost

Fixing the four correctness faults left every Windows cell passing and far too
slow: Python 3.11 took 2h18m43s against 14 minutes for the same suite on
`main`, individual install tests ran 76-247 seconds each, and Python 3.12 was
still running after three hours. Two independent `windows-latest` fault-handler
dumps landed on the same frame, so this was one hot path rather than a hang.

`_validate_namespace_independence` compares every declared namespace with every
other one, and each comparison canonicalised *both* of its operands. The pass
therefore performed filesystem work proportional to the square of the namespace
count, and it runs on every journal save — including twice per 32 KiB staging
chunk. One ordinary install measured 750,620 canonicalisations, 182 of its 210
seconds, on macOS, where `realpath` is cheap. Windows opens a handle per path
component to answer the same question, which is why only that platform became
unusable while POSIX merely looked slow.

A namespace is now a probe that resolves its path, and reads its physical
identity, at most once per pass; every comparison in the pass reads that one
answer. The same install now performs 12,575 canonicalisations and takes 27.9
seconds. No guard changed: parts comparison, prefix containment, and the
`samestat` alias check all still run for every pair, and a real `OSError` is
still never cached. Asking the filesystem one question once per pass is also
more internally consistent than asking it once per comparison, which is what
the pairwise form did.

Cost is part of this contract, so it is pinned by tests: one canonicalisation
per namespace plus the manager home, and growth that tracks the namespace count
rather than its square.

Resolving once per namespace left the pairwise scan itself as the remaining
square term — 364,635 comparisons for one install — so the pass now asks its
question of an index instead of of every pair. Two namespaces overlap when one
names the other, when one contains the other, or when two spellings reach one
physical object. Naming is a dictionary keyed by the normalized parts;
containment is a lookup of each namespace's proper prefixes, and path depth is
bounded; physical aliasing is a dictionary keyed by `st_dev` with `st_ino`,
which is exactly what `samestat` compares. Same predicate, same rejections,
same message shape, and the reported pair is still a genuinely colliding pair.
The install test that took 210 seconds now takes 5.5.

### Review rework: a swallowed `FileExistsError` is not `exist_ok=True`

Independent review found one defect the Windows work introduced. Provisioning
a manager home has to know whether *this* call created it, because only a home
this call creates may be stamped private; an established home must fail closed
on ownership drift instead. `mkdir(exist_ok=True)` cannot answer that question,
so it was rewritten as a plain `mkdir` with `except FileExistsError: return`.
That is not what `exist_ok=True` does. CPython's implementation is `if not
exist_ok or not self.is_dir(): raise` — it tolerates an existing *directory*
and re-raises for anything else. Dropping the `is_dir()` condition made a
regular file, or a symlink to a missing directory, an acceptable manager home:
the create mode and the whole Windows ownership stamp were skipped, and the
home materialised later at whatever the name pointed to.

Latent rather than live — every current call site loads configuration first,
which rejects a non-directory home before locking is reached — but it sat
inside the function added to establish the home's private state, and no test
covered it. The condition is restated explicitly, and five tests now cover all
five shapes the path can be in, with the two adopted rows as positive controls.

The other rework is a test. Memoising the probes and then indexing them were
two commits, and only the first had its contract pinned: restoring a pairwise
scan over memoised probes leaves both canonicalisation tests green while
reinstating the 364,635 comparisons that were the dominant Windows term once
the filesystem work was gone. Cost per namespace is now pinned directly, by
counting every read of the state a namespace is compared by. Three equally
spaced sizes grow by equal steps — 156, 296, 436 units of work for 32, 60 and
88 namespaces — where the pairwise form grows by widening ones (3,500 → 12,446
→ 26,880). Verified red against a restored pairwise scan, which fails this
test alone.

## 2026-07-30 — TASK-260720-3t8nr3 atomic project and hybrid materialization

Project and hybrid installs now use the lock order required by the build and
transaction protocols: a project lock spans planning and private compilation,
per-key build locks serialize cache misses, and the manager-home lock is held
only for recovery, generation/preimage revalidation, verified cache
publication, and the durable commit. The CLI therefore no longer wraps
project installs in its legacy outer manager-home lock; `update` and global
operations retain their existing lock ownership.

Adapter mirrors cannot be represented as aggregate transaction byte trees
because those trees deliberately reject symlink descendants. The integration
uses the transaction protocol's entry targets instead: every managed adapter
mirror is an independent entry target, its ledger is a later bytes target,
and missing parent directories are created and unwound around the protected
commit. This keeps symlink and copy modes transactional without weakening
tree validation.

Audit verdict and registry gates may legitimately update their own cache and
rollback-state paths before build planning. Generation baselines are rebased
only across those gate-owned paths; changes to manifests, configuration, MCP
inputs, build cache, or materialization targets still force a complete plan
restart. Dead legacy install temporaries are now explicit stale-removal
targets rather than an unjournaled post-install GC mutation.

### Review rework: concurrency liveness, GC, and portability

Main CI run `30556125542` exposed a Windows Python 3.14 timing flake in
`test_concurrent_project_transactions_preserve_both_consumers`: both workers
reported no exception, but one outlived the test's fixed five-second join.
The identical SHA passing a rerun does not prove the vector robust. The test
now uses explicit events to place both project locks before the manager-home
handoff, deterministically makes project A acquire the home lock before
project B attempts it, and retains the terminal thread-liveness assertion.
Its bounded coordination budget is ten seconds on POSIX and thirty seconds on
Windows (the production lock default), with a two-lock completion margin.
This tests the intended serialization instead of depending on a five-second
machine-speed assumption.

Install-time GC remains post-commit maintenance. It now runs only after a
successful real-install batch, never after dry-run, build, publication, or
target failure, and holds the manager-home lock while pruning, so failure
atomicity and consumer-ledger serialization are preserved while stale snapshot
cache entries and the remaining global/runtime install orphans are collected
again. If that post-commit maintenance lock is contended, the already committed
install remains successful and reports that garbage collection was skipped;
lock-order violations still fail loudly. Initial journal corruption is also
converted into the same per-project failed result as recovery errors discovered
inside planning, avoiding an uncaught CLI traceback.

The new transaction-vector module no longer has a module-wide non-POSIX skip.
Only the five vectors that execute POSIX protected-cache artifacts retain a
targeted skip; generation restart and consumer-last rollback isolation are
platform-independent and run on Windows. The rollback vector now uses a
context-only skill so it does not smuggle a POSIX shell dependency into that
claim.

Auto-mode adapter capability probing no longer creates a transient file in the
live project or depends on the process `TMPDIR`. Materialization staging is a
hidden operation-private sibling of the physical project, outside the checkout
but on its destination filesystem. If that parent cannot host staging, the
installer falls back to the manager home and conservatively chooses copies when
the fallback is on another filesystem. The probe accepts only a same-device
witness; explicit symlink mode is unchanged. This makes adapter output stable
across shells whose system temporary directories live on different devices.

The operation-private staging implementation still copies the complete live
project `.agents`, shared hybrid, and runtime trees even for a no-op install.
This is a known O(total installed state) performance tradeoff, not a
correctness shortcut: retaining complete isolated preimages keeps transaction
target derivation deterministic. The copy now lands beside the physical project
(or, only when that cannot be created, under the manager home), not in a
possibly RAM-backed system temporary directory. It should be replaced by
target-scoped copy-on-write staging if install scale makes the cost material;
this task does not add a second state model solely to optimize that path.

Consumer-ledger encoding now resolves each project path before sorting and
serializing it. That canonicalization is intentional: transaction digests and
preimage checks must not vary merely because the same checkout was reached
through a symlink. The first successful install after this change can therefore
rewrite a legacy unresolved ledger entry to its physical path.

## 2026-07-30 — TASK-260720-2x6mjn planning boundary

Independent review found that sharing the compiled-build closure with the
existing mutating global-install loop materialized undeclared context-only
providers, published their inactive commands, and allowed same-name inactive
commands to shadow one another. Running the toolchain/cache planner on real
project and global installs also made those established install paths require
Go before any downstream code consumed the resulting plan.

The implementation now keeps validated closure, source, toolchain, and cache
planning on the read-only dry-run path. Real global materialization remains
declaration-driven, and real project/global installs do not invoke compiled
build planning. TASK-260720-3t8nr3 owns connecting these plans to compilation
and materialization.

Regression coverage asserts that context-only transitive providers never
appear in `global/skills`, `global/bin`, or runtime storage during a real
global install, and that real project/global installs succeed without Go on
`PATH`.

Dry-run build planning intentionally fails closed when the captured operator
`PATH` has no trusted Go executable. The failure aborts the whole project or
global build plan, returns no partial build rows, and preserves every watched
filesystem surface. Both scopes now pin that behavior with host-independent
tests, and the unrelated schema-v6 build-root lifecycle test uses a
deterministic fake trusted toolchain instead of inheriting a contributor
machine's Go installation.

## 2026-07-30 — TASK-260720-2x6mjn Windows generation semantics

PR #15 exposed two Windows portability assumptions. Python 3.12 and later can
report creation time as `st_ctime` for a pathname stat while descriptor stat
reports metadata change time, so comparing every field across `lstat()` and
`fstat()` falsely reported `concurrent_state_change`. The generation probe now
uses `os.path.samestat()` for cross-API file identity plus comparable type,
size, and modification-time fields. Descriptor metadata remains checked
before and after reading, and a full same-API pathname recheck after the read
still rejects replacement or concurrent metadata changes.

The no-Go real-install tests also created extensionless executable symlinks.
That hid `git.exe` from Windows `PATHEXT` lookup. Their shared PATH helper now
preserves the native Git executable name while exposing no Go executable.
Regression tests simulate the Windows pathname/descriptor `st_ctime` split,
prove post-read replacement is still rejected, and exercise both project and
global real-install paths with Git available and Go absent.

## 2026-08-01 — TASK-260720-th0jdi build currentness, repair, and GC

Build-aware project and global status now rederive schema-v2 build state from
the selected persistent raw snapshot and current static descriptor, toolchain,
native target, and fixed `manager-worker-v1` execution policy. The complete
cache key and canonical receipt remain the single comparison mechanism for
those dimensions; capability evidence is surfaced as result-only diagnostics
and cannot change a currentness verdict. Marker v1 remains current for skill
schemas 1–5, using an operation-private Git archive when its persistent
snapshot has already been collected. Marker v2 deliberately requires its
recorded persistent raw snapshot and never recreates it as a side effect of
status.

Repair remains the normal install operation. Missing, corrupt, unsupported,
wrong-input, or untrusted protected cache state is classified non-current, and
install recompiles from a freshly frozen and revalidated snapshot rather than
adopting or repairing candidate bytes. This includes receipts or markers that
name either the legacy policy-less cache key or the reserved hardened-policy
key.

Maintenance now marks runtime generations, snapshots, and schema-v2 build keys
from project, global, hybrid, registered-consumer, and active-transaction
marker roots while holding the manager-home lock. Any malformed or unstable
mark source suppresses all three sweeps. After a successful mark phase, native
cache backends remove only unreferenced entries older than the 24-hour grace
whose protected boundary, canonical receipt, complete logical key, artifact
path, hash, and size can all be proven. Legacy-policy, corrupt, and otherwise
unprovable entries are retained with warnings; that conservative retention is
intentional because GC cannot establish that the candidate is safe to remove.

## 2026-08-01 — TASK-260720-th0jdi review hardening

The journal mark boundary is per transaction context target, not a flat set of
paths. Each group includes live, staged, staged-source, backup, rollback, and
cleanup generations. GC snapshots every candidate before and after reading and
requires at least one valid marker in every group. A vanished generation group,
an unreadable journal or marker, or a concurrent generation change makes the
entire mark phase uncertain and suppresses runtime, snapshot, and build sweeps.
This prevents a valid marker from one project, global, or hybrid target from
masking the loss of another in-flight target.

Build retirement is also bound to the object that was classified. The POSIX
backend compares the inspected directory generation at the held parent
descriptor, renames through that descriptor, and verifies the quarantined
inode before deletion. A concurrent entry replacement is restored or retained,
while a concurrent root replacement leaves the new namespace untouched. The
Windows backend reopens and records the verified file identity, then uses
`NtSetInformationFile(FileRenameInformation)` with the held quarantine
directory handle so the rename acts on that exact object without replacing a
destination. The Win32 `SetFileInformationByHandle(FileRenameInfo)` form
returned `ERROR_INVALID_PARAMETER` for a non-null relative root on hosted
Windows Server 2025, while a pathname-only fallback could not bind the
destination root. Native backend tests exchange entries, driver roots, and the
destination quarantine root between classification and retirement and assert
that replacement state is never deleted.

Runtime orphan cleanup recognizes only the manager-owned legacy and indexed
temporary/backup names and indexed stale names. PID liveness remains the sweep
gate; malformed, leading-zero, or unrelated dotfile names are retained.

## 2026-08-01 — TASK-260720-12r55p rc.6 candidate consumption

The Python conformance harness now consumes the reviewed protocol 1.0.0-rc.6
candidate at curator-spec commit
`432eb2ee1fe2d6b271e37269f867c8851c325539`, manifest
`sha256:12e58b82579645ba1ccafba49d3e2dd3216005ddf37ae63c68a9fafd46773071`,
exclusively through `CURATOR_CONFORMANCE_ROOT`. Data-driven adapters route the
schema-v6, local go-v1, portable execution-policy, cache/identity, and
manager-lifecycle vectors through CocoaSkills validators where the candidate
publishes executable inputs; declarative cases are asserted independently and
fail closed without copying Curator implementation code.

This commit records candidate evidence only. The committed CI curator-spec ref
remains unchanged, claim-v3 fixtures remain on protocol 1.0.0-rc.5, and no
release pin, conformance claim, schema-v7/external-repository surface, tag, or
GitHub Release is advanced here. The harness covers all 102 selected generated
schema cases, 8 build-driver positives, 77 build-driver rejections, 10 build
source cases, 12 toolchain cases, the complete policy minimum clusters, all 11
byte-exact build-driver artifacts, and all 32 manager-lifecycle cases.

Review hardening replaced metadata-only fallthroughs with closed name-to-seam
bindings. Every rejection case now rejects unknown names and fields and drives
the corresponding CocoaSkills manifest, filesystem, package-graph, compiler,
process, toolchain, cache, context, or execution-policy boundary. Build-source
and toolchain cases exercise snapshot/fingerprint creation and mutation guards.
The fixed-process case records the three real parent probes and captures the
worker plan before launch, then compares all 28 fixed environment entries and
all five argv, cwd, and source-awareness records exactly.

Candidate files are now admitted only through the reviewed manifest inventory:
the harness verifies membership and SHA-256 before loading every in-scope
vector, selected schema instance, go-build fixture file, and expected build
artifact. Lifecycle adapters have an exact 32-name cluster/field map and assert
all rollback lock and desired-digest guards. Mutation-sensitivity tests retain
the rejected reviewer probes so future expected-error, environment, argv,
lifecycle-field, or candidate-byte drift fails closed.

Hosted Windows rework exposed two fixture portability assumptions. The shared
toolchain entries are intentionally unsorted, so adapters now materialize
directories and files before internal links and never convert link failures
into platform skips. The shared Darwin launcher is modeled with its declared
execute mode at the filesystem-observation seam on every host; CocoaSkills'
real regular-file, stable-identity, native-header, and open-boundary validation
still runs. Regression tests remove the host execute bit and require each link
target to exist at creation time, making both Windows failures reproducible on
POSIX without `os.name` bypasses.

A subsequent Windows run showed that target-first creation alone was
insufficient: Windows rejects a relative link whose stored target uses the
suite's POSIX separators. The fixture now creates that target from native path
components, verifies that the native `readlink` result denotes the exact
declared vector target, and exposes the declared POSIX spelling only at the
protocol-byte boundary. CocoaSkills still performs the real tree walk,
resolution, mutation checks, framing, and digest calculation.

## 2026-08-02 — TASK-260720-12r55p accepted consumer integration

The independently accepted rejection binding at `7b016388` and lifecycle
binding through `80b5b167` were integrated into the rc.6 candidate consumer.
The shared adapter keeps the rejection implementation's exact observed product
outcomes while routing all 32 lifecycle cases through the native observation
harness. The combined authenticated candidate-root gate passes 1,025 tests;
the related transaction, install, status/currentness, GC, planner, Go driver,
toolchain, and source suites pass 496 tests. Strict mypy, package build, and
Twine validation also pass. The candidate remains identified only by reviewed
curator-spec commit `432eb2ee1fe2d6b271e37269f867c8851c325539` and manifest
SHA-256 `12e58b82579645ba1ccafba49d3e2dd3216005ddf37ae63c68a9fafd46773071`;
no workflow pin, tag, release, schema-v7 surface, or conformance claim changed.

The first integrated hosted run exposed one cross-platform regression after the
manager-home lock moved behind the lifecycle gates: `LockError` was absorbed by
the ordinary per-project/global result boundary, so CLI contention returned 1
instead of the stable `EXIT_LOCK` value 3. Project and global installers now
re-raise coordination failures to the existing CLI boundary, with direct
regressions for both scopes. The post-fix authenticated candidate gate again
passes 1,025 tests and the affected CLI/install suites pass 153 tests.

## 2026-08-01 — BUG-260801-1xvc35 observed rejection outcomes

The cycle-2 rejection audit found that a closed 77-name table was still acting
as an answer key: its values included the expected protocol error, while 75
published condition strings were ignored. That let an unrelated
`SkillSpecError`, the wrong toolchain error, or a synthetic cache inspection
produce a passing case. The binding registry now contains only the independent
boundary and exact published condition. Every case constructs its condition
fixture and records the error, cache status/reuse, command/cache effects, and
artifact-execution count observed at the corresponding CocoaSkills seam before
comparing the complete vector expectation.

Manifest, filesystem, module, package-graph, compiler-directive, worker
environment/argv, projected toolchain, context, build-metadata, and protected
cache cases now execute their real validators or backends. Cache artifact
corruption is published as a valid protected entry, mutated in place with the
platform's protected-state profiles restored, and inspected again; the hash
regression therefore observes `HIT` followed by `CORRUPT`. The marker-embed
regression materializes its exact `example.com/embedmarker` module and Go
source, reproduces the published legacy digest and both build-source digests,
and rejects the root source before any cache-key or Go command effect.

Regression coverage mutates all 75 conditions and all 321 expected-field
leaves through the public adapter. Dedicated sabotage cases require unrelated
skill errors and `untrusted_go_executable` at the wrong path seam to fail, and
require artifact-hash corruption to reach the post-mutation cache inspection.
This change remains confined to the rc.6 rejection harness: no workflow pin,
schema, tag, release metadata, or conformance claim is changed.

## 2026-08-01 — BUG-260801-1iu1ln lifecycle trace hardening

Cycle-7 review found three remaining ways that a normative lifecycle answer
could survive without its required CocoaSkills observation. A process could
mutate and restore a published cache descendant, a dry-run/planning/private-
failure path could transiently write and restore a persistent surface, and the
all-project upgrade deduplication answer could be true without fetching any
member of the dependency closure. Dedicated sabotage tests reproduced all
three gaps through the real transaction, installer/planner, private-build, and
upgrade seams before the observer was changed.

The shared persistent-mutation observer now resolves descriptor-relative I/O
on Darwin with `F_GETPATH` as well as the Linux `/proc/self/fd` and portable
`/dev/fd` forms. Publication traces cover the live entry after its atomic
rename and reject descendant writes, truncates, fsync-backed restoration, or
permission changes while distinguishing the single legitimate cache-root
seal. Project and global upgrade dry-runs, every planning-gate probe, and the
private-build failure case similarly classify any observed write as an effect
even when the final byte snapshot is identical.

All-project upgrade observation now requires the exact nonempty direct and
transitive repository closure, once per repository, and separately excludes
the unrelated repository. Zero-fetch and duplicate-fetch probes both fail the
normative vector. Strengthened descriptor tracing also exposed legitimate
permission changes while repair quarantines and replaces an invalid candidate;
the rebuild condition therefore remains based on the observed repair pipeline,
post-repair currentness, and absence of candidate execution, while the
chmod-and-adopt shortcut continues to reject permission mutation.

The pre-fix six-case sabotage gate exited 1 with five failures and one already-
protected duplicate-fetch pass. After the observer changes it exited 0 with
all six probes passing. The exact 32-case scalar-leaf/classification gate
passes all 417 cases, including the repair refinement. No release pin,
schema-v7 surface, tag, claim, or CI configuration changed.

## 2026-08-01 — BUG-260801-1iu1ln lifecycle alias hardening

Cycle-8 review demonstrated that attribute monkeypatching is not an authority
for persistent-state immutability: `io.open` file-object writes and callables
captured from `os` before observer installation could write, fsync, truncate,
chmod and timestamp-restore protected files without an event. The same review
showed that private-build failure observation omitted the project
`Skillfile.json`. A standalone three-case pre-fix gate reproduced all three
survivors and exited 1 with three failures.

Read-only lifecycle evidence now uses a recursive tamper witness containing
node kind, mode, device/inode identity, link count, owner, size, bytes or link
target, `mtime_ns` and `ctime_ns`. On the supported Darwin filesystem, a direct
probe restored bytes, mode, inode and `mtime_ns` exactly after a file-object
write while `ctime_ns` remained changed. Atomic publication compares the
staged and live descendant witnesses across the no-replace rename; only the
moved root's rename-induced ctime change is allowed before its normal seal.
This makes I/O spelling irrelevant without growing another alias patch list.

The private-build failure witness is taken exactly around
`_build_private_misses` over the complete project, config, manager-home and
source roots. Operation-private build staging remains outside those roots, and
stable lock records are explicitly excluded as coordination state. This phase
boundary avoids treating earlier source snapshot creation as a failure effect.
An idempotent `mkdir(exist_ok=True)` trace on the existing manager home also
confirmed that attempted operations alone are not state changes; recursive
identity/ctime equality is authoritative while operation traces continue to
classify named effects.

Post-fix, the four new alias/surface/timestamp regressions, all 32 canonical
lifecycle cases, all 417 scalar/classification checks, and all 28 inherited
sabotage probes pass. No product release, pin, claim, schema-v7, tag, CI,
changelog or packaging surface changed.

## 2026-08-02 — BUG-260801-1iu1ln atomic handoff boundary

Cycle-8 review found that the staged-to-live tree comparison still normalized
the moved root's final `ctime_ns` without proving which operation produced it.
A callable captured before observer installation could therefore `fchmod` and
restore the live root after the real no-replace rename, or rename the live name
away and back, while the complete publication case remained normative.

Publication observation now witnesses the destination immediately after the
underlying atomic OS primitive: `renameat2`/`renameatx_np` on POSIX and
`MoveFileExW` on Windows. The staged tree must match that raw handoff with only
the rename-induced root ctime transition allowed, and the raw handoff must then
match the state returned by the wrapping CocoaSkills seam exactly. This places
captured-callable mutations on the observed side of the boundary without
mistaking the later legitimate cache-root seal for sabotage.

Dedicated root-fchmod/restore and live-name-away/restore regressions exercise
captured callables and descriptor-relative POSIX names; the Windows regression
uses the corresponding supported path rename form. Both new probes, the four
cycle-8 regressions, all 32 canonical lifecycle cases, the 417-case exhaustive
scalar/classification gate, and the full authenticated conformance module pass.
No product, release, pin, claim, schema-v7, tag, CI, changelog or packaging
surface changed.

## 2026-08-02 — BUG-260801-1iu1ln native-callable audit boundary

Cycle-9 review demonstrated that wrapping the function returned by
`ctypes.CDLL` still sampled too late: a delegating `renameat2` or
`renameatx_np` callable could perform the real atomic rename, use a previously
captured `os.fchmod` to change and restore the published root, and only then
return to the observer. The raw destination snapshot therefore remained
normative despite two real post-handoff permission mutations.

Publication observation now activates a scoped CPython audit sink only around
`backend.publish`. CPython emits `os.chmod` audit events for descriptor-based
`os.fchmod` even when the callable was captured before monkeypatching. The
trusted audit paths below the live entry must exactly correspond to the
ordinary observed root-seal trace; any additional captured permission event
makes publication incomplete. A `ContextVar` scopes the sink to the active
publication and a `finally` block resets it, while the process audit hook stays
inert outside that boundary.

The retained POSIX regression injects the mutating/restoring callable through
`cache_posix.ctypes.CDLL`, one layer inside the former witness. A Windows-only
equivalent injects through `_api().kernel32.MoveFileExW` and changes/restores
the live directory with a captured `os.chmod`; Windows CI exercises that case,
and non-Windows runs skip it explicitly. The new POSIX probe, the cycle-8/9
barrier, all 32 canonical cases, all 378 scalar mutations, and the full
authenticated module pass. No product, release, pin, claim, schema-v7, tag,
CI, changelog or packaging surface changed.

## 2026-08-02 — TASK-260720-12r55p portable lifecycle chmod observation

The first hosted matrix after integrating the observed rc.6 bindings passed
strict mypy plus every Ubuntu and macOS lane, but all four Windows lanes failed
before lifecycle assertions because `os.fchmod` is not provided on Windows.
The persistent-mutation observer had unconditionally captured and patched that
POSIX-only callable, so one observation-infrastructure portability fault
expanded into 408 conformance failures per Windows job.

Descriptor chmod observation is now installed only when the host exposes
`os.fchmod`. Captured file-object sabotage uses the already supported
path-based `os.chmod` primitive, preserving independent write, mode and
timestamp restoration evidence on both platform families. A regression
removes `os.fchmod` from the active `os` module and still requires a protected
path-chmod mutation event.

The first post-repair Windows run then reached the native cache validator and
exposed a second fixture-only assumption: lifecycle artifacts were prepared
with raw `chmod(0700)`. That is sufficient on POSIX, but an elevated Windows
process creates new objects under the token-owner group, so publication
correctly rejected the source as not owned by the current manager principal.
The fixture now calls the same public
`cache.make_publication_source_private` primitive as the production installer;
that primitive retains chmod semantics on POSIX and applies owner/DACL state on
Windows. No skip, xfail, platform bypass, product behavior, release pin, tag,
claim, schema or workflow pin changed.

The next Windows run reached launcher materialization and exposed a third
fixture-only assumption: the candidate compiled-build receipt is intentionally
Darwin/arm64 and byte-bound to its published cache key, but an implicit launcher
selection followed the executing Windows host. Normative lifecycle installs now
derive only that implicit selection from the authenticated receipt target;
explicit platform arguments remain unchanged for launcher-specific cases. This
keeps the shared identity byte-exact without weakening the product's native
activation rejection.

The following matrix showed that deriving activation from the Darwin receipt
still forced a non-native executable contract on Windows. Lifecycle operations
now use an internally consistent host-native target, tuning, toolchain identity,
receipt, and cache key. Only after CocoaSkills has validated those native
operations does the observer project the authenticated candidate fixture key
into the protocol result. A regression proves both sides of that projection;
the shared vector bytes remain the source of the logical identity and the
product's native activation checks remain untouched.

The same Windows run exposed three independent probe portability faults.
Descriptor sabotage now uses direct file-descriptor I/O where Windows cannot
open directory descriptors, timestamp restoration falls back when
`follow_symlinks=False` is unsupported, and the `MoveFileExW` wrapper initializes
its delegated callable through `object.__setattr__` so its forwarding setter
cannot recurse before initialization. No skip, xfail, platform bypass, product
behavior, release pin, tag, claim, schema, or workflow pin changed.

## 2026-08-02 — TASK-260720-12r55p native Windows lifecycle evidence

Exact-head hosted run 30751379393 completed with the same three fixture-only
failures on Windows 3.11, 3.13, and 3.14; Windows 3.12 separately lost runner
communication. The atomic publication observer compared the Win32 extended
destination spelling with a normal `Path` spelling, the untrusted-cache case
never drifted the Windows DACL, and transient write/restore evidence used
Python's Windows `st_ctime` creation timestamp instead of the native change
time. The observer now compares native file IDs, deliberately changes one
sealed artifact to the mutable DACL, and records `FILE_BASIC_INFO.ChangeTime`
for ordinary Windows files and directories. Reparse points retain the portable
`lstat` witness. A focused native-identity regression and the full immutable
candidate-root conformance file are green locally. No product behavior,
release pin, tag, claim, schema, or workflow pin changed.

The next exact-head Windows run showed two remaining evidence-boundary defects.
The Windows publication sabotage still toggled the read-only attribute even
though the observer had moved to native DACL/change-time evidence, and the
status matrix treated change-time drift as a second mutation signal despite
already owning a causal write observer. The sabotage now drifts and restores
the real sealed-directory DACL. Status snapshots compare every persistent
field except change time and still require zero traced mutation operations;
publication and rollback witnesses continue to use native change time. This
keeps status reads non-mutating without weakening byte, identity, mode, DACL,
or explicit-write coverage.

Hosted validation disproved that attempted separation. Run 30763408033 still
reported a causal/persistent mutation for `unsupported-toolchain` after the
change, while the prior exact-head run reported `wrong-native-target` and
`build-source-mismatch`. Removing the causal observer would make the vector
pass by construction and recreate the self-asserting coverage rejected in
review. The task therefore stops before that workaround: either product status
behavior must become read-only under these failure boundaries, or the protocol
owner must explicitly redefine the vector's empty `mutations` outcome and its
required evidence model.

The next exact-head Windows matrix completed its full two-hour native lifecycle
path and exposed one remaining direct timestamp call in the GC fixture. The
entry-aging step still required `os.utime(..., follow_symlinks=False)`, so its
`NotImplementedError` invalidated the shared cached observation and cascaded to
408 failures. Timestamp changes now share one helper that first requests the
stronger no-follow form and retries only when the platform reports that keyword
unsupported. A platform-independent regression forces that exact fallback, and
the representative GC vector plus the full authenticated conformance module
remain green. No product behavior or release surface changed.

## 2026-08-02 — TASK-260720-12r55p portable lifecycle GC and launcher observation

The cycle-3 review and the terminal Windows 3.11 job of run 30743353816 agree on
one cause: every one of the 408 Windows failures reported
`NotImplementedError: utime: follow_symlinks unavailable on this platform`, all
raised at the same GC aging call. The previous repair centralized the fallback
but reused it at only one of five timestamp writes, so the first unconverted
call still aborted the shared cached observation and cascaded across all 32
lifecycle cases. All five aging writes now go through the one helper.

Two regressions close that class rather than the single line. A behavioural test
runs the complete GC observation while `os.utime` rejects `follow_symlinks`
exactly as Windows does, and asserts the observed sweep, grace, uncertainty and
lock evidence still match. A source-level test parses this module and the
conformance module and fails when any `utime`/`chmod` call passes
`follow_symlinks=False` outside the sanctioned helper or a POSIX-only scope.
Both were verified against the reverted pre-repair source.

Because that single boundary aborted the shared observation, hosted Windows CI
has in fact only ever executed the first six of thirteen observers; the whole
observation module postdates the last Windows-green commit. A static sweep of
the remaining path found two further boundaries and a companion source test now
guards the second class. Descriptor-relative low-level I/O in the read-only
binding used `dir_fd` and `O_DIRECTORY`, neither of which exists on Windows, and
now has a path-based branch. Launcher observation executed the POSIX launcher
unconditionally; a Windows host cannot even write that flavour, because `:`
separates a POSIX PATH list and every absolute Windows path carries a drive
separator. Each host now executes its native launcher and reads the other, which
is the symmetry POSIX hosts already had. No product behavior, release pin, tag,
claim, schema, or workflow pin changed.

## 2026-08-03 — BUG-260803-2sqyqy isolated Windows status observation

Matched hosted evidence and an exact-head native focus run excluded the accepted
status fix and the later DACL-witness change as deterministic sources of the
Python 3.14-only currentness labels. The conformance observer nevertheless
collapsed persistent snapshot drift and causal protected-root calls into one
label while retaining all fourteen status boundaries inside a shared lifecycle
root. It could therefore report a host or ordering signal as an unexplained
product mutation.

Every currentness failure now provisions and removes its own short temporary
root and owns an independent MonkeyPatch lifetime. The vector remains a single
matrix with the same fourteen conditions and exact fields. Non-vector
diagnostics record the sequence position and predecessor, fresh-root identity
and cleanup, snapshot differences as sanitized root/relative-path/field tuples,
and causal calls as sanitized operation/root/relative-path tuples. Windows
snapshots now retain the native owner/DACL alongside byte content, identity,
mode, attributes, last-write time and native ChangeTime. Persistent drift and
causal mutation observation both remain fail-closed; neither signal is filtered
or converted into an expected answer.

Regressions run the three hosted labels five times from distinct roots, run all
fourteen cases in forward and reverse order, and prove that snapshot-only host
drift is diagnosed separately from an attributed CocoaSkills write. Existing
low-level create/remove and permission-change/restore sabotages still invalidate
the matrix. The accepted product status implementation and rc.6 vectors are
unchanged.

## 2026-08-03 — BUG-260803-2sqyqy Windows VCS drift provenance

Fresh hosted Windows Python 3.13 evidence narrowed the remaining status labels
to archive-attribute changes on `.git` administrative directories. The same
run attributed no causal CocoaSkills write and exposed one planning failure at
the source-audit probe, whose cases still reused a mutable repository fixture
and published no diagnostic provenance.

Snapshot observations now retain every field-level difference and classify its
owner. A Windows directory-only `file-attributes` change below `.git` is
reported as `host-vcs-administration`; native ChangeTime remains a separate
observer signal; all other drift remains `protected-state`. Only the first two
non-product classifications can be read-only without a causal write. Any byte,
identity, mode, DACL, timestamp, existence, or mixed-field change remains
fail-closed, and any causally observed write remains a mutation regardless of
path or final snapshot.

Each planning failure probe now creates and removes its own repository,
manager-home, and skills-root fixture. Its non-vector diagnostics expose the
fresh-root lifecycle, raw snapshot provenance, causal writes, and mutation
verdict per gate. This removes gate-order contamination while keeping the rc.6
vector and its persistent-mutation sensitivity unchanged.

## 2026-08-04 — TASK-260720-3pemm6 real Go public-flow E2E

Added a small offline-vendored Go skill fixture and process-level tests that
join the previously separate real compiler, public installer, protected cache,
transaction, recovery, and native shim seams. macOS and Windows selections use
the installed manager plus Go 1.25 and cover project, global, hybrid, mixed
script/build activation, exact argv and exit forwarding, cache/currentness,
dry-run, rollback, recovery, concurrent CLI publishers, two projects, and
repair. Ubuntu has a separate portable script lane and requires source-aware
go-v1 to fail closed before worker launch or publication.

CI keeps the committed curator-spec checkout at
`0c81c1f8d5321d822be2a2817b05aea03e656e15`. A separate repository variable
supplies candidate commit `432eb2ee1fe2d6b271e37269f867c8851c325539`;
every matrix cell authenticates the manifest digest and uploads collected node
IDs plus JUnit evidence. This is non-release evidence and changes no release
workflow, tag, release record, or conformance claim.

The implementation is locally gated but its required signed commit is blocked
until the configured SSH signing key is unlocked or the GitHub credential is
granted permission to use the auto-signed `createCommitOnBranch` mutation. The
remote task branch currently remains at the recorded base and contains no
unsigned implementation commit.

The first PR-head run exposed two harness defects. Windows exhausted the legacy
path budget while pytest nested the vendored source snapshot under its default
temporary root; the focused command now uses the short, runner-owned
`runner.temp/csk-e2e` base, which is also reproducible locally with pytest's
`--basetemp` option. Ubuntu's fail-closed test now guards the concrete worker
launch seam instead of replacing the whole build entry point, so the real
source-aware native-control preflight returns its stable unavailable diagnostic.
## 2026-08-05 — TASK-260728-1ph8rs schema-7 repository models

Implemented the read-only Python boundary for external build repositories
without adding acquisition, audit, build, or install mutation. The binding
protocol is curator-spec `v1.0.0-rc.5` at
`f5d7673039226ab81de2f4f87e2155ae995c4df3`, source baseline
`57c1f56846d221ecc55786bd3c2467ec32f11730`, with conformance manifest SHA-256
`b6f56aacc0e37dcc6692f73f641bff761e89b645adfe20a47a06d81c6fda204c`.
The independently accepted architecture-v6 resource was rehashed as
`2abae77d80eba6789f9911db7e9722595b4f21ba47391ca9eafd0064af03d67e`.

The Curator reference checkout was at
`74fe162415d800cd0a6975313827f9dc8594d299`; its accepted schema/model task
`TASK-260728-pwbr32` was `done`, and the current relevant overlay
(`internal/buildrepo`, `internal/devsub`, `internal/skillspec`) had binary-diff
SHA-256 `01fd5e0d33dfce19b545f19342d7e0e20e944e1d9beeaa166ec24afadae5e91e`.
The broader referenced `TASK-260728-20ao7p` remained `backlog`, so no claim is
made that its future acquisition/E2E surface was consumed by this parsing-only
task.

Authenticated rc.5 schema/model tests passed with 43 tests, the focused Python
suite passed with 160 tests and one pre-existing environment-gated schema-6
skip, and the full local suite passed with 1,252 tests and 218 platform/external
suite skips. Mypy, compileall, diff validation, sdist/wheel build, and Twine
checks all exited zero. The release cases also exposed and closed a schema-1
compatibility gap: schemas 1–6 now reject reserved schema-7 repository/target
fields while retaining their prior script and local `go-v1` behavior.

## 2026-08-05 — TASK-260728-2mfeje clean Git and raw-object admission

Implemented Python Git acquisition and admission without archive, checkout, or
working-tree byte derivation. Network acquisition validates an operator-pinned
Git tool, initializes a private bare object store under an empty configuration,
fetches either the full locked OID or one exact tag refspec, and reads only full
OIDs through a bounded `cat-file` protocol. Admission recomputes SHA-1/SHA-256
object identities, parses commit/tag/tree semantics, proves the reachable graph,
and rejects Git LFS pointers, submodules, links, special modes, malformed or
missing objects, and unexpected reader termination.

Local substitutions admit only a narrow ordinary `.git` files/refs/object
layout. Config includes, alternates, grafts, replace refs, promisor/partial clone
state, reftable, linked worktrees, bare repositories, Git files, pack sidecars,
and source races fail closed. Pack/index pairs are checksum-, fanout-, offset-,
and CRC-validated before they enter a sealed private object store. Source-owned
filter and credential-helper configuration remains inert because no source
configuration is passed to an executing Git process.

The focused SHA-1/SHA-256 network/local, exact-tag, shared raw-object/LFS/pack,
all 15 local-config/ref vector, race, and adversarial suites passed with 171
tests and four platform/environment skips. Strict mypy, `git diff --check`, and
sdist/wheel build gates exited zero. Per the tracked operator directive, the
known over-23-minute bare full-suite run was not repeated; consolidated full
security/conformance/platform validation remains assigned to the manager E2E
task, and this result must remain unmerged/unreleased until that gate accepts.

## 2026-08-05 — TASK-260728-2uxmut external snapshot audit and cache

Added the schema-7 external repository pre-publication pipeline. Every admitted
repository snapshot is materialized and byte-revalidated, digested as an
independent build source, descriptor-checked, and passed to an independently
typed audit subject before any protected artifact lookup or compiler call.
Declared package identity and immutable lock remain separate from the effective
operator-selected identity in snapshot keys and receipt-v2 cache inputs. Fixed
policy state, target, descriptor selection, native target, and the existing Go
toolchain identity are bound into the same canonical input.

The protected store admits exact snapshots and receipt-v2 artifacts only below
an owner-controlled, link-free boundary. It verifies canonical metadata, file
sets, hashes, receipt input, executable bytes, and link counts on every reuse.
Mutating operations quarantine corrupt live entries before rebuilding; dry-run
detects corruption without mutation. Untagged installs may reuse an explicitly
referenced exact protected snapshot while offline, then repeat whole-snapshot
validation and audit. Tagged declarations still require fresh same-operation
tag proof and fail closed offline.

Compiler staging copies only the descriptor-selected build root and translates
the selected source directory relative to that root. The adapter invokes the
existing closed `go-v1` compiler with the exact established toolchain session;
it creates no second probe or altered local `go-v1` receipt. Persistent reuse
fails closed on non-POSIX platforms until a native protected identity proof is
provided, avoiding unproved Windows cache trust. Marker publication and the
full install transaction remain assigned to the downstream lifecycle task.

## 2026-08-05 — TASK-260728-3jaa57 mixed receipt marker and lifecycle

Extended the schema-7 lifecycle boundary with canonical receipt-v2 identity
vectors, marker-v3 records that structurally distinguish local `go-v1`
receipt-v1 commands from external `go-repository-v1` receipt-v2 commands, and
fail-closed activation validation for manager-derived artifact paths, hashes,
sizes, executable state, and single-link ownership. Existing schemas retain
marker v2 and cannot accept external receipt state through a schema alias.

Read-only status now recognizes marker v3 without interpreting external
evidence as a local cache receipt, and external artifact/snapshot collection is
rooted only by validated marker or journal state. The existing project/global
transaction, rollback, crash-recovery, collision, PATH/shim, repair, and
deduplication suites remain the shared lifecycle enforcement surface.

Focused verification passed with 209 tests and two platform skips in the
preserved producer run; an independent rerun passed 123 tests. Strict mypy,
offline isolated package build, Twine distribution checks, and `git diff
--check` all exited zero. The first ordinary isolated build attempt was
interrupted after dependency installation stalled (exit 130), and the direct
no-isolation retry correctly failed because the existing venv lacked
`setuptools` (exit 1); the offline cached `uv` build then succeeded without
network access.

## 2026-08-05 — TASK-260728-3kuxg7 rc.5 external-repository lifecycle

Wired schema-7 external repository builds into the existing project and global
installation transactions. The manager now acquires an exact admitted Git
snapshot, audits materialized source before cache lookup or compilation, binds
the fixed Go toolchain session into receipt-v2, publishes marker-v3 state, and
activates the protected artifact through the existing direct managed-shim
boundary. Offline reinstall can reuse only a named, revalidated immutable
snapshot; cache corruption is quarantined and repaired, while declaration
removal reconciles the shim and marker through the normal uninstall lifecycle.

Added an implementation-independent consumer for all 60 authenticated rc.5
external-repository cases (18 threat and 12 lifecycle vectors) and real local
Git project/global lifecycle tests. The consumer pins curator-spec
`v1.0.0-rc.5` at `f5d7673039226ab81de2f4f87e2155ae995c4df3`, manifest SHA-256
`b6f56aacc0e37dcc6692f73f641bff761e89b645adfe20a47a06d81c6fda204c`,
external corpus manifest SHA-256
`cc9e9c0f93b2497a060a533503a4d030d1a715fe1dd4eb8bf9820168a9257697`,
and Curator reference checkout
`74fe162415d800cd0a6975313827f9dc8594d299`. The rc.5 consumer does not import
or execute Curator internals.

The authoring and architecture documentation now describes schema 7, exact
identity and tag rules, ordered audit/build/install behavior, protected cache
and repair semantics, project/global activation, declaration-driven uninstall,
and local development substitutions. It recommends direct manager-owned shims,
not script wrappers or PATH hacks, and explicitly limits this qualification to
macOS and Windows without claiming Linux support.

Native macOS qualification ran through SSH alias `relux` on macOS 15.7.4
(24G517), Darwin 24.6.0 x86_64, Python 3.12.13, Apple Git 2.50.1, Go 1.25.5,
uv 0.11.29, and a staged csk `0.0.0rc5` build. The corrected task-focused
rc.5/rc.6 suite passed 311 tests with one platform skip in 215.50 seconds;
strict mypy passed 71 source files. A first broad attempt incorrectly pointed
rc.6 regression tests at the rc.5 fixture root and was interrupted with exit
130. The corrected broad run then exposed a real Darwin rename failure for a
pre-sealed directory; publication now retains the private root mode through
atomic rename and seals the published root afterward, matching the established
cache boundary. The corrected native external subset passed 23 tests.

Windows qualification resumed after `mbpro-win` returned to Tailscale and SSH.
The native host is Windows 10 Pro 10.0.19045.6466 on amd64 with Python 3.14.4,
Git 2.50.1.windows.1, Go 1.25.5, uv 0.11.29, and staged csk `0.0.0rc5`. All
three staged manifest hashes matched the recorded rc.5 external, rc.5 schema,
and current schema-regression pins. The first native run retained its real
negative result: 21 failed, 273 passed, and 18 skipped because fake Git tests
used POSIX helper paths and raw materialization incorrectly required sealed
cache ownership, while artifact sealing set readonly before applying the DACL.
The corrected design keeps strict owner/DACL validation for protected entries,
uses handle-based disk/reparse/type/link validation for fresh materialization,
applies the Windows security profile before readonly state, and uses native
test helpers. The focused rerun passed 46 tests; the final native qualification
passed 294 tests with 18 platform-appropriate skips in 269.45 seconds. Native
mypy still reports 113 pre-existing platform-stub diagnostics in POSIX-only
modules; the authoritative cross-platform source check remains the green
71-file macOS/local mypy run. No Linux support is claimed.

Reviewer rework tightened the source-acquisition boundary after an independent
reproducer showed that `INCOMPLETE_SOURCE` could be mistaken for an offline
transport outage and served from protected cache. Offline reuse is now limited
to the typed `SOURCE_UNAVAILABLE` admission result; malformed/incomplete graph
failures and non-recoverable exceptions propagate. The new regression passed
locally and in both native qualification suites. Final post-rework results were
312 passed/1 skipped on macOS and 295 passed/18 skipped on Windows, both exit
zero; local strict mypy, package build, Twine validation, and diff checks also
exited zero.

## 2026-08-06 — BUG-260805-fky0kz prerelease distribution routing

The preserved `v0.13.0-rc.3` Distribution Smoke logs prove that every installer
resolved the exact candidate from production PyPI while the release workflow
had published it only to TestPyPI. Pipx failed on Ubuntu, macOS, and Windows;
uv tool failed on the same three platforms; mise failed on Ubuntu and macOS;
and the `CSK_VERSION` install.sh path failed on Ubuntu and macOS. The resolver
also produced the non-canonical spelling `0.13.0rc.3`. The hash comparison then
ran after all smoke jobs failed and reported zero artifacts, a secondary error
rather than an independent content mismatch. A separate workflow-run guard
explicitly skipped prerelease tags, so only the release event exposed these
production-channel assumptions.

Release routing now accepts only canonical stable, rc, alpha, and beta tag
shapes and binds the tag to exact wheel/sdist filenames and embedded metadata.
Prereleases publish through trusted publishing to both TestPyPI and production
PyPI, upload GitHub assets to a draft, and only then publish the prerelease with
`make_latest: false`; this order supports immutable-release repositories. Stable
tags keep the production-only stable route and `make_latest: true`. Both
publishing paths explicitly generate PEP 740 attestations. Before upload and
again before each publication, the workflow verifies the exact two distributions and
`SHA256SUMS`. Post-publication verification downloads every GitHub asset and
requires exact asset names plus GitHub, checksum, and PyPI digest agreement.
It also requires `/releases/latest` to remain the stable tag and independently
derives the newest non-yanked stable version from PyPI.

Distribution Smoke now runs after successful RC release workflows, verifies
the published contract before any installer starts, pins pip, uv, and mise to
production PyPI, uses canonical `0.13.0rc4`, and suppresses Homebrew for
prereleases. Hash comparison runs only after successful resolution. Local
evidence built and installed both simulated `0.13.0rc4` and stable `0.13.0`
artifacts, then installed exact RC4 through pipx, uv tool, mise, and the
`CSK_VERSION` install.sh uv branch against an isolated PEP 503 index. Public
state was not mutated: production PyPI and GitHub latest remained `0.12.5`,
and no `v0.13.0-rc.4` tag or release existed during validation.

## 2026-08-06 — BUG-260806-2dgjjh repository root marker canonicalization

Schema 7 and `skill-build.json` already admitted `build_root="."` and
`source_dir="."`, but the external repository compiler adapter forwarded the
root marker into the closed go-v1 boundary, whose generic portable-path check
rejects dot components. The runtime now handles only the exact `"."` sentinel
as the frozen snapshot root before applying its existing canonical-directory,
containment, symlink/reparse, and `go.mod` checks. The shared identifier grammar
is unchanged, so traversal, absolute paths, backslashes, and embedded dot
components remain rejected. Focused schema, go-v1, pipeline, and native external
install tests cover root-module/nested-source and root-package forms.

## 2026-08-07 — BUG-260807-1r5oz9 verified natively, and the two Windows admission bugs were entangled

The operator Windows host (10.0.19045.6466, Python 3.14.4, Go 1.25.5, Git
2.50.1.windows.1) is reachable over SSH, so the local-admission fix was verified
on the machine that produced the original reproducer rather than by construction.

The two reported signatures had one shared chain. `_ObjectReader.read` issued a
single `self._stdout.read(size)` against a pipe; on Windows that returns short,
so `git cat-file --batch` blocked writing into a full stdout pipe and could not
exit when stdin closed, and `close()` raised `object reader did not terminate`
after its ten-second wait. The still-live process kept `pack-*.idx` mapped, so
`TemporaryDirectory` cleanup was refused with `[WinError 5]` — and that bare
`PermissionError` replaced the real diagnostic. The partial-read fix
(`BUG-260806-1bwq2z`) addresses the first link; `_remove_private_root` addresses
the last, so the admission diagnostic survives instead of being masked. Neither
alone completes the lifecycle; together `csk install` exits 0 for
`skill-bi@e9fa203d` with `bi-cli@e0f05112` from a local exact snapshot.

`_remove_private_root`'s docstring records this: on this host the removal
refusal comes from the live `git cat-file` handle, not from
`FILE_ATTRIBUTE_READONLY`. Unsealing remains correct for the POSIX `0o500`/`0o400`
seal, and READONLY defeating `shutil.rmtree` on Windows follows by construction —
but it has never been observed here, so it must not be cited as evidence.

Getting that far exposed a further defect, tracked as `BUG-260807-1it17m`. The
external build cache publishes its artifact under the extensionless name
`artifact` (`src/csk/build_repository_pipeline.py:341`) while `go_v1` builds
`<command>.exe` (`src/csk/builds/go_v1.py:6691`), and the Windows launcher calls
that target directly (`src/csk/shims.py:894`). `cmd.exe` resolves executables
through `PATHEXT`, so an extensionless PE cannot be run: the same 9507840-byte
`MZ` image fails as `artifact` in both sealed and plain directories and succeeds
as `artifact.exe`. Every `windows-latest` leg stays green because
`tests/test_install_external_repository.py` asserts only that the shim exists and
mentions the artifacts path, never executes it, and stubs the toolchain so the
cached artifact is not a real PE.

Both WB Draft pairs now complete the narrow lifecycle on that host, but only
with three fixes stacked. `skill-bi@e9fa203d` + `bi-cli@e0f05112` needs this fix
plus the partial-pipe-read fix (`BUG-260806-1bwq2z`). `skill-band@7b83aba1` +
`band-cli@0956c621` additionally needs the raised GOROOT fingerprint deadline
(`BUG-260807-3me1d5`): before it, band died at `dry-run` with
`go-v1 toolchain_timeout` and never reached admission at all, which is why the
band leg said nothing about admission ordering for two rounds. On a wheel
carrying all three, band runs audit -> dry-run -> install
(`would-preflight-and-build`, real Go build) -> repeat install (`cache-hit`) ->
`status --check` -> drift detected -> repair -> remove -> reconcile, with
neither reported signature anywhere and no patching of the installed manager.
The lesson worth keeping: a Windows leg that dies before admission is not
evidence about admission, in either direction.
