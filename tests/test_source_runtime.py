"""Local runtime and command-dependency materialization (TASK-260916-nawehj).

The leaf claim: a real local skill with a runnable script command and a
skill dependency installs from a path source, and its shim executes the
frozen runtime copy, never the live authored directory. Every test in
this file drives the production entry point ``csk.installer.install``
(or ``csk.cli.main`` for the upgrade twin) on fixtures built in tmp
dirs; no test touches the network or the real user home.

Row ownership from the attack-surface catalog: S-FS (frozen bytes, no
live resolution), S-IDENTITY (store key derivation), S-TXN (bin targets
in the atomic publication), S-CONFLICT (command collisions),
S-ERRORS (structured refusals at the new seams).

Schema-2 source selection is POSIX-only (operational note 8): the
install helpers skip with the shared ``NO_DESCRIPTOR_TRAVERSAL_REASON``
where traversal is unavailable. Tests that refuse before selection or
never install keep running everywhere.
"""

from __future__ import annotations

import hashlib
import json
import os
import platform as platform_module
import shutil
import subprocess
import sys
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest
from conftest import (
    commit_all,
    init_git_repo,
    make_config,
    make_project,
    write_files,
    write_skillfile,
)

from csk import cli, config, install_marker, installer, shims, skillspec
from csk import dev_substitutions, git_admission, protocol_json
from csk import status as status_module
from csk.builds import go_v1
from csk.builds import metadata as build_metadata
from csk.builds import planner as build_planner
from csk.builds import source as build_source
from csk.builds import toolchain as build_toolchain
from csk.sources import _selection_fs
from csk.sources import publish
from csk.sources import store as source_store
from csk.sources.errors import (
    CODE_MEMBER_INVALID,
    CODE_MEMBER_MISSING,
    CODE_NAME_CONFLICT,
    SourceError,
)
from csk.sources.package_identity import (
    LocalSnapshot,
    package_identity_sha256,
    parse_package_identity,
)

pytestmark = pytest.mark.usefixtures("stable_env")

PROVIDER_SCRIPT_V1 = "#!/bin/sh\necho provider-v1\n"
CONSUMER_SCRIPT_V1 = "#!/bin/sh\necho consumer-v1\n"
CONSUMER_SCRIPT_V2 = "#!/bin/sh\necho consumer-v2-live-edit\n"


def _skill_files(
    name: str,
    command: str,
    script: str,
    *,
    runtime_roots: bool,
    dependencies: dict[str, Any] | None = None,
) -> dict[str, str]:
    manifest: dict[str, Any] = {
        "schema_version": 2,
        "commands": {command: {"type": "script", "unix_path": f"scripts/{command}"}},
    }
    if runtime_roots:
        manifest["runtime_roots"] = ["scripts"]
    if dependencies is not None:
        manifest["dependencies"] = {"commands": dependencies}
    return {
        "SKILL.md": f"---\nname: {name}\ndescription: fixture skill {name}\n---\n\n# {name}\n",
        "agent-skill.json": json.dumps(manifest),
        f"scripts/{command}": script,
    }


def _write_source(root: Path, *, runtime_roots: bool) -> Path:
    source = root / "pkgs"
    write_files(
        source / "provider",
        _skill_files("provider", "provide", PROVIDER_SCRIPT_V1, runtime_roots=runtime_roots),
    )
    write_files(
        source / "consumer",
        _skill_files(
            "consumer",
            "consume",
            CONSUMER_SCRIPT_V1,
            runtime_roots=runtime_roots,
            dependencies={
                "needs-provide": {"type": "skill", "skill": "provider", "command": "provide"},
                "needs-sh": {"type": "system", "command": "sh"},
            },
        ),
    )
    return source


def _v2_config(
    csk_home: Path, skills_root: Path, project: Path
) -> config.GlobalConfig:
    base = make_config(csk_home, skills_root, project, agents=["codex_cli"])
    return replace(
        base,
        experimental=config.ExperimentalConfig(skillfile_sources=True),
    )


def _require_selection() -> None:
    """Skip where schema-2 source selection cannot run (operational note 8).

    Selection needs descriptor-relative traversal (``O_DIRECTORY`` plus
    ``dir_fd`` support), which is POSIX-only; without it every install
    through ``install_schema2`` refuses ``source_selection_invalid``.
    The guard lives on the install helpers -- the paths that need
    selection -- never at module level, so tests that refuse before
    selection or never install keep running on Windows. The predicate
    is the capability, read live: never ``os.name``.
    """

    if not _selection_fs.supports_descriptor_traversal():
        pytest.skip(_selection_fs.NO_DESCRIPTOR_TRAVERSAL_REASON)


def _install_ok(cfg: config.GlobalConfig, **options: Any) -> installer.ProjectResult:
    _require_selection()
    results = installer.install(cfg, alias="app", options=installer.InstallOptions(**options))
    assert len(results) == 1
    result = results[0]
    assert result.status == "ok", result.errors
    return result


def _install_failed(
    cfg: config.GlobalConfig, *, require_selection: bool = True, **options: Any
) -> list[str]:
    """Fail an install, requiring selection unless the refusal precedes it.

    ``require_selection=False`` is only for refusals raised before
    ``install_schema2`` runs (dev-substitution admission in
    ``installer.py``): those tests genuinely run on Windows and must
    not skip. Every other negative path in this module reaches
    selection, so the default stays ``True``.
    """

    if require_selection:
        _require_selection()
    results = installer.install(cfg, alias="app", options=installer.InstallOptions(**options))
    assert len(results) == 1
    result = results[0]
    assert result.status == "failed", result.messages
    assert result.errors
    return list(result.errors)


def _write_skillfile_v2(project: Path, source: Path, members: list[tuple[str, str]]) -> None:
    write_skillfile(
        project,
        {
            "schema_version": 2,
            "sources": {"local": {"path": os.fspath(source)}},
            "skills": [
                {"name": name, "from": "local", "directory": directory}
                for name, directory in members
            ],
        },
    )


def _member_package_key(project: Path, name: str) -> str:
    from csk.sources import lock as lock_module

    lock = lock_module.read_lock((project / publish.SKILLFILE_LOCK_NAME).read_bytes())
    member = next(item for item in lock.members if item.name == name)
    assert isinstance(member.package, LocalSnapshot)
    return package_identity_sha256(member.package)


def _project_tree_hash(project: Path) -> str:
    digest = hashlib.sha256()
    for root, dirs, files in os.walk(project):
        dirs[:] = sorted(entry for entry in dirs if entry != ".git")
        for name in sorted(files):
            path = Path(root) / name
            digest.update(path.relative_to(project).as_posix().encode("utf-8"))
            digest.update(b"\0")
            digest.update(path.read_bytes())
            digest.update(b"\0")
    return digest.hexdigest()


def _context_files(project: Path, name: str) -> set[str]:
    root = project / ".agents" / "skills" / name
    return {
        path.relative_to(root).as_posix()
        for path in root.rglob("*")
        if path.is_file() and path.name != ".csk-install.json"
    }


def _expected_consumer_target(
    project: Path, csk_home: Path, *, runtime_roots: bool
) -> Path:
    """Derive the protected-store target of the consumer shim from the lock."""
    from csk.sources import lock as lock_module

    lock = lock_module.read_lock((project / publish.SKILLFILE_LOCK_NAME).read_bytes())
    member = next(item for item in lock.members if item.name == "consumer")
    assert isinstance(member.package, LocalSnapshot)
    key = package_identity_sha256(member.package)
    entry = publish.runtime_entry_path(csk_home, "consumer", key)
    if runtime_roots:
        return entry / "scripts" / "consume"
    return entry / "bin" / "consume"


def _run_shim(shim: Path) -> str:
    proc = subprocess.run(
        [os.fspath(shim)],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        timeout=60,
    )
    assert proc.returncode == 0, (proc.returncode, proc.stderr.decode("utf-8", "replace"))
    return proc.stdout.decode("utf-8")


def _host_native_target() -> build_toolchain.NativeTarget:
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
    return build_toolchain.NativeTarget(goos=goos, goarch=goarch, tuning=tuning)


def _stub_frozen_baking_toolchain(monkeypatch: pytest.MonkeyPatch) -> None:
    """Stub the compiler with a worker that bakes in a frozen marker.

    The fake worker reads ``marker.txt`` beside the command sources
    through the frozen snapshot it is handed and bakes the bytes into
    a runnable shell artifact, so artifact output proves which frozen
    bytes the worker consumed.
    """

    host = _host_native_target()

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
    # Status re-planning preflights the toolchain read-only; the no-op
    # mirrors the establish stub above (hermetic, no real Go needed).
    monkeypatch.setattr(
        build_toolchain, "preflight_toolchain", lambda config: None
    )

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


def _build_skill_files(marker: bytes) -> dict[str, str]:
    return {
        "SKILL.md": "---\nname: built\ndescription: fixture build skill\n---\n\n# built\n",
        "agent-skill.json": json.dumps(
            {
                "schema_version": 6,
                "capabilities": {},
                "build_roots": ["build"],
                "commands": {
                    "greet": {
                        "type": "build",
                        "driver": "go-v1",
                        "source_dir": "build/cmd/greet",
                    }
                },
            }
        ),
        "build/go.mod": "module example.com/greet\n\ngo 1.23\n",
        "build/cmd/greet/main.go": "package main\n\nfunc main() {}\n",
        "build/cmd/greet/marker.txt": marker.decode("utf-8"),
    }


def _audit_enabled(cfg: config.GlobalConfig) -> config.GlobalConfig:
    return replace(cfg, audit=replace(cfg.audit, enabled=True))


@pytest.mark.skipif(sys.platform == "win32", reason="posix-shim-exec: executes POSIX shell shims")
@pytest.mark.parametrize("runtime_roots", [True, False], ids=["roots", "rootless"])
def test_shim_runs_frozen_bytes_after_live_edit(
    tmp_path: Path, skills_root: Path, csk_home: Path, runtime_roots: bool
) -> None:
    """The deciding test: edit the live script after install; the shim still runs frozen bytes."""
    project = make_project(tmp_path)
    source = _write_source(tmp_path, runtime_roots=runtime_roots)
    write_skillfile(
        project,
        {
            "schema_version": 2,
            "sources": {"local": {"path": os.fspath(source)}},
            "skills": [
                {"name": "provider", "from": "local", "directory": "provider"},
                {"name": "consumer", "from": "local", "directory": "consumer"},
            ],
        },
    )
    cfg = _v2_config(csk_home, skills_root, project)
    _install_ok(cfg)

    shim = project / ".agents" / "bin" / "consume"
    assert shim.exists() or shim.is_symlink()
    assert _run_shim(shim) == "consumer-v1\n"

    live_script = source / "consumer" / "scripts" / "consume"
    live_script.write_text(CONSUMER_SCRIPT_V2, encoding="utf-8")
    assert _run_shim(shim) == "consumer-v1\n", "shim must run the frozen copy, not the live edit"

    consumer_spec = skillspec.load_skill_spec(source / "consumer")
    entries = publish.schema2_shim_path_entries(
        consumer_spec, final_bin=project / ".agents" / "bin"
    )
    mismatch = shims.inspect_bin_shim(
        project / ".agents" / "bin",
        "consume",
        _expected_consumer_target(project, csk_home, runtime_roots=runtime_roots),
        path_entries=entries,
    )
    assert mismatch is None, mismatch


def _install_build_fixture(
    tmp_path: Path,
    skills_root: Path,
    csk_home: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    marker: bytes = b"built-v1",
) -> tuple[Path, Path, config.GlobalConfig]:
    project = make_project(tmp_path)
    source = tmp_path / "pkgs"
    write_files(source / "built", _build_skill_files(marker))
    write_skillfile(
        project,
        {
            "schema_version": 2,
            "sources": {"local": {"path": os.fspath(source)}},
            "skills": [{"name": "built", "from": "local", "directory": "built"}],
        },
    )
    cfg = _audit_enabled(_v2_config(csk_home, skills_root, project))
    _stub_frozen_baking_toolchain(monkeypatch)
    return project, source, cfg


def test_local_build_installs_receipt3_artifact_and_status_is_current(
    tmp_path: Path, skills_root: Path, csk_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A local go-v1 build compiles frozen bytes into the immutable cache."""
    project, _source, cfg = _install_build_fixture(
        tmp_path, skills_root, csk_home, monkeypatch
    )
    _install_ok(cfg)

    marker_raw = (
        project / ".agents" / "skills" / "built" / ".csk-install.json"
    ).read_bytes()
    marker = install_marker.read_install_marker(marker_raw)
    assert isinstance(marker, install_marker.InstallMarkerV5)
    assert set(marker.builds) == {"greet"}
    record = marker.builds["greet"]
    assert record.receipt_schema_version == 3
    assert record.driver == "go-v1"
    assert marker.build_source is not None
    assert list(marker.commands) == ["greet"]

    key_hex = record.cache_key.removeprefix("sha256:")
    entry_dir = csk_home / "builds" / "go-v1-receipt-v3" / key_hex
    # Receipt filename mirrors the cache backend layout (csk-receipt.ccj.json).
    receipt_path = entry_dir / "csk-receipt.ccj.json"
    assert entry_dir.is_dir(), "compiled artifact lives in the immutable cache"
    assert receipt_path.is_file()
    assert not receipt_path.is_symlink()

    statuses = status_module.collect_status(cfg, alias="app")
    assert len(statuses) == 1
    assert statuses[0].errors == ()
    assert [skill.label for skill in statuses[0].skills] == ["up-to-date"]


@pytest.mark.skipif(sys.platform == "win32", reason="posix-shim-exec: executes POSIX shell shims")
def test_built_shim_runs_frozen_artifact_after_live_edit(
    tmp_path: Path, skills_root: Path, csk_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Edit the live build sources after install; the shim still runs the frozen artifact."""
    project, source, cfg = _install_build_fixture(
        tmp_path, skills_root, csk_home, monkeypatch
    )
    _install_ok(cfg)

    shim = project / ".agents" / "bin" / "greet"
    assert _run_shim(shim) == "built-v1\n"

    (source / "built" / "build" / "cmd" / "greet" / "marker.txt").write_text(
        "built-v2-live-edit\n", encoding="utf-8"
    )
    assert _run_shim(shim) == "built-v1\n"

    _install_ok(cfg, fetch=True)
    assert _run_shim(shim) == "built-v2-live-edit\n"


@pytest.mark.skipif(sys.platform == "win32", reason="posix-shim-exec: executes POSIX shell shims")
@pytest.mark.parametrize("runtime_roots", [True, False], ids=["roots", "rootless"])
def test_refresh_replaces_frozen_runtime_under_new_store_key(
    tmp_path: Path,
    skills_root: Path,
    csk_home: Path,
    runtime_roots: bool,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Refresh twin: after ``csk upgrade`` the shim runs the new frozen copy under a new store key."""
    project = make_project(tmp_path)
    source = _write_source(tmp_path, runtime_roots=runtime_roots)
    _write_skillfile_v2(project, source, [("provider", "provider"), ("consumer", "consumer")])
    cfg = _v2_config(csk_home, skills_root, project)
    _install_ok(cfg)

    old_key = _member_package_key(project, "consumer")
    old_entry = publish.runtime_entry_path(csk_home, "consumer", old_key)
    assert old_entry.is_dir()
    shim = project / ".agents" / "bin" / "consume"
    assert _run_shim(shim) == "consumer-v1\n"

    (source / "consumer" / "scripts" / "consume").write_text(
        CONSUMER_SCRIPT_V2, encoding="utf-8"
    )

    cfg_path = csk_home / "config.json"
    cfg_path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "skills_root": os.fspath(skills_root),
                "projects": {
                    "app": {"path": os.fspath(project), "agents": ["codex_cli"]}
                },
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("CSK_CONFIG", os.fspath(cfg_path))
    monkeypatch.setenv("CSK_EXPERIMENTAL_SKILLFILE_SOURCES", "1")
    monkeypatch.chdir(project)
    assert cli.main(["upgrade"]) == 0
    assert _run_shim(shim) == "consumer-v2-live-edit\n"

    new_key = _member_package_key(project, "consumer")
    assert new_key != old_key
    new_entry = publish.runtime_entry_path(csk_home, "consumer", new_key)
    assert new_entry.is_dir()
    assert not old_entry.exists(), "refresh replaces the frozen runtime entry, not just the shim"


@pytest.mark.parametrize("runtime_roots", [True, False], ids=["roots", "rootless"])
def test_runtime_store_key_and_no_live_links(
    tmp_path: Path, skills_root: Path, csk_home: Path, runtime_roots: bool
) -> None:
    """The protected entry is keyed by the locked package and holds no link into the live tree."""
    project = make_project(tmp_path)
    source = _write_source(tmp_path, runtime_roots=runtime_roots)
    _write_skillfile_v2(project, source, [("provider", "provider"), ("consumer", "consumer")])
    cfg = _v2_config(csk_home, skills_root, project)
    _install_ok(cfg)

    key = _member_package_key(project, "consumer")
    entry = publish.runtime_entry_path(csk_home, "consumer", key)
    assert entry.is_dir()
    store_root = source_store.store_root(csk_home).resolve()
    assert entry.resolve().is_relative_to(store_root)
    assert entry.resolve() != (source / "consumer").resolve()

    seen: list[Path] = []
    for root, dirs, files in os.walk(entry):
        dirs.sort()
        for name in sorted(dirs + files):
            candidate = Path(root) / name
            seen.append(candidate)
            assert not candidate.is_symlink(), f"{candidate} must not be a link"
            assert candidate.resolve().is_relative_to(store_root)
    assert seen, "the runtime entry must hold installed files"

    target = _expected_consumer_target(project, csk_home, runtime_roots=runtime_roots)
    assert target.read_bytes() == CONSUMER_SCRIPT_V1.encode("utf-8")
    (source / "consumer" / "scripts" / "consume").write_text(
        CONSUMER_SCRIPT_V2, encoding="utf-8"
    )
    assert target.read_bytes() == CONSUMER_SCRIPT_V1.encode("utf-8")


@pytest.mark.skipif(os.name == "nt", reason="symlink-capture: POSIX symlink fixture needs privilege on Windows")
def test_symlinked_member_refuses(tmp_path: Path, skills_root: Path, csk_home: Path) -> None:
    """A link inside admitted package bytes refuses the install naming its package-relative path."""
    project = make_project(tmp_path)
    source = _write_source(tmp_path, runtime_roots=True)
    try:
        os.symlink(
            os.fspath(source / "consumer" / "scripts" / "consume"),
            os.fspath(source / "consumer" / "scripts" / "linked"),
        )
    except OSError:
        pytest.skip("symlink-capture: host cannot create symlinks")
    _write_skillfile_v2(project, source, [("provider", "provider"), ("consumer", "consumer")])
    cfg = _v2_config(csk_home, skills_root, project)
    errors = _install_failed(cfg)
    assert CODE_MEMBER_INVALID in errors[0]
    assert "scripts/linked" in errors[0]


def test_context_projection_eligibility_matrix(
    tmp_path: Path, skills_root: Path, csk_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Eligibility keeps its rules: scripts/ is context only without commands; roots stay out."""
    _stub_frozen_baking_toolchain(monkeypatch)
    project = make_project(tmp_path)
    source = tmp_path / "pkgs"
    write_files(
        source / "cmder",
        {
            "SKILL.md": "---\nname: cmder\ndescription: fixture\n---\n\n# cmder\n",
            "agent-skill.json": json.dumps(
                {
                    "schema_version": 2,
                    "runtime_roots": ["scripts"],
                    "commands": {"serve": {"type": "script", "unix_path": "scripts/serve"}},
                }
            ),
            "scripts/serve": "#!/bin/sh\necho serve\n",
            "scripts/notes.txt": "runtime, never context\n",
        },
    )
    write_files(
        source / "doc",
        {
            "SKILL.md": "---\nname: doc\ndescription: fixture\n---\n\n# doc\n",
            "agent-skill.json": json.dumps({"schema_version": 2}),
            "scripts/helper": "helper-bytes\n",
        },
    )
    write_files(
        source / "built",
        {
            "SKILL.md": "---\nname: built\ndescription: fixture\n---\n\n# built\n",
            "agent-skill.json": json.dumps(
                {
                    "schema_version": 6,
                    "capabilities": {},
                    "runtime_roots": ["scripts"],
                    "build_roots": ["build"],
                    "commands": {
                        "hello": {"type": "script", "unix_path": "scripts/hello"},
                        "greet": {
                            "type": "build",
                            "driver": "go-v1",
                            "source_dir": "build/cmd/greet",
                        },
                    },
                }
            ),
            "scripts/hello": "#!/bin/sh\necho hi\n",
            "build/go.mod": "module example.com/greet\n\ngo 1.23\n",
            "build/cmd/greet/main.go": "package main\n\nfunc main() {}\n",
            "build/cmd/greet/marker.txt": "built-v1",
        },
    )
    _write_skillfile_v2(
        project, source, [("cmder", "cmder"), ("doc", "doc"), ("built", "built")]
    )
    cfg = _audit_enabled(_v2_config(csk_home, skills_root, project))
    _install_ok(cfg)

    def _under(files: set[str], root: str) -> set[str]:
        return {path for path in files if path == root or path.startswith(root + "/")}

    cmder_ctx = _context_files(project, "cmder")
    assert "SKILL.md" in cmder_ctx
    assert _under(cmder_ctx, "scripts") == set()

    doc_ctx = _context_files(project, "doc")
    assert "scripts/helper" in doc_ctx

    built_ctx = _context_files(project, "built")
    assert _under(built_ctx, "scripts") == set()
    assert _under(built_ctx, "build") == set()

    cmder_entry = publish.runtime_entry_path(
        csk_home, "cmder", _member_package_key(project, "cmder")
    )
    assert (cmder_entry / "scripts" / "serve").is_file()
    assert (
        publish.runtime_entry_path(csk_home, "doc", _member_package_key(project, "doc"))
    ).exists() is False
    built_entry = publish.runtime_entry_path(
        csk_home, "built", _member_package_key(project, "built")
    )
    assert (built_entry / "scripts" / "hello").is_file()
    assert not (built_entry / "build").exists()


def test_command_collision_refuses(
    tmp_path: Path, skills_root: Path, csk_home: Path
) -> None:
    """S-CONFLICT: two members exporting one script command refuse before any staging."""
    project = make_project(tmp_path)
    source = tmp_path / "pkgs"
    write_files(
        source / "alpha",
        _skill_files("alpha", "dupe", "#!/bin/sh\necho alpha\n", runtime_roots=True),
    )
    write_files(
        source / "beta",
        _skill_files("beta", "dupe", "#!/bin/sh\necho beta\n", runtime_roots=True),
    )
    _write_skillfile_v2(project, source, [("alpha", "alpha"), ("beta", "beta")])
    cfg = _v2_config(csk_home, skills_root, project)
    errors = _install_failed(cfg)
    assert CODE_NAME_CONFLICT in errors[0]
    assert "dupe" in errors[0]


def test_build_root_script_refuses(
    tmp_path: Path, skills_root: Path, csk_home: Path
) -> None:
    """A rootless script command below a build root refuses: compiled inputs never enter script runtime."""
    project = make_project(tmp_path)
    source = tmp_path / "pkgs"
    write_files(
        source / "mixed",
        {
            "SKILL.md": "---\nname: mixed\ndescription: fixture\n---\n\n# mixed\n",
            "agent-skill.json": json.dumps(
                {
                    "schema_version": 6,
                    "capabilities": {},
                    "build_roots": ["build"],
                    "commands": {
                        "run": {"type": "script", "unix_path": "build/run"},
                        "gen": {
                            "type": "build",
                            "driver": "go-v1",
                            "source_dir": "build/cmd/gen",
                        },
                    },
                }
            ),
            "build/run": "#!/bin/sh\necho run\n",
            "build/go.mod": "module example.com/gen\n\ngo 1.23\n",
            "build/cmd/gen/main.go": "package main\n\nfunc main() {}\n",
        },
    )
    _write_skillfile_v2(project, source, [("mixed", "mixed")])
    cfg = _v2_config(csk_home, skills_root, project)
    errors = _install_failed(cfg)
    assert CODE_MEMBER_INVALID in errors[0]
    assert "never enters installed script runtime" in errors[0]


def test_missing_system_command_refuses(
    tmp_path: Path, skills_root: Path, csk_home: Path
) -> None:
    """System-command readiness stays mandatory: an unresolvable command refuses the install."""
    project = make_project(tmp_path)
    source = tmp_path / "pkgs"
    write_files(
        source / "needy",
        _skill_files(
            "needy",
            "need",
            "#!/bin/sh\necho need\n",
            runtime_roots=True,
            dependencies={
                "needs-nope": {"type": "system", "command": "csk-no-such-command-xyz"},
            },
        ),
    )
    _write_skillfile_v2(project, source, [("needy", "needy")])
    cfg = _v2_config(csk_home, skills_root, project)
    errors = _install_failed(cfg)
    assert CODE_MEMBER_MISSING in errors[0]
    assert "Missing system command" in errors[0]


def test_missing_skill_dependency_refuses(
    tmp_path: Path, skills_root: Path, csk_home: Path
) -> None:
    """A skill dependency on a non-selected member refuses: filesystem siblings never satisfy it."""
    project = make_project(tmp_path)
    source = _write_source(tmp_path, runtime_roots=True)
    _write_skillfile_v2(project, source, [("consumer", "consumer")])
    cfg = _v2_config(csk_home, skills_root, project)
    errors = _install_failed(cfg)
    assert CODE_MEMBER_MISSING in errors[0]
    assert "Missing skill dependency" in errors[0]


def _write_dev_manifest(project: Path, data: dict[str, Any]) -> None:
    (project / dev_substitutions.DEV_MANIFEST_NAME).write_text(
        json.dumps(data), encoding="utf-8"
    )


def test_source_substitution_still_refused(
    tmp_path: Path, skills_root: Path, csk_home: Path
) -> None:
    """New ``from`` selectors never gain source development substitution, whatever else is admitted."""
    project = make_project(tmp_path)
    source = _write_source(tmp_path, runtime_roots=True)
    _write_skillfile_v2(project, source, [("provider", "provider"), ("consumer", "consumer")])
    _write_dev_manifest(
        project,
        {
            "schema_version": 2,
            "substitutions": {"provider": {"path": "../other"}},
            "build_repository_substitutions": {},
        },
    )
    cfg = _v2_config(csk_home, skills_root, project)
    # The substitution refusal precedes selection (installer.py raises
    # before install_schema2), so this test genuinely runs on Windows.
    errors = _install_failed(cfg, require_selection=False)
    assert "source substitution is forbidden" in errors[0]


def test_strict_audit_refuses_substituted_install(
    tmp_path: Path, skills_root: Path, csk_home: Path
) -> None:
    """Strict audit refuses every substitution before planning, source and external alike."""
    project = make_project(tmp_path)
    source = _write_source(tmp_path, runtime_roots=True)
    _write_skillfile_v2(project, source, [("provider", "provider"), ("consumer", "consumer")])
    _write_dev_manifest(
        project,
        {
            "schema_version": 2,
            "substitutions": {"provider": {"path": "../other"}},
            "build_repository_substitutions": {},
        },
    )
    base = _v2_config(csk_home, skills_root, project)
    cfg = replace(base, audit=replace(base.audit, enabled=True, mode="strict"))
    # The strict-audit refusal precedes selection (installer.py raises
    # before install_schema2), so this test genuinely runs on Windows.
    errors = _install_failed(cfg, require_selection=False)
    assert "strict audit refuses substituted installs" in errors[0]


@pytest.mark.skipif(sys.platform == "win32", reason="posix-shim-exec: executes POSIX shell shims")
def test_failed_refresh_leaves_project_tree_identical(
    tmp_path: Path, skills_root: Path, csk_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """S-TXN/S-ERRORS: a fault at the shim boundary fails structured and leaves prior state byte-identical."""
    project = make_project(tmp_path)
    source = _write_source(tmp_path, runtime_roots=True)
    _write_skillfile_v2(project, source, [("provider", "provider"), ("consumer", "consumer")])
    cfg = _v2_config(csk_home, skills_root, project)
    _install_ok(cfg)
    before = _project_tree_hash(project)
    lock_before = (project / publish.SKILLFILE_LOCK_NAME).read_bytes()
    shim = project / ".agents" / "bin" / "consume"
    assert _run_shim(shim) == "consumer-v1\n"

    (source / "consumer" / "scripts" / "consume").write_text(
        CONSUMER_SCRIPT_V2, encoding="utf-8"
    )

    calls: list[str] = []

    def flaky(*args: Any, **kwargs: Any) -> Path:
        calls.append("write_bin_shim")
        raise PermissionError("injected shim staging fault")

    original = shims.write_bin_shim
    monkeypatch.setattr(shims, "write_bin_shim", flaky)
    errors = _install_failed(cfg, fetch=True)
    assert CODE_MEMBER_INVALID in errors[0]
    assert "launcher cannot be staged" in errors[0]
    assert calls, "the injected fault must be reached"

    assert _project_tree_hash(project) == before
    assert (project / publish.SKILLFILE_LOCK_NAME).read_bytes() == lock_before
    assert _run_shim(shim) == "consumer-v1\n"

    # Positive control: the same v2 bytes succeed unfaulted.
    monkeypatch.setattr(shims, "write_bin_shim", original)
    _install_ok(cfg, fetch=True)
    assert _run_shim(shim) == "consumer-v2-live-edit\n"


def test_plan_builds_refuses_package_without_audit(tmp_path: Path) -> None:
    """N3: a packaged provider without an audit hook refuses; the default cannot disable admission."""
    root = tmp_path / "pkg"
    root.mkdir()
    (root / "source.txt").write_text("sources", encoding="utf-8")
    manager_home = tmp_path / "manager"
    manager_home.mkdir()
    package = LocalSnapshot(snapshot="sha256:" + "c" * 64)
    with build_source.freeze_snapshot(root) as frozen:
        provider = build_planner.BuildProvider(
            name="smuggled",
            snapshot=frozen,
            commands=(
                build_planner.BuildCommand(
                    name="greet",
                    driver="go-v1",
                    build_root="build",
                    source_dir="build/cmd/greet",
                ),
            ),
            package=package,
        )
        with pytest.raises(build_planner.BuildPlanningError) as raised:
            build_planner.plan_builds(
                (provider,),
                manager_home=manager_home,
                operator_search_path=build_toolchain.OperatorSearchPath(("/fixture/bin",)),
                audit=None,
            )
    assert raised.value.code == "source_audit_required"
    assert "smuggled" in raised.value.detail


def _external_git_tool() -> git_admission.GitTool:
    executable_text = shutil.which("git")
    assert executable_text is not None
    executable = Path(executable_text).resolve(strict=True)
    version = subprocess.run(
        (os.fspath(executable), "--version"),
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    exec_path = Path(
        subprocess.run(
            (os.fspath(executable), "--exec-path"),
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()
    ).resolve(strict=True)
    return git_admission.GitTool(
        executable=executable,
        exec_path=exec_path,
        allowed_versions=(version,),
        askpass=Path(sys.executable).resolve(strict=True),
    )


def _external_repository_with_marker(tmp_path: Path) -> tuple[Path, str]:
    repository = init_git_repo(tmp_path / "external-tool")
    write_files(
        repository,
        {
            "skill-build.json": json.dumps(
                {
                    "schema_version": 1,
                    "targets": {
                        "external-tool": {
                            "driver": "go-repository-v1",
                            "build_root": ".",
                            "source_dir": "cmd/external-tool",
                        }
                    },
                }
            ),
            "go.mod": "module example.test/external-tool\n\ngo 1.25\n",
            "cmd/external-tool/main.go": "package main\nfunc main() {}\n",
            "cmd/external-tool/marker.txt": "committed-v1\n",
            "README.md": "external tool\n",
        },
    )
    commit = commit_all(repository, "external tool")
    # The operator transfers the source as a bundle and clones it, so admission
    # meets a packed object store, not the loose objects git init leaves here.
    subprocess.run(
        (
            os.fspath(_external_git_tool().executable),
            "-c",
            "repack.updateServerInfo=false",
            "-c",
            "pack.writeReverseIndex=false",
            "repack",
            "-a",
            "-d",
            "--quiet",
        ),
        cwd=repository,
        check=True,
        capture_output=True,
    )
    for child in list((repository / ".git").iterdir()):
        if child.name in {"HEAD", "config", "index", "objects", "refs", "packed-refs"}:
            continue
        if child.is_dir():
            shutil.rmtree(child)
        else:
            child.unlink()
    return repository, commit


@pytest.mark.skipif(
    sys.platform not in {"darwin", "win32"},
    reason="go-repository-v1 is qualified only on macOS and Windows",
)
@pytest.mark.skipif(os.name == "nt", reason="posix-shim-exec: executes POSIX shell shims")
def test_external_build_uses_committed_head_not_dirty_bytes(
    tmp_path: Path, skills_root: Path, csk_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """AC(e): a substituted external repository admits committed HEAD; dirty bytes never enter the snapshot."""
    _stub_frozen_baking_toolchain(monkeypatch)
    tool = _external_git_tool()
    monkeypatch.setattr(installer, "_external_git_tool", lambda *_args, **_kwargs: tool)

    external, commit = _external_repository_with_marker(tmp_path)
    # Dirty the worktree AFTER the commit: uncommitted bytes must never enter
    # the admitted snapshot, even though local path acquisition snapshots dirty bytes.
    (external / "cmd" / "external-tool" / "marker.txt").write_text(
        "dirty-v2-uncommitted\n", encoding="utf-8"
    )
    (external / "UNTRACKED.txt").write_text("untracked\n", encoding="utf-8")

    project = make_project(tmp_path)
    source = tmp_path / "pkgs"
    write_files(
        source / "tooling",
        {
            "SKILL.md": "---\nname: tooling\ndescription: fixture\n---\n\n# tooling\n",
            "agent-skill.json": json.dumps(
                {
                    "schema_version": 7,
                    "capabilities": {},
                    "build_repositories": {
                        "tools": {
                            "git": "https://example.test/external-tool.git",
                            "locked_commit": {"object_format": "sha1", "hex": commit},
                        }
                    },
                    "commands": {
                        "external-tool": {
                            "type": "build",
                            "driver": "go-repository-v1",
                            "repository": "tools",
                            "target": "external-tool",
                        }
                    },
                }
            ),
        },
    )
    _write_skillfile_v2(project, source, [("tooling", "tooling")])
    _write_dev_manifest(
        project,
        {
            "schema_version": 2,
            "substitutions": {},
            "build_repository_substitutions": {"tooling": {"tools": {"path": "../external-tool"}}},
        },
    )
    cfg = _v2_config(csk_home, skills_root, project)
    result = _install_ok(cfg)
    assert any(
        "BUILD REPOSITORY SUBSTITUTION tooling.tools -> path" in message
        for message in result.messages
    )

    marker_raw = (
        project / ".agents" / "skills" / "tooling" / ".csk-install.json"
    ).read_bytes()
    marker = install_marker.read_install_marker(marker_raw)
    assert isinstance(marker, install_marker.InstallMarkerV5)
    record = marker.builds["external-tool"]
    assert record.receipt_schema_version == 3
    assert record.driver == "go-repository-v1"
    assert record.commit == commit
    assert record.substituted is True
    assert record.substitution is not None
    assert record.substitution.type == "local-path"

    shim = project / ".agents" / "bin" / "external-tool"
    assert _run_shim(shim) == "committed-v1\n"

    # The receipt-3 input package is the installing member's locked package.
    from csk.sources import lock as lock_module

    lock = lock_module.read_lock((project / publish.SKILLFILE_LOCK_NAME).read_bytes())
    member = next(item for item in lock.members if item.name == "tooling")
    entry_dir = (
        csk_home
        / "external-builds"
        / "artifacts-v3"
        / record.cache_key.removeprefix("sha256:")
    )
    receipt_path = entry_dir / "receipt.json"
    assert receipt_path.is_file()
    receipt = protocol_json.loads_canonical(receipt_path.read_bytes())
    assert isinstance(receipt, dict)
    receipt_input = receipt["input"]
    assert isinstance(receipt_input, dict)
    assert parse_package_identity(receipt_input["package"]) == member.package


@pytest.mark.parametrize(
    "command_path",
    ["subdir/../evil.sh", "../evil.sh"],
    ids=["nested-dotdot", "leading-dotdot"],
)
def test_dotdot_command_path_refuses(
    tmp_path: Path, skills_root: Path, csk_home: Path, command_path: str
) -> None:
    """S-FS: a script command path that escapes its directory refuses without being resolved."""
    project = make_project(tmp_path)
    source = tmp_path / "pkgs"
    write_files(
        source / "dodgy",
        {
            "SKILL.md": "---\nname: dodgy\ndescription: fixture\n---\n\n# dodgy\n",
            "agent-skill.json": json.dumps(
                {
                    "schema_version": 2,
                    "commands": {"evil": {"type": "script", "unix_path": command_path}},
                }
            ),
            "evil.sh": "#!/bin/sh\necho evil\n",
            "subdir/placeholder.txt": "placeholder\n",
        },
    )
    _write_skillfile_v2(project, source, [("dodgy", "dodgy")])
    cfg = _v2_config(csk_home, skills_root, project)
    errors = _install_failed(cfg)
    assert CODE_MEMBER_INVALID in errors[0]
    assert "must be a relative path inside the skill repository" in errors[0]


@pytest.mark.parametrize("pruned", [".agents", ".git"], ids=["agents", "git"])
def test_command_exported_from_pruned_path_refuses(
    tmp_path: Path, skills_root: Path, csk_home: Path, pruned: str
) -> None:
    """A command exported from a pruned path refuses: pruned bytes never reach the frozen snapshot."""
    project = make_project(tmp_path)
    source = tmp_path / "pkgs"
    write_files(
        source / "sneaky",
        {
            "SKILL.md": "---\nname: sneaky\ndescription: fixture\n---\n\n# sneaky\n",
            "agent-skill.json": json.dumps(
                {
                    "schema_version": 2,
                    "commands": {"evil": {"type": "script", "unix_path": f"{pruned}/evil.sh"}},
                }
            ),
            f"{pruned}/evil.sh": "#!/bin/sh\necho evil\n",
        },
    )
    _write_skillfile_v2(project, source, [("sneaky", "sneaky")])
    cfg = _v2_config(csk_home, skills_root, project)
    errors = _install_failed(cfg)
    assert CODE_MEMBER_INVALID in errors[0]
    assert "source file not found" in errors[0]


@pytest.mark.parametrize(
    "command_path",
    ["subdir/../evil.sh", "../evil.sh", "..", "/abs/path.sh", "a/./b.sh", ""],
    ids=["nested-dotdot", "leading-dotdot", "bare-dotdot", "absolute", "dot", "empty"],
)
def test_script_command_relative_path_refuses_non_portable(command_path: str) -> None:
    """S-FS seam: the staging predicate refuses non-portable command paths without resolving them."""
    command = skillspec.CommandSpec(
        name="evil", type="script", unix_path=command_path, win_path=command_path
    )
    with pytest.raises(SourceError) as raised:
        publish.script_command_relative_path(command, name="dodgy")
    assert raised.value.code == CODE_MEMBER_INVALID


@pytest.mark.skipif(sys.platform == "win32", reason="posix-shim-exec: executes POSIX shell shims")
@pytest.mark.skipif(os.name == "nt", reason="symlink-plant: POSIX symlink fixture needs privilege on Windows")
def test_shim_destination_became_link_refuses_commit(
    tmp_path: Path, skills_root: Path, csk_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """S-TXN: a shim destination that became a link between plan and commit refuses; the link is never followed."""
    project = make_project(tmp_path)
    source = _write_source(tmp_path, runtime_roots=True)
    _write_skillfile_v2(project, source, [("provider", "provider"), ("consumer", "consumer")])
    cfg = _v2_config(csk_home, skills_root, project)
    _install_ok(cfg)
    shim = project / ".agents" / "bin" / "consume"
    assert _run_shim(shim) == "consumer-v1\n"
    pre_plant_shim = shim.read_bytes()
    pre_plant_mode = shim.stat().st_mode

    (source / "consumer" / "scripts" / "consume").write_text(
        CONSUMER_SCRIPT_V2, encoding="utf-8"
    )
    sentinel = tmp_path / "sentinel.txt"
    sentinel.write_text("sentinel-bytes\n", encoding="utf-8")

    real_payloads = publish.build_recheck_payloads

    def plant_then_freeze(*args: Any, **kwargs: Any) -> Any:
        # Interleave in the freeze-to-commit window: payloads already
        # describe the pre-plant live state, so the write-time recheck
        # must refuse the destination that became a link.
        payloads = real_payloads(*args, **kwargs)
        if shim.exists() and not shim.is_symlink():
            shim.unlink()
            try:
                os.symlink(os.fspath(sentinel), os.fspath(shim))
            except OSError:
                pytest.skip("symlink-plant: host cannot create symlinks")
        return payloads

    monkeypatch.setattr(publish, "build_recheck_payloads", plant_then_freeze)
    errors = _install_failed(cfg, fetch=True)
    assert "source_output_overlap" in errors[0]
    assert shim.is_symlink(), "the commit must not follow or replace the planted link"
    assert sentinel.read_bytes() == b"sentinel-bytes\n"

    # The boundary heals: recovery resumes the journaled commit, so the
    # operator restores the exact pre-plant live bytes and refreshes again.
    monkeypatch.setattr(publish, "build_recheck_payloads", real_payloads)
    shim.unlink()
    shim.write_bytes(pre_plant_shim)
    os.chmod(shim, pre_plant_mode)
    _install_ok(cfg, fetch=True)
    assert _run_shim(shim) == "consumer-v2-live-edit\n"
