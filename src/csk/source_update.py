"""Preflight declared legacy endpoints before whole-root, fetch-only updates."""

from __future__ import annotations

import tempfile
from pathlib import Path

from . import closure, git_ops, global_install, hybrid, manifest, skillspec
from .config import GlobalConfig


def declared_endpoints(config: GlobalConfig) -> dict[str, str]:
    """Resolve known legacy declarations without network or persistent writes.

    Source-schema projects own private acquisitions and never select a
    skills-root remote. Legacy update refreshes existing clones, not installs:
    inspect cached declared refs (or HEAD when a new ref is not available yet)
    for transitive declarations. Upgrade resolves the newly acquired graph.
    """
    pending: list[manifest.SkillDecl] = []
    hybrid_decls = hybrid.load_hybrid_decls(config.path.parent)
    for project in config.projects.values():
        loaded = manifest.load_manifest(project.path)
        if loaded is None or loaded.schema_version != 1:
            continue
        pending.extend(loaded.skills)
        names = {decl.name for decl in loaded.skills}
        aliases = tuple(value for value in (project.alias, project.project_alias, loaded.project_alias) if value)
        pending.extend(
            item.decl for item in hybrid_decls
            if item.decl.name not in names
            and hybrid.applies_to_project(item, aliases=aliases, project_path=project.path)
        )
    global_manifest = manifest.load_manifest(global_install.global_root(config.path.parent), scope="global")
    if global_manifest is not None:
        pending.extend(global_manifest.skills)

    endpoints: dict[str, str] = {}
    inspected: set[tuple[Path, str]] = set()
    while pending:
        decl = pending.pop(0)
        repo = config.skills_root / decl.source
        if decl.git is not None:
            closure._gate_source(config, decl.name, decl.git, "whole-root update")
            target = git_ops.declared_fetch_url(decl.git)
            if repo.exists():
                git_ops.validate_repo_source(repo, decl.git)
            previous = endpoints.get(decl.source)
            if previous is not None and previous != target:
                raise git_ops.GitError(
                    f"Conflicting declared endpoints for {decl.source}; "
                    "align the project, global and dependency declarations before csk update"
                )
            endpoints[decl.source] = target
        if not (repo / ".git").exists():
            continue
        try:
            commit = git_ops.resolve_ref(repo, decl.ref.kind, decl.ref.value).commit
        except git_ops.GitError as exc:
            if exc.code is not None:
                raise
            # Update may be needed to resolve this ref. Inspect cached HEAD
            # when resolution fails, but never suppress a typed trust refusal.
            cached = git_ops.git(repo, ["rev-parse", "--verify", "HEAD^{commit}"], check=False)
            if cached.returncode:
                continue
            commit = cached.stdout.strip()
        key = (repo.resolve(), commit)
        if key in inspected:
            continue
        inspected.add(key)
        with tempfile.TemporaryDirectory(prefix="csk-update-source-") as temporary:
            snapshot = Path(temporary) / "source"
            git_ops.archive(repo, commit, snapshot)
            spec = skillspec.load_skill_spec(snapshot)
        pending.extend(
            manifest.SkillDecl(
                name=item.name, source=item.name, git=item.git,
                ref=manifest.SkillRef(item.ref_kind, item.ref_value),
            )
            for item in spec.requirements.values()
        )

    # Preflight every clone before the first fetch, including declarations
    # without a git field and otherwise unreferenced, stored-remote clones.
    for repo in sorted(config.skills_root.iterdir()):
        if not repo.is_dir() or not (repo / ".git").exists():
            continue
        selected = git_ops.validate_repo_source(repo, endpoints.get(repo.name))
        if selected is not None:
            closure._gate_source(
                config, repo.name, endpoints.get(repo.name, selected.stored_url), "whole-root update",
            )
    return endpoints
