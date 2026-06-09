from __future__ import annotations

from conftest import commit_all, make_skill_repo, run, write_files
from csk import git_ops


def test_resolve_tag_branch_and_revision(skills_root):
    repo, commit = make_skill_repo(skills_root, "skill-a", tag="v1")
    run(["git", "checkout", "-b", "experiment"], repo)
    write_files(repo, {"note.txt": "branch\n"})
    branch_commit = commit_all(repo, "branch")

    assert git_ops.resolve_ref(repo, "tag", "v1").commit == commit
    assert git_ops.resolve_ref(repo, "branch", "experiment").commit == branch_commit
    assert git_ops.resolve_ref(repo, "revision", branch_commit[:8]).commit == branch_commit


def test_branch_without_remote_is_supported(skills_root):
    repo, _ = make_skill_repo(skills_root, "skill-a")
    run(["git", "checkout", "-b", "local-only"], repo)
    commit = git_ops.resolve_ref(repo, "branch", "local-only").commit
    assert len(commit) == 40



def test_clone_repo_rejects_option_like_url(tmp_path):
    import pytest

    with pytest.raises(git_ops.GitError, match="suspicious"):
        git_ops.clone_repo("--upload-pack=touch pwned", tmp_path / "dst")
    with pytest.raises(git_ops.GitError, match="suspicious"):
        git_ops.clone_repo("  ", tmp_path / "dst")
    assert not (tmp_path / "dst").exists()


def test_clone_repo_blocks_ext_transport(tmp_path):
    import pytest

    marker = tmp_path / "pwned"
    with pytest.raises(git_ops.GitError, match="clone failed"):
        git_ops.clone_repo(f"ext::sh -c 'touch {marker}'", tmp_path / "dst")
    assert not marker.exists()


def test_clone_repo_clones_local_repo(skills_root, tmp_path):
    repo, _ = make_skill_repo(skills_root, "skill-a", tag="v1")
    destination = tmp_path / "cloned"
    git_ops.clone_repo(str(repo), destination)
    assert (destination / ".git").exists()
