from __future__ import annotations

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
