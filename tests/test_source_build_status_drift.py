"""Read-only schema-2/3 Go currentness; toolchain identities are controlled.

The historical real Go 1.26.10 -> 1.27.3 witness establishes the operator
contract. Here only toolchain observation and fixture compilation are replaced;
source capture, planning, protected cache, audit reports, publication and CLI
status are real. No native compiler or installed command is executed.
"""
from __future__ import annotations

import hashlib
import json
import os
import shutil
import stat
import sys
from dataclasses import replace
from pathlib import Path

import pytest
from conftest import init_git_repo, make_config, write_files, write_skillfile
from test_installer_transactions import _build_skill_files, _install_fake_build_pipeline, _native_target

from csk import cli, config, install_marker, installer, status
from csk.audit import pipeline
from csk.builds import cache, go_v1, toolchain
from csk.sources import publish
from csk.sources import source_audit

pytestmark = pytest.mark.usefixtures("stable_env")


def _state(*roots: Path) -> dict[str, tuple[int, str | None]]:
    result = {}
    for root in roots:
        for path in (root, *sorted(root.rglob("*"))):
            if not path.exists() and not path.is_symlink():
                continue
            info = path.lstat()
            payload = (os.readlink(path) if stat.S_ISLNK(info.st_mode)
                       else hashlib.sha256(path.read_bytes()).hexdigest()
                       if stat.S_ISREG(info.st_mode) else None)
            result[str(path)] = (info.st_mode, payload)
    return result


def _observe(monkeypatch, *, changed=False, change_target=False):
    target = _native_target()
    if change_target:
        target = replace(target, tuning={"GOARM64": "v9.0"} if target.goarch == "arm64" else {"GOAMD64": "v2"})
    identity = toolchain.ToolchainIdentity(
        algorithm=toolchain.TOOLCHAIN_ALGORITHM,
        content_sha256="sha256:" + ("b" if changed else "a") * 64,
        go_relpath=toolchain.GO_RELPATH,
        go_version=f"go version go{'1.27.3' if changed else '1.26.10'} {target.goos}/{target.goarch}",
    )

    class Session:
        def __init__(self, cfg):
            self.target, self.toolchain = target, identity
            self.operation_root = cfg.private_base / "controlled-session"
            self.operation_root.mkdir(mode=0o700)
            self.executable = self.operation_root / "never-executed-go"
            self.goroot = self.operation_root / "goroot"

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return None

    monkeypatch.setattr(toolchain, "establish_toolchain", Session)


def _fixture(monkeypatch, tmp_path, csk_home, skills_root, schema):
    project = tmp_path / "project"
    init_git_repo(project)
    source = tmp_path / "source" / "built"
    files = _build_skill_files("tool")
    files["SKILL.md"] = "---\nname: built\ndescription: controlled build fixture\n---\n\n# Built\n"
    write_files(source, files)
    write_skillfile(project, {"schema_version": schema, "agents": ["codex_cli"],
                             "sources": {"local": {"path": str(source.parent)}},
                             "skills": [{"name": "built", "from": "local", "directory": "built"}]})
    cfg = replace(make_config(csk_home, skills_root, project, agents=["codex_cli"]),
                  experimental=config.ExperimentalConfig(skillfile_sources=True),
                  audit=config.AuditConfig(enabled=True), disable_builtin_registries=True)
    config.save_config(cfg)
    monkeypatch.setenv("CSK_CONFIG", str(cfg.path))
    monkeypatch.chdir(project)
    events = []
    _install_fake_build_pipeline(monkeypatch, events=events)
    _observe(monkeypatch)
    installed = installer.install(cfg, alias="app")[0]
    assert installed.status == "ok", installed.errors
    assert events == ["build:tool"]
    marker_path = project / ".agents/skills/built/.csk-install.json"
    marker = install_marker.read_install_marker(marker_path.read_bytes())
    assert isinstance(marker, install_marker.InstallMarkerV5)
    entry = csk_home / "builds/go-v1-receipt-v3" / marker.builds["tool"].cache_key.removeprefix("sha256:")

    def forbidden(*args, **kwargs):
        pytest.fail("read-only status invoked compiler, audit execution or capability probes")

    monkeypatch.setattr(go_v1, "build", forbidden)
    monkeypatch.setattr(pipeline, "audit_plans", forbidden)
    monkeypatch.setattr(source_audit, "record_source_audit", forbidden)
    monkeypatch.setattr(status, "_capability_evidence", forbidden)
    return project, source, marker_path, entry


# A complete input change is drift even when its new key has no artifact;
# same-input missing/corrupt/untrusted evidence cannot inherit that verdict.
# Each case also asserts real CLI exit/clean and all persisted bytes/modes.
@pytest.mark.parametrize("schema", [2, 3])
@pytest.mark.parametrize("change,expected,code,fragment", [
    ("toolchain", "build-input-drift", 1, "toolchain"),
    ("target", "build-input-drift", 1, "target"),
    ("current", "up-to-date", 0, "up-to-date"),
    ("missing", "error", 1, "miss:"),
    ("corrupt", "error", 1, "corrupt:"),
    ("untrusted", "error", 1, "untrusted-provenance:"),
    ("marker", "error", 1, "marker"),
    ("member", "error", 1, "identity differs"),
    ("descriptor", "source-changed", 1, "changed since"),
    ("source", "source-changed", 1, "changed since"),
    ("build-source", "source-changed", 1, "changed since"),
])
def test_source_build_status_read_only(monkeypatch, tmp_path, csk_home, skills_root, capsys,
                                      schema, change, expected, code, fragment):
    project, source, marker_path, entry = _fixture(monkeypatch, tmp_path, csk_home, skills_root, schema)
    if change in {"toolchain", "target"}:
        _observe(monkeypatch, changed=change == "toolchain", change_target=change == "target")
    elif change == "missing":
        # Owned fixture only; make the protected entry absent without a live repair.
        for p in entry.rglob("*"):
            if p.is_dir(): p.chmod(0o700)
        entry.chmod(0o700)
        shutil.rmtree(entry)
    elif change == "corrupt":
        artifact = entry / "bin" / ("tool.exe" if os.name == "nt" else "tool")
        artifact.chmod(0o700)
        artifact.write_bytes(b"corrupt")
        artifact.chmod(0o500)
    elif change == "untrusted":
        if os.name == "posix":
            entry.chmod(0o777)
        else:
            # Windows status dispatch control, not a claim about native ACLs.
            real_factory = cache.cache_for_manager_home
            def factory(home):
                backend = real_factory(home)
                original = backend.inspect
                def inspect(expectation):
                    hit = original(expectation)
                    assert hit.status is cache.CacheEntryStatus.HIT
                    return cache.CacheInspection(cache.CacheEntryStatus.UNTRUSTED_PROVENANCE,
                                                 "controlled protection refusal")
                monkeypatch.setattr(backend, "inspect", inspect)
                return backend
            monkeypatch.setattr(cache, "cache_for_manager_home", factory)
    elif change == "marker":
        marker_path.write_text('{"schema_version":99}')
    elif change == "member":
        marker = json.loads(marker_path.read_bytes())
        marker["name"] = "another"
        marker_path.write_bytes(install_marker.serialize_install_marker(marker))
        _observe(monkeypatch, changed=True)
    elif change == "descriptor":
        (source / "agent-skill.json").write_text("{not JSON")
        _observe(monkeypatch, changed=True)
    elif change == "source":
        with (source / "SKILL.md").open("a") as f: f.write("changed\n")
    elif change == "build-source":
        with (source / "build/cmd/tool/main.go").open("a") as f: f.write("// changed\n")
    before = _state(project, csk_home, source)
    try:
        actual = cli.main(["status", "--check", "--json"])
        output = capsys.readouterr()
        payload = json.loads(output.out)[0]
        assert _state(project, csk_home, source) == before
        assert actual == code, output
        assert payload["clean"] is (code == 0), payload
        row = payload["skills"][0]
        assert row["label"] == expected, payload
        assert fragment in row["detail"], payload
        if expected == "build-input-drift":
            assert "missing from the cache" not in row["detail"]
        print(json.dumps({"case": change, "schema": schema, "pid": os.getpid(),
                          "exit": actual, "status": payload, "state_unchanged": True,
                          "watched_entries": len(before), "executable": sys.executable,
                          "origin": publish.__file__,
                          "source_sha256": hashlib.sha256(Path(publish.__file__).read_bytes()).hexdigest()}))
    finally:
        if change == "untrusted" and os.name == "posix": entry.chmod(0o500)
