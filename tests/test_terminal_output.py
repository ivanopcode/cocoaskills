from __future__ import annotations
import sys
import shutil

import json
from pathlib import Path

import pytest

from csk import cli, manifest, status
from csk.terminal import terminal_text


# All C0/C1 controls are inert, while ordinary Unicode and valid URLs survive.
@pytest.mark.parametrize("text", ["\x1b[31m", "\nforged", "\r", "\t", "\x00", "\x7f", "\x85", "\u202e", "\u2028"])
def test_terminal_controls_are_visible(text):
    rendered = terminal_text(text)
    assert text not in rendered
    assert "\\" in rendered
    assert terminal_text(rendered) == rendered
    assert terminal_text("日本語 café https://example.org/é.git") == "日本語 café https://example.org/é.git"


# Injected row breaks/ANSI cannot reach status display; JSON retains the original value.
def test_status_display_and_json_are_separate():
    raw = "normal\nforged\x1b[31m"
    row = status.SkillStatus("healthy", "tag", raw, None, None, "error", raw)
    project = status.ProjectStatus(raw, Path(raw), True, [row], (raw,), errors=(raw,), capability_evidence_error=raw)
    rendered = status.render_collected([project])
    assert "\x1b" not in rendered
    assert "\nforged" not in rendered
    assert r"\nforged\x1b[31m" in rendered
    payload = status.statuses_to_payload([project])
    assert payload[0]["skills"][0]["ref"] == raw
    assert payload[0]["skills"][0]["detail"] == raw


# The URL admission gate rejects each Cc (including DEL/C1), without echoing it.
@pytest.mark.parametrize("control", [chr(n) for n in range(32)] + [chr(n) for n in range(127, 160)])
def test_manifest_git_rejects_controls(tmp_path, control):
    path = tmp_path / "Skillfile.json"
    value = {"schema_version": 1, "skills": [{"name": "demo", "tag": "v1", "git": "https://example.org/" + control + "repo.git"}]}
    path.write_text(json.dumps(value))
    with pytest.raises(manifest.ManifestError, match="field 'git' must not contain control characters") as error:
        manifest.load_manifest(tmp_path)
    assert control not in str(error.value)
    value["skills"][0]["git"] = "https://example.org/café.git"
    path.write_text(json.dumps(value))
    assert manifest.load_manifest(tmp_path).skills[0].git == "https://example.org/café.git"


# CLI exception output escapes controls without changing the selected failure exit.
def test_cli_error_is_terminal_safe(monkeypatch, capsys):
    def fail(*args, **kwargs):
        raise ValueError("bad\nforged\x1b[31m")
    monkeypatch.setattr(cli.config, "load_config", fail)
    assert cli.main(["status"]) == 2
    captured = capsys.readouterr()
    assert "\x1b" not in captured.err
    assert "\nforged" not in captured.err
    assert r"bad\nforged\x1b[31m" in captured.err


# Audit evidence/location/refs and hints are escaped only for display, with valid text controls.
def test_audit_and_diagnostic_renderers():
    from csk.audit.model import Finding, Surface, Severity, Location, Decision
    from csk.audit.pipeline import AuditReport, render_reports, reports_to_payload
    from csk.sources.diagnostics import format_diagnostic, REMEDIATION_BY_CODE

    raw = "café\nforged\x1b[31m"
    finding = Finding("test", Surface.CODE, "test", Severity.HIGH, Location(raw), raw, "test", "high", True)
    report = AuditReport("project", "demo", "demo", "tag", raw, "a" * 40, 1, None, (), "b" * 64, (finding,), Decision.WARN, "today")
    rendered = render_reports((report,))
    assert "\x1b" not in rendered
    assert "\nforged" not in rendered
    assert "café" in rendered
    assert reports_to_payload((report,))["reports"][0]["findings"][0]["evidence"] == raw
    assert finding.evidence == raw
    code = next(iter(REMEDIATION_BY_CODE))
    diagnostic = format_diagnostic(code, raw)
    assert "\x1b" not in diagnostic
    assert "\nforged" not in diagnostic
    assert "\nremediation: " in diagnostic
    assert "café" in format_diagnostic(code, "café")


# Unclassified check errors use the same display boundary as classified diagnostics.
def test_check_failure_fallback_is_terminal_safe(capsys):
    cli._print_check_failure(ValueError("café\nforged\x1b[31m"))
    assert capsys.readouterr().err == "error: café\\nforged\\x1b[31m\n"
    cli._print_check_failure(ValueError("café"))
    assert capsys.readouterr().err == "error: café\n"


# Unknown package agent names cannot forge warning rows; supported agents remain silent.
def test_agent_warning_escapes_package_names(capsys):
    from csk import adapters

    adapters.warn_unknown_agents(["unknown\nforged\x1b[31m"])
    warning = capsys.readouterr().err
    assert "\nforged" not in warning
    assert "\x1b" not in warning
    assert r"unknown\nforged\x1b[31m" in warning
    adapters.warn_unknown_agents(list(adapters.known_agents()))
    assert capsys.readouterr().err == ""


# bash and zsh receive precisely the hostile pathname; neighbors remain untouched.
@pytest.mark.parametrize('shell', ['bash', 'zsh'])
@pytest.mark.parametrize('name', ["ordinary café ' file", "hostile\n'\t\x1b[31m\\file", "unicode\x85\u2028\U000e0001file"])
@pytest.mark.skipif(sys.platform == 'win32', reason='POSIX shell remedies use POSIX paths; Windows does not execute this shell contract')
def test_copyable_shell_word(tmp_path, shell, name):
    import shutil
    import shlex
    import subprocess
    from csk.terminal import shell_word

    executable = '/bin/bash' if shell == 'bash' and sys.platform == 'darwin' else shutil.which(shell)
    if executable is None:
        pytest.skip(f'{shell} is unavailable')
    target = tmp_path / name
    target.write_text('target')
    neighbor = tmp_path / 'neighbor'
    neighbor.write_text('preserved')
    word = shell_word(str(target))
    assert not any(ord(c) < 32 or 127 <= ord(c) <= 159 for c in word)
    if terminal_text(str(target)) == str(target):
        assert word == shlex.quote(str(target))
    else:
        assert word.startswith("$'")
    probe = subprocess.run([executable, '-c', "printf '%s' " + word], capture_output=True)
    assert probe.returncode == 0
    assert probe.stdout == str(target).encode()
    removed = subprocess.run([executable, '-c', 'rm -f -- ' + word], capture_output=True)
    assert removed.returncode == 0
    assert not target.exists()
    assert neighbor.read_text() == 'preserved'


# Trace package names, refs, paths, remote text, URLs and hints through display entry points.
@pytest.mark.parametrize('plant', ['\x1b[31m', '\nforged-row'])
def test_package_interpolation_boundaries(plant):
    from csk import attest, installer, git_ops, mcp_project, skillcheck
    from csk.sources.diagnostics import format_diagnostic, REMEDIATION_BY_CODE

    raw = 'café' + plant
    outputs = [
        attest.render([attest.AttestResult(raw, raw, 'ok', 'https://example.org/' + raw, raw)]),
        skillcheck.format_issue(skillcheck.ValidationIssue('error', 'test', raw, raw)),
        installer.failure_text(git_ops.GitError('remote: ' + raw)),
        format_diagnostic(next(iter(REMEDIATION_BY_CODE)), raw),
    ]
    for text in outputs:
        assert plant not in text
        assert 'café' in text


# Composed remediation layout stays separate from escaped remote data.
def test_failure_layout_preserved():
    from csk import installer, git_ops
    error = git_ops.GitError('remote\nforged\x1b[31m')
    error.add_note('remediation: retry with the configured source')
    rendered = installer.failure_text(error)
    assert '\nforged' not in rendered and '\x1b' not in rendered
    assert '\nremediation: retry with the configured source' in rendered


# JSON preserves semantic controls and historical remediation; display alone escapes reason data.
def test_schema2_error_json_and_layout():
    from csk.sources.diagnostics import REMEDIATION_BY_CODE
    code = next(iter(REMEDIATION_BY_CODE))
    raw = code + ': café\nforged\x1b[31m'
    stored = status._schema2_status_error_row(raw)
    assert '\nforged\x1b' in stored
    assert stored.endswith('\nremediation: ' + REMEDIATION_BY_CODE[code])
    displayed = status._schema2_status_error_row(stored, terminal=True)
    assert '\nforged' not in displayed and '\x1b' not in displayed
    assert displayed.count('\nremediation: ') == 1


# Dependency hints enter human diagnostics escaped; the package hint itself stays raw.
@pytest.mark.parametrize('hint', ['café', 'café\nforged\x1b[31m'])
def test_dependency_hint_boundary(hint):
    from types import SimpleNamespace
    from csk import installer
    dependency = SimpleNamespace(type='skill', skill='provider', command='tool', name='tool', hint=hint)
    plan = SimpleNamespace(decl=SimpleNamespace(name='consumer'), spec=SimpleNamespace(dependencies={'tool': dependency}))
    errors = installer.skill_command_dependency_errors(plan, [plan])
    assert len(errors) == 1 and "Missing skill dependency 'provider'" in errors[0]
    assert 'Hint: ' + terminal_text(hint) in errors[0]
    assert dependency.hint == hint
    assert '\nforged' not in errors[0] and '\x1b' not in errors[0]


# A wrapped Git failure still escapes remote output before trusted note layout is appended.
def test_wrapped_git_failure_boundary():
    from csk import git_ops, installer
    try:
        try:
            raise git_ops.GitError('remote\nforged\x1b[31m')
        except git_ops.GitError as cause:
            raise installer.InstallError(str(cause)) from cause
    except installer.InstallError as error:
        error.add_note('remediation: retry')
        rendered = installer.failure_text(error)
    assert '\nforged' not in rendered and '\x1b' not in rendered
    assert rendered.endswith('\nremediation: retry')


# System-command hint rendering escapes a copy; closure errors retain raw semantic data.
@pytest.mark.parametrize('hint', ['café', 'café\nforged\x1b[31m'])
def test_system_hint_boundary(hint):
    from csk import closure, installer
    item = closure.MissingSystemCommand('demo', 'tool', hint)
    error = closure.MissingSystemCommandsError([item])
    rendered = installer.failure_text(error)
    assert 'Missing system command' in rendered and terminal_text(hint) in rendered
    assert '\nforged' not in rendered and '\x1b' not in rendered
    assert error.missing[0].hint == hint and hint in str(error)
