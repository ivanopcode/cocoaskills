from __future__ import annotations

import json
import os
import sys
from dataclasses import replace

from conftest import make_config, make_project, make_skill_repo, write_skillfile
from csk import config, installer
from csk.audit import runner
from csk.audit.model import Decision, Severity
from csk.audit.source_policy import SourcePolicy


def test_command_backend_receives_request_and_parses_findings(tmp_path, csk_home, skills_root):
    request_log = tmp_path / "request.json"
    backend = _write_backend_script(
        tmp_path,
        """
import json
import os
import sys

payload = json.load(sys.stdin.buffer)
if payload["skill"] != "csk-audit-canary":
    with open(os.environ["REQUEST_LOG"], "w", encoding="utf-8") as fh:
        json.dump(payload, fh, sort_keys=True)
print(json.dumps({
    "schema_version": 1,
    "findings": [
        {
            "id": "fixture.prompt.note",
            "surface": "prompt",
            "category": "semantic-risk",
            "severity": "medium",
            "location": {"file": "SKILL.md", "span": [1, 1]},
            "evidence": "fixture finding",
            "detector": "fixture",
            "confidence": "high",
            "verifiable": True,
            "capability_violation": None,
        }
    ] if payload["skill"] != "csk-audit-canary" else [
        {
            "id": "fixture.canary",
            "surface": "prompt",
            "category": "canary",
            "severity": "high",
            "location": {"file": "SKILL.md", "span": [1, 1]},
            "evidence": "canary finding",
            "detector": "fixture",
            "confidence": "high",
            "verifiable": True,
            "capability_violation": None,
        }
    ],
}))
""",
    )
    make_skill_repo(
        skills_root,
        "skill-a",
        {
            "csk-skill.json": json.dumps(
                {"schema_version": 3, "capabilities": {"network": "none", "exec": "none"}, "commands": {}}
            ),
            "references/note.md": "backend sees this file\n",
        },
        tag="v1",
    )
    project = make_project(tmp_path)
    write_skillfile(project, {"schema_version": 1, "skills": [{"name": "skill-a", "tag": "v1"}]})
    cfg = replace(
        make_config(csk_home, skills_root, project),
        audit=config.AuditConfig(
            backend="local-command",
            backends={
                "local-command": {
                    "kind": "command",
                    "command": [sys.executable, str(backend)],
                    "env": {"REQUEST_LOG": str(request_log)},
                }
            },
        ),
    )

    reports = runner.audit_projects(cfg, alias="app")

    assert reports[0].decision == Decision.WARN
    assert reports[0].findings[-1].id == "fixture.prompt.note"
    payload = json.loads(request_log.read_text(encoding="utf-8"))
    assert payload["skill"] == "skill-a"
    assert payload["files"]["references/note.md"]["content"] == "backend sees this file\n"


def test_command_backend_timeout_warns_in_advisory_install(tmp_path, csk_home, skills_root):
    backend = _write_backend_script(
        tmp_path,
        """
import json
import time
import sys

payload = json.load(sys.stdin.buffer)
if payload["skill"] == "csk-audit-canary":
    print(json.dumps({
        "schema_version": 1,
        "findings": [
            {
                "id": "fixture.canary",
                "surface": "prompt",
                "category": "canary",
                "severity": "high",
                "location": {"file": "SKILL.md", "span": [1, 1]},
                "evidence": "canary finding",
                "detector": "fixture",
                "confidence": "high",
                "verifiable": True,
                "capability_violation": None,
            }
        ],
    }))
    raise SystemExit(0)
time.sleep(10)
""",
    )
    make_skill_repo(
        skills_root,
        "skill-a",
        {
            "csk-skill.json": json.dumps(
                {"schema_version": 3, "capabilities": {"network": "none", "exec": "none"}, "commands": {}}
            )
        },
        tag="v1",
    )
    project = make_project(tmp_path)
    write_skillfile(project, {"schema_version": 1, "skills": [{"name": "skill-a", "tag": "v1"}]})
    cfg = replace(
        make_config(csk_home, skills_root, project),
        audit=config.AuditConfig(
            enabled=True,
            mode="advisory",
            backend="local-command",
            backends={
                "local-command": {
                    "kind": "command",
                    "command": [sys.executable, str(backend)],
                    "timeout_seconds": 1,
                }
            },
        ),
    )

    result = installer.install(cfg)[0]

    assert not result.errors
    assert any("audit warning: audit backend failed" in message for message in result.messages)
    assert (project / ".agents" / "skills" / "skill-a").exists()


def test_command_backend_failure_redacts_stderr(tmp_path, csk_home, skills_root):
    backend = _write_backend_script(
        tmp_path,
        """
import json
import sys

payload = json.load(sys.stdin.buffer)
if payload["skill"] == "csk-audit-canary":
    print(json.dumps({
        "schema_version": 1,
        "findings": [
            {
                "id": "fixture.canary",
                "surface": "prompt",
                "category": "canary",
                "severity": "high",
                "location": {"file": "SKILL.md", "span": [1, 1]},
                "evidence": "canary finding",
                "detector": "fixture",
                "confidence": "high",
                "verifiable": True,
                "capability_violation": None,
            }
        ],
    }))
    raise SystemExit(0)
print("failed at https://example.test/path?token=secret#frag", file=sys.stderr)
raise SystemExit(7)
""",
    )
    make_skill_repo(
        skills_root,
        "skill-a",
        {
            "csk-skill.json": json.dumps(
                {"schema_version": 3, "capabilities": {"network": "none", "exec": "none"}, "commands": {}}
            )
        },
        tag="v1",
    )
    project = make_project(tmp_path)
    write_skillfile(project, {"schema_version": 1, "skills": [{"name": "skill-a", "tag": "v1"}]})
    cfg = replace(
        make_config(csk_home, skills_root, project),
        audit=config.AuditConfig(
            enabled=True,
            mode="advisory",
            backend="local-command",
            backends={"local-command": {"kind": "command", "command": [sys.executable, str(backend)]}},
        ),
    )

    result = installer.install(cfg)[0]

    assert not result.errors
    message = "\n".join(result.messages)
    assert "https://example.test/path?<redacted>#<redacted>" in message
    assert "token=secret" not in message


def test_oversize_request_skips_backend_and_blocks_strict(tmp_path, csk_home, skills_root):
    make_skill_repo(
        skills_root,
        "skill-a",
        {
            "csk-skill.json": json.dumps(
                {"schema_version": 3, "capabilities": {"network": "none", "exec": "none"}, "commands": {}}
            ),
            "references/big.md": "x" * 200,
        },
        tag="v1",
    )
    project = make_project(tmp_path)
    write_skillfile(project, {"schema_version": 1, "skills": [{"name": "skill-a", "tag": "v1"}]})
    cfg = replace(
        make_config(csk_home, skills_root, project),
        audit=config.AuditConfig(
            enabled=True,
            mode="strict",
            max_request_bytes=1,
            backend="local-command",
            backends={"local-command": {"kind": "command", "command": ["/definitely/not/executed"]}},
        ),
    )

    result = installer.install(cfg)[0]

    assert result.errors
    assert "audit.request.too-large" in result.errors[0]
    assert not (project / ".agents" / "skills" / "skill-a").exists()


def test_cloud_command_backend_receives_redacted_files(tmp_path, csk_home, skills_root):
    request_log = tmp_path / "request.json"
    backend = _write_backend_script(
        tmp_path,
        """
import json
import os
import sys

payload = json.load(sys.stdin.buffer)
if payload["skill"] != "csk-audit-canary":
    with open(os.environ["REQUEST_LOG"], "w", encoding="utf-8") as fh:
        json.dump(payload, fh, sort_keys=True)
print(json.dumps({
    "schema_version": 1,
    "findings": [
        {
            "id": "fixture.canary",
            "surface": "prompt",
            "category": "canary",
            "severity": "high",
            "location": {"file": "SKILL.md", "span": [1, 1]},
            "evidence": "canary finding",
            "detector": "fixture",
            "confidence": "high",
            "verifiable": True,
            "capability_violation": None,
        }
    ] if payload["skill"] == "csk-audit-canary" else [],
}))
""",
    )
    make_skill_repo(
        skills_root,
        "skill-a",
        {
            "csk-skill.json": json.dumps(
                {"schema_version": 3, "capabilities": {"network": "none", "exec": "none"}, "commands": {}}
            ),
            "references/secret.md": "API_TOKEN=super-secret-token-value\n",
        },
        tag="v1",
    )
    project = make_project(tmp_path)
    write_skillfile(project, {"schema_version": 1, "skills": [{"name": "skill-a", "tag": "v1"}]})
    cfg = replace(
        make_config(csk_home, skills_root, project),
        audit=config.AuditConfig(
            allow_cloud=True,
            source_policy=SourcePolicy(default_class="public"),
            backend="cloud-command",
            backends={
                "cloud-command": {
                    "kind": "command",
                    "cloud": True,
                    "command": [sys.executable, str(backend)],
                    "env": {"REQUEST_LOG": str(request_log)},
                }
            },
        ),
    )

    reports = runner.audit_projects(cfg, alias="app")

    payload = json.loads(request_log.read_text(encoding="utf-8"))
    assert payload["redacted"] is True
    assert payload["files"]["references/secret.md"]["content"] == "API_TOKEN=<redacted>\n"
    redaction_findings = [finding for finding in reports[0].findings if finding.id == "audit.redaction.applied"]
    assert redaction_findings
    assert redaction_findings[0].severity == Severity.INFO


def test_codex_backend_constructs_hermetic_exec_command(monkeypatch, tmp_path, csk_home, skills_root):
    log_path = tmp_path / "codex-log.jsonl"
    _write_fake_codex(tmp_path, monkeypatch, log_path)
    make_skill_repo(
        skills_root,
        "skill-a",
        {
            "csk-skill.json": json.dumps(
                {"schema_version": 3, "capabilities": {"network": "none", "exec": "none"}, "commands": {}}
            ),
            "references/note.md": "codex sees this as data\n",
        },
        tag="v1",
    )
    project = make_project(tmp_path)
    write_skillfile(project, {"schema_version": 1, "skills": [{"name": "skill-a", "tag": "v1"}]})
    cfg = replace(
        make_config(csk_home, skills_root, project),
        audit=config.AuditConfig(
            backend="codex-local",
            model="qwen2.5-coder:32b",
            backends={
                "codex-local": {
                    "kind": "codex",
                    "profile": "local-audit",
                    "oss": True,
                    "local_provider": "ollama",
                    "extra_args": ["--quiet"],
                }
            },
        ),
    )

    reports = runner.audit_projects(cfg, alias="app")

    assert reports[0].decision == Decision.ALLOW
    entries = [json.loads(line) for line in log_path.read_text(encoding="utf-8").splitlines()]
    assert len(entries) == 2
    real = entries[-1]
    argv = real["argv"]
    assert argv[:2] == ["exec", "--sandbox"]
    assert "--sandbox" in argv and argv[argv.index("--sandbox") + 1] == "read-only"
    assert "--ask-for-approval" in argv and argv[argv.index("--ask-for-approval") + 1] == "never"
    assert "--cd" in argv
    assert "--ephemeral" in argv
    assert "--ignore-rules" in argv
    assert "--skip-git-repo-check" in argv
    assert "--output-schema" in argv
    assert "--output-last-message" in argv
    assert "--model" in argv and argv[argv.index("--model") + 1] == "qwen2.5-coder:32b"
    assert "--profile" in argv and argv[argv.index("--profile") + 1] == "local-audit"
    assert "--oss" in argv
    assert "--local-provider" in argv and argv[argv.index("--local-provider") + 1] == "ollama"
    assert "--quiet" in argv
    assert "--search" not in argv
    assert argv[-1] == "-"
    assert real["cwd_entries"] == []
    assert "codex sees this as data" in real["stdin"]
    assert "SKILL.md" in real["stdin"]


def _write_backend_script(tmp_path, body: str):
    script = tmp_path / "backend.py"
    script.write_text(body.lstrip(), encoding="utf-8")
    return script


def _write_fake_codex(tmp_path, monkeypatch, log_path):
    fake = tmp_path / "fake_codex.py"
    fake.write_text(
        """
import json
import os
import sys

argv = sys.argv[1:]
stdin = sys.stdin.read()
response_file = argv[argv.index("--output-last-message") + 1]
entry = {
    "argv": argv,
    "cwd": os.getcwd(),
    "cwd_entries": sorted(os.listdir(os.getcwd())),
    "stdin": stdin,
}
with open(os.environ["CODEX_LOG"], "a", encoding="utf-8") as fh:
    fh.write(json.dumps(entry, sort_keys=True) + "\\n")
if "csk-audit-canary" in stdin:
    findings = [
        {
            "id": "fixture.canary",
            "surface": "prompt",
            "category": "canary",
            "severity": "high",
            "location": {"file": "SKILL.md", "span": [1, 1]},
            "evidence": "canary finding",
            "detector": "fake-codex",
            "confidence": "high",
            "verifiable": True,
            "capability_violation": None,
        }
    ]
else:
    findings = []
with open(response_file, "w", encoding="utf-8") as fh:
    json.dump({"schema_version": 1, "findings": findings}, fh)
""".lstrip(),
        encoding="utf-8",
    )
    if sys.platform == "win32":
        wrapper = tmp_path / "codex.cmd"
        wrapper.write_text(f'@echo off\r\n"{sys.executable}" "{fake}" %*\r\n', encoding="utf-8")
    else:
        wrapper = tmp_path / "codex"
        wrapper.write_text(f'#!/bin/sh\nexec "{sys.executable}" "{fake}" "$@"\n', encoding="utf-8")
        wrapper.chmod(0o755)
    monkeypatch.setenv("CODEX_LOG", str(log_path))
    monkeypatch.setenv("PATH", str(tmp_path) + os.pathsep + os.environ.get("PATH", ""))
