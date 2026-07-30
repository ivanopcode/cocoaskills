from __future__ import annotations

import copy
import ctypes
import io
import json
import os
import shutil
import signal
import stat
import struct
import sys
import threading
import time
from dataclasses import replace
from pathlib import Path
from typing import Any, cast

import pytest

from csk import cli
from csk.builds import go_v1


def _synthetic_probes(platform: str) -> tuple[go_v1.ControlProbe, ...]:
    records = go_v1._NATIVE_CONTROL_PLATFORMS[platform]
    return tuple(
        go_v1.ControlProbe(
            name=name,
            availability=records[name].availability,
            mechanism=records[name].mechanism,
        )
        for name in go_v1.NATIVE_CONTROL_INVENTORY
    )


def _valid_evidence(platform: str) -> go_v1.CapabilityEvidence:
    probes = _synthetic_probes(platform)
    return go_v1.evidence_from_applied(
        platform,
        probes,
        [
            probe.name
            for probe in probes
            if probe.availability == go_v1.AVAILABILITY_AVAILABLE
        ],
    )


def test_protocol_constants_match_the_closed_rc5_policy():
    assert go_v1.EXECUTION_POLICY == "manager-worker-v1"
    assert go_v1.PROCESS_GRAPH == (
        "manager-parent",
        "identity-verified-manager-owned-worker",
        "fingerprinted-goroot-bin-go",
        "fingerprinted-goroot-pkg-tool-child",
    )
    assert len(go_v1.SESSION_STATES) == 13
    assert len(go_v1.MANDATORY_CONTROLS) == 18
    assert len(go_v1.NATIVE_CONTROL_INVENTORY) == 5
    assert len(go_v1.DEFERRED_HARDENED_GUARANTEES) == 6
    assert not set(go_v1.DEFERRED_HARDENED_GUARANTEES) & (
        set(go_v1.MANDATORY_CONTROLS)
        | set(go_v1.NATIVE_CONTROL_INVENTORY)
    )


def test_accepted_rc5_vectors_match_the_implementation():
    root_value = os.environ.get("CURATOR_CONFORMANCE_ROOT")
    if not root_value:
        pytest.skip("CURATOR_CONFORMANCE_ROOT is not set")
    vectors = Path(root_value) / "vectors"
    host = json.loads(
        (vectors / "go-host-execution-policy.json").read_text(encoding="utf-8")
    )
    drivers = json.loads(
        (vectors / "build-drivers.json").read_text(encoding="utf-8")
    )
    assert host["execution_policy"] == go_v1.EXECUTION_POLICY
    assert host["process_graph"] == list(go_v1.PROCESS_GRAPH)
    assert host["session_states"] == list(go_v1.SESSION_STATES)
    assert [item["name"] for item in host["mandatory_controls"]] == list(
        go_v1.MANDATORY_CONTROLS
    )
    inventory = host["native_control_inventory"]
    assert inventory["version"] == go_v1.NATIVE_CONTROL_INVENTORY_VERSION
    assert inventory["platforms"] == [
        go_v1.PLATFORM_MACOS,
        go_v1.PLATFORM_WINDOWS,
    ]
    assert [item["name"] for item in inventory["controls"]] == list(
        go_v1.NATIVE_CONTROL_INVENTORY
    )
    for platform, example in host["capability_evidence_record"]["examples"].items():
        assert _valid_evidence(platform).to_dict() == example
    assert [item["name"] for item in host["capability_evidence_cases"]] == list(
        _EVIDENCE_CASES
    )
    assert [item["name"] for item in host["identity_and_protocol_cases"]] == list(
        _IDENTITY_PROTOCOL_CASES
    )
    assert [item["name"] for item in host["package_influence_cases"]] == [
        item[0] for item in _PACKAGE_INFLUENCE_CASES
    ]
    source_aware = [item for item in drivers["argv"] if item["source_aware"]]
    assert [item["name"] for item in source_aware] == ["list", "build"]
    assert source_aware[0]["argv"][1:] == list(go_v1.LIST_ARGUMENTS)
    prefix_end = 1 + len(go_v1.BUILD_ARGUMENT_PREFIX)
    assert source_aware[1]["argv"][1:prefix_end] == list(
        go_v1.BUILD_ARGUMENT_PREFIX
    )
    assert source_aware[1]["argv"][-1] == "."


@pytest.mark.parametrize(
    ("platform", "expected"),
    [
        (
            go_v1.PLATFORM_MACOS,
            [
                ("descendant-domain-termination", "available", "applied"),
                ("active-process-count-limit", "unavailable", "unavailable"),
                ("aggregate-memory-limit", "unavailable", "unavailable"),
                ("per-file-size-limit", "available", "applied"),
                ("inherited-handle-restriction", "available", "applied"),
            ],
        ),
        (
            go_v1.PLATFORM_WINDOWS,
            [
                ("descendant-domain-termination", "available", "applied"),
                ("active-process-count-limit", "available", "applied"),
                ("aggregate-memory-limit", "available", "applied"),
                ("per-file-size-limit", "unavailable", "unavailable"),
                ("inherited-handle-restriction", "available", "applied"),
            ],
        ),
    ],
)
def test_capability_evidence_examples_are_exact(platform: str, expected: list[tuple[str, str, str]]):
    record = _valid_evidence(platform)
    assert list(record.to_dict()) == [
        "record_version",
        "execution_policy",
        "platform",
        "controls",
    ]
    assert [
        (entry.name, entry.availability, entry.status)
        for entry in record.controls
    ] == expected
    assert all(entry.probed_at == "pre-worker-launch" for entry in record.controls)
    assert all(
        list(entry.to_dict()) == ["name", "availability", "status", "probed_at"]
        for entry in record.controls
    )
    go_v1.validate_capability_evidence(
        record,
        platform,
        _synthetic_probes(platform),
    )


_EVIDENCE_CASES = (
    "available-native-control-is-applied",
    "unavailable-native-control-does-not-reject",
    "capability-evidence-is-not-cache-input",
    "unavailable-control-cannot-be-reported-as-applied",
    "available-control-cannot-be-reported-as-unavailable",
    "unknown-native-control-is-rejected",
    "missing-native-control-entry-is-rejected",
    "duplicate-native-control-entry-is-rejected",
    "unknown-evidence-record-version-is-rejected",
    "hardened-guarantee-claimed-under-portable-policy",
    "hardened-execution-policy-in-evidence-record",
)


@pytest.mark.parametrize("case", _EVIDENCE_CASES, ids=_EVIDENCE_CASES)
def test_capability_evidence_cases(case: str):
    platform = go_v1.PLATFORM_MACOS
    probes = _synthetic_probes(platform)
    record = _valid_evidence(platform)
    expected = ""

    if case == "unavailable-control-cannot-be-reported-as-applied":
        entries = list(record.controls)
        index = next(
            index
            for index, entry in enumerate(entries)
            if entry.availability == go_v1.AVAILABILITY_UNAVAILABLE
        )
        entries[index] = replace(entries[index], status=go_v1.STATUS_APPLIED)
        record = replace(record, controls=tuple(entries))
        expected = go_v1.CODE_CAPABILITY_EVIDENCE_INVALID
    elif case == "available-control-cannot-be-reported-as-unavailable":
        entries = list(record.controls)
        index = next(
            index
            for index, entry in enumerate(entries)
            if entry.availability == go_v1.AVAILABILITY_AVAILABLE
        )
        entries[index] = replace(entries[index], status=go_v1.STATUS_UNAVAILABLE)
        record = replace(record, controls=tuple(entries))
        expected = go_v1.CODE_CAPABILITY_EVIDENCE_INVALID
    elif case == "unknown-native-control-is-rejected":
        entries = list(record.controls)
        entries[0] = replace(entries[0], name="host-firewall-profile")
        record = replace(record, controls=tuple(entries))
        expected = go_v1.CODE_CAPABILITY_EVIDENCE_INVALID
    elif case == "missing-native-control-entry-is-rejected":
        record = replace(record, controls=record.controls[:-1])
        expected = go_v1.CODE_CAPABILITY_EVIDENCE_INVALID
    elif case == "duplicate-native-control-entry-is-rejected":
        record = replace(
            record,
            controls=(*record.controls, record.controls[-1]),
        )
        expected = go_v1.CODE_CAPABILITY_EVIDENCE_INVALID
    elif case == "unknown-evidence-record-version-is-rejected":
        record = replace(record, record_version="capability-evidence-v2")
        expected = go_v1.CODE_CAPABILITY_EVIDENCE_INVALID
    elif case == "hardened-guarantee-claimed-under-portable-policy":
        entries = list(record.controls)
        entries[0] = replace(entries[0], name="total-network-denial")
        record = replace(record, controls=tuple(entries))
        expected = go_v1.CODE_HARDENED_CLAIM_FORBIDDEN
    elif case == "hardened-execution-policy-in-evidence-record":
        record = replace(record, execution_policy="hardened-worker-v1")
        expected = go_v1.CODE_HARDENED_CLAIM_FORBIDDEN

    if not expected:
        go_v1.validate_capability_evidence(record, platform, probes)
        return
    with pytest.raises(go_v1.GoV1Error) as raised:
        go_v1.validate_capability_evidence(record, platform, probes)
    assert raised.value.code == expected
    assert raised.value.code != go_v1.CODE_CONTROL_UNAVAILABLE


@pytest.mark.parametrize(
    "guarantee",
    go_v1.DEFERRED_HARDENED_GUARANTEES,
    ids=go_v1.DEFERRED_HARDENED_GUARANTEES,
)
def test_each_deferred_hardened_guarantee_is_refused_without_rejecting_its_absence(
    guarantee: str,
):
    platform = go_v1.PLATFORM_MACOS
    probes = _synthetic_probes(platform)
    portable = _valid_evidence(platform)
    go_v1.validate_capability_evidence(portable, platform, probes)
    assert guarantee not in {entry.name for entry in portable.controls}
    entries = list(portable.controls)
    entries[0] = replace(entries[0], name=guarantee)
    with pytest.raises(go_v1.GoV1Error) as raised:
        go_v1.validate_capability_evidence(
            replace(portable, controls=tuple(entries)),
            platform,
            probes,
        )
    assert raised.value.code == go_v1.CODE_HARDENED_CLAIM_FORBIDDEN


@pytest.mark.parametrize(
    "platform",
    (go_v1.PLATFORM_MACOS, go_v1.PLATFORM_WINDOWS),
    ids=(go_v1.PLATFORM_MACOS, go_v1.PLATFORM_WINDOWS),
)
def test_probe_measures_every_inventory_control_once_per_operation(platform: str):
    records = go_v1._NATIVE_CONTROL_PLATFORMS[platform]
    calls: list[str] = []

    def probe(name: str, limits: go_v1.ResourceLimits) -> bool:
        assert limits == go_v1.ResourceLimits()
        calls.append(name)
        return records[name].availability == go_v1.AVAILABILITY_AVAILABLE

    selected, probes = go_v1.probe_native_controls(
        go_v1.ResourceLimits(),
        _platform=platform,
        _native_probe=probe,
    )
    assert selected == platform
    # Every one of the five inventory controls is measured on this host for
    # this operation, including the ones expected to be unavailable.
    assert calls == list(go_v1.NATIVE_CONTROL_INVENTORY)
    assert [entry.name for entry in probes] == list(
        go_v1.NATIVE_CONTROL_INVENTORY
    )
    assert [entry.availability for entry in probes] == [
        records[name].availability for name in go_v1.NATIVE_CONTROL_INVENTORY
    ]
    assert all(entry.probed_at == go_v1.PROBE_TIMING for entry in probes)


@pytest.mark.skipif(
    sys.platform != "darwin" and os.name != "nt",
    reason="the portable source-aware policy covers exactly macOS and Windows",
)
@pytest.mark.parametrize(
    "control",
    go_v1.NATIVE_CONTROL_INVENTORY,
    ids=go_v1.NATIVE_CONTROL_INVENTORY,
)
def test_native_probe_measurement_matches_the_frozen_inventory(control: str):
    platform = go_v1.inventory_platform()
    record = go_v1._NATIVE_CONTROL_PLATFORMS[platform][control]
    measured = go_v1._probe_native_control(control, go_v1.ResourceLimits())
    assert measured == (record.availability == go_v1.AVAILABILITY_AVAILABLE)


def test_probe_rejects_a_measurement_that_contradicts_the_inventory():
    calls: list[str] = []

    def always_available(name: str, _limits: go_v1.ResourceLimits) -> bool:
        calls.append(name)
        return True

    with pytest.raises(go_v1.GoV1Error) as raised:
        go_v1.probe_native_controls(
            go_v1.ResourceLimits(),
            _platform=go_v1.PLATFORM_MACOS,
            _native_probe=always_available,
        )
    assert raised.value.code == go_v1.CODE_CAPABILITY_EVIDENCE_INVALID
    assert calls == [
        "descendant-domain-termination",
        "active-process-count-limit",
    ]


def test_probe_failure_is_not_a_probed_availability_result():
    def broken(name: str, _limits: go_v1.ResourceLimits) -> bool:
        raise OSError(f"probe for {name} cannot run")

    with pytest.raises(go_v1.GoV1Error) as raised:
        go_v1.probe_native_controls(
            go_v1.ResourceLimits(),
            _platform=go_v1.PLATFORM_MACOS,
            _native_probe=broken,
        )
    assert raised.value.code == go_v1.CODE_CONTROL_UNAVAILABLE


def test_missing_available_native_control_fails_before_worker_launch():
    calls: list[str] = []

    def unavailable(name: str, _: go_v1.ResourceLimits) -> bool:
        calls.append(name)
        return False

    with pytest.raises(go_v1.GoV1Error) as raised:
        go_v1.probe_native_controls(
            go_v1.ResourceLimits(),
            _platform=go_v1.PLATFORM_MACOS,
            _native_probe=unavailable,
        )
    assert raised.value.code == go_v1.CODE_CONTROL_UNAVAILABLE
    assert calls == ["descendant-domain-termination"]


def test_unsupported_host_fails_closed_without_probing_or_worker(
    monkeypatch: pytest.MonkeyPatch,
):
    calls: list[str] = []
    monkeypatch.setattr(go_v1.sys, "platform", "linux")
    with pytest.raises(go_v1.GoV1Error) as raised:
        go_v1.probe_native_controls(
            go_v1.ResourceLimits(),
            _native_probe=lambda name, _limits: calls.append(name) or True,
        )
    assert raised.value.code == go_v1.CODE_CONTROL_UNAVAILABLE
    assert calls == []


def _make_manager(path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    interpreter = path.parent / "python"
    if not interpreter.exists():
        interpreter.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
        interpreter.chmod(0o755)
    package = (
        path.parent.parent
        / "lib"
        / "python3.11"
        / "site-packages"
        / "csk"
    )
    (package / "builds").mkdir(parents=True, exist_ok=True)
    for relative in ("__init__.py", "cli.py", "builds/go_v1.py"):
        target = package.joinpath(*relative.split("/"))
        if not target.exists():
            target.write_text(f"# synthetic {relative}\n", encoding="utf-8")
    stdlib_json = package.parent.parent / "json" / "__init__.py"
    stdlib_json.parent.mkdir(parents=True, exist_ok=True)
    stdlib_json.write_text("# synthetic json\n", encoding="utf-8")
    path.write_text(
        f"#!{interpreter}\nfrom csk.cli import main\nmain()\n",
        encoding="utf-8",
    )
    path.chmod(0o755)
    return path.resolve()


def _worker_fixture(tmp_path: Path) -> tuple[go_v1._WorkerPlan, str, str]:
    manager = _make_manager(tmp_path / "manager" / "bin" / "csk")
    identity = go_v1._resolve_manager_identity(manager)
    source_root = tmp_path / "snapshot"
    directory = source_root / "build" / "cmd"
    directory.mkdir(parents=True)
    goroot = tmp_path / "goroot"
    go_executable = goroot / "bin" / "go"
    go_executable.parent.mkdir(parents=True)
    go_executable.write_bytes(b"\xcf\xfa\xed\xfefake")
    go_executable.chmod(0o755)
    tool_directory = goroot / "pkg" / "tool" / "darwin_arm64"
    tool_directory.mkdir(parents=True)
    compiler = tool_directory / "compile"
    compiler.write_bytes(b"\xcf\xfa\xed\xfefake-compile")
    compiler.chmod(0o755)
    process_identity = go_v1._resolve_tool_process_identity(
        go_executable,
        tool_directory,
    )
    operation = tmp_path / "operation"
    private_names = {
        "GOPATH": "gopath",
        "GOMODCACHE": "gomodcache",
        "GOCACHE": "gocache",
        "GOTMPDIR": "gotmp",
        "HOME": "home",
        "XDG_CONFIG_HOME": "config",
        "PATH": "empty-path",
        "TMPDIR": "tmp",
    }
    environment: dict[str, str] = {}
    private_roots: list[Path] = []
    for name, child in private_names.items():
        path = operation / child
        path.mkdir(parents=True)
        environment[name] = str(path)
        private_roots.append(path)
    environment.update(
        {
            "GOENV": "off",
            "GOTOOLCHAIN": "local",
            "LC_ALL": "C",
            "LANG": "C",
            "GOROOT": str(goroot),
            "GOOS": "darwin",
            "GOARCH": "arm64",
            "GOARM64": "v8.0",
            "GO111MODULE": "on",
            "GOFLAGS": "",
            "GOPROXY": "off",
            "GOSUMDB": "off",
            "GOPRIVATE": "",
            "GONOPROXY": "none",
            "GONOSUMDB": "none",
            "GOVCS": "*:off",
            "GOWORK": "off",
            "CGO_ENABLED": "0",
            "GO_EXTLINK_ENABLED": "0",
            "GOEXPERIMENT": "",
        }
    )
    stage = operation / "stage"
    (stage / "bin").mkdir(parents=True)
    private_roots.append(stage)
    worker_cache = operation / "worker-cache"
    worker_cache.mkdir()
    private_roots.append(worker_cache)
    artifact = stage / "bin" / "fixture"
    build_argv = (*go_v1.BUILD_ARGUMENT_PREFIX, str(artifact), ".")
    secret = "a" * 64
    nonce = "b" * 64
    return (
        go_v1._WorkerPlan(
            executable=identity,
            process_identity=process_identity,
            go_executable=go_executable,
            goroot=goroot,
            tool_directory=tool_directory,
            worker_cache=worker_cache,
            directory=directory,
            environment=environment,
            list_argv=go_v1.LIST_ARGUMENTS,
            build_argv=build_argv,
            artifact_path=artifact,
            readonly_roots=(source_root, goroot),
            private_roots=tuple(private_roots),
            platform=go_v1.PLATFORM_MACOS,
            probes=_synthetic_probes(go_v1.PLATFORM_MACOS),
            limits=go_v1.ResourceLimits(),
        ),
        secret,
        nonce,
    )


def _frame(message: dict[str, object]) -> bytes:
    payload = json.dumps(message, sort_keys=True, separators=(",", ":")).encode()
    return struct.pack(">I", len(payload)) + payload


def _runtime_proof(identity: go_v1._ManagerIdentity) -> dict[str, object]:
    """A site-disabled worker startup proof for the synthetic manager."""

    package = identity.package_tree.path
    return {
        "executable": str(identity.interpreter.invocation_path),
        "argv0": str(identity.launcher.path),
        "launch": _launch_context().public_dict(),
        "flags": {name: 1 for name in go_v1._WORKER_RUNTIME_FLAGS},
        "native": {
            "process_image": str(
                identity.interpreter.runtime.process_image.path
            ),
            "runtime_image": str(
                identity.interpreter.runtime.runtime_image.path
            ),
        },
        "path": [
            str(identity.startup.site_root),
            str(identity.startup.archive_slots[0]),
            str(identity.startup.stdlib_root),
        ],
        "modules": [
            {"name": "csk", "path": str(package / "__init__.py")},
            {"name": "csk.cli", "path": str(package / "cli.py")},
            {
                "name": "csk.builds.go_v1",
                "path": str(package / "builds" / "go_v1.py"),
            },
            {
                "name": "json",
                "path": str(identity.startup.stdlib_root / "json" / "__init__.py"),
            },
        ],
    }


def _launch_context() -> go_v1._WorkerLaunchContext:
    return go_v1._WorkerLaunchContext(
        parent_pid=os.getpid(),
        secret=b"l" * go_v1._WORKER_LAUNCH_SECRET_BYTES,
    )


class RecordingExecutor:
    def __init__(self, results: list[go_v1.ProcessResult] | None = None):
        self.results = results or [
            go_v1.ProcessResult(stdout=b'{"ImportPath":"x"}\n'),
            go_v1.ProcessResult(),
        ]
        self.calls: list[go_v1.ProcessRequest] = []

    def run(self, request: go_v1.ProcessRequest) -> go_v1.ProcessResult:
        self.calls.append(request)
        return self.results[len(self.calls) - 1]


def _run_scripted_worker(
    monkeypatch: pytest.MonkeyPatch,
    plan: go_v1._WorkerPlan,
    secret: str,
    nonce: str,
    messages: bytes,
    executor: go_v1.ProcessExecutor,
    runtime: dict[str, object] | None = None,
    manager_auth_secret: bytes | None = None,
) -> list[dict[str, Any]]:
    monkeypatch.setattr(go_v1, "inventory_platform", lambda: go_v1.PLATFORM_MACOS)
    output = io.BytesIO()
    launch_context = _launch_context()
    request = go_v1._plan_request_mapping(plan, secret)
    status = go_v1.run_worker(
        io.BytesIO(
            _frame(
                {
                    "kind": "request",
                    "nonce": nonce,
                    "manager_auth": go_v1._launch_authenticator(
                        (
                            launch_context.secret
                            if manager_auth_secret is None
                            else manager_auth_secret
                        ),
                        go_v1._WORKER_LAUNCH_REQUEST_DOMAIN,
                        nonce,
                        request,
                    ),
                    "request": request,
                }
            )
            + messages
        ),
        output,
        executor=executor,
        worker_executable=plan.executable.path,
        identity_resolver=lambda _path: plan.executable,
        control_observer=lambda _platform, probes, _limits, _streams: tuple(
            probe.name
            for probe in probes
            if probe.availability == go_v1.AVAILABILITY_AVAILABLE
        ),
        runtime_proof=(
            _runtime_proof(plan.executable) if runtime is None else runtime
        ),
        _launch_context=launch_context,
    )
    frames: list[dict[str, Any]] = []
    stream = io.BytesIO(output.getvalue())
    while stream.tell() != len(output.getvalue()):
        frames.append(dict(go_v1._read_message(stream)))
    frames.append({"worker_status": status})
    return frames


def test_worker_issues_exactly_one_fixed_list_then_one_permitted_build(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    plan, secret, nonce = _worker_fixture(tmp_path)
    permit = go_v1._build_permit(secret, nonce, plan.build_argv)
    executor = RecordingExecutor()
    frames = _run_scripted_worker(
        monkeypatch,
        plan,
        secret,
        nonce,
        _frame({"kind": "list", "nonce": nonce})
        + _frame({"kind": "permit", "nonce": nonce, "permit": permit})
        + _frame({"kind": "shutdown", "nonce": nonce}),
        executor,
    )
    assert frames[-1] == {"worker_status": 0}
    assert [frame["kind"] for frame in frames[:-1]] == [
        "ready",
        "list-result",
        "build-result",
    ]
    assert [call.arguments for call in executor.calls] == [
        go_v1.LIST_ARGUMENTS,
        plan.build_argv,
    ]
    assert all(call.executable == plan.go_executable for call in executor.calls)
    assert all(call.identity == plan.process_identity for call in executor.calls)
    assert all(call.environment == plan.environment for call in executor.calls)


def test_worker_reverifies_manager_package_after_go_process(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    plan, secret, nonce = _worker_fixture(tmp_path)
    package_file = plan.executable.package_tree.path / "builds" / "go_v1.py"

    class MutatingExecutor(RecordingExecutor):
        def run(self, request: go_v1.ProcessRequest) -> go_v1.ProcessResult:
            result = super().run(request)
            package_file.write_bytes(
                package_file.read_bytes() + b"# replaced in session\n"
            )
            return result

    executor = MutatingExecutor()
    frames = _run_scripted_worker(
        monkeypatch,
        plan,
        secret,
        nonce,
        _frame({"kind": "list", "nonce": nonce}),
        executor,
    )
    assert [frame["kind"] for frame in frames[:-1]] == ["ready", "failure"]
    assert frames[-2]["failure"]["code"] == (
        go_v1.CODE_WORKER_IDENTITY_INVALID
    )
    assert frames[-1] == {"worker_status": 3}
    assert len(executor.calls) == 1


def test_manager_identity_binds_launcher_interpreter_and_package_tree(
    tmp_path: Path,
):
    plan, _, _ = _worker_fixture(tmp_path)
    identity = plan.executable
    assert list(identity.to_dict()) == [
        "launcher",
        "interpreter",
        "package_tree",
        "startup",
    ]
    assert identity.interpreter.executable.sha256.startswith("sha256:")
    assert identity.package_tree.sha256.startswith("sha256:")
    assert identity.startup.sha256.startswith("sha256:")
    package_file = identity.package_tree.path / "builds" / "go_v1.py"
    package_file.write_bytes(package_file.read_bytes() + b"# replaced\n")
    with pytest.raises(go_v1.GoV1Error) as raised:
        identity.verify()
    assert raised.value.code == go_v1.CODE_WORKER_IDENTITY_INVALID


def test_manager_identity_rejects_interpreter_replacement(tmp_path: Path):
    plan, _, _ = _worker_fixture(tmp_path)
    identity = plan.executable
    interpreter = identity.interpreter.executable.path
    interpreter.write_bytes(b"#!/bin/sh\nexit 1\n")
    interpreter.chmod(0o755)
    with pytest.raises(go_v1.GoV1Error) as raised:
        identity.verify()
    assert raised.value.code == go_v1.CODE_WORKER_IDENTITY_INVALID


def test_manager_identity_binds_native_python_runtime_image(tmp_path: Path):
    manager = _make_manager(tmp_path / "manager" / "bin" / "csk")
    runtime_image = manager.parent.parent / "lib" / "libpython3.11.dylib"
    runtime_image.write_bytes(b"synthetic Python runtime image")
    runtime_image.chmod(0o755)

    identity = go_v1._resolve_manager_identity(manager)

    assert identity.interpreter.runtime.process_image.path == (
        identity.interpreter.executable.path
    )
    assert identity.interpreter.runtime.runtime_image.path == runtime_image
    assert runtime_image in identity.watch_paths()

    runtime_image.write_bytes(b"replaced Python runtime image")
    with pytest.raises(go_v1.GoV1Error) as raised:
        identity.verify()
    assert raised.value.code == go_v1.CODE_WORKER_IDENTITY_INVALID


def test_windows_venv_runtime_layout_resolves_bound_base_installation(
    tmp_path: Path,
):
    base_home = tmp_path / "base-python"
    base_home.mkdir()
    base_interpreter = base_home / "python.exe"
    base_interpreter.write_bytes(b"base interpreter")
    base_interpreter.chmod(0o755)
    runtime_image = base_home / "python314.dll"
    runtime_image.write_bytes(b"base Python runtime")
    runtime_image.chmod(0o755)
    (base_home / "Lib").mkdir()
    (base_home / "DLLs").mkdir()

    venv = tmp_path / "manager-venv"
    scripts = venv / "Scripts"
    scripts.mkdir(parents=True)
    venv_interpreter = scripts / "python.exe"
    venv_interpreter.write_bytes(b"venv launcher")
    venv_interpreter.chmod(0o755)
    configuration = venv / "pyvenv.cfg"
    configuration.write_text(
        f"home = {base_home}\n"
        "include-system-site-packages = false\n"
        "version = 3.14.4\n",
        encoding="utf-8",
    )

    executable = go_v1._resolve_executable_identity(venv_interpreter)
    runtime = go_v1._resolve_windows_interpreter_runtime(executable)

    assert runtime.python_home == base_home
    assert runtime.configuration is not None
    assert runtime.configuration.path == configuration
    assert runtime.base_executable.path == base_interpreter
    assert runtime.process_image == runtime.base_executable
    assert runtime.runtime_image.path == runtime_image
    assert go_v1._manager_windows_stdlib_root(runtime) == base_home / "Lib"
    interpreter = go_v1._InterpreterIdentity(
        invocation_path=venv_interpreter,
        links=(),
        executable=executable,
        runtime=runtime,
    )
    site_root = venv / "Lib" / "site-packages"
    site_root.mkdir(parents=True)
    stdlib_root = go_v1._manager_stdlib_root(interpreter, site_root)
    assert stdlib_root == base_home / "Lib"
    assert go_v1._manager_runtime_roots(
        stdlib_root,
        interpreter,
    ) == (base_home,)
    assert go_v1._runtime_archive_slots(
        stdlib_root,
        interpreter,
    ) == (base_home / "python314.zip",)
    runtime_archive = base_home / "python314.zip"
    runtime_archive.write_bytes(b"PK\x05\x06" + b"\x00" * 18)
    startup = go_v1._resolve_startup_identity(
        tmp_path / "manager-venv" / "Scripts" / "csk.exe",
        interpreter,
        site_root,
    )
    assert [tree.path for tree in startup.runtime_trees] == [base_home]
    assert [archive.path for archive in startup.archives] == [runtime_archive]
    bound_manager = replace(
        _worker_fixture(tmp_path / "permitted-worker")[0].executable,
        interpreter=interpreter,
        startup=startup,
    )
    assert go_v1._manager_identity_from_mapping(
        bound_manager.to_dict()
    ) == bound_manager
    assert go_v1._permitted_worker_import_root(base_home, bound_manager)
    injected = base_home / "root_level_attack.py"
    injected.write_text("raise RuntimeError('unbound')\n", encoding="utf-8")
    with pytest.raises(go_v1.GoV1Error) as raised:
        startup.runtime_trees[0].verify()
    assert raised.value.code == go_v1.CODE_WORKER_IDENTITY_INVALID
    injected.unlink()
    manager = replace(
        _worker_fixture(tmp_path / "worker")[0].executable,
        interpreter=interpreter,
    )
    assert go_v1.worker_argv(manager) == (
        str(base_interpreter),
        *go_v1.WORKER_LAUNCH_FLAGS,
        str(manager.launcher.path),
        go_v1.WORKER_MODE,
    )


def test_interpreter_runtime_mapping_rejects_relative_identity_paths(
    tmp_path: Path,
):
    plan, _, _ = _worker_fixture(tmp_path)
    mapping = plan.executable.interpreter.to_dict()
    runtime = cast(dict[str, object], mapping["runtime"])
    runtime["python_home"] = "relative-python-home"

    with pytest.raises(go_v1.GoV1Error) as raised:
        go_v1._interpreter_identity_from_mapping(mapping)
    assert raised.value.code == go_v1.CODE_WORKER_PROTOCOL_INVALID


@pytest.mark.parametrize(
    "configuration",
    (
        "malformed-line",
        "version = 3.14\n",
        "home = first\nhome = second\n",
        "home = relative/path\n",
    ),
)
def test_pyvenv_home_rejects_malformed_or_untrusted_roots(
    configuration: str,
):
    with pytest.raises(go_v1.GoV1Error) as raised:
        go_v1._pyvenv_home(configuration)
    assert raised.value.code == go_v1.CODE_WORKER_IDENTITY_INVALID


@pytest.mark.parametrize(
    "payload",
    (
        b"\xff\xfe\xfa",
        b"home = C:\\Python\x00\n",
    ),
)
def test_pyvenv_configuration_rejects_non_utf8_and_nul(
    payload: bytes,
    tmp_path: Path,
):
    configuration = tmp_path / "pyvenv.cfg"
    configuration.write_bytes(payload)
    with pytest.raises(go_v1.GoV1Error) as raised:
        go_v1._resolve_configuration_identity(configuration)
    assert raised.value.code == go_v1.CODE_WORKER_IDENTITY_INVALID


def test_runtime_directory_must_exist_and_be_canonical(tmp_path: Path):
    with pytest.raises(go_v1.GoV1Error) as missing:
        go_v1._canonical_runtime_directory(
            tmp_path / "missing",
            "runtime",
        )
    assert missing.value.code == go_v1.CODE_WORKER_IDENTITY_INVALID

    regular_file = tmp_path / "not-a-directory"
    regular_file.write_bytes(b"runtime")
    with pytest.raises(go_v1.GoV1Error) as invalid:
        go_v1._canonical_runtime_directory(regular_file, "runtime")
    assert invalid.value.code == go_v1.CODE_WORKER_IDENTITY_INVALID


def test_base_windows_runtime_layout_and_dll_cardinality_fail_closed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    python_home = tmp_path / "python-home"
    python_home.mkdir()
    executable_path = python_home / "python.exe"
    executable_path.write_bytes(b"base Python")
    executable_path.chmod(0o755)
    executable = go_v1._resolve_executable_identity(executable_path)

    with pytest.raises(go_v1.GoV1Error) as missing:
        go_v1._resolve_windows_interpreter_runtime(executable)
    assert missing.value.code == go_v1.CODE_WORKER_IDENTITY_INVALID

    for name in ("python313.dll", "python314.dll"):
        runtime = python_home / name
        runtime.write_bytes(b"runtime")
        runtime.chmod(0o755)
    with pytest.raises(go_v1.GoV1Error) as ambiguous:
        go_v1._resolve_windows_interpreter_runtime(executable)
    assert ambiguous.value.code == go_v1.CODE_WORKER_IDENTITY_INVALID

    monkeypatch.setattr(
        go_v1.os,
        "scandir",
        lambda _path: (_ for _ in ()).throw(OSError("denied")),
    )
    with pytest.raises(go_v1.GoV1Error) as unavailable:
        go_v1._resolve_windows_interpreter_runtime(executable)
    assert unavailable.value.code == go_v1.CODE_WORKER_IDENTITY_INVALID


def test_macos_runtime_layouts_bind_framework_and_fallback_images(
    tmp_path: Path,
):
    framework_home = tmp_path / "framework"
    interpreter_path = framework_home / "bin" / "python3.14"
    interpreter_path.parent.mkdir(parents=True)
    interpreter_path.write_bytes(b"launcher")
    interpreter_path.chmod(0o755)
    runtime_image = framework_home / "Python"
    runtime_image.write_bytes(b"runtime")
    runtime_image.chmod(0o755)
    process_image = (
        framework_home
        / "Resources"
        / "Python.app"
        / "Contents"
        / "MacOS"
        / "Python"
    )
    process_image.parent.mkdir(parents=True)
    process_image.write_bytes(b"process")
    process_image.chmod(0o755)

    framework = go_v1._resolve_macos_interpreter_runtime(
        go_v1._resolve_executable_identity(interpreter_path)
    )
    assert framework.runtime_image.path == runtime_image
    assert framework.process_image.path == process_image

    fallback_home = tmp_path / "fallback"
    fallback_path = fallback_home / "bin" / "python3.14"
    fallback_path.parent.mkdir(parents=True)
    fallback_path.write_bytes(b"fallback")
    fallback_path.chmod(0o755)
    fallback = go_v1._resolve_macos_interpreter_runtime(
        go_v1._resolve_executable_identity(fallback_path)
    )
    assert fallback.runtime_image.path == fallback_path
    assert fallback.process_image.path == fallback_path


def test_macos_runtime_layout_rejects_ambiguous_dylibs(tmp_path: Path):
    python_home = tmp_path / "python-home"
    interpreter_path = python_home / "bin" / "python3.14"
    interpreter_path.parent.mkdir(parents=True)
    interpreter_path.write_bytes(b"launcher")
    interpreter_path.chmod(0o755)
    library = python_home / "lib"
    library.mkdir()
    for name in ("libpython3.13.dylib", "libpython3.14.dylib"):
        candidate = library / name
        candidate.write_bytes(b"runtime")
        candidate.chmod(0o755)

    with pytest.raises(go_v1.GoV1Error) as raised:
        go_v1._resolve_macos_interpreter_runtime(
            go_v1._resolve_executable_identity(interpreter_path)
        )
    assert raised.value.code == go_v1.CODE_WORKER_IDENTITY_INVALID


def test_runtime_archive_and_slot_bounds_fail_closed(tmp_path: Path):
    with pytest.raises(go_v1.GoV1Error) as excessive:
        go_v1._resolve_runtime_archives(
            tuple(
                tmp_path / f"python{index}.zip"
                for index in range(go_v1._MAX_RUNTIME_ARCHIVES + 1)
            )
        )
    assert excessive.value.code == go_v1.CODE_WORKER_IDENTITY_INVALID

    plan, _, _ = _worker_fixture(tmp_path / "worker")
    invalid_runtime = replace(
        plan.executable.interpreter.runtime,
        runtime_image=replace(
            plan.executable.interpreter.runtime.runtime_image,
            path=Path("/bound/python.dll"),
        ),
    )
    invalid_interpreter = replace(
        plan.executable.interpreter,
        runtime=invalid_runtime,
    )
    with pytest.raises(go_v1.GoV1Error) as invalid_slot:
        go_v1._runtime_archive_slots(
            Path("/bound/Lib"),
            invalid_interpreter,
        )
    assert invalid_slot.value.code == go_v1.CODE_WORKER_IDENTITY_INVALID


def test_manager_identity_binds_every_mutable_startup_component(tmp_path: Path):
    plan, _, _ = _worker_fixture(tmp_path)
    identity = plan.executable
    site = identity.startup.site_root
    assert identity.startup.stdlib_root == site.parent
    assert identity.startup.hooks == ()

    inserted = site / "attacker.pth"
    inserted.write_text("import os\n", encoding="utf-8")
    with pytest.raises(go_v1.GoV1Error) as raised:
        identity.verify()
    assert raised.value.code == go_v1.CODE_WORKER_IDENTITY_INVALID

    bound = go_v1._resolve_manager_identity(identity.path)
    assert [Path(hook.path).name for hook in bound.startup.hooks] == [
        "attacker.pth"
    ]
    bound.verify()
    inserted.write_text("import os  # mutated\n", encoding="utf-8")
    with pytest.raises(go_v1.GoV1Error) as raised:
        bound.verify()
    assert raised.value.code == go_v1.CODE_WORKER_IDENTITY_INVALID
    inserted.unlink()

    for name in ("sitecustomize.py", "usercustomize.py"):
        component = site / name
        component.write_text("# startup hook\n", encoding="utf-8")
        with pytest.raises(go_v1.GoV1Error) as raised:
            identity.verify()
        assert raised.value.code == go_v1.CODE_WORKER_IDENTITY_INVALID
        component.unlink()

    configuration = identity.path.parent.parent / "pyvenv.cfg"
    configuration.write_text("home = /attacker/bin\n", encoding="utf-8")
    with pytest.raises(go_v1.GoV1Error) as raised:
        identity.verify()
    assert raised.value.code == go_v1.CODE_WORKER_IDENTITY_INVALID
    configuration.unlink()
    identity.verify()


def test_manager_identity_binds_importable_stdlib_and_python_archives(
    tmp_path: Path,
):
    plan, _, _ = _worker_fixture(tmp_path)
    identity = plan.executable
    stdlib_json = identity.startup.stdlib_root / "json" / "__init__.py"

    assert identity.startup.runtime_trees[0].path == identity.startup.stdlib_root
    assert "json/__init__.py" in {
        entry.path
        for entry in identity.startup.runtime_trees[0].entries
        if entry.kind == "file"
    }
    assert identity.startup.archives == ()

    original = stdlib_json.read_bytes()
    stdlib_json.write_bytes(original + b"# replaced before proof\n")
    with pytest.raises(go_v1.GoV1Error) as raised:
        identity.verify()
    assert raised.value.code == go_v1.CODE_WORKER_IDENTITY_INVALID
    stdlib_json.write_bytes(original)
    identity.verify()

    archive = identity.startup.stdlib_root.parent / "python311.zip"
    archive.write_bytes(b"PK\x05\x06" + b"\x00" * 18)
    with pytest.raises(go_v1.GoV1Error) as raised:
        identity.verify()
    assert raised.value.code == go_v1.CODE_WORKER_IDENTITY_INVALID
    bound_archive = go_v1._resolve_manager_identity(identity.path)
    assert [item.path for item in bound_archive.startup.archives] == [archive]
    archive.write_bytes(archive.read_bytes() + b"mutated")
    with pytest.raises(go_v1.GoV1Error) as raised:
        bound_archive.verify()
    assert raised.value.code == go_v1.CODE_WORKER_IDENTITY_INVALID


@pytest.mark.skipif(
    sys.platform != "darwin",
    reason="the replacement/restore event guard is the macOS mechanism",
)
def test_macos_identity_guard_retains_importable_stdlib_until_teardown(
    tmp_path: Path,
):
    plan, _, _ = _worker_fixture(tmp_path)
    identity = plan.executable
    stdlib_json = identity.startup.stdlib_root / "json" / "__init__.py"
    original = stdlib_json.read_bytes()
    guard = go_v1._IdentityMutationGuard(
        go_v1.PLATFORM_MACOS,
        identity.watch_paths(),
    )

    stdlib_json.write_bytes(original + b"# transient replacement\n")
    stdlib_json.write_bytes(original)

    with pytest.raises(go_v1.GoV1Error) as raised:
        guard.verify()
    assert raised.value.code == go_v1.CODE_WORKER_IDENTITY_INVALID
    guard.close()


@pytest.mark.skipif(
    sys.platform != "darwin",
    reason="RLIMIT_NOFILE is the macOS identity-retention mechanism",
)
def test_macos_identity_descriptor_capacity_is_bounded_and_restored(
    monkeypatch: pytest.MonkeyPatch,
):
    import resource

    previous = (256, 131_072)
    required = go_v1._MACOS_FIXED_DESCRIPTOR_CAPACITY
    limits = iter((previous, (required, previous[1])))
    applied: list[tuple[int, tuple[int, int]]] = []
    monkeypatch.setattr(resource, "getrlimit", lambda _kind: next(limits))
    monkeypatch.setattr(
        resource,
        "setrlimit",
        lambda kind, value: applied.append((kind, value)),
    )

    retained = go_v1._ensure_macos_identity_descriptor_capacity(
        1000 - go_v1._MACOS_IDENTITY_FD_HEADROOM
    )
    assert retained == previous
    go_v1._restore_macos_identity_descriptor_capacity(retained)
    assert applied == [
        (resource.RLIMIT_NOFILE, (required, previous[1])),
        (resource.RLIMIT_NOFILE, previous),
    ]


@pytest.mark.skipif(
    sys.platform != "darwin",
    reason="RLIMIT_NOFILE is the macOS identity-retention mechanism",
)
def test_macos_identity_guard_raises_a_low_real_limit_and_restores_it(
    tmp_path: Path,
):
    import resource

    retained = tmp_path / "identity"
    retained.write_bytes(b"bound")
    original = resource.getrlimit(resource.RLIMIT_NOFILE)
    low = (min(original[0], 256), original[1])
    resource.setrlimit(resource.RLIMIT_NOFILE, low)
    guard: go_v1._IdentityMutationGuard | None = None
    try:
        guard = go_v1._IdentityMutationGuard(
            go_v1.PLATFORM_MACOS,
            (retained,),
        )
        assert resource.getrlimit(resource.RLIMIT_NOFILE)[0] >= (
            go_v1._MACOS_FIXED_DESCRIPTOR_CAPACITY
        )
        guard.close()
        guard = None
        assert resource.getrlimit(resource.RLIMIT_NOFILE) == low
    finally:
        if guard is not None:
            guard.close()
        resource.setrlimit(resource.RLIMIT_NOFILE, original)


@pytest.mark.skipif(
    sys.platform != "darwin",
    reason="RLIMIT_NOFILE is the macOS identity-retention mechanism",
)
def test_macos_identity_descriptor_capacity_failures_are_closed(
    monkeypatch: pytest.MonkeyPatch,
):
    import resource

    for count in (0, go_v1._MAX_RETAINED_IDENTITY_PATHS + 1):
        with pytest.raises(OSError):
            go_v1._ensure_macos_identity_descriptor_capacity(count)

    monkeypatch.setattr(
        resource,
        "getrlimit",
        lambda _kind: (256, 4096),
    )
    with pytest.raises(OSError, match="hard limit"):
        go_v1._ensure_macos_identity_descriptor_capacity(1)

    monkeypatch.setattr(
        resource,
        "getrlimit",
        lambda _kind: (
            go_v1._MACOS_FIXED_DESCRIPTOR_CAPACITY,
            131_072,
        ),
    )
    assert go_v1._ensure_macos_identity_descriptor_capacity(1) is None


def test_identity_guard_rejects_an_empty_or_unbounded_identity_set(
    monkeypatch: pytest.MonkeyPatch,
):
    with pytest.raises(go_v1.GoV1Error) as empty:
        go_v1._IdentityMutationGuard(go_v1.PLATFORM_MACOS, ())
    assert empty.value.code == go_v1.CODE_CONTROL_UNAVAILABLE

    monkeypatch.setattr(go_v1, "_MAX_RETAINED_IDENTITY_PATHS", 1)
    with pytest.raises(go_v1.GoV1Error) as excessive:
        go_v1._IdentityMutationGuard(
            go_v1.PLATFORM_MACOS,
            (Path("/bound/one"), Path("/bound/two")),
        )
    assert excessive.value.code == go_v1.CODE_CONTROL_UNAVAILABLE


class _FakeCFunction:
    def __init__(self, implementation: Any):
        self.implementation = implementation
        self.argtypes: list[object] = []
        self.restype: object | None = None

    def __call__(self, *arguments: object) -> object:
        return self.implementation(*arguments)


def test_loaded_image_path_must_be_absolute_and_present(tmp_path: Path):
    image = tmp_path / "runtime"
    image.write_bytes(b"runtime")
    assert go_v1._canonical_loaded_image(image, "runtime") == image

    with pytest.raises(go_v1.GoV1Error) as relative:
        go_v1._canonical_loaded_image(Path("relative"), "runtime")
    assert relative.value.code == go_v1.CODE_WORKER_IDENTITY_INVALID

    with pytest.raises(go_v1.GoV1Error) as missing:
        go_v1._canonical_loaded_image(tmp_path / "missing", "runtime")
    assert missing.value.code == go_v1.CODE_WORKER_IDENTITY_INVALID


def test_macos_loaded_process_image_success_and_failures(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    image = tmp_path / "process"
    image.write_bytes(b"process")
    count = _FakeCFunction(lambda: 1)
    name = _FakeCFunction(lambda _index: os.fsencode(image))
    process = type(
        "FakeProcess",
        (),
        {
            "_dyld_image_count": count,
            "_dyld_get_image_name": name,
        },
    )()
    monkeypatch.setattr(go_v1.ctypes, "CDLL", lambda _value: process)
    assert go_v1._macos_loaded_process_image() == image

    count.implementation = lambda: 0
    with pytest.raises(go_v1.GoV1Error) as no_images:
        go_v1._macos_loaded_process_image()
    assert no_images.value.code == go_v1.CODE_WORKER_IDENTITY_INVALID

    count.implementation = lambda: 1
    name.implementation = lambda _index: None
    with pytest.raises(go_v1.GoV1Error) as no_name:
        go_v1._macos_loaded_process_image()
    assert no_name.value.code == go_v1.CODE_WORKER_IDENTITY_INVALID


def test_macos_loaded_runtime_image_success_and_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    image = tmp_path / "Python"
    image.write_bytes(b"runtime")

    def resolve(
        _address: object,
        pointer: object,
    ) -> int:
        cast(Any, pointer)._obj.filename = os.fsencode(image)
        return 1

    lookup = _FakeCFunction(resolve)
    process = type("FakeProcess", (), {"dladdr": lookup})()
    monkeypatch.setattr(go_v1.ctypes, "CDLL", lambda _value: process)
    assert go_v1._macos_loaded_python_runtime_image() == image

    lookup.implementation = lambda _address, _pointer: 0
    with pytest.raises(go_v1.GoV1Error) as unavailable:
        go_v1._macos_loaded_python_runtime_image()
    assert unavailable.value.code == go_v1.CODE_WORKER_IDENTITY_INVALID


def test_windows_loaded_image_handles_growth_and_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    image = tmp_path / "python314.dll"
    image.write_bytes(b"runtime")
    calls = 0

    def resolve(
        _handle: object,
        buffer: Any,
        capacity: int,
    ) -> int:
        nonlocal calls
        calls += 1
        if calls == 1:
            return capacity
        buffer.value = str(image)
        return len(buffer.value)

    get_name = _FakeCFunction(resolve)
    kernel32 = type("FakeKernel32", (), {"GetModuleFileNameW": get_name})()
    monkeypatch.setattr(go_v1, "_windows_kernel32", lambda: kernel32)
    assert go_v1._windows_loaded_image(7, "runtime") == image
    assert calls == 2

    get_name.implementation = lambda _handle, _buffer, _capacity: 0
    with pytest.raises(go_v1.GoV1Error) as unavailable:
        go_v1._windows_loaded_image(7, "runtime")
    assert unavailable.value.code == go_v1.CODE_WORKER_IDENTITY_INVALID

    get_name.implementation = (
        lambda _handle, _buffer, capacity: capacity
    )
    with pytest.raises(go_v1.GoV1Error) as unbounded:
        go_v1._windows_loaded_image(7, "runtime")
    assert unbounded.value.code == go_v1.CODE_WORKER_IDENTITY_INVALID


def test_worker_native_runtime_proof_selects_only_the_host_branch(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    process_image = tmp_path / "process"
    runtime_image = tmp_path / "runtime"
    monkeypatch.setattr(
        go_v1,
        "_macos_loaded_process_image",
        lambda: process_image,
    )
    monkeypatch.setattr(
        go_v1,
        "_macos_loaded_python_runtime_image",
        lambda: runtime_image,
    )
    monkeypatch.setattr(
        go_v1,
        "inventory_platform",
        lambda: go_v1.PLATFORM_MACOS,
    )
    assert go_v1._worker_native_runtime_proof() == {
        "process_image": str(process_image),
        "runtime_image": str(runtime_image),
    }

    calls: list[tuple[int, str]] = []

    def loaded_image(handle: int, label: str) -> Path:
        calls.append((handle, label))
        return process_image if not handle else runtime_image

    monkeypatch.setattr(go_v1, "_windows_loaded_image", loaded_image)
    monkeypatch.setattr(
        go_v1,
        "inventory_platform",
        lambda: go_v1.PLATFORM_WINDOWS,
    )
    assert go_v1._worker_native_runtime_proof() == {
        "process_image": str(process_image),
        "runtime_image": str(runtime_image),
    }
    assert calls[0] == (0, "worker process image")
    assert calls[1][1] == "worker Python runtime image"


def test_manager_identity_rejects_a_shell_wrapper_launcher(tmp_path: Path):
    plan, _, _ = _worker_fixture(tmp_path)
    launcher = plan.executable.path
    launcher.write_text(
        "#!/bin/sh\n'''exec' '/attacker/python' \"$0\" \"$@\"\n' '''\n",
        encoding="utf-8",
    )
    launcher.chmod(0o755)
    with pytest.raises(go_v1.GoV1Error) as raised:
        go_v1._resolve_manager_identity(launcher)
    assert raised.value.code == go_v1.CODE_WORKER_IDENTITY_INVALID


def test_hidden_worker_launch_vector_is_fixed_and_site_disabled(tmp_path: Path):
    plan, _, _ = _worker_fixture(tmp_path)
    identity = plan.executable
    assert go_v1.WORKER_LAUNCH_FLAGS == ("-S", "-s", "-B", "-P")
    assert go_v1.worker_argv(identity) == (
        str(identity.interpreter.invocation_path),
        "-S",
        "-s",
        "-B",
        "-P",
        str(identity.launcher.path),
        go_v1.WORKER_MODE,
    )


def test_windows_distlib_argv0_restores_only_fixed_executable_suffix(
    tmp_path: Path,
):
    stripped = tmp_path / "Scripts" / "csk"
    assert go_v1._manager_executable_from_argv0(
        str(stripped),
        _windows=True,
    ) == stripped.with_name("csk.exe")
    assert go_v1._manager_executable_from_argv0(
        str(stripped.with_name("csk.exe")),
        _windows=True,
    ) == stripped.with_name("csk.exe")
    assert go_v1._manager_executable_from_argv0(
        str(stripped),
        _windows=False,
    ) == stripped


_RUNTIME_PROOF_CASES = (
    "startup-site-processing-enabled",
    "startup-user-site-enabled",
    "startup-unsafe-import-path",
    "startup-bytecode-writes-enabled",
    "startup-foreign-import-path-entry",
    "startup-module-outside-the-bound-tcb",
    "startup-site-adjacent-module",
    "startup-unbound-stdlib-module",
    "startup-unbound-package-module",
    "startup-foreign-launcher-archive-member",
    "startup-foreign-entry-point",
    "startup-foreign-interpreter",
    "startup-foreign-process-image",
    "startup-foreign-runtime-image",
    "startup-incomplete-module-proof",
)


@pytest.mark.parametrize("case", _RUNTIME_PROOF_CASES, ids=_RUNTIME_PROOF_CASES)
def test_worker_runtime_proof_rejects_unbound_startup(case: str, tmp_path: Path):
    plan, _, _ = _worker_fixture(tmp_path)
    identity = plan.executable
    proof = copy.deepcopy(_runtime_proof(identity))
    go_v1.validate_worker_runtime(proof, identity)
    flags = cast(dict[str, int], proof["flags"])
    modules = cast(list[dict[str, str]], proof["modules"])
    path = cast(list[str], proof["path"])
    if case == "startup-site-processing-enabled":
        flags["no_site"] = 0
    elif case == "startup-user-site-enabled":
        flags["no_user_site"] = 0
    elif case == "startup-unsafe-import-path":
        flags["safe_path"] = 0
    elif case == "startup-bytecode-writes-enabled":
        flags["dont_write_bytecode"] = 0
    elif case == "startup-foreign-import-path-entry":
        path.append(str(tmp_path / "attacker-site"))
    elif case == "startup-module-outside-the-bound-tcb":
        modules.append(
            {
                "name": "attacker_startup_hook",
                "path": str(tmp_path / "attacker-site" / "hook.py"),
            }
        )
    elif case == "startup-site-adjacent-module":
        modules.append(
            {
                "name": "reviewer_startup_hook",
                "path": str(identity.startup.site_root / "hook.py"),
            }
        )
    elif case == "startup-unbound-stdlib-module":
        modules.append(
            {
                "name": "unbound_stdlib",
                "path": str(
                    identity.startup.stdlib_root / "unbound_stdlib.py"
                ),
            }
        )
    elif case == "startup-unbound-package-module":
        modules.append(
            {
                "name": "csk.injected",
                "path": str(identity.package_tree.path / "injected.py"),
            }
        )
    elif case == "startup-foreign-launcher-archive-member":
        modules.append(
            {
                "name": "attacker",
                "path": str(identity.launcher.path / "attacker.py"),
            }
        )
    elif case == "startup-foreign-entry-point":
        proof["argv0"] = str(tmp_path / "attacker-csk")
    elif case == "startup-foreign-interpreter":
        proof["executable"] = str(tmp_path / "attacker-python")
    elif case == "startup-foreign-process-image":
        cast(dict[str, str], proof["native"])["process_image"] = str(
            tmp_path / "attacker-process"
        )
    elif case == "startup-foreign-runtime-image":
        cast(dict[str, str], proof["native"])["runtime_image"] = str(
            tmp_path / "attacker-runtime"
        )
    else:
        proof["modules"] = [
            module for module in modules if module["name"] != "csk.cli"
        ]
    with pytest.raises(go_v1.GoV1Error) as raised:
        go_v1.validate_worker_runtime(proof, identity)
    assert raised.value.code == go_v1.CODE_WORKER_IDENTITY_INVALID


def test_worker_runtime_proof_accepts_bound_windows_launcher_zip_member(
    tmp_path: Path,
):
    plan, _, _ = _worker_fixture(tmp_path)
    identity = plan.executable
    proof = copy.deepcopy(_runtime_proof(identity))
    cast(list[str], proof["path"]).append(str(identity.launcher.path))
    cast(list[dict[str, str]], proof["modules"]).append(
        {
            "name": "__main__",
            "path": str(identity.launcher.path / "__main__.py"),
        }
    )

    go_v1.validate_worker_runtime(proof, identity)


def test_worker_rejects_a_site_enabled_startup_before_any_go_process(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    plan, secret, nonce = _worker_fixture(tmp_path)
    poisoned = copy.deepcopy(_runtime_proof(plan.executable))
    cast(dict[str, int], poisoned["flags"])["no_site"] = 0
    executor = RecordingExecutor()
    frames = _run_scripted_worker(
        monkeypatch,
        plan,
        secret,
        nonce,
        _frame({"kind": "list", "nonce": nonce}),
        executor,
        runtime=poisoned,
    )
    assert [frame["kind"] for frame in frames[:-1]] == ["failure"]
    assert frames[-2]["failure"]["code"] == go_v1.CODE_WORKER_IDENTITY_INVALID
    assert frames[-1] == {"worker_status": 3}
    assert executor.calls == []


def test_worker_rejects_first_request_without_launch_capability_authentication(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    plan, secret, nonce = _worker_fixture(tmp_path)
    executor = RecordingExecutor()
    frames = _run_scripted_worker(
        monkeypatch,
        plan,
        secret,
        nonce,
        _frame({"kind": "list", "nonce": nonce}),
        executor,
        manager_auth_secret=b"x" * go_v1._WORKER_LAUNCH_SECRET_BYTES,
    )

    assert frames[-2]["kind"] == "failure"
    assert frames[-2]["failure"]["code"] == (
        go_v1.CODE_WORKER_IDENTITY_INVALID
    )
    assert frames[-1] == {"worker_status": 3}
    assert executor.calls == []


def test_process_executor_rechecks_go_and_tool_identity_before_start(
    tmp_path: Path,
):
    plan, _, _ = _worker_fixture(tmp_path)
    marker = tmp_path / "go-started"
    plan.go_executable.write_text(
        f"#!/bin/sh\nprintf started > {marker}\n",
        encoding="utf-8",
    )
    plan.go_executable.chmod(0o755)
    with pytest.raises(go_v1.GoV1Error) as raised:
        go_v1.SubprocessProcessExecutor().run(
            go_v1.ProcessRequest(
                executable=plan.go_executable,
                identity=plan.process_identity,
                arguments=plan.list_argv,
                cwd=plan.directory,
                environment=plan.environment,
                timeout_seconds=1,
                output_limit=4096,
            )
        )
    assert raised.value.code == go_v1.CODE_WORKER_IDENTITY_INVALID
    assert not marker.exists()


@pytest.mark.skipif(
    sys.platform != "darwin",
    reason="the replacement/restore event guard is the macOS mechanism",
)
@pytest.mark.parametrize("target_name", ("go", "tool"))
def test_macos_identity_guard_detects_process_graph_replacement_and_restore(
    target_name: str,
    tmp_path: Path,
):
    goroot = tmp_path / "goroot"
    go_executable = goroot / "bin" / "go"
    go_executable.parent.mkdir(parents=True)
    shutil.copyfile("/bin/sleep", go_executable)
    go_executable.chmod(0o755)
    tool_directory = goroot / "pkg" / "tool" / "darwin_arm64"
    tool_directory.mkdir(parents=True)
    compiler = tool_directory / "compile"
    shutil.copyfile("/usr/bin/true", compiler)
    compiler.chmod(0o755)
    identity = go_v1._resolve_tool_process_identity(
        go_executable,
        tool_directory,
    )
    guard = go_v1._IdentityMutationGuard(
        go_v1.PLATFORM_MACOS,
        identity.watch_paths(),
    )
    failures: list[BaseException] = []

    def run() -> None:
        try:
            go_v1.SubprocessProcessExecutor().run(
                go_v1.ProcessRequest(
                    executable=go_executable,
                    identity=identity,
                    arguments=("0.2",),
                    cwd=tmp_path,
                    environment={},
                    timeout_seconds=1,
                    output_limit=4096,
                )
            )
        except BaseException as exc:
            failures.append(exc)

    thread = threading.Thread(target=run)
    thread.start()
    time.sleep(0.05)
    target = go_executable if target_name == "go" else compiler
    original = target.read_bytes()
    target.write_bytes(b"\xcf\xfa\xed\xfereplacement")
    target.write_bytes(original)
    thread.join(timeout=2)
    assert not thread.is_alive()
    assert failures == []
    with pytest.raises(go_v1.GoV1Error) as raised:
        guard.close()
    assert raised.value.code == go_v1.CODE_WORKER_IDENTITY_INVALID


def test_worker_environment_matches_the_fixed_darwin_vector(tmp_path: Path):
    plan, _, _ = _worker_fixture(tmp_path)
    operation = plan.private_roots[0].parent
    normalized = {
        name: (
            value
            .replace(str(operation), "<operation-private>")
            .replace(str(plan.goroot), "<resolved-trusted-goroot>")
        )
        for name, value in plan.environment.items()
    }
    assert normalized == {
        "CGO_ENABLED": "0",
        "GO111MODULE": "on",
        "GOARCH": "arm64",
        "GOARM64": "v8.0",
        "GOCACHE": "<operation-private>/gocache",
        "GOENV": "off",
        "GOEXPERIMENT": "",
        "GOFLAGS": "",
        "GOMODCACHE": "<operation-private>/gomodcache",
        "GONOPROXY": "none",
        "GONOSUMDB": "none",
        "GOOS": "darwin",
        "GOPATH": "<operation-private>/gopath",
        "GOPRIVATE": "",
        "GOPROXY": "off",
        "GOROOT": "<resolved-trusted-goroot>",
        "GOSUMDB": "off",
        "GOTMPDIR": "<operation-private>/gotmp",
        "GOTOOLCHAIN": "local",
        "GOVCS": "*:off",
        "GOWORK": "off",
        "GO_EXTLINK_ENABLED": "0",
        "HOME": "<operation-private>/home",
        "LANG": "C",
        "LC_ALL": "C",
        "PATH": "<operation-private>/empty-path",
        "TMPDIR": "<operation-private>/tmp",
        "XDG_CONFIG_HOME": "<operation-private>/config",
    }
    go_v1._validate_worker_environment(
        plan.environment,
        plan.goroot,
        plan.private_roots,
        plan.platform,
    )


def test_worker_bootstrap_uses_one_empty_private_bytecode_cache(
    tmp_path: Path,
):
    plan, _, _ = _worker_fixture(tmp_path)
    assert go_v1._indispensable_worker_environment(
        go_v1.PLATFORM_MACOS,
        plan.worker_cache,
        plan.executable.startup.site_root,
        plan.executable.startup.python_home,
    ) == {
        "PYTHONDONTWRITEBYTECODE": "1",
        "PYTHONNOUSERSITE": "1",
        "PYTHONHOME": str(plan.executable.startup.python_home),
        "PYTHONPATH": str(plan.executable.startup.site_root),
        "PYTHONPYCACHEPREFIX": str(plan.worker_cache),
    }
    go_v1._verify_empty_worker_cache(plan.worker_cache)
    (plan.worker_cache / "injected.pyc").write_bytes(b"poison")
    with pytest.raises(go_v1.GoV1Error) as raised:
        go_v1._verify_empty_worker_cache(plan.worker_cache)
    assert raised.value.code == go_v1.CODE_CONTROL_UNAVAILABLE


def test_windows_worker_bootstrap_uses_manager_owned_profile(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    plan, _, _ = _worker_fixture(tmp_path)
    monkeypatch.setenv("SYSTEMROOT", r"C:\Windows")
    monkeypatch.setenv("WINDIR", r"C:\Windows")
    monkeypatch.setenv("USERPROFILE", r"C:\attacker")

    environment = go_v1._indispensable_worker_environment(
        go_v1.PLATFORM_WINDOWS,
        plan.worker_cache,
        plan.executable.startup.site_root,
        plan.executable.startup.python_home,
    )

    assert environment["USERPROFILE"] == str(plan.worker_cache)
    assert environment["USERPROFILE"] != r"C:\attacker"
    assert environment["SYSTEMROOT"] == r"C:\Windows"
    assert environment["WINDIR"] == r"C:\Windows"


_PACKAGE_INFLUENCE_CASES = (
    ("package-selected-executable", "executable"),
    ("package-selected-argv", "argv"),
    ("package-selected-environment", "environment"),
    ("package-selected-output-path", "output_path"),
    ("package-selected-flags", "flags"),
    ("package-selected-hooks", "hooks"),
    ("package-selected-plugins", "plugins"),
    ("package-selected-generators", "generators"),
)


@pytest.mark.parametrize(
    ("case", "field"),
    _PACKAGE_INFLUENCE_CASES,
    ids=[item[0] for item in _PACKAGE_INFLUENCE_CASES],
)
def test_package_influence_cases(case: str, field: str):
    del case
    request = go_v1.BuildRequest(
        toolchain_session=object(),  # type: ignore[arg-type]
        source_snapshot=object(),  # type: ignore[arg-type]
        command_object={
            "type": "build",
            "driver": "go-v1",
            "source_dir": "build/cmd",
            field: "poison",
        },
        build_root="build",
        source_dir="build/cmd",
        command="fixture",
    )
    with pytest.raises(go_v1.GoV1Error) as raised:
        go_v1._validate_package_command_surface(request)
    assert raised.value.code == go_v1.CODE_PACKAGE_INFLUENCE_FORBIDDEN


def test_hidden_worker_mode_is_not_a_public_parser_surface(
    monkeypatch: pytest.MonkeyPatch,
):
    calls: list[object] = []

    def worker(*, _launch_context: object) -> int:
        calls.append(_launch_context)
        return 0

    monkeypatch.setattr(go_v1, "run_worker", worker)
    assert cli.main([go_v1.WORKER_MODE]) == cli.EXIT_CONFIG
    assert calls == []

    def reject_user_launch() -> object:
        raise go_v1.GoV1Error(
            go_v1.CODE_WORKER_IDENTITY_INVALID,
            "no manager-owned launch capability",
        )

    monkeypatch.setattr(
        go_v1,
        "_consume_worker_launch_context",
        reject_user_launch,
    )
    monkeypatch.setattr("sys.argv", ["/installed/csk", go_v1.WORKER_MODE])
    assert cli.main() == cli.EXIT_CONFIG
    assert calls == []

    context = object()
    monkeypatch.setattr(
        go_v1,
        "_consume_worker_launch_context",
        lambda: context,
    )
    assert cli.main() == 0
    assert calls == [context]


_IDENTITY_PROTOCOL_CASES = (
    "pre-launch-identity-mismatch",
    "worker-executable-symlink-substitution",
    "worker-executable-replaced-between-checks",
    "worker-identity-proof-mismatch",
    "post-build-toolchain-identity-mismatch",
    "post-build-source-snapshot-mutated",
    "unexpected-program-started-below-the-worker",
    "build-permit-before-complete-list-validation",
    "replayed-session-nonce",
    "out-of-order-protocol-message",
    "oversize-protocol-message",
    "unknown-protocol-message-kind",
    "second-build-request-in-one-session",
    "mandatory-control-cannot-be-applied",
)


@pytest.mark.parametrize(
    "case",
    tuple(
        case
        for case in _IDENTITY_PROTOCOL_CASES
        if case != "worker-executable-replaced-between-checks"
    ),
    ids=tuple(
        case
        for case in _IDENTITY_PROTOCOL_CASES
        if case != "worker-executable-replaced-between-checks"
    ),
)
def test_identity_and_protocol_cases(
    case: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    plan, secret, nonce = _worker_fixture(tmp_path)
    identity = plan.executable
    expected = (
        go_v1.CODE_CONTROL_UNAVAILABLE
        if case == "mandatory-control-cannot-be-applied"
        else (
            go_v1.CODE_WORKER_PROTOCOL_INVALID
            if case
            in {
                "build-permit-before-complete-list-validation",
                "replayed-session-nonce",
                "out-of-order-protocol-message",
                "oversize-protocol-message",
                "unknown-protocol-message-kind",
                "second-build-request-in-one-session",
            }
            else go_v1.CODE_WORKER_IDENTITY_INVALID
        )
    )

    if case == "pre-launch-identity-mismatch":
        identity.path.write_text("#!/bin/sh\nexit 1\n", encoding="utf-8")
        identity.path.chmod(0o755)
        with pytest.raises(go_v1.GoV1Error) as raised:
            identity.verify()
        assert raised.value.code == expected
        return
    if case == "worker-executable-symlink-substitution":
        link = tmp_path / "linked-csk"
        link.symlink_to(identity.path)
        with pytest.raises(go_v1.GoV1Error) as raised:
            go_v1._resolve_manager_identity(link)
        assert raised.value.code == expected
        return
    if case == "worker-identity-proof-mismatch":
        proof = copy.deepcopy(identity.to_dict())
        proof["launcher"]["sha256"] = "sha256:" + "0" * 64  # type: ignore[index]
        with pytest.raises(go_v1.GoV1Error) as raised:
            identity.matches_mapping(proof)
        assert raised.value.code == expected
        return
    if case in {
        "post-build-toolchain-identity-mismatch",
        "post-build-source-snapshot-mutated",
    }:
        class Mutated:
            def recheck(self) -> None:
                if case == "post-build-source-snapshot-mutated":
                    raise go_v1.source.SnapshotMutationError("mutated")

            def verify(self) -> None:
                if case == "post-build-toolchain-identity-mismatch":
                    raise go_v1.toolchain.ToolchainError("toolchain_mutated", "mutated")

        mutated = Mutated()
        with pytest.raises(go_v1.GoV1Error) as raised:
            go_v1._verify_frozen_inputs(
                mutated,  # type: ignore[arg-type]
                mutated,  # type: ignore[arg-type]
                "after build",
            )
        assert raised.value.code == expected
        return
    if case == "unexpected-program-started-below-the-worker":
        marker = tmp_path / "outside-program-ran"
        outside = tmp_path / "outside-program"
        outside.write_text(
            f"#!/bin/sh\nprintf poison > {marker}\n",
            encoding="utf-8",
        )
        outside.chmod(0o755)
        attempts: list[go_v1.ProcessRequest] = []

        class OutsideAttemptExecutor:
            def run(
                self,
                request: go_v1.ProcessRequest,
            ) -> go_v1.ProcessResult:
                attempts.append(request)
                return go_v1.SubprocessProcessExecutor().run(
                    replace(request, executable=outside)
                )

        frames = _run_scripted_worker(
            monkeypatch,
            plan,
            secret,
            nonce,
            _frame({"kind": "list", "nonce": nonce}),
            OutsideAttemptExecutor(),
        )
        assert [frame["kind"] for frame in frames[:-1]] == [
            "ready",
            "failure",
        ]
        assert frames[-2]["failure"]["code"] == expected
        assert frames[-1] == {"worker_status": 3}
        assert len(attempts) == 1
        assert not marker.exists()
        return
    if case == "mandatory-control-cannot-be-applied":
        with pytest.raises(go_v1.GoV1Error) as raised:
            go_v1.probe_native_controls(
                go_v1.ResourceLimits(),
                _platform=go_v1.PLATFORM_MACOS,
                _native_probe=lambda _name, _limits: False,
            )
        assert raised.value.code == expected
        return

    permit = go_v1._build_permit(secret, nonce, plan.build_argv)
    executor = RecordingExecutor()
    if case == "build-permit-before-complete-list-validation":
        messages = _frame(
            {"kind": "permit", "nonce": nonce, "permit": permit}
        )
    elif case == "replayed-session-nonce":
        messages = _frame({"kind": "list", "nonce": "c" * 64})
    elif case == "out-of-order-protocol-message":
        messages = _frame({"kind": "shutdown", "nonce": nonce})
    elif case == "oversize-protocol-message":
        messages = struct.pack(">I", go_v1._MAX_PROTOCOL_FRAME + 1)
    elif case == "unknown-protocol-message-kind":
        messages = _frame({"kind": "compile", "nonce": nonce})
    else:
        messages = (
            _frame({"kind": "list", "nonce": nonce})
            + _frame({"kind": "permit", "nonce": nonce, "permit": permit})
            + _frame({"kind": "permit", "nonce": nonce, "permit": permit})
        )
    frames = _run_scripted_worker(
        monkeypatch,
        plan,
        secret,
        nonce,
        messages,
        executor,
    )
    failure = frames[-2]
    assert failure["kind"] == "failure"
    assert failure["failure"]["code"] == expected
    if case == "second-build-request-in-one-session":
        assert len(executor.calls) == 2
    else:
        assert executor.calls == []


class _FakeDomainProcess:
    def __init__(self, pid: int = 4242):
        self.pid = pid
        self.returncode: int | None = None
        self.killed = False

    def wait(self, timeout: float | None = None) -> int:
        del timeout
        self.returncode = -int(signal.SIGKILL)
        return self.returncode

    def poll(self) -> int | None:
        return self.returncode

    def kill(self) -> None:
        self.killed = True
        self.returncode = -int(signal.SIGKILL)


def _bare_domain(platform: str) -> go_v1._NativeControlDomain:
    domain = object.__new__(go_v1._NativeControlDomain)
    domain.platform = platform
    domain.controls = ()
    domain.limits = go_v1.ResourceLimits()
    domain.installed = True
    domain.terminated = False
    domain.job_handle = 0
    domain.file_limit = 0
    domain.launch_secret = b""
    return domain


class _StubIdentityGuard:
    def __init__(self) -> None:
        self.closed = 0

    def close(self) -> None:
        self.closed += 1

    def verify(self) -> None:
        return


def _bare_client(
    plan: go_v1._WorkerPlan,
    domain: go_v1._NativeControlDomain,
) -> tuple[go_v1._WorkerClient, _StubIdentityGuard]:
    client = object.__new__(go_v1._WorkerClient)
    guard = _StubIdentityGuard()
    process = _FakeDomainProcess()
    process.stdin = None  # type: ignore[attr-defined]
    process.stdout = None  # type: ignore[attr-defined]
    process.stderr = None  # type: ignore[attr-defined]
    client.plan = plan
    client.process = process  # type: ignore[assignment]
    client.domain = domain
    client.identity_guard = guard  # type: ignore[assignment]
    client.nonce = "b" * 64
    client.secret = "a" * 64
    client.launch_secret = b"l" * go_v1._WORKER_LAUNCH_SECRET_BYTES
    client.evidence = _valid_evidence(go_v1.PLATFORM_MACOS)
    client.deadline = time.monotonic() + 60.0
    client.finished = False
    client._expired = threading.Event()
    client._expiration_error = None
    client._lifecycle_lock = threading.Lock()
    client.stderr = bytearray()
    client.stderr_overflow = threading.Event()
    client._stderr_thread = threading.Thread(target=lambda: None)
    client._stderr_thread.start()
    client._deadline_timer = threading.Timer(60.0, lambda: None)
    client._deadline_timer.start()
    return client, guard


def test_teardown_failure_cannot_mask_an_identity_diagnostic(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    plan, _, _ = _worker_fixture(tmp_path)
    domain = _bare_domain(go_v1.PLATFORM_MACOS)
    client, guard = _bare_client(plan, domain)
    monkeypatch.setattr(
        go_v1._NativeControlDomain,
        "terminate",
        lambda _domain, _process: (_ for _ in ()).throw(
            go_v1.GoV1Error(
                go_v1.CODE_CONTROL_UNAVAILABLE,
                "injected complete-domain join failure",
            )
        ),
    )
    package_file = plan.executable.package_tree.path / "builds" / "go_v1.py"
    package_file.write_bytes(package_file.read_bytes() + b"# replaced\n")
    with pytest.raises(go_v1.GoV1Error) as raised:
        client.teardown()
    # A slow or failing domain join must never rewrite the public diagnostic of
    # an execution-identity violation.
    assert raised.value.code == go_v1.CODE_WORKER_IDENTITY_INVALID
    assert any(
        "injected complete-domain join failure" in note
        for note in getattr(raised.value, "__notes__", [])
    )
    assert guard.closed == 1


def test_launch_teardown_failure_preserves_the_launch_diagnostic(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    plan, _, _ = _worker_fixture(tmp_path)
    monkeypatch.setattr(
        go_v1._IdentityMutationGuard,
        "__init__",
        lambda self, _platform, _paths: None,
    )
    monkeypatch.setattr(
        go_v1._IdentityMutationGuard,
        "close",
        lambda self: (_ for _ in ()).throw(
            go_v1.GoV1Error(
                go_v1.CODE_CONTROL_UNAVAILABLE,
                "injected identity-set release failure",
            )
        ),
    )
    monkeypatch.setattr(
        go_v1._NativeControlDomain,
        "__init__",
        lambda self, _platform, _probes, _limits: (_ for _ in ()).throw(
            go_v1.GoV1Error(
                go_v1.CODE_WORKER_IDENTITY_INVALID,
                "injected pre-launch identity rejection",
            )
        ),
    )
    with pytest.raises(go_v1.GoV1Error) as raised:
        go_v1._WorkerClient.launch(plan)
    assert raised.value.code == go_v1.CODE_WORKER_IDENTITY_INVALID
    assert any(
        "teardown also failed" in note
        for note in getattr(raised.value, "__notes__", [])
    )


@pytest.mark.parametrize(
    ("first", "second", "expected"),
    (
        (
            go_v1.CODE_CONTROL_UNAVAILABLE,
            go_v1.CODE_WORKER_IDENTITY_INVALID,
            go_v1.CODE_WORKER_IDENTITY_INVALID,
        ),
        (
            go_v1.CODE_WORKER_IDENTITY_INVALID,
            go_v1.CODE_CONTROL_UNAVAILABLE,
            go_v1.CODE_WORKER_IDENTITY_INVALID,
        ),
        (
            go_v1.CODE_WORKER_PROTOCOL_INVALID,
            go_v1.CODE_CAPABILITY_EVIDENCE_INVALID,
            go_v1.CODE_CAPABILITY_EVIDENCE_INVALID,
        ),
    ),
    ids=(
        "identity-wins-over-earlier-control-failure",
        "identity-survives-later-control-failure",
        "evidence-wins-over-protocol-failure",
    ),
)
def test_failure_precedence_is_deterministic(
    first: str,
    second: str,
    expected: str,
):
    dominant = go_v1._dominant_failure(
        go_v1.GoV1Error(first, "first"),
        go_v1.GoV1Error(second, "second"),
    )
    assert isinstance(dominant, go_v1.GoV1Error)
    assert dominant.code == expected


def test_windows_job_close_failure_is_propagated_and_handle_is_retained(
    monkeypatch: pytest.MonkeyPatch,
):
    domain = _bare_domain(go_v1.PLATFORM_WINDOWS)
    domain.job_handle = 73
    process = _FakeDomainProcess()
    monkeypatch.setattr(
        go_v1,
        "_terminate_windows_job",
        lambda _handle, _timeout: None,
    )
    close_calls = 0

    def fail_close(handle: int) -> None:
        nonlocal close_calls
        close_calls += 1
        assert handle == 73
        raise OSError("CloseHandle failed")

    monkeypatch.setattr(go_v1, "_close_windows_handle", fail_close)
    with pytest.raises(go_v1.GoV1Error) as raised:
        domain.terminate(process)  # type: ignore[arg-type]
    assert raised.value.code == go_v1.CODE_CONTROL_UNAVAILABLE
    assert domain.job_handle == 73
    assert close_calls == 1

    monkeypatch.setattr(go_v1, "_close_windows_handle", lambda _handle: None)
    domain.close()
    assert domain.job_handle == 0


def test_windows_job_success_is_terminated_closed_and_joined(
    monkeypatch: pytest.MonkeyPatch,
):
    domain = _bare_domain(go_v1.PLATFORM_WINDOWS)
    domain.job_handle = 73
    process = _FakeDomainProcess()
    terminated: list[tuple[int, float]] = []
    closed: list[int] = []
    monkeypatch.setattr(
        go_v1,
        "_terminate_windows_job",
        lambda handle, timeout: terminated.append((handle, timeout)),
    )
    monkeypatch.setattr(
        go_v1,
        "_close_windows_handle",
        closed.append,
    )
    domain.terminate(process)  # type: ignore[arg-type]
    assert terminated == [(73, go_v1._WORKER_SHUTDOWN_GRACE)]
    assert closed == [73]
    assert domain.job_handle == 0
    assert domain.terminated
    assert process.returncode is not None


def test_macos_process_group_kill_failure_rejects_teardown(
    monkeypatch: pytest.MonkeyPatch,
):
    domain = _bare_domain(go_v1.PLATFORM_MACOS)
    process = _FakeDomainProcess()

    def denied(_pid: int, _signal: int) -> None:
        raise PermissionError("killpg denied")

    monkeypatch.setattr(go_v1.os, "killpg", denied)
    with pytest.raises(go_v1.GoV1Error) as raised:
        domain.terminate(process)  # type: ignore[arg-type]
    assert raised.value.code == go_v1.CODE_CONTROL_UNAVAILABLE
    assert not domain.terminated


def test_macos_process_group_success_is_terminated_and_joined(
    monkeypatch: pytest.MonkeyPatch,
):
    domain = _bare_domain(go_v1.PLATFORM_MACOS)
    process = _FakeDomainProcess()
    calls: list[int] = []

    def terminate_then_absent(pid: int, selected_signal: int) -> None:
        assert pid == process.pid
        calls.append(selected_signal)
        if selected_signal == 0:
            raise ProcessLookupError

    monkeypatch.setattr(go_v1.os, "killpg", terminate_then_absent)
    domain.terminate(process)  # type: ignore[arg-type]
    assert calls == [int(signal.SIGKILL), 0]
    assert domain.terminated
    assert process.returncode is not None


def _root_package(root: Path, *, source: str = "cmd") -> dict[str, object]:
    package_dir = root / source
    package_dir.mkdir(parents=True, exist_ok=True)
    (package_dir / "main.go").write_text(
        "package main\nfunc main() {}\n",
        encoding="utf-8",
    )
    go_mod = root / "go.mod"
    go_mod.write_text("module example.test/tool\ngo 1.25\n", encoding="utf-8")
    return {
        "Dir": str(package_dir),
        "ImportPath": "example.test/tool/cmd",
        "Name": "main",
        "Root": str(root),
        "Module": {
            "Path": "example.test/tool",
            "Main": True,
            "Dir": str(root),
            "GoMod": str(go_mod),
        },
        "GoFiles": ["main.go"],
    }


def _encode_packages(packages: list[dict[str, object]]) -> bytes:
    return b"".join(
        json.dumps(package, separators=(",", ":")).encode() + b"\n"
        for package in packages
    )


def test_valid_complete_package_graph_is_accepted(tmp_path: Path):
    root = tmp_path / "build"
    package = _root_package(root)
    go_v1.validate_package_graph(
        _encode_packages([package]),
        build_root=root,
        source_dir=root / "cmd",
        goroot=tmp_path / "goroot",
    )


_GRAPH_CASES = (
    ("non-main-package", "build_package_not_main"),
    ("multiple-packages", "build_package_ambiguous"),
    ("missing-vendored-dependency", "vendor_dependency_missing"),
    ("inconsistent-vendor-modules", "vendor_metadata_inconsistent"),
    ("workspace-only-dependency", "workspace_dependency_forbidden"),
    ("cgo-only-package", "cgo_required"),
    ("native-c-input", "go_native_input_forbidden"),
    ("native-cxx-input", "go_native_input_forbidden"),
    ("native-swig-input", "go_native_input_forbidden"),
    ("root-syso-input", "go_syso_forbidden"),
    ("transitive-syso-input", "go_syso_forbidden"),
    ("root-assembly-absolute-include", "go_assembly_forbidden"),
    ("transitive-assembly-escaping-include", "go_assembly_forbidden"),
    ("escaped-embed-input", "go_embed_input_escape"),
)


@pytest.mark.parametrize(
    ("case", "expected"),
    _GRAPH_CASES,
    ids=[item[0] for item in _GRAPH_CASES],
)
def test_package_graph_rejection_surface(case: str, expected: str, tmp_path: Path):
    root = tmp_path / "build"
    package = _root_package(root)
    packages = [package]

    if case == "non-main-package":
        package["Name"] = "library"
    elif case == "multiple-packages":
        second = copy.deepcopy(package)
        second["ImportPath"] = "example.test/tool/second"
        packages.append(second)
    elif case in {"missing-vendored-dependency", "inconsistent-vendor-modules"}:
        dep_dir = root / "vendor" / "example.test" / "dep"
        dep_dir.mkdir(parents=True)
        (dep_dir / "dep.go").write_text("package dep\n", encoding="utf-8")
        dep = {
            "Dir": str(dep_dir),
            "ImportPath": "example.test/dep",
            "Name": "dep",
            "Root": str(root),
            "DepOnly": True,
            "Module": {
                "Path": "example.test/dep",
                "Version": (
                    "" if case == "missing-vendored-dependency" else "v1.0.0"
                ),
                "Dir": str(dep_dir),
            },
            "GoFiles": ["dep.go"],
        }
        packages.append(dep)
    elif case == "workspace-only-dependency":
        (root / "go.work").write_text("go 1.25\n", encoding="utf-8")
        snapshot = type("Snapshot", (), {"path": tmp_path})()
        with pytest.raises(go_v1.GoV1Error) as raised:
            go_v1._canonical_build_directories(
                snapshot,  # type: ignore[arg-type]
                "build",
                "build/cmd",
            )
        assert raised.value.code == expected
        return
    elif case == "cgo-only-package":
        package["CgoFiles"] = ["main.go"]
    elif case == "native-c-input":
        package["CFiles"] = ["input.c"]
    elif case == "native-cxx-input":
        package["CXXFiles"] = ["input.cc"]
    elif case == "native-swig-input":
        package["SwigFiles"] = ["input.swig"]
    elif case == "root-syso-input":
        package["SysoFiles"] = ["root.syso"]
    elif case in {
        "transitive-syso-input",
        "transitive-assembly-escaping-include",
    }:
        dep_dir = root / "internal" / "dep"
        dep_dir.mkdir(parents=True)
        (dep_dir / "dep.go").write_text("package dep\n", encoding="utf-8")
        dep = copy.deepcopy(package)
        dep.update(
            {
                "Dir": str(dep_dir),
                "ImportPath": "example.test/tool/internal/dep",
                "Name": "dep",
                "DepOnly": True,
                "GoFiles": ["dep.go"],
            }
        )
        if case == "transitive-syso-input":
            dep["SysoFiles"] = ["dep.syso"]
        else:
            dep["SFiles"] = ["dep.s"]
        packages.append(dep)
    elif case == "root-assembly-absolute-include":
        package["SFiles"] = ["root.s"]
    elif case == "escaped-embed-input":
        outside = tmp_path / "outside.txt"
        outside.write_text("escape", encoding="utf-8")
        package["EmbedFiles"] = [str(outside)]

    with pytest.raises(go_v1.GoV1Error) as raised:
        go_v1.validate_package_graph(
            _encode_packages(packages),
            build_root=root,
            source_dir=root / "cmd",
            goroot=tmp_path / "goroot",
        )
    assert raised.value.code == expected


@pytest.mark.parametrize(
    ("name", "source_text", "pgo", "expected"),
    [
        (
            "cgo-import-dynamic-directive",
            "package main\n//go:cgo_import_dynamic x y \"z\"\nfunc main() {}\n",
            False,
            "go_forbidden_compiler_directive",
        ),
        (
            "attempted-go-generate",
            "package main\n//go:generate sh -c poison\nfunc main() {}\n",
            False,
            "go_generator_forbidden",
        ),
        (
            "default-pgo",
            "package main\nfunc main() {}\n",
            True,
            "go_pgo_forbidden",
        ),
    ],
)
def test_compiler_directive_and_pgo_guards(
    name: str,
    source_text: str,
    pgo: bool,
    expected: str,
    tmp_path: Path,
):
    del name
    root = tmp_path / "build"
    package = _root_package(root)
    (root / "cmd" / "main.go").write_text(source_text, encoding="utf-8")
    if pgo:
        (root / "cmd" / "default.pgo").write_text("pgo", encoding="utf-8")
    with pytest.raises(go_v1.GoV1Error) as raised:
        go_v1.validate_package_graph(
            _encode_packages([package]),
            build_root=root,
            source_dir=root / "cmd",
            goroot=tmp_path / "goroot",
        )
    assert raised.value.code == expected


def test_poisoned_compiler_environment_is_rejected(tmp_path: Path):
    plan, _, _ = _worker_fixture(tmp_path)
    poisoned = dict(plan.environment)
    poisoned.update(
        {
            "GOFLAGS": "-tags=attacker",
            "GOWORK": "/attacker/go.work",
            "GOTOOLCHAIN": "auto",
            "CC": "/attacker/cc",
            "HTTP_PROXY": "http://attacker",
        }
    )
    with pytest.raises(go_v1.GoV1Error) as raised:
        go_v1._validate_worker_environment(
            poisoned,
            plan.goroot,
            plan.private_roots,
            plan.platform,
        )
    assert raised.value.code == go_v1.CODE_WORKER_PROTOCOL_INVALID


def test_output_verification_hashes_permissions_and_never_executes(tmp_path: Path):
    stage = tmp_path / "stage"
    output = stage / "bin" / "tool"
    output.parent.mkdir(parents=True)
    payload = b"\xcf\xfa\xed\xfe" + b"never-run"
    output.write_bytes(payload)
    output.chmod(0o600)
    metadata = go_v1._verify_artifact(
        stage,
        output,
        "bin/tool",
        len(payload),
        go_v1.PLATFORM_MACOS,
    )
    assert metadata.path == "bin/tool"
    assert metadata.size == len(payload)
    assert metadata.sha256.startswith("sha256:")
    assert stat.S_IMODE(output.stat().st_mode) == 0o700
    assert output.read_bytes() == payload
