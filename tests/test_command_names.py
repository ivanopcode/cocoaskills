"""Plan-time reserved names across script and compiled publication lanes."""

from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path

import pytest

from csk import closure, git_ops, installer, manifest, skillspec
from csk.builds import planner, source, toolchain
from csk.sources import publish
from conftest import make_config


# Independent copy of the decided set: omitting a policy entry must fail.
SYSTEM_NAMES = (
    "git ssh ssh-add ssh-agent ssh-keygen scp sftp gpg gpg-agent sh bash zsh "
    "fish dash pwsh powershell cmd env sudo su doas python python3 pip pip3 "
    "uv go gofmt node npm npx ruby perl make cc gcc clang ld curl wget tar "
    "unzip openssl security keychain launchctl systemctl"
).split()
MANAGER_NAMES = ["csk", "cocoaskills", "csk-shell", "csk-script.py"]
GATES = [
    "closure-script",
    "closure-local-build",
    "closure-external-build",
    "schema2-script",
    "schema2-local-build",
    "schema2-external-build",
    "planner-build",
]


def _spec(command_name: str, kind: str) -> skillspec.SkillSpec:
    command = skillspec.CommandSpec(
        name=command_name,
        type="script" if kind == "script" else "build",
        unix_path="scripts/tool" if kind == "script" else None,
        win_path="scripts/tool.cmd" if kind == "script" else None,
        driver=(
            None if kind == "script"
            else "go-v1" if kind == "local-build"
            else "go-repository-v1"
        ),
        source_dir="build/cmd/tool" if kind == "local-build" else None,
        repository="tools" if kind == "external-build" else None,
        target="tool" if kind == "external-build" else None,
    )
    return skillspec.SkillSpec(commands={command_name: command}, source_file=None)


def _node(
    root: Path, command_name: str, kind: str, *, name: str = "provider",
    mode: str = "full", selected: tuple[str, ...] = (),
) -> closure.ClosureNode:
    return closure.ClosureNode(
        name=name,
        decl=manifest.SkillDecl(name, name, manifest.SkillRef("revision", "main")),
        resolved=git_ops.ResolvedRef("revision", "main", "a" * 40),
        repo=root,
        snapshot=root,
        spec=_spec(command_name, kind),
        identity=None,
        edges=[closure.ActivationEdge("consumer", mode, selected)],
    )


def _member(root: Path, name: str = "provider") -> publish.ResolvedMember:
    return publish.ResolvedMember(name, "local", name, 0, root)


def _check_gate(gate: str, command_name: str, root: Path) -> None:
    lane, kind = gate.split("-", 1)
    if lane == "closure":
        closure.detect_active_command_collisions([_node(root, command_name, kind)])
    elif lane == "schema2":
        publish.run_schema2_command_gates(
            (_member(root),), {"provider": _spec(command_name, kind)},
        )
    else:
        with source.freeze_snapshot(root) as frozen:
            provider = planner.BuildProvider(
                "provider", frozen,
                (planner.BuildCommand(command_name, "go-v1", "build", "build/cmd/tool"),),
            )
            planner.detect_command_collisions((provider,))


def _assert_reserved(call: Callable[[], None], command_name: str) -> None:
    # Use the existing boundaries without importing the new policy module,
    # so this regression also runs against the unmodified base.
    with pytest.raises(ValueError) as error:
        call()
    assert error.value.code == "command_name_reserved"
    assert command_name in str(error.value)
    assert "Hint:" in str(error.value)
    assert "rename" in str(error.value).lower()


@pytest.mark.parametrize("gate", GATES)
@pytest.mark.parametrize("command_name", SYSTEM_NAMES + MANAGER_NAMES)
def test_reserved_names_at_plan_time(tmp_path, gate, command_name):
    _assert_reserved(lambda: _check_gate(gate, command_name, tmp_path), command_name)


@pytest.mark.parametrize("gate", GATES)
@pytest.mark.parametrize("suffix", ["", ".exe", ".cmd", ".bat", ".ps1"])
def test_reserved_names_ignore_case_and_windows_suffix(tmp_path, gate, suffix):
    for name in SYSTEM_NAMES + MANAGER_NAMES:
        command_name = name.upper() + suffix.upper()
        _assert_reserved(lambda: _check_gate(gate, command_name, tmp_path), command_name)


@pytest.mark.parametrize("gate", GATES)
@pytest.mark.parametrize("command_name", ["git-tools", "python3-helper", "acsk", "cskx", "cocoaskills-tool", "tool.cmd", "tool.exe", "tool.ps1", "git.txt"])
def test_unreserved_names_remain_allowed(tmp_path, gate, command_name):
    _check_gate(gate, command_name, tmp_path)


@pytest.mark.parametrize("kind", ["script", "local-build", "external-build"])
@pytest.mark.parametrize("mode", ["full", "runtime"])
def test_closure_reserved_activation_modes(tmp_path, kind, mode):
    node = _node(tmp_path, "SSH.CMD", kind, mode=mode, selected=("SSH.CMD",))
    _assert_reserved(lambda: closure.detect_active_command_collisions([node]), "SSH.CMD")


@pytest.mark.parametrize("kind", ["script", "local-build", "external-build"])
@pytest.mark.parametrize("mode", ["context", "runtime"])
def test_unpublished_reserved_commands_remain_allowed(tmp_path, kind, mode):
    node = _node(tmp_path, "git", kind, mode=mode, selected=("tool",))
    node.spec.commands["tool"] = _spec("tool", kind).commands["tool"]
    closure.detect_active_command_collisions([node])


def test_system_dependencies_are_not_published_names(tmp_path):
    node = _node(tmp_path, "git", "script")
    node.spec.commands["git"] = skillspec.CommandSpec("git", "system", command="git")
    closure.detect_active_command_collisions([node])
    publish.check_schema2_command_collisions((_member(tmp_path),), {"provider": node.spec})


def test_skill_collision_check_stays(tmp_path):
    nodes = [_node(tmp_path, "tool", "script", name=name) for name in ("one", "two")]
    with pytest.raises(closure.ClosureError, match="Command collision"):
        closure.detect_active_command_collisions(nodes)
    with pytest.raises(publish.SourceError, match="Command collision"):
        publish.run_schema2_command_gates(
            tuple(_member(tmp_path, node.name) for node in nodes),
            {node.name: node.spec for node in nodes},
        )
    with source.freeze_snapshot(tmp_path) as frozen:
        provider = planner.BuildProvider(
            "provider", frozen,
            (planner.BuildCommand("tool", "go-v1", "build", "build/cmd/tool"),),
        )
        with pytest.raises(planner.BuildPlanningError, match="command_collision"):
            planner.detect_command_collisions((provider,), occupied={"tool": "script-provider"})


def test_schema2_build_refusal_precedes_toolchain_and_audit(tmp_path, monkeypatch):
    def forbidden(*args, **kwargs):
        pytest.fail("reserved command reached build planning or audit")

    monkeypatch.setattr(planner, "plan_builds", forbidden)
    with source.freeze_snapshot(tmp_path) as frozen:
        provider = planner.BuildProvider(
            "provider", frozen,
            (planner.BuildCommand("SSH.CMD", "go-v1", "build", "build/cmd/tool"),),
        )
        _assert_reserved(
            lambda: publish.plan_schema2_local_builds(
                (provider,), home=tmp_path,
                operator_search_path=toolchain.OperatorSearchPath(()),
                go_future_families="warn", forbidden_roots=(),
                cache_backend=None, occupied={}, audit=forbidden,
            ),
            "SSH.CMD",
        )


def test_direct_build_plan_refuses_before_toolchain_and_audit(tmp_path):
    def forbidden(*args, **kwargs):
        pytest.fail("reserved command reached toolchain or audit")

    with source.freeze_snapshot(tmp_path) as frozen:
        provider = planner.BuildProvider(
            "provider", frozen,
            (planner.BuildCommand("SSH.CMD", "go-v1", "build", "build/cmd/tool"),),
        )
        _assert_reserved(
            lambda: planner.plan_builds(
                (provider,), manager_home=tmp_path,
                operator_search_path=toolchain.OperatorSearchPath(()),
                establish_toolchain=forbidden, audit=forbidden,
            ),
            "SSH.CMD",
        )


@pytest.mark.parametrize("dry_run", [False, True])
def test_schema2_install_refuses_before_publication(
    tmp_path, csk_home, skills_root, monkeypatch, dry_run,
):
    project = tmp_path / "project"
    member = project / "sources" / "provider"
    scripts = member / "scripts"
    scripts.mkdir(parents=True)
    (member / "SKILL.md").write_text(
        "---\nname: provider\ndescription: Test provider\n---\n\n# Provider\n",
        encoding="utf-8",
    )
    (scripts / "tool").write_text("#!/bin/sh\n", encoding="utf-8")
    (scripts / "tool.cmd").write_text("@echo off\r\n", encoding="utf-8")
    (member / "agent-skill.json").write_text(json.dumps({
        "schema_version": 2,
        "runtime_roots": ["scripts"],
        "commands": {"SSH.CMD": {
            "type": "script", "unix_path": "scripts/tool", "win_path": "scripts/tool.cmd",
        }},
    }), encoding="utf-8")
    (project / "Skillfile.json").write_text(json.dumps({
        "schema_version": 2,
        "sources": {"local": {"path": "sources"}},
        "skills": [{"name": "provider", "from": "local", "directory": "provider"}],
    }), encoding="utf-8")
    # The fixture has no Git repository. Bypass only the unrelated ignore
    # check so the production path stays offline and creates no commits.
    monkeypatch.setattr(installer.gitignore_gate, "ensure_ignored", lambda *a, **kw: None)

    def forbidden(*args, **kwargs):
        pytest.fail("reserved command reached audit or publication")

    monkeypatch.setattr(publish, "run_schema2_audit_gate", forbidden)
    monkeypatch.setattr(publish, "stage_member_context", forbidden)
    cfg = make_config(csk_home, skills_root, project, agents=["codex_cli"])
    result = installer.install(cfg, options=installer.InstallOptions(dry_run=dry_run))[0]
    assert result.status == "failed"
    assert result.errors and result.errors[0].startswith("command_name_reserved:")
    assert "SSH.CMD" in result.errors[0] and "Hint:" in result.errors[0]
    assert not (project / ".agents").exists()
    assert not (project / "Skillfile.lock.json").exists()
    assert not (csk_home / "runtime").exists()


def test_reserved_refusal_is_user_visible(tmp_path):
    with pytest.raises(ValueError) as error:
        _check_gate("closure-script", "SSH.CMD", tmp_path)
    for render in (installer.failure_text, installer._schema2_failure_text):
        text = render(error.value)
        assert text.startswith("command_name_reserved:")
        assert "SSH.CMD" in text and "Hint:" in text
