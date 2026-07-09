from __future__ import annotations

import sys

import pytest

from csk import adapters


def test_copy_adapter_refreshes_when_canonical_changes(tmp_path):
    project = tmp_path / "project"
    canonical = project / ".agents" / "skills" / "skill-a"
    canonical.mkdir(parents=True)
    (canonical / "SKILL.md").write_text("v1", encoding="utf-8")

    adapters.refresh_adapters(project, ["claude_code"], ["skill-a"], "copy")
    copied = project / ".claude" / "skills" / "skill-a" / "SKILL.md"
    assert copied.read_text(encoding="utf-8") == "v1"

    (canonical / "SKILL.md").write_text("v2", encoding="utf-8")
    adapters.refresh_adapters(project, ["claude_code"], ["skill-a"], "copy")
    assert copied.read_text(encoding="utf-8") == "v2"


def test_adapter_refresh_preserves_unmanaged_content(tmp_path):
    project = tmp_path / "project"
    canonical = project / ".agents" / "skills" / "skill-a"
    canonical.mkdir(parents=True)
    (canonical / "SKILL.md").write_text("managed", encoding="utf-8")
    rules = project / ".cursor" / "rules"
    rules.mkdir(parents=True)
    (rules / "handwritten.md").write_text("keep me", encoding="utf-8")

    adapters.refresh_adapters(project, ["cursor"], ["skill-a"], "copy")

    assert (rules / "handwritten.md").read_text(encoding="utf-8") == "keep me"
    assert (rules / ".csk-managed.json").exists()


def test_adapter_cleanup_removes_only_previous_managed_entries(tmp_path):
    project = tmp_path / "project"
    canonical = project / ".agents" / "skills" / "skill-a"
    canonical.mkdir(parents=True)
    (canonical / "SKILL.md").write_text("managed", encoding="utf-8")
    adapters.refresh_adapters(project, ["claude_code"], ["skill-a"], "copy")

    rules = project / ".claude" / "skills"
    (rules / "manual").mkdir()
    (rules / "manual" / "SKILL.md").write_text("manual", encoding="utf-8")
    adapters.refresh_adapters(project, ["claude_code"], [], "copy")

    assert not (rules / "skill-a").exists()
    assert (rules / "manual" / "SKILL.md").exists()


@pytest.mark.skipif(sys.platform == "win32", reason="Symlink mode requires Developer Mode on Windows")
def test_symlink_adapter_creates_link(tmp_path):
    project = tmp_path / "project"
    canonical = project / ".agents" / "skills" / "skill-a"
    canonical.mkdir(parents=True)
    adapters.refresh_adapters(project, ["claude_code"], ["skill-a"], "symlink")
    assert (project / ".claude" / "skills" / "skill-a").is_symlink()


def test_native_discovery_agents_are_known_and_get_no_project_mirror(tmp_path, capsys):
    project = tmp_path / "project"
    canonical = project / ".agents" / "skills" / "skill-a"
    canonical.mkdir(parents=True)
    (canonical / "SKILL.md").write_text("v1", encoding="utf-8")

    adapters.refresh_adapters(project, ["opencode", "windsurf"], ["skill-a"], "copy")

    assert capsys.readouterr().err == ""
    assert not (project / ".opencode").exists()
    assert not (project / ".windsurf").exists()
    assert (canonical / "SKILL.md").read_text(encoding="utf-8") == "v1"


def test_native_discovery_agents_add_no_extra_gitignore_entries():
    assert adapters.required_gitignore_entries(["opencode", "windsurf"]) == [".agents/"]


def test_global_install_mirrors_to_home_agents_skills_for_native_agents(tmp_path):
    csk_home = tmp_path / ".cocoaskills"
    canonical = csk_home / "global" / "skills" / "skill-a"
    canonical.mkdir(parents=True)
    (canonical / "SKILL.md").write_text("global", encoding="utf-8")
    home = tmp_path / "home"
    home.mkdir()

    adapters.refresh_global_adapters(csk_home, ["windsurf"], ["skill-a"], "copy", home=home)

    mirrored = home / ".agents" / "skills" / "skill-a" / "SKILL.md"
    assert mirrored.read_text(encoding="utf-8") == "global"
