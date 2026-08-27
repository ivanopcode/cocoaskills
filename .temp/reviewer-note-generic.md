# Reviewer execution note (mandatory)

Prior reviewer runs on this board have parked tasks in `reviewing`
without a verdict (one exited waiting for a monitor). Do NOT start
monitors, background waits, or polling loops. Complete the review in one
pass: read the diff and the task AC, run your checks synchronously, then
IMMEDIATELY hand off with exactly one verdict branch: accepted (done) or
changes requested (to-dev) with a verdict resource attached. Ending your
run while the task sits in `reviewing` is a failed review.
