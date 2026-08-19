from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import textwrap
from pathlib import Path


ROOT = Path(__file__).parents[1]
WORKFLOW = ROOT / ".github" / "workflows" / "ci.yml"
CI_CONFIG = ROOT / ".github" / "ci"


def _workflow() -> str:
    return WORKFLOW.read_text(encoding="utf-8")


def _job(workflow: str, job_id: str) -> str:
    match = re.search(
        rf"(?ms)^  {re.escape(job_id)}:\n(.*?)(?=^  [a-z][a-z0-9_]*:|\Z)",
        workflow,
    )
    assert match is not None, f"missing CI job: {job_id}"
    return match.group(0)


def _nodeids(name: str) -> list[str]:
    nodeids = (CI_CONFIG / name).read_text(encoding="utf-8").splitlines()
    assert all(nodeids)
    assert len(nodeids) == len(set(nodeids))
    return nodeids


def _aggregate_script(aggregate: str) -> str:
    match = re.search(
        r"(?ms)        run: \|\n          python - <<'PY'\n(?P<script>.*?)^          PY$",
        aggregate,
    )
    assert match is not None
    return textwrap.dedent(match.group("script"))


def test_pull_request_lane_is_event_separated_and_bounded() -> None:
    workflow = _workflow()

    ordinary = _job(workflow, "fast_ordinary")
    assert "if: github.event_name == 'pull_request'" in ordinary
    assert "os: [ubuntu-latest, macos-latest, windows-latest]" in ordinary
    assert 'python-version: "3.14"' in ordinary
    assert ordinary.count("--ignore=") == 2
    assert "--ignore=tests/test_protocol_conformance.py" in ordinary
    assert "--ignore=tests/test_go_build_e2e.py" in ordinary
    assert "-n 4 --dist=loadfile" in ordinary
    assert '${{ runner.temp }}/csk-ordinary' in ordinary
    assert '${{ runner.temp }}/csk-pytest-cache' in ordinary
    assert "timeout-minutes: 20" in ordinary

    for job_id in ("fast_protocol", "fast_go_e2e"):
        job = _job(workflow, job_id)
        assert "if: github.event_name == 'pull_request'" in job
        assert "timeout-minutes: 20" in job


def test_fast_selections_are_exact_checked_in_node_inventories() -> None:
    workflow = _workflow()
    protocol = _nodeids("protocol-fast-nodeids.txt")
    native = _nodeids("go-e2e-native-smoke-nodeids.txt")
    ubuntu = _nodeids("go-e2e-ubuntu-smoke-nodeids.txt")

    assert len(protocol) == 10
    assert len(native) == 5
    assert len(ubuntu) == 4
    assert all(node.startswith("tests/test_protocol_conformance.py::") for node in protocol)
    assert all(node.startswith("tests/test_go_build_e2e.py::") for node in native + ubuntu)

    protocol_job = _job(workflow, "fast_protocol")
    assert protocol_job.count("@.github/ci/protocol-fast-nodeids.txt") == 2

    go_job = _job(workflow, "fast_go_e2e")
    for name in ("go-e2e-native-smoke-nodeids.txt", "go-e2e-ubuntu-smoke-nodeids.txt"):
        assert go_job.count(name) == 2
    assert "csk_e2e_ubuntu" in go_job
    assert "csk_e2e_native" in go_job


def test_main_lane_preserves_full_matrix_protocol_and_go_evidence() -> None:
    workflow = _workflow()

    ordinary = _job(workflow, "merge_ordinary")
    assert "if: github.event_name == 'push' && github.ref == 'refs/heads/main'" in ordinary
    assert "os: [ubuntu-latest, macos-latest, windows-latest]" in ordinary
    assert 'python-version: ["3.11", "3.12", "3.13", "3.14"]' in ordinary
    assert ordinary.count("--ignore=") == 2
    assert "-n 4 --dist=loadfile" in ordinary

    protocol = _job(workflow, "merge_protocol")
    assert "python -m pytest -v tests/test_protocol_conformance.py" in protocol
    assert "@.github/ci/" not in protocol
    assert "actions/setup-go@v7" in protocol
    assert "Configure git (POSIX)" in protocol
    assert "Configure git (Windows)" in protocol

    go_e2e = _job(workflow, "merge_go_e2e")
    assert "tests/test_go_build_e2e.py" in go_e2e
    assert "csk_e2e_ubuntu" in go_e2e
    assert "csk_e2e_native" in go_e2e
    assert "Collect accepted Go E2E node IDs" in go_e2e
    assert "Upload accepted Go E2E evidence" in go_e2e


def test_stable_aggregates_always_run_and_fail_closed() -> None:
    workflow = _workflow()

    expected = {
        "fast": {"typecheck", "build", "fast_ordinary", "fast_protocol", "fast_go_e2e"},
        "merge": {"typecheck", "build", "merge_ordinary", "merge_protocol", "merge_go_e2e"},
    }
    for job_id, children in expected.items():
        aggregate = _job(workflow, job_id)
        assert f"name: {job_id}" in aggregate
        assert "if: ${{ always() &&" in aggregate
        needs = re.search(r"needs: \[(.*?)\]", aggregate)
        assert needs is not None
        assert {item.strip() for item in needs.group(1).split(",")} == children
        assert 'if set(actual) != expected:' in aggregate
        assert 'value.get("result") != "success"' in aggregate

        script = _aggregate_script(aggregate)
        success = {child: {"result": "success"} for child in children}
        env = {**os.environ, "NEEDS_JSON": json.dumps(success)}
        assert subprocess.run([sys.executable, "-c", script], env=env, check=False).returncode == 0

        missing = dict(success)
        missing.pop(next(iter(children)))
        env["NEEDS_JSON"] = json.dumps(missing)
        assert subprocess.run([sys.executable, "-c", script], env=env, check=False).returncode != 0

        for result in ("failure", "cancelled", "skipped"):
            unhealthy = dict(success)
            unhealthy[next(iter(children))] = {"result": result}
            env["NEEDS_JSON"] = json.dumps(unhealthy)
            assert subprocess.run([sys.executable, "-c", script], env=env, check=False).returncode != 0


def test_candidate_authentication_is_identical_in_fast_and_merge_go_jobs() -> None:
    workflow = _workflow()
    expected_sha = "432eb2ee1fe2d6b271e37269f867c8851c325539"
    expected_manifest = "12e58b82579645ba1ccafba49d3e2dd3216005ddf37ae63c68a9fafd46773071"

    for job_id in ("fast_go_e2e", "merge_go_e2e"):
        job = _job(workflow, job_id)
        assert "ref: ${{ vars.CSK_E2E_CURATOR_SPEC_SHA }}" in job
        assert expected_sha in job
        assert expected_manifest in job
        assert "CSK_E2E_REQUIRED_PLATFORM:" in job


def test_xdist_is_a_bounded_dev_dependency() -> None:
    pyproject = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
    assert '"pytest-xdist>=3.8,<4"' in pyproject
