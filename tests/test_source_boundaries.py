"""Boundary enforcement for local path sources (spec section 2).

Drives ``csk.sources.boundaries`` on real temporary-filesystem fixtures:
selected-package gates (broad-root allow plus managed-source,
symlink-managed and case-alias refusals), deterministic discovery
pruning, prune-intersection refusals over selected packages and declared
runtime/build inputs, the operator ``root_inputs`` admission matrix, the
publication-time recheck, and the unmanaged-path protection composition
with ``csk.adapters`` and ``csk.global_bins``.

Instruments: a seam-recording confinement property plus a
``sys.addaudithook`` property (S-FS), fault injection at the real
``os.*`` call sites with positive controls (S-ERRORS), and a
zero-filesystem-effect counter before structural validation (S-POLICY).
"""

from __future__ import annotations

import errno
import ntpath
import os
import stat
import sys
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

import pytest

from csk import adapters, global_bins, shims
from csk.sources import boundaries, repository_policy
from csk.sources.errors import (
    CODE_ALIAS_UNKNOWN,
    CODE_MEMBER_INVALID,
    CODE_MEMBER_MISSING,
    CODE_OUTPUT_OVERLAP,
    CODE_PATH_CONFLICT,
    CODE_SELECTION_INVALID,
    SourceError,
)


@pytest.fixture()
def project_tree(tmp_path: Path) -> tuple[Path, Path]:
    """Build (project, home) with authored and managed subtrees."""

    project = tmp_path / "project"
    home = tmp_path / "home"
    (project / "agents" / "skills" / "review").mkdir(parents=True)
    (project / "agents" / "skills" / "review" / "SKILL.md").write_text(
        "# review\n", encoding="utf-8"
    )
    (project / ".agents" / "skills" / "review").mkdir(parents=True)
    (project / ".agents" / "skills" / "review" / "SKILL.md").write_text(
        "# managed\n", encoding="utf-8"
    )
    home.mkdir(parents=True)
    return (project, home)


def _policy(entries: dict[str, tuple[str, ...]] | None = None) -> (
    repository_policy.RepositoryPolicy
):
    return repository_policy.RepositoryPolicy(
        schema_version=1,
        repositories={},
        root_inputs=dict(entries or {}),
    )


def _freeze(project: Path, home: Path) -> boundaries.BoundaryRecord:
    return boundaries.freeze_boundaries(project, home)


def _refuses(code: str, func: Callable[[], Any]) -> SourceError:
    with pytest.raises(SourceError) as excinfo:
        func()
    assert excinfo.value.code == code
    return excinfo.value


def _filesystem_conflates_case(directory: Path) -> bool:
    probe = directory / "CSK-CASE-PROBE-x7"
    probe.write_text("x", encoding="utf-8")
    try:
        sibling = directory / "csk-case-probe-X7"
        if not sibling.exists():
            return False
        return os.path.samefile(probe, sibling)
    finally:
        probe.unlink()


# Corpus classes through check_selected_package.


def test_broad_root_selected_subdirectory_allowed(project_tree: tuple[Path, Path]) -> None:
    project, home = project_tree
    record = _freeze(project, home)
    policy = _policy()
    resolved = boundaries.check_selected_package(
        record, project, "agents/skills/review", alias="local", policy=policy
    )
    assert resolved == Path(os.path.realpath(project / "agents" / "skills" / "review"))


@pytest.mark.parametrize(
    "directory",
    [
        ".agents",
        ".agents/skills",
        ".agents/skills/review",
        ".claude/skills/review",
        ".codex/skills/review",
        ".gemini/skills/review",
        ".cursor/rules/review",
        ".git",
        ".git/objects",
    ],
)
def test_managed_source_refused(project_tree: tuple[Path, Path], directory: str) -> None:
    project, home = project_tree
    (project / directory).mkdir(parents=True, exist_ok=True)
    record = _freeze(project, home)
    error = _refuses(
        CODE_OUTPUT_OVERLAP,
        lambda: boundaries.check_selected_package(
            record, project, directory, alias="local", policy=_policy()
        ),
    )
    assert ".agents" in error.detail or directory.split("/")[0] in error.detail


@pytest.mark.parametrize(
    "shape", ["first-link", "mid-link", "absolute-link", "relative-link", "link-chain"]
)
def test_symlink_managed_refused(project_tree: tuple[Path, Path], shape: str) -> None:
    project, home = project_tree
    target = project / ".agents" / "skills"
    if shape == "first-link":
        (project / "authored").symlink_to(target, target_is_directory=True)
        directory = "authored/review"
    elif shape == "mid-link":
        (project / "authored").mkdir(exist_ok=True)
        (project / "authored" / "review").symlink_to(target / "review", target_is_directory=True)
        directory = "authored/review"
    elif shape == "absolute-link":
        (project / "authored").mkdir(exist_ok=True)
        (project / "authored" / "review").symlink_to(
            target / "review", target_is_directory=True
        )
        directory = "authored/review"
    elif shape == "relative-link":
        (project / "authored").symlink_to(".agents/skills", target_is_directory=True)
        directory = "authored/review"
    else:
        (project / "hop").symlink_to(target, target_is_directory=True)
        (project / "authored").symlink_to(project / "hop", target_is_directory=True)
        directory = "authored/review"
    record = _freeze(project, home)
    _refuses(
        CODE_OUTPUT_OVERLAP,
        lambda: boundaries.check_selected_package(
            record, project, directory, alias="local", policy=_policy()
        ),
    )


def test_case_alias_refused_or_bound_declared(
    project_tree: tuple[Path, Path], tmp_path: Path
) -> None:
    project, home = project_tree
    if _filesystem_conflates_case(tmp_path):
        # On a conflating filesystem this spelling names the same managed
        # directory the fixture already created as ``.agents``.
        (project / ".AGENTS" / "skills" / "review").mkdir(parents=True, exist_ok=True)
        record = _freeze(project, home)
        _refuses(
            CODE_OUTPUT_OVERLAP,
            lambda: boundaries.check_selected_package(
                record, project, ".AGENTS/skills/review", alias="local", policy=_policy()
            ),
        )
    else:
        # Declared bound: on a case-sensitive filesystem ``.AGENTS`` is a
        # distinct authored directory, not an alias of ``.agents``.
        (project / ".AGENTS" / "skills" / "review").mkdir(parents=True)
        record = _freeze(project, home)
        resolved = boundaries.check_selected_package(
            record, project, ".AGENTS/skills/review", alias="local", policy=_policy()
        )
        assert resolved.is_dir()


def test_case_probe_catches_post_freeze_alias(
    project_tree: tuple[Path, Path], tmp_path: Path
) -> None:
    """The same-parent equivalence probe refuses aliases created after freeze."""

    if not _filesystem_conflates_case(tmp_path):
        pytest.skip("case-alias probe needs a case-insensitive filesystem")
    project, home = project_tree
    record = _freeze(project, home)
    assert all(root.identity is not None for root in record.managed_roots if root.name == ".agents")
    (project / ".agents").rename(project / ".AGENTS-moved")
    (project / ".AGENTS" / "skills" / "review").mkdir(parents=True)
    _refuses(
        CODE_OUTPUT_OVERLAP,
        lambda: boundaries.check_selected_package(
            record, project, ".AGENTS/skills/review", alias="local", policy=_policy()
        ),
    )


@pytest.mark.parametrize("shape", ["first-link", "mid-link", "nested-link"])
def test_selector_escape_yields_selection_invalid(
    project_tree: tuple[Path, Path], tmp_path: Path, shape: str
) -> None:
    project, _ = project_tree
    outside = tmp_path / "outside" / "review"
    outside.mkdir(parents=True)
    if shape == "first-link":
        (project / "skills").symlink_to(tmp_path / "outside", target_is_directory=True)
        directory = "skills/review"
    elif shape == "mid-link":
        (project / "skills").mkdir(exist_ok=True)
        (project / "skills" / "review").symlink_to(outside, target_is_directory=True)
        directory = "skills/review"
    else:
        (project / "skills" / "nested").mkdir(parents=True)
        (project / "skills" / "nested" / "review").symlink_to(
            outside, target_is_directory=True
        )
        directory = "skills/nested/review"
    record = _freeze(project, tmp_path / "home")
    (tmp_path / "home").mkdir(exist_ok=True)
    _refuses(
        CODE_SELECTION_INVALID,
        lambda: boundaries.check_selected_package(
            record, project, directory, alias="local", policy=_policy()
        ),
    )


@pytest.mark.parametrize("entries", [{}, {"other": ("SKILL.md",)}, {"local": ()}])
def test_root_without_inputs_yields_output_overlap(
    project_tree: tuple[Path, Path], entries: dict[str, tuple[str, ...]]
) -> None:
    project, home = project_tree
    record = _freeze(project, home)
    error = _refuses(
        CODE_OUTPUT_OVERLAP,
        lambda: boundaries.check_selected_package(
            record, project, ".", alias="local", policy=_policy(entries)
        ),
    )
    assert "root_inputs" in error.detail


def test_selected_package_missing_yields_member_missing(
    project_tree: tuple[Path, Path]
) -> None:
    project, home = project_tree
    record = _freeze(project, home)
    _refuses(
        CODE_MEMBER_MISSING,
        lambda: boundaries.check_selected_package(
            record, project, "agents/skills/absent", alias="local", policy=_policy()
        ),
    )


def test_selected_package_file_yields_selection_invalid(
    project_tree: tuple[Path, Path]
) -> None:
    project, home = project_tree
    record = _freeze(project, home)
    _refuses(
        CODE_SELECTION_INVALID,
        lambda: boundaries.check_selected_package(
            record,
            project,
            "agents/skills/review/SKILL.md",
            alias="local",
            policy=_policy(),
        ),
    )


@pytest.mark.parametrize("directory", ["", "..", "/abs", "a\\b", "a/*/b", "has space/../x"])
def test_selected_package_nonportable_yields_selection_invalid(
    project_tree: tuple[Path, Path], directory: str
) -> None:
    project, home = project_tree
    record = _freeze(project, home)
    _refuses(
        CODE_SELECTION_INVALID,
        lambda: boundaries.check_selected_package(
            record, project, directory, alias="local", policy=_policy()
        ),
    )


# Discovery pruning.


def test_discovery_pruning_drops_managed_keeps_authored(
    project_tree: tuple[Path, Path]
) -> None:
    project, home = project_tree
    (project / ".git").mkdir(exist_ok=True)
    (project / ".claude" / "skills").mkdir(parents=True)
    (project / "authored").mkdir(exist_ok=True)
    (project / "link-managed").symlink_to(project / ".agents", target_is_directory=True)
    record = _freeze(project, home)
    kept = boundaries.prune_discovery_candidates(
        record,
        project,
        ("agents", ".agents", ".git", ".claude", "authored", "link-managed"),
    )
    assert kept == ("agents", "authored")


def test_discovery_pruning_never_drops_authored_near_miss(
    project_tree: tuple[Path, Path]
) -> None:
    project, home = project_tree
    for name in ("agents", "git", ".githooks", ".agent", "agents-backup"):
        (project / name).mkdir(exist_ok=True)
    record = _freeze(project, home)
    kept = boundaries.prune_discovery_candidates(
        record,
        project,
        ("agents", "git", ".githooks", ".agent", "agents-backup"),
    )
    assert kept == ("agents", "git", ".githooks", ".agent", "agents-backup")


def test_discovery_pruning_case_alias_dropped_or_bound_declared(
    project_tree: tuple[Path, Path], tmp_path: Path
) -> None:
    project, home = project_tree
    (project / ".AGENTS").mkdir(exist_ok=True)
    record = _freeze(project, home)
    kept = boundaries.prune_discovery_candidates(record, project, (".AGENTS", "agents"))
    if _filesystem_conflates_case(tmp_path):
        assert kept == ("agents",)
    else:
        # Declared bound: distinct authored spelling on case-sensitive hosts.
        assert kept == (".AGENTS", "agents")


def test_discovery_pruning_missing_candidate_kept_for_downstream(
    project_tree: tuple[Path, Path]
) -> None:
    project, home = project_tree
    record = _freeze(project, home)
    assert ("vanished",) == boundaries.prune_discovery_candidates(
        record, project, ("vanished",)
    )


def test_discovery_pruning_is_deterministic(project_tree: tuple[Path, Path]) -> None:
    project, home = project_tree
    (project / ".git").mkdir(exist_ok=True)
    record = _freeze(project, home)
    names = ("agents", ".agents", ".git", "review")
    first = boundaries.prune_discovery_candidates(record, project, names)
    second = boundaries.prune_discovery_candidates(record, project, names)
    assert first == second == ("agents", "review")


def test_discovery_pruning_managed_source_root_refuses(
    project_tree: tuple[Path, Path]
) -> None:
    project, home = project_tree
    nested = project / ".agents" / "skills" / "nested"
    nested.mkdir(parents=True)
    record = _freeze(nested, home)
    assert record.source_inside_managed is not None
    _refuses(
        CODE_OUTPUT_OVERLAP,
        lambda: boundaries.prune_discovery_candidates(record, nested, ("review",)),
    )


# Prune-intersection class: selected package, declared runtime input,
# declared build input, through both public entry points.


@pytest.mark.parametrize(
    "subject",
    [
        ".agents/skills/review",
        ".agents",
        ".git/objects",
        ".claude/skills/review",
        ".cursor/rules/review",
    ],
)
def test_prune_intersection_refuses_selected_package(
    project_tree: tuple[Path, Path], subject: str
) -> None:
    project, home = project_tree
    (project / subject).mkdir(parents=True, exist_ok=True)
    record = _freeze(project, home)
    error = _refuses(
        CODE_OUTPUT_OVERLAP,
        lambda: boundaries.check_selected_package(
            record, project, subject, alias="local", policy=_policy()
        ),
    )
    assert subject.split("/")[0] in error.detail


@pytest.mark.parametrize("label", ["runtime", "build"])
@pytest.mark.parametrize(
    "subject",
    [
        ".agents/skills/review",
        ".agents",
        ".git/objects",
        ".codex/skills/review",
        ".gemini/skills/review",
    ],
)
def test_prune_intersection_refuses_declared_input(
    project_tree: tuple[Path, Path], label: str, subject: str
) -> None:
    project, home = project_tree
    (project / subject).mkdir(parents=True, exist_ok=True)
    record = _freeze(project, home)
    error = _refuses(
        CODE_OUTPUT_OVERLAP,
        lambda: boundaries.check_declared_inputs(
            record, project, (subject,), label=label
        ),
    )
    assert subject in error.detail
    assert subject.split("/")[0] in error.detail


@pytest.mark.parametrize("label", ["runtime", "build"])
def test_prune_intersection_refuses_linked_declared_input(
    project_tree: tuple[Path, Path], label: str
) -> None:
    project, home = project_tree
    (project / "declared").symlink_to(project / ".agents" / "skills", target_is_directory=True)
    record = _freeze(project, home)
    _refuses(
        CODE_OUTPUT_OVERLAP,
        lambda: boundaries.check_declared_inputs(
            record, project, ("declared/review",), label=label
        ),
    )


def test_prune_intersection_refuses_through_check_selected_package_link(
    project_tree: tuple[Path, Path]
) -> None:
    project, home = project_tree
    (project / "declared").symlink_to(project / ".git", target_is_directory=True)
    (project / ".git").mkdir(exist_ok=True)
    record = _freeze(project, home)
    _refuses(
        CODE_OUTPUT_OVERLAP,
        lambda: boundaries.check_selected_package(
            record, project, "declared", alias="local", policy=_policy()
        ),
    )


@pytest.mark.parametrize("label", ["runtime", "build"])
def test_declared_input_containing_managed_subtree_still_allowed(
    project_tree: tuple[Path, Path], label: str
) -> None:
    """Only inputs WITHIN the prune set refuse; containers prune normally."""

    project, home = project_tree
    (project / "docs" / ".git").mkdir(parents=True)
    record = _freeze(project, home)
    boundaries.check_declared_inputs(record, project, ("docs",), label=label)


@pytest.mark.parametrize("label", ["runtime", "build"])
def test_declared_input_missing_yields_member_missing(
    project_tree: tuple[Path, Path], label: str
) -> None:
    project, home = project_tree
    record = _freeze(project, home)
    _refuses(
        CODE_MEMBER_MISSING,
        lambda: boundaries.check_declared_inputs(
            record, project, ("absent/root",), label=label
        ),
    )


@pytest.mark.parametrize("label", ["runtime", "build"])
def test_declared_input_escaping_yields_selection_invalid(
    project_tree: tuple[Path, Path], tmp_path: Path, label: str
) -> None:
    project, _ = project_tree
    outside = tmp_path / "outside" / "data"
    outside.mkdir(parents=True, exist_ok=True)
    (project / "hop").symlink_to(tmp_path / "outside", target_is_directory=True)
    (tmp_path / "home2").mkdir(exist_ok=True)
    record = _freeze(project, tmp_path / "home2")
    _refuses(
        CODE_SELECTION_INVALID,
        lambda: boundaries.check_declared_inputs(
            record, project, ("hop/data",), label=label
        ),
    )


# root_inputs admission matrix: one row per rule.


def _root_package(project: Path) -> None:
    (project / "SKILL.md").write_text("# root\n", encoding="utf-8")
    (project / "agents" / "runtime.json").write_text("{}", encoding="utf-8")


def test_root_inputs_unknown_alias(project_tree: tuple[Path, Path]) -> None:
    project, home = project_tree
    _root_package(project)
    record = _freeze(project, home)
    _refuses(
        CODE_ALIAS_UNKNOWN,
        lambda: boundaries.validate_root_inputs(
            record, project, "local", _policy({"other": ("SKILL.md",)})
        ),
    )


@pytest.mark.parametrize(
    "entry", ["/abs", "../escape", "a/../b", "a\\b", "", "trailing.", "nul\0byte"]
)
def test_root_inputs_entry_must_be_portable(
    project_tree: tuple[Path, Path], entry: str
) -> None:
    project, home = project_tree
    _root_package(project)
    record = _freeze(project, home)
    _refuses(
        CODE_SELECTION_INVALID,
        lambda: boundaries.validate_root_inputs(
            record, project, "local", _policy({"local": (entry,)})
        ),
    )


def test_root_inputs_duplicate_entry_refused(project_tree: tuple[Path, Path]) -> None:
    project, home = project_tree
    _root_package(project)
    record = _freeze(project, home)
    _refuses(
        CODE_SELECTION_INVALID,
        lambda: boundaries.validate_root_inputs(
            record, project, "local", _policy({"local": ("SKILL.md", "SKILL.md")})
        ),
    )


@pytest.mark.parametrize(
    "entries",
    [
        ("SKILL.md", "SKILL.md/nested"),
        ("docs", "docs"),
        ("docs", "docs/sub"),
        ("docs/sub", "docs"),
    ],
)
def test_root_inputs_lexical_overlap_refused(
    project_tree: tuple[Path, Path], entries: tuple[str, str]
) -> None:
    project, home = project_tree
    _root_package(project)
    record = _freeze(project, home)
    _refuses(
        CODE_SELECTION_INVALID,
        lambda: boundaries.validate_root_inputs(
            record, project, "local", _policy({"local": entries})
        ),
    )


@pytest.mark.parametrize("shape", ["entry-link", "mid-link", "member-link"])
def test_root_inputs_link_free(project_tree: tuple[Path, Path], shape: str) -> None:
    project, home = project_tree
    _root_package(project)
    (project / "docs").mkdir(exist_ok=True)
    (project / "docs" / "notes.md").write_text("n\n", encoding="utf-8")
    if shape == "entry-link":
        (project / "docs-link").symlink_to(project / "docs", target_is_directory=True)
        entries: tuple[str, ...] = ("SKILL.md", "docs-link")
    elif shape == "mid-link":
        (project / "hop").symlink_to(project / "docs", target_is_directory=True)
        entries = ("SKILL.md", "hop/notes.md")
    else:
        (project / "docs" / "alias.md").symlink_to(project / "docs" / "notes.md")
        entries = ("SKILL.md", "docs")
    record = _freeze(project, home)
    error = _refuses(
        CODE_MEMBER_INVALID,
        lambda: boundaries.validate_root_inputs(
            record, project, "local", _policy({"local": entries})
        ),
    )
    assert "link" in error.detail


@pytest.mark.parametrize(
    "entries",
    [
        ("SKILL.md", ".agents"),
        ("SKILL.md", ".agents/skills/review"),
        ("SKILL.md", ".git"),
        ("SKILL.md", ".claude/skills"),
    ],
)
def test_root_inputs_disjoint_from_outputs(
    project_tree: tuple[Path, Path], entries: tuple[str, ...]
) -> None:
    project, home = project_tree
    _root_package(project)
    for entry in entries:
        if entry != "SKILL.md":
            (project / entry).mkdir(parents=True, exist_ok=True)
    record = _freeze(project, home)
    _refuses(
        CODE_OUTPUT_OVERLAP,
        lambda: boundaries.validate_root_inputs(
            record, project, "local", _policy({"local": entries})
        ),
    )


def test_root_inputs_nested_managed_member_refused(
    project_tree: tuple[Path, Path]
) -> None:
    project, home = project_tree
    _root_package(project)
    (project / "docs" / ".git").mkdir(parents=True)
    (project / "docs" / "notes.md").write_text("n\n", encoding="utf-8")
    record = _freeze(project, home)
    _refuses(
        CODE_OUTPUT_OVERLAP,
        lambda: boundaries.validate_root_inputs(
            record, project, "local", _policy({"local": ("SKILL.md", "docs")})
        ),
    )


def test_root_inputs_missing_entry_yields_member_missing(
    project_tree: tuple[Path, Path]
) -> None:
    project, home = project_tree
    _root_package(project)
    record = _freeze(project, home)
    _refuses(
        CODE_MEMBER_MISSING,
        lambda: boundaries.validate_root_inputs(
            record, project, "local", _policy({"local": ("SKILL.md", "absent")})
        ),
    )


@pytest.mark.skipif(os.name == "nt", reason="POSIX permission bits required")
@pytest.mark.skipif(
    hasattr(os, "geteuid") and os.geteuid() == 0, reason="root bypasses permission bits"
)
def test_root_inputs_unreadable_file_refused(project_tree: tuple[Path, Path]) -> None:
    project, home = project_tree
    _root_package(project)
    secret = project / "secret.md"
    secret.write_text("s\n", encoding="utf-8")
    secret.chmod(0o000)
    try:
        record = _freeze(project, home)
        _refuses(
            CODE_MEMBER_INVALID,
            lambda: boundaries.validate_root_inputs(
                record, project, "local", _policy({"local": ("SKILL.md", "secret.md")})
            ),
        )
    finally:
        secret.chmod(0o644)


@pytest.mark.parametrize(
    "required",
    [
        ("agents/runtime.json",),
        ("agents/runtime.json", "scripts"),
        ("csk-out.yaml",),
    ],
)
def test_root_inputs_required_members_present(
    project_tree: tuple[Path, Path], required: tuple[str, ...]
) -> None:
    project, home = project_tree
    _root_package(project)
    (project / "scripts").mkdir(exist_ok=True)
    (project / "scripts" / "run.sh").write_text("x\n", encoding="utf-8")
    (project / "csk-out.yaml").write_text("x\n", encoding="utf-8")
    record = _freeze(project, home)
    admitted = boundaries.validate_root_inputs(
        record,
        project,
        "local",
        _policy({"local": ("SKILL.md", "agents", "scripts", "csk-out.yaml")}),
        required,
    )
    assert [item.path for item in admitted] == ["SKILL.md", "agents", "scripts", "csk-out.yaml"]


@pytest.mark.parametrize(
    ("entries", "required", "missing"),
    [
        (("agents",), (), "SKILL.md"),
        (("SKILL.md",), ("agents/runtime.json",), "agents/runtime.json"),
        (("SKILL.md", "agents"), ("scripts/run.sh",), "scripts/run.sh"),
        (("SKILL.md", "scripts/run.sh"), ("scripts",), "scripts"),
    ],
)
def test_root_inputs_required_member_absent_yields_member_missing(
    project_tree: tuple[Path, Path],
    entries: tuple[str, ...],
    required: tuple[str, ...],
    missing: str,
) -> None:
    project, home = project_tree
    _root_package(project)
    (project / "scripts").mkdir(exist_ok=True)
    (project / "scripts" / "run.sh").write_text("x\n", encoding="utf-8")
    record = _freeze(project, home)
    error = _refuses(
        CODE_MEMBER_MISSING,
        lambda: boundaries.validate_root_inputs(
            record, project, "local", _policy({"local": entries}), required
        ),
    )
    assert missing in error.detail


def test_root_inputs_missing_required_file_yields_member_missing(
    project_tree: tuple[Path, Path]
) -> None:
    project, home = project_tree
    _root_package(project)
    record = _freeze(project, home)
    error = _refuses(
        CODE_MEMBER_MISSING,
        lambda: boundaries.validate_root_inputs(
            record, project, "local", _policy({"local": ("SKILL.md",)}), ("ghost.md",)
        ),
    )
    assert "ghost.md" in error.detail


def test_root_inputs_physical_overlap_via_hardlink_refused(
    project_tree: tuple[Path, Path]
) -> None:
    project, home = project_tree
    _root_package(project)
    os.link(project / "SKILL.md", project / "twin.md")
    record = _freeze(project, home)
    with pytest.raises(SourceError) as excinfo:
        boundaries.validate_root_inputs(
            record, project, "local", _policy({"local": ("SKILL.md", "twin.md")})
        )
    assert excinfo.value.code == CODE_PATH_CONFLICT


def test_root_inputs_physical_overlap_via_case_alias_refused_or_bound(
    project_tree: tuple[Path, Path], tmp_path: Path
) -> None:
    project, home = project_tree
    _root_package(project)
    (project / "Docs").mkdir(exist_ok=True)
    record = _freeze(project, home)
    if not _filesystem_conflates_case(tmp_path):
        pytest.skip("physical case-alias overlap needs a case-insensitive filesystem")
    with pytest.raises(SourceError) as excinfo:
        boundaries.validate_root_inputs(
            record, project, "local", _policy({"local": ("SKILL.md", "Docs", "docs")})
        )
    assert excinfo.value.code == CODE_PATH_CONFLICT


def test_root_inputs_physical_nesting_order_reversed_refused(
    project_tree: tuple[Path, Path], tmp_path: Path
) -> None:
    project, home = project_tree
    _root_package(project)
    (project / "Docs" / "sub").mkdir(parents=True)
    record = _freeze(project, home)
    if not _filesystem_conflates_case(tmp_path):
        pytest.skip("physical case-alias nesting needs a case-insensitive filesystem")
    with pytest.raises(SourceError) as excinfo:
        boundaries.validate_root_inputs(
            record, project, "local", _policy({"local": ("SKILL.md", "docs/sub", "Docs")})
        )
    assert excinfo.value.code == CODE_PATH_CONFLICT


def test_selected_package_through_linked_managed_root_refused(
    project_tree: tuple[Path, Path], tmp_path: Path
) -> None:
    project, home = project_tree
    real = project / "real-agents"
    (real / "skills" / "review").mkdir(parents=True)
    (project / ".agents").rename(tmp_path / "agents-saved")
    (project / ".agents").symlink_to(real, target_is_directory=True)
    record = _freeze(project, home)
    assert next(root for root in record.managed_roots if root.name == ".agents").identity is not None
    _refuses(
        CODE_OUTPUT_OVERLAP,
        lambda: boundaries.check_selected_package(
            record, project, ".agents/skills/review", alias="local", policy=_policy()
        ),
    )


def test_root_inputs_valid_admits_recursive_members(project_tree: tuple[Path, Path]) -> None:
    project, home = project_tree
    _root_package(project)
    (project / "docs" / "sub").mkdir(parents=True)
    (project / "docs" / "b.md").write_text("b\n", encoding="utf-8")
    (project / "docs" / "sub" / "a.md").write_text("a\n", encoding="utf-8")
    record = _freeze(project, home)
    (admitted,) = [
        item
        for item in boundaries.validate_root_inputs(
            record, project, "local", _policy({"local": ("SKILL.md", "docs")})
        )
        if item.path == "docs"
    ]
    assert admitted.is_dir
    assert [member.path for member in admitted.members] == ["b.md", "sub", "sub/a.md"]
    assert [member.is_dir for member in admitted.members] == [False, True, False]
    assert admitted.identity == (
        os.stat(project / "docs").st_dev,
        os.stat(project / "docs").st_ino,
    )


def test_root_selection_with_allowlist_validates_entries(
    project_tree: tuple[Path, Path]
) -> None:
    project, home = project_tree
    _root_package(project)
    record = _freeze(project, home)
    resolved = boundaries.check_selected_package(
        record,
        project,
        ".",
        alias="local",
        policy=_policy({"local": ("SKILL.md", "agents")}),
    )
    assert resolved == Path(record.source_root)
    _refuses(
        CODE_MEMBER_MISSING,
        lambda: boundaries.check_selected_package(
            record,
            project,
            ".",
            alias="local",
            policy=_policy({"local": ("agents",)}),
        ),
    )


# Publication-time recheck.


def test_recheck_allows_legitimate_write(project_tree: tuple[Path, Path]) -> None:
    project, home = project_tree
    (project / ".agents" / "out").mkdir(parents=True)
    record = _freeze(project, home)
    boundaries.recheck_publication_destination(record, project, ".agents", ".agents/out/new.md")
    (project / ".agents" / "out" / "kept.md").write_text("k\n", encoding="utf-8")
    boundaries.recheck_publication_destination(
        record, project, ".agents", ".agents/out/kept.md"
    )
    boundaries.recheck_publication_destination(
        record, project, ".agents/skills", ".agents/skills/review/next.md"
    )


@pytest.mark.parametrize(
    ("planned", "destination"),
    [
        (".agents", "agents/skills/review/SKILL.md"),
        (".agents", "agents/skills/review"),
        (".agents", "SKILL.md"),
        (".agents", ".claude/skills/review/next.md"),
        (".agents/skills", ".agents/out/next.md"),
    ],
)
def test_recheck_write_boundary_retarget_refused(
    project_tree: tuple[Path, Path], planned: str, destination: str
) -> None:
    project, home = project_tree
    _root_package(project)
    (project / ".agents" / "out").mkdir(parents=True)
    (project / ".claude" / "skills" / "review").mkdir(parents=True)
    record = _freeze(project, home)
    error = _refuses(
        CODE_OUTPUT_OVERLAP,
        lambda: boundaries.recheck_publication_destination(
            record, project, planned, destination
        ),
    )
    assert "outside planned output" in error.detail


@pytest.mark.parametrize("shape", ["replaced-by-link", "retargeted-link", "deleted-root"])
def test_recheck_changed_boundary_refused(
    project_tree: tuple[Path, Path], tmp_path: Path, shape: str
) -> None:
    project, home = project_tree
    (project / ".agents" / "out").mkdir(parents=True)
    if shape == "retargeted-link":
        real = tmp_path / "real-agents"
        real.mkdir()
        (project / ".agents").rename(tmp_path / "agents-saved")
        (project / ".agents").symlink_to(real, target_is_directory=True)
    record = _freeze(project, home)
    if shape == "replaced-by-link":
        (project / ".agents").rename(tmp_path / "agents-saved")
        (project / ".agents").symlink_to(tmp_path, target_is_directory=True)
    elif shape == "retargeted-link":
        (project / ".agents").unlink()
        (project / ".agents").symlink_to(tmp_path, target_is_directory=True)
    else:
        (project / ".agents").rename(tmp_path / "agents-saved")
    _refuses(
        CODE_OUTPUT_OVERLAP,
        lambda: boundaries.recheck_publication_destination(
            record, project, ".agents", ".agents/out/next.md"
        ),
    )


def test_recheck_new_link_in_chain_refused_without_following(
    project_tree: tuple[Path, Path]
) -> None:
    project, home = project_tree
    out = project / ".agents" / "out"
    out.mkdir(parents=True)
    record = _freeze(project, home)
    # Even a link pointing back inside the planned output refuses: the
    # recheck cannot prove it was not newly introduced.
    (out / "hop").symlink_to(out, target_is_directory=True)
    error = _refuses(
        CODE_OUTPUT_OVERLAP,
        lambda: boundaries.recheck_publication_destination(
            record, project, ".agents", ".agents/out/hop/next.md"
        ),
    )
    assert "link" in error.detail


def test_recheck_frozen_link_root_allowed(project_tree: tuple[Path, Path], tmp_path: Path) -> None:
    project, home = project_tree
    real = tmp_path / "real-agents"
    (real / "out").mkdir(parents=True)
    (project / ".agents").rename(tmp_path / "agents-saved")
    (project / ".agents").symlink_to(real, target_is_directory=True)
    record = _freeze(project, home)
    assert next(root for root in record.managed_roots if root.name == ".agents").target is not None
    boundaries.recheck_publication_destination(record, project, ".agents", ".agents/out/next.md")


def test_recheck_destination_link_refused(project_tree: tuple[Path, Path]) -> None:
    project, home = project_tree
    out = project / ".agents" / "out"
    out.mkdir(parents=True)
    (out / "real.md").write_text("r\n", encoding="utf-8")
    (out / "alias.md").symlink_to(out / "real.md")
    record = _freeze(project, home)
    _refuses(
        CODE_OUTPUT_OVERLAP,
        lambda: boundaries.recheck_publication_destination(
            record, project, ".agents", ".agents/out/alias.md"
        ),
    )


def test_recheck_never_overwrites_admitted_input(
    project_tree: tuple[Path, Path], tmp_path: Path
) -> None:
    project, home = project_tree
    _root_package(project)
    out = project / ".agents" / "out"
    out.mkdir(parents=True)
    record = _freeze(project, home)
    admitted = boundaries.validate_root_inputs(
        record, project, "local", _policy({"local": ("SKILL.md", "agents")})
    )
    identities = frozenset(item.identity for item in admitted)
    assert identities
    # A hard link plants an admitted identity at a managed destination.
    os.link(project / "SKILL.md", out / "planted.md")
    try:
        _refuses(
            CODE_OUTPUT_OVERLAP,
            lambda: boundaries.recheck_publication_destination(
                record, project, ".agents", ".agents/out/planted.md", admitted=identities
            ),
        )
    finally:
        (out / "planted.md").unlink()
    # Without the admitted set the same destination is an ordinary write.
    boundaries.recheck_publication_destination(
        record, project, ".agents", ".agents/out/planted.md"
    )


def test_recheck_never_overwrites_admitted_case_alias(
    project_tree: tuple[Path, Path], tmp_path: Path
) -> None:
    project, home = project_tree
    _root_package(project)
    out = project / ".agents" / "out"
    out.mkdir(parents=True)
    (out / "SKILL.md").write_text("managed copy\n", encoding="utf-8")
    record = _freeze(project, home)
    admitted = boundaries.validate_root_inputs(
        record, project, "local", _policy({"local": ("SKILL.md", "agents")})
    )
    if not _filesystem_conflates_case(tmp_path):
        pytest.skip("admitted case-alias overwrite needs a case-insensitive filesystem")
    os.link(project / "SKILL.md", out / "planted.md")
    try:
        _refuses(
            CODE_OUTPUT_OVERLAP,
            lambda: boundaries.recheck_publication_destination(
                record,
                project,
                ".agents",
                ".agents/out/PLANTED.md",
                admitted=frozenset(item.identity for item in admitted),
            ),
        )
    finally:
        (out / "planted.md").unlink()


def test_recheck_missing_parent_refused(project_tree: tuple[Path, Path]) -> None:
    project, home = project_tree
    record = _freeze(project, home)
    _refuses(
        CODE_OUTPUT_OVERLAP,
        lambda: boundaries.recheck_publication_destination(
            record, project, ".agents", ".agents/absent-dir/next.md"
        ),
    )


@pytest.mark.parametrize(
    ("planned", "destination"),
    [("agents", ".agents/out/x.md"), (".agents", "../escape.md"), (".agents", "/abs.md")],
)
def test_recheck_nonoutput_or_nonportable_refused(
    project_tree: tuple[Path, Path], planned: str, destination: str
) -> None:
    project, home = project_tree
    (project / ".agents" / "out").mkdir(parents=True)
    record = _freeze(project, home)
    _refuses(
        CODE_OUTPUT_OVERLAP,
        lambda: boundaries.recheck_publication_destination(
            record, project, planned, destination
        ),
    )


# Unmanaged-path protection composition.


def test_allowlisted_input_destination_colliding_with_unmanaged_adapter_file_refuses(
    project_tree: tuple[Path, Path],
) -> None:
    project, home = project_tree
    _root_package(project)
    record = _freeze(project, home)
    admitted = boundaries.validate_root_inputs(
        record, project, "local", _policy({"local": ("SKILL.md", "agents")})
    )
    assert admitted
    unmanaged = project / ".claude" / "skills" / "review"
    unmanaged.mkdir(parents=True)
    (unmanaged / "SKILL.md").write_text("# foreign\n", encoding="utf-8")
    canonical = project / "agents" / "skills"
    with pytest.raises(adapters.AdapterError, match="not managed by csk"):
        adapters.plan_project_adapter_targets(
            project,
            ["claude_code"],
            [adapters.AdapterGroup(canonical_root=canonical, skill_names=("review",))],
        )


def test_adapter_ledger_refusal_names_path_and_shape(
    project_tree: tuple[Path, Path],
) -> None:
    """The ledger refusal names the absolute path and the shape (BUG-260918-wvfoqa).

    Pinned at the planner seam shared by both lanes: the schema-2
    boundary redacts absolute paths to ``<path>``, so the verbatim
    path is asserted here rather than end to end.
    """

    project, home = project_tree
    _root_package(project)
    ledger = project / ".claude" / "skills" / ".csk-managed.json"
    ledger.parent.mkdir(parents=True)
    ledger.write_text("MY PRECIOUS USER JSON\n", encoding="utf-8")
    canonical = project / "agents" / "skills"
    with pytest.raises(adapters.AdapterError) as caught:
        adapters.plan_project_adapter_targets(
            project,
            ["claude_code"],
            [adapters.AdapterGroup(canonical_root=canonical, skill_names=("review",))],
        )
    assert str(ledger) in str(caught.value)
    assert "(not JSON)" in str(caught.value)
    assert ledger.read_text(encoding="utf-8") == "MY PRECIOUS USER JSON\n"


@pytest.mark.parametrize("platform_name", [None, "windows"], ids=["ambient", "windows"])
def test_allowlisted_input_destination_colliding_with_unmanaged_bin_refuses(
    project_tree: tuple[Path, Path], tmp_path: Path, platform_name: str | None
) -> None:
    project, home = project_tree
    _root_package(project)
    record = _freeze(project, home)
    admitted = boundaries.validate_root_inputs(
        record, project, "local", _policy({"local": ("SKILL.md", "agents")})
    )
    assert admitted
    user_bin = tmp_path / "user-bin"
    user_bin.mkdir()
    # The collision is planted at the platform's real published path: on
    # Windows csk publishes ``review.cmd``, so an extensionless ``review``
    # file is a different path and correctly does not collide.
    shims.shim_path(user_bin, "review", platform_name=platform_name).write_text(
        "#!/bin/sh\n", encoding="utf-8"
    )
    targets, messages = global_bins.plan_user_bin_targets(
        home,
        {"review"},
        platform_name=platform_name,
        env={"PATH": os.pathsep.join([str(user_bin,)]),
             global_bins.USER_BIN_ENV: str(user_bin)},
        home=tmp_path,
    )
    assert not [target for target in targets if target.desired_kind == "forwarder"]
    assert any("not managed by csk" in message for message in messages)


# Closed managed-output table and freeze edges.


def test_managed_output_table_is_closed(project_tree: tuple[Path, Path]) -> None:
    project, home = project_tree
    table = boundaries.managed_output_table(project, home)
    keys = [member.key for member in table]
    assert keys == [".agents", ".claude", ".codex", ".cursor", ".gemini", ".git", "<csk-home>"]
    by_key = {member.key: member for member in table}
    assert "NATIVE_DISCOVERY_HOME_PATH" in by_key[".agents"].origin
    for agent, relative in adapters.AGENT_PATHS.items():
        first = relative.split("/", 1)[0]
        assert agent in by_key[first].origin
        assert relative in by_key[first].origin
    assert "skillfile-sources section 2" in by_key[".git"].origin
    assert "DEFAULT_CONFIG_PATH" in by_key["<csk-home>"].origin
    assert all(member.kind for member in table)
    assert all(member.display for member in table)


def test_agents_skills_is_authored_input(project_tree: tuple[Path, Path]) -> None:
    project, home = project_tree
    assert "agents" not in boundaries.managed_output_first_components()
    record = _freeze(project, home)
    resolved = boundaries.check_selected_package(
        record, project, "agents/skills/review", alias="local", policy=_policy()
    )
    assert resolved.is_dir()


def test_freeze_missing_source_refuses(project_tree: tuple[Path, Path]) -> None:
    _, home = project_tree
    _refuses(
        CODE_SELECTION_INVALID,
        lambda: boundaries.freeze_boundaries(Path("/nonexistent-csk-source-root"), home),
    )


def test_freeze_file_source_refuses(project_tree: tuple[Path, Path]) -> None:
    project, home = project_tree
    _refuses(
        CODE_SELECTION_INVALID,
        lambda: boundaries.freeze_boundaries(
            project / "agents" / "skills" / "review" / "SKILL.md", home
        ),
    )


def test_freeze_fresh_home_allowed(project_tree: tuple[Path, Path], tmp_path: Path) -> None:
    project, _ = project_tree
    absent_home = tmp_path / "fresh-home"
    record = boundaries.freeze_boundaries(project, absent_home)
    assert record.home_identity is None
    assert record.home_root is None
    assert record.home_contains_source is False


def test_freeze_file_home_refuses(project_tree: tuple[Path, Path]) -> None:
    project, home = project_tree
    blocker = home / "blocker"
    blocker.write_text("x", encoding="utf-8")
    _refuses(CODE_OUTPUT_OVERLAP, lambda: boundaries.freeze_boundaries(project, blocker))


def test_freeze_source_inside_managed_marks_record(
    project_tree: tuple[Path, Path]
) -> None:
    project, home = project_tree
    nested = project / ".agents" / "skills" / "nested"
    nested.mkdir(parents=True)
    record = _freeze(nested, home)
    assert record.source_inside_managed == ".agents"
    _refuses(
        CODE_OUTPUT_OVERLAP,
        lambda: boundaries.check_selected_package(
            record, nested, "review", alias="local", policy=_policy()
        ),
    )


def test_freeze_source_inside_home_marks_record(
    project_tree: tuple[Path, Path], tmp_path: Path
) -> None:
    nested = tmp_path / "home" / "sources" / "proj"
    nested.mkdir(parents=True)
    (nested / "agents" / "skills" / "review").mkdir(parents=True)
    record = boundaries.freeze_boundaries(nested, tmp_path / "home")
    assert record.home_contains_source is True
    _refuses(
        CODE_OUTPUT_OVERLAP,
        lambda: boundaries.check_selected_package(
            record, nested, "agents/skills/review", alias="local", policy=_policy()
        ),
    )


def test_record_root_mismatch_refuses(project_tree: tuple[Path, Path], tmp_path: Path) -> None:
    project, home = project_tree
    other = tmp_path / "other"
    (other / "agents").mkdir(parents=True)
    record = _freeze(project, home)
    _refuses(
        CODE_SELECTION_INVALID,
        lambda: boundaries.check_selected_package(
            record, other, "agents", alias="local", policy=_policy()
        ),
    )


def test_freeze_is_deterministic(project_tree: tuple[Path, Path]) -> None:
    project, home = project_tree
    assert _freeze(project, home) == _freeze(project, home)


# S-FS instrument: every filesystem call names a path inside the declared roots.


@contextmanager
def _recorded_filesystem() -> Iterator[list[tuple[str, str]]]:
    """Record (function, path) for every os call the boundary layer makes."""

    calls: list[tuple[str, str]] = []
    real_lstat = os.lstat
    real_stat = os.stat
    real_readlink = os.readlink
    real_open = os.open
    real_scandir = os.scandir

    def _text(value: Any) -> str:
        return value if isinstance(value, str) else os.fspath(value)

    def lstat(path: Any, *args: Any, **kwargs: Any) -> Any:
        calls.append(("lstat", _text(path)))
        return real_lstat(path, *args, **kwargs)

    def stat(path: Any, *args: Any, **kwargs: Any) -> Any:
        calls.append(("stat", _text(path)))
        return real_stat(path, *args, **kwargs)

    def readlink(path: Any, *args: Any, **kwargs: Any) -> Any:
        calls.append(("readlink", _text(path)))
        return real_readlink(path, *args, **kwargs)

    def checked_open(path: Any, *args: Any, **kwargs: Any) -> Any:
        calls.append(("open", _text(path)))
        return real_open(path, *args, **kwargs)

    def scandir(path: Any, *args: Any, **kwargs: Any) -> Any:
        calls.append(("scandir", _text(path)))
        return real_scandir(path, *args, **kwargs)

    original = (os.lstat, os.stat, os.readlink, os.open, os.scandir)
    os.lstat = lstat  # type: ignore[assignment]
    os.stat = stat  # type: ignore[assignment]
    os.readlink = readlink  # type: ignore[assignment]
    os.open = checked_open  # type: ignore[assignment]
    os.scandir = scandir  # type: ignore[assignment]
    try:
        yield calls
    finally:
        os.lstat, os.stat, os.readlink, os.open, os.scandir = original


def _is_filesystem_root(
    resolved: str, *, dirname: Callable[[str], str] | None = None
) -> bool:
    """Return whether one absolute path is its filesystem's root.

    The root is ``dirname``'s fixed point on every platform: ``/`` on
    POSIX, a drive root (``D:\\``) or UNC share root on Windows. The
    ``dirname`` hook exists so the Windows spelling is pinned on any host
    via ``ntpath``.
    """

    return (dirname or os.path.dirname)(resolved) == resolved


def _assert_confined(
    calls: list[tuple[str, str]], project: Path, home: Path, *, allow_home_absent: bool = False
) -> None:
    assert calls, "the confinement oracle observed no filesystem calls"
    project_real = os.path.realpath(project)
    home_real = os.path.realpath(home)
    managed = boundaries.managed_output_first_components()

    def _is_ancestor_or_self(candidate: str, root: str) -> bool:
        return candidate == root or root.startswith(candidate + os.sep)

    for func, path in calls:
        if not os.path.isabs(path):
            continue
        resolved = os.path.realpath(path)
        # Ancestors above the roots are inspected while resolving the
        # lookup path; they are never a read or listing target.
        if func in ("lstat", "stat", "readlink") and (
            _is_filesystem_root(resolved)
            or _is_ancestor_or_self(resolved, project_real)
            or _is_ancestor_or_self(resolved, home_real)
        ):
            continue
        # The above-root managed-ancestry probe stats the canonical
        # managed spelling under each ancestor of the roots.
        if func in ("lstat", "stat"):
            parent = os.path.dirname(resolved)
            if os.path.basename(resolved) in managed and (
                _is_filesystem_root(parent)
                or _is_ancestor_or_self(parent, project_real)
                or _is_ancestor_or_self(parent, home_real)
            ):
                continue
        inside_project = resolved == project_real or resolved.startswith(project_real + os.sep)
        inside_home = resolved == home_real or resolved.startswith(home_real + os.sep)
        if allow_home_absent and resolved == home_real:
            inside_home = True
        assert inside_project or inside_home, f"{func} escaped to {path!r}"


def test_filesystem_root_predicate_recognises_every_platform_root() -> None:
    """The confinement oracle's root test holds for POSIX and Windows spellings.

    The ancestry walk in ``boundaries._ancestry_stats`` stats every
    ancestor up to and including the filesystem root; on Windows that
    root is a drive root (``D:\\``), which the old ``== os.sep`` check
    never matched, so the probe was misreported as an escape.
    """
    assert _is_filesystem_root("/")
    assert not _is_filesystem_root("/a")
    assert not _is_filesystem_root("/a/b")
    assert _is_filesystem_root("D:\\", dirname=ntpath.dirname)
    assert not _is_filesystem_root("D:\\a", dirname=ntpath.dirname)
    assert not _is_filesystem_root("D:\\a\\b", dirname=ntpath.dirname)


def test_filesystem_access_confined_to_declared_roots(
    project_tree: tuple[Path, Path]
) -> None:
    project, home = project_tree
    _root_package(project)
    (project / ".agents" / "out").mkdir(parents=True)
    policy = _policy({"local": ("SKILL.md", "agents")})
    with _recorded_filesystem() as calls:
        record = boundaries.freeze_boundaries(project, home)
        boundaries.check_selected_package(
            record, project, "agents/skills/review", alias="local", policy=policy
        )
        boundaries.prune_discovery_candidates(record, project, ("agents", ".agents"))
        boundaries.check_declared_inputs(record, project, ("agents",), label="runtime")
        boundaries.validate_root_inputs(record, project, "local", policy)
        boundaries.recheck_publication_destination(
            record, project, ".agents", ".agents/out/next.md"
        )
    _assert_confined(calls, project, home)


def test_audit_hook_sees_no_outside_open(project_tree: tuple[Path, Path]) -> None:
    project, home = project_tree
    _root_package(project)
    policy = _policy({"local": ("SKILL.md", "agents")})
    record = boundaries.freeze_boundaries(project, home)
    events: list[tuple[str, tuple[Any, ...]]] = []
    sys.addaudithook(lambda event, args: events.append((event, args)))
    start = len(events)
    boundaries.check_selected_package(
        record, project, "agents/skills/review", alias="local", policy=policy
    )
    boundaries.validate_root_inputs(record, project, "local", policy)
    boundaries.prune_discovery_candidates(record, project, ("agents", ".agents"))
    window = events[start:]
    project_real = os.path.realpath(project)
    home_real = os.path.realpath(home)
    observed = 0
    for event, args in window:
        if event not in ("open", "os.scandir", "os.listdir"):
            continue
        if not args or not isinstance(args[0], str) or not os.path.isabs(args[0]):
            continue
        observed += 1
        resolved = os.path.realpath(args[0])
        inside = (
            resolved == project_real
            or resolved.startswith(project_real + os.sep)
            or resolved == home_real
            or resolved.startswith(home_real + os.sep)
        )
        assert inside, f"audit event {event} escaped to {args[0]!r}"
    assert observed > 0, "the audit oracle observed no open/scandir/listdir events"


# S-ERRORS instrument: fault injection at the real os call site.


@contextmanager
def _faulty_os(
    func_name: str, predicate: Callable[[Any], bool], error: Exception
) -> Iterator[list[str]]:
    """Replace one os function with a failing wrapper inside the window."""

    real = getattr(os, func_name)
    fired: list[str] = []

    def wrapper(first: Any, *args: Any, **kwargs: Any) -> Any:
        if predicate(first):
            fired.append(repr(first))
            raise error
        return real(first, *args, **kwargs)

    setattr(os, func_name, wrapper)
    try:
        yield fired
    finally:
        setattr(os, func_name, real)


def _raises_structured(code: str, func: Callable[[], Any]) -> SourceError:
    with pytest.raises(SourceError) as excinfo:
        func()
    assert excinfo.value.code == code
    assert excinfo.value.__cause__ is not None
    return excinfo.value


def _fault_fixture(tmp_path: Path) -> tuple[Path, Path]:
    project = tmp_path / "project"
    home = tmp_path / "home"
    (project / "agents" / "skills" / "review").mkdir(parents=True)
    (project / "agents" / "skills" / "review" / "SKILL.md").write_text("r\n", encoding="utf-8")
    (project / "SKILL.md").write_text("# root\n", encoding="utf-8")
    (project / ".agents" / "out").mkdir(parents=True)
    home.mkdir(parents=True)
    return (project, home)


def _spellings(path: Path) -> tuple[str, str]:
    """Precompute the resolved and lexical spellings outside the window."""

    return (os.path.realpath(path), os.path.abspath(path))


def _under(root: Path) -> Callable[[Any], bool]:
    root_real, root_lex = _spellings(root)

    def check(value: Any) -> bool:
        # Lexical only: resolving here would re-enter the wrapped os layer.
        text = value if isinstance(value, str) else os.fspath(value)
        normalized = os.path.abspath(text)
        for base in (root_real, root_lex):
            if normalized == base or normalized.startswith(base + os.sep):
                return True
        return False

    return check


def _exact(path: Path) -> Callable[[Any], bool]:
    target_real, target_lex = _spellings(path)

    def check(value: Any) -> bool:
        # Lexical only: resolving here would re-enter the wrapped os layer.
        text = value if isinstance(value, str) else os.fspath(value)
        normalized = os.path.abspath(text)
        return normalized == target_real or normalized == target_lex

    return check


_FAULT_ROWS: tuple[tuple[str, str, str], ...] = (
    ("freeze-managed-lstat", "lstat", CODE_OUTPUT_OVERLAP),
    ("freeze-managed-readlink", "readlink", CODE_OUTPUT_OVERLAP),
    ("freeze-managed-stat", "stat", CODE_OUTPUT_OVERLAP),
    ("selector-lstat", "lstat", CODE_SELECTION_INVALID),
    ("selector-readlink", "readlink", CODE_SELECTION_INVALID),
    ("expand-scandir", "scandir", CODE_MEMBER_INVALID),
    ("readability-open", "open", CODE_MEMBER_INVALID),
    ("readability-read", "read", CODE_MEMBER_INVALID),
    ("recheck-binding", "lstat", CODE_OUTPUT_OVERLAP),
    ("prune-candidate", "lstat", CODE_OUTPUT_OVERLAP),
 )


@pytest.mark.parametrize("row", [item[0] for item in _FAULT_ROWS])
@pytest.mark.parametrize(
    "fault",
    [
        pytest.param(PermissionError("injected"), id="permission"),
        pytest.param(OSError(errno.ELOOP, "injected"), id="eloop"),
        pytest.param(RuntimeError("injected"), id="runtime"),
        pytest.param(ValueError("injected"), id="value"),
        pytest.param(
            UnicodeDecodeError("utf-8", b"\xff", 0, 1, "injected"), id="unicode"
        ),
    ],
)
def test_injected_filesystem_fault_is_structured(
    tmp_path: Path, row: str, fault: Exception
) -> None:
    project, home = _fault_fixture(tmp_path)
    link = project / "sel-link"
    if row in ("selector-lstat", "selector-readlink"):
        link.symlink_to(project / "agents" / "skills", target_is_directory=True)
    if row in ("freeze-managed-readlink", "freeze-managed-stat"):
        (project / ".agents").rename(tmp_path / "agents-saved")
        (project / ".agents").symlink_to(tmp_path / "agents-saved", target_is_directory=True)
    expected = dict((name, code) for name, _, code in _FAULT_ROWS)[row]
    func = dict((name, patched) for name, patched, _ in _FAULT_ROWS)[row]

    def drive_after_freeze(record: boundaries.BoundaryRecord) -> None:
        if row in ("selector-lstat", "selector-readlink"):
            boundaries.check_selected_package(
                record, project, "sel-link/review", alias="local", policy=_policy()
            )
            return
        if row == "expand-scandir":
            boundaries.validate_root_inputs(
                record, project, "local", _policy({"local": ("SKILL.md", "agents")})
            )
            return
        if row in ("readability-open", "readability-read"):
            boundaries.validate_root_inputs(
                record, project, "local", _policy({"local": ("SKILL.md",)})
            )
            return
        if row == "recheck-binding":
            boundaries.recheck_publication_destination(
                record, project, ".agents", ".agents/out/next.md"
            )
            return
        if row == "prune-candidate":
            boundaries.prune_discovery_candidates(record, project, ("agents", ".agents"))
            return
        raise AssertionError(f"unknown fault row {row}")

    if row == "freeze-managed-lstat":
        predicate: Callable[[Any], bool] = _exact(project / ".agents")
    elif row in ("freeze-managed-readlink", "freeze-managed-stat"):
        predicate = _exact(project / ".agents")
    elif row in ("selector-lstat", "selector-readlink"):
        predicate = _exact(link)
    elif row == "expand-scandir":
        predicate = _under(project / "agents")
    elif row == "readability-open":
        predicate = _exact(project / "SKILL.md")
    elif row == "readability-read":
        predicate = lambda value: True  # noqa: E731
    elif row == "recheck-binding":
        predicate = _exact(project / ".agents")
    else:
        predicate = _exact(project / ".agents")
    if row.startswith("freeze-managed"):
        # Positive control: the same fixture freezes unfaulted.
        boundaries.freeze_boundaries(project, home)
        with _faulty_os(func, predicate, fault) as fired:
            _raises_structured(
                expected, lambda: boundaries.freeze_boundaries(project, home)
            )
    else:
        record = boundaries.freeze_boundaries(project, home)
        # Positive control: the same fixture succeeds unfaulted.
        drive_after_freeze(record)
        with _faulty_os(func, predicate, fault) as fired:
            _raises_structured(expected, lambda: drive_after_freeze(record))
    assert fired, f"fault at os.{func} was never reached for row {row}"


# S-POLICY instrument: structural validation runs before any filesystem touch.


def test_structural_validation_has_zero_filesystem_effects(
    project_tree: tuple[Path, Path]
) -> None:
    project, home = project_tree
    _root_package(project)
    record = _freeze(project, home)
    structural: list[tuple[str, ...]] = [
        ("../escape",),
        ("SKILL.md", "SKILL.md"),
        ("docs", "docs/sub"),
    ]
    for entries in structural:
        with _recorded_filesystem() as calls:
            _refuses(
                CODE_SELECTION_INVALID,
                lambda entries=entries: boundaries.validate_root_inputs(
                    record, project, "local", _policy({"local": entries})
                ),
            )
        assert calls == [], f"structural refusal touched the filesystem: {calls}"
    with _recorded_filesystem() as positive:
        boundaries.validate_root_inputs(
            record, project, "local", _policy({"local": ("SKILL.md", "agents")})
        )
    assert positive, "positive control observed no filesystem calls"
