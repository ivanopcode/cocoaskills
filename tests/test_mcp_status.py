"""Read-only project MCP currentness through the real status CLI."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest
from conftest import write_skillfile
from test_mcp_install_acceptance import ENV_HEADER, SURFACES, URL, _case, _snapshot


def _status(case, *options, globally=False):
    command = ['global', 'status'] if globally else ['status', 'app']
    return subprocess.run(
        [sys.executable, '-m', 'csk', *command, *options],
        cwd=case.project, env=case.env, stdin=subprocess.DEVNULL,
        capture_output=True, text=True, timeout=60,
    )


def _install(case):
    result = case.run('--fix-gitignore')
    assert result.returncode == 0, result.stdout + result.stderr


def _expected(states, name='docs'):
    return [
        {'server': name, 'agent': agent, 'path': path, 'state': state}
        for (agent, path), state in zip(SURFACES, states, strict=True)
    ]


def _rows(result):
    return [line for line in result.stdout.splitlines() if line.startswith('mcp ') and ' -> ' in line]


def _assert_status(case, expected, *, skills_current=True):
    before = _snapshot(case.project, case.home, Path(case.env['HOME']))
    result = _status(case)
    assert result.returncode == 0, result.stdout + result.stderr
    assert _rows(result) == [
        f"mcp {row['server']} -> {row['agent']} {row['path']} ({row['state']})"
        for row in expected
    ]
    lines = result.stdout.splitlines()
    note = 'MCP authentication and connectivity: not checked.'
    assert lines.count(note) == 1
    assert not expected or lines.index(note) > max(lines.index(row) for row in _rows(result))
    expected_exit = 0 if all(row['state'] == 'current' for row in expected) else 1
    text_checked = _status(case, '--check')
    assert text_checked.returncode == expected_exit, text_checked.stdout + text_checked.stderr
    assert _rows(text_checked) == _rows(result)
    checked = _status(case, '--check', '--json')
    assert checked.returncode == expected_exit, checked.stdout + checked.stderr
    payload = json.loads(checked.stdout)
    assert payload[0]['mcp'] == expected
    if skills_current:
        assert payload[0]['errors'] == []
        assert all(skill['label'] == 'up-to-date' for skill in payload[0]['skills'])
    assert payload[0]['clean'] == all(row['state'] == 'current' for row in expected)
    assert _snapshot(case.project, case.home, Path(case.env['HOME'])) == before


@pytest.mark.parametrize('state', ['current', 'missing', 'drift', 'conflict', 'unapproved', 'unreadable', 'orphaned'])
def test_status_states_and_check_are_read_only(tmp_path, state):
    # Each state comes from a real install followed by a native/ledger/declaration change; status never repairs it.
    case = _case(tmp_path / 'case')
    case.declare({'docs': URL})
    _install(case)
    states = ['current'] * 3
    if state == 'missing':
        (case.project / '.mcp.json').unlink()
        states[0] = state
    elif state == 'drift':
        (case.project / '.mcp.json').write_text(json.dumps({'mcpServers': {'docs': {'type': 'http', 'url': URL + '/edited'}}}))
        states[0] = state
    elif state == 'conflict':
        ledger = next((case.home / 'state/mcp/ledger').glob('*.json'))
        ledger.unlink()
        states = [state] * 3
    elif state == 'unapproved':
        case.declare({'docs': ENV_HEADER})
        states = [state] * 3
    elif state == 'unreadable':
        (case.project / '.mcp.json').write_bytes(b'{"mcpServers":{},}')
        states[0] = state
    elif state == 'orphaned':
        case.declare({})
        states = [state] * 3
    _assert_status(case, _expected(states))


@pytest.mark.parametrize('kind', ['link', 'oversize', 'directory'])
def test_unreadable_surface_variants_preserve_native_and_link_target(tmp_path, kind):
    # Linked, oversize and non-file surfaces report unreadable; neither status form changes their bytes or targets.
    case = _case(tmp_path / 'case')
    case.declare({'docs': URL})
    _install(case)
    path = case.project / '.mcp.json'
    path.unlink()
    target = Path(case.env['HOME']) / 'target.json'
    target.write_text('{}')
    if kind == 'link':
        path.symlink_to(target)
    elif kind == 'oversize':
        path.write_bytes(b' ' * (4 * 1024 * 1024 + 1))
    else:
        path.mkdir()
    _assert_status(case, _expected(['unreadable', 'current', 'current']))


def test_status_reports_server_then_agent_order_and_empty_plan(tmp_path):
    # Names sort before fixed agent order; removing all owned declarations and reinstalling leaves no rows and check=0.
    case = _case(tmp_path / 'case')
    case.declare({'zeta': URL, 'alpha': URL})
    _install(case)
    _assert_status(case, [*_expected(['current'] * 3, 'alpha'), *_expected(['current'] * 3, 'zeta')])
    case.declare({})
    _install(case)
    _assert_status(case, [])


@pytest.mark.parametrize('schema', [1, 2])
def test_legacy_and_global_status_have_no_mcp_payload_or_rows(tmp_path, schema):
    # Existing schema-1/2 and global status contain neither MCP rows, JSON keys nor the new authentication note.
    case = _case(tmp_path / 'case', schema=schema)
    _install(case)
    (case.home / 'global').mkdir(exist_ok=True)
    write_skillfile(case.home / 'global', {'schema_version': 1, 'skills': []})
    before = _snapshot(case.project, case.home, Path(case.env['HOME']))
    for globally in (False, True):
        result = _status(case, globally=globally)
        assert result.returncode == 0, result.stdout + result.stderr
        assert _rows(result) == []
        assert 'MCP authentication and connectivity:' not in result.stdout
        payload = json.loads(_status(case, '--json', globally=globally).stdout)
        assert 'mcp' not in (payload if globally else payload[0])
    assert _snapshot(case.project, case.home, Path(case.env['HOME'])) == before


def test_snapshot_control_detects_unexpected_file_and_changed_bytes(tmp_path):
    # The read-only witness ignores coordination only: unrelated extra files and modified bytes change its result.
    case = _case(tmp_path / 'case')
    before = _snapshot(case.project, case.home)
    (case.home / '.lock').write_text('coordination')
    assert _snapshot(case.project, case.home) == before
    planted = case.home / 'unexpected'
    planted.write_text('before')
    assert _snapshot(case.project, case.home) != before
    before_change = _snapshot(case.project, case.home)
    planted.write_text('after')
    assert _snapshot(case.project, case.home) != before_change


def test_changed_declaration_is_outdated_until_install_updates(tmp_path):
    # A changed desired URL is outdated, not native drift; install updates it and makes every row current.
    case = _case(tmp_path / 'case')
    case.declare({'docs': URL})
    _install(case)
    before = [(case.project / path).read_bytes() for _, path in SURFACES]
    case.declare({'docs': URL + '/next'})
    _assert_status(case, _expected(['outdated'] * 3))
    assert [(case.project / path).read_bytes() for _, path in SURFACES] == before
    updated = case.run('--fix-gitignore')
    assert updated.returncode == 0, updated.stdout + updated.stderr
    assert _rows(updated) == [f'mcp docs -> {agent} {path} (updated)' for agent, path in SURFACES]
    _assert_status(case, _expected(['current'] * 3))


@pytest.mark.parametrize(
    'overlap,states',
    [
        ('unreadable-orphan', ['unreadable', 'orphaned', 'orphaned']),
        ('modified-orphan', ['orphaned'] * 3),
        ('deleted-orphan', ['orphaned'] * 3),
        ('conflict-unapproved', ['conflict'] * 3),
        ('drift-unapproved', ['drift', 'unapproved', 'unapproved']),
        ('unapproved-outdated', ['unapproved'] * 3),
        ('unapproved-missing', ['unapproved'] * 3),
    ],
)
def test_overlapping_states_follow_read_ownership_consent_precedence(tmp_path, overlap, states):
    # Read failure wins over orphaning; orphaning wins over native drift; ownership wins over consent, then consent over update/missing.
    case = _case(tmp_path / 'case')
    case.declare({'docs': URL})
    _install(case)
    if overlap.endswith('orphan'):
        case.declare({})
        path = case.project / '.mcp.json'
        if overlap == 'unreadable-orphan':
            path.write_bytes(b'{invalid')
        elif overlap == 'deleted-orphan':
            for _, relative in SURFACES:
                (case.project / relative).unlink()
        else:
            path.write_text(json.dumps({'mcpServers': {'docs': {'type': 'http', 'url': URL + '/edited'}}}))
    else:
        case.declare({'docs': ENV_HEADER})
        if overlap == 'conflict-unapproved':
            next((case.home / 'state/mcp/ledger').glob('*.json')).unlink()
        elif overlap == 'drift-unapproved':
            (case.project / '.mcp.json').write_text(json.dumps({'mcpServers': {'docs': {'type': 'http', 'url': URL + '/edited'}}}))
        elif overlap == 'unapproved-missing':
            for _, path in SURFACES:
                (case.project / path).unlink()
    _assert_status(case, _expected(states))


@pytest.mark.parametrize('store', ['ledger', 'consent'])
def test_unknown_protected_state_is_error_without_repair(tmp_path, store):
    # Corrupt ledger/consent is ownership-unknown ERROR and check=1, never treated as empty or current and never repaired.
    case = _case(tmp_path / 'case')
    case.declare({'docs': URL})
    _install(case)
    ledger = next((case.home / 'state/mcp/ledger').glob('*.json'))
    path = case.home / 'state/mcp' / store / ledger.name
    path.parent.mkdir(mode=0o700, exist_ok=True)
    path.write_bytes(b'not-json\n')
    path.chmod(0o600)
    before = _snapshot(case.project, case.home, Path(case.env['HOME']))
    result = _status(case)
    assert result.returncode == 0, result.stdout + result.stderr
    assert 'ERROR mcp_ownership_unknown' in result.stdout
    checked = _status(case, '--check', '--json')
    assert checked.returncode == 1
    payload = json.loads(checked.stdout)[0]
    assert not payload['clean']
    assert payload['mcp'] == []
    assert any('mcp_ownership_unknown' in error for error in payload['errors'])
    assert _snapshot(case.project, case.home, Path(case.env['HOME'])) == before


def test_current_mcp_does_not_hide_source_status_error(tmp_path):
    # Current MCP rows preserve --check=1 and source errors when the existing source-lock evaluation fails.
    case = _case(tmp_path / 'case')
    case.declare({'docs': URL})
    _install(case)
    (case.project / 'Skillfile.lock.json').write_bytes(b'{invalid')
    before = _snapshot(case.project, case.home, Path(case.env['HOME']))
    result = _status(case, '--check', '--json')
    assert result.returncode == 1
    payload = json.loads(result.stdout)[0]
    assert payload['mcp'] == _expected(['current'] * 3)
    assert not payload['clean'] and payload['errors']
    assert _snapshot(case.project, case.home, Path(case.env['HOME'])) == before


def test_status_never_starts_stdio_or_auth_commands(tmp_path):
    # Status of an unapproved stdio declaration never invokes working PATH traps for its runner or auth clients.
    case = _case(tmp_path / 'case')
    traps = Path(case.env['HOME']) / 'traps'
    traps.mkdir()
    marker = Path(case.env['HOME']) / 'invoked'
    case.env['MCP_TRAP_MARKER'] = str(marker)
    case.env['PATH'] = str(traps) + os.pathsep + case.env.get('PATH', '')
    for name in ('npx', 'uvx', 'node', 'codex', 'claude', 'cursor'):
        if os.name == 'nt':
            executable = traps / f'{name}.cmd'
            executable.write_text('@echo off\r\necho invoked>>"%MCP_TRAP_MARKER%"\r\nexit /b 91\r\n')
            command = ['cmd.exe', '/d', '/c', str(executable)]
        else:
            executable = traps / name
            executable.write_text('#!/bin/sh\nprintf "invoked\\n" >> "$MCP_TRAP_MARKER"\nexit 91\n')
            executable.chmod(0o700)
            command = [str(executable)]
        control = subprocess.run(command, env=case.env, capture_output=True)
        assert control.returncode == 91 and marker.read_bytes().strip() == b'invoked'
        marker.unlink()
    case.declare({'docs': {'server': {'transport': 'stdio', 'command': 'uvx', 'args': ['pkg==1.0.0']}}})
    _assert_status(case, _expected(['unapproved'] * 3), skills_current=False)
    assert not marker.exists()


def test_recorded_consent_is_read_and_missing_consent_is_unapproved(tmp_path, monkeypatch, capsys):
    # Real terminal approval makes env routing current; an approved absent entry is missing, and lost consent is unapproved even on current bytes.
    from csk import cli

    case = _case(tmp_path / 'case')
    case.declare({'docs': ENV_HEADER})
    with monkeypatch.context() as patch:
        for key in list(os.environ):
            if key.startswith('CSK_') and key not in case.env:
                patch.delenv(key)
        for key, value in case.env.items():
            patch.setenv(key, value)
        patch.setattr(sys.stdin, 'isatty', lambda: True)
        patch.setattr(sys.stdout, 'isatty', lambda: True)
        patch.setattr('builtins.input', lambda _: 'yes')
        assert cli.main(['install', 'app', '--fix-gitignore']) == 0
    capsys.readouterr()
    _assert_status(case, _expected(['current'] * 3))
    (case.project / '.mcp.json').unlink()
    _assert_status(case, _expected(['missing', 'current', 'current']))
    _install(case)
    _assert_status(case, _expected(['current'] * 3))
    next((case.home / 'state/mcp/consent').glob('*.json')).unlink()
    _assert_status(case, _expected(['unapproved'] * 3))


def test_deselected_agent_entries_are_orphaned_and_still_reported(tmp_path):
    # Ledger-owned entries on deselected agents remain visible as orphaned beside the selected agent's current row.
    case = _case(tmp_path / 'case')
    case.declare({'docs': URL})
    _install(case)
    case.skillfile['agents'] = ['cursor']
    case.declare({'docs': URL})
    _assert_status(case, _expected(['orphaned', 'orphaned', 'current']), skills_current=False)
