"""Canonical source URLs: one repository, exactly one declared spelling.

Skill clones under ``skills_root`` are shared by every registered project,
branch, worktree and dependency declaration, so a repository declared with two
spellings would make csk alternate the clone's origin between them, and one of
them typically redirects. csk therefore treats mixed spellings as an invalid
state and refuses to clone or fetch such a repository instead of guessing.

Repository identity (what counts as "one repository"):

* network URLs: :func:`csk.source_identity.canonical_source_identity`, i.e.
  lowercase host plus the case-sensitive path, ignoring the transport (scheme
  and user), trailing slashes and one trailing ``.git``. Different hosts or
  different paths are never merged; explicit ports, queries and fragments are
  refused by that parser;
* local sources: the resolved absolute path (relative paths are anchored at
  the directory of the declaring manifest).

Spelling: the exact fetch target csk would use, i.e. the declared string for
network URLs and the anchored absolute path for relative local paths. Two
declarations of one identity agree only when their spellings are equal.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass
from pathlib import Path

from . import git_ops
from .source_identity import SourceIdentityError, canonical_source_identity

DOC_HINT = "see 'Canonical source URLs' in the csk documentation"
CHECK_HINT = "Run 'csk check --canonical-urls --all' to list every disagreement"


class SpellingConflictError(git_ops.GitError):
    """One repository identity is declared with more than one spelling."""


@dataclass(frozen=True)
class Declared:
    """One declared spelling and where it is declared.

    ``root`` names the closure root (a registered project or the global
    manifest) that owns the declaration; a command acting on that root
    replaces the root's cached declarations with its live ones.
    """

    spelling: str
    place: str
    root: str | None = None


def project_root_id(path: Path) -> str:
    return f"project:{_resolved(path)}"


GLOBAL_ROOT_ID = "global"


def _resolved(path: Path) -> Path:
    try:
        return path.expanduser().resolve()
    except OSError:
        return path.expanduser().absolute()


def repository_identity(spelling: str) -> str | None:
    """Identity key of one spelling, or None when it cannot be identified.

    Unidentifiable spellings (malformed URLs) are refused by the source checks
    that run on every declaration; they never join a spelling comparison.
    """
    try:
        identity = canonical_source_identity(spelling)
    except SourceIdentityError:
        return None
    if identity is not None:
        return identity
    try:
        return f"local:{git_ops._local_source_path(spelling, Path.cwd())}"
    except (git_ops.GitError, OSError, ValueError):
        return None


def describe_identity(identity: str) -> str:
    return identity[len("local:"):] if identity.startswith("local:") else identity


def conflict_message(identity: str, spellings: dict[str, list[str]]) -> str:
    """One-line refusal naming every spelling, its declaring places, and the fix."""
    parts = []
    for spelling in sorted(spellings):
        places = "; ".join(sorted(set(spellings[spelling]))) or "unknown place"
        parts.append(f"{git_ops.display_url(spelling)!r} in {places}")
    return (
        f"repository {describe_identity(identity)} is declared with {len(spellings)} different URL "
        f"spellings: {' | '.join(parts)}. csk accepts exactly one canonical spelling per repository and "
        "does not guess: use the spelling the server serves without a redirect (normally the clone URL "
        "your Git host shows) in every listed place, including other branches and worktrees; dependency "
        "declarations change with a new tag of the declaring skill; remove registered projects you no "
        f"longer use from 'projects' in the csk config. {CHECK_HINT}; {DOC_HINT}"
    )


class SpellingIndex:
    """Declared spellings grouped by repository identity."""

    def __init__(self, entries: Iterable[Declared] = ()) -> None:
        self._spellings: dict[str, dict[str, list[str]]] = {}
        for entry in entries:
            self.add(entry.spelling, entry.place)

    def add(self, spelling: str, place: str) -> str | None:
        identity = repository_identity(spelling)
        if identity is None:
            return None
        places = self._spellings.setdefault(identity, {}).setdefault(spelling, [])
        if place not in places:
            places.append(place)
        return identity

    def identities(self) -> list[str]:
        return sorted(self._spellings)

    def spellings(self, identity: str) -> dict[str, list[str]]:
        return {key: list(value) for key, value in self._spellings.get(identity, {}).items()}

    def conflict(self, identity: str | None) -> str | None:
        if identity is None:
            return None
        spellings = self._spellings.get(identity, {})
        return conflict_message(identity, spellings) if len(spellings) > 1 else None

    def conflicts(self) -> list[tuple[str, str]]:
        return [
            (identity, message)
            for identity in self.identities()
            if (message := self.conflict(identity)) is not None
        ]


class SpellingGuard:
    """Per-command view used before csk clones or fetches a repository.

    It combines the registry's declarations (loaded lazily, once) with the live
    declarations of the closures this command builds. Declarations of the roots
    the command itself resolves are taken live, never from the cached registry
    view, so a root that is being upgraded speaks for itself with fresh data.
    """

    def __init__(
        self,
        load_registry: Callable[[], Sequence[Declared]] | None = None,
        *,
        label: str = "",
    ) -> None:
        self._load_registry = load_registry
        self._registry: Sequence[Declared] | None = None
        self._live: list[Declared] = []
        self._live_roots: set[str] = set()
        self.label = label
        self.chain_root = ""

    def enter_root(self, root: str | None, label: str, chain_root: str = "") -> None:
        """Start resolving one root; its cached registry entries stop counting.

        ``label`` decorates the root's manifest place ("project app") and
        ``chain_root`` starts its dependency chains ("app -> a -> b"), exactly
        as the registry scan writes them, so every command words a conflict
        identically.
        """
        if root is not None:
            self._live_roots.add(root)
        self.label = label
        self.chain_root = chain_root

    def note(self, spelling: str | None, place: str) -> None:
        if spelling is not None:
            self._live.append(Declared(spelling, place))

    def _index(self) -> SpellingIndex:
        if self._registry is None:
            self._registry = tuple(self._load_registry()) if self._load_registry is not None else ()
        index = SpellingIndex(
            entry for entry in self._registry if entry.root is None or entry.root not in self._live_roots
        )
        for entry in self._live:
            index.add(entry.spelling, entry.place)
        return index

    def require(self, identity: str | None) -> None:
        """Refuse a clone or fetch of ``identity`` when its spellings disagree."""
        if identity is None:
            return
        message = self._index().conflict(identity)
        if message is not None:
            raise SpellingConflictError(message)


def dependency_place(consumer: str, ref_kind: str, ref_value: str, chain: str) -> str:
    return f"the skill spec of '{consumer}' at {ref_kind} {ref_value} (via {chain})"
