# Bounded worker observations for an owned fixture

This attachment is disabled by default. Importing `go_worker_observation` does
not open files, install hooks, change the environment or enable product events.
`go_v1._OBSERVATION_SINK` defaults to `None`; only a trusted in-process harness
sets it for the current context. There is no CLI, configuration, environment,
worker argument or protocol switch. The historical identity writer and Windows
timeouts remain unexplained; observations are not a fix or an admission waiver.

## Attach to the same attempt

The fixture creates a private plain directory under its own `tmp_path`, with a
normal seekable `x+b` sink, and retains the descriptor until the context exits:

```python
from go_worker_observation import Recorder

anchor = tmp_path / "worker-observation"
anchor.mkdir(mode=0o700)
sink = anchor / "trace.json"
with sink.open("x+b") as stream:
    trace = Recorder(anchor, sink, stream)
    with trace.node():
        result = run_the_existing_test_operation()  # same selector and budgets
    # Preserve this fixture-owned file before the fixture's ordinary teardown.
```

For example, the in-process `_install` calls in `test_go_build_e2e.py` enter the
real `go_v1.build` in this context. Existing state observers may coexist with the
attachment. A new Python process does **not** inherit a ContextVar: explicitly
attach within each owned child bootstrap if it needs manager events. No held
native or hosted attempt is run merely by importing or documenting this helper.

`node-start` is written immediately, with a fixed label rather than an arbitrary
pytest node ID. The parent associates each private sink with its selected node.
Launch boundaries and the original client deadline are also persisted
incrementally. `before-cleanup` flushes after worker teardown and before private
stage/cache removal; `node-end` flushes on return or error. A full process kill,
job cancellation, storage failure or missing fixture teardown can lose the last
buffered events, or leave a partial file; hard-kill retention is not guaranteed.
There is no collector in front of expiry termination.

## Identity coverage, including `-S`

Only an explicit short **owned child** may call
`CacheAudit.install_in_owned_child(owner_pid=parent_pid)`. The non-removable
Python audit hook must never be installed in the shared test runner. The child
must exit and be reaped. Use a trusted bootstrap before relevant imports, with
an exact fixture-owned package root and explicit cache-directory/module stems:

```python
# In the owned child's bootstrap, before importing the manager package:
audit = CacheAudit(trace, owned_package_root,
                   {"sources/__pycache__": ("local", "git")})
audit.install_in_owned_child(owner_pid=owned_parent_pid)
audit.post_state(owned_package_root / "sources" / "__pycache__")
# Perform the existing import/action; then inspect only this known cache path.
audit.post_state(owned_package_root / "sources" / "__pycache__")
```

Bootstrap helper/stdlib imports with bytecode writing disabled, then restore the
child's intended bytecode setting before the observed imports. The portable
tests do this with real `-S` children, both with and without `-B`. Relying only on
`sitecustomize` would miss `-S`. No audit hook or wrapper is inserted into the
hardened worker: its fixed startup vector, provenance and identity checks remain
unchanged. That worker, native writers, unrelated processes, pre-bootstrap
imports, relative/descriptor-relative operations, non-CPython cache names and
paths outside the explicit allowlist remain **uncovered**. This is not complete
writer attribution or permission to relax `__pycache__` event admission.

Audit `mkdir`, write-open and rename events are **pre-operation intents**. They
may describe an operation that subsequently fails. `cache-post` is a bounded
explicit lstat presence/identity observation, not proof which intent completed.
Only `audit.action(operation, known_path, callable)` records `action-returned`
after that exact callable returns. It preserves the callable's result/error;
an unwrapped action has no completion claim. No source bytes or whole tree are
read. Guard observations contain the numeric index into the guard's ordered
`paths` tuple and raw numeric vnode flags. Prior fixed stage events locate that
refusal in the attempt; the observer does not serialize those watched paths.
The batch's total event count precedes at most128 individual guard observations;
this bound and the recorder's dropped-input counter must not be read as complete
native event coverage.

## Deadline and safety contract

`launch-start`, `domain-launch` and `domain-launched` have monotonic times and
no client budget: the client deadline is created **after** actual domain launch
and identity/control validation. `client-deadline` records that unchanged
absolute deadline after its timer starts. Request, ready, list, manager
validation, permit, result and teardown events carry that original deadline and
remaining budget where available. Send/wait labels mark intent, not successful
I/O. Result labels follow nonce/kind and result-shape validation. Cached worker
PID/returncode are available; `null` returncode means unknown, not a fresh poll.
No deadlines are reset or extended, and observation time still spends the
original budget. Expiry termination has no observer call, scan, flush or stack
collection, and both teardown callbacks run outside its lifecycle lock. The
later `teardown-end` records the already-retained expiry flag; exact timer-fire
time, native Go stacks and Windows CPU/thread data remain unavailable.

Each recorder retains at most128 events and16KiB of JSON including counters.
Updates and snapshots are serialized within that process; audit recursion is
guarded per thread so another owned thread's event is not silently discarded.
Schema1 uses fixed stage/role/operation/root/coverage labels, numeric timing and
identity, booleans and configured bounded root-relative components. Unknown
fields/labels, nonfinite numbers and unapproved names are dropped **before**
serialization. Cache tags and temporary suffixes normalize to approved module
names. Dropped events/invalid inputs and failed sink writes have separate
counters (saturating at a documented63-bit ceiling in the encoded counters).
No argv, environment, protocol frame, nonce, exception text, source content,
stack local, absolute path or arbitrary node name is recorded.

The caller owns the private anchor and its namespace for the recorder lifetime.
The helper creates no paths and never chmods, unlinks or follows symlink/reparse
referents. Each flush requires plain ancestors, exact descriptor/path identity
and an exclusive regular-file inode (`nlink == 1`). Static outside, linked,
replaced and unavailable sinks refuse without modifying the referent. This is
not a hostile concurrently-renamed namespace capability or a cross-process
shared sink. Observation failures are isolated only around optional recording;
the actual operation's result, primary exception/cause/context, cancellation
and cleanup remain authoritative. POSIX portable controls are exercised;
native Windows filesystem/control semantics and actual native Go execution
remain unqualified here.

Same-thread callbacks follow an explicit reentry policy in addition to the
process-local RLock: nested `record` attempts consume no slot and increment the
drop counter; nested `flush` refuses without writing and increments sink failures.
A recursively requested `_encode` snapshot raises a diagnostic-only ValueError
and increments drops. Normal snapshots serialize a copied sanitized row list;
counters reflect their snapshot point, so a callback drop after that point appears
in the next snapshot. No non-reentrant lock or deferred unbounded event queue is
used. If both emission and diagnostic accounting fail, no complete/drop-count
claim is possible: preserve existing evidence and the real action instead.
All CacheAudit emission/accounting paths, including install coverage and explicit
post-state, are optional; the actual action callable runs outside those catches.

The sink recipe requires the caller-owned normal seekable read/write `x+b` stream.
Pass that stream to Recorder; its lifetime and underlying fd stay caller-owned.
Each flush requires the same open stream, exact `x+b` mode and descriptor binding.
POSIX flush additionally rejects O_APPEND and other access modes before any write.
Windows uses the same stream-mode/binding admission; POSIX descriptor-flag checks
are not Windows qualification or protection against concurrent fd replacement.
