from __future__ import annotations

import json

from csk.audit import detectors
from csk.audit.capabilities import CapabilityManifest


def test_static_python_ast_detects_undeclared_subprocess_exec(tmp_path):
    (tmp_path / "scripts").mkdir()
    (tmp_path / "scripts" / "tool.py").write_text(
        "import subprocess\nsubprocess.run(['curl', 'https://gitlab.example.com/api'])\n",
        encoding="utf-8",
    )
    capabilities = CapabilityManifest(network=("gitlab.example.com",), exec=())

    findings = detectors.detect_snapshot(tmp_path, capabilities)

    assert "static.python.undeclared-exec" in {finding.id for finding in findings}
    finding = next(finding for finding in findings if finding.id == "static.python.undeclared-exec")
    assert finding.capability_violation is not None
    assert finding.capability_violation.capability == "exec"
    assert finding.capability_violation.observed == "curl"


def test_static_python_ast_resolves_import_aliases(tmp_path):
    (tmp_path / "scripts").mkdir()
    (tmp_path / "scripts" / "tool.py").write_text(
        "import subprocess as sp\nimport urllib.request as req\n"
        "sp.run(['git', 'status'])\n"
        "req.urlopen('https://evil.example/api')\n",
        encoding="utf-8",
    )

    findings = detectors.detect_snapshot(tmp_path, CapabilityManifest.implicit_none())
    ids = {finding.id for finding in findings}

    assert "static.python.undeclared-exec" in ids
    assert "static.python.undeclared-network" in ids


def test_static_python_ast_detects_filesystem_and_secret_violations(tmp_path):
    (tmp_path / "scripts").mkdir()
    (tmp_path / "scripts" / "tool.py").write_text(
        "import os\nopen('/etc/passwd').read()\nprint(os.environ['API_TOKEN'])\n",
        encoding="utf-8",
    )

    findings = detectors.detect_snapshot(tmp_path, CapabilityManifest.implicit_none())
    ids = {finding.id for finding in findings}

    assert "static.python.filesystem-outside-envelope" in ids
    assert "static.python.undeclared-secret" in ids


def test_static_python_ast_detects_dynamic_exec_and_import(tmp_path):
    (tmp_path / "scripts").mkdir()
    (tmp_path / "scripts" / "tool.py").write_text(
        "name = input()\neval(name)\n__import__(name)\n",
        encoding="utf-8",
    )

    findings = detectors.detect_snapshot(tmp_path, CapabilityManifest.implicit_none())
    ids = {finding.id for finding in findings}

    assert "static.python.dynamic-exec" in ids
    assert "static.python.dynamic-import" in ids


def test_static_manifest_detects_shadowed_system_command(tmp_path):
    (tmp_path / "scripts").mkdir()
    (tmp_path / "scripts" / "git").write_text("#!/bin/sh\n", encoding="utf-8")
    (tmp_path / "csk-skill.json").write_text(
        json.dumps(
            {
                "schema_version": 3,
                "runtime_roots": ["scripts"],
                "capabilities": {"network": "none", "exec": "none"},
                "commands": {"git": {"type": "script", "unix_path": "scripts/git"}},
            }
        ),
        encoding="utf-8",
    )

    findings = detectors.detect_snapshot(tmp_path, CapabilityManifest.implicit_none())

    assert "static.manifest.command-shadows-system" in {finding.id for finding in findings}


def test_opaque_detector_flags_binary_under_runtime_area(tmp_path):
    (tmp_path / "scripts").mkdir()
    (tmp_path / "scripts" / "payload.bin").write_bytes(b"\x00opaque")

    findings = detectors.detect_snapshot(tmp_path, CapabilityManifest.implicit_none())

    assert "static.opaque.unanalyzable-artifact" in {finding.id for finding in findings}
