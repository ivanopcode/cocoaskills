"""First-run MCP contracts, including refusal ordering and publication failure."""

import json
import socket
import sys

import pytest

from csk import cli, gitignore_gate, mcp_ownership, transactions
from csk.sources.errors import SourceError, CODE_OUTPUT_OVERLAP
from csk.sources import publish
from child_cli_environment import child_cli_environment
from conftest import write_skillfile
from test_mcp_install_acceptance import _snapshot
from test_mcp_project_install import AGENTS, PATHS, URL, native_bytes, project_cli


TOOLS = {"server": {"transport": "stdio", "command": "uvx", "args": ["tools==1.2.3"]}}


# Check and install share declaration refusals; check never plans native writes or asks for consent.
@pytest.mark.parametrize("value,code", [(URL, None), ("http://example.invalid", "mcp_url_invalid"),
                                        ({"server": {"transport": "stdio", "command": "nice", "args": ["npx", "pkg"]}}, "mcp_command_invalid")])
def test_check_schema3(project_cli, monkeypatch, capsys, value, code):
    root, home, declare = project_cli
    declare({"docs": value})
    before = (root / ".gitignore").read_bytes()
    monkeypatch.setattr(socket, "create_connection", lambda *a, **kw: pytest.fail("check used network"))
    monkeypatch.setattr("builtins.input", lambda *_: pytest.fail("check prompted"))
    monkeypatch.setattr(publish, "install_schema2", lambda **_: pytest.fail("check installed"))
    result = cli.main(["check", "app"])
    output = capsys.readouterr()
    assert result == (1 if code else 0)
    if code:
        assert code in output.err
        assert "valid" not in output.out
    else:
        assert "schema_version 3 valid" in output.out
    assert native_bytes(root) == {}
    assert (root / ".gitignore").read_bytes() == before
    assert not (root / "Skillfile.lock.json").exists()
    assert not list((home / "state/mcp").rglob("*.json"))


# Legacy init refuses unchanged; clean init/install repairs only local MCP excludes.
def test_init_install_mcp_ignores(project_cli, capsys):
    root, home, _ = project_cli
    before = _snapshot(root, home)
    command = ["init", str(root), "--alias", "app", "--agents", ",".join(AGENTS), "--no-interactive"]
    assert cli.main(command) == 2
    assert "skillfile_deprecated_form" in capsys.readouterr().err
    assert _snapshot(root, home) == before
    data = json.loads((root / "Skillfile.json").read_text())
    data.pop("ignore_mode")
    write_skillfile(root, data)
    assert cli.main(["init", str(root), "--alias", "app", "--agents", ",".join(AGENTS), "--no-interactive"]) == 0
    assert "ignore_mode" not in json.loads((root / "Skillfile.json").read_text())
    before = (root / ".gitignore").read_bytes()
    assert cli.main(["install", "app", "--dry-run"]) == 0
    assert (root / ".gitignore").read_bytes() == before
    assert native_bytes(root) == {}
    assert "would: added MCP gitignore entries:" in capsys.readouterr().out
    assert cli.main(["install", "app"]) == 0
    assert (root / ".gitignore").read_bytes() == before
    text = gitignore_gate.ignore_path(root, "exclude").read_text()
    assert text.count("# CocoaSkill project ") == 1
    assert all("/" + path in text.splitlines() for path in PATHS)
    assert "added MCP gitignore entries:" in capsys.readouterr().out
    assert cli.main(["install", "app"]) == 0
    assert (root / ".gitignore").read_bytes() == before
    assert gitignore_gate.ignore_path(root, "exclude").read_text() == text


# A non-MCP ignore failure precedes consent and identifies the real project cause on every row.
@pytest.mark.parametrize("dry_run", [False, True])
def test_preflight_precedes_consent(project_cli, monkeypatch, capsys, dry_run):
    root, home, declare = project_cli
    declare({"docs": URL, "tools": TOOLS})
    (root / ".gitignore").write_text("")
    monkeypatch.setattr("builtins.input", lambda *_: pytest.fail("preflight prompted"))
    assert cli.main(["install", "app", *(["--dry-run"] if dry_run else [])]) == 1
    output = capsys.readouterr()
    assert "gitignore_required" in output.err
    assert "mcp_declaration_invalid" not in output.err
    assert "mcp_plan_unapproved" not in output.err
    assert native_bytes(root) == {}
    assert (root / ".gitignore").read_text() == ""
    assert not list((home / "state/mcp/consent").glob("*.json"))


# A server without authority is blocked by the pending plan, never labelled as lacking its own consent.
def test_mixed_consent_rows(project_cli, capsys):
    _, _, declare = project_cli
    declare({"docs": URL, "tools": TOOLS})
    assert cli.main(["install", "app"]) == 1
    output = capsys.readouterr().err
    assert "mcp docs -> claude_code .mcp.json (blocked by mcp_plan_unapproved)" in output
    assert "mcp tools -> claude_code .mcp.json (refused: mcp_plan_unapproved)" in output


# A transaction failure after approval rolls consent back with configs; a successful retry persists it.
def test_consent_publishes_atomically(project_cli, monkeypatch, capsys):
    root, home, declare = project_cli
    declare({"tools": TOOLS})
    monkeypatch.setattr(sys.stdin, "isatty", lambda: True)
    monkeypatch.setattr(sys.stdout, "isatty", lambda: True)
    prompts = []
    monkeypatch.setattr("builtins.input", lambda prompt: prompts.append(prompt) or "y")
    original = transactions.TransactionEngine._commit_target
    before_ignore = (root / ".gitignore").read_bytes()
    reached = []

    def fail_native(self, journal, target):
        if target.target_class == "mcp-native":
            assert all("/" + path in (root / ".gitignore").read_text().splitlines() for path in PATHS)
            reached.append(target.identifier)
            raise OSError("injected native publication failure")
        return original(self, journal, target)

    with monkeypatch.context() as fault:
        fault.setattr(transactions.TransactionEngine, "_commit_target", fail_native)
        assert cli.main(["install", "app"]) == 1
    assert len(prompts) == 1
    assert reached
    assert (root / ".gitignore").read_bytes() == before_ignore
    assert native_bytes(root) == {}
    assert not list((home / "state/mcp/consent").glob("*.json"))
    assert not (root / "Skillfile.lock.json").exists()
    assert cli.main(["install", "app"]) == 0
    assert len(prompts) == 2
    consent = list((home / "state/mcp/consent").glob("*.json"))
    assert len(consent) == 1
    assert mcp_ownership.load_consent(home, consent[0].stem)
    assert cli.main(["install", "app"]) == 0
    assert len(prompts) == 2


# Missing-lock status names the actual schema, and declaration diagnostics contain one code prefix.
def test_schema3_diagnostics(project_cli, capsys):
    _, _, declare = project_cli
    declare({"bad": {"server": {"transport": "stdio", "command": "/not/bare", "args": []}}})
    assert cli.main(["status", "app", "--check"]) == 1
    output = capsys.readouterr().out
    assert "schema-3 installation has no lock" in output
    assert "schema-2 installation" not in output
    assert "mcp_command_invalid: mcp_command_invalid:" not in output
    assert "mcp_command_invalid:" in output


# A late output preflight blocks before consent; repairing it permits approval and publication.
def test_late_preflight_precedes_consent(project_cli, monkeypatch, capsys):
    root, home, declare = project_cli
    declare({"tools": TOOLS})
    prompts = []
    monkeypatch.setattr(sys.stdin, "isatty", lambda: True)
    monkeypatch.setattr(sys.stdout, "isatty", lambda: True)
    monkeypatch.setattr("builtins.input", lambda prompt: prompts.append(prompt) or "y")

    def refuse_targets(**kwargs):
        raise SourceError(CODE_OUTPUT_OVERLAP, "injected output preflight refusal")

    with monkeypatch.context() as fault:
        fault.setattr(publish, "plan_schema2_targets", refuse_targets)
        assert cli.main(["install", "app"]) == 1
    assert not prompts
    assert CODE_OUTPUT_OVERLAP in capsys.readouterr().err
    assert native_bytes(root) == {}
    assert not list((home / "state/mcp/consent").glob("*.json"))
    assert cli.main(["install", "app"]) == 0
    assert len(prompts) == 1


# Root negations are repaired effectively and once; a later child rule refuses before any publication.
@pytest.mark.parametrize('rules', ['root-negation', 'after-block', 'nested-negation'])
@pytest.mark.parametrize('dry_run', [False, True])
def test_effective_mcp_ignore_policy(project_cli, monkeypatch, capsys, rules, dry_run):
    import subprocess

    root, home, declare = project_cli
    ignore = root / '.gitignore'
    original = ignore.read_text()
    if rules == 'root-negation':
        ignore.write_text(original + '\n/.mcp.json\n!/.mcp.json\n')
    elif rules == 'after-block':
        ignore.write_text(original + '\n# CocoaSkill\n/.mcp.json\n!/.mcp.json\n')
    else:
        declare({'tools': TOOLS})
        monkeypatch.setattr(sys.stdin, 'isatty', lambda: True)
        monkeypatch.setattr(sys.stdout, 'isatty', lambda: True)
        monkeypatch.setattr('builtins.input', lambda *_: pytest.fail('ignore preflight prompted'))
        (root / '.codex').mkdir()
        (root / '.codex/.gitignore').write_text('!config.toml\n')
    before = ignore.read_bytes()
    result = cli.main(['install', 'app', *(['--dry-run'] if dry_run else [])])
    output = capsys.readouterr()
    if rules == 'nested-negation':
        assert result == 1
        assert 'gitignore_required' in output.err
        assert 'does not effectively ignore /.codex/config.toml' in output.err
        assert 'added MCP gitignore entries:' not in output.out
        assert ignore.read_bytes() == before
        assert native_bytes(root) == {}
        assert not (root / 'Skillfile.lock.json').exists()
    else:
        assert result == 0, output
        if dry_run:
            assert ignore.read_bytes() == before
            assert native_bytes(root) == {}
        else:
            for path in PATHS:
                check = subprocess.run(['git', '-C', str(root), 'check-ignore', '-q', path])
                assert check.returncode == 0
            assert ignore.read_text().count('# CocoaSkill') == 1
            after = ignore.read_bytes()
            assert cli.main(['install', 'app']) == 0
            assert ignore.read_bytes() == after
    assert not list((home / 'state/mcp/consent').glob('*.json'))


# Existing unwritable/non-directory parents refuse without asking; fixing them allows consent and repeat.
@pytest.mark.parametrize('failure', [
    pytest.param('readonly', marks=pytest.mark.skipif(sys.platform == 'win32', reason='POSIX permission bits')),
    'not-directory',
])
def test_output_parent_readiness_before_consent(project_cli, monkeypatch, capsys, failure):
    from test_mcp_install_acceptance import _snapshot
    root, home, declare = project_cli
    declare({'tools': TOOLS})
    parent = root / '.codex'
    if failure == 'readonly':
        parent.mkdir()
        parent.chmod(0o500)
    else:
        parent.write_text('user file\n')
    before = (root / '.gitignore').read_bytes()
    snapshot = _snapshot(root, home)
    prompts = []
    monkeypatch.setattr(sys.stdin, 'isatty', lambda: True)
    monkeypatch.setattr(sys.stdout, 'isatty', lambda: True)
    monkeypatch.setattr('builtins.input', lambda prompt: prompts.append(prompt) or 'y')
    try:
        assert cli.main(['install', 'app']) == 1
        error = capsys.readouterr().err
        assert ('source_output_overlap' if failure == 'readonly' else 'generation_unreadable') in error
        assert not prompts
        assert (root / '.gitignore').read_bytes() == before
        assert native_bytes(root) == {}
        assert not (root / 'Skillfile.lock.json').exists()
        assert not list((home / 'state/mcp/consent').glob('*.json'))
        assert _snapshot(root, home) == snapshot
        if failure == 'not-directory':
            assert parent.read_text() == 'user file\n'
    finally:
        if failure == 'readonly':
            parent.chmod(0o700)
        else:
            parent.unlink()
    assert cli.main(['install', 'app']) == 0
    assert len(prompts) == 1
    assert len(list((home / 'state/mcp/consent').glob('*.json'))) == 1
    assert cli.main(['install', 'app']) == 0
    assert len(prompts) == 1


# Real terminal install cannot approve a find -exec runner; every publication surface stays unchanged.
@pytest.mark.skipif(sys.platform == 'win32', reason='requires POSIX PTY')
def test_find_runner_install_refused_in_terminal(project_cli):
    import errno
    import os
    import pty
    import select
    import subprocess
    import time
    from test_mcp_install_acceptance import _snapshot

    root, home, declare = project_cli
    declare({'bad': {'server': {'transport': 'stdio', 'command': 'find',
                               'args': ['.', '-prune', '-exec', 'npx', 'pkg', ';']}}})
    before = _snapshot(root, home)
    master, slave = pty.openpty()
    proc = subprocess.Popen([sys.executable, '-m', 'csk', 'install', 'app'], cwd=root,
                            env=child_cli_environment(), stdin=slave, stdout=slave, stderr=slave)
    os.close(slave)
    output = b''
    try:
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline:
            ready, _, _ = select.select([master], [], [], 0.1)
            if ready:
                try:
                    data = os.read(master, 65536)
                except OSError as exc:
                    if exc.errno == errno.EIO:
                        break
                    raise
                if not data:
                    break
                output += data
            elif proc.poll() is not None:
                break
        result = proc.wait(timeout=3)
    finally:
        if proc.poll() is None:
            proc.terminate()
            proc.wait(timeout=5)
        os.close(master)
    assert result == 1, output.decode(errors='replace')
    assert b'mcp_command_invalid' in output and b"mcp_servers.bad.server.command" in output
    assert b'[y/N]' not in output
    assert _snapshot(root, home) == before
