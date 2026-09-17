"""Deterministic skill-collection expansion (draft skillfile-sources-v1, opt-in).

This module implements protocol skillfile-sources section 1 selection against
a resolved source root: a local directory for ``path`` sources, or the
checked-out snapshot root for Git sources (supplied by later stories). Only
filesystem enumeration and package validation live here; snapshots, locks,
policy and transport belong to later leaves.

Entry points:

* :func:`resolve_selector_directory` (AC a): ``"."`` selects the source root,
  other directories are portable contained paths resolved with symlinks before
  containment. An escape fails ``source_selection_invalid``.
* :func:`expand_collection` (AC b, c, e): immediate child directories only,
  include literals checked before exclusions, ``"*"`` after pruning, SKILL.md
  frontmatter plus ordinary package/manifest validation of every remaining
  member, ascending UTF-8 folder-name byte order.
* :func:`resolve_individual` (AC d): one directory must carry the selector's
  name in validated SKILL.md metadata (and in the skill manifest identity
  where that manifest declares one).
* :func:`expand_selectors` (AC f): whole-set validation with
  ``source_name_conflict`` for repeated, filesystem-equivalent, or
  conflicting-identity installed names before any publication.
"""

from __future__ import annotations

import re
import unicodedata
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Final, TypeVar

from .. import (
    adapters,
    config,
    identifiers,
    protocol_json,
    skillcheck,
    skillspec,
    source_identity,
)
from ._selection_fs import (
    Directory,
    MemberSnapshot,
    PreflightPath,
    PreflightRequest,
    SelectionSession,
    _snapshot_path,
    read_optional_regular_path,
    read_regular_path,
)
from .errors import (
    CODE_MEMBER_INVALID,
    CODE_MEMBER_MISSING,
    CODE_NAME_CONFLICT,
    CODE_OUTPUT_OVERLAP,
    CODE_SELECTION_INVALID,
    SourceError,
)
from .skillfile_v2 import (
    CollectionSelector,
    IndividualSelector,
    SkillSelector,
    is_valid_selector_directory,
)

def _managed_output_names() -> frozenset[str]:
    """Return the managed-output directory names csk itself writes.

    The single source is ``csk.adapters`` (every adapter root first component
    plus the native-discovery ``.agents`` root) with ``.git`` metadata added;
    this module keeps no second copy of the adapter list. The source snapshot
    store, transaction staging, runtime stores and build caches live under the
    csk home, so they are covered by the csk-home containment rule in
    :func:`managed_output_boundary` rather than by extra names here.
    """
    names = {".git"}
    for relative in (
        *adapters.AGENT_PATHS.values(),
        adapters.NATIVE_DISCOVERY_HOME_PATH,
    ):
        first = relative.split("/", 1)[0]
        if first:
            names.add(first)
    return frozenset(names)


# Mandatory generated-output pruning for ``"*"`` discovery (protocol section 2).
PRUNED_CHILD_NAMES: Final[frozenset[str]] = _managed_output_names()

# Filesystem failures that must never leak as raw Python exceptions from the
# public selection entry points. Descriptor operations in ``_selection_fs``
# catch the same tuple; the reader boundary below covers the downstream
# validators as well.
_FS_ERRORS: Final[tuple[type[Exception], ...]] = (
    OSError,
    RuntimeError,
    ValueError,
    UnicodeError,
)

_T = TypeVar("_T")


def _run_member_reader(label: str, member_path: Path, reader: Callable[[], _T]) -> _T:
    """Run one downstream member reader inside the structured-failure boundary.

    Every downstream invocation (SKILL.md frontmatter, skill manifest loading,
    ``skillcheck.validate_skill`` with its skillspec/locale/markdown reads)
    runs through this single boundary: filesystem failures (``OSError``
    including ``PermissionError``, ``RuntimeError`` from symlink loops,
    ``ValueError`` from embedded NUL, ``UnicodeError``) become the structured
    ``SourceError(source_member_invalid)`` naming the member path, with the
    original exception chained as ``__cause__``. A structured ``SourceError``
    passes through unchanged.
    """
    try:
        return reader()
    except SourceError:
        raise
    except _FS_ERRORS as exc:
        raise SourceError(
            CODE_MEMBER_INVALID,
            f"Skill member {label} cannot be inspected: {member_path}: {exc}",
        ) from exc


def managed_output_boundary(
    package: Directory,
    *,
    session: SelectionSession,
    project_root: Path | None = None,
    csk_home: Path | None = None,
) -> str | None:
    """One descriptor-identity predicate shared by every selection path.

    ``project_root`` and ``csk_home`` remain accepted for callers that used
    the old helper signature, but production callers pass the opened
    ``Directory`` and ``SelectionSession``.  No path spelling is consulted
    for containment or pruning.
    """

    _ = (project_root, csk_home)
    return session.managed_boundary(
        package,
        PRUNED_CHILD_NAMES,
        context=f"{package.display}: boundary undetermined",
    )

# YAML 1.2 core-schema tag resolution for plain scalars in the supported
# frontmatter subset. Classification is by REGEX ONLY: a match is a non-string
# and is refused for ``name``/``description``. There is deliberately no numeric
# conversion anywhere in the plain-scalar path, so recognition never depends
# on representability: a 5000-digit integer refuses exactly like ``42``.
_PLAIN_NULL_RE: Final = re.compile(r"^(?:null|Null|NULL|~)$")
_PLAIN_BOOL_RE: Final = re.compile(r"^(?:true|True|TRUE|false|False|FALSE)$")
_PLAIN_INT_RES: Final = (
    re.compile(r"^[-+]?[0-9]+$"),
    re.compile(r"^0o[0-7]+$"),
    re.compile(r"^0x[0-9a-fA-F]+$"),
)
_PLAIN_FLOAT_RES: Final = (
    re.compile(r"^[-+]?(?:\.[0-9]+|[0-9]+(?:\.[0-9]*)?)(?:[eE][-+]?[0-9]+)?$"),
    re.compile(r"^[-+]?(?:\.inf|\.Inf|\.INF)$"),
    re.compile(r"^(?:\.nan|\.NaN|\.NAN)$"),
)
# Value starters that are never supported string scalars in the subset:
# aliases/anchors/tags, block scalars, flow delimiters, directives and
# explicit non-plain forms. A plain scalar can never begin with one of these
# (YAML 1.2 ``ns-plain-first``); ``-``, ``?`` and ``:`` are conditional
# instead and are handled with their space/end-of-line rules below.
_UNSUPPORTED_VALUE_STARTS: Final = frozenset(
    {"&", "*", "!", "|", ">", "%", "@", "`", "{", "[", ",", "]", "}"}
)

# Top-level frontmatter key grammar: bare keys only. A top-level entry is a
# line at indent 0 matching ``key:`` followed by end of line or by a space/tab
# and a value; any other indent-0 line refuses.
_FRONTMATTER_KEY_RE: Final = re.compile(r"[A-Za-z0-9_.-]+")
# Block-scalar explicit indentation indicator values (YAML 1.2 section 8.1.1.1:
# a single digit ``1``-``9``). A lookup table, not a numeric conversion, so the
# scalar path keeps its no-conversion invariant (plain-scalar classification
# stays regex-only).
_INDENT_VALUES: Final = {
    "1": 1,
    "2": 2,
    "3": 3,
    "4": 4,
    "5": 5,
    "6": 6,
    "7": 7,
    "8": 8,
    "9": 9,
}

# Grammar character classes for the frontmatter parser (single definitions,
# used at every structural decision -- blank-line tests, indentation counting,
# separation before ``#``, header whitespace, trailing whitespace after quotes,
# flow-list emptiness, and the required-field gate):
#
# * white space is U+0020 SPACE and U+0009 TAB only (YAML 1.2 ``s-white``);
#   every other Unicode whitespace (the Zs category) is ordinary content;
# * line breaks are LF, with CRLF normalised to LF at line splitting; a lone
#   CR refuses. NEL (U+0085), LS (U+2028) and PS (U+2029) are ordinary
#   printable content characters (YAML 1.2 section 5.1 ``c-printable``):
#   never whitespace, never line breaks, never rewritten -- in every
#   position, including trailing in block scalars. Block-scalar rendering
#   knows exactly one line break (LF from the line split); chomping acts
#   only on trailing empty lines and on that final LF.
_S_WHITE: Final = (" ", "\t")


def _is_blank_line(raw: str) -> bool:
    """Return whether one physical line is blank under the grammar classes.

    Blank means empty or spaces/tabs only. A line of any other Unicode
    whitespace (NBSP, EM SPACE, ...) is a content line, never a blank line.
    """
    return raw.strip(" \t") == ""


# Double-quoted escape table for the supported frontmatter subset: the
# complete YAML 1.2.2 section 5.7 single-character escape alphabet
# (productions [42]-[62]), each mapping to its decoded value:
# ``\0 \a \b \t \n \v \f \r \e``, backslash+SPACE, ``\" \\ \/``,
# ``\N \_ \L \P``, and backslash followed by a literal TAB (production [53],
# same value as ``\t``). ``\x``/``\u``/``\U`` are decoded separately without
# any numeric-conversion call in the plain-scalar path; any other escape
# (including truncated or non-hex ``\x``/``\u``/``\U``) refuses.
_DOUBLE_QUOTED_ESCAPES: Final = {
    '"': '"',
    "\\": "\\",
    "/": "/",
    "n": "\n",
    "t": "\t",
    "\t": "\t",
    "r": "\r",
    "0": "\0",
    "a": "\a",
    "b": "\b",
    "v": "\v",
    "f": "\f",
    "e": "\x1b",
    "N": "\x85",
    "_": "\xa0",
    "L": "\u2028",
    "P": "\u2029",
    " ": " ",
}

_HEX_VALUES: Final = {
    "0": 0,
    "1": 1,
    "2": 2,
    "3": 3,
    "4": 4,
    "5": 5,
    "6": 6,
    "7": 7,
    "8": 8,
    "9": 9,
    "a": 10,
    "b": 11,
    "c": 12,
    "d": 13,
    "e": 14,
    "f": 15,
    "A": 10,
    "B": 11,
    "C": 12,
    "D": 13,
    "E": 14,
    "F": 15,
}


@dataclass(frozen=True)
class SelectedSkill:
    """One validated selection: installed name plus its package location."""

    name: str
    from_alias: str
    directory: str
    path: Path
    folder: str
    requirements: tuple[skillspec.SkillRequirement, ...] = ()


def _configured_csk_home() -> Path:
    try:
        return config.config_path().expanduser().parent
    except _FS_ERRORS as exc:
        raise SourceError(
            CODE_OUTPUT_OVERLAP,
            f"Cannot determine the csk home boundary: {exc}",
        ) from exc


def _selector_components(directory: str, *, context: str) -> list[str]:
    if directory == ".":
        return []
    if not is_valid_selector_directory(directory):
        raise SourceError(
            CODE_SELECTION_INVALID,
            f"{context}: {directory!r} must be '.' or a portable contained path",
        )
    # The selector grammar rejects ``..`` and all platform separators. Keep
    # this split deliberately boring: no lexical normalisation is performed.
    return directory.split("/")


def _open_session(
    source_root: Path,
    *,
    preflight: PreflightRequest = PreflightRequest(),
) -> SelectionSession:
    return SelectionSession.open(
        source_root,
        _configured_csk_home(),
        managed_names=PRUNED_CHILD_NAMES,
        preflight=preflight,
    )


def _resolve_in_session(
    session: SelectionSession,
    directory: str,
    *,
    code: str = CODE_SELECTION_INVALID,
    missing_code: str | None = None,
    context: str,
) -> tuple[Directory, bool]:
    components = _selector_components(directory, context=context)
    result = session.descend(
        session.root,
        components,
        code=code,
        missing_code=missing_code,
        context=context,
    )
    return result.directory, result.final_component_link


def _member_path(directory: Directory) -> Path:
    return directory.display


def _validate_selected_member(
    session: SelectionSession,
    directory: Directory,
    *,
    selector_directory: str,
    folder: str | None,
) -> tuple[str, MemberSnapshot, tuple[skillspec.SkillRequirement, ...]]:
    label = _member_label(selector_directory, folder)
    snapshot = _run_member_reader(
        label,
        _member_path(directory),
        lambda: session.snapshot_member(directory, label=label),
    )
    skill_name = validate_member_package(
        _member_path(directory),
        selector_directory,
        folder,
        resolved_root=session.root.display,
        snapshot=snapshot,
    )
    _check_manifest_identity(
        _member_path(directory),
        skill_name,
        selector_directory,
        folder,
        resolved_root=session.root.display,
        snapshot=snapshot,
    )
    requirements = _member_declared_requirements(snapshot)
    return skill_name, snapshot, requirements


def resolve_selector_directory(source_root: Path, directory: str) -> Path:
    """Resolve one selector directory inside the source root.

    ``"."`` selects the source root itself. Any other directory must be a
    portable contained path; symlinks are resolved before the containment
    check, a missing or non-directory target fails
    ``source_selection_invalid`` with the precise inspection error, and an
    escape fails ``source_selection_invalid`` even when the escape target
    exists.
    """
    selector_components = tuple(
        _selector_components(
            directory,
            context=f"Selector directory {directory!r} cannot be resolved",
        )
    )
    try:
        session = _open_session(
            source_root,
            preflight=PreflightRequest(
                paths=(
                    PreflightPath(
                        selector_components,
                        code=CODE_SELECTION_INVALID,
                        context=f"Selector directory {directory!r} cannot be resolved",
                    ),
                )
            ),
        )
    except SourceError as exc:
        # Phase A now rejects before a session exists. Preserve this entry
        # point's source-containment error when the rejected absolute target
        # was only classified as a managed-output overlap by the boundary
        # preflight.
        if (
            exc.code == CODE_OUTPUT_OVERLAP
            and "absolute symlink target lies inside the csk home" in exc.detail
        ):
            raise SourceError(
                CODE_SELECTION_INVALID,
                f"Selector directory {directory!r} escapes the source root",
            ) from exc
        raise
    with session:
        try:
            directory_node, _ = _resolve_in_session(
                session,
                directory,
                context=f"Selector directory {directory!r} cannot be resolved",
            )
        except SourceError as exc:
            # A selector may not escape even when its target happens to lie in
            # the managed csk home.  Collections classify that same physical
            # target as output for wildcard pruning, but this entry point is
            # the source-root containment gate.
            if (
                exc.code == CODE_OUTPUT_OVERLAP
                and "absolute symlink target lies inside the csk home" in exc.detail
            ):
                raise SourceError(
                    CODE_SELECTION_INVALID,
                    f"Selector directory {directory!r} escapes the source root",
                ) from exc
            raise
        if not any(
            item.identity == session.root.identity
            for item in directory_node.ancestry()
        ):
            raise SourceError(
                CODE_SELECTION_INVALID,
                f"Selector directory {directory!r} escapes the source root",
            )
        return directory_node.display


def expand_collection(
    source_root: Path,
    selector: CollectionSelector,
) -> list[SelectedSkill]:
    """Expand one collection selector in UTF-8 folder-name byte order.

    Immediate child directories only. Include literals are checked for
    existence and directory type before exclusions; a missing explicit member
    fails ``source_member_missing`` even if excluded. ``"*"`` selects all
    immediate directories after pruning through the single
    :func:`managed_output_boundary` predicate (physical ancestry plus csk-home
    containment). Exclusions then remove names; a missing exclusion is
    harmless. Every remaining member is checked against the same predicate on
    its physical path before any metadata read: an explicit literal inside
    managed output refuses with ``source_output_overlap``, while a
    ``"*"``-discovered managed path prunes silently.
    Every remaining member must carry valid
    SKILL.md frontmatter and satisfy the ordinary package/manifest rules; an
    invalid discovered candidate fails the whole operation with
    ``source_member_invalid``. An empty expanded collection fails with
    ``source_member_invalid``. Repeated direct selections and directly declared
    skill requirements naming one dependency with different repository
    identities fail ``source_name_conflict``. No metadata or package read
    escapes the resolved source root.
    """
    base_components = tuple(
        _selector_components(
            selector.directory,
            context=f"Collection directory {selector.directory!r} cannot be resolved",
        )
    )
    include = list(selector.include)
    exclude = set(selector.exclude)
    has_star = "*" in include
    literals = [name for name in include if name != "*"]
    preflight_paths = tuple(
        [
            PreflightPath(
                base_components,
                code=CODE_SELECTION_INVALID,
                context=f"Collection directory {selector.directory!r} cannot be resolved",
                collect_boundaries=True,
            )
        ]
        + [
            PreflightPath(
                (*base_components, name),
                code=CODE_SELECTION_INVALID,
                missing_code=CODE_MEMBER_MISSING,
                not_directory_code=CODE_MEMBER_INVALID,
                context=f"Collection {selector.directory!r} member {name!r}",
                # An excluded literal still has to exist and be a directory,
                # but it is not a selected package and therefore does not
                # require internal managed-boundary inspection in Phase A.
                collect_boundaries=name not in exclude,
            )
            for name in literals
        ]
    )
    with _open_session(
        source_root,
        preflight=PreflightRequest(
            paths=preflight_paths,
            wildcard_bases=(base_components,) if has_star else (),
            wildcard_excludes=(frozenset(exclude),) if has_star else (),
        ),
    ) as session:
        base, _ = _resolve_in_session(
            session,
            selector.directory,
            context=f"Collection directory {selector.directory!r} cannot be resolved",
        )
        repeated_literal = len(literals) != len(set(literals))
        selected: dict[str, tuple[Directory, bool, bool]] = {}

        # Literals are opened before exclusions, as required by section 1.
        for name in literals:
            result = session.descend(
                base,
                [name],
                code=CODE_SELECTION_INVALID,
                missing_code=CODE_MEMBER_MISSING,
                context=f"Collection {selector.directory!r} member {name!r}",
            )
            selected[name] = (result.directory, True, result.final_component_link)

        if has_star:
            for name in session.child_directories(
                base,
                context=f"Collection directory {selector.directory!r}",
            ):
                if name in selected or name in exclude:
                    continue
                result = session.descend(
                    base,
                    [name],
                    code=CODE_SELECTION_INVALID,
                    missing_code=CODE_SELECTION_INVALID,
                    context=f"Collection {selector.directory!r} member {name!r}",
                )
                boundary = managed_output_boundary(result.directory, session=session)
                if boundary is not None:
                    continue
                selected[name] = (result.directory, False, result.final_component_link)

        remaining = sorted(
            (name for name in selected if name not in exclude),
            key=_utf8_key,
        )
        if not remaining:
            raise SourceError(
                CODE_MEMBER_INVALID,
                f"Collection {selector.directory!r} expands to an empty skill set",
            )

        members: list[SelectedSkill] = []
        for folder in remaining:
            member, literal, final_link = selected[folder]
            boundary = managed_output_boundary(member, session=session)
            if boundary is not None:
                if literal:
                    raise SourceError(
                        CODE_OUTPUT_OVERLAP,
                        f"Collection {selector.directory!r} member {folder!r} lies {boundary}",
                    )
                continue
            if final_link:
                raise SourceError(
                    CODE_MEMBER_INVALID,
                    f"Collection {selector.directory!r} member {folder!r} is a symbolic link",
                )
            skill_name, _, requirements = _validate_selected_member(
                session,
                member,
                selector_directory=selector.directory,
                folder=folder,
            )
            directory = folder if selector.directory == "." else f"{selector.directory}/{folder}"
            members.append(
                SelectedSkill(
                    name=skill_name,
                    from_alias=selector.from_alias,
                    directory=directory,
                    path=member.display,
                    folder=folder,
                    requirements=requirements,
                )
            )
        if not members:
            raise SourceError(
                CODE_MEMBER_INVALID,
                f"Collection {selector.directory!r} expands to an empty skill set",
            )
        if repeated_literal:
            raise SourceError(
                CODE_NAME_CONFLICT,
                f"Repeated direct member selection in collection {selector.directory!r}",
            )
        _check_installed_name_conflicts(
            [member.name for member in members],
            context=f"collection {selector.directory!r}",
        )
        _check_requirement_identity_conflicts(
            members,
            context=f"collection {selector.directory!r}",
        )
        session.reverify_managed_boundaries()
        return members


def resolve_individual(
    source_root: Path,
    selector: IndividualSelector,
) -> SelectedSkill:
    """Resolve one individual selector to its validated package.

    The selector directory must resolve inside the source root
    (``source_selection_invalid`` on escape, on a missing directory, or when
    the resolved target is not a directory). A package inside managed output
    (the single :func:`managed_output_boundary` predicate on the physical
    path, before any metadata read) refuses with ``source_output_overlap``.
    The package must carry valid SKILL.md frontmatter and satisfy the
    ordinary package/manifest rules (``source_member_invalid``), and the
    selector name must equal the validated SKILL.md name and the resolved
    skill manifest identity where that manifest declares one
    (``source_selection_invalid`` on mismatch: the member itself may be valid
    while the selector names the wrong skill).
    """
    selector_components = tuple(
        _selector_components(
            selector.directory,
            context=f"Selector directory {selector.directory!r} cannot be resolved",
        )
    )
    with _open_session(
        source_root,
        preflight=PreflightRequest(
            paths=(
                PreflightPath(
                    selector_components,
                    code=CODE_SELECTION_INVALID,
                    context=f"Selector directory {selector.directory!r} cannot be resolved",
                ),
            )
        ),
    ) as session:
        target, final_link = _resolve_in_session(
            session,
            selector.directory,
            context=f"Selector directory {selector.directory!r} cannot be resolved",
        )
        boundary = managed_output_boundary(target, session=session)
        if boundary is not None:
            raise SourceError(
                CODE_OUTPUT_OVERLAP,
                f"Selector directory {selector.directory!r} lies {boundary}",
            )
        if final_link:
            raise SourceError(
                CODE_MEMBER_INVALID,
                f"Selector directory {selector.directory!r} is a symbolic link",
            )
        skill_name, _, requirements = _validate_selected_member(
            session,
            target,
            selector_directory=selector.directory,
            folder=None,
        )
        if skill_name != selector.name:
            raise SourceError(
                CODE_SELECTION_INVALID,
                f"Selector names {selector.name!r} but {selector.directory!r} carries skill {skill_name!r}",
            )
        session.reverify_managed_boundaries()
        folder = "." if selector.directory == "." else selector.directory.rsplit("/", 1)[-1]
        return SelectedSkill(
            name=selector.name,
            from_alias=selector.from_alias,
            directory=selector.directory,
            path=target.display,
            folder=folder,
            requirements=requirements,
        )


def expand_selectors(
    selectors: list[SkillSelector],
    source_roots: dict[str, Path],
    *,
    reserved_names: tuple[str, ...] = (),
) -> list[SelectedSkill]:
    """Expand every selector and validate the whole set before publication.

    ``source_roots`` maps each referenced ``from`` alias to its resolved
    source root. Members keep selector order; each collection's members are in
    UTF-8 folder-name byte order. Repeated direct selections of one installed
    name (even identical selections), filesystem-equivalent destination names
    (case-insensitive and Unicode-equivalent collisions via NFD+casefold),
    conflicts against ``reserved_names`` (for example legacy declarations
    checked by the caller), and directly declared skill requirements naming one
    dependency with different repository identities fail
    ``source_name_conflict``. Ref-vs-ref comparison needs repository access
    and belongs to the closure validation at publication, which re-examines
    every deferred requirement.
    """
    for alias in {selector.from_alias for selector in selectors}:
        if alias not in source_roots:
            raise SourceError(
                CODE_SELECTION_INVALID,
                f"Selector names source {alias!r} without a resolved source root",
            )
    members: list[SelectedSkill] = []
    for selector in selectors:
        root = source_roots[selector.from_alias]
        if isinstance(selector, IndividualSelector):
            members.append(resolve_individual(root, selector))
        else:
            members.extend(expand_collection(root, selector))
    _check_installed_name_conflicts(
        [*reserved_names, *(member.name for member in members)],
        context="selection set",
    )
    _check_requirement_identity_conflicts(members, context="selection set")
    return members


def validate_member_package(
    member_path: Path,
    selector_directory: str,
    folder: str | None,
    *,
    resolved_root: Path,
    snapshot: MemberSnapshot | None = None,
) -> str:
    """Validate one member package and return its installed SKILL.md name.

    Every remaining member must carry SKILL.md frontmatter with the required
    non-empty ``name`` and ``description`` strings and satisfy the ordinary
    package/manifest rules (``skillcheck.validate_skill`` with no locale).
    Any failure raises ``source_member_invalid``; discovered candidates are
    never skipped. No file read escapes the resolved source root: one
    exhaustive physical pre-read walk (no name pruning, links and special
    files rejected, every physical path contained) runs before SKILL.md,
    manifest, or ``skillcheck`` reads, so an escaping ``SKILL.md``, manifest,
    or package file -- including one hidden in a pruned-name subtree such as
    ``references/.git`` -- fails without its outside bytes being opened.
    """
    label = _member_label(selector_directory, folder)
    if snapshot is None:
        raise SourceError(
            CODE_MEMBER_INVALID,
            f"Skill member {label} requires a descriptor-backed snapshot",
        )
    snapshot_path = _snapshot_path(snapshot)
    skill_name = _run_member_reader(
        label,
        member_path,
        lambda: read_skill_md_name(
            member_path,
            label,
            resolved_root=resolved_root,
            raw=snapshot.files.get(("SKILL.md",)),
            preloaded=True,
        ),
    )
    issues = _run_member_reader(
        label,
        member_path,
        lambda: skillcheck.validate_skill(snapshot_path, locale_value=None),
    )
    errors = [issue for issue in issues if issue.severity == "error"]
    if errors:
        detail = "; ".join(issue.message for issue in errors)
        raise SourceError(
            CODE_MEMBER_INVALID, f"Skill member {label} is invalid: {detail}"
        )
    return skill_name


def read_skill_md_name(
    member_path: Path,
    label: str,
    *,
    resolved_root: Path,
    raw: bytes | None = None,
    preloaded: bool = False,
) -> str:
    """Read and validate SKILL.md frontmatter, returning the skill name.

    The file is read as bytes and decoded as strict UTF-8 (invalid bytes
    refuse); a leading BOM is removed, CRLF is accepted (a trailing CR is
    dropped only where it precedes a consumed LF, so a final ``---<CR>`` at
    EOF keeps its lone CR), a lone CR inside the framed region refuses (body
    CR is ordinary ignored body bytes), NEL/LS/PS stay inside
    their physical line as content in every position (block-scalar chomping
    acts only on trailing empty lines and the final LF, so a trailing
    separator survives: clip keeps the separator AND one trailing LF, strip
    keeps the separator only), and the framed region (opening through
    closing fence) must carry only YAML 1.2 ``c-printable`` source
    characters with U+FEFF allowed only as the very first file character.
    The frontmatter must
    open at line 1 with exactly ``---`` at column 0 (optional trailing
    spaces/tabs only) and close at the first later line that is exactly
    ``---`` or ``...`` at column 0; anything before the opening fence, a
    missing closing fence, or ``--- x``-style trailing text refuses, and an
    indented fence-looking line is ordinary content for the block grammar
    (valid inside a block scalar, a structural error elsewhere). The block
    must carry non-empty ``name`` and ``description`` strings; ``name`` must
    be an installable destination name (at most 128 characters, portable
    across hosts per :func:`is_installable_name`, so Unicode names --
    including NEL -- are admitted while reserved device names and control
    characters other than NEL are refused) and ``triggers``, when present,
    a list of non-empty strings.
    Every single-line scalar goes through the single
    :func:`parse_frontmatter_scalar` tokenizer (quote-aware ``#`` comments,
    quoted scalars are strings, empty/comment-only values and
    flow/anchor/tag/block starters are refused, ``- `` sequence entries,
    ``? `` explicit mapping key entries and a lone ``=`` tag handle refuse,
    and plain scalars carrying a mapping indicator refuse before the
    REGEX-ONLY YAML 1.2 core-schema classification). The block parser is
    indentation-aware: a tab in indentation refuses, ``key:`` needs a space
    or end of line after the colon, nested blocks under other keys are
    consumed and ignored (their inner keys never satisfy root requirements),
    ``triggers`` alone parses block/flow lists, literal/folded block scalars
    (``|``/``>`` per YAML 1.2 section 8.1: strict header with chomping/indent
    indicators and an optional separated ``#`` comment, an explicit or
    auto-detected content indentation carried into the content reader,
    literal preservation, folded joining, and clip/strip/keep chomping)
    consume indented content (including indented fence-looking lines), and
    an indented line under a scalar-valued key is a structural error;
    anything else fails ``source_member_invalid``. Lines after the closing
    fence are the body and are ignored. The ``SKILL.md`` file itself must be
    a regular file inside the resolved source root; links are rejected
    before the read.
    """
    if preloaded:
        if raw is None:
            raise SourceError(
                CODE_MEMBER_INVALID, f"Skill member {label} has no SKILL.md"
            )
        raw_bytes = raw
    else:
        raw_bytes = read_regular_path(
            member_path / "SKILL.md",
            code=CODE_MEMBER_INVALID,
            label=label,
            what="SKILL.md",
        )
    try:
        text = raw_bytes.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise SourceError(
            CODE_MEMBER_INVALID,
            f"Skill member {label} SKILL.md is not valid UTF-8: {exc}",
        ) from exc
    fields = _parse_frontmatter(text, label)
    name = fields.get("name")
    description = fields.get("description")
    # The required-field gate strips ASCII structural whitespace: spaces,
    # tabs, and the line breaks block scalars produce under clip/keep. Any
    # other character (including Unicode whitespace) is content, so a value
    # of only NBSP is non-empty here exactly as the oracle reads it.
    if not isinstance(name, str) or not name.strip(" \t\n"):
        raise SourceError(
            CODE_MEMBER_INVALID,
            f"Skill member {label} SKILL.md frontmatter requires a non-empty 'name'",
        )
    stripped_name = name.strip(" \t\n")
    if len(stripped_name) > 128 or not is_installable_name(stripped_name):
        raise SourceError(
            CODE_MEMBER_INVALID,
            f"Skill member {label} SKILL.md names {stripped_name!r}, "
            "which is not a portable destination name",
        )
    if not isinstance(description, str) or not description.strip(" \t\n"):
        raise SourceError(
            CODE_MEMBER_INVALID,
            f"Skill member {label} SKILL.md frontmatter requires a non-empty 'description'",
        )
    triggers = fields.get("triggers")
    if triggers is not None:
        if not isinstance(triggers, list) or not triggers:
            raise SourceError(
                CODE_MEMBER_INVALID,
                f"Skill member {label} SKILL.md field 'triggers' must be a "
                "non-empty list of non-empty strings",
            )
        for entry in triggers:
            if not isinstance(entry, str) or not entry.strip(" \t\n"):
                raise SourceError(
                    CODE_MEMBER_INVALID,
                    f"Skill member {label} SKILL.md field 'triggers' must be a "
                    "non-empty list of non-empty strings",
                )
    return stripped_name


def destination_key(name: str) -> str:
    """Fold one installed name the way a case-insensitive host would map it."""
    return unicodedata.normalize("NFD", name).casefold()


def is_installable_name(value: str) -> bool:
    """Return whether one validated name is installable as a destination.

    The shared portable-component rule, with exactly U+0085 (NEL) admitted:
    NEL is YAML 1.2 ``c-printable`` content (the only C1 control the source
    gate permits raw), is legal in directory names on every supported host
    (Windows forbids C0 controls only), and behaves identically to the other
    two YAML separators LS/PS, which the shared rule already admits. Every
    other control character (C0, DEL, the remaining C1) still refuses: raw
    ones never reach this gate (the source-character gate refuses them
    first) and escape-decoded ones stay refused fail-closed.
    """
    if "\x85" in value:
        value = value.replace("\x85", "x")
    return identifiers.is_portable_component(value)


def _check_installed_name_conflicts(names: list[str], *, context: str) -> None:
    seen_exact: set[str] = set()
    seen_folded: dict[str, str] = {}
    for name in names:
        if name in seen_exact:
            raise SourceError(
                CODE_NAME_CONFLICT,
                f"Duplicate installed skill name {name!r} in {context}",
            )
        seen_exact.add(name)
        folded = destination_key(name)
        previous = seen_folded.get(folded)
        if previous is not None and previous != name:
            raise SourceError(
                CODE_NAME_CONFLICT,
                f"Filesystem-equivalent installed skill names {previous!r} "
                f"and {name!r} collide in {context}",
            )
        seen_folded.setdefault(folded, name)


def _check_requirement_identity_conflicts(
    members: list[SelectedSkill], *, context: str
) -> None:
    """Fail when two members declare one dependency with different identities.

    Mirrors the source half of the existing closure unification
    (``closure._unify``): the same requirement name with two known, different
    canonical repository identities is a ``source_name_conflict``. Identical
    declarations unify silently. Repository comparison uses the canonical
    ``host/path`` identity, so URL spelling variants of one repository never
    conflict. A requirement whose identity cannot be decided here (a local
    path with no network identity, or a malformed source the manifest schema
    accepts) is left for the closure validation at publication, which
    re-examines every declaration with repository access; ref-vs-ref
    comparison belongs there for the same reason.
    """

    seen: dict[str, tuple[str, str]] = {}
    for member in members:
        for requirement in member.requirements:
            try:
                identity = source_identity.canonical_source_identity(requirement.git)
            except source_identity.SourceIdentityError:
                continue
            if identity is None:
                continue
            previous = seen.get(requirement.name)
            if previous is None:
                seen[requirement.name] = (identity, member.name)
            elif previous[0] != identity:
                raise SourceError(
                    CODE_NAME_CONFLICT,
                    f"Conflicting identities for dependency {requirement.name!r} "
                    f"in {context}: {previous[0]!r} via member {previous[1]!r} "
                    f"and {identity!r} via member {member.name!r}",
                )


def _check_manifest_identity(
    member_path: Path,
    skill_name: str,
    selector_directory: str,
    folder: str | None,
    *,
    resolved_root: Path,
    snapshot: MemberSnapshot | None = None,
) -> None:
    label = _member_label(selector_directory, folder)
    declared = _run_member_reader(
        label,
        member_path,
        lambda: _manifest_declared_name(
            member_path,
            resolved_root=resolved_root,
            label=label,
            snapshot=snapshot,
        ),
    )
    if declared is not None and declared != skill_name:
        raise SourceError(
            CODE_MEMBER_INVALID,
            f"Skill member {label} SKILL.md names {skill_name!r} but its skill "
            f"manifest declares {declared!r}",
        )


def _manifest_declared_name(
    member_path: Path,
    *,
    resolved_root: Path,
    label: str,
    snapshot: MemberSnapshot | None = None,
) -> str | None:
    if snapshot is not None:
        for filename in (skillspec.CANONICAL_MANIFEST, skillspec.LEGACY_MANIFEST):
            raw = snapshot.files.get((filename,))
            if raw is None:
                continue
            try:
                data = protocol_json.loads(raw)
            except protocol_json.ProtocolJSONError:
                return None
            if isinstance(data, dict):
                name = data.get("name")
                if isinstance(name, str) and name:
                    return name
            return None
        return None
    for filename in (skillspec.CANONICAL_MANIFEST, skillspec.LEGACY_MANIFEST):
        candidate = member_path / filename
        raw = read_optional_regular_path(
            candidate,
            code=CODE_MEMBER_INVALID,
            label=label,
            what=filename,
        )
        if raw is None:
            continue
        try:
            data = protocol_json.loads(raw)
        except protocol_json.ProtocolJSONError:
            return None
        if isinstance(data, dict):
            name = data.get("name")
            if isinstance(name, str) and name:
                return name
        return None
    return None


def _member_declared_requirements(
    snapshot: MemberSnapshot,
) -> tuple[skillspec.SkillRequirement, ...]:
    """Extract validated skill requirements from snapshot manifest bytes.

    Runs after ``skillcheck.validate_skill`` accepted the member, so the
    manifest (when present) already satisfies the skillspec grammar; the
    shape checks below only narrow types for the extractor. Reads the same
    canonical-then-legacy manifest the declared-name check reads.
    """

    for filename in (skillspec.CANONICAL_MANIFEST, skillspec.LEGACY_MANIFEST):
        raw = snapshot.files.get((filename,))
        if raw is None:
            continue
        try:
            data = protocol_json.loads(raw)
        except protocol_json.ProtocolJSONError:
            return ()
        if not isinstance(data, dict):
            return ()
        dependencies = data.get("dependencies")
        if not isinstance(dependencies, dict):
            return ()
        skills = dependencies.get("skills")
        if skills is None:
            return ()
        if not isinstance(skills, dict):
            return ()
        found: list[skillspec.SkillRequirement] = []
        for requirement_name, entry in skills.items():
            if not isinstance(requirement_name, str) or not requirement_name:
                continue
            if not isinstance(entry, dict):
                continue
            git = entry.get("git")
            ref = entry.get("ref")
            if not isinstance(git, str) or not git:
                continue
            if not isinstance(ref, dict):
                continue
            kind = ref.get("kind")
            value = ref.get("value")
            if not isinstance(kind, str) or not isinstance(value, str):
                continue
            mode = entry.get("mode", "full")
            if not isinstance(mode, str):
                continue
            commands_raw = entry.get("commands", ())
            if isinstance(commands_raw, (list, tuple)) and all(
                isinstance(item, str) for item in commands_raw
            ):
                commands = tuple(commands_raw)
            elif commands_raw == ():
                commands = ()
            else:
                continue
            found.append(
                skillspec.SkillRequirement(
                    name=requirement_name,
                    git=git,
                    ref_kind=kind,
                    ref_value=value,
                    mode=mode,
                    commands=commands,
                    source=filename,
                )
            )
        return tuple(found)
    return ()


def _member_label(selector_directory: str, folder: str | None) -> str:
    if folder is None:
        return repr(selector_directory)
    if selector_directory == ".":
        return repr(folder)
    return repr(f"{selector_directory}/{folder}")


def _utf8_key(value: str) -> bytes:
    return value.encode("utf-8")


def _split_frontmatter_lines(text: str, label: str) -> list[str]:
    """Split document text on LF with CRLF normalisation (no validation).

    A trailing CR is dropped only where it actually precedes a consumed LF:
    every ``split("\\n")`` segment except the final unterminated one loses at
    most one trailing CR, while the final segment (text after the last LF, or
    the whole text when there is no LF) keeps any trailing CR. A document
    ending in ``---<CR>`` or ``...<CR>`` at EOF therefore keeps that lone CR
    instead of framing as a clean fence, and the framed validation refuses
    it. No character rule runs here: lone-CR refusal and every other
    frontmatter character/structure rule apply to the FRAMED region only (see
    :func:`_check_framed_no_lone_cr` and :func:`_check_frontmatter_printable`),
    after the opening and closing fences are located. Lines after the closing
    fence are the body and are never validated. NEL (U+0085), LS (U+2028) and
    PS (U+2029) are ordinary content characters: they are never line breaks
    here, never whitespace, and never rewritten, so they stay inside the
    physical line they were written on.
    """
    _ = label
    segments = text.split("\n")
    lines = [
        segment[:-1] if segment.endswith("\r") else segment
        for segment in segments[:-1]
    ]
    lines.append(segments[-1])
    return lines


def _check_framed_no_lone_cr(lines: list[str], end_index: int, label: str) -> None:
    """Refuse a lone CR anywhere inside the framed frontmatter region.

    Covers the opening fence through the closing fence only; body lines after
    the closing fence are never read, so a CR there is ordinary body bytes.
    A lone CR is neither a supported line ending nor content.
    """
    for line in lines[: end_index + 1]:
        if "\r" in line:
            raise SourceError(
                CODE_MEMBER_INVALID,
                f"Skill member {label} SKILL.md frontmatter carries a lone "
                "carriage return",
            )


def _is_opening_fence(line: str) -> bool:
    """Return whether one line is the column-0 opening ``---`` fence.

    Exactly ``---`` followed only by optional spaces/tabs. Leading
    whitespace, trailing text, and ``...`` are not an opening fence.
    """
    if line == "---":
        return True
    if line.startswith("---"):
        rest = line[3:]
        return rest != "" and all(char in (" ", "\t") for char in rest)
    return False


def _is_closing_fence(line: str) -> bool:
    """Return whether one line is a column-0 closing fence (``---``/``...``).

    Exactly the marker followed only by optional spaces/tabs. Fence
    detection never strips leading whitespace: an indented ``---`` or ``...``
    is an ordinary content line for the block grammar, where it is valid
    content inside a block scalar and a structural error elsewhere.
    """
    if line == "---" or line == "...":
        return True
    for marker in ("---", "..."):
        if line.startswith(marker):
            rest = line[len(marker):]
            if rest != "" and all(char in (" ", "\t") for char in rest):
                return True
    return False


def _is_yaml_printable(char: str) -> bool:
    """Return whether one source character is YAML 1.2 ``c-printable``.

    ``U+0009 | U+000A | U+000D | [U+0020-U+007E] | U+0085 |
    [U+00A0-U+D7FF] | [U+E000-U+FFFD] | [U+10000-U+10FFFF]``. Everything else
    (the remaining C0 controls, U+007F, the C1 range except U+0085, lone
    surrogates, U+FFFE/U+FFFF) is not a YAML source character at all. This is
    a range check over code points only; no numeric conversion is involved.
    """
    code = ord(char)
    if code in (0x09, 0x0A, 0x0D):
        return True
    if 0x20 <= code <= 0x7E:
        return True
    if code == 0x85:
        return True
    if 0xA0 <= code <= 0xD7FF:
        return True
    if 0xE000 <= code <= 0xFFFD:
        return True
    return 0x10000 <= code <= 0x10FFFF


def _check_frontmatter_printable(lines: list[str], end_index: int, label: str) -> None:
    """Refuse any non-printable YAML source character in the framed region.

    The gate covers the opening fence through the closing fence only; lines
    after the closing fence are the body and are never read. A byte-order
    mark (U+FEFF) is permitted only as the very first character of the file
    (already removed before framing), so any remaining U+FEFF refuses. The
    gate sees raw source text before any scalar decoding, so backslash escape
    spellings inside double-quoted scalars pass and only their decoded values
    may carry control characters.
    """
    for line in lines[: end_index + 1]:
        for char in line:
            if char == "\ufeff":
                raise SourceError(
                    CODE_MEMBER_INVALID,
                    f"Skill member {label} SKILL.md frontmatter carries U+FEFF "
                    "after the start of the file: a byte-order mark is "
                    "permitted only as the very first character",
                )
            if not _is_yaml_printable(char):
                raise SourceError(
                    CODE_MEMBER_INVALID,
                    f"Skill member {label} SKILL.md frontmatter carries "
                    f"U+{ord(char):04X}, which is not a YAML printable "
                    "source character",
                )


def _parse_frontmatter(text: str, label: str) -> dict[str, object]:
    """Parse the supported frontmatter block grammar (fail closed).

    Framing first, without discarding indentation and without character
    validation beyond UTF-8/BOM: a leading BOM is removed, the text splits on
    LF with a trailing CR dropped only before a consumed LF (CRLF files frame
    correctly while a final unterminated ``---<CR>`` keeps its lone CR;
    NEL/LS/PS are content and never split lines), line 1 must be exactly
    ``---`` at column 0 (optional trailing spaces/tabs only; anything before
    the opening fence refuses), and the closing fence is the first later line
    that is exactly ``---`` or ``...`` at column 0. Fence detection never
    strips leading whitespace, so an indented fence-looking line is ordinary
    content for the block grammar: valid inside a block scalar, a structural
    error elsewhere. Once framed, the opening fence through the closing fence
    is gated on a lone CR (refuses) and on YAML 1.2 ``c-printable`` source
    characters (any other U+FEFF or non-printable refuses before the block
    grammar runs). Lines after the closing fence are the body and are ignored
    and never validated; a block with zero entries refuses via the required
    keys.

    The block grammar is indentation-aware: blank (empty or spaces/tabs-only)
    lines and full-line comments are skipped, a tab in indentation refuses,
    and a top-level entry
    is a line at indent 0 matching ``key:`` (``[A-Za-z0-9_.-]+``) followed by
    end of line or by a space/tab and a value. An indent-0 line starting with
    ``---``/``...`` followed by other text refuses, duplicate keys refuse, as
    does any other indent-0 line. An empty value with indented children is a
    nested block: a sequence under ``triggers`` parses to its items, anything
    under ``name``/``description`` refuses (not a string), and anything under
    another key is consumed and ignored so a nested ``name:``/
    ``description:`` never counts as a root key. An empty value with no
    indented children is null (the tokenizer raises for every key). ``|``/
    ``>`` headers (strict YAML 1.2 section 8.1 headers: style, chomping/indent
    indicators in either order, optional spaces/tabs, an optional separated
    ``#`` comment) consume the indented content against the header's explicit
    indentation or the auto-detected first-content-line indentation; a
    non-empty line below that baseline is a structural error, and
    literal/folded rendering with clip/strip/keep chomping yields the stored
    value. Flow collections refuse for ``name`` and
    ``description``, parse for ``triggers`` lists, and are
    consumed-and-ignored (balanced, single-line) for other keys. A line with
    indent > 0 reaching a scalar-valued key is a structural error: a scalar
    cannot own children.
    """
    if text.startswith("\ufeff"):
        text = text[1:]
    lines = _split_frontmatter_lines(text, label)
    if not lines or not _is_opening_fence(lines[0]):
        raise SourceError(
            CODE_MEMBER_INVALID,
            f"Skill member {label} SKILL.md frontmatter must open with '---'",
        )
    end_index: int | None = None
    for index in range(1, len(lines)):
        if _is_closing_fence(lines[index]):
            end_index = index
            break
    if end_index is None:
        raise SourceError(
            CODE_MEMBER_INVALID,
            f"Skill member {label} SKILL.md frontmatter must close with '---'",
        )
    _check_framed_no_lone_cr(lines, end_index, label)
    _check_frontmatter_printable(lines, end_index, label)
    fields: dict[str, object] = {}
    body = lines[1:end_index]
    index = 0
    while index < len(body):
        raw = body[index]
        index += 1
        if _is_blank_line(raw):
            continue
        indent, content = _split_indent(raw, label)
        if content.startswith("#"):
            continue
        if indent > 0:
            raise SourceError(
                CODE_MEMBER_INVALID,
                f"Skill member {label} SKILL.md frontmatter has an indented line "
                f"{content!r} without a parent block: a scalar cannot own children",
            )
        if content.startswith("---") or content.startswith("..."):
            raise SourceError(
                CODE_MEMBER_INVALID,
                f"Skill member {label} SKILL.md frontmatter carries a fence-like "
                f"line {content!r} with trailing text: not a 'key: value' entry",
            )
        key, raw_after = _split_key_line(content, label)
        if key in fields:
            raise SourceError(
                CODE_MEMBER_INVALID,
                f"Skill member {label} SKILL.md frontmatter repeats key {key!r}",
            )
        if _is_empty_or_comment(raw_after):
            if _child_block_follows(body, index, label):
                value, index = _consume_child_block(body, index, key, label)
                fields[key] = value
            else:
                # No indented children: the scalar itself is empty or
                # comment-only (null, not a string). The tokenizer raises.
                fields[key] = parse_frontmatter_scalar(raw_after, label)
            continue
        stripped = raw_after.lstrip(" \t")
        if stripped.startswith(("|", ">")):
            style, chomping, explicit_indent = _parse_block_header(raw_after, label)
            value, index = _consume_block_scalar(
                body,
                index,
                label,
                style=style,
                chomping=chomping,
                explicit_indent=explicit_indent,
            )
            fields[key] = value
            continue
        trimmed = raw_after.strip(" \t")
        if trimmed.startswith("[") or trimmed.startswith("{"):
            if key in ("name", "description"):
                # Flow collections are never strings for scalar fields. The
                # tokenizer raises with the single unsupported-node error.
                fields[key] = parse_frontmatter_scalar(raw_after, label)
                continue
            if key == "triggers":
                if trimmed.startswith("["):
                    fields[key] = _parse_flow_list(raw_after, label)
                else:
                    fields[key] = parse_frontmatter_scalar(raw_after, label)
                continue
            _consume_balanced_flow(raw_after, label)
            fields[key] = {}
            continue
        fields[key] = parse_frontmatter_scalar(raw_after, label)
    return fields


def _split_indent(raw: str, label: str) -> tuple[int, str]:
    """Split one non-blank frontmatter line into (indent, content).

    A tab anywhere in the indentation refuses: YAML forbids tab indentation.
    Callers detect blank lines (empty or spaces/tabs-only, skipped) before calling.
    """
    pos = 0
    while pos < len(raw) and raw[pos] in (" ", "\t"):
        if raw[pos] == "\t":
            raise SourceError(
                CODE_MEMBER_INVALID,
                f"Skill member {label} SKILL.md frontmatter uses tab indentation",
            )
        pos += 1
    return pos, raw[pos:]


def _split_key_line(content: str, label: str) -> tuple[str, str]:
    """Split one indent-0 frontmatter line into (key, raw value text)."""
    colon = content.find(":")
    if colon < 0:
        raise SourceError(
            CODE_MEMBER_INVALID,
            f"Skill member {label} SKILL.md frontmatter line is not 'key: value': "
            f"{content!r}",
        )
    core = content[:colon].rstrip(" \t")
    if _FRONTMATTER_KEY_RE.fullmatch(core) is None:
        raise SourceError(
            CODE_MEMBER_INVALID,
            f"Skill member {label} SKILL.md frontmatter carries an invalid key: "
            f"{content[:colon]!r}",
        )
    after = content[colon + 1 :]
    if after != "" and not after.startswith((" ", "\t")):
        raise SourceError(
            CODE_MEMBER_INVALID,
            f"Skill member {label} SKILL.md frontmatter 'key:' must be followed "
            f"by a space or end of line: {content!r}",
        )
    return core, after


def _child_block_follows(body: list[str], index: int, label: str) -> bool:
    """Peek whether an empty-valued key owns an indented child block."""
    cursor = index
    while cursor < len(body):
        raw = body[cursor]
        if _is_blank_line(raw):
            cursor += 1
            continue
        indent, content = _split_indent(raw, label)
        if content.startswith("#"):
            cursor += 1
            continue
        return indent > 0
    return False


def _consume_child_block(
    body: list[str], index: int, key: str, label: str
) -> tuple[object, int]:
    """Consume the nested block owned by one empty-valued key.

    Only called when :func:`_child_block_follows` reported indented children.
    ``triggers`` parses a block sequence to its items; ``name`` and
    ``description`` refuse (nested children are not strings); any other key
    consumes the whole block and ignores its contents, so inner
    ``name:``/``description:`` lines never count as root keys.
    """
    if key == "triggers":
        return _consume_trigger_list(body, index, label)
    if key in ("name", "description"):
        raise SourceError(
            CODE_MEMBER_INVALID,
            f"Skill member {label} SKILL.md frontmatter field {key!r} "
            "has nested children and is not a string",
        )
    cursor = index
    while cursor < len(body):
        raw = body[cursor]
        if _is_blank_line(raw):
            cursor += 1
            continue
        indent, content = _split_indent(raw, label)
        if content.startswith("#"):
            cursor += 1
            continue
        if indent == 0:
            break
        cursor += 1
    return {}, cursor


def _consume_trigger_list(
    body: list[str], index: int, label: str
) -> tuple[list[str], int]:
    """Consume the block sequence owned by ``triggers`` to its items.

    Every content line must be a ``- item`` entry at the block's own indent;
    mapping children, deeper nesting, and inconsistent indents refuse.
    """
    items: list[str] = []
    cursor = index
    block_indent: int | None = None
    while cursor < len(body):
        raw = body[cursor]
        if _is_blank_line(raw):
            cursor += 1
            continue
        indent, content = _split_indent(raw, label)
        if content.startswith("#"):
            cursor += 1
            continue
        if indent == 0:
            break
        if block_indent is None:
            block_indent = indent
        if indent != block_indent or not (
            content == "-" or content.startswith(("- ", "-\t"))
        ):
            raise SourceError(
                CODE_MEMBER_INVALID,
                f"Skill member {label} SKILL.md frontmatter field 'triggers' "
                f"has a non-list child {content!r}",
            )
        items.append(parse_frontmatter_scalar(content[1:], label))
        cursor += 1
    return items, cursor


def _parse_block_header(raw_after: str, label: str) -> tuple[str, str, int | None]:
    """Parse one ``|``/``>`` header after ``key:`` to ``(style, chomping, indent)``.

    Grammar (YAML 1.2 section 8.1 headers for the root-mapping case): the
    style indicator, then in either order at most one chomping indicator
    (``-`` strip, ``+`` keep; default clip) and at most one indentation
    indicator (a single digit ``1``-``9``), then optional spaces/tabs, then
    an optional ``#`` comment, then end of line. The comment requires the
    separation (``|#c`` refuses, matching the plain-scalar comment rule that
    ``#`` needs a preceding space/tab); a duplicate indicator, a ``0`` or
    multi-digit indicator, or any other trailing text refuses. The parsed
    triple is passed to :func:`_consume_block_scalar`, never discarded.
    """
    pos = 0
    while pos < len(raw_after) and raw_after[pos] in (" ", "\t"):
        pos += 1
    style = raw_after[pos]
    pos += 1
    chomping = "clip"
    explicit: int | None = None
    for _ in range(2):
        if pos < len(raw_after) and raw_after[pos] in ("-", "+"):
            if chomping != "clip":
                raise SourceError(
                    CODE_MEMBER_INVALID,
                    f"Skill member {label} SKILL.md frontmatter block header "
                    f"{raw_after.strip(' \t')!r} repeats the chomping indicator",
                )
            chomping = "strip" if raw_after[pos] == "-" else "keep"
            pos += 1
        elif pos < len(raw_after) and raw_after[pos] in _INDENT_VALUES:
            if explicit is not None:
                raise SourceError(
                    CODE_MEMBER_INVALID,
                    f"Skill member {label} SKILL.md frontmatter block header "
                    f"{raw_after.strip(' \t')!r} repeats the indentation indicator",
                )
            explicit = _INDENT_VALUES[raw_after[pos]]
            pos += 1
        else:
            break
    separator = pos
    while pos < len(raw_after) and raw_after[pos] in (" ", "\t"):
        pos += 1
    if pos >= len(raw_after):
        return style, chomping, explicit
    if raw_after[pos] == "#" and pos > separator:
        return style, chomping, explicit
    raise SourceError(
        CODE_MEMBER_INVALID,
        f"Skill member {label} SKILL.md frontmatter block header "
        f"{raw_after.strip(' \t')!r} carries invalid trailing text",
    )


def _is_all_spaces(line: str) -> bool:
    """Return whether one line is empty or carries spaces only.

    A tab is content, never blank filler: ``" \\t"`` is a content line whose
    text after the indentation is ``"\\t"`` (spec example 8.2), so only the
    space character counts here.
    """
    return line.strip(" ") == ""


def _split_content_indent(raw: str, label: str) -> tuple[int, str]:
    """Split one non-blank scalar content line into ``(indent, content)``.

    The indentation is leading spaces only: a tab after the spaces is content
    (spec examples 8.2 and 8.7), while a line starting with a tab refuses,
    since YAML forbids tab indentation.
    """
    if raw.startswith("\t"):
        raise SourceError(
            CODE_MEMBER_INVALID,
            f"Skill member {label} SKILL.md frontmatter uses tab indentation",
        )
    pos = 0
    while pos < len(raw) and raw[pos] == " ":
        pos += 1
    return pos, raw[pos:]


def _consume_block_scalar(
    body: list[str],
    index: int,
    label: str,
    *,
    style: str,
    chomping: str,
    explicit_indent: int | None,
) -> tuple[str, int]:
    """Consume the content owned by a ``|``/``>`` header (YAML 1.2 8.1).

    The root mapping is the parent, so its indent is 0: with an explicit
    indicator the content indentation is exactly that many spaces, otherwise
    it is auto-detected as the indentation of the first non-empty content
    line. Leading all-space lines are allowed before it and render as empty
    lines, but under auto-detection a leading all-space line more indented
    than the first non-empty line refuses, per the specification; with an
    explicit indicator leading all-space lines are ordinary lines (the
    remainder after the baseline decides empty versus content). Every following line
    belongs to the scalar while it is all-space or indented at least the
    content indentation; a non-empty line below that baseline ends the
    scalar when its indent is 0 (the next root entry or the closing fence)
    and is a structural error otherwise. Tabs in the indentation refuse;
    tabs after the indentation are content. Rendering is literal (each line
    keeps its text after removing exactly the content indentation) or folded
    (adjacent ordinary lines join with one space, empty lines and
    more-indented lines keep line breaks), with clip/strip/keep chomping;
    see :func:`_render_block_scalar`. The only line break is LF (from the
    line split; NEL/LS/PS are content bytes of their line, including when
    trailing), and chomping acts only on trailing empty lines and the final
    LF: clip keeps the separator character AND one trailing LF, strip keeps
    the separator only, keep keeps the separator and every trailing LF.
    """
    gathered: list[str] = []
    indents: list[int | None] = []
    cursor = index
    while cursor < len(body):
        raw = body[cursor]
        if _is_all_spaces(raw):
            gathered.append(raw)
            indents.append(None)
            cursor += 1
            continue
        indent, _content = _split_content_indent(raw, label)
        if indent == 0:
            break
        gathered.append(raw)
        indents.append(indent)
        cursor += 1
    first_indent: int | None = None
    for probe in indents:
        if probe is not None:
            first_indent = probe
            break
    if explicit_indent is not None:
        baseline = explicit_indent
    elif first_indent is not None:
        baseline = first_indent
    else:
        baseline = 0
        for raw in gathered:
            if len(raw) > baseline:
                baseline = len(raw)
    seen_content = False
    for raw, stored in zip(gathered, indents):
        if stored is None:
            # The over-indented-leading-empty error guards auto-detection
            # only: with an explicit indicator there is no detection to
            # confuse, so a leading all-space line is an ordinary line whose
            # remainder after the baseline decides empty versus content.
            if (
                explicit_indent is None
                and not seen_content
                and first_indent is not None
                and len(raw) > first_indent
            ):
                raise SourceError(
                    CODE_MEMBER_INVALID,
                    f"Skill member {label} SKILL.md frontmatter block scalar "
                    "has a leading empty line more indented than its first "
                    "content line",
                )
            continue
        seen_content = True
        if stored < baseline:
            raise SourceError(
                CODE_MEMBER_INVALID,
                f"Skill member {label} SKILL.md frontmatter block scalar "
                f"line {raw.strip(' \t')!r} is indented below the content "
                f"indentation ({baseline})",
            )
    # Empty scalar content is decided AFTER baseline removal: a line whose
    # remainder after the baseline is non-empty (even when it is only spaces,
    # or ends in NEL/LS/PS) is content -- it participates in folding as a
    # more-indented line and is never part of trailing-empty-line chomping.
    # Flagging physical whitespace-only lines empty here would chomp that
    # content away. The only break is LF: NEL/LS/PS stay content bytes of
    # their line, so trailing separators survive chomping (clip keeps the
    # separator AND one trailing LF, strip keeps the separator only).
    texts = [raw[baseline:] for raw in gathered]
    empties = [text == "" for text in texts]
    return (
        _render_block_scalar(texts, empties, style=style, chomping=chomping),
        cursor,
    )


def _render_block_scalar(
    texts: list[str],
    empties: list[bool],
    *,
    style: str,
    chomping: str,
) -> str:
    """Render block-scalar lines with folding and LF-only chomping.

    ``texts`` holds each physical line after removing exactly the content
    indentation (more-indented lines keep their extra spaces; all-space
    lines keep their remainder, which may itself be spaces; NEL/LS/PS are
    content bytes of their line). The break after every line is LF. With no
    non-empty line the value is trailing-only: ``""`` under clip/strip and
    every remainder plus LF under keep. Otherwise leading empty lines each
    emit their remainder plus LF; literal style then emits every middle line
    with LF, while folded style joins adjacent ordinary (non-empty,
    non-more-indented) lines with a single space when no empty line is
    pending, renders pending empty lines as their own LFs with nothing
    extra, and renders LF plus the pending LFs wherever a more-indented
    line (or a blank line with a non-empty remainder, which is
    more-indented spacing) is adjacent; trailing empty lines are dropped
    under clip/strip (clip keeps exactly one trailing LF, strip drops it)
    and emitted under keep.
    """
    first: int | None = None
    last: int | None = None
    for at, is_empty in enumerate(empties):
        if not is_empty:
            if first is None:
                first = at
            last = at
    if first is None or last is None:
        if chomping == "keep":
            return "".join(text + "\n" for text in texts)
        return ""
    out = "".join(text + "\n" for text in texts[:first])
    if style == "|":
        for at in range(first, last + 1):
            out += texts[at] + "\n"
        if chomping == "strip":
            out = out[:-1]
        elif chomping == "keep":
            for text in texts[last + 1 :]:
                out += text + "\n"
        return out
    have_previous = False
    previous_more = False
    pending = 0
    for at in range(first, last + 1):
        text = texts[at]
        if empties[at] and text == "":
            pending += 1
            continue
        more = text[:1] in (" ", "\t")
        if not have_previous:
            out += text
        elif not previous_more and not more and pending == 0:
            out += " " + text
        elif not previous_more and not more:
            out += "\n" * pending + text
        else:
            out += "\n" * (pending + 1) + text
        have_previous = True
        previous_more = more
        pending = 0
    if chomping == "strip":
        return out
    out += "\n"
    if chomping == "keep":
        for text in texts[last + 1 :]:
            out += text + "\n"
    return out


def _consume_balanced_flow(raw_after: str, label: str) -> None:
    """Consume one single-line balanced flow collection for an ignored key.

    Quote-aware (``''`` escapes, backslash escapes) and nesting-aware for both
    bracket kinds; a ``#`` outside quotes ends the line. Unbalanced,
    mismatched, unterminated, or multi-line flow refuses.
    """
    pos = 0
    while pos < len(raw_after) and raw_after[pos] in (" ", "\t"):
        pos += 1
    stack: list[str] = []
    cursor = pos
    in_single = False
    in_double = False
    escaped = False
    end = -1
    while cursor < len(raw_after):
        char = raw_after[cursor]
        if in_single:
            if char == "'":
                if cursor + 1 < len(raw_after) and raw_after[cursor + 1] == "'":
                    cursor += 2
                    continue
                in_single = False
            cursor += 1
            continue
        if in_double:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                in_double = False
            cursor += 1
            continue
        if char == "'":
            in_single = True
        elif char == '"':
            in_double = True
        elif char in "[{":
            stack.append(char)
        elif char in "]}":
            if not stack:
                raise SourceError(
                    CODE_MEMBER_INVALID,
                    f"Skill member {label} SKILL.md frontmatter has an "
                    "unbalanced flow collection",
                )
            opener = stack.pop()
            if (opener == "[" and char != "]") or (opener == "{" and char != "}"):
                raise SourceError(
                    CODE_MEMBER_INVALID,
                    f"Skill member {label} SKILL.md frontmatter has a "
                    "mismatched flow collection",
                )
            if not stack:
                end = cursor + 1
                break
        elif char == "#" and cursor > pos and raw_after[cursor - 1] in (" ", "\t"):
            break
        cursor += 1
    if in_single or in_double or end < 0:
        raise SourceError(
            CODE_MEMBER_INVALID,
            f"Skill member {label} SKILL.md frontmatter has an unbalanced "
            "flow collection",
        )
    _check_scalar_trailer(raw_after, end, label)


def parse_frontmatter_scalar(raw: str, label: str) -> str:
    """Parse one supported frontmatter scalar value to a string.

    ``raw`` is the raw value text after the ``key:`` separator (leading
    spaces/tabs included). The tokenizer is an explicit state machine:

    1. Skip leading spaces/tabs.
    2. Empty, or ``#`` first (comment-only/absent): refuse (null, not a
       string).
    3. ``'``: single-quoted scalar (``''`` is an escaped quote); after the
       closing quote only spaces/tabs may follow, and a ``#`` comment is
       admitted only when at least one SPACE or TAB precedes it
       (``'x'#c`` refuses); anything else refuses. Content may be empty
       (the required-field gate refuses it for ``name``/``description``).
    4. ``"``: double-quoted scalar with the complete YAML 1.2.2 section 5.7
       escape alphabet (``\\"``, ``\\\\``, ``\\/``, ``\\n``, ``\\t`` and
       backslash + literal TAB, ``\\r``, ``\\0``, ``\\a``, ``\\b``, ``\\v``,
       ``\\f``, ``\\e``, backslash + SPACE, ``\\N``, ``\\_``, ``\\L``,
       ``\\P``, ``\\xXX``, ``\\uXXXX``, ``\\UXXXXXXXX`` -- surrogates and
       code points above U+10FFFF refuse); same closing/trailing rule as
       single-quoted (``"x"#c`` refuses).
    5. Any other leading indicator (``{ [ , ] } & * ! | > % @```):
       refuse as an unsupported node kind, never guessed. A leading ``-``
       followed by a space/tab or end of line is a sequence entry, not a
       string: refuse; a leading ``?`` followed by a space/tab or end of
       line is an explicit mapping key entry, not a string: refuse. A lone
       ``=`` is a tag handle, not a string: refuse.
    6. Plain scalar: ends at end of line or at the first ``#`` preceded by a
       space or tab; trailing spaces/tabs are stripped. The remainder must
       satisfy the plain-scalar grammar (no ``: ``/``:<TAB>`` mapping
       indicator inside, never ends with ``:``) BEFORE tag classification;
       then it is classified by REGEX ONLY against the YAML 1.2 core schema
       (null/bool/int/float): a match refuses as a non-string, everything
       else is the string. ``#`` inside quoted scalars never starts a
       comment, and ``#`` without a preceding space/tab stays plain text.

    Anything refused raises ``source_member_invalid``. Quoted scalars remain
    strings even when their content spells a core-schema token or carries a
    mapping/sequence indicator.
    """
    pos = 0
    length = len(raw)
    while pos < length and raw[pos] in (" ", "\t"):
        pos += 1
    if pos >= length:
        raise SourceError(
            CODE_MEMBER_INVALID,
            f"Skill member {label} SKILL.md frontmatter value is missing "
            "(null, not a string)",
        )
    first = raw[pos]
    if first == "#":
        raise SourceError(
            CODE_MEMBER_INVALID,
            f"Skill member {label} SKILL.md frontmatter value is comment-only "
            "(null, not a string)",
        )
    if first == "-" and (pos + 1 >= length or raw[pos + 1] in (" ", "\t")):
        raise SourceError(
            CODE_MEMBER_INVALID,
            f"Skill member {label} SKILL.md frontmatter value "
            f"{raw[pos:].strip(' \t')!r} is a sequence entry, not a string",
        )
    if first == "?" and (pos + 1 >= length or raw[pos + 1] in (" ", "\t")):
        raise SourceError(
            CODE_MEMBER_INVALID,
            f"Skill member {label} SKILL.md frontmatter value "
            f"{raw[pos:].strip(' \t')!r} is an explicit mapping key entry, not a string",
        )
    if first == "'":
        pos += 1
        out: list[str] = []
        closed = False
        while pos < length:
            char = raw[pos]
            if char == "'":
                if pos + 1 < length and raw[pos + 1] == "'":
                    out.append("'")
                    pos += 2
                    continue
                closed = True
                pos += 1
                break
            out.append(char)
            pos += 1
        if not closed:
            raise SourceError(
                CODE_MEMBER_INVALID,
                f"Skill member {label} SKILL.md frontmatter has an "
                "unterminated single-quoted string",
            )
        _check_scalar_trailer(raw, pos, label)
        return "".join(out)
    if first == '"':
        pos += 1
        out = []
        closed = False
        while pos < length:
            char = raw[pos]
            if char == '"':
                closed = True
                pos += 1
                break
            if char == "\\":
                if pos + 1 >= length:
                    raise SourceError(
                        CODE_MEMBER_INVALID,
                        f"Skill member {label} SKILL.md frontmatter has an "
                        "unterminated escape in a double-quoted string",
                    )
                esc = raw[pos + 1]
                mapped = _DOUBLE_QUOTED_ESCAPES.get(esc)
                if mapped is not None:
                    out.append(mapped)
                    pos += 2
                    continue
                if esc == "x":
                    digits = raw[pos + 2 : pos + 4]
                    if len(digits) != 2 or not _is_hex_digits(digits):
                        raise SourceError(
                            CODE_MEMBER_INVALID,
                            f"Skill member {label} SKILL.md frontmatter has an "
                            "invalid \\x escape in a double-quoted string",
                        )
                    out.append(_chr_from_hex_digits(digits, label))
                    pos += 4
                    continue
                if esc == "u":
                    digits = raw[pos + 2 : pos + 6]
                    if len(digits) != 4 or not _is_hex_digits(digits):
                        raise SourceError(
                            CODE_MEMBER_INVALID,
                            f"Skill member {label} SKILL.md frontmatter has an "
                            "invalid \\u escape in a double-quoted string",
                        )
                    out.append(_chr_from_hex_digits(digits, label))
                    pos += 6
                    continue
                if esc == "U":
                    digits = raw[pos + 2 : pos + 10]
                    if len(digits) != 8 or not _is_hex_digits(digits):
                        raise SourceError(
                            CODE_MEMBER_INVALID,
                            f"Skill member {label} SKILL.md frontmatter has an "
                            "invalid \\U escape in a double-quoted string",
                        )
                    out.append(_chr_from_hex_digits(digits, label))
                    pos += 10
                    continue
                raise SourceError(
                    CODE_MEMBER_INVALID,
                    f"Skill member {label} SKILL.md frontmatter has an "
                    f"unsupported escape {raw[pos:pos + 2]!r} in a double-quoted string",
                )
            out.append(char)
            pos += 1
        if not closed:
            raise SourceError(
                CODE_MEMBER_INVALID,
                f"Skill member {label} SKILL.md frontmatter has an "
                "unterminated double-quoted string",
            )
        _check_scalar_trailer(raw, pos, label)
        return "".join(out)
    if first in _UNSUPPORTED_VALUE_STARTS:
        raise SourceError(
            CODE_MEMBER_INVALID,
            f"Skill member {label} SKILL.md frontmatter value {raw[pos:].strip(' \t')!r} "
            "is an unsupported node kind, not a string",
        )
    end = length
    for cursor in range(pos, length):
        if raw[cursor] == "#" and cursor > pos and raw[cursor - 1] in (" ", "\t"):
            end = cursor
            break
    plain = raw[pos:end].rstrip(" \t")
    if not plain:
        raise SourceError(
            CODE_MEMBER_INVALID,
            f"Skill member {label} SKILL.md frontmatter value is missing "
            "(null, not a string)",
        )
    if plain == "=":
        raise SourceError(
            CODE_MEMBER_INVALID,
            f"Skill member {label} SKILL.md frontmatter value '=' "
            "is a tag handle, not a string",
        )
    if ": " in plain or ":\t" in plain or plain.endswith(":"):
        raise SourceError(
            CODE_MEMBER_INVALID,
            f"Skill member {label} SKILL.md frontmatter value {plain!r} "
            "is not a plain scalar here (it carries a mapping indicator)",
        )
    if _PLAIN_NULL_RE.match(plain):
        raise SourceError(
            CODE_MEMBER_INVALID,
            f"Skill member {label} SKILL.md frontmatter value {plain!r} "
            "is null, not a string",
        )
    if _PLAIN_BOOL_RE.match(plain):
        raise SourceError(
            CODE_MEMBER_INVALID,
            f"Skill member {label} SKILL.md frontmatter value {plain!r} "
            "is a boolean, not a string",
        )
    for pattern in _PLAIN_INT_RES:
        if pattern.match(plain):
            raise SourceError(
                CODE_MEMBER_INVALID,
                f"Skill member {label} SKILL.md frontmatter value {plain!r} "
                "is an integer, not a string",
            )
    for pattern in _PLAIN_FLOAT_RES:
        if pattern.match(plain):
            raise SourceError(
                CODE_MEMBER_INVALID,
                f"Skill member {label} SKILL.md frontmatter value {plain!r} "
                "is a float, not a string",
            )
    return plain


def _check_scalar_trailer(raw: str, pos: int, label: str) -> None:
    """Check the trailer after a value: spaces/tabs, then optionally ``#...``.

    A trailing ``#`` comment is admitted only when at least one SPACE or TAB
    separates it from the value token (``"x"#c`` refuses, matching the
    plain-scalar and block-header comment rules and YAML 1.2 separation
    spaces): ``#`` at ``pos`` with no gap is trailing garbage, not a comment.
    """
    cursor = pos
    while cursor < len(raw) and raw[cursor] in (" ", "\t"):
        cursor += 1
    if cursor >= len(raw):
        return
    if raw[cursor] == "#" and cursor > pos:
        return
    raise SourceError(
        CODE_MEMBER_INVALID,
        f"Skill member {label} SKILL.md frontmatter has trailing content "
        f"{raw[pos:]!r} after a scalar value",
    )


def _is_empty_or_comment(raw_after: str) -> bool:
    """Return whether a ``key:`` value is empty or comment-only."""
    pos = 0
    while pos < len(raw_after) and raw_after[pos] in (" ", "\t"):
        pos += 1
    return pos >= len(raw_after) or raw_after[pos] == "#"


def _is_hex_digits(text: str) -> bool:
    return len(text) > 0 and all(char in _HEX_VALUES for char in text)


def _chr_from_hex_digits(digits: str, label: str) -> str:
    value = 0
    for char in digits:
        value = value * 16 + _HEX_VALUES[char]
    if value > 1114111:
        raise SourceError(
            CODE_MEMBER_INVALID,
            f"Skill member {label} SKILL.md frontmatter has a code point "
            "above U+10FFFF in a double-quoted string",
        )
    if 0xD800 <= value <= 0xDFFF:
        raise SourceError(
            CODE_MEMBER_INVALID,
            f"Skill member {label} SKILL.md frontmatter has a surrogate "
            "code point in a double-quoted string escape: lone surrogates "
            "are not Unicode scalar values",
        )
    return chr(value)


def _parse_flow_list(raw_after: str, label: str) -> list[str]:
    """Parse a ``triggers`` flow list (quote-aware, trailing comment allowed)."""
    start = 0
    while start < len(raw_after) and raw_after[start] in (" ", "\t"):
        start += 1
    pos = start + 1
    in_single = False
    in_double = False
    escaped = False
    closing = -1
    while pos < len(raw_after):
        char = raw_after[pos]
        if in_single:
            if char == "'":
                if pos + 1 < len(raw_after) and raw_after[pos + 1] == "'":
                    pos += 2
                    continue
                in_single = False
            pos += 1
            continue
        if in_double:
            if escaped:
                escaped = False
                pos += 1
                continue
            if char == "\\":
                escaped = True
                pos += 1
                continue
            if char == '"':
                in_double = False
            pos += 1
            continue
        if char == "'":
            in_single = True
        elif char == '"':
            in_double = True
        elif char == "]":
            closing = pos
            break
        pos += 1
    if in_single or in_double or closing < 0:
        raise SourceError(
            CODE_MEMBER_INVALID,
            f"Skill member {label} SKILL.md frontmatter has a malformed flow list",
        )
    inner = raw_after[start + 1 : closing]
    _check_scalar_trailer(raw_after, closing + 1, label)
    if not inner.strip(" \t"):
        return []
    parts = _split_flow_items(inner)
    return [parse_frontmatter_scalar(part, label) for part in parts]


def _split_flow_items(inner: str) -> list[str]:
    """Split flow-list items on commas outside quoted scalars."""
    parts: list[str] = []
    current: list[str] = []
    in_single = False
    in_double = False
    escaped = False
    pos = 0
    while pos < len(inner):
        char = inner[pos]
        if in_single:
            current.append(char)
            if char == "'":
                if pos + 1 < len(inner) and inner[pos + 1] == "'":
                    current.append("'")
                    pos += 2
                    continue
                in_single = False
            pos += 1
            continue
        if in_double:
            current.append(char)
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                in_double = False
            pos += 1
            continue
        if char == "'":
            in_single = True
            current.append(char)
        elif char == '"':
            in_double = True
            current.append(char)
        elif char == ",":
            parts.append("".join(current))
            current = []
        else:
            current.append(char)
        pos += 1
    parts.append("".join(current))
    return parts
