"""Resolution-time system-command check and offline status closure inspection.

Rev-5 designs for TASK-261002-2fh9m7:

* install: one check at closure resolution, before any persistent write
  (source-store snapshots, store records, builds, adapters, markers);
* status without a lock: offline closure inspection that reports every
  tool it can read in ONE diagnostic plus a "not checked" list for each
  member it cannot read offline, with the remedy ``csk install --dry-run``.

Production call sites: ``installer.install`` -> ``publish.install_schema2``
-> ``publish.resolve_source_closure``; ``status.collect_status`` ->
``publish.evaluate_schema2_installation`` ->
``publish._inspect_status_closure``.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

import pytest
from conftest import make_project, write_files, write_skillfile
from test_source_closure_refresh import (
    _FakeTransport,
    _install_fake_transport,
    _make_repo,
    _v2_config,
)
from test_source_runtime import _require_selection
from test_system_commands_closure import (
    MATRIX_ALPHA,
    MATRIX_BETA,
    _drop_tools_dir,
    _make_status_matrix_tool,
)

from csk import closure, config, installer, status
from csk.source_identity import canonical_source_identity
from csk.sources import publish, store
from csk.sources import transport as source_transport
from csk.sources.package_identity import (
    LockedCommit,
    NetworkGit,
    package_identity_sha256,
)

GAMMA = "__csk_matrix_gamma__"
REV = "a" * 40
URL = "https://example.test/alpha.git"


def _write_pkg(
    directory: Path,
    name: str,
    *,
    tools: tuple[str, ...] = (),
    requirements: dict[str, Any] | None = None,
) -> None:
    """Write one schema-7 package declaring system tools and requirements."""

    dependencies: dict[str, Any] = {}
    if tools:
        dependencies["commands"] = {
            f"dep{index}": {
                "type": "system",
                "command": tool,
                "hint": f"install matrix {tool}",
            }
            for index, tool in enumerate(tools)
        }
    if requirements:
        dependencies["skills"] = requirements
    payload: dict[str, Any] = {"schema_version": 7, "capabilities": {}}
    if dependencies:
        payload["dependencies"] = dependencies
    write_files(
        directory,
        {
            "SKILL.md": f"---\nname: {name}\ndescription: fixture {name}\n---\n\n# {name}\n",
            "agent-skill.json": json.dumps(payload),
        },
    )


def _write_skillfile(
    project: Path, sources: dict[str, Any], selectors: list[dict[str, Any]]
) -> None:
    write_skillfile(
        project, {"schema_version": 2, "sources": sources, "skills": selectors}
    )


def _offline(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """Make any network touch fail loudly; returns the touched list."""

    touched: list[str] = []

    def _boom(*args: Any, **kwargs: Any) -> Any:
        touched.append("network")
        raise AssertionError("status must never touch the network")

    monkeypatch.setattr(source_transport, "resolve_ref", _boom)
    monkeypatch.setattr(source_transport, "acquire_network", _boom)
    return touched


def _status_diagnostic(cfg: config.GlobalConfig) -> str:
    collected = status.collect_status(cfg, alias="app")[0]
    assert not collected.clean
    assert collected.errors
    # ONE diagnostic: every readiness fact lives in a single error string.
    joined = [error for error in collected.errors if MATRIX_ALPHA in error or GAMMA in error]
    assert len(joined) == 1, collected.errors
    return joined[0]


def _seed_git_dependency(
    csk_home: Path, tmp_path: Path, *, tools: tuple[str, ...]
) -> None:
    """Stage a revision-pinned Git dependency into the verified store."""

    materialized = tmp_path / "seeded-alpha"
    _write_pkg(materialized, "alpha", tools=tools)
    captured = publish.capture_transitive_member(
        materialized, name="alpha", home=csk_home
    )
    identity = canonical_source_identity(URL)
    assert identity is not None
    package = NetworkGit(
        repository=identity,
        commit=LockedCommit("sha1", REV),
        directory=".",
    )
    store.stage_snapshot(csk_home, "alpha", package_identity_sha256(package), captured)


def _path_root_requiring_git(
    tmp_path: Path, *, root_tools: tuple[str, ...]
) -> tuple[Path, Path]:
    project = make_project(tmp_path)
    source = tmp_path / "pkgs"
    _write_pkg(
        source / "beta",
        "beta",
        tools=root_tools,
        requirements={"alpha": {"git": URL, "ref": {"kind": "revision", "value": REV}}},
    )
    _write_skillfile(
        project,
        {"local": {"path": os.fspath(source)}},
        [{"name": "beta", "from": "local", "directory": "beta"}],
    )
    return project, source


def test_status_mixed_path_and_uncached_git_reports_path_tools_and_lists_git(
    monkeypatch, tmp_path, skills_root, csk_home
):
    """Panel repro: a Git selector must not erase the readable path member.

    No lock, one path member (two missing tools) and one uncached Git
    source: both path tools are reported and the Git member is listed as
    not checked with the ``csk install --dry-run`` remedy. Killed mutants:
    ``git-short-circuits-manifest`` and ``not-checked-list-dropped``.
    """

    _require_selection()
    touched = _offline(monkeypatch)
    project = make_project(tmp_path)
    source = tmp_path / "pkgs"
    _write_pkg(source / "needy", "needy", tools=(MATRIX_ALPHA, MATRIX_BETA))
    _write_skillfile(
        project,
        {"local": {"path": os.fspath(source)}, "up": {"git": URL, "tag": "v1"}},
        [
            {"name": "needy", "from": "local", "directory": "needy"},
            {"name": "gitty", "from": "up", "directory": "."},
        ],
    )
    cfg = _v2_config(csk_home, skills_root, project)
    config.save_config(cfg)
    monkeypatch.setenv("CSK_CONFIG", str(cfg.path))
    monkeypatch.chdir(project)

    diagnostic = _status_diagnostic(cfg)

    assert MATRIX_ALPHA in diagnostic, diagnostic
    assert MATRIX_BETA in diagnostic, diagnostic
    assert f"install matrix {MATRIX_ALPHA}" in diagnostic, diagnostic
    assert "not checked" in diagnostic, diagnostic
    assert "gitty" in diagnostic, diagnostic
    assert "csk install --dry-run" in diagnostic, diagnostic
    assert touched == []


def test_status_path_root_with_cached_git_dependency_reports_both(
    monkeypatch, tmp_path, skills_root, csk_home
):
    """Panel repro: transitive requirements are followed from the store.

    A path root with a missing tool requires a revision-pinned Git
    dependency that sits in the verified snapshot store and has its own
    missing tool: both are reported, nothing is listed as not checked.
    Killed mutant: ``transitive-members-skipped``.
    """

    _require_selection()
    touched = _offline(monkeypatch)
    project, _source = _path_root_requiring_git(tmp_path, root_tools=(MATRIX_ALPHA,))
    _seed_git_dependency(csk_home, tmp_path, tools=(GAMMA,))
    cfg = _v2_config(csk_home, skills_root, project)
    config.save_config(cfg)
    monkeypatch.setenv("CSK_CONFIG", str(cfg.path))
    monkeypatch.chdir(project)

    diagnostic = _status_diagnostic(cfg)

    assert MATRIX_ALPHA in diagnostic, diagnostic
    assert GAMMA in diagnostic, diagnostic
    assert "alpha" in diagnostic, diagnostic
    assert "not checked" not in diagnostic, diagnostic
    assert touched == []


def test_status_path_root_with_uncached_git_dependency_lists_dependency(
    monkeypatch, tmp_path, skills_root, csk_home
):
    """Panel repro: an unreadable transitive member is named, not hidden.

    The root's tool is reported; the uncached Git dependency is listed
    as not checked with the remedy. Killed mutants:
    ``transitive-members-skipped`` (dependency vanishes from the list)
    and ``not-checked-list-dropped``.
    """

    _require_selection()
    touched = _offline(monkeypatch)
    project, _source = _path_root_requiring_git(tmp_path, root_tools=(MATRIX_ALPHA,))
    cfg = _v2_config(csk_home, skills_root, project)
    config.save_config(cfg)
    monkeypatch.setenv("CSK_CONFIG", str(cfg.path))
    monkeypatch.chdir(project)

    diagnostic = _status_diagnostic(cfg)

    assert MATRIX_ALPHA in diagnostic, diagnostic
    assert "not checked" in diagnostic, diagnostic
    assert "alpha (required by beta)" in diagnostic, diagnostic
    assert "csk install --dry-run" in diagnostic, diagnostic
    assert touched == []


def test_status_without_missing_tools_still_lists_unread_members(
    monkeypatch, tmp_path, skills_root, csk_home
):
    """No return path omits both lists: not-checked alone is still reported."""

    _require_selection()
    _offline(monkeypatch)
    project, _source = _path_root_requiring_git(tmp_path, root_tools=())
    cfg = _v2_config(csk_home, skills_root, project)
    config.save_config(cfg)
    monkeypatch.setenv("CSK_CONFIG", str(cfg.path))
    monkeypatch.chdir(project)

    collected = status.collect_status(cfg, alias="app")[0]

    assert not collected.clean
    assert any(
        "not checked" in error and "alpha (required by beta)" in error
        for error in collected.errors
    ), collected.errors


def _fake_git_project(
    monkeypatch, tmp_path: Path, csk_home: Path, skills_root: Path, *, git_tools, path_tools
):
    """Project with a Git root (alpha) and a path root (beta) requiring alpha."""

    lib = tmp_path / "librepo"
    _write_pkg(lib, "alpha", tools=git_tools)
    oids = _make_repo(lib, [{}])
    fake = _FakeTransport()
    fake.add(URL, lib, "example.test/alpha")
    _install_fake_transport(monkeypatch, fake)
    project = make_project(tmp_path)
    source = tmp_path / "pkgs"
    _write_pkg(
        source / "beta",
        "beta",
        tools=path_tools,
        requirements={"alpha": {"git": URL, "ref": {"kind": "revision", "value": oids[0]}}},
    )
    _write_skillfile(
        project,
        {"up": {"git": URL, "tag": "v1"}, "local": {"path": os.fspath(source)}},
        [
            {"name": "alpha", "from": "up", "directory": "."},
            {"name": "beta", "from": "local", "directory": "beta"},
        ],
    )
    return project, lib, oids


def test_status_git_root_and_transitive_serve_from_store_with_lock(
    monkeypatch, tmp_path, skills_root, csk_home
):
    """With a lock, Git roots and transitive members read from the store."""

    from conftest import run

    _require_selection()
    tools = tmp_path / "tools-bin"
    for tool in (MATRIX_ALPHA, GAMMA):
        _make_status_matrix_tool(tools, tool)
    monkeypatch.setenv("PATH", str(tools) + os.pathsep + os.environ.get("PATH", ""))
    lib = tmp_path / "librepo"
    _write_pkg(lib, "alpha", tools=(GAMMA,))
    oids = _make_repo(lib, [{}])
    run(["git", "tag", "v1"], lib)
    fake = _FakeTransport()
    fake.add(URL, lib, "example.test/alpha")
    _install_fake_transport(monkeypatch, fake)
    project = make_project(tmp_path)
    source = tmp_path / "pkgs"
    _write_pkg(
        source / "beta",
        "beta",
        tools=(MATRIX_ALPHA,),
        requirements={"alpha": {"git": URL, "ref": {"kind": "revision", "value": oids[0]}}},
    )
    _write_skillfile(
        project,
        {"up": {"git": URL, "tag": "v1"}, "local": {"path": os.fspath(source)}},
        [
            {"name": "alpha", "from": "up", "directory": "."},
            {"name": "beta", "from": "local", "directory": "beta"},
        ],
    )
    cfg = _v2_config(csk_home, skills_root, project)
    config.save_config(cfg)
    monkeypatch.setenv("CSK_CONFIG", str(cfg.path))
    monkeypatch.chdir(project)
    installed = installer.install(cfg, alias="app")[0]
    assert installed.status == "ok", installed.errors

    _drop_tools_dir(monkeypatch, tools)
    touched = _offline(monkeypatch)
    diagnostic = _status_diagnostic(cfg)

    assert MATRIX_ALPHA in diagnostic, diagnostic
    assert GAMMA in diagnostic, diagnostic
    assert "not checked" not in diagnostic, diagnostic
    assert touched == []


def test_install_refuses_missing_closure_commands_before_any_store_write(
    monkeypatch, tmp_path, skills_root, csk_home
):
    """The aggregate check runs at resolution, before snapshot staging.

    Root and transitive Git dependency each miss a tool: one diagnostic
    names both, the production store stage hook is never called, and the
    manager home holds no snapshot or record. Positive control: with the
    tools present the same project installs and ``check`` precedes the
    first ``stage``. Killed mutant: ``check-after-snapshot-staging``.
    """

    _require_selection()
    events: list[str] = []
    real_check = closure.collect_missing_system_commands
    real_stage = publish.stage_schema2_store
    real_hook = publish.make_publication_hook

    def traced_check(named_specs):
        events.append("check")
        return real_check(named_specs)

    def traced_stage(home, captured):
        events.append("stage-store")
        return real_stage(home, captured)

    def traced_hook():
        events.append("publish-hook")
        return real_hook()

    monkeypatch.setattr(closure, "collect_missing_system_commands", traced_check)
    monkeypatch.setattr(publish, "stage_schema2_store", traced_stage)
    monkeypatch.setattr(publish, "make_publication_hook", traced_hook)

    lib = tmp_path / "librepo"
    _write_pkg(lib, "alpha", tools=(GAMMA,))
    oids = _make_repo(lib, [{}])
    fake = _FakeTransport()
    fake.add(URL, lib, "example.test/alpha")
    _install_fake_transport(monkeypatch, fake)
    project = make_project(tmp_path)
    source = tmp_path / "pkgs"
    _write_pkg(
        source / "beta",
        "beta",
        tools=(MATRIX_ALPHA,),
        requirements={"alpha": {"git": URL, "ref": {"kind": "revision", "value": oids[0]}}},
    )
    _write_skillfile(
        project,
        {"local": {"path": os.fspath(source)}},
        [{"name": "beta", "from": "local", "directory": "beta"}],
    )
    cfg = _v2_config(csk_home, skills_root, project)
    config.save_config(cfg)
    monkeypatch.setenv("CSK_CONFIG", str(cfg.path))
    monkeypatch.chdir(project)

    refused = installer.install(cfg, alias="app")[0]

    assert refused.status == "failed"
    assert len(refused.errors) == 1
    assert MATRIX_ALPHA in refused.errors[0], refused.errors
    assert GAMMA in refused.errors[0], refused.errors
    assert "check" in events
    assert "stage-store" not in events, events
    assert "publish-hook" not in events, events
    store_dir = store.store_root(csk_home)
    assert not store_dir.exists() or not any(store_dir.rglob("*.json")), list(
        store_dir.rglob("*")
    )

    tools = tmp_path / "ok-bin"
    for tool in (MATRIX_ALPHA, GAMMA):
        _make_status_matrix_tool(tools, tool)
    monkeypatch.setenv("PATH", str(tools) + os.pathsep + os.environ.get("PATH", ""))
    events.clear()
    installed = installer.install(cfg, alias="app")[0]
    assert installed.status == "ok", installed.errors
    assert "stage-store" in events
    assert events.index("check") < events.index("stage-store"), events
    assert events.index("check") < events.index("publish-hook"), events
