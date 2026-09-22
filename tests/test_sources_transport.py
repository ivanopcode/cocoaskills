from __future__ import annotations

import base64
import errno
import getpass
import hashlib
import http.server
import json
import os
import shutil
import socket
import ssl
import struct
import subprocess
import sys
import tempfile
import threading
import time
from collections.abc import Callable, Iterator
from dataclasses import replace
from pathlib import Path
from typing import Self, TypeVar

import pytest
from test_git_admission_ssh import (
    _fixture_repository as _ssh_fixture_repository,
)
from test_git_admission_ssh import (
    _identity as _ssh_identity,
)
from test_git_admission_ssh import (
    _known_hosts as _ssh_known_hosts,
)
from test_git_admission_ssh import (
    _tool as _ssh_tool,
)

from csk import git_admission
from csk.build_repository import LockedCommit
from csk.build_repository_pipeline import (
    BuildTarget,
    CompilerIdentity,
    DeclaredState,
    EffectiveState,
    ExternalBuildError,
    Operation,
    PipelineRequest,
    receipt_input,
    run_pipeline,
    snapshot_key,
)
from csk.sources import repository_policy, transport

IDENTITY = "example.org/kit"
LOCK = LockedCommit("sha1", "0" * 40)

_posix_ssh_only = pytest.mark.skipif(
    os.name == "nt",
    reason="the stand-in ssh program relies on POSIX exec semantics",
)


def _git_path() -> Path:
    resolved = shutil.which("git")
    assert resolved is not None
    return Path(resolved).resolve()


def _git(cwd: Path | None, *args: str) -> str:
    return subprocess.run(
        (_git_path(), *args),
        cwd=cwd,
        check=True,
        stdin=subprocess.DEVNULL,
        capture_output=True,
        timeout=20,
        text=True,
    ).stdout


def _real_tool() -> git_admission.GitTool:
    executable = _git_path()
    version = _git(None, "--version").strip()
    parts = version.split()[2].split(".")
    return git_admission.GitTool(
        executable=executable,
        exec_path=Path(_git(None, "--exec-path").strip()).resolve(),
        allowed_versions=(f"git version {parts[0]}.{parts[1]}.",),
    )


def _bare_repository(root: Path) -> tuple[Path, str]:
    root.mkdir(parents=True)
    work = root / "work"
    bare = root / "remote.git"
    template = root / "template"
    template.mkdir(parents=True)
    _git(None, "init", "--quiet", f"--template={template}", os.fspath(work))
    (work / "README.md").write_bytes(b"transport fixture\n")
    _git(work, "add", "--", "README.md")
    _git(
        work,
        "-c",
        "user.name=Fixture",
        "-c",
        "user.email=fixture@example.test",
        "commit",
        "--quiet",
        "-m",
        "fixture",
    )
    commit = _git(work, "rev-parse", "HEAD").strip()
    _git(None, "clone", "--quiet", "--bare", os.fspath(work), os.fspath(bare))
    return bare, commit


def _host_is_windows() -> bool:
    """Whether this host executes the Windows flavour of fixture scripts.

    Split out so tests can select the flavour without renaming the host.
    """

    return os.name == "nt"


def _windows_git_wrapper_shim(interpreter: str, script: str) -> str:
    """Batch bytes invoking the wrapper payload through an explicit interpreter.

    ``CreateProcess`` runs ``.bat`` files but not extensionless scripts, so
    the Windows flavour is a one-line shim over the same payload POSIX runs
    directly (the production HTTPS broker uses the same ``.bat``/``.sh``
    split). ``%*`` replays the argument tail verbatim, and the shim's exit
    code is the payload's, hence real git's.
    """

    return f'@echo off\r\n"{interpreter}" "{script}" %*\r\n'


def _windows_fail_askpass_shim() -> str:
    """Batch bytes for the failing askpass: exit 1 with no output."""

    return "@echo off\r\nexit /b 1\r\n"


def _wrapper_payload_path(executable: Path) -> Path:
    """The Python payload behind a fixture wrapper executable.

    The POSIX executable IS the payload; the Windows ``.bat`` delegates to
    its sibling ``.py``. Tests instrumenting the payload (the deadline test
    injecting sleeps) edit this path on every host.
    """

    if executable.suffix.lower() == ".bat":
        return executable.with_suffix(".py")
    return executable


def _fake_http_tool(
    tmp_path: Path,
    mappings: dict[str, Path],
    *,
    allow_loopback_https: bool = False,
) -> git_admission.GitTool:
    real = _real_tool()
    tmp_path.mkdir(parents=True)
    script = tmp_path / "git-wrapper.py"
    wrapper = tmp_path / "git-wrapper"
    mapping = {key: "file://" + os.fspath(value) for key, value in mappings.items()}
    protocol_fallback = (
        "protocol.https.allow=always"
        if allow_loopback_https
        else "protocol.file.allow=always"
    )
    global_policy = "protocol.allow=always" if allow_loopback_https else "protocol.allow=never"
    script.write_text(
        f"#!{sys.executable}\n"
        "import json, subprocess, sys\n"
        f"MAPPINGS = {mapping!r}\n"
        f"args = [MAPPINGS.get(value, {protocol_fallback!r} if value == 'protocol.https.allow=always' else {global_policy!r} if value == 'protocol.allow=never' else value) for value in sys.argv[1:]]\n"
        f"raise SystemExit(subprocess.run([{os.fspath(real.executable)!r}, *args], check=False).returncode)\n",
        encoding="utf-8",
    )
    wrapper.write_bytes(script.read_bytes())
    wrapper.chmod(0o700)
    shim = tmp_path / "git-wrapper.bat"
    shim.write_bytes(
        _windows_git_wrapper_shim(sys.executable, os.fspath(script)).encode(
            "utf-8"
        )
    )
    shim.chmod(0o700)
    askpass = tmp_path / "askpass"
    askpass.write_text("#!/bin/sh\nexit 1\n", encoding="utf-8")
    askpass.chmod(0o700)
    windows_askpass = tmp_path / "askpass.bat"
    windows_askpass.write_bytes(_windows_fail_askpass_shim().encode("utf-8"))
    windows_askpass.chmod(0o700)
    windows_host = _host_is_windows()
    return git_admission.GitTool(
        executable=shim if windows_host else wrapper,
        exec_path=real.exec_path,
        allowed_versions=real.allowed_versions,
        askpass=windows_askpass if windows_host else askpass,
    )


def _snapshot() -> git_admission.Snapshot:
    files = (git_admission.SnapshotFile("value", b"value"),)
    canonical = b"curator-build-source-v1\0F\x00\x00\x00\x00\x00\x00\x00\x05value\x00\x00\x00\x00\x00\x00\x00\x05value"
    return git_admission.Snapshot(
        object_format="sha1",
        commit=LOCK.hex,
        files=files,
        canonical_bytes=canonical,
        digest="sha256:" + hashlib.sha256(canonical).hexdigest(),
    )


@pytest.fixture(scope="module")
def _loopback_tls_cert(
    tmp_path_factory: pytest.TempPathFactory,
) -> tuple[str, str]:
    """One self-signed 127.0.0.1 certificate for every loopback HTTPS test."""

    if shutil.which("openssl") is None:
        pytest.skip("the loopback TLS fixture requires the openssl CLI")
    root = tmp_path_factory.mktemp("loopback-tls")
    key = os.fspath(root / "key.pem")
    crt = os.fspath(root / "crt.pem")
    subprocess.run(
        [
            "openssl",
            "req",
            "-x509",
            "-newkey",
            "rsa:2048",
            "-nodes",
            "-keyout",
            key,
            "-out",
            crt,
            "-days",
            "2",
            "-subj",
            "/CN=127.0.0.1",
            "-addext",
            "subjectAltName=IP:127.0.0.1",
        ],
        check=True,
        capture_output=True,
    )
    return key, crt


class _StatusServer:
    """Loopback HTTPS server returning one fixed status/body/content-type."""

    def __init__(
        self,
        key_path: str,
        crt_path: str,
        *,
        status: int,
        body: bytes,
        content_type: str = "text/plain",
    ) -> None:
        self.status = status
        self.body = body
        self.content_type = content_type
        self.auth_seen: list[str | None] = []
        outer = self

        class Handler(http.server.BaseHTTPRequestHandler):
            def _respond(self) -> None:
                outer.auth_seen.append(self.headers.get("Authorization"))
                self.send_response(outer.status)
                if outer.status == 401:
                    self.send_header("WWW-Authenticate", 'Basic realm="git"')
                self.send_header("Content-Type", outer.content_type)
                self.send_header("Content-Length", str(len(outer.body)))
                self.end_headers()
                try:
                    self.wfile.write(outer.body)
                except (BrokenPipeError, ConnectionResetError):
                    pass

            do_GET = _respond
            do_POST = _respond

            def log_message(self, *args: object) -> None:
                pass

        self._server = http.server.HTTPServer(("127.0.0.1", 0), Handler)
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        context.load_cert_chain(crt_path, key_path)
        self._server.socket = context.wrap_socket(
            self._server.socket, server_side=True
        )
        self.port = self._server.server_address[1]
        self._thread = threading.Thread(
            target=self._server.serve_forever, daemon=True
        )
        self._thread.start()

    @property
    def url(self) -> str:
        return f"https://127.0.0.1:{self.port}/kit.git"

    def shutdown(self) -> None:
        self._server.shutdown()
        self._thread.join(timeout=10)


@pytest.fixture
def _tls_server_factory(
    _loopback_tls_cert: tuple[str, str],
    monkeypatch: pytest.MonkeyPatch,
) -> Iterator[Callable[..., _StatusServer]]:
    """Serve loopback HTTPS fixtures with additive CA trust only.

    The production clean environment is preserved bit-for-bit except for
    the added GIT_SSL_CAINFO entry, so this fixture cannot mask
    ambient-config leakage: it only lets the TLS handshake succeed.
    """

    key_path, crt_path = _loopback_tls_cert
    real_clean = git_admission._clean_git_environment

    def clean_with_ca(*args: object, **kwargs: object) -> dict[str, str]:
        environment = real_clean(*args, **kwargs)  # type: ignore[arg-type]
        environment["GIT_SSL_CAINFO"] = crt_path
        return environment

    monkeypatch.setattr(git_admission, "_clean_git_environment", clean_with_ca)
    servers: list[_StatusServer] = []

    def make(
        *,
        status: int,
        body: bytes,
        content_type: str = "text/plain",
    ) -> _StatusServer:
        server = _start_loopback_https_server(
            key_path,
            crt_path,
            status=status,
            body=body,
            content_type=content_type,
        )
        servers.append(server)
        return server

    yield make
    for server in servers:
        server.shutdown()


@pytest.fixture(scope="module")
def _shared_second_bare(
    tmp_path_factory: pytest.TempPathFactory,
) -> tuple[Path, str]:
    """One file-mapped fallback target shared by the real-server matrix.

    Fetches read the source bare repository without mutating it, and the
    matrix runs sequentially on one worker, so sharing is safe.
    """

    return _bare_repository(tmp_path_factory.mktemp("matrix") / "second")


def _https_mirror_plan(
    first_url: str, second_url: str
) -> repository_policy.ResolutionPlan:
    return _v2_plan(
        [
            {
                "url": first_url,
                "authentication": "first",
                "mirror_of": IDENTITY,
            },
            {
                "url": second_url,
                "authentication": "second",
                "mirror_of": IDENTITY,
            },
        ]
    )


def _sshd_path() -> Path | None:
    located = shutil.which("sshd")
    if located is not None:
        return Path(located)
    fallback = Path("/usr/sbin/sshd")
    return fallback if fallback.exists() else None


_requires_sshd = pytest.mark.skipif(
    _sshd_path() is None,
    reason="the live sshd fixture requires an sshd binary",
)


class _RealServerUnavailable(Exception):
    """A real-server fixture could not serve; the caller skips with the reason."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


_T = TypeVar("_T")


def _serve_or_skip(start: Callable[[], _T]) -> _T:
    """Run a real-server start, skipping with its named cause when it cannot.

    The single capability gate every real-server fixture goes through: the
    skip is conditional on the server actually failing to serve, never
    unconditional and never platform-named.
    """

    try:
        return start()
    except _RealServerUnavailable as unavailable:
        pytest.skip(unavailable.reason)


class _LiveSshdUnavailable(_RealServerUnavailable):
    """The live sshd did not come up; the caller must skip with the reason."""


def _classify_sshd_start_failure(log_text: str) -> str | None:
    """Name the specific cause a live sshd failed to come up, if known.

    Returns the cause fragment for the skip reason, or None when the log
    names nothing this fixture can distinguish.
    """

    lowered = log_text.lower()
    if (
        "address already in use" in lowered
        or "cannot bind" in lowered
        or "bind to port" in lowered
    ):
        return "port unavailable"
    if (
        "operation not permitted" in lowered
        or "setgroups" in lowered
        or "privilege separation" in lowered
    ):
        return "privilege refused"
    return None


def _sshd_skip_reason(log_text: str, *, timed_out: bool) -> str:
    """Build the named skip reason for a live sshd that did not come up."""

    lines = [line.strip() for line in log_text.splitlines() if line.strip()]
    detail = lines[-1][:160] if lines else "no log output"
    cause = _classify_sshd_start_failure(log_text)
    if cause is not None:
        return f"live sshd unavailable: {cause} ({detail})"
    if timed_out:
        return f"live sshd unavailable: server did not listen in time ({detail})"
    return f"live sshd unavailable: server exited before serving ({detail})"


class _LiveSshd:
    """An unprivileged loopback sshd for live-output verification."""

    def __init__(
        self,
        root: Path,
        options: list[str],
        *,
        authorized_keys: Path | None = None,
    ) -> None:
        root.mkdir(parents=True, exist_ok=True)
        hostkey = root / "hostkey"
        if not hostkey.exists():
            subprocess.run(
                (
                    "ssh-keygen",
                    "-q",
                    "-t",
                    "ed25519",
                    "-f",
                    os.fspath(hostkey),
                    "-N",
                    "",
                ),
                check=True,
                capture_output=True,
            )
        sshd = _sshd_path()
        if sshd is None:
            raise _LiveSshdUnavailable("live sshd unavailable: sshd binary absent")
        try:
            probe = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            probe.bind(("127.0.0.1", 0))
        except OSError as exc:
            raise _LiveSshdUnavailable(
                f"live sshd unavailable: loopback probe failed ({exc})"
            ) from exc
        self.port = probe.getsockname()[1]
        probe.close()
        self._log = (root / "sshd.log").open("wb")
        args = [
            os.fspath(sshd),
            "-D",
            "-e",
            "-p",
            str(self.port),
            "-h",
            os.fspath(hostkey),
            "-o",
            f"PidFile={os.fspath(root / 'sshd.pid')}",
            "-o",
            "UsePAM=no",
            "-o",
            (
                f"AuthorizedKeysFile={os.fspath(authorized_keys)}"
                if authorized_keys is not None
                else "AuthorizedKeysFile=none"
            ),
            "-o",
            "StrictModes=no",
            "-o",
            "LoginGraceTime=30",
        ]
        for option in options:
            args.extend(("-o", option))
        try:
            self._process = subprocess.Popen(
                args, stdout=self._log, stderr=subprocess.STDOUT
            )
        except FileNotFoundError as exc:
            self._log.close()
            raise _LiveSshdUnavailable(
                "live sshd unavailable: sshd binary absent"
            ) from exc
        except PermissionError as exc:
            self._log.close()
            raise _LiveSshdUnavailable(
                f"live sshd unavailable: privilege refused ({exc})"
            ) from exc
        except OSError as exc:
            self._log.close()
            raise _LiveSshdUnavailable(
                f"live sshd unavailable: sshd spawn refused ({exc})"
            ) from exc
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            if self._process.poll() is not None:
                self._log.flush()
                log_text = (root / "sshd.log").read_text(
                    encoding="utf-8", errors="replace"
                )
                self._log.close()
                raise _LiveSshdUnavailable(
                    _sshd_skip_reason(log_text, timed_out=False)
                )
            try:
                ready = socket.create_connection(
                    ("127.0.0.1", self.port), timeout=1
                )
            except OSError:
                time.sleep(0.1)
            else:
                ready.close()
                return
        self._process.terminate()
        try:
            self._process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            self._process.kill()
        self._log.flush()
        log_text = (root / "sshd.log").read_text(
            encoding="utf-8", errors="replace"
        )
        self._log.close()
        raise _LiveSshdUnavailable(
            _sshd_skip_reason(log_text, timed_out=True)
        )

    def close(self) -> None:
        self._process.terminate()
        try:
            self._process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            self._process.kill()
        self._log.close()


def _start_live_sshd(
    root: Path,
    options: list[str],
    *,
    authorized_keys: Path | None = None,
) -> _LiveSshd:
    """Start the live fixture, skipping with a named cause when it cannot.

    The skip is conditional on the server actually failing to come up: a
    host that can run an unprivileged sshd runs the test for real.
    """

    return _serve_or_skip(
        lambda: _LiveSshd(root, options, authorized_keys=authorized_keys)
    )


def _https_bind_skip_reason(exc: OSError) -> str:
    """Build the named skip reason for a loopback server that cannot start."""

    lowered = str(exc).lower()
    if isinstance(exc, ssl.SSLError):
        return f"loopback https unavailable: TLS context refused ({exc})"
    if exc.errno == errno.EADDRINUSE or "address already in use" in lowered:
        return f"loopback https unavailable: port unavailable ({exc})"
    return f"loopback https unavailable: server start refused ({exc})"


def _classify_https_probe_failure(stderr_text: str) -> str | None:
    """Name why a loopback-TLS probe fetch failed, if it names a capability.

    Returns the cause fragment for the skip reason, or None when the probe
    got past the capability (an HTTP status, an auth demand, anything the
    test itself must judge) or failed in a way this fixture cannot
    distinguish — unknown failures proceed so they fail loudly in the test
    rather than hide behind a skip. ``remote:`` relay lines are excluded: a
    response body echoing a token must not read as a capability absence.
    """

    records: list[str] = []
    for raw in stderr_text.split("\n"):
        line = raw[:-1].strip() if raw.endswith("\r") else raw.strip()
        if not line or line.lower().startswith("remote:"):
            continue
        records.append(line.lower())
    if any(
        fragment in record
        for record in records
        for fragment in (
            "couldn't connect to server",
            "connection refused",
            "failed to connect",
            "could not resolve host",
            "connection timed out",
            "operation timed out",
        )
    ):
        return "server unreachable"
    if any(
        token in record
        for record in records
        for token in ("ssl", "tls", "certificate", "schannel")
    ):
        return "TLS trust refused"
    return None


def _https_probe_skip_reason(stderr_text: str, cause: str) -> str:
    """Build the named skip reason for a loopback-TLS probe failure."""

    lines = [line.strip() for line in stderr_text.splitlines() if line.strip()]
    detail = lines[-1][:160] if lines else "no git output"
    return f"loopback https unavailable: {cause} ({detail})"


def _loopback_https_probe_environment(
    crt_path: str, root: Path
) -> dict[str, str]:
    """A clean git environment trusting only the loopback CA for the probe."""

    environment = git_admission._clean_discovery_environment()
    empty = root / "empty.gitconfig"
    empty.write_text("", encoding="utf-8")
    environment.update(
        {
            "GIT_SSL_CAINFO": crt_path,
            "GIT_CONFIG_GLOBAL": os.fspath(empty),
            "GIT_CONFIG_SYSTEM": os.fspath(empty),
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_TERMINAL_PROMPT": "0",
            "HOME": os.fspath(root),
            "XDG_CONFIG_HOME": os.fspath(root),
        }
    )
    return environment


_loopback_https_probe: dict[str, str | None] = {}


def _ensure_loopback_https_serves(key_path: str, crt_path: str) -> None:
    """Probe that git on this host completes loopback TLS trusting the CA.

    Real git runs ``ls-remote`` against a throwaway status server (never a
    test's server, whose ``auth_seen`` log the probe must not pollute). The
    result is cached per certificate: the capability cannot change mid-run.
    A probe failure naming a capability absence skips with that cause
    quoting git's own bytes; anything else — including a probe that itself
    errors — proceeds so the test judges the outcome.
    """

    if crt_path in _loopback_https_probe:
        cached = _loopback_https_probe[crt_path]
        if cached is not None:
            pytest.skip(cached)
        return
    probe_server = _serve_or_skip(
        lambda: _StatusServer(key_path, crt_path, status=503, body=b"")
    )
    try:
        with tempfile.TemporaryDirectory(prefix="csk-https-probe") as scratch:
            root = Path(scratch)
            try:
                completed = subprocess.run(
                    (os.fspath(_git_path()), "ls-remote", probe_server.url),
                    stdin=subprocess.DEVNULL,
                    capture_output=True,
                    text=True,
                    env=_loopback_https_probe_environment(crt_path, root),
                    cwd=root,
                    timeout=20,
                    check=False,
                )
            except (OSError, subprocess.SubprocessError):
                completed = None
    finally:
        probe_server.shutdown()
    reason: str | None = None
    if completed is not None and completed.returncode != 0:
        cause = _classify_https_probe_failure(completed.stderr)
        if cause is not None:
            reason = _https_probe_skip_reason(completed.stderr, cause)
    _loopback_https_probe[crt_path] = reason
    if reason is not None:
        pytest.skip(reason)


def _start_loopback_https_server(
    key_path: str,
    crt_path: str,
    *,
    status: int,
    body: bytes,
    content_type: str = "text/plain",
) -> _StatusServer:
    """Start a loopback HTTPS fixture, skipping with a named cause when it cannot.

    Construction failures (bind, TLS context) and a measured inability of
    git on this host to complete TLS trusting the fixture CA both skip; the
    skip is conditional on the capability actually being absent.
    """

    def start() -> _StatusServer:
        try:
            return _StatusServer(
                key_path,
                crt_path,
                status=status,
                body=body,
                content_type=content_type,
            )
        except OSError as exc:
            raise _RealServerUnavailable(
                _https_bind_skip_reason(exc)
            ) from exc

    server = _serve_or_skip(start)
    _ensure_loopback_https_serves(key_path, crt_path)
    return server


class _RefusingAgent:
    """A minimal SSH agent that lists one key and refuses every signature."""

    def __init__(self, root: Path) -> None:
        root.mkdir(parents=True, exist_ok=True)
        private = root / "agentkey"
        if not private.exists():
            subprocess.run(
                (
                    "ssh-keygen",
                    "-q",
                    "-t",
                    "ed25519",
                    "-f",
                    os.fspath(private),
                    "-N",
                    "",
                    "-C",
                    "refusing-agent-key",
                ),
                check=True,
                capture_output=True,
            )
        public = (private.parent / "agentkey.pub").read_text(
            encoding="utf-8"
        )
        self._key_blob = base64.b64decode(public.split()[1])
        # AF_UNIX paths are capped near 104 bytes on macOS, well below
        # what a pytest tmp_path spends on the test name alone.
        self._socket_dir = Path(tempfile.mkdtemp(prefix="csk-ragent-"))
        self.socket_path = (self._socket_dir / "agent.sock").resolve()
        self.seen: list[str] = []
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._serve, daemon=True)

    def _serve(self) -> None:
        listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        listener.bind(os.fspath(self.socket_path))
        listener.listen(5)
        listener.settimeout(0.2)
        while not self._stop.is_set():
            try:
                connection, _ = listener.accept()
            except TimeoutError:
                continue
            with connection:
                connection.settimeout(5)
                try:
                    while True:
                        header = connection.recv(4)
                        if len(header) < 4:
                            break
                        (length,) = struct.unpack(">I", header)
                        payload = b""
                        while len(payload) < length:
                            chunk = connection.recv(length - len(payload))
                            if not chunk:
                                break
                            payload += chunk
                        if not payload:
                            break
                        message = payload[0]
                        names = {11: "REQUEST_IDENTITIES", 13: "SIGN_REQUEST"}
                        self.seen.append(names.get(message, f"UNKNOWN({message})"))
                        if message == 11:
                            comment = b"refusing-agent-key"
                            body = struct.pack(">I", 1)
                            body += struct.pack(">I", len(self._key_blob))
                            body += self._key_blob
                            body += struct.pack(">I", len(comment)) + comment
                            reply = bytes([12]) + body
                        else:
                            reply = bytes([5])
                        connection.sendall(
                            struct.pack(">I", len(reply)) + reply
                        )
                except (OSError, struct.error):
                    pass
        listener.close()

    def __enter__(self) -> Self:
        self._thread.start()
        return self

    def __exit__(self, *args: object) -> None:
        self._stop.set()
        self._thread.join(timeout=5)
        shutil.rmtree(self._socket_dir, ignore_errors=True)


def _ssh_rejection_line(target: str, methods: str) -> str:
    """Render the OpenSSH terminal rejection frame for ``methods``.

    The user@host prefix echoes the invoked target; the method list is the
    server's offer.  The live-sshd equality test proves these bytes are
    exactly what real ssh prints; the stand-in CLASS tests prove the lane
    behaviour on them.
    """

    return f"{target}: Permission denied ({methods})."


def _v1_plan(fallback: str = "availability-auth") -> repository_policy.ResolutionPlan:
    return repository_policy.select_endpoints(
        repository_policy.parse_policy(
            {
                "schema_version": 1,
                "repositories": {
                    IDENTITY: {
                        "endpoints": [
                            {"url": "https://example.org/kit.git", "authentication": "first"},
                            {"url": "git@example.org:kit.git", "authentication": "second"},
                        ],
                        "fallback": fallback,
                    }
                },
            },
            reader_revision=1,
        ),
        IDENTITY,
    )


def _v2_plan(
    endpoints: list[dict[str, object]],
    *,
    aliases: dict[str, object] | None = None,
    fallback: str = "availability-auth",
) -> repository_policy.ResolutionPlan:
    raw: dict[str, object] = {
        "schema_version": 2,
        "repositories": {
            IDENTITY: {
                "endpoints": endpoints,
                "fallback": fallback,
            }
        },
    }
    if aliases is not None:
        raw["aliases"] = aliases
    return repository_policy.select_endpoints(
        repository_policy.parse_policy(raw, reader_revision=2), IDENTITY
    )


@pytest.mark.parametrize(
    ("stderr", "classification"),
    [
        (
            "fatal: unable to access 'https://example.org/kit.git': "
            + "The requested URL returned error: 503",
            "http-503",
        ),
        (
            "fatal: unable to access 'https://example.org/kit.git': "
            + "Could not resolve host: example.org",
            "dns",
        ),
        ("ssh: connect to host example.org port 22: Connection refused", "connection-refused"),
        ("git@example.org: Permission denied (publickey).", "ssh-auth-rejected"),
        ("git@example.org: Permission denied (password).", "ssh-auth-rejected"),
        (
            "git@example.org: Permission denied (keyboard-interactive).",
            "ssh-auth-rejected",
        ),
        (
            "git@example.org: Permission denied (publickey,password).",
            "ssh-auth-rejected",
        ),
        ("Host key verification failed.", "host-key"),
        (
            "fatal: unable to access 'https://example.org/kit.git': "
            + "SSL certificate problem: unable to get local issuer certificate",
            "tls",
        ),
        (
            "fatal: Authentication failed for 'https://example.org/kit.git/'",
            "auth-rejected",
        ),
        ("fatal: Authentication failed", "auth-rejected"),
        (
            "fatal: could not read Username for 'https://example.org': "
            + "terminal prompts disabled",
            "auth-unavailable",
        ),
        (
            "fatal: repository 'https://example.org/kit.git/' not found",
            "identity",
        ),
        (
            "fatal: unable to access 'https://example.org/kit.git': "
            + "The requested URL returned error: 302",
            "redirect",
        ),
    ],
)
def test_git_failure_classifier_accepts_only_complete_transport_records(
    stderr: str, classification: str
) -> None:
    assert git_admission._classify_git_failure_output(stderr) == classification


def test_windows_schannel_tls_record_classifies_as_tls() -> None:
    """The Windows Schannel TLS spelling is evidence for the tls class.

    Verbatim envelope captured through the production lane
    (``git_admission.acquire_network``) on windows-latest, git
    2.55.0.windows.5, hosted run 35221674975: a loopback fetch against a
    self-signed certificate fails in Schannel rather than OpenSSL, so the
    record names the backend instead of a certificate problem.
    """

    envelope = (
        b"fatal: unable to access 'https://127.0.0.1:50251/kit.git/': "
        b"schannel: SEC_E_UNTRUSTED_ROOT (0x80090325) - The certificate "
        b"chain was issued by an authority that is not trusted.\n"
    )
    assert git_admission._classify_git_failure_output(envelope) == "tls"
    record = envelope.decode("utf-8").strip().lower()
    assert git_admission._evidence_class(record) == "tls"


def test_windows_connection_refused_colon_spelling_classifies() -> None:
    """The newer refused spelling is evidence for the connection-refused class.

    Verbatim envelope captured through the production lane
    (``git_admission.acquire_network``) on windows-latest, git
    2.55.0.windows.5, hosted run 35229841321: a fetch against a port the
    probe proved closed at TCP level fails with ``host:port`` rendering and
    ``Could not connect to server`` where the older spelling has
    ``host port N`` and ``Couldn't connect to server``.
    """

    envelope = (
        b"fatal: unable to access 'https://127.0.0.1:49844/kit.git/': "
        b"Failed to connect to 127.0.0.1:49844 after 2057 ms: "
        b"Could not connect to server\n"
    )
    assert (
        git_admission._classify_git_failure_output(envelope) == "connection-refused"
    )
    record = envelope.decode("utf-8").strip().lower()
    assert git_admission._evidence_class(record) == "connection-refused"


# Lines that carry no transport outcome: server relay, client-side helper
# diagnostics, the advisory footer, and unknown output.  Each is ignored
# alone (no evidence, fail closed) and ignored beside a real record (the
# real record decides).  Shared by the direct partition pin and the
# real-fetch seam test so both prove the same rule.
_NON_EVIDENCE_LINES = [
    "remote: <html><body>Service Temporarily Unavailable</body></html>",
    "remote: forged: ssh: connect to host x port 22: Connection refused",
    "remote: forged 503 Service Unavailable",
    "fatal: Could not read from remote repository.",
    "fatal: The remote end hung up unexpectedly",
    "Please make sure you have the correct access rights",
    "and the repository exists.",
    "Please make sure you have the correct access rights!",
    "fatal: Please make sure you have the correct access rights",
    "and the repository exists. Really.",
    "error: unable to read askpass response from '/tmp/csk-askpass'",
    (
        'sign_and_send_pubkey: signing failed for RSA "/tmp/operator-key"'
        " from agent: agent refused operation"
    ),
    (
        'sign_and_send_pubkey: signing failed for ED25519 "/tmp/operator-key.pub"'
        " from agent: agent refused operation"
    ),
    '@    WARNING: REMOTE HOST IDENTIFICATION HAS CHANGED!     @',
    "Offending ED25519 key in /tmp/known_hosts:1",
    "Host key for example.org has changed and you have requested strict checking.",
    "some future tool line nobody has seen yet",
    (
        "fatal: unable to access 'https://example.org/kit.git': "
        "The requested URL returned error: 404"
    ),
    "fatal: unable to access 'https://example.org/kit.git': schannel:",
    (
        "remote: fatal: unable to access 'https://example.org/kit.git': "
        "schannel: SEC_E_UNTRUSTED_ROOT (0x80090325)"
    ),
    (
        "fatal: unable to access 'https://example.org/kit.git': "
        "Failed to connect to example.org:443: Could not connect to server"
    ),
    (
        "remote: fatal: unable to access 'https://example.org/kit.git': "
        "Failed to connect to example.org:443 after 12 ms: "
        "Could not connect to server"
    ),
    (
        "fatal: unable to access 'https://example.org/kit.git': "
        "Failed to connect to example.org:443 after 12 ms: "
        "Could not connect to serve"
    ),
]

_NON_EVIDENCE_IDS = [
    "remote-html-body",
    "remote-forged-refused",
    "remote-forged-503",
    "git-could-not-read",
    "git-hung-up",
    "advice-footer-1",
    "advice-footer-2",
    "near-miss-advice-bang",
    "near-miss-advice-fatal",
    "near-miss-advice-really",
    "askpass-error",
    "signing-rsa-refused",
    "signing-ed25519-refused",
    "host-key-warning-banner",
    "host-key-offending-key",
    "host-key-changed-notice",
    "unknown-future-line",
    "unreachable-404-record",
    "incomplete-schannel-record",
    "remote-forged-schannel",
    "incomplete-refused-colon-spelling",
    "remote-forged-refused-colon-spelling",
    "truncated-refused-tail",
]


@pytest.mark.parametrize("line", _NON_EVIDENCE_LINES, ids=_NON_EVIDENCE_IDS)
def test_non_evidence_lines_carry_no_outcome(line: str) -> None:
    """A line that states no transport outcome is neither evidence nor conflict."""

    assert git_admission._classify_git_failure_output(line) == "unclassified"
    assert git_admission._evidence_class(line.strip().lower()) is None
    assert (
        git_admission._classify_git_failure_output(
            "git@example.org: Permission denied (publickey).\n" + line
        )
        == "ssh-auth-rejected"
    )
    assert git_admission._classify_git_failure_output(None) == "unclassified"
    assert git_admission._classify_git_failure_output("") == "unclassified"


@pytest.mark.parametrize(
    "stderr",
    [
        "fatal: couldn't find remote ref refs/tags/release-401",
        "fatal: repository 'https://example.org/503.git/' not found",
        "fatal: protocol error: bad line length character: 502",
        "error: corrupt object 401; fsck failed",
        "fatal: unable to follow redirect to connection refused",
    ],
)
def test_adversarial_failure_text_cannot_open_a_fallback(
    stderr: str, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The real fetch call, not a pre-classified enum, drives this gate."""

    plan = _v1_plan()
    tool = _fake_http_tool(tmp_path / "tool", {})
    real_run = git_admission.subprocess.run
    fetches: list[object] = []

    def injected_run(*args: object, **kwargs: object) -> object:
        command = args[0]
        assert isinstance(command, tuple)
        if "fetch" in command:
            fetches.append(command)
            raise subprocess.CalledProcessError(
                128, command, stderr=stderr.encode("utf-8")
            )
        return real_run(*args, **kwargs)

    monkeypatch.setattr(git_admission.subprocess, "run", injected_run)
    with pytest.raises(git_admission.GitAdmissionError) as captured:
        transport.acquire_plan(plan, LOCK, tool)
    assert captured.value.code != git_admission.SOURCE_UNAVAILABLE
    assert len(fetches) == 1


def test_complete_http_503_record_drives_real_fallback_classification(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The token-preserving classifier mutant must fail at the fetch seam."""

    first_url = "https://example.org/kit.git"
    second_url = "https://mirror.example.org/kit.git"
    plan = _v2_plan(
        [
            {"url": first_url, "authentication": "first"},
            {
                "url": second_url,
                "authentication": "second",
                "mirror_of": IDENTITY,
            },
        ]
    )
    bare, commit = _bare_repository(tmp_path / "bare")
    tool = _fake_http_tool(tmp_path / "tool", {second_url: bare})
    real_run = git_admission.subprocess.run
    fetches: list[object] = []
    injected = False

    def inject_503(*args: object, **kwargs: object) -> object:
        nonlocal injected
        command = args[0]
        assert isinstance(command, tuple)
        if "fetch" in command:
            fetches.append(command)
            if not injected:
                injected = True
                stderr = (
                    f"fatal: unable to access '{first_url}': "
                    "The requested URL returned error: 503"
                )
                raise subprocess.CalledProcessError(
                    128, command, stderr=stderr.encode("utf-8")
                )
        return real_run(*args, **kwargs)

    monkeypatch.setattr(git_admission.subprocess, "run", inject_503)
    result = transport.acquire_plan(plan, LockedCommit("sha1", commit), tool)

    assert result.snapshot.commit == commit
    assert result.attempt_count == 2
    assert len(fetches) == 2
    assert result.attempts[0].classification == "http-503"


def test_windows_refused_spelling_opens_the_mandated_fallback(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The verbatim Windows refused bytes reach the second endpoint for real.

    The first fetch fails with the exact envelope git 2.55.0.windows.5
    writes for a closed port (hosted run 35229841321); the second fetch
    runs against a local bare repository and proves the locked commit.
    """

    first_url = "https://example.org/kit.git"
    second_url = "https://mirror.example.org/kit.git"
    plan = _v2_plan(
        [
            {"url": first_url, "authentication": "first"},
            {
                "url": second_url,
                "authentication": "second",
                "mirror_of": IDENTITY,
            },
        ]
    )
    bare, commit = _bare_repository(tmp_path / "bare")
    tool = _fake_http_tool(tmp_path / "tool", {second_url: bare})
    real_run = git_admission.subprocess.run
    fetches: list[object] = []
    injected = False

    def inject_refused(*args: object, **kwargs: object) -> object:
        nonlocal injected
        command = args[0]
        assert isinstance(command, tuple)
        if "fetch" in command:
            fetches.append(command)
            if not injected:
                injected = True
                raise subprocess.CalledProcessError(
                    128,
                    command,
                    stderr=(
                        b"fatal: unable to access "
                        b"'https://127.0.0.1:49844/kit.git/': Failed to connect "
                        b"to 127.0.0.1:49844 after 2057 ms: "
                        b"Could not connect to server\n"
                    ),
                )
        return real_run(*args, **kwargs)

    monkeypatch.setattr(git_admission.subprocess, "run", inject_refused)
    result = transport.acquire_plan(plan, LockedCommit("sha1", commit), tool)

    assert result.snapshot.commit == commit
    assert result.attempt_count == 2
    assert len(fetches) == 2
    assert result.attempts[0].classification == "connection-refused"


@pytest.mark.parametrize(
    "failure_class",
    [
        "tls",
        "host-key",
        "integrity",
        "identity",
        "ref-moved",
        "audit",
        "revocation",
        "canary",
        "assurance",
        "capability",
        "policy-unreadable",
        "http-404",
        "redirect",
        "malformed-response",
        "partial-response",
        "unknown",
    ],
)
def test_fail_closed_classes_never_try_the_second_endpoint(failure_class: str) -> None:
    calls: list[str] = []
    plan = _v1_plan()

    def attempt(**kwargs: object) -> git_admission.Snapshot:
        endpoint = kwargs["endpoint"]
        assert isinstance(endpoint, repository_policy.ResolvedEndpoint)
        calls.append(endpoint.url)
        raise transport.TransportFailure(
            failure_class, "token=do-not-leak", code="build_repository_specific_failure"
        )

    with pytest.raises(transport.TransportFailure) as captured:
        transport.acquire_plan(plan, LOCK, attempt=attempt)
    assert captured.value.failure_class == failure_class
    assert captured.value.code != git_admission.SOURCE_UNAVAILABLE
    assert calls == [plan.endpoints[0].url]


@pytest.mark.parametrize(
    "failure_class",
    [
        "dns",
        "connection-refused",
        "timeout",
        "endpoint-unavailable",
        "http-502",
        "http-503",
        "http-504",
        "auth-unavailable",
        "auth-rejected",
        "ssh-auth-rejected",
        "http-401",
        "http-403",
    ],
)
def test_positive_availability_auth_failure_opens_exactly_one_alternate(
    failure_class: str,
) -> None:
    calls: list[str] = []
    plan = _v1_plan()

    def attempt(**kwargs: object) -> git_admission.Snapshot:
        endpoint = kwargs["endpoint"]
        assert isinstance(endpoint, repository_policy.ResolvedEndpoint)
        calls.append(endpoint.url)
        if len(calls) == 1:
            raise transport.TransportFailure(failure_class, "first endpoint unavailable")
        return _snapshot()

    result = transport.acquire_plan(plan, LOCK, attempt=attempt)
    assert result.snapshot.commit == LOCK.hex
    assert result.attempt_count == 2
    assert calls == [endpoint.url for endpoint in plan.endpoints]


def test_availability_auth_failure_set_is_exactly_the_spec_set() -> None:
    """Widening the fallback set must break loudly, not silently admit more.

    ``connection-refused`` belongs here (the mandated fallback); every other
    value stays fail-closed through ``classify_failure``.
    """

    assert repository_policy.AVAILABILITY_AUTH_FAILURES == frozenset(
        {
            "dns",
            "connection-refused",
            "timeout",
            "endpoint-unavailable",
            "http-502",
            "http-503",
            "http-504",
            "auth-unavailable",
            "auth-rejected",
            "ssh-auth-rejected",
            "http-401",
            "http-403",
        }
    )
    assert (
        repository_policy.classify_failure("connection-refused")
        == repository_policy.FALLBACK_AVAILABILITY_AUTH
    )
    assert (
        repository_policy.classify_failure("unclassified")
        != repository_policy.FALLBACK_AVAILABILITY_AUTH
    )


def test_each_endpoint_receives_a_separate_authentication_provider_selection() -> None:
    plan = _v1_plan()
    providers: list[str | None] = []

    def provider(endpoint: repository_policy.ResolvedEndpoint) -> git_admission.GitTool:
        providers.append(endpoint.authentication)
        return _real_tool()

    def attempt(**kwargs: object) -> git_admission.Snapshot:
        if kwargs["endpoint"] == plan.endpoints[0]:
            raise transport.TransportFailure("auth-rejected")
        return _snapshot()

    result = transport.acquire_plan(
        plan,
        LOCK,
        tool_for_endpoint=provider,
        attempt=attempt,
    )
    assert result.snapshot.commit == LOCK.hex
    assert providers == ["first", "second"]


def test_two_attempts_share_one_total_deadline() -> None:
    plan = _v1_plan()
    observed_limits: list[float] = []

    def attempt(**kwargs: object) -> git_admission.Snapshot:
        limits = kwargs["limits"]
        assert isinstance(limits, git_admission.Limits)
        observed_limits.append(limits.timeout_seconds)
        if len(observed_limits) == 1:
            raise transport.TransportFailure("dns")
        return _snapshot()

    result = transport.acquire_plan(
        plan,
        LOCK,
        limits=git_admission.Limits(timeout_seconds=5.0),
        attempt=attempt,
    )
    assert result.attempt_count == 2
    assert len(observed_limits) == 2
    assert all(0 < value <= 5.0 for value in observed_limits)
    assert observed_limits[1] <= observed_limits[0]


def test_total_deadline_bounds_elapsed_time_across_both_attempts() -> None:
    """One total deadline bounds both attempts; the second gets the remainder.

    Proven against a driven clock, so the proof is identical on an idle
    laptop and a saturated runner: no sleeping, exact arithmetic.  The
    second attempt must observe the first budget minus what elapsed
    (0.17), never a fresh ``timeout_seconds`` (0.2).
    """

    plan = _v1_plan()
    calls: list[str] = []
    observed_limits: list[float] = []

    class _DrivenClock:
        """A monotonic source the injected attempt advances by fixed slices."""

        def __init__(self) -> None:
            self.now = 0.0

        def __call__(self) -> float:
            return self.now

        def advance(self, seconds: float) -> None:
            self.now += seconds

    clock = _DrivenClock()
    started = clock()

    def attempt(**kwargs: object) -> git_admission.Snapshot:
        endpoint = kwargs["endpoint"]
        assert isinstance(endpoint, repository_policy.ResolvedEndpoint)
        limits = kwargs["limits"]
        assert isinstance(limits, git_admission.Limits)
        calls.append(endpoint.url)
        observed_limits.append(limits.timeout_seconds)
        clock.advance(0.03)
        if len(calls) == 1:
            raise transport.TransportFailure("dns")
        return _snapshot()

    result = transport.acquire_plan(
        plan,
        LOCK,
        limits=git_admission.Limits(timeout_seconds=0.2),
        attempt=attempt,
        clock=clock,
    )
    elapsed = clock() - started
    assert result.attempt_count == 2
    assert calls == [endpoint.url for endpoint in plan.endpoints]
    assert observed_limits == [0.2, 0.17]
    assert elapsed < 0.2


def test_late_success_is_refused_instead_of_returning_after_the_deadline() -> None:
    calls: list[str] = []
    plan = repository_policy.ResolutionPlan(
        identity=IDENTITY,
        endpoints=(_v1_plan("none").endpoints[0],),
        fallback="none",
        pinned=False,
    )

    def late_attempt(**kwargs: object) -> git_admission.Snapshot:
        endpoint = kwargs["endpoint"]
        assert isinstance(endpoint, repository_policy.ResolvedEndpoint)
        calls.append(endpoint.url)
        time.sleep(0.04)
        return _snapshot()

    with pytest.raises(transport.TransportResolutionError) as captured:
        transport.acquire_plan(
            plan,
            LOCK,
            limits=git_admission.Limits(timeout_seconds=0.01),
            attempt=late_attempt,
        )
    assert captured.value.attempts[0].classification == "timeout"
    assert calls == [plan.endpoints[0].url]


@pytest.mark.parametrize(
    ("stages", "delay", "budget", "expect"),
    [
        ("init", 0.6, 6.0, "success"),
        ("fetch", 0.6, 6.0, "success"),
        ("both", 3.0, 6.0, "refusal"),
    ],
)
def test_shared_deadline_reaches_real_lane(
    stages: str, delay: float, budget: float, expect: str, tmp_path: Path
) -> None:
    """One absolute budget covers the real lane's init and fetch stages.

    The trusted Git wrapper is delayed at the named stages while the
    transport budget stays fixed: a single delayed stage still verifies
    within budget, delaying both exceeds the budget and refuses.  This is
    the outer cumulative-budget regression, not exhaustive per-stage
    certification.
    """

    bare, commit = _bare_repository(tmp_path / "bare")
    url = "https://example.org/kit.git"
    plan = _v2_plan([{"url": url, "authentication": "team-https"}])
    tool = _fake_http_tool(tmp_path / "tool", {url: bare})
    condition = {
        "init": "'init' in sys.argv",
        "fetch": "'fetch' in sys.argv",
        "both": "'init' in sys.argv or 'fetch' in sys.argv",
    }[stages]
    payload = _wrapper_payload_path(tool.executable)
    script = payload.read_text(encoding="utf-8")
    script = script.replace(
        "args = [",
        f"import time\nif {condition}: time.sleep({delay})\nargs = [",
    )
    assert "time.sleep" in script, "the delay must land in the payload"
    payload.write_text(script, encoding="utf-8")
    started = time.monotonic()
    if expect == "success":
        result = transport.acquire_plan(
            plan,
            LockedCommit("sha1", commit),
            tool,
            limits=git_admission.Limits(timeout_seconds=budget),
        )
        elapsed = time.monotonic() - started
        assert result.snapshot.commit == commit
        assert result.attempt_count == 1
        # Within-budget is proven by the success return itself: production
        # refuses a late success.  The stopwatch guards, in the load-safe
        # direction, that the injected delay actually fired.
        assert elapsed >= delay
    else:
        with pytest.raises(git_admission.GitAdmissionError):
            transport.acquire_plan(
                plan,
                LockedCommit("sha1", commit),
                tool,
                limits=git_admission.Limits(timeout_seconds=budget),
            )
        elapsed = time.monotonic() - started
        # Enforcement is proven by the refusal itself.  The stopwatch guards,
        # in the load-safe direction, that the budget was genuinely waited
        # out rather than failed fast; the 1.0 s slack covers the
        # subprocess-kill timing epsilon, and runner load only pushes
        # elapsed up from here.
        assert elapsed >= budget - 1.0


def test_alias_connection_target_preserves_ssh_path_and_selected_port() -> None:
    plan = _v2_plan(
        [
            {
                "url": "git@example.org:kit.git",
                "authentication": "team-ssh",
                "alias": "corp-mirror",
                "mirror_of": IDENTITY,
            }
        ],
        aliases={
            "corp-mirror": {
                "host": "mirror.corp.example",
                "port": 2222,
                "authentication": "team-ssh",
            }
        },
    )

    connection = transport._connection_target(plan.endpoints[0])

    assert connection.remote_url == "ssh://git@mirror.corp.example:2222/kit.git"
    assert connection.ssh_host == "git@mirror.corp.example"
    assert connection.repository_path == "/kit.git"
    assert connection.connect_port == 2222

    host_only = _v2_plan(
        [
            {
                "url": "git@example.org:kit.git",
                "authentication": "team-ssh",
                "alias": "corp-mirror",
                "mirror_of": IDENTITY,
            }
        ],
        aliases={
            "corp-mirror": {
                "host": "mirror.corp.example",
                "authentication": "team-ssh",
            }
        },
    )
    host_only_connection = transport._connection_target(host_only.endpoints[0])
    assert host_only_connection.remote_url == "git@mirror.corp.example:kit.git"
    assert host_only_connection.repository_path == "kit.git"


def test_skillfile_alias_port_uses_the_trusted_git_lane(
    tmp_path: Path,
) -> None:
    bare, commit = _bare_repository(tmp_path / "fixture")
    plan = _v2_plan(
        [
            {
                "url": "https://example.org/kit.git",
                "authentication": "team-https",
                "alias": "corp-mirror",
                "mirror_of": IDENTITY,
            }
        ],
        aliases={
            "corp-mirror": {
                "host": "mirror.corp.example",
                "port": 8443,
                "authentication": "team-https",
            }
        },
        fallback="none",
    )
    connection = transport._connection_target(plan.endpoints[0])
    tool = _fake_http_tool(
        tmp_path / "git-wrapper",
        {connection.remote_url: bare},
    )

    result = transport.acquire_plan(
        plan,
        LockedCommit("sha1", commit),
        tool,
    )

    assert result.snapshot.commit == commit
    assert result.attempt_count == 1
    assert result.attempts[0].endpoint.resolved_host == "mirror.corp.example"
    assert result.attempts[0].endpoint.resolved_port == 8443


def test_trusted_ssh_command_carries_a_selected_nondefault_port(
    tmp_path: Path,
) -> None:
    expected_host = "git@mirror.corp.example"
    expected_upload = "git-upload-pack '/kit.git'"
    # The pinned paths must be absolute on the host that runs this test: a
    # POSIX-absolute literal such as ``/private/wrapper`` is drive-relative
    # on Windows and the policy guard correctly refuses it, so the fixture
    # uses real absolute paths instead of literals.
    wrapper = tmp_path / "wrapper"
    command = git_admission.exact_ssh_command(
        git_admission.SSHPolicy(
            wrapper=wrapper,
            ssh=tmp_path / "ssh",
            expected_host=expected_host,
            repository_path="/kit.git",
            empty_config=tmp_path / "config",
            known_hosts=tmp_path / "known_hosts",
            empty_known_hosts=tmp_path / "empty_known_hosts",
            identity=tmp_path / "id",
            connect_port=2222,
        ),
        # The invoked argv names the same path the policy pins: spelling it
        # from the policy keeps the literal separator-correct on Windows.
        (os.fspath(wrapper), expected_host, expected_upload),
    )

    port_index = command.index("-p")
    assert command[port_index + 1] == "2222"
    assert command[-2:] == (expected_host, expected_upload)


@pytest.mark.parametrize(
    "field",
    ["wrapper", "ssh", "empty_config", "known_hosts", "empty_known_hosts"],
)
def test_ssh_command_policy_refuses_a_non_absolute_pinned_path(
    field: str, tmp_path: Path
) -> None:
    """Every pinned tool path must be absolute; a relative one fails closed.

    This is the mechanism behind the windows-latest failure of the selected
    port test above: ``Path("/private/wrapper")`` is not absolute there, so
    the guard raised ``IDENTITY_INVALID`` before any command was built.
    """

    paths = {
        "wrapper": tmp_path / "wrapper",
        "ssh": tmp_path / "ssh",
        "empty_config": tmp_path / "config",
        "known_hosts": tmp_path / "known_hosts",
        "empty_known_hosts": tmp_path / "empty_known_hosts",
    }
    paths[field] = Path("relative") / field
    policy = git_admission.SSHPolicy(
        wrapper=paths["wrapper"],
        ssh=paths["ssh"],
        expected_host="git@mirror.corp.example",
        repository_path="/kit.git",
        empty_config=paths["empty_config"],
        known_hosts=paths["known_hosts"],
        empty_known_hosts=paths["empty_known_hosts"],
        identity=tmp_path / "id",
    )
    argv = (
        os.fspath(policy.wrapper),
        policy.expected_host,
        "git-upload-pack '/kit.git'",
    )
    with pytest.raises(git_admission.GitAdmissionError) as captured:
        git_admission.exact_ssh_command(policy, argv)
    assert captured.value.code == git_admission.IDENTITY_INVALID


def test_none_and_pin_refuse_fallback_after_positive_failure() -> None:
    for plan in (_v1_plan("none"), _pinned_plan()):
        calls: list[str] = []

        def attempt(
            *, calls: list[str] = calls, **kwargs: object
        ) -> git_admission.Snapshot:
            endpoint = kwargs["endpoint"]
            assert isinstance(endpoint, repository_policy.ResolvedEndpoint)
            calls.append(endpoint.url)
            raise transport.TransportFailure("auth-rejected", "operator rejection")

        with pytest.raises(transport.TransportResolutionError) as captured:
            transport.acquire_plan(plan, LOCK, attempt=attempt)
        assert captured.value.code == repository_policy.CODE_ENDPOINT_UNAVAILABLE
        assert len(calls) == 1


def test_plan_bounds_expose_no_alternate_for_pinned_or_singleton() -> None:
    """Document the policy layer's own bound before acquisition runs."""

    plan = _v1_plan()
    pinned_with_alternate = repository_policy.ResolutionPlan(
        identity=plan.identity,
        endpoints=plan.endpoints,
        fallback="availability-auth",
        pinned=True,
    )
    assert pinned_with_alternate.next_endpoint("dns") is None
    three = repository_policy.ResolutionPlan(
        identity=plan.identity,
        endpoints=plan.endpoints + (plan.endpoints[0],),
        fallback=plan.fallback,
        pinned=False,
    )
    assert three.max_attempts == 2
    single_plan = repository_policy.ResolutionPlan(
        plan.identity, (plan.endpoints[0],), "availability-auth", False
    )
    assert single_plan.next_endpoint("connection-refused") is None
    none_plan = repository_policy.ResolutionPlan(
        plan.identity, plan.endpoints, "none", False
    )
    assert none_plan.next_endpoint("dns") is None


def test_pinned_plan_never_exposes_an_alternate_even_if_one_is_present() -> None:
    """A pinned plan attempts once through the production acquisition path."""

    plan = _v1_plan()
    pinned_with_alternate = repository_policy.ResolutionPlan(
        identity=plan.identity,
        endpoints=plan.endpoints,
        fallback="availability-auth",
        pinned=True,
    )
    calls: list[str] = []

    def attempt(**kwargs: object) -> git_admission.Snapshot:
        endpoint = kwargs["endpoint"]
        assert isinstance(endpoint, repository_policy.ResolvedEndpoint)
        calls.append(endpoint.url)
        raise transport.TransportFailure("dns")

    with pytest.raises(transport.TransportResolutionError) as captured:
        transport.acquire_plan(pinned_with_alternate, LOCK, attempt=attempt)
    assert captured.value.code == repository_policy.CODE_ENDPOINT_UNAVAILABLE
    assert calls == [plan.endpoints[0].url]


def _pinned_plan() -> repository_policy.ResolutionPlan:
    plan = _v1_plan()
    return repository_policy.ResolutionPlan(
        identity=plan.identity,
        endpoints=(plan.endpoints[0],),
        fallback=plan.fallback,
        pinned=True,
    )


def test_attempts_are_bounded_and_never_retry_one_endpoint() -> None:
    """Three listed endpoints still yield at most two production attempts."""

    plan = _v1_plan()
    three = repository_policy.ResolutionPlan(
        identity=plan.identity,
        endpoints=plan.endpoints + (plan.endpoints[0],),
        fallback=plan.fallback,
        pinned=False,
    )
    calls: list[str] = []

    def attempt(**kwargs: object) -> git_admission.Snapshot:
        endpoint = kwargs["endpoint"]
        assert isinstance(endpoint, repository_policy.ResolvedEndpoint)
        calls.append(endpoint.url)
        raise transport.TransportFailure("connection-refused")

    with pytest.raises(transport.TransportResolutionError):
        transport.acquire_plan(three, LOCK, attempt=attempt)
    assert calls == [endpoint.url for endpoint in plan.endpoints]

    calls.clear()
    single_plan = repository_policy.ResolutionPlan(
        plan.identity, (plan.endpoints[0],), "availability-auth", False
    )
    with pytest.raises(transport.TransportResolutionError):
        transport.acquire_plan(single_plan, LOCK, attempt=attempt)
    assert calls == [plan.endpoints[0].url]


def test_exhaustion_diagnostic_contains_classes_but_no_secret_or_provider_name() -> None:
    plan = _v1_plan()

    def attempt(**kwargs: object) -> git_admission.Snapshot:
        if kwargs["endpoint"] == plan.endpoints[0]:
            raise transport.TransportFailure("dns", "token=super-secret")
        raise transport.TransportFailure("auth-rejected", "credential-reference=private")

    with pytest.raises(transport.TransportResolutionError) as captured:
        transport.acquire_plan(plan, LOCK, attempt=attempt)
    error = captured.value
    rendered = str(error) + error.detail + json.dumps([item.as_dict() for item in error.attempts])
    assert "super-secret" not in rendered
    assert "private" not in rendered
    assert "first" not in rendered and "second" not in rendered
    assert "dns" in rendered and "auth-rejected" in rendered
    assert "remediation:" in rendered


def test_transport_provenance_stays_out_of_lock_and_package_identity() -> None:
    plan = _v2_plan(
        [
            {
                "url": "https://example.org/kit.git",
                "authentication": "team-https",
                "alias": "corp-mirror",
                "mirror_of": IDENTITY,
            }
        ],
        aliases={
            "corp-mirror": {
                "host": "mirror.example.net",
                "authentication": "team-https",
            }
        },
    )
    result = transport.acquire_plan(plan, LOCK, attempt=lambda **_: _snapshot())
    effective = EffectiveState(
        identity_kind="network-git",
        identity=IDENTITY,
        transport="https",
        object_format=LOCK.object_format,
        commit=LOCK.hex,
    )
    package_key = snapshot_key(effective, result.snapshot.digest)
    declared = DeclaredState(
        repository="source",
        identity=IDENTITY,
        transport="https",
        object_format=LOCK.object_format,
        commit=LOCK.hex,
    )
    request = PipelineRequest(
        operation=Operation.AUDIT,
        command="tool",
        target="tool",
        declared=declared,
        effective=effective,
        acquire=lambda: result.snapshot,
        audit=lambda _: None,
    )
    receipt = receipt_input(
        request,
        effective,
        BuildTarget("tool", "go-v1", ".", "."),
        result.snapshot.digest,
        CompilerIdentity(
            content_sha256="sha256:" + "a" * 64,
            go_version="go1.25.1",
            go_relpath="bin/go",
            goos="darwin",
            goarch="arm64",
            tuning={},
        ),
    )
    rendered = json.dumps(receipt, sort_keys=True)
    assert package_key == snapshot_key(effective, result.snapshot.digest)
    assert plan.identity == IDENTITY
    assert plan.endpoints[0].provenance.alias == "corp-mirror"
    assert "corp-mirror" not in rendered
    assert plan.endpoints[0].provenance.listed_url not in rendered
    assert LOCK == LockedCommit("sha1", "0" * 40)


def test_untrusted_failure_code_is_not_diagnostic_data() -> None:
    failure = transport.TransportFailure(
        "tls",
        "credential=super-secret",
        code="credential-reference-super-secret",
    )

    assert failure.code == "build_repository_transport_failure"
    assert "super-secret" not in str(failure)
    assert "super-secret" not in failure.detail


def test_fault_at_trusted_git_call_site_becomes_structured_unclassified_refusal(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    plan = _v1_plan()
    tool = _real_tool()
    reached = 0

    def succeeds(*args: object, **kwargs: object) -> git_admission.Snapshot:
        nonlocal reached
        reached += 1
        return _snapshot()

    monkeypatch.setattr(git_admission, "acquire_network", succeeds)
    positive = transport.acquire_plan(plan, LOCK, tool)
    assert positive.snapshot.commit == LOCK.hex
    assert reached == 1

    def injected_fault(*args: object, **kwargs: object) -> git_admission.Snapshot:
        nonlocal reached
        reached += 1
        raise PermissionError("credential=not-for-diagnostics")

    monkeypatch.setattr(git_admission, "acquire_network", injected_fault)
    with pytest.raises(transport.TransportFailure) as captured:
        transport.acquire_plan(plan, LOCK, tool)
    assert reached == 2
    assert captured.value.failure_class == "unclassified"
    assert "not-for-diagnostics" not in str(captured.value)


def test_fault_inside_trusted_git_fetch_call_site_is_structured(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    url = "https://example.org/kit.git"
    plan = _v2_plan([{"url": url, "authentication": "team-https"}], fallback="none")
    bare, commit = _bare_repository(tmp_path / "bare")
    tool = _fake_http_tool(tmp_path / "tool", {url: bare})
    lock = LockedCommit("sha1", commit)
    positive = transport.acquire_plan(
        plan, lock, tool, attempt=transport.default_attempt
    )
    assert positive.snapshot.commit == commit

    reached = 0

    def injected_fault(*args: object, **kwargs: object) -> None:
        nonlocal reached
        reached += 1
        raise PermissionError("credential=not-for-diagnostics")

    monkeypatch.setattr(git_admission, "_run_git", injected_fault)
    with pytest.raises(transport.TransportFailure) as captured:
        transport.acquire_plan(
            plan,
            lock,
            tool,
            attempt=transport.default_attempt,
        )

    assert reached == 1
    assert captured.value.failure_class == "unclassified"
    assert "not-for-diagnostics" not in str(captured.value)


def test_local_bare_repositories_verify_locked_commit_after_availability_fallback(
    tmp_path: Path,
) -> None:
    first_bare, _ = _bare_repository(tmp_path / "first")
    second_bare, commit = _bare_repository(tmp_path / "second")
    first_url = "https://first.example.org/kit.git"
    second_url = "https://fixture.test/kit.git"
    plan = _v2_plan(
        [
            {"url": first_url, "authentication": "first", "mirror_of": IDENTITY},
            {"url": second_url, "authentication": "second", "mirror_of": IDENTITY},
        ]
    )
    tool = _fake_http_tool(
        tmp_path / "tool",
        {second_url: second_bare, "https://unused.example.org/kit.git": first_bare},
    )
    calls: list[str] = []

    def attempt(**kwargs: object) -> git_admission.Snapshot:
        endpoint = kwargs["endpoint"]
        assert isinstance(endpoint, repository_policy.ResolvedEndpoint)
        calls.append(endpoint.url)
        if len(calls) == 1:
            raise transport.TransportFailure("connection-refused")
        return transport.default_attempt(**kwargs)

    result = transport.acquire_plan(
        plan,
        LockedCommit("sha1", commit),
        tool,
        attempt=attempt,
    )
    assert result.snapshot.commit == commit
    assert calls == [first_url, second_url]
    assert result.attempt_count == 2

    calls.clear()
    none_plan = _v2_plan(
        [
            {"url": first_url, "authentication": "first", "mirror_of": IDENTITY},
            {"url": second_url, "authentication": "second", "mirror_of": IDENTITY},
        ],
        fallback="none",
    )
    with pytest.raises(transport.TransportResolutionError):
        transport.acquire_plan(none_plan, LockedCommit("sha1", commit), tool, attempt=attempt)
    assert calls == [first_url]


def test_declared_tag_is_verified_through_the_transport_lane(tmp_path: Path) -> None:
    """An exact declared tag rides the plan into the trusted lane's proof."""

    fixture = tmp_path / "fixture"
    bare = fixture / "remote.git"
    work = fixture / "work"
    template = fixture / "template"
    template.mkdir(parents=True)
    _git(None, "init", "--quiet", f"--template={template}", os.fspath(work))
    (work / "README.md").write_bytes(b"transport fixture\n")
    _git(work, "add", "--", "README.md")
    _git(
        work,
        "-c",
        "user.name=Fixture",
        "-c",
        "user.email=fixture@example.test",
        "commit",
        "--quiet",
        "-m",
        "fixture",
    )
    commit = _git(work, "rev-parse", "HEAD").strip()
    _git(
        work,
        "-c",
        "user.name=Fixture",
        "-c",
        "user.email=fixture@example.test",
        "tag",
        "-a",
        "v1",
        "-m",
        "release",
        commit,
    )
    tag_object = _git(work, "cat-file", "tag", "v1")
    assert "\ntagger Fixture <fixture@example.test> " in "\n" + tag_object
    _git(None, "clone", "--quiet", "--bare", os.fspath(work), os.fspath(bare))
    url = "https://example.org/kit.git"
    plan = _v2_plan([{"url": url, "authentication": "team-https"}])
    tool = _fake_http_tool(tmp_path / "tool", {url: bare})

    result = transport.acquire_plan(
        plan, LockedCommit("sha1", commit), tool, tag="v1"
    )

    assert result.snapshot.commit == commit
    assert result.snapshot.tag_verified is True
    assert result.attempt_count == 1


@pytest.mark.parametrize(
    "error",
    [
        PermissionError,
        FileNotFoundError,
        NotADirectoryError,
        IsADirectoryError,
        RuntimeError,
        ValueError,
    ],
)
def test_provider_failures_are_typed_at_the_attempt_boundary(
    error: type[BaseException],
) -> None:
    """Every provider preparation failure becomes a structured refusal."""

    plan = _v1_plan()
    reached: list[str] = []

    def healthy_provider(
        endpoint: repository_policy.ResolvedEndpoint,
    ) -> git_admission.GitTool:
        reached.append(endpoint.url)
        return _real_tool()

    positive = transport.acquire_plan(
        plan, LOCK, tool_for_endpoint=healthy_provider, attempt=lambda **_: _snapshot()
    )
    assert positive.snapshot.commit == LOCK.hex
    assert reached == [plan.endpoints[0].url]

    reached.clear()

    def failing_provider(
        endpoint: repository_policy.ResolvedEndpoint,
    ) -> git_admission.GitTool:
        reached.append(endpoint.url)
        raise error("synthetic-provider-fault")

    with pytest.raises(git_admission.GitAdmissionError) as captured:
        transport.acquire_plan(
            plan, LOCK, tool_for_endpoint=failing_provider, attempt=lambda **_: _snapshot()
        )
    assert reached == [plan.endpoints[0].url]
    assert captured.value.code != ""


def test_unreachable_first_endpoint_is_classified_and_second_local_bare_succeeds(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """An actual closed loopback port opens exactly one alternate attempt."""

    first_socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    first_socket.bind(("127.0.0.1", 0))
    first_port = first_socket.getsockname()[1]
    first_socket.close()
    second_bare, commit = _bare_repository(tmp_path / "second")
    first_url = f"https://127.0.0.1:{first_port}/kit.git"
    second_url = "https://fixture.test/kit.git"
    policy = repository_policy.parse_policy(
        {
            "schema_version": 2,
            "repositories": {
                IDENTITY: {
                    "endpoints": [
                        {
                            "url": first_url,
                            "authentication": "first",
                            "mirror_of": IDENTITY,
                        },
                        {
                            "url": second_url,
                            "authentication": "second",
                            "mirror_of": IDENTITY,
                        },
                    ],
                    "fallback": "availability-auth",
                }
            },
        }
    )
    plan = repository_policy.select_endpoints(policy, IDENTITY)
    tool = _fake_http_tool(
        tmp_path / "tool", {second_url: second_bare}, allow_loopback_https=True
    )
    real_run = git_admission.subprocess.run
    fetches: list[object] = []

    def record_fetch(*args: object, **kwargs: object) -> object:
        command = args[0]
        assert isinstance(command, tuple)
        if "fetch" in command:
            fetches.append(command)
        return real_run(*args, **kwargs)

    monkeypatch.setattr(git_admission.subprocess, "run", record_fetch)
    result = transport.acquire_plan(plan, LockedCommit("sha1", commit), tool)
    assert result.snapshot.commit == commit
    assert result.attempt_count == 2
    assert len(fetches) == 2
    assert result.attempts[0].classification == "connection-refused"

    fetches.clear()
    none_policy = repository_policy.parse_policy(
        {
            "schema_version": 2,
            "repositories": {
                IDENTITY: {
                    "endpoints": [
                        {
                            "url": first_url,
                            "authentication": "first",
                            "mirror_of": IDENTITY,
                        },
                        {
                            "url": second_url,
                            "authentication": "second",
                            "mirror_of": IDENTITY,
                        },
                    ],
                    "fallback": "none",
                }
            },
        }
    )
    with pytest.raises(transport.TransportResolutionError):
        transport.acquire_plan(
            repository_policy.select_endpoints(none_policy, IDENTITY),
            LockedCommit("sha1", commit),
            tool,
        )
    assert len(fetches) == 1


def test_user_git_and_ssh_configuration_is_not_consulted(monkeypatch, tmp_path: Path) -> None:
    """The plan attempts the declared URL literally; undeclared names fail.

    Plan-level literalness under a hostile home directory: the declared
    URL is attempted once with its listed identity, and a logical
    identity without a policy entry fails without any attempt.  The
    git-level isolation (hostile insteadOf, hostile ssh config) is proven
    by the two discriminating tests below.
    """

    home = tmp_path / "home"
    (home / ".ssh").mkdir(parents=True)
    (home / ".ssh" / "config").write_text("Host example.org\n  HostName evil.example.net\n")
    (home / ".gitconfig").write_text(
        "[url \"https://evil.example.net/\"]\n\tinsteadOf = https://example.org/\n"
    )
    monkeypatch.setenv("HOME", os.fspath(home))

    declared = "https://example.org/kit.git"
    bare, commit = _bare_repository(tmp_path / "bare")
    tool = _fake_http_tool(tmp_path / "tool", {declared: bare})
    calls: list[str] = []

    def attempt(**kwargs: object) -> git_admission.Snapshot:
        endpoint = kwargs["endpoint"]
        assert isinstance(endpoint, repository_policy.ResolvedEndpoint)
        calls.append(endpoint.url)
        return _snapshot()

    result = transport.acquire(
        IDENTITY,
        LockedCommit("sha1", commit),
        tool,
        declaration=declared,
    )
    assert result.snapshot.commit == commit
    assert result.attempt_count == 1
    assert result.attempts[0].endpoint.listed_url == declared

    calls.clear()
    with pytest.raises(transport.TransportError) as captured:
        transport.acquire(IDENTITY, LOCK, attempt=attempt)
    assert captured.value.code == repository_policy.CODE_ENDPOINT_UNAVAILABLE
    assert calls == []


def test_external_build_mirror_is_admitted_with_the_same_verified_attempt() -> None:
    mirror = "https://mirror.example.net/kit.git"
    plan = _v2_plan(
        [{"url": mirror, "authentication": "team-https", "mirror_of": IDENTITY}]
    )
    calls: list[str] = []

    def attempt(**kwargs: object) -> git_admission.Snapshot:
        endpoint = kwargs["endpoint"]
        assert isinstance(endpoint, repository_policy.ResolvedEndpoint)
        calls.append(endpoint.url)
        return _snapshot()

    result = transport.acquire_plan(plan, LOCK, attempt=attempt, lane=transport.LANE_EXTERNAL_BUILD)
    assert result.snapshot.commit == LOCK.hex
    assert calls == [mirror]
    assert result.attempts[0].as_dict()["endpoint"] != {"authentication": "team-https"}


@pytest.mark.parametrize(
    "endpoint, alias_name, aliases",
    [
        ("ssh://git@example.org:2222/kit.git", None, None),
        (
            "https://example.org/kit.git",
            "corp-mirror",
            {
                "corp-mirror": {
                    "host": "mirror.corp.example",
                    "authentication": "team-https",
                },
            },
        ),
    ],
)
def test_external_build_strict_refusals_happen_before_network(
    endpoint: str,
    alias_name: str | None,
    aliases: dict[str, object] | None,
) -> None:
    item: dict[str, object] = {
        "url": endpoint,
        "authentication": "team-https",
    }
    if alias_name is not None:
        item["alias"] = alias_name
        item["mirror_of"] = IDENTITY
    plan = _v2_plan([item], aliases=aliases)
    calls: list[str] = []

    def attempt(**kwargs: object) -> git_admission.Snapshot:
        calls.append("attempt")
        return _snapshot()

    with pytest.raises(ExternalBuildError) as captured:
        transport.acquire_plan(plan, LOCK, attempt=attempt, lane=transport.LANE_EXTERNAL_BUILD)
    assert captured.value.code == "build_repository_identity_invalid"
    assert calls == []


def test_external_build_rejects_alias_port_before_network() -> None:
    plan = _v2_plan(
        [
            {
                "url": "https://example.org/kit.git",
                "authentication": "team-https",
                "alias": "corp-mirror",
                "mirror_of": IDENTITY,
            }
        ],
        aliases={
            "corp-mirror": {
                "host": "mirror.corp.example",
                "port": 8443,
                "authentication": "team-https",
            }
        },
    )
    calls: list[str] = []

    def attempt(**kwargs: object) -> git_admission.Snapshot:
        calls.append("attempt")
        return _snapshot()

    with pytest.raises(ExternalBuildError) as captured:
        transport.acquire_plan(
            plan,
            LOCK,
            attempt=attempt,
            lane=transport.LANE_EXTERNAL_BUILD,
        )
    assert captured.value.code == "build_repository_identity_invalid"
    assert calls == []


def test_pipeline_external_endpoint_gate_precedes_acquisition() -> None:
    plan = _v2_plan([{"url": "ssh://git@example.org:2222/kit.git", "authentication": "team-ssh"}])
    calls: list[str] = []
    request = PipelineRequest(
        operation=Operation.AUDIT,
        command="tool",
        target="tool",
        declared=DeclaredState("tools", IDENTITY, "ssh", "sha1", LOCK.hex),
        effective=EffectiveState("network-git", IDENTITY, "ssh", "sha1", LOCK.hex),
        acquire=lambda: calls.append("acquire") or _snapshot(),
        audit=lambda subject: None,
        endpoint=plan.endpoints[0],
    )
    with pytest.raises(ExternalBuildError) as captured:
        run_pipeline(request)
    assert captured.value.code == "build_repository_identity_invalid"
    assert calls == []


def _observed_first_class(error: BaseException) -> str | None:
    """Return the sanitized first-attempt class carried by a refusal."""

    observed = getattr(error, "failure_class", None)
    if isinstance(observed, str):
        return observed
    if isinstance(error, transport.TransportError) and error.attempts:
        return error.attempts[0].classification
    return None


def _failing_ssh_stub(root: Path, lines: list[str]) -> tuple[Path, Path]:
    """Write an ssh stand-in that logs argv, emits ``lines`` and exits 255."""

    root.mkdir(parents=True, exist_ok=True)
    log = root / "ssh-argv.jsonl"
    program = root / "ssh"
    program.write_text(
        f"#!{sys.executable}\n"
        "import json, os, sys\n"
        f"open({os.fspath(log)!r}, 'a', encoding='utf-8').write("
        "json.dumps(sys.argv) + '\\n')\n"
        + "".join(f"sys.stderr.write({line!r} + '\\n')\n" for line in lines)
        + "sys.exit(255)\n",
        encoding="utf-8",
    )
    program.chmod(0o700)
    return program, log


def test_pipeline_external_endpoint_gate_checks_every_selected_endpoint() -> None:
    plan = _v2_plan(
        [
            {
                "url": "https://mirror.example.net/kit.git",
                "authentication": "team-https",
                "mirror_of": IDENTITY,
            },
            {
                "url": "ssh://git@example.org:2222/kit.git",
                "authentication": "team-ssh",
            },
        ]
    )
    calls: list[str] = []
    request = PipelineRequest(
        operation=Operation.AUDIT,
        command="tool",
        target="tool",
        declared=DeclaredState("tools", IDENTITY, "https", "sha1", LOCK.hex),
        effective=EffectiveState("network-git", IDENTITY, "https", "sha1", LOCK.hex),
        acquire=lambda: calls.append("acquire") or _snapshot(),
        audit=lambda subject: None,
        endpoints=plan.endpoints,
    )
    with pytest.raises(ExternalBuildError) as captured:
        run_pipeline(request)
    assert captured.value.code == "build_repository_identity_invalid"
    assert calls == []


@pytest.mark.parametrize(
    "token",
    ["401", "403", "502", "503", "504", "connection refused"],
)
@pytest.mark.parametrize(
    "shape",
    [
        "fatal: couldn't find remote ref refs/tags/{token}",
        "fatal: repository 'https://example.org/{token}.git' not found",
        "fatal: protocol error: bad line length character: {token}",
        "error: object file 'objects/{token}' is empty",
        "fatal: submodule '{token}' failed",
        "remote: author {token}",
        "remote: file contents: {token}",
    ],
)
def test_generated_adversarial_family(
    token: str,
    shape: str,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Status tokens smuggled into refs, URLs, paths and content open no fallback.

    Every member is injected at the real fetch subprocess seam, so the
    production classifier and the production fallback gate both run.
    """

    plan = _v2_plan(
        [
            {"url": "https://example.org/kit.git", "authentication": "first"},
            {
                "url": "https://mirror.example.org/kit.git",
                "authentication": "second",
                "mirror_of": IDENTITY,
            },
        ]
    )
    tool = _fake_http_tool(tmp_path / "tool", {})
    real_run = git_admission.subprocess.run
    fetches: list[object] = []
    stderr = shape.format(token=token)

    def injected_run(*args: object, **kwargs: object) -> object:
        command = args[0]
        assert isinstance(command, tuple)
        if "fetch" in command:
            fetches.append(command)
            raise subprocess.CalledProcessError(
                128, command, stderr=stderr.encode("utf-8")
            )
        return real_run(*args, **kwargs)

    monkeypatch.setattr(git_admission.subprocess, "run", injected_run)
    with pytest.raises(git_admission.GitAdmissionError) as captured:
        transport.acquire_plan(plan, LOCK, tool)
    assert len(fetches) == 1
    observed = _observed_first_class(captured.value)
    assert observed is not None
    assert repository_policy.classify_failure(observed) == "forbidden"


@_posix_ssh_only
@pytest.mark.parametrize(
    ("message", "classification"),
    [
        ("git@example.org: Permission denied (publickey).", "ssh-auth-rejected"),
        ("Permission denied (publickey).", "ssh-auth-rejected"),
        (
            "git@example.org: Permission denied (publickey,keyboard-interactive).",
            "ssh-auth-rejected",
        ),
        (
            _ssh_rejection_line("git@example.org", "password"),
            "ssh-auth-rejected",
        ),
        (
            _ssh_rejection_line("git@example.org", "keyboard-interactive"),
            "ssh-auth-rejected",
        ),
        (
            _ssh_rejection_line("git@example.org", "publickey,password"),
            "ssh-auth-rejected",
        ),
        (
            _ssh_rejection_line("svc-deploy@example.org", "publickey"),
            "ssh-auth-rejected",
        ),
        (
            "No supported authentication methods available (server sent: publickey)",
            "ssh-auth-rejected",
        ),
        (
            "ssh: Could not resolve hostname example.org: Name or service not known",
            "dns",
        ),
        (
            "ssh: connect to host example.org port 22: Connection refused",
            "connection-refused",
        ),
        (
            "ssh: connect to host example.org port 22: Connection timed out",
            "timeout",
        ),
        (
            "ssh: connect to host example.org port 22: Operation timed out",
            "timeout",
        ),
    ],
)
def test_real_ssh_failure_falls_back(
    message: str, classification: str, tmp_path: Path
) -> None:
    """Every supported SSH availability/auth outcome opens exactly one alternate.

    Real Git, the production generated SSH wrapper and a local stand-in that
    emits one authentic transport record and exits 255; Git appends its own
    advisory footer, which must not suppress the required fallback.  The
    per-transport tool provider mirrors installer.endpoint_tool's production
    role; classification and the fallback gate run for real.  The
    non-publickey method lists are byte-tied to live servers by the
    live-sshd equality test.
    """

    bare, commit = _ssh_fixture_repository(tmp_path / "fixture")
    ssh, log = _failing_ssh_stub(tmp_path / "ssh", [message])
    creds = git_admission.OperatorSSHCredentials(
        identity=_ssh_identity(tmp_path / "operator"),
        known_hosts=_ssh_known_hosts(tmp_path / "operator"),
    )
    second = "https://example.org/kit.git"
    plan = _v2_plan(
        [
            {"url": "git@example.org:kit.git", "authentication": "first"},
            {"url": second, "authentication": "second"},
        ]
    )
    https = _fake_http_tool(tmp_path / "http", {second: bare})

    def provider(endpoint: repository_policy.ResolvedEndpoint) -> git_admission.GitTool:
        return _ssh_tool(ssh, creds) if endpoint.transport == "ssh" else https

    result = transport.acquire_plan(
        plan,
        git_admission.LockedCommit("sha1", commit),
        tool_for_endpoint=provider,
    )
    assert result.snapshot.commit == commit
    assert result.attempt_count == 2
    assert result.attempts[0].classification == classification
    assert len(log.read_text(encoding="utf-8").splitlines()) == 1


@pytest.mark.parametrize(
    "methods",
    ["publickey", "password", "keyboard-interactive", "publickey,password"],
)
def test_stand_in_rejection_frames_open_the_mandated_fallback_on_every_lane(
    methods: str, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The live-confirmed rejection bytes open the alternate on every lane.

    No POSIX gate, no sshd gate: the exact terminal line the live run
    recorded is injected as the first fetch's stderr at the real
    subprocess seam, so the production classifier and the production
    fallback gate both run.  The POSIX stand-in tests prove the
    ssh-spawn path; this test proves the frame-to-fallback rule where
    every ssh test skips.
    """

    frame = _ssh_rejection_line("git@example.org", methods)
    first_url = "https://origin.example.net/kit.git"
    second_url = "https://mirror.example.org/kit.git"
    bare, commit = _bare_repository(tmp_path / "second")
    plan = _v2_plan(
        [
            {
                "url": first_url,
                "authentication": "first",
                "mirror_of": IDENTITY,
            },
            {
                "url": second_url,
                "authentication": "second",
                "mirror_of": IDENTITY,
            },
        ]
    )
    tool = _fake_http_tool(tmp_path / "tool", {second_url: bare})
    real_run = git_admission.subprocess.run
    fetches: list[object] = []

    def injected_run(*args: object, **kwargs: object) -> object:
        command = args[0]
        assert isinstance(command, tuple)
        if "fetch" in command and not fetches:
            fetches.append(command)
            raise subprocess.CalledProcessError(
                128, command, stderr=(frame + "\n").encode("utf-8")
            )
        return real_run(*args, **kwargs)

    monkeypatch.setattr(git_admission.subprocess, "run", injected_run)
    result = transport.acquire_plan(plan, LockedCommit("sha1", commit), tool)
    assert result.snapshot.commit == commit
    assert result.attempt_count == 2
    assert len(fetches) == 1
    failed = fetches[0]
    assert isinstance(failed, tuple)
    assert first_url in failed
    assert result.attempts[0].classification == "ssh-auth-rejected"


@_posix_ssh_only
@pytest.mark.parametrize(
    "suffix",
    [
        "fatal: couldn't find remote ref refs/tags/release-401",
        "ssh: connect to host example.org port 22: Connection refused",
        (
            "fatal: unable to access 'https://example.org/kit.git': "
            "The requested URL returned error: 503"
        ),
        "Host key verification failed.",
        (
            "fatal: unable to access 'https://example.org/kit.git': "
            "SSL certificate problem: self signed certificate"
        ),
    ],
)
def test_real_ssh_failure_envelope_with_hostile_suffix_refuses_fallback(
    suffix: str, tmp_path: Path
) -> None:
    """Disagreeing evidence beside a real record fails closed.

    The stand-in emits one authentic auth-rejection record plus a second
    evidence record for a different class; Git appends its genuine advisory
    footer.  Conflict means two evidence-bearing records disagreeing, so
    the fallback is suppressed with exactly one SSH process and one fetch.
    Non-evidence lines moved out of this test in revision 4: they are
    ignored, and the companion test pins the real record deciding.
    """

    bare, commit = _ssh_fixture_repository(tmp_path / "fixture")
    ssh, log = _failing_ssh_stub(
        tmp_path / "ssh",
        ["git@example.org: Permission denied (publickey).", suffix],
    )
    creds = git_admission.OperatorSSHCredentials(
        identity=_ssh_identity(tmp_path / "operator"),
        known_hosts=_ssh_known_hosts(tmp_path / "operator"),
    )
    second = "https://example.org/kit.git"
    plan = _v2_plan(
        [
            {"url": "git@example.org:kit.git", "authentication": "first"},
            {"url": second, "authentication": "second"},
        ]
    )
    https = _fake_http_tool(tmp_path / "http", {second: bare})

    def provider(endpoint: repository_policy.ResolvedEndpoint) -> git_admission.GitTool:
        return _ssh_tool(ssh, creds) if endpoint.transport == "ssh" else https

    with pytest.raises(git_admission.GitAdmissionError) as captured:
        transport.acquire_plan(
            plan,
            git_admission.LockedCommit("sha1", commit),
            tool_for_endpoint=provider,
        )
    assert captured.value.code == "build_repository_transport_unclassified"
    assert _observed_first_class(captured.value) == "unclassified"
    assert len(log.read_text(encoding="utf-8").splitlines()) == 1


@_posix_ssh_only
@pytest.mark.parametrize(
    "ignored", _NON_EVIDENCE_LINES, ids=_NON_EVIDENCE_IDS
)
def test_non_evidence_beside_a_real_record_leaves_the_real_record_deciding(
    ignored: str, tmp_path: Path
) -> None:
    """Server relay, diagnostics and boilerplate never suppress a fallback.

    The stand-in emits one authentic auth-rejection record plus a line that
    carries no transport outcome; Git appends its genuine advisory footer.
    Revision 4 intentionally moves this rule: only disagreeing
    evidence-bearing records conflict, so the real record alone decides and
    the alternate is attempted exactly once.
    """

    bare, commit = _ssh_fixture_repository(tmp_path / "fixture")
    ssh, log = _failing_ssh_stub(
        tmp_path / "ssh",
        ["git@example.org: Permission denied (publickey).", ignored],
    )
    creds = git_admission.OperatorSSHCredentials(
        identity=_ssh_identity(tmp_path / "operator"),
        known_hosts=_ssh_known_hosts(tmp_path / "operator"),
    )
    second = "https://example.org/kit.git"
    plan = _v2_plan(
        [
            {"url": "git@example.org:kit.git", "authentication": "first"},
            {"url": second, "authentication": "second"},
        ]
    )
    https = _fake_http_tool(tmp_path / "http", {second: bare})

    def provider(endpoint: repository_policy.ResolvedEndpoint) -> git_admission.GitTool:
        return _ssh_tool(ssh, creds) if endpoint.transport == "ssh" else https

    result = transport.acquire_plan(
        plan,
        git_admission.LockedCommit("sha1", commit),
        tool_for_endpoint=provider,
    )
    assert result.snapshot.commit == commit
    assert result.attempt_count == 2
    assert result.attempts[0].classification == "ssh-auth-rejected"
    assert len(log.read_text(encoding="utf-8").splitlines()) == 1


@_posix_ssh_only
def test_real_ssh_host_key_failure_stays_fail_closed(tmp_path: Path) -> None:
    """Host-key evidence plus the genuine envelope refuses the alternate."""

    bare, commit = _ssh_fixture_repository(tmp_path / "fixture")
    ssh, log = _failing_ssh_stub(tmp_path / "ssh", ["Host key verification failed."])
    creds = git_admission.OperatorSSHCredentials(
        identity=_ssh_identity(tmp_path / "operator"),
        known_hosts=_ssh_known_hosts(tmp_path / "operator"),
    )
    second = "https://example.org/kit.git"
    plan = _v2_plan(
        [
            {"url": "git@example.org:kit.git", "authentication": "first"},
            {"url": second, "authentication": "second"},
        ]
    )
    https = _fake_http_tool(tmp_path / "http", {second: bare})

    def provider(endpoint: repository_policy.ResolvedEndpoint) -> git_admission.GitTool:
        return _ssh_tool(ssh, creds) if endpoint.transport == "ssh" else https

    with pytest.raises(git_admission.GitAdmissionError) as captured:
        transport.acquire_plan(
            plan,
            git_admission.LockedCommit("sha1", commit),
            tool_for_endpoint=provider,
        )
    assert captured.value.code == "build_repository_transport_host_key"
    assert _observed_first_class(captured.value) == "host-key"
    assert len(log.read_text(encoding="utf-8").splitlines()) == 1


# Full stderr envelopes recorded from the live server (see the
# TASK-260917-1s3ue1 results table); paths, ports and key material are
# synthetic — only the terminal record decides.  Lines are joined with LF
# per the stand-in convention; the live run proves the CRLF originals
# behave the same end to end.
_HOST_KEY_CHANGED_ENVELOPE = [
    "@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@",
    "@    WARNING: REMOTE HOST IDENTIFICATION HAS CHANGED!     @",
    "@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@",
    "IT IS POSSIBLE THAT SOMEONE IS DOING SOMETHING NASTY!",
    "Someone could be eavesdropping on you right now (man-in-the-middle attack)!",
    "It is also possible that a host key has just been changed.",
    "The fingerprint for the ED25519 key sent by the remote host is",
    "SHA256:gM/EmmTaplD6AvnDcFVxRYRMXHpVMgqp787i6zX10ho.",
    "Please contact your system administrator.",
    "Add correct host key in /tmp/csk-known-hosts to get rid of this message.",
    "Offending ED25519 key in /tmp/csk-known-hosts:1",
    "Host key for [127.0.0.1]:2222 has changed and you have requested strict checking.",
    "Host key verification failed.",
]
_HOST_KEY_UNKNOWN_ENVELOPE = [
    "No ED25519 host key is known for [127.0.0.1]:2222 and you have requested strict checking.",
    "Host key verification failed.",
]


@pytest.mark.parametrize(
    "envelope",
    [_HOST_KEY_CHANGED_ENVELOPE, _HOST_KEY_UNKNOWN_ENVELOPE],
    ids=["changed", "unknown"],
)
def test_stand_in_host_key_envelopes_stay_fail_closed_on_every_lane(
    envelope: list[str], monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The live-recorded host-key envelopes refuse the alternate on every lane.

    No POSIX gate, no sshd gate: the exact envelope lines the live run
    recorded are injected as the first fetch's stderr at the real
    subprocess seam, so the production classifier and the production
    fallback gate both run.  The terminal verification record alone
    decides: exactly one fetch, no fallback, host-key throughout.
    """

    first_url = "https://origin.example.net/kit.git"
    second_url = "https://mirror.example.org/kit.git"
    bare, commit = _bare_repository(tmp_path / "second")
    plan = _v2_plan(
        [
            {
                "url": first_url,
                "authentication": "first",
                "mirror_of": IDENTITY,
            },
            {
                "url": second_url,
                "authentication": "second",
                "mirror_of": IDENTITY,
            },
        ]
    )
    tool = _fake_http_tool(tmp_path / "tool", {second_url: bare})
    real_run = git_admission.subprocess.run
    fetches: list[object] = []

    def injected_run(*args: object, **kwargs: object) -> object:
        command = args[0]
        assert isinstance(command, tuple)
        if "fetch" in command:
            fetches.append(command)
            raise subprocess.CalledProcessError(
                128,
                command,
                stderr=("\n".join(envelope) + "\n").encode("utf-8"),
            )
        return real_run(*args, **kwargs)

    monkeypatch.setattr(git_admission.subprocess, "run", injected_run)
    with pytest.raises(git_admission.GitAdmissionError) as captured:
        transport.acquire_plan(plan, LockedCommit("sha1", commit), tool)
    assert len(fetches) == 1
    assert captured.value.code == "build_repository_transport_host_key"
    assert _observed_first_class(captured.value) == "host-key"


@_posix_ssh_only
def test_git_ssh_envelope_partitions_evidence_from_non_evidence(
    tmp_path: Path,
) -> None:
    """Pin the output grammar the evidence-or-ignore model is derived from.

    Supersedes test_git_ssh_envelope_contains_only_known_boilerplate
    (revision 3): there is no boilerplate allowlist anymore.  A real ``git
    fetch`` runs against a stand-in that emits one marker record; the
    marker must be the only evidence line, and the advisory footer must be
    present and carry no outcome.  The locale is pinned to C exactly as the
    production clean environment sets it.
    """

    marker = "ssh: connect to host example.org port 22: Connection refused"
    work = tmp_path / "work"
    _git(None, "init", "--quiet", os.fspath(work))
    stub = tmp_path / "ssh"
    stub.write_text(
        "#!/bin/sh\necho " + f"'{marker}' >&2\nexit 255\n", encoding="utf-8"
    )
    stub.chmod(0o700)
    environment = dict(os.environ)
    environment["GIT_SSH"] = os.fspath(stub)
    environment["GIT_TERMINAL_PROMPT"] = "0"
    environment["LANG"] = "C"
    environment["LC_ALL"] = "C"
    completed = subprocess.run(
        (
            os.fspath(_git_path()),
            "-C",
            os.fspath(work),
            "fetch",
            "--quiet",
            "git@example.org:kit.git",
            "HEAD",
        ),
        capture_output=True,
        text=True,
        env=environment,
        timeout=30,
        check=False,
    )
    assert completed.returncode == 128
    lines = [
        line.strip().lower()
        for line in completed.stderr.splitlines()
        if line.strip()
    ]
    assert marker.lower() in lines
    assert "please make sure you have the correct access rights" in lines
    assert "and the repository exists." in lines
    assert git_admission._evidence_class(marker.lower()) == "connection-refused"
    for line in lines:
        if line == marker.lower():
            continue
        assert git_admission._evidence_class(line) is None, line
    assert git_admission._classify_git_failure_output(completed.stderr) == (
        "connection-refused"
    )


_HTTPS_BODIES: dict[str, tuple[bytes, str]] = {
    "bodiless": (b"", "text/plain"),
    "text": (b"cdn-status-probe", "text/plain"),
    "multiline": (b"line one\nline two\nline three", "text/plain"),
    "html": (
        (
            b"<html><head><title>Error</title></head>\n"
            b"<body><h1>Failure</h1></body></html>"
        ),
        "text/html",
    ),
    "json": (b'{"message":"Denied"}', "application/json"),
}

# status, body, credentials, expected first class, fallback expected.
# "fail" is the failing askpass (the anonymous-lane shape: the helper
# exits without yielding); "badcreds" is the production HTTPS broker
# presenting wrong material.
_HTTPS_MATRIX: list[tuple[int, str, str, str, bool]] = []
for _status, _class in (
    (502, "http-502"),
    (503, "http-503"),
    (504, "http-504"),
    (403, "http-403"),
):
    for _body in ("bodiless", "text", "multiline", "html", "json"):
        _HTTPS_MATRIX.append((_status, _body, "fail", _class, True))
for _body in ("bodiless", "text", "multiline", "html", "json"):
    _HTTPS_MATRIX.append((404, _body, "fail", "identity", False))
    _HTTPS_MATRIX.append((302, _body, "fail", "redirect", False))
    _HTTPS_MATRIX.append((401, _body, "fail", "auth-unavailable", True))
for _body in ("bodiless", "text", "json"):
    _HTTPS_MATRIX.append((401, _body, "badcreds", "auth-rejected", True))


@pytest.mark.parametrize(
    ("status", "body_name", "credentials", "expected", "falls_back"),
    _HTTPS_MATRIX,
    ids=[f"{cell[0]}-{cell[1]}-{cell[2]}" for cell in _HTTPS_MATRIX],
)
def test_real_https_status_with_body_classifies_through_the_lane(
    status: int,
    body_name: str,
    credentials: str,
    expected: str,
    falls_back: bool,
    _tls_server_factory: Callable[..., _StatusServer],
    _shared_second_bare: tuple[Path, str],
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Loopback HTTPS statuses by body shape classify with exact counts.

    Real git and real curl against a loopback status server through
    ``transport.acquire_plan``: the first endpoint is the server, the
    second is a file-mapped bare repository reached only on fallback.
    Text bodies are echoed by git as ``remote:`` relay lines, which carry
    no outcome; the terminal transport record alone decides.
    """

    body, content_type = _HTTPS_BODIES[body_name]
    server = _tls_server_factory(
        status=status, body=body, content_type=content_type
    )
    second_bare, commit = _shared_second_bare
    second_url = "https://fixture.test/kit.git"
    plan = _https_mirror_plan(server.url, second_url)
    mapped = _fake_http_tool(
        tmp_path / "tool", {second_url: second_bare}, allow_loopback_https=True
    )
    tool: git_admission.GitTool | None = mapped
    tool_for_endpoint: (
        Callable[[repository_policy.ResolvedEndpoint], git_admission.GitTool]
        | None
    ) = None
    if credentials == "badcreds":
        first_tool = replace(
            _fake_http_tool(
                tmp_path / "tool-first", {}, allow_loopback_https=True
            ),
            https_credentials=git_admission.OperatorHTTPSCredentials(
                scope=IDENTITY,
                host="127.0.0.1",
                source="env",
                username="matrix-bad-user",
                token_value="matrix-bad-token",
            ),
        )

        def select(
            endpoint: repository_policy.ResolvedEndpoint,
        ) -> git_admission.GitTool:
            return first_tool if endpoint.url == server.url else mapped

        tool_for_endpoint = select
        tool = None
    real_run = git_admission.subprocess.run
    fetches: list[object] = []

    def record(*args: object, **kwargs: object) -> object:
        command = args[0]
        assert isinstance(command, tuple)
        if "fetch" in command:
            fetches.append(command)
        return real_run(*args, **kwargs)

    monkeypatch.setattr(git_admission.subprocess, "run", record)
    if falls_back:
        result = transport.acquire_plan(
            plan,
            git_admission.LockedCommit("sha1", commit),
            tool,
            tool_for_endpoint=tool_for_endpoint,
        )
        assert result.snapshot.commit == commit
        assert result.attempt_count == 2
        assert result.attempts[0].classification == expected
        assert len(fetches) == 2
    else:
        with pytest.raises(git_admission.GitAdmissionError) as captured:
            transport.acquire_plan(
                plan,
                git_admission.LockedCommit("sha1", commit),
                tool,
                tool_for_endpoint=tool_for_endpoint,
            )
        assert len(fetches) == 1
        assert _observed_first_class(captured.value) == expected
        assert repository_policy.classify_failure(expected) == "forbidden"
    if credentials == "badcreds":
        assert any(
            value is not None for value in server.auth_seen
        ), "the broker never presented credentials"
    else:
        assert server.auth_seen == [None]


@pytest.mark.parametrize(
    "log_line",
    [
        "setgroups() failed: Operation not permitted",
        "Privilege separation user sshd does not exist",
        "Missing privilege separation directory: /var/empty",
    ],
    ids=["setgroups", "privsep-user", "privsep-dir"],
)
def test_sshd_privilege_refusal_log_yields_a_named_privilege_skip_reason(
    log_line: str,
) -> None:
    """A privilege refusal in the sshd log names privilege in the skip."""

    assert _classify_sshd_start_failure(log_line) == "privilege refused"
    reason = _sshd_skip_reason(log_line, timed_out=False)
    assert "privilege refused" in reason
    assert log_line in reason


def test_sshd_port_conflict_log_yields_a_named_port_skip_reason() -> None:
    """A bind failure in the sshd log names the port in the skip."""

    log = "error: Bind to port 2222 on 127.0.0.1 failed: Address already in use."
    assert _classify_sshd_start_failure(log) == "port unavailable"
    reason = _sshd_skip_reason(log, timed_out=False)
    assert "port unavailable" in reason
    assert "Address already in use" in reason


@pytest.mark.parametrize(
    "log_text",
    ["", "sshd: no hostkeys available -- exiting.\n"],
    ids=["empty", "unrecognized"],
)
def test_sshd_unrecognized_start_failure_yields_a_named_generic_skip_reason(
    log_text: str,
) -> None:
    """A log the fixture cannot classify still skips with a named reason."""

    assert _classify_sshd_start_failure(log_text) is None
    assert "exited before serving" in _sshd_skip_reason(log_text, timed_out=False)
    assert "did not listen in time" in _sshd_skip_reason(log_text, timed_out=True)


def test_live_sshd_constructor_reports_a_missing_binary_instead_of_raising(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A host without sshd gets a named absence, never an assertion."""

    monkeypatch.setattr(sys.modules[__name__], "_sshd_path", lambda: None)
    with pytest.raises(_LiveSshdUnavailable, match="sshd binary absent"):
        _LiveSshd(tmp_path / "sshd", [])


def test_live_sshd_constructor_skips_rather_than_raising_when_the_server_exits(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A server that exits at once becomes a named skip, never an error.

    The stand-in executable exists but is not an sshd, so the spawn
    succeeds and the process exits before serving — the same shape as a
    privilege-refused daemon, without needing root to reproduce it.
    """

    monkeypatch.setattr(
        sys.modules[__name__], "_sshd_path", lambda: Path(sys.executable)
    )
    with pytest.raises(_LiveSshdUnavailable, match="live sshd unavailable"):
        _LiveSshd(tmp_path / "sshd", [])
    with pytest.raises(BaseException, match="live sshd unavailable") as captured:
        _start_live_sshd(tmp_path / "sshd-helper", [])
    assert type(captured.value).__name__ == "Skipped"


def test_shared_capability_helper_skips_with_the_fixture_reason() -> None:
    """Every real-server fixture skips through the one shared helper."""

    def fail() -> None:
        raise _RealServerUnavailable("loopback https unavailable: probe")

    with pytest.raises(
        BaseException, match="loopback https unavailable"
    ) as captured:
        _serve_or_skip(fail)
    assert type(captured.value).__name__ == "Skipped"
    assert _serve_or_skip(lambda: "served") == "served"


def test_windows_wrapper_shim_names_the_interpreter_and_forwards_all_arguments() -> (
    None
):
    """The Windows shim is one quoted invocation plus a verbatim tail."""

    assert _windows_git_wrapper_shim(
        "C:\\Python314\\python.exe", "D:\\work\\tool\\git-wrapper.py"
    ) == (
        '@echo off\r\n"C:\\Python314\\python.exe" '
        '"D:\\work\\tool\\git-wrapper.py" %*\r\n'
    )


def test_windows_fail_askpass_shim_exits_nonzero_without_output() -> None:
    """The Windows failing askpass is exit 1 with no output."""

    assert _windows_fail_askpass_shim() == "@echo off\r\nexit /b 1\r\n"


def test_wrapper_payload_path_resolves_behind_both_executable_flavours(
    tmp_path: Path,
) -> None:
    """Payload instrumentation addresses the .py behind either flavour."""

    tool = _fake_http_tool(tmp_path / "tool", {})
    assert _wrapper_payload_path(
        tmp_path / "git-wrapper.bat"
    ) == tmp_path / "git-wrapper.py"
    assert _wrapper_payload_path(tmp_path / "git-wrapper") == (
        tmp_path / "git-wrapper"
    )
    assert _wrapper_payload_path(tool.executable).exists()


def test_fake_http_tool_selects_the_executable_flavour_this_host_runs(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Windows gets the .bat pair; POSIX keeps the extensionless pair."""

    module = sys.modules[__name__]
    monkeypatch.setattr(module, "_host_is_windows", lambda: True)
    windows_tool = _fake_http_tool(tmp_path / "windows", {})
    assert windows_tool.executable.name == "git-wrapper.bat"
    assert windows_tool.askpass is not None
    assert windows_tool.askpass.name == "askpass.bat"
    assert windows_tool.executable.is_absolute()
    assert windows_tool.askpass.is_absolute()

    monkeypatch.setattr(module, "_host_is_windows", lambda: False)
    posix_tool = _fake_http_tool(tmp_path / "posix", {})
    assert posix_tool.executable.name == "git-wrapper"
    assert posix_tool.askpass is not None
    assert posix_tool.askpass.name == "askpass"

    payload = tmp_path / "posix" / "git-wrapper.py"
    assert payload.read_bytes() == (
        tmp_path / "posix" / "git-wrapper"
    ).read_bytes()


def test_wrapper_payload_forwards_through_an_explicit_interpreter(
    tmp_path: Path,
) -> None:
    """The payload git runs on Windows maps and forwards end to end.

    The Windows shim invokes ``<interpreter> git-wrapper.py <args>``; this
    drives exactly that shape with the running interpreter, so the payload
    logic the ``.bat`` text cannot prove is executed on every host.
    """

    bare, commit = _bare_repository(tmp_path / "bare")
    url = "https://example.org/kit.git"
    tool = _fake_http_tool(tmp_path / "tool", {url: bare})
    payload = _wrapper_payload_path(tool.executable)
    empty = tmp_path / "empty.gitconfig"
    empty.write_text("", encoding="utf-8")
    environment = {
        **os.environ,
        "GIT_CONFIG_GLOBAL": os.fspath(empty),
        "GIT_CONFIG_NOSYSTEM": "1",
        "XDG_CONFIG_HOME": os.fspath(tmp_path),
    }
    version = subprocess.run(
        (sys.executable, os.fspath(payload), "--version"),
        check=True,
        capture_output=True,
        text=True,
        timeout=20,
        env=environment,
    )
    assert version.stdout.startswith("git version ")
    refs = subprocess.run(
        (sys.executable, os.fspath(payload), "ls-remote", url),
        check=True,
        capture_output=True,
        text=True,
        timeout=20,
        env=environment,
    )
    assert commit in refs.stdout


@pytest.mark.parametrize(
    "stderr",
    [
        (
            "fatal: unable to access 'https://127.0.0.1:1/kit.git/': "
            "SSL certificate problem: self-signed certificate"
        ),
        (
            "fatal: unable to access 'https://127.0.0.1:1/kit.git/': "
            "server certificate verification failed. CAfile: none CRLfile: none"
        ),
        # A schannel-only spelling (no ssl/tls/certificate token): the
        # backend name alone must suffice, since handshake failures can
        # name only a SEC_E_* code.
        (
            "fatal: unable to access 'https://127.0.0.1:1/kit.git/': "
            "schannel: next InitializeSecurityContext failed: "
            "SEC_E_UNTRUSTED_ROOT (0x80090325)"
        ),
        (
            "fatal: unable to access 'https://127.0.0.1:1/kit.git/': "
            "schannel: CertGetCertificateChain trust error "
            "CERT_TRUST_IS_UNTRUSTED_ROOT"
        ),
    ],
    ids=["openssl", "verify-failed", "schannel", "schannel-cert-chain"],
)
def test_https_probe_trust_failure_yields_a_named_tls_skip_reason(
    stderr: str,
) -> None:
    """A probe fetch failing TLS trust names TLS trust in the skip."""

    assert _classify_https_probe_failure(stderr) == "TLS trust refused"
    reason = _https_probe_skip_reason(stderr, "TLS trust refused")
    assert reason.startswith("loopback https unavailable: TLS trust refused (")
    assert stderr in reason


@pytest.mark.parametrize(
    "stderr",
    [
        (
            "fatal: unable to access 'https://127.0.0.1:1/kit.git/': "
            "The requested URL returned error: 503"
        ),
        (
            "fatal: could not read Username for 'https://127.0.0.1:1/kit.git': "
            "terminal prompts disabled"
        ),
        (
            "remote: a body echo mentioning ssl in passing\n"
            "fatal: unable to access 'https://127.0.0.1:1/kit.git/': "
            "The requested URL returned error: 503"
        ),
    ],
    ids=["http-status", "auth-demand", "body-echo-with-token"],
)
def test_https_probe_past_tls_proceeds_without_skipping(stderr: str) -> None:
    """A probe that reached HTTP, auth, or a mere body echo never skips."""

    assert _classify_https_probe_failure(stderr) is None


@pytest.mark.parametrize(
    "stderr",
    [
        (
            "fatal: unable to access 'https://127.0.0.1:1/kit.git/': "
            "Failed to connect to 127.0.0.1 port 1 after 0 ms: "
            "Couldn't connect to server"
        ),
        (
            "fatal: unable to access 'https://127.0.0.1:1/kit.git/': "
            "Failed to connect to 127.0.0.1 port 1 after 0 ms: "
            "Connection refused"
        ),
    ],
    ids=["connect-failed", "refused"],
)
def test_https_probe_refused_yields_a_named_unreachable_skip_reason(
    stderr: str,
) -> None:
    """A probe fetch refused at TCP names the unreachable server in the skip."""

    assert _classify_https_probe_failure(stderr) == "server unreachable"
    reason = _https_probe_skip_reason(stderr, "server unreachable")
    assert "server unreachable" in reason


def test_https_probe_unknown_failure_proceeds_without_skipping() -> None:
    """A failure the probe cannot classify proceeds so the test judges it."""

    assert _classify_https_probe_failure("") is None
    assert _classify_https_probe_failure("fatal: something new happened") is None


def test_https_bind_conflict_yields_a_named_port_skip_reason() -> None:
    """A loopback bind conflict names the port in the skip."""

    exc = OSError(errno.EADDRINUSE, "Address already in use")
    reason = _https_bind_skip_reason(exc)
    assert reason.startswith("loopback https unavailable: port unavailable (")


def test_https_context_failure_yields_a_named_tls_skip_reason() -> None:
    """A TLS context failure names the context in the skip."""

    exc = ssl.SSLError("certificate verify failed")
    reason = _https_bind_skip_reason(exc)
    assert reason.startswith("loopback https unavailable: TLS context refused (")


def test_https_unrecognized_start_failure_yields_a_quoted_generic_skip_reason() -> (
    None
):
    """A start failure the fixture cannot classify still skips named."""

    exc = OSError("a new way to fail at bind time")
    reason = _https_bind_skip_reason(exc)
    assert reason.startswith("loopback https unavailable: server start refused (")
    assert "a new way to fail at bind time" in reason


def test_loopback_https_probe_passes_where_tls_trust_works(
    _tls_server_factory: Callable[..., _StatusServer],
) -> None:
    """The capability probe itself passes on a host with working TLS trust.

    The factory probes before returning; reaching the assertion proves the
    probe did not over-skip here. Where trust is absent this skips with the
    probe's named reason, declaring the bound instead of coverage.
    """

    server = _tls_server_factory(status=503, body=b"")
    assert server.port > 0


def _ssh_environment_without_agent() -> dict[str, str]:
    """Copy the process environment with every agent handle removed.

    The live-sshd equality test must measure the product's ssh behaviour,
    not the operator: with an agent holding MaxAuthTries keys or more, a
    client that offers ambient identities is disconnected before printing
    the rejection line the test asserts byte-for-byte.
    """

    environment = dict(os.environ)
    environment.pop("SSH_AUTH_SOCK", None)
    environment.pop("SSH_AGENT_PID", None)
    return environment


@_posix_ssh_only
@_requires_sshd
@pytest.mark.parametrize(
    ("methods", "server_options"),
    [
        (
            "password",
            [
                "PasswordAuthentication=yes",
                "PubkeyAuthentication=no",
                "KbdInteractiveAuthentication=no",
                "ChallengeResponseAuthentication=no",
            ],
        ),
        (
            "keyboard-interactive",
            [
                "PasswordAuthentication=no",
                "PubkeyAuthentication=no",
                "KbdInteractiveAuthentication=yes",
                "ChallengeResponseAuthentication=yes",
            ],
        ),
        (
            "publickey,password",
            [
                "PasswordAuthentication=yes",
                "PubkeyAuthentication=yes",
                "KbdInteractiveAuthentication=no",
                "ChallengeResponseAuthentication=no",
            ],
        ),
    ],
)
def test_live_sshd_rejection_bytes_match_stand_in_frames(
    methods: str, server_options: list[str], tmp_path: Path
) -> None:
    """The stand-in rejection bytes are exactly what real ssh prints.

    A lane-optioned real client (BatchMode, pubkey-only preference)
    against a live password-only, keyboard-interactive-only, or
    multi-method server prints the server's method list verbatim:
    client-side PreferredAuthentications does not change the display.
    The client offers no identity at all — IdentitiesOnly with no -i,
    IdentityAgent=none, and no agent handle in the environment — so the
    verdict cannot depend on how many keys the operator's agent holds.
    """

    sshd = _start_live_sshd(tmp_path / "sshd", server_options)
    try:
        real_ssh = shutil.which("ssh")
        assert real_ssh is not None
        completed = subprocess.run(
            [
                real_ssh,
                "-F",
                "/dev/null",
                "-o",
                "BatchMode=yes",
                "-o",
                "IdentitiesOnly=yes",
                "-o",
                "IdentityAgent=none",
                "-o",
                "PreferredAuthentications=publickey",
                "-o",
                "PasswordAuthentication=no",
                "-o",
                "KbdInteractiveAuthentication=no",
                "-o",
                "StrictHostKeyChecking=no",
                "-o",
                "UserKnownHostsFile=/dev/null",
                "-o",
                "ConnectTimeout=10",
                "-o",
                "ConnectionAttempts=1",
                "-p",
                str(sshd.port),
                "testuser@127.0.0.1",
                "git-upload-pack '/kit.git'",
            ],
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
            env=_ssh_environment_without_agent(),
        )
        assert completed.returncode == 255
        terminal = [
            line
            for line in completed.stderr.splitlines()
            if line and not line.startswith("Warning: Permanently added")
        ]
        assert terminal == [
            _ssh_rejection_line("testuser@127.0.0.1", methods)
        ]
    finally:
        sshd.close()


@_posix_ssh_only
@_requires_sshd
@pytest.mark.parametrize(
    "server_options",
    [
        [
            "PasswordAuthentication=yes",
            "PubkeyAuthentication=no",
            "KbdInteractiveAuthentication=no",
            "ChallengeResponseAuthentication=no",
        ],
        [
            "PasswordAuthentication=no",
            "PubkeyAuthentication=no",
            "KbdInteractiveAuthentication=yes",
            "ChallengeResponseAuthentication=yes",
        ],
    ],
    ids=["password-only", "keyboard-interactive-only"],
)
def test_live_sshd_rejection_falls_back_through_the_lane(
    server_options: list[str], tmp_path: Path
) -> None:
    """A live non-publickey server opens the alternate with no stand-in.

    Real git, the production generated SSH wrapper and the real ssh
    binary against a live sshd: the server's method list is an explicit
    authentication rejection whatever it offers.
    """

    sshd = _start_live_sshd(tmp_path / "sshd", server_options)
    try:
        real_ssh = shutil.which("ssh")
        assert real_ssh is not None
        bare, commit = _ssh_fixture_repository(tmp_path / "fixture")
        operator = tmp_path / "operator"
        _, known_hosts = _live_known_hosts(
            tmp_path / "sshd", operator, sshd.port
        )
        creds = git_admission.OperatorSSHCredentials(
            identity=_ssh_identity(operator),
            known_hosts=known_hosts,
        )
        first_url = f"ssh://git@127.0.0.1:{sshd.port}/kit.git"
        second_url = "https://fixture.test/kit.git"
        plan = _v2_plan(
            [
                {
                    "url": first_url,
                    "authentication": "first",
                    "mirror_of": IDENTITY,
                },
                {
                    "url": second_url,
                    "authentication": "second",
                    "mirror_of": IDENTITY,
                },
            ]
        )
        ssh_tool = _ssh_tool(Path(real_ssh), creds)
        https_tool = _fake_http_tool(
            tmp_path / "http", {second_url: bare}, allow_loopback_https=True
        )

        def provider(
            endpoint: repository_policy.ResolvedEndpoint,
        ) -> git_admission.GitTool:
            return ssh_tool if endpoint.transport == "ssh" else https_tool

        result = transport.acquire_plan(
            plan,
            git_admission.LockedCommit("sha1", commit),
            tool_for_endpoint=provider,
        )
        assert result.snapshot.commit == commit
        assert result.attempt_count == 2
        assert result.attempts[0].classification == "ssh-auth-rejected"
    finally:
        sshd.close()


def _live_known_hosts(
    sshd_root: Path, operator_root: Path, port: int
) -> tuple[Path, Path]:
    """Trust exactly the live fixture's host key for its bracketed address."""

    operator_root.mkdir(parents=True, exist_ok=True)
    public = (sshd_root / "hostkey.pub").read_text(encoding="utf-8").split()
    known_hosts = operator_root / "live_known_hosts"
    known_hosts.write_text(
        f"[127.0.0.1]:{port} {public[0]} {public[1]}\n", encoding="utf-8"
    )
    return sshd_root / "hostkey", known_hosts


@_posix_ssh_only
@_requires_sshd
def test_live_sshd_refusing_agent_falls_back_through_the_lane(
    tmp_path: Path,
) -> None:
    """A refusing agent beside a genuine rejection opens the alternate.

    Real git, the production wrapper, the real ssh binary, a live sshd
    and a speaking agent that refuses every signature: the client's
    signing diagnostic carries no outcome, so the terminal rejection
    record alone decides.  The agent log proves ssh actually consulted
    the agent rather than skipping it.
    """

    # The agent's key must be authorized for a real local user: only an
    # accepted key offer makes the client request a signature, which is
    # the refusal this test exercises.
    agent = _RefusingAgent(tmp_path / "agent")
    authkeys = tmp_path / "authkeys"
    authkeys.write_text(
        (tmp_path / "agent" / "agentkey.pub").read_text(encoding="utf-8"),
        encoding="utf-8",
    )
    sshd = _start_live_sshd(
        tmp_path / "sshd",
        [
            "PasswordAuthentication=no",
            "PubkeyAuthentication=yes",
            "KbdInteractiveAuthentication=no",
            "ChallengeResponseAuthentication=no",
        ],
        authorized_keys=authkeys,
    )
    try:
        real_ssh = shutil.which("ssh")
        assert real_ssh is not None
        with agent:
            bare, commit = _ssh_fixture_repository(tmp_path / "fixture")
            operator = tmp_path / "operator"
            _, known_hosts = _live_known_hosts(
                tmp_path / "sshd", operator, sshd.port
            )
            # The public half only: ssh cannot sign locally and must ask
            # the agent, which is the refusal under test.
            identity = tmp_path / "agent" / "agentkey.pub"
            creds = git_admission.OperatorSSHCredentials(
                identity=identity,
                known_hosts=known_hosts,
                agent_socket=agent.socket_path,
            )
            first_url = (
                f"ssh://{getpass.getuser()}@127.0.0.1:{sshd.port}/kit.git"
            )
            second_url = "https://fixture.test/kit.git"
            plan = _v2_plan(
                [
                    {
                        "url": first_url,
                        "authentication": "first",
                        "mirror_of": IDENTITY,
                    },
                    {
                        "url": second_url,
                        "authentication": "second",
                        "mirror_of": IDENTITY,
                    },
                ]
            )
            ssh_tool = _ssh_tool(Path(real_ssh), creds)
            https_tool = _fake_http_tool(
                tmp_path / "http",
                {second_url: bare},
                allow_loopback_https=True,
            )

            def provider(
                endpoint: repository_policy.ResolvedEndpoint,
            ) -> git_admission.GitTool:
                return ssh_tool if endpoint.transport == "ssh" else https_tool

            result = transport.acquire_plan(
                plan,
                git_admission.LockedCommit("sha1", commit),
                tool_for_endpoint=provider,
            )
            assert result.snapshot.commit == commit
            assert result.attempt_count == 2
            assert result.attempts[0].classification == "ssh-auth-rejected"
            assert "SIGN_REQUEST" in agent.seen
    finally:
        sshd.close()


@_posix_ssh_only
@_requires_sshd
@pytest.mark.parametrize("mismatch", ["changed", "unknown"])
def test_live_sshd_host_key_mismatch_stays_fail_closed(
    mismatch: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A live host-key failure refuses the alternate with the right class.

    The changed-key envelope carries the full ssh WARNING block; the
    unknown-key envelope carries the strict-checking notice.  Both are
    client-side context for the terminal verification record, which alone
    decides: exactly one fetch, no fallback, host-key throughout.
    """

    sshd = _start_live_sshd(
        tmp_path / "sshd",
        [
            "PasswordAuthentication=no",
            "PubkeyAuthentication=yes",
            "KbdInteractiveAuthentication=no",
            "ChallengeResponseAuthentication=no",
        ],
    )
    try:
        real_ssh = shutil.which("ssh")
        assert real_ssh is not None
        operator = tmp_path / "operator"
        identity = _ssh_identity(operator)
        if mismatch == "changed":
            other = tmp_path / "other-hostkey"
            subprocess.run(
                (
                    "ssh-keygen",
                    "-q",
                    "-t",
                    "ed25519",
                    "-f",
                    os.fspath(other),
                    "-N",
                    "",
                ),
                check=True,
                capture_output=True,
            )
            public = other.with_name("other-hostkey.pub").read_text(
                encoding="utf-8"
            )
            known_hosts = operator / "known_hosts"
            known_hosts.write_text(
                f"[127.0.0.1]:{sshd.port} {public}", encoding="utf-8"
            )
        else:
            known_hosts = _ssh_known_hosts(operator)
        creds = git_admission.OperatorSSHCredentials(
            identity=identity, known_hosts=known_hosts
        )
        bare, commit = _ssh_fixture_repository(tmp_path / "fixture")
        first_url = f"ssh://git@127.0.0.1:{sshd.port}/kit.git"
        second_url = "https://fixture.test/kit.git"
        plan = _v2_plan(
            [
                {
                    "url": first_url,
                    "authentication": "first",
                    "mirror_of": IDENTITY,
                },
                {
                    "url": second_url,
                    "authentication": "second",
                    "mirror_of": IDENTITY,
                },
            ]
        )
        ssh_tool = _ssh_tool(Path(real_ssh), creds)
        https_tool = _fake_http_tool(
            tmp_path / "http", {second_url: bare}, allow_loopback_https=True
        )

        def provider(
            endpoint: repository_policy.ResolvedEndpoint,
        ) -> git_admission.GitTool:
            return ssh_tool if endpoint.transport == "ssh" else https_tool

        real_run = git_admission.subprocess.run
        fetches: list[object] = []

        def record(*args: object, **kwargs: object) -> object:
            command = args[0]
            assert isinstance(command, tuple)
            if "fetch" in command:
                fetches.append(command)
            return real_run(*args, **kwargs)

        monkeypatch.setattr(git_admission.subprocess, "run", record)
        with pytest.raises(git_admission.GitAdmissionError) as captured:
            transport.acquire_plan(
                plan,
                git_admission.LockedCommit("sha1", commit),
                tool_for_endpoint=provider,
            )
        assert len(fetches) == 1
        assert captured.value.code == "build_repository_transport_host_key"
        assert _observed_first_class(captured.value) == "host-key"
    finally:
        sshd.close()


def test_hostile_insteadof_rewrite_does_not_deflect_the_literal_attempt(
    _loopback_tls_cert: tuple[str, str],
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """A hostile insteadOf rewrite cannot deflect the literal fetch.

    The declared URL points at a loopback TLS server with an untrusted
    certificate; the hostile rewrite points at a closed port.  A literal
    attempt fails in TLS (fail-closed, first failure); a rewritten
    attempt would fail refused and, under availability-auth, open the
    alternate.  No CA trust is installed, so only the literal URL can
    produce the observed TLS outcome.
    """

    key_path, crt_path = _loopback_tls_cert
    server = _StatusServer(key_path, crt_path, status=503, body=b"")
    try:
        probe = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        probe.bind(("127.0.0.1", 0))
        closed = probe.getsockname()[1]
        probe.close()
        home = tmp_path / "hostile-home"
        (home / ".ssh").mkdir(parents=True)
        (home / ".ssh" / "config").write_text(
            "Host 127.0.0.1\n  HostName 127.0.0.2\n", encoding="utf-8"
        )
        (home / ".gitconfig").write_text(
            f'[url "https://127.0.0.1:{closed}/"]\n'
            f"\tinsteadOf = https://127.0.0.1:{server.port}/\n",
            encoding="utf-8",
        )
        monkeypatch.setenv("HOME", os.fspath(home))
        monkeypatch.setenv("XDG_CONFIG_HOME", os.fspath(home / ".config"))
        monkeypatch.setenv(
            "GIT_CONFIG_GLOBAL", os.fspath(home / ".gitconfig")
        )
        second_bare, commit = _bare_repository(tmp_path / "second")
        second_url = "https://fixture.test/kit.git"
        plan = _https_mirror_plan(server.url, second_url)
        tool = _fake_http_tool(
            tmp_path / "tool",
            {second_url: second_bare},
            allow_loopback_https=True,
        )
        with pytest.raises(git_admission.GitAdmissionError) as captured:
            transport.acquire_plan(plan, LockedCommit("sha1", commit), tool)
    finally:
        server.shutdown()
    observed = _observed_first_class(captured.value)
    assert observed not in {
        "dns",
        "connection-refused",
        "timeout",
        "endpoint-unavailable",
        "http-502",
        "http-503",
        "http-504",
        "auth-unavailable",
        "auth-rejected",
        "ssh-auth-rejected",
    }, f"attempt was deflected from the literal URL: {observed!r}"
    assert not isinstance(captured.value, transport.TransportResolutionError)
    assert observed == "tls"


@_posix_ssh_only
def test_hostile_ssh_config_never_reaches_real_ssh(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Real ssh through the production wrapper ignores ~/.ssh/config.

    The hostile config remaps 127.0.0.1 to 127.0.0.2; the observed
    refused record must name the literal declared host (visible in the
    chained CalledProcessError stderr), proving the empty config won.
    """

    real_ssh = shutil.which("ssh")
    if real_ssh is None:
        pytest.skip("the hostile-config test requires the ssh binary")
    probe = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    probe.bind(("127.0.0.1", 0))
    closed = probe.getsockname()[1]
    probe.close()
    home = tmp_path / "hostile-home"
    (home / ".ssh").mkdir(parents=True)
    (home / ".ssh" / "config").write_text(
        "Host 127.0.0.1\n  HostName 127.0.0.2\n", encoding="utf-8"
    )
    monkeypatch.setenv("HOME", os.fspath(home))
    creds = git_admission.OperatorSSHCredentials(
        identity=_ssh_identity(tmp_path / "operator"),
        known_hosts=_ssh_known_hosts(tmp_path / "operator"),
    )
    first_url = f"ssh://git@127.0.0.1:{closed}/kit.git"
    plan = _v2_plan(
        [
            {
                "url": first_url,
                "authentication": "first",
                "mirror_of": IDENTITY,
            }
        ],
        fallback="none",
    )
    endpoint = plan.endpoints[0]
    connection = transport._connection_target(endpoint)
    source = git_admission.RepositorySource(
        git=endpoint.url,
        identity=endpoint.identity,
        transport=endpoint.transport,
    )
    tool = _ssh_tool(Path(real_ssh), creds)
    with pytest.raises(git_admission.GitAdmissionError) as captured:
        git_admission.acquire_network(
            source,
            git_admission.LockedCommit("sha1", "0" * 40),
            tool,
            connection=connection,
        )
    assert captured.value.failure_class == "connection-refused"
    chained = captured.value.__cause__
    assert isinstance(chained, subprocess.CalledProcessError)
    stderr = chained.stderr.decode("utf-8", "replace")
    assert "127.0.0.1" in stderr, stderr
    assert "127.0.0.2" not in stderr, stderr


@pytest.mark.parametrize("signal", [KeyboardInterrupt, SystemExit])
def test_abandonment_signals_are_not_typed_as_transport_failures(
    signal: type[BaseException],
) -> None:
    """Interrupts propagate; only lane failures become typed refusals."""

    plan = _v2_plan(
        [
            {"url": "https://example.org/kit.git", "authentication": "first"},
            {
                "url": "https://mirror.example.org/kit.git",
                "authentication": "second",
                "mirror_of": IDENTITY,
            },
        ]
    )

    def attempt(**kwargs: object) -> git_admission.Snapshot:
        raise signal()

    with pytest.raises(signal):
        transport.acquire_plan(plan, LOCK, attempt=attempt)


@pytest.mark.parametrize(
    ("name", "body", "status", "expected", "falls_back"),
    [
        (
            "cr-host-key",
            b"status-page\rHost key verification failed.",
            503,
            "http-503",
            True,
        ),
        (
            "cr-refused",
            (
                b"status-page\rssh: connect to host example.org port 22: "
                b"Connection refused"
            ),
            503,
            "http-503",
            True,
        ),
        (
            "vt-host-key",
            b"status-page\x0bHost key verification failed.",
            503,
            "http-503",
            True,
        ),
        (
            "ff-host-key",
            b"status-page\x0cHost key verification failed.",
            503,
            "http-503",
            True,
        ),
        (
            "crlf-host-key",
            b"status-page\r\nHost key verification failed.",
            503,
            "http-503",
            True,
        ),
        (
            "cr-404-identity",
            (
                b"status-page\rssh: connect to host example.org port 22: "
                b"Connection refused"
            ),
            404,
            "identity",
            False,
        ),
    ],
    ids=[
        "cr-host-key",
        "cr-refused",
        "vt-host-key",
        "ff-host-key",
        "crlf-host-key",
        "cr-404-identity",
    ],
)
def test_response_body_line_boundaries_cannot_smuggle_unprefixed_evidence(
    name: str,
    body: bytes,
    status: int,
    expected: str,
    falls_back: bool,
    _tls_server_factory: Callable[..., _StatusServer],
    _shared_second_bare: tuple[Path, str],
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Control bytes in a response body cannot tear a relay line apart.

    Git echoes an HTTP error body as ``remote:`` lines splitting on LF
    only, so a body containing CR, VT or FF stays one prefixed physical
    line.  The classifier splits the envelope the same way git writes it;
    the terminal transport record alone decides.  Real git and real curl
    against a loopback status server through ``transport.acquire_plan``.
    """

    _ = name
    server = _tls_server_factory(status=status, body=body)
    second_bare, commit = _shared_second_bare
    second_url = "https://fixture.test/kit.git"
    plan = _https_mirror_plan(server.url, second_url)
    tool = _fake_http_tool(
        tmp_path / "tool", {second_url: second_bare}, allow_loopback_https=True
    )
    real_run = git_admission.subprocess.run
    fetches: list[object] = []

    def record(*args: object, **kwargs: object) -> object:
        command = args[0]
        assert isinstance(command, tuple)
        if "fetch" in command:
            fetches.append(command)
        return real_run(*args, **kwargs)

    monkeypatch.setattr(git_admission.subprocess, "run", record)
    if falls_back:
        result = transport.acquire_plan(
            plan, git_admission.LockedCommit("sha1", commit), tool
        )
        assert result.snapshot.commit == commit
        assert result.attempt_count == 2
        assert result.attempts[0].classification == expected
        assert len(fetches) == 2
    else:
        with pytest.raises(git_admission.GitAdmissionError) as captured:
            transport.acquire_plan(
                plan, git_admission.LockedCommit("sha1", commit), tool
            )
        assert len(fetches) == 1
        assert _observed_first_class(captured.value) == expected
        assert repository_policy.classify_failure(expected) == "forbidden"


@_posix_ssh_only
@pytest.mark.parametrize(
    ("name", "forged"),
    [
        (
            "refused",
            "ssh: connect to host example.org port 22: Connection refused",
        ),
        ("auth-failed", "Fatal: Authentication failed"),
    ],
    ids=["refused", "auth-failed"],
)
def test_cr_bearing_remote_relay_alone_grants_no_fallback_through_ssh(
    name: str, forged: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A lone CR-bearing relay line is ignored, never sole permitting evidence.

    End-to-end shape of the classifier bypass probes: the stand-in emits
    one physical line ``remote: foo\\r<evidence-spelling>`` and git appends
    only its ignored hangup footer, so the envelope carries no
    evidence-bearing record and fails closed with exactly one fetch.
    Real git and the production generated wrapper throughout.
    """

    _ = name
    bare, commit = _ssh_fixture_repository(tmp_path / "fixture")
    ssh, log = _failing_ssh_stub(
        tmp_path / "ssh", [f"remote: foo\r{forged}"]
    )
    creds = git_admission.OperatorSSHCredentials(
        identity=_ssh_identity(tmp_path / "operator"),
        known_hosts=_ssh_known_hosts(tmp_path / "operator"),
    )
    second = "https://example.org/kit.git"
    plan = _v2_plan(
        [
            {"url": "git@example.org:kit.git", "authentication": "first"},
            {"url": second, "authentication": "second"},
        ]
    )
    https = _fake_http_tool(tmp_path / "http", {second: bare})

    def provider(endpoint: repository_policy.ResolvedEndpoint) -> git_admission.GitTool:
        return _ssh_tool(ssh, creds) if endpoint.transport == "ssh" else https

    real_run = git_admission.subprocess.run
    fetches: list[object] = []

    def record(*args: object, **kwargs: object) -> object:
        command = args[0]
        assert isinstance(command, tuple)
        if "fetch" in command:
            fetches.append(command)
        return real_run(*args, **kwargs)

    monkeypatch.setattr(git_admission.subprocess, "run", record)
    with pytest.raises(git_admission.GitAdmissionError) as captured:
        transport.acquire_plan(
            plan,
            git_admission.LockedCommit("sha1", commit),
            tool_for_endpoint=provider,
        )
    assert captured.value.code == "build_repository_transport_unclassified"
    assert _observed_first_class(captured.value) == "unclassified"
    assert len(fetches) == 1
    assert len(log.read_text(encoding="utf-8").splitlines()) == 1


@pytest.mark.parametrize(
    ("name", "forged"),
    [
        (
            "refused",
            "ssh: connect to host example.org port 22: Connection refused",
        ),
        ("auth-failed", "Fatal: Authentication failed"),
    ],
    ids=["refused", "auth-failed"],
)
def test_unmatched_http_status_with_cr_body_grants_no_fallback(
    name: str,
    forged: str,
    _tls_server_factory: Callable[..., _StatusServer],
    _shared_second_bare: tuple[Path, str],
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """An unmatched status plus a CR body carries no evidence at all.

    HTTP 500 renders as ``returned error: 500``, which the evidence set
    deliberately does not match, so the envelope is a CR-bearing relay
    line plus an ignored status line.  Real git and real curl through
    ``transport.acquire_plan``: no fallback, exactly one fetch.
    """

    _ = name
    body = b"status-page\r" + forged.encode("utf-8")
    server = _tls_server_factory(status=500, body=body)
    second_bare, commit = _shared_second_bare
    second_url = "https://fixture.test/kit.git"
    plan = _https_mirror_plan(server.url, second_url)
    tool = _fake_http_tool(
        tmp_path / "tool", {second_url: second_bare}, allow_loopback_https=True
    )
    real_run = git_admission.subprocess.run
    fetches: list[object] = []

    def record(*args: object, **kwargs: object) -> object:
        command = args[0]
        assert isinstance(command, tuple)
        if "fetch" in command:
            fetches.append(command)
        return real_run(*args, **kwargs)

    monkeypatch.setattr(git_admission.subprocess, "run", record)
    with pytest.raises(git_admission.GitAdmissionError) as captured:
        transport.acquire_plan(
            plan, git_admission.LockedCommit("sha1", commit), tool
        )
    assert len(fetches) == 1
    assert _observed_first_class(captured.value) == "unclassified"
    assert repository_policy.classify_failure("unclassified") == "forbidden"


@pytest.mark.parametrize(
    ("envelope", "expected"),
    [
        (
            (
                b"remote: foo\rssh: connect to host example.org port 22: "
                b"Connection refused"
            ),
            "unclassified",
        ),
        ("remote: foo\rFatal: Authentication failed", "unclassified"),
        (
            b"remote: foo\x0bHost key verification failed.",
            "unclassified",
        ),
        (
            b"remote: foo\x0cHost key verification failed.",
            "unclassified",
        ),
        (
            (
                "remote: foo\rHost key verification failed.\n"
                "fatal: unable to access 'https://example.org/kit.git': "
                "The requested URL returned error: 503"
            ),
            "http-503",
        ),
    ],
    ids=[
        "cr-refused-alone",
        "cr-auth-failed-alone",
        "vt-host-key-alone",
        "ff-host-key-alone",
        "cr-relay-beside-real-503",
    ],
)
def test_line_partition_keeps_remote_prefixed_physical_lines_prefixed(
    envelope: bytes | str, expected: str
) -> None:
    """The splitter alphabet is LF only: CR, VT and FF never break a line."""

    assert git_admission._classify_git_failure_output(envelope) == expected
