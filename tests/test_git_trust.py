from __future__ import annotations

import hashlib
import json
import os
import shutil
import ssl
import subprocess
from dataclasses import replace
from pathlib import Path

import pytest

from csk import build_https, config, git_admission, git_ops, git_trust, installer
from csk.build_repository import LockedCommit, parse_repository_source
from csk.builds.toolchain import OperatorSearchPath
from csk.sources import repository_policy, transport


@pytest.fixture
def bundle(git_ca_bundle: Path) -> Path:
    return git_ca_bundle


def _configuration(tmp_path: Path, bundle: str | None = None) -> config.GlobalConfig:
    data: dict = {
        "schema_version": 1,
        "skills_root": str(tmp_path / "skills"),
        "projects": {},
    }
    if bundle is not None:
        data["git"] = {"https_ca_bundle": bundle}
    return config.parse_config(data, tmp_path / "config.json")


def _list_certificate(monkeypatch: pytest.MonkeyPatch, bundle: Path) -> None:
    der = ssl.PEM_cert_to_DER_cert(bundle.read_text())
    monkeypatch.setattr(
        git_trust,
        "DISTRUSTED_CERTIFICATES",
        {
            hashlib.sha256(der).hexdigest(): "Generated blocked CA",
        },
    )


def test_precedence_and_config_roundtrip(
    bundle: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Environment wins without persisting its value; disk config wins over platform candidates.
    configured = tmp_path / "configured.pem"
    configured.write_bytes(bundle.read_bytes())
    cfg = _configuration(tmp_path, str(configured))
    config.save_config(cfg)
    assert config.load_config(cfg.path).git == cfg.git
    assert f"http.sslCAInfo={bundle}" in git_trust.https_git_args(cfg)
    assert json.loads(cfg.path.read_text())["git"]["https_ca_bundle"] == str(configured)
    monkeypatch.delenv("CSK_GIT_CA_BUNDLE")
    monkeypatch.setenv("CSK_CONFIG", str(cfg.path))
    assert f"http.sslCAInfo={configured}" in git_trust.https_git_args()


@pytest.mark.parametrize(
    "platform,constant,count",
    [("darwin", "MACOS_BUNDLES", 3), ("linux", "LINUX_BUNDLES", 4)],
)
def test_platform_candidate_order(
    bundle: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    platform: str,
    constant: str,
    count: int,
) -> None:
    # Both platforms choose the first existing candidate, including each fallback position.
    monkeypatch.delenv("CSK_GIT_CA_BUNDLE")
    monkeypatch.setattr(git_trust.sys, "platform", platform)
    paths = [tmp_path / f"candidate-{i}.pem" for i in range(count)]
    for path in paths:
        path.write_bytes(bundle.read_bytes())
    monkeypatch.setattr(git_trust, constant, tuple(map(str, paths)))
    for path in paths:
        assert f"http.sslCAInfo={path}" in git_trust.https_git_args(
            _configuration(tmp_path)
        )
        path.unlink()
    with pytest.raises(git_trust.GitTrustError) as error:
        git_trust.https_git_args(_configuration(tmp_path))
    assert error.value.code == git_trust.INVALID


def test_windows_backend(
    bundle: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Windows uses its store without reading a file; an explicit bundle selects OpenSSL.
    monkeypatch.setattr(git_trust.sys, "platform", "win32")
    assert "http.sslBackend=openssl" in git_trust.https_git_args()
    assert f"http.sslCAInfo={bundle}" in git_trust.https_git_args()
    monkeypatch.delenv("CSK_GIT_CA_BUNDLE")
    assert git_trust.https_git_args(_configuration(tmp_path)) == (
        "-c",
        "http.sslBackend=schannel",
    )


@pytest.mark.parametrize("value", ["relative.pem", "", "missing", "directory"])
def test_bad_override_refused(
    bundle: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, value: str
) -> None:
    # Invalid overrides never silently fall back to a platform bundle.
    selected = str(tmp_path / value) if value in {"missing", "directory"} else value
    if value == "directory":
        Path(selected).mkdir()
    monkeypatch.setenv("CSK_GIT_CA_BUNDLE", selected)
    with pytest.raises(git_trust.GitTrustError) as error:
        git_trust.https_git_args()
    assert error.value.code == git_trust.INVALID
    assert "remediation:" in str(error.value)


@pytest.mark.parametrize(
    "content",
    [
        b"",
        b"not a certificate",
        b"-----BEGIN CERTIFICATE-----\n@@\n-----END CERTIFICATE-----",
        b"-----BEGIN CERTIFICATE-----\nYQ==\n-----END CERTIFICATE-----",
    ],
)
def test_unparsable_bundle(bundle: Path, content: bytes) -> None:
    # Empty, malformed base64 and non-X.509 DER all fail with the intended code.
    bundle.write_bytes(content)
    with pytest.raises(git_trust.GitTrustError) as error:
        git_trust.https_git_args()
    assert error.value.code == git_trust.INVALID
    assert "BEGIN CERTIFICATE" not in str(error.value)


def test_valid_plus_malformed_and_oversize(
    bundle: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # A clean prefix cannot hide a malformed trailing certificate or an oversized file.
    content = bundle.read_bytes()
    git_trust.https_git_args()
    bundle.write_bytes(content + b"\n-----BEGIN CERTIFICATE-----\ninvalid")
    with pytest.raises(git_trust.GitTrustError, match=git_trust.INVALID):
        git_trust.https_git_args()
    bundle.write_bytes(content)
    monkeypatch.setattr(git_trust, "MAX_BUNDLE_BYTES", len(content) - 1)
    with pytest.raises(git_trust.GitTrustError, match=git_trust.INVALID):
        git_trust.https_git_args()


def test_unreadable_bundle(bundle: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # Read failures become a trust refusal rather than bypassing validation.
    def denied(*args: object, **kwargs: object) -> int:
        raise PermissionError("fixture denied")

    monkeypatch.setattr(git_trust.os, "open", denied)
    with pytest.raises(git_trust.GitTrustError, match=git_trust.INVALID):
        git_trust.https_git_args()


def test_cache_uses_content_and_policy(
    bundle: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Repeated clean bytes parse once; changing the distrust policy invalidates that success.
    original = git_trust.ssl.PEM_cert_to_DER_cert
    calls: list[str] = []

    def record(pem: str) -> bytes:
        calls.append(pem)
        return original(pem)

    monkeypatch.setattr(git_trust.ssl, "PEM_cert_to_DER_cert", record)
    git_trust.https_git_args()
    git_trust.https_git_args()
    assert len(calls) == 1
    _list_certificate(monkeypatch, bundle)
    with pytest.raises(git_trust.GitTrustError, match=git_trust.DISTRUSTED):
        git_trust.https_git_args()


@pytest.mark.parametrize("operation", ["acquire", "resolve", "clone", "fetch", "tool"])
@pytest.mark.parametrize("invalid", [False, True])
def test_distrust_before_any_child_or_token_environment(
    bundle: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    operation: str,
    invalid: bool,
) -> None:
    # Every entry point rejects a generated listed CA before Git discovery or broker environment construction.
    _list_certificate(monkeypatch, bundle)
    if invalid:
        bundle.write_bytes(b"malformed fixture")
    children: list[object] = []

    def forbidden(*args: object, **kwargs: object) -> None:
        children.append(kwargs.get("env"))
        pytest.fail("subprocess started before trust refusal")

    monkeypatch.setattr(subprocess, "run", forbidden)
    monkeypatch.setattr(git_admission, "_clean_git_environment", forbidden)
    credentials = git_admission.OperatorHTTPSCredentials(
        "example.test/repo", "example.test", "env", "user", "secret-sentinel"
    )
    tool = git_admission.GitTool(
        Path("unused"), Path("unused"), (), https_credentials=credentials
    )
    source = parse_repository_source("https://example.test/repo.git")
    lock = LockedCommit("sha1", "a" * 40)
    repo = tmp_path / "repo"
    (repo / ".git").mkdir(parents=True)
    (repo / ".git" / "config").write_text(
        '[remote "origin"]\n url = https://example.test/repo.git\n'
    )
    with pytest.raises((git_admission.GitAdmissionError, git_ops.GitError)) as error:
        if operation == "acquire":
            git_admission.acquire_network(source, lock, tool)
        elif operation == "resolve":
            git_admission.resolve_network_ref(source, "branch", "main", tool)
        elif operation == "clone":
            git_ops.clone_repo(source.git, tmp_path / "destination")
        elif operation == "fetch":
            git_ops.fetch_repo(repo)
        else:
            installer._external_git_tool(
                OperatorSearchPath(()), require_ssh=False, https_credentials=credentials
            )
    assert error.value.code == (git_trust.INVALID if invalid else git_trust.DISTRUSTED)
    assert str(bundle) in str(error.value)
    if not invalid:
        assert "Generated blocked CA" in str(error.value)
    assert "remediation:" in str(error.value)
    assert "secret-sentinel" not in str(error.value)
    assert not children
    assert not (tmp_path / "destination").exists()


def test_named_provider_checks_before_credential_read(
    bundle: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Anonymous HTTPS remains valid; a listed CA blocks the provider before any credential lookup.
    endpoint = repository_policy.ResolvedEndpoint(
        "example.test/repo",
        "https",
        "anonymous",
        repository_policy.EndpointProvenance(
            "https://example.test/repo.git",
            "example.test",
            None,
            "example.test",
            443,
            None,
            None,
        ),
    )
    cfg = _configuration(tmp_path)
    assert (
        installer._endpoint_https_credentials(
            endpoint, cfg, None, OperatorSearchPath(())
        )
        is None
    )
    endpoint = replace(endpoint, authentication="selected-provider")
    cfg = replace(
        cfg,
        build_https=(
            build_https.BuildHTTPSRule("example.test", token="git-credentials"),
        ),
    )
    credentials = git_admission.OperatorHTTPSCredentials(
        "example.test/repo", "example.test", "env", "user", "secret-sentinel"
    )
    monkeypatch.setattr(
        installer, "_https_rule_credentials", lambda *args, **kwargs: credentials
    )
    assert (
        installer._endpoint_https_credentials(
            endpoint, cfg, None, OperatorSearchPath(())
        )
        is credentials
    )
    _list_certificate(monkeypatch, bundle)
    reads: list[object] = []

    def forbidden(*args: object, **kwargs: object) -> None:
        reads.append(args)
        pytest.fail("credential access preceded trust validation")

    monkeypatch.setattr(installer, "_https_rule_credentials", forbidden)
    with pytest.raises(git_admission.GitAdmissionError, match=git_trust.DISTRUSTED):
        installer._endpoint_https_credentials(
            endpoint, cfg, None, OperatorSearchPath(())
        )
    assert not reads


def test_transport_preserves_only_manager_trust_diagnostics(
    bundle: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Genuine trust refusals retain remediation; a broker cannot expose arbitrary text by copying the code.
    _list_certificate(monkeypatch, bundle)
    with pytest.raises(git_admission.GitAdmissionError) as captured:
        git_admission.https_trust_args()
    normalised = transport._normalise_failure(captured.value)
    assert normalised.original is not None
    assert "remediation:" in str(normalised.original)
    forged = transport._normalise_failure(
        git_admission.GitAdmissionError(git_trust.INVALID, "secret-sentinel")
    )
    assert forged.original is not None
    assert "secret-sentinel" not in str(forged.original)


def test_clean_pins_apply_to_ssh_and_https(
    bundle: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Every transport pins a private copy; an invalid override blocks SSH too.
    tool = git_admission.GitTool(Path("unused"), Path("unused"), ())
    paths = git_admission._make_private_paths(tmp_path)
    for url in ("https://example.test/repo.git", "git@example.test:repo.git"):
        source = parse_repository_source(url)
        environment = git_admission._clean_git_environment(
            paths, tool, source.transport
        )
        args = git_trust.pinned_command(
            (
                "git",
                *git_admission._strict_fetch_args(
                    paths, tool, source, "a:refs/csk/locked"
                ),
            ),
            environment,
        )
        selected = Path(environment["GIT_SSL_CAINFO"])
        assert selected != bundle and selected.read_bytes() == bundle.read_bytes()
        assert f"http.sslCAInfo={selected}" in args
        assert "http.sslVerify=true" in args
        assert "http.followRedirects=false" in args
    monkeypatch.setenv("CSK_GIT_CA_BUNDLE", "invalid")
    with pytest.raises(git_admission.GitAdmissionError, match=git_trust.INVALID):
        git_admission._clean_git_environment(paths, tool, "ssh")


def test_clean_resolution_pins_bundle_before_token(
    bundle: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # A real resolution entry point pins the clean bundle and creates only the intended broker token environment.
    credentials = git_admission.OperatorHTTPSCredentials(
        "example.test/repo", "example.test", "env", "user", "secret-sentinel"
    )
    tool = git_admission.GitTool(
        Path("unused"),
        Path("unused"),
        (),
        askpass=Path("unused"),
        https_credentials=credentials,
    )
    monkeypatch.setattr(
        git_admission, "validate_git_tool", lambda *args, **kwargs: None
    )
    recorded: list[tuple[dict[str, str], tuple[str, ...]]] = []

    def resolve(tool, paths, environment, arguments, *, limits):
        assert Path(environment["GIT_SSL_CAINFO"]).read_bytes() == bundle.read_bytes()
        recorded.append(
            (
                dict(environment),
                git_trust.pinned_command(("git", *arguments), environment),
            )
        )
        return ("a" * 40 + "\trefs/heads/main\n").encode()

    monkeypatch.setattr(git_admission, "_run_git_ls_remote", resolve)
    result = git_admission.resolve_network_ref(
        parse_repository_source("https://example.test/repo.git"), "branch", "main", tool
    )
    assert result.hex == "a" * 40
    assert len(recorded) == 1
    environment, args = recorded[0]
    assert f"http.sslCAInfo={environment['GIT_SSL_CAINFO']}" in args
    assert environment["GIT_SSL_CAINFO"] != str(bundle)
    assert environment[git_admission.HTTPS_BROKER_TOKEN_ENV] == "secret-sentinel"
    assert "secret-sentinel" not in repr(args)


def test_changed_bundle_refuses_before_token_environment(
    bundle: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # A frozen selection is rechecked: replacing clean bytes cannot reuse the validation cache before token placement.
    args = git_trust.https_git_args()
    tool = git_admission.GitTool(
        Path("unused"), Path("unused"), (), https_trust_args=args
    )
    paths = git_admission._make_private_paths(tmp_path)
    bundle.write_bytes(b"broken replacement")
    with pytest.raises(git_admission.GitAdmissionError) as error:
        git_admission._clean_git_environment(
            paths, tool, "https", https_token="secret-sentinel"
        )
    assert error.value.code == git_trust.INVALID
    assert "secret-sentinel" not in str(error.value)


def test_legacy_clone_pins_tls_environment(
    bundle: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Legacy HTTPS pins the checked CA in argv and environment; ambient TLS overrides cannot replace it.
    monkeypatch.setenv("GIT_SSL_CAINFO", "untrusted.pem")
    monkeypatch.setenv("GIT_SSL_CAPATH", "untrusted-directory")
    monkeypatch.setenv("GIT_SSL_NO_VERIFY", "true")
    monkeypatch.setattr(git_ops, "_git_executable", lambda: "git")
    calls: list[tuple[list[str], dict[str, str]]] = []

    def record(command, **kwargs):
        environment = kwargs["env"]
        assert Path(environment["GIT_SSL_CAINFO"]).read_bytes() == bundle.read_bytes()
        assert Path(environment["GIT_SSL_CAPATH"]).is_dir()
        calls.append((command, environment))
        return subprocess.CompletedProcess(
            command, 0, "https://example.test/repo.git\n", ""
        )

    monkeypatch.setattr(git_ops, "_run", record)
    git_ops.clone_repo("https://example.test/repo.git", tmp_path / "destination")
    assert len(calls) == 2
    for command, environment in calls:
        selected = environment["GIT_SSL_CAINFO"]
        assert f"http.sslCAInfo={selected}" in command
        assert selected != str(bundle)
        assert "GIT_SSL_NO_VERIFY" not in environment
        assert not Path(selected).exists()


def test_ssh_worktree_fetch_checks_override_before_git(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The amended universal gate rejects SSH before discovery, including gitfile worktrees.
    repo = tmp_path / "checkout"
    repo.mkdir()
    (repo / ".git").write_text("gitdir: ../metadata\n")
    monkeypatch.setenv("CSK_GIT_CA_BUNDLE", "invalid")

    def forbidden(*args, **kwargs):
        pytest.fail("Git started before universal trust refusal")

    monkeypatch.setattr(subprocess, "run", forbidden)
    with pytest.raises(git_ops.GitError, match=git_trust.INVALID):
        git_ops.fetch_repo(repo)


def test_real_git_receives_selected_bundle(bundle: Path, tmp_path: Path) -> None:
    # Real Git's trace records the CA pin before an intentionally unreachable HTTPS connection fails.
    executable = shutil.which("git")
    if executable is None:
        pytest.skip("Git unavailable")
    trace = tmp_path / "trace.json"
    environment = git_admission._clean_discovery_environment()
    environment.update(
        {
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_CONFIG_GLOBAL": os.devnull,
            "HOME": str(tmp_path),
            "GIT_TRACE2_EVENT": str(trace),
            "GIT_TERMINAL_PROMPT": "0",
            "GIT_ALLOW_PROTOCOL": "https",
        }
    )
    result = subprocess.run(
        [
            executable,
            *git_trust.https_git_args(),
            "-c",
            "http.sslVerify=true",
            "-c",
            "http.proxy=",
            "ls-remote",
            "https://127.0.0.1:1/repo.git",
        ],
        env=environment,
        capture_output=True,
        timeout=10,
        check=False,
    )
    assert result.returncode != 0
    events = [json.loads(line) for line in trace.read_text().splitlines()]
    start = next(event for event in events if event.get("event") == "start")
    assert f"http.sslCAInfo={bundle}" in start["argv"]
    assert any(event.get("event") == "child_start" for event in events)
