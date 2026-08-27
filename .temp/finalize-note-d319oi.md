# Finalization run (previous run timed out after completing the work)

The working tree already contains the complete rc.10 acceptance changes
from your previous run (build_repository.py pins, four test files, the
audited-hash ledger in .research/, LOGBOOK entry). Do NOT redo the work
and do NOT run the full suite: the orchestrator already ran it on this
exact tree with result:

    1797 passed, 245 skipped, 28 warnings in 444.91s (0:07:24)

Your job now, within 10 minutes:
1. Verify the delta is intact: grep PROTOCOL_VERSION and the 803918bf
   SHA in src/csk/build_repository.py; run only
   pytest tests/test_schema_v7_repository.py tests/test_protocol_conformance.py tests/test_rc5_external_repository_conformance.py tests/test_build_metadata.py -q
2. Attach the task-scoped outcome resource describing what changed per
   pin site, why each conformance assertion matches the rc.10 corpus,
   with the literal subset output plus the full-suite line above.
3. Complete the checklist and hand off with task-board handoff.
