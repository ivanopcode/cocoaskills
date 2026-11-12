from __future__ import annotations

import http.server
import subprocess
import threading
from functools import partial
from pathlib import Path

import pytest
from conftest import commit_all, init_git_repo, make_config, make_project, run, write_files, write_skillfile

from csk import cli, closure, git_ops, git_trust
from csk.dev_substitutions import Substitution


@pytest.fixture
def redirecting_repository(tmp_path, monkeypatch, skills_root):
    """Real Git and a loopback-only dumb HTTP repository; no external service."""
    source = init_git_repo(tmp_path / "source")
    write_files(source, {"SKILL.md": "---\nname: fixture-skill\n---\n\n# one\n"})
    commit_all(source)
    run(["git", "tag", "v1"], source)
    served = tmp_path / "served"
    served.mkdir()
    remote = served / "repo.git"
    run(["git", "clone", "--bare", str(source), str(remote)], tmp_path)
    repo = skills_root / "fixture-skill"
    run(["git", "clone", str(remote), str(repo)], tmp_path)
    old_url = "https://example.test/repo"
    declared_url = old_url + ".git"
    run(["git", "remote", "set-url", "origin", old_url], repo)
    write_files(source, {"SKILL.md": "---\nname: fixture-skill\n---\n\n# two\n"})
    commit = commit_all(source)
    run(["git", "tag", "v2"], source)
    run(["git", "push", str(remote), "main", "--tags"], source)
    run(["git", "update-server-info"], remote)
    requests = []

    class Handler(http.server.SimpleHTTPRequestHandler):
        def do_GET(self):
            requests.append(self.path)
            if self.path.startswith("/repo/"):
                self.send_response(301)
                self.send_header("Location", self.path.replace("/repo/", "/repo.git/", 1))
                self.send_header("Content-Length", "0")
                self.end_headers()
            else:
                super().do_GET()

        def log_message(self, *args):
            pass

    server = http.server.HTTPServer(("127.0.0.1", 0), partial(Handler, directory=str(served)))
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    gitconfig = tmp_path / "gitconfig"
    gitconfig.write_text(
        f'[url "http://127.0.0.1:{server.server_port}/"]\n insteadOf = https://example.test/\n'
    )
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(gitconfig))
    try:
        yield repo, old_url, declared_url, commit, requests
    finally:
        server.shutdown()
        thread.join(timeout=5)
        server.server_close()
        assert not thread.is_alive()


def test_upgrade_reconciles_declared_url_before_fetch(
    redirecting_repository, tmp_path, csk_home, skills_root, monkeypatch, capsys,
):
    # Upgrade fetches the corrected Skillfile URL, gets the new tag, and persists
    # origin; the old endpoint really returns 301 and redirects remain refused.
    repo, old_url, declared_url, commit, requests = redirecting_repository
    with pytest.raises(git_ops.GitError, match="301"):
        git_ops.fetch_repo(repo)
    assert requests == ["/repo/info/refs?service=git-upload-pack"]
    requests.clear()
    project = make_project(tmp_path)
    write_skillfile(project, {"schema_version": 1, "skills": [
        {"name": "fixture-skill", "git": declared_url, "tag": "v2"},
    ]})
    monkeypatch.setattr(cli.config, "load_config", lambda: make_config(csk_home, skills_root, project))
    result = cli.main(["upgrade", "app", "--strict-tags"])
    output = capsys.readouterr()
    assert result == cli.EXIT_OK, output.err
    assert requests and all(path.startswith("/repo.git/") for path in requests)
    assert run(["git", "config", "--get", "remote.origin.url"], repo).stdout.strip() == declared_url
    assert git_ops.resolve_ref(repo, "tag", "v2").commit == commit
    assert (project / ".agents/skills/fixture-skill/SKILL.md").read_text().endswith("# two\n")


@pytest.mark.parametrize("lane", ["global-update", "dev-substitution"])
def test_other_stored_remote_callers_reconcile(
    redirecting_repository, tmp_path, csk_home, skills_root, monkeypatch, capsys, lane,
):
    # Global update and cached dev substitutions both fetch their own declared
    # URL, persist it, and obtain a tag unavailable in the old clone.
    repo, _, declared_url, commit, requests = redirecting_repository
    project = make_project(tmp_path)
    cfg = make_config(csk_home, skills_root, project)
    monkeypatch.setattr(cli.config, "load_config", lambda: cfg)
    if lane == "global-update":
        root = csk_home / "global"
        root.mkdir()
        write_skillfile(root, {"schema_version": 1, "skills": [
            {"name": "fixture-skill", "git": declared_url, "tag": "v2"},
        ]})
        assert cli.main(["global", "update"]) == cli.EXIT_OK, capsys.readouterr().err
    else:
        destination = csk_home / "dev/fixture-skill"
        destination.parent.mkdir()
        repo.rename(destination)
        repo = closure._ensure_dev_repo(
            cfg, "fixture-skill", Substitution("fixture-skill", git=declared_url),
            use_cache=True, stack=None, read_only=False,
        )
        assert repo == destination
    assert requests and all(path.startswith("/repo.git/") for path in requests)
    assert run(["git", "config", "--get", "remote.origin.url"], repo).stdout.strip() == declared_url
    assert git_ops.resolve_ref(repo, "tag", "v2").commit == commit


def _repo_bytes(repo):
    return {path.relative_to(repo): path.read_bytes() for path in repo.rglob("*") if path.is_file()}


@pytest.mark.parametrize(("stored", "declared"), [
    ("https://example.test/repo", "https://other.test/repo.git"),
    ("https://example.test/repo", "https://example.test/other.git"),
    ("git@example.test:Repo", "ssh://git@example.test/repo.git"),
    ("https://example.test/repo", "file:///other/repo.git"),
    ("/local/one", "/local/two"),
    ("https://USER_CANARY:PASS_CANARY@example.test/repo?unknown=QUERY_CANARY#FRAGMENT_CANARY",
     "https://example.test/repo.git"),
    ("https://example.test/repo",
     "https://USER_CANARY:PASS_CANARY@other.test/repo.git?token=QUERY_CANARY#FRAGMENT_CANARY"),
])
def test_identity_refusal_preserves_clone_and_redacts_urls(tmp_path, monkeypatch, stored, declared):
    # Distinct identities, case-sensitive paths, local paths and invalid
    # credential-bearing URLs refuse before traffic or any clone mutation.
    repo = init_git_repo(tmp_path / "clone")
    run(["git", "remote", "add", "origin", stored], repo)
    before = _repo_bytes(repo)
    real_run = git_ops._run

    def no_fetch(command, **kwargs):
        assert "fetch" not in command
        return real_run(command, **kwargs)

    monkeypatch.setattr(git_ops, "_run", no_fetch)
    credential_case = "CANARY" in stored + declared
    reason = "must not contain userinfo" if credential_case else "do not identify the same repository"
    with pytest.raises(git_ops.GitError, match=reason) as caught:
        git_ops.fetch_repo(repo, declared)
    message = str(caught.value)
    for secret in ("USER_CANARY", "PASS_CANARY", "QUERY_CANARY", "FRAGMENT_CANARY"):
        assert secret not in message
    if not credential_case:
        assert "stored origin URL" in message and "declared URL" in message
        assert "Correct the declaration" in message and "move the existing clone aside" in message
        assert stored in message
        assert declared.replace("ssh://git@", "ssh://***@") in message
    assert _repo_bytes(repo) == before


@pytest.mark.parametrize(("stored", "declared"), [
    ("git@example.test:repo", "git@example.test:repo.git"),
    ("ssh://git@example.test/repo", "ssh://git@example.test/repo.git"),
    ("git@example.test:repo", "https://example.test/repo.git"),
    ("https://example.test/repo.git", "https://example.test/repo"),
    ("https://example.test/repo.git", "https://example.test/repo.git"),
])
def test_same_identity_spellings_fetch_declared_target(tmp_path, stored, declared):
    # SSH/SCP, explicit transport changes, suffix removal and unchanged URLs
    # use the declaration. Native insteadOf maps each to a real offline fetch;
    # this tests URL selection, not an SSH server or authentication handshake.
    source = init_git_repo(tmp_path / "source")
    write_files(source, {"data": "fixture"})
    commit = commit_all(source)
    run(["git", "tag", "v1"], source)
    repo = init_git_repo(tmp_path / "clone")
    run(["git", "remote", "add", "origin", stored], repo)
    run(["git", "config", f"url.{source}.insteadOf", declared], repo)
    git_ops.fetch_repo(repo, declared)
    assert run(["git", "config", "--get", "remote.origin.url"], repo).stdout.strip() == declared
    assert git_ops.resolve_ref(repo, "tag", "v1").commit == commit
    assert git_ops.resolve_ref(repo, "branch", "main").commit == commit


def test_failed_declared_fetch_preserves_origin_and_hides_untrusted_output(tmp_path, monkeypatch):
    # Even a failure that echoes URL credentials and arbitrary server text
    # cannot publish those secrets or persist the proposed origin change.
    repo = init_git_repo(tmp_path / "clone")
    old_url = "https://example.test/repo"
    run(["git", "remote", "add", "origin", old_url], repo)
    before = _repo_bytes(repo)
    real_run = git_ops._run
    attempts = []
    probes = []

    def failed_fetch(command, **kwargs):
        if "ls-remote" in command and "--heads" in command:
            # The anonymous Location probe; injected so no real host is contacted.
            probes.append(command)
            return subprocess.CompletedProcess(command, 128, "", "fatal: probe refused\n")
        if "fetch" not in command:
            return real_run(command, **kwargs)
        attempts.append(command)
        return subprocess.CompletedProcess(command, 128, "", (
            "fatal: unable to access 'https://USER_CANARY:PASS_CANARY@example.test/repo.git?token=QUERY_CANARY': "
            "The requested URL returned error: 301\nremote: SERVER_CANARY\n"
        ))

    monkeypatch.setattr(git_ops, "_run", failed_fetch)
    with pytest.raises(git_ops.GitError, match="HTTP redirects are refused") as caught:
        git_ops.fetch_repo(repo, old_url + ".git")
    assert len(attempts) == 1 and attempts[0][-3] == old_url + ".git"
    assert len(probes) == 1 and probes[0][-1] == old_url + ".git"
    assert "credential.helper=" in probes[0]
    assert "CANARY" not in str(caught.value)
    assert _repo_bytes(repo) == before


def test_reconciled_fetch_pins_effective_url_trust(redirecting_repository, monkeypatch):
    # The actual rewritten endpoint receives URL-specific redirect/TLS pins
    # and a private CA snapshot, despite hostile repository HTTP settings.
    repo, _, declared_url, _, requests = redirecting_repository
    effective = git_ops.git(repo, ["ls-remote", "--get-url", "--", declared_url]).stdout.strip()
    for key, value in (("sslVerify", "false"), ("followRedirects", "true"), ("sslCAInfo", "/unchecked.pem")):
        run(["git", "config", f"http.{effective}.{key}", value], repo)
    real_run = git_ops._run
    observed = []

    def checked_fetch(command, **kwargs):
        if "fetch" in command:
            observed.append(command)
            assert command[-3] == declared_url
            assert f"http.{effective}.sslVerify=true" in command
            assert f"http.{effective}.followRedirects=false" in command
            ca = Path(kwargs["env"]["GIT_SSL_CAINFO"])
            assert ca.is_file() and ca.read_bytes()
            assert f"http.{effective}.sslCAInfo={ca}" in command
            assert kwargs["env"]["GIT_ALLOW_PROTOCOL"] == git_ops.ALLOWED_GIT_PROTOCOLS
        return real_run(command, **kwargs)

    monkeypatch.setattr(git_ops, "_run", checked_fetch)
    git_ops.fetch_repo(repo, declared_url)
    assert len(observed) == 1 and requests


def test_invalid_trust_refuses_reconcile_before_any_git(tmp_path, monkeypatch):
    # Reconciliation cannot bypass the universal trust gate or alter origin
    # when the configured CA is invalid; the valid control is the test above.
    repo = init_git_repo(tmp_path / "clone")
    run(["git", "remote", "add", "origin", "https://example.test/repo"], repo)
    before = _repo_bytes(repo)
    monkeypatch.setenv("CSK_GIT_CA_BUNDLE", str(tmp_path / "missing.pem"))

    def forbidden(*args, **kwargs):
        pytest.fail("Git started before the trust gate")

    monkeypatch.setattr(git_ops, "_run", forbidden)
    with pytest.raises(git_ops.GitError, match=git_trust.INVALID):
        git_ops.fetch_repo(repo, "https://example.test/repo.git")
    assert _repo_bytes(repo) == before


def test_declared_redirect_still_refuses_without_persisting(redirecting_repository):
    # Explicitly switching to a redirecting endpoint of the same identity
    # still fails: no redirect target is fetched and origin stays untouched.
    # The refused discovery request is repeated once, anonymously, only to
    # read Location, so the error names the canonical .git spelling.
    repo, old_url, declared_url, _, requests = redirecting_repository
    run(["git", "remote", "set-url", "origin", declared_url], repo)
    with pytest.raises(git_ops.GitError, match="HTTP redirects are refused") as caught:
        git_ops.fetch_repo(repo, old_url)
    assert requests == ["/repo/info/refs?service=git-upload-pack"] * 2
    assert f"declared URL {old_url!r} is not canonical" in str(caught.value)
    assert f"same repository at {declared_url!r}" in str(caught.value)
    assert run(["git", "config", "--get", "remote.origin.url"], repo).stdout.strip() == declared_url


def test_included_remote_cannot_report_false_persistence(tmp_path):
    # Git set-url can append a local URL while an included URL still wins.
    # Report that persistence conflict instead of claiming origin was updated;
    # the shared include remains untouched and the declared fetch is valid.
    source = init_git_repo(tmp_path / "source")
    write_files(source, {"data": "fixture"})
    commit = commit_all(source)
    repo = init_git_repo(tmp_path / "clone")
    old_url = "https://example.test/repo"
    declared = old_url + ".git"
    included = tmp_path / "remote.conf"
    original = f'[remote "origin"]\n url = {old_url}\n'
    included.write_text(original)
    run(["git", "config", "include.path", str(included)], repo)
    run(["git", "config", f"url.{source}.insteadOf", declared], repo)
    with pytest.raises(git_ops.GitError, match="stored remote URL is still ambiguous"):
        git_ops.fetch_repo(repo, declared)
    assert included.read_text() == original
    assert git_ops.resolve_ref(repo, "branch", "main").commit == commit


def test_multiple_remote_urls_refuse_before_any_mutation(tmp_path, monkeypatch):
    # Multiple stored URLs cannot silently select an identity or drop an
    # operator's extra endpoints; the single-URL controls above still fetch.
    repo = init_git_repo(tmp_path / "clone")
    run(["git", "remote", "add", "origin", "https://example.test/repo"], repo)
    run(["git", "config", "--add", "remote.origin.url", "https://other.test/repo"], repo)
    before = _repo_bytes(repo)
    real_run = git_ops._run

    def no_fetch(command, **kwargs):
        assert "fetch" not in command
        return real_run(command, **kwargs)

    monkeypatch.setattr(git_ops, "_run", no_fetch)
    with pytest.raises(git_ops.GitError, match="configure exactly one URL"):
        git_ops.fetch_repo(repo, "https://example.test/repo.git")
    assert _repo_bytes(repo) == before
