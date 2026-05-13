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

