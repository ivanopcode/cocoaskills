from __future__ import annotations

import errno
import json
import os
import random
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from csk import cli, config

REAL_HTTP_PUBLISH_RECORD = cli.audit_registry.http_publish_record
from csk.audit.backend_config import parse_backend_config
from csk.audit.backends.base import AuditRequest
from csk.audit.backends.codex_backend import CodexBackend
from csk.audit.backends.command_backend import CommandBackend
from csk.audit.capabilities import CapabilityManifest


@pytest.fixture
def publish_context(tmp_path, monkeypatch):
    home = tmp_path / "manager"
    home.mkdir()
    cfg = config.GlobalConfig(
        path=home / "config.json", skills_root=tmp_path / "skills", preferred_locale=None,
        default_agents=["codex_cli"], adapter_mode="auto", worktree_alias_pattern="[A-Z]+-[0-9]+",
        projects={},
    )
    config.save_config(cfg)
    monkeypatch.setenv("CSK_CONFIG", str(cfg.path))
    monkeypatch.delenv("CSK_SYSTEM_CONFIG", raising=False)
    monkeypatch.delenv("CSK_REGISTRY_TOKEN", raising=False)
    record = tmp_path / "record.json"
    record.write_text("{}", encoding="utf-8")
    calls = []

    def publish(url, token, data):
        calls.append((url, token, data))
        return {"seq": 1}

    monkeypatch.setattr(cli.audit_registry, "http_publish_record", publish)
    return ["audit", "--publish", str(record), "--registry", "https://registry.example"], calls


@pytest.mark.parametrize("command", ["audit", "build-https"])
@pytest.mark.parametrize("form", ["separate", "equals", "missing"])
def test_token_option_is_refused_before_dispatch(monkeypatch, capsys, command, form):
    argv = ["audit"] if command == "audit" else ["config", "build-https", "add", "git.example/team"]
    secret = "synthetic-argv-secret"
    argv += {"separate": ["--token", secret], "equals": ["--token=" + secret], "missing": ["--token"]}[form]

    def dispatch(args):
        pytest.fail("--token reached command dispatch")

    monkeypatch.setattr(cli, "_dispatch", dispatch)
    assert cli.main(argv) == cli.EXIT_CONFIG
    output = capsys.readouterr()
    assert "--token is refused" in output.err
    assert "CSK_REGISTRY_TOKEN" in output.err
    assert "--token-file" in output.err
    assert secret not in output.out + output.err
    if command == "build-https":
        assert "--token-source or --token-env" in output.err


def test_publish_token_sources_keep_secret_out_of_argv(publish_context, tmp_path, monkeypatch, capsys):
    argv, calls = publish_context
    secret = "synthetic-file-secret"
    token_file = tmp_path / "registry-token"
    token_file.write_text(secret + "\n", encoding="utf-8")
    token_file.chmod(0o600)
    monkeypatch.setenv("CSK_REGISTRY_TOKEN", "synthetic-env-secret")
    file_argv = argv + ["--token-file", str(token_file)]
    assert secret not in file_argv
    assert cli.main(file_argv) == cli.EXIT_OK
    assert calls[-1][1] == secret
    assert cli.main(argv) == cli.EXIT_OK
    assert calls[-1][1] == "synthetic-env-secret"
    output = capsys.readouterr()
    assert secret not in output.out + output.err
    assert "synthetic-env-secret" not in output.out + output.err


@pytest.mark.parametrize("source", ["git-credentials", "keyring"])
def test_build_https_token_source_preserves_source_selection(publish_context, source):
    assert cli.main(["config", "build-https", "add", "git.example/team", "--token-source", source]) == cli.EXIT_OK
    assert config.load_config().build_https[0].token == source


def test_build_https_token_env_preserves_source_selection(publish_context):
    assert cli.main(["config", "build-https", "add", "git.example/team", "--token-env", "SYNTHETIC_TOKEN"]) == cli.EXIT_OK
    assert config.load_config().build_https[0].token_env == "SYNTHETIC_TOKEN"


@pytest.mark.skipif(os.name == "nt", reason="POSIX permission bits")
@pytest.mark.parametrize("mode", [0o400, 0o600, 0o620])
def test_token_file_allows_private_read_permissions(publish_context, tmp_path, mode):
    argv, calls = publish_context
    token_file = tmp_path / "registry-token"
    token_file.write_text("synthetic-secret", encoding="utf-8")
    token_file.chmod(mode)
    assert cli.main(argv + ["--token-file", str(token_file)]) == cli.EXIT_OK
    assert calls[-1][1] == "synthetic-secret"


@pytest.mark.skipif(os.name == "nt", reason="POSIX permission bits")
@pytest.mark.parametrize("mode", [0o640, 0o604, 0o644])
def test_token_file_refuses_shared_read_permissions(publish_context, tmp_path, monkeypatch, capsys, mode):
    argv, calls = publish_context
    token_file = tmp_path / "registry-token"
    token_file.write_text("synthetic-secret", encoding="utf-8")
    token_file.chmod(mode)
    monkeypatch.setenv("CSK_REGISTRY_TOKEN", "synthetic-env-secret")
    assert cli.main(argv + ["--token-file", str(token_file)]) == cli.EXIT_CONFIG
    assert "group or others" in capsys.readouterr().err
    assert calls == []


@pytest.mark.skipif(os.name == "nt", reason="POSIX permission bits")
def test_token_file_checks_open_file_permissions(publish_context, tmp_path, monkeypatch, capsys):
    argv, calls = publish_context
    token_file = tmp_path / "registry-token"
    token_file.write_text("synthetic-secret", encoding="utf-8")
    token_file.chmod(0o600)
    original_open = os.open

    def change_before_open(path, flags, *args, **kwargs):
        if Path(path) == token_file:
            token_file.chmod(0o604)
        return original_open(path, flags, *args, **kwargs)

    monkeypatch.setattr(os, "open", change_before_open)
    assert cli.main(argv + ["--token-file", str(token_file)]) == cli.EXIT_CONFIG
    assert "group or others" in capsys.readouterr().err
    assert calls == []


@pytest.mark.skipif(os.name == "nt", reason="POSIX FIFO and nonblocking open")
def test_token_file_checks_open_file_type(publish_context, tmp_path, monkeypatch, capsys):
    argv, calls = publish_context
    token_file = tmp_path / "registry-token"
    token_file.write_text("synthetic-secret", encoding="utf-8")
    token_file.chmod(0o600)
    original_open = os.open

    def swap_before_open(path, flags, *args, **kwargs):
        if Path(path) == token_file:
            token_file.unlink()
            os.mkfifo(token_file, 0o600)
        return original_open(path, flags, *args, **kwargs)

    monkeypatch.setattr(os, "open", swap_before_open)
    assert cli.main(argv + ["--token-file", str(token_file)]) == cli.EXIT_CONFIG
    assert "regular file" in capsys.readouterr().err
    assert calls == []


@pytest.mark.parametrize("kind", ["directory", "missing", "empty", "invalid-utf8"])
def test_token_file_refuses_unusable_input(publish_context, tmp_path, capsys, kind):
    argv, calls = publish_context
    token_file = tmp_path / "registry-token"
    if kind == "directory":
        token_file.mkdir()
    elif kind != "missing":
        token_file.write_bytes(b"" if kind == "empty" else b"\xff")
        token_file.chmod(0o600)
    assert cli.main(argv + ["--token-file", str(token_file)]) == cli.EXIT_CONFIG
    assert "--token-file" in capsys.readouterr().err
    assert calls == []


def test_token_file_accepts_exact_64_kib(publish_context, tmp_path):
    argv, calls = publish_context
    token_file = tmp_path / "registry-token"
    token = "x" * (64 * 1024)
    token_file.write_bytes(token.encode("utf-8"))
    token_file.chmod(0o600)
    assert cli.main(argv + ["--token-file", str(token_file)]) == cli.EXIT_OK
    assert calls[0][1] == token


@pytest.mark.parametrize("grow_before_open", [False, True])
def test_token_file_refuses_over_64_kib_before_read(
    publish_context, tmp_path, monkeypatch, capsys, grow_before_open,
):
    argv, calls = publish_context
    token_file = tmp_path / "registry-token"
    token_file.write_bytes(b"x" if grow_before_open else b"x" * (64 * 1024 + 1))
    token_file.chmod(0o600)
    original_open, original_fdopen = os.open, os.fdopen

    def open_file(path, flags, *args, **kwargs):
        if Path(path) == token_file and grow_before_open:
            token_file.write_bytes(b"x" * (64 * 1024 + 1))
        return original_open(path, flags, *args, **kwargs)

    class NoRead:
        def __init__(self, stream):
            self.stream = stream

        def __enter__(self):
            return self

        def __exit__(self, *args):
            self.stream.close()

        def fileno(self):
            return self.stream.fileno()

        def read(self, *args):
            pytest.fail("oversized token file was read")

    monkeypatch.setattr(os, "open", open_file)
    monkeypatch.setattr(os, "fdopen", lambda *args, **kwargs: NoRead(original_fdopen(*args, **kwargs)))
    assert cli.main(argv + ["--token-file", str(token_file)]) == cli.EXIT_CONFIG
    assert "64 KiB" in capsys.readouterr().err
    assert calls == []


def test_token_file_bounds_read_if_file_grows_after_fstat(publish_context, tmp_path, monkeypatch, capsys):
    argv, calls = publish_context
    token_file = tmp_path / "registry-token"
    token_file.write_bytes(b"x")
    token_file.chmod(0o600)
    original_fstat, original_fdopen = os.fstat, os.fdopen
    token_fd = None
    reads = []

    class BoundedRead:
        def __init__(self, stream):
            self.stream = stream

        def __enter__(self):
            return self

        def __exit__(self, *args):
            self.stream.close()

        def fileno(self):
            return self.stream.fileno()

        def read(self, size=-1):
            assert size == 64 * 1024 + 1, "token read must have a byte bound"
            reads.append(size)
            return self.stream.read(size)

    def fdopen(*args, **kwargs):
        nonlocal token_fd
        stream = original_fdopen(*args, **kwargs)
        token_fd = stream.fileno()
        return BoundedRead(stream)

    def grow_after_fstat(fd):
        info = original_fstat(fd)
        if fd == token_fd:
            token_file.write_bytes(b"x" * (64 * 1024 + 1))
        return info

    monkeypatch.setattr(os, "fdopen", fdopen)
    monkeypatch.setattr(os, "fstat", grow_after_fstat)
    assert cli.main(argv + ["--token-file", str(token_file)]) == cli.EXIT_CONFIG
    assert "64 KiB" in capsys.readouterr().err
    assert reads == [64 * 1024 + 1]
    assert calls == []


@pytest.mark.skipif(os.name == "nt", reason="POSIX FIFO")
def test_token_file_refuses_fifo_without_opening_it(publish_context, tmp_path, capsys):
    argv, calls = publish_context
    token_file = tmp_path / "registry-token"
    os.mkfifo(token_file, 0o600)
    assert cli.main(argv + ["--token-file", str(token_file)]) == cli.EXIT_CONFIG
    assert "regular file" in capsys.readouterr().err
    assert calls == []


@pytest.mark.skipif(os.name == "nt", reason="symlink creation may need privileges on Windows")
def test_token_file_refuses_symlink(publish_context, tmp_path, capsys):
    argv, calls = publish_context
    target = tmp_path / "private-target"
    target.write_text("synthetic-secret", encoding="utf-8")
    target.chmod(0o600)
    token_file = tmp_path / "registry-token"
    token_file.symlink_to(target)
    assert cli.main(argv + ["--token-file", str(token_file)]) == cli.EXIT_CONFIG
    assert "regular file" in capsys.readouterr().err
    assert calls == []


@pytest.mark.skipif(os.name == "nt" or not hasattr(os, "O_NOFOLLOW"), reason="POSIX no-follow open")
def test_token_file_refuses_symlink_swapped_before_open(publish_context, tmp_path, monkeypatch, capsys):
    argv, calls = publish_context
    token_file = tmp_path / "registry-token"
    target = tmp_path / "private-target"
    for path in (token_file, target):
        path.write_text("synthetic-secret", encoding="utf-8")
        path.chmod(0o600)
    original_open = os.open

    def swap_before_open(path, flags, *args, **kwargs):
        if Path(path) == token_file:
            token_file.unlink()
            token_file.symlink_to(target)
        return original_open(path, flags, *args, **kwargs)

    monkeypatch.setattr(os, "open", swap_before_open)
    assert cli.main(argv + ["--token-file", str(token_file)]) == cli.EXIT_CONFIG
    assert "--token-file" in capsys.readouterr().err
    assert calls == []


@pytest.mark.parametrize("kind", ["missing", "empty", "invalid-utf8"])
def test_explicit_token_file_never_falls_back_to_environment(publish_context, tmp_path, monkeypatch, capsys, kind):
    argv, calls = publish_context
    monkeypatch.setenv("CSK_REGISTRY_TOKEN", "synthetic-env-secret")
    token_file = tmp_path / "registry-token"
    if kind != "missing":
        token_file.write_bytes(b"" if kind == "empty" else b"\xff")
        token_file.chmod(0o600)
    assert cli.main(argv + ["--token-file", str(token_file)]) == cli.EXIT_CONFIG
    assert "--token-file" in capsys.readouterr().err
    assert calls == []


@pytest.mark.parametrize("err", [errno.ENOENT, errno.EACCES])
def test_token_file_read_error_preserves_errno_without_content(tmp_path, monkeypatch, err):
    from csk.registry_token import registry_token

    token_file = tmp_path / "registry-token"
    token_file.write_text("synthetic-secret", encoding="utf-8")
    token_file.chmod(0o600)

    def fail_open(*args, **kwargs):
        raise OSError(err, "synthetic-content-must-not-appear")

    monkeypatch.setattr(os, "open", fail_open)
    with pytest.raises(ValueError) as caught:
        registry_token(str(token_file))
    assert os.strerror(err) in str(caught.value)
    assert "synthetic-content" not in str(caught.value)
    assert str(token_file) not in str(caught.value)


def test_token_file_closes_descriptor_if_fdopen_fails(tmp_path, monkeypatch):
    from csk.registry_token import registry_token

    token_file = tmp_path / "registry-token"
    token_file.write_text("synthetic-secret", encoding="utf-8")
    token_file.chmod(0o600)
    descriptors = []

    def fail_fdopen(fd, *args, **kwargs):
        descriptors.append(fd)
        raise OSError(errno.EMFILE, "synthetic-error")

    monkeypatch.setattr(os, "fdopen", fail_fdopen)
    with pytest.raises(ValueError):
        registry_token(str(token_file))
    assert len(descriptors) == 1
    with pytest.raises(OSError) as caught:
        os.fstat(descriptors[0])
    assert caught.value.errno == errno.EBADF


@pytest.mark.parametrize("name", ["SERVICE_TOKEN", "SERVICE_KEY", "SSH_AUTH_SOCK", "GIT_CONFIG_GLOBAL", "LC_PRIVATE_TOKEN", "LC_PRIVATE_KEY"])
def test_backend_required_variables_cannot_allow_secrets(monkeypatch, name):
    from csk.audit.backends import environment

    monkeypatch.setattr(environment, "os", SimpleNamespace(name="posix", environ={name: "synthetic-secret"}))
    assert environment.backend_environment(frozenset({name})) == {}


def test_publish_missing_token_names_safe_sources(publish_context, capsys):
    argv, calls = publish_context
    assert cli.main(argv) == cli.EXIT_CONFIG
    error = capsys.readouterr().err
    assert "CSK_REGISTRY_TOKEN" in error and "--token-file" in error
    assert "requires --token or" not in error
    assert calls == []


def _request():
    return AuditRequest(
        skill="fixture", source="fixture", commit="fixture", content_sha256="sha256:" + "0" * 64,
        files={}, capabilities=CapabilityManifest.implicit_none(), contract_reference="fixture",
        response_schema={}, static_findings=(), redacted=False,
    )


def test_backend_environment_windows_case_insensitivity(monkeypatch):
    from csk.audit.backends import environment

    monkeypatch.setattr(environment, "os", SimpleNamespace(name="nt", environ={
        "Path": "original", "SystemRoot": "synthetic-system", "ComSpec": "synthetic-shell",
        "lc_messages": "C", "lc_private_key": "synthetic-key", "Git_CONFIG_COUNT": "1",
        "ssh_auth_sock": "synthetic-socket", "unknown": "synthetic-unknown",
    }))
    observed = environment.backend_environment(environment.COMMAND_REQUIRED_ENV, overrides={
        "pAtH": "override", "Config_Token": "synthetic-token", "Custom_Config": "synthetic-config",
    })
    assert observed == {"PATH": "override", "SYSTEMROOT": "synthetic-system", "COMSPEC": "synthetic-shell", "LC_MESSAGES": "C"}


@pytest.mark.parametrize("kind", ["command", "codex"])
@pytest.mark.parametrize("phase", ["extract", "canary"])
def test_backend_child_environment_is_allowlisted(tmp_path, monkeypatch, kind, phase):
    _assert_backend_child_environment_is_allowlisted(tmp_path, monkeypatch, kind, phase)


def _assert_backend_child_environment_is_allowlisted(tmp_path, monkeypatch, kind, phase):
    # All values are synthetic. No parent credential file is read by the child.
    inherited = {
        "PATH": os.environ.get("PATH", ""), "HOME": str(tmp_path), "USER": "fixture",
        "LOGNAME": "fixture", "LANG": "C", "LC_MESSAGES": "C", "TMPDIR": str(tmp_path),
        "TEMP": str(tmp_path), "TMP": str(tmp_path), "CODEX_HOME": str(tmp_path / "codex"),
        "CSK_REGISTRY_TOKEN": "synthetic-registry-secret", "OPENAI_API_KEY": "synthetic-api-secret",
        "UNRELATED_TOKEN": "synthetic-token", "UNRELATED_KEY": "synthetic-key",
        "SSH_AUTH_SOCK": "synthetic-socket", "GIT_CONFIG_COUNT": "1", "GIT_EXEC_PATH": "synthetic-git",
        "UNRELATED_SETTING": "should-not-pass", "LC_PRIVATE_TOKEN": "synthetic-locale-secret",
        "LC_PRIVATE_KEY": "synthetic-locale-key",
        "SYSTEMROOT": os.environ.get("SYSTEMROOT", ""), "COMSPEC": os.environ.get("COMSPEC", ""),
    }
    for name, value in inherited.items():
        monkeypatch.setenv(name, value)
    script = tmp_path / "child.py"
    capture = tmp_path / "capture.json"
    script.write_text(
        "import json, os, sys\n"
        "from pathlib import Path\n"
        "sys.stdin.read()\n"
        "Path(sys.argv[1]).write_text(json.dumps({'env': dict(os.environ), 'argv': sys.argv[2:]}), encoding='utf-8')\n"
        "response = json.dumps({'schema_version': 1, 'findings': []})\n"
        "if '--output-last-message' in sys.argv:\n"
        "    Path(sys.argv[sys.argv.index('--output-last-message') + 1]).write_text(response, encoding='utf-8')\n"
        "else:\n"
        "    print(response)\n",
        encoding="utf-8",
    )
    if kind == "command":
        raw = {"kind": kind, "command": [sys.executable, str(script), str(capture)], "env": {
            "LANG": "C", "CONFIG_TOKEN": "synthetic-config-secret", "GIT_CONFIG_GLOBAL": "synthetic-config",
            "SSH_AUTH_SOCK": "synthetic-config-socket", "CUSTOM_CONFIG": "should-not-pass",
        }}
        backend = CommandBackend(parse_backend_config(kind, raw, global_model=None, allow_cloud=False))
    else:
        raw = {"kind": kind, "oss": True, "local_provider": "ollama"}
        backend = CodexBackend(parse_backend_config(kind, raw, global_model=None, allow_cloud=False))
        original_argv = backend._argv
        monkeypatch.setattr(backend, "_argv", lambda **kwargs: [sys.executable, str(script), str(capture), *original_argv(**kwargs)[1:]])
    launch_env = {}
    original_run = subprocess.run

    def capture_run(*args, **kwargs):
        launch_env.update(kwargs.get("env", os.environ))
        return original_run(*args, **kwargs)

    monkeypatch.setattr(subprocess, "run", capture_run)
    if phase == "extract":
        assert backend.extract(_request(), timeout=5) == ()
    else:
        assert backend.run_canary() is False
    observed = json.loads(capture.read_text(encoding="utf-8"))
    allowed = {"PATH", "HOME", "USER", "LOGNAME", "LANG", "TMPDIR", "TEMP", "TMP"}
    if os.name == "nt":
        allowed |= {"SYSTEMROOT", "COMSPEC"}
    if kind == "codex":
        allowed.add("CODEX_HOME")
    assert all(name in allowed or name.startswith("LC_") for name in launch_env)
    # The macOS runtime adds this variable after exec, outside the launch env.
    runtime_added = {"__CF_USER_TEXT_ENCODING"} if sys.platform == "darwin" else set()
    assert all(name in allowed | runtime_added or name.startswith("LC_") for name in observed["env"])
    assert "__CF_USER_TEXT_ENCODING" not in launch_env
    forbidden = set(inherited) - allowed - {"LC_MESSAGES"}
    assert forbidden.isdisjoint(observed["env"])
    assert {name: observed["env"][name] for name in allowed} == {name: inherited[name] for name in allowed}
    assert observed["env"]["LC_MESSAGES"] == "C"
    assert not any("synthetic-" in arg for arg in observed["argv"])


@pytest.mark.parametrize("mode", ["missing-repo", "existing-repo"])
@pytest.mark.parametrize("kind", ["command", "codex"])
@pytest.mark.parametrize("phase", ["extract", "canary"])
def test_audit_backend_git_auth_boundary(tmp_path, monkeypatch, capsys, mode, kind, phase):
    # Audit source resolution needs transport auth; skill-content backends do not.
    manager = tmp_path / "manager"
    project = tmp_path / "project"
    skills = tmp_path / "skills"
    for directory in (manager, project, skills):
        directory.mkdir()
    if mode == "existing-repo":
        (skills / "sample" / ".git").mkdir(parents=True)
    (project / "Skillfile.json").write_text(json.dumps({
        "schema_version": 1,
        "skills": [{"name": "sample", "git": "https://example.invalid/sample.git", "tag": "v1"}],
    }), encoding="utf-8")
    cfg_path = manager / "config.json"
    cfg_path.write_text(json.dumps({
        "schema_version": 1, "skills_root": str(skills),
        "projects": {"app": {"path": str(project), "agents": ["codex_cli"]}},
    }), encoding="utf-8")
    monkeypatch.setenv("CSK_CONFIG", str(cfg_path))
    monkeypatch.delenv("CSK_SYSTEM_CONFIG", raising=False)
    auth = {"SSH_AUTH_SOCK": "synthetic-socket", "GIT_CONFIG_GLOBAL": "synthetic-git-credentials"}
    for name, value in auth.items():
        monkeypatch.setenv(name, value)
    capture = tmp_path / "transport.json"
    observer = tmp_path / "transport.py"
    observer.write_text(
        "import json, os, sys\nfrom pathlib import Path\n"
        "Path(sys.argv[1]).write_text(json.dumps({\n"
        "    'argv': sys.argv[2:],\n"
        "    'auth': {name: os.environ.get(name) for name in ['SSH_AUTH_SOCK', 'GIT_CONFIG_GLOBAL']}\n"
        "}), encoding='utf-8')\nsys.exit(17)\n", encoding="utf-8",
    )
    real_run = subprocess.run

    def observe_transport(argv, **kwargs):
        # Replace only the executable; retain the production argv/env semantics.
        # Stop before network access or repository mutation, portably on Windows.
        assert Path(argv[0]).stem.lower() == "git"
        return real_run([sys.executable, str(observer), str(capture), *argv[1:]], **kwargs)

    with monkeypatch.context() as transport_patch:
        transport_patch.setattr(subprocess, "run", observe_transport)
        assert cli.main(["audit", "app", "--json"]) == cli.EXIT_CONFIG
    capsys.readouterr()
    observed = json.loads(capture.read_text(encoding="utf-8"))
    assert observed["auth"] == auth
    if mode == "missing-repo":
        assert observed["argv"][:2] == ["clone", "--"]
    else:
        assert observed["argv"][0] == "-C"
        assert observed["argv"][2:4] == ["rev-parse", "--verify"]
    _assert_backend_child_environment_is_allowlisted(tmp_path, monkeypatch, kind, phase)


@pytest.mark.parametrize("source", ["file", "env"])
@pytest.mark.parametrize("invalid", ["\n", "\r", " ", "\x00", "é", "\t", ":"])
def test_publish_refuses_invalid_token_before_request(publish_context, tmp_path, monkeypatch, capsys, source, invalid):
    argv, calls = publish_context
    secret = "synthetic-private-marker" + invalid + "tail"
    if source == "file":
        path = tmp_path / "token"
        path.write_bytes(secret.encode("utf-8"))
        path.chmod(0o600)
        argv = argv + ["--token-file", str(path)]
    else:
        # os.environ cannot carry NUL; simulate the source read for that case.
        if invalid == "\x00":
            from importlib import import_module
            monkeypatch.setattr(import_module("csk.registry_token"), "os", SimpleNamespace(environ={"CSK_REGISTRY_TOKEN": secret}))
        else:
            monkeypatch.setenv("CSK_REGISTRY_TOKEN", secret)
    assert cli.main(argv) == cli.EXIT_CONFIG
    assert calls == []
    output = capsys.readouterr()
    assert "synthetic-private-marker" not in output.out + output.err


@pytest.mark.parametrize("source", ["file", "env"])
def test_publish_accepts_token_alphabet(publish_context, tmp_path, monkeypatch, source):
    argv, calls = publish_context
    token = "AZaz09-._~+/="
    if source == "file":
        path = tmp_path / "token"
        path.write_bytes((token + "\r\n").encode())
        path.chmod(0o600)
        argv = argv + ["--token-file", str(path)]
    else:
        monkeypatch.setenv("CSK_REGISTRY_TOKEN", token)
    assert cli.main(argv) == cli.EXIT_OK
    assert calls[-1][1] == token


@pytest.mark.parametrize("exception", [ValueError, OSError, cli.audit_registry.RegistryError, RuntimeError])
def test_publish_http_exception_never_echoes_header(publish_context, monkeypatch, capsys, exception):
    argv, _ = publish_context
    from test_audit_publish import _record
    Path(argv[2]).write_text(json.dumps(_record()), encoding="utf-8")
    secret = "synthetic-private-marker"
    monkeypatch.setenv("CSK_REGISTRY_TOKEN", secret)
    # Keep http_publish_record real: fail at the HTTP serialization boundary.
    def serialize(request, **kwargs):
        raise exception("Authorization: " + request.headers["Authorization"])
    monkeypatch.setattr(cli.audit_registry, "http_publish_record", REAL_HTTP_PUBLISH_RECORD)
    monkeypatch.setattr(cli.audit_registry, "_open_registry_request", serialize)
    assert cli.main(argv) == cli.EXIT_CONFIG
    output = capsys.readouterr()
    assert "cannot publish audit record" in output.err
    assert secret not in output.out + output.err


def _command_prefixes(parser, prefix=()):
    yield prefix
    for action in parser._actions:
        if isinstance(action, cli.argparse._SubParsersAction):
            for name, child in action.choices.items():
                yield from _command_prefixes(child, prefix + (name,))


@pytest.mark.parametrize("prefix", list(_command_prefixes(cli.build_parser())) + [("check",)])
@pytest.mark.parametrize("option", ["--t", "--to", "--tok", "--toke", "--token", "--token-f"])
def test_token_prefix_never_discloses_on_any_subcommand(prefix, option, capsys):
    secret = "synthetic-private-marker"
    assert cli.main([*prefix, option + "=" + secret]) == cli.EXIT_CONFIG
    output = capsys.readouterr()
    assert secret not in output.out + output.err
    assert "CSK_REGISTRY_TOKEN" in output.err


def test_publish_does_not_accept_abbreviated_registry(publish_context, monkeypatch, capsys):
    argv, calls = publish_context
    monkeypatch.setenv("CSK_REGISTRY_TOKEN", "synthetic-token")
    argv[3] = "--reg"
    assert cli.main(argv) == cli.EXIT_CONFIG
    assert calls == []
    capsys.readouterr()


def test_token_file_refuses_regular_replacement_without_nofollow(publish_context, tmp_path, monkeypatch, capsys):
    argv, calls = publish_context
    path, target = tmp_path / "token", tmp_path / "replacement"
    for file in (path, target):
        file.write_text("synthetic-token")
        file.chmod(0o600)
    original_open = os.open
    def replace_before_open(p, flags, *args, **kwargs):
        if Path(p) == path:
            os.replace(target, path)
        return original_open(p, flags, *args, **kwargs)
    monkeypatch.delattr(os, "O_NOFOLLOW", raising=False)
    monkeypatch.setattr(os, "open", replace_before_open)
    assert cli.main(argv + ["--token-file", str(path)]) == cli.EXIT_CONFIG
    assert calls == []
    assert "identity" in capsys.readouterr().err


@pytest.mark.parametrize("field", ["st_ino", "st_dev"])
def test_token_file_refuses_open_identity_mismatch(publish_context, tmp_path, monkeypatch, capsys, field):
    argv, calls = publish_context
    path = tmp_path / "token"
    path.write_text("synthetic-token")
    path.chmod(0o600)
    original = os.fstat
    def mismatched(fd):
        info = original(fd)
        fields = {name: getattr(info, name) for name in ("st_ino", "st_dev", "st_mode", "st_size")}
        fields[field] += 1
        return SimpleNamespace(**fields)
    monkeypatch.setattr(os, "fstat", mismatched)
    assert cli.main(argv + ["--token-file", str(path)]) == cli.EXIT_CONFIG
    assert calls == []
    assert "identity" in capsys.readouterr().err


def test_token_file_refuses_missing_identity_capability(publish_context, tmp_path, monkeypatch, capsys):
    argv, calls = publish_context
    path = tmp_path / "token"
    path.write_text("synthetic-token")
    path.chmod(0o600)
    original = Path.lstat
    def missing_identity(p):
        info = original(p)
        return SimpleNamespace(st_ino=0, st_dev=info.st_dev, st_mode=info.st_mode)
    monkeypatch.delattr(os, "O_NOFOLLOW", raising=False)
    monkeypatch.setattr(Path, "lstat", missing_identity)
    assert cli.main(argv + ["--token-file", str(path)]) == cli.EXIT_CONFIG
    assert calls == []
    assert "identity" in capsys.readouterr().err


def test_token_file_refuses_reparse_attribute_before_open(publish_context, tmp_path, monkeypatch, capsys):
    import stat
    argv, calls = publish_context
    path = tmp_path / "token"
    path.write_text("synthetic-token")
    path.chmod(0o600)
    original = Path.lstat
    def reparse(p):
        info = original(p)
        return SimpleNamespace(st_ino=info.st_ino, st_dev=info.st_dev, st_mode=info.st_mode,
                               st_file_attributes=stat.FILE_ATTRIBUTE_REPARSE_POINT)
    monkeypatch.setattr(Path, "lstat", reparse)
    assert cli.main(argv + ["--token-file", str(path)]) == cli.EXIT_CONFIG
    assert calls == []
    assert "regular file" in capsys.readouterr().err


@pytest.mark.parametrize("exception", [RuntimeError, OSError])
def test_token_path_expansion_error_is_shaped(publish_context, monkeypatch, capsys, exception):
    argv, calls = publish_context
    original = Path.expanduser
    def fail(p):
        if str(p) == "~missing/token":
            raise exception("synthetic-private-marker")
        return original(p)
    monkeypatch.setattr(Path, "expanduser", fail)
    assert cli.main(argv + ["--token-file", "~missing/token"]) == cli.EXIT_CONFIG
    assert calls == []
    output = capsys.readouterr()
    assert "--token-file" in output.err
    assert "synthetic-private-marker" not in output.out + output.err


@pytest.mark.parametrize("form", ["separate", "equals"])
def test_invalid_token_source_does_not_disclose_literal(form):
    # Real module entry point in a child process, mirroring the panel
    # reproduction: parsing must refuse before any config read or dispatch.
    marker = "synthetic-selector-private-marker"
    option = ["--token-source", marker] if form == "separate" else ["--token-source=" + marker]
    root = Path(__file__).resolve().parents[1]
    env = dict(os.environ, PYTHONPATH=str(root / "src"))
    proc = subprocess.run(
        [sys.executable, "-m", "csk", "config", "build-https", "add",
         "example.test/team", *option],
        cwd=root, env=env, capture_output=True, text=True, timeout=30,
    )
    assert proc.returncode == 2
    assert marker not in proc.stdout + proc.stderr
    assert "must be one of git-credentials, keyring" in proc.stderr
    assert "invalid choice" not in proc.stderr


_INVALID_TOKEN_SOURCE_FAMILIES = (
    "registry-token",
    "github-pat",
    "env-name",
    "file-path",
    "base64",
    "case-variant",
)


def _generated_invalid_selector(family: str, rng: random.Random, index: int) -> str:
    alphabet = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-._~"
    if family == "registry-token":
        return "synthetic-registry-" + "".join(rng.choice(alphabet) for _ in range(12))
    if family == "github-pat":
        return "ghp_" + "".join(rng.choice(alphabet[:62]) for _ in range(16))
    if family == "env-name":
        return "CSK_" + "".join(rng.choice("ABCDEFGHIJKLMNOPQRSTUVWXYZ_") for _ in range(8))
    if family == "file-path":
        return "/tmp/csk-token-" + "".join(rng.choice(alphabet[:62]) for _ in range(10))
    if family == "base64":
        return "".join(rng.choice(alphabet[:64]) for _ in range(24))
    bases = ("Git-Credentials", "GIT-CREDENTIALS", "Keyring", "KEYRING", "Git-credentials", "KeyRing")
    value = bases[index % len(bases)]
    if index >= len(bases):
        value += f"-x{index}"
    return value


@pytest.mark.parametrize("family", _INVALID_TOKEN_SOURCE_FAMILIES)
def test_invalid_token_source_property_never_echoes_value(family, capsys):
    # Generated class test: every invalid selector family refuses with the
    # shaped enum-only diagnostic through the production entry point, in both
    # argv forms. Deterministic seed keeps the run reproducible.
    seed = 260917 + _INVALID_TOKEN_SOURCE_FAMILIES.index(family) * 1000
    rng = random.Random(seed)
    for index in range(6):
        value = _generated_invalid_selector(family, rng, index)
        assert value not in ("git-credentials", "keyring")
        argv = ["config", "build-https", "add", "example.test/team"]
        if index % 2 == 0:
            argv += ["--token-source", value]
        else:
            argv += ["--token-source=" + value]
        assert cli.main(argv) == cli.EXIT_CONFIG
        output = capsys.readouterr()
        assert value not in output.out + output.err
        assert "must be one of git-credentials, keyring" in output.err
        assert "invalid choice" not in output.err
