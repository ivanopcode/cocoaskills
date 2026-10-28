from __future__ import annotations

import http.server
import socket
import subprocess
import threading
from pathlib import Path

import pytest
from conftest import make_config, make_project, write_skillfile

from csk import cli, git_admission, git_ops


@pytest.fixture
def smart_http():
    """Anonymous empty-repository discovery, with bounded redirect routes."""
    requests = []

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            requests.append(self.path)
            route = self.path.split("/", 2)[1]
            if route in {"alias", "login"}:
                self.send_response(301 if route == "alias" else 302)
                self.send_header(
                    "Location",
                    "/repo.git" if route == "alias" else "/sign-in?token=LOCATION_CANARY",
                )
                body = b"redirect body STDERR_CANARY"
            elif route in {"repo.git", "direct"}:
                self.send_response(200)
                self.send_header("Content-Type", "application/x-git-upload-pack-advertisement")
                body = b"001e# service=git-upload-pack\n00000000"
            else:
                self.send_response(500)
                body = b"unexpected redirect destination"
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args):
            pass

    server = http.server.HTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}", requests
    finally:
        server.shutdown()
        thread.join(timeout=5)
        server.server_close()
        assert not thread.is_alive()


@pytest.mark.parametrize("route", ["repo.git", "direct"])
def test_clone_accepts_direct_suffixed_and_suffixless_endpoints(smart_http, tmp_path, route):
    # Real Git clones an empty smart-HTTP repository at either spelling without URL changes or retries.
    base, requests = smart_http
    url = f"{base}/{route}"
    destination = tmp_path / "clone"
    git_ops.clone_repo(url, destination)
    assert (destination / ".git").is_dir()
    assert f"url = {url}\n" in (destination / ".git/config").read_text()
    assert requests == [f"/{route}/info/refs?service=git-upload-pack"]


@pytest.mark.parametrize("route", ["alias", "login"])
@pytest.mark.parametrize("verbose", [False, True])
def test_cli_clone_redirect_refuses_with_canonical_url_guidance(
    smart_http, tmp_path, csk_home, skills_root, monkeypatch, capsys, route, verbose,
):
    # Real Git redirects fail through CLI install, without fetching Location or publishing a partial skill.
    # A fixture-only Git rewrite routes the valid public-form declaration to anonymous loopback HTTP.
    base, requests = smart_http
    gitconfig = tmp_path / "gitconfig"
    gitconfig.write_text(f'[url "{base}/"]\n insteadOf = https://example.test/\n')
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(gitconfig))
    project = make_project(tmp_path)
    write_skillfile(project, {
        "schema_version": 1,
        "skills": [{"name": "fixture-skill", "git": f"https://example.test/{route}", "tag": "v1"}],
    })
    original = {
        path.relative_to(project): path.read_bytes()
        for path in project.rglob("*")
        if path.is_file() and ".git" not in path.relative_to(project).parts
    }
    cfg = make_config(csk_home, skills_root, project)
    monkeypatch.setattr(cli.config, "load_config", lambda: cfg)
    arguments = ["install", "app", *(["--verbose"] if verbose else [])]
    assert cli.main(arguments) == cli.EXIT_PARTIAL_FAIL
    output = capsys.readouterr()
    assert "HTTP redirects are refused" in output.err
    assert "canonical Git clone URL" in output.err
    assert "Clone UI or API" in output.err
    assert ".git is provider-specific" in output.err
    assert "login or browser" in output.err
    assert "LOCATION_CANARY" not in output.out + output.err
    assert "STDERR_CANARY" not in output.out + output.err
    assert requests == [f"/{route}/info/refs?service=git-upload-pack"]
    assert not (skills_root / "fixture-skill").exists()
    assert not (project / ".agents").exists()
    assert not (csk_home / "runtime/fixture-skill").exists()
    assert {
        path.relative_to(project): path.read_bytes()
        for path in project.rglob("*")
        if path.is_file() and ".git" not in path.relative_to(project).parts
    } == original


@pytest.mark.parametrize("verbose", [False, True])
def test_cli_redirect_diagnostic_does_not_echo_git_secrets(
    tmp_path, csk_home, skills_root, monkeypatch, capsys, verbose,
):
    # An injected Git failure exercises public CLI rendering and cleanup; no secret URL is contacted.
    project = make_project(tmp_path)
    write_skillfile(project, {"schema_version": 1, "skills": [
        {"name": "fixture-skill", "git": "https://example.test/alias", "tag": "v1"},
    ]})
    monkeypatch.setattr(cli.config, "load_config", lambda: make_config(csk_home, skills_root, project))
    real_run = git_ops._run
    clones = []

    def fail_clone(command, **kwargs):
        if "clone" not in command:
            return real_run(command, **kwargs)
        clones.append(command)
        Path(command[-1]).mkdir()
        return subprocess.CompletedProcess(command, 128, "", (
            "fatal: unable to access 'https://USER_CANARY:PASS_CANARY@example.test/alias?token=QUERY_CANARY': "
            "The requested URL returned error: 301\n"
            "Location: https://example.test/login?token=LOCATION_CANARY\n"
            "remote: STDERR_CANARY\n"
        ))

    monkeypatch.setattr(git_ops, "_run", fail_clone)
    assert cli.main(["install", "app", *(["--verbose"] if verbose else [])]) == cli.EXIT_PARTIAL_FAIL
    output = capsys.readouterr()
    assert "canonical Git clone URL" in output.err
    for secret in ("USER_CANARY", "PASS_CANARY", "QUERY_CANARY", "LOCATION_CANARY", "STDERR_CANARY"):
        assert secret not in output.out + output.err
    assert len(clones) == 1
    assert clones[0][-2] == "https://example.test/alias"
    assert not (skills_root / "fixture-skill").exists()


@pytest.mark.parametrize(("record", "expected_class"), [
    ("fatal: Authentication failed for 'https://example.test/repo.git/'", "auth-rejected"),
    ("fatal: repository 'https://example.test/repo.git/' not found", "identity"),
    ("fatal: unable to access 'https://example.test/repo.git': Could not resolve host: example.test", "dns"),
    ("fatal: unable to access 'https://example.test/repo.git': SSL certificate problem: certificate has expired", "tls"),
    ("remote: fatal: unable to access 'https://example.test/repo.git': The requested URL returned error: 301", "unclassified"),
    ("fatal: unable to access 'https://example.test/repo.git': The requested URL returned error: 301\n"
     "fatal: Authentication failed for 'https://example.test/repo.git/'", "unclassified"),
])
def test_unrelated_clone_failure_keeps_existing_error(record, expected_class, tmp_path, monkeypatch):
    # Existing taxonomy, GitError identity/code and non-redirect text survive; forged/conflicting evidence adds no hint.
    assert git_admission._classify_git_failure_output(record) == expected_class
    real_run = git_ops._run

    def fail_clone(command, **kwargs):
        if "clone" in command:
            return subprocess.CompletedProcess(command, 128, "", record)
        return real_run(command, **kwargs)

    monkeypatch.setattr(git_ops, "_run", fail_clone)
    destination = tmp_path / "clone"
    url = "https://example.test/repo.git"
    with pytest.raises(git_ops.GitError) as caught:
        git_ops.clone_repo(url, destination)
    assert type(caught.value) is git_ops.GitError
    assert caught.value.code is None
    assert str(caught.value) == f"git clone failed for {url} -> {destination}: {record}"
    assert not destination.exists()


@pytest.mark.parametrize("suffix", [".git", ""])
def test_static_check_accepts_clone_url_forms_without_network(
    tmp_path, csk_home, skills_root, monkeypatch, capsys, suffix,
):
    # Public static check preserves both HTTPS spellings and cannot acquire sources or open sockets.
    project = make_project(tmp_path)
    write_skillfile(project, {
        "schema_version": 2,
        "sources": {"team": {"git": f"https://example.test/repo{suffix}", "tag": "v1"}},
        "skills": [{"name": "fixture-skill", "from": "team", "directory": "."}],
    })
    original = (project / "Skillfile.json").read_bytes()
    monkeypatch.setattr(cli.config, "load_config", lambda: make_config(csk_home, skills_root, project))

    def forbidden(*args, **kwargs):
        pytest.fail("static check attempted network or Git acquisition")

    real_run = subprocess.run

    def local_commands_only(command, *args, **kwargs):
        if Path(command[0]).name in {"git", "git.exe"} and any(
            operation in command for operation in ("clone", "fetch", "ls-remote")
        ):
            pytest.fail("static check attempted a Git network command")
        return real_run(command, *args, **kwargs)

    monkeypatch.setattr(socket, "create_connection", forbidden)
    monkeypatch.setattr(socket.socket, "connect", forbidden)
    monkeypatch.setattr(socket, "getaddrinfo", forbidden)
    monkeypatch.setattr(subprocess, "run", local_commands_only)
    monkeypatch.setattr(git_ops, "clone_repo", forbidden)
    monkeypatch.setattr(git_admission, "acquire_network", forbidden)
    assert cli.main(["check", "app"]) == cli.EXIT_OK
    assert "network sources planned, not acquired" in capsys.readouterr().out
    assert (project / "Skillfile.json").read_bytes() == original
    assert not (skills_root / "fixture-skill").exists()
