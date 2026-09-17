"""Deterministic skill-collection expansion (draft skillfile-sources-v1, opt-in).

Every test drives the production entry points in
:csk.sources.selection: (``resolve_selector_directory``,
``expand_collection``, ``resolve_individual``, ``expand_selectors``,
``validate_member_package`` / ``read_skill_md_name``) on filesystem fixtures.
"""

from __future__ import annotations

import errno
import json
import os
import unicodedata
from pathlib import Path

import pytest
import yaml

from csk.sources import _selection_fs
from csk.sources import errors as source_errors
from csk.sources import selection
from csk.sources.selection import (
    SelectedSkill,
    expand_collection,
    expand_selectors,
    resolve_individual,
    resolve_selector_directory,
)
from csk.sources.skillfile_v2 import CollectionSelector, IndividualSelector


def _write_skill(
    directory: Path,
    name: str,
    description: str = "A test skill",
    *,
    triggers: str | None = None,
    manifest: str | None = None,
    raw_frontmatter: str | None = None,
) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    if raw_frontmatter is not None:
        text = raw_frontmatter
    else:
        lines = ["---", f"name: {name}", f"description: {description}"]
        if triggers is not None:
            lines.append(triggers)
        lines.append("---")
        lines.append("")
        lines.append(f"# {name}")
        lines.append("")
        text = "\n".join(lines)
    (directory / "SKILL.md").write_text(text, encoding="utf-8")
    if manifest is not None:
        (directory / "agent-skill.json").write_text(manifest, encoding="utf-8")
    return directory


def _collection(
    directory: str = ".",
    include: tuple[str, ...] = ("*",),
    exclude: tuple[str, ...] = (),
) -> CollectionSelector:
    return CollectionSelector(
        from_alias="local", directory=directory, include=include, exclude=exclude
    )


# ---------------------------------------------------------------------------
# AC (a): selector directories, symlinks before containment, escape refusal
# ---------------------------------------------------------------------------


def test_selector_dot_selects_source_root(tmp_path: Path) -> None:
    _write_skill(tmp_path / "src", "review")
    resolved = resolve_selector_directory(tmp_path / "src", ".")
    assert resolved == (tmp_path / "src").resolve()
    individual = resolve_individual(
        tmp_path / "src",
        IndividualSelector(name="review", from_alias="local", directory="."),
    )
    assert isinstance(individual, SelectedSkill)
    assert (individual.name, individual.directory) == ("review", ".")


def test_selector_inside_symlink_resolves(tmp_path: Path) -> None:
    root = tmp_path / "src"
    _write_skill(root / "real" / "review", "review")
    (root / "link").symlink_to(root / "real", target_is_directory=True)
    resolved = resolve_selector_directory(root, "link/review")
    assert resolved == (root / "real" / "review").resolve()
    individual = resolve_individual(
        root, IndividualSelector(name="review", from_alias="local", directory="link/review")
    )
    assert individual.name == "review"


@pytest.mark.parametrize("entry", ["individual", "collection"])
@pytest.mark.parametrize("spelling", ["root-alias", "case-equivalent"])
def test_absolute_contained_link_resolves(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    entry: str,
    spelling: str,
) -> None:
    """Phase-A identity containment admits contained absolute link targets.

    The root may itself be supplied through a symlink, and on a
    case-insensitive filesystem the link target may use a different spelling
    of the root component. The selected package is the real ``review``
    directory below the intermediate link, not the link itself.
    """

    real_root = tmp_path / "source"
    target = real_root / "target"
    _write_skill(target / "review", "review")
    if spelling == "root-alias":
        root = tmp_path / "source-alias"
        root.symlink_to(real_root, target_is_directory=True)
        absolute_target = target
    else:
        root = real_root
        case_parent = tmp_path / "SOURCE"
        if not _same_file(case_parent, real_root):
            pytest.skip("case-insensitive filesystem unavailable: absolute target bound")
        absolute_target = case_parent / "target"

    (real_root / "alias").symlink_to(absolute_target, target_is_directory=True)
    monkeypatch.setenv("CSK_CONFIG", str(tmp_path / "home" / "config.json"))
    if entry == "individual":
        selected = resolve_individual(
            root,
            IndividualSelector(
                name="review", from_alias="local", directory="alias/review"
            ),
        )
        assert selected.name == "review"
    else:
        selected = expand_collection(
            root, _collection("alias", ("review",))
        )
        assert [member.name for member in selected] == ["review"]


def test_selector_escape_symlink_fails_even_when_target_exists(tmp_path: Path) -> None:
    root = tmp_path / "src"
    root.mkdir()
    outside = tmp_path / "outside" / "review"
    _write_skill(outside, "review")
    (root / "skills").symlink_to(tmp_path / "outside", target_is_directory=True)
    with pytest.raises(source_errors.SourceError) as excinfo:
        resolve_selector_directory(root, "skills/review")
    assert excinfo.value.code == source_errors.CODE_SELECTION_INVALID


def test_selector_escape_into_csk_home_fails_before_managed_pruning(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Directory resolution rejects a managed target outside the source root."""

    home = tmp_path / "home"
    monkeypatch.setenv("CSK_CONFIG", str(home / "config.json"))
    root = tmp_path / "src"
    root.mkdir()
    target = home / "generated"
    _write_skill(target, "generated")
    (root / "link").symlink_to(target, target_is_directory=True)
    with pytest.raises(source_errors.SourceError) as excinfo:
        resolve_selector_directory(root, "link")
    assert excinfo.value.code == source_errors.CODE_SELECTION_INVALID


def test_selector_escape_never_reads_outside(tmp_path: Path) -> None:
    """The escape refusal does not depend on outside bytes.

    Both a valid and an invalid outside skill fail with the same selection
    code, proving the gate fires before any member read.
    """

    for variant in ("valid", "invalid"):
        root = tmp_path / f"src-{variant}"
        root.mkdir()
        outside = tmp_path / f"outside-{variant}" / "review"
        outside.mkdir(parents=True)
        if variant == "valid":
            _write_skill(outside, "review")
        else:
            (outside / "SKILL.md").write_text("no frontmatter here\n", encoding="utf-8")
        (root / "skills").symlink_to(tmp_path / f"outside-{variant}", target_is_directory=True)
        with pytest.raises(source_errors.SourceError) as excinfo:
            resolve_selector_directory(root, "skills/review")
        assert excinfo.value.code == source_errors.CODE_SELECTION_INVALID


def test_selector_nonportable_directory_fails(tmp_path: Path) -> None:
    root = tmp_path / "src"
    root.mkdir()
    with pytest.raises(source_errors.SourceError) as excinfo:
        resolve_selector_directory(root, "a/../b")
    assert excinfo.value.code == source_errors.CODE_SELECTION_INVALID


@pytest.mark.parametrize("entry", ["individual", "collection"])
def test_missing_component_before_parent_is_not_existing_selector(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, entry: str
) -> None:
    """A missing symlink component is never collapsed across ``..``."""

    root = tmp_path / "source"
    package = root / "real" / "review"
    _write_skill(package, "review", "A review skill")
    monkeypatch.setenv("CSK_CONFIG", str(tmp_path / "home" / "config.json"))
    link = root / "link"
    link.symlink_to("missing/../real", target_is_directory=True)
    assert not link.exists()
    assert not link.is_dir()
    if entry == "individual":
        run = lambda directory: resolve_individual(
            root,
            IndividualSelector(
                name="review", from_alias="local", directory=f"{directory}/review"
            ),
        )
    else:
        run = lambda directory: expand_collection(
            root, _collection(directory, ("*",))
        )
    run("real")
    with pytest.raises(source_errors.SourceError) as excinfo:
        run("link")
    assert excinfo.value.code == source_errors.CODE_SELECTION_INVALID


def test_individual_missing_directory_fails_selection_invalid(tmp_path: Path) -> None:
    root = tmp_path / "src"
    root.mkdir()
    with pytest.raises(source_errors.SourceError) as excinfo:
        resolve_individual(
            root, IndividualSelector(name="review", from_alias="local", directory="nope")
        )
    assert excinfo.value.code == source_errors.CODE_SELECTION_INVALID


def test_individual_file_directory_fails_selection_invalid(tmp_path: Path) -> None:
    root = tmp_path / "src"
    root.mkdir()
    (root / "afile").write_text("x", encoding="utf-8")
    with pytest.raises(source_errors.SourceError) as excinfo:
        resolve_individual(
            root, IndividualSelector(name="review", from_alias="local", directory="afile")
        )
    assert excinfo.value.code == source_errors.CODE_SELECTION_INVALID


def test_selection_never_reads_outside_member_symlink(tmp_path: Path) -> None:
    root = tmp_path / "src"
    collection = root / "col"
    collection.mkdir(parents=True)
    outside = tmp_path / "outside-skill"
    _write_skill(outside, "evil")
    (collection / "evil").symlink_to(outside, target_is_directory=True)
    with pytest.raises(source_errors.SourceError) as excinfo:
        expand_collection(root, _collection("col", ("evil",)))
    assert excinfo.value.code == source_errors.CODE_SELECTION_INVALID


def test_selection_case_variant_directory_bound(tmp_path: Path) -> None:
    """Case-variant containment on a case-insensitive filesystem.

    On a case-sensitive host the two spellings are distinct directories and
    the test declares the platform bound instead of forcing a collision.
    """

    root = tmp_path / "src"
    (root / "Skills").mkdir(parents=True)
    _write_skill(root / "Skills" / "review", "review")
    probe = root / "SKILLS"
    try:
        probe.mkdir(exist_ok=False)
    except FileExistsError:
        pass
    if not _same_file(root / "Skills", probe):
        pytest.skip("case-insensitive filesystem not available: bound declared")
    resolved = resolve_selector_directory(root, "SKILLS/review")
    assert resolved.is_dir()


def _same_file(first: Path, second: Path) -> bool:
    try:
        return os.path.samefile(first, second)
    except OSError:
        return False


# ---------------------------------------------------------------------------
# AC (b): immediate children, literals before exclusions, pruning, excludes
# ---------------------------------------------------------------------------


def test_collection_literals_select_immediate_children(tmp_path: Path) -> None:
    root = tmp_path / "src"
    _write_skill(root / "col" / "review", "review")
    _write_skill(root / "col" / "docs", "docs")
    members = expand_collection(root, _collection("col", ("review", "docs")))
    assert [(member.folder, member.name) for member in members] == [
        ("docs", "docs"),
        ("review", "review"),
    ]


def test_collection_star_ignores_files_and_nested_paths(tmp_path: Path) -> None:
    root = tmp_path / "src"
    _write_skill(root / "col" / "review", "review")
    (root / "col" / "notes.txt").write_text("not a member", encoding="utf-8")
    (root / "col" / "review" / "nested").mkdir()
    members = expand_collection(root, _collection("col", ("*",)))
    assert [member.folder for member in members] == ["review"]


def test_collection_missing_literal_fails_member_missing(tmp_path: Path) -> None:
    root = tmp_path / "src"
    (root / "col").mkdir(parents=True)
    with pytest.raises(source_errors.SourceError) as excinfo:
        expand_collection(root, _collection("col", ("missing",)))
    assert excinfo.value.code == source_errors.CODE_MEMBER_MISSING


def test_collection_missing_excluded_literal_still_fails(tmp_path: Path) -> None:
    root = tmp_path / "src"
    (root / "col").mkdir(parents=True)
    with pytest.raises(source_errors.SourceError) as excinfo:
        expand_collection(root, _collection("col", ("missing",), ("missing",)))
    assert excinfo.value.code == source_errors.CODE_MEMBER_MISSING


def test_collection_file_literal_fails_before_exclusions(tmp_path: Path) -> None:
    root = tmp_path / "src"
    (root / "col").mkdir(parents=True)
    (root / "col" / "afile").write_text("x", encoding="utf-8")
    with pytest.raises(source_errors.SourceError) as excinfo:
        expand_collection(root, _collection("col", ("afile",), ("afile",)))
    assert excinfo.value.code == source_errors.CODE_MEMBER_INVALID
    assert "not a directory" in excinfo.value.detail


def test_collection_star_prunes_managed_outputs(tmp_path: Path) -> None:
    root = tmp_path / "src"
    _write_skill(root / "col" / "review", "review")
    for pruned in (".git", ".agents", ".claude", ".codex", ".cursor", ".gemini"):
        (root / "col" / pruned).mkdir(parents=True)
    members = expand_collection(root, _collection("col", ("*",)))
    assert [member.folder for member in members] == ["review"]


def test_collection_star_pruned_invalid_is_not_validated(tmp_path: Path) -> None:
    root = tmp_path / "src"
    _write_skill(root / "col" / "review", "review")
    pruned = root / "col" / ".git"
    pruned.mkdir(parents=True)
    (pruned / "SKILL.md").write_text("broken\n", encoding="utf-8")
    members = expand_collection(root, _collection("col", ("*",)))
    assert [member.folder for member in members] == ["review"]


def test_collection_exclude_removes_and_missing_is_harmless(tmp_path: Path) -> None:
    root = tmp_path / "src"
    _write_skill(root / "col" / "review", "review")
    _write_skill(root / "col" / "docs", "docs")
    members = expand_collection(root, _collection("col", ("*",), ("docs", "absent")))
    assert [member.folder for member in members] == ["review"]


def test_collection_excluded_invalid_is_not_validated(tmp_path: Path) -> None:
    root = tmp_path / "src"
    _write_skill(root / "col" / "review", "review")
    bad = root / "col" / "bad"
    bad.mkdir(parents=True)
    (bad / "SKILL.md").write_text("broken\n", encoding="utf-8")
    members = expand_collection(root, _collection("col", ("*",), ("bad",)))
    assert [member.folder for member in members] == ["review"]


# ---------------------------------------------------------------------------
# AC (c): SKILL.md frontmatter plus package rules, whole-operation failure
# ---------------------------------------------------------------------------


def test_collection_member_missing_skill_md_fails(tmp_path: Path) -> None:
    root = tmp_path / "src"
    (root / "col" / "review").mkdir(parents=True)
    with pytest.raises(source_errors.SourceError) as excinfo:
        expand_collection(root, _collection("col", ("review",)))
    assert excinfo.value.code == source_errors.CODE_MEMBER_INVALID


@pytest.mark.parametrize(
    "frontmatter",
    [
        "no frontmatter\n",
        "---\ndescription: only description\n---\n",
        "---\nname: review\n---\n",
        "---\nname: \ndescription: x\n---\n",
        "---\nname: review\ndescription: \n---\n",
        "---\nname: review\ndescription: x\n",
        "---\nname: con\ndescription: reserved device name\n---\n",
        "---\nname: review\ndescription: x\ntriggers: notalist\n---\n",
        "---\nname: review\ndescription: x\ntriggers: []\n---\n",
    ],
    ids=[
        "no-frontmatter",
        "missing-name",
        "missing-description",
        "empty-name",
        "empty-description",
        "unclosed",
        "reserved-name",
        "triggers-scalar",
        "triggers-empty",
    ],
)
def test_collection_member_bad_frontmatter_fails(tmp_path: Path, frontmatter: str) -> None:
    root = tmp_path / "src"
    member = root / "col" / "review"
    member.mkdir(parents=True)
    (member / "SKILL.md").write_text(frontmatter, encoding="utf-8")
    with pytest.raises(source_errors.SourceError) as excinfo:
        expand_collection(root, _collection("col", ("review",)))
    assert excinfo.value.code == source_errors.CODE_MEMBER_INVALID


def test_collection_member_invalid_manifest_fails(tmp_path: Path) -> None:
    root = tmp_path / "src"
    _write_skill(
        root / "col" / "review",
        "review",
        manifest='{"schema_version": 2, "bogus": true}',
    )
    with pytest.raises(source_errors.SourceError) as excinfo:
        expand_collection(root, _collection("col", ("review",)))
    assert excinfo.value.code == source_errors.CODE_MEMBER_INVALID


def test_collection_star_invalid_member_fails_whole_operation(tmp_path: Path) -> None:
    root = tmp_path / "src"
    _write_skill(root / "col" / "review", "review")
    bad = root / "col" / "docs"
    bad.mkdir(parents=True)
    with pytest.raises(source_errors.SourceError) as excinfo:
        expand_collection(root, _collection("col", ("*",)))
    assert excinfo.value.code == source_errors.CODE_MEMBER_INVALID


def test_collection_empty_after_exclusions_fails(tmp_path: Path) -> None:
    root = tmp_path / "src"
    _write_skill(root / "col" / "review", "review")
    with pytest.raises(source_errors.SourceError) as excinfo:
        expand_collection(root, _collection("col", ("review",), ("review",)))
    assert excinfo.value.code == source_errors.CODE_MEMBER_INVALID


def test_collection_star_empty_directory_fails(tmp_path: Path) -> None:
    root = tmp_path / "src"
    (root / "col").mkdir(parents=True)
    with pytest.raises(source_errors.SourceError) as excinfo:
        expand_collection(root, _collection("col", ("*",)))
    assert excinfo.value.code == source_errors.CODE_MEMBER_INVALID


def test_collection_member_frontmatter_variants_parse(tmp_path: Path) -> None:
    root = tmp_path / "src"
    _write_skill(
        root / "col" / "quoted",
        "ignored",
        raw_frontmatter='---\nname: "quoted"\ndescription: \'Does "things"\'\n---\n# q\n',
    )
    _write_skill(
        root / "col" / "listed",
        "ignored",
        raw_frontmatter="---\nname: listed\ndescription: Has triggers\ntriggers:\n  - one\n  - two\n---\n# l\n",
    )
    _write_skill(
        root / "col" / "blocked",
        "ignored",
        raw_frontmatter="---\nname: blocked\ndescription: |\n  line one\n  line two\n---\n# b\n",
    )
    members = expand_collection(root, _collection("col", ("*",)))
    assert [member.name for member in members] == ["blocked", "listed", "quoted"]


# ---------------------------------------------------------------------------
# AC (d): individual name equality with SKILL.md and manifest identity
# ---------------------------------------------------------------------------


def test_individual_name_matches_skill_md(tmp_path: Path) -> None:
    root = tmp_path / "src"
    _write_skill(root / "skills" / "review", "review")
    member = resolve_individual(
        root, IndividualSelector(name="review", from_alias="local", directory="skills/review")
    )
    assert member.name == "review"


def test_individual_name_mismatch_fails_selection_invalid(tmp_path: Path) -> None:
    root = tmp_path / "src"
    _write_skill(root / "skills" / "review", "other")
    with pytest.raises(source_errors.SourceError) as excinfo:
        resolve_individual(
            root,
            IndividualSelector(name="review", from_alias="local", directory="skills/review"),
        )
    assert excinfo.value.code == source_errors.CODE_SELECTION_INVALID


def test_individual_manifest_name_matches(tmp_path: Path) -> None:
    root = tmp_path / "src"
    _write_skill(
        root / "skills" / "review",
        "review",
        manifest='{"schema_version": 1, "name": "review"}',
    )
    member = resolve_individual(
        root, IndividualSelector(name="review", from_alias="local", directory="skills/review")
    )
    assert member.name == "review"


def test_individual_manifest_name_mismatch_fails_member_invalid(tmp_path: Path) -> None:
    root = tmp_path / "src"
    _write_skill(
        root / "skills" / "review",
        "review",
        manifest='{"schema_version": 1, "name": "other"}',
    )
    with pytest.raises(source_errors.SourceError) as excinfo:
        resolve_individual(
            root,
            IndividualSelector(name="review", from_alias="local", directory="skills/review"),
        )
    assert excinfo.value.code == source_errors.CODE_MEMBER_INVALID


def test_individual_invalid_skill_md_fails_member_invalid(tmp_path: Path) -> None:
    root = tmp_path / "src"
    (root / "skills" / "review").mkdir(parents=True)
    with pytest.raises(source_errors.SourceError) as excinfo:
        resolve_individual(
            root, IndividualSelector(name="review", from_alias="local", directory="skills/review")
        )
    assert excinfo.value.code == source_errors.CODE_MEMBER_INVALID


def test_individual_invalid_manifest_fails_member_invalid(tmp_path: Path) -> None:
    root = tmp_path / "src"
    _write_skill(
        root / "skills" / "review",
        "review",
        manifest='{"schema_version": 2, "bogus": true}',
    )
    with pytest.raises(source_errors.SourceError) as excinfo:
        resolve_individual(
            root, IndividualSelector(name="review", from_alias="local", directory="skills/review")
        )
    assert excinfo.value.code == source_errors.CODE_MEMBER_INVALID


# ---------------------------------------------------------------------------
# AC (e): ascending UTF-8 folder-name byte order
# ---------------------------------------------------------------------------


def test_collection_expansion_order_is_utf8_byte_order(tmp_path: Path) -> None:
    """Byte order differs from case-insensitive order on these three names.

    UTF-8 bytes: ``Banana`` (0x42..) < ``Zebra`` (0x5A..) < ``apple`` (0x61..).
    Case-insensitive: ``apple`` < ``Banana`` < ``Zebra``. Installed names are
    deliberately skewed so folder order cannot be confused with name order.
    """

    root = tmp_path / "src"
    _write_skill(root / "col" / "Zebra", "aaa-first")
    _write_skill(root / "col" / "apple", "zzz-last")
    _write_skill(root / "col" / "Banana", "mmm-middle")
    members = expand_collection(root, _collection("col", ("*",)))
    assert [member.folder for member in members] == ["Banana", "Zebra", "apple"]
    assert [member.name for member in members] == ["mmm-middle", "aaa-first", "zzz-last"]
    byte_sorted = sorted(["Zebra", "apple", "Banana"], key=lambda value: value.encode("utf-8"))
    folded_sorted = sorted(["Zebra", "apple", "Banana"], key=lambda value: value.casefold())
    assert byte_sorted != folded_sorted


def test_collection_literal_order_is_normalized_to_byte_order(tmp_path: Path) -> None:
    root = tmp_path / "src"
    _write_skill(root / "col" / "b", "b")
    _write_skill(root / "col" / "a", "a")
    members = expand_collection(root, _collection("col", ("b", "a")))
    assert [member.folder for member in members] == ["a", "b"]


# ---------------------------------------------------------------------------
# AC (f): destination conflicts before any publication
# ---------------------------------------------------------------------------


def test_duplicate_installed_names_fail_name_conflict(tmp_path: Path) -> None:
    root = tmp_path / "src"
    _write_skill(root / "col" / "a", "review")
    _write_skill(root / "col" / "b", "review")
    with pytest.raises(source_errors.SourceError) as excinfo:
        expand_collection(root, _collection("col", ("a", "b")))
    assert excinfo.value.code == source_errors.CODE_NAME_CONFLICT


def test_identical_individual_selections_fail_name_conflict(tmp_path: Path) -> None:
    root = tmp_path / "src"
    _write_skill(root / "skills" / "review", "review")
    first = IndividualSelector(name="review", from_alias="local", directory="skills/review")
    second = IndividualSelector(name="review", from_alias="local", directory="skills/review")
    with pytest.raises(source_errors.SourceError) as excinfo:
        expand_selectors([first, second], {"local": root})
    assert excinfo.value.code == source_errors.CODE_NAME_CONFLICT


def test_repeated_literal_in_one_collection_fails_name_conflict(tmp_path: Path) -> None:
    """A repeated direct literal is not silently deduplicated by expansion."""

    root = tmp_path / "src"
    _write_skill(root / "col" / "review", "review")
    with pytest.raises(source_errors.SourceError) as excinfo:
        expand_collection(root, _collection("col", ("review", "review")))
    assert excinfo.value.code == source_errors.CODE_NAME_CONFLICT


def test_case_insensitive_collision_fails_name_conflict(tmp_path: Path) -> None:
    root = tmp_path / "src"
    _write_skill(root / "col" / "upper", "Review")
    _write_skill(root / "col" / "lower", "review")
    with pytest.raises(source_errors.SourceError) as excinfo:
        expand_collection(root, _collection("col", ("upper", "lower")))
    assert excinfo.value.code == source_errors.CODE_NAME_CONFLICT


def test_unicode_equivalent_collision_fails_name_conflict(tmp_path: Path) -> None:
    nfc = unicodedata.normalize("NFC", "caf\u00e9")
    nfd = unicodedata.normalize("NFD", "caf\u00e9")
    assert nfc != nfd
    root = tmp_path / "src"
    _write_skill(root / "col" / "first", nfc)
    _write_skill(root / "col" / "second", nfd)
    with pytest.raises(source_errors.SourceError) as excinfo:
        expand_collection(root, _collection("col", ("first", "second")))
    assert excinfo.value.code == source_errors.CODE_NAME_CONFLICT


def test_cross_selector_duplicate_fails_name_conflict(tmp_path: Path) -> None:
    root = tmp_path / "src"
    _write_skill(root / "one" / "review", "review")
    _write_skill(root / "two" / "review", "review")
    selectors = [
        IndividualSelector(name="review", from_alias="local", directory="one/review"),
        IndividualSelector(name="review", from_alias="other", directory="two/review"),
    ]
    with pytest.raises(source_errors.SourceError) as excinfo:
        expand_selectors(selectors, {"local": root, "other": root})
    assert excinfo.value.code == source_errors.CODE_NAME_CONFLICT


def test_reserved_names_conflict_with_expansion(tmp_path: Path) -> None:
    root = tmp_path / "src"
    _write_skill(root / "col" / "review", "review")
    with pytest.raises(source_errors.SourceError) as excinfo:
        expand_selectors(
            [_collection("col", ("review",))], {"local": root}, reserved_names=("review",)
        )
    assert excinfo.value.code == source_errors.CODE_NAME_CONFLICT


def test_expand_selectors_missing_root_fails_selection_invalid(tmp_path: Path) -> None:
    root = tmp_path / "src"
    root.mkdir()
    with pytest.raises(source_errors.SourceError) as excinfo:
        expand_selectors([_collection(".", ("*",))], {})
    assert excinfo.value.code == source_errors.CODE_SELECTION_INVALID


# ---------------------------------------------------------------------------
# F1: metadata/package read boundary (no reads outside the source root)
# ---------------------------------------------------------------------------


def _counting_content_reads(
    monkeypatch: pytest.MonkeyPatch,
) -> list[tuple[str, str]]:
    calls: list[tuple[str, str]] = []
    orig_read_text = Path.read_text
    orig_read_bytes = Path.read_bytes

    def counting_read_text(self: Path, *args: object, **kwargs: object) -> str:
        calls.append(("text", str(self)))
        return orig_read_text(self, *args, **kwargs)  # type: ignore[arg-type]

    def counting_read_bytes(self: Path, *args: object, **kwargs: object) -> bytes:
        calls.append(("bytes", str(self)))
        return orig_read_bytes(self, *args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(Path, "read_text", counting_read_text)
    monkeypatch.setattr(Path, "read_bytes", counting_read_bytes)
    return calls


def test_member_skill_md_symlink_escape_rejected_without_external_read(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An escaping SKILL.md link fails before its outside bytes are opened."""

    root = tmp_path / "src"
    member = root / "col" / "review"
    member.mkdir(parents=True)
    outside = tmp_path / "outside.md"
    outside.write_text(
        "---\nname: evil\ndescription: evil outside\n---\n# evil\n", encoding="utf-8"
    )
    (member / "SKILL.md").symlink_to(outside)
    calls = _counting_content_reads(monkeypatch)
    with pytest.raises(source_errors.SourceError) as excinfo:
        expand_collection(root, _collection("col", ("review",)))
    assert excinfo.value.code == source_errors.CODE_MEMBER_INVALID
    assert "escapes the source root" in excinfo.value.detail
    assert calls == [], f"outside bytes were opened: {calls}"


def test_individual_skill_md_symlink_escape_rejected_without_external_read(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "src"
    member = root / "skills" / "review"
    member.mkdir(parents=True)
    outside = tmp_path / "outside.md"
    outside.write_text(
        "---\nname: review\ndescription: valid outside\n---\n# review\n",
        encoding="utf-8",
    )
    (member / "SKILL.md").symlink_to(outside)
    calls = _counting_content_reads(monkeypatch)
    with pytest.raises(source_errors.SourceError) as excinfo:
        resolve_individual(
            root,
            IndividualSelector(
                name="review", from_alias="local", directory="skills/review"
            ),
        )
    assert excinfo.value.code == source_errors.CODE_MEMBER_INVALID
    assert "escapes the source root" in excinfo.value.detail
    assert calls == [], f"outside bytes were opened: {calls}"


def test_member_manifest_symlink_escape_rejected_without_external_read(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An escaping manifest link fails before any package content is read."""

    root = tmp_path / "src"
    member = root / "col" / "review"
    _write_skill(member, "review")
    outside = tmp_path / "outside-manifest.json"
    outside.write_text('{"schema_version": 1, "name": "review"}', encoding="utf-8")
    (member / "agent-skill.json").symlink_to(outside)
    calls = _counting_content_reads(monkeypatch)
    with pytest.raises(source_errors.SourceError) as excinfo:
        expand_collection(root, _collection("col", ("review",)))
    assert excinfo.value.code == source_errors.CODE_MEMBER_INVALID
    assert "escapes the source root" in excinfo.value.detail
    assert calls == [], f"outside bytes were opened: {calls}"


def test_member_nested_symlink_escape_rejected_before_skillcheck_read(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A nested escaping link fails before skillcheck opens package files."""

    root = tmp_path / "src"
    member = root / "col" / "review"
    _write_skill(member, "review")
    references = member / "references"
    references.mkdir()
    outside = tmp_path / "outside-note.md"
    outside.write_text("# outside\n", encoding="utf-8")
    (references / "evil.md").symlink_to(outside)
    calls = _counting_content_reads(monkeypatch)
    with pytest.raises(source_errors.SourceError) as excinfo:
        expand_collection(root, _collection("col", ("review",)))
    assert excinfo.value.code == source_errors.CODE_MEMBER_INVALID
    assert "escapes the source root" in excinfo.value.detail
    assert calls == [], f"outside bytes were opened: {calls}"


def test_member_inside_skill_md_link_rejected(tmp_path: Path) -> None:
    """Links inside admitted inputs are rejected even without an escape."""

    root = tmp_path / "src"
    member = root / "col" / "review"
    member.mkdir(parents=True)
    (member / "real.md").write_text(
        "---\nname: review\ndescription: inside target\n---\n# review\n",
        encoding="utf-8",
    )
    (member / "SKILL.md").symlink_to(member / "real.md")
    with pytest.raises(source_errors.SourceError) as excinfo:
        expand_collection(root, _collection("col", ("review",)))
    assert excinfo.value.code == source_errors.CODE_MEMBER_INVALID
    assert "link" in excinfo.value.detail


# ---------------------------------------------------------------------------
# F2: pruning by physical/equivalent identity
# ---------------------------------------------------------------------------


def _probes_same_file(first: Path, second: Path) -> bool:
    try:
        return first.exists() and os.path.samefile(first, second)
    except OSError:
        return False


def test_collection_star_prunes_case_alias_git(tmp_path: Path) -> None:
    """On a case-insensitive host `.GIT` prunes as `.git` (authored control kept)."""

    root = tmp_path / "src"
    base = root / "col"
    _write_skill(base / "normal", "review")
    _write_skill(base / ".GIT", "generated")
    if not _probes_same_file(base / ".git", base / ".GIT"):
        pytest.skip(
            "case-insensitive filesystem not available: "
            ".GIT/.git alias bound (pruning needs actual FS equivalence)"
        )
    members = expand_collection(root, _collection("col", ("*",)))
    assert [member.name for member in members] == ["review"]


def test_collection_star_prunes_case_alias_managed_output(tmp_path: Path) -> None:
    """On a case-insensitive host `.AGENTS` prunes as `.agents`."""

    root = tmp_path / "src"
    base = root / "col"
    _write_skill(base / "normal", "review")
    _write_skill(base / ".AGENTS", "generated")
    if not _probes_same_file(base / ".agents", base / ".AGENTS"):
        pytest.skip(
            "case-insensitive filesystem not available: "
            ".AGENTS/.agents alias bound (pruning needs actual FS equivalence)"
        )
    members = expand_collection(root, _collection("col", ("*",)))
    assert [member.name for member in members] == ["review"]


def test_collection_star_case_variant_distinct_is_selected(tmp_path: Path) -> None:
    """Without FS aliasing a case variant stays authored input (no naive fold)."""

    root = tmp_path / "src"
    base = root / "col"
    _write_skill(base / "normal", "review")
    _write_skill(base / ".GIT", "generated")
    if _probes_same_file(base / ".git", base / ".GIT"):
        pytest.skip(
            "case-insensitive filesystem aliases .GIT/.git: "
            "distinct-input bound (pruning test covers this host)"
        )
    members = expand_collection(root, _collection("col", ("*",)))
    assert [member.folder for member in members] == sorted(
        ["normal", ".GIT"], key=lambda value: value.encode("utf-8")
    )


def test_collection_star_prunes_linked_alias_git(tmp_path: Path) -> None:
    """A symlink to the sibling `.git` directory prunes like `.git` itself."""

    root = tmp_path / "src"
    base = root / "col"
    _write_skill(base / "normal", "review")
    pruned = base / ".git"
    pruned.mkdir(parents=True)
    (pruned / "SKILL.md").write_text("broken\n", encoding="utf-8")
    (base / "evil-link").symlink_to(pruned, target_is_directory=True)
    members = expand_collection(root, _collection("col", ("*",)))
    assert [member.folder for member in members] == ["normal"]


def test_collection_star_prunes_linked_alias_managed_output(tmp_path: Path) -> None:
    """A symlink to the sibling `.agents` directory prunes with it."""

    root = tmp_path / "src"
    base = root / "col"
    _write_skill(base / "normal", "review")
    pruned = base / ".agents"
    pruned.mkdir(parents=True)
    (pruned / "SKILL.md").write_text("broken\n", encoding="utf-8")
    (base / "agents-link").symlink_to(pruned, target_is_directory=True)
    members = expand_collection(root, _collection("col", ("*",)))
    assert [member.folder for member in members] == ["normal"]


@pytest.mark.parametrize("managed", [".agents", ".git", ".codex"])
def test_managed_descendant_alias_pruned(tmp_path: Path, managed: str) -> None:
    """A child alias into a managed-output DESCENDANT prunes (subtree class).

    The target carries valid SKILL.md, so without subtree containment it
    would be discovered as `generated` alongside the authored skill.
    """

    root = tmp_path / "src"
    _write_skill(root / "normal", "review")
    _write_skill(root / managed / "nested" / "generated", "generated")
    (root / "alias").symlink_to(
        root / managed / "nested" / "generated", target_is_directory=True
    )
    members = expand_collection(root, _collection(".", ("*",)))
    assert [member.name for member in members] == ["review"]


def test_managed_descendant_alias_chain_pruned(tmp_path: Path) -> None:
    """A two-hop link chain into a managed descendant still prunes."""

    root = tmp_path / "src"
    _write_skill(root / "normal", "review")
    _write_skill(root / ".agents" / "nested" / "deep-target", "deep")
    (root / "hop").symlink_to(
        root / ".agents" / "nested" / "deep-target", target_is_directory=True
    )
    (root / "alias").symlink_to(root / "hop", target_is_directory=True)
    members = expand_collection(root, _collection(".", ("*",)))
    assert [member.name for member in members] == ["review"]


def test_managed_descendant_alias_pruned_from_nested_base(tmp_path: Path) -> None:
    """A child alias into the source-root managed subtree prunes from any base.

    Per protocol section 2 ("reject a selected package within ANY managed
    output") the managed roots are anchored at the source root as well as at
    the enumerated collection base.
    """

    root = tmp_path / "src"
    _write_skill(root / "col" / "normal", "review")
    _write_skill(root / ".agents" / "nested" / "nested-out", "nested")
    (root / "col" / "alias").symlink_to(
        root / ".agents" / "nested" / "nested-out", target_is_directory=True
    )
    members = expand_collection(root, _collection("col", ("*",)))
    assert [member.name for member in members] == ["review"]


def test_collection_star_near_miss_names_are_selected(tmp_path: Path) -> None:
    """Authored near-miss names are never pruned (no naive fold or prefix)."""

    root = tmp_path / "src"
    base = root / "col"
    _write_skill(base / "agents", "agents-skill")
    (base / "agents" / "skills").mkdir()
    _write_skill(base / "git", "git-skill")
    _write_skill(base / ".githooks", "hooks-skill")
    members = expand_collection(root, _collection("col", ("*",)))
    assert [member.folder for member in members] == [".githooks", "agents", "git"]


@pytest.mark.parametrize("managed", [".agents", ".git", ".codex"])
def test_nested_managed_descendant_alias_pruned(tmp_path: Path, managed: str) -> None:
    """A child alias into a NESTED-workspace managed descendant prunes.

    Committed reviewer attack (rev3): the target lives under an intermediate
    ``workspace`` directory, so anchor enumeration at the source root or the
    collection base cannot see it. Only the physical-ancestry walk prunes it.
    """

    root = tmp_path / "src"
    base = root / "collection"
    _write_skill(base / "authored", "review")
    generated = root / "workspace" / managed / "nested" / "generated"
    _write_skill(generated, "generated")
    (base / "alias").symlink_to(generated, target_is_directory=True)
    members = expand_collection(root, _collection("collection", ("*",)))
    assert [member.name for member in members] == ["review"]


def test_collection_star_prunes_child_inside_csk_home(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A child alias into the csk home prunes (runtime/staging/snapshot outputs)."""

    home = tmp_path / "home"
    monkeypatch.setenv("CSK_CONFIG", str(home / "config.json"))
    root = tmp_path / "src"
    base = root / "col"
    _write_skill(base / "normal", "review")
    generated = home / "source-v1" / "snapshots" / "generated"
    _write_skill(generated, "generated")
    (base / "alias").symlink_to(generated, target_is_directory=True)
    members = expand_collection(root, _collection("col", ("*",)))
    assert [member.name for member in members] == ["review"]


# ---------------------------------------------------------------------------
# F3: frontmatter type semantics (unquoted non-strings rejected)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "value",
    ["null", "Null", "NULL", "~", "true", "True", "TRUE", "false", "42", "-7", "3.14", "{}", "{a: b}"],
    ids=[
        "null",
        "Null",
        "NULL",
        "tilde",
        "true",
        "True",
        "TRUE",
        "false",
        "int",
        "negative-int",
        "float",
        "empty-mapping",
        "mapping",
    ],
)
def test_collection_non_string_description_rejected(
    tmp_path: Path, value: str
) -> None:
    root = tmp_path / "src"
    member = root / "col" / "review"
    member.mkdir(parents=True)
    (member / "SKILL.md").write_text(
        f"---\nname: review\ndescription: {value}\n---\n# review\n", encoding="utf-8"
    )
    with pytest.raises(source_errors.SourceError) as excinfo:
        expand_collection(root, _collection("col", ("review",)))
    assert excinfo.value.code == source_errors.CODE_MEMBER_INVALID


@pytest.mark.parametrize(
    "value",
    ["null", "true", "42", "{}", "3.14"],
    ids=["null", "true", "int", "empty-mapping", "float"],
)
def test_collection_non_string_name_rejected(tmp_path: Path, value: str) -> None:
    root = tmp_path / "src"
    member = root / "col" / "review"
    member.mkdir(parents=True)
    (member / "SKILL.md").write_text(
        f"---\nname: {value}\ndescription: valid description\n---\n# skill\n",
        encoding="utf-8",
    )
    with pytest.raises(source_errors.SourceError) as excinfo:
        expand_collection(root, _collection("col", ("review",)))
    assert excinfo.value.code == source_errors.CODE_MEMBER_INVALID


@pytest.mark.parametrize(
    "field,value",
    [
        ("description", "null"),
        ("description", "true"),
        ("description", "42"),
        ("description", "{}"),
        ("name", "null"),
        ("name", "42"),
    ],
    ids=[
        "description-null",
        "description-true",
        "description-int",
        "description-mapping",
        "name-null",
        "name-int",
    ],
)
def test_individual_non_string_frontmatter_rejected(
    tmp_path: Path, field: str, value: str
) -> None:
    root = tmp_path / "src"
    member = root / "skills" / "review"
    member.mkdir(parents=True)
    name_token = value if field == "name" else "review"
    description_token = value if field == "description" else "valid description"
    (member / "SKILL.md").write_text(
        f"---\nname: {name_token}\ndescription: {description_token}\n---\n# skill\n",
        encoding="utf-8",
    )
    with pytest.raises(source_errors.SourceError) as excinfo:
        resolve_individual(
            root,
            IndividualSelector(
                name="review", from_alias="local", directory="skills/review"
            ),
        )
    assert excinfo.value.code == source_errors.CODE_MEMBER_INVALID


def test_collection_quoted_non_string_spellings_accepted(tmp_path: Path) -> None:
    """Quoted scalars stay strings even when they spell null/bool/number."""

    root = tmp_path / "src"
    _write_skill(
        root / "col" / "quoted-null",
        "ignored",
        raw_frontmatter='---\nname: "null"\ndescription: "true"\n---\n# q\n',
    )
    _write_skill(
        root / "col" / "quoted-num",
        "ignored",
        raw_frontmatter="---\nname: 'num42'\ndescription: '42'\n---\n# q\n",
    )
    _write_skill(
        root / "col" / "quoted-map",
        "ignored",
        raw_frontmatter='---\nname: mapskill\ndescription: "{}"\n---\n# q\n',
    )
    _write_skill(
        root / "col" / "quoted-hex",
        "ignored",
        raw_frontmatter='---\nname: "0x2A"\ndescription: \'0o52\'\n---\n# q\n',
    )
    members = expand_collection(root, _collection("col", ("*",)))
    assert [member.name for member in members] == [
        "0x2A",
        "mapskill",
        "null",
        "num42",
    ]


def test_individual_quoted_non_string_spellings_accepted(tmp_path: Path) -> None:
    root = tmp_path / "src"
    member = root / "skills" / "review"
    member.mkdir(parents=True)
    (member / "SKILL.md").write_text(
        '---\nname: "review"\ndescription: "null"\n---\n# review\n', encoding="utf-8"
    )
    selected = resolve_individual(
        root,
        IndividualSelector(name="review", from_alias="local", directory="skills/review"),
    )
    assert selected.name == "review"


@pytest.mark.parametrize(
    "value",
    ["1" * 5000, "true\t# comment", "0x2A\t# comment"],
    ids=["long-decimal", "bool-tab-comment", "hex-tab-comment"],
)
@pytest.mark.parametrize("entry", ["individual", "collection"])
def test_nonstring_description_still_refused(
    tmp_path: Path, value: str, entry: str
) -> None:
    """Conversion failure and tab comments never admit a non-string description.

    Committed reviewer attack (rev3): a 5000-digit integer refuses like ``42``
    (recognition is independent of conversion, and there is no conversion);
    ``true`` and ``0x2A`` followed by TAB + ``# comment`` refuse like their
    space-separated forms (comments separate on any preceding whitespace).
    """

    root = tmp_path / "src"
    member = root / "review"
    member.mkdir(parents=True)
    (member / "SKILL.md").write_text(
        f"---\nname: review\ndescription: {value}\n---\n# review\n",
        encoding="utf-8",
    )
    with pytest.raises(source_errors.SourceError) as excinfo:
        if entry == "individual":
            resolve_individual(
                root,
                IndividualSelector(
                    name="review", from_alias="local", directory="review"
                ),
            )
        else:
            expand_collection(root, _collection(".", ("*",)))
    assert excinfo.value.code == source_errors.CODE_MEMBER_INVALID


@pytest.mark.parametrize("entry", ["collection", "individual"])
@pytest.mark.parametrize("field", ["name", "description"])
@pytest.mark.parametrize(
    "scalar",
    [
        "0x2A",
        "0xFF",
        "0o52",
        "0o777",
        "+42",
        "-7",
        "3.14",
        ".5",
        "1.",
        "1e6",
        "2.5e-3",
        ".inf",
        "-.Inf",
        "+.INF",
        ".nan",
        ".NaN",
        ".NAN",
        "null",
        "~",
        "true",
        "FALSE",
        "{}",
        "[]",
        "[a, b]",
        "{a: b}",
        "",
    ],
    ids=[
        "hex",
        "hex-upper-digits",
        "octal",
        "octal-max",
        "signed-plus",
        "signed-minus",
        "float",
        "float-leading-dot",
        "float-trailing-dot",
        "exponent",
        "exponent-signed",
        "inf",
        "inf-signed",
        "inf-plus-upper",
        "nan",
        "nan-mixed",
        "nan-upper",
        "null",
        "tilde",
        "bool-true",
        "bool-false-upper",
        "flow-empty-map",
        "flow-empty-list",
        "flow-list",
        "flow-map",
        "empty",
    ],
)
def test_numeric_scalar_refused(
    tmp_path: Path, entry: str, field: str, scalar: str
) -> None:
    """Every YAML 1.2 core-schema non-string scalar is refused for name/description.

    Covers hex, octal, signed decimal, float, exponent, `.inf`/`.nan` forms
    plus null/bool/flow controls, through both `expand_collection` and
    `resolve_individual` for both required fields.
    """

    root = tmp_path / "src"
    name_token = scalar if field == "name" else "review"
    description_token = scalar if field == "description" else "valid description"
    frontmatter = (
        f"---\nname: {name_token}\ndescription: {description_token}\n---\n# skill\n"
    )
    if entry == "collection":
        member = root / "col" / "review"
        member.mkdir(parents=True)
        (member / "SKILL.md").write_text(frontmatter, encoding="utf-8")
        with pytest.raises(source_errors.SourceError) as excinfo:
            expand_collection(root, _collection("col", ("review",)))
    else:
        member = root / "skills" / "review"
        member.mkdir(parents=True)
        (member / "SKILL.md").write_text(frontmatter, encoding="utf-8")
        with pytest.raises(source_errors.SourceError) as excinfo:
            resolve_individual(
                root,
                IndividualSelector(
                    name="review", from_alias="local", directory="skills/review"
                ),
            )
    assert excinfo.value.code == source_errors.CODE_MEMBER_INVALID


# ---------------------------------------------------------------------------
# Rev5 reviewer attacks (committed under the reviewer's names)
# ---------------------------------------------------------------------------


def _drive_review_entry(root: Path, entry: str, directory: str = "skill"):
    if entry == "individual":
        return resolve_individual(
            root,
            IndividualSelector(name="review", from_alias="local", directory=directory),
        )
    return expand_collection(
        root, CollectionSelector(from_alias="local", directory=".", include=(directory,), exclude=())
    )


@pytest.mark.parametrize("entry", ["individual", "collection"])
def test_comment_only_description_refused(tmp_path: Path, entry: str) -> None:
    """Committed reviewer attack (rev4): comment-only description is null."""

    member = tmp_path / "skill"
    member.mkdir()
    (member / "SKILL.md").write_text(
        "---\nname: review\ndescription: # only a comment\n---\n", encoding="utf-8"
    )
    with pytest.raises(source_errors.SourceError) as excinfo:
        _drive_review_entry(tmp_path, entry)
    assert excinfo.value.code == source_errors.CODE_MEMBER_INVALID


@pytest.mark.parametrize("entry", ["individual", "collection"])
def test_quoted_description_with_comment_accepted(tmp_path: Path, entry: str) -> None:
    """Committed reviewer attack (rev4): quoted value plus trailing comment."""

    member = tmp_path / "skill"
    member.mkdir()
    (member / "SKILL.md").write_text(
        '---\nname: review\ndescription: "A valid description" # comment\n---\n',
        encoding="utf-8",
    )
    _drive_review_entry(tmp_path, entry)


@pytest.mark.parametrize("entry", ["individual", "collection"])
def test_explicit_managed_package_refused(tmp_path: Path, entry: str) -> None:
    """Committed reviewer attack (rev4): explicit managed package refuses."""

    member = tmp_path / ".agents"
    member.mkdir()
    (member / "SKILL.md").write_text(
        "---\nname: review\ndescription: Generated output\n---\n", encoding="utf-8"
    )
    with pytest.raises(source_errors.SourceError) as excinfo:
        _drive_review_entry(tmp_path, entry, ".agents")
    assert excinfo.value.code == source_errors.CODE_OUTPUT_OVERLAP


# ---------------------------------------------------------------------------
# F2 rev5: one boundary predicate on every selection path
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("entry", ["individual", "collection"])
@pytest.mark.parametrize(
    "shape",
    ["direct", "nested", "link", "case-variant", "csk-home"],
    ids=["direct-.agents", "nested-workspace", "link-into-managed", "case-variant", "csk-home"],
)
def test_managed_boundary_explicit_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, entry: str, shape: str
) -> None:
    """Every explicit managed selection refuses with source_output_overlap.

    Covers both production entry points (individual selector, literal include)
    for direct, nested-workspace, linked-alias, case-variant and csk-home
    shapes. The predicate runs before any metadata read, so even a valid
    SKILL.md inside managed output refuses.
    """

    if shape == "direct":
        root = tmp_path / "src"
        _write_skill(root / ".agents", "review", "Generated output")
        directory = ".agents"
        collection_base = "."
        literal = ".agents"
    elif shape == "nested":
        root = tmp_path / "src"
        _write_skill(root / "workspace" / ".agents" / "nested" / "generated", "review")
        directory = "workspace/.agents/nested/generated"
        collection_base = "workspace/.agents/nested"
        literal = "generated"
    elif shape == "link":
        root = tmp_path / "src"
        _write_skill(root / ".agents" / "nested" / "target", "review")
        (root / "col").mkdir(parents=True)
        (root / "col" / "alias").symlink_to(
            root / ".agents" / "nested" / "target", target_is_directory=True
        )
        directory = "col/alias"
        collection_base = "col"
        literal = "alias"
    elif shape == "case-variant":
        root = tmp_path / "src"
        _write_skill(root / ".AGENTS", "review", "Generated output")
        if not _probes_same_file(root / ".agents", root / ".AGENTS"):
            pytest.skip(
                "case-insensitive filesystem not available: "
                ".AGENTS/.agents explicit bound"
            )
        directory = ".AGENTS"
        collection_base = "."
        literal = ".AGENTS"
    else:
        home = tmp_path / "home"
        monkeypatch.setenv("CSK_CONFIG", str(home / "config.json"))
        root = home / "src"
        _write_skill(root / "pkg", "review")
        directory = "pkg"
        collection_base = "."
        literal = "pkg"
    with pytest.raises(source_errors.SourceError) as excinfo:
        if entry == "individual":
            resolve_individual(
                root,
                IndividualSelector(name="review", from_alias="local", directory=directory),
            )
        else:
            expand_collection(root, _collection(collection_base, (literal,)))
    assert excinfo.value.code == source_errors.CODE_OUTPUT_OVERLAP


@pytest.mark.parametrize(
    "shape",
    ["direct", "nested", "link", "case-variant", "csk-home"],
    ids=["direct-.agents", "nested-workspace", "link-into-managed", "case-variant", "csk-home"],
)
def test_managed_boundary_wildcard_pruned(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, shape: str
) -> None:
    """The same predicate prunes silently for '*' discovery."""

    if shape == "csk-home":
        home = tmp_path / "home"
        monkeypatch.setenv("CSK_CONFIG", str(home / "config.json"))
        root = tmp_path / "src"
        base = root / "col"
        _write_skill(base / "normal", "review")
        generated = home / "source-v1" / "snapshots" / "generated"
        _write_skill(generated, "generated")
        (base / "alias").symlink_to(generated, target_is_directory=True)
    elif shape == "nested":
        root = tmp_path / "src"
        base = root / "col"
        _write_skill(base / "normal", "review")
        generated = root / "workspace" / ".agents" / "nested" / "generated"
        _write_skill(generated, "generated")
        (base / "alias").symlink_to(generated, target_is_directory=True)
    elif shape == "link":
        root = tmp_path / "src"
        base = root / "col"
        _write_skill(base / "normal", "review")
        target = root / ".agents" / "nested" / "target"
        _write_skill(target, "generated")
        (base / "alias").symlink_to(target, target_is_directory=True)
    elif shape == "case-variant":
        root = tmp_path / "src"
        base = root / "col"
        _write_skill(base / "normal", "review")
        _write_skill(base / ".AGENTS", "generated")
        if not _probes_same_file(base / ".agents", base / ".AGENTS"):
            pytest.skip(
                "case-insensitive filesystem not available: "
                ".AGENTS/.agents wildcard bound"
            )
    else:
        root = tmp_path / "src"
        base = root / "col"
        _write_skill(base / "normal", "review")
        _write_skill(base / ".agents", "generated")
    members = expand_collection(root, _collection("col", ("*",)))
    assert [member.name for member in members] == ["review"]


@pytest.mark.parametrize("entry", ["individual", "collection"])
def test_managed_boundary_authored_inputs_pass(tmp_path: Path, entry: str) -> None:
    """Authored inputs (agents/skills, near-miss, ordinary) are never managed."""

    root = tmp_path / "src"
    _write_skill(root / "agents" / "skills" / "review", "review")
    _write_skill(root / "ordinary", "ordinary")
    _write_skill(root / ".githooks", "hooks-skill")
    _write_skill(root / "col" / "ordinary", "ordinary")
    _write_skill(root / "col" / ".githooks", "hooks-skill")
    _write_skill(root / "col" / "agents", "agents-skill")
    if entry == "individual":
        first = resolve_individual(
            root,
            IndividualSelector(
                name="review", from_alias="local", directory="agents/skills/review"
            ),
        )
        second = resolve_individual(
            root, IndividualSelector(name="ordinary", from_alias="local", directory="ordinary")
        )
        third = resolve_individual(
            root,
            IndividualSelector(
                name="hooks-skill", from_alias="local", directory=".githooks"
            ),
        )
        assert (first.name, second.name, third.name) == (
            "review",
            "ordinary",
            "hooks-skill",
        )
    else:
        members = expand_collection(root, _collection(".", ("ordinary", ".githooks")))
        assert [member.name for member in members] == ["hooks-skill", "ordinary"]
        members = expand_collection(root, _collection("col", ("*",)))
        assert [member.folder for member in members] == [".githooks", "agents", "ordinary"]


def test_managed_boundary_predicate_is_single_source(tmp_path: Path) -> None:
    """The predicate reports every shape; discovery and explicit share it."""

    root = tmp_path / "src"
    target = root / "workspace" / ".agents" / "nested" / "pkg"
    target.mkdir(parents=True)
    _write_skill(target, "review")
    _write_skill(root / "ordinary", "ordinary")
    with pytest.raises(source_errors.SourceError) as managed:
        resolve_individual(
            root,
            IndividualSelector(name="review", from_alias="local", directory="workspace/.agents/nested/pkg"),
        )
    assert managed.value.code == source_errors.CODE_OUTPUT_OVERLAP
    ordinary = resolve_individual(
        root,
        IndividualSelector(name="ordinary", from_alias="local", directory="ordinary"),
    )
    assert ordinary.name == "ordinary"


# ---------------------------------------------------------------------------
# F3 rev5: frontmatter scalar tokenizer tables
# ---------------------------------------------------------------------------


_ACCEPT_CASES: tuple[tuple[str, str, str], ...] = (
    ("double-trailing-space", '"review" # comment', "review"),
    ("single-trailing-tab", "'review'\t# comment", "review"),
    ("double-trailing-two-spaces", '"review"  # comment', "review"),
    ("single-trailing-two-spaces", "'review'  # comment", "review"),
    ("double-trailing-tab-space", '"review"\t # comment', "review"),
    ("single-trailing-space-tab", "'review' \t# comment", "review"),
    ("double-contains-hash", '"a # b"', "a # b"),
    ("single-contains-hash", "'a # b'", "a # b"),
    ("single-escaped-quote", "'it''s ok'", "it's ok"),
    ("double-escaped-quote", '"a\\"b"', 'a"b'),
    ("double-unicode-escape", '"\\u0041review"', "Areview"),
    ("plain-trailing-space", "review # comment", "review"),
    ("plain-trailing-tab", "review\t# comment", "review"),
    ("plain-hash-no-space", "a#b", "a#b"),
    ("plain-question-glued", "?foo", "?foo"),
    ("plain-equals-glued", "=foo", "=foo"),
    ("plain-equals-spaced", "= foo", "= foo"),
)

_DESCRIPTION_ONLY_ACCEPT_CASES: tuple[tuple[str, str, str], ...] = (
    ("double-escaped-backslash", '"a\\\\b"', "a\\b"),
    ("double-newline-escape", '"a\\n b"', "a\n b"),
    ("double-tab-escape", '"a\\tb"', "a\tb"),
)

_REFUSE_CASES: tuple[tuple[str, str], ...] = (
    ("comment-only-space", "# only a comment"),
    ("comment-only-tab", "\t# comment"),
    ("empty", ""),
    ("unterminated-single", "'abc"),
    ("unterminated-double", '"abc'),
    ("trailing-garbage-single", "'review' garbage"),
    ("trailing-garbage-double", '"review" garbage'),
    ("trailing-glued-single", "'review'garbage"),
    ("trailing-glued-double", '"review"garbage'),
    ("trailing-comment-no-sep-single", "'review'# comment"),
    ("trailing-comment-no-sep-double", '"review"# comment'),
    ("trailing-comment-no-sep-single-bare", "'review'#"),
    ("trailing-comment-no-sep-double-bare", '"review"#'),
    ("indicator-brace", "{"),
    ("indicator-bracket", "["),
    ("indicator-amp", "&anchor"),
    ("indicator-star", "*alias"),
    ("indicator-bang", "!tag"),
    ("indicator-pipe", "|"),
    ("indicator-fold", ">"),
    ("indicator-percent", "%directive"),
    ("indicator-at", "@user"),
    ("indicator-tick", "`code"),
    ("indicator-comma", ",foo"),
    ("indicator-comma-lone", ","),
    ("indicator-flow-close-list", "]foo"),
    ("indicator-flow-close-map", "}foo"),
    ("indicator-explicit-key", "? foo"),
    ("indicator-explicit-key-lone", "?"),
    ("indicator-value-tag", "="),
    ("flow-empty-map", "{}"),
    ("flow-empty-list", "[]"),
    ("flow-list", "[a, b]"),
    ("flow-map", "{a: b}"),
    ("null-lower", "null"),
    ("null-capital", "Null"),
    ("null-upper", "NULL"),
    ("null-tilde", "~"),
    ("bool-true", "true"),
    ("bool-True", "True"),
    ("bool-TRUE", "TRUE"),
    ("bool-false", "false"),
    ("bool-False", "False"),
    ("bool-FALSE", "FALSE"),
    ("int-decimal", "42"),
    ("int-signed-plus", "+42"),
    ("int-signed-minus", "-7"),
    ("int-hex", "0x2A"),
    ("int-hex-upper", "0xFF"),
    ("int-octal", "0o52"),
    ("int-octal-max", "0o777"),
    ("float-plain", "3.14"),
    ("float-leading-dot", ".5"),
    ("float-trailing-dot", "1."),
    ("float-exponent", "1e6"),
    ("float-exponent-signed", "2.5e-3"),
    ("float-inf", ".inf"),
    ("float-inf-signed", "-.Inf"),
    ("float-inf-plus", "+.INF"),
    ("float-nan", ".nan"),
    ("float-nan-mixed", ".NaN"),
    ("float-nan-upper", ".NAN"),
    ("long-decimal", "1" * 5000),
    ("bool-tab-comment", "true\t# comment"),
    ("hex-tab-comment", "0x2A\t# comment"),
    ("quoted-empty-double", '""'),
    ("quoted-empty-single", "''"),
    ("bad-escape", '"a\\qb"'),
    ("bad-hex-escape", '"\\xZZ"'),
)


@pytest.mark.parametrize("entry", ["individual", "collection"])
@pytest.mark.parametrize("field", ["name", "description"])
@pytest.mark.parametrize(
    "case_id,raw,expected",
    [pytest.param(case_id, raw, expected, id=case_id) for case_id, raw, expected in _ACCEPT_CASES],
)
def test_frontmatter_scalar_accepted_table(
    tmp_path: Path, entry: str, field: str, case_id: str, raw: str, expected: str
) -> None:
    """Tokenizer accept table: quoted/trailing/comment-in-quote shapes pass."""

    _ = case_id
    root = tmp_path / "src"
    if field == "name":
        name_token = raw
        description_token = "valid description"
        selector_name = expected
    else:
        name_token = "review"
        description_token = raw
        selector_name = "review"
        expected = "review"
    frontmatter = (
        f"---\nname: {name_token}\ndescription: {description_token}\n---\n# skill\n"
    )
    if entry == "individual":
        member = root / "skills" / "review"
        member.mkdir(parents=True)
        (member / "SKILL.md").write_text(frontmatter, encoding="utf-8")
        selected = resolve_individual(
            root,
            IndividualSelector(name=selector_name, from_alias="local", directory="skills/review"),
        )
        assert selected.name == selector_name
    else:
        member = root / "col" / "review"
        member.mkdir(parents=True)
        (member / "SKILL.md").write_text(frontmatter, encoding="utf-8")
        members = expand_collection(root, _collection("col", ("review",)))
        assert members[0].name == expected


@pytest.mark.parametrize("entry", ["individual", "collection"])
@pytest.mark.parametrize(
    "case_id,raw,expected",
    [
        pytest.param(case_id, raw, expected, id=case_id)
        for case_id, raw, expected in _DESCRIPTION_ONLY_ACCEPT_CASES
    ],
)
def test_frontmatter_scalar_description_only_accepted(
    tmp_path: Path, entry: str, case_id: str, raw: str, expected: str
) -> None:
    """Backslash/control escapes decode for descriptions (non-portable names)."""

    _ = (case_id, expected)
    root = tmp_path / "src"
    frontmatter = f"---\nname: review\ndescription: {raw}\n---\n# skill\n"
    if entry == "individual":
        member = root / "skills" / "review"
        member.mkdir(parents=True)
        (member / "SKILL.md").write_text(frontmatter, encoding="utf-8")
        selected = resolve_individual(
            root,
            IndividualSelector(name="review", from_alias="local", directory="skills/review"),
        )
        assert selected.name == "review"
    else:
        member = root / "col" / "review"
        member.mkdir(parents=True)
        (member / "SKILL.md").write_text(frontmatter, encoding="utf-8")
        members = expand_collection(root, _collection("col", ("review",)))
        assert members[0].name == "review"


@pytest.mark.parametrize("entry", ["individual", "collection"])
@pytest.mark.parametrize("field", ["name", "description"])
@pytest.mark.parametrize(
    "case_id,raw",
    [pytest.param(case_id, raw, id=case_id) for case_id, raw in _REFUSE_CASES],
)
def test_frontmatter_scalar_refused_table(
    tmp_path: Path, entry: str, field: str, case_id: str, raw: str
) -> None:
    """Tokenizer refuse table: null/bool/numeric/indicator/quote gaps refuse."""

    _ = case_id
    root = tmp_path / "src"
    name_token = raw if field == "name" else "review"
    description_token = raw if field == "description" else "valid description"
    frontmatter = (
        f"---\nname: {name_token}\ndescription: {description_token}\n---\n# skill\n"
    )
    with pytest.raises(source_errors.SourceError) as excinfo:
        if entry == "individual":
            member = root / "skills" / "review"
            member.mkdir(parents=True)
            (member / "SKILL.md").write_text(frontmatter, encoding="utf-8")
            resolve_individual(
                root,
                IndividualSelector(
                    name="review", from_alias="local", directory="skills/review"
                ),
            )
        else:
            member = root / "col" / "review"
            member.mkdir(parents=True)
            (member / "SKILL.md").write_text(frontmatter, encoding="utf-8")
            expand_collection(root, _collection("col", ("review",)))
    assert excinfo.value.code == source_errors.CODE_MEMBER_INVALID


# ---------------------------------------------------------------------------
# Rev6 reviewer attack (committed under the reviewer's name) + block grammar
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("entry", ["collection", "individual"])
@pytest.mark.parametrize(
    "frontmatter",
    [
        "metadata: wrapper\n  name: review\n  description: Nested fields are not root fields",
        "name: review\ndescription: nested: mapping",
        "name: review\ndescription: - item",
    ],
    ids=["indented-fields", "mapping-value", "sequence-value"],
)
def test_non_scalar_or_nested_frontmatter_refused(
    tmp_path: Path, entry: str, frontmatter: str
) -> None:
    """Committed reviewer attack (rev5): non-scalar/nested syntax never satisfies root fields."""

    root = tmp_path / "source"
    package = root / "pkg"
    package.mkdir(parents=True)
    (package / "SKILL.md").write_text("---\n" + frontmatter + "\n---\n# Body\n")
    with pytest.raises(source_errors.SourceError) as caught:
        if entry == "collection":
            expand_collection(
                root,
                CollectionSelector(
                    from_alias="local", directory=".", include=("*",), exclude=()
                ),
            )
        else:
            resolve_individual(
                root,
                IndividualSelector(
                    name="review", from_alias="local", directory="pkg"
                ),
            )
    assert caught.value.code == "source_member_invalid"


_STRUCTURE_REFUSE_CASES: tuple[tuple[str, str], ...] = (
    ("nested-block-inner-name-root-absent", "metadata:\n  name: inner\n  description: inner desc"),
    ("mapping-in-plain", "name: review\ndescription: nested: mapping"),
    ("sequence-value", "name: review\ndescription: - item"),
    ("child-under-plain-scalar", "name: review\n  note: under scalar\ndescription: d"),
    ("child-under-quoted-scalar", 'name: "review"\n  extra: x\ndescription: d'),
    ("scalar-owning-nested-name", "metadata: wrapper\n  name: review\ndescription: d"),
    ("tab-indentation", "name: review\n\tdescription: d"),
    ("duplicate-keys", "name: review\nname: other\ndescription: d"),
    ("unbalanced-flow-map", "name: review\ndescription: d\nmetadata: {a: b"),
    ("unbalanced-flow-list", "name: review\ndescription: d\nmetadata: [a, b"),
    ("trailing-colon", "name: review\ndescription: foo:"),
    ("tab-mapping-separator", "name: review\ndescription: nested:\tmapping"),
    ("list-under-scalar", "name: review\ndescription: text\n  - item"),
    ("key-without-space", "name:review\ndescription: d"),
    ("triggers-mapping-children", "name: review\ndescription: d\ntriggers:\n  a: b"),
    ("name-with-children", "name:\n  a: b\ndescription: d"),
    ("description-with-sequence", "name: review\ndescription:\n  - a"),
    ("indent-zero-sequence", "name: review\n- item\ndescription: d"),
)

_STRUCTURE_ACCEPT_CASES: tuple[tuple[str, str, str], ...] = (
    (
        "nested-metadata-ignored",
        "metadata:\n  name: inner\n  description: inner\n  count: 42\nname: review\ndescription: d",
        "review",
    ),
    (
        "allowed-tools-skipped",
        "name: review\ndescription: d\nallowed-tools:\n  - Read\n  - Write",
        "review",
    ),
    ("quoted-nested-mapping", 'name: review\ndescription: "nested: mapping"', "review"),
    ("quoted-sequence", 'name: review\ndescription: "- item"', "review"),
    ("flow-map-other-key", "name: review\ndescription: d\nmetadata: {a: b}", "review"),
    ("flow-list-other-key", "name: review\ndescription: d\ntags: [a, b]", "review"),
    ("block-chomping", "name: review\ndescription: |-\n  line one\n  line two", "review"),
    ("block-indent-indicator", "name: review\ndescription: |2\n  line one", "review"),
    ("folded-block", "name: review\ndescription: >\n  line one\n  line two", "review"),
    ("url-value", "name: review\ndescription: see http://example.com/x", "review"),
    (
        "blanks-and-comments",
        "# top comment\nname: review\n\ndescription: d # trailing",
        "review",
    ),
    (
        "triggers-with-comment",
        "name: review\ndescription: d\ntriggers:\n  # pick one\n  - one\n  - two",
        "review",
    ),
    ("key-space-before-colon", "name : review\ndescription: d", "review"),
)


@pytest.mark.parametrize("entry", ["individual", "collection"])
@pytest.mark.parametrize(
    "case_id,frontmatter",
    [pytest.param(case_id, frontmatter, id=case_id) for case_id, frontmatter in _STRUCTURE_REFUSE_CASES],
)
def test_frontmatter_structure_refused_table(
    tmp_path: Path, entry: str, case_id: str, frontmatter: str
) -> None:
    """Block-grammar refuse table: nested/promoted/non-scalar shapes refuse."""

    _ = case_id
    root = tmp_path / "src"
    text = f"---\n{frontmatter}\n---\n# skill\n"
    with pytest.raises(source_errors.SourceError) as excinfo:
        if entry == "individual":
            member = root / "skills" / "review"
            member.mkdir(parents=True)
            (member / "SKILL.md").write_text(text, encoding="utf-8")
            resolve_individual(
                root,
                IndividualSelector(
                    name="review", from_alias="local", directory="skills/review"
                ),
            )
        else:
            member = root / "col" / "review"
            member.mkdir(parents=True)
            (member / "SKILL.md").write_text(text, encoding="utf-8")
            expand_collection(root, _collection("col", ("review",)))
    assert excinfo.value.code == source_errors.CODE_MEMBER_INVALID


@pytest.mark.parametrize("entry", ["individual", "collection"])
@pytest.mark.parametrize(
    "case_id,frontmatter,expected",
    [
        pytest.param(case_id, frontmatter, expected, id=case_id)
        for case_id, frontmatter, expected in _STRUCTURE_ACCEPT_CASES
    ],
)
def test_frontmatter_structure_accepted_table(
    tmp_path: Path, entry: str, case_id: str, frontmatter: str, expected: str
) -> None:
    """Block-grammar accept table: skipped blocks, quoted indicators, headers."""

    _ = case_id
    root = tmp_path / "src"
    text = f"---\n{frontmatter}\n---\n# skill\n"
    if entry == "individual":
        member = root / "skills" / "review"
        member.mkdir(parents=True)
        (member / "SKILL.md").write_text(text, encoding="utf-8")
        selected = resolve_individual(
            root,
            IndividualSelector(
                name=expected, from_alias="local", directory="skills/review"
            ),
        )
        assert selected.name == expected
    else:
        member = root / "col" / "review"
        member.mkdir(parents=True)
        (member / "SKILL.md").write_text(text, encoding="utf-8")
        members = expand_collection(root, _collection("col", ("review",)))
        assert members[0].name == expected


def test_parse_frontmatter_scalar_comment_only_raises_directly() -> None:
    """Direct tokenizer probe: comment-only is null, not an empty string.

    Kills narrowing mutant (a): returning "" for comment-only would still be
    refused for required fields via the empty gate, so only a direct raise
    assertion distinguishes the tokenizer rule from the gate.
    """

    with pytest.raises(source_errors.SourceError) as excinfo:
        selection.parse_frontmatter_scalar("# only a comment", "'probe'")
    assert excinfo.value.code == source_errors.CODE_MEMBER_INVALID
    with pytest.raises(source_errors.SourceError):
        selection.parse_frontmatter_scalar("   ", "'probe'")
    assert selection.parse_frontmatter_scalar('"review" # comment', "'probe'") == "review"
    assert selection.parse_frontmatter_scalar("'a # b'", "'probe'") == "a # b"


# ---------------------------------------------------------------------------
# Rev1 reviewer attacks (committed under the reviewer's names)
# ---------------------------------------------------------------------------


def _review_write(path: Path, name: str = "review", description: str = "valid") -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f"---\nname: {name}\ndescription: {description}\n---\n")


def test_review_external_skill_md_is_rejected(tmp_path: Path) -> None:
    """Committed reviewer attack (rev1): escaping SKILL.md link refuses."""

    root = tmp_path / "src"
    member = root / "member"
    member.mkdir(parents=True)
    _review_write(tmp_path / "outside.md")
    (member / "SKILL.md").symlink_to(tmp_path / "outside.md")
    with pytest.raises(source_errors.SourceError):
        expand_collection(root, CollectionSelector("local", ".", ("*",), ()))


def test_review_case_equivalent_git_is_pruned(tmp_path: Path) -> None:
    """Committed reviewer attack (rev1): case-equivalent .git prunes where aliased."""

    root = tmp_path / "src"
    _review_write(root / "normal" / "SKILL.md")
    _review_write(root / ".GIT" / "SKILL.md", name="generated")
    if not (root / ".git").exists():
        pytest.skip("case-insensitive filesystem unavailable")
    assert [
        member.name
        for member in expand_collection(root, CollectionSelector("local", ".", ("*",), ()))
    ] == ["review"]


@pytest.mark.parametrize("description", ["null", "{}", "true", "42"])
def test_review_non_string_description_is_rejected(
    tmp_path: Path, description: str
) -> None:
    """Committed reviewer attack (rev1): unquoted non-string descriptions refuse."""

    root = tmp_path / "src"
    _review_write(root / "member" / "SKILL.md", description=description)
    with pytest.raises(source_errors.SourceError) as excinfo:
        expand_collection(root, CollectionSelector("local", ".", ("*",), ()))
    assert excinfo.value.code == "source_member_invalid"


# ---------------------------------------------------------------------------
# Rev6 reviewer attack (committed under the reviewer's name)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("entry", ["collection", "individual"])
@pytest.mark.parametrize(
    "suffix",
    [
        "metadata: wrapper\n  ---\n  name: nested\n",
        "description: good\n  ---\nname: duplicate\n",
    ],
)
def test_indented_fence_cannot_bypass_structure(
    tmp_path: Path, entry: str, suffix: str
) -> None:
    """Committed reviewer attack (rev6): an indented fence never ends frontmatter.

    The first shape hides an indented child under the scalar ``metadata:
    wrapper``; the second hides a duplicate root ``name`` after the
    pseudo-fence. Column-0 fence detection hands the indented line to the
    block grammar, where it is a structural error.
    """

    root = tmp_path / "source"
    member = root / "review"
    member.mkdir(parents=True)
    prefix = (
        "name: review\n"
        if suffix.startswith("description:")
        else "name: review\ndescription: good\n"
    )
    (member / "SKILL.md").write_text("---\n" + prefix + suffix + "---\n")
    with pytest.raises(source_errors.SourceError) as excinfo:
        if entry == "collection":
            expand_collection(
                root,
                CollectionSelector(
                    from_alias="local", directory=".", include=("*",), exclude=()
                ),
            )
        else:
            resolve_individual(
                root,
                IndividualSelector(
                    name="review", from_alias="local", directory="review"
                ),
            )
    assert excinfo.value.code == "source_member_invalid"


# ---------------------------------------------------------------------------
# Rev7: fence and document-framing decision tables (both entry points)
# ---------------------------------------------------------------------------


def _drive_bytes_entry(root: Path, entry: str, payload: bytes) -> str:
    member = root / "pkg"
    member.mkdir(parents=True, exist_ok=True)
    (member / "SKILL.md").write_bytes(payload)
    if entry == "individual":
        return resolve_individual(
            root,
            IndividualSelector(name="review", from_alias="local", directory="pkg"),
        ).name
    return expand_collection(
        root,
        CollectionSelector(from_alias="local", directory=".", include=("*",), exclude=()),
    )[0].name


_FRAMING_REFUSE_CASES: tuple[tuple[str, bytes], ...] = (
    ("text-before-opening", b"hello\n---\nname: review\ndescription: d\n---\n"),
    ("blank-before-opening", b"\n---\nname: review\ndescription: d\n---\n"),
    ("indented-opening", b"  ---\nname: review\ndescription: d\n---\n"),
    ("opening-with-text", b"--- x\nname: review\ndescription: d\n---\n"),
    ("opening-ellipsis", b"...\nname: review\ndescription: d\n---\n"),
    ("missing-closing", b"---\nname: review\ndescription: d\n"),
    ("closing-with-text", b"---\nname: review\ndescription: d\n--- x\n---\n"),
    ("ellipsis-with-text", b"---\nname: review\ndescription: d\n... x\n---\n"),
    ("four-dashes", b"---\nname: review\ndescription: d\n----\n---\n"),
    ("indented-fence-under-scalar", b"---\nname: review\ndescription: good\n  ---\n---\n"),
    ("indented-ellipsis-under-scalar", b"---\nname: review\ndescription: good\n  ...\n---\n"),
    (
        "duplicate-after-pseudo-fence",
        b"---\nname: review\ndescription: good\n  ---\nname: duplicate\n---\n",
    ),
    ("empty-frontmatter", b"---\n---\n"),
    ("only-comments", b"---\n# comment\n---\n"),
    ("lone-cr", b"---\nname: review\ndescription: a\rb\n---\n"),
    ("cr-only-separators", b"---\rname: review\r---\r"),
    ("invalid-utf8", b"---\nname: \xff\n---\n"),
    ("fence-like-key-colon", b"---\nname: review\ndescription: d\n---: foo\n---\n"),
    ("ellipsis-like-key-colon", b"---\nname: review\ndescription: d\n...: foo\n---\n"),
    # Rev14 self-attack: every EOF/empty/opening-only boundary.
    ("empty-file", b""),
    ("only-opening-fence", b"---\n"),
    ("only-opening-fence-no-newline", b"---"),
)

_FRAMING_ACCEPT_CASES: tuple[tuple[str, bytes], ...] = (
    ("bom", b"\xef\xbb\xbf---\nname: review\ndescription: d\n---\n"),
    ("crlf", b"---\r\nname: review\r\ndescription: d\r\n---\r\n# body\r\n"),
    ("trailing-spaces-on-fences", b"---   \nname: review\ndescription: d\n---\t \n# body\n"),
    ("ellipsis-closing", b"---\nname: review\ndescription: d\n...\n# body\n"),
    ("ellipsis-trailing-spaces", b"---\nname: review\ndescription: d\n...   \n"),
    (
        "block-scalar-with-indented-fence",
        b"---\nname: review\ndescription: |\n  line one\n  ---\n  line two\n---\n",
    ),
    (
        "block-scalar-with-indented-ellipsis",
        b"---\nname: review\ndescription: |\n  line one\n  ...\n  line two\n---\n",
    ),
    ("body-ignored", b"---\nname: review\ndescription: d\n---\n---\nname: evil\n"),
    ("bom-plus-crlf", b"\xef\xbb\xbf---\r\nname: review\r\ndescription: d\r\n---\r\n"),
    ("body-lone-cr", b"---\nname: review\ndescription: d\n---\n# Body\ntext\rcontent\n"),
    ("body-multiple-cr", b"---\nname: review\ndescription: d\n---\nline\rmore\rend\n"),
    # Rev14 self-attack: fence at EOF with and without a newline, blank lines
    # around fences, CRLF fence variants, frontmatter-only files.
    ("closing-at-eof-no-newline", b"---\nname: review\ndescription: d\n---"),
    ("ellipsis-at-eof-no-newline", b"---\nname: review\ndescription: d\n..."),
    ("frontmatter-only-no-body", b"---\nname: review\ndescription: d\n---\n"),
    ("frontmatter-only-ellipsis-no-body", b"---\nname: review\ndescription: d\n...\n"),
    ("blank-after-opening", b"---\n\nname: review\ndescription: d\n---\n"),
    ("blank-before-closing", b"---\nname: review\ndescription: d\n\n---\n"),
    ("crlf-ellipsis-closing", b"---\r\nname: review\r\ndescription: d\r\n...\r\n"),
    ("crlf-content-lf-less-fence", b"---\r\nname: review\r\ndescription: d\r\n---"),
)


@pytest.mark.parametrize("entry", ["individual", "collection"])
@pytest.mark.parametrize(
    "case_id,payload",
    [pytest.param(case_id, payload, id=case_id) for case_id, payload in _FRAMING_REFUSE_CASES],
)
def test_frontmatter_framing_refused_table(
    tmp_path: Path, entry: str, case_id: str, payload: bytes
) -> None:
    """Framing refuse table: one negative per fence/decoder decision point."""

    _ = case_id
    with pytest.raises(source_errors.SourceError) as excinfo:
        _drive_bytes_entry(tmp_path, entry, payload)
    assert excinfo.value.code == source_errors.CODE_MEMBER_INVALID


@pytest.mark.parametrize("entry", ["individual", "collection"])
@pytest.mark.parametrize(
    "case_id,payload",
    [pytest.param(case_id, payload, id=case_id) for case_id, payload in _FRAMING_ACCEPT_CASES],
)
def test_frontmatter_framing_accepted_table(
    tmp_path: Path, entry: str, case_id: str, payload: bytes
) -> None:
    """Framing accept table: BOM, CRLF, fence trailers, ... close, body ignored."""

    _ = case_id
    assert _drive_bytes_entry(tmp_path, entry, payload) == "review"


# ---------------------------------------------------------------------------
# Rev8: reviewer block-scalar attacks (committed under the reviewer's names)
# ---------------------------------------------------------------------------


def _drive_block_value(tmp_path: Path, entry: str, value: str) -> list[SelectedSkill]:
    root = tmp_path / "source"
    package = root / "review"
    package.mkdir(parents=True)
    (package / "SKILL.md").write_text(
        "---\nname: review\ndescription: " + value + "\n---\n", encoding="utf-8"
    )
    if entry == "collection":
        return expand_collection(
            root,
            CollectionSelector(
                from_alias="local", directory=".", include=("*",), exclude=()
            ),
        )
    selected = resolve_individual(
        root,
        IndividualSelector(name="review", from_alias="local", directory="review"),
    )
    return [selected]


@pytest.mark.parametrize("entry", ["collection", "individual"])
@pytest.mark.parametrize(
    "value",
    ["|4\n  underindented", "|\n    first\n  dedented", ">4\n  underindented"],
)
def test_block_scalar_indentation_refused(
    tmp_path: Path, entry: str, value: str
) -> None:
    """Committed reviewer attack (rev7): content below the baseline refuses.

    ``|4`` with two-space content, a four-space baseline followed by a
    two-space line, and the folded explicit-indent form all refuse with
    ``source_member_invalid`` through both production entry points.
    """

    with pytest.raises(source_errors.SourceError) as excinfo:
        _drive_block_value(tmp_path, entry, value)
    assert excinfo.value.code == source_errors.CODE_MEMBER_INVALID


@pytest.mark.parametrize("entry", ["collection", "individual"])
def test_block_scalar_header_comment_accepted(tmp_path: Path, entry: str) -> None:
    """Committed reviewer attack (rev7): a legal header comment is accepted."""

    assert _drive_block_value(tmp_path, entry, "| # description\n  legitimate description")


# ---------------------------------------------------------------------------
# Rev8: YAML 1.2 section 8.1 block-scalar tables (both entry points)
# ---------------------------------------------------------------------------
#
# Each case adapts one YAML 1.2 specification section 8.1 example (8.1-8.13,
# the complete block-scalar example series: 8.14+ belong to section 8.2 block
# collections, outside this branch) to the root-mapping frontmatter case
# (parent indent 0) with its specified value or refusal. Expected values were
# cross-checked against the specification text and a PyYAML oracle; the two
# documented adaptations are the `>1` sequence-indent arithmetic (in a root
# mapping the baseline is exactly 1) and the 8.10 blank separator (the spec
# source line carries two spaces, covered as its own case).
#
# Entry points strip installed names, so byte-exact chomping is asserted by
# the parser probes below over the same production function the entry points
# call; the entry tables assert acceptance/refusal plus installed-name
# equality where the value is a portable single line.


def _drive_block_text(tmp_path: Path, entry: str, text: str, name: str) -> str:
    root = tmp_path / "source"
    package = root / "pkg"
    package.mkdir(parents=True, exist_ok=True)
    (package / "SKILL.md").write_text(text, encoding="utf-8")
    if entry == "individual":
        return resolve_individual(
            root,
            IndividualSelector(name=name, from_alias="local", directory="pkg"),
        ).name
    return expand_collection(
        root,
        CollectionSelector(from_alias="local", directory=".", include=("*",), exclude=()),
    )[0].name


# (case_id, complete SKILL.md text, expected stored description value).
_BLOCK_SPEC_ACCEPT: tuple[tuple[str, str, str], ...] = (
    ("s81-literal-header", "---\nname: review\ndescription: | # Empty header\n literal\n---\n", "literal\n"),
    ("s81-fold-indent1", "---\nname: review\ndescription: >1 # Indentation indicator\n folded\n---\n", "folded\n"),
    ("s81-literal-keep", "---\nname: review\ndescription: |+ # Chomping indicator\n keep\n\n---\n", "keep\n\n"),
    ("s81-fold-both", "---\nname: review\ndescription: >1- # Both indicators\n strip\n---\n", "strip"),
    ("s82-auto", "---\nname: review\ndescription: |\n detected\n---\n", "detected\n"),
    ("s82-leading-blanks", "---\nname: review\ndescription: >\n \n  \n  # detected\n---\n", "\n\n# detected\n"),
    ("s82-explicit1", "---\nname: review\ndescription: |1\n  explicit\n---\n", " explicit\n"),
    ("s82-tab-content", "---\nname: review\ndescription: >\n \t\n detected\n---\n", "\t\ndetected\n"),
    ("s84-strip", "---\nname: review\ndescription: |-\n  text\n---\n", "text"),
    ("s84-clip", "---\nname: review\ndescription: |\n  text\n---\n", "text\n"),
    ("s84-keep", "---\nname: review\ndescription: |+\n  text\n---\n", "text\n"),
    ("s85-strip", "---\nname: review\ndescription: |-\n  # text\n\n---\n", "# text"),
    ("s85-clip", "---\nname: review\ndescription: |\n  # text\n\n---\n", "# text\n"),
    ("s85-keep", "---\nname: review\ndescription: |+\n  # text\n\n---\n", "# text\n\n"),
    ("s85-trail-comment", "---\nname: review\ndescription: |\n  # text\n\n# Trail\n---\n", "# text\n"),
    ("s87-literal-tab", "---\nname: review\ndescription: |\n literal\n \ttext\n---\n", "literal\n\ttext\n"),
    (
        "s88-literal-content",
        "---\nname: review\ndescription: |\n \n  \n  literal\n   \n  \n  text\n\n# Comment\n---\n",
        "\n\nliteral\n \n\ntext\n",
    ),
    ("s89-folded", "---\nname: review\ndescription: >\n folded\n text\n---\n", "folded text\n"),
    (
        "s810-folded-empty-separators",
        "---\nname: review\ndescription: >\n\n folded\n line\n\n next\n line\n   * bullet\n\n   * list\n   * lines\n\n last\n line\n\n# Comment\n---\n",
        "\nfolded line\nnext line\n  * bullet\n\n  * list\n  * lines\n\nlast line\n",
    ),
    (
        "s810-spec-spaced-separator",
        "---\nname: review\ndescription: >\n\n folded\n line\n\n next\n line\n   * bullet\n  \n   * list\n   * lines\n\n last\n line\n\n# Comment\n---\n",
        "\nfolded line\nnext line\n  * bullet\n \n  * list\n  * lines\n\nlast line\n",
    ),
)

# (case_id, complete SKILL.md text): spec example 8.3, refused through both entries.
_BLOCK_SPEC_REFUSE: tuple[tuple[str, str], ...] = (
    ("s83-leading-too-indented", "---\nname: review\ndescription: |\n  \n text\n---\n"),
    ("s83-following-less-indented", "---\nname: review\ndescription: >\n  text\n text\n---\n"),
    ("s83-under-explicit", "---\nname: review\ndescription: |2\n text\n---\n"),
)

# (case_id, complete SKILL.md text, expected stored value): spec example 8.6.
# All three refuse through the entry points (empty/whitespace-only for the
# required field); the stored values differ and are asserted by parser probe.
_BLOCK_EMPTY_CHOMP: tuple[tuple[str, str, str], ...] = (
    ("s86-strip", "---\nname: review\ndescription: >-\n\n---\n", ""),
    ("s86-clip", "---\nname: review\ndescription: >\n\n---\n", ""),
    ("s86-keep", "---\nname: review\ndescription: |+\n\n---\n", "\n"),
)

# (case_id, complete SKILL.md text, expected stored name value): block scalars
# on the `name` field whose stripped value is asserted through both entries.
_BLOCK_NAME_VALUES: tuple[tuple[str, str, str], ...] = (
    ("n81-literal", "---\nname: | # header\n literal\ndescription: d\n---\n", "literal\n"),
    ("n81-fold1", "---\nname: >1\n folded\ndescription: d\n---\n", "folded\n"),
    ("n81-keep", "---\nname: |+\n keep\n\ndescription: d\n---\n", "keep\n\n"),
    ("n81-strip", "---\nname: >1-\n strip\ndescription: d\n---\n", "strip"),
    ("n82-auto", "---\nname: |\n detected\ndescription: d\n---\n", "detected\n"),
    ("n82-explicit1", "---\nname: |1\n  explicit\ndescription: d\n---\n", " explicit\n"),
    ("n84-strip", "---\nname: |-\n  text\ndescription: d\n---\n", "text"),
    ("n84-clip", "---\nname: |\n  text\ndescription: d\n---\n", "text\n"),
    ("n85-keep", "---\nname: |+\n  # text\n\ndescription: d\n---\n", "# text\n\n"),
    ("n89-folded", "---\nname: >\n folded\n text\ndescription: d\n---\n", "folded text\n"),
    ("n-header-comment", "---\nname: | # name\n review\ndescription: d\n---\n", "review\n"),
)

# (case_id, header text after `description:`): invalid headers refuse.
_BLOCK_HEADER_REFUSE: tuple[tuple[str, str], ...] = (
    ("header-garbage", "| x"),
    ("header-double-style", "||"),
    ("header-indent-zero", "|0"),
    ("header-indent-multidigit", "|12"),
    ("header-double-chomp", "|+-"),
    ("header-double-chomp-rev", "|-+"),
    ("header-double-indent", "|22"),
    ("header-nospace-comment", "|#c"),
    ("header-nospace-comment-indicators", "|-2#c"),
    ("header-nospace-comment-folded", ">+#c"),
    ("header-indicator-after-space", "| -"),
    ("header-fold-garbage", ">2x"),
    ("header-chomp-space-chomp", "|- +"),
)

# (case_id, header text after `description:`): valid headers accept.
_BLOCK_HEADER_ACCEPT: tuple[tuple[str, str], ...] = (
    ("header-strip-indent-comment", ">-2 # comment"),
    ("header-indent-keep-comment", "|+2 # comment"),
    ("header-trailing-spaces", "|   "),
    ("header-tab-comment", "|\t# comment"),
    ("header-two-spaces-comment", "|  # comment"),
    ("header-tab-space-comment", ">-\t # comment"),
)


@pytest.mark.parametrize("entry", ["individual", "collection"])
@pytest.mark.parametrize(
    "case_id,text,_expected",
    [pytest.param(case_id, text, expected, id=case_id) for case_id, text, expected in _BLOCK_SPEC_ACCEPT],
)
def test_block_scalar_spec_accepted(
    tmp_path: Path, entry: str, case_id: str, text: str, _expected: str
) -> None:
    """Spec 8.1 accept table: every valid example shape accepted, both entries."""

    _ = (case_id, _expected)
    assert _drive_block_text(tmp_path, entry, text, "review") == "review"


@pytest.mark.parametrize("entry", ["individual", "collection"])
@pytest.mark.parametrize(
    "case_id,text",
    [pytest.param(case_id, text, id=case_id) for case_id, text in _BLOCK_SPEC_REFUSE],
)
def test_block_scalar_spec_refused(
    tmp_path: Path, entry: str, case_id: str, text: str
) -> None:
    """Spec 8.3 refuse table: invalid indentation refused, both entries."""

    _ = case_id
    with pytest.raises(source_errors.SourceError) as excinfo:
        _drive_block_text(tmp_path, entry, text, "review")
    assert excinfo.value.code == source_errors.CODE_MEMBER_INVALID


@pytest.mark.parametrize("entry", ["individual", "collection"])
@pytest.mark.parametrize(
    "case_id,text,_value",
    [pytest.param(case_id, text, value, id=case_id) for case_id, text, value in _BLOCK_EMPTY_CHOMP],
)
def test_block_scalar_empty_only_refused(
    tmp_path: Path, entry: str, case_id: str, text: str, _value: str
) -> None:
    """Spec 8.6: empty-only scalars refuse for the required field, both entries."""

    _ = (case_id, _value)
    with pytest.raises(source_errors.SourceError) as excinfo:
        _drive_block_text(tmp_path, entry, text, "review")
    assert excinfo.value.code == source_errors.CODE_MEMBER_INVALID


@pytest.mark.parametrize("entry", ["individual", "collection"])
@pytest.mark.parametrize(
    "case_id,text,stored",
    [pytest.param(case_id, text, stored, id=case_id) for case_id, text, stored in _BLOCK_NAME_VALUES],
)
def test_block_scalar_name_values(
    tmp_path: Path, entry: str, case_id: str, text: str, stored: str
) -> None:
    """Name-field values: folding/chomping observable as the installed name."""

    _ = case_id
    assert _drive_block_text(tmp_path, entry, text, stored.strip()) == stored.strip()


@pytest.mark.parametrize("entry", ["individual", "collection"])
@pytest.mark.parametrize(
    "case_id,header",
    [pytest.param(case_id, header, id=case_id) for case_id, header in _BLOCK_HEADER_REFUSE],
)
def test_block_scalar_header_refused(
    tmp_path: Path, entry: str, case_id: str, header: str
) -> None:
    """Header refuse table: malformed headers refuse before content is read."""

    _ = case_id
    text = f"---\nname: review\ndescription: {header}\n  text\n---\n"
    with pytest.raises(source_errors.SourceError) as excinfo:
        _drive_block_text(tmp_path, entry, text, "review")
    assert excinfo.value.code == source_errors.CODE_MEMBER_INVALID


@pytest.mark.parametrize("entry", ["individual", "collection"])
@pytest.mark.parametrize(
    "case_id,header",
    [pytest.param(case_id, header, id=case_id) for case_id, header in _BLOCK_HEADER_ACCEPT],
)
def test_block_scalar_header_accepted(
    tmp_path: Path, entry: str, case_id: str, header: str
) -> None:
    """Header accept table: indicators in either order, comments, trailers."""

    _ = case_id
    text = f"---\nname: review\ndescription: {header}\n  text\n---\n"
    assert _drive_block_text(tmp_path, entry, text, "review") == "review"


def test_block_scalar_exact_values() -> None:
    """Parser probes: byte-exact stored values for every spec accept shape.

    The entry points strip installed names, so trailing-break distinctions
    (clip vs strip vs keep) are asserted here over the same production
    function (`_parse_frontmatter`) the entry points call.
    """

    for case_id, text, expected in _BLOCK_SPEC_ACCEPT:
        fields = selection._parse_frontmatter(text, f"'{case_id}'")
        assert fields["description"] == expected, case_id
    for case_id, text, stored in _BLOCK_NAME_VALUES:
        fields = selection._parse_frontmatter(text, f"'{case_id}'")
        assert fields["name"] == stored, case_id
    for case_id, text, value in _BLOCK_EMPTY_CHOMP:
        fields = selection._parse_frontmatter(text, f"'{case_id}'")
        assert fields["description"] == value, case_id
    for case_id, text in _BLOCK_SPEC_REFUSE:
        with pytest.raises(source_errors.SourceError) as excinfo:
            selection._parse_frontmatter(text, f"'{case_id}'")
        assert excinfo.value.code == source_errors.CODE_MEMBER_INVALID, case_id


@pytest.mark.parametrize("entry", ["individual", "collection"])
@pytest.mark.parametrize(
    "text",
    [
        pytest.param("---\nname: review\ndescription: |\n\ttext\n---\n", id="leading-tab"),
        pytest.param("---\nname: review\ndescription: |\n  ok\n\tbad\n---\n", id="tab-after-content"),
    ],
)
def test_block_scalar_tab_indentation_refused(tmp_path: Path, entry: str, text: str) -> None:
    """Tab-indented content lines refuse; tabs after spaces stay content (s82/s87)."""

    with pytest.raises(source_errors.SourceError) as excinfo:
        _drive_block_text(tmp_path, entry, text, "review")
    assert excinfo.value.code == source_errors.CODE_MEMBER_INVALID


@pytest.mark.parametrize("entry", ["individual", "collection"])
def test_block_scalar_closing_fence_immediately_after(tmp_path: Path, entry: str) -> None:
    """The closing fence may follow block content with no trailing blank."""

    assert _drive_block_text(tmp_path, entry, "---\nname: review\ndescription: |\n  text\n---\n", "review") == "review"


@pytest.mark.parametrize("entry", ["collection", "individual"])
@pytest.mark.parametrize(
    "header,expected",
    [
        ("|", "text\n  \n"),
        ("|-", "text\n  "),
        (">", "text\n  \n"),
        (">-", "text\n  "),
    ],
)
def test_block_scalar_trailing_space_content_preserved(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, entry: str, header: str, expected: str
) -> None:
    """Committed reviewer attack (rev8): trailing space content is preserved.

    A baseline of two spaces followed by ``text`` and a final four-space line
    carries scalar content ``text`` plus a two-space more-indented line. Empty
    scalar content is decided AFTER baseline removal, so the trailing spaces
    participate in rendering instead of being chomped away as trailing empty
    lines. The entries strip installed names, so the exact stored value is
    observed at the same ``_parse_frontmatter`` the entry points call.
    """

    root = tmp_path / "source"
    package = root / "review"
    package.mkdir(parents=True)
    (package / "SKILL.md").write_text(
        "---\nname: review\ndescription: " + header + "\n  text\n    \n---\n",
        encoding="utf-8",
    )
    seen: list[str] = []
    original = selection._parse_frontmatter

    def observe(*args: object, **kwargs: object) -> dict[str, object]:
        fields = original(*args, **kwargs)  # type: ignore[arg-type]
        seen.append(fields["description"])  # type: ignore[arg-type]
        return fields

    monkeypatch.setattr(selection, "_parse_frontmatter", observe)
    if entry == "collection":
        expand_collection(
            root,
            CollectionSelector(from_alias="local", directory=".", include=("*",), exclude=()),
        )
    else:
        resolve_individual(
            root,
            IndividualSelector(name="review", from_alias="local", directory="review"),
        )
    assert seen == [expected]


@pytest.mark.parametrize("entry", ["collection", "individual"])
@pytest.mark.parametrize(
    "scalar", ["hello\x00world", "'hello\x01world'", '"hello\x7fworld"', "|\n  hello\x0bworld"]
)
def test_nonprintable_yaml_source_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, entry: str, scalar: str
) -> None:
    """Committed reviewer attack (rev9): raw non-printable source refused.

    Each scalar carries a raw character outside YAML 1.2 ``c-printable``
    (NUL plain, SOH single-quoted, DEL double-quoted, VT in a literal
    block). The independent oracle rejects every document, and both
    production entry points must refuse ``source_member_invalid``.
    """

    monkeypatch.setenv("CSK_CONFIG", str(tmp_path / "home" / "config.json"))
    body = f"name: review\ndescription: {scalar}\n"
    with pytest.raises(yaml.YAMLError):
        yaml.safe_load(body)
    root = tmp_path / "source"
    package = root / "pkg"
    package.mkdir(parents=True)
    (package / "SKILL.md").write_text("---\n" + body + "---\n", encoding="utf-8")
    with pytest.raises(source_errors.SourceError) as exc:
        if entry == "collection":
            expand_collection(
                root,
                CollectionSelector(from_alias="local", directory=".", include=("*",), exclude=()),
            )
        else:
            resolve_individual(
                root, IndividualSelector(name="review", from_alias="local", directory="pkg")
            )
    assert exc.value.code == "source_member_invalid"


@pytest.mark.parametrize("entry", ["collection", "individual"])
@pytest.mark.parametrize(
    "tail",
    [
        "body with NUL: \x00\n",
        "body with DEL: \x7f and VT: \x0b\n",
        "body with mid-text BOM: \ufeff\n",
        "# \x01\x02\x03\n",
    ],
)
def test_frontmatter_gate_ignores_body_controls(
    tmp_path: Path, entry: str, tail: str
) -> None:
    """Only the framed frontmatter region is gated; the body is never read.

    Control characters, DEL, and a mid-text BOM after the closing fence are
    ordinary body bytes and must not refuse an otherwise valid package.
    """

    root = tmp_path / "source"
    package = root / "pkg"
    package.mkdir(parents=True)
    (package / "SKILL.md").write_text(
        "---\nname: review\ndescription: hello\n---\n" + tail, encoding="utf-8"
    )
    if entry == "collection":
        members = expand_collection(
            root,
            CollectionSelector(from_alias="local", directory=".", include=("*",), exclude=()),
        )
        assert [member.name for member in members] == ["review"]
    else:
        selected = resolve_individual(
            root, IndividualSelector(name="review", from_alias="local", directory="pkg")
        )
        assert selected.name == "review"


@pytest.mark.parametrize("point", ["\ud800", "\udfff", "\udc00"])
def test_parse_frontmatter_rejects_lone_surrogates(point: str) -> None:
    """Lone surrogates are outside ``c-printable`` and refuse at the gate.

    Surrogates cannot round-trip through a UTF-8 file, so they are probed
    directly at the same ``_parse_frontmatter`` the entry points call.
    """

    with pytest.raises(source_errors.SourceError) as exc:
        selection._parse_frontmatter(
            f"---\nname: review\ndescription: hello{point}world\n---\n", "'surrogate'"
        )
    assert exc.value.code == source_errors.CODE_MEMBER_INVALID


@pytest.mark.parametrize("char", ["\u00a0", "\u2003", "\u202f", "\u3000"])
@pytest.mark.parametrize("entry", ["collection", "individual"])
def test_unicode_non_yaml_blank_line_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, char: str, entry: str
) -> None:
    """Committed reviewer attack (rev10): a Unicode-whitespace line is content.

    A standalone line of NBSP/EM SPACE/narrow NBSP/ideographic space is not a
    YAML blank line (white space is SPACE/TAB only), so the document is
    invalid and both production entry points must refuse
    ``source_member_invalid``.
    """

    monkeypatch.setenv("CSK_CONFIG", str(tmp_path / "home" / "config.json"))
    body = f"name: review\n{char}\ndescription: valid\n"
    with pytest.raises(yaml.YAMLError):
        yaml.safe_load(body)
    root = tmp_path / "source"
    package = root / "pkg"
    package.mkdir(parents=True)
    (package / "SKILL.md").write_text("---\n" + body + "---\n# Review\n", encoding="utf-8")
    with pytest.raises(source_errors.SourceError) as exc:
        if entry == "collection":
            expand_collection(
                root,
                CollectionSelector(from_alias="local", directory=".", include=("*",), exclude=()),
            )
        else:
            resolve_individual(
                root, IndividualSelector(name="review", from_alias="local", directory="pkg")
            )
    assert exc.value.code == "source_member_invalid"


@pytest.mark.parametrize("char", ["\u2028", "\u2029"])
def test_block_unicode_separator_value_preserved(char: str) -> None:
    """Committed reviewer attack (rev10, corrected to YAML 1.2 in rev12).

    A separator ending a block content line is ordinary content (YAML 1.2
    section 5.4: NEL/LS/PS are never line breaks), so clip chomping keeps
    the separator AND one trailing LF. The rev10 version pinned the PyYAML
    1.1 oracle value (which treats LS/PS as breaks); that expectation was
    wrong for YAML 1.2 and is corrected here to the 1.2 value asserted
    directly (the 1.1/1.2 divergence is an SB1 row in the differential
    corpus, never an oracle-exact pin).
    """

    body = f"name: review\ndescription: |\n  hello{char}\n"
    actual = selection._parse_frontmatter("---\n" + body + "---\n", "'review'")
    assert actual["description"] == f"hello{char}\n"


_UNICODE_STRUCTURE_REFUSED: tuple[tuple[str, str], ...] = (
    ("nbsp-standalone", "name: review\n\xa0\ndescription: valid\n"),
    ("emspace-standalone", "name: review\n\u2003\ndescription: valid\n"),
    ("nbsp-leading-indent", "\xa0name: review\ndescription: valid\n"),
    ("nbsp-between-key-colon", "name\xa0: review\ndescription: valid\n"),
    ("nbsp-after-colon", "name:\xa0review\ndescription: valid\n"),
    ("nbsp-trailer", 'name: review\ndescription: "review"\xa0\n'),
    ("nbsp-trailer-comment", 'name: review\ndescription: "review"\xa0# c\n'),
    ("nbsp-header", "name: review\ndescription: |\xa0# c\n  text\n"),
    ("nbsp-header-bare", "name: review\ndescription: |\xa0\n  text\n"),
    ("ls-standalone", "name: review\n\u2028\ndescription: valid\n"),
    ("ls-leading-indent", "\u2028name: review\ndescription: valid\n"),
    ("ls-trailer", 'name: review\ndescription: "review"\u2028\n'),
    ("ls-header-bare", "name: review\ndescription: |\u2028\n  text\n"),
    ("nel-standalone", "name: review\n\x85\ndescription: valid\n"),
    ("nel-trailer", 'name: review\ndescription: "review"\x85\n'),
    ("keycolon-ls", "name\u2028: review\ndescription: valid\n"),
    ("aftercolon-ls", "name:\u2028review\ndescription: valid\n"),
)


@pytest.mark.parametrize("entry", ["collection", "individual"])
@pytest.mark.parametrize(
    "case_id,body",
    [pytest.param(case_id, body, id=case_id) for case_id, body in _UNICODE_STRUCTURE_REFUSED],
)
def test_frontmatter_unicode_structure_refused(
    tmp_path: Path, entry: str, case_id: str, body: str
) -> None:
    """Grammar whitespace at structural positions refuses through both entries.

    Zs characters and separators are content, never indentation, separation,
    or blank lines: standalone lines, indentation, key/colon glue,
    after-colon glue, quote trailers, and block headers carrying them refuse
    ``source_member_invalid`` on both production paths.
    """

    _ = case_id
    root = tmp_path / "source"
    package = root / "pkg"
    package.mkdir(parents=True)
    (package / "SKILL.md").write_text("---\n" + body + "---\n", encoding="utf-8")
    with pytest.raises(source_errors.SourceError) as exc:
        if entry == "collection":
            expand_collection(
                root,
                CollectionSelector(from_alias="local", directory=".", include=("*",), exclude=()),
            )
        else:
            resolve_individual(
                root, IndividualSelector(name="review", from_alias="local", directory="pkg")
            )
    assert exc.value.code == "source_member_invalid"


_UNICODE_CONTENT_ACCEPTED: tuple[tuple[str, str, str], ...] = (
    ("nbsp-plain", "name: review\ndescription: hello\xa0world\n", "review"),
    ("nbsp-quoted", "name: review\ndescription: 'hello\xa0world'\n", "review"),
    ("nbsp-block", "name: review\ndescription: |\n  hello\xa0world\n", "review"),
    ("nbsp-name", "name: rev\xa0iew\ndescription: valid description\n", "rev\xa0iew"),
    ("nbsp-before-hash", "name: review\ndescription: hello\xa0# c\n", "review"),
    ("nbsp-only-desc", "name: review\ndescription: \"\xa0\"\n", "review"),
    ("ls-plain", "name: review\ndescription: hello\u2028world\n", "review"),
    ("ls-quoted", "name: review\ndescription: 'hello\u2028world'\n", "review"),
    ("ls-block-mid", "name: review\ndescription: |\n  hello\u2028world\n", "review"),
    ("ls-block-trail", "name: review\ndescription: |\n  hello\u2028\n", "review"),
    ("ls-name", "name: rev\u2028iew\ndescription: valid description\n", "rev\u2028iew"),
    ("space-blank-line", "name: review\n   \ndescription: valid\n", "review"),
)


@pytest.mark.parametrize("entry", ["collection", "individual"])
@pytest.mark.parametrize(
    "case_id,body,expected",
    [pytest.param(case_id, body, expected, id=case_id) for case_id, body, expected in _UNICODE_CONTENT_ACCEPTED],
)
def test_frontmatter_unicode_content_accepted(
    tmp_path: Path, entry: str, case_id: str, body: str, expected: str
) -> None:
    """Unicode whitespace as scalar content accepts with the exact name.

    Inside plain, quoted, and block scalars (and as a quoted-name value),
    NBSP and LS are ordinary content on both production paths; ASCII blank
    lines stay skippable. Each accepted package installs under the expected
    name.
    """

    _ = case_id
    root = tmp_path / "source"
    package = root / "pkg"
    package.mkdir(parents=True)
    (package / "SKILL.md").write_text("---\n" + body + "---\n", encoding="utf-8")
    if entry == "collection":
        members = expand_collection(
            root,
            CollectionSelector(from_alias="local", directory=".", include=("*",), exclude=()),
        )
        assert [member.name for member in members] == [expected]
    else:
        selected = resolve_individual(
            root, IndividualSelector(name=expected, from_alias="local", directory="pkg")
        )
        assert selected.name == expected


@pytest.mark.parametrize(
    "body,expected",
    [
        pytest.param(
            "name: review\ndescription: d\ntriggers:\n  - a\u2028b\n",
            ["a\u2028b"],
            id="block-item-ls",
        ),
        pytest.param(
            "name: review\ndescription: d\ntriggers:\n  - a\x85b\n",
            ["a\x85b"],
            id="block-item-nel",
        ),
        pytest.param(
            "name: review\ndescription: d\ntriggers: [a\x85b]\n",
            ["a\x85b"],
            id="flow-item-nel",
        ),
        pytest.param(
            "name: review\ndescription: d\ntriggers:\n  - a\xa0b\n",
            ["a\xa0b"],
            id="block-item-nbsp",
        ),
    ],
)
def test_parse_frontmatter_separator_trigger_values(body: str, expected: list[str]) -> None:
    """Trigger items keep separators/NBSP as content (SB1 trigger pins).

    The differential SB1 trigger rows pin the description at parse level;
    these direct probes pin the trigger ITEM values at the same
    ``_parse_frontmatter`` the entry points call: separators are content, so
    the items keep their exact spelling while the 1.1 oracle splits (block
    items) or folds NEL to a space (flow items).
    """

    fields = selection._parse_frontmatter("---\n" + body + "---\n", "'triggers'")
    assert fields["triggers"] == expected


@pytest.mark.parametrize("separator", ["\x85", "\u2028", "\u2029"])
@pytest.mark.parametrize("entry", ["collection", "individual"])
def test_strip_chomping_preserves_unicode_content(
    tmp_path: Path, separator: str, entry: str
) -> None:
    """Committed reviewer attack (rev11): strip keeps separator content.

    Verbatim from the rev11 review (imports adapted to this module):
    a package with ``name: |-`` + ``review<separator>`` has YAML 1.2 name
    ``review<separator>`` (strip removes LF only), so collection expansion
    returns that name and an individual selector for ``review`` fails the
    identity check with ``source_selection_invalid``.
    """

    root = tmp_path / "source"
    member = root / "pkg"
    member.mkdir(parents=True)
    text = f"---\nname: |-\n  review{separator}\ndescription: valid\n---\n"
    (member / "SKILL.md").write_text(text, encoding="utf-8")
    # YAML 1.2 non-ASCII separators are content; strip removes LF only.
    if entry == "collection":
        result = expand_collection(
            root,
            CollectionSelector(from_alias="local", directory=".", include=("*",), exclude=()),
        )
        assert result[0].name == "review" + separator
    else:
        with pytest.raises(source_errors.SourceError) as error:
            resolve_individual(
                root, IndividualSelector(name="review", from_alias="local", directory="pkg")
            )
        assert error.value.code == "source_selection_invalid"


@pytest.mark.parametrize("separator", ["\x85", "\u2028", "\u2029"])
@pytest.mark.parametrize("style", ["|", "|-", "|+"])
def test_block_separator_is_content_in_all_positions(separator: str, style: str) -> None:
    """Committed reviewer attack (rev11): trailing separators are content.

    Verbatim from the rev11 review (imports adapted to this module): under
    every chomping mode the trailing separator survives -- clip/keep append
    one trailing LF after it, strip keeps the separator only.
    """

    text = f"---\nname: review\ndescription: {style}\n  hello{separator}\n---\n"
    expected = "hello" + separator + ("" if style == "|-" else "\n")
    assert selection._parse_frontmatter(text, "attack")["description"] == expected


@pytest.mark.parametrize("entry", ["collection", "individual"])
@pytest.mark.parametrize(
    "spelling, accepted",
    [
        pytest.param("rev\x85iew", True, id="raw-nel"),
        pytest.param('"rev\\x85iew"', True, id="decoded-nel"),
        pytest.param('"rev\\x80iew"', False, id="decoded-u0080"),
        pytest.param('"rev\\x9fiew"', False, id="decoded-u009f"),
        pytest.param('"rev\\x00iew"', False, id="decoded-nul"),
        pytest.param('"rev\\x01iew"', False, id="decoded-soh"),
        pytest.param('"rev\\x7fiew"', False, id="decoded-del"),
    ],
)
def test_installable_name_admits_only_nel(
    tmp_path: Path, entry: str, spelling: str, accepted: bool
) -> None:
    """The installable-name gate admits exactly NEL among controls.

    NEL is YAML 1.2 ``c-printable`` content and filesystem-legal on every
    supported host, so NEL names install under both entry points; every
    other control (C0, DEL, the remaining C1, reachable only via explicit
    double-quoted escapes since the source gate refuses them raw) refuses
    ``source_member_invalid`` fail-closed. Killer for the R12b mutant,
    which admits U+0080.
    """

    root = tmp_path / "source"
    package = root / "pkg"
    package.mkdir(parents=True)
    (package / "SKILL.md").write_text(
        f"---\nname: {spelling}\ndescription: valid description\n---\n",
        encoding="utf-8",
    )
    if accepted:
        if entry == "collection":
            members = expand_collection(root, _collection())
            assert [member.name for member in members] == ["rev\x85iew"]
        else:
            selected = resolve_individual(
                root,
                IndividualSelector(name="rev\x85iew", from_alias="local", directory="pkg"),
            )
            assert selected.name == "rev\x85iew"
        return
    with pytest.raises(source_errors.SourceError) as exc:
        if entry == "collection":
            expand_collection(root, _collection())
        else:
            resolve_individual(
                root, IndividualSelector(name="review", from_alias="local", directory="pkg")
            )
    assert exc.value.code == "source_member_invalid"
    assert "not a portable destination name" in str(exc.value)


# ---------------------------------------------------------------------------
# Rev13 reviewer attacks (committed under the reviewer's names)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("entry", ["collection", "individual"])
@pytest.mark.parametrize("value", ['"review"#unseparated', "'review'#unseparated"])
@pytest.mark.parametrize("field", ["name", "description"])
def test_quoted_comment_requires_separation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, entry: str, value: str, field: str
) -> None:
    """Committed reviewer attack (rev12): quoted comment needs separation.

    After a closing quote only SPACE/TAB may follow; a ``#`` comment is
    admitted only with at least one separating SPACE or TAB. The reviewer's
    oracle assertion is corrected here: PyYAML 6.0.3 ACCEPTS the
    zero-separator shape (pinned below), so the refusal is csk's documented
    YAML 1.2 separation-strictness (bound B14), not an oracle agreement.
    """

    monkeypatch.setenv("CSK_CONFIG", str(tmp_path / "home" / "config.json"))
    root = tmp_path / "src"
    member = root / "review"
    member.mkdir(parents=True)
    fields: dict[str, str] = {"name": "review", "description": "Valid description"}
    fields[field] = value
    document = "\n".join(f"{key}: {val}" for key, val in fields.items()) + "\n"
    # PyYAML leniency pin: the oracle accepts the unseparated comment.
    loaded = yaml.safe_load(document)
    assert isinstance(loaded, dict)
    assert loaded[field] == "review"
    (member / "SKILL.md").write_text("---\n" + document + "---\n# Body\n")
    with pytest.raises(source_errors.SourceError) as caught:
        if entry == "collection":
            expand_collection(
                root,
                CollectionSelector(from_alias="local", directory=".", include=("*",), exclude=()),
            )
        else:
            resolve_individual(
                root, IndividualSelector(name="review", from_alias="local", directory="review")
            )
    assert caught.value.code == "source_member_invalid"


@pytest.mark.parametrize("entry", ["collection", "individual"])
def test_body_lone_cr_is_ignored(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, entry: str) -> None:
    """Committed reviewer attack (rev12): a lone CR in the body is ignored.

    Verbatim from the rev12 review (imports adapted to this module): only the
    framed frontmatter region is validated, so body bytes after the closing
    fence never refuse. The paired frontmatter-negative control is the framing
    refuse table's ``lone-cr`` row (both entries).
    """

    monkeypatch.setenv("CSK_CONFIG", str(tmp_path / "home" / "config.json"))
    root = tmp_path / "src"
    member = root / "review"
    member.mkdir(parents=True)
    (member / "SKILL.md").write_bytes(
        b"---\nname: review\ndescription: Valid description\n---\n# Body\ntext\rcontent\n"
    )
    if entry == "collection":
        assert (
            expand_collection(
                root,
                CollectionSelector(from_alias="local", directory=".", include=("*",), exclude=()),
            )[0].name
            == "review"
        )
    else:
        assert (
            resolve_individual(
                root, IndividualSelector(name="review", from_alias="local", directory="review")
            ).name
            == "review"
        )


@pytest.mark.parametrize("entry", ["collection", "individual"])
@pytest.mark.parametrize(
    "triggers, accepted",
    [
        pytest.param("[alpha]#c", False, id="flow-zero-sep"),
        pytest.param("[alpha] #c", True, id="flow-one-space"),
        pytest.param("[alpha]  #c", True, id="flow-two-spaces"),
    ],
)
def test_flow_trailer_comment_requires_separation(
    tmp_path: Path, entry: str, triggers: str, accepted: bool
) -> None:
    """The shared trailer checker enforces separation for flow lists too."""

    root = tmp_path / "src"
    package = root / "pkg"
    package.mkdir(parents=True)
    (package / "SKILL.md").write_text(
        f"---\nname: review\ndescription: valid\ntriggers: {triggers}\n---\n",
        encoding="utf-8",
    )
    if accepted:
        if entry == "collection":
            assert expand_collection(root, _collection())[0].name == "review"
        else:
            assert (
                resolve_individual(
                    root, IndividualSelector(name="review", from_alias="local", directory="pkg")
                ).name
                == "review"
            )
        return
    with pytest.raises(source_errors.SourceError) as excinfo:
        if entry == "collection":
            expand_collection(root, _collection())
        else:
            resolve_individual(
                root, IndividualSelector(name="review", from_alias="local", directory="pkg")
            )
    assert excinfo.value.code == source_errors.CODE_MEMBER_INVALID


# ---------------------------------------------------------------------------
# Rev14: EOF carriage return is not CRLF (committed reviewer attack + matrix)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("entry", ["collection", "individual"])
@pytest.mark.parametrize("fence", ["---", "..."])
@pytest.mark.parametrize("ending", ["\r", "\r\n", ""])
def test_eof_carriage_return_is_not_crlf(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, entry: str, fence: str, ending: str
) -> None:
    """Committed reviewer attack (rev13): a final unpaired CR is lone, not CRLF.

    Verbatim from the rev13 review (imports adapted to this module):
    ``_split_frontmatter_lines`` stripped a trailing CR from every
    ``split("\\n")`` segment including the final unterminated one, so
    ``---<CR>``/``...<CR>`` at EOF framed as a clean fence and was accepted.
    CRLF is normalised only where a CR immediately precedes a consumed LF,
    so the ``\\r`` ending refuses (``source_member_invalid``) while genuine
    CRLF and plain EOF accept. Driven through BOTH public entry points.
    """

    monkeypatch.setenv("CSK_CONFIG", str(tmp_path / "home" / "config.json"))
    root = tmp_path / "source"
    member = root / "review"
    member.mkdir(parents=True)
    (member / "SKILL.md").write_bytes(
        ("---\nname: review\ndescription: valid\n" + fence + ending).encode()
    )

    def drive() -> object:
        if entry == "collection":
            return expand_collection(
                root,
                CollectionSelector(
                    from_alias="local", directory=".", include=("*",), exclude=()
                ),
            )
        return resolve_individual(
            root, IndividualSelector(name="review", from_alias="local", directory="review")
        )

    if ending == "\r":
        with pytest.raises(source_errors.SourceError) as caught:
            drive()
        assert caught.value.code == "source_member_invalid"
    else:
        drive()


@pytest.mark.parametrize("entry", ["collection", "individual"])
@pytest.mark.parametrize("marker", ["---", "..."])
@pytest.mark.parametrize("region", ["fence", "body"])
@pytest.mark.parametrize(
    "suffix",
    [
        pytest.param(b"", id="eof"),
        pytest.param(b"\n", id="lf"),
        pytest.param(b"\r\n", id="crlf"),
        pytest.param(b"\r", id="lone-cr"),
    ],
)
def test_framing_eof_endings_matrix(
    tmp_path: Path, entry: str, marker: str, region: str, suffix: bytes
) -> None:
    """EOF endings x closing markers x framed/body region (both entries).

    A lone CR terminates the framed region as a refusal (``---<CR>`` at EOF
    is not a fence, so the frontmatter never closes), while EOF, LF and
    CRLF endings accept; every body-region ending accepts because body
    lines after the closing fence are never validated.
    """

    if region == "fence":
        payload = b"---\nname: review\ndescription: d\n" + marker.encode() + suffix
        refused = suffix == b"\r"
    else:
        payload = (
            b"---\nname: review\ndescription: d\n"
            + marker.encode()
            + b"\nnote"
            + suffix
        )
        refused = False
    if refused:
        with pytest.raises(source_errors.SourceError) as excinfo:
            _drive_bytes_entry(tmp_path, entry, payload)
        assert excinfo.value.code == source_errors.CODE_MEMBER_INVALID
    else:
        assert _drive_bytes_entry(tmp_path, entry, payload) == "review"


# ---------------------------------------------------------------------------
# F3 rev15: YAML 1.2.2 section 5.7 double-quoted escape alphabet
# ---------------------------------------------------------------------------


@pytest.mark.parametrize('entry', ['collection', 'individual'])
@pytest.mark.parametrize('escape', ['\t','0','a','b','t','n','v','f','r','e',' ', '"', '/', '\\','N','_', 'L','P','x41','u0041','U00000041'])
def test_yaml_double_quote_escape_alphabet(tmp_path: Path, entry: str, escape: str):
    # Committed verbatim from the rev14 review: `_DOUBLE_QUOTED_ESCAPES`
    # omitted `\/` and backslash + literal TAB, so valid double-quoted
    # descriptions were refused. The oracle decides the expectation.
    front = 'name: review\ndescription: "before\\' + escape + 'after"\n'
    expected = yaml.safe_load(front)
    assert isinstance(expected['description'], str)
    root = tmp_path / 'source'
    member = root / 'review'
    member.mkdir(parents=True)
    (member / 'SKILL.md').write_bytes(('---\n' + front + '---\n').encode())
    if entry == 'collection':
        result = expand_collection(root, CollectionSelector(from_alias='local', directory='.', include=('*',), exclude=()))
        assert result[0].name == 'review'
    else:
        assert resolve_individual(root, IndividualSelector(name='review', from_alias='local', directory='review')).name == 'review'


# The 20 double-quoted escape rows of YAML 1.2.2 section 5.7, productions
# [42]-[62], written independently of the implementation: literal spellings
# and literal expected values, nothing imported from selection.py. Row `t`
# has two spellings (`t` and backslash + literal TAB, production [53]), so
# the table expands to 21 parametrize cases.
_DOUBLE_QUOTED_ALPHABET: tuple[tuple[str, str, str], ...] = (
    ("esc-0", "\\0", "\0"),
    ("esc-a", "\\a", "\a"),
    ("esc-b", "\\b", "\b"),
    ("esc-t-letter", "\\t", "\t"),
    ("esc-t-literal-tab", "\\\t", "\t"),
    ("esc-n", "\\n", "\n"),
    ("esc-v", "\\v", "\v"),
    ("esc-f", "\\f", "\f"),
    ("esc-r", "\\r", "\r"),
    ("esc-e", "\\e", "\x1b"),
    ("esc-space", "\\ ", " "),
    ("esc-quote", "\\\"", "\""),
    ("esc-slash", "\\/", "/"),
    ("esc-backslash", "\\\\", "\\"),
    ("esc-N", "\\N", "\x85"),
    ("esc-underscore", "\\_", "\xa0"),
    ("esc-L", "\\L", "\u2028"),
    ("esc-P", "\\P", "\u2029"),
    ("esc-x", "\\x41", "A"),
    ("esc-u", "\\u0041", "A"),
    ("esc-U", "\\U00000041", "A"),
)


@pytest.mark.parametrize("entry", ["collection", "individual"])
@pytest.mark.parametrize(
    "case_id,suffix,expected",
    [pytest.param(case_id, suffix, expected, id=case_id) for case_id, suffix, expected in _DOUBLE_QUOTED_ALPHABET],
)
def test_double_quoted_escape_alphabet_description_exact(
    tmp_path: Path, entry: str, case_id: str, suffix: str, expected: str
) -> None:
    """Every alphabet row decodes to its exact value (description field).

    Exactness is pinned through the public scalar tokenizer; acceptance is
    driven through BOTH public entry points on a filesystem fixture. Entry
    points surface the installed name only, so exact description bytes are
    asserted at the tokenizer they both call.
    """

    _ = case_id
    token = f'"before{suffix}after"'
    assert selection.parse_frontmatter_scalar(token, "'alphabet'") == f"before{expected}after"
    frontmatter = f"---\nname: review\ndescription: {token}\n---\n# skill\n"
    root = tmp_path / "src"
    if entry == "individual":
        member = root / "skills" / "pkg"
        member.mkdir(parents=True)
        (member / "SKILL.md").write_text(frontmatter, encoding="utf-8")
        selected = resolve_individual(
            root,
            IndividualSelector(name="review", from_alias="local", directory="skills/pkg"),
        )
        assert selected.name == "review"
    else:
        member = root / "col" / "pkg"
        member.mkdir(parents=True)
        (member / "SKILL.md").write_text(frontmatter, encoding="utf-8")
        members = expand_collection(root, _collection("col", ("pkg",)))
        assert members[0].name == "review"


# Alphabet rows whose decoded value is an installable destination name
# (space, quote, NEL, NBSP, LS, PS, `A`); every other row decodes to a
# control or separator and refuses as a name (next test).
_DOUBLE_QUOTED_PORTABLE_NAMES: tuple[tuple[str, str, str], ...] = (
    ("esc-space", "\\ ", " "),
    ("esc-quote", "\\\"", "\""),
    ("esc-N", "\\N", "\x85"),
    ("esc-underscore", "\\_", "\xa0"),
    ("esc-L", "\\L", "\u2028"),
    ("esc-P", "\\P", "\u2029"),
    ("esc-x", "\\x41", "A"),
    ("esc-u", "\\u0041", "A"),
    ("esc-U", "\\U00000041", "A"),
)


@pytest.mark.parametrize("entry", ["collection", "individual"])
@pytest.mark.parametrize(
    "case_id,suffix,expected",
    [
        pytest.param(case_id, suffix, expected, id=case_id)
        for case_id, suffix, expected in _DOUBLE_QUOTED_PORTABLE_NAMES
    ],
)
def test_double_quoted_escape_alphabet_name_exact(
    tmp_path: Path, entry: str, case_id: str, suffix: str, expected: str
) -> None:
    """Portable-safe alphabet rows install under their exact decoded name."""

    _ = case_id
    expected_name = f"before{expected}after"
    frontmatter = (
        f'---\nname: "before{suffix}after"\ndescription: valid description\n---\n# skill\n'
    )
    root = tmp_path / "src"
    if entry == "individual":
        member = root / "skills" / "pkg"
        member.mkdir(parents=True)
        (member / "SKILL.md").write_text(frontmatter, encoding="utf-8")
        selected = resolve_individual(
            root,
            IndividualSelector(name=expected_name, from_alias="local", directory="skills/pkg"),
        )
        assert selected.name == expected_name
    else:
        member = root / "col" / "pkg"
        member.mkdir(parents=True)
        (member / "SKILL.md").write_text(frontmatter, encoding="utf-8")
        members = expand_collection(root, _collection("col", ("pkg",)))
        assert members[0].name == expected_name


# Alphabet rows whose decoded value is NOT installable (C0/DEL controls and
# the `/` and `\` separators): the parse succeeds and the installable-name
# gate refuses.
_DOUBLE_QUOTED_NONPORTABLE_NAMES: tuple[tuple[str, str], ...] = (
    ("esc-0", "\\0"),
    ("esc-a", "\\a"),
    ("esc-b", "\\b"),
    ("esc-t-letter", "\\t"),
    ("esc-t-literal-tab", "\\\t"),
    ("esc-n", "\\n"),
    ("esc-v", "\\v"),
    ("esc-f", "\\f"),
    ("esc-r", "\\r"),
    ("esc-e", "\\e"),
    ("esc-slash", "\\/"),
    ("esc-backslash", "\\\\"),
)


@pytest.mark.parametrize("entry", ["collection", "individual"])
@pytest.mark.parametrize(
    "case_id,suffix",
    [pytest.param(case_id, suffix, id=case_id) for case_id, suffix in _DOUBLE_QUOTED_NONPORTABLE_NAMES],
)
def test_double_quoted_escape_name_gate_refuses(
    tmp_path: Path, entry: str, case_id: str, suffix: str
) -> None:
    """Control/separator decodings refuse as names on BOTH entry points."""

    _ = case_id
    frontmatter = (
        f'---\nname: "before{suffix}after"\ndescription: valid description\n---\n# skill\n'
    )
    root = tmp_path / "src"
    if entry == "individual":
        member = root / "skills" / "pkg"
        member.mkdir(parents=True)
        (member / "SKILL.md").write_text(frontmatter, encoding="utf-8")
        with pytest.raises(source_errors.SourceError) as excinfo:
            resolve_individual(
                root,
                IndividualSelector(name="review", from_alias="local", directory="skills/pkg"),
            )
    else:
        member = root / "col" / "pkg"
        member.mkdir(parents=True)
        (member / "SKILL.md").write_text(frontmatter, encoding="utf-8")
        with pytest.raises(source_errors.SourceError) as excinfo:
            expand_collection(root, _collection("col", ("pkg",)))
    assert excinfo.value.code == source_errors.CODE_MEMBER_INVALID
    assert "not a portable destination name" in str(excinfo.value)


# Not escapable: any other escape letter, a truncated hex escape, a non-hex
# digit, a code point above U+10FFFF, or a surrogate. Each row pins the
# refusal fragment its shape must produce.
_DOUBLE_QUOTED_INVALID: tuple[tuple[str, str, str], ...] = (
    ("other-letter-q", '"a\\qb"', "unsupported escape"),
    ("other-letter-s", '"a\\sb"', "unsupported escape"),
    ("other-digit", '"a\\2b"', "unsupported escape"),
    ("other-punct", '"a\\?b"', "unsupported escape"),
    ("nonhex-x", '"\\xZZ"', "invalid \\x escape"),
    ("truncated-x", '"a\\x4"', "invalid \\x escape"),
    ("nonhex-u", '"\\u00Z1"', "invalid \\u escape"),
    ("truncated-u", '"a\\u004"', "invalid \\u escape"),
    ("truncated-U", '"a\\U000041b"', "invalid \\U escape"),
    ("above-max-U", '"\\U00110000"', "above U+10FFFF"),
    ("surrogate-u-low", '"\\uD800"', "surrogate"),
    ("surrogate-u-high", '"\\uDFFF"', "surrogate"),
    ("surrogate-U-low", '"\\U0000D800"', "surrogate"),
    ("surrogate-U-high", '"\\U0000DC00"', "surrogate"),
    ("unterminated-escape", '"ab\\', "unterminated escape"),
)


@pytest.mark.parametrize("entry", ["collection", "individual"])
@pytest.mark.parametrize("field", ["name", "description"])
@pytest.mark.parametrize(
    "case_id,token,fragment",
    [pytest.param(case_id, token, fragment, id=case_id) for case_id, token, fragment in _DOUBLE_QUOTED_INVALID],
)
def test_double_quoted_invalid_escape_refused(
    tmp_path: Path, entry: str, field: str, case_id: str, token: str, fragment: str
) -> None:
    """Invalid double-quoted escapes refuse for BOTH fields on BOTH entries."""

    _ = case_id
    if field == "name":
        name_token = token
        description_token = "valid description"
    else:
        name_token = "review"
        description_token = token
    frontmatter = f"---\nname: {name_token}\ndescription: {description_token}\n---\n# skill\n"
    root = tmp_path / "src"
    if entry == "individual":
        member = root / "skills" / "pkg"
        member.mkdir(parents=True)
        (member / "SKILL.md").write_text(frontmatter, encoding="utf-8")
        with pytest.raises(source_errors.SourceError) as excinfo:
            resolve_individual(
                root,
                IndividualSelector(name="review", from_alias="local", directory="skills/pkg"),
            )
    else:
        member = root / "col" / "pkg"
        member.mkdir(parents=True)
        (member / "SKILL.md").write_text(frontmatter, encoding="utf-8")
        with pytest.raises(source_errors.SourceError) as excinfo:
            expand_collection(root, _collection("col", ("pkg",)))
    assert excinfo.value.code == source_errors.CODE_MEMBER_INVALID
    assert fragment in str(excinfo.value)


# ---------------------------------------------------------------------------
# F1 rev16: exhaustive pre-read walk (pruned nested trees are not skipped)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize('entry', ['collection', 'individual'])
@pytest.mark.parametrize('managed', ['.git', '.agents', '.codex'])
def test_pruned_nested_tree_never_read_outside(tmp_path, monkeypatch, entry, managed):
    # Committed from the rev15 review: `references/<managed>/leak.md ->
    # /outside` bypassed `_reject_links_in_member` (which skipped pruned
    # names) and was opened by `skillcheck._prompt_markdown_files`
    # (`references.rglob("*.md")` without that pruning). The pre-read walk
    # now covers every entry with no name pruning, so no outside bytes open.
    monkeypatch.setenv('CSK_CONFIG', str(tmp_path / 'home/config.json'))
    root = tmp_path / 'source'
    member = root / 'review'
    hidden = member / 'references' / managed
    hidden.mkdir(parents=True)
    outside = tmp_path / 'outside.md'
    outside.write_text('scripts/tool outside bytes', encoding='utf-8')
    (hidden / 'leak.md').symlink_to(outside)
    (member / 'SKILL.md').write_text('---\nname: review\ndescription: valid\n---\n')
    (member / 'scripts').mkdir()
    (member / 'scripts/tool').write_text('#!/bin/sh\n')
    (member / 'agent-skill.json').write_text(json.dumps({'schema_version': 2, 'runtime_roots': ['scripts'], 'commands': {'tool': {'type': 'script', 'unix_path': 'scripts/tool'}}}))
    reads = []
    original = Path.read_text
    def watched(path, *args, **kwargs):
        if path.resolve() == outside.resolve():
            reads.append(str(path.relative_to(member)))
        return original(path, *args, **kwargs)
    monkeypatch.setattr(Path, 'read_text', watched)
    try:
        if entry == 'collection':
            expand_collection(root, CollectionSelector(from_alias='local', directory='.', include=('*',), exclude=()))
        else:
            resolve_individual(root, IndividualSelector(name='review', from_alias='local', directory='review'))
    except source_errors.SourceError:
        pass
    assert reads == [], f'External reads through pruned tree: {reads}'


# Every link shape the pre-read walk must refuse before any downstream
# reader (SKILL.md, manifest, skillcheck rglob) opens outside bytes. The
# manifest + scripts fixture forces `skillcheck` to enumerate prompt
# Markdown, so a skipped subtree would actually read outside.
_PRE_READ_LINK_SHAPES: tuple[str, ...] = (
    "references-git",
    "references-agents",
    "references-codex",
    "deep-nested",
    "member-root-file",
    "member-dir-link",
)


def _install_pre_read_link_shape(member: Path, shape: str, outside_file: Path) -> None:
    if shape == "references-git":
        hidden = member / "references" / ".git"
        hidden.mkdir(parents=True)
        (hidden / "leak.md").symlink_to(outside_file)
    elif shape == "references-agents":
        hidden = member / "references" / ".agents"
        hidden.mkdir(parents=True)
        (hidden / "leak.md").symlink_to(outside_file)
    elif shape == "references-codex":
        hidden = member / "references" / ".codex"
        hidden.mkdir(parents=True)
        (hidden / "leak.md").symlink_to(outside_file)
    elif shape == "deep-nested":
        hidden = member / "references" / "deep" / "nested" / "dir"
        hidden.mkdir(parents=True)
        (hidden / "leak.md").symlink_to(outside_file)
    elif shape == "member-root-file":
        (member / "evil.md").symlink_to(outside_file)
    elif shape == "member-dir-link":
        outside_dir = outside_file.parent
        outside_dir.mkdir(parents=True, exist_ok=True)
        (member / "references").mkdir(parents=True, exist_ok=True)
        (member / "references" / "linked").symlink_to(
            outside_dir, target_is_directory=True
        )
    else:  # pragma: no cover - exhaustive parametrize ids
        raise AssertionError(f"unknown pre-read shape {shape!r}")


@pytest.mark.parametrize("entry", ["collection", "individual"])
@pytest.mark.parametrize(
    "shape",
    [pytest.param(shape, id=shape) for shape in _PRE_READ_LINK_SHAPES],
)
def test_pre_read_walk_refuses_link_without_outside_read(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, entry: str, shape: str
) -> None:
    """Every link shape refuses AND opens zero outside bytes on BOTH entries."""

    monkeypatch.setenv("CSK_CONFIG", str(tmp_path / "home" / "config.json"))
    root = tmp_path / "src"
    member = root / "review"
    member.mkdir(parents=True)
    if shape == "member-dir-link":
        outside_dir = tmp_path / "outside-dir"
        outside_dir.mkdir()
        outside_file = outside_dir / "inside.md"
        outside_file.write_text("scripts/tool outside bytes", encoding="utf-8")
    else:
        outside_file = tmp_path / "outside.md"
        outside_file.write_text("scripts/tool outside bytes", encoding="utf-8")
    _install_pre_read_link_shape(member, shape, outside_file)
    (member / "SKILL.md").write_text(
        "---\nname: review\ndescription: valid\n---\n", encoding="utf-8"
    )
    (member / "scripts").mkdir(exist_ok=True)
    (member / "scripts" / "tool").write_text("#!/bin/sh\n", encoding="utf-8")
    (member / "agent-skill.json").write_text(
        json.dumps(
            {
                "schema_version": 2,
                "runtime_roots": ["scripts"],
                "commands": {
                    "tool": {"type": "script", "unix_path": "scripts/tool"}
                },
            }
        ),
        encoding="utf-8",
    )
    outside_root = outside_file.resolve().parent if shape == "member-dir-link" else None
    outside_resolved = outside_file.resolve()
    reads: list[str] = []
    original_read_text = Path.read_text
    original_read_bytes = Path.read_bytes

    def watched_text(path: Path, *args: object, **kwargs: object) -> str:
        try:
            resolved = path.resolve()
        except OSError:
            resolved = path
        if resolved == outside_resolved or (
            outside_root is not None
            and (resolved == outside_root or outside_root in resolved.parents)
        ):
            reads.append(str(path))
        return original_read_text(path, *args, **kwargs)  # type: ignore[arg-type]

    def watched_bytes(path: Path, *args: object, **kwargs: object) -> bytes:
        try:
            resolved = path.resolve()
        except OSError:
            resolved = path
        if resolved == outside_resolved or (
            outside_root is not None
            and (resolved == outside_root or outside_root in resolved.parents)
        ):
            reads.append(str(path))
        return original_read_bytes(path, *args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(Path, "read_text", watched_text)
    monkeypatch.setattr(Path, "read_bytes", watched_bytes)
    with pytest.raises(source_errors.SourceError) as excinfo:
        if entry == "collection":
            expand_collection(
                root,
                CollectionSelector(
                    from_alias="local", directory=".", include=("*",), exclude=()
                ),
            )
        else:
            resolve_individual(
                root,
                IndividualSelector(
                    name="review", from_alias="local", directory="review"
                ),
            )
    assert excinfo.value.code == source_errors.CODE_MEMBER_INVALID
    assert reads == [], f"outside bytes were opened for {shape}: {reads}"


@pytest.mark.parametrize("entry", ["collection", "individual"])
def test_pre_read_walk_accepts_regular_pruned_name_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, entry: str
) -> None:
    """A regular `references/.git/notes.md` (no link) is accepted on BOTH entries.

    The pre-read walk has no name pruning, so the regular file is walked and
    contained; it is inside the member, so validation proceeds and the
    downstream `rglob` reads it safely.
    """

    monkeypatch.setenv("CSK_CONFIG", str(tmp_path / "home" / "config.json"))
    root = tmp_path / "src"
    member = root / "review"
    hidden = member / "references" / ".git"
    hidden.mkdir(parents=True)
    (hidden / "notes.md").write_text("# notes\n", encoding="utf-8")
    (member / "SKILL.md").write_text(
        "---\nname: review\ndescription: valid\n---\n", encoding="utf-8"
    )
    (member / "scripts").mkdir()
    (member / "scripts" / "tool").write_text("#!/bin/sh\n", encoding="utf-8")
    (member / "agent-skill.json").write_text(
        json.dumps(
            {
                "schema_version": 2,
                "runtime_roots": ["scripts"],
                "commands": {
                    "tool": {"type": "script", "unix_path": "scripts/tool"}
                },
            }
        ),
        encoding="utf-8",
    )
    if entry == "collection":
        members = expand_collection(
            root,
            CollectionSelector(
                from_alias="local", directory=".", include=("*",), exclude=()
            ),
        )
        assert [found.name for found in members] == ["review"]
    else:
        found = resolve_individual(
            root,
            IndividualSelector(name="review", from_alias="local", directory="review"),
        )
        assert found.name == "review"


# ---------------------------------------------------------------------------
# F1 rev17: cyclic links refuse structured (no leaked RuntimeError)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("entry", ["collection", "individual"])
@pytest.mark.parametrize("shape", ["self-loop", "two-link-cycle", "fifo"])
def test_pre_read_walk_structured_refusal(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, entry: str, shape: str
) -> None:
    # Committed from the rev16 review: the pre-read walk resolved each link
    # before refusing and caught only OSError, so a self-referential or
    # two-link cyclic symlink raised RuntimeError on Python 3.12 instead of
    # source_member_invalid. Refusal is now decided from lstat alone, with
    # resolution kept diagnostic-only behind a never-raising wrapper.
    if shape == "fifo" and not hasattr(os, "mkfifo"):
        pytest.skip("os.mkfifo unavailable: fifo bound not observable on this host")
    monkeypatch.setenv("CSK_CONFIG", str(tmp_path / "home" / "config.json"))
    root = tmp_path / "source"
    member = root / "review"
    member.mkdir(parents=True)
    (member / "SKILL.md").write_text(
        "---\nname: review\ndescription: Review skill\n---\n", encoding="utf-8"
    )
    nested = member / "references" / ".git"
    nested.mkdir(parents=True)
    if shape == "self-loop":
        (nested / "loop.md").symlink_to("loop.md")
    elif shape == "two-link-cycle":
        (nested / "a.md").symlink_to("b.md")
        (nested / "b.md").symlink_to("a.md")
    else:
        os.mkfifo(nested / "pipe")
    with pytest.raises(source_errors.SourceError) as excinfo:
        if entry == "collection":
            expand_collection(root, _collection(".", ("*",), ()))
        else:
            resolve_individual(
                root,
                IndividualSelector(
                    name="review", from_alias="local", directory="review"
                ),
            )
    assert excinfo.value.code == source_errors.CODE_MEMBER_INVALID


_CYCLIC_LINK_POSITIONS: tuple[str, ...] = (
    "member-root-self-loop",
    "nested-two-link-cycle",
    "references-git-self-loop",
    "skill-md-self-loop",
)


def _install_cyclic_link_position(member: Path, position: str) -> None:
    if position == "member-root-self-loop":
        (member / "loop.md").symlink_to("loop.md")
    elif position == "nested-two-link-cycle":
        nested = member / "docs" / "deep"
        nested.mkdir(parents=True)
        (nested / "a.md").symlink_to("b.md")
        (nested / "b.md").symlink_to("a.md")
    elif position == "references-git-self-loop":
        hidden = member / "references" / ".git"
        hidden.mkdir(parents=True)
        (hidden / "loop.md").symlink_to("loop.md")
    elif position == "skill-md-self-loop":
        (member / "SKILL.md").unlink()
        (member / "SKILL.md").symlink_to("SKILL.md")
    else:  # pragma: no cover - exhaustive parametrize ids
        raise AssertionError(f"unknown cyclic position {position!r}")


@pytest.mark.parametrize("entry", ["collection", "individual"])
@pytest.mark.parametrize(
    "position",
    [pytest.param(position, id=position) for position in _CYCLIC_LINK_POSITIONS],
)
def test_cyclic_link_position_yields_structured_refusal(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, entry: str, position: str
) -> None:
    """A cyclic link at any member position refuses structured on BOTH entries.

    Covers the member root, a nested directory, `references/.git`, and the
    SKILL.md path itself: no path-based post-open resolution may leak
    the symlink-loop `RuntimeError` for any filesystem shape.
    """

    monkeypatch.setenv("CSK_CONFIG", str(tmp_path / "home" / "config.json"))
    root = tmp_path / "src"
    member = root / "review"
    member.mkdir(parents=True)
    (member / "SKILL.md").write_text(
        "---\nname: review\ndescription: valid\n---\n", encoding="utf-8"
    )
    _install_cyclic_link_position(member, position)
    with pytest.raises(source_errors.SourceError) as excinfo:
        if entry == "collection":
            expand_collection(root, _collection(".", ("*",), ()))
        else:
            resolve_individual(
                root,
                IndividualSelector(
                    name="review", from_alias="local", directory="review"
                ),
            )
    assert excinfo.value.code == source_errors.CODE_MEMBER_INVALID


def test_cyclic_skill_md_link_refused_before_read(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A self-referential SKILL.md link refuses without resolving its target.

    Drives the metadata-file gate (`read_skill_md_name` ->
    `_ensure_contained_regular_file`) directly: the link decision comes from
    `lstat` alone, so the cyclic target never raises.
    """

    monkeypatch.setenv("CSK_CONFIG", str(tmp_path / "home" / "config.json"))
    root = tmp_path / "src"
    member = root / "review"
    member.mkdir(parents=True)
    (member / "SKILL.md").write_text(
        "---\nname: review\ndescription: valid\n---\n", encoding="utf-8"
    )
    (member / "SKILL.md").unlink()
    (member / "SKILL.md").symlink_to("SKILL.md")
    with pytest.raises(source_errors.SourceError) as excinfo:
        selection.read_skill_md_name(member, "'review'", resolved_root=root.resolve())
    assert excinfo.value.code == source_errors.CODE_MEMBER_INVALID


def test_nul_source_root_yields_structured_refusal(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An embedded-NUL path converts to SourceError, never a leaked ValueError."""

    monkeypatch.setenv("CSK_CONFIG", str(tmp_path / "home" / "config.json"))
    with pytest.raises(source_errors.SourceError) as excinfo:
        resolve_selector_directory(Path(str(tmp_path) + "\x00"), ".")
    assert excinfo.value.code == source_errors.CODE_SELECTION_INVALID


@pytest.mark.parametrize("entry", ["collection", "individual"])
def test_cyclic_selector_directory_yields_structured_refusal(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, entry: str
) -> None:
    """A self-referential selector directory refuses structured on BOTH entries.

    Pins the `candidate.resolve()` conversion in `resolve_selector_directory`:
    the symlink loop raises `RuntimeError` on resolution, which must surface
    as `source_selection_invalid` instead of leaking.
    """

    monkeypatch.setenv("CSK_CONFIG", str(tmp_path / "home" / "config.json"))
    root = tmp_path / "src"
    root.mkdir(parents=True)
    (root / "loop").symlink_to("loop")
    with pytest.raises(source_errors.SourceError) as excinfo:
        if entry == "collection":
            expand_collection(root, _collection("loop", ("*",), ()))
        else:
            resolve_individual(
                root,
                IndividualSelector(
                    name="loop", from_alias="local", directory="loop"
                ),
            )
    assert excinfo.value.code == source_errors.CODE_SELECTION_INVALID


@pytest.mark.parametrize("entry", ["collection", "individual"])
@pytest.mark.parametrize("failure", ["reference_read", "alias_probe"])
def test_filesystem_failure_is_structured_and_closed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, entry: str, failure: str
) -> None:
    """Committed from the rev17 review: filesystem failures stay structured.

    `reference_read` injects PermissionError at the real `Path.read_text`
    inside skillcheck's downstream reads: both entry points must refuse with
    the structured `source_member_invalid` (chained cause), never leak the
    raw PermissionError. `alias_probe` injects PermissionError at the
    descriptor identity probe for `source/.agents`: both entry points must
    STILL refuse with `source_output_overlap` (boundary undetermined), never
    accept the managed package as outside.
    """

    monkeypatch.setenv("CSK_CONFIG", str(tmp_path / "home" / "config.json"))
    root = tmp_path / "source"
    member = root / (".AGENTS" if failure == "alias_probe" else "skill")
    member.mkdir(parents=True)
    (member / "SKILL.md").write_text(
        "---\nname: review\ndescription: Review skill\n---\n# Review\n",
        encoding="utf-8",
    )

    def invoke():  # type: ignore[no-untyped-def]
        if entry == "collection":
            return expand_collection(
                root,
                CollectionSelector(
                    from_alias="local",
                    directory=".",
                    include=(member.name,),
                    exclude=(),
                ),
            )
        return resolve_individual(
            root,
            IndividualSelector(
                name="review", from_alias="local", directory=member.name
            ),
        )

    calls: list[str] = []
    if failure == "reference_read":
        refs = member / "references"
        refs.mkdir()
        target = refs / "notes.md"
        target.write_text("# Notes\n", encoding="utf-8")
        (member / "scripts").mkdir()
        (member / "scripts" / "tool").write_text("#!/bin/sh\n", encoding="utf-8")
        (member / "agent-skill.json").write_text(
            json.dumps(
                {
                    "schema_version": 2,
                    "runtime_roots": ["scripts"],
                    "commands": {
                        "tool": {"type": "script", "unix_path": "scripts/tool"}
                    },
                }
            ),
            encoding="utf-8",
        )
        invoke()  # Positive control: the same readable package is valid.
        original_read_text = Path.read_text

        def read_text(path: Path, *args, **kwargs):  # type: ignore[no-untyped-def]
            if path.resolve() == target.resolve():
                calls.append("read")
                raise PermissionError("injected reference read denial")
            return original_read_text(path, *args, **kwargs)

        monkeypatch.setattr(Path, "read_text", read_text)
    else:
        if not (root / ".agents").exists():
            pytest.skip("case-insensitive filesystem unavailable")
        with pytest.raises(source_errors.SourceError) as control:
            invoke()
        assert control.value.code == source_errors.CODE_OUTPUT_OVERLAP
        original_open_child = _selection_fs._open_child_directory

        def open_child(parent_fd, name, *, parent_path=None):  # type: ignore[no-untyped-def]
            if name == ".agents" and parent_path == root:
                calls.append("probe")
                raise PermissionError("injected identity probe denial")
            return original_open_child(parent_fd, name, parent_path=parent_path)

        monkeypatch.setattr(_selection_fs, "_open_child_directory", open_child)
    try:
        with pytest.raises(source_errors.SourceError) as excinfo:
            invoke()
    finally:
        assert calls, "injected filesystem operation was not reached"
    if failure == "reference_read":
        assert excinfo.value.code == source_errors.CODE_MEMBER_INVALID
    else:
        assert excinfo.value.code == source_errors.CODE_OUTPUT_OVERLAP
        assert "boundary undetermined" in excinfo.value.detail
    assert isinstance(excinfo.value.__cause__, PermissionError)


@pytest.mark.parametrize("entry", ["collection", "individual"])
def test_home_realpath_failure_is_not_absence(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, entry: str
) -> None:
    """Committed from the rev18 review: a failed home probe is not absence.

    A valid ``source/review`` package with ``CSK_CONFIG`` under a home-alias
    symlink to the source refuses ``source_output_overlap`` (the package is
    inside the csk home). Injecting ``PermissionError`` only at the descriptor
    open of that home alias must STILL refuse structured through BOTH entry
    points instead of treating the failed boundary probe as absence.
    """

    root = tmp_path / "source"
    member = root / "review"
    member.mkdir(parents=True)
    (member / "SKILL.md").write_text(
        "---\nname: review\ndescription: Review\n---\n", encoding="utf-8"
    )
    home_alias = tmp_path / "home-alias"
    home_alias.symlink_to(root, target_is_directory=True)
    monkeypatch.setenv("CSK_CONFIG", str(home_alias / "config.json"))

    def invoke():  # type: ignore[no-untyped-def]
        if entry == "collection":
            return expand_collection(
                root,
                CollectionSelector(
                    from_alias="local",
                    directory=".",
                    include=("review",),
                    exclude=(),
                ),
            )
        return resolve_individual(
            root,
            IndividualSelector(
                name="review", from_alias="local", directory="review"
            ),
        )

    with pytest.raises(source_errors.SourceError) as baseline:
        invoke()
    assert baseline.value.code == source_errors.CODE_OUTPUT_OVERLAP
    original_open = _selection_fs.os.open
    hits: list[str] = []

    def denied(path, *args, **kwargs):  # type: ignore[no-untyped-def]
        if os.fspath(path) == str(home_alias):
            hits.append(str(path))
            raise PermissionError("injected home alias lookup denial")
        return original_open(path, *args, **kwargs)

    monkeypatch.setattr(_selection_fs.os, "open", denied)
    try:
        with pytest.raises(source_errors.SourceError) as excinfo:
            invoke()
        assert excinfo.value.code == source_errors.CODE_OUTPUT_OVERLAP
        assert "boundary undetermined" in excinfo.value.detail
        assert isinstance(excinfo.value.__cause__, PermissionError)
    finally:
        assert hits, "injected home-alias lookup was not reached"


@pytest.mark.parametrize("entry", ["collection", "individual"])
def test_member_intermediate_realpath_failure_is_not_absence(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, entry: str
) -> None:
    """A failed intermediate member probe refuses ``source_member_invalid``.

    Same shape as the home-alias fault, but for a member subtree: the
    descriptor-backed pre-read walk's open of ``references`` raises
    ``PermissionError``. BOTH entry points must still refuse structured
    instead of accepting the package after a failed filesystem operation.
    """

    monkeypatch.setenv("CSK_CONFIG", str(tmp_path / "home" / "config.json"))
    root = tmp_path / "source"
    member = root / "review"
    refs = member / "references"
    refs.mkdir(parents=True)
    (member / "SKILL.md").write_text(
        "---\nname: review\ndescription: Review skill\n---\n", encoding="utf-8"
    )
    (refs / "notes.md").write_text("# Notes\n", encoding="utf-8")

    def invoke():  # type: ignore[no-untyped-def]
        if entry == "collection":
            return expand_collection(root, _collection(".", ("review",), ()))
        return resolve_individual(
            root,
            IndividualSelector(
                name="review", from_alias="local", directory="review"
            ),
        )

    invoke()  # Positive control: the same readable package is valid.
    original_open_child = _selection_fs.SelectionSession._open_regular_child
    hits: list[str] = []

    def counting(self, parent, name, *, code, context):  # type: ignore[no-untyped-def]
        if name == "references" and parent.display == member:
            hits.append(name)
            raise PermissionError("injected intermediate lookup denial")
        return original_open_child(self, parent, name, code=code, context=context)

    monkeypatch.setattr(_selection_fs.SelectionSession, "_open_regular_child", counting)
    try:
        with pytest.raises(source_errors.SourceError) as excinfo:
            invoke()
        assert excinfo.value.code == source_errors.CODE_MEMBER_INVALID
        assert isinstance(excinfo.value.__cause__, PermissionError)
    finally:
        assert hits, "injected intermediate lookup was not reached"


@pytest.mark.parametrize("entry", ["collection", "individual"])
def test_selector_two_link_cycle_yields_structured_refusal(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, entry: str
) -> None:
    """A two-link selector cycle refuses structured via the visited set.

    Pins the descriptor traversal's visited-link identity detection plus the
    expansion cap: ``loop-a`` <-> ``loop-b`` must
    surface as ``source_selection_invalid`` on BOTH entries, never leak a
    raw ``RuntimeError``/``OSError`` and never resolve to a partial path.
    """

    monkeypatch.setenv("CSK_CONFIG", str(tmp_path / "home" / "config.json"))
    root = tmp_path / "src"
    root.mkdir(parents=True)
    (root / "loop-a").symlink_to("loop-b")
    (root / "loop-b").symlink_to("loop-a")
    with pytest.raises(source_errors.SourceError) as excinfo:
        if entry == "collection":
            expand_collection(root, _collection("loop-a", ("*",), ()))
        else:
            resolve_individual(
                root,
                IndividualSelector(
                    name="loop", from_alias="local", directory="loop-a"
                ),
            )
    assert excinfo.value.code == source_errors.CODE_SELECTION_INVALID


@pytest.mark.parametrize("entry", ["collection", "individual"])
def test_home_eloop_failure_is_not_absence(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, entry: str
) -> None:
    """An ``ELOOP`` home probe failure refuses, never reports outside.

    Same alias shape as ``test_home_realpath_failure_is_not_absence`` but
    with the ``ELOOP`` errno instead of ``PermissionError``: every errno
    except ``ENOENT`` propagates fail-closed through BOTH entry points.
    """

    root = tmp_path / "source"
    member = root / "review"
    member.mkdir(parents=True)
    (member / "SKILL.md").write_text(
        "---\nname: review\ndescription: Review\n---\n", encoding="utf-8"
    )
    home_alias = tmp_path / "home-alias"
    home_alias.symlink_to(root, target_is_directory=True)
    monkeypatch.setenv("CSK_CONFIG", str(home_alias / "config.json"))

    def invoke():  # type: ignore[no-untyped-def]
        if entry == "collection":
            return expand_collection(
                root,
                CollectionSelector(
                    from_alias="local",
                    directory=".",
                    include=("review",),
                    exclude=(),
                ),
            )
        return resolve_individual(
            root,
            IndividualSelector(
                name="review", from_alias="local", directory="review"
            ),
        )

    with pytest.raises(source_errors.SourceError) as baseline:
        invoke()
    assert baseline.value.code == source_errors.CODE_OUTPUT_OVERLAP
    original_open = _selection_fs.os.open
    hits: list[str] = []

    def eloop(path, *args, **kwargs):  # type: ignore[no-untyped-def]
        if os.fspath(path) == str(home_alias):
            hits.append(str(path))
            raise OSError(errno.ELOOP, "injected too many levels")
        return original_open(path, *args, **kwargs)

    monkeypatch.setattr(_selection_fs.os, "open", eloop)
    try:
        with pytest.raises(source_errors.SourceError) as excinfo:
            invoke()
        assert excinfo.value.code == source_errors.CODE_OUTPUT_OVERLAP
        assert "boundary undetermined" in excinfo.value.detail
        assert isinstance(excinfo.value.__cause__, OSError)
    finally:
        assert hits, "injected ELOOP lookup was not reached"


@pytest.mark.parametrize("entry", ["collection", "individual"])
def test_fresh_home_does_not_refuse(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, entry: str
) -> None:
    """A nonexistent csk home resolves via the literal tail and passes.

    Positive control for the ``ENOENT``-tail rule: ``CSK_CONFIG`` under a
    never-created home directory must not refuse ``source_output_overlap``;
    BOTH entry points accept the valid package.
    """

    home = tmp_path / "home"
    assert not home.exists()
    monkeypatch.setenv("CSK_CONFIG", str(home / "config.json"))
    root = tmp_path / "source"
    member = root / "review"
    _write_skill(member, "review", "Review skill")
    if entry == "collection":
        members = expand_collection(root, _collection(".", ("review",), ()))
        assert [member.name for member in members] == ["review"]
    else:
        selected = resolve_individual(
            root,
            IndividualSelector(
                name="review", from_alias="local", directory="review"
            ),
        )
        assert selected.name == "review"


# ---------------------------------------------------------------------------
# F2 (rev20): containment by filesystem identity, never by spelling
# ---------------------------------------------------------------------------

_BOUNDARY_UNICODE_NFC = "sourc\u00e9"
_BOUNDARY_UNICODE_NFD = unicodedata.normalize("NFD", _BOUNDARY_UNICODE_NFC)


@pytest.mark.parametrize("entry", ["collection", "individual"])
@pytest.mark.parametrize("alias", ["SOURCE", "source"])
def test_home_case_equivalent_boundary_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, entry: str, alias: str
) -> None:
    """Committed from the rev19 review: a case-variant home spelling refuses.

    ``CSK_CONFIG`` under ``SOURCE/`` (the same directory as ``source/`` on a
    case-insensitive filesystem) must refuse ``source_output_overlap``
    through BOTH entry points exactly like the identical spelling: home
    containment compares filesystem identity, never string spelling. The
    ``SOURCE`` params skip with a named bound where the host does not alias
    the two spellings.
    """

    root = tmp_path / "source"
    member = root / "review"
    member.mkdir(parents=True)
    (member / "SKILL.md").write_text(
        "---\nname: review\ndescription: Review\n---\n", encoding="utf-8"
    )
    home = tmp_path / alias
    if not _probes_same_file(home, root):
        pytest.skip(
            "case-insensitive filesystem unavailable: "
            "SOURCE/source home-alias bound (identity needs actual FS equivalence)"
        )
    assert home.samefile(root)
    monkeypatch.setenv("CSK_CONFIG", str(home / "config.json"))
    with pytest.raises(source_errors.SourceError) as excinfo:
        if entry == "collection":
            expand_collection(
                root,
                CollectionSelector(
                    from_alias="local",
                    directory=".",
                    include=("review",),
                    exclude=(),
                ),
            )
        else:
            resolve_individual(
                root,
                IndividualSelector(
                    name="review", from_alias="local", directory="review"
                ),
            )
    assert excinfo.value.code == source_errors.CODE_OUTPUT_OVERLAP


@pytest.mark.parametrize("entry", ["collection", "individual"])
@pytest.mark.parametrize("variant", ["nfc-exact", "nfd-variant"])
def test_home_unicode_equivalent_boundary_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, entry: str, variant: str
) -> None:
    """A Unicode-equivalent home spelling refuses like the identical one.

    Same shape as ``test_home_case_equivalent_boundary_refused`` with an
    NFC/NFD pair: ``CSK_CONFIG`` under the NFD spelling of the NFC home
    directory refuses ``source_output_overlap`` through BOTH entry points
    where the filesystem normalises the two spellings to one object. The
    ``nfd-variant`` params skip with a named bound elsewhere.
    """

    assert _BOUNDARY_UNICODE_NFC != _BOUNDARY_UNICODE_NFD
    root = tmp_path / _BOUNDARY_UNICODE_NFC
    member = root / "review"
    _write_skill(member, "review", "Review skill")
    home = tmp_path / (
        _BOUNDARY_UNICODE_NFC if variant == "nfc-exact" else _BOUNDARY_UNICODE_NFD
    )
    if not _probes_same_file(home, root):
        pytest.skip(
            "unicode-normalising filesystem unavailable: "
            "NFC/NFD home-alias bound (identity needs actual FS equivalence)"
        )
    assert home.samefile(root)
    monkeypatch.setenv("CSK_CONFIG", str(home / "config.json"))
    with pytest.raises(source_errors.SourceError) as excinfo:
        if entry == "collection":
            expand_collection(root, _collection(".", ("review",), ()))
        else:
            resolve_individual(
                root,
                IndividualSelector(
                    name="review", from_alias="local", directory="review"
                ),
            )
    assert excinfo.value.code == source_errors.CODE_OUTPUT_OVERLAP


def test_home_case_variant_wildcard_pruned(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Wildcard discovery prunes a case-variant csk home, keeping the survivor.

    The home is ``source/sub`` reached via ``SOURCE/SUB`` config spelling:
    ``"*"`` prunes ``sub`` (itself inside the home, no SKILL.md needed
    since pruning precedes validation) and returns the outside-home
    survivor, proving the wildcard path uses the same identity predicate
    as the explicit paths. Skips with a named bound on case-sensitive
    hosts.
    """

    root = tmp_path / "source"
    (root / "sub").mkdir(parents=True)
    _write_skill(root / "ok", "ok", "Survivor skill")
    probe = tmp_path / "SOURCE" / "SUB"
    if not _probes_same_file(probe, root / "sub"):
        pytest.skip(
            "case-insensitive filesystem unavailable: "
            "SOURCE/SUB home-alias bound (identity needs actual FS equivalence)"
        )
    monkeypatch.setenv("CSK_CONFIG", str(probe / "config.json"))
    members = expand_collection(root, _collection(".", ("*",), ()))
    assert [member.name for member in members] == ["ok"]


@pytest.mark.parametrize("entry", ["individual", "literal", "wildcard"])
def test_existing_home_outside_package_accepted(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, entry: str
) -> None:
    """Authored controls: an existing home refuses nothing outside itself.

    The csk home exists (with a config file) in a separate tree; a valid
    package under an unrelated source root is accepted through every entry
    point, pinning that the identity predicate refuses only true
    containment, never mere coexistence with a home.
    """

    home = tmp_path / "home"
    home.mkdir(parents=True)
    (home / "config.json").write_text("{}", encoding="utf-8")
    monkeypatch.setenv("CSK_CONFIG", str(home / "config.json"))
    root = tmp_path / "source"
    _write_skill(root / "review", "review", "Review skill")
    if entry == "individual":
        selected = resolve_individual(
            root,
            IndividualSelector(
                name="review", from_alias="local", directory="review"
            ),
        )
        assert selected.name == "review"
    elif entry == "literal":
        members = expand_collection(root, _collection(".", ("review",), ()))
        assert [member.name for member in members] == ["review"]
    else:
        members = expand_collection(root, _collection(".", ("*",), ()))
        assert [member.name for member in members] == ["review"]


@pytest.mark.parametrize("entry", ["collection", "individual"])
def test_source_root_inside_csk_home_boundary_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, entry: str
) -> None:
    """Home containment also covers a source root nested below the home.

    The source-root descriptor is opened directly, so this regression proves
    the boundary check uses its physical ancestor identities rather than only
    the selector's opened child chain or path spelling.
    """

    home = tmp_path / "home"
    root = home / "source"
    _write_skill(root / "review", "review", "Review skill")
    monkeypatch.setenv("CSK_CONFIG", str(home / "config.json"))
    with pytest.raises(source_errors.SourceError) as excinfo:
        if entry == "collection":
            expand_collection(root, _collection(".", ("review",), ()))
        else:
            resolve_individual(
                root,
                IndividualSelector(
                    name="review", from_alias="local", directory="review"
                ),
            )
    assert excinfo.value.code == source_errors.CODE_OUTPUT_OVERLAP


@pytest.mark.parametrize("entry", ["collection", "individual"])
@pytest.mark.parametrize("managed_name", [".agents", ".git"])
@pytest.mark.parametrize("spelling", ["exact", "case-variant"])
def test_source_root_inside_managed_ancestor_refused(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    entry: str,
    managed_name: str,
    spelling: str,
) -> None:
    """A source root below a managed output is not selectable by spelling."""

    monkeypatch.setenv("CSK_CONFIG", str(tmp_path / "home" / "config.json"))
    root_name = managed_name if spelling == "exact" else managed_name.upper()
    managed_root = tmp_path / managed_name
    root = tmp_path / root_name / "workspace"
    if spelling == "case-variant" and not _probes_same_file(
        managed_root, tmp_path / root_name
    ):
        pytest.skip(
            "case-insensitive filesystem unavailable: "
            f"{root_name}/{managed_name} managed-ancestor bound"
        )
    _write_skill(root / "review", "review", "Review skill")
    with pytest.raises(source_errors.SourceError) as excinfo:
        if entry == "collection":
            expand_collection(root, _collection(".", ("review",), ()))
        else:
            resolve_individual(
                root,
                IndividualSelector(
                    name="review", from_alias="local", directory="review"
                ),
            )
    assert excinfo.value.code == source_errors.CODE_OUTPUT_OVERLAP


@pytest.mark.parametrize(
    "root_kind",
    ["source-root", "csk-home", "adapter-agents", "git", "staging", "snapshot"],
)
@pytest.mark.parametrize(
    "spelling",
    ["exact", "case-variant", "unicode-variant", "symlink-alias", "hard-path"],
)
@pytest.mark.parametrize("entry", ["individual", "literal", "wildcard"])
def test_boundary_decision_table(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    root_kind: str,
    spelling: str,
    entry: str,
) -> None:
    """Boundary decision table: root kind x spelling x entry point.

    Every cell drives a production entry point and asserts its verdict:

    * ``source-root``: an escape reached with the root spelled the given
      way refuses ``source_selection_invalid`` through every entry point
      (wildcard carries a valid survivor to prove the whole operation
      fails, never partially publishes); each cell first accepts the
      inside survivor through the same spelling, pinning that containment
      holds (not merely refuses) through every spelling.
    * managed kinds (``csk-home``, ``adapter-agents``, ``git``,
      ``staging``, ``snapshot``): a package inside the managed location
      refuses ``source_output_overlap`` for explicit selections
      (``individual``, ``literal``). Wildcard prunes: ``csk-home``,
      ``adapter-agents`` and ``git`` assert the outside survivor list,
      while ``staging`` and ``snapshot`` (whose roots coincide with the
      home, so no survivor can exist beside the managed tree) assert the
      empty-set ``source_member_invalid`` over a VALID package -- without
      pruning the valid package would be accepted, so the refusal proves
      the prune.
    * ``staging``/``snapshot`` live under the csk home (representative
      ``staging/`` and ``source-v1/`` subdirs: csk keeps no separate
      staging/snapshot path constant outside the home, so any in-home
      path refuses through the one home-containment rule).

    ``case-variant``/``unicode-variant`` cells skip with a named platform
    bound where the host filesystem does not alias the spellings;
        ``hard-path`` spells the location with zero links remaining after the
        fixture's link is resolved, and asserts the linked form it was
        resolved from differs textually, so the cell is meaningful even where
        the temporary tree is already link-free.
    """

    base = tmp_path / "w"
    if root_kind == "source-root":
        if spelling == "unicode-variant":
            src = base / _BOUNDARY_UNICODE_NFC
            root_arg: Path = base / _BOUNDARY_UNICODE_NFD
        else:
            src = base / "source"
            root_arg = src
        outside = base / "outside" / "review"
        _write_skill(outside, "review", "Outside skill")
        src.mkdir(parents=True, exist_ok=True)
        (src / "evil").symlink_to(base / "outside", target_is_directory=True)
        _write_skill(src / "ok", "ok", "Survivor skill")
        monkeypatch.setenv(
            "CSK_CONFIG", str(tmp_path / "isolated-home" / "config.json")
        )
        if spelling == "case-variant":
            root_arg = base / "SOURCE"
            if not _probes_same_file(root_arg, src):
                pytest.skip(
                    "case-insensitive filesystem unavailable: "
                    "SOURCE/source root-alias bound"
                )
        elif spelling == "unicode-variant":
            if not _probes_same_file(root_arg, src):
                pytest.skip(
                    "unicode-normalising filesystem unavailable: "
                    "NFC/NFD root-alias bound"
                )
        elif spelling == "symlink-alias":
            linked = base / "rlink"
            linked.symlink_to(src, target_is_directory=True)
            root_arg = linked
        elif spelling == "hard-path":
            linked = base / "rlink"
            linked.symlink_to(src, target_is_directory=True)
            root_arg = linked.resolve()
            assert root_arg != linked
        control = resolve_individual(
            root_arg,
            IndividualSelector(name="ok", from_alias="local", directory="ok"),
        )
        assert control.name == "ok"
        with pytest.raises(source_errors.SourceError) as excinfo:
            if entry == "individual":
                resolve_individual(
                    root_arg,
                    IndividualSelector(
                        name="review", from_alias="local", directory="evil"
                    ),
                )
            elif entry == "literal":
                expand_collection(root_arg, _collection(".", ("evil",), ()))
            else:
                expand_collection(root_arg, _collection(".", ("*",), ()))
        assert excinfo.value.code == source_errors.CODE_SELECTION_INVALID
        return

    if root_kind == "csk-home":
        src = base / "source"
        home_name = (
            "s\u00fcb" if spelling == "unicode-variant" else "sub"
        )
        home_dir = src / home_name
        home_dir.mkdir(parents=True)
        _write_skill(src / "ok", "ok", "Survivor skill")
        config_spelling = home_dir
        if spelling == "case-variant":
            config_spelling = src / "SUB"
            if not _probes_same_file(config_spelling, home_dir):
                pytest.skip(
                    "case-insensitive filesystem unavailable: "
                    "SUB/sub home-alias bound"
                )
        elif spelling == "unicode-variant":
            config_spelling = src / unicodedata.normalize("NFD", home_name)
            if not _probes_same_file(config_spelling, home_dir):
                pytest.skip(
                    "unicode-normalising filesystem unavailable: "
                    "NFC/NFD home-alias bound"
                )
        elif spelling == "symlink-alias":
            linked_home = base / "hlink"
            linked_home.symlink_to(home_dir, target_is_directory=True)
            config_spelling = linked_home
        elif spelling == "hard-path":
            linked_home = base / "hlink"
            linked_home.symlink_to(home_dir, target_is_directory=True)
            config_spelling = linked_home.resolve()
            assert config_spelling != linked_home
        monkeypatch.setenv("CSK_CONFIG", str(config_spelling / "config.json"))
        if entry == "wildcard":
            members = expand_collection(src, _collection(".", ("*",), ()))
            assert [member.name for member in members] == ["ok"]
            return
        with pytest.raises(source_errors.SourceError) as excinfo:
            if entry == "individual":
                resolve_individual(
                    src,
                    IndividualSelector(
                        name="review", from_alias="local", directory=home_name
                    ),
                )
            else:
                expand_collection(src, _collection(".", (home_name,), ()))
        assert excinfo.value.code == source_errors.CODE_OUTPUT_OVERLAP
        return

    if root_kind in ("adapter-agents", "git"):
        managed_exact = ".agents" if root_kind == "adapter-agents" else ".git"
        src = base / "source"
        if spelling == "unicode-variant":
            src = base / _BOUNDARY_UNICODE_NFC
        member = managed_exact
        if spelling == "case-variant":
            # On-disk uppercase: discovery yields the alias spelling itself.
            member = managed_exact.upper()
        nested = src / member / "x"
        _write_skill(nested, "inner", "Managed skill")
        _write_skill(src / "ok", "ok", "Survivor skill")
        monkeypatch.setenv(
            "CSK_CONFIG", str(tmp_path / "isolated-home" / "config.json")
        )
        root_arg = src
        if spelling == "case-variant":
            if not _probes_same_file(src / managed_exact, src / member):
                pytest.skip(
                    "case-insensitive filesystem unavailable: "
                    f"{member}/{managed_exact} managed-alias bound"
                )
        elif spelling == "unicode-variant":
            root_arg = base / _BOUNDARY_UNICODE_NFD
            if not _probes_same_file(root_arg, src):
                pytest.skip(
                    "unicode-normalising filesystem unavailable: "
                    "NFC/NFD root-alias bound"
                )
        elif spelling == "symlink-alias":
            linked_member = src / "mlink"
            linked_member.symlink_to(nested, target_is_directory=True)
            member = "mlink"
        elif spelling == "hard-path":
            linked_root = base / "rlink"
            linked_root.symlink_to(src, target_is_directory=True)
            root_arg = linked_root.resolve()
            assert root_arg != linked_root
        if entry == "wildcard":
            members = expand_collection(root_arg, _collection(".", ("*",), ()))
            assert [member.name for member in members] == ["ok"]
            return
        with pytest.raises(source_errors.SourceError) as excinfo:
            if entry == "individual":
                resolve_individual(
                    root_arg,
                    IndividualSelector(
                        name="review", from_alias="local", directory=member
                    ),
                )
            else:
                expand_collection(root_arg, _collection(".", (member,), ()))
        assert excinfo.value.code == source_errors.CODE_OUTPUT_OVERLAP
        return

    # staging / snapshot: the package sits under the csk home itself, so the
    # selectable root and the home share one path (spelled the given way).
    assert root_kind in ("staging", "snapshot")
    subdir = "staging" if root_kind == "staging" else "source-v1"
    home = base / "home"
    if spelling == "unicode-variant":
        home = base / "hom\u00e9"
    package = home / subdir / "pkg"
    _write_skill(package, "review", "In-home skill")
    home_arg = home
    if spelling == "case-variant":
        home_arg = base / "HOME"
        if not _probes_same_file(home_arg, home):
            pytest.skip(
                "case-insensitive filesystem unavailable: "
                "HOME/home alias bound"
            )
    elif spelling == "unicode-variant":
        home_arg = base / unicodedata.normalize("NFD", home.name)
        if not _probes_same_file(home_arg, home):
            pytest.skip(
                "unicode-normalising filesystem unavailable: "
                "NFC/NFD home-alias bound"
            )
    elif spelling == "symlink-alias":
        linked_home = base / "alink"
        linked_home.symlink_to(home, target_is_directory=True)
        home_arg = linked_home
    elif spelling == "hard-path":
        linked_home = base / "alink"
        linked_home.symlink_to(home, target_is_directory=True)
        home_arg = linked_home.resolve()
        assert home_arg != linked_home
    monkeypatch.setenv("CSK_CONFIG", str(home_arg / "config.json"))
    selector_dir = f"{subdir}/pkg"
    if entry == "wildcard":
        with pytest.raises(source_errors.SourceError) as excinfo:
            expand_collection(home_arg, _collection(subdir, ("*",), ()))
        assert excinfo.value.code == source_errors.CODE_MEMBER_INVALID
        assert "expands to an empty skill set" in excinfo.value.detail
        return
    with pytest.raises(source_errors.SourceError) as excinfo:
        if entry == "individual":
            resolve_individual(
                home_arg,
                IndividualSelector(
                    name="review", from_alias="local", directory=selector_dir
                ),
            )
        else:
            expand_collection(home_arg, _collection(subdir, ("pkg",), ()))
    assert excinfo.value.code == source_errors.CODE_OUTPUT_OVERLAP
