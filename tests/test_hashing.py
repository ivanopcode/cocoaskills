from __future__ import annotations

from csk import hashing


def test_hash_excludes_marker_and_is_stable(tmp_path):
    root = tmp_path / "root"
    root.mkdir()
    (root / "b.txt").write_text("b", encoding="utf-8")
    (root / "a.txt").write_text("a", encoding="utf-8")
    first = hashing.content_sha256(root)
    (root / ".csk-install.json").write_text("ignored", encoding="utf-8")
    assert hashing.content_sha256(root) == first
    assert first.startswith("sha256:")
    assert len(first) == len("sha256:") + 64

