"""Portable guards for the dedicated native Windows SSH CI job, not a VM substitute."""
from __future__ import annotations

import copy
import importlib.util
from pathlib import Path
import xml.etree.ElementTree as ET

import pytest
import yaml

ROOT = Path(__file__).parents[1]
SPEC = importlib.util.spec_from_file_location("windows_ssh_kit", ROOT / ".github/scripts/windows_ssh_kit.py")
assert SPEC is not None and SPEC.loader is not None
KIT = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(KIT)


def _report(path: Path) -> ET.Element:
    suite = ET.Element("testsuite")
    for name in sorted(KIT.NATIVE_CASES):
        ET.SubElement(suite, "testcase", classname="tests.test_git_admission_ssh", name=name)
    # Scoped POSIX skips are permitted; only the native contract must not skip.
    ET.SubElement(ET.SubElement(suite, "testcase", classname="tests.test_git_admission_ssh", name="test_posix_stub"), "skipped")
    ET.ElementTree(suite).write(path)
    return suite


def test_native_junit_accepts_complete_matrix_with_unrelated_skips(tmp_path: Path) -> None:
    # A real complete native result is admitted even with honest POSIX skips.
    report = tmp_path / "results.xml"
    _report(report)
    KIT.verify_results(report)


@pytest.mark.parametrize("violation", ["missing", "skipped", "failure", "error", "duplicate", "wrong-module"])
def test_native_junit_rejects_unexecuted_or_failed_cases(tmp_path: Path, violation: str) -> None:
    # Each planted JUnit defect must be rejected with the native-case diagnostic;
    # the adjacent complete report is a positive control against reject-all.
    report = tmp_path / "results.xml"
    suite = _report(report)
    first = suite[0]
    if violation == "missing":
        suite.remove(first)
    elif violation == "duplicate":
        suite.append(copy.deepcopy(first))
    elif violation == "wrong-module":
        first.set("classname", "tests.unrelated")
    else:
        ET.SubElement(first, violation)
    ET.ElementTree(suite).write(report)
    with pytest.raises(ValueError, match="native Windows SSH cases must run exactly once without skips or failures"):
        KIT.verify_results(report)


def _assert_job(job: dict) -> None:
    assert job["runs-on"] == "windows-2025", "native SSH job requires Windows 2025"
    assert "if" not in job, "native SSH job must not be manual-only or conditional"
    # runner is unavailable in job-level env expressions; GitHub rejects the
    # workflow before scheduling any job if it is used there.
    assert all("runner." not in value for value in job["env"].values()), "runner context is unavailable in job env"
    steps = {step["id"]: step for step in job["steps"] if "id" in step}
    assert {"setup", "run", "cleanup"} <= steps.keys(), "native SSH kit lifecycle is required"
    for phase in ("setup", "run", "cleanup"):
        command = steps[phase]["run"]
        assert f"windows_ssh_kit.py {phase} " in command, "native SSH kit command required"
        assert '--root "$env:CSK_SSH_KIT"' in command, "native SSH phases must share the same kit"
    assert steps["cleanup"].get("if") == "always()", "native SSH cleanup must always run"
    assert '--results "$env:CSK_SSH_EVIDENCE/results.xml"' in steps["run"]["run"], "native SSH JUnit guard requires its report"


def _workflow() -> dict:
    return yaml.safe_load((ROOT / ".github/workflows/windows-ssh.yml").read_text())


def test_native_windows_job_runs_on_feature_push_and_pull_requests() -> None:
    # Only this job is added. Normal events run the actual kit/zero-skip gate
    # and unconditional cleanup; this shape check does not prove hosted execution.
    workflow = _workflow()
    assert set(workflow["jobs"]) == {"windows_ssh"}
    events = workflow.get("on", workflow.get(True))
    assert "main" in events["pull_request"]["branches"]
    assert "feat/windows-build-ssh-agent" in events["push"]["branches"]
    assert workflow["permissions"] == {"contents": "read"}
    _assert_job(workflow["jobs"]["windows_ssh"])


@pytest.mark.parametrize("violation,diagnostic", [
    ("platform", "requires Windows 2025"),
    ("conditional", "must not be manual-only"),
    ("missing-run", "lifecycle is required"),
    ("wrong-root", "must share the same kit"),
    ("no-cleanup", "cleanup must always run"),
    ("no-junit", "JUnit guard requires its report"),
    ("invalid-job-context", "runner context is unavailable in job env"),
])
def test_native_job_checker_rejects_skip_or_cleanup_bypasses(violation: str, diagnostic: str) -> None:
    # Bounded workflow plants exercise the same checker as the valid job:
    # selecting another platform, bypassing the run, or losing cleanup is red.
    job = _workflow()["jobs"]["windows_ssh"]
    steps = {step["id"]: step for step in job["steps"] if "id" in step}
    if violation == "platform":
        job["runs-on"] = "ubuntu-latest"
    elif violation == "conditional":
        job["if"] = "github.event_name == 'workflow_dispatch'"
    elif violation == "missing-run":
        job["steps"].remove(steps["run"])
    elif violation == "wrong-root":
        steps["run"]["run"] = steps["run"]["run"].replace("$env:CSK_SSH_KIT", "$env:OTHER")
    elif violation == "no-cleanup":
        steps["cleanup"].pop("if")
    elif violation == "invalid-job-context":
        job["env"]["CSK_SSH_KIT"] = "${{ runner.temp }}/csk-native-ssh-kit"
    else:
        steps["run"]["run"] = steps["run"]["run"].split(" --results")[0]
    with pytest.raises(AssertionError, match=diagnostic):
        _assert_job(job)
