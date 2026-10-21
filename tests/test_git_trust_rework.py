from __future__ import annotations

import hashlib
import json
import os
import shutil
import ssl
import subprocess
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from conftest import commit_all, init_git_repo, run
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import NameOID
from test_git_trust import _list_certificate

from csk import git_admission, git_ops, git_trust


@pytest.mark.parametrize("suffix", [b"\x00", b"garbage", b"\x30\x00"])
def test_der_suffix_cannot_hide_a_listed_certificate(
    git_ca_bundle, monkeypatch, suffix
):
    # OpenSSL loads the listed prefix, but the checker rejects the whole ambiguous payload as invalid.
    original = ssl.PEM_cert_to_DER_cert(git_ca_bundle.read_text())
    git_trust.https_git_args()
    _list_certificate(monkeypatch, git_ca_bundle)
    with pytest.raises(git_trust.GitTrustError, match=git_trust.DISTRUSTED):
        git_trust.https_git_args()
    pem = ssl.DER_cert_to_PEM_cert(original + suffix)
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    context.load_verify_locations(cadata=pem)
    assert original in context.get_ca_certs(binary_form=True)
    git_ca_bundle.write_text(pem)
    with pytest.raises(git_trust.GitTrustError) as error:
        git_trust.https_git_args()
    assert error.value.code == git_trust.INVALID


@pytest.mark.parametrize(
    "der",
    [
        b"",
        b"\x30",
        b"\x30\x80\x00\x00",
        b"\x30\x85",
        b"\x30\x81\x01x",
        b"\x30\x82\x00\x80" + b"x" * 128,
        b"\x30\x04abc",
    ],
)
def test_malformed_outer_der_lengths_fail_closed(git_ca_bundle, der):
    # Truncated, indefinite, nonminimal and mismatched outer lengths all produce the typed refusal.
    git_ca_bundle.write_text(ssl.DER_cert_to_PEM_cert(der))
    with pytest.raises(git_trust.GitTrustError) as error:
        git_trust.https_git_args()
    assert error.value.code == git_trust.INVALID


@pytest.mark.parametrize(
    "mode", ["crlf", "spaces", "duplicate-begin", "raw-der", "trailing-junk"]
)
def test_reviewer_pem_neighbours(git_ca_bundle, monkeypatch, mode):
    # PEM whitespace preserves distrust; malformed blocks/raw DER cannot turn a listed root into an accepted bundle.
    content = git_ca_bundle.read_bytes()
    _list_certificate(monkeypatch, git_ca_bundle)
    if mode == "crlf":
        content = content.replace(b"\n", b"\r\n")
    elif mode == "spaces":
        content = content.replace(b"\n", b" \n")
    elif mode == "duplicate-begin":
        content = b"-----BEGIN CERTIFICATE-----\n" + content
    elif mode == "raw-der":
        content = ssl.PEM_cert_to_DER_cert(content.decode())
    else:
        content += b"\x00\xff junk"
    git_ca_bundle.write_bytes(content)
    with pytest.raises(git_trust.GitTrustError) as error:
        git_trust.https_git_args()
    assert error.value.code == (
        git_trust.INVALID
        if mode in {"duplicate-begin", "raw-der"}
        else git_trust.DISTRUSTED
    )


@pytest.mark.parametrize("mutation", ["replace", "symlink", "unlink"])
@pytest.mark.parametrize("failure", [False, True])
def test_private_snapshot_survives_operator_path_changes(
    git_ca_bundle, tmp_path, monkeypatch, mutation, failure
):
    # Discovery-time replacement/retarget/unlink cannot change child trust; private files are removed on success or failure.
    original = git_ca_bundle.read_bytes()
    selected = tmp_path / "operator.pem"
    if mutation == "symlink":
        selected.symlink_to(git_ca_bundle)
    else:
        selected.write_bytes(original)
    monkeypatch.setenv("CSK_GIT_CA_BUNDLE", str(selected))
    bad = tmp_path / "unchecked.pem"
    bad.write_bytes(b"not a certificate after check")

    def discover():
        selected.unlink()
        if mutation == "symlink":
            selected.symlink_to(bad)
        elif mutation == "replace":
            selected.write_bytes(bad.read_bytes())
        return "git"

    observed = []

    def child(command, **kwargs):
        environment = kwargs["env"]
        private = Path(environment["GIT_SSL_CAINFO"])
        assert private != selected and private.read_bytes() == original
        assert not private.is_symlink()
        assert private.stat().st_mode & 0o777 == 0o600
        assert private.parent.stat().st_mode & 0o777 == 0o700
        assert f"http.sslCAInfo={private}" in command
        assert not any(str(selected) in arg for arg in command)
        assert "GIT_SSL_NO_VERIFY" not in environment
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
        context.load_verify_locations(cafile=str(private))
        assert (
            hashlib.sha256(context.get_ca_certs(binary_form=True)[0]).digest()
            == hashlib.sha256(ssl.PEM_cert_to_DER_cert(original.decode())).digest()
        )
        observed.append(private)
        return subprocess.CompletedProcess(
            command,
            int(failure and "clone" in command),
            "https://fixture.invalid/repo.git\n",
            "fixture failure",
        )

    monkeypatch.setattr(git_ops, "_git_executable", discover)
    monkeypatch.setattr(git_ops, "_run", child)
    if failure:
        with pytest.raises(git_ops.GitError, match="git clone failed"):
            git_ops.clone_repo(
                "https://fixture.invalid/repo.git", tmp_path / "destination"
            )
    else:
        git_ops.clone_repo("https://fixture.invalid/repo.git", tmp_path / "destination")
    assert len(observed) == 2
    assert all(not path.exists() for path in observed)


def test_private_snapshot_exclusive_creation(git_ca_bundle, tmp_path):
    # A pre-existing snapshot symlink is refused and its target remains untouched.
    target = tmp_path / "target"
    target.write_bytes(b"preserve")
    directory = tmp_path / "private"
    directory.mkdir(mode=0o700)
    (directory / "ca.pem").symlink_to(target)
    with pytest.raises(git_trust.GitTrustError, match=git_trust.INVALID):
        git_trust.materialize_git_trust(directory, git_trust.https_git_args())
    assert target.read_bytes() == b"preserve"


def test_symlink_retarget_cannot_load_a_different_listed_root(
    git_ca_bundle, tmp_path, monkeypatch
):
    # A valid listed replacement cannot reach OpenSSL after validation of the original symlink target.
    key = ec.generate_private_key(ec.SECP256R1())
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "Other generated root")])
    now = datetime.now(UTC)
    certificate = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(key.public_key())
        .serial_number(2)
        .not_valid_before(now - timedelta(days=1))
        .not_valid_after(now + timedelta(days=1))
        .add_extension(x509.BasicConstraints(ca=True, path_length=None), critical=True)
        .sign(key, hashes.SHA256())
    )
    blocked = tmp_path / "blocked.pem"
    blocked.write_bytes(certificate.public_bytes(serialization.Encoding.PEM))
    digest = hashlib.sha256(
        certificate.public_bytes(serialization.Encoding.DER)
    ).hexdigest()
    monkeypatch.setattr(
        git_trust, "DISTRUSTED_CERTIFICATES", {digest: "Generated blocked root"}
    )
    selected = tmp_path / "selected.pem"
    selected.symlink_to(git_ca_bundle)
    monkeypatch.setenv("CSK_GIT_CA_BUNDLE", str(selected))
    observed = []

    def discover():
        selected.unlink()
        selected.symlink_to(blocked)
        return "git"

    def child(command, **kwargs):
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
        context.load_verify_locations(cafile=kwargs["env"]["GIT_SSL_CAINFO"])
        observed.extend(
            hashlib.sha256(item).hexdigest()
            for item in context.get_ca_certs(binary_form=True)
        )
        return subprocess.CompletedProcess(
            command, 0, "https://fixture.invalid/repo.git\n", ""
        )

    monkeypatch.setattr(git_ops, "_git_executable", discover)
    monkeypatch.setattr(git_ops, "_run", child)
    git_ops.clone_repo("https://fixture.invalid/repo.git", tmp_path / "destination")
    assert observed and digest not in observed
    assert set(observed) == {
        hashlib.sha256(ssl.PEM_cert_to_DER_cert(git_ca_bundle.read_text())).hexdigest()
    }


def test_private_snapshot_is_removed_on_interruption(git_ca_bundle):
    # Interrupted children cannot leave their private CA snapshot behind.
    with pytest.raises(KeyboardInterrupt), git_trust.private_git_trust() as trust:
        root = trust.empty_directory.parent
        assert trust.ca_file.read_bytes() == git_ca_bundle.read_bytes()
        raise KeyboardInterrupt
    assert not root.exists()


def test_private_snapshot_creation_failure_is_typed_and_cleaned(
    git_ca_bundle, monkeypatch
):
    # Failure to create the private CA file starts no child and removes the operation root.
    original_open = git_trust.os.open
    roots = []

    def refuse(path, flags, *args, **kwargs):
        path = Path(path) if isinstance(path, (str, Path)) else path
        if isinstance(path, Path) and path.name == "ca.pem" and flags & os.O_EXCL:
            roots.append(path.parent)
            raise PermissionError("fixture refusal")
        return original_open(path, flags, *args, **kwargs)

    monkeypatch.setattr(git_trust.os, "open", refuse)
    with (
        pytest.raises(git_trust.GitTrustError, match=git_trust.INVALID),
        git_trust.private_git_trust(),
    ):
        pytest.fail("private state failure reached the child boundary")
    assert len(roots) == 1 and not roots[0].exists()


@pytest.mark.parametrize(
    "arguments", [(), ("-c", "http.sslCAInfo="), ("-c", "http.sslCAInfo=relative.pem")]
)
def test_empty_or_relative_frozen_selection_cannot_skip_gate(arguments):
    # An absent or malformed frozen selection is never interpreted as permission to use ambient CA settings.
    with pytest.raises(git_trust.GitTrustError, match=git_trust.INVALID):
        git_trust.check_git_args(arguments)


@pytest.mark.parametrize(
    "kind",
    ["literal", "rewrite", "ssh-rewrite", "included", "include-if", "ssh", "local"],
)
def test_real_cli_invalid_bundle_precedes_every_git_start(tmp_path, kind):
    # Literal, rewritten, included, SSH and local remotes all refuse before the real CLI starts any Git process.
    skills = tmp_path / "skills"
    repo = init_git_repo(skills / "fixture")
    url = "https://127.0.0.1:1/repo.git"
    if kind in {"included", "include-if"}:
        included = tmp_path / "remotes.conf"
        included.write_text(f'[remote "origin"]\n url = {url}\n')
        key = "include.path" if kind == "included" else f"includeIf.gitdir:{repo}/.path"
        run(["git", "config", key, str(included)], repo)
    else:
        remote = {
            "rewrite": "review:repo.git",
            "ssh-rewrite": "ssh://fixture.invalid/repo.git",
            "ssh": "git@fixture.invalid:repo.git",
            "local": str(tmp_path / "other"),
        }.get(kind, url)
        run(["git", "remote", "add", "origin", remote], repo)
        if kind in {"rewrite", "ssh-rewrite"}:
            prefix = "review:" if kind == "rewrite" else "ssh://fixture.invalid/"
            run(["git", "config", "url.https://127.0.0.1:1/.insteadOf", prefix], repo)
    configuration = tmp_path / "config.json"
    configuration.write_text(
        json.dumps({"schema_version": 1, "skills_root": str(skills), "projects": {}})
    )
    trace = tmp_path / "trace.json"
    environment = dict(
        os.environ,
        CSK_CONFIG=str(configuration),
        CSK_GIT_CA_BUNDLE=str(tmp_path / "absent.pem"),
        GIT_TRACE2_EVENT=str(trace),
        PYTHONPATH=str(Path(__file__).resolve().parents[1] / "src"),
    )
    result = subprocess.run(
        [sys.executable, "-m", "csk", "update"],
        cwd=tmp_path,
        env=environment,
        capture_output=True,
        text=True,
        timeout=15,
        check=False,
    )
    assert result.returncode == 1
    assert git_trust.INVALID in result.stdout + result.stderr
    assert not trace.exists(), result.stdout + result.stderr


@pytest.mark.parametrize(
    "kind", ["included", "include-if", "valueless", "rewrite", "worktree"]
)
def test_native_git_configuration_still_fetches(git_ca_bundle, tmp_path, kind):
    # Native include/conditional include, boolean, rewrite and worktree semantics reach a real local fetch and update the tracking ref.
    source = init_git_repo(tmp_path / "source")
    (source / "data").write_text("fixture")
    commit = commit_all(source)
    repo = init_git_repo(tmp_path / "repo")
    if kind in {"included", "include-if"}:
        included = tmp_path / "remote.conf"
        included.write_text(f'[remote "origin"]\n url = {source}\n')
        key = "include.path" if kind == "included" else f"includeIf.gitdir:{repo}/.path"
        run(["git", "config", key, str(included)], repo)
    else:
        run(
            [
                "git",
                "remote",
                "add",
                "origin",
                "review:source" if kind == "rewrite" else str(source),
            ],
            repo,
        )
        if kind == "rewrite":
            run(["git", "config", f"url.{source}.insteadOf", "review:source"], repo)
        if kind == "valueless":
            with (repo / ".git/config").open("a") as stream:
                stream.write("\n[core]\n ignorecase\n")
            assert (
                run(["git", "config", "--bool", "core.ignorecase"], repo).stdout.strip()
                == "true"
            )
        if kind == "worktree":
            (repo / "data").write_text("initial")
            commit_all(repo)
            worktree = tmp_path / "worktree"
            run(["git", "worktree", "add", "--detach", str(worktree)], repo)
            repo = worktree
    git_ops.fetch_repo(repo)
    branch = run(["git", "branch", "--show-current"], source).stdout.strip()
    assert (
        run(["git", "rev-parse", f"refs/remotes/origin/{branch}"], repo).stdout.strip()
        == commit
    )


@pytest.mark.parametrize(
    "windows,override", [(False, True), (True, True), (True, False)]
)
def test_native_url_specific_tls_config_cannot_replace_pins(
    git_ca_bundle, tmp_path, monkeypatch, windows, override
):
    # Real Git URL matching retains manager pins over project settings; Windows backend/store choices are simulated.
    repo = init_git_repo(tmp_path / "repo")
    url = "https://fixture.invalid/repo.git"
    for key, value in {
        "sslVerify": "false",
        "sslCAInfo": "/unchecked.pem",
        "sslBackend": "unchecked",
        "schannelUseSSLCAInfo": "true",
    }.items():
        run(["git", "config", f"http.{url}.{key}", value], repo)
    executable = shutil.which("git")
    assert executable is not None
    if windows:
        monkeypatch.setattr(git_trust.sys, "platform", "win32")
    if not override:
        monkeypatch.delenv("CSK_GIT_CA_BUNDLE")
    with git_trust.private_git_trust() as trust:
        environment = trust.environment(os.environ)
        command = git_trust.pinned_command(
            (executable, "-C", str(repo), "config", "--get-urlmatch", "http", url),
            environment,
            url=url,
        )
        result = subprocess.run(
            command, env=environment, text=True, capture_output=True, check=True
        )
        settings = dict(line.split(" ", 1) for line in result.stdout.splitlines())
        assert settings["http.sslverify"] == "true"
        assert settings["http.schannelusesslcainfo"] == "false"
        if override:
            assert settings["http.sslcainfo"] == str(trust.ca_file)
        if windows:
            assert settings["http.sslbackend"] == (
                "openssl" if override else "schannel"
            )


@pytest.mark.parametrize(
    "operation",
    ["git", "archive", "ignore", "branch", "credential", "version", "local", "ssh"],
)
def test_ancillary_git_paths_refuse_before_child(tmp_path, monkeypatch, operation):
    # The amended pre-Git gate includes ancillary/local/SSH paths and never constructs a child with an invalid override.
    from csk import build_https, gitignore_gate, project_resolver

    repo = tmp_path / "repo"
    (repo / ".git").mkdir(parents=True)
    monkeypatch.setenv("CSK_GIT_CA_BUNDLE", str(tmp_path / "missing"))

    def forbidden(*args, **kwargs):
        pytest.fail("Git started before the universal gate")

    monkeypatch.setattr(subprocess, "run", forbidden)
    tool = git_admission.GitTool(Path("unused"), Path("unused"), ())
    with pytest.raises(
        (git_ops.GitError, git_admission.GitAdmissionError), match=git_trust.INVALID
    ):
        if operation == "git":
            git_ops.git(repo, ["remote"])
        elif operation == "archive":
            git_ops.archive(repo, "a" * 40, tmp_path / "out")
        elif operation == "ignore":
            gitignore_gate.missing_entries(repo, [".agents/"])
        elif operation == "branch":
            project_resolver.git_branch(repo)
        elif operation == "credential":
            build_https._run_credential("fill", {"host": "fixture.invalid"})
        elif operation == "version":
            git_admission.validate_git_tool(tool)
        elif operation == "local":
            paths = git_admission._make_private_paths(tmp_path)
            git_admission._clean_git_environment(paths, tool, "")
        else:
            from csk.build_repository import LockedCommit, parse_repository_source

            git_admission.acquire_network(
                parse_repository_source("git@fixture.invalid:repo.git"),
                LockedCommit("sha1", "a" * 40),
                tool,
            )
