# Reviewer execution note (mandatory)

Two prior reviewer runs on this task ended without a verdict: one exited
with code 1, one ended its turn "waiting for a monitor" and parked the
task in reviewing. Do NOT start monitors, background waits, or polling
loops. Complete the review in one pass: read the diff, verify the two
prior blocking findings (W1: external build repo qualified as one of two
forms at README.md:146; W2: показывает instead of считывает), run your
checks synchronously, then IMMEDIATELY hand off with exactly one verdict
branch: accepted (done) or changes requested (to-dev) with a verdict
resource. Ending your run while the task sits in reviewing is a failed
review.
