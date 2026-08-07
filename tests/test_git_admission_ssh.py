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

from csk import git_admission
from csk.build_repository import LockedCommit, parse_repository_source


SOURCE = "git@fixture.test:repository.git"
EXPECTED_HOST = "git@fixture.test"
EXPECTED_PATH = "repository.git"


pytestmark = pytest.mark.skipif(
    os.name == "nt", reason="the stand-in ssh program relies on POSIX exec semantics"
)


def _git_path() -> Path:
    resolved = shutil.which("git")
    assert resolved is not None
    return Path(resolved).resolve(strict=True)


def _fixture_repository(root: Path) -> tuple[Path, str]:
    """Build a bare repository the stand-in ssh program can serve."""

    git = os.fspath(_git_path())
    work = root / "work"
    work.mkdir(parents=True)
    environment = {
        **os.environ,
        "GIT_AUTHOR_NAME": "csk",
        "GIT_AUTHOR_EMAIL": "csk@fixture.test",
        "GIT_COMMITTER_NAME": "csk",
        "GIT_COMMITTER_EMAIL": "csk@fixture.test",
        "GIT_AUTHOR_DATE": "1700000000 +0000",
        "GIT_COMMITTER_DATE": "1700000000 +0000",
    }
    def run(*arguments: str, cwd: Path = work) -> str:
        return subprocess.run(
            (git, *arguments),
            cwd=cwd,
            env=environment,
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            check=True,
        ).stdout

    run("init", "--quiet", "--object-format=sha1", "--initial-branch=main", ".")
    (work / "README.md").write_text("external tool\n", encoding="utf-8")
    run("add", "README.md")
    run("commit", "--quiet", "-m", "initial")
    commit = run("rev-parse", "HEAD").strip()
    bare = root / "remote.git"
    run("clone", "--quiet", "--bare", os.fspath(work), os.fspath(bare), cwd=root)
    return bare, commit


def _stub_ssh(root: Path, bare: Path) -> tuple[Path, Path]:
    """Write an ssh stand-in that logs its argv and serves ``git upload-pack``."""

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
    version = subprocess.run(
        (os.fspath(executable), "--version"),
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    exec_path = Path(
        subprocess.run(
            (os.fspath(executable), "--exec-path"),
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()
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
