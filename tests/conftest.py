from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

import pytest

from csk import deprecation
from csk.config import GlobalConfig, ProjectConfig


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
    home.mkdir()
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
