"""Live-path ``root_inputs`` enforcement (BUG-260922-1o40hs).

Every test drives a production entry point in :mod:`csk.sources.selection`
(``resolve_individual``, ``expand_selectors``) or the session read primitive
selection itself calls (``SelectionSession.snapshot_member``): a root
selection without an explicit allowlist for its alias refuses
``source_output_overlap``; a declared allowlist is validated through the
shared ``boundaries`` validator (wired as ``root_inputs_gate``, never
reimplemented); and the member read admits exactly the declared entries.
"""

from __future__ import annotations

import json
import os
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from csk import protocol_json, skillcheck, skillspec
from csk.sources import _selection_fs
from csk.sources import boundaries
from csk.sources import errors as source_errors
from csk.sources import repository_policy
from csk.sources import selection
from csk.sources.selection import expand_selectors, resolve_individual
from csk.sources.skillfile_v2 import IndividualSelector


@pytest.fixture(autouse=True)
def _require_descriptor_traversal(request: pytest.FixtureRequest) -> None:
    if not _selection_fs.supports_descriptor_traversal():
        pytest.skip(_selection_fs.NO_DESCRIPTOR_TRAVERSAL_REASON)


def _write_skill_md(directory: Path, name: str = "review") -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "SKILL.md").write_text(
        f"---\nname: {name}\ndescription: A test skill\n---\n# {name}\n",
        encoding="utf-8",
    )
    return directory


def _policy(entries: dict[str, tuple[str, ...]]) -> repository_policy.RepositoryPolicy:
    return repository_policy.RepositoryPolicy(
        schema_version=1, repositories={}, root_inputs=dict(entries)
    )


def _gate(
    policy: repository_policy.RepositoryPolicy, home: Path
) -> selection.RootInputsGate:
    return boundaries.root_inputs_gate(policy, home)


def _refuses(code: str, func: Callable[[], Any]) -> source_errors.SourceError:
    with pytest.raises(source_errors.SourceError) as excinfo:
        func()
    assert excinfo.value.code == code
    return excinfo.value


def _root_fixture(tmp_path: Path) -> tuple[Path, Path]:
    project = tmp_path / "project"
    home = tmp_path / "home"
    home.mkdir(parents=True)
    return project, home


def _register_manifest_spelling(
    monkeypatch: pytest.MonkeyPatch, spelling: str, grammar: str
) -> None:
    """Register a spelling and regenerate the order derived from that registry."""

    registry = dict(skillspec.MANIFEST_GRAMMAR)
    registry[spelling] = grammar
    monkeypatch.setattr(skillspec, "MANIFEST_GRAMMAR", registry)
    monkeypatch.setattr(skillspec, "MANIFEST_PROBE_ORDER", tuple(registry))


# AC (a): root without an explicit allowlist refuses on the live path.


@pytest.mark.parametrize(
    "entries",
    [{}, {"other": ("SKILL.md",)}, {"local": ()}],
    ids=["no-policy-map", "unknown-alias", "empty-entries"],
)
def test_live_root_without_root_inputs_refuses_output_overlap(
    tmp_path: Path, entries: dict[str, tuple[str, ...]]
) -> None:
    """Production call site: ``selection.resolve_individual`` with ``"."``."""

    project, home = _root_fixture(tmp_path)
    _write_skill_md(project)
    policy = _policy(entries)
    error = _refuses(
        source_errors.CODE_OUTPUT_OVERLAP,
        lambda: resolve_individual(
            project,
            IndividualSelector(name="review", from_alias="local", directory="."),
            policy=policy,
            root_inputs_gate=_gate(policy, home),
        ),
    )
    assert "root_inputs" in error.detail


def test_live_root_without_policy_refuses_output_overlap(tmp_path: Path) -> None:
    """Production call site: ``selection.resolve_individual`` with ``"."``."""

    project, _ = _root_fixture(tmp_path)
    _write_skill_md(project)
    error = _refuses(
        source_errors.CODE_OUTPUT_OVERLAP,
        lambda: resolve_individual(
            project,
            IndividualSelector(name="review", from_alias="local", directory="."),
        ),
    )
    assert "root_inputs" in error.detail


def test_live_root_without_validator_refuses_output_overlap(tmp_path: Path) -> None:
    """An unvalidated allowlist cannot prove separation: fail closed."""

    project, _ = _root_fixture(tmp_path)
    _write_skill_md(project)
    policy = _policy({"local": ("SKILL.md",)})
    error = _refuses(
        source_errors.CODE_OUTPUT_OVERLAP,
        lambda: resolve_individual(
            project,
            IndividualSelector(name="review", from_alias="local", directory="."),
            policy=policy,
            root_inputs_gate=None,
        ),
    )
    assert "validator" in error.detail


def test_live_root_set_level_refusal_threads_through_expand_selectors(
    tmp_path: Path,
) -> None:
    """Production call site: ``selection.expand_selectors`` (set level)."""

    project, home = _root_fixture(tmp_path)
    _write_skill_md(project)
    selector = IndividualSelector(name="review", from_alias="local", directory=".")
    _refuses(
        source_errors.CODE_OUTPUT_OVERLAP,
        lambda: expand_selectors([selector], {"local": project}),
    )
    policy = _policy({"local": ("SKILL.md",)})
    members = expand_selectors(
        [selector],
        {"local": project},
        policy=policy,
        root_inputs_gate=_gate(policy, home),
    )
    assert [(member.name, member.directory) for member in members] == [
        ("review", ".")
    ]


# AC (b): with an allowlist the live path admits exactly the declared inputs.


def test_live_root_with_root_inputs_admits_declared_file(tmp_path: Path) -> None:
    project, home = _root_fixture(tmp_path)
    _write_skill_md(project)
    (project / "notes.txt").write_text("declared\n", encoding="utf-8")
    policy = _policy({"local": ("SKILL.md", "notes.txt")})
    selected = resolve_individual(
        project,
        IndividualSelector(name="review", from_alias="local", directory="."),
        policy=policy,
        root_inputs_gate=_gate(policy, home),
    )
    assert (selected.name, selected.directory) == ("review", ".")


def test_live_root_with_root_inputs_admits_recursive_directory(
    tmp_path: Path,
) -> None:
    project, home = _root_fixture(tmp_path)
    _write_skill_md(project)
    (project / "docs" / "nested").mkdir(parents=True)
    (project / "docs" / "nested" / "deep.txt").write_text("deep\n", encoding="utf-8")
    policy = _policy({"local": ("SKILL.md", "docs")})
    selected = resolve_individual(
        project,
        IndividualSelector(name="review", from_alias="local", directory="."),
        policy=policy,
        root_inputs_gate=_gate(policy, home),
    )
    assert (selected.name, selected.directory) == ("review", ".")


def test_live_root_declared_subtree_link_still_refuses(tmp_path: Path) -> None:
    """A link nested in a declared directory refuses: recursion reads it."""

    project, home = _root_fixture(tmp_path)
    _write_skill_md(project)
    (project / "docs").mkdir()
    (project / "docs" / "evil").symlink_to(project / "SKILL.md")
    policy = _policy({"local": ("SKILL.md", "docs")})
    _refuses(
        source_errors.CODE_MEMBER_INVALID,
        lambda: resolve_individual(
            project,
            IndividualSelector(name="review", from_alias="local", directory="."),
            policy=policy,
            root_inputs_gate=_gate(policy, home),
        ),
    )


def test_live_root_ignores_undeclared_subtree(tmp_path: Path) -> None:
    """Undeclared bytes are never opened: a stray link cannot break selection."""

    project, home = _root_fixture(tmp_path)
    _write_skill_md(project)
    (project / "junk").mkdir()
    (project / "junk" / "stray").symlink_to(project / "SKILL.md")
    (project / "junk" / "garbage.bin").write_bytes(b"\x00\xff not admitted")
    policy = _policy({"local": ("SKILL.md",)})
    selected = resolve_individual(
        project,
        IndividualSelector(name="review", from_alias="local", directory="."),
        policy=policy,
        root_inputs_gate=_gate(policy, home),
    )
    assert (selected.name, selected.directory) == ("review", ".")


def test_live_root_snapshot_contains_exactly_declared(tmp_path: Path) -> None:
    """Production call site: ``SelectionSession.snapshot_member``.

    The read primitive selection itself calls admits a file as itself,
    a directory as its recursive contents, and nothing else.
    """

    project, home = _root_fixture(tmp_path)
    _write_skill_md(project)
    (project / "docs" / "sub").mkdir(parents=True)
    (project / "docs" / "a.txt").write_text("a\n", encoding="utf-8")
    (project / "docs" / "sub" / "b.txt").write_text("b\n", encoding="utf-8")
    (project / "undeclared.txt").write_text("no\n", encoding="utf-8")
    policy = _policy({"local": ("SKILL.md", "docs")})
    gate = _gate(policy, home)
    entries = gate(project, "local", ())
    assert entries == ("SKILL.md", "docs")
    session = _selection_fs.SelectionSession.open(
        project,
        home,
        managed_names=selection.PRUNED_CHILD_NAMES,
        preflight=_selection_fs.PreflightRequest(paths=()),
    )
    with session:
        snapshot = session.snapshot_member(
            session.root, label="'.'", allowlist=entries
        )
    assert set(snapshot.files) == {
        ("SKILL.md",),
        ("docs", "a.txt"),
        ("docs", "sub", "b.txt"),
    }
    assert ("undeclared.txt",) not in snapshot.files


# AC (c): every listed path must exist; missing or unreadable is an error.


def test_live_root_missing_entry_refuses_member_missing(tmp_path: Path) -> None:
    project, home = _root_fixture(tmp_path)
    _write_skill_md(project)
    policy = _policy({"local": ("SKILL.md", "ghost.txt")})
    error = _refuses(
        source_errors.CODE_MEMBER_MISSING,
        lambda: resolve_individual(
            project,
            IndividualSelector(name="review", from_alias="local", directory="."),
            policy=policy,
            root_inputs_gate=_gate(policy, home),
        ),
    )
    assert "ghost.txt" in error.detail


def test_live_root_unreadable_entry_refuses(tmp_path: Path) -> None:
    if os.name == "nt":
        pytest.skip(
            "this permission-bound test uses POSIX geteuid and mode bits; "
            "Windows ACLs differ"
        )
    if os.geteuid() == 0:
        pytest.skip("permission bits do not refuse reads for root")
    project, home = _root_fixture(tmp_path)
    _write_skill_md(project)
    secret = project / "secret.txt"
    secret.write_text("declared\n", encoding="utf-8")
    secret.chmod(0o000)
    try:
        policy = _policy({"local": ("SKILL.md", "secret.txt")})
        _refuses(
            source_errors.CODE_MEMBER_INVALID,
            lambda: resolve_individual(
                project,
                IndividualSelector(name="review", from_alias="local", directory="."),
                policy=policy,
                root_inputs_gate=_gate(policy, home),
            ),
        )
    finally:
        secret.chmod(0o644)


# AC (c): entries are source-relative, portable, link-free, disjoint.


def test_live_root_nonportable_entry_refuses(tmp_path: Path) -> None:
    project, home = _root_fixture(tmp_path)
    _write_skill_md(project)
    policy = _policy({"local": ("SKILL.md", "a/../b")})
    _refuses(
        source_errors.CODE_SELECTION_INVALID,
        lambda: resolve_individual(
            project,
            IndividualSelector(name="review", from_alias="local", directory="."),
            policy=policy,
            root_inputs_gate=_gate(policy, home),
        ),
    )


def test_live_root_link_entry_refuses(tmp_path: Path) -> None:
    project, home = _root_fixture(tmp_path)
    _write_skill_md(project)
    (project / "linked").symlink_to(project / "SKILL.md")
    policy = _policy({"local": ("SKILL.md", "linked")})
    error = _refuses(
        source_errors.CODE_MEMBER_INVALID,
        lambda: resolve_individual(
            project,
            IndividualSelector(name="review", from_alias="local", directory="."),
            policy=policy,
            root_inputs_gate=_gate(policy, home),
        ),
    )
    assert "link" in error.detail


def test_live_root_output_entry_refuses(tmp_path: Path) -> None:
    project, home = _root_fixture(tmp_path)
    _write_skill_md(project)
    (project / ".agents" / "evil").mkdir(parents=True)
    (project / ".agents" / "evil" / "payload.txt").write_text("x\n", encoding="utf-8")
    policy = _policy({"local": ("SKILL.md", ".agents/evil")})
    error = _refuses(
        source_errors.CODE_OUTPUT_OVERLAP,
        lambda: resolve_individual(
            project,
            IndividualSelector(name="review", from_alias="local", directory="."),
            policy=policy,
            root_inputs_gate=_gate(policy, home),
        ),
    )
    assert ".agents" in error.detail


# AC (c): duplicate or overlapping entries fail.


def test_live_root_duplicate_entries_refuse(tmp_path: Path) -> None:
    project, home = _root_fixture(tmp_path)
    _write_skill_md(project)
    policy = _policy({"local": ("SKILL.md", "SKILL.md")})
    error = _refuses(
        source_errors.CODE_SELECTION_INVALID,
        lambda: resolve_individual(
            project,
            IndividualSelector(name="review", from_alias="local", directory="."),
            policy=policy,
            root_inputs_gate=_gate(policy, home),
        ),
    )
    assert "duplicate" in error.detail


def test_live_root_overlapping_entries_refuse(tmp_path: Path) -> None:
    project, home = _root_fixture(tmp_path)
    _write_skill_md(project)
    (project / "docs" / "sub").mkdir(parents=True)
    policy = _policy({"local": ("SKILL.md", "docs", "docs/sub")})
    error = _refuses(
        source_errors.CODE_SELECTION_INVALID,
        lambda: resolve_individual(
            project,
            IndividualSelector(name="review", from_alias="local", directory="."),
            policy=policy,
            root_inputs_gate=_gate(policy, home),
        ),
    )
    assert "overlapping" in error.detail


# AC (c): SKILL.md, the effective manifest and every required input covered.


def test_live_root_omitted_skill_md_refuses(tmp_path: Path) -> None:
    project, home = _root_fixture(tmp_path)
    _write_skill_md(project)
    (project / "docs").mkdir()
    (project / "docs" / "a.txt").write_text("a\n", encoding="utf-8")
    policy = _policy({"local": ("docs",)})
    error = _refuses(
        source_errors.CODE_MEMBER_MISSING,
        lambda: resolve_individual(
            project,
            IndividualSelector(name="review", from_alias="local", directory="."),
            policy=policy,
            root_inputs_gate=_gate(policy, home),
        ),
    )
    assert "SKILL.md" in error.detail


def test_live_root_omitted_canonical_manifest_refuses(tmp_path: Path) -> None:
    project, home = _root_fixture(tmp_path)
    _write_skill_md(project)
    (project / "agent-skill.json").write_text(
        '{"schema_version": 1, "name": "review"}', encoding="utf-8"
    )
    policy = _policy({"local": ("SKILL.md",)})
    error = _refuses(
        source_errors.CODE_MEMBER_MISSING,
        lambda: resolve_individual(
            project,
            IndividualSelector(name="review", from_alias="local", directory="."),
            policy=policy,
            root_inputs_gate=_gate(policy, home),
        ),
    )
    assert "agent-skill.json" in error.detail


def test_live_root_omitted_legacy_manifest_refuses(tmp_path: Path) -> None:
    project, home = _root_fixture(tmp_path)
    _write_skill_md(project)
    (project / "csk-skill.json").write_text(
        '{"schema_version": 1, "name": "review"}', encoding="utf-8"
    )
    policy = _policy({"local": ("SKILL.md",)})
    error = _refuses(
        source_errors.CODE_MEMBER_MISSING,
        lambda: resolve_individual(
            project,
            IndividualSelector(name="review", from_alias="local", directory="."),
            policy=policy,
            root_inputs_gate=_gate(policy, home),
        ),
    )
    assert "csk-skill.json" in error.detail


# BUG-260922-1383no: the probe order is derived from the manifest model, so
# the nested runtime fallback is required input exactly like the top level.


def test_live_root_omitted_runtime_fallback_manifest_refuses(
    tmp_path: Path,
) -> None:
    """Production call site: ``selection.resolve_individual`` with ``"."``.

    The root's only manifest is the nested runtime fallback; the allowlist
    covers ``SKILL.md`` but omits it, so the gate refuses
    ``source_member_missing`` naming ``agents/runtime.json`` -- the same
    refusal the canonical and legacy spellings pin above.
    """

    project, home = _root_fixture(tmp_path)
    _write_skill_md(project)
    (project / "agents").mkdir()
    (project / "agents" / "runtime.json").write_text(
        json.dumps({"commands": {"run": "scripts/run.sh"}}), encoding="utf-8"
    )
    (project / "scripts").mkdir()
    (project / "scripts" / "run.sh").write_text("#!/bin/sh\n", encoding="utf-8")
    policy = _policy({"local": ("SKILL.md",)})
    error = _refuses(
        source_errors.CODE_MEMBER_MISSING,
        lambda: resolve_individual(
            project,
            IndividualSelector(name="review", from_alias="local", directory="."),
            policy=policy,
            root_inputs_gate=_gate(policy, home),
        ),
    )
    assert "agents/runtime.json" in error.detail


def test_live_root_runtime_fallback_admits_when_listed(tmp_path: Path) -> None:
    """The refusal above pivots on the manifest listing, not the shape.

    Production call site: ``selection.resolve_individual`` with ``"."``.
    The same runtime-fallback root with ``agents/runtime.json`` and its
    declared script listed resolves: the probe captured the fallback
    bytes, derived the script, and the gate admitted exactly the
    declared entries.
    """

    project, home = _root_fixture(tmp_path)
    _write_skill_md(project)
    (project / "agents").mkdir()
    (project / "agents" / "runtime.json").write_text(
        json.dumps({"commands": {"run": "scripts/run.sh"}}), encoding="utf-8"
    )
    (project / "scripts").mkdir()
    (project / "scripts" / "run.sh").write_text("#!/bin/sh\n", encoding="utf-8")
    policy = _policy({"local": ("SKILL.md", "agents/runtime.json", "scripts/run.sh")})
    selected = resolve_individual(
        project,
        IndividualSelector(name="review", from_alias="local", directory="."),
        policy=policy,
        root_inputs_gate=_gate(policy, home),
    )
    assert (selected.name, selected.directory) == ("review", ".")


def test_live_root_omitted_runtime_script_refuses(tmp_path: Path) -> None:
    """R1: a runtime-declared script is a required input like any other.

    Production call site: ``selection.resolve_individual`` with ``"."``.
    The root's only manifest is the runtime fallback declaring
    ``scripts/run.sh``; the allowlist covers ``SKILL.md`` and the
    manifest but omits the script, so the gate refuses
    ``source_member_missing`` naming it -- the same refusal a schema
    manifest's declared script pins. Pre-fix this root admitted.
    """

    project, home = _root_fixture(tmp_path)
    _write_skill_md(project)
    (project / "agents").mkdir()
    (project / "agents" / "runtime.json").write_text(
        json.dumps({"commands": {"run": "scripts/run.sh"}}), encoding="utf-8"
    )
    (project / "scripts").mkdir()
    (project / "scripts" / "run.sh").write_text("#!/bin/sh\n", encoding="utf-8")
    policy = _policy({"local": ("SKILL.md", "agents/runtime.json")})
    error = _refuses(
        source_errors.CODE_MEMBER_MISSING,
        lambda: resolve_individual(
            project,
            IndividualSelector(name="review", from_alias="local", directory="."),
            policy=policy,
            root_inputs_gate=_gate(policy, home),
        ),
    )
    assert "scripts/run.sh" in error.detail


@pytest.mark.parametrize(
    ("spelling", "anomaly"),
    [
        ("agent-skill.json", "link"),
        ("agents/runtime.json", "link"),
        ("agent-skill.json", "directory"),
        ("agents/runtime.json", "directory"),
        ("agent-skill.json", "hardlink"),
        ("agents/runtime.json", "hardlink"),
    ],
    ids=[
        "top-link",
        "nested-leaf-link",
        "top-directory",
        "nested-leaf-directory",
        "top-hardlink",
        "nested-leaf-hardlink",
    ],
)
def test_live_root_manifest_probe_anomaly_refuses_top_and_nested(
    tmp_path: Path, spelling: str, anomaly: str
) -> None:
    """The nested probe refuses the same leaf anomalies the top level does.

    Production call site: ``selection.resolve_individual`` with ``"."``.
    A link, a directory, or a hard link where a manifest file belongs
    refuses ``source_member_invalid`` from the probe itself, whether the
    spelling is top-level or nested.
    """

    project, home = _root_fixture(tmp_path)
    _write_skill_md(project)
    target = project / spelling
    target.parent.mkdir(parents=True, exist_ok=True)
    if anomaly == "link":
        real = project / "real.json"
        real.write_text('{"schema_version": 1, "name": "review"}', encoding="utf-8")
        target.symlink_to(real)
    elif anomaly == "directory":
        target.mkdir()
    else:
        real = project / "real.json"
        real.write_text('{"schema_version": 1, "name": "review"}', encoding="utf-8")
        os.link(real, target)
    policy = _policy({"local": ("SKILL.md",)})
    error = _refuses(
        source_errors.CODE_MEMBER_INVALID,
        lambda: resolve_individual(
            project,
            IndividualSelector(name="review", from_alias="local", directory="."),
            policy=policy,
            root_inputs_gate=_gate(policy, home),
        ),
    )
    assert "manifest probe" in error.detail


@pytest.mark.parametrize("link_points_at_manifest", [False, True])
def test_live_root_manifest_probe_intermediate_link_refuses(
    tmp_path: Path, link_points_at_manifest: bool
) -> None:
    """A link at ``agents/`` could conceal the fallback: refuse, never follow.

    Production call site: ``selection.resolve_individual`` with ``"."``.
    Whether the link dangles or points at a real directory carrying
    ``runtime.json``, the probe refuses ``source_member_invalid`` -- the
    pointed-at case proves the outside bytes are never read as the
    manifest (a followed read would refuse ``source_member_missing``
    naming the spelling instead, or admit).
    """

    project, home = _root_fixture(tmp_path)
    _write_skill_md(project)
    if link_points_at_manifest:
        outside = tmp_path / "outside"
        (outside / "agents-real").mkdir(parents=True)
        (outside / "agents-real" / "runtime.json").write_text(
            json.dumps({"commands": {"run": "scripts/run.sh"}}), encoding="utf-8"
        )
        (project / "agents").symlink_to(outside / "agents-real")
    else:
        (project / "agents").symlink_to(project / "nowhere")
    policy = _policy({"local": ("SKILL.md",)})
    error = _refuses(
        source_errors.CODE_MEMBER_INVALID,
        lambda: resolve_individual(
            project,
            IndividualSelector(name="review", from_alias="local", directory="."),
            policy=policy,
            root_inputs_gate=_gate(policy, home),
        ),
    )
    assert "rejected link" in error.detail


@pytest.mark.parametrize(
    "layout",
    ["no-agents-dir", "agents-dir-without-manifest", "agents-regular-file"],
)
def test_live_root_manifest_probe_absent_nested_skips(tmp_path: Path, layout: str) -> None:
    """The nested probe treats absence the same way the top level does.

    Production call site: ``selection.resolve_individual`` with ``"."``.
    A missing ``agents/`` directory, an ``agents/`` directory without the
    fallback, and a regular file where ``agents/`` would be all mean the
    fallback is certainly absent (the file case is the ``ENOTDIR`` the
    file read itself already reports as missing), so ``("SKILL.md",)``
    admits the manifest-free package instead of refusing.
    """

    project, home = _root_fixture(tmp_path)
    _write_skill_md(project)
    if layout == "agents-dir-without-manifest":
        (project / "agents").mkdir()
        (project / "agents" / "notes.txt").write_text("not a manifest\n", encoding="utf-8")
    elif layout == "agents-regular-file":
        (project / "agents").write_text("not a directory\n", encoding="utf-8")
    policy = _policy({"local": ("SKILL.md",)})
    selected = resolve_individual(
        project,
        IndividualSelector(name="review", from_alias="local", directory="."),
        policy=policy,
        root_inputs_gate=_gate(policy, home),
    )
    assert (selected.name, selected.directory) == ("review", ".")


@pytest.mark.parametrize(
    "omitted",
    ["agent-skill.json", "agents/runtime.json"],
    ids=["omit-canonical", "omit-runtime-fallback"],
)
def test_live_root_canonical_and_runtime_fallback_both_required(
    tmp_path: Path, omitted: str
) -> None:
    """Every spelling present is required input, not just the effective one.

    Production call site: ``selection.resolve_individual`` with ``"."``.
    With both the canonical manifest and the runtime fallback present,
    omitting either from the allowlist refuses ``source_member_missing``
    naming the omitted spelling.
    """

    project, home = _root_fixture(tmp_path)
    _write_skill_md(project)
    (project / "agent-skill.json").write_text(
        '{"schema_version": 1, "name": "review"}', encoding="utf-8"
    )
    (project / "agents").mkdir()
    (project / "agents" / "runtime.json").write_text(
        json.dumps({"commands": {"run": "scripts/run.sh"}}), encoding="utf-8"
    )
    (project / "scripts").mkdir()
    (project / "scripts" / "run.sh").write_text("#!/bin/sh\n", encoding="utf-8")
    covered = "agents/runtime.json" if omitted == "agent-skill.json" else "agent-skill.json"
    policy = _policy({"local": ("SKILL.md", covered)})
    error = _refuses(
        source_errors.CODE_MEMBER_MISSING,
        lambda: resolve_individual(
            project,
            IndividualSelector(name="review", from_alias="local", directory="."),
            policy=policy,
            root_inputs_gate=_gate(policy, home),
        ),
    )
    assert omitted in error.detail


def test_live_root_effective_manifest_is_first_spelling_found(
    tmp_path: Path,
) -> None:
    """Required inputs derive from the first spelling found, not the last.

    Production call site: ``selection.resolve_individual`` with ``"."``.
    The canonical manifest declares ``runtime_roots=["scripts"]`` and the
    runtime fallback is also present; the allowlist covers both manifests
    but omits the declared root, so the gate refuses naming ``scripts``.
    Had the fallback bytes been effective, the required set would lack
    the root and the package would admit.
    """

    project, home = _root_fixture(tmp_path)
    _write_skill_md(project)
    (project / "scripts").mkdir()
    (project / "scripts" / "run.sh").write_text("#!/bin/sh\n", encoding="utf-8")
    (project / "agent-skill.json").write_text(
        '{"schema_version": 2, "runtime_roots": ["scripts"],'
        ' "commands": {"run": {"type": "script", "unix_path": "scripts/run.sh"}}}',
        encoding="utf-8",
    )
    (project / "agents").mkdir()
    (project / "agents" / "runtime.json").write_text(
        json.dumps({"commands": {"run": "scripts/run.sh"}}), encoding="utf-8"
    )
    policy = _policy({"local": ("SKILL.md", "agent-skill.json", "agents/runtime.json")})
    error = _refuses(
        source_errors.CODE_MEMBER_MISSING,
        lambda: resolve_individual(
            project,
            IndividualSelector(name="review", from_alias="local", directory="."),
            policy=policy,
            root_inputs_gate=_gate(policy, home),
        ),
    )
    assert "scripts" in error.detail


def _spelling_covered_by_probe(base: Path, home: Path, spelling: str) -> bool:
    """True when the live path refuses a single-manifest root naming it.

    Builds ``base`` with SKILL.md plus exactly one manifest spelling, then
    drives ``selection.resolve_individual`` with ``root_inputs=("SKILL.md",)``:
    covered means the probe found the spelling and the gate refused
    ``source_member_missing`` naming it. Admission (or any other outcome)
    means uncovered. The payload bytes are fixed: name coverage needs
    readable bytes, not a valid manifest of that spelling's own grammar.
    """

    _write_skill_md(base)
    target = base / spelling
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(b'{"schema_version": 1, "name": "review"}')
    policy = _policy({"local": ("SKILL.md",)})
    try:
        resolve_individual(
            base,
            IndividualSelector(name="review", from_alias="local", directory="."),
            policy=policy,
            root_inputs_gate=boundaries.root_inputs_gate(policy, home),
        )
    except source_errors.SourceError as exc:
        return exc.code == source_errors.CODE_MEMBER_MISSING and spelling in exc.detail
    return False


def _assert_probe_covers_spellings(
    tmp_path: Path, home: Path, spellings: tuple[str, ...]
) -> None:
    uncovered = [
        spelling
        for index, spelling in enumerate(spellings)
        if not _spelling_covered_by_probe(tmp_path / f"case{index}", home, spelling)
    ]
    assert not uncovered, (
        "root-input probe does not cover manifest spelling(s): " + ", ".join(uncovered)
    )


def test_manifest_probe_order_is_derived_from_registry() -> None:
    assert skillspec.MANIFEST_PROBE_ORDER == tuple(skillspec.MANIFEST_GRAMMAR)


def test_manifest_probe_covers_every_skillspec_spelling(tmp_path: Path) -> None:
    """Growth test: the probe covers every spelling the model resolves.

    Production call site: ``selection.resolve_individual`` with ``"."``,
    once per spelling in the grammar registry. The expected set comes
    from ``MANIFEST_GRAMMAR`` while the production probe consumes the
    separately derived ``MANIFEST_PROBE_ORDER``. The comparison is by
    identity, so it names each uncovered spelling. A bare length
    comparison would pass the day someone adds one spelling and removes
    another.
    """

    _, home = _root_fixture(tmp_path)
    _assert_probe_covers_spellings(
        tmp_path, home, tuple(skillspec.MANIFEST_GRAMMAR)
    )


def test_manifest_probe_growth_check_names_an_uncovered_spelling(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Proof the growth test is not vacuous: an uncovered spelling fails it.

    Simulates ``MANIFEST_GRAMMAR`` gaining ``agents/v2.json`` while the
    exported probe order is stale. The same coverage assertion as the live
    growth test must fail and name the uncovered registry spelling. Pre-fix,
    ``agents/runtime.json`` behaved exactly like this (admitted with
    ``("SKILL.md",)``); post-fix it is covered.
    """

    _, home = _root_fixture(tmp_path)
    spelling = "agents/v2.json"
    registry = dict(skillspec.MANIFEST_GRAMMAR)
    registry[spelling] = skillspec.MANIFEST_GRAMMAR_RUNTIME
    monkeypatch.setattr(skillspec, "MANIFEST_GRAMMAR", registry)
    with pytest.raises(AssertionError, match="agents/v2.json"):
        _assert_probe_covers_spellings(tmp_path, home, tuple(registry))


@pytest.mark.parametrize(
    ("spelling", "payload"),
    [
        (
            "extra-manifest.json",
            {"schema_version": 1, "commands": {"extra": {"type": "system", "command": "extra"}}},
        ),
        ("agents/v2.json", {"commands": {"run": "scripts/run.sh"}}),
    ],
    ids=["schema", "runtime"],
)
def test_load_skill_spec_honours_registered_spelling(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, spelling: str, payload: dict[str, Any]
) -> None:
    """R2: the registry controls loading, not just the probe and diagnostics.

    Production call site: ``skillspec.load_skill_spec``. A spelling
    registered with a grammar is honoured by the resolver: the
    diagnostic selector sees it and the loader returns it as
    ``source_file`` with its commands.
    Pre-fix the loader held a private if-chain, so a newly registered
    spelling was seen by diagnostics while the loader returned
    ``source_file=None`` and zero commands.
    """

    grammar = (
        skillspec.MANIFEST_GRAMMAR_RUNTIME
        if spelling == "agents/v2.json"
        else skillspec.MANIFEST_GRAMMAR_SCHEMA
    )
    _register_manifest_spelling(monkeypatch, spelling, grammar)
    target = tmp_path / spelling
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(payload), encoding="utf-8")
    assert skillspec.manifest_source_path(tmp_path) == spelling
    spec = skillspec.load_skill_spec(tmp_path)
    assert spec.source_file == spelling, (
        f"resolver does not honour registered spelling {spelling!r}: "
        f"source_file={spec.source_file!r}"
    )
    assert spec.commands, f"resolver honours {spelling!r} with zero commands"


@pytest.mark.parametrize("removed", skillspec.MANIFEST_PROBE_ORDER)
def test_removing_manifest_spelling_removes_it_from_all_resolvers(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, removed: str
) -> None:
    """The grammar registry, not a private fallback list, controls loading.

    Production call sites: ``skillspec.load_skill_spec``,
    ``skillspec.manifest_source_path``, and ``selection.resolve_individual``
    with ``"."``. Removing one registry entry removes it from the derived
    probe order for all three. A file with a removed spelling is no longer
    treated as an effective manifest or required root input.
    """

    project, home = _root_fixture(tmp_path)
    _write_skill_md(project)
    grammar = skillspec.MANIFEST_GRAMMAR[removed]
    registry = {
        name: value
        for name, value in skillspec.MANIFEST_GRAMMAR.items()
        if name != removed
    }
    monkeypatch.setattr(skillspec, "MANIFEST_GRAMMAR", registry)
    monkeypatch.setattr(skillspec, "MANIFEST_PROBE_ORDER", tuple(registry))
    target = project / removed
    target.parent.mkdir(parents=True, exist_ok=True)
    if grammar == skillspec.MANIFEST_GRAMMAR_RUNTIME:
        target.write_text(
            json.dumps({"commands": {"run": "scripts/run.sh"}}), encoding="utf-8"
        )
    else:
        target.write_text(
            json.dumps(
                {
                    "schema_version": 1,
                    "commands": {"run": {"type": "system", "command": "run"}},
                }
            ),
            encoding="utf-8",
        )

    assert skillspec.manifest_source_path(project) == ""
    spec = skillspec.load_skill_spec(project)
    assert spec.source_file is None
    assert not spec.commands
    policy = _policy({"local": ("SKILL.md",)})
    selected = resolve_individual(
        project,
        IndividualSelector(name="review", from_alias="local", directory="."),
        policy=policy,
        root_inputs_gate=_gate(policy, home),
    )
    assert (selected.name, selected.directory) == ("review", ".")


def test_live_root_grown_runtime_spelling_declared_input_must_be_required(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """G-1 (repeat of R1): a grown runtime spelling derives its inputs.

    Production call sites: ``skillspec.load_skill_spec`` for the loader
    half, ``selection.resolve_individual`` with ``"."`` for the gate
    half. A spelling registered as ``MANIFEST_GRAMMAR_RUNTIME`` has
    runtime grammar; this fixture omits ``schema_version``. The loader
    exposes its commands, and the gate refuses when the allowlist covers
    ``SKILL.md`` and the manifest but omits the declared script. Pre-fix the gate
    dispatched runtime derivation only for ``agents/runtime.json``, so
    the grown spelling went through the schema parser, whose
    missing-schema failure was swallowed into no required inputs, and
    the root admitted -- the exact R1 hole one registry entry over.
    """

    spelling = "agents/v2.json"
    _register_manifest_spelling(
        monkeypatch, spelling, skillspec.MANIFEST_GRAMMAR_RUNTIME
    )
    project, home = _root_fixture(tmp_path)
    _write_skill_md(project)
    (project / "agents").mkdir()
    (project / "agents" / "v2.json").write_text(
        json.dumps({"commands": {"run": "scripts/run.sh"}}), encoding="utf-8"
    )
    (project / "scripts").mkdir()
    (project / "scripts" / "run.sh").write_text("#!/bin/sh\n", encoding="utf-8")
    spec = skillspec.load_skill_spec(project)
    assert spec.source_file == spelling
    assert spec.commands, f"loader honours {spelling!r} with zero commands"
    policy = _policy({"local": ("SKILL.md", spelling)})
    error = _refuses(
        source_errors.CODE_MEMBER_MISSING,
        lambda: resolve_individual(
            project,
            IndividualSelector(name="review", from_alias="local", directory="."),
            policy=policy,
            root_inputs_gate=_gate(policy, home),
        ),
    )
    assert "scripts/run.sh" in error.detail


def test_live_root_grown_schema_effective_inputs_match_loader(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """G-2: loader and gate agree on the effective manifest when grown.

    Production call sites: ``skillspec.load_skill_spec`` for the loader
    half, ``selection.resolve_individual`` with ``"."`` for the gate
    half. With the runtime fallback (declaring nothing) earlier in the
    registry and an appended schema spelling declaring
    ``scripts/run.sh``, the loader selects the schema manifest, and the
    gate derives from that same manifest: the allowlist covering both
    manifests but omitting the script refuses ``source_member_missing``
    naming it. Pre-fix the gate derived from the first present registry
    entry (the runtime bytes) while the loader chose the schema
    manifest, so the root admitted.
    """

    spelling = "extra-manifest.json"
    _register_manifest_spelling(
        monkeypatch, spelling, skillspec.MANIFEST_GRAMMAR_SCHEMA
    )
    project, home = _root_fixture(tmp_path)
    _write_skill_md(project)
    (project / "scripts").mkdir()
    (project / "scripts" / "run.sh").write_text("#!/bin/sh\n", encoding="utf-8")
    (project / "agents").mkdir()
    (project / "agents" / "runtime.json").write_text(
        json.dumps({"commands": {}}), encoding="utf-8"
    )
    (project / spelling).write_text(
        json.dumps(
            {
                "schema_version": 1,
                "commands": {"run": {"type": "script", "unix_path": "scripts/run.sh"}},
            }
        ),
        encoding="utf-8",
    )
    assert skillspec.load_skill_spec(project).source_file == spelling
    assert skillspec.manifest_source_path(project) == spelling
    policy = _policy({"local": ("SKILL.md", "agents/runtime.json", spelling)})
    error = _refuses(
        source_errors.CODE_MEMBER_MISSING,
        lambda: resolve_individual(
            project,
            IndividualSelector(name="review", from_alias="local", directory="."),
            policy=policy,
            root_inputs_gate=_gate(policy, home),
        ),
    )
    assert "scripts/run.sh" in error.detail


def test_runtime_fallback_malformed_message_matches_reference(tmp_path: Path) -> None:
    """G-3: the v1 malformed-runtime diagnostic stays byte-identical.

    Production call sites: ``skillspec.load_skill_spec`` for the loader
    message, ``skillcheck.validate_skill`` for the user-visible forward.
    The source identity (the ``agents/runtime.json`` spelling stored on
    the spec) and the diagnostic label (the full snapshot path shown in
    the malformed-JSON message) are separate values: the message names
    the path, exactly as before the shared bytes parser existed.
    """

    (tmp_path / "agents").mkdir()
    (tmp_path / "agents" / "runtime.json").write_text("{", encoding="utf-8")
    _write_skill_md(tmp_path)
    try:
        protocol_json.loads(b"{")
        raise AssertionError("unreachable: '{' must not parse")
    except protocol_json.ProtocolJSONError as exc:
        suffix = str(exc)
    expected = f"Malformed JSON in {tmp_path / 'agents' / 'runtime.json'}: {suffix}"
    with pytest.raises(skillspec.SkillSpecError) as excinfo:
        skillspec.load_skill_spec(tmp_path)
    assert str(excinfo.value) == expected
    issues = skillcheck.validate_skill(tmp_path)
    actual = next(issue.message for issue in issues if issue.code == "skill.spec_invalid")
    assert actual == expected


def test_live_root_omitted_declared_runtime_root_refuses(tmp_path: Path) -> None:
    """The required set is derived from the effective manifest, not passed in.

    Production call site: ``selection.resolve_individual`` with ``"."``.
    The manifest declares ``runtime_roots=["scripts"]``; the allowlist
    covers ``SKILL.md`` and the manifest but omits the declared root, so
    the gate refuses ``source_member_missing`` naming the root. No caller
    argument carries the required set: revision 2 derives it from the
    manifest bytes read through the member descriptor.
    """

    project, home = _root_fixture(tmp_path)
    _write_skill_md(project)
    (project / "scripts").mkdir()
    (project / "scripts" / "run.sh").write_text("#!/bin/sh\n", encoding="utf-8")
    (project / "agent-skill.json").write_text(
        '{"schema_version": 2, "runtime_roots": ["scripts"],'
        ' "commands": {"run": {"type": "script", "unix_path": "scripts/run.sh"}}}',
        encoding="utf-8",
    )
    policy = _policy({"local": ("SKILL.md", "agent-skill.json")})
    error = _refuses(
        source_errors.CODE_MEMBER_MISSING,
        lambda: resolve_individual(
            project,
            IndividualSelector(name="review", from_alias="local", directory="."),
            policy=policy,
            root_inputs_gate=_gate(policy, home),
        ),
    )
    assert "scripts" in error.detail


def test_live_root_partially_listed_runtime_root_refuses(tmp_path: Path) -> None:
    """Coverage, not membership: a descendant never satisfies a declared root.

    Production call site: ``selection.resolve_individual`` with ``"."``.
    The manifest declares ``runtime_roots=["scripts"]`` and the allowlist
    names ``scripts/run.sh`` -- a file inside the root -- while
    ``scripts/required.dat`` exists in the source but is listed nowhere.
    The listed file would create the ``scripts`` directory in the snapshot
    and satisfy package validation with an incomplete runtime, so the gate
    refuses ``source_member_missing`` naming the uncovered root. Listing
    the root itself (or an ancestor of it) is the only satisfaction.
    """

    project, home = _root_fixture(tmp_path)
    _write_skill_md(project)
    (project / "scripts").mkdir()
    (project / "scripts" / "run.sh").write_text("#!/bin/sh\n", encoding="utf-8")
    (project / "scripts" / "required.dat").write_text(
        "required runtime data\n", encoding="utf-8"
    )
    (project / "agent-skill.json").write_text(
        '{"schema_version": 2, "runtime_roots": ["scripts"],'
        ' "commands": {"run": {"type": "script", "unix_path": "scripts/run.sh"}}}',
        encoding="utf-8",
    )
    policy = _policy({"local": ("SKILL.md", "agent-skill.json", "scripts/run.sh")})
    error = _refuses(
        source_errors.CODE_MEMBER_MISSING,
        lambda: resolve_individual(
            project,
            IndividualSelector(name="review", from_alias="local", directory="."),
            policy=policy,
            root_inputs_gate=_gate(policy, home),
        ),
    )
    assert "scripts" in error.detail


def test_live_root_whole_runtime_root_admits_recursive_contents(
    tmp_path: Path,
) -> None:
    """The whole-root control: listing the root admits its recursive contents.

    Production call sites: ``selection.resolve_individual`` with ``"."``
    for admission, ``SelectionSession.snapshot_member`` (the read primitive
    selection itself calls) for the admitted bytes. The recording gate
    proves the required set carries the model-derived command path and
    ``scripts`` root although no caller passed them; the snapshot proves
    the admitted set contains the root's recursive contents, including
    ``required.dat``.
    """

    project, home = _root_fixture(tmp_path)
    _write_skill_md(project)
    (project / "scripts").mkdir()
    (project / "scripts" / "run.sh").write_text("#!/bin/sh\n", encoding="utf-8")
    (project / "scripts" / "required.dat").write_text(
        "required runtime data\n", encoding="utf-8"
    )
    (project / "agent-skill.json").write_text(
        '{"schema_version": 2, "runtime_roots": ["scripts"],'
        ' "commands": {"run": {"type": "script", "unix_path": "scripts/run.sh"}}}',
        encoding="utf-8",
    )
    policy = _policy({"local": ("SKILL.md", "agent-skill.json", "scripts")})
    inner = _gate(policy, home)
    seen_required: list[tuple[str, ...]] = []
    seen_entries: list[tuple[str, ...]] = []

    def _recording_gate(
        source_root: Path, alias: str, required: tuple[str, ...] = ()
    ) -> tuple[str, ...]:
        seen_required.append(required)
        entries = inner(source_root, alias, required)
        seen_entries.append(entries)
        return entries

    selected = resolve_individual(
        project,
        IndividualSelector(name="review", from_alias="local", directory="."),
        policy=policy,
        root_inputs_gate=_recording_gate,
    )
    assert (selected.name, selected.directory) == ("review", ".")
    assert seen_required == [("agent-skill.json", "scripts/run.sh", "scripts")]
    assert seen_entries == [("SKILL.md", "agent-skill.json", "scripts")]
    session = _selection_fs.SelectionSession.open(
        project,
        home,
        managed_names=selection.PRUNED_CHILD_NAMES,
        preflight=_selection_fs.PreflightRequest(paths=()),
    )
    with session:
        snapshot = session.snapshot_member(
            session.root, label="'.'", allowlist=seen_entries[0]
        )
    assert ("scripts", "run.sh") in snapshot.files
    assert ("scripts", "required.dat") in snapshot.files


def test_live_root_partially_listed_build_root_refuses(tmp_path: Path) -> None:
    """Build roots take the same coverage rule as runtime roots.

    Production call site: ``selection.resolve_individual`` with ``"."``.
    The manifest declares ``build_roots=["build"]`` and the allowlist
    names only ``build/go.mod``; the declared root itself is uncovered,
    so the gate refuses ``source_member_missing`` naming it.
    """

    project, home = _root_fixture(tmp_path)
    _write_skill_md(project)
    (project / "build").mkdir()
    (project / "build" / "go.mod").write_text("module example.org/kit\n", encoding="utf-8")
    (project / "agent-skill.json").write_text(
        '{"schema_version": 6, "capabilities": {}, "build_roots": ["build"],'
        ' "commands": {"tool": {"type": "build", "driver": "go-v1",'
        ' "source_dir": "build"}}}',
        encoding="utf-8",
    )
    policy = _policy({"local": ("SKILL.md", "agent-skill.json", "build/go.mod")})
    error = _refuses(
        source_errors.CODE_MEMBER_MISSING,
        lambda: resolve_individual(
            project,
            IndividualSelector(name="review", from_alias="local", directory="."),
            policy=policy,
            root_inputs_gate=_gate(policy, home),
        ),
    )
    assert "build" in error.detail


def test_live_root_omitted_schema1_command_refuses(tmp_path: Path) -> None:
    """A schema-1 script path is a required runtime input.

    Production call site: ``selection.resolve_individual`` with ``"."``.
    Mirrors the reviewer reproduction (``include_command=False``): the
    manifest declares ``commands.run.unix_path="scripts/run.sh"`` with no
    runtime roots, and the allowlist omits the script, so the gate refuses
    ``source_member_missing`` naming it. Schema 1 carries no exclusion.
    """

    project, home = _root_fixture(tmp_path)
    _write_skill_md(project)
    (project / "scripts").mkdir()
    (project / "scripts" / "run.sh").write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    (project / "agent-skill.json").write_text(
        '{"schema_version": 1,'
        ' "commands": {"run": {"type": "script", "unix_path": "scripts/run.sh"}}}',
        encoding="utf-8",
    )
    policy = _policy({"local": ("SKILL.md", "agent-skill.json")})
    error = _refuses(
        source_errors.CODE_MEMBER_MISSING,
        lambda: resolve_individual(
            project,
            IndividualSelector(name="review", from_alias="local", directory="."),
            policy=policy,
            root_inputs_gate=_gate(policy, home),
        ),
    )
    assert "scripts/run.sh" in error.detail


def test_live_root_schema1_command_admits_when_listed(tmp_path: Path) -> None:
    """The schema-1 positive control: listing the script admits the root.

    Production call site: ``selection.resolve_individual`` with ``"."``.
    Mirrors the reviewer reproduction (``include_command=True``): the same
    manifest with ``scripts/run.sh`` listed resolves, proving the refusal
    above is the missing input and not the schema.
    """

    project, home = _root_fixture(tmp_path)
    _write_skill_md(project)
    (project / "scripts").mkdir()
    (project / "scripts" / "run.sh").write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    (project / "agent-skill.json").write_text(
        '{"schema_version": 1,'
        ' "commands": {"run": {"type": "script", "unix_path": "scripts/run.sh"}}}',
        encoding="utf-8",
    )
    policy = _policy({"local": ("SKILL.md", "agent-skill.json", "scripts/run.sh")})
    selected = resolve_individual(
        project,
        IndividualSelector(name="review", from_alias="local", directory="."),
        policy=policy,
        root_inputs_gate=_gate(policy, home),
    )
    assert (selected.name, selected.directory) == ("review", ".")


def test_live_root_omitted_rootless_script_refuses(tmp_path: Path) -> None:
    """A schema-2 script path without runtime roots is still required.

    Production call site: ``selection.resolve_individual`` with ``"."``.
    No ``runtime_roots`` member means no containing root can cover the
    script, so omitting ``scripts/run.sh`` refuses ``source_member_missing``
    naming it.
    """

    project, home = _root_fixture(tmp_path)
    _write_skill_md(project)
    (project / "scripts").mkdir()
    (project / "scripts" / "run.sh").write_text("#!/bin/sh\n", encoding="utf-8")
    (project / "agent-skill.json").write_text(
        '{"schema_version": 2,'
        ' "commands": {"run": {"type": "script", "unix_path": "scripts/run.sh"}}}',
        encoding="utf-8",
    )
    policy = _policy({"local": ("SKILL.md", "agent-skill.json")})
    error = _refuses(
        source_errors.CODE_MEMBER_MISSING,
        lambda: resolve_individual(
            project,
            IndividualSelector(name="review", from_alias="local", directory="."),
            policy=policy,
            root_inputs_gate=_gate(policy, home),
        ),
    )
    assert "scripts/run.sh" in error.detail


def test_live_root_omitted_win_path_refuses(tmp_path: Path) -> None:
    """A declared ``win_path`` is a required input on every host.

    Production call site: ``selection.resolve_individual`` with ``"."``.
    The manifest declares only ``win_path``; omitting it refuses
    ``source_member_missing`` naming it.
    """

    project, home = _root_fixture(tmp_path)
    _write_skill_md(project)
    (project / "scripts").mkdir()
    (project / "scripts" / "run.cmd").write_text("@echo off\n", encoding="utf-8")
    (project / "agent-skill.json").write_text(
        '{"schema_version": 2,'
        ' "commands": {"run": {"type": "script", "win_path": "scripts/run.cmd"}}}',
        encoding="utf-8",
    )
    policy = _policy({"local": ("SKILL.md", "agent-skill.json")})
    error = _refuses(
        source_errors.CODE_MEMBER_MISSING,
        lambda: resolve_individual(
            project,
            IndividualSelector(name="review", from_alias="local", directory="."),
            policy=policy,
            root_inputs_gate=_gate(policy, home),
        ),
    )
    assert "scripts/run.cmd" in error.detail


def test_live_root_omitted_module_dir_refuses(tmp_path: Path) -> None:
    """A schema-8 declared module directory is a required build input.

    Production call site: ``selection.resolve_individual`` with ``"."``.
    Module directories are disjoint from every root by validity, so no
    listed root can cover ``ext/mod``: omitting it refuses
    ``source_member_missing`` naming it, and listing it admits.
    """

    project, home = _root_fixture(tmp_path)
    _write_skill_md(project)
    (project / "build").mkdir()
    (project / "build" / "go.mod").write_text("module example.org/kit\n", encoding="utf-8")
    (project / "ext" / "mod").mkdir(parents=True)
    (project / "ext" / "mod" / "go.mod").write_text(
        "module example.org/ext\n", encoding="utf-8"
    )
    (project / "agent-skill.json").write_text(
        '{"schema_version": 8, "capabilities": {}, "build_roots": ["build"],'
        ' "commands": {"tool": {"type": "build", "driver": "go-v1",'
        ' "source_dir": "build", "modules": ["ext/mod"]}}}',
        encoding="utf-8",
    )
    policy = _policy({"local": ("SKILL.md", "agent-skill.json", "build")})
    error = _refuses(
        source_errors.CODE_MEMBER_MISSING,
        lambda: resolve_individual(
            project,
            IndividualSelector(name="review", from_alias="local", directory="."),
            policy=policy,
            root_inputs_gate=_gate(policy, home),
        ),
    )
    assert "ext/mod" in error.detail
    admitted = _policy(
        {"local": ("SKILL.md", "agent-skill.json", "build", "ext/mod")}
    )
    selected = resolve_individual(
        project,
        IndividualSelector(name="review", from_alias="local", directory="."),
        policy=admitted,
        root_inputs_gate=_gate(admitted, home),
    )
    assert (selected.name, selected.directory) == ("review", ".")


def test_live_root_exempt_alias_skips_allowlist(tmp_path: Path) -> None:
    """Aliases outside ``root_inputs_aliases`` keep the complete-tree lane.

    Production call site: ``selection.resolve_individual`` with ``"."``.
    The install planner scopes enforcement to path aliases, so a
    materialized non-path root (complete tree, no admitted subset)
    resolves without an allowlist.
    """

    project, _ = _root_fixture(tmp_path)
    _write_skill_md(project)
    selected = resolve_individual(
        project,
        IndividualSelector(name="review", from_alias="up", directory="."),
        root_inputs_aliases=frozenset({"local"}),
    )
    assert (selected.name, selected.directory) == ("review", ".")


# AC (f): the allowlist does not authorize takeover of unmanaged destinations.


def test_live_root_allowlist_does_not_authorize_unmanaged_takeover(
    tmp_path: Path,
) -> None:
    """An admitted root still meets the publication recheck unchanged."""

    project, home = _root_fixture(tmp_path)
    _write_skill_md(project)
    (project / ".agents" / "skills" / "review").mkdir(parents=True)
    (project / "docs" / "evil").mkdir(parents=True)
    policy = _policy({"local": ("SKILL.md",)})
    gate = _gate(policy, home)
    assert gate(project, "local", ()) == ("SKILL.md",)
    record = boundaries.freeze_boundaries(project, home)
    error = _refuses(
        source_errors.CODE_OUTPUT_OVERLAP,
        lambda: boundaries.recheck_publication_destination(
            record,
            project,
            ".agents/skills/review",
            "docs/evil/SKILL.md",
        ),
    )
    assert "outside planned output" in error.detail
