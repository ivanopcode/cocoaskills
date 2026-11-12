"""Plan whole-root, fetch-only updates over clones already under skills_root.

``csk update`` never clones. It considers every immediate clone in
skills_root, symlinked clones included, plus every declared source path that
already has a clone, and applies the registered legacy declarations to them.
A declared source without a clone is reported as skipped. Planning is local:
no network traffic and no repository writes happen before the caller fetches.

A failure that belongs to one clone (source policy, repository identity,
conflicting endpoints, an unusable stored origin, an unreadable dependency
spec) refuses that clone only; the others are still fetched and the command
exits nonzero. A failure that hides which clones are declared (an unreadable
registered Skillfile, hybrid or global manifest) or that affects every Git
command (a typed trust refusal) refuses the whole run before any fetch.
"""

from __future__ import annotations

import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

from . import closure, git_ops, global_install, hybrid, manifest, skillspec
from .config import GlobalConfig

UpdateAction = Literal["fetch", "refused", "skipped"]


@dataclass(frozen=True)
class UpdateItem:
    """One planned outcome; ``source`` names the clone relative to skills_root."""

    source: str
    action: UpdateAction
    repo: Path | None = None
    endpoint: str | None = None
    detail: str | None = None


@dataclass(frozen=True)
class _Declaration:
    name: str
    source: str
    git: str | None
    ref: manifest.SkillRef
    base: Path
    chain: str


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


def _direct(decl: manifest.SkillDecl, base: Path, label: str) -> _Declaration:
    return _Declaration(decl.name, decl.source, decl.git, decl.ref, base, f"{label} -> {decl.name}")


def _roots(config: GlobalConfig) -> list[list[_Declaration]]:
    """Direct declarations per closure root, in the installer's order."""
    csk_home = config.path.parent
    hybrid_decls = hybrid.load_hybrid_decls(csk_home)
    hybrid_base = hybrid.hybrid_root(csk_home)
    roots: list[list[_Declaration]] = []
    for project in config.projects.values():
        loaded = manifest.load_manifest(project.path)
        if loaded is None or loaded.schema_version != 1:
            # Source-schema projects own private acquisitions and never
            # select a skills-root remote.
            continue
        names = {decl.name for decl in loaded.skills}
        aliases = tuple(value for value in (project.alias, project.project_alias, loaded.project_alias) if value)
        direct = [_direct(decl, loaded.path.parent, project.alias) for decl in loaded.skills]
        direct.extend(
            _direct(item.decl, hybrid_base, f"hybrid for {project.alias}")
            for item in hybrid_decls
            if item.decl.name not in names
            and hybrid.applies_to_project(item, aliases=aliases, project_path=project.path)
        )
        roots.append(direct)
    global_manifest = manifest.load_manifest(global_install.global_root(csk_home), scope="global")
    if global_manifest is not None:
        roots.append([_direct(decl, global_manifest.path.parent, "global") for decl in global_manifest.skills])
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


def _requirements(
    clone: _Clone,
    ref: manifest.SkillRef,
    specs: dict[tuple[int, str], tuple[skillspec.SkillRequirement, ...]],
) -> tuple[skillspec.SkillRequirement, ...]:
    commit = ""
    declared_ref = True
    failure: Exception
    try:
        inspected = _inspected_commit(clone.repo, ref)
        if inspected is None:
            return ()
        commit, declared_ref = inspected
        key = (id(clone), commit)
        if key in specs:
            return specs[key]
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
        return specs[key]
    if not declared_ref:
        # The declared ref is not cached yet; an unreadable HEAD says nothing
        # about it, exactly like an unresolvable HEAD.
        return ()
    if clone.error is None:
        at = f" at cached commit {commit[:12]}" if commit else ""
        clone.error = (
            f"cannot read its dependency declarations{at}: {failure}; "
            "correct the declared ref or its skill spec"
        )
    return ()


def _preflight(config: GlobalConfig, clone: _Clone) -> UpdateItem:
    def refused(detail: str) -> UpdateItem:
        return UpdateItem(clone.source, "refused", clone.repo, detail=detail)

    if clone.error is not None:
        return refused(clone.error)
    try:
        targets: dict[str, _Declaration] = {}
        for decl in clone.declarations:
            if decl.git is None:
                continue
            closure._gate_source(config, decl.name, decl.git, decl.chain)
            target = git_ops.declared_fetch_url(decl.git, decl.base)
            if target not in targets:
                git_ops.validate_repo_source(clone.repo, target)
                targets[target] = decl
        if len(targets) > 1:
            # Same identity, different spellings: no endpoint is guessed.
            shown = ", ".join(repr(git_ops.display_url(target)) for target in targets)
            raise git_ops.GitError(
                f"Conflicting declared endpoints {shown}; align the project, global "
                "and dependency declarations before csk update"
            )
        endpoint = next(iter(targets), None)
        selected = git_ops.validate_repo_source(clone.repo, endpoint)
        if endpoint is None and selected is not None:
            closure._gate_source(config, clone.source, selected.stored_url, "whole-root update")
    except git_ops.GitError as exc:
        if exc.code is not None:
            raise
        return refused(str(exc))
    except (closure.ClosureError, ValueError) as exc:
        return refused(str(exc))
    return UpdateItem(clone.source, "fetch", clone.repo, endpoint=endpoint)


def plan_update(config: GlobalConfig) -> list[UpdateItem]:
    """Resolve, without network or writes, what ``csk update`` does per clone.

    Transitive requirements resolve to the clone the closure would use: a
    name declared directly by the same root keeps that declaration's source.
    Dependency declarations come from cached declared refs, or cached HEAD
    when a new ref is not available yet.
    """
    roots = _roots(config)
    inventory = _Inventory(config.skills_root)
    specs: dict[tuple[int, str], tuple[skillspec.SkillRequirement, ...]] = {}
    for direct in roots:
        sources: dict[str, str] = {}
        for decl in direct:
            sources.setdefault(decl.name, decl.source)
        pending = list(direct)
        expanded: set[str] = set()
        while pending:
            decl = pending.pop(0)
            clone = inventory.locate(decl.source)
            if clone is None:
                continue
            clone.declarations.append(decl)
            if decl.name in expanded:
                continue
            expanded.add(decl.name)
            pending.extend(
                _Declaration(
                    name=requirement.name,
                    source=sources.setdefault(requirement.name, requirement.name),
                    git=requirement.git,
                    ref=manifest.SkillRef(requirement.ref_kind, requirement.ref_value),
                    base=decl.base,
                    chain=f"{decl.chain} -> {requirement.name}",
                )
                for requirement in _requirements(clone, decl.ref, specs)
            )
    items = [_preflight(config, clone) for clone in inventory.clones.values()]
    items.extend(inventory.other.values())
    return sorted(items, key=lambda item: item.source)
