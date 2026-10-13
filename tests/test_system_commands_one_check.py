"""Exactly one aggregate system-command check per install, before publication.

Rev-6 contract for TASK-261002-2fh9m7 (install side): on every install path
(schema 1 and 2, project and global, install, dry-run and upgrade) the
aggregate ``closure.collect_missing_system_commands`` check runs exactly once
and before the first publication event (store stage, publication hook, build
publication, materialization commit).

Boundary stated once: acquisition caches populated while reading manifests
during resolution (the schema-1 snapshot cache: content addressed, immutable,
garbage-collected) are acquisition; install publication (store records,
builds, adapters, markers, runtime, locks) happens only after the check.

Production call sites: ``installer.install`` -> ``closure.build_closure``
(schema 1), ``global_install.install`` -> ``closure.build_closure``,
``installer.install`` -> ``publish.install_schema2`` ->
``publish.resolve_source_closure`` (schema 2 resolving) and
``publish._plan_and_publish_schema2`` (schema 2 frozen).

Killed mutants (see the ``test_mutant_*`` tests): a second check re-added
after staging (schema 2), the schema-1 dependency gate re-checking system
commands, the global dependency filter re-checking, and the frozen lane
dropping its single check.
"""

from __future__ import annotations

import os
from dataclasses import replace

import pytest
from conftest import make_config, make_project, write_skillfile
from test_system_commands_closure import (
    MATRIX_ALPHA,
    MATRIX_BETA,
    _drop_tools_dir,
    _install_v1_ok,
    _install_v2_ok,
    _make_status_matrix_tool,
    _save_config,
    _write_global_skillfile,
    _write_matrix_skill_repos,
    _write_needy_v2_source,
)

from csk import closure, config, global_install, installer
from csk.sources import publish

_PUBLICATION_HOOKS = (
    (installer, "_publish_external_builds", "publish-external-builds"),
    (installer, "_publish_planned_builds", "publish-planned-builds"),
    (installer, "_commit_materialization", "publish-materialization"),
    (installer, "_commit_transaction_targets", "publish-transaction"),
    (publish, "stage_schema2_store", "publish-store-stage"),
    (publish, "make_publication_hook", "publish-hook"),
)


def _trace(monkeypatch) -> list[str]:
    """Record ``check`` and every publication event, in call order."""

    events: list[str] = []
    real_check = closure.collect_missing_system_commands

    def traced_check(named_specs):
        events.append("check")
        return real_check(named_specs)

    monkeypatch.setattr(closure, "collect_missing_system_commands", traced_check)
    for owner, attr, label in _PUBLICATION_HOOKS:
        real = getattr(owner, attr)

        def traced(*args, _real=real, _label=label, **kwargs):
            events.append(_label)
            return _real(*args, **kwargs)

        monkeypatch.setattr(owner, attr, traced)
    return events


def _assert_one_check_first(events: list[str], *, publishes: bool | None) -> None:
    """The invariant: one check, ahead of every publication event."""

    assert events.count("check") == 1, events
    first = events.index("check")
    publication = [i for i, event in enumerate(events) if event != "check"]
    assert all(i > first for i in publication), events
    if publishes is True:
        assert publication, events
    elif publishes is False:
        assert not publication, events


def _check_then_clear(events: list[str], *, publishes: bool | None) -> None:
    _assert_one_check_first(events, publishes=publishes)
    events.clear()


def _remove_tools(monkeypatch, tools) -> None:
    _drop_tools_dir(monkeypatch, tools)


def test_schema1_project_one_check_on_every_install_path(
    monkeypatch, tmp_path, skills_root, csk_home
):
    events = _trace(monkeypatch)
    _project, cfg, tools = _install_v1_ok(
        monkeypatch, tmp_path, csk_home, skills_root, scope="project"
    )
    _check_then_clear(events, publishes=True)

    assert installer.install(cfg)[0].status == "ok"
    _check_then_clear(events, publishes=None)

    dry = installer.install(cfg, options=installer.InstallOptions(dry_run=True))[0]
    assert dry.status == "ok", dry.errors
    _check_then_clear(events, publishes=None)

    upgrade = installer.install(cfg, options=installer.InstallOptions(fetch=True))[0]
    assert upgrade.status == "ok", upgrade.errors
    _check_then_clear(events, publishes=None)

    _remove_tools(monkeypatch, tools)
    refused = installer.install(cfg)[0]
    assert refused.status == "failed"
    assert events == ["check"], events


def test_schema1_global_one_check_on_every_install_path(
    monkeypatch, tmp_path, skills_root, csk_home
):
    events = _trace(monkeypatch)
    _project, cfg, tools = _install_v1_ok(
        monkeypatch, tmp_path, csk_home, skills_root, scope="global"
    )
    _check_then_clear(events, publishes=True)

    assert global_install.install(cfg).status == "ok"
    _check_then_clear(events, publishes=None)

    dry = global_install.install(
        cfg, options=installer.InstallOptions(dry_run=True)
    )
    assert dry.status == "ok", dry.errors
    _check_then_clear(events, publishes=None)

    upgrade = global_install.install(
        cfg, options=installer.InstallOptions(fetch=True)
    )
    assert upgrade.status == "ok", upgrade.errors
    _check_then_clear(events, publishes=None)

    _remove_tools(monkeypatch, tools)
    refused = global_install.install(cfg)
    assert refused.status == "failed"
    assert events == ["check"], events


def test_schema2_project_one_check_on_every_install_path(
    monkeypatch, tmp_path, skills_root, csk_home
):
    events = _trace(monkeypatch)
    _project, cfg, _source, tools = _install_v2_ok(
        monkeypatch, tmp_path, csk_home, skills_root
    )
    # Initial install: resolving lane, one check at resolution.
    _check_then_clear(events, publishes=True)

    # Reinstall with a lock and no fetch: frozen lane, its one check.
    assert installer.install(cfg, alias="app")[0].status == "ok"
    _check_then_clear(events, publishes=None)

    dry = installer.install(
        cfg, alias="app", options=installer.InstallOptions(dry_run=True)
    )[0]
    assert dry.status == "ok", dry.errors
    _check_then_clear(events, publishes=None)

    # Upgrade: resolving lane again.
    upgrade = installer.install(
        cfg, alias="app", options=installer.InstallOptions(fetch=True)
    )[0]
    assert upgrade.status == "ok", upgrade.errors
    _check_then_clear(events, publishes=None)

    _remove_tools(monkeypatch, tools)
    frozen_refused = installer.install(cfg, alias="app")[0]
    assert frozen_refused.status == "failed"
    assert events == ["check"], events
    events.clear()

    upgrade_refused = installer.install(
        cfg, alias="app", options=installer.InstallOptions(fetch=True)
    )[0]
    assert upgrade_refused.status == "failed"
    assert events == ["check"], events


def test_schema2_initial_resolving_refusal_runs_one_check_and_publishes_nothing(
    monkeypatch, tmp_path, skills_root, csk_home
):
    from test_source_runtime import (
        _require_selection,
        _v2_config,
        _write_skillfile_v2,
    )

    _require_selection()
    events = _trace(monkeypatch)
    project = make_project(tmp_path)
    source = tmp_path / "pkgs"
    _write_needy_v2_source(source)
    _write_skillfile_v2(project, source, [("needy", "needy")])
    cfg = _v2_config(csk_home, skills_root, project)
    config.save_config(cfg)
    monkeypatch.setenv("CSK_CONFIG", str(cfg.path))
    monkeypatch.chdir(project)

    refused = installer.install(cfg, alias="app")[0]

    assert refused.status == "failed"
    assert MATRIX_ALPHA in refused.errors[0] and MATRIX_BETA in refused.errors[0]
    assert events == ["check"], events


def _v2_initial_install_events(monkeypatch, tmp_path, skills_root, csk_home):
    events = _trace(monkeypatch)
    _install_v2_ok(monkeypatch, tmp_path, csk_home, skills_root)
    return events


def test_mutant_second_check_after_staging_is_killed(
    monkeypatch, tmp_path, skills_root, csk_home
):
    """Mutant: the schema-2 gate set re-checks system commands after staging.

    Narrows the invariant to "a check somewhere before publication": the
    mutated install still refuses and still checks before the pipeline, but
    the exactly-one-check assertion fails (two ``check`` events, the second
    after ``publish-store-stage``).
    """

    real_gates = publish.run_schema2_command_gates

    def mutated(members, specs_map):
        real_gates(members, specs_map)
        publish.check_schema2_system_commands(members, specs_map)

    monkeypatch.setattr(publish, "run_schema2_command_gates", mutated)
    events = _v2_initial_install_events(monkeypatch, tmp_path, skills_root, csk_home)

    assert events.count("check") == 2, events
    assert events.index("publish-store-stage") < len(events) - 1
    with pytest.raises(AssertionError):
        _assert_one_check_first(events, publishes=True)


def test_mutant_schema1_dependency_gate_rechecks_system_commands_is_killed(
    monkeypatch, tmp_path, skills_root, csk_home
):
    """Mutant: the schema-1 dependency gate runs the system check again."""

    real = installer._check_dependencies

    def mutated(plans):
        closure.collect_missing_system_commands(
            [(plan.decl.name, plan.spec) for plan in plans]
        )
        return real(plans)

    monkeypatch.setattr(installer, "_check_dependencies", mutated)
    events = _trace(monkeypatch)
    _install_v1_ok(monkeypatch, tmp_path, csk_home, skills_root, scope="project")

    assert events.count("check") == 2, events
    with pytest.raises(AssertionError):
        _assert_one_check_first(events, publishes=True)


def test_mutant_global_dependency_filter_rechecks_system_commands_is_killed(
    monkeypatch, tmp_path, skills_root, csk_home
):
    """Mutant: the global per-plan dependency filter checks system commands."""

    real = global_install._plans_with_available_dependencies

    def mutated(plans, result):
        for plan in plans:
            closure.collect_missing_system_commands([(plan.decl.name, plan.spec)])
        return real(plans, result)

    monkeypatch.setattr(global_install, "_plans_with_available_dependencies", mutated)
    events = _trace(monkeypatch)
    _install_v1_ok(monkeypatch, tmp_path, csk_home, skills_root, scope="global")

    assert events.count("check") >= 2, events
    with pytest.raises(AssertionError):
        _assert_one_check_first(events, publishes=True)


def test_mutant_frozen_lane_without_its_check_is_killed(
    monkeypatch, tmp_path, skills_root, csk_home
):
    """Mutant: the frozen lane drops its single check (check present, uncalled).

    With the tools gone, a frozen reinstall must refuse. Without the check it
    publishes over the missing tools, so the refusal assertions fail.
    """

    _project, cfg, _source, tools = _install_v2_ok(
        monkeypatch, tmp_path, csk_home, skills_root
    )
    _remove_tools(monkeypatch, tools)
    monkeypatch.setattr(
        publish, "check_schema2_system_commands", lambda members, specs_map: None
    )
    events = _trace(monkeypatch)

    result = installer.install(cfg, alias="app")[0]

    assert result.status == "ok", "the mutant must install over missing tools"
    assert "check" not in events, events
    with pytest.raises(AssertionError):
        _assert_one_check_first(events, publishes=None)
