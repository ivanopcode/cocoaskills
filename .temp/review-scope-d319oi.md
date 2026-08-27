# Review time budget (mandatory)

Two prior reviewer runs died at the 20m timeout. Hard rules:

1. Do NOT run the full pytest suite. The orchestrator ran it on this
   exact tree: 1797 passed, 245 skipped, 444.91s. Trust it or run ONLY
   the four pin-related test files (they finish in seconds).
2. Do NOT clone curator-spec. A checkout already exists at
   /Users/iv/Developer/ReluxWorks/curator-spec, currently at b8b03d5
   (= v1.0.0-rc.10). Read corpus facts from there.
3. Verify the delta by reading: git diff of the six changed files, the
   rc.10 corpus files you need, and the outcome resource. Then hand off
   with one verdict immediately. Budget your run to finish in 25
   minutes.
