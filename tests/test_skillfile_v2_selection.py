"""Deterministic skill-collection expansion (draft skillfile-sources-v1, opt-in).

Every test drives the production entry points in
:csk.sources.selection: (``resolve_selector_directory``,
``expand_collection``, ``resolve_individual``, ``expand_selectors``,
``validate_member_package`` / ``read_skill_md_name``) on filesystem fixtures.
Filesystem-boundary mechanics (read confinement, traversal, managed-output
identity, fail-closed probes) are owned by the access layer and live in
test_selection_boundary_property.py; this file keeps selection semantics
plus one representative fixture per error-code mapping branch.
"""

from __future__ import annotations

import json
import unicodedata
from pathlib import Path

import pytest
import yaml

from csk import skillspec
from csk.sources import _selection_fs
from csk.sources import boundaries
from csk.sources import errors as source_errors
from csk.sources import repository_policy
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


@pytest.fixture(autouse=True)
def _require_descriptor_traversal(request: pytest.FixtureRequest) -> None:
    """Skip traversal-bound selection tests where the runtime lacks it.

    Every test in this module except those marked
    ``posix_traversal_independent`` drives the descriptor-confined
    selection entries. On a runtime without descriptor-relative traversal
    schema-2 selection refuses when neither descriptor backend is available,
    so those tests skip with the
    single named reason instead of failing. Pure frontmatter/parser probes
    and pure argument-validation checks (which raise before the capability
    gate) carry the marker and run everywhere.
    """

    if request.node.get_closest_marker("posix_traversal_independent"):
        return
    if not _selection_fs.supports_descriptor_traversal():
        pytest.skip(_selection_fs.NO_DESCRIPTOR_TRAVERSAL_REASON)


# ---------------------------------------------------------------------------
# AC (a): selector directories, symlinks before containment, escape refusal
# ---------------------------------------------------------------------------


def test_selector_dot_selects_source_root(tmp_path: Path) -> None:
    _write_skill(tmp_path / "src", "review")
    resolved = resolve_selector_directory(tmp_path / "src", ".")
    assert resolved == (tmp_path / "src").resolve()
    # A root package requires the operator allowlist on the live path
    # (BUG-260922-1o40hs): without it the selection below refuses
    # ``source_output_overlap`` instead of succeeding.
    policy = repository_policy.RepositoryPolicy(
        schema_version=1, repositories={}, root_inputs={"local": ("SKILL.md",)}
    )
    home = tmp_path / "home"
    home.mkdir()
    individual = resolve_individual(
        tmp_path / "src",
        IndividualSelector(name="review", from_alias="local", directory="."),
        policy=policy,
        root_inputs_gate=boundaries.root_inputs_gate(policy, home),
    )
    assert isinstance(individual, SelectedSkill)
    assert (individual.name, individual.directory) == ("review", ".")


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


@pytest.mark.posix_traversal_independent  # pure grammar check precedes the gate
def test_selector_nonportable_directory_fails(tmp_path: Path) -> None:
    root = tmp_path / "src"
    root.mkdir()
    with pytest.raises(source_errors.SourceError) as excinfo:
        resolve_selector_directory(root, "a/../b")
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
    """Byte order differs from case-insensitive order on these four names.

    UTF-8 bytes: ``Banana`` (0x42..) < ``Zebra`` (0x5A..) < ``apple`` (0x61..)
    < ``Ωmega`` (0xCE..). Case-insensitive: ``apple`` < ``Banana`` <
    ``Zebra`` < ``ωmega``. The upper/lower split and the non-ASCII name pin
    that neither case folding nor locale collation decides the order: the key
    is the raw UTF-8 byte string. (A same-word case pair would collide on
    case-insensitive filesystems, so portability forbids it here.) Installed
    names are deliberately skewed so folder order cannot be confused with
    name order.
    """

    root = tmp_path / "src"
    folders = ["Zebra", "apple", "Banana", "Ωmega"]
    names = ["aaa-first", "zzz-last", "mmm-middle", "nnn-fifth"]
    for folder, name in zip(folders, names, strict=True):
        _write_skill(root / "col" / folder, name)
    members = expand_collection(root, _collection("col", ("*",)))
    assert [member.folder for member in members] == [
        "Banana",
        "Zebra",
        "apple",
        "Ωmega",
    ]
    assert [member.name for member in members] == [
        "mmm-middle",
        "aaa-first",
        "zzz-last",
        "nnn-fifth",
    ]
    byte_sorted = sorted(folders, key=lambda value: value.encode("utf-8"))
    folded_sorted = sorted(folders, key=lambda value: value.casefold())
    assert byte_sorted != folded_sorted
    assert [member.folder for member in members] == byte_sorted


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


def _requirement_manifest(git: str, ref_value: str = "v1", name: str = "helper") -> str:
    """One schema-4 manifest declaring a single skill requirement identity."""

    return json.dumps(
        {
            "schema_version": 4,
            "capabilities": {"exec": "none", "network": "none"},
            "dependencies": {
                "skills": {name: {"git": git, "ref": {"kind": "tag", "value": ref_value}}}
            },
        }
    )


def test_conflicting_requirement_identities_fail_name_conflict(tmp_path: Path) -> None:
    """Same requirement name with different repository identities refuses (AC f)."""

    root = tmp_path / "src"
    _write_skill(
        root / "col" / "a",
        "alpha-skill",
        manifest=_requirement_manifest("https://git.example.test/team/db-a"),
    )
    _write_skill(
        root / "col" / "b",
        "beta-skill",
        manifest=_requirement_manifest("https://git.example.test/team/db-b"),
    )
    with pytest.raises(source_errors.SourceError) as excinfo:
        expand_collection(root, _collection("col", ("a", "b")))
    assert excinfo.value.code == source_errors.CODE_NAME_CONFLICT
    assert "helper" in excinfo.value.detail


def test_conflicting_requirement_identities_fail_across_selectors(tmp_path: Path) -> None:
    """Requirement-identity conflicts refuse across the whole selection set."""

    root = tmp_path / "src"
    _write_skill(
        root / "one",
        "alpha-skill",
        manifest=_requirement_manifest("https://git.example.test/team/db-a"),
    )
    _write_skill(
        root / "two",
        "beta-skill",
        manifest=_requirement_manifest("https://git.example.test/team/db-b"),
    )
    selectors = [
        IndividualSelector(name="alpha-skill", from_alias="local", directory="one"),
        IndividualSelector(name="beta-skill", from_alias="local", directory="two"),
    ]
    with pytest.raises(source_errors.SourceError) as excinfo:
        expand_selectors(selectors, {"local": root})
    assert excinfo.value.code == source_errors.CODE_NAME_CONFLICT
    assert "helper" in excinfo.value.detail


def test_identical_requirement_identities_unify(tmp_path: Path) -> None:
    """Identical transitive requirements unify instead of conflicting."""

    root = tmp_path / "src"
    manifest = _requirement_manifest("https://git.example.test/team/db")
    _write_skill(root / "col" / "a", "alpha-skill", manifest=manifest)
    _write_skill(root / "col" / "b", "beta-skill", manifest=manifest)
    members = expand_collection(root, _collection("col", ("a", "b")))
    assert [member.name for member in members] == ["alpha-skill", "beta-skill"]


def test_same_repo_different_ref_accepted_for_closure(tmp_path: Path) -> None:
    """Same repository with different ref spellings defers to closure validation.

    A tag and a revision may resolve to one commit, and only the closure (with
    repository access at publication) can decide. Selection refuses solely on
    provably different repository identities.
    """

    root = tmp_path / "src"
    _write_skill(
        root / "col" / "a",
        "alpha-skill",
        manifest=_requirement_manifest("https://git.example.test/team/db", "v1"),
    )
    _write_skill(
        root / "col" / "b",
        "beta-skill",
        manifest=_requirement_manifest("https://git.example.test/team/db", "v2"),
    )
    members = expand_collection(root, _collection("col", ("a", "b")))
    assert [member.name for member in members] == ["alpha-skill", "beta-skill"]


def test_same_repo_spelling_variants_do_not_conflict(tmp_path: Path) -> None:
    """One repository under two URL spellings is one identity, not a conflict."""

    root = tmp_path / "src"
    _write_skill(
        root / "col" / "a",
        "alpha-skill",
        manifest=_requirement_manifest("https://git.example.test/team/db"),
    )
    _write_skill(
        root / "col" / "b",
        "beta-skill",
        manifest=_requirement_manifest("https://git.example.test/team/db.git"),
    )
    members = expand_collection(root, _collection("col", ("a", "b")))
    assert [member.name for member in members] == ["alpha-skill", "beta-skill"]


def test_local_requirement_paths_defer_to_closure(tmp_path: Path) -> None:
    """Local-path requirements carry no network identity and never conflict here."""

    root = tmp_path / "src"
    _write_skill(
        root / "col" / "a",
        "alpha-skill",
        manifest=_requirement_manifest("./vendor/db-a"),
    )
    _write_skill(
        root / "col" / "b",
        "beta-skill",
        manifest=_requirement_manifest("./vendor/db-b"),
    )
    members = expand_collection(root, _collection("col", ("a", "b")))
    assert [member.name for member in members] == ["alpha-skill", "beta-skill"]


def test_malformed_requirement_source_defers_without_unstructured_failure(
    tmp_path: Path,
) -> None:
    """An undecidable requirement identity defers; no raw exception escapes."""

    root = tmp_path / "src"
    _write_skill(
        root / "col" / "a",
        "alpha-skill",
        manifest=_requirement_manifest("https://git.example.test/team/db?x=1"),
    )
    _write_skill(
        root / "col" / "b",
        "beta-skill",
        manifest=_requirement_manifest("https://git.example.test/team/other"),
    )
    members = expand_collection(root, _collection("col", ("a", "b")))
    assert [member.name for member in members] == ["alpha-skill", "beta-skill"]


def _write_named_manifest(directory: Path, manifest: str, filename: str) -> None:
    """Write a skill manifest under an explicit canonical/legacy filename."""

    directory.mkdir(parents=True, exist_ok=True)
    (directory / filename).write_text(manifest, encoding="utf-8")


@pytest.mark.parametrize(
    "manifest_a,manifest_b",
    [
        (skillspec.CANONICAL_MANIFEST, skillspec.CANONICAL_MANIFEST),
        (skillspec.LEGACY_MANIFEST, skillspec.LEGACY_MANIFEST),
        (skillspec.CANONICAL_MANIFEST, skillspec.LEGACY_MANIFEST),
        (skillspec.LEGACY_MANIFEST, skillspec.CANONICAL_MANIFEST),
    ],
    ids=["canonical-canonical", "legacy-legacy", "canonical-legacy", "legacy-canonical"],
)
@pytest.mark.parametrize("entry", ["collection", "selectors"])
def test_conflicting_requirement_identities_fail_across_manifest_kinds(
    tmp_path: Path, manifest_a: str, manifest_b: str, entry: str
) -> None:
    """Requirement-identity conflicts refuse in every manifest-kind pairing."""

    root = tmp_path / "src"
    first = _write_skill(root / "col" / "a", "alpha-skill")
    _write_named_manifest(
        first,
        _requirement_manifest("https://git.example.test/team/db-a"),
        manifest_a,
    )
    second = _write_skill(root / "col" / "b", "beta-skill")
    _write_named_manifest(
        second,
        _requirement_manifest("https://git.example.test/team/db-b"),
        manifest_b,
    )
    with pytest.raises(source_errors.SourceError) as excinfo:
        if entry == "collection":
            expand_collection(root, _collection("col", ("a", "b")))
        else:
            expand_selectors(
                [
                    IndividualSelector(
                        name="alpha-skill", from_alias="local", directory="col/a"
                    ),
                    IndividualSelector(
                        name="beta-skill", from_alias="local", directory="col/b"
                    ),
                ],
                {"local": root},
            )
    assert excinfo.value.code == source_errors.CODE_NAME_CONFLICT
    assert "helper" in excinfo.value.detail


@pytest.mark.parametrize("entry", ["collection", "selectors"])
def test_conflicting_requirement_chain_fails_across_entries(
    tmp_path: Path, entry: str
) -> None:
    """A three-member chain with one divergent identity refuses (AC f)."""

    root = tmp_path / "src"
    shared = _requirement_manifest("https://git.example.test/team/db-a")
    _write_skill(root / "col" / "a", "alpha-skill", manifest=shared)
    _write_skill(root / "col" / "b", "beta-skill", manifest=shared)
    _write_skill(
        root / "col" / "c",
        "gamma-skill",
        manifest=_requirement_manifest("https://git.example.test/team/db-b"),
    )
    with pytest.raises(source_errors.SourceError) as excinfo:
        if entry == "collection":
            expand_collection(root, _collection("col", ("a", "b", "c")))
        else:
            expand_selectors(
                [
                    IndividualSelector(
                        name=name, from_alias="local", directory=f"col/{folder}"
                    )
                    for folder, name in (
                        ("a", "alpha-skill"),
                        ("b", "beta-skill"),
                        ("c", "gamma-skill"),
                    )
                ],
                {"local": root},
            )
    assert excinfo.value.code == source_errors.CODE_NAME_CONFLICT
    assert "helper" in excinfo.value.detail


_HOSTILE_REQUIREMENT_GIT_STRINGS = (
    "https://example.com/org/\x00.git",
    "\x00",
    "\x01\x02bad",
    "https://example.com:badport/org/x.git",
    "https://user:pass@example.com/org/x.git",
    "https://example.com/org/x.git?query=1",
    "https://example.com/org/x.git#frag",
    "https://example.com/org/x%20space.git",
    "https://example.com/org/x\\back.git",
    "HTTPS://EXAMPLE.COM/org/x.git",
    "  https://example.com/org/x.git  ",
    "git@example.com:org/x.git\n",
    "C:\\vendor\\helper",
    "https://ex ample.com/org/x.git",
    "https://example.com/" + "a" * 5000 + ".git",
    "ftp://example.com/org/x.git",
    "file:///tmp/helper",
    "~/helper",
    "org/x.git\u00a0",
    "https://example.com/org/x.git\u200b",
    "\u202ehttps://example.com/gro/x.git",
)


@pytest.mark.parametrize("git", _HOSTILE_REQUIREMENT_GIT_STRINGS)
@pytest.mark.parametrize("entry", ["collection", "individual"])
def test_hostile_requirement_git_never_leaks_raw(
    tmp_path: Path, git: str, entry: str
) -> None:
    """Hostile git strings: structured SourceError or acceptance, never raw."""

    root = tmp_path / "src"
    member = _write_skill(root / "pkg", "review")
    payload = json.loads(_requirement_manifest("https://example.com/org/placeholder"))
    payload["dependencies"]["skills"]["helper"]["git"] = git
    _write_named_manifest(member, json.dumps(payload), skillspec.CANONICAL_MANIFEST)
    try:
        if entry == "collection":
            expand_collection(root, _collection(".", ("pkg",)))
        else:
            resolve_individual(
                root,
                IndividualSelector(name="review", from_alias="local", directory="pkg"),
            )
    except source_errors.SourceError as exc:
        assert exc.code in {
            source_errors.CODE_MEMBER_INVALID,
            source_errors.CODE_NAME_CONFLICT,
            source_errors.CODE_SELECTION_INVALID,
        }, exc.code
    except Exception as exc:  # noqa: BLE001 - the assertion IS no-raw-leak
        pytest.fail(f"raw {type(exc).__name__} escaped for git={git!r}: {exc}")


_MALFORMED_REQUIREMENT_PAYLOADS = (
    {
        "name": "review",
        "dependencies": {
            "skills": {
                "helper": {"git": 42, "ref": {"kind": "branch", "value": "x"}}
            }
        },
    },
    {
        "name": "review",
        "dependencies": {
            "skills": {"helper": {"ref": {"kind": "branch", "value": "x"}}}
        },
    },
    {
        "name": "review",
        "dependencies": {"skills": {"helper": {"git": "https://e.com/o/h.git"}}},
    },
    {
        "name": "review",
        "dependencies": {
            "skills": {
                "helper": {"git": "https://e.com/o/h.git", "ref": "main"}
            }
        },
    },
    {
        "name": "review",
        "dependencies": {
            "skills": {
                "helper": {"git": "", "ref": {"kind": "b", "value": "v"}}
            }
        },
    },
    {
        "name": "review",
        "dependencies": {
            "skills": {
                "": {"git": "https://e.com/o/h.git", "ref": {"kind": "b", "value": "v"}}
            }
        },
    },
    {
        "name": "review",
        "dependencies": {
            "skills": {
                "helper": {
                    "git": "https://e.com/o/h.git",
                    "ref": {"kind": "b", "value": "v"},
                    "mode": 7,
                }
            }
        },
    },
    {
        "name": "review",
        "dependencies": {
            "skills": {
                "helper": {
                    "git": "https://e.com/o/h.git",
                    "ref": {"kind": "b", "value": "v"},
                    "commands": ["ok", 7],
                }
            }
        },
    },
    {"name": "review", "dependencies": {"skills": ["not-a-dict"]}},
    {"name": "review", "dependencies": ["not-a-dict-either"]},
    {"name": "review"},
)


@pytest.mark.parametrize("payload", _MALFORMED_REQUIREMENT_PAYLOADS)
@pytest.mark.parametrize("entry", ["collection", "individual"])
def test_malformed_requirement_shapes_structured(
    tmp_path: Path, payload: dict, entry: str
) -> None:
    """Malformed dependency shapes: structured SourceError or acceptance."""

    root = tmp_path / "src"
    member = _write_skill(root / "pkg", "review")
    _write_named_manifest(
        member, json.dumps(payload), skillspec.CANONICAL_MANIFEST
    )
    try:
        if entry == "collection":
            expand_collection(root, _collection(".", ("pkg",)))
        else:
            resolve_individual(
                root,
                IndividualSelector(name="review", from_alias="local", directory="pkg"),
            )
    except source_errors.SourceError:
        pass
    except Exception as exc:  # noqa: BLE001 - the assertion IS no-raw-leak
        pytest.fail(f"raw {type(exc).__name__} escaped for {payload!r}: {exc}")


@pytest.mark.parametrize("entry", ["collection", "individual"])
def test_requirement_dual_manifest_must_agree(tmp_path: Path, entry: str) -> None:
    """One member whose canonical and legacy manifests disagree is invalid."""

    root = tmp_path / "src"
    member = _write_skill(root / "col" / "a", "alpha-skill")
    _write_named_manifest(
        member,
        _requirement_manifest("https://git.example.test/team/db-a"),
        skillspec.CANONICAL_MANIFEST,
    )
    _write_named_manifest(
        member,
        _requirement_manifest("https://git.example.test/team/db-b"),
        skillspec.LEGACY_MANIFEST,
    )
    with pytest.raises(source_errors.SourceError) as excinfo:
        if entry == "collection":
            expand_collection(root, _collection("col", ("a",)))
        else:
            resolve_individual(
                root,
                IndividualSelector(
                    name="alpha-skill", from_alias="local", directory="col/a"
                ),
            )
    assert excinfo.value.code == source_errors.CODE_MEMBER_INVALID


@pytest.mark.parametrize("entry", ["collection", "individual"])
def test_deeply_nested_manifest_is_structured(tmp_path: Path, entry: str) -> None:
    """A pathologically nested manifest refuses structured, never raw."""

    root = tmp_path / "src"
    member = _write_skill(root / "pkg", "review")
    depth = 10000
    text = '{"name": "review", "deep": ' + '{"wrap": ' * depth + '1' + '}' * depth + '}'
    (member / skillspec.CANONICAL_MANIFEST).write_text(text, encoding="utf-8")
    with pytest.raises(source_errors.SourceError) as excinfo:
        if entry == "collection":
            expand_collection(root, _collection(".", ("pkg",)))
        else:
            resolve_individual(
                root,
                IndividualSelector(name="review", from_alias="local", directory="pkg"),
            )
    assert excinfo.value.code == source_errors.CODE_MEMBER_INVALID


@pytest.mark.parametrize("entry", ["collection", "individual"])
def test_duplicate_json_keys_structured(tmp_path: Path, entry: str) -> None:
    """Duplicate JSON keys: structured SourceError or acceptance, never raw."""

    root = tmp_path / "src"
    member = _write_skill(root / "pkg", "review")
    (member / skillspec.CANONICAL_MANIFEST).write_text(
        '{"name": "review", "name": "review", "x": 1}', encoding="utf-8"
    )
    try:
        if entry == "collection":
            expand_collection(root, _collection(".", ("pkg",)))
        else:
            resolve_individual(
                root,
                IndividualSelector(name="review", from_alias="local", directory="pkg"),
            )
    except source_errors.SourceError as exc:
        assert exc.code == source_errors.CODE_MEMBER_INVALID
    except Exception as exc:  # noqa: BLE001 - the assertion IS no-raw-leak
        pytest.fail(f"raw {type(exc).__name__} escaped: {exc}")


def test_reserved_names_conflict_with_expansion(tmp_path: Path) -> None:
    root = tmp_path / "src"
    _write_skill(root / "col" / "review", "review")
    with pytest.raises(source_errors.SourceError) as excinfo:
        expand_selectors(
            [_collection("col", ("review",))], {"local": root}, reserved_names=("review",)
        )
    assert excinfo.value.code == source_errors.CODE_NAME_CONFLICT


@pytest.mark.posix_traversal_independent  # pure alias check precedes the gate
def test_expand_selectors_missing_root_fails_selection_invalid(tmp_path: Path) -> None:
    root = tmp_path / "src"
    root.mkdir()
    with pytest.raises(source_errors.SourceError) as excinfo:
        expand_selectors([_collection(".", ("*",))], {})
    assert excinfo.value.code == source_errors.CODE_SELECTION_INVALID


# ---------------------------------------------------------------------------
# Authored-input controls: near-miss names are never pruned
# ---------------------------------------------------------------------------


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
# Authored inputs are never managed (selection control)
# ---------------------------------------------------------------------------


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


@pytest.mark.posix_traversal_independent  # direct tokenizer probe, no filesystem
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


@pytest.mark.posix_traversal_independent  # direct parser probe, no filesystem
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
@pytest.mark.posix_traversal_independent  # direct parser probe, no filesystem
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
@pytest.mark.posix_traversal_independent  # direct parser probe, no filesystem
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
@pytest.mark.posix_traversal_independent  # direct parser probe, no filesystem
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
@pytest.mark.posix_traversal_independent  # direct parser probe, no filesystem
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
# S-ERRORS: injected filesystem failures refuse structured (no raw leaks)
# ---------------------------------------------------------------------------


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
