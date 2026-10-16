"""Exercise the mutation harness's real sandbox and CONTROL subprocess."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

import audit_secrets_mutants as harness


@pytest.fixture
def harness_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    root = tmp_path / "source"
    (root / "src").mkdir(parents=True)
    tests = root / "tests"
    (tests / "fixtures").mkdir(parents=True)
    (tests / "fixtures" / "c1-redaction-matrix.json").write_text("[]", encoding="utf-8")
    (root / "CHANGELOG.md").write_text("fixture", encoding="utf-8")
    for name in (harness.TEST, harness.REVIEW_TEST, harness.MATRIX_TEST, harness.CLOSURE_TEST,
                 "tests/test_audit_publish.py"):
        (root / name).write_text("def test_control():\n    assert True\n", encoding="utf-8")
    monkeypatch.setattr(harness, "ROOT", root)
    monkeypatch.setattr(harness, "MUTANTS", ())
    monkeypatch.setattr(sys, "argv", ["audit_secrets_mutants.py"])
    return root


def test_control_carries_new_and_transitive_support_modules(harness_root: Path, capsys: pytest.CaptureFixture[str]) -> None:
    # Real CONTROL imports newly named helpers transitively; unrelated test files stay excluded.
    tests = harness_root / "tests"
    (tests / "conftest.py").write_text(
        "import future_fixture_support\n"
        "from pathlib import Path\n"
        "assert future_fixture_support.VALUE == 42\n"
        "assert not Path(__file__).with_name('test_unselected.py').exists()\n",
        encoding="utf-8",
    )
    (tests / "future_fixture_support.py").write_text("from nested_support import VALUE\n", encoding="utf-8")
    (tests / "nested_support.py").write_text("VALUE = 42\n", encoding="utf-8")
    (tests / "test_unselected.py").write_text("raise AssertionError('unselected')\n", encoding="utf-8")

    assert harness.main() == 0
    output = capsys.readouterr()
    assert "4 passed" in output.out
    assert "MUTANTS: 0/0 selected" in output.out
    assert "setup failure" not in output.err


@pytest.mark.parametrize("failure", ["assertion", "collection", "internal", "import", "no-tests"])
def test_broken_control_refused_before_any_mutant(
    harness_root: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], failure: str,
) -> None:
    # Real pytest exits 1–5 refuse before mutation; exit 4 can have empty stdout.
    tests = harness_root / "tests"
    if failure == "import":
        (tests / "conftest.py").write_text("import deliberately_absent_control_dependency\n", encoding="utf-8")
        expected_exit = 4
    elif failure == "collection":
        (harness_root / harness.TEST).write_text("raise RuntimeError('control collection failure')\n", encoding="utf-8")
        expected_exit = 2
    elif failure == "assertion":
        (harness_root / harness.TEST).write_text("def test_control():\n    assert False\n", encoding="utf-8")
        expected_exit = 1
    elif failure == "internal":
        (tests / "conftest.py").write_text(
            "def pytest_sessionstart(session):\n    raise RuntimeError('control internal failure')\n", encoding="utf-8",
        )
        expected_exit = 3
    else:
        for name in (harness.TEST, harness.REVIEW_TEST, harness.MATRIX_TEST, harness.CLOSURE_TEST):
            (harness_root / name).write_text("", encoding="utf-8")
        expected_exit = 5
    def forbidden_mutation(*args: object) -> str:
        pytest.fail("mutant execution reached after broken CONTROL")

    monkeypatch.setattr(harness, "_replace_occurrence", forbidden_mutation)
    monkeypatch.setattr(harness, "MUTANTS", (("must-not-run", "CHANGELOG.md", (("fixture", "mutated", 0),), ""),))

    assert harness.main() == 1
    output = capsys.readouterr()
    assert f"CONTROL setup failure: pytest exit {expected_exit}" in output.err
    assert "no mutants run or counted as killed" in output.err
    if failure == "import":
        assert "ModuleNotFoundError" in output.err
        assert "deliberately_absent_control_dependency" in output.err
    assert "IndexError" not in output.out + output.err
    assert "KILLED" not in output.out + output.err
    assert "MUTANTS:" not in output.out
