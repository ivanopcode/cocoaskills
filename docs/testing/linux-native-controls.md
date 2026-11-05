# Internal Linux native controls

Linux public build admission remains closed. The internal adapter exercises the
five native-control cells: session/group teardown, per-file size limit,
close-on-exec with explicit descriptor release, and two unavailable aggregate
controls (`no-private-aggregate-domain`). It does not adopt a protocol pin,
resolve ELF identities, implement mutation retention, or qualify complete builds.

The anonymous launch capability temporarily raises only the soft descriptor
limit when needed, verifies restoration, atomically allocates unoccupied fixed
slots with `F_DUPFD_CLOEXEC`, and closes the parent copy. The parent descriptor
stays close-on-exec; only the explicit `pass_fds` list admits it to the child. The worker consumes the capability once. Its observations
check both session and group leadership and inherited file size limit. The file
limit launch path verifies installation and restoration. These temporary limits
are process-wide; locks serialize cooperating launch paths, not unrelated writes
or descriptor allocation by other manager threads.

Linux teardown signals while the unreaped direct child anchors the numerical
group ID, then reaps it and only observes group absence. It never sends a
termination signal after reaping. A pre-reaped leader with a still-present group
rejects rather than risking a signal to a reused group ID. A remaining group,
including unreaped zombies or a late fork, cannot produce successful teardown.
This conservative implementation does not guarantee cleanup of that remaining
group; stronger late-fork cleanup is still required before qualification.

## Manager death

The POSIX worker watches protocol-pipe hangup without consuming protocol bytes,
and checks its authenticated parent PID. On loss it kills its own group. This is
cooperative cleanup: a suspended or independently killed worker cannot run the
watcher. It does not prevent a descendant from creating another session. No
PDEATHSIG is installed: a kernel SIGKILL can destroy the watcher before it cleans
up the group.

Native synthetic-build measurements (one run per comparison) used real manager
SIGKILL, the existing session/file-limit launch path, and one ordinary plus one
`setsid()` descendant. These are control measurements, not complete Go build or
identity qualification.

| Host / mechanism | Worker remains live | Group descendant remains live | Detached descendant remains live |
| --- | --- | --- | --- |
| macOS baseline | yes | yes | yes |
| macOS EOF watcher | no | no | yes |
| Linux baseline | yes | yes | yes |
| Linux PDEATHSIG | no | yes | yes |
| Linux EOF watcher | no | no | yes |
| Linux PDEATHSIG plus EOF watcher | no | yes | yes |

The combination result is scheduling-dependent and establishes a counterexample
to a stronger guarantee, not a universal outcome. Linux PDEATHSIG additionally
refers to the creating parent thread, is cleared in a forked child, and can reset
on privileged exec. A separate Linux probe confirmed that creator-thread exit killed the worker
while the manager process was still alive; both descendants survived. Privileged
exec was not measured. The implemented watcher was measured separately on Linux and is covered
by native POSIX crash regression tests.

Windows code creates a noninheritable Job, sets kill-on-close for descendant
termination, assigns the suspended worker, then resumes it. Last Job-handle close
therefore requests kernel termination of assigned processes. This is code review,
not native Windows execution. Death before assignment can leave a suspended
unassigned child; no cross-platform promise covers every instruction of launch.
There is no measured equal-strength kernel crash guarantee on all platforms.

## Publication and reproducibility

`test_sigkill_before_publication_never_exposes_partial_artifact` kills the actual
POSIX cache publisher immediately before its atomic rename. The visible entry is
absent, lookup reports MISS, and a subsequent valid publication reports HIT.
This proves the tested process-death boundary, not filesystem power-loss durability
or every crash point in an installed-manager build. Private staging may remain
following manager death; it is not a published artifact.

Run `python -m pytest tests/test_linux_native_controls.py -q` on native Linux.
The fixture uses a scoped Linux subreaper only in its own test process and reaps
its descendants. It writes only under pytest's temporary directory. No cgroup,
shared UID limit, privilege, or system configuration is changed. The baseline
crash case is a control plant: without the watcher, the ordinary descendant must
survive. The file-limit noop plant must prevent worker creation.

Remaining qualification: full identity-authenticated list/build; fork-near-teardown
cleanup; worker-loss and scheduler-stall bounds; all restoration-error precedence
cases; privileged-exec probes; native Windows crash execution;
full installed-manager publication crash matrix. GOOS mapping is left to the
plan/artifact change.
