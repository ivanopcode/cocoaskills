from __future__ import annotations

import json
import os
import sys
import sysconfig
from pathlib import Path

import pytest
from conftest import make_config, make_project, write_skillfile
from test_install_external_repository import _external_repository, _git_tool, _skill_repository

from csk import install_marker, installer
from csk.builds import go_v1


pytestmark = [
    pytest.mark.csk_external_build_repeat,
    pytest.mark.skipif(
        os.environ.get("CSK_EXTERNAL_BUILD_REPEAT") != "1",
        reason="external build repeat coverage requires explicit opt-in",
    ),
    pytest.mark.skipif(
        sys.platform not in {"darwin", "win32"},
        reason="go-repository-v1 is qualified only on macOS and Windows",
    ),
]


@pytest.mark.parametrize("iteration", range(1, 21), ids=lambda value: f"install-{value:02d}")
def test_local_external_build_keeps_snapshot_root_until_compile_returns(
    iteration: int,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    skills_root: Path,
    csk_home: Path,
    record_property,
) -> None:
    """BUG-260808-1471m6: twenty independent cold installs with real Go workers.

    Each parameter gets a new project and manager home, so artifact cache hits
    cannot bypass the TemporaryDirectory / FrozenSnapshot lifetime under test.
    CI opts in only in the workflow_dispatch Windows job; macOS can exercise
    the same install path locally, but cannot establish Windows reliability.
    """
    record_property("iteration", iteration)
    # Keep local admission independent of ambient operator credentials.
    for variable in (
        "CSK_BUILD_SSH_IDENTITY", "CSK_BUILD_SSH_AGENT", "CSK_BUILD_SSH_KNOWN_HOSTS",
        "CSK_BUILD_HTTPS_TOKEN", "CSK_BUILD_HTTPS_USERNAME", "CSK_BUILD_HTTPS_HOST",
    ):
        monkeypatch.delenv(variable, raising=False)
    manager = Path(sysconfig.get_path("scripts")) / ("csk.exe" if os.name == "nt" else "csk")
    assert manager.is_file(), "install the package into the test interpreter's environment"
    monkeypatch.setattr(sys, "argv", [str(manager)])
    project = make_project(tmp_path)
    _external, commit = _external_repository(tmp_path)
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
                    "external-skill": {"tools": {"path": "../external-tool"}}
                },
            }
        ),
        encoding="utf-8",
    )
    with (project / ".gitignore").open("a", encoding="utf-8") as stream:
        stream.write("Skillfile.dev.json\n")

    # Admit the real local Git repository using the host Git, with no fetch.
    tool = _git_tool()
    monkeypatch.setattr(installer, "_external_git_tool", lambda *_args, **_kwargs: tool)
    real_build = go_v1.build
    compile_calls = 0

    def observed_build(request: go_v1.BuildRequest) -> go_v1.BuildResult:
        nonlocal compile_calls
        compile_calls += 1
        root = request.source_snapshot.path
        assert root.name.startswith("csk-external-build-root-")
        assert root.is_dir(), f"iteration {iteration}: build root vanished before compilation"
        result = real_build(request)
        assert root.is_dir(), f"iteration {iteration}: build root vanished during compilation"
        return result

    monkeypatch.setattr(go_v1, "build", observed_build)
    config = make_config(csk_home, skills_root, project, agents=["codex_cli"])
    try:
        result = installer.install(config)[0]
    finally:
        record_property("compile_calls", compile_calls)
    assert not result.errors, f"iteration {iteration}/20: {result.errors}"
    assert result.status == "ok", f"iteration {iteration}/20: {result}"
    assert compile_calls == 1, f"iteration {iteration}/20 must compile rather than reuse an artifact"

    marker = install_marker.read_install_marker(
        (project / ".agents/skills/external-skill/.csk-install.json").read_bytes()
    )
    assert isinstance(marker, install_marker.InstallMarkerV3)
    build = marker.builds["external-tool"]
    assert build.driver == "go-repository-v1"
    artifact = (
        csk_home
        / "external-builds/artifacts"
        / build.cache_key.removeprefix("sha256:")
        / ("artifact.exe" if os.name == "nt" else "artifact")
    )
    assert artifact.is_file() and artifact.stat().st_size > 0
    shim = project / ".agents/bin" / ("external-tool.cmd" if os.name == "nt" else "external-tool")
    assert shim.is_file()
