from __future__ import annotations

from csk import whitelist


def test_whitelist_copies_only_allowed_and_prunes_nested_excludes(tmp_path):
    snapshot = tmp_path / "snapshot"
    snapshot.mkdir()
    files = {
        "SKILL.md": "# skill\n",
        "README.md": "readme\n",
        "examples/ok.txt": "ok\n",
        "examples/tests/bad.txt": "bad\n",
        "dependencies.json": '{"dependencies": []}\n',
        "references/ref.md": "ref\n",
        "scripts/tool": "tool\n",
    }
    for rel, content in files.items():
        path = snapshot / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")

    copied = whitelist.copy_context(snapshot, tmp_path / "out")
    assert "SKILL.md" in copied
    assert "examples/ok.txt" in copied
    assert "examples/tests/bad.txt" not in copied
    assert "dependencies.json" not in copied
    assert "scripts/tool" not in copied
    assert not (tmp_path / "out" / "dependencies.json").exists()
    assert not (tmp_path / "out" / "README.md").exists()


def test_missing_skill_md_fails(tmp_path):
    snapshot = tmp_path / "snapshot"
    snapshot.mkdir()
    try:
        whitelist.copy_context(snapshot, tmp_path / "out")
    except whitelist.WhitelistError as exc:
        assert "SKILL.md" in str(exc)
    else:
        raise AssertionError("expected WhitelistError")


def test_nested_build_root_is_excluded_while_skill_and_unrelated_assets_remain(tmp_path):
    snapshot = tmp_path / "snapshot"
    snapshot.mkdir()
    files = {
        "SKILL.md": "# skill\n",
        "assets/prompt.md": "visible\n",
        "assets/build-tool/go.mod": "module example.com/tool\n",
        "assets/build-tool/cmd/tool/main.go": "package main\n",
        "assets/build-tool/internal/README.md": "build-only\n",
        "assets/build-toolkit/guide.md": "similarly named but visible\n",
    }
    for relative, content in files.items():
        path = snapshot / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")

    destination = tmp_path / "out"
    copied = whitelist.copy_context(
        snapshot,
        destination,
        build_roots=("assets/build-tool",),
    )

    assert copied == [
        "SKILL.md",
        "assets/build-toolkit/guide.md",
        "assets/prompt.md",
    ]
    assert (destination / "SKILL.md").is_file()
    assert (destination / "assets" / "prompt.md").is_file()
    assert not (destination / "assets" / "build-tool").exists()
