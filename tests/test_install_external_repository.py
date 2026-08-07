from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest
from conftest import commit_all, init_git_repo, make_config, make_project, make_skill_repo, write_files, write_skillfile
from test_install import _stub_trusted_toolchain

from csk import git_admission, global_install, install_marker, installer, status
from csk.builds import toolchain as build_toolchain


pytestmark = pytest.mark.skipif(
    sys.platform not in {"darwin", "win32"},
    reason="go-repository-v1 is qualified only on macOS and Windows",
)


def _git_tool() -> git_admission.GitTool:
    executable_text = shutil.which("git")
    assert executable_text is not None
    executable = Path(executable_text).resolve(strict=True)
    version = subprocess.run(
        (os.fspath(executable), "--version"),
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    exec_path = Path(
        subprocess.run(
            (os.fspath(executable), "--exec-path"),
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()
    ).resolve(strict=True)
    return git_admission.GitTool(
        executable=executable,
        exec_path=exec_path,
        allowed_versions=(version,),
        askpass=Path(sys.executable).resolve(strict=True),
    )


def _external_repository(tmp_path: Path) -> tuple[Path, str]:
    repository = init_git_repo(tmp_path / "external-tool")
    write_files(
        repository,
        {
            "skill-build.json": json.dumps(
                {
                    "schema_version": 1,
                    "targets": {
                        "external-tool": {
                            "driver": "go-repository-v1",
                            "build_root": ".",
                            "source_dir": "cmd/external-tool",
                        }
                    },
                }
            ),
            "go.mod": "module example.test/external-tool\n\ngo 1.25\n",
            "cmd/external-tool/main.go": "package main\nfunc main() {}\n",
        },
    )
    commit = commit_all(repository, "external tool")
    for child in list((repository / ".git").iterdir()):
        if child.name in {"HEAD", "config", "index", "objects", "refs", "packed-refs"}:
            continue
        if child.is_dir():
            shutil.rmtree(child)
        else:
            child.unlink()
    return repository, commit


def _skill_repository(
    skills_root: Path,
    commit: str,
    git: str = "https://example.test/external-tool.git",
) -> None:
    make_skill_repo(
        skills_root,
        "external-skill",
        {
            "agent-skill.json": json.dumps(
                {
                    "schema_version": 7,
                    "capabilities": {},
                    "build_repositories": {
                        "tools": {
                            "git": git,
                            "locked_commit": {
                                "object_format": "sha1",
                                "hex": commit,
                            },
                        }
                    },
                    "commands": {
                        "external-tool": {
                            "type": "build",
                            "driver": "go-repository-v1",
                            "repository": "tools",
                            "target": "external-tool",
                        }
                    },
                }
            )
        },
        tag="v1",
    )


def test_project_external_build_install_offline_repair_activation_and_uninstall(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    skills_root: Path,
    csk_home: Path,
) -> None:
    project = make_project(tmp_path)
    external, commit = _external_repository(tmp_path)
    _skill_repository(skills_root, commit)
    write_skillfile(
        project,
        {
            "schema_version": 1,
            "agents": ["codex_cli"],
            "skills": [{"name": "external-skill", "tag": "v1"}],
        },
    )
    (project / "Skillfile.dev.json").write_text(
        json.dumps(
            {
                "schema_version": 2,
                "substitutions": {},
                "build_repository_substitutions": {
                    "external-skill": {
                            "tools": {"path": "../external-tool"}
                    }
                },
            }
        ),
        encoding="utf-8",
    )
    with (project / ".gitignore").open("a", encoding="utf-8") as stream:
        stream.write("Skillfile.dev.json\n")
    commit_all(project, "external declaration")
    config = make_config(csk_home, skills_root, project, agents=["codex_cli"])
    _stub_trusted_toolchain(monkeypatch)
    tool = _git_tool()
    monkeypatch.setattr(installer, "_external_git_tool", lambda *_args, **_kwargs: tool)

    first = installer.install(config)[0]
    assert not first.errors
    marker_path = project / ".agents/skills/external-skill/.csk-install.json"
    marker = install_marker.read_install_marker(marker_path.read_bytes())
    assert isinstance(marker, install_marker.InstallMarkerV3)
    build = marker.builds["external-tool"]
    assert build.driver == "go-repository-v1"
    shim = project / ".agents/bin" / (
        "external-tool.cmd" if os.name == "nt" else "external-tool"
    )
    assert shim.is_file()
    assert str(csk_home / "external-builds" / "artifacts") in shim.read_text(
        encoding="utf-8"
    )
    current = status.collect_status(config)[0]
    assert current.clean, current
    assert len(current.builds) == 1
    assert current.builds[0].current

    monkeypatch.setattr(
        git_admission,
        "admit_local",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            git_admission.GitAdmissionError(
                git_admission.SOURCE_UNAVAILABLE, "offline"
            )
        ),
    )
    offline = installer.install(config)[0]
    assert not offline.errors
    assert any("cache-hit" in message for message in offline.messages)

    artifact = (
        csk_home
        / "external-builds/artifacts"
        / build.cache_key.removeprefix("sha256:")
        / "artifact"
    )
    artifact.chmod(0o700)
    artifact.write_bytes(b"corrupt")
    corrupt = status.collect_status(config)[0]
    assert not corrupt.clean
    assert corrupt.builds[0].label == "corrupt-build-cache"
    monkeypatch.undo()
    _stub_trusted_toolchain(monkeypatch)
    monkeypatch.setattr(installer, "_external_git_tool", lambda *_args, **_kwargs: tool)
    repaired = installer.install(config)[0]
    assert not repaired.errors
    assert artifact.read_bytes() != b"corrupt"

    write_skillfile(
        project,
        {"schema_version": 1, "agents": ["codex_cli"], "skills": []},
    )
    removed = installer.install(config)[0]
    assert not removed.errors
    assert not shim.exists()


def test_global_external_build_uses_direct_managed_shim(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    skills_root: Path,
    csk_home: Path,
) -> None:
    project = make_project(tmp_path)
    external, commit = _external_repository(tmp_path)
    _skill_repository(skills_root, commit)
    config = make_config(csk_home, skills_root, project, agents=["codex_cli"])
    global_install.init(csk_home, default_agents=["codex_cli"])
    (global_install.global_skillfile(csk_home)).write_text(
        json.dumps(
            {
                "schema_version": 1,
                "agents": ["codex_cli"],
                "skills": [{"name": "external-skill", "tag": "v1"}],
            }
        ),
        encoding="utf-8",
    )
    snapshot = git_admission.admit_local(external, _git_tool())
    _stub_trusted_toolchain(monkeypatch)
    tool = _git_tool()
    monkeypatch.setattr(installer, "_external_git_tool", lambda *_args, **_kwargs: tool)
    monkeypatch.setattr(git_admission, "acquire_network", lambda *_args, **_kwargs: snapshot)

    result = global_install.install(config)
    assert not result.errors
    shim = global_install.global_bin_dir(csk_home) / (
        "external-tool.cmd" if os.name == "nt" else "external-tool"
    )
    assert shim.is_file()
    assert str(csk_home / "external-builds" / "artifacts") in shim.read_text(
        encoding="utf-8"
    )


@pytest.mark.skipif(
    shutil.which("ssh") is None,
    reason="the credential boundary is reached only once an operator ssh resolves",
)
def test_ssh_external_build_without_operator_credentials_fails_closed(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    skills_root: Path,
    csk_home: Path,
) -> None:
    """A private SSH build repository must not silently fall back to ambient state."""

    project = make_project(tmp_path)
    _external, commit = _external_repository(tmp_path)
    _skill_repository(
        skills_root, commit, git="git@gitlab.fixture.test:portals/external-tool.git"
    )
    write_skillfile(
        project,
        {
            "schema_version": 1,
            "agents": ["codex_cli"],
            "skills": [{"name": "external-skill", "tag": "v1"}],
        },
    )
    commit_all(project, "external declaration")
    config = make_config(csk_home, skills_root, project, agents=["codex_cli"])
    _stub_trusted_toolchain(monkeypatch)
    # The Go stub pins a fixture search path; git and ssh must still resolve so
    # the run reaches the credential boundary rather than tool discovery.
    monkeypatch.setattr(
        build_toolchain,
        "capture_operator_search_path",
        lambda: build_toolchain.OperatorSearchPath(
            tuple(os.environ.get("PATH", "").split(os.pathsep))
        ),
    )
    for name in (
        git_admission.OPERATOR_SSH_IDENTITY_ENV,
        git_admission.OPERATOR_SSH_AGENT_ENV,
    ):
        monkeypatch.delenv(name, raising=False)

    result = installer.install(config)[0]

    assert result.errors
    assert any(
        git_admission.SSH_CREDENTIAL_MISSING in error for error in result.errors
    ), result.errors
    shim = project / ".agents/bin" / (
        "external-tool.cmd" if os.name == "nt" else "external-tool"
    )
    assert not shim.exists()
    assert not (csk_home / "external-builds" / "artifacts").exists()
    assert not (project / ".agents/skills/external-skill/.csk-install.json").exists()
