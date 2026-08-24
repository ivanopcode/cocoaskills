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
        assert "github.event_name == 'pull_request'" in job
        assert "refs/heads/main" not in job
        assert "timeout-minutes: 20" in job


def test_fast_selections_are_exact_checked_in_node_inventories() -> None:
    workflow = _workflow()
    protocol = _nodeids("protocol-fast-nodeids.txt")
    native = _nodeids("go-e2e-native-smoke-nodeids.txt")
    macos_worker_domain = _nodeids("go-e2e-macos-worker-domain-nodeids.txt")
    ubuntu = _nodeids("go-e2e-ubuntu-smoke-nodeids.txt")

    assert len(protocol) == 10
    assert len(native) == 5
    assert macos_worker_domain == [
        "tests/test_go_build_e2e.py::test_real_go_cache_hit_and_relevant_source_mutation"
    ]
    assert len(ubuntu) == 4
    assert all(node.startswith("tests/test_protocol_conformance.py::") for node in protocol)
    assert all(
        node.startswith("tests/test_go_build_e2e.py::")
        for node in native + macos_worker_domain + ubuntu
    )

    protocol_job = _job(workflow, "fast_protocol")
    assert protocol_job.count("@.github/ci/protocol-fast-nodeids.txt") == 2

    go_job = _job(workflow, "fast_go_e2e")
    for name in (
        "go-e2e-native-smoke-nodeids.txt",
        "go-e2e-macos-worker-domain-nodeids.txt",
        "go-e2e-ubuntu-smoke-nodeids.txt",
    ):
        assert go_job.count(name) == 2
    assert "csk_e2e_ubuntu" in go_job
    assert "csk_e2e_native" in go_job


def test_main_lane_preserves_full_platform_coverage_and_go_evidence() -> None:
    workflow = _workflow()

    ordinary = _job(workflow, "merge_ordinary")
    assert "if: github.event_name == 'push' && github.ref == 'refs/heads/main'" in ordinary
    assert "os: [ubuntu-latest, macos-latest, windows-latest]" in ordinary
    assert 'python-version: ["3.11", "3.12", "3.13", "3.14"]' in ordinary
    assert ordinary.count("--ignore=") == 2
    assert "-n 4 --dist=loadfile" in ordinary

    protocol = _job(workflow, "merge_protocol")
    assert "python -m pytest -v tests/test_protocol_conformance.py" in protocol
    assert "actions/setup-go@v7" in protocol
    assert "Configure git (POSIX)" in protocol
    assert "Configure git (Windows)" in protocol
    assert "ubuntu-full" in protocol
    assert "macos-full" in protocol
    assert protocol.count("os: windows-latest") == 6

    go_e2e = _job(workflow, "merge_go_e2e")
    assert "tests/test_go_build_e2e.py" in go_e2e
    assert "csk_e2e_ubuntu" in go_e2e
    assert "csk_e2e_native" in go_e2e
    assert "Collect accepted Go E2E node IDs" in go_e2e
    assert "Upload accepted Go E2E evidence" in go_e2e


def test_windows_protocol_shards_are_static_bounded_and_fail_closed() -> None:
    protocol = _job(_workflow(), "merge_protocol")
    expected_timeouts = {
        "p00-contract-and-registry": 5,
        "p01-lifecycle-cached-baseline": 30,
        "p02-lifecycle-sabotage-a": 45,
        "p03-lifecycle-sabotage-b": 45,
        "p04-lifecycle-sabotage-c": 45,
        "p05-lifecycle-sabotage-d": 45,
    }
    for shard_id, timeout in expected_timeouts.items():
        row = re.search(
            rf"(?ms)^          - os: windows-latest\n"
            rf"            label: windows-{re.escape(shard_id)}\n"
            rf"            shard: {re.escape(shard_id)}\n"
            rf"            temp_tag: p\d{{2}}\n"
            rf"            timeout_minutes: (?P<timeout>\d+)$",
            protocol,
        )
        assert row is not None
        assert int(row.group("timeout")) == timeout

    collect = protocol.index("Collect canonical Windows protocol inventory")
    verify = protocol.index("Verify and select deterministic Windows protocol shard")
    execute = protocol.index("Run deterministic Windows protocol shard")
    upload = protocol.index("Upload Windows protocol shard evidence")
    assert collect < verify < execute < upload
    assert "timeout-minutes: 360" in protocol[:collect]
    assert (
        "Run deterministic Windows protocol shard\n"
        "        if: runner.os == 'Windows'\n"
        "        timeout-minutes: ${{ matrix.timeout_minutes }}"
    ) in protocol
    assert "--collect-only -q tests/test_protocol_conformance.py" in protocol
    assert "Relocate Windows protocol checkout and create evidence directory" in protocol
    configure_windows = protocol.index("Configure git (Windows)")
    normalize_checkout = protocol.index("git reset --hard HEAD", configure_windows)
    install = protocol.index("Install package", normalize_checkout)
    assert configure_windows < normalize_checkout < install < collect
    assert 'Move-Item -LiteralPath "${{ github.workspace }}/protocol-spec"' in protocol
    assert "--classification .research/TASK-260803-2ol7ok_protocol-isolation-classification.json" in protocol
    assert "--manifest .research/TASK-260803-2ol7ok_protocol-shards.json" in protocol
    assert '--shard-id "${{ matrix.shard }}"' in protocol
    assert 'python -m pytest -vv "@${{ runner.temp }}/protocol-${{ matrix.temp_tag }}/shard-nodeids.txt"' in protocol
    assert '${{ runner.temp }}/csk-${{ matrix.temp_tag }}' in protocol
    assert '${{ runner.temp }}/csk-cache-${{ matrix.temp_tag }}' in protocol
    assert '${{ runner.temp }}/protocol-${{ matrix.temp_tag }}/collected-nodeids.txt' in protocol
    assert '${{ runner.temp }}/protocol-${{ matrix.temp_tag }}/verification.json' in protocol
    assert '${{ runner.temp }}/protocol-${{ matrix.temp_tag }}/results.xml' in protocol
    assert "if-no-files-found: error" in protocol


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


def test_candidate_input_is_an_explicit_dispatch_contract() -> None:
    workflow = _workflow()
    trigger = re.search(r"(?ms)^  workflow_dispatch:\n(.*?)(?=^concurrency:)", workflow)
    assert trigger is not None

    inputs = trigger.group(1)
    for name in (
        "candidate_ref",
        "candidate_manifest_sha256",
        "candidate_protocol_version",
    ):
        assert f"      {name}:" in inputs
        assert f'{name}:\n        description' in inputs
    assert inputs.count('default: ""') == 3


def test_candidate_authentication_is_identical_in_fast_and_merge_go_jobs() -> None:
    workflow = _workflow()

    for job_id in ("fast_go_e2e", "merge_go_e2e"):
        job = _job(workflow, job_id)
        assert "github.event_name == 'workflow_dispatch'" in job
        # The candidate identity is resolved once, before it can be fetched, and
        # every later step of the job reads the same override environment.
        for name in (
            "CANDIDATE_REF: ${{ inputs.candidate_ref }}",
            "CANDIDATE_MANIFEST_SHA256: ${{ inputs.candidate_manifest_sha256 }}",
            "CANDIDATE_PROTOCOL_VERSION: ${{ inputs.candidate_protocol_version }}",
        ):
            assert job.count(name) == 1
        assert job.index("candidate_suite.py resolve") < job.index(
            "ref: ${{ steps.candidate.outputs.revision }}"
        )
        assert "repository: ${{ steps.candidate.outputs.repository }}" in job
        assert "candidate_suite.py record" in job
        assert "--evidence candidate-suite-identity.txt" in job
        assert job.count("candidate-suite-identity.txt") == 2
        assert "CSK_E2E_REQUIRED_PLATFORM:" in job


def test_the_released_suite_pin_is_declared_once_and_never_inlined() -> None:
    workflow = _workflow()
    pin = re.findall(r"^  RELEASED_SUITE_PIN: ([0-9a-f]{40})$", workflow, re.MULTILINE)
    assert pin == ["0c81c1f8d5321d822be2a2817b05aea03e656e15"]
    assert workflow.count("ref: ${{ env.RELEASED_SUITE_PIN }}") == 4

    for job_id in ("fast_ordinary", "fast_protocol", "merge_protocol"):
        assert pin[0] not in _job(workflow, job_id)

    # The candidate lanes never see the released pin, and no lane still reads
    # the retired repository variable.
    for job_id in ("fast_go_e2e", "merge_go_e2e"):
        assert "RELEASED_SUITE_PIN" not in _job(workflow, job_id)
    assert "CSK_E2E_CURATOR_SPEC_SHA" not in workflow


def test_xdist_is_a_bounded_dev_dependency() -> None:
    pyproject = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
    assert '"pytest-xdist>=3.8,<4"' in pyproject
