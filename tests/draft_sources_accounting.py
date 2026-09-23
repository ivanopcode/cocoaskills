"""Outcome accounting for the draft-sources conformance module.

The category counts reported into the junit artifact need per-test
outcomes, and outcomes are only visible to pytest hooks, which cannot
live in a test module. So the storage, the report categories and the
completeness check live here, the ``pytest_runtest_logreport`` hook in
``tests/conftest.py`` records into :data:`OUTCOMES` (scoped to the
conformance module by node-id filename), and the accounting test at the
end of ``tests/test_draft_sources_conformance.py`` reads it back.

An earlier revision tried an autouse fixture with try/except around the
yield; a forced-skip probe proved test outcomes never propagate through
the yield (six skips recorded as passed), so the fixture form is wrong
here and must not come back.
"""

from __future__ import annotations

from collections.abc import Mapping

# Call-phase outcome per node id for the conformance module. Values are
# "passed", "skipped" or "failed". A setup failure records "failed" for
# a test whose body never ran; a teardown failure overwrites the
# call-phase outcome.
OUTCOMES: dict[str, str] = {}

REPORT_CATEGORIES = ("schema", "snapshot", "semantic", "harness")


def record(nodeid: str, outcome: str) -> None:
    """Record one conformance-module outcome observed by the hook."""
    assert outcome in ("passed", "skipped", "failed"), outcome
    OUTCOMES[nodeid] = outcome


def category(nodeid: str) -> str:
    """Map one conformance-module node id to its report category.

    The three corpus categories count exactly the parametrized corpus
    tests; every inventory, gate, dispatch, instrument and accounting
    test lands in "harness".
    """
    name = nodeid.split("::")[-1]
    if name.startswith("test_draft_sources_schema_case["):
        return "schema"
    if name.startswith("test_draft_sources_snapshot_vector["):
        return "snapshot"
    if name.startswith("test_draft_sources_semantic_case["):
        return "semantic"
    return "harness"


def check_accounting(
    collected: list[str], recorded: Mapping[str, str], self_nodeid: str
) -> dict[str, dict[str, int]]:
    """Check the hook saw every collected test of the module exactly once.

    Returns per-category tallies of ``passed``, ``skipped``, ``failed``
    and ``total``. Raises AssertionError naming a dropped test when the
    hook missed one: the only admitted absence is the accounting test
    itself, whose outcome records after its body runs.
    """
    missing = [
        nodeid
        for nodeid in collected
        if nodeid != self_nodeid and nodeid not in recorded
    ]
    assert not missing, (
        f"outcome collector missed {len(missing)} collected test(s): "
        f"{missing[:3]}; the category counts require module-coherent "
        "scheduling (one worker runs the whole module, as in CI and "
        "with --dist=loadfile)"
    )
    tallies: dict[str, dict[str, int]] = {
        bucket: {"passed": 0, "skipped": 0, "failed": 0, "total": 0}
        for bucket in REPORT_CATEGORIES
    }
    for nodeid in collected:
        if nodeid == self_nodeid:
            continue
        tally = tallies[category(nodeid)]
        tally[recorded[nodeid]] += 1
        tally["total"] += 1
    for bucket, tally in tallies.items():
        assert (
            tally["total"] == tally["passed"] + tally["skipped"] + tally["failed"]
        ), bucket
    return tallies
