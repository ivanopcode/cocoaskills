from pathlib import Path

import pytest

from csk.audit import detectors
from csk.audit.capabilities import CapabilityManifest
from csk.whitelist import INCLUDE_ROOTS


def scan(root):
    return detectors.detect_snapshot(root, CapabilityManifest.implicit_none())


# Context roots, conventional scripts and arbitrary manifest runtime roots all
# receive generic scanning. This does not claim language-specific AST support.
@pytest.mark.parametrize("root", sorted(INCLUDE_ROOTS - {"SKILL.md", ".skill_triggers"}) + ["scripts", "custom-runtime"])
@pytest.mark.parametrize("name", ["run", "run.py", "run.js", "run.mjs", "run.cjs", "run.ps1", "run.cmd", "run.bat", "run.rb", "run.pl", "run.lua", "run.SH", "run.unusual"])
def test_all_roots_and_script_names_are_scanned(tmp_path, root, name):
    path = tmp_path / root / name
    path.parent.mkdir()
    path.write_text('# synthetic value: "' + "ghp_" + "a" * 36 + '"\n')
    assert "static.secret.github-token" in {f.id for f in scan(tmp_path)}
    path.write_text("# ordinary benign text\n")
    assert not scan(tmp_path)


@pytest.mark.parametrize("name", ["SKILL.md", ".skill_triggers", "agent-skill.json", "custom-runtime/run.js"])
@pytest.mark.parametrize("raw,reason", [
    (b"x" * 129, "size exceeds text limit"),
    (b"normal\x00binary", "NUL byte (binary content)"),
    (b"invalid\xff", "invalid UTF-8"),
])
def test_skipped_files_have_exact_reason(tmp_path, monkeypatch, name, raw, reason):
    # Each rejected input produces exactly one reason; bounded UTF-8 is scanned.
    monkeypatch.setattr(detectors, "MAX_TEXT_BYTES", 128)
    path = tmp_path / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(raw)
    findings = scan(tmp_path)
    assert len(findings) == 1
    assert findings[0].id == "static.opaque.unanalyzable-artifact"
    assert findings[0].evidence == "scan skipped: " + reason
    assert findings[0].location.file == name
    # A manifest control must also be valid JSON after bounded parser admission.
    path.write_bytes(b"{}" + b" " * 126 if name == "agent-skill.json" else b" " * 128)
    assert not scan(tmp_path)


def test_read_error_does_not_leak_exception(tmp_path, monkeypatch):
    # Fault injection proves read errors are reasoned without copying exception data.
    path = tmp_path / "run.js"
    path.write_text("// normal text\n")
    original = Path.open
    def fail(self, *args, **kwargs):
        if self == path:
            raise OSError("sensitive exception payload")
        return original(self, *args, **kwargs)
    with monkeypatch.context() as patch:
        patch.setattr(Path, "open", fail)
        findings = scan(tmp_path)
    assert len(findings) == 1
    assert findings[0].evidence == "scan skipped: read error"
    assert "sensitive" not in repr(findings)
    assert not scan(tmp_path)


def test_size_gate_precedes_open_and_read_is_bounded(tmp_path, monkeypatch):
    # Oversize metadata prevents opening; growth after stat is caught by limit+1 read.
    import io
    monkeypatch.setattr(detectors, "MAX_TEXT_BYTES", 128)
    path = tmp_path / "agent-skill.json"
    path.write_bytes(b" " * 129)
    requests = []
    class Reader(io.BytesIO):
        def read(self, size=-1):
            requests.append(size)
            assert size == 129
            return super().read(size)
    def opened(self, *args, **kwargs):
        assert self.stat().st_size <= 128
        return Reader(b" " * 130)
    monkeypatch.setattr(Path, "open", opened)
    assert scan(tmp_path)[0].evidence == "scan skipped: size exceeds text limit"
    assert requests == []
    # Truncate via the original builtin, avoiding the injected reader.
    with open(path, "wb") as stream:
        stream.write(b" ")
    assert scan(tmp_path)[0].evidence == "scan skipped: size exceeds text limit"
    assert requests == [129]


@pytest.mark.parametrize("maximum", [128, 1_000_000])
@pytest.mark.parametrize("extra", [-1, 0, 1])
def test_shared_reader_preserves_scan_limit_with_short_reads(tmp_path, monkeypatch, maximum, extra):
    # Chunk requests stay <=64 KiB, short reads drain, and exactly limit bytes
    # remain admitted while growth by one byte produces the specific omission.
    import io
    monkeypatch.setattr(detectors, "MAX_TEXT_BYTES", maximum)
    path = tmp_path / "run.txt"
    path.write_bytes(b"\n")
    body = b"\n" * (maximum + extra)
    requests = []
    returned = []
    class Reader(io.BytesIO):
        def read(self, size=-1):
            requests.append(size)
            assert 0 < size <= 64 * 1024
            chunk = super().read(min(size, 4093))
            returned.append(len(chunk))
            return chunk
    monkeypatch.setattr(Path, "open", lambda *args, **kwargs: Reader(body))
    findings = scan(tmp_path)
    assert requests and sum(returned) == len(body)
    if extra > 0:
        assert len(findings) == 1
        assert findings[0].evidence == "scan skipped: size exceeds text limit"
    else:
        assert not findings


def test_minified_text_in_custom_root_is_opaque(tmp_path):
    # Minification detection covers arbitrary roots, with a multiline control.
    path = tmp_path / "custom-runtime" / "bundle.js"
    path.parent.mkdir()
    path.write_text("x" * 10000)
    assert scan(tmp_path)[0].evidence == "minified or bundled text"
    path.write_text("// benign line\n" * 800)
    assert not scan(tmp_path)


def test_symlink_is_not_followed(tmp_path):
    # A link cannot expand the scan outside the snapshot; regular content is scanned.
    target = tmp_path / "SKILL.md"
    target.write_text("ordinary text\n")
    (tmp_path / "linked").symlink_to(target)
    findings = scan(tmp_path)
    assert len(findings) == 1
    assert findings[0].evidence == "scan skipped: symbolic link"
