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

The POSIX worker samples its authenticated parent PID and protocol-pipe hangup.
A changed parent PID triggers group cleanup on the next scheduled watch iteration.
Pipe hangup alone starts a five-second grace, because HUP can accompany an
orderly close while a frame is still queued or buffered. The watch does not read
protocol bytes and rechecks its stop event before signaling. The manager keeps
stdin open through orderly worker exit, requires exit status zero on the POSIX
path, then completes domain teardown and closes stdin. Linux observes orderly
exit with WNOWAIT so the child still pins its PID until domain teardown.

This is cooperative cleanup: a suspended or independently killed worker cannot
run the watcher, and there is no scheduler-independent latency guarantee. A
reader taking longer than the pipe-close grace can still be killed; arbitrary
stalls are not orderly-shutdown success evidence. A descendant that creates a
new session escapes the group. No PDEATHSIG is installed: a kernel SIGKILL can
destroy the watcher before it cleans up the group.

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
exec was not measured. The implemented watcher is covered by native POSIX crash regression tests.
After the shutdown correction, a real native Go compiler held in flight by
bounded diagnostic-pipe backpressure reproduced the baseline/EOF contrast on
macOS and Linux x86-64: baseline left the compiler live, the watcher killed the
worker and compiler, and a setsid descendant survived. These are one-run control
measurements, not complete installed-manager Go builds.

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
The fixture atomically publishes readiness/PID records and treats zombie status
as dead on both macOS and Linux. It uses a scoped Linux subreaper only in its own
test process and reaps its descendants; macOS orphan reaping remains OS-owned.
Its startup allowance is 90 seconds to accommodate imports under host load;
this does not change any production deadline. It verifies all three processes
are live before manager SIGKILL and observes EOF cleanup for up to ten seconds
rather than asserting a scheduler-independent half-second response. It records
startup and observation durations. An outer fixture timeout requests TERM cleanup
and waits for the fixture to reap its children instead of killing their owner. It writes only under pytest's temporary directory. No cgroup,
shared UID limit, privilege, or system configuration is changed. The baseline
crash case is a control plant: without the watcher, the ordinary descendant must
survive. The file-limit noop plant must prevent worker creation. An inheritable-allocation
plant must fail the descriptor probe without the probe repairing it. Aggregate
unavailability is an adapter policy (no private aggregate domain is acquired),
not a measurement that the host has no such facility.

Orderly-watch tests cover queued input with an open writer, a closed writer with
a reader doing one second of artificial work, and a reader blocked awaiting the
frame. Each case repeats three times per file run and requires the full frame
and exit zero. A stop-during-observation plant suppresses signaling; a shortened
grace control still kills a permanently busy worker after channel loss. Startup
refusal tests require a framed diagnostic and status 3. These bounded controls
do not prove successful shutdown under arbitrary scheduling delay.

Remaining qualification: full identity-authenticated list/build; fork-near-teardown
cleanup; worker-loss and scheduler-stall bounds; all restoration-error precedence
cases; privileged-exec probes; native Windows crash execution;
full installed-manager publication crash matrix. GOOS mapping is left to the
plan/artifact change.
