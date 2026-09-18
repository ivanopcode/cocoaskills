"""Atomic publication of schema-2 source installs (TASK-260916-17x3o1).

Covers the one-transaction publication of lock, marker v5, runtime
store entries, context and adapters for schema-2 path-source installs
through the production entry points ``csk.installer.install`` and
``csk.status.collect_status`` (plus ``csk.cli.main`` for ``status
--check`` exit codes). Instruments: before/after tree digests built
from ``csk.transactions.digest_target``/``digest_path`` (never another
hasher), fault injection at real call sites with positive controls and
reached-assertions, and a ``sys.addaudithook`` confinement property.

Row ownership from the attack-surface catalog: S-TXN (the whole file),
S-CONFLICT (member-name destination conflation), S-FS where the
publication-time recheck touches the filesystem, and S-ERRORS (the
publisher seams).
"""

from __future__ import annotations

import json
import os
import stat
import sys
import tempfile
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest
from conftest import commit_all, make_config, make_project, write_files, write_skillfile

from csk import cli, config, install_marker, installer, manifest, status
from csk.sources import _selection_fs
from csk.sources import lock as lock_module
from csk.sources import publish, skillfile_v2
from csk.sources.errors import SourceError
from csk.sources.package_identity import LocalSnapshot, package_identity_sha256
from csk.transactions import (
    ABSENT_DIGEST,
    JournalTarget,
    TransactionEngine,
    TransactionError,
    digest_path,
    digest_target,
)

pytestmark = pytest.mark.usefixtures("stable_env")


# ---------------------------------------------------------------------------
# Fixtures and helpers
# ---------------------------------------------------------------------------


def _write_skill(
    directory: Path,
    name: str,
    *,
    script: str = "#!/bin/sh\necho hi\n",
    runtime_roots: bool = True,
    agent_manifest: dict[str, Any] | None = None,
    extra: dict[str, str | bytes] | None = None,
) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "SKILL.md").write_text(
        f"---\nname: {name}\ndescription: fixture skill {name}\n---\n\n# {name}\n",
        encoding="utf-8",
    )
    scripts = directory / "scripts"
    scripts.mkdir(exist_ok=True)
    (scripts / "tool.sh").write_text(script, encoding="utf-8")
    if agent_manifest is not None:
        payload: dict[str, Any] | None = agent_manifest
    elif runtime_roots:
        payload = {"schema_version": 2, "runtime_roots": ["scripts"]}
    else:
        payload = None
    if payload is not None:
        (directory / "agent-skill.json").write_text(
            json.dumps(payload), encoding="utf-8"
        )
    if extra:
        write_files(directory, extra)
    return directory


def _write_manifest_v2(
    project: Path,
    skills: list[tuple[str, str]],
    *,
    sources: dict[str, Any] | None = None,
    agents: list[str] | None = None,
) -> dict[str, Any]:
    doc: dict[str, Any] = {
        "schema_version": 2,
        "sources": sources if sources is not None else {"local": {"path": "."}},
        "skills": [
            {"name": name, "from": "local", "directory": directory}
            for name, directory in skills
        ],
    }
    if agents is not None:
        doc["agents"] = agents
    write_skillfile(project, doc)
    return doc


def _v2_config(
    csk_home: Path,
    skills_root: Path,
    project: Path,
    *,
    agents: list[str] | None = None,
    adapter_mode: str = "auto",
) -> config.GlobalConfig:
    base = make_config(
        csk_home, skills_root, project, agents=agents or ["codex_cli"]
    )
    return replace(
        base,
        adapter_mode=adapter_mode,
        experimental=config.ExperimentalConfig(skillfile_sources=True),
    )


def _install(
    cfg: config.GlobalConfig, alias: str = "app", **options: Any
) -> installer.ProjectResult:
    results = installer.install(
        cfg, alias=alias, options=installer.InstallOptions(**options)
    )
    assert len(results) == 1
    return results[0]


def _install_ok(
    cfg: config.GlobalConfig, alias: str = "app", **options: Any
) -> installer.ProjectResult:
    result = _install(cfg, alias, **options)
    assert result.status == "ok", result.errors
    return result


def _install_failed(
    cfg: config.GlobalConfig, code: str, alias: str = "app", **options: Any
) -> installer.ProjectResult:
    result = _install(cfg, alias, **options)
    assert result.status == "failed", result.messages
    assert result.errors, "failed install must carry a diagnostic"
    assert result.errors[0].startswith(code + ":"), result.errors
    return result


def _collect(
    cfg: config.GlobalConfig, alias: str = "app"
) -> status.ProjectStatus:
    collected = status.collect_status(cfg, alias=alias)
    assert len(collected) == 1
    return collected[0]


def _read_lock(project: Path) -> lock_module.SkillfileLock | None:
    path = project / publish.SKILLFILE_LOCK_NAME
    if not path.exists() or path.is_symlink():
        return None
    return lock_module.read_lock(path.read_bytes())


def _published_digests(
    project: Path,
    home: Path,
    names: list[str],
) -> dict[str, str]:
    """Digest the published set with the engine digesters only.

    Covers the lock, every named member context, every live runtime
    entry, binding, adapter mirror and ledger, plus the whole
    source-v1 home namespace. Adapter links digest by readlink
    spelling; missing paths digest as absent. Engine-owned sidecar
    temporaries (``.csk-txn-*``) are excluded from the per-entry rows
    but remain inside the whole-tree ``home-source-v1`` row; they get
    explicit presence/absence assertions instead. Bindings carry
    absolute machine paths, so cross-fixture comparison must exclude
    the ``binding:`` rows.
    """

    out: dict[str, str] = {}
    out["lock"] = digest_target(project / publish.SKILLFILE_LOCK_NAME, kind="bytes")
    for name in sorted(names):
        out[f"context:{name}"] = digest_target(
            project / ".agents" / "skills" / name, kind="entry"
        )
    runtime_root = home / "source-v1" / "runtime"
    if runtime_root.is_dir() and not runtime_root.is_symlink():
        for skill_hex in sorted(runtime_root.iterdir(), key=lambda p: p.name):
            if _is_engine_temporary(skill_hex.name):
                continue
            if not skill_hex.is_dir() or skill_hex.is_symlink():
                continue
            for package_hex in sorted(skill_hex.iterdir(), key=lambda p: p.name):
                if _is_engine_temporary(package_hex.name):
                    continue
                out[f"runtime:{skill_hex.name}/{package_hex.name}"] = digest_target(
                    package_hex, kind="entry"
                )
    bindings_root = home / "source-v1" / "bindings"
    if bindings_root.is_dir() and not bindings_root.is_symlink():
        for slug_dir in sorted(bindings_root.iterdir(), key=lambda p: p.name):
            if _is_engine_temporary(slug_dir.name):
                continue
            if not slug_dir.is_dir() or slug_dir.is_symlink():
                continue
            for binding in sorted(slug_dir.iterdir(), key=lambda p: p.name):
                if _is_engine_temporary(binding.name):
                    continue
                out[f"binding:{slug_dir.name}/{binding.name}"] = digest_target(
                    binding, kind="bytes"
                )
    adapter_roots = [
        project / ".agents" / "skills",
        project / ".codex" / "skills",
        project / ".claude" / "skills",
        project / ".gemini" / "skills",
        project / ".cursor" / "rules",
    ]
    for root in adapter_roots:
        try:
            info = root.lstat()
        except FileNotFoundError:
            continue
        if not stat.S_ISDIR(info.st_mode) or stat.S_ISLNK(info.st_mode):
            out[f"adapter-root:{root.name}"] = digest_target(root, kind="entry")
            continue
        for child in sorted(root.iterdir(), key=lambda p: p.name):
            if _is_engine_temporary(child.name):
                continue
            out[f"adapter:{root.parent.name}/{root.name}/{child.name}"] = (
                digest_target(child, kind="entry")
            )
    out["home-source-v1"] = digest_path(home / "source-v1")
    return out


def _source_snapshots(
    project: Path, members: list[tuple[str, str]], home: Path
) -> dict[str, str]:
    """Re-capture member digests to prove sources byte-identical."""

    from csk.sources import snapshot as snapshot_module

    return {
        name: snapshot_module.capture_package_snapshot(
            project, directory, home=home
        ).inventory["snapshot"]
        for name, directory in members
    }


def _expected_store_entries(
    project: Path, members: list[tuple[str, str]], home: Path
) -> dict[Path, tuple[str, str, str]]:
    """Map every fixture entry dir to (skill, package key, snapshot)."""

    from csk.sources import snapshot as snapshot_module
    from csk.sources import store as store_module

    expected: dict[Path, tuple[str, str, str]] = {}
    for name, directory in members:
        captured = snapshot_module.capture_package_snapshot(
            project, directory, home=home
        )
        snapshot_digest = captured.inventory["snapshot"]
        package = LocalSnapshot(snapshot=snapshot_digest)
        key = package_identity_sha256(package)
        expected[store_module.entry_dir(home, name, key)] = (
            name,
            key,
            snapshot_digest,
        )
    return expected


def _assert_store_residue_verifies(
    home: Path,
    expected: dict[Path, tuple[str, str, str]],
    *,
    exact: bool = False,
) -> None:
    """Assert store residue is a verified subset of the fixture entries.

    Snapshot staging runs before the transaction, so any fault after
    staging leaves complete staged entries behind. Staging is atomic
    per key: every entry dir present must be an expected fixture entry
    and must serve back verified bytes; no staging scratch may remain.
    ``exact`` additionally requires every expected entry to be present,
    for faults known to run after staging completed.
    """

    from csk.sources import store as store_module

    root = store_module.store_root(home)
    staging = root / store_module.STAGING_DIRNAME
    if staging.is_symlink() or (staging.exists() and not staging.is_dir()):
        raise AssertionError(f"staging scratch is not a directory: {staging}")
    if staging.is_dir():
        leftovers = sorted(p.name for p in staging.iterdir())
        assert leftovers == [], f"staging scratch remains: {leftovers}"
    snapshots_root = root / store_module.SNAPSHOTS_DIRNAME
    present: list[Path] = []
    if not snapshots_root.is_symlink() and snapshots_root.is_dir():
        for skill_dir in sorted(snapshots_root.iterdir(), key=lambda p: p.name):
            if skill_dir.is_symlink() or not skill_dir.is_dir():
                raise AssertionError(f"unexpected store child: {skill_dir}")
            for entry in sorted(skill_dir.iterdir(), key=lambda p: p.name):
                if entry.is_symlink() or not entry.is_dir():
                    raise AssertionError(f"unexpected store leaf: {entry}")
                present.append(entry)
    for entry in present:
        assert entry in expected, f"foreign store residue: {entry}"
    if exact:
        missing = sorted(str(path) for path in expected if path not in present)
        assert missing == [], f"expected store entries missing: {missing}"
    for entry in present:
        name, key, snapshot_digest = expected[entry]
        stored = store_module.lookup_snapshot(home, name, key)
        assert stored.snapshot == snapshot_digest


def _published_without_store(digests: dict[str, str]) -> dict[str, str]:
    """Drop the store-namespace row for residue-tolerant comparison."""

    return {key: value for key, value in digests.items() if key != "home-source-v1"}


def _is_engine_temporary(name: str) -> bool:
    """Decide whether a filename is an engine-owned sidecar temporary."""

    return name.startswith(".csk-txn-")


def _engine_temporaries(root: Path) -> list[Path]:
    """Collect engine-owned sidecar temporaries under one root."""

    if root.is_symlink() or not root.is_dir():
        return []
    found: list[Path] = []
    for path in root.rglob("*"):
        if ".git" in path.parts:
            continue
        if _is_engine_temporary(path.name):
            found.append(path)
    return sorted(found)


def _assert_no_journal(home: Path) -> None:
    journal_root = home / "state" / "transactions" / "v1"
    if not journal_root.exists():
        return
    leftovers = sorted(path.name for path in journal_root.iterdir())
    assert leftovers == [], f"journal residue remains: {leftovers}"


def _journal_texts(home: Path) -> list[Path]:
    journal_root = home / "state" / "transactions" / "v1"
    if not journal_root.exists():
        return []
    # Both live journals (``*.json``) and removal tombs
    # (``*.json.delete``) count: a fault between the tomb rename and the
    # final unlink leaves only the tomb behind.
    return sorted(
        (
            path
            for path in journal_root.iterdir()
            if path.name.endswith(".json") or path.name.endswith(".json.delete")
        ),
        key=lambda path: path.name,
    )


class _EngineFault:
    """Inject engine fault-hook behavior into publish.install_schema2."""

    def __init__(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self.calls: list[tuple[str, str | None, str | None]] = []
        self._real_engine = publish.TransactionEngine
        self._point: str | None = None
        self._action = None
        self._when = None
        self._armed = True
        monkeypatch.setattr(publish, "TransactionEngine", self._factory)

    def _factory(self, home: Path, **kwargs: Any) -> TransactionEngine:
        assert "fault_hook" not in kwargs or kwargs["fault_hook"] is None
        hook = kwargs.get("pre_write_hook")

        def combined(point: str, target: JournalTarget | None) -> None:
            self.calls.append(
                (
                    point,
                    target.target_class if target is not None else None,
                    target.identifier if target is not None else None,
                )
            )
            if self._armed and self._point is not None and point == self._point:
                if self._when is None or self._when(target):
                    self._armed = False
                    assert self._action is not None
                    self._action(point, target)

        return self._real_engine(home, fault_hook=combined, pre_write_hook=hook)

    def fail_once_at(
        self, point: str, error: BaseException, *, when: Any = None
    ) -> None:
        self._point = point
        self._when = when

        def action(_point: str, _target: JournalTarget | None) -> None:
            raise error

        self._action = action

    def mutate_once_at(
        self, point: str, action: Any, *, when: Any = None
    ) -> None:
        self._point = point
        self._action = action
        self._when = when

    def fired(self, point: str) -> bool:
        return any(call[0] == point for call in self.calls)


def _basic_fixture(
    tmp_path: Path, csk_home: Path, name: str = "review"
) -> tuple[Path, config.GlobalConfig, list[tuple[str, str]]]:
    """One project with one nested path-source skill, uncommitted Skillfile."""

    if not _selection_fs.supports_descriptor_traversal():
        pytest.skip(_selection_fs.NO_DESCRIPTOR_TRAVERSAL_REASON)
    skills_root = tmp_path / "skills"
    project = make_project(tmp_path)
    members = [(name, f"agents/skills/{name}")]
    _write_skill(project / "agents" / "skills" / name, name)
    _write_manifest_v2(project, members)
    commit_all(project, "v2 fixture")
    return project, _v2_config(csk_home, skills_root, project), members


def _collection_fixture(
    tmp_path: Path, csk_home: Path
) -> tuple[Path, config.GlobalConfig, list[tuple[str, str]]]:
    """One individual plus a two-member collection for the fault matrix."""

    if not _selection_fs.supports_descriptor_traversal():
        pytest.skip(_selection_fs.NO_DESCRIPTOR_TRAVERSAL_REASON)
    skills_root = tmp_path / "skills"
    project = make_project(tmp_path)
    _write_skill(project / "agents" / "skills" / "review", "review")
    _write_skill(project / "agents" / "skills" / "team" / "alpha", "alpha")
    _write_skill(project / "agents" / "skills" / "team" / "beta", "beta")
    doc: dict[str, Any] = {
        "schema_version": 2,
        "sources": {"local": {"path": "."}},
        "skills": [
            {"name": "review", "from": "local", "directory": "agents/skills/review"},
            {
                "from": "local",
                "directory": "agents/skills/team",
                "include": ["*"],
            },
        ],
    }
    write_skillfile(project, doc)
    commit_all(project, "v2 matrix fixture")
    members = [
        ("review", "agents/skills/review"),
        ("alpha", "agents/skills/team/alpha"),
        ("beta", "agents/skills/team/beta"),
    ]
    return project, _v2_config(csk_home, skills_root, project), members


# ---------------------------------------------------------------------------
# Stage-boundary enumeration (pinned before the fault matrix)
# ---------------------------------------------------------------------------


def test_stage_boundaries_are_pinned() -> None:
    assert publish.STAGE_BOUNDARIES == (
        "selection",
        "capture",
        "re-enumeration",
        "snapshot-store",
        "lock-create",
        "prepare",
        "commit-05-bindings",
        "commit-07-lock",
        "commit-10-context",
        "commit-20-runtime",
        "commit-60-adapter-ledger",
        "commit-80-removal",
        "cleanup",
    )


def test_every_journal_target_class_maps_to_an_enumerated_boundary(
    tmp_path: Path, csk_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A real install journals only enumerated commit-stage classes."""

    project, cfg, _members = _basic_fixture(tmp_path, csk_home)
    fault = _EngineFault(monkeypatch)
    fault.fail_once_at("before_cleanup", RuntimeError("hold the journal"))
    result = _install(cfg)
    assert result.status == "failed"
    journals = _journal_texts(csk_home)
    assert len(journals) == 1
    journal = json.loads(journals[0].read_text(encoding="utf-8"))
    classes = {target["target_class"] for target in journal["targets"]}
    assert classes, "the install must journal at least one target"
    expected = {
        boundary[len("commit-"):]
        for boundary in publish.STAGE_BOUNDARIES
        if boundary.startswith("commit-")
    }
    assert classes <= expected, classes - expected
    # The held journal replays to completion on a clean engine.
    engine = TransactionEngine(
        csk_home, pre_write_hook=publish.make_publication_hook()
    )
    from csk import locking

    with locking.ManagerHomeLock(csk_home) as home_lock:
        engine.recover(home_lock)
    _assert_no_journal(csk_home)
    assert _collect(cfg).clean


# ---------------------------------------------------------------------------
# End to end through the production entry points
# ---------------------------------------------------------------------------


def test_path_source_install_locks_and_status_end_to_end(
    tmp_path: Path, csk_home: Path
) -> None:
    """DoD script: install, no-op, status 0, mutate, status nonzero, refuse."""

    project, cfg, members = _basic_fixture(tmp_path, csk_home)

    before = _published_digests(project, csk_home, ["review"])
    result = _install_ok(cfg)
    assert any("review installed" in message for message in result.messages)
    assert any("lock created" in message for message in result.messages)
    _assert_no_journal(csk_home)

    lock_path = project / publish.SKILLFILE_LOCK_NAME
    assert lock_path.is_file() and not lock_path.is_symlink()
    lock = _read_lock(project)
    assert lock is not None
    assert [member.name for member in lock.members] == ["review"]
    member = lock.members[0]
    assert isinstance(member.package, LocalSnapshot)

    marker_path = project / ".agents" / "skills" / "review" / ".csk-install.json"
    assert marker_path.is_file()
    marker = install_marker.read_install_marker(marker_path.read_bytes())
    assert isinstance(marker, install_marker.InstallMarkerV5)
    assert marker.package == member.package
    assert marker.lock_sha256 == lock.lock_sha256

    mirror = project / ".codex" / "skills" / "review"
    assert mirror.is_symlink()
    assert Path(os.readlink(mirror)).as_posix() == "../../.agents/skills/review"
    assert (project / ".codex" / "skills" / ".csk-managed.json").is_file()

    after = _published_digests(project, csk_home, ["review"])
    assert after != before
    assert after["context:review"] != before["context:review"]
    runtime_rows = {key: value for key, value in after.items() if key.startswith("runtime:")}
    assert len(runtime_rows) == 1
    assert "home-source-v1" in after and after["home-source-v1"] != before["home-source-v1"]

    collected = _collect(cfg)
    assert collected.clean
    assert [skill.label for skill in collected.skills] == ["up-to-date"]

    # Second install is a no-op: no transaction, identical published set.
    again = _install_ok(cfg)
    assert any("review up-to-date" in message for message in again.messages)
    assert "lock created" not in " ".join(again.messages)
    assert "lock replaced" not in " ".join(again.messages)
    _assert_no_journal(csk_home)
    assert _published_digests(project, csk_home, ["review"]) == after

    # Mutating a source script flips status nonzero...
    script = project / "agents" / "skills" / "review" / "scripts" / "tool.sh"
    script.write_text("#!/bin/sh\necho MUTATED\n", encoding="utf-8")
    drifted = _collect(cfg)
    assert not drifted.clean
    assert [skill.label for skill in drifted.skills] == ["source-changed"]

    # ...and a locked install without refresh refuses instead of adopting.
    lock_bytes = lock_path.read_bytes()
    _install_failed(cfg, "source_snapshot_changed")
    assert lock_path.read_bytes() == lock_bytes
    assert _published_digests(project, csk_home, ["review"]) == after
    _assert_no_journal(csk_home)

    # Explicit refresh re-resolves and restores currency atomically.
    refreshed = _install_ok(cfg, fetch=True)
    assert any("lock replaced" in message for message in refreshed.messages)
    new_lock = _read_lock(project)
    assert new_lock is not None
    assert new_lock.lock_sha256 != lock.lock_sha256
    assert _collect(cfg).clean
    _assert_no_journal(csk_home)


def test_status_check_exits_zero_when_current_and_nonzero_when_drifted(
    tmp_path: Path, csk_home: Path, monkeypatch: pytest.MonkeyPatch, capsys: Any
) -> None:
    project, cfg, _members = _basic_fixture(tmp_path, csk_home)
    _install_ok(cfg)
    from csk import config as config_module

    config_module.save_config(cfg)
    monkeypatch.setenv("CSK_CONFIG", str(cfg.path))

    assert cli.main(["status", str(project), "--check"]) == cli.EXIT_OK

    script = project / "agents" / "skills" / "review" / "scripts" / "tool.sh"
    script.write_text("#!/bin/sh\necho MUTATED\n", encoding="utf-8")
    assert cli.main(["status", str(project), "--check"]) == cli.EXIT_PARTIAL_FAIL
    capsys.readouterr()


def test_dry_run_plans_without_writing(
    tmp_path: Path, csk_home: Path
) -> None:
    project, cfg, _members = _basic_fixture(tmp_path, csk_home)
    before = _published_digests(project, csk_home, ["review"])
    store_before = digest_path(csk_home / "source-v1")

    result = _install_ok(cfg, dry_run=True)
    assert any("dry-run; no files modified" in message for message in result.messages)

    assert _published_digests(project, csk_home, ["review"]) == before
    assert digest_path(csk_home / "source-v1") == store_before
    assert not (project / publish.SKILLFILE_LOCK_NAME).exists()
    _assert_no_journal(csk_home)


def test_legacy_schema_1_project_installs_unchanged_with_opt_in(
    tmp_path: Path, csk_home: Path
) -> None:
    """The v2 dispatch never hijacks schema-1 installs, even opted in."""

    skills_root = tmp_path / "skills"
    project = make_project(tmp_path)
    write_skillfile(project, {"schema_version": 1, "agents": [], "skills": []})
    commit_all(project, "v1 empty")
    cfg = _v2_config(csk_home, skills_root, project)

    result = _install_ok(cfg)
    assert not (project / publish.SKILLFILE_LOCK_NAME).exists()
    assert _collect(cfg).clean
    assert result.messages == () or all(
        "lock" not in message for message in result.messages
    )


# ---------------------------------------------------------------------------
# Fault matrix: an injected fault at every stage boundary
# ---------------------------------------------------------------------------


def _recover_clean(csk_home: Path) -> None:
    from csk import locking

    engine = TransactionEngine(
        csk_home, pre_write_hook=publish.make_publication_hook()
    )
    with locking.ManagerHomeLock(csk_home) as home_lock:
        engine.recover(home_lock)
    _assert_no_journal(csk_home)


class _FailOnce:
    """Single-shot fault with a reached-assertion for seam injection."""

    def __init__(self, error: BaseException) -> None:
        self.error = error
        self.fired = 0

    def __call__(self, *args: Any, **kwargs: Any) -> Any:
        if self.fired:
            raise AssertionError("fault fired twice; expected single-shot")
        self.fired += 1
        raise self.error


def _seam_fault(
    monkeypatch: pytest.MonkeyPatch,
    target: str,
    error: BaseException,
    *,
    fire_on_call: int = 1,
) -> _FailOnce:
    """Patch one publish seam to fail once; returns the fired counter."""

    fault = _FailOnce(error)
    module_name, _, attr = target.rpartition(".")
    module = sys.modules[module_name]
    real = getattr(module, attr)
    calls = 0

    def wrapper(*args: Any, **kwargs: Any) -> Any:
        nonlocal calls
        calls += 1
        if not fault.fired and calls >= fire_on_call:
            return fault(*args, **kwargs)
        return real(*args, **kwargs)

    monkeypatch.setattr(module, attr, wrapper)
    return fault


def _combo_fault(
    monkeypatch: pytest.MonkeyPatch, points: list[str], error: BaseException
) -> list[tuple[str, str | None, str | None]]:
    """Fail once at each listed engine point; returns the call record."""

    calls: list[tuple[str, str | None, str | None]] = []
    remaining = set(points)
    real_factory = publish.TransactionEngine

    def factory(home: Path, **kwargs: Any) -> TransactionEngine:
        def combined(point: str, target: JournalTarget | None) -> None:
            calls.append(
                (
                    point,
                    target.target_class if target is not None else None,
                    target.identifier if target is not None else None,
                )
            )
            if point in remaining:
                remaining.discard(point)
                raise error

        return real_factory(
            home, fault_hook=combined, pre_write_hook=kwargs.get("pre_write_hook")
        )

    monkeypatch.setattr(publish, "TransactionEngine", factory)
    return calls


def _pre_transaction_cases() -> list[tuple[str, str, BaseException, str, int, bool]]:
    return [
        # (stage, seam, error, code, fire_on_call, store_untouched)
        ("selection", "csk.sources.publish.expand_selectors", RuntimeError("injected selection fault"), "source_selection_invalid", 1, True),
        ("capture", "csk.sources.snapshot.capture_package_snapshot", RuntimeError("injected capture fault"), "source_snapshot_changed", 1, True),
        ("re-enumeration", "csk.sources.publish.expand_collection", RuntimeError("injected re-enumeration fault"), "source_snapshot_changed", 2, True),
        ("snapshot-store", "csk.sources.store.stage_snapshot", OSError("injected store fault"), "source_snapshot_unavailable", 1, True),
        ("lock-create", "csk.sources.publish.create_lock", RuntimeError("injected lock fault"), "source_member_invalid", 1, False),
    ]


@pytest.mark.parametrize(
    "stage,seam,error,code,fire_on_call,store_untouched",
    [pytest.param(*case, id=case[0]) for case in _pre_transaction_cases()],
)
def test_pre_transaction_fault_preserves_prior_state(
    tmp_path: Path,
    csk_home: Path,
    monkeypatch: pytest.MonkeyPatch,
    stage: str,
    seam: str,
    error: BaseException,
    code: str,
    fire_on_call: int,
    store_untouched: bool,
) -> None:
    """Faults before the transaction leave lock, markers and state identical."""

    project, cfg, members = _collection_fixture(tmp_path, csk_home)
    names = [name for name, _ in members]
    before = _published_digests(project, csk_home, names)
    sources_before = _source_snapshots(project, members, csk_home)

    fault = _seam_fault(monkeypatch, seam, error, fire_on_call=fire_on_call)
    result = _install(cfg)
    assert result.status == "failed", result.messages
    assert fault.fired == 1, f"fault at {seam} was never reached"
    assert result.errors and result.errors[0].startswith(code + ":"), result.errors

    live = _published_digests(project, csk_home, names)
    if store_untouched:
        assert live == before
    else:
        # Lock creation runs after snapshot staging: the published set
        # is identical while the store holds inert staged entries.
        assert _published_without_store(live) == _published_without_store(before)
        _assert_store_residue_verifies(
            csk_home, _expected_store_entries(project, members, csk_home), exact=True
        )
        staged_snapshots = digest_path(csk_home / "source-v1" / "snapshots")
    assert _source_snapshots(project, members, csk_home) == sources_before
    assert not (project / publish.SKILLFILE_LOCK_NAME).exists()
    _assert_no_journal(csk_home)

    # Positive control: the same unfaulted fixture installs cleanly, and
    # re-staging over residue is idempotent (residue was legitimate).
    monkeypatch.undo()
    assert _install_ok(cfg)
    assert _collect(cfg).clean
    if not store_untouched:
        restaged = digest_path(csk_home / "source-v1" / "snapshots")
        assert restaged == staged_snapshots


_COMMIT_WRITE_POINTS = ("after_backup", "target_committed", "after_install")

_STAGING_POINTS = (
    "before_staging_entry_create",
    "staging_entry_created",
    "staging_entry_completed",
    "before_staging_mode_finalize",
    "staging_mode_finalized",
    "staging_mode_finalize_completed",
    "after_staging_chunk_sync",
    "during_staging_copy",
    "prepared",
)

_REMOVAL_POINTS = (
    "cleanup_journaled",
    "sidecar_tombed",
    "before_sidecar_mode_writable",
    "sidecar_mode_writable",
    "before_journal_tomb",
    "journal_tombed",
    "before_journal_unlink",
)

# Removal points that fire while backup sidecars are still being
# discarded (engine temporaries linger until recovery finishes).
_SIDECAR_PHASE_POINTS = frozenset(
    {
        "cleanup_journaled",
        "sidecar_tombed",
        "before_sidecar_mode_writable",
        "sidecar_mode_writable",
    }
)


@pytest.mark.parametrize("point", [*_STAGING_POINTS, *_COMMIT_WRITE_POINTS])
def test_engine_fault_before_commit_preserves_prior_state(
    tmp_path: Path,
    csk_home: Path,
    monkeypatch: pytest.MonkeyPatch,
    point: str,
) -> None:
    """Faults during staging or commit roll back to byte-identical state."""

    project, cfg, members = _collection_fixture(tmp_path, csk_home)
    names = [name for name, _ in members]
    before = _published_digests(project, csk_home, names)
    sources_before = _source_snapshots(project, members, csk_home)

    fault = _EngineFault(monkeypatch)
    fault.fail_once_at(point, RuntimeError(f"injected fault at {point}"))
    result = _install(cfg)
    assert result.status == "failed", result.messages
    assert fault.fired(point), f"fault point {point} was never reached"

    live = _published_digests(project, csk_home, names)
    assert _published_without_store(live) == _published_without_store(before)
    _assert_store_residue_verifies(
        csk_home, _expected_store_entries(project, members, csk_home), exact=True
    )
    assert _source_snapshots(project, members, csk_home) == sources_before
    assert not (project / publish.SKILLFILE_LOCK_NAME).exists()
    # Rollback already discarded its sidecars and journal; recovery is a
    # no-op that must change nothing.
    _assert_no_journal(csk_home)
    assert _engine_temporaries(project) == []
    assert _engine_temporaries(csk_home / "source-v1") == []
    _recover_clean(csk_home)
    recovered = _published_digests(project, csk_home, names)
    assert _published_without_store(recovered) == _published_without_store(before)


def test_engine_fault_during_cleanup_leaves_completed_installation(
    tmp_path: Path, csk_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A fault after the last write keeps the completed state, recoverable."""

    project, cfg, members = _collection_fixture(tmp_path, csk_home)
    names = [name for name, _ in members]

    fault = _EngineFault(monkeypatch)
    fault.fail_once_at("before_cleanup", RuntimeError("injected cleanup fault"))
    result = _install(cfg)
    assert result.status == "failed", result.messages
    assert fault.fired("before_cleanup")

    # Everything is published; only the journal remains.
    lock = _read_lock(project)
    assert lock is not None
    assert len(_journal_texts(csk_home)) == 1
    completed = _published_digests(project, csk_home, names)

    _recover_clean(csk_home)
    assert _published_digests(project, csk_home, names) == completed
    collected = _collect(cfg)
    assert collected.clean


@pytest.mark.parametrize("point", list(_REMOVAL_POINTS))
def test_engine_fault_during_journal_removal_recovers_clean(
    tmp_path: Path,
    csk_home: Path,
    monkeypatch: pytest.MonkeyPatch,
    point: str,
) -> None:
    """Faults in journal teardown keep the completed state and recover clean.

    Removal points only fire while discarding sidecars, so the faulted
    run is a refresh over installed state: backups exist to tomb and
    remove. A single removal fault always lands after every target
    committed (phase ``cleanup``); removal during rollback needs a
    second, commit-breaking fault and is covered by the rollback test.
    """

    project, cfg, members = _collection_fixture(tmp_path, csk_home)
    names = [name for name, _ in members]
    _install_ok(cfg)
    staged_old = _expected_store_entries(project, members, csk_home)
    (project / "agents" / "skills" / "review" / "scripts" / "tool.sh").write_text(
        "#!/bin/sh\necho refresh-me\n", encoding="utf-8"
    )

    fault = _EngineFault(monkeypatch)
    fault.fail_once_at(point, RuntimeError(f"injected removal fault at {point}"))
    result = _install(cfg, fetch=True)
    assert result.status == "failed", result.messages
    assert fault.fired(point), f"fault point {point} was never reached"

    journals = _journal_texts(csk_home)
    assert len(journals) == 1
    journal = json.loads(journals[0].read_text(encoding="utf-8"))
    assert journal["phase"] == "cleanup", journal["phase"]
    assert _read_lock(project) is not None
    completed = _published_digests(project, csk_home, names)
    # Both generations verify: the superseded entries stay valid history.
    both_generations = {
        **staged_old,
        **_expected_store_entries(project, members, csk_home),
    }
    _assert_store_residue_verifies(csk_home, both_generations, exact=True)
    # Sidecar-phase faults interrupt the discard (temporaries linger);
    # journal-phase faults run after the discard completed (none linger).
    pre_temps = _engine_temporaries(project) + _engine_temporaries(
        csk_home / "source-v1"
    )
    if point in _SIDECAR_PHASE_POINTS:
        assert pre_temps, f"discard left no temporaries at {point}"
    else:
        assert pre_temps == [], f"temporaries linger past the discard: {pre_temps}"
    _recover_clean(csk_home)
    recovered = _published_digests(project, csk_home, names)
    assert _published_without_store(recovered) == _published_without_store(completed)
    _assert_store_residue_verifies(csk_home, both_generations, exact=True)
    post_temps = _engine_temporaries(project) + _engine_temporaries(
        csk_home / "source-v1"
    )
    assert post_temps == [], f"recovery left temporaries: {post_temps}"
    assert _collect(cfg).clean


def test_engine_fault_during_rollback_recovers_to_prior_state(
    tmp_path: Path, csk_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A fault inside rollback keeps the journal; recovery restores before."""

    project, cfg, members = _collection_fixture(tmp_path, csk_home)
    names = [name for name, _ in members]
    # Refresh over installed state so rollback has real backups to restore.
    _install_ok(cfg)
    staged_old = _expected_store_entries(project, members, csk_home)
    lock_before = (project / publish.SKILLFILE_LOCK_NAME).read_bytes()
    (project / "agents" / "skills" / "review" / "scripts" / "tool.sh").write_text(
        "#!/bin/sh\necho refresh-me\n", encoding="utf-8"
    )
    before = _published_digests(project, csk_home, names)

    # Capture the real factory before _EngineFault patches the attribute.
    real_factory = publish.TransactionEngine
    fault = _EngineFault(monkeypatch)

    def combo_factory(home: Path, **kwargs: Any) -> TransactionEngine:
        def combined(point: str, target: JournalTarget | None) -> None:
            fault.calls.append(
                (
                    point,
                    target.target_class if target is not None else None,
                    target.identifier if target is not None else None,
                )
            )
            if point == "after_backup" and not any(
                call[0] == "after_backup" for call in fault.calls[:-1]
            ):
                raise RuntimeError("trigger rollback")
            if point == "after_restore":
                raise RuntimeError("fault inside rollback")

        return real_factory(home, fault_hook=combined, pre_write_hook=kwargs.get("pre_write_hook"))

    monkeypatch.setattr(publish, "TransactionEngine", combo_factory)
    result = _install(cfg, fetch=True)
    assert result.status == "failed", result.messages
    assert fault.fired("after_backup")
    assert fault.fired("after_restore"), "rollback never reached after_restore"

    journals = _journal_texts(csk_home)
    assert len(journals) == 1
    _assert_store_residue_verifies(
        csk_home,
        {
            **staged_old,
            **_expected_store_entries(project, members, csk_home),
        },
        exact=True,
    )
    _recover_clean(csk_home)
    recovered = _published_digests(project, csk_home, names)
    assert _published_without_store(recovered) == _published_without_store(before)
    assert (project / publish.SKILLFILE_LOCK_NAME).read_bytes() == lock_before


# ---------------------------------------------------------------------------
# Fault matrix: one fault per commit-stage target class
# ---------------------------------------------------------------------------


_COMMIT_FRESH_CLASSES = (
    publish.CLASS_BINDINGS,
    publish.CLASS_LOCK,
    publish.CLASS_CONTEXT,
    publish.CLASS_RUNTIME,
    publish.CLASS_ADAPTER_LEDGER,
)


@pytest.mark.parametrize("target_class", list(_COMMIT_FRESH_CLASSES))
def test_engine_fault_while_committing_target_class_rolls_back(
    tmp_path: Path,
    csk_home: Path,
    monkeypatch: pytest.MonkeyPatch,
    target_class: str,
) -> None:
    """A fault during any class commit rolls back to byte-identical state.

    Each commit-stage boundary (commit-05 through commit-60) is faulted
    while its own class is being written: earlier classes committed and
    are restored, later classes never start.
    """

    project, cfg, members = _collection_fixture(tmp_path, csk_home)
    names = [name for name, _ in members]
    before = _published_digests(project, csk_home, names)
    sources_before = _source_snapshots(project, members, csk_home)

    fault = _EngineFault(monkeypatch)
    fault.fail_once_at(
        "after_backup",
        RuntimeError(f"injected fault while committing {target_class}"),
        when=lambda target: target is not None
        and target.target_class == target_class,
    )
    result = _install(cfg)
    assert result.status == "failed", result.messages
    assert any(
        call[0] == "after_backup" and call[1] == target_class
        for call in fault.calls
    ), f"no {target_class} target reached after_backup"

    live = _published_digests(project, csk_home, names)
    assert _published_without_store(live) == _published_without_store(before)
    _assert_store_residue_verifies(
        csk_home, _expected_store_entries(project, members, csk_home), exact=True
    )
    assert _source_snapshots(project, members, csk_home) == sources_before
    assert not (project / publish.SKILLFILE_LOCK_NAME).exists()
    _assert_no_journal(csk_home)
    assert _engine_temporaries(project) == []
    assert _engine_temporaries(csk_home / "source-v1") == []
    _recover_clean(csk_home)


def _drop_beta_from_skillfile(project: Path) -> None:
    """Rewrite the fixture Skillfile without the beta member."""

    doc = json.loads((project / "Skillfile.json").read_text(encoding="utf-8"))
    doc["skills"] = [
        {"name": "review", "from": "local", "directory": "agents/skills/review"},
        {
            "from": "local",
            "directory": "agents/skills/team",
            "include": ["alpha"],
        },
    ]
    (project / "Skillfile.json").write_text(
        json.dumps(doc, indent=2) + "\n", encoding="utf-8"
    )


def test_engine_fault_while_committing_removal_rolls_back(
    tmp_path: Path, csk_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A fault during the commit-80-removal boundary restores everything."""

    project, cfg, members = _collection_fixture(tmp_path, csk_home)
    names = [name for name, _ in members]
    _install_ok(cfg)
    lock_before = (project / publish.SKILLFILE_LOCK_NAME).read_bytes()
    _drop_beta_from_skillfile(project)
    before = _published_digests(project, csk_home, names)
    sources_before = _source_snapshots(project, members, csk_home)

    fault = _EngineFault(monkeypatch)
    fault.fail_once_at(
        "after_backup",
        RuntimeError("injected fault while committing 80-removal"),
        when=lambda target: target is not None
        and target.target_class == publish.CLASS_REMOVAL,
    )
    result = _install(cfg, fetch=True)
    assert result.status == "failed", result.messages
    assert any(
        call[0] == "after_backup" and call[1] == publish.CLASS_REMOVAL
        for call in fault.calls
    ), "no 80-removal target reached after_backup"

    live = _published_digests(project, csk_home, names)
    assert _published_without_store(live) == _published_without_store(before)
    assert _source_snapshots(project, members, csk_home) == sources_before
    assert (project / publish.SKILLFILE_LOCK_NAME).read_bytes() == lock_before
    _recover_clean(csk_home)
    recovered = _published_digests(project, csk_home, names)
    assert _published_without_store(recovered) == _published_without_store(before)


# ---------------------------------------------------------------------------
# Publication recheck: boundary moved between two writes
# ---------------------------------------------------------------------------


def test_publication_recheck_runs_between_writes(
    tmp_path: Path, csk_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A boundary retargeted after the first commit refuses the next write.

    The S3 write-boundary-retarget claim end to end: bindings commit
    first, then the ``.codex`` ancestor becomes a link before any
    adapter mirror is written. The pre-write hook refuses
    ``source_output_overlap`` and the engine rolls back the committed
    prefix; after the probe restores its own swap the tree is
    byte-identical to before.
    """

    project, cfg, members = _collection_fixture(tmp_path, csk_home)
    names = [name for name, _ in members]
    before = _published_digests(project, csk_home, names)
    sources_before = _source_snapshots(project, members, csk_home)
    outside = tmp_path / "outside-codex"
    outside.mkdir()

    swapped: list[str] = []

    def retarget(_point: str, _target: JournalTarget | None) -> None:
        dot_codex = project / ".codex"
        if dot_codex.is_symlink() or not dot_codex.exists():
            raise AssertionError(".codex ancestor is missing when retargeting")
        dot_codex.rename(project / ".codex-saved-by-probe")
        dot_codex.symlink_to(outside, target_is_directory=True)
        swapped.append("codex")

    fault = _EngineFault(monkeypatch)
    fault.mutate_once_at("target_committed", retarget)
    result = _install(cfg)
    assert result.status == "failed", result.messages
    assert swapped == ["codex"], "retarget never ran between writes"
    assert result.errors and result.errors[0].startswith(
        "source_output_overlap:"
    ), result.errors
    committed_classes = {
        call[1] for call in fault.calls if call[0] == "target_committed"
    }
    assert committed_classes, "no write preceded the retarget"

    # Rollback is deferred while the boundary is moved: the journal stays
    # in rolling_back instead of recording bogus discard states.
    journals = _journal_texts(csk_home)
    assert len(journals) == 1
    journal = json.loads(journals[0].read_text(encoding="utf-8"))
    assert journal["phase"] == "rolling_back", journal["phase"]

    # Restore the probe's own swap; the deferred rollback replays clean.
    (project / ".codex").unlink()
    (project / ".codex-saved-by-probe").rename(project / ".codex")
    _recover_clean(csk_home)
    live = _published_digests(project, csk_home, names)
    assert _published_without_store(live) == _published_without_store(before)
    _assert_store_residue_verifies(
        csk_home, _expected_store_entries(project, members, csk_home), exact=True
    )
    assert _source_snapshots(project, members, csk_home) == sources_before
    assert not (project / publish.SKILLFILE_LOCK_NAME).exists()
    assert _engine_temporaries(project) == []
    assert _engine_temporaries(csk_home / "source-v1") == []


# ---------------------------------------------------------------------------
# Unmanaged destinations: the section-2 four-vector family
# ---------------------------------------------------------------------------


def _host_conflates_case(scratch: Path) -> bool:
    """Probe whether the filesystem conflates case-variant spellings."""

    probe = scratch / "case-probe"
    probe.mkdir(exist_ok=True)
    lower = probe / "alias-probe"
    upper = probe / "ALIAS-PROBE"
    lower.write_text("lower", encoding="utf-8")
    try:
        return upper.exists() and os.path.samefile(lower, upper)
    except OSError:
        return False
    finally:
        lower.unlink(missing_ok=True)


@pytest.mark.parametrize(
    "vector",
    [
        "destination-link",
        "adapter-file",
        "adapter-dir",
        "empty-unmanaged-dir",
        "context-file",
        "case-variant-marker",
        "casing-alias",
        "changed-parent",
    ],
)
def test_unmanaged_destination_vectors_never_overwritten(
    tmp_path: Path,
    csk_home: Path,
    monkeypatch: pytest.MonkeyPatch,
    vector: str,
) -> None:
    """Every section-2 vector refuses source_output_overlap, obstacle intact.

    Links, adapter occupants and casing aliases are planted before the
    install; the changed parent swaps the ancestor between two writes.
    On a case-sensitive host the casing spelling is a distinct
    directory (declared bound): the install succeeds and still leaves
    it byte-identical.
    """

    project, cfg, members = _basic_fixture(tmp_path, csk_home)
    names = [name for name, _ in members]
    obstacle_checks: list[Any] = []
    restore: Any = None
    expect_refusal = True

    if vector == "destination-link":
        outside = tmp_path / "outside-context"
        (outside / "payload").mkdir(parents=True)
        (outside / "payload" / "keep.txt").write_text("keep\n", encoding="utf-8")
        skills = project / ".agents" / "skills"
        skills.mkdir(parents=True)
        link = skills / "review"
        link.symlink_to(outside / "payload", target_is_directory=True)
        spelling = os.readlink(link)

        def check_link() -> None:
            assert link.is_symlink()
            assert os.readlink(link) == spelling
            assert (outside / "payload" / "keep.txt").read_text(
                encoding="utf-8"
            ) == "keep\n"

        obstacle_checks.append(check_link)
    elif vector == "adapter-file":
        target = project / ".codex" / "skills" / "review"
        target.parent.mkdir(parents=True)
        target.write_text("unmanaged-adapter-file\n", encoding="utf-8")

        def check_adapter_file() -> None:
            assert target.is_file() and not target.is_symlink()
            assert (
                target.read_text(encoding="utf-8") == "unmanaged-adapter-file\n"
            )

        obstacle_checks.append(check_adapter_file)
    elif vector == "adapter-dir":
        target = project / ".codex" / "skills" / "review"
        (target / "nested").mkdir(parents=True)
        (target / "nested" / "keep.txt").write_text("keep\n", encoding="utf-8")
        stamp = digest_path(target)

        def check_adapter_dir() -> None:
            assert target.is_dir() and not target.is_symlink()
            assert digest_path(target) == stamp

        obstacle_checks.append(check_adapter_dir)
    elif vector == "empty-unmanaged-dir":
        # Even an empty directory without a marker is unmanaged: the
        # gate admits no shape exception.
        target = project / ".agents" / "skills" / "review"
        target.mkdir(parents=True)

        def check_empty_dir() -> None:
            assert target.is_dir() and not target.is_symlink()
            assert list(target.iterdir()) == []

        obstacle_checks.append(check_empty_dir)
    elif vector == "context-file":
        target = project / ".agents" / "skills" / "review"
        target.parent.mkdir(parents=True)
        target.write_text("not-a-directory\n", encoding="utf-8")

        def check_context_file() -> None:
            assert target.is_file() and not target.is_symlink()
            assert target.read_text(encoding="utf-8") == "not-a-directory\n"

        obstacle_checks.append(check_context_file)
    elif vector == "case-variant-marker":
        # A marker claiming a different-cased name is foreign: exact
        # match authorizes, nothing looser. Installed first so the
        # marker is a real v5 marker, then renamed.
        _install_ok(cfg)
        marker_path = project / ".agents" / "skills" / "review" / ".csk-install.json"
        raw_marker = json.loads(marker_path.read_bytes().decode("utf-8"))
        raw_marker["name"] = "REVIEW"
        marker_path.write_bytes(
            install_marker.serialize_install_marker(raw_marker)
        )

        def check_variant_marker() -> None:
            renamed = install_marker.read_install_marker(
                marker_path.read_bytes()
            )
            assert isinstance(renamed, install_marker.InstallMarkerV5)
            assert renamed.name == "REVIEW"

        obstacle_checks.append(check_variant_marker)
    elif vector == "casing-alias":
        alias = project / ".agents" / "skills" / "REVIEW"
        (alias / "nested").mkdir(parents=True)
        (alias / "nested" / "keep.txt").write_text("keep\n", encoding="utf-8")
        stamp = digest_path(alias)

        def check_alias() -> None:
            assert digest_path(alias) == stamp

        obstacle_checks.append(check_alias)
        if not _host_conflates_case(tmp_path):
            # Declared bound: on a case-sensitive filesystem REVIEW is a
            # distinct authored directory, not an alias of review.
            expect_refusal = False
    elif vector == "changed-parent":
        swapped: list[str] = []

        def replace(_point: str, _target: JournalTarget | None) -> None:
            skills = project / ".agents" / "skills"
            skills.rename(project / ".agents" / "skills-saved-by-probe")
            skills.mkdir()
            swapped.append("skills")

        def restore_parents() -> None:
            fresh = project / ".agents" / "skills"
            if fresh.is_symlink():
                raise AssertionError("swapped parent became a link")
            if fresh.is_dir():
                # Proves nothing was published into the swapped parent:
                # the hook refused before the first context write.
                fresh.rmdir()
            (project / ".agents" / "skills-saved-by-probe").rename(fresh)

        fault = _EngineFault(monkeypatch)
        fault.mutate_once_at("target_committed", replace)
        restore = (restore_parents, swapped)
    else:  # pragma: no cover - parametrization is closed above
        raise AssertionError(f"unknown vector {vector}")

    before = _published_digests(project, csk_home, names)
    sources_before = _source_snapshots(project, members, csk_home)
    result = _install(cfg)

    if not expect_refusal:
        assert result.status == "ok", result.errors
        for check in obstacle_checks:
            check()
        assert _collect(cfg).clean
        return

    assert result.status == "failed", result.messages
    assert result.errors and result.errors[0].startswith(
        "source_output_overlap:"
    ), result.errors
    for check in obstacle_checks:
        check()
    if restore is not None:
        restore_parents, swapped = restore
        assert swapped == ["skills"], "parent swap never ran between writes"
        journals = _journal_texts(csk_home)
        assert len(journals) == 1
        journal = json.loads(journals[0].read_text(encoding="utf-8"))
        assert journal["phase"] == "rolling_back", journal["phase"]
        restore_parents()
        _recover_clean(csk_home)
    else:
        _assert_no_journal(csk_home)
    live = _published_digests(project, csk_home, names)
    assert _published_without_store(live) == _published_without_store(before)
    _assert_store_residue_verifies(
        csk_home, _expected_store_entries(project, members, csk_home), exact=True
    )
    assert _source_snapshots(project, members, csk_home) == sources_before
    if vector != "case-variant-marker":
        assert not (project / publish.SKILLFILE_LOCK_NAME).exists()
    assert _engine_temporaries(project) == []
    assert _engine_temporaries(csk_home / "source-v1") == []


@pytest.mark.parametrize(
    "shape", ["no-marker", "foreign-marker", "garbage-marker"]
)
def test_unmanaged_nonmember_sibling_survives_install(
    tmp_path: Path,
    csk_home: Path,
    monkeypatch: pytest.MonkeyPatch,
    shape: str,
) -> None:
    """A distinct-path unmanaged sibling is ignored on every host.

    The casing-alias Linux lesson as a platform-independent class: on
    a case-sensitive host ``REVIEW`` and ``review`` are distinct
    directories, so the install must neither remove the sibling (AC
    (c)) nor refuse for it (it is referenced by no lock this project
    owns, so per the F-A decision it is not this install's problem).
    Where the filesystem conflates the spelling, the
    member-destination check still refuses (casing-alias vector).
    Status stays clean here on every host, and no removal targets the
    sibling in the committed plan.
    """

    project, cfg, _members = _basic_fixture(tmp_path / "main", csk_home)
    sibling = project / ".agents" / "skills" / "unrelated"
    (sibling / "nested").mkdir(parents=True)
    (sibling / "nested" / "keep.txt").write_text("keep\n", encoding="utf-8")
    if shape == "foreign-marker":
        seed_project, seed_cfg, _seed = _basic_fixture(tmp_path / "seed", csk_home)
        _install_ok(seed_cfg)
        seed_marker = json.loads(
            (
                seed_project
                / ".agents"
                / "skills"
                / "review"
                / ".csk-install.json"
            )
            .read_bytes()
            .decode("utf-8")
        )
        assert seed_marker["name"] == "review"
        (sibling / ".csk-install.json").write_bytes(
            install_marker.serialize_install_marker(seed_marker)
        )
    elif shape == "garbage-marker":
        (sibling / ".csk-install.json").write_bytes(b"not-a-marker{{{")
    stamp = digest_path(sibling)

    fault = _EngineFault(monkeypatch)
    _install_ok(cfg)

    assert digest_path(sibling) == stamp
    assert (sibling / "nested" / "keep.txt").read_text(encoding="utf-8") == "keep\n"
    assert not any(
        identifier == "context/project/unrelated" for _, _, identifier in fault.calls
    ), "sibling drew a removal target"
    assert _collect(cfg).clean


# ---------------------------------------------------------------------------
# Status: read-only in every non-current case
# ---------------------------------------------------------------------------


def _tree_listing(root: Path) -> list[str]:
    """List a whole tree with engine digests per node.

    The engine ``digest_path`` refuses trees containing links (adapter
    mirrors), so the read-only proof walks with its own framing but
    hashes every content byte with the engine digesters: links by
    readlink spelling (``entry``), files by bytes.
    """

    rows: list[str] = []
    for path in sorted(
        root.rglob("*"), key=lambda item: item.relative_to(root).as_posix()
    ):
        rel = path.relative_to(root).as_posix()
        info = path.lstat()
        if stat.S_ISLNK(info.st_mode):
            rows.append(f"L {rel} {digest_target(path, kind='entry')}")
        elif stat.S_ISDIR(info.st_mode):
            rows.append(f"D {rel}")
        elif stat.S_ISREG(info.st_mode):
            rows.append(f"F {rel} {digest_target(path, kind='bytes')}")
        else:
            raise AssertionError(f"special file in tree: {path}")
    return rows


@pytest.mark.parametrize(
    "case",
    [
        "source-changed",
        "missing-marker",
        "drifted-context",
        "lock-stale",
        "no-lock",
        "snapshot-unavailable",
        "mirror-retargeted",
        "runtime-tampered",
    ],
)
def test_status_is_read_only_and_nonzero_when_not_current(
    tmp_path: Path,
    csk_home: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: Any,
    case: str,
) -> None:
    """Status changes nothing and exits nonzero in each non-current case."""

    project, cfg, _members = _basic_fixture(tmp_path, csk_home)
    _install_ok(cfg)
    from csk import config as config_module

    config_module.save_config(cfg)
    monkeypatch.setenv("CSK_CONFIG", str(cfg.path))

    context = project / ".agents" / "skills" / "review"
    if case == "source-changed":
        (project / "agents" / "skills" / "review" / "scripts" / "tool.sh").write_text(
            "#!/bin/sh\necho MUTATED\n", encoding="utf-8"
        )
    elif case == "missing-marker":
        (context / ".csk-install.json").unlink()
    elif case == "drifted-context":
        (context / "SKILL.md").write_text("# tampered\n", encoding="utf-8")
    elif case == "lock-stale":
        doc = json.loads((project / "Skillfile.json").read_text(encoding="utf-8"))
        doc["agents"] = ["codex_cli"]
        (project / "Skillfile.json").write_text(
            json.dumps(doc, indent=2) + "\n", encoding="utf-8"
        )
    elif case == "no-lock":
        (project / publish.SKILLFILE_LOCK_NAME).unlink()
    elif case == "snapshot-unavailable":
        import shutil

        shutil.rmtree(project / "agents" / "skills" / "review")
    elif case == "mirror-retargeted":
        mirror = project / ".codex" / "skills" / "review"
        mirror.unlink()
        mirror.symlink_to(tmp_path / "elsewhere")
    elif case == "runtime-tampered":
        lock = _read_lock(project)
        assert lock is not None
        package = lock.members[0].package
        assert isinstance(package, LocalSnapshot)
        entry = publish.runtime_entry_path(
            csk_home, "review", package_identity_sha256(package)
        )
        probe = entry / "tampered.txt"
        probe.write_text("tampered\n", encoding="utf-8")
    else:  # pragma: no cover - parametrization is closed above
        raise AssertionError(f"unknown case {case}")

    project_before = _tree_listing(project)
    home_before = _tree_listing(csk_home)
    collected = _collect(cfg)
    assert not collected.clean, f"status is clean for {case}"
    exit_code = cli.main(["status", str(project), "--check"])
    capsys.readouterr()
    assert exit_code != cli.EXIT_OK, f"status exits 0 for {case}"
    assert _tree_listing(project) == project_before
    assert _tree_listing(csk_home) == home_before


# ---------------------------------------------------------------------------
# Repair: locked install revalidates, never trusts, never rewrites the lock
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "case",
    [
        "tampered-context",
        "forged-marker",
        "snapshot-gone",
        "store-heals",
        "lock-stale",
    ],
)
def test_locked_repair_revalidates_without_trust_or_rewrite(
    tmp_path: Path, csk_home: Path, case: str
) -> None:
    """Repair heals from the locked snapshot, refuses drift, keeps the lock.

    Tampered outputs and lying markers are rebuilt from revalidated
    locked bytes; a lost snapshot with a lost source refuses instead
    of recreating; a lost store entry heals from verified live bytes;
    a changed manifest refuses lock-stale. No case rewrites the lock.
    """

    import shutil

    from csk.sources import store as store_module

    project, cfg, members = _basic_fixture(tmp_path, csk_home)
    names = [name for name, _ in members]
    _install_ok(cfg)
    lock_path = project / publish.SKILLFILE_LOCK_NAME
    lock_bytes = lock_path.read_bytes()
    lock = _read_lock(project)
    assert lock is not None and lock.lock_sha256 is not None
    member = lock.members[0]
    assert isinstance(member.package, LocalSnapshot)
    context = project / ".agents" / "skills" / "review"
    marker_path = context / ".csk-install.json"

    if case == "tampered-context":
        (context / "SKILL.md").write_text("# tampered\n", encoding="utf-8")
        assert not _collect(cfg).clean
    elif case == "forged-marker":
        raw = json.loads(marker_path.read_bytes().decode("utf-8"))
        raw["package"]["snapshot"] = "sha256:" + "0" * 64
        marker_path.write_bytes(
            install_marker.serialize_install_marker(raw)
        )
        forged = install_marker.read_install_marker(marker_path.read_bytes())
        assert isinstance(forged, install_marker.InstallMarkerV5)
        assert forged.package.snapshot != member.package.snapshot
    elif case == "snapshot-gone":
        key = package_identity_sha256(member.package)
        shutil.rmtree(store_module.entry_dir(csk_home, member.name, key))
        shutil.rmtree(project / "agents" / "skills" / "review")
    elif case == "store-heals":
        key = package_identity_sha256(member.package)
        shutil.rmtree(store_module.entry_dir(csk_home, member.name, key))
    elif case == "lock-stale":
        doc = json.loads((project / "Skillfile.json").read_text(encoding="utf-8"))
        doc["agents"] = ["codex_cli"]
        (project / "Skillfile.json").write_text(
            json.dumps(doc, indent=2) + "\n", encoding="utf-8"
        )
    else:  # pragma: no cover - parametrization is closed above
        raise AssertionError(f"unknown case {case}")

    if case in {"tampered-context", "forged-marker", "store-heals"}:
        result = _install_ok(cfg)
        assert lock_path.read_bytes() == lock_bytes
        repaired = install_marker.read_install_marker(marker_path.read_bytes())
        assert isinstance(repaired, install_marker.InstallMarkerV5)
        assert repaired.package.snapshot == member.package.snapshot
        assert repaired.lock_sha256 == lock.lock_sha256
        assert _collect(cfg).clean
        if case == "store-heals":
            assert any(
                "review up-to-date" in message for message in result.messages
            )
            key = package_identity_sha256(member.package)
            stored = store_module.lookup_snapshot(csk_home, member.name, key)
            assert stored.snapshot == member.package.snapshot
        else:
            assert not any(
                "up-to-date" in message for message in result.messages
            ), "repair skipped instead of republishing"
    else:
        code = (
            "source_snapshot_unavailable"
            if case == "snapshot-gone"
            else "source_lock_stale"
        )
        _install_failed(cfg, code)
        assert lock_path.read_bytes() == lock_bytes
    _assert_no_journal(csk_home)


# ---------------------------------------------------------------------------
# Refresh: gates rerun, lock and markers replace only after success
# ---------------------------------------------------------------------------


def test_refresh_reruns_gates_and_keeps_old_lock_on_refusal(
    tmp_path: Path, csk_home: Path
) -> None:
    """A refresh with drifted sources and an unmanaged victim refuses."""

    project, cfg, members = _collection_fixture(tmp_path, csk_home)
    names = [name for name, _ in members]
    _install_ok(cfg)
    lock_path = project / publish.SKILLFILE_LOCK_NAME
    lock_bytes = lock_path.read_bytes()
    staged_old = _expected_store_entries(project, members, csk_home)
    (project / "agents" / "skills" / "review" / "scripts" / "tool.sh").write_text(
        "#!/bin/sh\necho refresh-me\n", encoding="utf-8"
    )
    (project / ".agents" / "skills" / "beta" / ".csk-install.json").unlink()
    before = _published_digests(project, csk_home, names)

    _install_failed(cfg, "source_output_overlap", fetch=True)

    assert lock_path.read_bytes() == lock_bytes
    live = _published_digests(project, csk_home, names)
    assert _published_without_store(live) == _published_without_store(before)
    _assert_store_residue_verifies(
        csk_home,
        {**staged_old, **_expected_store_entries(project, members, csk_home)},
        exact=True,
    )
    _assert_no_journal(csk_home)
    assert _engine_temporaries(project) == []
    assert _engine_temporaries(csk_home / "source-v1") == []


def test_removed_skills_clean_up_from_the_lock(
    tmp_path: Path, csk_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Dropping a member removes its outputs with the source dir deleted.

    The member directory is gone from disk, so the removal plan cannot
    come from rescanning live sources: it derives from the lock plus
    live managed state, and the transaction commits 80-removal targets.
    """

    import shutil

    project, cfg, members = _collection_fixture(tmp_path, csk_home)
    names = [name for name, _ in members]
    _install_ok(cfg)
    old_lock = _read_lock(project)
    assert old_lock is not None
    beta_package = next(
        member.package
        for member in old_lock.members
        if member.name == "beta"
    )
    assert isinstance(beta_package, LocalSnapshot)
    beta_key = package_identity_sha256(beta_package)

    _drop_beta_from_skillfile(project)
    shutil.rmtree(project / "agents" / "skills" / "team" / "beta")
    # Dot-shapes are never removal candidates: user keeps survive refresh.
    context_keep = project / ".agents" / "skills" / ".keep"
    adapter_keep = project / ".codex" / "skills" / ".keep"
    context_keep.write_text("context-keep\n", encoding="utf-8")
    adapter_keep.write_text("adapter-keep\n", encoding="utf-8")

    fault = _EngineFault(monkeypatch)
    result = _install_ok(cfg, fetch=True)
    assert any("lock replaced" in message for message in result.messages)
    assert any(
        call[0] == "target_committed" and call[1] == publish.CLASS_REMOVAL
        for call in fault.calls
    ), "no 80-removal target committed"

    new_lock = _read_lock(project)
    assert new_lock is not None
    assert [member.name for member in new_lock.members] == ["alpha", "review"]
    assert not (project / ".agents" / "skills" / "beta").exists()
    assert not (project / ".codex" / "skills" / "beta").exists()
    assert not publish.runtime_entry_path(csk_home, "beta", beta_key).exists()
    assert context_keep.read_text(encoding="utf-8") == "context-keep\n"
    assert adapter_keep.read_text(encoding="utf-8") == "adapter-keep\n"
    assert _collect(cfg).clean
    _assert_no_journal(csk_home)
    live = _published_digests(project, csk_home, ["alpha", "review"])
    assert not any("beta" in key for key in live)


# ---------------------------------------------------------------------------
# Frozen link roots: stable links work, retargeted links refuse
# ---------------------------------------------------------------------------


def _hook_target(
    target_class: str,
    identifier: str,
    live: Path,
    payload: dict[str, Any],
) -> JournalTarget:
    """Build a pending journal target carrying one recheck payload."""

    return JournalTarget(
        target_class=target_class,
        identifier=identifier,
        kind="entry",
        live_path=os.fspath(live),
        staged_path=None,
        staged_source=None,
        staging_entries=[],
        backup_path=os.fspath(live.parent / ".probe-backup"),
        rollback_path=os.fspath(live.parent / ".probe-rollback"),
        expected_preimage_digest=ABSENT_DIGEST,
        expected_generation=None,
        generation_path=None,
        desired_digest="sha256:" + "1" * 64,
        state="pending",
        publication_recheck=payload,
    )


def test_publication_hook_defers_frozen_link_root_to_recheck(
    tmp_path: Path, csk_home: Path
) -> None:
    """A frozen .agents link defers to the recheck: stable allows, swap refuses.

    Installer-level link roots are skipped by the pre-existing gitignore
    gate (``git check-ignore`` refuses beyond-symlink probes with exit
    128 before publication starts), so the deferral is driven at the
    payload/hook seam instead of through the installer entry point.
    """

    project = make_project(tmp_path)
    real_a = tmp_path / "real-agents-a"
    real_b = tmp_path / "real-agents-b"
    (real_a / "skills").mkdir(parents=True)
    (real_b / "skills").mkdir(parents=True)
    dot_agents = project / ".agents"
    dot_agents.symlink_to(real_a, target_is_directory=True)

    record = publish.freeze_publication_record(project, csk_home)
    live = project / ".agents" / "skills" / "review"
    key = (publish.CLASS_CONTEXT, "project/review")
    payloads = publish.build_recheck_payloads(
        record=record,
        project_path=project,
        home=csk_home,
        staged_specs=(
            publish.TargetSpec(
                target_class=key[0],
                identifier=key[1],
                live_path=live,
                kind="entry",
                staged=None,
            ),
        ),
        live_digests={key: ABSENT_DIGEST},
        admitted=frozenset(),
    )
    payload = payloads[key]
    assert payload["ancestors"] is None

    hook = publish.make_publication_hook()
    hook(_hook_target(key[0], key[1], live, payload))

    dot_agents.unlink()
    dot_agents.symlink_to(real_b, target_is_directory=True)
    with pytest.raises(SourceError) as excinfo:
        hook(_hook_target(key[0], key[1], live, payload))
    assert excinfo.value.code == "source_output_overlap"


def test_install_succeeds_when_csk_home_is_a_stable_link(
    tmp_path: Path, csk_home: Path
) -> None:
    """A symlinked csk home installs, resolving like the engine does."""

    import shutil

    from csk import locking

    real_home = tmp_path / "real-home"
    locking.provision_new_manager_home(real_home)
    shutil.rmtree(csk_home)
    csk_home.symlink_to(real_home, target_is_directory=True)

    project, cfg, _members = _basic_fixture(tmp_path, csk_home)
    result = _install_ok(cfg)
    assert any("review installed" in message for message in result.messages)
    assert (real_home / "source-v1" / "snapshots").is_dir()
    assert _collect(cfg).clean
    _assert_no_journal(csk_home)


# ---------------------------------------------------------------------------
# Conflict and error families
# ---------------------------------------------------------------------------


def test_locked_install_refuses_folded_duplicate_lock_names(
    tmp_path: Path, csk_home: Path
) -> None:
    """A crafted lock with case-colliding names refuses name-conflict."""

    project, cfg, _members = _basic_fixture(tmp_path, csk_home)
    _install_ok(cfg)
    lock = _read_lock(project)
    assert lock is not None
    member = lock.members[0]
    assert isinstance(member.package, LocalSnapshot)
    manifest_data = json.loads(
        (project / "Skillfile.json").read_text(encoding="utf-8")
    )
    forged = lock_module.create_lock(
        manifest_data,
        (
            lock_module.LockMember(
                name="REVIEW",
                selection=0,
                directory=member.directory,
                package=member.package,
                content_sha256=member.content_sha256,
            ),
            lock_module.LockMember(
                name="review",
                selection=1,
                directory=member.directory,
                package=member.package,
                content_sha256=member.content_sha256,
            ),
        ),
    )
    lock_path = project / publish.SKILLFILE_LOCK_NAME
    lock_path.write_bytes(lock_module.serialize_lock(forged))

    _install_failed(cfg, "source_name_conflict")
    assert lock_path.read_bytes() == lock_module.serialize_lock(forged)
    assert not _collect(cfg).clean
    _assert_no_journal(csk_home)


@pytest.mark.skipif(os.name == "nt", reason="POSIX permission bits required")
@pytest.mark.skipif(
    hasattr(os, "geteuid") and os.geteuid() == 0,
    reason="root bypasses permission bits",
)
@pytest.mark.parametrize(
    "location", ["context-destination", "home-bindings"]
)
def test_unreadable_destination_refuses_structured(
    tmp_path: Path, csk_home: Path, location: str
) -> None:
    """Unreadable destinations fail structured, never a raw permission error.

    A project managed root trips the installer's generation probe
    first; a home bindings root (outside the probe) trips this leaf's
    own inspection mapping. Both refuse with a stable code.
    """

    import shutil

    project, cfg, members = _basic_fixture(tmp_path, csk_home)
    names = [name for name, _ in members]
    if location == "context-destination":
        target = project / ".agents" / "skills" / "review"
        target.mkdir(parents=True)
        (target / "keep.txt").write_text("keep\n", encoding="utf-8")
        before = _published_digests(project, csk_home, names)
        sources_before = _source_snapshots(project, members, csk_home)
        target.chmod(0o000)
        try:
            _install_failed(cfg, "generation_unreadable")
            _assert_no_journal(csk_home)
        finally:
            target.chmod(0o755)
        assert (target / "keep.txt").read_text(encoding="utf-8") == "keep\n"
        live = _published_digests(project, csk_home, names)
        assert _published_without_store(live) == _published_without_store(before)
        assert _source_snapshots(project, members, csk_home) == sources_before
        assert not (project / publish.SKILLFILE_LOCK_NAME).exists()
        # Positive control: the same fixture installs once readable.
        shutil.rmtree(target)
        assert _install_ok(cfg)
        assert _collect(cfg).clean
    else:
        _install_ok(cfg)
        bindings_root = next((csk_home / "source-v1" / "bindings").iterdir())
        before = _published_digests(project, csk_home, names)
        bindings_root.chmod(0o000)
        try:
            _install_failed(cfg, "source_output_overlap", fetch=True)
            _assert_no_journal(csk_home)
        finally:
            bindings_root.chmod(0o755)
        live = _published_digests(project, csk_home, names)
        assert _published_without_store(live) == _published_without_store(before)
        _assert_store_residue_verifies(
            csk_home,
            _expected_store_entries(project, members, csk_home),
            exact=True,
        )
        # Positive control: the same fixture refreshes once readable.
        assert _install_ok(cfg, fetch=True)
        assert _collect(cfg).clean


# ---------------------------------------------------------------------------
# Revision 2: cross-project runtime, ledger shapes, digest errors, rollback
# diagnostics (review F-A .. F-D)
# ---------------------------------------------------------------------------


def test_unregistered_sibling_runtime_survives_install_and_status(
    tmp_path: Path, csk_home: Path, monkeypatch: pytest.MonkeyPatch, capsys: Any
) -> None:
    """A no-op install never deletes another project's runtime (F-A).

    Two schema-2 projects share one home and both install clean. After
    B is unregistered (its lock and outputs stay on disk), A's status
    stays clean, A's locked install is a no-op, and B's runtime entry
    is byte-identical with B's own status still clean. The removal set
    is the installing project's lock diff, never a live-home sweep.
    """

    # Manual two-project setup bypasses the shared v2 fixtures above.
    if not _selection_fs.supports_descriptor_traversal():
        pytest.skip(_selection_fs.NO_DESCRIPTOR_TRAVERSAL_REASON)
    skills_root = tmp_path / "skills"
    project_a = make_project(tmp_path, "pa")
    project_b = make_project(tmp_path, "pb")
    _write_skill(project_a / "agents" / "skills" / "alpha", "alpha")
    _write_skill(project_b / "agents" / "skills" / "gamma", "gamma")
    _write_manifest_v2(project_a, [("alpha", "agents/skills/alpha")])
    _write_manifest_v2(project_b, [("gamma", "agents/skills/gamma")])
    commit_all(project_a, "a")
    commit_all(project_b, "b")
    cfg_a = _v2_config(csk_home, skills_root, project_a)
    app = cfg_a.projects["app"]
    cfg_both = replace(
        cfg_a,
        projects={**cfg_a.projects, "pb": replace(app, alias="pb", path=project_b)},
    )
    _install_ok(cfg_both, "app")
    _install_ok(cfg_both, "pb")
    assert _collect(cfg_both, "app").clean
    assert _collect(cfg_both, "pb").clean

    lock_b = _read_lock(project_b)
    assert lock_b is not None
    package_b = lock_b.members[0].package
    assert isinstance(package_b, LocalSnapshot)
    gamma_rt = publish.runtime_entry_path(
        csk_home, "gamma", package_identity_sha256(package_b)
    )
    assert gamma_rt.is_dir()
    gamma_before = digest_target(gamma_rt, kind="entry")
    home_before = _published_digests(project_a, csk_home, ["alpha"])

    # B unregistered: A is untouched and current, and stays so.
    assert _collect(cfg_a, "app").clean
    config.save_config(cfg_a)
    monkeypatch.setenv("CSK_CONFIG", str(cfg_a.path))
    assert cli.main(["status", str(project_a), "--check"]) == cli.EXIT_OK
    capsys.readouterr()

    result = _install_ok(cfg_a, "app")
    assert any("alpha up-to-date" in message for message in result.messages)
    assert gamma_rt.is_dir()
    assert digest_target(gamma_rt, kind="entry") == gamma_before
    assert _published_digests(project_a, csk_home, ["alpha"]) == home_before
    assert _collect(cfg_both, "pb").clean
    _assert_no_journal(csk_home)


@pytest.mark.parametrize("shape", ["dir", "symlink"])
def test_adapter_ledger_nonregular_shapes_refuse(
    tmp_path: Path, csk_home: Path, shape: str
) -> None:
    """A non-regular ledger path refuses overlap with user bytes intact (F-B).

    Class over the live shapes the shared planner would silently
    replace: a directory holding a user file and a symlink to an
    outside file. The schema-2 translation refuses before any write;
    the shared planner and the legacy lane are deliberately untouched.
    """

    import shutil

    project, cfg, members = _basic_fixture(tmp_path, csk_home)
    names = [name for name, _ in members]
    ledger = project / ".codex" / "skills" / ".csk-managed.json"
    ledger.parent.mkdir(parents=True)
    outside = tmp_path / "outside-ledger.txt"
    if shape == "dir":
        ledger.mkdir()
        (ledger / "user-file.txt").write_text("mine\n", encoding="utf-8")
        stamp = digest_path(ledger)
    else:
        outside.write_text("mine\n", encoding="utf-8")
        ledger.symlink_to(outside)
        stamp = os.readlink(ledger)
    before = _published_digests(project, csk_home, names)
    sources_before = _source_snapshots(project, members, csk_home)

    _install_failed(cfg, "source_output_overlap")

    if shape == "dir":
        assert ledger.is_dir() and not ledger.is_symlink()
        assert (ledger / "user-file.txt").read_text(encoding="utf-8") == "mine\n"
        assert digest_path(ledger) == stamp
    else:
        assert ledger.is_symlink()
        assert os.readlink(ledger) == stamp
        assert outside.read_text(encoding="utf-8") == "mine\n"
    _assert_no_journal(csk_home)
    live = _published_digests(project, csk_home, names)
    assert _published_without_store(live) == _published_without_store(before)
    assert _source_snapshots(project, members, csk_home) == sources_before
    assert not (project / publish.SKILLFILE_LOCK_NAME).exists()
    # Positive control: removing the obstacle installs clean.
    if shape == "dir":
        shutil.rmtree(ledger)
    else:
        ledger.unlink()
    assert _install_ok(cfg)
    assert _collect(cfg).clean


@pytest.mark.parametrize(
    ("root", "code"),
    [
        ("codex-skills", "source_output_overlap"),
        ("codex", "generation_unreadable"),
        ("agents-skills", "source_output_overlap"),
        ("agents", "generation_unreadable"),
    ],
)
def test_managed_root_as_file_refuses_structured(
    tmp_path: Path, csk_home: Path, root: str, code: str
) -> None:
    """A managed root that is a file refuses structured with no raw error.

    Class over the ENOTDIR shapes at adapter and context roots: the
    file survives, the refusal is structured, and nothing is
    published. Subdirectory roots reach this leaf's own digest seam
    (``source_output_overlap``); top-level roots trip the pre-existing
    generation probe first (``generation_unreadable``). The same
    fixture installs once the file is removed (positive control).
    """

    project, cfg, members = _basic_fixture(tmp_path, csk_home)
    target = {
        "codex-skills": project / ".codex" / "skills",
        "codex": project / ".codex",
        "agents-skills": project / ".agents" / "skills",
        "agents": project / ".agents",
    }[root]
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text("file\n", encoding="utf-8")
    project_before = _tree_listing(project)
    sources_before = _source_snapshots(project, members, csk_home)

    _install_failed(cfg, code)

    assert target.is_file() and not target.is_symlink()
    assert target.read_text(encoding="utf-8") == "file\n"
    _assert_no_journal(csk_home)
    assert _tree_listing(project) == project_before
    if code == "source_output_overlap":
        _assert_store_residue_verifies(
            csk_home, _expected_store_entries(project, members, csk_home), exact=True
        )
    else:
        # The generation probe refuses before snapshot staging runs.
        assert not (csk_home / "source-v1" / "snapshots").exists()
    assert _source_snapshots(project, members, csk_home) == sources_before
    assert not (project / publish.SKILLFILE_LOCK_NAME).exists()
    assert _engine_temporaries(project) == []
    assert _engine_temporaries(csk_home / "source-v1") == []
    target.unlink()
    assert _install_ok(cfg)
    assert _collect(cfg).clean


def _digest_fault(error: str, victim: Path) -> OSError:
    """Build one catalog filesystem error naming the digest victim."""

    import errno

    if error == "not-a-directory":
        return NotADirectoryError(20, "Not a directory", os.fspath(victim))
    if error == "is-a-directory":
        return IsADirectoryError(21, "Is a directory", os.fspath(victim))
    return OSError(
        errno.ELOOP, "Too many levels of symbolic links", os.fspath(victim)
    )


@pytest.mark.parametrize("error", ["not-a-directory", "is-a-directory", "loop"])
def test_live_digest_failures_refuse_structured(
    tmp_path: Path, csk_home: Path, monkeypatch: pytest.MonkeyPatch, error: str
) -> None:
    """Filesystem errors at the live-digest seam refuse overlap (F-C).

    Class over the catalog's ``NotADirectoryError`` /
    ``IsADirectoryError`` / ``ELOOP`` injections at the real digest
    call site, with a reached-assertion and a positive control proving
    the same fixture installs unfaulted.
    """

    project, cfg, members = _basic_fixture(tmp_path, csk_home)
    names = [name for name, _ in members]
    victim = project / ".agents" / "skills" / "review"
    fault = _digest_fault(error, victim)
    real_digest = publish.digest_target
    fired: list[str] = []

    def raising(path: Path, *, kind: Any = "bytes") -> str:
        if path == victim:
            fired.append(str(path))
            raise fault
        return real_digest(path, kind=kind)

    monkeypatch.setattr(publish, "digest_target", raising)
    before = _published_digests(project, csk_home, names)
    sources_before = _source_snapshots(project, members, csk_home)

    _install_failed(cfg, "source_output_overlap")

    assert fired, "injected fault never reached the digest seam"
    _assert_no_journal(csk_home)
    live = _published_digests(project, csk_home, names)
    assert _published_without_store(live) == _published_without_store(before)
    assert _source_snapshots(project, members, csk_home) == sources_before
    assert not (project / publish.SKILLFILE_LOCK_NAME).exists()
    # Positive control: the same fixture installs once unfaulted.
    monkeypatch.setattr(publish, "digest_target", real_digest)
    assert _install_ok(cfg)
    assert _collect(cfg).clean


@pytest.mark.parametrize("error", ["not-a-directory", "is-a-directory", "loop"])
def test_status_survives_live_digest_failures_without_writing(
    tmp_path: Path, csk_home: Path, monkeypatch: pytest.MonkeyPatch, error: str
) -> None:
    """Status reports non-current (never crashes) when live is unreadable.

    Same error class as the install seam, at the status comparison
    seam: the tree hashes before and after are equal and the
    installation reads non-current.
    """

    project, cfg, _members = _basic_fixture(tmp_path, csk_home)
    _install_ok(cfg)
    victim = project / ".agents" / "skills" / "review"
    fault = _digest_fault(error, victim)
    real_digest = publish.digest_target
    fired: list[str] = []

    def raising(path: Path, *, kind: Any = "bytes") -> str:
        if path == victim:
            fired.append(str(path))
            raise fault
        return real_digest(path, kind=kind)

    monkeypatch.setattr(publish, "digest_target", raising)
    project_before = _tree_listing(project)
    home_before = _tree_listing(csk_home)
    collected = _collect(cfg)
    assert fired, "injected fault never reached the status seam"
    assert not collected.clean
    assert _tree_listing(project) == project_before
    assert _tree_listing(csk_home) == home_before
    # Positive control: unfaulted status is clean again.
    monkeypatch.setattr(publish, "digest_target", real_digest)
    assert _collect(cfg).clean


@pytest.mark.parametrize("root", ["codex-skills", "agents-skills"])
def test_status_survives_managed_root_replaced_by_file(
    tmp_path: Path,
    csk_home: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: Any,
    root: str,
) -> None:
    """Status stays read-only when a managed root becomes a file (F-C).

    After a clean install the adapter or context root is replaced by a
    regular file: status reports non-current without crashing, writes
    nothing, and ``status --check`` exits nonzero.
    """

    import shutil

    project, cfg, _members = _basic_fixture(tmp_path, csk_home)
    _install_ok(cfg)
    config.save_config(cfg)
    monkeypatch.setenv("CSK_CONFIG", str(cfg.path))
    target = {
        "codex-skills": project / ".codex" / "skills",
        "agents-skills": project / ".agents" / "skills",
    }[root]
    shutil.rmtree(target)
    target.write_text("file\n", encoding="utf-8")
    project_before = _tree_listing(project)
    home_before = _tree_listing(csk_home)

    collected = _collect(cfg)
    assert not collected.clean
    assert cli.main(["status", str(project), "--check"]) != cli.EXIT_OK
    capsys.readouterr()
    assert target.is_file() and not target.is_symlink()
    assert _tree_listing(project) == project_before
    assert _tree_listing(csk_home) == home_before


@pytest.mark.parametrize(
    "shape",
    ["lock-leaf-link", "context-parent-link-in-window", "bindings-parent-link"],
)
def test_boundary_move_between_writes_reports_overlap(
    tmp_path: Path, csk_home: Path, monkeypatch: pytest.MonkeyPatch, shape: str
) -> None:
    """Every rollback-side boundary move still reports overlap (F-D).

    Class over the three shapes where the move also breaks the
    rollback's own digests: the refusal reaching the operator is the
    hook's ``source_output_overlap`` diagnostic, nothing is written
    through the moved boundary, and restoring the boundary replays the
    deferred rollback to a byte-identical tree.
    """

    store_before: dict[Path, tuple[str, str, str]] = {}
    if shape == "context-parent-link-in-window":
        project, cfg, members = _collection_fixture(tmp_path, csk_home)
        _install_ok(cfg)
        store_before = _expected_store_entries(project, members, csk_home)
        (project / "agents" / "skills" / "review" / "scripts" / "tool.sh").write_text(
            "#!/bin/sh\necho v2\n", encoding="utf-8"
        )
        fetch = True
    elif shape == "bindings-parent-link":
        project, cfg, members = _collection_fixture(tmp_path, csk_home)
        extra = project / "extra-src"
        _write_skill(extra / "gamma", "gamma")
        doc = json.loads((project / "Skillfile.json").read_text(encoding="utf-8"))
        doc["sources"]["extra"] = {"path": "./extra-src"}
        doc["skills"].append({"name": "gamma", "from": "extra", "directory": "gamma"})
        (project / "Skillfile.json").write_text(
            json.dumps(doc, indent=2) + "\n", encoding="utf-8"
        )
        members = [*members, ("gamma", "extra-src/gamma")]
        fetch = False
    else:
        project, cfg, members = _collection_fixture(tmp_path, csk_home)
        fetch = False
    names = [name for name, _ in members]
    lock_path = project / publish.SKILLFILE_LOCK_NAME
    lock_before = lock_path.read_bytes() if lock_path.exists() else None
    before = _published_digests(project, csk_home, names)
    sources_before = _source_snapshots(project, members, csk_home)
    fired: list[str] = []

    if shape == "lock-leaf-link":
        outside = tmp_path / "outside-lock.json"
        outside.write_text("{}\n", encoding="utf-8")

        def mutate(_point: str, _target: JournalTarget | None) -> None:
            lock_path.symlink_to(outside)
            fired.append("lock-link")

        def restore() -> None:
            assert lock_path.is_symlink()
            assert outside.read_text(encoding="utf-8") == "{}\n"
            lock_path.unlink()

        point = "target_committed"
        when = lambda target: target is not None and target.target_class == publish.CLASS_BINDINGS  # noqa: E731
    elif shape == "context-parent-link-in-window":
        outside_dir = tmp_path / "outside-ctx"
        outside_dir.mkdir()

        def mutate(_point: str, _target: JournalTarget | None) -> None:
            skills = project / ".agents" / "skills"
            skills.rename(project / ".agents" / "skills-saved")
            skills.symlink_to(outside_dir, target_is_directory=True)
            fired.append("parent-link")

        def restore() -> None:
            skills = project / ".agents" / "skills"
            assert skills.is_symlink()
            assert list(outside_dir.iterdir()) == []
            skills.unlink()
            (project / ".agents" / "skills-saved").rename(skills)

        point = "after_backup"
        when = lambda target: target is not None and target.target_class == publish.CLASS_CONTEXT  # noqa: E731
    else:
        outside_dir = tmp_path / "outside-bind"
        outside_dir.mkdir()
        swapped: list[Path] = []

        def mutate(_point: str, target: JournalTarget | None) -> None:
            assert target is not None
            slug_dir = Path(target.live_path).parent
            slug_dir.rename(slug_dir.with_name(slug_dir.name + "-saved"))
            slug_dir.symlink_to(outside_dir, target_is_directory=True)
            swapped.append(slug_dir)
            fired.append("bindings-link")

        def restore() -> None:
            victim = swapped[0]
            assert victim.is_symlink()
            assert list(outside_dir.iterdir()) == []
            victim.unlink()
            victim.with_name(victim.name + "-saved").rename(victim)

        point = "target_committed"
        when = lambda target: target is not None and target.target_class == publish.CLASS_BINDINGS  # noqa: E731

    fault = _EngineFault(monkeypatch)
    fault.mutate_once_at(point, mutate, when=when)
    result = _install(cfg, fetch=fetch)
    assert result.status == "failed", result.messages
    assert fired, f"mutation never ran; calls={fault.calls}"
    assert result.errors and result.errors[0].startswith(
        "source_output_overlap:"
    ), result.errors

    journals = _journal_texts(csk_home)
    if journals:
        journal = json.loads(journals[0].read_text(encoding="utf-8"))
        assert journal["phase"] == "rolling_back", journal["phase"]
    restore()
    if journals:
        _recover_clean(csk_home)
    else:
        _assert_no_journal(csk_home)
    if lock_before is None:
        assert not lock_path.exists()
    else:
        assert lock_path.read_bytes() == lock_before
    live = _published_digests(project, csk_home, names)
    assert _published_without_store(live) == _published_without_store(before)
    store_expected = _expected_store_entries(project, members, csk_home)
    store_expected = {**store_before, **store_expected}
    _assert_store_residue_verifies(csk_home, store_expected, exact=True)
    assert _source_snapshots(project, members, csk_home) == sources_before
    assert _engine_temporaries(project) == []
    assert _engine_temporaries(csk_home / "source-v1") == []
