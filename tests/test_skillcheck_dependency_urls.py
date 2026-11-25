"""Tests for dependency and build repository URL validation in ``csk skill check``.

Covers offline validation (shared malformed/forbidden URL rules, within-manifest
``canonical_urls.SpellingIndex`` conflicts, and the offline ``--network`` hint),
``--network`` HTTP(S) redirect discovery via a real loopback HTTP server, and a
three-way agreement table across the installer, ``csk check --canonical-urls``,
and ``csk skill check``.
"""

from __future__ import annotations

import http.server
import json
import os
import socket
import sys
import threading
from dataclasses import dataclass, field
from functools import partial
from pathlib import Path
from typing import Any

import pytest
from conftest import (
    commit_all,
    init_git_repo,
    make_config,
    make_project,
    make_skill_repo,
    run,
    write_files,
    write_skillfile,
)

from csk import cli, git_ops, installer, skillcheck

OLD = "https://gitlab.test/team/{}"
NEW = "https://gitlab.test/team/{}.git"
SHA1 = "a" * 40


@dataclass
class _LoopbackGitHost:
    served: Path
    sources: Path
    requests: list[str]
    request_headers: list[dict[str, str]]
    redirect_status: dict[str, int] = field(default_factory=lambda: {"code": 301})
    canonical_status: dict[str, int | None] = field(default_factory=lambda: {"code": None})
    gitconfig: Path = Path()

    def publish(self, name: str, tag: str, files: dict[str, str] | None = None) -> str:
        source = self.sources / name
        if not source.exists():
            init_git_repo(source)
        write_files(
            source,
            {"SKILL.md": f"---\nname: {name}\n---\n\n# {name} {tag}\n", **(files or {})},
        )
        commit = commit_all(source)
        run(["git", "tag", tag], source)
        bare = self.served / "team" / f"{name}.git"
        if not bare.exists():
            run(["git", "clone", "--bare", str(source), str(bare)], self.sources)
        else:
            run(["git", "push", str(bare), "main", "--tags"], source)
        run(["git", "update-server-info"], bare)
        return commit

    def clone(self, skills_root: Path, name: str, origin: str) -> Path:
        repo = skills_root / name
        run(["git", "clone", str(self.served / "team" / f"{name}.git"), str(repo)], self.sources)
        run(["git", "remote", "set-url", "origin", origin], repo)
        return repo

    def hits(self, name: str) -> list[str]:
        return [path for path in self.requests if path.startswith((f"/team/{name}/", f"/team/{name}.git/"))]


@pytest.fixture
def loopback_host(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    served = tmp_path / "served"
    (served / "team").mkdir(parents=True)
    sources = tmp_path / "sources"
    sources.mkdir()
    requests: list[str] = []
    request_headers: list[dict[str, str]] = []
    redirect_status = {"code": 301}
    canonical_status: dict[str, int | None] = {"code": None}

    class Handler(http.server.SimpleHTTPRequestHandler):
        def do_GET(self) -> None:
            requests.append(self.path)
            request_headers.append({k.lower(): v for k, v in self.headers.items()})
            parts = self.path.split("/", 3)
            if (
                len(parts) == 4
                and parts[1] == "team"
                and not parts[2].endswith(".git")
                and (served / "team" / f"{parts[2]}.git").is_dir()
            ):
                self.send_response(redirect_status["code"])
                if 300 <= redirect_status["code"] < 400:
                    self.send_header("Location", f"/team/{parts[2]}.git/{parts[3]}")
                self.send_header("Content-Length", "0")
                self.end_headers()
            elif canonical_status["code"] is not None:
                self.send_response(canonical_status["code"])
                if canonical_status["code"] == 401:
                    self.send_header("WWW-Authenticate", 'Basic realm="test"')
                self.send_header("Content-Length", "0")
                self.end_headers()
            else:
                super().do_GET()

        def log_message(self, *args: Any) -> None:
            pass

    server = http.server.HTTPServer(("127.0.0.1", 0), partial(Handler, directory=str(served)))
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    gitconfig = tmp_path / "gitconfig"
    gitconfig.write_text(
        f'[url "http://127.0.0.1:{server.server_port}/"]\n insteadOf = https://gitlab.test/\n',
        encoding="utf-8",
    )
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(gitconfig))
    try:
        yield _LoopbackGitHost(
            served=served,
            sources=sources,
            requests=requests,
            request_headers=request_headers,
            redirect_status=redirect_status,
            canonical_status=canonical_status,
            gitconfig=gitconfig,
        )
    finally:
        server.shutdown()
        thread.join(timeout=5)
        server.server_close()
        assert not thread.is_alive()


def _closed_local_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _write_skill_with_deps(
    skill_dir: Path,
    requirements: dict[str, str] | None = None,
    *,
    build_repositories: dict[str, str] | None = None,
) -> Path:
    skill_dir.mkdir(parents=True, exist_ok=True)
    resolver_hint = (
        "Resolve .agents/bin/tool (tool.cmd on Windows), then global/bin/tool, "
        "then command -v tool or Get-Command tool.\n"
    )
    (skill_dir / "SKILL.md").write_text(
        f"---\nname: sample\ndescription: sample skill\n---\n\n{resolver_hint}",
        encoding="utf-8",
    )
    manifest_data: dict[str, Any] = {
        "schema_version": 7 if build_repositories else 4,
        "capabilities": {"exec": "none", "network": "none"},
    }
    if requirements:
        manifest_data["dependencies"] = {
            "skills": {
                name: {
                    "git": url,
                    "ref": {"kind": "tag", "value": "v1"},
                    "mode": "context",
                }
                for name, url in requirements.items()
            }
        }
    if build_repositories:
        manifest_data["build_repositories"] = {
            name: {
                "git": url,
                "locked_commit": {"object_format": "sha1", "hex": SHA1},
            }
            for name, url in build_repositories.items()
        }
        manifest_data["commands"] = {
            f"cmd-{name}": {
                "type": "build",
                "driver": "go-repository-v1",
                "repository": name,
                "target": "tool",
            }
            for name in build_repositories
        }
    (skill_dir / "agent-skill.json").write_text(json.dumps(manifest_data), encoding="utf-8")
    return skill_dir


def _setup_project_env(
    tmp_path: Path,
    csk_home: Path,
    skills_root: Path,
    monkeypatch: pytest.MonkeyPatch,
    skills: list[dict[str, Any]],
) -> Path:
    project = make_project(tmp_path, name="app")
    write_skillfile(project, {"schema_version": 1, "skills": skills})
    cfg = make_config(csk_home, skills_root, project, agents=["codex_cli"])
    monkeypatch.setattr(cli.config, "load_config", lambda: cfg)
    return project


# Offline validation refuses every malformed or forbidden dependency URL via the shared validator without network I/O or secret leakage.
@pytest.mark.parametrize(
    ("raw_url", "forbidden_secret"),
    [
        ("https://user:secret-token@gitlab.test/team/b.git", "secret-token"),
        ("https://gitlab.test:8443/team/b.git", None),
        ("https://gitlab.test/team/b.git?token=secret-q", "secret-q"),
        ("https://gitlab.test/team/b.git#secret-f", "secret-f"),
        ("https://gitlab.test/team/b%20c.git", None),
        ("https://gitlab.test/team/b\\c.git", None),
        ("ftp://gitlab.test/team/b.git", None),
        ("-uhttps://gitlab.test/team/b.git", None),
        ("https://gitlab.test/", None),
    ],
)
def test_offline_malformed_or_forbidden_dependency_urls_are_errors(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    raw_url: str,
    forbidden_secret: str | None,
) -> None:
    def _no_network(_url: str) -> git_ops.RedirectProbe:
        raise AssertionError("offline skill check must not probe the network")

    monkeypatch.setattr(git_ops, "probe_declared_url", _no_network)
    skill_dir = _write_skill_with_deps(tmp_path / "skill", {"dep": raw_url})

    issues = skillcheck.validate_skill(skill_dir)
    assert [(i.severity, i.code, i.path) for i in issues] == [
        ("error", "skill.dependency_url_invalid", "agent-skill.json")
    ]
    assert "dependencies.skills.dep.git" in issues[0].message

    rc = cli.main(["skill", "check", str(skill_dir)])
    text_out = capsys.readouterr()
    assert rc == cli.EXIT_PARTIAL_FAIL
    assert "error: skill.dependency_url_invalid agent-skill.json:" in text_out.out
    assert ": ok" not in text_out.out
    assert "csk skill check --network" not in text_out.out

    rc_json = cli.main(["skill", "check", str(skill_dir), "--json"])
    json_out = capsys.readouterr()
    assert rc_json == cli.EXIT_PARTIAL_FAIL
    payload = json.loads(json_out.out)
    assert len(payload) == 1
    assert payload[0]["severity"] == "error"
    assert payload[0]["code"] == "skill.dependency_url_invalid"
    if forbidden_secret is not None:
        assert forbidden_secret not in issues[0].message
        assert forbidden_secret not in text_out.out
        assert forbidden_secret not in json_out.out


# Offline validation accepts valid HTTPS, SSH, SCP and local URLs without heuristics and prints the --network note only for HTTP(S) in text mode.
@pytest.mark.parametrize(
    ("case", "expect_http_note"),
    [
        ("https-git", True),
        ("https-bare", True),
        ("ssh-url", False),
        ("scp", False),
        ("relative-local", False),
        ("absolute-local", False),
        ("file-url", False),
    ],
)
def test_offline_valid_dependency_urls_are_clean_and_hint_only_for_http(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    case: str,
    expect_http_note: bool,
) -> None:
    def _no_network(_url: str) -> git_ops.RedirectProbe:
        raise AssertionError("offline skill check must not probe the network")

    monkeypatch.setattr(git_ops, "probe_declared_url", _no_network)
    local_dep = tmp_path / "dep-repo"
    local_dep.mkdir()
    url_map = {
        "https-git": "https://gitlab.test/team/b.git",
        "https-bare": "https://gitlab.test/team/b",
        "ssh-url": "ssh://git@gitlab.test/team/b.git",
        "scp": "git@gitlab.test:team/b.git",
        "relative-local": "../dep-repo",
        "absolute-local": str(local_dep),
        "file-url": local_dep.as_uri(),
    }
    skill_dir = _write_skill_with_deps(tmp_path / "skill", {"dep": url_map[case]})
    other_cwd = tmp_path / "elsewhere"
    other_cwd.mkdir()
    monkeypatch.chdir(other_cwd)

    assert skillcheck.validate_skill(skill_dir) == []

    rc = cli.main(["skill", "check", str(skill_dir)])
    text_out = capsys.readouterr()
    assert rc == cli.EXIT_OK, text_out.err
    assert f"{skill_dir.resolve()}: ok" in text_out.out
    if expect_http_note:
        assert "note:" in text_out.out
        assert "csk skill check --network" in text_out.out
    else:
        assert "note:" not in text_out.out
        assert "csk skill check --network" not in text_out.out

    rc_json = cli.main(["skill", "check", str(skill_dir), "--json"])
    json_out = capsys.readouterr()
    assert rc_json == cli.EXIT_OK
    assert json.loads(json_out.out) == []
    assert "note:" not in json_out.out


# Offline validation flags conflicting spellings of one repository identity among dependencies.skills.*.git via SpellingIndex and never conflicts with build_repositories.
@pytest.mark.parametrize(
    "case",
    [
        "deps-bare-vs-git",
        "deps-https-vs-ssh",
        "dep-vs-build-repo",
        "dep-ssh-vs-build-https",
        "build-repos-different-spellings",
        "local-path-vs-file-uri",
        "same-spelling-ok",
        "same-relative-local-ok",
    ],
)
def test_offline_conflicting_spellings_within_manifest_are_errors(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    case: str,
) -> None:
    local_dep = tmp_path / "local-dep"
    local_dep.mkdir()
    if case == "deps-bare-vs-git":
        skill_dir = _write_skill_with_deps(
            tmp_path / "skill",
            {"dep-a": "https://gitlab.test/team/b", "dep-b": "https://gitlab.test/team/b.git"},
        )
        expect_conflict = ("dependencies.skills.dep-a.git", "dependencies.skills.dep-b.git")
    elif case == "deps-https-vs-ssh":
        skill_dir = _write_skill_with_deps(
            tmp_path / "skill",
            {"dep-a": "https://gitlab.test/team/b.git", "dep-b": "git@gitlab.test:team/b.git"},
        )
        expect_conflict = ("dependencies.skills.dep-a.git", "dependencies.skills.dep-b.git")
    elif case == "dep-vs-build-repo":
        skill_dir = _write_skill_with_deps(
            tmp_path / "skill",
            {"dep-a": "https://gitlab.test/team/tool"},
            build_repositories={"tool-src": "https://gitlab.test/team/tool.git"},
        )
        expect_conflict = None
    elif case == "dep-ssh-vs-build-https":
        skill_dir = _write_skill_with_deps(
            tmp_path / "skill",
            {"dep-a": "ssh://git@gitlab.test/team/tool.git"},
            build_repositories={"tool-src": "https://gitlab.test/team/tool.git"},
        )
        expect_conflict = None
    elif case == "build-repos-different-spellings":
        skill_dir = _write_skill_with_deps(
            tmp_path / "skill",
            build_repositories={
                "tool-a": "https://gitlab.test/team/tool",
                "tool-b": "ssh://git@gitlab.test/team/tool.git",
            },
        )
        expect_conflict = None
    elif case == "local-path-vs-file-uri":
        skill_dir = _write_skill_with_deps(
            tmp_path / "skill",
            {"dep-a": str(local_dep), "dep-b": local_dep.as_uri()},
        )
        expect_conflict = ("dependencies.skills.dep-a.git", "dependencies.skills.dep-b.git")
    elif case == "same-spelling-ok":
        skill_dir = _write_skill_with_deps(
            tmp_path / "skill",
            {"dep-a": "https://gitlab.test/team/b.git", "dep-b": "https://gitlab.test/team/b.git"},
        )
        expect_conflict = None
    else:
        skill_dir = _write_skill_with_deps(
            tmp_path / "skill",
            {"dep-a": "../local-dep", "dep-b": "./../local-dep"},
        )
        expect_conflict = None

    issues = skillcheck.validate_skill(skill_dir)
    rc = cli.main(["skill", "check", str(skill_dir), "--json"])
    payload = json.loads(capsys.readouterr().out)
    if expect_conflict is None:
        assert issues == []
        assert rc == cli.EXIT_OK
        assert payload == []
        return

    assert rc == cli.EXIT_PARTIAL_FAIL
    assert [(i.severity, i.code, i.path) for i in issues] == [
        ("error", "skill.dependency_url_conflict", "agent-skill.json")
    ]
    assert "declared with 2 different URL spellings" in issues[0].message
    for field_name in expect_conflict:
        assert field_name in issues[0].message
    assert payload[0]["code"] == "skill.dependency_url_conflict"


# Offline validation refuses invalid build_repositories URLs and emits the --network note for valid HTTPS build_repositories.
def test_offline_build_repository_invalid_and_valid_controls(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    bad = _write_skill_with_deps(
        tmp_path / "bad",
        build_repositories={"tool": "https://user:pass@gitlab.test/team/tool.git"},
    )
    rc_bad = cli.main(["skill", "check", str(bad)])
    out_bad = capsys.readouterr()
    assert rc_bad == cli.EXIT_PARTIAL_FAIL
    assert "skill.spec_invalid" in out_bad.out
    assert "csk skill check --network" not in out_bad.out

    good = _write_skill_with_deps(
        tmp_path / "good",
        build_repositories={"tool": "https://gitlab.test/team/tool.git"},
    )
    rc_good = cli.main(["skill", "check", str(good)])
    out_good = capsys.readouterr()
    assert rc_good == cli.EXIT_OK
    assert f"{good.resolve()}: ok" in out_good.out
    assert "note:" in out_good.out
    assert "csk skill check --network" in out_good.out


# --network reports a 301 redirect as an error naming the field and canonical URL and passes a 200 canonical URL for both dependencies and build_repositories.
@pytest.mark.parametrize("surface", ["dependencies.skills", "build_repositories"])
@pytest.mark.parametrize("spelling", [OLD, NEW])
def test_network_redirect_and_ok_for_dependency_and_build_repository(
    loopback_host: _LoopbackGitHost,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    surface: str,
    spelling: str,
) -> None:
    loopback_host.publish("b", "v1")
    url = spelling.format("b")
    if surface == "dependencies.skills":
        skill_dir = _write_skill_with_deps(tmp_path / "skill", {"dep-b": url})
        expected_field = "dependencies.skills.dep-b.git"
    else:
        skill_dir = _write_skill_with_deps(tmp_path / "skill", build_repositories={"tool-b": url})
        expected_field = "build_repositories.tool-b.git"

    rc = cli.main(["skill", "check", "--network", str(skill_dir)])
    text_out = capsys.readouterr()
    assert "note:" not in text_out.out

    if spelling is NEW:
        assert rc == cli.EXIT_OK, text_out.err
        assert f"{skill_dir.resolve()}: ok" in text_out.out
        assert loopback_host.hits("b")[0] == "/team/b.git/info/refs?service=git-upload-pack"
        rc_json = cli.main(["skill", "check", "--network", str(skill_dir), "--json"])
        assert rc_json == cli.EXIT_OK
        assert json.loads(capsys.readouterr().out) == []
        return

    assert rc == cli.EXIT_PARTIAL_FAIL
    assert ": ok" not in text_out.out
    assert "error: skill.dependency_url_redirect agent-skill.json:" in text_out.out
    assert expected_field in text_out.out
    assert f"declared URL {OLD.format('b')!r} is not canonical" in text_out.out
    assert f"same repository at {NEW.format('b')!r}" in text_out.out
    assert loopback_host.hits("b") == ["/team/b/info/refs?service=git-upload-pack"]

    rc_json = cli.main(["skill", "check", "--network", str(skill_dir), "--json"])
    payload = json.loads(capsys.readouterr().out)
    assert rc_json == cli.EXIT_PARTIAL_FAIL
    assert len(payload) == 1
    assert payload[0]["severity"] == "error"
    assert payload[0]["code"] == "skill.dependency_url_redirect"
    assert payload[0]["path"] == "agent-skill.json"
    assert expected_field in payload[0]["message"]
    assert NEW.format("b") in payload[0]["message"]


# --network reports connection refused, HTTP 500 and HTTP 401 as warning skill.dependency_url_unverified (exit 0, never ': ok', never redirect).
@pytest.mark.parametrize("failure_mode", ["connection-refused", "http-500", "http-401"])
def test_network_unreachable_and_unverified_are_warnings_never_ok_or_redirect(
    loopback_host: _LoopbackGitHost,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    failure_mode: str,
) -> None:
    loopback_host.publish("b", "v1")
    url = NEW.format("b")
    if failure_mode == "connection-refused":
        closed_port = _closed_local_port()
        dead_cfg = tmp_path / "dead-gitconfig"
        dead_cfg.write_text(
            f'[url "http://127.0.0.1:{closed_port}/"]\n insteadOf = https://unreachable.test/\n',
            encoding="utf-8",
        )
        monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(dead_cfg))
        url = "https://unreachable.test/team/b.git"
    elif failure_mode == "http-500":
        loopback_host.canonical_status["code"] = 500
    else:
        loopback_host.canonical_status["code"] = 401

    skill_dir = _write_skill_with_deps(tmp_path / "skill", {"dep-b": url})

    rc = cli.main(["skill", "check", "--network", str(skill_dir)])
    text_out = capsys.readouterr()
    assert rc == cli.EXIT_OK, text_out.err
    assert ": ok" not in text_out.out
    assert "warning: skill.dependency_url_unverified agent-skill.json:" in text_out.out
    assert "dependencies.skills.dep-b.git" in text_out.out
    assert f"not verified {url!r}:" in text_out.out
    assert "skill.dependency_url_redirect" not in text_out.out
    assert "not canonical" not in text_out.out

    rc_json = cli.main(["skill", "check", "--network", str(skill_dir), "--json"])
    payload = json.loads(capsys.readouterr().out)
    assert rc_json == cli.EXIT_OK
    assert len(payload) == 1
    assert payload[0]["severity"] == "warning"
    assert payload[0]["code"] == "skill.dependency_url_unverified"
    assert payload[0]["path"] == "agent-skill.json"
    assert "dependencies.skills.dep-b.git" in payload[0]["message"]


# --network deduplicates identical HTTP(S) URLs to one request, names all fields, and never probes SSH or local dependencies.
def test_network_deduplicates_http_urls_and_skips_non_http(
    loopback_host: _LoopbackGitHost,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    loopback_host.publish("b", "v1")
    skill_dir = _write_skill_with_deps(
        tmp_path / "skill",
        {
            "dep-a": OLD.format("b"),
            "dep-b": OLD.format("b"),
            "dep-ssh": "ssh://git@gitlab.test/team/other.git",
            "dep-local": "../local-dep",
        },
    )

    rc = cli.main(["skill", "check", "--network", str(skill_dir), "--json"])
    payload = json.loads(capsys.readouterr().out)
    assert rc == cli.EXIT_PARTIAL_FAIL
    assert loopback_host.requests == ["/team/b/info/refs?service=git-upload-pack"]
    assert len(payload) == 1
    assert payload[0]["code"] == "skill.dependency_url_redirect"
    assert "dependencies.skills.dep-a.git, dependencies.skills.dep-b.git" in payload[0]["message"]


# --network discovery requests strip credential helpers, prompts, extra headers and cookies, matching csk check --canonical-urls --network.
def test_network_probe_omits_credentials_extra_headers_and_cookies(
    loopback_host: _LoopbackGitHost,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    loopback_host.publish("b", "v1")
    helper_marker = tmp_path / "helper-ran"
    helper_script = tmp_path / "mark_helper.py"
    helper_script.write_text(
        f"from pathlib import Path\nPath({helper_marker.as_posix()!r}).write_text('ran', encoding='utf-8')\n",
        encoding="utf-8",
    )
    cookie_file = tmp_path / "cookies.txt"
    cookie_file.write_text("127.0.0.1\tFALSE\t/\tFALSE\t2147483647\tsession\tsecret-cookie\n", encoding="utf-8")
    # Forward slashes inside double quotes keep git config valid on Windows,
    # where raw backslashes trigger Git's config escape parser.
    py_posix = Path(sys.executable).as_posix()
    script_posix = helper_script.as_posix()
    cookie_posix = cookie_file.as_posix()
    helper_cmd = f'!"{py_posix}" "{script_posix}"'
    escaped_helper = helper_cmd.replace('"', '\\"')
    with open(loopback_host.gitconfig, "a", encoding="utf-8") as cfg_file:
        cfg_file.write(
            f'[credential]\n helper = "{escaped_helper}"\n'
            '[http]\n extraHeader = "X-Secret: should-not-send"\n'
            f' cookieFile = "{cookie_posix}"\n'
        )
    # The fixture is effective only if Git can parse every appended setting.
    assert (
        run(["git", "config", "--file", str(loopback_host.gitconfig), "--get", "http.cookieFile"], tmp_path)
        .stdout.strip()
        == cookie_posix
    )
    assert (
        run(["git", "config", "--file", str(loopback_host.gitconfig), "--get", "credential.helper"], tmp_path)
        .stdout.strip()
        == helper_cmd
    )
    skill_dir = _write_skill_with_deps(tmp_path / "skill", {"dep-b": OLD.format("b")})

    rc = cli.main(["skill", "check", "--network", str(skill_dir)])
    capsys.readouterr()
    assert rc == cli.EXIT_PARTIAL_FAIL
    assert not helper_marker.exists()
    assert len(loopback_host.request_headers) == 1
    headers = loopback_host.request_headers[0]
    assert "authorization" not in headers
    assert "cookie" not in headers
    assert "x-secret" not in headers


# --network re-raises typed Git trust refusals (exit 2) while converting untyped GitError failures to unverified warnings (exit 0).
@pytest.mark.parametrize("typed", [True, False])
def test_network_handles_typed_and_untyped_git_errors(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    typed: bool,
) -> None:
    def _failing_probe(_url: str) -> git_ops.RedirectProbe:
        if typed:
            err = git_ops.GitError("trust anchor refused")
            err.code = "git_trust_refused"
            raise err
        raise git_ops.GitError("transient transport error")

    monkeypatch.setattr(git_ops, "probe_declared_url", _failing_probe)
    skill_dir = _write_skill_with_deps(tmp_path / "skill", {"dep-b": NEW.format("b")})

    rc = cli.main(["skill", "check", "--network", str(skill_dir)])
    out = capsys.readouterr()
    if typed:
        assert rc == cli.EXIT_CONFIG
        assert "trust anchor refused" in out.err
    else:
        assert rc == cli.EXIT_OK
        assert "warning: skill.dependency_url_unverified" in out.out
        assert ": ok" not in out.out


# git_ops.declared_fetch_url keeps base behavior for file:// URLs so installs never gain a new refusal.
def test_declared_fetch_url_preserves_base_file_otherhost_behavior() -> None:
    assert git_ops.declared_fetch_url("file://otherhost/abs/path") == "file://otherhost/abs/path"


# A dependency and a build repository spelling one repository differently stay clean in skill check, installer and csk check --canonical-urls.
@pytest.mark.parametrize(
    ("dep_url", "build_url"),
    [
        (NEW.format("b"), OLD.format("b")),
        ("ssh://git@gitlab.test/team/b.git", NEW.format("b")),
    ],
    ids=["dotgit-vs-bare", "ssh-vs-https"],
)
def test_dependency_vs_build_repo_different_spelling_agrees_across_install_check_and_skill_check(
    loopback_host: _LoopbackGitHost,
    tmp_path: Path,
    csk_home: Path,
    skills_root: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    dep_url: str,
    build_url: str,
) -> None:
    loopback_host.publish("b", "v1")
    loopback_host.clone(skills_root, "b", dep_url)

    consumer_manifest = {
        "schema_version": 7,
        "capabilities": {"exec": "none", "network": "none"},
        "dependencies": {
            "skills": {
                "b": {"git": dep_url, "ref": {"kind": "tag", "value": "v1"}, "mode": "context"}
            }
        },
        "build_repositories": {
            "tools-src": {
                "git": build_url,
                "locked_commit": {"object_format": "sha1", "hex": SHA1},
            }
        },
        "commands": {
            "cmd-tools-src": {
                "type": "build",
                "driver": "go-repository-v1",
                "repository": "tools-src",
                "target": "tool",
            }
        },
    }
    resolver_hint = (
        "---\nname: consumer\ndescription: consumer skill\n---\n\n"
        "Resolve .agents/bin/tool (tool.cmd on Windows), then global/bin/tool, "
        "then command -v tool or Get-Command tool.\n"
    )
    consumer_repo, _ = make_skill_repo(
        skills_root,
        "consumer",
        {"SKILL.md": resolver_hint, "agent-skill.json": json.dumps(consumer_manifest)},
        tag="v1",
    )
    # Require consumer in context mode from a wrapper skill so real csk install runs full
    # closure resolution and installer._validate_skills without building the external repo.
    wrapper_manifest = {
        "schema_version": 4,
        "capabilities": {"exec": "none", "network": "none"},
        "dependencies": {
            "skills": {
                "consumer": {
                    "git": str(consumer_repo),
                    "ref": {"kind": "tag", "value": "v1"},
                    "mode": "context",
                }
            }
        },
    }
    make_skill_repo(
        skills_root,
        "wrapper",
        {"agent-skill.json": json.dumps(wrapper_manifest)},
        tag="v1",
    )
    _setup_project_env(
        tmp_path,
        csk_home,
        skills_root,
        monkeypatch,
        [{"name": "wrapper", "tag": "v1"}],
    )

    # 1. csk skill check (offline): no conflict issue, exit 0.
    rc_skill = cli.main(["skill", "check", str(consumer_repo)])
    out_skill = capsys.readouterr()
    assert rc_skill == cli.EXIT_OK, out_skill.out + out_skill.err
    assert f"{consumer_repo.resolve()}: ok" in out_skill.out

    # 2. installer._validate_skills (legacy and global path): zero validation errors.
    from types import SimpleNamespace

    install_issues = installer._validate_skills([SimpleNamespace(snapshot=consumer_repo)], None)  # type: ignore[list-item]
    assert [i for _p, i in install_issues if i.severity == "error"] == []

    # 3. real csk install (positive control, unchanged against base): exit 0.
    rc_install = cli.main(["install", "app"])
    out_install = capsys.readouterr()
    assert rc_install == cli.EXIT_OK, out_install.out + out_install.err

    # 4. csk check --canonical-urls: unchanged, exit 0.
    rc_check = cli.main(["check", "--canonical-urls", "app"])
    out_check = capsys.readouterr()
    assert rc_check == cli.EXIT_OK, out_check.out + out_check.err


# Installer, csk check --canonical-urls and csk skill check reach the same verdict across shared valid, malformed, conflicting, redirect and unverified cases.
@pytest.mark.parametrize(
    "case",
    [
        "canonical-https-git",
        "bare-https-offline",
        "bare-https-redirect",
        "conflicting-spellings",
        "conflicting-ssh-vs-https",
        "malformed-userinfo",
        "malformed-port",
        "malformed-query",
        "malformed-scheme",
        "valid-local-path",
        "valid-ssh-scp",
        "network-unverified",
    ],
)
def test_installer_check_canonical_urls_and_skill_check_agree_on_shared_cases(
    loopback_host: _LoopbackGitHost,
    tmp_path: Path,
    csk_home: Path,
    skills_root: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    case: str,
) -> None:
    loopback_host.publish("b", "v1")
    local_repo, _ = make_skill_repo(tmp_path / "local-remotes", "local-dep", tag="v1")

    network_mode = case in {"canonical-https-git", "bare-https-redirect", "network-unverified"}
    expected_outcome: str  # "accept" | "refuse" | "unverified"
    expected_phrase: str | None = None
    forbidden_secret: str | None = None

    if case == "canonical-https-git":
        deps = {"b": NEW.format("b")}
        project_skills = [{"name": "b", "git": NEW.format("b"), "tag": "v1"}]
        expected_outcome = "accept"
    elif case == "bare-https-offline":
        # Cached clone already exists, so offline install, offline check and offline skill check all accept.
        loopback_host.clone(skills_root, "b", OLD.format("b"))
        deps = {"b": OLD.format("b")}
        project_skills = [{"name": "b", "git": OLD.format("b"), "tag": "v1"}]
        expected_outcome = "accept"
    elif case == "bare-https-redirect":
        deps = {"b": OLD.format("b")}
        project_skills = [{"name": "b", "git": OLD.format("b"), "tag": "v1"}]
        expected_outcome = "refuse"
        expected_phrase = f"same repository at {NEW.format('b')!r}"
    elif case == "conflicting-spellings":
        deps = {"b": OLD.format("b"), "b-dup": NEW.format("b")}
        project_skills = [
            {"name": "b", "git": OLD.format("b"), "tag": "v1"},
            {"name": "b-dup", "source": "b-dup", "git": NEW.format("b"), "tag": "v1"},
        ]
        expected_outcome = "refuse"
        expected_phrase = "declared with 2 different URL spellings"
    elif case == "conflicting-ssh-vs-https":
        deps = {"b": NEW.format("b"), "b-ssh": "ssh://git@gitlab.test/team/b.git"}
        project_skills = [
            {"name": "b", "git": NEW.format("b"), "tag": "v1"},
            {"name": "b-ssh", "source": "b-ssh", "git": "ssh://git@gitlab.test/team/b.git", "tag": "v1"},
        ]
        expected_outcome = "refuse"
        expected_phrase = "declared with 2 different URL spellings"
    elif case == "malformed-userinfo":
        bad_url = "https://user:shared-secret@gitlab.test/team/b.git"
        deps = {"b": bad_url}
        project_skills = [{"name": "b", "git": bad_url, "tag": "v1"}]
        expected_outcome = "refuse"
        forbidden_secret = "shared-secret"
    elif case == "malformed-port":
        bad_url = "https://gitlab.test:8443/team/b.git"
        deps = {"b": bad_url}
        project_skills = [{"name": "b", "git": bad_url, "tag": "v1"}]
        expected_outcome = "refuse"
    elif case == "malformed-query":
        bad_url = "https://gitlab.test/team/b.git?token=query-secret"
        deps = {"b": bad_url}
        project_skills = [{"name": "b", "git": bad_url, "tag": "v1"}]
        expected_outcome = "refuse"
        forbidden_secret = "query-secret"
    elif case == "malformed-scheme":
        bad_url = "ftp://gitlab.test/team/b.git"
        deps = {"b": bad_url}
        project_skills = [{"name": "b", "git": bad_url, "tag": "v1"}]
        expected_outcome = "refuse"
    elif case == "valid-local-path":
        deps = {"local-dep": str(local_repo)}
        project_skills = [{"name": "local-dep", "git": str(local_repo), "tag": "v1"}]
        expected_outcome = "accept"
    elif case == "valid-ssh-scp":
        scp_url = "git@gitlab.test:team/b.git"
        loopback_host.clone(skills_root, "b", scp_url)
        deps = {"b": scp_url}
        project_skills = [{"name": "b", "git": scp_url, "tag": "v1"}]
        expected_outcome = "accept"
    else:
        assert case == "network-unverified"
        loopback_host.canonical_status["code"] = 500
        deps = {"b": NEW.format("b")}
        project_skills = [{"name": "b", "git": NEW.format("b"), "tag": "v1"}]
        expected_outcome = "unverified"

    skill_dir = _write_skill_with_deps(tmp_path / "skill-under-check", deps)
    _setup_project_env(tmp_path, csk_home, skills_root, monkeypatch, project_skills)

    # 1. csk skill check [--network]
    skill_argv = ["skill", "check", *(["--network"] if network_mode else []), str(skill_dir)]
    rc_skill = cli.main(skill_argv)
    out_skill = capsys.readouterr()
    text_skill = out_skill.out + out_skill.err

    # 2. csk check --canonical-urls [--network]
    check_argv = ["check", "--canonical-urls", *(["--network"] if network_mode else []), "app"]
    rc_check = cli.main(check_argv)
    out_check = capsys.readouterr()
    text_check = out_check.out + out_check.err

    # 3. installer (csk install app)
    rc_install = cli.main(["install", "app"])
    out_install = capsys.readouterr()
    text_install = out_install.out + out_install.err

    if forbidden_secret is not None:
        assert forbidden_secret not in text_skill
        assert forbidden_secret not in text_check
        assert forbidden_secret not in text_install

    if expected_outcome == "accept":
        assert rc_skill == cli.EXIT_OK, text_skill
        assert rc_check == cli.EXIT_OK, text_check
        assert rc_install == cli.EXIT_OK, text_install
        assert f"{skill_dir.resolve()}: ok" in out_skill.out
    elif expected_outcome == "refuse":
        assert rc_skill == cli.EXIT_PARTIAL_FAIL, text_skill
        assert rc_check == cli.EXIT_PARTIAL_FAIL, text_check
        assert rc_install != cli.EXIT_OK, text_install
        if expected_phrase is not None:
            assert expected_phrase in text_skill
            assert expected_phrase in text_check
            assert expected_phrase in text_install
    else:
        assert expected_outcome == "unverified"
        assert rc_skill == cli.EXIT_OK, text_skill
        assert rc_check == cli.EXIT_OK, text_check
        assert "warning: skill.dependency_url_unverified" in out_skill.out
        assert ": ok" not in out_skill.out
        assert "warning: not verified" in out_check.out
        assert "not canonical" not in text_skill
        assert "not canonical" not in text_check
        # The installer must download the repo and therefore refuses a 500 server.
        assert rc_install != cli.EXIT_OK
        assert "not canonical" not in text_install
