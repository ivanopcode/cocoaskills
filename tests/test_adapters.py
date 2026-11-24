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


@pytest.mark.skipif(
    sys.platform == "win32", reason="Symlink mode requires Developer Mode on Windows"
)
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


def test_refresh_refuses_foreign_ledger_bytes(tmp_path):
    """Direct refresh refuses a foreign ledger instead of overwriting it.

    The direct-write path shares ``_read_managed`` with the
    transaction planner, so it inherits the same adoption rule
    (BUG-260918-wvfoqa): user bytes at the ledger path refuse with
    the path and the shape.
    """

    project = tmp_path / "project"
    canonical = project / ".agents" / "skills" / "skill-a"
    canonical.mkdir(parents=True)
    (canonical / "SKILL.md").write_text("managed", encoding="utf-8")
    rules = project / ".claude" / "skills"
    rules.mkdir(parents=True)
    ledger = rules / ".csk-managed.json"
    ledger.write_text("MY PRECIOUS USER JSON\n", encoding="utf-8")

    with pytest.raises(adapters.AdapterError, match=r"not a csk ledger \(not JSON\)"):
        adapters.refresh_adapters(project, ["claude_code"], ["skill-a"], "copy")

    assert ledger.read_text(encoding="utf-8") == "MY PRECIOUS USER JSON\n"


def test_global_install_mirrors_to_home_agents_skills_for_native_agents(tmp_path):
    csk_home = tmp_path / ".cocoaskills"
    canonical = csk_home / "global" / "skills" / "skill-a"
    canonical.mkdir(parents=True)
    (canonical / "SKILL.md").write_text("global", encoding="utf-8")
    home = tmp_path / "home"
    home.mkdir()

    adapters.refresh_global_adapters(
        csk_home, ["windsurf"], ["skill-a"], "copy", home=home
    )

    mirrored = home / ".agents" / "skills" / "skill-a" / "SKILL.md"
    assert mirrored.read_text(encoding="utf-8") == "global"


def test_transaction_auto_mode_probes_only_private_staging(monkeypatch, tmp_path):
    stage_root = tmp_path / "private-stage"
    live_root = tmp_path / "project"
    stage_root.mkdir()
    live_root.mkdir()
    probed = []
    monkeypatch.setattr(
        adapters,
        "_link_probe",
        lambda directory: probed.append(directory) or True,
    )

    supported = adapters._transaction_links_supported(
        stage_root,
        live_root / ".claude" / "skills" / "skill-a",
    )

    assert supported
    assert probed == [stage_root]
    assert list(live_root.iterdir()) == []


def test_transaction_auto_mode_rejects_cross_device_staging_without_probe(
    monkeypatch, tmp_path
):
    stage_root = tmp_path / "private-stage"
    live_root = tmp_path / "project"
    live_directory = live_root / ".claude" / "skills"
    stage_root.mkdir()
    live_directory.mkdir(parents=True)
    probed = []
    monkeypatch.setattr(
        adapters,
        "_device_id",
        lambda path: 1 if path == stage_root else 2,
    )
    monkeypatch.setattr(
        adapters,
        "_link_probe",
        lambda directory: probed.append(directory) or True,
    )

    supported = adapters._transaction_links_supported(
        stage_root,
        live_directory / "skill-a",
    )

    assert not supported
    assert probed == []
    assert list(live_root.rglob("*")) == [
        live_root / ".claude",
        live_directory,
    ]


@pytest.mark.skipif(sys.platform == "win32", reason="Creates POSIX directory symlinks")
def test_adapters_follow_symlinked_user_agent_dirs_preserving_unmanaged_files(tmp_path):
    # User-owned agent directories (such as a dotfiles-symlinked .claude or ~/.claude) remain followed during adapter refresh and planning, removing only ledger-managed entries while preserving unmanaged files; a symlinked .csk-managed.json ledger still refuses.
    dotfiles_claude = tmp_path / "dotfiles-claude"
    (dotfiles_claude / "skills" / "old-skill").mkdir(parents=True)
    (dotfiles_claude / "skills" / "old-skill" / "SKILL.md").write_text("stale", encoding="utf-8")
    (dotfiles_claude / "skills" / "handwritten.md").write_text("keep-user", encoding="utf-8")
    (dotfiles_claude / "skills" / ".csk-managed.json").write_text(
        '{"entries": ["old-skill"], "schema_version": 1}\n',
        encoding="utf-8",
    )

    project = tmp_path / "project"
    canonical = project / ".agents" / "skills" / "new-skill"
    canonical.mkdir(parents=True)
    (canonical / "SKILL.md").write_text("new", encoding="utf-8")
    (project / ".claude").symlink_to(dotfiles_claude, target_is_directory=True)

    # Planning and direct refresh succeed through symlinked user-owned .claude,
    # removing stale ledger entry while preserving unmanaged handwritten.md.
    targets = adapters.plan_project_adapter_targets(
        project,
        ["claude_code"],
        [adapters.AdapterGroup(canonical_root=project / ".agents" / "skills", skill_names=("new-skill",))],
    )
    assert any(t.desired_kind == "remove" and t.skill_name == "old-skill" for t in targets)
    adapters.refresh_adapters(project, ["claude_code"], ["new-skill"], "copy")
    assert not (dotfiles_claude / "skills" / "old-skill").exists()
    assert (dotfiles_claude / "skills" / "new-skill" / "SKILL.md").read_text(encoding="utf-8") == "new"
    assert (dotfiles_claude / "skills" / "handwritten.md").read_text(encoding="utf-8") == "keep-user"

    # Global symlinked ~/.claude also succeeds and preserves unmanaged files.
    csk_home = tmp_path / ".cocoaskills"
    global_canonical = csk_home / "global" / "skills" / "global-skill"
    global_canonical.mkdir(parents=True)
    (global_canonical / "SKILL.md").write_text("global", encoding="utf-8")
    user_home = tmp_path / "user-home"
    user_home.mkdir()
    global_dotfiles = tmp_path / "global-dotfiles-claude"
    (global_dotfiles / "skills").mkdir(parents=True)
    (global_dotfiles / "skills" / "manual.md").write_text("keep-global", encoding="utf-8")
    (user_home / ".claude").symlink_to(global_dotfiles, target_is_directory=True)

    adapters.refresh_global_adapters(csk_home, ["claude_code"], ["global-skill"], "copy", home=user_home)
    assert (global_dotfiles / "skills" / "global-skill" / "SKILL.md").read_text(encoding="utf-8") == "global"
    assert (global_dotfiles / "skills" / "manual.md").read_text(encoding="utf-8") == "keep-global"

    # Negative control: a symlinked .csk-managed.json ledger inside the adapter dir still refuses.
    outside_ledger = tmp_path / "outside-ledger.json"
    outside_ledger.write_text('{"entries": ["new-skill"], "schema_version": 1}\n', encoding="utf-8")
    ledger = dotfiles_claude / "skills" / ".csk-managed.json"
    ledger.unlink()
    ledger.symlink_to(outside_ledger)
    with pytest.raises(adapters.AdapterError, match="is a link"):
        adapters.refresh_adapters(project, ["claude_code"], [], "copy")
    assert (dotfiles_claude / "skills" / "new-skill" / "SKILL.md").is_file()
