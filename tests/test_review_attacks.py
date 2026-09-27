from test_source_closure_refresh import *
from test_source_closure_refresh import _bare_repo_with_tag, _http_rewriting_tool
def test_review_duplicate_peeled(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The production resolver fails closed on an unrequested ls-remote ref."""

    from csk.build_repository import parse_repository_source

    bare, tag_commit, _, _ = _bare_repo_with_tag(tmp_path)
    url = "https://example.test/kit.git"
    tool = _http_rewriting_tool(tmp_path / "wrapper", bare, url)
    source = parse_repository_source(url)

    def advertise_stray_ref(
        _tool: git_admission.GitTool,
        _paths: Any,
        _environment: Any,
        _arguments: Any,
        *,
        limits: git_admission.Limits,
    ) -> bytes:
        del limits
        return (
            f"{tag_commit}\trefs/tags/v1\n"
            f"{tag_commit}\trefs/tags/v1^{{}}\n"
            f"{tag_commit}\trefs/tags/v1^{{}}\n"
        ).encode()

    monkeypatch.setattr(git_admission, "_run_git_ls_remote", advertise_stray_ref)
    with pytest.raises(git_admission.GitAdmissionError) as captured:
        git_admission.resolve_network_ref(source, "tag", "v1", tool)
    assert captured.value.code == git_admission.OBJECT_SEMANTICS_INVALID
