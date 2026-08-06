from __future__ import annotations

import re
from pathlib import Path


WORKFLOW = Path(__file__).parents[1] / ".github" / "workflows" / "ci.yml"
HEAVY_PYTHON = "if: matrix.python-version == '3.11'"


def _workflow() -> str:
    return WORKFLOW.read_text(encoding="utf-8")


def _step(workflow: str, name: str) -> str:
    match = re.search(
        rf"(?ms)^      - name: {re.escape(name)}\n(.*?)(?=^      - name: |^  [a-z])",
        workflow,
    )
    assert match is not None, f"missing CI step: {name}"
    return match.group(0)


def test_required_matrix_check_names_and_coverage_are_preserved() -> None:
    workflow = _workflow()

    assert "name: Tests / Python ${{ matrix.python-version }} on ${{ matrix.os }}" in workflow
    assert "os: [ubuntu-latest, macos-latest, windows-latest]" in workflow
    assert 'python-version: ["3.11", "3.12", "3.13", "3.14"]' in workflow
    ordinary = _step(workflow, "Run ordinary tests")
    assert HEAVY_PYTHON not in ordinary
    assert "python -m pytest -v" in ordinary
    assert ordinary.count("--ignore=") == 2
    assert "--ignore=tests/test_protocol_conformance.py" in ordinary
    assert "--ignore=tests/test_go_build_e2e.py" in ordinary


def test_heavy_suites_run_once_per_os_without_losing_nodes() -> None:
    workflow = _workflow()

    protocol = _step(workflow, "Run protocol conformance suite")
    assert HEAVY_PYTHON in protocol
    assert "python -m pytest -v tests/test_protocol_conformance.py" in protocol

    for name in (
        "Checkout caller-supplied rc.6 candidate",
        "Authenticate caller-supplied rc.6 candidate",
        "Collect accepted Go E2E node IDs",
        "Run accepted Go E2E selection",
        "Upload accepted Go E2E evidence",
    ):
        assert HEAVY_PYTHON in _step(workflow, name)

    collect = _step(workflow, "Collect accepted Go E2E node IDs")
    accepted = _step(workflow, "Run accepted Go E2E selection")
    for step in (collect, accepted):
        assert "tests/test_go_build_e2e.py" in step
        assert "csk_e2e_ubuntu" in step
        assert "csk_e2e_native" in step
        assert "${{ runner.os == 'Linux'" in step

    assert "needs: test" in workflow
