"""Launcher consent disclosure through a real strict legacy text encoder."""
from __future__ import annotations

import builtins
import io
import json
from dataclasses import asdict, replace
from types import SimpleNamespace

import pytest

from csk import mcp_declarations, mcp_launchers, mcp_project


class ReviewOutput(io.TextIOWrapper):
    def isatty(self):
        return True


# Disclosure is reversible on legacy redirected output, including Unicode paths
# and publisher names. Both consumer consent groups reach their actual review
# branch; noninteractive review refuses before asking, without changing the plan.
# This is the pure review boundary, not native verification or publication.
@pytest.mark.parametrize("consumer", ["claude_code", "codex_cli"])
@pytest.mark.parametrize("interactive", [False, True], ids=["refuse", "approve"])
@pytest.mark.parametrize("path,publisher", [
    (r"C:\Apps\ordinary\server.exe", "Example Inc"),
    (r"C:\Apps\приложение-📦\server.exe", "Example Inc"),
    (r"C:\Apps\ordinary\server.exe", "Разработчик 📦"),
], ids=["ascii", "unicode-path", "unicode-publisher"])
def test_launcher_review_legacy_encoding(monkeypatch, consumer, interactive, path, publisher):
    raw = {"app": {"server": {"transport": "stdio", "launcher": {"linux": {
        "path": "~/.local/bin/server", "trust": "allow-unverified"}}}}}
    parsed = mcp_declarations.parse_mcp_servers(raw, invocation_scope="project", target_agents=[consumer])
    decl = parsed["app"]
    observation = mcp_launchers.ResolvedLauncher(path, "unverified", publisher, "a" * 64)
    parsed["app"] = replace(decl, server=replace(decl.server, resolved_launcher=observation))
    before = asdict(parsed["app"])
    plan = SimpleNamespace(declarations=parsed, pending=[SimpleNamespace(agents=(consumer,), scope="project")])
    buffer = io.BytesIO()
    output = ReviewOutput(buffer, encoding="cp1252", errors="strict", write_through=True)
    assert output.encoding == "cp1252" and output.errors == "strict"
    prompts = []

    def display(*args, **kwargs):
        builtins.print(*args, file=output, **kwargs)

    def answer(prompt):
        output.write(prompt)
        prompts.append(prompt)
        return "yes"

    monkeypatch.setattr(mcp_project, "print", display, raising=False)
    monkeypatch.setattr(mcp_project, "input", answer, raising=False)
    monkeypatch.setattr(mcp_project, "sys", SimpleNamespace(stdin=output, stdout=output))
    if interactive:
        mcp_project.review_consent(plan, interactive=True, dry_run=False, project="app")
        assert len(prompts) == 1 and "Approve 1 MCP server(s)" in prompts[0]
    else:
        with pytest.raises(mcp_project.McpProjectError, match="mcp_plan_unapproved"):
            mcp_project.review_consent(plan, interactive=False, dry_run=False, project="app")
        assert not prompts
    text = buffer.getvalue().decode("cp1252")
    assert text.isascii()
    lines = text.splitlines()
    command = lines[0].split("stdio ", 1)[1].split("; agents=", 1)[0]
    assert json.loads(command) == [path]
    identity, displayed_path = lines[1].split("; publisher=", 1)[1].split("; path=", 1)
    assert json.loads(identity) == publisher and json.loads(displayed_path) == path
    assert asdict(parsed["app"]) == before
