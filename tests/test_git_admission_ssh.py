"""Operator SSH surface for external build repository admission.

These tests drive the real fetch path with a stand-in ``ssh`` program so the
generated wrapper, the pinned argv, and the operator identity material are all
exercised end to end without reaching a network.
"""

from __future__ import annotations

import json
import os
import shutil
import socket
import stat
import subprocess
import sys
import tempfile
from pathlib import Path

import pytest

from git_fixture_isolation import run_fixture_git

from csk import git_admission
from csk.build_repository import LockedCommit, RepositorySource, parse_repository_source
from csk.sources import repository_policy, transport


SOURCE = "git@fixture.test:repository.git"
EXPECTED_HOST = "git@fixture.test"
EXPECTED_PATH = "repository.git"


def _git_path() -> Path:
    resolved = shutil.which("git")
    assert resolved is not None
    return Path(resolved).resolve(strict=True)


def _fixture_repository(root: Path) -> tuple[Path, str]:
    """Build a bare repository the stand-in ssh program can serve."""

    git = os.fspath(_git_path())
    work = root / "work"
    work.mkdir(parents=True)
    # Fixed author identity only: the shared runner builds the rest of the
    # child environment from its allowlist, so no ambient GIT_* variable can
    # ride along through an os.environ spread (BUG-261004-473myt rev3).
    environment = {
        "GIT_AUTHOR_NAME": "csk",
        "GIT_AUTHOR_EMAIL": "csk@fixture.test",
        "GIT_COMMITTER_NAME": "csk",
        "GIT_COMMITTER_EMAIL": "csk@fixture.test",
        "GIT_AUTHOR_DATE": "1700000000 +0000",
        "GIT_COMMITTER_DATE": "1700000000 +0000",
    }
    def run(*arguments: str, cwd: Path = work) -> str:
        # Fixture git children run through the one shared runner, which
        # scrubs ambient redirectors and attests the fixture repository
        # (BUG-261004-473myt). The pinned author identity rides along as
        # the child environment's non-redirector entries.
        return run_fixture_git(
            arguments,
            cwd=cwd,
            executable=git,
            env=environment,
            stdin=subprocess.DEVNULL,
        ).stdout

    run("init", "--quiet", "--object-format=sha1", "--initial-branch=main", ".")
    (work / "README.md").write_bytes(b"external tool\n")
    run("add", "README.md")
    run("commit", "--quiet", "-m", "initial")
    commit = run("rev-parse", "HEAD").strip()
    bare = root / "remote.git"
    run("clone", "--quiet", "--bare", os.fspath(work), os.fspath(bare), cwd=root)
    return bare, commit


def _stub_ssh(root: Path, bare: Path) -> tuple[Path, Path]:
    """Write an ssh stand-in that logs its argv and serves ``git upload-pack``."""

    if os.name == "nt":
        pytest.skip("the stand-in ssh program requires POSIX exec semantics")
    root.mkdir(parents=True, exist_ok=True)
    log = root / "ssh-argv.jsonl"
    program = root / "ssh"
    program.write_text(
        f"#!{sys.executable}\n"
        "import json, os, sys\n"
        f"open({os.fspath(log)!r}, 'a', encoding='utf-8').write("
        "json.dumps(sys.argv) + '\\n')\n"
        f"os.execv({os.fspath(_git_path())!r}, "
        f"[{os.fspath(_git_path())!r}, 'upload-pack', {os.fspath(bare)!r}])\n",
        encoding="utf-8",
    )
    program.chmod(0o700)
    return program, log


def _tool(
    ssh: Path, credentials: git_admission.OperatorSSHCredentials | None
) -> git_admission.GitTool:
    executable = _git_path()
    version = run_fixture_git(["--version"], executable=executable).stdout.strip()
    exec_path = Path(
        run_fixture_git(["--exec-path"], executable=executable).stdout.strip()
    ).resolve(strict=True)
    return git_admission.GitTool(
        executable=executable,
        exec_path=exec_path,
        allowed_versions=(version,),
        askpass=Path(sys.executable).resolve(strict=True),
        ssh=ssh,
        ssh_credentials=credentials,
    )


def _identity(root: Path) -> Path:
    root.mkdir(parents=True, exist_ok=True)
    identity = root / "identity"
    identity.write_text("csk fixture identity\n", encoding="utf-8")
    identity.chmod(0o600)
    return identity


def _known_hosts(root: Path) -> Path:
    root.mkdir(parents=True, exist_ok=True)
    known_hosts = root / "known_hosts"
    known_hosts.write_text("fixture.test ssh-ed25519 AAAA\n", encoding="utf-8")
    known_hosts.chmod(0o600)
    return known_hosts


def _agent_socket(root: Path, request: pytest.FixtureRequest) -> Path:
    # AF_UNIX paths are capped near 104 bytes on macOS, well below what a
    # pytest tmp_path spends on the test name alone.
    if os.name == "nt":
        pytest.skip("POSIX agent socket fixture; Windows pipe is tested separately")
    directory = Path(tempfile.mkdtemp(prefix="csk-agent-"))
    path = directory / "agent.sock"
    listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    listener.bind(os.fspath(path))

    def cleanup() -> None:
        listener.close()
        shutil.rmtree(directory, ignore_errors=True)

    request.addfinalizer(cleanup)
    # Operator material is admitted as its resolved target, and the macOS
    # temporary root is itself a symlink.
    return path.resolve(strict=True)


def _private_paths(root: Path) -> git_admission._PrivatePaths:
    root.mkdir(parents=True)
    return git_admission._make_private_paths(root)


def test_operator_identity_reaches_ssh_and_admits_the_locked_commit(
    tmp_path: Path,
) -> None:
    bare, commit = _fixture_repository(tmp_path / "fixture")
    ssh, log = _stub_ssh(tmp_path / "stub", bare)
    credentials = git_admission.OperatorSSHCredentials(
        identity=_identity(tmp_path / "operator"),
        known_hosts=_known_hosts(tmp_path / "operator"),
    )
    snapshot = git_admission.acquire_network(
        parse_repository_source(SOURCE),
        LockedCommit(object_format="sha1", hex=commit),
        _tool(ssh, credentials),
    )
    assert snapshot.commit == commit
    assert [item.path for item in snapshot.files] == ["README.md"]

    argv = json.loads(log.read_text(encoding="utf-8").splitlines()[0])
    assert argv[0] == os.fspath(ssh)
    assert argv[-2:] == [EXPECTED_HOST, f"git-upload-pack '{EXPECTED_PATH}'"]
    assert "-i" in argv
    assert argv[argv.index("-i") + 1] == os.fspath(credentials.identity)
    assert "IdentitiesOnly=yes" in argv
    assert "IdentityAgent=none" in argv
    assert "BatchMode=yes" in argv
    assert "StrictHostKeyChecking=yes" in argv


def test_operator_agent_pins_the_selected_identity(
    tmp_path: Path, request: pytest.FixtureRequest
) -> None:
    bare, commit = _fixture_repository(tmp_path / "fixture")
    ssh, log = _stub_ssh(tmp_path / "stub", bare)
    agent = _agent_socket(tmp_path / "operator", request)
    credentials = git_admission.OperatorSSHCredentials(
        identity=_identity(tmp_path / "operator"),
        agent_socket=agent,
        known_hosts=_known_hosts(tmp_path / "operator"),
    )
    snapshot = git_admission.acquire_network(
        parse_repository_source(SOURCE),
        LockedCommit(object_format="sha1", hex=commit),
        _tool(ssh, credentials),
    )
    assert snapshot.commit == commit

    argv = json.loads(log.read_text(encoding="utf-8").splitlines()[0])
    # Pinning both keeps a populated agent from exhausting the server's
    # MaxAuthTries budget before it offers the key that authenticates.
    assert f"IdentityAgent={agent}" in argv
    assert "IdentitiesOnly=yes" in argv
    assert argv[argv.index("-i") + 1] == os.fspath(credentials.identity)


def test_agent_only_selection_delegates_every_key_to_the_agent(
    tmp_path: Path, request: pytest.FixtureRequest
) -> None:
    bare, commit = _fixture_repository(tmp_path / "fixture")
    ssh, log = _stub_ssh(tmp_path / "stub", bare)
    agent = _agent_socket(tmp_path / "operator", request)
    credentials = git_admission.OperatorSSHCredentials(
        agent_socket=agent, known_hosts=_known_hosts(tmp_path / "operator")
    )
    git_admission.acquire_network(
        parse_repository_source(SOURCE),
        LockedCommit(object_format="sha1", hex=commit),
        _tool(ssh, credentials),
    )
    argv = json.loads(log.read_text(encoding="utf-8").splitlines()[0])
    assert f"IdentityAgent={agent}" in argv
    assert "IdentitiesOnly=no" in argv
    assert "IdentityFile=none" in argv
    assert "-i" not in argv


@pytest.mark.parametrize(
    "credentials",
    [
        None,
        git_admission.OperatorSSHCredentials(),
        git_admission.OperatorSSHCredentials(known_hosts=Path("/nonexistent")),
    ],
)
def test_credential_free_ssh_fails_closed_without_contacting_the_remote(
    tmp_path: Path, credentials: git_admission.OperatorSSHCredentials | None
) -> None:
    bare, commit = _fixture_repository(tmp_path / "fixture")
    ssh, log = _stub_ssh(tmp_path / "stub", bare)
    with pytest.raises(git_admission.GitAdmissionError) as captured:
        git_admission.acquire_network(
            parse_repository_source(SOURCE),
            LockedCommit(object_format="sha1", hex=commit),
            _tool(ssh, credentials),
        )
    assert captured.value.code == git_admission.SSH_CREDENTIAL_MISSING
    assert not log.exists()


def test_known_hosts_are_required_because_host_key_checking_is_pinned(
    tmp_path: Path,
) -> None:
    bare, commit = _fixture_repository(tmp_path / "fixture")
    ssh, log = _stub_ssh(tmp_path / "stub", bare)
    credentials = git_admission.OperatorSSHCredentials(
        identity=_identity(tmp_path / "operator")
    )
    with pytest.raises(git_admission.GitAdmissionError) as captured:
        git_admission.acquire_network(
            parse_repository_source(SOURCE),
            LockedCommit(object_format="sha1", hex=commit),
            _tool(ssh, credentials),
        )
    assert captured.value.code == git_admission.SSH_CREDENTIAL_MISSING
    assert not log.exists()


def test_generated_wrapper_refuses_any_invocation_it_was_not_pinned_to(
    tmp_path: Path,
) -> None:
    bare, commit = _fixture_repository(tmp_path / "fixture")
    ssh, log = _stub_ssh(tmp_path / "stub", bare)
    credentials = git_admission.OperatorSSHCredentials(
        identity=_identity(tmp_path / "operator"),
        known_hosts=_known_hosts(tmp_path / "operator"),
    )
    paths = _private_paths(tmp_path / "private")
    command = git_admission._materialize_ssh_wrapper(
        paths, _tool(ssh, credentials), parse_repository_source(SOURCE)
    )
    script = Path(command[1])
    assert script.lstat().st_mode & stat.S_IRWXO == 0

    for argv in (
        ("evil.test", f"git-upload-pack '{EXPECTED_PATH}'"),
        (EXPECTED_HOST, "git-upload-pack '/etc/passwd'"),
        (EXPECTED_HOST, f"git-upload-pack '{EXPECTED_PATH}'", "--extra"),
        (EXPECTED_HOST,),
    ):
        result = subprocess.run(
            (*command, *argv), capture_output=True, text=True, check=False
        )
        assert result.returncode == 1
        assert "refused unexpected ssh invocation" in result.stderr
    assert not log.exists()

    accepted = subprocess.run(
        (*command, EXPECTED_HOST, f"git-upload-pack '{EXPECTED_PATH}'"),
        stdin=subprocess.DEVNULL,
        capture_output=True,
        check=False,
    )
    assert log.exists(), accepted.stderr


def test_clean_environment_carries_the_wrapper_and_no_ambient_ssh_state(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("SSH_AUTH_SOCK", "/ambient/agent.sock")
    monkeypatch.setenv("GIT_SSH_COMMAND", "/ambient/evil-ssh")
    bare, _ = _fixture_repository(tmp_path / "fixture")
    ssh, _log = _stub_ssh(tmp_path / "stub", bare)
    credentials = git_admission.OperatorSSHCredentials(
        identity=_identity(tmp_path / "operator"),
        known_hosts=_known_hosts(tmp_path / "operator"),
    )
    tool = _tool(ssh, credentials)
    paths = _private_paths(tmp_path / "private")
    command = git_admission._materialize_ssh_wrapper(
        paths, tool, parse_repository_source(SOURCE)
    )
    environment = git_admission._clean_git_environment(
        paths, tool, "ssh", ssh_command=command
    )
    assert command[1] in environment["GIT_SSH_COMMAND"]
    assert "/ambient/evil-ssh" not in environment["GIT_SSH_COMMAND"]
    assert environment["GIT_SSH_VARIANT"] == "ssh"
    assert "SSH_AUTH_SOCK" not in environment
    assert environment["HOME"] == os.fspath(paths.home)

    # The operator's known hosts are copied, never referenced in place, so the
    # fetch cannot mutate operator state.
    copied = paths.ssh / "known_hosts"
    assert copied.read_bytes() == credentials.known_hosts.read_bytes()
    assert copied != credentials.known_hosts


@pytest.mark.parametrize(
    ("source", "host", "path"),
    [
        (SOURCE, EXPECTED_HOST, EXPECTED_PATH),
        (
            "git@gitlab.wildberries.ru:portals/agentic-infra/cli/sentry-cli.git",
            "git@gitlab.wildberries.ru",
            "portals/agentic-infra/cli/sentry-cli.git",
        ),
        (
            "ssh://git@fixture.test/portals/tool.git",
            "git@fixture.test",
            "/portals/tool.git",
        ),
        ("ssh://fixture.test/portals/tool.git", "fixture.test", "/portals/tool.git"),
    ],
)
def test_ssh_endpoint_matches_what_git_hands_the_ssh_program(
    source: str, host: str, path: str
) -> None:
    assert git_admission.ssh_endpoint(parse_repository_source(source)) == (host, path)


@pytest.mark.parametrize("port", [None, 22, 2222])
def test_network_lane_accepts_each_explicit_ssh_uri_port(
    tmp_path: Path, port: int | None
) -> None:
    bare, commit = _fixture_repository(tmp_path / "fixture")
    ssh, log = _stub_ssh(tmp_path / "stub", bare)
    credentials = git_admission.OperatorSSHCredentials(
        identity=_identity(tmp_path / "operator"),
        known_hosts=_known_hosts(tmp_path / "operator"),
    )
    authority = "git@fixture.test"
    suffix = "" if port is None else f":{port}"
    url = f"ssh://{authority}{suffix}/repository.git"
    policy = repository_policy.parse_policy(
        {
            "schema_version": 2,
            "repositories": {
                "fixture.test/repository": {
                    "endpoints": [
                        {"url": url, "authentication": "operator-ssh"}
                    ],
                    "fallback": "none",
                }
            },
        }
    )
    endpoint = repository_policy.select_endpoints(
        policy, "fixture.test/repository"
    ).endpoints[0]
    tool = _tool(ssh, credentials)
    result = git_admission.acquire_network(
        RepositorySource(url, "fixture.test/repository", "ssh"),
        LockedCommit("sha1", commit),
        tool,
        connection=transport._connection_target(endpoint),
    )
    assert result.commit == commit
    assert result.tag_verified is False
    assert len(log.read_text(encoding="utf-8").splitlines()) == 1
    logged = json.loads(log.read_text(encoding="utf-8").splitlines()[0])
    if port == 2222:
        assert "-p" in logged and "2222" in logged
    else:
        assert "-p" not in logged


def test_capture_prefers_command_line_over_environment(
    tmp_path: Path, request: pytest.FixtureRequest
) -> None:
    identity = _identity(tmp_path / "operator")
    known_hosts = _known_hosts(tmp_path / "operator")
    other = tmp_path / "operator" / "other-identity"
    other.write_text("other\n", encoding="utf-8")
    environment = {
        git_admission.OPERATOR_SSH_IDENTITY_ENV: os.fspath(other),
        git_admission.OPERATOR_SSH_KNOWN_HOSTS_ENV: os.fspath(known_hosts),
    }
    captured = git_admission.capture_operator_ssh_credentials(
        environment, identity=os.fspath(identity)
    )
    assert captured.identity == identity
    assert captured.known_hosts == known_hosts
    assert captured.selected

    from_environment = git_admission.capture_operator_ssh_credentials(environment)
    assert from_environment.identity == other


def test_capture_resolves_auto_agent_from_the_operator_environment(
    tmp_path: Path, request: pytest.FixtureRequest
) -> None:
    agent = _agent_socket(tmp_path / "operator", request)
    known_hosts = _known_hosts(tmp_path / "operator")
    captured = git_admission.capture_operator_ssh_credentials(
        {
            "SSH_AUTH_SOCK": os.fspath(agent),
            git_admission.OPERATOR_SSH_KNOWN_HOSTS_ENV: os.fspath(known_hosts),
        },
        agent=git_admission.OPERATOR_SSH_AGENT_AUTO,
    )
    assert captured.agent_socket == agent

    with pytest.raises(git_admission.GitAdmissionError) as captured_error:
        git_admission.capture_operator_ssh_credentials(
            {}, agent=git_admission.OPERATOR_SSH_AGENT_AUTO
        )
    assert captured_error.value.code == git_admission.SSH_CREDENTIAL_MISSING


def test_capture_defaults_known_hosts_to_the_operator_home(tmp_path: Path) -> None:
    home = tmp_path / "home"
    (home / ".ssh").mkdir(parents=True)
    known_hosts = home / ".ssh" / "known_hosts"
    known_hosts.write_text("fixture.test ssh-ed25519 AAAA\n", encoding="utf-8")
    captured = git_admission.capture_operator_ssh_credentials(
        {"HOME": os.fspath(home)}, identity=os.fspath(_identity(tmp_path / "operator"))
    )
    assert captured.known_hosts == known_hosts


@pytest.mark.skipif(os.name == "nt", reason="requires POSIX symlink privileges")
def test_capture_rejects_material_that_is_not_an_admitted_object(
    tmp_path: Path,
) -> None:
    known_hosts = _known_hosts(tmp_path / "operator")
    directory = tmp_path / "operator" / "directory"
    directory.mkdir()
    directory_link = tmp_path / "operator" / "directory-link"
    directory_link.symlink_to(directory)
    dangling = tmp_path / "operator" / "dangling"
    dangling.symlink_to(tmp_path / "operator" / "missing")
    for identity in (
        directory,
        directory_link,
        dangling,
        tmp_path / "operator" / "missing",
    ):
        with pytest.raises(git_admission.GitAdmissionError) as captured:
            git_admission.capture_operator_ssh_credentials(
                {}, identity=os.fspath(identity), known_hosts=os.fspath(known_hosts)
            )
        assert captured.value.code == git_admission.IDENTITY_INVALID
    with pytest.raises(git_admission.GitAdmissionError):
        git_admission.capture_operator_ssh_credentials(
            {}, identity="relative/identity", known_hosts=os.fspath(known_hosts)
        )


@pytest.mark.skipif(os.name == "nt", reason="requires POSIX symlink privileges")
def test_capture_resolves_symlinked_material_to_its_target(tmp_path: Path) -> None:
    # A live agent socket is conventionally a stable symlink onto a per-session
    # rendezvous point, so operator paths are resolved rather than refused.
    identity = _identity(tmp_path / "operator")
    known_hosts = _known_hosts(tmp_path / "operator")
    link = tmp_path / "operator" / "identity-link"
    link.symlink_to(identity)
    captured = git_admission.capture_operator_ssh_credentials(
        {}, identity=os.fspath(link), known_hosts=os.fspath(known_hosts)
    )
    assert captured.identity == identity.resolve(strict=True)


@pytest.mark.skipif(os.name != "nt", reason="Windows service pipe contract")
def test_windows_auto_agent_selects_only_the_standard_pipe(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # With no SSH_AUTH_SOCK, auto and the explicit standard pipe are valid;
    # arbitrary pipes, files and missing service endpoints must still refuse.
    pipe = Path(r"\\.\pipe\openssh-ssh-agent")
    known_hosts = _known_hosts(tmp_path)
    monkeypatch.setattr(git_admission, "_windows_ssh_agent_pipe_available", lambda: True)
    for selection in ("auto", str(pipe)):
        result = git_admission.capture_operator_ssh_credentials(
            {"SSH_AUTH_SOCK": r"\\.\pipe\untrusted"},
            agent=selection, known_hosts=str(known_hosts),
        )
        assert result.agent_socket == pipe
    for selection in (r"\\.\pipe\untrusted", str(known_hosts), "relative"):
        with pytest.raises(git_admission.GitAdmissionError) as error:
            git_admission.capture_operator_ssh_credentials(
                {}, agent=selection, known_hosts=str(known_hosts)
            )
        assert error.value.code == git_admission.IDENTITY_INVALID
        assert "not the admitted Windows pipe" in str(error.value)
    monkeypatch.setattr(git_admission, "_windows_ssh_agent_pipe_available", lambda: False)
    with pytest.raises(git_admission.GitAdmissionError) as error:
        git_admission.capture_operator_ssh_credentials({}, agent="auto")
    assert error.value.code == git_admission.IDENTITY_INVALID
    assert "SSH agent pipe is unavailable" in str(error.value)


@pytest.mark.skipif(os.name != "nt", reason="Windows named pipe API")
def test_windows_agent_pipe_probe_distinguishes_busy_from_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Only success or ERROR_SEM_TIMEOUT proves an existing endpoint;
    # missing pipes and access failures must refuse.
    import ctypes

    class Wait:
        def __call__(self, name: str, timeout: int) -> int:
            assert name == r"\\.\pipe\openssh-ssh-agent"
            assert timeout == 1
            return result

    class Kernel:
        WaitNamedPipeW = Wait()

    monkeypatch.setattr(ctypes, "WinDLL", lambda *a, **kw: Kernel())
    for result, error, expected in ((1, 0, True), (0, 121, True), (0, 2, False), (0, 5, False)):
        monkeypatch.setattr(ctypes, "get_last_error", lambda: error)
        assert git_admission._windows_ssh_agent_pipe_available() is expected


@pytest.mark.skipif(os.name != "nt", reason="Windows SSH option escaping")
def test_windows_agent_argument_survives_ssh_config_parsing(tmp_path: Path) -> None:
    # Slash spelling reaches the native pipe; raw backslashes are SSH config
    # escapes and caused real authentication failure with a loaded key.
    policy = git_admission.SSHPolicy(
        wrapper=tmp_path / "wrapper.py", ssh=tmp_path / "ssh.exe",
        expected_host=EXPECTED_HOST, repository_path=EXPECTED_PATH,
        empty_config=tmp_path / "config", known_hosts=tmp_path / "known_hosts",
        empty_known_hosts=tmp_path / "empty",
        agent_socket=Path(r"\\.\pipe\openssh-ssh-agent"),
    )
    command = git_admission.exact_ssh_command(
        policy, [str(policy.wrapper), EXPECTED_HOST, f"git-upload-pack '{EXPECTED_PATH}'"]
    )
    assert "IdentityAgent=//./pipe/openssh-ssh-agent" in command
    assert r"IdentityAgent=\\.\pipe\openssh-ssh-agent" not in command
    assert "BatchMode=yes" in command
    assert "NumberOfPasswordPrompts=0" in command
    assert "IdentitiesOnly=no" in command


@pytest.mark.parametrize("platform", ["win32", "darwin", "linux"])
@pytest.mark.parametrize("ambient", [None, "", "relative-xyz", "hostile"])
def test_clean_ssh_environment_admits_only_windows_system_data(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, platform: str, ambient: str | None
) -> None:
    # Windows SSH gets an empty private PROGRAMDATA even without a parent
    # value. Ambient absolute/relative/empty values never select trust paths;
    # HTTPS/POSIX and executable/home/agent exclusions stay unchanged.
    if ambient is None:
        monkeypatch.delenv("PROGRAMDATA", raising=False)
    else:
        monkeypatch.setenv("PROGRAMDATA", str(tmp_path / "hostile") if ambient == "hostile" else ambient)
    monkeypatch.setenv("PATH", "hostile-search-path")
    monkeypatch.setenv("USERPROFILE", "hostile-home")
    monkeypatch.setenv("SSH_AUTH_SOCK", "hostile-agent")
    monkeypatch.setenv("GIT_SSH_COMMAND", "hostile-command")
    monkeypatch.setattr(sys, "platform", platform)
    paths = _private_paths(tmp_path / "private")
    tool = git_admission.GitTool(tmp_path / "git", tmp_path / "exec", ("git version",))
    for route in ("ssh", "https"):
        environment = git_admission._clean_git_environment(paths, tool, route)
        assert environment.get("PROGRAMDATA") == (
            str(paths.ssh / "program-data") if platform == "win32" and route == "ssh" else None
        )
        if "PROGRAMDATA" in environment:
            assert Path(environment["PROGRAMDATA"]).is_dir()
            assert list(Path(environment["PROGRAMDATA"]).iterdir()) == []
        assert environment["PATH"] == str(paths.empty_path)
        assert environment["HOME"] == str(paths.home)
        for name in ("USERPROFILE", "SSH_AUTH_SOCK", "GIT_SSH_COMMAND"):
            assert name not in environment


@pytest.mark.skipif(os.name != "nt", reason="native Windows SSH startup")
@pytest.mark.parametrize("ambient", ["absent", "hostile"])
def test_windows_native_ssh_starts_with_private_program_data(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, ambient: str
) -> None:
    # Real Win32-OpenSSH starts with synthesized PROGRAMDATA, whether the
    # parent omits it or selects hostile configuration. No server is needed.
    hostile = tmp_path / "hostile"
    (hostile / "ssh").mkdir(parents=True)
    config = hostile / "ssh" / "ssh_config"
    config.write_bytes(b"ForbiddenTestOption yes\n")
    if ambient == "absent":
        monkeypatch.delenv("PROGRAMDATA", raising=False)
    else:
        monkeypatch.setenv("PROGRAMDATA", str(hostile))
    ssh = Path(os.environ["SYSTEMROOT"]) / "System32" / "OpenSSH" / "ssh.exe"
    if not ssh.is_file():
        pytest.skip("native OpenSSH client is not installed")
    paths = _private_paths(tmp_path / "private")
    tool = git_admission.GitTool(tmp_path / "git", tmp_path / "exec", ("git version",))
    environment = git_admission._clean_git_environment(paths, tool, "ssh")
    assert environment["PROGRAMDATA"] == str(paths.ssh / "program-data")
    try:
        result = subprocess.run([str(ssh), "-V"], env=environment, capture_output=True, timeout=10)
    except subprocess.TimeoutExpired:
        pytest.fail('native SSH startup fixture: ssh -V exceeded 10 seconds')
    assert result.returncode == 0, result.stderr
    assert b"OpenSSH_for_Windows" in result.stderr
    assert config.read_bytes() == b"ForbiddenTestOption yes\n"


@pytest.mark.skipif(os.name != "nt", reason="native Windows loopback SSH")
@pytest.mark.parametrize("selection", ["ed25519", "rsa", "ecdsa", "encrypted", "agent"])
def test_windows_real_git_ssh_clean_environment_matrix(
    tmp_path: Path, selection: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Opt-in disposable server kit: real Git, current guarded wrapper, native
    # ssh and full snapshot admission. Plain keys/loaded agent must succeed;
    # an encrypted file without a matching agent refuses locally before any
    # wrapper, fetch or partial repository. Server authorization cannot unlock
    # the local key. The agent case also checks the encrypted pin, then an empty
    # agent by removing only this kit's generated identity (cleanup permits it).
    # This does not qualify interactive prompts, hardware keys or real hosts.
    configured = os.environ.get("CSK_TEST_WINDOWS_SSH_ROOT")
    if not configured:
        pytest.skip("requires a disposable Windows loopback sshd kit")
    root = Path(configured)
    if selection == "agent":
        # Validate fixture ownership before even selecting the service pipe.
        # A server-only VM reproduction must never query an operator's agent.
        state = json.loads((root / 'state.json').read_text())
        assert state['kind'] == 'csk-windows-ssh-kit-v1'
        assert state['root'] == str(root) and state['agent_add_attempted']
        from csk import build_ssh
        parent = dict(os.environ)
        parent.pop("SSH_AUTH_SOCK", None)
        candidates = build_ssh.discover_candidates(environment=parent, home=str(root / "empty-home"))
        assert candidates.agent_socket == git_admission._WINDOWS_SSH_AGENT_PIPE
        assert candidates.agent_key_count and candidates.agent_key_count >= 1
    assert " " in str(root), "native kit must exercise private paths with spaces"
    # PROGRAMDATA is a startup prerequisite, not authority to import system
    # SSH config. If that config is read, this positive control cannot pass.
    system_data = tmp_path / "system-data"
    (system_data / "ssh").mkdir(parents=True)
    (system_data / "ssh" / "ssh_config").write_text("ForbiddenTestOption yes\n")
    monkeypatch.setenv("PROGRAMDATA", str(system_data))
    endpoint = json.loads((root / "endpoint.json").read_text(encoding="utf-8-sig"))
    port, user = endpoint["port"], endpoint["user"]
    assert isinstance(port, int) and 1024 <= port <= 65535
    assert isinstance(user, str) and user.isalnum()
    bare, commit = _fixture_repository(tmp_path / "fixture")
    # The kit's forced command reads this operator-controlled pointer. Neither
    # a package nor the remote chooses a local repository or executable.
    (root / "repository").write_text(bare.as_posix(), encoding="utf-8")
    identity = root / "First Last" / ".ssh" / selection
    credentials = git_admission.capture_operator_ssh_credentials(
        {}, identity=None if selection == "agent" else str(identity),
        agent="auto" if selection == "agent" else None,
        known_hosts=str(root / "known_hosts"),
    )
    ssh = Path(os.environ["SYSTEMROOT"]) / "System32" / "OpenSSH" / "ssh.exe"
    tool = _tool(ssh, credentials)
    url = f"ssh://{user}@127.0.0.1:{port}/repository.git"
    source = RepositorySource(url, "127.0.0.1/repository", "ssh")
    connection = git_admission.NetworkEndpoint(
        url, url, source.identity, "ssh", "127.0.0.1", port,
        f"{user}@127.0.0.1", "/repository.git", port, port,
    )
    private_roots: list[Path] = []
    wrappers: list[Path] = []
    operations: list[tuple[str, ...]] = []
    make_paths = git_admission._make_private_paths
    materialize = git_admission._materialize_ssh_wrapper
    run_git = git_admission._run_git
    def record_paths(path):
        private_roots.append(path)
        return make_paths(path)
    def record_wrapper(paths, *args, **kwargs):
        wrappers.append(paths.root)
        return materialize(paths, *args, **kwargs)
    def record_git(tool, paths, environment, *arguments, **kwargs):
        operations.append(arguments)
        return run_git(tool, paths, environment, *arguments, **kwargs)
    monkeypatch.setattr(git_admission, '_make_private_paths', record_paths)
    monkeypatch.setattr(git_admission, '_materialize_ssh_wrapper', record_wrapper)
    monkeypatch.setattr(git_admission, '_run_git', record_git)
    def acquire(selected_tool=tool) -> git_admission.Snapshot:
        return git_admission.acquire_network(
            source, LockedCommit("sha1", commit), selected_tool, connection=connection
        )
    def assert_local_refusal(selected_tool):
        private_roots.clear()
        wrappers.clear()
        operations.clear()
        with pytest.raises(git_admission.GitAdmissionError) as error:
            acquire(selected_tool)
        assert error.value.code == git_admission.SSH_CREDENTIAL_UNUSABLE
        assert error.value.failure_class == "auth-unavailable"
        assert source.identity in str(error.value)
        assert 'key has a passphrase and no matching agent key is available' in str(error.value)
        assert 'SSH prompts are disabled' in str(error.value)
        assert 'next: run csk config build-ssh doctor' in str(error.value)
        assert private_roots == wrappers == operations == []
    if selection == "encrypted":
        assert_local_refusal(tool)
    else:
        snapshot = acquire()
        assert snapshot.commit == commit
        assert [(f.path, f.content) for f in snapshot.files] == [("README.md", b"external tool\n")]
        assert wrappers and any('fetch' in operation for operation in operations)
        assert all(not path.exists() for path in private_roots)
        if selection == "agent":
            from dataclasses import replace
            # Only the disposable kit owns this initially empty service agent.
            # This branch must never run against an operator's existing agent.
            pinned = replace(tool, ssh_credentials=replace(credentials, identity=root / 'encrypted'))
            assert acquire(pinned).files == snapshot.files
            environment = {'LC_ALL': 'C', 'SystemRoot': os.environ['SYSTEMROOT'],
                           'PROGRAMDATA': str(tmp_path),
                           'SSH_AUTH_SOCK': str(credentials.agent_socket)}
            removed = subprocess.run([str(ssh.with_name('ssh-add.exe')), '-d', str(root / 'encrypted.pub')],
                                     env=environment, stdin=subprocess.DEVNULL, capture_output=True, timeout=10)
            assert removed.returncode == 0, removed.stderr
            assert_local_refusal(pinned)


@pytest.mark.skipif(os.name != "nt", reason="Windows Job Object descendant lifecycle")
def test_windows_git_tree_deadline(tmp_path: Path) -> None:
    # A child retaining stderr cannot extend the budget; its process handle is
    # signalled after the tree deadline. A normal command is the positive control.
    import ctypes
    import time
    from csk._windows_git_process import run
    kernel = getattr(ctypes, "WinDLL")("kernel32", use_last_error=True)
    kernel.OpenProcess.argtypes = [ctypes.c_uint32, ctypes.c_int, ctypes.c_uint32]
    kernel.OpenProcess.restype = ctypes.c_void_p
    kernel.WaitForSingleObject.argtypes = [ctypes.c_void_p, ctypes.c_uint32]
    kernel.CloseHandle.argtypes = [ctypes.c_void_p]
    common = dict(stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=True)
    assert run([sys.executable, "-c", "print('ok')"], timeout=5, **common).stdout.strip() == b"ok"
    pid_file = tmp_path / "descendant.pid"
    code = "import subprocess,sys,time; p=subprocess.Popen([sys.executable,'-c','import time;time.sleep(120)']);open(sys.argv[1],'w').write(str(p.pid));time.sleep(120)"
    started = time.monotonic()
    with pytest.raises(subprocess.TimeoutExpired):
        run([sys.executable, "-c", code, str(pid_file)], timeout=3, **common)
    assert time.monotonic() - started < 8
    handle = kernel.OpenProcess(0x100000, False, int(pid_file.read_text()))
    if handle:
        try:
            assert kernel.WaitForSingleObject(handle, 0) == 0, "descendant survived deadline"
        finally:
            kernel.CloseHandle(handle)


@pytest.mark.skipif(os.name != "nt", reason="native authenticated SSH timeout")
def test_windows_real_ssh_authenticated_stall(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # Real authentication completes, then the remote stalls 120 seconds. The
    # fetch must return timeout within budget plus bounded cleanup.
    import time
    configured = os.environ.get("CSK_TEST_WINDOWS_SSH_ROOT")
    if not configured:
        pytest.skip("requires native loopback kit")
    root = Path(configured)
    marker = root / "stall"
    original = git_admission.Limits
    monkeypatch.setattr(git_admission, "Limits", lambda: original(timeout_seconds=3))
    original_acquire = git_admission.acquire_network
    def bounded(*args, **kwargs):
        return original_acquire(*args, **kwargs, limits=original(timeout_seconds=3))
    monkeypatch.setattr(git_admission, "acquire_network", bounded)
    marker.write_text("stall")
    started = time.monotonic()
    try:
        with pytest.raises(git_admission.GitAdmissionError) as error:
            test_windows_real_git_ssh_clean_environment_matrix(tmp_path, "ed25519", monkeypatch)
        assert error.value.failure_class == "timeout"
        assert (root / "authenticated").is_file(), "must reach authenticated remote command"
        assert time.monotonic() - started < 10
    finally:
        marker.unlink(missing_ok=True)
        (root / "authenticated").unlink(missing_ok=True)


@pytest.mark.skipif(os.name != "nt", reason="native TCP black hole deadline")
@pytest.mark.parametrize("transport", ["ssh", "https"])
def test_windows_tcp_blackhole(tmp_path: Path, transport: str) -> None:
    # A TCP listener accepts but never speaks SSH/TLS. Real Git must time out
    # for either transport; the successful native matrix is the positive control.
    import socket
    import threading
    import time
    stop = threading.Event()
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        listener.listen()
        listener.settimeout(0.2)
        port = listener.getsockname()[1]
        def serve():
            while not stop.is_set():
                try:
                    conn, _ = listener.accept()
                except TimeoutError:
                    continue
                with conn:
                    stop.wait(15)
        thread = threading.Thread(target=serve)
        thread.start()
        try:
            ssh = Path(os.environ["SYSTEMROOT"]) / "System32/OpenSSH/ssh.exe"
            paths = _private_paths(tmp_path / "private space")
            credentials = git_admission.OperatorSSHCredentials()
            command = None
            if transport == "ssh":
                configured = os.environ.get("CSK_TEST_WINDOWS_SSH_ROOT")
                if not configured:
                    pytest.skip("requires native loopback kit for the guarded SSH path")
                root = Path(configured)
                credentials = git_admission.capture_operator_ssh_credentials({},
                    identity=str(root / "First Last/.ssh/ed25519"), known_hosts=str(root / "known_hosts"))
            tool = _tool(ssh, credentials)
            if transport == "ssh":
                url = f"ssh://127.0.0.1:{port}/repo"
                source = RepositorySource(url, "127.0.0.1/repo", "ssh")
                connection = git_admission.NetworkEndpoint(url, url, source.identity, "ssh", "127.0.0.1", port,
                    "127.0.0.1", "/repo", port, port)
                command = git_admission._materialize_ssh_wrapper(paths, tool, source, connection=connection)
            env = git_admission._clean_git_environment(paths, tool, transport, ssh_command=command)
            started = time.monotonic()
            with pytest.raises(git_admission.GitAdmissionError) as error:
                # Match admission's protocol pin; Git v2 adds SendEnv, which
                # the guard correctly refuses before reaching the black hole.
                git_admission._run_git_ls_remote(tool, paths, env,
                    ["-c", "protocol.version=0", "ls-remote", f"{transport}://127.0.0.1:{port}/repo"],
                    limits=git_admission.Limits(timeout_seconds=3))
            assert error.value.failure_class == "timeout"
            assert time.monotonic() - started < 10
        finally:
            stop.set()
            thread.join(3)


@pytest.mark.skipif(os.name != "nt", reason="native Windows destination guard")
def test_windows_destination_guard_with_space_paths(tmp_path: Path) -> None:
    # Reject destination substitution before starting native ssh/DNS; nearby
    # real matrix cases prove the expected host succeeds with these path shapes.
    configured = os.environ.get("CSK_TEST_WINDOWS_SSH_ROOT")
    if not configured:
        pytest.skip("requires native loopback kit")
    root = Path(configured)
    credentials = git_admission.capture_operator_ssh_credentials({},
        identity=str(root / "First Last/.ssh/ed25519"), known_hosts=str(root / "known_hosts"))
    ssh = Path(os.environ["SYSTEMROOT"]) / "System32/OpenSSH/ssh.exe"
    paths = _private_paths(tmp_path / "private space")
    command = git_admission._materialize_ssh_wrapper(paths, _tool(ssh, credentials), parse_repository_source(SOURCE))
    result = subprocess.run([*command, "shifted.invalid", f"git-upload-pack '{EXPECTED_PATH}'"], capture_output=True)
    assert result.returncode == 1
    assert result.stderr.strip() == b"csk: refused unexpected ssh invocation"


@pytest.mark.skipif(os.name != "nt", reason="real Windows timeout diagnostics")
def test_windows_timeout_preserves_signing_hint(monkeypatch: pytest.MonkeyPatch) -> None:
    # A real child emits a signing error then stalls. Timeout retains the safe
    # hint and classification; a normal child is the nearby positive control.
    from csk import _windows_git_process as runner
    run = runner.run
    common = dict(stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=True)
    assert run([sys.executable, "-c", "print('ok')"], timeout=5, **common).stdout.strip() == b"ok"
    code = "import sys,time;sys.stderr.write('sign_and_send_pubkey: signing failed: agent refused operation\\n');sys.stderr.flush();time.sleep(120)"
    with pytest.raises(subprocess.TimeoutExpired) as error:
        run([sys.executable, "-c", code], timeout=2, **common)
    assert error.value.stderr and b"sign_and_send_pubkey" in error.value.stderr
    assert "ssh-agent-refused" in git_admission._git_failure_hints(error.value.stderr)
    # A cleanup bookkeeping error after actual termination must not replace
    # the timeout or discard its diagnostic. The preceding run uses no stub.
    end_job = runner._end_job
    def failed_cleanup(kernel, job):
        end_job(kernel, job)
        raise OSError("injected cleanup bookkeeping failure")
    monkeypatch.setattr(runner, "_end_job", failed_cleanup)
    with pytest.raises(subprocess.TimeoutExpired) as cleanup_error:
        run([sys.executable, "-c", code], timeout=2, **common)
    assert "bounded Windows Git cleanup failed" in cleanup_error.value.__notes__
    assert "ssh-agent-refused" in git_admission._git_failure_hints(cleanup_error.value.stderr)


@pytest.mark.skipif(os.name != "nt", reason="Windows pinned path expansion refusal")
@pytest.mark.parametrize("segment", ["a%b", "a%hb", "a%%b", "a${HOME}b", "a${NOPE}b"])
def test_windows_ssh_paths_refuse_token_expansion(tmp_path: Path, segment: str) -> None:
    # Expansion-capable identity and private trust paths must refuse before
    # SSH can substitute a sibling, including targets behind clean junctions.
    # Ordinary space paths and junctions to them remain valid controls.
    import _winapi

    valid = tmp_path / "First Last" / "identity"
    valid.parent.mkdir(); valid.write_text("fixture")
    assert git_admission.validate_operator_ssh_credentials(
        git_admission.OperatorSSHCredentials(identity=valid)).identity == valid
    bad = tmp_path / segment / "identity"
    bad.parent.mkdir(); bad.write_text("fixture")
    with pytest.raises(git_admission.GitAdmissionError, match="SSH would expand the pinned path") as error:
        git_admission.validate_operator_ssh_credentials(git_admission.OperatorSSHCredentials(identity=bad))
    assert error.value.code == git_admission.IDENTITY_INVALID
    with pytest.raises(git_admission.GitAdmissionError, match="SSH would expand the pinned path"):
        git_admission._ssh_config_path(bad)
    for target in (valid, bad):
        link = tmp_path / "clean junction"
        _winapi.CreateJunction(str(target.parent), str(link))
        try:
            assert (link / "identity").resolve(strict=True) == target
            for field in ("identity", "known_hosts"):
                credentials = git_admission.OperatorSSHCredentials(**{field: link / "identity"})
                if target == valid:
                    assert getattr(git_admission.validate_operator_ssh_credentials(credentials), field) == valid
                else:
                    with pytest.raises(git_admission.GitAdmissionError, match="SSH would expand the pinned path") as error:
                        git_admission.validate_operator_ssh_credentials(credentials)
                    assert error.value.code == git_admission.IDENTITY_INVALID
        finally:
            link.rmdir()  # Remove only the junction, never its target.


@pytest.mark.skipif(os.name != "nt", reason="Windows wrapper exit and manager lifecycle")
def test_windows_wrapper_exit_propagates_and_manager_kill(tmp_path: Path) -> None:
    # The real generated wrapper must preserve nonzero SSH exit status; a Job
    # owner killed externally must leave no inherited child running. No server.
    import ctypes
    import time
    from csk._windows_git_process import run
    paths = _private_paths(tmp_path / "private space")
    credentials = git_admission.OperatorSSHCredentials(identity=_identity(tmp_path / "operator"),
        known_hosts=_known_hosts(tmp_path / "operator"))
    command = git_admission._materialize_ssh_wrapper(paths, _tool(Path(sys.executable), credentials), parse_repository_source(SOURCE))
    wrapper = Path(command[1])
    # Replace only the executable stand-in after materializing the real guard
    # and dispatch: Python exits with the requested code, with normal CRT argv.
    source = wrapper.read_text(encoding="utf-8")
    prefix = source[:source.index("COMMAND = ")]
    suffix = source[source.index("if sys.argv != EXPECTED:"):]
    for status in (0, 7, 255):
        argv = [sys.executable, "-c", f"raise SystemExit({status})"]
        wrapper.write_text(prefix + "COMMAND = " + repr(argv) + "\n" + suffix, encoding="utf-8")
        result = subprocess.run([*command, EXPECTED_HOST, f"git-upload-pack '{EXPECTED_PATH}'"], capture_output=True)
        assert result.returncode == status
    manager = tmp_path / "manager.py"
    child_pid = tmp_path / "child.pid"
    # The manager installs the actual production Job domain and blocks. Its
    # child signals readiness only after the suspended launch was resumed.
    manager.write_text("from csk._windows_git_process import run\nimport subprocess,sys\n"
        "run([sys.executable,'-c'," + repr("import os,time;open(" + repr(str(child_pid)) + ",'w').write(str(os.getpid()));time.sleep(120)") +
        "],stdin=subprocess.DEVNULL,stdout=subprocess.PIPE,stderr=subprocess.PIPE,timeout=120,check=True)\n", encoding="utf-8")
    env = dict(os.environ, PYTHONPATH=str(Path(git_admission.__file__).parent.parent))
    process = subprocess.Popen([sys.executable, str(manager)], env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    kernel = getattr(ctypes, "WinDLL")("kernel32", use_last_error=True)
    kernel.OpenProcess.argtypes = [ctypes.c_uint32, ctypes.c_int, ctypes.c_uint32]
    kernel.OpenProcess.restype = ctypes.c_void_p
    kernel.WaitForSingleObject.argtypes = [ctypes.c_void_p, ctypes.c_uint32]
    kernel.CloseHandle.argtypes = [ctypes.c_void_p]
    kernel.TerminateProcess.argtypes = [ctypes.c_void_p, ctypes.c_uint32]
    handle = None
    try:
        deadline = time.monotonic() + 10
        while not child_pid.exists() and time.monotonic() < deadline:
            time.sleep(0.05)
        assert child_pid.exists(), "Job child never became ready"
        handle = kernel.OpenProcess(0x100001, False, int(child_pid.read_text()))
        assert handle
        process.kill(); process.wait(timeout=5)
        assert kernel.WaitForSingleObject(handle, 5000) == 0, "child survived manager kill"
    finally:
        if process.poll() is None:
            process.kill(); process.wait(timeout=5)
        if handle:
            if kernel.WaitForSingleObject(handle, 0) != 0:
                kernel.TerminateProcess(handle, 1)
                kernel.WaitForSingleObject(handle, 5000)
            kernel.CloseHandle(handle)


@pytest.mark.skipif(os.name != "nt", reason="native Windows hostile agent pipe")
def test_windows_discovery_ignores_live_hostile_pipe(tmp_path: Path) -> None:
    # Prove the sentinel can answer a real ssh-add (empty agent), then discovery
    # must count the loaded standard agent without contacting that sentinel.
    import ctypes
    import struct
    import threading
    import time
    import uuid
    from csk import build_ssh
    if not os.environ.get("CSK_TEST_WINDOWS_SSH_ROOT"):
        pytest.skip("requires generated loaded standard agent")
    kernel = getattr(ctypes, "WinDLL")("kernel32", use_last_error=True)
    kernel.CreateNamedPipeW.argtypes = [ctypes.c_wchar_p, ctypes.c_uint32, ctypes.c_uint32,
        ctypes.c_uint32, ctypes.c_uint32, ctypes.c_uint32, ctypes.c_uint32, ctypes.c_void_p]
    kernel.CreateNamedPipeW.restype = ctypes.c_void_p
    kernel.ConnectNamedPipe.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
    kernel.DisconnectNamedPipe.argtypes = [ctypes.c_void_p]
    kernel.ReadFile.argtypes = [ctypes.c_void_p, ctypes.c_void_p, ctypes.c_uint32, ctypes.c_void_p, ctypes.c_void_p]
    kernel.WriteFile.argtypes = kernel.ReadFile.argtypes
    kernel.CloseHandle.argtypes = [ctypes.c_void_p]
    name = r"\\.\pipe\csk-discovery-test-" + uuid.uuid4().hex
    handle = kernel.CreateNamedPipeW(name, 3, 1, 1, 4096, 4096, 0, None)
    assert handle and handle != ctypes.c_void_p(-1).value
    hits = []
    stop = threading.Event()
    def serve():
        while not stop.is_set():
            connected = kernel.ConnectNamedPipe(handle, None)
            if connected or getattr(ctypes, "get_last_error")() == 535:
                hits.append(True)
                buffer = ctypes.create_string_buffer(4096)
                count = ctypes.c_uint32()
                deadline = time.monotonic() + 2
                while not stop.is_set() and time.monotonic() < deadline:
                    if kernel.ReadFile(handle, buffer, len(buffer), ctypes.byref(count), None) and count.value:
                        payload = struct.pack(">IBI", 5, 12, 0)
                        kernel.WriteFile(handle, payload, len(payload), ctypes.byref(count), None)
                        break
                    time.sleep(0.01)
                kernel.DisconnectNamedPipe(handle)
            time.sleep(0.01)
    thread = threading.Thread(target=serve)
    thread.start()
    try:
        ssh_add = Path(os.environ["SYSTEMROOT"]) / "System32/OpenSSH/ssh-add.exe"
        env = dict(os.environ, SSH_AUTH_SOCK=name)
        control = subprocess.run([str(ssh_add), "-l"], env=env, capture_output=True, timeout=5)
        assert control.returncode == 1 and len(hits) == 1, "hostile pipe control did not apply"
        for value in (name, None, "", "/tmp/stale-agent", r"\\.\pipe\csk-missing-agent"):
            candidate_env = dict(os.environ)
            candidate_env.pop("SSH_AUTH_SOCK", None)
            if value is not None:
                candidate_env["SSH_AUTH_SOCK"] = value
            result = build_ssh.discover_candidates(environment=candidate_env, home=str(tmp_path))
            assert len(hits) == 1, "discovery contacted the ambient hostile pipe"
            assert result.agent_socket == git_admission._WINDOWS_SSH_AGENT_PIPE
            assert result.agent_key_count and result.agent_key_count >= 1
    finally:
        stop.set(); thread.join(3)
        kernel.CloseHandle(handle)
        assert not thread.is_alive()
