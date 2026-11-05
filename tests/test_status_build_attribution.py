# A healthy installed build must not receive its missing sibling's error as its own detail.
def test_attribution_healthy_build(tmp_path, skills_root, csk_home, monkeypatch):
    from conftest import make_config, make_project, make_skill_repo, write_skillfile
    from test_installer_transactions import _build_skill_files, _install_fake_build_pipeline
    from test_build_currentness import _fixed_evidence
    from csk import global_install, status
    project = make_project(tmp_path)
    cfg = make_config(csk_home, skills_root, project)
    make_skill_repo(skills_root, 'healthy', _build_skill_files('tool'), tag='v1')
    _install_fake_build_pipeline(monkeypatch, events=[])
    _fixed_evidence(monkeypatch)
    root = csk_home / 'global'
    root.mkdir()
    declarations = [{'name':'healthy','tag':'v1'}]
    write_skillfile(root, {'schema_version':1,'skills':declarations})
    assert not global_install.install(cfg).errors
    before = status.collect_global_status(cfg)
    assert before.clean and before.builds[0].current
    declarations.append({'name':'absent','tag':'v1'})
    write_skillfile(root, {'schema_version':1,'skills':declarations})
    result = status.collect_global_status(cfg)
    assert result.skills[0].label == 'up-to-date'
    assert result.skills[1].label == 'error'
    assert not result.clean and result.errors
    payload = status.global_status_to_payload(result)
    assert payload['builds'][0]['provider'] == 'healthy'
    assert 'absent' not in (payload['builds'][0]['detail'] or ''), payload['builds'][0]

    assert payload["builds"][0]["detail"] == "Build currentness unknown because scope closure resolution failed"
    assert not payload["builds"][0]["current"]
    assert "absent" in payload["errors"][0]
    assert "scope closure resolution failed" in status.render_global_collected(result)
