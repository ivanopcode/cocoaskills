"""Production CLI regressions for rewritten upstream tags (no network)."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from conftest import (
    commit_all, init_git_repo, make_config, make_project, run, write_files, write_skillfile,
)
from csk import cli, config, git_ops


@pytest.fixture
def tag_case(monkeypatch, tmp_path, skills_root, csk_home):
    source = init_git_repo(tmp_path / "upstream")
    write_files(source, {"SKILL.md": "---\nname: test\n---\n\n# Unchanged tree\n"})
    old = commit_all(source, "original history")
    for tag in ("v1", "v2"):
        run(["git", "tag", tag], source)
    for name in ("skill-a", "skill-b"):
        run(["git", "clone", str(source), str(skills_root / name)], tmp_path)
    project = make_project(tmp_path)
    declarations = [
        {"name": "skill-a", "tag": "v1"},
        {"name": "skill-b", "tag": "v2"},
    ]
    write_skillfile(project, {"schema_version": 1, "skills": declarations})
    cfg = make_config(csk_home, skills_root, project, agents=["codex_cli"])
    config.save_config(cfg)
    monkeypatch.setenv("CSK_CONFIG", str(cfg.path))
    global_root = csk_home / "global"
    global_root.mkdir()
    write_skillfile(global_root, {
        "schema_version": 1, "agents": ["codex_cli"], "skills": declarations,
    })
    return source, old, project, skills_root, global_root


def _rewrite(source: Path) -> str:
    # An orphan root with the SAME tree models filter-repo dropping signatures.
    run(["git", "checkout", "--orphan", "rewritten"], source)
    new = commit_all(source, "rewritten history")
    for tag in ("v1", "v2"):
        run(["git", "tag", "-f", tag, new], source)
    return new


def _markers(root: Path) -> list[Path]:
    return [root / "skills" / name / ".csk-install.json" for name in ("skill-a", "skill-b")]


# Both scopes accept unmoved tags. Rewritten tags are compared with installed
# markers even after update has already replaced cache refs; strict refusal
# preserves both installations and remains a refusal on a second attempt.
@pytest.mark.parametrize("global_scope", [False, True], ids=["project", "global"])
@pytest.mark.parametrize("mode", ["default", "strict", "update"])
def test_cli_repointed_tags(tag_case, capsys, global_scope, mode):
    source, old, project, skills_root, global_root = tag_case
    prefix = ["global"] if global_scope else []
    target = [] if global_scope else ["app"]
    assert cli.main([*prefix, "install", *target, "--strict-tags"]) == 0
    assert cli.main([*prefix, "upgrade", *target, "--strict-tags"]) == 0
    markers = _markers(global_root if global_scope else project / ".agents")
    before = [path.read_bytes() for path in markers]
    new = _rewrite(source)
    assert new != old
    assert run(["git", "rev-parse", f"{old}^{{tree}}"], source).stdout == run(
        ["git", "rev-parse", f"{new}^{{tree}}"], source,
    ).stdout
    capsys.readouterr()

    if mode == "update":
        assert cli.main([*prefix, "update"]) == 0
        output = capsys.readouterr()
        assert "fetched skill-a" in output.out and "fetched skill-b" in output.out
        assert not output.err
        assert [path.read_bytes() for path in markers] == before
        # update changes cache only; strict upgrade must still detect BOTH moves.
        assert cli.main([*prefix, "upgrade", *target, "--strict-tags"]) == cli.EXIT_PARTIAL_FAIL
        output = capsys.readouterr()
        assert "moved_tag:" in output.err
        for name, tag in (("skill-a", "v1"), ("skill-b", "v2")):
            assert f"Moved tag for {name}: {tag} {old} -> {new}" in output.err
        assert [path.read_bytes() for path in markers] == before

    attempts = 2 if mode == "strict" else 1
    for _ in range(attempts):
        args = [*prefix, "upgrade", *target]
        if mode == "strict":
            args.append("--strict-tags")
        assert cli.main(args) == (cli.EXIT_PARTIAL_FAIL if mode == "strict" else 0)
        output = capsys.readouterr()
        diagnostics = output.err if mode == "strict" else output.out
        assert "would clobber" not in output.out + output.err
        for name, tag in (("skill-a", "v1"), ("skill-b", "v2")):
            assert f"Moved tag for {name}: {tag} {old} -> {new}" in diagnostics
            assert git_ops.resolve_ref(skills_root / name, "tag", tag).commit == new
        if mode == "strict":
            assert "moved_tag:" in diagnostics
            assert [path.read_bytes() for path in markers] == before
        else:
            assert not output.err
            assert [json.loads(path.read_bytes())["commit"] for path in markers] == [new, new]


# Configured names are positional even when they start with '-'. In both CLI
# scopes, unmoved tags pass, strict moves preserve markers, and default accepts
# the moves with warnings; origin and an ordinary sole remote are controls.
@pytest.mark.parametrize("global_scope", [False, True], ids=["project", "global"])
@pytest.mark.parametrize("remote", ["origin", "upstream", "-operator"],
                         ids=["origin", "sole", "leading-hyphen"])
def test_cli_remote_name_is_positional(tag_case, capsys, global_scope, remote):
    source, old, project, skills_root, global_root = tag_case
    for name in ("skill-a", "skill-b"):
        if remote != "origin":
            run(["git", "remote", "rename", "--", "origin", remote], skills_root / name)
    prefix = ["global"] if global_scope else []
    target = [] if global_scope else ["app"]
    assert cli.main([*prefix, "install", *target]) == 0
    assert cli.main([*prefix, "upgrade", *target, "--strict-tags"]) == 0
    markers = _markers(global_root if global_scope else project / ".agents")
    before = [path.read_bytes() for path in markers]
    new = _rewrite(source)
    capsys.readouterr()

    assert cli.main([*prefix, "upgrade", *target, "--strict-tags"]) == cli.EXIT_PARTIAL_FAIL
    output = capsys.readouterr()
    assert "moved_tag:" in output.err
    for name, tag in (("skill-a", "v1"), ("skill-b", "v2")):
        assert f"Moved tag for {name}: {tag} {old} -> {new}" in output.err
        assert git_ops.git(skills_root / name, [
            "rev-parse", f"refs/remotes/{remote}/rewritten",
        ]).stdout.strip() == new
    assert [path.read_bytes() for path in markers] == before

    assert cli.main([*prefix, "upgrade", *target]) == 0
    output = capsys.readouterr()
    assert not output.err
    for name, tag in (("skill-a", "v1"), ("skill-b", "v2")):
        assert f"Moved tag for {name}: {tag} {old} -> {new}" in output.out
    assert [json.loads(path.read_bytes())["commit"] for path in markers] == [new, new]


# A real inaccessible remote remains a fetch failure, without moved-tag
# diagnostics or partial installation writes (both CLI callers and update).
@pytest.mark.parametrize("global_scope", [False, True], ids=["project", "global"])
@pytest.mark.parametrize("command", ["upgrade", "update"])
def test_cli_fetch_failure_is_not_moved_tag(tag_case, capsys, tmp_path, global_scope, command):
    source, old, project, skills_root, global_root = tag_case
    prefix = ["global"] if global_scope else []
    target = [] if global_scope else ["app"]
    assert cli.main([*prefix, "install", *target]) == 0
    markers = _markers(global_root if global_scope else project / ".agents")
    before = [path.read_bytes() for path in markers]
    for name in ("skill-a", "skill-b"):
        run(["git", "remote", "set-url", "origin", str(tmp_path / "missing.git")], skills_root / name)
    capsys.readouterr()
    args = [*prefix, command, *(target if command == "upgrade" else [])]
    assert cli.main(args) == cli.EXIT_PARTIAL_FAIL
    output = capsys.readouterr()
    assert "fetch" in output.err.lower()
    assert "missing.git" in output.err
    assert "Moved tag" not in output.out + output.err
    assert "moved_tag:" not in output.out + output.err
    assert [path.read_bytes() for path in markers] == before


# Remote ownership is deterministic: origin wins over conflicting remotes,
# a sole renamed remote works, ambiguous ownership refuses before any ref write.
@pytest.mark.parametrize("policy", ["origin", "sole", "ambiguous", "local"])
def test_fetch_remote_ownership(tag_case, tmp_path, policy):
    source, old, project, skills_root, global_root = tag_case
    repo = skills_root / "skill-a"
    if policy != "origin":
        run(["git", "remote", "rename", "origin", "upstream"], repo)
    if policy in {"origin", "ambiguous"}:
        run(["git", "remote", "add", "other", str(skills_root / "skill-b")], repo)
    if policy == "local":
        run(["git", "remote", "remove", "upstream"], repo)
    new = _rewrite(source)
    if policy == "ambiguous":
        with pytest.raises(git_ops.GitError, match="Ambiguous fetch remote.*other, upstream"):
            git_ops.fetch_repo(repo)
        assert git_ops.resolve_ref(repo, "tag", "v1").commit == old
    else:
        git_ops.fetch_repo(repo)
        assert git_ops.resolve_ref(repo, "tag", "v1").commit == (old if policy == "local" else new)
        if policy == "sole":
            assert git_ops.git(repo, ["rev-parse", "refs/remotes/upstream/rewritten"]).stdout.strip() == new


# The strict gate also rejects exactly one moved tag while accepting the
# unchanged neighboring tag; a v1-only policy exception must fail this test.
@pytest.mark.parametrize("global_scope", [False, True], ids=["project", "global"])
def test_cli_strict_refuses_single_moved_v1(tag_case, capsys, global_scope):
    source, old, project, skills_root, global_root = tag_case
    prefix = ["global"] if global_scope else []
    target = [] if global_scope else ["app"]
    assert cli.main([*prefix, "install", *target]) == 0
    markers = _markers(global_root if global_scope else project / ".agents")
    before = [path.read_bytes() for path in markers]
    new = _rewrite(source)
    run(["git", "tag", "-f", "v2", old], source)
    capsys.readouterr()
    assert cli.main([*prefix, "upgrade", *target, "--strict-tags"]) == cli.EXIT_PARTIAL_FAIL
    output = capsys.readouterr()
    assert "moved_tag:" in output.err
    assert f"Moved tag for skill-a: v1 {old} -> {new}" in output.err
    assert "Moved tag for skill-b" not in output.err
    assert [path.read_bytes() for path in markers] == before
