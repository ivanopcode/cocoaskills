"""Executable examples from the project MCP documentation."""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest
from test_mcp_install_acceptance import SURFACES, _case, _expected_rows, _rows

from csk import manifest, mcp_declarations


ROOT = Path(__file__).resolve().parents[1]
FIXTURE = ROOT / "tests/fixtures/mcp/docs-example.Skillfile.json"
AUTHORING = ROOT / "docs/skill-authoring.md"


def _documented_payloads(document: str) -> list[dict]:
    section = document.split('<a id="project-mcp-schema-3"></a>', 1)[1]
    match = re.search(r"^### [^\n]+\n(.*?)(?=^### |\Z)", section, re.M | re.S)
    assert match is not None, "The project MCP section must exist"
    blocks = re.findall(r"^```json\s*\n(.*?)^```\s*$", match[1], re.M | re.S)
    assert blocks, "The project MCP section must contain JSON examples"
    payloads = [json.loads(block) for block in blocks]
    assert all(isinstance(payload, dict) for payload in payloads)
    return payloads


def _parse_example(payload: dict) -> None:
    if "schema_version" not in payload:
        # The object-form example explicitly documents the value of mcp_servers.
        payload = {
            "schema_version": 3,
            "agents": [agent for agent, _ in SURFACES],
            "skills": [],
            "mcp_servers": payload,
        }
    parsed = manifest.parse_manifest(payload, Path("Skillfile.json"))
    assert parsed.schema_version == 3
    declarations = mcp_declarations.parse_mcp_servers(
        parsed.mcp_servers, invocation_scope="project", target_agents=parsed.agents,
    )
    assert declarations, "Each documented MCP example must declare servers"


def test_docs_fixture_installs_three_configs_and_repeats_unchanged(tmp_path: Path) -> None:
    # The exact published fixture installs headlessly, then preserves native bytes on reinstall.
    case = _case(tmp_path / "docs", schema=1)
    (case.project / "Skillfile.json").write_bytes(FIXTURE.read_bytes())
    first = case.run()
    assert first.returncode == 0, first.stdout + first.stderr
    assert _rows(first) == _expected_rows("added")
    before = {path: (case.project / path).read_bytes() for _, path in SURFACES}
    second = case.run()
    assert second.returncode == 0, second.stdout + second.stderr
    assert _rows(second) == _expected_rows("unchanged")
    assert {path: (case.project / path).read_bytes() for _, path in SURFACES} == before


def test_every_project_mcp_json_example_parses_as_schema_3() -> None:
    # Every fenced JSON example in the MCP section passes both manifest and MCP validation.
    payloads = _documented_payloads(AUTHORING.read_text(encoding="utf-8"))
    full_examples = [payload for payload in payloads if "schema_version" in payload]
    assert json.loads(FIXTURE.read_bytes()) in full_examples
    for payload in payloads:
        _parse_example(payload)


def test_docs_checker_rejects_a_planted_invalid_mcp_example() -> None:
    # A bad HTTP URL planted in the actual section must fail MCP validation, not extraction/setup.
    document = AUTHORING.read_text(encoding="utf-8")
    valid_url = "https://mcp.example.com/mcp"
    assert valid_url in document
    planted = document.replace(valid_url, "http://mcp.example.com/mcp", 1)
    example = next(
        payload for payload in _documented_payloads(planted) if "schema_version" in payload
    )
    with pytest.raises(mcp_declarations.McpDeclarationError) as exc:
        _parse_example(example)
    assert exc.value.code == "mcp_url_invalid"
