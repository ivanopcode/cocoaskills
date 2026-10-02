from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

import pytest

from csk import git_ops, installer, manifest, skillspec
from csk.tool_paths import resolve_tool


@pytest.fixture(autouse=True)
def isolated_resolver(monkeypatch, tmp_path):
    resolve_tool.cache_clear()
    monkeypatch.setenv("CSK_CONFIG", str(tmp_path / "manager" / "config.json"))
    monkeypatch.setenv("PATHEXT", ".COM;.EXE;.BAT;.CMD")
    yield
    resolve_tool.cache_clear()


def _tool(directory: Path) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    tool = directory / ("external-helper.cmd" if os.name == "nt" else "external-helper")
    tool.write_text(
        "@echo off\necho system-ok\n" if os.name == "nt" else "#!/bin/sh\necho system-ok\n",
        encoding="utf-8",
    )
    tool.chmod(0o755)
    return tool


def _link(link: Path, target: Path, *, directory: bool = False) -> None:
    link.parent.mkdir(parents=True, exist_ok=True)
    try:
        link.symlink_to(os.path.relpath(target, link.parent), target_is_directory=directory)
    except OSError:
        if os.name == "nt":
            pytest.skip("Windows runner cannot create symlinks")
        raise


def _plan(tmp_path: Path, declaration: str, command: str = "external-helper") -> installer.SkillPlan:
    root = tmp_path / "snapshot"
    scripts = root / "scripts"
    scripts.mkdir(parents=True)
    (scripts / "tool").write_text("#!/bin/sh\nexternal-helper\n", encoding="utf-8")
    (scripts / "tool.cmd").write_text("@echo off\ncall external-helper\n", encoding="utf-8")
    data = {
        "schema_version": 2,
        "runtime_roots": ["scripts"],
        "commands": {
            "tool": {"type": "script", "unix_path": "scripts/tool", "win_path": "scripts/tool.cmd"},
        },
    }
    dependency = {"type": "system", "command": command}
    if declaration == "legacy":
        data["commands"]["external-helper"] = dependency
    else:
        data["dependencies"] = {"commands": {"helper": dependency}}
    (root / "csk-skill.json").write_text(json.dumps(data), encoding="utf-8")
    return installer.SkillPlan(
        decl=manifest.SkillDecl("skill-system", "local", manifest.SkillRef("tag", "v1")),
        resolved=git_ops.ResolvedRef("tag", "v1", "a" * 40),
        repo=root,
        snapshot=root,
        spec=skillspec.load_skill_spec(root),
    )


@pytest.mark.parametrize("declaration", ["explicit", "legacy"])
@pytest.mark.parametrize("location", ["project", "global", "published", "custom"])
@pytest.mark.parametrize("alias_kind", ["direct", "executable-chain", "directory-chain"])
@pytest.mark.parametrize("has_trusted", [False, True], ids=["shim-only", "trusted-fallback"])
@pytest.mark.parametrize("command_form", ["name", "absolute"])
def test_launcher_manifest_dependency_cannot_capture_shim_path(
    tmp_path, monkeypatch, declaration, location, alias_kind, has_trusted, command_form,
):
    """Drive real runtime materialization, including manifest parsing and shim writing."""
    roots = {
        "project": tmp_path / "other-project" / ".agents" / "bin",
        "global": tmp_path / ".cocoaskills" / "global" / "bin",
        "published": tmp_path / "published",
        "custom": tmp_path / "manager" / "global" / "bin",
    }
    root = roots[location]
    shim_tool = _tool(root)
    if location == "published":
        (root / ".csk-managed.json").write_text("{}", encoding="utf-8")
    trusted = _tool(tmp_path / "trusted tools")
    # Every chain exits the shim again. A final-target-only check would lose
    # the intermediate shim directory and admit the outside terminal tool.
    search = root
    intermediate = tmp_path / "intermediate"
    if alias_kind != "direct":
        shim_tool.unlink()
        _link(shim_tool, trusted)
        search = tmp_path / "alias"
        if alias_kind == "directory-chain":
            _link(intermediate, root, directory=True)
            _link(search, intermediate, directory=True)
        else:
            _link(intermediate / shim_tool.name, shim_tool)
            _link(search / shim_tool.name, intermediate / shim_tool.name)
    monkeypatch.setenv("PATH", os.pathsep.join(map(str, [search, *([trusted.parent] if has_trusted else [])])))
    command = str(search / shim_tool.name) if command_form == "absolute" else "external-helper"
    plan = _plan(tmp_path, declaration, command)
    bin_dir = tmp_path / "project" / ".agents" / "bin"
    home = tmp_path / "home"
    if not has_trusted or command_form == "absolute":
        with pytest.raises(installer.InstallError, match="csk shims"):
            installer.install_runtime_commands(home, bin_dir, plan)
        assert not bin_dir.exists(), "refusal must precede launcher publication"
        assert not (home / "runtime").exists()
        return

    assert installer.install_runtime_commands(home, bin_dir, plan) == {"tool"}
    launcher = bin_dir / ("tool.cmd" if os.name == "nt" else "tool")
    content = launcher.read_text(encoding="utf-8")
    assert str(trusted.parent.resolve()) in content
    assert str(root) not in content
    assert str(search) not in content
    assert str(intermediate) not in content
    proc = subprocess.run(
        [str(launcher)], env={**os.environ, "PATH": os.defpath},
        text=True, capture_output=True, timeout=30,
    )
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout.strip() == "system-ok"


@pytest.mark.parametrize("declaration", ["explicit", "legacy"])
def test_launcher_admits_absolute_system_dependency(tmp_path, monkeypatch, declaration):
    tool = _tool(tmp_path / "trusted tools")
    monkeypatch.setenv("PATH", str(tmp_path / "empty"))
    plan = _plan(tmp_path, declaration, str(tool))
    bin_dir = tmp_path / "bin"
    assert installer.install_runtime_commands(tmp_path / "home", bin_dir, plan) == {"tool"}
    launcher = bin_dir / ("tool.cmd" if os.name == "nt" else "tool")
    assert str(tool.parent.resolve()) in launcher.read_text(encoding="utf-8")
    proc = subprocess.run(
        [str(launcher)], env={**os.environ, "PATH": os.defpath},
        text=True, capture_output=True, timeout=30,
    )
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout.strip() == "system-ok"


@pytest.mark.parametrize("declaration", ["explicit", "legacy"])
def test_launcher_missing_dependency_refuses_before_publication(tmp_path, monkeypatch, declaration):
    monkeypatch.setenv("PATH", str(tmp_path / "empty"))
    plan = _plan(tmp_path, declaration)
    bin_dir = tmp_path / "bin"
    with pytest.raises(installer.InstallError, match="Cannot resolve system command 'external-helper'"):
        installer.install_runtime_commands(tmp_path / "home", bin_dir, plan)
    assert not bin_dir.exists()
