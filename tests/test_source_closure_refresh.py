"""Schema-2 source closure and explicit refresh (TASK-260916-18j5hg).

End-to-end coverage for the two modes (resolving vs frozen), the
schema-2 full closure over Git and local members, all-or-nothing lock
creation across the five failure points, the distinct stale and
unavailable refusals, atomic refresh, and the runtime store key. Every
install flows through ``installer.install`` and every status through
``status.collect_status``; Git tests serve local fixture repositories
through fakes installed at the transport boundary, never the network.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import sys
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest

from csk import closure, config, git_admission, installer, manifest, status
from csk.config import GlobalConfig
from csk.sources import lock as lock_module
from csk.sources import modes, publish, selection
from csk.sources import transport as source_transport
from csk.sources.errors import (
    CODE_LOCK_STALE,
    CODE_MEMBER_INVALID,
    CODE_MEMBER_MISSING,
    CODE_NAME_CONFLICT,
    CODE_SELECTION_INVALID,
    CODE_SNAPSHOT_CHANGED,
    CODE_SNAPSHOT_UNAVAILABLE,
    SourceError,
)
from csk.sources.package_identity import (
    LocalSnapshot,
    NetworkGit,
    package_identity_sha256,
)
from tests.conftest import (
    csk_home,  # noqa: F401 - shared fixtures
    commit_all,
    init_git_repo,
    make_config,
    make_project,
    run,
    skills_root,  # noqa: F401 - shared fixtures
    stable_env,  # noqa: F401 - autouse via import
    write_files,
    write_skillfile,
)


def _require_selection() -> None:
    from csk.sources import _selection_fs

    if not _selection_fs.supports_descriptor_traversal():
        pytest.skip(_selection_fs.NO_DESCRIPTOR_TRAVERSAL_REASON)


def _v2_config(
    csk_home: Path, skills_root: Path, project: Path
) -> config.GlobalConfig:
    base = make_config(csk_home, skills_root, project, agents=["codex_cli"])
    return replace(
        base,
        experimental=config.ExperimentalConfig(skillfile_sources=True),
    )


def _install_ok(cfg: GlobalConfig, **options: Any) -> installer.ProjectResult:
    _require_selection()
    results = installer.install(cfg, alias="app", options=installer.InstallOptions(**options))
    assert len(results) == 1
    result = results[0]
    assert result.status == "ok", result.errors
    return result


def _install_failed(cfg: GlobalConfig, **options: Any) -> list[str]:
    _require_selection()
    results = installer.install(cfg, alias="app", options=installer.InstallOptions(**options))
    assert len(results) == 1
    result = results[0]
    assert result.status == "failed", result.messages
    assert result.errors
    return list(result.errors)


def _assert_head_code(errors: list[str], code: str) -> None:
    """Assert the refusal HEAD code; a chained cause must not satisfy it.

    Refusal details chain the causing error, which embeds its own
    code — a substring assertion would pass while answering the
    wrong class. The ``"<code>: ..."`` prefix is the answer.
    """
    assert errors, "expected a refusal diagnostic"
    assert errors[0].startswith(code + ":"), errors[0]


def _write_skill(
    directory: Path,
    name: str,
    *,
    requirements: dict[str, Any] | None = None,
    commands: dict[str, Any] | None = None,
    runtime_roots: tuple[str, ...] = (),
    build_roots: tuple[str, ...] = (),
    extra_files: dict[str, str | bytes] | None = None,
) -> None:
    """Write one schema-7 skill package directory."""
    manifest_payload: dict[str, Any] = {"schema_version": 7, "capabilities": {}}
    if requirements:
        manifest_payload["dependencies"] = {"skills": requirements}
    if commands:
        manifest_payload["commands"] = commands
    if runtime_roots:
        manifest_payload["runtime_roots"] = list(runtime_roots)
    if build_roots:
        manifest_payload["build_roots"] = list(build_roots)
    files: dict[str, str | bytes] = {
        "SKILL.md": f"---\nname: {name}\ndescription: fixture {name}\n---\n\n# {name}\n",
        "agent-skill.json": json.dumps(manifest_payload),
    }
    if extra_files:
        files.update(extra_files)
    write_files(directory, files)


def _write_skillfile_v2(
    project: Path,
    sources: dict[str, dict[str, Any]],
    selectors: list[dict[str, Any]],
) -> None:
    write_skillfile(
        project,
        {"schema_version": 2, "sources": sources, "skills": selectors},
    )


def _read_lock(project: Path) -> lock_module.SkillfileLock:
    return lock_module.read_lock(
        (project / publish.SKILLFILE_LOCK_NAME).read_bytes()
    )


def _tree_hash(root: Path, *, normalize_markers: bool = False) -> str:
    """Hash one installed tree (relative paths, kinds and bytes).

    With ``normalize_markers``, the ``installed_at`` timestamp inside
    install markers is blanked before hashing: a reinstall
    legitimately restamps it, so success-path comparisons normalize
    it while fault tests keep the strict byte comparison (nothing
    may be rewritten at all).
    """
    digest = hashlib.sha256()
    if root.is_symlink() or not root.exists():
        digest.update(b"missing:")
        digest.update(str(root).encode())
        return digest.hexdigest()
    for path in sorted(root.rglob("*")):
        rel = path.relative_to(root).as_posix()
        digest.update(rel.encode())
        if path.is_symlink():
            digest.update(b"link:")
            digest.update(os.readlink(path).encode())
        elif path.is_dir():
            digest.update(b"dir")
        else:
            digest.update(b"file:")
            raw = path.read_bytes()
            if normalize_markers and path.name == ".csk-install.json":
                try:
                    document = json.loads(raw.decode("utf-8"))
                except (UnicodeDecodeError, ValueError):
                    pass
                else:
                    if isinstance(document, dict) and "installed_at" in document:
                        document["installed_at"] = ""
                        raw = json.dumps(document, sort_keys=True).encode()
            digest.update(raw)
    return digest.hexdigest()


def _installed_state_hash(
    project: Path, csk_home: Path, names: list[str], *, normalize_markers: bool = False
) -> str:
    """Hash the lock, project outputs and member runtime entries."""
    digest = hashlib.sha256()
    lock_path = project / publish.SKILLFILE_LOCK_NAME
    digest.update(b"lock:")
    digest.update(lock_path.read_bytes() if lock_path.exists() else b"absent")
    digest.update(b"agents:")
    digest.update(
        _tree_hash(project / ".agents", normalize_markers=normalize_markers).encode()
    )
    for name in sorted(names):
        try:
            member = next(
                item for item in _read_lock(project).members if item.name == name
            )
        except (StopIteration, SourceError):
            digest.update(f"runtime:{name}:no-lock".encode())
            continue
        key = package_identity_sha256(member.package)
        digest.update(f"runtime:{name}:{key}:".encode())
        digest.update(
            _tree_hash(
                publish.runtime_entry_path(csk_home, name, key),
                normalize_markers=normalize_markers,
            ).encode()
        )
    return digest.hexdigest()


def _git_identity() -> list[str]:
    return ["-c", "user.name=Fixture", "-c", "user.email=fixture@example.test"]


def _make_repo(workdir: Path, commits: list[dict[str, str | bytes]]) -> list[str]:
    """Build a fixture repo with one commit per file-set; returns OIDs."""
    init_git_repo(workdir)
    oids: list[str] = []
    for index, files in enumerate(commits):
        write_files(workdir, files)
        run(["git", "add", "--", "."], workdir)
        run(["git", *_git_identity(), "commit", "--quiet", "-m", f"c{index}"], workdir)
        completed = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=workdir,
            capture_output=True,
            text=True,
            check=True,
            timeout=20,
        )
        oids.append(completed.stdout.strip())
    return oids


def _real_tool() -> git_admission.GitTool:
    discovered = shutil.which("git")
    executable = Path(discovered).resolve() if discovered is not None else Path("git")
    version = subprocess.run(
        [os.fspath(executable), "--version"],
        capture_output=True,
        timeout=20,
        text=True,
        check=True,
    ).stdout.strip()
    parts = version.split()[2].split(".")
    exec_path = subprocess.run(
        [os.fspath(executable), "--exec-path"],
        capture_output=True,
        timeout=20,
        text=True,
        check=True,
    ).stdout.strip()
    return git_admission.GitTool(
        executable=executable,
        exec_path=Path(exec_path).resolve(),
        allowed_versions=(f"git version {parts[0]}.{parts[1]}.",),
    )


@pytest.mark.skipif(os.name == "nt", reason="unprivileged Windows symlinks are not portable")
def test_real_tool_resolves_symlinked_git_on_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A symlinked git on PATH (the Homebrew shape) still yields an admittable tool.

    Regression test for the macos-latest lane: ``shutil.which("git")``
    resolves to a symlink there, and the admission layer refuses a
    symlinked executable by contract, so the helper must hand over the
    resolved object rather than the discovered link.
    """

    discovered = shutil.which("git")
    assert discovered is not None
    link_dir = tmp_path / "link-bin"
    link_dir.mkdir()
    link = link_dir / "git"
    link.symlink_to(discovered)
    assert link.is_symlink()
    monkeypatch.setenv(
        "PATH", os.pathsep.join((os.fspath(link_dir), os.environ["PATH"]))
    )
    assert Path(shutil.which("git") or "git").is_symlink()

    tool = _real_tool()

    assert tool.executable == Path(discovered).resolve()
    assert not tool.executable.is_symlink()
    git_admission.validate_git_tool(tool)


def _prepare_admittable_repo(workdir: Path) -> None:
    """Pack objects and strip .git to the admission allowlist.

    Mirrors the operator transfer the admission lane expects: a
    packed object store with only HEAD, config, index, objects,
    refs and packed-refs present.
    """
    subprocess.run(
        (
            "git",
            "-c",
            "repack.updateServerInfo=false",
            "-c",
            "pack.writeReverseIndex=false",
            "repack",
            "-a",
            "-d",
            "--quiet",
        ),
        cwd=workdir,
        check=True,
        capture_output=True,
        timeout=60,
    )
    for child in list((workdir / ".git").iterdir()):
        if child.name in {"HEAD", "config", "index", "objects", "refs", "packed-refs"}:
            continue
        if child.is_dir():
            shutil.rmtree(child)
        else:
            child.unlink()


class _FakeTransport:
    """Serve fixture repositories at the transport boundary.

    Maps a declared URL (or a repository identity) to a fixture
    workdir; ``resolve_ref`` answers from the fixture's refs and
    ``acquire_network`` checks out the pinned commit and admits the
    workdir, so every served snapshot is a transport-verified
    ``Snapshot`` with a real commit. Counts calls for memo tests.
    """

    def __init__(self) -> None:
        self.by_url: dict[str, Path] = {}
        self.by_identity: dict[str, Path] = {}
        self.resolve_calls: list[tuple[str, str, str]] = []
        self.acquire_calls: list[tuple[str, str]] = []
        self.resolve_script: list[str] | None = None
        self.tool = _real_tool()

    def add(self, url: str, workdir: Path, identity: str | None = None) -> None:
        _prepare_admittable_repo(workdir)
        self.by_url[url] = workdir
        if identity is not None:
            self.by_identity[identity] = workdir

    def _workdir(self, identity: str, declared_url: str | None) -> Path:
        if declared_url is not None and declared_url in self.by_url:
            return self.by_url[declared_url]
        if identity in self.by_identity:
            return self.by_identity[identity]
        raise AssertionError(f"no fixture for {identity!r} url={declared_url!r}")

    def resolve_ref(
        self,
        plan_or_identity: Any,
        ref_kind: str,
        ref_value: str,
        tool: Any = None,
        **kwargs: Any,
    ) -> source_transport.ResolutionResult:
        identity = plan_or_identity if isinstance(plan_or_identity, str) else "plan"
        declared_url = kwargs.get("declared_url")
        self.resolve_calls.append((identity, ref_kind, ref_value))
        if self.resolve_script is not None:
            commit = self.resolve_script.pop(0)
        else:
            workdir = self._workdir(identity, declared_url)
            wanted = f"refs/tags/{ref_value}" if ref_kind == "tag" else f"refs/heads/{ref_value}"
            completed = subprocess.run(
                ["git", "rev-parse", "--verify", "--quiet", f"{wanted}^{{commit}}"],
                cwd=workdir,
                capture_output=True,
                text=True,
                timeout=20,
            )
            if completed.returncode != 0 or not completed.stdout.strip():
                raise source_transport.TransportResolutionError(())
            commit = completed.stdout.strip()
        from csk import build_repository as build_repository_model

        return source_transport.ResolutionResult(
            lock=build_repository_model.LockedCommit("sha1", commit), attempts=()
        )

    def acquire_network(
        self,
        identity: Any,
        lock: Any,
        tool: Any = None,
        **kwargs: Any,
    ) -> source_transport.AcquisitionResult:
        if isinstance(identity, str):
            identity_str = identity
        else:
            identity_str = str(identity)
        declared_url = kwargs.get("declared_url")
        self.acquire_calls.append((identity_str, lock.hex))
        workdir = self._workdir(identity_str, declared_url)
        run(
            ["git", "-c", "core.logAllRefUpdates=false", "checkout", "--quiet", lock.hex],
            workdir,
        )
        logs = workdir / ".git" / "logs"
        if logs.exists():
            shutil.rmtree(logs)
        snapshot = git_admission.admit_local(workdir, self.tool)
        assert snapshot.commit == lock.hex
        return source_transport.AcquisitionResult(snapshot=snapshot, attempts=())


def _install_fake_transport(
    monkeypatch: pytest.MonkeyPatch, fake: _FakeTransport
) -> None:
    monkeypatch.setattr(source_transport, "resolve_ref", fake.resolve_ref)
    monkeypatch.setattr(source_transport, "acquire_network", fake.acquire_network)


def _write_local_project(
    tmp_path: Path, members: list[tuple[str, str]], **skill_kwargs: Any
) -> tuple[Path, Path]:
    """Write a local path source plus a selecting project; returns (project, source)."""
    project = make_project(tmp_path)
    source = tmp_path / "pkgs"
    for name, directory in members:
        _write_skill(source / directory, name, **skill_kwargs)
    _write_skillfile_v2(
        project,
        {"local": {"path": os.fspath(source)}},
        [
            {"name": name, "from": "local", "directory": directory}
            for name, directory in members
        ],
    )
    return project, source


def test_frozen_mode_holds_no_resolving_capabilities(tmp_path: Path) -> None:
    """AC(a): frozen mode is a type without the resolving grant.

    The grant is absent twice: the frozen mode value carries no
    resolving field, and neither the publisher entry point nor the
    frozen lane takes the fetch flag, the tool provider, the policy
    path or a workspace as a parameter — so the frozen lane cannot
    name them. Production call sites: ``csk.sources.modes``
    (``select``, ``FrozenSources``, ``ResolvingSources``) and
    ``publish.install_schema2`` / ``publish._install_schema2_frozen``.
    """
    import inspect

    from csk.sources.lock import SkillfileLock

    lock = SkillfileLock(manifest_sha256="sha256:" + "0" * 64, members=())
    frozen = modes.FrozenSources(home=tmp_path, lock=lock)
    assert set(frozen.__dataclass_fields__) == {"home", "lock"}
    for resolving_only in (
        "tool_for_endpoint",
        "policy_path",
        "workspace",
        "alias_commits",
        "acquisitions",
    ):
        assert not hasattr(frozen, resolving_only), resolving_only
    grant = {"fetch", "tool_for_endpoint", "policy_path", "workspace"}
    entry_params = set(inspect.signature(publish.install_schema2).parameters)
    assert "mode" in entry_params
    assert grant & entry_params == set()
    frozen_params = set(
        inspect.signature(publish._install_schema2_frozen).parameters
    )
    assert "mode" in frozen_params
    assert grant & frozen_params == set()
    resolving_params = set(
        inspect.signature(publish._install_schema2_resolving).parameters
    )
    assert grant & resolving_params == set()

    selected = modes.select(
        home=tmp_path,
        old_lock=lock,
        fetch=False,
        workspace=tmp_path,
        tool_for_endpoint=None,
        policy_path=None,
    )
    assert isinstance(selected, modes.FrozenSources)
    assert modes.select(
        home=tmp_path,
        old_lock=None,
        fetch=False,
        workspace=tmp_path,
        tool_for_endpoint=None,
        policy_path=None,
    ).__class__ is modes.ResolvingSources
    assert modes.select(
        home=tmp_path,
        old_lock=lock,
        fetch=True,
        workspace=tmp_path,
        tool_for_endpoint=None,
        policy_path=None,
    ).__class__ is modes.ResolvingSources


def test_initial_resolve_creates_lock_after_gates(
    tmp_path: Path, skills_root: Path, csk_home: Path
) -> None:
    """AC: initial explicit resolve/install creates the lock; gates ran.

    Production call site: ``installer.install`` -> ``publish.install_schema2``.
    """
    project, _source = _write_local_project(tmp_path, [("review", "review")])
    cfg = _v2_config(csk_home, skills_root, project)
    result = _install_ok(cfg)
    assert any("lock created" in message for message in result.messages)

    lock = _read_lock(project)
    assert [member.name for member in lock.members] == ["review"]
    member = lock.members[0]
    assert isinstance(member.package, LocalSnapshot)
    assert member.selection == 0
    marker_raw = (
        project / ".agents" / "skills" / "review" / ".csk-install.json"
    ).read_bytes()
    from csk import install_marker

    marker = install_marker.read_install_marker(marker_raw)
    assert isinstance(marker, install_marker.InstallMarkerV5)
    assert marker.lock_sha256 == lock.lock_sha256


def test_locked_install_ignores_new_member_directory(
    tmp_path: Path, skills_root: Path, csk_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """AC: locked install uses frozen membership; no rescan, no lock write.

    A collection selector admits a new member directory that appears
    on disk after the lock; the locked install must not enumerate
    it, must not touch the network, and must leave the lock
    byte-identical. The collection shape makes the outcome assert
    the frozen membership too: under a rescan ``docs`` would
    install.
    Production call site: ``installer.install`` (fetch=False).
    """
    project = make_project(tmp_path)
    source = tmp_path / "pkgs"
    _write_skill(source / "coll" / "review", "review")
    _write_skillfile_v2(
        project,
        {"local": {"path": os.fspath(source)}},
        [{"from": "local", "directory": "coll", "include": ["*"]}],
    )
    cfg = _v2_config(csk_home, skills_root, project)
    _install_ok(cfg)
    lock_before = (project / publish.SKILLFILE_LOCK_NAME).read_bytes()

    _write_skill(source / "coll" / "docs", "docs")
    reached: list[str] = []

    def _boom(*args: Any, **kwargs: Any) -> Any:
        reached.append("network-or-enumeration")
        raise AssertionError("frozen install must not enumerate or fetch")

    monkeypatch.setattr(source_transport, "resolve_ref", _boom)
    monkeypatch.setattr(source_transport, "acquire_network", _boom)
    monkeypatch.setattr(publish, "expand_selectors", _boom)
    monkeypatch.setattr(publish, "expand_collection", _boom)
    result = _install_ok(cfg)
    assert reached == []
    assert (project / publish.SKILLFILE_LOCK_NAME).read_bytes() == lock_before
    assert not (project / ".agents" / "skills" / "docs").exists()
    assert any("review up-to-date" in message for message in result.messages)


def test_frozen_install_plans_no_lock_target(
    tmp_path: Path, skills_root: Path, csk_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """AC(a): the lock target enters the commit set in resolving mode only.

    The test observes the planned target set at the commit seam: a
    locked install plans no lock target (frozen mode cannot write a
    lock), while an explicit refresh plans one. A frozen lane that
    kept the lock target would die here even though the engine
    would skip the identical write.
    Production call site: ``installer.install`` -> ``publish`` planning.
    """
    project, _source = _write_local_project(tmp_path, [("review", "review")])
    cfg = _v2_config(csk_home, skills_root, project)
    _install_ok(cfg)

    planned_sets: list[tuple[tuple[str, str], ...]] = []
    real_commit = publish._commit_schema2

    def _spy(*args: Any, **kwargs: Any) -> Any:
        specs = kwargs.get("specs", args[4] if len(args) > 4 else ())
        planned_sets.append(
            tuple((spec.target_class, spec.identifier) for spec in specs)
        )
        return real_commit(*args, **kwargs)

    monkeypatch.setattr(publish, "_commit_schema2", _spy)
    _install_ok(cfg)
    assert len(planned_sets) == 1
    assert (publish.CLASS_LOCK, "Skillfile.lock.json") not in planned_sets[0]

    _install_ok(cfg, fetch=True)
    assert len(planned_sets) == 2
    assert (publish.CLASS_LOCK, "Skillfile.lock.json") in planned_sets[1]


def test_locked_install_repairs_outputs_from_frozen_store(
    tmp_path: Path, skills_root: Path, csk_home: Path
) -> None:
    """AC: locked install consumes the locked snapshot, never re-resolves.

    Installed outputs are deleted while live and store stay intact;
    the locked install rebuilds them from the frozen bytes.
    Production call site: ``installer.install`` (fetch=False).
    """
    project, _source = _write_local_project(tmp_path, [("review", "review")])
    cfg = _v2_config(csk_home, skills_root, project)
    _install_ok(cfg)
    before = _installed_state_hash(project, csk_home, ["review"], normalize_markers=True)

    shutil.rmtree(project / ".agents" / "skills" / "review")
    result = _install_ok(cfg)
    assert any("review installed" in message for message in result.messages)
    assert (
        _installed_state_hash(project, csk_home, ["review"], normalize_markers=True)
        == before
    )


@pytest.mark.skipif(
    sys.platform == "win32", reason="posix-permissions: live tree made unreadable"
)
def test_locked_install_serves_store_when_live_unreadable(
    tmp_path: Path, skills_root: Path, csk_home: Path
) -> None:
    """AC: unreadable live bytes fall back to the verified store copy.

    The live tree exists (the recheck passes) but cannot be captured,
    so the install serves the store bytes it verified.
    Production call site: ``installer.install`` (fetch=False).
    """
    project, source = _write_local_project(tmp_path, [("review", "review")])
    cfg = _v2_config(csk_home, skills_root, project)
    _install_ok(cfg)
    before = _installed_state_hash(project, csk_home, ["review"], normalize_markers=True)

    member_dir = source / "review"
    member_dir.chmod(0o000)
    try:
        shutil.rmtree(project / ".agents" / "skills" / "review")
        result = _install_ok(cfg)
    finally:
        member_dir.chmod(0o755)
    assert any("review installed" in message for message in result.messages)
    assert (
        _installed_state_hash(project, csk_home, ["review"], normalize_markers=True)
        == before
    )


def test_deleted_live_source_refuses_changed(
    tmp_path: Path, skills_root: Path, csk_home: Path
) -> None:
    """AC boundary: a deleted live source is drift, not silent adoption.

    Repair revalidates the exact locked source, so a missing live
    directory refuses ``source_snapshot_changed`` instead of
    installing from the store alone.
    Production call site: ``installer.install`` (fetch=False).
    """
    project, source = _write_local_project(tmp_path, [("review", "review")])
    cfg = _v2_config(csk_home, skills_root, project)
    _install_ok(cfg)
    before = _installed_state_hash(project, csk_home, ["review"])

    shutil.rmtree(source / "review")
    errors = _install_failed(cfg)
    _assert_head_code(errors, CODE_SNAPSHOT_CHANGED)
    assert _installed_state_hash(project, csk_home, ["review"]) == before


def test_stale_lock_refuses_without_reresolve(
    tmp_path: Path, skills_root: Path, csk_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """AC: changed manifest_sha256 is source_lock_stale, never a re-resolve.

    Production call site: ``installer.install`` (fetch=False).
    """
    project, source = _write_local_project(tmp_path, [("review", "review")])
    cfg = _v2_config(csk_home, skills_root, project)
    _install_ok(cfg)
    before = _installed_state_hash(project, csk_home, ["review"])

    _write_skill(source / "docs", "docs")
    _write_skillfile_v2(
        project,
        {"local": {"path": os.fspath(source)}},
        [
            {"name": "review", "from": "local", "directory": "review"},
            {"name": "docs", "from": "local", "directory": "docs"},
        ],
    )

    def _boom(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError("stale lock must not re-resolve")

    monkeypatch.setattr(source_transport, "resolve_ref", _boom)
    monkeypatch.setattr(source_transport, "acquire_network", _boom)
    monkeypatch.setattr(publish, "expand_selectors", _boom)
    errors = _install_failed(cfg)
    _assert_head_code(errors, CODE_LOCK_STALE)
    assert CODE_SNAPSHOT_UNAVAILABLE not in errors[0]
    assert _installed_state_hash(project, csk_home, ["review"]) == before


def test_unavailable_snapshot_refuses_without_recreate(
    tmp_path: Path, skills_root: Path, csk_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """AC: missing snapshot is source_snapshot_unavailable, never recreated.

    Both the live bytes and the store entry are gone, so nothing could
    serve; the install refuses and recreates nothing.
    Production call site: ``installer.install`` (fetch=False).
    """
    project, source = _write_local_project(tmp_path, [("review", "review")])
    cfg = _v2_config(csk_home, skills_root, project)
    _install_ok(cfg)
    member = _read_lock(project).members[0]
    assert isinstance(member.package, LocalSnapshot)
    key = package_identity_sha256(member.package)
    from csk.sources import store as store_module

    store_entry = store_module.entry_dir(csk_home, "review", key)
    assert store_entry.exists()
    before = _installed_state_hash(project, csk_home, ["review"])

    shutil.rmtree(source / "review")
    shutil.rmtree(store_entry)

    def _boom(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError("unavailable snapshot must not re-resolve")

    monkeypatch.setattr(source_transport, "resolve_ref", _boom)
    monkeypatch.setattr(source_transport, "acquire_network", _boom)
    errors = _install_failed(cfg)
    _assert_head_code(errors, CODE_SNAPSHOT_UNAVAILABLE)
    assert CODE_SNAPSHOT_CHANGED not in errors[0]
    assert CODE_LOCK_STALE not in errors[0]
    assert not store_entry.exists()
    assert _installed_state_hash(project, csk_home, ["review"]) == before


def test_live_drift_refuses_changed_without_touching_store(
    tmp_path: Path, skills_root: Path, csk_home: Path
) -> None:
    """AC: drifted live bytes refuse; the locked store entry is untouched.

    The store key always serves lock bytes, never current bytes: a
    drifted live tree refuses ``source_snapshot_changed`` and the
    previous store entry stays byte-identical.
    Production call site: ``installer.install`` (fetch=False).
    """
    project, source = _write_local_project(tmp_path, [("review", "review")])
    cfg = _v2_config(csk_home, skills_root, project)
    _install_ok(cfg)
    member = _read_lock(project).members[0]
    assert isinstance(member.package, LocalSnapshot)
    key = package_identity_sha256(member.package)
    from csk.sources import store as store_module

    entry = store_module.entry_dir(csk_home, "review", key)
    entry_before = _tree_hash(entry)

    (source / "review" / "SKILL.md").write_text(
        "---\nname: review\ndescription: drifted\n---\n\n# review\n", encoding="utf-8"
    )
    errors = _install_failed(cfg)
    _assert_head_code(errors, CODE_SNAPSHOT_CHANGED)
    assert _tree_hash(entry) == entry_before


@pytest.mark.parametrize(
    "live",
    [
        pytest.param("unchanged", id="live-unchanged"),
        pytest.param("drifted", id="live-drifted"),
        pytest.param("missing", id="live-missing"),
    ],
)
def test_missing_store_entry_refuses_unavailable_never_heals(
    tmp_path: Path,
    skills_root: Path,
    csk_home: Path,
    monkeypatch: pytest.MonkeyPatch,
    live: str,
) -> None:
    """AC: a missing store entry refuses unavailable whatever live bytes are.

    The store is consulted before any live read: with the entry
    absent, live bytes that match the lock, live bytes that drifted,
    and missing live bytes all refuse
    ``source_snapshot_unavailable`` — never success-by-heal, never a
    re-resolve into ``source_snapshot_changed`` — with the store
    still absent and the lock and installed state byte-identical.
    Live capture is deliberately not patched: the drifted param
    proves the ordering, because a live-first implementation would
    answer ``source_snapshot_changed`` for it.
    Production call site: ``installer.install`` (fetch=False).
    """
    project, source = _write_local_project(tmp_path, [("review", "review")])
    cfg = _v2_config(csk_home, skills_root, project)
    _install_ok(cfg)
    member = _read_lock(project).members[0]
    assert isinstance(member.package, LocalSnapshot)
    key = package_identity_sha256(member.package)
    from csk.sources import store as store_module

    store_entry = store_module.entry_dir(csk_home, "review", key)
    assert store_entry.exists()
    before = _installed_state_hash(project, csk_home, ["review"])

    shutil.rmtree(store_entry)
    if live == "drifted":
        (source / "review" / "SKILL.md").write_text(
            "---\nname: review\ndescription: drifted\n---\n\n# review\n",
            encoding="utf-8",
        )
    elif live == "missing":
        shutil.rmtree(source / "review")

    def _boom(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError("unavailable snapshot must not re-resolve")

    monkeypatch.setattr(source_transport, "resolve_ref", _boom)
    monkeypatch.setattr(source_transport, "acquire_network", _boom)
    monkeypatch.setattr(publish, "expand_selectors", _boom)
    errors = _install_failed(cfg)
    _assert_head_code(errors, CODE_SNAPSHOT_UNAVAILABLE)
    assert CODE_SNAPSHOT_CHANGED not in errors[0]
    assert CODE_LOCK_STALE not in errors[0]
    assert not store_entry.exists()
    assert _installed_state_hash(project, csk_home, ["review"]) == before


def test_missing_snapshot_corpus_case_refuses_unavailable_via_install(
    tmp_path: Path, skills_root: Path, csk_home: Path
) -> None:
    """Corpus ``missing-snapshot`` through the production install entry point.

    The corpus input is locked A, ``snapshot_store: absent``, live B;
    the expected outcome is ``source_snapshot_unavailable``. The
    store leaf's driver covers the four consumer readers; this test
    covers the install operation, which must answer the same class.
    Production call site: ``installer.install`` (fetch=False).
    """
    project, source = _write_local_project(tmp_path, [("review", "review")])
    cfg = _v2_config(csk_home, skills_root, project)
    _install_ok(cfg)
    member = _read_lock(project).members[0]
    assert isinstance(member.package, LocalSnapshot)
    key = package_identity_sha256(member.package)
    from csk.sources import store as store_module

    store_entry = store_module.entry_dir(csk_home, "review", key)
    assert store_entry.exists()

    shutil.rmtree(store_entry)
    (source / "review" / "SKILL.md").write_text(
        "---\nname: review\ndescription: B\n---\n\n# review B\n", encoding="utf-8"
    )
    errors = _install_failed(cfg)
    _assert_head_code(errors, CODE_SNAPSHOT_UNAVAILABLE)
    assert CODE_SNAPSHOT_CHANGED not in errors[0]
    assert not store_entry.exists()


def test_refresh_replaces_lock_and_markers_after_gates(
    tmp_path: Path, skills_root: Path, csk_home: Path
) -> None:
    """AC: explicit refresh is the only way to replace refs and membership.

    Production call site: ``installer.install`` (fetch=True).
    """
    project, source = _write_local_project(tmp_path, [("review", "review")])
    cfg = _v2_config(csk_home, skills_root, project)
    _install_ok(cfg)
    old_lock = (project / publish.SKILLFILE_LOCK_NAME).read_bytes()

    _write_skill(source / "docs", "docs")
    _write_skillfile_v2(
        project,
        {"local": {"path": os.fspath(source)}},
        [
            {"name": "review", "from": "local", "directory": "review"},
            {"name": "docs", "from": "local", "directory": "docs"},
        ],
    )
    result = _install_ok(cfg, fetch=True)
    assert any("lock replaced" in message for message in result.messages)
    new_lock = (project / publish.SKILLFILE_LOCK_NAME).read_bytes()
    assert new_lock != old_lock
    lock = _read_lock(project)
    assert sorted(member.name for member in lock.members) == ["docs", "review"]
    assert (project / ".agents" / "skills" / "docs" / ".csk-install.json").exists()


class _FailOnce:
    """Fail the first call, then delegate; records that it fired."""

    def __init__(self, error: BaseException) -> None:
        self.error = error
        self.fired = 0

    def __call__(self, *args: Any, **kwargs: Any) -> Any:
        if self.fired:
            raise AssertionError("fault fired twice; expected single-shot")
        self.fired += 1
        raise self.error


def _seam_fault(
    monkeypatch: pytest.MonkeyPatch, target: str, error: BaseException
) -> _FailOnce:
    """Patch one seam to fail once; returns the fired counter."""

    import sys as _sys

    fault = _FailOnce(error)
    module_name, _, attr = target.rpartition(".")
    module = _sys.modules[module_name]
    real = getattr(module, attr)

    def wrapper(*args: Any, **kwargs: Any) -> Any:
        if not fault.fired:
            return fault(*args, **kwargs)
        return real(*args, **kwargs)

    monkeypatch.setattr(module, attr, wrapper)
    return fault


def _refresh_fixture(
    tmp_path: Path, skills_root: Path, csk_home: Path
) -> tuple[Path, Path, GlobalConfig]:
    project, source = _write_local_project(tmp_path, [("review", "review")])
    cfg = _v2_config(csk_home, skills_root, project)
    _install_ok(cfg)
    (source / "review" / "SKILL.md").write_text(
        "---\nname: review\ndescription: v2\n---\n\n# review\n", encoding="utf-8"
    )
    return project, source, cfg


def test_all_or_nothing_selection_fault(
    tmp_path: Path, skills_root: Path, csk_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """S-TXN: a selection failure leaves lock and installed state identical.

    Production call site: ``installer.install`` (fetch=True).
    """
    project, _source, cfg = _refresh_fixture(tmp_path, skills_root, csk_home)
    before = _installed_state_hash(project, csk_home, ["review"])
    fault = _seam_fault(
        monkeypatch, "csk.sources.publish.expand_selectors", RuntimeError("injected")
    )
    errors = _install_failed(cfg, fetch=True)
    assert fault.fired == 1
    assert errors
    assert _installed_state_hash(project, csk_home, ["review"]) == before


def test_all_or_nothing_snapshot_fault(
    tmp_path: Path, skills_root: Path, csk_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """S-TXN: a snapshot failure leaves lock and installed state identical.

    Production call site: ``installer.install`` (fetch=True).
    """
    project, _source, cfg = _refresh_fixture(tmp_path, skills_root, csk_home)
    before = _installed_state_hash(project, csk_home, ["review"])
    fault = _seam_fault(
        monkeypatch,
        "csk.sources.snapshot.capture_package_snapshot",
        RuntimeError("injected"),
    )
    errors = _install_failed(cfg, fetch=True)
    assert fault.fired == 1
    assert errors
    assert _installed_state_hash(project, csk_home, ["review"]) == before


def test_all_or_nothing_closure_fault(
    tmp_path: Path, skills_root: Path, csk_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """S-TXN: a closure failure leaves lock and installed state identical.

    Production call site: ``installer.install`` (fetch=True).
    """
    project, _source, cfg = _refresh_fixture(tmp_path, skills_root, csk_home)
    before = _installed_state_hash(project, csk_home, ["review"])
    fault = _seam_fault(
        monkeypatch, "csk.sources.publish.resolve_source_closure", RuntimeError("injected")
    )
    errors = _install_failed(cfg, fetch=True)
    assert fault.fired == 1
    assert errors
    assert _installed_state_hash(project, csk_home, ["review"]) == before


def test_all_or_nothing_audit_fault(
    tmp_path: Path, skills_root: Path, csk_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """S-TXN: an audit failure leaves lock and installed state identical.

    Production call site: ``installer.install`` (fetch=True).
    """
    project, _source, cfg = _refresh_fixture(tmp_path, skills_root, csk_home)
    before = _installed_state_hash(project, csk_home, ["review"])
    fault = _seam_fault(
        monkeypatch, "csk.sources.publish.run_schema2_audit_gate", RuntimeError("injected")
    )
    errors = _install_failed(cfg, fetch=True)
    assert fault.fired == 1
    assert errors
    assert _installed_state_hash(project, csk_home, ["review"]) == before


def test_all_or_nothing_publication_fault(
    tmp_path: Path, skills_root: Path, csk_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """S-TXN: a publication failure leaves lock and installed state identical.

    The fault fires inside the transaction engine at the first write;
    the journal rolls everything back.
    Production call site: ``installer.install`` (fetch=True).
    """
    from csk.sources.publish import TransactionEngine

    project, _source, cfg = _refresh_fixture(tmp_path, skills_root, csk_home)
    before = _installed_state_hash(project, csk_home, ["review"])
    fired: list[str] = []
    real_factory = publish.TransactionEngine

    def factory(home: Path, **kwargs: Any) -> TransactionEngine:
        def hook(point: str, target: Any) -> None:
            if not fired:
                fired.append(point)
                raise RuntimeError("injected publication fault")

        return real_factory(
            home, fault_hook=hook, pre_write_hook=kwargs.get("pre_write_hook")
        )

    monkeypatch.setattr(publish, "TransactionEngine", factory)
    errors = _install_failed(cfg, fetch=True)
    assert fired
    assert errors
    assert _installed_state_hash(project, csk_home, ["review"]) == before


def test_membership_change_during_capture_fails_snapshot_changed(
    tmp_path: Path, skills_root: Path, csk_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """AC: a membership change during capture fails source_snapshot_changed.

    The race is a real filesystem mutation at the capture seam: the
    first capture observes a new member directory appear, and the
    re-enumeration afterwards refuses.
    Production call site: ``installer.install`` (fetch=True).
    """
    project = make_project(tmp_path)
    source = tmp_path / "pkgs"
    _write_skill(source / "coll" / "review", "review")
    _write_skillfile_v2(
        project,
        {"local": {"path": os.fspath(source)}},
        [{"from": "local", "directory": "coll", "include": ["*"]}],
    )
    cfg = _v2_config(csk_home, skills_root, project)
    _install_ok(cfg)
    before = _installed_state_hash(project, csk_home, ["review"])

    real_capture = publish.snapshot.capture_package_snapshot
    mutated: list[str] = []

    def racing_capture(*args: Any, **kwargs: Any) -> Any:
        if not mutated:
            mutated.append("docs")
            _write_skill(source / "coll" / "docs", "docs")
        return real_capture(*args, **kwargs)

    monkeypatch.setattr(publish.snapshot, "capture_package_snapshot", racing_capture)
    errors = _install_failed(cfg, fetch=True)
    assert mutated == ["docs"]
    _assert_head_code(errors, CODE_SNAPSHOT_CHANGED)
    assert _installed_state_hash(project, csk_home, ["review"]) == before


def _write_git_collection(
    workdir: Path, skills: dict[str, dict[str, Any]], *, prefix: str = "skills"
) -> None:
    """Write skill packages under prefix/ in a fixture workdir (uncommitted)."""
    for name, kwargs in skills.items():
        _write_skill(workdir / prefix / name, name, **kwargs)


def _git_skillfile(
    project: Path,
    url: str,
    ref: dict[str, str],
    selectors: list[dict[str, Any]],
    *,
    alias: str = "upstream",
) -> None:
    _write_skillfile_v2(
        project, {alias: {"git": url, **ref}}, selectors
    )


def test_git_members_install_from_pinned_revision(
    tmp_path: Path, skills_root: Path, csk_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """AC: Git members install; the lock pins repository, commit, directory.

    Production call site: ``installer.install`` -> ``publish.install_schema2``.
    """
    _require_selection()
    kit = tmp_path / "kit"
    _write_git_collection(kit, {"review": {}, "docs": {}})
    oids = _make_repo(kit, [{}])
    url = "https://example.test/kit.git"
    fake = _FakeTransport()
    fake.add(url, kit, "example.test/kit")
    _install_fake_transport(monkeypatch, fake)

    project = make_project(tmp_path)
    _git_skillfile(
        project,
        url,
        {"revision": oids[0]},
        [{"from": "upstream", "directory": "skills", "include": ["*"]}],
    )
    cfg = _v2_config(csk_home, skills_root, project)
    _install_ok(cfg)

    lock = _read_lock(project)
    assert sorted(member.name for member in lock.members) == ["docs", "review"]
    for member in lock.members:
        assert isinstance(member.package, NetworkGit)
        assert member.package.repository == "example.test/kit"
        assert member.package.commit.hex == oids[0]
        assert member.package.directory == member.directory
        assert member.selection is not None
    assert (project / ".agents" / "skills" / "review" / "SKILL.md").exists()
    assert (project / ".agents" / "skills" / "docs" / "SKILL.md").exists()
    assert fake.acquire_calls, "expected at least one acquisition"


def test_diamond_requirements_unify_with_single_acquisition(
    tmp_path: Path, skills_root: Path, csk_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """AC: identical transitive requirements unify, including diamonds.

    Two roots require one skill at one commit; the closure installs it
    once and acquires the commit once.
    Production call site: ``installer.install`` -> ``publish.install_schema2``.
    """
    lib = tmp_path / "librepo"
    _write_skill(lib, "lib")
    lib_oids = _make_repo(lib, [{}])
    lib_url = "https://example.test/lib.git"
    fake = _FakeTransport()
    fake.add(lib_url, lib, "example.test/lib")
    _install_fake_transport(monkeypatch, fake)

    requirement = {
        "lib": {"git": lib_url, "ref": {"kind": "revision", "value": lib_oids[0]}}
    }
    project = make_project(tmp_path)
    source = tmp_path / "pkgs"
    _write_skill(source / "alpha", "alpha", requirements=requirement)
    _write_skill(source / "beta", "beta", requirements=requirement)
    _write_skillfile_v2(
        project,
        {"local": {"path": os.fspath(source)}},
        [
            {"name": "alpha", "from": "local", "directory": "alpha"},
            {"name": "beta", "from": "local", "directory": "beta"},
        ],
    )
    cfg = _v2_config(csk_home, skills_root, project)
    _install_ok(cfg)

    lock = _read_lock(project)
    assert sorted(member.name for member in lock.members) == ["alpha", "beta", "lib"]
    lib_member = next(item for item in lock.members if item.name == "lib")
    assert isinstance(lib_member.package, NetworkGit)
    assert lib_member.package.commit.hex == lib_oids[0]
    assert lib_member.selection is None
    assert (project / ".agents" / "skills" / "lib" / "SKILL.md").exists()
    lib_acquisitions = [
        call for call in fake.acquire_calls if call == ("example.test/lib", lib_oids[0])
    ]
    assert len(lib_acquisitions) == 1


def test_conflicting_dependency_identities_fail_name_conflict(
    tmp_path: Path, skills_root: Path, csk_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """AC: conflicting dependency identities fail source_name_conflict.

    One name required from two repositories refuses through the
    production install.
    Production call site: ``installer.install`` -> ``publish.install_schema2``.
    """
    lib1 = tmp_path / "lib1"
    _write_skill(lib1, "lib")
    oids1 = _make_repo(lib1, [{}])
    lib2 = tmp_path / "lib2"
    _write_skill(lib2, "lib")
    _make_repo(lib2, [{}])
    fake = _FakeTransport()
    fake.add("https://example.test/lib1.git", lib1, "example.test/lib1")
    fake.add("https://example.test/lib2.git", lib2, "example.test/lib2")
    _install_fake_transport(monkeypatch, fake)

    project = make_project(tmp_path)
    source = tmp_path / "pkgs"
    _write_skill(
        source / "alpha",
        "alpha",
        requirements={
            "lib": {
                "git": "https://example.test/lib1.git",
                "ref": {"kind": "revision", "value": oids1[0]},
            }
        },
    )
    _write_skill(
        source / "beta",
        "beta",
        requirements={
            "lib": {
                "git": "https://example.test/lib2.git",
                "ref": {"kind": "revision", "value": oids1[0]},
            }
        },
    )
    _write_skillfile_v2(
        project,
        {"local": {"path": os.fspath(source)}},
        [
            {"name": "alpha", "from": "local", "directory": "alpha"},
            {"name": "beta", "from": "local", "directory": "beta"},
        ],
    )
    cfg = _v2_config(csk_home, skills_root, project)
    errors = _install_failed(cfg)
    _assert_head_code(errors, CODE_NAME_CONFLICT)


def _two_member_git_project(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> tuple[Path, _FakeTransport, list[str], str]:
    kit = tmp_path / "kit"
    _write_git_collection(kit, {"review": {}, "docs": {}})
    oids = _make_repo(kit, [{}])
    run(["git", "tag", "v1"], kit)
    (kit / "skills" / "review" / "SKILL.md").write_text(
        "---\nname: review\ndescription: v2\n---\n\n# review\n", encoding="utf-8"
    )
    run(["git", "add", "--", "."], kit)
    run(["git", *_git_identity(), "commit", "--quiet", "-m", "c1"], kit)
    moved = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=kit,
        capture_output=True,
        text=True,
        check=True,
        timeout=20,
    ).stdout.strip()
    run(["git", "tag", "-f", "v1", moved], kit)
    url = "https://example.test/kit.git"
    fake = _FakeTransport()
    fake.add(url, kit, "example.test/kit")
    _install_fake_transport(monkeypatch, fake)
    return kit, fake, [oids[0], moved], url


def test_one_commit_per_alias_across_two_passes(
    tmp_path: Path, skills_root: Path, csk_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """AC: two members from one alias resolve to one commit, two passes.

    The tag moves between passes; the operation-scoped memo serves
    the first resolution to both members, both closure passes agree
    on it, and the transport resolves once. A repeated top-level
    root resolution with the same mode serves the memo too instead
    of observing the moved tag.
    Production call site: ``publish.resolve_source_closure`` (shared
    ``ResolvingSources``), the same seam ``install_schema2`` uses.
    """
    _require_selection()
    _kit, fake, (first, moved), url = _two_member_git_project(tmp_path, monkeypatch)
    fake.resolve_script = [first, moved]

    project = make_project(tmp_path)
    _git_skillfile(
        project,
        url,
        {"tag": "v1"},
        [
            {"name": "review", "from": "upstream", "directory": "skills/review"},
            {"name": "docs", "from": "upstream", "directory": "skills/docs"},
        ],
    )
    cfg = _v2_config(csk_home, skills_root, project)
    fresh = manifest.load_manifest(project, allow_schema_2=True)
    assert fresh is not None
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    mode = modes.ResolvingSources(
        home=csk_home, workspace=workspace, tool_for_endpoint=None, policy_path=None
    )
    source_roots, git_resolutions = publish.resolve_schema2_source_roots(
        project, fresh, mode=mode
    )
    members = publish.resolve_schema2_members(
        project, fresh, mode=mode, source_roots=source_roots,
        git_resolutions=git_resolutions,
    )
    raw_captures = publish.capture_schema2_members(members, home=csk_home)
    by_name = {member.name: member for member in members}

    first_pass = publish.resolve_source_closure(
        cfg, (by_name["review"],), raw_captures, fresh, git_resolutions, mode=mode
    )
    second_pass = publish.resolve_source_closure(
        cfg, (by_name["docs"],), raw_captures, fresh, git_resolutions, mode=mode
    )
    review_commit = next(node for node in first_pass if node.name == "review")
    docs_commit = next(node for node in second_pass if node.name == "docs")
    assert review_commit.resolved.commit == first
    assert docs_commit.resolved.commit == first
    assert fake.resolve_calls == [("example.test/kit", "tag", "v1")]
    repeated_roots, repeated_resolutions = publish.resolve_schema2_source_roots(
        project, fresh, mode=mode
    )
    assert repeated_roots["upstream"] == source_roots["upstream"]
    assert repeated_resolutions["upstream"].commit == first
    assert fake.resolve_calls == [("example.test/kit", "tag", "v1")]


def test_root_branch_resolves_but_transitive_branch_refuses(
    tmp_path: Path, skills_root: Path, csk_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """AC: branch refs stay root-only.

    A root branch alias resolves through the bounded transport;
    a branch in a transitive requirement refuses at the manifest
    grammar before the closure ever sees it.
    Production call site: ``installer.install``.
    """
    kit = tmp_path / "kit"
    _write_git_collection(kit, {"review": {}})
    oids = _make_repo(kit, [{}])
    branch = subprocess.run(
        ["git", "branch", "--show-current"],
        cwd=kit,
        capture_output=True,
        text=True,
        check=True,
        timeout=20,
    ).stdout.strip()
    url = "https://example.test/kit.git"
    fake = _FakeTransport()
    fake.add(url, kit, "example.test/kit")
    _install_fake_transport(monkeypatch, fake)

    project = make_project(tmp_path)
    _git_skillfile(
        project,
        url,
        {"branch": branch},
        [{"name": "review", "from": "upstream", "directory": "skills/review"}],
    )
    cfg = _v2_config(csk_home, skills_root, project)
    _install_ok(cfg)
    lock = _read_lock(project)
    assert lock.members[0].package.commit.hex == oids[0]

    lib = tmp_path / "librepo"
    _write_skill(lib, "lib")
    lib_oids = _make_repo(lib, [{}])
    lib_url = "https://example.test/lib.git"
    fake.add(lib_url, lib, "example.test/lib")
    project2 = make_project(tmp_path, name="project2")
    source2 = tmp_path / "pkgs2"
    _write_skill(
        source2 / "alpha",
        "alpha",
        requirements={
            "lib": {"git": lib_url, "ref": {"kind": "branch", "value": "main"}}
        },
    )
    _write_skillfile_v2(
        project2,
        {"local": {"path": os.fspath(source2)}},
        [{"name": "alpha", "from": "local", "directory": "alpha"}],
    )
    cfg2 = _v2_config(csk_home, skills_root, project2)
    errors = _install_failed(cfg2)
    _assert_head_code(errors, CODE_MEMBER_INVALID)
    assert lib_oids  # fixture used for the requirement shape only


def test_transitive_tag_refused_as_floating(
    tmp_path: Path, skills_root: Path, csk_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """AC: transitive tags refuse; the extension adds no floating refs.

    Production call site: ``installer.install`` -> ``publish.install_schema2``.
    """
    lib = tmp_path / "librepo"
    _write_skill(lib, "lib")
    _make_repo(lib, [{}])
    lib_url = "https://example.test/lib.git"
    fake = _FakeTransport()
    fake.add(lib_url, lib, "example.test/lib")
    _install_fake_transport(monkeypatch, fake)

    project = make_project(tmp_path)
    source = tmp_path / "pkgs"
    _write_skill(
        source / "alpha",
        "alpha",
        requirements={"lib": {"git": lib_url, "ref": {"kind": "tag", "value": "v1"}}},
    )
    _write_skillfile_v2(
        project,
        {"local": {"path": os.fspath(source)}},
        [{"name": "alpha", "from": "local", "directory": "alpha"}],
    )
    cfg = _v2_config(csk_home, skills_root, project)
    errors = _install_failed(cfg)
    _assert_head_code(errors, CODE_SELECTION_INVALID)
    assert fake.acquire_calls == []


def test_transitive_name_mismatch_refused(
    tmp_path: Path, skills_root: Path, csk_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """AC: a requirement satisfied by the wrong skill refuses as missing.

    Production call site: ``installer.install`` -> ``publish.install_schema2``.
    """
    lib = tmp_path / "librepo"
    _write_skill(lib, "other")
    lib_oids = _make_repo(lib, [{}])
    lib_url = "https://example.test/lib.git"
    fake = _FakeTransport()
    fake.add(lib_url, lib, "example.test/lib")
    _install_fake_transport(monkeypatch, fake)

    project = make_project(tmp_path)
    source = tmp_path / "pkgs"
    _write_skill(
        source / "alpha",
        "alpha",
        requirements={
            "lib": {"git": lib_url, "ref": {"kind": "revision", "value": lib_oids[0]}}
        },
    )
    _write_skillfile_v2(
        project,
        {"local": {"path": os.fspath(source)}},
        [{"name": "alpha", "from": "local", "directory": "alpha"}],
    )
    cfg = _v2_config(csk_home, skills_root, project)
    errors = _install_failed(cfg)
    _assert_head_code(errors, CODE_MEMBER_MISSING)


def test_dependency_cycle_fails(
    tmp_path: Path, skills_root: Path, csk_home: Path
) -> None:
    """AC: existing cycle rules apply to the full closure.

    A revision-pinned mutual cycle is a hash fixed-point —
    content(X) must name Y while content(Y) names X — so no pair
    of real Git commits can form one through an install. The
    closure is driven directly with a stubbed ``acquire_git``
    serving two mutually requiring manifests at fixed revisions;
    the pins agree as strings, unification succeeds, and the
    topological order fails.
    Production call site: ``closure.build_source_closure``, the
    same function ``publish.resolve_source_closure`` calls.
    """
    from csk.source_identity import canonical_source_identity

    rev_a, rev_b = "a" * 40, "b" * 40
    url_a, url_b = "https://example.test/aaa.git", "https://example.test/bbb.git"
    root_dir = tmp_path / "root-pkg"
    _write_skill(
        root_dir,
        "root",
        requirements={"aaa": {"git": url_a, "ref": {"kind": "revision", "value": rev_a}}},
    )
    dir_a = tmp_path / "aaa-pkg"
    _write_skill(
        dir_a,
        "aaa",
        requirements={"bbb": {"git": url_b, "ref": {"kind": "revision", "value": rev_b}}},
    )
    dir_b = tmp_path / "bbb-pkg"
    _write_skill(
        dir_b,
        "bbb",
        requirements={"aaa": {"git": url_a, "ref": {"kind": "revision", "value": rev_a}}},
    )
    by_commit = {rev_a: ("aaa", dir_a, url_a), rev_b: ("bbb", dir_b, url_b)}

    def acquire(git_url: str, commit: str, chain: str) -> closure.SourceGitAcquisition:
        _name, materialized, expected_url = by_commit[commit]
        assert git_url == expected_url, (git_url, chain)
        identity = canonical_source_identity(git_url)
        assert identity is not None
        return closure.SourceGitAcquisition(
            commit=commit,
            object_format="sha1",
            identity=identity,
            materialized=materialized,
        )

    cfg = make_config(csk_home, skills_root, tmp_path, agents=["codex_cli"])
    roots = (
        closure.SourceClosureRoot(
            name="root",
            from_alias="local",
            directory="root",
            local=True,
            materialized=root_dir,
            identity=None,
            ref_kind=closure.SOURCE_LOCAL_REF_KIND,
            ref_value="root",
            commit="",
        ),
    )
    with pytest.raises(SourceError) as excinfo:
        closure.build_source_closure(cfg, roots, acquire_git=acquire)
    assert excinfo.value.code == CODE_MEMBER_INVALID
    assert "cycle" in str(excinfo.value)


def test_same_repository_two_commits_fail_name_conflict(
    tmp_path: Path, skills_root: Path, csk_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """AC: one name at two commits of one repository is a name conflict.

    A network-git package identity includes the commit, so two
    commits are conflicting dependency identities.
    Production call site: ``installer.install`` -> ``publish.install_schema2``.
    """
    lib = tmp_path / "librepo"
    _write_skill(lib, "lib")
    first = _make_repo(lib, [{}])
    (lib / "extra.txt").write_text("v2\n", encoding="utf-8")
    run(["git", "add", "--", "."], lib)
    run(["git", *_git_identity(), "commit", "--quiet", "-m", "c1"], lib)
    second = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=lib,
        capture_output=True,
        text=True,
        check=True,
        timeout=20,
    ).stdout.strip()
    assert first[0] != second
    lib_url = "https://example.test/lib.git"
    fake = _FakeTransport()
    fake.add(lib_url, lib, "example.test/lib")
    _install_fake_transport(monkeypatch, fake)

    project = make_project(tmp_path)
    source = tmp_path / "pkgs"
    _write_skill(
        source / "alpha",
        "alpha",
        requirements={
            "lib": {"git": lib_url, "ref": {"kind": "revision", "value": first[0]}}
        },
    )
    _write_skill(
        source / "beta",
        "beta",
        requirements={
            "lib": {"git": lib_url, "ref": {"kind": "revision", "value": second}}
        },
    )
    _write_skillfile_v2(
        project,
        {"local": {"path": os.fspath(source)}},
        [
            {"name": "alpha", "from": "local", "directory": "alpha"},
            {"name": "beta", "from": "local", "directory": "beta"},
        ],
    )
    cfg = _v2_config(csk_home, skills_root, project)
    errors = _install_failed(cfg)
    _assert_head_code(errors, CODE_NAME_CONFLICT)
    assert "Version conflict" in errors[0]


@pytest.mark.parametrize(
    "root_ref",
    [
        pytest.param({"tag": "v1"}, id="tag-root"),
        pytest.param({"branch": "feat"}, id="branch-root"),
    ],
)
def test_git_root_unifies_with_same_commit_revision_requirement(
    tmp_path: Path,
    skills_root: Path,
    csk_home: Path,
    monkeypatch: pytest.MonkeyPatch,
    root_ref: dict[str, str],
) -> None:
    """AC: unification compares resolved commits, never ref spellings.

    A root declared at a floating ``tag`` or ``branch`` resolving to
    C1 and a transitive requirement pinning ``revision`` C1 of the
    same repository are one identity ``(repository, C1, ".")`` and
    install together; the lock pins C1 on both members.
    Production call site: ``installer.install`` -> ``publish.install_schema2``.
    """
    lib = tmp_path / "librepo"
    _write_skill(lib, "alpha")
    oids = _make_repo(lib, [{}])
    if "tag" in root_ref:
        run(["git", "tag", "v1"], lib)
    else:
        run(["git", "branch", "feat"], lib)
    url = "https://example.test/alpha.git"
    fake = _FakeTransport()
    fake.add(url, lib, "example.test/alpha")
    _install_fake_transport(monkeypatch, fake)

    project = make_project(tmp_path)
    source = tmp_path / "pkgs"
    _write_skill(
        source / "beta",
        "beta",
        requirements={
            "alpha": {"git": url, "ref": {"kind": "revision", "value": oids[0]}}
        },
    )
    _write_skillfile_v2(
        project,
        {"up": {"git": url, **root_ref}, "local": {"path": os.fspath(source)}},
        [
            {"name": "alpha", "from": "up", "directory": "."},
            {"name": "beta", "from": "local", "directory": "beta"},
        ],
    )
    cfg = _v2_config(csk_home, skills_root, project)
    result = _install_ok(cfg)
    assert any("alpha installed" in message for message in result.messages)
    assert any("beta installed" in message for message in result.messages)
    lock = _read_lock(project)
    assert sorted(member.name for member in lock.members) == ["alpha", "beta"]
    alpha = next(member for member in lock.members if member.name == "alpha")
    assert isinstance(alpha.package, NetworkGit)
    assert alpha.package.commit.hex == oids[0]


def test_same_name_different_commit_refuses_version_conflict(
    tmp_path: Path, skills_root: Path, csk_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """AC: one name at two commits conflicts however the refs are spelled.

    A root declared at ``tag: v1`` (-> C1) plus a transitive
    requirement pinning ``revision`` C2 of the same repository are
    two identities and refuse ``source_name_conflict`` with a
    version-conflict reason — unification by commit, not by spelling.
    Production call site: ``installer.install`` -> ``publish.install_schema2``.
    """
    lib = tmp_path / "librepo"
    _write_skill(lib, "alpha")
    first = _make_repo(lib, [{}])
    run(["git", "tag", "v1"], lib)
    (lib / "extra.txt").write_text("v2\n", encoding="utf-8")
    run(["git", "add", "--", "."], lib)
    run(["git", *_git_identity(), "commit", "--quiet", "-m", "c1"], lib)
    second = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=lib,
        capture_output=True,
        text=True,
        check=True,
        timeout=20,
    ).stdout.strip()
    assert first[0] != second
    url = "https://example.test/alpha.git"
    fake = _FakeTransport()
    fake.add(url, lib, "example.test/alpha")
    _install_fake_transport(monkeypatch, fake)

    project = make_project(tmp_path)
    source = tmp_path / "pkgs"
    _write_skill(
        source / "beta",
        "beta",
        requirements={
            "alpha": {"git": url, "ref": {"kind": "revision", "value": second}}
        },
    )
    _write_skillfile_v2(
        project,
        {"up": {"git": url, "tag": "v1"}, "local": {"path": os.fspath(source)}},
        [
            {"name": "alpha", "from": "up", "directory": "."},
            {"name": "beta", "from": "local", "directory": "beta"},
        ],
    )
    cfg = _v2_config(csk_home, skills_root, project)
    errors = _install_failed(cfg)
    _assert_head_code(errors, CODE_NAME_CONFLICT)
    assert "Version conflict" in errors[0]


def _closure_acquire_stub(
    packages: dict[str, tuple[Path, str]],
) -> Any:
    """Serve fixed materialized directories keyed by revision hex."""

    from csk.source_identity import canonical_source_identity

    def acquire(git_url: str, commit: str, chain: str) -> closure.SourceGitAcquisition:
        materialized, expected_url = packages[commit]
        assert git_url == expected_url, (git_url, chain)
        identity = canonical_source_identity(git_url)
        assert identity is not None
        return closure.SourceGitAcquisition(
            commit=commit,
            object_format="sha1",
            identity=identity,
            materialized=materialized,
        )

    return acquire


def _local_closure_root(name: str, materialized: Path) -> closure.SourceClosureRoot:
    return closure.SourceClosureRoot(
        name=name,
        from_alias="local",
        directory=name,
        local=True,
        materialized=materialized,
        identity=None,
        ref_kind=closure.SOURCE_LOCAL_REF_KIND,
        ref_value=name,
        commit="",
    )


def test_repeated_root_selection_fails_name_conflict(
    tmp_path: Path, skills_root: Path, csk_home: Path
) -> None:
    """S-CONFLICT: repeated root selections of one name fail in the closure.

    Selection refuses duplicates first through an install, so the
    closure's own repeated-root gate is driven directly: two roots
    named ``dupe`` refuse ``source_name_conflict``.
    Production call site: ``closure.build_source_closure``.
    """
    first = tmp_path / "first"
    second = tmp_path / "second"
    _write_skill(first, "dupe")
    _write_skill(second, "dupe")
    cfg = make_config(csk_home, skills_root, tmp_path, agents=["codex_cli"])
    with pytest.raises(SourceError) as excinfo:
        closure.build_source_closure(
            cfg,
            (_local_closure_root("dupe", first), _local_closure_root("dupe", second)),
            acquire_git=_closure_acquire_stub({}),
        )
    assert excinfo.value.code == CODE_NAME_CONFLICT
    assert "dupe" in str(excinfo.value)


def test_closure_orders_providers_before_consumers(
    tmp_path: Path, skills_root: Path, csk_home: Path
) -> None:
    """S-ORDER: the schema-2 closure keeps provider-first ordering.

    A diamond (alpha and beta require lib at one revision) orders
    exactly ``[lib, alpha, beta]``: providers precede consumers and
    ties break alphabetically, as the schema-1 closure does.
    Production call site: ``closure.build_source_closure``.
    """
    rev = "c" * 40
    url = "https://example.test/lib.git"
    requirement = {"lib": {"git": url, "ref": {"kind": "revision", "value": rev}}}
    dir_alpha = tmp_path / "alpha-pkg"
    dir_beta = tmp_path / "beta-pkg"
    dir_lib = tmp_path / "lib-pkg"
    _write_skill(dir_alpha, "alpha", requirements=requirement)
    _write_skill(dir_beta, "beta", requirements=requirement)
    _write_skill(dir_lib, "lib")
    cfg = make_config(csk_home, skills_root, tmp_path, agents=["codex_cli"])
    nodes = closure.build_source_closure(
        cfg,
        (
            _local_closure_root("alpha", dir_alpha),
            _local_closure_root("beta", dir_beta),
        ),
        acquire_git=_closure_acquire_stub({rev: (dir_lib, url)}),
    )
    assert [node.name for node in nodes] == ["lib", "alpha", "beta"]


def test_transitive_branch_refused_at_closure_gate() -> None:
    """S-ERRORS: the closure gate refuses floating transitive refs.

    Branch requirements refuse at the manifest grammar through an
    install, so the closure's own gate is driven directly: a branch
    names the root-only rule, a tag names the pinned-revision rule,
    and a revision passes.
    Production call site: ``closure._refuse_transitive_ref`` via
    ``closure.build_source_closure``.
    """
    from csk import skillspec

    git = "https://example.test/lib.git"
    branch = skillspec.SkillRequirement(
        name="lib", git=git, ref_kind="branch", ref_value="main"
    )
    with pytest.raises(SourceError) as excinfo:
        closure._refuse_transitive_ref(branch, chain="test -> alpha")
    assert excinfo.value.code == CODE_SELECTION_INVALID
    assert "root project" in str(excinfo.value)
    tag = skillspec.SkillRequirement(
        name="lib", git=git, ref_kind="tag", ref_value="v1"
    )
    with pytest.raises(SourceError) as taginfo:
        closure._refuse_transitive_ref(tag, chain="test -> alpha")
    assert taginfo.value.code == CODE_SELECTION_INVALID
    assert "must pin a revision" in str(taginfo.value)
    revision = skillspec.SkillRequirement(
        name="lib", git=git, ref_kind="revision", ref_value="a" * 40
    )
    closure._refuse_transitive_ref(revision, chain="test -> alpha")


def _context_hash(project: Path, name: str) -> str:
    """Hash one installed context tree, excluding the install marker.

    The marker legitimately changes on refresh (new lock digest,
    new timestamp); everything else in the projected context must
    be byte-identical across a runtime-only or build-only edit.
    """
    digest = hashlib.sha256()
    root = project / ".agents" / "skills" / name
    for path in sorted(root.rglob("*")):
        if path.name == ".csk-install.json":
            continue
        rel = path.relative_to(root).as_posix()
        digest.update(rel.encode())
        if path.is_symlink():
            digest.update(b"link:")
            digest.update(os.readlink(path).encode())
        elif path.is_dir():
            digest.update(b"dir")
        else:
            digest.update(b"file:")
            digest.update(path.read_bytes())
    return digest.hexdigest()


def _member_store_key(project: Path, name: str) -> str:
    member = next(item for item in _read_lock(project).members if item.name == name)
    return package_identity_sha256(member.package)


def test_runtime_only_edit_changes_store_key(
    tmp_path: Path, skills_root: Path, csk_home: Path
) -> None:
    """AC: a runtime-only edit changes the store key, not the context.

    The script changes (B to C) while SKILL.md and the projected
    context stay byte-identical; after refresh the ``(name,
    SHA-256(CCJ-1(package)))`` key in ``source-v1`` is new and the
    old runtime entry is gone.
    Production call site: ``installer.install`` (fetch=True).
    """
    project = make_project(tmp_path)
    source = tmp_path / "pkgs"
    _write_skill(
        source / "review",
        "review",
        commands={"run": {"type": "script", "unix_path": "scripts/run"}},
        runtime_roots=("scripts",),
        extra_files={"scripts/run": "#!/bin/sh\necho v1\n"},
    )
    _write_skillfile_v2(
        project,
        {"local": {"path": os.fspath(source)}},
        [{"name": "review", "from": "local", "directory": "review"}],
    )
    cfg = _v2_config(csk_home, skills_root, project)
    _install_ok(cfg)

    key_before = _member_store_key(project, "review")
    entry_before = publish.runtime_entry_path(csk_home, "review", key_before)
    assert entry_before.is_dir()
    context_before = _context_hash(project, "review")
    skill_before = (project / ".agents" / "skills" / "review" / "SKILL.md").read_bytes()
    assert "scripts/run" not in {
        path.relative_to(project / ".agents" / "skills" / "review").as_posix()
        for path in (project / ".agents" / "skills" / "review").rglob("*")
        if path.is_file()
    }

    (source / "review" / "scripts" / "run").write_text(
        "#!/bin/sh\necho v2\n", encoding="utf-8"
    )
    _install_ok(cfg, fetch=True)

    key_after = _member_store_key(project, "review")
    assert key_after != key_before
    entry_after = publish.runtime_entry_path(csk_home, "review", key_after)
    assert entry_after.is_dir()
    assert not entry_before.exists()
    assert (
        project / ".agents" / "skills" / "review" / "SKILL.md"
    ).read_bytes() == skill_before
    assert _context_hash(project, "review") == context_before


def _stub_build_toolchain(monkeypatch: pytest.MonkeyPatch) -> None:
    """Stub the go-v1 compiler with a hermetic marker-baking worker."""

    import platform as platform_module

    from csk.builds import go_v1
    from csk.builds import metadata as build_metadata
    from csk.builds import toolchain as build_toolchain

    machine = platform_module.machine().lower()
    if machine in {"arm64", "aarch64"}:
        goarch, tuning = "arm64", {"GOARM64": "v8.0"}
    else:
        goarch, tuning = "amd64", {"GOAMD64": "v1"}
    if sys.platform == "darwin":
        goos = "darwin"
    elif os.name == "nt":
        goos = "windows"
    else:
        goos = "linux"
    host = build_toolchain.NativeTarget(goos=goos, goarch=goarch, tuning=tuning)

    class FakeSession:
        target = host
        toolchain = build_toolchain.ToolchainIdentity(
            algorithm=build_toolchain.TOOLCHAIN_ALGORITHM,
            content_sha256="sha256:" + "b" * 64,
            go_relpath=build_toolchain.GO_RELPATH,
            go_version=f"go version go1.25.5 {host.goos}/{host.goarch}",
        )

        def __init__(self, toolchain_config: build_toolchain.ToolchainConfig):
            self.operation_root = toolchain_config.private_base / "operation"
            self.operation_root.mkdir(mode=0o700)
            self.executable = self.operation_root / "go"
            self.goroot = self.operation_root / "goroot"

        def __enter__(self):  # noqa: ANN204
            return self

        def __exit__(self, *args: object) -> None:
            return None

    monkeypatch.setattr(
        build_toolchain,
        "capture_operator_search_path",
        lambda: build_toolchain.OperatorSearchPath(("/fixture/bin",)),
    )
    monkeypatch.setattr(build_toolchain, "establish_toolchain", FakeSession)
    monkeypatch.setattr(build_toolchain, "preflight_toolchain", lambda config: None)

    def fake_build(request: go_v1.BuildRequest) -> go_v1.BuildResult:
        marker = (
            request.source_snapshot.path / request.source_dir / "marker.txt"
        ).read_bytes()
        payload = b"#!/bin/sh\necho " + marker.strip() + b"\n"
        artifact_path = request.toolchain_session.operation_root / (
            f"artifact-{request.command}"
        )
        artifact_path.write_bytes(payload)
        artifact_path.chmod(0o700)
        return go_v1.BuildResult(
            artifact=go_v1.BuildArtifact(
                staged_path=artifact_path,
                metadata=go_v1.ArtifactMetadata(
                    path=build_metadata.derived_artifact_path(
                        request.command, goos=host.goos
                    ),
                    sha256="sha256:" + hashlib.sha256(payload).hexdigest(),
                    size=len(payload),
                ),
            ),
            capability_evidence=go_v1.CapabilityEvidence(
                record_version="capability-evidence-v1",
                execution_policy="manager-worker-v1",
                platform=host.goos,
                controls=(),
            ),
        )

    monkeypatch.setattr(go_v1, "build", fake_build)


def test_build_only_edit_changes_store_key(
    tmp_path: Path, skills_root: Path, csk_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """AC: a build-only edit changes the package and cache identity.

    The build input changes (B to C) while SKILL.md and the
    projected context stay byte-identical; after refresh the
    store key and the receipt-3 cache key are both new.
    Production call site: ``installer.install`` (fetch=True).
    """
    from csk import install_marker

    project = make_project(tmp_path)
    source = tmp_path / "pkgs"
    _write_skill(
        source / "built",
        "built",
        commands={
            "greet": {"type": "build", "driver": "go-v1", "source_dir": "build/cmd/greet"}
        },
        build_roots=("build",),
        extra_files={
            "build/go.mod": "module example.com/greet\n\ngo 1.23\n",
            "build/cmd/greet/main.go": "package main\n\nfunc main() {}\n",
            "build/cmd/greet/marker.txt": "built-v1\n",
        },
    )
    _write_skillfile_v2(
        project,
        {"local": {"path": os.fspath(source)}},
        [{"name": "built", "from": "local", "directory": "built"}],
    )
    base = _v2_config(csk_home, skills_root, project)
    cfg = replace(base, audit=replace(base.audit, enabled=True))
    _stub_build_toolchain(monkeypatch)
    _install_ok(cfg)

    key_before = _member_store_key(project, "built")
    marker_before = install_marker.read_install_marker(
        (project / ".agents" / "skills" / "built" / ".csk-install.json").read_bytes()
    )
    assert isinstance(marker_before, install_marker.InstallMarkerV5)
    cache_before = marker_before.builds["greet"].cache_key
    context_before = _context_hash(project, "built")
    skill_before = (project / ".agents" / "skills" / "built" / "SKILL.md").read_bytes()

    (source / "built" / "build" / "cmd" / "greet" / "marker.txt").write_text(
        "built-v2\n", encoding="utf-8"
    )
    _install_ok(cfg, fetch=True)

    key_after = _member_store_key(project, "built")
    assert key_after != key_before
    marker_after = install_marker.read_install_marker(
        (project / ".agents" / "skills" / "built" / ".csk-install.json").read_bytes()
    )
    assert isinstance(marker_after, install_marker.InstallMarkerV5)
    assert marker_after.builds["greet"].cache_key != cache_before
    assert (
        project / ".agents" / "skills" / "built" / "SKILL.md"
    ).read_bytes() == skill_before
    assert _context_hash(project, "built") == context_before


def test_status_reports_locked_membership_without_rescan(
    tmp_path: Path, skills_root: Path, csk_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """AC: status never rescans collections or advances refs.

    A new member directory appears on disk after the lock; status
    reports the locked review-only membership, stays read-only,
    and touches neither enumeration nor the network.
    Production call site: ``status.collect_status``.
    """
    project, source = _write_local_project(tmp_path, [("review", "review")])
    cfg = _v2_config(csk_home, skills_root, project)
    _install_ok(cfg)
    lock_before = (project / publish.SKILLFILE_LOCK_NAME).read_bytes()

    _write_skill(source / "docs", "docs")

    def _boom(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError("status must not enumerate or fetch")

    monkeypatch.setattr(source_transport, "resolve_ref", _boom)
    monkeypatch.setattr(source_transport, "acquire_network", _boom)
    monkeypatch.setattr(publish, "expand_selectors", _boom)
    monkeypatch.setattr(publish, "expand_collection", _boom)
    collected = status.collect_status(cfg, alias="app")
    assert len(collected) == 1
    assert [skill.name for skill in collected[0].skills] == ["review"]
    assert [skill.label for skill in collected[0].skills] == ["up-to-date"]
    assert collected[0].errors == ()
    assert (project / publish.SKILLFILE_LOCK_NAME).read_bytes() == lock_before


def test_lock_members_sorted_with_selector_ordinals(
    tmp_path: Path, skills_root: Path, csk_home: Path
) -> None:
    """S-ORDER: lock members sort by UTF-8 name; ordinals keep selector order.

    Selectors list review before docs (reverse alphabetical): the
    written lock orders members ``[docs, review]`` while their
    zero-based selection indices stay ``[1, 0]``.
    Production call site: ``installer.install`` -> ``publish.install_schema2``.
    """
    project = make_project(tmp_path)
    source = tmp_path / "pkgs"
    _write_skill(source / "review", "review")
    _write_skill(source / "docs", "docs")
    _write_skillfile_v2(
        project,
        {"local": {"path": os.fspath(source)}},
        [
            {"name": "review", "from": "local", "directory": "review"},
            {"name": "docs", "from": "local", "directory": "docs"},
        ],
    )
    cfg = _v2_config(csk_home, skills_root, project)
    _install_ok(cfg)

    raw_names = [
        member["name"]
        for member in json.loads(
            (project / publish.SKILLFILE_LOCK_NAME).read_text(encoding="utf-8")
        )["members"]
    ]
    assert raw_names == ["docs", "review"]
    lock = _read_lock(project)
    assert [member.name for member in lock.members] == ["docs", "review"]
    assert [member.selection for member in lock.members] == [1, 0]


def _write_built_skill(source: Path) -> None:
    _write_skill(
        source / "built",
        "built",
        commands={
            "greet": {"type": "build", "driver": "go-v1", "source_dir": "build/cmd/greet"}
        },
        build_roots=("build",),
        extra_files={
            "build/go.mod": "module example.com/greet\n\ngo 1.23\n",
            "build/cmd/greet/main.go": "package main\n\nfunc main() {}\n",
            "build/cmd/greet/marker.txt": "built-v1\n",
        },
    )


def test_marker_plan_rejects_build_source_mismatch(
    tmp_path: Path, skills_root: Path, csk_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """N3: the marker plan enforces top-level build-source exactness.

    The wired ``check_top_level_build_source`` call refuses in both
    directions through the plan entry: a local go-v1 record without
    a build source, and a build source without local records. A
    consistent plan passes through.
    Production call site: ``publish.build_marker_plan``.
    """
    from csk import install_marker, skillspec

    project = make_project(tmp_path)
    source = tmp_path / "pkgs"
    _write_built_skill(source)
    _write_skillfile_v2(
        project,
        {"local": {"path": os.fspath(source)}},
        [{"name": "built", "from": "local", "directory": "built"}],
    )
    base = _v2_config(csk_home, skills_root, project)
    cfg = replace(base, audit=replace(base.audit, enabled=True))
    _stub_build_toolchain(monkeypatch)
    _install_ok(cfg)

    marker = install_marker.read_install_marker(
        (project / ".agents" / "skills" / "built" / ".csk-install.json").read_bytes()
    )
    assert isinstance(marker, install_marker.InstallMarkerV5)
    record = marker.builds["greet"]
    member = next(item for item in _read_lock(project).members if item.name == "built")
    spec = skillspec.load_skill_spec(source / "built")

    def plan(
        builds: Any, build_source: Any
    ) -> install_marker.MarkerPlan:
        return publish.build_marker_plan(
            name="built",
            package=member.package,
            lock_sha256="sha256:" + "0" * 64,
            content_sha256="sha256:" + "0" * 64,
            spec=spec,
            files=(),
            agents=("codex_cli",),
            locale_value=None,
            builds=builds,
            build_source=build_source,
        )

    with pytest.raises(SourceError) as missing:
        plan({"greet": record}, None)
    assert missing.value.code == CODE_MEMBER_INVALID
    assert "plan is not valid" in str(missing.value)
    with pytest.raises(SourceError) as spurious:
        plan({}, marker.build_source)
    assert spurious.value.code == CODE_MEMBER_INVALID
    assert "plan is not valid" in str(spurious.value)
    assert plan({"greet": record}, marker.build_source) is not None


_EXTERNAL_COMMIT = "0123456789abcdef0123456789abcdef01234567"


def _external_pipeline_snapshot() -> Any:
    """Build a hermetic external source snapshot (no git, no network)."""

    from csk import protocol_json

    files = tuple(
        sorted(
            (
                git_admission.SnapshotFile(
                    "repo/go.mod", b"module example.test/tool\n\ngo 1.25\n"
                ),
                git_admission.SnapshotFile(
                    "repo/cmd/tool/main.go", b"package main\nfunc main() {}\n"
                ),
                git_admission.SnapshotFile(
                    "skill-build.json",
                    protocol_json.canonical_bytes(
                        {
                            "schema_version": 1,
                            "targets": {
                                "tool": {
                                    "driver": "go-repository-v1",
                                    "build_root": "repo",
                                    "source_dir": "repo/cmd/tool",
                                }
                            },
                        }
                    ),
                ),
            ),
            key=lambda item: item.path,
        )
    )
    framed = bytearray(b"curator-build-source-v1\0")
    for item in files:
        path = item.path.encode()
        framed.extend(b"F")
        framed.extend(len(path).to_bytes(8, "big"))
        framed.extend(path)
        framed.extend(len(item.content).to_bytes(8, "big"))
        framed.extend(item.content)
    canonical = bytes(framed)
    return git_admission.Snapshot(
        object_format="sha1",
        commit=_EXTERNAL_COMMIT,
        files=files,
        canonical_bytes=canonical,
        digest="sha256:" + hashlib.sha256(canonical).hexdigest(),
        tag_verified=True,
    )


class _ExternalFakeCompiler:
    """A deterministic fake Go compiler for the external pipeline."""

    def __init__(self) -> None:
        from csk.build_repository_pipeline import CompilerIdentity

        self.calls = 0
        self.identity = CompilerIdentity(
            content_sha256="sha256:" + "c" * 64,
            go_version="go version go1.26.1 darwin/arm64",
            go_relpath="bin/go",
            goos="darwin",
            goarch="arm64",
            tuning={"GOARM64": "v8.0"},
        )

    def compile(self, root: Path, source_dir: str, command: str) -> bytes:
        self.calls += 1
        assert command == "tool"
        return b"compiled-tool"


def _external_pipeline_result(home: Path, package: Any) -> Any:
    """Run the receipt-3 pipeline into this home's protected store."""

    from csk.build_repository_pipeline import (
        DeclaredState,
        DiskProtectedStore,
        EffectiveState,
        Operation,
        PipelineRequest,
        run_pipeline,
    )

    return run_pipeline(
        PipelineRequest(
            operation=Operation.INSTALL,
            command="tool",
            target="tool",
            declared=DeclaredState(
                repository="tools",
                identity="github.com/example/tools",
                transport="https",
                object_format="sha1",
                commit=_EXTERNAL_COMMIT,
                tag="v1.0.0",
            ),
            effective=EffectiveState(
                identity_kind="network-git",
                identity="github.com/example/tools",
                transport="https",
                object_format="sha1",
                commit=_EXTERNAL_COMMIT,
            ),
            acquire=_external_pipeline_snapshot,
            audit=lambda subject: None,
            store=DiskProtectedStore(home / "external-builds"),
            compiler=_ExternalFakeCompiler(),
            package=package,
        )
    )


def _external_genuine_record(result: Any, package: Any) -> Any:
    """Derive the installed record the receipt evidence supports."""

    from csk import install_marker
    from csk.builds import metadata as build_metadata

    receipt = build_metadata.read_receipt_v3(result.receipt)
    build = receipt.input.build
    assert receipt.input.package == package
    declared = build.source.declared
    effective = build.source.effective
    substitution = effective.substitution
    return install_marker.InstallMarkerBuildV5(
        driver="go-repository-v1",
        receipt_schema_version=3,
        execution_policy="manager-worker-v1",
        cache_key=result.cache_key,
        receipt_sha256=build_metadata.receipt_sha256(result.receipt),
        artifact_sha256="sha256:" + hashlib.sha256(result.artifact).hexdigest(),
        artifact_path=build.artifact_path,
        repository=build.source.repository,
        declared_identity=install_marker.MarkerRepositoryIdentity(
            kind=declared.identity.kind, value=declared.identity.value
        ),
        declared_locked_commit=install_marker.MarkerRepositoryCommit(
            object_format=declared.locked_commit.object_format,
            hex=declared.locked_commit.hex,
        ),
        declared_tag=declared.tag,
        effective_identity=install_marker.MarkerRepositoryIdentity(
            kind=effective.identity.kind, value=effective.identity.value
        ),
        object_format=effective.object_format,
        commit=effective.commit,
        substituted=effective.substituted,
        substitution=None if substitution is None else install_marker.MarkerRepositorySubstitution(
            type=substitution.type,
            ref=None
            if substitution.ref is None
            else install_marker.MarkerRepositoryRef(
                kind=substitution.ref.kind, value=substitution.ref.value
            ),
        ),
        build_source=effective.build_source,
        descriptor_target=build.source.descriptor.target,
    )


def _external_member_spec(result: Any) -> Any:
    """Build the member spec the genuine receipt agrees with."""

    from csk import skillspec
    from csk.build_repository import BuildRepository, LockedCommit
    from csk.builds import metadata as build_metadata

    build = build_metadata.read_receipt_v3(result.receipt).input.build
    declared = build.source.declared
    return skillspec.SkillSpec(
        commands={
            "tool": skillspec.CommandSpec(
                name="tool",
                type="build",
                driver="go-repository-v1",
                repository="tools",
                target=build.source.descriptor.target,
            )
        },
        source_file=None,
        build_repositories={
            "tools": BuildRepository(
                name="tools",
                git="https://github.com/example/tools.git",
                identity=declared.identity.value,
                transport=declared.transport,
                locked_commit=LockedCommit(
                    object_format=declared.locked_commit.object_format,
                    hex=declared.locked_commit.hex,
                ),
                tag=declared.tag,
            )
        },
    )


def _external_marker_bytes(package: Any, record: Any) -> bytes:
    """Serialize an external-only marker carrying one build record."""

    from csk import install_marker

    marker = install_marker.InstallMarkerV5(
        name="tooling",
        package=package,
        lock_sha256="sha256:" + "0" * 64,
        content_sha256="sha256:" + "0" * 64,
        locale=None,
        agents=(),
        commands=("tool",),
        dependencies=(),
        skill_schema_version=8,
        runtime_roots=(),
        build_roots=(),
        installed_at="2000-01-01T00:00:00Z",
        files=("SKILL.md",),
        builds={"tool": record},
        build_source=None,
        attestation=None,
    )
    return install_marker.serialize_install_marker(marker.to_json())


def test_external_evidence_mismatch_refuses_at_reconstruct(tmp_path: Path) -> None:
    """N3: status reconstruction compares the record against receipt evidence.

    The wired ``compare_external_build_evidence`` call runs here for
    the first time: a genuine record reconstructs, while a record
    whose commit no longer matches the receipt refuses
    ``source_member_invalid`` naming the differing field.
    Production call site: ``publish.reconstruct_schema2_external_builds``.
    """
    from dataclasses import replace as dc_replace

    home = tmp_path / "home"
    home.mkdir()
    package = LocalSnapshot(snapshot="sha256:" + "1" * 64)
    result = _external_pipeline_result(home, package)
    assert result.receipt is not None and result.artifact is not None
    record = _external_genuine_record(result, package)
    spec = _external_member_spec(result)
    marker_path = tmp_path / ".csk-install.json"

    marker_path.write_bytes(_external_marker_bytes(package, record))
    outputs = publish.reconstruct_schema2_external_builds(
        home=home,
        name="tooling",
        spec=spec,
        package=package,
        live_marker_path=marker_path,
    )
    assert "tool" in outputs.marker_builds["tooling"]

    # A commit tamper trips marker validation before the comparison
    # (an unsubstituted record must equal declared state), so the
    # comparison is reached with a value mismatch the marker grammar
    # accepts: a wrong receipt digest.
    tampered = dc_replace(record, receipt_sha256="sha256:" + "f" * 64)
    marker_path.write_bytes(_external_marker_bytes(package, tampered))
    with pytest.raises(SourceError) as excinfo:
        publish.reconstruct_schema2_external_builds(
            home=home,
            name="tooling",
            spec=spec,
            package=package,
            live_marker_path=marker_path,
        )
    assert excinfo.value.code == CODE_MEMBER_INVALID
    assert "receipt_sha256" in str(excinfo.value)


def test_parse_ls_remote_accepts_exact_ref_shapes() -> None:
    """The advertisement parser resolves tags, peeled tags and branches."""

    from csk.build_repository import LockedCommit

    commit, tag_object = "a" * 40, "b" * 40
    assert git_admission._parse_ls_remote(
        f"{commit}\trefs/tags/v1\n".encode(), wanted="refs/tags/v1", ref_kind="tag"
    ) == LockedCommit("sha1", commit)
    assert git_admission._parse_ls_remote(
        f"{tag_object}\trefs/tags/v1\n{commit}\trefs/tags/v1^{{}}\n".encode(),
        wanted="refs/tags/v1",
        ref_kind="tag",
    ) == LockedCommit("sha1", commit)
    assert git_admission._parse_ls_remote(
        f"{commit}\trefs/heads/main\n".encode(),
        wanted="refs/heads/main",
        ref_kind="branch",
    ) == LockedCommit("sha1", commit)
    sha256 = "c" * 64
    assert git_admission._parse_ls_remote(
        f"{sha256}\trefs/tags/v2\n".encode(), wanted="refs/tags/v2", ref_kind="tag"
    ) == LockedCommit("sha256", sha256)


def test_parse_ls_remote_fails_closed() -> None:
    """Every malformed advertisement refuses instead of resolving."""

    commit = "a" * 40
    cases = [
        b"no-tab-here\n",
        f"{commit}\trefs/tags/other\n".encode(),
        f"{commit}\trefs/tags/v1\n{commit}\trefs/tags/v1\n".encode(),
        f"{commit}\trefs/tags/v1^{{}}\n{commit}\trefs/tags/v1^{{}}\n".encode(),
        b"x" * 65537,
        b"\xff\xfe\trefs/tags/v1\n",
        b"",
        f"{'z' * 40}\trefs/tags/v1\n".encode(),
        f"{commit}\trefs/heads/main\n".encode(),
        f"{commit}\trefs/tags/v1.evil\n".encode(),
    ]
    for index, output in enumerate(cases):
        if index == 8:
            wanted, ref_kind = "refs/tags/v1", "branch"
        else:
            wanted, ref_kind = "refs/tags/v1", "tag"
        with pytest.raises(git_admission.GitAdmissionError):
            git_admission._parse_ls_remote(output, wanted=wanted, ref_kind=ref_kind)


def _bare_repo_with_tag(tmp_path: Path) -> tuple[Path, str, str, str]:
    """Build a bare repo; returns (bare, tag_commit, branch_commit, branch)."""

    work = tmp_path / "work"
    init_git_repo(work)
    write_files(work, {"file.txt": "v1\n"})
    commit_all(work, "c0")
    run(["git", "tag", "v1"], work)
    tag_commit = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=work,
        capture_output=True,
        text=True,
        check=True,
        timeout=20,
    ).stdout.strip()
    write_files(work, {"file.txt": "v2\n"})
    commit_all(work, "c1")
    branch_commit = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=work,
        capture_output=True,
        text=True,
        check=True,
        timeout=20,
    ).stdout.strip()
    branch = subprocess.run(
        ["git", "branch", "--show-current"],
        cwd=work,
        capture_output=True,
        text=True,
        check=True,
        timeout=20,
    ).stdout.strip()
    bare = tmp_path / "bare.git"
    run(
        ["git", "clone", "--quiet", "--bare", "--", os.fspath(work), os.fspath(bare)],
        tmp_path,
    )
    return bare, tag_commit, branch_commit, branch


def _http_rewriting_tool(
    tmp_path: Path, repository: Path, fixture_url: str
) -> git_admission.GitTool:
    """Wrap real git, rewriting one fixture URL to a local bare repo."""

    real = _real_tool()
    tmp_path.mkdir(parents=True)
    script = tmp_path / "git-wrapper.py"
    wrapper = tmp_path / ("git-wrapper.cmd" if os.name == "nt" else "git-wrapper")
    script.write_text(
        f"#!{sys.executable}\n"
        "import subprocess, sys\n"
        f"args = [({('file://' + os.fspath(repository))!r} if x == {fixture_url!r} else "
        "'protocol.file.allow=always' if x == 'protocol.https.allow=always' else x) for x in sys.argv[1:]]\n"
        f"raise SystemExit(subprocess.run([{os.fspath(real.executable)!r}, *args], check=False).returncode)\n",
        encoding="utf-8",
    )
    if os.name == "nt":
        wrapper.write_text(
            f'@echo off\r\n"{sys.executable}" "{script}" %*\r\n',
            encoding="utf-8",
        )
    else:
        wrapper.write_bytes(script.read_bytes())
    wrapper.chmod(0o700)
    askpass = tmp_path / ("askpass.cmd" if os.name == "nt" else "askpass")
    askpass.write_text("@exit /b 1\r\n" if os.name == "nt" else "#!/bin/sh\nexit 1\n", encoding="utf-8")
    askpass.chmod(0o700)
    return git_admission.GitTool(
        executable=wrapper,
        exec_path=real.exec_path,
        allowed_versions=real.allowed_versions,
        askpass=askpass,
    )


def test_resolve_network_ref_resolves_tag_and_branch(tmp_path: Path) -> None:
    """Tags and branches resolve to advertised commits through ls-remote."""

    from csk.build_repository import parse_repository_source

    bare, tag_commit, branch_commit, branch = _bare_repo_with_tag(tmp_path)
    assert tag_commit != branch_commit
    tool = _http_rewriting_tool(
        tmp_path / "wrapper", bare, "https://example.test/kit.git"
    )
    source = parse_repository_source("https://example.test/kit.git")
    resolved_tag = git_admission.resolve_network_ref(source, "tag", "v1", tool)
    assert resolved_tag.hex == tag_commit
    resolved_branch = git_admission.resolve_network_ref(source, "branch", branch, tool)
    assert resolved_branch.hex == branch_commit


def test_resolve_network_ref_missing_and_invalid(tmp_path: Path) -> None:
    """Missing refs are unavailable; revisions and bad names are invalid."""

    from csk.build_repository import parse_repository_source

    bare, _, _, _ = _bare_repo_with_tag(tmp_path)
    tool = _http_rewriting_tool(
        tmp_path / "wrapper", bare, "https://example.test/kit.git"
    )
    source = parse_repository_source("https://example.test/kit.git")
    with pytest.raises(git_admission.GitAdmissionError) as missing:
        git_admission.resolve_network_ref(source, "tag", "nope", tool)
    assert missing.value.code == git_admission.SOURCE_UNAVAILABLE
    assert missing.value.failure_class == "ref-missing"
    with pytest.raises(git_admission.GitAdmissionError) as revision:
        git_admission.resolve_network_ref(source, "revision", "a" * 40, tool)
    assert revision.value.code == git_admission.IDENTITY_INVALID
    with pytest.raises(git_admission.GitAdmissionError) as bad_name:
        git_admission.resolve_network_ref(source, "branch", "..", tool)
    assert bad_name.value.code == git_admission.IDENTITY_INVALID


def _resolve_test_plan() -> Any:
    """Two-endpoint availability-auth plan for resolve_plan tests."""

    from csk.sources import repository_policy

    return repository_policy.select_endpoints(
        repository_policy.parse_policy(
            {
                "schema_version": 1,
                "repositories": {
                    "example.org/kit": {
                        "endpoints": [
                            {"url": "https://example.org/kit.git", "authentication": "first"},
                            {"url": "git@example.org:kit.git", "authentication": "second"},
                        ],
                        "fallback": "availability-auth",
                    }
                },
            },
            reader_revision=1,
        ),
        "example.org/kit",
    )


def test_resolve_plan_first_success() -> None:
    """One successful attempt resolves with a success diagnostic."""

    from csk.build_repository import LockedCommit

    plan = _resolve_test_plan()
    calls: list[str] = []

    def attempt(**kwargs: Any) -> LockedCommit:
        calls.append(kwargs["endpoint"].url)
        assert kwargs["ref_kind"] == "tag"
        assert kwargs["ref_value"] == "v1"
        return LockedCommit("sha1", "a" * 40)

    result = source_transport.resolve_plan(plan, "tag", "v1", attempt=attempt)
    assert result.lock.hex == "a" * 40
    assert result.attempt_count == 1
    assert calls == [plan.endpoints[0].url]


def test_resolve_plan_falls_back_once_on_availability_failure() -> None:
    """An availability failure opens exactly one alternate endpoint."""

    from csk.build_repository import LockedCommit

    plan = _resolve_test_plan()
    calls: list[str] = []

    def attempt(**kwargs: Any) -> LockedCommit:
        calls.append(kwargs["endpoint"].url)
        if len(calls) == 1:
            raise source_transport.TransportFailure("dns", "first endpoint down")
        return LockedCommit("sha1", "a" * 40)

    result = source_transport.resolve_plan(plan, "tag", "v1", attempt=attempt)
    assert result.lock.hex == "a" * 40
    assert result.attempt_count == 2
    assert calls == [endpoint.url for endpoint in plan.endpoints]


def test_resolve_plan_fail_closed_never_retries() -> None:
    """Fail-closed classes raise without trying the second endpoint."""

    plan = _resolve_test_plan()
    calls: list[str] = []

    def attempt(**kwargs: Any) -> Any:
        calls.append(kwargs["endpoint"].url)
        raise source_transport.TransportFailure("integrity", "do-not-retry")

    with pytest.raises(source_transport.TransportFailure) as captured:
        source_transport.resolve_plan(plan, "tag", "v1", attempt=attempt)
    assert captured.value.failure_class == "integrity"
    assert calls == [plan.endpoints[0].url]


def test_resolve_plan_exhaustion_reports_diagnostics() -> None:
    """Two availability failures exhaust the plan with both diagnostics."""

    plan = _resolve_test_plan()

    def attempt(**kwargs: Any) -> Any:
        raise source_transport.TransportFailure("dns", "all down")

    with pytest.raises(source_transport.TransportResolutionError) as captured:
        source_transport.resolve_plan(plan, "branch", "main", attempt=attempt)
    assert [item.classification for item in captured.value.diagnostics] == [
        "dns",
        "dns",
    ]


def test_resolve_ref_resolves_through_policy_plan() -> None:
    """resolve_ref builds the attempt plan from policy, then resolves."""

    from csk.build_repository import LockedCommit
    from csk.sources import repository_policy

    policy = repository_policy.parse_policy(
        {
            "schema_version": 1,
            "repositories": {
                "example.org/kit": {
                    "endpoints": [
                        {"url": "https://example.org/kit.git", "authentication": "team"},
                    ],
                    "fallback": "none",
                }
            },
        },
        reader_revision=1,
    )
    seen: list[str] = []

    def attempt(**kwargs: Any) -> LockedCommit:
        seen.append(kwargs["endpoint"].url)
        return LockedCommit("sha1", "b" * 40)

    result = source_transport.resolve_ref(
        "example.org/kit", "tag", "v1", None, policy=policy, attempt=attempt
    )
    assert result.lock.hex == "b" * 40
    assert seen == ["https://example.org/kit.git"]
