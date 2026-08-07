from __future__ import annotations

import hashlib
import json
import os
import platform
import stat
import subprocess
import sys
import time
from dataclasses import replace
from pathlib import Path

import pytest
from conftest import (
    make_config,
    make_project,
    make_skill_repo,
    write_files,
    write_skillfile,
)

from csk import cli, consumers, hybrid, installer, transactions
from csk.audit import pipeline as audit_pipeline
from csk.builds import go_v1
from csk.builds import metadata as build_metadata
from csk.builds import toolchain as build_toolchain

POSIX_BUILD_VECTOR = pytest.mark.skipif(
    os.name != "posix",
    reason="Exercises the POSIX protected build cache and launchers",
)

CAPS = {"exec": "none", "network": "none"}


def _native_target() -> build_toolchain.NativeTarget:
    if os.name == "nt":
        goos = "windows"
    else:
        goos = "darwin" if sys.platform == "darwin" else "linux"
    machine = platform.machine().lower()
    if machine in {"arm64", "aarch64"}:
        return build_toolchain.NativeTarget(
            goos=goos,
            goarch="arm64",
            tuning={"GOARM64": "v8.0"},
        )
    if machine in {"x86_64", "amd64"}:
        return build_toolchain.NativeTarget(
            goos=goos,
            goarch="amd64",
            tuning={"GOAMD64": "v1"},
        )
    pytest.skip(f"unsupported test architecture: {machine}")


def _install_fake_build_pipeline(
    monkeypatch: pytest.MonkeyPatch,
    *,
    events: list[str],
    fail_command: str | None = None,
    fail_error: BaseException | None = None,
) -> None:
    target = _native_target()
    identity = build_toolchain.ToolchainIdentity(
        algorithm=build_toolchain.TOOLCHAIN_ALGORITHM,
        content_sha256="sha256:" + "a" * 64,
        go_relpath=build_toolchain.GO_RELPATH,
        go_version=(f"go version go1.25.5 {target.goos}/{target.goarch}"),
    )

    class FakeSession:
        def __init__(self, config: build_toolchain.ToolchainConfig):
            self.target = target
            self.toolchain = identity
            self.operation_root = config.private_base / "operation"
            self.operation_root.mkdir(mode=0o700)
            self.executable = self.operation_root / "go"
            self.goroot = self.operation_root / "goroot"

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return None

    def fake_build(request: go_v1.BuildRequest) -> go_v1.BuildResult:
        events.append(f"build:{request.command}")
        if request.command == fail_command:
            if fail_error is not None:
                raise fail_error
            raise go_v1.GoV1Error(
                "fixture_build_failure",
                f"forced failure for {request.command}",
            )
        if target.goos == "windows":
            payload = (f"compiled fixture: {request.command}\n").encode()
        else:
            payload = (
                f"#!/bin/sh\nprintf '%s\\n' {request.command}\n"
            ).encode()
        artifact_path = request.toolchain_session.operation_root / (
            f"artifact-{request.command}"
        )
        artifact_path.write_bytes(payload)
        artifact_path.chmod(0o700)
        digest = "sha256:" + hashlib.sha256(payload).hexdigest()
        return go_v1.BuildResult(
            artifact=go_v1.BuildArtifact(
                staged_path=artifact_path,
                metadata=go_v1.ArtifactMetadata(
                    path=build_metadata.derived_artifact_path(
                        request.command,
                        goos=target.goos,
                    ),
                    sha256=digest,
                    size=len(payload),
                ),
            ),
            capability_evidence=go_v1.CapabilityEvidence(
                record_version="capability-evidence-v1",
                execution_policy="manager-worker-v1",
                platform=target.goos,
                controls=(),
            ),
        )

    monkeypatch.setattr(
        build_toolchain,
        "capture_operator_search_path",
        lambda: build_toolchain.OperatorSearchPath(("/fixture/bin",)),
    )
    monkeypatch.setattr(build_toolchain, "preflight_toolchain", lambda _config: None)
    monkeypatch.setattr(build_toolchain, "establish_toolchain", FakeSession)
    monkeypatch.setattr(go_v1, "build", fake_build)


def _exhausted_fingerprint_deadline() -> build_toolchain.ToolchainError:
    """The error the product raises on a missed deadline, notes and all."""

    with pytest.raises(build_toolchain.ToolchainError) as raised:
        build_toolchain._check_deadline(time.monotonic() - 1.0)
    return raised.value


def _build_skill_files(
    *commands: str,
    requirements: dict[str, object] | None = None,
) -> dict[str, str]:
    payload: dict[str, object] = {
        "schema_version": 6,
        "capabilities": CAPS,
        "build_roots": ["build"],
        "commands": {
            command: {
                "type": "build",
                "driver": "go-v1",
                "source_dir": f"build/cmd/{command}",
            }
            for command in commands
        },
    }
    if requirements:
        payload["dependencies"] = {"skills": requirements}
    files = {
        "agent-skill.json": json.dumps(payload),
        "build/go.mod": "module example.com/tools\n\ngo 1.23\n",
    }
    for command in commands:
        files[f"build/cmd/{command}/main.go"] = "package main\n\nfunc main() {}\n"
    return files


def _write_hybrid_manifest(csk_home: Path, entries: list[dict[str, object]]) -> None:
    path = hybrid.hybrid_manifest_path(csk_home)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps({"schema_version": 1, "skills": entries}, indent=2) + "\n",
        encoding="utf-8",
    )


def _tree_state(roots: tuple[Path, ...]) -> dict[str, tuple[object, ...]]:
    state: dict[str, tuple[object, ...]] = {}
    for root in roots:
        key = str(root)
        if not root.exists() and not root.is_symlink():
            state[key] = ("missing",)
            continue
        for path in (root, *root.rglob("*")):
            info = path.lstat()
            relative = "." if path == root else path.relative_to(root).as_posix()
            if stat.S_ISREG(info.st_mode):
                value: object = path.read_bytes()
                kind = "file"
            elif stat.S_ISLNK(info.st_mode):
                value = os.readlink(path)
                kind = "link"
            elif stat.S_ISDIR(info.st_mode):
                value = None
                kind = "directory"
            else:
                value = None
                kind = "special"
            state[f"{key}:{relative}"] = (
                kind,
                stat.S_IMODE(info.st_mode),
                value,
            )
    return state


@POSIX_BUILD_VECTOR
def test_real_builds_run_provider_first_then_lexically_and_activate_marker_v2(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    skills_root: Path,
    csk_home: Path,
) -> None:
    project = make_project(tmp_path)
    provider_repo, _ = make_skill_repo(
        skills_root,
        "provider",
        _build_skill_files("zeta", "alpha"),
        tag="v1",
    )
    make_skill_repo(
        skills_root,
        "consumer",
        _build_skill_files(
            "middle",
            requirements={
                "provider": {
                    "git": str(provider_repo),
                    "ref": {"kind": "tag", "value": "v1"},
                }
            },
        ),
        tag="v1",
    )
    write_skillfile(
        project,
        {
            "schema_version": 1,
            "agents": ["claude_code"],
            "skills": [{"name": "consumer", "tag": "v1"}],
        },
    )
    cfg = make_config(
        csk_home,
        skills_root,
        project,
        agents=["claude_code"],
    )
    events: list[str] = []
    _install_fake_build_pipeline(monkeypatch, events=events)

    monkeypatch.setattr(
        installer.audit_pipeline,
        "gate_plans",
        lambda *args, **kwargs: (
            events.append("audit") or audit_pipeline.GateResult(reports=())
        ),
    )

    def engine_factory(home: Path) -> transactions.TransactionEngine:
        return transactions.TransactionEngine(
            home,
            fault_hook=lambda point, target: events.append(f"transaction:{point}"),
        )

    monkeypatch.setattr(
        installer,
        "_transaction_engine",
        engine_factory,
        raising=False,
    )

    result = installer.install(cfg)[0]

    assert not result.errors, result.errors
    assert [event for event in events if event.startswith("build:")] == [
        "build:alpha",
        "build:zeta",
        "build:middle",
    ]
    assert events.index("audit") < events.index("build:alpha")
    assert events.index("build:middle") < events.index("transaction:prepared")
    assert [plan.command for plan in result.builds] == [
        "alpha",
        "zeta",
        "middle",
    ]
    marker_path = project / ".agents" / "skills" / "provider" / ".csk-install.json"
    marker = json.loads(marker_path.read_text(encoding="utf-8"))
    assert marker["schema_version"] == 2
    assert marker["build_roots"] == ["build"]
    assert list(marker["builds"]) == ["alpha", "zeta"]
    assert marker["build_source"]["algorithm"] == "curator-build-source-v1"
    assert not (project / ".agents" / "skills" / "provider" / "build").exists()
    for command in ("alpha", "zeta", "middle"):
        shim = project / ".agents" / "bin" / command
        assert shim.is_file() and not shim.is_symlink()
        executed = subprocess.run(
            [shim],
            check=True,
            text=True,
            capture_output=True,
        )
        assert executed.stdout == f"{command}\n"
    assert consumers.load_consumers(csk_home) == [project.resolve()]


@POSIX_BUILD_VECTOR
def test_build_failure_preserves_every_live_materialization_surface(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    skills_root: Path,
    csk_home: Path,
) -> None:
    project = make_project(tmp_path)
    make_skill_repo(
        skills_root,
        "compiled",
        _build_skill_files("broken"),
        tag="v1",
    )
    write_skillfile(
        project,
        {
            "schema_version": 1,
            "agents": ["codex_cli", "claude_code"],
            "skills": [{"name": "compiled", "tag": "v1"}],
        },
    )
    cfg = make_config(
        csk_home,
        skills_root,
        project,
        agents=["codex_cli", "claude_code"],
    )
    write_files(
        project,
        {
            ".agents/sentinel": "project-live\n",
            ".codex/sentinel": "codex-live\n",
            ".claude/sentinel": "claude-live\n",
        },
    )
    write_files(
        csk_home,
        {
            "runtime/sentinel/tool": "runtime-live\n",
            "hybrid/skills/sentinel/SKILL.md": "hybrid-live\n",
            "consumers.json": (
                '{"schema_version":1,"consumers":["/existing/project"]}\n'
            ),
        },
    )
    watched = (
        project / ".agents",
        project / ".codex",
        project / ".claude",
        csk_home / "runtime",
        csk_home / "hybrid",
        csk_home / "builds",
        csk_home / "consumers.json",
    )
    before = _tree_state(watched)
    events: list[str] = []
    _install_fake_build_pipeline(
        monkeypatch,
        events=events,
        fail_command="broken",
    )

    result = installer.install(cfg)[0]

    assert result.status == "failed"
    assert result.errors == ["go-v1 fixture_build_failure: forced failure for broken"]
    assert events[-1] == "build:broken"
    assert _tree_state(watched) == before


@POSIX_BUILD_VECTOR
def test_project_boundary_reports_the_operator_remedy_a_failure_carries(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    skills_root: Path,
    csk_home: Path,
) -> None:
    """A missed fingerprint deadline names its override where an operator reads.

    The project boundary records failures as strings, so a remedy attached to
    the exception only reaches the operator if the boundary renders it.
    """

    project = make_project(tmp_path)
    make_skill_repo(
        skills_root,
        "compiled",
        _build_skill_files("broken"),
        tag="v1",
    )
    write_skillfile(
        project,
        {
            "schema_version": 1,
            "agents": ["codex_cli"],
            "skills": [{"name": "compiled", "tag": "v1"}],
        },
    )
    cfg = make_config(csk_home, skills_root, project, agents=["codex_cli"])
    events: list[str] = []
    _install_fake_build_pipeline(
        monkeypatch,
        events=events,
        fail_command="broken",
        fail_error=_exhausted_fingerprint_deadline(),
    )

    result = installer.install(cfg)[0]

    assert result.status == "failed"
    reported = result.errors[0]
    # The cross-implementation protocol string stays the first line, byte for
    # byte; the remedy follows it instead of replacing it.
    assert reported.splitlines()[0] == (
        "go-v1 toolchain_timeout: toolchain fingerprint deadline exceeded"
    )
    assert build_toolchain.FINGERPRINT_TIMEOUT_ENV in reported


@POSIX_BUILD_VECTOR
def test_csk_install_prints_the_operator_remedy_to_stderr(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    skills_root: Path,
    csk_home: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """What an operator actually reads when the deadline is exhausted.

    Drives the whole chain the reported Windows failure took: the build driver
    raises, the project boundary records it, and `csk install` prints it.
    """

    project = make_project(tmp_path)
    make_skill_repo(
        skills_root,
        "compiled",
        _build_skill_files("broken"),
        tag="v1",
    )
    write_skillfile(
        project,
        {
            "schema_version": 1,
            "agents": ["codex_cli"],
            "skills": [{"name": "compiled", "tag": "v1"}],
        },
    )
    cfg_path = csk_home / "config.json"
    cfg_path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "skills_root": str(skills_root),
                "projects": {"app": {"path": str(project), "agents": ["codex_cli"]}},
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("CSK_CONFIG", str(cfg_path))
    monkeypatch.chdir(project)
    events: list[str] = []
    _install_fake_build_pipeline(
        monkeypatch,
        events=events,
        fail_command="broken",
        fail_error=_exhausted_fingerprint_deadline(),
    )

    code = cli.main(["install"])
    captured = capsys.readouterr()

    assert code == cli.EXIT_PARTIAL_FAIL
    printed = captured.err.splitlines()
    reported = next(
        index
        for index, line in enumerate(printed)
        if line.endswith(
            ": go-v1 toolchain_timeout: toolchain fingerprint deadline exceeded"
        )
    )
    # The remedy lands on the line after the protocol string, not inside it.
    assert build_toolchain.FINGERPRINT_TIMEOUT_ENV in printed[reported + 1]


def test_failure_text_renders_notes_and_leaves_plain_failures_alone() -> None:
    plain = RuntimeError("forced failure for broken")
    assert installer.failure_text(plain) == "forced failure for broken"

    annotated = RuntimeError("primary failure")
    annotated.add_note("first remedy")
    annotated.add_note("second remedy")
    assert installer.failure_text(annotated) == (
        "primary failure\nfirst remedy\nsecond remedy"
    )


@POSIX_BUILD_VECTOR
def test_publication_failure_leaves_materialization_surfaces_unchanged(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    skills_root: Path,
    csk_home: Path,
) -> None:
    project = make_project(tmp_path)
    make_skill_repo(
        skills_root,
        "compiled",
        _build_skill_files("tool"),
        tag="v1",
    )
    write_skillfile(
        project,
        {
            "schema_version": 1,
            "agents": ["claude_code"],
            "skills": [{"name": "compiled", "tag": "v1"}],
        },
    )
    cfg = make_config(
        csk_home,
        skills_root,
        project,
        agents=["claude_code"],
    )
    events: list[str] = []
    _install_fake_build_pipeline(monkeypatch, events=events)
    backend = installer.build_cache.cache_for_manager_home(csk_home)

    class PublishFailureCache:
        manager_home = backend.manager_home

        def inspect(self, expectation):
            return backend.inspect(expectation)

        def publish(self, publication, *, guard):
            raise RuntimeError("forced publication failure")

        def quarantine(self, cache_key, *, guard):
            return backend.quarantine(cache_key, guard=guard)

    monkeypatch.setattr(
        installer.build_cache,
        "cache_for_manager_home",
        lambda _home: PublishFailureCache(),
    )
    watched = (
        project / ".agents",
        project / ".claude",
        csk_home / "runtime",
        csk_home / "hybrid",
        csk_home / "builds",
        csk_home / "consumers.json",
    )
    before = _tree_state(watched)

    result = installer.install(cfg)[0]

    assert result.status == "failed"
    assert result.errors == ["forced publication failure"]
    assert events == ["build:tool"]
    assert _tree_state(watched) == before


def test_commit_generation_change_restarts_complete_project_plan(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    skills_root: Path,
    csk_home: Path,
) -> None:
    project = make_project(tmp_path)
    write_skillfile(project, {"schema_version": 1, "skills": []})
    cfg = make_config(csk_home, skills_root, project)
    first = "sha256:" + "1" * 64
    second = "sha256:" + "2" * 64
    observations = iter(
        [
            {"shared": first},
            {"shared": first},
            {"shared": second},
            {"shared": second},
            {"shared": second},
            {"shared": second},
        ]
    )

    class Generation:
        def capture(self):
            return next(observations)

    generation = Generation()
    audits: list[str] = []
    monkeypatch.setattr(
        installer,
        "_project_generation_probe",
        lambda _config, _project: generation,
    )
    monkeypatch.setattr(
        installer.audit_pipeline,
        "gate_plans",
        lambda *args, **kwargs: (
            audits.append("audit") or audit_pipeline.GateResult(reports=())
        ),
    )

    result = installer.install(cfg)[0]

    assert not result.errors, result.errors
    assert audits == ["audit", "audit"]
    with pytest.raises(StopIteration):
        next(observations)


@POSIX_BUILD_VECTOR
def test_hybrid_build_is_committed_to_shared_marker_and_project_shim(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    skills_root: Path,
    csk_home: Path,
) -> None:
    project = make_project(tmp_path)
    make_skill_repo(
        skills_root,
        "hybrid-tool",
        _build_skill_files("hybrid-command"),
        tag="v1",
    )
    write_skillfile(
        project,
        {
            "schema_version": 1,
            "agents": ["claude_code"],
            "skills": [],
        },
    )
    _write_hybrid_manifest(
        csk_home,
        [
            {
                "name": "hybrid-tool",
                "tag": "v1",
                "targets": [str(project)],
            }
        ],
    )
    cfg = make_config(
        csk_home,
        skills_root,
        project,
        agents=["claude_code"],
    )
    events: list[str] = []
    _install_fake_build_pipeline(monkeypatch, events=events)

    result = installer.install(cfg)[0]

    assert not result.errors, result.errors
    marker_path = (
        hybrid.hybrid_skills_root(csk_home) / "hybrid-tool" / ".csk-install.json"
    )
    marker = json.loads(marker_path.read_text(encoding="utf-8"))
    assert marker["schema_version"] == 2
    assert list(marker["builds"]) == ["hybrid-command"]
    assert not (marker_path.parent / "build").exists()
    shim = project / ".agents" / "bin" / "hybrid-command"
    assert (
        subprocess.run(
            [shim],
            check=True,
            text=True,
            capture_output=True,
        ).stdout
        == "hybrid-command\n"
    )
    assert (project / ".claude" / "skills" / "hybrid-tool" / "SKILL.md").exists()


@POSIX_BUILD_VECTOR
def test_two_projects_share_verified_compiled_cache_winner(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    skills_root: Path,
    csk_home: Path,
) -> None:
    project_one = make_project(tmp_path, "project-one")
    project_two = make_project(tmp_path, "project-two")
    make_skill_repo(
        skills_root,
        "compiled",
        _build_skill_files("shared-tool"),
        tag="v1",
    )
    for project in (project_one, project_two):
        write_skillfile(
            project,
            {
                "schema_version": 1,
                "skills": [{"name": "compiled", "tag": "v1"}],
            },
        )
    base = make_config(csk_home, skills_root, project_one)
    cfg = replace(
        base,
        projects={
            "one": replace(
                base.projects["app"],
                alias="one",
                path=project_one,
            ),
            "two": replace(
                base.projects["app"],
                alias="two",
                path=project_two,
            ),
        },
    )
    events: list[str] = []
    _install_fake_build_pipeline(monkeypatch, events=events)

    first = installer.install(cfg, alias="one")[0]
    second = installer.install(cfg, alias="two")[0]

    assert not first.errors, first.errors
    assert not second.errors, second.errors
    assert events == ["build:shared-tool"]
    markers = [
        json.loads(
            (
                project / ".agents" / "skills" / "compiled" / ".csk-install.json"
            ).read_text(encoding="utf-8")
        )
        for project in (project_one, project_two)
    ]
    assert markers[0]["builds"] == markers[1]["builds"]
    shared_build = markers[0]["builds"]["shared-tool"]
    artifact = (
        csk_home
        / "builds"
        / "go-v1"
        / shared_build["cache_key"].removeprefix("sha256:")
        / shared_build["artifact_path"]
    )
    assert artifact.is_file()
    for project in (project_one, project_two):
        shim = project / ".agents" / "bin" / "shared-tool"
        assert (
            subprocess.run(
                [shim],
                check=True,
                text=True,
                capture_output=True,
            ).stdout
            == "shared-tool\n"
        )
    assert consumers.load_consumers(csk_home) == sorted(
        (project_one.resolve(), project_two.resolve()),
        key=str,
    )


def test_second_project_consumer_failure_rolls_back_without_touching_first(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    skills_root: Path,
    csk_home: Path,
) -> None:
    project_one = make_project(tmp_path, "project-one")
    project_two = make_project(tmp_path, "project-two")
    make_skill_repo(skills_root, "context-skill", tag="v1")
    for project in (project_one, project_two):
        write_skillfile(
            project,
            {
                "schema_version": 1,
                "agents": ["claude_code"],
                "skills": [{"name": "context-skill", "tag": "v1"}],
            },
        )
    base = make_config(
        csk_home,
        skills_root,
        project_one,
        agents=["claude_code"],
    )
    cfg = replace(
        base,
        projects={
            "one": replace(
                base.projects["app"],
                alias="one",
                path=project_one,
            ),
            "two": replace(
                base.projects["app"],
                alias="two",
                path=project_two,
            ),
        },
    )

    first = installer.install(cfg, alias="one")[0]
    assert not first.errors, first.errors
    watched = (
        project_one / ".agents",
        project_one / ".claude",
        project_two / ".agents",
        project_two / ".claude",
        csk_home / "runtime",
        csk_home / "hybrid",
        csk_home / "consumers.json",
    )
    before = _tree_state(watched)
    committed: list[str] = []

    def fail_consumer(point: str, target: transactions.JournalTarget | None) -> None:
        if point != "target_committed" or target is None:
            return
        committed.append(target.target_class)
        if target.target_class == "90-consumer":
            raise RuntimeError("forced consumer failure")

    monkeypatch.setattr(
        installer,
        "_transaction_engine",
        lambda home: transactions.TransactionEngine(
            home,
            fault_hook=fail_consumer,
        ),
        raising=False,
    )

    second = installer.install(cfg, alias="two")[0]

    assert second.status == "failed"
    assert second.errors == ["forced consumer failure"]
    assert committed[-1] == "90-consumer"
    assert _tree_state(watched) == before
    assert consumers.load_consumers(csk_home) == [project_one.resolve()]
