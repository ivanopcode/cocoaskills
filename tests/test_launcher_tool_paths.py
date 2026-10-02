from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from csk import git_ops, installer, manifest, skillspec
from csk.sources import publish
from csk.sources.errors import SourceError
from csk.tool_paths import resolve_tool
from conftest import make_project, write_files
from test_source_runtime import (
    _install_failed,
    _install_ok,
    _skill_files,
    _v2_config,
    _write_skillfile_v2,
)


@pytest.fixture(autouse=True)
def isolated_resolver(monkeypatch, tmp_path):
    resolve_tool.cache_clear()
    monkeypatch.setenv("CSK_CONFIG", str(tmp_path / "manager" / "config.json"))
    monkeypatch.setenv("PATHEXT", ".COM;.EXE;.BAT;.CMD")
    yield
    resolve_tool.cache_clear()


def _named_tool(directory: Path, name: str, *, marker: str = "system-ok") -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    tool = directory / (f"{name}.cmd" if os.name == "nt" else name)
    tool.write_text(
        f"@echo off\necho {marker}\n" if os.name == "nt" else f"#!/bin/sh\necho {marker}\n",
        encoding="utf-8",
    )
    tool.chmod(0o755)
    return tool


def _tool(directory: Path) -> Path:
    return _named_tool(directory, "external-helper")


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


def _launcher_text(bin_dir: Path, name: str = "tool") -> str:
    launcher = bin_dir / (f"{name}.cmd" if os.name == "nt" else name)
    return launcher.read_text(encoding="utf-8")


def _split_launcher(content: str) -> tuple[str, str]:
    """Split launcher text at the inherited-PATH marker into prefix/suffix sides."""

    for marker in ('"$PATH"', "%PATH%"):
        if marker in content:
            before, _, after = content.partition(marker)
            return before, after
    raise AssertionError("launcher carries no inherited-PATH marker")


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
    content = _launcher_text(bin_dir)
    before, after = _split_launcher(content)
    assert str(trusted.parent.resolve()) in after
    assert str(trusted.parent.resolve()) not in before
    assert str(root) not in content
    assert str(search) not in content
    assert str(intermediate) not in content
    proc = subprocess.run(
        [str(bin_dir / ("tool.cmd" if os.name == "nt" else "tool"))],
        env={**os.environ, "PATH": os.defpath},
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
    content = _launcher_text(bin_dir)
    before, after = _split_launcher(content)
    assert str(tool.parent.resolve()) in after
    assert str(tool.parent.resolve()) not in before
    proc = subprocess.run(
        [str(bin_dir / ("tool.cmd" if os.name == "nt" else "tool"))],
        env={**os.environ, "PATH": os.defpath},
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


@pytest.mark.parametrize("declaration", ["explicit", "legacy"])
def test_runtime_prefix_contains_only_manager_directories(tmp_path, monkeypatch, declaration):
    tool = _tool(tmp_path / "tools")
    monkeypatch.setenv("PATH", str(tool.parent))
    resolve_tool.cache_clear()
    plan = _plan(tmp_path, declaration)
    bin_dir = tmp_path / "project" / ".agents" / "bin"
    prefix = installer._runtime_path_entries(plan, bin_dir)
    assert prefix[0] == bin_dir.absolute()
    assert tool.parent.resolve() not in prefix
    assert len(prefix) <= 2
    if sys.executable:
        interpreter_dir = Path(sys.executable).resolve().parent
        if interpreter_dir != bin_dir.absolute():
            assert interpreter_dir in prefix


@pytest.mark.parametrize("absolute", [False, True])
def test_manifest_only_change_cannot_steer_prefix(tmp_path, monkeypatch, absolute):
    """Panel A regression: only the dependency command changes between installs."""
    trusted = _tool(tmp_path / "trusted")
    hostile = _named_tool(tmp_path / "hostile-repo" / "scripts", "attacker-tool")
    monkeypatch.setenv("PATH", os.pathsep.join([str(trusted.parent), str(hostile.parent)]))
    resolve_tool.cache_clear()
    plan = _plan(tmp_path / "first", "explicit", "external-helper")
    changed = _plan(tmp_path / "second", "explicit", str(hostile) if absolute else "attacker-tool")
    bin_dir = tmp_path / "bin"
    installer.install_runtime_commands(tmp_path / "home", bin_dir, plan)
    before_text = _launcher_text(bin_dir)
    installer.install_runtime_commands(tmp_path / "home", bin_dir, changed)
    after_text = _launcher_text(bin_dir)
    before_prefix, before_suffix = _split_launcher(before_text)
    after_prefix, after_suffix = _split_launcher(after_text)
    assert before_prefix == after_prefix
    assert str(hostile.parent.resolve()) not in after_prefix
    assert str(trusted.parent.resolve()) not in before_prefix
    assert str(trusted.parent.resolve()) in before_suffix
    assert str(hostile.parent.resolve()) in after_suffix


def test_manifest_names_cannot_steer_prefix(tmp_path, monkeypatch):
    """Panel B regression: same PATH and destination, only the name differs."""
    hostile = _named_tool(tmp_path / "hostile-repo" / "bin", "external-helper")
    operator = _named_tool(tmp_path / "operator-tools", "operator-helper")
    monkeypatch.setenv("PATH", os.pathsep.join([str(hostile.parent), str(operator.parent)]))
    resolve_tool.cache_clear()
    first_plan = _plan(tmp_path / "a", "explicit", "operator-helper")
    second_plan = _plan(tmp_path / "b", "explicit", "external-helper")
    destination = tmp_path / "project" / ".agents" / "bin"
    first = installer._runtime_path_entries(first_plan, destination)
    second = installer._runtime_path_entries(second_plan, destination)
    assert first == second
    installer.install_runtime_commands(tmp_path / "home", destination, first_plan)
    first_text = _launcher_text(destination)
    installer.install_runtime_commands(tmp_path / "home", destination, second_plan)
    second_text = _launcher_text(destination)
    assert _split_launcher(first_text)[0] == _split_launcher(second_text)[0]
    assert str(hostile.parent.resolve()) not in _split_launcher(second_text)[0]
    proc = subprocess.run(
        [str(destination / ("tool.cmd" if os.name == "nt" else "tool"))],
        env={**os.environ, "PATH": os.defpath},
        text=True, capture_output=True, timeout=30,
    )
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout.strip() == "system-ok"


@pytest.mark.parametrize("command", ["git", "external-helper"])
def test_only_hostile_repo_tool_lands_in_suffix_not_prefix(tmp_path, monkeypatch, command):
    """Panel B regression: an ordinary repo dir is deprioritized, never prefixed."""
    repo_tool = _named_tool(tmp_path / "hostile-repo" / "bin", command)
    monkeypatch.setenv("PATH", str(repo_tool.parent))
    resolve_tool.cache_clear()
    plan = _plan(tmp_path, "explicit", command)
    destination = tmp_path / "project" / ".agents" / "bin"
    installer.install_runtime_commands(tmp_path / "home", destination, plan)
    before, after = _split_launcher(_launcher_text(destination))
    assert str(repo_tool.parent.resolve()) not in before
    assert str(repo_tool.parent.resolve()) in after


def test_inherited_path_outranks_dependency_suffix(tmp_path, monkeypatch):
    shadow = _named_tool(tmp_path / "caller-bin", "external-helper", marker="caller-wins")
    repo_tool = _named_tool(tmp_path / "hostile-repo" / "bin", "external-helper", marker="suffix-tool")
    monkeypatch.setenv("PATH", str(repo_tool.parent))
    resolve_tool.cache_clear()
    plan = _plan(tmp_path, "explicit")
    destination = tmp_path / "project" / ".agents" / "bin"
    installer.install_runtime_commands(tmp_path / "home", destination, plan)
    launcher = destination / ("tool.cmd" if os.name == "nt" else "tool")
    proc = subprocess.run(
        [str(launcher)],
        env={**os.environ, "PATH": str(shadow.parent)},
        text=True, capture_output=True, timeout=30,
    )
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout.strip() == "caller-wins"


@pytest.mark.parametrize("declaration", ["explicit", "legacy"])
def test_suffix_resolves_tool_when_inherited_path_is_empty(tmp_path, monkeypatch, declaration):
    tool = _tool(tmp_path / "trusted tools")
    monkeypatch.setenv("PATH", str(tool.parent))
    resolve_tool.cache_clear()
    plan = _plan(tmp_path, declaration)
    bin_dir = tmp_path / "bin"
    assert installer.install_runtime_commands(tmp_path / "home", bin_dir, plan) == {"tool"}
    before, after = _split_launcher(_launcher_text(bin_dir))
    assert str(tool.parent.resolve()) in after
    assert str(tool.parent.resolve()) not in before
    launcher = bin_dir / ("tool.cmd" if os.name == "nt" else "tool")
    proc = subprocess.run(
        [str(launcher)],
        env={**os.environ, "PATH": ""},
        text=True, capture_output=True, timeout=30,
    )
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout.strip() == "system-ok"


def test_schema2_path_builder_rejects_project_shim(tmp_path, monkeypatch):
    """Panel A direct regression: the schema-2 builder admits no shim dir."""
    shim = _tool(tmp_path / "hostile" / ".agents" / "bin")
    monkeypatch.setenv("PATH", str(shim.parent))
    resolve_tool.cache_clear()
    plan = _plan(tmp_path, "explicit")
    entries = publish.schema2_shim_path_entries(plan.spec, final_bin=tmp_path / "consumer" / ".agents" / "bin")
    assert shim.parent not in entries
    with pytest.raises(SourceError, match="external-helper"):
        publish.schema2_shim_path_suffix(plan.spec, name="consumer")


def _write_consumer_source(root: Path) -> Path:
    source = root / "pkgs"
    write_files(
        source / "consumer",
        _skill_files(
            "consumer",
            "consume",
            "#!/bin/sh\nexternal-helper\n",
            runtime_roots=True,
            dependencies={"helper": {"type": "system", "command": "external-helper"}},
        ),
    )
    return source


def test_schema2_install_refuses_shim_only_system_dependency(tmp_path, skills_root, csk_home, monkeypatch):
    """Panel A/B regression: a real schema-2 install publishes no hostile shim dir."""
    project = make_project(tmp_path)
    hostile = _tool(tmp_path / "other-project" / ".agents" / "bin")
    source = _write_consumer_source(tmp_path)
    _write_skillfile_v2(project, source, [("consumer", "consumer")])
    cfg = _v2_config(csk_home, skills_root, project)
    monkeypatch.setenv("CSK_CONFIG", str(csk_home / "config.json"))
    monkeypatch.setenv("PATH", str(hostile.parent) + os.pathsep + os.environ["PATH"])
    resolve_tool.cache_clear()
    errors = _install_failed(cfg)
    assert any("external-helper" in error for error in errors)
    assert not (project / ".agents" / "bin" / "consume").exists()
    assert not (project / ".agents" / "bin" / "consume.cmd").exists()


def test_schema2_dependency_suffix_follows_inherited_path(tmp_path, skills_root, csk_home, monkeypatch):
    project = make_project(tmp_path)
    tools = _tool(tmp_path / "operator tools")
    source = _write_consumer_source(tmp_path)
    _write_skillfile_v2(project, source, [("consumer", "consumer")])
    cfg = _v2_config(csk_home, skills_root, project)
    monkeypatch.setenv("CSK_CONFIG", str(csk_home / "config.json"))
    monkeypatch.setenv("PATH", str(tools.parent) + os.pathsep + os.environ["PATH"])
    resolve_tool.cache_clear()
    _install_ok(cfg)
    launcher = project / ".agents" / "bin" / ("consume.cmd" if os.name == "nt" else "consume")
    before, after = _split_launcher(launcher.read_text(encoding="utf-8"))
    assert str(tools.parent.resolve()) in after
    assert str(tools.parent.resolve()) not in before
    assert str((project / ".agents" / "bin").absolute()) in before


@pytest.mark.skipif(sys.platform == "win32", reason="posix-shim-exec: executes POSIX shell shims")
def test_schema2_suffix_tool_runs_with_minimal_path(tmp_path, skills_root, csk_home, monkeypatch):
    project = make_project(tmp_path)
    tools = _tool(tmp_path / "operator tools")
    source = _write_consumer_source(tmp_path)
    _write_skillfile_v2(project, source, [("consumer", "consumer")])
    cfg = _v2_config(csk_home, skills_root, project)
    monkeypatch.setenv("CSK_CONFIG", str(csk_home / "config.json"))
    monkeypatch.setenv("PATH", str(tools.parent) + os.pathsep + os.environ["PATH"])
    resolve_tool.cache_clear()
    _install_ok(cfg)
    launcher = project / ".agents" / "bin" / "consume"
    proc = subprocess.run(
        [str(launcher)],
        env={**os.environ, "PATH": os.defpath},
        text=True, capture_output=True, timeout=60,
    )
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout.strip() == "system-ok"
