"""The presence and consumption ledgers that make a candidate lane meaningful.

The gate these cover exists because a green lane against a schema-8 suite is
not evidence that anything read the suite. Both halves are checked here: the
ledgers parse fail-closed, a root that stops publishing a family defers its
consumer by name, and a run that stops executing a declared case fails even
when pytest itself exits 0.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from candidate_consumption_support import load_candidate_consumption


consumption = load_candidate_consumption()
LedgerError = consumption.LedgerError

ROOT = Path(__file__).parents[1]
ARTIFACT_LEDGER = ROOT / ".github" / "ci" / "candidate-artifacts.tsv"
CASE_LEDGER = ROOT / ".github" / "ci" / "candidate-cases.tsv"
CONSUMER = "tests/test_schema8_candidate_conformance.py"
SCHEMA8_FAMILIES = (
    "schema-cases/agent-skill-v8",
    "schema-cases/csk-skill-v8",
    "schema-cases/install-marker-v4",
    "vectors/module-roots.json",
    "vectors/script-host-execution-policy.json",
)


def _ledger(tmp_path: Path, body: str, name: str = "ledger.tsv") -> Path:
    path = tmp_path / name
    path.write_text(body, encoding="utf-8")
    return path


def _root(tmp_path: Path, paths: tuple[str, ...]) -> Path:
    root = tmp_path / "root"
    root.mkdir(parents=True, exist_ok=True)
    (root / "manifest.json").write_text(
        json.dumps(
            {
                "protocol_version": "1.0.0-rc.9",
                "files": [{"path": path, "sha256": "sha256:" + "0" * 64} for path in paths],
            },
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    return root


def _junit(tmp_path: Path, cases: tuple[tuple[str, str, str], ...]) -> Path:
    body = "".join(
        f'<testcase classname="{classname}" name="{name}">'
        + ("" if outcome == "passed" else f"<{outcome} />")
        + "</testcase>"
        for classname, name, outcome in cases
    )
    tmp_path.mkdir(parents=True, exist_ok=True)
    path = tmp_path / "results.xml"
    path.write_text(
        f'<?xml version="1.0"?><testsuites><testsuite>{body}</testsuite></testsuites>',
        encoding="utf-8",
    )
    return path


# --- the committed ledgers -------------------------------------------------


def test_the_committed_presence_ledger_declares_every_schema8_family() -> None:
    rows = consumption.load_artifact_ledger(ARTIFACT_LEDGER)
    declared = {row.consumer: row.artifacts for row in rows}
    assert CONSUMER in declared
    assert set(SCHEMA8_FAMILIES) <= set(declared[CONSUMER])
    assert all(row.note for row in rows)


def test_the_committed_case_ledger_requires_every_consumer_case_everywhere() -> None:
    rows = consumption.load_case_ledger(CASE_LEDGER)
    assert rows
    for row in rows:
        assert row.nodeid.startswith(f"{CONSUMER}::")
        assert set(row.must_run_on) == set(consumption.PLATFORMS)
        assert row.note


def test_every_declared_case_names_a_test_the_consumer_defines() -> None:
    """A ledger row that names nothing proves nothing."""
    source = (ROOT / CONSUMER).read_text(encoding="utf-8")
    for row in consumption.load_case_ledger(CASE_LEDGER):
        name = row.nodeid.split("::")[-1]
        assert f"def {name}(" in source, f"{row.nodeid} names no test in {CONSUMER}"


# --- ledger parsing --------------------------------------------------------


@pytest.mark.parametrize(
    "body",
    [
        "consumer\tvectors/a.json\n",
        "consumer\tvectors/a.json\tnote\textra\n",
        "consumer\t\tnote\n",
        "# only comments\n",
        "",
    ],
)
def test_a_malformed_presence_row_is_rejected(tmp_path: Path, body: str) -> None:
    with pytest.raises(LedgerError):
        consumption.load_artifact_ledger(_ledger(tmp_path, body))


def test_a_repeated_consumer_or_artifact_is_rejected(tmp_path: Path) -> None:
    with pytest.raises(LedgerError, match="twice"):
        consumption.load_artifact_ledger(
            _ledger(tmp_path, "a\tv/a.json\tnote\na\tv/b.json\tnote\n")
        )
    with pytest.raises(LedgerError, match="repeats"):
        consumption.load_artifact_ledger(
            _ledger(tmp_path, "a\tv/a.json,v/a.json\tnote\n")
        )


def test_a_missing_ledger_is_not_an_empty_ledger(tmp_path: Path) -> None:
    with pytest.raises(LedgerError, match="does not exist"):
        consumption.load_artifact_ledger(tmp_path / "absent.tsv")


@pytest.mark.parametrize(
    "body",
    [
        "not-a-nodeid\tlinux\tnote\n",
        "tests/a.py::test_b[case]\tlinux\tnote\n",
        "tests/a.py::test_b\tplan9\tnote\n",
    ],
)
def test_a_malformed_case_row_is_rejected(tmp_path: Path, body: str) -> None:
    with pytest.raises(LedgerError):
        consumption.load_case_ledger(_ledger(tmp_path, body))


# --- presence --------------------------------------------------------------


def test_a_family_directory_counts_as_present_through_its_members(tmp_path: Path) -> None:
    root = _root(tmp_path, ("schema-cases/agent-skill-v8/valid.json", "vectors/a.json"))
    inventory = consumption.manifest_inventory(root)
    assert consumption.missing_artifacts(
        ("schema-cases/agent-skill-v8", "vectors/a.json"), inventory
    ) == ()


def test_an_unpublished_family_is_named_as_missing(tmp_path: Path) -> None:
    root = _root(tmp_path, ("vectors/a.json",))
    inventory = consumption.manifest_inventory(root)
    assert consumption.missing_artifacts(
        ("schema-cases/agent-skill-v8", "vectors/module-roots.json", "vectors/a.json"),
        inventory,
    ) == ("schema-cases/agent-skill-v8", "vectors/module-roots.json")


def test_a_prefix_that_is_not_a_directory_boundary_is_still_missing(tmp_path: Path) -> None:
    """`vectors/module-roots.json` must not be satisfied by a longer name."""
    root = _root(tmp_path, ("vectors/module-roots.json.bak",))
    inventory = consumption.manifest_inventory(root)
    assert consumption.missing_artifacts(("vectors/module-roots.json",), inventory) == (
        "vectors/module-roots.json",
    )


@pytest.mark.parametrize(
    "payload",
    [
        {"protocol_version": "1.0.0-rc.9"},
        {"files": []},
        {"files": [{"path": "a", "sha256": "sha256:" + "0" * 64}, {"path": "a", "sha256": "sha256:" + "0" * 64}]},
        {"files": [{"path": "a"}]},
        {"files": [{"path": "a", "sha256": "0" * 64}]},
    ],
)
def test_a_broken_manifest_fails_closed(tmp_path: Path, payload: dict) -> None:
    root = tmp_path / "root"
    root.mkdir()
    (root / "manifest.json").write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(LedgerError):
        consumption.manifest_inventory(root)


def test_the_deferred_partition_is_derived_from_the_root(tmp_path: Path) -> None:
    ledger = consumption.load_artifact_ledger(
        _ledger(tmp_path, "consumer\tvectors/a.json,vectors/b.json\tnote\n")
    )
    served = _root(tmp_path / "served", ("vectors/a.json", "vectors/b.json"))
    deferred = _root(tmp_path / "deferred", ("vectors/a.json",))
    assert consumption.deferred_consumers(served, ledger) == {}
    assert consumption.deferred_consumers(deferred, ledger) == {
        "consumer": ("vectors/b.json",)
    }


def test_the_require_command_names_the_missing_family(tmp_path: Path) -> None:
    ledger = _ledger(tmp_path, "consumer\tvectors/a.json,vectors/b.json\tnote\n")
    served = _root(tmp_path / "served", ("vectors/a.json", "vectors/b.json"))
    deferred = _root(tmp_path / "deferred", ("vectors/a.json",))
    assert consumption.main(["require", "--root", str(served), "--ledger", str(ledger)]) == 0
    assert consumption.main(["require", "--root", str(deferred), "--ledger", str(ledger)]) == 1


# --- consumption -----------------------------------------------------------


def test_junit_results_reduce_to_pytest_node_ids(tmp_path: Path) -> None:
    results = _junit(
        tmp_path,
        (
            ("tests.test_x", "test_a", "passed"),
            ("tests.test_x", "test_b[case-1]", "skipped"),
            ("tests.test_x.Suite", "test_c", "failure"),
        ),
    )
    observed = consumption.read_junit(results)
    assert [item.nodeid for item in observed] == [
        "tests/test_x.py::test_a",
        "tests/test_x.py::test_b[case-1]",
        "tests/test_x.py::Suite::test_c",
    ]
    assert [item.outcome for item in observed] == ["passed", "skipped", "failed"]


def test_a_row_covers_every_parameterization_of_its_test(tmp_path: Path) -> None:
    ledger = consumption.load_case_ledger(
        _ledger(tmp_path, "tests/test_x.py::test_a\tlinux,darwin,windows\tnote\n")
    )
    passing = consumption.read_junit(
        _junit(
            tmp_path / "pass",
            (("tests.test_x", "test_a[one]", "passed"), ("tests.test_x", "test_a[two]", "passed")),
        )
    )
    assert consumption.gate_cases(passing, "linux", ledger) == ()

    partial = consumption.read_junit(
        _junit(
            tmp_path / "partial",
            (("tests.test_x", "test_a[one]", "passed"), ("tests.test_x", "test_a[two]", "skipped")),
        )
    )
    failures = consumption.gate_cases(partial, "linux", ledger)
    assert len(failures) == 1 and "skipped" in failures[0]


def test_a_case_that_stopped_running_fails_the_gate(tmp_path: Path) -> None:
    """A shrunk run is green to pytest; it must not be green to the gate."""
    ledger = consumption.load_case_ledger(
        _ledger(tmp_path, "tests/test_x.py::test_a\tlinux,darwin,windows\tnote\n")
    )
    observed = consumption.read_junit(
        _junit(tmp_path, (("tests.test_x", "test_other", "passed"),))
    )
    failures = consumption.gate_cases(observed, "linux", ledger)
    assert len(failures) == 1
    assert "tests/test_x.py::test_a" in failures[0]
    assert "zero times" in failures[0]


def test_a_platform_a_row_does_not_require_is_not_gated(tmp_path: Path) -> None:
    ledger = consumption.load_case_ledger(
        _ledger(tmp_path, "tests/test_x.py::test_a\tlinux\tnote\n")
    )
    observed = consumption.read_junit(
        _junit(tmp_path, (("tests.test_x", "test_other", "passed"),))
    )
    assert consumption.gate_cases(observed, "darwin", ledger) == ()
    assert consumption.gate_cases(observed, "linux", ledger) != ()


def test_an_unknown_platform_or_empty_stream_fails_closed(tmp_path: Path) -> None:
    ledger = consumption.load_case_ledger(
        _ledger(tmp_path, "tests/test_x.py::test_a\tlinux\tnote\n")
    )
    with pytest.raises(LedgerError, match="unknown platform"):
        consumption.gate_cases((), "plan9", ledger)
    empty = tmp_path / "empty.xml"
    empty.write_text('<?xml version="1.0"?><testsuites></testsuites>', encoding="utf-8")
    with pytest.raises(LedgerError, match="no testcase"):
        consumption.read_junit(empty)
    with pytest.raises(LedgerError, match="does not exist"):
        consumption.read_junit(tmp_path / "absent.xml")
