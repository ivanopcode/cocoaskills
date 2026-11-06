from dataclasses import replace

import pytest

from conftest import make_config, make_project, make_skill_repo, write_skillfile
from csk import config
from csk.audit import canary, detectors, runner, trust


@pytest.mark.parametrize("old_ruleset", [1, 2, 3])
def test_previous_ruleset_cannot_hide_new_secret_findings(monkeypatch, tmp_path, csk_home, skills_root, old_ruleset):
    # A planted old ALLOW cache must be bypassed, while a current cache remains reusable.
    make_skill_repo(skills_root, "skill-a", {"csk-skill.json": '{"schema_version": 3, "capabilities": {"network": "none", "exec": "none"}, "commands": {}}', "references/value.txt": 'value="' + "ghp_" + "a" * 36 + '"\n'}, tag="v1")
    project = make_project(tmp_path)
    write_skillfile(project, {"schema_version": 1, "skills": [{"name": "skill-a", "tag": "v1"}]})
    cfg = make_config(csk_home, skills_root, project)
    cfg = replace(cfg, audit=config.AuditConfig(mode="strict", fail_on="high"))
    with monkeypatch.context() as old:
        old.setattr(trust, "RULESET_VERSION", old_ruleset)
        old.setattr(canary, "run_static_canary", lambda: True)
        old.setattr(detectors, "detect_snapshot", lambda *args: ())
        first = runner.audit_projects(cfg, alias="app")[0]
    assert first.decision == "allow"
    assert not first.cache_hit
    second = runner.audit_projects(cfg, alias="app")[0]
    assert not second.cache_hit
    assert second.decision == "block"
    assert "static.secret.github-token" in {f.id for f in second.findings}
    def unexpected_scan(*args):
        raise AssertionError("current ruleset cache should remain reusable")
    monkeypatch.setattr(detectors, "detect_snapshot", unexpected_scan)
    third = runner.audit_projects(cfg, alias="app")[0]
    assert third.cache_hit
    assert third.decision == "block"
