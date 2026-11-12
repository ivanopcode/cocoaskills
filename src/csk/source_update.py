"""Plan whole-root, fetch-only updates over clones already under skills_root.

``csk update`` never clones. It considers every immediate clone in
skills_root, symlinked clones included, plus every declared source path that
already has a clone, and applies the registered legacy declarations to them.
A declared source without a clone is reported as skipped. Planning is local:
no network traffic and no repository writes happen before the caller fetches.

A failure that belongs to one clone (source policy, repository identity,
URL spellings that disagree, an unusable stored origin, an unreadable
dependency spec) refuses that clone only; the others are still fetched and
the command exits nonzero. A failure that hides which clones are declared (an
unreadable registered Skillfile, hybrid or global manifest) or that affects
every Git command (a typed trust refusal) refuses the whole run before any
fetch.

The same registry scan feeds the canonical-URL rule (:mod:`csk.canonical_urls`)
for ``csk upgrade`` and ``csk check --canonical-urls``, so every command reaches
the same spelling verdict on the same state.
"""

from __future__ import annotations

import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

from . import canonical_urls, closure, git_ops, global_install, hybrid, manifest, skillspec
from .config import GlobalConfig
from .source_identity import SourceIdentityError, canonical_source_identity

UpdateAction = Literal["fetch", "refused", "skipped"]


@dataclass(frozen=True)
class UpdateItem:
    """One planned outcome; ``source`` names the clone relative to skills_root."""

    source: str
    action: UpdateAction
    repo: Path | None = None
    endpoint: str | None = None
    detail: str | None = None
    # Where the endpoint spelling is declared, for fetch failure messages.
    places: tuple[str, ...] = ()


@dataclass(frozen=True)
class _Declaration:
    name: str
    source: str
    git: str | None
    ref: manifest.SkillRef
    base: Path
    chain: str
    place: str = ""
    root: str | None = None
    # False for dependencies read from cached HEAD because the declared ref is
    # not cached yet: they locate clones and are checked for identity and
    # policy, but they declare no spelling and select no endpoint.
    at_declared_ref: bool = True

    def spelling(self) -> str | None:
        if self.git is None or not self.at_declared_ref:
            return None
        try:
            return git_ops.declared_fetch_url(self.git, self.base)
        except git_ops.GitError:
            return None


@dataclass
class _Clone:
    repo: Path
    source: str
    declarations: list[_Declaration] = field(default_factory=list)
    error: str | None = None


_NOT_INSTALLED = (
    "no clone under skills_root; csk update fetches existing clones only, "
    "run csk install or csk upgrade to acquire it"
)
_NOT_A_CLONE = (
    "path exists but is not a Git clone; csk update fetches existing clones only, "
    "run csk install or csk upgrade to report or repair it"
)


class _Inventory:
    """Existing clones keyed by directory identity, so aliases fetch once."""

    def __init__(self, skills_root: Path) -> None:
        self.skills_root = skills_root
        self.clones: dict[tuple[int, int], _Clone] = {}
        self.by_source: dict[str, _Clone | None] = {}
        self.other: dict[str, UpdateItem] = {}
        for name, repo in git_ops.skill_clones(skills_root).items():
            self._register(name, repo)

    def _register(self, source: str, repo: Path) -> _Clone | None:
        try:
            status = repo.stat()
        except OSError as exc:
            self.other[source] = UpdateItem(source, "refused", repo, detail=f"cannot inspect clone: {exc}")
            self.by_source[source] = None
            return None
        key = (status.st_dev, status.st_ino)
        clone = self.clones.get(key)
        if clone is None:
            clone = self.clones[key] = _Clone(repo, source)
        self.by_source[source] = clone
        return clone

    def locate(self, source: str) -> _Clone | None:
        if source in self.by_source:
            return self.by_source[source]
        try:
            repo = git_ops.skill_source_path(self.skills_root, source)
        except git_ops.GitError as exc:
            self.other[source] = UpdateItem(source, "refused", detail=str(exc))
            self.by_source[source] = None
            return None
        if not repo.is_dir() or not (repo / ".git").exists():
            detail = _NOT_A_CLONE if repo.is_dir() else _NOT_INSTALLED
            self.other[source] = UpdateItem(source, "skipped", repo, detail=detail)
            self.by_source[source] = None
            return None
        return self._register(source, repo)


def _direct(decl: manifest.SkillDecl, base: Path, label: str, place: str, root: str) -> _Declaration:
    return _Declaration(
        decl.name, decl.source, decl.git, decl.ref, base, f"{label} -> {decl.name}", place=place, root=root,
    )


@dataclass(frozen=True)
class _Root:
    root: str
    declarations: list[_Declaration]


def _roots(config: GlobalConfig, *, unreadable: list[str] | None = None) -> list[_Root]:
    """Direct declarations per closure root, in the installer's order.

    Without ``unreadable`` a registered Skillfile that cannot be read raises;
    with it, the failure is recorded there and that project is left out.
    """
    csk_home = config.path.parent
    try:
        hybrid_decls = hybrid.load_hybrid_decls(csk_home)
    except hybrid.HybridError as exc:
        if unreadable is None:
            raise
        unreadable.append(f"hybrid declarations: {exc}")
        hybrid_decls = []
    hybrid_base = hybrid.hybrid_root(csk_home)
    hybrid_file = hybrid.hybrid_manifest_path(csk_home)
    roots: list[_Root] = []
    for project in config.projects.values():
        try:
            loaded = manifest.load_manifest(project.path)
        except (manifest.ManifestError, ValueError, OSError) as exc:
            if unreadable is None:
                raise
            unreadable.append(f"project {project.alias}: {exc}")
            continue
        if loaded is None or loaded.schema_version != 1:
            # Source-schema projects own private acquisitions and never
            # select a skills-root remote.
            continue
        names = {decl.name for decl in loaded.skills}
        aliases = tuple(value for value in (project.alias, project.project_alias, loaded.project_alias) if value)
        root = canonical_urls.project_root_id(project.path)
        direct = [
            _direct(decl, loaded.path.parent, project.alias, f"{loaded.path} (project {project.alias})", root)
            for decl in loaded.skills
        ]
        direct.extend(
            _direct(
                item.decl, hybrid_base, f"hybrid for {project.alias}",
                f"{hybrid_file} (hybrid declaration, for project {project.alias})", root,
            )
            for item in hybrid_decls
            if item.decl.name not in names
            and hybrid.applies_to_project(item, aliases=aliases, project_path=project.path)
        )
        roots.append(_Root(root, direct))
    try:
        global_manifest = manifest.load_manifest(global_install.global_root(csk_home), scope="global")
    except (manifest.ManifestError, ValueError, OSError) as exc:
        if unreadable is None:
            raise
        unreadable.append(f"global Skillfile: {exc}")
        global_manifest = None
    if global_manifest is not None:
        roots.append(_Root(canonical_urls.GLOBAL_ROOT_ID, [
            _direct(decl, global_manifest.path.parent, "global", f"{global_manifest.path} (global)",
                    canonical_urls.GLOBAL_ROOT_ID)
            for decl in global_manifest.skills
        ]))
    return roots


def _inspected_commit(repo: Path, ref: manifest.SkillRef) -> tuple[str, bool] | None:
    """Return the commit to inspect and whether it is the declared ref itself."""
    try:
        return git_ops.resolve_ref(repo, ref.kind, ref.value).commit, True
    except git_ops.GitError as exc:
        if exc.code is not None:
            raise
    # Update may be needed to resolve this ref. Inspect cached HEAD instead,
    # best effort; upgrade resolves the graph of the newly fetched ref.
    cached = git_ops.git(repo, ["rev-parse", "--verify", "HEAD^{commit}"], check=False)
    return None if cached.returncode else (cached.stdout.strip(), False)


RequirementStatus = Literal["declared", "head", "unresolved", "unreadable"]


def _requirements(
    clone: _Clone,
    ref: manifest.SkillRef,
    specs: dict[tuple[int, str], tuple[skillspec.SkillRequirement, ...]],
    *,
    lenient: bool = False,
) -> tuple[tuple[skillspec.SkillRequirement, ...], RequirementStatus]:
    """Dependencies at the declared ref, or best effort at cached HEAD.

    The status says where they come from: the declared ref, the cached HEAD
    fallback, nowhere (nothing resolvable), or an unreadable spec. An
    unreadable spec at the declared ref refuses the clone, unless
    ``lenient`` (views that never fetch) only records the status.
    """
    commit = ""
    declared_ref = True
    failure: Exception
    try:
        inspected = _inspected_commit(clone.repo, ref)
        if inspected is None:
            return (), "unresolved"
        commit, declared_ref = inspected
        status: RequirementStatus = "declared" if declared_ref else "head"
        key = (id(clone), commit)
        if key in specs:
            return specs[key], status
        with tempfile.TemporaryDirectory(prefix="csk-update-source-") as temporary:
            snapshot = Path(temporary) / "source"
            git_ops.archive(clone.repo, commit, snapshot)
            spec = skillspec.load_skill_spec(snapshot)
    except git_ops.GitError as exc:
        if exc.code is not None:
            raise
        failure = exc
    except (skillspec.SkillSpecError, OSError, ValueError) as exc:
        failure = exc
    else:
        specs[key] = tuple(spec.requirements.values())
        return specs[key], status
    if not declared_ref:
        # The declared ref is not cached yet; an unreadable HEAD says nothing
        # about it, exactly like an unresolvable HEAD.
        return (), "unresolved"
    if not lenient and clone.error is None:
        at = f" at cached commit {commit[:12]}" if commit else ""
        clone.error = (
            f"cannot read its dependency declarations{at}: {failure}; "
            "correct the declared ref or its skill spec"
        )
    return (), "unreadable"


def _clone_identity(clone: _Clone, selected: git_ops.FetchSource | None, endpoint: str | None) -> str | None:
    """Repository identity of what this clone is fetched from."""
    if endpoint is not None:
        return canonical_urls.repository_identity(endpoint)
    if selected is None:
        return None
    stored = selected.stored_url
    try:
        if canonical_source_identity(stored) is None:
            # Git reads a stored relative path against the clone.
            stored = str(git_ops._local_source_path(stored, clone.repo))
    except (SourceIdentityError, git_ops.GitError, OSError):
        return None
    return canonical_urls.repository_identity(stored)


def _preflight(config: GlobalConfig, clone: _Clone, spellings: canonical_urls.SpellingIndex) -> UpdateItem:
    def refused(detail: str) -> UpdateItem:
        return UpdateItem(clone.source, "refused", clone.repo, detail=detail)

    if clone.error is not None:
        return refused(clone.error)
    try:
        targets: list[str] = []
        checked: set[str] = set()
        for decl in clone.declarations:
            if decl.git is None:
                continue
            closure._gate_source(config, decl.name, decl.git, decl.chain)
            target = git_ops.declared_fetch_url(decl.git, decl.base)
            if target not in checked:
                git_ops.validate_repo_source(clone.repo, target)
                checked.add(target)
            if decl.at_declared_ref and target not in targets:
                targets.append(target)
        # Every declared target has the stored origin's identity by now, so
        # two targets are always two spellings of one repository; the index
        # names every place that declares each of them.
        endpoint = targets[0] if targets else None
        selected = git_ops.validate_repo_source(clone.repo, endpoint)
        identity = _clone_identity(clone, selected, endpoint)
        conflict = spellings.conflict(identity)
        if conflict is not None:
            raise canonical_urls.SpellingConflictError(conflict)
        places = tuple(sorted(set(spellings.spellings(identity or "").get(endpoint or "", ()))))
        if endpoint is None and selected is not None:
            closure._gate_source(config, clone.source, selected.stored_url, "whole-root update")
    except git_ops.GitError as exc:
        if exc.code is not None:
            raise
        return refused(str(exc))
    except (closure.ClosureError, ValueError) as exc:
        return refused(str(exc))
    return UpdateItem(clone.source, "fetch", clone.repo, endpoint=endpoint, places=places)


@dataclass
class RegistryScan:
    """Registry declarations mapped onto existing clones, without network or writes."""

    inventory: _Inventory
    declarations: list[_Declaration]
    unreadable: list[str]
    not_checked: list[tuple[str | None, str]]
    roots: int = 0

    def declared(self) -> list[canonical_urls.Declared]:
        return [
            canonical_urls.Declared(spelling, decl.place, decl.root)
            for decl in self.declarations
            if (spelling := decl.spelling()) is not None
        ]

    def index(self) -> canonical_urls.SpellingIndex:
        return canonical_urls.SpellingIndex(self.declared())


def scan_registry(config: GlobalConfig, *, lenient: bool = False) -> RegistryScan:
    """Resolve every registered declaration, transitive ones included.

    Transitive requirements resolve to the clone the closure would use: a
    name declared directly by the same root keeps that declaration's source.
    Dependency declarations come from cached declared refs, or cached HEAD
    when a new ref is not available yet (those declare no spelling).

    ``lenient`` (views that never fetch) records unreadable registered
    Skillfiles and dependency specs instead of raising or refusing clones.
    """
    unreadable: list[str] = []
    roots = _roots(config, unreadable=unreadable if lenient else None)
    inventory = _Inventory(config.skills_root)
    specs: dict[tuple[int, str], tuple[skillspec.SkillRequirement, ...]] = {}
    declarations: list[_Declaration] = []
    not_checked: list[tuple[str | None, str]] = []
    for root in roots:
        sources: dict[str, str] = {}
        for decl in root.declarations:
            sources.setdefault(decl.name, decl.source)
        pending = list(root.declarations)
        expanded: set[str] = set()
        while pending:
            decl = pending.pop(0)
            declarations.append(decl)
            clone = inventory.locate(decl.source)
            if clone is None:
                not_checked.append((decl.root, f"dependencies of {decl.name}: no clone under skills_root"))
                continue
            clone.declarations.append(decl)
            if decl.name in expanded:
                continue
            expanded.add(decl.name)
            requirements, status = _requirements(clone, decl.ref, specs, lenient=lenient)
            if status != "declared":
                reason = {
                    "head": "the ref is not cached; cached HEAD was read to locate clones only",
                    "unresolved": "the ref is not cached",
                    "unreadable": "its skill spec cannot be read",
                }[status]
                not_checked.append((
                    decl.root, f"dependencies of {decl.name} at {decl.ref.kind} {decl.ref.value}: {reason}",
                ))
            pending.extend(
                _Declaration(
                    name=requirement.name,
                    source=sources.setdefault(requirement.name, requirement.name),
                    git=requirement.git,
                    ref=manifest.SkillRef(requirement.ref_kind, requirement.ref_value),
                    base=decl.base,
                    chain=f"{decl.chain} -> {requirement.name}",
                    place=canonical_urls.dependency_place(
                        decl.name, decl.ref.kind, decl.ref.value, f"{decl.chain} -> {requirement.name}",
                    ),
                    root=decl.root,
                    at_declared_ref=status == "declared" and decl.at_declared_ref,
                )
                for requirement in requirements
            )
    return RegistryScan(inventory, declarations, unreadable, not_checked, len(roots))


def registry_declarations(config: GlobalConfig) -> list[canonical_urls.Declared]:
    """Spellings declared across the registry, for :class:`canonical_urls.SpellingGuard`.

    A registered project whose Skillfile cannot be read, or a dependency
    spec that cannot be read, contributes nothing here; ``csk update`` and
    ``csk check --canonical-urls`` report those failures themselves.
    """
    from .skillfile_deprecations import silenced

    with silenced():
        return scan_registry(config, lenient=True).declared()


def plan_update(config: GlobalConfig) -> list[UpdateItem]:
    """Resolve, without network or writes, what ``csk update`` does per clone."""
    scan = scan_registry(config)
    spellings = scan.index()
    items = [_preflight(config, clone, spellings) for clone in scan.inventory.clones.values()]
    items.extend(scan.inventory.other.values())
    return sorted(items, key=lambda item: item.source)


@dataclass
class CanonicalUrlReport:
    """Result of ``csk check --canonical-urls`` for one root or the registry."""

    spellings: dict[str, list[str]]
    repositories: int
    conflicts: list[str]
    unreadable: list[str]
    not_checked: list[str]
    # Readable closure roots scanned: registered schema-1 projects and the
    # global Skillfile.
    roots: int = 0


def canonical_url_report(config: GlobalConfig, *, root: str | None) -> CanonicalUrlReport:
    """Spelling disagreements that involve ``root`` (or any, for None).

    The whole registry is scanned either way, so a target Skillfile is
    compared with every registered project, the global and hybrid
    declarations, and the cached dependency specs of all of them.
    """
    scan = scan_registry(config, lenient=True)
    index = scan.index()
    scoped: dict[str, list[str]] = {}
    for decl in scan.declarations:
        spelling = decl.spelling()
        if spelling is not None and (root is None or decl.root == root):
            places = scoped.setdefault(spelling, [])
            if decl.place not in places:
                places.append(decl.place)
    identities = sorted({
        identity for spelling in scoped if (identity := canonical_urls.repository_identity(spelling)) is not None
    })
    conflicts = [message for identity in identities if (message := index.conflict(identity)) is not None]
    not_checked = sorted({text for owner, text in scan.not_checked if root is None or owner == root})
    return CanonicalUrlReport(scoped, len(identities), conflicts, list(scan.unreadable), not_checked, scan.roots)
