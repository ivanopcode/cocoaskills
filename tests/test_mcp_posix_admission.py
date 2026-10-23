"""Current POSIX base metadata takes precedence over the portable snapshot."""
import os

import pytest

from csk import cli, mcp_command_resolution as resolution, mcp_declarations as mcp
from test_mcp_command_resolution import executable, parse, stdio
from test_mcp_install_acceptance import _snapshot
from test_mcp_project_install import project_cli

pytestmark = [pytest.mark.usefixtures("stable_env"), pytest.mark.skipif(os.name == "nt", reason="POSIX base-directory inode contract")]

# An unlisted base command is rejected by host admission, with an opaque
# non-base control admitted. No finite portable inventory is assumed complete.
def test_uninventoried_current_base_name(tmp_path, monkeypatch):
    base = tmp_path / "base"; base.mkdir()
    own = tmp_path / "own"; own.mkdir()
    executable(base / "new-host-tool")
    executable(own / "new-host-tool")
    parsed = parse("new-host-tool", ["--port", "1"])
    monkeypatch.setattr(resolution, "_BASE_DIRECTORIES", (str(base),))
    monkeypatch.setenv("PATH", str(base) + os.pathsep + str(own))
    with pytest.raises(mcp.McpDeclarationError, match="mcp_command_invalid"):
        resolution.validate_host_commands(parsed, cwd=tmp_path)
    monkeypatch.setenv("PATH", str(own) + os.pathsep + str(base))
    resolution.validate_host_commands(parsed, cwd=tmp_path)


# The same host observation runs for check, dry-run and actual install on every
# POSIX host. Refusal precedes consent and preserves all project/home bytes.
@pytest.mark.parametrize("args", [("check", "app"), ("install", "app", "--dry-run"), ("install", "app")])
def test_uninventoried_current_base_cli(project_cli, tmp_path, monkeypatch, capsys, args):
    root, home, declare = project_cli
    base = tmp_path / "base"; base.mkdir()
    executable(base / "new-host-tool")
    monkeypatch.setattr(resolution, "_BASE_DIRECTORIES", (str(base),))
    monkeypatch.setenv("PATH", str(base))
    declare(stdio("new-host-tool", ["--port", "1"]))
    before = _snapshot(root, home)
    monkeypatch.setattr("builtins.input", lambda _: pytest.fail("must refuse before consent"))
    assert cli.main(list(args)) == 1
    output = capsys.readouterr()
    assert "mcp_command_invalid" in output.out + output.err
    assert _snapshot(root, home) == before
