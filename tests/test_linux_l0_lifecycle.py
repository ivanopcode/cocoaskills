"""Offline non-build lifecycle used by the Ubuntu L0 CI steps and native probes."""
from __future__ import annotations

import json
import socket
import sys

import pytest

from conftest import init_git_repo
from csk import cli
from csk.sources import _selection_fs
from csk.sources import transport
from test_source_closure_refresh import _write_skill, _write_skillfile_v2

pytestmark = pytest.mark.skipif(
    not _selection_fs.supports_descriptor_traversal(),
    reason=_selection_fs.NO_DESCRIPTOR_TRAVERSAL_REASON,
)


@pytest.mark.parametrize("schema", [2, 3])
def test_offline_non_build_lifecycle(monkeypatch, tmp_path, capsys, schema):
    # Real CLI handlers install local text-only skills, remain idempotent, audit
    # each member and apply removal only through upgrade. Network attempts fail
    # at both source transport and Python socket boundaries; no skills are run.
    def no_network(*args, **kwargs):
        pytest.fail("offline lifecycle attempted network access")

    monkeypatch.setattr(transport, "resolve_ref", no_network)
    monkeypatch.setattr(transport, "acquire_network", no_network)
    monkeypatch.setattr(socket, "create_connection", no_network)
    monkeypatch.setattr(socket.socket, "connect", no_network)
    manager = tmp_path / "manager"
    config_path = manager / "config.json"
    monkeypatch.setenv("CSK_CONFIG", str(config_path))
    source = tmp_path / "source"
    for name in ("alpha", "beta"):
        _write_skill(source / name, name)
    project = init_git_repo(tmp_path / "project")
    rows = []

    def invoke(argv, expected=0):
        code = cli.main(argv)
        captured = capsys.readouterr()
        rows.append({"argv": ["csk", *argv], "exit": code,
                     "stdout": captured.out, "stderr": captured.err})
        assert code == expected, rows[-1]
        return captured.out, captured.err

    try:
        invoke(["bootstrap", "--non-interactive", "--skills-root", str(source),
                "--default-agents", "codex_cli,claude_code"])
        invoke(["init", str(project)])
        _write_skillfile_v2(project, {"local": {"path": str(source)}}, [
            {"name": name, "from": "local", "directory": name}
            for name in ("alpha", "beta")
        ])
        skillfile = project / "Skillfile.json"
        data = json.loads(skillfile.read_text())
        data["schema_version"] = schema
        skillfile.write_text(json.dumps(data))
        monkeypatch.chdir(project)
        invoke(["install"])
        lock_bytes = (project / "Skillfile.lock.json").read_bytes()
        invoke(["install"])
        assert (project / "Skillfile.lock.json").read_bytes() == lock_bytes
        invoke(["status", "--check", "--json"])
        invoke(["update"])
        invoke(["upgrade"])
        out, _ = invoke(["audit", "--json"])
        assert {r["skill"] for r in json.loads(out)["reports"]} == {"alpha", "beta"}
        before_remove = (project / "Skillfile.lock.json").read_bytes()
        out, _ = invoke(["remove", "beta"])
        assert "Run 'csk upgrade' to apply." in out
        assert "Run 'csk install'" not in out
        assert (project / "Skillfile.lock.json").read_bytes() == before_remove
        assert (project / ".agents" / "skills" / "beta").exists()
        out, err = invoke(["install"], expected=1)
        assert "source_lock_stale:" in out + err
        assert (project / "Skillfile.lock.json").read_bytes() == before_remove
        assert (project / ".agents" / "skills" / "beta").exists()
        invoke(["upgrade"])
        assert not (project / ".agents" / "skills" / "beta").exists()
        assert (project / ".agents" / "skills" / "alpha" / "SKILL.md").is_file()
        invoke(["status", "--check", "--json"])
        out, _ = invoke(["audit", "--json"])
        assert [r["skill"] for r in json.loads(out)["reports"]] == ["alpha"]
    finally:
        print(json.dumps({"lifecycle_commands": rows}, indent=2))


@pytest.mark.skipif(sys.platform != "linux", reason="Linux admission refusal")
def test_linux_external_go_build_still_refuses_before_acquisition(
    monkeypatch, tmp_path, csk_home, skills_root, capsys,
):
    # A real schema-7 external-build declaration is refused by Linux admission;
    # no transport, credential provider or Go worker is reached and nothing installs.
    from conftest import make_project, make_config
    from csk import config, installer
    from csk.builds import go_v1

    def forbidden(*args, **kwargs):
        pytest.fail("Linux external-build refusal crossed into acquisition or execution")

    monkeypatch.setattr(transport, "acquire_network", forbidden)
    monkeypatch.setattr(installer, "_endpoint_ssh_credentials", forbidden)
    monkeypatch.setattr(installer, "_endpoint_https_credentials", forbidden)
    monkeypatch.setattr(go_v1._NativeControlDomain, "launch", forbidden)
    project = make_project(tmp_path)
    source = tmp_path / "external-source"
    _write_skill(source / "external", "external", commands={"external-tool": {
        "type": "build", "driver": "go-repository-v1",
        "repository": "tools", "target": "external-tool",
    }})
    path = source / "external" / "agent-skill.json"
    data = json.loads(path.read_text())
    data["build_repositories"] = {"tools": {
        "git": "https://example.test/external-tool.git",
        "locked_commit": {"object_format": "sha1", "hex": "a" * 40},
    }}
    path.write_text(json.dumps(data))
    _write_skillfile_v2(project, {"local": {"path": str(source)}}, [
        {"name": "external", "from": "local", "directory": "external"},
    ])
    cfg = make_config(csk_home, skills_root, project)
    config.save_config(cfg)
    monkeypatch.setenv("CSK_CONFIG", str(cfg.path))
    assert cli.main(["install", "app"]) == 1
    output = capsys.readouterr()
    assert "source_member_invalid:" in output.out + output.err
    assert "Linux qualification is deferred" in output.out + output.err
    assert not (project / ".agents").exists()
    assert not (project / "Skillfile.lock.json").exists()
    assert not (csk_home / "store").exists()
    assert not (csk_home / "builds").exists()
