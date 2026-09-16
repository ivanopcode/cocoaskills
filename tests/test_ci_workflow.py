from __future__ import annotations

import json
import os
import re
import shlex
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


def test_the_shard_verifier_is_reachable_before_a_merge() -> None:
    """merge_protocol must be dispatchable, otherwise it is unverifiable on a branch."""
    protocol = _job(_workflow(), "merge_protocol")
    assert (
        "    if: >-\n"
        "      (github.event_name == 'push' && github.ref == 'refs/heads/main') ||\n"
        "      github.event_name == 'workflow_dispatch'\n"
    ) in protocol
    assert "Verify and select deterministic Windows protocol shard" in protocol


def test_stable_aggregates_always_run_and_fail_closed() -> None:
    workflow = _workflow()

    expected = {
        "fast": {
            "typecheck",
            "build",
            "fast_ordinary",
            "fast_protocol",
            "fast_go_e2e",
            "fast_draft_sources",
        },
        "merge": {
            "typecheck",
            "build",
            "merge_ordinary",
            "merge_protocol",
            "merge_go_e2e",
            "merge_draft_sources",
        },
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
    assert pin == ["0ed5c691e9208eea52f21db2fc05e226ce3516fd"]
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


def test_the_candidate_lane_requires_and_gates_schema8_consumption() -> None:
    """Both halves of the false-green hole are closed in both candidate jobs.

    Checking out a candidate suite and matching its digest proves the lane read
    the right bytes, not that it read them at all. Every candidate job must
    therefore assert the root serves the declared consumers, run the consumer,
    and gate the declared cases against that run's own result stream.
    """
    workflow = _workflow()

    for job_id in ("fast_go_e2e", "merge_go_e2e"):
        job = _job(workflow, job_id)
        assert "candidate_consumption.py require" in job
        assert "tests/test_schema8_candidate_conformance.py" in job
        assert 'CSK_REQUIRE_FULL_CANDIDATE_ROOT: "1"' in job
        assert "--junitxml=csk-schema8-results.xml" in job
        assert "candidate_consumption.py gate" in job
        assert "--results csk-schema8-results.xml" in job
        # The gate runs against the stream the consumer just produced, and the
        # stream is uploaded as evidence rather than discarded.
        assert job.index("candidate_suite.py record") < job.index(
            "candidate_consumption.py require"
        )
        assert job.index("candidate_consumption.py require") < job.index(
            "--junitxml=csk-schema8-results.xml"
        )
        assert job.index("--junitxml=csk-schema8-results.xml") < job.index(
            "candidate_consumption.py gate"
        )
        assert job.count("csk-schema8-results.xml") == 3
        for platform in ("linux", "darwin", "windows"):
            assert f"'{platform}'" in job


def test_the_declared_candidate_is_one_immutable_qualified_identity() -> None:
    descriptor = json.loads(
        (CI_CONFIG / "candidate-suite.json").read_text(encoding="utf-8")
    )
    assert descriptor["repository"] == "relux-works/curator-spec"
    assert re.fullmatch(r"[0-9a-f]{40}", descriptor["revision"])
    for field in ("manifest_sha256", "tree_sha256"):
        assert re.fullmatch(r"sha256:[0-9a-f]{64}", descriptor[field])
    # A candidate never equals the released pin, which is what keeps candidate
    # evidence from impersonating the qualified suite.
    pin = re.findall(r"^  RELEASED_SUITE_PIN: ([0-9a-f]{40})$", _workflow(), re.MULTILINE)
    assert descriptor["revision"] != pin[0]


def test_draft_sources_lanes_run_the_harness_against_the_pinned_suite() -> None:
    workflow = _workflow()

    fast = _job(workflow, "fast_draft_sources")
    assert "if: github.event_name == 'pull_request'" in fast
    assert "refs/heads/main" not in fast

    merge = _job(workflow, "merge_draft_sources")
    assert "if: github.event_name == 'push' && github.ref == 'refs/heads/main'" in merge

    for job in (fast, merge):
        assert "os: [ubuntu-latest, macos-latest]" in job
        assert 'python-version: "3.14"' in job
        assert "timeout-minutes: 20" in job
        # The draft suite enters only through its own pin file, never through
        # the released pin, so the lane cannot impersonate qualified evidence.
        assert "RELEASED_SUITE_PIN" not in job
        assert job.index("Resolve the draft sources suite pin") < job.index(
            "ref: ${{ steps.suite.outputs.revision }}"
        )
        assert "repository: ${{ steps.suite.outputs.repository }}" in job
        assert ".github/ci/draft-sources-suite.json" in job
        assert "path: protocol-spec-draft" in job
        assert (
            "CSK_DRAFT_SOURCES_SUITE_ROOT: ${{ github.workspace }}"
            "/protocol-spec-draft/conformance/draft-sources-v1" in job
        )
        assert "python -m pytest -q tests/test_draft_sources_conformance.py" in job
        assert "--junitxml=draft-sources-results.xml" in job
        assert job.count("draft-sources-results.xml") == 2
        assert "if-no-files-found: error" in job


_DRAFT_SOURCES_PYTEST_TARGET = "tests/test_draft_sources_conformance.py"
_DRAFT_SOURCES_JUNIT = "draft-sources-results.xml"

# Selection/filter flags that must never appear in a draft lane's pytest
# command: any of them silently narrows the corpus while every searched token
# stays present. Short flags are matched as single-dash tokens containing k/m
# (catching -k, -m and bundled forms like -qk); long flags match exactly or
# with an =value.
_DRAFT_SOURCES_FORBIDDEN_LONG_FLAGS = (
    "--deselect",
    "--deselect-from-file",
    "--ignore",
    "--ignore-glob",
    "--collect-only",
    "--co",
    "--last-failed",
    "--lf",
    "--failed",
    "--ff",
    "--stepwise",
    "--sw",
    "--markers",
)


def _draft_sources_pytest_tokens(job_id: str) -> list[str]:
    """Parse the ACTUAL draft-lane pytest command of one job into tokens.

    The run step uses a YAML ``>-`` folded scalar, so joining the indented
    continuation lines with single spaces reproduces the shell command; shlex
    then splits it exactly as the runner would.
    """
    job = _job(_workflow(), job_id)
    marker = "- name: Run draft sources conformance"
    assert marker in job, f"missing draft conformance run step in {job_id}"
    body = job[job.index(marker):]
    run_marker = "run: >-"
    assert run_marker in body, f"missing folded run block in {job_id}"
    rest = body[body.index(run_marker) + len(run_marker):].splitlines()
    command_lines = []
    for line in rest:
        if not line.strip():
            continue
        if re.match(r"^ {10}\S", line):
            command_lines.append(line.strip())
        else:
            break
    assert command_lines, f"empty draft conformance run block in {job_id}"
    return shlex.split(" ".join(command_lines))


def test_draft_sources_lanes_run_the_full_harness_without_selection_filters() -> None:
    """Both draft lanes run the whole harness file with no selection filter.

    Regression for the token-preserving narrowing hole: substring assertions
    cannot see an appended ``-k``/``-m``/``--deselect``/``--ignore`` filter, so
    this test parses the ACTUAL ``Run draft sources conformance`` pytest
    command of BOTH jobs and asserts it targets exactly
    ``tests/test_draft_sources_conformance.py``, carries no selection/filter
    flag, node id (``::``) or argfile (``@``), and writes the junit artifact
    from that same unfiltered run. Appending
    ``-k test_draft_sources_snapshot_vector`` to either job fails this test
    while keeping every substring token present.
    """
    for job_id in ("fast_draft_sources", "merge_draft_sources"):
        tokens = _draft_sources_pytest_tokens(job_id)
        assert tokens[:3] == ["python", "-m", "pytest"], f"{job_id}: {tokens!r}"
        rest = tokens[3:]

        # No selection/filter flag may narrow the corpus.
        for token in rest:
            assert not re.match(r"^-(?!-)[A-Za-z]*[kKmM]", token), (
                f"{job_id} narrows the draft corpus with {token!r}"
            )
            for flag in _DRAFT_SOURCES_FORBIDDEN_LONG_FLAGS:
                assert not (token == flag or token.startswith(flag + "=")), (
                    f"{job_id} narrows the draft corpus with {token!r}"
                )
            assert "::" not in token, f"{job_id} selects a node id with {token!r}"
            assert not token.startswith("@"), f"{job_id} selects an argfile with {token!r}"

        # The junit artifact is produced by this same unfiltered run.
        junit = [token for token in rest if token.startswith("--junitxml=")]
        assert junit == [f"--junitxml={_DRAFT_SOURCES_JUNIT}"], f"{job_id}: {tokens!r}"

        # Exactly one positional target: the whole harness file.
        options = {"-q", f"--junitxml={_DRAFT_SOURCES_JUNIT}"}
        positionals = [token for token in rest if not token.startswith("-")]
        assert positionals == [_DRAFT_SOURCES_PYTEST_TARGET], f"{job_id}: {tokens!r}"
        for token in rest:
            if token.startswith("-"):
                assert token in options, f"{job_id} carries unexpected option {token!r}"

        job = _job(_workflow(), job_id)
        assert f"path: {_DRAFT_SOURCES_JUNIT}" in job, f"{job_id} uploads no junit artifact"
        # A PYTEST_ADDOPTS override would filter the run without touching the
        # command line at all.
        assert "PYTEST_ADDOPTS" not in job, f"{job_id} reads PYTEST_ADDOPTS"


def test_the_draft_sources_suite_pin_is_one_immutable_identity() -> None:
    pin = json.loads((CI_CONFIG / "draft-sources-suite.json").read_text(encoding="utf-8"))
    assert set(pin) == {"repository", "revision", "suite_root", "files"}
    assert pin["repository"] == "relux-works/curator-spec"
    assert pin["revision"] == "8ba9c235ec5be00d52378479516c82386fd0c178"
    assert pin["suite_root"] == "conformance/draft-sources-v1"
    assert pin["files"] == {
        "index.json": (
            "sha256:c1c2e60a595107a79aaefdbd7822d95279cc792cc8cdc969ded36522663e246f"
        ),
        "semantic-cases.json": (
            "sha256:552c1eed16d2d6bb37b0a5726b9420d20e4dc1d5e97e34cfa4ee6182bcb90ce0"
        ),
        "snapshot-cases.json": (
            "sha256:1922256efe21f934b667ec913f34a2af3b16004ecb00c63ee8712eacaf347999"
        ),
    }
    # A draft suite never equals the released pin, which is what keeps draft
    # evidence from impersonating the qualified suite.
    released = re.findall(r"^  RELEASED_SUITE_PIN: ([0-9a-f]{40})$", _workflow(), re.MULTILINE)
    assert pin["revision"] != released[0]


def test_the_consumption_ledgers_are_declared_and_reachable() -> None:
    for name in ("candidate-artifacts.tsv", "candidate-cases.tsv"):
        path = CI_CONFIG / name
        assert path.is_file(), f"missing consumption ledger: {name}"
        rows = [
            line
            for line in path.read_text(encoding="utf-8").splitlines()
            if line.strip() and not line.lstrip().startswith("#")
        ]
        assert rows, f"{name} declares no row"
        assert all(len(line.split("\t")) == 3 for line in rows)
