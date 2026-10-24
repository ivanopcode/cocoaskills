"""HTTP header grammar, native rendering and public consent boundaries."""
from __future__ import annotations

import json
import sys
import tomllib
from dataclasses import replace

import pytest
from test_mcp_install_acceptance import _snapshot
from test_mcp_project_install import install, project_cli  # noqa: F401

from csk import cli, mcp_declarations as mcp

AGENTS = ("claude_code", "codex_cli", "cursor")
URL = "https://example.invalid/mcp"
SECRET = "owned-fixture-secret-never-persisted"


def declaration(headers, *, agent=None, auth=None):
    server = {"transport": "http", "url": URL}
    if agent:
        server["environments"] = [agent]
    extensions = {"headers": headers}
    if auth is not None:
        extensions["auth"] = {"kind": auth}
    return {"server": server, "install": extensions}


def parse(headers, *, agent="claude_code", auth=None):
    return mcp.parse_mcp_servers(
        {"docs": declaration(headers, agent=agent, auth=auth)},
        invocation_scope="project", target_agents=[agent],
    )["docs"]


# Secret-name matching is case-insensitive, including embedded substrings; safe names are paired below.
@pytest.mark.parametrize("name", ["Authorization", "Cookie", "Proxy-Authorization"] + [
    variant for word in ("token", "key", "secret", "auth")
    for variant in (word, word.upper(), "X-" + word.title() + "-Scope", "prefix" + word + "suffix")
])
def test_literal_secret_names_refuse(name):
    with pytest.raises(mcp.McpDeclarationError) as caught:
        parse({name: {"value": "issues,merge_requests"}})
    assert caught.value.code == "mcp_declaration_invalid"
    assert "issues,merge_requests" not in str(caught.value)


# Values cannot interpolate, inject controls or exceed 1024 UTF-8 bytes, and scanner signatures still refuse.
@pytest.mark.parametrize("value", [
    "$", "${HEADER_TOKEN}", "a\nb", "a\rb", "a\tb", "a\x00b", "a\x7fb", "a\x85b", "\u200b", "\ud800",
    "a" * 1025, "é" * 513, "ghp_0123456789abcdefghijklmnop", "API_TOKEN=fixture-value",
    "Authorization: Bearer fixture-value", "0123456789abcdefghijklmnopqrstuvwxyz" * 2,
])
def test_literal_unsafe_values_refuse(value):
    with pytest.raises(mcp.McpDeclarationError) as caught:
        parse({"X-Scope": {"value": value}})
    assert caught.value.code == "mcp_declaration_invalid"
    assert value not in str(caught.value)


# Literal objects are value-only, even when a competing field is null or prefix is explicitly none.
@pytest.mark.parametrize("body", [
    {"value": "scope", "env": "HEADER_TOKEN"}, {"value": "scope", "env": None},
    {"value": "scope", "prefix": "none"}, {"value": "scope", "other": "x"},
    {"value": None}, {"value": 3}, {"value": []}, {},
])
def test_literal_shape_refuses(body):
    with pytest.raises(mcp.McpDeclarationError) as caught:
        parse({"X-Scope": body})
    assert caught.value.code == "mcp_declaration_invalid"


def expected_entry(agent, name, prefix):
    if agent == "codex_cli":
        field = "bearer_token_env_var" if prefix == "bearer" else "env_http_headers"
        return {"url": URL, field: "HEADER_TOKEN" if prefix == "bearer" else {name: "HEADER_TOKEN"}}
    reference = "${HEADER_TOKEN}" if agent == "claude_code" else "${env:HEADER_TOKEN}"
    fixed = {"none": "", "bearer": "Bearer ", "api-token": "Api-Token ", "token": "Token "}[prefix]
    entry = {"url": URL, "headers": {name: fixed + reference}}
    if agent == "claude_code":
        entry["type"] = "http"
    return entry


# This independent prefix/name/agent table preserves bearer placement and distinguishes unsupported native prefixes.
@pytest.mark.parametrize("prefix", ["none", "bearer", "api-token", "token"])
@pytest.mark.parametrize("agent", AGENTS)
@pytest.mark.parametrize("name", ["Authorization", "authorization", "X-Scope", "X-Api-Key", "Cookie", "Proxy-Authorization"])
def test_prefix_agent_name_table(prefix, agent, name):
    invalid = name.lower() in {"cookie", "proxy-authorization"} or (prefix == "bearer" and name.lower() != "authorization")
    unsupported = agent == "codex_cli" and prefix in {"api-token", "token"}
    if invalid or unsupported:
        with pytest.raises(mcp.McpDeclarationError) as caught:
            parse({name: {"env": "HEADER_TOKEN", "prefix": prefix}}, agent=agent)
        assert caught.value.code == ("mcp_declaration_invalid" if invalid else "mcp_header_prefix_unsupported")
    else:
        decl = parse({name: {"env": "HEADER_TOKEN", "prefix": prefix}}, agent=agent)
        assert mcp.render_native_entry(agent, decl) == expected_entry(agent, name, prefix)


# Safe literal values, including the exact UTF-8 byte boundary, route no secret in every native renderer.
@pytest.mark.parametrize("agent", AGENTS)
@pytest.mark.parametrize("value", ["", "issues,merge_requests", "a" * 1024, "é" * 512])
def test_literal_positive_native_controls(agent, value):
    decl = parse({"X-Scope": {"value": value}}, agent=agent)
    assert decl.auth.kind == "none"
    assert not mcp.authority(decl).consent_required
    field = "http_headers" if agent == "codex_cli" else "headers"
    assert mcp.render_native_entry(agent, decl)[field] == {"X-Scope": value}


def native_entry(root, agent):
    if agent == "codex_cli":
        return tomllib.loads((root / ".codex/config.toml").read_text())["mcp_servers"]["docs"]
    path = ".mcp.json" if agent == "claude_code" else ".cursor/mcp.json"
    return json.loads((root / path).read_bytes())["mcpServers"]["docs"]


def approve(monkeypatch, prompts):
    monkeypatch.setattr(sys.stdin, "isatty", lambda: True)
    monkeypatch.setattr(sys.stdout, "isatty", lambda: True)
    monkeypatch.setattr("builtins.input", lambda prompt: prompts.append(prompt) or "yes")


def captured(capsys):
    streams = capsys.readouterr()
    return streams.out + streams.err


def assert_no_secret(root, home, output):
    assert SECRET not in output
    for folder in (root, home):
        for path in folder.rglob("*"):
            if path.is_file() and not path.is_symlink():
                assert SECRET.encode() not in path.read_bytes(), path


# Public check and dry-run are read-only; supported prefixes write exact native references after consent, never env bytes.
@pytest.mark.parametrize("agent", AGENTS)
@pytest.mark.parametrize("prefix", ["none", "bearer", "api-token", "token"])
def test_prefix_public_writers(project_cli, capsys, monkeypatch, agent, prefix):
    root, home, declare = project_cli
    declare({"docs": declaration({"Authorization": {"env": "HEADER_TOKEN", "prefix": prefix}}, agent=agent)})
    monkeypatch.setenv("HEADER_TOKEN", SECRET)
    prompts = []
    approve(monkeypatch, prompts)
    before = _snapshot(root, home)
    unsupported = agent == "codex_cli" and prefix in {"api-token", "token"}
    for argv in (["check", "app"], ["install", "app", "--fix-gitignore", "--dry-run"]):
        result = cli.main(argv)
        output = captured(capsys)
        assert (result != 0) is unsupported, output
        if unsupported:
            assert "mcp_header_prefix_unsupported" in output
        assert _snapshot(root, home) == before
    result = install()
    output = captured(capsys)
    if unsupported:
        assert result != 0 and "mcp_header_prefix_unsupported" in output
        assert _snapshot(root, home) == before and not prompts
    else:
        assert result == 0, output
        assert len(prompts) == 1
        assert native_entry(root, agent) == expected_entry(agent, "Authorization", prefix)
    assert_no_secret(root, home, output)


# OAuth may combine both forms; only actual env references prompt, and an unapproved env install leaves all bytes unchanged.
@pytest.mark.parametrize("agent", AGENTS)
@pytest.mark.parametrize("oauth", [False, True])
@pytest.mark.parametrize("with_env", [False, True])
def test_oauth_and_literal_public_consent(project_cli, capsys, monkeypatch, agent, oauth, with_env):
    root, home, declare = project_cli
    headers = {"X-Scope": {"value": "issues,merge_requests"}}
    if with_env:
        headers["Authorization"] = {"env": "HEADER_TOKEN"}
    declare({"docs": declaration(headers, agent=agent, auth="oauth" if oauth else None)})
    monkeypatch.setenv("HEADER_TOKEN", SECRET)
    monkeypatch.setattr(sys.stdin, "isatty", lambda: False)
    before = _snapshot(root, home)
    assert cli.main(["check", "app"]) == 0
    assert _snapshot(root, home) == before
    prompts = []
    result = install()
    output = captured(capsys)
    if with_env:
        assert result != 0 and "mcp_plan_unapproved" in output
        assert _snapshot(root, home) == before
        approve(monkeypatch, prompts)
        assert install() == 0, captured(capsys)
        assert len(prompts) == 1
    else:
        assert result == 0, output
        assert not list((home / "state/mcp/consent").glob("*.json"))
    field = "http_headers" if agent == "codex_cli" else "headers"
    assert native_entry(root, agent)[field]["X-Scope"] == "issues,merge_requests"
    assert_no_secret(root, home, captured(capsys))


# All public commands reject unsafe literals before prompting or writing; nearby valid controls exercise publication.
@pytest.mark.parametrize("agent", AGENTS)
@pytest.mark.parametrize("headers", [
    {"Authorization": {"value": "fixture-value"}},
    {"X-Scope": {"value": "ghp_0123456789abcdefghijklmnop"}},
    {"X-Scope": {"value": "${HEADER_TOKEN}"}},
    {"X-Scope": {"value": "a\nb"}},
    {"X-Scope": {"value": "é" * 513}},
    {"X-Scope": {"value": "scope", "prefix": "none"}},
])
def test_literal_public_refusals(project_cli, capsys, monkeypatch, agent, headers):
    root, home, declare = project_cli
    declare({"docs": declaration(headers, agent=agent)})
    prompts = []
    approve(monkeypatch, prompts)
    before = _snapshot(root, home)
    for argv in (["check", "app"], ["install", "app", "--dry-run"], ["install", "app"]):
        assert cli.main(argv) != 0
        assert "mcp_declaration_invalid" in captured(capsys)
        assert _snapshot(root, home) == before
    assert not prompts


# A mixed target selection refuses atomically, and direct rendering cannot bypass the Codex capability check.
@pytest.mark.parametrize("prefix", ["api-token", "token"])
def test_unsupported_prefix_has_no_render_bypass(prefix):
    headers = {"Authorization": {"env": "HEADER_TOKEN", "prefix": prefix}}
    with pytest.raises(mcp.McpDeclarationError) as caught:
        mcp.parse_mcp_servers({"docs": declaration(headers)}, invocation_scope="project", target_agents=AGENTS)
    assert caught.value.code == "mcp_header_prefix_unsupported"
    decl = parse(headers)
    with pytest.raises(mcp.McpDeclarationError) as caught:
        mcp.render_native_entry("codex_cli", replace(decl, agents=("codex_cli",)))
    assert caught.value.code == "mcp_header_prefix_unsupported"


# Existing none/bearer consent digests remain byte-compatible with pre-extension declarations.
@pytest.mark.parametrize("prefix,digest", [
    ("none", "ac222a69d76752e7e95fb43f8dd44792d9fdea6bfb6a3bf98a59c7ba40123e0a"),
    ("bearer", "3b196640c9c73e82ee25ff1403ffd53c31ee170e7f123f220b91320613c72132"),
])
def test_existing_env_declaration_digest_unchanged(prefix, digest):
    raw = declaration({"Authorization": {"env": "DOCS_TOKEN", "prefix": prefix}})
    decl = mcp.parse_mcp_servers({"docs": raw}, invocation_scope="project", target_agents=AGENTS)["docs"]
    assert mcp.declaration_digest(decl) == digest


# Literal changes affect ownership identity, while map ordering does not.
def test_literal_digest_changes_and_order():
    first = parse({"X-A": {"value": "a"}, "X-B": {"value": "b"}})
    same = parse({"X-B": {"value": "b"}, "X-A": {"value": "a"}})
    other = parse({"X-A": {"value": "changed"}, "X-B": {"value": "b"}})
    assert mcp.declaration_digest(first) == mcp.declaration_digest(same)
    assert mcp.declaration_digest(first) != mcp.declaration_digest(other)
