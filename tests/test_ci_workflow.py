from __future__ import annotations

import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest
import yaml


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


GATE_SCRIPT = ROOT / ".github" / "scripts" / "floor_gate.py"
RESOLVER_SCRIPT = ROOT / ".github" / "scripts" / "floor_resolve.py"

_FLOOR_GATE_RUN = "python -I " + GATE_SCRIPT.relative_to(ROOT).as_posix()
_FLOOR_RESOLVE_RUN = "python3 " + RESOLVER_SCRIPT.relative_to(ROOT).as_posix()
_FLOOR_SETUP_USES = "actions/setup-python@v5"

# Exact key whitelists for the floor_syntax job (TASK-260918-16r0fm closes
# BUG-260917-3txerf rev-2 finding F3): a blacklist can only forbid spellings
# someone already thought of, so the permitted shape is pinned as sets and
# any added or renamed key fails _assert_floor_shape.
_FLOOR_JOB_KEYS = frozenset({"name", "if", "runs-on", "timeout-minutes", "steps"})
_FLOOR_STEP_NAMES = (
    "Checkout",
    "Resolve the floor Python from requires-python",
    "Set up floor Python ${{ steps.floor.outputs.version }}",
    "Compile the package on the floor interpreter",
)
_FLOOR_CHECKOUT_KEYS = frozenset({"name", "uses"})
_FLOOR_RESOLVE_KEYS = frozenset({"name", "id", "run"})
_FLOOR_SETUP_KEYS = frozenset({"name", "uses", "with"})
_FLOOR_COMPILE_KEYS = frozenset({"name", "env", "run", "shell"})

# The workflow-level surfaces inherited into every floor step (revision-1
# finding whitelist-scope-job-dict-only): top-level `defaults.run` and
# top-level `env` apply to steps that do not override them, so the
# whitelist pins them here, not just the job dict.
_WORKFLOW_TOP_ENV_KEYS = frozenset({"RELEASED_SUITE_PIN"})

# The incident class: a backslash inside an f-string expression (PEP 701)
# parses from 3.12 but is a SyntaxError on the 3.11 floor.
_PEP701_MODULE = 'def bad(value):\n    return f"{value.strip(\' \\t\')}"\n'


def _floor_job_dict() -> dict:
    data = yaml.safe_load(_workflow())
    job = data["jobs"]["floor_syntax"]
    assert isinstance(job, dict)
    return job


def _floor_workflow_dict() -> dict:
    data = yaml.safe_load(_workflow())
    assert isinstance(data, dict)
    return data


def _assert_floor_shape(job: dict) -> None:
    """Whitelist the floor job shape: exact keys, exact wiring, one command."""
    assert set(job) == _FLOOR_JOB_KEYS, set(job)
    steps = job["steps"]
    assert isinstance(steps, list)
    assert [step.get("name") for step in steps] == list(_FLOOR_STEP_NAMES)

    checkout, resolve, setup, compile_step = steps
    assert set(checkout) == _FLOOR_CHECKOUT_KEYS, set(checkout)
    assert checkout.get("uses") == "actions/checkout@v4"

    assert set(resolve) == _FLOOR_RESOLVE_KEYS, set(resolve)
    floor_id = resolve.get("id")
    assert isinstance(floor_id, str) and floor_id, "resolve step carries no id"
    expected_ref = f"${{{{ steps.{floor_id}.outputs.version }}}}"
    assert resolve.get("run") == _FLOOR_RESOLVE_RUN

    assert set(setup) == _FLOOR_SETUP_KEYS, set(setup)
    assert setup.get("uses") == _FLOOR_SETUP_USES
    assert setup.get("with") == {"python-version": expected_ref}

    assert set(compile_step) == _FLOOR_COMPILE_KEYS, set(compile_step)
    assert compile_step.get("shell") == "bash"
    assert compile_step.get("env") == {"FLOOR": expected_ref}
    assert compile_step.get("run") == _FLOOR_GATE_RUN


def _assert_floor_workflow_surfaces(data: dict) -> None:
    """Whitelist the workflow-level surfaces inherited into the floor steps.

    A clause enforced on the job dict and absent from a surface that reaches
    the same step is a bypass path (revision-1 finding
    ``whitelist-scope-job-dict-only``): a workflow-level
    ``defaults.run.shell`` substitutes the compile step's shell, and a
    workflow-level ``PYTHONPATH`` reaches the gate process, with every
    job-scope assertion green. The workflow therefore carries no top-level
    ``defaults`` at all, and its top-level ``env`` key set is exactly the
    committed one.
    """
    assert "defaults" not in data, sorted(map(str, data))
    env = data.get("env")
    assert isinstance(env, dict)
    assert set(env) == _WORKFLOW_TOP_ENV_KEYS, set(env)


def _mutate_floor_job(old: str, new: str) -> str:
    """Apply one text-level edit inside the floor_syntax job span."""
    workflow = _workflow()
    span = _job(workflow, "floor_syntax")
    assert span.count(old) == 1, old
    start = workflow.index(span)
    return workflow[:start] + span.replace(old, new) + workflow[start + len(span):]


def _mutate_workflow(old: str, new: str) -> str:
    """Apply one text-level edit anywhere in the workflow text."""
    workflow = _workflow()
    assert workflow.count(old) == 1, old
    return workflow.replace(old, new)


def _workflow_with_top_level_defaults_shell() -> str:
    """The X6a mutant: a workflow-level shell the job dict cannot see."""
    return _mutate_workflow(
        "\njobs:\n",
        "\ndefaults:\n  run:\n    shell: bash -n {0}\n\njobs:\n",
    )


def _workflow_with_top_level_pythonpath() -> str:
    """The X6b mutant: a workflow-level PYTHONPATH the job dict cannot see."""
    return _mutate_workflow(
        "  RELEASED_SUITE_PIN: 0ed5c691e9208eea52f21db2fc05e226ce3516fd\n",
        "  RELEASED_SUITE_PIN: 0ed5c691e9208eea52f21db2fc05e226ce3516fd\n"
        "  PYTHONPATH: .github/ci\n",
    )


def _run_gate_script(
    script: Path,
    executable: str,
    cwd: Path,
    floor: str | None,
    *,
    isolated: bool = True,
) -> subprocess.CompletedProcess[str]:
    """Execute a gate script the way the workflow invokes the committed one.

    ``PYTHONDONTWRITEBYTECODE`` is scrubbed so the compile result is
    observable the same way on every host (the no-short-circuit test reads
    the bytecode the gate writes for the second target).
    """
    env = {
        key: value
        for key, value in os.environ.items()
        if key not in ("FLOOR", "PYTHONDONTWRITEBYTECODE")
    }
    if floor is not None:
        env["FLOOR"] = floor
    argv = [executable]
    if isolated:
        argv.append("-I")
    argv.append(os.fspath(script))
    return subprocess.run(
        argv,
        cwd=cwd,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )


def _run_gate(
    executable: str, cwd: Path, floor: str | None
) -> subprocess.CompletedProcess[str]:
    """Execute the COMMITTED gate script exactly as the workflow invokes it."""
    return _run_gate_script(GATE_SCRIPT, executable, cwd, floor)


def _run_resolver(
    cwd: Path, outputs: Path | None
) -> subprocess.CompletedProcess[str]:
    """Execute the COMMITTED resolver the way the resolve step invokes it.

    ``outputs=None`` leaves ``GITHUB_OUTPUT`` unset, which the resolver must
    fail closed on.
    """
    env = {
        key: value
        for key, value in os.environ.items()
        if key != "GITHUB_OUTPUT"
    }
    if outputs is not None:
        env["GITHUB_OUTPUT"] = os.fspath(outputs)
    return subprocess.run(
        [sys.executable, os.fspath(RESOLVER_SCRIPT)],
        cwd=cwd,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )


def _write_floor_tree(root: Path, floor: str) -> None:
    """Write a minimal tree the gate accepts: floor plus both sentinels."""
    (root / "src" / "csk").mkdir(parents=True)
    (root / "tests").mkdir(parents=True)
    (root / "pyproject.toml").write_text(
        f'[project]\nrequires-python = ">={floor}"\n', encoding="utf-8"
    )
    (root / "src" / "csk" / "__init__.py").write_text("", encoding="utf-8")
    (root / "tests" / "conftest.py").write_text("", encoding="utf-8")


def _floor_interpreter(major: int, minor: int) -> str | None:
    if (sys.version_info[0], sys.version_info[1]) == (major, minor):
        return sys.executable
    return shutil.which(f"python{major}.{minor}")


def _modern_interpreter() -> tuple[str, str] | None:
    if (sys.version_info[0], sys.version_info[1]) >= (3, 12):
        return sys.executable, f"{sys.version_info[0]}.{sys.version_info[1]}"
    for minor in (14, 13, 12):
        exe = shutil.which(f"python3.{minor}")
        if exe is not None:
            return exe, f"3.{minor}"
    return None


# Specifier lines the floor resolver must fail closed on. The SET is the
# value of the test: never drop, rename or soften one to make a filename
# legal (see test_floor_unsupported_fixture_set_is_unchanged).
_FLOOR_UNSUPPORTED_FIXTURES: tuple[str, ...] = (
    'requires-python = "==3.14.*"',
    'requires-python = ">3.11"',
)


def _floor_unsupported_dir(tmp_path: Path, index: int) -> Path:
    """Scratch directory for one unsupported-specifier fixture.

    Derived from the fixture INDEX, never from the specifier text: the
    text carries characters Windows rejects (``*``, ``>``), so embedding
    it in a path fails the test on windows-latest with WinError 123.
    """
    return tmp_path / f"unsupported-{index}"


# Characters Windows rejects in a path component, plus the trailing-dot /
# trailing-space and reserved-stem rules (WinError 123 family).
_WINDOWS_RESERVED_STEMS = (
    {"con", "prn", "aux", "nul"}
    | {f"com{i}" for i in range(1, 10)}
    | {f"lpt{i}" for i in range(1, 10)}
)


def test_floor_unsupported_fixture_dirs_are_portable(tmp_path: Path) -> None:
    """Every fixture scratch name is legal on every host filesystem.

    Regression for the windows-latest failure of
    test_floor_resolve_script_reads_the_floor_from_requires_python: names
    derived from the specifier text carried ``*`` and ``>``. The class is
    any host-rejected character, so this test pins the whole rule (not the
    two examples) over every name the helper can produce for this set.
    """
    assert _FLOOR_UNSUPPORTED_FIXTURES
    for index in range(len(_FLOOR_UNSUPPORTED_FIXTURES)):
        name = _floor_unsupported_dir(tmp_path, index).name
        assert re.fullmatch(r"[a-z0-9-]+", name) is not None, name
        assert name.lower() not in _WINDOWS_RESERVED_STEMS, name
        assert "." not in name, name
        # The filesystem itself is the oracle for the local host; the
        # character rule above is the proxy for the other hosts.
        _floor_unsupported_dir(tmp_path, index).mkdir()


def test_floor_unsupported_fixture_dirs_are_injective(tmp_path: Path) -> None:
    """Two fixtures must never share one scratch directory.

    A sanitiser that maps two specifiers onto the same slug would silently
    run one fixture twice and never run the other -- worse than the crash
    it replaces, because nothing reports it.
    """
    dirs = [
        _floor_unsupported_dir(tmp_path, index)
        for index in range(len(_FLOOR_UNSUPPORTED_FIXTURES))
    ]
    assert len(set(dirs)) == len(dirs)
    for index, directory in enumerate(dirs):
        directory.mkdir()
        (directory / "index.txt").write_text(str(index), encoding="utf-8")
    for index, directory in enumerate(dirs):
        assert (directory / "index.txt").read_text(encoding="utf-8") == str(index)


def test_floor_unsupported_fixture_set_is_unchanged() -> None:
    """The unsupported-specifier SET is pinned: change names, never fixtures."""
    assert _FLOOR_UNSUPPORTED_FIXTURES == (
        'requires-python = "==3.14.*"',
        'requires-python = ">3.11"',
    )


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
            "floor_syntax",
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


def test_floor_syntax_gate_is_a_cheap_pr_time_floor_compile() -> None:
    """The floor gate runs on pull requests and compiles the package verbatim.

    Regression for BUG-260917-3txerf: the Fast lane runs only Python 3.14,
    so a construct that parses there but not on the declared floor reached
    main unseen. The gate is one ubuntu runner compiling ``src/csk`` and
    ``tests`` with the real floor interpreter -- a syntax gate, not a
    second test matrix. The compile step is exactly one command running the
    committed gate script, so the check and the compile share one process
    and its exit status IS the step status (TASK-260918-16r0fm).
    """
    job = _job(_workflow(), "floor_syntax")
    # Whole-line pins: a substring pin cannot see a narrowed target
    # (``src/csk/builds`` contains ``src/csk``) or a live line kept as a
    # comment, so every load-bearing line is matched newline to newline.
    assert "\n    if: github.event_name == 'pull_request'\n" in job
    assert "refs/heads/main" not in job
    assert "\n    runs-on: ubuntu-latest\n" in job
    assert re.search(r"(?m)^ +(matrix|strategy):", job) is None
    assert "\n    timeout-minutes: 10\n" in job
    assert "actions/checkout@v4" in job
    assert "actions/setup-python@v5" in job
    assert "pip install" not in job

    # The floor comes from requires-python, never hardcoded: pinning a
    # literal version here keeps every other token present while silently
    # reintroducing the drift this gate exists to close.
    assert "\n      - name: Resolve the floor Python from requires-python\n" in job
    assert "\n          python-version: ${{ steps.floor.outputs.version }}\n" in job
    assert 'python-version: "3.11"' not in job
    assert 'python-version: "3.14"' not in job

    # FLOOR reaches the compile step from the resolve step; the structural
    # test pins that the reference names the resolve step's actual id.
    assert "\n          FLOOR: ${{ steps.floor.outputs.version }}\n" in job

    # The resolve step is exactly one command running the committed
    # resolver: a heredoc here let shell text hide around the tested part
    # with every test green (revision-1 floor-source-self-agreeing).
    assert "\n        run: python3 .github/scripts/floor_resolve.py\n" in job
    assert RESOLVER_SCRIPT.is_file()

    # Exactly one command running the committed gate script. The old
    # two-command block (a heredoc self-check plus a ``compileall`` CLI
    # line) let a second run line, a PATH change, ``set +e``, a shell
    # override or ``continue-on-error`` separate the check from the compile
    # with every test green (rev-2 finding F3); the CLI spelling itself is
    # gone because its "Can't list" exit-0 is finding F4. The shell is
    # declared explicitly so no workflow-level default can substitute it,
    # and -I keeps the gate immune to inherited PYTHON* variables. No
    # heredoc is left in this job at all.
    assert "\n        shell: bash\n" in job
    assert "\n        run: python -I .github/scripts/floor_gate.py\n" in job
    assert "python -m compileall" not in job
    assert job.count("<<'PY'") == 0
    assert GATE_SCRIPT.is_file()


def test_floor_resolve_script_reads_the_floor_from_requires_python(
    tmp_path: Path,
) -> None:
    """The COMMITTED resolver maps requires-python to the gate version.

    Token assertions cannot see a script that keeps the ``requires-python``
    token while printing a constant, so this test executes the committed
    resolver file -- the same bytes the resolve step runs and the gate
    resolves through: the committed tree resolves ``3.11``, a raised floor
    moves the gate to ``3.12``, a specifier without a ``>=`` bound fails
    closed, and the read is scoped to the PEP 621 ``[project]`` table so a
    ``requires-python`` line under any other table cannot decide the floor.
    """
    committed_out = tmp_path / "committed.github_output"
    committed = _run_resolver(ROOT, committed_out)
    assert committed.returncode == 0
    assert committed_out.read_text(encoding="utf-8") == "version=3.11\n"

    raised = tmp_path / "raised"
    raised.mkdir()
    (raised / "pyproject.toml").write_text(
        '[project]\nrequires-python = ">=3.12"\n', encoding="utf-8"
    )
    raised_out = tmp_path / "raised.github_output"
    moved = _run_resolver(raised, raised_out)
    assert moved.returncode == 0
    assert raised_out.read_text(encoding="utf-8") == "version=3.12\n"

    for index, bad in enumerate(_FLOOR_UNSUPPORTED_FIXTURES):
        disarmed = _floor_unsupported_dir(tmp_path, index)
        disarmed.mkdir()
        (disarmed / "pyproject.toml").write_text(
            f"[project]\n{bad}\n", encoding="utf-8"
        )
        proc = _run_resolver(disarmed, tmp_path / f"unsupported-{index}.out")
        assert proc.returncode != 0, bad

    other_first = tmp_path / "other-table-first"
    other_first.mkdir()
    (other_first / "pyproject.toml").write_text(
        '[tool.foo]\nrequires-python = ">=3.9"\n'
        '[project]\nrequires-python = ">=3.11"\n',
        encoding="utf-8",
    )
    other_out = tmp_path / "other-table-first.out"
    scoped = _run_resolver(other_first, other_out)
    assert scoped.returncode == 0
    assert other_out.read_text(encoding="utf-8") == "version=3.11\n"

    tool_only = tmp_path / "tool-only"
    tool_only.mkdir()
    (tool_only / "pyproject.toml").write_text(
        '[tool.foo]\nrequires-python = ">=3.9"\n', encoding="utf-8"
    )
    missing = _run_resolver(tool_only, tmp_path / "tool-only.out")
    assert missing.returncode != 0

    unset = _run_resolver(ROOT, None)
    assert unset.returncode != 0
    assert "GITHUB_OUTPUT is empty" in unset.stderr


def test_floor_syntax_wiring_is_structural_and_self_verifying(
    tmp_path: Path,
) -> None:
    """The floor gate proves it runs ON the resolved floor, not just somewhere.

    Regression for BUG-260917-3txerf review finding F1: the resolve step's
    output was consumed by setup-python and nothing downstream checked the
    interpreter executing the compile, so deleting the resolve id,
    renaming it, or inserting a second setup-python kept every text pin
    green while the job fell through to the runner-default interpreter.
    The gate therefore resolves the floor from pyproject.toml itself and
    fails closed unless it equals both FLOOR and its own major.minor (FLOOR
    compared against the interpreter FLOOR selected was satisfied by
    construction -- revision-1 floor-source-self-agreeing). This test pins
    the job shape as exact key whitelists over the parsed YAML (ids read,
    not spelled; any added or renamed key fails -- rev-2 finding F3 showed
    a blacklist of spellings cannot close the advisory-disarm class), pins
    the workflow-level surfaces inherited into the steps, and executes the
    COMMITTED gate script four ways: the same bytes the workflow runs.
    """
    _assert_floor_shape(_floor_job_dict())
    _assert_floor_workflow_surfaces(_floor_workflow_dict())

    running = f"{sys.version_info[0]}.{sys.version_info[1]}"

    unset = _run_gate(sys.executable, ROOT, None)
    assert unset.returncode != 0
    assert "FLOOR is empty" in unset.stderr

    mismatched_floor = "9.9" if running != "9.9" else "8.8"
    mismatched = _run_gate(sys.executable, ROOT, mismatched_floor)
    assert mismatched.returncode != 0
    assert "floor mismatch" in mismatched.stderr

    matched_tree = tmp_path / "matched"
    _write_floor_tree(matched_tree, running)
    matched = _run_gate(sys.executable, matched_tree, running)
    assert matched.returncode == 0
    assert f"floor_compiled={running}" in matched.stdout

    # The committed tree declares the 3.11 floor: on the floor interpreter
    # the gate attests the real tree, off it the gate refuses to attest a
    # floor it is not running on -- the F1 remedy on the committed tree.
    committed = _run_gate(sys.executable, ROOT, "3.11")
    if running == "3.11":
        assert committed.returncode == 0
        assert "floor_compiled=3.11" in committed.stdout
    else:
        assert committed.returncode != 0
        assert "floor mismatch" in committed.stderr


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
            "/protocol-spec-draft/conformance/skillfile-sources-v1" in job
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
    assert pin["revision"] == "574636785c9da22757095ca279e8a9da801156ec"
    assert pin["suite_root"] == "conformance/skillfile-sources-v1"
    assert pin["files"] == {
        "index.json": (
            "sha256:654707af529bffc5104e92e98d4ab6fb910163b187861152bfd7fa769d852575"
        ),
        "semantic-cases.json": (
            "sha256:12ae1318a25a96a21333bccb65b07fcd70b8bb1bf165bbd4b006f092806488c8"
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


def test_floor_evasion_exit_zero_first_line_is_rejected() -> None:
    """T9 (rev-2 F3): ``exit 0`` as the first run line fails the whitelist.

    On the old two-command block this exited the step 0 before either the
    check or the compile ran, with all committed tests green. The run
    block is now exactly one command, so any added line is rejected.
    """
    mutated = _mutate_floor_job(
        "        run: python -I .github/scripts/floor_gate.py\n",
        "        run: |\n"
        "          exit 0\n"
        "          python -I .github/scripts/floor_gate.py\n",
    )
    job = yaml.safe_load(mutated)["jobs"]["floor_syntax"]
    with pytest.raises(AssertionError):
        _assert_floor_shape(job)


def test_floor_evasion_path_change_between_check_and_compile_is_rejected() -> None:
    """T11b (rev-2 F3): a PATH line next to the gate fails the whitelist.

    On the old shape a PATH change between the self-check and ``compileall``
    made the check certify one interpreter while another did the compiling.
    One process has no between; any second run line is rejected.
    """
    mutated = _mutate_floor_job(
        "        run: python -I .github/scripts/floor_gate.py\n",
        "        run: |\n"
        '          export PATH="/tmp/evil:$PATH"\n'
        "          python -I .github/scripts/floor_gate.py\n",
    )
    job = yaml.safe_load(mutated)["jobs"]["floor_syntax"]
    with pytest.raises(AssertionError):
        _assert_floor_shape(job)


def test_floor_evasion_set_plus_e_is_rejected() -> None:
    """T8 (rev-2 F3): ``set +e`` as the first run line fails the whitelist.

    On the old shape this demoted the self-check from a gate to a report:
    with wiring drift the log said ``FLOOR is empty`` and the step still
    exited 0. A single command has no ``-e`` dependence to remove.
    """
    mutated = _mutate_floor_job(
        "        run: python -I .github/scripts/floor_gate.py\n",
        "        run: |\n"
        "          set +e\n"
        "          python -I .github/scripts/floor_gate.py\n",
    )
    job = yaml.safe_load(mutated)["jobs"]["floor_syntax"]
    with pytest.raises(AssertionError):
        _assert_floor_shape(job)


def test_floor_evasion_shell_override_is_rejected() -> None:
    """T10 (rev-2 F3): ``shell: bash {0}`` on the step fails the whitelist.

    On the old shape dropping ``-e`` had the same effect as ``set +e``.
    The compile step declares ``shell: bash`` exactly, so substituting the
    value is rejected; the step's key set is exactly
    ``{name, env, run, shell}``, so any added step key --
    ``working-directory``, ``if``, ``continue-on-error`` -- is rejected too.
    """
    mutated = _mutate_floor_job(
        "        shell: bash\n",
        "        shell: bash {0}\n",
    )
    job = yaml.safe_load(mutated)["jobs"]["floor_syntax"]
    with pytest.raises(AssertionError):
        _assert_floor_shape(job)


def test_floor_evasion_job_continue_on_error_is_rejected() -> None:
    """T12 (rev-2 F3): job-level ``continue-on-error`` fails the whitelist.

    The compile can fail while ``needs.floor_syntax.result`` still reports
    ``success`` to the ``fast`` aggregate. The job's key set is exactly
    ``{name, if, runs-on, timeout-minutes, steps}``.
    """
    mutated = _mutate_floor_job(
        "    runs-on: ubuntu-latest\n    timeout-minutes: 10\n",
        "    runs-on: ubuntu-latest\n"
        "    continue-on-error: true\n"
        "    timeout-minutes: 10\n",
    )
    job = yaml.safe_load(mutated)["jobs"]["floor_syntax"]
    with pytest.raises(AssertionError):
        _assert_floor_shape(job)


@pytest.mark.parametrize(
    "relocation",
    [
        pytest.param("        with:\n          path: checkout\n", id="path"),
        pytest.param(
            "        with:\n          sparse-checkout: tests\n", id="sparse"
        ),
    ],
)
def test_floor_evasion_checkout_relocation_is_rejected(relocation: str) -> None:
    """T13/T26 (rev-2 F4): the checkout step takes no ``with:`` at all.

    A relocated or sparse checkout leaves the spelled targets unlistable,
    and ``compileall`` treats that as empty and exits 0. The checkout
    step's key set is exactly ``{name, uses}``; even a well-formed
    relocation is rejected statically, and the gate's sentinel check fails
    it closed at run time if it ever gets that far.
    """
    mutated = _mutate_floor_job(
        "      - name: Checkout\n        uses: actions/checkout@v4\n",
        "      - name: Checkout\n        uses: actions/checkout@v4\n" + relocation,
    )
    job = yaml.safe_load(mutated)["jobs"]["floor_syntax"]
    with pytest.raises(AssertionError):
        _assert_floor_shape(job)


def test_floor_gate_proves_each_target_present_before_compiling(
    tmp_path: Path,
) -> None:
    """The COMMITTED gate script fails on absent and on present-but-empty targets.

    Regression for rev-2 finding F4: ``compileall`` treats a target it
    cannot list as empty and exits 0, so a working-directory change, a
    sparse or relocated checkout, or a moved package left the gate green
    while it proved nothing. Each target is now proved by a named sentinel
    read (``src/csk/__init__.py``, ``tests/conftest.py``) before compiling.
    Every scratch tree carries a matching floor so the failure asserted is
    the target stage, not the floor stage.
    """
    running = f"{sys.version_info[0]}.{sys.version_info[1]}"

    absent = tmp_path / "absent"
    absent.mkdir()
    (absent / "pyproject.toml").write_text(
        f'[project]\nrequires-python = ">={running}"\n', encoding="utf-8"
    )
    missing = _run_gate(sys.executable, absent, running)
    assert missing.returncode != 0
    assert "compile target missing" in missing.stderr

    empty = tmp_path / "empty"
    (empty / "src" / "csk").mkdir(parents=True)
    (empty / "tests").mkdir(parents=True)
    (empty / "pyproject.toml").write_text(
        f'[project]\nrequires-python = ">={running}"\n', encoding="utf-8"
    )
    hollow = _run_gate(sys.executable, empty, running)
    assert hollow.returncode != 0
    assert "compile target missing" in hollow.stderr

    real = tmp_path / "real"
    _write_floor_tree(real, running)
    (real / "src" / "csk" / "ok.py").write_text("x = 1\n", encoding="utf-8")
    (real / "tests" / "test_ok.py").write_text("x = 1\n", encoding="utf-8")
    present = _run_gate(sys.executable, real, running)
    assert present.returncode == 0


def test_floor_gate_compiles_every_target_before_deciding(tmp_path: Path) -> None:
    """The gate compiles the second target even when the first one fails.

    The reviewer's sketch used ``all(compile_dir(t) ...)``, which
    short-circuits: a failure in the first target leaves the second
    uncompiled with its state unknown. The committed gate compiles every
    target, then decides -- proved here by the bytecode the run leaves
    behind for the good target while still exiting 1 for the bad one.
    """
    running = f"{sys.version_info[0]}.{sys.version_info[1]}"
    root = tmp_path / "tree"
    _write_floor_tree(root, running)
    (root / "src" / "csk" / "bad.py").write_text(
        "def broken(:\n    pass\n", encoding="utf-8"
    )
    (root / "tests" / "test_ok.py").write_text("x = 1\n", encoding="utf-8")

    proc = _run_gate(sys.executable, root, running)
    assert proc.returncode != 0
    assert "floor compile failed" in proc.stderr
    compiled = list((root / "tests").glob("__pycache__/*.pyc"))
    assert compiled, "tests/ was never compiled: the gate short-circuits"


def test_floor_gate_fails_when_tests_do_not_compile(tmp_path: Path) -> None:
    """A compile failure under ``tests/`` alone fails the gate.

    Narrowing guard for the target set: a gate that compiled only
    ``src/csk`` keeps every other token while dropping the tree whose
    collection errors were the incident's visible symptom.
    """
    running = f"{sys.version_info[0]}.{sys.version_info[1]}"
    root = tmp_path / "tree"
    _write_floor_tree(root, running)
    (root / "src" / "csk" / "ok.py").write_text("x = 1\n", encoding="utf-8")
    (root / "tests" / "test_broken.py").write_text(
        "def broken(:\n    pass\n", encoding="utf-8"
    )

    proc = _run_gate(sys.executable, root, running)
    assert proc.returncode != 0
    assert "floor compile failed" in proc.stderr


def test_floor_gate_rejects_a_real_312_only_construct(tmp_path: Path) -> None:
    """The gate still refuses the accidental case it was built for: PEP 701.

    A backslash inside an f-string expression parses from Python 3.12 but
    is a SyntaxError on the 3.11 floor. The planted module must fail the
    gate on a real 3.11 interpreter and pass it on a 3.12+ one -- the
    contrast proves the fixture is the version-sensitive class rather
    than a universal syntax error. (The 3.11 run also proves the gate
    script itself parses on the floor it runs on: a gate SyntaxError
    reports the gate file, never ``floor compile failed``.)

    Bound: the floor half skips wherever ``python3.11`` is not on PATH --
    every hosted Fast lane (3.14 only) and windows-latest -- and executes
    on the Merge 3.11 lanes and on developer hosts with 3.11. The PR-time
    protection for the class on the skipping hosts is the floor job itself.
    """
    root = tmp_path / "tree"
    _write_floor_tree(root, "3.11")
    (root / "src" / "csk" / "pep701.py").write_text(_PEP701_MODULE, encoding="utf-8")
    (root / "tests" / "test_ok.py").write_text("x = 1\n", encoding="utf-8")

    floor = _floor_interpreter(3, 11)
    if floor is None:
        pytest.skip("no Python 3.11 interpreter on this host")
    refused = _run_gate(floor, root, "3.11")
    assert refused.returncode != 0
    assert "floor compile failed" in refused.stderr
    assert "pep701.py" in refused.stderr + refused.stdout

    modern = _modern_interpreter()
    if modern is not None:
        executable, tag = modern
        (root / "pyproject.toml").write_text(
            f'[project]\nrequires-python = ">={tag}"\n', encoding="utf-8"
        )
        admitted = _run_gate(executable, root, tag)
        assert admitted.returncode == 0


def test_floor_evasion_resolve_run_extra_line_is_rejected() -> None:
    """X6c (revision-1 F1): an appended GITHUB_OUTPUT write fails the pin.

    The resolve step's run value is pinned whole, exactly like the compile
    step's: the old heredoc let an ``echo "version=3.14"`` line after the
    terminator win last-write on the runner with every committed test
    green, resolving, installing, and attesting 3.14 over a 3.11 tree.
    """
    mutated = _mutate_floor_job(
        "        run: python3 .github/scripts/floor_resolve.py\n",
        "        run: |\n"
        "          python3 .github/scripts/floor_resolve.py\n"
        '          echo "version=3.14" >> "$GITHUB_OUTPUT"\n',
    )
    job = yaml.safe_load(mutated)["jobs"]["floor_syntax"]
    with pytest.raises(AssertionError):
        _assert_floor_shape(job)


def test_floor_evasion_resolve_run_pipe_is_rejected() -> None:
    """X6d (revision-1 F1): a piped resolve command fails the pin.

    The heredoc body survived this edit untouched while ``sed`` rewrote
    the output to 3.14 with no last-wins dependence. This form starts with
    the exact pinned string, so it also kills a ``startswith`` narrowing
    of the resolve-run pin.
    """
    mutated = _mutate_floor_job(
        "        run: python3 .github/scripts/floor_resolve.py\n",
        "        run: python3 .github/scripts/floor_resolve.py | sed 's/3\\.11/3.14/'\n",
    )
    job = yaml.safe_load(mutated)["jobs"]["floor_syntax"]
    with pytest.raises(AssertionError):
        _assert_floor_shape(job)


def test_floor_evasion_resolve_run_redirect_and_echo_is_rejected() -> None:
    """X6e (revision-1 F1): redirect-to-null plus echo fails the pin.

    The resolver output discarded, the version minted by hand, every
    committed test green. The whole-value pin rejects any shell around the
    one command.
    """
    mutated = _mutate_floor_job(
        "        run: python3 .github/scripts/floor_resolve.py\n",
        "        run: |\n"
        "          python3 .github/scripts/floor_resolve.py > /dev/null\n"
        '          echo "version=3.14" >> "$GITHUB_OUTPUT"\n',
    )
    job = yaml.safe_load(mutated)["jobs"]["floor_syntax"]
    with pytest.raises(AssertionError):
        _assert_floor_shape(job)


def test_floor_evasion_workflow_default_shell_is_rejected() -> None:
    """X6a (revision-1 F2): a workflow-level shell fails the surfaces pin.

    ``defaults.run.shell: bash -n {0}`` leaves the job and step dicts
    unchanged (and actionlint clean) while the runner never executes the
    step script: the step exits 0 with the gate never run. The job-scope
    whitelist cannot see this edit; the workflow-level pin rejects it.
    """
    data = yaml.safe_load(_workflow_with_top_level_defaults_shell())
    with pytest.raises(AssertionError):
        _assert_floor_workflow_surfaces(data)


def test_floor_evasion_workflow_env_pythonpath_is_rejected() -> None:
    """X6b (revision-1 F2): a workflow-level PYTHONPATH fails the pin.

    Top-level ``env`` is inherited into the gate process, and a shadow
    module on that path turned the gate green with every committed test
    green. The top-level ``env`` key set is exactly the committed one, and
    the gate additionally runs under ``-I``, which ignores ``PYTHON*``.
    """
    data = yaml.safe_load(_workflow_with_top_level_pythonpath())
    with pytest.raises(AssertionError):
        _assert_floor_workflow_surfaces(data)


def test_floor_gate_refuses_a_floor_it_is_not_running_on(
    tmp_path: Path,
) -> None:
    """pyproject 3.11 + interpreter 3.14 + FLOOR=3.14 exits 1 (F1 end to end).

    The revision-1 bypass replayed end to end: a tampered resolve step
    reports 3.14, setup-python installs 3.14, and the gate used to exit 0
    over a tree carrying the PEP-701 module. The gate now resolves 3.11
    from pyproject.toml itself and refuses before compiling anything.
    """
    modern = _modern_interpreter()
    if modern is None:
        pytest.skip("no Python 3.12+ interpreter on this host")
    executable, tag = modern
    root = tmp_path / "tree"
    _write_floor_tree(root, "3.11")
    (root / "src" / "csk" / "pep701.py").write_text(
        _PEP701_MODULE, encoding="utf-8"
    )
    proc = _run_gate(executable, root, tag)
    assert proc.returncode != 0
    assert "floor mismatch" in proc.stderr


def test_floor_gate_trusts_pyproject_not_floor(tmp_path: Path) -> None:
    """FLOOR agreeing with the interpreter is never enough by itself.

    Always-on killer for the revision-1 ``floor-source-self-agreeing``
    class: a synthetic floor no running interpreter equals, with FLOOR
    set to the running version, exits 1; an interpreter on the resolved
    floor with a disagreeing FLOOR exits 1 (the cross-check); a FLOOR that
    agrees with the resolved floor on the wrong interpreter exits 1 (the
    identity check); and a tree without pyproject.toml at all exits 1
    (fail closed). A gate that trusts FLOOR without reading pyproject
    exits 0 on the first scenario (committed mutant M1 below).
    """
    running = f"{sys.version_info[0]}.{sys.version_info[1]}"
    other = "9.9" if running != "9.9" else "8.8"

    foreign = tmp_path / "foreign-floor"
    _write_floor_tree(foreign, other)
    proc = _run_gate(sys.executable, foreign, running)
    assert proc.returncode != 0
    assert "floor mismatch" in proc.stderr

    own = tmp_path / "own-floor"
    _write_floor_tree(own, running)
    crossed = _run_gate(sys.executable, own, other)
    assert crossed.returncode != 0
    assert "floor mismatch" in crossed.stderr

    agreed = _run_gate(sys.executable, foreign, other)
    assert agreed.returncode != 0
    assert "floor mismatch" in agreed.stderr

    bare = tmp_path / "no-pyproject"
    (bare / "src" / "csk").mkdir(parents=True)
    (bare / "tests").mkdir(parents=True)
    missing = _run_gate(sys.executable, bare, running)
    assert missing.returncode != 0
    assert "pyproject.toml" in missing.stderr


def _require_posix_permission_bits() -> None:
    """Skip permission-bit tests where the bits do not bind."""
    if sys.platform == "win32":
        pytest.skip("POSIX permission bits have no effect on win32")
    if hasattr(os, "geteuid") and os.geteuid() == 0:
        pytest.skip("permission bits do not bind uid 0")


def test_floor_gate_fails_when_a_target_is_unlistable(tmp_path: Path) -> None:
    """A target the gate cannot list fails it; 'Can't list' is never success.

    Residual of rev-2 F4 (revision-1
    ``unlistable-target-read-failure-as-absence``): traverse-but-not-read
    permission keeps ``isdir`` and the sentinel ``isfile`` true while
    ``compileall`` reports ``Can't list`` and returns success, and no
    sentinel covers subdirectories at all. The gate walks every target
    with a raising ``onerror`` and reads each sentinel, so an unlistable
    target, an unlistable subdirectory, and an unreadable sentinel each
    exit 1 with the listing reason -- never 0, never a compile reason.
    """
    _require_posix_permission_bits()
    running = f"{sys.version_info[0]}.{sys.version_info[1]}"

    locked = tmp_path / "locked-target"
    _write_floor_tree(locked, running)
    (locked / "src" / "csk" / "ok.py").write_text("x = 1\n", encoding="utf-8")
    (locked / "src" / "csk").chmod(0o111)
    try:
        proc = _run_gate(sys.executable, locked, running)
    finally:
        (locked / "src" / "csk").chmod(0o755)
    assert proc.returncode != 0
    assert "unlistable" in proc.stderr

    nested = tmp_path / "locked-subdir"
    _write_floor_tree(nested, running)
    fixtures = nested / "tests" / "fixtures"
    fixtures.mkdir()
    (fixtures / "test_hidden.py").write_text(
        "def broken(:\n    pass\n", encoding="utf-8"
    )
    (nested / "tests" / "test_ok.py").write_text("x = 1\n", encoding="utf-8")
    fixtures.chmod(0o000)
    try:
        proc = _run_gate(sys.executable, nested, running)
    finally:
        fixtures.chmod(0o755)
    assert proc.returncode != 0
    assert "unlistable" in proc.stderr

    sealed = tmp_path / "unreadable-sentinel"
    _write_floor_tree(sealed, running)
    sentinel = sealed / "src" / "csk" / "__init__.py"
    sentinel.chmod(0o000)
    try:
        proc = _run_gate(sys.executable, sealed, running)
    finally:
        sentinel.chmod(0o644)
    assert proc.returncode != 0
    assert "unreadable" in proc.stderr


@pytest.mark.parametrize(
    ("old", "new"),
    [
        pytest.param(
            "    runs-on: ubuntu-latest\n    timeout-minutes: 10\n",
            "    runs-on: ubuntu-latest\n"
            "    x-never-seen: 1\n"
            "    timeout-minutes: 10\n",
            id="job-unknown-key",
        ),
        pytest.param(
            "      - name: Checkout\n        uses: actions/checkout@v4\n",
            "      - name: Checkout\n"
            "        uses: actions/checkout@v4\n"
            "        working-directory: /tmp\n",
            id="checkout-unknown-key",
        ),
        pytest.param(
            "        uses: actions/checkout@v4\n",
            "        uses: actions/checkout@v3\n",
            id="checkout-uses-value",
        ),
        pytest.param(
            "      - name: Resolve the floor Python from requires-python\n",
            "      - name: Resolve the floor Python from requires-python\n"
            "        shell: bash\n",
            id="resolve-unknown-key",
        ),
        pytest.param(
            "      - name: Set up floor Python ${{ steps.floor.outputs.version }}\n",
            "      - name: Set up floor Python ${{ steps.floor.outputs.version }}\n"
            "        shell: bash\n",
            id="setup-unknown-key",
        ),
        pytest.param(
            "        uses: actions/setup-python@v5\n",
            "        uses: ./tools/setup-python\n",
            id="setup-uses-substring-trap",
        ),
        pytest.param(
            "          python-version: ${{ steps.floor.outputs.version }}\n",
            "          python-version: ${{ steps.floor.outputs.version }}\n"
            "          allow-prereleases: true\n",
            id="setup-with-extra-entry",
        ),
        pytest.param(
            "          python-version: ${{ steps.floor.outputs.version }}\n",
            "          python-version: '3.14'\n",
            id="setup-with-value",
        ),
        pytest.param(
            "      - name: Compile the package on the floor interpreter\n",
            "      - name: Compile the package on the floor interpreter\n"
            "        working-directory: /tmp\n",
            id="compile-unknown-key",
        ),
        pytest.param(
            "          FLOOR: ${{ steps.floor.outputs.version }}\n",
            "          FLOOR: ${{ steps.floor.outputs.version }}\n"
            "          PYTHONPATH: /tmp/evil\n",
            id="compile-env-extra-entry",
        ),
        pytest.param(
            "          FLOOR: ${{ steps.floor.outputs.version }}\n",
            "          FLOOR: '3.14'\n",
            id="compile-env-value",
        ),
        pytest.param(
            "        run: python -I .github/scripts/floor_gate.py\n",
            "        run: python -I .github/scripts/floor_gate.py || true\n",
            id="compile-run-or-true-suffix",
        ),
        pytest.param(
            "        run: python -I .github/scripts/floor_gate.py\n",
            "        run: python -I .github/scripts/floor_gate.py\n"
            "      - name: Extra step\n"
            "        run: echo hi\n",
            id="added-step",
        ),
    ],
)
def test_floor_shape_clause_has_a_committed_killer(old: str, new: str) -> None:
    """Every whitelist clause has a committed negative case (revision-1 N2).

    A whitelist whose clauses can be narrowed unnoticed decays into the
    blacklist this leaf exists to replace: disabling or narrowing any one
    clause -- an unknown key on the job or on any step, a tampered
    ``uses``/``with``/``env``/``run`` value, an extra ``with``/``env``
    entry, a ``|| true`` suffix, an added step -- must fail exactly the
    param below that exercises it. The ``setup-uses-substring-trap`` param
    carries the ``setup-python`` token while naming a different action, so
    it kills both a disabled and a substring-narrowed ``uses`` pin.
    """
    mutated = _mutate_floor_job(old, new)
    job = yaml.safe_load(mutated)["jobs"]["floor_syntax"]
    with pytest.raises(AssertionError):
        _assert_floor_shape(job)


def test_floor_gate_rejects_a_non_exact_floor(tmp_path: Path) -> None:
    """FLOOR must equal the resolved floor exactly -- no prefix, no patch.

    Killer for the prefix and major-only narrowings of the identity check:
    ``3`` is admitted by ``startswith`` and by a major-only comparison
    while ``3.11.0`` and padded forms probe the other direction. Each exits
    1 with a mismatch, on any host interpreter.
    """
    running = f"{sys.version_info[0]}.{sys.version_info[1]}"
    root = tmp_path / "tree"
    _write_floor_tree(root, running)
    bad_floors = {"3", f"{running}.0", f" {running}", f"{running}\n"}
    assert all(bad != running for bad in bad_floors)
    for bad in sorted(bad_floors):
        proc = _run_gate(sys.executable, root, bad)
        assert proc.returncode != 0, bad
        assert "floor mismatch" in proc.stderr, bad


def test_floor_gate_requires_the_second_sentinel(tmp_path: Path) -> None:
    """``tests/`` missing while ``src/csk`` is present fails the gate.

    The three-way test fails on the first target before the second is ever
    consulted, so a gate that checks only the first sentinel keeps it
    green; this case names ``tests`` in the failure and kills that
    narrowing.
    """
    running = f"{sys.version_info[0]}.{sys.version_info[1]}"
    root = tmp_path / "tree"
    (root / "src" / "csk").mkdir(parents=True)
    (root / "pyproject.toml").write_text(
        f'[project]\nrequires-python = ">={running}"\n', encoding="utf-8"
    )
    (root / "src" / "csk" / "__init__.py").write_text("", encoding="utf-8")
    proc = _run_gate(sys.executable, root, running)
    assert proc.returncode != 0
    assert "compile target missing" in proc.stderr
    assert "tests" in proc.stderr


def _mutant_gate_source(old: str, new: str) -> str:
    """Derive a narrowed gate from the COMMITTED gate bytes.

    The anchor must match exactly once: if the gate is refactored so the
    anchor drifts, the mutant test errors loudly instead of silently
    executing the unmutated gate.
    """
    source = GATE_SCRIPT.read_text(encoding="utf-8")
    assert source.count(old) == 1, old
    return source.replace(old, new)


def _write_mutant_gate(source: str, directory: Path) -> Path:
    """Write a mutant gate next to a copy of the committed resolver bytes.

    The gate loads its resolver by file location from its own directory,
    so the mutant directory carries the committed resolver unchanged.
    """
    directory.mkdir(parents=True, exist_ok=True)
    (directory / RESOLVER_SCRIPT.name).write_text(
        RESOLVER_SCRIPT.read_text(encoding="utf-8"), encoding="utf-8"
    )
    mutant = directory / "floor_gate_mutant.py"
    mutant.write_text(source, encoding="utf-8")
    return mutant


def test_floor_mutant_gate_trusting_floor_alone_is_killed(
    tmp_path: Path,
) -> None:
    """Narrowing mutant M1 (remedy 1): resolved=FLOOR admits a wrong floor.

    The mutant keeps every check but resolves nothing: it can only be told
    apart from the committed gate by a tree whose floor disagrees with
    FLOOR. On the trust test's scenario the mutant exits 0 (it is
    narrowing, and the assertion proves it) while the committed gate exits
    1 (the named test kills it).
    """
    running = f"{sys.version_info[0]}.{sys.version_info[1]}"
    other = "9.9" if running != "9.9" else "8.8"
    root = tmp_path / "tree"
    _write_floor_tree(root, other)
    mutant = _write_mutant_gate(
        _mutant_gate_source(
            "    resolved = _resolve_floor_from_pyproject()\n",
            "    resolved = floor  # MUTANT M1: trusts FLOOR, never reads pyproject\n",
        ),
        tmp_path / "mutant",
    )
    admitted = _run_gate_script(mutant, sys.executable, root, running)
    assert admitted.returncode == 0, admitted.stderr
    refused = _run_gate(sys.executable, root, running)
    assert refused.returncode != 0


def test_floor_mutant_job_scope_only_whitelist_admits_workflow_defaults() -> None:
    """Narrowing mutant M2 (remedy 2): the rev1 scope admits X6a.

    The mutant is not a weakened copy -- it is the previous scope itself:
    ``_assert_floor_shape`` over the job dict accepts the workflow carrying
    top-level ``defaults.run.shell`` (the job dict is unchanged), while the
    committed ``_assert_floor_workflow_surfaces`` rejects it. This proves
    the top-level clause load-bearing, not merely present.
    """
    mutated = _workflow_with_top_level_defaults_shell()
    data = yaml.safe_load(mutated)
    _assert_floor_shape(data["jobs"]["floor_syntax"])
    with pytest.raises(AssertionError):
        _assert_floor_workflow_surfaces(data)


def test_floor_mutant_gate_without_listability_walk_is_killed(
    tmp_path: Path,
) -> None:
    """Narrowing mutant M3 (remedy 3): ``onerror`` dropped, 'Can't list' wins.

    The mutant walks each target exactly as the committed gate does but
    swallows listing errors, so ``compileall``'s treat-unlistable-as-empty
    decides the outcome again. On the unlistable-target tree the mutant
    exits 0 (it is narrowing, and the assertion proves it) while the
    committed gate exits 1 with the listing reason (the named test kills
    it).
    """
    _require_posix_permission_bits()
    running = f"{sys.version_info[0]}.{sys.version_info[1]}"
    root = tmp_path / "tree"
    _write_floor_tree(root, running)
    (root / "src" / "csk").chmod(0o111)
    try:
        mutant = _write_mutant_gate(
            _mutant_gate_source(
                "os.walk(target, onerror=_fail_on_list_error):",
                "os.walk(target):  # MUTANT M3: listing errors swallowed",
            ),
            tmp_path / "mutant",
        )
        admitted = _run_gate_script(mutant, sys.executable, root, running)
    finally:
        (root / "src" / "csk").chmod(0o755)
    assert admitted.returncode == 0, admitted.stderr
    (root / "src" / "csk").chmod(0o111)
    try:
        refused = _run_gate(sys.executable, root, running)
    finally:
        (root / "src" / "csk").chmod(0o755)
    assert refused.returncode != 0
    assert "unlistable" in refused.stderr


def test_floor_gate_is_immune_to_a_smuggled_compileall(tmp_path: Path) -> None:
    """``python -I`` refuses import hijack via a shadowing compileall (N1).

    A ``compileall.py`` that always reports success, placed next to the
    gate, turns the gate green on a tree with a broken module when the
    gate runs as ``python gate.py`` -- the attack is real, and the first
    assertion proves it. The committed ``python -I`` invocation refuses
    the shadow (isolated mode keeps the script directory off ``sys.path``)
    and fails on the broken module. This pins the ``-I`` token
    load-bearing, not decorative.
    """
    running = f"{sys.version_info[0]}.{sys.version_info[1]}"
    scripts = tmp_path / "scripts"
    gate = _write_mutant_gate(
        GATE_SCRIPT.read_text(encoding="utf-8"), scripts
    )
    (scripts / "compileall.py").write_text(
        '"""Shadow compileall: always reports success."""\n'
        "\n"
        "\n"
        "def compile_dir(*args, **kwargs):\n"
        "    return True\n",
        encoding="utf-8",
    )
    root = tmp_path / "tree"
    _write_floor_tree(root, running)
    (root / "src" / "csk" / "bad.py").write_text(
        "def broken(:\n    pass\n", encoding="utf-8"
    )
    hijacked = _run_gate_script(gate, sys.executable, root, running, isolated=False)
    assert hijacked.returncode == 0, hijacked.stderr
    immune = _run_gate_script(gate, sys.executable, root, running, isolated=True)
    assert immune.returncode != 0
    assert "floor compile failed" in immune.stderr
