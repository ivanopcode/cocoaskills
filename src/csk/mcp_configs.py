from __future__ import annotations

import json
import tomllib
from pathlib import Path


# Resolution of declared MCP server dependencies (dependencies.mcp_servers,
# schema v5) against the configuration surfaces of the target agent
# environments. The check is read-only: csk never provisions MCP servers, it
# verifies that a declared server is already configured where the skill will
# run. Config files that are missing or malformed count as configuring no
# servers; these files belong to third-party tools and their absence is an
# expected state.

# Per agent: config files that may declare MCP servers. Project-relative
# paths resolve against the project root, home-relative against the user
# home directory.
_PROJECT_SOURCES: dict[str, tuple[str, ...]] = {
    "claude_code": (".mcp.json",),
    "cursor": (".cursor/mcp.json",),
    "codex_cli": (),
    "gemini": (),
}

_HOME_SOURCES: dict[str, tuple[str, ...]] = {
    "claude_code": (".claude.json",),
    "cursor": (".cursor/mcp.json",),
    "codex_cli": (".codex/config.toml",),
    "gemini": (".gemini/settings.json",),
}


def known_agents() -> set[str]:
    return set(_PROJECT_SOURCES) | set(_HOME_SOURCES)


def configured_servers(project_root: Path, agent: str, *, home: Path | None = None) -> set[str]:
    """Names of MCP servers configured for one agent, project and user level."""
    user_home = home or Path.home()
    names: set[str] = set()
    for rel in _PROJECT_SOURCES.get(agent, ()):
        names |= _servers_in_file(project_root / rel)
    for rel in _HOME_SOURCES.get(agent, ()):
        names |= _servers_in_file(user_home / rel)
    return names


def resolve_server(
    project_root: Path,
    agents: list[str],
    server_name: str,
    *,
    home: Path | None = None,
) -> dict[str, bool]:
    """Map each target agent to whether it has the server configured.

    Agents without a known MCP configuration surface resolve to False: csk
    cannot verify them, and for 'all' semantics an unverifiable environment
    counts as missing.
    """
    return {
        agent: server_name in configured_servers(project_root, agent, home=home)
        for agent in agents
    }


def _servers_in_file(path: Path) -> set[str]:
    if not path.is_file():
        return set()
    try:
        if path.suffix == ".toml":
            data = tomllib.loads(path.read_text(encoding="utf-8"))
            servers = data.get("mcp_servers", {})
        else:
            loaded = json.loads(path.read_text(encoding="utf-8"))
            servers = loaded.get("mcpServers", {}) if isinstance(loaded, dict) else {}
    except (OSError, ValueError):
        return set()
    if not isinstance(servers, dict):
        return set()
    return {name for name in servers if isinstance(name, str) and name}
