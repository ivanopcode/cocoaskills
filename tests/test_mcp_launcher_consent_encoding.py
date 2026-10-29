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


# The Claude initial install that precedes the dry-run lifecycle must actually
# disclose/approve/publish Unicode identity through legacy child stdout. This
# single publication complements the separate full Codex permission lifecycle;
# no unselected native surface, pending journal or helper execution is allowed.
def test_unicode_launcher_legacy_cli_publication(tmp_path):
    from test_mcp_install_acceptance import SURFACES
    from test_mcp_launcher_path_encoding import NAMES, invoke, lifecycle_fixture, unselected_state

    case, root, marker = lifecycle_fixture(tmp_path, "claude_code")
    case.env["PYTHONIOENCODING"] = "cp1252:strict"
    neighbors = unselected_state(case, "claude_code")
    code, output, _ = invoke(case, root, marker, NAMES[3], approve=True)
    assert code == 0 and b"mcp app -> claude_code .mcp.json (added)" in output, output
    assert b"Approve 1 MCP server(s)" in output
    assert b"project_preflight_failed" not in output
    command = str(root.parent / NAMES[3].decode("utf-8")) + "/App/bin/server.exe"
    assert json.loads((case.project / ".mcp.json").read_bytes())["mcpServers"]["app"]["command"] == command
    assert all((case.project / path).exists() == (agent == "claude_code") for agent, path in SURFACES)
    seed = (case.project / "source/seed/SKILL.md").read_bytes()
    assert (case.project / ".agents/skills/seed/SKILL.md").read_bytes() == seed
    assert (case.project / ".claude/skills/seed/SKILL.md").read_bytes() == seed
    assert (case.project / "Skillfile.lock.json").is_file()
    grants = list((case.home / "state/mcp/consent").glob("*.json"))
    assert len(grants) == 1 and len(json.loads(grants[0].read_bytes())["approved"]) == 1
    assert not list((case.home / "state/transactions").rglob("*.json"))
    assert unselected_state(case, "claude_code") == neighbors
    assert not marker.exists()
