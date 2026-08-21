from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import sys
from dataclasses import replace
from pathlib import Path
from typing import Callable

import pytest

from conftest import (
    commit_all,
    init_git_repo,
    make_config,
    make_project,
    write_skillfile,
)
from csk import config, consumers, global_install, hybrid, installer, transactions
from csk.builds import go_v1


FIXTURE = Path(__file__).parent / "fixtures" / "skill_go_e2e"
NATIVE = pytest.mark.skipif(
    sys.platform not in {"darwin", "win32"},
    reason="real go-v1 native controls are accepted only on macOS and Windows",
)
UBUNTU = pytest.mark.skipif(
    not sys.platform.startswith("linux"),
    reason="portable/fail-closed lane is Ubuntu-only",
)


class SimulatedCrash(BaseException):
    pass


@pytest.fixture(autouse=True)
def _authenticated_host(
    required_go_e2e_host: tuple[Path, Path],
    authenticated_e2e_candidate_root: Path,
) -> None:
    assert required_go_e2e_host[0].is_file()
    assert authenticated_e2e_candidate_root.name == "v1"


def _copy_fixture_repo(skills_root: Path, *, portable_only: bool = False) -> Path:
    repo = skills_root / "go-e2e"
    shutil.copytree(FIXTURE, repo)
    if os.name != "nt":
        (repo / "scripts" / "echo-script").chmod(0o755)
    if portable_only:
        payload = json.loads((repo / "agent-skill.json").read_text(encoding="utf-8"))
        payload.pop("build_roots")
        payload["commands"] = {"echo-script": payload["commands"]["echo-script"]}
        (repo / "agent-skill.json").write_text(
            json.dumps(payload, indent=2) + "\n",
            encoding="utf-8",
        )
        shutil.rmtree(repo / "build")
    init_git_repo(repo)
    commit_all(repo, "go e2e fixture")
    return repo


def _write_global_manifest(csk_home: Path) -> None:
    root = csk_home / "global"
    root.mkdir(parents=True, exist_ok=True)
    (root / "Skillfile.json").write_text(
        json.dumps(
            {
                "schema_version": 1,
                "agents": ["codex_cli"],
                "skills": [{"name": "go-e2e", "branch": "main"}],
            },
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )


def _write_hybrid_manifest(csk_home: Path) -> None:
    path = hybrid.hybrid_manifest_path(csk_home)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "skills": [
                    {
                        "name": "go-e2e",
                        "branch": "main",
                        "targets": ["app"],
                    }
                ],
            },
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )


def _setup_scope(
    tmp_path: Path,
    skills_root: Path,
    csk_home: Path,
    scope: str,
    *,
    portable_only: bool = False,
) -> tuple[object, Path, Path]:
    repo = _copy_fixture_repo(skills_root, portable_only=portable_only)
    project = make_project(tmp_path)
    cfg = replace(
        make_config(csk_home, skills_root, project, agents=["codex_cli"]),
        adapter_mode="copy",
    )
    if scope == "global":
        _write_global_manifest(csk_home)
    elif scope == "hybrid":
        write_skillfile(project, {"schema_version": 1, "skills": []})
        _write_hybrid_manifest(csk_home)
    else:
        write_skillfile(
            project,
            {
                "schema_version": 1,
                "agents": ["codex_cli"],
                "skills": [{"name": "go-e2e", "branch": "main"}],
            },
        )
    return cfg, project, repo


def _install(cfg: object, scope: str, *, dry_run: bool = False):
    options = installer.InstallOptions(dry_run=dry_run)
    if scope == "global":
        result = global_install.install(cfg, options=options)  # type: ignore[arg-type]
    else:
        result = installer.install(cfg, alias="app", options=options)[0]  # type: ignore[arg-type]
    return result


def _assert_ok(result: object) -> None:
    assert not result.errors, result.errors  # type: ignore[attr-defined]


def _marker(csk_home: Path, project: Path, scope: str) -> Path:
    if scope == "global":
        root = csk_home / "global" / "skills"
    elif scope == "hybrid":
        root = hybrid.hybrid_skills_root(csk_home)
    else:
        root = project / ".agents" / "skills"
    return root / "go-e2e" / ".csk-install.json"


def _shim(csk_home: Path, project: Path, scope: str, command: str) -> Path:
    name = f"{command}.cmd" if os.name == "nt" else command
    root = csk_home / "global" / "bin" if scope == "global" else project / ".agents" / "bin"
    return root / name


def _run_shim(path: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [path, *args],
        check=False,
        text=True,
        encoding="utf-8",
        capture_output=True,
    )


def _marker_build(csk_home: Path, project: Path, scope: str, command: str) -> dict[str, object]:
    payload = json.loads(_marker(csk_home, project, scope).read_text(encoding="utf-8"))
    return payload["builds"][command]


def _artifact(csk_home: Path, build: dict[str, object]) -> Path:
    key = str(build["cache_key"]).removeprefix("sha256:")
    return csk_home / "builds" / "go-v1" / key / str(build["artifact_path"])


def _receipt(csk_home: Path, build: dict[str, object]) -> dict[str, object]:
    key = str(build["cache_key"]).removeprefix("sha256:")
    path = csk_home / "builds" / "go-v1" / key / "csk-receipt.ccj.json"
    return json.loads(path.read_text(encoding="utf-8"))


def _marker_payload(csk_home: Path, project: Path, scope: str) -> dict[str, object]:
    return json.loads(_marker(csk_home, project, scope).read_text(encoding="utf-8"))


def _verified_build_identity(
    csk_home: Path,
    build: dict[str, object],
) -> tuple[str, str, str]:
    key = str(build["cache_key"])
    entry = csk_home / "builds" / "go-v1" / key.removeprefix("sha256:")
    receipt_path = entry / "csk-receipt.ccj.json"
    receipt_bytes = receipt_path.read_bytes()
    receipt = json.loads(receipt_bytes)
    artifact = entry / str(build["artifact_path"])
    artifact_bytes = artifact.read_bytes()
    receipt_sha256 = "sha256:" + hashlib.sha256(receipt_bytes).hexdigest()
    artifact_sha256 = "sha256:" + hashlib.sha256(artifact_bytes).hexdigest()
    assert receipt["cache_key"] == key
    assert receipt["artifact"]["path"] == build["artifact_path"]
    assert receipt["artifact"]["size"] == len(artifact_bytes)
    assert receipt["artifact"]["sha256"] == artifact_sha256
    assert build["receipt_sha256"] == receipt_sha256
    assert build["artifact_sha256"] == artifact_sha256
    return key, receipt_sha256, artifact_sha256


def _cache_keys(csk_home: Path) -> set[str]:
    root = csk_home / "builds" / "go-v1"
    return {
        "sha256:" + child.name
        for child in root.iterdir()
        if child.is_dir()
    }


def _assert_no_cache_staging(csk_home: Path) -> None:
    staging = csk_home / ".builds-staging"
    assert not staging.exists() or not any(staging.iterdir())


def _shim_bytes(path: Path) -> bytes:
    if path.is_symlink():
        return os.readlink(path).encode("utf-8")
    return path.read_bytes()


def _observe_real_builds(
    monkeypatch: pytest.MonkeyPatch,
) -> tuple[list[str], Callable[..., go_v1.BuildResult]]:
    calls: list[str] = []
    real = go_v1.build

    def observed(*args, **kwargs):
        request = args[0]
        calls.append(request.command)
        return real(*args, **kwargs)

    monkeypatch.setattr(go_v1, "build", observed)
    return calls, real


@pytest.mark.csk_e2e_native
@NATIVE
@pytest.mark.parametrize("scope", ["project", "global", "hybrid"])
def test_go_install_builds_without_network_and_does_not_launch(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    skills_root: Path,
    csk_home: Path,
    scope: str,
) -> None:
    sentinel = tmp_path / "launched"
    monkeypatch.setenv("CSK_GO_E2E_LAUNCH_SENTINEL", str(sentinel))
    cfg, project, _ = _setup_scope(tmp_path, skills_root, csk_home, scope)
    result = _install(cfg, scope)
    _assert_ok(result)
    assert not sentinel.exists()
    build = _marker_build(csk_home, project, scope, "argv-exit")
    receipt = _receipt(csk_home, build)
    assert receipt["input"]["policy"]["execution_policy"] == "manager-worker-v1"  # type: ignore[index]
    assert _artifact(csk_home, build).is_file()


@pytest.mark.csk_e2e_native
@NATIVE
@pytest.mark.parametrize("scope", ["project", "global", "hybrid"])
def test_explicit_go_shim_forwards_argv_stdout_stderr_and_exit(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    skills_root: Path,
    csk_home: Path,
    scope: str,
) -> None:
    sentinel = tmp_path / "launched"
    monkeypatch.setenv("CSK_GO_E2E_LAUNCH_SENTINEL", str(sentinel))
    cfg, project, _ = _setup_scope(tmp_path, skills_root, csk_home, scope)
    _assert_ok(_install(cfg, scope))
    assert not sentinel.exists()
    executed = _run_shim(
        _shim(csk_home, project, scope, "argv-exit"),
        "--exit",
        "23",
        "space value",
        "snowman-☃",
    )
    assert executed.returncode == 23
    assert executed.stdout == 'argv:["space value","snowman-☃"]\n'
    assert executed.stderr == "stderr:2\n"
    assert sentinel.read_text(encoding="utf-8") == "launched\n"


@pytest.mark.csk_e2e_native
@NATIVE
def test_real_go_cache_hit_and_relevant_source_mutation(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, skills_root: Path, csk_home: Path
) -> None:
    calls, _ = _observe_real_builds(monkeypatch)
    cfg, project, repo = _setup_scope(tmp_path, skills_root, csk_home, "project")
    _assert_ok(_install(cfg, "project"))
    first = _marker_build(csk_home, project, "project", "argv-exit")
    _assert_ok(_install(cfg, "project"))
    assert calls.count("argv-exit") == 1
    source = repo / "build" / "cmd" / "argv-exit" / "main.go"
    source.write_text(source.read_text(encoding="utf-8") + "\n// relevant mutation\n", encoding="utf-8")
    commit_all(repo, "relevant mutation")
    _assert_ok(_install(cfg, "project"))
    second = _marker_build(csk_home, project, "project", "argv-exit")
    assert first["cache_key"] != second["cache_key"]
    assert calls.count("argv-exit") == 2


@pytest.mark.csk_e2e_native
@NATIVE
def test_empty_source_revision_preserves_real_go_cache_identity(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, skills_root: Path, csk_home: Path
) -> None:
    calls, _ = _observe_real_builds(monkeypatch)
    cfg, project, repo = _setup_scope(tmp_path, skills_root, csk_home, "project")
    _assert_ok(_install(cfg, "project"))
    first_marker = _marker_payload(csk_home, project, "project")
    first_build = first_marker["builds"]["argv-exit"]  # type: ignore[index]
    first_identity = _verified_build_identity(csk_home, first_build)
    subprocess.run(
        ["git", "commit", "--allow-empty", "-m", "irrelevant empty revision"],
        cwd=repo,
        check=True,
        text=True,
        capture_output=True,
    )
    _assert_ok(_install(cfg, "project"))
    second_marker = _marker_payload(csk_home, project, "project")
    second_build = second_marker["builds"]["argv-exit"]  # type: ignore[index]
    assert first_marker["commit"] != second_marker["commit"]
    assert first_marker["build_source"] == second_marker["build_source"]
    assert first_identity == _verified_build_identity(csk_home, second_build)
    assert calls.count("argv-exit") == 1


@pytest.mark.csk_e2e_native
@NATIVE
def test_real_go_dry_run_cache_miss_is_byte_pure(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, skills_root: Path, csk_home: Path
) -> None:
    calls, _ = _observe_real_builds(monkeypatch)
    cfg, project, _ = _setup_scope(tmp_path, skills_root, csk_home, "project")
    before = sorted((path.relative_to(csk_home), path.read_bytes()) for path in csk_home.rglob("*") if path.is_file())
    result = _install(cfg, "project", dry_run=True)
    _assert_ok(result)
    after = sorted((path.relative_to(csk_home), path.read_bytes()) for path in csk_home.rglob("*") if path.is_file())
    assert before == after
    assert calls == []
    assert not (project / ".agents").exists()


@pytest.mark.csk_e2e_native
@NATIVE
def test_two_real_go_commands_keep_distinct_artifacts(
    tmp_path: Path, skills_root: Path, csk_home: Path
) -> None:
    cfg, project, _ = _setup_scope(tmp_path, skills_root, csk_home, "project")
    _assert_ok(_install(cfg, "project"))
    first = _marker_build(csk_home, project, "project", "argv-exit")
    second = _marker_build(csk_home, project, "project", "second-tool")
    assert first["cache_key"] != second["cache_key"]
    assert _artifact(csk_home, first) != _artifact(csk_home, second)
    assert _run_shim(_shim(csk_home, project, "project", "second-tool"), "a", "b").stdout == "second:2\n"


@pytest.mark.csk_e2e_native
@NATIVE
def test_real_go_target_swap_failure_rolls_back_live_state(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, skills_root: Path, csk_home: Path
) -> None:
    cfg, project, repo = _setup_scope(tmp_path, skills_root, csk_home, "project")
    _assert_ok(_install(cfg, "project"))
    marker = _marker(csk_home, project, "project").read_bytes()
    source = repo / "build" / "cmd" / "argv-exit" / "main.go"
    source.write_text(source.read_text(encoding="utf-8") + "\n// replacement\n", encoding="utf-8")
    commit_all(repo, "replacement")

    def engine(home: Path) -> transactions.TransactionEngine:
        def fail(point: str, target: transactions.JournalTarget | None) -> None:
            if point == "target_committed" and target is not None:
                raise RuntimeError("injected target swap failure")
        return transactions.TransactionEngine(home, fault_hook=fail)

    monkeypatch.setattr(installer, "_transaction_engine", engine)
    result = _install(cfg, "project")
    assert result.errors == ["injected target swap failure"]
    assert _marker(csk_home, project, "project").read_bytes() == marker
    assert _run_shim(_shim(csk_home, project, "project", "argv-exit"), "old").stdout == 'argv:["old"]\n'


@pytest.mark.csk_e2e_native
@NATIVE
def test_interrupted_real_go_install_recovers_on_next_public_command(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, skills_root: Path, csk_home: Path
) -> None:
    cfg, project, _ = _setup_scope(tmp_path, skills_root, csk_home, "project")
    crashed = False

    def engine(home: Path) -> transactions.TransactionEngine:
        def interrupt(point: str, target: transactions.JournalTarget | None) -> None:
            nonlocal crashed
            if not crashed and point == "target_committed" and target is not None:
                crashed = True
                raise SimulatedCrash()
        return transactions.TransactionEngine(home, fault_hook=interrupt)

    monkeypatch.setattr(installer, "_transaction_engine", engine)
    with pytest.raises(SimulatedCrash):
        _install(cfg, "project")
    assert list((csk_home / "state" / "transactions" / "v1").glob("*.json"))
    monkeypatch.setattr(installer, "_transaction_engine", transactions.TransactionEngine)
    _assert_ok(_install(cfg, "project"))
    assert not list((csk_home / "state" / "transactions" / "v1").glob("*.json"))
    assert _run_shim(_shim(csk_home, project, "project", "argv-exit"), "recovered").returncode == 0


@pytest.mark.csk_e2e_native
@NATIVE
def test_concurrent_real_go_publishers_converge_on_one_identity(
    tmp_path: Path,
    skills_root: Path,
    csk_home: Path,
    required_go_e2e_host: tuple[Path, Path],
) -> None:
    cfg, project, _ = _setup_scope(tmp_path, skills_root, csk_home, "project")
    config.save_config(cfg)  # type: ignore[arg-type]
    environment = dict(os.environ)
    environment["CSK_CONFIG"] = str(csk_home / "config.json")
    # A native Windows build can outlive the default 30-second contention
    # budget. Keep the persistent lock invariant and let the losing public
    # installer wait long enough to consume the winner's verified cache entry.
    environment["CSK_LOCK_TIMEOUT"] = "300"
    manager = os.fspath(required_go_e2e_host[0])
    environment["CSK_GO_V1_MANAGER_EXECUTABLE"] = manager
    processes = [
        subprocess.Popen(
            [manager, "install", "app"],
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=environment,
        )
        for _ in range(2)
    ]
    results = [process.communicate(timeout=360) for process in processes]
    assert [process.returncode for process in processes] == [0, 0], results
    marker = _marker_payload(csk_home, project, "project")
    builds = marker["builds"]
    identities = {
        command: _verified_build_identity(csk_home, build)
        for command, build in builds.items()  # type: ignore[union-attr]
    }
    assert set(identities) == {"argv-exit", "second-tool"}
    assert _cache_keys(csk_home) == {
        identity[0] for identity in identities.values()
    }
    _assert_no_cache_staging(csk_home)
    for command, identity in identities.items():
        assert identity[0].removeprefix("sha256:").encode() in _shim_bytes(
            _shim(csk_home, project, "project", command)
        )
    assert _run_shim(
        _shim(csk_home, project, "project", "argv-exit"), "concurrent"
    ).returncode == 0


@pytest.mark.csk_e2e_native
@NATIVE
def test_two_projects_preserve_both_real_go_consumers(
    tmp_path: Path, skills_root: Path, csk_home: Path
) -> None:
    repo = _copy_fixture_repo(skills_root)
    one = make_project(tmp_path, "one")
    two = make_project(tmp_path, "two")
    for project in (one, two):
        write_skillfile(project, {"schema_version": 1, "skills": [{"name": "go-e2e", "branch": "main"}]})
    base = make_config(csk_home, skills_root, one, agents=["codex_cli"])
    cfg = replace(
        base,
        projects={
            "one": replace(base.projects["app"], alias="one", path=one),
            "two": replace(base.projects["app"], alias="two", path=two),
        },
    )
    _assert_ok(installer.install(cfg, alias="one")[0])
    _assert_ok(installer.install(cfg, alias="two")[0])
    one_marker_before = _marker_payload(csk_home, one, "project")
    two_marker_before = _marker_payload(csk_home, two, "project")
    assert one_marker_before["builds"] == two_marker_before["builds"]
    old_identities = {
        command: _verified_build_identity(csk_home, build)
        for command, build in one_marker_before["builds"].items()  # type: ignore[union-attr]
    }
    one_shims_before = {
        command: _shim_bytes(_shim(csk_home, one, "project", command))
        for command in old_identities
    }
    source = repo / "build" / "cmd" / "argv-exit" / "main.go"
    source.write_text(source.read_text(encoding="utf-8") + "\n// project two update\n", encoding="utf-8")
    commit_all(repo, "project two update")
    _assert_ok(installer.install(cfg, alias="two")[0])
    one_marker_after = _marker_payload(csk_home, one, "project")
    two_marker_after = _marker_payload(csk_home, two, "project")
    assert one_marker_after == one_marker_before
    assert two_marker_after["commit"] != two_marker_before["commit"]
    new_identities = {
        command: _verified_build_identity(csk_home, build)
        for command, build in two_marker_after["builds"].items()  # type: ignore[union-attr]
    }
    assert set(old_identities) == set(new_identities) == {"argv-exit", "second-tool"}
    assert all(old_identities[name] != new_identities[name] for name in old_identities)
    assert _cache_keys(csk_home) == {
        identity[0]
        for identities in (old_identities, new_identities)
        for identity in identities.values()
    }
    _assert_no_cache_staging(csk_home)
    for command in old_identities:
        old_key = old_identities[command][0].removeprefix("sha256:").encode()
        new_key = new_identities[command][0].removeprefix("sha256:").encode()
        one_shim = _shim_bytes(_shim(csk_home, one, "project", command))
        two_shim = _shim_bytes(_shim(csk_home, two, "project", command))
        assert one_shim == one_shims_before[command]
        assert old_key in one_shim
        assert new_key in two_shim
        assert old_key not in two_shim
    assert consumers.load_consumers(csk_home) == [one.resolve(), two.resolve()]
    assert _run_shim(_shim(csk_home, one, "project", "argv-exit"), "one").returncode == 0
    assert _run_shim(_shim(csk_home, two, "project", "argv-exit"), "two").returncode == 0


@pytest.mark.csk_e2e_native
@NATIVE
def test_real_go_repair_restores_current_explicit_launch(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, skills_root: Path, csk_home: Path
) -> None:
    calls, _ = _observe_real_builds(monkeypatch)
    cfg, project, _ = _setup_scope(tmp_path, skills_root, csk_home, "project")
    _assert_ok(_install(cfg, "project"))
    build = _marker_build(csk_home, project, "project", "argv-exit")
    artifact = _artifact(csk_home, build)
    artifact.chmod(0o700)
    artifact.write_bytes(b"corrupt")
    _assert_ok(_install(cfg, "project"))
    assert calls.count("argv-exit") == 2
    assert _run_shim(_shim(csk_home, project, "project", "argv-exit"), "repaired").stdout == 'argv:["repaired"]\n'


@pytest.mark.csk_e2e_native
@NATIVE
def test_mixed_script_and_real_go_commands_share_native_namespace(
    tmp_path: Path, skills_root: Path, csk_home: Path
) -> None:
    cfg, project, _ = _setup_scope(tmp_path, skills_root, csk_home, "project")
    _assert_ok(_install(cfg, "project"))
    assert _run_shim(_shim(csk_home, project, "project", "argv-exit"), "build").stdout == 'argv:["build"]\n'
    assert _run_shim(_shim(csk_home, project, "project", "echo-script"), "script", "args").stdout.strip() == "script:script args"


@pytest.mark.csk_e2e_ubuntu
@UBUNTU
@pytest.mark.parametrize("scope", ["project", "global"])
def test_portable_script_install_and_launch(
    tmp_path: Path, skills_root: Path, csk_home: Path, scope: str
) -> None:
    cfg, project, _ = _setup_scope(
        tmp_path, skills_root, csk_home, scope, portable_only=True
    )
    _assert_ok(_install(cfg, scope))
    executed = _run_shim(_shim(csk_home, project, scope, "echo-script"), "portable")
    assert executed.returncode == 0
    assert executed.stdout.strip() == "script:portable"


@pytest.mark.csk_e2e_ubuntu
@UBUNTU
@pytest.mark.parametrize("scope", ["project", "global"])
def test_go_install_fails_closed_without_native_control(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    skills_root: Path,
    csk_home: Path,
    scope: str,
) -> None:
    cfg, project, _ = _setup_scope(tmp_path, skills_root, csk_home, scope)

    def worker_must_not_launch(*args, **kwargs):
        raise AssertionError("Ubuntu fail-closed path launched the go-v1 worker")

    monkeypatch.setattr(go_v1._NativeControlDomain, "launch", worker_must_not_launch)
    result = _install(cfg, scope)
    assert result.status == "failed"
    assert any(
        "build_execution_control_unavailable" in error for error in result.errors
    ), result.errors
    assert not _marker(csk_home, project, scope).exists()
    assert not _shim(csk_home, project, scope, "argv-exit").exists()
    assert not (csk_home / "builds" / "go-v1").exists()


@NATIVE
def test_manager_identity_resolves_through_operator_symlink(
    required_go_e2e_host: tuple[Path, Path],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A Homebrew/pipx-style symlink launcher must resolve to the real manager.

    The harness fixture pre-resolves argv0, which is exactly why the original
    symlink rejection was never caught end to end; this test hands the
    unresolved symlink to the argv0 recovery path instead.
    """

    manager, _ = required_go_e2e_host
    shim_dir = tmp_path / "local-bin"
    shim_dir.mkdir()
    shim = shim_dir / manager.name
    shim.symlink_to(manager)

    launcher = go_v1._manager_executable_from_argv0(str(shim))
    assert launcher == manager

    identity = go_v1._resolve_manager_identity(launcher)
    assert identity.launcher.path == manager
