# Logbook

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
