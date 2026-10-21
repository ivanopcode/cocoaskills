# Linux go-v1 source builds

Status: design proposal; protocol amendment and native qualification required.
Date: 2026-10-21.
Code baseline: `2e39fbb1af1bfa791cfa3196ba5699fd7d7a6a00`.

## 1. Scope and evidence

Extend the existing `manager-worker-v1` build boundary to native Linux for both
local `go-v1` and external `go-repository-v1` sources. Keep the fixed process
graph, compiler-input validation, source/toolchain identity, authenticated
one-list/one-build exchange, publication transaction, and deferred guarantees.
This document changes no product behavior and grants no Linux qualification.

**All Linux behavior below is unverified on a real host.** Mechanisms are
supported by interface documentation; proposed availability is a design verdict,
not a measured capability. Remote authentication failed before any remote
command ran. No Linux OS, kernel, architecture, filesystem, libc, interpreter,
Go, or uv version was observed. No remote retry or alternate access was used.
Section 8 records the exact pending probes. Code findings were inspected at the
baseline above; later implementations must recheck their cited symbols.

The current admission boundary is explicit:

- [`inventory_platform()`](../../src/csk/builds/go_v1.py) raises
  `build_execution_control_unavailable` outside macOS/Windows, before the worker
  or Go starts.
- [`supports_external_builds()`](../../src/csk/installer.py) admits only those
  two platforms; its external-build refusal precedes credential use.
- [Platform controls](../../ARCHITECTURE.md#platform-controls-and-evidence),
  [external platform qualification](../external-build-repositories.md#platform-qualification),
  and [the authoring guide](../authoring-cli-commands.md) document the same limit.

Code citations below name symbols in
[`src/csk/builds/go_v1.py`](../../src/csk/builds/go_v1.py) unless a different file
is linked. They refer to the inspected baseline, including its existing macOS
layout heuristic; they do not assume a pending macOS parser change is merged.

## 2. Protocol decision comes first

The accepted shared protocol is `1.0.0-rc.10`, commit
`b8b03d597ac83d158a0eadd9d0b25d2e883de1a3`, as pinned in
[external build repositories](../external-build-repositories.md).
Its `protocol/core.md` section 4.2.1 and
`conformance/v1/vectors/go-host-execution-policy.json#native_control_inventory`
were read at that exact revision. They define:

- `rc5-native-control-inventory-v1` as exhaustive, with five controls and only
  `macos` and `windows` tables;
- mandatory per-operation probes and application of every available control;
- a new inventory version for additions or changes of scope;
- `capability-evidence-v1` with only those two platform values and two
  availability values, `available` and `unavailable`;
- result-only evidence, excluded from hashed build identities.

The repository independently pins these rules in
[`test_rc6_native_control_inventory_entry_is_exact` and
`test_rc6_native_control_inventory_is_closed_and_exhaustive`](../../tests/test_protocol_conformance.py).
Generic Linux support elsewhere in the protocol does not extend this inventory.
In particular, section 4.1.1's separate script inventory already has Linux and
`host-conditional` controls. Its own text makes the inventories independent.

**Recommendation:** request a new build inventory version with a Linux table,
plus updated build evidence grammar/vectors admitting `linux`. For the two
aggregate controls, use an explicitly specified host-conditional model, similar
to the script inventory, with updated evidence consistency rules. Prefer a new
evidence record version if adding `host-conditional` to the record vocabulary;
old readers reject unknown records rather than misinterpret them. Exact version
names belong to the protocol owner. Preserve `manager-worker-v1` and existing
cache inputs unless that owner changes execution semantics; inventory expansion
alone is not a reason to change policy identity or force a new cache format.

Until that revision is accepted, csk must retain both Linux admission refusals.
Do not label Linux as macOS, append Linux privately to the v1 table, borrow a
script evidence record, or treat a successful toolchain probe as qualification.
A Linux table without an evidence-platform amendment is incomplete.

[`UNRESOLVED_QUESTIONS.md`](../../UNRESOLVED_QUESTIONS.md) currently records
source-lock directory agreement and future Go-family admission, not a Linux
build amendment. The latter Go-family exception is unrelated and supplies no
Linux permission. Keep this design as the Linux decision record; create the
protocol amendment record through the owner's normal flow before enabling it.

## 3. Native controls and truthful availability

These are proposed per-operation outcomes for an ordinary unprivileged account
with **no delegated cgroup subtree**. Every Linux entry is host-unverified.
`unavailable_reason` belongs to the normative inventory metadata; the current
build evidence entries have only name, availability, status, and probe timing.
Do not silently add a reason field to that closed record.

| Control | macOS mechanism at baseline | Windows mechanism at baseline | Proposed Linux outcome | Linux mechanism or exact unavailable reason |
| --- | --- | --- | --- | --- |
| `descendant-domain-termination` | available: process group/session | available: Job Object kill-on-close | `available`, subject to probe | `process-group-and-session-teardown`: `setsid`, `killpg`, direct-child `waitpid`, bounded group-absence verification |
| `active-process-count-limit` | unavailable: `no-private-aggregate-domain` | available: active-process Job limit | `unavailable` | `no-private-aggregate-domain`; proposed normative Linux cell is host-conditional |
| `aggregate-memory-limit` | unavailable: `no-private-aggregate-domain` | available: process/job memory limits | `unavailable` | `no-private-aggregate-domain`; proposed normative Linux cell is host-conditional |
| `per-file-size-limit` | available: `RLIMIT_FSIZE` | unavailable: `no-private-aggregate-domain` | `available`, subject to probe | `rlimit-fsize`: `getrlimit`/`setrlimit(RLIMIT_FSIZE)` and worker readback |
| `inherited-handle-restriction` | available: close-on-exec and release | available: explicit handle list | `available`, subject to probe | `close-on-exec-and-explicit-descriptor-release`: `O_CLOEXEC`, `close_fds`, exact `pass_fds`, explicit protocol-FD release |

Source: `_NATIVE_CONTROL_PLATFORMS`, `_probe_macos_control`,
`_probe_windows_control`, `_NativeControlDomain`, `_observe_native_controls`,
and `_release_protocol_descriptors`. The probes must exercise mechanisms once
per operation, including measured absence of aggregate facilities. A failed
probe or denied syscall is not evidence of a successful control. Loss of a
required mechanism rejects before worker/Go launch. An unavailable optional
aggregate control alone does not reject a portable operation.

### 3.1 Process lifetime and file limits

Use `Popen(start_new_session=True)` for the fixed worker. Verify both SID and
PGID equal its PID in the worker proof; Go inherits the group. At teardown,
signal only the operation's recorded group, wait for the direct worker, and
repeat bounded group teardown until absence is established. Never signal the
manager's group. Linux errno and zombie behavior need native tests; do not copy
Darwin's `_is_inconclusive_group_signal()` explanation as a Linux fact.
[`setsid(2)`](https://man7.org/linux/man-pages/man2/setsid.2.html) describes the
session mechanism, including inheritance.

A process group is the portable manager-owned domain, not containment against a
process deliberately calling `setsid()` or changing groups. The fixed graph
must not request detachment; detached hostile descendants require a stronger
domain. A retained pidfd could protect a direct-worker signal against PID reuse,
but does not name a process group or prove all descendants joined. Reaping
orphan grandchildren may require a scoped supervisor/subreaper design; do not
set a long-lived manager's subreaper role without considering unrelated children.
These are qualification gates, not facts proved by a unit mock.

`prctl(PR_SET_PDEATHSIG, SIGKILL)` can supplement manager-loss handling. It is
cleared on fork, refers to the creating parent thread, and does not kill a whole
descendant group. Set/check it in the earliest trusted worker bootstrap and
recheck the expected parent after registration to close the death-before-setup
race. Worker startup still precedes that registration. Do not claim Windows
kill-on-close crash semantics or use it to replace explicit teardown. Avoid
Python `preexec_fn` in a threaded manager.
See [the parent-death interface](https://man7.org/linux/man-pages/man2/PR_SET_PDEATHSIG.2const.html).

Apply the per-file limit before worker execution, then verify inheritance before
Go. `_launch_macos` temporarily lowers the manager soft limit around `Popen`
under `_FILE_LIMIT_LOCK`; Linux can reuse the lifecycle only after concurrency
and restoration tests. The lock protects cooperating launch paths, not all
manager threads doing writes. A child-only native spawn helper would add a
trusted executable and requires an explicit graph decision. Never lower the
manager hard limit irreversibly. Check failure restores its soft limit and
reaps any started worker. `RLIMIT_FSIZE` bounds file extension, not disk totals;
`RLIMIT_AS` is per-process address space and `RLIMIT_NPROC` counts tasks against
a real UID, not one private build. Neither backs an aggregate inventory entry.
See [`getrlimit(2)`](https://man7.org/linux/man-pages/man2/getrlimit.2.html).

### 3.2 cgroup v2 is conditional, not a default privilege

A mounted cgroup v2 hierarchy does not imply permission to create/configure an
operation domain. A delegated subtree needs controller availability, writable
membership and limit files, valid topology, and authority to move the worker.
Inside an owned child, `pids.max` counts tasks and `memory.max` bounds accounted
memory. Controller limits and ancestor restrictions need readback and a bounded
positive/negative fixture. No delegation means the exact reason
`no-private-aggregate-domain`; do not modify shared user/session cgroups.

If the amended inventory adopts conditional controls, probe each requested
controller separately. Verify attach-before-Go and descendants' inherited
membership; manager and unrelated processes stay outside. A trusted bootstrap
barrier can hold the worker until attachment, but does not make pre-exec
startup contained. Native `clone3(CLONE_INTO_CGROUP)` has a different integration
cost. `cgroup.kill` and `cgroup.events` can assist teardown when available;
neither a file's presence nor UID ownership guarantees safe authority. Do not
silently upgrade this into a hardened guarantee. See the kernel's
[cgroup v2 delegation and controller documentation](https://docs.kernel.org/admin-guide/cgroup-v2.html).

A fixed Linux `unavailable` table would contradict hosts where the probe finds a
private aggregate domain, just as `probe_native_controls()` currently rejects
an available measurement against an unavailable cell. Recommend resolving the
conditional grammar in the amendment, rather than configuring false negatives
or making delegation mandatory for every Linux install.

### 3.3 Descriptor capability and observation

Generalize `_PreparedWorkerLaunch._prepare_macos`, `add_popen_options`, and
`_consume_worker_launch_context` to an explicit POSIX branch. Keep the inherited
anonymous-pipe secret out of argv/environment and package inputs. Preserve
fixed-FD collision fallback, bounded launch-record parsing, single consumption,
parent-PID binding, request/ready authenticators, and closing the parent's copy.
Use `pipe2(O_CLOEXEC)` where exposed, retaining only the intentional launch FD
across exec. After consumption it must not reach Go. Clear stdin and release
protocol streams before each fixed Go invocation.

`close_range` can close or mark descriptor ranges close-on-exec, with
`CLOSE_RANGE_UNSHARE` addressing shared tables. It is an optimization behind a
behavioral fallback, not a required kernel-version label. Account for the one
launch capability and subprocess error pipe; blindly closing every FD can
break authentication or startup diagnostics. Verify actual exec inheritance
with an intentional unrelated inheritable FD and a valid launch control.
See [`close_range(2)`](https://man7.org/linux/man-pages/man2/close_range.2.html).

## 4. Linux interpreter identity must follow ELF evidence

The pre-launch identity must decide the expected process and Python runtime
images without executing an unbound interpreter or trusting a directory glob.
The worker independently proves what actually loaded; agreement of two path
heuristics is insufficient. Preserve `_InterpreterIdentity` link-chain bounds,
`_ExecutableIdentity` hashes/device/inode/mode/single-link rules, configuration
binding, complete startup/package/runtime trees, and post-exec verification.

### 4.1 Resolve from captured bytes

Introduce a bounded ELF parser over the same captured bytes whose identity was
verified. Parse ELF class, endianness, machine/type, program headers, `PT_LOAD`,
`PT_INTERP`, `PT_DYNAMIC`, `DT_NEEDED`, string-table bounds, `DT_RPATH` and
`DT_RUNPATH`. Check every offset/count/size, virtual-address-to-file mapping,
string termination, duplicate/conflicting declaration and total bound before
use. Program headers matter even when section headers are stripped. Never run
`ldd` on an untrusted candidate or use its output as identity evidence.
See [`elf(5)`](https://man7.org/linux/man-pages/man5/elf.5.html).

The implementation needs an explicit platform field, not
`_interpreter_runtime_platform()`'s current `.dll`-versus-macOS inference.
A PIE executable can be `ET_DYN`; `PT_INTERP` means a dynamic loader, not
necessarily a separately linked Python runtime. Distinguish:

1. **Embedded Python runtime:** ELF evidence establishes that Python belongs to
   the executable, with no ambiguous external Python dependency. Expected
   `runtime_image == process_image == canonical executable`. A neighboring
   `libpython*.so` is irrelevant. Absence of a filename alone does not prove
   embedding: validate the supported layout and require the worker's actual
   `Py_GetVersion` address to belong to the executable's loaded segment.
2. **External Python runtime:** one supported libpython dependency is selected
   by ELF linkage and deterministic loader resolution. Bind the SONAME link
   chain and canonical library bytes. A static archive `.a` is never a loaded
   runtime. Ambiguous versions/ABI tags or an unresolved/transitive Python
   provider cause identity refusal, not fallback to the executable.

For glibc, model the *applicable* `DT_RPATH`/`DT_RUNPATH` semantics, dependency
scope, `$ORIGIN`, cache, and default directories under the worker's fixed
loader-free environment. `RUNPATH` is not inherited transitively like `RPATH`.
Reject unsupported relative/search tokens and escaping or ambiguous candidates.
A distribution library may legitimately live outside the executable prefix;
an exact dependency-bound absolute library is not a prefix escape by itself.
Bind any loader configuration/cache actually consulted, its absence slots, and
resolved links to the startup identity and mutation guard. Account explicitly
for `/etc/ld.so.preload`; clearing `LD_PRELOAD` does not disable it. Initially
refuse nonempty system preload configuration and unsupported search layouts.
Do not treat `ctypes.util.find_library`, `sysconfig`, `PATH`, or the current
manager's mappings as authority for a potentially different worker invocation.
See [the loader's resolution rules](https://man7.org/linux/man-pages/man8/ld.so.8.html).

This is a deliberately bounded resolver, not a promise to emulate arbitrary
loaders. Recommend glibc x86-64 and AArch64 first; musl and unusual ABI layouts
remain refused until separately qualified. Also identify/bind `PT_INTERP` and
its configuration as Linux startup inputs. This expands the current explicit
Python-image closure; it does not claim to hash every system DSO. Document
that trust boundary and resolve any newly discovered mutable loader input
before accepting a layout.

### 4.2 Layout rules

All rows are expected Linux shapes, **unverified on the target host**.
Installation-tool names are hints, never an identity verdict.

| Layout | Expected process/runtime decision | Required additional binding or refusal |
| --- | --- | --- |
| Distribution `/usr/bin/python3` | Follow bounded versioned symlinks. ELF determines embedded Python versus one external `libpython3.X.so...`; some distributions embed Python even with a shared library installed. | Library SONAME may resolve into an architecture-specific system directory. Derive stdlib independently; do not infer `/usr/lib` from the library's directory. |
| python-build-standalone via uv or mise, embedded Python | Canonical executable is both images, even if shared libpython is shipped alongside it. | Prove embedding and loaded symbol ownership. An embedded Python runtime may still depend on libc/other DSOs. |
| python-build-standalone via uv or mise, shared Python | ELF dependency and validated origin-relative search select one canonical libpython. | Bind relocatable links and base home; moving/replacing them during execution rejects. |
| pyenv | Follow the installed manager's fixed interpreter, not the shell shim. Default and `--enable-shared` builds can differ. | Resolve from ELF; do not invoke `pyenv which` or inherit `LD_LIBRARY_PATH`. Refuse a build that requires an unbound loader environment. |
| venv with symlinked interpreter | Preserve invocation path/link identity and bind the physical base executable/runtime. | Bind `pyvenv.cfg`, base home, venv package tree, base stdlib and archive slots. |
| venv with copied interpreter | Copy is the process image if launched directly; external runtime follows its own ELF search. | Bind config and base home independently. Refuse an origin-relative copied layout whose dependency cannot be resolved; never silently substitute a different interpreter. |

[Standalone distribution metadata](https://gregoryszorc.com/docs/python-build-standalone/main/distributions.html)
describes multiple build forms; it is supporting evidence, not a replacement
for executable bytes. [Python's venv contract](https://docs.python.org/3/library/venv.html)
allows copies and symlinks and records a base `home` in `pyvenv.cfg`.
Version-dependent `-S`/prefix behavior must be tested across supported Python
versions. System installs can split `site-packages`/`dist-packages`, `lib`/`lib64`
and architecture directories; `_manager_stdlib_root()` cannot infer the base
stdlib version from the installed package parent for all of them. Preserve
closed imports and exclude unrelated site trees; refuse unresolved layouts.

### 4.3 Worker proof

Use `/proc/self/exe` to identify the executed image, rejecting missing procfs,
nonabsolute results, and deleted images rather than stripping ` (deleted)`.
Compare its opened device/inode and hash with the retained manager identity,
not only the pathname. See [`proc_pid_exe(5)`](https://man7.org/linux/man-pages/man5/proc_pid_exe.5.html).

Locate `ctypes.pythonapi.Py_GetVersion` in a loaded `PT_LOAD` segment using
`dl_iterate_phdr`, with bounded callbacks and ranges; `dladdr` can corroborate.
Handle the main executable's empty loader name via `/proc/self/exe`. Resolve
exactly one symbol-owning image and compare it with the pre-launch runtime;
loading a second irrelevant libpython does not change that owner. A bounded
`/proc/self/maps` check can correlate device/inode and segment identity, but
pathname parsing alone must not select the runtime. Reject deleted, anonymous,
ambiguous, substituted, or inaccessible image evidence. See
[`dl_iterate_phdr(3)`](https://man7.org/linux/man-pages/man3/dl_iterate_phdr.3.html).

Keep `validate_worker_runtime()`'s nonce/launch binding, exact flags, allowed
`sys.path`, and complete loaded-module checks. Do not weaken them to accept an
unexpected Linux module. Failure to obtain native proof rejects before Go.

## 5. Mutation guard is a separate qualification gate

At baseline `_IdentityMutationGuard._setup_macos()` retains vnode descriptors
and kqueue notifications for file/directory changes; `verify()` and `close()`
reject observed mutation, including replacement followed by restoration.
Windows `_setup_windows()` retains `CreateFileW` handles with read sharing only,
preventing conflicting write/delete opens while held. Descriptor exhaustion,
setup, observation, release and limit-restoration failures fail closed.
Neither mechanism is equivalent to merely hashing paths at the end.

Linux path retention can use `openat2` relative to a retained directory, with
`RESOLVE_BENEATH`, `RESOLVE_NO_SYMLINKS`, and `RESOLVE_NO_MAGICLINKS`, after
explicitly resolving/binding permitted interpreter/library symlinks. Use
`O_PATH|O_CLOEXEC` for inode references and separately opened read descriptors
for hashing; `O_PATH` alone cannot read bytes. It neither locks writes nor
prevents replacement of the pathname. On older kernels, a component-wise
`openat(O_NOFOLLOW)` walk requires its own race tests; no silent string-path
fallback. See [`openat2(2)`](https://man7.org/linux/man-pages/man2/openat2.2.html)
and [`open(2)`](https://man7.org/linux/man-pages/man2/open.2.html).

An `inotify_init1(IN_CLOEXEC|IN_NONBLOCK)` monitor can watch retained files and
parent directories for modify/attribute/create/delete/move/self-delete events.
Install watches before accepting the captured identity, then rehash and drain
events to close registration races. Treat `IN_Q_OVERFLOW`, watch loss,
`IN_IGNORED`, unmount, queue errors and capacity exhaustion as failure. Watch
absent import/archive/configuration slots through their parents. Retain the
existing identity-count bound, validate `RLIMIT_NOFILE`, inotify watch limits,
and actual opened inode, and close every descriptor on every exit.

**An inotify-only solution is not yet an equivalent guard.** Its documented
blind spots include writes through `mmap` and remote filesystem mutations;
`O_PATH` and post-run hashing do not fix transient modify-and-restore. Advisory
`flock` does not exclude a noncooperating writer. File leases depend on
ownership/capability and filesystem support, and do not provide a general
unprivileged lock over a distribution-owned runtime tree. See
[`inotify(7)` limitations](https://man7.org/linux/man-pages/man7/inotify.7.html).

Recommendation: implement bounded retention/observation as an internal slice,
but keep admission closed until native ordinary-write, rename/restore,
hardlink, existing-mapping write/restore, and filesystem tests establish the
claimed contract. If transient mapped writes cannot be covered, require an
identity closure with verified enforced immutability or redesign it around
manager-owned immutable snapshots, then review path/import semantics. Otherwise
escalate that precise guarantee decision; do not silently weaken mutation
detection to a final hash or publish Linux support with the gap hidden. This
is a possible architecture blocker, not an optional aggregate-control absence.

## 6. Complete implementation map

All proposed Linux mechanisms in this map remain host-unverified. Shared paths
still need Linux integration tests even when their source has no OS branch.

| Existing boundary / code citation | Current proof or enforcement and purpose | Required Linux work |
| --- | --- | --- |
| `inventory_platform`, `_NATIVE_CONTROL_PLATFORMS`, `probe_native_controls`, `_validate_probe_set`, `validate_capability_evidence` | Closed inventory; measured per-operation availability; reject missing, duplicate, contradictory, unknown, or hardened entries. | Add only the accepted protocol table/version and evidence grammar; conditional outcomes need an explicit schema change. |
| `_PreparedWorkerLaunch`, `_read_posix_worker_launch_record`, `_launch_authenticator` | Anonymous inherited capability authenticates the manager-owned child; prevents arbitrary hidden-worker entry. Windows uses a restricted inheritable handle. | Explicit Linux/POSIX dispatch, FD-capacity and collision probes; preserve no-secret argv/env and consume-before-Go rules. |
| `_resolve_interpreter_identity`, `_resolve_interpreter_runtime`, `_manager_interpreter_path` | Bind launcher, bounded symlink chain, interpreter and native runtime. Windows binds venv base/DLL; baseline macOS uses framework/dylib layout selection. | Captured-byte ELF linkage resolver and independent runtime proof, no glob-only decision. Keep absolute shebang, no shell wrappers. |
| `_resolve_manager_identity`, `_resolve_worker_manager_identity`, `_manager_stdlib_root`, `_manager_runtime_roots`, `_runtime_archive_slots`, `_resolve_startup_identity` | Close installed package, mutable startup/config, stdlib, extension modules, archives and absent slots; avoid startup/import injection. | Explicit Linux layout/base prefix, dist-packages and lib64 handling, loader inputs, bound venv configuration, correct archive slots. |
| `_IdentityMutationGuard`, `_ensure_macos_identity_descriptor_capacity` | Observe mutation on macOS; deny conflicting opens on Windows; retain complete identities until teardown. | Section 5 gate, bounded retention/watches, registration race closure, descriptor-limit restoration; no claim based solely on O_PATH. |
| `_NativeControlDomain.launch`, `_launch_macos`, `_attach_and_resume_windows` | Install session/file limit or suspended Job before worker execution; return only a controlled process. | Session/limit installation with verified rollback; resolve parent-death and optional cgroup attachment timing without unbound helper/preexec code. |
| `_indispensable_worker_environment`, `worker_argv` | Fixed `-S -s -B -P`, manager-derived Python roots/cache; Windows-only startup variables handled explicitly. | Linux uses private `HOME`; exclude `LD_*`, operator Python variables and Windows values. Validate actual base/venv prefix under fixed Python home. |
| `_worker_native_runtime_proof`, `worker_runtime_proof`, `validate_worker_runtime` | Actual native images, exact flags/import roots/module inventory and launch context precede permission to compile. | procfs plus symbol-owning loaded ELF segment; preserve complete proof and diagnostics. |
| `_observe_native_controls`, `_release_protocol_descriptors` | Worker confirms applied native controls and drops protocol handles before Go. | Verify SID/PGID, inherited file limit and FD release; read back any accepted conditional controller. No evidence from probe success alone. |
| `SubprocessProcessExecutor`, `_WorkerClient`, `_WorkerSession`, `run_worker` | Fixed Go executable/argv, framed bounded protocol, one list then authenticated permit then one build; bound output/time and teardown. | Preserve identical protocol and fresh nonces; validate Linux signal/EOF/cancellation/start-failure paths. |
| `_NativeControlDomain.terminate`, `_terminate_windows_job`, `_dominant_failure` | Kill/join complete worker domain before publication; retain identity/protocol failure priority over cleanup errors. | Linux-specific group absence/zombie/reaping behavior, ownership/reuse protection, bounded cleanup and error precedence. |
| `_validate_worker_environment`, `_normalize_limits`, `_verify_private_state` | Exact native target/tuning and offline/private environment; artifact/storage/deadline/output limits independent of optional controls. | Replace darwin-versus-windows GOOS ternary with closed mapping including linux; preserve fixed limits, private roots and refusal of extra env. |
| `_verify_artifact`, `_native_executable_header` | One regular single-link output, bounded hash/size, permissions, native-format check; never execute artifact. | Add explicit ELF artifact branch; reject PE/Mach-O and truncated/non-native fixtures. Do not reuse the interpreter parser without a format-policy decision. |
| [`toolchain._native_host`, `_native_executable_header`](../../src/csk/builds/toolchain.py), [`planner`](../../src/csk/builds/planner.py) | Already recognize native Linux target and ELF for toolchain selection/fingerprint and private plan. | Reuse Linux target/tuning and architecture-tool directory rules; test actual executable bytes and interpreter/Go layouts, not a support flag alone. |
| [`installer.supports_external_builds`](../../src/csk/installer.py), [`build_repository_pipeline`](../../src/csk/build_repository_pipeline.py), [`build cache`](../../src/csk/builds/cache.py) | Early platform admission; locked acquisition/frozen inputs; POSIX ownership/modes and atomic publication/currentness. | Enable last, after native qualification; retain refusal before credentials until then. Verify cancellation, dry-run, receipt/currentness, repair, rollback, GC and no partial publication. |
| [`CI workflow`](../../.github/workflows/ci.yml), [`protocol tests`](../../tests/test_protocol_conformance.py), [`draft-source tests`](../../tests/test_draft_sources_conformance.py) | Ubuntu lanes currently prove intentional source-build refusal; observed-entry membership follows the product predicate. | Keep refusal controls while gated; later require native positive builds and negative neighbors, updating expected lane/membership from real execution. |

The native artifact check currently only inspects a short magic. Recommend
preserving its existing scope while explicitly adding Linux magic, with a
separate decision on architecture/type validation. The interpreter identity
parser must be stricter because it selects executable trusted inputs.

## 7. Review-sized implementation slices

The order below keeps public admission closed until every prerequisite is
reviewed. Every new test needs a concise claim comment describing its actual
inputs/path and limits. Each negative has a valid nearby positive control and
asserts the intended diagnostic, child count, cleanup and publication state.

| Order / slice | Positive claims | Negative claims and control plants | Where evidence can run |
| --- | --- | --- | --- |
| 1. Protocol amendment and admission sentinels | Accepted Linux table/evidence examples parse; existing platform examples remain exact. | Old version plus linux, extra/missing/duplicate controls, false applied claims, unprobed outcomes and hardened names reject. Existing Linux refusal still starts zero workers/Go and publishes nothing. Plant a false applied value to show the checker rejects that property. | Data/adapter tests on macOS and Linux; normative corpus acceptance belongs to protocol review. |
| 2. Bounded ELF identity parser/resolver | Embedded, shared, PIE, stripped executable, SONAME symlink, approved origin-relative and distribution-library fixtures select exact expected images. | Truncated/overflowing headers, invalid class/endian/machine, unmapped string table, unterminated names, duplicate Python dependencies, escape/ambiguous search, forged static inference, wrong ABI and loader config mutation reject with identity diagnostic. Include valid embedded executable beside irrelevant shared library. | Captured-byte and fake-tree fixtures on macOS; real glibc layout/loader agreement on Linux. Never mark fixture success as loader qualification. |
| 3. Linux startup closure and loaded-image proof | Installed distribution, standalone embedded/shared, pyenv and copied/symlink venv identities agree with symbol-owning loaded image under fixed flags/environment. | Replaced executable/library/config, unexpected LD/Python values, injected archive/module/root, absent procfs, deleted/mapped substitute or wrong image reject before Go. | Layout fixtures on macOS; actual procfs/libc/Python version matrix on Linux. |
| 4. Mutation retention and native control adapter | Unchanged bounded identity closure stays silent; session/file limit/descriptor controls are applied and observed; no-delegation aggregate outcomes are truthful. | Ordinary write and rename/restore plants must trigger the guard; mapped write/restore plant must either trigger it or block the design. Inject overflow/watch loss/FD exhaustion and setup/restore failures; assert no worker or publication. Show unrelated UID tasks do not become a claimed private domain. | Parsers, event-state/rollback fixtures on macOS; real Linux filesystems, limits and inheritance required for acceptance. Conditional cgroup tests only inside explicitly delegated isolated fixtures. |
| 5. Authenticated launch and teardown | Correct capability, nonce, parent, SID/PGID and release let fixed Go list/build complete; normal exit leaves no owned child. | Missing/forged/replayed capability, FD collisions, wrong parent, child-start failure, truncated frames, bad permit, second list/build, timeout, output overflow, cancellation and fork-near-teardown leave no publishable result. Exercise group absence and zombies with recorded fixture PIDs only. | Message/rollback tests on macOS; real Linux exec, FD, process group, signal and reaping tests. |
| 6. Integrate native plan/artifact and external lifecycle | Native Linux GOOS/GOARCH/tuning, vendored build and one ELF artifact pass; cold install and verified reuse/currentness work. | Cross-target/extra env, unvendored dependency, generator/cgo/replacement escapes, PE/Mach-O/symlink/hardlinked/oversize artifact, source/toolchain swap and interrupted activation reject without artifact execution or partial publication. | Plan/format/refusal fixtures on macOS; real Go family/architecture and end-to-end Linux lifecycle. |
| 7. Qualify and enable last | Native Linux installed-manager runs, full suite and corpus/lifecycle evidence at exact signed head, then product predicate and docs agree. | Unqualified libc/architecture/layout and unsupported kernel capabilities remain closed. After enablement, require an actual Linux build lane so an all-refusing implementation cannot pass; retain macOS/Windows regressions. | Linux x86-64 and AArch64 native jobs, existing macOS/Windows jobs; cross-builds alone do not qualify a host. |

Do not merge an admission change ahead of the mutation guard or protocol gates.
Native test fixtures own and reap their children; no shared-user resource stress,
shared cgroup writes, services, or production-runtime mutation is required.
The current document is non-executable: validate code-reference accuracy,
protocol counterexamples and document links, plus the required regression run;
no product-test additions are justified just to test prose.

## 8. Pending exact probes and evidence log

No command in this section has run on Linux. Run only after access is restored,
in an owned disposable run directory, with all paths and logs under the
approved workspace. Record each exit code; optional tool absence is a limit,
not a pass. Never collect environment dumps or credentials.

### 8.1 Read-only host and runtime facts

```sh
uname -srm
cat /etc/os-release
getconf GNU_LIBC_VERSION
python3 --version
git --version
go version
uv --version
ulimit -Sn
ulimit -Hn
cat /proc/self/cgroup
cat /proc/self/mountinfo
python3 - <<'PY'
import ctypes, json, os, platform, resource, sys
from pathlib import Path
libc = ctypes.CDLL(None, use_errno=True)
print(json.dumps({
    'architecture': platform.machine(), 'python': sys.version,
    'executable': sys.executable, 'prefix': sys.prefix,
    'base_prefix': sys.base_prefix, 'proc_exe': os.readlink('/proc/self/exe'),
    'rlimits': {n: resource.getrlimit(getattr(resource, n)) for n in
                ('RLIMIT_NOFILE', 'RLIMIT_FSIZE', 'RLIMIT_NPROC', 'RLIMIT_AS')},
    'libc_symbols': {n: hasattr(libc, n) for n in
                     ('prctl', 'dladdr', 'dl_iterate_phdr', 'close_range',
                      'inotify_init1', 'inotify_add_watch')},
}, sort_keys=True))
for name in ('max_user_watches', 'max_user_instances', 'max_queued_events'):
    p = Path('/proc/sys/fs/inotify') / name
    print(str(p), p.read_text().strip())
for line in Path('/proc/self/maps').read_text().splitlines():
    if 'libpython' in line or '/python' in line:
        print(line)
PY
```

Do not infer `openat2` absence from the lack of a libc wrapper. A syscall probe
must use architecture-appropriate numbers from system headers in a disposable
fixture; distinguish `ENOSYS`, seccomp denial and invalid arguments. Capture
`readelf --version`; if available, inspect each approved interpreter with:

```sh
readelf -h -l -d -- "$PYTHON_IMAGE"
```

Set `PYTHON_IMAGE` to that fixture's canonical executable. Compare with the
symbol-address proof below; repeat under fixed `-S -s -B -P` launch for each
approved installed layout. No unbound executable is run to discover identity.

```sh
python3 - <<'PY'
import ctypes, os
class DlInfo(ctypes.Structure):
    _fields_ = [('name', ctypes.c_char_p), ('base', ctypes.c_void_p),
                ('symbol', ctypes.c_char_p), ('address', ctypes.c_void_p)]
libc = ctypes.CDLL(None, use_errno=True)
lookup = libc.dladdr
lookup.argtypes = [ctypes.c_void_p, ctypes.POINTER(DlInfo)]
lookup.restype = ctypes.c_int
info = DlInfo()
address = ctypes.cast(ctypes.pythonapi.Py_GetVersion, ctypes.c_void_p)
assert lookup(address, ctypes.byref(info)) and info.name
print('exe', os.readlink('/proc/self/exe'))
print('runtime', os.fsdecode(info.name), 'address', hex(address.value))
PY
```

This corroborates a proposed resolver; it is not itself a pre-launch resolver
or the full `dl_iterate_phdr` segment proof.

### 8.2 Isolated file-limit and FD controls

Run from an owned scratch directory; only the disposable child changes its
limit. This also demonstrates actual FD closure across exec. The 2-byte case
is the valid control, and the third-byte write must fail specifically.

```sh
python3 - <<'PY'
import os, subprocess, sys
from pathlib import Path
p = Path('limit-probe.bin')
assert not p.exists()
r, w = os.pipe()
try:
    os.set_inheritable(r, True)
    code = r'''
import errno, os, resource, signal, sys
from pathlib import Path
try:
    os.fstat(int(sys.argv[1]))
except OSError as e:
    assert e.errno == errno.EBADF
else:
    raise AssertionError('unrelated FD inherited')
resource.setrlimit(resource.RLIMIT_FSIZE, (2, 2))
signal.signal(signal.SIGXFSZ, signal.SIG_IGN)
p = Path('limit-probe.bin')
with p.open('xb', buffering=0) as f:
    assert f.write(b'ok') == 2
    try:
        f.write(b'!')
    except OSError as e:
        assert e.errno == errno.EFBIG
    else:
        raise AssertionError('file limit not enforced')
assert p.read_bytes() == b'ok'
'''
    subprocess.run([sys.executable, '-S', '-s', '-B', '-P', '-c', code, str(r)],
                   close_fds=True, check=True, timeout=10)
finally:
    os.close(r)
    os.close(w)
    if p.exists():
        p.unlink()
PY
```

### 8.3 Delegation and unresolved behavioral probes

Read the cgroup2 mount and relative membership from the facts above. For the
resolved current subtree, inspect `cgroup.controllers`,
`cgroup.subtree_control`, `cgroup.procs`, `cgroup.events`, `pids.max`, and
`memory.max` where present. Record ownership/modes and effective access. Do
not create a sibling or move a process merely because these files exist.
Only an explicitly delegated owned fixture may perform create/set/readback,
blocked-child attachment, bounded task/memory positive-negative controls,
`cgroup.kill` where supported, empty-domain verification and removal.

After the implementation fixtures in section 7 exist, the exact native suite
entry points should be agreed in their reviews; this document does not invent
commands for nonexistent tests. Required probes still missing are:

- `dl_iterate_phdr` segment ownership versus embedded and shared `Py_GetVersion`,
  including PIE, copied venv, and deleted/substituted images;
- ordinary and pre-existing writable-mapping modify/restore, directory rename,
  watch-overflow/loss and descriptor-capacity controls in a private identity tree;
- fixed-capability collision and unrelated-FD noninheritance through the actual
  worker and Go graph, not just the standalone subprocess control above;
- repeated group kill/join with late fork and zombie fixtures, and separately
  manager loss, parent-death registration race, and child detachment limits.

For every native run, record source head, OS/kernel/architecture/libc/filesystem,
Python/Git/Go/uv/csk versions, protocol pin, exact command and exit code, verdict,
cleanup evidence, and any missing coverage. Local fixture results cannot fill
these native evidence slots. The failed access attempt supplied no Linux
measurements; no run directory was created remotely.

## 9. Owner decisions and recommendations

| Decision or risk | Recommendation / acceptance gate |
| --- | --- |
| Protocol lacks Linux build inventory and evidence platform | Amend both, version the inventory, and qualify corpus vectors before any product admission. Keep policy/cache identity stable absent a semantic protocol change. |
| cgroup availability varies within Linux | Adopt explicit conditional grammar; report `no-private-aggregate-domain` on measured absence. Do not use per-UID/per-process rlimits as aggregate controls or require system administration for the portable baseline. |
| Mutation guard cannot yet match transient mapped-write detection | Treat as a blocker to enabling support. Prove an equivalent supported identity closure or choose verified immutability/snapshot redesign; never hide a known blind spot. |
| Arbitrary ELF loader/layout support is too broad | Start with bounded, deterministic glibc x86-64/AArch64 layouts and exact supported Python versions. Refuse unsupported loader/configuration/ABI forms with identity diagnostics. |
| Manager death versus normal teardown | Qualify bounded normal teardown first. Parent-death signals are supplementary; decide whether crash-complete domain death is a new requirement needing a stronger domain/supervisor. |
| File-limit inheritance changes manager-global state | Reuse the existing locked lifecycle only with restoration/concurrency evidence; evaluate helper graph costs before introducing one. |
| Native availability and CI expectations are unmeasured | Keep every Linux claim unverified until native evidence exists; run real accepted builds plus refusal controls on Linux and preserve other-platform regressions. |
| Real runtime matrix may expose additional mutable startup inputs | Expand the bound identity closure or refuse the layout. Do not add allowlists of unexpected proof modules just to make tests pass. |

This proposal is ready for protocol and architecture decisions; it is not an
implementation-complete or platform-qualified claim.
