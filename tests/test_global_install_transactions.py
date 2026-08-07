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
    commit_all,
    make_config,
    make_project,
    make_skill_repo,
    write_files,
)

from csk import (
    adapters,
    global_bins,
    global_install,
    installer,
    locking,
    transactions,
)
from csk.audit import pipeline as audit_pipeline
from csk.builds import go_v1
from csk.builds import toolchain as build_toolchain

POSIX_BUILD_VECTOR = pytest.mark.skipif(
    os.name != "posix",
    reason="Exercises the POSIX protected build cache and launchers",
)

CAPS = {"exec": "none", "network": "none"}


def _write_global_skillfile(csk_home: Path, skills: list[dict[str, str]]) -> None:
    root = csk_home / "global"
    root.mkdir(parents=True, exist_ok=True)
    (root / "Skillfile.json").write_text(
        json.dumps(
            {
                "schema_version": 1,
                "agents": ["claude_code"],
                "skills": skills,
            },
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )


def _native_target() -> build_toolchain.NativeTarget:
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
        go_version=f"go version go1.25.5 {target.goos}/{target.goarch}",
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
        assert locking._STATE.home is None
        events.append(f"build:{request.command}:home-unlocked")
        if request.command == fail_command:
            if fail_error is not None:
                raise fail_error
            raise go_v1.GoV1Error(
                "fixture_build_failure",
                f"forced failure for {request.command}",
            )
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
                    path=f"bin/{request.command}",
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
    revision: str = "one",
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
        files[f"build/cmd/{command}/main.go"] = (
            f"package main\n\n// {revision}\nfunc main() {{}}\n"
        )
    return files


def _script_skill_files(*commands: str, revision: str) -> dict[str, str]:
    return {
        "agent-skill.json": json.dumps(
            {
                "schema_version": 6,
                "capabilities": CAPS,
                "runtime_roots": ["scripts"],
                "commands": {
                    command: {
                        "type": "script",
                        "unix_path": f"scripts/{command}",
                        "win_path": f"scripts/{command}.cmd",
                    }
                    for command in commands
                },
            }
        ),
        "SKILL.md": (
            f"---\nname: scripted\n---\n\n# {revision}\n"
        ),
        **{
            f"scripts/{command}": (
                f"#!/bin/sh\nprintf '%s\\n' {revision}-{command}\n"
            )
            for command in commands
        },
        **{
            f"scripts/{command}.cmd": (
                f"@echo off\r\necho {revision}-{command}\r\n"
            )
            for command in commands
        },
    }


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
def test_global_builds_publish_marker_v2_and_activate_every_bin_surface(
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
    _write_global_skillfile(
        csk_home,
        [{"name": "consumer", "tag": "v1"}],
    )
    cfg = replace(
        make_config(csk_home, skills_root, project),
        adapter_mode="copy",
    )
    user_bin = tmp_path / "user-bin"
    monkeypatch.setenv("CSK_GLOBAL_USER_BIN", str(user_bin))
    events: list[str] = []
    _install_fake_build_pipeline(monkeypatch, events=events)
    monkeypatch.setattr(
        global_install.audit_pipeline,
        "gate_plans",
        lambda *args, **kwargs: (
            events.append("audit")
            or audit_pipeline.GateResult(reports=())
        ),
    )

    def engine_factory(home: Path) -> transactions.TransactionEngine:
        return transactions.TransactionEngine(
            home,
            fault_hook=lambda point, target: events.append(
                f"transaction:{point}"
            ),
        )

    monkeypatch.setattr(global_install, "_transaction_engine", engine_factory)

    result = global_install.install(cfg)

    assert not result.errors, result.errors
    builds = [
        event for event in events if event.startswith("build:")
    ]
    assert builds == [
        "build:alpha:home-unlocked",
        "build:zeta:home-unlocked",
        "build:middle:home-unlocked",
    ]
    assert events.index("audit") < events.index(builds[0])
    assert events.index(builds[-1]) < events.index("transaction:prepared")
    assert [plan.command for plan in result.builds] == [
        "alpha",
        "zeta",
        "middle",
    ]
    marker_path = (
        csk_home
        / "global"
        / "skills"
        / "provider"
        / ".csk-install.json"
    )
    marker = json.loads(marker_path.read_text(encoding="utf-8"))
    assert marker["schema_version"] == 2
    assert marker["build_roots"] == ["build"]
    assert list(marker["builds"]) == ["alpha", "zeta"]
    assert marker["build_source"]["algorithm"] == "curator-build-source-v1"
    assert not (marker_path.parent / "build").exists()
    assert (Path.home() / ".claude" / "skills" / "provider").is_dir()
    for command in ("alpha", "zeta", "middle"):
        canonical = csk_home / "global" / "bin" / command
        published = user_bin / command
        assert canonical.is_file() and not canonical.is_symlink()
        assert published.is_symlink()
        for executable in (canonical, published):
            proc = subprocess.run(
                [executable],
                check=True,
                text=True,
                capture_output=True,
            )
            assert proc.stdout == f"{command}\n"


@POSIX_BUILD_VECTOR
@pytest.mark.parametrize("failure", ["build", "publication"])
def test_global_build_or_publication_failure_preserves_prior_install(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    skills_root: Path,
    csk_home: Path,
    failure: str,
) -> None:
    project = make_project(tmp_path)
    repo, _ = make_skill_repo(
        skills_root,
        "compiled",
        _build_skill_files("tool", revision="one"),
        tag="v1",
    )
    _write_global_skillfile(
        csk_home,
        [{"name": "compiled", "tag": "v1"}],
    )
    cfg = replace(
        make_config(csk_home, skills_root, project),
        adapter_mode="copy",
    )
    user_bin = tmp_path / "user-bin"
    monkeypatch.setenv("CSK_GLOBAL_USER_BIN", str(user_bin))
    first_events: list[str] = []
    _install_fake_build_pipeline(monkeypatch, events=first_events)
    first = global_install.install(cfg)
    assert not first.errors, first.errors

    write_files(repo, _build_skill_files("tool", revision="two"))
    commit_all(repo, "compiled v2")
    subprocess.run(["git", "tag", "v2"], cwd=repo, check=True)
    _write_global_skillfile(
        csk_home,
        [{"name": "compiled", "tag": "v2"}],
    )
    watched = (
        csk_home / "global",
        csk_home / "runtime",
        csk_home / "builds",
        Path.home() / ".claude" / "skills",
        user_bin,
    )
    before = _tree_state(watched)
    events: list[str] = []
    _install_fake_build_pipeline(
        monkeypatch,
        events=events,
        fail_command="tool" if failure == "build" else None,
    )
    if failure == "publication":
        backend = global_install.build_cache.cache_for_manager_home(csk_home)

        class PublishFailureCache:
            manager_home = backend.manager_home

            def inspect(self, expectation):
                return backend.inspect(expectation)

            def publish(self, publication, *, guard):
                raise RuntimeError("forced publication failure")

            def quarantine(self, cache_key, *, guard):
                return backend.quarantine(cache_key, guard=guard)

        monkeypatch.setattr(
            global_install.build_cache,
            "cache_for_manager_home",
            lambda _home: PublishFailureCache(),
        )

    result = global_install.install(cfg)

    assert result.status == "failed"
    expected = (
        "go-v1 fixture_build_failure: forced failure for tool"
        if failure == "build"
        else "forced publication failure"
    )
    assert result.errors == [expected]
    assert events == ["build:tool:home-unlocked"]
    assert _tree_state(watched) == before


@POSIX_BUILD_VECTOR
def test_global_boundary_reports_the_operator_remedy_a_failure_carries(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    skills_root: Path,
    csk_home: Path,
) -> None:
    """The global boundary renders a failure's remedy like the project one."""

    project = make_project(tmp_path)
    make_skill_repo(
        skills_root,
        "compiled",
        _build_skill_files("tool", revision="one"),
        tag="v1",
    )
    _write_global_skillfile(csk_home, [{"name": "compiled", "tag": "v1"}])
    cfg = replace(
        make_config(csk_home, skills_root, project),
        adapter_mode="copy",
    )
    monkeypatch.setenv("CSK_GLOBAL_USER_BIN", str(tmp_path / "user-bin"))
    events: list[str] = []
    _install_fake_build_pipeline(
        monkeypatch,
        events=events,
        fail_command="tool",
        fail_error=_exhausted_fingerprint_deadline(),
    )

    result = global_install.install(cfg)

    assert result.status == "failed"
    reported = result.errors[0]
    assert reported.splitlines()[0] == (
        "go-v1 toolchain_timeout: toolchain fingerprint deadline exceeded"
    )
    assert build_toolchain.FINGERPRINT_TIMEOUT_ENV in reported


@pytest.mark.parametrize(
    "target_class",
    [
        "10-context",
        "20-runtime",
        "30-shim-canonical",
        "40-user-bin",
        "50-env-file",
        "60-adapter-ledger",
        "80-removal",
    ],
)
def test_global_target_failure_reverse_rolls_back_every_surface(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    skills_root: Path,
    csk_home: Path,
    target_class: str,
) -> None:
    project = make_project(tmp_path)
    repo, _ = make_skill_repo(
        skills_root,
        "scripted",
        _script_skill_files("tool", "obsolete", revision="one"),
        tag="v1",
    )
    _write_global_skillfile(
        csk_home,
        [{"name": "scripted", "tag": "v1"}],
    )
    cfg = replace(
        make_config(csk_home, skills_root, project),
        adapter_mode="copy",
    )
    user_bin = tmp_path / "user-bin"
    monkeypatch.setenv("CSK_GLOBAL_USER_BIN", str(user_bin))
    first = global_install.install(cfg)
    assert not first.errors, first.errors

    write_files(
        repo,
        _script_skill_files("tool", "extra", revision="two"),
    )
    commit_all(repo, "scripted v2")
    subprocess.run(["git", "tag", "v2"], cwd=repo, check=True)
    _write_global_skillfile(
        csk_home,
        [{"name": "scripted", "tag": "v2"}],
    )
    env_sh = csk_home / "global" / "env.sh"
    env_sh.write_text(
        env_sh.read_text(encoding="utf-8") + "# still valid prior env\n",
        encoding="utf-8",
    )
    watched = (
        csk_home / "global",
        csk_home / "runtime",
        Path.home() / ".claude" / "skills",
        user_bin,
    )
    before = _tree_state(watched)
    committed: list[str] = []

    def fail_target(
        point: str,
        target: transactions.JournalTarget | None,
    ) -> None:
        if point != "target_committed" or target is None:
            return
        committed.append(target.target_class)
        if target.target_class == target_class:
            raise RuntimeError(f"forced {target_class} failure")

    monkeypatch.setattr(
        global_install,
        "_transaction_engine",
        lambda home: transactions.TransactionEngine(
            home,
            fault_hook=fail_target,
        ),
    )

    result = global_install.install(cfg)

    assert result.status == "failed"
    assert result.errors == [f"forced {target_class} failure"]
    assert target_class in committed
    assert _tree_state(watched) == before


def test_global_dry_run_never_constructs_a_mutation_lock_or_changes_home(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    skills_root: Path,
    csk_home: Path,
) -> None:
    project = make_project(tmp_path)
    make_skill_repo(
        skills_root,
        "scripted",
        _script_skill_files("tool", revision="one"),
        tag="v1",
    )
    _write_global_skillfile(
        csk_home,
        [{"name": "scripted", "tag": "v1"}],
    )
    cfg = make_config(csk_home, skills_root, project)
    watched = (csk_home, Path.home())
    before = _tree_state(watched)

    class ForbiddenLock:
        def __init__(self, *args, **kwargs):
            raise AssertionError("global dry-run constructed a mutation lock")

    monkeypatch.setattr(global_install.locking, "ProjectLock", ForbiddenLock)
    monkeypatch.setattr(
        global_install.locking,
        "ManagerHomeLock",
        ForbiddenLock,
    )
    monkeypatch.setattr(global_install.locking, "BuildLock", ForbiddenLock)

    result = global_install.install(
        cfg,
        options=installer.InstallOptions(dry_run=True),
    )

    assert not result.errors, result.errors
    assert result.messages[-1] == "global: dry-run; no files modified"
    assert _tree_state(watched) == before


def test_windows_user_bin_transaction_plan_stages_forwarder_and_ledger(
    tmp_path: Path,
) -> None:
    csk_home = tmp_path / "manager home"
    user_bin = tmp_path / "user bin"
    targets, messages = global_bins.plan_user_bin_targets(
        csk_home,
        {"tool"},
        platform_name="windows",
        env={
            "CSK_GLOBAL_USER_BIN": str(user_bin),
            "PATH": "",
        },
        home=tmp_path,
    )

    desired = global_bins.stage_user_bin_targets(
        tmp_path / "stage",
        targets,
        csk_home=csk_home,
        platform_name="windows",
    )

    command_target = next(
        target for target in targets if target.desired_kind == "forwarder"
    )
    ledger_target = next(
        target for target in targets if target.desired_kind == "ledger"
    )
    wrapper = desired[
        (command_target.target_class, command_target.identifier)
    ]
    ledger = desired[
        (ledger_target.target_class, ledger_target.identifier)
    ]
    assert wrapper is not None and wrapper.name == "tool.cmd"
    assert (
        f'"{csk_home / "global" / "bin" / "tool.cmd"}" %*'
        in wrapper.read_text(encoding="utf-8")
    )
    assert ledger is not None
    assert json.loads(ledger.read_text(encoding="utf-8"))["entries"] == [
        "tool"
    ]
    assert messages == [f"global: command shims published to {user_bin}"]
    assert not user_bin.exists()


def test_global_adapter_planning_deduplicates_shared_native_root(
    tmp_path: Path,
) -> None:
    csk_home = tmp_path / "manager"
    home = tmp_path / "home"

    targets = adapters.plan_global_adapter_targets(
        csk_home,
        ["windsurf", "opencode"],
        ("skill-a",),
        home=home,
    )

    assert len(targets) == 2
    assert {target.live_path for target in targets} == {
        home / ".agents" / "skills" / "skill-a",
        home / ".agents" / "skills" / ".csk-managed.json",
    }
