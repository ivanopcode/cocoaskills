from __future__ import annotations

import json
import shutil
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
    "codex_cli": (".codex/config.toml",),
    "gemini": (".gemini/settings.json",),
    "windsurf": (),
    "opencode": ("opencode.json", "opencode.jsonc"),
}

_HOME_SOURCES: dict[str, tuple[str, ...]] = {
    "claude_code": (".claude.json",),
    "cursor": (".cursor/mcp.json",),
    "codex_cli": (".codex/config.toml",),
    "gemini": (".gemini/settings.json",),
    "windsurf": (".codeium/windsurf/mcp_config.json",),
    "opencode": (".config/opencode/opencode.json", ".config/opencode/opencode.jsonc"),
}

# OpenCode declares MCP servers under "mcp" instead of "mcpServers", and an
# entry can be present but switched off with "enabled": false.
_OPENCODE_FILES = {"opencode.json", "opencode.jsonc"}

# Claude Code project settings can reject servers declared in .mcp.json; a
# rejected server never activates, so it counts as not configured.
_CLAUDE_SETTINGS = (".claude/settings.json", ".claude/settings.local.json")


def known_agents() -> set[str]:
    return set(_PROJECT_SOURCES) | set(_HOME_SOURCES)


def configured_servers(project_root: Path, agent: str, *, home: Path | None = None) -> set[str]:
    """Names of MCP servers configured for one agent, project and user level."""
    return set(_project_entries(project_root, agent)) | set(_home_entries(agent, home=home))


def project_only_servers(project_root: Path, agent: str, *, home: Path | None = None) -> set[str]:
    """Names configured only through project-level surfaces.

    Agents gate project-level configs behind checkout trust, so a server that
    resolves only here may sit pending in a fresh clone.
    """
    return set(_project_entries(project_root, agent)) - set(_home_entries(agent, home=home))


def missing_stdio_commands(
    project_root: Path,
    agents: list[str],
    server_name: str,
    *,
    home: Path | None = None,
) -> dict[str, str]:
    """Agents whose entries for one configured server all fail the PATH probe.

    Only warns when every entry for the server is positively a stdio server
    whose command does not resolve; a remote entry or an unrecognized shape
    counts as potentially reachable. The probe is static: csk never launches
    a server. Maps agent to the unresolved command.
    """
    missing: dict[str, str] = {}
    for agent in agents:
        entries = [
            entry
            for source in (_project_entries(project_root, agent), _home_entries(agent, home=home))
            for name, entry in source.items()
            if name == server_name
        ]
        if not entries:
            continue
        commands = [_stdio_command(entry) for entry in entries]
        if any(command is None for command in commands):
            continue
        unresolved = [command for command in commands if command and shutil.which(command) is None]
        if len(unresolved) == len(commands):
            missing[agent] = unresolved[0]
    return missing


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


def _project_entries(project_root: Path, agent: str) -> dict[str, object]:
    entries: dict[str, object] = {}
    for rel in _PROJECT_SOURCES.get(agent, ()):
        entries.update(_entries_in_file(project_root / rel))
    if agent == "claude_code" and entries:
        for name in _claude_disabled_servers(project_root):
            entries.pop(name, None)
    return entries


def _home_entries(agent: str, *, home: Path | None = None) -> dict[str, object]:
    user_home = home or Path.home()
    entries: dict[str, object] = {}
    for rel in _HOME_SOURCES.get(agent, ()):
        entries.update(_entries_in_file(user_home / rel))
    return entries


def _entries_in_file(path: Path) -> dict[str, object]:
    if not path.is_file():
        return {}
    try:
        if path.suffix == ".toml":
            data = tomllib.loads(path.read_text(encoding="utf-8"))
            servers = data.get("mcp_servers", {})
        elif path.name in _OPENCODE_FILES:
            loaded = json.loads(path.read_text(encoding="utf-8"))
            servers = loaded.get("mcp", {}) if isinstance(loaded, dict) else {}
            if isinstance(servers, dict):
                servers = {
                    name: entry
                    for name, entry in servers.items()
                    if not (isinstance(entry, dict) and entry.get("enabled") is False)
                }
        else:
            loaded = json.loads(path.read_text(encoding="utf-8"))
            servers = loaded.get("mcpServers", {}) if isinstance(loaded, dict) else {}
    except (OSError, ValueError):
        return {}
    if not isinstance(servers, dict):
        return {}
    return {name: entry for name, entry in servers.items() if isinstance(name, str) and name}


def _claude_disabled_servers(project_root: Path) -> set[str]:
    disabled: set[str] = set()
    for rel in _CLAUDE_SETTINGS:
        path = project_root / rel
        if not path.is_file():
            continue
        try:
            loaded = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if not isinstance(loaded, dict):
            continue
        names = loaded.get("disabledMcpjsonServers", [])
        if isinstance(names, list):
            disabled |= {name for name in names if isinstance(name, str) and name}
    return disabled


def _stdio_command(entry: object) -> str | None:
    """Executable of a stdio server entry, None when the entry may be remote."""
    if not isinstance(entry, dict):
        return None
    command = entry.get("command")
    if isinstance(command, str) and command:
        return command
    # OpenCode local servers declare the command as an argv list.
    if isinstance(command, list) and command and isinstance(command[0], str) and command[0]:
        return str(command[0])
    return None
