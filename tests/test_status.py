from __future__ import annotations

from conftest import make_config, make_project, make_skill_repo, write_skillfile
from csk import installer, status


def test_status_reports_missing_and_up_to_date(tmp_path, skills_root, csk_home):
    project = make_project(tmp_path)
    make_skill_repo(skills_root, "skill-a", tag="v1")
    write_skillfile(project, {"schema_version": 1, "skills": [{"name": "skill-a", "tag": "v1"}]})
    cfg = make_config(csk_home, skills_root, project)

    before = status.render_status(cfg)
    assert "missing" in before
    assert not installer.install(cfg)[0].errors
    after = status.render_status(cfg)
    assert "up-to-date" in after


def test_status_reports_content_drift_and_install_restores(tmp_path, skills_root, csk_home):
    project = make_project(tmp_path)
    make_skill_repo(skills_root, "skill-a", tag="v1")
    write_skillfile(project, {"schema_version": 1, "skills": [{"name": "skill-a", "tag": "v1"}]})
    cfg = make_config(csk_home, skills_root, project)
    assert not installer.install(cfg)[0].errors

    installed_skill = project / ".agents" / "skills" / "skill-a" / "SKILL.md"
    installed_skill.write_text(installed_skill.read_text(encoding="utf-8") + "\ntampered\n", encoding="utf-8")

    drift = status.render_status(cfg)
    assert "content-drift" in drift

    result = installer.install(cfg)[0]
    restored = status.render_status(cfg)

    assert not result.errors
    assert "installed" in "\n".join(result.messages)
    assert "up-to-date" in restored
    assert "tampered" not in installed_skill.read_text(encoding="utf-8")
