# Logbook

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
