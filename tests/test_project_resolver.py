from __future__ import annotations

from conftest import make_project, run, write_skillfile
from csk import project_resolver


def test_resolve_current_worktree_alias_from_skillfile_project_alias_and_task_branch(tmp_path):
    project = make_project(tmp_path)
    write_skillfile(
        project,
        {
            "schema_version": 1,
            "project": {"alias": "partners-ios"},
            "skills": [],
        },
    )
    run(["git", "checkout", "-b", "feature/PMA-23523-current-dir-install"], project)
    (project / "subdir").mkdir()

    resolved = project_resolver.resolve(project / "subdir")

    assert resolved.project_alias == "partners-ios"
    assert resolved.checkout_alias == f"partners-ios-pma-23523-{resolved.path_hash}"
    assert resolved.root == project
    assert resolved.skillfile == project / "Skillfile.json"
    assert len(resolved.path_hash) == 4


def test_resolve_shared_checkout_uses_logical_alias(tmp_path):
    project = make_project(tmp_path)
    write_skillfile(project, {"schema_version": 1, "project": {"alias": "partners-ios"}, "skills": []})

    resolved = project_resolver.resolve(project)

    assert resolved.branch == "main"
    assert resolved.checkout_alias == "partners-ios"


def test_resolve_feature_branch_without_task_uses_stable_path_hash(tmp_path):
    project = make_project(tmp_path)
    write_skillfile(project, {"schema_version": 1, "project": {"alias": "Partners iOS"}, "skills": []})
    run(["git", "checkout", "-b", "feature/local-experiment"], project)

    resolved = project_resolver.resolve(project)

    assert resolved.project_alias == "partners-ios"
    assert resolved.checkout_alias == f"partners-ios-worktree-{resolved.path_hash}"
    assert len(resolved.path_hash) == 4


def test_resolve_uses_configurable_task_pattern(tmp_path):
    project = make_project(tmp_path)
    write_skillfile(project, {"schema_version": 1, "project": {"alias": "partners-ios"}, "skills": []})
    run(["git", "checkout", "-b", "feature/pma_23523"], project)

    resolved = project_resolver.resolve(project, worktree_alias_pattern=r"[a-z]+_[0-9]+")

    assert resolved.task_id == "pma_23523"
    assert resolved.checkout_alias == f"partners-ios-pma_23523-{resolved.path_hash}"
