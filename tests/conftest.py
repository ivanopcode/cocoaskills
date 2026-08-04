from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest

from csk import deprecation, locking
from csk.config import GlobalConfig, ProjectConfig


E2E_CANDIDATE_SHA = "432eb2ee1fe2d6b271e37269f867c8851c325539"
E2E_CANDIDATE_MANIFEST_SHA256 = (
    "12e58b82579645ba1ccafba49d3e2dd3216005ddf37ae63c68a9fafd46773071"
)


def run(cmd: list[str], cwd: Path, *, check: bool = True) -> subprocess.CompletedProcess[str]:
    proc = subprocess.run(cmd, cwd=cwd, text=True, capture_output=True)
    if check and proc.returncode != 0:
        raise AssertionError(f"{cmd} failed in {cwd}\nstdout={proc.stdout}\nstderr={proc.stderr}")
    return proc


def init_git_repo(path: Path) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    run(["git", "init"], path)
    run(["git", "branch", "-M", "main"], path)
    run(["git", "config", "user.name", "Test User"], path)
    run(["git", "config", "user.email", "test@example.com"], path)
    return path


def commit_all(path: Path, message: str = "commit") -> str:
    run(["git", "add", "."], path)
    run(["git", "commit", "-m", message], path)
    return run(["git", "rev-parse", "HEAD"], path).stdout.strip()


def write_files(root: Path, files: dict[str, str | bytes]) -> None:
    for rel, content in files.items():
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        if isinstance(content, bytes):
            path.write_bytes(content)
        else:
            path.write_text(content, encoding="utf-8")


@pytest.fixture
def skills_root(tmp_path: Path) -> Path:
    root = tmp_path / "skills"
    root.mkdir()
    return root


@pytest.fixture
def csk_home(tmp_path: Path) -> Path:
    home = tmp_path / ".cocoaskills"
    # Provision the way csk provisions its own home: a plain mkdir does not
    # give the home the private state the protected build cache requires on
    # Windows, where new objects belong to the token owner.
    locking.provision_new_manager_home(home)
    return home


def make_skill_repo(
    skills_root: Path,
    name: str,
    files: dict[str, str | bytes] | None = None,
    *,
    tag: str | None = None,
) -> tuple[Path, str]:
    repo = init_git_repo(skills_root / name)
    base = {
        "SKILL.md": "---\nname: test\n---\n\n# Test\n",
    }
    if files:
        base.update(files)
    write_files(repo, base)
    commit = commit_all(repo, "skill")
    if tag:
        run(["git", "tag", tag], repo)
    return repo, commit


def make_project(tmp_path: Path, name: str = "project", *, gitignore: bool = True) -> Path:
    project = init_git_repo(tmp_path / name)
    if gitignore:
        write_files(
            project,
            {
                ".gitignore": ".agents/\n.claude/skills/\n.codex/skills/\n.gemini/skills/\n.cursor/rules/\n",
            },
        )
        commit_all(project, "gitignore")
    return project


def write_skillfile(project: Path, data: dict) -> None:
    (project / "Skillfile.json").write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")


def set_path_with_git_without_go(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    git_executable = shutil.which("git")
    if git_executable is None:
        raise AssertionError("test requires git on the original PATH")

    isolated_bin = tmp_path / "no-go-bin"
    isolated_bin.mkdir()
    native_git_name = Path(git_executable).name
    (isolated_bin / native_git_name).symlink_to(git_executable)
    monkeypatch.setenv("PATH", str(isolated_bin))

    if shutil.which("git") is None:
        raise AssertionError("isolated PATH must preserve git")
    if shutil.which("go") is not None:
        raise AssertionError("isolated PATH must exclude Go")


def make_config(csk_home: Path, skills_root: Path, project: Path, *, agents: list[str] | None = None) -> GlobalConfig:
    agents = agents or ["codex_cli", "claude_code", "cursor"]
    return GlobalConfig(
        path=csk_home / "config.json",
        skills_root=skills_root,
        preferred_locale="ru",
        default_agents=agents,
        adapter_mode="auto",
        worktree_alias_pattern="[A-Z]+-[0-9]+",
        projects={
            "app": ProjectConfig(alias="app", path=project, agents=agents),
        },
    )


@pytest.fixture(autouse=True)
def stable_env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    deprecation.reset_for_tests()
    monkeypatch.delenv("CSK_CONFIG", raising=False)
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setenv("USERPROFILE", str(tmp_path / "home"))


@pytest.fixture
def required_go_e2e_host(monkeypatch: pytest.MonkeyPatch) -> tuple[Path, Path]:
    """Bind a required native E2E run to its installed manager and Go 1.25."""
    manager_value = os.environ.get("CSK_GO_V1_MANAGER_EXECUTABLE")
    go_value = os.environ.get("CSK_GO_V1_GO_EXECUTABLE")
    required = os.environ.get("CSK_E2E_REQUIRED_PLATFORM")
    if not manager_value or not go_value:
        if required:
            pytest.fail(
                "required Go E2E host lacks CSK_GO_V1_MANAGER_EXECUTABLE or "
                "CSK_GO_V1_GO_EXECUTABLE"
            )
        pytest.skip("real Go E2E manager/toolchain inputs are not configured")
    manager_path = shutil.which(manager_value) or manager_value
    go_path = shutil.which(go_value) or go_value
    manager = Path(manager_path).resolve(strict=True)
    go = Path(go_path).resolve(strict=True)
    version = subprocess.run(
        [go, "version"],
        check=True,
        text=True,
        capture_output=True,
    ).stdout
    if " go1.25." not in version:
        pytest.fail(f"Go E2E requires the accepted 1.25 family, got {version.strip()!r}")
    monkeypatch.setattr("sys.argv", [str(manager)])
    monkeypatch.setenv("PATH", os.pathsep.join((str(go.parent), os.environ.get("PATH", ""))))
    return manager, go


@pytest.fixture
def authenticated_e2e_candidate_root() -> Path:
    """Authenticate the explicitly supplied rc.6 candidate without pinning it."""
    root_value = os.environ.get("CURATOR_CONFORMANCE_ROOT")
    required = os.environ.get("CSK_E2E_REQUIRED_PLATFORM")
    if not root_value:
        if required:
            pytest.fail("required Go E2E run lacks CURATOR_CONFORMANCE_ROOT")
        pytest.skip("rc.6 candidate root is not configured")
    root = Path(root_value).resolve(strict=True)
    checkout = root.parent.parent
    head = run(["git", "rev-parse", "HEAD"], checkout).stdout.strip()
    if head != E2E_CANDIDATE_SHA:
        pytest.fail(f"wrong rc.6 candidate HEAD: {head}")
    digest = hashlib.sha256((root / "manifest.json").read_bytes()).hexdigest()
    if digest != E2E_CANDIDATE_MANIFEST_SHA256:
        pytest.fail(f"wrong rc.6 candidate manifest SHA-256: {digest}")
    return root
