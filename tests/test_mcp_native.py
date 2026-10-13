"""Tests for the native MCP config codecs (slice 1, pure bytes).

Golden round trips freeze exact output bytes; refusal tests drive every
malformed/unreadable shape; the seeded property tests check P1 (semantic)
and P2 (single-span bytes) over generated documents. Each narrowing mutant
from the brief is killed by a named test referenced here.
"""

from __future__ import annotations

import copy
import difflib
import hashlib
import json
import os
import random
import re
import stat
import tomllib
from pathlib import Path

import pytest

from csk import mcp_native as mn
from csk.mcp_native import AddEntry, RemoveEntry, ReplaceEntry

FIXTURES = Path(__file__).resolve().parent / "fixtures" / "mcp" / "native"

CC = mn.project_surface("claude_code")
CURSOR = mn.project_surface("cursor")
CODEX = mn.project_surface("codex_cli")

MAX_BYTES = 4 * 1024 * 1024
TOO_LARGE = "mcp_native_config_too_large"
UNSUP = "mcp_native_config_unsupported_syntax"
CONFLICT = "mcp_entry_conflict"
INVALID = "mcp_declaration_invalid"

V_SMALL = {"command": "new-cmd"}
V_HTTP = {"type": "http", "url": "https://example.invalid/mcp"}
V_REPL = {"type": "stdio", "command": "uvx", "args": ["example-tools==1.2.3"]}


def fixture(name: str) -> bytes:
    return (FIXTURES / name).read_bytes()


def to_crlf(data: bytes) -> bytes:
    assert b"\r" not in data
    return data.replace(b"\n", b"\r\n")


def sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def assert_p2(old: bytes, new: bytes, span: tuple[int, int]) -> None:
    """Assert byte identity outside one explicit span (byte offsets)."""
    start, end = span
    assert 0 <= start <= end <= len(old)
    assert new[:start] == old[:start]
    tail = len(old) - end
    if tail:
        assert len(new) - tail >= start
        assert new[len(new) - tail :] == old[end:]
    else:
        assert len(new) >= start


def changed_blocks(old: bytes, new: bytes) -> int:
    matcher = difflib.SequenceMatcher(a=old, b=new, autojunk=False)
    return sum(1 for tag, *_rest in matcher.get_opcodes() if tag != "equal")


def assert_single_span(old: bytes, new: bytes) -> None:
    assert changed_blocks(old, new) == 1


def _common_affixes(old: bytes, new: bytes) -> tuple[int, int]:
    """Maximal common prefix length and maximal non-overlapping suffix length."""
    limit = min(len(old), len(new))
    prefix = 0
    while prefix < limit and old[prefix] == new[prefix]:
        prefix += 1
    suffix = 0
    while (
        suffix < len(old) - prefix
        and suffix < len(new) - prefix
        and old[len(old) - 1 - suffix] == new[len(new) - 1 - suffix]
    ):
        suffix += 1
    return prefix, suffix


def assert_p2_confinement(
    old: bytes,
    new: bytes,
    marker: bytes,
    *,
    in_prefix: bool,
    in_old_mid: bool,
    in_new_mid: bool,
) -> None:
    """Assert the change is one window pinned to the target member marker.

    The maximal common affixes must be identical, and the unique target
    marker must sit exactly where a single-span edit puts it: an added
    marker only in the new middle, a replaced key only in the untouched
    prefix, a removed marker only in the old middle.
    """
    prefix, suffix = _common_affixes(old, new)
    assert new[:prefix] == old[:prefix]
    old_mid = old[prefix : len(old) - suffix] if suffix else old[prefix:]
    new_mid = new[prefix : len(new) - suffix] if suffix else new[prefix:]
    if suffix:
        assert new[len(new) - suffix :] == old[len(old) - suffix :]
    assert (marker in old[:prefix]) is in_prefix
    assert (marker in old_mid) is in_old_mid
    assert (marker in new_mid) is in_new_mid


def try_symlink(link: Path, target: Path, *, directory: bool = False) -> None:
    link.parent.mkdir(parents=True, exist_ok=True)
    try:
        link.symlink_to(target, target_is_directory=directory)
    except OSError:
        if os.name == "nt":
            pytest.skip("Windows runner cannot create symlinks")
        raise


# ---------------------------------------------------------------------------
# Surfaces and frozen API shape
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("agent", "relpath", "syntax", "container", "capability"),
    [
        ("claude_code", ".mcp.json", "json", "mcpServers", "claude_code/project/v1"),
        ("cursor", ".cursor/mcp.json", "json", "mcpServers", "cursor/project/v1"),
        (
            "codex_cli",
            ".codex/config.toml",
            "toml",
            "mcp_servers",
            "codex_cli/project/v1",
        ),
    ],
)
def test_project_surface_agents(agent, relpath, syntax, container, capability):
    surface = mn.project_surface(agent)
    assert surface.agent == agent
    assert surface.scope == "project"
    assert surface.relpath == relpath
    assert surface.syntax == syntax
    assert surface.container == container
    assert surface.capability == capability


@pytest.mark.parametrize("agent", ["windsurf", "gemini", "", "claude-code"])
def test_project_surface_unknown_agent_raises(agent):
    with pytest.raises(mn.McpNativeError) as exc_info:
        mn.project_surface(agent)
    assert exc_info.value.code == "mcp_capability_unsupported"
    assert exc_info.value.surface == ""
    assert isinstance(exc_info.value, ValueError)


def test_non_canonical_surface_is_rejected():
    bad = mn.NativeSurface(
        agent="claude_code",
        scope="project",
        relpath="evil.json",
        syntax="json",
        container="mcpServers",
        capability="claude_code/project/v1",
    )
    with pytest.raises(mn.McpNativeError) as exc_info:
        mn.parse_surface_bytes(bad, b"{}")
    assert exc_info.value.code == "mcp_capability_unsupported"


# ---------------------------------------------------------------------------
# JSON reads
# ---------------------------------------------------------------------------


def test_parse_json_present_entries_and_hash():
    data = b'{"mcpServers": {"a": {"command": "x"}}, "other": 1}'
    read = mn.parse_surface_bytes(CC, data)
    assert read.state == "present"
    assert read.entries == {"a": {"command": "x"}}
    assert read.raw_sha256 == sha(data)
    assert read.reason is None


def test_parse_json_container_absent_is_present_empty():
    read = mn.parse_surface_bytes(CC, b'{"other": 1}')
    assert read.state == "present"
    assert read.entries == {}


@pytest.mark.parametrize(
    "data",
    [
        b'{"mcpServers": {"a": 1 /* c */}}',
        b'// comment\n{"mcpServers": {}}',
        b'{"mcpServers": {"a": 1,}}',
        b'{"mcpServers": {"a": 1},,}',
        b'\xef\xbb\xbf{"mcpServers": {}}',
        b'{"mcpServers": {"a": "\xff"}}',
        b'{"mcpServers": {"a": 1}, "mcpServers": {}}',
        b'{"mcpServers": {"a": 1, "a": 2}}',
        b'[{"mcpServers": {}}]',
        b'"str"',
        b'{"mcpServers": []}',
        b'{"mcpServers": "x"}',
        b'{"mcpServers": {"a": "\\ud800"}}',
        b"",
        b"   ",
        b"{",
    ],
)
def test_parse_json_malformed_shapes(data):
    read = mn.parse_surface_bytes(CC, data)
    assert read.state == "malformed"
    assert read.entries == {}
    assert read.raw_sha256 == sha(data)
    assert read.reason == UNSUP


def test_parse_json_duplicate_keys_are_malformed():
    """Kills M4 (duplicate-key check removed): plain json.loads would admit."""
    read = mn.parse_surface_bytes(CC, b'{"mcpServers": {"a": 1, "a": 2}}')
    assert read.state == "malformed"
    assert read.reason == UNSUP


def test_parse_json_refusal_carries_no_content():
    read = mn.parse_surface_bytes(CC, b'{"mcpServers": {"a": 1, /* SECRET-TOKEN */}}')
    assert read.state == "malformed"
    assert read.reason is not None and "SECRET" not in read.reason


# ---------------------------------------------------------------------------
# TOML reads
# ---------------------------------------------------------------------------


def test_parse_toml_present_foreign_forms():
    read = mn.parse_surface_bytes(CODEX, fixture("codex_foreign.toml"))
    assert read.state == "present"
    assert read.reason is None
    assert set(read.entries) == {"dotted_entry", "quoted.entry"}
    assert read.entries["dotted_entry"] == {
        "command": "run-dotted",
        "args": ["a", "b"],
    }


def test_parse_toml_empty_file_is_present_empty():
    read = mn.parse_surface_bytes(CODEX, b"")
    assert read.state == "present"
    assert read.entries == {}


def test_parse_toml_container_absent_is_present_empty():
    read = mn.parse_surface_bytes(CODEX, b'[tool]\nx = 1\n')
    assert read.state == "present"
    assert read.entries == {}


@pytest.mark.parametrize(
    "data",
    [
        b'\xef\xbb\xbf[tool]\nx = 1\n',
        b'[tool]\nx = "\xff"\n',
        b"[mcp_servers.docs]\nurl = \"u\"\n[mcp_servers.docs]\nurl = \"v\"\n",
        b'[tool]\nx = 1\nx = 2\n',
        b'mcp_servers = "x"\n',
        b"[[mcp_servers]]\n",
        b"[unclosed\n",
    ],
)
def test_parse_toml_malformed_shapes(data):
    read = mn.parse_surface_bytes(CODEX, data)
    assert read.state == "malformed"
    assert read.entries == {}
    assert read.raw_sha256 == sha(data)
    assert read.reason == UNSUP


@pytest.mark.parametrize("surface", [CC, CODEX])
def test_parse_oversize_is_malformed_too_large(surface):
    data = b"x" * (MAX_BYTES + 1)
    read = mn.parse_surface_bytes(surface, data)
    assert read.state == "malformed"
    assert read.reason == TOO_LARGE
    assert read.raw_sha256 == sha(data)


def test_parse_size_boundary_exact_max_is_not_too_large():
    json_read = mn.parse_surface_bytes(CC, b" " * MAX_BYTES)
    assert json_read.state == "malformed"
    assert json_read.reason == UNSUP
    toml_data = b"#c\n" * 1398101 + b"#"
    assert len(toml_data) == MAX_BYTES
    toml_read = mn.parse_surface_bytes(CODEX, toml_data)
    assert toml_read.state == "present"
    assert toml_read.entries == {}


# ---------------------------------------------------------------------------
# read_surface: typed no-follow reads
# ---------------------------------------------------------------------------


def test_read_surface_absent(tmp_path):
    read, data = mn.read_surface(tmp_path, CC)
    assert (read.state, read.entries, read.raw_sha256, read.reason, data) == (
        "absent",
        {},
        None,
        None,
        None,
    )


def test_read_surface_absent_missing_parent_dir(tmp_path):
    read, data = mn.read_surface(tmp_path, CURSOR)
    assert read.state == "absent"
    assert data is None


def test_read_surface_present_round_trip(tmp_path):
    (tmp_path / ".mcp.json").write_bytes(b'{"mcpServers": {"a": {"command": "x"}}}')
    read, data = mn.read_surface(tmp_path, CC)
    assert read.state == "present"
    assert read.entries == {"a": {"command": "x"}}
    assert data == b'{"mcpServers": {"a": {"command": "x"}}}'
    assert read.raw_sha256 == sha(data)


def test_read_surface_nested_present(tmp_path):
    target = tmp_path / ".codex" / "config.toml"
    target.parent.mkdir()
    target.write_bytes(b'[mcp_servers.docs]\nurl = "u"\n')
    read, data = mn.read_surface(tmp_path, CODEX)
    assert read.state == "present"
    assert read.entries == {"docs": {"url": "u"}}
    assert data == b'[mcp_servers.docs]\nurl = "u"\n'


def test_dangling_symlink_is_unreadable_not_absent(tmp_path):
    """Kills M3 (dangling link reported as absent)."""
    link = tmp_path / ".mcp.json"
    try_symlink(link, tmp_path / "does-not-exist")
    read, data = mn.read_surface(tmp_path, CC)
    assert read.state == "unreadable"
    assert read.reason == "link"
    assert data is None


def test_symlinked_file_is_unreadable(tmp_path):
    target = tmp_path / "real.json"
    target.write_bytes(b"{}")
    try_symlink(tmp_path / ".mcp.json", target)
    read, data = mn.read_surface(tmp_path, CC)
    assert (read.state, read.reason, data) == ("unreadable", "link", None)


@pytest.mark.parametrize("surface", [CURSOR, CODEX])
def test_symlinked_parent_dir_is_unreadable(tmp_path, surface):
    parent = surface.relpath.split("/")[0]
    real = tmp_path / "realdir"
    real.mkdir()
    try_symlink(tmp_path / parent, real, directory=True)
    read, data = mn.read_surface(tmp_path, surface)
    assert (read.state, read.reason, data) == ("unreadable", "link", None)


def test_directory_at_file_path_is_unreadable(tmp_path):
    (tmp_path / ".mcp.json").mkdir()
    read, data = mn.read_surface(tmp_path, CC)
    assert (read.state, read.reason, data) == (
        "unreadable",
        "not_regular_file",
        None,
    )


def test_parent_component_as_file_is_unreadable(tmp_path):
    (tmp_path / ".cursor").write_bytes(b"not a dir")
    read, data = mn.read_surface(tmp_path, CURSOR)
    assert (read.state, read.reason, data) == (
        "unreadable",
        "parent_not_directory",
        None,
    )


@pytest.mark.skipif(os.name == "nt", reason="POSIX permission bits")
def test_unreadable_permission_denied(tmp_path):
    if os.geteuid() == 0:
        pytest.skip("root reads through permission bits")
    path = tmp_path / ".mcp.json"
    path.write_bytes(b"{}")
    path.chmod(0o000)
    try:
        try:
            with open(path, "rb"):
                pass
        except OSError:
            pass
        else:
            pytest.skip("permission bits do not block reads here")
        read, data = mn.read_surface(tmp_path, CC)
        assert (read.state, read.reason, data) == (
            "unreadable",
            "permission_denied",
            None,
        )
    finally:
        path.chmod(0o644)


def test_read_surface_oversize_file(tmp_path):
    path = tmp_path / ".mcp.json"
    with open(path, "wb") as handle:
        handle.write(b"y" * (MAX_BYTES + 1))
    read, data = mn.read_surface(tmp_path, CC)
    assert read.state == "malformed"
    assert read.reason == TOO_LARGE
    assert read.raw_sha256 is None
    assert data is None


def test_is_link_predicate_covers_symlink_and_reparse():
    assert mn._is_link(stat.S_IFLNK | 0o777, None)
    assert mn._is_link(stat.S_IFREG | 0o644, 0x400)
    assert mn._is_link(stat.S_IFDIR | 0o755, 0x400 | 0x10)
    assert not mn._is_link(stat.S_IFREG | 0o644, None)
    assert not mn._is_link(stat.S_IFREG | 0o644, 0x80)
    assert not mn._is_link(stat.S_IFDIR | 0o755, None)


# ---------------------------------------------------------------------------
# JSON golden round trips
# ---------------------------------------------------------------------------


def test_json_absent_file_add_uses_fixed_template():
    new = mn.edit_surface_bytes(CC, None, AddEntry("docs", V_HTTP))
    assert new == (
        b'{\n  "mcpServers": {\n    "docs": {\n      "type": "http",\n'
        b'      "url": "https://example.invalid/mcp"\n    }\n  }\n}\n'
    )


def test_json_add_to_empty_container():
    old = b'{\n  "mcpServers": {}\n}\n'
    new = mn.edit_surface_bytes(CC, old, AddEntry("docs", V_SMALL))
    assert new == (
        b'{\n  "mcpServers": {\n    "docs": {\n      "command": "new-cmd"\n    }\n  }\n}\n'
    )
    start = old.index(b"{}") + 1
    assert_p2(old, new, (start, start))


def test_json_add_appends_after_foreign_entries():
    old = fixture("claude_deep.json")
    new = mn.edit_surface_bytes(CC, old, AddEntry("csk-new", V_SMALL))
    assert new == (
        '{\n  "editor": {\n    "fontSize": 14,\n    "nested": {\n'
        '      "deep": [1, 2, {"x": "y"}],\n      "flag": true\n    }\n  },\n'
        '  "mcpServers": {\n    "foreign-before": {\n      "command": "before-cmd",\n'
        '      "args": ["--x", "ünïcodé ✓"]\n    },\n'
        '    "foreign-after": {\n      "command": "after-cmd"\n    },\n'
        '    "csk-new": {\n      "command": "new-cmd"\n    }\n  },\n  "other": true\n}\n'
    ).encode("utf-8")
    anchor = old.index(b'"after-cmd"')
    end_of_value = old.index(b"}", anchor) + 1
    container_close = old.index(b"}", end_of_value)
    assert_p2(old, new, (end_of_value, container_close))


def test_json_add_when_container_absent():
    old = b'{\n  "editor": {"a": 1},\n  "other": true\n}\n'
    new = mn.edit_surface_bytes(CC, old, AddEntry("docs", V_SMALL))
    assert new == (
        b'{\n  "editor": {"a": 1},\n  "other": true,\n  "mcpServers": {\n'
        b'    "docs": {\n      "command": "new-cmd"\n    }\n  }\n}\n'
    )
    start = old.index(b"true") + len(b"true")
    assert_p2(old, new, (start, len(old) - 2))


def test_json_add_when_top_level_empty():
    new = mn.edit_surface_bytes(CC, b"{}", AddEntry("docs", V_SMALL))
    assert new == (
        b'{\n  "mcpServers": {\n    "docs": {\n      "command": "new-cmd"\n    }\n  }\n}'
    )


def test_json_add_tab_indent():
    old = fixture("cursor_tab.json")
    new = mn.edit_surface_bytes(CURSOR, old, AddEntry("csk-new", V_SMALL))
    assert new == (
        b'{\n\t"mcpServers": {\n\t\t"foreign": {\n\t\t\t"command": "tab-cmd",\n'
        b'\t\t\t"args": ["a"]\n\t\t},\n\t\t"csk-new": {\n\t\t\t"command": "new-cmd"\n'
        b"\t\t}\n\t},\n\t\"other\": false\n}\n"
    )
    assert_single_span(old, new)


def test_json_add_four_space_indent():
    old = b'{\n    "mcpServers": {\n        "a": {"command": "x"}\n    }\n}\n'
    new = mn.edit_surface_bytes(CC, old, AddEntry("b", V_SMALL))
    assert new == (
        b'{\n    "mcpServers": {\n        "a": {"command": "x"},\n        "b": {\n'
        b'            "command": "new-cmd"\n        }\n    }\n}\n'
    )


def test_json_crlf_insert_uses_crlf():
    """Kills M5 (CRLF file gets LF on insert) on the JSON surface."""
    old = to_crlf(b'{\n  "mcpServers": {}\n}\n')
    new = mn.edit_surface_bytes(CC, old, AddEntry("docs", V_SMALL))
    assert new == (
        b'{\r\n  "mcpServers": {\r\n    "docs": {\r\n      "command": "new-cmd"\r\n'
        b"    }\r\n  }\r\n}\r\n"
    )
    stripped = new.replace(b"\r\n", b"")
    assert b"\n" in new and b"\r" not in stripped and b"\n" not in stripped


def test_json_add_preserves_missing_final_newline():
    old = b'{\n  "mcpServers": {}}'
    new = mn.edit_surface_bytes(CC, old, AddEntry("docs", V_SMALL))
    assert new == (
        b'{\n  "mcpServers": {\n    "docs": {\n      "command": "new-cmd"\n    }\n  }}'
    )
    assert not new.endswith(b"\n")


def test_json_replace_rewrites_only_value_span():
    old = fixture("claude_deep.json")
    new = mn.edit_surface_bytes(CC, old, ReplaceEntry("foreign-after", V_REPL))
    assert new == (
        '{\n  "editor": {\n    "fontSize": 14,\n    "nested": {\n'
        '      "deep": [1, 2, {"x": "y"}],\n      "flag": true\n    }\n  },\n'
        '  "mcpServers": {\n    "foreign-before": {\n      "command": "before-cmd",\n'
        '      "args": ["--x", "ünïcodé ✓"]\n    },\n'
        '    "foreign-after": {\n      "type": "stdio",\n      "command": "uvx",\n'
        '      "args": [\n        "example-tools==1.2.3"\n      ]\n    }\n  },\n'
        '  "other": true\n}\n'
    ).encode("utf-8")
    key_at = old.index(b'"foreign-after"')
    value_start = old.index(b"{", key_at)
    value_end = old.index(b"\n  },", value_start)
    assert_p2(old, new, (value_start, value_end))


def test_json_remove_first_keeps_sibling_indent():
    old = fixture("claude_deep.json")
    new = mn.edit_surface_bytes(CC, old, RemoveEntry("foreign-before"))
    assert new == (
        '{\n  "editor": {\n    "fontSize": 14,\n    "nested": {\n'
        '      "deep": [1, 2, {"x": "y"}],\n      "flag": true\n    }\n  },\n'
        '  "mcpServers": {\n    "foreign-after": {\n      "command": "after-cmd"\n    }\n'
        '  },\n  "other": true\n}\n'
    ).encode("utf-8")
    assert_p2(old, new, (old.index(b'"foreign-before"'), old.index(b'"foreign-after"')))


def test_json_remove_last_drops_one_comma():
    old = fixture("claude_deep.json")
    new = mn.edit_surface_bytes(CC, old, RemoveEntry("foreign-after"))
    assert b'"foreign-after"' not in new
    assert new.count(b",") == old.count(b",") - 1
    assert mn.parse_surface_bytes(CC, new).entries == {
        "foreign-before": {
            "command": "before-cmd",
            "args": ["--x", "ünïcodé ✓"],
        }
    }


def test_json_remove_middle_and_crlf():
    old = (
        b'{\n  "mcpServers": {\n    "a": {"command": "x"},\n'
        b'    "b": {"command": "y"},\n    "c": {"command": "z"}\n  }\n}\n'
    )
    new = mn.edit_surface_bytes(CC, old, RemoveEntry("b"))
    assert new == (
        b'{\n  "mcpServers": {\n    "a": {"command": "x"},\n'
        b'    "c": {"command": "z"}\n  }\n}\n'
    )
    crlf_old = to_crlf(old)
    crlf_new = mn.edit_surface_bytes(CC, crlf_old, RemoveEntry("a"))
    assert crlf_new == (
        b'{\r\n  "mcpServers": {\r\n    "b": {"command": "y"},\r\n'
        b'    "c": {"command": "z"}\r\n  }\r\n}\r\n'
    )


def test_json_remove_sole_member():
    old = b'{\n  "mcpServers": {\n    "a": {"command": "x"}\n  }\n}\n'
    new = mn.edit_surface_bytes(CC, old, RemoveEntry("a"))
    assert new == b'{\n  "mcpServers": {\n  }\n}\n'
    assert mn.is_effectively_empty(CC, new)


def test_json_remove_sole_single_line_member():
    new = mn.edit_surface_bytes(
        CC, b'{"mcpServers": {"a": {"command": "x"}}}', RemoveEntry("a")
    )
    assert new == b'{"mcpServers": {}}'


def test_json_remove_preserves_missing_final_newline():
    old = b'{\n  "mcpServers": {\n    "a": {"command": "x"}\n  }\n}'
    new = mn.edit_surface_bytes(CC, old, RemoveEntry("a"))
    assert new == b'{\n  "mcpServers": {\n  }\n}'
    assert not new.endswith(b"\n")


@pytest.mark.parametrize(
    "data",
    [
        b'{"mcpServers": {"a": 1 /* c */}}',
        b'{"mcpServers": {"a": 1,}}',
        b'\xef\xbb\xbf{"mcpServers": {}}',
        b'{"mcpServers": {"a": 1, "a": 2}}',
        b'{"mcpServers": []}',
        b'{"mcpServers": {"a": "\\ud800"}}',
    ],
)
@pytest.mark.parametrize(
    "op",
    [AddEntry("n", V_SMALL), ReplaceEntry("a", V_SMALL), RemoveEntry("a")],
)
def test_json_edits_refuse_malformed(data, op):
    with pytest.raises(mn.McpNativeError) as exc_info:
        mn.edit_surface_bytes(CC, data, op)
    assert exc_info.value.code == UNSUP
    assert exc_info.value.surface == ".mcp.json"


def test_json_empty_file_add_refuses():
    with pytest.raises(mn.McpNativeError) as exc_info:
        mn.edit_surface_bytes(CC, b"", AddEntry("docs", V_SMALL))
    assert exc_info.value.code == UNSUP


def test_json_edits_refuse_oversize():
    with pytest.raises(mn.McpNativeError) as exc_info:
        mn.edit_surface_bytes(CC, b"x" * (MAX_BYTES + 1), AddEntry("n", V_SMALL))
    assert exc_info.value.code == TOO_LARGE


# ---------------------------------------------------------------------------
# TOML golden round trips
# ---------------------------------------------------------------------------


def test_toml_absent_file_add_writes_group_only():
    new = mn.edit_surface_bytes(
        CODEX,
        None,
        AddEntry(
            "docs",
            {"url": "https://x/m", "env_http_headers": {"Authorization": "TOK"}},
        ),
    )
    assert new == (
        b"[mcp_servers.docs]\nurl = \"https://x/m\"\n"
        b'[mcp_servers.docs.env_http_headers]\nAuthorization = "TOK"\n'
    )


def test_toml_empty_file_add_writes_group_only():
    new = mn.edit_surface_bytes(CODEX, b"", AddEntry("docs", V_SMALL))
    assert new == b'[mcp_servers.docs]\ncommand = "new-cmd"\n'


def test_toml_add_appends_with_blank_separator():
    old = b"[tool]\nx = 1\n"
    new = mn.edit_surface_bytes(CODEX, old, AddEntry("docs", V_SMALL))
    assert new == b'[tool]\nx = 1\n\n[mcp_servers.docs]\ncommand = "new-cmd"\n'
    assert_p2(old, new, (len(old), len(old)))


def test_toml_add_without_final_newline():
    old = b"[tool]\nx = 1"
    new = mn.edit_surface_bytes(CODEX, old, AddEntry("docs", V_SMALL))
    assert new == b'[tool]\nx = 1\n\n[mcp_servers.docs]\ncommand = "new-cmd"\n'


def test_toml_crlf_insert_uses_crlf():
    """Kills M5 (CRLF file gets LF on insert) on the TOML surface."""
    old = to_crlf(b"[tool]\nx = 1\n")
    new = mn.edit_surface_bytes(CODEX, old, AddEntry("docs", V_SMALL))
    assert new == (
        b"[tool]\r\nx = 1\r\n\r\n[mcp_servers.docs]\r\ncommand = \"new-cmd\"\r\n"
    )


def test_toml_add_preserves_foreign_forms():
    old = fixture("codex_foreign.toml")
    new = mn.edit_surface_bytes(CODEX, old, AddEntry("csk-new", V_SMALL))
    assert new == old + b'\n[mcp_servers.csk-new]\ncommand = "new-cmd"\n'
    assert_p2(old, new, (len(old), len(old)))
    read = mn.parse_surface_bytes(CODEX, new)
    assert set(read.entries) == {"dotted_entry", "quoted.entry", "csk-new"}


def test_toml_replace_substitutes_group_in_place():
    old = fixture("codex_group_comments.toml")
    new = mn.edit_surface_bytes(
        CODEX,
        old,
        ReplaceEntry(
            "docs",
            {"url": "https://new.example/m", "env_http_headers": {"X-Key": "K"}},
        ),
    )
    assert new == (
        b"# leading comment\n[mcp_servers.docs]\nurl = \"https://new.example/m\"\n"
        b"[mcp_servers.docs.env_http_headers]\nX-Key = \"K\"\n"
        b'[mcp_servers.other]\ncommand = "other-cmd"\n'
    )
    start = old.index(b"[mcp_servers.docs]")
    end = old.index(b"[mcp_servers.other]")
    assert_p2(old, new, (start, end))


def test_toml_replace_drops_subtable_when_value_has_none():
    old = fixture("codex_group_comments.toml")
    new = mn.edit_surface_bytes(
        CODEX, old, ReplaceEntry("docs", {"url": "https://new.example/m"})
    )
    assert new == (
        b"# leading comment\n[mcp_servers.docs]\nurl = \"https://new.example/m\"\n"
        b'[mcp_servers.other]\ncommand = "other-cmd"\n'
    )
    assert mn.parse_surface_bytes(CODEX, new).entries["docs"] == {
        "url": "https://new.example/m"
    }


def test_toml_replace_renders_arrays_inline():
    old = fixture("codex_group_comments.toml")
    new = mn.edit_surface_bytes(
        CODEX, old, ReplaceEntry("other", {"command": "n", "args": ["a", "b"]})
    )
    assert new.endswith(b'[mcp_servers.other]\ncommand = "n"\nargs = ["a", "b"]\n')
    assert b"# interior comment one" in new


def test_toml_remove_keeps_comment_lines():
    """Kills M2 (remove also deletes adjacent comment lines)."""
    old = fixture("codex_group_comments.toml")
    new = mn.edit_surface_bytes(CODEX, old, RemoveEntry("docs"))
    assert new == (
        b"# leading comment\n# interior comment one\n# interior comment two\n"
        b"# trailing comment\n[mcp_servers.other]\ncommand = \"other-cmd\"\n"
    )
    assert mn.parse_surface_bytes(CODEX, new).entries == {
        "other": {"command": "other-cmd"}
    }


def test_toml_remove_sole_content_leaves_empty_file():
    new = mn.edit_surface_bytes(
        CODEX, b'[mcp_servers.docs]\nurl = "u"\n', RemoveEntry("docs")
    )
    assert new == b""
    assert mn.is_effectively_empty(CODEX, new)


def test_toml_bracket_in_multiline_string_is_not_a_header():
    old = (
        b'[tool]\nnote = """\n[not-a-header]\n"""\n\n[mcp_servers.docs]\nurl = "u"\n'
    )
    new = mn.edit_surface_bytes(CODEX, old, ReplaceEntry("docs", V_SMALL))
    assert new == (
        b'[tool]\nnote = """\n[not-a-header]\n"""\n\n[mcp_servers.docs]\n'
        b'command = "new-cmd"\n'
    )
    start = old.index(b"[mcp_servers.docs]")
    assert_p2(old, new, (start, len(old)))


def test_toml_bracket_inside_group_multiline_is_not_a_header():
    """Kills M6 (header scanner ignores multi-line strings)."""
    old = b'[mcp_servers.docs]\nurl = "u"\nnote = """\n[other]\n"""\nmore = "x"\n'
    assert mn.parse_surface_bytes(CODEX, old).state == "present"
    new = mn.edit_surface_bytes(CODEX, old, RemoveEntry("docs"))
    assert new == b""


def test_toml_literal_multiline_string_hides_header():
    old = (
        b"[tool]\nnote = '''\n[mcp_servers.docs]\n'''\n\n"
        b'[mcp_servers.docs]\nurl = "u"\n'
    )
    new = mn.edit_surface_bytes(CODEX, old, RemoveEntry("docs"))
    assert new == b"[tool]\nnote = '''\n[mcp_servers.docs]\n'''\n\n"
    assert mn.parse_surface_bytes(CODEX, new).entries == {}


def test_toml_crlf_remove():
    old = to_crlf(
        b"[a]\nx = 1\n\n[mcp_servers.docs]\nurl = \"u\"\n\n[mcp_servers.o]\nurl = \"v\"\n"
    )
    new = mn.edit_surface_bytes(CODEX, old, RemoveEntry("docs"))
    assert new == to_crlf(b"[a]\nx = 1\n\n[mcp_servers.o]\nurl = \"v\"\n")


@pytest.mark.parametrize("crlf", [False, True])
def test_toml_remove_group_at_eof_without_final_newline(crlf):
    old = b"[a]\nx = 1\n\n[mcp_servers.docs]\nurl = \"u\""
    if crlf:
        old = to_crlf(old)
    new = mn.edit_surface_bytes(CODEX, old, RemoveEntry("docs"))
    expected = b"[a]\nx = 1\n\n"
    assert new == (to_crlf(expected) if crlf else expected)
    assert mn.parse_surface_bytes(CODEX, new).entries == {}


def test_toml_replace_group_at_eof_without_final_newline():
    old = b'[mcp_servers.docs]\nurl = "u"'
    new = mn.edit_surface_bytes(CODEX, old, ReplaceEntry("docs", V_SMALL))
    assert new == b'[mcp_servers.docs]\ncommand = "new-cmd"'
    assert not new.endswith(b"\n")


@pytest.mark.parametrize(
    "old",
    [
        b'[mcp_servers]\ndocs = {command = "x"}\n',
        b'[mcp_servers]\ndocs.command = "x"\n',
        b'[mcp_servers."docs"]\ncommand = "x"\n',
        b'["mcp_servers".docs]\ncommand = "x"\n',
        (
            b'[mcp_servers.docs]\nurl = "u"\n[other]\nx = 1\n'
            b'[mcp_servers.docs.extra]\nk = "v"\n'
        ),
    ],
)
@pytest.mark.parametrize("make_op", ["replace", "remove"])
def test_toml_nonstandard_group_replace_remove_refuse(old, make_op):
    read = mn.parse_surface_bytes(CODEX, old)
    assert read.state == "present" and "docs" in read.entries
    op: ReplaceEntry | RemoveEntry
    op = ReplaceEntry("docs", V_SMALL) if make_op == "replace" else RemoveEntry("docs")
    with pytest.raises(mn.McpNativeError) as exc_info:
        mn.edit_surface_bytes(CODEX, old, op)
    assert exc_info.value.code == UNSUP


def test_toml_replace_absorbs_quoted_continuation_subtable():
    old = (
        b'[mcp_servers.docs]\nurl = "u"\n[mcp_servers.docs."extra"]\nk = "v"\n'
    )
    new = mn.edit_surface_bytes(CODEX, old, ReplaceEntry("docs", V_SMALL))
    assert mn.parse_surface_bytes(CODEX, new).entries == {"docs": V_SMALL}


def test_toml_add_onto_inline_container_refuses():
    """Kills M1 (P1 verification disabled): the span edit succeeds, P1 refuses."""
    old = b'mcp_servers = {docs = {command = "x"}}\n'
    assert mn.parse_surface_bytes(CODEX, old).state == "present"
    with pytest.raises(mn.McpNativeError) as exc_info:
        mn.edit_surface_bytes(CODEX, old, AddEntry("other", V_SMALL))
    assert exc_info.value.code == UNSUP


@pytest.mark.parametrize(
    "data",
    [
        b"\xef\xbb\xbf[tool]\nx = 1\n",
        b"[mcp_servers.docs]\nurl = \"u\"\n[mcp_servers.docs]\nurl = \"v\"\n",
        b'mcp_servers = "x"\n',
    ],
)
@pytest.mark.parametrize(
    "op",
    [AddEntry("n", V_SMALL), ReplaceEntry("docs", V_SMALL), RemoveEntry("docs")],
)
def test_toml_edits_refuse_malformed(data, op):
    with pytest.raises(mn.McpNativeError) as exc_info:
        mn.edit_surface_bytes(CODEX, data, op)
    assert exc_info.value.code == UNSUP
    assert exc_info.value.surface == ".codex/config.toml"


# ---------------------------------------------------------------------------
# Preconditions and value validation
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("surface", "data", "op"),
    [
        (CC, b'{"mcpServers": {"a": {"command": "x"}}}', AddEntry("a", V_SMALL)),
        (CC, b'{"mcpServers": {"a": {"command": "x"}}}', ReplaceEntry("b", V_SMALL)),
        (CC, b'{"mcpServers": {"a": {"command": "x"}}}', RemoveEntry("b")),
        (CODEX, b'[mcp_servers.docs]\nurl = "u"\n', AddEntry("docs", V_SMALL)),
        (CODEX, b'[mcp_servers.docs]\nurl = "u"\n', ReplaceEntry("b", V_SMALL)),
        (CODEX, b'[mcp_servers.docs]\nurl = "u"\n', RemoveEntry("b")),
        (CODEX, b'mcp_servers = {docs = {command = "x"}}\n', AddEntry("docs", V_SMALL)),
    ],
)
def test_failed_preconditions_raise_conflict(surface, data, op):
    with pytest.raises(mn.McpNativeError) as exc_info:
        mn.edit_surface_bytes(surface, data, op)
    assert exc_info.value.code == CONFLICT
    assert exc_info.value.surface == surface.relpath


@pytest.mark.parametrize("surface", [CC, CODEX])
@pytest.mark.parametrize("op", [ReplaceEntry("a", V_SMALL), RemoveEntry("a")])
def test_replace_remove_on_absent_file_raise_conflict(surface, op):
    with pytest.raises(mn.McpNativeError) as exc_info:
        mn.edit_surface_bytes(surface, None, op)
    assert exc_info.value.code == CONFLICT


@pytest.mark.parametrize(
    "value",
    [
        {"n": 1},
        {"n": True},
        {"n": None},
        {"n": 1.5},
        {"n": ("a",)},
        {"n": b"x"},
        {"n": ["a", 1]},
        {"n": {"k": 1}},
        {"n": {"k": {"j": "v"}}},
        {"n": [{"k": "v"}]},
        {1: "x"},
        {"n": "ok", 2: "bad"},
        {"bad\ud800key": "x"},
        {"n": "bad\ud800"},
        {"n": ["bad\ud800"]},
        {"n": {"k": "bad\ud800"}},
        "not-a-dict",
        ["a-list"],
    ],
)
@pytest.mark.parametrize("surface", [CC, CODEX])
def test_invalid_values_rejected(surface, value):
    with pytest.raises(mn.McpNativeError) as exc_info:
        mn.edit_surface_bytes(surface, None, AddEntry("docs", value))
    assert exc_info.value.code == INVALID
    assert exc_info.value.surface == surface.relpath


@pytest.mark.parametrize("name", ["", "a.b", "a b", "a/b", "bad\ud800", 123, None])
def test_invalid_names_rejected(name):
    with pytest.raises(mn.McpNativeError) as exc_info:
        mn.edit_surface_bytes(CODEX, None, AddEntry(name, V_SMALL))
    assert exc_info.value.code == INVALID


def test_invalid_op_type_rejected():
    with pytest.raises(mn.McpNativeError) as exc_info:
        mn.edit_surface_bytes(CC, b"{}", "add")  # type: ignore[arg-type]
    assert exc_info.value.code == INVALID


def test_valid_edge_values_accepted():
    value: dict[str, object] = {"a": "", "l": [], "d": {}, "s": "x"}
    new = mn.edit_surface_bytes(CC, None, AddEntry("docs", value))
    assert mn.parse_surface_bytes(CC, new).entries == {"docs": value}
    new_toml = mn.edit_surface_bytes(CODEX, None, AddEntry("docs", value))
    assert mn.parse_surface_bytes(CODEX, new_toml).entries == {"docs": value}


def test_error_messages_carry_no_content():
    secret = "SECRET-TOKEN-ABC"
    data = ('{"mcpServers": {"a": "%s",}}' % secret).encode()
    with pytest.raises(mn.McpNativeError) as exc_info:
        mn.edit_surface_bytes(CC, data, AddEntry("n", V_SMALL))
    assert secret not in str(exc_info.value)


# ---------------------------------------------------------------------------
# is_effectively_empty
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("data", "expected"),
    [
        (b'{"mcpServers": {}}', True),
        (b'{\n  "mcpServers": {\n  }\n}\n', True),
        (b"{}", False),
        (b'{"mcpServers": {"a": 1}}', False),
        (b'{"mcpServers": {}, "x": 1}', False),
        (b"{", False),
        (b"", False),
    ],
)
def test_is_effectively_empty_json(data, expected):
    assert mn.is_effectively_empty(CC, data) is expected


@pytest.mark.parametrize(
    ("data", "expected"),
    [
        (b"", True),
        (b"[mcp_servers]\n", True),
        (b"[mcp_servers]\n", True),
        (b"# only a comment\n", False),
        (b"[mcp_servers]\n# trailing thought\n", False),
        (b"[mcp_servers] # hello\n", False),
        (b"x = 1\n", False),
        (b'[mcp_servers.docs]\nurl = "u"\n', False),
        (b"[unclosed\n", False),
    ],
)
def test_is_effectively_empty_toml(data, expected):
    assert mn.is_effectively_empty(CODEX, data) is expected


def test_is_effectively_empty_oversize_is_false():
    assert mn.is_effectively_empty(CC, b"x" * (MAX_BYTES + 1)) is False


# ---------------------------------------------------------------------------
# P2 instrument check and seeded property tests
# ---------------------------------------------------------------------------


def test_single_span_check_catches_whole_file_rewrite():
    old = fixture("cursor_tab.json")
    rewritten = (json.dumps(json.loads(old), indent=2) + "\n").encode()
    assert rewritten != old
    assert changed_blocks(old, rewritten) > 1
    crlf = to_crlf(fixture("claude_deep.json"))
    assert changed_blocks(crlf, crlf.replace(b"\r\n", b"\n")) > 1


_WORDS = [
    "a",
    "x-y_9",
    'q"q',
    "back\\slash",
    "ünïcodé ✓",
    "",
    "with space",
    "line\nbreak",
    "tab\there",
    "br[ack]et",
    "#hash",
    "https://example.invalid/m?q=1",
]

_VALUE_KEYS = ["command", "url", "type", "args", "env", "headers", "extra", "X-Key"]


def _gen_value(rng: random.Random) -> dict[str, object]:
    value: dict[str, object] = {}
    for key in rng.sample(_VALUE_KEYS, rng.randint(1, 3)):
        roll = rng.random()
        if roll < 0.5:
            value[key] = rng.choice(_WORDS)
        elif roll < 0.75:
            value[key] = [rng.choice(_WORDS) for _ in range(rng.randint(0, 2))]
        else:
            value[key] = {
                f"h{i}": rng.choice(_WORDS) for i in range(rng.randint(0, 2))
            }
    return value


def _gen_json_any(rng: random.Random, depth: int) -> object:
    roll = rng.random()
    if depth < 2 and roll < 0.25:
        return {
            f"k{i}": _gen_json_any(rng, depth + 1) for i in range(rng.randint(0, 2))
        }
    if depth < 2 and roll < 0.4:
        return [_gen_json_any(rng, depth + 1) for _ in range(rng.randint(0, 3))]
    if roll < 0.6:
        return rng.choice(_WORDS)
    if roll < 0.7:
        return rng.randint(-10**6, 10**6)
    if roll < 0.75:
        return round(rng.uniform(-1000.0, 1000.0), 3)
    if roll < 0.85:
        return rng.choice([True, False])
    return None


def _trial_letters(rng: random.Random) -> list[str]:
    letters = [chr(code) for code in range(ord("a"), ord("z") + 1)]
    rng.shuffle(letters)
    return letters


def _gen_json_doc(rng: random.Random, trial: int) -> tuple[bytes, list[str]]:
    letters = _trial_letters(rng)
    doc: dict[str, object] = {}
    for i in range(rng.randint(0, 3)):
        doc[f"foreign-{trial}-{i}"] = _gen_json_any(rng, 0)
    if rng.random() < 0.8:
        count = rng.randint(0, 3)
        doc["mcpServers"] = {
            f"{letters[i]}-srv-{trial}-{i}": _gen_value(rng) for i in range(count)
        }
    items = list(doc.items())
    rng.shuffle(items)
    indent = rng.choice([2, 4, "\t", None])
    if indent is None:
        text = json.dumps(dict(items), ensure_ascii=False)
    else:
        text = json.dumps(dict(items), indent=indent, ensure_ascii=False)
    if rng.random() < 0.85:
        text += "\n"
    data = text.encode("utf-8")
    if rng.random() < 0.3:
        data = data.replace(b"\n", b"\r\n")
    return data, letters


def _toml_str(value: str) -> str:
    assert "\x7f" not in value and "\ud800" not in value
    return json.dumps(value, ensure_ascii=False)


def _toml_key(key: str) -> str:
    if key and all(c.isascii() and (c.isalnum() or c in "_-") for c in key):
        return key
    return _toml_str(key)


def _toml_scalar(value: object) -> str:
    if isinstance(value, str):
        return _toml_str(value)
    assert isinstance(value, list)
    return "[" + ", ".join(_toml_str(item) for item in value) + "]"


def _gen_toml_group_lines(rng: random.Random, name: str) -> tuple[list[str], dict]:
    value = _gen_value(rng)
    lines = [f"[mcp_servers.{name}]"]
    for key, item in value.items():
        if not isinstance(item, dict):
            lines.append(f"{_toml_key(key)} = {_toml_scalar(item)}")
    for key, item in value.items():
        if isinstance(item, dict):
            lines.append(f"[mcp_servers.{name}.{_toml_key(key)}]")
            for sub_key, sub_item in item.items():
                assert isinstance(sub_item, str)
                lines.append(f"{_toml_key(sub_key)} = {_toml_str(sub_item)}")
    return lines, value


def _gen_toml_doc(
    rng: random.Random, trial: int
) -> tuple[bytes, set[str], set[str], list[str]]:
    letters = _trial_letters(rng)
    chunks: list[list[str]] = []
    if rng.random() < 0.6:
        chunks.append([f"# top comment {trial}"])
    for i in range(rng.randint(0, 2)):
        chunks.append([f"topkey{i} = {_toml_str(rng.choice(_WORDS))}"])
    owned = {f"{letters[i]}-srv-{trial}-{i}" for i in range(rng.randint(0, 2))}
    foreign: set[str] = set()
    if rng.random() < 0.5:
        block = ["[mcp_servers]"]
        if rng.random() < 0.5:
            block.append(f"fr_inline_{trial} = {{ command = {_toml_str('x')} }}")
            foreign.add(f"fr_inline_{trial}")
        if rng.random() < 0.5:
            block.append(f"fr_dotted_{trial}.command = {_toml_str('y')}")
            foreign.add(f"fr_dotted_{trial}")
        chunks.append(block)
    if rng.random() < 0.4:
        chunks.append(
            [
                f'[mcp_servers."fr.quoted.{trial}"]',
                f"command = {_toml_str('q')}",
            ]
        )
        foreign.add(f"fr.quoted.{trial}")
    for name in sorted(owned):
        group_lines, _value = _gen_toml_group_lines(rng, name)
        chunks.append(group_lines)
    if rng.random() < 0.6:
        chunks.append(["[tool.foreign]", f"note = {_toml_str('n')}"])
    if rng.random() < 0.4:
        chunks.append(["[[tool.items]]", 'name = "one"'])
    if rng.random() < 0.4:
        chunks.append(["[tool.ml]", 'note = """', "[not-a-header]", '"""'])
    out: list[str] = []
    for index, chunk in enumerate(chunks):
        if index and rng.random() < 0.7:
            out.append("")
        if rng.random() < 0.3:
            out.append(f"# sep {trial}-{index}")
        out.extend(chunk)
    text = "\n".join(out)
    if out and rng.random() < 0.85:
        text += "\n"
    data = text.encode("utf-8")
    if rng.random() < 0.3:
        data = data.replace(b"\n", b"\r\n")
    parsed = tomllib.loads(data.decode("utf-8"))
    entries = parsed.get("mcp_servers", {})
    assert owned <= set(entries)
    assert foreign <= set(entries)
    return data, owned, foreign, letters


def _fresh_op_name(letters: list[str], live_firsts: set[str], trial: int, step: int) -> str:
    """Allocate a name whose first char no live sibling shares (no marker straddle)."""
    for letter in letters:
        if letter not in live_firsts:
            live_firsts.add(letter)
            return f"{letter}-op-{trial}-{step}"
    raise AssertionError("ran out of distinct first letters")


def test_property_json_random_edit_sequences():
    rng = random.Random(20261013)
    for trial in range(60):
        data, letters = _gen_json_doc(rng, trial)
        assert mn.parse_surface_bytes(CC, data).state == "present"
        live = dict(mn.parse_surface_bytes(CC, data).entries)
        live_firsts = {name[0] for name in live}
        for step in range(rng.randint(1, 4)):
            old_doc = json.loads(data.decode("utf-8"))
            expected = copy.deepcopy(old_doc)
            choice = rng.random()
            if choice < 0.4 or not live:
                name = _fresh_op_name(letters, live_firsts, trial, step)
                marker = name.encode()
                assert data.count(marker) == 0
                value = _gen_value(rng)
                new = mn.edit_surface_bytes(CC, data, AddEntry(name, value))
                expected.setdefault("mcpServers", {})[name] = value
                assert json.loads(new.decode("utf-8")) == expected
                assert new.count(marker) == 1
                assert_p2_confinement(
                    data, new, marker, in_prefix=False, in_old_mid=False, in_new_mid=True
                )
                data = new
            elif choice < 0.7:
                name = rng.choice(sorted(live))
                marker = name.encode()
                assert data.count(marker) == 1
                value = _gen_value(rng)
                if value == live[name]:
                    value = dict(value)
                    value["bump"] = f"{trial}-{step}"
                new = mn.edit_surface_bytes(CC, data, ReplaceEntry(name, value))
                expected["mcpServers"][name] = value
                assert json.loads(new.decode("utf-8")) == expected
                assert new.count(marker) == 1
                assert_p2_confinement(
                    data, new, marker, in_prefix=True, in_old_mid=False, in_new_mid=False
                )
                data = new
            elif choice < 0.9:
                name = rng.choice(sorted(live))
                marker = name.encode()
                assert data.count(marker) == 1
                new = mn.edit_surface_bytes(CC, data, RemoveEntry(name))
                del expected["mcpServers"][name]
                assert json.loads(new.decode("utf-8")) == expected
                assert new.count(marker) == 0
                assert_p2_confinement(
                    data, new, marker, in_prefix=False, in_old_mid=True, in_new_mid=False
                )
                data = new
                live_firsts.discard(name[0])
            elif live and rng.random() < 0.5:
                with pytest.raises(mn.McpNativeError) as exc_info:
                    mn.edit_surface_bytes(
                        CC, data, AddEntry(rng.choice(sorted(live)), V_SMALL)
                    )
                assert exc_info.value.code == CONFLICT
            else:
                with pytest.raises(mn.McpNativeError) as exc_info:
                    mn.edit_surface_bytes(
                        CC, data, RemoveEntry(f"missing-{trial}-{step}")
                    )
                assert exc_info.value.code == CONFLICT
            live = dict(mn.parse_surface_bytes(CC, data).entries)


def test_property_toml_random_edit_sequences():
    rng = random.Random(20261014)
    for trial in range(40):
        data, owned, foreign, letters = _gen_toml_doc(rng, trial)
        live_owned = set(owned)
        live_firsts = {name[0] for name in live_owned}
        for step in range(rng.randint(1, 3)):
            old_doc = tomllib.loads(data.decode("utf-8"))
            choice = rng.random()
            if choice < 0.35 or not live_owned:
                name = _fresh_op_name(letters, live_firsts, trial, step)
                marker = f"[mcp_servers.{name}]".encode()
                assert data.count(marker) == 0
                value = _gen_value(rng)
                new = mn.edit_surface_bytes(CODEX, data, AddEntry(name, value))
                _assert_toml_p1(old_doc, new, ("add", name, value))
                assert new.count(marker) == 1
                assert_p2_confinement(
                    data, new, marker, in_prefix=False, in_old_mid=False, in_new_mid=True
                )
                data = new
                live_owned.add(name)
            elif choice < 0.6:
                name = rng.choice(sorted(live_owned))
                marker = f"[mcp_servers.{name}]".encode()
                assert data.count(marker) == 1
                value = _gen_value(rng)
                current = tomllib.loads(data.decode("utf-8"))["mcp_servers"][name]
                if value == current:
                    value = dict(value)
                    value["bump"] = f"{trial}-{step}"
                new = mn.edit_surface_bytes(CODEX, data, ReplaceEntry(name, value))
                _assert_toml_p1(old_doc, new, ("replace", name, value))
                assert new.count(marker) == 1
                assert_p2_confinement(
                    data, new, marker, in_prefix=True, in_old_mid=False, in_new_mid=False
                )
                data = new
            elif choice < 0.8:
                name = rng.choice(sorted(live_owned))
                marker = f"{name}]".encode()
                assert data.count(marker) == 1
                new = mn.edit_surface_bytes(CODEX, data, RemoveEntry(name))
                _assert_toml_p1(old_doc, new, ("remove", name, None))
                assert new.count(marker) == 0
                assert_p2_confinement(
                    data, new, marker, in_prefix=False, in_old_mid=True, in_new_mid=False
                )
                data = new
                live_owned.discard(name)
                live_firsts.discard(name[0])
            elif foreign and rng.random() < 0.5:
                name = rng.choice(sorted(foreign))
                op = (
                    ReplaceEntry(name, V_SMALL)
                    if rng.random() < 0.5
                    else RemoveEntry(name)
                )
                # Bare foreign names refuse at group lookup; dotted names
                # refuse earlier at caller-contract validation.
                expected_code = (
                    UNSUP
                    if re.fullmatch(r"[A-Za-z0-9_-]+", name)
                    else INVALID
                )
                with pytest.raises(mn.McpNativeError) as exc_info:
                    mn.edit_surface_bytes(CODEX, data, op)
                assert exc_info.value.code == expected_code
            elif live_owned:
                with pytest.raises(mn.McpNativeError) as exc_info:
                    mn.edit_surface_bytes(
                        CODEX, data, AddEntry(rng.choice(sorted(live_owned)), V_SMALL)
                    )
                assert exc_info.value.code == CONFLICT
            else:
                with pytest.raises(mn.McpNativeError) as exc_info:
                    mn.edit_surface_bytes(
                        CODEX, data, RemoveEntry(f"missing-{trial}-{step}")
                    )
                assert exc_info.value.code == CONFLICT


def _assert_toml_p1(old_doc: dict, new_bytes: bytes, change: tuple) -> None:
    kind, name, value = change
    expected = copy.deepcopy(old_doc)
    node = expected.setdefault("mcp_servers", {})
    if kind == "add":
        node[name] = value
    elif kind == "replace":
        node[name] = value
    else:
        del node[name]
    if expected.get("mcp_servers") == {}:
        del expected["mcp_servers"]
    new_doc = tomllib.loads(new_bytes.decode("utf-8"))
    if new_doc.get("mcp_servers") == {}:
        del new_doc["mcp_servers"]
    assert new_doc == expected

