from test_source_closure_refresh import *
from test_source_closure_refresh import _require_selection, _committed_cli_git_project, _read_lock, _http_rewriting_tool, _fresh_cli_home, _register_cli_v2_project
def test_review_stale_lock_real_transport(
    tmp_path: Path,
    skills_root: Path,
    csk_home: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """A legacy tag-object lock refuses without rewrite and points to csk upgrade."""

    _require_selection()
    from csk.build_repository import parse_repository_source

    project, kit, commit, url, _fake, _lock_before = _committed_cli_git_project(
        tmp_path,
        skills_root,
        csk_home,
        monkeypatch,
        annotated_tag=True,
    )
    tag_object = subprocess.run(
        ["git", "rev-parse", "refs/tags/v1"],
        cwd=kit,
        capture_output=True,
        text=True,
        check=True,
        timeout=20,
    ).stdout.strip()
    assert tag_object != commit

    old_lock = _read_lock(project)
    old_member = old_lock.members[0]
    assert isinstance(old_member.package, NetworkGit)
    bad_member = replace(
        old_member,
        package=replace(
            old_member.package,
            commit=lock_module.LockedCommit("sha1", tag_object),
        ),
    )
    bad_lock = replace(old_lock, members=(bad_member,), lock_sha256=None)
    legacy_lock_bytes = lock_module.serialize_lock(bad_lock, verify_digest=False)
    (project / publish.SKILLFILE_LOCK_NAME).write_bytes(legacy_lock_bytes)

    tool = _http_rewriting_tool(tmp_path / "locked-wrapper", kit, url)
    source = parse_repository_source(url)

    def acquire_locked_object(
        _identity: Any,
        lock: Any,
        _tool: Any = None,
        **_kwargs: Any,
    ) -> source_transport.AcquisitionResult:
        return source_transport.acquire(_identity, lock, tool, declared_url=url)

    monkeypatch.setattr(source_transport, "acquire_network", acquire_locked_object)
    fresh_home = _fresh_cli_home(tmp_path)
    _register_cli_v2_project(monkeypatch, fresh_home, skills_root, project)
    shutil.rmtree(project / ".agents" / "skills" / "nested")

    assert cli.main(["install", "app"]) == 1
    error = capsys.readouterr().err
    assert "source_lock_stale:" in error
    assert "remediation: run csk upgrade to refresh the lock" in error
    assert (project / publish.SKILLFILE_LOCK_NAME).read_bytes() == legacy_lock_bytes
